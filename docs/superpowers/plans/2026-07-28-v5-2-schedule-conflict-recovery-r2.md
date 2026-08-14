# Schedule-Conflict Recovery r2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seal a fresh r2 source-only recovery authority while preserving all former recovery receipts as immutable forensic evidence.

**Architecture:** Exact equality with the canonical r2 `SourceRunSpec` selects one bundle of design, plan, contract, raw, audit, and sources paths. Baseline and superseding retain their current bundles. All new evidence uses `_r2`; the former recovery authority is never resumed.

**Tech Stack:** Python 3, `unittest`, JSON Schema Draft 2020-12, canonical JSON, SHA-256, and Git blob binding.

---

## File map and guard

The r2 implementation starts from sealed old head `7344313` in a new branch and
worktree; it appends Stage1, Stage2, and Stage3. Stage1 changes
`mlb_backtest/protocols.py`, `mlb_backtest/v52_acquire.py`,
`mlb_backtest/v52_query_plan.py`, `mlb_backtest/v52_source_orchestration.py`,
`mlb_backtest/execution_registry.py`, `mlb_backtest/v52_game_evidence_inventory.py`,
`mlb_backtest/v52_schedule_conflict_recovery_disposition.py`,
`mlb_backtest/v52_schedule_inventory.py`, `mlb_backtest/v52_snapshot_store.py`,
`mlb_backtest/v52_verify_sources.py`, `mlb_backtest/v52_timezone_review.py`,
`mlb_backtest/v52_sources/history.py`, `mlb_backtest/verify_v52_design.py`,
all recovery-bearing schemas, matching tests and fixture (including the r2
schema-mapping and authority-TDD tests), and creates these r2 documents. It
creates no r2 protocol artifact. The old design, plan, RunSpec, and contract
remain byte-identical in the working tree and old branch.

The exact Stage1 recovery-schema allowlist is:
`schemas/v5_2_run_spec.schema.json`,
`schemas/v5_2_acquisition_contract.schema.json`,
`schemas/v5_2_acquisition_checkpoint.schema.json`,
`schemas/v5_2_game_evidence_inventory.schema.json`,
`schemas/v5_2_execution_receipt.schema.json`,
`schemas/v5_2_schedule_anchor_inventory.schema.json`,
`schemas/v5_2_source_inventory.schema.json`,
`schemas/v5_2_normalized_snapshot.schema.json`,
`schemas/v5_2_clean_source_replay_receipt.schema.json`,
`schemas/v5_2_source_receipt_commit.schema.json`,
`schemas/v5_2_source_review_receipt.schema.json`,
`schemas/v5_2_timezone_candidate.schema.json`,
`schemas/v5_2_timezone_review_receipt.schema.json`,
`schemas/v5_2_timezone_seal_receipt.schema.json`, and
`schemas/v5_2_venue_timezones.schema.json`. Update the bound-closure
assertions for this exact list; no schema outside it is staged in Stage1.

Before each stage, run `git status --short` and verify the r2 branch/worktree.
Never write, move, delete, or stage former `historical_recovery_schedule_conflicts`
evidence. Historical, superseding, former recovery, and protected parent paths
are read-only. No r2 command may target an old recovery protocol, raw, audit,
or source path. Before and after every write, capture both canonical parent
tree manifests, run full protected-inventory verification, record the former
recovery protocol/receipt forensic digest, and compare each post-write value
to its pre-write value. Before the first r2 write, require that both r2 raw and
r2 audit roots are absent. After any r2 failure, resume only the r2 authority.

`CODE_ROOT` is the clean implementation worktree at
`7344313efa5c8181bd96fbd09fef934e8b40c4a0`; `EXEC_ROOT` and `FORENSIC_ROOT`
are `/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest`.
The latter intentionally holds native legacy inodes and untracked forensic
evidence, so Task 4 never dirties CODE_ROOT with runtime evidence. Before
Stage1 require the exact base, an allowed r2 branch name, clean index/worktree,
and `git diff 7344313 --` empty for old design/plan/RunSpec/contract. Stage1
uses a written exact allowlist and requires
`git diff --cached --name-only | sort` to equal that list; do not use broad
`git add`. Stage2 and Stage3 each require a one-file equality assertion.

Bootstrap the clean implementation worktree before Stage1:

```bash
set -euo pipefail
export CODE_ROOT=/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2
export OLD_HEAD=7344313efa5c8181bd96fbd09fef934e8b40c4a0
test "$(git -C "$CODE_ROOT" rev-parse --show-toplevel)" = "$CODE_ROOT"
test "$(git -C "$CODE_ROOT" rev-parse HEAD)" = "$OLD_HEAD"
test -z "$(git -C "$CODE_ROOT" status --porcelain=v1 --untracked-files=all)"
git -C "$CODE_ROOT" diff --quiet "$OLD_HEAD" -- docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-run-design.md docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-run.md protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json
git -C "$CODE_ROOT" switch -c codex/v52-recovery-run-r2 "$OLD_HEAD"
test "$(git -C "$CODE_ROOT" rev-parse --abbrev-ref HEAD)" = codex/v52-recovery-run-r2
```

### Task 1: Seal r2 closure (Stage1)

**Files:**
- Create: this plan and `docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md`.
- Modify: every implementation, schema, runner, fixture, and test file named in the file map.

- [ ] **Step 1: Write RED regressions.**

Add a real r2 runtime test that creates canonical r2 RunSpec and contract only
inside a temporary Git repository, copies bound execution code, invokes
`_real_begin_runtime`, and requires r2 `DESIGN_ACCEPTED` then `ACQUIRING`; it
must not create repository protocol artifacts. Add exact named tests for the
selector plus forged authority, timezone gate, source disposition authorities,
registry namespace, source-finalize authority, and all 15 schema branches. Add
a raw-parent test calling `_ensure_canonical_raw_parent` for baseline,
superseding, and r2.

- [ ] **Step 2: Record the RED output.**

Run:

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_acquire.AcquisitionOrchestrationTests.test_real_recovery_runtime_initializes_and_advances_r2_registry \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_selector_routes_r2_and_forged_source_fails_closed \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_canonical_raw_parent_uses_r2_namespace \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_timezone_gate_uses_r2_authority_pair \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_recovery_disposition_catalog_uses_exact_r2_authority_pair \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_recovery_r2_registry_authority_pair_is_exact \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_finalization_accepts_only_the_r2_authority_path_pair \
  tests.test_verify_v52_design.DesignVerificationTest.test_all_recovery_r2_schema_branches_are_exact -v
```

Expected: RED exposes former superseding-contract inheritance and raw-namespace rejection. Retain logs; never alter an old receipt to pass.

The Step 4 command is the canonical GREEN target list. It retains the same
coverage with live exact IDs: temporary-Git r2 registry advancement, the r2
selector and forged-authority rejection, r2 raw namespace, timezone gate,
recovery disposition catalog, registry/finalization authority pairs, and all
15 exact schema branches.

- [ ] **Step 3: Implement minimal exact r2 dispatch.**

Define r2 ID, protocol filenames, design/plan paths, and raw/audit/source prefixes.
Make `expected_recovery_source_run()` use the r2 literal. One selector must
return design, plan, contract, raw, audit, and source paths together. Add RED
then GREEN coverage for `_load_required_timezone_gate` and
`_source_disposition_authorities`: r2 must pass r2 protocol paths, superseding
must retain superseding paths, and an unknown source authority must fail closed.
Replace recovery hardcodes in the mapped lifecycle modules with this bundle and
update each recovery schema branch to r2. Preserve three parent provenance
objects, v5.2.2 schedule restriction, four HTTP plus three raw-less entries,
and all baseline/superseding literals.

- [ ] **Step 4: Run GREEN closure validation.**

Run:

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_v52_acquire.AcquisitionOrchestrationTests.test_real_recovery_runtime_initializes_and_advances_r2_registry \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_selector_routes_r2_and_forged_source_fails_closed \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_canonical_raw_parent_uses_r2_namespace \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_timezone_gate_uses_r2_authority_pair \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_recovery_disposition_catalog_uses_exact_r2_authority_pair \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_recovery_r2_registry_authority_pair_is_exact \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_finalization_accepts_only_the_r2_authority_path_pair \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_verify_sources_rejects_non_r2_source_before_selecting_superseding_path \
  tests.test_v52_recovery_r2_tdd.RecoveryR2TddTests.test_orchestration_verify_and_timezone_use_r2_authority \
  tests.test_verify_v52_design.DesignVerificationTest.test_all_recovery_r2_schema_branches_are_exact -v
PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_task13_exact_suite.py --help
git diff --check
```

Expected: GREEN, help exits zero, r2 paths use `_r2`, and former recovery remains untouched.

- [ ] **Step 5: Commit Stage1.**

```bash
cat > /private/tmp/recovery-r2-stage1.paths <<'EOF'
docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md
docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-r2.md
mlb_backtest/protocols.py
mlb_backtest/v52_acquire.py
mlb_backtest/v52_query_plan.py
mlb_backtest/v52_source_orchestration.py
mlb_backtest/execution_registry.py
mlb_backtest/v52_game_evidence_inventory.py
mlb_backtest/v52_schedule_conflict_recovery_disposition.py
mlb_backtest/v52_schedule_inventory.py
mlb_backtest/v52_snapshot_store.py
mlb_backtest/v52_verify_sources.py
mlb_backtest/v52_timezone_review.py
mlb_backtest/v52_sources/history.py
mlb_backtest/verify_v52_design.py
schemas/v5_2_run_spec.schema.json
schemas/v5_2_acquisition_contract.schema.json
schemas/v5_2_acquisition_checkpoint.schema.json
schemas/v5_2_game_evidence_inventory.schema.json
schemas/v5_2_execution_receipt.schema.json
schemas/v5_2_schedule_anchor_inventory.schema.json
schemas/v5_2_source_inventory.schema.json
schemas/v5_2_normalized_snapshot.schema.json
schemas/v5_2_clean_source_replay_receipt.schema.json
schemas/v5_2_source_receipt_commit.schema.json
schemas/v5_2_source_review_receipt.schema.json
schemas/v5_2_timezone_candidate.schema.json
schemas/v5_2_timezone_review_receipt.schema.json
schemas/v5_2_timezone_seal_receipt.schema.json
schemas/v5_2_venue_timezones.schema.json
tests/test_v52_acquire.py
tests/test_execution_registry.py
tests/test_verify_v52_design.py
tests/test_v52_query_plan.py
tests/test_v52_game_evidence_inventory.py
tests/test_v52_schedule_inventory.py
tests/test_v52_snapshot_store.py
tests/test_v52_source_finalize.py
tests/test_v52_source_schedule.py
tests/test_v52_timezone_review.py
tests/test_v52_verify_sources.py
tests/v52_recovery_schedule_conflict_fixture.py
tests/test_v52_recovery_r2_schema_mapping.py
tests/test_v52_recovery_r2_tdd.py
EOF
git add --pathspec-from-file=/private/tmp/recovery-r2-stage1.paths
git diff --cached --name-only | LC_ALL=C sort > /private/tmp/recovery-r2-stage1.actual
LC_ALL=C sort /private/tmp/recovery-r2-stage1.paths > /private/tmp/recovery-r2-stage1.expected
cmp /private/tmp/recovery-r2-stage1.expected /private/tmp/recovery-r2-stage1.actual
git commit -m "feat: seal recovery r2 execution closure"
```

Expected: code, schemas, tests, and r2 documents only; no r2 RunSpec or contract.

### Task 2: Add r2 RunSpec only (Stage2)

**Files:**
- Create: `protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json`.

- [ ] **Step 1: Create canonical RunSpec.**

Use run ID `historical_walkforward_500_recovery_schedule_conflicts_r2`, required years 2018--2024, forbidden 2025, source-only downstream flags, r2 raw/sources/audit/reports prefixes, v5.2.1/v5.2.2 parser policy, three disposition names, and current minimum/optional sources. Include neither old recovery namespace nor contract.

- [ ] **Step 2: Verify and commit only the RunSpec.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_v52_query_plan tests.test_execution_registry tests.test_verify_v52_design -v
git diff --check
git add protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json
git commit -m "feat: add recovery r2 source authority"
git show --name-only --format= HEAD
test "$(git diff-tree --no-commit-id --name-only -r HEAD)" = "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json"
```

Expected: GREEN, no whitespace errors, and exactly the r2 RunSpec in Stage2.

### Task 3: Bind r2 contract only (Stage3)

**Files:**
- Create: `protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json`.

- [ ] **Step 1: Generate after Stage1 and Stage2 are committed.**

Bind only committed r2 design, plan, and RunSpec blobs plus unchanged exact parent provenance. Use only r2 ID, protocol paths, and prefixes; bind no old recovery receipt, namespace, design, plan, RunSpec, or contract.

- [ ] **Step 2: Verify and commit only the contract.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_execution_registry tests.test_v52_query_plan tests.test_verify_v52_design -v
git diff --check
git add protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json
git commit -m "feat: bind recovery r2 acquisition contract"
git show --name-only --format= HEAD
test "$(git diff-tree --no-commit-id --name-only -r HEAD)" = "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json"
```

Expected: GREEN and exactly the r2 contract in Stage3. No contract-bound file may change afterward; a correction needs a fresh authority.

- [ ] **Step 3: Freeze contract-head and blob guard.**

```bash
test -z "$(git status --porcelain=v1 --untracked-files=all)"
export CONTRACT_HEAD=$(git rev-parse HEAD)
git status --porcelain=v1
git ls-tree "$CONTRACT_HEAD" -- docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-r2.md protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json > /private/tmp/recovery-r2-bound-blobs.txt
```

Expected: the status output is empty and the blob map is retained. Before and
after every Task 4 command, assert `git rev-parse HEAD` equals `$CONTRACT_HEAD`,
`git status --porcelain=v1 --untracked-files=no` is empty, and
`cmp /private/tmp/recovery-r2-bound-blobs.txt <(git ls-tree "$CONTRACT_HEAD" -- docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-r2.md protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json)` succeeds.

- [ ] **Step 4: Move the protected execution worktree to the sealed r2 head.**

```bash
set -euo pipefail
export OLD_HEAD=7344313efa5c8181bd96fbd09fef934e8b40c4a0
git -C "$EXEC_ROOT" diff --quiet
test -z "$(git -C "$EXEC_ROOT" status --porcelain=v1 --untracked-files=no)"
test "$(git -C "$EXEC_ROOT" rev-parse HEAD)" = "$OLD_HEAD"
test "$(git -C "$CODE_ROOT" rev-parse 7344313)" = "$OLD_HEAD"
git -C "$EXEC_ROOT" switch -c codex/v52-recovery-protected-execution-r2 "$CONTRACT_HEAD"
test "$(git -C "$EXEC_ROOT" rev-parse HEAD)" = "$CONTRACT_HEAD"
test "$(git -C "$CODE_ROOT" rev-parse 7344313)" = "$OLD_HEAD"
cd "$EXEC_ROOT"
```

Expected: the old branch pointer remains sealed at `$OLD_HEAD`; only the
protected execution worktree changes branch, so Task4 output is untracked there.

### Task 4: Execute fresh r2 source evidence

**Files:**
- Create only under `data/v5_2_qd/historical_recovery_schedule_conflicts_r2/` and `audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/`.
- Create: `audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/venue_timezones.json`.

- [ ] **Step 0: Capture the mandatory pre/post write guard.**

Before every write command in Steps 1--3, run the macOS guard below before and
after that one command. Genesis requires every r2 raw/audit/sources/reports/
mapping root absent; later resume requires them present and uses only r2
arguments. The guard writes only under `/private/tmp`, validates the two parent
canonical manifests and protected inventory in `EXEC_ROOT`, and proves the
external former evidence has not changed.

```bash
set -euo pipefail
export CODE_ROOT=/Users/taejunoh/Documents/MLB Analysis/.worktrees/v52-recovery-run-r2
export EXEC_ROOT=/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest
export FORENSIC_ROOT="$EXEC_ROOT"
export GUARD=/private/tmp/recovery-r2-operator-guard.sh
cat > "$GUARD" <<'SH'
#!/bin/zsh
set -euo pipefail
mode="$1"; output="$2"
if [[ "$mode" == genesis ]]; then
  test ! -e "$EXEC_ROOT/data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw"
  test ! -e "$EXEC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts_r2"
  test ! -e "$EXEC_ROOT/data/v5_2_qd/historical_recovery_schedule_conflicts_r2/sources"
  test ! -e "$EXEC_ROOT/reports/v5_2_qd/historical_recovery_schedule_conflicts_r2"
fi
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$CODE_ROOT" python3 - "$output.parents.json" <<'PY'
import dataclasses, json, sys
from pathlib import Path
from mlb_backtest.v52_recovery_parent_evidence import capture_tree_fingerprint
from mlb_backtest.verify_v52_design import verify_protected_inventory
from mlb_backtest.v52_protected_inventory import verify_protected_raw_inventory
root = Path(__import__('os').environ['EXEC_ROOT']).resolve()
verify_protected_inventory(root, root / 'protocols/v5_2_qd/v51_protected_inventory.json')
verify_protected_raw_inventory(root, root / 'audit/v5_1_bt/protected_raw_inventory.json')
groups = {
    'historical': ('data/v5_2_qd/historical', 'audit/v5_2_qd/historical'),
    'superseding': ('data/v5_2_qd/historical_superseding_565534', 'audit/v5_2_qd/historical_superseding_565534'),
}
payload = {name: [dataclasses.asdict(capture_tree_fingerprint(root, Path(item))) for item in items] for name, items in groups.items()}
for values in payload.values():
    for value in values:
        value['relative'] = str(value['relative'])
Path(sys.argv[1]).write_text(json.dumps(payload, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
PY
test -f "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/phase_receipts/000000-DESIGN_ACCEPTED-c10c89bd8739e883b2251032d15e9e41995c7c6c1cfce9cb0b2f0a57e7354bc7.json"
test -f "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/phase_receipts/000001-ACQUIRING-514ed262068cbc40477b4bd157d66266260d5834704a38fd169133929df22036.json"
test -f "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/.registry.lock"
test -f "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/.acquisition-runner.lock"
test ! -e "$FORENSIC_ROOT/data/v5_2_qd/historical_recovery_schedule_conflicts/raw"
test ! -e "$FORENSIC_ROOT/data/v5_2_qd/historical_recovery_schedule_conflicts/sources"
test ! -e "$FORENSIC_ROOT/reports/v5_2_qd/historical_recovery_schedule_conflicts"
test "$(shasum -a 256 "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/phase_receipts/000000-DESIGN_ACCEPTED-c10c89bd8739e883b2251032d15e9e41995c7c6c1cfce9cb0b2f0a57e7354bc7.json" | awk '{print $1}')" = c10c89bd8739e883b2251032d15e9e41995c7c6c1cfce9cb0b2f0a57e7354bc7
test "$(shasum -a 256 "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts/execution/phase_receipts/000001-ACQUIRING-514ed262068cbc40477b4bd157d66266260d5834704a38fd169133929df22036.json" | awk '{print $1}')" = 514ed262068cbc40477b4bd157d66266260d5834704a38fd169133929df22036
shasum -a 256 "$CODE_ROOT/protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json" "$CODE_ROOT/protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json" "$CODE_ROOT/docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-run-design.md" "$CODE_ROOT/docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-run.md" > "$output.forensic.sha256"
find "$FORENSIC_ROOT/audit/v5_2_qd/historical_recovery_schedule_conflicts" -type f -exec shasum -a 256 {} + | LC_ALL=C sort >> "$output.forensic.sha256"
SH
chmod 700 "$GUARD"
guarded_write() {
  local mode="$1" label="$2"; shift 2
  assert_authority() {
    test "$(git -C "$CODE_ROOT" rev-parse HEAD)" = "$CONTRACT_HEAD"
    test "$(git -C "$EXEC_ROOT" rev-parse HEAD)" = "$CONTRACT_HEAD"
    test -z "$(git -C "$CODE_ROOT" status --porcelain=v1 --untracked-files=no)"
    test -z "$(git -C "$EXEC_ROOT" status --porcelain=v1 --untracked-files=no)"
    cmp /private/tmp/recovery-r2-bound-blobs.txt <(git -C "$CODE_ROOT" ls-tree "$CONTRACT_HEAD" -- docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-r2.md protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json)
    cmp /private/tmp/recovery-r2-bound-blobs.txt <(git -C "$EXEC_ROOT" ls-tree "$CONTRACT_HEAD" -- docs/superpowers/specs/2026-07-28-v5-2-schedule-conflict-recovery-r2-design.md docs/superpowers/plans/2026-07-28-v5-2-schedule-conflict-recovery-r2.md protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json)
  }
  assert_authority
  "$GUARD" "$mode" "/private/tmp/recovery-r2-${label}.before"
  local rc
  if "$@"; then rc=0; else rc=$?; fi
  "$GUARD" resume "/private/tmp/recovery-r2-${label}.after"
  cmp "/private/tmp/recovery-r2-${label}.before.parents.json" "/private/tmp/recovery-r2-${label}.after.parents.json"
  cmp "/private/tmp/recovery-r2-${label}.before.forensic.sha256" "/private/tmp/recovery-r2-${label}.after.forensic.sha256"
  assert_authority
  return "$rc"
}
```

- [ ] **Step 1: Run r2 plan-only.**

```bash
cd "$EXEC_ROOT"
guarded_write genesis plan-only env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --root . \
  --run-spec protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json \
  --acquisition-contract protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json \
  --years 2018:2024 --mode hybrid --legacy-root data/v5_1_bt/raw \
  --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --plan-only
```

Expected: r2 design/plan/contract authenticate and old paths are untouched.


- [ ] **Step 2: Run reference and full acquisition.**

```bash
export RECOVERY_RUN_SPEC=protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json
export RECOVERY_CONTRACT=protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json
export RECOVERY_AUDIT=audit/v5_2_qd/historical_recovery_schedule_conflicts_r2
export RECOVERY_RAW=data/v5_2_qd/historical_recovery_schedule_conflicts_r2/raw
export SOURCE_ROOT=data/v5_2_qd/historical_recovery_schedule_conflicts_r2/sources
cd "$EXEC_ROOT"
guarded_write resume reference-inputs env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --years 2018:2024 --mode hybrid --legacy-root data/v5_1_bt/raw --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --resume --stop-after reference-inputs
guarded_write resume full-acquisition env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --years 2018:2024 --mode hybrid --legacy-root data/v5_1_bt/raw --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --resume
```

Expected: four HTTP and three raw-less schedule entries; all failure evidence remains r2.

- [ ] **Step 3: Run timezone, publish, replay, review, status, and advance.**

```bash
STATUS_JSON=$(PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize status --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --json)
TIMEZONE_CHECKPOINT=$(python3 -c 'import json,sys; value=json.loads(sys.argv[1]); assert value["status"] == "OK" and value["phase"] == "ACQUIRING"; c=value["result"]["checkpoint"]; assert c["stage"] == "REMAINING" and c["pending_count"] == 0 and c["quarantine_count"] == 0 and isinstance(c["head"], str); print("audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/execution/acquisition_checkpoints/{:08d}-{}.json".format(int(c["sequence"]), c["head"]))' "$STATUS_JSON")
TIMEZONE_BATCH_IDS=$(python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); assert len(value["query_batches"]) >= 2; print(" ".join(item["batch_id"] for item in value["query_batches"][:2]))' "$TIMEZONE_CHECKPOINT")
set -- $TIMEZONE_BATCH_IDS
guarded_write resume timezone-candidate env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review candidate --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --evidence-root "$RECOVERY_RAW" --batch-id "$1" --batch-id "$2" --output "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json"
guarded_write resume timezone-semantic-review env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review review --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --candidate "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json" --output "$RECOVERY_AUDIT/acquisition/timezone_reviews/semantic.json" --reviewer-identity timezone-semantic-auditor --reviewer-role TIMEZONE_SEMANTIC_REVIEW --decision APPROVED
guarded_write resume timezone-coverage-review env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review review --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --candidate "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json" --output "$RECOVERY_AUDIT/acquisition/timezone_reviews/coverage.json" --reviewer-identity timezone-coverage-auditor --reviewer-role TIMEZONE_COVERAGE_REVIEW --decision APPROVED
guarded_write resume timezone-seal env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_timezone_review seal --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --candidate "$RECOVERY_AUDIT/acquisition/venue_timezone_candidate.json" --review-receipt "$RECOVERY_AUDIT/acquisition/timezone_reviews/semantic.json" --review-receipt "$RECOVERY_AUDIT/acquisition/timezone_reviews/coverage.json" --output "$RECOVERY_AUDIT/acquisition/venue_timezones.json" --receipt-output "$RECOVERY_AUDIT/acquisition/venue_timezone_seal.json"
guarded_write resume source-publish env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize publish --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --lock-owner recovery-r2-source-publisher
guarded_write resume replay-one env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize replay --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --write-receipt "$RECOVERY_AUDIT/acquisition/source_replays/replay-one.json" --write-commit "$RECOVERY_AUDIT/acquisition/source_replays/replay-one.commit.json"
guarded_write resume replay-two env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize replay --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --write-receipt "$RECOVERY_AUDIT/acquisition/source_replays/replay-two.json" --write-commit "$RECOVERY_AUDIT/acquisition/source_replays/replay-two.commit.json"
guarded_write resume source-review-dir mkdir -p "$RECOVERY_AUDIT/acquisition/source_reviews"
guarded_write resume canonical-query-ids env PYTHONDONTWRITEBYTECODE=1 python3 -c 'import json,sys; from pathlib import Path; value=json.loads(Path(sys.argv[1]).read_text()); ids=sorted(set(value["logical_query_ids"])); Path(sys.argv[2]).write_text(json.dumps({"query_ids":ids}, sort_keys=True, separators=(",",":"))+"\n", encoding="utf-8")' "$SOURCE_ROOT/source_inventory.json" "$RECOVERY_AUDIT/acquisition/source_reviews/canonical_query_ids.json"
guarded_write resume source-review env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize review --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --reviewer-identity independent-source-auditor --reviewer-role independent-source-auditor --decision APPROVED --query-ids-file "$RECOVERY_AUDIT/acquisition/source_reviews/canonical_query_ids.json" --write-receipt "$RECOVERY_AUDIT/acquisition/source_reviews/review.json" --write-commit "$RECOVERY_AUDIT/acquisition/source_reviews/review.commit.json"
STATUS_JSON=$(PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize status --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --json)
CHECKPOINT=$(python3 -c 'import json,sys; value=json.loads(sys.argv[1]); assert value["status"] == "OK" and value["phase"] == "ACQUIRING"; c=value["result"]["checkpoint"]; assert c["stage"] == "REMAINING" and c["pending_count"] == 0 and c["quarantine_count"] == 0 and isinstance(c["head"], str); print("audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/execution/acquisition_checkpoints/{:08d}-{}.json".format(int(c["sequence"]), c["head"]))' "$STATUS_JSON")
python3 -c 'import json,sys; value=json.load(open(sys.argv[1])); assert len(value["query_batches"]) >= 4' "$CHECKPOINT"
guarded_write resume sources-ready-advance env PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_source_finalize advance --root . --run-spec "$RECOVERY_RUN_SPEC" --acquisition-contract "$RECOVERY_CONTRACT" --protected-inventory audit/v5_1_bt/protected_raw_inventory.json --review-receipt "$RECOVERY_AUDIT/acquisition/source_reviews/review.commit.json" --replay-receipt "$RECOVERY_AUDIT/acquisition/source_replays/replay-one.commit.json" --replay-receipt "$RECOVERY_AUDIT/acquisition/source_replays/replay-two.commit.json" --checkpoint "$CHECKPOINT" --inventory "$SOURCE_ROOT/source_inventory.json"
```

Expected: every output has an r2 prefix; both replays have distinct UUIDv4 IDs; phase is `SOURCES_READY`.

- [ ] **Step 4: Run final exact validation.**

```bash
cd "$CODE_ROOT"
PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_task13_exact_suite.py --repo . --v51-input-root "$EXEC_ROOT" --old-v52-evidence-root "$EXEC_ROOT"
ruff check mlb_backtest tests
git diff --check
git status --short
```

Expected: exact suite GREEN; former recovery, baseline, superseding, and protected parents unchanged; only r2 lifecycle evidence is new.

## Plan self-review

**Spec coverage:** Tasks 1--3 cover forensic preservation, r2 selector/paths, source semantics, parent provenance, schemas/tests, strict commit order, and post-binding immutability. Task 4 covers plan-only, reference, full acquisition, timezone, publish, replay, review, advance, and exact final validation.

**Placeholder scan:** No `TODO` or `TBD` occurs. Commands identify r2 artifact paths when they execute authority code.

**Consistency review:** The run ID, filenames, and raw/audit/source `_r2` suffix match the design and every command. Baseline and superseding remain distinct; former recovery is forensic-only.
