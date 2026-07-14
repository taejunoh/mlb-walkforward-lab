"""Frozen regular-season boundaries shared by v5.2 planning and evidence."""

from __future__ import annotations

from datetime import date, timedelta
from types import MappingProxyType
from typing import Final, Mapping


SEASON_BOUNDARIES: Final[Mapping[int, tuple[date, date]]] = MappingProxyType(
    {
        2018: (date(2018, 3, 29), date(2018, 9, 30)),
        2019: (date(2019, 3, 20), date(2019, 9, 29)),
        2020: (date(2020, 7, 23), date(2020, 9, 27)),
        2021: (date(2021, 4, 1), date(2021, 10, 3)),
        2022: (date(2022, 4, 7), date(2022, 10, 5)),
        2023: (date(2023, 3, 30), date(2023, 10, 1)),
        2024: (date(2024, 3, 20), date(2024, 9, 30)),
    }
)


def monthly_ranges(first: date, last: date) -> tuple[tuple[date, date], ...]:
    """Partition an inclusive interval into canonical calendar-month children."""
    if not isinstance(first, date) or not isinstance(last, date) or first > last:
        raise ValueError("monthly range must be a nonempty ordered date interval")
    result = []
    cursor = first
    while cursor <= last:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        chunk_end = min(last, next_month - timedelta(days=1))
        result.append((cursor, chunk_end))
        cursor = next_month
    return tuple(result)


__all__ = ["SEASON_BOUNDARIES", "monthly_ranges"]
