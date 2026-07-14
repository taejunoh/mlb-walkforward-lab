"""Baseball Savant pitch-grain semantic recomputation."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from ..statcast import REQUIRED_FIELDS, STATCAST_ROW_CAP, parse_statcast
from ..v52_evidence import LogicalQuery, canonical_query_bytes
from ..v52_source_contracts import (
    QUERY_SIDECAR_KEYS,
    canonical_json_bytes,
    canonical_sha256,
    query_sidecar,
    require_query,
)


_STATCAST_ENDPOINT = "https://baseballsavant.mlb.com/statcast_search/csv"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
        "canonical_query": json.loads(canonical_query_bytes(query)),
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
        "plate_appearance_evidence": sorted(
            [
                {
                    "at_bat_number": row["at_bat_number"],
                    "batter_id": row["batter_id"],
                    "events": row["events"],
                    "game_date": row["game_date"],
                    "game_pk": row["game_pk"],
                    "pitch_number": row["pitch_number"],
                    "pitcher_id": row["pitcher_id"],
                }
                for row in parsed.plate_appearances
            ],
            key=lambda row: (row["game_pk"], row["at_bat_number"]),
        ),
        "pitcher_batters_faced": {
            str(pitcher_id): sum(
                row["pitcher_id"] == pitcher_id
                for row in parsed.plate_appearances
            )
            for pitcher_id in sorted(
                {int(row["pitcher_id"]) for row in parsed.plate_appearances}
            )
        },
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


def recompute_statcast_season_snapshot(
    parent_range: Mapping[str, object],
    child_sidecars: Sequence[Mapping[str, object]],
    played_game_pks: Sequence[int],
) -> Mapping[str, object]:
    if (
        not isinstance(parent_range, Mapping)
        or set(parent_range) != {"inclusive_start", "inclusive_end"}
        or not isinstance(child_sidecars, Sequence)
    ):
        raise ValueError("Statcast aggregate inputs are invalid")
    parent_first = _day(parent_range["inclusive_start"], "Statcast parent start")
    parent_last = _day(parent_range["inclusive_end"], "Statcast parent end")
    if (
        parent_first > parent_last
        or (parent_first.year, parent_first.month)
        != (parent_last.year, parent_last.month)
    ):
        raise ValueError("Statcast aggregate requires exact monthly parent coverage")
    played = list(played_game_pks)
    if played != sorted(set(played)) or any(type(value) is not int or value <= 0 for value in played):
        raise ValueError("played gamePks must be sorted unique positive integers")
    children = []
    statcast_games: set[int] = set()
    total_pitches = 0
    seen_child_hashes: set[str] = set()
    for sidecar in child_sidecars:
        if (
            not isinstance(sidecar, Mapping)
            or set(sidecar) != QUERY_SIDECAR_KEYS
            or sidecar.get("source") != "baseball_savant_statcast"
            or sidecar.get("grain") != "pitch"
            or sidecar.get("parser_version") != "v5.2.1"
            or sidecar.get("schema_version") != 1
            or type(sidecar.get("byte_count")) is not int
            or sidecar["byte_count"] < 0
            or not isinstance(sidecar.get("query_id"), str)
            or not _SHA256.fullmatch(sidecar["query_id"])
            or not isinstance(sidecar.get("raw_sha256"), str)
            or not _SHA256.fullmatch(sidecar["raw_sha256"])
            or not isinstance(sidecar.get("semantics"), Mapping)
        ):
            raise ValueError("Statcast child must be a Task2 pitch sidecar")
        semantics = sidecar["semantics"]
        coverage = semantics.get("coverage")
        if not isinstance(coverage, Mapping):
            raise ValueError("Statcast child coverage is missing")
        if semantics.get("split_disposition") != "READY":
            raise ValueError("Statcast child cap/split state is not terminal READY")
        game_pks = semantics.get("game_pks")
        pitch_count = semantics.get("unique_pitch_count")
        if (
            not isinstance(game_pks, list)
            or game_pks != sorted(set(game_pks))
            or any(type(value) is not int or value <= 0 for value in game_pks)
            or type(pitch_count) is not int
            or pitch_count < 0
        ):
            raise ValueError("Statcast child population is invalid")
        if statcast_games.intersection(game_pks):
            raise ValueError("Statcast game population appears in multiple children")
        statcast_games.update(game_pks)
        total_pitches += pitch_count
        sidecar_hash = hashlib.sha256(
            canonical_json_bytes(dict(sidecar)) + b"\n"
        ).hexdigest()
        if sidecar_hash in seen_child_hashes:
            raise ValueError("Statcast aggregate contains duplicate child sidecar")
        seen_child_hashes.add(sidecar_hash)
        children.append(
            {
                "inclusive_start": coverage.get("inclusive_start"),
                "inclusive_end": coverage.get("inclusive_end"),
                "query_sidecar_sha256": sidecar_hash,
                "split_disposition": "READY",
            }
        )
    validate_statcast_child_coverage(parent_range, children)
    if sorted(statcast_games) != played:
        raise ValueError("Statcast game population differs from played schedule population")
    return {
        "schema_version": 1,
        "grain": "pitch",
        "parent_coverage": dict(parent_range),
        "children": children,
        "child_query_sidecar_sha256s": [row["query_sidecar_sha256"] for row in children],
        "played_game_pks": played,
        "statcast_game_pks": sorted(statcast_games),
        "unique_pitch_count": total_pitches,
        "split_state": "READY_COMPLETE",
    }


__all__ = [
    "recompute_statcast_sidecar",
    "recompute_statcast_season_snapshot",
    "statcast_split_action",
    "validate_statcast_child_coverage",
]
