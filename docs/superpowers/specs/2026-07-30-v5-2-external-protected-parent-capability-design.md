# v5.2 External Protected Parent Capability Design

**Status:** Approved direction A on 2026-07-30  
**Scope:** Permit the final r3 acquisition to authenticate and read the original inode-bound v5.1 raw tree from its protected parent worktree without copying, linking, or weakening existing evidence.

## Problem

The final r3 code and authority are in the clean target worktree:

```text
/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd
```

The contract-bound v5.1 raw objects are in the protected parent:

```text
/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest/data/v5_1_bt/raw
```

The target contains the canonical protected inventory but not the raw objects. Existing acquisition requires the physical raw tree below the execution root. Materializing a target-local copy cannot satisfy the current trust model:

- a byte copy changes the inventory-bound device and inode values;
- a hard link preserves the inode but changes `st_nlink` from one and is rejected;
- a symbolic link is rejected by no-follow path validation; and
- regenerating the inventory for copied inodes changes the contract-bound protected inventory SHA-256.

The protected raw tree contains 4,711 unique bound objects and four required root lock files, totaling 3,186,182,528 object bytes. It must remain in place and read-only.

## Decision

Add one public, immutable external-parent capability for the r3 authority. It authenticates the original protected raw tree in place and exposes descriptor-bound reads to acquisition. It does not copy data, relax inode or link-count checks, or persist host-specific absolute paths in canonical evidence.

The capability is available only when all of these are true:

- the loaded RunSpec and acquisition contract are the exact r3 authority;
- the protected inventory path and SHA-256 equal the r3 contract constants;
- the physical parent root is outside and disjoint from the target execution root;
- every required file is reachable through no-follow directory-descriptor traversal;
- every inventory record matches its relative path, type, device, inode, size, content hash, and `st_nlink == 1`; and
- the logical raw namespace remains the contract-fixed `data/v5_1_bt/raw`.

Historical, superseding, r2, and v5.1 execution paths remain root-local and byte-semantic unchanged.

## Public Boundary

The implementation introduces a small public value/capability pair, with final names chosen to follow the existing source-contract module:

```python
@dataclass(frozen=True)
class ExternalProtectedParentInputs:
    target_root: Path
    physical_raw_root: Path
    target_protected_inventory_path: Path
    run_spec_path: Path
    acquisition_contract_path: Path


class ExternalProtectedParentCapability:
    logical_raw_root: PurePosixPath
    inventory_sha256: str
    object_count: int
    legacy_evidence: LegacyEvidenceCapability

    def validate(self) -> None: ...
    def rebind(self) -> None: ...
    def open_bound_object(self, relative_path: PurePosixPath) -> BinaryIO: ...


def build_external_protected_parent_capability(
    inputs: ExternalProtectedParentInputs,
) -> ExternalProtectedParentCapability: ...
```

Callers cannot construct authenticated state from free-form mappings. A public factory loads the exact r3 RunSpec and contract, validates the protected inventory, opens the parent directory chain with no-follow semantics, and returns the capability.

`target_protected_inventory_path` must resolve, through no-follow traversal, to the exact target-local canonical path:

```text
audit/v5_1_bt/protected_raw_inventory.json
```

The factory reads only this target-local inventory and checks its bytes against the r3 contract SHA-256. A parent-local or caller-selected inventory is never accepted. The external raw descriptor tree is verified against the records parsed from the authenticated target-local inventory.

The capability stores canonical relative identities, an immutable `LegacyEvidenceCapability`, and private descriptor-bound state. Existing r3 consumers receive evidence records and raw readers only from this capability; they do not independently reopen a root-local raw tree. The capability does not expose or serialize the external absolute path. Canonical receipts continue to bind:

- r3 run ID, RunSpec, acquisition contract, and Git HEAD;
- logical raw-root namespace;
- protected inventory relative path and SHA-256;
- query, checkpoint, inventory, and snapshot hashes; and
- existing parent evidence fingerprints.

## Trust and Descriptor Model

Validation and reads use the existing secure filesystem primitives:

1. Open each ancestor and directory with `O_DIRECTORY`, `O_NOFOLLOW`, and descriptor-relative traversal.
2. Reject symbolic links, hard links, special files, missing members, extra members where the inventory requires closure, and ancestor identity changes.
3. Compare `fstat` device, inode, and size with the contract-bound inventory; independently require a regular file and `st_nlink == 1`.
4. Hash bytes from the authenticated file descriptor, not by reopening a string path.
5. Recheck the parent directory identities and inventory binding at every public mutation boundary.
6. Perform a final capability rebind inside the registry publication lock/state-verifier callback before each checkpoint or receipt becomes visible, and again before final terminal return.

An ancestor swap, inventory replacement, file replacement, content mutation, link-count change, descriptor/path mismatch, or parent-root overlap fails closed.

## Legacy Evidence Adapter Boundary

The existing `ProtectedLegacyCatalog` cannot represent this split-root authority because it requires the inventory and raw artifacts below one `repository_root` and reopens that root during revalidation. It is not embedded in the external capability.

Introduce a narrow public protocol used by legacy-decision, game-evidence, and evidence-inventory consumers:

```python
class LegacyEvidenceCapability(Protocol):
    logical_raw_root: PurePosixPath
    inventory_sha256: str
    records: tuple[ProtectedRawRecord, ...]

    def validate(self) -> None: ...
    def rebind(self) -> None: ...
    def read_bytes(self, relative_path: PurePosixPath) -> bytes: ...
    def open_bound_object(self, relative_path: PurePosixPath) -> BinaryIO: ...
```

Two implementations exist:

- `RootLocalLegacyEvidenceAdapter`, wrapping the current `ProtectedLegacyCatalog` without changing its behavior; and
- `ExternalProtectedParentCapability`, implementing the protocol directly from the target-local inventory and external descriptor tree.

Consumers no longer join `catalog.repository_root` to artifact paths. The shared consumer boundary accepts `LegacyEvidenceCapability`, resolves only canonical logical relative paths, and reads through `read_bytes()` or `open_bound_object()`. R3 uses the external implementation. Historical, superseding, r2, and v5.1 use the root-local adapter. `_revalidate_catalog`, game-evidence lineage checks, and evidence-inventory revalidation call `validate()`/`rebind()` on the protocol instead of reopening a repository root.

The adapter conversion is required in:

- `v52_acquire` and resume;
- `v52_legacy` and `v52_legacy_decision_inventory`;
- `v52_game_evidence_inventory`;
- `v52_evidence`;
- `v52_source_orchestration.build_context()` and terminal replay; and
- review, replay, atomic advance, and final post-seal context reconstruction.

## Acquisition Integration

The existing acquisition `--legacy-root` CLI argument remains the explicit physical parent input. The source-finalization CLI adds the same argument to r3 `status`, `preflight`, `publish`, `review`, `replay`, and `advance`. `CommonArgs` and resume inputs carry it to the capability factory. For r3, the flag is required and may name the external protected raw root. For other authorities it is optional; when supplied it must equal the exact canonical root-local `ROOT/data/v5_1_bt/raw`, and any external value is rejected. Omission preserves current root-local behavior.

Data flow:

1. Load and authenticate the target-local r3 RunSpec, acquisition contract, protected inventory, and Git binding.
2. Construct the external protected-parent capability from `--legacy-root`.
3. Validate the full parent inventory before creating r3 runtime state.
4. Build the immutable external `LegacyEvidenceCapability` from descriptor-authenticated reads.
5. Thread the same capability through r3 acquisition, resume, legacy-decision inventory publication/load, evidence revalidation, `v52_source_orchestration.build_context()`, terminal replay, review/replay receipt generation, and atomic `SOURCES_READY`.
6. Replace r3-only calls that reopen `root/data/v5_1_bt/raw` in `v52_legacy_decision_inventory`, `v52_evidence`, and source orchestration with capability catalog/read methods.
7. Read legacy objects exclusively through capability methods.
8. Write new acquisition data, checkpoints, inventories, receipts, and snapshot-store files only below the target r3 namespaces.
9. Run capability rebind inside each registry-locked publication state verifier and before final terminal return.

No code writes to the protected parent. Locks, reservations, partial outputs, quarantine evidence, and cleanup remain target-local.

## Exact Public Calls

Acquisition uses:

```bash
python3.11 -m mlb_backtest.v52_acquire \
  --root "$R3_ROOT" \
  --run-spec "$RUN_SPEC" \
  --acquisition-contract "$CONTRACT" \
  --years 2018:2024 \
  --mode hybrid \
  --legacy-root "$PARENT/data/v5_1_bt/raw" \
  --protected-inventory "$R3_ROOT/audit/v5_1_bt/protected_raw_inventory.json" \
  --reserve-bytes 21474836480
```

The first run omits `--resume`; resume uses the same arguments plus the existing `--resume` flag and loads the existing authenticated checkpoint chain from the target r3 namespace.

Every source-finalization command uses this common prefix:

```bash
python3.11 -m mlb_backtest.v52_source_finalize SUBCOMMAND \
  --root "$R3_ROOT" \
  --run-spec "$RUN_SPEC" \
  --acquisition-contract "$CONTRACT" \
  --protected-inventory "$R3_ROOT/audit/v5_1_bt/protected_raw_inventory.json" \
  --legacy-root "$PARENT/data/v5_1_bt/raw"
```

`SUBCOMMAND` is exactly one of `status`, `preflight`, `publish`, `review`, `replay`, or `advance`; their existing receipt-specific flags remain unchanged.

The Python boundary is:

```python
common = CommonArgs(
    root=R3_ROOT,
    run_spec=RUN_SPEC,
    acquisition_contract=CONTRACT,
    protected_inventory=R3_PROTECTED_INVENTORY,
    legacy_root=PARENT_RAW_ROOT,
)
context = build_context(common)
```

`CommonArgs.legacy_root` is `Path | None`. It is mandatory for r3 and optional/root-local-only for other authorities.

`load_snapshot_store()` does not receive the parent capability directly. `build_context()` authenticates and final-rebinds the parent before returning `context.snapshot_inputs`; Stage4C then uses:

```python
loaded = load_snapshot_store(
    source_root,
    context.snapshot_inputs,
    expected_root=source_root,
)
```

`execution_registry.validate_sources_ready_evidence_set()` continues to receive the inventory path, terminal-checkpoint path, and ordered three commit-marker paths. The orchestration layer supplies the parent capability’s `rebind` as the atomic registry state-verifier callback, so evidence validation and parent revalidation occur inside the same publication lock.

## Storage Reserve

The contract-fixed reserve remains 21,474,836,480 bytes (20 GiB). The capability design avoids an unnecessary 3.19 GB raw copy but does not lower or bypass the reserve.

The final acquisition may start only after the target filesystem reports at least 20 GiB free. Operationally, 8–10 GiB should be freed from the current machine to provide reserve plus output headroom. Space cleanup is a separate operational action and may not delete protected evidence.

## Failure and Rollback

Capability validation completes before any r3 runtime mutation. A preflight failure writes nothing.

After acquisition begins:

- failures use the existing target-local checkpoint and quarantine semantics;
- external-parent failures are classified as authority/integrity failures, not network failures;
- cleanup may remove only files created by the current r3 execution and only through existing ownership/lock rules; and
- protected parent files, old evidence, partial older namespaces, and experimental artifacts are never modified, deleted, or reclassified.

Retry requires the same target Git HEAD, RunSpec, contract, protected inventory SHA, authenticated inventory record set, and existing r3 checkpoint chain. On every new process, the supplied external root is fully reauthenticated against the target-local inventory. The path string is neither persisted nor compared; security identity is the authenticated object set. During one process, descriptor-root identity is rechecked to prevent ancestor replacement.

## Root Lock Contract

The external raw root must contain exactly these four root locks:

```text
.baseball_savant_statcast.lock
.mlb_schedule.lock
.mlb_stats_pitcher_bf.lock
.mlb_stats_pitching.lock
```

Each lock is opened no-follow, must be a regular single-link file, and must contain zero bytes. Its device/inode is captured in the live capability and rechecked during the process. Because the current protected inventory has no lock records, lock inode values are not presented as contract-bound fields and are not persisted across processes.

## Testing

Implementation follows RED–GREEN TDD.

1. The current r3 external `--legacy-root` fails before the capability exists.
2. The exact protected parent builds a capability and authenticates all 4,711 bound objects plus four locks without writing either root.
3. Byte copy, hard link, symlink, special file, missing member, extra member, inode swap, content mutation, ancestor swap, and inventory replacement fail closed.
4. Target/parent overlap and any path inside the target execution root are rejected for the external mode.
5. Historical, superseding, r2, and v5.1 paths retain their root-local behavior and canonical bytes.
6. Checkpoint and receipt publication revalidate the capability inside the registry-locked state verifier; a mutation between validation and publication leaves no partial receipt.
7. Resume rejects a parent whose authenticated object set differs from the target inventory, or a changed inventory, Git HEAD, or checkpoint binding.
8. The real r3 acquisition reaches the authenticated terminal `REMAINING` checkpoint without network access for parent reads.
9. Public-command integration runs `v52_acquire`, then `v52_source_finalize publish`, `review`, `replay` twice, and `advance`; it produces 81 snapshots, 42 source-season pairs, one approved review, two distinct offline replays, three commit markers, and atomic `SOURCES_READY`.
10. Public API integration runs `build_context(CommonArgs(..., legacy_root=...))`, `execution_registry.validate_sources_ready_evidence_set()`, and `v52_snapshot_store.load_snapshot_store()`; it proves the sealed inventory and loaded store both contain the exact 81 snapshots and 42 source-season pairs.
11. Ruff, Python compilation, schema/canonical validation, `git diff --check`, protected hashes, and the clean detached exact suite pass.

## Rejected Alternatives

### Target-local byte copy

Rejected because copied inodes cannot match the contract-bound protected inventory and the copy consumes unnecessary space.

### Hard-link or symbolic-link projection

Rejected because hard links violate the single-link invariant and symbolic links violate no-follow containment rules.

### Execute directly in the protected worktree

Rejected because that root contains 681 untracked artifacts and partial pre-r3 runs. Mixing final r3 outputs with that state would weaken isolation and make preservation claims ambiguous.

### Reissue the inventory for copied files

Rejected because it changes the approved protected inventory SHA and trust authority rather than solving the execution boundary.

## Completion Criteria

The design is complete only when:

- the external parent capability is independently reviewed with no P0/P1/P2 findings;
- no protected parent bytes or metadata are changed;
- the final r3 run is bound to the final reviewed Git HEAD;
- real acquisition, source finalization, atomic `SOURCES_READY`, and post-seal load succeed;
- the protected-hash manifest is unchanged; and
- the clean detached exact suite exits zero.
