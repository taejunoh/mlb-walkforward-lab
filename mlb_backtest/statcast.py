"""Strict Baseball Savant Statcast CSV acquisition and normalization."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import tempfile
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


def is_statcast_row_capped_manifest(manifest: Mapping[str, object]) -> bool:
    """Return whether Savant stopped this raw CSV at its silent data-row cap."""
    row_count = manifest.get("row_count")
    return (
        isinstance(row_count, int)
        and not isinstance(row_count, bool)
        and row_count == STATCAST_ROW_CAP + 1
    )


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
    known_queries: dict[str, str] | None = None,
    verified_catalog: list[dict[str, object]] | None = None,
) -> list[object]:
    """Fetch and manifest inclusive monthly chunks, retrying transient failures."""
    if fetcher is None:
        fetcher = fetch_bytes
    if sleeper is None:
        sleeper = time.sleep
    if known_queries is None:
        known_queries = {}
    artifacts = []
    known_artifacts = {}
    known_capped_queries = set()
    if known_queries:
        from .models import RawArtifact

        catalog = store.verify_manifests() if verified_catalog is None else verified_catalog
        for verified in catalog:
            manifest = verified["manifest"]
            if manifest["source"] != "baseball_savant_statcast":
                continue
            key = json.dumps(manifest["query"], separators=(",", ":"), sort_keys=True)
            if known_queries.get(key) != manifest["sha256"]:
                continue
            if is_statcast_row_capped_manifest(manifest):
                known_capped_queries.add(key)
                continue
            known_artifacts[key] = RawArtifact(
                source=manifest["source"],
                sha256=manifest["sha256"],
                data_path=Path(verified["payload_path"]),
                manifest_path=Path(verified["manifest_path"]),
                retrieved_at_utc=manifest["retrieved_at_utc"],
                row_count=manifest["row_count"],
            )

    def reuse_range(chunk_start: str, chunk_end: str) -> bool:
        query = {
            "start": chunk_start,
            "end": chunk_end,
            "game_type": "R",
            "url": statcast_url(chunk_start, chunk_end),
        }
        exact_key = json.dumps(query, separators=(",", ":"), sort_keys=True)
        if exact_key in known_artifacts:
            artifacts.append(known_artifacts[exact_key])
            return True
        first, last = _iso(chunk_start, "chunk start"), _iso(chunk_end, "chunk end")
        expected = {
            first + timedelta(days=offset)
            for offset in range((last - first).days + 1)
        }
        covered = set()
        children = []
        for key, artifact in known_artifacts.items():
            child = json.loads(key)
            child_first = _iso(child["start"], "child start")
            child_last = _iso(child["end"], "child end")
            dates = {
                child_first + timedelta(days=offset)
                for offset in range((child_last - child_first).days + 1)
            }
            if dates < expected:
                if covered & dates:
                    raise RuntimeError("overlapping resumed Statcast child ranges")
                covered.update(dates)
                children.append((child_first, child_last, artifact))
        if covered == expected:
            artifacts.extend(item[2] for item in sorted(children))
            return True
        return False

    def fetch_range(chunk_start: str, chunk_end: str) -> None:
        if reuse_range(chunk_start, chunk_end):
            return
        url = statcast_url(chunk_start, chunk_end)
        query = {
            "start": chunk_start,
            "end": chunk_end,
            "game_type": "R",
            "url": url,
        }
        query_key = json.dumps(query, separators=(",", ":"), sort_keys=True)

        def split_range() -> None:
            first, last = _iso(chunk_start, "chunk start"), _iso(
                chunk_end, "chunk end"
            )
            if first >= last:
                raise ValueError("single-day Statcast response reached row limit")
            midpoint = first + timedelta(days=(last - first).days // 2)
            fetch_range(first.isoformat(), midpoint.isoformat())
            fetch_range((midpoint + timedelta(days=1)).isoformat(), last.isoformat())

        if query_key in known_capped_queries:
            split_range()
            return
        for attempt in range(len(retries) + 1):
            try:
                payload = fetcher(url, max_bytes=MAX_STATCAST_BYTES)
                break
            except ValueError as exc:
                message = str(exc).lower()
                if "exceed" not in message or not (
                    "size" in message or "byte" in message
                ):
                    raise
                if _iso(chunk_start, "chunk start") >= _iso(
                    chunk_end, "chunk end"
                ):
                    raise ValueError(
                        "single-day Statcast response exceeds size limit"
                    ) from exc
                split_range()
                return
            except HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code < 600
                if not transient or attempt == len(retries):
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == len(retries):
                    raise
            sleeper(retries[attempt])
        if sum(bool(line.strip()) for line in payload.splitlines()) - 1 >= STATCAST_ROW_CAP:
            split_range()
            return
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
        digest = hashlib.sha256(payload).hexdigest()
        known_digest = known_queries.get(query_key)
        if known_digest is not None and known_digest != digest:
            raise RuntimeError("ambiguous raw payload for canonical Statcast query")
        artifacts.append(
            store.put_unique_query(
                "baseball_savant_statcast",
                query,
                payload,
            )
        )
        known_queries[query_key] = digest

    for chunk_start, chunk_end in monthly_ranges(start, end):
        fetch_range(chunk_start, chunk_end)
    return artifacts


def fetch_statcast_stage(
    config_path: str | Path, data_dir: str | Path
) -> list[object]:
    """Acquire and manifest every frozen regular-season Statcast month."""
    from .pipeline import _safe_data_dir, _stage

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    store = RawStore(_stage(root, "raw"))
    known_queries = {}
    verified_catalog = store.verify_manifests()
    for verified in verified_catalog:
        manifest = verified["manifest"]
        if manifest["source"] == "baseball_savant_statcast":
            key = json.dumps(
                manifest["query"], separators=(",", ":"), sort_keys=True
            )
            known_queries[key] = manifest["sha256"]
    artifacts = []
    for start, end in regular_season_ranges(config["extension_end"]):
        artifacts.extend(
            fetch_monthly(
                store,
                start,
                end,
                known_queries=known_queries,
                verified_catalog=verified_catalog,
            )
        )
    return artifacts


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


def iter_unique_statcast_pitches_on_disk(
    pitches: Iterable[dict[str, object]],
) -> Iterable[dict[str, object]]:
    """Deduplicate pitches with a fixed-size SQLite cache instead of row RAM."""
    with tempfile.TemporaryDirectory(prefix="mlb-statcast-unique-") as temp_dir:
        database = Path(temp_dir) / "pitches.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-1024")
            connection.execute(
                """
                CREATE TABLE pitches (
                    game_pk INTEGER NOT NULL,
                    at_bat_number INTEGER NOT NULL,
                    pitch_number INTEGER NOT NULL,
                    fingerprint BLOB NOT NULL,
                    PRIMARY KEY (game_pk, at_bat_number, pitch_number)
                ) WITHOUT ROWID
                """
            )
            cursor = connection.cursor()
            for row in pitches:
                key = _statcast_pitch_identity(row)
                fingerprint = _statcast_pitch_fingerprint(row)
                cursor.execute(
                    "INSERT OR IGNORE INTO pitches VALUES (?, ?, ?, ?)",
                    (*key, fingerprint),
                )
                if cursor.rowcount:
                    yield row
                    continue
                prior = cursor.execute(
                    """
                    SELECT fingerprint FROM pitches
                    WHERE game_pk = ? AND at_bat_number = ? AND pitch_number = ?
                    """,
                    key,
                ).fetchone()
                if prior is None or bytes(prior[0]) != fingerprint:
                    raise _conflicting_duplicate_pitch(key)
        finally:
            connection.close()


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


def derive_outs_recorded(
    statcast: StatcastData, final_outs_by_game: Mapping[int, int]
) -> StatcastData:
    """Derive pitcher outs from official pre-pitch states and final linescores.

    Baseball Savant exposes ``outs_when_up`` (the pre-pitch state), not an
    outs-on-play field.  Consecutive pitch states therefore carry the exact
    state transition, including outs made between pitches or on the bases.
    The final pitch of each game is closed by MLB's hydrated linescore outs.
    """
    if not isinstance(statcast, StatcastData) or not isinstance(
        final_outs_by_game, Mapping
    ):
        raise ValueError("statcast and final_outs_by_game are required")
    by_game: dict[int, list[dict[str, object]]] = {}
    for raw in statcast.pitches:
        row = dict(raw)
        game_pk = int(row["game_pk"])
        by_game.setdefault(game_pk, []).append(row)

    derived: list[dict[str, object]] = []
    for game_pk, rows in sorted(by_game.items()):
        final_outs = final_outs_by_game.get(game_pk)
        if type(final_outs) is not int or not 0 <= final_outs <= 3:
            raise ValueError(
                f"game {game_pk}: final linescore outs must be an integer from 0 to 3"
            )
        rows.sort(
            key=lambda row: (
                int(row["inning"]),
                0 if row["inning_topbot"] == "Top" else 1,
                int(row["at_bat_number"]),
                int(row["pitch_number"]),
            )
        )
        for index, row in enumerate(rows):
            pre = row.get("outs_when_up")
            if type(pre) is not int or not 0 <= pre <= 2:
                raise ValueError(
                    f"game {game_pk}: outs_when_up must be an integer from 0 to 2"
                )
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            current_half = (int(row["inning"]), str(row["inning_topbot"]))
            if next_row is None:
                post = final_outs
            else:
                next_half = (
                    int(next_row["inning"]),
                    str(next_row["inning_topbot"]),
                )
                if next_half == current_half:
                    post = next_row.get("outs_when_up")
                    if type(post) is not int or not 0 <= post <= 2:
                        raise ValueError(
                            f"game {game_pk}: outs_when_up must be an integer from 0 to 2"
                        )
                else:
                    legal_next = (
                        (current_half[1] == "Top" and next_half == (current_half[0], "Bot"))
                        or (
                            current_half[1] == "Bot"
                            and next_half == (current_half[0] + 1, "Top")
                        )
                    )
                    if not legal_next:
                        raise ValueError(f"game {game_pk}: illegal half-inning transition")
                    post = 3
            outs = post - pre
            if not 0 <= outs <= 3:
                raise ValueError(f"game {game_pk}: outs state regressed or jumped")
            row["outs_recorded"] = outs
            derived.append(row)

    derived.sort(
        key=lambda row: (
            row["game_date"],
            row["game_pk"],
            row["at_bat_number"],
            row["pitch_number"],
            row["pitcher_id"],
            row["batter_id"],
        )
    )
    terminals = {
        str(row["event_id"]): dict(row)
        for row in derived
        if is_completed_pa_row(row)
    }
    plate_appearances = tuple(
        terminals[key]
        for key in sorted(terminals, key=lambda key: tuple(map(int, key.split(":"))))
    )
    return StatcastData(tuple(derived), plate_appearances)


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
    "NON_PA_EVENTS",
    "REQUIRED_FIELDS",
    "SAVANT_CSV",
    "StatcastData",
    "derive_outs_recorded",
    "fetch_monthly",
    "fetch_statcast_stage",
    "identify_pitcher_roles",
    "is_completed_pa_row",
    "is_statcast_row_capped_manifest",
    "iter_statcast_pitches",
    "iter_unique_statcast_pitches",
    "iter_unique_statcast_pitches_on_disk",
    "monthly_ranges",
    "parse_statcast",
    "regular_season_ranges",
    "statcast_url",
]
