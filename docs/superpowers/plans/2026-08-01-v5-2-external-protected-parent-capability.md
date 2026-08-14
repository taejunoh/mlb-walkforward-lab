# v5.2 External Protected Parent Capability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the final r3 authority authenticate and read the original inode-bound v5.1 raw tree from the protected parent, without copying or modifying it, while keeping every older authority root-local and byte-semantic unchanged.

**Architecture:** Introduce one immutable `LegacyEvidenceCapability` boundary with a root-local adapter and a descriptor-bound external-parent implementation. Build the external capability only from the target-local canonical inventory plus the exact r3 RunSpec/contract, thread that single capability through acquisition, evidence, finalization, replay, and resume, and invoke its final rebind inside registry publication locks. Runtime outputs remain target-local; the parent absolute path is never serialized.

**Tech Stack:** Python 3.11, `unittest`, frozen dataclasses, `typing.Protocol`, descriptor-relative POSIX filesystem APIs, canonical JSON, SHA-256, Git-bound source authority, Ruff.

---

## Execution Context and Non-Negotiable Guards

All implementation commands run in:

```bash
export CODE_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd'
export PARENT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest'
export PARENT_RAW="$PARENT/data/v5_1_bt/raw"
export PY311='/opt/homebrew/opt/python@3.11/bin/python3.11'
export PYTHONDONTWRITEBYTECODE=1
cd "$CODE_ROOT"
```

The approved design is:

```text
docs/superpowers/specs/2026-07-30-v5-2-external-protected-parent-capability-design.md
```

The implementation begins from commit `74298aaea5cc5c1b5eb39a7601851299bcee412a`. Before the first code edit, record a byte/hash manifest of the 16 required v5.1 files, the three old-evidence files, the eight recovery-parent bound files, four parent-tree fingerprints, and two preflight hashes:

```bash
MANIFEST_OUT=/private/tmp/v52-external-parent-protected-before.json "$PY311" - <<'PY'
import dataclasses
import json
import os
from pathlib import Path

from mlb_backtest.verify_v52_design import V51_REQUIRED_FILES
from scripts.run_task13_exact_suite import (
    OLD_EVIDENCE_RELATIVES,
    OLD_EVIDENCE_SHA256,
    ORIGINAL_PREFLIGHT_RELATIVES,
    ORIGINAL_PREFLIGHT_SHA256,
    _assert_recovery_authority_tree_fingerprints,
    capture_recovery_parent_trees,
    sha256_paths,
    validate_recovery_parent_inputs,
)

parent = Path(os.environ["PARENT"])
trees = capture_recovery_parent_trees(parent)
_assert_recovery_authority_tree_fingerprints(trees)
old = sha256_paths(parent, OLD_EVIDENCE_RELATIVES)
preflight = sha256_paths(parent, ORIGINAL_PREFLIGHT_RELATIVES)
recovery = validate_recovery_parent_inputs(parent)
assert len(V51_REQUIRED_FILES) == 16
assert len(old) == 3 and old == OLD_EVIDENCE_SHA256
assert len(recovery) == 8
assert len(trees) == 4
assert len(preflight) == 2 and preflight == ORIGINAL_PREFLIGHT_SHA256
payload = {
    "v51_required": sha256_paths(parent, V51_REQUIRED_FILES),
    "old_evidence": old,
    "recovery_parent": recovery,
    "parent_trees": {
        key: dataclasses.asdict(value) for key, value in sorted(trees.items())
    },
    "preflight": preflight,
}
Path(os.environ["MANIFEST_OUT"]).write_text(
    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
)
PY
```

Record the four raw-root lock identities separately for the capability-specific set:

```bash
"$PY311" - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["PARENT_RAW"])
locks = (
    ".baseball_savant_statcast.lock",
    ".mlb_schedule.lock",
    ".mlb_stats_pitcher_bf.lock",
    ".mlb_stats_pitching.lock",
)
snapshot = {}
for name in locks:
    stat = (root / name).stat(follow_symlinks=False)
    snapshot[name] = [stat.st_dev, stat.st_ino, stat.st_mode, stat.st_nlink, stat.st_size]
Path("/private/tmp/v52-external-parent-locks-before.json").write_text(
    json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n"
)
PY
```

Never write, move, link, chmod, delete, stage, or clean anything below `$PARENT`. Never use the protected parent as an execution root. Never lower the contract-fixed reserve of `21_474_836_480` bytes. Historical, superseding, r2, v5.1, old recovery evidence, and existing r3 forensic artifacts remain immutable.

### Task 1: Build the protocol, root-local adapter, and external descriptor capability

**Files:**

- Create: `mlb_backtest/v52_external_protected_parent.py`
- Create: `tests/test_v52_external_protected_parent.py`
- Modify: `mlb_backtest/v52_legacy.py`
- Modify: `mlb_backtest/v52_protected_inventory.py`
- Test: `tests/test_v52_legacy.py`
- Test: `tests/test_v52_protected_inventory.py`

- [ ] **Step 1: Add RED protocol and factory tests**

Create a temporary split-root fixture for the private descriptor binder only: a disjoint physical raw root, a minimal parsed record set, and exactly these four zero-byte single-link root locks:

```python
ROOT_LOCKS = (
    ".baseball_savant_statcast.lock",
    ".mlb_schedule.lock",
    ".mlb_stats_pitcher_bf.lock",
    ".mlb_stats_pitching.lock",
)
```

The synthetic fixture must call a private record/tree-binding primitive, not the public r3 factory. It must not add a test-only RunSpec, contract, authority selector, inventory-hash override, or production bypass. Add named tests that require:

```python
class ExternalProtectedParentCapabilityTest(unittest.TestCase):
    def test_private_split_root_binder_reads_authenticated_record_descriptors(self): ...
    def test_factory_accepts_only_target_canonical_inventory(self): ...
    def test_factory_rejects_target_parent_overlap_or_parent_inside_target(self): ...
    def test_factory_rejects_copy_hardlink_symlink_fifo_missing_and_extra_members(self): ...
    def test_factory_rejects_inode_content_inventory_and_ancestor_swaps(self): ...
    def test_rebind_rejects_changed_root_lock_identity_or_contents(self): ...
    def test_bound_capability_does_not_expose_serializable_parent_path(self): ...
    def test_close_releases_descriptors_and_future_operations_fail_closed(self): ...
```

The public-factory positive case is the slow real-corpus test in Task 6 because the exact r3 contract binds the real 4,711-object inventory SHA. Unit tests may test public-factory rejection with malformed/caller-selected inputs, but may not make a synthetic inventory pass exact-r3 authentication.

Add `V52LegacyTest.test_root_local_adapter_preserves_catalog_bytes_and_rebind_failures` so the current root-local behavior is pinned before refactoring.

- [ ] **Step 2: Run the RED tests**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_external_protected_parent \
  tests.test_v52_legacy \
  tests.test_v52_protected_inventory
```

Expected: the new module/protocol/factory assertions fail because the capability does not exist; existing root-local tests remain green.

- [ ] **Step 3: Define the public capability boundary**

In `v52_legacy.py`, expose the immutable catalog record type and add:

```python
class LegacyEvidenceCapability(Protocol):
    logical_raw_root: PurePosixPath
    inventory_sha256: str
    records: tuple[ProtectedRawRecord, ...]

    def validate(self) -> None: ...
    def rebind(self) -> None: ...
    def read_bytes(self, relative_path: PurePosixPath) -> bytes: ...
    def open_bound_object(self, relative_path: PurePosixPath) -> BinaryIO: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class RootLocalLegacyEvidenceAdapter:
    catalog: ProtectedLegacyCatalog
```

`ProtectedRawRecord` is a new canonical logical record. It contains source/query identity, `PurePosixPath` manifest/seal/payload identities below `logical_raw_root`, and the existing SHA-256/size/device/inode bindings. It contains no repository root and no absolute `Path`. `RootLocalLegacyEvidenceAdapter` translates the current `_CatalogRecord` values to that public shape; `ExternalProtectedParentCapability` constructs the same shape directly. Do not alias `_CatalogRecord` because it embeds absolute physical `LegacyCandidate` paths.

The adapter delegates to the existing catalog validation and descriptor reads and implements `close()` as a no-op. The external capability owns its directory descriptors, implements `close()` plus context-manager entry/exit, and uses a finalizer only as a leak safety net. Acquisition owns it for one process in `try/finally`; `FinalizationContext` owns and closes it through its context-manager/`close()` boundary, and the CLI closes the context in `_dispatch` cleanup. `validate()`, `rebind()`, and reads fail closed after closure. Keep `load_protected_legacy_catalog()` and every root-local integrity rule unchanged. No public consumer may require the concrete `ProtectedLegacyCatalog` after Task 2.

The Protocol is a static typing boundary, not an authority admission check. Both concrete implementations carry a module-private issuer token that can be created only by `adapt_root_local_legacy_catalog()` or `build_external_protected_parent_capability()`. Every trusted public consumer checks the concrete issued capability through one internal admission function and rejects duck-typed objects, direct/forged dataclass instances, and subclasses with substituted readers. Add `test_duck_typed_or_directly_constructed_capability_is_rejected_by_public_consumers`.

- [ ] **Step 4: Implement the external factory with closed r3 authority selection**

In the new module add exactly:

```python
@dataclass(frozen=True)
class ExternalProtectedParentInputs:
    target_root: Path
    physical_raw_root: Path
    target_protected_inventory_path: Path
    run_spec_path: Path
    acquisition_contract_path: Path


def build_external_protected_parent_capability(
    inputs: ExternalProtectedParentInputs,
) -> ExternalProtectedParentCapability: ...
```

The factory must:

1. authenticate the exact r3 RunSpec and acquisition contract;
2. require the no-follow target-local path `audit/v5_1_bt/protected_raw_inventory.json`;
3. compare its bytes to the contract-bound protected-inventory SHA-256;
4. parse with `parse_protected_raw_inventory_document()`;
5. validate all record paths, type, device, inode, size, SHA-256, and `st_nlink == 1` against the disjoint physical root using descriptor-relative traversal;
6. require the exact four root locks, each regular, single-link, and zero-byte;
7. reject any unexpected inventory-closed member; and
8. retain private directory descriptors/identities for reads and rebinds without exposing or serializing the parent absolute path.

Refactor only the reusable no-follow primitives from `v52_protected_inventory.py`; do not weaken `verify_protected_raw_inventory()` or `snapshot_protected_inventory_membership()`.

- [ ] **Step 5: Run GREEN and commit the boundary**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_external_protected_parent \
  tests.test_v52_legacy \
  tests.test_v52_protected_inventory
"$PY311" -m ruff check \
  mlb_backtest/v52_external_protected_parent.py \
  mlb_backtest/v52_legacy.py \
  mlb_backtest/v52_protected_inventory.py \
  tests/test_v52_external_protected_parent.py \
  tests/test_v52_legacy.py \
  tests/test_v52_protected_inventory.py
git diff --check
git add mlb_backtest/v52_external_protected_parent.py mlb_backtest/v52_legacy.py mlb_backtest/v52_protected_inventory.py tests/test_v52_external_protected_parent.py tests/test_v52_legacy.py tests/test_v52_protected_inventory.py
git commit -m "feat: add external protected parent capability"
```

Expected: mutation matrix GREEN; the commit contains no protocol JSON, audit, data, report, config, or parent file.

### Task 2: Convert legacy decisions and promotion to the capability protocol

**Files:**

- Modify: `mlb_backtest/v52_legacy.py`
- Modify: `mlb_backtest/v52_legacy_decision_inventory.py`
- Test: `tests/test_v52_legacy.py`
- Test: `tests/test_v52_legacy_decision_inventory.py`

- [ ] **Step 1: Add RED split-root consumer tests**

Add tests for `audit_legacy_candidate`, `audit_legacy_candidates_evidence`, `promote_legacy_candidate`, preview/build/publish/load of the legacy-decision inventory, and the final rebind. Run each once through `RootLocalLegacyEvidenceAdapter` and once through `ExternalProtectedParentCapability`. Assert equal canonical decisions, semantic hashes, and logical relative references.

Add attacks where the parent object or target inventory changes after preview but before publication. Assert no inventory or promotion receipt is published.

- [ ] **Step 2: Run RED**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_legacy \
  tests.test_v52_legacy_decision_inventory
```

Expected: external instances fail the concrete `ProtectedLegacyCatalog` gates and root-relative path conversion.

- [ ] **Step 3: Replace concrete catalog assumptions**

Change `_revalidate_catalog`, `_verify_selected_revalidated`, all public audit/promote functions, `preview_legacy_decisions`, `_authority_identities`, `_assemble`, and inventory rebinds to accept `LegacyEvidenceCapability`. Read objects only through `read_bytes()` or `open_bound_object()` and serialize only the contract-fixed logical path under `data/v5_1_bt/raw`.

For older authorities, construct `RootLocalLegacyEvidenceAdapter(load_protected_legacy_catalog(...))`. For r3, require the already-authenticated external capability; `_assemble` must not silently reconstruct a target-root catalog. Both implementations expose only the new logical `ProtectedRawRecord` shape to consumers; legacy promotion constructs its existing canonical reference bytes from those logical identities.

- [ ] **Step 4: Run GREEN and commit**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_legacy \
  tests.test_v52_legacy_decision_inventory
"$PY311" -m ruff check mlb_backtest/v52_legacy.py mlb_backtest/v52_legacy_decision_inventory.py tests/test_v52_legacy.py tests/test_v52_legacy_decision_inventory.py
git diff --check
git add mlb_backtest/v52_legacy.py mlb_backtest/v52_legacy_decision_inventory.py tests/test_v52_legacy.py tests/test_v52_legacy_decision_inventory.py
git commit -m "refactor: route legacy decisions through capability"
```

Expected: root-local output bytes stay unchanged and split-root output contains no absolute parent path.

### Task 3: Thread the capability through evidence publication and verification

**Files:**

- Modify: `mlb_backtest/v52_evidence.py`
- Modify: `mlb_backtest/v52_game_evidence_inventory.py`
- Test: `tests/test_v52_evidence.py`
- Test: `tests/test_v52_game_evidence_inventory.py`

- [ ] **Step 1: Add RED evidence tests**

Extend the existing protected-reference fixtures so `V52EvidenceStore.publish_legacy_reference()`, verified reads, and game-evidence inventory construction accept the same external capability. Add races at both current final-rebind seams: replace a bound parent object or target inventory after validation and assert atomic failure with no partial success marker or final inventory. Add construction tests for `V52EvidenceStore(root, *, legacy_evidence=...)` and `V52EvidenceStore.open_read_only(root, *, legacy_evidence=...)`; a closed capability must fail before any read/write.

Pin that the existing r3 recovery parent catalog and its four parent-tree fingerprints are still a separate authority and are not replaced by the raw legacy capability.

- [ ] **Step 2: Run RED**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_evidence \
  tests.test_v52_game_evidence_inventory
```

Expected: `_legacy_repository_root()`, `_read_repository_snapshot()`, and `_bound_snapshot(root, ...)` reject the external lineage.

- [ ] **Step 3: Remove root reconstruction from legacy evidence paths**

Add keyword-only `legacy_evidence: LegacyEvidenceCapability | None` to `V52EvidenceStore` construction and `open_read_only()` and retain it on the store. Pass it into `_validated_legacy_reference`, `publish_legacy_reference`, verified read revalidation, and the `PROTECTED_LEGACY_REFERENCE` game-inventory branch. R3 callers must supply it; older root-local callers either supply `RootLocalLegacyEvidenceAdapter` or use the current root-local default. Resolve target-local receipts from the target root, but read protected manifest/seal/payload objects through the capability. Replace every legacy lineage `path.relative_to(target_root)` assumption with canonical logical identities supplied by the capability.

Do not change `_build_r3_recovery_catalog`, `_r3_catalog_parent_fingerprint`, or the missing-venue recovery-parent authority except for type plumbing required to keep the two capabilities explicit.

- [ ] **Step 4: Run GREEN and commit**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_evidence \
  tests.test_v52_game_evidence_inventory
"$PY311" -m ruff check mlb_backtest/v52_evidence.py mlb_backtest/v52_game_evidence_inventory.py tests/test_v52_evidence.py tests/test_v52_game_evidence_inventory.py
git diff --check
git add mlb_backtest/v52_evidence.py mlb_backtest/v52_game_evidence_inventory.py tests/test_v52_evidence.py tests/test_v52_game_evidence_inventory.py
git commit -m "refactor: authenticate legacy evidence by capability"
```

### Task 4: Integrate acquisition, resume, terminal replay, and locked checkpoint publication

**Files:**

- Modify: `mlb_backtest/v52_acquire.py`
- Modify: `mlb_backtest/execution_registry.py`
- Modify: `mlb_backtest/v52_query_plan.py`
- Test: `tests/test_v52_acquire.py`
- Test: `tests/test_v52_acquire_e2e.py`
- Test: `tests/test_execution_registry.py`
- Test: `tests/test_v52_query_plan.py`

- [ ] **Step 1: Add RED r3 acquisition and race tests**

Add tests that pass the external `$PARENT_RAW` shape to the r3 acquisition request and prove:

- initial authentication happens before any target runtime mutation;
- `--resume` fully reauthenticates the supplied parent in the new process;
- REMAINING preview/promotion and terminal replay reuse the capability rather than loading `root/data/v5_1_bt/raw`;
- historical/superseding/r2/v5.1 omit `--legacy-root` or accept only their exact root-local raw path;
- `acquire_sources()`, `_AcquisitionRequest`, parser normalization, and resume preserve the same `Path | None` meaning end-to-end;
- mutation after initial authentication but before the `DESIGN_ACCEPTED -> ACQUIRING` receipt leaves no phase change;
- `build_participant_derived_batch()` and registry checkpoint semantic verification receive the retained capability and never create an unbound `V52EvidenceStore` on r3;
- a parent mutation in the registry publication race hook leaves no checkpoint, receipt, or phase change; and
- parent reads are offline while all locks, reservations, partial outputs, and quarantine evidence are target-local.

- [ ] **Step 2: Run RED**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_acquire \
  tests.test_v52_acquire_e2e \
  tests.test_execution_registry
```

Expected: `_validate_request_contract()` rejects the external r3 path and later stages rebuild a root-local catalog.

- [ ] **Step 3: Select and retain one capability per acquisition process**

Change `acquire_sources(..., legacy_root: Path | None = None)`, `_AcquisitionRequest.legacy_root`, and CLI parsing to carry `Path | None`. `--legacy-root` becomes parser-optional. After exact authority authentication, r3 requires a non-`None` external path; non-r3 omission normalizes to `ROOT/data/v5_1_bt/raw`; a supplied non-r3 value must equal that canonical root-local path or fail closed.

In `_real_authenticate()`, select by exact authenticated authority:

```python
if authority.is_r3:
    legacy_evidence = build_external_protected_parent_capability(...)
else:
    legacy_evidence = RootLocalLegacyEvidenceAdapter(
        load_protected_legacy_catalog(...)
    )
```

Return that capability in authenticated acquisition state and thread it through `_real_run_stages`, REMAINING validation/promotion, resume reconstruction, and `_real_replay_terminal`. Do not call `load_protected_legacy_catalog()` again on the r3 path. Keep the target output raw root under the canonical r3 namespace.

Add a keyword-only capability parameter to `v52_query_plan.build_participant_derived_batch()` and its internal game-inventory/evidence-store loading path. Acquisition and registry checkpoint semantic validation pass the retained r3 capability; older branches preserve their root-local adapter/default. Every registry construction of `V52EvidenceStore` used by r3 receives that same capability.

- [ ] **Step 4: Add composable locked state verifiers**

Extend `publish_acquisition_checkpoint()`, the generic phase-advance path used by `_real_begin_runtime()`, and `advance_sources_ready_phase()` with a keyword-only `publication_state_verifier: Callable[[], None] | None = None`. `publish_acquisition_checkpoint()` also receives `legacy_evidence: LegacyEvidenceCapability | None` so its semantic verifier can pass the capability through `build_participant_derived_batch()` and every r3 evidence-store construction. Compose the publication callback with the current semantic verifier so `_publish_locked()` invokes both before the write and again before return while holding the same exclusive registry lock:

```python
def verify_publication_state() -> None:
    verify_existing_semantics()
    if publication_state_verifier is not None:
        publication_state_verifier()
```

R3 passes `legacy_evidence.rebind` from `_real_begin_runtime()` before the `ACQUIRING` receipt, every checkpoint publication, and the source-ready advance; older authorities pass `None`. Existing/idempotent success branches must invoke the verifier while holding their publication lock before returning. `_real_replay_terminal()` performs one final `rebind()` immediately before successful terminal return. The public read-only `validate_sources_ready_evidence_set()` remains diagnostic and does not substitute for the locked callback.

- [ ] **Step 5: Run GREEN and commit**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_acquire \
  tests.test_v52_acquire_e2e \
  tests.test_execution_registry \
  tests.test_v52_query_plan
"$PY311" -m ruff check mlb_backtest/v52_acquire.py mlb_backtest/execution_registry.py mlb_backtest/v52_query_plan.py tests/test_v52_acquire.py tests/test_v52_acquire_e2e.py tests/test_execution_registry.py tests/test_v52_query_plan.py
git diff --check
git add mlb_backtest/v52_acquire.py mlb_backtest/execution_registry.py mlb_backtest/v52_query_plan.py tests/test_v52_acquire.py tests/test_v52_acquire_e2e.py tests/test_execution_registry.py tests/test_v52_query_plan.py
git commit -m "feat: bind r3 acquisition to protected parent"
```

### Task 5: Add source-finalization CLI and post-seal context plumbing

**Files:**

- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `mlb_backtest/v52_source_finalize.py`
- Modify: `mlb_backtest/v52_verify_sources.py`
- Modify: `mlb_backtest/v52_snapshot_store.py`
- Test: `tests/test_v52_source_finalize.py`
- Test: `tests/test_v52_snapshot_store.py`
- Test: `tests/test_v52_verify_sources.py`

- [ ] **Step 1: Add RED parser, context, and finalization tests**

Require `--legacy-root` on r3 `status`, `preflight`, `publish`, `review`, `replay`, and `advance`; add it to duplicate single-value conflict protection. Assert omission fails for r3, an external value fails for non-r3, and exact root-local omission/value preserves older behavior.

Add `build_context(CommonArgs(..., legacy_root=parent_raw))` tests that rebind the parent before returning `snapshot_inputs`, retain the same capability through `_rebuild_context_inventory`, and invoke the capability inside review/replay/advance publication boundaries. Cover `load_snapshot_replay_inputs()`, `load_r3_terminal_verified_evidence()`, and their game-inventory/evidence-store construction so no r3 loader reopens target-root legacy objects. Add the post-seal API sequence:

```python
context = build_context(common)
validate_sources_ready_evidence_set(...)
loaded = load_snapshot_store(
    source_root,
    context.snapshot_inputs,
    expected_root=source_root,
)
self.assertEqual(len(loaded.inventory.snapshots), 81)
self.assertEqual(len(loaded.inventory.source_season_pairs), 42)
```

Add named atomicity tests:

```text
test_publish_inventory_post_link_parent_rebind_failure_removes_only_owned_inventory
test_publish_inventory_idempotent_return_rebinds_parent_under_lock
test_review_parent_rebind_failure_removes_owned_receipt_and_commit
test_replay_parent_rebind_failure_removes_owned_receipt_and_commit
test_receipt_cleanup_never_removes_preexisting_winner
test_context_close_releases_external_parent_capability
```

- [ ] **Step 2: Run RED**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_source_finalize \
  tests.test_v52_snapshot_store \
  tests.test_v52_verify_sources
```

Expected: `CommonArgs` drops `legacy_root`, the parser rejects the flag, and context reconstruction reopens the target-root inventory.

- [ ] **Step 3: Implement the common CLI/context path**

Add `legacy_root: Path | None` to `CommonArgs` and the retained capability to `FinalizationContext`. Add `--legacy-root` to the parser, `_SINGLE_VALUE_OPTIONS`, `_common_args`, and all six subcommands. `build_context()` constructs the exact authority-selected capability once; `_rebind_context_authorities()` retains it. Replace `_rebuild_context_inventory()`'s recursive public `build_context(CommonArgs(...))` call with a private reconstruction helper that rebinds and reuses `context.legacy_evidence`, so it creates no second descriptor owner. `load_snapshot_replay_inputs()` and `load_r3_terminal_verified_evidence()` take the retained capability and pass it to every game-inventory and `V52EvidenceStore` construction. Review, replay, publish, terminal replay, and final post-seal reconstruction call its `rebind()` at their dependency-rebind seams. `advance_sources_ready()` must pass `context.legacy_evidence.rebind` explicitly as `publication_state_verifier` to `execution_registry.advance_sources_ready_phase()`; an unlocked pre-call is not sufficient.

`build_context()` returns an owned context manager. Every non-CLI caller, including tests and Task 7 verification, uses `with build_context(common) as context:`; the CLI wraps dispatch in `try/finally: context.close()`. A caller that deliberately transfers ownership must call `close()` explicitly. Add a descriptor-count regression that repeated context build/close cycles do not leak FDs.

Make receipt and source-inventory visibility atomic with parent authentication:

1. `_write_validated_receipt()` rebinds before its exclusive write and retains the exact newly-created safe snapshot.
2. If its post-write rebind fails, remove only that invocation's new file through a descriptor-relative unlink after proving the inode and bytes still equal the owned snapshot.
3. `write_review()` and `write_replay()` track both the receipt and commit marker; if a later marker/rebind fails, remove their newly-created artifacts in reverse order. Never remove a preexisting winner.
4. `publish_snapshot_store_from_fd()` accepts `publication_state_verifier`; it runs on the existing/idempotent return, immediately before linking `source_inventory.json`, and after the link while the source-store lock is held.
5. If the post-link verifier fails, descriptor-unlink only the newly-created canonical inventory after proving its inode/bytes; immutable blobs/checksum may remain as safe retry residue. A preexisting committed inventory is never removed.

Do not add a capability parameter to `load_snapshot_store()`. The authenticated boundary remains `build_context()` and its returned `snapshot_inputs`.

- [ ] **Step 4: Run GREEN and commit**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_source_finalize \
  tests.test_v52_snapshot_store \
  tests.test_v52_verify_sources
"$PY311" -m ruff check mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_source_finalize.py mlb_backtest/v52_verify_sources.py mlb_backtest/v52_snapshot_store.py tests/test_v52_source_finalize.py tests/test_v52_snapshot_store.py tests/test_v52_verify_sources.py
git diff --check
git add mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_source_finalize.py mlb_backtest/v52_verify_sources.py mlb_backtest/v52_snapshot_store.py tests/test_v52_source_finalize.py tests/test_v52_snapshot_store.py tests/test_v52_verify_sources.py
git commit -m "feat: finalize sources with external parent capability"
```

### Task 6: Prove the real protected corpus, backwards compatibility, and exact-suite closure

**Files:**

- Modify: `tests/test_task13_exact_suite_runner.py`
- Modify: `scripts/run_task13_exact_suite.py`
- Test: all modules changed in Tasks 1–5

- [ ] **Step 1: Run the real-parent read-only capability test**

First add `tests.test_v52_external_protected_parent` to `scripts/run_task13_exact_suite.py::CHILD_MODULES`. Add `Task13ExactSuiteRunnerTest.test_external_parent_capability_module_is_in_exact_suite` so removal fails closed. Mark the 4,711-object test with the repository's existing opt-in slow-test gate. It must authenticate the real target inventory against `$PARENT_RAW`, verify all 4,711 records plus four locks, read at least one object from every source family through descriptors, and compare before/after target and parent snapshots. It must not hard-link or copy the raw corpus.

```bash
MLB_V52_RUN_SLOW_INTEGRATION=1 \
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
"$PY311" -m unittest -v \
  tests.test_v52_external_protected_parent.ExternalProtectedParentCapabilityTest.test_real_protected_parent_authenticates_4711_objects_without_writes
```

Expected: GREEN, object count 4,711, four lock identities unchanged, no parent write.

- [ ] **Step 2: Run focused compatibility suites**

```bash
"$PY311" -m unittest -v \
  tests.test_v52_acquisition_contract \
  tests.test_v52_protected_inventory \
  tests.test_v52_legacy \
  tests.test_v52_legacy_decision_inventory \
  tests.test_v52_evidence \
  tests.test_v52_game_evidence_inventory \
  tests.test_v52_acquire \
  tests.test_v52_acquire_e2e \
  tests.test_execution_registry \
  tests.test_v52_source_finalize \
  tests.test_v52_snapshot_store \
  tests.test_v52_verify_sources
```

Expected: zero failures/errors; historical, superseding, r2, and v5.1 golden bytes remain unchanged.

- [ ] **Step 3: Run static checks and protected hash verification**

```bash
"$PY311" -m ruff check mlb_backtest tests scripts/run_task13_exact_suite.py
"$PY311" -m compileall -q mlb_backtest tests scripts/run_task13_exact_suite.py
git diff --check
```

Rerun the baseline manifest command with `MANIFEST_OUT=/private/tmp/v52-external-parent-protected-after.json`. Rerun the four-lock command with its output path changed to `/private/tmp/v52-external-parent-locks-after.json`, then require:

```bash
cmp /private/tmp/v52-external-parent-protected-before.json /private/tmp/v52-external-parent-protected-after.json
cmp /private/tmp/v52-external-parent-locks-before.json /private/tmp/v52-external-parent-locks-after.json
```

- [ ] **Step 4: Obtain independent spec and code reviews**

Require one spec-compliance review and one code-quality/security review. Resolve every P0–P2 finding with RED–GREEN tests, rerun the affected suites, and record both final verdicts as `APPROVED` with no P0–P2 findings.

- [ ] **Step 5: Commit review fixes and create a clean detached review worktree**

```bash
git status --short
git log --oneline --decorate -8
git worktree add --detach /private/tmp/v52-external-parent-exact HEAD
```

Expected: implementation worktree clean; detached worktree HEAD equals the reviewed commit.

- [ ] **Step 6: Run the canonical Task 13 exact suite once on the frozen HEAD**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
"$PY311" scripts/run_task13_exact_suite.py \
  --repo /private/tmp/v52-external-parent-exact \
  --v51-input-root "$PARENT" \
  --old-v52-evidence-root "$PARENT"
```

Expected: exit code 0, zero failures/errors, only the suite's documented skips, Ruff/diff-check GREEN, and every protected hash/fingerprint unchanged. Do not count the historical Python 3.14 `cryptography` failure as a product result.

### Task 7: Execute and seal the final r3 source authority

**Files written only under the target r3 namespaces:**

- `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/**`
- `data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/raw/**`
- `data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/sources/**`
- `reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/**`

- [ ] **Step 1: Enforce the 20 GiB reserve without deleting protected evidence**

```bash
"$PY311" - <<'PY'
import shutil
from pathlib import Path

root = Path('/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd')
free = shutil.disk_usage(root).free
required = 21_474_836_480
print({"free_bytes": free, "required_bytes": required})
raise SystemExit(0 if free >= required else 1)
PY
```

Expected: exit 0 before acquisition. If it exits 1, stop only Task 7 and report the exact deficit; do not lower the reserve and do not delete any protected/forensic path. Tasks 1–6 remain complete.

- [ ] **Step 2: Run acquisition to authenticated terminal REMAINING**

Set the exact tracked r3 paths:

```bash
export RUN_SPEC="$CODE_ROOT/protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
export CONTRACT="$CODE_ROOT/protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
export PROTECTED_INVENTORY="$CODE_ROOT/audit/v5_1_bt/protected_raw_inventory.json"
```

Start or resume with the same authenticated arguments:

```bash
"$PY311" -m mlb_backtest.v52_acquire \
  --root "$CODE_ROOT" \
  --run-spec "$RUN_SPEC" \
  --acquisition-contract "$CONTRACT" \
  --years 2018:2024 \
  --mode hybrid \
  --legacy-root "$PARENT_RAW" \
  --protected-inventory "$PROTECTED_INVENTORY" \
  --reserve-bytes 21474836480
```

Use the existing `--resume` flag only if the target r3 checkpoint chain already exists and authenticates. Expected: terminal `REMAINING`, seven completed/dispositioned source queries, zero pending/quarantined, and no parent mutation.

- [ ] **Step 3: Publish, review, replay twice, and atomically advance**

Use this exact common prefix for every command:

```bash
"$PY311" -m mlb_backtest.v52_source_finalize SUBCOMMAND \
  --root "$CODE_ROOT" \
  --run-spec "$RUN_SPEC" \
  --acquisition-contract "$CONTRACT" \
  --protected-inventory "$PROTECTED_INVENTORY" \
  --legacy-root "$PARENT_RAW"
```

Set deterministic target-local paths and create the canonical review selection from the published inventory:

```bash
export R3_AUDIT="$CODE_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3"
export INVENTORY="$CODE_ROOT/data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/sources/source_inventory.json"
export REVIEW_DIR="$R3_AUDIT/acquisition/source_reviews"
export REPLAY_DIR="$R3_AUDIT/acquisition/source_replays"
export QUERY_IDS="$REVIEW_DIR/independent-source-auditor-query-ids.json"
export REVIEW_RECEIPT="$REVIEW_DIR/independent-source-auditor.json"
export REVIEW_COMMIT="$REVIEW_DIR/independent-source-auditor.commit.json"
export REPLAY_ONE_RECEIPT="$REPLAY_DIR/offline-replay-1.json"
export REPLAY_ONE_COMMIT="$REPLAY_DIR/offline-replay-1.commit.json"
export REPLAY_TWO_RECEIPT="$REPLAY_DIR/offline-replay-2.json"
export REPLAY_TWO_COMMIT="$REPLAY_DIR/offline-replay-2.commit.json"

"$PY311" - <<'PY'
import json
import os
from pathlib import Path

inventory = json.loads(Path(os.environ["INVENTORY"]).read_bytes())
query_ids = inventory["logical_query_ids"]
if not isinstance(query_ids, list) or not query_ids:
    raise SystemExit("published source inventory has no logical_query_ids")
Path(os.environ["QUERY_IDS"]).parent.mkdir(parents=True, exist_ok=True)
Path(os.environ["QUERY_IDS"]).write_text(
    json.dumps({"query_ids": query_ids}, sort_keys=True, separators=(",", ":")) + "\n"
)
PY
```

Run the six services with explicit arguments:

```bash
"$PY311" -m mlb_backtest.v52_source_finalize status --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --json
"$PY311" -m mlb_backtest.v52_source_finalize preflight --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --json
"$PY311" -m mlb_backtest.v52_source_finalize publish --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --lock-owner final-r3-source-seal --json
"$PY311" -m mlb_backtest.v52_source_finalize review --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --reviewer-identity independent-source-auditor --reviewer-role independent-source-auditor --decision APPROVED --query-ids-file "$QUERY_IDS" --write-receipt "$REVIEW_RECEIPT" --write-commit "$REVIEW_COMMIT" --json
"$PY311" -m mlb_backtest.v52_source_finalize replay --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --write-receipt "$REPLAY_ONE_RECEIPT" --write-commit "$REPLAY_ONE_COMMIT" --json
"$PY311" -m mlb_backtest.v52_source_finalize replay --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --write-receipt "$REPLAY_TWO_RECEIPT" --write-commit "$REPLAY_TWO_COMMIT" --json
```

Derive the exact terminal checkpoint path from authenticated `status` output rather than guessing its receipt hash:

```bash
export STATUS_JSON=/private/tmp/v52-r3-final-status.json
"$PY311" -m mlb_backtest.v52_source_finalize status --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --json > "$STATUS_JSON"
export CHECKPOINT="$R3_AUDIT/execution/acquisition_checkpoints/$("$PY311" -c 'import json,os; d=json.load(open(os.environ["STATUS_JSON"])); c=d["result"]["checkpoint"]; print("%08d-%s.json" % (c["sequence"], c["head"]))')"
"$PY311" -m mlb_backtest.v52_source_finalize advance --root "$CODE_ROOT" --run-spec "$RUN_SPEC" --acquisition-contract "$CONTRACT" --protected-inventory "$PROTECTED_INVENTORY" --legacy-root "$PARENT_RAW" --review-receipt "$REVIEW_COMMIT" --replay-receipt "$REPLAY_ONE_COMMIT" --replay-receipt "$REPLAY_TWO_COMMIT" --checkpoint "$CHECKPOINT" --inventory "$INVENTORY" --json
```

Expected: one approved review, two different replay receipts, three commit markers, 81 snapshots, 42 source-season pairs, and atomic phase `SOURCES_READY`.

- [ ] **Step 4: Verify the sealed public API and final invariants**

Use `with build_context(CommonArgs(..., legacy_root=Path(PARENT_RAW))) as context:` to run `validate_sources_ready_evidence_set()` and `load_snapshot_store()` against the sealed tree. Require exact counts 81/42, valid terminal checkpoint, valid ordered three commit markers, clean Git HEAD, and unchanged parent/hash manifests from Task 6.

- [ ] **Step 5: Record final completion evidence**

Report the acquisition exit code and terminal summary, finalization receipt hashes, registry phase, snapshot/source-season counts, exact-suite summary, Ruff/diff-check status, 16-file/3-file/8-file/4-fingerprint/2-hash preservation status, four raw-root lock identities, and both review verdicts. Only then mark the external-parent capability and r3 source seal complete.
