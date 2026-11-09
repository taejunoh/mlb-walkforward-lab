# Postseason ML research verification — 2026-09-30

Status: **IMPLEMENTED_TESTED_NOT_ACTIVATED** (2026-09-30T13:59:47Z). This document is a verification ledger,
not authorization to run live sources or adopt the mode in recurring automation.

## Approved scope

The user approved implementation and offline testing of the existing postseason
moneyline research design without further intermediate approval. The work uses
the existing `live-venue-timezone-fix` worktree and preserves unrelated dirty
and untracked changes. No commit, push, merge, PR, live model run, network
collection, actual bet, automation change, retraining, calibration change, or
totals implementation is included.

The implementation adds explicit `postseason-research-v1` routing, full-slate
F/D/L/W research evaluation, authenticated R/F/D/L/W recent history, a separate
prospective first-claim cohort, and official-result settlement with descriptive
metrics. All output remains research-only and out of domain; `VERIFIED` means
input readiness, not validated probability or betting eligibility.

## Independent review

- Task 1, acquisition/history/features: accepted after independent review and
  a five-import test-collection supplement.
- Task 2, report/trace/daily routing: accepted after fixes for exact consumed
  source closure and compatibility of optional legacy receipt queries.
- Task 3, prospective claims and settlement: all four independently reproduced
  Important findings are fixed and accepted by the original reviewer after
  reading the 650-line delta and independently running 5 tests (14.830s).
  The fixes pin first-primary bytes, enforce actual durable-publication timing,
  require complete-slate receipt coverage, and preserve readable/resumable
  partial receipts after pending-result fetch failure.
- Final cross-owner integration review: **PASS**, with no unresolved Critical
  or Important finding and no new actionable Minor finding. Review record:
  `.superpowers/sdd/2026-09-30-postseason-ml-research/integration-review-final.md`.
- An interim integration review additionally reproduced lexical UTC comparison
  errors for mixed fractional precision in Task 2. The parsed-instant repair
  passed owner regression (105 tests/107 subtests), 11 contracts unittest cases,
  and independent re-review with 2 tests/3 subtests. The public timestamp format
  and schema are unchanged. Final cross-owner acceptance includes this repair.

Review packages, findings, owner reports, and pre-turn source snapshots are
retained in `.superpowers/sdd/2026-09-30-postseason-ml-research/`. Reviews compare
with those snapshots, not with Git HEAD, because the worktree was already dirty.

## Test evidence

The pre-turn focused regular baseline was 348 passing tests. An intermediate
regular regression returned 348 tests and 340 subtests passing in 55.56 seconds.
The coordinator's explicit 29-file combined checkpoint returned **497 tests and
424 subtests passing in 207.10 seconds**, with file hashes unchanged throughout
that run. This checkpoint predates the four Task 3 review fixes and is **not
final acceptance**.

After all implementation fixes, the same explicit 29-file selection returned
**508 passed, 427 subtests passed in 226.86 seconds**. All 23 owned source,
test, and runbook hashes were unchanged throughout that run. Independent
Task 3 re-review and final integration acceptance subsequently passed.
The subsequent runbook-only addition documents the already implemented
fail-closed behavior for an interrupted claim reservation.

The offline end-to-end fixture exercises actual frozen scoring for all four
supported postseason types, persistence and strict loading, pre-start primary
claims, official-result settlement, isolated metrics, and terminal resume/status.
No live source data was fetched. Exact-neutral behavior is a synthetic raw-feature
boundary test; it is not claimed as a fully source-authenticated neutral forecast.

All final regression commands use an explicit safe test-file list, scoped
`TMPDIR`, `PYTHONDONTWRITEBYTECODE=1`, `python3 -B`, and pytest without its cache
provider. No unrestricted historical test discovery is used.

## Preservation checks

At the combined checkpoint, `normalize.py`, `scoring.py`, `scoring_prep.py`,
`live_model_card.py`, the frozen model card, legacy prospective modules, tracked
fixture payloads, and selected existing Sep16 settlement/review records matched
their baseline hashes. `git diff --check` passed. HEAD remained
`ef5680beaf37e16686bd27f9c4bf78441f9519a8`. These preservation checks were
repeated after the 508-test final-tree run with identical results; the Git
index remained unchanged and no tracked fixture changes were present.

## Limitations and disclosed deviation

- The optional historical parser file `tests/test_v52_source_schedule.py` could
  not collect because immutable recovery-parent evidence is absent from this
  checkout. No external authority was supplied, reconstructed, or accessed to
  bypass this restriction. This file and the whole-repository suite are not
  claimed as passing; focused live schedule/source regressions are included.
- Full-season live runtime/memory, real network behavior, multiple-process
  contention, and power-loss recovery were not experimentally measured.
- Task 2's initial RED test omitted scoped TMPDIR and temporarily copied a
  fixture model card into an OS-managed TemporaryDirectory, automatically
  removed on context exit. It did not use live/user records or network. Later
  tests and all coordinator integration runs used workspace-scoped TMPDIR.
- Recording research outcomes does not automatically retrain or validate the
  frozen model, and does not establish betting profitability.
- An interrupted first-claim identity reservation fails closed and is not
  automatically repaired by a fresh invocation. Its partial evidence must be
  preserved for separately scoped recovery; no backdated claim is allowed.

## Handoff

The runbook is `docs/postseason_ml_research.md`. It documents exact trace and
receipt linkage and immutable primary preservation. Live smoke testing and
automation adoption remain outside this implementation approval. No current-day
prediction or recommendation is implied by offline test completion.

## Exact focused regression selection

Run from the approved worktree above. This exact selection produced the
final-tree 508-test result recorded above.

```sh
TMPDIR="$PWD/.pytest-tmp-baseline" PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -p no:cacheprovider \
  tests/test_live_sources.py \
  tests/test_live_history.py \
  tests/test_live_features.py \
  tests/test_live_top3_contracts.py \
  tests/test_live_top3_trace.py \
  tests/test_live_top3_service.py \
  tests/test_daily_top3.py \
  tests/test_daily_top3_e2e.py \
  tests/test_prospective_ml_daily_flow.py \
  tests/test_prospective_ml_settlement.py \
  tests/test_market_play_authorities.py \
  tests/test_daily_totals.py \
  tests/test_prospective_ml_observation.py \
  tests/test_prospective_ml_results.py \
  tests/test_prospective_ml_report_integrity.py \
  tests/test_prospective_ml_integration.py \
  tests/test_prospective_ml_persistence_integrity.py \
  tests/test_prospective_ml_shadow.py \
  tests/test_prospective_ml_shadow_report.py \
  tests/test_statcast.py \
  tests/test_v52_source_statcast.py \
  tests/test_postseason_research_sources.py \
  tests/test_postseason_research_history.py \
  tests/test_postseason_research_features.py \
  tests/test_postseason_research_daily.py \
  tests/test_postseason_research_contracts.py \
  tests/test_postseason_research_prospective.py \
  tests/test_postseason_research_settlement.py \
  tests/test_postseason_research_flow.py -q --tb=short
```
