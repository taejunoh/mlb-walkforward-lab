"""D-1 league pitching and player career-BF source semantics."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date

from ..career_bf import parse_prior_career_bf
from ..mlb_stats import parse_league_era
from ..v52_evidence import LogicalQuery
from ..v52_source_contracts import (
    QUERY_SIDECAR_KEYS,
    canonical_json_bytes,
    query_sidecar,
    require_query,
)


_STATS_ENDPOINT = "https://statsapi.mlb.com/api/v1/stats"
_SEASON_OPEN = {
    2018: "2018-03-29",
    2019: "2019-03-20",
    2020: "2020-07-23",
    2021: "2021-04-01",
    2022: "2022-04-07",
    2023: "2023-03-30",
    2024: "2024-03-20",
}
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


def _pitching_group(raw: bytes) -> tuple[int, int]:
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
    if isinstance(season, bool) or not isinstance(season, int) or season not in _SEASON_OPEN:
        raise ValueError("pitching season must be within 2018-2024")
    season_open = _day(query.requested_range.get("start_date"), "pitching season open")
    if season_open.isoformat() != _SEASON_OPEN[season]:
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
    consumers, cutoffs = _consumer_cutoffs(query, consuming_game_pks)
    if any(as_of != cutoff for cutoff in cutoffs.values()):
        raise ValueError("pitching as-of date must equal every consumer D-1 cutoff")
    parsed = parse_league_era(raw)
    total_splits, batters_faced = _pitching_group(raw)
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
        endpoint=_STATS_ENDPOINT,
        expected_media_type="application/json",
    )
    pitcher_id = _positive(query.requested_range.get("pitcher_id"), "pitcher_id")
    inclusive_end = _day(
        query.requested_range.get("inclusive_end"), "career BF inclusive end"
    )
    expected_parameters = {
        "personId": pitcher_id,
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
        raise ValueError("legacy 2021-12-31 BF baseline is allowed only for 2022+ consumers")
    if any(inclusive_end > cutoff for cutoff in cutoffs.values()):
        raise ValueError("career BF inclusive end must be no later than every consumer D-1")
    if inclusive_end not in {date(2017, 12, 31), date(2021, 12, 31)}:
        raise ValueError("career BF baseline cutoff is not supported")
    baseline = parse_prior_career_bf(raw, pitcher_id)
    semantics = {
        "baseline_batters_faced": baseline,
        "consumer_cutoff_dates": {
            str(game_pk): cutoffs[game_pk].isoformat()
            for game_pk in consumers
        },
        "consuming_game_pks": consumers,
        "inclusive_end_date": inclusive_end.isoformat(),
        "pitcher_id": pitcher_id,
        "requires_verified_statcast_bf_through_consumer_d1": True,
    }
    verified = query.requested_range.get("verified_statcast_bf")
    if verified is not None:
        if not isinstance(verified, Mapping):
            raise ValueError("verified Statcast BF must be a mapping")
        try:
            additions = {int(key): value for key, value in verified.items()}
        except (TypeError, ValueError) as exc:
            raise ValueError("verified Statcast BF keys must be gamePks") from exc
        if set(additions) != set(consumers) or any(
            type(value) is not int or value < 0 for value in additions.values()
        ):
            raise ValueError("verified Statcast BF must cover every consumer")
        semantics["consumer_batters_faced"] = {
            str(game_pk): baseline + additions[game_pk] for game_pk in consumers
        }
        semantics["verified_statcast_bf"] = {
            str(game_pk): additions[game_pk] for game_pk in consumers
        }
    return query_sidecar(raw, query, semantics)


def _artifact_sha256(sidecar: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(sidecar)) + b"\n").hexdigest()


def _require_task2_sidecar(
    sidecar: Mapping[str, object], *, source: str, grain: str, label: str
) -> Mapping[str, object]:
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


def recompute_pitching_season_snapshot(
    query_sidecars: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    records = []
    seen_as_of: set[str] = set()
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


def recompute_career_bf_season_snapshot(
    baseline_sidecars: Sequence[Mapping[str, object]],
    statcast_bf_additions: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    additions: dict[tuple[int, int], Mapping[str, object]] = {}
    for row in statcast_bf_additions:
        if not isinstance(row, Mapping) or set(row) != {
            "consumer_cutoff_date",
            "game_pk",
            "pitcher_id",
            "statcast_bf",
            "statcast_query_sidecars",
            "statcast_query_sidecar_sha256s",
        }:
            raise ValueError("Statcast BF additions must be mappings")
        pitcher_id = _positive(row.get("pitcher_id"), "addition pitcher_id")
        game_pk = _positive(row.get("game_pk"), "addition game_pk")
        cutoff = _day(row.get("consumer_cutoff_date"), "addition cutoff")
        bf = row.get("statcast_bf")
        source_sidecars = row.get("statcast_query_sidecars")
        source_hashes = row.get("statcast_query_sidecar_sha256s")
        if (
            type(bf) is not int
            or bf < 0
            or not isinstance(source_sidecars, Sequence)
            or isinstance(source_sidecars, (str, bytes))
            or not source_sidecars
            or not isinstance(source_hashes, Sequence)
            or isinstance(source_hashes, (str, bytes))
        ):
            raise ValueError("Statcast BF addition value/source lineage is invalid")
        computed_hashes = []
        source_ranges = []
        for sidecar in source_sidecars:
            semantics = _require_task2_sidecar(
                sidecar,
                source="baseball_savant_statcast",
                grain="pitch",
                label="Statcast BF addition",
            )
            pitcher_ids = semantics.get("pitcher_ids")
            coverage = semantics.get("coverage")
            if (
                not isinstance(pitcher_ids, list)
                or pitcher_ids != sorted(set(pitcher_ids))
                or any(type(value) is not int or value <= 0 for value in pitcher_ids)
                or not isinstance(coverage, Mapping)
            ):
                raise ValueError("Statcast BF source pitcher population is invalid")
            first = _day(coverage.get("inclusive_start"), "Statcast BF source start")
            last = _day(coverage.get("inclusive_end"), "Statcast BF source end")
            if first > last:
                raise ValueError("Statcast BF source coverage is reversed")
            source_ranges.append((first, last))
            computed_hashes.append(_artifact_sha256(sidecar))
        if list(source_hashes) != computed_hashes or len(set(computed_hashes)) != len(
            computed_hashes
        ):
            raise ValueError("Statcast BF source hash differs from canonical sidecar")
        for previous, current in zip(source_ranges, source_ranges[1:]):
            if current[0] <= previous[1]:
                raise ValueError("Statcast BF source coverage overlaps or is unordered")
        if source_ranges[-1][1] != cutoff:
            raise ValueError("Statcast BF source coverage must end at consumer D-1")
        key = (pitcher_id, game_pk)
        if key in additions:
            raise ValueError("duplicate Statcast BF addition")
        additions[key] = {
            **row,
            "consumer_cutoff_date": cutoff.isoformat(),
            "source_first_date": source_ranges[0][0].isoformat(),
        }
    records = []
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
        for game_pk in consumers:
            key = (pitcher_id, game_pk)
            if key in used:
                raise ValueError("duplicate career baseline consumer")
            cutoff = _day(cutoffs[str(game_pk)], "career consumer cutoff")
            if inclusive_end > cutoff or (
                inclusive_end == date(2021, 12, 31) and cutoff < inclusive_end
            ):
                raise ValueError("career baseline cutoff differs from consumer D-1")
            addition = additions.get((pitcher_id, game_pk))
            if addition is None:
                raise ValueError("verified Statcast BF addition is missing")
            if addition["consumer_cutoff_date"] != cutoffs.get(str(game_pk)):
                raise ValueError("Statcast BF cutoff differs from career consumer D-1")
            if _day(addition["source_first_date"], "Statcast BF source start") <= inclusive_end:
                raise ValueError("Statcast BF source overlaps career baseline")
            used.add((pitcher_id, game_pk))
            records.append(
                {
                    "pitcher_id": pitcher_id,
                    "game_pk": game_pk,
                    "consumer_cutoff_date": addition["consumer_cutoff_date"],
                    "baseline_inclusive_end_date": inclusive_end.isoformat(),
                    "baseline_batters_faced": baseline,
                    "verified_statcast_bf": addition["statcast_bf"],
                    "total_batters_faced": baseline + addition["statcast_bf"],
                    "baseline_query_sidecar_sha256": _artifact_sha256(sidecar),
                    "statcast_query_sidecar_sha256s": list(
                        addition["statcast_query_sidecar_sha256s"]
                    ),
                }
            )
    if used != set(additions) or not records:
        raise ValueError("Statcast BF additions contain wrong pitcher, consumer, or source lineage")
    records.sort(key=lambda row: (row["pitcher_id"], row["game_pk"]))
    return {
        "schema_version": 1,
        "grain": "player_career_batters_faced_reference",
        "records": records,
    }


__all__ = [
    "recompute_career_bf_season_snapshot",
    "recompute_career_bf_sidecar",
    "recompute_pitching_season_snapshot",
    "recompute_pitching_sidecar",
]
