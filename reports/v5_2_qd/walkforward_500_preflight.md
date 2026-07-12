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

## Optional source evidence

| Source | Status | Fallback | Queries | Errors |
|---|---|---|---:|---:|
| market_odds | ABSENT_NULL | ABSENT_NULL | 0 | 0 |
| environment | ABSENT_NEUTRAL | ABSENT_NEUTRAL | 0 | 0 |
| defense | ABSENT_NEUTRAL | ABSENT_NEUTRAL | 0 | 0 |
| catcher | ABSENT_NEUTRAL | ABSENT_NEUTRAL | 0 | 0 |
| injury_restriction | ABSENT_NULL | ABSENT_NULL | 0 | 0 |

Optional evidence never blocks minimum-source READY; absent or invalid evidence uses the frozen neutral/null fallback.

H22 fitting permitted: No
