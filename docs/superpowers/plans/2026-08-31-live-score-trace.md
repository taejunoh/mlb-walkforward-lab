# Live Score Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development to implement this bounded task with TDD, spec review, then code-quality review. User requested execution before games; reuse the current isolated worktree.

**Goal:** Save an independently verifiable explanation of each already-computed live score without changing the model or primary predictions.

**Architecture:** One optional in-memory trace sink in the existing service loop. One focused pure trace module handles canonical payloads and small offline scoring verification; the CLI persists a separate bound sidecar after the primary. Never rerun history construction to explain a saved prediction.

**Tech Stack:** Existing Python 3/unittest, production scoring/normalization helpers, trusted-root canonical JSON persistence.

**Worktree:** `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`

**Baseline:** HEAD `31ca83bd318b2e213b37aec25577be09e1bfc8de`; frozen model-card SHA `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`. `tests.test_daily_top3`: 22 passing before edits. Untracked data/audit/reports and earlier venue plan belong to existing work and must not be staged or changed.

## Task 1: Capture, persist, and verify a score trace

Files: create `mlb_backtest/live_top3_trace.py`, `tests/test_live_top3_trace.py`; modify `mlb_backtest/live_top3_service.py`, `mlb_backtest/daily_top3.py`, and narrowly extend `tests/test_live_top3_service.py`, `tests/test_daily_top3.py` / `tests/test_daily_top3_e2e.py` where integration belongs. Update `docs/experimental_live_top3.md` only to document the new artifact and its limited meaning.

- [ ] Write failing behavior tests before production edits. Use existing real feature/model fixtures and offline fetch doubles only at the external I/O boundary. Assert literal game IDs/counts/dispositions, primary equality with/without a sink, and captured raw SHA. A removal of sink append, wrong contribution sign, or omitted non-ranked row must fail the tests.
- [ ] Run those tests and record the expected RED failures. Exact command prefix: `PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest`.
- [ ] Implement an optional `trace_sink=None` argument in `build_live_top3_run`. Capture the existing `raw`, scoring result and probability in the scoring loop; attach rank/disposition after ranking. Do not modify `scoring.py`, `normalize.py`, `scoring_prep.py`, feature functions, or the model card.
- [ ] Implement the focused trace helpers. Minimum payload: schema version, research-only status, not-a-betting-recommendation true, primary relative path/content digest, card digest, games, canonical self digest. Reuse existing numeric/canonical utilities where appropriate. Each contribution is `COMPONENT_WEIGHT[c] * (home_score[c] - away_score[c])`; preserve the original total edge and compare with tolerance rather than changing summation.
- [ ] Implement the stored-row replay using `score_raw_game(raw_features, frozen_card.normalization)` and the existing probability/selection functions. Validate expected card/feature identity before accepting outputs. Reject malformed, tampered and non-finite data. This helper must not import the live service/fetcher or load history.
- [ ] Wire `run_daily_top3` to pass the sink and persist `<primary-stem>.trace.json`, bound to the actual returned primary report. Reread and verify the completed artifact; preserve old primary/readiness files and do not relabel them as traced.
- [ ] Extend integration tests for trace binding, unchanged primary content, trace write failure, tampering and legacy service callers. Run new tests plus `tests.test_daily_top3 tests.test_daily_top3_e2e tests.test_live_top3_service tests.test_live_top3_contracts`.
- [ ] Perform self-review and `git diff --check`, then report exact files/tests and remaining concerns. Do not run the live CLI or stage/commit until the controller has reviewed the scope.

## Task 2: Review and pregame handoff

- [ ] Independent read-only spec review against the design and Task 1; fix genuine gaps with a failing regression test.
- [ ] Independent code-quality review only after spec approval; address Critical/Important findings, do not broaden into unrelated infrastructure.
- [ ] Controller runs one proportional final verification, confirms frozen model and prior report digests, and commits only this patch's files if clean.
- [ ] If read-only network authorization is supplied and no run is already active, make exactly one new dated pregame capture using the supported CLI, preserve its exit/log/artifacts, verify trace binding and small offline replay, and explain actual signed contributions. No retry on a failed live capture without a diagnosed plan.
- [ ] If authorization or pregame time is unavailable, finish the code/fixture verification and explicitly report that there is no new real pregame trace. Never present fixture values or started-game observations as a fresh pick.

## Stop conditions

An unchanged model/report is mandatory. No attempt to manufacture trace data for old records, no bulk reconstruction, no betting/ROI claim from this patch. Larger feature or model changes and incomplete as-of history require a separate evaluation plan, not a deadline-driven patch.

## Execution record — 2026-08-31

Task 1 and the local review/verification portion of Task 2 are complete. The checklist above retains the originally planned steps; this record is the execution status.

- Initial RED was the expected missing trace module. A later regression reproduced the neutral-state validator defect (`ValueError not raised`) before its minimal fix; the focused 9 tests then passed.
- Independent spec review approved the final patch after adding exact-neutral, rehashed-tamper/nonfinite/bool, and actual CLI-orchestration fixture coverage. Independent code-quality review found no Critical or Important issues.
- Controller final verification: the five named unittest modules ran **111 tests in 21.268 seconds, OK**. `git diff --check` passed. These are offline software tests, not measured prediction accuracy.
- Scoring, normalization, feature construction, and the frozen card were not changed. The model-card SHA remains `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`.
- The preserved real primary JSON SHA remains `e551101bf83e86ba437550e94dfb9acb10286270acc089045a615732ce3a743d`; its readiness SHA remains `ca7eeaeb643c458d6aafec9fb48b750421d6772232e4dddf486909147fe05eb7`.
- No fresh real live capture or historical reconstruction was run. Separate read-only official-source network authorization is still unanswered; fixture traces must not be presented as real pregame predictions. The supported fresh capture remains pending, not failed or implicitly authorized.
- No independent live calibration, price/value validation, or O/U model was completed by this patch. Existing candidates remain research observations rather than betting recommendations.
