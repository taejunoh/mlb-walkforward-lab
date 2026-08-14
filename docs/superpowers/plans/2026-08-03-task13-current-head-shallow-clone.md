# Task 13 Current-HEAD Shallow Clone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Task 6 fixture and exact-suite checkouts independent of unrelated Git history while retaining an exact, detached, cryptographically verified current-HEAD tree.

**Architecture:** Keep the existing `clone_repo_at_head()` and `matching_source_authority_root()` boundaries. Each records the source HEAD, performs a depth-one single-branch `file://` clone with no local optimization, tags, checkout, hard links, shared object store, or alternates, then checks out and verifies the recorded SHA in detached state. Real-Git regression fixtures prove that a missing parent-only blob breaks a full clone but cannot block the new current-tree snapshot.

**Tech Stack:** Python 3.11, `unittest`/`pytest`, Git CLI, `pathlib.Path.as_uri()`, Ruff.

---

## Execution Context and Guards

Run every command from:

```bash
export CODE_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2-tdd'
export PARENT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest'
export V51_INPUT='/private/tmp/v51-artifact-recovery.1CItVL/recovery-root'
export PY311='/opt/homebrew/opt/python@3.11/bin/python3.11'
export PYTHONDONTWRITEBYTECODE=1
cd "$CODE_ROOT"
```

The approved design is:

```text
docs/superpowers/specs/2026-08-03-task13-current-head-shallow-clone-design.md
```

Never modify refs or history to hide the missing blob. Never restore `.dataless-disabled` packs. Never write below `$PARENT`. Do not add `--local`, `--shared`, alternates, hard links, a full-history fallback, or missing-object tolerance. Only the four files listed in Tasks 1 and 2 may change.

### Task 1: Make the exact-suite clone an independent current-HEAD snapshot

**Files:**

- Modify: `tests/test_task13_exact_suite_runner.py`
- Modify: `scripts/run_task13_exact_suite.py`

- [ ] **Step 1: Add the real-Git RED fixture helpers**

Add this helper beside `_git()` in `tests/test_task13_exact_suite_runner.py`:

```python
def _repository_with_missing_parent_blob(base: Path) -> tuple[Path, str, str]:
    repo = base / "missing-parent-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "runner@example.test")
    _git(repo, "config", "user.name", "Runner Test")
    _git(repo, "config", "gc.auto", "0")

    obsolete = repo / "obsolete.txt"
    obsolete.write_bytes(b"historical-only\n")
    _git(repo, "add", "obsolete.txt")
    _git(repo, "commit", "-qm", "historical parent")
    blob = _git(repo, "hash-object", "obsolete.txt").stdout.strip()

    obsolete.unlink()
    (repo / "current.txt").write_bytes(b"current tree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "current tip")
    head = _git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()

    loose = repo / ".git/objects" / blob[:2] / blob[2:]
    if not loose.is_file():
        raise AssertionError("historical fixture blob must be loose")
    loose.unlink()
    return repo, head, blob
```

The production change that these tests catch is a regression from a current-HEAD shallow clone back to any clone that traverses the missing parent blob, shares the source object store, preserves history, or leaves an attached HEAD.

- [ ] **Step 2: Add attached and detached RED tests**

Add `from unittest.mock import patch`, then add these methods to
`Task13ExactSuiteRunnerTest`:

```python
def _assert_independent_shallow_snapshot(self, clone: Path, head: str) -> None:
    self.assertEqual(_git(clone, "rev-parse", "--verify", "HEAD").stdout.strip(), head)
    self.assertEqual(
        _git(clone, "rev-parse", "--is-shallow-repository").stdout.strip(),
        "true",
    )
    self.assertEqual(_git(clone, "rev-list", "--count", "HEAD").stdout.strip(), "1")
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=clone,
        check=False,
        text=True,
        capture_output=True,
    )
    self.assertNotEqual(symbolic.returncode, 0)
    self.assertFalse((clone / ".git/objects/info/alternates").exists())
    refs = _git(clone, "for-each-ref", "--format=%(refname)").stdout.splitlines()
    self.assertFalse(any(ref.startswith("refs/tags/") for ref in refs))
    for ref in refs:
        self.assertEqual(_git(clone, "rev-parse", ref).stdout.strip(), head)
    object_files = tuple(
        path
        for path in (clone / ".git/objects").rglob("*")
        if path.is_file() and path.parent.name != "info"
    )
    self.assertTrue(object_files)
    self.assertTrue(all(path.stat().st_nlink == 1 for path in object_files))

def test_clone_repo_at_head_is_shallow_and_survives_missing_historical_blob(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, head, missing_blob = _repository_with_missing_parent_blob(base)
        full = subprocess.run(
            ["git", "clone", "--no-local", "--no-tags", repo.as_uri(), str(base / "full")],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(full.returncode, 0)
        self.assertIn(missing_blob, f"{full.stdout}\n{full.stderr}")

        clone = clone_repo_at_head(repo, base / "shallow")
        self._assert_independent_shallow_snapshot(clone, head)

def test_clone_repo_at_head_supports_detached_source_head(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, head, _missing_blob = _repository_with_missing_parent_blob(base)
        _git(repo, "checkout", "--detach", head)

        clone = clone_repo_at_head(repo, base / "detached-shallow")
        self._assert_independent_shallow_snapshot(clone, head)

def test_clone_repo_at_head_fails_closed_when_source_head_changes_during_clone(self) -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, recorded_head, _missing_blob = _repository_with_missing_parent_blob(base)
        original_run = subprocess.run

        def racing_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[:2] == ["git", "clone"]:
                (repo / "new-tip.txt").write_bytes(b"racing tip\n")
                original_run(["git", "add", "new-tip.txt"], cwd=repo, check=True)
                original_run(
                    ["git", "commit", "-qm", "racing tip"], cwd=repo, check=True
                )
            return original_run(args, **kwargs)

        with patch(
            "scripts.run_task13_exact_suite.subprocess.run", side_effect=racing_run
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                clone_repo_at_head(repo, base / "racing-shallow")
        self.assertNotEqual(
            _git(repo, "rev-parse", "--verify", "HEAD").stdout.strip(),
            recorded_head,
        )
```

- [ ] **Step 3: Run RED and confirm the failure is the old clone behavior**

```bash
"$PY311" -m unittest -v \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_clone_repo_at_head_is_shallow_and_survives_missing_historical_blob \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_clone_repo_at_head_supports_detached_source_head \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_clone_repo_at_head_fails_closed_when_source_head_changes_during_clone
```

Expected: at least one assertion fails because the current helper performs a non-shallow `--local --no-hardlinks` clone. The full-clone control must independently fail on the deleted parent-only blob.
The race test must fail closed because the depth-one clone contains only the new tip and
cannot check out the pre-recorded source HEAD.

- [ ] **Step 4: Implement the minimal shallow snapshot clone**

Replace only `clone_repo_at_head()` in `scripts/run_task13_exact_suite.py` with:

```python
def clone_repo_at_head(repo: Path, destination: Path) -> Path:
    repo = Path(repo).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=repo).stdout.strip()
    subprocess.run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-checkout",
            "--no-tags",
            "--single-branch",
            "--depth=1",
            repo.as_uri(),
            str(destination),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    _run_git(["checkout", "--detach", head], cwd=destination)
    checked_out = _run_git(
        ["rev-parse", "--verify", "HEAD"], cwd=destination
    ).stdout.strip()
    if checked_out != head:
        raise RuntimeError("shallow clone did not checkout the source HEAD")
    return destination
```

- [ ] **Step 5: Run GREEN and runner regression tests**

```bash
"$PY311" -m unittest -v tests.test_task13_exact_suite_runner
"$PY311" -m ruff check scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
PYTHONPYCACHEPREFIX=/private/tmp/v52-shallow-pyc \
  "$PY311" -m py_compile \
  scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git diff --check -- scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
```

Expected: all runner tests pass, Ruff and compilation pass, and diff check emits nothing.

- [ ] **Step 6: Commit Task 1**

```bash
git add scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "fix: clone exact suite from current head only"
```

### Task 2: Make source-authority materialization use the same security boundary

**Files:**

- Modify: `tests/test_v52_source_schedule.py`
- Modify: `tests/v52_recovery_schedule_conflict_fixture.py`

- [ ] **Step 1: Add the current-tree-compatible broken-history fixture**

Add `import subprocess` to `tests/test_v52_source_schedule.py`, then add:

```python
def _current_tree_checkout_with_missing_parent_blob(
    base: Path,
) -> tuple[Path, str, str]:
    checkout = base / "current-tree-checkout"
    subprocess.run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-checkout",
            "--no-tags",
            "--single-branch",
            "--depth=1",
            ROOT.as_uri(),
            str(checkout),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "fixture-current", "HEAD"],
        cwd=checkout,
        check=True,
        text=True,
        capture_output=True,
    )
    for key, value in (
        ("user.email", "source-fixture@example.test"),
        ("user.name", "Source Fixture"),
        ("gc.auto", "0"),
    ):
        subprocess.run(
            ["git", "config", key, value], cwd=checkout, check=True
        )

    obsolete = checkout / "historical-only.txt"
    obsolete.write_bytes(b"parent-only\n")
    subprocess.run(["git", "add", "historical-only.txt"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "historical parent"], cwd=checkout, check=True
    )
    blob = subprocess.run(
        ["git", "hash-object", "historical-only.txt"],
        cwd=checkout,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()

    obsolete.unlink()
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "restore current tree"], cwd=checkout, check=True
    )
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=checkout,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    loose = checkout / ".git/objects" / blob[:2] / blob[2:]
    if not loose.is_file():
        raise AssertionError("historical source fixture blob must be loose")
    loose.unlink()
    return checkout, head, blob
```

- [ ] **Step 2: Add the source-authority RED test**

Add beside `test_stale_source_authority_is_materialized_without_mutating_evidence`:

```python
def test_source_authority_materializes_current_tree_without_historical_parent_blob(self) -> None:
    from tests.v52_recovery_schedule_conflict_fixture import (
        matching_source_authority_root,
    )

    evidence = CONFIGURED_SOURCE_AUTHORITY_ROOT
    protected = tuple(
        path
        for path in evidence.glob(
            "data/v5_2_qd/historical/**/mlb_schedule_game_feed/**/*"
        )
        if path.is_file()
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        checkout, head, missing_blob = _current_tree_checkout_with_missing_parent_blob(
            base
        )
        full = subprocess.run(
            ["git", "clone", "--no-local", "--no-tags", checkout.as_uri(), str(base / "full")],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(full.returncode, 0)
        self.assertIn(missing_blob, f"{full.stdout}\n{full.stderr}")

        materialized = matching_source_authority_root(checkout, evidence)
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=materialized,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip(),
            head,
        )
        self.assertEqual(
            subprocess.run(
                ["git", "rev-parse", "--is-shallow-repository"],
                cwd=materialized,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip(),
            "true",
        )
        symbolic = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=materialized,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(symbolic.returncode, 0)
        self.assertFalse((materialized / ".git/objects/info/alternates").exists())
        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)"],
            cwd=materialized,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        self.assertFalse(any(ref.startswith("refs/tags/") for ref in refs))
        for ref in refs:
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", ref],
                    cwd=materialized,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip(),
                head,
            )
        object_files = tuple(
            path
            for path in (materialized / ".git/objects").rglob("*")
            if path.is_file() and path.parent.name != "info"
        )
        self.assertTrue(object_files)
        self.assertTrue(all(path.stat().st_nlink == 1 for path in object_files))
    self.assertEqual(
        before,
        {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected},
    )
```

- [ ] **Step 3: Run RED and confirm upload-pack traverses the parent**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
"$PY311" -m pytest -q \
  tests/test_v52_source_schedule.py \
  -k source_authority_materializes_current_tree_without_historical_parent_blob
```

Expected: collection or the selected test fails because `matching_source_authority_root()` still runs a full `git clone --no-local` and upload-pack tries to read the missing historical blob. The control full clone must fail independently.

- [ ] **Step 4: Implement the minimal fixture clone change**

Replace only the clone block in `matching_source_authority_root()` with the following, retaining its surrounding `try`/cleanup and all subsequent evidence-copy/authority logic:

```python
    head = _git_head(checkout, home=git_home)
    try:
        _git_run(
            [
                "clone",
                "--no-local",
                "--no-checkout",
                "--no-tags",
                "--single-branch",
                "--depth=1",
                "-q",
                checkout.as_uri(),
                str(repository),
            ],
            cwd=Path(temporary.name),
            home=git_home,
        )
        _git_run(
            ["checkout", "--detach", head],
            cwd=repository,
            home=git_home,
        )
        if _git_head(repository, home=git_home) != head:
            raise ParentEvidenceConfigurationError(
                "materialized source authority HEAD differs from the supplied checkout"
            )
```

Do not re-read the source HEAD after cloning. The pre-recorded `head` is the race-resistant authority.

- [ ] **Step 5: Run GREEN and source fixture regressions**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
"$PY311" -m pytest -q \
  tests/test_v52_source_schedule.py \
  -k 'source_authority_materializes_current_tree_without_historical_parent_blob or stale_source_authority_is_materialized_without_mutating_evidence or recovery_authority_fixture_ignores_hostile_git_environment_and_hooks'

"$PY311" -m ruff check \
  tests/v52_recovery_schedule_conflict_fixture.py tests/test_v52_source_schedule.py
PYTHONPYCACHEPREFIX=/private/tmp/v52-shallow-pyc \
  "$PY311" -m py_compile \
  tests/v52_recovery_schedule_conflict_fixture.py tests/test_v52_source_schedule.py
git diff --check -- \
  tests/v52_recovery_schedule_conflict_fixture.py tests/test_v52_source_schedule.py
```

Expected: selected tests and static checks pass; immutable evidence hashes remain unchanged.

- [ ] **Step 6: Commit Task 2**

```bash
git add tests/v52_recovery_schedule_conflict_fixture.py tests/test_v52_source_schedule.py
git commit -m "fix: materialize source authority from current head"
```

### Task 3: Close compatibility, review, and frozen exact-suite gates

**Files:**

- Verify: `scripts/run_task13_exact_suite.py`
- Verify: `tests/v52_recovery_schedule_conflict_fixture.py`
- Verify: `tests/test_task13_exact_suite_runner.py`
- Verify: `tests/test_v52_source_schedule.py`
- No additional implementation files unless a P0-P2 review finding has a new RED test.

- [ ] **Step 1: Run the focused compatibility modules that previously failed during clone**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
"$PY311" -m pytest -q \
  tests/test_v52_acquire_e2e.py \
  tests/test_v52_verify_sources.py \
  tests/test_v52_game_evidence_inventory.py
```

Expected: all three modules collect and execute; there is no `git-pack-objects`, missing `23f74f...`, early EOF, or invalid index-pack error. Any product assertion failure is handled as a new independent TDD root cause, not classified as clone recovery.

- [ ] **Step 2: Run all directly affected tests and static checks**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
"$PY311" -m pytest -q \
  tests/test_task13_exact_suite_runner.py \
  tests/test_v52_source_schedule.py

"$PY311" -m ruff check \
  scripts/run_task13_exact_suite.py \
  tests/v52_recovery_schedule_conflict_fixture.py \
  tests/test_task13_exact_suite_runner.py \
  tests/test_v52_source_schedule.py

PYTHONPYCACHEPREFIX=/private/tmp/v52-shallow-pyc \
  "$PY311" -m py_compile \
  scripts/run_task13_exact_suite.py \
  tests/v52_recovery_schedule_conflict_fixture.py \
  tests/test_task13_exact_suite_runner.py \
  tests/test_v52_source_schedule.py

git diff --check -- \
  scripts/run_task13_exact_suite.py \
  tests/v52_recovery_schedule_conflict_fixture.py \
  tests/test_task13_exact_suite_runner.py \
  tests/test_v52_source_schedule.py
```

Expected: zero failures/errors and clean static output.

- [ ] **Step 3: Obtain independent reviews**

Require:

1. a spec-compliance review against `docs/superpowers/specs/2026-08-03-task13-current-head-shallow-clone-design.md`; and
2. a code-quality/security review covering Git object independence, source-HEAD races, detached HEAD, isolated fixture Git `HOME`, evidence immutability, and test strength.

Resolve every P0-P2 finding with a named RED test, minimal GREEN change, and re-review. Both final verdicts must be `APPROVED` with no P0-P2 findings.

- [ ] **Step 4: Commit any review fixes and freeze the reviewed HEAD**

```bash
git status --short
git log --oneline --decorate -8
EXACT_ROOT="/private/tmp/v52-external-parent-exact-$(git rev-parse --short=12 HEAD)"
test ! -e "$EXACT_ROOT"
git worktree add --detach "$EXACT_ROOT" HEAD
test "$(git -C "$EXACT_ROOT" rev-parse HEAD)" = "$(git rev-parse HEAD)"
```

Expected: implementation worktree clean and detached worktree HEAD exactly equals the reviewed commit. If FileProvider blocks ordinary Git status or commit operations, use the repository's already-established verified external-object pack procedure; do not restore disabled packs or rewrite refs other than advancing this feature branch.

- [ ] **Step 5: Revalidate the recovered v5.1 exact input**

```bash
test -d "$V51_INPUT"
PYTHONPATH="$CODE_ROOT" "$PY311" - <<'PY'
from pathlib import Path
from mlb_backtest.verify_v52_design import load_v51_evidence

root = Path("/private/tmp/v51-artifact-recovery.1CItVL/recovery-root")
evidence = load_v51_evidence(root)
assert len(evidence.evaluated_rows) == 1340
assert len(evidence.candidate_rows) == 1972
print("v5.1 exact input: 16/16, evaluated=1340, candidates=1972")
PY
```

Expected: the previously regenerated locked-hash root authenticates without reading the three dataless parent placeholders.

- [ ] **Step 6: Run the canonical exact suite once on the frozen HEAD**

```bash
EXACT_ROOT="/private/tmp/v52-external-parent-exact-$(git rev-parse --short=12 HEAD)"
cd "$EXACT_ROOT"
MLB_V52_SOURCE_AUTHORITY_ROOT="$PARENT" \
PYTHONDONTWRITEBYTECODE=1 \
"$PY311" scripts/run_task13_exact_suite.py \
  --repo "$EXACT_ROOT" \
  --v51-input-root "$V51_INPUT" \
  --old-v52-evidence-root "$PARENT"
```

Expected: exit code 0; zero failures/errors; only documented skips; Ruff/diff checks green; v5.1 16/16 input, old evidence, recovery parent trees, and original preflight hashes unchanged. The opt-in real-parent slow test remains a separate gate because the current physical parent no longer matches the contract-bound device/inode inventory.

- [ ] **Step 7: Record Task 6 closure or the next exact blocker**

If the exact suite is green, record the frozen HEAD, total passed/skipped counts, static results, and before/after evidence hashes, then mark the shallow-clone Task 6 work complete. If it fails, capture the first independent traceback and classify it as environment, fixture, or product before making any change.
