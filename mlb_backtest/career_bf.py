"""Immutable MLB Stats API prior-to-2022 career batters-faced evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .manifest import RawStore
from .sources import MLB_BASE, fetch_bytes


CAREER_START = "1876-01-01"
CAREER_CUTOFF = "2021-12-31"


def _pitcher_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("pitcher_id must be a positive integer")
    return value


def prior_career_bf_url(pitcher_id: int) -> str:
    pitcher = _pitcher_id(pitcher_id)
    query = urlencode(
        {
            "stats": "byDateRange",
            "group": "pitching",
            "gameType": "R",
            "sportIds": 1,
            "startDate": CAREER_START,
            "endDate": CAREER_CUTOFF,
        }
    )
    return f"{MLB_BASE}/people/{pitcher}/stats?{query}"


def _query(pitcher_id: int) -> dict[str, object]:
    pitcher = _pitcher_id(pitcher_id)
    return {
        "pitcher_id": pitcher,
        "stats": "byDateRange",
        "group": "pitching",
        "gameType": "R",
        "sportIds": 1,
        "startDate": CAREER_START,
        "endDate": CAREER_CUTOFF,
        "url": prior_career_bf_url(pitcher),
    }


def parse_prior_career_bf(source: str | Path | bytes, pitcher_id: int) -> int:
    """Return the unique all-sport aggregate, avoiding duplicated team splits."""
    _pitcher_id(pitcher_id)
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("career BF payload must be UTF-8 JSON") from exc
    if not isinstance(payload, dict) or "stats" not in payload:
        raise ValueError("career BF payload root is invalid")
    stats = payload["stats"]
    if stats == []:
        return 0
    if not isinstance(stats, list) or len(stats) != 1 or not isinstance(stats[0], dict):
        raise ValueError("career BF payload must contain one stats group")
    group = stats[0]
    if (
        not isinstance(group.get("type"), dict)
        or group["type"].get("displayName") != "byDateRange"
        or not isinstance(group.get("group"), dict)
        or group["group"].get("displayName") != "pitching"
        or not isinstance(group.get("splits"), list)
    ):
        raise ValueError("career BF stats group is invalid")
    if group["splits"] == []:
        return 0
    team_bf = []
    team_ids = []
    aggregates = []
    for split in group["splits"]:
        if not isinstance(split, dict):
            raise ValueError("career BF split is invalid")
        sport = split.get("sport")
        if not isinstance(sport, dict) or type(sport.get("id")) is not int:
            raise ValueError("career BF split sport is invalid")
        stat = split.get("stat")
        bf = stat.get("battersFaced") if isinstance(stat, dict) else None
        if type(bf) is not int or bf < 0:
            raise ValueError("career BF split battersFaced is invalid")
        if sport["id"] == 1:
            team = split.get("team")
            team_id = team.get("id") if isinstance(team, dict) else None
            if type(team_id) is not int or team_id <= 0:
                raise ValueError("career BF MLB team split is invalid")
            team_bf.append(bf)
            team_ids.append(team_id)
        elif sport["id"] == 0:
            aggregate_team = split.get("team")
            if aggregate_team is None:
                aggregate_team_id = None
            elif (
                not isinstance(aggregate_team, dict)
                or type(aggregate_team.get("id")) is not int
                or aggregate_team["id"] <= 0
            ):
                raise ValueError("career BF aggregate split is invalid")
            else:
                aggregate_team_id = aggregate_team["id"]
            aggregates.append((bf, aggregate_team_id))
        else:
            raise ValueError("career BF split contains a non-MLB sport")
    if len(aggregates) != 1 or not team_bf:
        raise ValueError("career BF requires exactly one aggregate split")
    total = sum(team_bf)
    aggregate_bf, aggregate_team_id = aggregates[0]
    if aggregate_bf != total:
        raise ValueError("career BF aggregate does not match MLB team splits")
    if aggregate_team_id is not None and (
        len(set(team_ids)) != 1 or aggregate_team_id != team_ids[0]
    ):
        raise ValueError("career BF aggregate team is inconsistent")
    return total


def _record(pitcher_id: int, prior_bf: int, manifest_sha256: str) -> dict[str, object]:
    return {
        "pitcher_id": pitcher_id,
        "prior_bf": prior_bf,
        "cutoff_date": CAREER_CUTOFF,
        "source": "mlb_stats_api",
        "manifest_sha256": manifest_sha256,
    }


def fetch_prior_career_bf_records(
    store: RawStore,
    pitcher_ids: Iterable[int],
    *,
    fetcher=None,
    sleeper=None,
    retries: tuple[float, ...] = (1, 2, 4, 8, 16),
    verified_catalog=None,
) -> dict[int, dict[str, object]]:
    """Fetch or resume one frozen by-date-range query per pitcher."""
    if not isinstance(store, RawStore):
        raise ValueError("store must be a RawStore")
    pitchers = sorted({_pitcher_id(value) for value in pitcher_ids})
    if any(
        isinstance(delay, bool)
        or not isinstance(delay, (int, float))
        or not math.isfinite(delay)
        or delay < 0
        for delay in retries
    ):
        raise ValueError("retries must contain nonnegative finite delays")
    fetcher = fetch_bytes if fetcher is None else fetcher
    sleeper = time.sleep if sleeper is None else sleeper
    existing = {}
    catalog = (
        store.verify_manifests() if verified_catalog is None else verified_catalog
    )
    for verified in catalog:
        manifest = verified["manifest"]
        if manifest.get("source") == "mlb_stats_pitcher_bf":
            existing[json.dumps(manifest["query"], sort_keys=True)] = verified
    records = {}
    for pitcher in pitchers:
        query = _query(pitcher)
        known = existing.get(json.dumps(query, sort_keys=True))
        if known is not None:
            prior_bf = parse_prior_career_bf(known["payload_path"], pitcher)
            records[pitcher] = _record(
                pitcher, prior_bf, str(known["manifest_sha256"])
            )
            continue
        for attempt in range(len(retries) + 1):
            try:
                payload = fetcher(str(query["url"]))
                break
            except HTTPError as exc:
                transient = exc.code in {429, 500, 502, 503, 504}
                if not transient or attempt == len(retries):
                    raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt == len(retries):
                    raise
            sleeper(retries[attempt])
        if not isinstance(payload, bytes):
            raise ValueError("career BF fetcher must return bytes")
        prior_bf = parse_prior_career_bf(payload, pitcher)
        artifact = store.put_unique_query("mlb_stats_pitcher_bf", query, payload)
        manifest_sha = hashlib.sha256(artifact.manifest_path.read_bytes()).hexdigest()
        records[pitcher] = _record(pitcher, prior_bf, manifest_sha)
    return records


def load_prior_career_bf_records(
    raw_root: str | Path,
    pitcher_ids: Iterable[int],
    *,
    catalog=None,
) -> tuple[dict[int, dict[str, object]], list[str]]:
    """Load exact canonical prior-career records with no network fallback."""
    pitchers = sorted({_pitcher_id(value) for value in pitcher_ids})
    verified_rows = (
        RawStore(Path(raw_root)).verify_manifests() if catalog is None else catalog
    )
    by_query = {}
    for verified in verified_rows:
        manifest = verified["manifest"]
        if manifest.get("source") != "mlb_stats_pitcher_bf":
            continue
        query = manifest.get("query")
        pitcher = query.get("pitcher_id") if isinstance(query, dict) else None
        if type(pitcher) is not int or query != _query(pitcher):
            raise ValueError("career BF manifest query is not canonical")
        if pitcher in by_query:
            raise ValueError("career BF manifest query is ambiguous")
        by_query[pitcher] = verified
    records = {}
    for pitcher in pitchers:
        verified = by_query.get(pitcher)
        if verified is None:
            raise RuntimeError(f"missing prior career BF manifest for pitcher {pitcher}")
        prior_bf = parse_prior_career_bf(verified["payload_path"], pitcher)
        manifest_sha = str(verified["manifest_sha256"])
        if re.fullmatch(r"[0-9a-f]{64}", manifest_sha) is None:
            raise ValueError("career BF manifest SHA is invalid")
        records[pitcher] = _record(pitcher, prior_bf, manifest_sha)
    hashes = sorted(record["manifest_sha256"] for record in records.values())
    return records, hashes


def apply_prior_career_bf(
    raw_features: Iterable[dict[str, object]],
    records: Mapping[int, Mapping[str, object]],
) -> None:
    """Add only the frozen through-2021 portion to D-1 2022+ Statcast BF."""
    for raw in raw_features:
        if raw.get("eligible") is not True:
            continue
        for side in ("away", "home"):
            starter = raw[side]["starter"]
            pitcher = _pitcher_id(starter.get("pitcher_id"))
            record = records.get(pitcher)
            if (
                not isinstance(record, Mapping)
                or record.get("pitcher_id") != pitcher
                or record.get("cutoff_date") != CAREER_CUTOFF
                or record.get("source") != "mlb_stats_api"
                or type(record.get("prior_bf")) is not int
                or record["prior_bf"] < 0
                or not isinstance(record.get("manifest_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", record["manifest_sha256"]) is None
            ):
                raise ValueError("prior career BF record is invalid or missing")
            current = starter.get("career_bf")
            if type(current) is not int or current < 0:
                raise ValueError("D-1 Statcast career_bf must be nonnegative")
            starter["career_bf"] = current + record["prior_bf"]


__all__ = [
    "CAREER_CUTOFF",
    "apply_prior_career_bf",
    "fetch_prior_career_bf_records",
    "load_prior_career_bf_records",
    "parse_prior_career_bf",
    "prior_career_bf_url",
]
