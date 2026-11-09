from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from mlb_backtest.market_play_contracts import (
    EXPECTED_BOOKS,
    MlQuote,
    POLICY,
    REASON_ORDER,
    SUPPORTED_BOOK_IDS,
    TotalQuote,
    canonical_utc_ns,
    decimal_fraction,
    quote_age_ns,
    quoted_break_even_fraction,
    validate_american_price,
    validate_line_half_runs,
    validate_policy,
    validate_relative_path,
)


class MarketPlayContractsTests(unittest.TestCase):
    def test_exact_policy_boundaries(self):
        self.assertEqual(decimal_fraction("0.50"), Fraction(1, 2))
        self.assertEqual(decimal_fraction("0.05"), Fraction(1, 20))
        self.assertEqual(quoted_break_even_fraction(400), Fraction(1, 5))
        self.assertEqual(quoted_break_even_fraction(-150), Fraction(3, 5))

    def test_quote_age_accepts_300_seconds_not_one_nanosecond_more(self):
        evaluated = "2026-09-01T20:05:00Z"
        self.assertEqual(
            quote_age_ns("2026-09-01T20:00:00Z", evaluated),
            300_000_000_000,
        )
        self.assertGreater(
            quote_age_ns("2026-09-01T19:59:59.999999999Z", evaluated),
            300_000_000_000,
        )

    def test_canonical_utc_ns_rejects_noncanonical_or_lossy_times(self):
        for value in (
            "2026-09-01T20:00:00+00:00",
            "2026-09-01T20:00:60Z",
            "2026-09-01T20:00:00.1234567890Z",
            "2026-02-29T20:00:00Z",
            1_788_293_600,
        ):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                canonical_utc_ns(value)

    def test_closed_contracts_and_types(self):
        self.assertEqual(tuple(SUPPORTED_BOOK_IDS), EXPECTED_BOOKS)
        self.assertEqual(len(REASON_ORDER), len(set(REASON_ORDER)))
        self.assertEqual(validate_policy(POLICY), POLICY)
        with self.assertRaises(ValueError):
            validate_policy({**POLICY, "minimum_book_coverage": 2})
        quote = MlQuote(1, "FANDUEL", "event", "2026-09-01T20:00:00.123456789Z", -110, 100)
        self.assertEqual(quote.game_pk, 1)
        total = TotalQuote(1, "DRAFTKINGS", "event", "2026-09-01T20:00:00Z", 85, -105, -115)
        self.assertEqual(total.line_half_runs, 85)

    def test_validation_rejects_unsafe_values(self):
        for value in (0, 99, 10_001, True, "-110"):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                validate_american_price(value)
        for value in (0, -1, True, 8.5):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                validate_line_half_runs(value)
        for value in (Path("/absolute"), Path("../escape"), Path("a\\b"), Path(".")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_relative_path(value)


if __name__ == "__main__":
    unittest.main()
