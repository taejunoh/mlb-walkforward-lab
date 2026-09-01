# Task8 OOF Label-Free Prediction-Seal Design

## Status, ownership, and corrected order

This document is the normative Common Task8 design. It supersedes the Task8
sections and all old Task8 function signatures in
`docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md` and in
the Lane A/B/C/H24 plans. The implementation plan that follows this design
must patch those documents before implementation begins; no consumer may keep
an older `seal_oof_predictions(spec, ledger_id, rows)` or spec-only row-loader
call.

Task8 owns immutable **label-free** OOF prediction seals, prediction JSONL,
their closed schema, and the one authoritative prediction-row loader. It owns
no outcome mapping input, labelled JSONL, outcome reader, or metric. Those
belong to the Common Task6 authenticated outcome/evaluation boundary. Task6
must first design a
source-gate-bound deterministic outcome authority and receipt, and must not
accept arbitrary caller mappings as a proxy for outcomes. Task8 implements
none of that producer or persistence.

The approved implementation and runtime activation sequence is:

```text
Task8A  Common OOF FeatureSeal admission + baseline v5.1 label-free seals/loaders
Task6A  baseline replay receipt, sealed only after the matching baseline seal
Task8B  nonbaseline label-free seals/loaders, binding the secure Task6A receipt
Task6B  authenticated outcome authority, outcome/evaluation join, and metrics
```

This replaces the shorthand `Task7 -> Task8 -> Task6`. Task7's scoring-card
code can be implemented and synthetically tested before this sequence, but a
real nonbaseline scoring card or Task8B prediction cannot be admitted until
the same-fold Task6A `BaselineReplayReceipt` exists. This removes the old
artifact cycle: baseline prediction seal precedes replay receipt, replay
receipt precedes a nonbaseline prediction seal, and outcomes open only in
Task6B after all required prediction seals are immutable.

Task8 is limited to the files named below. It does not download data, modify
the v5.1 card, use a live/experimental output, start an H24 run, or add a
second RunSpec/compatibility path.

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
| v5.1 | `baseline/v5.1/{fold}` | O19--O23, H22, H23 | `oof/baseline/v5.1/{fold}` |
| A | `lane_a/A/{fold}` | O19--O23, H22, H23 | `oof/lane_a/A/{fold}` |
| A+B | `lane_b/{candidate_id}/{fold}` | O19--O23, H22, H23 | `oof/lane_b/{candidate_id}/{fold}` |
| A+B+C | `lane_c/A+B+C/{fold}` | H22, H23 | `oof/lane_c/A+B+C/{fold}` |

`candidate_id` is required only for A+B and must remain the Task7 registered
safe segment `[A-Za-z0-9][A-Za-z0-9._-]*`, excluding `.` and `..`; it is null
for every other model. H24, a bare fold ID, another model/scope pairing, and a
2025 target all fail before a path is calculated.

For any accepted identity, `prediction_paths(spec, ledger_id)` is the sole
pure identity-to-path function. It calls only `spec.output_path("oof",
relative_name)` and returns these exact siblings:

```text
prediction_rows.jsonl
prediction_rows.jsonl.sha256
prediction_seal.json
prediction_seal.json.sha256
```

No Task8 API accepts an output directory, relative file name, glob, listing,
or caller-selected scoring-card path.

## Closed PredictionSeal schema and descriptor

`schemas/model_improvement/prediction_seal.schema.json` is a closed Draft
2020-12 document. Its closed definitions are `prediction_row` and
`prediction_seal`; each object has `additionalProperties: false` and every
digest is lowercase 64-hex. Persisted JSON always uses
`canonical_json_bytes` (sorted compact UTF-8 plus one newline).

The `prediction_row` definition requires exactly the existing `PredictionRow`
payload fields:

```text
model_id, game_pk, official_date, target_year, p_home, picked_side,
pick_probability, confidence, starter_edge_abs, scheduled_start_utc, rank,
feature_row_sha256, prediction_contract_sha256, scoring_model_card_sha256
```

It does not include `label`, `outcome`, `result`, `won`, a score, or any other
result field. The runtime dataclass remains the final validator for finite
ranges, pick/rank consistency, and digest syntax.

Task8A extends the pure `PredictionSeal` dataclass with the required field
`feature_seal_sha256`. Its exact persisted payload is:

```text
schema = "MODEL_IMPROVEMENT_PREDICTION_SEAL_V1"
ledger_id, fold_id, model_id, candidate_id,
run_spec_sha256, source_gate_sha256, feature_seal_sha256,
prediction_contract_sha256, scoring_model_card_sha256,
population_sha256, frozen_model_card_sha256, rows_sha256
```

`artifact_sha256` and `path` remain runtime descriptor fields. They are
computed only from the primary payload bytes after publication and are absent
from the payload, schema preimage, and payload hash. `as_mapping()` excludes
them. Both `PredictionSeal.from_payload_mapping` and every reader require the
new feature-seal field; no legacy payload is accepted.

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
def seal_oof_predictions(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    ledger_id: str,
    rows: Sequence[PredictionRow],
) -> PredictionSeal

def load_prediction_seal(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    path: Path | str,
    expected_sha256: str,
) -> PredictionSeal

def load_prediction_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    seal: PredictionSeal,
) -> tuple[PredictionRow, ...]
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

* **Baseline Task8A:** `ledger_id` must be `baseline/v5.1/{fold}` and
  `baseline_replay` must be `None`. Task8 securely reads exactly
  `spec.baseline.card.path`, recomputes its digest, validates the closed
  baseline-card bytes and exact RunSpec weights, and requires that digest in
  every row and seal. It creates no wrapper and sets
  `frozen_model_card_sha256` to null.
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

For Task8B A+B+C, `PredictionSeal.feature_seal_sha256` and the Task7 card
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

`load_prediction_seal` securely reads the exact primary and its mandatory
sidecar, recomputes the supplied digest, requires canonical JSON and the
closed schema, creates the runtime descriptor, and executes every input/card
binding above. A later consumer must compare a received descriptor in full,
not merely by digest.

`load_prediction_rows` first securely reloads the input seal. It then derives
`prediction_rows.jsonl` from `prediction_paths(spec, seal.ledger_id)` rather
than assuming a descriptor directory, secure-reads its sidecar and primary,
recomputes bytes/row digest/population, parses every canonical line through
`PredictionRow.from_payload_mapping`, and reruns the source gate, FeatureSeal,
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
Modify  schemas/model_improvement/feature_seal.schema.json
Modify  mlb_backtest/model_improvement/run_spec.py
Modify  mlb_backtest/model_improvement/sealed_features.py
Modify  mlb_backtest/model_improvement/contracts.py       # PredictionSeal field only
Create  schemas/model_improvement/prediction_seal.schema.json
Create  mlb_backtest/model_improvement/oof_ledger.py       # label-free only
Modify  mlb_backtest/model_improvement/__init__.py
Create  tests/test_model_improvement_oof_ledger.py
Modify  tests/test_model_improvement_sealed_features.py
Modify  tests/test_model_improvement_contracts.py
Modify  tests/test_model_improvement_run_spec.py
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
6. baseline direct-card success plus wrong path/digest/weights failure;
   Task8B derived-card and same-fold BaselineReplayReceipt success plus every
   path/digest/model/lane/fold/candidate/FeatureSeal/replay mismatch failure;
7. each four-file crash point, safe matching orphan completion, orphan
   non-loadability, missing sidecar rejection, and immutable byte conflict;
8. AST/module-import guards proving Task8 imports no outcome provider, metric
   evaluator, labelled-ledger API, or legacy top-level pipeline; and
9. package exports of `PredictionSeal`, `seal_oof_predictions`,
   `load_prediction_seal`, and `load_prediction_rows` only for Task8.

Run focused Task8, feature-seal, contracts, RunSpec, source-gate,
scoring-card, baseline-replay, and canonical suites after each stage; run Ruff
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
    prediction_seal: PredictionSeal,
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
    candidate: PredictionSeal,
    baseline: PredictionSeal,
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
baseline `baseline/v5.1/{fold}` PredictionSeal and its Common FeatureSeal.
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
persistence/API. The new required `feature_seal_sha256` is in the
PredictionSeal payload, schema, sealer, descriptor loader, and row loader.
The staged Task8A/Task6A/Task8B/Task6B order gives every nonbaseline card a
prior immutable baseline receipt and leaves outcome authority exclusively to
Task6B. The only necessary scope correction is Common OOF FeatureSeal
admission for O19--O23, which is required to authenticate the Task7 OOF
scoring-card population. Direct feature-seal equality is restricted to
Common A/A+B versus the baseline replay; A+B+C is admitted only through its
securely reloaded Common parent relation. Task6B persists and reloads one
baseline/Common OutcomeReceipt per fold through both reports and their final
pair, so no frozen-card consumer can treat `outcomes_sha256` as provenance on
its own. No JoinedLedger, outcome join, raw mapping, or label-bearing Task8
interface remains in scope.
