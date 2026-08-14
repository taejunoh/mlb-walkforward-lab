# v5.2 R4 Physical Custody Authority Design

**Status:** Approved direction on 2026-08-04

**Scope:** Define one closed R4 source-only authority that succeeds the unavailable R3 physical custody root. The only changed claim is the physical custody identity of the protected v5.1 corpus. R2/R3 remain immutable; v5.2 model, data, query, and parser v5.2.3 semantics remain unchanged.

## Decision and Current Evidence

R3 correctly fails closed when supplied the available corpus: its protected inventory binds the original APFS `st_dev`/inode values, but that physical root is unavailable. R4 is a new authority, never an R3 repair, compatibility mode, or validation downgrade.

The observed preconditions are:

- the clean exact suite completed with `1458` tests, `skipped=2`, and return code `0`;
- the approved source inventory has `1,675` logical records, `5,025` record-role bindings, and `4,711` distinct physical artifact paths;
- all `4,711` physical artifact bytes and hashes, plus the complete `5,025`-binding closure, equal the R3 inventory's contents;
- every compared R3 device identity differs, and `522` compared inode identities differ; and
- the real R3 launch returned code `2` during authority authentication before any runtime write.

R4 creates one dedicated, immutable APFS snapshot root:

```text
/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4
```

The root is the R4 physical protected parent only. It is neither a target-local execution copy nor a replacement for the R3 parent. R4's versioned inventory attests the new snapshot's persistent physical identities, while a transition receipt proves its complete byte-equivalence to the approved corpus.

The immutable source approval is the canonical source of this split: `audit/v5_1_bt/protected_raw_inventory.json` has SHA-256 `2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6`. Its only repeated artifact paths are both `raw` bindings under `data/v5_1_bt/raw/mlb_stats_pitcher_bf`: `729bad325d9eafb22883c3318896f2baf877c99b5600fa77aba2bb9f6da6a6e7.raw` appears `85` times and `f8615992094a949312f8a27b4e129e9c14abd006b994818642110733e260d86f.raw` appears `231` times. Each repeated source reference has the same approved path, SHA-256, byte size, and original source identity. R4 preserves those logical references without creating aliases, hard links, renamed copies, or duplicate physical files.

## Exact R4 Authority Identity

R4 has these exact canonical authority paths and run identifier:

```text
run_id:
  historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4

RunSpec:
  protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json

acquisition contract:
  protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json

raw namespace:
  data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw

sources namespace:
  data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources

audit namespace:
  audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4

reports namespace:
  reports/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4
```

The R4 contract's `canonical_roots` are exactly:

```text
acquisition_audit:
  audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition
execution_audit:
  audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/execution
raw:
  data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/raw
sources:
  data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/sources
```

The R4 custody artifacts are in the R4 audit authority area, never under the R2/R3 or v5.1 audit paths:

```text
versioned protected inventory:
  audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json

content-transition receipt:
  audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json

inventory schema:
  schemas/v5_2_r4_protected_raw_inventory.schema.json

transition-receipt schema:
  schemas/v5_2_r4_physical_custody_transition_receipt.schema.json
```

The immutable source approval remains exactly:

```text
logical raw root: data/v5_1_bt/raw
approved protected inventory path: audit/v5_1_bt/protected_raw_inventory.json
approved protected inventory SHA-256:
  2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6
```

The existing v5.1 Git baseline remains `protocols/v5_2_qd/v51_protected_inventory.json` with SHA-256 `7dcc6a2e9c23f00353839b42e504715bc449d4ed86c8d27576a12abddf1ecde1`; R4 does not modify or reinterpret it.

## Semantic Preservation

R4 retains the approved R3 source meaning exactly:

- model version and v5.2 model semantics are unchanged;
- existing parser behavior is unchanged and the recovery schedule/missing-venue parser-policy version remains `v5.2.3`;
- the source years remain 2018--2024, with the same season boundaries, endpoint meanings, recovery dispositions, required years, and reserve policy;
- the four recovery records, three projection relationships, excluded game, parent evidence semantics, source requirements, and parser policy remain unchanged;
- no model, feature, schedule, venue, query, source coverage, network, or acquisition policy is altered.

R4 query objects necessarily carry the R4 RunSpec and R4 contract hashes. Therefore R4 query IDs, static-batch ID, checkpoints, source inventories, receipts, and terminal evidence are new R4 values despite identical query/data semantics. No R3 query or batch identifier is reused.

## Design and Binding Semantics

This approved design source is:

```text
docs/superpowers/specs/2026-08-04-v5-2-r4-physical-custody-authority-design.md
```

Its approved baseline before this protocol amendment is Git commit `39307d7619d52362090007e0b2182721d9f29c44`. The future R4 contract binds the final approved revision's actual file SHA-256 and Git blob ID; this document does not claim its own future commit or blob ID.

The implementation plan is a separate, committed future file. Before the R4 contract is generated, the plan must have a final relative path, Git blob ID, and working-tree SHA-256. The R4 contract uses the existing R3 closed binding shapes: `approved_design` has exactly `path`, `sha256`, and `git_blob_id`; `implementation_plan` has exactly `path`, `git_blob_id`, and `working_tree_sha256`; and `run_spec` has exactly `path`, `sha256`, and `git_blob_id`. Each hash is the actual lowercase fixed-length SHA-256 or Git blob ID of the already committed referenced artifact.

R4 keeps the active execution binding and the immutable source approval distinct. Its closed `legacy_protection` object retains the existing fields `baseline_inventory_path`, `baseline_inventory_sha256`, `legacy_root`, and `permitted_years` with their R3 values, but its active `protected_inventory_path` and `protected_inventory_sha256` are the exact R4 versioned inventory path and hash. It is the only inventory binding consumed by R4 legacy reads.

R4 adds one closed `physical_custody_transition` contract object with exactly these children:

```text
receipt:
  path = audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json
  sha256 = exact receipt SHA-256

source_approval:
  logical_raw_root = data/v5_1_bt/raw
  protected_inventory_path = audit/v5_1_bt/protected_raw_inventory.json
  protected_inventory_sha256 = 2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6

r4_inventory:
  path = audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json
  sha256 = exact R4 inventory SHA-256
```

The contract validator requires `physical_custody_transition.r4_inventory.path` and `.sha256` to equal the active `legacy_protection.protected_inventory_path` and `.protected_inventory_sha256`. The source-approval child is immutable provenance only; it is never accepted as an active R4 inventory and never changes R3's `legacy_protection` meaning.

The publication order is deliberately acyclic:

```text
approved design commit
  -> approved implementation-plan commit
  -> committed R4 RunSpec
  -> immutable physical snapshot
  -> versioned R4 inventory
  -> transition receipt (binds RunSpec + inventory, never contract SHA)
  -> R4 contract (binds design + plan + RunSpec + inventory + receipt)
  -> R4 query/batch authority and execution artifacts
```

The transition receipt contains the R4 run ID and RunSpec SHA-256, source inventory path/SHA, R4 inventory path/SHA, the separate physical-member and logical-binding counts, physical-content comparison result, canonical logical-binding comparison result, custody identity manifest hash, and receipt schema version. It never contains the R4 contract SHA. The contract then binds the receipt SHA. Query and batch objects are generated only after the contract exists and bind its SHA. Thus no receipt, contract, or query hash depends on itself.

## Snapshot Filesystem Protocol

The custody parent, final root, lock, and temporary naming are exact:

```text
custody parent:
  /Users/taejunoh/Documents/MLB Custody

final root:
  /Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4

issuance lock:
  /Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock

temporary root pattern:
  /Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.<32-lowercase-hex>.tmp
```

The issuance lock is created by the issuing process with no-follow `O_CREAT|O_EXCL`, mode `0600`, and held by its descriptor for the whole snapshot issuance. The process records the lock descriptor identity and its 16-byte issuance nonce in memory. A process is the live owner only while it holds that original descriptor, its descriptor identity still equals the no-follow lock-path identity, and its in-memory nonce equals the temporary-root suffix. The same unbroken live-owner process and capability alone may perform snapshot publication, immutable revalidation, inventory publication, receipt publication, final-path authentication of the RunSpec and contract, and lock release. An existing lock is fail-closed for every non-owner; no later process removes, truncates, reuses, or changes it automatically.

The temporary root suffix is generated from a cryptographically secure 16-byte nonce encoded as exactly 32 lowercase hexadecimal characters. The temporary directory is created with `0700`, owned by the issuing process, and must be a sibling of the final root on the same APFS volume. The final root must be absent before lock acquisition and again immediately before rename. An existing final root is never merged, replaced, or repaired.

On normal issuance success, only after that same live owner has re-read and authenticated the final root, R4 inventory, transition receipt, R4 RunSpec, and exact R4 contract no-follow from their final paths, it verifies the original lock identity, unlinks only `.v5_1_bt_r4.issuance.lock`, `fsync`s the custody-parent directory descriptor, closes the unlinked lock descriptor, then closes the parent descriptor. `EXECUTABLE`, `RUNNING`, and `FINAL` require that neither the lock nor a matching temporary root exists. Before the snapshot rename, a live owner that aborts may descriptor-delete only its own temporary root, `fsync` the custody-parent directory, unlink its own verified lock, `fsync` the custody-parent directory again, then close its lock and parent descriptors. After rename, a live owner never removes the final root, any inventory/receipt/contract, or the lock as an abort cleanup; a crash preserves the lock and produces an incomplete state. A restarted process, even if it can authenticate already-published artifacts, is an observer rather than the original owner and must not adopt, continue, or release that issuance.

Construction proceeds only through descriptor-relative no-follow traversal:

1. Open and authenticate the approved source root; derive its `1,675` logical records, `5,025` record-role bindings, and `4,711` unique logical member paths from the source inventory; derive each physical traversal suffix only by stripping `logical_raw_root` under the exact mapping below; reject a symlink, special file, missing/extra member, conflicting repeated reference, non-regular object, or object link count other than one.
2. Populate only the owned temporary root. For each of the `4,711` unique physical members reached by that derived suffix, stream from an authenticated source descriptor while calculating SHA-256, write the destination object once without path-following, then reopen and hash it through a destination descriptor. Require equal source/destination SHA-256 and size. Separately prove the complete `5,025` logical binding closure against the source inventory; a shared logical path is a repeated logical reference to that one physical member, never a second copy.
3. Require the exact `1,675` records, `4,711` unique physical members, `5,025` logical bindings, and these exact root locks: `.baseball_savant_statcast.lock`, `.mlb_schedule.lock`, `.mlb_stats_pitcher_bf.lock`, and `.mlb_stats_pitching.lock`. Each lock must be a regular single-link zero-byte file. The source/snapshot non-lock physical leaf set is exactly the set of `members.path` values after the required logical-prefix strip; the complete physical leaf set is exactly that suffix set union the four separate `root_locks` paths.
4. Set every regular snapshot file, including the four locks, to `0444`; set every snapshot directory to `0555`; `fsync` every written file and directory as required for the platform; and verify the complete custody manifest through no-follow descriptors.
5. Atomically publish the owned temporary root once to the absent final root with Darwin `renameatx_np(old_parent_fd, old_name, new_parent_fd, new_name, RENAME_EXCL)`, using authenticated parent descriptors. The call must fail if the final name exists; a check-then-plain-rename sequence is forbidden. If that platform primitive is unavailable, only an audited descriptor-based no-replace primitive with equivalent atomic exclusion is permitted; otherwise issuance fails closed. The rename is the only physical-custody publication operation, but the root is not yet an accepted R4 authority.
6. While the issuance lock is still held, reopen the final root no-follow, rehash every one of the `4,711` physical members, revalidate the complete `5,025` logical-binding closure, then set and verify `UF_IMMUTABLE` on every snapshot file and directory through no-follow metadata reads, setting the final root directory last. Failure to set or verify mode or `UF_IMMUTABLE` leaves an incomplete snapshot, never an accepted authority. Only after this revalidation may the process write the versioned inventory and transition receipt under the R4 audit authority area before generating the R4 contract.

The versioned inventory, transition receipt, and R4 contract are individually canonical JSON documents rendered as `canonical_json_bytes(document) + b"\\n"`. Each is written through an owned same-directory temporary file, `fsync`ed, re-read and schema-validated no-follow, and published to its exact canonical path with Darwin `renameatx_np(old_parent_fd, old_name, new_parent_fd, new_name, RENAME_EXCL)` using authenticated parent descriptors. An unavailable `RENAME_EXCL` requires the same audited descriptor-based atomic no-replace equivalent as the physical snapshot; otherwise publication fails closed. Existing canonical paths are conflicts, never overwrite targets. The contract is generated only after both exact documents have been re-read from their final paths and their SHA-256 values match the intended contract fields.

The source corpus is read-only. Snapshot issuance is the sole approved physical copy. `--plan-only`, acquisition, resume, source finalization, review, replay, and advance never create, edit, chmod, flag, relink, or repair the snapshot.

## Stable R4 Custody Identity

R4 does not persist `st_dev`. It persists a descriptor-authenticated APFS identity for every unique physical member and each of the four separate root locks:

```text
volume UUID, inode, ctime_ns, file type, st_nlink, byte size, SHA-256
```

The inventory also persists a root/ancestor identity chain for the snapshot root and every directory traversed to reach a physical member. Each chain entry contains canonical relative path, APFS Volume UUID, inode, `ctime_ns`, directory type, and link count. The chain is revalidated before descriptor reads and at every publication rebind; a root, ancestor, or member swap therefore fails even when the replacement has matching bytes.

Volume UUID acquisition is platform-specific and fail-closed:

1. Open the snapshot root as a directory descriptor using no-follow flags and verify it is a directory.
2. Call the trusted macOS system API `fgetattrlist` (the descriptor form of `getattrlist`) directly through the audited Darwin binding, requesting `ATTR_VOL_FSTYPE` and `ATTR_VOL_UUID`. The implementation must call the system API directly, not execute a shell command, resolve a command through `PATH`, parse `diskutil`, or derive identity from a mount path.
3. Require the reported filesystem type to be APFS and the returned UUID to be exactly 16 bytes. Canonicalize those bytes as a 32-character lowercase hexadecimal `volume_uuid` string. Human-only diagnostics may render the same 16 bytes as conventional lowercase hyphenated UUID text; canonical evidence persists only the 32-character form.
4. Obtain and compare the same Volume UUID through authenticated descriptors for all `4,711` physical members, all four root locks, and every ancestor. Any unavailable API, non-APFS filesystem, malformed UUID, cross-volume member, or cross-volume root lock is `CUSTODY_UNAVAILABLE`.

`ctime_ns` supplements inode identity and is never treated as a substitute for SHA-256. A remount or reboot that changes only transient `st_dev` succeeds. A byte-identical volume copy fails because its APFS Volume UUID differs, even if inode values collide.

## Closed Logical Custody Schemas

The two future schema files named above are closed JSON schemas. Every object at every level has `additionalProperties: false`; every array has its stated exact length or a nonzero bounded length where noted; every SHA-256 field is exactly 64 lowercase hexadecimal characters; every `volume_uuid` is exactly 32 lowercase hexadecimal characters; every `inode`, `ctime_ns`, `byte_size`, and `mode` is a non-boolean integer; and every persisted path is canonical POSIX text with no empty, `..`, absolute, or backslash component. Member, binding, and source-comparison paths follow the full logical-path rule below; root locks and ancestor identities follow their explicitly separate snapshot-root-relative rules. The sole `.` path exception is the required first `ancestor_identities` entry. Neither document contains a wall-clock issuance timestamp. `ctime_ns` is an observed filesystem identity field, not an issuance timestamp.

### `v5_2_r4_protected_raw_inventory`

`schemas/v5_2_r4_protected_raw_inventory.schema.json` accepts exactly one top-level object with these required keys and no others:

```text
schema_version
inventory_kind
logical_raw_root
source_approval
snapshot
manifest_count
physical_member_count
logical_binding_count
rawstore_verification
records
members
bindings
root_locks
ancestor_identities
custody_identity_manifest_sha256
```

Their exact meanings and constraints are:

- `schema_version` is the integer `1`.
- `inventory_kind` is the string `v5_2_r4_physical_custody`.
- `logical_raw_root` is the string `data/v5_1_bt/raw`.
- `source_approval` is a closed object with exactly `logical_raw_root`, `protected_inventory_path`, and `protected_inventory_sha256`, fixed to the immutable source values in **Exact R4 Authority Identity**.
- `snapshot` is a closed object with exactly `volume_uuid`, `root_identity`, `manifest_count`, `physical_member_count`, `logical_binding_count`, and `root_locks_sha256`. Its UUID equals every persisted member, root-lock, and ancestor UUID; its counts are exactly `1675`, `4711`, and `5025`; and `root_locks_sha256` is the SHA-256 of canonical JSON for the ordered `root_locks` array.
- `manifest_count` is the integer `1675`; `physical_member_count` is the integer `4711`; `logical_binding_count` is the integer `5025`; and `rawstore_verification` is the string `PASS`.
- `custody_identity_manifest_sha256` is the SHA-256 of canonical JSON for the ordered identity-bearing and logical-binding fields `snapshot`, `records`, `members`, `bindings`, `root_locks`, and `ancestor_identities`.

### Canonical logical-to-physical path mapping

Every `members.path`, `bindings.path`, receipt `comparison.objects[].path`, and source-derived logical-binding/content-manifest path is persisted exactly as the approved source inventory's full logical path under `logical_raw_root`, for example `data/v5_1_bt/raw/mlb_stats_pitcher_bf/<name>.raw`. It must be a canonical strict descendant of `PurePosixPath(logical_raw_root)`: the prefix itself, an absolute path, an empty suffix, `..`, backslash, or an additional nested logical-root prefix is invalid.

Physical descriptor traversal derives, but never serializes as an additional field, `physical_relative_path = logical_path.relative_to(PurePosixPath(logical_raw_root))`. The suffix must be nonempty and canonical, contain exactly the bound record's source directory and one leaf name, and its first component must equal that record's `source`. The snapshot's non-lock physical leaves are compared only with that derived suffix set; no member, binding, or receipt row may instead persist a suffix path. A collision after prefix stripping fails closed only when two **distinct** full logical paths resolve to one suffix. Repeated bindings of one identical approved full logical raw path, including the approved `85`- and `231`-reference paths, intentionally resolve to its one member and one suffix.

`root_locks` are the four bare snapshot-root-relative names listed below. They are outside `members` and `bindings`, are never prefixed with `logical_raw_root`, and may not collide with a derived member suffix. `ancestor_identities.path` remains a snapshot-root-relative directory path needed for descriptor traversal, with `.` only for the snapshot root; it is not a separately persisted physical-relative member path.

An object identity is a closed object with exactly these keys:

```text
volume_uuid
inode
ctime_ns
file_type
nlink
byte_size
sha256
mode
```

Every physical `member` is a closed object with exactly `path` plus the eight identity keys. It is a regular snapshot file with `nlink` `1`, `mode` `292` (`0444`), `inode` at least `1`, `ctime_ns` at least `0`, `byte_size` at least `0`, and descriptor-read `sha256`. `root_identity` is closed and has exactly `volume_uuid`, `inode`, `ctime_ns`, `file_type`, `nlink`, and `mode`, with `file_type` `directory`, `nlink` at least `1`, and `mode` `365` (`0555`); it deliberately has no byte size or SHA-256.

`records` is an array of exactly `1675` closed logical-metadata objects, strictly increasing by their source-inventory manifest ordering. Each record has exactly:

```text
source
canonical_query
rawstore_verified
```

`source` and `canonical_query` form one source-discriminated closed schema branch. `canonical_query` is copied from the immutable source inventory exactly as its source document represents it: no key is renamed, normalized, synthesized, or dropped. The JSON Schema uses a closed `oneOf` branch for the complete record, with `additionalProperties: false` in every branch and the exact query key/type mapping below:

| `source` | Exact `canonical_query` keys and types | Approved record count |
|---|---|---:|
| `baseball_savant_statcast` | `start`, `end`, `game_type`, `url`: strings | 213 |
| `mlb_schedule` | `start`, `end`, `gameType`, `role`, `url`: strings | 5 |
| `mlb_stats_pitcher_bf` | `startDate`, `endDate`, `gameType`, `group`, `stats`, `url`: strings; `pitcher_id`, `sportIds`: non-boolean integers | 731 |
| `mlb_stats_pitching` | `startDate`, `endDate`, `gameType`, `group`, `playerPool`, `stats`, `url`: strings; `limit`, `sportIds`: non-boolean integers | 726 |

The builder and semantic validator require the `1,675` R4 records, in source-inventory manifest order, to preserve the source record's `source`, complete `canonical_query`, and `rawstore_verified` value exactly; the approved source branch counts above are a derived validation assertion, not an alternate source policy. `rawstore_verified` is the boolean `true`. A record never serializes a physical artifact identity: its three artifact resolutions are exclusively `bindings` rows joined to `members` by canonical full logical `path`. The logical-binding manifest deliberately covers only `(record_index, role, path, byte_size, sha256)`; the separate custody identity manifest includes the complete `records` array, so any query-key, type, or value mutation is detected even though it is not a binding-manifest input.

`members` is an array of exactly `4711` closed physical artifact identities, strictly increasing by unique full logical `path`. Every member is referenced by at least one binding, its logical path is under `logical_raw_root` and the source named by every binding that references it, and the source/snapshot non-lock physical leaf set is exactly the derived-suffix set of `members.path`. The complete physical leaf set is exactly that suffix set union the four separate `root_locks` paths. No alias, hard link, renamed duplicate, extra non-lock leaf, path-prefix collision, or unreferenced member is permitted.

`bindings` is an array of exactly `5025` closed objects, ordered first by increasing `record_index` and then by the fixed role order `manifest`, `raw`, `seal`. Each binding has exactly `record_index`, `role`, and full logical `path`; `record_index` is an integer from `0` through `1674`, `role` is exactly one of `manifest`, `raw`, or `seal`, and `path` is exactly one member path. There is exactly one binding for every `(record_index, role)`, so every record resolves exactly three distinct role paths. A `manifest` or `seal` path is unique across bindings; only a `raw` path may be shared. A shared `raw` path must resolve to the one same member identity for every reference and must equal the repeated reference relationship derived from the immutable source inventory. That generic source-derived closure, not a hard-coded duplicate count, is the validator authority; for this approved source it admits only the two paths stated in **Decision and Current Evidence**, with `85` and `231` references.

`root_locks` is an array of exactly four closed artifact objects, strictly increasing by bare snapshot-root-relative `path`, whose paths are exactly:

```text
.baseball_savant_statcast.lock
.mlb_schedule.lock
.mlb_stats_pitcher_bf.lock
.mlb_stats_pitching.lock
```

Each root-lock identity has `file_type` `regular`, `nlink` `1`, `byte_size` `0`, `sha256` `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`, and `mode` `292` (`0444`). Its persisted `volume_uuid` equals `snapshot.volume_uuid`; schema and every descriptor rebind reject a root lock on any other volume.

`ancestor_identities` is a nonempty array of closed directory identity objects, ordered first by path depth and then lexicographically. It contains `.` first and then every unique snapshot-root-relative directory that is an ancestor of a derived member suffix; every entry has exactly `path`, `volume_uuid`, `inode`, `ctime_ns`, `file_type`, `nlink`, and `mode`, with `file_type` `directory`, `nlink` at least `1`, and `mode` `365` (`0555`). Paths are unique, every parent directory required by each derived member suffix is present, and no non-ancestor directory is present. The `.` entry's identity fields equal `snapshot.root_identity`; its additional `path` field is exactly `.`.

### `v5_2_r4_physical_custody_transition_receipt`

`schemas/v5_2_r4_physical_custody_transition_receipt.schema.json` accepts exactly one top-level object with these required keys and no others:

```text
schema_version
receipt_kind
run_id
run_spec
source_approval
r4_inventory
comparison
root_lock_comparisons
custody_identity_manifest_sha256
```

- `schema_version` is the integer `1`.
- `receipt_kind` is the string `R4_PHYSICAL_CUSTODY_TRANSITION`.
- `run_id` is exactly `historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4`.
- `run_spec` is a closed object with exactly `path` and `sha256`; `path` is the exact R4 RunSpec path and `sha256` is its actual committed-file SHA-256.
- `source_approval` is the same closed immutable source-approval object required by the R4 inventory.
- `r4_inventory` is a closed object with exactly `path` and `sha256`, fixed to the exact R4 inventory path and its actual SHA-256.
- `custody_identity_manifest_sha256` equals the same-named field in the authenticated R4 inventory.

`comparison` is a closed object with exactly `record_count`, `physical_member_count`, `logical_binding_count`, `source_content_manifest_sha256`, `snapshot_content_manifest_sha256`, `objects`, and `binding_comparison`. The counts are exactly `1675`, `4711`, and `5025`. `objects` is an array of exactly `4711` closed objects, strictly increasing by unique full logical `path`, each with exactly `path`, `byte_size`, `source_sha256`, and `snapshot_sha256`. Each path equals one R4 inventory member path; size and snapshot hash equal that member's values; source and snapshot hash must be equal for every physical member; and the two content-manifest hashes are SHA-256 values of the same ordered unique full-logical-path `(path, byte_size, sha256)` sequence.

`binding_comparison` is a closed object with exactly `source_logical_binding_manifest_sha256` and `snapshot_logical_binding_manifest_sha256`. Those hashes are the SHA-256 values of the canonical ordered `5025`-row `(record_index, role, full_logical_path, byte_size, sha256)` logical-binding sequence derived respectively from the immutable source inventory and the R4 `records`/`bindings`/`members` join. They must be equal. This proves the complete record-role closure, including valid shared raw references, without falsely treating repeated references as separate physical objects.

`root_lock_comparisons` is an array of exactly four closed objects, strictly increasing by `path`, each with exactly `path`, `byte_size`, `source_sha256`, and `snapshot_sha256`. Its paths are the exact four root-lock paths above, its size is `0`, and both hashes are the empty-file SHA-256. It proves lock-content equality without claiming that source and snapshot lock inodes are the same.

The receipt contains no contract SHA-256, inventory object identity record, absolute physical path, lock nonce, host name, or timestamp. Contract validation additionally requires the receipt's source approval and R4 inventory bindings to equal the corresponding `physical_custody_transition` fields, and requires its `run_spec` binding to equal the contract `bindings.run_spec` path/SHA. These equality checks are semantic validator requirements in addition to the closed schemas.

## Explicit Issuance State Machine

Issuance state is derived from no-follow observations of the exact lock, matching temporary roots, final root, R4 inventory, receipt, contract, checkpoints, and terminal evidence. There is no mutable repair journal. Classification has this strict precedence:

1. A caller with the live-owner capability described in **Snapshot Filesystem Protocol** receives an `OWNER_*` state while its verified lock exists.
2. Every caller without that capability classifies any existing lock or matching temporary root as `OBSERVER_INCOMPLETE_LOCKED`, regardless of which later artifacts it can see.
3. Only when both lock and matching temporary roots are absent does an observer classify `FINAL`, `RUNNING`, `EXECUTABLE`, `OBSERVER_INCOMPLETE_UNLOCKED`, or `ABSENT`, in that order.

The resulting rows are mutually exclusive:

| State | Required capability and exact condition | Permitted transition | Crash/retry behavior |
|---|---|---|---|
| `OWNER_LOCK_ACQUIRED` | live owner; verified lock exists; matching temp and final root are both absent | create the matching temporary root, or owner-abort before temp creation | owner-abort verifies and unlinks only its lock, `fsync`s the custody parent, then closes the lock and parent descriptors; a crash becomes `OBSERVER_INCOMPLETE_LOCKED` |
| `OWNER_PREPARING` | live owner; verified lock and its matching temp exist; final root absent | populate temp, or owner-abort before rename | owner may clean only its temp and lock in the stated ordering |
| `OWNER_SNAPSHOT_PUBLISHED` | live owner; verified lock; matching temp absent; final root exists; valid R4 inventory absent | finish immutable-flag revalidation and publish inventory | owner may continue; a crash becomes `OBSERVER_INCOMPLETE_LOCKED` |
| `OWNER_INVENTORY_PUBLISHED` | live owner; verified lock; matching temp absent; final root and valid inventory exist; valid receipt absent | publish receipt | owner may continue; a crash becomes `OBSERVER_INCOMPLETE_LOCKED` |
| `OWNER_RECEIPT_PUBLISHED` | live owner; verified lock; matching temp absent; final root, valid inventory, and valid receipt exist; exact contract absent | generate/authenticate contract | owner may continue; a crash becomes `OBSERVER_INCOMPLETE_LOCKED` |
| `OWNER_CONTRACT_AUTHENTICATED` | live owner; verified lock; matching temp absent; final root, inventory, receipt, RunSpec, and exact contract all authenticate | unlink, `fsync`, and close lock in normal-success order | owner may finish; a crash becomes `OBSERVER_INCOMPLETE_LOCKED` |
| `OBSERVER_INCOMPLETE_LOCKED` | caller is not the live owner and a lock or matching temp exists | none automatically | fail `AUTHORITY_SNAPSHOT_INCOMPLETE`; never remove, adopt, or continue it |
| `FINAL` | no lock/temp; exact terminal R4 evidence and live custody rebind pass | read-only idempotent return | return `ALREADY_FINALIZED_EXACT` without writes |
| `RUNNING` | no lock/temp; exact R4 authority and valid checkpoint chain exist; no terminal evidence exists | resume after fresh rebind | custody/authority mismatch fails closed |
| `EXECUTABLE` | no lock/temp; final root, inventory, receipt, exact RunSpec/contract, and all cross-bindings authenticate; no checkpoint or terminal evidence exists | mandatory real plan-only | plan-only must pass before execution |
| `OBSERVER_INCOMPLETE_UNLOCKED` | no lock/temp; a final root or any R4 inventory/receipt/contract exists, but no exact `EXECUTABLE`, `RUNNING`, or `FINAL` condition holds | none automatically | fail `AUTHORITY_SNAPSHOT_INCOMPLETE` or `FINAL_STATE_CONFLICT`; do not repair |
| `ABSENT` | no lock/temp, final root absent, and R4 inventory/receipt/contract all absent | acquire issuance lock | a new issuance may start |

The normal issuance lock release occurs only from `OWNER_CONTRACT_AUTHENTICATED`; release makes the next observer state `EXECUTABLE`. No owner state is visible to a non-owner as an executable partial authority. After a process crash, no process automatically removes an old lock/temp, adopts a final root, fills a missing inventory/receipt/contract, or overwrites an issuance artifact. All incomplete states require explicit human disposition and a separately authorized issuance action; normal R4 execution never performs that action.

## Fail-Closed Execution and Finalization

| State | Condition | Required behavior |
|---|---|---|
| `CUSTODY_UNAVAILABLE` | APFS identity cannot be obtained or a required no-follow descriptor cannot be opened | stop; never infer from `st_dev` or a path string |
| `CUSTODY_SNAPSHOT_INVALID` | inventory, receipt, mode/flag, schema, count, closure, or binding is malformed | stop; do not regenerate or edit it |
| `CUSTODY_IDENTITY_MISMATCH` | Volume UUID, inode, ctime, type, link count, size, SHA, lock, root, or ancestor differs | stop; byte equality is not a substitute |
| `CUSTODY_CHANGED` | a registry-locked rebind differs after initial authentication | abort that publication with no visible partial checkpoint, receipt, registry state, or terminal marker |
| `AUTHORITY_SNAPSHOT_INCOMPLETE` | the issuance state is incomplete or artifacts have an invalid combination | do not auto-adopt, overwrite, or repair |
| `FINAL_STATE_CONFLICT` | an existing final path is not the exact R4 authority/receipt/inventory/checkpoint chain plus live rebind | do not overwrite, reclassify, or synthesize completion |
| `ALREADY_FINALIZED_EXACT` | terminal R4 evidence and fresh live rebind are exact | return idempotent success without writes |

The R4 capability rebinds inside the existing registry publication lock before checkpoint, receipt, registry, and terminal publication, and again before final terminal return. A custody failure is an authority/integrity error, not a retryable network error.

Resume is permitted only from `RUNNING` with the exact R4 RunSpec, contract, transition receipt, inventory, custody manifest, snapshot identity, and checkpoint chain. A crash after the complete `EXECUTABLE` authority exists but before or during acquisition may resume only after a fresh full rebind. Any Volume UUID, inode, ctime, type, link, size, hash, lock, root, or ancestor change blocks resume.

## Diagnostics Privacy

Canonical inventory, receipt, contract, RunSpec, checkpoint, and terminal evidence must not serialize the current working directory, absolute custody path, external source-root path, user name, temporary-root nonce, lock path, mount path, or other host topology. Human diagnostics may emit a canonical relative object path, record kind, error state, expected/observed identity class, and hash prefix. They must not expose a portable external filesystem locator as authority evidence.

## TDD Acceptance Criteria

Implementation is test-first and may not broaden the authority model beyond this R4 branch.

### Snapshot and transition

- A valid issuance creates the exact `1,675`-record/`4,711`-physical-member/`5,025`-logical-binding snapshot, inventory, and receipt. Every physical member is fully descriptor-hashed at source and destination exactly once; the complete logical-binding closure is proved separately.
- The R4 inventory round-trips every source-inventory logical record in manifest order: all `1,675` `source` values, complete source-discriminated `canonical_query` objects, and `rawstore_verified` values are exactly preserved. Tests assert the approved `213/5/731/726` source-branch counts and reject a missing, extra, renamed, cross-branch, or wrong-typed query key (including `gameType` versus `game_type` and a string in either integer field).
- Tests round-trip every member, binding, and receipt-comparison path as its exact approved full logical source path; descriptor traversal derives the one nonempty suffix by stripping `data/v5_1_bt/raw` without serializing that suffix. Repeated bindings of either approved shared raw full logical path must map to its one member/suffix and pass. A path equal to or outside that root, a nested duplicate root prefix, a suffix collision between two distinct full logical paths, a member suffix colliding with a root-lock name, a persisted suffix in a member/binding/receipt row, or an extra snapshot non-lock leaf each fails closed.
- The four named root locks are present, zero-byte, regular, single-link, `0444`, and `UF_IMMUTABLE`; every other file is `0444` plus `UF_IMMUTABLE`; every directory is `0555` plus `UF_IMMUTABLE`.
- Symlink, special-file, hard-link, physical alias, renamed duplicate, missing/extra member, unreferenced member, conflicting repeated binding, one-byte mutation, destination swap, mode/flag failure, and non-APFS root each fail before R4 authority publication.
- A competing creator that publishes the final snapshot name, R4 inventory name, receipt name, or contract name after the losing issuer's absence check causes that issuer's `RENAME_EXCL` publication to fail; the winner is never overwritten and the loser does not retry with a plain rename.
- A crash before rename leaves no accepted final root. Crashes after rename, inventory, or receipt publication produce the exact incomplete state and are never automatically adopted or repaired.
- Fault injection after lock acquisition must yield `OWNER_LOCK_ACQUIRED` for the live owner and `OBSERVER_INCOMPLETE_LOCKED` after a crash; injection after temporary-root creation, each physical-root rename/immutability boundary, inventory publication, receipt publication, and contract publication yields the corresponding state-machine row. No retry automatically removes, adopts, completes, or overwrites the interrupted artifact.

### Persistent identity and races

- An R4 plan-only run authenticates all `4,711` physical member identities, all four root-lock identities on the same snapshot APFS volume, all required ancestry, and the complete `5,025` logical-binding closure before any network or runtime write.
- A remount/reboot simulation that changes only `st_dev` while preserving APFS Volume UUID and all persisted R4 fields succeeds.
- A byte-identical volume copy fails on Volume UUID even if inode values collide. A cross-volume or mismatched-UUID root lock fails with `CUSTODY_UNAVAILABLE` before authority publication, plan-only network access, or runtime writes.
- Inode/ctime replacement, content mutation, type or link-count change, root-lock mutation, and ancestor/root swap each fail closed.
- Mutation injected between initial validation and the registry-locked checkpoint, receipt, registry, or terminal publish returns `CUSTODY_CHANGED` and leaves no visible partial publication.

### Plan-only, resume, and terminal state

- A mandatory real `v52_acquire --plan-only` against the actual R4 snapshot succeeds before any acquire/resume/finalize/review/replay/advance command is authorized.
- Successful and failing plan-only runs leave the custody root, R4 inventory/receipt/contract, R4 runtime namespaces, locks, checkpoints, and temporary authority artifacts unchanged.
- Resume accepts only an exact `RUNNING` R4 state and fresh rebind. Every custody mismatch fails without copying, relinking, chmodding, flag changes, inventory regeneration, receipt replacement, or contract repair.
- Exact terminal R4 evidence is read-only idempotent. Partial/mismatched R4 final evidence and every R2/R3 final path are `FINAL_STATE_CONFLICT` for R4 and remain unchanged.
- The complete R4 source seal has exactly `81` snapshots, `42` source-season pairs, `0` pending source items, and `0` quarantine items before atomic `SOURCES_READY`.

### Preservation

- R2/R3 RunSpecs, contracts, schemas, protected inventories, query/batch IDs, receipts, namespaces, and CLI/API behavior remain byte-for-byte unchanged.
- R3 retains its existing `st_dev`/inode policy; R4's APFS Volume UUID, ctime, and immutable-flag rules are not backported.
- The clean exact suite passes with its expected skips, and protected parent/snapshot manifests are unchanged after verification.

## Rejected Alternatives and Out of Scope

- **Reissue R3 in place:** rejected because it rewrites R3's approved authority.
- **Byte-only or `st_dev`-only validation:** rejected because it loses custody continuity or rejects a valid remount/reboot while failing to distinguish a copied APFS volume.
- **Copy, repair, rebind, physical alias, hard link, or renamed duplicate during plan-only/acquisition/resume:** rejected because retries would alter the custody evidence they must authenticate and aliases would change the canonical physical leaf set.
- **General reissue/rebind framework or arbitrary `rN` authority parsing:** rejected; R4 is one exact closed branch.
- **Parser/model/feature/data/query-semantic change:** out of scope; parser v5.2.3 and all approved source semantics remain unchanged.
- **Broad R2/R3 refactoring:** out of scope; shared code may add only the exact R4 branch necessary for R4 authentication and sealing without changing historical serialized behavior.

## Completion Boundary

R4 is complete only when the immutable snapshot, versioned inventory, and transition receipt are authenticated; the real mandatory plan-only gate passes; execution/finalization reaches `81/42/0` with zero quarantine; all issuance-crash, rebind-race, resume, remount, volume-copy, and final-conflict tests pass; R2/R3 preservation remains exact; and the final exact suite reports no failures or errors.
