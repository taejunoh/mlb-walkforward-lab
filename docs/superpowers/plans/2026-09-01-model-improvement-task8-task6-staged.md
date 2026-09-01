# Model Improvement Task8/Task6 Staged Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish an immutable, source-derived and pre-target probability
calibration authority, then persist authenticated label-free OOF predictions,
baseline replay evidence, nonbaseline predictions, and one source-derived
outcome/evaluation chain per fold.

**Architecture:** The work is deliberately staged as Task6P → Task8A V2 →
Task6A → Task8B → Task6B. Task6P owns only source-derived prior-target
calibration evidence/cards; Task8 owns only immutable label-free prediction
rows and `PredictionSealV2`; Task6A produces the replay receipt required by
nonbaseline scoring cards; Task6B alone opens target-fold outcomes through the
RunSpec-bound V5.2 game-evidence authority and publishes receipt-bound
evaluation reports/pairs. The global v5.1 2022--2024 alpha/beta and the
invented alpha=0/beta=1 normalizer are explicitly inadmissible for OOF replay.

**Tech Stack:** Python 3.11 standard library, existing model-improvement canonical/source-gate/feature-seal/scoring-card modules, `v52_snapshot_store`, `v52_game_evidence_inventory`, JSON Schema Draft 2020-12, `unittest`, Ruff, Git.

---

## Fixed implementation boundary

Implement only from the normative specification
`docs/superpowers/specs/2026-09-01-model-improvement-task8-oof-ledger-design.md`.
Do not create `JoinedLedger`, `join_oof_outcomes`, a labelled Task8 JSONL,
or a raw outcome mapping/label/source-authority-path argument. Do not run the unrelated full test
suite, download data, rewrite the audited v5.1 card, or create an H24
prediction seal. The current Task6P is admission-only: it validates an
existing derived calibration authority and does not alter the audited card.

The implementation owns these final module boundaries:

| Stage | Persistent authority | Files created | Public API admitted at the end |
| --- | --- | --- | --- |
| Task6P | pre-RunSpec `CalibrationFeatureReceiptV1` → typed SourceInventory → VerifiedGameEvidence → authenticated weights → V2 calibration admission | `calibration_feature_receipt.schema.json`, `probability_calibration_card_v2.schema.json`, `calibration_feature_receipts.py`, `probability_calibration.py`, focused tests | `CalibrationFeatureReceiptV1`, `load_calibration_feature_receipt`, `ProbabilityCalibrationCardV2`, `load_probability_calibration_card_v2` |
| Task8A V2 | Common O19–O23/H22/H23 `FeatureSeal`, direct weights plus calibration card | `prediction_seal_v2.schema.json`, `oof_ledger_v2.py`, `test_model_improvement_oof_ledger.py` | baseline-only `PredictionSealV2`, `seal_oof_predictions_v2`, `load_prediction_seal_v2`, `load_prediction_rows_v2` |
| Task6A | V2 sealed baseline prediction plus calibration card | `baseline_replay_receipt_v2.schema.json`, `baseline_replay_v2.py`, `test_model_improvement_baseline_replay.py` | `BaselineReplayReceiptV2`, `replay_v51_predictions_v2`, `load_baseline_replay_v2` |
| Task8B | same-fold V2 replay, Task7 scoring card, per-candidate calibration card | `prediction_seal_v2.schema.json`, `oof_ledger_v2.py`, and tests | all non-H24 model identities through the same three V2 Task8 APIs |
| Task6B | verified SourceInventory → GameEvidenceInventory → baseline prediction | `outcome_receipt.schema.json`, `evaluation_report.schema.json`, `evaluation_pair.schema.json`, `outcomes.py`, `evaluation.py`, `test_model_improvement_outcomes.py`, `test_model_improvement_evaluation.py` | `OutcomeReceipt`, outcome loaders, `evaluate_predictions`, report/pair loaders |

All descriptor payloads exclude their runtime `artifact_sha256` and `path`.
Every JSON primary is canonical compact UTF-8 with one newline; every sidecar
is `canonical_json_bytes({"artifact_sha256": digest})`. Immutable publication
uses `write_immutable_bytes`/`write_immutable_json`, sidecars first and the
named descriptor primary last.

### Task 0: Patch all stale documentation contracts before runtime work

**Files:**
- Modify: `docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md`
- Modify: `docs/superpowers/plans/2026-08-31-model-improvement-lane-a.md`
- Modify: `docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md`
- Modify: `docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md`
- Modify: `docs/superpowers/plans/2026-08-31-model-improvement-h24-integration.md`
- Test: `tests/test_model_improvement_oof_ledger.py` (AST/import assertions added in Task8A)

- [ ] **Step 1: Write the failing documentation-contract test that detects legacy Task8 outcome APIs and stale positional signatures.**

```python
def test_consumer_plans_name_only_the_receipt_bound_task8_task6_interfaces(self):
    texts = [path.read_text(encoding="utf-8") for path in PLAN_PATHS]
    forbidden = (
        "join_oof_outcomes(", "JoinedLedger", "load_joined_ledger(",
        "load_joined_prediction_rows(", "outcomes: Mapping", "labels:",
        "seal_oof_predictions(spec, ledger_id, rows)",
        "load_prediction_rows(spec, seal)",
        "evaluate_predictions(spec, source_gate, candidate, baseline, outcomes)",
    )
    for text in texts:
        self.assertFalse(any(token in text for token in forbidden), text)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger.OofLedgerDocumentationContractTest -v`

Expected: FAIL because the Common and Lane plans still advertise `JoinedLedger`, raw outcomes, and the superseded three-argument sealer.

- [ ] **Step 3: Replace every stale contract with the exact staged contract.**

In the Common plan replace its Task8/Task6 order with:

```text
Task6P source-derived pre-target calibration evidence/cards
Task8A V2 Common OOF FeatureSeal admission + calibrated baseline label-free seals/loaders
Task6A V2 baseline replay receipt after its matching baseline seal
Task8B candidate calibration plus nonbaseline label-free seals/loaders after same-fold replay receipt
Task6B SourceInventory → GameEvidence outcomes, reports, and final pair
```

Replace the old public import block with only these Task8/Task6 names:

```python
from mlb_backtest.model_improvement.oof_ledger_v2 import (
    load_prediction_rows_v2, load_prediction_seal_v2, seal_oof_predictions_v2,
)
from mlb_backtest.model_improvement.baseline_replay_v2 import (
    BaselineReplayReceiptV2, load_baseline_replay_v2, replay_v51_predictions_v2,
)
from mlb_backtest.model_improvement.outcomes import (
    OutcomeReceipt, load_outcome_receipt, load_outcome_rows, seal_outcome_receipt,
)
from mlb_backtest.model_improvement.evaluation import (
    EvaluationPair, EvaluationReport, evaluate_predictions,
    load_evaluation_pair, load_evaluation_report,
)
```

Replace every lane call site/documented adapter with the full Task8 call:

```python
seal_oof_predictions_v2(spec, source_gate, feature_seal, baseline_replay,
                        ledger_id, rows)
load_prediction_seal_v2(spec, source_gate, feature_seal, baseline_replay,
                        path, expected_sha256)
load_prediction_rows_v2(spec, source_gate, feature_seal, baseline_replay, seal)
```

and replace every raw-outcome evaluator call with:

```python
outcome_receipt = load_outcome_receipt(
    spec, source_gate, common_feature_seal, baseline_replay,
    outcome_receipt_path, outcome_receipt_sha256,
)
pair = evaluate_predictions(spec, candidate_prediction, baseline_prediction,
                            outcome_receipt)
```

State explicitly in Lane A/B/C/H24 sections that a downstream frozen-card
consumer receives only the full `load_evaluation_pair(spec, path, expected_sha256)` result and must check its
`outcome_receipt_path` plus `outcome_receipt_sha256`, never `outcomes_sha256`
alone. Delete all described labelled Task8 artefacts and all `JoinedLedger`
imports, rather than leaving an alternate compatibility path.

- [ ] **Step 4: Run GREEN and diff validation.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger.OofLedgerDocumentationContractTest -v && git diff --check`

Expected: PASS; the five plans describe one staged API and no raw outcome input.

- [ ] **Step 5: Commit the documentation contract.**

```bash
git add docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md \
  docs/superpowers/plans/2026-08-31-model-improvement-lane-a.md \
  docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md \
  docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md \
  docs/superpowers/plans/2026-08-31-model-improvement-h24-integration.md \
  tests/test_model_improvement_oof_ledger.py
git commit -m "docs: stage model improvement prediction and outcome contracts"
```

### Task 1: Task6P — Admit existing source-derived, pre-target probability calibration

**Files:**
- Create: `schemas/model_improvement/calibration_feature_receipt.schema.json`
- Create: `schemas/model_improvement/probability_calibration_card_v2.schema.json`
- Modify: `schemas/model_improvement/parallel_model_result_run_spec.schema.json`
- Modify: `tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json`
- Create: `mlb_backtest/model_improvement/calibration_feature_receipts.py`
- Create: `mlb_backtest/model_improvement/probability_calibration.py` (V2 executable; V1 inspection only)
- Modify: `mlb_backtest/model_improvement/run_spec.py`
- Modify: `mlb_backtest/model_improvement/source_gate.py` (public read-only `load_bound_source_gate_receipt`)
- Modify: `mlb_backtest/model_improvement/contracts.py`
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Create: `tests/test_model_improvement_probability_calibration.py`
- Create: `tests/test_model_improvement_calibration_feature_receipt.py`
- Modify: `tests/test_model_improvement_run_spec.py`

**Task6P V2 authority override (normative):** The remaining historical prose
in this Task 1 is superseded by this V2 contract. Before sealing RunSpec,
create no data but support loading a label-free `CalibrationFeatureReceiptV1`
whose closed fields are `schema, model_id, lane_id, fold_id, candidate_id,
calibration_key, target_year, training_start, training_end,
source_authority_sha256, source_inventory_sha256,
game_evidence_inventory_relative_path, game_evidence_inventory_sha256,
feature_contract_sha256, feature_code_revision, feature_rows_path,
feature_rows_sha256, feature_population_sha256`. Its `calibration_feature_rows.jsonl`
contains full canonical ordered D-1 feature rows and it binds neither RunSpec
nor weights. The RunSpec policy instead has an exact seven-entry
`calibration_feature_receipts` map, keyed C19/C20/C21/C22O/C22H/C23O/C23H,
where each entry has only canonical repository-relative `path` and lowercase
SHA-256 `sha256`. The RunSpec pins the receipt; the receipt never pins the
RunSpec, so the trust chain is acyclic.

Implement `CalibrationFeatureReceiptV1`,
`load_calibration_feature_receipt(spec, model_id, lane_id, fold_id,
candidate_id)`, `ProbabilityCalibrationCardV2`,
`load_probability_calibration_card_v2(...)`, and
`require_probability_calibration_card_v2(...)`. V1 card APIs introduced by
`2fa82cd` are inspection-only and must be rejected for execution. The V2 card
uses exactly the V2 fields in the normative design, including receipt
path/digest, `game_evidence_inventory_relative_path`, and `feature_row_sha256` /
`source_game_sha256` rows; V2 paths include the literal `/v2/` segment.
Missing receipt/card/sidecar/typed authority raises
`CalibrationAuthorityBlockedError` before output; a present primary lacking a
sidecar, or any present invalid binding, raises `ValueError`.

The loader calls only the public read-only
`load_bound_source_gate_receipt(spec, supplied_gate)`, existing typed
`v52_snapshot_store.parse_inventory`, and
`load_verified_game_evidence_inventory` (or `load_snapshot_replay_inputs`
when its typed bundle is required). It exact-pins the receipt, rechecks typed
SourceInventory/GameEvidence/D-1 game/source bindings, resolves baseline and
candidate weights only with `resolve_prediction_weight_authority`, recomputes
edge with `math.fsum`, derives `home_win` from verified game state, and then
recomputes the frozen fit. No source-to-feature producer, real receipt, or
receipt publication is claimed by this task. Task8B remains blocked for
historical candidates until its scoring card has an independent pre-target
weight-selection authority; a Task7 card digest alone is not that authority.

- [ ] **Step 1: Write RED tests for the only admitted calibration keys and
  their evidence boundary.**

The exact baseline key/pool table is C19=(O19, 2018), C20=(O20, 2018--19),
C21=(O21, 2018--20), C22O=(O22, 2018--21), C22H=(H22, 2018--21),
C23O=(O23, 2018--22), and C23H=(H23, 2018--22). Each row has an independently
verified D-1 feature provenance digest, a source-derived completed
GameEvidence outcome digest, and a recomputed direct-card edge. Test target
year insertion, future date, target-fold outcome, arbitrary mapping/path,
source inventory/GameEvidence substitution, feature/weight substitution,
duplicate/reordered row, fit-spec/alpha/beta mutation, alias, and all
persisted-path substitutions, plus every model/lane/candidate/key mapping
substitution. Add exact canonical-recomputation cases for `fit_logistic_v1`,
non-finite alpha/beta, nonpositive beta, under-minimum rows, and missing
classes. Test that only `calibration_paths` can obtain all permitted
calibration primaries/sidecars, and that direct `output_path("calibration",
...)` rejects every noncanonical shape/leaf. A missing admissible prior-outcome
authority must raise `BLOCKED_CALIBRATION_AUTHORITY_MISSING` before Task8
opens a prediction output.

- [ ] **Step 2: Define the closed card and read-only admission loader.**

Persist `MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V2` with exactly:

```text
schema, model_id, lane_id, fold_id, candidate_id, calibration_key,
target_year, training_start, training_end,
run_spec_sha256, source_gate_sha256, prediction_contract_sha256,
calibration_feature_receipt_path, calibration_feature_receipt_sha256,
weight_authority_path, weight_authority_sha256,
calibration_rows_path, calibration_rows_sha256,
calibration_population_sha256,
game_evidence_inventory_relative_path, game_evidence_inventory_sha256,
fit_spec, alpha, beta
```

For baseline the only derived files are
`baseline/v5.1/{fold_id}/v2/calibration_rows.jsonl` and
`baseline/v5.1/{fold_id}/v2/probability_calibration_card.json`, both under the
new `calibration` output namespace. Candidate files use exactly
`{lane_id}/{candidate_segment}/{fold_id}/...` under that same namespace.
Rows are canonical JSONL with exactly `game_pk`, `official_date`,
`target_year`, `edge`, `home_win`, `feature_row_sha256`, and
`source_game_sha256`; their labels never enter Task8.

Do not change `PredictionWeightAuthority.path` semantics. For the persisted
card field, Task6P securely resolves/canonicalizes baseline's authenticated
repository-relative weight path against the same repository root derivation as
`run_spec._repository_root()` and the current authenticated loader; candidate
Task7-card authority is already absolute. Compare `weight_authority_path` only
to this canonical absolute result, never to a raw descriptor `.path`.

Freeze the card identity before any path lookup: `v5.1` uses literal
`lane_id="baseline"` and null candidate; `A` uses literal `lane_id="A"` and
null candidate; `A+B` uses literal `lane_id="B"` and a required Task7-safe
candidate; and `A+B+C` uses literal `lane_id="C"` and null candidate. For
each accepted identity, `calibration_key` is exactly
`spec.probability_calibration_policy.fold_keys[fold_id]`, never a parameter or
caller choice. Candidate templates therefore use literal `A`, `B`, or `C`
lane segments, plus the canonical `_none`/safe candidate segment, rather than
the prediction-directory aliases.

Implement exactly `load_calibration_feature_receipt(spec, model_id, lane_id,
fold_id, candidate_id)`, `load_probability_calibration_card_v2(spec, model_id,
lane_id, fold_id, candidate_id)`, and
`require_probability_calibration_card_v2(spec, model_id, lane_id, fold_id,
candidate_id)`. They derive all paths and accept no caller
path/receipt/outcomes/rows/weights; securely reread the full authority before
derivation and immediately before return; and never write, build, fit, or
publish anything. Do not claim the required historical rows already exist.

The RunSpec `fit_spec` is not optional or opaque: after the canonical rows and
all authority bindings are reread, admission requires exact card/RunSpec
fit-spec equality and recomputes `alpha`/`beta` with the existing
`normalize.fit_logistic` using `algorithm="fit_logistic_v1"`, edge divisor
10.0, iterations 100, l2 1e-6, and tolerance 1e-10. Require at least two
ordered rows, classes `[0, 1]`, finite alpha, finite positive beta, and exact
canonical numeric equality with the card coefficients; any mismatch is
`ValueError`. This validates an existing authority and does not publish a new
fit.

`CalibrationAuthorityBlockedError` is a dedicated `RuntimeError`; both its
`.code` and `str(error)` are exactly
`BLOCKED_CALIBRATION_AUTHORITY_MISSING`. Missing prior-target source evidence,
rows, or card is this block and produces no V2 prediction/replay output;
present but invalid bytes/bindings remain `ValueError` evidence failures.

Future producer publication, any `fit_pre_target_calibration` flow, and any
`normalize.py` change are outside this executable plan; admission calls the
existing `fit_logistic` only to verify immutable authority.

- [ ] **Step 3: Add the RunSpec calibration policy, not speculative result
  digests.**

The immutable RunSpec adds one closed canonical object named
`probability_calibration_policy` with exact fields `schema`,
`fold_keys`, `fit_spec`, `pools`, `calibration_feature_receipts`,
`baseline_rows_relative_template`, `baseline_card_relative_template`,
`candidate_rows_relative_template`, and `candidate_card_relative_template`.
`schema` is `MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_POLICY_V1`; the exact
seven fold-key/date values, closed `fit_spec`, and templates are those in the
normative design. The `fit_spec` is exactly
`{schema: MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1,
algorithm: fit_logistic_v1, edge_divisor: 10.0, iterations: 100,
l2: 1e-6, tolerance: 1e-10, min_rows: 2, required_classes: [0, 1]}`.
Candidate templates use `{fold_id}` and canonical `{candidate_segment}`: null
`candidate_id` maps to literal `_none`, otherwise it uses the existing safe
single-segment candidate grammar. The RunSpec `outputs.namespaces` adds the
literal `calibration`, and the schema, inline fixture, RunSpec parser, and
RunSpec tests migrate together. `calibration_paths_v2(spec, identity)` is the
only caller of `spec.output_path("calibration", relative_name)` and it enforces
the exact baseline `baseline/v5.1/{fold_id}/v2/...` or candidate
`{lane_id}/{candidate_segment}/{fold_id}/v2/...` shapes/leaves. The exact
seven-key receipt map pins pre-RunSpec `CalibrationFeatureReceiptV1` primaries
by canonical repository-relative path and digest; a receipt must not bind the
RunSpec or weights. The RunSpec must not contain a future card digest: binding
a result digest into the same RunSpec that source-gates its inputs creates a
circular authority. Each emitted V2 card instead binds `run_spec_sha256` and
the SourceGate digest; V2 prediction artifacts bind the emitted card digest.

- [ ] **Step 4: Validate and commit Task6P.**

Run focused calibration/RunSpec/source-gate/contracts/canonical tests, validate
both changed/new Draft 2020-12 schemas, run Ruff on the new module/test, and
`git diff --check`.

```bash
git add schemas/model_improvement/calibration_feature_receipt.schema.json \
  schemas/model_improvement/probability_calibration_card_v2.schema.json \
  schemas/model_improvement/parallel_model_result_run_spec.schema.json \
  mlb_backtest/model_improvement/probability_calibration.py \
  mlb_backtest/model_improvement/calibration_feature_receipts.py \
  mlb_backtest/model_improvement/run_spec.py \
  mlb_backtest/model_improvement/source_gate.py \
  mlb_backtest/model_improvement/contracts.py \
  mlb_backtest/model_improvement/__init__.py \
  tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json \
  tests/test_model_improvement_probability_calibration.py \
  tests/test_model_improvement_calibration_feature_receipt.py \
  tests/test_model_improvement_run_spec.py
git commit -m "feat: admit pre-target probability calibration authority"
```

### Task 2: Task8A V2 — Admit Common OOF feature seals and bind calibrated predictions

**Files:**
- Modify: `schemas/model_improvement/feature_seal.schema.json`
- Modify: `mlb_backtest/model_improvement/run_spec.py`
- Modify: `mlb_backtest/model_improvement/sealed_features.py`
- Modify: `mlb_backtest/model_improvement/contracts.py`
- Modify: `tests/test_model_improvement_run_spec.py`
- Modify: `tests/test_model_improvement_sealed_features.py`
- Modify: `tests/test_model_improvement_contracts.py`

- [ ] **Step 1: Write RED tests for every fixed Task8 fold/year relation and the new seal field.**

```python
EXPECTED_TASK8_YEARS = {
    "O19": 2019, "O20": 2020, "O21": 2021, "O22": 2022,
    "O23": 2023, "H22": 2022, "H23": 2023,
}

def test_common_oof_paths_are_admitted_but_lane_c_oof_and_h24_predictions_are_not(self):
    for fold, year in EXPECTED_TASK8_YEARS.items():
        self.assertEqual(task8_target_year(fold), year)
        self.assertEqual(self.spec.output_path("features", f"common/{fold}/rows.jsonl").name,
                         "rows.jsonl")
    for forbidden in ("H24", "O24", "2025", "Q19"):
        with self.assertRaises(ValueError):
            task8_target_year(forbidden)
    with self.assertRaises(ValueError):
        self.spec.output_path("features", "lane_c/O19/rows.jsonl")

def test_prediction_seal_v2_payload_requires_feature_and_calibration_sha256(self):
    seal = make_prediction_seal_v2(feature_seal_sha256=SHA, calibration_card_sha256=SHA2)
    self.assertEqual(PredictionSealV2.from_payload_mapping(
        seal.as_mapping(), seal.artifact_sha256, seal.path), seal)
    with self.assertRaises(ValueError):
        PredictionSealV2.from_payload_mapping(
            seal.as_mapping() | {"feature_seal_sha256": "not-a-digest"}, SHA, "/x")
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_run_spec tests.test_model_improvement_sealed_features tests.test_model_improvement_contracts -v`

Expected: FAIL because O19–O23 feature paths are rejected and there is no
separate V2 row/seal type with the required calibration binding.

- [ ] **Step 3: Make the minimal shared-contract change.**

In `run_spec.py`, define exactly:

```python
TASK8_FOLD_TARGET_YEAR: Mapping[str, int] = MappingProxyType({
    "O19": 2019, "O20": 2020, "O21": 2021, "O22": 2022, "O23": 2023,
    "H22": 2022, "H23": 2023,
})
TASK8_FOLD_IDS = tuple(TASK8_FOLD_TARGET_YEAR)

def task8_target_year(fold_id: str) -> int:
    try:
        return TASK8_FOLD_TARGET_YEAR[fold_id]
    except KeyError as exc:
        raise ValueError("Task8 fold differs") from exc
```

Extend only the `features` branch of `ModelImprovementRunSpec.output_path` so
`("common", "O19")` through `("common", "O23")` accept exactly
`feature_seal.json` and `rows.jsonl`. Keep `fold()` unchanged: it remains
H22/H23/H24-only. In `sealed_features.py`, use `task8_target_year` for Common
OOF row year and official-date-year validation, while retaining `spec.fold()`
for all H22/H23/H24 checks; Lane C never accepts OOF folds.

Preserve the published V1 `PredictionRow`, `PredictionSeal`, schema, module,
paths, and public functions unchanged for inspection. Add separate
`PredictionRowV2`/`PredictionSealV2` types, with this required V2 seal field
between `source_gate_sha256` and `prediction_contract_sha256`:

```python
feature_seal_sha256: str
```

Task8A V2 also adds `calibration_card_sha256` to both `PredictionRowV2` and
`PredictionSealV2`; it is required, 64-hex, and equals the securely reloaded
`ProbabilityCalibrationCardV2` descriptor. Add V2 constructors, schemas, and
fixtures without changing V1 bytes. The V2 seal schema is
`MODEL_IMPROVEMENT_PREDICTION_SEAL_V2`; a V1 artifact is never an executable
Task6A/Task8B/Task6B prerequisite. Do not add a target year to `FeatureSeal`.

- [ ] **Step 4: Run GREEN and validate the changed feature schema.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_run_spec tests.test_model_improvement_sealed_features tests.test_model_improvement_contracts -v && python3 -c 'import json; from jsonschema import Draft202012Validator; Draft202012Validator.check_schema(json.load(open("schemas/model_improvement/feature_seal.schema.json")))'`

Expected: PASS; Common OOF rows are admitted only for O19–O23 and every prediction-seal payload carries the feature-seal digest.

- [ ] **Step 5: Commit Task8A shared admission.**

```bash
git add schemas/model_improvement/feature_seal.schema.json \
  mlb_backtest/model_improvement/run_spec.py \
  mlb_backtest/model_improvement/sealed_features.py \
  mlb_backtest/model_improvement/contracts.py \
  tests/test_model_improvement_run_spec.py \
  tests/test_model_improvement_sealed_features.py \
  tests/test_model_improvement_contracts.py
git commit -m "feat: admit common OOF feature seal bindings"
```

### Task 3: Task8A V2 — Implement calibrated baseline-only label-free prediction sealing and reloading

**Files:**
- Create: `schemas/model_improvement/prediction_seal_v2.schema.json`
- Create: `mlb_backtest/model_improvement/oof_ledger_v2.py`
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Create: `tests/test_model_improvement_oof_ledger.py`

- [ ] **Step 1: Write RED baseline-only tests, including the exact four-file transaction.**

```python
def test_baseline_prediction_seal_has_no_labels_and_binds_every_feature_row(self):
    seal = seal_oof_predictions_v2(
        self.spec, self.gate, self.common_o19_seal, None,
        "baseline/v5.1/O19", self.baseline_rows,
    )
    self.assertEqual(seal.feature_seal_sha256, self.common_o19_seal.artifact_sha256)
    self.assertEqual(load_prediction_rows_v2(
        self.spec, self.gate, self.common_o19_seal, None, seal),
        self.baseline_rows,
    )
    payload = json.loads(Path(seal.path).read_text())
    self.assertFalse({"label", "outcome", "result", "won", "score"} & set(payload))

def test_baseline_four_file_crash_recovery_has_only_final_seal_commit_point(self):
    with patch("mlb_backtest.model_improvement.oof_ledger_v2.write_immutable_bytes",
               side_effect=OSError("row primary failure")):
        with self.assertRaises(OSError):
            seal_oof_predictions_v2(self.spec, self.gate, self.common_o19_seal, None,
                                 "baseline/v5.1/O19", self.baseline_rows)
    paths = prediction_paths_v2(self.spec, "baseline/v5.1/O19")
    self.assertTrue(paths.rows_sidecar.exists())
    self.assertTrue(paths.seal_sidecar.exists())
    self.assertFalse(paths.rows.exists())
    self.assertFalse(paths.seal.exists())
```

Also cover sorted `(official_date, scheduled_start_utc, game_pk)` input,
nonempty unique full population tuples, exact population preimage, wrong
feature-row hash, source gate/inventory/RunSpec/contract substitution, wrong
baseline card path/digest/weights, `H24` rejection, and lexical `../`, `//`,
relative, wrong-leaf path failure before `read_regular_bytes` is called.

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger -v`

Expected: FAIL with `ModuleNotFoundError: mlb_backtest.model_improvement.oof_ledger_v2`.

- [ ] **Step 3: Implement the closed schema, exact identities, and baseline transaction.**

Create the V2 schema with exactly the two closed definitions from the approved
spec: `prediction_row_v2` has the 15 `PredictionRowV2.as_mapping()` fields and
no result field; `prediction_seal_v2` has exactly
`schema`, `ledger_id`, `fold_id`, `model_id`, `candidate_id`,
`run_spec_sha256`, `source_gate_sha256`, `feature_seal_sha256`,
`prediction_contract_sha256`, `scoring_model_card_sha256`,
`calibration_card_sha256`, `population_sha256`, `frozen_model_card_sha256`,
`rows_sha256`.

In `oof_ledger_v2.py`, make identity parsing and derived paths private and pure;
keep the published V1 module untouched:

```text
@dataclass(frozen=True, slots=True)
class PredictionPaths:
    rows: Path
    rows_sidecar: Path
    seal: Path
    seal_sidecar: Path

prediction_paths_v2(spec, ledger_id) -> PredictionPaths
seal_oof_predictions_v2(spec, source_gate, feature_seal, baseline_replay, ledger_id, rows) -> PredictionSealV2
load_prediction_seal_v2(spec, source_gate, feature_seal, baseline_replay, path, expected_sha256) -> PredictionSealV2
load_prediction_rows_v2(spec, source_gate, feature_seal, baseline_replay, seal) -> tuple[PredictionRowV2, ...]
```

Task8A rejects a non-`None` replay and all nonbaseline identities before a
path is calculated. Reauthenticate the source gate by loading only
`spec.output_path("source_gate", "source_gate_receipt.json")`, then compare
the full descriptor, RunSpec digest, and inventory digest. Reload the Common
FeatureSeal at its exact `features/common/{fold}/feature_seal.json` path and
load all rows through `load_sealed_feature_rows(spec, gate, (year,), seal)`.
Require a one-for-one match of every feature row to every prediction row by
game PK, official date, scheduled UTC, target year, and `feature_row_sha256`.

For v5.1, secure-read the direct baseline weight authority and derive/reload
the exact seven-key (`C19`, `C20`, `C21`, `C22O`, `C22H`, `C23O`, `C23H`)
`ProbabilityCalibrationCardV2` before materializing any row.
Compare the weight/card source gate/contract/pool/provenance bindings and use
the card alpha/beta in `home_probability`; require its digest in every row and
seal. `alpha=0,beta=1` and the global 2022--2024 calibration are rejection
cases. Set `frozen_model_card_sha256=None`.

Serialize rows exactly as:

```python
rows_bytes = b"".join(canonical_json_bytes(row.as_mapping()) for row in rows)
population = [{"game_pk": row.game_pk, "official_date": row.official_date,
               "target_year": row.target_year} for row in rows]
```

Perform preflight checks on all four files, then write in this exact order:

```python
write_immutable_bytes(paths.rows_sidecar, canonical_json_bytes({"artifact_sha256": rows_sha}))
write_immutable_bytes(paths.seal_sidecar, canonical_json_bytes({"artifact_sha256": seal_sha}))
write_immutable_bytes(paths.rows, rows_bytes)
write_immutable_bytes(paths.seal, seal_bytes)
```

Only the final line commits. The loaders rerun every input and card check,
rederive paths from the identity, verify both sidecars, enforce canonical
JSONL, and reject an uncommitted row-primary orphan.

- [ ] **Step 4: Run GREEN, schema validation, AST boundary test, and lint.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_sealed_features tests.test_model_improvement_source_gate tests.test_model_improvement_scoring_cards tests.test_model_improvement_canonical -v && python3 -c 'import json; from jsonschema import Draft202012Validator; Draft202012Validator.check_schema(json.load(open("schemas/model_improvement/prediction_seal_v2.schema.json")))' && python3 -m ruff check mlb_backtest/model_improvement/oof_ledger_v2.py mlb_backtest/model_improvement/contracts.py mlb_backtest/model_improvement/run_spec.py mlb_backtest/model_improvement/sealed_features.py tests/test_model_improvement_oof_ledger.py`

Expected: PASS; the AST test confirms `oof_ledger_v2.py` imports neither
`outcomes`, `evaluation`, a metric evaluator, nor a legacy top-level pipeline.
The calibration loader is an explicit typed authority and does not expose raw
label rows to Task8.

- [ ] **Step 5: Commit Task8A.**

```bash
git add schemas/model_improvement/prediction_seal_v2.schema.json \
  mlb_backtest/model_improvement/oof_ledger_v2.py \
  mlb_backtest/model_improvement/__init__.py \
  tests/test_model_improvement_oof_ledger.py
git commit -m "feat: seal baseline label-free OOF predictions"
```

### Task 4: Task6A — Seal an exact calibrated baseline replay receipt after prediction publication

**Files:**
- Create: `schemas/model_improvement/baseline_replay_receipt_v2.schema.json`
- Create: `mlb_backtest/model_improvement/baseline_replay_v2.py`
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Create: `tests/test_model_improvement_baseline_replay.py`

- [ ] **Step 1: Write RED tests for independent replay equality and receipt visibility.**

```python
def test_replay_receipt_is_published_only_after_matching_baseline_prediction(self):
    receipt = replay_v51_predictions_v2(self.spec, self.gate, self.common_o19_seal)
    seal = load_prediction_seal_v2(self.spec, self.gate, self.common_o19_seal, None,
                                receipt.prediction_seal_path,
                                receipt.prediction_seal_sha256)
    self.assertEqual(receipt.feature_seal_sha256, self.common_o19_seal.artifact_sha256)
    self.assertEqual(receipt.population_sha256, seal.population_sha256)
    self.assertEqual(receipt.replay_one_rows_sha256, receipt.replay_two_rows_sha256)
    self.assertEqual(receipt.replay_one_rows_sha256, seal.rows_sha256)

def test_replay_receipt_rejects_changed_second_replay_before_receipt_primary(self):
    with patch("mlb_backtest.model_improvement.baseline_replay_v2._materialize_v51_rows",
               side_effect=(self.rows, self.rows[:-1])):
        with self.assertRaisesRegex(ValueError, "independent replay"):
            replay_v51_predictions_v2(self.spec, self.gate, self.common_o19_seal)
```

Also test stale feature/source/card/contract population bindings, receipt
sidecar-without-primary, receipt primary without exact baseline seal/rows,
absolute alias rejection, and immutable receipt conflict.

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_baseline_replay -v`

Expected: FAIL with `ModuleNotFoundError: mlb_backtest.model_improvement.baseline_replay_v2`.

- [ ] **Step 3: Implement the receipt type, schema, replay, and secure loader.**

Define the V2 frozen descriptor exactly; the canonical payload field `schema`
is the literal `MODEL_IMPROVEMENT_BASELINE_REPLAY_RECEIPT_V2` and the runtime
descriptor-only fields `artifact_sha256` and `path` are excluded from it:

```python
@dataclass(frozen=True, slots=True)
class BaselineReplayReceiptV2:
    schema: str
    ledger_id: str
    fold_id: str
    model_id: str
    candidate_id: None
    run_spec_sha256: str
    source_gate_sha256: str
    feature_seal_sha256: str
    prediction_contract_sha256: str
    scoring_model_card_sha256: str
    calibration_card_path: str
    calibration_card_sha256: str
    population_sha256: str
    replay_one_rows_sha256: str
    replay_two_rows_sha256: str
    replay_one_bytes_sha256: str
    replay_two_bytes_sha256: str
    prediction_rows_sha256: str
    prediction_seal_path: str
    prediction_seal_sha256: str
    artifact_sha256: str
    path: str
```

`replay_v51_predictions_v2(spec, source_gate, feature_seal)` accepts only a
Common Task8 fold. It securely loads the source gate/FeatureSeal/feature rows,
the direct weight authority, and the exact pre-target
`ProbabilityCalibrationCardV2`; it materializes two independent in-memory v5.1
`tuple[PredictionRowV2, ...]` streams using that card's alpha/beta, compares
canonical bytes, each mapping, row digest and population digest exactly, then
calls `seal_oof_predictions_v2` exactly once. The receipt repeats the derived calibration path
and digest and its loader secure-reloads the card before replaying either
stream. A V1 baseline seal/receipt is inadmissible.

```python
seal = seal_oof_predictions_v2(spec, gate, common_feature_seal, None,
                               f"baseline/v5.1/{fold_id}", replay_one_rows)
```

Derive only `oof/baseline/v5.1/{fold}/v2/baseline_replay_receipt.json` under
the `oof/baseline` V2 namespace. Write its sidecar before its primary. The loader derives
that path, validates the closed canonical schema and sidecar, then reloads the
named Task8A seal and rows through their full Task8A API and compares every
receipt binding, including both equal replay stream digests.

- [ ] **Step 4: Run GREEN and validate the new schema.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_baseline_replay tests.test_model_improvement_oof_ledger tests.test_model_improvement_contracts tests.test_model_improvement_sealed_features tests.test_model_improvement_source_gate tests.test_model_improvement_canonical -v && python3 -c 'import json; from jsonschema import Draft202012Validator; Draft202012Validator.check_schema(json.load(open("schemas/model_improvement/baseline_replay_receipt_v2.schema.json")))' && python3 -m ruff check mlb_backtest/model_improvement/baseline_replay_v2.py tests/test_model_improvement_baseline_replay.py`

Expected: PASS; an immutable calibrated baseline V2 seal is a prerequisite and
its replay receipt is the only Task6A commit point.

- [ ] **Step 5: Commit Task6A.**

```bash
git add schemas/model_improvement/baseline_replay_receipt_v2.schema.json \
  mlb_backtest/model_improvement/baseline_replay_v2.py \
  mlb_backtest/model_improvement/__init__.py \
  tests/test_model_improvement_baseline_replay.py
git commit -m "feat: seal baseline replay receipts"
```

### Task 5: Task8B — Admit nonbaseline seals through same-fold replay and card authority

**Blocking precondition:** Do not implement historical A/A+B/A+B+C execution
until the selected Task7 scoring card has its own pre-target
`WeightSelectionReceipt` (or equivalent closed selection authority). The card
digest authenticates card bytes but does not prove selection without target
outcomes; a V2 calibration receipt/card cannot supply that missing authority.
Until then, Task8B returns `CalibrationAuthorityBlockedError` before output.

**Files:**
- Modify: `schemas/model_improvement/probability_calibration_card_v2.schema.json`
- Modify: `schemas/model_improvement/prediction_seal_v2.schema.json`
- Modify: `mlb_backtest/model_improvement/probability_calibration.py`
- Modify: `mlb_backtest/model_improvement/oof_ledger_v2.py`
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Modify: `tests/test_model_improvement_oof_ledger.py`

- [ ] **Step 1: Write RED tests for A/A+B direct Common binding and A+B+C parent binding.**

```python
def test_lane_a_requires_same_fold_baseline_replay_and_derived_card(self):
    seal = seal_oof_predictions_v2(self.spec, self.gate, self.common_h22_seal,
                                self.h22_replay, "lane_a/A/H22", self.a_rows)
    self.assertEqual(seal.feature_seal_sha256, self.common_h22_seal.artifact_sha256)
    for bad in (None, self.h23_replay):
        with self.assertRaises(ValueError):
            seal_oof_predictions_v2(self.spec, self.gate, self.common_h22_seal,
                                 bad, "lane_a/A/H22", self.a_rows)

def test_lane_c_uses_its_own_feature_digest_and_verified_common_parent(self):
    seal = seal_oof_predictions_v2(self.spec, self.gate, self.lane_c_h22_seal,
                                self.h22_replay, "lane_c/A+B+C/H22", self.c_rows)
    self.assertEqual(seal.feature_seal_sha256, self.lane_c_h22_seal.artifact_sha256)
    self.assertNotEqual(seal.feature_seal_sha256, self.h22_replay.feature_seal_sha256)
```

Cover missing candidate calibration, wrong Task7 path/digest/model/lane/fold/candidate/card contract,
baseline-replay descriptor forgery, direct Lane-C-to-baseline digest equality,
wrong parent Common seal, reordered or different-year Lane-C rows, all H24
identities, and reload-time failure after any prerequisite mutation.

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger -v`

Expected: FAIL because Task8A rejects every nonbaseline ledger.

- [ ] **Step 3: Extend only the Task8 identity/card/replay admission branch.**

Extend `prediction_paths_v2` to admit precisely:

```text
lane_a/A/{O19..O23,H22,H23}
lane_b/{candidate_id}/{O19..O23,H22,H23}
lane_c/A+B+C/{H22,H23}
```

For a nonbaseline call, require a concrete `BaselineReplayReceiptV2`, then
late-import `load_baseline_replay_v2` inside the verifier to avoid the
Task6A↔Task8 import cycle. Reload that receipt from `baseline_replay.path` and
digest and compare full descriptor values. Derive the Task7 scoring-card path
from identity and call `load_scoring_model_card(spec, derived_path, digest)`;
never accept a card path. Require the loaded card's model/lane/fold/candidate,
RunSpec, source gate, contract, feature seal, and
`baseline_replay_sha256 == replay.artifact_sha256` to match. Require the
Task7 scoring-card schema and runtime contract remain independent of
calibration. Derive, not accept, the matching candidate-specific
`ProbabilityCalibrationCardV2` path; revalidate its
candidate/model/lane/fold, source gate, contract, weight-authority, strict
pre-target pool, GameEvidence/feature provenance, and fit before calculating
any candidate probability. Baseline calibration reuse after a weight change is
an error.

Use this exact compatibility branch:

```python
if model_id in {"A", "A+B"}:
    require(feature_seal.feature_set_id == "common")
    require(feature_seal.artifact_sha256 == replay.feature_seal_sha256)
else:  # A+B+C only
    require(feature_seal.feature_set_id == "lane_c")
    parent = load_feature_seal(common_feature_seal_path(spec, fold_id),
                               feature_seal.parent_feature_seal_sha256)
    require(feature_seal.parent_feature_seal_sha256 == replay.feature_seal_sha256)
    require_matching_fold_source_spec_population(feature_seal, parent, replay)
```

For the Lane-C branch securely reload, in sealed order, parent Common feature
rows, Lane-C feature rows, and baseline prediction rows. Require every three
populations to share one-for-one game PK/date/UTC/target-year and require
`task8_target_year(fold) == row.target_year == date.fromisoformat(row.official_date).year`.
Do not compare the Lane-C artifact digest directly to the baseline feature
digest. Retain Task8A's four-file publication and rerun this entire branch on
both public loaders.

- [ ] **Step 4: Run GREEN and focused regressions.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger tests.test_model_improvement_baseline_replay tests.test_model_improvement_scoring_cards tests.test_model_improvement_sealed_features tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_source_gate tests.test_model_improvement_canonical -v && python3 -m ruff check mlb_backtest/model_improvement/oof_ledger_v2.py tests/test_model_improvement_oof_ledger.py && git diff --check`

Expected: PASS; nonbaseline output requires a same-fold replay and a derived scoring card, while Task8 stays label-free.

- [ ] **Step 5: Commit Task8B.**

```bash
git add schemas/model_improvement/probability_calibration_card.schema.json \
  schemas/model_improvement/prediction_seal_v2.schema.json \
  mlb_backtest/model_improvement/probability_calibration.py \
  mlb_backtest/model_improvement/oof_ledger_v2.py \
  mlb_backtest/model_improvement/__init__.py \
  tests/test_model_improvement_oof_ledger.py
git commit -m "feat: bind nonbaseline OOF seals to replay evidence"
```

### Task 6: Task6B — Derive and seal the one baseline outcome receipt per fold

**Files:**
- Create: `schemas/model_improvement/outcome_receipt.schema.json`
- Create: `mlb_backtest/model_improvement/outcomes.py`
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Create: `tests/test_model_improvement_outcomes.py`

- [ ] **Step 1: Write RED outcome-authority tests with no mapping input.**

```python
def test_only_verified_game_evidence_can_create_baseline_outcomes(self):
    receipt = seal_outcome_receipt(self.spec, self.gate, self.common_h22_seal,
                                   self.h22_replay, self.h22_baseline_prediction)
    rows = load_outcome_rows(self.spec, self.gate, self.common_h22_seal,
                             self.h22_replay, receipt)
    self.assertEqual([row.game_pk for row in rows], [row.game_pk for row in self.baseline_rows])
    self.assertTrue(all(type(row.home_win) is bool for row in rows))

def test_nonbaseline_or_unverified_or_mismatched_evidence_is_rejected(self):
    with self.assertRaises(ValueError):
        seal_outcome_receipt(self.spec, self.gate, self.common_h22_seal,
                             self.h22_replay, self.lane_a_prediction)
    for state in ("Scheduled", "Postponed", "Cancelled", "In Progress"):
        with self.subTest(state=state), self.assertRaises(ValueError):
            seal_from_game_evidence(self.fixture_with(terminal_status=state))
```

Add cases for SourceInventory and GameEvidenceInventory path/digest
substitution, rebind mutation between reads, `played=False`, unverified,
non-bool/non-int/negative/tied scores, winner mismatch, missing/extra/duplicate
game, date/UTC/year mismatch, resumed game retaining original date/UTC,
all four publication crash points, and receipt sidecar/primary conflicts.

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_outcomes -v`

Expected: FAIL with `ModuleNotFoundError: mlb_backtest.model_improvement.outcomes`.

- [ ] **Step 3: Implement the source authority chain, closed types, and four-file receipt.**

Define exactly:

```python
@dataclass(frozen=True, slots=True)
class OutcomeRow:
    game_pk: int
    official_date: str
    target_year: int
    scheduled_start_utc: str
    home_win: bool
    source_game_sha256: str

@dataclass(frozen=True, slots=True)
class OutcomeReceipt:
    schema: str
    ledger_id: str
    fold_id: str
    model_id: str
    candidate_id: None
    run_spec_sha256: str
    source_gate_sha256: str
    source_inventory_sha256: str
    game_evidence_inventory_relative_path: str
    game_evidence_inventory_sha256: str
    feature_seal_sha256: str
    baseline_replay_sha256: str
    prediction_seal_sha256: str
    prediction_rows_sha256: str
    prediction_contract_sha256: str
    scoring_model_card_sha256: str
    population_sha256: str
    outcomes_sha256: str
    artifact_sha256: str
    path: str
```

Implement `_load_verified_outcome_authority(spec, source_gate)` as follows:

```python
gate = load_source_gate_receipt(spec.output_path("source_gate", "source_gate_receipt.json"),
                                source_gate.artifact_sha256)
source_bytes = read_regular_bytes(authority_root / spec.source_authority.inventory.path,
                                  "Task6 source inventory")
require(sha256(source_bytes).hexdigest() == spec.source_authority.inventory.sha256)
source_inventory = v52_snapshot_store.load_source_inventory_bytes(source_bytes)
binding = source_inventory.game_evidence_inventory
verified = load_verified_game_evidence_inventory(
    authority_root, read_only_progress=True,
    run_spec_path=authority_root / spec.source_authority.source_run_spec.path,
    acquisition_contract_path=authority_root / spec.source_authority.acquisition_contract.path,
)
require(verified.path.relative_to(authority_root) == Path(binding.path))
require(verified.sha256 == binding.sha256)
```

Use the project parser that corresponds to the current `SourceInventory`
contract; it must construct the `SourceInventory` typed value from the exact
already-hashed bytes, not accept a mapping. Verify the authority chain twice:
once before outcome-row derivation and once after collecting evidence, before
any output write.

Accept only the securely reloaded baseline V2 seal under
`oof/baseline/v5.1/{fold}/v2` and Common feature seal matching the replay. Derive
rows from verified evidence in prediction order. Require exactly `Final` or
`Completed Early`, `played is True`, `verification_status == "VERIFIED"`,
non-bool nonnegative unequal scores, and a `winner_team_id` consistent with
the home/away score. Write sidecars then row primary then receipt primary in
the exact baseline V2 prediction directory. The public APIs have these exact
signatures and never accept a mapping or raw outcome input:

```text
seal_outcome_receipt(spec, source_gate, feature_seal, baseline_replay,
                     baseline_prediction_seal)
load_outcome_receipt(spec, source_gate, feature_seal, baseline_replay, path,
                     expected_sha256)
load_outcome_rows(spec, source_gate, feature_seal, baseline_replay, receipt)
```

- [ ] **Step 4: Run GREEN, schema validation, and boundary check.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_outcomes tests.test_model_improvement_oof_ledger tests.test_model_improvement_baseline_replay tests.test_v52_game_evidence_inventory tests.test_model_improvement_source_gate tests.test_model_improvement_canonical -v && python3 -c 'import json; from jsonschema import Draft202012Validator; Draft202012Validator.check_schema(json.load(open("schemas/model_improvement/outcome_receipt.schema.json")))' && python3 -m ruff check mlb_backtest/model_improvement/outcomes.py tests/test_model_improvement_outcomes.py`

Expected: PASS; only a baseline/Common prediction can reveal results and Task8 imports no outcome module.

- [ ] **Step 5: Commit Task6B outcome authority.**

```bash
git add schemas/model_improvement/outcome_receipt.schema.json \
  mlb_backtest/model_improvement/outcomes.py \
  mlb_backtest/model_improvement/__init__.py \
  tests/test_model_improvement_outcomes.py
git commit -m "feat: seal authenticated model improvement outcomes"
```

### Task 7: Task6B — Persist receipt-bound reports and an evaluation pair

**Files:**
- Create: `schemas/model_improvement/evaluation_report.schema.json`
- Create: `schemas/model_improvement/evaluation_pair.schema.json`
- Create: `mlb_backtest/model_improvement/evaluation.py`
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Create: `tests/test_model_improvement_evaluation.py`

- [ ] **Step 1: Write RED tests for provenance, population, metrics, and final pair commit.**

```python
def test_pair_reloads_the_same_receipt_from_both_reports_before_metrics(self):
    pair = evaluate_predictions(self.spec, self.lane_a_h22, self.baseline_h22,
                                self.h22_outcome_receipt)
    loaded = load_evaluation_pair(self.spec, pair.path, pair.artifact_sha256)
    self.assertEqual(loaded.outcome_receipt_path, self.h22_outcome_receipt.path)
    self.assertEqual(loaded.outcome_receipt_sha256, self.h22_outcome_receipt.artifact_sha256)
    self.assertEqual(loaded.candidate_report.outcome_receipt_sha256,
                     loaded.baseline_report.outcome_receipt_sha256)

def test_lane_c_uses_parent_common_relation_not_direct_lane_c_digest_equality(self):
    pair = evaluate_predictions(self.spec, self.lane_c_h22, self.baseline_h22,
                                self.h22_outcome_receipt)
    self.assertEqual(pair.fold_id, "H22")
```

Add RED cases for receipt path/digest substitution, report receipt disagreement,
pair/report disagreement, candidate/baseline key/order/population mismatch,
wrong fold/source/contract, Common direct relation failure, Lane-C parent
relation failure, missing rank one, invalid rank, neutral dates, pair crash
points, missing sidecars, and all immutable retries.

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_evaluation -v`

Expected: FAIL with `ModuleNotFoundError: mlb_backtest.model_improvement.evaluation`.

- [ ] **Step 3: Implement outcome-receipt admission before every metric.**

Implement exactly:

```text
evaluate_predictions(spec, candidate, baseline, outcome_receipt) -> EvaluationPair
load_evaluation_report(spec, path, expected_sha256) -> EvaluationReport
load_evaluation_pair(spec, path, expected_sha256) -> EvaluationPair
```

At the first line of evaluation, secure-reload the baseline and candidate seal
through Task8B and the explicitly named receipt through `load_outcome_receipt`
at the exact derived `oof/baseline/v5.1/{fold}/v2/outcome_receipt.json` path.
Compare the entire descriptor, not a digest string. Require same fold, target
year, source gate, contract, ordered population digest, and one-for-one row
keys. Then apply the Task8 branch compatibility exactly: A/A+B Common digest
equality; A+B+C Lane-C own digest plus secure parent Common relation to the
replay receipt.

Build all-game Brier/log loss from verified `OutcomeRow.home_win` only after
these checks. Build daily pairs sorted by official date and neutral dates as
the exact preimages in the approved Task8 design. Write candidate/baseline
reports with receipt path/digest and identical `outcomes_sha256`, then publish
the final pair only after both report primaries are reloaded successfully.
All V2 reports/pairs are derived beneath the same candidate `/v2` namespace.
The pair path is derived from the candidate ledger identity; no report, pair,
or receipt loader accepts an arbitrary namespace or alias.

- [ ] **Step 4: Run GREEN, schemas, lint, and complete focused regression.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_evaluation tests.test_model_improvement_outcomes tests.test_model_improvement_oof_ledger tests.test_model_improvement_baseline_replay tests.test_model_improvement_scoring_cards tests.test_model_improvement_sealed_features tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_source_gate tests.test_model_improvement_canonical -v && python3 -c 'import json; from jsonschema import Draft202012Validator; [Draft202012Validator.check_schema(json.load(open(path))) for path in ("schemas/model_improvement/evaluation_report.schema.json", "schemas/model_improvement/evaluation_pair.schema.json")]' && python3 -m ruff check mlb_backtest/model_improvement/evaluation.py tests/test_model_improvement_evaluation.py && git diff --check`

Expected: PASS; no metric or frozen-card consumer can establish outcome provenance from `outcomes_sha256` alone.

- [ ] **Step 5: Commit Task6B evaluation.**

```bash
git add schemas/model_improvement/evaluation_report.schema.json \
  schemas/model_improvement/evaluation_pair.schema.json \
  mlb_backtest/model_improvement/evaluation.py \
  mlb_backtest/model_improvement/__init__.py \
  tests/test_model_improvement_evaluation.py
git commit -m "feat: persist receipt-bound model evaluations"
```

### Task 8: Final focused package/export and downstream contract verification

**Files:**
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Modify: `tests/test_model_improvement_oof_ledger.py`
- Modify: `tests/test_model_improvement_evaluation.py`

- [ ] **Step 1: Write RED export and import-boundary assertions.**

```python
def test_task8_exports_only_v2_label_free_prediction_surface(self):
    exported = set(model_improvement.__all__)
    self.assertTrue({"PredictionSealV2", "seal_oof_predictions_v2", "load_prediction_seal_v2",
                     "load_prediction_rows_v2"} <= exported)
    self.assertFalse({"JoinedLedger", "join_oof_outcomes", "load_joined_ledger",
                      "load_joined_prediction_rows"} & exported)

def test_task8_v2_module_has_no_outcome_or_metric_import(self):
    tree = ast.parse(Path(oof_ledger_v2.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    self.assertFalse({".outcomes", ".evaluation"} & imported)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger.OofLedgerImportBoundaryTest -v`

Expected: FAIL until package exports omit superseded labelled interfaces and the final module boundary is explicit.

- [ ] **Step 3: Export the finalized public surface once.**

Export `CalibrationFeatureReceiptV1`, `load_calibration_feature_receipt`,
`ProbabilityCalibrationCardV2`, `load_probability_calibration_card_v2`, and
`require_probability_calibration_card_v2`; `PredictionSealV2`, Task8's three V2
functions; `BaselineReplayReceiptV2` and its two V2 functions; `OutcomeRow`,
`OutcomeReceipt` and three outcome functions; and `EvaluationReport`,
`EvaluationPair`, `evaluate_predictions`, `load_evaluation_report`,
`load_evaluation_pair`. Do not export internal path parsers, source inventory
parsers, raw JSONL readers, calibration-row writers, or any labelled ledger
API. Keep the `TYPE_CHECKING` import from `baseline_replay_v2` out of Task8
runtime imports.

- [ ] **Step 4: Run the final focused validation.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_probability_calibration tests.test_model_improvement_oof_ledger tests.test_model_improvement_baseline_replay tests.test_model_improvement_outcomes tests.test_model_improvement_evaluation tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_sealed_features tests.test_model_improvement_source_gate tests.test_model_improvement_scoring_cards tests.test_model_improvement_canonical -v && python3 -m ruff check mlb_backtest/model_improvement tests/test_model_improvement_probability_calibration.py tests/test_model_improvement_oof_ledger.py tests/test_model_improvement_baseline_replay.py tests/test_model_improvement_outcomes.py tests/test_model_improvement_evaluation.py && git diff --check`

Expected: PASS; Task6P plus all four Task8A/Task6A/Task8B/Task6B stages
remain independently covered and no full suite is started.

- [ ] **Step 5: Commit the final public API check.**

```bash
git add mlb_backtest/model_improvement/__init__.py \
  tests/test_model_improvement_oof_ledger.py \
  tests/test_model_improvement_evaluation.py
git commit -m "test: lock staged model improvement public boundaries"
```

## Self-review

**Spec coverage:** Task 0 repairs every named stale plan. Task 1 establishes
the only pre-target probability-calibration authority, including the seven
baseline pools and its missing-authority block. Tasks 2–3 cover Common OOF
feature admission, all seven Task8 fold/year pairs, V2
`PredictionSealV2.feature_seal_sha256`/`calibration_card_sha256`, calibrated
baseline four-file seals, and the sole row loader. Task 4 binds replay only
after the matching V2 baseline seal. Task 5 covers Task7-card-gated,
candidate-specific pre-target calibration and both Common/Lane-C compatibility
relations. Tasks 6–7 cover SourceInventory-to-GameEvidence authentication,
all forbidden evidence states, one baseline receipt per fold, receipt-bound
reports/pairs, and final receipt provenance. Task 8 locks package exposure and
the no-outcomes-in-Task8 boundary.

**Placeholder scan:** This plan intentionally contains no `TODO`, `TBD`,
"implement later", raw outcome mapping, unnamed output path, or omitted
function body disguised as an implementation sketch. Each implementation
step states the exact validation, input authority, byte preimage, and
persistence order required of the named function.

**Type consistency:** `CalibrationFeatureReceiptV1` and
`ProbabilityCalibrationCardV2` are introduced in Task 1; V1 remains
inspection-only;
V2 `PredictionSealV2.feature_seal_sha256` and `calibration_card_sha256` are
introduced in Tasks 2–3 and used in all later stages. Every Task8 caller uses
`(spec, source_gate, feature_seal, baseline_replay, path_or_identity)`; the
calibration loader derives its authority from that identity and accepts no
caller path or outcomes. Task6B outcome loaders use the same authenticated
prerequisite quartet; evaluation consumes a typed `OutcomeReceipt`, never
labels. The Task8 target-year mapping is the only OOF-year authority, and
`ModelImprovementRunSpec.fold()` remains H22/H23/H24-only.

**Known implementation dependency:** Task6B must call the typed
`SourceInventory` parser corresponding to the current
`mlb_backtest.v52_snapshot_store.SourceInventory` wire contract before reading
the `game_evidence_inventory` binding. The existing source inventory already
contains this binding; implementation must expose/reuse a bytes-to-typed-value
parser without adding a mapping bypass. If that parser is not public, add the
smallest public `parse_source_inventory_bytes(raw: bytes) -> SourceInventory`
helper in `v52_snapshot_store.py` with its own closed-schema/round-trip test
before Task6B writes any outcome artefact.
