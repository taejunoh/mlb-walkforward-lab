# v5.2 R4 Disjoint Custody Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one closed `v5.2-r4-disjoint-custody-v2` issuance and verification path that creates a fully immutable R4 snapshot outside File Provider storage while preserving the old incomplete root and lock as read-only forensic evidence.

**Architecture:** Keep existing R2/R3 and non-v2 R4 behavior byte-compatible. Put the new closed namespace, platform checks, execution binding, old-attempt fingerprint, journal, artifacts, anchor, and issuer in focused `v52_r4_disjoint_*` modules; reuse only stable APFS/source-authority/canonicalization primitives from existing R4 modules. The implementation phase uses disposable APFS fixtures only; the real v2 preflight and one-shot issuance are a separately authorized rollout after all code, reviews, and exact gates are green.

**Tech Stack:** Python 3.11, Darwin descriptor APIs (`openat`, `fstat`, `flistxattr`, `getattrlist`/Foundation resource values, `fchflags`, `fsync_volume_np`), APFS identity helpers, canonical JSON/NDJSON, Ed25519 via `cryptography`, JSON Schema Draft 2020-12, `unittest`, Ruff, Git plumbing.

---

## Global Safety Constraints

- Work only in `/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd` on a clean `codex/v52-recovery-run-r2-tdd` branch.
- Run Python with `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11`.
- Tests substitute disposable paths for both old and new custody locations. No implementation or test command may inspect or mutate the real old root, old lock, new parent, new final root, production signing seed, or six production repository outputs.
- The real old root and lock are permanently excluded from cleanup, chmod/chflags, rename, unlink, owner acquisition, and nonce reads. Production code may perform only the approved bounded no-follow metadata/content fingerprint.
- No production `provision-custody-parent`, `preflight`, `issue-and-bind`, or `verify` runs are part of Tasks 1–14.
- Do not add recovery escape hatches: `resume`, `adopt`, `recover`, `unlock`, `cleanup`, `old-custody`, `allow-file-provider`, `force`, caller-selected journal paths, generations, final names, or production authority digests.
- A failed v2 transaction is evidence. It retains journal/lock/temp/final/static artifacts exactly as the state machine specifies and is never retried automatically.
- Use TDD in every implementation task: create a focused RED test, confirm the intended failure, add the minimum production behavior, confirm focused GREEN, run the affected compatibility tests, then commit only the listed files.

## File Map

| Path | Responsibility |
| --- | --- |
| `mlb_backtest/v52_r4_disjoint_policy.py` | Closed v2 constants, exact path/generation validation, state names, and six repository artifact paths. |
| `mlb_backtest/v52_r4_disjoint_platform.py` | Descriptor-bound local APFS/File Provider rejection and target Volume UUID binding. |
| `mlb_backtest/v52_r4_disjoint_execution.py` | Python executable identity/hash plus clean Git HEAD/tree/blob binding and rebind. |
| `mlb_backtest/v52_r4_forensic_fingerprint.py` | Bounded read-only no-follow fingerprint of the fixed old incomplete root/lock; never reads lock contents. |
| `mlb_backtest/v52_r4_disjoint_journal.py` | Canonical seed-free NDJSON records, event schema, hash chain, exclusive writer, fsync/rebind, close/freeze, and verifier. |
| `mlb_backtest/v52_r4_disjoint_artifacts.py` | v2 full directory/leaf manifests, closed v2 inventory/receipt documents, publication, and frozen-tree verification. |
| `mlb_backtest/v52_r4_disjoint_anchor.py` | v2 signed anchor shape, inline supersession fingerprint, journal and execution bindings, signature-first verification. |
| `mlb_backtest/v52_r4_disjoint_issuer.py` | Closed v2 state/owner layout, journaled issuance orchestration, six-artifact release bundle, exact CLI, and fresh verification. |
| `mlb_backtest/v52_r4_snapshot_issuer.py` | Existing non-v2 R4 issuer; unchanged production behavior and compatibility target. |
| `mlb_backtest/v52_r4_custody_artifacts.py` | Existing non-v2 R4 artifacts plus reusable canonical/APFS primitives; unchanged v1 schemas and paths. |
| `mlb_backtest/v52_r4_signed_anchor.py` | Existing non-v2 R4 signed anchor; unchanged v1 domain/schema/paths. |
| `mlb_backtest/v52_source_contracts.py` | Separate closed validation for the v2 acquisition contract. |
| `mlb_backtest/v52_query_plan.py` | Exact `(RunSpec, contract)` authority-pair selection while reusing existing R4 query semantics. |
| `schemas/v5_2_acquisition_contract.schema.json` | One additional closed v2 contract variant; existing variants remain unchanged. |
| `scripts/run_task13_exact_suite.py` | Append the new v2 test modules to the authoritative child suite. |
| `schemas/v5_2_r4_disjoint_custody_journal_record.schema.json` | Closed schema for one canonical journal record. |
| `schemas/v5_2_r4_disjoint_protected_raw_inventory.schema.json` | Closed v2 inventory including generation, directory manifest, journal, and execution binding. |
| `schemas/v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json` | Closed v2 transition receipt. |
| `schemas/v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json` | Closed v2 signed anchor. |
| `tests/test_v52_r4_disjoint_policy.py` | Exact namespace, generation, artifact paths, and state-name tests. |
| `tests/test_v52_r4_disjoint_platform.py` | File Provider, APFS, descriptor rebind, and provisioning-preflight tests. |
| `tests/test_v52_r4_disjoint_execution.py` | Executable and clean Git binding tests. |
| `tests/test_v52_r4_forensic_fingerprint.py` | Old-evidence isolation, no-follow traversal, digest, and no-lock-content tests. |
| `tests/test_v52_r4_disjoint_journal.py` | Journal schema, ordering, hash chain, durability, close, and failure tests. |
| `tests/test_v52_r4_disjoint_custody.py` | Full v2 artifact, issuer, race/failure, CLI, verification, and compatibility tests. |
| `tests/r4_issued_authority_fixture.py` | Test-only v2 signing/profile construction through the existing private authority seam. |

## Closed Interfaces

The following interfaces are fixed for the plan. Later tasks must use these names and types rather than inventing alternate seams:

| Interface | Exact result |
| --- | --- |
| `require_disjoint_policy(*, generation: str, custody_parent: Path, final_name: str) -> DisjointCustodyPolicy` | Frozen generation/parent/final/lock/journal/six-artifact layout. |
| `preflight_local_apfs_parent(parent: Path, *, allow_missing_leaf: bool) -> str` | Canonical accepted target APFS Volume UUID. |
| `capture_execution_binding(repository: Path, executable: Path, *, target_volume_uuid: str, supersession_sha256: str) -> ExecutionBinding` | Frozen executable and Git authority binding. |
| `capture_incomplete_attempt() -> Mapping[str, object]` | Fixed production old-attempt read-only fingerprint. |
| `capture_incomplete_attempt_for_test(*, old_parent: Path, old_final_name: str) -> Mapping[str, object]` | Private disposable-fixture fingerprint seam. |
| `DisjointJournalWriter.append(*, event: str, outcome: str, detail: Mapping[str, object]) -> Mapping[str, object]` | One canonical, fsynced, rebound hash-chain record. |
| `DisjointJournalWriter.close_pre_anchor(*, snapshot_manifest_sha256: str, inventory_sha256: str, receipt_sha256: str, contract_sha256: str) -> Mapping[str, object]` | Frozen journal summary with payload and terminal-event hashes. |
| `verify_journal_bytes(payload: bytes, *, execution_binding_sha256: str, target_volume_uuid: str) -> Mapping[str, object]` | Validated terminal journal summary. |
| `_CapturedDisjointSnapshot` | Factory-only owner-bound records, members, bindings, four locks, snapshot row, ordered directory rows, directory-manifest SHA-256, and target Volume UUID. |
| `DisjointIssuanceState` | Closed `ABSENT`, `OBSERVER_INCOMPLETE_UNOWNED`, `OBSERVER_INCOMPLETE_LOCKED`, `OBSERVER_INCOMPLETE_PUBLISHED`, and `FINAL` observer states. |

### Task 1: Lock the v2 namespace and schemas

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_policy.py`
- Create: `schemas/v5_2_r4_disjoint_custody_journal_record.schema.json`
- Create: `schemas/v5_2_r4_disjoint_protected_raw_inventory.schema.json`
- Create: `schemas/v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json`
- Create: `schemas/v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json`
- Create: `tests/test_v52_r4_disjoint_policy.py`

- [ ] **Step 1: Write RED constant and closed-schema tests.**

Add tests that import the exact constants, require the fixed parent/final/generation, assert the six exact repository paths, and reject omitted/extra properties plus every old R4 generation/path. The policy test must use a substituted test policy factory; production `require_disjoint_policy` must accept only the fixed literals.

```python
def test_production_policy_is_closed() -> None:
    policy = require_disjoint_policy(
        generation="v5.2-r4-disjoint-custody-v2",
        custody_parent=Path("/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2"),
        final_name="v5_1_bt_r4",
    )
    assert policy.owner_lock_name == ".v5_1_bt_r4.disjoint-v2.issuance.lock"
    assert policy.journal_name == ".v5_1_bt_r4.disjoint-v2.issuance.journal.ndjson"
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_policy -v
```

Expected: import failure because `v52_r4_disjoint_policy` and the four v2 schemas do not exist.

- [ ] **Step 3: Implement the minimal closed policy and schemas.**

Define immutable constants and validate raw values before `Path` normalization. JSON schemas use `additionalProperties: false`, fixed `const` values for generation/kind/version/path fields, lowercase hash patterns, positive counts, and exact nested required sets. Do not expose a generic constructor that can authorize a different production parent.

```python
GENERATION = "v5.2-r4-disjoint-custody-v2"
FINAL_NAME = "v5_1_bt_r4"
CUSTODY_PARENT = Path("/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2")
OWNER_LOCK_NAME = ".v5_1_bt_r4.disjoint-v2.issuance.lock"
JOURNAL_NAME = ".v5_1_bt_r4.disjoint-v2.issuance.journal.ndjson"
DISJOINT_ARTIFACT_PATHS = {
    "inventory": PurePosixPath("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4_disjoint_v2.json"),
    "receipt": PurePosixPath("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4_disjoint_v2.json"),
    "contract": PurePosixPath("protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json"),
    "anchor": PurePosixPath("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4_disjoint_v2.json"),
    "signature": PurePosixPath("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4_disjoint_v2.ed25519"),
    "journal": PurePosixPath("audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/custody_issuance_journal_r4_disjoint_v2.ndjson"),
}

@dataclass(frozen=True)
class DisjointCustodyPolicy:
    generation: str
    custody_parent: Path
    final_name: str
    owner_lock_name: str
    journal_name: str
    artifact_paths: Mapping[str, PurePosixPath]

PRODUCTION_POLICY = DisjointCustodyPolicy(
    generation=GENERATION,
    custody_parent=CUSTODY_PARENT,
    final_name=FINAL_NAME,
    owner_lock_name=OWNER_LOCK_NAME,
    journal_name=JOURNAL_NAME,
    artifact_paths=MappingProxyType(DISJOINT_ARTIFACT_PATHS),
)

def require_disjoint_policy(*, generation: str, custody_parent: Path, final_name: str) -> DisjointCustodyPolicy:
    if generation != GENERATION or custody_parent != CUSTODY_PARENT or final_name != FINAL_NAME:
        raise CUSTODY_UNAVAILABLE("disjoint custody policy differs")
    return PRODUCTION_POLICY
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_policy -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_policy.py tests/test_v52_r4_disjoint_policy.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_policy.py schemas/v5_2_r4_disjoint_custody_journal_record.schema.json schemas/v5_2_r4_disjoint_protected_raw_inventory.schema.json schemas/v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json schemas/v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json tests/test_v52_r4_disjoint_policy.py
git commit -m "feat: close r4 disjoint custody namespace"
```

Expected: all focused tests and static checks pass; the commit contains only the six listed files.

### Task 2: Reject File Provider and bind one local APFS volume

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_platform.py`
- Modify: `mlb_backtest/v52_r4_apfs.py`
- Create: `tests/test_v52_r4_disjoint_platform.py`
- Modify: `tests/test_v52_r4_apfs.py`

- [ ] **Step 1: Write RED descriptor-bound platform tests.**

Cover `NSURLIsUbiquitousItemKey`, local/internal/removable/read-only volume values, forbidden xattr prefixes, unavailable APIs, contradictory second reads, ancestor symlinks, cross-volume components, non-APFS, and the one missing final `custody-v2` leaf permitted only to provisioning. Assert rejection occurs before any fixture journal/lock/temp/final/repository output exists.

```python
for resource_values, code in REJECTED_RESOURCE_CASES:
    with self.subTest(code=code), self.assertRaisesRegex(
        CUSTODY_FILE_PROVIDER_REJECTED, code
    ):
        preflight_local_apfs_parent(candidate, allow_missing_leaf=False)
    self.assertEqual([], list(candidate_parent.iterdir()))
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_platform tests.test_v52_r4_apfs -v
```

Expected: failures because descriptor-bound resource-value and xattr checks are absent.

- [ ] **Step 3: Implement fail-closed Darwin detection.**

Walk every component from `/Users` through the candidate parent using retained `O_DIRECTORY|O_NOFOLLOW` descriptors. Query resource values from a descriptor-bound path twice, enumerate xattr names with `flistxattr`, require one writable local/internal/non-removable APFS Volume UUID, and rebind descriptor/name identities. Map a positive provider signal to `CUSTODY_FILE_PROVIDER_REJECTED`; map missing identity APIs to `CUSTODY_UNAVAILABLE`.

```python
class CUSTODY_FILE_PROVIDER_REJECTED(CUSTODY_UNAVAILABLE):
    """The target belongs to a rejected or unprovable storage class."""

@dataclass(frozen=True)
class ResourceValues:
    is_ubiquitous: bool
    volume_is_local: bool
    volume_is_internal: bool
    volume_is_removable: bool
    volume_is_read_only: bool

FORBIDDEN_XATTR_PREFIXES = (
    "com.apple.fileprovider.",
    "com.apple.file-provider-domain-id",
    "com.apple.icloud.",
)

def _require_resource_values(values: ResourceValues) -> None:
    accepted = (
        values.is_ubiquitous is False
        and values.volume_is_local is True
        and values.volume_is_internal is True
        and values.volume_is_removable is False
        and values.volume_is_read_only is False
    )
    if not accepted:
        raise CUSTODY_FILE_PROVIDER_REJECTED("target storage class rejected")
```

- [ ] **Step 4: Run GREEN, compatibility tests, and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_platform tests.test_v52_r4_apfs tests.test_v52_r4_snapshot_issuer.SnapshotIssuerCliTests -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_platform.py mlb_backtest/v52_r4_apfs.py tests/test_v52_r4_disjoint_platform.py tests/test_v52_r4_apfs.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_platform.py mlb_backtest/v52_r4_apfs.py tests/test_v52_r4_disjoint_platform.py tests/test_v52_r4_apfs.py
git commit -m "feat: reject file provider r4 custody roots"
```

Expected: v2 platform cases pass and legacy R4 CLI behavior remains unchanged.

### Task 3: Bind the executable and clean Git checkout

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_execution.py`
- Create: `tests/test_v52_r4_disjoint_execution.py`

- [ ] **Step 1: Write RED authority-binding tests.**

Use a disposable Git repository and executable file. Cover detached/non-commit HEAD, dirty index, dirty worktree, untracked file, changed executable bytes/identity, source file not in the HEAD tree, wrong design/plan blob, wrong implementation blob, environment/path aliases, and a valid clean binding.

```python
binding = capture_execution_binding(
    repository,
    executable,
    target_volume_uuid=VOLUME_UUID,
    supersession_sha256="a" * 64,
)
assert binding.repository_clean is True
assert binding.repository_head == git(repository, "rev-parse", "--verify", "HEAD^{commit}")
assert binding.repository_tree == git(repository, "rev-parse", "HEAD^{tree}")
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_execution -v
```

Expected: failure because `capture_execution_binding` is absent.

- [ ] **Step 3: Implement descriptor and Git plumbing validation.**

Open the exact executable no-follow and record SHA-256 plus full regular-file identity. Resolve `HEAD^{commit}` and `HEAD^{tree}` with Git plumbing; require empty staged, unstaged, and untracked status; require `git ls-tree` blob IDs for the issuer, v2 policy, approved design, and this plan. Rebind executable and repository identities after capture. Never accept `PATH`, `HOME`, or a caller-supplied hash as authority.

The frozen `ExecutionBinding` fields are exactly `executable_sha256`, `executable_identity`, `repository_head`, `repository_tree`, `repository_clean`, `implementation_blob`, `design_blob`, `plan_blob`, `source_approval_sha256`, `target_volume_uuid`, and `supersedes_incomplete_attempt_sha256`.

`repository_clean: true` is an authenticated issuance-preflight fact. Fresh verification must validate the signed HEAD/tree/blob values but must not demand that the live repository remain clean after the six intentionally untracked v2 outputs have been published.

```python
required_blobs = {
    "implementation": "mlb_backtest/v52_r4_disjoint_issuer.py",
    "design": "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md",
    "plan": "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md",
}
if git_status_porcelain(repository):
    raise CUSTODY_UNAVAILABLE("repository is not clean")
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_execution -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_execution.py tests/test_v52_r4_disjoint_execution.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_execution.py tests/test_v52_r4_disjoint_execution.py
git commit -m "feat: bind r4 disjoint execution authority"
```

Expected: binding tests pass and the worktree is clean after commit.

### Task 4: Capture the old attempt without mutation

**Files:**
- Create: `mlb_backtest/v52_r4_forensic_fingerprint.py`
- Create: `tests/test_v52_r4_forensic_fingerprint.py`

- [ ] **Step 1: Write RED no-mutation tests.**

Construct a disposable inconsistent old root and owner lock. Patch write/chmod/chflags/rename/unlink/flock and lock-content reads to fail if invoked. Capture directory entries, identities, modes, flags, ctimes, and bytes before and after every success/failure path. Test missing root, missing lock, symlink, special file, hard link, changed rebind identity, wrong observer class, and stable ordered digest.

```python
before = snapshot_metadata(disposable_old_parent)
fingerprint = capture_incomplete_attempt_for_test(
    old_parent=disposable_old_parent,
    old_final_name="v5_1_bt_r4",
)
after = snapshot_metadata(disposable_old_parent)
self.assertEqual(before, after)
self.assertEqual(fingerprint["observer_state"], "OBSERVER_INCOMPLETE_LOCKED")
self.assertTrue(fingerprint["metadata_inconsistent"])
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_forensic_fingerprint -v
```

Expected: import failure because the forensic module does not exist.

- [ ] **Step 3: Implement bounded no-follow fingerprinting.**

Enumerate the old root descriptor-relatively in sorted UTF-8 path order. Hash a domain separator plus canonical rows containing relative path, type, size, content SHA-256 for regular leaves, mode, flags, Volume UUID, device, inode, and ctime. Stat the owner lock only; do not read or hash its 16-byte nonce. Require the exact fixed production old paths in the public function and keep the injectable path seam private/test-only.

```python
OLD_PARENT = Path("/Users/taejunoh/Documents/MLB Custody")
OLD_FINAL_NAME = "v5_1_bt_r4"
OLD_LOCK_NAME = ".v5_1_bt_r4.issuance.lock"
DOMAIN = b"MLB-R4-INCOMPLETE-ATTEMPT-v1\0"

def capture_incomplete_attempt() -> Mapping[str, object]:
    return _capture_incomplete_attempt(OLD_PARENT, OLD_FINAL_NAME, OLD_LOCK_NAME)
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_forensic_fingerprint -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_forensic_fingerprint.py tests/test_v52_r4_forensic_fingerprint.py
git diff --check
git add mlb_backtest/v52_r4_forensic_fingerprint.py tests/test_v52_r4_forensic_fingerprint.py
git commit -m "feat: fingerprint incomplete r4 custody read only"
```

Expected: tests prove identical before/after state and no lock-content access.

### Task 5: Implement the seed-free append-only journal

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_journal.py`
- Create: `tests/test_v52_r4_disjoint_journal.py`
- Modify: `schemas/v5_2_r4_disjoint_custody_journal_record.schema.json`

- [ ] **Step 1: Write RED record, writer, and verifier tests.**

Require canonical one-write newline records, sequence from 1, zero hash for the first record, exact previous/event hashes, exact generation/version/volume/execution hash, closed event-specific detail, `fsync` plus descriptor/name rebind before action, no seek/truncate/reopen/adopt, no secret/nonce/argv/env/payload/timestamp fields, one terminal record, and no append after close. Test invalid sequence/hash/event/outcome/detail and failure-to-write/fsync/rebind/freeze.

```python
record = writer.append(
    event="LOCK_ACQUIRE",
    outcome="INTENT",
    detail={"class": "LOCK"},
)
self.assertEqual(record["sequence"], 2)
self.assertEqual(record["previous_event_sha256"], first["event_sha256"])
self.assertNotIn("nonce", journal_path.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_journal -v
```

Expected: import failure because the journal module does not exist.

- [ ] **Step 3: Implement the minimal journal state machine.**

Create the journal exclusively as mode `0600` only after parent acceptance. Keep its descriptor; append each canonical record with one `os.write`, `fsync`, parent rebind, and descriptor/name identity check. Validate the closed stage order. `close_pre_anchor` writes `PRE_ANCHOR_AUTHORITY_READY`, fsyncs, changes to `0444`, sets `UF_IMMUTABLE`, verifies, and returns the final payload SHA and terminal event hash.

Require the single `os.write` return value to equal the complete record length; a short write is `JOURNAL_UNAVAILABLE` and is never completed by a second write. Failure detail is exactly `{"class": "<closed class>", "code": "<closed code>"}` with no traceback, path, nonce, or arbitrary exception text.

```python
EVENT_ORDER = (
    "PREFLIGHT_PARENT_ACCEPTED",
    "LOCK_ACQUIRE",
    "SOURCE_CLOSURE_VALIDATE",
    "TEMP_ROOT_CREATE",
    "COPY_AND_CONTENT_VALIDATE",
    "MODE_FLAG_VALIDATE",
    "SNAPSHOT_PUBLISH",
    "INVENTORY_PUBLISH",
    "RECEIPT_PUBLISH",
    "CONTRACT_PUBLISH_AND_AUTHENTICATE",
    "PRE_ANCHOR_AUTHORITY_READY",
)

def _event_sha256(record_without_hash: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json(record_without_hash)).hexdigest()
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_journal -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_journal.py tests/test_v52_r4_disjoint_journal.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_journal.py schemas/v5_2_r4_disjoint_custody_journal_record.schema.json tests/test_v52_r4_disjoint_journal.py
git commit -m "feat: add r4 disjoint custody journal"
```

Expected: journal tests pass, including crash-prefix verification and post-close append rejection.

### Task 6: Add full-tree v2 inventory and receipt authority

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_artifacts.py`
- Modify: `tests/test_v52_r4_disjoint_custody.py`
- Modify: `schemas/v5_2_r4_disjoint_protected_raw_inventory.schema.json`
- Modify: `schemas/v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json`

- [ ] **Step 1: Write RED exact-closure and mutation tests.**

Build a disposable 1,675-record profile with 4,711 distinct members, four zero-byte locks, and all reachable directories. Assert exactly 4,715 leaf objects and every directory are inspected. Reject one missing/extra leaf, hard link, symlink, wrong size/hash/identity/volume, leaf mode other than `0444`, directory mode other than `0555`, missing immutable flag, and directory-manifest mismatch.

```python
captured = capture_frozen_disjoint_snapshot(root_fd, expected_closure, VOLUME_UUID)
self.assertEqual(len(captured.members) + len(captured.root_locks), 4715)
self.assertEqual(captured.directory_manifest_sha256, expected_directory_digest)
self.assertEqual({row["mode"] for row in captured.directories}, {0o555})
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_custody_artifacts tests.test_v52_r4_disjoint_custody.DisjointArtifactTests -v
```

Expected: v2 capture/build/validate symbols are missing.

- [ ] **Step 3: Implement v2 capture, inventory, and receipt.**

Reuse the existing no-follow APFS identity primitives, but produce distinct v2 documents and schemas. The inventory binds policy, source approval, ordered directory manifest, journal summary, and execution binding. The receipt binds inventory hash, target Volume UUID, 4,715-leaf comparison, directory digest, and journal terminal hash. Keep existing R4 builders/validators unchanged.

```python
def build_disjoint_inventory(
    *, profile: R4SourceAuthorityProfile, snapshot: _CapturedDisjointSnapshot,
    policy: DisjointCustodyPolicy, journal: Mapping[str, object],
    execution_binding: ExecutionBinding,
) -> dict[str, object]:
    document = {
        "schema_version": "v5.2-r4-disjoint-protected-raw-inventory-v1",
        "kind": "MLB_R4_DISJOINT_PROTECTED_RAW_INVENTORY",
        "custody_generation": policy.generation,
        "custody_parent_class": "LOCAL_APFS_NON_FILE_PROVIDER",
        "final_name": policy.final_name,
        "source_approval": dict(profile.source_approval),
        "operational_journal": dict(journal),
        "execution_binding": dataclasses.asdict(execution_binding),
        "snapshot": dict(snapshot.snapshot),
        "records": [dict(record) for record in profile.closure.records],
        "members": [dict(member) for member in snapshot.members],
        "bindings": [dict(binding) for binding in snapshot.bindings],
        "root_locks": [dict(lock) for lock in snapshot.root_locks],
        "directories": [dict(directory) for directory in snapshot.directories],
        "directory_manifest_sha256": snapshot.directory_manifest_sha256,
    }
    validate_disjoint_inventory(document, profile=profile)
    return document
```

- [ ] **Step 4: Run GREEN, legacy compatibility, and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_custody_artifacts tests.test_v52_r4_disjoint_custody.DisjointArtifactTests -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_artifacts.py tests/test_v52_r4_disjoint_custody.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_artifacts.py schemas/v5_2_r4_disjoint_protected_raw_inventory.schema.json schemas/v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json tests/test_v52_r4_disjoint_custody.py
git commit -m "feat: bind full r4 disjoint custody tree"
```

Expected: old R4 artifact tests remain byte/behavior compatible and all v2 mutation cases fail closed.

### Task 7: Add the v2 contract and signed supersession anchor

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_anchor.py`
- Modify: `tests/test_v52_r4_signed_anchor.py`
- Modify: `tests/test_v52_r4_disjoint_custody.py`
- Modify: `tests/r4_issued_authority_fixture.py`
- Modify: `schemas/v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json`

- [ ] **Step 1: Write RED v2 contract/anchor tests.**

Require the distinct v2 contract path and generation, fixed RunSpec/source approval, inventory/receipt/journal paths, execution binding, design/plan/implementation blobs, and exact inline `supersedes_incomplete_attempt`. Verify the detached signature before parsing mutable artifacts. Reject old R4 schemas, wrong journal copy/hash/terminal hash, missing or malformed supersession fields, lock nonce fields, wrong artifact paths, and every hash mutation.

```python
verified = verify_disjoint_authority_anchor(
    anchor_bytes,
    signature,
    public_key=test_public_key,
    expected_key_id=test_key_id,
)
self.assertEqual(
    verified["supersedes_incomplete_attempt"]["observer_state"],
    "OBSERVER_INCOMPLETE_LOCKED",
)
self.assertNotIn("nonce", json.dumps(verified))
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_signed_anchor tests.test_v52_r4_disjoint_custody.DisjointAnchorTests -v
```

Expected: failures because v2 contract/anchor builders and verifier are absent.

- [ ] **Step 3: Implement distinct v2 contract and anchor functions.**

Add `build_disjoint_authority_anchor`, `render_disjoint_authority_anchor`, and `verify_disjoint_authority_anchor` with a new domain separator. Anchor inventory, receipt, contract, RunSpec, journal copy, execution binding, and amendment blobs; include the inline fingerprint. Do not add the anchor's own hash to its payload. Keep the existing R4 anchor functions unchanged.

```python
DISJOINT_DOMAIN_MESSAGE = b"MLB-R4-DISJOINT-CUSTODY-ANCHOR-v1\0"
DISJOINT_SCHEMA_VERSION = "v5.2-r4-disjoint-custody-authority-anchor-v1"

def verify_disjoint_authority_anchor(anchor_bytes: bytes, signature: bytes, *, public_key: bytes, expected_key_id: str) -> Mapping[str, object]:
    Ed25519PublicKey.from_public_bytes(public_key).verify(
        signature, DISJOINT_DOMAIN_MESSAGE + anchor_bytes
    )
    return _validate_disjoint_canonical_anchor(anchor_bytes, expected_key_id)
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_signed_anchor tests.test_v52_r4_disjoint_custody.DisjointAnchorTests tests.test_v52_r4_snapshot_issuer.SignedAnchorIssuanceRedTests -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_anchor.py tests/test_v52_r4_signed_anchor.py tests/test_v52_r4_disjoint_custody.py tests/r4_issued_authority_fixture.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_anchor.py schemas/v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json tests/test_v52_r4_signed_anchor.py tests/test_v52_r4_disjoint_custody.py tests/r4_issued_authority_fixture.py
git commit -m "feat: sign r4 disjoint custody authority"
```

Expected: v2 signature-first tests pass and legacy signed-anchor tests remain green.

### Task 8: Implement the disjoint owner, layout, and observer state

**Files:**
- Create: `mlb_backtest/v52_r4_disjoint_issuer.py`
- Modify: `tests/test_v52_r4_disjoint_custody.py`

- [ ] **Step 1: Write RED namespace and state tests.**

Test the exact v2 lock/journal/temp names, 32-lowercase-hex temp suffix, exclusive creation, no collision with old R4 names, and all state-table rows: `ABSENT`, `OBSERVER_INCOMPLETE_UNOWNED`, `OBSERVER_INCOMPLETE_LOCKED`, `OBSERVER_INCOMPLETE_PUBLISHED`, `FINAL`, and mismatch. Assert old evidence never participates in v2 conflict checks beyond the fingerprint input.

```python
self.assertEqual(owner.lock_name, ".v5_1_bt_r4.disjoint-v2.issuance.lock")
self.assertRegex(
    owner.temporary_name,
    r"^\.v5_1_bt_r4\.disjoint-v2\.issuance\.[0-9a-f]{32}\.tmp$",
)
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointOwnerAndStateTests -v
```

Expected: v2 owner/state symbols are missing or legacy names are observed.

- [ ] **Step 3: Implement a closed v2 owner path without weakening the old owner.**

Add private `_DisjointIssuanceOwner` and `_DisjointAuthorityLayout` in the new issuer, bound to `DisjointCustodyPolicy`. The v2 abort path only closes descriptors; it never unlinks transaction residue. Observer precedence is authenticated lock, journal, final/static closure, then absence. Do not add generic naming parameters to the existing `IssuanceOwner` or change existing R4 state semantics.

```python
class DisjointIssuanceState(str, Enum):
    ABSENT = "ABSENT"
    OBSERVER_INCOMPLETE_UNOWNED = "OBSERVER_INCOMPLETE_UNOWNED"
    OBSERVER_INCOMPLETE_LOCKED = "OBSERVER_INCOMPLETE_LOCKED"
    OBSERVER_INCOMPLETE_PUBLISHED = "OBSERVER_INCOMPLETE_PUBLISHED"
    FINAL = "FINAL"

def _disjoint_temporary_name(nonce: bytes) -> str:
    if len(nonce) != 16:
        raise CUSTODY_UNAVAILABLE("disjoint issuance nonce must contain 16 bytes")
    return f".{FINAL_NAME}.disjoint-v2.issuance.{nonce.hex()}.tmp"
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointOwnerAndStateTests tests.test_v52_r4_snapshot_issuer.SnapshotIssuerLockTests tests.test_v52_r4_snapshot_issuer.SnapshotIssuerObserverTests -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_custody.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_custody.py
git commit -m "feat: isolate r4 disjoint issuance state"
```

Expected: v2 state tests pass with existing owner/observer tests unchanged.

### Task 9: Journal the v2 issuance transaction and publish six artifacts

**Files:**
- Modify: `mlb_backtest/v52_r4_disjoint_issuer.py`
- Modify: `mlb_backtest/v52_r4_disjoint_journal.py`
- Modify: `tests/test_v52_r4_disjoint_custody.py`
- Modify: `tests/test_v52_r4_disjoint_journal.py`

- [ ] **Step 1: Write RED happy-path ordering and failure-retention tests.**

Instrument every pre-anchor seam. Require `INTENT` fsync before action and `SUCCESS` after action for lock, source closure, temp root, copy/content, mode/flag, snapshot rename, inventory, receipt, and contract. Require terminal close/freeze, exact repository journal copy, anchor/signature, six-file freeze/rebind, then owner-lock unlink. For each injected error, assert no successful return and exact retained evidence; after journal close, assert no new journal record is attempted.

```python
expected = [
    ("PREFLIGHT_PARENT_ACCEPTED", "SUCCESS"),
    ("LOCK_ACQUIRE", "INTENT"), ("LOCK_ACQUIRE", "SUCCESS"),
    ("SOURCE_CLOSURE_VALIDATE", "INTENT"), ("SOURCE_CLOSURE_VALIDATE", "SUCCESS"),
    ("TEMP_ROOT_CREATE", "INTENT"), ("TEMP_ROOT_CREATE", "SUCCESS"),
    ("COPY_AND_CONTENT_VALIDATE", "INTENT"), ("COPY_AND_CONTENT_VALIDATE", "SUCCESS"),
    ("MODE_FLAG_VALIDATE", "INTENT"), ("MODE_FLAG_VALIDATE", "SUCCESS"),
    ("SNAPSHOT_PUBLISH", "INTENT"), ("SNAPSHOT_PUBLISH", "SUCCESS"),
    ("INVENTORY_PUBLISH", "INTENT"), ("INVENTORY_PUBLISH", "SUCCESS"),
    ("RECEIPT_PUBLISH", "INTENT"), ("RECEIPT_PUBLISH", "SUCCESS"),
    ("CONTRACT_PUBLISH_AND_AUTHENTICATE", "INTENT"),
    ("CONTRACT_PUBLISH_AND_AUTHENTICATE", "SUCCESS"),
    ("PRE_ANCHOR_AUTHORITY_READY", "SUCCESS"),
]
self.assertEqual(observed_events, expected)
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointIssuanceTests tests.test_v52_r4_disjoint_journal -v
```

Expected: no v2 orchestration exists.

- [ ] **Step 3: Implement `_issue_and_bind_disjoint`.**

Order operations exactly: policy/platform preflight → old fingerprint → execution binding → journal creation/first success → lock → source validation → temp → copy → pre-publish mode validation → snapshot publication → inventory → receipt → v2 contract → terminal journal close → exact journal copy → signed anchor/signature → six-file freeze/rebind → final snapshot rebind → lock release. `SNAPSHOT_PUBLISH` includes exclusive rename, descendant-first/root-last immutable sealing, volume full sync, and a complete post-seal mode/flag/identity capture before its `SUCCESS` record. Wrap each pre-anchor stage with journal intent/success/failure; after close, retain lock and partial immutable artifacts on any failure.

```python
def _journaled_stage(writer, event, detail, action):
    writer.append(event=event, outcome="INTENT", detail=detail)
    try:
        result = action()
    except Exception as exc:
        writer.append(event=event, outcome="FAILURE", detail=closed_error(exc))
        raise
    writer.append(event=event, outcome="SUCCESS", detail=detail)
    return result
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointIssuanceTests tests.test_v52_r4_disjoint_journal tests.test_v52_r4_snapshot_issuer.SignedAnchorIssuanceRedTests -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_issuer.py mlb_backtest/v52_r4_disjoint_journal.py tests/test_v52_r4_disjoint_custody.py tests/test_v52_r4_disjoint_journal.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_issuer.py mlb_backtest/v52_r4_disjoint_journal.py tests/test_v52_r4_disjoint_custody.py tests/test_v52_r4_disjoint_journal.py
git commit -m "feat: issue journaled r4 disjoint custody"
```

Expected: full disposable issuance succeeds and every injected failure remains non-executable without cleanup.

### Task 10: Add signature-first fresh v2 verification

**Files:**
- Modify: `mlb_backtest/v52_r4_disjoint_issuer.py`
- Modify: `tests/test_v52_r4_disjoint_custody.py`

- [ ] **Step 1: Write RED fresh-process verification tests.**

Verify from a subprocess with no raw source tree. Require the fixed v2 custody root, six exact frozen repository outputs, valid signature, v2 contract, RunSpec, journal chain/copy, inline supersession, execution binding, 4,715 leaf objects, every directory, modes/flags, and target Volume UUID. Reject the old root, byte-identical copied volume, open/changed journal, owner lock, temp root, any partial artifact set, and each mutation.

```python
completed = subprocess.run(
    [PYTHON311, "-m", "mlb_backtest.v52_r4_disjoint_issuer", "verify",
     "--custody-generation", GENERATION,
     "--custody-root", str(disposable_final),
     "--inventory", str(v2_inventory),
     "--receipt", str(v2_receipt),
     "--run-spec", str(run_spec),
     "--contract", str(v2_contract)],
    env=raw_free_environment,
    check=False,
)
self.assertEqual(completed.returncode, 0)
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointVerificationTests -v
```

Expected: verify does not understand the v2 generation/artifacts.

- [ ] **Step 3: Implement `_verify_disjoint`.**

Open and verify detached signature and canonical anchor first. Use its authenticated paths/hashes to read the five other static outputs through retained no-follow descriptors, verify the journal copy and terminal chain, validate the v2 contract/RunSpec/execution/supersession bindings, rehash and rebind the complete frozen snapshot, then require absent authenticated v2 owner lock/temp roots. Never reconstruct source authority from the issued inventory.

Treat the signed clean-checkout bit as historical preflight evidence. Verification checks the signed commit/tree/blob IDs and live implementation identities but does not reject solely because the newly published six authority outputs make the repository status non-clean before their audit commit.

```python
anchor = verify_disjoint_authority_anchor(
    anchor_bytes, signature_bytes,
    public_key=profile.anchor_public_key,
    expected_key_id=profile.anchor_key_id,
)
bundle = _open_disjoint_release_bundle(repository, anchor)
bundle.verify_journal_and_artifacts()
bundle.verify_frozen_snapshot(custody_root)
bundle.require_released()
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointVerificationTests tests.test_v52_r4_signed_anchor tests.test_v52_r4_custody_artifacts -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_custody.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_custody.py
git commit -m "feat: verify r4 disjoint custody authority"
```

Expected: fresh raw-free v2 verification passes only for the complete immutable authority.

### Task 11: Expose the exact closed CLI without recovery commands

**Files:**
- Modify: `mlb_backtest/v52_r4_disjoint_issuer.py`
- Modify: `tests/test_v52_r4_disjoint_custody.py`

- [ ] **Step 1: Write RED CLI tests.**

Require `--custody-generation` for v2 `provision-custody-parent`, `preflight`, `issue-and-bind`, and `verify`. Test exact accepted literals plus missing/wrong generation, parent, final name, v2 contract path, raw noncanonical spellings, old root, extra journal flag, and every forbidden command/flag. Patch the mutation functions and assert invalid argv returns 2 before they are called.

```python
for forbidden in ("--resume", "--adopt", "--recover", "--unlock", "--cleanup", "--old-custody", "--allow-file-provider", "--force"):
    with self.subTest(forbidden=forbidden):
        self.assertNotIn(forbidden, parser_help)
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointCliTests tests.test_v52_r4_snapshot_issuer.SnapshotIssuerCliTests -v
```

Expected: v2 generation routing is absent.

- [ ] **Step 3: Implement raw-string validation and closed routing.**

Validate exact v2 strings before converting them to `Path`. The new module exposes only the v2 generation and delegates no argv to the legacy issuer; the legacy `v52_r4_snapshot_issuer` parser remains unchanged. Provisioning may create only the missing `custody-v2` leaf after its existing ancestors pass the File Provider/APFS checks; it may not call recursive `mkdir(parents=True)`.

```python
if args.custody_generation != GENERATION:
    raise CUSTODY_UNAVAILABLE("disjoint custody generation differs")
policy = require_disjoint_cli_policy(
    raw_parent=args.custody_parent,
    raw_final_name=args.final_name,
)
return _dispatch_disjoint(args, policy)
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointCliTests tests.test_v52_r4_snapshot_issuer.SnapshotIssuerCliTests -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_snapshot_issuer.py tests/test_v52_r4_disjoint_custody.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_disjoint_custody.py
git commit -m "feat: expose closed r4 disjoint custody cli"
```

Expected: both legacy and v2 CLI contracts pass; no forbidden recovery surface exists.

### Task 12: Admit the exact v2 contract to the existing source workflow

**Files:**
- Modify: `schemas/v5_2_acquisition_contract.schema.json`
- Modify: `mlb_backtest/v52_source_contracts.py`
- Modify: `mlb_backtest/v52_query_plan.py`
- Modify: `tests/test_v52_acquisition_contract.py`
- Modify: `tests/test_v52_query_plan.py`
- Modify: `tests/test_protocols.py`

- [ ] **Step 1: Write RED contract and authority-branch tests.**

Add a closed v2 acquisition-contract variant with exact generation and inventory/receipt/journal paths. Use the existing R4 RunSpec with both the old R4 contract and new v2 contract; prove branch selection uses the exact `(RunSpec path, contract path)` pair rather than stopping at the first matching RunSpec. Reject cross-paired old/v2 artifacts, old generation on the v2 path, and v2 generation on the old path. Assert query IDs, batches, source years, parser/model semantics, and recovery dispositions are identical to the existing R4 branch.

```python
old_branch = resolve_authority_branch(R4_RUN_SPEC, R4_CONTRACT)
v2_branch = resolve_authority_branch(R4_RUN_SPEC, R4_DISJOINT_V2_CONTRACT)
self.assertEqual(v2_branch.query_semantics, old_branch.query_semantics)
self.assertNotEqual(v2_branch.contract_path, old_branch.contract_path)
```

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_acquisition_contract tests.test_v52_query_plan tests.test_protocols -v
```

Expected: the acquisition schema/validator rejects the v2 contract or `_authority_branch` selects the old contract by RunSpec alone.

- [ ] **Step 3: Implement a separate v2 validator and pair-based branch selection.**

Add `validate_r4_disjoint_v2_recovery_acquisition_contract` with distinct constants; do not loosen `validate_r4_recovery_acquisition_contract`. Append a closed schema variant. Change the query-plan resolver to compare both exact relative paths before returning a branch, then reuse the existing R4 source/query semantics for the v2 pair.

```python
R4_DISJOINT_V2_CONTRACT = PurePosixPath(
    "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json"
)

def _authority_branch(run_spec: PurePosixPath, contract: PurePosixPath) -> AuthorityBranch:
    key = (run_spec, contract)
    try:
        return AUTHORITY_BRANCHES[key]
    except KeyError as exc:
        raise ValueError("source authority pair differs") from exc
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_acquisition_contract tests.test_v52_query_plan tests.test_protocols -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_source_contracts.py mlb_backtest/v52_query_plan.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py tests/test_protocols.py
git diff --check
git add schemas/v5_2_acquisition_contract.schema.json mlb_backtest/v52_source_contracts.py mlb_backtest/v52_query_plan.py tests/test_v52_acquisition_contract.py tests/test_v52_query_plan.py tests/test_protocols.py
git commit -m "feat: admit r4 disjoint custody contract"
```

Expected: old and v2 contracts both validate only in their exact branch and produce identical R4 query semantics.

### Task 13: Complete the race, signal, and old-evidence isolation matrix

**Files:**
- Modify: `tests/test_v52_r4_disjoint_custody.py`
- Modify: `tests/test_v52_r4_disjoint_journal.py`
- Modify: `mlb_backtest/v52_r4_disjoint_issuer.py` only if a RED test demonstrates a product defect
- Modify: `mlb_backtest/v52_r4_disjoint_journal.py` only if a RED test demonstrates a product defect

- [ ] **Step 1: Add the complete destructive-swap matrix.**

Inject descriptor/name swaps before and after every journal write, lock creation, temp creation, copy, seal, exclusive rename, inventory/receipt/contract publication, terminal close, journal copy, anchor/signature publication, bundle rebind, and lock unlink. Add SIGTERM/SIGINT injection at pre-anchor and post-close boundaries. For each case assert no success, correct retained lock/journal/artifacts, no executable classification, and exact old-fixture identity preservation.

```python
for stage, seam in DISJOINT_RELEASE_SEAMS:
    with self.subTest(stage=stage):
        outcome = inject_swap_and_run(stage, seam)
        self.assertNotEqual(outcome.returncode, 0)
        self.assertNotEqual(observe_disjoint_state(outcome.repository, outcome.parent), DisjointIssuanceState.FINAL)
        self.assertEqual(outcome.old_before, outcome.old_after)
```

- [ ] **Step 2: Run RED and fix only independently reproduced defects.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody.DisjointRaceAndFailureTests tests.test_v52_r4_disjoint_journal -v
```

Expected: any failing seam identifies one concrete missing rebind, failure record, or state guard. For each failure, retain the failing test, apply the smallest fix at that seam, and rerun this exact command until PASS.

- [ ] **Step 3: Prove production paths are absent from test syscalls.**

Patch `os.open`, `os.stat`, `os.lstat`, `os.rename`, `os.unlink`, `os.chmod`, `os.chflags`, and `fcntl.flock` at the module boundaries to record paths. Assert no call targets either real old path or the real Application Support v2 path; test-only injected roots must be the only custody targets.

```python
for call in recorded_calls:
    self.assertFalse(str(call.path).startswith("/Users/taejunoh/Documents/MLB Custody"))
    self.assertFalse(str(call.path).startswith("/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2"))
```

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_custody tests.test_v52_r4_disjoint_journal tests.test_v52_r4_forensic_fingerprint -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_disjoint_issuer.py mlb_backtest/v52_r4_disjoint_journal.py tests/test_v52_r4_disjoint_custody.py tests/test_v52_r4_disjoint_journal.py
git diff --check
git add mlb_backtest/v52_r4_disjoint_issuer.py mlb_backtest/v52_r4_disjoint_journal.py tests/test_v52_r4_disjoint_custody.py tests/test_v52_r4_disjoint_journal.py
git commit -m "test: seal r4 disjoint failure matrix"
```

Expected: all matrix cases pass with no production custody operation.

### Task 14: Run independent reviews and authoritative gates

**Files:**
- Review: all changes from `5d0be9d9eee3935af83d22c8df5ceb2cce66aa9e..HEAD`
- Verify only: protected `audit/`, `data/`, `reports/`, and existing `protocols/` outputs
- Modify: `scripts/run_task13_exact_suite.py`
- Modify: `tests/test_task13_exact_suite_runner.py`

- [ ] **Step 1: Run focused v2 and legacy R4 suites.**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_policy tests.test_v52_r4_disjoint_platform tests.test_v52_r4_disjoint_execution tests.test_v52_r4_forensic_fingerprint tests.test_v52_r4_disjoint_journal tests.test_v52_r4_disjoint_custody tests.test_v52_r4_apfs tests.test_v52_r4_custody_artifacts tests.test_v52_r4_signed_anchor tests.test_v52_r4_snapshot_issuer tests.test_v52_r4_offline_signer tests.test_v52_acquisition_contract tests.test_v52_query_plan tests.test_protocols -v
```

Expected: all focused tests pass; imports resolve only from this worktree.

- [ ] **Step 2: Run the authoritative exact suite once.**

Use the repository's committed exact runner from a fresh clone at the candidate HEAD, Python 3.11, the pinned Chromium bundle, the 16 v5.1 required files, three old evidence files, eight recovery parent-bound files, four parent tree fingerprints, and two preflight hashes. Record the candidate HEAD, command, log, explicit exit file, summary, and integrity results. Do not include interrupted or wrong-interpreter runs in the result.

Before running it, append the six new v2 test modules (`disjoint_policy`, `disjoint_platform`, `disjoint_execution`, `forensic_fingerprint`, `disjoint_journal`, and `disjoint_custody`) to `CHILD_MODULES` in `scripts/run_task13_exact_suite.py` without reordering or removing any existing module, add a runner test asserting each appears exactly once, and commit those two runner files separately:

```bash
git add scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "test: add r4 disjoint modules to exact suite"
```

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_task13_exact_suite_runner -v
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/run_task13_exact_suite.py \
  --repo . \
  --v51-input-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --old-v52-evidence-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --chromium-executable '/Users/taejunoh/.cache/puppeteer/chrome-headless-shell/mac_arm-146.0.7680.153/chrome-headless-shell-mac-arm64/chrome-headless-shell' \
  --chromium-sha256 454577a889a2c0b698c09f0ca8db047ea2e6cd604a42c36dc09b375c81362518 \
  --chromium-bundle-sha256 2ea5035c2085aaf0fb70be6d2949f51eeb42affaac47772044ddaa727cb4c253 \
  --python /opt/homebrew/bin/python3.11
```

Expected: exit 0 and the exact runner's complete child suite passes with all protected hashes unchanged.

- [ ] **Step 3: Run static gates and protected-path audit.**

```bash
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest tests scripts
git diff --check
git status --short
git diff --name-only 5d0be9d9eee3935af83d22c8df5ceb2cce66aa9e..HEAD -- audit data reports protocols
```

Expected: Ruff and diff-check exit 0; worktree clean; only new schema/test/code/plan paths and the intentionally new v2 contract-generation code changed; no generated production audit/data/report/protocol output exists.

- [ ] **Step 4: Independent specification review.**

The reviewer maps every approved design section to code/tests, verifies the journal remains pre-anchor and acyclic, confirms the six artifacts and full-tree invariants, and confirms old-path mutation APIs are absent. Expected verdict: `APPROVED`, no P0/P1/P2.

- [ ] **Step 5: Independent quality/security review.**

The reviewer checks descriptor lifetime/rebind, File Provider fail-closed behavior, Git/executable binding, journal durability and secret exclusion, signature-first verification, failure retention, test realism, and legacy compatibility. Expected verdict: `APPROVED`, no P0/P1/P2.

- [ ] **Step 6: Fix findings with TDD and rerun all affected gates.**

For each accepted finding, first add one focused failing regression test, run it to observe the failure, apply the smallest fix, rerun focused tests, then repeat Steps 1–5. Commit each independent fix separately with a message naming the invariant.

### Task 15: Separately authorized one-shot production rollout

**Files created only after a new explicit rollout approval:**
- `/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/v5_1_bt_r4`
- `/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/.v5_1_bt_r4.disjoint-v2.issuance.journal.ndjson`
- Six fixed v2 repository outputs listed in the approved design

This task is deliberately outside implementation authorization. Do not execute any step merely because Tasks 1–14 are green.

- [ ] **Step 1: Obtain explicit production-rollout approval for the exact reviewed HEAD.**

Record the approved full commit SHA. Require a clean worktree, the reviewed Python executable identity/hash, valid frozen signing authority, and all Task 14 evidence. If the HEAD changes, approval expires and Task 14 repeats.

- [ ] **Step 2: Run read-only production preflight once.**

Use the exact generation, Application Support parent, final name, external v5.1 source authority, fixed RunSpec, and fixed new v2 contract path. Require accepted local/internal writable APFS, no File Provider markers, exact old-attempt read-only fingerprint, clean execution binding, valid source closure, and absence of every v2 journal/lock/temp/final/output path. Any rejection stops rollout.

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m mlb_backtest.v52_r4_disjoint_issuer preflight \
  --custody-generation v5.2-r4-disjoint-custody-v2 \
  --source-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --source-inventory '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/audit/v5_1_bt/protected_raw_inventory.json' \
  --custody-parent '/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2' \
  --final-name v5_1_bt_r4
```

Expected: exit 0 with seed-free accepted-state output; no journal, lock, temp, final, or repository artifact is created.

- [ ] **Step 3: Provision only the fixed missing `custody-v2` leaf if required, then repeat preflight once.**

The command may create only that single leaf after accepted ancestors. Any unexpected child, journal, lock, temp, final, or repository output stops rollout with no cleanup.

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m mlb_backtest.v52_r4_disjoint_issuer provision-custody-parent \
  --custody-generation v5.2-r4-disjoint-custody-v2 \
  --custody-parent '/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2' \
  --final-name v5_1_bt_r4
```

Expected: exit 0, followed by the exact Step 2 preflight command returning exit 0 again.

- [ ] **Step 4: Execute `issue-and-bind` exactly once.**

Expected successful order is the Task 9 order. Any nonzero exit ends the rollout; retain all evidence and do not retry, adopt, repair, unlock, or clean.

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m mlb_backtest.v52_r4_disjoint_issuer issue-and-bind \
  --custody-generation v5.2-r4-disjoint-custody-v2 \
  --source-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --source-inventory '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/audit/v5_1_bt/protected_raw_inventory.json' \
  --source-approval-logical-root data/v5_1_bt/raw \
  --source-approval-inventory audit/v5_1_bt/protected_raw_inventory.json \
  --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json \
  --contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json \
  --custody-parent '/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2' \
  --final-name v5_1_bt_r4 \
  --audit-root audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition
```

Expected: exit 0 exactly once.

- [ ] **Step 5: Run fresh-process v2 `verify` once.**

Expected: exit 0, valid detached signature, fixed v2 generation/paths, valid journal chain/copy, exact 1,675/4,711/5,025 closure, all 4,715 leaves and all directories immutable with correct modes, correct Volume UUID, all six repository outputs frozen, and authenticated owner lock absent.

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m mlb_backtest.v52_r4_disjoint_issuer verify \
  --custody-generation v5.2-r4-disjoint-custody-v2 \
  --custody-root '/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/v5_1_bt_r4' \
  --inventory audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4_disjoint_v2.json \
  --receipt audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4_disjoint_v2.json \
  --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json \
  --contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json
```

Expected: exit 0 in a new process.

- [ ] **Step 6: Stage and commit exactly the six v2 repository outputs.**

```bash
git add audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4_disjoint_v2.json audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4_disjoint_v2.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4_disjoint_v2.json audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4_disjoint_v2.ed25519 audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/custody_issuance_journal_r4_disjoint_v2.ndjson
git commit -m "audit: issue r4 disjoint custody authority"
```

Expected: exactly six staged files; old incomplete root/lock and all prior R4 artifacts remain byte- and metadata-unchanged.

## Completion Criteria

- Tasks 1–14 are complete at one clean reviewed commit with all focused, exact, Ruff, diff, integrity, specification-review, and quality-review gates green.
- No implementation/test operation touched the real old or new production custody paths.
- Existing R2/R3 and non-v2 R4 behavior remains compatible.
- Task 15 remains blocked until the user explicitly approves the exact reviewed HEAD for production rollout.

## Spec Coverage Map

| Approved design requirement | Implemented and proven by |
| --- | --- |
| Exact disjoint parent/final/lock/temp/journal/generation and six outputs | Tasks 1, 8, 11 |
| Local writable internal APFS and fail-closed File Provider rejection before writes | Task 2 |
| Executable SHA/identity plus clean HEAD/tree/blob/source binding | Task 3 |
| Fixed old-attempt read-only no-follow fingerprint; no nonce read or mutation | Tasks 4, 13 |
| Canonical seed-free append-only pre-anchor journal and acyclic close | Tasks 5, 9, 13 |
| Exact 1,675 records, 4,711 members, 5,025 bindings, four locks, 4,715 leaves, and every directory | Tasks 6, 10 |
| New v2 inventory/receipt/contract/anchor schemas and signature-first verification | Tasks 6, 7, 10 |
| One-shot state model with no resume/adopt/recover/unlock/cleanup | Tasks 8, 11, 13 |
| Existing R4 RunSpec admitted only with the exact v2 contract pair | Task 12 |
| Races, signals, failure retention, old-evidence isolation, and no production test syscalls | Task 13 |
| Legacy R2/R3/non-v2 R4 compatibility, exact suite, Ruff, diff, hashes, and two reviews | Task 14 |
| Real preflight/issuance/verify only after a new exact-HEAD approval | Task 15 |
