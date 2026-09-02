"""Closed contracts and exact arithmetic for the variable market-play layer."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any


SUPPORTED_BOOK_IDS = (
    "FANDUEL",
    "DRAFTKINGS",
    "BETMGM",
    "CAESARS",
    "FANATICS",
)
EXPECTED_BOOKS = SUPPORTED_BOOK_IDS

POLICY = {
    "schema": "PROVISIONAL_MULTIBOOK_MARKET_PLAYS_POLICY_V1",
    "maximum_quote_age_seconds": 300,
    "minimum_book_coverage": 3,
    "minimum_data_completeness": "0.50",
    "minimum_ml_price_margin": "0.05",
    "required_residual_count": 764,
    "minimum_nonpush_support": "0.80",
    "minimum_point_gap_runs": "1.00",
    "minimum_total_support_margin": "0.05",
}

REASON_ORDER = (
    "GAME_STARTED",
    "INCOMPLETE_PARTICIPANTS",
    "STARTER_CONFLICT",
    "LATE_SCRATCH",
    "INSUFFICIENT_BOOK_COVERAGE",
    "STALE_PRICE",
    "NO_QUOTE",
    "MODEL_UNSCORED",
    "DATA_COMPLETENESS_TOO_LOW",
    "PRICE_MARGIN_TOO_LOW",
    "RESIDUAL_COUNT_MISMATCH",
    "NONPUSH_SUPPORT_TOO_LOW",
    "POINT_GAP_TOO_SMALL",
    "SUPPORT_MARGIN_TOO_LOW",
    "AMBIGUOUS_DIRECTION",
)

_UTC_RE = re.compile(
    r"^(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T"
    r"(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?P<fraction>\.[0-9]{1,9})?Z$"
)
_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)\.[0-9]+$")


def canonical_utc(value: object) -> str:
    """Validate and return an RFC3339 UTC instant without lossy conversion."""
    if not isinstance(value, str):
        raise TypeError("UTC timestamp must be a string")
    match = _UTC_RE.fullmatch(value)
    if match is None:
        raise ValueError("UTC timestamp must be YYYY-MM-DDTHH:MM:SS[.fraction]Z")
    year, month, day = (int(part) for part in match.group("date").split("-"))
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second"))
    if second > 59:
        raise ValueError("leap seconds are not accepted")
    try:
        # Validation only; freshness arithmetic uses integer epoch nanoseconds.
        import datetime as _datetime

        _datetime.datetime(year, month, day, hour, minute, second)
    except (ValueError, OverflowError) as exc:
        raise ValueError("UTC timestamp has an invalid calendar value") from exc
    return value


def canonical_utc_ns(value: object) -> int:
    """Parse a canonical UTC string into integer epoch nanoseconds."""
    text = canonical_utc(value)
    match = _UTC_RE.fullmatch(text)
    assert match is not None
    year, month, day = (int(part) for part in match.group("date").split("-"))
    seconds = calendar.timegm(
        (
            year,
            month,
            day,
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
        )
    )
    fraction = match.group("fraction")
    fraction_ns = int(fraction[1:].ljust(9, "0")) if fraction else 0
    return seconds * 1_000_000_000 + fraction_ns


def quote_age_ns(captured: str, evaluated: str) -> int:
    """Return exact non-negative age in nanoseconds."""
    delta_ns = canonical_utc_ns(evaluated) - canonical_utc_ns(captured)
    if delta_ns < 0:
        raise ValueError("quote timestamp is in the future")
    return delta_ns


def validate_american_price(price: object) -> int:
    if isinstance(price, bool) or not isinstance(price, int):
        raise TypeError("American price must be an integer")
    if price == 0 or not 100 <= abs(price) <= 10_000:
        raise ValueError("American price absolute value must be in [100, 10000]")
    return price


def validate_line_half_runs(line_half_runs: object) -> int:
    if isinstance(line_half_runs, bool) or not isinstance(line_half_runs, int):
        raise TypeError("total line must be an integer half-run tick")
    if line_half_runs <= 0:
        raise ValueError("total line must be positive half-run ticks")
    return line_half_runs


def quoted_break_even_fraction(price: int) -> Fraction:
    price = validate_american_price(price)
    if price > 0:
        return Fraction(100, price + 100)
    return Fraction(abs(price), abs(price) + 100)


def decimal_fraction(value: object, label: str = "decimal") -> Fraction:
    if not isinstance(value, str) or _DECIMAL_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical decimal string")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label} is not numeric") from exc
    return Fraction(decimal)


def validate_relative_path(relative: object) -> Path:
    if not isinstance(relative, Path):
        raise TypeError("relative path must be pathlib.Path")
    parts = relative.parts
    if (
        relative.is_absolute()
        or not parts
        or any(not part or part in (".", "..") or "\\" in part for part in parts)
    ):
        raise ValueError("path must be a strict POSIX-relative path")
    return relative


def canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be canonical JSON compatible") from exc


def content_digest(value: object) -> str:
    payload = value if isinstance(value, bytes) else canonical_json_bytes(value)
    return hashlib.sha256(payload).hexdigest()


def sidecar_bytes(raw: bytes) -> bytes:
    if not isinstance(raw, bytes):
        raise TypeError("sidecar input must be bytes")
    return (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii")


def validate_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value != POLICY:
        raise ValueError("market-play policy is closed and differs from POLICY")
    return dict(value)


@dataclass(frozen=True, slots=True)
class MlQuote:
    game_pk: int
    book_id: str
    event_id: str
    captured_at_utc: str
    away_american_odds: int
    home_american_odds: int

    def __post_init__(self) -> None:
        if isinstance(self.game_pk, bool) or not isinstance(self.game_pk, int) or self.game_pk <= 0:
            raise ValueError("game_pk must be a positive integer")
        if self.book_id not in SUPPORTED_BOOK_IDS:
            raise ValueError("unsupported sportsbook")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be non-empty")
        canonical_utc(self.captured_at_utc)
        validate_american_price(self.away_american_odds)
        validate_american_price(self.home_american_odds)


@dataclass(frozen=True, slots=True)
class TotalQuote:
    game_pk: int
    book_id: str
    event_id: str
    captured_at_utc: str
    line_half_runs: int
    over_american_odds: int
    under_american_odds: int

    def __post_init__(self) -> None:
        if isinstance(self.game_pk, bool) or not isinstance(self.game_pk, int) or self.game_pk <= 0:
            raise ValueError("game_pk must be a positive integer")
        if self.book_id not in SUPPORTED_BOOK_IDS:
            raise ValueError("unsupported sportsbook")
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("event_id must be non-empty")
        canonical_utc(self.captured_at_utc)
        validate_line_half_runs(self.line_half_runs)
        validate_american_price(self.over_american_odds)
        validate_american_price(self.under_american_odds)
