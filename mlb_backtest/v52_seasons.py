"""Frozen regular-season boundaries shared by v5.2 planning and evidence."""

from __future__ import annotations

from datetime import date
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


__all__ = ["SEASON_BOUNDARIES"]
