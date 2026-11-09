# 2026-09-30 Pregame Prediction Coverage Gap

**Status:** `PARTIAL_NO_PREGAME_PREDICTION` — coverage-only record noting that the scheduled cutoff for PHI@ATL (849841) has passed without a saved pregame prediction. This is not a formal settlement.

In the parent-provided score-free official schedule observation (2026-09-30 18:20:50 UTC), game 849841 was listed as `In Progress`. The exact first-pitch time was not verified. Game results, scores, and winners were neither requested nor collected.

| Game (gamePk) | Start (UTC) | Observed status | Pregame prediction saved | Coverage |
| --- | --- | --- | --- | --- |
| PHI @ ATL (849841) | 2026-09-30 18:00 | In Progress | No | New cutoff missed |
| CWS @ HOU (849846) | 2026-09-30 21:00 | Pre-Game | No | Cutoff not reached |
| BOS @ NYY (849848) | 2026-10-01 00:00 | Scheduled | No | Cutoff not reached |
| CHC @ SD (849842) | 2026-10-01 02:00 | Scheduled | No | Cutoff not reached |

## Interpretation

So far, the scheduled cutoff has passed for 1 of 4 games, and there is no saved pregame prediction for PHI@ATL. This is not a model `PASS`, a prediction failure, 0% accuracy, or a formal game result. No validation samples were added; accuracy, Brier score, log loss, P&L, and totals diagnostics are all not applicable (`null`). The remaining 3 games are scheduled or pregame.

The existing postseason schedule duplicate-ID validation failure and the 4-versus-91 historical source-binding mismatch in post-start inputs remain unchanged; they were not retried. No predictions, post-start evaluation, or settlements were run; no source changes or result collection were made. The previous settlement/receipt inventory remains unchanged: Sep16=4, Sep23=16, Sep24=4.

## Evidence

- Schedule/status source: [MLB StatsAPI schedule](https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate=2026-09-29&endDate=2026-10-01&fields=totalGames,dates,date,games,gamePk,gameType,gameDate,status,abstractGameState,detailedState,codedGameState,teams,away,home,team,id,name) — parent-verified read-only, score-free observation at `2026-09-30T18:20:50Z`.
- Current operations check: `reports/daily_review/2026-09-30/operations-check-20260930T182003Z.json`.
- Previous operations check: `reports/daily_review/2026-09-30/operations-check-20260930T175003Z.json`.

The operations record file was written at `2026-09-30T18:27:22Z`.
