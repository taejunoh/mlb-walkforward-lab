# Model Improvement H24 Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seal the outputs of the common, Lane A, Lane B, and optional Lane C workflows before any H24 label access, execute one identical-population H24 comparison, and emit an immutable promotion or `NO_PROMOTION` decision without modifying the v5.1 card.

**Architecture:** A pre-outcome registry module validates and seals the required `v5.1` baseline plus only the qualified optional definitions (`A`, `A+B`, `A+B+C`). A prediction-only module scores and ranks H24 without importing outcomes. A separate outcome-capable module opens the typed H24 `OutcomeReceipt` exactly once after every prediction seal exists, computes common metrics, and writes the final decision. An append-only phase ledger makes premature outcome access, post-freeze mutation, duplicate H24 execution, and same-registry retry after `OUTCOMES_OPENED` fail closed.

**Tech Stack:** Python 3.11 standard library, existing `mlb_backtest` scoring/normalization/diagnostic primitives, canonical JSON and SHA-256 artifacts, `unittest`, Ruff, Git.

H24 consumes the shared staged contract in this order: **Task8A Common OOF FeatureSeal admission + baseline label-free seals/loaders → Task6A baseline replay receipt after its matching baseline seal → Task8B nonbaseline label-free seals/loaders after same-fold replay receipt → Task6B SourceInventory → GameEvidence outcomes, reports, and final pair**. Prediction seals are created and loaded only through `seal_oof_predictions(spec, source_gate, feature_seal, baseline_replay, ledger_id, rows)`, `load_prediction_seal(spec, source_gate, feature_seal, baseline_replay, path, expected_sha256)`, and `load_prediction_rows(spec, source_gate, feature_seal, baseline_replay, seal)`. Task6B supplies typed `OutcomeReceipt` values; common evaluation is exactly `evaluate_predictions(spec, candidate, baseline, outcome_receipt)`.

---

## Coordination contract and dependency DAG

This plan starts only after the three implementation plans below have produced their tested interfaces:

- `docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md`
- `docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md`
- `docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md`

The implementation order is:

```text
common RunSpec + verified 2018-2024 source gate
  -> sealed common primitive rows and post-evaluation Lane A final card
  -> post-evaluation Lane B final card
  -> optional post-evaluation Lane C final card plus replay-safe H24 lane_c feature seal, or explicit ABSENT disposition
  -> candidate-specific H24 ScoringModelCards + pre-H24 registry seal
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

Common/Lane A owns `ModelImprovementRunSpec`, `SourceGateReceipt`, `FeatureSeal`, `SealedFeatureRow`, `ScoringModelCard`, `FrozenModelCard`, `LaneDisposition`, `PredictionRow`, and `PredictionSeal` in the Common modules. This plan must import those definitions and must not redefine them. This plan owns `RegisteredCandidate`, `CandidateRegistry`, `H24PredictionManifest`, `H24Phase`, `H24LedgerState`, `OutcomeCapability`, `H24OutcomeRow`, `EvaluatedPredictionRow`, `H24Comparison`, `FinalVerificationReceipt`, and `PromotionDecision` in the modules listed above.

```python
from mlb_backtest.model_improvement.contracts import MODEL_IDS, ScoringModelCard, load_frozen_model_card
from mlb_backtest.model_improvement.scoring_cards import (
    load_scoring_model_card, resolve_prediction_weight_authority,
    seal_scoring_model_card,
)
from mlb_backtest.model_improvement.lane_disposition import (
    LANE_DISPOSITION_STATES, LANE_IDS, LaneDisposition, load_lane_disposition,
)
from mlb_backtest.model_improvement.run_spec import (
    ModelImprovementRunSpec, load_verified_run_spec, run_spec_sha256,
)
from mlb_backtest.model_improvement.source_gate import (
    SourceGateReceipt, load_source_gate_receipt, require_source_gate,
)
from mlb_backtest.model_improvement.sealed_features import (
    FeatureSeal, load_sealed_feature_rows,
)
```

H24 imports Common `MODEL_IDS` and the pure `ScoringModelCard` type from `contracts`, and validates every model ID against that closed set; its registry schema enumerates exactly those four values, not a local type alias. `seal_scoring_model_card`, `load_scoring_model_card`, and `resolve_prediction_weight_authority` come only from `scoring_cards`; `contracts` never imports any of them, preventing a contracts↔scoring-cards cycle. `LaneDisposition`, `LANE_IDS`, `LANE_DISPOSITION_STATES`, and the disposition loader come only from `lane_disposition`, never from `contracts`. For every nonbaseline candidate it consumes a H24 `ScoringModelCard` only through `load_scoring_model_card(spec, path, expected_sha256)` and the prior final card only through `load_frozen_model_card(spec, path, expected_sha256)`, where each path/digest is registry-pinned. `v5.1` has no generated scoring-card or final-card wrapper: its scoring identity is the existing canonical card path/digest/weights directly pinned in RunSpec and its final-card fields are null. It does not construct cards from mappings, discover files, or accept a caller-supplied descriptor in place of those pins. The real H24 controller is the only H24 boundary that may call `load_verified_run_spec` or `require_source_gate`; registry, scorer, sealer, outcome, and final-gate functions accept an already-bound RunSpec/receipt for synthetic tests and must never run provenance verification themselves.

### Task 1: Define the closed candidate-registry schema

**Files:**
- Create: `schemas/model_improvement/candidate_registry.schema.json`
- Create: `tests/test_model_improvement_registry.py`
- Create: `mlb_backtest/model_improvement/candidate_registry.py`

- [ ] Add a failing test that constructs a fully qualified registry with model IDs in any order and expects canonical order `v5.1`, `A`, `A+B`, `A+B+C`. Also test the valid baseline-only registry. Reject unknown IDs, a missing `v5.1`, more than one identity per ID, `A+B` without `A`, and `A+B+C` without `A+B`; verify the direct RunSpec baseline v5.1 path/digest/weights are exactly `35/25/20/10/5/5`, every optional H24 scoring card has the closed six-key integer `component_weights_pp` binding, its exact Common-derived H24 path plus mandatory sidecar, and every optional final card binds its declared scoring-card path/digest. Add payload-chain failures: A must be null/null; A+B must equal the securely reloaded H23 B scoring card and frozen-card selected-candidate parameter; and A+B+C must be null ID with a digest equal to its securely reloaded H23 C scoring card and present in the H24 Lane-C FeatureSeal evidence tuple. The fixture supplies no mutable/in-memory coverage object.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_registry.CandidateRegistryContractTest -v` and confirm the import or assertion fails.
- [ ] Define frozen dataclasses with these public signatures:

```python
@dataclass(frozen=True)
class RegisteredCandidate:
    model_id: str  # validate membership in Common MODEL_IDS
    scoring_model_card_path: str
    scoring_model_card_sha256: str
    frozen_model_card_path: str | None
    frozen_model_card_sha256: str | None
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
    optional_scoring_card_paths: Mapping[str, Path],
    optional_scoring_card_sha256: Mapping[str, str],
    optional_frozen_card_paths: Mapping[str, Path],
    optional_frozen_card_sha256: Mapping[str, str],
    lane_dispositions: Mapping[str, LaneDisposition],
    h24_population_sha256: str,
) -> CandidateRegistry:
    """Load optional cards by explicit path/digest, validate bindings, and seal the registry."""

def canonical_registry_bytes(registry: CandidateRegistry) -> bytes:
    """Return canonical JSON bytes using the Common artifact serializer."""

def seal_candidate_registry(registry: CandidateRegistry, output_path: Path) -> str:
    """Exclusively create, reread, and return the registry SHA-256."""

def load_candidate_registry(path: Path, expected_sha256: str) -> CandidateRegistry:
    """Reread a regular file, verify its digest and closed schema, and parse it."""
```

- [ ] Implement the closed JSON Schema with `additionalProperties: false`, exact 64-character lowercase SHA-256 fields, exactly the Common `MODEL_IDS` values, and one-to-four candidate records. Every candidate binds one explicit H24 `FeatureSeal` path/digest: `v5.1`, `A`, and `A+B` bind the identical `feature_set_id="common"` seal, while `A+B+C` binds the separately sealed `feature_set_id="lane_c"` H24 rows. The baseline record exactly copies the direct RunSpec v5.1 canonical card path/digest into `scoring_model_card_path`/`scoring_model_card_sha256` and has null frozen-card fields. Every optional record pins both the H24 scoring-card path/digest and the already qualified H23 final-card path/digest; neither identity may be omitted or discovered.
- [ ] Before sealing the registry, create or securely reload exactly one H24 `ScoringModelCard` for each qualified optional final card only through Common `seal_scoring_model_card(spec, payload)`. Common derives the only allowed primary paths: `h24_predictions/A/scoring_model_card.json`, `h24_predictions/A+B/scoring_model_card.json`, and `h24_predictions/A+B+C/scoring_model_card.json`; the registry never passes a relative path, alias, or output directory to this sealer. Each card has `lane_id="H24"`, `fold_id="H24"`, its parent is that exact final-card digest, its feature seal is the exact H24 registry feature seal, its weights equal the final card’s validated weights, and its `prediction_contract_sha256` is the RunSpec digest. The H24 A card has `candidate_id=null` and `candidate_payload_sha256=null`. For A+B, the adapter securely reloads the H23 B frozen card and its declared H23 scoring card, requires their candidate ID agreement (including the final card's selected-candidate parameter), then copies that H23 scoring card's `candidate_id` and `candidate_payload_sha256`. For A+B+C, it securely reloads the H23 C frozen/scoring cards and requires the C card's null ID plus coverage digest to be present in the registry-pinned H24 Lane-C FeatureSeal `evidence_sha256`, then copies that digest with a null ID. Thus no new coverage artifact or in-memory Lane-C coverage object is added to H24. Before the Common call the adapter also securely loads and compares the SourceGateReceipt, H24 FeatureSeal, calibration evidence, and parent final/scoring card. Common only validates closed payload/hash/path and cannot infer those semantic matches from digest strings. The baseline creates no wrapper and continues to use the direct RunSpec canonical-card reference. The registry persists the resulting scoring-card and final-card path/digest pairs together; no prediction may begin before the registry has reread both pairs.
- [ ] Implement canonical serialization with sorted keys, UTF-8, compact separators, and a trailing newline; use exclusive creation and reject symlinks, non-regular files, and digest mismatches on reread.
- [ ] Run the focused test and confirm all registry tests pass.
- [ ] Commit only these three files with `git commit -m "feat: seal model improvement candidate registry"`.

### Task 2: Bind candidate registry to all pre-H24 evidence

**Files:**
- Modify: `mlb_backtest/model_improvement/candidate_registry.py`
- Modify: `tests/test_model_improvement_registry.py`

- [ ] Add failing tests requiring the registry builder to verify the common RunSpec, source-gate receipt, exact v5.1 replay receipt, one Common `LaneDisposition` for each of A/B/C, the H24 common feature seal, and the optional H24 Lane C feature seal. A `QUALIFIED` disposition must have both the corresponding H24 scoring card and final card; `ABSENT` or `REJECTED` must have neither. Parent dependencies are strict: B can qualify only when A qualifies, and C can qualify only when B qualifies and its H24 `lane_c` feature seal securely rereads with the scoring-card-pinned digest.
- [ ] Add tamper cases for every referenced digest, a Lane B final card not derived from the registered Lane A card, a Lane C final card not derived from the registered A+B card, an H24 scoring card whose parent/fold/weights disagree with its final card, a C card paired with a common or wrong-parent H24 feature seal, differing H24 eligible populations, and any artifact created after a supplied pre-H24 freeze boundary.
- [ ] Run the focused registry suite and confirm the new tamper cases fail.
- [ ] Add this exact final input contract and `validate_registry_inputs(inputs: RegistryInputs) -> None`:

```python
@dataclass(frozen=True)
class RegistryInputs:
    run_spec: ModelImprovementRunSpec
    source_gate: SourceGateReceipt
    baseline_replay: BaselineReplayReceipt
    common_h24_feature_seal: FeatureSeal
    optional_scoring_card_paths: Mapping[str, Path]
    optional_scoring_card_sha256: Mapping[str, str]
    optional_frozen_card_paths: Mapping[str, Path]
    optional_frozen_card_sha256: Mapping[str, str]
    lane_c_h24_feature_seal: FeatureSeal | None
    lane_dispositions: Mapping[str, LaneDisposition]
    h24_population_sha256: str
    pre_h24_freeze_sha256: str

def validate_registry_inputs(inputs: RegistryInputs) -> None:
    """Fail closed on any card, disposition, feature, population, or freeze mismatch."""

def build_candidate_registry(inputs: RegistryInputs) -> CandidateRegistry:
    """Build the sole registry only after validate_registry_inputs succeeds."""
```

Verify exact upstream artifact hashes, parent-card hashes, fold IDs, `H23_FROZEN` states, H24 feature-seal `feature_set_id`/parent/evidence bindings, and one identical `population_sha256` across every candidate without importing any outcome module. `validate_registry_inputs` must first require the optional scoring/final-card path/digest maps to contain only `A`, `A+B`, and `A+B+C`, then call only `load_scoring_model_card(inputs.run_spec, inputs.optional_scoring_card_paths[model_id], inputs.optional_scoring_card_sha256[model_id])` and `load_frozen_model_card(inputs.run_spec, inputs.optional_frozen_card_paths[model_id], inputs.optional_frozen_card_sha256[model_id])` for each present candidate; reject descriptor objects passed in place of those pins. No new RegistryInputs coverage/candidate object is required: the existing loaded H23 scoring-card/frozen-card pair is the typed/path+digest authority. Verify the loaded H24 scoring card has the exact six-key `parameters.component_weights_pp`, `fold_id="H24"`, `lane_id="H24"`, its sole Common-derived path, the required H24 feature seal, and the pinned final-card digest as its parent. Independently verify each final card’s own H23 scoring-card path/digest, H23 final state, parent hierarchy, and weights; then require the H24 scoring weights/model to match that final card. Require the card payload matrix through that equality chain: A has null/null candidate fields; A+B's ID and payload digest equal the securely loaded H23 B scoring card and the B frozen-card selected-candidate parameter; A+B+C has null ID and a payload digest equal to the securely loaded H23 C scoring card and present in the secure H24 Lane-C FeatureSeal evidence tuple. Verify the direct RunSpec v5.1 `35/25/20/10/5/5` pin, A’s unchanged baseline pin, B’s selected-grid/candidate equality, and C’s byte-identical A+B inheritance. Every registered H24 `PredictionRow` and `PredictionSeal` must bind `prediction_contract_sha256 == run_spec.prediction_contract_sha256` and `scoring_model_card_sha256 == candidate.scoring_model_card_sha256`. Only the H24 `PredictionSeal` additionally binds `frozen_model_card_sha256 == candidate.frozen_model_card_sha256` for optional candidates; the baseline seal has `frozen_model_card_sha256 is None`. The `A+B+C` H24 scoring card parameters must pin its H24 Lane C feature-seal digest; no directory scan or latest-file selection is allowed.
- [ ] Make `build_candidate_registry` accept only validated `RegistryInputs`; prove with `modulefinder` or an AST import test that `candidate_registry.py` cannot import `h24_outcomes`, `outcomes`, or an outcome fixture.
- [ ] Run the focused registry suite and confirm the valid fixture passes and every tamper fixture fails.
- [ ] Commit with `git commit -m "feat: bind pre-h24 candidate evidence"`.

### Task 3: Implement prediction-only H24 scoring and sealing

**Files:**
- Create: `mlb_backtest/model_improvement/h24_predictions.py`
- Create: `tests/test_model_improvement_h24_predictions.py`
- Create: `tests/fixtures/model_improvement/h24/eligible_rows.jsonl`

- [ ] Add a failing synthetic test that calls `score_h24_candidates(spec, source_gate, registry, feature_seals)` exactly, then calls `seal_h24_predictions(spec, registry, rows, output_dir)`, and expects one prediction per `(model_id, game_pk)`, identical eligible `game_pk` populations, deterministic daily ranks, and no outcome fields in memory or serialized output. The registry pins the only permitted card paths/digests, so the test supplies no `cards` fixture or argument. Spy or inject the resolver to prove `score_h24_candidates` calls `resolve_prediction_weight_authority(spec, "v5.1", None)` exactly for baseline and calls it once for every registered optional candidate with that candidate's securely loaded H24 `ScoringModelCard`; reject an omitted call or an optional `None`. It must prove that `A+B+C` reads its `lane_c` rows while all other models read the common rows, and that sealing rejects a row whose universal contract, scoring-card digest, registry-derived final-card binding, or authenticated source-gate binding differs.
- [ ] Add failure cases for duplicate `game_pk`, non-H24 dates, nonfinite probability, mismatched population hashes, post-registry scoring-card or final-card mutation, a scoring card whose parent/weights/feature seal disagree with the pinned final card, a baseline wrapper card, and an `A+B+C` input when absent from the registry.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_h24_predictions -v` and confirm RED.
- [ ] Implement these public functions:

```python
def score_h24_candidates(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    registry: CandidateRegistry,
    feature_seals: Mapping[str, FeatureSeal],
) -> tuple[PredictionRow, ...]:
    """Load each registry-pinned card, resolve its scoring authority, then score it."""

def validate_identical_prediction_population(
    rows: Sequence[PredictionRow],
    registry: CandidateRegistry,
) -> str:
    """Return the common population digest after exact per-model equality checks."""

def seal_h24_predictions(
    spec: ModelImprovementRunSpec,
    registry: CandidateRegistry,
    rows: Sequence[PredictionRow],
    output_dir: Path,
) -> H24PredictionManifest:
    """Derive registry bindings, validate rows, then seal canonical per-model artifacts."""
```

- [ ] Securely load each registry-pinned feature seal and rows. `score_h24_candidates` must first require `source_gate.run_spec_sha256 == run_spec_sha256(spec)`, `source_gate.inventory_sha256 == spec.source_authority.inventory.sha256`, registry `source_gate_sha256 == source_gate.artifact_sha256`, and every registry-pinned `FeatureSeal.source_gate_sha256 == source_gate.artifact_sha256`; it calls Common only as `load_sealed_feature_rows(spec, source_gate, (2024,), feature_seal)`. For every optional candidate call only `load_scoring_model_card(spec, Path(candidate.scoring_model_card_path), candidate.scoring_model_card_sha256)` and `load_frozen_model_card(spec, Path(candidate.frozen_model_card_path), candidate.frozen_model_card_sha256)` after matching exact registry paths/digests; prove the scoring card is H24, has that final-card digest as parent, matches that final card’s model/weights, and is bound to the registry feature seal. `score_h24_candidates` must call `resolve_prediction_weight_authority(spec, "v5.1", None)` for baseline and must call `resolve_prediction_weight_authority(spec, candidate.model_id, loaded_h24_scoring_card)` for every optional candidate before deriving any score; `None` is valid only for v5.1, and an optional caller/card bypass fails closed. Derive edge, completeness, and 0–10 confidence only with that returned authority under the universal RunSpec prediction contract: confidence is the Common `scoring.confidence_score` call using completeness, retained component qualities, and signed core-edge agreement; no H24-specific formula is permitted. Require one identical `game_pk` population digest. Reject a caller mapping whose key/path/digest differs from the registry. `seal_h24_predictions(spec, registry, rows, output_dir)` must derive each model's scoring-card and optional final-card binding solely from the securely reread registry, validate each row's `prediction_contract_sha256 == spec.prediction_contract_sha256` and `scoring_model_card_sha256` against that derived binding, and derive (rather than accept from a caller) the optional `PredictionSeal.frozen_model_card_sha256`; baseline derives a null final-card binding. Every seal uses canonical `ledger_id="h24/{model_id}/H24"`, so each model has an unambiguous path and one seal. Each H24 `PredictionRow` and `PredictionSeal` must carry the universal contract digest and exact `scoring_model_card_sha256`; only the optional candidate seal additionally sets `frozen_model_card_sha256` to the exact registry final-card digest, while the baseline seal sets it to null. Reuse the frozen deterministic ranking key from the common plan; do not call the legacy v5.1 top-level pipeline. Serialize one canonical JSONL file and SHA-256 sidecar per model, then a population manifest binding all model, scoring-card, final-card, and feature-seal digests.
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

### Task 5: Materialize the H24 outcome receipt for the sealed population

**Files:**
- Create: `mlb_backtest/model_improvement/h24_outcomes.py`
- Create: `tests/test_model_improvement_h24_outcomes.py`
- Create: `tests/fixtures/model_improvement/h24/outcomes.jsonl`
- Create: `schemas/model_improvement/h24_comparison.schema.json`

- [ ] Add a failing test that calls the H24 outcome-receipt producer with the registry capability and prediction manifest, and expects exactly one binary home-win observation for every eligible `game_pk` and every registered model.
- [ ] Add failure cases for a missing/extra/duplicate outcome, nonbinary label, game postponement ambiguity, population mismatch, prediction digest mismatch, no capability, reused capability, and any 2025 row.
- [ ] Run `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_h24_outcomes -v` and confirm RED.
- [ ] Implement:

```python
def seal_h24_outcome_receipt(
    capability: OutcomeCapability,
    prediction_manifest: H24PredictionManifest,
    outcome_rows: Sequence[H24OutcomeRow],
) -> tuple[EvaluatedPredictionRow, ...]:
    """Consume the capability and join the exact sealed H24 population once."""

def evaluate_h24_models(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    rows: Sequence[EvaluatedPredictionRow],
    registry: CandidateRegistry,
) -> H24Comparison:
    """Call Common evaluation for each registered candidate and bind its pair."""
```

- [ ] Materialize the authenticated `OutcomeReceipt` once, then call Common `evaluate_predictions(spec, candidate_prediction_seal, baseline_prediction_seal, outcome_receipt)` for every nonbaseline registry candidate. `H24Comparison` is a registry-level index, not another evaluator: it stores each candidate's `ledger_id`, `candidate_id`, `EvaluationPair` path/digest, and named candidate/baseline report digests. It must not implement its own Brier, log loss, rank-one pairing, neutral-date handling, or observation coercion. Common owns the full-precision all-game metrics, independently selected date pairs, and maximum losing streak. Every downstream frozen-card consumer securely loads the `EvaluationPair` and checks its `outcome_receipt_path` and `outcome_receipt_sha256`; `outcomes_sha256` alone is never sufficient.
- [ ] Store raw integer counts, full-precision metric values, signed deltas versus v5.1, and only pair-derived digests. Reject rounded values as gate inputs, and reject a pair whose candidate ID/ledger ID differs from the registry record.
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

- [ ] Add failing CLI tests requiring explicit paths for RunSpec, source gate, registry, every registry-declared H24 feature seal, every card, outcome file, and output root; reject defaults, path discovery, a missing Lane C H24 seal, a dirty card/feature digest, and a pre-existing `OUTCOMES_OPENED` record. The real entrypoint test must prove that an ordinary/synthetic RunSpec is rejected before registry work, that a clean verified activation calls `require_source_gate`, and that an explicit source-gate path/digest differing in payload, resolved path, or artifact digest from that returned receipt is rejected before any prediction/output/phase-ledger write.
- [ ] Run the controller suite and confirm RED.
- [ ] Implement a thin CLI whose first actions are `spec = load_verified_run_spec(explicit_run_spec_path)`, `verified_gate = require_source_gate(spec)`, and secure `load_source_gate_receipt(explicit_source_gate_path, expected_sha256)`. Require exact canonical payload, resolved path, and artifact-digest equality between the caller-supplied receipt and `verified_gate`; after that comparison use the returned verified `spec` and receipt only. It then validates all other pre-outcome artifacts, computes `rows = score_h24_candidates(spec, verified_gate, registry, feature_seals)`, seals only with `manifest = seal_h24_predictions(spec, registry, rows, output_dir)`, rereads their hashes, advances the phase ledger, opens outcomes once, evaluates, writes the comparison and decision, rereads both, and advances `REPORT_SEALED`. `build_candidate_registry`, `score_h24_candidates`, `seal_h24_predictions`, and all downstream pure validators must not call `load_verified_run_spec` or `require_source_gate`; they verify the RunSpec/receipt bindings passed by this controller instead.
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
- Every registered model has an H24 prediction seal over the same population before the outcome receipt opens.
- Outcome access is one-shot and impossible before prediction sealing.
- The final comparison uses all-game probability metrics and date-paired Daily Top-1 metrics as specified.
- `NO_PROMOTION` is a successful, valid terminal decision.
- The current v5.1 model card and all live/experimental daily paths remain unchanged.
