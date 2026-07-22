from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlencode

from .diagnostics import deep_freeze
from .protocols import (
    RunSpec,
    SourceRunSpec,
    parse_run_spec,
    parse_source_run_spec,
)
from .v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    canonical_query_bytes,
    logical_query_id,
)
from .v52_seasons import SEASON_BOUNDARIES, monthly_ranges


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_BLOB = re.compile(r"^[0-9a-f]{40}$")
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_STATCAST_ENDPOINT = "https://baseballsavant.mlb.com/statcast_search/csv"
_STATS_ENDPOINT = "https://statsapi.mlb.com/api/v1/stats"
_TEAMS_ENDPOINT = "https://statsapi.mlb.com/api/v1/teams"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_ACQUISITION_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/historical_source_acquisition.json"
)
_SOURCE_RUN_SPEC_RELATIVE = Path(
    "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
_SOURCE_ACQUISITION_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
_APPROVED_DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
)
_IMPLEMENTATION_PLAN_RELATIVE = Path(
    "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md"
)
_PLAN_A_AMENDMENT_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md"
)
_SOURCE_APPROVED_DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/"
    "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md"
)
_SOURCE_IMPLEMENTATION_PLAN_RELATIVE = Path(
    "docs/superpowers/plans/"
    "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md"
)
_SOURCE_EPHEMERAL_PARSE_SCHEDULE_EQUIVALENCE_ADDENDUM_RELATIVE = Path(
    "docs/superpowers/specs/"
    "2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md"
)
_ACQUISITION_SCHEMA_RELATIVE = Path("schemas/v5_2_acquisition_contract.schema.json")
_ACQUISITION_BATCH_ROOT = Path("audit/v5_2_qd/historical/acquisition/query_batches")
_GAME_EVIDENCE_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json"
)
_SPLIT_RECEIPT_ROOT = Path(
    "audit/v5_2_qd/historical/acquisition/split_receipts"
)
_PRESEASON_DISPOSITION_REASON = "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START"
_SPLIT_REASONS = frozenset({"ROW_CAP", "SIZE_LIMIT", "TIMEOUT"})
_SPLIT_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_ALLOWED_STAGES = frozenset(
    {
        "STATIC_SCHEDULES",
        "REFERENCE_INPUTS",
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }
)


@dataclass(frozen=True)
class QueryBatch:
    batch_id: str
    stage: str
    derived_from_sha256: str | None
    queries: tuple[LogicalQuery, ...]


@dataclass(frozen=True)
class SplitReceipt:
    split_receipt_id: str
    receipt_sha256: str
    stage: str
    parent_batch_id: str
    parent_query_id: str
    reason: str
    exhausted_attempt_number: int
    depth: int
    child_batch_id: str
    child_query_ids: tuple[str, ...]
    recorded_at_utc: str


@dataclass(frozen=True)
class _StaticSeason:
    year: int
    first: date
    last: date


@dataclass(frozen=True)
class _StaticQueryInputs:
    seasons: tuple[_StaticSeason, ...]
    schedule_grain: str
    schedule_parser_version: str
    statcast_grain: str
    statcast_parser_version: str
    run_spec_sha256: str
    acquisition_contract_sha256: str


@dataclass(frozen=True)
class SourceAuthorityContext:
    repository_root: Path
    authority: RunSpec | SourceRunSpec
    authority_path: Path
    authority_sha256: str
    acquisition_contract_path: Path
    acquisition_contract_sha256: str
    contract: Mapping[str, object]
    run_id: str
    raw_root: Path
    sources_root: Path
    acquisition_audit_root: Path
    execution_audit_root: Path
    report_root: Path


@dataclass(frozen=True)
class _AuthorityBranch:
    authority_relative: Path
    contract_relative: Path
    design_relative: Path
    plan_relative: Path
    amendment_relative: Path | None
    addendum_relative: Path | None
    schema_index: int


_AUTHORITY_BRANCHES = (
    _AuthorityBranch(
        _RUN_SPEC_RELATIVE,
        _ACQUISITION_CONTRACT_RELATIVE,
        _APPROVED_DESIGN_RELATIVE,
        _IMPLEMENTATION_PLAN_RELATIVE,
        _PLAN_A_AMENDMENT_RELATIVE,
        None,
        0,
    ),
    _AuthorityBranch(
        _SOURCE_RUN_SPEC_RELATIVE,
        _SOURCE_ACQUISITION_CONTRACT_RELATIVE,
        _SOURCE_APPROVED_DESIGN_RELATIVE,
        _SOURCE_IMPLEMENTATION_PLAN_RELATIVE,
        None,
        _SOURCE_EPHEMERAL_PARSE_SCHEDULE_EQUIVALENCE_ADDENDUM_RELATIVE,
        1,
    ),
)


_SOURCE_SUPERSEDES = {
    "run_id": "historical_walkforward_500",
    "run_spec_sha256": "d622bff184f0bdade22848d4b76fc43bdf9ac88c7a51513dee4a7d5f51bd1492",
    "acquisition_contract_sha256": "cef89a1d4191ef83eb0fc2b0f4bdd191aeb8ba53cbb1bc6df73199604f23a9d9",
    "static_batch_id": "069fd746f9c7ecb1267b7ae44f8d44a1aeb3575a92c64482dacbc5b61d8f0571",
    "static_batch_sha256": "a6a2ac816620797b21e5bcf297fb92453f1143ed0d3d4c01effbf15c531ca39a",
    "checkpoint_sha256": "00b8b6eae2c0ba8de17f6ed1c05f0c9ddfb98e967e9b7ef0284581cc98b5ad06",
    "query_id": "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8",
    "quarantine_receipt_sha256": "a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8",
    "raw_sha256": "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0",
}


def _canonical_json_bytes(value: object) -> bytes:
    def normalized(item: object) -> object:
        if isinstance(item, Mapping):
            return {key: normalized(child) for key, child in item.items()}
        if isinstance(item, tuple):
            return [normalized(child) for child in item]
        if isinstance(item, list):
            return [normalized(child) for child in item]
        return item

    try:
        return json.dumps(
            normalized(value),
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


def _validate_shared_query_bindings(
    queries: Sequence[LogicalQuery], label: str
) -> None:
    bindings = {
        (query.run_spec_sha256, query.acquisition_contract_sha256)
        for query in queries
    }
    if len(bindings) != 1:
        raise ValueError(f"{label} queries must share run/contract bindings")
    run_spec_sha256, acquisition_contract_sha256 = next(iter(bindings))
    _validate_sha256(run_spec_sha256, f"{label} run_spec_sha256")
    _validate_sha256(
        acquisition_contract_sha256,
        f"{label} acquisition_contract_sha256",
    )


def _validate_static_schedule_stage(
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> None:
    if derived_from_sha256 is not None:
        raise ValueError("STATIC_SCHEDULES root cannot be derived")
    years = list(range(2018, 2025))
    if len(queries) != len(years):
        raise ValueError(
            "STATIC_SCHEDULES stage must contain the seven canonical season roots"
        )
    _validate_shared_query_bindings(queries, "STATIC_SCHEDULES")
    for query, year in zip(queries, years):
        first, last = SEASON_BOUNDARIES[year]
        expected_parameters = {
            "sportId": 1,
            "gameType": "R",
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "hydrate": "probablePitcher,lineups,venue,linescore",
        }
        expected_range = {
            "year": year,
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
        }
        if (
            query.source != "mlb_schedule_game_feed"
            or query.grain != "season_schedule_and_game"
            or query.endpoint != _SCHEDULE_ENDPOINT
            or query.expected_media_type != "application/json"
            or query.parser_version != "v5.2.1"
            or query.parent_query_id is not None
            or dict(query.parameters) != expected_parameters
            or dict(query.requested_range) != expected_range
        ):
            raise ValueError(
                "STATIC_SCHEDULES queries must be the ordered canonical "
                "2018-2024 season roots"
            )


def _validate_remaining_stage(
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> None:
    if not queries:
        raise ValueError("REMAINING stage must contain nonempty Statcast queries")
    _validate_shared_query_bindings(queries, "REMAINING")
    ranges = [_canonical_statcast_parent_range(query) for query in queries]
    if derived_from_sha256 is not None:
        for query in queries:
            _validate_sha256(query.parent_query_id, "REMAINING parent_query_id")
        return

    expected = [
        (year, first, last)
        for year in range(2018, 2025)
        for first, last in monthly_ranges(*SEASON_BOUNDARIES[year])
    ]
    if len(queries) != len(expected):
        raise ValueError(
            "REMAINING Statcast root must contain all 46 canonical monthly queries"
        )
    for query, (first, last), (year, expected_first, expected_last) in zip(
        queries, ranges, expected
    ):
        if (
            query.parent_query_id is not None
            or (first, last) != (expected_first, expected_last)
            or dict(query.requested_range)
            != {
                "year": year,
                "start_date": expected_first.isoformat(),
                "end_date": expected_last.isoformat(),
            }
        ):
            raise ValueError(
                "REMAINING Statcast root must be the ordered canonical monthly coverage"
            )


def _reference_common(query: LogicalQuery, label: str) -> None:
    if (
        query.parent_query_id is not None
        or query.expected_media_type != "application/json"
        or query.parser_version != "v5.2.1"
    ):
        raise ValueError(f"REFERENCE_INPUTS {label} query contract differs")


def _validate_reference_inputs_stage(
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> None:
    derived = _validate_sha256(
        derived_from_sha256, "REFERENCE_INPUTS derived_from_sha256"
    )
    if not derived or not queries:
        raise ValueError("REFERENCE_INPUTS requires a nonempty derived batch")
    _validate_shared_query_bindings(queries, "REFERENCE_INPUTS")

    categories: list[int] = []
    boxscores: list[tuple[int, int, date]] = []
    pitching: list[tuple[int, date, tuple[int, ...]]] = []
    teams: list[int] = []
    venues: list[int] = []
    boxscore_endpoint = re.compile(rf"^{re.escape(_MLB_BASE)}/game/([1-9][0-9]*)/boxscore$")
    venue_endpoint = re.compile(rf"^{re.escape(_MLB_BASE)}/venues/([1-9][0-9]*)$")

    for query in queries:
        _reference_common(query, "source")
        boxscore_match = boxscore_endpoint.fullmatch(query.endpoint)
        venue_match = venue_endpoint.fullmatch(query.endpoint)
        if query.source == "mlb_schedule_game_feed" and boxscore_match is not None:
            categories.append(0)
            game_pk = int(boxscore_match.group(1))
            requested = dict(query.requested_range)
            season = requested.get("season")
            requested_game_pk = requested.get("game_pk")
            if (
                query.grain != "season_schedule_and_game"
                or dict(query.parameters)
                or set(requested) != {"season", "game_pk", "official_date"}
                or isinstance(season, bool)
                or not isinstance(season, int)
                or season not in SEASON_BOUNDARIES
                or requested_game_pk != game_pk
            ):
                raise ValueError("REFERENCE_INPUTS boxscore query differs")
            official = _iso_date(
                requested.get("official_date"), "REFERENCE_INPUTS boxscore date"
            )
            first, last = SEASON_BOUNDARIES[season]
            if not first <= official <= last:
                raise ValueError("REFERENCE_INPUTS boxscore date differs from season")
            boxscores.append((game_pk, season, official))
            continue

        if query.source == "mlb_stats_pitching":
            categories.append(1)
            requested = dict(query.requested_range)
            season = requested.get("season")
            if (
                query.grain != "season_d1_league_pitching_reference"
                or isinstance(season, bool)
                or not isinstance(season, int)
                or season not in SEASON_BOUNDARIES
            ):
                raise ValueError("REFERENCE_INPUTS pitching query differs")
            as_of = _iso_date(
                requested.get("as_of_date"), "REFERENCE_INPUTS pitching as_of_date"
            )
            consumers = requested.get("consuming_game_pks")
            cutoffs = requested.get("consumer_cutoff_dates")
            if (
                not isinstance(consumers, list)
                or not consumers
                or any(
                    isinstance(game_pk, bool)
                    or not isinstance(game_pk, int)
                    or game_pk <= 0
                    for game_pk in consumers
                )
                or consumers != sorted(set(consumers))
                or not isinstance(cutoffs, dict)
                or set(cutoffs) != {str(game_pk) for game_pk in consumers}
                or any(value != as_of.isoformat() for value in cutoffs.values())
            ):
                raise ValueError("REFERENCE_INPUTS pitching consumers differ")
            first, _ = SEASON_BOUNDARIES[season]
            if as_of < first:
                expected_requested = {
                    "season": season,
                    "as_of_date": as_of.isoformat(),
                    "consumer_cutoff_dates": cutoffs,
                    "consuming_game_pks": consumers,
                    "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
                    "reason": _PRESEASON_DISPOSITION_REASON,
                }
                if (
                    as_of != first - timedelta(days=1)
                    or query.endpoint
                    != "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
                    or dict(query.parameters)
                    or requested != expected_requested
                ):
                    raise ValueError(
                        "REFERENCE_INPUTS preseason disposition differs"
                    )
            else:
                expected_parameters = {
                    "stats": "byDateRange",
                    "group": "pitching",
                    "sportIds": 1,
                    "gameType": "R",
                    "playerPool": "ALL",
                    "limit": 5000,
                    "startDate": first.isoformat(),
                    "endDate": as_of.isoformat(),
                }
                expected_requested = {
                    "season": season,
                    "start_date": first.isoformat(),
                    "as_of_date": as_of.isoformat(),
                    "consumer_cutoff_dates": cutoffs,
                    "consuming_game_pks": consumers,
                    "response_completeness": {
                        "limit": 5000,
                        "reject_total_splits_at_or_above_limit": True,
                    },
                }
                if (
                    query.endpoint != _STATS_ENDPOINT
                    or dict(query.parameters) != expected_parameters
                    or requested != expected_requested
                ):
                    raise ValueError("REFERENCE_INPUTS pitching query differs")
            pitching.append((season, as_of, tuple(consumers)))
            continue

        if query.source == "venue_team_timezone_table" and query.endpoint == _TEAMS_ENDPOINT:
            categories.append(2)
            requested = dict(query.requested_range)
            season = requested.get("season")
            if (
                query.grain != "effective_dated_venue_team_reference"
                or isinstance(season, bool)
                or not isinstance(season, int)
                or dict(query.parameters)
                != {
                    "sportId": 1,
                    "season": season,
                    "hydrate": "venue(location)",
                }
                or requested != {"season": season, "entity": "teams"}
            ):
                raise ValueError("REFERENCE_INPUTS team query differs")
            teams.append(season)
            continue

        if (
            query.source == "venue_team_timezone_table"
            and venue_match is not None
        ):
            categories.append(3)
            venue_id = int(venue_match.group(1))
            if (
                query.grain != "effective_dated_venue_team_reference"
                or dict(query.parameters) != {"hydrate": "location"}
                or dict(query.requested_range) != {"venue_id": venue_id}
            ):
                raise ValueError("REFERENCE_INPUTS venue query differs")
            venues.append(venue_id)
            continue
        raise ValueError("REFERENCE_INPUTS contains an unapproved source query")

    if categories != sorted(categories):
        raise ValueError("REFERENCE_INPUTS query partitions are out of order")
    game_pks = [row[0] for row in boxscores]
    if game_pks != sorted(set(game_pks)):
        raise ValueError("REFERENCE_INPUTS boxscores are not uniquely ordered")
    pitching_keys = [(season, as_of) for season, as_of, _ in pitching]
    if pitching_keys != sorted(set(pitching_keys)):
        raise ValueError(
            "REFERENCE_INPUTS pitching groups are not uniquely ordered"
        )
    if teams != list(range(2018, 2025)):
        raise ValueError("REFERENCE_INPUTS requires the seven canonical team queries")
    if not venues or venues != sorted(set(venues)):
        raise ValueError("REFERENCE_INPUTS venues are not uniquely ordered")

    boxscore_by_pk = {
        game_pk: (season, official) for game_pk, season, official in boxscores
    }
    pitching_consumers: set[int] = set()
    for season, as_of, consumers in pitching:
        for game_pk in consumers:
            if game_pk in pitching_consumers:
                raise ValueError("REFERENCE_INPUTS pitching consumer is duplicated")
            expected = boxscore_by_pk.get(game_pk)
            if expected != (season, as_of + timedelta(days=1)):
                raise ValueError(
                    "REFERENCE_INPUTS pitching consumer differs from boxscore"
                )
            pitching_consumers.add(game_pk)
    if pitching_consumers != set(boxscore_by_pk):
        raise ValueError("REFERENCE_INPUTS pitching coverage differs from boxscores")


def _validate_participant_derived_stage(
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> None:
    _validate_sha256(derived_from_sha256, "PARTICIPANT_DERIVED derived_from_sha256")
    if not queries:
        raise ValueError("PARTICIPANT_DERIVED requires a nonempty derived batch")
    _validate_shared_query_bindings(queries, "PARTICIPANT_DERIVED")

    baselines = ("2017-12-31", "2021-12-31")
    keys: list[tuple[int, int]] = []
    consumers_by_pitcher: dict[int, set[int]] = {}
    for query in queries:
        requested = dict(query.requested_range)
        parameters = dict(query.parameters)
        pitcher_id = requested.get("pitcher_id")
        inclusive_end = requested.get("inclusive_end")
        consumers = requested.get("consuming_game_pks")
        cutoffs = requested.get("consumer_cutoff_dates")
        if (
            isinstance(pitcher_id, bool)
            or not isinstance(pitcher_id, int)
            or pitcher_id <= 0
            or inclusive_end not in baselines
            or query.source != "mlb_stats_pitcher_bf"
            or query.grain != "player_career_batters_faced_reference"
            or query.endpoint != f"{_MLB_BASE}/people/{pitcher_id}/stats"
            or query.expected_media_type != "application/json"
            or query.parser_version != "v5.2.1"
            or query.parent_query_id is not None
            or set(requested)
            != {
                "consumer_cutoff_dates",
                "consuming_game_pks",
                "pitcher_id",
                "inclusive_end",
            }
            or parameters
            != {
                "stats": "byDateRange",
                "group": "pitching",
                "gameType": "R",
                "sportIds": 1,
                "startDate": "1876-01-01",
                "endDate": inclusive_end,
            }
            or not isinstance(consumers, list)
            or not consumers
            or any(
                isinstance(game_pk, bool)
                or not isinstance(game_pk, int)
                or game_pk <= 0
                for game_pk in consumers
            )
            or consumers != sorted(set(consumers))
            or not isinstance(cutoffs, dict)
            or set(cutoffs) != {str(game_pk) for game_pk in consumers}
        ):
            raise ValueError("PARTICIPANT_DERIVED query contract differs")

        expected_baseline = None
        for game_pk in consumers:
            cutoff = _iso_date(
                cutoffs[str(game_pk)], "PARTICIPANT_DERIVED consumer cutoff"
            )
            if not date(2017, 12, 31) <= cutoff <= date(2024, 12, 30):
                raise ValueError(
                    "PARTICIPANT_DERIVED consumer cutoff is outside the historical run"
                )
            baseline = "2017-12-31" if cutoff <= date(2021, 12, 31) else "2021-12-31"
            if expected_baseline is None:
                expected_baseline = baseline
            elif expected_baseline != baseline:
                raise ValueError("PARTICIPANT_DERIVED query mixes baseline partitions")
        if inclusive_end != expected_baseline:
            raise ValueError("PARTICIPANT_DERIVED baseline differs from cutoffs")

        key = (pitcher_id, baselines.index(inclusive_end))
        keys.append(key)
        seen = consumers_by_pitcher.setdefault(pitcher_id, set())
        if seen.intersection(consumers):
            raise ValueError("PARTICIPANT_DERIVED consumer is duplicated")
        seen.update(consumers)

    if keys != sorted(set(keys)):
        raise ValueError("PARTICIPANT_DERIVED queries are not uniquely ordered")


def _validate_stage_batch(
    stage: str,
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> None:
    if stage == "STATIC_SCHEDULES":
        _validate_static_schedule_stage(derived_from_sha256, queries)
    elif stage == "REFERENCE_INPUTS":
        _validate_reference_inputs_stage(derived_from_sha256, queries)
    elif stage == "PARTICIPANT_DERIVED":
        _validate_participant_derived_stage(derived_from_sha256, queries)
    elif stage == "REMAINING":
        _validate_remaining_stage(derived_from_sha256, queries)


def _batch_payload(
    stage: str,
    derived_from_sha256: str | None,
    queries: Sequence[LogicalQuery],
) -> dict[str, object]:
    if not isinstance(stage, str) or not stage:
        raise ValueError("batch stage must be nonempty text")
    if stage not in _ALLOWED_STAGES:
        raise ValueError("batch stage is not an approved acquisition stage")
    if derived_from_sha256 is not None:
        _validate_sha256(derived_from_sha256, "derived_from_sha256")
    if not isinstance(queries, Sequence) or any(
        not isinstance(query, LogicalQuery) for query in queries
    ):
        raise ValueError("batch queries must be LogicalQuery values")
    query_records = [_query_record(query) for query in queries]
    query_ids = [logical_query_id(query) for query in queries]
    if len(query_ids) != len(set(query_ids)):
        raise ValueError(f"{stage} query batch contains duplicate logical queries")
    _validate_stage_batch(stage, derived_from_sha256, queries)
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


def _parse_json_object(
    contents: bytes, label: str, *, canonical: bool = False
) -> dict[str, object]:
    try:
        value = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if canonical and contents != _canonical_json_bytes(value) + b"\n":
        raise ValueError(f"{label} must be canonical JSON with one newline")
    return value


def _load_json_object(
    path: Path, label: str, *, canonical: bool = False
) -> tuple[dict[str, object], str]:
    if not isinstance(path, Path):
        path = Path(path)
    contents = path.read_bytes()
    return (
        _parse_json_object(contents, label, canonical=canonical),
        _sha256_bytes(contents),
    )


def _authority_branch(
    run_spec_path: Path, acquisition_contract_path: Path
) -> tuple[Path, _AuthorityBranch]:
    supplied_run = Path(run_spec_path)
    supplied_contract = Path(acquisition_contract_path)
    for supplied, label in (
        (supplied_run, "RunSpec"),
        (supplied_contract, "acquisition contract"),
    ):
        text = os.fspath(supplied)
        if (
            not supplied.is_absolute()
            or "\\" in text
            or any(part in {"", ".", ".."} for part in supplied.parts)
            or Path(os.path.abspath(text)) != supplied
        ):
            raise ValueError(f"{label} path differs from the repository contract")
    for branch in _AUTHORITY_BRANCHES:
        if (
            supplied_run.parts[-len(branch.authority_relative.parts) :]
            != branch.authority_relative.parts
        ):
            continue
        root = supplied_run
        for _ in branch.authority_relative.parts:
            root = root.parent
        if supplied_contract != root / branch.contract_relative:
            raise ValueError("RunSpec/acquisition contract pair differs")
        return root, branch
    raise ValueError("RunSpec path differs from the repository contract")


def _read_repository_file(root: Path, relative: Path, label: str) -> bytes:
    if (
        relative.is_absolute()
        or not relative.parts
        or any(component in {"", ".", ".."} for component in relative.parts)
    ):
        raise ValueError(f"{label} path must stay under the repository root")
    descriptors = [_open_root(root)]
    try:
        current = descriptors[0]
        for component in relative.parent.parts:
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except OSError as exc:
                raise ValueError(
                    f"{label} ancestors must be no-follow directories"
                ) from exc
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError(f"{label} ancestor identity differs")
            except BaseException:
                os.close(child)
                raise
            descriptors.append(child)
            current = child
        result = _read_linked_regular(current, relative.name, label)
        if result is None:
            raise ValueError(f"{label} is missing")
        contents, metadata = result
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} must be an unaliased regular file")
        return contents
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_repository_binding(
    root: Path,
    raw: object,
    *,
    expected_path: Path,
    label: str,
    digest_key: str = "sha256",
    extra_keys: frozenset[str] = frozenset(),
) -> bytes:
    expected_keys = {"path", digest_key, *extra_keys}
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise ValueError(f"{label} binding differs")
    if raw.get("path") != expected_path.as_posix():
        raise ValueError(f"{label} path binding differs")
    digest = raw.get(digest_key)
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} hash binding differs")
    contents = _read_repository_file(root, expected_path, label)
    if _sha256_bytes(contents) != digest:
        raise ValueError(f"{label} hash binding differs")
    return contents


def _clean_git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _verify_head_blob(
    root: Path,
    relative: Path,
    contents: bytes,
    label: str,
    *,
    expected_blob: object | None = None,
) -> str:
    if expected_blob is not None and (
        not isinstance(expected_blob, str) or not _GIT_BLOB.fullmatch(expected_blob)
    ):
        raise ValueError(f"{label} Git blob binding differs")
    environment = _clean_git_environment()
    top_level = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        check=False,
        capture_output=True,
        env=environment,
    )
    resolved = subprocess.run(
        ["git", "rev-parse", f"HEAD:{relative.as_posix()}"],
        cwd=root,
        check=False,
        capture_output=True,
        env=environment,
    )
    resolved_blob = resolved.stdout.decode("ascii", errors="replace").strip()
    blob_identity = expected_blob if expected_blob is not None else resolved_blob
    blob = subprocess.run(
        ["git", "cat-file", "blob", str(blob_identity)],
        cwd=root,
        check=False,
        capture_output=True,
        env=environment,
    )
    if (
        top_level.returncode
        or Path(
            os.path.abspath(top_level.stdout.decode("utf-8", errors="replace").strip())
        )
        != Path(os.path.abspath(root))
        or resolved.returncode
        or not _GIT_BLOB.fullmatch(resolved_blob)
        or resolved_blob != blob_identity
        or blob.returncode
        or blob.stdout != contents
    ):
        raise ValueError(f"{label} Git blob binding differs")
    return resolved_blob


def _verify_implementation_plan_git_blob(
    root: Path,
    relative: Path,
    raw: dict[str, object],
    contents: bytes,
) -> None:
    _verify_head_blob(
        root,
        relative,
        contents,
        "implementation plan",
        expected_blob=raw.get("git_blob_id"),
    )


def _verify_frozen_acquisition_schema(
    root: Path,
    contract: dict[str, object],
    *,
    schema_index: int,
) -> None:
    contents = _read_repository_file(
        root,
        _ACQUISITION_SCHEMA_RELATIVE,
        "acquisition contract schema",
    )
    schema = _parse_json_object(
        contents,
        "acquisition contract schema",
        canonical=True,
    )
    variants = schema.get("oneOf")
    if (
        set(schema) != {"$schema", "oneOf"}
        or schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or not isinstance(variants, list)
        or len(variants) != len(_AUTHORITY_BRANCHES)
        or not 0 <= schema_index < len(variants)
        or not all(isinstance(item, dict) for item in variants)
    ):
        raise ValueError("acquisition contract differs from its frozen schema")
    selected = variants[schema_index]
    required = selected.get("required")
    properties = selected.get("properties")
    if (
        selected.get("additionalProperties") is not False
        or selected.get("type") != "object"
        or _canonical_json_bytes(selected.get("const"))
        != _canonical_json_bytes(contract)
        or not isinstance(required, list)
        or set(required) != set(contract)
        or len(required) != len(contract)
        or not isinstance(properties, dict)
        or set(properties) != set(contract)
    ):
        raise ValueError("acquisition contract differs from its frozen schema")
    _verify_head_blob(
        root,
        _ACQUISITION_SCHEMA_RELATIVE,
        contents,
        "acquisition contract schema",
    )


def _namespace_relative(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{label} namespace path differs")
    relative = Path(value)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != value
    ):
        raise ValueError(f"{label} namespace path differs")
    return relative


def _validate_namespace_ancestors(
    root: Path, relative: Path, label: str
) -> tuple[int, int] | None:
    descriptors = [_open_root(root)]
    try:
        current = descriptors[0]
        for index, component in enumerate(relative.parts):
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ValueError(
                    f"{label} namespace ancestors must be no-follow directories"
                ) from exc
            descriptors.append(child)
            opened = os.fstat(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (linked.st_dev, linked.st_ino)
            ):
                raise ValueError(f"{label} namespace directory identity differs")
            current = child
            if index == len(relative.parts) - 1:
                return opened.st_dev, opened.st_ino
        raise AssertionError("nonempty namespace path was not traversed")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.parts[: len(right.parts)] == right.parts
        or right.parts[: len(left.parts)] == left.parts
    )


def _context_roots(
    root: Path,
    authority: RunSpec | SourceRunSpec,
    contract: Mapping[str, object],
) -> tuple[Path, Path, Path, Path, Path]:
    namespaces = authority.raw.get("namespaces")
    canonical_roots = contract.get("canonical_roots")
    if not isinstance(namespaces, Mapping) or not isinstance(
        canonical_roots, Mapping
    ):
        raise ValueError("authority namespace bindings differ")
    raw_relative = _namespace_relative(namespaces.get("raw"), "raw")
    audit_relative = _namespace_relative(namespaces.get("audit"), "audit")
    report_relative = _namespace_relative(namespaces.get("reports"), "reports")
    sources_relative = _namespace_relative(
        namespaces.get("sources", (raw_relative.parent / "sources").as_posix()),
        "sources",
    )
    acquisition_relative = audit_relative / "acquisition"
    execution_relative = audit_relative / "execution"
    expected_contract_roots = {
        "raw": raw_relative.as_posix(),
        "sources": sources_relative.as_posix(),
        "acquisition_audit": acquisition_relative.as_posix(),
        "execution_audit": execution_relative.as_posix(),
    }
    if dict(canonical_roots) != expected_contract_roots:
        raise ValueError("acquisition contract canonical roots differ")
    relatives = (
        raw_relative,
        sources_relative,
        acquisition_relative,
        execution_relative,
        report_relative,
    )
    for index, left in enumerate(relatives):
        for right in relatives[index + 1 :]:
            if _paths_overlap(left, right):
                raise ValueError("authority namespace roots overlap")
    identities = [
        _validate_namespace_ancestors(root, relative, relative.as_posix())
        for relative in relatives
    ]
    present = [identity for identity in identities if identity is not None]
    if len(present) != len(set(present)):
        raise ValueError("authority namespace roots alias")
    return tuple(root / relative for relative in relatives)  # type: ignore[return-value]


def _load_static_contracts(
    run_spec_path: Path, acquisition_contract_path: Path
) -> SourceAuthorityContext:
    run_spec_path = Path(run_spec_path)
    acquisition_contract_path = Path(acquisition_contract_path)
    root, branch = _authority_branch(run_spec_path, acquisition_contract_path)
    run_spec_contents = _read_repository_file(root, branch.authority_relative, "RunSpec")
    run_raw = _parse_json_object(run_spec_contents, "RunSpec", canonical=True)
    authority = (
        parse_run_spec(run_raw)
        if branch.schema_index == 0
        else parse_source_run_spec(run_raw)
    )
    run_sha = _sha256_bytes(run_spec_contents)
    contract_contents = _read_repository_file(
        root,
        branch.contract_relative,
        "acquisition contract",
    )
    contract = _parse_json_object(
        contract_contents,
        "acquisition contract",
        canonical=True,
    )
    contract_sha = _sha256_bytes(contract_contents)
    if contract.get("schema_version") != 2:
        raise ValueError("acquisition contract must use schema version 2")
    bindings = contract.get("bindings")
    if not isinstance(bindings, dict):
        raise AssertionError("validated acquisition contract bindings must be an object")
    verified_run_spec = _verify_repository_binding(
        root,
        bindings.get("run_spec"),
        expected_path=branch.authority_relative,
        label="RunSpec",
    )
    if verified_run_spec != run_spec_contents:
        raise ValueError("acquisition contract RunSpec binding differs")
    _verify_repository_binding(
        root,
        bindings.get("approved_design"),
        expected_path=branch.design_relative,
        label="approved design",
    )
    plan_binding = bindings.get("implementation_plan")
    plan_contents = _verify_repository_binding(
        root,
        plan_binding,
        expected_path=branch.plan_relative,
        label="implementation plan",
        digest_key="working_tree_sha256",
        extra_keys=frozenset({"git_blob_id"}),
    )
    if not isinstance(plan_binding, dict):
        raise AssertionError("validated implementation plan binding must be an object")
    _verify_implementation_plan_git_blob(
        root, branch.plan_relative, plan_binding, plan_contents
    )
    if branch.addendum_relative is None:
        if branch.amendment_relative is None:
            raise AssertionError("default authority amendment path is missing")
        _verify_repository_binding(
            root,
            bindings.get("plan_a_amendment"),
            expected_path=branch.amendment_relative,
            label="Plan A amendment",
        )
    else:
        if set(bindings) != {
            "approved_design",
            "ephemeral_parse_schedule_equivalence_addendum",
            "implementation_plan",
            "run_spec",
        } or contract.get("supersedes") != _SOURCE_SUPERSEDES:
            raise ValueError("superseding acquisition authority differs")
        addendum_binding = bindings.get(
            "ephemeral_parse_schedule_equivalence_addendum"
        )
        addendum_contents = _verify_repository_binding(
            root,
            addendum_binding,
            expected_path=branch.addendum_relative,
            label="ephemeral parse schedule equivalence addendum",
            extra_keys=frozenset({"git_blob_id"}),
        )
        if not isinstance(addendum_binding, dict):
            raise AssertionError("validated addendum binding must be an object")
        _verify_head_blob(
            root,
            branch.addendum_relative,
            addendum_contents,
            "ephemeral parse schedule equivalence addendum",
            expected_blob=addendum_binding.get("git_blob_id"),
        )
        _verify_repository_binding(
            root,
            addendum_binding,
            expected_path=branch.addendum_relative,
            label="ephemeral parse schedule equivalence addendum",
            extra_keys=frozenset({"git_blob_id"}),
        )
    _verify_frozen_acquisition_schema(
        root,
        contract,
        schema_index=branch.schema_index,
    )
    raw_root, sources_root, acquisition_root, execution_root, report_root = (
        _context_roots(root, authority, contract)
    )
    return SourceAuthorityContext(
        repository_root=root,
        authority=authority,
        authority_path=root / branch.authority_relative,
        authority_sha256=run_sha,
        acquisition_contract_path=root / branch.contract_relative,
        acquisition_contract_sha256=contract_sha,
        contract=deep_freeze(contract),
        run_id=authority.run_id,
        raw_root=raw_root,
        sources_root=sources_root,
        acquisition_audit_root=acquisition_root,
        execution_audit_root=execution_root,
        report_root=report_root,
    )


def _validate_run_bound_acquisition_contract(
    root: Path,
    run_spec_contents: bytes,
    contract_contents: bytes,
) -> tuple[dict[str, object], str, dict[str, object], str]:
    """Existing old-authority content binding used by downstream validators."""
    run_spec = _parse_json_object(run_spec_contents, "RunSpec")
    run_sha = _sha256_bytes(run_spec_contents)
    contract = _parse_json_object(
        contract_contents,
        "acquisition contract",
        canonical=True,
    )
    contract_sha = _sha256_bytes(contract_contents)
    if contract.get("schema_version") != 2:
        raise ValueError("acquisition contract must use schema version 2")
    bindings = contract.get("bindings")
    if not isinstance(bindings, dict):
        raise ValueError("acquisition contract bindings must be an object")
    verified = _verify_repository_binding(
        root,
        bindings.get("run_spec"),
        expected_path=_RUN_SPEC_RELATIVE,
        label="RunSpec",
    )
    if verified != run_spec_contents:
        raise ValueError("acquisition contract RunSpec binding differs")
    return run_spec, run_sha, contract, contract_sha


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


def _source_contract(
    contract: Mapping[str, object], name: str
) -> Mapping[str, object]:
    sources = contract.get("sources")
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        raise ValueError("acquisition contract sources must be a list")
    matches = [
        item
        for item in sources
        if isinstance(item, Mapping) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise ValueError(f"acquisition contract must define {name} exactly once")
    return matches[0]


def _source_identity_text(
    source_contract: Mapping[str, object], key: str, label: str
) -> str:
    value = source_contract.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"acquisition {label} differs")
    return value


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


def _validated_static_query_inputs(
    run_spec_path: Path,
    acquisition_contract_path: Path,
) -> _StaticQueryInputs:
    context = _load_static_contracts(
        run_spec_path, acquisition_contract_path
    )
    run_spec = context.authority.raw
    run_sha = context.authority_sha256
    contract = context.contract
    contract_sha = context.acquisition_contract_sha256
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("acquisition contract bindings must be an object")
    run_binding = bindings.get("run_spec")
    if not isinstance(run_binding, Mapping) or run_binding.get("sha256") != run_sha:
        raise ValueError("acquisition contract RunSpec binding differs")
    forbidden = contract.get("forbidden_years")
    years = contract.get("required_years")
    if (
        not isinstance(years, Sequence)
        or isinstance(years, (str, bytes))
        or tuple(years) != tuple(range(2018, 2025))
        or not isinstance(forbidden, Sequence)
        or isinstance(forbidden, (str, bytes))
        or 2025 not in forbidden
    ):
        raise ValueError("acquisition contract year scope differs")
    forbidden_authority_years = (
        run_spec.get("forbidden_target_years", [])
        if isinstance(context.authority, RunSpec)
        else run_spec.get("forbidden_source_years", [])
    )
    if 2025 not in forbidden_authority_years:
        raise ValueError("RunSpec must forbid 2025")

    schedule_contract = _source_contract(contract, "mlb_schedule_game_feed")
    statcast_contract = _source_contract(contract, "baseball_savant_statcast")
    if _SCHEDULE_ENDPOINT not in schedule_contract.get("endpoints", []):
        raise ValueError("official schedule endpoint differs")
    if _STATCAST_ENDPOINT not in statcast_contract.get("endpoints", []):
        raise ValueError("official Statcast endpoint differs")
    boundaries = contract.get("season_boundaries")
    if not isinstance(boundaries, Sequence) or isinstance(boundaries, (str, bytes)):
        raise ValueError("season boundaries must be a list")
    by_year = {
        item.get("year"): item
        for item in boundaries
        if isinstance(item, Mapping)
    }
    if set(by_year) != set(years):
        raise ValueError("season boundary years differ")

    seasons: list[_StaticSeason] = []
    for year in years:
        if isinstance(year, bool) or not isinstance(year, int):
            raise ValueError("season boundary year differs")
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
        seasons.append(_StaticSeason(year=year, first=first, last=last))
    return _StaticQueryInputs(
        seasons=tuple(seasons),
        schedule_grain=_source_identity_text(
            schedule_contract, "grain", "schedule grain"
        ),
        schedule_parser_version=_source_identity_text(
            schedule_contract, "parser_version", "schedule parser version"
        ),
        statcast_grain=_source_identity_text(
            statcast_contract, "grain", "Statcast grain"
        ),
        statcast_parser_version=_source_identity_text(
            statcast_contract, "parser_version", "Statcast parser version"
        ),
        run_spec_sha256=run_sha,
        acquisition_contract_sha256=contract_sha,
    )


def build_static_batch(
    run_spec_path: Path,
    acquisition_contract_path: Path,
) -> QueryBatch:
    inputs = _validated_static_query_inputs(run_spec_path, acquisition_contract_path)

    queries: list[LogicalQuery] = []
    for season in inputs.seasons:
        queries.append(
            _logical_query(
                source="mlb_schedule_game_feed",
                grain=inputs.schedule_grain,
                endpoint=_SCHEDULE_ENDPOINT,
                parameters={
                    "sportId": 1,
                    "gameType": "R",
                    "startDate": season.first.isoformat(),
                    "endDate": season.last.isoformat(),
                    "hydrate": "probablePitcher,lineups,venue,linescore",
                },
                requested_range={
                    "year": season.year,
                    "start_date": season.first.isoformat(),
                    "end_date": season.last.isoformat(),
                },
                expected_media_type="application/json",
                parser_version=inputs.schedule_parser_version,
                run_spec_sha256=inputs.run_spec_sha256,
                acquisition_contract_sha256=inputs.acquisition_contract_sha256,
            )
        )
    return _make_batch("STATIC_SCHEDULES", None, queries)


def _build_statcast_root_batch(
    run_spec_path: Path,
    acquisition_contract_path: Path,
) -> QueryBatch:
    inputs = _validated_static_query_inputs(run_spec_path, acquisition_contract_path)

    queries: list[LogicalQuery] = []
    for season in inputs.seasons:
        for chunk_start, chunk_end in monthly_ranges(season.first, season.last):
            start_text = chunk_start.isoformat()
            end_text = chunk_end.isoformat()
            queries.append(
                _logical_query(
                    source="baseball_savant_statcast",
                    grain=inputs.statcast_grain,
                    endpoint=_STATCAST_ENDPOINT,
                    parameters=_statcast_parameters(start_text, end_text),
                    requested_range={
                        "year": season.year,
                        "start_date": start_text,
                        "end_date": end_text,
                    },
                    expected_media_type="text/csv",
                    parser_version=inputs.statcast_parser_version,
                    run_spec_sha256=inputs.run_spec_sha256,
                    acquisition_contract_sha256=inputs.acquisition_contract_sha256,
                )
            )
    return _make_batch("REMAINING", None, queries)


def build_statcast_root_batch(repository_root: Path) -> QueryBatch:
    root = Path(repository_root)
    return _build_statcast_root_batch(
        root / _RUN_SPEC_RELATIVE,
        root / _ACQUISITION_CONTRACT_RELATIVE,
    )


def _canonical_mapping_sha256(value: Mapping[str, object], label: str) -> str:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a string-keyed mapping")
    return _sha256_bytes(_canonical_json_bytes(dict(value)))


def _validate_binding_hashes(
    run_spec_sha256: str, acquisition_contract_sha256: str
) -> None:
    _validate_sha256(run_spec_sha256, "run_spec_sha256")
    _validate_sha256(acquisition_contract_sha256, "acquisition_contract_sha256")


_SCHEDULE_INVENTORY_KEYS = frozenset(
    {
        "acquisition_contract",
        "anchors",
        "artifact_kind",
        "counts",
        "run_id",
        "run_spec",
        "schema_version",
        "season_sources",
        "static_batch",
        "team_ids",
        "venue_ids",
    }
)
_SCHEDULE_ANCHOR_KEYS = frozenset(
    {
        "alternate_site",
        "away_score",
        "away_team_id",
        "completed_innings",
        "doubleheader_order",
        "game_pk",
        "home_score",
        "home_team_id",
        "neutral_site",
        "official_date",
        "played",
        "scheduled_innings",
        "scheduled_start_utc",
        "season",
        "season_start_date",
        "terminal_status",
        "venue_id",
        "winner_team_id",
    }
)
_SCHEDULE_SOURCE_KEYS = frozenset(
    {
        "game_count",
        "inclusive_end",
        "inclusive_start",
        "played_game_count",
        "query_id",
        "query_sidecar",
        "raw_sha256",
        "season",
        "storage_mode",
        "success_receipt",
        "unplayed_game_count",
    }
)


def _inventory_binding(
    raw: object,
    label: str,
    *,
    expected_path: str | None = None,
    expected_sha256: str | None = None,
) -> tuple[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != {"path", "sha256"}:
        raise ValueError(f"schedule inventory {label} binding differs")
    path = raw.get("path")
    digest = raw.get("sha256")
    if (
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or any(part in {"", ".", ".."} for part in Path(path).parts)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or (expected_path is not None and path != expected_path)
        or (expected_sha256 is not None and digest != expected_sha256)
    ):
        raise ValueError(f"schedule inventory {label} binding differs")
    return path, digest


def _validated_schedule_inventory_anchors(
    anchor_inventory: Mapping[str, object],
    *,
    inventory_sha256: str,
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> list[Mapping[str, object]]:
    _validate_sha256(inventory_sha256, "schedule inventory SHA-256")
    if (
        _sha256_bytes(_canonical_json_bytes(anchor_inventory) + b"\n")
        != inventory_sha256
    ):
        raise ValueError("schedule inventory SHA-256 differs from canonical bytes")
    if set(anchor_inventory) != _SCHEDULE_INVENTORY_KEYS:
        raise ValueError("schedule inventory top-level keys differ")
    if (
        anchor_inventory.get("artifact_kind") != "SCHEDULE_ANCHOR_INVENTORY"
        or anchor_inventory.get("schema_version") != 1
        or anchor_inventory.get("run_id") != "historical_walkforward_500"
    ):
        raise ValueError("schedule inventory identity differs")
    _inventory_binding(
        anchor_inventory.get("run_spec"),
        "RunSpec",
        expected_path=_RUN_SPEC_RELATIVE.as_posix(),
        expected_sha256=run_spec_sha256,
    )
    _inventory_binding(
        anchor_inventory.get("acquisition_contract"),
        "acquisition contract",
        expected_path=_ACQUISITION_CONTRACT_RELATIVE.as_posix(),
        expected_sha256=acquisition_contract_sha256,
    )
    static_batch = anchor_inventory.get("static_batch")
    if not isinstance(static_batch, Mapping) or set(static_batch) != {
        "batch_id",
        "path",
        "sha256",
    }:
        raise ValueError("schedule inventory static batch binding differs")
    static_batch_id = _validate_sha256(
        static_batch.get("batch_id"), "schedule inventory static batch ID"
    )
    _inventory_binding(
        {"path": static_batch.get("path"), "sha256": static_batch.get("sha256")},
        "static batch",
        expected_path=(
            _ACQUISITION_BATCH_ROOT / f"{static_batch_id}.json"
        ).as_posix(),
    )

    raw_anchors = anchor_inventory.get("anchors")
    if (
        not isinstance(raw_anchors, Sequence)
        or isinstance(raw_anchors, (str, bytes))
        or not raw_anchors
    ):
        raise ValueError("schedule inventory anchors must be nonempty")
    anchors: list[Mapping[str, object]] = []
    prior: tuple[int, int] | None = None
    game_pks: set[int] = set()
    teams: set[int] = set()
    venues: set[int] = set()
    by_season: dict[int, list[Mapping[str, object]]] = {
        season: [] for season in range(2018, 2025)
    }
    for raw in raw_anchors:
        if not isinstance(raw, Mapping) or set(raw) != _SCHEDULE_ANCHOR_KEYS:
            raise ValueError("schedule inventory anchor keys differ")
        season = raw.get("season")
        game_pk = raw.get("game_pk")
        venue_id = raw.get("venue_id")
        away_team_id = raw.get("away_team_id")
        home_team_id = raw.get("home_team_id")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (
                season,
                game_pk,
                venue_id,
                away_team_id,
                home_team_id,
            )
        ):
            raise ValueError("schedule inventory anchor IDs differ")
        if season not in SEASON_BOUNDARIES:
            raise ValueError("schedule inventory anchor season differs")
        if type(raw.get("played")) is not bool:
            raise ValueError("schedule inventory anchor played differs")
        official = _iso_date(
            raw.get("official_date"), "schedule inventory official_date"
        )
        season_start = _iso_date(
            raw.get("season_start_date"), "schedule inventory season_start_date"
        )
        first, last = SEASON_BOUNDARIES[season]
        if season_start != first or not first <= official <= last:
            raise ValueError("schedule inventory anchor dates differ from season")
        identity = (season, game_pk)
        if prior is not None and identity <= prior:
            raise ValueError("schedule inventory anchors are not strictly ordered")
        if game_pk in game_pks:
            raise ValueError("schedule inventory contains duplicate game_pk")
        prior = identity
        game_pks.add(game_pk)
        teams.update((away_team_id, home_team_id))
        venues.add(venue_id)
        anchors.append(raw)
        by_season[season].append(raw)

    inventory_team_ids = anchor_inventory.get("team_ids")
    inventory_venue_ids = anchor_inventory.get("venue_ids")
    if (
        not isinstance(inventory_team_ids, Sequence)
        or isinstance(inventory_team_ids, (str, bytes))
        or list(inventory_team_ids) != sorted(teams)
    ):
        raise ValueError("schedule inventory team IDs differ from anchors")
    if (
        not isinstance(inventory_venue_ids, Sequence)
        or isinstance(inventory_venue_ids, (str, bytes))
        or list(inventory_venue_ids) != sorted(venues)
    ):
        raise ValueError("schedule inventory venue IDs differ from anchors")
    played_count = sum(anchor.get("played") is True for anchor in anchors)
    if anchor_inventory.get("counts") != {
        "game_count": len(anchors),
        "played_game_count": played_count,
        "season_count": 7,
        "unplayed_game_count": len(anchors) - played_count,
    }:
        raise ValueError("schedule inventory counts differ from anchors")
    sources = anchor_inventory.get("season_sources")
    if (
        not isinstance(sources, Sequence)
        or isinstance(sources, (str, bytes))
        or len(sources) != 7
    ):
        raise ValueError("schedule inventory season sources differ")
    for season, source in zip(range(2018, 2025), sources, strict=True):
        if not isinstance(source, Mapping) or set(source) != _SCHEDULE_SOURCE_KEYS:
            raise ValueError("schedule inventory season source keys differ")
        rows = by_season[season]
        played = sum(row.get("played") is True for row in rows)
        first, last = SEASON_BOUNDARIES[season]
        if (
            not rows
            or source.get("season") != season
            or source.get("inclusive_start") != first.isoformat()
            or source.get("inclusive_end") != last.isoformat()
            or source.get("game_count") != len(rows)
            or source.get("played_game_count") != played
            or source.get("unplayed_game_count") != len(rows) - played
            or source.get("storage_mode")
            not in {"FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"}
        ):
            raise ValueError("schedule inventory season source differs")
        _validate_sha256(source.get("query_id"), "schedule source query ID")
        _validate_sha256(source.get("raw_sha256"), "schedule source raw SHA-256")
        _inventory_binding(source.get("query_sidecar"), "query sidecar")
        _inventory_binding(source.get("success_receipt"), "success receipt")
    return anchors


def _build_schedule_reference_batch_from_inventory(
    anchor_inventory: Mapping[str, object],
    *,
    inventory_sha256: str,
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> QueryBatch:
    _validate_binding_hashes(run_spec_sha256, acquisition_contract_sha256)
    anchors = _validated_schedule_inventory_anchors(
        anchor_inventory,
        inventory_sha256=inventory_sha256,
        run_spec_sha256=run_spec_sha256,
        acquisition_contract_sha256=acquisition_contract_sha256,
    )

    unique_games: dict[int, tuple[int, date, date, int, bool]] = {}
    seasons: set[int] = set()
    venues: set[int] = set()
    pitching_consumers: dict[tuple[int, date, date], set[int]] = {}
    for raw in anchors:
        if not isinstance(raw, Mapping):
            raise ValueError("schedule anchor must be an object")
        season = raw.get("season")
        game_pk = raw.get("game_pk")
        venue_id = raw.get("venue_id")
        played = raw.get("played")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (season, game_pk, venue_id)
        ):
            raise ValueError("schedule anchor IDs must be positive integers")
        if season not in range(2018, 2025):
            raise ValueError("schedule anchor season is outside 2018-2024")
        if type(played) is not bool:
            raise ValueError("schedule anchor played must be explicitly boolean")
        official = _iso_date(raw.get("official_date"), "anchor official_date")
        season_start = _iso_date(
            raw.get("season_start_date"), "anchor season_start_date"
        )
        frozen_start, frozen_end = SEASON_BOUNDARIES[season]
        if season_start != frozen_start or not frozen_start <= official <= frozen_end:
            raise ValueError("schedule anchor dates differ from season")
        identity = (season, season_start, official, venue_id, played)
        if game_pk in unique_games:
            raise ValueError("schedule inventory contains duplicate game_pk")
        unique_games[game_pk] = identity
        seasons.add(season)
        venues.add(venue_id)
        if played:
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
                "reason": _PRESEASON_DISPOSITION_REASON,
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
                "response_completeness": {
                    "limit": 5000,
                    "reject_total_splits_at_or_above_limit": True,
                },
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
                parameters={
                    "sportId": 1,
                    "season": season,
                    "hydrate": "venue(location)",
                },
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
    return _make_batch("REFERENCE_INPUTS", inventory_sha256, queries)


def build_schedule_reference_batch(
    repository_root: Path,
    run_spec_path: Path,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
    read_only_progress: bool = False,
) -> QueryBatch:
    repository_root = Path(os.path.abspath(os.fspath(repository_root)))
    run_spec_path = Path(os.path.abspath(os.fspath(run_spec_path)))
    authority_root, _ = _authority_branch(
        run_spec_path, acquisition_contract_path
    )
    if authority_root != repository_root:
        raise ValueError("reference RunSpec path differs from repository root")
    from .protocols import load_run_spec
    from .v52_schedule_inventory import load_verified_schedule_anchor_inventory

    verified = load_verified_schedule_anchor_inventory(
        repository_root,
        load_run_spec(run_spec_path),
        acquisition_contract_path=acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    run_binding = verified.payload.get("run_spec")
    contract_binding = verified.payload.get("acquisition_contract")
    if not isinstance(run_binding, Mapping) or not isinstance(
        contract_binding, Mapping
    ):
        raise ValueError("verified schedule inventory authority bindings differ")
    return _build_schedule_reference_batch_from_inventory(
        verified.payload,
        inventory_sha256=verified.sha256,
        run_spec_sha256=_validate_sha256(
            run_binding.get("sha256"), "verified schedule RunSpec SHA-256"
        ),
        acquisition_contract_sha256=_validate_sha256(
            contract_binding.get("sha256"),
            "verified schedule acquisition contract SHA-256",
        ),
    )


def _load_fixed_reference_batch_authority(
    repository_root: Path,
    *,
    read_only_progress: bool = False,
) -> QueryBatch:
    repository_root = Path(os.path.abspath(os.fspath(repository_root)))
    from .protocols import parse_run_spec
    from .v52_schedule_inventory import (
        _read_inventory_contents,
        load_verified_schedule_anchor_inventory,
    )

    run_contents = _read_repository_file(
        repository_root, _RUN_SPEC_RELATIVE, "reference RunSpec"
    )
    run_spec = parse_run_spec(_parse_json_object(run_contents, "reference RunSpec"))
    namespaces = run_spec.raw.get("namespaces")
    raw_namespace = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(raw_namespace, str):
        raise ValueError("reference RunSpec raw namespace differs")
    raw_relative = Path(raw_namespace)
    if (
        raw_relative.is_absolute()
        or not raw_relative.parts
        or any(part in {"", ".", ".."} for part in raw_relative.parts)
    ):
        raise ValueError("reference RunSpec raw namespace differs")
    inventory_record = _parse_json_object(
        _read_inventory_contents(repository_root),
        "schedule anchor inventory",
        canonical=True,
    )
    static_binding = inventory_record.get("static_batch")
    if not isinstance(static_binding, dict):
        raise ValueError("schedule inventory static batch binding differs")
    static_batch_id = _validate_sha256(
        static_binding.get("batch_id"), "schedule inventory static batch ID"
    )
    verified = load_verified_schedule_anchor_inventory(
        repository_root,
        run_spec,
        acquisition_contract_path=repository_root
        / _ACQUISITION_CONTRACT_RELATIVE,
        static_batch_id=static_batch_id,
        evidence_root=repository_root / raw_relative,
        read_only_progress=read_only_progress,
    )
    run_binding = verified.payload.get("run_spec")
    contract_binding = verified.payload.get("acquisition_contract")
    if not isinstance(run_binding, Mapping) or not isinstance(
        contract_binding, Mapping
    ):
        raise ValueError("verified schedule inventory authority bindings differ")
    return _build_schedule_reference_batch_from_inventory(
        verified.payload,
        inventory_sha256=verified.sha256,
        run_spec_sha256=_validate_sha256(
            run_binding.get("sha256"), "verified schedule RunSpec SHA-256"
        ),
        acquisition_contract_sha256=_validate_sha256(
            contract_binding.get("sha256"),
            "verified schedule acquisition contract SHA-256",
        ),
    )


def _validate_reference_batch_authority(
    repository_root: Path,
    batch: QueryBatch,
    *,
    read_only_progress: bool = False,
) -> None:
    if batch.stage != "REFERENCE_INPUTS":
        return
    expected = _load_fixed_reference_batch_authority(
        repository_root,
        read_only_progress=read_only_progress,
    )
    if batch != expected:
        raise ValueError(
            "REFERENCE_INPUTS differs from fixed schedule inventory replay"
        )


def _build_participant_derived_batch_from_inventory(
    game_evidence_inventory: Mapping[str, object],
    *,
    inventory_sha256: str,
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> QueryBatch:
    _validate_binding_hashes(run_spec_sha256, acquisition_contract_sha256)
    derived = _validate_sha256(inventory_sha256, "game evidence inventory SHA-256")
    games = game_evidence_inventory.get("games")
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        raise ValueError("game_evidence_inventory.games must be an array")
    pitcher_consumers: dict[int, dict[int, date]] = {}
    seen_game_pks: set[int] = set()
    for game in games:
        if not isinstance(game, Mapping):
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
        played = game.get("played")
        if type(played) is not bool:
            raise ValueError("game evidence played must be explicitly boolean")
        if not played:
            continue
        if (
            game.get("verification_status") != "VERIFIED"
            or game.get("participant_provenance") != "RECONSTRUCTED_FINAL_PARTICIPANTS"
        ):
            continue
        starters = game.get("actual_starter_ids")
        if (
            not isinstance(starters, Sequence)
            or isinstance(starters, (str, bytes))
            or len(starters) != 2
        ):
            raise ValueError("verified game must contain two actual starter IDs")
        for pitcher_id in starters:
            if (
                isinstance(pitcher_id, bool)
                or not isinstance(pitcher_id, int)
                or pitcher_id <= 0
            ):
                raise ValueError("actual starter IDs must be positive integers")
            pitcher_consumers.setdefault(pitcher_id, {})[game_pk] = (
                official_date - timedelta(days=1)
            )
        if starters[0] == starters[1]:
            raise ValueError("verified game actual starter IDs must be distinct")

    queries = []
    for pitcher_id in sorted(pitcher_consumers):
        grouped: dict[str, dict[int, date]] = {
            "2017-12-31": {},
            "2021-12-31": {},
        }
        for game_pk, cutoff in pitcher_consumers[pitcher_id].items():
            baseline = "2017-12-31" if cutoff <= date(2021, 12, 31) else "2021-12-31"
            grouped[baseline][game_pk] = cutoff
        for inclusive_end, consumers in grouped.items():
            if not consumers:
                continue
            queries.append(
                _logical_query(
                    source="mlb_stats_pitcher_bf",
                    grain="player_career_batters_faced_reference",
                    endpoint=f"{_MLB_BASE}/people/{pitcher_id}/stats",
                    parameters={
                        "stats": "byDateRange",
                        "group": "pitching",
                        "gameType": "R",
                        "sportIds": 1,
                        "startDate": "1876-01-01",
                        "endDate": inclusive_end,
                    },
                    requested_range={
                        "consumer_cutoff_dates": {
                            str(game_pk): consumers[game_pk].isoformat()
                            for game_pk in sorted(consumers)
                        },
                        "consuming_game_pks": sorted(consumers),
                        "pitcher_id": pitcher_id,
                        "inclusive_end": inclusive_end,
                    },
                    expected_media_type="application/json",
                    parser_version="v5.2.1",
                    run_spec_sha256=run_spec_sha256,
                    acquisition_contract_sha256=acquisition_contract_sha256,
                )
            )
    return _make_batch("PARTICIPANT_DERIVED", derived, queries)


def build_participant_derived_batch(
    repository_root: Path,
    *,
    read_only_progress: bool = False,
) -> QueryBatch:
    from .v52_game_evidence_inventory import (
        load_verified_game_evidence_inventory,
    )

    root = Path(repository_root)
    verified = load_verified_game_evidence_inventory(
        root,
        read_only_progress=read_only_progress,
    )
    inventory_contents = _read_repository_file(
        root,
        _GAME_EVIDENCE_INVENTORY_RELATIVE,
        "game evidence inventory",
    )
    inventory_sha256 = _sha256_bytes(inventory_contents)
    if inventory_sha256 != verified.sha256:
        raise ValueError("game evidence inventory changed after verified load")
    run_binding = verified.payload.get("run_spec")
    contract_binding = verified.payload.get("acquisition_contract")
    if not isinstance(run_binding, Mapping) or not isinstance(
        contract_binding, Mapping
    ):
        raise ValueError("verified game inventory authority bindings differ")
    return _build_participant_derived_batch_from_inventory(
        verified.payload,
        inventory_sha256=inventory_sha256,
        run_spec_sha256=_validate_sha256(
            run_binding.get("sha256"), "verified game inventory RunSpec SHA-256"
        ),
        acquisition_contract_sha256=_validate_sha256(
            contract_binding.get("sha256"),
            "verified game inventory acquisition contract SHA-256",
        ),
    )


def _validate_participant_batch_authority(
    repository_root: Path,
    batch: QueryBatch,
    *,
    read_only_progress: bool = False,
) -> None:
    if batch.stage != "PARTICIPANT_DERIVED":
        return
    expected = build_participant_derived_batch(
        repository_root,
        read_only_progress=read_only_progress,
    )
    if batch != expected:
        raise ValueError("PARTICIPANT_DERIVED differs from fixed game inventory replay")


def _is_statcast_root_batch(batch: QueryBatch) -> bool:
    return (
        batch.stage == "REMAINING"
        and batch.derived_from_sha256 is None
        and len(batch.queries) == 46
        and all(query.parent_query_id is None for query in batch.queries)
    )


def _validate_statcast_root_batch_authority(
    repository_root: Path, batch: QueryBatch
) -> None:
    if not _is_statcast_root_batch(batch):
        return
    expected = build_statcast_root_batch(repository_root)
    if batch != expected:
        raise ValueError("REMAINING root differs from fixed Statcast authority replay")


def _is_legacy_partition_batch_candidate(
    repository_root: Path, batch: QueryBatch
) -> bool:
    if batch.stage != "REMAINING" or not batch.queries:
        return False
    if any(
        query.requested_range.get("acquisition")
        not in {"FETCH_REQUIRED", "PROTECTED_LEGACY_REFERENCE"}
        for query in batch.queries
    ):
        return False
    root_ids = {
        logical_query_id(parent)
        for parent in build_statcast_root_batch(repository_root).queries
    }
    direct_root_ids = {
        query.parent_query_id
        for query in batch.queries
        if query.parent_query_id in root_ids
    }
    return len(direct_root_ids) >= 1


def _validate_legacy_partition_batch_authority(
    repository_root: Path,
    batch: QueryBatch,
    *,
    read_only_progress: bool = False,
) -> None:
    if not _is_legacy_partition_batch_candidate(repository_root, batch):
        return
    expected = build_legacy_partition_batch(
        repository_root,
        read_only_progress=read_only_progress,
    )
    if batch != expected:
        raise ValueError("REMAINING legacy partition differs from decision replay")


def _validate_query_batch_authority(
    repository_root: Path,
    batch: QueryBatch,
    *,
    read_only_progress: bool = False,
) -> None:
    _validate_reference_batch_authority(
        repository_root,
        batch,
        read_only_progress=read_only_progress,
    )
    _validate_participant_batch_authority(
        repository_root,
        batch,
        read_only_progress=read_only_progress,
    )
    _validate_statcast_root_batch_authority(repository_root, batch)
    _validate_legacy_partition_batch_authority(
        repository_root,
        batch,
        read_only_progress=read_only_progress,
    )


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


def _validate_statcast_split_parent_batch(
    repository_root: Path,
    parent_batch: QueryBatch,
    *,
    read_only_progress: bool = False,
) -> None:
    if _is_statcast_root_batch(parent_batch) or _is_legacy_partition_batch_candidate(
        repository_root, parent_batch
    ):
        return
    ancestor_batch_id = _validate_sha256(
        parent_batch.derived_from_sha256,
        "Statcast split parent batch derived_from_sha256",
    )
    parent_query_ids = {query.parent_query_id for query in parent_batch.queries}
    if len(parent_query_ids) != 1 or None in parent_query_ids:
        raise ValueError("Statcast split parent batch has noncanonical parent lineage")
    ancestor = load_query_batch(
        repository_root,
        ancestor_batch_id,
        read_only_progress=read_only_progress,
    )
    parent_query_id = next(iter(parent_query_ids))
    matches = [
        query
        for query in ancestor.queries
        if logical_query_id(query) == parent_query_id
    ]
    if len(matches) != 1:
        raise ValueError("Statcast split parent batch has noncanonical parent lineage")
    if parent_batch != build_statcast_split_batch(
        repository_root,
        ancestor,
        matches[0],
        read_only_progress=read_only_progress,
    ):
        raise ValueError("Statcast split parent batch differs from canonical bisection")


def build_statcast_split_batch(
    repository_root: Path,
    parent_batch: QueryBatch,
    parent_query: LogicalQuery,
    *,
    read_only_progress: bool = False,
) -> QueryBatch:
    """Build the canonical sealed child batch for one Statcast parent query."""
    if not isinstance(parent_batch, QueryBatch):
        raise TypeError("parent_batch must be a QueryBatch")
    if not isinstance(parent_query, LogicalQuery):
        raise TypeError("parent_query must be a LogicalQuery")
    if parent_batch.stage != "REMAINING":
        raise ValueError("Statcast split parent batch must use REMAINING stage")

    payload = _batch_payload(
        parent_batch.stage,
        parent_batch.derived_from_sha256,
        parent_batch.queries,
    )
    if parent_batch.batch_id != _sha256_bytes(_canonical_json_bytes(payload)):
        raise ValueError("parent batch_id differs from its canonical sealed payload")
    root = Path(repository_root)
    _validate_query_batch_authority(
        root,
        parent_batch,
        read_only_progress=read_only_progress,
    )
    _validate_statcast_split_parent_batch(
        root,
        parent_batch,
        read_only_progress=read_only_progress,
    )

    requested_parent_id = logical_query_id(parent_query)
    matches = [
        query
        for query in parent_batch.queries
        if logical_query_id(query) == requested_parent_id
    ]
    if len(matches) != 1 or matches[0] != parent_query:
        raise ValueError("Statcast split parent query must occur exactly once in its batch")

    return _make_batch(
        "REMAINING",
        parent_batch.batch_id,
        bisect_statcast_query(parent_query),
    )


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


def _build_legacy_partition_batch_from_catalog(
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
    candidates_by_parent: list[list[tuple[date, date]]] = [[] for _ in parent_ranges]
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
    for (first, last, parent), candidates in zip(parent_ranges, candidates_by_parent):
        exact = [
            (start, end) for start, end in candidates if start == first and end == last
        ]
        strict = [
            (start, end) for start, end in candidates if (start, end) != (first, last)
        ]
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
            promoted_range["protected_inventory_sha256"] = protected_inventory_sha256
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
    return _make_batch("REMAINING", protected_inventory_sha256, queries)


def build_legacy_partition_batch_from_catalog(
    root_batch: QueryBatch,
    protected_catalog: Sequence[Mapping[str, object]],
    *,
    protected_inventory_sha256: str,
) -> QueryBatch:
    """Build the pure first-level legacy partition without publishing files."""
    if not isinstance(root_batch, QueryBatch) or not _is_statcast_root_batch(
        root_batch
    ):
        raise ValueError("root_batch must be the canonical Statcast root batch")
    return _build_legacy_partition_batch_from_catalog(
        root_batch.queries,
        protected_catalog,
        protected_inventory_sha256=protected_inventory_sha256,
    )


def build_legacy_partition_batch(
    repository_root: Path,
    *,
    read_only_progress: bool = False,
) -> QueryBatch:
    """Replay the sealed legacy decisions into the fixed first-level partition."""
    from .v52_legacy_decision_inventory import (
        _load_legacy_partition_decision_replay,
    )

    root = Path(repository_root)
    decision_sha256, protected_sha256, decisions = (
        _load_legacy_partition_decision_replay(root)
    )
    expected_root = build_statcast_root_batch(root)
    root_batch = _load_query_batch(
        root,
        expected_root.batch_id,
        invoke_authority_hook=False,
        read_only_progress=read_only_progress,
    )
    if root_batch != expected_root or len(root_batch.queries) != 46:
        raise ValueError("legacy partition Statcast root authority differs")
    if len(decisions) != 48:
        raise ValueError("legacy partition requires exactly 48 sealed decisions")

    parents = {
        logical_query_id(parent): (parent, *_canonical_statcast_parent_range(parent))
        for parent in root_batch.queries
    }
    if len(parents) != 46:
        raise ValueError("legacy partition root query IDs are not unique")
    by_parent: dict[str, list[tuple[date, date]]] = {
        parent_id: [] for parent_id in parents
    }
    queries: list[LogicalQuery] = []
    disposition_counts = {"PROMOTED": 0, "FETCH_REQUIRED": 0}
    for decision in decisions:
        parent_id = decision.get("parent_query_id")
        candidate_id = decision.get("candidate_query_id")
        decision_range = decision.get("range")
        disposition = decision.get("disposition")
        if (
            not isinstance(parent_id, str)
            or parent_id not in parents
            or not isinstance(candidate_id, str)
            or not isinstance(decision_range, Mapping)
            or disposition not in disposition_counts
        ):
            raise ValueError("legacy partition decision binding differs")
        year = decision_range.get("year")
        first = _iso_date(decision_range.get("start_date"), "decision start")
        last = _iso_date(decision_range.get("end_date"), "decision end")
        parent, parent_first, parent_last = parents[parent_id]
        if (
            isinstance(year, bool)
            or not isinstance(year, int)
            or year != first.year
            or first > last
            or not parent_first <= first <= last <= parent_last
        ):
            raise ValueError("legacy partition decision range differs")
        acquisition = (
            "PROTECTED_LEGACY_REFERENCE"
            if disposition == "PROMOTED"
            else "FETCH_REQUIRED"
        )
        query = _statcast_child(parent, first, last, acquisition=acquisition)
        if disposition == "PROMOTED":
            requested = dict(query.requested_range)
            requested["protected_inventory_sha256"] = protected_sha256
            query = replace(query, requested_range=requested)
        if logical_query_id(query) != candidate_id:
            raise ValueError("legacy partition candidate query ID differs")
        queries.append(query)
        by_parent[parent_id].append((first, last))
        disposition_counts[disposition] += 1

    if disposition_counts != {"PROMOTED": 1, "FETCH_REQUIRED": 47}:
        raise ValueError("legacy partition dispositions differ")
    for parent_id, (parent, parent_first, parent_last) in parents.items():
        ranges = by_parent[parent_id]
        if not ranges or ranges[0][0] != parent_first or ranges[-1][1] != parent_last:
            raise ValueError("legacy partition does not cover every root parent")
        if any(
            current[0] != previous[1] + timedelta(days=1)
            for previous, current in zip(ranges, ranges[1:])
        ):
            raise ValueError("legacy partition root coverage is not contiguous")
        if logical_query_id(parent) != parent_id:
            raise ValueError("legacy partition root parent identity differs")

    batch = _make_batch("REMAINING", decision_sha256, queries)
    if tuple(logical_query_id(query) for query in batch.queries) != tuple(
        decision["candidate_query_id"] for decision in decisions
    ):
        raise ValueError("legacy partition decision replay order differs")
    return batch


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


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
                    or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
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
            raise ValueError(
                "query batch ancestors must be no-follow directories"
            ) from exc
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("query batch ancestor must be a directory")
    return descriptor


def _read_existing(parent_fd: int, name: str) -> bytes | None:
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
        if _batch_file_identity(before) != _batch_file_identity(
            after
        ) or _batch_file_identity(after) != _batch_file_identity(rebound):
            raise ValueError(f"{label} identity changed while reading")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _open_existing_directory(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(
            "query batch ancestors must be existing no-follow directories"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("query batch ancestor identity differs")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_opened_batch_chain(root: Path, descriptors: Sequence[int]) -> None:
    expected_components = _ACQUISITION_BATCH_ROOT.parts
    if len(descriptors) != len(expected_components) + 1:
        raise ValueError("query batch ancestor chain differs")
    fresh_root = _open_root(root)
    try:
        opened_root = os.fstat(descriptors[0])
        rebound_root = os.fstat(fresh_root)
        if (opened_root.st_dev, opened_root.st_ino) != (
            rebound_root.st_dev,
            rebound_root.st_ino,
        ):
            raise ValueError("query batch root identity changed")
    finally:
        os.close(fresh_root)
    for index, component in enumerate(expected_components):
        try:
            opened = os.fstat(descriptors[index + 1])
            linked = os.stat(
                component,
                dir_fd=descriptors[index],
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("query batch ancestor identity changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("query batch ancestor identity changed")


def _load_query_batch(
    root: Path,
    batch_id: str,
    *,
    invoke_authority_hook: bool,
    read_only_progress: bool = False,
) -> QueryBatch:
    if type(invoke_authority_hook) is not bool:
        raise TypeError("invoke_authority_hook must be a bool")
    root = Path(root)
    requested_id = _validate_sha256(batch_id, "batch_id")
    descriptors = [_open_root(root)]
    try:
        current = descriptors[0]
        for component in _ACQUISITION_BATCH_ROOT.parts:
            current = _open_existing_directory(current, component)
            descriptors.append(current)
        loaded = _read_linked_regular(current, f"{requested_id}.json", "query batch")
        if loaded is None:
            raise ValueError("query batch is missing")
        contents, metadata = loaded
        if metadata.st_nlink != 1:
            raise ValueError("query batch must be an unaliased regular file")
        record = _parse_json_object(contents, "query batch", canonical=True)
        if set(record) != {
            "batch_id",
            "derived_from_sha256",
            "queries",
            "schema_version",
            "stage",
        }:
            raise ValueError("query batch keys differ")
        if record.get("schema_version") != 1:
            raise ValueError("query batch schema version differs")
        embedded_id = _validate_sha256(record.get("batch_id"), "embedded batch_id")
        if embedded_id != requested_id:
            raise ValueError("query batch requested and embedded IDs differ")
        raw_queries = record.get("queries")
        if not isinstance(raw_queries, list):
            raise ValueError("query batch queries must be an array")
        queries = []
        for raw in raw_queries:
            if not isinstance(raw, dict):
                raise ValueError("query batch query must be an object")
            try:
                query = LogicalQuery(**raw)
            except TypeError as exc:
                raise ValueError("query batch query keys differ") from exc
            if _query_record(query) != raw:
                raise ValueError("query batch query record differs")
            queries.append(query)
        batch = _make_batch(
            record.get("stage"),  # type: ignore[arg-type]
            record.get("derived_from_sha256"),  # type: ignore[arg-type]
            queries,
        )
        expected = {
            "batch_id": batch.batch_id,
            "derived_from_sha256": batch.derived_from_sha256,
            "queries": [_query_record(query) for query in batch.queries],
            "schema_version": 1,
            "stage": batch.stage,
        }
        if batch.batch_id != requested_id or record != expected:
            raise ValueError("query batch canonical ID binding differs")
        _validate_query_batch_authority(
            root,
            batch,
            read_only_progress=read_only_progress,
        )
        rebound = _read_linked_regular(current, f"{requested_id}.json", "query batch")
        if rebound is None:
            raise ValueError("query batch is missing after reconstruction")
        rebound_contents, rebound_metadata = rebound
        if rebound_contents != contents or _batch_file_identity(
            rebound_metadata
        ) != _batch_file_identity(metadata):
            raise ValueError("query batch identity or bytes changed during load")
        _verify_opened_batch_chain(root, descriptors)
        if invoke_authority_hook and (
            batch.stage == "PARTICIPANT_DERIVED"
            or _is_statcast_root_batch(batch)
            or _is_legacy_partition_batch_candidate(root, batch)
        ):
            _rebind_query_batch_authority(
                root,
                batch,
                read_only_progress=read_only_progress,
            )
        return batch
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_query_batch(
    repository_root: Path,
    batch_id: str,
    *,
    read_only_progress: bool = False,
) -> QueryBatch:
    return _load_query_batch(
        repository_root,
        batch_id,
        invoke_authority_hook=True,
        read_only_progress=read_only_progress,
    )


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

    stale_name = re.compile(rf"^\.{re.escape(batch_id)}\.[0-9a-f]{{32}}\.tmp$")
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
        if (candidate_metadata.st_dev, candidate_metadata.st_ino) != (
            final_metadata.st_dev,
            final_metadata.st_ino,
        ):
            continue
        if candidate_contents != expected_contents:
            raise ValueError("query batch stale temp bytes differ")
        rebound = os.stat(candidate_name, dir_fd=parent_fd, follow_symlinks=False)
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
    private_name = re.compile(rf"^\.{re.escape(batch_id)}\.[0-9a-f]{{32}}\.tmp$")
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
            linked = os.stat(candidate_name, dir_fd=parent_fd, follow_symlinks=False)
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
            rebound = os.stat(candidate_name, dir_fd=parent_fd, follow_symlinks=False)
            if _batch_file_identity(opened) != _batch_file_identity(rebound):
                raise ValueError("query batch private temp identity changed")
            os.unlink(candidate_name, dir_fd=parent_fd)
            removed = True
        finally:
            os.close(descriptor)
    if removed:
        os.fsync(parent_fd)


def _before_query_batch_authority_rebind() -> None:
    """Deterministic authority-mutation test hook; production does nothing."""


def _rebind_query_batch_authority(
    root: Path,
    batch: QueryBatch,
    *,
    read_only_progress: bool = False,
) -> None:
    if (
        batch.stage
        not in {
            "REFERENCE_INPUTS",
            "PARTICIPANT_DERIVED",
        }
        and not _is_statcast_root_batch(batch)
        and not _is_legacy_partition_batch_candidate(root, batch)
    ):
        return
    _before_query_batch_authority_rebind()
    _validate_query_batch_authority(
        root,
        batch,
        read_only_progress=read_only_progress,
    )


def _cleanup_owned_query_batch_final(
    parent_fd: int,
    name: str,
    contents: bytes,
    identity: tuple[int, ...],
) -> None:
    loaded = _read_linked_regular(parent_fd, name, "owned query batch final")
    if loaded is None:
        return
    final_contents, metadata = loaded
    if final_contents != contents or _batch_file_identity(metadata) != identity:
        raise ValueError("query batch final ownership changed before cleanup")
    rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _batch_file_identity(rebound) != identity:
        raise ValueError("query batch final ownership changed before cleanup")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def publish_query_batch(root: Path, batch: QueryBatch) -> Path:
    if not isinstance(batch, QueryBatch):
        raise TypeError("batch must be a QueryBatch")
    root = Path(root)
    _validate_query_batch_authority(root, batch)
    payload = _batch_payload(batch.stage, batch.derived_from_sha256, batch.queries)
    expected_id = _sha256_bytes(_canonical_json_bytes(payload))
    if batch.batch_id != expected_id:
        raise ValueError("batch_id differs from canonical batch payload")
    record = {"schema_version": 1, "batch_id": batch.batch_id, **payload}
    contents = _canonical_json_bytes(record) + b"\n"
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
                _rebind_query_batch_authority(root, batch)
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
                    _rebind_query_batch_authority(root, batch)
                    return root / _ACQUISITION_BATCH_ROOT / name
                _unlink_owned_temp(current, temp_name, descriptor)
                temp_exists = False
                created_identity = _batch_file_identity(os.fstat(descriptor))
                os.fsync(current)
                try:
                    if (
                        _reconcile_batch_final(current, batch.batch_id, contents)
                        != contents
                    ):
                        raise ValueError("query batch bytes changed after publication")
                    _rebind_query_batch_authority(root, batch)
                    return root / _ACQUISITION_BATCH_ROOT / name
                except BaseException:
                    _cleanup_owned_query_batch_final(
                        current,
                        name,
                        contents,
                        created_identity,
                    )
                    raise
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


def _split_range(query: LogicalQuery) -> dict[str, object]:
    first, last = validate_statcast_query(query)
    return {
        "end_date": last.isoformat(),
        "start_date": first.isoformat(),
        "year": first.year,
    }


def _validate_split_timestamp(value: object) -> str:
    if not isinstance(value, str) or _SPLIT_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("split receipt timestamp differs")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("split receipt timestamp differs") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("split receipt timestamp differs")
    return value


def _verify_split_evidence_root(
    repository_root: Path,
    evidence_root: Path,
    parent: LogicalQuery | None = None,
) -> None:
    contents = _read_repository_file(
        repository_root,
        _RUN_SPEC_RELATIVE,
        "split RunSpec",
    )
    contract_contents = _read_repository_file(
        repository_root,
        _ACQUISITION_CONTRACT_RELATIVE,
        "split acquisition contract",
    )
    run_spec, run_spec_sha256, _, contract_sha256 = (
        _validate_run_bound_acquisition_contract(
            repository_root,
            contents,
            contract_contents,
        )
    )
    if parent is not None:
        if run_spec_sha256 != parent.run_spec_sha256:
            raise ValueError("split parent RunSpec hash binding differs")
        if contract_sha256 != parent.acquisition_contract_sha256:
            raise ValueError("split parent acquisition contract hash binding differs")
    namespaces = run_spec.get("namespaces")
    raw = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(raw, str):
        raise ValueError("split RunSpec raw namespace binding differs")
    raw_path = Path(raw)
    if (
        raw_path.is_absolute()
        or not raw_path.parts
        or any(component in {"", ".", ".."} for component in raw_path.parts)
    ):
        raise ValueError("split RunSpec raw namespace binding differs")
    expected = Path(os.path.abspath(os.fspath(Path(repository_root) / raw_path)))
    supplied = Path(os.path.abspath(os.fspath(evidence_root)))
    if supplied != expected:
        raise ValueError("split evidence root differs from the RunSpec raw namespace")


def _verified_split_attempt(
    evidence_root: Path,
    parent: LogicalQuery,
    reason: str,
    attempt_number: int,
    *,
    read_only_progress: bool = False,
) -> tuple[dict[str, object], str, str]:
    if reason not in _SPLIT_REASONS:
        raise ValueError("split reason is not approved")
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or not 1 <= attempt_number <= 999999
    ):
        raise ValueError("exhausted_attempt_number must be a positive integer")
    store = (
        V52EvidenceStore.open_read_only(evidence_root)
        if read_only_progress
        else V52EvidenceStore(evidence_root)
    )
    progress = store.read_verified_query_progress(parent)
    if not progress.attempt_receipts:
        raise ValueError("split parent has no exhausted attempt receipt")
    if progress.pending_attempt_numbers:
        raise ValueError("split attempt membership changed")
    latest = max(
        progress.attempt_receipts,
        key=lambda record: int(record["attempt_number"]),
    )
    if latest.get("attempt_number") != attempt_number:
        raise ValueError("split receipt must bind the latest attempt receipt")
    if latest.get("outcome") != "SPLIT_REQUIRED":
        raise ValueError("latest attempt is not SPLIT_REQUIRED")
    if latest.get("error_classification") != reason:
        raise ValueError("split reason differs from the latest attempt receipt")
    split_evidence = store.read_verified_split_attempt(parent, attempt_number)
    if (
        split_evidence.query_id != logical_query_id(parent)
        or split_evidence.attempt_number != attempt_number
        or split_evidence.outcome != "SPLIT_REQUIRED"
        or split_evidence.error_classification != reason
    ):
        raise ValueError("dedicated split evidence binding differs")
    if reason in {"SIZE_LIMIT", "ROW_CAP"} and (
        split_evidence.body_path is None
        or split_evidence.body_sha256 is None
        or split_evidence.byte_count <= 0
    ):
        raise ValueError(f"{reason} requires authenticated partial-body split evidence")
    if (split_evidence.body_path is None) != (
        split_evidence.body_sha256 is None
    ) or (split_evidence.body_path is None) != (split_evidence.byte_count == 0):
        raise ValueError("dedicated split evidence body binding differs")
    parent_query_id = logical_query_id(parent)
    relative = Path(
        "queries",
        parent.source,
        parent_query_id,
        "attempts",
        f"{attempt_number:06d}.json",
    )
    if split_evidence.attempt_receipt_path != evidence_root / relative:
        raise ValueError("dedicated split attempt receipt path differs")
    contents = _read_repository_file(
        evidence_root, relative, "split attempt receipt"
    )
    if (
        contents != _canonical_json_bytes(dict(latest)) + b"\n"
        or split_evidence.attempt_receipt_sha256 != _sha256_bytes(contents)
    ):
        raise ValueError("split attempt receipt bytes differ")
    return dict(latest), relative.as_posix(), _sha256_bytes(contents)


def _verified_split_lineage(
    repository_root: Path,
    evidence_root: Path,
    split_fd: int,
    *,
    parent_batch_id: str,
    parent_query_id: str,
    reason: str,
    exhausted_attempt_number: int,
    child_batch_id: str,
    active_parent_ids: set[str],
    read_only_progress: bool = False,
) -> tuple[
    QueryBatch,
    LogicalQuery,
    QueryBatch,
    dict[str, object],
    str,
    str,
    int,
]:
    requested_parent_batch_id = _validate_sha256(
        parent_batch_id, "parent_batch_id"
    )
    requested_parent_query_id = _validate_sha256(
        parent_query_id, "parent_query_id"
    )
    requested_child_batch_id = _validate_sha256(child_batch_id, "child_batch_id")
    parent_batch = load_query_batch(
        repository_root,
        requested_parent_batch_id,
        read_only_progress=read_only_progress,
    )
    child_batch = load_query_batch(
        repository_root,
        requested_child_batch_id,
        read_only_progress=read_only_progress,
    )
    matches = [
        query
        for query in parent_batch.queries
        if logical_query_id(query) == requested_parent_query_id
    ]
    if len(matches) != 1:
        raise ValueError("split parent query must occur exactly once in its batch")
    parent = matches[0]
    _verify_split_evidence_root(repository_root, evidence_root, parent)
    parent_first, parent_last = validate_statcast_query(parent)
    if parent_batch.stage != "REMAINING" or child_batch.stage != parent_batch.stage:
        raise ValueError("split parent and child batches must use REMAINING stage")
    if child_batch.derived_from_sha256 != parent_batch.batch_id:
        raise ValueError("split child batch must derive from the parent batch")
    if child_batch != build_statcast_split_batch(
        repository_root,
        parent_batch,
        parent,
        read_only_progress=read_only_progress,
    ):
        raise ValueError("split child batch differs from the canonical bisection")
    if len(child_batch.queries) < 2:
        raise ValueError("split child batch must contain at least two queries")
    cursor = parent_first
    for child in child_batch.queries:
        child_first, child_last = validate_statcast_query(child)
        if (
            child.parent_query_id != requested_parent_query_id
            or child.run_spec_sha256 != parent.run_spec_sha256
            or child.acquisition_contract_sha256
            != parent.acquisition_contract_sha256
        ):
            raise ValueError("split child query lineage bindings differ")
        if child != _statcast_child(parent, child_first, child_last):
            raise ValueError("split child query differs from its canonical parent slice")
        if child_first != cursor or child_first > child_last or child_last > parent_last:
            raise ValueError("split child ranges are unordered, overlapping, or gapped")
        cursor = child_last + timedelta(days=1)
    if cursor != parent_last + timedelta(days=1):
        raise ValueError("split child ranges do not cover the parent exactly")

    legacy_depth_one_anchor = (
        _is_legacy_partition_batch_candidate(repository_root, parent_batch)
        and parent_batch
        == build_legacy_partition_batch(
            repository_root,
            read_only_progress=read_only_progress,
        )
    )
    if parent.parent_query_id is None:
        if parent_batch.derived_from_sha256 is not None:
            raise ValueError("split root parent batch must not be derived")
        depth = 1
    elif legacy_depth_one_anchor:
        depth = 1
    else:
        ancestor = _load_split_receipt_locked(
            repository_root,
            evidence_root,
            split_fd,
            parent.parent_query_id,
            active_parent_ids=active_parent_ids,
            read_only_progress=read_only_progress,
        )
        if ancestor is None:
            raise ValueError("recursive split ancestor receipt is missing")
        else:
            if (
                ancestor.child_batch_id != parent_batch.batch_id
                or requested_parent_query_id not in ancestor.child_query_ids
            ):
                raise ValueError("recursive split ancestor binding differs")
            depth = ancestor.depth + 1

    attempt, attempt_path, attempt_sha256 = _verified_split_attempt(
        evidence_root,
        parent,
        reason,
        exhausted_attempt_number,
        read_only_progress=read_only_progress,
    )
    return (
        parent_batch,
        parent,
        child_batch,
        attempt,
        attempt_path,
        attempt_sha256,
        depth,
    )


def _split_record(
    repository_root: Path,
    evidence_root: Path,
    split_fd: int,
    *,
    parent_batch_id: str,
    parent_query_id: str,
    reason: str,
    exhausted_attempt_number: int,
    child_batch_id: str,
    active_parent_ids: set[str],
    read_only_progress: bool = False,
) -> dict[str, object]:
    (
        parent_batch,
        parent,
        child_batch,
        attempt,
        attempt_path,
        attempt_sha256,
        depth,
    ) = _verified_split_lineage(
        repository_root,
        evidence_root,
        split_fd,
        parent_batch_id=parent_batch_id,
        parent_query_id=parent_query_id,
        reason=reason,
        exhausted_attempt_number=exhausted_attempt_number,
        child_batch_id=child_batch_id,
        active_parent_ids=active_parent_ids,
        read_only_progress=read_only_progress,
    )
    record = {
        "attempt_receipt": {
            "attempt_number": exhausted_attempt_number,
            "path": attempt_path,
            "sha256": attempt_sha256,
        },
        "child_batch_id": child_batch.batch_id,
        "children": [
            {
                "query_id": logical_query_id(query),
                "range": _split_range(query),
            }
            for query in child_batch.queries
        ],
        "depth": depth,
        "parent_batch_id": parent_batch.batch_id,
        "parent_query_id": logical_query_id(parent),
        "parent_range": _split_range(parent),
        "reason": reason,
        "receipt_kind": "QUERY_SPLIT",
        "recorded_at_utc": _validate_split_timestamp(attempt["ended_at_utc"]),
        "schema_version": 1,
        "stage": parent_batch.stage,
    }
    return {
        **record,
        "split_receipt_id": _sha256_bytes(_canonical_json_bytes(record)),
    }


_SPLIT_RECORD_KEYS = frozenset(
    {
        "attempt_receipt",
        "child_batch_id",
        "children",
        "depth",
        "parent_batch_id",
        "parent_query_id",
        "parent_range",
        "reason",
        "receipt_kind",
        "recorded_at_utc",
        "schema_version",
        "split_receipt_id",
        "stage",
    }
)


def _split_receipt_value(record: dict[str, object], contents: bytes) -> SplitReceipt:
    children = record["children"]
    attempt = record["attempt_receipt"]
    if not isinstance(children, list) or not isinstance(attempt, dict):
        raise ValueError("split receipt nested records differ")
    child_query_ids = tuple(
        _validate_sha256(child.get("query_id"), "split child query_id")
        if isinstance(child, dict)
        else _validate_sha256(None, "split child query_id")
        for child in children
    )
    attempt_number = attempt.get("attempt_number")
    depth = record["depth"]
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or isinstance(depth, bool)
        or not isinstance(depth, int)
    ):
        raise ValueError("split receipt numeric fields differ")
    return SplitReceipt(
        split_receipt_id=_validate_sha256(
            record["split_receipt_id"], "split_receipt_id"
        ),
        receipt_sha256=_sha256_bytes(contents),
        stage=str(record["stage"]),
        parent_batch_id=_validate_sha256(
            record["parent_batch_id"], "parent_batch_id"
        ),
        parent_query_id=_validate_sha256(
            record["parent_query_id"], "parent_query_id"
        ),
        reason=str(record["reason"]),
        exhausted_attempt_number=attempt_number,
        depth=depth,
        child_batch_id=_validate_sha256(record["child_batch_id"], "child_batch_id"),
        child_query_ids=child_query_ids,
        recorded_at_utc=_validate_split_timestamp(record["recorded_at_utc"]),
    )


def _load_split_receipt_locked(
    repository_root: Path,
    evidence_root: Path,
    split_fd: int,
    parent_query_id: str,
    *,
    active_parent_ids: set[str],
    read_only_progress: bool = False,
) -> SplitReceipt | None:
    requested_id = _validate_sha256(parent_query_id, "parent_query_id")
    if requested_id in active_parent_ids:
        raise ValueError("split receipt ancestry contains a cycle")
    name = f"{requested_id}.json"
    loaded = _read_linked_regular(split_fd, name, "split receipt")
    if loaded is None:
        return None
    contents, metadata = loaded
    if metadata.st_nlink != 1:
        raise ValueError("split receipt must be an unaliased regular file")
    record = _parse_json_object(contents, "split receipt", canonical=True)
    if set(record) != _SPLIT_RECORD_KEYS:
        raise ValueError("split receipt keys differ")
    if (
        record.get("schema_version") != 1
        or record.get("receipt_kind") != "QUERY_SPLIT"
        or record.get("stage") != "REMAINING"
        or record.get("reason") not in _SPLIT_REASONS
        or record.get("parent_query_id") != requested_id
    ):
        raise ValueError("split receipt closed fields differ")
    embedded_id = record.get("split_receipt_id")
    identity = dict(record)
    identity.pop("split_receipt_id", None)
    if embedded_id != _sha256_bytes(_canonical_json_bytes(identity)):
        raise ValueError("split receipt canonical ID binding differs")
    attempt = record.get("attempt_receipt")
    if not isinstance(attempt, dict) or set(attempt) != {
        "attempt_number",
        "path",
        "sha256",
    }:
        raise ValueError("split attempt receipt binding differs")
    attempt_number = attempt.get("attempt_number")
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int):
        raise ValueError("split attempt receipt number differs")

    active_parent_ids.add(requested_id)
    try:
        expected = _split_record(
            repository_root,
            evidence_root,
            split_fd,
            parent_batch_id=_validate_sha256(
                record.get("parent_batch_id"), "parent_batch_id"
            ),
            parent_query_id=requested_id,
            reason=str(record["reason"]),
            exhausted_attempt_number=attempt_number,
            child_batch_id=_validate_sha256(
                record.get("child_batch_id"), "child_batch_id"
            ),
            active_parent_ids=active_parent_ids,
            read_only_progress=read_only_progress,
        )
    finally:
        active_parent_ids.remove(requested_id)
    if record != expected:
        raise ValueError("split receipt verified reconstruction differs")
    rebound = _read_linked_regular(split_fd, name, "split receipt")
    if rebound is None:
        raise ValueError("split receipt disappeared during load")
    rebound_contents, rebound_metadata = rebound
    if rebound_contents != contents or _batch_file_identity(
        rebound_metadata
    ) != _batch_file_identity(metadata):
        raise ValueError("split receipt identity or bytes changed during load")
    return _split_receipt_value(record, contents)


def _open_optional_directory(parent_fd: int, name: str) -> int | None:
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(
            "split receipt ancestors must be existing no-follow directories"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("split receipt ancestor identity differs")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_split_namespace(root: Path, *, create: bool) -> list[int] | None:
    descriptors = [_open_root(root)]
    try:
        current = descriptors[0]
        for component in _SPLIT_RECEIPT_ROOT.parts:
            child = (
                _open_or_create_directory(current, component)
                if create
                else _open_optional_directory(current, component)
            )
            if child is None:
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
                return None
            descriptors.append(child)
            current = child
        return descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _verify_split_namespace_chain(root: Path, descriptors: Sequence[int]) -> None:
    if len(descriptors) != len(_SPLIT_RECEIPT_ROOT.parts) + 1:
        raise ValueError("split receipt ancestor chain differs")
    fresh_root = _open_root(root)
    try:
        opened = os.fstat(descriptors[0])
        fresh = os.fstat(fresh_root)
        if (opened.st_dev, opened.st_ino) != (fresh.st_dev, fresh.st_ino):
            raise ValueError("split receipt root identity changed")
    finally:
        os.close(fresh_root)
    for index, component in enumerate(_SPLIT_RECEIPT_ROOT.parts):
        try:
            opened = os.fstat(descriptors[index + 1])
            linked = os.stat(
                component,
                dir_fd=descriptors[index],
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("split receipt ancestor identity changed") from exc
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("split receipt ancestor identity changed")


def load_split_receipt(
    repository_root: Path,
    parent_query_id: str,
    *,
    evidence_root: Path,
    read_only_progress: bool = False,
) -> SplitReceipt | None:
    requested_id = _validate_sha256(parent_query_id, "parent_query_id")
    repository_root = Path(repository_root)
    evidence_root = Path(evidence_root)
    _verify_split_evidence_root(repository_root, evidence_root)
    descriptors = _open_split_namespace(repository_root, create=False)
    if descriptors is None:
        return None
    current = descriptors[-1]
    try:
        fcntl.flock(current, fcntl.LOCK_SH)
        try:
            receipt = _load_split_receipt_locked(
                repository_root,
                evidence_root,
                current,
                requested_id,
                active_parent_ids=set(),
                read_only_progress=read_only_progress,
            )
            _verify_split_namespace_chain(repository_root, descriptors)
            return receipt
        finally:
            fcntl.flock(current, fcntl.LOCK_UN)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _publish_split_bytes_locked(
    parent_fd: int,
    parent_query_id: str,
    contents: bytes,
) -> tuple[int, ...] | None:
    temp_name = f".{parent_query_id}.{secrets.token_hex(16)}.tmp"
    final_name = f"{parent_query_id}.json"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
    temp_exists = True
    try:
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("split receipt publication made no progress")
            view = view[written:]
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("split receipt temp publication was aliased")
        os.fsync(descriptor)
        if _read_existing(parent_fd, temp_name) != contents:
            raise ValueError("split receipt temp bytes differ")
        try:
            os.link(
                temp_name,
                final_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            _unlink_owned_temp(parent_fd, temp_name, descriptor)
            temp_exists = False
            os.fsync(parent_fd)
            concurrent = _reconcile_batch_final(
                parent_fd, parent_query_id, contents
            )
            if concurrent is None:
                raise ValueError("concurrent split receipt disappeared")
            return None
        _unlink_owned_temp(parent_fd, temp_name, descriptor)
        temp_exists = False
        os.fsync(parent_fd)
        if _reconcile_batch_final(parent_fd, parent_query_id, contents) != contents:
            raise ValueError("split receipt bytes changed after publication")
        return _batch_file_identity(os.fstat(descriptor))
    finally:
        try:
            if temp_exists:
                _unlink_owned_temp(parent_fd, temp_name, descriptor)
                os.fsync(parent_fd)
        finally:
            os.close(descriptor)


def _cleanup_owned_split_final(
    parent_fd: int,
    parent_query_id: str,
    contents: bytes,
    identity: tuple[int, ...],
) -> None:
    name = f"{parent_query_id}.json"
    loaded = _read_linked_regular(parent_fd, name, "owned split receipt final")
    if loaded is None:
        return
    final_contents, metadata = loaded
    if final_contents != contents or _batch_file_identity(metadata) != identity:
        raise ValueError("split receipt final ownership changed before cleanup")
    rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if _batch_file_identity(rebound) != identity:
        raise ValueError("split receipt final ownership changed before cleanup")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def publish_split_receipt(
    repository_root: Path,
    *,
    evidence_root: Path,
    parent_batch_id: str,
    parent_query_id: str,
    reason: str,
    exhausted_attempt_number: int,
    child_batch_id: str,
) -> SplitReceipt:
    requested_parent_query_id = _validate_sha256(
        parent_query_id, "parent_query_id"
    )
    repository_root = Path(repository_root)
    evidence_root = Path(evidence_root)
    descriptors = _open_split_namespace(repository_root, create=True)
    if descriptors is None:
        raise AssertionError("created split receipt namespace must exist")
    current = descriptors[-1]
    try:
        fcntl.flock(current, fcntl.LOCK_EX)
        try:
            _verify_split_namespace_chain(repository_root, descriptors)
            record = _split_record(
                repository_root,
                evidence_root,
                current,
                parent_batch_id=parent_batch_id,
                parent_query_id=requested_parent_query_id,
                reason=reason,
                exhausted_attempt_number=exhausted_attempt_number,
                child_batch_id=child_batch_id,
                active_parent_ids={requested_parent_query_id},
            )
            contents = _canonical_json_bytes(record) + b"\n"
            existing = _reconcile_batch_final(
                current, requested_parent_query_id, contents
            )
            _cleanup_prelink_batch_temps(current, requested_parent_query_id)
            created_identity = None
            if existing is None:
                created_identity = _publish_split_bytes_locked(
                    current,
                    requested_parent_query_id,
                    contents,
                )
            try:
                receipt = _load_split_receipt_locked(
                    repository_root,
                    evidence_root,
                    current,
                    requested_parent_query_id,
                    active_parent_ids=set(),
                )
                if receipt is None:
                    raise ValueError("split receipt is missing after publication")
                _verify_split_namespace_chain(repository_root, descriptors)
                return receipt
            except BaseException:
                if created_identity is not None:
                    _cleanup_owned_split_final(
                        current,
                        requested_parent_query_id,
                        contents,
                        created_identity,
                    )
                raise
        finally:
            fcntl.flock(current, fcntl.LOCK_UN)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
