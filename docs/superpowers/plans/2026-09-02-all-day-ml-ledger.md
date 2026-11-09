# All-day ML Ledger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development and test-driven-development. Preserve the current dirty worktree. No commits, push, refits, holdouts or prospective-claim backfill.

**Goal:** Record every game, including day games, with existing forecasts, pregame quote evidence, missing reasons and later official outcomes, while retaining evidence classes.

**Architecture:** A standalone all-day ledger reuses authenticated ML/market authorities and official-result capture/admission. It freezes forecast selection before loading outcomes. Explicit source inputs produce a content-addressed JSON record plus a readable Markdown table; the strict prospective pipeline remains unchanged.

**Tech Stack:** Python 3.11, existing TrustedRoot/source evidence helpers, canonical JSON/SHA-256, unittest, Ruff.

## Task 1 — Standalone all-day recording

**Create:** `mlb_backtest/all_day_ml_ledger.py`

**Create:** `tests/test_all_day_ml_ledger.py`

Do not modify existing modules without reporting a concrete integration need.

```python
collect_daily_ml_ledger(
    root, *, ml_traces, prospective_reports=(), market_traces=(),
    market_capture_outcomes=(), market_supersession=None,
    result_receipts=None, fetcher=fetch_live_http, durable_clock=utc_now,
) -> CollectedDailyLedger
```

`result_receipts` is an explicit mapping from gamePk to relative receipt path;
`None` means no pre-supplied outcomes. The injectable transport/clock is for
hermetic tests; CLI accepts neither arbitrary URLs nor a historical clock.

```sh
python -m mlb_backtest.all_day_ml_ledger collect --root ROOT \
  --ml-trace TRACE --ml-trace OTHER_TRACE \
  --prospective-report REPORT --market-trace MARKET_TRACE \
  --market-capture-outcome OUTCOME --market-supersession SELECTOR \
  --result-receipt GAMEPK=RECEIPT
```

- [x] RED: add real sealed-input fixture tests and observe missing module or behavior failure. Include a full slate with a historical day forecast, an unscored game with diagnostic numeric probability, and a current primary. The report must contain all games, never only ranked picks.
- [x] Admit every explicit ML trace with `load_ml_authority`; read its linked validated primary report to distinguish scored ranked rows from unscored/excluded rows. Reject cross-date/conflicting identities, duplicate primary claims, invalid digests and invalid explicitly supplied market/prospective inputs before any HTTP request.
- [x] Freeze selection before outcome loading: exact primary snapshot first; otherwise latest genuinely pregame eligible scored version by the documented timestamp/hash/path key. Include authenticated eligible `SCORED_NOT_TOP3` rows, not only ranked preliminary rows. Keep every supplied prediction version, source refs, model ID, reasons and diagnostic-only values. No result-dependent selection or promotion of an explicitly unscored/ineligible probability.
- [x] Validate and preserve market trace/report evidence with existing validators. For selected quote require exact selected forecast trace/date/side binding and strictly pre-start capture/evaluation. Preserve book/side/price/opposing price/time/source and old quote versions. Label after-forecast pregame quotes, missing quotes and incompatible quote reasons. Never use superseded wrong-unit captures.
- [x] Separately admit explicit raw-capture outcomes through `load_capture_outcome` and an authenticated, date/type/active/do-not-use checked supersession selector. Preserve available paired ML quotes and unavailable book reasons. Match normalized away/home, same UTC date, absolute start delta <=60 seconds and exactly one game; retain both starts/delta/policy. Check pregame timing against the official start. Do not manufacture a market PLAY/ML-trace binding. Test valid +60 seconds, ambiguity, >60 seconds, post-start capture, superseded input and tampering before HTTP.
- [x] Validate explicit existing result receipts. Fetch only games missing supplied receipts, once each, with the fixed official single-game schedule URL. Persist with existing `new_observation_id`/`_persist_observation` and re-admit using `load_official_result_evidence`; require exact date/game/team identity. Use existing helper-supported raw source/query kind without changing its schema.
- [x] Final non-tied results get winner/score and selected forecast win/loss. Nonfinal, exceptional, malformed, HTTP or persistence failures remain explicit pending/error rows, never erased or counted as losses. Preserve successful captures if a later game fails; errors produce nonzero CLI status with useful saved paths.
- [x] Persist immutable content-addressed JSON and Markdown under `reports/experimental_all_day_ml_ledger/<date>/`. Exact identical records may be idempotent; different captures create different versions. Do not overwrite prior versions, source artifacts or primary/head records. Output all-game/cohort/result counts and artifact paths.
- [x] GREEN: test source tampering before HTTP, same-game versions in reverse input order, selection independence from winner, post-start versions, prospective linkage, missing/unscored cases, matched/mismatched/superseded quote evidence, Final identity/error states, repeated run preservation, partial result and final report write failure. Use real admission/persistence, fake only HTTP or the exact failing disk boundary.
- [x] Run new tests plus affected authority, prospective result/settlement/report/integration tests; Ruff on new files; `git diff --check`.

## Task 2 — Review and actual daily record

**Create:** `docs/all_day_ml_ledger.md`

- [x] Independent specification review, then independent code-quality review. Reproduce and fix material findings within this scope.
- [x] Controller runs focused tests, Ruff and original-source hash comparisons. Preserve the existing linked worktree/HEAD.
- [x] Inventory explicit September 2 ML traces and valid active market inputs. Reuse the seven previously captured official result receipts, then run one collection for the remaining full-slate games. No retry loop or model execution.
- [x] Verify every daily game has a row, original probabilities remain unchanged, source classes are explicit, missing fields remain null/reasoned, and no prospective claims/heads changed.
- [x] Document exact reusable manual command/input list, source selection, class meanings, failure recovery and actual artifact paths. Report the full-day result in Korean with day games visible and no profitability claim.

## Baseline

Existing isolated branch: `codex/live-venue-timezone-fix`.
HEAD: `3ab6cd4c3ca878fccaaaf155a7adb8c33c4a185d`.
32 focused existing prospective/result/report/integration tests passed before edits.
The observer/results/settlement and existing user changes were SHA-256 recorded
before implementation; they are not owned by this new task.
