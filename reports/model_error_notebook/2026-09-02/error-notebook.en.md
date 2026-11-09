# 2026-09-02 MLB Error Notebook

Results checked: 2026-09-03 06:00 ET. All 15 games were Final. Of 13 fixed predictions, 4 hit and 9 missed; 2 games with no prediction were excluded from evaluation. These are directional hits for the favored team, not returns on actual bets or a recommended portfolio.

## First, clarify the record

The probabilities were **not uncalibrated; calibration was applied, but independent validation is insufficient**. The existing diagnostic showed `calibration_status=IN_SAMPLE_NOT_OOF`, with 7,265 records in each of the eligible training and calibration samples. Calibration on the same sample does not guarantee accurate win probabilities for future games.

The actual causes of the 9 losses have not been established. The metric directions below are **pregame weighted raw-score differences, home minus away**, not percentage-point contributions to win probability. Observing a lineup is not the same as having complete player features.

## Results by record cohort

| Cohort | Predictions | Hits–misses | Hit rate | Mean estimated win probability | Brier | Log loss |
|---|---:|---:|---:|---:|---:|---:|
| All 13 games (illustrative aggregate) | 13 | 4–9 | 30.8% | 56.4% | 0.2713 | 0.7372 |
| Existing pregame records, 6 games | 6 | 1–5 | 16.7% | 57.4% | 0.3243 | 0.8470 |
| Preregistered, 7 games | 7 | 3–4 | 42.9% | 55.6% | 0.2259 | 0.6430 |

The aggregate of all 13 games is for illustration. The 6 games reconstructed from historical pregame traces and the 7 preregistered games should not be treated as one strict live-validation sample. Lower Brier and log loss are better. The simple reference values for assigning 50% to every probability in this sample are 0.25 and 0.6931, respectively. The preregistered 7-game values are below these references, but the sample is too small to establish a model advantage.

## Game-by-game misses

### San Diego Padres @ Cincinnati Reds (824470)

- Confirmed facts: SD 61.3%, lost 3–7. Stored starter, bullpen, and batting raw-score signals favored SD. One of 9 SD batters had a missing feature.
- Lesson: even when several model indicators point the same way, input gaps and uncertainty must be shown separately. The cause of the loss is not established.
- Next validation: compare whether pregame data existed for the missing player and the expected starters/lineup at the time with the actual lineup. Record any before/after difference only as a separate experiment.

### Atlanta Braves @ Washington Nationals (822686)

- Confirmed facts: WSH 53.5%, lost 0–9. Starter and batting signals favored WSH, while the bullpen signal pointed to ATL. Player features were present for 18 batters.
- Lesson: do not express a small advantage from conflicting signals as strong confidence. Do not infer starter failure from a 9-run margin alone.
- Next validation: inspect starter/bullpen runs and innings, then evaluate separately whether the prediction indicators explained the relevant segments.

### Athletics @ Texas Rangers (822851)

- Confirmed facts: TEX 57.3%, lost 2–9. Bullpen and batting favored TEX; the starter signal favored ATH. TEX starter quality was approximately 0.442; the sample count for the velocity-change baseline was 0 and the change value was null.
- Lesson: check starter input quality separately even for a moderate edge. Do not interpret a baseline of 0 as actual velocity.
- Next validation: inspect handling of missing starter sample counts/baseline velocity, and compare the low-quality-starter cohort separately.

### Baltimore Orioles @ Colorado Rockies (824312)

- Confirmed facts: BAL 68.9%, lost 5–6. Starter, bullpen, and batting signals all favored BAL. Player features were present for 18 batters; no probable-starter mismatch was flagged.
- Lesson: one high-probability loss does not by itself imply a probability error. Conversely, agreement among indicators does not guarantee an independently validated 68.9% win probability.
- Next validation: accumulate future observations in the same probability range, and compare missing defense/environment input effects through time-split validation. Do not lower the probability based only on this game.

### Philadelphia Phillies @ Arizona Diamondbacks (825037)

- Confirmed facts: PHI 50.6%, lost 0–1. The PHI bullpen edge and ARI starter/batting edges offset each other. At the time, the game was SCORED_NOT_TOP3.
- Lesson: this was an almost-even, observation-only prediction; do not retroactively present it as a betting recommendation. A one-run loss does not establish a batting-model defect.
- Next validation: record pregame prices and uncertainty together, then validate performance over the full cohort of slight favorites.

### New York Mets @ Tampa Bay Rays (822931)

- Confirmed facts: TB 52.3%, lost 4–10. Preregistered input completeness was approximately 0.2773; NYM starter quality was 0. Starter/schedule and bullpen/batting signals conflicted.
- Lesson: the confirmed input-quality warning is the most specific item to investigate. PRIMARY is a record-eligibility class, not betting eligibility or high confidence.
- Next validation: trace the completeness components and the reason for quality 0. Define observation-only/recommendation-blocking rules for low-quality inputs in advance and test them on a new sample.

### San Francisco Giants @ Pittsburgh Pirates (823339)

- Confirmed facts: PIT 55.1%, lost 4–5. Bullpen/batting signals favored PIT and the starter signal favored SF. Player features were present for 18 batters.
- Lesson: a small raw-score edge and a one-run result do not justify blaming bullpen weighting.
- Next validation: compare pregame bullpen availability/expected usage with actual deployment, and include wins under the same conditions.

### Detroit Tigers @ Minnesota Twins (823660)

- Confirmed facts: MIN 51.1%, lost 6–11. Batting/schedule favored MIN, while starter/bullpen favored DET.
- Lesson: an edge near 50% indicates direction only. Do not change the probability sign after seeing a blowout.
- Next validation: expose conflicting inputs in explanations and validate probability, market-price, and uncertainty conditions as separate preregistered rules.

### Miami Marlins @ Kansas City Royals (824068)

- Confirmed facts: KC 50.4%, lost 6–9. Batting favored KC; starter/bullpen favored MIA.
- Lesson: this output was effectively near-even. Do not present it as if there were a strong reason to select a side.
- Next validation: log the entire cohort of slight edges without omissions, and separate model-side advantage from price-relative advantage.

## Record hits and unscored games too

SEA, TOR, MIL, and HOU were hits. Do not discard the high-probability model based only on BAL's 68.9% loss, or claim it was validated by selecting only MIL's 67.9% hit. NYY@LAA (6–3) and STL@LAD (8–6) had no scored prediction and are excluded from the performance denominator; do not create predictions after the fact.

## Confirmed structural limitations and next work

For all 9 loss traces, defense/environment signals are stored with quality 0 and neutral values. This is evidence of missing inputs, not evidence that those inputs caused the losses. None of the 9 has admitted market-judgment evidence; the presence of some raw prices does not establish betting eligibility or an actual bet.

### 1. TB game's input completeness is 0.2773, opposing starter quality is 0; SD is missing one batter feature.

- Validation work: trace missing/defaulted values using data available at the time, and specify observation-only/recommendation-blocking rules based on data quality first.
- Completion criteria: record causes and effects by input. Freeze the rules before new games; do not optimize thresholds to fit these 13 outcomes.

### 2. IN_SAMPLE_NOT_OOF: calibration was applied, but not validated on an independent sample.

- Validation work: split training/calibration/evaluation chronologically, and accumulate Brier, log loss, and probability-bin frequencies for preregistered records.
- Completion criteria: report reconstructed historical pregame records separately from prospective samples. Do not reuse the new evaluation period for parameter decisions.

### 3. All 9 misses have defense/environment quality 0 and neutral values. Actual weather/defense effects are not represented.

- Validation work: first check whether defense/environment inputs can be acquired before games, then run a time-split experiment against the existing model.
- Completion criteria: compare missingness, acquisition time, and performance together. Do not attribute losses to weather or change weights based on a single day.

### 4. Some raw prices exist, but the 9 losses have no admitted market-judgment evidence.

- Validation work: connect verified two-sided prices aligned to prediction times with recommendation decisions; prepare a no-vig market comparison and subsequent closing-line comparison.
- Completion criteria: do not infer an actual bet from price alone. Without execution records, ROI is N/A; do not invent an unverified closing price.

These are follow-up validation items; this request made no model, retraining, weight, or threshold changes. Do not tune to one day's results; freeze proposed rules before inspecting new games and evaluate them separately.

## Evidence and validation scope

- [Fixed prediction/final result ledger](../../experimental_all_day_ml_ledger/2026-09-02/11869f74a98db326afa5d653f71c40b66f78bfb0341cdfbde75f6da336d5be2d.json)
- [Calibration independence diagnostic](../../v5_1_bt/diagnostic_100.json)
- [Historical sample limitations](../../../audit/v5_1_bt/manual_audit.en.md)
- [Model card](../../../audit/v5_1_bt/model_card.json)
- Ledger SHA-256: `11869f74a98db326afa5d653f71c40b66f78bfb0341cdfbde75f6da336d5be2d`.
- This note uses stored traces/primary snapshots and official Final evidence. No new boxscore or inning-level pitching records were collected, so comparison against actual game causes is incomplete.
- Calculations use `selected_forecast.pick_probability` and `result.forecast_outcome` from the ledger: Brier = mean((p−y)²); log loss = mean(−y ln p − (1−y) ln(1−p)); y=1 for a hit and 0 for a miss.
- Original prediction/price/result evidence and existing user source changes were preserved. The HTML build passed, but visual inspection was not performed due to the local-file access policy.
