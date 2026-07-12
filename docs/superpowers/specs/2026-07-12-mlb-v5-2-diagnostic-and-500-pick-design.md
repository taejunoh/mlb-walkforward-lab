# MLB Winner Model v5.2-QD — Diagnostic, Historical 500-Pick, and Prospective Validation Design

## 1. Decision and purpose

On 2026-07-12 the user approved the corrected two-track validation design:

1. Run an immediate, multi-season `Historical Walk-forward Robustness 500` over 2022-2024.
2. Begin a separate prospective ledger on 2026-07-13 using genuinely timestamped T-45 inputs.
3. Never combine those two samples or describe the historical 500 as independent prospective confirmation.

This supersedes the earlier proposal to wait until 2028 or 2029 for a single 500-pick sample. The historical run answers whether the frozen structure behaves robustly under chronological replay. The prospective ledger answers whether it can be reproduced from information actually observable before games.

The sequence is fixed: publish the v5.1 100-pick diagnostic, freeze `v5.2-QD`, execute the historical 500, and operate the prospective ledger separately. `QD` means quality-and-data discipline. It is not a claim of improved predictive power.

## 2. Authoritative v5.1 evidence

The diagnostic must be reproducible from these repository files:

- `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`: all 1,340 locked candidates joined to official outcomes.
- `data/v5_1_bt/reports/summary.json`: primary, subgroup, calibration, monthly, team, baseline, and market summaries.
- `data/v5_1_bt/outcomes/baseline.json`: D-1 higher-season-win-percentage winner by `gamePk`.
- `data/v5_1_bt/locked/predictions.jsonl` and `data/v5_1_bt/locked/lock_manifest.json`: sealed pre-outcome predictions and hashes.
- `audit/v5_1_bt/lock_manifest.json`: audited hashes, completion counts, and outcome/result hashes.
- `audit/v5_1_bt/model_card.json`: training boundary, normalization, calibration, weights, and immutable input hashes.
- `data/v5_1_bt/features/candidate_raw_features.jsonl`: D-1 raw features and participant provenance.
- `audit/v5_1_bt/manual_audit.md`: traced audits for 2025-05-01, 2025-06-15, and 2025-08-12.
- `config/v5_1_bt.json`: frozen v5.1-BT protocol configuration.
- `/Users/taejunoh/Documents/Codex/2026-07-11/files-mentioned-by-the-user-mlb/outputs/mlb_v5_1_100_pick_backtest.xlsx`: delivered human-readable workbook; it is a presentation artifact, not the calculation authority.

The sealed v5.1 prediction content hash is `bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb`; the evaluated-prediction content hash recorded by the audited lock manifest is `db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76`.

## 3. Durable 100-pick diagnostic contract

The diagnostic is a versioned Markdown report plus machine-readable JSON. Every table carries its population, formula, exclusions, and source path. Calculations use unrounded values and the sealed 100 Daily Top 1 rows unless explicitly labeled as all-candidate or Top 3 analysis.

### 3.1 Primary result and exact paired baselines

The report must preserve these verified results:

- Daily Top 1: 67 wins, 33 losses, `N=100`, accuracy `67.00%`.
- Wilson 95% interval: `57.3052%-75.4370%`.
- Brier score `0.2213716579`, log loss `0.6351164919`, ECE `0.1150816008`, maximum losing streak `5`.
- Home baseline: `59/100=59.00%`. Exact pairing is both correct 52, model only 15, home only 7, both wrong 26; model delta `+8.00pp`, exact McNemar `p=0.1338`, approximate paired 95% interval `-1.10pp to +17.10pp`.
- D-1 higher-season-win-percentage baseline: `65/99=65.6566%`; one season-record tie is excluded from the paired denominator. Exact pairing is both correct 62, model only 4, baseline only 3, both wrong 30. On the same 99 games the model is `66/99=66.6667%`, delta `+1.0101pp`, exact McNemar `p=1.0`, approximate paired 95% interval `-4.25pp to +6.27pp`.
- Market comparison has `N=0`. The report must not state market edge, ROI, CLV, or betting profitability.

The primary interpretation is frozen: 67% is an encouraging signal, but the seven disagreements with the season-record baseline produced only a 4-to-3 model advantage. Incremental predictive value is not established.

### 3.2 Calibration and discrimination

Report bin count, mean predicted probability, actual accuracy, gap, and Brier contribution for the fixed bins `50-54`, `54-58`, `58-63`, `63-68`, and `68-100`. The verified Top 1 bins are:

| Bin | N | Mean probability | Actual accuracy |
|---|---:|---:|---:|
| 54-58 | 1 | 56.5420% | 0.0000% |
| 58-63 | 17 | 61.5177% | 47.0588% |
| 63-68 | 59 | 65.3417% | 76.2712% |
| 68-100 | 23 | 69.7232% | 60.8696% |

The `50-54` bin has `N=0`. Probability versus outcome has Spearman rho `0.0077`, `p=0.939`; confidence versus outcome has rho `0.0365`, `p=0.719`. The `>=63%` result of `59/82=71.9512%` and `>=68%` result of `14/23=60.8696%` are post-hoc diagnostics, not new cutoffs. The report must say that monotonic calibration is not validated and must not optimize bins or thresholds on these 100 outcomes.

### 3.3 Rank diagnostic

Daily rank accuracy is rank 1 `67/100=67%`, rank 2 `60/100=60%`, and rank 3 `59/100=59%`. Paired rank-1 versus rank-2 has `p=0.349`; rank-1 versus rank-3 has `p=0.291`. The rank ordering is directionally favorable but not established as superior.

### 3.4 Temporal and team concentration

Month results are May `21/31=67.7419%`, June `22/30=73.3333%`, July `14/27=51.8519%`, and August `10/12=83.3333%`. July versus all other dates has Fisher exact `p=0.0589`; this is instability evidence, not a regime-discovery claim.

Home-side picks were `52/78=66.6667%`; away-side picks were `15/22=68.1818%`. Twenty-three distinct predicted teams appeared. The most selected teams were Yankees 14, Phillies 11, Astros 9, Padres 9, Dodgers 7, and Mariners 7; those six account for 57% of Top 1 picks. Team-level accuracies must be shown with `N` and Wilson intervals, never ranked as durable team effects from these small cells.

### 3.5 Component diagnostic

For the chosen side, win/loss mean component edges are starter `14.879/13.751`, offense `8.287/7.049`, bullpen `11.752/14.238`, and schedule `1.313/2.424`. The bullpen absolute-edge lower/upper halves returned `74%/60%`; bullpen edge versus outcome has rho `-0.151`, `p=0.135`. These are hypotheses for prior-season testing only. No component weight, shrinkage constant, sign, or threshold may be changed from this sample.

### 3.6 Data quality, provenance, and leakage

All 1,340 locked candidates, including all Top 1 rows, are `INCOMPLETE`, `High` uncertainty, and `Toss-up`. Defense and environment are neutral 50 with quality 0. Top 1 completeness ranges `0.2358031605-0.5769861616`, mean `0.4602764334`.

Lineups are `RECONSTRUCTED_FINAL_LINEUP` and starters are `RECONSTRUCTED_ACTUAL_STARTER`. They identify eventual participants but do not prove what was observable at T-45. The training/calibration pool contains 7,265 eligible 2022-2024 games, and the same pool was used to fit alpha and beta rather than rolling-origin out-of-fold predictions.

The leakage audit must separately report:

- statistical as-of safety: `max_history_date <= D-1`, target `gamePk` Statcast excluded, outcomes inaccessible before lock;
- participant-time limitation: final participants were reconstructed after the event and are not a timestamped T-45 snapshot;
- source-revision limitation: later provider corrections are reproducible by retrieval hash but are not a historical database snapshot.

Passing the first category does not cure the latter two. The diagnostic conclusion must not claim live reproducibility.

## 4. Frozen v5.2-QD change specification

### 4.1 What remains unchanged

`v5.2-QD` retains the v5.1 predictive structure:

- final component weights `starter/offense/bullpen/environment/defense/schedule = 35/25/20/10/5/5`;
- every internal metric weight and sign documented in the v5.1 design;
- shrinkage constants `150` for starter/hitter splits, `100` for recent form, `300` for bullpen, and `500` for defense/catcher;
- clipping, neutral score 50, no redistribution of missing weights, score construction, probability clipping `[0.10,0.90]`, and predicted-side rule;
- unrounded daily ranking order: winner probability, data-completeness score, absolute starter edge, immutable `lock_basis_start_utc`, then `gamePk`;
- lineup-slot opportunity weighting method, with coefficients re-estimated only from the allowed prior-season window.

The 100-pick outcomes cannot justify a bullpen reduction, season-record feature, new cutoff, home adjustment, or any other predictive change. Such candidates belong to separately versioned research and cannot enter `v5.2-QD`.

### 4.2 Rolling-origin out-of-fold probability calibration

The v5.1 in-sample alpha/beta fit is replaced. For a target season `Y`, normalization and slot weights use only completed regular seasons before `Y`. Calibration is fit only on earlier-season out-of-fold edges: each calibration season is normalized from seasons preceding it, scored without its outcomes, and its outcomes are opened only after its predictions are sealed. No 2025 outcome may enter any v5.2-QD normalization, calibration, threshold, or weight.

The historical folds are fixed:

| Evaluation fold | Target dates | Target normalization/slot-weight pool | OOF calibration seasons and their normalization pools |
|---|---|---|---|
| H22 | 2022 regular season | 2018-2021 | 2019 from 2018; 2020 from 2018-2019; 2021 from 2018-2020 |
| H23 | 2023 regular season | 2018-2022 | H22 OOF set plus 2022 from 2018-2021 |
| H24 | 2024 regular season | 2018-2023 | H23 OOF set plus 2023 from 2018-2022 |

Each fold gets its own immutable model card, normalization table, slot weights, alpha/beta, source hashes, code commit, and latest permitted training date. Fold parameters may not be shared backward or refit after target outcomes are inspected.

For prospective season 2026, the production card uses completed 2018-2024 data only; 2025 is excluded because it is the development diagnostic set. Its OOF calibration ledger is exactly: 2019 normalized from 2018, 2020 from 2018-2019, 2021 from 2018-2020, 2022 from 2018-2021, 2023 from 2018-2022, and 2024 from 2018-2023. Alpha and beta are fit on those sealed 2019-2024 OOF predictions and outcomes only. Target-game 2026 features may use 2026 events completed no later than D-1; this does not permit 2026 outcomes to refit the card. The 2026 card must be sealed before the first counted pick on or after 2026-07-13. A missed date before or after sealing is `SYSTEM_MISS` and is never reconstructed or backfilled. A later season card may be created only before that season's first counted pick, using the same declared rolling-origin process, and remains frozen for the season.

### 4.3 Confidence becomes data completeness

The new score contains component evidence coverage only:

```text
weighted_overall_completeness
  = 0.35*starter_quality
  + 0.25*offense_quality
  + 0.20*bullpen_quality
  + 0.10*environment_quality
  + 0.05*defense_quality
  + 0.05*schedule_quality

data_completeness_score = 10 * weighted_overall_completeness
```

Every quality term is clipped to `[0,1]`, so the displayed score is `[0,10]`. `component_agreement` and `Full Agreement` are reported separately and never increase completeness. The old mixed arithmetic is retained only as an audit field named `legacy_selection_quality_score`:

```text
2.5*weighted_overall_completeness
+ 2.0*starter_quality
+ 1.5*offense_quality
+ 1.5*bullpen_quality
+ 1.5*component_agreement
+ (2.0*environment_quality + schedule_quality)/3.0
```

It cannot drive tier, eligibility, official ranking, or a predictive-confidence claim. A compatibility reader may ingest the old `confidence` field only into this legacy audit field.

Uncertainty is also explicit: `High` for a critical-integrity failure or completeness below 7.5; `Medium` for completeness from 7.5 through below 9.0, either starter below 150 prior career batters faced, or unresolved pregame context; and `Low` for completeness at least 9.0 with no such condition. Tier evaluation is a deterministic first-match cascade using unrounded winner probability and completeness:

1. `UNRATED` when any critical-integrity requirement fails for the row's claimed provenance stratum.
2. `Toss-up` when uncertainty is `High`, winner probability is below `0.54`, or `data_completeness_score < 8.0`.
3. `S` when winner probability is at least `0.68` and completeness is at least `9.0`.
4. `A` when winner probability is at least `0.63` and completeness is at least `8.5`.
5. `B` when winner probability is at least `0.58` and completeness is at least `8.0`.
6. `Lean` when winner probability is at least `0.54` and completeness is at least `8.0`.
7. `Toss-up` as the exhaustive fallback.

The probability and 8.0/8.5/9.0 cutoffs preserve the v5.1 published cutoffs; they are not inferred from the 100 outcomes. `Full Agreement` remains descriptive and cannot override `UNRATED` or this cascade.

### 4.4 Deterministic eligibility and pre-outcome status

All decisions below occur before outcomes are accessible. A critical reason code is append-only; later data cannot erase it from the audit row.

| Requirement or event | Historical reconstructed run | Prospective run | Pre-outcome disposition and reason code |
|---|---|---|---|
| Regular-season `gamePk`, two MLB teams, lock-basis scheduled start UTC, and venue present | required | required | exclude candidate: `MISSING_GAME_ID`, `INVALID_COMPETITION`, `MISSING_TEAMS`, `MISSING_START_TIME`, or `MISSING_VENUE` |
| Fold/season model card and all minimum-source manifests pass hash, schema, and as-of checks | required | required | exclude candidate; any hash/fold/as-of violation also makes the affected run `RUN_INVALID` |
| Two starters and nine distinct lineup hitters per team | final official participants required | required in a timely snapshot | historical missing: exclude with `MISSING_RECONSTRUCTED_PARTICIPANTS`; prospective partial: shadow `UNRATED` with `PARTIAL_T45_PARTICIPANTS` |
| Participant provenance matches the claimed stratum | `RECONSTRUCTED_FINAL_PARTICIPANTS` only | `TIMESTAMP_VERIFIED_T45` for official pool | mismatch: exclude from that official pool with `PROVENANCE_MISMATCH`; a timely partial row remains shadow only |
| Starter, offense, bullpen, and schedule minimum inputs can be computed without target-date events | required | required | exclude with `MISSING_REQUIRED_FEATURE_INPUT` or `AS_OF_VIOLATION`; as-of violation makes the run `RUN_INVALID` |
| Environment or defense strictly as-of observation missing | optional | optional | retain candidate, set component score 50 and quality 0, add `ENV_NEUTRAL_NO_ASOF_SOURCE` or `DEF_NEUTRAL_NO_ASOF_SOURCE` |
| Pre-lock canceled/postponed status | not eligible | not eligible | exclude with `PRELOCK_NOT_SCHEDULED`; if all candidates are excluded, date status is `NO_ELIGIBLE_DATE` or prospective `NO_VERIFIED_ELIGIBLE_DATE` |
| Acquisition failed while an official slate existed | not silently skipped | not silently skipped | date status `SYSTEM_MISS`; it is neither a pick nor a VOID and cannot be backfilled |
| Only shadow/partial rows exist | not applicable | no official candidate | preserve shadow rows with rank only inside `shadow_rank`; date status `SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE` |
| Calibrated `p_home` equals exactly 0.5 | no directional official candidate | no directional official candidate | set `predicted_side=none`, predicted winner empty, and `pick_probability=0.5`; retain `UNRATED` shadow row with `NO_DIRECTION_AT_50` |

Historical reconstructed participants are eligible only when the final official record supplies exactly one actual starter and nine distinct starting hitters for each team, all participant IDs resolve to the scheduled clubs, and no target-game performance enters a feature. This is deterministic participant identity, not historical T-45 availability.

For directional rows, `p_home > 0.5` predicts home and `p_home < 0.5` predicts away; exact equality follows the no-direction row in the table and cannot enter an official ranking. The displayed winner probability for directional rows is `max(p_home, 1-p_home)`. Equal winner probabilities among official directional candidates continue through the frozen ranking keys: completeness descending, absolute starter edge descending, immutable lock-basis start UTC ascending, then `gamePk` ascending. Shadow equality uses the same keys inside `shadow_rank`. A duplicate `gamePk` is `RUN_INVALID` rather than a further tie-break.

Date-state precedence is also fixed: `RUN_INVALID` halts and overrides all ordinary statuses; otherwise a required acquisition/clock failure is `SYSTEM_MISS`; otherwise a prospective slate containing shadow rows but no verified eligible row is `SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE`; otherwise a prospective slate with no verified eligible row is `NO_VERIFIED_ELIGIBLE_DATE`; otherwise a historical slate with no reconstructed eligible row is `NO_ELIGIBLE_DATE`; otherwise lock one official Top 1. This prevents the same failure from being relabeled as benign no-eligibility.

### 4.5 Environment, defense, and participant provenance

Environment and defense contribute non-neutral values only when a manifest-bound source proves the observation was available by the game's as-of cutoff and supplies the required opportunity denominator. Later game weather, target-game fielding, or retroactively inferred conditions are forbidden. Otherwise the component remains 50, quality 0, and a reason code is emitted.

Every prediction has exactly one participant provenance stratum:

- `TIMESTAMP_VERIFIED_T45`: the raw snapshot satisfies the Section 6.1 freshness rule at or before `lock_basis_start_utc - 45 minutes`, and both starters and all 18 lineup slots were visible in that snapshot.
- `TIMESTAMP_PARTIAL_T45`: a timely snapshot exists but at least one required participant was unresolved; the prediction may be retained as shadow output but is `UNRATED` and cannot count in the verified prospective sample.
- `RECONSTRUCTED_FINAL_PARTICIPANTS`: participants came from a later official record; allowed in historical robustness only and never described as T-45 reproducible.

Historical and prospective metrics are always split by stratum. No relabeling based on later confirmation is permitted. Prospective official Top 1 ranking is computed exclusively over eligible `TIMESTAMP_VERIFIED_T45` rows. Partial/shadow rows have a separate `shadow_rank` and cannot change an official candidate's rank, tie-break, Top 1 status, or sample membership. If none is verified and eligible, the official date status is `NO_VERIFIED_ELIGIBLE_DATE` (or the more specific shadow-only status from Section 4.4).

### 4.6 Immutable freeze and change control

Before outcomes open, seal canonical JSON for configuration, model card, normalization, slot weights, calibration inputs/outputs, raw manifests, candidate features, candidate predictions, daily ranks, provenance, code commit, and environment lockfile. Record SHA-256 for file bytes and canonical row content. Replaying the same source hashes must reproduce byte-equivalent locked rows.

Any change to a predictive/internal weight, sign, shrinkage, probability transformation, ranking, eligibility rule, source definition, missing-value behavior, or tier threshold creates a new model version and a new validation sample. Presentation-only changes are recorded but do not reset the model.

## 5. Historical Walk-forward Robustness 500

### 5.1 Population and deterministic selection

Create one chronological queue of every official MLB regular-season game date from 2022 opening day through the end of the 2024 regular season. Exclude Spring Training, exhibitions, All-Star events, and postseason. Process dates strictly ascending and games with the fold-specific frozen card.

For every date with at least one eligible reconstructed candidate, lock the full ranking and select exactly one `Historical Daily Top 1`. Never skip a date because probability is low, the slate is difficult, a favored team repeats, or the result is unfavorable. A date with no eligible candidate is recorded as `NO_ELIGIBLE_DATE`, with reasons, and does not count as a pick. A source outage is `SYSTEM_MISS`, not no-eligibility. Continue through subsequent dates, including later 2024 dates, until exactly 500 graded Top 1 dates are sealed. If fewer than 500 graded dates exist by the 2024 regular-season end, the run fails acceptance rather than entering 2025.

The final boundary date is therefore determined by the 500th chronologically graded pick, not by results. Outputs must show attempted dates, no-eligible dates, system misses, pending rows, void dates, replacement dates, and the final 500 dates.

### 5.2 Terminal outcome and replacement contract

The official MLB detailed status is mapped exactly:

| Observed terminal state | Treatment |
|---|---|
| `Final` with unequal official scores and an official winner | `GRADED` |
| `Completed Early` with unequal official scores and an official winner | `GRADED`; preserve the completed-early reason and innings |
| Canceled/postponed before lock | never a locked candidate; `PRELOCK_NOT_SCHEDULED` |
| Canceled/postponed after lock and never completed by the deadline | `VOID` with `POSTLOCK_CANCELED` or `POSTLOCK_POSTPONED` |
| Suspended or delayed without a terminal result | `PENDING` until the deadline |
| Official tie, abandoned/no-contest, missing winner, or equal terminal scores | `VOID` with `TIE`, `ABANDONED`, `NO_CONTEST`, `MISSING_WINNER`, or `EQUAL_TERMINAL_SCORE` |

For each season the resolution deadline is 23:59:59 America/New_York on the seventh calendar day after MLB's originally published final regular-season date. Seven days permits ordinary resumptions and official corrections while providing a bounded, outcome-independent close before later historical knowledge can keep the ledger open indefinitely. A `PENDING` row that lacks a gradable terminal result at that instant becomes irrevocably `VOID`; a result posted later is retained as an audit annotation but cannot restore sample membership.

Doubleheader games remain distinct by `gamePk`. Within a date they are ordered by immutable lock-basis start UTC and then `gamePk`; first-game outcomes or usage cannot enter the second game's pregame features. Every VOID replacement comes from the next chronological regular-season date after the original selected date that yields a newly graded Top 1. Replacement processing never reorders earlier dates, substitutes a second-ranked game from the same locked slate, or enters 2025. Multiple VOID rows consume multiple later graded dates in chronological order.

### 5.3 Outcome isolation and metrics

Target-season outcomes and baselines are unavailable to feature generation, calibration, eligibility, ranking, and tie-breaking. They are joined only after each date's ranked predictions and hashes are sealed.

Primary metrics are wins, losses, accuracy, Wilson 95% interval, Brier, log loss, ECE with the fixed v5.1 bins, maximum losing streak, and full calibration table. Report rank 1/2/3, 58%+, Full Agreement, `UNRATED`, season/fold, month, home/away, selected team, probability bin, completeness band, component-edge band, and provenance stratum.

Baselines are paired on the same selected `gamePk`:

- home team;
- team with higher D-1 season win percentage, omitting exact ties only from that paired comparison;
- 50:50 probability baseline for Brier and log loss;
- market favorite only if a trustworthy as-of odds snapshot with timestamp and source manifest exists. Otherwise market `N=0` and all market/ROI/CLV fields are null.

For categorical baselines report the 2x2 paired table, accuracy delta, paired 95% interval, and exact McNemar p-value. Accuracy without pairing is insufficient. Denominators are fixed as follows:

- model primary, home baseline, Brier, log loss, Wilson interval, and maximum losing streak use all 500 graded Top 1 games (`N=500`);
- the paired home table also uses those same 500 games and recalculates model accuracy on exactly those 500;
- the D-1 season-record table uses only Top 1 games among the 500 where both teams have a defined, unequal D-1 season win percentage; call this `N_season`, and recalculate both model and baseline accuracy on exactly that identical subset;
- market comparison uses only games among the 500 with a manifest-bound as-of market snapshot and both predictions defined; call this `N_market`, and never compare its market accuracy with the model's 500-game accuracy;
- rank and subgroup denominators are printed from their own explicit eligible populations and never substituted into a primary gate.

### 5.4 Interpretation and predeclared gates

This run is explicitly `Historical Walk-forward Robustness`, not independent confirmation: v5.2-QD was specified after inspecting the 2025 diagnostic, even though no 2025 outcome is used in its fit.

Gate outcomes are fixed before the run:

- `RUN_INVALID`: any leakage, chronology, hash, fold-boundary, sample-count, deterministic-replay, or source-integrity gate fails.
- `ROBUSTNESS_NOT_SUPPORTED`: on their explicit identical paired denominators, a valid run fails to exceed either the home baseline on `N=500` or the D-1 season-record baseline on `N_season`, or the model's `N=500` probability forecasts fail to beat either 50:50 Brier `0.25` or log loss `0.6931471806`.
- `ROBUSTNESS_SUPPORTED_NOT_CONFIRMED`: on their explicit identical paired denominators, a valid run exceeds home on `N=500` and season record on `N_season`, and the model's 500 forecasts beat both probability-null metrics. A positive paired 95% lower bound and exact McNemar `p<0.05` against the D-1 season baseline on `N_season` is reported as stronger evidence, but the label still cannot become prospective confirmation.

ECE, subgroups, components, seasons, months, and teams are diagnostic only. No historical result can trigger a v5.2-QD refit while preserving the same version.

## 6. Separate prospective validation

The prospective ledger starts 2026-07-13. Its sample ID, storage tree, manifests, locks, and reports are distinct from the historical 500. Historical rows may never be appended, pooled, or used to backfill a missed prospective date. The terminal-state, seven-day deadline, PENDING/VOID, doubleheader, and chronological replacement rules in Section 5.2 apply unchanged by season.

Each game is fetched and sealed at T-45. Candidate predictions are created without outcomes. The official daily observational Top 1 is ranked exclusively from eligible, sealed `TIMESTAMP_VERIFIED_T45` predictions after the final verified candidate lock, while the outcome store remains inaccessible. Partial/shadow predictions never enter that ranking. Because some early games may already have started by the ranking time, this validates pregame prediction capture but is not automatically an executable single-bet strategy; profitability or actionability requires a separately frozen decision-time protocol and odds snapshots.

Only `TIMESTAMP_VERIFIED_T45` rows count in the verified prospective sample. Partial snapshots remain shadow records. A date without an eligible verified row is `NO_VERIFIED_ELIGIBLE_DATE`, with `SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE` when partial rows exist. VOID replacement advances to the next chronological regular-season date; missed collection is `SYSTEM_MISS`, never silently reconstructed or counted.

Publish descriptive milestone reports at 25, 50, 100, 250, and 500 graded verified dates. Every report states `N/current target`, date range, uncertainty, and that 500 is incomplete until it is actually reached. No milestone may be merged with historical results or called final confirmation.

At 500, `CONFIRMED_INCREMENTAL_EDGE` requires all integrity gates; model accuracy above home on the identical 500-game population; model accuracy above D-1 season record plus a positive paired 95% lower bound and exact McNemar `p<0.05` on the identical prospective `N_season` subset; and model Brier/log loss below the 50:50 baselines on all 500. Otherwise the result is `NOT_CONFIRMED`. These are evidence labels, not promises of future profitability.

### 6.1 T-45 snapshot freshness and clock contract

Every poll records `observed_scheduled_start_utc` from the authoritative MLB schedule feed. The final qualifying pre-lock poll freezes its observed value as `lock_basis_start_utc`; `lock_at_utc = lock_basis_start_utc - 45 minutes`. Both fields are immutable once the prediction is sealed. This is the only clock basis used for snapshot freshness, provenance, ranking, and replay.

The collector uses a host clock synchronized to UTC by NTP; before every polling window it records offset and uncertainty. Absolute offset greater than 2 seconds or missing clock evidence makes the game `SYSTEM_MISS_CLOCK`, so no timestamp-verified row can be emitted. Polling cadence is fixed: every 5 minutes from T-90 through just before T-50, then every 60 seconds from T-50 through T-45 inclusive, with every boundary calculated from the then-current observed scheduled start.

A qualifying lock snapshot is the latest successful complete snapshot whose client receipt time is in the closed interval `[lock_basis_start_utc - 50 minutes, lock_basis_start_utc - 45 minutes]`. Thus maximum age at lock is 5 minutes. The snapshot must itself contain the same scheduled start that becomes `lock_basis_start_utc`. Server `Date`/provider timestamps are recorded when present, but the NTP-audited client receipt time governs. A response received after the resulting `lock_at_utc` is never used, even within the 2-second clock tolerance.

Before sealing, every poll re-reads status and scheduled start. Any pre-lock start change invalidates the prior countdown and all previously qualifying snapshots, recomputes T-90/T-50/T-45 from the new observed start, and requires a new complete snapshot in the new T-50..T-45 interval. Superseded snapshots remain in the audit log. If the recomputed T-45 has already passed when the change is first observed, the row is `TIMESTAMP_PARTIAL_T45` with `LATE_RESCHEDULE_UNVERIFIED`, shadow only. A cancellation or postponement observed before sealing is `PRELOCK_NOT_SCHEDULED` and no lock is created.

After sealing, later schedule polls write the newest published start only to `latest_effective_start_utc`. A difference from `lock_basis_start_utc` adds `POSTLOCK_START_CHANGE`. It never changes `lock_basis_start_utc`, `lock_at_utc`, snapshot qualification, prediction, provenance, official/shadow rank, or Top 1 selection, and the same `gamePk` is never re-locked. A post-lock cancellation, postponement, or suspension preserves the sealed prediction and follows the PENDING/VOID/graded terminal contract in Section 5.2.

Every prospective report displays both start fields when they differ and states that T-45 verification is relative to the schedule observable when the lock was sealed, not a later revised start time.

## 7. Sources and acquisition implications

Minimum historical sources are manifest-bound MLB schedule/game feed for competition, `gamePk`, dates, starts, teams, venue, final participants, status, and outcomes; Baseball Savant Statcast events for starter/offense/bullpen inputs; MLB Stats pitching records for D-1 league ERA and required pitcher denominators; a frozen venue/team/time-zone table; and prior schedule history for rest, travel, time zones, extra innings, and doubleheaders. All are required for H22/H23/H24 eligibility and fold fitting.

Minimum prospective sources are the same D-1 feature/history inputs through the current target's D-1, the sealed 2026 model card and its 2018-2024 fit manifests, the authoritative MLB schedule feed for effective start/status, and a timestamped authoritative pregame participant feed proving both starters and all 18 lineup slots under Section 6.1. Absence of any of these prevents an official verified candidate; it cannot be repaired from the postgame feed.

Environment/weather/roof, defense/OAA, catcher, injury/restriction, market, and odds feeds are optional. They activate a component or comparison only with a strict as-of timestamp, denominator, schema, and manifest. Their absence invokes the frozen neutral/null rule and cannot fail the minimum-source preflight by itself.

Before any H22 feature is built, publish a preflight coverage report for every minimum source and every season 2018-2024. It records expected and observed date ranges, queries, row/game counts, schemas, missing critical fields, duplicates, maximum/minimum event date, retrieval times, and SHA-256, and proves that each fold's prior-season pool is complete enough for the declared feature contract. At design time the audited v5.1 artifact set proves acquisition beginning in 2022 but does not prove the required 2018-2021 sources. Acquiring and validating 2018-2021 is therefore an explicit implementation prerequisite and current gap; H22 cannot start by assuming it exists.

Data acquisition must be query-specific, resumable, content-addressed, idempotent, bounded by size, and capable of splitting oversized date ranges without changing the logical query identity.

Historical MLB feeds that show final lineups or actual starters are stored only as reconstructed participant evidence. They cannot be promoted to T-45 provenance. Strictly as-of weather, roof, defense, injury, restriction, and odds data are used only when their observation timestamp and query are preserved; otherwise neutral/null behavior applies.

Prospective collection follows Section 6.1, writes append-only timestamped snapshots, and seals only a snapshot satisfying its maximum-age and clock rules. Clock skew, source time zone, scheduled-start changes, doubleheaders, postponements, and resumed games receive explicit normalized fields and reason codes.

## 8. Pipeline architecture and error handling

Generalize the pipeline around `RunSpec`, `FoldSpec`, `ModelCard`, `AsOfSnapshot`, `ProvenanceStratum`, and `SampleLedger` rather than hard-coded v5.1 years or a 100-date target. The same pure feature/scoring/ranking functions operate across folds; data roots, cutoffs, calibration cards, and target counts are injected.

Stages remain physically separated: `raw -> normalized snapshots -> features -> locked predictions/ranks -> outcomes/baselines -> reports`. Feature and lock stages have no import path or filesystem capability for outcome stores. Fold training, target scoring, historical reporting, and prospective reporting have different output namespaces.

Errors fail closed:

- retry transient HTTP/rate-limit failures with bounded exponential backoff and preserve attempts;
- quarantine schema drift, malformed rows, duplicate `gamePk`, non-monotonic dates, and hash mismatches;
- mark missing critical evidence ineligible or `UNRATED` according to the frozen rule; never impute from target outcomes;
- mark source outage/clock failure `SYSTEM_MISS`, pre-lock cancellation `PRELOCK_NOT_SCHEDULED`, post-lock unresolved cancellation `VOID`, and no eligible slate `NO_ELIGIBLE_DATE` distinctly;
- stop before outcome join on replay mismatch, noncanonical JSON, incomplete manifest, or fold leakage;
- resume from the last verified immutable stage without overwriting prior snapshots.

## 9. Testing and verification

Required automated coverage includes:

- exact preservation of all final/internal weights, signs, shrinkage, clipping, neutralization, probability bounds, and ranking tie-breakers;
- exact completeness arithmetic from component qualities alone, separation of agreement and legacy audit score, and every boundary/fallback in the complete tier cascade;
- table-driven eligibility/reason-code tests covering exclude, official, shadow, neutral optional component, no-eligible, no-verified-eligible, shadow-only, system-miss, and run-invalid dispositions;
- rolling-origin fold construction, including intentional future-season and 2025 injections that must fail;
- OOF calibration isolation, the exact 2026 OOF sequence, allowed target-year D-1 history, card sealing, and model-card maximum-date assertions;
- T-45 polling cadence, five-minute freshness, two-second NTP tolerance, late response rejection, pre-lock start-time recomputation with snapshot invalidation, immutable lock-basis sealing, post-lock `latest_effective_start_utc`, no re-lock invariant, rescheduled games, doubleheaders, postponed/resumed games, time zones, and provenance classification;
- target `gamePk` and same-day event exclusion, outcome-store import denial, and market-schema exclusion;
- official prospective ranking isolation from shadow rows and deterministic `p_home == 0.5` side/rank behavior;
- exact terminal mapping for Final, Completed Early, cancellation, suspension/PENDING, deadline conversion, tie/abandoned VOID, doubleheaders, chronological replacements, and late-result immutability;
- exactly one Top 1 per counted date, exactly 500 graded historical dates, VOID extension, no-eligible/system-miss handling, and 2025 exclusion;
- exact paired baseline tables, Wilson interval, McNemar, Brier, log loss, ECE, rank, temporal, team, and component diagnostics;
- minimum-source preflight coverage and explicit optional-source neutralization across every required 2018-2024 season;
- raw and derived hash verification, canonical byte replay, interruption/resume, schema drift, rate limit, and oversized extract splitting;
- separate historical/prospective namespace and rejection of any attempted pooling;
- a miniature multi-fold end-to-end fixture plus manual source-to-lock-to-outcome audits for at least three dates per historical fold and three prospective dates once available.

The final workbook verifier must scan formulas for errors, recalculate values, confirm 500 unique graded historical dates, validate all metric totals against JSON, and render every sheet for visual inspection.

## 10. Deliverables

The implementation produces:

1. `reports/v5_1_bt/diagnostic_100.md` and `.json`, containing every Section 3 contract item and source hash.
2. Frozen `config/v5_2_qd.json`, schema documentation, and one model card per H22/H23/H24 fold.
3. Historical raw manifests, OOF calibration ledgers, full candidate rankings, sealed Top 1 ledger, outcomes, paired baselines, diagnostics, audit report, and a `v5.2-QD Historical Walk-forward Robustness 500` workbook.
4. A prospective 2026 ledger with timestamped raw snapshots, daily locks, system-miss/void records, and milestone reports beginning 2026-07-13.
5. A machine-readable run manifest binding code, configuration, source, model-card, prediction, outcome, metric, and workbook hashes.

## 11. Acceptance criteria

The design is implemented only when all of the following are proven by current artifacts and tests:

- The 100-pick diagnostic reproduces every exact result in Section 3 and concludes that incremental value and live reproducibility remain unproven.
- v5.2-QD preserves the predictive/internal weights, shrinkage, and ranking; uses rolling-origin OOF calibration; and contains no 2025 outcome in fit or rule selection.
- Confidence is absent from new user-facing semantics; completeness uses component qualities only; agreement and the legacy audit score cannot affect it; the full tier cascade and `UNRATED` behavior are enforced; and environment/defense activate only from strict as-of evidence.
- Every row has an immutable provenance stratum and verified T-45 rows are never mixed with reconstructed rows.
- The preflight proves every minimum 2018-2024 source; the presently missing 2018-2021 evidence is acquired and validated before H22; optional sources remain clearly separate.
- The historical run contains exactly 500 unique graded Daily Top 1 dates from 2022-2024, follows the fixed H22/H23/H24 chronology, applies the frozen terminal deadline, extends only for documented VOID/no-eligible/system-miss dates, and never enters 2025.
- The historical report carries every baseline, metric, limitation, and the label `Historical Walk-forward Robustness`; it never claims official independent 500-pick confirmation.
- The prospective ledger begins no earlier than 2026-07-13, uses the exact 2019-2024 OOF calibration contract, satisfies the frozen clock/cadence/freshness rules, seals an immutable observable-at-lock start basis, never re-locks after schedule revision, ranks only timestamp-verified T-45 candidates, stays separate, never backfills misses, and labels interim results as incomplete.
- Leakage, replay, hash, error-handling, paired-metric, workbook, and visual verification gates all pass with preserved evidence.
- No unsupported market, ROI, CLV, profitability, actionable-betting, or predictive-improvement claim appears in any artifact.

Failure of any hard integrity criterion invalidates the affected run; it cannot be repaired by favorable results or presentation changes.
