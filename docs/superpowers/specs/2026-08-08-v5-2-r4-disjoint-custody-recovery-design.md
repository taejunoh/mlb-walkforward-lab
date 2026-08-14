# v5.2 R4 Disjoint Custody Recovery Design

**Status:** Approved Option 1 — design-only recovery authorization (2026-08-08)

**Scope:** Recover the interrupted R4 custody issuance by creating one *new*, disjoint physical-custody authority namespace. The existing incomplete R4 root and lock are forensic evidence and are never resumed, authenticated as usable authority, repaired, removed, renamed, unlocked, or otherwise mutated. The sole permitted old-path operation is a read-only, no-follow forensic fingerprint that is signed inline in the new anchor. This document authorizes no production operation by itself.

## Decision and Evidence

The prior R4 issuance targeted a custody parent under `Documents`, a location that can be governed by iCloud Drive or another File Provider. An incomplete issuance now exists at the old namespace. The approved recovery is therefore not retry-in-place and not an adoption of a prior owner capability.

Known authority facts retained by this design are:

- The immutable v5.1 source approval is `audit/v5_1_bt/protected_raw_inventory.json`, SHA-256 `2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6`.
- The approved logical closure contains 1,675 records, 5,025 record-role bindings, 4,711 distinct physical data members, and four root locks. The 4,715 protected leaf objects are those 4,711 members plus the four zero-byte locks.
- The R4 source run remains `historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4`; its source, model, parser, query, and recovery semantics are unchanged.
- Existing R4 issuance design requires APFS identity, no-follow descriptor access, immutable modes/flags, and a signed authority anchor. It does not make an incomplete root executable.
- The old incomplete evidence is exactly:

  ```text
  old custody root:
    /Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4

  old owner lock:
    /Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock
  ```

The old final name is deliberately reused only inside a different parent. A same leaf name does not imply the same custody authority, owner, lock, volume, or recovery lineage.

## Goals

1. Create at most one new immutable snapshot at a custody root outside iCloud/File Provider-managed storage.
2. Make the new authority cryptographically and physically distinguishable from every prior R4 attempt.
3. Capture seed-free, append-only before/after evidence for every pre-anchor critical operation and observed pre-anchor failure, plus deterministic post-close evidence from immutable artifacts and owner-lock state.
4. Fail closed before any copy or authority publication when the proposed custody parent, filesystem, executable, checkout, or source authority is not exactly bound.
5. Validate every protected leaf and directory's mode/flags in addition to content and identity; byte equality alone is insufficient.

## Non-goals

- Mutating, deleting, releasing, clearing flags on, copying from, renaming, or otherwise changing the old root/lock above. A bounded read-only no-follow fingerprint is permitted only to populate the signed inline forensic summary defined below.
- Continuing or adopting an old issuance after process restart, even when bytes happen to match expected content.
- Changing R2/R3 behavior, v5.2.3 parser behavior, source years, model/data/query semantics, or the approved 1,675/4,711/5,025 logical closure.
- Adding a generic recovery/adoption command or allowing caller-chosen custody generations, final names, journal locations, authority hashes, or File Provider exceptions.
- Treating a successful copy on a non-APFS, cloud-synced, removable, or detector-unavailable filesystem as an acceptable degraded mode.

## Exact Namespaces and Versioning

The only permitted new custody parent and final root are:

```text
new custody parent:
  /Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2

new final root:
  /Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/v5_1_bt_r4

new owner lock:
  /Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/.v5_1_bt_r4.disjoint-v2.issuance.lock

new temporary-root pattern:
  /Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/.v5_1_bt_r4.disjoint-v2.issuance.<32-lowercase-hex>.tmp

operational journal:
  /Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2/.v5_1_bt_r4.disjoint-v2.issuance.journal.ndjson
```

`v5_1_bt_r4` remains the fixed snapshot leaf name for compatibility with the protected-corpus meaning. Its new authority generation is the closed literal `v5.2-r4-disjoint-custody-v2`; it is not an alias for the previous R4 issuance. Every new schema, inventory, transition receipt, signed anchor, verifier, and CLI state must require this literal generation and reject the old generation.

The repository-side authority outputs are six new paths, so an older R4 artifact cannot be overwritten or silently reused:

```text
audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/
  protected_raw_inventory_r4_disjoint_v2.json
  physical_custody_transition_r4_disjoint_v2.json
protocols/v5_2_qd/
  historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json
audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/
  physical_custody_authority_anchor_r4_disjoint_v2.json
  physical_custody_authority_anchor_r4_disjoint_v2.ed25519
  custody_issuance_journal_r4_disjoint_v2.ndjson
```

The repository-side contract is a new closed v2 contract, not a mutation of `historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json`. It binds the fixed RunSpec, source approval, v2 inventory/receipt/journal paths, and `v5.2-r4-disjoint-custody-v2` generation. The journal copied into the repository is an exact, canonical, final read-only copy of the operational journal. It is not a replacement journal and is published only after the operational journal is closed. The new signed anchor binds its SHA-256 and terminal chain hash, along with the inventory, receipt, and v2 contract. The detached signature authenticates the anchor bytes; after signature verification and descriptor rebinding, all six repository outputs are frozen. No existing `*_r4` output path is replaced.

## Authority and Preflight Binding

The new inventory must retain the fixed v5.1 approval path/hash and validate the source closure before creating any target leaf. It adds these closed fields:

```text
custody_generation: "v5.2-r4-disjoint-custody-v2"
custody_parent_class: "LOCAL_APFS_NON_FILE_PROVIDER"
final_name: "v5_1_bt_r4"
operational_journal:
  format: "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1"
  sha256: <64 lowercase hex>
  terminal_event_sha256: <64 lowercase hex>
execution_binding:
  executable_sha256: <64 lowercase hex>
  executable_identity: <APFS/device/inode/ctime/mode tuple>
  repository_head: <40 lowercase Git commit hex>
  repository_tree: <40 lowercase Git tree hex>
  repository_clean: true
  implementation_blob: <40 lowercase Git blob hex>
  source_approval_sha256: "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6"
  target_volume_uuid: <canonical APFS UUID>
  supersedes_incomplete_attempt_sha256: <64 lowercase hex>
```

The transition receipt binds the new inventory, target volume UUID, the 4,715 leaf-object comparison result, the complete directory validation digest, and the journal's terminal chain hash. The anchor signs the inventory, receipt, new v2 acquisition contract, fixed RunSpec, implementation/design document bindings, final journal copy, and `execution_binding`.

The anchor additionally has the closed inline `supersedes_incomplete_attempt` object. It is the only old-attempt provenance object; no separate legacy artifact is created. It has exactly the following fields:

```text
old_custody_parent:
  /Users/taejunoh/Documents/MLB Custody
old_final_root:
  /Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4
old_owner_lock:
  /Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock
observer_state: "OBSERVER_INCOMPLETE_LOCKED"
metadata_inconsistent: true
final_root_identity: <read-only no-follow type/Volume UUID/device/inode/ctime_ns/mode/flags tuple>
owner_lock_identity: <read-only no-follow type/Volume UUID/device/inode/ctime_ns/mode/flags/size tuple>
observed_leaf_count: <integer>
observed_directory_count: <integer>
observed_content_closure_sha256: <64 lowercase hex>
```

The observed closure digest is an ordered, domain-separated SHA-256 over every no-follow reachable old root directory and regular leaf's relative path, type, size, SHA-256, mode, flags, and identity tuple. The fingerprint is read-only: it opens only no-follow descriptors, performs no create/write/chmod/chflags/rename/unlink/lock action, and does not read or hash old lock contents; `owner_lock_identity` is metadata only. It excludes any lock nonce and does not establish that the old attempt is valid or resumable. A missing/unreadable/inconsistent old root or lock does not weaken v2 validation; it records the applicable closed observation class and blocks issuance because this approved recovery specifically supersedes the observed locked incomplete attempt.

`HEAD` means the commit resolved by `git rev-parse --verify HEAD`, not an arbitrary user-provided string. Before creating the journal (and therefore before its first event), the issuer completes the accepted-parent preflight, read-only old-attempt fingerprint, executable binding, and checkout binding so that every journal record has one stable `execution_binding_sha256`. It must verify that `HEAD` is a full commit, the index and worktree are clean, the source files used by the issuer belong to the bound tree, and the approved design/implementation blobs equal their bound Git blob IDs. The Python executable is opened with no-follow semantics and bound by SHA-256 plus `(Volume UUID, device, inode, ctime_ns, regular type, mode, UID, nlink, size)`. A later mismatch aborts; `sys.executable`, `PATH`, `HOME`, environment overrides, or a matching filename are not authority.

### File Provider rejection

Before provisioning or writing the operational journal, preflight opens every ancestor from `/Users` through the proposed custody parent with no-follow directory descriptors. It requires all of the following:

- each component is a real directory, current process can bind it descriptor-to-name, and all target components are on one writable local APFS volume with one stable Volume UUID;
- for every ancestor and the parent volume, Darwin `NSURL` resource values are available and require `NSURLIsUbiquitousItemKey == false`, `NSURLVolumeIsLocalKey == true`, `NSURLVolumeIsInternalKey == true`, `NSURLVolumeIsRemovableKey == false`, and `NSURLVolumeIsReadOnlyKey == false`;
- no ancestor or target-parent descriptor has an extended-attribute name matching `com.apple.fileprovider.*`, `com.apple.file-provider-domain-id`, or `com.apple.icloud.*`; and
- every required Darwin filesystem/resource-property query succeeds and is mutually consistent.

APFS alone is not sufficient. The `NSURL` resource keys must be obtained from the descriptor-bound path and re-read after resolution; the extended-attribute namespace is checked with descriptor-relative `flistxattr`, never an unchecked pathname. Any iCloud/File Provider indication, matching xattr, cross-volume path, network/removable/unknown classification, symlink, unavailable detector/API, unsupported xattr enumeration, or contradictory result is `CUSTODY_FILE_PROVIDER_REJECTED` (or `CUSTODY_UNAVAILABLE` for unavailable identity) before a journal, lock, temporary root, repository artifact, or target directory is created. There is no `--allow-file-provider`, `--force`, or fallback classification.

The source authority is read-only and may be on a different volume; the source and target volumes are never equated. All target snapshot objects, locks, and target ancestors must instead bind to the one accepted target Volume UUID.

## Append-only Seed-free Journal

The journal is newline-delimited canonical JSON. It contains no Ed25519 seed, private key bytes, key material, secret environment values, bearer token, command line, or raw source payload. It never serializes a temporary-root nonce. Diagnostics may include the fixed generation, event sequence, stage, reason code, fixed logical artifact name, volume UUID, Git HEAD/tree, executable hash prefix, and expected/observed identity class.

The owner creates the journal exclusively only after the target-parent File Provider preflight succeeds and before lock acquisition. It is mode `0600` while active, opened through a retained no-follow descriptor, and each record is appended in one write, `fsync`ed, and descriptor/name rebound before the next critical operation. Records form a hash chain over canonical bytes; a writer never seeks, truncates, rewrites, rotates, or unlinks it. A restart cannot append to or adopt a prior journal. The last journal record is `PRE_ANCHOR_AUTHORITY_READY`; after it is fsynced, the operational journal is frozen `0444` plus `UF_IMMUTABLE` and no further record can be added. If failure occurs before this close, the journal remains present and unfrozen as forensic evidence, with the owner lock retained when acquired.

Every record has exactly these fields:

```json
{
  "journal_version": "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1",
  "custody_generation": "v5.2-r4-disjoint-custody-v2",
  "sequence": 1,
  "event": "BEFORE_LOCK_ACQUIRE",
  "outcome": "INTENT",
  "previous_event_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
  "execution_binding_sha256": "<sha256 of canonical execution_binding>",
  "target_volume_uuid": "<canonical APFS UUID>",
  "detail": {"class": "LOCK"},
  "event_sha256": "<sha256 of this record without event_sha256>"
}
```

`detail` is a closed event-specific object. Failure records replace `detail` with only `{"class": "<closed error class>", "code": "<closed error code>"}`. No timestamp is an authority input; a monotonic process-local elapsed value may be emitted only in non-authoritative human stderr, never in the canonical record.

Pre-anchor critical stages are journaled immediately before (`INTENT`) and after (`SUCCESS`) each action:

| Stage | Action |
| --- | --- |
| `PREFLIGHT_PARENT_ACCEPTED` | target ancestry/File Provider/APFS checks completed; this is the journal's first event |
| `LOCK_ACQUIRE` | create and authenticate the new owner lock |
| `SOURCE_CLOSURE_VALIDATE` | validate 1,675/4,711/5,025 source closure read-only |
| `TEMP_ROOT_CREATE` | create the owned new temporary sibling |
| `COPY_AND_CONTENT_VALIDATE` | copy and hash every member/lock into the temporary root |
| `MODE_FLAG_VALIDATE` | validate modes and flags for all leaves/directories |
| `SNAPSHOT_PUBLISH` | exclusive temporary-to-final rename in the new parent |
| `INVENTORY_PUBLISH` | publish the v2 inventory |
| `RECEIPT_PUBLISH` | publish the v2 transition receipt |
| `CONTRACT_PUBLISH_AND_AUTHENTICATE` | publish/authenticate the new v2 acquisition contract and all new-generation bindings |
| `PRE_ANCHOR_AUTHORITY_READY` | terminal success record: binds clean execution binding, snapshot manifest hash, inventory SHA-256, receipt SHA-256, and v2 contract SHA-256 immediately before journal freeze |

The closed journal is intentionally **pre-anchor only**. Once the journal copy is hashed by the anchor, recording `JOURNAL_COPY_PUBLISH`, `ANCHOR_PUBLISH`, `RELEASE_BUNDLE_REBIND`, or `OWNER_LOCK_RELEASE` in that journal would alter the copy's hash and create an anchor/journal cycle. The acyclic boundary is therefore mandatory: after `PRE_ANCHOR_AUTHORITY_READY`, freeze/copy the journal, publish the anchor/signature, rebind the immutable release bundle, then release the owner lock. Those post-close actions are evidenced by the immutable repository artifact identities, detached signature, final snapshot state, and absence of the authenticated owner lock—not by new journal records.

An exception or signal handler that still has a bound, open journal descriptor appends one `FAILURE` record before closing descriptors. A crash can leave only the preceding `INTENT`; that is an incomplete, non-adoptable state, never a successful step. Failure to append or fsync is itself `JOURNAL_UNAVAILABLE` and aborts before the corresponding pre-anchor action. No event is backfilled. A failure after journal close leaves the closed journal, owner lock, and any already-published immutable v2 artifacts in place; it is never executable, resumable, adopted, or automatically cleaned.

## Snapshot and Security Invariants

1. The old custody root and old lock are excluded from new issuance traversal, state classification, cleanup, and lock-conflict logic. Their existence neither blocks nor authorizes the new namespace. The sole exception is the bounded read-only no-follow fingerprint that populates the signed `supersedes_incomplete_attempt` object.
2. The new issuer accepts exactly the new custody parent, exact final name, exact generation, and exactly one source approval hash. Any other value fails closed before writes.
3. Every open of a target/source/artifact is no-follow and is rebound from parent descriptor to final descriptor before and after hashing, flag changes, publication, and lock release. Symlinks, special files, and hard links are forbidden.
4. The temporary and final snapshot must contain exactly the 4,715 approved leaf objects: 4,711 regular members plus the four named root locks. Each regular member has mode `0444`, `nlink == 1`, expected size/SHA-256, APFS Volume UUID, device, inode, ctime, and `UF_IMMUTABLE` after sealing. Each root lock is a zero-byte regular file, `nlink == 1`, mode `0444`, and `UF_IMMUTABLE` after sealing.
5. Every directory reachable from the final root, including the root, is enumerated descriptor-relatively; it must be mode `0555`, `UF_IMMUTABLE`, on the accepted target volume, and have the expected type/identity. The inventory stores an ordered directory manifest and its SHA-256, so validation covers the 4,715 leaves **and every directory**, not a best-effort recursive chmod.
6. The final root, its four locks, all 4,711 members, all contained directories, custody parent, and ancestors through the accepted local target anchor are checked on the same target APFS Volume UUID. A copied volume, remount identity mismatch, mode/flag drift, or name-to-descriptor rebind mismatch is an integrity failure.
7. The signed anchor is verified before trusting mutable inventory/receipt/contract content. `verify` rehashes/rebinds the whole final tree, validates the directory manifest and journal chain/copy, and requires all frozen modes/flags; it never uses the new inventory as its own source authority.
8. No process other than the lock creator can release, append to, continue, clean, freeze, or adopt the transaction. After snapshot publication, no automated deletion occurs, including of the new temporary residue, final root, lock, journal, or repository artifact. A post-journal-close failure retains the closed journal plus the lock and partial immutable artifacts and is never executable.

## Failure Handling and State Model

The recovery is deliberately one-shot per new namespace. There is no `resume`, `repair`, `adopt`, `unlock`, or `cleanup` operation.

| Observed new-namespace state | Classification | Required behavior |
| --- | --- | --- |
| parent fails File Provider/APFS checks | `CUSTODY_FILE_PROVIDER_REJECTED` / `CUSTODY_UNAVAILABLE` | no write anywhere under the proposed custody parent |
| journal absent after accepted parent | `ABSENT` | a fresh owner may start only if all new final/lock/temp/artifact paths are absent |
| journal exists, no lock/final | `OBSERVER_INCOMPLETE_UNOWNED` | stop; do not append, reuse, or delete it |
| lock exists, with any journal/temp/final state | `OBSERVER_INCOMPLETE_LOCKED` | stop; never release/adopt/clean |
| final root exists with lock or incomplete artifacts | `OBSERVER_INCOMPLETE_PUBLISHED` | stop; never authenticate as executable or repair |
| final root plus all six v2 repository outputs and valid frozen journal/anchor | `FINAL` | read-only verify only |
| any identity/hash/mode/flag/journal-chain mismatch | `CUSTODY_IDENTITY_MISMATCH` / `CUSTODY_SNAPSHOT_INVALID` | stop with no mutation |

If preflight fails before journal creation, the CLI returns the closed error and does not attempt to leave a log in the unsafe target. If it fails after journal creation but before `PRE_ANCHOR_AUTHORITY_READY`, it records failure only in the new journal if descriptor authentication remains possible; otherwise it stops with `JOURNAL_UNAVAILABLE`. If it fails after that terminal record, the closed journal and owner lock remain with any partial v2 artifacts. Neither outcome permits an automatic retry over existing residue. A human may perform a separately authorized forensic investigation later, but that investigation is out of scope and may not be hidden behind normal issuance CLI flags.

## CLI Contract Changes

The existing subcommands remain conceptually `provision-custody-parent`, `preflight`, `issue-and-bind`, and `verify`, but production R4-disjoint operation introduces an explicit closed mode:

```text
--custody-generation v5.2-r4-disjoint-custody-v2
--custody-parent /Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2
--final-name v5_1_bt_r4
```

`issue-and-bind` requires all three exact values; they are validated before any provision or journal action. It writes only the new v2 contract path, never the prior R4 contract path. `preflight` validates them read-only and reports the accepted target volume identity only after File Provider rejection succeeds. `provision-custody-parent` creates at most the one missing `custody-v2` directory after ancestor validation; it may not recursively create unknown parents. `verify` receives only the fixed v2 final root or derives it from the fixed v2 parent; it rejects old R4 root paths and requires the v2 contract, journal-copy, inline supersession summary, and anchor bindings.

The following are expressly absent: `--resume`, `--adopt`, `--recover`, `--unlock`, `--cleanup`, `--old-custody`, `--allow-file-provider`, a caller-configured journal path, a caller-configured generation, and a caller-configured final name. A command that sees old evidence logs no old path and makes no syscall that changes it.

## Test Matrix

| Area | Required acceptance cases |
| --- | --- |
| Old-evidence isolation | In a disposable fixture that substitutes the old path, create sentinel root/lock and run every v2 preflight/issue/verify failure path. Assert exact bytes, names, modes, flags, timestamps/identities, and directory entries remain unchanged; only the read-only no-follow fingerprint is allowed. Assert no old-path write, rename, chmod/chflags, unlink, lock release, nonce read, or recovery/adoption action. Tests never touch the real old path. |
| File Provider preflight | Simulate each `NSURL` rejection (`ubiquitous`, non-local, non-internal, removable, read-only), each forbidden xattr namespace, detector/xattr API absence, ancestor symlink, cross-volume ancestor, non-APFS, and contradictory metadata. Each fails before journal/lock/temp/final/repository output creation. Verify the approved Application Support fixture succeeds only as a local APFS non-provider root. |
| Authority generation | Reject old R4 inventory/receipt/contract/anchor schemas, old journal, omitted/wrong generation, wrong final name, caller-selected parent, stale HEAD, dirty index/worktree, changed executable hash/identity, wrong design/plan blob, source approval mismatch, and malformed/missing/inconsistent inline supersession fingerprint. |
| Journal | Assert canonical chained `INTENT`/`SUCCESS` records around every pre-anchor stage, a seed-free failure record, `fsync` before action, terminal `PRE_ANCHOR_AUTHORITY_READY` with snapshot/inventory/receipt/contract hashes, no append after close, no restart adoption, no truncation/rotation, invalid previous hash/sequence/event hash rejection, and anchor rejection when repository copy or terminal chain hash differs. Assert post-close anchor/rebind/lock-release state is proven by immutable artifacts and lock state, not an impossible extra journal record. |
| Full tree | Exercise exact 1,675/4,711/5,025 closure; reject one missing/extra leaf, member/lock hard link, leaf or directory symlink, hash/size/identity change, wrong volume UUID, wrong leaf `0444`, wrong directory `0555`, missing/wrong `UF_IMMUTABLE`, one lock mismatch, and one directory-manifest mismatch. Verify all 4,715 leaves and every directory are inspected. |
| Race/failure | Inject swaps before/after each journal write, lock creation, copy, seal, exclusive rename, v2 inventory/receipt/contract publication, terminal journal close, journal copy, anchor publication, bundle rebind, and lock unlink. Require an incomplete new state with no successful return; a post-close failure retains closed journal plus lock/partial immutable artifacts and is never executable. Never remove old or new forensic residue. |
| Final verification | Fresh-process verify succeeds only with valid signature, fixed source approval, v2 inventory/receipt/contract, valid signed inline supersession summary, complete directory/leaf validation, journal copy/chain, executable/HEAD binding, and all final modes/flags. A byte-identical copied volume fails by Volume UUID. |
| Compatibility | Existing R2/R3 and non-v2 R4 tests remain byte/behavior compatible. The exact suite, targeted R4 tests, Ruff, and `git diff --check` must pass before any real issuance authorization. |

Tests use disposable APFS fixtures and test signing authority only. They never create, inspect, clear flags on, or otherwise operate the real old custody path.

## Migration and Rollout

1. Implement and independently review the closed v2 branch without performing a real custody operation.
2. Run the complete test matrix, clean exact suite, static checks, and a manual code review that specifically verifies no old-path mutation capability exists.
3. On the production host, run v2 `preflight` against the exact Application Support parent. A rejection stops rollout; changing to another parent requires a new approved design rather than a CLI override.
4. Require a clean bound checkout, validated production signing authority, successful read-only old-attempt fingerprint, absent v2 final/lock/temp/journal/repository v2 artifacts, and a successful read-only source closure validation.
5. Perform `issue-and-bind` once. A failure leaves the new namespace as evidence and ends this rollout; do not retry, adopt, or clean it.
6. In a fresh process, run v2 `verify`. Only its successful immutable authority may be used by the source-only R4 workflow.

No migration step touches the old `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4` root or `.v5_1_bt_r4.issuance.lock`. They remain locked, incomplete forensic evidence outside the v2 authority graph indefinitely unless a separately approved forensic procedure explicitly changes that policy.
