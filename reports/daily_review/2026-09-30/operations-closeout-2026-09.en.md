# MLB Forecasting Operations Closeout

This report covers September 1–30, 2026 in New York time. Operational status reflects the final September 30 check and pause. Post-closeout verification is recorded separately through November 9. [Korean edition](operations-closeout-2026-09.md) · [Repository overview](../../../README.md)

## Summary

- Regular-season operations recorded 24 eligible pregame predictions as primary claims. Of the 23 formally settled games, 10 picks were correct (43.5%). One claim remains open because its official result response contained multiple date buckets.
- The monthly review verified 33 report–trace pairs and 89 distinct date–game keys. Those saved forecasts have a broader scope than the formally settled claims. [Monthly verification](existing-september-prediction-verification.json)
- The first postseason run against live data stopped at schedule validation. The last September 30 check found zero saved pregame predictions for that day's four games. The user paused the recurring checks at 16:18 EDT. [Activation attempt](postseason-live-activation-attempt-20260930T1521Z.json) · [Final operating status](operations-status.json)
- After the pause, the full regular-season and postseason schedule–Statcast reconciliation and the offline pregame–settlement tests passed. The live single-game check was not run. [Verification record](post-closeout-verification-2026-11-09.json)

## Operational timeline

| Time in New York | Event | Evidence |
|---|---|---|
| September 1–24 | Checked official schedules and participant inputs and saved model reports and traces. The first eligible forecast saved before each game's start became its primary claim. | [Monthly coverage](september-analysis-coverage-20260930.en.md) · [Pregame and settlement workflow](../../../docs/prospective_ml_daily_flow.md) |
| September 16, 23, and 24 game slates | Compared saved primary claims with archived official final results. The September 16 slate was collected and settled on September 30. | [Sep 16](../2026-09-16/prospective_ml_settlement.en.md) · [Sep 23](../2026-09-23/prospective_ml_settlement.md) · [Sep 24](../2026-09-24/prospective_ml_settlement.en.md) |
| September 29 | The existing regular-season workflow lacked support for the four postseason games. Zero pregame forecasts were saved. | [Coverage gap](../2026-09-29/no_pregame_prediction_review.en.md) |
| September 30, about 11:21–11:22 EDT | The first live run of the postseason research mode returned exit code 2 before writing a report, trace, or pregame receipt. | [Run log](postseason-live-activation-attempt-20260930T1521Z.md) |
| September 30, 15:51 EDT | Final operational check recorded four games, zero saved forecasts, one elapsed cutoff for PHI at ATL (`849841`), and three upcoming cutoffs. | [Final check](operations-check-20260930T195003Z.json) · [Coverage gap](no_pregame_prediction_review.en.md) |
| September 30, 16:18 EDT | The user requested a pause. The 30-minute `mlb` automation was set to `PAUSED`. | Local automation status and update timestamp |
| November 9 | Code, reports, settlement records, and source data were archived and merged into GitHub `master`. | [Archive commit](https://github.com/taejunoh/mlb-walkforward-lab/commit/75e85fdce49af645b62da15d1e398a689efdc8a7) · [Merge commit](https://github.com/taejunoh/mlb-walkforward-lab/commit/ef4930b1996f247da91e5574d4305392b494008a) |

### Data collection, review, and accuracy work

| Work | Recorded scale and outcome |
|---|---|
| Source collection and preservation | The archived history cache contains 438 raw schedule, Statcast, and other responses totaling about 2.20 GB. The file count includes saved acquisition attempts. [Raw cache](../../../data/experimental_daily/history_cache) |
| Recurring operational checks | September 29–30 folders contain 43 saved checks (14 and 29, respectively). Checks covered schedules, game states, participant availability, and new forecasts or settlements. [Daily reviews](..) · [Final check](operations-check-20260930T195003Z.json) |
| Reproducibility | Replayed 121 stored prediction rows across 33 report–trace pairs with zero recorded verification errors. A September 5 readiness review recorded 170 passing contract and regression tests for input checks, calibration, totals, and evidence binding. [Trace verification](existing-september-prediction-verification.json) · [Readiness audit](../../model_readiness/2026-09-05/readiness-work-audit.en.md) |
| Accuracy improvement attempt | On a fixed retrospective sample of 1,340 games, the baseline recorded 718 correct picks (53.58%) and the candidate recorded 717 (53.51%). Brier score moved from 0.246271 to 0.246172 and log loss from 0.685553 to 0.685362. The 2025 outcomes had already been seen, so the comparison could not establish independent improvement. The candidate stayed in research. [Calibration results](../../model_readiness/2026-09-02/temporal_calibration/RESULTS.md) |

The main Codex log records **34 h 58 m 50 s inside turns** and **1,084,491,069 tokens** in September. The total includes 1,054,564,352 cached input tokens, 27,965,982 uncached input tokens, and 1,960,735 output tokens. Elapsed time includes waits and tool execution inside turns. The accounting excludes subagent sessions and unmetered events, and does not support a per-activity or billing breakdown. [Usage accounting and method](operations-effort-2026-09.en.md)

## Prediction and settlement results

**Monthly coverage.** The saved schedule inventory contains 370 date entries and 369 distinct game IDs. Postponements and rescheduled occurrences account for the difference. The 33 report–trace pairs contain 121 stored rows and 89 distinct date–game keys. Formal performance below is restricted to settled primary claims. [Schedule inventory](schedule-identity-inventory-sep01-30-20260930T1528Z.json) · [Game-level coverage](september-analysis-coverage-20260930.json)

**Win–loss forecasts.** The table uses sealed home-win probabilities and archived official outcomes. Pick accuracy divides correct picks by formal settlements. Brier score and log loss average each game's loss over the same settled population.

| Game date | Primary claims | Formal settlements | Correct | Accuracy | Brier score | Log loss | Evidence |
|---|---:|---:|---:|---:|---:|---:|---|
| September 16 | 4 | 4 | 1 | 25.0% | 0.2456 | 0.6825 | [Settlement JSON](../2026-09-16/prospective_ml_settlement.json) |
| September 23 | 16 | 15 | 8 | 53.3% | 0.2645 | 0.7225 | [Settlement JSON](../2026-09-23/prospective_ml_settlement.json) |
| September 24 | 4 | 4 | 1 | 25.0% | 0.3130 | 0.8289 | [Settlement JSON](../2026-09-24/prospective_ml_settlement.json) |
| **Total** | **24** | **23** | **10** | **43.5%** | **0.2696** | **0.7340** | |

The separate September 23 diagnostic that includes open claim `824785` found 9 correct picks in 16 games (56.25%). The formal total excludes that claim. The settled sample spans three game dates and supports only a descriptive performance readout. [September 23 review](../2026-09-23/prospective_ml_settlement.md)

**Combined-runs diagnostic.** The earliest eligible pregame totals were compared with official combined final runs. This measures forecast error in runs. The archive has no formal O/U or realized-return metric for these comparisons. The September 23 record contains zero pregame betting picks. [Sep 16 diagnostic](../2026-09-16/prospective_ml_settlement.json) · [Sep 23 diagnostic](../2026-09-23/prospective_ml_settlement.json) · [Sep 24 diagnostic](../2026-09-24/prospective_ml_settlement.json)

| Game date | Games | MAE | RMSE | Inside saved 80% interval |
|---|---:|---:|---:|---:|
| September 16 | 15 | 1.609 | 2.170 | 15/15 |
| September 23 | 16 | 2.885 | 3.430 | Unrecorded |
| September 24 | 7 | 3.276 | 3.809 | 6/7 |

## Reason for closure

The user requested an operational pause. Before that request, the postseason workflow was blocked by schedule validation. The same game ID appeared on both its postponed and rescheduled dates, and the research-mode duplicate check ran before schedule normalization. The run ended before forecast generation. [Live attempt](postseason-live-activation-attempt-20260930T1521Z.md) · [Final status](operations-status.json)

## Open items

| Item | Status at closeout or later verification |
|---|---|
| TOR at BAL formal settlement (`824785`) | The official response contained a September 22 postponed occurrence and a September 23 final occurrence. The settlement parser required one date and one game. The archived final score was used for diagnostics only. [Review](../2026-09-23/prospective_ml_settlement.md) |
| One-game live postseason validation | Input reconciliation and offline tests passed after the pause. No live pregame claim was created, and no future postseason game was listed in the official schedule checked November 9. No follow-up run is scheduled. [Original attempt](postseason-live-activation-attempt-20260930T1521Z.md) · [Verification record](post-closeout-verification-2026-11-09.json) |
| Reconstruction of missing forecasts | The evaluator required four evidence bindings, one per role. The verified collection example carried 91 (55 Statcast, 34 venue, one schedule, and one ERA). Preparation code was saved. The unresolved input contract prevented a pilot or any new reconstructed forecast. [Blocker](reconstruction-preparation/input-contract-blocker.md) |
| Final September 30 coverage | The last check shows one elapsed cutoff and three still ahead. Later game states fall outside the recorded observation window. [Final check](operations-check-20260930T195003Z.json) |

**Resumption criteria.** First, validate the postseason schedule by reconciling game IDs repeated across postponed and rescheduled dates into one verified game per ID. Confirm that game IDs, dates, and competition types agree between the official schedule and Statcast. Then use a new postseason game whose outcome is still unknown. Verify the official starters, lineups, and model inputs, and save the first eligible forecast as the primary claim before the scheduled start, together with its source trace and pregame receipt. After the game, match the official final result by game ID, date, and participating teams, then save and verify retrieval of the formal settlement record and receipt linked to that same claim. Resume operations after documenting one game that passes this full sequence. If any step fails, retain the evidence and keep resumption on hold.

### Post-closeout verification — November 9, 2026

1. **Archived regular-season regression passed.** The shared parser reconciles postponed and rescheduled entries before establishing coverage and rejects conflicting teams or game types for the same ID. From March 25 through September 23, 2,402 archived entries with 29 repeated IDs resolved to 2,373 games. All matched 55 Statcast files containing 700,401 pitches. The focused regression run passed 106 tests. [Archived source report](../../experimental_daily/2026-09-24/20260924T203303766913Z-095c7301fcd8a3f4.json) · [Regression tests](../../../tests/test_postseason_research_history.py)
2. **Full-season official input check passed.** The research loader and a separate raw-data comparison matched all 2,469 played games (2,429 regular season, 40 postseason) and 728,343 pitches through November 8. The schedule contained one cancelled game outside the played set. There were zero missing or extra games, date/type mismatches, or duplicate pitch keys. The March shard was reused; April onward was collected in 45 windows of up to five days. Only the collection windows changed in the isolated run; existing validation gates were retained. One schedule receipt and 46 Statcast receipts were verified.
3. **Offline workflow passed; live one-game validation was not run.** An isolated run on GitHub `master` with network connections blocked passed 33 tests and 9 subtests covering the pregame deadline, first-claim preservation, final-result identity, and settlement resume. These are fixture results. On November 9, MLB's official schedule listed no future postseason game in the queried range. No new live forecast or settlement was created. Operations remain paused. [Verification evidence and sources](post-closeout-verification-2026-11-09.json)

## Preserved materials

| Material | Location |
|---|---|
| Monthly schedule, first saved forecasts, and coverage | [Monthly report](september-analysis-coverage-20260930.en.md) · [Coverage JSON](september-analysis-coverage-20260930.json) |
| Operations logs and settlement reviews | [Daily review directory](..) · [Final status JSON](operations-status.json) |
| Pregame and postgame workflow receipts and immutable settlement heads | [Workflow receipts](../../../audit/prospective_ml_daily_flow) · [Settlement heads](../../../audit/prospective_ml_observation/settlement_heads) |
| Model runs, traces, and source inputs | [ML reports](../../experimental_daily) · [Totals reports](../../experimental_daily_totals) · [Raw input cache](../../../data/experimental_daily/history_cache) |
| Post-closeout checks | [Verification record and evidence fingerprints](post-closeout-verification-2026-11-09.json) · [November verification evidence index](november-verification/README.md) · [Evidence archive](november-verification) |
| License scope | [MIT License](../../../LICENSE) for original project code and documentation; [third-party notices](../../../THIRD_PARTY_NOTICES.md) exclude MLB/Statcast and other external materials |
| GitHub archive | [Master merge snapshot](https://github.com/taejunoh/mlb-walkforward-lab/tree/ef4930b1996f247da91e5574d4305392b494008a) |
