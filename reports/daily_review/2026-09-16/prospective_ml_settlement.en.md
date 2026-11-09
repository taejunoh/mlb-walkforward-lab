# 2026-09-16 Settlement of Saved Pregame ML Picks

The 4 saved primary claims were compared with official final results. The predicted winners went 1–3 (25% accuracy); using the sealed home-win probabilities, the Brier score was 0.245561 and log loss was 0.682516. The sample consists only of the 4 primary claims for this date and does not establish model quality or improvement.

| Game (gamePk) | Pregame selected winner (selected probability) | Final score (away–home) | Result | Input completeness |
| --- | --- | --- | --- | ---: |
| White Sox @ Guardians (824382) | Guardians (home-win probability 0.6510) | 3–6 | Hit | 0.584 |
| Giants @ Cardinals (823004) | Cardinals (home-win probability 0.5289) | 6–5 | Miss | 0.432 |
| Yankees @ Twins (823655) | Yankees (away-win probability 0.5504) | 4–5 | Miss | 0.542 |
| Tigers @ Blue Jays (822763) | Tigers (away-win probability 0.5270) | 1–5 | Miss | 0.576 |

Predictions were settled exactly as saved in each game's 2026-09-16 pregame snapshot. The ML report trace ran at 2026-09-16 16:43:15.059750 UTC; the four candidate snapshots were observed at 16:48:00.010502 UTC. The corresponding primary claims were saved at 16:48:00.152446, 16:48:00.173135, 16:48:00.191346, and 16:48:00.216204 UTC. Timestamps and each game's prediction/input evidence are recorded in the accompanying JSON along with paths to each claim, snapshot, participant receipt/payload, and settlement head. Final results were observed in the official Stats API response at 2026-09-30 11:43:38–11:43:44 UTC.

## Input and settlement completeness

Two of the three pregame receipts (`4460f407…` and `a1751cdc…`) contain no primary-claim references. Therefore they have no settlements, and no hit rate is calculated for them. The remaining receipt contained 4 primary claims, and the postgame receipt recorded FINAL settlements for all four with `workflow_complete=true` and `errors=[]`. There was no prior settlement head before this review.

Of the 15 daily receipt/result rows, the 11 that are not the 4 primary claims (gamePk: 822926, 823332, 824467, 822680, 823576, 824626, 822846, 824140, 824306, 823979, 825031) are recorded as `NOT_PRIMARY`. They are neither prediction failures nor settled primary-sample observations.

Input completeness for the four predictions ranged from 0.432 to 0.584. These values cannot identify the cause of the outcomes or the causal effect of missing inputs, so no game-specific post-hoc causal explanations were added. No predictions were regenerated and no retraining was performed. Betting P&L was not calculated because there was no formally admitted market evidence or betting execution record.

## Separate diagnostic of saved totals predictions

The 15 `PROJECTED` rows in the first saved totals report were matched to the official 2026-09-16 final results by gamePk and away/home team IDs. The result capture was saved at 2026-09-30 11:48:28 UTC; all 15 games were FINAL and non-tied. For all 15 rows, the saved prediction's game-start timestamp was after the report timestamp (2026-09-16 12:50:18.654611 UTC).

In this limited single-day comparison, mean absolute error for total runs was 1.609 runs, RMSE was 2.170 runs, and the saved 80% intervals covered 15/15 games. These are technical diagnostics only, not official O/U settlements, betting performance, or evidence of model improvement.

| Game (gamePk) | Projected total | Actual total | Absolute error | Saved 80% interval | Covered |
| --- | ---: | ---: | ---: | ---: | :---: |
| White Sox @ Guardians (824382) | 8.902 | 9 | 0.098 | 3–15 | Yes |
| Giants @ Cardinals (823004) | 9.944 | 11 | 1.056 | 4–16 | Yes |
| Yankees @ Twins (823655) | 9.042 | 9 | 0.042 | 3–15 | Yes |
| Tigers @ Blue Jays (822763) | 8.408 | 6 | 2.408 | 3–14 | Yes |
| Athletics @ Rays (822926) | 9.031 | 7 | 2.031 | 3–15 | Yes |
| Brewers @ Pirates (823332) | 8.943 | 9 | 0.057 | 3–15 | Yes |
| Dodgers @ Reds (824467) | 9.523 | 8 | 1.523 | 4–15 | Yes |
| Phillies @ Nationals (822680) | 9.070 | 3 | 6.070 | 3–15 | Yes |
| Orioles @ Mets (823576) | 8.671 | 8 | 0.671 | 3–14 | Yes |
| Braves @ Cubs (824626) | 9.373 | 12 | 2.627 | 4–15 | Yes |
| Red Sox @ Rangers (822846) | 8.630 | 10 | 1.370 | 3–14 | Yes |
| Royals @ Astros (824140) | 8.546 | 7 | 1.546 | 3–14 | Yes |
| Padres @ Rockies (824306) | 10.050 | 12 | 1.950 | 4–16 | Yes |
| Mariners @ Angels (823979) | 8.487 | 9 | 0.513 | 3–14 | Yes |
| Marlins @ Diamondbacks (825031) | 9.171 | 7 | 2.171 | 4–15 | Yes |

The totals-prediction trace/report, original report hash, and official-results capture path and hash are preserved in JSON under `saved_totals_diagnostic`. The designated capture file contains the full list of official final results used in the comparison; the ML win/loss settlement references the existing result receipt and settlement head for each of the four claims.

## Evidence and limitations

The baseline ML report is `reports/prospective_ml_observation/c9d13305d1446a474f1f5a0eafb46ea03a7dd0e14164dd9425c8dbf957e4ba05.json`; the shadow-comparison report is `reports/prospective_ml_shadow/eecf6b183b46769a0def327d8e02de1877d698ea8afbb487a9bab7f7019f7b26.json`. This settlement used only the 4 baseline primary claims. Brier score and log loss were independently recalculated from the four sealed probabilities and outcome labels and agree with the rounded values in the baseline report. Original result collection was not rerun.
