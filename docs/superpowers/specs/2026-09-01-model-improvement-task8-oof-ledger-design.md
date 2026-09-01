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
belong to the Common Task6 authenticated baseline-outcome derivation and
persistence boundary. Task6 must first design a
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
Task6P  source-derived, pre-target CalibrationFeatureReceiptV1 and ProbabilityCalibrationCardV2 authority
Task8A  Common OOF FeatureSeal admission + V2 baseline v5.1 label-free seals/loaders
Task6A  baseline replay receipt, sealed only after the matching baseline seal
Task8B  deferred: always blocked pending a separately approved candidate-selection authority design
Task6B  authenticated baseline `OutcomeReceipt`/`OutcomeRow` derivation and persistence
```

This replaces the shorthand `Task7 -> Task8 -> Task6`. The current executable
chain is baseline-only: Task6P prepares only prior-season calibration evidence,
the baseline prediction seal precedes its replay receipt, and target-fold
outcomes open only in Task6B after the required baseline prediction seals are
immutable. Task8B/nonbaseline work must fail
`BLOCKED_WEIGHT_SELECTION_AUTHORITY_MISSING` until a separate reviewed design
is approved.

Task8 does not download data, modify the audited v5.1 card, use a
live/experimental output, start an H24 run, or add a compatibility execution
path. The current Task6P is admission-only: it validates existing derived
calibration authority and never rewrites the audited v5.1 card.

## Task6P probability-calibration authority

`ProbabilityCalibrationCardV2` is the only executable authority allowed to
turn a sealed edge into `p_home`. It is not a Task8 artifact and it is the
only permitted label-bearing artifact before Task6B: its labels are strictly
*prior-target* outcomes used for fitting, never target-fold outcomes or future
analysis labels. `MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V1`,
introduced by commit `2fa82cd`, is inspection-only and is never executable:
it did not bind a typed source/evidence chain, authenticated weights, or a
recomputed feature edge. Its loader must reject it for Task8/Task6A/Task8B
execution rather than treating a structurally valid V1 payload as authority.

There are seven immutable calibration keys, keyed by exact fold identity, not
merely target year. O22/H22 and O23/H23 may have identical permitted-year
pools but are distinct immutable receipts, cards, and paths.

| calibration key | consumers | permitted official-date years |
| --- | --- | --- |
| C19 | O19 | 2018 |
| C20 | O20 | 2018--2019 |
| C21 | O21 | 2018--2020 |
| C22O | O22 | 2018--2021 |
| C22H | H22 | 2018--2021 |
| C23O | O23 | 2018--2022 |
| C23H | H23 | 2018--2022 |

For a key's pool, every calibration input and emitted calibration row has an
official date within its declared dates, a target year strictly earlier than
the consumer target year, and a D-1 feature cutoff. A target-year row, a
future source revision substituted for bound source evidence, an arbitrary
outcome mapping, an arbitrary feature-row mapping, or a caller-selected path
is a hard failure. There is no baseline C24 in this work: H24 remains
prediction-only and must consume a separately frozen qualified card later.

### Acyclic pre-RunSpec calibration-feature trust root

The calibration feature evidence must exist **before** the RunSpec is sealed;
otherwise a card that says its inputs agree with the RunSpec would be a
self-signed authority. `probability_calibration_policy` therefore contains
the exact, closed seven-entry `calibration_feature_receipts` mapping keyed by
`C19`, `C20`, `C21`, `C22O`, `C22H`, `C23O`, and `C23H`. Each value is exactly
`{"path": <canonical absolute normal path>, "sha256": <lowercase SHA-256>}`.
The path names `calibration_feature_receipt.json`; it is an exact immutable
input pin, not a repository-relative template or a caller argument. The
RunSpec digest pins this mapping, while the receipt does not bind the RunSpec
digest and therefore cannot complete a cycle.

`CalibrationFeatureReceiptV1` is a pre-RunSpec, label-free artifact with
schema literal `MODEL_IMPROVEMENT_CALIBRATION_FEATURE_RECEIPT_V1`. Its exact
primary is `calibration_feature_receipt.json`, with mandatory
`calibration_feature_receipt.json.sha256`; its only sibling row primary is
`calibration_feature_rows.jsonl`, with mandatory
`calibration_feature_rows.jsonl.sha256`. Its closed primary payload is exactly:

```text
schema, model_id, lane_id, fold_id, calibration_key,
target_year, training_start, training_end,
source_authority_sha256, source_inventory_sha256,
game_evidence_inventory_relative_path, game_evidence_inventory_sha256,
feature_contract_sha256, feature_code_revision,
feature_rows_path, feature_rows_sha256, feature_population_sha256
```

`feature_rows_path` must equal the canonical absolute sibling path byte for
byte; the primary/rows sidecars bind their raw primary bytes. The receipt
deliberately contains neither `run_spec_sha256`, `source_gate_sha256`, nor a
weight binding. It is reusable feature evidence; a later RunSpec selects and
pins it, and a later calibration card binds independently resolved weights.

Rows are **not** `SealedFeatureRow`. Every line is a distinct
`CalibrationFeatureRowV1` whose closed payload is exactly:

```text
schema, model_id, lane_id, fold_id, calibration_key,
game_pk, official_date, scheduled_start_utc, max_history_date, target_year,
history_start_date, history_end_date, home_team_id, away_team_id,
component_scores, component_qualities,
source_feature_file_sha256, source_feature_content_sha256,
source_authority_sha256, feature_row_sha256
```

`schema` is `MODEL_IMPROVEMENT_CALIBRATION_FEATURE_ROW_V1`.
`component_scores` and `component_qualities` are closed six-component
home/away mappings in the frozen feature-contract component order; both team
IDs must match the verified game. `feature_row_sha256` is SHA-256 of the
canonical row preimage consisting of every listed field except itself (no
runtime field is included). Thus it is derived from that row's own complete,
label-free canonical content and cannot name a `SealedFeatureRow`. Rows are
strictly ordered by `(official_date, scheduled_start_utc, game_pk)`, unique by
game/population, have `max_history_date == official_date - 1 day`, and have no
`run_spec_sha256` or `source_gate_sha256` field. A digest-only feature claim
is insufficient.

`source_authority_sha256` is SHA-256 of canonical JSON for this exact
pre-RunSpec binding object, independently copied from the authority a later
RunSpec will require:

```text
authority_root,
source_run_spec: {path, sha256},
acquisition_contract: {path, sha256},
inventory: {path, sha256},
terminal_checkpoint: {path, sha256}
```

The absolute preimage resolution is exact. The activation authority base is
`Path(spec.verified_activation.declaration_path).parents[2]`; `authority_root`
is that base joined to relative `spec.source_authority.authority_root`.
Each nested path resolves beneath this authority root. The resulting canonical
no-alias absolute strings and the existing lowercase SHA-256 values form the
preimage above. The pre-RunSpec producer must use this identical declared
activation base and source bindings. This preimage contains no RunSpec digest,
receipt digest, source-gate digest, weight, outcome, or target label. After
RunSpec sealing, Task6P reconstructs it from `RunSpec.source_authority`, then
requires its digest to equal both receipt and every row and compares every
bound path/digest to the typed SourceGate/SourceInventory/VerifiedGameEvidence
chain; disagreement is `ValueError` rather than alternate authority.

There is presently no source-to-feature producer/re-executor that can create
such a receipt from the V5.2 authority. That producer is deliberately outside
this plan. No real receipt is claimed, no synthetic receipt is usable outside
tests, and a missing pinned receipt blocks before any V2 calibration,
prediction, or replay output.

### Per-fold execution order

The seven receipt primaries must all exist before RunSpec sealing, but their
admission is otherwise per `(identity, fold_id)` rather than a global
"calibrate everything, then predict everything" stage. For each tuple the
only permitted order is: (1) derive the fixed calibration key and receipt pin;
(2) load/revalidate receipt, bound gate, typed inventory and verified games;
(3) resolve the tuple's authenticated weights; (4) recompute each edge and
prior-target `home_win`, then refit/compare the V2 card; (5) securely reread
all inputs and admit the V2 card; (6) only then allow that tuple's Task8A
prediction seal and Task6A replay. This prevents one fold's later authority
from becoming a prerequisite or an input for another. Task8B is not a step in
this sequence: it is a future design placeholder and currently always blocks
with `BLOCKED_WEIGHT_SELECTION_AUTHORITY_MISSING`.

### Typed source, outcome, and weight authority

Task6P must use one shared read-only source-authority loader. It first calls
the public `load_bound_source_gate_receipt(spec, supplied_gate)` wrapper,
which derives the sole source-gate path, canonical-reloads it and its sidecar,
compares the full supplied descriptor, and revalidates the activated
source-gate binding without accepting an arbitrary path. It then reads the
exact gate/RunSpec-bound inventory bytes, verifies their SHA-256, and parses
them with the existing typed `v52_snapshot_store.parse_inventory`; it must not
decode an untyped mapping. From that typed `SourceInventory` it loads the
existing `VerifiedGameEvidenceInventory` with
`load_verified_game_evidence_inventory(authority_root,
read_only_progress=True, run_spec_path=..., acquisition_contract_path=...)`
and requires its relative path and SHA-256 to equal the inventory's
`game_evidence_inventory` binding. The source inventory, game-evidence
inventory, receipt, and all source bindings are reloaded once before work and
once immediately before return/publication; any drift is `ValueError`.

Task6P alone defines private
`_iter_verified_calibration_games_v1(verified_inventory)`. It obtains no
caller mapping: it reads only the nonempty list
`verified_inventory.payload["games"]`, which
`load_verified_game_evidence_inventory` has already fully schema-validated.
It iterates that list in its canonical stored order and requires selected
positive integer `game_pk` values to be strictly increasing and unique. It
does **not** impose an exact-key schema on each full game mapping. Instead it
projects and runtime-validates only the fields required here:

```text
game_pk, official_date, scheduled_start_utc, home_team_id, away_team_id,
winner_team_id, home_score, away_score, played, terminal_status,
verification_status, source_game_sha256
```

Dates/timestamps, team IDs, scores, terminal/verification values and source
digest must meet the existing typed rules; the typed inventory loader remains
responsible for every other wire field. No caller-provided outcome/feature
mapping is accepted. The helper returns immutable projections for the
team/date/source comparison and internal `home_win` derivation below.

For baseline, Task6P resolves weights only through
`resolve_prediction_weight_authority(spec, "v5.1", None)`. It canonicalizes
the resolver's repository-relative direct-card path to the repository root,
then requires exact model, path, digest, and parsed six-component pp weights.
Nonbaseline weight resolution is outside this executable plan. A raw file SHA
or a card-declared digest alone is not a weight authority.

For every input feature row, Task6P recomputes rather than accepts its edge:

```python
edge = math.fsum(
    weights[component] / 100.0
    * (
        feature.component_scores["home"][component]
        - feature.component_scores["away"][component]
    )
    for component in COMPONENTS
)
```

It derives `home_win` internally from the typed verified game row only after
the label-free receipt is sealed: `terminal_status` is `Final` or `Completed
Early`, `played is True`, `verification_status == "VERIFIED"`, scores are
non-bool unequal nonnegative numbers, and `winner_team_id` is consistent with
the home/away score. The emitted row records its source-game SHA-256 and the
bound feature-row SHA-256. No caller supplies outcomes.

The typed authority and feature receipt are checked before the row/fit work
and again immediately before the card primary becomes visible. Missing any
pinned receipt, card, sidecar, typed source input, or direct authority raises
`CalibrationAuthorityBlockedError` with
`BLOCKED_CALIBRATION_AUTHORITY_MISSING` before a V2 output is opened. A
present primary with a missing/malformed sidecar, a digest/path/type mismatch,
or any invalid content is `ValueError`, never a recoverable missing authority.

### V2 cards and paths

The executable primary schema is
`MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V2` with exactly:

```text
schema, model_id, lane_id, fold_id, calibration_key,
target_year, training_start, training_end,
run_spec_sha256, source_gate_sha256, prediction_contract_sha256,
calibration_feature_receipt_path, calibration_feature_receipt_sha256,
weight_authority_path, weight_authority_sha256,
calibration_rows_path, calibration_rows_sha256,
calibration_population_sha256,
game_evidence_inventory_relative_path, game_evidence_inventory_sha256,
fit_spec, alpha, beta
```

The canonical V2 row has exactly `game_pk`, `official_date`, `target_year`,
`edge`, `home_win`, `feature_row_sha256`, and `source_game_sha256`.
`fit_spec` is a closed immutable validation policy; `alpha` and `beta` are
persisted coefficients that Task6P recomputes from these rows, not opaque
accepted values. Runtime descriptor fields `artifact_sha256` and `path` are
excluded from every payload.

The RunSpec contains policy and pre-existing **input receipt** pins, not an
emitted-card digest, at the exact closed key
`probability_calibration_policy`. Its `calibration_feature_receipts` member
has exactly the seven calibration keys and each value has exactly `path` and
`sha256`; every path is canonical absolute and every digest is a
lowercase 64-hex SHA-256. Concrete values are pre-execution inputs, so a
template, default path, or derived digest is never authority:

```json
{
  "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_POLICY_V1",
  "fold_keys": {"O19": "C19", "O20": "C20", "O21": "C21", "O22": "C22O", "H22": "C22H", "O23": "C23O", "H23": "C23H"},
  "fit_spec": {"schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1", "algorithm": "fit_logistic_v1", "edge_divisor": 10.0, "iterations": 100, "l2": 1e-6, "tolerance": 1e-10, "min_rows": 2, "required_classes": [0, 1]},
  "pools": {
    "C19": {"target_year": 2019, "training_start": "2018-01-01", "training_end": "2018-12-31"},
    "C20": {"target_year": 2020, "training_start": "2018-01-01", "training_end": "2019-12-31"},
    "C21": {"target_year": 2021, "training_start": "2018-01-01", "training_end": "2020-12-31"},
    "C22O": {"target_year": 2022, "training_start": "2018-01-01", "training_end": "2021-12-31"},
    "C22H": {"target_year": 2022, "training_start": "2018-01-01", "training_end": "2021-12-31"},
    "C23O": {"target_year": 2023, "training_start": "2018-01-01", "training_end": "2022-12-31"},
    "C23H": {"target_year": 2023, "training_start": "2018-01-01", "training_end": "2022-12-31"}
  },
  "calibration_feature_receipts": {
    "C19": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"},
    "C20": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"},
    "C21": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"},
    "C22O": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"},
    "C22H": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"},
    "C23O": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"},
    "C23H": {"path": "<canonical-absolute-calibration_feature_receipt.json>", "sha256": "<64-lowercase-hex>"}
  },
  "baseline_rows_relative_template": "baseline/v5.1/{fold_id}/v2/calibration_rows.jsonl",
  "baseline_card_relative_template": "baseline/v5.1/{fold_id}/v2/probability_calibration_card.json"
}
```

This object is part of the canonical RunSpec payload and is validated by the
RunSpec schema/dataclass with exact keys, fixed ordering, and no caller
overrides. Its `fit_spec` schema name is
`MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1` and it has exactly the
JSON members and constants shown above: the existing `fit_logistic` function
is invoked as algorithm `fit_logistic_v1`, with no default/caller overrides.
It intentionally contains neither V2 card/rows digests nor source outcomes:
those appear only in a subsequently validated card and would otherwise make
the source-gate authority circular. Nonbaseline path/identity templates are
intentionally absent: Task8B is deferred. The sole executable identity is
`model_id="v5.1"` and `lane_id="baseline"`; its
`calibration_key` is exactly
`RunSpec.probability_calibration_policy.fold_keys[fold_id]` and never a caller
argument.

The exact `outputs.namespaces` closed list additionally contains `calibration`:
`["source_gate", "features", "baseline", "calibration", "oof", "registry",
"h24_predictions"]`.
`calibration_paths_v2(spec, identity)` is the sole V2 output path helper. It
validates the fixed identity mapping above, substitutes only its derived
values into the policy templates, and calls only
`spec.output_path("calibration", relative_name)`. `output_path` must enforce
these closed calibration shapes and leaves before returning a path: baseline
is `baseline/v5.1/{fold_id}/v2/{calibration_rows.jsonl|probability_calibration_card.json}`.
The helper derives the two primaries and their `.sha256` sidecars; no caller
may invoke a calibration read/write with a hand-built path. All calibration
tests use this helper and direct `output_path` tests reject every other
calibration relative shape or leaf.

The sibling V2 rows primary is canonical JSONL with exactly `game_pk`,
`official_date`, `target_year`, `edge`, `home_win`, `feature_row_sha256`, and
`source_game_sha256`. Current Task6P is admission-only: it does not build,
fit, rewrite, or publish card/rows data. Its mandatory recomputation is only
an admission comparison against a pre-existing V2 card and rows primary.

Paths are derived, not provided:

```text
baseline/v5.1/{fold_id}/v2/calibration_rows.jsonl
baseline/v5.1/{fold_id}/v2/probability_calibration_card.json
```

`load_calibration_feature_receipt(spec, fold_id)`,
`load_probability_calibration_card_v2(spec, fold_id)`, and
`require_probability_calibration_card_v2(spec, fold_id)` are the only
operational APIs. They derive the baseline receipt pin and V2
paths and accept no path, receipt, rows, weights, or outcomes. The old
`load_probability_calibration_card` / `require_probability_calibration_card`
V1 surface is inspection-only and must not be exported to Task6A, Task8, or
Task8B execution. The V2 APIs first reject any nonbaseline identity and any
fold-derived key mismatch. The
V2 loader returns the fully revalidated immutable card; the V2 require helper is the
read-only admission gate used by seal/replay code. If the exact derived
prior-outcome card, rows, sidecar, receipt, or typed evidence authority is absent,
the loader and every V2 sealer/replay caller raise the dedicated
`CalibrationAuthorityBlockedError` (`RuntimeError`) whose immutable
`code` and complete string are exactly `BLOCKED_CALIBRATION_AUTHORITY_MISSING`.
It is a pre-publication disposition, not a JSON artifact state: it writes no
rows, card, V2 prediction seal, or replay receipt and must be propagated
unchanged. A present-but-malformed or digest-mismatched authority instead
raises `ValueError`. No value may be invented or inherited from the global
2022--2024 v5.1 card.

Every persisted card path field is a canonical absolute path. The card and rows
paths must equal, byte-for-byte, the fold-derived paths under
`spec.outputs.root/calibration` through `calibration_paths_v2`; the existing
`PredictionWeightAuthority.path` semantics remain unchanged. Instead, Task6P
computes the card's `weight_authority_path` by securely resolving and
canonicalizing the authenticated authority path: baseline's repository-relative
path is resolved against the repository root derived exactly as
`run_spec._repository_root()` and the current authenticated loader do. The persisted field is
compared to that canonicalized absolute form, never to a raw descriptor
`.path`; and
`game_evidence_inventory_relative_path` and SHA must equal the securely
reloaded typed inventory binding. The admission loader
securely rereads the complete card/rows/sidecars, weight authority, source
gate, and game-evidence authority before derivation and again immediately
before returning; it performs no writes. Seal/replay callers repeat that full
admission immediately before their own commit/return.

After rows/card/sidecars and all binding digests are securely reread, admission
requires the card `fit_spec` to equal the RunSpec's exact frozen `fit_spec`.
It passes canonical row edges divided by `edge_divisor` and their ordered
binary outcomes to the existing `normalize.fit_logistic` with the frozen
`iterations`, `l2`, and `tolerance`; it enforces at least `min_rows`, exactly
the required classes, finite recomputed/card `alpha`, and finite positive
recomputed/card `beta`. The card values must equal the recomputed values under
canonical numeric serialization exactly; any fit-spec or coefficient mutation
raises `ValueError`. This is admission validation only, not a fitting-output
publication. Future producer work, including any new
`fit_pre_target_calibration` flow or `normalize.py` modification, remains
outside this executable plan.

## V2 baseline replay receipt

`BaselineReplayReceiptV2` is V2-only and lives in
`mlb_backtest/model_improvement/baseline_replay_v2.py`; V1 remains
inspection-only. Its schema is
`schemas/model_improvement/baseline_replay_receipt_v2.schema.json` and its
primary schema literal is
`MODEL_IMPROVEMENT_BASELINE_REPLAY_RECEIPT_V2`, with exactly this payload:

```text
schema, ledger_id, fold_id, model_id,
run_spec_sha256, source_gate_sha256, feature_seal_sha256,
prediction_contract_sha256, scoring_model_card_sha256,
calibration_card_path, calibration_card_sha256, population_sha256,
replay_one_rows_sha256, replay_two_rows_sha256,
replay_one_bytes_sha256, replay_two_bytes_sha256,
prediction_rows_sha256, prediction_seal_path, prediction_seal_sha256
```

`artifact_sha256` and `path` are runtime-only. Its only path is
`oof/baseline/v5.1/{fold}/v2/baseline_replay_receipt.json`. It is valid only when the
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

No nonbaseline feature namespace is added to the executable plan. Existing Common
H22/H23/H24 feature admissions remain unchanged. Feature-row validation uses
the Task8 map for Common OOF folds and the existing RunSpec fold mapping
for H22/H23/H24. Every row still proves D-1 history, its source-gate digest,
`source_manifest_sha256 == spec.source_authority.inventory.sha256`, and its
existing feature-row hash. H24 can remain a feature seal for the future
registry, but all Task8 identity parsers, sealers, and loaders reject H24.

The only Task8 prediction identities and primary directories are:

| model | ledger ID | permitted folds | exact directory |
| --- | --- | --- | --- |
| v5.1 | `baseline/v5.1/{fold}` | O19--O23, H22, H23 | `oof/baseline/v5.1/{fold}/v2` |

H24, a bare fold ID, another model/scope pairing, and a 2025 target all fail
before a path is calculated.

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
ledger_id, fold_id, model_id,
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
inadmissible for every executable V2 model-result path.

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

Task8A baseline is the only executable branch. Task8B is deferred and fails
before these APIs load any nonbaseline input. The exact canonical API surface is:

```python
def seal_oof_predictions_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None,
    ledger_id: str,
    rows: Sequence[PredictionRowV2],
) -> PredictionSealV2

def load_prediction_seal_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None,
    path: Path | str,
    expected_sha256: str,
) -> PredictionSealV2

def load_prediction_rows_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None,
    seal: PredictionSealV2,
) -> tuple[PredictionRowV2, ...]
```

Task8A has `baseline_replay is None` and must not use a duck-typed mapping in
place of any authenticated input. Task6A imports only the Task8A baseline
sealer.

For every call, Task8 first parses the ledger ID, derives every exact output
path, and securely revalidates its inputs before generating or reading bytes:

* `source_gate` securely reloads from the sole RunSpec
  `source_gate/source_gate_receipt.json` path. The reloaded full descriptor,
  RunSpec digest, and inventory digest must match the supplied value and
  `spec.source_authority.inventory.sha256`.
* `feature_seal` securely reloads from its exact derived Common path for
  v5.1 Common path only. The full descriptor, RunSpec, source gate, fold,
  feature set, and new `seal.feature_seal_sha256` binding must match.
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
  exact fold-specific `ProbabilityCalibrationCardV2` before materializing a row. It
  validates the card's source gate, contract, direct v5.1 weight authority,
  pre-target pool, feature and GameEvidence provenance, then uses its alpha
  and beta in `home_probability`. Its digest is required in every row and
  seal; `scoring_model_card_sha256` continues to name the direct weight
  authority and `frozen_model_card_sha256` remains null. No identity
  calibration and no global 2022--2024 calibration is an alternative.
* **Nonbaseline block:** Task8B/nonbaseline work has no current identity,
  path, loader, schema, or API and fails before artifact I/O with
  `BLOCKED_WEIGHT_SELECTION_AUTHORITY_MISSING`. A separate reviewed design is
  required; Task8A never computes a label or imports an outcome authority.

## Lexical path checks and authoritative loader behavior

Before any filesystem call, every caller-supplied descriptor path must be a
nonempty canonical absolute string, equal both to `str(Path(raw))` and
`os.path.normpath(raw)`, with no `.`/`..` segment or duplicate slash alias.
The path is decoded structurally under `outputs.root/oof`, converted to one
accepted ledger identity, and required to equal the derived path byte for
byte. Relative, `./`, `../`, `//`, wrong namespace, wrong identity, and wrong
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
cross-feature-set, cross-source-gate, cross-inventory,
cross-contract, cross-card, or cross-replay condition raises `ValueError`
before the affected descriptor is published or returned. Prerequisite-only
crash remnants follow the immutable orphan rules above. Evidence is never
deleted, rewritten, or converted into a label-bearing prediction artifact.

The Task6P + Task8 V2 implementation boundary is limited to:

```text
Create  schemas/model_improvement/calibration_feature_receipt.schema.json
Create  schemas/model_improvement/probability_calibration_card_v2.schema.json
Create  mlb_backtest/model_improvement/calibration_feature_receipts.py
Modify  mlb_backtest/model_improvement/probability_calibration.py # V2 only; V1 inspection parser is non-executable
Create  tests/test_model_improvement_calibration_feature_receipt.py
Create  tests/test_model_improvement_probability_calibration.py
Modify  schemas/model_improvement/parallel_model_result_run_spec.schema.json
Modify  schemas/model_improvement/feature_seal.schema.json
Modify  mlb_backtest/model_improvement/run_spec.py
Modify  mlb_backtest/model_improvement/source_gate.py      # public read-only load_bound_source_gate_receipt
Modify  mlb_backtest/model_improvement/sealed_features.py
Modify  mlb_backtest/model_improvement/contracts.py       # add V2 row/seal calibration types
Create  schemas/model_improvement/prediction_seal_v2.schema.json
Create  mlb_backtest/model_improvement/oof_ledger_v2.py    # label-free only
Modify  mlb_backtest/model_improvement/__init__.py
Create  tests/test_model_improvement_oof_ledger.py
Modify  tests/test_model_improvement_sealed_features.py
Modify  tests/test_model_improvement_contracts.py
Modify  tests/test_model_improvement_run_spec.py
Modify  tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json
Modify  docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-lane-a.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md
Modify  docs/superpowers/plans/2026-08-31-model-improvement-h24-integration.md
```

Focused tests must prove:

1. all seven fixed fold/year pairs plus every H24, 2025, and unknown-fold
   rejection path;
2. baseline calibration/prediction namespaces and sibling names, plus
   relative/alias/`..`/duplicate-slash rejection before reads;
3. Common OOF FeatureSeal admission for O19--O23;
4. closed label-free row/seal schemas, strict order/uniqueness/population
   preimage, and feature-row hash/population substitution rejection;
5. secure source-gate/inventory/FeatureSeal/RunSpec/prediction-contract
   mismatches at both write and reload;
6. all seven fold-specific baseline pre-target calibration pools, including
   distinct O22/H22 and O23/H23 pre-RunSpec receipt pins; full label-free
   feature-row, typed SourceInventory/VerifiedGameEvidence, direct-weight,
   recomputed-edge/outcome/fit provenance; card/sidecar/orphan and
   missing-authority rejection; primary-present/sidecar-missing invalidity;
   baseline direct-card success plus wrong path/digest/weights/calibration failure;
7. each four-file crash point, safe matching orphan completion, orphan
   non-loadability, missing sidecar rejection, and immutable byte conflict;
8. AST/module-import guards proving Task8 imports no outcome provider, metric
   evaluator, labelled-ledger API, or legacy top-level pipeline; and
9. package exports of `CalibrationFeatureReceiptV1`,
   `load_calibration_feature_receipt`, `load_probability_calibration_card_v2`,
   `require_probability_calibration_card_v2`, `PredictionSealV2`, `seal_oof_predictions_v2`,
   `load_prediction_seal_v2`, and `load_prediction_rows_v2` only for Task8;
   V1 execution adapters must be rejected by Task6A and Task6B.

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
`OutcomeReceipt`, derivation, and persistence; Task8 remains label-free.
Evaluation and metrics are unadmitted in the current executable scope.
Every Task6B baseline `OutcomeReceipt` consuming a V2 prediction/replay is
derived and persisted only beneath that identity's `/v2` namespace
(`oof/baseline/v5.1/{fold}/v2/`); no V2 reader may read or overwrite a V1
outcome path.

The only Task6B APIs that open or consume outcomes are:

```python
def seal_outcome_receipt(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2,
    baseline_prediction_seal: PredictionSealV2,
) -> OutcomeReceipt

def load_outcome_receipt(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2,
    path: Path | str,
    expected_sha256: str,
) -> OutcomeReceipt

def load_outcome_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2,
    receipt: OutcomeReceipt,
) -> tuple[OutcomeRow, ...]

```

No API accepts a raw outcome source or outcome-authority path parameter.
`load_outcome_receipt`'s `path` is solely an artifact-location assertion: it
must equal the canonical V2 receipt path rederived from the securely verified
baseline identity before any I/O, and cannot select the outcome authority.
Task6B must use a type-only/late import where necessary so its new outcome
module does not create a Task6A--Task8 import cycle.

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
prediction's already-sealed order and Common-feature population; no
nonbaseline identity can seal an outcome receipt.
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
ledger_id, fold_id, model_id,
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
`oof/baseline/v5.1/{fold}/v2` prediction directory. The
sidecar-first immutable order is outcome-row sidecar, receipt sidecar,
outcome-row primary, then outcome-receipt primary as the sole commit point.
Matching pre-primary sidecars/rows are uncommitted retry prerequisites;
missing/mismatched sidecars, different primary bytes, and a published receipt
without both verified row artifacts fail closed. No outcome artifact is
loadable by Task8.

Task8B/nonbaseline evaluation remains unadmitted; a separate reviewed design
must define weight-selection authority, schemas, APIs, paths, and tests.

## Self-review

The design contains no Task8 arbitrary outcome mapping or labelled-row
persistence/API. The V2 required `feature_seal_sha256` and
`calibration_card_sha256` are in the PredictionSealV2 payload, V2 schema,
sealer, descriptor loader, and row loader; published V1 material remains
inspection-only.
The executable Task6P/Task8A V2/Task6A/Task6B order gives each baseline
probability a prior immutable, pre-target calibration authority and leaves
target-fold outcome authority exclusively to Task6B. Nonbaseline work is
unadmitted and blocks with `BLOCKED_WEIGHT_SELECTION_AUTHORITY_MISSING`.
The calibration source data and its producer are not claimed to exist: missing
pre-RunSpec receipt, typed evidence, V2 rows, card, or sidecar blocks execution
with `BLOCKED_CALIBRATION_AUTHORITY_MISSING`; present invalid evidence is
`ValueError`. The only necessary feature scope
correction is Common OOF FeatureSeal
admission for O19--O23. Task6B persists one baseline/Common OutcomeReceipt per
fold. No JoinedLedger, outcome join, raw mapping, or label-bearing Task8
interface remains in scope.
