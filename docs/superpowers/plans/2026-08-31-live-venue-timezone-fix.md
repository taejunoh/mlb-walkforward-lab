# Live venue timezone repair

## Approved design and scope

User approved this bounded repair on 2026-08-31: request MLB venue timezone hydration, parse the actual venue-level `timeZone.id`, test against the saved official response, reuse existing season cache, then run the internal model once. No H6/Task12/Task14, odds or O/U development, broad refactor, or full exact suite.

## Global Constraints

- Edit only the isolated `live-venue-timezone-fix` worktree under `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees`.
- Preserve canonical v5.1 worktree and its failed-attempt evidence, all user changes, and the model card.
- No Documents, custody/production, privileged operations, installs, push, merge, PR, or unrelated network.
- Missing timezone must still fail: do not guess a timezone or relax validation.
- Controller alone owns cache copying and the single live model run. Implementation/tests must be offline.

### Task 1: Match the actual MLB venue API contract

**Files:**
- Modify `mlb_backtest/live_history.py`: `_venue_url` and `_parse_venue` only, except minimal directly necessary edits.
- Modify `tests/test_live_history.py` and directly affected offline test fixtures.
- Add a real response fixture under `tests/fixtures/` if appropriate.

1. Read repository instructions and the existing focused venue tests.
2. Add regression coverage using the actual saved response `/Users/taejunoh/Developer/LFG/2026-08-31-slate-readiness/venue-1-location-timezone.json` (SHA-256 `7d52062263ce66a95a7dadad7a60437cd6b09d51c6dc26e1b1420ebf9c6df25d`). Assert venue-level timezone `America/Los_Angeles`; check exact requested URL independently of the function under test. Verify missing/invalid timezone remains rejected.
3. Run focused new tests and record the expected RED failure on old code, without network or bytecode writes.
4. Change hydration to `location,timezone` and lookup to `venue["timeZone"]["id"]`. Update affected fake API payloads to the real response shape.
5. Run new tests, then related `test_live_history` and `test_daily_top3` tests (and directly impacted integration tests only). Use existing `/Users/taejunoh/.local/bin/python3`, `PYTHONDONTWRITEBYTECODE=1`; no package install or full exact suite.
6. Self-review, `git diff --check`, and commit only the owned implementation/test files on this isolated branch. Report exact commands, RED/GREEN outputs, changed files and commit to the controller; do not spawn agents or run the live CLI.

### Controller follow-through

Review the implementation against this scope. Copy only canonical `data/experimental_daily` and `audit/experimental_daily` to the new worktree as ordinary files (no symlinks/hardlinks), verify cache/receipt identity and no source changes. Run the repaired code once with `--root` set to this isolated worktree, `--date 2026-08-31`, and `--model-card audit/v5_1_bt/model_card.json`. Report actual output or exact remaining failure; experimental ML probability is not verified betting value or O/U support.
