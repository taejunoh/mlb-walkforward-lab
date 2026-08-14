"""Confirmed-lineup feature helpers for the frozen v5.1-BT model."""

import math
from datetime import date, timedelta
from types import MappingProxyType
from typing import Mapping, Sequence


WEIGHTS = MappingProxyType(
    {
        "platoon_xwoba": 0.50,
        "last14_xwoba": 0.20,
        "bb_minus_k": 0.15,
        "barrel_rate": 0.10,
        "iso": 0.05,
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


def _validate_lineup(lineup: Sequence[Mapping[str, object]]) -> None:
    message = "exactly nine starting hitters with ordered slots are required"
    if isinstance(lineup, (str, bytes)) or len(lineup) != 9:
        raise ValueError(message)
    player_ids: set[int] = set()
    for expected_slot, player in enumerate(lineup, start=1):
        if not isinstance(player, Mapping):
            raise ValueError(message)
        player_id = player.get("player_id")
        if (
            isinstance(player_id, bool)
            or not isinstance(player_id, int)
            or player_id in player_ids
        ):
            raise ValueError(message)
        if "batting_slot" in player:
            slot = player["batting_slot"]
            if (
                isinstance(slot, bool)
                or not isinstance(slot, int)
                or slot != expected_slot
            ):
                raise ValueError(message)
        player_ids.add(player_id)


def offense_score(
    lineup: Sequence[Mapping[str, object]], metric_scores: Mapping[str, float]
) -> float:
    """Validate the starting nine and return the frozen offense score."""

    _validate_lineup(lineup)
    if not isinstance(metric_scores, Mapping) or set(metric_scores) != set(WEIGHTS):
        raise ValueError("offense metric set changed")
    scores = {
        name: _bounded(metric_scores[name], name, 0.0, 100.0) for name in WEIGHTS
    }
    result = sum(WEIGHTS[name] * scores[name] for name in WEIGHTS)
    if not math.isfinite(result) or not 0.0 <= result <= 100.0:
        raise ValueError("offense score must be finite and between 0 and 100")
    return result


def select_prior_hitter_events(
    events: Sequence[Mapping[str, object]],
    target_date: date | str,
    days: int = 14,
) -> list[Mapping[str, object]]:
    """Return hitter events in ``[target-days, target-1]``, newest first."""

    target = _date(target_date, "target_date")
    if isinstance(days, bool) or not isinstance(days, int) or days != 14:
        raise ValueError("hitter event window must be exactly 14 days")
    first_date = target - timedelta(days=days)
    dated: list[
        tuple[date, tuple[int, int | str], Mapping[str, object]]
    ] = []
    seen_ids: set[object] = set()
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("each event must be a mapping")
        game_date = _date(event.get("game_date"), "game_date")
        event_id = event.get("event_id")
        if isinstance(event_id, bool):
            raise ValueError("event_id must be an integer or nonempty string")
        if isinstance(event_id, int):
            event_key: tuple[int, int | str] = (0, event_id)
        elif isinstance(event_id, str) and event_id:
            event_key = (1, event_id)
        else:
            raise ValueError("event_id must be an integer or nonempty string")
        if event_id in seen_ids:
            raise ValueError("duplicate event_id")
        seen_ids.add(event_id)
        if first_date <= game_date < target:
            dated.append((game_date, event_key, event))
    dated.sort(key=lambda item: (-item[0].toordinal(), item[1]))
    return [item[2] for item in dated]


def derive_slot_weights(
    training_rows: Sequence[Mapping[str, object]],
) -> dict[int, float]:
    """Derive frozen opportunity weights from 2022-2024 regular-season PA."""

    totals = {slot: 0.0 for slot in range(1, 10)}
    for row in training_rows:
        if not isinstance(row, Mapping):
            raise ValueError("each training row must be a mapping")
        season = row.get("season")
        slot = row.get("slot", row.get("batting_slot"))
        if "slot" in row and "batting_slot" in row and row["slot"] != row["batting_slot"]:
            raise ValueError("slot and batting_slot must agree")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in {2022, 2023, 2024}
        ):
            raise ValueError("training season must be from 2022 through 2024")
        if row.get("game_type") != "R":
            raise ValueError("only regular-season rows are allowed")
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in totals:
            raise ValueError("batting_slot must be an integer from 1 through 9")
        pa = _finite_number(row.get("pa"), "pa")
        if pa <= 0.0:
            raise ValueError("pa must be positive")
        totals[slot] += pa

    if any(value == 0.0 for value in totals.values()):
        raise ValueError("all batting slots must be present")
    total_pa = sum(totals.values())
    if total_pa <= 0.0 or not math.isfinite(total_pa):
        raise ValueError("total pa must be positive and finite")
    return {slot: totals[slot] / total_pa for slot in totals}


def weighted_lineup_metric(
    lineup: Sequence[Mapping[str, object]],
    player_metric_map: Mapping[int, Mapping[str, float]],
    slot_weights: Mapping[int, float],
    neutral: float = 50.0,
) -> tuple[float, float]:
    """Return score and quality without redistributing missing-player weight."""

    _validate_lineup(lineup)
    neutral_score = _bounded(neutral, "neutral", 0.0, 100.0)
    if (
        not isinstance(slot_weights, Mapping)
        or len(slot_weights) != 9
        or any(isinstance(slot, bool) or not isinstance(slot, int) for slot in slot_weights)
        or set(slot_weights) != set(range(1, 10))
    ):
        raise ValueError("slot weights must have exactly slots 1 through 9")
    weights = {
        slot: _bounded(slot_weights[slot], f"slot weight {slot}", 0.0, 1.0)
        for slot in range(1, 10)
    }
    if not math.isclose(
        sum(weights.values()), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("slot weights must sum to 1")
    if not isinstance(player_metric_map, Mapping):
        raise ValueError("player_metric_map must be a mapping")

    weighted_score = 0.0
    weighted_quality = 0.0
    for slot, player in enumerate(lineup, start=1):
        player_id = player["player_id"]
        metric = player_metric_map.get(player_id)
        if metric is None:
            score = neutral_score
            quality = 0.0
        else:
            if not isinstance(metric, Mapping) or set(metric) != {"score", "quality"}:
                raise ValueError("player metrics require score and quality")
            score = _bounded(metric["score"], "player score", 0.0, 100.0)
            quality = _bounded(metric["quality"], "player quality", 0.0, 1.0)
        weighted_score += weights[slot] * score
        weighted_quality += weights[slot] * quality
    return weighted_score, weighted_quality
