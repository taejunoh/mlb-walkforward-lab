# Model Improvement H24 Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seal the outputs of the common, Lane A, Lane B, and optional Lane C workflows before any H24 label access, execute one identical-population H24 comparison, and emit an immutable promotion or `NO_PROMOTION` decision without modifying the v5.1 card.

**Architecture:** A pre-outcome registry module validates and seals the required `v5.1` baseline plus only the qualified optional definitions (`A`, `A+B`, `A+B+C`). A prediction-only module scores and ranks H24 without importing outcomes. A separate outcome-capable module opens H24 labels exactly once after every prediction seal exists, computes common metrics, and writes the final decision. An append-only phase ledger makes premature label access, post-freeze mutation, duplicate H24 execution, and same-registry retry after `OUTCOMES_OPENED` fail closed.

**Tech Stack:** Python 3.11 standard library, existing `mlb_backtest` scoring/normalization/diagnostic primitives, canonical JSON and SHA-256 artifacts, `unittest`, Ruff, Git.

---

## Coordination contract and dependency DAG

This plan starts only after the three implementation plans below have produced their tested interfaces:

- `docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md`
- `docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md`
- `docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md`

The implementation order is:

```text
common RunSpec + verified 2018-2024 source gate
  -> sealed common primitive rows and Lane A card
  -> frozen Lane B card
  -> optional frozen Lane C card plus replay-safe H24 lane_c feature seal, or explicit ABSENT disposition
  -> pre-H24 candidate registry seal
  -> H24 prediction seals for every registered model
  -> OUTCOMES_OPENED transition exactly once
  -> identical-population outcome join and comparison
  -> immutable promotion decision or NO_PROMOTION
```

Real H22, H23, or H24 execution remains prohibited until the common source gate proves the missing 2018–2021 minimum inputs. Unit tests use synthetic fixtures and do not relax that prerequisite.

The prerequisite is not a new ad-hoc downloader. Resume the already specified, content-addressed V5.2 acquisition/finalization workflow in `docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md` and `docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md`. Only their authenticated `SOURCES_READY` terminal inventory may supply the exact paths and digests used to publish the real model-improvement RunSpec. Until that evidence exists, execute implementation and synthetic tests only.

## File ownership

Create these files:

- `schemas/model_improvement/candidate_registry.schema.json`
- `schemas/model_improvement/h24_comparison.schema.json`
- `mlb_backtest/model_improvement/candidate_registry.py`
- `mlb_backtest/model_improvement/h24_predictions.py`
- `mlb_backtest/model_improvement/h24_outcomes.py`
- `mlb_backtest/model_improvement/final_gate.py`
- `mlb_backtest/model_improvement/h24_controller.py`
- `scripts/run_model_improvement_h24.py`
- `tests/fixtures/model_improvement/h24/eligible_rows.jsonl`
- `tests/fixtures/model_improvement/h24/outcomes.jsonl`
- `tests/test_model_improvement_registry.py`
- `tests/test_model_improvement_h24_predictions.py`
- `tests/test_model_improvement_h24_outcomes.py`
- `tests/test_model_improvement_final_gate.py`
- `tests/test_model_improvement_h24_controller.py`

Do not modify `config/v5_1_bt.json`, any file below `audit/v5_1_bt/`, any existing v5.1 model card, or any live/experimental daily output.

Common/Lane A owns `ModelImprovementRunSpec`, `SourceGateReceipt`, `FeatureSeal`, `SealedFeatureRow`, `FrozenModelCard`, `LaneDisposition`, `PredictionRow`, and `PredictionSeal` in the Common modules. This plan must import those definitions and must not redefine them. This plan owns `RegisteredCandidate`, `CandidateRegistry`, `H24PredictionManifest`, `H24Phase`, `H24LedgerState`, `OutcomeCapability`, `H24OutcomeRow`, `EvaluatedPredictionRow`, `H24Comparison`, `FinalVerificationReceipt`, and `PromotionDecision` in the modules listed above.

### Task 1: Define the closed candidate-registry schema

**Files:**
- Create: `schemas/model_improvement/candidate_registry.schema.json`
- Create: `tests/test_model_improvement_registry.py`
- Create: `mlb_backtest/model_improvement/candidate_registry.py`

- [ ] Add a failing test that constructs a fully qualified registry with model IDs in any order and expects canonical order `v5.1`, `A`, `A+B`, `A+B+C`. Also test the valid baseline-only registry. Reject unknown IDs, a missing `v5.1`, more than one card per ID, `A+B` without `A`, and `A+B+C` without `A+B`.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_registry.CandidateRegistryContractTest -v` and confirm the import or assertion fails.
- [ ] Define frozen dataclasses with these public signatures:

```python
@dataclass(frozen=True)
class RegisteredCandidate:
    model_id: Literal["v5.1", "A", "A+B", "A+B+C"]
    card_path: str
    card_sha256: str
    feature_seal_path: str
    feature_seal_sha256: str
    prediction_contract_sha256: str
    ranking_changes: bool

@dataclass(frozen=True)
class CandidateRegistry:
    schema_version: Literal["MODEL_IMPROVEMENT_CANDIDATE_REGISTRY_V1"]
    run_spec_sha256: str
    source_gate_sha256: str
    h24_population_sha256: str
    candidates: tuple[RegisteredCandidate, ...]

def build_candidate_registry(
    run_spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    baseline_replay: BaselineReplayReceipt,
    lane_a_card: FrozenModelCard | None,
    lane_b_card: FrozenModelCard | None,
    lane_c_card: FrozenModelCard | None,
    lane_dispositions: Mapping[str, LaneDisposition],
    h24_population_sha256: str,
) -> CandidateRegistry:
    """Validate every parent binding and return the sole pre-H24 registry value."""

def canonical_registry_bytes(registry: CandidateRegistry) -> bytes:
    """Return canonical JSON bytes using the Common artifact serializer."""

def seal_candidate_registry(registry: CandidateRegistry, output_path: Path) -> str:
    """Exclusively create, reread, and return the registry SHA-256."""

def load_candidate_registry(path: Path, expected_sha256: str) -> CandidateRegistry:
    """Reread a regular file, verify its digest and closed schema, and parse it."""
```

- [ ] Implement the closed JSON Schema with `additionalProperties: false`, exact 64-character lowercase SHA-256 fields, the four allowed IDs, and one-to-four candidate records. Every candidate binds one explicit H24 `FeatureSeal` path/digest: `v5.1`, `A`, and `A+B` bind the identical `feature_set_id="common"` seal, while `A+B+C` binds the separately sealed `feature_set_id="lane_c"` H24 rows.
- [ ] Implement canonical serialization with sorted keys, UTF-8, compact separators, and a trailing newline; use exclusive creation and reject symlinks, non-regular files, and digest mismatches on reread.
- [ ] Run the focused test and confirm all registry tests pass.
- [ ] Commit only these three files with `git commit -m "feat: seal model improvement candidate registry"`.

### Task 2: Bind candidate registry to all pre-H24 evidence

**Files:**
- Modify: `mlb_backtest/model_improvement/candidate_registry.py`
- Modify: `tests/test_model_improvement_registry.py`

- [ ] Add failing tests requiring the registry builder to verify the common RunSpec, source-gate receipt, exact v5.1 replay receipt, one Common `LaneDisposition` for each of A/B/C, the H24 common feature seal, and the optional H24 Lane C feature seal. A `QUALIFIED` disposition must have the corresponding frozen card; `ABSENT` or `REJECTED` must not. Parent dependencies are strict: B can qualify only when A qualifies, and C can qualify only when B qualifies and its H24 `lane_c` feature seal securely rereads with the card-pinned digest.
- [ ] Add tamper cases for every referenced digest, a Lane B card not derived from the registered Lane A card, a Lane C card not derived from the registered A+B card, a C card paired with a common or wrong-parent H24 feature seal, differing H24 eligible populations, and any artifact created after a supplied pre-H24 freeze boundary.
- [ ] Run the focused registry suite and confirm the new tamper cases fail.
- [ ] Add this exact final input contract and `validate_registry_inputs(inputs: RegistryInputs) -> None`:

```python
@dataclass(frozen=True)
class RegistryInputs:
    run_spec: ModelImprovementRunSpec
    source_gate: SourceGateReceipt
    baseline_replay: BaselineReplayReceipt
    common_h24_feature_seal: FeatureSeal
    lane_a_card: FrozenModelCard | None
    lane_b_card: FrozenModelCard | None
    lane_c_card: FrozenModelCard | None
    lane_c_h24_feature_seal: FeatureSeal | None
    lane_dispositions: Mapping[str, LaneDisposition]
    h24_population_sha256: str
    pre_h24_freeze_sha256: str

def validate_registry_inputs(inputs: RegistryInputs) -> None:
    """Fail closed on any card, disposition, feature, population, or freeze mismatch."""

def build_candidate_registry(inputs: RegistryInputs) -> CandidateRegistry:
    """Build the sole registry only after validate_registry_inputs succeeds."""
```

Verify exact upstream artifact hashes, parent-card hashes, fold IDs, `H23_FROZEN` states, H24 feature-seal `feature_set_id`/parent/evidence bindings, and one identical `population_sha256` across every candidate without importing any outcome module. The `A+B+C` card parameters must pin its H24 Lane C feature-seal digest; no directory scan or latest-file selection is allowed.
- [ ] Make `build_candidate_registry` accept only validated `RegistryInputs`; prove with `modulefinder` or an AST import test that `candidate_registry.py` cannot import `h24_outcomes`, `outcomes`, or an outcome fixture.
- [ ] Run the focused registry suite and confirm the valid fixture passes and every tamper fixture fails.
- [ ] Commit with `git commit -m "feat: bind pre-h24 candidate evidence"`.

### Task 3: Implement prediction-only H24 scoring and sealing

**Files:**
- Create: `mlb_backtest/model_improvement/h24_predictions.py`
- Create: `tests/test_model_improvement_h24_predictions.py`
- Create: `tests/fixtures/model_improvement/h24/eligible_rows.jsonl`

- [ ] Add a failing synthetic test for `score_h24_candidates(registry, feature_seals, cards)` that expects one prediction per `(model_id, game_pk)`, identical eligible `game_pk` populations, deterministic daily ranks, and no outcome fields in memory or serialized output. The test must prove that `A+B+C` reads its `lane_c` rows while all other models read the common rows.
- [ ] Add failure cases for duplicate `game_pk`, non-H24 dates, nonfinite probability, mismatched population hashes, post-registry card mutation, and an `A+B+C` input when absent from the registry.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_h24_predictions -v` and confirm RED.
- [ ] Implement these public functions:

```python
def score_h24_candidates(
    registry: CandidateRegistry,
    feature_seals: Mapping[str, FeatureSeal],
    cards: Mapping[str, FrozenModelCard],
) -> tuple[PredictionRow, ...]:
    """Score each registered card on its registry-pinned H24 feature seal."""

def validate_identical_prediction_population(
    rows: Sequence[PredictionRow],
    registry: CandidateRegistry,
) -> str:
    """Return the common population digest after exact per-model equality checks."""

def seal_h24_predictions(
    rows: Sequence[PredictionRow],
    output_dir: Path,
) -> H24PredictionManifest:
    """Seal one canonical prediction artifact per model and their common manifest."""
```

- [ ] Securely load each registry-pinned feature seal and rows, require one identical `game_pk` population digest, and reject a caller mapping whose key/path/digest differs from the registry. Reuse the frozen deterministic ranking key from the common plan; do not call the legacy v5.1 top-level pipeline. Serialize one canonical JSONL file and SHA-256 sidecar per model, then a population manifest binding all model and feature-seal digests.
- [ ] Add an AST import-denial test proving this module does not import `h24_outcomes`, `outcomes`, or any file below an outcome root.
- [ ] Run the prediction suite and confirm all cases pass.
- [ ] Commit with `git commit -m "feat: seal identical-population h24 predictions"`.

### Task 4: Add the append-only H24 phase ledger

**Files:**
- Create: `mlb_backtest/model_improvement/h24_controller.py`
- Create: `tests/test_model_improvement_h24_controller.py`

- [ ] Add failing tests for the only allowed phase sequence: `REGISTRY_SEALED -> PREDICTIONS_SEALED -> OUTCOMES_OPENED -> REPORT_SEALED`.
- [ ] Add failure tests for outcome access before all prediction seals, a second `OUTCOMES_OPENED`, mutation of a predecessor digest, transition overwrite, and retry under the same registry after `OUTCOMES_OPENED` even when no report exists.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_h24_controller -v` and confirm RED.
- [ ] Implement:

```python
class H24Phase(str, Enum):
    REGISTRY_SEALED = "REGISTRY_SEALED"
    PREDICTIONS_SEALED = "PREDICTIONS_SEALED"
    OUTCOMES_OPENED = "OUTCOMES_OPENED"
    REPORT_SEALED = "REPORT_SEALED"

def initialize_h24_ledger(
    registry_path: Path,
    registry_sha256: str,
    output_dir: Path,
) -> H24LedgerState:
    """Create the REGISTRY_SEALED genesis record after a secure registry reread."""

def advance_h24_phase(
    ledger: H24LedgerState,
    phase: H24Phase,
    evidence_sha256: str,
    expected_predecessor_sha256: str,
) -> H24LedgerState:
    """Append exactly one permitted transition to the digest chain."""

def authorize_h24_outcome_open(
    ledger: H24LedgerState,
    registry: CandidateRegistry,
    predictions: H24PredictionManifest,
) -> OutcomeCapability:
    """Advance OUTCOMES_OPENED once and return a process-local one-use capability."""
```

- [ ] Store each phase as an exclusive-create canonical record whose predecessor digest forms a hash chain. `OutcomeCapability` must be non-serializable and issued once only after registry and all prediction digests reread successfully.
- [ ] Make deterministic interruption resumable only before `OUTCOMES_OPENED`; after it, return a terminal `SAME_REGISTRY_RETRY_FORBIDDEN` error.
- [ ] Run the controller suite and confirm all phase and crash-point tests pass.
- [ ] Commit with `git commit -m "feat: enforce one-shot h24 phase ledger"`.

### Task 5: Join H24 outcomes on the sealed population

**Files:**
- Create: `mlb_backtest/model_improvement/h24_outcomes.py`
- Create: `tests/test_model_improvement_h24_outcomes.py`
- Create: `tests/fixtures/model_improvement/h24/outcomes.jsonl`
- Create: `schemas/model_improvement/h24_comparison.schema.json`

- [ ] Add a failing test that calls `join_h24_outcomes(capability, prediction_manifest, outcome_rows)` and expects exactly one binary home-win label for every eligible `game_pk` and every registered model.
- [ ] Add failure cases for a missing/extra/duplicate outcome, nonbinary label, game postponement ambiguity, population mismatch, prediction digest mismatch, no capability, reused capability, and any 2025 row.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_h24_outcomes -v` and confirm RED.
- [ ] Implement:

```python
def join_h24_outcomes(
    capability: OutcomeCapability,
    prediction_manifest: H24PredictionManifest,
    outcome_rows: Sequence[H24OutcomeRow],
) -> tuple[EvaluatedPredictionRow, ...]:
    """Consume the capability and join the exact sealed H24 population once."""

def evaluate_h24_models(
    rows: Sequence[EvaluatedPredictionRow],
    registry: CandidateRegistry,
) -> H24Comparison:
    """Compute full-precision all-game and date-paired comparison metrics."""
```

- [ ] Compute all-eligible-game Brier and log loss for each model. Compute Daily Top-1 correctness pairs by date after each model independently chooses its Top-1; do not join those pairs by chosen `game_pk`. Compute maximum consecutive Daily Top-1 losses with dates sorted strictly ascending.
- [ ] Store raw integer counts, full-precision metric values, signed deltas versus v5.1, paired day rows, and population digests. Reject rounded values as gate inputs.
- [ ] Implement the closed comparison schema and canonical bytes/sidecar writer.
- [ ] Run the outcome suite and confirm valid results pass and every population/capability violation fails.
- [ ] Commit with `git commit -m "feat: evaluate one-shot h24 outcomes"`.

### Task 6: Encode the final promotion gate

**Files:**
- Create: `mlb_backtest/model_improvement/final_gate.py`
- Create: `tests/test_model_improvement_final_gate.py`

- [ ] Add a table-driven failing test for the exact gate rules: Lane A must have strictly lower Brier and log loss than v5.1; other candidates must be no worse on both; ranking-changing candidates must have positive paired Daily Top-1 delta and a maximum losing streak no worse than v5.1.
- [ ] Add failing cases for an unresolved replay/source/leakage review, a missing applicable metric, a post-freeze adjustment marker, mismatched population, and a point estimate presented as significance or profitability.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_final_gate -v` and confirm RED.
- [ ] Implement:

```python
def evaluate_promotion_gate(
    comparison: H24Comparison,
    verification: FinalVerificationReceipt,
) -> PromotionDecision:
    """Return clause-level eligibility and PROMOTION_ELIGIBLE or NO_PROMOTION."""
```

`PromotionDecision` must contain per-candidate boolean clauses and reason codes, plus `decision` equal to `PROMOTION_ELIGIBLE` only when at least one candidate passes every applicable clause; otherwise it must equal `NO_PROMOTION`. It must never modify or name a replacement production card.

- [ ] Make deterministic selection among multiple eligible candidates report-only: Brier ascending, log loss ascending, Daily Top-1 delta descending, model ID ascending. State explicitly that this is a proposal ordering, not deployment authorization.
- [ ] Run the gate suite and confirm all truth-table cases pass.
- [ ] Commit with `git commit -m "feat: encode model improvement final gate"`.

### Task 7: Add the one-shot command-line controller

**Files:**
- Create: `scripts/run_model_improvement_h24.py`
- Modify: `tests/test_model_improvement_h24_controller.py`

- [ ] Add failing CLI tests requiring explicit paths for RunSpec, source gate, registry, every registry-declared H24 feature seal, every card, outcome file, and output root; reject defaults, path discovery, a missing Lane C H24 seal, a dirty card/feature digest, and a pre-existing `OUTCOMES_OPENED` record.
- [ ] Run the controller suite and confirm RED.
- [ ] Implement a thin CLI that validates all pre-outcome artifacts, seals predictions, rereads their hashes, advances the phase ledger, opens outcomes once, evaluates, writes the comparison and decision, rereads both, and advances `REPORT_SEALED`.
- [ ] Return exit `0` for a valid report, including `NO_PROMOTION`; return exit `1` for contract/preflight rejection and exit `2` for a forbidden same-registry rerun after outcomes opened. Never catch an integrity failure and retry automatically.
- [ ] Add `--dry-run-contract` that validates paths and hashes but cannot score, open outcomes, or create phase records.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_h24_controller -v` and confirm all CLI tests pass.
- [ ] Commit with `git commit -m "feat: add one-shot h24 comparison command"`.

### Task 8: Verify cross-plan contracts and legacy isolation

**Files:**
- Modify only if a verified mismatch exists: files created by Tasks 1–7 and the three sibling model-improvement plans' new files

- [ ] Run the focused integration suite:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_model_improvement_registry \
  tests.test_model_improvement_h24_predictions \
  tests.test_model_improvement_h24_controller \
  tests.test_model_improvement_h24_outcomes \
  tests.test_model_improvement_final_gate -v
```

Expected: all tests pass, no skipped contract or leakage test.

- [ ] Run every model-improvement test: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_model_improvement_*.py' -v`.
- [ ] Run legacy regression tests: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_scoring tests.test_scoring_prep tests.test_normalize tests.test_ranking tests.test_metrics tests.test_leakage -v`.
- [ ] Run `ruff check mlb_backtest/model_improvement scripts/run_model_improvement_h24.py tests/test_model_improvement_*.py` and `git diff --check`.
- [ ] Inspect `git status --short`; confirm no protected v5.1 file, experimental daily artifact, or unrelated user file was staged.
- [ ] Run the CLI only with `--dry-run-contract` against synthetic fixtures. Confirm no `OUTCOMES_OPENED` record is created.
- [ ] Commit verified integration-only fixes with `git commit -m "test: verify model improvement h24 integration"`. Do not execute a real H22/H23/H24 fold while the common 2018–2021 source gate is blocked.

## Completion criteria

- The pre-H24 registry binds all present candidates, each candidate's exact H24 feature seal, and an explicit Lane C presence/absence disposition.
- Every registered model has an H24 prediction seal over the same population before labels open.
- Outcome access is one-shot and impossible before prediction sealing.
- The final comparison uses all-game probability metrics and date-paired Daily Top-1 metrics as specified.
- `NO_PROMOTION` is a successful, valid terminal decision.
- The current v5.1 model card and all live/experimental daily paths remain unchanged.
