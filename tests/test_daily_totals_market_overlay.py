from __future__ import annotations

import math
import unittest
from dataclasses import FrozenInstanceError

from mlb_backtest.daily_totals_market_overlay import (
    PriceSupport,
    SupportRates,
    american_net_profit,
    compare_sample_support,
    price_support,
    quoted_break_even,
    select_direction,
    validate_american_price,
    validate_line_half_runs,
)


class PureMathTests(unittest.TestCase):
    def test_price_rules_accept_signed_integer_range(self) -> None:
        self.assertEqual(validate_american_price(100), 100)
        self.assertEqual(validate_american_price(-10000), -10000)
        for value in (0, 99, -99, 100.0, True, "-110", 10001, -10001):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_american_price(value)

    def test_half_run_ticks_are_positive_integers(self) -> None:
        self.assertEqual(validate_line_half_runs(19), 19)
        for value in (0, -1, 18.5, True, "19"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_line_half_runs(value)

    def test_dataclasses_are_frozen_and_slot_based(self) -> None:
        rates = SupportRates(0.25, 0.5, 0.25)
        support = PriceSupport(0.5, 110 / 210, 0.5 - 110 / 210)
        self.assertFalse(hasattr(rates, "__dict__"))
        self.assertFalse(hasattr(support, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            rates.over = 0.75  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            support.quoted_break_even = 0.5  # type: ignore[misc]

    def test_compare_sample_support_uses_integer_half_run_identity(self) -> None:
        self.assertEqual(
            compare_sample_support((7, 8, 8, 9), 16),
            SupportRates(0.25, 0.25, 0.50),
        )
        self.assertEqual(
            compare_sample_support((7, 8, 8, 9), 17),
            SupportRates(0.25, 0.75, 0.0),
        )

    def test_compare_sample_support_rejects_invalid_container(self) -> None:
        for samples in ((), [7, 8], "78", {7, 8}):
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError):
                    compare_sample_support(samples, 16)  # type: ignore[arg-type]

    def test_compare_sample_support_rejects_invalid_samples(self) -> None:
        for samples in ((-1,), (1.0,), (True,), (1, False)):
            with self.subTest(samples=samples):
                with self.assertRaises(ValueError):
                    compare_sample_support(samples, 16)

    def test_compare_sample_support_rejects_invalid_line(self) -> None:
        for line_half_runs in (0, -1, 16.0, True, "16"):
            with self.subTest(line_half_runs=line_half_runs):
                with self.assertRaises(ValueError):
                    compare_sample_support((7,), line_half_runs)  # type: ignore[arg-type]

    def test_american_price_math(self) -> None:
        self.assertEqual(american_net_profit(150), 1.5)
        self.assertEqual(american_net_profit(-200), 0.5)
        self.assertEqual(quoted_break_even(-110), 110 / 210)

    def test_price_support_uses_conditional_support_and_margin(self) -> None:
        result = price_support(0.75, 0.25, -110)
        self.assertEqual(result.conditional_support, 0.75)
        self.assertEqual(result.quoted_break_even, 110 / 210)
        self.assertEqual(result.support_margin, 0.75 - 110 / 210)

    def test_price_support_returns_none_for_all_pushes(self) -> None:
        self.assertEqual(
            price_support(0.0, 0.0, 100),
            PriceSupport(None, 0.5, None),
        )

    def test_price_support_rejects_invalid_probabilities(self) -> None:
        invalid = (True, "0.5", float("nan"), float("inf"), -0.01, 1.01)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    price_support(value, 0.0, 100)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            price_support(0.6, 0.41, 100)

    def test_price_support_rejects_invalid_price(self) -> None:
        with self.assertRaises(ValueError):
            price_support(0.5, 0.5, 99)

    def test_select_direction_chooses_strictly_larger_margin(self) -> None:
        over = price_support(0.8, 0.2, -110)
        under = price_support(0.6, 0.4, -110)
        self.assertEqual(select_direction(over, under), ("OVER", ()))
        self.assertEqual(select_direction(under, over), ("UNDER", ()))

    def test_select_direction_marks_tied_margins_ambiguous(self) -> None:
        over = PriceSupport(0.6, 0.5, 0.1)
        under = PriceSupport(0.7, 0.5, 0.1)
        self.assertEqual(select_direction(over, under), (None, ("AMBIGUOUS_DIRECTION",)))

    def test_select_direction_handles_unavailable_side(self) -> None:
        available = PriceSupport(0.55, 0.5, 0.05)
        unavailable = PriceSupport(None, 0.5, None)
        self.assertEqual(select_direction(available, unavailable), ("OVER", ()))
        self.assertEqual(select_direction(unavailable, available), ("UNDER", ()))
        self.assertEqual(
            select_direction(unavailable, unavailable),
            (None, ("AMBIGUOUS_DIRECTION",)),
        )

    def test_support_rates_are_exact_counts_over_sample_count(self) -> None:
        result = compare_sample_support((0, 1, 2), 2)
        self.assertTrue(math.isclose(result.over, 1 / 3))
        self.assertTrue(math.isclose(result.under, 1 / 3))
        self.assertEqual(result.push, 1 / 3)


if __name__ == "__main__":
    unittest.main()
