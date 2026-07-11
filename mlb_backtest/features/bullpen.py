"""Bullpen feature helpers for the frozen v5.1-BT model."""

import math
from collections import defaultdict
from datetime import date, timedelta
from types import MappingProxyType
from typing import Mapping, Sequence


WEIGHTS = MappingProxyType(
    {
        "season_xwoba": 0.35,
        "k_minus_bb": 0.20,
        "last14_xwoba": 0.15,
        "availability": 0.30,
    }
)


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _bounded(value: object, name: str, low: float, high: float) -> float:
    result = _finite_number(value, name)
    if not low <= result <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return result


def _date(value: object, name: str) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO date") from exc
    raise ValueError(f"{name} must be a date or ISO date")


def _event_id(value: object) -> tuple[int, int | str]:
    if isinstance(value, bool):
        raise ValueError("event_id must be an integer or nonempty string")
    if isinstance(value, int):
        return (0, value)
    if isinstance(value, str) and value:
        return (1, value)
    raise ValueError("event_id must be an integer or nonempty string")


def reliever_availability(pitches_by_prior_day: Sequence[int]) -> float:
    """Score a reliever from pitch counts on D-1, D-2, and D-3."""

    if isinstance(pitches_by_prior_day, (str, bytes)):
        raise ValueError("pitch history must be a sequence of at most three integers")
    try:
        pitches = list(pitches_by_prior_day)
    except TypeError as exc:
        raise ValueError(
            "pitch history must be a sequence of at most three integers"
        ) from exc
    if len(pitches) > 3:
        raise ValueError("pitch history cannot contain more than three days")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in pitches
    ):
        raise ValueError("pitch counts must be nonnegative integers")

    d1, d2, d3 = (pitches + [0, 0, 0])[:3]
    if d1 >= 25 or d1 + d2 >= 40 or all(value > 0 for value in (d1, d2, d3)):
        return 0.0
    if 15 <= d1 <= 24 or 30 <= d1 + d2 <= 39:
        return 0.5
    return 1.0


def leverage_pool(
    events: Sequence[Mapping[str, object]], target_date: date | str
) -> list[dict[str, int]]:
    """Return the top-five prior-30-day leverage relievers by batters faced."""

    target = _date(target_date, "target_date")
    first_date = target - timedelta(days=30)
    totals: defaultdict[int, int] = defaultdict(int)
    seen_ids: set[tuple[int, int | str]] = set()

    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("each event must be a mapping")
        event_key = _event_id(event.get("event_id"))
        if event_key in seen_ids:
            raise ValueError("duplicate event_id")
        seen_ids.add(event_key)

        game_date = _date(event.get("game_date"), "game_date")
        inning = event.get("inning")
        if isinstance(inning, bool) or not isinstance(inning, int) or inning < 1:
            raise ValueError("inning must be a positive integer")
        margin = _finite_number(event.get("score_margin"), "score_margin")
        pitcher_id = event.get("pitcher_id")
        if (
            isinstance(pitcher_id, bool)
            or not isinstance(pitcher_id, int)
            or pitcher_id <= 0
        ):
            raise ValueError("pitcher_id must be a positive integer")
        batters_faced = event.get("batters_faced")
        if (
            isinstance(batters_faced, bool)
            or not isinstance(batters_faced, int)
            or batters_faced < 0
        ):
            raise ValueError("batters_faced must be a nonnegative integer")
        is_reliever = event.get("is_reliever", True)
        if not isinstance(is_reliever, bool):
            raise ValueError("is_reliever must be boolean")

        if (
            first_date <= game_date < target
            and inning >= 7
            and abs(margin) <= 3
            and is_reliever
        ):
            totals[pitcher_id] += batters_faced

    ranked = sorted(
        ((pitcher_id, bf) for pitcher_id, bf in totals.items() if bf > 0),
        key=lambda item: (-item[1], item[0]),
    )[:5]
    return [
        {"pitcher_id": pitcher_id, "batters_faced": bf}
        for pitcher_id, bf in ranked
    ]


def team_availability(
    pool: Sequence[Mapping[str, object]],
    pitches_by_pitcher: Mapping[int, Sequence[int]],
) -> tuple[float, float]:
    """Return BF-weighted availability score and source quality for a pool."""

    if not isinstance(pitches_by_pitcher, Mapping):
        raise ValueError("pitches_by_pitcher must be a mapping")
    for pitcher_id in pitches_by_pitcher:
        if (
            isinstance(pitcher_id, bool)
            or not isinstance(pitcher_id, int)
            or pitcher_id <= 0
        ):
            raise ValueError("usage pitcher ids must be positive integers")

    total_bf = 0
    weighted_score = 0.0
    weighted_quality = 0.0
    seen_pitchers: set[int] = set()
    for reliever in pool:
        if not isinstance(reliever, Mapping) or set(reliever) != {
            "pitcher_id",
            "batters_faced",
        }:
            raise ValueError("pool entries require pitcher_id and batters_faced")
        pitcher_id = reliever["pitcher_id"]
        bf = reliever["batters_faced"]
        if (
            isinstance(pitcher_id, bool)
            or not isinstance(pitcher_id, int)
            or pitcher_id <= 0
            or pitcher_id in seen_pitchers
        ):
            raise ValueError("pool pitcher ids must be unique positive integers")
        if isinstance(bf, bool) or not isinstance(bf, int) or bf <= 0:
            raise ValueError("pool batters_faced must be positive integers")
        seen_pitchers.add(pitcher_id)

        usage = pitches_by_pitcher.get(pitcher_id)
        if usage is None:
            score = 50.0
            quality = 0.0
        else:
            score = 100.0 * reliever_availability(usage)
            quality = 1.0
        total_bf += bf
        weighted_score += bf * score
        weighted_quality += bf * quality

    if total_bf == 0:
        return 50.0, 0.0
    return weighted_score / total_bf, weighted_quality / total_bf


def bullpen_score(
    metric_parts: Mapping[str, Mapping[str, float]],
) -> tuple[float, float]:
    """Return the frozen weighted bullpen score and quality."""

    if not isinstance(metric_parts, Mapping) or set(metric_parts) != set(WEIGHTS):
        raise ValueError("bullpen metric set changed")
    score = 0.0
    quality = 0.0
    for name, weight in WEIGHTS.items():
        part = metric_parts[name]
        if not isinstance(part, Mapping) or set(part) != {"score", "quality"}:
            raise ValueError("bullpen metric parts require score and quality")
        score += weight * _bounded(part["score"], f"{name} score", 0.0, 100.0)
        quality += weight * _bounded(part["quality"], f"{name} quality", 0.0, 1.0)
    return score, quality
