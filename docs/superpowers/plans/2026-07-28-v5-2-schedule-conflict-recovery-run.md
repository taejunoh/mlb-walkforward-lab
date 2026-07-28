# Schedule-Conflict Recovery Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved isolated recovery source authority for the three schedule conflicts and stop at `SOURCES_READY`.

**Architecture:** The historical and prior superseding authorities stay closed and immutable. A third exact authority branch owns a recovery-only three-entry disposition catalog; the catalog is authenticated from fixed parent evidence, then propagates through checkpoints, normalized snapshots, inventory, replay, and finalization. Parser version `v5.2.2` is valid only for the recovery static schedule roots and dispositions.

**Tech Stack:** Python 3, `unittest`, JSON Schema Draft 2020-12, canonical JSON, SHA-256, Git blob bindings.

---

## File map and non-negotiable boundaries

| Responsibility | Files |
|---|---|
| Immutable parents | Read only: `data/v5_2_qd/historical/**`, `audit/v5_2_qd/historical/**`, `data/v5_2_qd/historical_superseding_565534/**`, `audit/v5_2_qd/historical_superseding_565534/**` |
| Third authority | `protocols.py`, `v52_query_plan.py`, `v52_acquire.py`, `v52_source_finalize.py` |
| Catalog | Create `v52_schedule_conflict_recovery_disposition.py`; retain `v52_duplicate_gamepk_disposition.py` unchanged |
| Evidence/parser | `v52_evidence.py`, `v52_source_contracts.py`, `source_parsers/schedule.py`, `v52_sources/schedule.py` |
| Source lifecycle | `execution_registry.py`, `v52_schedule_inventory.py`, `v52_game_evidence_inventory.py`, `v52_verify_sources.py`, `v52_snapshot_store.py`, `v52_source_orchestration.py` |
| Availability consumers | `v52_sources/history.py`, `prepare.py`, `feature_engine.py` |
| Schemas | RunSpec, acquisition contract, derived sidecar, query sidecar, acquisition checkpoint, schedule anchor, game evidence, normalized snapshot, source inventory, review, replay, source receipt commit, execution receipt |
| Immutable legacy code | Do not change `v52_legacy.py` or `v52_legacy_relation.py` |

`tests/v52_recovery_schedule_conflict_fixture.py` owns every test helper used
below: `recovery_contract() -> dict[str, object]`,
`recovery_catalog() -> RecoveryDispositionCatalog`,
`recovery_game(game_pk: int) -> Mapping[str, object]`,
`recovery_history_for(game_pk: int) -> ScheduleHistory`,
`visible_game_pks(history: ScheduleHistory, target: date) -> set[int]`,
`schedule_counts(year: int) -> tuple[int, int, int, int, int]`,
`recovery_static_checkpoint() -> AcquisitionCheckpoint` (the post-publication
`STATIC_SCHEDULES` checkpoint with seven completed IDs, no pending/quarantined/
split IDs, three sorted `dispositioned_query_ids`, and the three full binding
objects),
`recovery_authority_bindings() -> AuthorityBindings`, and
`replay_receipt(execution_id: str) -> Path`, and
`advance_recovery_with_replays(receipts: Sequence[Path]) -> None`. The
fixture wrapper calls the production finalization function with a complete
`FinalizationContext`; it does not reimplement validation. The fixture exports `ROOT`,
`RECOVERY_RUN_SPEC`, `RECOVERY_CONTRACT`, `RECOVERY_CONTRACT_FIXTURE`,
`RECOVERY_RUN_SPEC_OBJECT`,
`RECOVERY_RAW_ROOT`, and `RECOVERY_STATIC_BATCH_ID`.

### Task 1: Characterize and freeze parent evidence inputs

**Files:**
- Create: `mlb_backtest/v52_recovery_parent_evidence.py`
- Create: `tests/v52_recovery_schedule_conflict_fixture.py`
- Modify: `tests/test_v52_evidence.py`

- [ ] **Step 1: Add a real TestCase RED test**

```python
class RecoveryParentEvidenceTests(unittest.TestCase):
    def test_catalog_rejects_parent_path_supplied_by_caller(self) -> None:
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'parent_paths'"):
            build_recovery_parent_catalog(ROOT, parent_paths={})
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_evidence.RecoveryParentEvidenceTests -v`

Expected: FAIL because `build_recovery_parent_catalog` does not exist.

- [ ] **Step 3: Implement the production closed catalog**

```python
PARENT_RELATIVES = MappingProxyType({
    "original_2019_565534": (OLD_REGISTRATION_RELATIVE, OLD_QUARANTINE_RELATIVE, OLD_RAW_RELATIVE),
    "superseding_2020_630882": (RECOVERY_2020_REGISTRATION_RELATIVE, RECOVERY_2020_ATTEMPT_RELATIVE, RECOVERY_2020_QUARANTINE_RELATIVE, RECOVERY_2020_RAW_RELATIVE),
    "superseding_2021_633224": (RECOVERY_2021_REGISTRATION_RELATIVE, RECOVERY_2021_ATTEMPT_RELATIVE, RECOVERY_2021_QUARANTINE_RELATIVE, RECOVERY_2021_RAW_RELATIVE),
})

def build_recovery_parent_catalog(root: Path) -> RecoveryParentCatalog:
    return RecoveryParentCatalog(tuple(_authenticate_fixed_parent(root, name) for name in PARENT_RELATIVES))
```

The function accepts only `root`. Add `capture_tree_fingerprint(root, relative)` and
assert its canonical file-count/path/SHA-256 digest before and after each catalog
test for both `data/v5_2_qd/historical` plus `audit/v5_2_qd/historical` and
`data/v5_2_qd/historical_superseding_565534` plus
`audit/v5_2_qd/historical_superseding_565534`. Each fixed reader uses `lstat`,
`O_NOFOLLOW`, single-link, device/inode, exact byte count, canonical JSON, and
SHA-256 validation.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_evidence.RecoveryParentEvidenceTests -v`

Expected: PASS; changing one fixed parent byte or replacing it with a symlink fails.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_recovery_parent_evidence.py tests/v52_recovery_schedule_conflict_fixture.py tests/test_v52_evidence.py
git commit -m "test: characterize recovery parent evidence"
```

### Task 2: Add the third authority and all closed schema families

**Files:**
- Declare paths only; create in Task 11 closure sequence: `protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json`, `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json`
- Modify: `mlb_backtest/protocols.py:635,1007,1035,6065,6573,6624`
- Modify: `mlb_backtest/v52_query_plan.py:163,1121`
- Modify: `schemas/v5_2_run_spec.schema.json`, `schemas/v5_2_acquisition_contract.schema.json`, `schemas/v5_2_derived_disposition_sidecar.schema.json`, `schemas/v5_2_query_sidecar.schema.json`, `schemas/v5_2_acquisition_checkpoint.schema.json`, `schemas/v5_2_schedule_anchor_inventory.schema.json`, `schemas/v5_2_game_evidence_inventory.schema.json`, `schemas/v5_2_normalized_snapshot.schema.json`, `schemas/v5_2_source_inventory.schema.json`, `schemas/v5_2_source_review_receipt.schema.json`, `schemas/v5_2_clean_source_replay_receipt.schema.json`, `schemas/v5_2_source_receipt_commit.schema.json`, `schemas/v5_2_execution_receipt.schema.json`, `schemas/v5_2_timezone_candidate.schema.json`, `schemas/v5_2_timezone_review_receipt.schema.json`, `schemas/v5_2_timezone_seal_receipt.schema.json`, `schemas/v5_2_venue_timezones.schema.json`
- Modify: `tests/test_protocols.py`, `tests/test_v52_acquisition_contract.py`, `tests/test_v52_query_plan.py`

- [ ] **Step 1: Add a TestCase RED test**

```python
class RecoveryAuthorityTests(unittest.TestCase):
    def test_contract_requires_three_named_parent_objects(self) -> None:
        contract = recovery_contract()
        self.assertEqual(tuple(contract["recovery_provenance"]), (
            "original_2019_565534", "superseding_2020_630882", "superseding_2021_633224"))
        broken = RECOVERY_CONTRACT_FIXTURE.with_name("broken-contract.json")
        broken.write_bytes(canonical_json_bytes({**contract, "recovery_provenance": {}}) + b"\n")
        with self.assertRaisesRegex(ValueError, "recovery provenance differs"):
            _load_static_contracts(RECOVERY_RUN_SPEC, broken)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_protocols tests.test_v52_acquisition_contract tests.test_v52_query_plan -v`

Expected: FAIL because schemas contain only the historical and superseding branches.

- [ ] **Step 3: Add exact branch constants and schema branches**

```python
_RECOVERY_RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json")
_RECOVERY_CONTRACT_RELATIVE = Path("protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json")
_RECOVERY_DESIGN_RELATIVE = Path("docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-run-design.md")
_RECOVERY_PLAN_RELATIVE = Path("docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-run.md")
```

Append one `_AuthorityBranch`. Add closed recovery `oneOf` branches: RunSpec 2 to 3, acquisition contract 2 to 3, derived sidecar 2 to 5, checkpoint 2 to 3, anchor 2 to 3, game evidence 2 to 3, source inventory 2 to 3, review 2 to 3, replay 2 to 3, receipt commit 2 to 3, and execution receipt recovery branch. Add recovery family branches to normalized snapshot. Keep every old branch byte-identical.

Task 2 deliberately does not write or commit either recovery protocol artifact:
their canonical bytes depend on the committed design/plan/RunSpec blob sequence
and are created and committed only in Task 11. Task 2 tests use the fixture
payloads named in the file map, never an uncommitted production contract.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_protocols tests.test_v52_acquisition_contract tests.test_v52_query_plan -v`

Expected: PASS; mismatched paths, prefixes, absent parent attempt, and unknown properties fail.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/protocols.py mlb_backtest/v52_query_plan.py schemas tests/test_protocols.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py
git commit -m "feat: add recovery source authority branch"
```

### Task 3: Implement the closed three-entry disposition catalog

**Files:**
- Create: `mlb_backtest/v52_schedule_conflict_recovery_disposition.py`
- Modify: `tests/test_v52_source_schedule.py`

- [ ] **Step 1: Add TestCase RED tests**

```python
class RecoveryDispositionCatalogTests(unittest.TestCase):
    def test_catalog_has_exactly_three_entries(self) -> None:
        catalog = build_recovery_schedule_conflict_catalog(ROOT, RECOVERY_RUN_SPEC, RECOVERY_CONTRACT)
        self.assertEqual(tuple(entry.game_pk for entry in catalog.entries), (565534, 630882, 633224))
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule.RecoveryDispositionCatalogTests -v`

Expected: FAIL because the recovery catalog is absent.

- [ ] **Step 3: Implement exact catalog types**

```python
RECOVERY_DISPOSITION_ENDPOINTS = (
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534",
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_630882",
    "disposition://SCHEDULE_DUPLICATE_GAMEPK_633224",
)

@dataclass(frozen=True)
class RecoveryDispositionBinding:
    game_pk: int
    query_id: str
    query_sidecar: Mapping[str, str]
    success_receipt: Mapping[str, str]
    retained_scheduled_start_utc: str
    played_date: str | None
    result_available_on: str | None
    raw_object_sha256s: tuple[str, ...]
    selected_raw_game_object_sha256s_sha256: str
    normalized_games_sha256: str
    sidecar_semantics_sha256: str

@dataclass(frozen=True)
class RecoveryDispositionAuthority:
    repository_root: Path
    source_run_spec_path: Path
    acquisition_contract_path: Path
    parent_catalog: RecoveryParentCatalog
    evidence_root: Path
    query: LogicalQuery
    game_pk: int
    played_date: str | None
    result_available_on: str | None

    def sidecar_semantics(self) -> Mapping[str, object]:
        return _recompute_catalog_semantics(self.parent_catalog, self.game_pk)

    def reauthenticate(self) -> "RecoveryDispositionAuthority":
        return build_recovery_schedule_conflict_catalog(
            self.repository_root, self.source_run_spec_path, self.acquisition_contract_path
        ).for_query(self.query)

@dataclass(frozen=True)
class RecoveryDispositionCatalog:
    entries: tuple[RecoveryDispositionAuthority, RecoveryDispositionAuthority, RecoveryDispositionAuthority]

    def for_game_pk(self, game_pk: int) -> RecoveryDispositionAuthority:
        return next(entry for entry in self.entries if entry.game_pk == game_pk)

    def for_query(self, query: LogicalQuery) -> RecoveryDispositionAuthority:
        return next(entry for entry in self.entries if entry.query == query)

    def bindings(self) -> tuple[RecoveryDispositionBinding, RecoveryDispositionBinding, RecoveryDispositionBinding]:
        return tuple(_binding_for(entry) for entry in self.entries)
```

`entries` is permanently in gamePk order `(565534, 630882, 633224)`. Every
checkpoint `dispositioned_query_ids` is the separately sorted tuple of the three
`binding.query_id` values. Validate all exact object indices/digests/counts.
Only 630882 emits `2020-08-14`; only 633224 emits `2021-09-24`; 565534 rejects
both fields.

`_binding_for()` recomputes every binding field from the authenticated parent
catalog before returning it. Thus `retained_scheduled_start_utc`,
`raw_object_sha256s`, `selected_raw_game_object_sha256s_sha256`,
`normalized_games_sha256`, and `sidecar_semantics_sha256` are never caller
inputs.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule.RecoveryDispositionCatalogTests -v`

Expected: PASS; changed index, digest, status, linkage, or boundary fails.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_schedule_conflict_recovery_disposition.py tests/test_v52_source_schedule.py
git commit -m "feat: add recovery disposition catalog"
```

### Task 4: Use the real V52 evidence-store lease API for raw-less publication

**Files:**
- Modify: `mlb_backtest/v52_evidence.py:2778,4391,4429,5237`
- Modify: `mlb_backtest/v52_source_contracts.py:67,90`
- Modify: `mlb_backtest/v52_acquire.py:1525,2092,3014,3552`
- Modify: `tests/test_v52_evidence.py`, `tests/test_v52_acquire.py`

- [ ] **Step 1: Add TestCase RED test using the store API**

```python
class RecoveryDerivedEvidenceTests(unittest.TestCase):
    def test_derived_publish_has_no_attempt_or_raw(self) -> None:
        authority = recovery_catalog().for_game_pk(630882)
        store = V52EvidenceStore(authority.evidence_root)
        with store.acquire_query(authority.query):
            published = store.publish_derived_disposition(authority.query, authority.sidecar_semantics(), authority=authority)
        self.assertIsNone(published.raw_sha256)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_evidence.RecoveryDerivedEvidenceTests -v`

Expected: FAIL because derived admission recognizes only the old singleton authority.

- [ ] **Step 3: Extend exact authority dispatch**

```python
if query.endpoint in RECOVERY_DISPOSITION_ENDPOINTS:
    if (not isinstance(authority, RecoveryDispositionAuthority)
            or authority.query != query
            or query.source != "mlb_schedule_game_feed"
            or query.grain != "season_schedule_and_game"
            or authority.evidence_root != self.root):
        raise TypeError("recovery schedule disposition requires its authenticated authority")
    if authority.reauthenticate() != authority:
        raise ValueError("recovery schedule disposition rebind differs")
```

Use this guard in `_admit_derived_authority`, `_derived_semantics`, and `recompute_query_sidecar`. Publication remains inside `with store.acquire_query(query):`; reauthenticate immediately before `success.json`. Cleanup removes only a newly-owned sidecar inode.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_evidence tests.test_v52_acquire -v`

Expected: PASS; no raw object, attempt, reservation, retry, or network operation is created for any catalog entry.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_evidence.py mlb_backtest/v52_source_contracts.py mlb_backtest/v52_acquire.py tests/test_v52_evidence.py tests/test_v52_acquire.py
git commit -m "feat: publish recovery dispositions under a lease"
```

### Task 5: Restrict v5.2.2 and split raw occurrence counts from normalization

**Files:**
- Modify: `mlb_backtest/source_parsers/schedule.py:295`
- Modify: `mlb_backtest/v52_sources/schedule.py:153,365,436`
- Modify: `mlb_backtest/v52_source_contracts.py:67`
- Modify: `mlb_backtest/v52_query_plan.py:273,1357,1444,1759`
- Modify: `mlb_backtest/v52_acquire.py:55,80,806,1000,1040,2363,3893,3954`
- Modify: `tests/test_v52_source_schedule.py`, `tests/test_v52_query_plan.py`, `tests/test_v52_acquire.py`

- [ ] **Step 1: Add TestCase RED tests**

```python
class RecoveryParserVersionTests(unittest.TestCase):
    def test_v522_preserves_raw_and_normalized_counts(self) -> None:
        self.assertEqual(schedule_counts(2018), (2485, 2430, 53, 108, 55))
        self.assertEqual(schedule_counts(2022), (2479, 2430, 49, 98, 49))
        self.assertEqual(schedule_counts(2023), (2475, 2430, 45, 90, 45))
        self.assertEqual(schedule_counts(2024), (2469, 2430, 37, 76, 39))
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule tests.test_v52_query_plan tests.test_v52_acquire -v`

Expected: FAIL because raw duplicate IDs are rejected before ordinary duplicate reconciliation.

- [ ] **Step 3: Implement the parser/version boundary**

```python
def _require_recovery_schedule_query(query: LogicalQuery, context: SourceAuthorityContext) -> None:
    if (query.run_spec_sha256 != context.authority_sha256
            or query.acquisition_contract_sha256 != context.acquisition_contract_sha256
            or query.parser_version != "v5.2.2"
            or query.endpoint not in {_SCHEDULE_ENDPOINT, *RECOVERY_DISPOSITION_ENDPOINTS}):
        raise ValueError("recovery schedule query authority differs")
```

Keep generic `require_query()` exactly `v5.2.1`; invoke the helper only after
`_load_static_contracts()` proves the exact recovery RunSpec/contract digest
pair. Add `parse_schedule_occurrences(raw)` in `source_parsers/schedule.py` to
count raw regular occurrences without selection. In `v52_sources/schedule.py`, compare
raw `totalGames` to raw occurrences and normalized `parse_schedule(raw)` separately.
The static validator accepts only HTTP 2018, derived 2019, derived 2020, derived
2021, HTTP 2022, HTTP 2023, HTTP 2024. Boxscore and all non-schedule queries
remain `v5.2.1`.

Define `RECOVERY_STATIC_QUERY_TABLE` as the exact authority-bound predicate:
`(2018, schedule HTTP, v5.2.2)`, `(2019, 565534 disposition, v5.2.2)`,
`(2020, 630882 disposition, v5.2.2)`, `(2021, 633224 disposition, v5.2.2)`,
`(2022, schedule HTTP, v5.2.2)`, `(2023, schedule HTTP, v5.2.2)`, and
`(2024, schedule HTTP, v5.2.2)`. The table is constructed from the recovery
catalog endpoints and `_SCHEDULE_ENDPOINT`.
`_validate_recovery_static_schedule_queries(context, queries)`
also compares every query's RunSpec and contract digest to `context`.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule tests.test_v52_query_plan tests.test_v52_acquire -v`

Expected: PASS; HTTP 2020 and 2021 still reject terminal conflicts.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/source_parsers/schedule.py mlb_backtest/v52_sources/schedule.py mlb_backtest/v52_source_contracts.py mlb_backtest/v52_query_plan.py mlb_backtest/v52_acquire.py tests/test_v52_source_schedule.py tests/test_v52_query_plan.py tests/test_v52_acquire.py
git commit -m "feat: constrain recovery schedule parser v5.2.2"
```

### Task 6: Propagate the ordered catalog through checkpoints

**Files:**
- Modify: `mlb_backtest/execution_registry.py:251,4696,4714,5010`
- Modify: `mlb_backtest/v52_acquire.py:806,1525`
- Modify: `schemas/v5_2_acquisition_checkpoint.schema.json`
- Modify: `tests/test_execution_registry.py`, `tests/test_v52_acquire.py`

- [ ] **Step 1: Add TestCase RED test**

```python
class RecoveryCheckpointTests(unittest.TestCase):
    def test_static_checkpoint_requires_three_sorted_dispositions(self) -> None:
        checkpoint = recovery_static_checkpoint()
        self.assertEqual(len(checkpoint.dispositioned_query_ids), 3)
        self.assertEqual(checkpoint.dispositioned_query_ids, tuple(sorted(checkpoint.dispositioned_query_ids)))
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry tests.test_v52_acquire -v`

Expected: FAIL because the writer validates one singleton disposition.

- [ ] **Step 3: Implement recovery-only catalog binding**

```python
def _validate_recovery_disposition_population(ids: Sequence[str], catalog: RecoveryDispositionCatalog) -> tuple[str, str, str]:
    expected = tuple(sorted(binding.query_id for binding in catalog.bindings()))
    if tuple(ids) != expected:
        raise ValueError("recovery checkpoint dispositioned query IDs differ")
    return expected
```

Call this only for a recovery checkpoint after at least one disposition is
completed. `publish_acquisition_checkpoint()` receives `dispositioned_query_ids=()`
only for the genesis STATIC_SCHEDULES checkpoint with seven pending IDs. Its
post-publication and later loader path calls `_validate_recovery_disposition_population`
and requires all three IDs. Extend `_validate_checkpoint_payload`,
`load_latest_acquisition_checkpoint`, `load_terminal_acquisition_checkpoint`, and
the checkpoint writer at `execution_registry.py:5010` to carry the three exact
binding objects: parent raw/object digest, selected-object digest, sidecar
semantics digest, sidecar path/SHA-256, and success path/SHA-256. Terminal static
stage has seven completed and no pending, quarantine, or split IDs.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry tests.test_v52_acquire -v`

Expected: PASS; duplicate ID, one/two/four bindings, stale sidecar, and writer race fail.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/execution_registry.py mlb_backtest/v52_acquire.py schemas/v5_2_acquisition_checkpoint.schema.json tests/test_execution_registry.py tests/test_v52_acquire.py
git commit -m "feat: bind recovery catalog in checkpoints"
```

### Task 7: Carry three bindings through schedule and game inventory

**Files:**
- Modify: `mlb_backtest/v52_schedule_inventory.py:42,1127`
- Modify: `mlb_backtest/v52_game_evidence_inventory.py:662,753,956,965,1562`
- Modify: `mlb_backtest/v52_query_plan.py:1759`
- Modify: `schemas/v5_2_schedule_anchor_inventory.schema.json`, `schemas/v5_2_game_evidence_inventory.schema.json`
- Modify: `tests/test_v52_schedule_inventory.py`, `tests/test_v52_game_evidence_inventory.py`

- [ ] **Step 1: Add TestCase RED test**

```python
class RecoveryScheduleInventoryTests(unittest.TestCase):
    def test_inventory_has_three_retained_special_anchors(self) -> None:
        payload = build_schedule_anchor_inventory(
            ROOT, RECOVERY_RUN_SPEC_OBJECT,
            acquisition_contract_path=RECOVERY_CONTRACT,
            static_batch_id=RECOVERY_STATIC_BATCH_ID,
            evidence_root=RECOVERY_RAW_ROOT,
        )
        self.assertEqual(payload["schedule_conflict_dispositions"]["query_ids"], sorted(payload["schedule_conflict_dispositions"]["query_ids"]))
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_schedule_inventory tests.test_v52_game_evidence_inventory -v`

Expected: FAIL because inventory accepts the singular old representation only.

- [ ] **Step 3: Implement recovery-only inventory branch**

```python
@dataclass(frozen=True)
class RecoveryDispositionBindings:
    entries: tuple[RecoveryDispositionBinding, RecoveryDispositionBinding, RecoveryDispositionBinding]

    def query_ids(self) -> tuple[str, str, str]:
        return tuple(binding.query_id for binding in self.entries)

    def as_mapping(self) -> dict[str, object]:
        return {
            "entries": [
                {
                    "game_pk": entry.game_pk,
                    "query_id": entry.query_id,
                    "query_sidecar": dict(entry.query_sidecar),
                    "success_receipt": dict(entry.success_receipt),
                    "retained_scheduled_start_utc": entry.retained_scheduled_start_utc,
                    "played_date": entry.played_date,
                    "result_available_on": entry.result_available_on,
                    "raw_object_sha256s": list(entry.raw_object_sha256s),
                    "selected_raw_game_object_sha256s_sha256": entry.selected_raw_game_object_sha256s_sha256,
                    "normalized_games_sha256": entry.normalized_games_sha256,
                    "sidecar_semantics_sha256": entry.sidecar_semantics_sha256,
                }
                for entry in self.entries
            ],
            "query_ids": list(sorted(self.query_ids())),
        }

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], catalog: RecoveryDispositionCatalog
    ) -> "RecoveryDispositionBindings":
        expected = cls(catalog.bindings())
        if canonical_json_bytes(value) != canonical_json_bytes(expected.as_mapping()):
            raise ValueError("recovery disposition bindings differ")
        return expected
```

Wire this model into `build_schedule_anchor_inventory(root, run_spec, *,
acquisition_contract_path, static_batch_id, evidence_root, read_only_progress=False)`
and its matching loader. Require seven season sources, retained anchors for
565534/630882/633224, and no child edge derived from a suppressed object.
`from_mapping()` rejects an omitted/extra key, noncanonical key order, a
reordered entry, or any field that differs from `catalog.bindings()`; it returns
only the freshly reauthenticated projection. Reference-batch construction reads
the binding catalog only after authenticated inventory loading.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_schedule_inventory tests.test_v52_game_evidence_inventory -v`

Expected: PASS; missing, extra, cross-run, reordered, or copied binding fails.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_schedule_inventory.py mlb_backtest/v52_game_evidence_inventory.py mlb_backtest/v52_query_plan.py schemas/v5_2_schedule_anchor_inventory.schema.json schemas/v5_2_game_evidence_inventory.schema.json tests/test_v52_schedule_inventory.py tests/test_v52_game_evidence_inventory.py
git commit -m "feat: project recovery schedule conflict bindings"
```

### Task 8: Enforce resumed-result availability in source history and feature consumers

**Files:**
- Modify: `mlb_backtest/v52_sources/history.py:255`
- Modify: `mlb_backtest/v52_sources/statcast.py:101,113`
- Modify: `mlb_backtest/prepare.py:961,1198`
- Modify: `mlb_backtest/feature_engine.py:102,981`
- Modify: `mlb_backtest/v52_game_evidence_inventory.py`
- Modify: `mlb_backtest/v52_verify_sources.py`
- Modify: `schemas/v5_2_schedule_anchor_inventory.schema.json`, `schemas/v5_2_game_evidence_inventory.schema.json`, `schemas/v5_2_normalized_snapshot.schema.json`
- Modify: `tests/test_v52_source_history.py`, `tests/test_prepare.py`, `tests/test_feature_engine.py`, `tests/test_v52_game_evidence_inventory.py`, `tests/test_v52_verify_sources.py`

- [ ] **Step 1: Add TestCase RED behavior tests**

```python
class RecoveryAvailabilityTests(unittest.TestCase):
    def test_630882_first_enters_history_on_2020_08_15(self) -> None:
        history = recovery_history_for(630882)
        self.assertNotIn(630882, visible_game_pks(history, date(2020, 8, 14)))
        self.assertIn(630882, visible_game_pks(history, date(2020, 8, 15)))

    def test_633224_uses_local_resume_date_not_utc_date(self) -> None:
        self.assertEqual(recovery_game(633224)["played_date"], "2021-09-24")
        self.assertNotIn(633224, visible_game_pks(recovery_history_for(633224), date(2021, 9, 24)))

    def test_boundary_rejects_changed_or_cross_bound_catalog_fields(self) -> None:
        bindings = RecoveryDispositionBindings(recovery_catalog().bindings())
        with self.assertRaisesRegex(ValueError, "availability boundary"):
            _availability_date({**recovery_game(630882), "result_available_on": "2020-08-13"}, bindings)
        with self.assertRaisesRegex(ValueError, "availability boundary"):
            _availability_date({**recovery_game(630882), "schedule_query_sidecar_sha256": bindings.entries[2].query_sidecar["sha256"]}, bindings)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_history tests.test_prepare tests.test_feature_engine -v`

Expected: FAIL because history and ScheduleHistory index by `official_date`.

- [ ] **Step 3: Implement authenticated availability projection**

```python
def _availability_date(row: Mapping[str, object], capability: RecoveryDispositionBindings | None) -> date:
    value = row.get("result_available_on")
    if value is None:
        if row.get("played_date") is not None:
            raise ValueError("unapproved schedule availability boundary")
        return _day(row["official_date"], "official_date")
    if capability is None:
        raise ValueError("unapproved schedule availability boundary")
    return capability.require_boundary(row)
```

Add `RecoveryDispositionBindings.require_boundary(row) -> date`. It accepts only
the exact `630882` or `633224` entry and compares all of `game_pk`, immutable
`official_date`, `retained_scheduled_start_utc`, schedule-sidecar SHA-256,
`played_date`, and `result_available_on` with that entry. It returns the
authenticated availability date only after every comparison succeeds. It rejects
a boundary field on `565534` or an ordinary row, a missing paired field, a
changed date, a copied sidecar, an unlisted gamePk, and a capability whose three
entries do not equal a freshly reauthenticated catalog projection.

`v52_game_evidence_inventory` and `v52_verify_sources` authenticate the catalog
sidecar before emitting the two fields. `v52_sources/statcast.py` requires raw
Savant `game_date == played_date` for 630882/633224 and preserves the ordinary
official-date join otherwise. Keep `official_date` immutable. Pass the capability
only on the recovery-gated `prepare.py` and `feature_engine.py` path; their
ordinary public inputs reject availability fields. Index `ScheduleHistory`,
`last_before`, D-1 rest, travel, home-city, innings, and latest-game calculations
by `_availability_date(row, capability)`. Preserve all three existing 2019
neutralization records unchanged.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_history tests.test_prepare tests.test_feature_engine -v`

Expected: PASS; 630882 first appears 2020-08-15 and 633224 first appears 2021-09-25.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_sources/history.py mlb_backtest/v52_sources/statcast.py mlb_backtest/v52_game_evidence_inventory.py mlb_backtest/v52_verify_sources.py mlb_backtest/prepare.py mlb_backtest/feature_engine.py schemas/v5_2_schedule_anchor_inventory.schema.json schemas/v5_2_game_evidence_inventory.schema.json schemas/v5_2_normalized_snapshot.schema.json tests/test_v52_source_history.py tests/test_prepare.py tests/test_feature_engine.py tests/test_v52_game_evidence_inventory.py tests/test_v52_verify_sources.py
git commit -m "feat: enforce resumed result availability"
```

### Task 9: Rebuild normalized snapshots and the 81-row source inventory

**Files:**
- Modify: `mlb_backtest/v52_verify_sources.py:248,344,384`
- Modify: `mlb_backtest/v52_snapshot_store.py:44,101,248,317,450,620,651,817,1247`
- Modify: `schemas/v5_2_normalized_snapshot.schema.json`, `schemas/v5_2_source_inventory.schema.json`, `schemas/v5_2_clean_source_replay_receipt.schema.json`
- Modify: `tests/test_v52_verify_sources.py`, `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Add TestCase RED cardinality test**

```python
class RecoverySnapshotReplayTests(unittest.TestCase):
    def test_recovery_replay_has_81_snapshots_42_pairs_and_three_bindings(self) -> None:
        inputs = SnapshotStoreInputs(
            replay=load_snapshot_replay_inputs(ROOT, RECOVERY_RUN_SPEC_OBJECT, acquisition_contract_path=RECOVERY_CONTRACT),
            bindings=recovery_authority_bindings(), current_phase="ACQUIRING")
        inventory = build_inventory(inputs)
        self.assertEqual(inventory.counts["snapshot_count"], 81)
        self.assertEqual(inventory.counts["source_season_count"], 42)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_verify_sources tests.test_v52_snapshot_store -v`

Expected: FAIL because the replay model stores one disposition binding.

- [ ] **Step 3: Implement recovery snapshot envelope validation**

```python
def _validate_recovery_snapshot_bindings(bindings: AuthorityBindings) -> None:
    catalog = bindings.schedule_conflict_dispositions
    if catalog is None or len(catalog.entries) != 3:
        raise ValueError("recovery snapshot disposition bindings differ")
```

Add `schedule_conflict_dispositions: RecoveryDispositionBindings | None` to
`AuthorityBindings`; carry it into `SnapshotStoreInputs`, `SourceInventory`, and
`NormalizedSnapshot`. Require `snapshot_count == 81`, `source_season_count == 42`,
`source_count == 6`, seven `mlb_schedule_game_feed` season partitions, and three
catalog sidecar/success bindings on every recovery inventory/replay envelope.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_verify_sources tests.test_v52_snapshot_store -v`

Expected: PASS; one, two, four, stale, or reordered binding fails.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_verify_sources.py mlb_backtest/v52_snapshot_store.py schemas/v5_2_normalized_snapshot.schema.json schemas/v5_2_source_inventory.schema.json schemas/v5_2_clean_source_replay_receipt.schema.json tests/test_v52_verify_sources.py tests/test_v52_snapshot_store.py
git commit -m "feat: seal recovery source inventory"
```

### Task 10: Gate review, finalization, and SOURCES_READY

**Files:**
- Modify: `mlb_backtest/v52_source_finalize.py:86,196,289`
- Modify: `mlb_backtest/v52_source_orchestration.py:76,91,700,835,961,1238,1525`
- Modify: `mlb_backtest/v52_timezone_review.py:44,90,118,1197,1289,1961,2288`
- Modify: `schemas/v5_2_source_review_receipt.schema.json`, `schemas/v5_2_clean_source_replay_receipt.schema.json`, `schemas/v5_2_source_receipt_commit.schema.json`, `schemas/v5_2_execution_receipt.schema.json`, `schemas/v5_2_timezone_candidate.schema.json`, `schemas/v5_2_timezone_review_receipt.schema.json`, `schemas/v5_2_timezone_seal_receipt.schema.json`, `schemas/v5_2_venue_timezones.schema.json`
- Modify: `tests/test_v52_source_finalize.py`, `tests/test_execution_registry.py`, `tests/test_v52_timezone_review.py`

- [ ] **Step 1: Add TestCase RED finalization test**

```python
class RecoveryFinalizationTests(unittest.TestCase):
    def test_advance_requires_two_distinct_replay_execution_ids(self) -> None:
        with self.assertRaisesRegex(FinalizationError, "exactly two"):
            advance_recovery_with_replays((replay_receipt("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),))
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_finalize tests.test_execution_registry -v`

Expected: FAIL because recovery catalog evidence is not revalidated by finalization.

- [ ] **Step 3: Implement exact recovery finalization**

```python
def _source_disposition_payload_fields(context: FinalizationContext, inventory: SourceInventory) -> dict[str, object]:
    branch = _exact_authority_branch(context.run_spec, context.authority_context)
    if branch is _HISTORICAL_BRANCH:
        return {}
    if branch is _SUPERSEDING_565534_BRANCH:
        return _superseding_565534_disposition_payload_fields(context, inventory)
    if branch is _RECOVERY_SCHEDULE_CONFLICTS_BRANCH:
        if inventory is None:
            raise ValueError("recovery disposition payload differs")
        bindings = inventory.bindings.schedule_conflict_dispositions
        if bindings is None or len(bindings.entries) != 3:
            raise ValueError("recovery disposition payload differs")
        return {"schedule_conflict_dispositions": bindings.as_mapping()}
    raise ValueError("source disposition authority branch differs")
```

Extend the actual `_source_disposition_payload_fields(context, inventory)` in
`v52_source_orchestration.py:961`; call it from status, preflight, publish,
review, replay, and advance payload creation. The review command records reviewer
identity `independent-source-auditor` and role `independent-source-auditor`.
Advance accepts one or more review receipts and exactly two replay receipts with
distinct canonical UUIDv4 execution IDs, then rebinds checkpoint, inventory,
catalog, and Git head.

`_exact_authority_branch()` selects only the exact RunSpec/contract path pair;
it must dispatch historical and superseding paths before reading recovery-only
attributes. Historical returns `{}`, the old superseding helper retains its
existing singular payload byte shape, and only the recovery branch emits
`schedule_conflict_dispositions`. Add regression tests proving that historical
and superseding inventories without that attribute load unchanged, while the
recovery branch rejects its absence. Recovery advance additionally requires terminal
`REMAINING`, zero pending/quarantined/split IDs, all three full bindings, and
exactly two projected availability rows for 630882 and 633224.

Extend every `v52_timezone_review` subcommand (`candidate`, `review`, and
`seal`) with backward-compatible `--run-spec` and `--acquisition-contract`
arguments. Their defaults remain the existing historical authority. Resolve the
pair through `_load_static_contracts()` and the exact `SourceAuthorityContext`,
then add a third `_timezone_paths(context)` branch for this recovery authority:
candidate, semantic review, coverage review, seal, schedule inventory, and
query-batch root are respectively under
`audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition/`; the
RunSpec/contract paths are the two recovery protocol paths. The existing
production mapping path remains `config/v5_2_qd/venue_timezones.json`, but its
seal receipt must bind the recovery authority and dynamic audit paths. Reject a
recovery path supplied with either old authority, an old path supplied with the
recovery authority, and any inferred/fallback path. Add a closed recovery
schema branch for each listed timezone artifact. Task 12 supplies both new
arguments to every timezone command.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_finalize tests.test_execution_registry -v`

Expected: PASS; finalization rejects invalid reviewer role, replay UUID, stale catalog, or changed checkpoint.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_source_finalize.py mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_timezone_review.py schemas/v5_2_source_review_receipt.schema.json schemas/v5_2_clean_source_replay_receipt.schema.json schemas/v5_2_source_receipt_commit.schema.json schemas/v5_2_execution_receipt.schema.json schemas/v5_2_timezone_candidate.schema.json schemas/v5_2_timezone_review_receipt.schema.json schemas/v5_2_timezone_seal_receipt.schema.json schemas/v5_2_venue_timezones.schema.json tests/test_v52_source_finalize.py tests/test_execution_registry.py tests/test_v52_timezone_review.py
git commit -m "feat: finalize recovery sources safely"
```

### Task 11: Seal the execution closure and preserve the exact baseline runner

**Files:**
- Modify: `mlb_backtest/execution_registry.py`
- Modify: `mlb_backtest/verify_v52_design.py:43,937,951`
- Modify: `scripts/run_task13_exact_suite.py`
- Modify: `tests/test_execution_registry.py`, `tests/test_verify_v52_design.py`

- [ ] **Step 1: Add TestCase RED closure test**

```python
class RecoveryExecutionClosureTests(unittest.TestCase):
    def test_recovery_catalog_module_is_bound_execution_code(self) -> None:
        self.assertIn("mlb_backtest/v52_schedule_conflict_recovery_disposition.py", _BOUND_EXECUTION_CODE)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry tests.test_verify_v52_design -v`

Expected: FAIL because the new module and recovery schemas are absent from the exact closure.

- [ ] **Step 3: Bind code without self-hash circularity**

Add the recovery catalog module and schema consumers to `_BOUND_EXECUTION_CODE`.
Bind `mlb_backtest/v52_recovery_parent_evidence.py` and every changed recovery
schema explicitly, including `schemas/v5_2_run_spec.schema.json`,
`schemas/v5_2_acquisition_contract.schema.json`,
`schemas/v5_2_derived_disposition_sidecar.schema.json`,
`schemas/v5_2_query_sidecar.schema.json`,
`schemas/v5_2_acquisition_checkpoint.schema.json`,
`schemas/v5_2_schedule_anchor_inventory.schema.json`,
`schemas/v5_2_game_evidence_inventory.schema.json`,
`schemas/v5_2_normalized_snapshot.schema.json`,
`schemas/v5_2_source_inventory.schema.json`,
`schemas/v5_2_source_review_receipt.schema.json`,
`schemas/v5_2_clean_source_replay_receipt.schema.json`,
`schemas/v5_2_source_receipt_commit.schema.json`,
`schemas/v5_2_execution_receipt.schema.json`,
`schemas/v5_2_timezone_candidate.schema.json`,
`schemas/v5_2_timezone_review_receipt.schema.json`,
`schemas/v5_2_timezone_seal_receipt.schema.json`, and
`schemas/v5_2_venue_timezones.schema.json`.
First stage and commit every final closure-code/schema/test change, this design,
and this plan. Commit the canonical recovery RunSpec second. Calculate the
committed design blob ID, plan blob ID, RunSpec blob ID, and RunSpec SHA-256;
create the contract binding only those values and the three parent objects, then
commit the contract third. Finally verify the resulting HEAD against the
static/execution closure. Do not edit a bound code, schema, design, plan,
RunSpec, or contract file after that verify; the contract never binds its own
uncommitted bytes.

Keep the runner's current `V51_REQUIRED_FILES` and `OLD_EVIDENCE_RELATIVES`
validation dynamically unchanged. Add a separate explicit
`validate_recovery_parent_inputs(repo: Path) -> Mapping[str, str]` API for the
2020/2021 parent registrations, attempts, quarantines, and raw bodies; do not
append those inputs to either legacy tuple.

- [ ] **Step 4: Run PASS**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry tests.test_verify_v52_design -v && PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_task13_exact_suite.py --help`

Expected: PASS; the unchanged dynamic baseline and separate 2020/2021 input API both validate.

- [ ] **Step 5: Verify the post-contract tree is clean**

Run: `git status --porcelain=v1 && PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.verify_v52_design --root .`

Expected: no output from `git status`; verification observes the contract commit
as the final execution-closure HEAD. Do not create another closure commit.

### Task 12: Run the separate operator recovery workflow

**Files produced by tools, never hand-edited:**
- `data/v5_2_qd/historical_recovery_schedule_conflicts/raw/`
- `audit/v5_2_qd/historical_recovery_schedule_conflicts/`
- `data/v5_2_qd/historical_recovery_schedule_conflicts/sources/source_inventory.json`

- [ ] **Step 1: Verify implemented CLI flags and clean committed authority**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --help && PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize advance --help && git status --porcelain=v1`

Expected: help lists `--acquisition-contract`, `--plan-only`, `--resume`, two repeatable `--replay-receipt` values, and status output is empty.

Before each acquisition, timezone, publish, replay, review, and advance write,
run the Task 1 fingerprint capture for both immutable trees and save the two
canonical manifests under `$RECOVERY_AUDIT/acquisition/parent_fingerprints/`.
Immediately re-run the capture after the write and require byte-identical
manifests; a difference stops the operator workflow.

- [ ] **Step 2: Build and acquire the recovery**

```bash
export RECOVERY_RUN_SPEC=protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json
export RECOVERY_CONTRACT=protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json
export RECOVERY_AUDIT=audit/v5_2_qd/historical_recovery_schedule_conflicts
export SOURCE_ROOT=data/v5_2_qd/historical_recovery_schedule_conflicts/sources
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --years 2018:2024 --mode hybrid --legacy-root data/v5_1_bt/raw --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --plan-only
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --years 2018:2024 --mode hybrid --legacy-root data/v5_1_bt/raw --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --resume --stop-after reference-inputs
```

Expected: STATIC_SCHEDULES has four HTTP attempts and three raw-less publications. The later reference, participant, and remaining stages may make additional network calls.

- [ ] **Step 3: Complete acquisition and publish inventory**

```bash
RECOVERY_RUN_SPEC=protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json
RECOVERY_CONTRACT=protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json
RECOVERY_AUDIT=audit/v5_2_qd/historical_recovery_schedule_conflicts
SOURCE_ROOT=data/v5_2_qd/historical_recovery_schedule_conflicts/sources
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --years 2018:2024 --mode hybrid --legacy-root data/v5_1_bt/raw --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --resume
STATUS_JSON=$(PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize status --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --json)
TIMEZONE_CHECKPOINT=$(python3 -c 'import json,sys; c=json.loads(sys.argv[1])["checkpoint"]; print("audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/acquisition_checkpoints/{:08d}-{}.json".format(int(c["sequence"]), c["head"]))' "$STATUS_JSON")
TIMEZONE_BATCH_IDS=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); print(" ".join(item["batch_id"] for item in value["query_batches"][:2]))' "$TIMEZONE_CHECKPOINT")
set -- $TIMEZONE_BATCH_IDS
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review candidate --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --evidence-root data/v5_2_qd/historical_recovery_schedule_conflicts/raw --batch-id "$1" --batch-id "$2" --output "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json"
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review review --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --candidate "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json" --output "$RECOVERY_AUDIT/acquisition/timezone_reviews/semantic.json" --reviewer-identity timezone-semantic-auditor --reviewer-role TIMEZONE_SEMANTIC_REVIEW --decision APPROVED
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review review --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --candidate "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json" --output "$RECOVERY_AUDIT/acquisition/timezone_reviews/coverage.json" --reviewer-identity timezone-coverage-auditor --reviewer-role TIMEZONE_COVERAGE_REVIEW --decision APPROVED
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review seal --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --candidate "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json" --review-receipt "$RECOVERY_AUDIT/acquisition/timezone_reviews/semantic.json" --review-receipt "$RECOVERY_AUDIT/acquisition/timezone_reviews/coverage.json" --output config/v5_2_qd/venue_timezones.json --receipt-output "$RECOVERY_AUDIT/acquisition/venue_timezone_seal.json"
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize publish --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --lock-owner recovery-source-publisher
```

Expected: `$SOURCE_ROOT/source_inventory.json` has 81 snapshots and three ordered catalog bindings.

- [ ] **Step 4: Write two replays, review, and advance**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize replay --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --write-receipt audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition/source_replays/replay-one.json --write-commit audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition/source_replays/replay-one.commit.json
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize replay --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --write-receipt audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition/source_replays/replay-two.json --write-commit audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition/source_replays/replay-two.commit.json
mkdir -p "$RECOVERY_AUDIT/acquisition/source_reviews"
PYTHONDONTWRITEBYTECODE=1 python3 -c 'import json,sys; from pathlib import Path; value=json.loads(Path(sys.argv[1]).read_text()); ids=sorted(set(value["logical_query_ids"])); Path(sys.argv[2]).write_text(json.dumps({"query_ids":ids}, sort_keys=True, separators=(",",":"))+"\n", encoding="utf-8")' "$SOURCE_ROOT/source_inventory.json" "$RECOVERY_AUDIT/acquisition/source_reviews/canonical_query_ids.json"
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize review --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --reviewer-identity independent-source-auditor --reviewer-role independent-source-auditor --decision APPROVED --query-ids-file "$RECOVERY_AUDIT/acquisition/source_reviews/canonical_query_ids.json" --write-receipt "$RECOVERY_AUDIT/acquisition/source_reviews/review.json" --write-commit "$RECOVERY_AUDIT/acquisition/source_reviews/review.commit.json"
STATUS_JSON=$(PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize status --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --json)
CHECKPOINT=$(python3 -c 'import json,sys; value=json.loads(sys.argv[1]); c=value["checkpoint"]; print("audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/acquisition_checkpoints/{:08d}-{}.json".format(int(c["sequence"]), c["head"]))' "$STATUS_JSON")
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize advance --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --review-receipt "$RECOVERY_AUDIT/acquisition/source_reviews/review.commit.json" --replay-receipt "$RECOVERY_AUDIT/acquisition/source_replays/replay-one.commit.json" --replay-receipt "$RECOVERY_AUDIT/acquisition/source_replays/replay-two.commit.json" --checkpoint "$CHECKPOINT" --inventory "$SOURCE_ROOT/source_inventory.json"
```

Expected: phase is `SOURCES_READY`; both replay receipts have distinct UUIDv4 execution IDs.

- [ ] **Step 5: Run exact verification and stop**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_task13_exact_suite.py --repo . --v51-input-root data/v5_1_bt/raw --old-v52-evidence-root data/v5_2_qd/historical && ruff check mlb_backtest tests && git diff --check`

Expected: PASS with unchanged protected manifests. Do not run fitting, OOF, cards, targets, selections, outcomes, or reports.
