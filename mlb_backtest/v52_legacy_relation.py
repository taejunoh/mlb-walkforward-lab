"""Closed current-query to protected-legacy relationships."""

from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from collections.abc import Mapping
from datetime import date
from typing import Protocol
from urllib.parse import urlencode

from .v52_seasons import SEASON_BOUNDARIES


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CAREER_ENDPOINT = re.compile(
    r"^https://statsapi\.mlb\.com/api/v1/people/[1-9][0-9]*/stats$"
)


class LogicalQueryShape(Protocol):
    source: str
    grain: str
    endpoint: str
    parameters: Mapping[str, object]
    requested_range: Mapping[str, object]
    expected_media_type: str
    parser_version: str
    parent_query_id: str | None
    run_spec_sha256: str
    acquisition_contract_sha256: str


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


def _canonical_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _ordered_url(
    endpoint: str, parameters: Mapping[str, object], keys: tuple[str, ...]
) -> str:
    return f"{endpoint}?{urlencode(tuple((key, parameters[key]) for key in keys))}"


def _consumer_binding_matches(
    requested: Mapping[str, object],
    *,
    exact_cutoff: str | None = None,
    earliest_cutoff: date | None = None,
) -> bool:
    consumers = requested.get("consuming_game_pks")
    cutoffs = requested.get("consumer_cutoff_dates")
    if (
        not isinstance(consumers, list)
        or not consumers
        or any(type(game_pk) is not int or game_pk <= 0 for game_pk in consumers)
        or consumers != sorted(set(consumers))
        or not isinstance(cutoffs, Mapping)
        or set(cutoffs) != {str(game_pk) for game_pk in consumers}
    ):
        return False
    for value in cutoffs.values():
        parsed = _canonical_day(value)
        if (
            parsed is None
            or (exact_cutoff is not None and value != exact_cutoff)
            or (earliest_cutoff is not None and parsed < earliest_cutoff)
        ):
            return False
    return True


def _career_baseline_eligible(
    requested: Mapping[str, object], baseline: str
) -> bool:
    baseline_day = _canonical_day(baseline)
    if baseline_day is None or not _consumer_binding_matches(
        requested, earliest_cutoff=baseline_day
    ):
        return False
    cutoffs = requested.get("consumer_cutoff_dates")
    if not isinstance(cutoffs, Mapping):
        return False
    parsed = tuple(_canonical_day(value) for value in cutoffs.values())
    if any(value is None for value in parsed):
        return False
    boundary = date(2022, 1, 1)
    if baseline == "2017-12-31":
        return all(value < boundary for value in parsed if value is not None)
    if baseline == "2021-12-31":
        return all(value >= boundary for value in parsed if value is not None)
    return False


def _query_id(
    query: LogicalQueryShape,
    *,
    parameters: Mapping[str, object],
    requested_range: Mapping[str, object],
    parent_query_id: str | None,
) -> str:
    record = {
        "acquisition_contract_sha256": query.acquisition_contract_sha256,
        "endpoint": query.endpoint,
        "expected_media_type": query.expected_media_type,
        "grain": query.grain,
        "parameters": dict(parameters),
        "parent_query_id": parent_query_id,
        "parser_version": query.parser_version,
        "requested_range": dict(requested_range),
        "run_spec_sha256": query.run_spec_sha256,
        "source": query.source,
    }
    encoded = json.dumps(
        record,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _statcast_parameters(first: str, last: str) -> dict[str, str]:
    return {
        "all": "true",
        "type": "details",
        "player_type": "batter",
        "game_date_gt": first,
        "game_date_lt": last,
        "hfGT": "R|",
        "group_by": "name-year",
        "sort_col": "pitches",
        "player_event_sort": "api_p_release_speed",
        "sort_order": "desc",
        "min_pitches": "0",
    }


def _schedule_matches(
    query: LogicalQueryShape, legacy: Mapping[str, object]
) -> bool:
    requested = query.requested_range
    year = requested.get("year")
    if type(year) is not int or year not in {2022, 2023, 2024}:
        return False
    first, last = SEASON_BOUNDARIES[year]
    parameters = {
        "sportId": 1,
        "gameType": "R",
        "startDate": first.isoformat(),
        "endDate": last.isoformat(),
        "hydrate": "probablePitcher,lineups,venue,linescore",
    }
    return (
        set(requested) == {"year", "start_date", "end_date"}
        and requested.get("start_date") == first.isoformat()
        and requested.get("end_date") == last.isoformat()
        and query.parent_query_id is None
        and dict(query.parameters) == parameters
        and dict(legacy)
        == {
            "end": last.isoformat(),
            "gameType": "R",
            "role": "training",
            "start": first.isoformat(),
            "url": _ordered_url(
                query.endpoint,
                parameters,
                ("sportId", "gameType", "startDate", "endDate", "hydrate"),
            ),
        }
    )


def _statcast_matches(
    query: LogicalQueryShape,
    legacy: Mapping[str, object],
    protected_inventory_sha256: str,
) -> bool:
    requested = query.requested_range
    if set(requested) != {
        "year",
        "start_date",
        "end_date",
        "acquisition",
        "protected_inventory_sha256",
    }:
        return False
    year = requested.get("year")
    first = _canonical_day(requested.get("start_date"))
    last = _canonical_day(requested.get("end_date"))
    if (
        type(year) is not int
        or year not in {2022, 2023, 2024}
        or first is None
        or last is None
        or first.year != year
        or last.year != year
        or first.month != last.month
        or first > last
        or not SEASON_BOUNDARIES[year][0] <= first <= last <= SEASON_BOUNDARIES[year][1]
        or requested.get("acquisition") != "PROTECTED_LEGACY_REFERENCE"
        or requested.get("protected_inventory_sha256")
        != protected_inventory_sha256
    ):
        return False
    child_parameters = _statcast_parameters(first.isoformat(), last.isoformat())
    if dict(query.parameters) != child_parameters:
        return False
    season_first, season_last = SEASON_BOUNDARIES[year]
    parent_first = max(season_first, date(year, first.month, 1))
    parent_last = min(
        season_last, date(year, first.month, monthrange(year, first.month)[1])
    )
    parent_parameters = _statcast_parameters(
        parent_first.isoformat(), parent_last.isoformat()
    )
    parent_requested = {
        "year": year,
        "start_date": parent_first.isoformat(),
        "end_date": parent_last.isoformat(),
    }
    if (
        not isinstance(query.parent_query_id, str)
        or _SHA256.fullmatch(query.parent_query_id) is None
        or query.parent_query_id
        != _query_id(
            query,
            parameters=parent_parameters,
            requested_range=parent_requested,
            parent_query_id=None,
        )
    ):
        return False
    return dict(legacy) == {
        "end": last.isoformat(),
        "game_type": "R",
        "start": first.isoformat(),
        "url": _ordered_url(
            query.endpoint,
            child_parameters,
            tuple(child_parameters),
        ),
    }


def _d1_matches(query: LogicalQueryShape, legacy: Mapping[str, object]) -> bool:
    requested = query.requested_range
    if set(requested) != {
        "season",
        "start_date",
        "as_of_date",
        "consuming_game_pks",
        "consumer_cutoff_dates",
        "response_completeness",
    }:
        return False
    season = requested.get("season")
    start = _canonical_day(requested.get("start_date"))
    as_of = _canonical_day(requested.get("as_of_date"))
    if (
        type(season) is not int
        or season not in {2022, 2023, 2024}
        or start != SEASON_BOUNDARIES[season][0]
        or as_of is None
        or not start <= as_of <= SEASON_BOUNDARIES[season][1]
    ):
        return False
    parameters: dict[str, object] = {
        "stats": "byDateRange",
        "group": "pitching",
        "sportIds": 1,
        "gameType": "R",
        "playerPool": "ALL",
        "limit": 5000,
        "startDate": start.isoformat(),
        "endDate": as_of.isoformat(),
    }
    return (
        query.parent_query_id is None
        and requested.get("response_completeness")
        == {"limit": 5000, "reject_total_splits_at_or_above_limit": True}
        and _consumer_binding_matches(requested, exact_cutoff=as_of.isoformat())
        and dict(query.parameters) == parameters
        and dict(legacy)
        == {
            **parameters,
            "url": _ordered_url(
                query.endpoint,
                parameters,
                (
                    "stats",
                    "group",
                    "sportIds",
                    "gameType",
                    "playerPool",
                    "limit",
                    "startDate",
                    "endDate",
                ),
            ),
        }
    )


def _career_matches(
    query: LogicalQueryShape, legacy: Mapping[str, object]
) -> bool:
    requested = query.requested_range
    if set(requested) != {
        "pitcher_id",
        "inclusive_end",
        "consuming_game_pks",
        "consumer_cutoff_dates",
    }:
        return False
    pitcher_id = requested.get("pitcher_id")
    baseline = requested.get("inclusive_end")
    if (
        type(pitcher_id) is not int
        or pitcher_id <= 0
        or not isinstance(baseline, str)
        or baseline not in {"2017-12-31", "2021-12-31"}
        or query.endpoint
        != f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
        or query.parent_query_id is not None
        or not _career_baseline_eligible(requested, baseline)
    ):
        return False
    parameters: dict[str, object] = {
        "stats": "byDateRange",
        "group": "pitching",
        "gameType": "R",
        "sportIds": 1,
        "startDate": "1876-01-01",
        "endDate": baseline,
    }
    return (
        dict(query.parameters) == parameters
        and dict(legacy)
        == {
            **parameters,
            "pitcher_id": pitcher_id,
            "url": _ordered_url(
                query.endpoint,
                parameters,
                (
                    "stats",
                    "group",
                    "gameType",
                    "sportIds",
                    "startDate",
                    "endDate",
                ),
            ),
        }
    )


def matches_current_query_to_legacy(
    query: LogicalQueryShape,
    *,
    legacy_source: object,
    legacy_query: object,
    protected_inventory_sha256: object,
) -> bool:
    """Match one current logical query to one exact protected canonical query."""
    if not isinstance(legacy_query, Mapping) or not isinstance(
        protected_inventory_sha256, str
    ):
        return False
    if not is_approved_legacy_source_relation(
        current_source=query.source,
        endpoint=query.endpoint,
        grain=query.grain,
        parser_version=query.parser_version,
        expected_media_type=query.expected_media_type,
        legacy_source=legacy_source,
    ):
        return False
    try:
        if query.source == "mlb_schedule_game_feed":
            return _schedule_matches(query, legacy_query)
        if query.source == "baseball_savant_statcast":
            return _statcast_matches(
                query, legacy_query, protected_inventory_sha256
            )
        if query.source == "mlb_stats_pitching":
            return _d1_matches(query, legacy_query)
        if query.source == "mlb_stats_pitcher_bf":
            return _career_matches(query, legacy_query)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return False


__all__ = [
    "LogicalQueryShape",
    "is_approved_legacy_source_relation",
    "matches_current_query_to_legacy",
]
