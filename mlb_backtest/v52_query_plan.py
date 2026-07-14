from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlencode

from .v52_evidence import LogicalQuery, canonical_query_bytes, logical_query_id
from .v52_seasons import SEASON_BOUNDARIES, monthly_ranges


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_STATCAST_ENDPOINT = "https://baseballsavant.mlb.com/statcast_search/csv"
_STATS_ENDPOINT = "https://statsapi.mlb.com/api/v1/stats"
_TEAMS_ENDPOINT = "https://statsapi.mlb.com/api/v1/teams"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_ACQUISITION_BATCH_ROOT = Path(
    "audit/v5_2_qd/historical/acquisition/query_batches"
)
@dataclass(frozen=True)
class QueryBatch:
    batch_id: str
    stage: str
    derived_from_sha256: str | None
    queries: tuple[LogicalQuery, ...]


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be canonical JSON compatible") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _query_record(query: LogicalQuery) -> dict[str, object]:
    record = json.loads(canonical_query_bytes(query))
    if not isinstance(record, dict):
        raise ValueError("logical query must encode as an object")
    return record


def _batch_payload(
    stage: str,
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> dict[str, object]:
    if not isinstance(stage, str) or not stage:
        raise ValueError("batch stage must be nonempty text")
    if derived_from_sha256 is not None:
        _validate_sha256(derived_from_sha256, "derived_from_sha256")
    if not isinstance(queries, Sequence) or any(
        not isinstance(query, LogicalQuery) for query in queries
    ):
        raise ValueError("batch queries must be LogicalQuery values")
    query_records = [_query_record(query) for query in queries]
    query_ids = [logical_query_id(query) for query in queries]
    if len(query_ids) != len(set(query_ids)):
        raise ValueError("query batch contains duplicate logical queries")
    return {
        "stage": stage,
        "derived_from_sha256": derived_from_sha256,
        "queries": query_records,
    }


def _make_batch(
    stage: str,
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> QueryBatch:
    ordered = tuple(queries)
    payload = _batch_payload(stage, derived_from_sha256, ordered)
    return QueryBatch(
        batch_id=_sha256_bytes(_canonical_json_bytes(payload)),
        stage=stage,
        derived_from_sha256=derived_from_sha256,
        queries=ordered,
    )


def _load_json_object(path: Path, label: str) -> tuple[dict[str, object], str]:
    if not isinstance(path, Path):
        path = Path(path)
    contents = path.read_bytes()
    try:
        value = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value, _sha256_bytes(contents)


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _logical_query(
    *,
    source: str,
    grain: str,
    endpoint: str,
    parameters: Mapping[str, object],
    requested_range: Mapping[str, object],
    expected_media_type: str,
    parser_version: str,
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
    parent_query_id: str | None = None,
) -> LogicalQuery:
    return LogicalQuery(
        source=source,
        grain=grain,
        endpoint=endpoint,
        parameters=dict(parameters),
        requested_range=dict(requested_range),
        expected_media_type=expected_media_type,
        parser_version=parser_version,
        parent_query_id=parent_query_id,
        run_spec_sha256=run_spec_sha256,
        acquisition_contract_sha256=acquisition_contract_sha256,
    )


def _source_contract(contract: Mapping[str, object], name: str) -> dict[str, object]:
    sources = contract.get("sources")
    if not isinstance(sources, list):
        raise ValueError("acquisition contract sources must be a list")
    matches = [item for item in sources if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"acquisition contract must define {name} exactly once")
    return matches[0]


def _statcast_parameters(first: str, last: str) -> dict[str, object]:
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


def build_static_batch(
    run_spec_path: Path,
    acquisition_contract_path: Path,
) -> QueryBatch:
    run_spec, run_sha = _load_json_object(run_spec_path, "RunSpec")
    contract, contract_sha = _load_json_object(
        acquisition_contract_path, "acquisition contract"
    )
    bindings = contract.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError("acquisition contract bindings must be an object")
    run_binding = bindings.get("run_spec")
    if not isinstance(run_binding, dict) or run_binding.get("sha256") != run_sha:
        raise ValueError("acquisition contract RunSpec binding differs")
    forbidden = contract.get("forbidden_years")
    years = contract.get("required_years")
    if years != list(range(2018, 2025)) or not isinstance(forbidden, list) or 2025 not in forbidden:
        raise ValueError("acquisition contract year scope differs")
    if 2025 not in run_spec.get("forbidden_target_years", []):
        raise ValueError("RunSpec must forbid 2025")

    schedule_contract = _source_contract(contract, "mlb_schedule_game_feed")
    statcast_contract = _source_contract(contract, "baseball_savant_statcast")
    if _SCHEDULE_ENDPOINT not in schedule_contract.get("endpoints", []):
        raise ValueError("official schedule endpoint differs")
    if _STATCAST_ENDPOINT not in statcast_contract.get("endpoints", []):
        raise ValueError("official Statcast endpoint differs")
    boundaries = contract.get("season_boundaries")
    if not isinstance(boundaries, list):
        raise ValueError("season boundaries must be a list")
    by_year = {
        item.get("year"): item for item in boundaries if isinstance(item, dict)
    }
    if set(by_year) != set(years):
        raise ValueError("season boundary years differ")

    queries: list[LogicalQuery] = []
    for year in years:
        boundary = by_year[year]
        first = _iso_date(boundary.get("start_date"), "season start")
        last = _iso_date(boundary.get("end_date"), "season end")
        if (
            first.year != year
            or last.year != year
            or first > last
            or (first, last) != SEASON_BOUNDARIES[year]
        ):
            raise ValueError("season boundary range differs")
        queries.append(
            _logical_query(
                source="mlb_schedule_game_feed",
                grain=str(schedule_contract["grain"]),
                endpoint=_SCHEDULE_ENDPOINT,
                parameters={
                    "sportId": 1,
                    "gameType": "R",
                    "startDate": first.isoformat(),
                    "endDate": last.isoformat(),
                    "hydrate": "probablePitcher,lineups,venue,linescore",
                },
                requested_range={
                    "year": year,
                    "start_date": first.isoformat(),
                    "end_date": last.isoformat(),
                },
                expected_media_type="application/json",
                parser_version=str(schedule_contract["parser_version"]),
                run_spec_sha256=run_sha,
                acquisition_contract_sha256=contract_sha,
            )
        )
        for chunk_start, chunk_end in monthly_ranges(first, last):
            start_text = chunk_start.isoformat()
            end_text = chunk_end.isoformat()
            queries.append(
                _logical_query(
                    source="baseball_savant_statcast",
                    grain=str(statcast_contract["grain"]),
                    endpoint=_STATCAST_ENDPOINT,
                    parameters=_statcast_parameters(start_text, end_text),
                    requested_range={
                        "year": year,
                        "start_date": start_text,
                        "end_date": end_text,
                    },
                    expected_media_type="text/csv",
                    parser_version=str(statcast_contract["parser_version"]),
                    run_spec_sha256=run_sha,
                    acquisition_contract_sha256=contract_sha,
                )
            )
    return _make_batch("STATIC_SCHEDULES", None, queries)


def _canonical_mapping_sha256(value: Mapping[str, object], label: str) -> str:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a string-keyed mapping")
    return _sha256_bytes(_canonical_json_bytes(dict(value)))


def _validate_binding_hashes(run_spec_sha256: str, acquisition_contract_sha256: str) -> None:
    _validate_sha256(run_spec_sha256, "run_spec_sha256")
    _validate_sha256(acquisition_contract_sha256, "acquisition_contract_sha256")


def build_schedule_reference_batch(
    anchor_inventory: Mapping[str, object],
    *,
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> QueryBatch:
    _validate_binding_hashes(run_spec_sha256, acquisition_contract_sha256)
    derived = _canonical_mapping_sha256(anchor_inventory, "anchor_inventory")
    anchors = anchor_inventory.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError("anchor_inventory.anchors must be a list")

    unique_games: dict[int, tuple[int, date, date, int, bool]] = {}
    seasons: set[int] = set()
    venues: set[int] = set()
    pitching_consumers: dict[tuple[int, date, date], set[int]] = {}
    for raw in anchors:
        if not isinstance(raw, dict):
            raise ValueError("schedule anchor must be an object")
        season = raw.get("season")
        game_pk = raw.get("game_pk")
        venue_id = raw.get("venue_id")
        played = raw.get("played")
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (season, game_pk, venue_id)):
            raise ValueError("schedule anchor IDs must be positive integers")
        if season not in range(2018, 2025):
            raise ValueError("schedule anchor season is outside 2018-2024")
        if type(played) is not bool:
            raise ValueError("schedule anchor played must be explicitly boolean")
        official = _iso_date(raw.get("official_date"), "anchor official_date")
        season_start = _iso_date(raw.get("season_start_date"), "anchor season_start_date")
        frozen_start, frozen_end = SEASON_BOUNDARIES[season]
        if season_start != frozen_start or not frozen_start <= official <= frozen_end:
            raise ValueError("schedule anchor dates differ from season")
        identity = (season, season_start, official, venue_id, played)
        if game_pk in unique_games and unique_games[game_pk] != identity:
            raise ValueError("conflicting duplicate schedule game_pk")
        unique_games[game_pk] = identity
        seasons.add(season)
        venues.add(venue_id)
        pitching_consumers.setdefault(
            (season, season_start, official - timedelta(days=1)), set()
        ).add(game_pk)

    queries: list[LogicalQuery] = []
    for game_pk, (season, _season_start, official, _venue_id, played) in sorted(
        unique_games.items()
    ):
        if not played:
            continue
        queries.append(
            _logical_query(
                source="mlb_schedule_game_feed",
                grain="season_schedule_and_game",
                endpoint=f"{_MLB_BASE}/game/{game_pk}/boxscore",
                parameters={},
                requested_range={
                    "season": season,
                    "game_pk": game_pk,
                    "official_date": official.isoformat(),
                },
                expected_media_type="application/json",
                parser_version="v5.2.1",
                run_spec_sha256=run_spec_sha256,
                acquisition_contract_sha256=acquisition_contract_sha256,
            )
        )
    for season, season_start, as_of in sorted(pitching_consumers):
        consumers = sorted(pitching_consumers[(season, season_start, as_of)])
        consumer_cutoffs = {str(game_pk): as_of.isoformat() for game_pk in consumers}
        if as_of < season_start:
            endpoint = "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
            parameters: dict[str, object] = {}
            requested = {
                "season": season,
                "as_of_date": as_of.isoformat(),
                "consumer_cutoff_dates": consumer_cutoffs,
                "consuming_game_pks": consumers,
                "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
            }
        else:
            endpoint = _STATS_ENDPOINT
            parameters = {
                "stats": "byDateRange",
                "group": "pitching",
                "sportIds": 1,
                "gameType": "R",
                "playerPool": "ALL",
                "limit": 5000,
                "startDate": season_start.isoformat(),
                "endDate": as_of.isoformat(),
            }
            requested = {
                "season": season,
                "start_date": season_start.isoformat(),
                "as_of_date": as_of.isoformat(),
                "consumer_cutoff_dates": consumer_cutoffs,
                "consuming_game_pks": consumers,
            }
        queries.append(
            _logical_query(
                source="mlb_stats_pitching",
                grain="season_d1_league_pitching_reference",
                endpoint=endpoint,
                parameters=parameters,
                requested_range=requested,
                expected_media_type="application/json",
                parser_version="v5.2.1",
                run_spec_sha256=run_spec_sha256,
                acquisition_contract_sha256=acquisition_contract_sha256,
            )
        )
    for season in sorted(seasons):
        queries.append(
            _logical_query(
                source="venue_team_timezone_table",
                grain="effective_dated_venue_team_reference",
                endpoint=_TEAMS_ENDPOINT,
                parameters={"sportId": 1, "season": season, "hydrate": "venue(location)"},
                requested_range={"season": season, "entity": "teams"},
                expected_media_type="application/json",
                parser_version="v5.2.1",
                run_spec_sha256=run_spec_sha256,
                acquisition_contract_sha256=acquisition_contract_sha256,
            )
        )
    for venue_id in sorted(venues):
        queries.append(
            _logical_query(
                source="venue_team_timezone_table",
                grain="effective_dated_venue_team_reference",
                endpoint=f"{_MLB_BASE}/venues/{venue_id}",
                parameters={"hydrate": "location"},
                requested_range={"venue_id": venue_id},
                expected_media_type="application/json",
                parser_version="v5.2.1",
                run_spec_sha256=run_spec_sha256,
                acquisition_contract_sha256=acquisition_contract_sha256,
            )
        )
    return _make_batch("REFERENCE_INPUTS", derived, queries)


def build_participant_derived_batch(
    game_evidence_inventory: Mapping[str, object],
    *,
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> QueryBatch:
    _validate_binding_hashes(run_spec_sha256, acquisition_contract_sha256)
    derived = _canonical_mapping_sha256(
        game_evidence_inventory, "game_evidence_inventory"
    )
    games = game_evidence_inventory.get("games")
    if not isinstance(games, list):
        raise ValueError("game_evidence_inventory.games must be a list")
    pitcher_consumers: dict[int, dict[int, date]] = {}
    seen_game_pks: set[int] = set()
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("game evidence must be an object")
        game_pk = game.get("game_pk")
        season = game.get("season")
        if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
            raise ValueError("game evidence game_pk must be a positive integer")
        if game_pk in seen_game_pks:
            raise ValueError("game evidence contains duplicate game_pk")
        seen_game_pks.add(game_pk)
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in SEASON_BOUNDARIES
        ):
            raise ValueError("game evidence season must be within 2018-2024")
        official_date = _iso_date(
            game.get("official_date"), "game evidence official_date"
        )
        season_start, season_end = SEASON_BOUNDARIES[season]
        if not season_start <= official_date <= season_end:
            raise ValueError("game evidence official_date differs from season")
        if (
            game.get("verification_status") != "VERIFIED"
            or game.get("participant_provenance")
            != "RECONSTRUCTED_FINAL_PARTICIPANTS"
        ):
            continue
        starters = game.get("actual_starter_ids")
        if not isinstance(starters, list) or len(starters) != 2:
            raise ValueError("verified game must contain two actual starter IDs")
        for pitcher_id in starters:
            if isinstance(pitcher_id, bool) or not isinstance(pitcher_id, int) or pitcher_id <= 0:
                raise ValueError("actual starter IDs must be positive integers")
            pitcher_consumers.setdefault(pitcher_id, {})[game_pk] = (
                official_date - timedelta(days=1)
            )
        if starters[0] == starters[1]:
            raise ValueError("verified game actual starter IDs must be distinct")

    queries = []
    for pitcher_id in sorted(pitcher_consumers):
        consumers = pitcher_consumers[pitcher_id]
        queries.append(_logical_query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            endpoint=_STATS_ENDPOINT,
            parameters={
                "personId": pitcher_id,
                "stats": "byDateRange",
                "group": "pitching",
                "gameType": "R",
                "sportIds": 1,
                "startDate": "1876-01-01",
                "endDate": "2017-12-31",
            },
            requested_range={
                "consumer_cutoff_dates": {
                    str(game_pk): consumers[game_pk].isoformat()
                    for game_pk in sorted(consumers)
                },
                "consuming_game_pks": sorted(consumers),
                "pitcher_id": pitcher_id,
                "inclusive_end": "2017-12-31",
            },
            expected_media_type="application/json",
            parser_version="v5.2.1",
            run_spec_sha256=run_spec_sha256,
            acquisition_contract_sha256=acquisition_contract_sha256,
        ))
    return _make_batch("PARTICIPANT_DERIVED", derived, queries)


def _canonical_statcast_parent_range(parent: LogicalQuery) -> tuple[date, date]:
    if (
        parent.source != "baseball_savant_statcast"
        or parent.grain != "pitch"
        or parent.endpoint != _STATCAST_ENDPOINT
        or parent.expected_media_type != "text/csv"
        or parent.parser_version != "v5.2.1"
    ):
        raise ValueError("parent must be an official Statcast query")
    first = _iso_date(parent.requested_range.get("start_date"), "parent start")
    last = _iso_date(parent.requested_range.get("end_date"), "parent end")
    if first > last:
        raise ValueError("parent query range is reversed")
    year = parent.requested_range.get("year")
    if (
        isinstance(year, bool)
        or not isinstance(year, int)
        or year not in SEASON_BOUNDARIES
        or first.year != year
        or last.year != year
    ):
        raise ValueError("Statcast parent year must be within 2018-2024")
    if first.month != last.month:
        raise ValueError("Statcast parent must stay within one calendar month")
    season_start, season_end = SEASON_BOUNDARIES[year]
    if not season_start <= first <= last <= season_end:
        raise ValueError("Statcast parent range is outside regular-season bounds")
    allowed_requested = {
        "year",
        "start_date",
        "end_date",
        "acquisition",
        "protected_inventory_sha256",
    }
    if not set(parent.requested_range) <= allowed_requested:
        raise ValueError("Statcast parent requested range contains extra fields")
    acquisition = parent.requested_range.get("acquisition")
    if acquisition is not None and acquisition not in {
        "FETCH_REQUIRED",
        "PROTECTED_LEGACY_REFERENCE",
    }:
        raise ValueError("Statcast parent acquisition disposition differs")
    protected_inventory_sha256 = parent.requested_range.get(
        "protected_inventory_sha256"
    )
    if acquisition == "PROTECTED_LEGACY_REFERENCE":
        _validate_sha256(
            protected_inventory_sha256,
            "Statcast protected_inventory_sha256",
        )
    elif protected_inventory_sha256 is not None:
        raise ValueError(
            "only protected legacy Statcast queries may bind inventory lineage"
        )
    expected_parameters = _statcast_parameters(first.isoformat(), last.isoformat())
    if dict(parent.parameters) != expected_parameters:
        raise ValueError("Statcast parent parameters differ from canonical filters")
    return first, last


def validate_statcast_query(query: LogicalQuery) -> tuple[date, date]:
    """Validate one Task3 Statcast query, including acquisition metadata."""
    return _canonical_statcast_parent_range(query)


def _statcast_child(
    parent: LogicalQuery,
    first: date,
    last: date,
    *,
    acquisition: str | None = None,
) -> LogicalQuery:
    requested = {
        key: value
        for key, value in parent.requested_range.items()
        if key not in {"start_date", "end_date", "acquisition"}
    }
    requested.update({"start_date": first.isoformat(), "end_date": last.isoformat()})
    if acquisition is not None:
        requested["acquisition"] = acquisition
    elif parent.requested_range.get("acquisition") is not None:
        requested["acquisition"] = parent.requested_range["acquisition"]
    parameters = dict(parent.parameters)
    parameters["game_date_gt"] = first.isoformat()
    parameters["game_date_lt"] = last.isoformat()
    return replace(
        parent,
        parameters=parameters,
        requested_range=requested,
        parent_query_id=logical_query_id(parent),
    )


def bisect_statcast_query(
    parent: LogicalQuery,
) -> tuple[LogicalQuery, LogicalQuery]:
    first, last = _canonical_statcast_parent_range(parent)
    if first == last:
        raise ValueError("single-day Statcast query cannot be bisected")
    midpoint = first + timedelta(days=(last - first).days // 2)
    left = _statcast_child(parent, first, midpoint)
    right = _statcast_child(parent, midpoint + timedelta(days=1), last)
    return left, right


def _legacy_ranges(
    protected_catalog: Sequence[Mapping[str, object]],
) -> list[tuple[date, date]]:
    result = []
    for record in protected_catalog:
        if not isinstance(record, Mapping):
            raise ValueError("protected catalog records must be mappings")
        if record.get("source") != "baseball_savant_statcast":
            continue
        if record.get("rawstore_verified") is not True:
            continue
        query = record.get("canonical_query")
        if not isinstance(query, Mapping) or set(query) != {
            "start",
            "end",
            "game_type",
            "url",
        }:
            raise ValueError("legacy Statcast canonical query fields differ")
        if query.get("game_type") != "R":
            raise ValueError("legacy Statcast game type differs")
        first = _iso_date(query.get("start"), "legacy start")
        last = _iso_date(query.get("end"), "legacy end")
        if first > last:
            raise ValueError("legacy Statcast range is reversed")
        if first.year not in {2022, 2023, 2024} or last.year != first.year:
            raise ValueError("legacy Statcast range must be within 2022-2024")
        if first.month != last.month:
            raise ValueError("legacy Statcast child must stay within one month")
        expected_url = (
            f"{_STATCAST_ENDPOINT}?"
            f"{urlencode(_statcast_parameters(first.isoformat(), last.isoformat()))}"
        )
        if query.get("url") != expected_url:
            raise ValueError("legacy Statcast canonical URL differs")
        result.append((first, last))
    return result


def build_legacy_partition_batch(
    parents: Sequence[LogicalQuery],
    protected_catalog: Sequence[Mapping[str, object]],
    *,
    protected_inventory_sha256: str,
) -> QueryBatch:
    _validate_sha256(protected_inventory_sha256, "protected_inventory_sha256")
    if not isinstance(parents, Sequence) or not isinstance(protected_catalog, Sequence):
        raise ValueError("parents and protected_catalog must be sequences")
    parent_ranges = []
    for parent in parents:
        if not isinstance(parent, LogicalQuery):
            raise ValueError("parents must contain LogicalQuery values")
        first, last = _canonical_statcast_parent_range(parent)
        parent_ranges.append((first, last, parent))
    for previous, current in zip(parent_ranges, parent_ranges[1:]):
        if current[0] <= previous[0]:
            raise ValueError("Statcast parent ranges must be strictly ordered")
        if current[0] <= previous[1]:
            raise ValueError("Statcast parent ranges overlap")

    legacy = _legacy_ranges(protected_catalog)
    candidates_by_parent: list[list[tuple[date, date]]] = [
        [] for _ in parent_ranges
    ]
    prior_parent_index = -1
    for child_first, child_last in legacy:
        matches = [
            index
            for index, (first, last, _parent) in enumerate(parent_ranges)
            if first <= child_first <= child_last <= last
        ]
        if len(matches) != 1:
            raise ValueError("verified legacy Statcast child maps to no unique parent")
        parent_index = matches[0]
        if parent_index < prior_parent_index:
            raise ValueError("legacy Statcast children must be ordered by parent")
        prior_parent_index = parent_index
        candidates_by_parent[parent_index].append((child_first, child_last))

    queries: list[LogicalQuery] = []
    for (first, last, parent), candidates in zip(
        parent_ranges, candidates_by_parent
    ):
        exact = [(start, end) for start, end in candidates if start == first and end == last]
        strict = [(start, end) for start, end in candidates if (start, end) != (first, last)]
        if exact and strict:
            raise ValueError("mixed parent/child legacy ambiguity")
        for previous, current in zip(candidates, candidates[1:]):
            if current == previous:
                raise ValueError("duplicate legacy Statcast child mapping")
            if current[0] < previous[0]:
                raise ValueError("legacy Statcast children must be strictly ordered")
            if current[0] <= previous[1]:
                raise ValueError("legacy Statcast child ranges overlap")

        cursor = first
        for child_first, child_last in candidates:
            if cursor < child_first:
                queries.append(
                    _statcast_child(
                        parent,
                        cursor,
                        child_first - timedelta(days=1),
                        acquisition="FETCH_REQUIRED",
                    )
                )
            promoted = _statcast_child(
                parent,
                child_first,
                child_last,
                acquisition="PROTECTED_LEGACY_REFERENCE",
            )
            promoted_range = dict(promoted.requested_range)
            promoted_range["protected_inventory_sha256"] = (
                protected_inventory_sha256
            )
            queries.append(replace(promoted, requested_range=promoted_range))
            cursor = child_last + timedelta(days=1)
        if cursor <= last:
            queries.append(
                _statcast_child(
                    parent,
                    cursor,
                    last,
                    acquisition="FETCH_REQUIRED",
                )
            )
    return _make_batch(
        "REMAINING", protected_inventory_sha256, queries
    )


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_root(root: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(root)))
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise ValueError("query batch root must resolve to an absolute path")
    current = os.open(os.sep, _directory_flags())
    try:
        for component in parts[1:]:
            child = -1
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(
                        "query batch root ancestors must be no-follow directories"
                    ) from exc
                raise
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError("query batch root directory identity differs")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_or_create_directory(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except FileExistsError:
            pass
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise ValueError("query batch ancestors must be no-follow directories") from exc
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("query batch ancestor must be a directory")
    return descriptor


def _read_existing(parent_fd: int, name: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("query batch must be a no-follow regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("query batch must be an unaliased regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _unlink_owned_temp(parent_fd: int, name: str, descriptor: int) -> None:
    try:
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
    ):
        raise ValueError("query batch temp ownership changed")
    os.unlink(name, dir_fd=parent_fd)


def _batch_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_linked_regular(
    parent_fd: int, name: str, label: str
) -> tuple[bytes, os.stat_result] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"{label} must be a no-follow regular file") from exc
    try:
        before = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or _batch_file_identity(before) != _batch_file_identity(linked)
        ):
            raise ValueError(f"{label} identity differs")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _batch_file_identity(before) != _batch_file_identity(after)
            or _batch_file_identity(after) != _batch_file_identity(rebound)
        ):
            raise ValueError(f"{label} identity changed while reading")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _reconcile_batch_final(
    parent_fd: int,
    batch_id: str,
    expected_contents: bytes,
) -> bytes | None:
    final_name = f"{batch_id}.json"
    final = _read_linked_regular(parent_fd, final_name, "query batch final")
    if final is None:
        return None
    final_contents, final_metadata = final
    if final_contents != expected_contents:
        raise RuntimeError("immutable query batch differs")
    if final_metadata.st_nlink == 1:
        return final_contents

    stale_name = re.compile(
        rf"^\.{re.escape(batch_id)}\.[0-9a-f]{{32}}\.tmp$"
    )
    removed = False
    for candidate_name in os.listdir(parent_fd):
        if stale_name.fullmatch(candidate_name) is None:
            continue
        candidate = _read_linked_regular(
            parent_fd, candidate_name, "query batch stale temp"
        )
        if candidate is None:
            continue
        candidate_contents, candidate_metadata = candidate
        if (
            (candidate_metadata.st_dev, candidate_metadata.st_ino)
            != (final_metadata.st_dev, final_metadata.st_ino)
        ):
            continue
        if candidate_contents != expected_contents:
            raise ValueError("query batch stale temp bytes differ")
        rebound = os.stat(
            candidate_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            rebound.st_dev,
            rebound.st_ino,
        ) != (
            candidate_metadata.st_dev,
            candidate_metadata.st_ino,
        ):
            raise ValueError("query batch stale temp identity changed")
        os.unlink(candidate_name, dir_fd=parent_fd)
        removed = True
    if removed:
        os.fsync(parent_fd)
    reconciled = _read_existing(parent_fd, final_name)
    if reconciled != expected_contents:
        raise ValueError("query batch final did not reconcile to one link")
    return reconciled


def _cleanup_prelink_batch_temps(parent_fd: int, batch_id: str) -> None:
    private_name = re.compile(
        rf"^\.{re.escape(batch_id)}\.[0-9a-f]{{32}}\.tmp$"
    )
    removed = False
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    for candidate_name in os.listdir(parent_fd):
        if private_name.fullmatch(candidate_name) is None:
            continue
        try:
            descriptor = os.open(candidate_name, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(
                "query batch private temp must be a no-follow regular file"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            linked = os.stat(
                candidate_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(linked.st_mode)
                or opened.st_nlink != 1
                or linked.st_nlink != 1
                or _batch_file_identity(opened) != _batch_file_identity(linked)
            ):
                raise ValueError(
                    "query batch private temp must be an unaliased regular file"
                )
            rebound = os.stat(
                candidate_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if _batch_file_identity(opened) != _batch_file_identity(rebound):
                raise ValueError("query batch private temp identity changed")
            os.unlink(candidate_name, dir_fd=parent_fd)
            removed = True
        finally:
            os.close(descriptor)
    if removed:
        os.fsync(parent_fd)


def publish_query_batch(root: Path, batch: QueryBatch) -> Path:
    if not isinstance(batch, QueryBatch):
        raise TypeError("batch must be a QueryBatch")
    payload = _batch_payload(batch.stage, batch.derived_from_sha256, batch.queries)
    expected_id = _sha256_bytes(_canonical_json_bytes(payload))
    if batch.batch_id != expected_id:
        raise ValueError("batch_id differs from canonical batch payload")
    record = {"schema_version": 1, "batch_id": batch.batch_id, **payload}
    contents = _canonical_json_bytes(record) + b"\n"
    root = Path(root)
    root_fd = _open_root(root)
    descriptors = [root_fd]
    try:
        current = root_fd
        for component in _ACQUISITION_BATCH_ROOT.parts:
            current = _open_or_create_directory(current, component)
            descriptors.append(current)
        fcntl.flock(current, fcntl.LOCK_EX)
        try:
            name = f"{batch.batch_id}.json"
            existing = _reconcile_batch_final(current, batch.batch_id, contents)
            _cleanup_prelink_batch_temps(current, batch.batch_id)
            if existing is not None:
                return root / _ACQUISITION_BATCH_ROOT / name
            temp_name = f".{batch.batch_id}.{secrets.token_hex(16)}.tmp"
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=current)
            temp_exists = True
            try:
                view = memoryview(contents)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("query batch publication made no progress")
                    view = view[written:]
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValueError("query batch temp publication was aliased")
                os.fsync(descriptor)
                if _read_existing(current, temp_name) != contents:
                    raise ValueError("query batch temp bytes differ")
                try:
                    os.link(
                        temp_name,
                        name,
                        src_dir_fd=current,
                        dst_dir_fd=current,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    _unlink_owned_temp(current, temp_name, descriptor)
                    temp_exists = False
                    os.fsync(current)
                    concurrent = _reconcile_batch_final(
                        current, batch.batch_id, contents
                    )
                    if concurrent is None:
                        raise ValueError("concurrent query batch final disappeared")
                    return root / _ACQUISITION_BATCH_ROOT / name
                _unlink_owned_temp(current, temp_name, descriptor)
                temp_exists = False
                os.fsync(current)
                if (
                    _reconcile_batch_final(current, batch.batch_id, contents)
                    != contents
                ):
                    raise ValueError("query batch bytes changed after publication")
                return root / _ACQUISITION_BATCH_ROOT / name
            finally:
                try:
                    if temp_exists:
                        _unlink_owned_temp(current, temp_name, descriptor)
                        os.fsync(current)
                finally:
                    os.close(descriptor)
        finally:
            fcntl.flock(current, fcntl.LOCK_UN)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
