from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from .models import GameClock


def make_game_clock(
    game_pk: int,
    official_date: str,
    start_utc: str,
    lock_minutes: int,
) -> GameClock:
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        raise ValueError("game_pk must be a positive integer")
    if (
        isinstance(lock_minutes, bool)
        or not isinstance(lock_minutes, int)
        or lock_minutes <= 0
    ):
        raise ValueError("lock_minutes must be a positive integer")

    try:
        day = date.fromisoformat(official_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("official_date must be an ISO date") from exc
    try:
        start = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("start_utc must be an ISO datetime") from exc
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start_utc must be timezone-aware")

    start = start.astimezone(timezone.utc)
    return GameClock(
        game_pk=game_pk,
        official_date=day,
        scheduled_start_utc=start,
        lock_at_utc=start - timedelta(minutes=lock_minutes),
        feature_as_of=day - timedelta(days=1),
    )


def assert_feature_dates(target_date: str, source_dates: Iterable[str]) -> None:
    try:
        target = date.fromisoformat(target_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_date must be an ISO date") from exc

    for value in source_dates:
        try:
            source_date = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("source dates must be ISO dates") from exc
        if source_date >= target:
            raise ValueError("same-day leakage detected")
