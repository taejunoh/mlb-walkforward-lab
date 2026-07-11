"""Starting-pitcher feature helpers for the frozen v5.1-BT model."""

import math
import statistics
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Mapping, Sequence


WEIGHTS = MappingProxyType(
    {
        "season_xwoba": 0.35,
        "k_minus_bb": 0.20,
        "fip": 0.15,
        "last5_xwoba": 0.15,
        "expected_ip": 0.10,
        "velo_change": 0.05,
    }
)


@dataclass(frozen=True)
class ExpectedInnings:
    """Expected innings value and whether fewer than three starts were available."""

    value: float
    small_sample: bool


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _score(value: object, name: str) -> float:
    result = _finite_number(value, name)
    if not 0.0 <= result <= 100.0:
        raise ValueError(f"{name} must be between 0 and 100")
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


def starter_score(metric_scores: Mapping[str, float]) -> float:
    """Return the frozen weighted starter score."""

    if not isinstance(metric_scores, Mapping) or set(metric_scores) != set(WEIGHTS):
        raise ValueError("starter metric set changed")
    scores = {name: _score(metric_scores[name], name) for name in WEIGHTS}
    result = sum(WEIGHTS[name] * scores[name] for name in WEIGHTS)
    if not math.isfinite(result) or not 0.0 <= result <= 100.0:
        raise ValueError("starter score must be finite and between 0 and 100")
    return result


def select_prior_starts(
    events: Sequence[Mapping[str, object]],
    target_date: date | str,
    limit: int = 5,
) -> list[Mapping[str, object]]:
    """Select at most five unique starts strictly before ``target_date``."""

    target = _date(target_date, "target_date")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")

    eligible: list[tuple[date, int, Mapping[str, object]]] = []
    seen_game_pks: set[int] = set()
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("each event must be a mapping")
        game_date = _date(event.get("game_date"), "game_date")
        game_pk = event.get("game_pk")
        if isinstance(game_pk, bool) or not isinstance(game_pk, int):
            raise ValueError("game_pk must be an integer")
        if game_pk in seen_game_pks:
            raise ValueError("duplicate game_pk")
        seen_game_pks.add(game_pk)
        is_starter = event.get("is_starter")
        if not isinstance(is_starter, bool):
            raise ValueError("is_starter must be boolean")
        if is_starter and game_date < target:
            eligible.append((game_date, game_pk, event))

    eligible.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[Mapping[str, object]] = []
    for _, _, event in eligible:
        selected.append(event)
        if len(selected) == min(limit, 5):
            break
    return selected


def expected_innings(
    prior_starts: Sequence[Mapping[str, object]], rest_days: float
) -> ExpectedInnings:
    """Estimate innings from the latest five starts, represented by integer outs."""

    rest = _finite_number(rest_days, "rest_days")
    if rest < 0:
        raise ValueError("rest_days cannot be negative")

    innings: list[float] = []
    pitch_counts: list[float] = []
    for start in prior_starts[:5]:
        if not isinstance(start, Mapping):
            raise ValueError("each prior start must be a mapping")
        outs = start.get("outs")
        if isinstance(outs, bool) or not isinstance(outs, int) or outs < 0:
            raise ValueError("outs must be a nonnegative integer")
        pitch_count = _finite_number(start.get("pitch_count"), "pitch_count")
        if pitch_count < 0:
            raise ValueError("pitch_count cannot be negative")
        innings.append(outs / 3.0)
        pitch_counts.append(pitch_count)

    count = len(innings)
    if count == 0:
        baseline = 5.0
    else:
        baseline = min(7.0, max(3.0, statistics.median(innings)))
        if count < 3:
            reliability = count / (count + 3.0)
            baseline = reliability * baseline + (1.0 - reliability) * 5.0

    if pitch_counts and statistics.median(pitch_counts) < 70.0:
        baseline -= 1.0
    if rest < 4.0:
        baseline -= 0.5
    return ExpectedInnings(
        value=min(7.0, max(2.0, baseline)),
        small_sample=count < 3,
    )
