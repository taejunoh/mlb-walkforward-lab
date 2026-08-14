# Task 13 Exact-Suite Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the seven independently evidenced Task 13 exact-suite regressions without weakening immutable authority, source-evidence, or old-run protections.

**Architecture:** Keep every fail-closed production boundary intact. Fix product regressions at their value origin, repair stale hermetic fixtures to construct complete authenticated authorities, and canonicalize committed schemas without changing their JSON meaning. Existing r2 evidence and parent authority trees remain byte-for-byte immutable; a fresh r3 authority is created only after this suite is green.

**Tech Stack:** Python 3.11, `unittest`, JSON Schema Draft 2020-12, Git-bound authority snapshots, Ruff.

---

### Task 1: Reconcile ordinary duplicate schedule occurrences before binding raw-object provenance

**Files:**
- Modify: `mlb_backtest/v52_duplicate_gamepk_disposition.py`
- Modify: `mlb_backtest/v52_schedule_inventory.py`
- Test: `tests/test_v52_schedule_inventory.py`

- [ ] **Step 1: Write the failing recovery projection test**

Add a focused test using the immutable 2019/2020/2021 parent schedules. Assert that the projection accepts their 42/71/80 ordinary duplicate IDs after the catalog suppression, emits exactly one anchor per gamePk, and binds each anchor to the raw occurrence selected by the unchanged parser. Assert that a genuinely conflicting terminal pair still fails closed.

- [ ] **Step 2: Run the RED test**

Run:

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
PYTHONDONTWRITEBYTECODE=1 \
/Users/taejunoh/.local/bin/python3 -m unittest -v \
  tests.test_v52_schedule_inventory.RecoveryDispositionBindingsTests.test_recovery_schedule_publish_then_verified_load_rebinds_all_bindings
```

Expected: `ValueError: schedule raw contains duplicate gamePk`.

- [ ] **Step 3: Reuse the unchanged parser's occurrence selection**

Extract or expose one package-private helper from `v52_duplicate_gamepk_disposition.py` that accepts schedule occurrences, applies the existing `_normalize_game` and duplicate-selection rules, and returns the selected normalized game plus its exact raw object. In `_recovery_projection_anchors`, build occurrences from the filtered payload, call that helper, verify its normalized population equals the recomputed sidecar population, and bind canonical raw-object hashes from only those selected occurrences. Do not select “first” or “last” by position and do not relax conflict checks.

- [ ] **Step 4: Run focused GREEN tests**

Run the RED test plus the existing duplicate-disposition projection tests. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add mlb_backtest/v52_duplicate_gamepk_disposition.py \
  mlb_backtest/v52_schedule_inventory.py \
  tests/test_v52_schedule_inventory.py
git commit -m "fix: bind selected recovery schedule occurrences"
```

### Task 2: Repair recovery authority object field and non-source early return

**Files:**
- Modify: `mlb_backtest/v52_game_evidence_inventory.py`
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Test: `tests/test_v52_game_evidence_inventory.py`
- Test: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Add RED tests**

Add one table-driven test proving historical, source, and recovery `SourceAuthorityContext` values choose their exact timezone mapping using `context.authority.run_id`. Add two orchestration tests: a non-`SourceRunSpec` context with placeholder paths returns no disposition fields, while a `SourceRunSpec` with a forged path pair still raises `AUTHORITY_BLOCKED`.

- [ ] **Step 2: Confirm RED**

Run only those tests. Expected failures: missing `run_spec` on `SourceAuthorityContext`, and premature `source authority path pair differs`.

- [ ] **Step 3: Apply the two minimal fixes**

Use `context.authority.run_id` in `_timezone_authority_relatives`. In `_source_disposition_payload_fields`, restore:

```python
if not isinstance(context.run_spec, SourceRunSpec):
    return {}
```

before `_exact_authority_branch`; retain strict path-pair resolution for every `SourceRunSpec`.

- [ ] **Step 4: Run focused GREEN and commit**

```bash
git add mlb_backtest/v52_game_evidence_inventory.py \
  mlb_backtest/v52_source_orchestration.py \
  tests/test_v52_game_evidence_inventory.py \
  tests/test_v52_source_finalize.py
git commit -m "fix: restore source authority interface boundaries"
```

### Task 3: Restore historical timezone schema branch and complete the sealed CLI fixture

**Files:**
- Modify: `schemas/v5_2_timezone_seal_receipt.schema.json`
- Modify: `tests/test_v52_timezone_review.py`

- [ ] **Step 1: Add RED schema tests**

Validate both exact cases: authority-free historical seal maps to `config/v5_2_qd/venue_timezones.json`; recovery authority maps to its recovery audit path. Add a sealed-candidate CLI fixture assertion that all contract-bound design, plan, schema, and Git blobs exist before invocation.

- [ ] **Step 2: Confirm RED**

Run `tests.test_v52_timezone_review`. Expected: historical mapping const mismatch and missing no-follow design ancestor.

- [ ] **Step 3: Fix schema and fixture**

Restore the base historical `mapping.path.const`. Keep the recovery mapping const only inside its authority-conditioned branch. Replace manual RunSpec/contract copying in `_sealed_candidate_fixture` with the repository's complete `_statcast_authority_fixture(..., in_place=True)` setup.

- [ ] **Step 4: Run GREEN and commit**

```bash
git add schemas/v5_2_timezone_seal_receipt.schema.json tests/test_v52_timezone_review.py
git commit -m "fix: close timezone authority fixtures"
```

### Task 4: Update stale immutable-authority fixtures without weakening public selectors

**Files:**
- Modify: `tests/test_v52_legacy_decision_inventory.py`
- Modify: `tests/test_v52_query_plan.py`
- Modify: `tests/test_execution_registry.py`
- Modify: `tests/test_v52_timezone_review.py`

- [ ] **Step 1: Preserve the production selector and add fixture RED assertions**

Assert the historical RunSpec/contract SHA pair remains the committed immutable pair before each public publish. For deliberately forged batches, install canonical forged bytes through the test-only raw writer and assert the downstream validator fails; do not ask `publish_query_batch` to accept forged authority.

- [ ] **Step 2: Confirm current failures**

Run the exact 37 affected tests or their shared fixture entry tests. Expected: `query batch authority hash pair differs`.

- [ ] **Step 3: Repair fixtures**

Stop rewriting `historical_source_acquisition.json` to a temporary protected-inventory hash. Use the existing hermetic source/recovery authority fixture or a test-only catalog seam for local inventory data. Keep `_batch_root_for_batch` and its fixed authority pairs unchanged.

- [ ] **Step 4: Run GREEN and commit**

```bash
git add tests/test_v52_legacy_decision_inventory.py \
  tests/test_v52_query_plan.py \
  tests/test_execution_registry.py \
  tests/test_v52_timezone_review.py
git commit -m "test: preserve immutable query batch authority"
```

### Task 5: Canonicalize bound schemas and update exact branch-count assertions

**Files:**
- Mechanically rewrite: `schemas/v5_2_clean_source_replay_receipt.schema.json`
- Mechanically rewrite: `schemas/v5_2_derived_disposition_sidecar.schema.json`
- Mechanically rewrite: `schemas/v5_2_normalized_snapshot.schema.json`
- Mechanically rewrite: `schemas/v5_2_source_inventory.schema.json`
- Test: `tests/test_execution_registry.py`
- Test: `tests/test_verify_v52_design.py`

- [ ] **Step 1: Record semantic and canonical RED evidence**

For each schema, load JSON, compute canonical compact sorted-key bytes plus one LF, assert current bytes differ, and record the pre-rewrite semantic object digest. Update the stale acquisition-checkpoint assertion to locate branches by run-id rather than expecting `len(oneOf) == 2`.

- [ ] **Step 2: Canonicalize bytes only**

Use the repository's canonical JSON formatter. Re-read every rewritten file and prove:

```python
file_bytes == canonical_json_bytes(json.loads(file_bytes)) + b"\n"
```

and prove the parsed JSON object is unchanged.

- [ ] **Step 3: Run schema-bound focused suites**

Run execution-registry source-receipt tests, verify-design tests, source-finalize tests, and `git diff --check`. Expected: no canonical JSON rejection.

- [ ] **Step 4: Commit**

```bash
git add schemas/v5_2_clean_source_replay_receipt.schema.json \
  schemas/v5_2_derived_disposition_sidecar.schema.json \
  schemas/v5_2_normalized_snapshot.schema.json \
  schemas/v5_2_source_inventory.schema.json \
  tests/test_execution_registry.py tests/test_verify_v52_design.py
git commit -m "fix: canonicalize recovery-bound schemas"
```

### Task 6: Final review and exact-suite verification

**Files:**
- Verify only; no product edits.

- [ ] **Step 1: Run spec-compliance review**

Confirm all seven evidenced causes are addressed and no production authority selector, old evidence, or r2 contract-bound document was weakened or modified.

- [ ] **Step 2: Run code-quality review**

Review the complete diff from `226e1ba` through the final fix HEAD. Resolve every P0/P1/P2 finding through the owning implementer and re-review.

- [ ] **Step 3: Run focused validation**

Run Ruff on changed Python files, canonical checks on all bound schemas, `git diff --check`, and the affected test modules.

- [ ] **Step 4: Run a fresh isolated exact suite**

Create a new detached clean worktree at the final HEAD and run `scripts/run_task13_exact_suite.py` alone, using the protected source root for both input roots. Require exit code 0, zero failures/errors, the expected skip count, and unchanged four parent-tree fingerprints plus protected v5.1 hashes.

- [ ] **Step 5: Proceed to fresh r3 authority**

Only after exact-suite success, leave r2 byte-for-byte unchanged and execute the separately reviewed r3 authority-sealing plan.
