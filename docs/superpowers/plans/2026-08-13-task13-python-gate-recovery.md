# Task 13 Python Gate Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Python execution registry's R4 bound-code gate pass with exactly four reachable R4 modules while preserving fail-closed authority and evidence handling.

**Architecture:** Extend only `mlb_backtest.execution_registry._BOUND_EXECUTION_CODE` with the four approved R4 modules. The exact closure tests remain the source of truth; no issuer module or snapshot producer is added. Verification consumes only a user-authenticated evidence root and otherwise reports blocked without creating substitute evidence.

**Tech Stack:** Python 3.11, `unittest`, Git fixed-command helpers, Ruff, Swift Package Manager/XCTest.

---

## Global Constraints

- The exact R4 additions are `mlb_backtest/v52_r4_apfs.py`, `mlb_backtest/v52_r4_authority.py`, `mlb_backtest/v52_r4_offline_signer.py`, and `mlb_backtest/v52_r4_production_public_key.py`.
- Do not add `mlb_backtest/v52_r4_snapshot_issuer.py`, any snapshot issuer, or any other non-reachable R4 file.
- Do not synthesize, copy, or commit external evidence. A verification run may use an evidence root only when the user has authenticated and supplied that exact root; with no such root, report `BLOCKED` and stop before the runner.
- Keep incomplete R4 authority fail-closed. Do not weaken APFS, key, signature, source-authority, immutable-parent, or policy checks.
- Never access `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4`, `/Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock`, `/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2`, or `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`.
- Never delete `/tmp/r4-readonly-tree-manifest.txt`.
- Any other evidence root requires explicit user authentication before access; absent that authentication, verification reports `BLOCKED` and performs no substitute provisioning.
- Do not access or create recovery parent trees, custody snapshots, issuer outputs, Swift source/authority implementation, generated evidence, or approval envelopes as part of this docs-only recovery.
- Preserve fixed-Git behavior and the existing exact-runner no-mutation guarantees.

### Task 1: Repair the registry boundary with TDD

**Files:**
- Modify: `mlb_backtest/execution_registry.py` (the `_BOUND_EXECUTION_CODE` tuple only)
- Test: `tests/test_execution_registry.py` (use existing exact tests; add no broader production selector)

- [ ] **Step 1: Run the RED registry contract.**

Run:

```bash
python3 -m unittest -v \
  tests.test_execution_registry.ExecutionRegistryTest.test_task7d_runtime_authority_is_exactly_in_production_bound_code \
  tests.test_execution_registry.ExecutionRegistryTest.test_bound_execution_import_closure_is_computed_and_exact
```

Expected: failure identifying the four reachable R4 modules as absent from `_BOUND_EXECUTION_CODE`.

- [ ] **Step 2: Add exactly the four approved paths.**

Append only these strings to `_BOUND_EXECUTION_CODE`, in stable lexical order with the surrounding R4 entries:

```python
    "mlb_backtest/v52_r4_apfs.py",
    "mlb_backtest/v52_r4_authority.py",
    "mlb_backtest/v52_r4_offline_signer.py",
    "mlb_backtest/v52_r4_production_public_key.py",
```

Do not add `v52_r4_snapshot_issuer.py`; do not change closure computation, schema membership, or any authority selector.

- [ ] **Step 3: Run the complete focused registry contract.**

Run:

```bash
python3 -m unittest -v \
  tests.test_execution_registry.ExecutionRegistryTest.test_task7d_runtime_authority_is_exactly_in_production_bound_code \
  tests.test_execution_registry.ExecutionRegistryTest.test_bound_execution_import_closure_is_computed_and_exact \
  tests.test_execution_registry.ExecutionRegistryTest.test_rejects_dirty_bound_execution_code_but_not_unrelated_files \
  tests.test_execution_registry.ExecutionRegistryTest.test_task7c_bound_runtime_rejects_dirty_initialize_and_load \
  tests.test_execution_registry.ExecutionRegistryTest.test_task7d_timezone_runtime_rejects_dirty_initialize_and_load \
  tests.test_execution_registry.ExecutionRegistryTest.test_rejects_committed_bound_code_head_drift_before_checkpoint \
  tests.test_execution_registry.ExecutionRegistryTest.test_rejects_dirty_bound_input_and_identity_race
```

Expected: all selected tests pass; the unrelated-file case remains accepted while every bound-file mutation is rejected.

- [ ] **Step 4: Check the diff and Python quality.**

Run:

```bash
ruff check mlb_backtest/execution_registry.py tests/test_execution_registry.py
git diff --check
git diff -- mlb_backtest/execution_registry.py tests/test_execution_registry.py
```

Expected: Ruff and whitespace checks pass; the diff contains only the four tuple entries and any narrowly necessary test assertion text.

### Task 2: Verify with authenticated evidence or report blocked

**Files:**
- Verify only: `scripts/run_task13_exact_suite.py`, `scripts/r4_protected_fixed_git.py`, `tests/test_r4_protected_fixed_git.py`, `tools/r4_protected_execution_authority/`
- Do not create or modify evidence, snapshots, approval envelopes, or protected inputs.

- [ ] **Step 1: Establish the evidence precondition.**

Proceed only if the user supplied an authenticated evidence root and its expected fixed hashes are independently available. If that root is absent or cannot be authenticated, record `BLOCKED: authenticated evidence root not supplied` and do not invoke the exact runner.

- [ ] **Step 2: Verify fixed-Git behavior.**

Run:

```bash
python3 -m unittest -v tests.test_r4_protected_fixed_git
```

Expected: fixed executable/Git argument grammar and equality checks pass without reading or creating external evidence.

- [ ] **Step 3: Run focused Swift preservation tests.**

On macOS with the existing package, run the focused preservation set and then the full package:

```bash
swift test --package-path tools/r4_protected_execution_authority \
  --filter 'KeyringTests|MaterializeTests|VerifyTests|StateMachineTests|StorageTests|ProcessRunnerTests'
swift test --package-path tools/r4_protected_execution_authority
```

Expected: both focused and full Swift suites remain green; no source or authority files are changed by this plan.

- [ ] **Step 4: Run the exact runner only with authenticated roots.**

Use the existing `scripts/run_task13_exact_suite.py` command with the user-authenticated `--v51-input-root` and `--old-v52-evidence-root`, plus the already pinned Chromium arguments. Require exit code 0, zero failures/errors, expected skips, unchanged fixed-Git inputs, unchanged parent-tree fingerprints, and unchanged v5.1 preservation hashes. If any authenticated input is missing, report `BLOCKED` instead of substituting a fixture.

- [ ] **Step 5: Re-run the full Python suite and final checks.**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
ruff check mlb_backtest/execution_registry.py
git diff --check
git status --short
```

Expected: the full Python suite and Ruff pass; `git diff --check` is clean; only the intended registry/docs changes are present. Do not claim completion if the authenticated-evidence gate was blocked.

## Self-review checklist

- [ ] Search both documents for unresolved-task markers and ambiguity; none may remain.
- [ ] Confirm the plan names every required exact registry test and both dirty-bound initialize/load tests.
- [ ] Confirm no step asks for evidence synthesis, copying, committing, snapshot issuance, or policy weakening.
- [ ] Confirm the four-file list is identical in the design and plan and excludes the snapshot issuer.
- [ ] Run `git diff --check` after writing the documents.
