"""Pure arithmetic contracts for the DraftKings totals market overlay."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real


@dataclass(frozen=True, slots=True)
class SupportRates:
    """Unconditional over, under, and push rates for a totals sample."""

    over: float
    under: float
    push: float


@dataclass(frozen=True, slots=True)
class PriceSupport:
    """Conditional support and its margin over a quoted break-even rate."""

    conditional_support: float | None
    quoted_break_even: float
    support_margin: float | None


def validate_american_price(value: object) -> int:
    """Validate and return a DraftKings-style American price."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not (-10_000 <= value <= -100 or 100 <= value <= 10_000)
    ):
        raise ValueError("american price must be an integer from -10000 to -100 or 100 to 10000")
    return value


def validate_line_half_runs(value: object) -> int:
    """Validate and return a positive integer number of half-run ticks."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("line_half_runs must be a positive integer")
    return value


def american_net_profit(price: int) -> float:
    """Return net profit on a one-unit stake for an American price."""

    price = validate_american_price(price)
    if price > 0:
        return price / 100.0
    return 100.0 / abs(price)


def quoted_break_even(price: int) -> float:
    """Return the implied break-even probability for an American price."""

    return 1.0 / (1.0 + american_net_profit(price))


def compare_sample_support(
    samples: tuple[int, ...], line_half_runs: int
) -> SupportRates:
    """Classify sample totals against a half-run encoded totals line."""

    if not isinstance(samples, tuple) or not samples:
        raise ValueError("samples must be a non-empty tuple")
    for sample in samples:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise ValueError("samples must contain nonnegative integers")
    line_half_runs = validate_line_half_runs(line_half_runs)

    over = under = push = 0
    for sample in samples:
        doubled_sample = sample * 2
        if doubled_sample > line_half_runs:
            over += 1
        elif doubled_sample < line_half_runs:
            under += 1
        else:
            push += 1

    count = len(samples)
    return SupportRates(over / count, under / count, push / count)


def _finite_probability(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite probability")
    if value < 0 or value > 1:
        raise ValueError(f"{label} must be a finite probability between 0 and 1")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite probability") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite probability between 0 and 1")
    return result


def price_support(win_support: float, loss_support: float, price: int) -> PriceSupport:
    """Compare conditional directional support with the quoted price."""

    win_support = _finite_probability(win_support, "win_support")
    loss_support = _finite_probability(loss_support, "loss_support")
    if win_support + loss_support > 1.0:
        raise ValueError("win_support and loss_support must sum to at most 1")
    price = validate_american_price(price)
    break_even = quoted_break_even(price)

    directional_support = win_support + loss_support
    if directional_support == 0.0:
        return PriceSupport(None, break_even, None)
    conditional = win_support / directional_support
    return PriceSupport(conditional, break_even, conditional - break_even)


def select_direction(
    over: PriceSupport, under: PriceSupport
) -> tuple[str | None, tuple[str, ...]]:
    """Choose the uniquely better-supported side, if one exists."""

    over_margin = over.support_margin
    under_margin = under.support_margin
    if over_margin is not None and (under_margin is None or over_margin > under_margin):
        return "OVER", ()
    if under_margin is not None and (over_margin is None or under_margin > over_margin):
        return "UNDER", ()
    return None, ("AMBIGUOUS_DIRECTION",)
