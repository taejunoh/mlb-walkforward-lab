"""Strict Baseball Savant Statcast CSV acquisition and normalization."""

from __future__ import annotations

import csv
import hashlib
import io
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .config import load_config
from .manifest import RawStore
from .sources import fetch_bytes


SAVANT_CSV = "https://baseballsavant.mlb.com/statcast_search/csv"
MAX_STATCAST_BYTES = 256 * 1024 * 1024
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
    "post_home_score",
    "post_away_score",
    "game_type",
)


@dataclass(frozen=True)
class StatcastData:
    pitches: tuple[dict[str, object], ...]
    plate_appearances: tuple[dict[str, object], ...]


def _iso(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def statcast_url(start: str, end: str) -> str:
    """Build the official detail CSV query for an inclusive regular-season range."""
    first, last = _iso(start, "start"), _iso(end, "end")
    if first > last:
        raise ValueError("start must not be after end")
    query = urlencode(
        {
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
    )
    return f"{SAVANT_CSV}?{query}"


def monthly_ranges(start: str, end: str) -> list[tuple[str, str]]:
    first, last = _iso(start, "start"), _iso(end, "end")
    if first > last:
        raise ValueError("start must not be after end")
    result: list[tuple[str, str]] = []
    cursor = first
    while cursor <= last:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        chunk_end = min(last, next_month - timedelta(days=1))
        result.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = next_month
    return result


def regular_season_ranges(extension_end: str) -> list[tuple[str, str]]:
    """Return the frozen regular-season acquisition windows."""
    end = _iso(extension_end, "extension_end")
    opening_days = {
        2022: date(2022, 4, 7),
        2023: date(2023, 3, 30),
        2024: date(2024, 3, 20),
        2025: date(2025, 3, 18),
    }
    closing_days = {
        2022: date(2022, 10, 5),
        2023: date(2023, 10, 1),
        2024: date(2024, 9, 30),
        2025: end,
    }
    if not opening_days[2025] <= end <= date(2025, 9, 28):
        raise ValueError("extension_end must be within the frozen 2025 regular season")
    return [
        (opening_days[year].isoformat(), closing_days[year].isoformat())
        for year in range(2022, 2026)
    ]


def fetch_monthly(
    store: RawStore,
    start: str,
    end: str,
    *,
    retries: tuple[float, ...] = (1, 2, 4, 8, 16),
    fetcher=None,
    sleeper=None,
) -> list[object]:
    """Fetch and manifest inclusive monthly chunks, retrying transient failures."""
    if fetcher is None:
        fetcher = fetch_bytes
    if sleeper is None:
        sleeper = time.sleep
    artifacts = []
    for chunk_start, chunk_end in monthly_ranges(start, end):
        url = statcast_url(chunk_start, chunk_end)
        for attempt in range(len(retries) + 1):
            try:
                payload = fetcher(url, max_bytes=MAX_STATCAST_BYTES)
                break
            except HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code < 600
                if not transient or attempt == len(retries):
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == len(retries):
                    raise
            sleeper(retries[attempt])
        parsed = parse_statcast(payload)
        if not parsed.pitches:
            raise ValueError("Statcast chunk must contain at least one data row")
        first = _iso(chunk_start, "chunk start")
        last = _iso(chunk_end, "chunk end")
        if any(
            not first <= _iso(str(row["game_date"]), "game_date") <= last
            for row in parsed.pitches
        ):
            raise ValueError("Statcast row game_date is outside inclusive query range")
        query = {
            "start": chunk_start,
            "end": chunk_end,
            "game_type": "R",
            "url": url,
        }
        digest = hashlib.sha256(payload).hexdigest()
        for existing in store.verify_manifests():
            manifest = existing["manifest"]
            if (
                manifest["source"] == "baseball_savant_statcast"
                and manifest["query"] == query
            ):
                if manifest["sha256"] != digest:
                    raise RuntimeError(
                        "ambiguous raw payload for canonical Statcast query"
                    )
                break
        artifacts.append(
            store.put(
                "baseball_savant_statcast",
                query,
                payload,
            )
        )
    return artifacts


def fetch_statcast_stage(
    config_path: str | Path, data_dir: str | Path
) -> list[object]:
    """Acquire and manifest every frozen regular-season Statcast month."""
    from .pipeline import _safe_data_dir, _stage

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    store = RawStore(_stage(root, "raw"))
    artifacts = []
    for start, end in regular_season_ranges(config["extension_end"]):
        artifacts.extend(fetch_monthly(store, start, end))
    return artifacts


def _integer(
    value: str | None, field: str, row_number: int, *, positive: bool = False
) -> int:
    try:
        result = int(value or "")
    except ValueError as exc:
        raise ValueError(f"statcast row {row_number}: {field} must be an integer") from exc
    if positive and result <= 0:
        raise ValueError(f"statcast row {row_number}: {field} must be positive")
    if not positive and result < 0:
        raise ValueError(f"statcast row {row_number}: {field} must be nonnegative")
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


def _read_text(source: str | Path | bytes) -> str:
    if isinstance(source, bytes):
        raw = source
    else:
        raw = Path(source).read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("statcast CSV must be UTF-8-sig") from exc


def parse_statcast(source: str | Path | bytes) -> StatcastData:
    """Parse compact pitch rows and one terminal event row per plate appearance."""
    reader = csv.DictReader(io.StringIO(_read_text(source), newline=""))
    fields = set(reader.fieldnames or ())
    missing = sorted(set(REQUIRED_FIELDS) - fields)
    if missing:
        raise ValueError(f"statcast CSV missing required fields: {', '.join(missing)}")
    pitches: list[dict[str, object]] = []
    for row_number, raw in enumerate(reader, 2):
        game_date = raw["game_date"]
        _iso(game_date, f"statcast row {row_number} game_date")
        half = raw["inning_topbot"]
        if half not in {"Top", "Bot"}:
            raise ValueError(f"statcast row {row_number}: inning_topbot must be Top or Bot")
        hand = raw["p_throws"]
        if hand not in {"L", "R"}:
            raise ValueError(f"statcast row {row_number}: p_throws must be L or R")
        if raw["game_type"] != "R":
            raise ValueError(f"statcast row {row_number}: game_type must be R")
        if not raw["home_team"] or not raw["away_team"]:
            raise ValueError(f"statcast row {row_number}: team abbreviations are required")
        row = {
            "pitch_type": raw["pitch_type"],
            "game_date": game_date,
            "game_pk": _integer(raw["game_pk"], "game_pk", row_number, positive=True),
            "pitcher_id": _integer(raw["pitcher"], "pitcher", row_number, positive=True),
            "batter_id": _integer(raw["batter"], "batter", row_number, positive=True),
            "events": raw["events"],
            "description": raw["description"],
            "p_throws": hand,
            "home_team": raw["home_team"],
            "away_team": raw["away_team"],
            "inning": _integer(raw["inning"], "inning", row_number, positive=True),
            "inning_topbot": half,
            "release_speed": _number(raw["release_speed"], "release_speed", row_number),
            "woba_value": _number(raw["woba_value"], "woba_value", row_number),
            "woba_denom": _number(raw["woba_denom"], "woba_denom", row_number),
            "estimated_woba_using_speedangle": _number(
                raw["estimated_woba_using_speedangle"],
                "estimated_woba_using_speedangle",
                row_number,
            ),
            "launch_speed_angle": _number(
                raw["launch_speed_angle"], "launch_speed_angle", row_number
            ),
            "at_bat_number": _integer(
                raw["at_bat_number"], "at_bat_number", row_number, positive=True
            ),
            "pitch_number": _integer(
                raw["pitch_number"], "pitch_number", row_number, positive=True
            ),
            "post_home_score": _integer(
                raw["post_home_score"], "post_home_score", row_number
            ),
            "post_away_score": _integer(
                raw["post_away_score"], "post_away_score", row_number
            ),
            "game_type": "R",
        }
        row["event_id"] = f'{row["game_pk"]}:{row["at_bat_number"]}'
        pitches.append(row)
    unique_pitches: dict[tuple[int, int, int], dict[str, object]] = {}
    for row in pitches:
        key = (
            int(row["game_pk"]),
            int(row["at_bat_number"]),
            int(row["pitch_number"]),
        )
        prior = unique_pitches.get(key)
        if prior is not None and prior != row:
            raise ValueError(
                f"conflicting duplicate pitch {key[0]}:{key[1]}:{key[2]}"
            )
        unique_pitches[key] = row
    pitches = list(unique_pitches.values())
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
        if row["events"]:
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


def identify_pitcher_roles(
    pitches: Iterable[Mapping[str, object]],
) -> dict[tuple[int, int], dict[str, object]]:
    """Identify actual starters as each fielding side's first pitcher in a game."""
    ordered = sorted(
        pitches,
        key=lambda r: (
            r["game_date"],
            r["game_pk"],
            r["inning"],
            0 if r["inning_topbot"] == "Top" else 1,
            r["at_bat_number"],
            r["pitch_number"],
        ),
    )
    starters: dict[tuple[int, str], int] = {}
    roles: dict[tuple[int, int], dict[str, object]] = {}
    for row in ordered:
        game_pk = int(row["game_pk"])
        pitcher_id = int(row["pitcher_id"])
        team = row["home_team"] if row["inning_topbot"] == "Top" else row["away_team"]
        starter_id = starters.setdefault((game_pk, str(team)), pitcher_id)
        is_starter = pitcher_id == starter_id
        roles[(game_pk, pitcher_id)] = {
            "team": team,
            "is_starter": is_starter,
            "is_reliever": not is_starter,
        }
    return roles


__all__ = [
    "MAX_STATCAST_BYTES",
    "REQUIRED_FIELDS",
    "SAVANT_CSV",
    "StatcastData",
    "fetch_monthly",
    "fetch_statcast_stage",
    "identify_pitcher_roles",
    "monthly_ranges",
    "parse_statcast",
    "regular_season_ranges",
    "statcast_url",
]
