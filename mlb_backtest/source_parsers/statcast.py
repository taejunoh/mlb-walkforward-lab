"""Pure, offline Baseball Savant Statcast parsing."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping


STATCAST_ROW_CAP = 25_000
REQUIRED_FIELDS = (
    "pitch_type",
    "game_date",
    "game_pk",
    "pitcher",
    "batter",
    "events",
    "description",
    "p_throws",
    "home_team",
    "away_team",
    "inning",
    "inning_topbot",
    "release_speed",
    "woba_value",
    "woba_denom",
    "estimated_woba_using_speedangle",
    "launch_speed_angle",
    "at_bat_number",
    "pitch_number",
    "home_score",
    "away_score",
    "post_home_score",
    "post_away_score",
    "outs_when_up",
    "game_type",
)
NON_PA_EVENTS = frozenset({"truncated_pa"})


def is_completed_pa_row(row: Mapping[str, object]) -> bool:
    """Return whether a Statcast pitch row proves a completed plate appearance."""
    events = row.get("events")
    if events:
        return events not in NON_PA_EVENTS
    return row.get("description") == "hit_by_pitch"


@dataclass(frozen=True)
class StatcastData:
    pitches: tuple[dict[str, object], ...]
    plate_appearances: tuple[dict[str, object], ...]


def _iso(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _integer(
    value: str | None,
    field: str,
    row_number: int,
    *,
    positive: bool = False,
    maximum: int | None = None,
) -> int:
    try:
        result = int(value or "")
    except ValueError as exc:
        raise ValueError(f"statcast row {row_number}: {field} must be an integer") from exc
    if positive and result <= 0:
        raise ValueError(f"statcast row {row_number}: {field} must be positive")
    if not positive and result < 0:
        raise ValueError(f"statcast row {row_number}: {field} must be nonnegative")
    if maximum is not None and result > maximum:
        raise ValueError(f"statcast row {row_number}: {field} must be at most {maximum}")
    return result


def _number(value: str | None, field: str, row_number: int) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"statcast row {row_number}: {field} must be numeric") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"statcast row {row_number}: {field} must be finite")
    return result


def iter_statcast_pitches(
    source: str | Path | bytes,
) -> Iterable[dict[str, object]]:
    """Validate and stream normalized pitches without retaining the CSV as text."""
    raw_stream = io.BytesIO(source) if isinstance(source, bytes) else Path(source).open("rb")
    try:
        with raw_stream, io.TextIOWrapper(
            raw_stream, encoding="utf-8-sig", newline=""
        ) as stream:
            reader = csv.DictReader(stream)
            fields = set(reader.fieldnames or ())
            missing = sorted(set(REQUIRED_FIELDS) - fields)
            if missing:
                raise ValueError(
                    f"statcast CSV missing required fields: {', '.join(missing)}"
                )
            for row_number, raw in enumerate(reader, 2):
                game_date = raw["game_date"]
                _iso(game_date, f"statcast row {row_number} game_date")
                event = raw["events"]
                if not event and raw["description"] == "hit_by_pitch":
                    event = "hit_by_pitch"
                half = raw["inning_topbot"]
                if half not in {"Top", "Bot"}:
                    raise ValueError(
                        f"statcast row {row_number}: inning_topbot must be Top or Bot"
                    )
                hand = raw["p_throws"]
                if hand not in {"L", "R"}:
                    raise ValueError(
                        f"statcast row {row_number}: p_throws must be L or R"
                    )
                if raw["game_type"] != "R":
                    raise ValueError(
                        f"statcast row {row_number}: game_type must be R"
                    )
                if not raw["home_team"] or not raw["away_team"]:
                    raise ValueError(
                        f"statcast row {row_number}: team abbreviations are required"
                    )
                row = {
                    "pitch_type": raw["pitch_type"],
                    "game_date": game_date,
                    "game_pk": _integer(
                        raw["game_pk"], "game_pk", row_number, positive=True
                    ),
                    "pitcher_id": _integer(
                        raw["pitcher"], "pitcher", row_number, positive=True
                    ),
                    "batter_id": _integer(
                        raw["batter"], "batter", row_number, positive=True
                    ),
                    "events": event,
                    "description": raw["description"],
                    "p_throws": hand,
                    "home_team": raw["home_team"],
                    "away_team": raw["away_team"],
                    "inning": _integer(
                        raw["inning"], "inning", row_number, positive=True
                    ),
                    "inning_topbot": half,
                    "release_speed": _number(
                        raw["release_speed"], "release_speed", row_number
                    ),
                    "woba_value": _number(
                        raw["woba_value"], "woba_value", row_number
                    ),
                    "woba_denom": _number(
                        raw["woba_denom"], "woba_denom", row_number
                    ),
                    "estimated_woba_using_speedangle": _number(
                        raw["estimated_woba_using_speedangle"],
                        "estimated_woba_using_speedangle",
                        row_number,
                    ),
                    "launch_speed_angle": _number(
                        raw["launch_speed_angle"],
                        "launch_speed_angle",
                        row_number,
                    ),
                    "at_bat_number": _integer(
                        raw["at_bat_number"],
                        "at_bat_number",
                        row_number,
                        positive=True,
                    ),
                    "pitch_number": _integer(
                        raw["pitch_number"],
                        "pitch_number",
                        row_number,
                        positive=True,
                    ),
                    "home_score": _integer(
                        raw["home_score"], "home_score", row_number
                    ),
                    "away_score": _integer(
                        raw["away_score"], "away_score", row_number
                    ),
                    "post_home_score": _integer(
                        raw["post_home_score"], "post_home_score", row_number
                    ),
                    "post_away_score": _integer(
                        raw["post_away_score"], "post_away_score", row_number
                    ),
                    "outs_when_up": _integer(
                        raw["outs_when_up"],
                        "outs_when_up",
                        row_number,
                        maximum=2,
                    ),
                    "game_type": "R",
                }
                row["event_id"] = f'{row["game_pk"]}:{row["at_bat_number"]}'
                yield row
    except UnicodeDecodeError as exc:
        raise ValueError("statcast CSV must be UTF-8-sig") from exc


def _statcast_pitch_identity(row: Mapping[str, object]) -> tuple[int, int, int]:
    return (
        int(row["game_pk"]),
        int(row["at_bat_number"]),
        int(row["pitch_number"]),
    )


def _statcast_pitch_fingerprint(row: Mapping[str, object]) -> bytes:
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _conflicting_duplicate_pitch(key: tuple[int, int, int]) -> ValueError:
    return ValueError(f"conflicting duplicate pitch {key[0]}:{key[1]}:{key[2]}")


def iter_unique_statcast_pitches(
    pitches: Iterable[dict[str, object]],
) -> Iterable[dict[str, object]]:
    """Yield one row per canonical pitch identity and reject conflicts."""
    seen: dict[tuple[int, int, int], bytes] = {}
    for row in pitches:
        key = _statcast_pitch_identity(row)
        fingerprint = _statcast_pitch_fingerprint(row)
        prior = seen.get(key)
        if prior is not None:
            if prior != fingerprint:
                raise _conflicting_duplicate_pitch(key)
            continue
        seen[key] = fingerprint
        yield row


def parse_statcast(source: str | Path | bytes) -> StatcastData:
    """Parse compact pitch rows and one terminal event row per plate appearance."""
    pitches = list(iter_unique_statcast_pitches(iter_statcast_pitches(source)))
    pitches.sort(
        key=lambda r: (
            r["game_date"],
            r["game_pk"],
            r["at_bat_number"],
            r["pitch_number"],
            r["pitcher_id"],
            r["batter_id"],
        )
    )
    terminal: dict[str, dict[str, object]] = {}
    for row in pitches:
        if is_completed_pa_row(row):
            event_id = str(row["event_id"])
            prior = terminal.get(event_id)
            if prior is not None and prior != row:
                raise ValueError(f"multiple terminal events for plate appearance {event_id}")
            terminal[event_id] = dict(row)
    plate_appearances = tuple(
        terminal[key]
        for key in sorted(terminal, key=lambda key: tuple(map(int, key.split(":"))))
    )
    return StatcastData(tuple(pitches), plate_appearances)
