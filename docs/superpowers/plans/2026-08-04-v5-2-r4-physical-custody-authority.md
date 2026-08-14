# v5.2 R4 Physical Custody Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce one new R4 authority which attests a dedicated immutable APFS physical copy of the approved v5.1 raw corpus, then runs the existing recovery source workflow without changing R2/R3 bytes or v5.2.3 semantics.

**Architecture:** R4 is a new, fail-closed authority branch.  Descriptor-authenticated APFS volume UUID plus inode/ctime identity replaces R3's transient device binding only inside the new R4 custody inventory; the R3 inventory and all R2/R3 behavior stay byte-for-byte unchanged.  An owner-capability snapshot issuer publishes one immutable snapshot, then independently published closed inventory and transition receipt bind the new physical identity before the R4 contract, query/batch, evidence, and source-finalization layers admit it.

**Tech Stack:** Python 3.11, `unittest`, standard-library `ctypes` Darwin binding for `fgetattrlist`, descriptor-relative POSIX I/O, existing `mlb_backtest.secure_fs`, JSON Schema, Ruff.

---

## Non-negotiable invariants

- R2/R3 artifact bytes, serialization, accepted behavior, parser behavior, and failure mode are unchanged.  Shared code receives only additive R4 branches guarded by R2/R3 regressions; no existing R2/R3 artifact is edited or reinterpreted.
- R4 run id is `historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4`; its only source meaning is the existing R3 recovery meaning with a new physical-custody identity.
- Canonical inventory, transition receipt, and contract bytes are exactly `canonical_json_bytes(document) + b"\n"`; final destinations are exclusive publications through `secure_fs.rename_exclusive_at`.
- Persisted R4 identity is `volume_uuid`, `inode`, `ctime_ns`, file type, `nlink`, byte size, SHA-256, and mode.  `st_dev` is not persisted in any R4 artifact.  R4 persists 1,675 logical records, 4,711 unique physical members, and 5,025 logical record-role bindings; repeated approved raw references do not create physical copies.
- The snapshot issuer alone may copy corpus bytes.  `--plan-only`, acquisition, review, resume, replay, and finalization do not create, mutate, relink, chmod, flag, or repair it.
- The source approval is provenance only.  The R4 active protected inventory is `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json`.
- Test fixtures are same-module by default.  A later test may import only an explicitly listed shared test-support module created by an earlier task; it may never import a production fixture/private helper.
- Required order is: corrected plan commit → identity → issuance code → artifact code → legacy split → RunSpec → three review gates → fixture-only contract/query support → one live-owner snapshot/inventory/receipt/contract publication → capability/acquire → registry/evidence/exact suite → finalization → plan-only → real run.

## File map

| Path | Responsibility |
| --- | --- |
| `mlb_backtest/v52_r4_apfs.py` | Direct descriptor `fgetattrlist` binding, APFS UUID normalization, and R4 object/directory identity capture/rebind. |
| `mlb_backtest/v52_r4_snapshot_issuer.py` | Owner lock, issuer state machine, descriptor-only source-to-temp copy, immutable snapshot publication, and exclusive JSON publication. |
| `mlb_backtest/v52_r4_custody_artifacts.py` | Closed inventory/receipt builders for full-logical-path members and bindings, source-branch queries, physical/binding comparisons, canonical render/read/validate helpers, and identity-manifest/lock-array digests. |
| `mlb_backtest/v52_r4_external_protected_parent.py` | R4-only descriptor capability which reads the R4 inventory and validates APFS identity without R3 device matching. |
| `schemas/v5_2_r4_protected_raw_inventory.schema.json` | Closed R4 inventory schema. |
| `schemas/v5_2_r4_physical_custody_transition_receipt.schema.json` | Closed R4 transition receipt schema. |
| `mlb_backtest/v52_legacy.py`, `mlb_backtest/v52_legacy_decision_inventory.py` | Separate legacy logical evidence from the R3-only device/inode serialization. |
| `mlb_backtest/protocols.py`, `schemas/v5_2_run_spec.schema.json` | Exact R4 RunSpec literal and closed schema branch. |
| `mlb_backtest/v52_query_plan.py`, `mlb_backtest/v52_source_contracts.py`, `schemas/v5_2_acquisition_contract.schema.json` | `_validate_recovery_acquisition_contract` and `_load_static_contracts` own R4 contract admission; source-contract helpers/schema provide closed parsing.  This layer binds design/plan/RunSpec/custody and creates distinct R4 query/batch identity. |
| `mlb_backtest/v52_acquire.py` | R4 path authority, capability admission, plan-only rejection/success semantics, and no-mutation guard. |
| `mlb_backtest/execution_registry.py`, `mlb_backtest/v52_evidence.py`, `mlb_backtest/v52_game_evidence_inventory.py`, `mlb_backtest/v52_schedule_inventory.py` | R4 registry, checkpoint, evidence, and schedule identity propagation. |
| `mlb_backtest/v52_snapshot_store.py`, `mlb_backtest/v52_verify_sources.py`, `mlb_backtest/v52_source_orchestration.py`, `mlb_backtest/v52_source_finalize.py` | R4 source readiness, rebinding, verification, and terminal finalization. |
| `mlb_backtest/v52_r4_terminal_verify.py` | Read-only authenticated terminal report that requires atomic R4 `SOURCES_READY` and the 81/42/0/0 population. |
| `scripts/run_v52_r4_recovery.sh` | Narrow R4 execution entrypoint; it never alters the R2/R3 runner. |
| `scripts/run_task13_exact_suite.py`, `tests/test_task13_exact_suite_runner.py` | Append R4 custody modules to the authoritative exact suite without reordering existing children; prove runner isolation and R2/R3 preservation. |

The existing tests named in each task are extended rather than copied into a new broad integration fixture.  New test modules own security and custody protocol fixtures so that synthetic trees never claim to be the production custody root.

## Test-fixture convention

Every test module imports `unittest`, `tempfile`, and `Path`.  Every test is a discoverable `unittest.TestCase.test_*` method: no top-level test function relies on a third-party fixture injection, and no top-level function is silently ignored by `unittest`.  Each method needing a filesystem uses `with tempfile.TemporaryDirectory() as temporary: root = Path(temporary)`.  In the same RED step, each named fixture helper is declared in that test module before the test class; no helper is imported from a production module.  The sole exception is a shared test-support module explicitly created and listed by an earlier task (Task 11's `tests/r4_issued_authority_fixture.py`).  For example, custody tests use this concrete minimum fixture instead of an undeclared `_owner`:

```python
class SnapshotIssuerTests(unittest.TestCase):
    def _owner(self, root: Path) -> IssuanceOwner:
        parent = root / "custody"
        parent.mkdir(exist_ok=True)
        return acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x01" * 16)

    def _source_tree(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        return source
```

Each task's RED step explicitly lists its additional helper return shape before calling it.

### Task 1: Seal the implementation-plan baseline

**Files:**

- Create: `docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md`
- Read only: `docs/superpowers/specs/2026-08-04-v5-2-r4-physical-custody-authority-design.md`

- [ ] **Step 1: Verify the plan is the only staged file (2 minutes).**

Run: `git status --short && git diff --check -- docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md`

Expected: the plan is the sole intended change and `git diff --check` prints no whitespace error.

- [ ] **Step 2: Record the identity inputs before any R4 artifact exists (3 minutes).**

Run: `python3.11 -c "from pathlib import Path; import hashlib; p=Path('docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md'); print(hashlib.sha256(p.read_bytes()).hexdigest())" && git hash-object docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md`

Expected: two lowercase hexadecimal identifiers for the reviewed corrected plan.  Task 12 binds the blob and working-tree SHA of this corrected committed plan, not the superseded `969300c` version.

- [ ] **Step 3: Commit the immutable planning baseline (2 minutes).**

```bash
git add docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md
git commit -m "docs: correct r4 execution plan"
```

Expected: one corrected-plan commit whose tree includes only this plan.  Do not amend that commit after Task 12 has bound its blob and working-tree SHA-256.

### Task 2: Implement descriptor-authenticated APFS custody identity

**Files:**

- Create: `mlb_backtest/v52_r4_apfs.py`
- Create: `tests/test_v52_r4_apfs.py`
- Modify: `mlb_backtest/secure_fs.py: rename_exclusive_at` only if a descriptor helper required by the new module is absent
- Test: `tests/test_secure_fs.py`

- [ ] **Step 1: Write the failing normalization and direct-binding tests (4 minutes).**

```python
import unittest
from unittest.mock import patch
from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE, apfs_volume_uuid_from_fd

class R4ApfsTests(unittest.TestCase):
    def test_apfs_volume_uuid_from_fd_returns_lowercase_32_hex(self) -> None:
        with patch("mlb_backtest.v52_r4_apfs._fgetattrlist", return_value=(b"apfs", bytes.fromhex("00112233445566778899aabbccddeeff"))):
            self.assertEqual(apfs_volume_uuid_from_fd(17), "00112233445566778899aabbccddeeff")

    def test_apfs_volume_uuid_from_fd_rejects_non_apfs_and_bad_uuid(self) -> None:
        with patch("mlb_backtest.v52_r4_apfs._fgetattrlist", return_value=(b"hfs", b"x" * 16)):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "APFS"):
                apfs_volume_uuid_from_fd(17)
```

Also add tests that the binding requests both `ATTR_VOL_FSTYPE` and `ATTR_VOL_UUID`, refuses a bool/negative fd, refuses a 15-byte UUID, and that `capture_regular_identity()` stores no `st_dev` key.

- [ ] **Step 2: Run the red tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_apfs -v`

Expected: import failure because `mlb_backtest.v52_r4_apfs` does not exist.

- [ ] **Step 3: Add the minimal audited Darwin binding and typed identity values (5 minutes).**

```python
class CUSTODY_UNAVAILABLE(ValueError):
    pass

def apfs_volume_uuid_from_fd(fd: int) -> str:
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise CUSTODY_UNAVAILABLE("invalid custody descriptor")
    filesystem_type, raw_uuid = _fgetattrlist(fd, ATTR_VOL_FSTYPE | ATTR_VOL_UUID)
    if filesystem_type.lower() != b"apfs":
        raise CUSTODY_UNAVAILABLE("custody filesystem is not APFS")
    if len(raw_uuid) != 16:
        raise CUSTODY_UNAVAILABLE("custody volume UUID is malformed")
    return raw_uuid.hex()

@dataclass(frozen=True)
class R4ObjectIdentity:
    volume_uuid: str
    inode: int
    ctime_ns: int
    file_type: str
    nlink: int
    byte_size: int
    sha256: str
    mode: int
```

`_fgetattrlist` calls `ctypes.CDLL(None, use_errno=True).fgetattrlist` with the descriptor form and a structure requesting `ATTR_VOL_FSTYPE | ATTR_VOL_UUID`; it raises `CUSTODY_UNAVAILABLE` on missing symbol, nonzero errno, or malformed buffer.  It must not invoke a shell, `diskutil`, `mount`, `PATH`, or path-based volume lookup.  Capture and rebind functions use `os.fstat(fd)`, require regular/directory type as applicable, use `stat.S_IMODE`, and compare the UUID through descriptors for every ancestor/member.

- [ ] **Step 4: Make remount and volume-copy semantics executable (4 minutes).**

```python
class R4ApfsRebindTests(unittest.TestCase):
    def test_rebind_ignores_changed_device_but_rejects_changed_volume_uuid(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        self.assertIsNone(rebind_directory_identity(11, expected, stat_result=_directory_stat(99, 9, 10)))
        with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "volume UUID"):
            rebind_directory_identity(11, expected, stat_result=_directory_stat(99, 9, 10), volume_uuid="1" * 32)

    def test_rebind_rejects_regular_file_or_non_directory_expectation(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "directory"):
            rebind_directory_identity(11, expected, stat_result=SimpleNamespace(st_ino=9, st_ctime_ns=10, st_mode=stat.S_IFREG | 0o444, st_nlink=1), volume_uuid="0" * 32)
```

Define these names before the test in `tests/test_v52_r4_apfs.py`:

```python
def _directory_stat(st_dev: int, st_ino: int, st_ctime_ns: int) -> SimpleNamespace:
    return SimpleNamespace(st_dev=st_dev, st_ino=st_ino, st_ctime_ns=st_ctime_ns, st_mode=stat.S_IFDIR | 0o555, st_nlink=2)
```

and in `mlb_backtest/v52_r4_apfs.py`:

```python
@dataclass(frozen=True)
class R4DirectoryIdentity:
    volume_uuid: str
    inode: int
    ctime_ns: int
    file_type: str
    nlink: int
    mode: int

def rebind_directory_identity(fd: int, expected: R4DirectoryIdentity, *, stat_result: os.stat_result | object | None = None, volume_uuid: str | None = None) -> None:
    observed = os.fstat(fd) if stat_result is None else stat_result
    actual_uuid = apfs_volume_uuid_from_fd(fd) if volume_uuid is None else volume_uuid
    if expected.file_type != "directory" or not stat.S_ISDIR(observed.st_mode):
        raise CUSTODY_UNAVAILABLE("directory custody identity differs")
    if actual_uuid != expected.volume_uuid or (observed.st_ino, observed.st_ctime_ns, stat.S_IMODE(observed.st_mode), observed.st_nlink) != (expected.inode, expected.ctime_ns, expected.mode, expected.nlink):
        raise CUSTODY_UNAVAILABLE("directory custody identity differs")
```

Run: `python3.11 -m unittest tests.test_v52_r4_apfs tests.test_secure_fs -v`

Expected: all selected tests pass; a changed synthetic device alone passes and a changed UUID fails.

- [ ] **Step 5: Refactor and commit the identity boundary (3 minutes).**

Run: `python3.11 -m ruff check mlb_backtest/v52_r4_apfs.py tests/test_v52_r4_apfs.py && git diff --check`

Expected: exit status 0 and no output other than any Ruff summary.

```bash
git add mlb_backtest/v52_r4_apfs.py tests/test_v52_r4_apfs.py mlb_backtest/secure_fs.py tests/test_secure_fs.py
git commit -m "feat: add r4 APFS custody identity"
```

### Task 3: Build the exclusive snapshot issuer and owner state machine

**Files:**

- Create: `mlb_backtest/v52_r4_snapshot_issuer.py`
- Create: `tests/test_v52_r4_snapshot_issuer.py`
- Modify: `mlb_backtest/secure_fs.py: rename_exclusive_at` only to expose the already-audited descriptor primitive
- Test: `tests/test_secure_fs.py`

- [ ] **Step 1: Write red tests for nonce lock ownership and observer states (5 minutes).**

```python
class SnapshotIssuerLockTests(unittest.TestCase):
    def test_live_owner_requires_original_lock_identity_and_matching_nonce(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "custody"; parent.mkdir()
            owner = acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x01" * 16)
            self.assertEqual(owner.temporary_name, ".v5_1_bt_r4.issuance." + "01" * 16 + ".tmp")
            self.assertIs(observe_issuance_state(parent, "v5_1_bt_r4"), IssuanceState.OBSERVER_INCOMPLETE_LOCKED)
            self.assertTrue(owner.is_live_owner())
            owner.abort_before_rename()

    def test_nonowner_cannot_remove_existing_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "custody"; parent.mkdir()
            owner = acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x02" * 16)
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "issuance lock exists"):
                acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x03" * 16)
            owner.abort_before_rename()
```

Add table-driven tests for `OWNER_LOCK_ACQUIRED`, `OWNER_PREPARING`, `OWNER_SNAPSHOT_PUBLISHED`, `OWNER_INVENTORY_PUBLISHED`, `OWNER_RECEIPT_PUBLISHED`, `OWNER_CONTRACT_AUTHENTICATED`, `OBSERVER_INCOMPLETE_LOCKED`, `FINAL`, `RUNNING`, `EXECUTABLE`, `OBSERVER_INCOMPLETE_UNLOCKED`, and `ABSENT` in the precedence stated by the design.

- [ ] **Step 2: Run the red issuer tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_snapshot_issuer -v`

Expected: import failure for `v52_r4_snapshot_issuer`.

- [ ] **Step 3: Implement the owner capability with no-follow lock acquisition (5 minutes).**

```python
def acquire_issuance_lock(parent: Path, final_name: str, *, nonce: bytes | None = None) -> IssuanceOwner:
    if nonce is None:
        nonce = secrets.token_bytes(16)
    if len(nonce) != 16:
        raise CUSTODY_UNAVAILABLE("issuance nonce must contain 16 bytes")
    parent_fd = _open_directory_no_follow(parent)
    lock_name = f".{final_name}.issuance.lock"
    try:
        lock_fd = os.open(lock_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    except FileExistsError as exc:
        os.close(parent_fd)
        raise CUSTODY_UNAVAILABLE("issuance lock exists") from exc
    return IssuanceOwner(parent_fd, lock_fd, lock_name, final_name, nonce, os.fstat(lock_fd))
```

`IssuanceOwner.abort_before_rename()` deletes only its descriptor-verified temporary tree while no final directory exists, fsyncs the parent, unlinks its descriptor-verified lock, fsyncs again, and closes both descriptors.  Its post-rename abort method raises `CUSTODY_UNAVAILABLE` without removing final/audit/contract/lock data.

- [ ] **Step 4: Write red tests for safe copy, immutable publish, and no-replace publication (5 minutes).**

```python
class SnapshotIssuerPublicationTests(unittest.TestCase):
    def _owner(self, root: Path) -> IssuanceOwner:
        parent = root / "custody"; parent.mkdir()
        return acquire_issuance_lock(parent, "v5_1_bt_r4", nonce=b"\x04" * 16)

    def _source_tree(self, root: Path) -> Path:
        source = root / "source"; source.mkdir()
        return source

    def test_publish_uses_descriptor_exclusive_rename_and_never_plain_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            owner = self._owner(Path(temporary))
            with patch("mlb_backtest.v52_r4_snapshot_issuer.rename_exclusive_at") as rename, patch.object(os, "replace") as replace:
                publish_owned_snapshot(owner, "temp", "v5_1_bt_r4")
            rename.assert_called_once(); replace.assert_not_called()

    def test_snapshot_copy_rejects_symlink_and_multilink_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = self._source_tree(root); (source / "bad").symlink_to("target")
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "symlink"):
                populate_snapshot(self._owner(root), source)
```

Add assertions for 1,675 records, 4,711 unique physical members, 5,025 ordered logical bindings, the four exact zero-byte lock names, source/destination rehash equality, regular `0444`, directory `0555`, post-rename no-follow rehash, and `UF_IMMUTABLE` verification with root last.  Define `_shared_raw_paths()` in this same test module to return the two full logical paths with expected reference counts `85` and `231`; assert each has one destination member/suffix and its complete repeated binding set.  Mock the OS flag setter on non-Darwin hosts; retain one Darwin-only integration test guarded by `@unittest.skipUnless(sys.platform == "darwin", "Darwin custody protocol")`.

- [ ] **Step 5: Implement publication and exercise failure cleanup boundaries (5 minutes).**

```python
def publish_owned_snapshot(owner: IssuanceOwner, temporary_name: str, final_name: str) -> None:
    owner.require_pre_rename_owner()
    rename_exclusive_at(temporary_name, final_name, src_dir_fd=owner.parent_fd, dst_dir_fd=owner.parent_fd)
    owner.mark_snapshot_published()

def publish_canonical_json(parent_fd: int, temporary_name: str, final_name: str, document: Mapping[str, object]) -> str:
    payload = canonical_json_bytes(document) + b"\n"
    _write_owned_regular_file(parent_fd, temporary_name, payload)
    rename_exclusive_at(temporary_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    return hashlib.sha256(payload).hexdigest()
```

`populate_snapshot` opens every source and destination path relative to authenticated descriptors, rejects special files and links other than one, streams while hashing, reopens destination to hash again, and closes descriptors deterministically.  The method never calls `shutil.copy*`, `Path.write_*`, `os.rename`, `os.replace`, or a plain name-existence check followed by rename.

- [ ] **Step 6: Verify and commit the issuer (3 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_snapshot_issuer tests.test_secure_fs -v && python3.11 -m ruff check mlb_backtest/v52_r4_snapshot_issuer.py tests/test_v52_r4_snapshot_issuer.py && git diff --check`

Expected: selected suite and Ruff exit 0; exclusive rename tests prove a destination conflict is never overwritten.

```bash
git add mlb_backtest/v52_r4_snapshot_issuer.py tests/test_v52_r4_snapshot_issuer.py mlb_backtest/secure_fs.py tests/test_secure_fs.py
git commit -m "feat: add r4 custody snapshot issuer"
```

### Task 4: Define and publish closed custody artifacts

**Files:**

- Create: `mlb_backtest/v52_r4_custody_artifacts.py`
- Create: `schemas/v5_2_r4_protected_raw_inventory.schema.json`
- Create: `schemas/v5_2_r4_physical_custody_transition_receipt.schema.json`
- Create: `tests/test_v52_r4_custody_artifacts.py`
- Modify: `mlb_backtest/v52_r4_snapshot_issuer.py: publish_canonical_json`

- [ ] **Step 1: Write red closed-schema and canonical-byte tests (5 minutes).**

```python
from collections.abc import Sequence

@dataclass(frozen=True)
class CapturedCustodySnapshot:
    records: Sequence[dict[str, object]]
    members: Sequence[dict[str, object]]
    bindings: Sequence[dict[str, object]]
    root_locks: Sequence[dict[str, object]]

class R4CustodyArtifactTests(unittest.TestCase):
    def _captured_snapshot(self) -> CapturedCustodySnapshot:
        return CapturedCustodySnapshot(_records_1675(), _members_4711(), _bindings_5025(), _root_locks_4())

    def _owner(self, root: Path) -> IssuanceOwner:
        parent = root / "audit"; parent.mkdir()
        return acquire_issuance_lock(parent, "artifact", nonce=b"\x05" * 16)

    def test_inventory_render_is_canonical_newline_and_has_no_device_field(self) -> None:
        document = build_r4_inventory(self._captured_snapshot())
        payload = render_canonical_document(document)
        self.assertEqual(payload, canonical_json_bytes(document) + b"\n")
        self.assertNotIn(b'"st_dev"', payload)
        validate_r4_inventory(document)

    def test_receipt_binds_runspec_and_inventory_but_not_contract(self) -> None:
        receipt = build_transition_receipt(build_r4_inventory(self._captured_snapshot()), run_spec_sha256="a" * 64)
        self.assertEqual(receipt["run_spec"]["sha256"], "a" * 64)
        self.assertNotIn("contract", receipt)
        validate_transition_receipt(receipt)
```

In this same RED step, `_records_1675`, `_members_4711`, `_bindings_5025`, and `_root_locks_4` are concrete deterministic fixture factories that return exact cardinalities and all cross-links; no validator constant is monkeypatched.  `CapturedCustodySnapshot` is the exact production input dataclass created in `v52_r4_custody_artifacts.py`, so the test invokes the same count checks as production.

Include negative fixtures for every extra property, unsorted `records`/`members`/`bindings`/locks, a `..` path, an ancestor list not starting with `.`, a non-APFS UUID, wrong 1,675/4,711/5,025 count, nonempty lock, wrong modes, source approval mismatch, a repeated non-raw binding, a shared raw with a conflicting identity, a non-member binding path, and a digest mismatch.

- [ ] **Step 2: Run the red artifact tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_custody_artifacts -v`

Expected: import failure because artifact builders and schemas do not yet exist.

- [ ] **Step 3: Add both closed schemas and strict builder/validator functions (5 minutes).**

```python
R4_INVENTORY_PATH = Path("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json")
R4_RECEIPT_PATH = Path("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json")

def render_canonical_document(document: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(document) + b"\n"

def identity_manifest_sha256(document: Mapping[str, object]) -> str:
    selected = {key: document[key] for key in ("snapshot", "records", "members", "bindings", "root_locks", "ancestor_identities")}
    return hashlib.sha256(canonical_json_bytes(selected)).hexdigest()
```

The inventory schema requires precisely `schema_version`, `inventory_kind`, `logical_raw_root`, `source_approval`, `snapshot`, `manifest_count`, `physical_member_count`, `logical_binding_count`, `rawstore_verification`, `records`, `members`, `bindings`, `root_locks`, `ancestor_identities`, and `custody_identity_manifest_sha256`; every schema object has `additionalProperties: false`.  The fixed counts are 1,675/4,711/5,025.  `members` is sorted unique full logical paths below `data/v5_1_bt/raw`; traversal strips that prefix to one unpersisted physical suffix.  `bindings` is `(record_index, role, path)` ordered by record then `manifest,raw,seal`, joins to members, and permits only the source-derived repeated raw paths (85/231) without aliases.  The complete record schema uses closed source-discriminated `canonical_query` branches: baseball-savant 213 (`start,end,game_type,url` strings), schedule 5 (`start,end,gameType,role,url` strings), pitcher-bf 731 (`startDate,endDate,gameType,group,stats,url` strings plus integer `pitcher_id,sportIds`), and pitching 726 (`startDate,endDate,gameType,group,playerPool,stats,url` strings plus integer `limit,sportIds`).  The receipt has precisely the design's `schema_version`, `receipt_kind`, `run_id`, `run_spec`, `source_approval`, `r4_inventory`, `comparison`, `root_lock_comparisons`, and `custody_identity_manifest_sha256` fields; `comparison` separately contains 4,711 physical objects and 5,025 binding-comparison hashes.  All SHA and UUID patterns are lowercase fixed-length hex.

- [ ] **Step 4: Test final-path reread and conflict handling (4 minutes).**

```python
class R4CustodyArtifactPublishTests(R4CustodyArtifactTests):
    def test_publish_inventory_rereads_final_no_follow_before_returning_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = publish_inventory(self._owner(root), self._captured_snapshot())
            payload = (root / "audit" / "protected_raw_inventory_r4.json").read_bytes()
            self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
            self.assertEqual(payload[-1:], b"\n")
```

Run: `python3.11 -m unittest tests.test_v52_r4_custody_artifacts -v`

Expected: PASS, including conflict tests that preserve an existing canonical artifact byte-for-byte.

- [ ] **Step 5: Refactor only common canonical publication and commit (3 minutes).**

Run: `python3.11 -m ruff check mlb_backtest/v52_r4_custody_artifacts.py tests/test_v52_r4_custody_artifacts.py && git diff --check`

Expected: exit status 0.

```bash
git add mlb_backtest/v52_r4_custody_artifacts.py mlb_backtest/v52_r4_snapshot_issuer.py schemas/v5_2_r4_protected_raw_inventory.schema.json schemas/v5_2_r4_physical_custody_transition_receipt.schema.json tests/test_v52_r4_custody_artifacts.py
git commit -m "feat: add r4 custody artifacts"
```

### Task 5: Split legacy logical evidence from R3 device serialization

**Files:**

- Modify: `mlb_backtest/v52_legacy.py: ProtectedRawRecord and _protected_inventory_record`
- Modify: `mlb_backtest/v52_legacy_decision_inventory.py: inventory serialization and parser`
- Modify: `tests/test_v52_legacy.py`
- Modify: `tests/test_v52_legacy_decision_inventory.py`
- Modify: `tests/test_v52_external_protected_parent.py`

- [ ] **Step 1: Write the regression test that isolates R3 fields (4 minutes).**

```python
class LogicalProtectedRawRecordTests(unittest.TestCase):
    def test_r4_logical_record_never_serializes_device_identity(self) -> None:
        record = LogicalProtectedRawRecord(PurePosixPath("mlb_schedule/a.raw"), "a" * 64, 7)
        self.assertEqual(record.as_mapping(), {"path": "mlb_schedule/a.raw", "sha256": "a" * 64, "byte_size": 7})

    def test_existing_r3_inventory_still_requires_device_and_inode(self) -> None:
        with self.assertRaisesRegex(ValueError, "inode_fingerprint"):
            parse_protected_raw_inventory_document(_r3_inventory_without_inode())
```

In this same RED step, `_r3_inventory_without_inode()` returns the existing valid R3 fixture dictionary with only `inode_fingerprint` removed from its first artifact; it does not construct an R4 artifact.

- [ ] **Step 2: Run the red split tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_legacy tests.test_v52_legacy_decision_inventory tests.test_v52_external_protected_parent -v`

Expected: failure because `LogicalProtectedRawRecord` is not available.

- [ ] **Step 3: Add an R4-neutral logical type without changing R3 bytes (5 minutes).**

```python
@dataclass(frozen=True)
class LogicalProtectedRawRecord:
    path: PurePosixPath
    sha256: str
    byte_size: int

    def as_mapping(self) -> dict[str, object]:
        return {"path": self.path.as_posix(), "sha256": self.sha256, "byte_size": self.byte_size}
```

Keep `ProtectedRawRecord` and every R3 parser/serializer unchanged.  R4 builders consume `LogicalProtectedRawRecord` plus `R4ObjectIdentity`; no R4 path calls `_protected_inventory_record` or imports `manifest_device`, `seal_device`, or `payload_device`.

- [ ] **Step 4: Prove R2/R3 artifact immutability and commit (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_legacy tests.test_v52_legacy_decision_inventory tests.test_v52_external_protected_parent -v && git diff --exit-code -- audit/v5_1_bt/protected_raw_inventory.json protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json && python3.11 -m ruff check mlb_backtest/v52_legacy.py mlb_backtest/v52_legacy_decision_inventory.py`

Expected: all tests pass and the diff command prints nothing with exit 0.

```bash
git add mlb_backtest/v52_legacy.py mlb_backtest/v52_legacy_decision_inventory.py tests/test_v52_legacy.py tests/test_v52_legacy_decision_inventory.py tests/test_v52_external_protected_parent.py
git commit -m "refactor: separate r4 logical custody records"
```

### Task 6: Add the exact R4 RunSpec branch

**Files:**

- Create: `protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json`
- Modify: `mlb_backtest/protocols.py: expected_r4_recovery_source_run and parse_source_run_spec`
- Modify: `schemas/v5_2_run_spec.schema.json`
- Modify: `tests/test_protocols.py`

- [ ] **Step 1: Write the exact-literal red test (3 minutes).**

```python
class R4RunSpecTests(unittest.TestCase):
    def test_r4_recovery_run_is_closed_r3_semantics_with_r4_namespaces(self) -> None:
        value = expected_r4_recovery_source_run()
        self.assertEqual(value["run_id"], "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4")
        self.assertEqual(value["raw_root"], "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw")
        self.assertEqual(parse_source_run_spec(value).run_id, value["run_id"])
```

- [ ] **Step 2: Run the red RunSpec test (2 minutes).**

Run: `python3.11 -m unittest tests.test_protocols.ProtocolTests.test_r4_recovery_run_is_closed_r3_semantics_with_r4_namespaces -v`

Expected: attribute failure for `expected_r4_recovery_source_run`.

- [ ] **Step 3: Add the literal and only R4 path substitutions (5 minutes).**

```python
def expected_r4_recovery_source_run() -> dict[str, object]:
    value = expected_r3_recovery_source_run()
    return _replace_closed_run_paths(value, "r3", "r4")
```

Implement `_replace_closed_run_paths` as a private recursive copier that replaces only the exact R3 run-id/path segments with the exact R4 values specified in the approved design, then validate its output against a committed literal JSON file.  Do not make parsing accept arbitrary suffixes; add a closed `r4` `const` branch beside the existing R3 branch.

- [ ] **Step 4: Verify literal file/schema agreement and commit (3 minutes).**

Run: `python3.11 -m unittest tests.test_protocols -v && python3.11 -m ruff check mlb_backtest/protocols.py tests/test_protocols.py && git diff --check`

Expected: all protocol tests pass; R2/R3 literals remain accepted unchanged.

```bash
git add protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json mlb_backtest/protocols.py schemas/v5_2_run_spec.schema.json tests/test_protocols.py
git commit -m "feat: add r4 recovery run spec"
```

### Task 7: Review gate 1 — custody primitive security

**Files:**

- Review only: `mlb_backtest/v52_r4_apfs.py`, `mlb_backtest/v52_r4_snapshot_issuer.py`, `mlb_backtest/v52_r4_custody_artifacts.py`, `mlb_backtest/secure_fs.py`
- Test: `tests/test_v52_r4_apfs.py`, `tests/test_v52_r4_snapshot_issuer.py`, `tests/test_v52_r4_custody_artifacts.py`, `tests/test_secure_fs.py`

- [ ] **Step 1: Run the focused hostile-filesystem suite (5 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_apfs tests.test_v52_r4_snapshot_issuer tests.test_v52_r4_custody_artifacts tests.test_secure_fs -v`

Expected: PASS; unsupported fgetattrlist/non-APFS/malformed UUID, symlink/hardlink/special input, lock theft, rename conflict, wrong flags, and post-rename failure all fail closed.

- [ ] **Step 2: Review direct system-call and write-surface evidence (3 minutes).**

Run: `rg -n "diskutil|subprocess|os\\.system|shutil\\.copy|os\\.(replace|rename)" mlb_backtest/v52_r4_apfs.py mlb_backtest/v52_r4_snapshot_issuer.py`

Expected: no result for shell/copy/plain-rename mechanisms; the only publication call is `rename_exclusive_at`.

- [ ] **Step 3: Record a review-only checkpoint (2 minutes).**

Run: `git diff --check && git status --short`

Expected: no uncommitted review change.  Do not proceed on a failing gate.

### Task 8: Review gate 2 — legacy split and R4 RunSpec closure

**Files:**

- Review only: `mlb_backtest/v52_legacy.py`, `mlb_backtest/v52_legacy_decision_inventory.py`, `mlb_backtest/protocols.py`, `schemas/v5_2_run_spec.schema.json`, and the committed R4 RunSpec

- [ ] **Step 1: Execute legacy/R4 literal regression tests (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_legacy tests.test_v52_legacy_decision_inventory tests.test_v52_external_protected_parent tests.test_protocols -v`

Expected: PASS; R3 continues to require its device/inode inventory, R4 logical data has no device field, and the R4 literal is the sole accepted new RunSpec.

- [ ] **Step 2: Inspect the forbidden identity surface (3 minutes).**

Run: `rg -n 'manifest_device|seal_device|payload_device|st_dev' mlb_backtest/v52_r4_apfs.py mlb_backtest/v52_r4_snapshot_issuer.py mlb_backtest/v52_r4_custody_artifacts.py mlb_backtest/v52_legacy.py mlb_backtest/v52_legacy_decision_inventory.py`

Expected: R4 modules contain no persisted `st_dev` or R3 device field; any device terms are confined to unchanged R3 compatibility code in legacy modules.

- [ ] **Step 3: Check R2/R3 byte preservation before irreversible work (2 minutes).**

Run: `git diff --exit-code 94d1ec6fda0128bda1ecceae6e5c83df31a44898 -- audit/v5_1_bt/protected_raw_inventory.json protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json && git diff --check`

Expected: both commands exit 0 and print no R2/R3 artifact difference.

### Task 9: Review gate 3 — issuer-to-artifact readiness

**Files:**

- Review only: `mlb_backtest/v52_r4_apfs.py`, `mlb_backtest/v52_r4_snapshot_issuer.py`, `mlb_backtest/v52_r4_custody_artifacts.py`, `schemas/v5_2_r4_protected_raw_inventory.schema.json`, `schemas/v5_2_r4_physical_custody_transition_receipt.schema.json`

- [ ] **Step 1: Execute issuance/artifact protocol tests as one boundary (5 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_apfs tests.test_v52_r4_snapshot_issuer tests.test_v52_r4_custody_artifacts -v`

Expected: PASS; every publication is canonical newline JSON via `rename_exclusive_at`, the receipt has no contract hash, and every failure leaves an observer-incomplete state rather than repairing it.

- [ ] **Step 2: Audit exact issuance names and exclusive primitive use (3 minutes).**

Run: `rg -n 'v5_1_bt_r4|issuance\\.lock|issuance\\.[0-9a-f]|protected_raw_inventory_r4\\.json|physical_custody_transition_r4\\.json|rename_exclusive_at|canonical_json_bytes' mlb_backtest/v52_r4_snapshot_issuer.py mlb_backtest/v52_r4_custody_artifacts.py`

Expected: the exact final root, lock, temp suffix protocol, artifact names, canonical renderer, and exclusive publication primitive are present; no ordinary `os.rename` or `os.replace` is present.

- [ ] **Step 3: Close the third review gate without edits (2 minutes).**

Run: `python3.11 -m ruff check mlb_backtest/v52_r4_apfs.py mlb_backtest/v52_r4_snapshot_issuer.py mlb_backtest/v52_r4_custody_artifacts.py && git diff --check && git status --short`

Expected: exit 0, no whitespace errors, and no review-only change.  Only after this gate may Task 12 perform the one real issuance.

### Task 10: Implement fixture-only R4 contract/query support before issuance

**Files:**

- Create in Task 12 only: `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json`
- Create: `tests/fixtures/r4_contract.json`
- Modify: `mlb_backtest/v52_query_plan.py: _validate_recovery_acquisition_contract, _load_static_contracts, R4 authority branch, and static batch construction`
- Modify: `mlb_backtest/v52_source_contracts.py: closed physical_custody_transition parser helpers`
- Modify: `schemas/v5_2_acquisition_contract.schema.json`
- Modify: `tests/test_v52_acquisition_contract.py`
- Modify: `tests/test_v52_query_plan.py`

- [ ] **Step 1: Write red contract closure and distinct-ID tests (4 minutes).**

```python
import json
from pathlib import Path

FIXTURES = Path(__file__).with_name("fixtures")

class AcquisitionContractTests(unittest.TestCase):
    def _r4_contract_fixture(self) -> dict[str, object]:
        return json.loads((FIXTURES / "r4_contract.json").read_text(encoding="utf-8"))

    def test_r4_contract_requires_active_inventory_equal_transition_inventory(self) -> None:
        contract = self._r4_contract_fixture()
        contract["legacy_protection"]["protected_inventory_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "r4 inventory"):
            _validate_recovery_acquisition_contract(contract, contract["bindings"])
```

In this same step, `tests/test_v52_query_plan.py` declares `_r4_authority_fixture()` and `_r3_authority_fixture()` as concrete parsed fixture dictionaries and asserts `build_static_batch(_r4_authority_fixture()).batch_id != build_static_batch(_r3_authority_fixture()).batch_id`.  The fixture contract uses synthetic 64-hex R4 inventory/receipt hashes and does not create the canonical R4 contract file.

- [ ] **Step 2: Run the red contract/query tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_acquisition_contract tests.test_v52_query_plan -v`

Expected: failure because the R4 branches of `_validate_recovery_acquisition_contract` and `_load_static_contracts` do not exist.

- [ ] **Step 3: Implement acyclic R4 contract admission (5 minutes).**

```python
def _validate_r4_physical_custody_transition(contract: Mapping[str, object]) -> None:
    transition = _closed_mapping(contract["physical_custody_transition"], "physical_custody_transition")
    active = _closed_mapping(contract["legacy_protection"], "legacy_protection")
    inventory = _closed_mapping(transition["r4_inventory"], "r4_inventory")
    if inventory["path"] != active["protected_inventory_path"] or inventory["sha256"] != active["protected_inventory_sha256"]:
        raise ValueError("r4 inventory must be active legacy protection")

def _validate_recovery_acquisition_contract(contract: Mapping[str, object], bindings: Mapping[str, object]) -> None:
    if contract.get("run_id") == _R4_RECOVERY_RUN_ID:
        _validate_r4_physical_custody_transition(contract)
        _validate_r4_contract_bindings(contract, bindings)
        return
    if contract.get("run_id") == _R3_RECOVERY_RUN_ID:
        _validate_r3_recovery_acquisition_contract(contract, bindings)
        return
```

`_load_static_contracts` calls this owner validation before constructing an R4 static batch.  It accepts only a canonical final-path contract in production, but tests use the fixture document.  This task deliberately does not render, create, stage, or commit the canonical R4 contract literal because a live owner must publish it in Task 12 while retaining the issuance lock.

- [ ] **Step 4: Verify fixture-only support and commit (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_acquisition_contract tests.test_v52_query_plan -v && python3.11 -m ruff check mlb_backtest/v52_source_contracts.py mlb_backtest/v52_query_plan.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py && git diff --check`

Expected: PASS; fixture contracts enforce the closed R4 transition and produce distinct R4 batch identities, while `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json` remains absent.

```bash
git add mlb_backtest/v52_source_contracts.py schemas/v5_2_acquisition_contract.schema.json mlb_backtest/v52_query_plan.py tests/fixtures/r4_contract.json tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py
git commit -m "feat: add r4 contract query support"
```

### Task 11: Implement and commit the live-owner issuer before operation

**Files:**

- Create in later Task 12 only: `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4`, R4 inventory, receipt, and canonical contract
- Create: `tests/r4_issued_authority_fixture.py`
- Modify: `mlb_backtest/v52_r4_snapshot_issuer.py: provision_custody_parent, issue_and_bind CLI command, and owner state transitions`
- Modify: `tests/test_v52_r4_snapshot_issuer.py`
- Modify: `mlb_backtest/v52_r4_snapshot_issuer.py: _parser and main argparse dispatch for provision-custody-parent, preflight, issue-and-bind, and verify`

- [ ] **Step 1: Add the red live-owner continuity test (4 minutes).**

```python
from tests.r4_issued_authority_fixture import issue_test_r4_authority


class SnapshotIssuerTests(unittest.TestCase):
    def test_provision_requires_absent_or_authenticated_custody_parent(self) -> None:
        parent = self._temporary_custody_parent()
        provision_custody_parent(parent)
        self.assertTrue(parent.is_dir())

    def test_issue_and_bind_releases_lock_only_after_final_contract_reread(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = issue_test_r4_authority(Path(temporary))
            self.assertIs(
                observe_issuance_state(fixture.custody_root.parent, fixture.custody_root.name),
                IssuanceState.EXECUTABLE,
            )
            self.assertTrue(fixture.custody_root.is_dir())
```

In this same step, create the only cross-module fixture API, `tests/r4_issued_authority_fixture.py`.  It defines `@dataclass(frozen=True) IssuedR4Fixture(root: Path, custody_root: Path, inventory_path: Path, receipt_path: Path, contract_path: Path, run_spec_path: Path, sample_logical_path: PurePosixPath, sample_payload: bytes)` and `issue_test_r4_authority(root: Path) -> IssuedR4Fixture`.  Move the concrete successful `_issue_inputs()` writer into this module—do not retain a second writer in `test_v52_r4_snapshot_issuer.py`.  The helper materializes the deterministic full 1,675-record/4,711-member/5,025-binding source corpus and source inventory already required by the Task 3/4/11 tests, calls public `provision_custody_parent` followed by public `issue_and_bind`, and returns only after final-path no-follow authentication proves custody observer state `EXECUTABLE`, no issuance lock/temp, and authenticated custody/inventory/receipt/contract/RunSpec paths.  It returns a full-logical-path `sample_logical_path` from the generated inventory and its independently known `sample_payload`, so consumers never hard-code a toy fixture path.  It owns no persistent state outside `root`, has no production-private fixture import, and is listed here so Task 14 may import it.  `_temporary_custody_parent()` remains only the absent-parent negative-fixture helper; the shared issuer helper alone constructs absolute source-root/source-inventory and relative output paths, captures one `owner.release_after_contract_authentication()` call after final-path no-follow authentication of snapshot, inventory, receipt, RunSpec, and contract, and rejects any separate-process post-issue adoption.  `provision_custody_parent` creates only an absent exact parent, fsyncs/reopens/authenticates it, and rejects a symlink or unexpected preexisting type.

Add discoverable `SnapshotIssuerCliTests(unittest.TestCase)` methods using `tempfile.TemporaryDirectory()` that call the four complete argv vectors `("provision-custody-parent", "--custody-parent", str(parent))`, `("preflight", "--source-root", str(source_root), "--source-inventory", str(source_inventory), "--custody-parent", str(parent), "--final-name", "v5_1_bt_r4")`, the full Task 12 `issue-and-bind` vector, and the full Task 12 `verify` vector.  The parser declares these four exact subcommands.  The standalone preflight test passes an absent parent and asserts nonzero `CUSTODY_UNAVAILABLE`; the chained provision-then-preflight test asserts zero.  The verify test patches a preexisting lock and asserts a nonzero incomplete-state result without calling unlink, chmod, or any repair routine.

- [ ] **Step 2: Run the red owner-continuity test (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_snapshot_issuer -v`

Expected: failure because `issue_and_bind` and `OWNER_CONTRACT_AUTHENTICATED` final-path sequencing are unavailable.

- [ ] **Step 3: Implement one owner-only publication transaction (5 minutes).**

```python
def issue_and_bind(inputs: IssuanceInputs) -> IssuanceResult:
    with acquire_issuance_lock(inputs.custody_parent, "v5_1_bt_r4") as owner:
        snapshot = populate_and_publish_snapshot(owner, inputs.source_root)
        inventory = publish_inventory(owner, snapshot, inputs.source_approval)
        receipt = publish_transition_receipt(owner, inventory, inputs.run_spec_path)
        contract = publish_r4_contract(owner, inventory, receipt, inputs)
        authenticate_final_paths_no_follow(owner, snapshot, inventory, receipt, inputs.run_spec_path, contract)
        owner.mark_contract_authenticated()
        owner.release_after_contract_authentication()
        return IssuanceResult(IssuanceState.OWNER_CONTRACT_AUTHENTICATED, IssuanceState.EXECUTABLE)
```

The owner holds its original descriptor and nonce from lock creation through snapshot, inventory, receipt, and exact contract publication.  A crash at every point leaves `OBSERVER_INCOMPLETE_LOCKED`; a later process cannot adopt, delete, repair, or release that lock.  `release_after_contract_authentication()` verifies the descriptor/path identity, unlinks only the owner lock, fsyncs the custody parent, closes the unlinked lock fd and parent fd; no lock or matching temp remains when an observer reports `EXECUTABLE`.

- [ ] **Step 4: Run implementation-only issuer verification and commit (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_snapshot_issuer tests.test_v52_r4_custody_artifacts -v && python3.11 -m ruff check mlb_backtest/v52_r4_snapshot_issuer.py tests/r4_issued_authority_fixture.py tests/test_v52_r4_snapshot_issuer.py && git diff --check`

Expected: PASS without creating the real custody parent, snapshot, inventory, receipt, or contract.

```bash
git add mlb_backtest/v52_r4_snapshot_issuer.py tests/r4_issued_authority_fixture.py tests/test_v52_r4_snapshot_issuer.py
git commit -m "feat: implement r4 live-owner issuer"
```

Expected: this clean pre-issuance implementation HEAD is recorded only as an operator diagnostic.  The generated contract uses only the approved design/plan/RunSpec and physical-custody transition binding shapes; no new implementation-head field is serialized.

### Task 12: Operate the committed issuer exactly once

**Files:**

- Create at runtime: `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4`
- Create: `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json`
- Create: `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json`
- Create: `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json`

- [ ] **Step 1: Start from the committed issuer head (2 minutes).**

Run: `git status --short && git rev-parse HEAD`

Expected: no output from status and one recorded pre-issuance implementation SHA for the operator log only; the contract does not bind or serialize it.

- [ ] **Step 2: Prove standalone preflight fails without the custody parent, then provision and pass preflight (4 minutes).**

Run: `python3.11 -m mlb_backtest.v52_r4_snapshot_issuer preflight --source-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/data/v5_1_bt/raw' --source-inventory '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/audit/v5_1_bt/protected_raw_inventory.json' --custody-parent '/Users/taejunoh/Documents/MLB Custody' --final-name v5_1_bt_r4; test $? -ne 0; python3.11 -m mlb_backtest.v52_r4_snapshot_issuer provision-custody-parent --custody-parent '/Users/taejunoh/Documents/MLB Custody'; python3.11 -m mlb_backtest.v52_r4_snapshot_issuer preflight --source-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/data/v5_1_bt/raw' --source-inventory '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/audit/v5_1_bt/protected_raw_inventory.json' --custody-parent '/Users/taejunoh/Documents/MLB Custody' --final-name v5_1_bt_r4`

Expected: standalone preflight is `CUSTODY_UNAVAILABLE`; provision creates only the absent exact parent; chained preflight reports ABSENT plus 1,675/4,711/5,025 and persists no absolute source path.

- [ ] **Step 3: Issue and bind once from that head (5 minutes).**

Run: `python3.11 -m mlb_backtest.v52_r4_snapshot_issuer issue-and-bind --source-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/data/v5_1_bt/raw' --source-inventory '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/audit/v5_1_bt/protected_raw_inventory.json' --source-approval-logical-root data/v5_1_bt/raw --source-approval-inventory audit/v5_1_bt/protected_raw_inventory.json --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json --contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json --custody-parent '/Users/taejunoh/Documents/MLB Custody' --final-name v5_1_bt_r4 --audit-root audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition`

Expected: one live owner remains through snapshot→inventory→receipt→contract→final-path auth→lock release; crash leaves observer-incomplete and no adopter repairs it.

- [ ] **Step 4: Verify and commit only generated authority artifacts (3 minutes).**

Run: `python3.11 -m mlb_backtest.v52_r4_snapshot_issuer verify --custody-root '/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4' --inventory audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json --receipt audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json --contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json`

Expected: EXECUTABLE with no lock/temp and authenticated approved design, implementation-plan, RunSpec, and `physical_custody_transition` bindings only.  The recorded Step 1 implementation SHA remains an operator diagnostic and is neither serialized nor bound by the contract.

```bash
git add audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json
git commit -m "audit: issue r4 custody authority"
```

### Task 13: Verify lineage and authority binding after contract publication

**Files:**

- Review only: `mlb_backtest/v52_legacy.py`, `mlb_backtest/v52_legacy_decision_inventory.py`, `mlb_backtest/v52_source_contracts.py`, `mlb_backtest/v52_query_plan.py`, R4 RunSpec/contract/audit artifacts

- [ ] **Step 1: Execute closed authority tests (4 minutes).**

Run: `python3.11 -m unittest tests.test_protocols tests.test_v52_legacy tests.test_v52_legacy_decision_inventory tests.test_v52_acquisition_contract tests.test_v52_query_plan -v`

Expected: PASS; all R3 tests remain unchanged, R4 requires the R4 active inventory, and source approval is provenance-only.

- [ ] **Step 2: Check acyclic and immutable bindings (3 minutes).**

Run: `rg -n 'contract.*sha256|st_dev|physical_custody_transition' audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4 protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json mlb_backtest/v52_r4_custody_artifacts.py`

Expected: receipt contains no contract SHA, R4 artifacts contain no `st_dev`, and the contract contains exactly one closed transition object.

- [ ] **Step 3: Verify legacy artifact bytes are untouched (2 minutes).**

Run: `git diff --exit-code HEAD^ -- audit/v5_1_bt/protected_raw_inventory.json protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json && git diff --check`

Expected: no output and exit 0.

### Task 14: Admit R4 external protected-parent capability and acquisition

**Files:**

- Create: `mlb_backtest/v52_r4_external_protected_parent.py`
- Create: `tests/test_v52_r4_external_protected_parent.py`
- Use shared test support from Task 11: `tests/r4_issued_authority_fixture.py`
- Modify: `mlb_backtest/v52_legacy.py: issue/admit exact R4 legacy-evidence capability`
- Modify: `mlb_backtest/v52_acquire.py: R4 authority paths, real authentication, CLI legacy-root policy`
- Modify: `tests/test_v52_acquire.py`
- Modify: `tests/test_v52_acquire_e2e.py`
- Modify: `tests/test_v52_legacy.py`, `tests/test_execution_registry.py`

- [ ] **Step 1: Write red capability tests for UUID-aware rebind (5 minutes).**

```python
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import mlb_backtest.v52_r4_external_protected_parent as r4_external_parent
from mlb_backtest import execution_registry
from mlb_backtest.protocols import load_source_run_spec
from mlb_backtest.v52_legacy import _require_issued_legacy_evidence
from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE
from mlb_backtest.v52_r4_external_protected_parent import (
    R4ExternalProtectedParentCapability,
    R4ExternalProtectedParentInputs,
    bind_r4_snapshot_root,
    build_r4_external_protected_parent,
    read_r4_inventory_no_follow,
)
from tests.r4_issued_authority_fixture import IssuedR4Fixture, issue_test_r4_authority


class R4ExternalProtectedParentTests(unittest.TestCase):
    def _r4_inputs(self, root: Path) -> tuple[R4ExternalProtectedParentInputs, IssuedR4Fixture]:
        fixture = issue_test_r4_authority(root)
        return (
            R4ExternalProtectedParentInputs(
                target_root=fixture.root,
                physical_raw_root=fixture.custody_root,
                inventory_path=fixture.inventory_path,
            ),
            fixture,
        )

    def test_r4_capability_reads_only_full_logical_inventory_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs, fixture = self._r4_inputs(Path(temporary))
            capability = build_r4_external_protected_parent(inputs)
            try:
                with capability.open_bound_object(fixture.sample_logical_path) as stream:
                    self.assertEqual(stream.read(), fixture.sample_payload)
                with self.assertRaisesRegex(ValueError, "full logical path"):
                    capability.open_bound_object(PurePosixPath("mlb_schedule/a.raw"))
            finally:
                capability.close()

    def test_r4_capability_rejects_same_bytes_on_different_volume_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs, _fixture = self._r4_inputs(Path(temporary))
            with patch.object(
                r4_external_parent, "_apfs_volume_uuid_from_fd", return_value="f" * 32
            ) as reader:
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "volume UUID"):
                    build_r4_external_protected_parent(inputs)
            reader.assert_called_once()

    def test_factory_always_queries_private_actual_uuid_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs, _fixture = self._r4_inputs(Path(temporary))
            with patch.object(
                r4_external_parent,
                "_apfs_volume_uuid_from_fd",
                wraps=r4_external_parent._apfs_volume_uuid_from_fd,
            ) as reader:
                capability = build_r4_external_protected_parent(inputs)
            try:
                reader.assert_called()
            finally:
                capability.close()

    def test_actual_apfs_query_failure_fails_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs, _fixture = self._r4_inputs(Path(temporary))
            with patch.object(
                r4_external_parent,
                "_apfs_volume_uuid_from_fd",
                side_effect=CUSTODY_UNAVAILABLE("actual UUID query failed"),
            ):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "actual UUID"):
                    build_r4_external_protected_parent(inputs)

    def test_only_issued_exact_r4_capability_admits_through_legacy_and_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            issued_inputs, _issued_fixture = self._r4_inputs(root / "issued")
            capability = build_r4_external_protected_parent(issued_inputs)
            try:
                run_spec = load_source_run_spec(Path(
                    "protocols/v5_2_qd/historical_walkforward_500_"
                    "recovery_schedule_conflicts_missing_venue_r4.json"
                ))
                self.assertIs(_require_issued_legacy_evidence(capability), capability)
                self.assertIs(
                    execution_registry._admit_loader_legacy_evidence(
                        run_spec, capability, require_r3=False
                    ),
                    capability,
                )
                forged = object.__new__(R4ExternalProtectedParentCapability)
                with self.assertRaises(TypeError):
                    _require_issued_legacy_evidence(forged)
                class ForgedSubclass(R4ExternalProtectedParentCapability):
                    pass
                subclass = object.__new__(ForgedSubclass)
                with self.assertRaises(TypeError):
                    _require_issued_legacy_evidence(subclass)

                unissued_fixture = issue_test_r4_authority(root / "unissued")
                inventory = read_r4_inventory_no_follow(
                    unissued_fixture.root, unissued_fixture.inventory_path
                )
                tree = bind_r4_snapshot_root(unissued_fixture.custody_root, inventory)
                unissued = R4ExternalProtectedParentCapability(
                    inventory_sha256=inventory.sha256,
                    records=inventory.records,
                    tree=tree,
                )
                try:
                    with self.assertRaises(TypeError):
                        _require_issued_legacy_evidence(unissued)
                finally:
                    unissued.close()
            finally:
                capability.close()
```

- [ ] **Step 2: Run the red capability/acquire tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_external_protected_parent tests.test_v52_legacy tests.test_execution_registry tests.test_v52_acquire tests.test_v52_acquire_e2e -v`

Expected: import failure for the R4 capability and R4 acquisition path tests.

- [ ] **Step 3: Implement a separate R4 capability; do not generalize R3 device policy (5 minutes).**

```python
from mlb_backtest.v52_legacy import _register_issued_legacy_evidence


@dataclass(frozen=True)
class R4ExternalProtectedParentInputs:
    target_root: Path
    physical_raw_root: Path
    inventory_path: Path

def build_r4_external_protected_parent(inputs: R4ExternalProtectedParentInputs) -> R4ExternalProtectedParentCapability:
    inventory = read_r4_inventory_no_follow(inputs.target_root, inputs.inventory_path)
    tree = bind_r4_snapshot_root(inputs.physical_raw_root, inventory)
    capability = R4ExternalProtectedParentCapability(inventory_sha256=inventory.sha256, records=inventory.records, tree=tree)
    try:
        capability.validate()
        return _register_issued_legacy_evidence(capability)
    except Exception:
        capability.close()
        raise
```

Before this function, define frozen production dataclasses `R4ProtectedInventory(sha256: str, records: Sequence[LogicalProtectedRawRecord], members: Sequence[R4ObjectIdentity], bindings: Sequence[R4Binding])` and `R4BoundSnapshot(root_fd: int, volume_uuid: str)`, plus exact functions `read_r4_inventory_no_follow(target_root: Path, inventory_path: Path) -> R4ProtectedInventory`, private `_apfs_volume_uuid_from_fd(root_fd: int) -> str`, and `bind_r4_snapshot_root(physical_raw_root: Path, inventory: R4ProtectedInventory) -> R4BoundSnapshot`.  The public bind function always opens the custody root descriptor then calls that one private reader; its returned UUID must equal the persisted inventory UUID.  No public production input, CLI flag, dataclass field, environment variable, or fallback supplies an observed UUID.  Tests patch only `_apfs_volume_uuid_from_fd` at the module boundary: the normal path wraps/asserts the real reader was called, `"f" * 32` rejects against the persisted inventory UUID, and a query error fails the factory.  `R4ExternalProtectedParentCapability` implements `LegacyEvidenceCapability` exactly: public `logical_raw_root: PurePosixPath` fixed to `data/v5_1_bt/raw`, `inventory_sha256: str`, `records: tuple[LogicalProtectedRawRecord, ...]`, and `validate() -> None`, `rebind() -> None`, `read_bytes(logical_path: PurePosixPath) -> bytes`, `open_bound_object(logical_path: PurePosixPath) -> BinaryIO`, and idempotent `close() -> None`; it additionally exposes `observer_state() -> IssuanceState` for R4 readiness.  `validate` and `rebind` both re-open through descriptors and recheck APFS volume UUID, inode/ctime identities, all four locks, inventory membership, and no lock/temp issuance state; `rebind_and_validate()` and `open_record()` may remain aliases only.  `open_bound_object` requires a persisted full logical path under `data/v5_1_bt/raw`, validates membership, strips that exact prefix internally, then performs descriptor-relative access; `read_bytes` reads through it.  The capability reads only R4 schema records, has no `st_dev` compare path, and cannot accept an R3 inventory, a local execution copy, a hard link, a symlink, or a byte-identical different-volume snapshot.

Import `_register_issued_legacy_evidence` into the R4 factory and return its issued capability only after `validate()` succeeds.  Extend `_require_issued_legacy_evidence` in `v52_legacy.py` by adding a separate lazy import of `R4ExternalProtectedParentCapability` and an exact `type(capability) is R4ExternalProtectedParentCapability and _is_issued_legacy_evidence(capability)` branch.  The existing RootLocal/R3 branches stay byte-for-byte unchanged; the additive R4 branch rejects a forged object, subclass, or an unissued real object.  Add legacy and execution-registry tests for all three rejections and for an issued R4 object passing `_require_issued_legacy_evidence` and `execution_registry._admit_loader_legacy_evidence(..., require_r3=False)`, whose `.rebind()` is exercised by that registry/source admission path.

- [ ] **Step 4: Wire exact R4 acquisition paths and no-mutation plan-only test (5 minutes).**

```python
R4_CUSTODY_PARENT = Path("/Users/taejunoh/Documents/MLB Custody")
R4_CUSTODY_ROOT = R4_CUSTODY_PARENT / "v5_1_bt_r4"
R4_ISSUANCE_LOCK = R4_CUSTODY_PARENT / ".v5_1_bt_r4.issuance.lock"

def _entry_state(path: Path) -> tuple[object]:
    metadata = path.lstat()
    flags = getattr(metadata, "st_flags", None)
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(metadata.st_mode) else None
    return (stat.S_IFMT(metadata.st_mode), stat.S_IMODE(metadata.st_mode), metadata.st_nlink, metadata.st_size, metadata.st_ino, metadata.st_ctime_ns, flags, digest)

def _capture_tree(path: Path) -> tuple[tuple[str, tuple[object]]]:
    if not path.exists() and not path.is_symlink():
        return (("<ABSENT>", ()),)
    entries = [(".", _entry_state(path))]
    if path.is_dir() and not path.is_symlink():
        for child in sorted(path.rglob("*")):
            entries.append((child.relative_to(path).as_posix(), _entry_state(child)))
    return tuple(entries)

def _r4_authority_fingerprint(repository_root: Path) -> dict[str, tuple[tuple[str, tuple[object]]]]:
    targets = {
        "custody_root": R4_CUSTODY_ROOT,
        "issuance_lock": R4_ISSUANCE_LOCK,
        "issuance_temps": R4_CUSTODY_PARENT,
        "inventory": repository_root / "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json",
        "receipt": repository_root / "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json",
        "contract": repository_root / "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json",
        "raw": repository_root / "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw",
        "sources": repository_root / "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources",
        "execution_audit": repository_root / "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/execution",
        "reports": repository_root / "reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4",
    }
    snapshot = {name: _capture_tree(path) for name, path in targets.items() if name != "issuance_temps"}
    snapshot["issuance_temps"] = tuple((path.name, _entry_state(path)) for path in sorted(R4_CUSTODY_PARENT.glob(".v5_1_bt_r4.issuance.*.tmp")))
    return snapshot

def _r4_cli(repository_root: Path, *extra: str) -> list[str]:
    return [sys.executable, "-m", "mlb_backtest.v52_acquire", "--root", ".", "--run-spec", "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json", "--acquisition-contract", "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json", "--years", "2018:2024", "--mode", "hybrid", "--legacy-root", str(R4_CUSTODY_ROOT), "--protected-inventory", "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json", *extra]

class R4PlanOnlyTests(unittest.TestCase):
    def test_r4_plan_only_authenticates_captured_authority_without_mutation(self) -> None:
        repository_root = Path.cwd()
        before = _r4_authority_fingerprint(repository_root)
        result = subprocess.run(_r4_cli(repository_root, "--plan-only"), cwd=repository_root, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(_r4_authority_fingerprint(repository_root), before)
```

The fingerprint captures lstat file type/mode/nlink/size/inode/ctime, Darwin `st_flags` including `UF_IMMUTABLE`, regular-file bytes, directory topology, the actual absolute custody final root, its sibling lock, all matching sibling temps, canonical R4 inventory/receipt/contract, and every R4 runtime namespace.  The no-mutation assertion is limited to exactly this captured metadata/bytes/topology surface.  `_r4_cli` returns a concrete argument list and `subprocess.run` returns `subprocess.CompletedProcess[str]`.  Add an R4 branch beside—not inside—the exact R3 comparisons for RunSpec, contract, audit root, and `--legacy-root` admission.  The branch calls only `build_r4_external_protected_parent`; R3 keeps `build_external_protected_parent_capability` unchanged.

- [ ] **Step 5: Verify and commit capability admission (3 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_external_protected_parent tests.test_v52_legacy tests.test_execution_registry tests.test_v52_acquire tests.test_v52_acquire_e2e -v && python3.11 -m ruff check mlb_backtest/v52_r4_external_protected_parent.py mlb_backtest/v52_legacy.py mlb_backtest/v52_acquire.py tests/r4_issued_authority_fixture.py tests/test_v52_r4_external_protected_parent.py tests/test_v52_legacy.py tests/test_execution_registry.py tests/test_v52_acquire.py && git diff --check`

Expected: PASS; R4 plan-only succeeds solely on authenticated custody evidence, leaves the custody root, inventory, receipt, contract, raw namespace, sources namespace, execution-audit namespace, and reports namespace byte-for-byte/topology unchanged, and R3 continues to fail against the unavailable old inode/device root.

```bash
git add mlb_backtest/v52_r4_external_protected_parent.py mlb_backtest/v52_legacy.py mlb_backtest/v52_acquire.py tests/test_v52_r4_external_protected_parent.py tests/test_v52_legacy.py tests/test_execution_registry.py tests/test_v52_acquire.py tests/test_v52_acquire_e2e.py
git commit -m "feat: admit r4 protected parent capability"
```

### Task 15: Propagate R4 through registry and evidence

**Files:**

- Modify: `mlb_backtest/execution_registry.py: R4 constants, recovery variant, contract/checkpoint bindings`
- Modify: `mlb_backtest/v52_evidence.py: R4 authority artifact binding`
- Modify: `mlb_backtest/v52_game_evidence_inventory.py: R4 inventory admission`
- Modify: `mlb_backtest/v52_schedule_inventory.py: R4 schedule inventory admission`
- Modify: `tests/test_execution_registry.py`
- Modify: `tests/test_v52_evidence.py`
- Modify: `tests/test_v52_game_evidence_inventory.py`
- Modify: `tests/test_v52_schedule_inventory.py`
- Modify: `scripts/run_task13_exact_suite.py: CHILD_MODULES`
- Modify: `tests/test_task13_exact_suite_runner.py`

- [ ] **Step 1: Write red R4 terminal binding tests (4 minutes).**

```python
class R4RegistryEvidenceTests(unittest.TestCase):
    def test_r4_registry_requires_r4_contract_and_audit_prefix(self) -> None:
        checkpoint = _r4_checkpoint()
        checkpoint["artifact_bindings"]["acquisition_contract"]["path"] = _R3_CONTRACT_PATH
        with self.assertRaisesRegex(ValueError, "r4 acquisition contract path differs"):
            parse_execution_checkpoint(checkpoint)

    def test_r4_evidence_has_new_query_and_batch_identity(self) -> None:
        evidence = build_evidence(_r4_authority())
        self.assertEqual(evidence.run_id, _R4_RUN_ID)
        self.assertNotEqual(evidence.static_batch_id, _r3_evidence().static_batch_id)
```

In this same RED step, `_r4_checkpoint()` returns a deep-copied valid R4 checkpoint fixture, `_r4_authority()`/`_r3_evidence()` return parsed committed authority fixtures, and `build_evidence` is the existing tested constructor; no helper writes registry or audit files.

- [ ] **Step 2: Run the red propagation tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_execution_registry tests.test_v52_evidence tests.test_v52_game_evidence_inventory tests.test_v52_schedule_inventory -v`

Expected: R4 fixture parsing fails before its registry/evidence branch exists.

- [ ] **Step 3: Add explicit R4 variant tables and bindings (5 minutes).**

```python
_R4_RECOVERY_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4"

def _recovery_variant(authority: SourceRunSpec) -> Literal["r2", "r3", "r4"] | None:
    if authority.run_id == _R4_RECOVERY_RUN_ID:
        return "r4"
    return _existing_recovery_variant(authority)
```

Define R4 constants for exact RunSpec, contract, design, plan, audit, raw, and source prefixes.  Reuse the identical four recovery records, projection topology, excluded game `632924`, and parser-policy version from R3; alter only the authority identifiers/hashes and enforce R4 audit/raw paths.  No test fixture may use an R3 query id or batch id for R4.

- [ ] **Step 4: Append R4 modules to the authoritative exact suite (4 minutes).**

```python
_PRE_R4_CHILD_MODULES = (
    "tests.test_v52_source_schedule",
    "tests.test_v52_missing_venue_recovery_disposition",
    "tests.test_v52_recovery_r2_tdd",
    "tests.test_v52_evidence",
    "tests.test_v52_schedule_inventory",
    "tests.test_v52_legacy_decision_inventory",
    "tests.test_v52_timezone_review",
    "tests.test_v52_game_evidence_inventory",
    "tests.test_v52_query_plan",
    "tests.test_v52_verify_sources",
    "tests.test_v52_snapshot_store",
    "tests.test_execution_registry",
    "tests.test_v52_source_finalize",
    "tests.test_v52_source_history",
    "tests.test_feature_engine",
    "tests.test_protocols",
    "tests.test_v52_acquisition_contract",
    "tests.test_v52_external_protected_parent",
    "tests.test_verify_v52_design",
)

class Task13ExactSuiteRunnerTest(unittest.TestCase):
    def test_exact_suite_preserves_existing_order_and_appends_r4_modules(self) -> None:
        expected_r4 = (
            "tests.test_v52_r4_apfs",
            "tests.test_v52_r4_snapshot_issuer",
            "tests.test_v52_r4_custody_artifacts",
            "tests.test_v52_r4_external_protected_parent",
        )
        self.assertEqual(CHILD_MODULES[:len(_PRE_R4_CHILD_MODULES)], _PRE_R4_CHILD_MODULES)
        self.assertEqual(CHILD_MODULES[len(_PRE_R4_CHILD_MODULES):], expected_r4)
```

In this same step, append the four implemented R4 custody test module names at the end of `CHILD_MODULES`.  The literal `_PRE_R4_CHILD_MODULES` above is the exact pre-edit tuple; existing child order is unchanged and no R2/R3 child is deleted, moved, or renamed.  Task 18 appends the finalizer after these four, Task 19 appends plan-only, and Task 20 appends the terminal verifier.

Run: `python3.11 -m unittest tests.test_task13_exact_suite_runner -v`

Expected: PASS; the isolated runner still provisions and fingerprints its existing R2/R3 authority trees while its appended R4 children run after the unchanged sequence.

- [ ] **Step 5: Verify complete propagation and exact-suite inclusion, then commit (4 minutes).**

Run: `python3.11 -m unittest tests.test_execution_registry tests.test_v52_evidence tests.test_v52_game_evidence_inventory tests.test_v52_schedule_inventory tests.test_task13_exact_suite_runner -v && python3.11 -m ruff check mlb_backtest/execution_registry.py mlb_backtest/v52_evidence.py mlb_backtest/v52_game_evidence_inventory.py mlb_backtest/v52_schedule_inventory.py scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py && git diff --check`

Expected: PASS; R4 accepts only R4 terminal/checkpoint/evidence paths while R2/R3 assertions still pass.

```bash
git add mlb_backtest/execution_registry.py mlb_backtest/v52_evidence.py mlb_backtest/v52_game_evidence_inventory.py mlb_backtest/v52_schedule_inventory.py scripts/run_task13_exact_suite.py tests/test_execution_registry.py tests/test_v52_evidence.py tests/test_v52_game_evidence_inventory.py tests/test_v52_schedule_inventory.py tests/test_task13_exact_suite_runner.py
git commit -m "feat: propagate r4 registry evidence authority"
```

### Task 16: Verify full authority propagation

**Files:**

- Review only: R4 RunSpec/contract/audit artifacts and all modules changed in Tasks 9–12

- [ ] **Step 1: Run the combined authority suite (5 minutes).**

Run: `python3.11 -m unittest tests.test_v52_acquisition_contract tests.test_v52_query_plan tests.test_v52_r4_external_protected_parent tests.test_v52_acquire tests.test_execution_registry tests.test_v52_evidence tests.test_v52_game_evidence_inventory tests.test_v52_schedule_inventory -v`

Expected: PASS; no R4 object is admitted with R3 authority hashes, paths, inventory, query ID, or static batch ID.

- [ ] **Step 2: Prove R2/R3 source artifacts are unmodified (2 minutes).**

Run: `git diff --exit-code 94d1ec6fda0128bda1ecceae6e5c83df31a44898 -- audit/v5_1_bt protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json`

Expected: no output and exit 0; no R2/R3 artifact changed after the approved R4 design head.

- [ ] **Step 3: Record a no-change review checkpoint (2 minutes).**

Run: `git diff --check && git status --short`

Expected: no unstaged review edit and no whitespace failure.

### Task 17: Add R4 finalization/readiness without changing source semantics

**Files:**

- Modify: `mlb_backtest/v52_snapshot_store.py: R4 snapshot-store capability hooks`
- Modify: `mlb_backtest/v52_verify_sources.py: R4 trusted source verification`
- Modify: `mlb_backtest/v52_source_orchestration.py: R4 authority pair and readiness/rebind`
- Modify: `mlb_backtest/v52_source_finalize.py: exact R4 CLI/finalization policy`
- Modify: `tests/test_v52_snapshot_store.py`
- Modify: `tests/test_v52_verify_sources.py`
- Modify: `tests/test_v52_source_finalize.py`
- Modify: `tests/test_v52_acquire_e2e.py`

- [ ] **Step 1: Write red R4 readiness/finalization tests (4 minutes).**

```python
class R4FinalizationTests(unittest.TestCase):
    def test_r4_source_readiness_rebinds_custody_before_terminal_publication(self) -> None:
        self.assertEqual(_r4_source_readiness(_r4_context(), _r4_inventory()).status, "READY")

    def test_r4_finalization_rejects_lock_or_temp_state(self) -> None:
        with self.assertRaisesRegex(ValueError, "custody issuance incomplete"):
            finalize_sources(_r4_request(issuance_state="OBSERVER_INCOMPLETE_LOCKED"))
```

In this same RED step, `_r4_context()` returns a `SourceContext` holding a fake R4 capability with deterministic protocol `rebind()`; if the fixture retains `rebind_and_validate`, it delegates directly to `rebind()` rather than becoming the only implementation.  `_r4_inventory()` returns the matching `SourceInventory`, and `_r4_request()` returns a `FinalizeRequest` with only the supplied issuance state changed.

- [ ] **Step 2: Run the red finalization tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_snapshot_store tests.test_v52_verify_sources tests.test_v52_source_finalize tests.test_v52_acquire_e2e -v`

Expected: R4 readiness symbols are unavailable or reject the new fixture.

- [ ] **Step 3: Implement exact R4 readiness with immutable-custody checks (5 minutes).**

```python
def _r4_source_readiness(context: SourceContext, inventory: SourceInventory) -> SourceReadiness:
    capability = context.bindings.r4_external_protected_parent_capability
    if capability is None or context.run_spec.run_id != _R4_RECOVERY_RUN_ID:
        return SourceReadiness("AUTHORITY_MISSING", "r4 custody authority is missing", (), 3)
    capability.rebind()
    if capability.observer_state() not in {IssuanceState.EXECUTABLE, IssuanceState.RUNNING, IssuanceState.FINAL}:
        return SourceReadiness("AUTHORITY_BLOCKED", "custody issuance incomplete", (), 4)
    return SourceReadiness("READY", "r4 custody authority authenticated", (), 0)
```

The R4 branch shares existing source/query/recovery logic after readiness.  It invokes the shared `LegacyEvidenceCapability.rebind()` protocol method (whose R4 implementation performs the full UUID/descriptor validation); `rebind_and_validate()` may only be a behavior-identical alias.  It does not create a snapshot, clear a lock, alter a mode/flag, or reissue evidence.  Finalization requires an R4 contract/receipt/inventory re-read, exact artifact hashes, no lock/temp, and revalidated external capability.

- [ ] **Step 4: Verify no semantic drift and commit (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_snapshot_store tests.test_v52_verify_sources tests.test_v52_source_finalize tests.test_v52_acquire_e2e tests.test_v52_source_history tests.test_v52_source_pitching tests.test_v52_source_schedule tests.test_v52_source_statcast -v && python3.11 -m ruff check mlb_backtest/v52_snapshot_store.py mlb_backtest/v52_verify_sources.py mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_source_finalize.py && git diff --check`

Expected: PASS; existing parser/source behavior is unchanged and only R4 authority bindings differ.

```bash
git add mlb_backtest/v52_snapshot_store.py mlb_backtest/v52_verify_sources.py mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_source_finalize.py tests/test_v52_snapshot_store.py tests/test_v52_verify_sources.py tests/test_v52_source_finalize.py tests/test_v52_acquire_e2e.py
git commit -m "feat: finalize r4 custody source authority"
```

### Task 18: Implement R4 independent-review finalization service

**Files:**

- Create: `mlb_backtest/v52_r4_finalize.py`
- Create: `tests/test_v52_r4_finalize.py`
- Modify: `mlb_backtest/v52_source_finalize.py: _validate_authority_policy R4 branch`
- Modify: `mlb_backtest/v52_source_orchestration.py: _legacy_evidence_for_context R4 branch`
- Modify: `scripts/run_task13_exact_suite.py`, `tests/test_task13_exact_suite_runner.py`

- [ ] **Step 1: Write the explicit review/finalization sequence test (5 minutes).**

```python
import tempfile
import unittest
import json
from dataclasses import dataclass
from pathlib import Path

from mlb_backtest.v52_r4_finalize import finalize_r4
from mlb_backtest.v52_source_orchestration import (
    CommonArgs,
    FinalizationContext,
    build_context,
)


_R4_PROJECTION_QUERY_IDS: tuple[str, str, str] = (
    "1111111111111111111111111111111111111111111111111111111111111111",
    "2222222222222222222222222222222222222222222222222222222222222222",
    "3333333333333333333333333333333333333333333333333333333333333333",
)
_R4_RECORD_TO_PROJECTION_INDEXES: tuple[int, int, int, int] = (0, 1, 2, 2)
_R4_RECORD_GAME_PKS: tuple[int, int, int, int] = (565534, 630882, 633224, 632924)


def _record_query_ids(
    projection_query_ids: tuple[str, str, str],
) -> tuple[str, str, str, str]:
    first, second, third = projection_query_ids
    return first, second, third, third


_R4_RECORD_QUERY_IDS = _record_query_ids(_R4_PROJECTION_QUERY_IDS)
assert _R4_RECORD_QUERY_IDS == (
    _R4_PROJECTION_QUERY_IDS[0],
    _R4_PROJECTION_QUERY_IDS[1],
    _R4_PROJECTION_QUERY_IDS[2],
    _R4_PROJECTION_QUERY_IDS[2],
)


@dataclass(frozen=True)
class R4FinalizeFixture:
    root: Path
    common_args: CommonArgs
    logical_query_ids: tuple[str, ...]
    projection_query_ids: tuple[str, str, str]
    record_query_ids: tuple[str, str, str, str]
    record_to_projection_indexes: tuple[int, int, int, int]


def _build_r4_finalize_fixture(parent: Path) -> R4FinalizeFixture:
    """Build this module's authenticated R4 ACQUIRING terminal fixture."""
    root = (parent / "r4-finalize-authority").resolve()
    # Write canonical R4 RunSpec, contract, protected inventory/transition,
    # initialized Git authority, registry, source-verification checkpoint,
    # 81 committed snapshots, and the 42-entry source inventory directly in
    # this test module.  Bind every path to ``root`` and use the actual R4
    # acquisition/execution/source roots from that contract.  The checkpoint
    # is ACQUIRING/REMAINING with empty pending, quarantined, and split tuples;
    # its inventory has exactly the three distinct projection IDs below.  The
    # external review later selects one canonical authenticated subset ID.  It
    # also installs the
    # authenticated executable R4 external-custody capability.  Do not import
    # a production fixture helper or borrow an R2/R3 authority/query ID.
    _write_authenticated_r4_terminal_authority(
        root,
        projection_query_ids=_R4_PROJECTION_QUERY_IDS,
        record_query_ids=_R4_RECORD_QUERY_IDS,
        record_to_projection_indexes=_R4_RECORD_TO_PROJECTION_INDEXES,
        snapshot_count=81,
        source_season_count=42,
    )
    return R4FinalizeFixture(
        root=root,
        common_args=CommonArgs(
            root=root,
            run_spec=Path(
                "protocols/v5_2_qd/"
                "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json"
            ),
            acquisition_contract=Path(
                "protocols/v5_2_qd/"
                "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json"
            ),
            protected_inventory=Path(
                "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
                "acquisition/protected_raw_inventory_r4.json"
            ),
            legacy_root=root / "r4-custody/v5_1_bt_r4",
        ),
        logical_query_ids=_R4_PROJECTION_QUERY_IDS,
        projection_query_ids=_R4_PROJECTION_QUERY_IDS,
        record_query_ids=_R4_RECORD_QUERY_IDS,
        record_to_projection_indexes=_R4_RECORD_TO_PROJECTION_INDEXES,
    )


def _review_file_path(context: FinalizationContext) -> Path:
    authority = context.authority_context
    if authority is None:
        raise AssertionError("R4 finalize fixture requires source authority")
    return (
        context.root
        / authority.acquisition_audit_root.relative_to(context.root)
        / "source_reviews/independent_review_query_ids.json"
    )


class R4FinalizeTests(unittest.TestCase):
    def test_finalize_requires_explicit_approved_review_and_fresh_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = _build_r4_finalize_fixture(Path(temporary))
            with build_context(fixture.common_args) as context:
                review_file = _review_file_path(context)
            review_file.parent.mkdir(parents=True, exist_ok=True)
            review_file.write_bytes(
                json.dumps(
                    {"query_ids": [fixture.logical_query_ids[0]]},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode() + b"\n"
            )
            result = finalize_r4(fixture.common_args, review_query_ids_file=review_file, review_decision="APPROVED", lock_owner="operator")
            self.assertEqual(result.phase, "SOURCES_READY")
            self.assertTrue(result.review.commit_path.is_file())
```

`_write_authenticated_r4_terminal_authority(root: Path, *, projection_query_ids: tuple[str, str, str], record_query_ids: tuple[str, str, str, str], record_to_projection_indexes: tuple[int, int, int, int], snapshot_count: int, source_season_count: int) -> None` is declared immediately above this helper in the same test module; it performs the listed direct writes and returns only after `build_context` authenticates them.  It requires three distinct lowercase 64-hex projection IDs, four recovery records for game PKs `(565534, 630882, 633224, 632924)`, and exactly `(0, 1, 2, 2)`.  It derives `record_query_ids` as `(projection[0], projection[1], projection[2], projection[2])`, therefore requires exactly three unique record IDs with the third equal to the fourth.  It writes the canonical R4 contract/recovery-disposition with those four records and mapping, writes the terminal checkpoint/query batch and source inventory with `logical_query_ids == projection_query_ids`, and binds all 81 snapshots/42 source-seasons to that same R4 topology.  It is test-local, does not import a fixture from production, and exposes the precise valid `logical_query_ids` through `R4FinalizeFixture`.  `_review_file_path` is likewise test-local: it derives the documented acquisition review namespace from public context fields and never imports/calls production-private `_review_prefix`.  The selection test writes the first projection ID only: it is deliberately nonempty, canonical, sorted, and an authenticated subset rather than an assertion that the inventory has one query.

Define `R4FinalizeResult(review: ReceiptReport, replays: tuple[ReplayReport, ReplayReport], phase: str, checkpoint: Path, inventory: Path)` and `R4TerminalFacts(phase: str, snapshot_count: int, source_season_count: int, pending_query_count: int, quarantined_query_count: int, split_query_count: int)` before use.  Test a missing review file, any decision other than APPROVED, missing lock owner, and an attempted automatic approval; all fail before publication.  Mock `write_review` and inspect `ReviewRequest` for fixed identity/role `independent-source-auditor`.  Mock `build_context` and assert exactly six calls in this lifecycle: one fresh context for terminal-check/preflight/publish, then one each for review, replay one, replay two, advance, and the read-only terminal core.

- [ ] **Step 2: Implement the exact service and CLI (5 minutes).**

`finalize_r4` opens its first context, requires terminal `REMAINING` with pending/quarantine/split all zero, calls `run_preflight` and requires `status == "PASS"`, `snapshot_count == 81`, and `network_attempts == 0`; it additionally requires `context.inventory is not None` and `len(context.inventory.source_seasons) == 42`, then calls `publish_inventory(context, lock_owner=lock_owner)` in that same context.  The service creates exactly one 32-lowercase-hex `execution_nonce` before any write.  Given that nonce, it derives exclusive output leaves (all under public R4 authority roots) as `acquisition_audit_root / "source_reviews" / f"r4-finalize-{execution_nonce}.review.json"`, `... / f"r4-finalize-{execution_nonce}.review.commit.json"`, and `acquisition_audit_root / "source_replays" / f"r4-finalize-{execution_nonce}.replay-01.json"`, `...replay-01.commit.json`, `...replay-02.json`, `...replay-02.commit.json`.  It rejects a nonce outside that format, any duplicate output path, a path equal to the externally supplied review-selection file, or an existing/symlink destination before publication; a collision raises without retry or alternate name.

It then creates a fresh context for `write_review` using `ReviewRequest(reviewer_identity="independent-source-auditor", reviewer_role="independent-source-auditor", decision="APPROVED", query_ids_file=review_file, write_receipt=review_receipt, write_commit=review_commit)`, then fresh contexts for the two `write_replay` calls with their matching derived receipt/commit paths.  In a fifth context it calls `advance_sources_ready` with `review_receipts=(review.commit_path,)`, `replay_receipts=(replay_one.commit_path, replay_two.commit_path)`, checkpoint `context.root / _checkpoint_path(context.checkpoint, context.authority_context)`, and inventory `context.authority_context.sources_root / "source_inventory.json"`.  The sixth and final context is owned by this shared read-only core, which both finalization and Task 20 use:

```python
from mlb_backtest import execution_registry
from mlb_backtest.v52_snapshot_store import load_snapshot_store
from mlb_backtest.v52_source_orchestration import _checkpoint_path


@dataclass(frozen=True)
class R4TerminalFacts:
    phase: str
    snapshot_count: int
    source_season_count: int
    pending_query_count: int
    quarantined_query_count: int
    split_query_count: int


def verify_r4_sources_ready_core(
    common_args: CommonArgs,
    *,
    review_commit: Path,
    replay_commits: tuple[Path, Path],
) -> R4TerminalFacts:
    with build_context(common_args) as context:
        if context.run_spec is None or context.checkpoint is None or context.snapshot_inputs is None or context.authority_context is None:
            raise ValueError("R4 terminal authority is unavailable")
        checkpoint = context.checkpoint
        inventory_path = context.authority_context.sources_root / "source_inventory.json"
        checkpoint_path = context.root / _checkpoint_path(checkpoint, context.authority_context)
        execution_registry.validate_sources_ready_evidence_set(
            context.root, context.run_spec, inventory_path=inventory_path,
            checkpoint_path=checkpoint_path,
            validation_receipts=(review_commit, *replay_commits),
            legacy_evidence=context.legacy_evidence,
        )
        store = load_snapshot_store(
            context.authority_context.sources_root, context.snapshot_inputs,
            expected_root=context.authority_context.sources_root,
        )
        facts = R4TerminalFacts(
            context.phase, len(store.snapshots), len(store.inventory.source_seasons),
            len(checkpoint.pending_query_ids), len(checkpoint.quarantined_query_ids),
            len(checkpoint.split_query_ids),
        )
    if (facts.phase, facts.snapshot_count, facts.source_season_count, facts.pending_query_count, facts.quarantined_query_count, facts.split_query_count) != ("SOURCES_READY", 81, 42, 0, 0, 0):
        raise ValueError("R4 terminal state is not exact SOURCES_READY")
    return facts
```

`finalize_r4` calls that core with returned commit paths and returns only those report paths in its handoff JSON; no guessed query IDs or artifact IDs are accepted.  Test destination collision and a same-nonce retry: both fail closed before `write_review`, leave the external selection unchanged, and do not silently generate a second artifact set.

The CLI requires `--review-query-ids-file`, `--review-decision APPROVED`, and `--lock-owner`; it has fixed reviewer identity/role `independent-source-auditor`.  The independent review command is run only after a fresh evidence/subagent review has generated canonical JSON whose `query_ids` array is lexicographically sorted; it never selects queries or approves itself.  Add exact R4 branches to `_validate_authority_policy` and `_legacy_evidence_for_context`, preserving existing R2/R3 branches.

- [ ] **Step 3: Verify and commit the service (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_finalize tests.test_v52_source_finalize tests.test_v52_source_orchestration -v && python3.11 -m ruff check mlb_backtest/v52_r4_finalize.py tests/test_v52_r4_finalize.py mlb_backtest/v52_source_finalize.py mlb_backtest/v52_source_orchestration.py && git diff --check`

Expected: PASS; the service has no auto-approval and uses returned report paths only.

Append `tests.test_v52_r4_finalize` immediately after the four custody children in `CHILD_MODULES`; the authoritative order is custody4 → finalize → plan_only → terminal_verify.  Update `test_task13_exact_suite_runner.py` to assert that prefix and retain every existing R2/R3 child unchanged.

```bash
git add mlb_backtest/v52_r4_finalize.py tests/test_v52_r4_finalize.py mlb_backtest/v52_source_finalize.py mlb_backtest/v52_source_orchestration.py scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "feat: finalize r4 sources after independent review"
```

### Task 19: Mandatory R4 plan-only execution

**Files:**

- Create: `scripts/run_v52_r4_recovery.sh`
- Create: `tests/test_v52_r4_plan_only.py`
- Modify: `tests/test_v52_acquire_e2e.py`
- Modify: `scripts/run_task13_exact_suite.py: append plan-only child after existing R4 children`
- Modify: `tests/test_task13_exact_suite_runner.py`

- [ ] **Step 1: Write red command-contract test (3 minutes).**

```python
class R4RunnerTests(unittest.TestCase):
    def test_r4_runner_plan_only_contains_exact_authority_paths(self) -> None:
        script = Path("scripts/run_v52_r4_recovery.sh").read_text(encoding="utf-8")
        self.assertIn("--root .", script)
        self.assertIn("historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json", script)
        self.assertIn("historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json", script)
        self.assertIn("--years 2018:2024", script)
        self.assertIn("--mode hybrid", script)
        self.assertIn("--legacy-root '/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4'", script)
        self.assertIn("--protected-inventory audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json", script)
        self.assertNotIn("missing_venue_r3", script)
```

- [ ] **Step 2: Run the red runner test (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_plan_only -v`

Expected: file-not-found failure because the R4 runner does not exist.

- [ ] **Step 3: Create the narrow plan-only-safe runner (3 minutes).**

```bash
#!/bin/zsh
set -euo pipefail
python3.11 -m mlb_backtest.v52_acquire \
  --root . \
  --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json \
  --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json \
  --years 2018:2024 \
  --mode hybrid \
  --legacy-root '/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4' \
  --protected-inventory audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json \
  "$@"
```

The runner fixes every required acquisition argument above and forwards only `"$@"` for `--plan-only`, resume, and other supported runtime switches.  It contains no issuance command and does not modify an R2/R3 script.

- [ ] **Step 4: Execute mandatory plan-only and prove custody/read-only state (5 minutes).**

Run: `zsh scripts/run_v52_r4_recovery.sh --plan-only && python3.11 -m unittest tests.test_v52_r4_plan_only tests.test_v52_acquire_e2e -v && git diff --check`

Expected: plan-only exits 0 after authenticating all R4 authority documents and custody descriptors; the Task 13 `_r4_authority_fingerprint` before/after comparison proves no snapshot member, lock, temp root, inventory, receipt, contract, raw/sources/audit/reports namespace, mode, flag, or R2/R3 file changed.

- [ ] **Step 5: Append the runner test to the exact suite, verify, and commit (4 minutes).**

```python
class R4ExactSuiteAppendTests(unittest.TestCase):
    def test_plan_only_child_is_appended_after_prior_exact_children(self) -> None:
        self.assertEqual(CHILD_MODULES[-1], "tests.test_v52_r4_plan_only")
        self.assertEqual(CHILD_MODULES[-2], "tests.test_v52_r4_finalize")
        self.assertEqual(CHILD_MODULES[-3], "tests.test_v52_r4_external_protected_parent")
```

In this same step, `CHILD_MODULES` receives exactly one final appended item, `tests.test_v52_r4_plan_only`, after the finalizer and the four custody children; it does not reorder the prior 19 R2/R3 children.

Run: `python3.11 -m unittest tests.test_v52_r4_plan_only tests.test_task13_exact_suite_runner -v && git diff --check`

Expected: PASS; Task 13 exact-suite isolation/fingerprints remain intact and now execute every new R4 test module in append-only order.

```bash
git add scripts/run_v52_r4_recovery.sh scripts/run_task13_exact_suite.py tests/test_v52_r4_plan_only.py tests/test_v52_acquire_e2e.py tests/test_task13_exact_suite_runner.py
git commit -m "feat: add r4 plan-only runner"
```

### Task 20: Add an authenticated R4 terminal verifier

**Files:**

- Create: `mlb_backtest/v52_r4_terminal_verify.py`
- Create: `tests/test_v52_r4_terminal_verify.py`
- Modify: `mlb_backtest/v52_r4_finalize.py: export shared read-only terminal core`
- Modify: `scripts/run_task13_exact_suite.py: append terminal verifier child`
- Modify: `tests/test_task13_exact_suite_runner.py`

- [ ] **Step 1: Write executable terminal-report tests (4 minutes).**

```python
class R4TerminalVerifierTests(unittest.TestCase):
    def test_report_requires_sources_ready_and_exact_population(self) -> None:
        report = verify_r4_terminal(self._sources_ready_fixture())
        self.assertEqual(report.phase, "SOURCES_READY")
        self.assertEqual((report.snapshot_count, report.source_season_count, report.pending_query_count, report.quarantined_query_count), (81, 42, 0, 0))

    def test_report_rejects_nonterminal_or_wrong_population(self) -> None:
        with self.assertRaisesRegex(ValueError, "SOURCES_READY"):
            verify_r4_terminal(self._sources_ready_fixture(phase="RUNNING"))
```

In this same step, `_sources_ready_fixture()` returns a concrete frozen `R4TerminalVerifyInputs` fixture with authenticated CommonArgs, review commit path, two replay commit paths, authenticated terminal registry/context phase, 81 snapshot identifiers, 42 source-season identifiers, and empty pending/quarantine/split tuples; keyword overrides replace exactly one field.

- [ ] **Step 2: Run the red terminal-verifier tests (2 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_terminal_verify -v`

Expected: import failure because the verifier does not exist.

- [ ] **Step 3: Implement the narrow verifier and CLI (5 minutes).**

```python
from mlb_backtest.v52_r4_finalize import verify_r4_sources_ready_core

@dataclass(frozen=True)
class R4TerminalVerifyInputs:
    common_args: CommonArgs
    review_commit: Path
    replay_commits: tuple[Path, Path]

@dataclass(frozen=True)
class R4TerminalReport:
    phase: str
    snapshot_count: int
    source_season_count: int
    pending_query_count: int
    quarantined_query_count: int

def verify_r4_terminal(inputs: R4TerminalVerifyInputs) -> R4TerminalReport:
    facts = verify_r4_sources_ready_core(
        inputs.common_args,
        review_commit=inputs.review_commit,
        replay_commits=inputs.replay_commits,
    )
    return R4TerminalReport(
        facts.phase, facts.snapshot_count, facts.source_season_count,
        facts.pending_query_count, facts.quarantined_query_count,
    )
```

Task 20 imports and reuses Task 18's only read-only terminal implementation; it must not reimplement registry validation, checkpoint derivation, snapshot loading, or the 81/42/0/0 check.  Its `main(argv)` requires `--root`, `--run-spec`, `--acquisition-contract`, `--legacy-root`, `--protected-inventory`, `--review-commit`, two `--replay-commit` values, and `--json`; it creates `R4TerminalVerifyInputs`, emits exactly the five `R4TerminalReport` fields, never writes a checkpoint, and never repairs custody.

- [ ] **Step 4: Append verifier test in exact-suite order and commit (4 minutes).**

Run: `python3.11 -m unittest tests.test_v52_r4_terminal_verify tests.test_task13_exact_suite_runner -v && python3.11 -m ruff check mlb_backtest/v52_r4_terminal_verify.py tests/test_v52_r4_terminal_verify.py scripts/run_task13_exact_suite.py && git diff --check`

Expected: PASS; `CHILD_MODULES[-3:] == ("tests.test_v52_r4_finalize", "tests.test_v52_r4_plan_only", "tests.test_v52_r4_terminal_verify")`, with all earlier children byte-for-byte ordered as asserted in Tasks 15, 18, and 19.

```bash
git add mlb_backtest/v52_r4_finalize.py mlb_backtest/v52_r4_terminal_verify.py tests/test_v52_r4_terminal_verify.py scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "feat: verify r4 terminal state"
```

### Task 21: Execute the first real R4 recovery run and seal verification

**Files:**

- Create at runtime: `data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw/**`
- Create at runtime: `data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources/**`
- Create at runtime: `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/execution/**`
- Require external input at runtime: `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/source_reviews/independent_review_query_ids.json`
- Create at runtime: `reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/**`
- Modify: no implementation source unless a failing test identifies a defect; repair follows a new red test and repeats relevant Tasks 13–20 verification.

- [ ] **Step 1: Run the exact R4 execution command (5 minutes).**

Run: `zsh scripts/run_v52_r4_recovery.sh`

Expected: only R4 namespaces receive new execution artifacts and the authenticated terminal state is `REMAINING` or `RUNNING` with 81 snapshots, 42 source-season artifacts, and pending/quarantine/split all zero.  Acquisition alone does not claim `SOURCES_READY` or `FINAL`.

- [ ] **Step 2: Create the independent review selection (4 minutes).**

Run: `REVIEW_SELECTION='audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/source_reviews/independent_review_query_ids.json'; test -f "$REVIEW_SELECTION" && python3.11 -c "import json,sys; p=sys.argv[1]; raw=open(p,'rb').read(); value=json.loads(raw); expected=(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()+b'\n'); ids=value.get('query_ids'); assert set(value)=={'query_ids'} and raw==expected and isinstance(ids,list) and ids and ids==sorted(set(ids)); assert set(ids).issubset(set(json.load(open('audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources/source_inventory.json'))['logical_query_ids']))" "$REVIEW_SELECTION"`

Expected: an externally supplied, canonical independent-review selection artifact exists after human/independent audit.  The command stops when it is absent, empty while authority inventory has queries, non-canonical, duplicated, or outside the authenticated authority query set; it neither creates selection nor approves it.

- [ ] **Step 3: Run explicit R4 finalization and save its handoff JSON (5 minutes).**

Run: `set -o pipefail; REVIEW_SELECTION='audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/source_reviews/independent_review_query_ids.json'; python3.11 -m mlb_backtest.v52_r4_finalize --root . --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json --legacy-root '/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4' --protected-inventory audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json --review-query-ids-file "$REVIEW_SELECTION" --review-decision APPROVED --lock-owner r4-source-finalizer --json | tee /private/tmp/r4-finalize-result.json`

Expected: JSON has exactly `review_commit`, `replay_commits` with two paths, `checkpoint`, `inventory`, and `phase` equal to `SOURCES_READY`; the service performs publish/review/replay/replay/advance once.

- [ ] **Step 4: Verify terminal state using returned commit paths (5 minutes).**

Run: `python3.11 -c "import json,subprocess,sys; r=json.load(open('/private/tmp/r4-finalize-result.json')); assert set(r)=={'review_commit','replay_commits','checkpoint','inventory','phase'} and r['phase']=='SOURCES_READY' and len(r['replay_commits'])==2; cmd=[sys.executable,'-m','mlb_backtest.v52_r4_terminal_verify','--root','.','--run-spec','protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json','--acquisition-contract','protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json','--legacy-root','/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4','--protected-inventory','audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json','--review-commit',r['review_commit'],'--replay-commit',r['replay_commits'][0],'--replay-commit',r['replay_commits'][1],'--json']; subprocess.run(cmd, check=True)"`

Expected: terminal verifier receives all required returned paths and reports exactly `SOURCES_READY`, 81, 42, 0, 0.

- [ ] **Step 5: Verify final custody, run tests/guards, and commit evidence (5 minutes).**

Run: `python3.11 -m mlb_backtest.v52_r4_snapshot_issuer verify --custody-root '/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4' --inventory audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json --receipt audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json --contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json && python3.11 -m unittest discover -s tests -p 'test_*.py' -v && python3.11 -m ruff check mlb_backtest tests && git diff --check`

Expected: custody state `FINAL`, no lock/temp, all 4,711 physical descriptor identities plus four same-UUID root locks and the complete 5,025 binding closure match, full suite exits 0 (the prior clean baseline was 1,458 tests with 2 skips; update the count only for intentional new R4 tests).

- [ ] **Step 6: Run final R2/R3 diff guard (3 minutes).**

Run: `python3.11 -m ruff check mlb_backtest tests && git diff --check && git diff --exit-code 94d1ec6fda0128bda1ecceae6e5c83df31a44898 -- audit/v5_1_bt protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json`

Expected: Ruff and whitespace checks exit 0; inspect the final diff guard and require it to show no R2/R3 content change before staging R4 run artifacts.

- [ ] **Step 7: Commit the authenticated R4 execution evidence (2 minutes).**

```bash
git add data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4 audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4 reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4
git commit -m "audit: execute r4 recovery authority"
```

Expected: the commit contains only R4 execution artifacts validated in Steps 1–3.

## Completion verification

- [ ] `python3.11 -m unittest discover -s tests -p 'test_*.py' -v` exits 0.
- [ ] `python3.11 -m ruff check mlb_backtest tests` exits 0.
- [ ] `git diff --check` exits 0.
- [ ] R4 inventory/receipt/contract deserialize from canonical newline-terminated bytes and bind the expected committed design, plan, and RunSpec identities.
- [ ] A remount-only synthetic identity change passes; a same-byte cross-volume snapshot fails; all lock/temp/post-rename states fail closed according to the issuer state machine.
- [ ] The R4 `--plan-only` command authenticates the real snapshot without mutation, and the first real run reaches atomic `SOURCES_READY` with 81 snapshots, 42 source-season pairs, zero pending queries, and zero quarantined queries.
- [ ] R2/R3 protected inventory, RunSpec, contract, parser policy, and artifacts are byte-for-byte unchanged.

Plan complete and saved to `docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md`. Two execution options: subagent-driven implementation (recommended) with a fresh worker and two-stage review per task, or inline execution through `superpowers:executing-plans` with the three review gates above.
