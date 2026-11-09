# MLB v5.1 100-Pick Diagnostic

## Executive Summary

67 wins and 33 losses is encouraging, but the model went only 4–3 in the 7 games that differed from the season-winning-percentage baseline. No additional predictive advantage has been established. Reconstructed final participants do not establish reproducibility at actual T-45.

## Metric, population, and denominator definitions

The population is the sealed Daily Top 1 set of 100 games from 2025-05-01 through 2025-08-12. Accuracy=W/N; Brier=mean((p-y)^2); Log loss=-mean(y log p+(1-y)log(1-p)); ECE is the N-weighted absolute gap over 5 fixed bins. All calculations use unrounded values from the sealed JSON.

## 1. Primary result

67 wins, 33 losses, N=100, accuracy 67.00%, Wilson 95% 57.3052%–75.4370%. Brier 0.2213716579, Log loss 0.6351164919, ECE 0.1150816008, maximum losing streak 5.

## Paired baselines

### Table contract: `paired_baselines`

- Population: `Daily Top 1 paired games`
- Formula: `paired correctness 2x2; exact McNemar; paired delta`
- Definitions: N=sum of four paired cells; model/baseline wins come from exact paired cells; delta=model accuracy minus baseline accuracy; interval uses z=1.97.
- Exclusions: `season-record tie excluded`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl; data/v5_1_bt/outcomes/baseline.json`

| baseline | N | model | baseline | both correct | model only | baseline only | both wrong | delta | paired 95% interval | McNemar p |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| home | 100 | 67/100 | 59/100 | 52 | 15 | 7 | 26 | +8.00pp | -1.10pp–+17.10pp | 0.1338 |
| D-1 season record | 99 | 66/99 | 65/99 | 62 | 4 | 3 | 30 | +1.0101pp | -4.25pp–+6.27pp | 1.0000 |

The full paired cells are: home both correct 52, model only 15, baseline only 7, both wrong 26; D-1 both correct 62, model only 4, baseline only 3, both wrong 30. Intervals use z=1.97. One tie in season winning percentage was excluded from the denominator. Market N=0, so market advantage or betting performance cannot be evaluated.

## Probability calibration and discrimination

### Table contract: `calibration`

- Population: `Daily Top 1 N=100`
- Formula: `fixed-bin probability mean, accuracy, gap, Brier contribution`
- Definitions: N=bin count; gap=absolute mean-probability minus actual-accuracy difference; Brier contribution is weighted over all Top 1 picks.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| bin | N | mean probability | actual accuracy | gap | Brier contribution |
| --- | --- | --- | --- | --- | --- |
| 50-54 | 0 | — | — | — | 0.0000000000 |
| 54-58 | 1 | 0.565420 | 0.000000 | 0.565420 | 0.0031969951 |
| 58-63 | 17 | 0.615177 | 0.470588 | 0.144589 | 0.0461498282 |
| 63-68 | 59 | 0.653417 | 0.762712 | 0.109295 | 0.1151687714 |
| 68-100 | 23 | 0.697232 | 0.608696 | 0.088536 | 0.0568560633 |

Probability–outcome Spearman ρ=0.0077, p=0.939; confidence–outcome Spearman ρ=0.0365, p=0.719. ≥63%: 59/82=71.9512%; ≥68%: 14/23=60.8696%. These are post-hoc results and monotonic calibration is not validated; these are not new cutoffs, and bins and thresholds were not optimized on these 100 games.

## Daily rank

### Table contract: `rank`

- Population: `graded daily ranks 1-3`
- Formula: `wins and N by immutable daily_rank`
- Definitions: W=correct predictions and N=graded predictions at immutable daily_rank.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| rank | W | N | accuracy |
| --- | --- | --- | --- |
| 1 | 67 | 100 | 67.0000% |
| 2 | 60 | 100 | 60.0000% |
| 3 | 59 | 100 | 59.0000% |

Exact McNemar p-values for rank 1 versus 2/3 are 0.349/0.291. The ordering is directionally favorable, but superiority is not established.

## Time and team concentration

### Table contract: `monthly`

- Population: `Daily Top 1 N=100`
- Formula: `wins and N by official_date month`
- Definitions: Month is the actual YYYY-MM official_date key; W/L/N and accuracy use graded Top 1 picks.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| month | W | L | N | accuracy |
| --- | --- | --- | --- | --- |
| 2025-05 | 21 | 10 | 31 | 67.7419% |
| 2025-06 | 22 | 8 | 30 | 73.3333% |
| 2025-07 | 14 | 13 | 27 | 51.8519% |
| 2025-08 | 10 | 2 | 12 | 83.3333% |

The July (month 7)-versus-rest Fisher p=0.0589 is evidence of instability, not discovery of a regime. Home: 52/78=66.6667%; away: 15/22=68.1818%. There are 23 teams; the top 6 are New York Yankees 14, Philadelphia Phillies 11, Houston Astros 9, San Diego Padres 9, Los Angeles Dodgers 7, Seattle Mariners 7; together they account for 57%. Do not interpret small cells as persistent team effects.

### Table contract: `teams`

- Population: `Daily Top 1 N=100`
- Formula: `wins, N, accuracy, Wilson interval by predicted winner`
- Definitions: Team is predicted winner; W/L/N, accuracy and Wilson 95% interval are descriptive small-cell results.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| team | N | W | accuracy | Wilson 95% |
| --- | --- | --- | --- | --- |
| Arizona Diamondbacks | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| Athletics | 1 | 0 | 0.0000% | 0.0000%–79.3457% |
| Atlanta Braves | 3 | 2 | 66.6667% | 20.7655%–93.8510% |
| Boston Red Sox | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| Chicago Cubs | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| Chicago White Sox | 1 | 1 | 100.0000% | 20.6543%–100.0000% |
| Detroit Tigers | 5 | 4 | 80.0000% | 37.5528%–96.3777% |
| Houston Astros | 9 | 4 | 44.4444% | 18.8775%–73.3353% |
| Kansas City Royals | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| Los Angeles Angels | 1 | 0 | 0.0000% | 0.0000%–79.3457% |
| Los Angeles Dodgers | 7 | 4 | 57.1429% | 25.0454%–84.1783% |
| Milwaukee Brewers | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| Minnesota Twins | 6 | 4 | 66.6667% | 29.9988%–90.3231% |
| New York Mets | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| New York Yankees | 14 | 10 | 71.4286% | 45.3505%–88.2788% |
| Philadelphia Phillies | 11 | 10 | 90.9091% | 62.2635%–98.3768% |
| Pittsburgh Pirates | 2 | 1 | 50.0000% | 9.4529%–90.5471% |
| San Diego Padres | 9 | 3 | 33.3333% | 12.0582%–64.5803% |
| San Francisco Giants | 5 | 3 | 60.0000% | 23.0720%–88.2382% |
| Seattle Mariners | 7 | 6 | 85.7143% | 48.6865%–97.4321% |
| Tampa Bay Rays | 1 | 0 | 0.0000% | 0.0000%–79.3457% |
| Texas Rangers | 2 | 2 | 100.0000% | 34.2372%–100.0000% |
| Toronto Blue Jays | 4 | 4 | 100.0000% | 51.0100%–100.0000% |

## Components

### Table contract: `components`

- Population: `Daily Top 1 N=100`
- Formula: `chosen-side signed edge mean by outcome`
- Definitions: Edges are signed for the chosen side; win/loss means split on the observed Top 1 result.
- Exclusions: `none`
- Source paths: `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`

| chosen-side edge | win mean | loss mean |
| --- | --- | --- |
| starter | 14.879 | 13.751 |
| offense | 8.287 | 7.049 |
| bullpen | 11.752 | 14.238 |
| schedule | 1.313 | 2.424 |

Bottom/top halves of absolute bullpen edge were 74%/60%; signed bullpen edge–outcome ρ=-0.151, p=0.135. This is a prior-season hypothesis only; weights, shrinkage, sign, and thresholds are unchanged.

## Data quality and leakage

All 1,340 locked candidates and all Top 1 picks are INCOMPLETE/High/Toss-up, with defense and environment at 50, quality 0. Top 1 completeness ranges from 0.2358031605 to 0.5769861616, mean 0.4602764334. Lineups are RECONSTRUCTED_FINAL_LINEUP and starters are RECONSTRUCTED_ACTUAL_STARTER, not actual T-45 snapshots. α/β from the same 7,265-game pool are not rolling-origin OOF. Feature isolation is PASS: max_history_date <= D-1 and target gamePk Statcast excluded. Locked-row isolation is PASS: locked prediction rows contain no outcome fields and bind exactly to later evaluated prediction fields. However, there is no outcome-join execution timestamp. Statistical as-of aggregation status is `LIMITATION`, and lock-to-result join ordering is `NOT_TIMESTAMP_PROVEN`. The proven PASS subchecks do not establish outcome-join chronology and do not cure participant-time or source-revision limitations. Participants come from the post-hoc final record, and retrieval hashes provide reproducibility but are not historical database snapshots.

## Source hashes

- `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`: `db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76`
- `data/v5_1_bt/reports/summary.json`: `eea2faa794adb0372a54a6c4c10a7fe443c6f4b123056edb554396dbf3231b49`
- `data/v5_1_bt/outcomes/baseline.json`: `97e97094a7db5144c4e5d86aed2b9d96d178f6277fd2e035526780c9b90aa0e8`
- `data/v5_1_bt/locked/predictions.jsonl`: `bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb`
- `data/v5_1_bt/locked/lock_manifest.json`: `f38e881d7dc3ffff523c0117fa12abebbb71bc7b35624cb14ef4412397dcd26b`
- `audit/v5_1_bt/lock_manifest.json`: `59aa1cbfca3d8eaa9c6f48e88615b9018a3aa828c39ac77e883096be566f4274`
- `audit/v5_1_bt/model_card.json`: `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`
- `audit/v5_1_bt/manual_audit.md`: `814c02d7bb0da9e9cda591d9f9c7681396d7272e8df8fd37a1bbb3f86dd8fc66`
- `config/v5_1_bt.json`: `1bd3008a7bb95b622263c1ce73bb07182c1ffeea39ab1efd1ae38c271cc729d9`
- `data/v5_1_bt/features/candidate_raw_features.jsonl`: `a1817d4a7e50c77f35fbd03f3f79aa8e629e4b686de88b66c8c8d3877924d4a4`

## Recommendation

v5.2-QD should retain the prediction structure and freeze the meaning of data completeness and OOF calibration. Keep the 500-game historical analysis separate from the actual T-45 prospective ledger.

## Limitations and assumptions

No claims are made for independent prospective validation, market advantage, ROI, CLV, profitability, actionable betting, or improved v5.2 predictive performance.
