# Task6P/Task8 OOF Calibration and Label-Free Prediction-Seal Design

## Status, ownership, and corrected order

This document is the normative Common Task8 design. It supersedes the Task8
sections and all old Task8 function signatures in
`docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md` and in
the Lane A/B/C/H24 plans. The implementation plan that follows this design
must patch those documents before implementation begins; no consumer may keep
an older `seal_oof_predictions(spec, ledger_id, rows)` or spec-only row-loader
call. New executable consumers must use the explicitly versioned V2 surface;
the published V1 surface remains inspection-only.

Task8 owns immutable **label-free** OOF prediction seals, prediction JSONL,
their closed schema, and the one authoritative prediction-row loader. It owns
no outcome mapping input, labelled JSONL, outcome reader, or metric. Those
belong to the Common Task6 authenticated outcome/evaluation boundary. Task6
must first design a
source-gate-bound deterministic outcome authority and receipt, and must not
accept arbitrary caller mappings as a proxy for outcomes. Task8 implements
none of that producer or persistence.

The previous baseline branch was incomplete: a RunSpec containing only v5.1
component weights cannot reproduce a v5.1 probability.  The audited v5.1
card's alpha/beta were fitted over 2022--2024, so using those values for O19,
O20, or O21 is future leakage.  Using an invented identity calibration
(`alpha=0`, `beta=1`) is not a replay.  This is a proven missing authority,
not a permitted default.

The approved implementation and runtime activation sequence is:

```text
Task6P  source-derived, pre-target ProbabilityCalibrationCard authority
Task8A  Common OOF FeatureSeal admission + V2 baseline v5.1 label-free seals/loaders
Task6A  baseline replay receipt, sealed only after the matching baseline seal
Task8B  per-candidate pre-target calibration plus nonbaseline label-free seals/loaders
Task6B  authenticated outcome authority, outcome/evaluation join, and metrics
```

This replaces the shorthand `Task7 -> Task8 -> Task6`. Task7's scoring-card
code can be implemented and synthetically tested before this sequence, but a
real nonbaseline scoring card or Task8B prediction cannot be admitted until
the same-fold Task6A `BaselineReplayReceipt` and its own pre-target
calibration authority exist. This removes the old artifact cycle: Task6P
prepares only prior-season calibration evidence, baseline prediction seal
precedes replay receipt, replay receipt and a candidate card precede that
candidate's calibration card and prediction seal, and target-fold outcomes
open only in Task6B after all required prediction seals are immutable.

Task8 does not download data, modify the audited v5.1 card, use a
live/experimental output, start an H24 run, or add a compatibility execution
path. Task6P may add new derived calibration authority artifacts; it never
rewrites the audited v5.1 card.

## Task6P probability-calibration authority

`ProbabilityCalibrationCard` is the only authority allowed to turn a sealed
edge into `p_home`. It is not a Task8 artifact and it is the only permitted
label-bearing artifact before Task6B: its labels are strictly *prior-target*
outcomes used for fitting, never target-fold outcomes or evaluation labels.

There are five immutable baseline calibration keys, keyed by target year,
not by a caller-selected fold. H22 shares `C22` with O22 and H23 shares C23
with O23.

| calibration key | consumers | permitted official-date years |
| --- | --- | --- |
| C19 | O19 | 2018 |
| C20 | O20 | 2018--2019 |
| C21 | O21 | 2018--2020 |
| C22 | O22, H22 | 2018--2021 |
| C23 | O23, H23 | 2018--2022 |

For key `CYY`, every calibration row must have an official date no later
than `YYYY-1-12-31`; rows must also prove their own D-1 feature cutoff. A
target-year row, a future source revision substituted for the bound source
evidence, an arbitrary outcome mapping, or a caller-selected calibration path
is a hard failure. There is no baseline C24 in this work: H24 remains
prediction-only and must consume a separately frozen qualified card later.

The closed primary schema is
`MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V1` with exactly:

```text
schema, model_id, lane_id, fold_id, candidate_id, calibration_key,
target_year, training_start, training_end,
run_spec_sha256, source_gate_sha256, prediction_contract_sha256,
weight_authority_path, weight_authority_sha256,
calibration_rows_path, calibration_rows_sha256,
calibration_population_sha256,
game_evidence_inventory_path, game_evidence_inventory_sha256,
fit_spec, alpha, beta
```

`fit_spec` is closed and fixes the existing logistic calculation rather than
allowing caller tuning: `algorithm="fit_pre_target_calibration"`,
`edge_scale=10`, `l2=0.000001`, `iterations=100`,
`tolerance=0.0000000001`, and clip `[0.10, 0.90]`. `alpha` and `beta` are
finite; baseline and candidate cards require `beta > 0`. The primary's
runtime descriptor fields `artifact_sha256` and `path` are excluded from the
payload as for every other artifact.

The RunSpec contains policy, not an emitted-card digest, at the exact closed
key `probability_calibration_policy`:

```json
{
  "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_POLICY_V1",
  "baseline_key_by_fold": {"O19": "C19", "O20": "C20", "O21": "C21", "O22": "C22", "H22": "C22", "O23": "C23", "H23": "C23"},
  "pools": {
    "C19": {"target_year": 2019, "training_start": "2018-01-01", "training_end": "2018-12-31"},
    "C20": {"target_year": 2020, "training_start": "2018-01-01", "training_end": "2019-12-31"},
    "C21": {"target_year": 2021, "training_start": "2018-01-01", "training_end": "2020-12-31"},
    "C22": {"target_year": 2022, "training_start": "2018-01-01", "training_end": "2021-12-31"},
    "C23": {"target_year": 2023, "training_start": "2018-01-01", "training_end": "2022-12-31"}
  },
  "fit_spec": {"algorithm": "fit_pre_target_calibration", "edge_scale": 10, "l2": 0.000001, "iterations": 100, "tolerance": 0.0000000001, "clip_low": 0.10, "clip_high": 0.90},
  "baseline_rows_relative_template": "baseline/v5.1/calibration/{target_year}/calibration_rows.jsonl",
  "baseline_card_relative_template": "baseline/v5.1/calibration/{target_year}/probability_calibration_card.json",
  "candidate_rows_relative_template": "candidates/{lane_id}/{candidate_id}/calibration/{target_year}/calibration_rows.jsonl",
  "candidate_card_relative_template": "candidates/{lane_id}/{candidate_id}/calibration/{target_year}/probability_calibration_card.json"
}
```

This object is part of the canonical RunSpec payload and is validated by the
RunSpec schema/dataclass with exact keys, fixed ordering, and no caller
overrides. It intentionally contains neither card/rows digests nor source
outcomes: those appear only in a subsequently emitted card and would otherwise
make the source-gate authority circular.

The sibling rows primary is canonical JSONL and has exactly
`game_pk, official_date, target_year, edge, home_win,
feature_provenance_sha256, game_evidence_sha256`. It is built only by
securely deriving eligible completed prior-game outcomes through the
RunSpec-bound SourceInventory and GameEvidenceInventory, then pairing them
with the exact D-1 feature provenance and the named weight authority. The
card loader rereads rows, verifies every source/provenance binding, canonical
order and population, recomputes every edge, reruns the fixed fit, and
requires byte-identical alpha/beta results. It must reject missing classes,
too few rows, dates beyond the cutoff, duplicate game PKs, aliases, sidecar
or digest mismatches, and any nonpositive beta.

Paths are derived, not provided:

```text
baseline/v5.1/calibration/{target_year}/calibration_rows.jsonl
baseline/v5.1/calibration/{target_year}/probability_calibration_card.json
```

Each has its canonical SHA-256 sidecar. Publication is rows sidecar, card
sidecar, rows primary, card primary; only the card primary commits the
authority. A matching orphan may only be completed with identical bytes.
`load_probability_calibration_card(spec, model identity)` derives these paths
and revalidates the entire chain; it takes no rows, outcomes, or path
argument. If the required source-derived prior-outcome evidence is absent,
the loader and every V2 sealer/replay caller raise the dedicated
`CalibrationAuthorityBlockedError` (`RuntimeError`) whose immutable
`code` and complete string are exactly `BLOCKED_CALIBRATION_AUTHORITY_MISSING`.
It is a pre-publication disposition, not a JSON artifact state: it writes no
rows, card, V2 prediction seal, or replay receipt and must be propagated
unchanged. A present-but-malformed or digest-mismatched authority instead
raises `ValueError`. No value may be invented or inherited from the global
2022--2024 v5.1 card.

The existing `mlb_backtest.normalize.fit_frozen_calibration` is not an
admitted implementation primitive for these pools because its fixed
2022--2024 bounds reject C19--C22. Task6P must add the narrowly scoped
`fit_pre_target_calibration(rows, *, training_start, training_end)` in
`mlb_backtest/normalize.py`, plus `tests/test_normalize.py` coverage. It must
share the same finite-row, unique-game-PK, edge-scale-10, `fit_logistic`
(`l2=1e-6`, 100 iterations, tolerance `1e-10`) and metadata mechanics, while
requiring caller bounds exactly equal to a policy pool and all row dates within
those inclusive bounds. It must not weaken or change
`fit_frozen_calibration`; `probability_calibration.py` calls only this new
primitive with the card's fixed policy values.

## V2 baseline replay receipt

`BaselineReplayReceipt` is V2-only even though no V1 receipt need be
generated by this work. Its primary schema literal is
`MODEL_IMPROVEMENT_BASELINE_REPLAY_RECEIPT_V2`, with exactly this payload:

```text
schema, ledger_id, fold_id, model_id, candidate_id,
run_spec_sha256, source_gate_sha256, feature_seal_sha256,
prediction_contract_sha256, scoring_model_card_sha256,
calibration_card_path, calibration_card_sha256, population_sha256,
replay_one_rows_sha256, replay_two_rows_sha256,
replay_one_bytes_sha256, replay_two_bytes_sha256,
prediction_rows_sha256, prediction_seal_path, prediction_seal_sha256
```

`artifact_sha256` and `path` are runtime-only. It is valid only when the
referenced seal has schema `MODEL_IMPROVEMENT_PREDICTION_SEAL_V2`, resides in
the derived V2 directory, and its row and calibration-card digests equal the
receipt. Any V1 seal or any receipt missing these V2 bindings is rejected
before replay or publication.

## OOF fold correction and exact identities

Task7 admits O19--O23 scoring cards while the current FeatureSeal type admits
only H22/H23/H24. Task8A necessarily extends **Common** FeatureSeal admission
so an OOF prediction can prove the exact feature population that produced it:

```python
TASK8_FOLD_TARGET_YEAR: Mapping[str, int] = {
    "O19": 2019,
    "O20": 2020,
    "O21": 2021,
    "O22": 2022,
    "O23": 2023,
    "H22": 2022,
    "H23": 2023,
}
TASK8_FOLD_IDS = tuple(TASK8_FOLD_TARGET_YEAR)
```

O19--O23 are historical OOF identifiers, not inputs to
`ModelImprovementRunSpec.fold()`, which intentionally remains H22/H23/H24
only. Task8 uses the mapping above at every target-year check; it never parses
a year from a fold suffix or obtains it from a clock or outcome.

Task8A extends `feature_seal.schema.json`, `FeatureSeal`, feature-row
validation, and `spec.output_path("features", relative)` for exactly these
additional Common paths:

```text
features/common/O19/{feature_seal.json,rows.jsonl}
features/common/O20/{feature_seal.json,rows.jsonl}
features/common/O21/{feature_seal.json,rows.jsonl}
features/common/O22/{feature_seal.json,rows.jsonl}
features/common/O23/{feature_seal.json,rows.jsonl}
```

No Lane-C OOF feature namespace is added. Existing Common H22/H23/H24 and
Lane-C H22/H23/H24 feature admissions remain unchanged. Feature-row validation
uses the Task8 map for Common OOF folds and the existing RunSpec fold mapping
for H22/H23/H24. Every row still proves D-1 history, its source-gate digest,
`source_manifest_sha256 == spec.source_authority.inventory.sha256`, and its
existing feature-row hash. H24 can remain a feature seal for the future
registry, but all Task8 identity parsers, sealers, and loaders reject H24.

The only Task8 prediction identities and primary directories are:

| model | ledger ID | permitted folds | exact directory |
| --- | --- | --- | --- |
| v5.1 | `baseline/v5.1/{fold}` | O19--O23, H22, H23 | `oof/baseline/v5.1/{fold}/v2` |
| A | `lane_a/A/{fold}` | O19--O23, H22, H23 | `oof/lane_a/A/{fold}/v2` |
| A+B | `lane_b/{candidate_id}/{fold}` | O19--O23, H22, H23 | `oof/lane_b/{candidate_id}/{fold}/v2` |
| A+B+C | `lane_c/A+B+C/{fold}` | H22, H23 | `oof/lane_c/A+B+C/{fold}/v2` |

`candidate_id` is required only for A+B and must remain the Task7 registered
safe segment `[A-Za-z0-9][A-Za-z0-9._-]*`, excluding `.` and `..`; it is null
for every other model. H24, a bare fold ID, another model/scope pairing, and a
2025 target all fail before a path is calculated.

For any accepted identity, `prediction_paths_v2(spec, ledger_id)` is the sole
V2 identity-to-path function. It calls only `spec.output_path("oof",
relative_name)` and returns these exact V2 siblings:

```text
prediction_rows.jsonl
prediction_rows.jsonl.sha256
prediction_seal.json
prediction_seal.json.sha256
```

No Task8 API accepts an output directory, relative file name, glob, listing,
or caller-selected scoring-card path.

## Closed PredictionSeal schema and descriptor

`schemas/model_improvement/prediction_seal.schema.json` remains the published
V1 schema and is never rewritten. V2 uses the new closed Draft 2020-12
document `schemas/model_improvement/prediction_seal_v2.schema.json`; its
closed definitions are `prediction_row_v2` and `prediction_seal_v2`; each
object has `additionalProperties: false` and every digest is lowercase 64-hex.
Persisted JSON always uses
`canonical_json_bytes` (sorted compact UTF-8 plus one newline).

The `prediction_row_v2` definition and `PredictionRowV2` runtime type require
exactly these payload fields:

```text
model_id, game_pk, official_date, target_year, p_home, picked_side,
pick_probability, confidence, starter_edge_abs, scheduled_start_utc, rank,
feature_row_sha256, prediction_contract_sha256, scoring_model_card_sha256,
calibration_card_sha256
```

It does not include `label`, `outcome`, `result`, `won`, a score, or any other
result field. The runtime dataclass remains the final validator for finite
ranges, pick/rank consistency, and digest syntax.

Task8A V2 introduces the separate `PredictionSealV2` dataclass with the
required fields `feature_seal_sha256` and `calibration_card_sha256`. Its exact
persisted payload is:

```text
schema = "MODEL_IMPROVEMENT_PREDICTION_SEAL_V2"
ledger_id, fold_id, model_id, candidate_id,
run_spec_sha256, source_gate_sha256, feature_seal_sha256,
prediction_contract_sha256, scoring_model_card_sha256,
calibration_card_sha256, population_sha256, frozen_model_card_sha256,
rows_sha256
```

`artifact_sha256` and `path` remain runtime descriptor fields. They are
computed only from the primary payload bytes after publication and are absent
from the payload, schema preimage, and payload hash. `as_mapping()` excludes
them. `PredictionSealV2.from_payload_mapping` and every V2 execution reader
require the feature-seal and calibration-card fields. V1 seal/row payloads,
schemas, and public code remain immutable inspection material; they are
inadmissible for Task6A, Task8B, Task6B, or any executable model result.

## Canonical row bytes and four-file commit

Rows are supplied in and must retain this strict order:

```python
row_key = (row.official_date, row.scheduled_start_utc, row.game_pk)
prediction_rows_bytes = b"".join(
    canonical_json_bytes(row.as_mapping()) for row in ordered_rows
)
population_preimage = [
    {"game_pk": row.game_pk, "official_date": row.official_date,
     "target_year": row.target_year}
    for row in ordered_rows
]
population_sha256 = sha256(canonical_json_bytes(population_preimage)).hexdigest()
rows_sha256 = sha256(prediction_rows_bytes).hexdigest()
```

There must be at least one row. `game_pk` and the complete population tuple
`(game_pk, official_date, target_year)` are unique. A sorted copy must equal
the supplied tuple. Every row's target year and official-date year equal the
fixed year for its ledger fold. The population digest must equal the bound
FeatureSeal's population digest exactly: Task8 publishes every sealed feature
row, never a selected subset or a later outcome intersection.

Each sidecar is exactly canonical
`{"artifact_sha256": digest}\n`, where `digest` is SHA-256 of the raw primary
bytes it accompanies. `write_immutable_bytes` is the only writer. Publication
is always in this order:

```text
1. prediction_rows.jsonl.sha256
2. prediction_seal.json.sha256
3. prediction_rows.jsonl
4. prediction_seal.json  (the sole commit point)
```

A matching sidecar without its later primary is a reusable prerequisite for
the same byte-for-byte retry. A matching row primary and both sidecars with no
final descriptor primary is an uncommitted orphan and is never loadable; only
the exact descriptor bytes may complete it. A missing, malformed,
noncanonical, mismatched sidecar, symlink, nonregular file, or different
existing primary is a failure. A published descriptor without both verified
row artifacts is rejected and never repaired or treated as committed.

## Authenticated input and scoring authority

Task8A implements the baseline branch; Task8B implements the nonbaseline
branch. The exact canonical API surface is:

```python
def seal_oof_predictions_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    ledger_id: str,
    rows: Sequence[PredictionRowV2],
) -> PredictionSealV2

def load_prediction_seal_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    path: Path | str,
    expected_sha256: str,
) -> PredictionSealV2

def load_prediction_rows_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    seal: PredictionSealV2,
) -> tuple[PredictionRowV2, ...]
```

The implementation may use a type-only import and a late local
`load_baseline_replay` import to avoid a module cycle: Task6A imports the
Task8 baseline sealer, while Task8B needs Task6A's secure receipt loader. It
must not use a duck-typed mapping in place of `BaselineReplayReceipt`.

For every call, Task8 first parses the ledger ID, derives every exact output
path, and securely revalidates its inputs before generating or reading bytes:

* `source_gate` securely reloads from the sole RunSpec
  `source_gate/source_gate_receipt.json` path. The reloaded full descriptor,
  RunSpec digest, and inventory digest must match the supplied value and
  `spec.source_authority.inventory.sha256`.
* `feature_seal` securely reloads from its exact derived Common path for
  v5.1/A/A+B or its exact Lane-C path for A+B+C. The full descriptor, RunSpec,
  source gate, fold, feature set, and new `seal.feature_seal_sha256` binding
  must match; Task8B then applies the branch-specific replay compatibility
  rule below, including secure Lane-C-parent loading rather than a false
  direct Lane-C-to-baseline digest equality.
* Task8 calls the receipt-bound feature row loader and compares the complete
  row population one-for-one by game PK, official date, scheduled UTC, target
  year, and feature-row digest. Its source inventory digest must match the
  RunSpec. A supplied hash list or population digest alone is insufficient.
* Every row and descriptor binds the immutable prediction contract exactly;
  every row's scoring-card digest equals the seal's digest; the seal's
  RunSpec/source-gate/feature-seal/population digests equal the authenticated
  inputs.

The baseline and nonbaseline scoring authorities are intentionally different:

* **Baseline Task8A V2:** `ledger_id` must be `baseline/v5.1/{fold}` and
  `baseline_replay` must be `None`. Task8 securely derives and reloads the
  exact C19--C23 `ProbabilityCalibrationCard` before materializing a row. It
  validates the card's source gate, contract, direct v5.1 weight authority,
  pre-target pool, feature and GameEvidence provenance, then uses its alpha
  and beta in `home_probability`. Its digest is required in every row and
  seal; `scoring_model_card_sha256` continues to name the direct weight
  authority and `frozen_model_card_sha256` remains null. No identity
  calibration and no global 2022--2024 calibration is an alternative.
* **Nonbaseline Task8B:** A/A+B/A+B+C requires a non-null, securely reloaded
  same-fold `BaselineReplayReceipt`. Its RunSpec, source gate, fold, baseline
  card, prediction contract, baseline prediction seal, and population bindings
  must all agree with the current Task8 inputs, with FeatureSeal compatibility
  determined only by the branch-specific rule below. Task8
  derives the one Task7 scoring-card path from the ledger identity and calls
  `load_scoring_model_card`, not a caller-provided descriptor/path. The loaded
  card's model, lane, fold, candidate, RunSpec, source gate, FeatureSeal,
  prediction contract, and
  `baseline_replay_sha256 == baseline_replay.artifact_sha256` must match.
  Before any Task8B prediction, Task7's selected card must name a non-null
  candidate-specific `calibration_card_sha256`; Task8 derives and reloads the
  corresponding pre-target ProbabilityCalibrationCard and requires model,
  lane, candidate, fold, card, source-gate, contract, and calibration digest
  equality. Reusing the baseline calibrator after weights change is forbidden.
  Nonbaseline seals also set `frozen_model_card_sha256` to null.

### Feature compatibility with the baseline replay

`BaselineReplayReceipt.feature_seal_sha256` always names the **Common**
FeatureSeal used by the baseline prediction, never a Lane-C descendant. For a
baseline Task8A operation there is no replay receipt. For a Task8B A or A+B
operation, the supplied feature seal must be a Common seal and its artifact
digest must equal `baseline_replay.feature_seal_sha256` exactly; its fold,
target year, RunSpec, source-gate, and ordered population bindings must also
equal the replay receipt. This is the only branch that uses direct feature
seal digest equality.

For Task8B A+B+C, `PredictionSealV2.feature_seal_sha256` and the Task7 card
bind the supplied Lane-C FeatureSeal's own artifact digest. Task8 must
securely reload the Lane-C seal's declared parent Common seal from its exact
derived path (not a caller path), then require all of the following:

```text
lane_c.parent_feature_seal_sha256 == baseline_replay.feature_seal_sha256
lane_c.fold_id == parent_common.fold_id == baseline_replay.fold_id
lane_c.run_spec_sha256 == parent_common.run_spec_sha256 == baseline_replay.run_spec_sha256
lane_c.source_gate_sha256 == parent_common.source_gate_sha256 == baseline_replay.source_gate_sha256
lane_c.population_sha256 == parent_common.population_sha256 == baseline_replay.population_sha256
```

FeatureSeal has no `target_year` descriptor field and Task8 must not extend
its closed schema to add one. Instead, after the receipt-bound parent Common
rows, Lane-C rows, and baseline prediction rows have each been securely
reloaded in their sealed order, it requires every row in all three populations
to have `target_year == TASK8_FOLD_TARGET_YEAR[fold_id]` and
`official_date.year == TASK8_FOLD_TARGET_YEAR[fold_id]`, in addition to the
existing exact row population/identity checks. A missing, extra, reordered, or
different-year row is a compatibility failure.

The implementation must not require, infer, or accept
`lane_c.artifact_sha256 == baseline_replay.feature_seal_sha256`: those are
different layers. A direct Lane-C-to-baseline feature-digest comparison is a
pre-publication error. The sealer and both prediction loaders must make this
branch choice from the authenticated ledger/model identity and reapply it on
every reread; a digest-only comparison cannot substitute for the complete
parent descriptor checks above.

H24, an unexpected frozen-card digest, a baseline receipt on a baseline
operation, or a missing receipt on a nonbaseline operation fails before
publication. Task8 never computes a label or imports an outcome authority.

## Lexical path checks and authoritative loader behavior

Before any filesystem call, every caller-supplied descriptor path must be a
nonempty canonical absolute string, equal both to `str(Path(raw))` and
`os.path.normpath(raw)`, with no `.`/`..` segment or duplicate slash alias.
The path is decoded structurally under `outputs.root/oof`, converted to one
accepted ledger identity, and required to equal the derived path byte for
byte. Relative, `./`, `../`, `//`, wrong namespace, wrong candidate, and wrong
filename paths fail before `read_regular_bytes` observes them.

`load_prediction_seal_v2` securely reads the exact V2 primary and its mandatory
sidecar, recomputes the supplied digest, requires canonical JSON and the
closed schema, creates the runtime descriptor, and executes every input/card
binding above. A later consumer must compare a received descriptor in full,
not merely by digest.

`load_prediction_rows_v2` first securely reloads the input seal. It then derives
`prediction_rows.jsonl` from `prediction_paths_v2(spec, seal.ledger_id)` rather
than assuming a descriptor directory, secure-reads its sidecar and primary,
recomputes bytes/row digest/population, parses every canonical line through
`PredictionRowV2.from_payload_mapping`, and reruns the source gate, FeatureSeal,
feature-row, contract, and scoring-card checks. It is the sole public
prediction-row accessor. There is no labelled-row accessor in Task8.

## Failure rules, files, and tests

Any malformed, stale, incomplete, aliased, cross-fold, cross-model,
cross-candidate, cross-feature-set, cross-source-gate, cross-inventory,
cross-contract, cross-card, or cross-replay condition raises `ValueError`
before the affected descriptor is published or returned. Prerequisite-only
crash remnants follow the immutable orphan rules above. Evidence is never
deleted, rewritten, or converted into a label-bearing prediction artifact.

Task8 implementation is limited to:

```text
Create  schemas/model_improvement/probability_calibration_card.schema.json
Create  mlb_backtest/model_improvement/probability_calibration.py
Create  tests/test_model_improvement_probability_calibration.py
Modify  schemas/model_improvement/feature_seal.schema.json
Modify  mlb_backtest/model_improvement/run_spec.py
Modify  mlb_backtest/model_improvement/sealed_features.py
Modify  mlb_backtest/model_improvement/contracts.py       # add V2 row/seal calibration types
Modify  mlb_backtest/normalize.py                         # pre-target fit primitive; frozen fit unchanged
Create  schemas/model_improvement/prediction_seal_v2.schema.json
Create  mlb_backtest/model_improvement/oof_ledger_v2.py    # label-free only
Modify  mlb_backtest/model_improvement/__init__.py
Create  tests/test_model_improvement_oof_ledger.py
Modify  tests/test_model_improvement_sealed_features.py
Modify  tests/test_model_improvement_contracts.py
Modify  tests/test_model_improvement_run_spec.py
Modify  tests/test_normalize.py
Modify  docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-lane-a.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-h24-integration.md
```

Focused tests must prove:

1. all seven fixed fold/year pairs plus every H24, 2025, and unknown-fold
   rejection path;
2. all four exact namespaces/sibling names, Lane-B candidate uniqueness, and
   relative/alias/`..`/duplicate-slash rejection before reads;
3. Common OOF FeatureSeal admission for O19--O23 and Lane-C OOF rejection;
4. closed label-free row/seal schemas, strict order/uniqueness/population
   preimage, and feature-row hash/population substitution rejection;
5. secure source-gate/inventory/FeatureSeal/RunSpec/prediction-contract
   mismatches at both write and reload;
6. all five baseline pre-target calibration pools and shared O22/H22 and
   O23/H23 keys; source-derived outcome/feature provenance, cutoff, fit,
   card/sidecar/orphan, and missing-authority rejection; baseline direct-card
   success plus wrong path/digest/weights/calibration failure; Task8B
   candidate-specific derived-card/calibration and same-fold
   BaselineReplayReceipt success plus every
   path/digest/model/lane/fold/candidate/FeatureSeal/replay mismatch failure;
7. each four-file crash point, safe matching orphan completion, orphan
   non-loadability, missing sidecar rejection, and immutable byte conflict;
8. AST/module-import guards proving Task8 imports no outcome provider, metric
   evaluator, labelled-ledger API, or legacy top-level pipeline; and
9. package exports of `PredictionSealV2`, `seal_oof_predictions_v2`,
   `load_prediction_seal_v2`, and `load_prediction_rows_v2` only for Task8;
   V1 execution adapters must be rejected by Task6A/Task8B/Task6B.

Run focused Task6P calibration, Task8, feature-seal, contracts, RunSpec,
source-gate, scoring-card, baseline-replay, and canonical suites after each
stage; run Ruff
on changed Python files; validate changed/new schemas with Draft 2020-12; and
run `git diff --check`. Do not restart or duplicate the separately running
full-suite baseline.

## Normative Task6B authenticated-outcome handoff

This appendix is a normative precondition for Task6B. It replaces every
Common Task6 phrase that accepts raw outcome mappings, labels, or caller
chosen outcome paths. Task6B owns its outcome schema, `OutcomeRow`,
`OutcomeReceipt`, persistence, and evaluation; Task8 remains label-free.

The only Task6B APIs that open or consume outcomes are:

```python
def seal_outcome_receipt(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt,
    prediction_seal: PredictionSealV2,
) -> OutcomeReceipt

def load_outcome_receipt(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt,
    path: Path | str,
    expected_sha256: str,
) -> OutcomeReceipt

def load_outcome_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt,
    receipt: OutcomeReceipt,
) -> tuple[OutcomeRow, ...]

def evaluate_predictions(
    spec: ModelImprovementRunSpec,
    candidate: PredictionSealV2,
    baseline: PredictionSealV2,
    outcome_receipt: OutcomeReceipt,
) -> EvaluationPair
```

There is no raw mapping, label sequence, or outcome-path parameter. Task6B
must use a type-only/late import where necessary so its new outcome module
does not create a Task6A--Task8 import cycle.

### Exact authority chain

`seal_outcome_receipt` and both loaders first securely reauthenticate the
provided SourceGateReceipt at the exact RunSpec source-gate path. They require
its RunSpec and inventory digest bindings, then securely reread the exact
SourceInventory authority path and bytes pinned by
`spec.source_authority.inventory.path` and
`spec.source_authority.inventory.sha256`. SourceGateReceipt has no direct
GameEvidenceInventory field; it is therefore insufficient by itself.

The canonical SourceInventory parser must derive the sole closed
`GameEvidenceInventory` binding (relative path and SHA-256) from that
authenticated inventory. A caller cannot provide, replace, append, or select
this path. Task6B derives the authority root from the verified RunSpec,
revalidates the SourceInventory binding, calls
`load_verified_game_evidence_inventory(authority_root,
read_only_progress=True,
run_spec_path=authority_root / spec.source_authority.source_run_spec.path,
acquisition_contract_path=authority_root /
spec.source_authority.acquisition_contract.path)`, and requires both its
returned path relative to the authority root and its SHA-256 to equal the
derived SourceInventory binding. Reparse/rebind failures reject the whole
receipt before any outcome byte is written.

This exact chain is required on sealing and every reread:

```text
verified RunSpec
  -> reauthenticated SourceGateReceipt
  -> exact SourceInventory path + digest
  -> SourceInventory-derived GameEvidenceInventory relative path + digest
  -> load_verified_game_evidence_inventory revalidation
  -> exact prediction population in prediction order
```

### Outcome row and receipt contracts

`OutcomeRow` is a frozen, closed value with exactly:

```text
game_pk, official_date, target_year, scheduled_start_utc, home_win,
source_game_sha256
```

`home_win` is a bool and is not caller-supplied. It is derived only after the
verified GameEvidenceInventory proves: `played is True`,
`terminal_status` is exactly `Final` or `Completed Early`,
`verification_status == "VERIFIED"`, both scores are non-bool integers at
least zero, scores are unequal, and `winner_team_id` is the home team exactly
when the home score is larger (otherwise the away team). The row's source game
digest is copied exactly from that verified evidence row.

The evidence game must match the prediction row by game PK, official date,
target year, and scheduled UTC. The target year and official-date year must
match the Task8 fixed fold-year map. For a resumed game, identity remains its
original `official_date` and retained `scheduled_start_utc`; no actual-resume
timestamp can relabel or reorder it. A postponed, cancelled, unfinished,
unplayed, tied, missing, duplicate, mismatched-date/UTC/year, nonverified, or
nonterminal game rejects the entire receipt. Task6B never takes an
intersection, marks a row VOID, or silently drops a prediction game.

An OutcomeReceipt is produced **once per fold**, only from the derived
baseline `baseline/v5.1/{fold}` PredictionSealV2 and its Common FeatureSeal.
`seal_outcome_receipt` rejects every nonbaseline prediction identity and
every non-Common feature seal. Outcome rows have exactly that baseline
prediction's already-sealed order and Common-feature population; no candidate
can seal its own receipt.
Their raw preimage is:

```python
outcome_rows_bytes = b"".join(
    canonical_json_bytes(row.as_mapping()) for row in ordered_outcome_rows
)
outcomes_sha256 = sha256(outcome_rows_bytes).hexdigest()
```

The persisted OutcomeReceipt payload is closed with exactly:

```text
schema = "MODEL_IMPROVEMENT_OUTCOME_RECEIPT_V1"
ledger_id, fold_id, model_id, candidate_id,
run_spec_sha256, source_gate_sha256, source_inventory_sha256,
game_evidence_inventory_relative_path, game_evidence_inventory_sha256,
feature_seal_sha256, baseline_replay_sha256,
prediction_seal_sha256, prediction_rows_sha256,
prediction_contract_sha256, scoring_model_card_sha256,
population_sha256, outcomes_sha256
```

`artifact_sha256` and `path` are runtime descriptor fields only. The receipt
must bind its own prediction seal in full: identity, RunSpec, source gate,
FeatureSeal, baseline replay, prediction-card, contract, rows, and population
digests all equal the securely reloaded typed inputs. Its
`source_inventory_sha256` equals the authenticated source-gate inventory
digest. `game_evidence_inventory_relative_path` is a normal relative path,
but only the SourceInventory parser may create it; a payload path must equal
that derived value exactly.

For that baseline prediction identity, Task6B derives the exact sibling set
rather than accepting an output location:

```text
outcome_rows.jsonl
outcome_rows.jsonl.sha256
outcome_receipt.json
outcome_receipt.json.sha256
```

They share exactly the baseline
`oof/baseline/v5.1/{fold}` prediction directory. The
sidecar-first immutable order is outcome-row sidecar, receipt sidecar,
outcome-row primary, then outcome-receipt primary as the sole commit point.
Matching pre-primary sidecars/rows are uncommitted retry prerequisites;
missing/mismatched sidecars, different primary bytes, and a published receipt
without both verified row artifacts fail closed. No outcome artifact is
loadable by Task8.

### Evaluation admission

`evaluate_predictions` accepts only securely reloaded candidate and baseline
PredictionSeals plus a securely reloaded OutcomeReceipt. In production the
receipt is sealed from the baseline prediction seal; it must have
`ledger_id == baseline.ledger_id`. The candidate and baseline must have the
same fold, target-year map, source gate, prediction contract, and identical
ordered population digest. The receipt's rows must match the baseline seal
exactly; candidate eligibility is checked against that same population, never
through an outcome intersection.

The candidate-to-baseline feature admission is deliberately branch-specific.
For A and A+B, the candidate Common FeatureSeal artifact digest must equal the
baseline replay's Common `feature_seal_sha256`, with matching fold, target
year, source gate, RunSpec, and population. For A+B+C, Task6B must securely
reload the candidate Lane-C FeatureSeal and its exact derived parent Common
seal and require
`lane_c.parent_feature_seal_sha256 == baseline_replay.feature_seal_sha256`,
plus equality of fold, target year, source gate, RunSpec, and population across
the Lane-C seal, its parent, and the replay receipt. It must bind the
candidate prediction seal to the Lane-C digest but must reject a direct
Lane-C-artifact-to-baseline-replay digest equality test. `load_outcome_receipt`
validates the one baseline/Common receipt; `evaluate_predictions` performs the
candidate relation before metrics. Metrics may convert the verified `home_win`
bool to their local binary target only after these checks.

### Persisted evaluation provenance

Before any metric computation, `evaluate_predictions` must securely reload the
explicit OutcomeReceipt primary through `load_outcome_receipt` using its
canonical path and expected digest, then compare the entire returned runtime
descriptor (identity, RunSpec, source gate, SourceInventory,
GameEvidenceInventory path/digest, Common FeatureSeal, baseline replay,
baseline prediction/row/card/contract/population bindings, primary path, and
artifact digest) with the expected baseline-fold descriptor. Passing an
`outcomes_sha256` string, a decoded receipt mapping, or an arbitrary outcome
path is not an alternative input.

`EvaluationReport` is a closed persisted payload with exactly:

```text
schema = "MODEL_IMPROVEMENT_EVALUATION_REPORT_V1"
ledger_id, candidate_id, model_id, baseline_model_id, fold_id,
run_spec_sha256, source_gate_sha256, prediction_seal_sha256,
baseline_prediction_seal_sha256, population_sha256,
outcome_receipt_path, outcome_receipt_sha256, outcomes_sha256,
all_games_count, brier, log_loss, daily_top1_count, daily_top1_correct,
daily_top1_delta_vs_baseline, max_losing_streak,
paired_days_sha256, neutral_days_sha256
```

`EvaluationPair` is likewise a closed persisted payload with exactly:

```text
schema = "MODEL_IMPROVEMENT_EVALUATION_PAIR_V1"
ledger_id, candidate_id, candidate_model_id, baseline_model_id, fold_id,
run_spec_sha256, source_gate_sha256,
candidate_prediction_seal_path, candidate_prediction_seal_sha256,
baseline_prediction_seal_path, baseline_prediction_seal_sha256,
population_sha256, outcome_receipt_path, outcome_receipt_sha256,
outcomes_sha256, paired_days_sha256, neutral_days_sha256,
candidate_report_path, candidate_report_sha256,
baseline_report_path, baseline_report_sha256
```

In both schemas `outcome_receipt_path` is the canonical absolute string for
the exactly derived baseline sibling
`oof/baseline/v5.1/{fold}/outcome_receipt.json`; it is not caller-selected.
`outcome_receipt_sha256` is the SHA-256 of that receipt primary. Candidate and
baseline reports for one pair must contain byte-for-byte identical
`outcome_receipt_path`, `outcome_receipt_sha256`, and `outcomes_sha256`, and
the pair must contain those same values. `outcomes_sha256` remains useful for
the canonical OutcomeRow bytes but the stale outcomes-SHA-only evaluation
contract is explicitly superseded: it can never establish outcome provenance
without the receipt path and digest.

`load_evaluation_report` and `load_evaluation_pair` must lexically rederive
every named path before opening it, secure-read the mandatory sidecar and
primary, recompute the primary digest, parse only the closed canonical payload,
and reject aliases. Each loader rederives the baseline receipt path from the
fold, requires the persisted path to equal it, invokes `load_outcome_receipt`
with the persisted digest, and rechecks the full receipt descriptor and source
chain above. The pair loader also securely reloads the named candidate and
baseline reports; each report must bind the same reloaded receipt. Pair
publication is sidecars first and the pair primary last, after the named
candidate and baseline report primaries have been committed and verified, so a
published pair cryptographically names reports that themselves name the same
receipt. The frozen-card downstream path must consume only this loaded,
receipt-verified EvaluationPair; it may not rely on `outcomes_sha256` alone.

The implementation plans must add these closed outcome/evaluation
schemas/loaders/tests and patch the Common Task6 raw-mapping and
outcomes-SHA-only prose, Lane A/B/C/H24 callers, and frozen-card downstream
references to persist and securely reload the receipt path/digest chain.
Required tests include SourceInventory and
GameEvidenceInventory path/digest substitution, evidence rebind mutation,
every forbidden game state, resumed-game identity, missing/extra/duplicate
population, sidecar crash points, candidate/baseline population mismatch,
both feature-compatibility branches including the parent-Common/Lane-C/
baseline-row target-year and official-date-year checks, receipt path/digest
substitution, report receipt mismatch, and pair/report receipt disagreement.

## Self-review

The design contains no Task8 arbitrary outcome mapping or labelled-row
persistence/API. The V2 required `feature_seal_sha256` and
`calibration_card_sha256` are in the PredictionSealV2 payload, V2 schema,
sealer, descriptor loader, and row loader; published V1 material remains
inspection-only.
The staged Task6P/Task8A V2/Task6A/Task8B/Task6B order gives every baseline
and nonbaseline probability a prior immutable, pre-target calibration
authority; it leaves target-fold outcome authority exclusively to Task6B. The
calibration source data is not claimed to exist: its absence blocks execution
with `BLOCKED_CALIBRATION_AUTHORITY_MISSING`. The only necessary feature scope
correction is Common OOF FeatureSeal
admission for O19--O23, which is required to authenticate the Task7 OOF
scoring-card population. Direct feature-seal equality is restricted to
Common A/A+B versus the baseline replay; A+B+C is admitted only through its
securely reloaded Common parent relation. Task6B persists and reloads one
baseline/Common OutcomeReceipt per fold through both reports and their final
pair, so no frozen-card consumer can treat `outcomes_sha256` as provenance on
its own. No JoinedLedger, outcome join, raw mapping, or label-bearing Task8
interface remains in scope.
