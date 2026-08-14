"""D-1 league pitching and player career-BF source semantics."""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right, insort
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING

from ..source_parsers.pitching import parse_league_era, parse_prior_career_bf
from ..v52_evidence import LogicalQuery, canonical_query_bytes, logical_query_id
from ..v52_seasons import SEASON_BOUNDARIES, monthly_ranges
from ..v52_source_contracts import (
    QUERY_SIDECAR_KEYS,
    canonical_json_bytes,
    query_sidecar,
    require_query,
)

if TYPE_CHECKING:
    from ..v52_query_plan import SplitReceipt


_STATS_ENDPOINT = "https://statsapi.mlb.com/api/v1/stats"
_MLB_BASE = "https://statsapi.mlb.com/api/v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_QUERY_KEYS = {
    "acquisition_contract_sha256",
    "endpoint",
    "expected_media_type",
    "grain",
    "parameters",
    "parent_query_id",
    "parser_version",
    "requested_range",
    "run_spec_sha256",
    "source",
}
_DERIVED_SIDECAR_KEYS = {
    "grain",
    "parser_version",
    "query_id",
    "schema_version",
    "semantics",
    "source",
    "storage_mode",
}
_DERIVED_SEMANTIC_KEYS = {
    "as_of_date",
    "consumer_cutoff_dates",
    "consuming_game_pks",
    "disposition",
    "reason",
    "season",
}
_PRESEASON_DISPOSITION = "PRESEASON_NO_CURRENT_SEASON_PITCHING"
_PRESEASON_REASON = "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START"
_CAREER_BASELINE_ENDS = (date(2017, 12, 31), date(2021, 12, 31))
_CAREER_ROOT_RANGES = tuple(
    (first, last)
    for season in sorted(SEASON_BOUNDARIES)
    for first, last in monthly_ranges(*SEASON_BOUNDARIES[season])
)
_CAREER_ROOT_STARTS = tuple(first for first, _last in _CAREER_ROOT_RANGES)
_CAREER_ROOT_ORDINAL_BY_START = {
    first: ordinal for ordinal, first in enumerate(_CAREER_ROOT_STARTS)
}
_CAREER_FIRST_ROOT_ORDINAL_BY_YEAR = {
    year: _CAREER_ROOT_ORDINAL_BY_START[monthly_ranges(*boundary)[0][0]]
    for year, boundary in SEASON_BOUNDARIES.items()
}
_CAREER_LAST_ROOT_ORDINAL_BY_YEAR = {
    year: _CAREER_ROOT_ORDINAL_BY_START[monthly_ranges(*boundary)[-1][0]]
    for year, boundary in SEASON_BOUNDARIES.items()
}


def validate_statcast_query(query: LogicalQuery) -> tuple[date, date]:
    """Keep the non-Statcast parser import surface offline-only."""
    from ..v52_query_plan import validate_statcast_query as validate

    return validate(query)


def bisect_statcast_query(query: LogicalQuery) -> tuple[LogicalQuery, LogicalQuery]:
    """Load the query-plan split helper only for Statcast replay work."""
    from ..v52_query_plan import bisect_statcast_query as bisect

    return bisect(query)


def _is_split_receipt(value: object) -> bool:
    from ..v52_query_plan import SplitReceipt

    return isinstance(value, SplitReceipt)


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


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _consumer_cutoffs(
    query: LogicalQuery, consuming_game_pks: Sequence[int]
) -> tuple[list[int], dict[int, date]]:
    if isinstance(consuming_game_pks, (str, bytes)) or not isinstance(
        consuming_game_pks, Sequence
    ):
        raise ValueError("consuming_game_pks must be a sequence")
    consumers = [_positive(value, "consuming gamePk") for value in consuming_game_pks]
    if consumers != sorted(set(consumers)) or not consumers:
        raise ValueError("consuming gamePks must be nonempty, sorted, and unique")
    bound_consumers = query.requested_range.get("consuming_game_pks")
    if bound_consumers != consumers:
        raise ValueError("query consumer binding differs from consuming gamePks")
    raw_dates = query.requested_range.get("consumer_cutoff_dates")
    if not isinstance(raw_dates, Mapping):
        raise ValueError("query must bind consumer_cutoff_dates")
    try:
        normalized = {
            int(key): _day(value, "consumer cutoff date")
            for key, value in raw_dates.items()
        }
    except (TypeError, ValueError) as exc:
        raise ValueError("consumer_cutoff_dates keys must be decimal gamePks") from exc
    if set(normalized) != set(consumers):
        raise ValueError("consumer date mapping differs from consuming gamePks")
    return consumers, normalized


def _pitching_group(raw: bytes, *, limit: int) -> tuple[int, int]:
    payload = _json_object(raw, "MLB pitching stats")
    stats = payload.get("stats")
    if not isinstance(stats, list) or len(stats) != 1 or not isinstance(stats[0], dict):
        raise ValueError("MLB pitching stats must contain exactly one stats group")
    group = stats[0]
    total_splits = group.get("totalSplits")
    splits = group.get("splits")
    if type(total_splits) is not int or total_splits < 0:
        raise ValueError("MLB pitching totalSplits must be a nonnegative integer")
    if not isinstance(splits, list) or len(splits) != total_splits:
        raise ValueError("MLB pitching stats are truncated or paged")
    if total_splits >= limit:
        raise ValueError("MLB pitching totalSplits reached the response limit")
    batters_faced = 0
    for split in splits:
        stat = split.get("stat") if isinstance(split, dict) else None
        value = stat.get("battersFaced") if isinstance(stat, dict) else None
        if type(value) is not int or value < 0:
            raise ValueError("battersFaced must be a nonnegative integer")
        batters_faced += value
    return total_splits, batters_faced


def recompute_pitching_sidecar(
    raw: bytes,
    query: LogicalQuery,
    consuming_game_pks: Sequence[int],
) -> Mapping[str, object]:
    require_query(
        query,
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        endpoint=_STATS_ENDPOINT,
        expected_media_type="application/json",
    )
    season = query.requested_range.get("season")
    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season not in SEASON_BOUNDARIES
    ):
        raise ValueError("pitching season must be within 2018-2024")
    season_open = _day(query.requested_range.get("start_date"), "pitching season open")
    if season_open != SEASON_BOUNDARIES[season][0]:
        raise ValueError("pitching query must use the exact season open")
    as_of = _day(query.requested_range.get("as_of_date"), "pitching as-of date")
    if as_of < season_open or as_of.year != season:
        raise ValueError("pitching as-of date differs from season")
    expected_parameters = {
        "stats": "byDateRange",
        "group": "pitching",
        "sportIds": 1,
        "gameType": "R",
        "playerPool": "ALL",
        "limit": 5000,
        "startDate": season_open.isoformat(),
        "endDate": as_of.isoformat(),
    }
    if dict(query.parameters) != expected_parameters:
        raise ValueError("pitching query parameters differ from the cutoff contract")
    if query.requested_range.get("response_completeness") != {
        "limit": 5000,
        "reject_total_splits_at_or_above_limit": True,
    }:
        raise ValueError("pitching query completeness contract differs")
    consumers, cutoffs = _consumer_cutoffs(query, consuming_game_pks)
    if any(as_of != cutoff for cutoff in cutoffs.values()):
        raise ValueError("pitching as-of date must equal every consumer D-1 cutoff")
    parsed = parse_league_era(raw)
    total_splits, batters_faced = _pitching_group(raw, limit=5000)
    semantics = {
        "as_of_date": as_of.isoformat(),
        "batters_faced": batters_faced,
        "consuming_game_pks": consumers,
        "consumer_cutoff_dates": {
            str(game_pk): cutoffs[game_pk].isoformat() for game_pk in consumers
        },
        "earned_runs": parsed["earned_runs"],
        "league_era": parsed["value"],
        "outs": parsed["outs"],
        "season": season,
        "season_open_date": season_open.isoformat(),
        "total_splits": total_splits,
    }
    return query_sidecar(raw, query, semantics)


def recompute_career_bf_sidecar(
    raw: bytes,
    query: LogicalQuery,
    consuming_game_pks: Sequence[int],
) -> Mapping[str, object]:
    require_query(
        query,
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        endpoint=f"{_MLB_BASE}/people/{_positive(query.requested_range.get('pitcher_id'), 'pitcher_id')}/stats",
        expected_media_type="application/json",
    )
    pitcher_id = _positive(query.requested_range.get("pitcher_id"), "pitcher_id")
    inclusive_end = _day(
        query.requested_range.get("inclusive_end"), "career BF inclusive end"
    )
    expected_parameters = {
        "stats": "byDateRange",
        "group": "pitching",
        "gameType": "R",
        "sportIds": 1,
        "startDate": "1876-01-01",
        "endDate": inclusive_end.isoformat(),
    }
    if dict(query.parameters) != expected_parameters:
        raise ValueError("career BF query parameters differ from the cutoff contract")
    consumers, cutoffs = _consumer_cutoffs(query, consuming_game_pks)
    if inclusive_end == date(2021, 12, 31) and any(
        cutoff < inclusive_end for cutoff in cutoffs.values()
    ):
        raise ValueError(
            "legacy 2021-12-31 BF baseline is allowed only for 2022+ consumers"
        )
    if any(inclusive_end > cutoff for cutoff in cutoffs.values()):
        raise ValueError(
            "career BF inclusive end must be no later than every consumer D-1"
        )
    if inclusive_end not in {date(2017, 12, 31), date(2021, 12, 31)}:
        raise ValueError("career BF baseline cutoff is not supported")
    baseline = parse_prior_career_bf(raw, pitcher_id)
    semantics = {
        "baseline_batters_faced": baseline,
        "consumer_cutoff_dates": {
            str(game_pk): cutoffs[game_pk].isoformat() for game_pk in consumers
        },
        "consuming_game_pks": consumers,
        "inclusive_end_date": inclusive_end.isoformat(),
        "pitcher_id": pitcher_id,
        "requires_verified_statcast_bf_through_consumer_d1": True,
    }
    return query_sidecar(raw, query, semantics)


def _artifact_sha256(sidecar: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(sidecar)) + b"\n").hexdigest()


def _require_task2_sidecar(
    sidecar: Mapping[str, object], *, source: str, grain: str, label: str
) -> Mapping[str, object]:
    if isinstance(sidecar, Mapping) and sidecar.get("storage_mode") is not None:
        semantics = sidecar.get("semantics")
        if (
            set(sidecar) != _DERIVED_SIDECAR_KEYS
            or sidecar.get("storage_mode") != "DERIVED_DISPOSITION"
            or source != "mlb_stats_pitching"
            or grain != "season_d1_league_pitching_reference"
            or sidecar.get("source") != source
            or sidecar.get("grain") != grain
            or sidecar.get("parser_version") != "v5.2.1"
            or sidecar.get("schema_version") != 1
            or not isinstance(sidecar.get("query_id"), str)
            or not _SHA256.fullmatch(sidecar["query_id"])
            or not isinstance(semantics, Mapping)
            or set(semantics) != _DERIVED_SEMANTIC_KEYS
        ):
            raise ValueError(
                f"{label} requires a closed verified derived disposition sidecar"
            )
        season = semantics.get("season")
        as_of = _day(semantics.get("as_of_date"), "derived pitching as-of")
        consumers = semantics.get("consuming_game_pks")
        cutoffs = semantics.get("consumer_cutoff_dates")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in SEASON_BOUNDARIES
            or as_of != SEASON_BOUNDARIES[season][0] - timedelta(days=1)
            or not isinstance(consumers, list)
            or consumers != sorted(set(consumers))
            or not consumers
            or any(type(value) is not int or value <= 0 for value in consumers)
            or not isinstance(cutoffs, Mapping)
            or cutoffs != {str(game_pk): as_of.isoformat() for game_pk in consumers}
            or semantics.get("disposition") != _PRESEASON_DISPOSITION
            or semantics.get("reason") != _PRESEASON_REASON
        ):
            raise ValueError(f"{label} derived disposition semantics differ")
        return semantics
    if (
        not isinstance(sidecar, Mapping)
        or set(sidecar) != QUERY_SIDECAR_KEYS
        or sidecar.get("source") != source
        or sidecar.get("grain") != grain
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
        raise ValueError(f"{label} requires a valid Task2 {source}/{grain} sidecar")
    return sidecar["semantics"]


def _statcast_query_lineage(
    sidecar: Mapping[str, object], semantics: Mapping[str, object]
) -> tuple[date, date, LogicalQuery, str]:
    canonical = semantics.get("canonical_query")
    coverage = semantics.get("coverage")
    if (
        not isinstance(canonical, Mapping)
        or set(canonical) != _CANONICAL_QUERY_KEYS
        or not isinstance(canonical.get("parameters"), Mapping)
        or not isinstance(canonical.get("requested_range"), Mapping)
        or not isinstance(coverage, Mapping)
    ):
        raise ValueError("Statcast BF source canonical query lineage is invalid")
    try:
        query = LogicalQuery(
            source=canonical["source"],
            grain=canonical["grain"],
            endpoint=canonical["endpoint"],
            parameters=dict(canonical["parameters"]),
            requested_range=dict(canonical["requested_range"]),
            expected_media_type=canonical["expected_media_type"],
            parser_version=canonical["parser_version"],
            parent_query_id=canonical["parent_query_id"],
            run_spec_sha256=canonical["run_spec_sha256"],
            acquisition_contract_sha256=canonical["acquisition_contract_sha256"],
        )
        normalized = json.loads(canonical_query_bytes(query))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Statcast BF source canonical query lineage is invalid"
        ) from exc
    if (
        normalized != dict(canonical)
        or not isinstance(query.run_spec_sha256, str)
        or not _SHA256.fullmatch(query.run_spec_sha256)
        or not isinstance(query.acquisition_contract_sha256, str)
        or not _SHA256.fullmatch(query.acquisition_contract_sha256)
        or (
            query.parent_query_id is not None
            and (
                not isinstance(query.parent_query_id, str)
                or not _SHA256.fullmatch(query.parent_query_id)
            )
        )
        or sidecar.get("query_id") != logical_query_id(query)
    ):
        raise ValueError("Statcast BF source canonical query lineage is invalid")
    try:
        first, last = validate_statcast_query(query)
    except ValueError as exc:
        raise ValueError(
            f"Statcast BF whole regular season or regular-season coverage gap: {exc}"
        ) from exc
    if (
        coverage.get("inclusive_start") != first.isoformat()
        or coverage.get("inclusive_end") != last.isoformat()
    ):
        raise ValueError("Statcast BF source requested range is invalid")
    disposition = semantics.get("split_disposition")
    if disposition not in {"READY", "SPLIT_REQUIRED"}:
        raise ValueError("Statcast BF source split state is not terminal READY")
    return first, last, query, disposition


@dataclass(frozen=True)
class _StatcastNode:
    query_id: str
    first: date
    last: date
    query: LogicalQuery
    disposition: str


def _monthly_root_query(
    node: _StatcastNode,
) -> tuple[date, date, LogicalQuery]:
    season = SEASON_BOUNDARIES.get(node.first.year)
    if season is None:
        raise ValueError("Statcast split node is outside required monthly roots")
    parent_range = next(
        (
            (first, last)
            for first, last in monthly_ranges(*season)
            if first <= node.first <= node.last <= last
        ),
        None,
    )
    if parent_range is None:
        raise ValueError("Statcast split node has wrong root coverage")
    parent_first, parent_last = parent_range
    requested = {
        key: value
        for key, value in node.query.requested_range.items()
        if key not in {"acquisition", "protected_inventory_sha256"}
    }
    requested.update(
        {
            "start_date": parent_first.isoformat(),
            "end_date": parent_last.isoformat(),
        }
    )
    parameters = {
        **node.query.parameters,
        "game_date_gt": parent_first.isoformat(),
        "game_date_lt": parent_last.isoformat(),
    }
    return (
        parent_first,
        parent_last,
        replace(
            node.query,
            parameters=parameters,
            requested_range=requested,
            parent_query_id=None,
        ),
    )


def _statcast_population_contract(query: LogicalQuery) -> tuple[object, ...]:
    invariant_parameters = {
        key: value
        for key, value in query.parameters.items()
        if key not in {"game_date_gt", "game_date_lt"}
    }
    return (
        query.source,
        query.grain,
        query.endpoint,
        query.expected_media_type,
        query.parser_version,
        query.acquisition_contract_sha256,
        canonical_json_bytes(invariant_parameters),
    )


def _validate_statcast_population_contract(
    nodes: Sequence[_StatcastNode], ready_leaves: Sequence[_StatcastNode]
) -> None:
    if len({node.query.run_spec_sha256 for node in nodes}) > 1:
        raise ValueError("Statcast population has mixed global run_spec_sha256")
    if len({_statcast_population_contract(node.query) for node in nodes}) > 1:
        raise ValueError("Statcast population has mixed global query contract")
    protected_inventories = {
        leaf.query.requested_range.get("protected_inventory_sha256")
        for leaf in ready_leaves
        if leaf.query.requested_range.get("acquisition") == "PROTECTED_LEGACY_REFERENCE"
    }
    if len(protected_inventories) > 1:
        raise ValueError("Statcast population has mixed global protected inventory")


def recompute_pitching_season_snapshot(
    query_sidecars: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    records = []
    seen_as_of: set[str] = set()
    seen_consumers: set[int] = set()
    for sidecar in query_sidecars:
        semantics = _require_task2_sidecar(
            sidecar,
            source="mlb_stats_pitching",
            grain="season_d1_league_pitching_reference",
            label="pitching aggregate",
        )
        as_of = _day(semantics.get("as_of_date"), "pitching aggregate as-of")
        as_of_text = as_of.isoformat()
        if as_of_text in seen_as_of:
            raise ValueError("pitching aggregate contains duplicate as-of date")
        seen_as_of.add(as_of_text)
        consumers = semantics.get("consuming_game_pks")
        cutoffs = semantics.get("consumer_cutoff_dates")
        if (
            not isinstance(consumers, list)
            or consumers != sorted(set(consumers))
            or not consumers
            or any(type(value) is not int or value <= 0 for value in consumers)
            or not isinstance(cutoffs, Mapping)
            or set(cutoffs) != {str(value) for value in consumers}
            or any(value != as_of_text for value in cutoffs.values())
        ):
            raise ValueError("pitching aggregate consumer cutoffs differ from as-of")
        duplicate_consumers = seen_consumers.intersection(consumers)
        if duplicate_consumers:
            raise ValueError(
                "pitching aggregate consumer gamePk maps to multiple D-1 records"
            )
        seen_consumers.update(consumers)
        record = {
            "as_of_date": as_of_text,
            "consumer_cutoff_dates": dict(cutoffs),
            "consuming_game_pks": consumers,
            "query_sidecar_sha256": _artifact_sha256(sidecar),
        }
        disposition = semantics.get("disposition")
        if disposition is not None:
            if disposition != "PRESEASON_NO_CURRENT_SEASON_PITCHING" or any(
                field in semantics for field in ("earned_runs", "outs", "batters_faced")
            ):
                raise ValueError("pitching aggregate disposition semantics are invalid")
            record["disposition"] = disposition
        else:
            for field in ("earned_runs", "outs", "batters_faced"):
                value = semantics.get(field)
                if type(value) is not int or value < 0:
                    raise ValueError(f"pitching aggregate {field} must be nonnegative")
                record[field] = value
        records.append(record)
    records.sort(key=lambda row: row["as_of_date"])
    if not records:
        raise ValueError("pitching aggregate requires at least one as-of record")
    return {
        "schema_version": 1,
        "grain": "season_d1_league_pitching_reference",
        "records": records,
        "query_sidecar_sha256s": [row["query_sidecar_sha256"] for row in records],
    }


def _validate_career_statcast_coverage(
    baseline_end: date,
    cutoff: date,
    source_queries: Sequence[tuple[date, date, LogicalQuery]],
) -> None:
    boundary = SEASON_BOUNDARIES.get(cutoff.year)
    if boundary is None:
        raise ValueError("career consumer D-1 is outside the regular season")
    in_season = boundary[0] <= cutoff <= boundary[1]
    opening_day_cutoff = cutoff == boundary[0] - timedelta(days=1)
    if not in_season and not opening_day_cutoff:
        raise ValueError("career consumer D-1 is outside the regular season")
    first_year = baseline_end.year + 1
    if first_year not in SEASON_BOUNDARIES:
        raise ValueError("career baseline does not precede an approved Statcast season")

    final_required_year = cutoff.year if in_season else cutoff.year - 1
    expected_by_year: dict[int, list[tuple[date, date]]] = {}
    for year in range(first_year, final_required_year + 1):
        season_start, season_end = SEASON_BOUNDARIES[year]
        expected = list(monthly_ranges(season_start, season_end))
        if in_season and year == cutoff.year:
            expected = [item for item in expected if item[0] <= cutoff]
        expected_by_year[year] = expected

    actual_by_year: dict[int, list[tuple[date, date, LogicalQuery]]] = {}
    for first, last, query in source_queries:
        if first.year != last.year:
            raise ValueError("Statcast BF source range crosses a season boundary")
        actual_by_year.setdefault(first.year, []).append((first, last, query))

    for year, expected in expected_by_year.items():
        actual = actual_by_year.pop(year, [])
        if expected and not actual:
            raise ValueError(f"whole regular season {year} is omitted")
        index = 0
        for parent_first, parent_last in expected:
            cursor = parent_first
            children: list[tuple[date, date, LogicalQuery]] = []
            while index < len(actual) and actual[index][0] <= parent_last:
                first, last, query = actual[index]
                if first < cursor:
                    raise ValueError(
                        "regular-season coverage gap or overlap: "
                        "Statcast split child overlap"
                    )
                if first > cursor:
                    raise ValueError(
                        "regular-season coverage gap: Statcast split child gap"
                    )
                if last < first or last > parent_last:
                    raise ValueError(
                        "regular-season coverage gap or overlap: "
                        "Statcast split child overlap"
                    )
                children.append((first, last, query))
                cursor = last + timedelta(days=1)
                index += 1
                if cursor > parent_last:
                    break
            if cursor != parent_last + timedelta(days=1):
                raise ValueError(
                    "regular-season coverage gap: Statcast split child gap"
                )
        if index != len(actual):
            raise ValueError("Statcast BF source range is outside monthly parents")
    if actual_by_year:
        raise ValueError("Statcast BF source range is outside required seasons")


_SPLIT_RECEIPT_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)


def _validate_statcast_roots(
    root_queries: Sequence[LogicalQuery],
) -> tuple[list[_StatcastNode], dict[str, LogicalQuery]]:
    if isinstance(root_queries, (str, bytes)) or not isinstance(root_queries, Sequence):
        raise ValueError("statcast_root_queries must be a sequence")
    roots: list[_StatcastNode] = []
    by_id: dict[str, LogicalQuery] = {}
    for query in root_queries:
        if not isinstance(query, LogicalQuery):
            raise ValueError("statcast_root_queries must contain LogicalQuery values")
        first, last = validate_statcast_query(query)
        season = SEASON_BOUNDARIES[first.year]
        if (
            query.parent_query_id is not None
            or set(query.requested_range) != {"year", "start_date", "end_date"}
            or (first, last) not in monthly_ranges(*season)
        ):
            raise ValueError("Statcast monthly root query contract is invalid")
        query_id = logical_query_id(query)
        if query_id in by_id:
            raise ValueError("Statcast monthly roots contain a duplicate query")
        by_id[query_id] = query
        roots.append(_StatcastNode(query_id, first, last, query, "ROOT"))
    if [(node.first, node.last) for node in roots] != sorted(
        {(node.first, node.last) for node in roots}
    ):
        raise ValueError("Statcast monthly roots are not uniquely ordered")
    if roots:
        _validate_statcast_population_contract(roots, ())
    return roots, by_id


def _validate_split_receipts(
    split_receipts: Sequence[SplitReceipt],
    queries: dict[str, LogicalQuery],
) -> tuple[dict[str, tuple[str, str]], dict[str, str], set[str]]:
    if isinstance(split_receipts, (str, bytes)) or not isinstance(
        split_receipts, Sequence
    ):
        raise ValueError("statcast_split_receipts must be a sequence")
    receipts_by_parent: dict[str, SplitReceipt] = {}
    parent_by_child: dict[str, str] = {}
    children_by_parent: dict[str, tuple[str, str]] = {}
    receipt_hashes: dict[str, str] = {}
    generated_ids: set[str] = set()
    for receipt in split_receipts:
        if (
            not _is_split_receipt(receipt)
            or not _SHA256.fullmatch(receipt.split_receipt_id)
            or not _SHA256.fullmatch(receipt.receipt_sha256)
            or receipt.stage != "REMAINING"
            or not _SHA256.fullmatch(receipt.parent_batch_id)
            or not _SHA256.fullmatch(receipt.parent_query_id)
            or receipt.reason not in {"ROW_CAP", "SIZE_LIMIT", "TIMEOUT"}
            or type(receipt.exhausted_attempt_number) is not int
            or receipt.exhausted_attempt_number <= 0
            or type(receipt.depth) is not int
            or receipt.depth <= 0
            or not _SHA256.fullmatch(receipt.child_batch_id)
            or not isinstance(receipt.child_query_ids, tuple)
            or len(receipt.child_query_ids) != 2
            or any(
                not isinstance(value, str) or not _SHA256.fullmatch(value)
                for value in receipt.child_query_ids
            )
            or len(set(receipt.child_query_ids)) != 2
            or not _SPLIT_RECEIPT_TIMESTAMP.fullmatch(receipt.recorded_at_utc)
        ):
            raise ValueError("Statcast split receipt authority fields are invalid")
        if receipt.parent_query_id in receipts_by_parent:
            raise ValueError("Statcast split receipts duplicate a parent")
        receipts_by_parent[receipt.parent_query_id] = receipt
        for child_id in receipt.child_query_ids:
            if child_id in parent_by_child:
                raise ValueError(
                    "Statcast split child lineage overlaps or is duplicated"
                )
            parent_by_child[child_id] = receipt.parent_query_id

    for parent_id, receipt in receipts_by_parent.items():
        parent = queries.get(parent_id)
        if parent is None:
            raise ValueError("Statcast split receipt has an orphan or unknown parent")
        expected_children = bisect_statcast_query(parent)
        expected_ids = tuple(logical_query_id(query) for query in expected_children)
        if receipt.child_query_ids != expected_ids:
            raise ValueError("Statcast split child lineage range/order/cover differs")
        children_by_parent[parent_id] = expected_ids
        receipt_hashes[parent_id] = receipt.receipt_sha256
        for child_id, child in zip(expected_ids, expected_children):
            if child_id in queries and canonical_query_bytes(
                queries[child_id]
            ) != canonical_query_bytes(child):
                raise ValueError("Statcast split child lineage is ambiguous")
            queries[child_id] = child
            generated_ids.add(child_id)

    resolved_depths: dict[str, int] = {}
    visiting: set[str] = set()

    def resolve_depth(parent_id: str) -> int:
        if parent_id in resolved_depths:
            return resolved_depths[parent_id]
        if parent_id in visiting:
            raise ValueError("Statcast split receipt graph contains a cycle")
        visiting.add(parent_id)
        ancestor_id = parent_by_child.get(parent_id)
        if ancestor_id is None:
            depth = 1
        else:
            ancestor = receipts_by_parent.get(ancestor_id)
            if ancestor is None:
                raise ValueError("Statcast split receipt graph contains an orphan")
            receipt = receipts_by_parent[parent_id]
            if receipt.parent_batch_id != ancestor.child_batch_id:
                raise ValueError("Statcast split receipt batch lineage differs")
            depth = resolve_depth(ancestor_id) + 1
        visiting.remove(parent_id)
        resolved_depths[parent_id] = depth
        return depth

    for parent_id, receipt in receipts_by_parent.items():
        if receipt.depth != resolve_depth(parent_id):
            raise ValueError("Statcast split receipt topological depth differs")
    return children_by_parent, receipt_hashes, generated_ids


def _seed_bodyless_split_parents(
    split_receipts: Sequence[SplitReceipt],
    queries: dict[str, LogicalQuery],
    roots: Sequence[_StatcastNode],
) -> None:
    parent_of_query: dict[str, str] = {}
    for receipt in split_receipts:
        if not _is_split_receipt(receipt):
            continue
        for child_id in receipt.child_query_ids:
            parent_of_query[child_id] = receipt.parent_query_id

    unresolved = list(split_receipts)
    while unresolved:
        progressed = False
        remaining = []
        for receipt in unresolved:
            if not _is_split_receipt(receipt):
                remaining.append(receipt)
                continue
            parent_id = receipt.parent_query_id
            child_ids = receipt.child_query_ids
            if parent_id in queries:
                progressed = True
                continue
            if (
                not isinstance(parent_id, str)
                or not isinstance(child_ids, tuple)
                or len(child_ids) != 2
                or any(child_id not in queries for child_id in child_ids)
            ):
                remaining.append(receipt)
                continue
            left, right = (queries[child_ids[0]], queries[child_ids[1]])
            left_first, left_last = validate_statcast_query(left)
            right_first, right_last = validate_statcast_query(right)
            if left_last + timedelta(days=1) != right_first:
                raise ValueError(
                    "Statcast split child lineage has a range gap or overlap"
                )
            grandparent_id = parent_of_query.get(parent_id)
            if grandparent_id is None:
                containing_roots = [
                    root
                    for root in roots
                    if root.first <= left_first <= right_last <= root.last
                ]
                if len(containing_roots) != 1:
                    raise ValueError(
                        "Statcast bodyless split parent has no unique root"
                    )
                grandparent_id = containing_roots[0].query_id
            candidate = replace(
                left,
                parameters={
                    **left.parameters,
                    "game_date_gt": left_first.isoformat(),
                    "game_date_lt": right_last.isoformat(),
                },
                requested_range={
                    **left.requested_range,
                    "start_date": left_first.isoformat(),
                    "end_date": right_last.isoformat(),
                },
                parent_query_id=grandparent_id,
            )
            expected_children = bisect_statcast_query(candidate)
            if logical_query_id(candidate) != parent_id or tuple(
                canonical_query_bytes(query) for query in expected_children
            ) != (canonical_query_bytes(left), canonical_query_bytes(right)):
                raise ValueError("Statcast bodyless split parent lineage differs")
            queries[parent_id] = candidate
            progressed = True
        if not progressed:
            return
        unresolved = remaining


def _scan_ready_statcast_leaf(
    sidecar: Mapping[str, object],
) -> tuple[_StatcastNode, list[tuple[date, int]], str]:
    semantics = _require_task2_sidecar(
        sidecar,
        source="baseball_savant_statcast",
        grain="pitch",
        label="Statcast BF READY leaf",
    )
    first, last, query, disposition = _statcast_query_lineage(sidecar, semantics)
    if disposition != "READY":
        raise ValueError(
            "Statcast BF additions accept READY leaf sidecars only; SPLIT_REQUIRED success sidecars are forbidden"
        )
    pitcher_ids = semantics.get("pitcher_ids")
    pitcher_bf = semantics.get("pitcher_batters_faced")
    plate_appearances = semantics.get("plate_appearance_evidence")
    game_pks = semantics.get("game_pks")
    if (
        not isinstance(pitcher_ids, list)
        or pitcher_ids != sorted(set(pitcher_ids))
        or any(type(value) is not int or value <= 0 for value in pitcher_ids)
        or not isinstance(pitcher_bf, Mapping)
        or not isinstance(plate_appearances, list)
        or not isinstance(game_pks, list)
    ):
        raise ValueError("Statcast BF source pitcher population is invalid")
    required_pa_keys = {
        "at_bat_number",
        "batter_id",
        "events",
        "game_date",
        "game_pk",
        "pitch_number",
        "pitcher_id",
    }
    counts: dict[int, int] = {}
    evidence: list[tuple[date, int]] = []
    local_seen: set[tuple[int, int]] = set()
    for pa in plate_appearances:
        if not isinstance(pa, Mapping) or set(pa) != required_pa_keys:
            raise ValueError("Statcast PA evidence fields are invalid")
        game_pk = _positive(pa.get("game_pk"), "Statcast PA game_pk")
        at_bat = _positive(pa.get("at_bat_number"), "Statcast PA at_bat_number")
        pitcher_id = _positive(pa.get("pitcher_id"), "Statcast PA pitcher_id")
        _positive(pa.get("batter_id"), "Statcast PA batter_id")
        _positive(pa.get("pitch_number"), "Statcast PA pitch_number")
        game_date = _day(pa.get("game_date"), "Statcast PA game_date")
        if (
            game_pk not in game_pks
            or pitcher_id not in pitcher_ids
            or not first <= game_date <= last
            or not isinstance(pa.get("events"), str)
            or not pa["events"]
            or (game_pk, at_bat) in local_seen
        ):
            raise ValueError("Statcast PA evidence is inconsistent")
        local_seen.add((game_pk, at_bat))
        counts[pitcher_id] = counts.get(pitcher_id, 0) + 1
        evidence.append((game_date, pitcher_id))
    if dict(pitcher_bf) != {str(key): counts[key] for key in sorted(counts)}:
        raise ValueError("Statcast BF evidence summary differs from PA evidence")
    return (
        _StatcastNode(sidecar["query_id"], first, last, query, "READY"),
        evidence,
        _artifact_sha256(sidecar),
    )


@dataclass(frozen=True)
class _CareerLineagePrefix:
    nodes: tuple[_StatcastNode, ...]
    first_dates: tuple[date, ...]
    leaf_hashes: tuple[tuple[str, ...], ...]
    receipt_hashes: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class _CareerStatcastIndex:
    terminal_nodes: tuple[_StatcastNode, ...]
    leaf_hash_by_id: Mapping[str, str]
    pitcher_dates: Mapping[int, tuple[date, ...]]
    root_for_query: Mapping[str, str]
    parent_by_query: Mapping[str, str | None]
    receipt_hash_by_parent: Mapping[str, str]
    leaf_root_ordinal_by_id: Mapping[str, int]
    leaf_receipt_hashes_by_id: Mapping[str, tuple[str, ...]]
    root_run_end_by_ordinal: Mapping[int, int]
    lineage_prefix_by_baseline: Mapping[date, _CareerLineagePrefix]
    lineage_cache: dict[
        tuple[date, date],
        tuple[tuple[_StatcastNode, ...], tuple[str, ...], tuple[str, ...]],
    ]


def _build_career_statcast_index(
    ready_leaf_sidecars: Sequence[Mapping[str, object]],
    root_queries: Sequence[LogicalQuery],
    split_receipts: Sequence[SplitReceipt],
    *,
    require_complete_roots: bool = True,
) -> _CareerStatcastIndex:
    """Authenticate the global Statcast BF graph with one scan per READY leaf."""
    if isinstance(ready_leaf_sidecars, (str, bytes)) or not isinstance(
        ready_leaf_sidecars, Sequence
    ):
        raise ValueError("statcast_ready_leaf_sidecars must be a sequence")
    root_nodes, query_by_id = _validate_statcast_roots(root_queries)
    if require_complete_roots and tuple(
        (node.first, node.last) for node in root_nodes
    ) != _CAREER_ROOT_RANGES:
        raise ValueError("career BF requires the complete 2018-2024 monthly roots")

    leaf_by_id: dict[str, _StatcastNode] = {}
    leaf_hash_by_id: dict[str, str] = {}
    pitcher_dates: dict[int, list[date]] = {}
    global_pa_seen: set[tuple[int, int]] = set()
    for sidecar in ready_leaf_sidecars:
        node, evidence, digest = _scan_ready_statcast_leaf(sidecar)
        if node.query_id in leaf_by_id:
            raise ValueError(
                "Statcast READY leaf population contains a duplicate query"
            )
        semantics = sidecar["semantics"]
        for pa, (game_date, pitcher_id) in zip(
            semantics["plate_appearance_evidence"], evidence
        ):
            identity = (pa["game_pk"], pa["at_bat_number"])
            if identity in global_pa_seen:
                raise ValueError(
                    "Statcast PA evidence is duplicated across READY leaves"
                )
            global_pa_seen.add(identity)
            pitcher_dates.setdefault(pitcher_id, []).append(game_date)
        leaf_by_id[node.query_id] = node
        leaf_hash_by_id[node.query_id] = digest
    if not leaf_by_id and (require_complete_roots or root_nodes or split_receipts):
        raise ValueError("verified Statcast BF READY leaf population is missing")
    for dates in pitcher_dates.values():
        dates.sort()

    query_by_id.update({query_id: node.query for query_id, node in leaf_by_id.items()})
    _seed_bodyless_split_parents(split_receipts, query_by_id, root_nodes)
    children_by_parent, receipt_hash_by_parent, generated_ids = (
        _validate_split_receipts(split_receipts, query_by_id)
    )
    split_parent_nodes = []
    for parent_id in children_by_parent:
        query = query_by_id[parent_id]
        first, last = validate_statcast_query(query)
        split_parent_nodes.append(
            _StatcastNode(parent_id, first, last, query, "SPLIT_REQUIRED")
        )
    _validate_statcast_population_contract(
        [*root_nodes, *split_parent_nodes, *leaf_by_id.values()],
        list(leaf_by_id.values()),
    )

    root_ids = {node.query_id for node in root_nodes}
    root_for_query: dict[str, str] = {}
    consumed_leaf_ids: set[str] = set()
    consumed_receipt_parents: set[str] = set()

    def consume_subtree(query_id: str, root_id: str, visiting: set[str]) -> None:
        if query_id in visiting:
            raise ValueError("Statcast split lineage contains a cycle")
        prior_root = root_for_query.setdefault(query_id, root_id)
        if prior_root != root_id:
            raise ValueError("Statcast split lineage overlaps monthly roots")
        child_ids = children_by_parent.get(query_id)
        if child_ids is None:
            if query_id not in leaf_by_id:
                raise ValueError(
                    "Statcast split lineage is missing complete READY leaf cover"
                )
            consumed_leaf_ids.add(query_id)
            return
        if query_id in leaf_by_id:
            raise ValueError("Statcast split parent cannot also be a READY leaf")
        consumed_receipt_parents.add(query_id)
        for child_id in child_ids:
            consume_subtree(child_id, root_id, {*visiting, query_id})

    for root in root_nodes:
        root_id = root.query_id
        direct_leaves = sorted(
            (
                leaf
                for leaf in leaf_by_id.values()
                if leaf.query.parent_query_id == root_id
            ),
            key=lambda node: (node.first, node.last, node.query_id),
        )
        if root_id in children_by_parent:
            consume_subtree(root_id, root_id, set())
        elif root_id in leaf_by_id:
            if direct_leaves:
                raise ValueError("Statcast READY root has ambiguous children")
            consume_subtree(root_id, root_id, set())
        else:
            direct_split_parents = [
                node
                for node in split_parent_nodes
                if node.query.parent_query_id == root_id
            ]
            partition_nodes = sorted(
                [*direct_leaves, *direct_split_parents],
                key=lambda node: (node.first, node.last, node.query_id),
            )
            if not partition_nodes or any(
                node.query.requested_range.get("acquisition")
                not in {"FETCH_REQUIRED", "PROTECTED_LEGACY_REFERENCE"}
                for node in partition_nodes
            ):
                raise ValueError(
                    "Statcast first-level legacy partition is missing: "
                    "regular-season coverage gap/overlap; READY leaf has wrong parent "
                    "or whole regular season omitted"
                )
            cursor = root.first
            for node in partition_nodes:
                _parent_first, _parent_last, rebuilt_root = _monthly_root_query(node)
                if (
                    logical_query_id(rebuilt_root) != root_id
                    or node.first != cursor
                    or node.last > root.last
                ):
                    raise ValueError(
                        "Statcast first-level legacy partition has a gap or overlap"
                    )
                cursor = node.last + timedelta(days=1)
                consume_subtree(node.query_id, root_id, set())
            if cursor != root.last + timedelta(days=1):
                raise ValueError(
                    "Statcast first-level legacy partition lacks exact cover"
                )

    if consumed_leaf_ids != set(leaf_by_id):
        raise ValueError(
            "Statcast READY leaf population has an orphan or unknown parent"
        )
    if consumed_receipt_parents != set(children_by_parent):
        raise ValueError("Statcast split lineage contains an orphan or cycle")
    if generated_ids.intersection(root_ids):
        raise ValueError("Statcast split lineage contains a cycle")
    terminal_nodes = tuple(
        sorted(
            leaf_by_id.values(),
            key=lambda node: (node.first, node.last, node.query_id),
        )
    )
    parent_by_query = {
        query_id: query_by_id[query_id].parent_query_id for query_id in root_for_query
    }
    root_ordinal_by_id = {
        root.query_id: _CAREER_ROOT_ORDINAL_BY_START[root.first]
        for root in root_nodes
    }
    leaf_root_ordinal_by_id = {
        node.query_id: root_ordinal_by_id[root_for_query[node.query_id]]
        for node in terminal_nodes
    }
    leaf_receipt_hashes_by_id: dict[str, tuple[str, ...]] = {}
    for node in terminal_nodes:
        receipt_hashes: set[str] = set()
        parent_id = parent_by_query.get(node.query_id)
        visited: set[str] = set()
        while parent_id is not None:
            if parent_id in visited:
                raise ValueError("Statcast split lineage contains a cycle")
            visited.add(parent_id)
            receipt_hash = receipt_hash_by_parent.get(parent_id)
            if receipt_hash is not None:
                receipt_hashes.add(receipt_hash)
            parent_id = parent_by_query.get(parent_id)
        leaf_receipt_hashes_by_id[node.query_id] = tuple(sorted(receipt_hashes))

    root_run_end_by_ordinal: dict[int, int] = {}
    for ordinal in reversed(sorted(root_ordinal_by_id.values())):
        root_run_end_by_ordinal[ordinal] = root_run_end_by_ordinal.get(
            ordinal + 1, ordinal
        )

    lineage_prefix_by_baseline: dict[date, _CareerLineagePrefix] = {}
    for baseline_end in _CAREER_BASELINE_ENDS:
        nodes = tuple(node for node in terminal_nodes if node.first > baseline_end)
        sorted_leaf_hashes: list[str] = []
        cumulative_receipt_hashes: set[str] = set()
        leaf_hash_prefixes: list[tuple[str, ...]] = [()]
        receipt_hash_prefixes: list[tuple[str, ...]] = [()]
        for node in nodes:
            insort(sorted_leaf_hashes, leaf_hash_by_id[node.query_id])
            cumulative_receipt_hashes.update(
                leaf_receipt_hashes_by_id[node.query_id]
            )
            leaf_hash_prefixes.append(tuple(sorted_leaf_hashes))
            receipt_hash_prefixes.append(tuple(sorted(cumulative_receipt_hashes)))
        lineage_prefix_by_baseline[baseline_end] = _CareerLineagePrefix(
            nodes=nodes,
            first_dates=tuple(node.first for node in nodes),
            leaf_hashes=tuple(leaf_hash_prefixes),
            receipt_hashes=tuple(receipt_hash_prefixes),
        )
    return _CareerStatcastIndex(
        terminal_nodes=terminal_nodes,
        leaf_hash_by_id=dict(leaf_hash_by_id),
        pitcher_dates={key: tuple(values) for key, values in pitcher_dates.items()},
        root_for_query=dict(root_for_query),
        parent_by_query=parent_by_query,
        receipt_hash_by_parent=dict(receipt_hash_by_parent),
        leaf_root_ordinal_by_id=leaf_root_ordinal_by_id,
        leaf_receipt_hashes_by_id=leaf_receipt_hashes_by_id,
        root_run_end_by_ordinal=root_run_end_by_ordinal,
        lineage_prefix_by_baseline=lineage_prefix_by_baseline,
        lineage_cache={},
    )


def _career_lineage_for_cutoff(
    index: _CareerStatcastIndex,
    baseline_end: date,
    cutoff: date,
) -> tuple[list[_StatcastNode], list[str], list[str]]:
    key = (baseline_end, cutoff)
    cached = index.lineage_cache.get(key)
    if cached is not None:
        nodes, leaf_hashes, receipt_hashes = cached
        return list(nodes), list(leaf_hashes), list(receipt_hashes)

    boundary = SEASON_BOUNDARIES.get(cutoff.year)
    if boundary is None:
        raise ValueError("career consumer D-1 is outside the regular season")
    in_season = boundary[0] <= cutoff <= boundary[1]
    opening_day_cutoff = cutoff == boundary[0] - timedelta(days=1)
    if not in_season and not opening_day_cutoff:
        raise ValueError("career consumer D-1 is outside the regular season")
    first_year = baseline_end.year + 1
    start_ordinal = _CAREER_FIRST_ROOT_ORDINAL_BY_YEAR.get(first_year)
    if start_ordinal is None:
        raise ValueError("career baseline does not precede an approved Statcast season")
    if in_season:
        end_ordinal = bisect_right(_CAREER_ROOT_STARTS, cutoff) - 1
    else:
        end_ordinal = _CAREER_LAST_ROOT_ORDINAL_BY_YEAR.get(
            cutoff.year - 1, start_ordinal - 1
        )
    if end_ordinal >= start_ordinal and index.root_run_end_by_ordinal.get(
        start_ordinal, start_ordinal - 1
    ) < end_ordinal:
        raise ValueError("whole regular season Statcast coverage is omitted")

    prefix = index.lineage_prefix_by_baseline.get(baseline_end)
    if prefix is None:
        raise ValueError("career baseline cutoff differs from consumer D-1")
    end = bisect_right(prefix.first_dates, cutoff)
    cached = (
        prefix.nodes[:end],
        prefix.leaf_hashes[end],
        prefix.receipt_hashes[end],
    )
    index.lineage_cache[key] = cached
    nodes, leaf_hashes, receipt_hashes = cached
    return list(nodes), list(leaf_hashes), list(receipt_hashes)


def _recompute_career_bf_from_index(
    baseline_sidecars: Sequence[Mapping[str, object]],
    index: _CareerStatcastIndex,
) -> Mapping[str, object]:
    records: list[dict[str, object]] = []
    used: set[tuple[int, int]] = set()
    for sidecar in baseline_sidecars:
        semantics = _require_task2_sidecar(
            sidecar,
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            label="career aggregate",
        )
        pitcher_id = _positive(semantics.get("pitcher_id"), "baseline pitcher_id")
        baseline = semantics.get("baseline_batters_faced")
        consumers = semantics.get("consuming_game_pks")
        cutoffs = semantics.get("consumer_cutoff_dates")
        inclusive_end = _day(
            semantics.get("inclusive_end_date"), "career baseline inclusive end"
        )
        if (
            type(baseline) is not int
            or baseline < 0
            or not isinstance(consumers, list)
            or consumers != sorted(set(consumers))
            or not consumers
            or any(type(value) is not int or value <= 0 for value in consumers)
            or not isinstance(cutoffs, Mapping)
            or set(cutoffs) != {str(value) for value in consumers}
            or inclusive_end not in {date(2017, 12, 31), date(2021, 12, 31)}
            or semantics.get("requires_verified_statcast_bf_through_consumer_d1")
            is not True
        ):
            raise ValueError("career baseline semantics are invalid")
        baseline_hash = _artifact_sha256(sidecar)
        for game_pk in consumers:
            key = (pitcher_id, game_pk)
            if key in used:
                raise ValueError("duplicate career baseline consumer")
            cutoff = _day(cutoffs[str(game_pk)], "career consumer cutoff")
            expected_baseline_end = (
                date(2017, 12, 31)
                if cutoff <= date(2021, 12, 31)
                else date(2021, 12, 31)
            )
            if inclusive_end != expected_baseline_end:
                raise ValueError("career baseline cutoff differs from consumer D-1")
            lineage_key = (inclusive_end, cutoff)
            cached_lineage = index.lineage_cache.get(lineage_key)
            if cached_lineage is None:
                _nodes, leaf_hashes, receipt_hashes = _career_lineage_for_cutoff(
                    index, inclusive_end, cutoff
                )
            else:
                _nodes, cached_leaf_hashes, cached_receipt_hashes = cached_lineage
                leaf_hashes = list(cached_leaf_hashes)
                receipt_hashes = list(cached_receipt_hashes)
            dates = index.pitcher_dates.get(pitcher_id, ())
            verified_statcast_bf = bisect_right(dates, cutoff) - bisect_right(
                dates, inclusive_end
            )
            records.append(
                {
                    "pitcher_id": pitcher_id,
                    "game_pk": game_pk,
                    "consumer_cutoff_date": cutoff.isoformat(),
                    "baseline_inclusive_end_date": inclusive_end.isoformat(),
                    "baseline_batters_faced": baseline,
                    "verified_statcast_bf": verified_statcast_bf,
                    "total_batters_faced": baseline + verified_statcast_bf,
                    "baseline_query_sidecar_sha256": baseline_hash,
                    "statcast_ready_leaf_sidecar_sha256s": leaf_hashes,
                    "statcast_split_receipt_sha256s": receipt_hashes,
                }
            )
            used.add(key)
    if not records:
        raise ValueError("career aggregate requires at least one consumer")
    records.sort(key=lambda row: (row["pitcher_id"], row["game_pk"]))
    return {
        "schema_version": 1,
        "grain": "player_career_batters_faced_reference",
        "records": records,
    }


def recompute_career_bf_snapshot(
    baseline_sidecars: Sequence[Mapping[str, object]],
    *,
    statcast_ready_leaf_sidecars: Sequence[Mapping[str, object]],
    statcast_root_queries: Sequence[LogicalQuery],
    statcast_split_receipts: Sequence[SplitReceipt],
) -> Mapping[str, object]:
    """Globally index Statcast once, then compute every career-BF consumer."""
    index = _build_career_statcast_index(
        statcast_ready_leaf_sidecars,
        statcast_root_queries,
        statcast_split_receipts,
    )
    return _recompute_career_bf_from_index(baseline_sidecars, index)


def recompute_career_bf_season_snapshot(
    baseline_sidecars: Sequence[Mapping[str, object]],
    statcast_bf_additions: Sequence[Mapping[str, object]],
    *,
    statcast_root_queries: Sequence[LogicalQuery],
    statcast_split_receipts: Sequence[SplitReceipt],
) -> Mapping[str, object]:
    """Validate the legacy claim envelope over the shared global BF index."""
    if not statcast_bf_additions:
        raise ValueError("verified Statcast BF addition is missing")
    expected_keys = {
        "consumer_cutoff_date",
        "game_pk",
        "pitcher_id",
        "statcast_bf",
        "statcast_ready_leaf_sidecars",
        "statcast_ready_leaf_sidecar_sha256s",
    }
    unique_sidecars: dict[str, Mapping[str, object]] = {}
    canonical_by_id: dict[str, bytes] = {}
    claims: dict[tuple[int, int], dict[str, object]] = {}
    for row in statcast_bf_additions:
        if not isinstance(row, Mapping) or set(row) != expected_keys:
            raise ValueError("Statcast BF additions must contain READY leaf lineage")
        sidecars = row.get("statcast_ready_leaf_sidecars")
        hashes = row.get("statcast_ready_leaf_sidecar_sha256s")
        if (
            isinstance(sidecars, (str, bytes))
            or not isinstance(sidecars, Sequence)
            or isinstance(hashes, (str, bytes))
            or not isinstance(hashes, Sequence)
        ):
            raise ValueError("Statcast BF READY leaf lineage is invalid")
        claimed_hashes = list(hashes)
        if (
            any(
                not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                for digest in claimed_hashes
            )
            or claimed_hashes != sorted(set(claimed_hashes))
        ):
            raise ValueError("Statcast BF READY leaf lineage is invalid")
        query_ids: list[str] = []
        for sidecar in sidecars:
            if not isinstance(sidecar, Mapping):
                raise ValueError("Statcast BF READY leaf lineage is invalid")
            query_id = sidecar.get("query_id")
            if not isinstance(query_id, str) or _SHA256.fullmatch(query_id) is None:
                raise ValueError("Statcast BF READY leaf query identity is invalid")
            canonical = canonical_json_bytes(dict(sidecar))
            if query_id in canonical_by_id and canonical_by_id[query_id] != canonical:
                raise ValueError("Statcast BF READY leaf query is ambiguous")
            canonical_by_id[query_id] = canonical
            unique_sidecars[query_id] = sidecar
            query_ids.append(query_id)
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("Statcast BF READY leaf lineage is duplicated")
        pitcher_id = _positive(row.get("pitcher_id"), "addition pitcher_id")
        game_pk = _positive(row.get("game_pk"), "addition game_pk")
        cutoff = _day(row.get("consumer_cutoff_date"), "addition cutoff")
        statcast_bf = row.get("statcast_bf")
        if type(statcast_bf) is not int or statcast_bf < 0:
            raise ValueError("Statcast BF addition value is invalid")
        key = (pitcher_id, game_pk)
        if key in claims:
            raise ValueError("duplicate Statcast BF addition")
        claims[key] = {
            "consumer_cutoff_date": cutoff.isoformat(),
            "leaf_hashes": claimed_hashes,
            "statcast_bf": statcast_bf,
        }

    index = _build_career_statcast_index(
        tuple(unique_sidecars.values()),
        statcast_root_queries,
        statcast_split_receipts,
        require_complete_roots=False,
    )
    computed = _recompute_career_bf_from_index(baseline_sidecars, index)
    records = computed["records"]
    computed_by_key = {
        (record["pitcher_id"], record["game_pk"]): record for record in records
    }
    if set(computed_by_key) != set(claims):
        raise ValueError(
            "Statcast BF additions contain wrong pitcher, consumer, or source lineage"
        )
    authenticated_supply_hashes = set(index.leaf_hash_by_id.values())
    for key, claim in claims.items():
        record = computed_by_key[key]
        claimed_hashes = claim["leaf_hashes"]
        if (
            not set(claimed_hashes).issubset(authenticated_supply_hashes)
            or claimed_hashes != record["statcast_ready_leaf_sidecar_sha256s"]
            or claim["consumer_cutoff_date"] != record["consumer_cutoff_date"]
            or claim["statcast_bf"] != record["verified_statcast_bf"]
        ):
            raise ValueError("claimed Statcast BF differs from canonical evidence")
    return computed


__all__ = [
    "recompute_career_bf_snapshot",
    "recompute_career_bf_season_snapshot",
    "recompute_career_bf_sidecar",
    "recompute_pitching_season_snapshot",
    "recompute_pitching_sidecar",
]
