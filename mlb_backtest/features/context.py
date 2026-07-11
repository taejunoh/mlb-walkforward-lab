"""Environment, defense, and schedule helpers for the frozen v5.1-BT model."""

import math
from types import MappingProxyType
from typing import Mapping, Sequence


ENVIRONMENT_WEIGHTS = MappingProxyType({"park": 0.60, "weather": 0.40})
DEFENSE_WEIGHTS = MappingProxyType(
    {"oaa": 0.55, "framing": 0.30, "catcher": 0.15}
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


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _weighted_component(
    parts: Sequence[Mapping[str, float] | None], weights: Sequence[float]
) -> tuple[float, float]:
    score = 0.0
    quality = 0.0
    for part, weight in zip(parts, weights, strict=True):
        if part is None:
            part_score = 50.0
            part_quality = 0.0
        else:
            if not isinstance(part, Mapping) or set(part) != {"score", "quality"}:
                raise ValueError("component parts require score and quality")
            part_score = _bounded(part["score"], "component score", 0.0, 100.0)
            part_quality = _bounded(part["quality"], "component quality", 0.0, 1.0)
        score += weight * part_score
        quality += weight * part_quality
    return score, quality


def environment_score(
    park: Mapping[str, float] | None,
    weather: Mapping[str, float] | None,
) -> tuple[float, float]:
    """Combine park and weather without redistributing missing weight."""

    return _weighted_component(
        (park, weather), tuple(ENVIRONMENT_WEIGHTS.values())
    )


def defense_score(
    oaa: Mapping[str, float] | None,
    framing: Mapping[str, float] | None,
    catcher: Mapping[str, float] | None,
) -> tuple[float, float]:
    """Combine fielding and catcher values without redistributing missing weight."""

    return _weighted_component(
        (oaa, framing, catcher), tuple(DEFENSE_WEIGHTS.values())
    )


def schedule_score(
    *,
    rest_day: bool,
    home_days: int,
    prior_innings: int,
    miles: float,
    time_zones: int,
    doubleheader: bool,
) -> float:
    """Return the frozen pregame schedule, travel, and rest score."""

    if not isinstance(rest_day, bool) or not isinstance(doubleheader, bool):
        raise ValueError("rest_day and doubleheader must be boolean")
    home_days_value = _nonnegative_integer(home_days, "home_days")
    prior_innings_value = _nonnegative_integer(prior_innings, "prior_innings")
    miles_value = _finite_number(miles, "miles")
    if miles_value < 0.0:
        raise ValueError("miles must be nonnegative")
    time_zones_value = _nonnegative_integer(time_zones, "time_zones")

    score = 50.0
    if rest_day:
        score += 12.0
    if home_days_value >= 3:
        score += 5.0
    if prior_innings_value >= 10:
        score -= 12.0
    if miles_value >= 1000.0 and not rest_day:
        score -= 10.0
    if time_zones_value >= 2 and not rest_day:
        score -= 6.0
    if doubleheader:
        score -= 8.0
    return min(100.0, max(0.0, score))
