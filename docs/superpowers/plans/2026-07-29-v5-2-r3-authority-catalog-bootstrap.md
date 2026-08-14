# v5.2 R3 Authority Catalog Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the synthetic r3 disposition-catalog trust boundary with a real tracked seven-query authority bootstrap and a canonical immutable four-record/three-projection catalog while preserving r2 byte-for-byte.

**Architecture:** Create the minimum canonical r3 RunSpec, acquisition contract, protocol branch, query-plan branch, and loading schemas before constructing the catalog. The only public catalog factory loads those artifacts, builds and validates the complete production static batch, authenticates protected parent bytes, and stores nested values as canonical immutable bytes. A small read-only projection capability gives Task 2 and Task 3 one stable interface for r2 and r3.

**Tech Stack:** Python 3.11, `unittest`, JSON Schema Draft 2020-12, canonical JSON, SHA-256, Git-bound source authority, Ruff.

---

## Execution Context and Preservation Rules

All commands run in:

```text
/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd
```

Use:

```bash
export MLB_V52_SOURCE_AUTHORITY_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest'
export PYTHONDONTWRITEBYTECODE=1
export PY311='/opt/homebrew/opt/python@3.11/bin/python3.11'
```

Never write to the protected authority root. Do not change r2 RunSpec, contract, query table, query IDs, batch ID, audit, data, reports, config, checkpoints, or evidence. The diagnostic exact run that started at `cdde3c5` is not final verification because the worktree HEAD changed during execution.

### Task 1: Bootstrap the real r3 RunSpec, contract, and seven-query static authority

**Files:**

- Create: `protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json`
- Create: `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json`
- Modify: `schemas/v5_2_run_spec.schema.json`
- Modify: `schemas/v5_2_acquisition_contract.schema.json`
- Modify: `mlb_backtest/protocols.py`
- Modify: `mlb_backtest/v52_query_plan.py`
- Test: `tests/test_protocols.py`
- Test: `tests/test_v52_recovery_r2_schema_mapping.py`
- Test: `tests/test_verify_v52_design.py`
- Test: `tests/test_v52_missing_venue_recovery_disposition.py`

- [ ] **Step 1: Record the clean baseline**

Run:

```bash
git status --short
git rev-parse HEAD
git diff --name-only 60df962..HEAD
```

Expected: clean worktree and no implementation commit after the approved design commit unless it is explicitly recorded before continuing.

- [ ] **Step 2: Write RED tests for the tracked r3 identity**

Add tests that require both canonical files to exist and pin:

```python
R3_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
R3_RUN_SPEC = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
)
R3_CONTRACT = Path(
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
)
R3_YEARS = tuple(range(2018, 2025))
R3_RECORD_GAME_PKS = (565534, 630882, 633224, 632924)
R3_RECORD_TO_PROJECTION_INDEXES = (0, 1, 2, 2)
R3_EXCLUDED_GAME_PKS = (632924,)
```

The tests must assert:

```python
self.assertEqual(run_spec["run_id"], R3_RUN_ID)
self.assertEqual(tuple(run_spec["required_source_years"]), R3_YEARS)
self.assertEqual(
    run_spec["parser_policy"]["recovery_schedule_parser_version"],
    "v5.2.3",
)
self.assertEqual(
    run_spec["parser_policy"]["missing_venue_disposition_policy_version"],
    "v5.2.3",
)
self.assertEqual(contract["run_id"], R3_RUN_ID)
self.assertEqual(contract["required_years"], list(R3_YEARS))
self.assertEqual(
    contract["recovery_disposition"]["record_game_pks"],
    list(R3_RECORD_GAME_PKS),
)
self.assertEqual(
    contract["recovery_disposition"]["record_to_projection_indexes"],
    list(R3_RECORD_TO_PROJECTION_INDEXES),
)
self.assertEqual(
    contract["recovery_disposition"]["excluded_game_pks"],
    list(R3_EXCLUDED_GAME_PKS),
)
```

Also assert canonical files:

```python
self.assertEqual(
    path.read_bytes(),
    canonical_json_bytes(json.loads(path.read_bytes())) + b"\n",
)
```

- [ ] **Step 3: Write RED tests for the production seven-query batch**

Call the real loaders and `build_static_batch()` with the tracked r3 paths. Assert:

```python
self.assertEqual(batch.stage, "STATIC_SCHEDULES")
self.assertIsNone(batch.derived_from_sha256)
self.assertEqual(len(batch.queries), 7)
self.assertEqual(
    tuple(query.requested_range["year"] for query in batch.queries),
    tuple(range(2018, 2025)),
)
```

Pin the three disposition endpoints:

```python
self.assertEqual(
    tuple(
        query.endpoint
        for query in batch.queries
        if query.endpoint.startswith("disposition://")
    ),
    (
        "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
        "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
        "disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924",
    ),
)
```

Assert that no query uses:

```text
disposition://MISSING_VENUE_632924
```

and that all seven queries carry the SHA-256 of the tracked r3 RunSpec and contract.

- [ ] **Step 4: Run RED**

Run:

```bash
"$PY311" -m unittest -v \
  tests.test_protocols \
  tests.test_v52_recovery_r2_schema_mapping \
  tests.test_verify_v52_design \
  tests.test_v52_missing_venue_recovery_disposition
```

Expected: r3 tests fail because the canonical r3 artifacts and production authority branch do not exist. Existing r2 assertions remain green.

- [ ] **Step 5: Create the canonical r3 RunSpec and contract**

Use the existing r2 artifacts as structural references, but create new canonical values with:

```text
run_id = historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3
audit root = audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3
raw root = data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/raw
sources root = data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/sources
reports root = reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3
required_source_years = [2018, 2019, 2020, 2021, 2022, 2023, 2024]
existing source parser = v5.2.1
recovery schedule parser = v5.2.3
missing_venue_disposition_policy_version = v5.2.3
record gamePks = [565534, 630882, 633224, 632924]
record-to-projection indexes = [0, 1, 2, 2]
excluded gamePks = [632924]
authority_scope = SOURCE_ONLY_THROUGH_SOURCES_READY
downstream_authority = all eight existing flags false
```

Serialize as:

```python
canonical_json_bytes(payload) + b"\n"
```

Do not copy an r2 query ID, sidecar, checkpoint, or runtime binding into the r3 artifacts.

The r3 RunSpec `parser_policy` is exactly:

```json
{
  "existing_source_parser_version": "v5.2.1",
  "recovery_schedule_parser_version": "v5.2.3",
  "schedule_duplicate_dispositions": [
    "SCHEDULE_DUPLICATE_GAMEPK_565534",
    "SCHEDULE_DUPLICATE_GAMEPK_630882",
    "SCHEDULE_DUPLICATE_GAMEPK_633224"
  ],
  "missing_venue_disposition_policy_version": "v5.2.3"
}
```

The r3-only acquisition-contract object is:

```json
{
  "parser_version": "v5.2.3",
  "policy_version": "v5.2.3",
  "record_game_pks": [565534, 630882, 633224, 632924],
  "projection_endpoints": [
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
    "disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924"
  ],
  "record_to_projection_indexes": [0, 1, 2, 2],
  "excluded_game_pks": [632924]
}
```

Store it under the exact key `recovery_disposition`. Keep `recovery_provenance` as a separate four-parent authenticated object built from the three existing conflict parent specifications plus the 632924 missing-venue parent specification. The new r3 contract validator requires both objects, the r3 canonical roots, and the same closed non-recovery fields used by r2. Its exact top-level key set is the r2 key set plus `recovery_disposition`; it does not add a second top-level `excluded_game_pks`.

Bind `approved_design`, `implementation_plan`, and `run_spec` using the existing recovery Git-blob/hash format. The approved design is `docs/superpowers/specs/2026-07-29-v5-2-r3-authority-catalog-bootstrap-design.md`; the implementation plan is this committed file.

- [ ] **Step 6: Add exact schema and protocol branches**

Add exact closed `oneOf` branches for the new run ID in only:

```text
schemas/v5_2_run_spec.schema.json
schemas/v5_2_acquisition_contract.schema.json
```

Require the exact r3 paths, `authority_scope`, all-false `downstream_authority`, parser-policy version pair, seven source years, four record gamePks, three projection indexes with `(0, 1, 2, 2)`, and excluded set `[632924]`. Preserve every r2 branch byte except canonical one-line schema regeneration required by the repository.

In `mlb_backtest/protocols.py`, add one exact r3 identity branch. Do not widen an existing r2 condition to accept arbitrary recovery runs.

- [ ] **Step 7: Add the exact r3 query-plan branch**

In `mlb_backtest/v52_query_plan.py`, add an r3-only authority entry and static table. Do not use the current boolean `recovery` flag as the complete authority identity. Extend `_AuthorityBranch` with an exact recovery variant (`None`, `"r2"`, or `"r3"`) while preserving existing `branch.recovery` behavior for callers.

Dispatch exact validators as follows:

```text
schema index 2 -> existing r2 closed recovery schema and contract validator
schema index 3 -> new r3 closed recovery schema and contract validator
recovery variant r2 -> existing v5.2.2 static query validation
recovery variant r3 -> new v5.2.3 static query validation
```

Update `_verify_frozen_acquisition_schema()` so schema indexes 2 and 3 both use closed recovery-property validation. Index 3 must not fall through to the top-level `const` branch. Split `_validate_recovery_acquisition_contract()` into exact r2 and r3 validators selected by authenticated branch/run ID; do not widen the r2 validator.

Build seven ordered queries:

```text
2018: ordinary MLB schedule endpoint
2019: disposition://SCHEDULE_DUPLICATE_GAMEPK_565534
2020: disposition://SCHEDULE_DUPLICATE_GAMEPK_630882
2021: disposition://SCHEDULE_RECOVERY_2021_633224_AND_MISSING_VENUE_632924
2022: ordinary MLB schedule endpoint
2023: ordinary MLB schedule endpoint
2024: ordinary MLB schedule endpoint
```

Every `LogicalQuery` must bind:

```python
R3_STATIC_QUERY_TABLE_BY_YEAR = {
    2018: _SCHEDULE_ENDPOINT,
    2019: "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
    2020: "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
    2021: (
        "disposition://SCHEDULE_RECOVERY_2021_"
        "633224_AND_MISSING_VENUE_632924"
    ),
    2022: _SCHEDULE_ENDPOINT,
    2023: _SCHEDULE_ENDPOINT,
    2024: _SCHEDULE_ENDPOINT,
}
run_spec_bytes = run_spec_path.read_bytes()
contract_bytes = acquisition_contract_path.read_bytes()
records = []
for year in range(2018, 2025):
    first, last = SEASON_BOUNDARIES[year]
    endpoint = R3_STATIC_QUERY_TABLE_BY_YEAR[year]
    parameters = (
        {}
        if endpoint.startswith("disposition://")
        else {
            "sportId": 1,
            "gameType": "R",
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "hydrate": "probablePitcher,lineups,venue,linescore",
        }
    )
    records.append(
        {
            "source": "mlb_schedule_game_feed",
            "grain": "season_schedule_and_game",
            "endpoint": endpoint,
            "parameters": parameters,
            "requested_range": {
                "year": year,
                "start_date": first.isoformat(),
                "end_date": last.isoformat(),
            },
            "expected_media_type": "application/json",
            "parser_version": "v5.2.3",
            "parent_query_id": None,
            "run_spec_sha256": hashlib.sha256(run_spec_bytes).hexdigest(),
            "acquisition_contract_sha256": hashlib.sha256(
                contract_bytes
            ).hexdigest(),
        }
    )
```

Compute and pin the resulting seven logical query IDs and batch ID through canonical builders. `LogicalQuery` has no stored query-ID field; mutation tests change canonical fields and then compare `logical_query_id(query)`. The r2 table and constants must not change.

Add `_validate_r3_static_schedule_queries()` and dispatch to it when the seven-query stage carries parser `v5.2.3`, before the existing `v5.2.2` r2 dispatch. The generic `v5.2.1` validator must never receive r3 queries. Add an r3 branch in `build_static_batch()` selected by exact authenticated `SourceRunSpec.run_id`, not parser text alone.

Complete the same exact variant dispatch in every query-plan batch-authority boundary that is currently r2-only:

```text
_recovery_context_if_present()
_validate_recovery_marked_batch_authority()
_validate_recovery_parser_batch()
authority-to-batch-root selection
```

For r3, these paths require the exact r3 RunSpec/contract pair, parser `v5.2.3`, r3 endpoint set, and r3 audit/raw namespace. For r2, retain the existing path pair, parser `v5.2.2`, endpoint set, and namespaces. A parser version or endpoint alone must never select an authority branch.

- [ ] **Step 8: Run GREEN and negative schema tests**

Run Step 4 again. Add mutations for:

```text
wrong run ID
wrong r3 path/root
missing or extra season
parser/policy version mismatch
three records instead of four
four projection queries instead of three
record-to-projection indexes other than [0,1,2,2]
excluded set other than [632924]
standalone MISSING_VENUE_632924 projection endpoint
r2 identity supplied at r3 paths
r3 identity supplied at r2 paths
```

Expected: all positive tests pass and every mutation fails closed.

- [ ] **Step 9: Verify and commit Task 1**

Run:

```bash
"$PY311" -m json.tool \
  protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json \
  >/dev/null
"$PY311" -m json.tool \
  protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json \
  >/dev/null
ruff check \
  mlb_backtest/protocols.py mlb_backtest/v52_query_plan.py \
  tests/test_protocols.py tests/test_v52_recovery_r2_schema_mapping.py \
  tests/test_verify_v52_design.py tests/test_v52_missing_venue_recovery_disposition.py
"$PY311" -m py_compile mlb_backtest/protocols.py mlb_backtest/v52_query_plan.py
git diff --check
```

Commit only the Task 1 files:

```bash
git add \
  protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json \
  protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json \
  schemas/v5_2_run_spec.schema.json \
  schemas/v5_2_acquisition_contract.schema.json \
  mlb_backtest/protocols.py \
  mlb_backtest/v52_query_plan.py \
  tests/test_protocols.py \
  tests/test_v52_recovery_r2_schema_mapping.py \
  tests/test_verify_v52_design.py \
  tests/test_v52_missing_venue_recovery_disposition.py
git commit -m "feat: bootstrap sealed r3 source authority"
```

### Task 2: Replace the injected catalog builder with the real public factory

**Files:**

- Modify: `mlb_backtest/v52_missing_venue_recovery_disposition.py`
- Modify: `mlb_backtest/v52_recovery_parent_evidence.py`
- Test: `tests/test_v52_missing_venue_recovery_disposition.py`
- Test: `tests/test_v52_recovery_r2_tdd.py`
- Test: `tests/test_v52_source_schedule.py`

- [ ] **Step 1: Write RED public-factory tests**

Delete tests that treat `_build_catalog_from_authenticated_context`, `SimpleNamespace`, or a three-query static batch as a valid construction path.

Call only:

```python
catalog = build_missing_venue_recovery_disposition_catalog(
    protected_root,
    repo / R3_RUN_SPEC,
    repo / R3_CONTRACT,
)
```

Assert:

```python
self.assertEqual(catalog.game_pks(), (565534, 630882, 633224, 632924))
self.assertEqual(len(catalog.projection_query_ids()), 3)
self.assertEqual(
    catalog.projection_query_for_game_pk(633224),
    catalog.projection_query_for_game_pk(632924),
)
self.assertEqual(catalog.excluded_game_pks(), frozenset({632924}))
self.assertEqual(catalog.record_to_projection_indexes(), (0, 1, 2, 2))
```

The current implementation must fail RED because it still relies on injected context/static/parent objects or mutable nested values.

- [ ] **Step 2: Write RED full-query identity mutations**

Expose a pure validator used by the public factory:

```python
_validate_r3_static_batch(
    batch: QueryBatch,
    expected_run_spec_sha256: str,
    expected_contract_sha256: str,
) -> tuple[LogicalQuery, LogicalQuery, LogicalQuery]
```

For each of the seven queries, independently mutate:

```text
source
grain
endpoint
parameters
requested_range
expected_media_type
parser_version
parent_query_id
run_spec_sha256
acquisition_contract_sha256
canonical-field mutation and resulting `logical_query_id(query)`/order
```

Also mutate batch stage, batch ID, derived parent, count, order, duplicate query, and insert a standalone 632924 projection. Every case must raise an exact `ValueError`.

- [ ] **Step 3: Write RED immutable-alias tests**

Build semantically valid mutable input dictionaries, construct a record/catalog value, and mutate the original inputs:

```python
semantics["forged"] = True
parameters["sportId"] = 99
requested_range["year"] = 1900
```

Assert that catalog canonical bytes, query IDs, record semantics, and projection mapping remain unchanged.

Direct construction with canonical bytes that differ in any field must fail. Direct construction with exact canonical bytes may succeed because integrity depends on exact value equality, not a hidden token.

- [ ] **Step 4: Run RED**

Run:

```bash
"$PY311" -m unittest -v \
  tests.test_v52_missing_venue_recovery_disposition \
  tests.test_v52_recovery_r2_tdd \
  tests.test_v52_source_schedule.RecoveryDispositionCatalogTests
```

Expected: public-factory and immutable-alias tests fail for the current implementation. Existing r2 tests remain green.

- [ ] **Step 5: Implement canonical immutable records**

Store nested data as immutable canonical bytes:

```python
@dataclass(frozen=True)
class MissingVenueDispositionRecord:
    game_pk: int
    projection_index: int
    _query_canonical_bytes: bytes
    _semantics_canonical_bytes: bytes

    @property
    def query(self) -> LogicalQuery:
        value = json.loads(self._query_canonical_bytes)
        if not isinstance(value, dict):
            raise ValueError("r3 canonical query must be an object")
        return LogicalQuery(**value)

    @property
    def semantics(self) -> dict[str, object]:
        value = json.loads(self._semantics_canonical_bytes)
        if not isinstance(value, dict):
            raise ValueError("r3 canonical semantics must be an object")
        return value
```

Catalog storage owns immutable bytes and properties return fresh values. Existing `record.query` and `record.semantics` call sites therefore remain value accesses rather than bound methods.

In `__post_init__`, require:

```python
self.game_pk in {565534, 630882, 633224, 632924}
self.projection_index in {0, 1, 2}
self._query_canonical_bytes == canonical_query_bytes(self.query)
self._semantics_canonical_bytes == canonical_json_bytes(self.semantics)
```

Compare both byte strings and the `(game_pk, projection_index)` pair with the exact r3 sealed templates. The catalog constructor must require ordered scalar pairs `((565534, 0), (630882, 1), (633224, 2), (632924, 2))`.

- [ ] **Step 6: Implement the public factory**

The public factory must:

```python
context = _load_static_contracts(run_spec_path, acquisition_contract_path)
static_batch = build_static_batch(run_spec_path, acquisition_contract_path)
projection_queries = _validate_r3_static_batch(
    static_batch,
    context.authority_sha256,
    context.acquisition_contract_sha256,
)
conflict_parents = build_recovery_parent_catalog(root)
missing_parent = build_missing_venue_parent_catalog(root)
conflict_records = _build_r3_conflict_records(
    conflict_parents,
    projection_queries,
    context.authority_sha256,
    context.acquisition_contract_sha256,
)
missing_record = _build_authenticated_missing_venue_record(
    missing_parent,
    projection_queries[2],
)
return _catalog_from_authenticated_values(
    context=context,
    projection_queries=projection_queries,
    records=(*conflict_records, missing_record),
)
```

Do not retain a production helper that accepts caller-supplied `context`, `static`, and `parent_catalog` and returns a catalog without reauthentication.

Define `_build_r3_conflict_records()` in this task. It reads the three existing conflict parents through `build_recovery_parent_catalog(root)` and recomputes their record semantics under the r3 query bindings. It must not import an r2 sidecar, query ID, source binding, checkpoint, or serialized record. Define `_build_authenticated_missing_venue_record()` and `_catalog_from_authenticated_values()` as internal value constructors that still perform canonical equality validation.

Add the accessors required by the public-factory tests:

```python
def record_to_projection_indexes(self) -> tuple[int, ...]:
    indexes = tuple(record.projection_index for record in self.records)
    if indexes != (0, 1, 2, 2):
        raise ValueError("r3 record-to-projection mapping differs")
    return indexes

def projection_query_for_game_pk(self, game_pk: int) -> str:
    record_index = self.game_pks().index(game_pk)
    return self.projection_query_ids()[
        self.record_to_projection_indexes()[record_index]
    ]
```

- [ ] **Step 7: Authenticate the exact 632924 object**

Require:

```text
query ID = 91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2
registration SHA/bytes = b915233fb3c3ea796330486490f3b90d6746ce82e1f03b30b9c964ba6569cfd3 / 675
attempt SHA/bytes = b7d76d96191bc80c578965ec5242a3abeb44dade601a5ca3c76c30d4c45f1652 / 664
quarantine SHA/bytes = d3c9d9f197cab51d5ab509887002e4df3f300cb20c23a888120395e9b7042c36 / 938
raw SHA/bytes = 84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17 / 21223271
position = (130, 10)
canonical no-LF object SHA = 705969812dedbc3b7d1b3e4bbd6d877671b34657a96cc5b34b22d35d820261a4
```

The object must prove gamePk/date/time/calendar ID/teams/scores/winner flags and venue exactly:

```json
{"link":"/api/v1/venues/null"}
```

Reject the LF-added object digest `977c3526bc783ea3544b9424b4ed35fff5f08b12e4c627a5359a59b4fdf4f503`.

- [ ] **Step 8: Run GREEN and mutation tests**

Run Step 4. Add parent mutation cases for every path, SHA, byte count, position, object digest, game identity, score, winner, status, and venue field.

Expected: all Task 2 and r2 regression tests pass; every mutation fails closed.

- [ ] **Step 9: Verify and commit Task 2**

Run:

```bash
ruff check \
  mlb_backtest/v52_missing_venue_recovery_disposition.py \
  mlb_backtest/v52_recovery_parent_evidence.py \
  tests/test_v52_missing_venue_recovery_disposition.py \
  tests/test_v52_recovery_r2_tdd.py
"$PY311" -m py_compile \
  mlb_backtest/v52_missing_venue_recovery_disposition.py \
  mlb_backtest/v52_recovery_parent_evidence.py
git diff --check
```

Commit:

```bash
git add \
  mlb_backtest/v52_missing_venue_recovery_disposition.py \
  mlb_backtest/v52_recovery_parent_evidence.py \
  tests/test_v52_missing_venue_recovery_disposition.py \
  tests/test_v52_recovery_r2_tdd.py \
  tests/test_v52_source_schedule.py
git commit -m "fix: construct r3 catalog from sealed authority"
```

### Task 3: Add the shared read-only r2/r3 projection capability

**Files:**

- Modify: `mlb_backtest/v52_schedule_conflict_recovery_disposition.py`
- Modify: `mlb_backtest/v52_missing_venue_recovery_disposition.py`
- Test: `tests/test_v52_recovery_r2_tdd.py`
- Test: `tests/test_v52_missing_venue_recovery_disposition.py`
- Test: `tests/test_v52_source_schedule.py`

- [ ] **Step 1: Write RED adapter tests**

Require:

```python
r2_projection = r2_catalog.as_projection()
self.assertEqual(r2_projection.record_game_pks, (565534, 630882, 633224))
self.assertEqual(r2_projection.record_to_projection_indexes, (0, 1, 2))
self.assertEqual(r2_projection.excluded_game_pks, frozenset())

r3_projection = r3_catalog.as_projection()
self.assertEqual(
    r3_projection.record_game_pks,
    (565534, 630882, 633224, 632924),
)
self.assertEqual(
    r3_projection.record_to_projection_indexes,
    (0, 1, 2, 2),
)
self.assertEqual(r3_projection.excluded_game_pks, frozenset({632924}))
self.assertEqual(
    r3_projection.projection_query_id_for_game_pk(633224),
    r3_projection.projection_query_id_for_game_pk(632924),
)
```

Mutation tests must reject mismatched lengths, out-of-range indexes, duplicate projection IDs, unordered gamePks, and exclusions not represented by a record.

- [ ] **Step 2: Run RED**

Run:

```bash
"$PY311" -m unittest -v \
  tests.test_v52_recovery_r2_tdd \
  tests.test_v52_missing_venue_recovery_disposition \
  tests.test_v52_source_schedule.RecoveryDispositionCatalogTests
```

Expected: adapter tests fail because the common capability does not exist.

- [ ] **Step 3: Implement the immutable capability**

Add:

```python
@dataclass(frozen=True)
class RecoveryDispositionProjection:
    record_game_pks: tuple[int, ...]
    projection_query_ids: tuple[str, ...]
    record_to_projection_indexes: tuple[int, ...]
    excluded_game_pks: frozenset[int]

    def projection_query_id_for_game_pk(self, game_pk: int) -> str:
        record_index = self.record_game_pks.index(game_pk)
        projection_index = self.record_to_projection_indexes[record_index]
        return self.projection_query_ids[projection_index]
```

Its constructor validates exact tuple lengths, unique query IDs, valid indexes, unique ordered gamePks, and `excluded_game_pks <= set(record_game_pks)`.

Add adapters:

```python
# r2
RecoveryDispositionProjection(
    record_game_pks=(565534, 630882, 633224),
    projection_query_ids=r2_catalog.query_ids(),
    record_to_projection_indexes=(0, 1, 2),
    excluded_game_pks=frozenset(),
)

# r3
RecoveryDispositionProjection(
    record_game_pks=(565534, 630882, 633224, 632924),
    projection_query_ids=r3_catalog.projection_query_ids(),
    record_to_projection_indexes=(0, 1, 2, 2),
    excluded_game_pks=frozenset({632924}),
)
```

Do not change any existing r2 method or serialized value.

- [ ] **Step 4: Run GREEN**

Run Step 2. Also run:

```bash
"$PY311" -m unittest -v \
  tests.test_v52_source_schedule \
  tests.test_v52_recovery_r2_tdd \
  tests.test_v52_missing_venue_recovery_disposition
```

Expected: all tests pass with unchanged r2 query IDs and the shared r3 2021 mapping.

- [ ] **Step 5: Verify and commit Task 3**

Run:

```bash
ruff check \
  mlb_backtest/v52_schedule_conflict_recovery_disposition.py \
  mlb_backtest/v52_missing_venue_recovery_disposition.py \
  tests/test_v52_recovery_r2_tdd.py \
  tests/test_v52_missing_venue_recovery_disposition.py \
  tests/test_v52_source_schedule.py
"$PY311" -m py_compile \
  mlb_backtest/v52_schedule_conflict_recovery_disposition.py \
  mlb_backtest/v52_missing_venue_recovery_disposition.py
git diff --check
```

Commit:

```bash
git add \
  mlb_backtest/v52_schedule_conflict_recovery_disposition.py \
  mlb_backtest/v52_missing_venue_recovery_disposition.py \
  tests/test_v52_recovery_r2_tdd.py \
  tests/test_v52_missing_venue_recovery_disposition.py \
  tests/test_v52_source_schedule.py
git commit -m "feat: expose recovery disposition projection capability"
```

### Task 4: Review, preservation verification, and handoff to the original r3 Task 2

**Files:**

- Verify: all files changed by Tasks 1–3
- Modify only if required by review: files owned by the task that introduced the issue
- Reference: `docs/superpowers/plans/2026-07-29-v5-2-r3-missing-venue-632924.md`

- [ ] **Step 1: Run the focused aggregate suite**

Run:

```bash
"$PY311" -m unittest -v \
  tests.test_protocols \
  tests.test_v52_recovery_r2_schema_mapping \
  tests.test_verify_v52_design \
  tests.test_v52_missing_venue_recovery_disposition \
  tests.test_v52_recovery_r2_tdd \
  tests.test_v52_source_schedule
```

Expected: zero failures and zero errors. Record exact pass and skip counts.

- [ ] **Step 2: Verify canonical artifacts and source quality**

Run:

```bash
"$PY311" - <<'PY'
import json
from pathlib import Path
from mlb_backtest.diagnostics import canonical_json_bytes

paths = (
    Path("protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"),
    Path("protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"),
)
for path in paths:
    raw = path.read_bytes()
    assert raw == canonical_json_bytes(json.loads(raw)) + b"\n", path
print("canonical artifacts: 2")
PY

ruff check \
  mlb_backtest/protocols.py \
  mlb_backtest/v52_query_plan.py \
  mlb_backtest/v52_recovery_parent_evidence.py \
  mlb_backtest/v52_schedule_conflict_recovery_disposition.py \
  mlb_backtest/v52_missing_venue_recovery_disposition.py
git diff --check
```

- [ ] **Step 3: Verify the preservation boundary**

Confirm no changed path appears under:

```text
audit/
data/
reports/
config/
```

Confirm the protected root is clean and unchanged:

```bash
git -C "$MLB_V52_SOURCE_AUTHORITY_ROOT" status --short
git diff --name-only 60df962..HEAD -- \
  audit data reports config
```

Re-run the existing r2 hash/inventory preservation checks used by Task 13 and require equality for:

```text
16 v5.1 required files
3 old-evidence files
8 recovery-parent bound files
4 parent-tree fingerprints
2 preflight hashes
```

- [ ] **Step 4: Run spec-compliance review**

Give a fresh reviewer:

```text
spec: docs/superpowers/specs/2026-07-29-v5-2-r3-authority-catalog-bootstrap-design.md
base: 60df962
head: current reviewed HEAD
```

Require no P0/P1/P2. If issues exist, return to the owning implementer, add a RED test, apply one root-cause fix, rerun focused verification, and request re-review.

- [ ] **Step 5: Run code-quality review**

After spec approval, give a different fresh reviewer the same base/head and require no P0/P1/P2 for:

```text
authority trust boundary
complete seven-query canonical identity
immutable alias safety
r2 compatibility
test seam honesty
protected-evidence preservation
```

- [ ] **Step 6: Update execution sequencing**

After both reviews approve, continue at original plan:

```text
docs/superpowers/plans/2026-07-29-v5-2-r3-missing-venue-632924.md
Task 2: Bind exclusion before schedule, anchor, and venue derivation
```

Treat the RunSpec, contract, protocol branch, query-plan authority branch, and RunSpec/contract schema work completed here as removed from the original Task 4 scope. Do not duplicate or regenerate their identities.

- [ ] **Step 7: Final exact-suite rule**

Do not use the existing `task13-exact-cdde3c5-py311` run as final evidence. After all r3 Task 1–4 implementation and reviews are complete, create a clean detached worktree at the reviewed HEAD and run exactly one isolated:

```bash
"$PY311" scripts/run_task13_exact_suite.py \
  --repo "$CLEAN_REVIEWED_WORKTREE" \
  --v51-input-root "$MLB_V52_SOURCE_AUTHORITY_ROOT" \
  --old-v52-evidence-root "$MLB_V52_SOURCE_AUTHORITY_ROOT"
```

Require exit 0, zero failures/errors, expected skips only, and preservation-manifest equality before executing the single r3 recovery run.

Before the exact command, create the clean detached worktree and bind the variable:

```bash
CLEAN_REVIEWED_WORKTREE="$(mktemp -d /private/tmp/mlb-r3-reviewed.XXXXXX)"
rmdir "$CLEAN_REVIEWED_WORKTREE"
git worktree add --detach "$CLEAN_REVIEWED_WORKTREE" "$(git rev-parse HEAD)"
test -z "$(git -C "$CLEAN_REVIEWED_WORKTREE" status --short)"
```
