"""D-1 league pitching and player career-BF source semantics."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date

from ..career_bf import parse_prior_career_bf
from ..mlb_stats import parse_league_era
from ..v52_evidence import LogicalQuery
from ..v52_source_contracts import query_sidecar, require_query


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


__all__ = ["recompute_career_bf_sidecar", "recompute_pitching_sidecar"]
