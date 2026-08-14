# PR #1 Merge-Blocker Hardening Design

**Date:** 2026-07-21

**Status:** Approved for implementation planning

**PR:** `master <- feature/mlb-v5-1-backtest` (`#1`)
**Reviewed range:** `5110d20389326015e9eb4ddc2c4aaf4c23227d2e..2e0764b530ff8ca2b978c9990b5eff5145c36bae`

## 1. Purpose

PR #1 remains a draft until three merge blockers are closed:

1. the execution registry can currently validate a weak projection of a source
   inventory without independently authenticating the canonical snapshot store;
2. path-based root opening in the execution registry, timezone-review writer,
   and snapshot-store adapters leaves an ancestor-directory replacement window;
3. the approved source-finalization design and plan still describe raw receipt
   paths, while the hardened implementation requires separate immutable
   `SOURCE_RECEIPT_COMMIT` markers.

This design closes those gaps without performing canonical historical
acquisition, source publication, review, replay, or phase transition.

## 2. Decisions

The approved approach is a shared root-FD capability, an independent registry
snapshot-store gate, and documentation of the existing commit-marker protocol.

Rejected alternatives are:

- adding only more `lstat`, `O_NOFOLLOW`, or path rechecks independently in
  each module, because those checks do not preserve ancestor identity across an
  operation;
- converting every public API to FD-only in this change, because that would be
  a broad breaking migration unrelated to closing PR #1;
- removing the commit-marker layer, because it would discard an implemented
  control-plane binding that prevents raw append-only receipts from becoming
  selected phase evidence without a separate immutable commit artifact.

## 3. Security invariants

After this change:

1. a trust-critical operation holds an FD chain from `/` through every root
   component until that operation finishes;
2. every held component remains a no-follow directory with the original
   `(st_dev, st_ino)` identity and remains linked from its held parent;
3. no path string is reopened to restore trust after a capability has been
   established;
4. a Git subprocess used for authority verification runs from the authenticated
   root capability, not from a mutable pathname;
5. `SOURCES_READY` cannot be sealed, or returned as an idempotent existing
   transition, unless the registry independently authenticates the exact
   canonical snapshot store;
6. source review and replay raw receipts are not selectable phase evidence
   until a schema-valid commit marker binds their canonical path, SHA-256,
   receipt kind, RunSpec, acquisition contract, and code authority;
7. read-only status output never scans receipt directories or returns an
   implicitly selected receipt set.

The unavoidable pathname limitation is explicit: if an attacker replaces a
root before the caller establishes its initial binding, a pathname alone cannot
identify an earlier intended inode. Long term, mutation APIs should accept an
already-held capability. This PR guarantees identity from initial binding
through completion.

## 4. Shared trusted-root capability

Add a stdlib-only low-level module, `mlb_backtest/secure_fs.py`. It imports no
domain module and exposes the following internal infrastructure:

- `RootBinding`: the absolute lexical path, component names, and the initial
  `(st_dev, st_ino)` identity for every component from `/` to the root;
- `TrustedRoot`: ownership of the complete FD chain, with `assert_bound()`,
  `dup_root_fd()`, context-manager cleanup, and fail-closed double-close rules;
- `open_trusted_root(path, expected=None)`: opens components from `/` using
  `O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`, compares `fstat(child)` with
  `stat(name, dir_fd=parent, follow_symlinks=False)`, and optionally requires an
  existing `RootBinding`;
- `open_parent(...)`: rejects empty, `.`, and `..` components and opens or
  creates descendants relative to the held root;
- narrowly scoped regular-file snapshot and exclusive-write helpers that
  enforce one-link regular files, before/after identity, bounded reads, fsync,
  and fail-closed residue preservation.

An `O_EXCL` create publishes a name before later validation and durability
steps finish. If any subsequent step fails, the writer closes every descriptor
but never unlinks that public name: an authenticate-then-unlink sequence has an
unavoidable TOCTOU window in which an attacker can replace the authenticated
inode before `unlink`. The owned partial or complete file therefore remains as
uncommitted residue, a retry conflicts with `FileExistsError`, and an attacker
replacement remains unchanged. Registry receipt publication and every later
execution-registry cleanup path use the same residue-preservation rule.

Content addressing, receipt schemas, snapshot-store lock semantics, and other
domain policy remain in their current modules. The shared module owns only
filesystem trust mechanics.

The capability provides deterministic test hooks after each component open,
before the first artifact read, immediately before a Git subprocess, after file
fsync but before parent fsync, and before final return revalidation.

### 4.1 Execution registry integration

`_RegistryPaths` or its lock binding holds a `TrustedRoot` for the full registry
operation. Registry reads, writes, lock verification, checkpoint handling, and
final receipt publication use the held root FD and call `assert_bound()` before
and after trust-critical stages.

Git authority checks use `/dev/fd/<root-fd>` with the descriptor passed to the
child. A platform without a verified FD-backed working directory fails closed;
it must not fall back to pathname `cwd` for a trust-critical Git check.

### 4.2 Timezone-review integration

The existing `_trusted_root()` plus `_open_root()` path sequence is replaced by
one `TrustedRoot`. Read, exclusive write, reconciliation, cleanup, and final
rebind share that same capability. Public signatures remain unchanged.

### 4.3 Snapshot-store integration

Path-based `publish_snapshot_store`, `load_snapshot_store`, and inventory reads
open a `TrustedRoot` and delegate to the existing FD-oriented implementation.
The direct `_open_readonly_root()` and `_open_root_directory()` pathname trust
paths are removed or reduced to capability adapters. The already-safe
source-finalization FD publication path remains the reference implementation.

`FinalizationContext` stores the `RootBinding` created by `build_context`.
Every later public service operation opens a fresh capability that must match
that binding, closing replacement between context construction and use.

## 5. Non-bypassable `SOURCES_READY` authority

The execution registry must authenticate the canonical snapshot store itself;
the source-finalization service preflight is not a substitute for the phase
authority.

Under the existing registry lock, a private registry helper:

1. authenticates the RunSpec, acquisition contract, current phase, trusted Git
   snapshot, and terminal `REMAINING` checkpoint;
2. constructs replay inputs from that already-loaded checkpoint without
   reacquiring the registry lock;
3. builds `SnapshotStoreInputs` using the exact schedule, game, timezone,
   checkpoint, RunSpec, contract, and code bindings;
4. calls the canonical `load_snapshot_store` path through the held trusted-root
   capability;
5. requires the loaded inventory bytes to equal the caller-supplied inventory
   snapshot exactly.

To avoid lock re-entry, `v52_verify_sources` gains a private replay-input
factory that accepts an authenticated checkpoint. Its existing public function
keeps its signature and delegates after loading the checkpoint normally.
Registry imports of snapshot/replay helpers occur locally after module
initialization to avoid import cycles.

The exact store authentication runs:

- during read-only `validate_sources_ready_evidence_set`;
- once after loading the terminal checkpoint in the transition path;
- again immediately before publishing the immutable phase receipt; and
- on an identical `SOURCES_READY` retry before returning an existing result.

A generic direct-call failure preserves the current quarantine policy and does
not advance the phase. The atomic source-finalization API fails without adding
generic quarantine evidence, preserving its existing all-or-nothing contract.

## 6. Receipt and CLI contract amendment

Review and replay each produce two separate artifacts:

1. an append-only raw source-review or clean-replay receipt at
   `--write-receipt`;
2. an immutable `SOURCE_RECEIPT_COMMIT` marker at `--write-commit`.

The marker does not replace or duplicate the raw receipt. It commits the raw
receipt's canonical path, SHA-256, and kind together with the execution
authority. If raw receipt creation succeeds but marker creation fails, the raw
artifact remains uncommitted residue and cannot satisfy the phase gate.

For compatibility, the existing `advance` option names remain
`--review-receipt` and `--replay-receipt`, but their values are explicitly
defined as commit-marker paths. `advance` authenticates marker to raw receipt
and never scans a directory to select either artifact.

The 2026-07-19 design, 2026-07-20 plan, exact operator sequence, CLI examples,
and implementation record are amended to describe this pair and include
`schemas/v5_2_source_receipt_commit.schema.json` as the fourteenth bound schema.

The `status` contract is narrowed to the implemented explicit-path-only policy.
It reports registry/checkpoint/inventory readiness and blockers but does not
enumerate review or replay receipt namespaces. Receipt authentication happens
only when explicit paths are supplied to the matching operation. This avoids a
new scanner and preserves the rule that status cannot select phase evidence.

## 7. Execution-code closure

`mlb_backtest/secure_fs.py` is reachable from existing execution-authority
roots. The expected exact closure therefore changes from:

```text
49 = 34 reachable Python + 1 explicit Python extra + 14 schemas
```

to:

```text
50 = 35 reachable Python + 1 explicit Python extra + 14 schemas
```

Tests recompute the real import closure and require exact equality. The design,
plan, implementation record, and trusted Git bindings must all use the
recomputed value; no test may merely update a hard-coded count without proving
reachability.

## 8. Error handling and compatibility

- Existing public CLI and Python signatures remain stable.
- Root replacement, unavailable FD-backed Git verification, invalid store
  replay, or marker/raw mismatch fails closed with the existing stable error
  mapping and no traceback in JSON output.
- No existing file is overwritten to recover from a conflict.
- Cleanup removes only a file created by the current attempt after exact FD,
  inode, link-count, and byte verification.
- Existing exact retries remain idempotent only while the complete trusted
  store and evidence set still authenticate.

## 9. Test design

Implementation follows red-green-refactor. Required regressions include:

### 9.1 Registry authority

- a schema-shaped fake inventory plus otherwise valid receipts cannot directly
  advance `ACQUIRING -> SOURCES_READY` without the canonical store;
- a structurally complete store with a missing blob, mismatched digest,
  authority mismatch, or non-replayable contents is rejected;
- a real temporary 81-snapshot store and terminal checkpoint succeed through
  the direct registry API;
- deleting or replacing store evidence after a successful seal prevents an
  idempotent retry from returning `EXISTING`;
- the final revalidation catches replacement between initial validation and
  phase-receipt publication;
- checkpoint-injected replay construction does not reacquire the registry lock.

### 9.2 Filesystem capability

For execution registry, timezone review, and snapshot-store path adapters:

- replace a root ancestor with a symlink and with a different regular
  directory at every hookable boundary;
- replace the project root between context construction and each mutating or
  replay operation;
- substitute a fake Git repository immediately before authority verification;
- exercise symlink, hard-link, FIFO, and regular-file swaps;
- assert no external canary path is written, no FD leaks remain, and locks are
  cleaned only when owned and authenticated;
- retain existing concurrent identical-call and idempotency behavior.

### 9.3 Receipt protocol and CLI

- parser/help and subprocess E2E require both raw and commit output paths for
  review and replay;
- advance succeeds only with marker paths and rejects raw receipt paths;
- wrong marker path, digest, kind, duplicate marker, missing raw receipt, or
  raw receipt replacement leaves phase and phase receipts unchanged;
- raw receipt success followed by marker conflict remains unselectable;
- status performs no receipt-directory scan and returns no selected set.

### 9.4 Repository gates

- remove the existing trailing whitespace in the PR range;
- run both worktree and committed-range checks:

```bash
git diff --check
git diff --check 5110d20389326015e9eb4ddc2c4aaf4c23227d2e HEAD
```

- run focused secure-FS, registry, snapshot-store, timezone, source-finalize,
  and source-verification suites before the full stable test suite;
- preserve the four protected untracked duplicate files;
- assert that production historical namespaces remain absent after tests.

## 10. Documentation and verification record

After code and tests pass:

1. update the existing source-finalization design and implementation plan;
2. update `source_finalization_cli_implementation.md` with the new commit range,
   exact closure, schema hashes, commands, test counts, elapsed times, and
   committed-range whitespace result;
3. update PR #1's body with the final verified figures;
4. request independent core and security re-review;
5. mark the PR ready only when no Critical or Important issue remains;
6. merge to `master` only after the ready PR is still mergeable and every
   available check is green.

## 11. Out of scope

- canonical historical acquisition or publication;
- performing an independent human source review;
- executing clean production replays or the `SOURCES_READY` transition;
- fitting v5.2, generating OOF predictions, or running the 500-pick evaluation;
- converting all callers to a public FD-only API;
- adding a receipt-directory discovery or selection service.

## 12. Acceptance criteria

The hardening is complete when all required adversarial tests pass, the exact
execution closure is recomputed and bound, targeted and full suites pass on the
final commit, committed-range whitespace is clean, independent re-review finds
no merge blocker, the four protected duplicates are untouched, and no canonical
historical namespace has been created by implementation tests.
