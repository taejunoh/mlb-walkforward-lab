# MLB Walk-Forward Lab

A research project for timestamped MLB pregame forecasts and auditable settlement. The September 2026 operating period ended on September 30 at 16:18 EDT. Recurring checks remain paused. This repository preserves the code, reports, and evidence from that period.

The official record is **24 pregame claims, 23 settlements, and 10 correct picks (43.5%)**. The sample covers three regular-season slates. It does not establish predictive improvement or a betting edge.

- [Operations closeout](reports/daily_review/2026-09-30/operations-closeout-2026-09.en.md)
- [Post-closeout verification record](reports/daily_review/2026-09-30/post-closeout-verification-2026-11-09.json)
- [Recorded time and token accounting](reports/daily_review/2026-09-30/operations-effort-2026-09.en.md) · [Report index](reports/english-index.md)

## What it did

```text
Official MLB schedule + Statcast + pregame participants
    → validate inputs → forecast report + source trace
    → seal the first eligible forecast before game start
    → match the official Final → settlement + receipt
```

The Python workflow stores forecasts and their evidence separately from settlement. A rerun can add diagnostics but cannot replace the first eligible forecast. Postseason research has its own records and does not contribute to the regular-season performance sample. Totals comparisons were diagnostic; they did not produce formal O/U returns.

## What the record shows

| Evidence | Result | What it supports |
|---|---|---|
| Settled regular-season primary claims | 10/23 correct; Brier 0.2696; log loss 0.7340 | A descriptive result for three slates; one of the 24 claims remains unresolved |
| Saved September forecasts | 33 report–trace pairs; 89 distinct date–game keys | Forecast coverage, a broader population than formal settlements |
| Post-closeout official schedule and Statcast | 2,469 played games and 728,343 pitches; no missing or extra games, date/type mismatches, or duplicate pitch keys | Input coverage and identity across the 2026 regular season and postseason |
| Isolated postseason pregame and settlement tests | 33 tests and 9 subtests passed | Fixture-based verification of claim timing, result identity, and preservation of completed settlements |

The data check and offline tests did not create a live postseason claim. The postseason model remains research-only and is not validated for postseason performance.

## Why operations stopped

The first postseason research run stopped before writing a forecast: an official game ID appeared in both a postponed entry and its rescheduled entry, and the duplicate check ran before normalization. The user paused recurring checks that day. The shared parser fix and the later data checks resolved that input issue in isolation; they did not restart operations. No new actual postseason game was available for a pregame-to-Final check at the November 9 closeout. That check remains unperformed and is not required to close this archive.

## What to keep, and what to change

- **Keep the original forecast and its receipts.** A score without proof of when the forecast existed is not a prospective record. Missing pregame records should stay missing in the performance ledger.
- **Report the denominator next to the result.** Saved rows, eligible claims, and official settlements answer different questions. A large input dataset cannot turn a small forward sample into performance evidence.
- **Check source contracts earlier.** Use real rescheduling cases to test game identity and check the Statcast row limit before accepting a date window as complete. Keep the rejected inputs and the reason for rejection.

## Repository map

- [`mlb_backtest/`](mlb_backtest) — forecast, evidence, and settlement code.
- [`reports/daily_review/`](reports/daily_review) — operational checks, closeout, and settlements.
- [`audit/prospective_ml_daily_flow/`](audit/prospective_ml_daily_flow) — archived workflow receipts.
- [`data/experimental_daily/history_cache/`](data/experimental_daily/history_cache) — archived operating-period source responses.
- [`tests/`](tests) and [research runbook](docs/postseason_ml_research.md) — reproducibility and operating boundaries. The runbook's live examples are documentation, not a startup step.
