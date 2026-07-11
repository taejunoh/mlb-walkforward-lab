"""Deterministic, market-blind daily candidate ranking."""

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from numbers import Real


_REQUIRED_KEYS = frozenset(
    (
        "game_pk",
        "official_date",
        "pick_probability",
        "confidence",
        "starter_edge_abs",
        "scheduled_start_utc",
    )
)
_FORBIDDEN_KEY_MARKERS = (
    "market",
    "moneyline",
    "odds",
    "sportsbook",
    "bookmaker",
    "betting",
    "runline",
    "spread",
    "overunder",
    "impliedprobability",
    "americanprice",
    "decimalprice",
)


def _reject_market_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                canonical = re.sub(r"[^a-z0-9]", "", key.lower())
                if any(marker in canonical for marker in _FORBIDDEN_KEY_MARKERS):
                    raise ValueError(f"market-derived ranking key is forbidden: {key}")
            _reject_market_keys(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_market_keys(nested)


def _finite_number(value: object, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    if not low <= converted <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return converted


def _official_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("official_date must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("official_date must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("official_date must be a canonical ISO date")
    return value


def _scheduled_start(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("scheduled_start_utc must be a canonical UTC datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_start_utc must be a canonical UTC datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("scheduled_start_utc must be timezone-aware")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if parsed.utcoffset().total_seconds() != 0 or value != canonical:
        raise ValueError("scheduled_start_utc must be canonical UTC ending in Z")
    return parsed


def rank_daily(rows: list[dict]) -> list[dict]:
    """Validate and rank one official date without consulting market fields."""
    if rows is None:
        raise ValueError("rows must be iterable mappings")
    try:
        items = list(rows)
    except TypeError as exc:
        raise ValueError("rows must be iterable mappings") from exc
    if not items:
        return []

    seen_game_pks: set[int] = set()
    official_dates: set[str] = set()
    validated: list[tuple[dict, float, float, float, datetime]] = []
    for source in items:
        if not isinstance(source, Mapping):
            raise ValueError("ranking rows must be mappings")
        _reject_market_keys(source)
        missing = _REQUIRED_KEYS - set(source)
        if missing:
            raise ValueError(f"ranking row missing required keys: {sorted(missing)}")

        game_pk = source["game_pk"]
        if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
            raise ValueError("game_pk must be a positive integer")
        if game_pk in seen_game_pks:
            raise ValueError("game_pk values must be unique")
        seen_game_pks.add(game_pk)

        official_dates.add(_official_date(source["official_date"]))
        probability = _finite_number(
            source["pick_probability"], "pick_probability", 0.5, 0.9
        )
        confidence = _finite_number(source["confidence"], "confidence", 0.0, 10.0)
        starter_edge = _finite_number(
            source["starter_edge_abs"], "starter_edge_abs", 0.0, math.inf
        )
        scheduled_start = _scheduled_start(source["scheduled_start_utc"])
        validated.append(
            (dict(source), probability, confidence, starter_edge, scheduled_start)
        )

    if len(official_dates) != 1:
        raise ValueError("all ranking rows must share one official_date")

    validated.sort(
        key=lambda item: (
            -item[1],
            -item[2],
            -item[3],
            item[4],
            item[0]["game_pk"],
        )
    )
    return [
        dict(
            item[0],
            daily_rank=rank,
            top1="Yes" if rank == 1 else "No",
            top3="Yes" if rank <= 3 else "No",
        )
        for rank, item in enumerate(validated, 1)
    ]
