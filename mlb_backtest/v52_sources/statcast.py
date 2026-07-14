"""Baseball Savant pitch-grain semantic recomputation."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from ..statcast import REQUIRED_FIELDS, STATCAST_ROW_CAP, parse_statcast
from ..v52_evidence import LogicalQuery
from ..v52_source_contracts import canonical_sha256, query_sidecar, require_query


_STATCAST_ENDPOINT = "https://baseballsavant.mlb.com/statcast_search/csv"


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


def _raw_row_count_and_fields(raw: bytes) -> tuple[int, list[str]]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    try:
        with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            fields = list(reader.fieldnames or ())
            if len(fields) != len(set(fields)):
                raise ValueError("Statcast CSV contains duplicate field names")
            missing = sorted(set(REQUIRED_FIELDS) - set(fields))
            if missing:
                raise ValueError(
                    f"statcast CSV missing required fields: {', '.join(missing)}"
                )
            return sum(1 for _ in reader), fields
    except UnicodeDecodeError as exc:
        raise ValueError("statcast CSV must be UTF-8-sig") from exc


def recompute_statcast_sidecar(
    raw: bytes, query: LogicalQuery
) -> Mapping[str, object]:
    require_query(
        query,
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint=_STATCAST_ENDPOINT,
        expected_media_type="text/csv",
    )
    first = _day(query.requested_range.get("start_date"), "Statcast query start")
    last = _day(query.requested_range.get("end_date"), "Statcast query end")
    year = query.requested_range.get("year")
    if (
        first > last
        or isinstance(year, bool)
        or not isinstance(year, int)
        or year not in range(2018, 2025)
        or first.year != year
        or last.year != year
    ):
        raise ValueError("Statcast query range differs from 2018-2024 year")
    expected_parameters = {
        "all": "true",
        "type": "details",
        "player_type": "batter",
        "game_date_gt": first.isoformat(),
        "game_date_lt": last.isoformat(),
        "hfGT": "R|",
        "group_by": "name-year",
        "sort_col": "pitches",
        "player_event_sort": "api_p_release_speed",
        "sort_order": "desc",
        "min_pitches": "0",
    }
    if dict(query.parameters) != expected_parameters:
        raise ValueError("Statcast query parameters differ from requested range")
    row_count, fields = _raw_row_count_and_fields(raw)
    if row_count >= STATCAST_ROW_CAP and first == last:
        raise ValueError("single-day Statcast response reached row cap")
    parsed = parse_statcast(raw)
    if row_count and not parsed.pitches:
        raise ValueError("Statcast payload contains no usable pitches")
    if any(
        not first <= _day(row["game_date"], "Statcast game_date") <= last
        for row in parsed.pitches
    ):
        raise ValueError("Statcast pitch is outside query range")

    games = []
    game_pks = sorted({int(row["game_pk"]) for row in parsed.pitches})
    for game_pk in game_pks:
        pitches = [row for row in parsed.pitches if row["game_pk"] == game_pk]
        terminals = [
            row for row in parsed.plate_appearances if row["game_pk"] == game_pk
        ]
        dates = sorted({str(row["game_date"]) for row in pitches})
        if len(dates) != 1:
            raise ValueError("Statcast game must have exactly one official date")
        terminal_projection = [
            {
                "at_bat_number": row["at_bat_number"],
                "batter_id": row["batter_id"],
                "events": row["events"],
                "pitch_number": row["pitch_number"],
                "pitcher_id": row["pitcher_id"],
            }
            for row in terminals
        ]
        games.append(
            {
                "batter_ids": sorted({int(row["batter_id"]) for row in pitches}),
                "game_date": dates[0],
                "game_pk": game_pk,
                "pitch_count": len(pitches),
                "pitcher_ids": sorted({int(row["pitcher_id"]) for row in pitches}),
                "terminal_digest": canonical_sha256(terminal_projection),
                "terminal_plate_appearance_count": len(terminals),
            }
        )
    semantics = {
        "batter_ids": sorted({int(row["batter_id"]) for row in parsed.pitches}),
        "coverage": {
            "inclusive_end": last.isoformat(),
            "inclusive_start": first.isoformat(),
        },
        "fields_present": sorted(fields),
        "first_game_date": min(
            (str(row["game_date"]) for row in parsed.pitches), default=None
        ),
        "game_pks": game_pks,
        "games": games,
        "last_game_date": max(
            (str(row["game_date"]) for row in parsed.pitches), default=None
        ),
        "pitcher_ids": sorted({int(row["pitcher_id"]) for row in parsed.pitches}),
        "required_fields": sorted(REQUIRED_FIELDS),
        "split_disposition": (
            "SPLIT_REQUIRED" if row_count >= STATCAST_ROW_CAP else "READY"
        ),
        "terminal_plate_appearance_count": len(parsed.plate_appearances),
        "unique_pitch_count": len(parsed.pitches),
    }
    return query_sidecar(raw, query, semantics)


def validate_statcast_child_coverage(
    parent: Mapping[str, object],
    children: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Require ordered, nonoverlapping children to cover the parent exactly."""
    if not isinstance(parent, Mapping) or isinstance(children, (str, bytes)):
        raise ValueError("Statcast parent and children must be mappings")
    first = _day(parent.get("inclusive_start"), "Statcast parent start")
    last = _day(parent.get("inclusive_end"), "Statcast parent end")
    if first > last or not isinstance(children, Sequence) or not children:
        raise ValueError("Statcast child coverage is empty or reversed")
    result: list[dict[str, object]] = []
    cursor = first
    for child in children:
        if not isinstance(child, Mapping):
            raise ValueError("Statcast child must be a mapping")
        child_first = _day(child.get("inclusive_start"), "Statcast child start")
        child_last = _day(child.get("inclusive_end"), "Statcast child end")
        if child_first != cursor or child_first > child_last or child_last > last:
            raise ValueError("Statcast children are unordered, overlapping, or gapped")
        result.append(dict(child))
        cursor = child_last + timedelta(days=1)
    if cursor != last + timedelta(days=1):
        raise ValueError("Statcast children do not cover the parent range")
    return result


def statcast_split_action(query: LogicalQuery, reason: str) -> str:
    """Classify transport timeout/size failures without claiming raw semantics."""
    require_query(
        query,
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint=_STATCAST_ENDPOINT,
        expected_media_type="text/csv",
    )
    if reason not in {"TIMEOUT", "SIZE_LIMIT"}:
        raise ValueError("Statcast split reason must be TIMEOUT or SIZE_LIMIT")
    first = _day(query.requested_range.get("start_date"), "Statcast query start")
    last = _day(query.requested_range.get("end_date"), "Statcast query end")
    if first >= last:
        raise ValueError(f"single-day Statcast {reason} cannot be split")
    return "SPLIT_REQUIRED"


__all__ = [
    "recompute_statcast_sidecar",
    "statcast_split_action",
    "validate_statcast_child_coverage",
]
