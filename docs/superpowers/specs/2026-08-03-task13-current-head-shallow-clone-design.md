# Task 13 Current-HEAD Shallow Clone Design

**Status:** Approved design on 2026-08-03  
**Scope:** Remove Task 6 test and exact-suite dependence on unrelated Git history while preserving an independent, exact-HEAD checkout.

## Problem

Task 6 must run tests and the canonical exact suite from an isolated checkout of the current reviewed commit. Two helpers currently clone more repository history than their consumers use:

- `tests/v52_recovery_schedule_conflict_fixture.py::matching_source_authority_root()` uses `git clone --no-local` from a local path; and
- `scripts/run_task13_exact_suite.py::clone_repo_at_head()` uses `git clone --local --no-hardlinks`.

The local feature branch contains one missing historical blob, `23f74f057d09b8773ea5244d5c47a8fbef3e056c`, formerly used by `tests/test_v52_query_plan.py` between commits `82818f1ad3e41b14a4524677ba5bfecda0bfa378` and `84fac42694105248390d0f174fbf380ac6d03174`. The blob is not present in GitHub, the current HEAD tree, the current index, the disabled pack indexes, or 53 accessible same-name local candidates. The current file is complete and has blob ID `103d64aa3e6c5dfce1d371d6c5c7548b313632b5`.

A normal clone asks upload-pack to traverse the full advertised feature-branch history and fails on the missing historical blob. A depth-one single-branch clone of the same checkout succeeds because both consumers require only the current committed tree.

## Goals

1. Materialize an independent checkout containing exactly the source repository's recorded HEAD tree.
2. Avoid requesting historical parents, tags, other branches, or unrelated refs.
3. Avoid hard links, shared object stores, and Git alternates.
4. Finish with a detached HEAD equal to the source HEAD recorded before cloning.
5. Fail closed if the source HEAD changes during cloning, the recorded commit is unavailable, checkout fails, or the resulting HEAD differs.
6. Preserve all existing clean-checkout, evidence-copy, authority-building, and final exact-suite checks.

## Non-Goals

- Recovering, synthesizing, deleting, or ignoring the missing historical blob.
- Deleting or rewriting the feature branch or any historical commit.
- Changing canonical MLB evidence, contracts, inventories, snapshots, or protected parent files.
- Supporting history-dependent operations inside the isolated checkout.
- Introducing a new shared Git utility module or refactoring unrelated fixture code.

## Chosen Architecture

Keep the two existing helper boundaries and change only their clone mechanism.

Each helper performs this sequence:

1. Resolve the source repository and record `git rev-parse --verify HEAD`.
2. Convert the resolved source path to a `file://` URI using `Path.as_uri()`.
3. Clone with:

   ```text
   git clone
     --no-local
     --no-checkout
     --no-tags
     --single-branch
     --depth=1
     file://<absolute-source>
     <destination>
   ```

4. Run `git checkout --detach <recorded-head>` in the destination.
5. Require destination `git rev-parse --verify HEAD` to equal the recorded source HEAD.
6. Continue the existing helper-specific work:
   - `matching_source_authority_root()` copies only authenticated immutable 2019 evidence and builds the duplicate-disposition authority.
   - `clone_repo_at_head()` returns the detached design/source checkout used by the exact runner.

No explicit `--branch` is needed. With `--single-branch` and no branch argument, Git follows the source repository's advertised HEAD. This works for both an attached branch and a detached source HEAD. The explicit detached checkout and exact comparison remain authoritative.

## Security Invariants

The implementation must preserve these invariants:

- The source HEAD is captured before the clone begins.
- The destination contains only a depth-one snapshot of the source's advertised HEAD.
- Git still verifies every transferred current-tree object by object ID; the shallow boundary removes unused parents, not current-tree integrity checks.
- The destination has no `.git/objects/info/alternates` file and does not share objects through hard links.
- Tags and unrelated local or remote-tracking branches are not copied; only the selected current-HEAD branch tracking ref may exist.
- The final checkout is detached and its HEAD equals the pre-recorded source HEAD.
- The exact runner still rejects a dirty source checkout before cloning.
- The source-authority fixture still treats the configured old authority as evidence-only and never mutates it.
- The source-authority fixture retains its isolated temporary Git `HOME` behavior.
- All evidence copied after cloning retains its existing byte/hash authentication.
- No fallback to `--local`, `--shared`, full-history clone, ref deletion, or missing-object tolerance is allowed.

## Race and Error Handling

If the source HEAD advances after it is recorded, the depth-one clone will normally contain only the newer tip. Checkout of the recorded commit then fails because that commit is outside the shallow boundary. If Git happens to provide the recorded commit, the final exact-HEAD comparison still detects any mismatch. Both cases fail closed.

Clone failure, checkout failure, an unavailable recorded commit, destination HEAD mismatch, or authority construction failure propagates as an error. Existing temporary-directory cleanup remains responsible for removing incomplete fixture clones. The exact runner continues to use its existing temporary directory lifecycle.

Detached source HEAD is supported without a special branch fallback: omit `--branch`, clone the advertised detached HEAD with `--single-branch --depth=1`, then explicitly detach at the recorded SHA and compare it.

## Test Design

### Exact-suite runner

Extend `tests/test_task13_exact_suite_runner.py` with a real Git regression fixture:

1. Create a temporary repository.
2. Commit a sentinel file in commit A.
3. Remove or replace it in commit B so the current tree is complete without the old blob.
4. Delete only commit A's loose sentinel blob.
5. Prove a normal full `file://` clone fails.
6. Prove `clone_repo_at_head()` succeeds at commit B.
7. Assert the result is shallow, detached, exact-HEAD, has one reachable commit, has no tags/unrelated refs, and has no alternates.

Run the same helper with the source HEAD detached at commit B and require the same result.

### Source-authority fixture

Extend `tests/test_v52_source_schedule.py` beside the stale-authority materialization test. Build a temporary checkout whose tip tree contains the current required repository files but whose historical parent alone references a deleted loose sentinel blob. Use it as `checkout_root` with a byte-authenticated stale evidence root and require:

- `matching_source_authority_root()` succeeds without reading the missing parent blob;
- the materialized authority HEAD equals the supplied checkout HEAD and is detached/shallow;
- immutable evidence source bytes and metadata remain unchanged; and
- copied evidence and the built authority retain their existing authentication checks.

Tests must exercise real Git behavior. They may not merely assert command text or mock clone success.

## Files

Modify only:

- `scripts/run_task13_exact_suite.py`
- `tests/v52_recovery_schedule_conflict_fixture.py`
- `tests/test_task13_exact_suite_runner.py`
- `tests/test_v52_source_schedule.py`

No production MLB model, evidence, protocol, schema, audit, data, report, or configuration file changes are permitted.

## Acceptance Criteria

1. Both regression fixtures demonstrate that a full clone fails on a parent-only missing blob while the shallow current-HEAD helper succeeds.
2. Attached and detached source HEAD cases pass.
3. Destination checkouts are shallow, detached, exact-HEAD, independent, and have no alternates.
4. Existing clone, source-authority, Task 13 runner, and relevant Task 6 compatibility tests pass.
5. The previously blocked `tests.test_v52_acquire_e2e`, `tests.test_v52_verify_sources`, and clone-dependent game-evidence tests collect and execute rather than failing in upload-pack.
6. Ruff, Python 3.11 compilation, and diff checks pass.
7. No protected evidence or parent tree changes, ref rewrites, missing-object repairs, or Git history changes occur.
