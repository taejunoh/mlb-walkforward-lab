# Prospective ML Daily Flow Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Preserve the existing dirty worktree; no commits or network collection during implementation/tests.

Goal: Link existing pregame observations, shadow attachments, supplied market evidence, and postgame official results without backfilling predictions.

Architecture: A thin CLI/API coordinator with immutable content-addressed receipts and exact artifact references. Existing observation, shadow, settlement, result collection and market admission functions remain authoritative. No scheduler, database, model changes or automatic trace discovery.

Tech Stack: Python 3.11, existing mlb_backtest APIs, pytest.

Final verification (2026-09-16): 23 focused coordinator tests; combined coordinator/integration/results/shadow/shadow-report/all-day-ledger run: 80 passed, 13 subtests passed. Ruff and git diff --check passed. Independent specification and bounded quality rereviews approved. No real daily collection, prediction backfill, commit, push or scheduling was performed. Existing worktree retained.

## Task 1 — Coordinator and focused tests

Files: create `mlb_backtest/prospective_ml_daily_flow.py`, `tests/test_prospective_ml_daily_flow.py`, and `docs/prospective_ml_daily_flow.md`.

- [x] Write failing tests for pregame, postgame and read-only status.
- [x] Implement `pregame --root --ml-trace` with optional explicit market trace/raw capture/unavailable records. Normalize root to absolute. Persist baseline observations, use returned primary_claim_paths directly for shadow attachments, and record partial outcomes per game. Include unscored/excluded slate games. Preserve every durable output after partial failure.
- [x] Distinguish missing odds, explicit unavailable odds, saved-but-unlinked raw odds, and evaluated linked market evidence. No network or model runs in pregame/status. Do not fabricate prices or treat unavailable as evidence complete.
- [x] Implement immutable hash-bound receipts below `audit/prospective_ml_daily_flow/<date>/`. Validate exact references, receipt integrity and run association before side effects. Do not discover latest records via glob.
- [x] Implement `postgame --root --pregame-receipt` using only original primary claims and exact attachments. Invoke existing result collector, then paired shadow report. Require explicit previous postgame receipt for resume; carry exact settlement heads, skip settled games, and respect stale report rejection. FINAL settles, nonfinal is pending; never invoke scorer/observer/shadow snapshot here.
- [x] Implement read-only `status --root --pregame-receipt [--postgame-receipt]` with per-game ordered missing steps and permanent missed-pregame indications after start. Report workflow completion separately from complete evaluation evidence.
- [x] Cover before/equal/after-start and mid-processing start, partial failure, unscored games, forged receipts/references, wrong-run postgame receipt, nonfinal results, repeat/resume, no duplicate terminal fetches and status purity. Reuse real existing fixture/API paths where practical, not only mocked coordinator behavior.
- [x] Document exact CLI workflow and limitations; retain old CLI compatibility.
- [x] Run `/opt/homebrew/opt/python@3.11/bin/python3.11 -B -m pytest -q -p no:cacheprovider tests/test_prospective_ml_daily_flow.py` with bytecode disabled.

## Task 2 — Review and regression

- [x] Independent spec compliance review; fix and rereview findings.
- [x] Independent code quality review after spec approval; fix and rereview findings.
- [x] Run focused tests plus existing integration/results/shadow/shadow-report/all-day-ledger tests, then `git diff --check`.
- [x] Report actual verification results and remaining operational limitations. Do not claim real daily records were produced by fixture tests.
