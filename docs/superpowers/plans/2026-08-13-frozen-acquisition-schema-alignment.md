# Frozen Acquisition-Schema Test Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the five acquisition-schema test/fixture failures with the already-frozen production schema and fail-closed authority model, while adding exact R2/R3/R4 successor checks and a selected-branch rewrite regression.

**Architecture:** Keep production schemas, contracts, verifiers, evidence, and custody immutable. Rework only the test seams so legacy/source-batch inputs use the existing path-bound in-memory capability (`read_bytes()` plus `rebind()`), absent context is represented by `None`, and successor selection is keyed by exact run ID, authority paths, hashes, and batch-root constants.

**Tech Stack:** Python 3, `unittest`, `pytest`, `jsonschema`, `ruff`, temporary Git repositories, and the existing `ExternalProtectedParentCapability`/descriptor-bound fixture helpers.

---

## File and responsibility map

Only these test files may change:

- `tests/test_execution_registry.py:1589-1686` — `ExecutionRegistryTest.make_repo`; remove the fixture-only `remaining_authority` schema rewrite and expose only the existing bound-capability seam. `remaining_checkpoint_fixture` at `4702-4770` must request an explicit legacy capability when it needs one; no boolean may rewrite `schemas/v5_2_acquisition_contract.schema.json`.
- `tests/test_v52_legacy_decision_inventory.py:366-400` — `_superseding_legacy_decision_repository`; stop mutating `oneOf[1].const`, and use `_split_legacy_decision_capabilities` plus `_verified_legacy_decision_authority` for the positive external-legacy case. `V52LegacyDecisionInventoryTest.test_superseding_pair_authenticates_then_requires_source_batch` at `410-433` remains the fail-closed consumer.
- `tests/test_v52_query_plan.py:1177-1230` — `V52QueryPlanTest.test_external_source_authority_ignores_unselected_legacy_schema_suffix`; retain the unrelated legacy-suffix mutation as a negative control and add selected-branch mutation coverage. `V52QueryPlanTest.test_r4_static_batch_uses_its_own_authority_hash_pair` at `1236-1283` is the successor-specific batch-root/hash regression.
- `tests/test_v52_source_finalize.py:2306-2425` — `FinalizationEndToEndTests.build_finalization_fixture`; construct the same fixture with an explicitly bound capability instead of `remaining_authority=True`. `test_absent_status_context_is_read_only` at `3558-3622` must use no capability and assert the production absent-context blocker.
- `tests/test_verify_v52_design.py:99-339` — `DesignVerificationTest.test_all_recovery_r2_schema_branches_are_exact`; replace the R2/R3 substring assertion with an exact R2/R3/R4 identity table and exact branch/path/hash checks.

Do not edit `mlb_backtest/**`, `schemas/**`, `protocols/**`, `audit/**`, `data/**`, Chromium assets/tests, ledger/evidence files, or any forbidden custody/authority path. Do not read, write, delete, or stat `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4`, `/Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock`, `/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2`, `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`, or `/tmp/r4-readonly-tree-manifest.txt`.

### Task 1: Lock the fixture seam contract with failing tests

**Files:**
- Modify: `tests/test_execution_registry.py:1589-1686,4702-4770`
- Modify: `tests/test_v52_source_finalize.py:2306-2425,3558-3622`
- Test: existing `test_absent_status_context_is_read_only`, `test_source_remaining_checkpoint_uses_bounded_population_and_authority`, and the source-finalization fixture consumers.

- [ ] **Step 1: Write the RED assertions before changing helpers.** In `test_absent_status_context_is_read_only`, call `fixture.make_repo(Path(directory))` with no keyword and assert `build_context(CommonArgs(root=root)).phase == "ABSENT"`, `context.registry is None`, `context.checkpoint is None`, and one blocker has code `"REGISTRY_ABSENT"`; do not create a protected raw catalog. In the positive remaining-checkpoint helper, add an explicit capability argument at the call site and assert the downstream registry API receives that exact object and its `rebind` callback.

  ```python
  root, _run_spec = fixture.make_repo(Path(directory))
  context = orchestration.build_context(orchestration.CommonArgs(root=root))
  self.assertEqual(context.phase, "ABSENT")
  self.assertIsNone(context.registry)
  self.assertIsNone(context.checkpoint)
  self.assertIn("REGISTRY_ABSENT", {item.code for item in context.blockers})
  ```

- [ ] **Step 2: Run the focused RED tests.**

  Run: `python3 -m pytest -q tests/test_execution_registry.py -k 'remaining_checkpoint or schema or fixture' tests/test_v52_source_finalize.py -k 'absent_status_context or finalization'`

  Expected: the pre-change fixture path either raises at the strict acquisition-schema validation boundary or fails the new capability identity assertion; no production module is changed.

- [ ] **Step 3: Commit the RED test-only contract.**

  ```bash
  git add tests/test_execution_registry.py tests/test_v52_source_finalize.py
  git commit -m "test: pin absent acquisition context seam"
  ```

### Task 2: Remove `remaining_authority` and make legacy/source capabilities explicit

**Files:**
- Modify: `tests/test_execution_registry.py:1589-1686,4702-4770`
- Modify: `tests/test_v52_legacy_decision_inventory.py:141-220,366-400,410-433`
- Modify: `tests/test_v52_source_finalize.py:2306-2425,3558-3622`

- [ ] **Step 1: Delete the fixture-only schema rewrite.** Change `ExecutionRegistryTest.make_repo` to have no `remaining_authority` parameter. Remove the `if remaining_authority:` blocks at lines `1620-1649` and `1679-1686`, including the `acquisition_schema["oneOf"][0]["const"] = template` assignment. The helper must still copy the canonical checked-in schema unchanged and commit only the test contract binding.

  The resulting signature is exactly `def make_repo(self, parent: Path) -> tuple[Path, object]:`; preserve the current copy/commit statements for `RUN_SPEC_RELATIVE`, `CONTRACT_RELATIVE`, `ACQUISITION_SCHEMA_RELATIVE`, and the bound paths, and remove only the two `remaining_authority` blocks. Do not introduce a generic loader or trust bypass.

- [ ] **Step 2: Bind synthetic legacy evidence only through the existing path-bound capability.** In `_superseding_legacy_decision_repository`, remove lines `386-389` that assign `schema["oneOf"][1]["const"] = source_contract` and write the schema. Keep the temporary contract bytes local, then obtain the capability from `_split_legacy_decision_capabilities(parent)` and route the positive test through `_verified_legacy_decision_authority`. The capability must remain bound to `data/v5_1_bt/raw`, the target protected-inventory path, the descriptor identities, `read_bytes()`, and `rebind()`; do not patch `load_verified_legacy_decision_inventory` globally.

  The positive call must retain this exact production-facing shape:

  ```python
  with _verified_legacy_decision_authority(target, digest):
      payload = build_legacy_decision_inventory(
          target,
          legacy_evidence=external,
      )
  ```

  The absent case must pass `legacy_evidence=None` (or omit the keyword), and the expected error must match the production authority/input failure rather than a synthetic `remaining_authority` result.

- [ ] **Step 3: Update remaining-checkpoint and source-finalization callers.** Replace both `fixture.make_repo(parent, remaining_authority=True)` calls (`tests/test_execution_registry.py:4715` and `tests/test_v52_source_finalize.py:2315`) with `fixture.make_repo(parent)`. Where the test genuinely needs legacy raw bytes, construct an `ExternalProtectedParentCapability` with `_bind_external_inventory` using the temporary root and exact inventory snapshot, pass it as `legacy_evidence`, and register `capability.close` as cleanup. Where the test is checking absence (`tests/test_v52_source_finalize.py:3564`), do not create or request a capability at all.

- [ ] **Step 4: Run the GREEN capability and absence suites.**

  Run: `python3 -m pytest -q tests/test_execution_registry.py -k 'remaining_checkpoint or schema or fixture' tests/test_v52_legacy_decision_inventory.py -k 'superseding or external or authority' tests/test_v52_source_finalize.py -k 'absent_status_context or finalization'`

  Expected: all selected tests pass; no test writes `schemas/v5_2_acquisition_contract.schema.json` after fixture commit, and absent context reports `REGISTRY_ABSENT` without asking for `remaining_authority`.

- [ ] **Step 5: Commit the seam change.**

  ```bash
  git add tests/test_execution_registry.py tests/test_v52_legacy_decision_inventory.py tests/test_v52_source_finalize.py
  git commit -m "test: bind legacy fixtures through capability seam"
  ```

### Task 3: Add selected-branch rewrite rejection and preserve unselected-suffix negative control

**Files:**
- Modify: `tests/test_v52_query_plan.py:1177-1230`
- Test: `V52QueryPlanTest.test_external_source_authority_ignores_unselected_legacy_schema_suffix` and new selected-branch regression beside it.

- [ ] **Step 1: Add the RED regression against the selected branch.** Build a canonical `_statcast_authority_fixture`, load its committed `schemas/v5_2_acquisition_contract.schema.json`, and first prove `_load_static_contracts(source_run, source_contract)` succeeds. Then mutate the selected source branch (`oneOf[1]["const"]["run_id"] = "forged-successor"`), commit that schema through `_commit_acquisition_schema_variants`, and assert `_load_static_contracts` raises `ValueError` matching `"frozen schema"`. Also mutate only the unselected legacy branch suffix (`oneOf[0]["const"]["legacy_protection"]["protected_inventory_sha256"] = "f" * 64`) and assert the same selected source load still fails. The untouched canonical fixture must remain accepted.

  ```python
  canonical = json.loads(schema_path.read_bytes())["oneOf"]
  query_plan._load_static_contracts(source_run, source_contract)
  selected = copy.deepcopy(canonical)
  selected[1]["const"]["run_id"] = "forged-successor"
  _commit_acquisition_schema_variants(root, selected)
  with self.assertRaisesRegex(ValueError, "frozen schema"):
      query_plan._load_static_contracts(source_run, source_contract)
  ```

- [ ] **Step 2: Run the RED test.**

  Run: `python3 -m pytest -q tests/test_v52_query_plan.py -k 'external_source_authority_ignores_unselected_legacy_schema_suffix or selected_branch'`

  Expected: the new test fails before implementation if the fixture still treats a rewritten branch as authoritative; the existing test continues to reject arity, reorder, and branch mutations.

- [ ] **Step 3: Keep the test-only mutation helper honest.** Ensure `_commit_acquisition_schema_variants` is used only to create a disposable negative fixture. It must not be used by a positive legacy/source-batch path, and the production schema bytes under `ROOT` must not be written.

- [ ] **Step 4: Run the GREEN query-plan schema suite.**

  Run: `python3 -m pytest -q tests/test_v52_query_plan.py -k 'schema or authority or successor or legacy or source_batch or absent'`

  Expected: PASS, including the selected-branch rejection and the existing R4 hash-pair/batch-root test; no Chromium selector is included.

- [ ] **Step 5: Commit the regression.**

  ```bash
  git add tests/test_v52_query_plan.py
  git commit -m "test: reject selected acquisition schema rewrites"
  ```

### Task 4: Replace the stale R2 substring oracle with exact R2/R3/R4 successor identity

**Files:**
- Modify: `tests/test_verify_v52_design.py:99-339`
- Test: `DesignVerificationTest.test_all_recovery_r2_schema_branches_are_exact`.

- [ ] **Step 1: Write the RED table entries for all three successor IDs.** Extend `game_authority_branches` and `authority_paths` with the exact entries below; use `canonical_json_bytes` and `hashlib.sha256` for exact branch digests. The table must contain no prefix/suffix matching:

  ```python
  successor_ids = (
      "historical_walkforward_500_recovery_schedule_conflicts_r2",
      "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3",
      "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4",
  )
  authority_paths = {
      successor_ids[0]: (
          "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json",
          "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json",
      ),
      successor_ids[1]: (
          "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json",
          "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json",
      ),
      successor_ids[2]: (
          "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json",
          "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json",
      ),
  }
  ```

  Assert exactly one branch matches each ID/path pair, then compare its canonical SHA-256 to the expected value obtained from the bound schema fixture. For the RunSpec branches, the canonical R2/R3/R4 digest anchors are `8ae189531e93c373590c1ce47b39b17a502f66bfb0dedeceea18730f8a7b722d`, `f569e3914f1b24b1e48cc2621a02a5029db92608d35d95109ad52bf0b5fa65a8`, and `4ca16938527d65f99ea8e20ad3a801cdef97be3f2946f05acadd7eb40e00bd21`; contract successor branch anchors are `a1202b7f9f85fbce2a0d5b4d36a2419316b6997f5aa2ff7d64cecc4c0f70e34c`, `8cdbba9eb09fa91865dd438e1622764326ade22460a05aa19b38df8f3c4ce252`, and `cc2ce2d365bb6a31d4c55ecd74f0db64dc71a5f74f339eb8aab5532696b8e3b7`.

- [ ] **Step 2: Remove substring acceptance.** Delete the current `self.assertIn(recovery_id, json.dumps(branch, sort_keys=True))` at `tests/test_verify_v52_design.py:240`. Replace it with exact `run_id == successor_id`, exact `run_spec_path`, exact `contract_path`, and exact digest assertions. Keep the `former_recovery_id` check only as an exact absence assertion after removing the two known successor IDs from the serialized branch; do not use that removal as a positive authority test.

- [ ] **Step 3: Add mixed-generation rejection.** In the same test, copy the R4 table row, replace its contract path with the R3 path (and separately replace its run-spec path with R2), and assert the matching list has length zero or raises the verifier's existing mismatch `ValueError` before any batch-root assertion. Assert `_R4_RECOVERY_ACQUISITION_BATCH_ROOT` is selected for the R4 identity and that `_R3_RECOVERY_ACQUISITION_BATCH_ROOT` is not selected for it.

- [ ] **Step 4: Run the RED/GREEN oracle suite.**

  Run: `python3 -m pytest -q tests/test_verify_v52_design.py -k 'all_recovery_r2_schema_branches_are_exact'`

  Expected after the table/oracle update: PASS with all three successor IDs; before the update, the new R4 and mixed-generation assertions fail because the old oracle knows only R2/R3 and uses substring matching.

- [ ] **Step 5: Commit the exact oracle.**

  ```bash
  git add tests/test_verify_v52_design.py
  git commit -m "test: assert exact recovery successor authorities"
  ```

### Task 5: Focused verification, scope audit, and final plan-only commit

**Files:**
- Verify only: `docs/superpowers/plans/2026-08-13-frozen-acquisition-schema-alignment.md` and the five test files listed above.

- [ ] **Step 1: Run the required schema-cluster suites.**

  ```bash
  python3 -m pytest -q tests/test_execution_registry.py -k 'schema or fixture or branch'
  python3 -m pytest -q tests/test_v52_query_plan.py tests/test_v52_source_finalize.py -k 'schema or authority or successor or legacy or source_batch or absent'
  ```

  Expected: zero failures and zero errors for the schema cluster. Do not run the exact 6-hour/1,480-test runner at this phase; it requires protected authenticated evidence. Do not select Chromium tests.

- [ ] **Step 2: Run lint and diff checks.**

  ```bash
  ruff check tests/test_execution_registry.py tests/test_v52_legacy_decision_inventory.py tests/test_v52_query_plan.py tests/test_v52_source_finalize.py tests/test_verify_v52_design.py
  git diff --check
  git diff --stat
  git status --short
  ```

  Expected: Ruff exits 0; `git diff --check` reports no whitespace errors; `git status --short` lists only the plan and the explicitly scoped test edits. Confirm no production/schema/contract/verifier/evidence/Chromium path changed with `git diff --name-only`.

- [ ] **Step 3: Self-review the plan and implementation.** Search this plan for unresolved markers and generic instructions; replace any result with a concrete path/function/command. Re-check that every later function name matches the earlier file map, absent-context never requests `remaining_authority`, selected-branch rewrites are rejected, exact R2/R3/R4 successor IDs and paths are checked, and the fixture seam is path-bound rather than a generic bypass.

- [ ] **Step 4: Create the requested plan-only commit.** After the implementation work is complete and the plan has passed `git diff --check`, commit only the plan file as requested:

  ```bash
  git add docs/superpowers/plans/2026-08-13-frozen-acquisition-schema-alignment.md
  git commit -m "docs: plan frozen schema test alignment"
  git rev-parse HEAD
  git status --short
  ```

  Expected: the commit subject is exactly `docs: plan frozen schema test alignment`; report the resulting SHA and whether the worktree is clean or contains the separately scoped implementation edits. The only plan risk to report is that the full authenticated gate remains deferred and the five Chromium defects remain intentionally out of scope.
