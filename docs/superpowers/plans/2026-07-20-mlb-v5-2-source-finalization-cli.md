# MLB v5.2-QD Source Finalization CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a network-free staged CLI and shared service that authenticates completed v5.2 historical acquisition evidence, publishes the immutable source inventory, records explicit review and clean-replay receipts, and advances only a fully bound `SOURCES_READY` gate.

**Architecture:** `v52_verify_sources.py` gains one public read-only input factory over its existing verified loaders; `v52_source_orchestration.py` owns context construction and all finalization operations; `v52_source_finalize.py` owns parsing and output rendering only. The execution registry remains the non-bypassable phase authority and independently exact-validates the same review/replay schemas before sealing the explicitly selected receipt set.

**Tech Stack:** Python 3.11, frozen dataclasses, `argparse`, `pathlib`/POSIX no-follow descriptors, canonical JSON plus SHA-256, UUIDv4 from `secrets`, JSON Schema Draft 2020-12, `unittest`, Ruff, and Git import-closure verification.

---

## File map and boundaries

- Create `mlb_backtest/v52_source_orchestration.py`: frozen common/context/request/report types, safe path and canonical receipt helpers, read-only status/preflight, publish/review/replay/advance services, and stable error classification.
- Create `mlb_backtest/v52_source_finalize.py`: six-subcommand parser, service dispatch, canonical JSON/human rendering, and exit-code mapping; no evidence parsing, network client, or registry mutation logic.
- Modify `mlb_backtest/v52_verify_sources.py`: expose `load_snapshot_replay_inputs(root, run_spec, *, acquisition_contract_path)`, constructed only from terminal registry evidence and existing read-only authority loaders.
- Modify `mlb_backtest/execution_registry.py`: exact schema enforcement for source receipts, query-ID document rebinding, reviewer role enforcement, UUIDv4/distinct replay execution checks, and expanded bound execution code.
- Create `schemas/v5_2_source_review_receipt.schema.json`: closed source-review receipt contract.
- Create `schemas/v5_2_clean_source_replay_receipt.schema.json`: closed clean-replay receipt contract.
- Create `tests/test_v52_source_finalize.py`: service/CLI unit, path, receipt, concurrency, output, and miniature end-to-end tests.
- Modify `tests/test_v52_verify_sources.py`: public input-factory tests.
- Modify `tests/test_execution_registry.py`: schema fixtures, direct-registry bypass tests, persisted-load tests, and exact import-closure assertions.
- Reuse `tests/v52_snapshot_store_fixture.py`; production code must never import it.
- Create `reports/v5_2_qd/source_finalization_cli_implementation.md`: implementation-only verification record; it must explicitly disclaim canonical historical publication.
- Never touch or stage the protected untracked duplicates `mlb_backtest/v52_query_plan 2.py`, `tests/test_v52_query_plan 2.py`, `reports/v5_2_qd/walkforward_500_preflight 2.md`, or `schemas/v5_2_preflight.schema 2.json`.

All commands below run from `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`.

### Task 1: Freeze the two exact source-evidence receipt schemas

**Files:**
- Create: `schemas/v5_2_source_review_receipt.schema.json`
- Create: `schemas/v5_2_clean_source_replay_receipt.schema.json`
- Modify: `mlb_backtest/execution_registry.py:108-149`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Write failing schema-closure tests**

Add a table-driven test that loads both paths, calls `Draft202012Validator.check_schema`, requires canonical one-line JSON plus newline, and checks these exact required keys:

```python
def test_source_evidence_receipt_schemas_are_closed_and_canonical(self) -> None:
    expected = {
        "v5_2_source_review_receipt.schema.json": {
            "schema_version", "receipt_kind", "run_id", "critical_errors",
            "reviewer_identity", "reviewer_role", "decision",
            "reviewed_query_ids", "query_ids_document", "inventory_sha256",
            "snapshot_sha256s", "run_spec", "acquisition_contract",
            "git_head", "occurred_at_utc",
        },
        "v5_2_clean_source_replay_receipt.schema.json": {
            "schema_version", "receipt_kind", "run_id", "critical_errors",
            "execution_id", "status", "network_attempts",
            "reviewed_query_ids", "inventory_sha256", "snapshot_sha256s",
            "run_spec", "acquisition_contract", "git_head",
            "recorded_at_utc",
        },
    }
    for name, keys in expected.items():
        path = ROOT / "schemas" / name
        payload = json.loads(path.read_bytes())
        jsonschema.Draft202012Validator.check_schema(payload)
        self.assertFalse(payload["additionalProperties"])
        self.assertEqual(set(payload["properties"]), keys)
        self.assertEqual(set(payload["required"]), keys)
        self.assertEqual(path.read_bytes(), canonical_json_bytes(payload) + b"\n")
```

In the same change, append both new schema paths to production
`_BOUND_EXECUTION_CODE` and to the test's `BOUND_SCHEMA_PATHS`, so `make_repo`
copies the authoritative schemas into every temporary registry repository and
the exact bound-set test remains green after this task.

- [ ] **Step 2: Run the test and verify the schemas are missing**

Run: `python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest.test_source_evidence_receipt_schemas_are_closed_and_canonical -v`

Expected: `FileNotFoundError` for `v5_2_source_review_receipt.schema.json`.

- [ ] **Step 3: Create the complete review schema**

Use Draft 2020-12, `additionalProperties: false`, and the exact keys above. Both schemas use these complete definitions:

```json
{
  "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
  "gitHead": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
  "timestamp": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z$"},
  "binding": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "path": {"type": "string", "minLength": 1, "pattern": "^(?!/)(?!.*(?:^|/)\\.\\.(?:/|$))(?!.*\\\\).+$"},
      "sha256": {"$ref": "#/$defs/sha256"}
    },
    "required": ["path", "sha256"]
  }
}
```

Install this object under `$defs` and require:

```json
{
  "schema_version": {"const": 1},
  "receipt_kind": {"const": "SOURCE_REVIEW"},
  "run_id": {"const": "historical_walkforward_500"},
  "critical_errors": {"const": []},
  "reviewer_identity": {"const": "independent-source-auditor"},
  "reviewer_role": {"const": "independent-source-auditor"},
  "decision": {"enum": ["APPROVED", "REJECTED"]},
  "reviewed_query_ids": {"type": "array", "items": {"$ref": "#/$defs/sha256"}, "minItems": 1, "uniqueItems": true},
  "query_ids_document": {"$ref": "#/$defs/binding"},
  "inventory_sha256": {"$ref": "#/$defs/sha256"},
  "snapshot_sha256s": {"type": "array", "items": {"$ref": "#/$defs/sha256"}, "minItems": 1, "uniqueItems": true},
  "run_spec": {"$ref": "#/$defs/binding"},
  "acquisition_contract": {"$ref": "#/$defs/binding"},
  "git_head": {"$ref": "#/$defs/gitHead"},
  "occurred_at_utc": {"$ref": "#/$defs/timestamp"}
}
```

`binding.path` must be a safe non-absolute slash path with no `..` segment, and `binding.sha256` must use `$defs.sha256`. The implementation task must serialize the full schema with `canonical_json_bytes(schema) + b"\n"`, not pretty-printed JSON.

- [ ] **Step 4: Create the complete replay schema**

Use the same definitions and exact-key rule. Require `receipt_kind == "CLEAN_SOURCE_REPLAY"`, `critical_errors == []`, `status == "PASS"`, `network_attempts == 0`, and:

```json
{
  "execution_id": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"},
  "reviewed_query_ids": {"type": "array", "items": {"$ref": "#/$defs/sha256"}, "minItems": 1, "uniqueItems": true},
  "inventory_sha256": {"$ref": "#/$defs/sha256"},
  "snapshot_sha256s": {"type": "array", "items": {"$ref": "#/$defs/sha256"}, "minItems": 1, "uniqueItems": true},
  "run_spec": {"$ref": "#/$defs/binding"},
  "acquisition_contract": {"$ref": "#/$defs/binding"},
  "git_head": {"$ref": "#/$defs/gitHead"},
  "recorded_at_utc": {"$ref": "#/$defs/timestamp"}
}
```

- [ ] **Step 5: Verify and commit the schema contract**

Run: `python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest.test_source_evidence_receipt_schemas_are_closed_and_canonical -v && ruff check tests/test_execution_registry.py && git diff --check`

Expected: test `OK`, Ruff exit 0, diff check exit 0.

```bash
git add schemas/v5_2_source_review_receipt.schema.json schemas/v5_2_clean_source_replay_receipt.schema.json mlb_backtest/execution_registry.py tests/test_execution_registry.py
git commit -m "feat: freeze v5.2 source evidence receipt schemas"
```

### Task 2: Make the execution registry enforce the receipt contracts directly

**Files:**
- Modify: `mlb_backtest/execution_registry.py:616-729`
- Modify: `tests/test_execution_registry.py:401-457,4178-4974`

- [ ] **Step 1: Upgrade the registry fixture and write bypass tests**

Change `write_real_source_evidence` to use SHA-256 query IDs, a canonical query-ID selection file, the frozen reviewer role, and distinct UUIDv4 strings:

```python
def run_spec_binding(self, root: Path) -> dict[str, str]:
    path = root / "protocols/v5_2_qd/historical_walkforward_500.json"
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}

def acquisition_contract_binding(self, root: Path) -> dict[str, str]:
    path = root / "protocols/v5_2_qd/historical_source_acquisition.json"
    return {"path": path.relative_to(root).as_posix(), "sha256": _sha256(path)}

def git_head(self, root: Path) -> str:
    return subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

query_ids = ["1" * 64, "2" * 64]
selection = self.write_artifact(
    root,
    "audit/v5_2_qd/historical/acquisition/source_reviews/query_ids.json",
    {"query_ids": query_ids},
)
common = {
    "schema_version": 1,
    "run_id": "historical_walkforward_500",
    "critical_errors": [],
    "reviewed_query_ids": query_ids,
    "inventory_sha256": _sha256(inventory),
    "snapshot_sha256s": snapshots,
    "run_spec": self.run_spec_binding(root),
    "acquisition_contract": self.acquisition_contract_binding(root),
    "git_head": self.git_head(root),
}
review_payload = {
    **common,
    "receipt_kind": "SOURCE_REVIEW",
    "reviewer_identity": "independent-source-auditor",
    "reviewer_role": "independent-source-auditor",
    "decision": "APPROVED",
    "query_ids_document": {
        "path": selection.relative_to(root).as_posix(),
        "sha256": _sha256(selection),
    },
    "occurred_at_utc": "2026-07-20T00:00:00.000000Z",
}
replay_payloads = tuple(
    {
        **common,
        "receipt_kind": "CLEAN_SOURCE_REPLAY",
        "execution_id": execution_id,
        "status": "PASS",
        "network_attempts": 0,
        "recorded_at_utc": timestamp,
    }
    for execution_id, timestamp in (
        ("11111111-1111-4111-8111-111111111111", "2026-07-20T00:00:01.000000Z"),
        ("22222222-2222-4222-8222-222222222222", "2026-07-20T00:00:02.000000Z"),
    )
)
review = self.write_artifact(
    root,
    "audit/v5_2_qd/historical/acquisition/source_reviews/review.json",
    review_payload,
)
replays = tuple(
    self.write_artifact(
        root,
        f"audit/v5_2_qd/historical/acquisition/source_replays/replay-{index}.json",
        payload,
    )
    for index, payload in enumerate(replay_payloads, start=1)
)
```

Add direct `advance_execution_phase` cases for an extra replay key, missing/wrong reviewer role, missing/malformed/non-v4 UUID, duplicated execution IDs, tampered query-ID document, and document hash mismatch. Each case must assert `ValueError`, unchanged `ACQUIRING` state, and one `SOURCE_GATE_EVIDENCE_INVALID` quarantine receipt.

- [ ] **Step 2: Run the bypass tests and verify current loose validation fails them**

Run: `python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest.test_source_receipt_schema_cannot_be_bypassed tests.test_execution_registry.ExecutionRegistryTest.test_duplicate_replay_execution_id_cannot_advance tests.test_execution_registry.ExecutionRegistryTest.test_review_query_id_document_is_rebound -v`

Expected: failures because the current registry ignores the new fields or accepts duplicated execution IDs.

- [ ] **Step 3: Add exact schema loading and common authority validation**

In `execution_registry.py`, load each schema from the repository root with the existing safe artifact snapshot reader, require canonical JSON bytes, run `Draft202012Validator`, and rebind the schema before returning. Extend `_source_receipt_payload` to accept `root`, the expected `git_head`, and expected RunSpec/acquisition-contract bindings:

```python
def _validate_bound_schema(
    root: Path, schema_name: str, payload: Mapping[str, object]
) -> None:
    schema_path = root / "schemas" / schema_name
    snapshot = _capture_artifact(root, schema_path, "source receipt schema")
    schema = _canonical_object(snapshot.contents, "source receipt schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    except (SchemaError, ValidationError) as exc:
        raise ValueError("source evidence receipt schema validation failed") from exc
    _verify_artifact_snapshot(root, snapshot, "source receipt schema")

def _source_receipt_payload(
    root: Path,
    contents: bytes,
    *,
    path: str,
    run_id: str,
    inventory_sha256: str,
    snapshot_sha256s: tuple[str, ...],
    git_head: str,
    run_spec: Mapping[str, object],
    acquisition_contract: Mapping[str, object],
) -> tuple[str, tuple[str, ...], str | None]:
    payload = _canonical_object(contents, "source evidence receipt")
    schema_name = (
        "v5_2_source_review_receipt.schema.json"
        if payload.get("receipt_kind") == "SOURCE_REVIEW"
        else "v5_2_clean_source_replay_receipt.schema.json"
    )
    _validate_bound_schema(root, schema_name, payload)
    if payload["run_id"] != run_id or payload["inventory_sha256"] != inventory_sha256:
        raise ValueError("source evidence receipt inventory binding failed")
    if tuple(payload["snapshot_sha256s"]) != snapshot_sha256s:
        raise ValueError("source evidence receipt snapshots differ")
    if payload["git_head"] != git_head:
        raise ValueError("source evidence receipt git binding differs")
    if payload["run_spec"] != run_spec or payload["acquisition_contract"] != acquisition_contract:
        raise ValueError("source evidence receipt protocol binding differs")
    queries = _sorted_string_list(payload["reviewed_query_ids"], "reviewed query IDs")
    if payload["receipt_kind"] == "SOURCE_REVIEW":
        if not path.startswith(_SOURCE_REVIEW_PREFIX) or payload["decision"] != "APPROVED":
            raise ValueError("independent source review is not approved")
        relative = _validate_binding_value(
            payload["query_ids_document"], "review query IDs document"
        )
        if not str(relative["path"]).startswith(_SOURCE_REVIEW_PREFIX):
            raise ValueError("review query ID document is outside review namespace")
        document = _capture_artifact(
            root, root / str(relative["path"]), "review query IDs document"
        )
        if document.binding != dict(relative):
            raise ValueError("review query ID document binding differs")
        selected = _canonical_object(document.contents, "review query IDs document")
        if set(selected) != {"query_ids"} or tuple(selected["query_ids"]) != queries:
            raise ValueError("review query ID document selection differs")
        _verify_artifact_snapshot(root, document, "review query IDs document")
        return "SOURCE_REVIEW", queries, None
    if not path.startswith(_SOURCE_REPLAY_PREFIX):
        raise ValueError("clean source replay is outside replay namespace")
    execution_id = str(payload["execution_id"])
    parsed = uuid.UUID(execution_id)
    if parsed.version != 4 or str(parsed) != execution_id:
        raise ValueError("clean replay execution ID is not canonical UUIDv4")
    return "CLEAN_SOURCE_REPLAY", queries, execution_id
```

For reviews, capture `query_ids_document.path`, compare its digest, parse an exact canonical object whose only key is `query_ids`, and require its array equals `reviewed_query_ids`. For replay UUIDs, require `uuid.UUID(value).version == 4` and `str(uuid.UUID(value)) == value` in addition to the schema pattern.

- [ ] **Step 4: Enforce selected-set invariants in `_validate_sources_ready_evidence`**

Pass the base receipt bindings through `_validate_phase_evidence`. Track replay execution IDs alongside paths and hashes:

```python
replay_execution_ids: set[str] = set()
kind, receipt_queries, execution_id = _source_receipt_payload(
    root,
    artifact.contents,
    path=artifact.path,
    run_id=run_id,
    inventory_sha256=inventory.sha256,
    snapshot_sha256s=snapshots,
    git_head=str(base["git_head"]),
    run_spec=_validate_binding_value(base["run_spec"], "run spec"),
    acquisition_contract=_validate_binding_value(
        base["acquisition_contract"], "acquisition contract"
    ),
)
if kind == "CLEAN_SOURCE_REPLAY":
    if execution_id in replay_execution_ids:
        raise ValueError("clean source replay execution ID is duplicated")
    replay_execution_ids.add(execution_id)
if reviews < 1 or len(replay_paths) != 2 or len(replay_execution_ids) != 2:
    raise ValueError("SOURCES_READY requires an approved review and two clean replays")
```

Keep the public `advance_execution_phase(root, run_spec, *, expected_phase, next_phase, inventory_path, validation_receipts)` signature unchanged. The immutable phase receipt's `validation_receipts` remains the canonical explicit selection record.

- [ ] **Step 5: Run registry tests and commit**

Run: `python3 -m unittest tests.test_execution_registry -v`

Expected: all registry tests `OK`, including stored-receipt reload and quarantine behavior.

```bash
git add mlb_backtest/execution_registry.py tests/test_execution_registry.py
git commit -m "fix: close v5.2 source gate receipt validation"
```

### Task 3: Expose a verified read-only replay-input factory

**Files:**
- Modify: `mlb_backtest/v52_verify_sources.py`
- Modify: `tests/test_v52_verify_sources.py`

- [ ] **Step 1: Write failing public-factory tests**

Patch the existing verified loaders and assert the new factory uses read-only progress and `V52EvidenceStore.open_read_only`, selects exactly one `STATIC_SCHEDULES` batch, and returns frozen `SnapshotReplayInputs`:

```python
@patch("mlb_backtest.v52_verify_sources.V52EvidenceStore.open_read_only")
@patch("mlb_backtest.v52_verify_sources.load_verified_timezone_gate")
@patch("mlb_backtest.v52_verify_sources.load_verified_game_evidence_inventory")
@patch("mlb_backtest.v52_verify_sources.load_verified_schedule_anchor_inventory")
@patch("mlb_backtest.v52_verify_sources._load_terminal_query_graph")
def test_load_snapshot_replay_inputs_uses_only_verified_readers(
    self, load_graph, load_schedule, load_games, load_timezone, open_store
) -> None:
    result = verifier.load_snapshot_replay_inputs(
        self.root,
        self.run_spec,
        acquisition_contract_path=self.contract_path,
    )
    self.assertIsInstance(result, verifier.SnapshotReplayInputs)
    open_store.assert_called_once_with(self.evidence_root)
    self.assertTrue(load_schedule.call_args.kwargs["read_only_progress"])
    self.assertTrue(load_games.call_args.kwargs["read_only_progress"])
    self.assertTrue(load_timezone.call_args.kwargs["read_only_progress"])
```

Add failures for zero or two `STATIC_SCHEDULES` batches, an unsafe raw namespace, and changed authority on a second load.

- [ ] **Step 2: Run the new tests and verify the symbol is absent**

Run: `python3 -m unittest tests.test_v52_verify_sources.SnapshotReplayInputFactoryTests -v`

Expected: `AttributeError` for `load_snapshot_replay_inputs`.

- [ ] **Step 3: Implement the minimal public factory**

Import the three public authority loaders and implement:

```python
def load_snapshot_replay_inputs(
    root: Path,
    run_spec: RunSpec,
    *,
    acquisition_contract_path: Path,
) -> SnapshotReplayInputs:
    graph = _load_terminal_query_graph(root, run_spec)
    static_batches = tuple(batch for batch in graph.query_batches if batch.stage == "STATIC_SCHEDULES")
    if len(static_batches) != 1:
        raise ValueError("source verification requires one STATIC_SCHEDULES batch")
    raw_relative = _safe_raw_namespace(run_spec)
    evidence_root = root / raw_relative
    schedule = load_verified_schedule_anchor_inventory(
        root, run_spec,
        acquisition_contract_path=acquisition_contract_path,
        static_batch_id=static_batches[0].batch_id,
        evidence_root=evidence_root,
        read_only_progress=True,
    )
    timezone = load_verified_timezone_gate(root, read_only_progress=True)
    games = load_verified_game_evidence_inventory(root, read_only_progress=True)
    store = V52EvidenceStore.open_read_only(evidence_root)
    records = _recompute_verified_query_records(store, graph.completed_queries)
    game_snapshots = _build_game_normalized_snapshots(
        run_spec.run_id, records, games, timezone, schedule
    )
    return SnapshotReplayInputs(
        run_spec.run_id, graph, records, game_snapshots, games, timezone, schedule
    )
```

`_safe_raw_namespace` must reject absolute paths, backslashes, empty values, and `..` parts without resolving through a symlink.

- [ ] **Step 4: Verify factory and existing snapshot reducers**

Run: `python3 -m unittest tests.test_v52_verify_sources tests.test_v52_verify_statcast_snapshots tests.test_v52_verify_reference_snapshots -v && ruff check mlb_backtest/v52_verify_sources.py tests/test_v52_verify_sources.py`

Expected: all tests `OK`, Ruff exit 0.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_verify_sources.py tests/test_v52_verify_sources.py
git commit -m "feat: load verified v5.2 snapshot replay inputs"
```

### Task 4: Add frozen orchestration types and the read-only context factory

**Files:**
- Create: `mlb_backtest/v52_source_orchestration.py`
- Create: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Write failing type, absent-state, and path tests**

Test frozen instances, canonical defaults, absent registry reporting without directory creation, symlink-ancestor rejection, and absolute path mismatch:

```python
def tree_snapshot(root: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.lstat().st_mode,
                path.lstat().st_ino,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            )
            for path in root.rglob("*")
        )
    )

def test_absent_status_context_is_read_only(self) -> None:
    before = tree_snapshot(self.root)
    context = orchestration.build_context(orchestration.CommonArgs(root=self.root))
    self.assertEqual(context.phase, "ABSENT")
    self.assertIsNone(context.registry)
    self.assertIsNone(context.snapshot_inputs)
    self.assertIn("REGISTRY_ABSENT", {item.code for item in context.blockers})
    self.assertEqual(tree_snapshot(self.root), before)

def test_context_types_are_frozen(self) -> None:
    context = orchestration.FinalizationContext.absent(self.root, ())
    with self.assertRaises(FrozenInstanceError):
        context.phase = "ACQUIRING"
```

- [ ] **Step 2: Run the tests and verify the module is absent**

Run: `python3 -m unittest tests.test_v52_source_finalize.ContextTests -v`

Expected: `ImportError` for `mlb_backtest.v52_source_orchestration`.

- [ ] **Step 3: Define the exact public service types**

```python
@dataclass(frozen=True)
class CommonArgs:
    root: Path = Path(".")
    run_spec: Path = Path("protocols/v5_2_qd/historical_walkforward_500.json")
    acquisition_contract: Path = Path("protocols/v5_2_qd/historical_source_acquisition.json")
    protected_inventory: Path = Path("audit/v5_1_bt/protected_raw_inventory.json")

@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    paths: tuple[str, ...] = ()

@dataclass(frozen=True)
class FinalizationContext:
    root: Path
    run_spec_path: Path
    acquisition_contract_path: Path
    protected_inventory_path: Path
    run_spec: RunSpec | None
    registry: ExecutionRegistryState | None
    checkpoint: AcquisitionCheckpoint | None
    snapshot_inputs: SnapshotStoreInputs | None
    inventory: SourceInventory | None
    phase: str
    git_head: str | None
    blockers: tuple[Blocker, ...]

    @classmethod
    def absent(cls, root: Path, blockers: tuple[Blocker, ...]) -> "FinalizationContext":
        return cls(
            root=root,
            run_spec_path=root / CommonArgs.run_spec,
            acquisition_contract_path=root / CommonArgs.acquisition_contract,
            protected_inventory_path=root / CommonArgs.protected_inventory,
            run_spec=None,
            registry=None,
            checkpoint=None,
            snapshot_inputs=None,
            inventory=None,
            phase="ABSENT",
            git_head=None,
            blockers=blockers,
        )

@dataclass(frozen=True)
class StatusReport:
    phase: str
    checkpoint: Mapping[str, object] | None
    publish_ready: bool
    review_ready: bool
    replay_ready: bool
    advance_ready: bool
    blockers: tuple[Blocker, ...]

@dataclass(frozen=True)
class PreflightReport:
    phase: str
    status: Literal["PASS", "BLOCKED"]
    inventory_sha256: str | None
    snapshot_count: int
    network_attempts: Literal[0]
    blockers: tuple[Blocker, ...]
```

Also define `FinalizationError(error_code, message, paths, exit_code)` with exit codes 3–7 from the design; never use free-form exception text as the machine error code.

```python
class FinalizationError(Exception):
    def __init__(
        self,
        error_code: str,
        message: str,
        paths: Sequence[str],
        exit_code: int,
    ) -> None:
        if exit_code not in {3, 4, 5, 6, 7}:
            raise ValueError("finalization error exit code is invalid")
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.paths = tuple(paths)
        self.exit_code = exit_code

def _require_mutable_context(
    context: FinalizationContext, *, phase: str | frozenset[str]
) -> FinalizationContext:
    allowed = frozenset({phase}) if isinstance(phase, str) else phase
    if context.phase not in allowed:
        names = ",".join(sorted(allowed))
        raise FinalizationError("PHASE_BLOCKED", f"phase must be one of {names}", (), 4)
    if context.blockers:
        raise FinalizationError("AUTHORITY_BLOCKED", "finalization context is blocked", (), 4)
    required = (
        context.run_spec, context.registry, context.checkpoint,
        context.snapshot_inputs, context.inventory, context.git_head,
    )
    if any(value is None for value in required):
        raise FinalizationError("AUTHORITY_MISSING", "finalization authority is missing", (), 3)
    return context
```

Define these internal helpers in the same module so later tasks use one stable
path/binding vocabulary:

```python
def _trusted_root(value: Path) -> Path:
    root = v52_timezone_review._trusted_root(value)
    descriptor = v52_timezone_review._open_root(root)
    os.close(descriptor)
    return root

def _relative_artifact(root: Path, value: Path, label: str) -> tuple[str, Path]:
    relative = v52_timezone_review._relative(root, value, label)
    return relative.as_posix(), root / relative

def _read_explicit_artifact(root: Path, value: Path, label: str) -> tuple[str, bytes]:
    relative, raw = v52_timezone_review._read_safe(root, value, label)
    return relative.as_posix(), raw

def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result

def _canonical_object(raw: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", f"{label} is not JSON", (), 3) from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != raw:
        raise FinalizationError("AUTHORITY_MALFORMED", f"{label} is not canonical", (), 3)
    return value

def _binding(root: Path, value: Path) -> AuthorityBinding:
    relative, raw = _read_explicit_artifact(root, value, "authority")
    return AuthorityBinding(relative, hashlib.sha256(raw).hexdigest())

def _verified_binding(root: Path, value: Path, expected_sha256: str) -> AuthorityBinding:
    binding = _binding(root, value)
    if binding.sha256 != expected_sha256:
        raise FinalizationError("AUTHORITY_CHANGED", "verified binding changed", (binding.path,), 5)
    return binding

def _source_root(root: Path) -> Path:
    return root / "data/v5_2_qd/historical/sources"
```

The implementation of the first six helpers must use descriptor-relative,
no-follow, regular-single-link reads with identity-before/after checks, return
repository-relative slash paths, and reject absolute escapes, backslashes,
symlink ancestors, hardlinks, and special files. These are complete signatures;
later tasks must extend their bodies rather than introduce differently named
path readers.

- [ ] **Step 4: Implement safe `build_context`**

Canonicalize the root by walking ancestors with `lstat`; do not use `resolve()` as the trust check. Authenticate the RunSpec and static contract, protected inventory plus membership snapshot, registry state, terminal checkpoint, public replay-input factory, and snapshot-store bindings. Catch only the expected absent registry/checkpoint conditions into blockers for `status`/`preflight`; malformed or tampered authorities remain explicit blockers and are never synthesized.

The successful binding construction must be:

```python
bindings = AuthorityBindings(
    run_spec=_binding(root, run_spec_path),
    acquisition_contract=_binding(root, contract_path),
    terminal_checkpoint=TerminalCheckpointBinding(
        checkpoint_path, checkpoint.receipt_sha256, checkpoint.sequence, "REMAINING"
    ),
    schedule_inventory=_verified_binding(root, replay.schedule_inventory.path, replay.schedule_inventory.sha256),
    game_evidence_inventory=_verified_binding(root, replay.game_inventory.path, replay.game_inventory.sha256),
    timezone_gate=_verified_binding(root, timezone_seal_path, replay.timezone_gate.seal_sha256),
)
inputs = SnapshotStoreInputs(replay, bindings, registry.phase)
inventory = build_inventory(inputs)
```

Perform final RunSpec, contract, protected membership, registry, checkpoint, and inventory-byte rebinds before returning.

- [ ] **Step 5: Verify context tests and commit**

Run: `python3 -m unittest tests.test_v52_source_finalize.ContextTests -v && ruff check mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py && git diff --check`

Expected: `OK`, Ruff exit 0, no finalization directory created by absent-state tests.

```bash
git add mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git commit -m "feat: authenticate v5.2 finalization context"
```

### Task 5: Implement read-only status and preflight

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `mlb_backtest/execution_registry.py:3685-3848`
- Modify: `tests/test_v52_source_finalize.py`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Write status/preflight tests**

Cover absent, partial, valid `ACQUIRING`, already `SOURCES_READY`, stale checkpoint, tampered inventory, and a filesystem/network write trap. Require advisory candidate receipt counts but no selected receipt paths:

```python
def test_status_never_selects_receipts_or_infers_phase(self) -> None:
    report = orchestration.read_status(self.context)
    self.assertEqual(report.phase, "ACQUIRING")
    self.assertNotIn("selected_review_receipts", dataclasses.asdict(report))
    self.assertNotIn("selected_replay_receipts", dataclasses.asdict(report))

def test_preflight_is_read_only_and_returns_recomputed_digest(self) -> None:
    before = tree_snapshot(self.root)
    report = orchestration.run_preflight(self.context)
    self.assertEqual(report.status, "PASS")
    self.assertEqual(
        report.inventory_sha256,
        hashlib.sha256(inventory_bytes(self.context.inventory)).hexdigest(),
    )
    self.assertEqual(report.network_attempts, 0)
    self.assertEqual(tree_snapshot(self.root), before)
```

- [ ] **Step 2: Run tests and verify the service functions are missing**

Run: `python3 -m unittest tests.test_v52_source_finalize.StatusAndPreflightTests -v`

Expected: `AttributeError` for `read_status` or `run_preflight`.

- [ ] **Step 3: Implement status and preflight reports**

`read_status` may enumerate candidate receipts for diagnostics with no-follow bounded reads, but must never return an implicitly selected set. `run_preflight` must call `build_inventory`, compare canonical bytes twice across a final authority rebind, and return `BLOCKED` rather than mutate on any blocker:

```python
def run_preflight(context: FinalizationContext) -> PreflightReport:
    if context.blockers or context.inventory is None:
        return PreflightReport(context.phase, "BLOCKED", None, 0, 0, context.blockers)
    first = inventory_bytes(context.inventory)
    rebound = _rebuild_context_inventory(context)
    second = inventory_bytes(rebound)
    if first != second:
        raise FinalizationError("AUTHORITY_CHANGED", "inventory changed during preflight", (), 5)
    return PreflightReport(
        context.phase, "PASS", hashlib.sha256(first).hexdigest(),
        len(context.inventory.snapshots), 0, (),
    )
```

Define `_rebuild_context_inventory(context)` in this task as a second call to
`build_context(CommonArgs(root=context.root, run_spec=context.run_spec_path,
acquisition_contract=context.acquisition_contract_path,
protected_inventory=context.protected_inventory_path))`;
require equal root, phase, registry head, checkpoint head, and return its
non-optional `inventory`. It must raise `AUTHORITY_CHANGED` rather than reuse the
first in-memory object when any binding differs.

```python
def _rebuild_context_inventory(context: FinalizationContext) -> SourceInventory:
    rebound = build_context(CommonArgs(
        root=context.root,
        run_spec=context.run_spec_path,
        acquisition_contract=context.acquisition_contract_path,
        protected_inventory=context.protected_inventory_path,
    ))
    before = (context.phase, context.registry, context.checkpoint)
    after = (rebound.phase, rebound.registry, rebound.checkpoint)
    if before != after or rebound.inventory is None:
        raise FinalizationError("AUTHORITY_CHANGED", "finalization authority changed", (), 5)
    return rebound.inventory
```

- [ ] **Step 4: Run read-only and existing snapshot-store tests**

Run: `python3 -m unittest tests.test_v52_source_finalize.StatusAndPreflightTests tests.test_v52_snapshot_store.InventoryTests tests.test_v52_snapshot_store.LoaderTests -v`

Expected: all tests `OK` and no network-call mock invoked.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git commit -m "feat: add v5.2 finalization status and preflight"
```

### Task 6: Delegate canonical source publication to the snapshot store

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Write publish service tests**

Test the exact target root, `ACQUIRING` and terminal-checkpoint gates, required safe lock owner, clean success, identical retry, changed-byte conflict, interrupted write, symlink/hardlink/special-file rejection, and concurrent identical/nonidentical calls.

```python
def test_publish_uses_canonical_source_root(self) -> None:
    with patch.object(orchestration, "publish_snapshot_store") as publish:
        report = orchestration.publish_inventory(self.context, lock_owner="operator-1")
    publish.assert_called_once()
    self.assertEqual(
        publish.call_args.args[0],
        self.root / "data/v5_2_qd/historical/sources",
    )
    self.assertEqual(report.status, "PUBLISHED")
```

- [ ] **Step 2: Run tests and verify publication is not implemented**

Run: `python3 -m unittest tests.test_v52_source_finalize.PublishTests -v`

Expected: `AttributeError` for `publish_inventory`.

- [ ] **Step 3: Implement the thin publication service**

```python
@dataclass(frozen=True)
class PublishReport:
    status: Literal["PUBLISHED", "EXISTING"]
    inventory_sha256: str
    snapshot_count: int

def publish_inventory(context: FinalizationContext, *, lock_owner: str) -> PublishReport:
    _require_mutable_context(context, phase="ACQUIRING")
    preflight = run_preflight(context)
    if preflight.status != "PASS" or context.inventory is None or context.snapshot_inputs is None:
        raise FinalizationError("PREFLIGHT_BLOCKED", "source preflight is blocked", (), 4)
    source_root = context.root / "data/v5_2_qd/historical/sources"
    existed = _canonical_inventory_exists(source_root)
    receipt = publish_snapshot_store(
        source_root, context.inventory, context.snapshot_inputs, lock_owner=lock_owner
    )
    loaded = load_snapshot_store(source_root, context.snapshot_inputs)
    if inventory_bytes(loaded.inventory) != inventory_bytes(context.inventory):
        raise FinalizationError("POSTCONDITION_FAILED", "published inventory differs", (), 5)
    return PublishReport("EXISTING" if existed else "PUBLISHED", receipt.inventory_sha256, receipt.snapshot_count)
```

Implement `_canonical_inventory_exists(source_root)` with `os.lstat` on
`source_inventory.json`: return `False` only for `FileNotFoundError`, return
`True` only for a regular single-link file, and raise `PATH_UNSAFE` for a
symlink, hardlink, directory, FIFO, socket, or inaccessible entry.

Map `PublishConflict`/`LockHeld` to exit 6 and unsafe postconditions to exit 5. Do not duplicate the snapshot store's lock implementation.

- [ ] **Step 4: Verify publication and snapshot-store race suites**

Run: `python3 -m unittest tests.test_v52_source_finalize.PublishTests tests.test_v52_snapshot_store.PublicationTests -v`

Expected: all tests `OK`.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git commit -m "feat: orchestrate v5.2 source publication"
```

### Task 7: Write explicit independent-review receipts

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Write review request and receipt tests**

Cover exact identity/role, both decisions, canonical sorted unique SHA query IDs, unknown/out-of-graph IDs, query-document tampering, explicit namespace, existing-path conflict, extra keys, and final rebind.

```python
@dataclass(frozen=True)
class ReviewRequest:
    reviewer_identity: str
    reviewer_role: str
    decision: Literal["APPROVED", "REJECTED"]
    query_ids_file: Path
    write_receipt: Path

def test_review_receipt_binds_explicit_query_document(self) -> None:
    report = orchestration.write_review(self.context, self.review_request)
    payload = json.loads(report.path.read_bytes())
    self.assertEqual(payload["query_ids_document"], {
        "path": self.query_ids_path.relative_to(self.root).as_posix(),
        "sha256": sha256(self.query_ids_path.read_bytes()),
    })
    self.assertEqual(payload["reviewed_query_ids"], self.query_ids)
```

- [ ] **Step 2: Run tests and verify review writing is absent**

Run: `python3 -m unittest tests.test_v52_source_finalize.ReviewTests -v`

Expected: `AttributeError` for `write_review`.

- [ ] **Step 3: Implement canonical query-ID loading and review payload**

Use a no-follow, regular-single-link, bounded read. Require exact document shape and canonical bytes:

```python
def _load_query_ids_document(root: Path, path: Path) -> tuple[tuple[str, ...], dict[str, str]]:
    relative, raw = _read_explicit_artifact(root, path, "review query IDs")
    value = _canonical_object(raw, "review query IDs")
    if set(value) != {"query_ids"}:
        raise FinalizationError("QUERY_IDS_INVALID", "query ID document keys differ", (relative,), 3)
    query_ids = tuple(value["query_ids"])
    if query_ids != tuple(sorted(set(query_ids))) or not query_ids:
        raise FinalizationError("QUERY_IDS_INVALID", "query IDs must be sorted and unique", (relative,), 3)
    return query_ids, {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}
```

Build the exact Task 1 review payload and write it through the shared writer:

```python
def write_review(context: FinalizationContext, request: ReviewRequest) -> ReviewReport:
    _require_mutable_context(context, phase="ACQUIRING")
    if request.reviewer_identity != "independent-source-auditor" or request.reviewer_role != "independent-source-auditor":
        raise FinalizationError("REVIEWER_INVALID", "reviewer identity or role differs", (), 3)
    query_ids, document = _load_query_ids_document(context.root, request.query_ids_file)
    inventory_ids = set(context.inventory.logical_query_ids)
    if not set(query_ids).issubset(inventory_ids):
        raise FinalizationError("QUERY_IDS_INVALID", "review selection is outside inventory", (), 3)
    raw_inventory = inventory_bytes(context.inventory)
    payload = {
        "schema_version": 1,
        "receipt_kind": "SOURCE_REVIEW",
        "run_id": context.run_spec.run_id,
        "critical_errors": [],
        "reviewer_identity": request.reviewer_identity,
        "reviewer_role": request.reviewer_role,
        "decision": request.decision,
        "reviewed_query_ids": list(query_ids),
        "query_ids_document": document,
        "inventory_sha256": hashlib.sha256(raw_inventory).hexdigest(),
        "snapshot_sha256s": list(context.inventory.snapshot_sha256s),
        "run_spec": context.snapshot_inputs.bindings.run_spec.as_mapping(),
        "acquisition_contract": context.snapshot_inputs.bindings.acquisition_contract.as_mapping(),
        "git_head": context.git_head,
        "occurred_at_utc": _utc_now(),
    }
    return _write_validated_receipt(
        context.root,
        request.write_receipt,
        payload,
        schema_name="v5_2_source_review_receipt.schema.json",
        expected_prefix="audit/v5_2_qd/historical/acquisition/source_reviews/",
    )
```

`_require_mutable_context` must narrow and return a context with non-optional
RunSpec, inventory, snapshot inputs, registry, checkpoint, and Git head before
the body accesses those fields. Validate against the bound schema, then publish
with no-follow parent traversal, `O_CREAT|O_EXCL`, fsync, final rebind, and
cleanup only for the inode created by the current attempt.

Define `_utc_now()` as
`datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")`; tests patch this
single clock boundary to deterministic canonical timestamps.

Define the common result and writer contracts here:

```python
@dataclass(frozen=True)
class ReceiptReport:
    path: Path
    sha256: str
    receipt_kind: Literal["SOURCE_REVIEW", "CLEAN_SOURCE_REPLAY"]

ReviewReport = ReceiptReport
ReplayReport = ReceiptReport

def _write_receipt_exclusive(
    root: Path, path: Path, raw: bytes, expected_prefix: str
) -> str:
    relative = v52_timezone_review._relative(root, path, "source receipt")
    if not relative.as_posix().startswith(expected_prefix):
        raise FinalizationError(
            "PATH_UNSAFE", "source receipt is outside its namespace",
            (relative.as_posix(),), 5,
        )
    published, snapshot = v52_timezone_review._write_safe(
        root, relative, raw, "source receipt"
    )
    v52_timezone_review._rebind_safe_snapshot(root, snapshot, "source receipt")
    return published.relative_to(root).as_posix()

def _write_validated_receipt(
    root: Path,
    path: Path,
    payload: Mapping[str, object],
    *,
    schema_name: str,
    expected_prefix: str,
) -> ReceiptReport:
    raw = canonical_json_bytes(payload) + b"\n"
    execution_registry._validate_bound_schema(root, schema_name, payload)
    relative = _write_receipt_exclusive(root, path, raw, expected_prefix)
    rebound = _read_explicit_artifact(root, root / relative, "source receipt")[1]
    if rebound != raw:
        raise FinalizationError("POSTCONDITION_FAILED", "source receipt changed", (relative,), 5)
    return ReceiptReport(root / relative, hashlib.sha256(raw).hexdigest(), payload["receipt_kind"])
```

- [ ] **Step 4: Verify review and registry compatibility**

Run: `python3 -m unittest tests.test_v52_source_finalize.ReviewTests tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_transition_validates_real_evidence -v`

Expected: all tests `OK`.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git commit -m "feat: record explicit v5.2 source reviews"
```

### Task 8: Write clean replay receipts with distinct UUIDv4 nonces

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Write replay tests**

Test explicit receipt path, no default/discovery, new exclusive execution on every call, canonical lowercase UUIDv4, zero network attempts, complete inventory query set, byte-identical replay, existing-path conflict even for equal bytes, and distinct IDs/hashes for two calls.

```python
def test_two_replays_have_distinct_execution_ids_and_equal_projection(self) -> None:
    first = orchestration.write_replay(self.context, ReplayRequest(self.first_path))
    second = orchestration.write_replay(self.context, ReplayRequest(self.second_path))
    first_payload = json.loads(first.path.read_bytes())
    second_payload = json.loads(second.path.read_bytes())
    self.assertNotEqual(first_payload["execution_id"], second_payload["execution_id"])
    self.assertNotEqual(first.sha256, second.sha256)
    self.assertEqual(first_payload["inventory_sha256"], second_payload["inventory_sha256"])
    self.assertEqual(first_payload["network_attempts"], 0)
    self.assertEqual(second_payload["network_attempts"], 0)
```

- [ ] **Step 2: Run tests and verify replay writing is absent**

Run: `python3 -m unittest tests.test_v52_source_finalize.ReplayTests -v`

Expected: `AttributeError` for `write_replay`.

- [ ] **Step 3: Implement network-free replay and receipt creation**

```python
@dataclass(frozen=True)
class ReplayRequest:
    write_receipt: Path

def _execution_id() -> str:
    raw = bytearray(secrets.token_bytes(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))

def _clean_replay_payload(
    context: FinalizationContext,
    inventory: SourceInventory,
    execution_id: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "receipt_kind": "CLEAN_SOURCE_REPLAY",
        "run_id": context.run_spec.run_id,
        "critical_errors": [],
        "execution_id": execution_id,
        "status": "PASS",
        "network_attempts": 0,
        "reviewed_query_ids": list(inventory.logical_query_ids),
        "inventory_sha256": hashlib.sha256(inventory_bytes(inventory)).hexdigest(),
        "snapshot_sha256s": list(inventory.snapshot_sha256s),
        "run_spec": context.snapshot_inputs.bindings.run_spec.as_mapping(),
        "acquisition_contract": context.snapshot_inputs.bindings.acquisition_contract.as_mapping(),
        "git_head": context.git_head,
        "recorded_at_utc": _utc_now(),
    }

def write_replay(context: FinalizationContext, request: ReplayRequest) -> ReplayReport:
    _require_mutable_context(context, phase="ACQUIRING")
    loaded = load_snapshot_store(_source_root(context.root), context.snapshot_inputs)
    replayed = replay_verified_snapshots(context.snapshot_inputs.replay)
    rebuilt = build_inventory(context.snapshot_inputs)
    if replayed != loaded.snapshots or inventory_bytes(rebuilt) != inventory_bytes(loaded.inventory):
        raise FinalizationError("REPLAY_MISMATCH", "clean replay projection differs", (), 5)
    payload = _clean_replay_payload(context, loaded.inventory, _execution_id())
    return _write_validated_receipt(
        context.root,
        request.write_receipt,
        payload,
        schema_name="v5_2_clean_source_replay_receipt.schema.json",
        expected_prefix="audit/v5_2_qd/historical/acquisition/source_replays/",
    )
```

The schema validator must reject any payload key not shown above.

Wrap all network entry points used in tests (`urllib.request.urlopen`, `socket.socket`) with fail-fast mocks and ensure production imports no HTTP/acquisition module in this path.

- [ ] **Step 4: Verify replay, snapshot replay, and receipt schema tests**

Run: `python3 -m unittest tests.test_v52_source_finalize.ReplayTests tests.test_v52_snapshot_store.ReplayTests tests.test_execution_registry.ExecutionRegistryTest.test_source_evidence_receipt_schemas_are_closed_and_canonical -v`

Expected: all tests `OK`.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git commit -m "feat: record clean v5.2 source replays"
```

### Task 9: Advance only the explicit authenticated receipt set

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Write advance request and gate tests**

Cover missing/glob/directory/duplicate paths, not-exactly-two replay paths, no approved review, different review subsets, cross-inventory/commit/contract receipts, stale checkpoint, exact retry, and a differing retry after `SOURCES_READY`.

```python
@dataclass(frozen=True)
class AdvanceRequest:
    review_receipts: tuple[Path, ...]
    replay_receipts: tuple[Path, Path]
    checkpoint: Path
    inventory: Path

def test_advance_passes_only_explicit_selected_receipts(self) -> None:
    with patch.object(orchestration, "advance_execution_phase") as advance:
        orchestration.advance_sources_ready(self.context, self.request)
    advance.assert_called_once_with(
        self.root,
        self.context.run_spec,
        expected_phase="ACQUIRING",
        next_phase="SOURCES_READY",
        inventory_path=self.inventory_path,
        validation_receipts=(*self.review_paths, *self.replay_paths),
    )
```

- [ ] **Step 2: Run tests and verify advance service is absent**

Run: `python3 -m unittest tests.test_v52_source_finalize.AdvanceTests -v`

Expected: `AttributeError` for `advance_sources_ready`.

- [ ] **Step 3: Add exact idempotent retry to the registry**

Before rejecting `state.phase != expected_phase`, permit only
`state.phase == next_phase` with an authenticated current phase receipt whose
inventory and ordered validation bindings equal fresh snapshots of the supplied
paths. For `SOURCES_READY`, also require its stored terminal checkpoint head is
still the authenticated terminal head. Return the existing state without a new
timestamp or file; every difference raises `ValueError` and writes no second
phase receipt. Add direct-registry tests for an exact retry and for changed
inventory, reordered/changed validation paths, and stale checkpoint evidence.

- [ ] **Step 4: Implement explicit prevalidation and registry delegation**

Authenticate each supplied receipt with the same schema helper, compare the explicit checkpoint to `context.checkpoint`, require the canonical inventory path, at least one approved review, exactly two replay paths/hashes/UUIDs, identical review subsets, and all receipt authority bindings. Then call the registry exactly once.

Implement the service body as:

```python
def advance_sources_ready(
    context: FinalizationContext, request: AdvanceRequest
) -> AdvanceReport:
    _require_mutable_context(
        context, phase=frozenset({"ACQUIRING", "SOURCES_READY"})
    )
    if len(request.review_receipts) < 1 or len(request.replay_receipts) != 2:
        raise FinalizationError("RECEIPT_SET_INVALID", "explicit receipt cardinality differs", (), 4)
    supplied = (*request.review_receipts, *request.replay_receipts)
    relative = tuple(_relative_artifact(context.root, path, "validation receipt")[0] for path in supplied)
    if len(relative) != len(set(relative)):
        raise FinalizationError("RECEIPT_SET_INVALID", "receipt path is duplicated", relative, 4)
    checkpoint = _relative_artifact(context.root, request.checkpoint, "terminal checkpoint")[0]
    inventory = _relative_artifact(context.root, request.inventory, "source inventory")[0]
    expected_checkpoint = context.snapshot_inputs.bindings.terminal_checkpoint.path
    expected_inventory = "data/v5_2_qd/historical/sources/source_inventory.json"
    if checkpoint != expected_checkpoint or inventory != expected_inventory:
        raise FinalizationError("AUTHORITY_MISMATCH", "checkpoint or inventory path differs", (checkpoint, inventory), 4)
    for path in supplied:
        _read_explicit_artifact(context.root, path, "validation receipt")
    state = advance_execution_phase(
        context.root,
        context.run_spec,
        expected_phase="ACQUIRING",
        next_phase="SOURCES_READY",
        inventory_path=context.root / inventory,
        validation_receipts=tuple(context.root / path for path in relative),
    )
    return AdvanceReport(
        "EXISTING" if context.registry.phase == "SOURCES_READY" else "ADVANCED",
        "SOURCES_READY",
        state.receipt_sha256,
    )
```

For idempotent retry, `build_context` returns the authenticated context at
`SOURCES_READY`; the registry makes the final exact-binding decision. Never
scan a receipt directory to repair or complete the request.

Define the result explicitly:

```python
@dataclass(frozen=True)
class AdvanceReport:
    status: Literal["ADVANCED", "EXISTING"]
    phase: Literal["SOURCES_READY"]
    receipt_sha256: str
```

- [ ] **Step 5: Verify service and direct-registry gate suites**

Run: `python3 -m unittest tests.test_v52_source_finalize.AdvanceTests tests.test_execution_registry -v`

Expected: all tests `OK`; invalid direct calls remain quarantined and do not advance.

- [ ] **Step 6: Commit**

```bash
git add mlb_backtest/v52_source_orchestration.py mlb_backtest/execution_registry.py tests/test_v52_source_finalize.py tests/test_execution_registry.py
git commit -m "feat: advance explicit v5.2 source evidence set"
```

### Task 10: Add the staged subcommand CLI and stable output contract

**Files:**
- Create: `mlb_backtest/v52_source_finalize.py`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Write parser/help/dispatch/output tests**

Require all six subcommands, common defaults, command-specific arguments, duplicate-conflict errors, `--json` handled failures as exactly one canonical stdout object plus newline and empty stderr, non-JSON errors on stderr, `--quiet`, exit codes 0/2–7, and `KeyboardInterrupt == 130`.

```python
def canonical_json(value: object) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True,
    )

def test_help_lists_all_stages(self) -> None:
    with self.assertRaises(SystemExit) as caught, redirect_stdout(io.StringIO()) as stdout:
        cli.main(["--help"])
    self.assertEqual(caught.exception.code, 0)
    for command in ("status", "preflight", "publish", "review", "replay", "advance"):
        self.assertIn(command, stdout.getvalue())

def test_json_failure_is_one_canonical_stdout_object(self) -> None:
    with patch.object(cli, "build_context", side_effect=FinalizationError("AUTHORITY_MISSING", "missing", ("x",), 3)), redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
        code = cli.main(["status", "--json"])
    self.assertEqual(code, 3)
    self.assertEqual(stderr.getvalue(), "")
    value = json.loads(stdout.getvalue())
    self.assertEqual(value["status"], "ERROR")
    self.assertEqual(stdout.getvalue(), canonical_json(value) + "\n")
```

- [ ] **Step 2: Run tests and verify the CLI module is absent**

Run: `python3 -m unittest tests.test_v52_source_finalize.CliTests -v`

Expected: `ImportError` for `v52_source_finalize`.

- [ ] **Step 3: Implement parser and dispatch only**

Create a custom `ArgumentParser` that raises a structured usage exception instead of writing immediately, pre-detect `--json` so usage errors follow machine-output rules, and define:

```python
def _parser() -> argparse.ArgumentParser:
    parser = FinalizationArgumentParser(prog="python3 -m mlb_backtest.v52_source_finalize")
    common = FinalizationArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=Path("."))
    common.add_argument("--run-spec", type=Path, default=DEFAULT_RUN_SPEC)
    common.add_argument("--acquisition-contract", type=Path, default=DEFAULT_CONTRACT)
    common.add_argument("--protected-inventory", type=Path, default=DEFAULT_PROTECTED)
    common.add_argument("--json", action="store_true")
    common.add_argument("--quiet", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", parents=[common])
    commands.add_parser("preflight", parents=[common])
    publish = commands.add_parser("publish", parents=[common])
    publish.add_argument("--lock-owner", required=True)
    review = commands.add_parser("review", parents=[common])
    review.add_argument("--reviewer-identity", required=True)
    review.add_argument("--reviewer-role", required=True)
    review.add_argument("--decision", choices=("APPROVED", "REJECTED"), required=True)
    review.add_argument("--query-ids-file", type=Path, required=True)
    review.add_argument("--write-receipt", type=Path, required=True)
    replay = commands.add_parser("replay", parents=[common])
    replay.add_argument("--write-receipt", type=Path, required=True)
    advance = commands.add_parser("advance", parents=[common])
    advance.add_argument("--review-receipt", type=Path, action="append", required=True)
    advance.add_argument("--replay-receipt", type=Path, action="append", required=True)
    advance.add_argument("--checkpoint", type=Path, required=True)
    advance.add_argument("--inventory", type=Path, required=True)
    return parser
```

Before `parse_args`, call this exact conflict guard; repeated review/replay
receipt options are intentionally excluded because their multiplicity is part
of the command contract:

```python
_SINGLE_VALUE_OPTIONS = frozenset({
    "--root", "--run-spec", "--acquisition-contract", "--protected-inventory",
    "--lock-owner", "--reviewer-identity", "--reviewer-role", "--decision",
    "--query-ids-file", "--write-receipt", "--checkpoint", "--inventory",
})

def _reject_conflicting_options(argv: Sequence[str]) -> None:
    observed: dict[str, str] = {}
    index = 0
    while index < len(argv):
        raw = argv[index]
        option, separator, attached = raw.partition("=")
        if option in _SINGLE_VALUE_OPTIONS:
            if separator:
                value = attached
                consumed = 1
            else:
                if index + 1 >= len(argv):
                    raise CliUsageError(f"{option} requires a value")
                value = argv[index + 1]
                consumed = 2
            if option in observed and observed[option] != value:
                raise CliUsageError(f"{option} has conflicting values")
            observed[option] = value
            index += consumed
        else:
            index += 1
```

Normalize repeated replay args to exactly two before creating `AdvanceRequest`.
The CLI may import service symbols only; it must not import `v52_http`,
`v52_acquire`, or a source network adapter.

- [ ] **Step 4: Implement canonical rendering and verify CLI tests**

Success JSON keys are `schema_version`, `command`, `status`, `phase`, `result`; failure keys are `schema_version`, `command`, `status`, `error_code`, `message`, `paths`. Serialize with `allow_nan=False`, `sort_keys=True`, `separators=(",", ":")`, and one newline.

Run: `python3 -m unittest tests.test_v52_source_finalize.CliTests -v && python3 -m mlb_backtest.v52_source_finalize --help >/tmp/v52-finalize-help.txt && ruff check mlb_backtest/v52_source_finalize.py tests/test_v52_source_finalize.py`

Expected: tests `OK`, help command exit 0, Ruff exit 0.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_source_finalize.py tests/test_v52_source_finalize.py
git commit -m "feat: add staged v5.2 source finalization CLI"
```

### Task 11: Bind the full execution closure and prove the miniature workflow

**Files:**
- Modify: `mlb_backtest/execution_registry.py:108-149`
- Modify: `tests/test_execution_registry.py:139-178,5520-5610`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Extend the computed closure test before the production tuple**

Rename the test root constant to cover acquisition and finalization, and include:

```python
EXECUTION_IMPORT_ROOT_MODULES = (
    "mlb_backtest.execution_registry",
    "mlb_backtest.v52_acquire",
    "mlb_backtest.v52_game_evidence_inventory",
    "mlb_backtest.v52_http",
    "mlb_backtest.v52_legacy",
    "mlb_backtest.v52_legacy_decision_inventory",
    "mlb_backtest.v52_query_plan",
    "mlb_backtest.v52_schedule_inventory",
    "mlb_backtest.v52_source_finalize",
    "mlb_backtest.v52_source_orchestration",
    "mlb_backtest.v52_source_contracts",
    "mlb_backtest.v52_timezone_review",
)

BOUND_SCHEMA_PATHS = (
    Path("schemas/v5_2_acquisition_checkpoint.schema.json"),
    Path("schemas/v5_2_schedule_anchor_inventory.schema.json"),
    Path("schemas/v5_2_execution_receipt.schema.json"),
    Path("schemas/v5_2_game_evidence_inventory.schema.json"),
    Path("schemas/v5_2_legacy_decision_inventory.schema.json"),
    Path("schemas/v5_2_split_receipt.schema.json"),
    Path("schemas/v5_2_timezone_candidate.schema.json"),
    Path("schemas/v5_2_timezone_review_receipt.schema.json"),
    Path("schemas/v5_2_timezone_seal_receipt.schema.json"),
    Path("schemas/v5_2_venue_timezones.schema.json"),
    Path("schemas/v5_2_source_inventory.schema.json"),
    Path("schemas/v5_2_source_review_receipt.schema.json"),
    Path("schemas/v5_2_clean_source_replay_receipt.schema.json"),
)
```

Keep `verify_v52_design.py` and any true dynamic-only dependency as explicit extras. The exact assertion must remain `bound == reachable | extras | schemas`, not a subset check. Add dirty and untracked cases for the CLI, service, verifier, snapshot store, and each new schema.

- [ ] **Step 2: Run the closure tests and verify production binding is stale**

Run: `python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest.test_bound_acquisition_import_closure_is_computed_and_exact tests.test_execution_registry.ExecutionRegistryTest.test_rejects_dirty_bound_execution_code_but_not_unrelated_files -v`

Expected: failure listing newly reachable finalization/verifier/snapshot-store files absent from `_BOUND_EXECUTION_CODE`.

- [ ] **Step 3: Update `_BOUND_EXECUTION_CODE` to exact computed authority**

Copy the sorted exact result proven by the test into the production tuple. Do not retain stale Python extras merely to make a superset pass. Include the dynamically loaded source inventory and both receipt schemas.

- [ ] **Step 4: Add a temporary-repository end-to-end workflow**

Using the real registry helper and `tests/v52_snapshot_store_fixture.py` only inside a temporary Git repository, execute `status -> preflight -> publish -> review -> replay -> replay -> advance -> status`. Assert final `SOURCES_READY`, two selected distinct replay UUIDs, one approved review, zero network calls, and canonical output. Before and after the test, assert the real repository path `data/v5_2_qd/historical/sources` is absent and unchanged.

```python
def lexists_snapshot(path: Path) -> tuple[int, int, int, int] | None:
    if not os.path.lexists(path):
        return None
    value = path.lstat()
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size)

def test_miniature_workflow_never_writes_real_historical_namespace(self) -> None:
    real_sources = ROOT / "data/v5_2_qd/historical/sources"
    before = lexists_snapshot(real_sources)
    with tempfile.TemporaryDirectory() as directory:
        fixture_root, context, request_bundle = self.build_finalization_fixture(
            Path(directory)
        )
        result = self.run_finalization_sequence(
            fixture_root, context, request_bundle
        )
        self.assertEqual(result.phase, "SOURCES_READY")
    self.assertEqual(lexists_snapshot(real_sources), before)
```

Define `build_finalization_fixture(parent)` in the test class to: initialize a
temporary Git repository with the same protocol/bound-code copier used by
`ExecutionRegistryTest.make_repo`; advance its real registry to `ACQUIRING`;
install its terminal checkpoint stub; use `store_inputs(current_phase="ACQUIRING")`
and `patched_store_replay()` for the deterministic 81-snapshot context; and
return explicit query-document, review, two replay, checkpoint, and inventory
paths. Define `run_finalization_sequence(fixture_root, context, request_bundle)`
as eight direct service calls in
the exact order named above, patching only the expensive verified-loader input
factory—not receipt writers, snapshot publication, or registry advancement.

- [ ] **Step 5: Run the focused security and E2E gate**

Run: `python3 -m unittest tests.test_v52_source_finalize tests.test_execution_registry tests.test_v52_snapshot_store tests.test_v52_verify_sources -v`

Expected: all tests `OK` with no production historical artifact created.

- [ ] **Step 6: Commit**

```bash
git add mlb_backtest/execution_registry.py tests/test_execution_registry.py tests/test_v52_source_finalize.py
git commit -m "test: bind v5.2 finalization execution authority"
```

### Task 12: Run the full verification gate and update the implementation record

**Files:**
- Create: `reports/v5_2_qd/source_finalization_cli_implementation.md`

- [ ] **Step 1: Run targeted static and schema checks**

Run:

```bash
ruff check mlb_backtest tests
git diff --check
python3 -m unittest tests.test_v52_source_finalize tests.test_execution_registry tests.test_v52_snapshot_store tests.test_v52_verify_sources -v
```

Expected: Ruff exit 0, diff check exit 0, all focused tests `OK`.

- [ ] **Step 2: Run the complete stable suite**

Run: `python3 -m unittest discover -s tests -p 'test*.py' -v`

Expected: exit 0 and final `OK`. Record the exact test count and elapsed time; do not replace this with a filtered or fail-fast run.

- [ ] **Step 3: Verify repository and protected-file state**

Run:

```bash
git status --short
git diff --name-only --cached
git ls-files --error-unmatch 'mlb_backtest/v52_query_plan 2.py' >/dev/null 2>&1; test $? -ne 0
git ls-files --error-unmatch 'tests/test_v52_query_plan 2.py' >/dev/null 2>&1; test $? -ne 0
```

Expected: only the four protected duplicate files remain untracked; neither protected Python duplicate is tracked or staged.

- [ ] **Step 4: Write an honest implementation record**

Create the implementation record with the commit range, exact commands/results,
schema hashes, closure count, and this statement: `No canonical historical
acquisition, source publication, review, replay, or phase transition was
performed by implementation tests.` Do not create production
`audit/v5_2_qd/historical/` or `data/v5_2_qd/historical/` evidence.

- [ ] **Step 5: Commit and push the verified implementation**

```bash
git add reports/v5_2_qd/source_finalization_cli_implementation.md
git commit -m "docs: record verified v5.2 finalization implementation"
git push origin feature/mlb-v5-1-backtest
```

## Completion criteria

- The CLI exposes exactly `status`, `preflight`, `publish`, `review`, `replay`, and `advance`.
- `status` and `preflight` are demonstrably read-only and network-free.
- Publication delegates to the immutable snapshot store at the canonical source root.
- Review receipts bind the explicit canonical query-ID document and frozen reviewer identity/role.
- Replay receipts carry canonical distinct UUIDv4 nonces and zero network attempts.
- The registry itself exact-validates both schemas and cannot be bypassed by direct public API calls.
- The immutable `SOURCES_READY` receipt selects exactly two distinct replays and at least one approved review.
- The finalization roots, transitive production imports, dynamic schemas, verifier, and snapshot store are exactly bound as execution code.
- Full tests and Ruff pass, the four protected duplicate files remain untouched, and no fixture is published into the real historical namespace.
