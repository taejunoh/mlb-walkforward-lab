# PR #1 Merge-Blocker Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close PR #1's registry-bypass, ancestor-TOCTOU, and receipt-contract blockers so the branch can safely merge to `master`.

**Architecture:** Add a stdlib-only root-FD capability that retains and revalidates the complete directory chain. Route registry, snapshot-store, timezone, and finalization trust boundaries through it, and make the registry independently exact-load the canonical 81-snapshot store before every `SOURCES_READY` seal or retry.

**Tech Stack:** Python 3.11, `unittest`, POSIX `dir_fd`, `O_DIRECTORY`, `O_NOFOLLOW`, macOS `/dev/fd`, JSON Schema 2020-12, Git/GitHub CLI, Ruff.

---

## Working rules and file map

- Work only in `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest` on `feature/mlb-v5-1-backtest`.
- Never touch or stage the four protected `* 2.*` duplicates already listed by `git status`.
- Tests must not create `audit/v5_2_qd/historical/` or `data/v5_2_qd/historical/` in the real repository.
- Every task is RED test, minimal implementation, focused GREEN, Ruff/diff check, explicit-file commit.
- `mlb_backtest/secure_fs.py` owns filesystem trust mechanics only. Domain schemas, content addressing, and receipt policy stay in their existing modules.

### Task 1: Add the trusted-root FD capability

**Files:**
- Create: `mlb_backtest/secure_fs.py`
- Create: `tests/test_secure_fs.py`

- [ ] **Step 1: Write failing root-binding tests**

Create `TrustedRootTests` for regular-directory ancestor replacement, symlink ancestors, expected binding mismatch, close/use-after-close, and FD cleanup.

```python
def test_expected_binding_rejects_replacement_between_operations(self):
    root = self.path / "root"
    root.mkdir()
    with secure_fs.open_trusted_root(root) as trusted:
        binding = trusted.binding
    root.rename(self.path / "original")
    root.mkdir()
    with self.assertRaisesRegex(ValueError, "differs from expected"):
        secure_fs.open_trusted_root(root, expected=binding)

def test_use_after_close_fails_closed(self):
    trusted = secure_fs.open_trusted_root(self.path)
    trusted.close()
    with self.assertRaisesRegex(RuntimeError, "closed"):
        trusted.assert_bound()
    with self.assertRaisesRegex(RuntimeError, "closed"):
        trusted.dup_root_fd()
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_secure_fs.TrustedRootTests -v
```

Expected: `ModuleNotFoundError: mlb_backtest.secure_fs`.

- [ ] **Step 3: Implement the exact capability interface**

Use exact fields `RootBinding(path: Path, component_names: tuple[str, ...], component_identities: tuple[tuple[int, int], ...])`. `TrustedRoot` exposes read-only `path`, `binding`, and `root_fd` properties plus `assert_bound()`, `dup_root_fd()`, `close()`, and context-manager methods. The constructor stays private; callers use exactly `open_trusted_root(path: Path, *, expected: RootBinding | None = None) -> TrustedRoot`. Define `_secure_fs_test_hook(stage: str, component: str | None = None) -> None` as a no-op production hook.

Open `/` and each lexical `abspath` component with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`. Compare `fstat(child)` to `stat(name, dir_fd=parent, follow_symlinks=False)` at open and in every `assert_bound()`. Retain all FDs until close; do not use `resolve()`.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_secure_fs.TrustedRootTests -v
ruff check mlb_backtest/secure_fs.py tests/test_secure_fs.py
git diff --check
git add mlb_backtest/secure_fs.py tests/test_secure_fs.py
git commit -m "feat: add trusted root filesystem capability"
```

### Task 2: Add capability-relative safe file primitives

**Files:**
- Modify: `mlb_backtest/secure_fs.py`
- Modify: `tests/test_secure_fs.py`

- [ ] **Step 1: Write failing path, read, write, and cleanup tests**

Reject absolute, empty, `.`, `..`, and backslash paths; symlink, hard-link, FIFO, socket, and directory reads; ancestor swaps before/after reads; short writes; and attacker replacement during cleanup. Snapshot `/dev/fd` around every fault test when available.

```python
def test_write_never_unlinks_attacker_replacement(self):
    with secure_fs.open_trusted_root(self.root) as trusted:
        def replace(stage, component=None):
            if stage == "after_file_fsync":
                target = self.root / "audit/receipt.json"
                target.unlink()
                target.write_bytes(b"attacker\n")
        with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace):
            with self.assertRaises(ValueError):
                secure_fs.write_exclusive(
                    trusted, Path("audit/receipt.json"), b"owned\n", "receipt"
                )
    self.assertEqual((self.root / "audit/receipt.json").read_bytes(), b"attacker\n")
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_secure_fs -v
```

Expected: missing helpers or an unsafe case does not raise.

- [ ] **Step 3: Implement the primitive boundary**

Add exact immutable fields `RegularFileSnapshot(relative: Path, contents: bytes, identity: tuple[int, ...])`. Add these exact functions: `open_parent(root: TrustedRoot, relative: Path, *, create: bool) -> Iterator[int]`; `read_regular_snapshot(root, relative, label, *, max_bytes=None, missing_ok=False) -> RegularFileSnapshot | None`; `rebind_regular_snapshot(root, expected, label, *, max_bytes=None) -> None`; and `write_exclusive(root, relative, contents, label, *, final_rebind=None) -> RegularFileSnapshot`.

Reads require a one-link regular file and identical pre/post identity. Writes use `O_EXCL | O_NOFOLLOW`, handle short writes, fsync file and parent, and unlink only the inode created by the current attempt.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_secure_fs -v
ruff check mlb_backtest/secure_fs.py tests/test_secure_fs.py
git diff --check
git add mlb_backtest/secure_fs.py tests/test_secure_fs.py
git commit -m "feat: add capability-bound secure file operations"
```

### Task 3: Route snapshot-store path APIs through the capability

**Files:**
- Modify: `mlb_backtest/v52_snapshot_store.py:692-865,1075-1112,1250-1263,1337-1356`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Add RED tests** named `test_path_publish_rejects_regular_ancestor_swap_without_external_write`, `test_path_read_rejects_symlink_ancestor_swap`, `test_path_load_rejects_root_replacement_before_final_return`, and `test_path_adapters_close_capability_fds_on_failures`.

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_snapshot_store.AttackTests -v
```

Expected: at least one path adapter accepts replacement or leaks an FD.

- [ ] **Step 3: Replace pathname trust adapters only**

```python
def load_snapshot_store(root: Path, inputs: SnapshotStoreInputs) -> LoadedSnapshotStore:
    with open_trusted_root(root) as trusted:
        result = load_snapshot_store_from_fd(trusted.root_fd, inputs)
        _load_test_hook("before_path_load_return")
        trusted.assert_bound()
        return result

def read_inventory(root: Path) -> SourceInventory:
    with open_trusted_root(root) as trusted:
        result = read_inventory_from_fd(trusted.root_fd)
        trusted.assert_bound()
        return result
```

Use the same adapter for `publish_snapshot_store`. Preserve caller-owned `*_from_fd` behavior and remove independent root-string reopen paths.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_snapshot_store -v
ruff check mlb_backtest/v52_snapshot_store.py tests/test_v52_snapshot_store.py
git diff --check
git add mlb_backtest/v52_snapshot_store.py tests/test_v52_snapshot_store.py
git commit -m "fix: anchor snapshot store paths to trusted root"
```

### Task 4: Bind timezone-review artifacts to one capability

**Files:**
- Modify: `mlb_backtest/v52_timezone_review.py:621-946,953-1270`
- Modify: `tests/test_v52_timezone_review.py`

- [ ] **Step 1: Add RED tests** for candidate, review, and seal writes with regular-directory and symlink ancestor swaps after binding and after mapping fsync. Assert canary trees, attacker replacements, and FD counts are unchanged.

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_timezone_review -k ancestor -v
```

Expected: replacement root is accepted or an external artifact appears.

- [ ] **Step 3: Keep public signatures and share one capability**

```python
def write_timezone_review_receipt(root: Path, **kwargs: object) -> Path:
    with open_trusted_root(root) as trusted:
        result = _write_timezone_review_receipt(trusted, **kwargs)
        trusted.assert_bound()
        return result
```

Replace `_trusted_root()` plus `_open_root()` with capability-relative read/write/reconcile helpers. Never reopen the root pathname inside one operation.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_timezone_review -v
ruff check mlb_backtest/v52_timezone_review.py tests/test_v52_timezone_review.py
git diff --check
git add mlb_backtest/v52_timezone_review.py tests/test_v52_timezone_review.py
git commit -m "fix: bind timezone review writes to trusted root"
```

### Task 5: Bind registry and Git checks to one capability

**Files:**
- Modify: `mlb_backtest/secure_fs.py`
- Modify: `mlb_backtest/execution_registry.py:278-598,1013-1139,1340-1542,3657-4440`
- Modify: `tests/test_secure_fs.py`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Add RED tests** for root swap while locked, fake repository substitution before Git, `/dev/fd` unavailability, captured `pass_fds`, final receipt rebind, and FD cleanup.

```python
@contextmanager
def fd_working_directory(root: TrustedRoot) -> Iterator[tuple[str, int]]:
    root.assert_bound()
    if not Path("/dev/fd").is_dir():
        raise RuntimeError("authenticated fd working directory is unavailable")
    descriptor = root.dup_root_fd()
    try:
        yield f"/dev/fd/{descriptor}", descriptor
    finally:
        os.close(descriptor)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest -k "root_swap or authenticated_dev_fd or fake_repository" -v
```

Expected: pathname `cwd` is observed or replacement is accepted.

- [ ] **Step 3: Implement** `_RegistryPaths.trusted_root`, start descendant opens from `dup_root_fd()`, replace absolute-root `stat` with `assert_bound()`, and run Git with `/dev/fd/<fd>` plus `pass_fds=(fd,)`. No pathname fallback.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_secure_fs tests.test_execution_registry -v
ruff check mlb_backtest/secure_fs.py mlb_backtest/execution_registry.py tests/test_secure_fs.py tests/test_execution_registry.py
git diff --check
git add mlb_backtest/secure_fs.py mlb_backtest/execution_registry.py tests/test_secure_fs.py tests/test_execution_registry.py
git commit -m "fix: bind execution registry to root capability"
```

### Task 6: Persist root identity in `FinalizationContext`

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py:91-123,245-440,620-835,1022-1665`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Add stale-context RED tests** replacing root after context construction but before each of `status`, `preflight`, `publish`, `review`, `replay`, and `advance`. Assert no phase, receipt, source, canary, or FD changes.

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_finalize.ContextTests -k binding -v
```

Expected: context lacks a binding or starts in the replacement root.

- [ ] **Step 3: Add and enforce a non-optional binding**

Insert the exact non-default field `root_binding: RootBinding` immediately after the existing `root: Path` field in `FinalizationContext`. Keep every other existing field name, order, and type unchanged, and update all constructors explicitly.

`build_context()` records `trusted.binding`. Every public service opens `open_trusted_root(context.root, expected=context.root_binding)` through return. Update all direct test contexts with a real binding helper; do not add a production `None` bypass.

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_finalize -v
ruff check mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git diff --check
git add mlb_backtest/v52_source_orchestration.py tests/test_v52_source_finalize.py
git commit -m "fix: rebind finalization services to trusted root"
```

### Task 7: Factor checkpoint-injected replay inputs

**Files:**
- Modify: `mlb_backtest/v52_verify_sources.py:1223-1325`
- Modify: `tests/test_v52_verify_sources.py`

- [ ] **Step 1: Add a RED test** that patches `load_source_verification_checkpoint` to raise, calls the injected factory with an authenticated checkpoint, and asserts the query graph receives that exact object.

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_verify_sources.SnapshotReplayInputFactoryTests -v
```

Expected: missing `_load_snapshot_replay_inputs_from_checkpoint`.

- [ ] **Step 3: Extract without changing the public signature**

Add the exact private signature `_load_snapshot_replay_inputs_from_checkpoint(root: Path, run_spec: RunSpec, *, acquisition_contract_path: Path, checkpoint: AcquisitionCheckpoint) -> SnapshotReplayInputs`. Move the existing verified-reader body from `load_snapshot_replay_inputs` into it without changing reader order or validation. Leave this concrete public wrapper:

```python
def load_snapshot_replay_inputs(
    root: Path, run_spec: RunSpec, *, acquisition_contract_path: Path,
) -> SnapshotReplayInputs:
    checkpoint = load_source_verification_checkpoint(root, run_spec)
    return _load_snapshot_replay_inputs_from_checkpoint(
        root, run_spec,
        acquisition_contract_path=acquisition_contract_path,
        checkpoint=checkpoint,
    )
```

- [ ] **Step 4: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_verify_sources.SnapshotReplayInputFactoryTests -v
ruff check mlb_backtest/v52_verify_sources.py tests/test_v52_verify_sources.py
git diff --check
git add mlb_backtest/v52_verify_sources.py tests/test_v52_verify_sources.py
git commit -m "refactor: inject authenticated source checkpoint"
```

### Task 8: Build a real 81-snapshot registry fixture

**Files:**
- Modify: `tests/test_execution_registry.py`
- Reuse: `tests/v52_snapshot_store_fixture.py`

- [ ] **Step 1: Add `authenticated_source_evidence`** while preserving the weak two-SHA fixture for negative tests. Import `fake_81`, `replay_inputs`, `store_inputs`, and `patched_store_replay` from `tests.v52_snapshot_store_fixture`. Rebind the returned replay `run_id` and every `AuthorityBinding` path/SHA to files created by `make_repo`, replace the fixture checkpoint object with the terminal checkpoint loaded for that repository, call real `build_inventory()` and `publish_snapshot_store()` inside `patched_store_replay()`, then construct the review/replay raw receipts and three commit markers from the published inventory's actual `snapshot_sha256s` and `logical_query_ids`. The exact signature is `authenticated_source_evidence(self, root: Path, run_spec: RunSpec) -> Iterator[tuple[Path, tuple[Path, Path, Path], Path]]`; it yields inventory path, three marker paths, and terminal checkpoint path, and removes only fixture patches on exit.

- [ ] **Step 2: Verify the fixture contract**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest.test_authenticated_source_fixture_publishes_exact_81_store -v
```

Expected: `OK`; inventory contains 81 snapshots and 42 source-season rows, with three markers and one terminal checkpoint.

- [ ] **Step 3: Commit**

```bash
git diff --check
git add tests/test_execution_registry.py
git commit -m "test: build authenticated 81-snapshot registry fixture"
```

### Task 9: Make the registry authenticate the canonical store

**Files:**
- Modify: `mlb_backtest/execution_registry.py:639-680,3818-3975,4046-4350`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Add direct-call and read-only prevalidator RED tests.** A schema-shaped inventory plus otherwise valid markers must fail, remain `ACQUIRING`, create no `SOURCES_READY` receipt, and follow existing generic quarantine policy. The prevalidator must leave the execution tree byte-identical.

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_execution_registry.ExecutionRegistryTest.test_direct_sources_ready_rejects_inventory_without_snapshot_store \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_prevalidator_rejects_inventory_without_snapshot_store_read_only -v
```

Expected: `ValueError not raised` before the fix.

- [ ] **Step 3: Add the exact verifier closure**

Add the exact signature `_build_sources_ready_store_verifier(paths: _RegistryPaths, run_spec: RunSpec, base: Mapping[str, object], *, checkpoint: AcquisitionCheckpoint, checkpoint_head: Mapping[str, object], inventory_snapshot: _ArtifactSnapshot, current_phase: Literal["ACQUIRING", "SOURCES_READY"]) -> Callable[[], None]`.

Use local imports only for `AuthorityBinding`, `AuthorityBindings`, `SnapshotStoreInputs`, `TerminalCheckpointBinding`, `inventory_bytes`, `load_snapshot_store`, and `_load_snapshot_replay_inputs_from_checkpoint`. Build frozen inputs from the locked base/checkpoint and recaptured schedule/game/timezone authorities. Each closure call exact-loads all 81 snapshots and requires `inventory_bytes(loaded.inventory) == inventory_snapshot.contents`. Convert domain failures to `ValueError("SOURCES_READY snapshot store authentication failed")`.

- [ ] **Step 4: Call the verifier twice in the prevalidator**: after terminal checkpoint authentication and after final artifact/Git rebind. Convert every success-expected source-gate test to `authenticated_source_evidence`; keep weak fixtures only for rejection tests.

- [ ] **Step 5: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_execution_registry.ExecutionRegistryTest.test_direct_sources_ready_rejects_inventory_without_snapshot_store \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_prevalidator_rejects_inventory_without_snapshot_store_read_only \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_binds_real_inventory_review_and_two_clean_replays \
  tests.test_execution_registry.ExecutionRegistryTest.test_public_sources_ready_prevalidator_is_read_only_for_valid_evidence -v
ruff check mlb_backtest/execution_registry.py tests/test_execution_registry.py
git diff --check
git add mlb_backtest/execution_registry.py tests/test_execution_registry.py
git commit -m "fix: authenticate snapshot store at source gate"
```

### Task 10: Reauthenticate retries and final publication

**Files:**
- Modify: `mlb_backtest/execution_registry.py:4046-4350`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Add tampered-retry and post-link race RED tests.** After a real seal, alter one blob and retry identical arguments; it must not return `EXISTING`. Mutate a blob from `_after_registry_publish`; the new receipt must be removed and phase remain `ACQUIRING`.

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_exact_retry_rejects_tampered_snapshot_blob \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_post_link_blob_race_cleans_new_phase_receipt -v
```

Expected: retry returns existing or the race leaves a phase receipt.

- [ ] **Step 3: Wire one verifier through every authority boundary.** Call it after checkpoint load, inside `verify_source_evidence_and_checkpoint()` before/after link and final rebind, and in the already-`SOURCES_READY` branch before `created=False`.

```python
try:
    store_verifier()
    verify_existing_source_evidence()
except ValueError as exc:
    raise _LateSourceEvidenceError(
        "late source store or evidence changed before publication"
    ) from exc
```

- [ ] **Step 4: Add the atomic counterpart.** The same post-link mutation raises `atomic source gate evidence failed`, creates no quarantine or partial receipt, and remains `ACQUIRING`.

- [ ] **Step 5: Run GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_exact_retry_returns_existing_without_new_receipt \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_exact_retry_rejects_tampered_snapshot_blob \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_post_link_blob_race_cleans_new_phase_receipt \
  tests.test_execution_registry.ExecutionRegistryTest.test_atomic_sources_ready_post_link_blob_race_is_nonmutating -v
ruff check mlb_backtest/execution_registry.py tests/test_execution_registry.py
git diff --check
git add mlb_backtest/execution_registry.py tests/test_execution_registry.py
git commit -m "fix: revalidate source store through phase publication"
```

### Task 11: Freeze marker behavior and exact closure 50

**Files:**
- Modify: `mlb_backtest/execution_registry.py:110-160`
- Modify: `tests/test_v52_source_finalize.py`
- Modify: `tests/test_execution_registry.py:164-190,6573-6660`
- Verify: `tests/test_v52_acquire_e2e.py`

- [ ] **Step 1: Add subprocess and direct marker negatives.** Missing `--write-commit` must exit 2 with one canonical `USAGE_ERROR` JSON line and empty stderr. Passing raw receipt paths to `advance`, wrong marker path/SHA/kind, missing/replaced raw, duplicates, and uncommitted residue must leave phase and receipt tree unchanged. Patch `listdir`/`scandir` to prove status never scans receipt namespaces.

- [ ] **Step 2: Run marker tests**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_source_finalize.CliTests \
  tests.test_v52_source_finalize.FinalizationEndToEndTests \
  tests.test_execution_registry.ExecutionRegistryTest.test_sources_ready_rejects_markerless_raw_source_receipts \
  tests.test_execution_registry.ExecutionRegistryTest.test_source_commit_marker_binding_and_underlying_uniqueness_are_enforced -v
```

Expected: new tests pass against existing marker hardening; change production code only if RED proves a bypass.

- [ ] **Step 3: Bind the recomputed closure**

```python
self.assertIn(Path("mlb_backtest/secure_fs.py"), reachable)
self.assertEqual(len(reachable), 35)
self.assertEqual(len(bound_python - reachable), 1)
self.assertEqual(len(BOUND_SCHEMA_PATHS), 14)
self.assertEqual(len(bound), 50)
```

Add `secure_fs.py` to `_BOUND_EXECUTION_CODE` only after the reachability assertion fails as expected. Dirtying or untracking it must block initialize, load, publication, and transition.

- [ ] **Step 4: Run closure/E2E GREEN and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_execution_registry.ExecutionRegistryTest.test_bound_execution_import_closure_is_computed_and_exact \
  tests.test_execution_registry.ExecutionRegistryTest.test_rejects_dirty_bound_execution_code_but_not_unrelated_files \
  tests.test_v52_acquire_e2e -v
ruff check mlb_backtest tests
git diff --check
git add mlb_backtest/execution_registry.py tests/test_execution_registry.py tests/test_v52_source_finalize.py
git commit -m "test: bind secure filesystem and marker authority"
```

### Task 12: Amend approved contracts and PR-range whitespace

**Files:**
- Modify: `docs/superpowers/specs/2026-07-19-mlb-v5-2-source-finalization-cli-design.md:52-99,178-194,223-315,367-429`
- Modify: `docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md:13-30,691-730,856-1195,1267-1490`
- Modify: `docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md:3`

- [ ] **Step 1: Remove the two existing trailing spaces** after `Date: 2026-07-14`.

- [ ] **Step 2: Amend both contracts** to state:

```text
review/replay = append-only raw receipt (--write-receipt)
              + immutable SOURCE_RECEIPT_COMMIT (--write-commit)
advance --review-receipt/--replay-receipt values = commit-marker paths
status = registry/checkpoint/inventory readiness only; no receipt scan/selection
closure = 50 = 35 reachable Python + 1 explicit extra + 14 schemas
```

The operator sequence writes three raw/marker pairs and passes only marker paths to `advance`. Raw success plus marker failure remains unselectable residue.

- [ ] **Step 3: Add both diff gates to the old plan**

```bash
git diff --check
git diff --check 5110d20389326015e9eb4ddc2c4aaf4c23227d2e HEAD
```

- [ ] **Step 4: Verify and commit**

```bash
git diff --check
git diff --check 5110d20389326015e9eb4ddc2c4aaf4c23227d2e HEAD
git add docs/superpowers/specs/2026-07-19-mlb-v5-2-source-finalization-cli-design.md \
  docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md \
  docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md
git commit -m "docs: align source finalization hardening contracts"
```

Expected: both diff checks exit 0 with no output.

### Task 13: Run focused verification and update the implementation record

**Files:**
- Modify: `reports/v5_2_qd/source_finalization_cli_implementation.md`

- [ ] **Step 1: Run the focused gate and record exact count/time**

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/time -p python3 -m unittest \
  tests.test_secure_fs \
  tests.test_execution_registry \
  tests.test_v52_snapshot_store \
  tests.test_v52_timezone_review \
  tests.test_v52_source_finalize \
  tests.test_v52_verify_sources -v
```

Expected: all focused tests `OK`; never estimate count or elapsed time.

- [ ] **Step 2: Run static, closure, and repository guards**

```bash
ruff check mlb_backtest tests
git diff --check
git diff --check 5110d20389326015e9eb4ddc2c4aaf4c23227d2e HEAD
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry.ExecutionRegistryTest.test_bound_execution_import_closure_is_computed_and_exact -v
git status --short
git diff --name-only --cached
test ! -e audit/v5_2_qd/historical
test ! -e data/v5_2_qd/historical
git ls-files --error-unmatch 'mlb_backtest/v52_query_plan 2.py' >/dev/null 2>&1; test $? -ne 0
git ls-files --error-unmatch 'tests/test_v52_query_plan 2.py' >/dev/null 2>&1; test $? -ne 0
```

Expected: all gates pass; cached diff is empty; only four protected duplicates are untracked; production namespaces are absent.

- [ ] **Step 3: Update the record** with tested code HEAD/range, `50 = 35 + 1 + 14`, 14 current schema hashes, exact focused result, both diff gates, protected state, namespace absence, and this exact sentence:

```text
No canonical historical acquisition, source publication, review, replay, or phase transition was performed by implementation tests.
```

- [ ] **Step 4: Review and commit**

```bash
git diff --check
git add reports/v5_2_qd/source_finalization_cli_implementation.md
git commit -m "docs: record verified PR hardening"
```

### Task 14: Run full verification, re-review, and integrate PR #1

**Files:**
- Update externally: GitHub PR #1 body and draft status
- Preserve the feature worktree through review feedback

- [ ] **Step 1: Run the full stable suite on final HEAD**

```bash
PYTHONDONTWRITEBYTECODE=1 /usr/bin/time -p python3 -m unittest discover \
  -s tests -p 'test*.py' -v
```

Expected: all discovered tests `OK`; capture exact count and elapsed time in the PR body.

- [ ] **Step 2: Repeat final gates**

```bash
ruff check mlb_backtest tests
git diff --check
git diff --check 5110d20389326015e9eb4ddc2c4aaf4c23227d2e HEAD
git status --short
test ! -e audit/v5_2_qd/historical
test ! -e data/v5_2_qd/historical
```

Expected: only four protected duplicates remain untracked.

- [ ] **Step 3: Push and request three independent re-reviews**

```bash
git push origin feature/mlb-v5-1-backtest
```

Review `5110d20389326015e9eb4ddc2c4aaf4c23227d2e..HEAD` for core architecture, security/data integrity, and plan/test alignment. Fix every Critical/Important item with RED/GREEN tests and rerun affected focused suites plus the full suite on final HEAD.

- [ ] **Step 4: Update PR and inspect checks**

```bash
gh pr view 1 --json isDraft,mergeable,headRefOid,statusCheckRollup
gh pr checks 1 --watch
```

Update PR #1 with closure, exact focused/full results, final HEAD, and safety disclaimer. If no checks are configured, record that fact rather than claiming green CI.

- [ ] **Step 5: Mark ready and merge only after clean gates**

```bash
gh pr ready 1
gh pr view 1 --json isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup
gh pr merge 1 --merge --delete-branch=false
```

Expected: `isDraft=false`, `mergeable=MERGEABLE`, zero Critical/Important review findings, and every available check successful. After merge, verify `origin/master` contains the merge, then use the finishing-branch cleanup workflow without deleting protected untracked files.
