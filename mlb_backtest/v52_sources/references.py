"""Official team/venue identities plus reviewed effective timezones."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..v52_evidence import LogicalQuery
from ..v52_source_contracts import query_sidecar, require_query


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _payload(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError(f"{label} payload must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} payload must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} payload root must be an object")
    return value


def _team_records(payloads: Sequence[bytes]) -> list[dict[str, object]]:
    teams: dict[int, dict[str, object]] = {}
    for raw in payloads:
        values = _payload(raw, "team").get("teams")
        if not isinstance(values, list):
            raise ValueError("team payload teams must be a list")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("team identity must be an object")
            venue = value.get("venue")
            if not isinstance(venue, dict):
                raise ValueError("team home venue must be an object")
            record = {
                "home_venue_id": _positive(venue.get("id"), "team home venue id"),
                "location_name": _text(value.get("locationName"), "team location"),
                "team_id": _positive(value.get("id"), "team id"),
                "team_name": _text(value.get("name"), "team name"),
            }
            team_id = int(record["team_id"])
            if team_id in teams and teams[team_id] != record:
                raise ValueError("conflicting team identity")
            teams[team_id] = record
    return [teams[key] for key in sorted(teams)]


def _coordinate(value: object, label: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"venue {label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError(f"venue {label} is outside its valid range")
    return result


def _venue_records(payloads: Sequence[bytes]) -> list[dict[str, object]]:
    venues: dict[int, dict[str, object]] = {}
    for raw in payloads:
        values = _payload(raw, "venue").get("venues")
        if not isinstance(values, list):
            raise ValueError("venue payload venues must be a list")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("venue identity must be an object")
            location = value.get("location")
            coordinates = (
                location.get("defaultCoordinates")
                if isinstance(location, dict)
                else None
            )
            if not isinstance(location, dict) or not isinstance(coordinates, dict):
                raise ValueError("venue location and coordinates are required")
            record = {
                "city": _text(location.get("city"), "venue city"),
                "latitude": _coordinate(
                    coordinates.get("latitude"), "latitude", -90.0, 90.0
                ),
                "longitude": _coordinate(
                    coordinates.get("longitude"), "longitude", -180.0, 180.0
                ),
                "venue_id": _positive(value.get("id"), "venue id"),
                "venue_name": _text(value.get("name"), "venue name"),
            }
            venue_id = int(record["venue_id"])
            if venue_id in venues and venues[venue_id] != record:
                raise ValueError("conflicting venue identity")
            venues[venue_id] = record
    return [venues[key] for key in sorted(venues)]


def recompute_team_query_sidecar(
    raw: bytes, query: LogicalQuery
) -> Mapping[str, object]:
    require_query(
        query,
        source="venue_team_timezone_table",
        grain="effective_dated_venue_team_reference",
        endpoint="https://statsapi.mlb.com/api/v1/teams",
        expected_media_type="application/json",
    )
    season = query.requested_range.get("season")
    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season not in range(2018, 2025)
        or query.requested_range.get("entity") != "teams"
    ):
        raise ValueError("team query must bind a 2018-2024 teams season")
    if dict(query.parameters) != {
        "sportId": 1,
        "season": season,
        "hydrate": "venue(location)",
    }:
        raise ValueError("team query parameters differ from requested season")
    return query_sidecar(raw, query, {"season": season, "teams": _team_records([raw])})


def recompute_venue_query_sidecar(
    raw: bytes, query: LogicalQuery
) -> Mapping[str, object]:
    require_query(
        query,
        source="venue_team_timezone_table",
        grain="effective_dated_venue_team_reference",
        expected_media_type="application/json",
    )
    venue_id = _positive(query.requested_range.get("venue_id"), "venue query id")
    if query.endpoint != f"https://statsapi.mlb.com/api/v1/venues/{venue_id}":
        raise ValueError("venue endpoint differs from requested venue ID")
    if dict(query.parameters) != {"hydrate": "location"}:
        raise ValueError("venue query parameters differ from source contract")
    venues = _venue_records([raw])
    if len(venues) != 1 or venues[0]["venue_id"] != venue_id:
        raise ValueError("venue response must contain exactly the requested venue")
    return query_sidecar(raw, query, {"venues": venues})


def _timezone_records(
    rows: Sequence[Mapping[str, object]],
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("timezone rows must be mappings")
        venue_id = _positive(raw.get("venue_id"), "timezone venue_id")
        first = _day(raw.get("effective_start"), "timezone effective start")
        last = _day(raw.get("effective_end"), "timezone effective end")
        if first > last:
            raise ValueError("timezone effective range is reversed")
        name = _text(raw.get("time_zone"), "IANA time zone")
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone row must use a valid IANA ZoneInfo name") from exc
        result.setdefault(venue_id, []).append(
            {
                "effective_end": last.isoformat(),
                "effective_start": first.isoformat(),
                "time_zone": name,
                "venue_id": venue_id,
            }
        )
    for venue_id, records in result.items():
        records.sort(key=lambda row: (row["effective_start"], row["effective_end"]))
        for previous, current in zip(records, records[1:]):
            if _day(current["effective_start"], "timezone start") <= _day(
                previous["effective_end"], "timezone end"
            ):
                raise ValueError(
                    f"timezone effective ranges overlap for venue {venue_id}"
                )
    return result


def recompute_reference_sidecar(
    team_payloads: Sequence[bytes],
    venue_payloads: Sequence[bytes],
    timezone_rows: Sequence[Mapping[str, object]],
    schedules: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    if any(
        isinstance(values, (str, bytes)) or not isinstance(values, Sequence)
        for values in (team_payloads, venue_payloads, timezone_rows, schedules)
    ):
        raise ValueError("reference inputs must be sequences")
    if any(
        not values
        for values in (team_payloads, venue_payloads, timezone_rows, schedules)
    ):
        raise ValueError("reference aggregate requires nonempty source populations")
    teams = _team_records(team_payloads)
    venues = _venue_records(venue_payloads)
    timezones = _timezone_records(timezone_rows)
    team_ids = {int(row["team_id"]) for row in teams}
    venue_ids = {int(row["venue_id"]) for row in venues}
    observed_teams: set[int] = set()
    observed: dict[tuple[int, str], tuple[bool, bool]] = {}
    for schedule in schedules:
        if not isinstance(schedule, Mapping):
            raise ValueError("schedule reference rows must be mappings")
        official = _day(schedule.get("official_date"), "schedule official date")
        away = _positive(schedule.get("away_team_id"), "schedule away team")
        home = _positive(schedule.get("home_team_id"), "schedule home team")
        venue_id = _positive(schedule.get("venue_id"), "schedule venue")
        observed_teams.update((away, home))
        key = (venue_id, official.isoformat())
        neutral = schedule.get("neutral_site")
        alternate = schedule.get("alternate_site")
        if type(neutral) is not bool:
            raise ValueError("schedule neutral_site must be boolean")
        if type(alternate) is not bool:
            raise ValueError("schedule alternate_site must be boolean")
        site_disposition = (neutral, alternate)
        if key in observed and observed[key] != site_disposition:
            raise ValueError("conflicting alternate/neutral venue disposition")
        observed[key] = site_disposition
    missing_teams = observed_teams - team_ids
    if missing_teams:
        raise ValueError(f"official team identity missing for team {min(missing_teams)}")
    missing_venues = {key[0] for key in observed} - venue_ids
    if missing_venues:
        raise ValueError(
            f"official venue identity missing for venue {min(missing_venues)}"
        )
    mappings = []
    for (venue_id, official_text), (neutral, alternate) in sorted(observed.items()):
        official = date.fromisoformat(official_text)
        matches = [
            row
            for row in timezones.get(venue_id, [])
            if _day(row["effective_start"], "timezone start")
            <= official
            <= _day(row["effective_end"], "timezone end")
        ]
        if len(matches) != 1:
            raise ValueError(
                "every observed venue/date must resolve to exactly one timezone mapping"
            )
        mappings.append(
            {
                "alternate_site": alternate,
                "neutral_site": neutral,
                "official_date": official_text,
                "time_zone": matches[0]["time_zone"],
                "venue_id": venue_id,
            }
        )
    return {
        "grain": "effective_dated_venue_team_reference",
        "observed_venue_date_mappings": mappings,
        "schema_version": 1,
        "team_payload_sha256s": sorted(
            hashlib.sha256(raw).hexdigest() for raw in team_payloads
        ),
        "teams": teams,
        "timezone_rows": [
            row for venue_id in sorted(timezones) for row in timezones[venue_id]
        ],
        "venue_payload_sha256s": sorted(
            hashlib.sha256(raw).hexdigest() for raw in venue_payloads
        ),
        "venues": venues,
    }


__all__ = [
    "recompute_reference_sidecar",
    "recompute_team_query_sidecar",
    "recompute_venue_query_sidecar",
]
