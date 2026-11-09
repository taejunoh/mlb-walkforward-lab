# v5.1-BT Manual Audit Record

## Audit scope

- Protocol: Retrospective Daily Top 1, model `v5.1-BT`, Backtest.
- First 100 MLB game dates: 2025-05-01–2025-08-12. There were 0 `VOID` Top 1 picks, so no replacement extension through 2025-09-28 was used.
- Every lock time is exactly T-45 minutes before the scheduled first pitch.
- For the 3 dates below, game participants, actual starters, lock times, D-1 cutoff, predictions, and final results were checked against the original artifacts.

## 2025-05-01 — gamePk 778097

- Participants/venue: Kansas City Royals (away) @ Tampa Bay Rays (home), George M. Steinbrenner Field.
- Actual starters: Seth Lugo (607625, KC) / Shane Baz (669358, TB). Both teams have probable-starter mismatch=`false`; starter provenance=`RECONSTRUCTED_ACTUAL_STARTER`.
- Final-lineup participant IDs: KC `[663697, 677951, 686469, 521692, 672580, 686681, 592192, 666023, 671221]`; TB `[802415, 650490, 664040, 666018, 691406, 670224, 608671, 676609, 666163]`.
- Start/T45: `2025-05-01T17:10:00Z` / `2025-05-01T16:25:00Z`.
- Top 1: Tampa Bay Rays, probability 0.6193001628, starter edge 12.2696732234, offense edge 1.0866787893, bullpen edge 8.4563961651, completeness 0.3857785317.
- Result: Kansas City Royals won 8-2. Prediction missed.
- D-1 cutoff: both max history date and schedule-used max date were `2025-04-30`; target Statcast was excluded (`statcast_target_excluded=true`). D-1 league ERA 4.031644004944376.
- Source hashes: schedule manifest `50c009b7865a1a8f11d5342014f4e6dca986cb210b13ab9ec49326e55d1c8dd8`; D-1 Statcast manifest `ed97c3ddfff25ac32582fc90eb44efe9255d1754612129af83daf7962b653dc4`; D-1 league-ERA manifest `0d68d617401a321ba6121ccc1ceeef0ed40959779a3a15f8353da21ce2fc2794`.

## 2025-06-15 — gamePk 777499

- Participants/venue: Toronto Blue Jays (away) @ Philadelphia Phillies (home), Citizens Bank Park.
- Actual starters: José Berríos (621244, TOR) / Zack Wheeler (554430, PHI). Both teams have probable-starter mismatch=`false`; starter provenance=`RECONSTRUCTED_ACTUAL_STARTER`.
- Final-lineup participant IDs: TOR `[666182, 665489, 680718, 672386, 665926, 676391, 702176, 683770, 682729]`; PHI `[607208, 656941, 664761, 592206, 596146, 592663, 681082, 805249, 669016]`.
- Start/T45: `2025-06-15T17:35:00Z` / `2025-06-15T16:50:00Z`.
- Top 1: Philadelphia Phillies, probability 0.6373237432, starter edge 19.7651012334, offense edge -0.8056385471, bullpen edge 4.0018998301, completeness 0.5123861951.
- Result: Philadelphia Phillies won 11-4. Prediction hit.
- D-1 cutoff: both max history date and schedule-used max date were `2025-06-14`; target Statcast was excluded. D-1 league ERA 4.027113749398364.
- Source hashes: schedule manifest `50c009b7865a1a8f11d5342014f4e6dca986cb210b13ab9ec49326e55d1c8dd8`; D-1 Statcast manifest `ce90b30119339820ee466970a00501afee3c35f4e5a27ca9173b1f8f01145edf`; D-1 league-ERA manifest `ede85a508febeb6b08c9f6dde59c94fc5f6931393cb6f324b078b84427ada97c`.

## 2025-08-12 — gamePk 776774

- Participants/venue: Minnesota Twins (away) @ New York Yankees (home), Yankee Stadium.
- Actual starters: Travis Adams (701519, MIN) / Carlos Rodón (607074, NYY). Both teams have probable-starter mismatch=`false`; starter provenance=`RECONSTRUCTED_ACTUAL_STARTER`.
- Final-lineup participant IDs: MIN `[668885, 621439, 680777, 807712, 668904, 665019, 681508, 666397, 680837]`; NYY `[663757, 592450, 641355, 519317, 700250, 665862, 502671, 641857, 683011]`.
- Start/T45: `2025-08-12T23:05:00Z` / `2025-08-12T22:20:00Z`.
- Top 1: New York Yankees, probability 0.6492080217, starter edge 9.6849413444, offense edge 13.8366384135, bullpen edge 6.3146300496, schedule edge 5.0, completeness 0.3668183945.
- Result: New York Yankees won 9-1. Prediction hit.
- D-1 cutoff: both max history date and schedule-used max date were `2025-08-11`; target Statcast was excluded. D-1 league ERA 4.1289829239223295.
- Source hashes: schedule manifest `50c009b7865a1a8f11d5342014f4e6dca986cb210b13ab9ec49326e55d1c8dd8`; D-1 Statcast manifest `f7571b3c2b6287bb9b42d72d9a40c887c37b13903cd7dfa6989a528629a7f4c2`; D-1 league-ERA manifest `c6382d44818a8deb6eafc8671ad8a2d34fb54500a302d3ff6cdd4c4a3f36bf3e`.

## Overall results

- Top 1: 67 wins, 33 losses, N=100, accuracy 0.67, Wilson 95% CI `[0.5730516033971139, 0.7543701620610456]`.
- Brier 0.22137165792176453, log loss 0.6351164919136184, ECE 0.11508160079200008, maximum losing streak 5.
- Top 3: 186 wins, 114 losses, N=300, accuracy 0.62.
- Home-team baseline: 59/100=0.59. D-1 season winning-percentage top-team baseline: 65/99=0.6565656565656566 (1 tie excluded).
- Market comparison: paired market N=0, so no advantage over the market was calculated.

## Important limitations

- All 1,340 locked records are `INCOMPLETE`, `High` uncertainty, and `Toss-up` tier because defense and environment are both neutral 50 with quality 0.
- Lineups are `RECONSTRUCTED_FINAL_LINEUP`, reconstructed from the postgame confirmed lineup. This is not the lineup snapshot available at actual T-45.
- Starters are `RECONSTRUCTED_ACTUAL_STARTER`. Participant identity is guaranteed, but this does not mean the starter matches the actual probable-starter snapshot at the time.
- There are no market snapshots, so paired market N=0 and no performance claim against the market can be made.
- Original downloads extend through 2025-09-28, but feature calculations for each game were restricted to `max_history_date <= D-1` and excluded target Statcast.
