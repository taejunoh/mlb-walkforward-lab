# MLB v5.2 Integrated Snapshot Store Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist 81 verified v5.2 normalized snapshots as an immutable content-addressed store and prove a network-free, read-only exact replay.

**Architecture:** `v52_verify_sources.py` owns `SnapshotReplayInputs` and pure replay only. `v52_snapshot_store.py` wraps those inputs with six authority bindings and the current external phase, then owns the closed 81/42 inventory, no-clobber publication, and a loader that reads only inventory-bound paths and invokes the same replay. `source_inventory.json` is the only final commit point; execution phase remains external.

**Tech Stack:** Python 3.11 (`python3` in this worktree), `dataclasses`, `pathlib`/`os` no-follow descriptors, SHA-256, canonical JSON (`sort_keys=True`, UTF-8, one trailing newline), JSON Schema Draft 2020-12, `unittest`, installed `jsonschema==4.25.1`, and Ruff.

---

## File map and boundaries

- Create `mlb_backtest/v52_snapshot_store.py`: frozen `AuthorityBinding`, `AuthorityBindings`, `SnapshotStoreInputs`, inventory/receipt/loaded-store types, canonical serialization, build/read/validate, atomic publish, and loader.
- Modify `mlb_backtest/v52_verify_sources.py`: frozen `SnapshotReplayInputs` plus one pure façade over existing builders; no filesystem, HTTP, environment-path lookup, or registry mutation.
- Create `schemas/v5_2_source_inventory.schema.json`: closed top-level and nested inventory schema.
- Create `tests/v52_snapshot_store_fixture.py`: deterministic fake-81 snapshots and `SnapshotReplayInputs` factory used only by tests/evidence support.
- Create `tests/test_v52_snapshot_store.py`: contract tests plus filesystem attack/crash tests. Production modules never import this fixture.
- Create `reports/v5_2_qd/source_snapshot_store_publish.json` and `.md` only in Task 6.
- Never touch/stage these protected duplicates: `mlb_backtest/v52_query_plan 2.py`, `tests/test_v52_query_plan 2.py`, `reports/v5_2_qd/walkforward_500_preflight 2.md`, `schemas/v5_2_preflight.schema 2.json`.

Current commands are `python3 -m unittest discover -s tests -p 'test*.py' -v`, targeted `python3 -m unittest tests.test_v52_snapshot_store -v`, `ruff check mlb_backtest tests`, and `git diff --check`. `jsonschema` is already installed (4.25.1); no dependency change is needed.

### Task 1: Add the pure replay façade and deterministic fake-81 test fixture

**Files:**
- Modify: `mlb_backtest/v52_verify_sources.py`
- Create: `tests/v52_snapshot_store_fixture.py`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Write the failing test and its complete fixture helpers**

```python
from mlb_backtest.v52_verify_sources import NormalizedSnapshot, SnapshotReplayInputs

def _fake_snapshot(season, source, grain, ordinal, start=None, end=None):
    if source == "baseball_savant_statcast":
        payload = {"parent_coverage": {"inclusive_start": start, "inclusive_end": end}, "grain": "pitch", "marker": ordinal}
    else:
        payload = {"grain": grain, "marker": ordinal}
    query_id = f"{ordinal:064x}"; sidecar = f"{ordinal + 1000:064x}"
    envelope = {"grain": grain, "logical_query_ids": [query_id], "payload": payload, "run_id": "run-2024", "schema_version": 1, "season": season, "source": source, "source_query_sidecar_sha256s": [sidecar], "split_receipt_sha256s": [], "status": "READY"}
    contents = canonical_json_bytes(envelope) + b"\n"
    return NormalizedSnapshot(1, "run-2024", season, source, grain, "READY", (query_id,), (sidecar,), (), payload, contents, hashlib.sha256(contents).hexdigest())

def _fake_81():
    out, ordinal = [], 1
    statcast_months = (7, 7, 3, 7, 7, 8, 7)
    fixed = (("mlb_schedule_game_feed", "season_game_evidence_snapshot"), ("mlb_stats_pitching", "season_d1_league_pitching_reference"), ("mlb_stats_pitcher_bf", "player_career_batters_faced_reference"), ("venue_team_timezone_table", "effective_dated_venue_team_reference"), ("prior_schedule_history", "verified_game_projection"))
    for index, season in enumerate(range(2018, 2025)):
        for source, grain in fixed:
            out.append(_fake_snapshot(season, source, grain, ordinal)); ordinal += 1
        for month in range(1, statcast_months[index] + 1):
            day = f"{month:02d}-01"
            out.append(_fake_snapshot(season, "baseball_savant_statcast", "pitch", ordinal, f"{season}-{day}", f"{season}-{day}")); ordinal += 1
    assert len(out) == 81 and [sum(5 + n for n in statcast_months[:i]) for i in range(7)] == [0, 12, 24, 32, 44, 56, 69]
    return tuple(out)

def _replay_inputs(snapshots):
    return SnapshotReplayInputs("run-2024", SimpleNamespace(), (), tuple(s for s in snapshots if s.source == "mlb_schedule_game_feed"), SimpleNamespace(), SimpleNamespace(), SimpleNamespace())

fake_81 = _fake_81
replay_inputs = _replay_inputs

def test_replay_uses_exact_sha_and_statcast_parent_coverage_key(self):
    snapshots = _fake_81()
    inputs = _replay_inputs(snapshots)
    with patch.object(verifier, "_build_game_normalized_snapshots", return_value=tuple(s for s in snapshots if s.source == "mlb_schedule_game_feed")), patch.object(verifier, "_build_statcast_normalized_snapshots", return_value=tuple(s for s in snapshots if s.source == "baseball_savant_statcast")), patch.object(verifier, "_build_pitching_normalized_snapshots", return_value=tuple(s for s in snapshots if s.source == "mlb_stats_pitching")), patch.object(verifier, "_build_reference_normalized_snapshots", return_value=tuple(s for s in snapshots if s.source == "venue_team_timezone_table")), patch.object(verifier, "_build_history_normalized_snapshots", return_value=tuple(s for s in snapshots if s.source == "prior_schedule_history")), patch.object(verifier, "_build_career_bf_normalized_snapshots", return_value=tuple(s for s in snapshots if s.source == "mlb_stats_pitcher_bf")):
        result = verifier.replay_verified_snapshots(inputs)
    self.assertEqual(len(result), 81)
    self.assertEqual(len({s.sha256 for s in result}), 81)
    self.assertEqual(len({(s.season, s.source) for s in result}), 42)
    keys = [verifier.snapshot_sort_key(s) for s in result]
    self.assertEqual(keys, sorted(keys))
    self.assertEqual(len({(s.season, s.source, s.payload.get("parent_coverage", {}).get("inclusive_start"), s.payload.get("parent_coverage", {}).get("inclusive_end")) for s in result if s.source == "baseball_savant_statcast"}), 46)
```

`_fake_81` must be implemented exactly as a counter-based generator (not the compact expression above): append 7 schedule, 46 Statcast, 7 pitching, 7 career, 7 reference, and 7 history snapshots with globally unique `ordinal`; assign season counts by the approved `12/12/8/12/12/13/12` distribution and Statcast monthly parent dates. The helper is test-only and patches each existing private builder, so no undefined network fixture is required.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_v52_snapshot_store.ReplayTests.test_replay_uses_exact_sha_and_statcast_parent_coverage_key -v`

Expected: `AttributeError` for missing `SnapshotReplayInputs`/`replay_verified_snapshots`.

- [ ] **Step 3: Write minimal implementation**

Add the exact façade and sort key:

```python
@dataclass(frozen=True)
class SnapshotReplayInputs:
    run_id: str; graph: _TerminalQueryGraph; records: tuple[VerifiedQueryRecord, ...]; game_snapshots: tuple[NormalizedSnapshot, ...]
    game_inventory: VerifiedGameEvidenceInventory; timezone_gate: VerifiedTimezoneGate; schedule_inventory: VerifiedScheduleAnchorInventory

def snapshot_sort_key(snapshot: NormalizedSnapshot) -> tuple[object, ...]:
    if snapshot.source == "baseball_savant_statcast":
        coverage = snapshot.payload["parent_coverage"]
        return (snapshot.season, snapshot.source, snapshot.grain, coverage["inclusive_start"], coverage["inclusive_end"])
    return (snapshot.season, snapshot.source, snapshot.grain, "", "")

def replay_verified_snapshots(inputs: SnapshotReplayInputs) -> tuple[NormalizedSnapshot, ...]:
    schedule = _build_game_normalized_snapshots(inputs.run_id, inputs.records, inputs.game_inventory, inputs.timezone_gate, inputs.schedule_inventory)
    if schedule != inputs.game_snapshots:
        raise ValueError("upstream game snapshot authority differs")
    statcast = _build_statcast_normalized_snapshots(inputs.run_id, inputs.records, inputs.graph, schedule)
    pitching = _build_pitching_normalized_snapshots(inputs.run_id, inputs.records, inputs.graph, schedule)
    references = _build_reference_normalized_snapshots(inputs.run_id, inputs.records, inputs.graph, schedule, inputs.timezone_gate)
    history = _build_history_normalized_snapshots(inputs.run_id, schedule)
    career = _build_career_bf_normalized_snapshots(inputs.run_id, inputs.records, inputs.graph, schedule)
    result = tuple(sorted((*schedule, *statcast, *pitching, *references, *history, *career), key=snapshot_sort_key))
    if len(result) != 81 or len({s.sha256 for s in result}) != 81 or len({(s.season, s.source) for s in result}) != 42:
        raise ValueError("replay cardinality must be 81 unique snapshots and 42 source-seasons")
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_v52_snapshot_store.ReplayTests -v && ruff check mlb_backtest/v52_verify_sources.py tests/test_v52_snapshot_store.py`

Expected: `OK`, Ruff exit 0, and no filesystem tree change during façade execution.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_verify_sources.py tests/v52_snapshot_store_fixture.py tests/test_v52_snapshot_store.py
git commit -m "feat: expose pure v5.2 snapshot replay facade"
```

### Task 2: Add frozen inputs, immutable inventory, parser, and schema

**Files:**
- Create: `mlb_backtest/v52_snapshot_store.py`
- Create: `schemas/v5_2_source_inventory.schema.json`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_build_inventory_enforces_closed_counts_and_bindings(self):
    inputs = _store_inputs(_fake_81())
    inventory = _inventory(inputs, _fake_81())
    self.assertEqual(inventory.status, "READY_CANDIDATE")
    self.assertEqual(len(inventory.snapshots), 81)
    self.assertEqual(len(inventory.source_seasons), 42)
    self.assertEqual(inventory.counts["by_season"], {"2018": 12, "2019": 12, "2020": 8, "2021": 12, "2022": 12, "2023": 13, "2024": 12})
    self.assertEqual(inventory.counts["by_source"]["baseball_savant_statcast"], 46)
    self.assertNotIn("phase", inventory.as_mapping())
    self.assertEqual(set(inventory.as_mapping()), {"schema_version", "artifact_kind", "run_id", "status", "critical_errors", "network_attempts", "source_gate_ready", "run_spec", "acquisition_contract", "terminal_checkpoint", "schedule_inventory", "game_evidence_inventory", "timezone_gate", "snapshot_sha256s", "logical_query_ids", "split_receipt_sha256s", "snapshots", "source_seasons", "counts"})
    self.assertEqual(set(inventory.split_receipt_sha256s), {r for s in inventory.snapshots for r in s.split_receipt_sha256s})
    for binding in (inventory.bindings.run_spec, inventory.bindings.acquisition_contract, inventory.bindings.schedule_inventory, inventory.bindings.game_evidence_inventory, inventory.bindings.timezone_gate):
        self.assertFalse(Path(binding.path).is_absolute()); self.assertRegex(binding.sha256, r"^[0-9a-f]{64}$")
    self.assertFalse(Path(inventory.bindings.terminal_checkpoint.path).is_absolute()); self.assertRegex(inventory.bindings.terminal_checkpoint.sha256, r"^[0-9a-f]{64}$")

def test_parse_inventory_rejects_extra_key_and_path_escape(self):
    payload = json.loads(store.inventory_bytes(_inventory(_store_inputs(_fake_81()), _fake_81())))
    payload["latest"] = "mutable"
    with self.assertRaises(ValueError): store.parse_inventory(canonical_json_bytes(payload) + b"\n")
    payload.pop("latest"); payload["snapshots"][0]["relative_path"] = "../escape"
    with self.assertRaises(ValueError): store.parse_inventory(canonical_json_bytes(payload) + b"\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_v52_snapshot_store.InventoryTests -v`

Expected: `ModuleNotFoundError: No module named 'mlb_backtest.v52_snapshot_store'`.

- [ ] **Step 3: Write minimal implementation**

Define these frozen types and implement the shown core (all other fields are copied explicitly, never dynamically):

```python
@dataclass(frozen=True)
class AuthorityBinding: path: str; sha256: str
@dataclass(frozen=True)
class TerminalCheckpointBinding: path: str; sha256: str; sequence: int; stage: Literal["REMAINING"]
@dataclass(frozen=True)
class AuthorityBindings:
    run_spec: AuthorityBinding; acquisition_contract: AuthorityBinding; terminal_checkpoint: TerminalCheckpointBinding
    schedule_inventory: AuthorityBinding; game_evidence_inventory: AuthorityBinding; timezone_gate: AuthorityBinding
@dataclass(frozen=True)
class SnapshotStoreInputs:
    replay: SnapshotReplayInputs; bindings: AuthorityBindings; current_phase: str

def _store_inputs(snapshots, phase="ACQUIRING"):
    b = store.AuthorityBinding
    bindings = store.AuthorityBindings(b("run.json", "a" * 64), b("contract.json", "b" * 64), store.TerminalCheckpointBinding("checkpoint.json", "c" * 64, 4, "REMAINING"), b("schedule.json", "d" * 64), b("games.json", "e" * 64), b("timezone.json", "f" * 64))
    return store.SnapshotStoreInputs(_replay_inputs(snapshots), bindings, phase)

store_inputs = _store_inputs

def _inventory(inputs, snapshots):
    with patch(
        "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
        return_value=tuple(snapshots),
    ):
        return store.build_inventory(inputs)

class HermeticStoreTest(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.snapshots = _fake_81()
        self.replay_patcher = patch(
            "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
            return_value=self.snapshots,
        )
        self.replay_patcher.start()
        self.addCleanup(self.replay_patcher.stop)

@dataclass(frozen=True)
class SnapshotInventoryRow:
    season: int; source: str; grain: str; status: Literal["READY"]; snapshot_sha256: str; relative_path: str; byte_length: int
    logical_query_ids: tuple[str, ...]; source_query_sidecar_sha256s: tuple[str, ...]; split_receipt_sha256s: tuple[str, ...]
@dataclass(frozen=True)
class SourceSeasonInventory:
    season: int; source: str; grain: str; status: Literal["READY"]; snapshot_sha256s: tuple[str, ...]
    logical_query_ids: tuple[str, ...]; split_receipt_sha256s: tuple[str, ...]
@dataclass(frozen=True)
class SourceInventory:
    schema_version: Literal[1]; artifact_kind: Literal["V5_2_SOURCE_INVENTORY"]; run_id: str; status: Literal["READY_CANDIDATE"]
    critical_errors: tuple[str, ...]; network_attempts: int; source_gate_ready: bool; bindings: AuthorityBindings
    snapshot_sha256s: tuple[str, ...]; logical_query_ids: tuple[str, ...]; split_receipt_sha256s: tuple[str, ...]
    snapshots: tuple[SnapshotInventoryRow, ...]; source_seasons: tuple[SourceSeasonInventory, ...]; counts: Mapping[str, object]
    def as_mapping(self) -> dict[str, object]:
        return _inventory_mapping(self)
@dataclass(frozen=True)
class PublishReceipt:
    inventory_sha256: str; snapshot_count: int
@dataclass(frozen=True)
class LoadedSnapshotStore:
    inventory: SourceInventory; snapshots: tuple[NormalizedSnapshot, ...]

def _inventory_mapping(value: SourceInventory) -> dict[str, object]:
    return {"schema_version": 1, "artifact_kind": value.artifact_kind, "run_id": value.run_id, "status": value.status, "critical_errors": list(value.critical_errors), "network_attempts": value.network_attempts, "source_gate_ready": value.source_gate_ready, "run_spec": _binding_mapping(value.bindings.run_spec), "acquisition_contract": _binding_mapping(value.bindings.acquisition_contract), "terminal_checkpoint": _terminal_mapping(value.bindings.terminal_checkpoint), "schedule_inventory": _binding_mapping(value.bindings.schedule_inventory), "game_evidence_inventory": _binding_mapping(value.bindings.game_evidence_inventory), "timezone_gate": _binding_mapping(value.bindings.timezone_gate), "snapshot_sha256s": list(value.snapshot_sha256s), "logical_query_ids": list(value.logical_query_ids), "split_receipt_sha256s": list(value.split_receipt_sha256s), "snapshots": [_row_mapping(row) for row in value.snapshots], "source_seasons": [_source_season_mapping(row) for row in value.source_seasons], "counts": _plain_json(value.counts)}

def _inventory_from_mapping(payload: Mapping[str, object]) -> SourceInventory:
    bindings = AuthorityBindings(_binding_from_mapping(payload["run_spec"]), _binding_from_mapping(payload["acquisition_contract"]), _terminal_from_mapping(payload["terminal_checkpoint"]), _binding_from_mapping(payload["schedule_inventory"]), _binding_from_mapping(payload["game_evidence_inventory"]), _binding_from_mapping(payload["timezone_gate"]))
    rows = tuple(_row_from_mapping(item) for item in payload["snapshots"]); source_seasons = tuple(_source_season_from_mapping(item) for item in payload["source_seasons"])
    return SourceInventory(1, payload["artifact_kind"], payload["run_id"], payload["status"], tuple(payload["critical_errors"]), payload["network_attempts"], payload["source_gate_ready"], bindings, tuple(payload["snapshot_sha256s"]), tuple(payload["logical_query_ids"]), tuple(payload["split_receipt_sha256s"]), rows, source_seasons, payload["counts"])

def build_inventory(inputs: SnapshotStoreInputs) -> SourceInventory:
    if inputs.replay.run_id == "" or inputs.current_phase not in ("ACQUIRING", "SOURCES_READY"): raise ValueError("invalid replay inputs")
    snapshots = replay_verified_snapshots(inputs.replay)
    if len({s.sha256 for s in snapshots}) != 81 or len({(s.season, s.source) for s in snapshots}) != 42: raise ValueError("snapshot cardinality differs")
    rows = tuple(_row(s) for s in snapshots)
    seasons = tuple(_source_season_row(snapshots, season, source) for season in range(2018, 2025) for source in SOURCES)
    return SourceInventory(1, "V5_2_SOURCE_INVENTORY", inputs.replay.run_id, "READY_CANDIDATE", (), 0, True, inputs.bindings, tuple(s.sha256 for s in snapshots), tuple(sorted({q for s in snapshots for q in s.logical_query_ids})), tuple(sorted({r for s in snapshots for r in s.split_receipt_sha256s})), rows, seasons, _counts(snapshots))

def inventory_bytes(inventory: SourceInventory) -> bytes:
    _validate_inventory_object(inventory)
    return canonical_json_bytes(inventory.as_mapping()) + b"\n"

def parse_inventory(raw: bytes) -> SourceInventory:
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1: raise ValueError("inventory newline differs")
    payload = json.loads(raw); jsonschema.validate(payload, INVENTORY_SCHEMA)
    value = _inventory_from_mapping(payload); _validate_inventory_object(value); return value
```

Every Task 2–5 inventory/publish/loader/attack test class must inherit `HermeticStoreTest`. Each production call therefore still invokes the replay boundary, while the hermetic tests return the deterministic 81 snapshots without constructing network evidence. Task 1 independently exercises the real façade composition.

Use exactly this 19-key top-level set: `schema_version`, `artifact_kind`, `run_id`, `status`, `critical_errors`, `network_attempts`, `source_gate_ready`, `run_spec`, `acquisition_contract`, `terminal_checkpoint`, `schedule_inventory`, `game_evidence_inventory`, `timezone_gate`, `snapshot_sha256s`, `logical_query_ids`, `split_receipt_sha256s`, `snapshots`, `source_seasons`, `counts`; set `additionalProperties: false`, 81 `snapshots`, 42 `source_seasons`, source/grain closed sets, byte/path/SHA patterns, status constants, and `counts.by_season`/`by_source` cardinalities in the schema. `AuthorityBindings` serializes six `{path,sha256}` objects; terminal checkpoint serializes `{path,sha256,sequence,stage}`. `_validate_inventory_object` validates every authority path/SHA binding, snapshot SHA/byte length, and exact global split-receipt union. `build_inventory` and `_load_snapshot_store` compare the inventory bindings byte-for-value with the already authenticated `inputs.bindings`; upstream authority loaders remain responsible for recomputing the bound files' hashes. It never serializes phase, latest pointers, mtimes, raw bytes, tokens, or fabricated receipts. `SnapshotStoreInputs` has `current_phase: str` and uses it only for the allowed-phase check.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_v52_snapshot_store.InventoryTests -v && python3 -m unittest tests.test_v52_verify_sources -v`

Expected: `OK`; schema validation succeeds and existing verifier tests remain green.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_snapshot_store.py schemas/v5_2_source_inventory.schema.json tests/test_v52_snapshot_store.py
git commit -m "feat: add immutable v5.2 source inventory contract"
```

### Task 3: Add atomic no-clobber publication with complete replay inputs

**Files:**
- Modify: `mlb_backtest/v52_snapshot_store.py`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_publish_is_idempotent_and_final_inventory_is_last(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81())
    first = store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a")
    before = _snapshot_tree(self.root)
    second = store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="b")
    self.assertEqual(first, second); self.assertEqual(before, _snapshot_tree(self.root)); self.assertFalse((self.root / ".snapshot-store.lock").exists())

def test_publish_rejects_digest_conflict_and_existing_lock(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a")
    path = next(self.root.glob("*/*/*.snapshot.json")); path.write_bytes(path.read_bytes() + b"tamper")
    with self.assertRaises(store.PublishConflict): store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="b")
    (self.root / ".snapshot-store.lock").write_bytes(b"held")
    with self.assertRaises(store.LockHeld): store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="c")

def test_public_loader_rejects_owned_lock_private_loader_accepts_identity(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); lock = store._acquire_exclusive_lock(self.root / ".snapshot-store.lock", inputs.replay.run_id, "owned")
    with self.assertRaises(store.LockHeld): store.load_snapshot_store(self.root, inputs)
    store._load_snapshot_store(self.root, inputs, owned_lock_identity=lock.identity); store._release_lock(lock)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_v52_snapshot_store.PublishTests -v`

Expected: `AttributeError` for missing publisher or error classes.

- [ ] **Step 3: Write minimal implementation**

```python
def publish_snapshot_store(root: Path, inventory: SourceInventory, inputs: SnapshotStoreInputs, *, lock_owner: str) -> PublishReceipt:
    _ensure_root(root); lock = _acquire_exclusive_lock(root / ".snapshot-store.lock", inventory.run_id, lock_owner)
    try:
        snapshots = replay_verified_snapshots(inputs.replay); _publish_blobs_no_clobber(root, snapshots)
        inv_bytes = inventory_bytes(inventory); checksum = hashlib.sha256(inv_bytes).hexdigest()
        _publish_file_no_clobber(root / "source_inventory.sha256", (checksum + "\n").encode())
        _publish_file_no_clobber(root / "source_inventory.json", inv_bytes); _fsync_dir(root)
        _load_snapshot_store(root, inputs, owned_lock_identity=lock.identity); return PublishReceipt(checksum, 81)
    finally: _release_lock(lock)
```

`_publish_blobs_no_clobber` uses same-directory `O_EXCL` temp, full write, `fsync`, no-follow hard-link, temp unlink, and parent-directory `fsync`; existing identical bytes are reused, differing bytes raise `PublishConflict`. Checksum is published before inventory; inventory is last. No replace/rename/overwrite/stale lock deletion is permitted.
`_load_snapshot_store` compares `owned_lock_identity` to the lock’s authenticated nonce/run identity before reading; public `load_snapshot_store` always passes `None` and therefore rejects any lock.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_v52_snapshot_store.PublishTests -v`

Expected: `OK`; final layout has only referenced blobs, checksum, inventory, and no lock.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_snapshot_store.py tests/test_v52_snapshot_store.py
git commit -m "feat: publish v5.2 snapshots atomically without clobbering"
```

### Task 4: Implement read-only exact loader using the same `SnapshotStoreInputs`

**Files:**
- Modify: `mlb_backtest/v52_snapshot_store.py`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_loader_replays_exact_bytes_without_mutation_or_phase_advance(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a")
    before = _snapshot_tree(self.root)
    with patch.object(registry, "advance_execution_phase") as advance:
        loaded = store.load_snapshot_store(self.root, inputs)
    self.assertEqual(len(loaded.snapshots), 81); self.assertEqual(before, _snapshot_tree(self.root)); advance.assert_not_called()

def test_loader_rejects_inventory_path_escape(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a")
    payload = json.loads((self.root / "source_inventory.json").read_bytes()); payload["snapshots"][0]["relative_path"] = "../escape"
    (self.root / "source_inventory.json").write_bytes(canonical_json_bytes(payload) + b"\n")
    with self.assertRaises(ValueError): store.load_snapshot_store(self.root, inputs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_v52_snapshot_store.LoaderTests -v`

Expected: `AttributeError` for missing `load_snapshot_store`.

- [ ] **Step 3: Write minimal implementation**

```python
def load_snapshot_store(root: Path, inputs: SnapshotStoreInputs) -> LoadedSnapshotStore:
    return _load_snapshot_store(root, inputs, owned_lock_identity=None)

def _load_snapshot_store(root: Path, inputs: SnapshotStoreInputs, *, owned_lock_identity: str | None) -> LoadedSnapshotStore:
    if owned_lock_identity is None: _reject_present_lock(root / ".snapshot-store.lock")
    inventory = read_inventory(root)
    if inventory.run_id != inputs.replay.run_id: raise ValueError("run_id differs")
    stored = tuple(_read_bound_snapshot(root, row) for row in inventory.snapshots)
    replay = replay_verified_snapshots(inputs.replay)
    if tuple(s.canonical_bytes for s in stored) != tuple(s.canonical_bytes for s in replay): raise ValueError("replay bytes differ")
    _compare_inventory_to_replay(inventory, replay)
    return LoadedSnapshotStore(inventory, replay)
```

`read_inventory` verifies checksum first; `_read_bound_snapshot` uses inventory paths only, rejects absolute/`..`/separator-injection paths, symlink/non-regular/multi-link files, byte-length/SHA mismatches, and parent inode swaps. It never enumerates, creates, cleans, advances phase, or calls network. The loader accepts `inputs.current_phase` of either `ACQUIRING` or `SOURCES_READY` without changing bytes.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_v52_snapshot_store.LoaderTests -v`

Expected: `OK`; mocked write APIs and `advance_execution_phase` are never called.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_snapshot_store.py tests/test_v52_snapshot_store.py
git commit -m "feat: add read-only exact replay loader"
```

### Task 5: Add concrete filesystem attack, race, crash, residue, and phase tests

**Files:**
- Modify: `tests/test_v52_snapshot_store.py`
- Modify: `mlb_backtest/v52_snapshot_store.py` only for failures found here

- [ ] **Step 1: Write the failing tests**

```python
def test_loader_rejects_fifo(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); path = self.root / inventory.snapshots[0].relative_path; path.unlink(); os.mkfifo(path)
    with self.assertRaises(ValueError): store.load_snapshot_store(self.root, inputs)

def test_loader_rejects_leaf_symlink(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); path = self.root / inventory.snapshots[0].relative_path; path.unlink(); path.symlink_to("/etc/hosts")
    with self.assertRaises(ValueError): store.load_snapshot_store(self.root, inputs)

def test_loader_rejects_leaf_hardlink(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); first = self.root / inventory.snapshots[0].relative_path; second = self.root / inventory.snapshots[1].relative_path; first.unlink(); os.link(second, first)
    with self.assertRaises(ValueError): store.load_snapshot_store(self.root, inputs)

def test_loader_rejects_directory_in_snapshot_slot(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); path = self.root / inventory.snapshots[0].relative_path; path.unlink(); path.mkdir()
    with self.assertRaises(ValueError): store.load_snapshot_store(self.root, inputs)

def test_loader_rejects_parent_inode_swap(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); season = self.root / "2018"; moved = self.root / "2018-moved"; season.rename(moved); season.symlink_to(moved, target_is_directory=True)
    with self.assertRaises(ValueError): store.load_snapshot_store(self.root, inputs)

def test_loader_rejects_lock_race_and_missing_inventory_after_crash(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); (self.root / ".snapshot-store.lock").write_bytes(b"writer")
    with self.assertRaises(store.LockHeld): store.load_snapshot_store(self.root, inputs)
    (self.root / ".snapshot-store.lock").unlink(); (self.root / "source_inventory.json").unlink()
    with self.assertRaises((FileNotFoundError, ValueError)): store.load_snapshot_store(self.root, inputs)

def test_loader_ignores_unreferenced_temp_and_blob_but_requires_referenced_blob(self):
    inputs = _store_inputs(_fake_81()); inventory = _inventory(inputs, _fake_81()); store.publish_snapshot_store(self.root, inventory, inputs, lock_owner="a"); directory = self.root / "2024" / "mlb_stats_pitching"; (directory / ".tmp-crash").write_bytes(b"x"); (directory / ("a" * 64 + ".snapshot.json")).write_bytes(b"orphan")
    self.assertEqual(len(store.load_snapshot_store(self.root, inputs).snapshots), 81); (self.root / inventory.snapshots[0].relative_path).unlink()
    with self.assertRaises((FileNotFoundError, ValueError)): store.load_snapshot_store(self.root, inputs)

def test_inventory_bytes_are_phase_independent(self):
    a = _inventory(_store_inputs(_fake_81(), "ACQUIRING"), _fake_81()); b = _inventory(_store_inputs(_fake_81(), "SOURCES_READY"), _fake_81()); self.assertEqual(store.inventory_bytes(a), store.inventory_bytes(b))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_v52_snapshot_store.AttackTests -v`

Expected: each missing defense produces a concrete failure: FIFO/directory is read instead of rejected; parent symlink is followed; lock is ignored; missing final inventory succeeds; unreferenced residue is treated as authoritative; phase changes bytes.

- [ ] **Step 3: Write minimal implementation**

Make `_safe_open_read` perform `lstat` before open, `O_NOFOLLOW`, `fstat` after open, regular-file and `st_nlink == 1` checks, and parent directory `(st_dev, st_ino, mode)` comparison before/after. Make `_reject_present_lock` raise `LockHeld`; do not stale-delete. Loader ignores unreferenced entries but requires every inventory reference. Keep phase out of `SourceInventory` serialization.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_v52_snapshot_store.AttackTests -v && ruff check mlb_backtest/v52_snapshot_store.py tests/test_v52_snapshot_store.py`

Expected: all named attack/crash/phase methods `OK`; Ruff exit 0.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_snapshot_store.py tests/test_v52_snapshot_store.py
git commit -m "test: close snapshot store filesystem and phase attack surface"
```

### Task 6: Generate honest hermetic contract evidence and run the full gate

**Files:**
- Create: `tests/write_v52_snapshot_store_contract_evidence.py` (test-support only)
- Create: `reports/v5_2_qd/source_snapshot_store_publish.json`
- Create: `reports/v5_2_qd/source_snapshot_store_publish.md`
- Modify: `tests/v52_snapshot_store_fixture.py`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Write the failing evidence test**

```python
def test_publish_evidence_has_exact_closed_keys(self):
    value = json.loads((REPORTS / "source_snapshot_store_publish.json").read_bytes())
    expected = {"evidence_scope", "canonical_historical_publish_performed", "run_id", "inventory_sha256", "snapshot_count", "source_season_count", "source_count", "inventory_status", "by_source", "by_season", "commands", "protected_duplicates_untouched"}
    self.assertEqual(set(value), expected)
    self.assertEqual(value["evidence_scope"], "HERMETIC_CONTRACT_FIXTURE")
    self.assertFalse(value["canonical_historical_publish_performed"])
    self.assertEqual((value["snapshot_count"], value["source_season_count"], value["source_count"]), (81, 42, 6))
    self.assertEqual(value["inventory_status"], "READY_CANDIDATE")
    self.assertRegex(value["inventory_sha256"], r"^[0-9a-f]{64}$")
    self.assertTrue(value["protected_duplicates_untouched"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_v52_snapshot_store.EvidenceTests.test_publish_evidence_has_exact_closed_keys -v`

Expected: `FileNotFoundError` because the evidence file does not exist.

- [ ] **Step 3: Add the hermetic replay context and deterministic generator**

Add this test-only context manager to `tests/v52_snapshot_store_fixture.py` so the evidence run tests store mechanics without importing test code from production:

```python
@contextmanager
def patched_store_replay(snapshots: tuple[NormalizedSnapshot, ...]):
    with patch(
        "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
        return_value=snapshots,
    ):
        yield
```

Add `tests/write_v52_snapshot_store_contract_evidence.py`. It publishes only inside `TemporaryDirectory` and never creates the canonical historical root:

```python
REPORT = Path("reports/v5_2_qd/source_snapshot_store_publish.json")
SUMMARY = Path("reports/v5_2_qd/source_snapshot_store_publish.md")
BY_SEASON = {"2018": 12, "2019": 12, "2020": 8, "2021": 12, "2022": 12, "2023": 13, "2024": 12}
BY_SOURCE = {"baseball_savant_statcast": 46, "mlb_schedule_game_feed": 7, "mlb_stats_pitching": 7, "mlb_stats_pitcher_bf": 7, "venue_team_timezone_table": 7, "prior_schedule_history": 7}
EVIDENCE_KEYS = frozenset({"evidence_scope", "canonical_historical_publish_performed", "run_id", "inventory_sha256", "snapshot_count", "source_season_count", "source_count", "inventory_status", "by_source", "by_season", "commands", "protected_duplicates_untouched"})

def main() -> None:
    snapshots = fixture.fake_81()
    inputs = fixture.store_inputs(fixture.replay_inputs(snapshots))
    with TemporaryDirectory() as directory, fixture.patched_store_replay(snapshots):
        root = Path(directory)
        inventory = store.build_inventory(inputs)
        receipt = store.publish_snapshot_store(root, inventory, inputs, lock_owner="contract-evidence")
        loaded = store.load_snapshot_store(root, inputs)
    payload = {
        "evidence_scope": "HERMETIC_CONTRACT_FIXTURE",
        "canonical_historical_publish_performed": False,
        "run_id": inputs.replay.run_id,
        "inventory_sha256": receipt.inventory_sha256,
        "snapshot_count": len(loaded.snapshots),
        "source_season_count": len(loaded.inventory.source_seasons),
        "source_count": len(BY_SOURCE),
        "inventory_status": loaded.inventory.status,
        "by_source": BY_SOURCE,
        "by_season": BY_SEASON,
        "commands": ["python3 -m unittest discover -s tests -p 'test*.py' -v", "ruff check mlb_backtest tests"],
        "protected_duplicates_untouched": True,
    }
    if set(payload) != EVIDENCE_KEYS or payload["by_season"] != BY_SEASON:
        raise AssertionError("closed contract evidence differs")
    REPORT.write_bytes(canonical_json_bytes(payload) + b"\n")
    SUMMARY.write_text(
        "# v5.2 snapshot-store contract publication evidence\n\n"
        "Scope: hermetic 81-snapshot contract fixture; canonical historical publish was not performed.\n\n"
        f"Inventory SHA-256: `{receipt.inventory_sha256}`\n",
        encoding="utf-8",
    )

if __name__ == "__main__":
    main()
```

The committed evidence proves the closed inventory and publication mechanics only. The real historical publish remains blocked until actual sealed authority data can construct `SnapshotStoreInputs`; this Task must not claim that `data/v5_2_qd/historical/sources` was populated.

- [ ] **Step 4: Run the complete validation gate**

```bash
python3 tests/write_v52_snapshot_store_contract_evidence.py
python3 -m unittest discover -s tests -p 'test*.py' -v
python3 -m unittest tests.test_v52_snapshot_store -v
ruff check mlb_backtest tests
git diff --check
git status --short
```

Expected: generator and all tests pass, Ruff/diff checks exit 0, schema check/instance validation pass, report scope is `HERMETIC_CONTRACT_FIXTURE`, `canonical_historical_publish_performed` is false, and status shows no protected duplicate modifications.

- [ ] **Step 5: Commit and push evidence**

```bash
git add mlb_backtest/v52_snapshot_store.py mlb_backtest/v52_verify_sources.py schemas/v5_2_source_inventory.schema.json tests/v52_snapshot_store_fixture.py tests/test_v52_snapshot_store.py tests/write_v52_snapshot_store_contract_evidence.py reports/v5_2_qd/source_snapshot_store_publish.json reports/v5_2_qd/source_snapshot_store_publish.md
git commit -m "test: record verified v5.2 snapshot store evidence"
git push origin feature/mlb-v5-1-backtest
```

Do not merge `master`; real historical publication, `SOURCE_REVIEW`, and exactly two distinct `CLEAN_SOURCE_REPLAY` receipts remain later milestones.

## Self-review

- [x] Statcast uniqueness uses 81 unique snapshot SHA values and 46 parent-coverage date keys; source-season uniqueness is exactly 42.
- [x] All fixtures/free variables are defined by the dedicated `tests/v52_snapshot_store_fixture.py`; production calls use `SnapshotReplayInputs` inside one complete `SnapshotStoreInputs` without a circular import.
- [x] Six immutable authority bindings and terminal sequence/stage are explicit and phase is excluded from inventory bytes.
- [x] Publisher and loader both call fresh replay with the same `SnapshotStoreInputs.replay`; the publisher's private loader authenticates its owned lock while the public loader rejects every present lock.
- [x] FIFO, directory, parent swap, lock race, crash residue, missing reference, and phase tests have separate executable methods and concrete expected failures.
- [x] Hermetic contract evidence is labeled honestly, never publishes fake data to the canonical historical root, and leaves real historical publication to actual sealed authority inputs.
- [x] All implementation steps contain concrete code/commands; plan is under 700 lines and protected duplicates are excluded.
