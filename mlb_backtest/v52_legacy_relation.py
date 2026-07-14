"""Closed current-to-protected-legacy source relationships."""

from __future__ import annotations

import re


_CAREER_ENDPOINT = re.compile(
    r"^https://statsapi\.mlb\.com/api/v1/people/[1-9][0-9]*/stats$"
)


def is_approved_legacy_source_relation(
    *,
    current_source: object,
    endpoint: object,
    grain: object,
    parser_version: object,
    expected_media_type: object,
    legacy_source: object,
) -> bool:
    """Return whether one exact current contract may cite a legacy source."""
    if parser_version != "v5.2.1":
        return False
    fixed = {
        (
            "mlb_schedule_game_feed",
            "https://statsapi.mlb.com/api/v1/schedule",
            "season_schedule_and_game",
            "application/json",
        ): "mlb_schedule",
        (
            "baseball_savant_statcast",
            "https://baseballsavant.mlb.com/statcast_search/csv",
            "pitch",
            "text/csv",
        ): "baseball_savant_statcast",
        (
            "mlb_stats_pitching",
            "https://statsapi.mlb.com/api/v1/stats",
            "season_d1_league_pitching_reference",
            "application/json",
        ): "mlb_stats_pitching",
    }
    key = (current_source, endpoint, grain, expected_media_type)
    if fixed.get(key) == legacy_source:
        return True
    return (
        current_source == "mlb_stats_pitcher_bf"
        and grain == "player_career_batters_faced_reference"
        and expected_media_type == "application/json"
        and legacy_source == "mlb_stats_pitcher_bf"
        and isinstance(endpoint, str)
        and _CAREER_ENDPOINT.fullmatch(endpoint) is not None
    )


__all__ = ["is_approved_legacy_source_relation"]
