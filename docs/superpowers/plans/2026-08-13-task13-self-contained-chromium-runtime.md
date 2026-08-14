# Task 13 Self-Contained Chromium Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject macOS `.app` Chromium launchers at runner admission while preserving the authenticated, complete-parent self-contained runtime snapshot contract and allowing the five portable-verifier tests to run against a real authenticated runtime.

**Architecture:** Keep the existing CLI triple (`--chromium-executable`, `--chromium-sha256`, `--chromium-bundle-sha256`) and descriptor-based parent-directory manifest/snapshot implementation. Add one path-shape guard immediately after absolute no-symlink path admission and before executable metadata or hashing; `.app` is an explicit unsupported input, while ordinary self-contained directories continue through the existing scanner, snapshot validator, environment isolation, and cleanup boundaries.

**Tech Stack:** Python 3 (`pathlib`, `unittest.mock`, descriptor-safe `os` helpers), unittest, Node portable verifier, Ruff, Git.

---

## Files and ownership

- Modify: `scripts/run_task13_exact_suite.py:197-272` — add the stable `.app` admission constant/helper and call it from `validate_chromium_runtime` before `_chromium_metadata`/`_sha256_chromium_executable`; do not alter the manifest format, snapshot copier, environment override removal, CLI names, or cleanup owner.
- Modify: `tests/test_task13_exact_suite_runner.py:105-115,309-387` — add `.app` fixtures and red/green admission tests, including nested/case-insensitive names and no child/provision side effect. Keep the existing source-swap, companion-tamper, snapshot-tamper, environment-isolation, and descriptor regression tests unchanged unless a focused run identifies a real gap.
- Do not modify: `tests/test_verify_v52_design.py` unless the real self-contained exact run proves one of the five browser-related tests needs an assertion change. Its existing browser-failure, exit-integrity, final-scope, CLI, and no-network/no-write assertions are the integration oracle; structural `.app` admission belongs in the runner tests.
- No production model, report, schema, evidence, custody, authority, or external-runtime lookup files are in scope.

### Task 1: Add fail-closed `.app` admission (TDD)

**Files:**
- Modify: `tests/test_task13_exact_suite_runner.py:105-115,309-387`
- Modify: `scripts/run_task13_exact_suite.py:197-272`

- [ ] **Step 1: Add a fixture helper and red tests for unsupported application bundles.**

Add this helper beside `_safe_chromium`/`_safe_chromium_bundle`:

```python
def _safe_chromium_app(
    base: Path,
    *,
    app_name: str = "Google Chrome.app",
    executable_name: str = "Google Chrome",
) -> tuple[Path, str]:
    launcher = base / app_name / "Contents" / "MacOS" / executable_name
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"test app launcher\n")
    launcher.chmod(0o700)
    return launcher, hashlib.sha256(launcher.read_bytes()).hexdigest()
```

Add this test immediately after `test_chromium_runtime_requires_a_matching_explicit_pair`:

```python
def test_chromium_app_launchers_reject_before_hashing_or_snapshot_creation(self) -> None:
    import scripts.run_task13_exact_suite as runner_module

    with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
        base = Path(directory)
        candidates = (
            _safe_chromium_app(base, app_name="Google Chrome.app"),
            _safe_chromium_app(base, app_name="Outer.APP/Contents/Framework.app"),
        )
        for launcher, digest in candidates:
            with self.subTest(launcher=launcher):
                runtime_root = base / f"runtime-{len(launcher.parts)}"
                with (
                    patch.object(
                        runner_module,
                        "_chromium_metadata",
                        side_effect=AssertionError("metadata must not be read"),
                    ) as metadata,
                    patch.object(
                        runner_module,
                        "_sha256_chromium_executable",
                        side_effect=AssertionError("executable must not be hashed"),
                    ) as executable_hash,
                    self.assertRaisesRegex(
                        ValueError,
                        r"^chromium \.app bundle launchers are unsupported; provide a self-contained runtime directory$",
                    ),
                ):
                    validate_chromium_runtime(launcher, digest)
                metadata.assert_not_called()
                executable_hash.assert_not_called()
                with self.assertRaisesRegex(
                    ValueError,
                    r"^chromium \.app bundle launchers are unsupported; provide a self-contained runtime directory$",
                ):
                    provision_chromium_runtime(
                        launcher,
                        digest,
                        "0" * 64,
                        runtime_root,
                    )
                self.assertFalse(runtime_root.exists())

        ordinary, ordinary_digest = _safe_chromium(base / "self-contained")
        self.assertEqual(validate_chromium_runtime(ordinary, ordinary_digest), ordinary.resolve())
```

The second fixture intentionally places a case-insensitive nested `.APP`/`.app` component in the lexical path. The final ordinary-directory assertion proves the new guard does not reject the accepted self-contained contract.

- [ ] **Step 2: Run only the new test and verify the expected red failure.**

Run:

```bash
python -m unittest tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_app_launchers_reject_before_hashing_or_snapshot_creation -v
```

Expected before implementation: `FAIL` because the current validator proceeds to `_chromium_metadata` and does not emit the stable unsupported-input error.

- [ ] **Step 3: Implement the minimal path guard before executable metadata/hash operations.**

In `scripts/run_task13_exact_suite.py`, immediately after `_absolute_path_without_symlink` and before the `before = _chromium_fingerprint(_chromium_metadata(executable))` line, add:

```python
_CHROMIUM_APP_ERROR = (
    "chromium .app bundle launchers are unsupported; "
    "provide a self-contained runtime directory"
)


def _reject_chromium_app_bundle(path: Path) -> None:
    if any(component.lower().endswith(".app") for component in path.parts):
        raise ValueError(_CHROMIUM_APP_ERROR)
```

Update `validate_chromium_runtime` as follows; all pair and lowercase-digest checks remain unchanged:

```python
    executable = _absolute_path_without_symlink(
        Path(chromium_executable), "chromium executable"
    )
    _reject_chromium_app_bundle(executable)
    before = _chromium_fingerprint(_chromium_metadata(executable))
```

This checks every absolute path component case-insensitively, including nested bundles, and raises a path-independent message before executable metadata, content hashing, bundle scanning, snapshot creation, checkout cloning, or child launch.

- [ ] **Step 4: Run the new test and the existing Chromium admission/security slice.**

Run:

```bash
python -m unittest \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_app_launchers_reject_before_hashing_or_snapshot_creation \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_runtime_requires_a_matching_explicit_pair \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_runtime_rejects_unsafe_or_mismatched_input \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_runtime_rejects_ancestor_swap_after_hash \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_provisioned_chromium_snapshot_survives_source_swap \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_child_environment_rejects_tampered_chromium_snapshot \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_bundle_rejects_manifest_mutation_and_unsafe_members \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_bundle_fd_survives_source_ancestor_swap_after_open \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_child_environment_rejects_tampered_chromium_bundle_companion -v
```

Expected: all selected tests `OK`; the ordinary self-contained fixture still validates and snapshots, while every app fixture fails with the exact stable error and leaves no runtime directory.

- [ ] **Step 5: Commit only the runner and runner-test changes.**

```bash
git add scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "fix: reject chromium app bundle launchers"
```

### Task 2: Audit snapshot, environment, and cleanup boundaries without production churn

**Files:**
- Test only: `tests/test_task13_exact_suite_runner.py:389-526,764-826`
- No source change expected in `scripts/run_task13_exact_suite.py` beyond Task 1.

- [ ] **Step 1: Confirm existing regression coverage maps to the approved security contract.**

Run the exact existing tests covering source-ancestor swap, snapshot executable/companion tamper, hostile environment override removal, and child execution:

```bash
python -m unittest \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_provisioned_chromium_snapshot_survives_source_swap \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_child_environment_rejects_tampered_chromium_snapshot \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_child_environment_rejects_tampered_chromium_bundle_companion \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_chromium_bundle_fd_survives_source_ancestor_swap_after_open \
  tests.test_task13_exact_suite_runner.Task13ExactSuiteRunnerTest.test_child_command_and_environment_execute_real_child -v
```

Expected: `OK`; `CHROMIUM_EXECUTABLE_PATH` points to the runner-owned snapshot, all caller values for `CHROMIUM_EXECUTABLE_PATH`, `PLAYWRIGHT_EXECUTABLE_PATH`, and `PLAYWRIGHT_BROWSERS_PATH` are removed/replaced, and mutations fail with `ValueError` rather than falling back to another browser.

- [ ] **Step 2: Verify the `.app` red/green test already covers the missing admission-side-effect boundary.**

Do not add a second copier or a new cleanup mechanism. The test in Task 1 asserts that a rejected `.app` never creates the runner-owned runtime root; the existing `TemporaryDirectory(prefix="task13-exact-")` owner in `run_exact_suite` remains responsible for cleanup after a valid child run. If all tests in Step 1 pass, leave `tests/test_verify_v52_design.py` untouched and record that no production churn is justified.

- [ ] **Step 3: Commit only if a concrete regression test was added.**

The expected result is no additional diff and therefore no second commit. If (and only if) a focused test exposes a missing assertion that does not require production behavior changes, add that single test to `tests/test_task13_exact_suite_runner.py`, rerun the affected test, and commit it separately:

```bash
git add tests/test_task13_exact_suite_runner.py
git commit -m "test: cover chromium snapshot boundary"
```

### Task 3: Exercise portable-verifier integration with a real self-contained runtime

**Files:**
- Test execution: existing five tests in `tests/test_verify_v52_design.py` (no source edit planned)
- Runner integration: `scripts/run_task13_exact_suite.py` and `tests/test_task13_exact_suite_runner.py` from Task 1

- [ ] **Step 1: Keep the existing browser-failure semantics test as the negative oracle.**

Run the existing test that supplies a genuine child failure payload:

```bash
python -m unittest tests.test_verify_v52_design.DesignVerificationTest.test_html_verifier_reports_stderr_when_node_fails_before_json_parse -v
```

Expected: `OK` with `portable HTML verification failed: ... browser_failed`; do not replace this with a mocked success or reinterpret a real child exit as structural admission success.

- [ ] **Step 2: Run all five previously masked/erroring tests through the exact runner’s authenticated child environment.**

Use the exact runner (not direct test invocation with a host browser) so `provision_chromium_runtime` snapshots the supplied self-contained directory and `build_child_environment` points the child only at that snapshot. The five names to inspect in verbose output are:

```text
tests.test_verify_v52_design.DesignVerificationTest.test_exit_integrity_recheck_uses_mock_without_touching_sealed_files
tests.test_verify_v52_design.DesignVerificationTest.test_final_boundary_rechecks_scope_and_forbidden_namespaces
tests.test_verify_v52_design.DesignVerificationTest.test_current_deliverables_cross_validate
tests.test_verify_v52_design.DesignVerificationTest.test_main_and_module_cli_success_output_and_failure
tests.test_verify_v52_design.DesignVerificationTest.test_verifier_uses_no_network_or_python_write_api
```

Expected: these tests reach their intended assertions with the authenticated runtime; no `.app` path reaches the child, and any genuine browser process failure remains `browser_failed`. Do not add a test-only browser stub or modify verifier assertions merely to hide a runtime failure.

- [ ] **Step 3: Decide the verifier-test diff using the observed result, with YAGNI as the default.**

If the five tests pass under the self-contained runtime, make no edit to `tests/test_verify_v52_design.py`: the structural admission assertion is already isolated in `tests/test_task13_exact_suite_runner.py`, and the existing verifier tests remain the intended behavior checks. Only if a failing test demonstrates an untested contract (not an unavailable runtime) add one narrowly scoped assertion to the relevant existing test, then rerun that test and commit just that test change:

```bash
git add tests/test_verify_v52_design.py
git commit -m "test: cover portable verifier runtime boundary"
```

### Task 4: Validate the documented runtime authority directly, without discovery

**Files:**
- No repository source changes.
- External input read only at the exact documented candidate path below.

- [ ] **Step 1: Validate the user-approved candidate path and both digests through runner helpers.**

Run this direct check; it must not scan cache directories or select another executable:

```bash
python - <<'PY'
from pathlib import Path
from scripts.run_task13_exact_suite import chromium_bundle_sha256, validate_chromium_runtime

candidate = Path("/Users/taejunoh/.cache/puppeteer/chrome-headless-shell/mac_arm-146.0.7680.153/chrome-headless-shell-mac-arm64/chrome-headless-shell")
executable_sha256 = "454577a889a2c0b698c09f0ca8db047ea2e6cd604a42c36dc09b375c81362518"
bundle_sha256 = "2ea5035c2085aaf0fb70be6d2949f51eeb42affaac47772044ddaa727cb4c253"

assert validate_chromium_runtime(candidate, executable_sha256) == candidate
assert chromium_bundle_sha256(candidate.parent) == bundle_sha256
print("authenticated self-contained Chromium candidate", candidate)
PY
```

Expected with the supplied runtime: the two assertions pass. If the exact path is absent, a symlink/unsafe member is observed, or either digest differs, the command must fail closed; report that exact reason and continue Tasks 1–3 without scanning, copying, synthesizing, or substituting a runtime. The documented values are claims until this held-descriptor validation succeeds.

- [ ] **Step 2: Confirm no forbidden custody/authority roots were touched.**

Review the command history and diff to ensure only the documented candidate path was read, and do not access the four forbidden custody roots or any manifest outside the approved evidence roots.

### Task 5: Focused quality gates and authenticated exact run

**Files:**
- Verification only; final diff must contain only the Task 1 runner/test changes and any explicitly justified Task 3 test assertion.

- [ ] **Step 1: Run focused tests, Ruff, and whitespace validation.**

```bash
python -m unittest tests.test_task13_exact_suite_runner -v
python -m unittest tests.test_verify_v52_design -v
ruff check scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py tests/test_verify_v52_design.py
git diff --check
git status --short
```

Expected: both unittest modules pass, Ruff reports no violations, `git diff --check` is silent, and `git status` shows only the documented implementation/test files (plus no unrelated edits staged by this work).

- [ ] **Step 2: Run the exact selected gate only after Task 4 succeeds.**

Use the approved evidence root for both evidence flags and the validated runtime triple; do not substitute another root:

```bash
python scripts/run_task13_exact_suite.py \
  --repo "/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2" \
  --v51-input-root "/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest" \
  --old-v52-evidence-root "/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest" \
  --chromium-executable "/Users/taejunoh/.cache/puppeteer/chrome-headless-shell/mac_arm-146.0.7680.153/chrome-headless-shell-mac-arm64/chrome-headless-shell" \
  --chromium-sha256 "454577a889a2c0b698c09f0ca8db047ea2e6cd604a42c36dc09b375c81362518" \
  --chromium-bundle-sha256 "2ea5035c2085aaf0fb70be6d2949f51eeb42affaac47772044ddaa727cb4c253" \
  --python "python3"
```

Expected: the long-running exact gate reports zero failures and zero errors, with the two expected skips and the current selected count of 1,480. Retain the full verbose log; if test selection changes, report the actual count and why.

- [ ] **Step 3: Recheck scope, hashes, and cleanup before completion.**

Confirm the runner’s existing post-child checks passed for fixed preflight files, v5.1 inputs, copied old-v5.2 evidence, source/preflight hashes, parent-tree fingerprints, and Chromium snapshot validation. Confirm the temporary runtime directory is gone after the run and that no forbidden custody/authority path was accessed or modified.

- [ ] **Step 4: Perform plan self-review and final diff review.**

Check every requirement in `docs/superpowers/specs/2026-08-13-task13-self-contained-chromium-runtime-design.md` against Tasks 1–5, search this plan for `TBD`, `TODO`, `FIXME`, or vague “appropriate”/“as needed” instructions, and verify helper names/signatures (`_reject_chromium_app_bundle`, `validate_chromium_runtime`, `provision_chromium_runtime`, `validate_chromium_snapshot`, `build_child_environment`) are consistent. Run `git diff --check` once more and ensure the final diff is limited to the documented boundaries.
