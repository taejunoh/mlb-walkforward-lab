# Live history ID order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Fix the verified chronological-versus-numeric ID order contract conflict and execute one model attempt.

**Architecture:** Keep chronological feature rows intact. Independently sort the verification ID tuple at the caller; retain the shared validator.

**Tech Stack:** Existing Python 3.11, unittest, local v5.1 experimental live pipeline.

**Spec:** docs/superpowers/specs/2026-08-31-live-history-id-order-design.md

## Global Constraints

Work only in `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`; always specify this cwd. No Documents, sudo, installs, network from the worker, push, merge, PR, custody/production/Task12/Task14 or forbidden manifest access. Preserve all caches, evidence, untracked user files, model card and feature formulas. No live execution from worker. No subagents from worker. Main handles review and one live attempt. Use apply_patch for edits and PYTHONDONTWRITEBYTECODE=1 for Python. User AGENTS routing applies: Luna implementation, Terra review.

### Task 1: Separate verification ID order from feature row order

**Files:** Modify only `mlb_backtest/live_history.py` and `tests/test_live_history.py`.

- [ ] Run baseline: `PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_live_history tests.test_daily_top3_e2e`. Expected pass on the unchanged source.
- [ ] Add a regression near `test_schedule_history_builder_indexes_statcast_pitches_once` using the actual builder and two valid schedule/pitch records. First date `2026-03-25`, ID `823244`; second `2026-03-26`, ID `823081`; reuse established fixture field shapes and valid venue reference. Supply schedule input in reverse date order so the test independently proves row sorting too. Assert literals:

```python
self.assertEqual(tuple(row["game_pk"] for row in result.rows), (823244, 823081))
self.assertEqual(result.expected_game_pks, (823081, 823244))
```

- [ ] Run that focused test against unchanged production and record the expected `expected_game_pks must be sorted unique positive integers` failure.
- [ ] Change only the verification tuple construction:

```python
expected = tuple(sorted(row["game_pk"] for row in rows))
```

- [ ] Run the focused test, then the two baseline modules once; expected pass. Confirm chronological row sort and shared validator unchanged. Do not silently use set(), refactor, change model behavior, or adjust E2E golden values.
- [ ] Run `git diff --check`; self-review; commit only the two owned files with message `Fix live history verification game ID ordering`.
- [ ] Write task report containing RED/GREEN command and observed output, changed files, commit and concerns. Return compact status to controller.

## Controller completion

Obtain scoped task review and final integration review; no duplicate test suites. Verify original/model-card identity and no active duplicate process. Record a single live command/start/outcome under the existing LFG attempt evidence directory. On failure preserve evidence and diagnose, no retry. On success inspect actual report classifications/probabilities/source bindings and state experimental ML-only limitations. Keep isolated worktree/evidence; do not integrate branches.
