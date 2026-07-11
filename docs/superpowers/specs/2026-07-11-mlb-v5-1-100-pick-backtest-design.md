# MLB Winner Model v5.1 — 100-Pick Backtest Design

## 1. Objective

Build a deterministic, auditable historical replay of MLB Winner Model v5.1 and produce 100 `Retrospective Daily Top 1` picks from the 2025 regular season.

The backtest measures the model's ranking and probability quality. It does not claim that the selected Daily Top 1 was actionable before every game on that date, because each game is reconstructed independently at its own pregame cutoff.

## 2. Frozen evaluation window

- Initial selection dates: 2025-05-01 through 2025-08-12.
- These are the first 100 MLB regular-season game dates beginning 2025-05-01.
- Every date is included. Dates are never skipped because the slate looks difficult or because the result is unfavorable.
- The target is 100 graded picks. If a locked Top 1 is canceled or never receives an official result, it is marked `VOID`; the test extends one game date beyond 2025-08-12 for each void until 100 graded picks exist.
- Postseason, Spring Training, exhibitions, and All-Star events are excluded.

## 3. Historical replay clock

Each game has an independent synthetic lock time:

```text
lock_at = scheduled_start_utc - 45 minutes
feature_as_of_date = official_game_date - 1 day
```

Rules:

- Season, recent-form, matchup, bullpen, defense, and schedule statistics may use games completed no later than the previous calendar date.
- No pitch, plate appearance, score, result, or in-game event from the target date may enter a feature.
- The official starting pitcher and official starting nine are used as the reconstructed pregame participants.
- MLB's historical feed does not prove the original lineup announcement timestamp. Therefore lineup provenance is labeled `RECONSTRUCTED_FINAL_LINEUP`, not `TIMESTAMP_VERIFIED_LINEUP`.
- Each game's prediction is frozen independently at T-45. After every candidate prediction for a date is frozen, the system ranks those predictions without opening the outcome store and labels the strongest one `Retrospective Daily Top 1`.

This preserves the v5.1 confirmed-lineup intent but is not a simulation of a single executable daily betting decision.

## 4. Data sources and provenance

Primary sources:

- MLB Stats API: schedule, game identifiers, scheduled start, teams, venue, probable/actual starter, official starting lineup, roster, boxscore, and official result.
- Baseball Savant Statcast search/raw CSV: pitch-level and batted-ball data, velocity, pitch usage, xwOBA inputs, hard-hit/barrel inputs, catcher/fielding inputs where available.
- MLB game feed: recorded weather and roof status where available.
- Frozen venue table: coordinates, time zone, roof type, and park identifiers.

Reference URLs:

- https://statsapi.mlb.com/api/v1/schedule
- https://baseballsavant.mlb.com/csv-docs
- https://www.mlb.com/glossary/statcast

Every raw extract receives a manifest containing source URL/query, retrieval time in UTC, minimum and maximum source game date, row count, schema version, and SHA-256 hash.

Historical moneyline odds are evaluation-only. They are merged after predictions are locked and never enter features, probability calibration, eligibility, ranking, or tie-breaking. If a reliable historical odds source is unavailable, model results are completed first and market-comparison fields remain explicitly missing until a source is supplied.

## 5. Pre-2025 fitting boundary

The scoring rule uses fixed v5.1 domain weights. Only normalization constants and the final score-to-probability scale may be estimated.

- Normalization and probability-calibration period: 2022-01-01 through 2024-12-31 regular seasons.
- 2025 data may not change metric definitions, signs, internal weights, shrinkage constants, cutoffs, normalization values, or probability coefficients.
- No feature-selection search is run against the 2025 results.
- Any change after inspecting 2025 outcomes creates a new model version and invalidates the current v5.1 backtest.

## 6. Common normalization

All raw metrics are converted so that higher values favor the team being scored.

For a rate metric with sample size `n`:

```text
reliability = n / (n + k)
shrunk_value = reliability × observed_value + (1 - reliability) × training_league_mean
z = direction × clip((shrunk_value - training_mean) / training_sd, -2.5, 2.5)
metric_score = clip(50 + 20 × z, 0, 100)
```

`direction` is `+1` when higher is better and `-1` when lower is better.

Frozen shrinkage constants:

- Starting-pitcher and hitter split rates: `k = 150` batters faced or plate appearances.
- Recent-form rates: `k = 100` batters faced or plate appearances.
- Bullpen rates: `k = 300` batters faced.
- Defense and catcher rates: `k = 500` opportunities or called pitches.

Missing metrics receive the neutral score of 50. Their weights are not redistributed. Missingness lowers data quality and may raise uncertainty.

## 7. Frozen v5.1 component rules

Each team receives six component scores from 0 to 100. Internal weights below sum to 100% within each component.

### 7.1 Starting pitcher — 35% of final score

- 35% season xwOBA allowed, lower is better.
- 20% season K%-BB%, higher is better.
- 15% season FIP, lower is better.
- 15% xwOBA allowed across the previous five starts, lower is better.
- 10% expected innings, based only on previous starts, rest, and known pregame restrictions.
- 5% fastball/overall velocity change versus the pitcher's prior-season or season baseline.

The official starter must be identifiable. Otherwise the game is ineligible.

### 7.2 Confirmed lineup and offense — 25%

The official starting nine are combined with frozen batting-order opportunity weights derived from 2022-2024 plate appearances by lineup slot.

- 50% shrunk lineup xwOBA versus the opposing starter's handedness.
- 20% lineup xwOBA over the previous 14 days.
- 15% lineup BB%-K%.
- 10% lineup barrel rate.
- 5% lineup ISO.

All hitter statistics stop at D-1. Target-game plate appearances are forbidden.

### 7.3 Bullpen — 20%

- 35% season bullpen xwOBA allowed, lower is better.
- 20% season bullpen K%-BB%, higher is better.
- 15% bullpen xwOBA allowed over the previous 14 days, lower is better.
- 30% leverage-pool availability.

The leverage pool is the five relievers with the most batters faced during prior 30-day appearances in the seventh inning or later with a score margin of three runs or fewer. Availability for each reliever is frozen as:

- `0.0`: at least 25 pitches on the previous day, at least 40 over the previous two days, or three consecutive usage days.
- `0.5`: 15-24 pitches on the previous day or 30-39 over the previous two days.
- `1.0`: otherwise.

Team availability is the prior-30-day leverage batters-faced weighted mean.

### 7.4 Park, weather, and home context — 10%

- 60% handedness-aware park run/home-run factor interacted with the two lineups' fly-ball and barrel profiles.
- 40% recorded weather/roof interaction with the same profiles.

If defensible historical weather inputs are unavailable, the weather portion is neutral 50 and data quality is reduced. Generic home-field advantage is handled by the fitted probability intercept and is not added again here.

### 7.5 Defense and catcher — 5%

- 55% active starting lineup OAA per opportunity.
- 30% starting-catcher framing runs per 1,000 called pitches.
- 15% catcher blocking/throwing value per opportunity; pop-time z-score is the fallback.

Unavailable historical defensive inputs are neutral rather than reconstructed from the target game's result.

### 7.6 Schedule, travel, and rest — 5%

Start at 50 and apply the following pregame-known adjustments, then clip to 0-100:

- `+12` for a rest day on the previous calendar date.
- `+5` for at least three consecutive days in the same home city.
- `-12` after a game of at least 10 innings on the previous date.
- `-10` for at least 1,000 miles of travel without a rest day.
- `-6` for crossing at least two time zones without a rest day.
- `-8` for a scheduled doubleheader.

The target date's first-game usage or result cannot affect a later game on the same date.

## 8. Final score and probability

For home (`H`) and away (`A`) component scores:

```text
D = 0.35×(SP_H-SP_A)
  + 0.25×(OFF_H-OFF_A)
  + 0.20×(BP_H-BP_A)
  + 0.10×(ENV_H-ENV_A)
  + 0.05×(DEF_H-DEF_A)
  + 0.05×(SCH_H-SCH_A)

p_home = sigmoid(alpha + beta × D/10)
```

`alpha` and `beta` are the only fitted probability parameters. They are estimated once from 2022-2024 chronological as-of features and then frozen. Probabilities are clipped to `[0.10, 0.90]`.

The predicted winner is the side with probability above 50%. The displayed winner probability is `max(p_home, 1-p_home)`.

## 9. Confidence, uncertainty, tier, and agreement

Data-quality values lie in `[0,1]` and preserve the v5.1 Confidence structure:

```text
Confidence = 2.5×overall_completeness
           + 2.0×starter_quality
           + 1.5×lineup_quality
           + 1.5×bullpen_quality
           + 1.5×component_agreement
           + 1.0×weather_schedule_quality
```

`component_agreement` is the fraction of starter, offense, and bullpen edges pointing to the predicted winner.

Uncertainty:

- `High`: starter unavailable, fewer than nine reconstructed starters, overall completeness below 75%, or another critical integrity failure.
- `Medium`: completeness from 75% through 89.99%, a rookie/small-sample starter, or an unresolved weather/role limitation.
- `Low`: completeness at least 90% with no critical flag.

Tier remains the original v5.1 rule:

- `S`: probability at least 68% and Confidence at least 9.0.
- `A`: probability at least 63% and Confidence at least 8.5.
- `B`: probability at least 58% and Confidence at least 8.0.
- `Lean`: probability 54-57.99%.
- `Toss-up`: probability below 54% or Uncertainty `High`.

`Full Agreement = Yes` only when starter, offense, and bullpen all favor the predicted winner and Uncertainty is not `High`.

## 10. Eligibility and Daily Top 1 selection

A candidate game is eligible when:

- It is a 2025 MLB regular-season game with an official `gamePk`.
- Both teams, scheduled start, venue, official starter, and nine starting hitters per team are available.
- Its reconstructed lock-time status is not canceled or postponed. Whether it eventually receives an official result is checked only after locking; a selected game without an official result becomes `VOID` under Section 2.
- The feature build passes all as-of and schema checks.

All eligible games on a date are ranked using unrounded values:

1. Predicted-winner probability, descending.
2. Confidence, descending.
3. Absolute starting-pitcher component edge, descending.
4. Scheduled start UTC, ascending.
5. `gamePk`, ascending.

Ranks 1-3 receive the Top 3 flag. Rank 1 is the sole `Retrospective Daily Top 1`.

The full candidate ranking is retained, not just the selected pick.

## 11. Outcome isolation and anti-leakage controls

The pipeline has separate stages and stores:

1. `raw/`: immutable source snapshots and manifests.
2. `features/`: D-1 features and quality flags; cannot read outcomes.
3. `locked/`: candidate predictions, daily rankings, hashes, and model version.
4. `outcomes/`: official results and optional market data; joined only after lock hashes exist.
5. `reports/`: metrics and workbook output.

Mandatory assertions:

- Every feature row has `source_game_date < target_game_date`.
- No target `gamePk` appears in the feature input's event rows.
- No 2025 result is available to the normalization/calibration process.
- Re-running from the same raw hashes produces byte-equivalent locked prediction rows.
- Market columns are absent from the feature schema and ranking function.

## 12. Evaluation outputs

The final report includes:

- 100 locked Daily Top 1 picks and outcomes.
- Full daily candidate rankings.
- Accuracy, wins, losses, Wilson 95% interval.
- Brier score and log loss.
- Calibration by the frozen probability bins.
- Top 3, 58%+, and Full Agreement subgroup results.
- Home-team and higher-season-win-percentage baselines.
- Same-game market-favorite comparison when trustworthy historical odds are available.
- Maximum losing streak and performance by month, team, tier, and probability bin.
- Data-quality coverage and counts of neutralized/missing components.

Backtest rows use model version `v5.1-BT` and protocol phase `Backtest`. They are not mixed with future live v5.1 operating picks.

## 13. Verification and acceptance criteria

Before reporting results:

- Unit-test metric direction, shrinkage, clipping, component weights, probability conversion, tier thresholds, and tie-breaking.
- Run leakage tests that intentionally inject same-day rows and confirm rejection.
- Manually audit at least three dates from raw source through locked rank and result join.
- Confirm exactly one Top 1 per non-void selection date and exactly 100 graded Top 1 picks in the final sample.
- Confirm all six final weights equal `35/25/20/10/5/5` and sum to 100%.
- Confirm prediction generation completes before outcome joining.
- Scan exported workbook formulas and render every sheet for visual QA.

## 14. Known limitations

- Historical official lineups are available, but their original announcement timestamps may not be provable; they are labeled reconstructed.
- Current Statcast extracts can contain later corrections. Raw hashes and retrieval timestamps make the run reproducible but do not recreate the exact database state in 2025.
- Historical injury news, pitch-limit statements, roof decisions, and weather forecasts may be incomplete. Missing evidence is neutralized and disclosed.
- Retrospective Daily Top 1 tests ranking quality, not a single executable daily wager chosen before all games.
- Historical market odds may require a paid or user-supplied source and are not allowed to delay or alter the model picks.

## 15. Change control

This document is the pre-registration for the v5.1-BT run. Once implementation begins, any material change to dates, eligibility, metrics, weights, shrinkage, probability calibration, ranking, or missing-data policy must be documented before outcomes are inspected and must trigger a new backtest version identifier.
