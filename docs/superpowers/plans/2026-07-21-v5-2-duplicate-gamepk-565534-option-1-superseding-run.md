# MLB v5.2 Duplicate `gamePk` 565534 Option 1 Superseding-Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every production change and superpowers:verification-before-completion before every completion claim. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one source-only, namespace-isolated superseding v5.2 historical run that derives a raw-less disposition for the exact quarantined `gamePk=565534` pair, preserves all old-run evidence byte-for-byte, and carries the derived authority through `SOURCES_READY` with exactly three bounded schedule neutralizations.

**Architecture:** Keep the old `RunSpec`, acquisition contract, registry chain, checkpoint bytes, raw evidence, and default CLI paths unchanged. Add a distinct closed `SourceRunSpec`, exact old/new schema branches, and a frozen source-authority context. An authority-aware adapter reopens the old forensic evidence, authenticates the 2,472-occurrence/42-duplicate response, suppresses only the first `565534`, lets the unchanged parser reconcile 41 ordinary pairs to 2,430 games, and publishes through the existing derived-disposition transaction. Thread that binding through branch-exact checkpoint, inventory, replay, orchestration, and finalization gates; make `SOURCES_READY` terminal for the source-only run and place exactly three raw-provenance-bound neutralizations in the verified prior-schedule projection/consumer path.

**Tech Stack:** Python 3.11, frozen dataclasses, `pathlib`, POSIX no-follow descriptors and advisory leases, canonical JSON plus SHA-256, JSON Schema Draft 2020-12, `unittest`, Ruff, and Git exact import-closure verification.

---

## Authority, workspace, and no-network boundaries

All commands run from `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`.

This plan authorizes source-code, test, schema, protocol, and documentation changes only. It does **not** authorize a network request, `v52_acquire` execution, `--plan-only` rollout, registry creation, evidence publication, or source finalization against either production namespace. Task 13 ends at a tested implementation and an operator handoff. Do not run `curl`, `wget`, an acquisition CLI, or any command that can contact MLB, Baseball Savant, GitHub, or another remote service.

The following are protected and must never be edited, removed, staged, or committed:

- `mlb_backtest/v52_query_plan 2.py`
- `tests/test_v52_query_plan 2.py`
- `schemas/v5_2_preflight.schema 2.json`
- `reports/v5_2_qd/walkforward_500_preflight 2.md`
- `protocols/v5_2_qd/historical_walkforward_500.json`
- `protocols/v5_2_qd/historical_source_acquisition.json`
- every existing path under `data/v5_2_qd/historical/`
- every existing path under `audit/v5_2_qd/historical/`

Use explicit `git add` path lists at every commit boundary. Never use `git add .`, `git add -A`, a directory-wide add, or a glob that could capture protected duplicates or production evidence.

The approved amendment and this implementation-plan authority must be committed
by the repository owner before Task 1 begins because the new contract binds the
design SHA-256 and both plan SHA-256/blob ID. The executor verifies, but does
not rewrite, either file:

```bash
git diff --exit-code -- docs/superpowers/plans/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md
git diff --exit-code -- docs/superpowers/specs/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md
git rev-parse HEAD:docs/superpowers/plans/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md
git rev-parse HEAD:docs/superpowers/specs/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md
```

Expected: no diff and two 40-hex blob IDs. If any check fails, stop before
creating the new contract.

## Fixed old-evidence oracle

Tests and runtime checks must use the following complete literals; no test may derive an expected value from mutable production bytes and no abbreviated digest is permitted:

```python
OLD = {
    "run_id": "historical_walkforward_500",
    "run_spec_sha256": "d622bff184f0bdade22848d4b76fc43bdf9ac88c7a51513dee4a7d5f51bd1492",
    "contract_sha256": "cef89a1d4191ef83eb0fc2b0f4bdd191aeb8ba53cbb1bc6df73199604f23a9d9",
    "static_batch_id": "069fd746f9c7ecb1267b7ae44f8d44a1aeb3575a92c64482dacbc5b61d8f0571",
    "static_batch_sha256": "a6a2ac816620797b21e5bcf297fb92453f1143ed0d3d4c01effbf15c531ca39a",
    "checkpoint_sha256": "00b8b6eae2c0ba8de17f6ed1c05f0c9ddfb98e967e9b7ef0284581cc98b5ad06",
    "query_id": "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8",
    "registration_sha256": "5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95",
    "attempt_sha256": "d88722e680d30cd5bb78ef36d62adde15a20e91e6c1a0c132028a2b0aa746fce",
    "quarantine_sha256": "a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8",
    "raw_sha256": "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0",
    "raw_byte_count": 21182105,
    "suppressed_object_sha256": "5d2fc4945a5e562bb6676d8af0d1272dec29c946c293925a938655d15a000941",
    "retained_object_sha256": "6b07d5e4781c472ca1e0a22b7190acdef61c8fe3d8885524835d6f6c876a9dd6",
}
```

Also freeze these old registry heads in `tests/test_execution_registry.py`:

```python
OLD_DESIGN_HEAD = "ec9dca911b919f32f2012b8aaacb996b09e1c5e976cd00df7026502c6e07413a"
OLD_ACQUIRING_HEAD = "e889d76bf07e66b1cdfc1a65170e2a7aab83dad855cab535fa5483e5bbe55f2a"
```

Before Task 1, create an out-of-repository byte manifest and retain its printed
directory path for Task 13:

```bash
V52_565534_AUDIT_DIR="$(mktemp -d)"
find data/v5_2_qd/historical audit/v5_2_qd/historical -type f -print0 | xargs -0 shasum -a 256 | LC_ALL=C sort > "$V52_565534_AUDIT_DIR/old-evidence.sha256"
find data/v5_2_qd/historical audit/v5_2_qd/historical -type f | wc -l > "$V52_565534_AUDIT_DIR/old-evidence.count"
echo "$V52_565534_AUDIT_DIR"
```

After every task, run `shasum -a 256 -c
"$V52_565534_AUDIT_DIR/old-evidence.sha256"`. Any mismatch stops execution;
never repair the mismatch by rewriting old evidence.

### Task 1: Freeze old evidence and add the new source-only authorities

**Files:**
- Modify: `tests/test_v52_acquisition_contract.py`
- Modify: `tests/test_protocols.py`
- Modify: `tests/test_verify_v52_design.py`
- Modify: `mlb_backtest/protocols.py`
- Modify: `mlb_backtest/verify_v52_design.py`
- Modify: `schemas/v5_2_run_spec.schema.json`
- Modify: `schemas/v5_2_acquisition_contract.schema.json`
- Create: `protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json`
- Create: `protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json`

- [ ] **Step 1: Write red authority and immutable-old tests**

Add `test_source_run_spec_is_a_closed_source_only_authority_not_old_run_spec`, `test_superseding_565534_run_spec_is_source_only_and_namespace_disjoint`, `test_superseding_565534_contract_binds_design_plan_run_and_exact_supersedes`, `test_superseding_565534_retains_v521_existing_parser_versions`, `test_combined_run_schema_writer_and_design_verifier_preserve_both_exact_branches`, and `test_old_default_authorities_keep_authoritative_sha256s`.

Use the exact 11-key SourceRunSpec literal in the approved amendment: `schema_version`, `run_id`, `model_version`, `label`, `authority_scope`, `required_source_years`, `forbidden_source_years`, `source_requirements`, `parser_policy`, four-key `namespaces`, and all-false `downstream_authority`. Assert it has no fold, target-count, selection, model-card, feature, lock, outcome, OOF, or 500-result key. Do not route it through `parse_run_spec()`.

The new contract keeps schema version 2 and existing parser versions, adds only the disposition endpoint to the schedule source, changes `run_id`/roots, binds the amendment design, this plan, and SourceRunSpec, and has the old 15 top-level keys plus exact `supersedes`. Its bindings omit old `plan_a_amendment`. The `supersedes` object contains only:

```python
{
    "run_id", "run_spec_sha256", "acquisition_contract_sha256",
    "static_batch_id", "static_batch_sha256", "checkpoint_sha256",
    "query_id", "quarantine_receipt_sha256", "raw_sha256",
}
```

Use the complete values in `OLD`. Do not put an old filesystem path or a mutation permission in `supersedes`.

- [ ] **Step 2: Prove the new files/branch are absent**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_protocols tests.test_v52_acquisition_contract -v
```

Expected: failures for missing SourceRunSpec/contract, old-only schemas, or old-only schema writers; old literal parser tests still pass.

- [ ] **Step 3: Add a separate exact SourceRunSpec API and combined schema builder**

Keep `RunSpec`, `parse_run_spec()`, `validate_run()`, `RUN_REQUIRED_KEYS`, `NAMESPACE_KEYS`, and `expected_historical_run()` unchanged. Add frozen `SourceRunSpec`, `SOURCE_RUN_REQUIRED_KEYS`, `expected_superseding_source_run()`, `parse_source_run_spec()`, and `load_source_run_spec()`. Exact-validate every nested key and literal; do not dispatch by arbitrary run ID.

Add `historical_authority_schema()` returning a closed top-level `oneOf` of the current old const/schema and exact SourceRunSpec const/schema. Make `write_historical_contract()` publish old protocol bytes plus this combined schema; add a source-authority writer for new protocol bytes plus the same schema. Update `verify_contract_schemas()` to compare the combined builder and separately parse both files without rewriting either.

- [ ] **Step 4: Create canonical authorities and two exact schema branches**

Serialize both new JSON files as sorted compact JSON plus one newline. Write the combined `v5_2_run_spec` schema. Convert acquisition schema to an exact old/new `oneOf`; each branch has its own `const`, exact properties/required keys, and closed nested objects. Update `_verify_frozen_acquisition_schema()` in Task 2; no open common subset.

- [ ] **Step 5: Verify and commit the authority branch**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_protocols tests.test_v52_acquisition_contract tests.test_verify_v52_design -v
ruff check mlb_backtest/protocols.py mlb_backtest/verify_v52_design.py tests/test_protocols.py tests/test_v52_acquisition_contract.py tests/test_verify_v52_design.py
git diff --check
```

Expected: `OK`, Ruff exit 0, diff check exit 0, and the two old authority hashes still equal `OLD`.

```bash
git add protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json schemas/v5_2_run_spec.schema.json schemas/v5_2_acquisition_contract.schema.json mlb_backtest/protocols.py mlb_backtest/verify_v52_design.py tests/test_protocols.py tests/test_v52_acquisition_contract.py tests/test_verify_v52_design.py
git commit -m "feat: add isolated v5.2 superseding source authorities"
```

### Task 2: Introduce one authenticated namespace context and preserve old defaults

**Files:**
- Modify: `mlb_backtest/v52_query_plan.py`
- Modify: `mlb_backtest/execution_registry.py`
- Modify: `tests/test_v52_query_plan.py`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Write red cross-binding and path-factory tests**

Add tests that load old `RunSpec` and new `SourceRunSpec`, preserve old `_paths()`, derive new execution root, and reject every old/new cross-pair, overlap, traversal, absolute/backslash path, symlink, hard link, or outside root. Add `test_source_run_registry_stops_at_sources_ready_and_rejects_later_phase_receipts` covering load, advance, retry, and quarantine.

- [ ] **Step 2: Run the focused red tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_query_plan tests.test_execution_registry -v
```

Expected: new-run contract/schema and path assertions fail while old default fixtures remain green.

- [ ] **Step 3: Add `SourceAuthorityContext` at the existing contract load boundary**

Extend `_load_static_contracts(run_spec_path, acquisition_contract_path)` rather than adding an unauthenticated loader. Return a frozen context with `authority: RunSpec | SourceRunSpec`, authority/contract paths and hashes, run ID, four roots, and report root. Select the parser by exact path; select the acquisition schema branch by exact contract path. Verify authority, design, plan working-tree/blob, and new exact `supersedes`.

At construction, canonicalize repository-relative lexical paths, open each existing ancestor with no-follow descriptors, reject aliases/overlap, and require every context path to match authority namespaces and contract roots. Defaults remain old.

- [ ] **Step 4: Make registry `_paths()` consume the authenticated audit namespace**

Derive registry paths from `authority.raw["namespaces"]["audit"] / "execution"`. Define `SOURCE_ONLY_PHASES=("DESIGN_ACCEPTED","ACQUIRING","SOURCES_READY")`; use it for new receipt parsing, load, transition, retry, and quarantine. Reject every later phase before any write. Old `PHASES` remains unchanged.

- [ ] **Step 5: Verify and commit the context boundary**

Run the two test modules, Ruff the four touched Python files, and run `git diff --check`. Then commit only:

```bash
git add mlb_backtest/v52_query_plan.py mlb_backtest/execution_registry.py tests/test_v52_query_plan.py tests/test_execution_registry.py
git commit -m "refactor: derive v5.2 source paths from authenticated namespaces"
```

### Task 3: Thread namespace factories through acquisition and inventory services

**Files:**
- Modify: `mlb_backtest/v52_acquire.py`
- Modify: `mlb_backtest/v52_schedule_inventory.py`
- Modify: `mlb_backtest/v52_legacy_decision_inventory.py`
- Modify: `mlb_backtest/v52_timezone_review.py`
- Modify: `mlb_backtest/v52_game_evidence_inventory.py`
- Modify: `mlb_backtest/v52_verify_sources.py`
- Modify: `mlb_backtest/v52_snapshot_store.py`
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `mlb_backtest/v52_source_finalize.py`
- Modify: `tests/test_v52_acquire_e2e.py`
- Modify: `tests/test_v52_schedule_inventory.py`
- Modify: `tests/test_v52_legacy_decision_inventory.py`
- Modify: `tests/test_v52_timezone_review.py`
- Modify: `tests/test_v52_game_evidence_inventory.py`
- Modify: `tests/test_v52_verify_sources.py`
- Modify: `tests/test_v52_snapshot_store.py`
- Modify: `tests/test_v52_source_finalize.py`

- [ ] **Step 1: Add red old/new path matrix tests**

For every public constructor/loader, assert old default paths are unchanged and new explicit `--run-spec` plus `--acquisition-contract` arguments resolve only below the four superseding roots. Add negative tests for every old/new cross-pair and for caller-supplied `evidence_root`, checkpoint, inventory, review, replay, or output path outside its authenticated context.

- [ ] **Step 2: Run the focused red matrix**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_acquire_e2e tests.test_v52_schedule_inventory tests.test_v52_legacy_decision_inventory tests.test_v52_timezone_review tests.test_v52_game_evidence_inventory tests.test_v52_verify_sources tests.test_v52_snapshot_store tests.test_v52_source_finalize -v
```

Expected: new-path cases fail at current hardcoded constants such as `_INVENTORY_RELATIVE`, `_CHECKPOINT_ROOT`, `_SOURCE_ROOT_RELATIVE`, and `_RUN_ID`.

- [ ] **Step 3: Replace constants only at path-construction boundaries**

Keep public signatures where possible. Pass the authenticated context through:

- `v52_acquire.py` batch, split receipt, checkpoint, and raw-store roots;
- `build_schedule_anchor_inventory()`, `publish_schedule_anchor_inventory()`, and `load_verified_schedule_anchor_inventory()`;
- legacy-decision inventory receipt/batch/output bindings and timezone candidate/review/seal paths;
- game-evidence inventory build/publish/load;
- source verification and snapshot-store inventory/replay loaders;
- `CommonArgs`, `build_context()`, review/replay/inventory publication, and `advance_sources_ready()`;
- CLI defaults (old paths) and explicit new-path validation.

Do not replace strict paths with freely supplied strings. A path argument is accepted only after equality with the authenticated context-derived path.

- [ ] **Step 4: Re-run and commit the parametric plumbing**

Run all eight focused test modules, Ruff touched files, and `git diff --check`. Commit only the listed files:

```bash
git add mlb_backtest/v52_acquire.py mlb_backtest/v52_schedule_inventory.py mlb_backtest/v52_legacy_decision_inventory.py mlb_backtest/v52_timezone_review.py mlb_backtest/v52_game_evidence_inventory.py mlb_backtest/v52_verify_sources.py mlb_backtest/v52_snapshot_store.py mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_source_finalize.py tests/test_v52_acquire_e2e.py tests/test_v52_schedule_inventory.py tests/test_v52_legacy_decision_inventory.py tests/test_v52_timezone_review.py tests/test_v52_game_evidence_inventory.py tests/test_v52_verify_sources.py tests/test_v52_snapshot_store.py tests/test_v52_source_finalize.py
git commit -m "refactor: propagate isolated v5.2 source namespaces"
```

### Task 4: Specify the exact local duplicate fixture and disposition recomputation

**Files:**
- Create: `tests/fixtures/v52_duplicate_gamepk_565534/exact_pair.json`
- Create: `tests/fixtures/v52_duplicate_gamepk_565534/ordinary_games.json`
- Create: `tests/fixtures/v52_duplicate_gamepk_565534/negative_cases.json`
- Create: `mlb_backtest/v52_duplicate_gamepk_disposition.py`
- Modify: `tests/test_v52_source_schedule.py`

- [ ] **Step 1: Write the four required red selection/projection tests**

Add:

- `test_565534_exact_pair_retains_only_resumed_record_and_binds_disposition`;
- `test_565534_requires_both_complete_fingerprints_and_all_fixed_fields`;
- `test_nonordinary_duplicate_gamepk_or_third_special_copy_fails_closed`;
- `test_2019_projection_authenticates_2472_occurrences_42_duplicates_and_2430_unique`.

Small fixtures cover pair predicates and ordinary reconciliation. A read-only test over the immutable old raw verifies root/date totals 2,472, 2,430 unique IDs, 42 duplicate IDs, one extra each, 2,471 after the special suppression, 41 ordinary pairs, and 2,430 final games. Negative cases mutate every special predicate, totals, or an ordinarily irreconcilable pair. Do not call every duplicate invalid.

- [ ] **Step 2: Confirm the module is missing**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule -v
```

Expected: import failure for `mlb_backtest.v52_duplicate_gamepk_disposition` or the new tests fail.

- [ ] **Step 3: Implement the closed recomputer, not a generic parser policy**

Create constants for the exact endpoint, old bindings, positions, hashes, and fixed fields. Expose narrowly named functions such as:

```python
def build_duplicate_disposition_authority(
    root: Path,
    *,
    source_run_spec_path: Path,
    acquisition_contract_path: Path,
) -> DuplicateGamePkDispositionAuthority: ...

def project_schedule_occurrences(
    authority: DuplicateGamePkDispositionAuthority,
) -> ScheduleOccurrenceProjection: ...

def publish_duplicate_gamepk_disposition(
    store: V52EvidenceStore,
    authority: DuplicateGamePkDispositionAuthority,
) -> PublishedEvidence: ...
```

Construct the exact LogicalQuery from §3 of the amendment, including all 10 dataclass fields: empty parameters, complete closed `requested_range` (year/dates/policy/game ID/old authority/two occurrences), `expected_media_type="application/json"`, parser v5.2.1, no parent, and authenticated new hashes. No caller supplies a literal.

The frozen adapter authenticates new files/store root and snapshots old registration, quarantine, and raw using no-follow/single-link identity checks. `project_schedule_occurrences()` validates raw totals before suppression, mirrors unchanged parser reconciliation with raw provenance, compares the 2,430-game canonical output to `parse_schedule`, and returns an immutable projection. Never return or serialize replacement raw bytes. Keep `source_parsers/schedule.py` unchanged.

- [ ] **Step 4: Verify strictness and commit**

Run the focused schedule tests, the pre-existing strict parser tests, Ruff, and diff check. Confirm `git diff --exit-code -- mlb_backtest/source_parsers/schedule.py`.

```bash
git add mlb_backtest/v52_duplicate_gamepk_disposition.py tests/test_v52_source_schedule.py tests/fixtures/v52_duplicate_gamepk_565534/exact_pair.json tests/fixtures/v52_duplicate_gamepk_565534/ordinary_games.json tests/fixtures/v52_duplicate_gamepk_565534/negative_cases.json
git commit -m "feat: recompute exact 565534 duplicate disposition"
```

### Task 5: Add the second closed derived-sidecar branch and reuse publication

**Files:**
- Modify: `schemas/v5_2_derived_disposition_sidecar.schema.json`
- Modify: `mlb_backtest/v52_source_contracts.py`
- Modify: `mlb_backtest/v52_evidence.py`
- Modify: `tests/test_v52_evidence.py`
- Modify: `tests/test_v52_source_schedule.py`

- [ ] **Step 1: Write red schema, dispatch, and lifecycle tests**

Add:

- `test_old_preseason_derived_disposition_branch_is_unchanged`;
- `test_duplicate_disposition_is_rawless_and_has_no_attempt_or_reservation`;
- `test_publish_derived_disposition_rejects_nonempty_raw_or_attempt_inventory`;
- `test_disposition_sidecar_tamper_cross_run_binding_and_rebind_fail_closed`;
- `test_schedule_disposition_requires_authenticated_old_root_adapter_at_final_rebind`;
- `test_old_2019_query_quarantine_and_raw_are_immutable_and_read_only_disposition_authority`.

Use existing publication failure hooks to assert `success.json` is absent before final rebind, no object/attempt/reservation appears, and a failed publish cleans only its owned new sidecar. Snapshot old files before and after every case with fixed hashes from `OLD`.

- [ ] **Step 2: Run the red tests**

Run the two test modules. Expected: the current `_derived_semantics()` rejects the schedule endpoint and the single-branch schema rejects schedule semantics.

- [ ] **Step 3: Convert the existing schema to two closed `oneOf` branches**

Preserve the current preseason branch byte-for-byte in meaning. Add a second exact schedule branch with only the existing envelope keys:

```python
{
    "grain", "parser_version", "query_id", "schema_version",
    "semantics", "source", "storage_mode",
}
```

Use the amendment's exact nine semantic keys, closed subobjects, nine occurrence counts, stable first-seen 2,430-game digest, and numeric-game-sorted selected-raw-provenance digest. Reject unknown/missing keys, noncanonical bindings, or cross-run query ID.

- [ ] **Step 4: Dispatch recomputation by the exact endpoint**

Add keyword-only `authority=None` to `recompute_query_sidecar()` and `publish_derived_disposition()` without changing old two-argument calls. The schedule endpoint locally imports and type-checks the adapter, checks query/store root, requires `raw==b""`, and recomputes before sidecar construction and immediately before success. Preseason requires `authority is None`. The wrapper owns lease-scoped publication; direct schedule calls without/cross-bound adapter fail.

Do not change the transaction in `publish_derived_disposition()`: it already requires a lease, empty attempts/objects, O_EXCL-like write-once sidecar behavior, a final query/lease/inventory/sidecar rebind, and only then `success.json`.

- [ ] **Step 5: Verify and commit**

Run both focused modules, Ruff, and diff check, then:

```bash
git add schemas/v5_2_derived_disposition_sidecar.schema.json mlb_backtest/v52_source_contracts.py mlb_backtest/v52_evidence.py tests/test_v52_evidence.py tests/test_v52_source_schedule.py
git commit -m "feat: publish schedule duplicate derived disposition"
```

### Task 6: Add a backward-readable superseding checkpoint branch

**Files:**
- Modify: `schemas/v5_2_acquisition_checkpoint.schema.json`
- Modify: `mlb_backtest/execution_registry.py`
- Modify: `mlb_backtest/v52_acquire.py`
- Modify: `tests/test_execution_registry.py`
- Modify: `tests/test_v52_acquire_e2e.py`

- [ ] **Step 1: Write red branch and population tests**

Add `test_checkpoint_requires_exact_dispositioned_query_ids`, `test_old_checkpoint_bytes_parse_to_trailing_empty_tuple_and_old_writer_omits_field`, and `test_new_checkpoint_never_contains_old_query_id_or_old_artifact_binding`. Assert every new checkpoint requires explicit sorted unique IDs; derived is completed/dispositioned only; old ID/path is absent.

- [ ] **Step 2: Run the red tests**

Expected: current `_CHECKPOINT_KEYS`, `AcquisitionCheckpoint`, schema, semantic projection, progression, and writer lack the new branch/field.

- [ ] **Step 3: Implement exact old/new schema dispatch**

Append `dispositioned_query_ids: tuple[str, ...] = ()` as the final dataclass field. Parse old exact keys to the trailing empty tuple without byte normalization; old writer omits the JSON field. Use an internal unset sentinel in `publish_acquisition_checkpoint`: old calls may only use the default; new calls must explicitly provide even `[]`. Make payload validation, semantic projection, progression, snapshots, and rebinds branch-exact.

For each dispositioned ID, reopen the registered query and success/sidecar under the new raw root and require `storage_mode="DERIVED_DISPOSITION"` plus the exact schedule branch. Reject old query/artifact bindings even though the sidecar carries old hashes as provenance.

- [ ] **Step 4: Verify and commit**

Run the two focused modules, Ruff, diff check, then:

```bash
git add schemas/v5_2_acquisition_checkpoint.schema.json mlb_backtest/execution_registry.py mlb_backtest/v52_acquire.py tests/test_execution_registry.py tests/test_v52_acquire_e2e.py
git commit -m "feat: bind derived dispositions in superseding checkpoints"
```

### Task 7: Build the one-anchor schedule inventory and retained-only query edges

**Files:**
- Modify: `mlb_backtest/v52_schedule_inventory.py`
- Modify: `mlb_backtest/v52_query_plan.py`
- Modify: `schemas/v5_2_schedule_anchor_inventory.schema.json`
- Modify: `tests/test_v52_schedule_inventory.py`
- Modify: `tests/test_v52_query_plan.py`

- [ ] **Step 1: Write red inventory and child-edge tests**

Add `test_schedule_inventory_contains_one_565534_and_only_retained_children` and `test_2019_schedule_source_is_closed_derived_branch_with_null_new_raw_sha`. Assert seven ordered static entries with derived 2019 slot; exact nine counts; one retained anchor; retained-only edges; and every anchor's selected `raw_game_object_sha256`.

- [ ] **Step 2: Run the red tests**

Expected: `build_schedule_anchor_inventory()` accepts only fetched/protected evidence and expects seven fetched schedule queries.

- [ ] **Step 3: Add the exact new-run schedule path**

For old runs, preserve exact behavior. For the new run, publish via adapter/lease, require checkpoint binding, consume the immutable projection directly, and never call `_raw_schedule_metadata` on filtered bytes. Add an exact season-source `oneOf`: six old non-null-raw items; one 2019 DERIVED item with `raw_sha256:null`, new query/receipt/sidecar, and occurrence counts. Add `raw_game_object_sha256` to the closed anchor branch. Never create fetched 2019 success/raw.

Update static/reference query-batch construction and inventory validation to branch only on the authenticated run identity. Reject a second `565534`, missing retained anchor, changed sidecar, or any edge count sourced from the suppressed object.

- [ ] **Step 4: Verify and commit**

Run both focused modules plus strict schedule-source tests, Ruff, diff check, then:

```bash
git add mlb_backtest/v52_schedule_inventory.py mlb_backtest/v52_query_plan.py schemas/v5_2_schedule_anchor_inventory.schema.json tests/test_v52_schedule_inventory.py tests/test_v52_query_plan.py
git commit -m "feat: bind retained 565534 anchor into query planning"
```

### Task 8: Carry the disposition through game inventory, replay, and snapshot authority

**Files:**
- Modify: `mlb_backtest/v52_game_evidence_inventory.py`
- Modify: `mlb_backtest/v52_verify_sources.py`
- Modify: `mlb_backtest/v52_snapshot_store.py`
- Modify: `schemas/v5_2_game_evidence_inventory.schema.json`
- Modify: `schemas/v5_2_source_inventory.schema.json`
- Modify: `tests/test_v52_game_evidence_inventory.py`
- Modify: `tests/test_v52_verify_sources.py`
- Modify: `tests/test_v52_snapshot_store.py`

- [ ] **Step 1: Write red propagation/rebind tests**

Add assertions that game inventory, normalized schedule snapshot, prior-history snapshot, source inventory, and replay inputs carry the same derived query ID, success receipt, sidecar hash, and `dispositioned_query_ids`. Tamper the sidecar, old raw, filtered population, checkpoint list, or inventory binding after initial read and require the final rebind to fail.

- [ ] **Step 2: Run the three red modules**

Expected: current schemas have old constant paths/run IDs and current snapshot authority types have no disposition binding.

- [ ] **Step 3: Extend closed schemas and frozen authority types**

Use old/new `oneOf` branches for constant run/path values. Add a closed `duplicate_gamepk_disposition` binding object and sorted `dispositioned_query_ids` only to the superseding branch. Extend `AuthorityBindings`, `SnapshotReplayInputs`, normalized snapshot/source inventory builders, and validators. Every read must reopen old authority and new sidecar, recompute the selection, and rebind device/inode/link count/bytes/hash immediately before commit.

- [ ] **Step 4: Verify and commit**

Run the three modules, Ruff, diff check, then:

```bash
git add mlb_backtest/v52_game_evidence_inventory.py mlb_backtest/v52_verify_sources.py mlb_backtest/v52_snapshot_store.py schemas/v5_2_game_evidence_inventory.schema.json schemas/v5_2_source_inventory.schema.json tests/test_v52_game_evidence_inventory.py tests/test_v52_verify_sources.py tests/test_v52_snapshot_store.py
git commit -m "feat: propagate duplicate disposition through source snapshots"
```

### Task 9: Gate orchestration, finalization, and `SOURCES_READY`

**Files:**
- Modify: `mlb_backtest/v52_source_orchestration.py`
- Modify: `mlb_backtest/v52_source_finalize.py`
- Modify: `mlb_backtest/execution_registry.py`
- Modify: `schemas/v5_2_source_review_receipt.schema.json`
- Modify: `schemas/v5_2_clean_source_replay_receipt.schema.json`
- Modify: `tests/test_v52_source_finalize.py`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Write the red end-to-end gate test**

Add `test_replay_inventory_and_sources_ready_require_disposition_binding`. Build only temporary local evidence. Assert status/preflight/publish/review/replay/advance all reject a missing, extra, tampered, cross-run, or stale disposition binding, and accept it only when the terminal checkpoint, source inventory, independent review, two clean replays, and explicit selected checkpoint all agree.

- [ ] **Step 2: Run red tests**

Run `tests.test_v52_source_finalize` and `tests.test_execution_registry`. Expected: current receipt schemas/constants and registry phase validators bind only the old run.

- [ ] **Step 3: Add exact new receipt branches and final rebinds**

Preserve old receipt schemas/selection semantics. Add superseding branches that bind SourceRunSpec/contract and disposition authority. Recompute immediately before phase publication. Enforce `SOURCE_ONLY_PHASES` again at orchestration/CLI advance so `SOURCES_READY` is terminal. `supersedes` never joins chains.

- [ ] **Step 4: Verify and commit**

Run both modules, Ruff, diff check, then:

```bash
git add mlb_backtest/v52_source_orchestration.py mlb_backtest/v52_source_finalize.py mlb_backtest/execution_registry.py schemas/v5_2_source_review_receipt.schema.json schemas/v5_2_clean_source_replay_receipt.schema.json tests/test_v52_source_finalize.py tests/test_execution_registry.py
git commit -m "feat: require disposition authority at sources ready"
```

### Task 10: Add exactly three schedule neutralizations at the verified history boundary

**Files:**
- Modify: `mlb_backtest/v52_sources/history.py`
- Modify: `mlb_backtest/feature_engine.py`
- Modify: `tests/test_v52_source_history.py`
- Modify: `tests/test_feature_engine.py`

- [ ] **Step 1: Write the two red allowlist tests**

Add `test_three_exact_duplicate_dependency_neutralizations_and_no_others` and `test_565535_and_566157_complete_hashes_are_known_but_unapproved`.

The only rows are:

```python
EXPECTED_NEUTRALIZATIONS = (
    ("35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab", "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29", 566156, "2019-09-06", 116),
    ("35b92c2f65d844a4e66574c23cf1ba4b8c2c30c8823024cf94378c67fda8c2ab", "3e80a9bb13f2751a7378e3a4157777b2aad6b99d70b51f700c4393a9b73ddf29", 566156, "2019-09-06", 133),
    ("92900ec47f924a1a68d533d63ae068faabd42262b808461a543974b61f75d5ef", "5224b908bc7a340ac057bb9f56efa1e9ebdf3ac64972191611e0e0b802410c96", 565338, "2019-05-20", 133),
)
```

Each tuple is `(raw hash, normalized anchor hash, game_pk, date, team_id)`. Sorting is raw hash, game PK, team. Every exact-key output row adds those identities to this metric:

```python
{
    "value": 50.0,
    "sample_size": 0,
    "quality": 0.0,
    "reason_codes": ["schedule_neutral_duplicate_gamepk_dependency"],
}
```

- [ ] **Step 2: Run red tests**

Expected: the current verified history projection has no `schedule_neutralizations`, and `_schedule_component()` computes ordinary history.

- [ ] **Step 3: Produce and consume the exact projection**

In `recompute_prior_history_sidecar()`, add exact-key rows only with authenticated disposition. Verify selected raw provenance and `canonical_sha256(anchor)` against both complete literals. Reject duplicates, changes, missing/unlisted targets.

In `_schedule_component()`, consult only this exact tuple and return the fixed neutral metric. Preserve missing-weight/no-redistribution. Do not edit weights/score/calibration. Tests assert complete raw hashes for 565535/566157 are known but absent from the allowlist; never use truncated prefixes.

- [ ] **Step 4: Verify and commit**

Run source-history and feature-engine tests, Ruff, diff check, then:

```bash
git add mlb_backtest/v52_sources/history.py mlb_backtest/feature_engine.py tests/test_v52_source_history.py tests/test_feature_engine.py
git commit -m "feat: neutralize three duplicate-dependent schedule metrics"
```

### Task 11: Close the exact execution-code set

**Files:**
- Modify: `mlb_backtest/execution_registry.py`
- Modify: `tests/test_execution_registry.py`

- [ ] **Step 1: Write the red equality test**

Update the expected `_BOUND_EXECUTION_CODE` set by adding exactly:

```python
{
    "mlb_backtest/v52_duplicate_gamepk_disposition.py",
    "schemas/v5_2_derived_disposition_sidecar.schema.json",
}
```

Require set equality, not subset containment, and retain every existing member.

- [ ] **Step 2: Run the exact closure test and see it fail**

Run the named closure tests in `tests.test_execution_registry`. Expected: the two new paths are unbound.

- [ ] **Step 3: Add the two paths and verify import/schema rebind behavior**

Append both to `_BOUND_EXECUTION_CODE`; ensure registry Git snapshots and schema binding cover them. Do not add fixtures, docs, protocol authorities, or protected duplicates to the execution-code tuple.

- [ ] **Step 4: Verify and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry -v
ruff check mlb_backtest/execution_registry.py tests/test_execution_registry.py
git diff --check
git add mlb_backtest/execution_registry.py tests/test_execution_registry.py
git commit -m "chore: bind duplicate disposition execution code"
```

### Task 12: Amend downstream plans without authorizing downstream execution

**Files:**
- Modify: `docs/superpowers/specs/2026-07-19-mlb-v5-2-source-finalization-cli-design.md`
- Modify: `docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md`
- Modify: `docs/superpowers/plans/2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md`
- Create: `docs/superpowers/specs/2026-07-23-v5-2-downstream-continuation-amendment.md`
- Create: `tests/test_v52_downstream_documentation.py`

**Narrow correction:** The approved 2026-07-12 design remains byte-identical
to its runtime-approved blob. Do not edit, re-pin, or reapprove that design.
The new non-runtime downstream-continuation
amendment supersedes only the downstream-continuation interpretation of that
design; it is not a runtime-approved design authority. This task does not
claim rollout, hashes, or that a `SOURCES_READY` inventory exists.

- [ ] **Step 1: Add documentation assertions before prose changes**

Extend the relevant documentation-binding tests to require: namespace-parametric finalization, exact disposition authority for the new contract, unchanged old-run selection behavior, a preflight rule that only a superseding-run `SOURCES_READY` inventory may be considered by a later continuation, explicit propagation of the neutralization projection, no implicit “latest” selection, and no Plan B/C artifact.

- [ ] **Step 2: Run those tests and confirm missing clauses**

Run the exact documentation/contract tests discovered with:

```bash
rg -n "source-finalization-cli|diagnostic-and-500-pick" tests
```

Then run only the matching local `unittest` modules. Expected: assertions for the new clauses fail.

- [ ] **Step 3: Add narrow amendment sections**

State that old behavior remains unchanged and that any later fit/OOF/card/target/outcome/500 work requires a separately approved plan explicitly selecting the superseding run. Do not add rollout results, production hashes, or imply that `SOURCES_READY` already exists.

Keep `docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md`
byte-identical: its approved runtime pin makes the former instruction to amend
that file inapplicable. Put the downstream-continuation interpretation in
`2026-07-23-v5-2-downstream-continuation-amendment.md` instead. That amendment
is non-runtime documentation only; it does not re-pin or reapprove the 7/12
design.

- [ ] **Step 4: Verify and commit**

Run matching tests and `git diff --check`, then:

```bash
git add \
  docs/superpowers/plans/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md \
  docs/superpowers/specs/2026-07-19-mlb-v5-2-source-finalization-cli-design.md \
  docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md \
  docs/superpowers/plans/2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md \
  docs/superpowers/specs/2026-07-23-v5-2-downstream-continuation-amendment.md \
  tests/test_v52_downstream_documentation.py
git commit -m "docs: bind downstream gates to superseding source run"
```

This Task 12 commit stages this governing plan once for its narrow correction.
Afterward, Task 13 must not stage the plan authority again.

### Task 13: Run the offline acceptance gate and prove old evidence immutability

**Files:**
- Test only; no production evidence writes

- [ ] **Step 1: Run every required focused module with bytecode disabled**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_source_schedule tests.test_v52_evidence tests.test_v52_schedule_inventory tests.test_v52_legacy_decision_inventory tests.test_v52_timezone_review tests.test_v52_game_evidence_inventory tests.test_v52_query_plan tests.test_v52_verify_sources tests.test_v52_snapshot_store tests.test_execution_registry tests.test_v52_source_finalize tests.test_v52_source_history tests.test_feature_engine tests.test_protocols tests.test_v52_acquisition_contract tests.test_verify_v52_design -v
```

Expected: all tests `OK`; all fixtures use temporary/local bytes; network-attempt count remains zero.

- [ ] **Step 2: Run lint and whitespace verification**

```bash
ruff check mlb_backtest tests
git diff --check
```

Expected: both exit 0.

- [ ] **Step 3: Recompute every fixed old authority and chain head**

Use `shasum -a 256` on the exact old RunSpec, contract, registration, attempt, quarantine receipt, quarantined raw, static batch artifact, checkpoint, and the two phase receipts. Assert every result equals the fixed literals in this plan/amendment, assert the raw byte count is `21182105`, and run:

```bash
shasum -a 256 -c "$V52_565534_AUDIT_DIR/old-evidence.sha256"
test "$(find data/v5_2_qd/historical audit/v5_2_qd/historical -type f | wc -l | tr -d ' ')" = "$(tr -d ' ' < "$V52_565534_AUDIT_DIR/old-evidence.count")"
git diff --exit-code -- protocols/v5_2_qd/historical_walkforward_500.json protocols/v5_2_qd/historical_source_acquisition.json data/v5_2_qd/historical audit/v5_2_qd/historical
git status --short -- "mlb_backtest/v52_query_plan 2.py" "tests/test_v52_query_plan 2.py" "schemas/v5_2_preflight.schema 2.json" "reports/v5_2_qd/walkforward_500_preflight 2.md"
```

Expected: the first command has no diff; the second shows the same pre-existing protected status and no staged entry. If any old byte, inode/link safety check, or protected status differs, stop and investigate; do not “repair” evidence by rewriting it.

- [ ] **Step 4: Review the final change set and commit only remaining implementation files**

Run `git status --short` and inspect every path. There should be no remaining intended code change after the task commits. If a small correction remains, stage only its explicit paths and make a focused commit; never stage the plan authority again, protected duplicates, or either old evidence tree.

- [ ] **Step 5: Stop at the no-network operator boundary**

Report the clean offline test/lint/diff results, exact old-evidence hash audit, commits, and the new protocol paths. Do **not** run `--plan-only`, create a new registry, register/publish the derived query in production, start acquisition, or advance any phase. A separately authorized operator session must perform those actions, inspect the one `565534` anchor and three neutralizations after `REFERENCE_INPUTS`, and independently decide whether to continue.

## Final review checkpoints

After Tasks 3, 6, 9, and 12, request an independent code/spec review before proceeding. Each review must answer:

1. Does the old default path and exact old literal branch remain unchanged?
2. Can any caller cross-bind an old RunSpec/contract/path with a new one?
3. Does the derived query remain raw-less and network-free with no attempts/reservations?
4. Are old evidence reads no-follow, regular, single-link, identity/hash checked, and immediately rebound?
5. Is `gamePk=565534` the only collapsible duplicate and are all predicates complete?
6. Is the derived ID completed/dispositioned and absent from pending/quarantine/split, while the old ID is absent everywhere in the new checkpoint?
7. Are exactly three complete-hash neutralizations present and adjacent games absent?
8. Does the exact execution closure contain the new module/schema and no extra path?

Any “no” blocks the next task. Review fixes follow the same red-test-first rule and receive their own focused commit.
