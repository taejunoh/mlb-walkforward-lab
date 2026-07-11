"""Frozen v5.1 component scoring and pick classification."""

import math
from collections.abc import Mapping
from numbers import Integral, Real
from types import MappingProxyType

from . import normalize


WEIGHTS = MappingProxyType(
    {
        "starter": 0.35,
        "offense": 0.25,
        "bullpen": 0.20,
        "environment": 0.10,
        "defense": 0.05,
        "schedule": 0.05,
    }
)
_CORE_COMPONENTS = frozenset(("starter", "offense", "bullpen"))
_UNCERTAINTY_LABELS = frozenset(("High", "Medium", "Low"))


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    return converted


def _bounded_number(value: object, name: str, low: float, high: float) -> float:
    converted = _finite_number(value, name)
    if not low <= converted <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return converted


def _validated_values(
    values: Mapping[str, object],
    expected_keys: frozenset[str],
    name: str,
    low: float,
    high: float,
) -> dict[str, float]:
    if not isinstance(values, Mapping) or set(values) != expected_keys:
        raise ValueError(f"{name} must contain exactly {sorted(expected_keys)}")
    return {
        key: _bounded_number(values[key], f"{name}.{key}", low, high)
        for key in expected_keys
    }


def component_edge(
    home: Mapping[str, object], away: Mapping[str, object]
) -> float:
    """Return the weighted home-minus-away edge on the 0-100 scale."""
    keys = frozenset(WEIGHTS)
    home_scores = _validated_values(home, keys, "home", 0.0, 100.0)
    away_scores = _validated_values(away, keys, "away", 0.0, 100.0)
    return math.fsum(
        WEIGHTS[key] * (home_scores[key] - away_scores[key]) for key in WEIGHTS
    )


def component_qualities(qualities: Mapping[str, object]) -> float:
    """Return frozen-weight overall completeness for six component qualities."""
    validated = _validated_values(
        qualities, frozenset(WEIGHTS), "qualities", 0.0, 1.0
    )
    return math.fsum(WEIGHTS[key] * validated[key] for key in WEIGHTS)


def overall_completeness(qualities: Mapping[str, object]) -> float:
    return component_qualities(qualities)


def home_probability(edge: float, alpha: float, beta: float) -> float:
    """Calibrate a final edge through the shared frozen normalizer."""
    edge_value = _finite_number(edge, "edge")
    alpha_value = _finite_number(alpha, "alpha")
    beta_value = _finite_number(beta, "beta")
    return normalize.probability(
        edge_value,
        alpha_value,
        beta_value,
        clip_bounds=(0.10, 0.90),
    )


def predicted_winner(
    home_team: str, away_team: str, p_home: float
) -> dict[str, object]:
    """Choose a predicted winner; an exact 50% tie deterministically chooses home."""
    if not isinstance(home_team, str) or not home_team.strip():
        raise ValueError("home_team must be a non-empty string")
    if not isinstance(away_team, str) or not away_team.strip():
        raise ValueError("away_team must be a non-empty string")
    probability = _bounded_number(p_home, "p_home", 0.0, 1.0)
    if probability >= 0.5:
        return {
            "side": "home",
            "name": home_team,
            "pick_probability": probability,
        }
    return {
        "side": "away",
        "name": away_team,
        "pick_probability": 1.0 - probability,
    }


def component_agreement(edges: Mapping[str, object], predicted_side: str) -> float:
    validated = _validated_values(
        edges, _CORE_COMPONENTS, "edges", -100.0, 100.0
    )
    if predicted_side not in ("home", "away"):
        raise ValueError("predicted_side must be 'home' or 'away'")
    sign = 1.0 if predicted_side == "home" else -1.0
    return sum(sign * validated[key] > 0 for key in _CORE_COMPONENTS) / 3.0


def confidence_score(
    overall: float,
    starter: float,
    offense: float,
    bullpen: float,
    agreement: float,
    environment: float,
    schedule: float,
) -> float:
    values = [
        _bounded_number(value, name, 0.0, 1.0)
        for name, value in (
            ("overall", overall),
            ("starter", starter),
            ("offense", offense),
            ("bullpen", bullpen),
            ("agreement", agreement),
            ("environment", environment),
            ("schedule", schedule),
        )
    ]
    (
        overall_value,
        starter_value,
        offense_value,
        bullpen_value,
        agreement_value,
        environment_value,
        schedule_value,
    ) = values
    score = (
        2.5 * overall_value
        + 2.0 * starter_value
        + 1.5 * offense_value
        + 1.5 * bullpen_value
        + 1.5 * agreement_value
        + (2.0 * environment_value + schedule_value) / 3.0
    )
    return min(max(score, 0.0), 10.0)


def uncertainty(
    overall: float,
    critical_failure: bool,
    starter_career_bf: int,
    unresolved_context: bool,
) -> str:
    completeness = _bounded_number(overall, "overall", 0.0, 1.0)
    if not isinstance(critical_failure, bool):
        raise ValueError("critical_failure must be a bool")
    if (
        isinstance(starter_career_bf, bool)
        or not isinstance(starter_career_bf, Integral)
        or starter_career_bf < 0
    ):
        raise ValueError("starter_career_bf must be a non-negative integer")
    if not isinstance(unresolved_context, bool):
        raise ValueError("unresolved_context must be a bool")
    if critical_failure or completeness < 0.75:
        return "High"
    if completeness < 0.90 or starter_career_bf < 150 or unresolved_context:
        return "Medium"
    return "Low"


def tier(probability: float, confidence: float, uncertainty_label: str) -> str:
    pick_probability = _bounded_number(probability, "probability", 0.0, 1.0)
    confidence_value = _bounded_number(confidence, "confidence", 0.0, 10.0)
    if uncertainty_label not in _UNCERTAINTY_LABELS:
        raise ValueError("uncertainty_label must be High, Medium, or Low")
    if uncertainty_label == "High" or pick_probability < 0.54:
        return "Toss-up"
    if pick_probability >= 0.68 and confidence_value >= 9.0:
        return "S"
    if pick_probability >= 0.63 and confidence_value >= 8.5:
        return "A"
    if pick_probability >= 0.58 and confidence_value >= 8.0:
        return "B"
    return "Lean"


def full_agreement(
    edges: Mapping[str, object],
    predicted_side: str,
    uncertainty_label: str,
) -> str:
    if uncertainty_label not in _UNCERTAINTY_LABELS:
        raise ValueError("uncertainty_label must be High, Medium, or Low")
    return (
        "Yes"
        if component_agreement(edges, predicted_side) == 1.0
        and uncertainty_label != "High"
        else "No"
    )
