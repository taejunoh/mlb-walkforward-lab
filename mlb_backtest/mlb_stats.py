"""Strict cumulative MLB Stats pitching acquisition for league ERA."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .manifest import RawStore, _safe_leaf_bytes
from .sources import MLB_BASE, fetch_bytes


_TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})


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


def league_era_url(season_open: str, as_of: str) -> str:
    """Return the canonical cumulative MLB Stats pitching URL."""
    first = _day(season_open, "season_open")
    last = _day(as_of, "as_of")
    if last < first:
        raise ValueError("as_of must not be before season_open")
    query = urlencode(
        {
            "stats": "byDateRange",
            "group": "pitching",
            "sportIds": 1,
            "gameType": "R",
            "playerPool": "ALL",
            "limit": 5000,
            "startDate": season_open,
            "endDate": as_of,
        }
    )
    return f"{MLB_BASE}/stats?{query}"


def _payload_bytes(source: str | Path | bytes) -> bytes:
    return source if isinstance(source, bytes) else Path(source).read_bytes()


def parse_league_era(source: str | Path | bytes) -> dict[str, int | float]:
    """Validate one complete pitching group and derive ERA from integer outs."""
    try:
        payload = json.loads(_payload_bytes(source).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("MLB pitching stats must be UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("MLB pitching stats root must be an object")
    stats = payload.get("stats")
    if not isinstance(stats, list) or len(stats) != 1:
        raise ValueError("MLB pitching stats must contain exactly one stats group")
    group = stats[0]
    if not isinstance(group, dict):
        raise ValueError("MLB pitching stats group must be an object")
    group_meta = group.get("group")
    if (
        not isinstance(group_meta, dict)
        or group_meta.get("displayName") != "pitching"
    ):
        raise ValueError("MLB pitching stats group must be pitching")
    total_splits = group.get("totalSplits")
    splits = group.get("splits")
    if type(total_splits) is not int or total_splits < 0:
        raise ValueError("MLB pitching totalSplits must be a nonnegative integer")
    if not isinstance(splits, list) or total_splits != len(splits):
        raise ValueError("MLB pitching stats are truncated or paged")
    earned_runs = 0
    outs = 0
    for index, split in enumerate(splits):
        if not isinstance(split, dict) or not isinstance(split.get("stat"), dict):
            raise ValueError(f"MLB pitching split {index} must contain a stat object")
        stat = split["stat"]
        er = stat.get("earnedRuns")
        split_outs = stat.get("outs")
        if type(er) is not int or er < 0:
            raise ValueError("earnedRuns must be a nonnegative integer")
        if type(split_outs) is not int or split_outs < 0:
            raise ValueError("outs must be a nonnegative integer")
        earned_runs += er
        outs += split_outs
    if outs <= 0:
        raise ValueError("MLB pitching stats must contain positive total outs")
    return {
        "earned_runs": earned_runs,
        "outs": outs,
        "value": 27.0 * earned_runs / outs,
    }


def _query(season_open: str, as_of: str) -> dict[str, object]:
    return {
        "stats": "byDateRange",
        "group": "pitching",
        "sportIds": 1,
        "gameType": "R",
        "playerPool": "ALL",
        "limit": 5000,
        "startDate": season_open,
        "endDate": as_of,
        "url": league_era_url(season_open, as_of),
    }


def _record(parsed: Mapping[str, int | float], as_of: str, digest: str) -> dict[str, object]:
    return {
        "as_of_date": as_of,
        "value": parsed["value"],
        "earned_runs": parsed["earned_runs"],
        "outs": parsed["outs"],
        "source": "mlb_stats_api",
        "manifest_sha256": digest,
    }


def _safe_artifact_bytes(path: Path, label: str) -> bytes:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"{label} parent must be a real directory")
    return _safe_leaf_bytes(path, parent.resolve(strict=True), label)


def fetch_league_era_records(
    store: RawStore,
    season_open_by_year: Mapping[int, str],
    as_of_dates: Sequence[str],
    *,
    fetcher=None,
    retries: tuple[float, ...] = (1, 2, 4, 8, 16),
    sleeper=None,
    verified_catalog=None,
) -> dict[str, dict[str, object]]:
    """Fetch one validated cumulative query per unique D-1 as-of date."""
    if not isinstance(store, RawStore):
        raise ValueError("store must be a RawStore")
    if not isinstance(season_open_by_year, Mapping):
        raise ValueError("season_open_by_year must be a mapping")
    if isinstance(as_of_dates, (str, bytes)) or not isinstance(as_of_dates, Sequence):
        raise ValueError("as_of_dates must be a sequence of ISO dates")
    if any(
        isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(delay)
        or delay < 0
        for delay in retries
    ):
        raise ValueError("retries must contain nonnegative delays")
    fetcher = fetch_bytes if fetcher is None else fetcher
    sleeper = time.sleep if sleeper is None else sleeper

    requested = sorted({_day(value, "as_of").isoformat() for value in as_of_dates})
    queries: dict[str, dict[str, object]] = {}
    for as_of in requested:
        parsed_as_of = date.fromisoformat(as_of)
        season_open = season_open_by_year.get(parsed_as_of.year)
        if season_open is None:
            raise ValueError(f"missing season open for {parsed_as_of.year}")
        open_day = _day(season_open, "season_open")
        if open_day.year != parsed_as_of.year or parsed_as_of < open_day:
            raise ValueError("as_of must not be before its season open")
        queries[as_of] = _query(open_day.isoformat(), as_of)

    existing = {}
    catalog = (
        store.verify_manifests() if verified_catalog is None else verified_catalog
    )
    for verified in catalog:
        manifest = verified["manifest"]
        if manifest["source"] == "mlb_stats_pitching":
            existing[json.dumps(manifest["query"], sort_keys=True)] = verified

    records = {}
    for as_of in requested:
        query = queries[as_of]
        known = existing.get(json.dumps(query, sort_keys=True))
        if known is not None:
            payload = _safe_artifact_bytes(Path(known["payload_path"]), "raw")
            if hashlib.sha256(payload).hexdigest() != known["manifest"]["sha256"]:
                raise RuntimeError("immutable raw payload digest mismatch")
            parsed = parse_league_era(payload)
            records[as_of] = _record(parsed, as_of, known["manifest_sha256"])
            continue

        url = str(query["url"])
        for attempt in range(len(retries) + 1):
            try:
                payload = fetcher(url)
                break
            except HTTPError as exc:
                if exc.code not in _TRANSIENT_HTTP or attempt == len(retries):
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == len(retries):
                    raise
            sleeper(retries[attempt])
        if not isinstance(payload, bytes):
            raise ValueError("MLB pitching fetcher must return bytes")
        parsed = parse_league_era(payload)
        artifact = store.put_unique_query("mlb_stats_pitching", query, payload)
        manifest_content = _safe_artifact_bytes(artifact.manifest_path, "manifest")
        manifest_digest = hashlib.sha256(manifest_content).hexdigest()
        records[as_of] = _record(parsed, as_of, manifest_digest)
    return records


__all__ = ["fetch_league_era_records", "league_era_url", "parse_league_era"]
