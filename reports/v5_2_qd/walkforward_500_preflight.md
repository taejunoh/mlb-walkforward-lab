# v5.2-QD Historical 500 Source Preflight

Status: **BLOCKED_REQUIRED_ACQUISITION**

| Season | Minimum contract | Blocking source status |
|---|---|---|
| 2018 | MISSING | mlb_schedule_game_feed=MISSING, baseball_savant_statcast=MISSING, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=MISSING |
| 2019 | MISSING | mlb_schedule_game_feed=MISSING, baseball_savant_statcast=MISSING, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=MISSING |
| 2020 | MISSING | mlb_schedule_game_feed=MISSING, baseball_savant_statcast=MISSING, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=MISSING |
| 2021 | MISSING | mlb_schedule_game_feed=MISSING, baseball_savant_statcast=MISSING, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=MISSING |
| 2022 | MISSING | mlb_schedule_game_feed=OBSERVED_LEGACY_NOT_V52_COMPLETE, baseball_savant_statcast=OBSERVED_LEGACY_NOT_V52_COMPLETE, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=OBSERVED_LEGACY_NOT_V52_COMPLETE |
| 2023 | MISSING | mlb_schedule_game_feed=OBSERVED_LEGACY_NOT_V52_COMPLETE, baseball_savant_statcast=OBSERVED_LEGACY_NOT_V52_COMPLETE, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=OBSERVED_LEGACY_NOT_V52_COMPLETE |
| 2024 | MISSING | mlb_schedule_game_feed=OBSERVED_LEGACY_NOT_V52_COMPLETE, baseball_savant_statcast=OBSERVED_LEGACY_NOT_V52_COMPLETE, mlb_stats_pitching=OBSERVED_LEGACY_NOT_V52_COMPLETE, venue_team_timezone_table=MISSING, prior_schedule_history=OBSERVED_LEGACY_NOT_V52_COMPLETE |

## Required gamePk coverage contract

Game-grain schedule, Statcast, and prior-schedule sources must expose the exact same nonempty gamePk set per season. Pitching and venue/timezone reference grains must expose a nonempty subset of that schedule set and may not contain extra gamePk values.

## Inventory errors

| Path | Code | Detail |
|---|---|---|
| none | none | none |

## Optional source evidence

| Source | Status | Fallback | Queries | Errors |
|---|---|---|---:|---:|
| market_odds | ABSENT_NULL | ABSENT_NULL | 0 | 0 |
| environment | ABSENT_NEUTRAL | ABSENT_NEUTRAL | 0 | 0 |
| defense | ABSENT_NEUTRAL | ABSENT_NEUTRAL | 0 | 0 |
| catcher | ABSENT_NEUTRAL | ABSENT_NEUTRAL | 0 | 0 |
| injury_restriction | ABSENT_NULL | ABSENT_NULL | 0 | 0 |

Optional evidence never blocks minimum-source READY; absent or invalid evidence uses the frozen neutral/null fallback.

## Acquisition steps

| Stage | Status | Prerequisite | Command |
|---|---|---|---|
| acquire-raw-2018-2024 | NOT_IMPLEMENTED_PLANNED | approved deferred execution plan | PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --years 2018:2024 --output data/v5_2_qd/raw |
| verify-source-coverage | NOT_IMPLEMENTED_PLANNED | acquire-raw-2018-2024 complete | PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources --root data/v5_2_qd/raw |

## Execution steps after READY

| Stage | Status | Prerequisite | Command |
|---|---|---|---|
| fit-fold-cards | NOT_IMPLEMENTED_PLANNED | preflight status READY | PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_fit --run-spec protocols/v5_2_qd/historical_walkforward_500.json |
| lock-historical-500 | NOT_IMPLEMENTED_PLANNED | H22/H23/H24 cards sealed | PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_run --run-spec protocols/v5_2_qd/historical_walkforward_500.json --stop-after lock |
| join-outcomes | NOT_IMPLEMENTED_PLANNED | all candidate/date locks sealed | PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_run --run-spec protocols/v5_2_qd/historical_walkforward_500.json --resume outcomes |
| report-and-verify | NOT_IMPLEMENTED_PLANNED | exactly 500 graded dates resolved | PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_run --run-id historical_walkforward_500 |

H22 fitting permitted: No
