"""Pure moneyline evaluator tests."""

from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from mlb_backtest.market_play_authorities import (
    MlAuthority,
    MlGameAuthority,
    ParticipantGateEvidence,
)
from mlb_backtest.market_play_contracts import MlQuote
from mlb_backtest.multibook_market_board import MarketBoardAuthority, MarketBoardGame
from mlb_backtest.multibook_ml_evaluator import evaluate_moneylines, select_best_ml_quote


def _authority(*, completeness: str = "0.50", probability: str = "0.60") -> MlAuthority:
    game = MlGameAuthority(
        1,
        "2026-09-03T23:00:00Z",
        "Away",
        "Home",
        "home",
        Fraction(probability),
        Fraction(completeness),
        ParticipantGateEvidence(True, True, False, ()),
        "SCORED",
        None,
    )
    return MlAuthority((game,), "2026-09-03", Path("trace.json"), Path.cwd(), ())


class MultibookMlEvaluatorTests(unittest.TestCase):
    def test_best_price_and_exact_margin(self) -> None:
        quotes = tuple(
            MlQuote(1, book, book, "2026-09-03T22:59:00Z", -125, price)
            for book, price in (("FANDUEL", -125), ("DRAFTKINGS", -120), ("BETMGM", 100))
        )
        best = select_best_ml_quote("home", quotes)
        self.assertIsNotNone(best)
        self.assertEqual(best.book_id, "BETMGM")
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T23:00:00Z", quotes, ()),),
            Path("board.json"),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )
        row = evaluate_moneylines(_authority(), board, evaluated_at="2026-09-03T22:59:59Z")[0]
        self.assertEqual(row["decision"], "PLAY")
        self.assertEqual(row["selected_book"], "BETMGM")
        self.assertEqual(row["price_margin"], "0.10")

    def test_two_books_are_insufficient_and_unscored_is_preserved(self) -> None:
        quotes = tuple(
            MlQuote(1, book, book, "2026-09-03T22:59:00Z", 100, 100)
            for book in ("FANDUEL", "DRAFTKINGS")
        )
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T23:00:00Z", quotes, ()),),
            Path("board.json"), None, None,  # type: ignore[arg-type]
        )
        row = evaluate_moneylines(_authority(probability="0.55"), board, evaluated_at="2026-09-03T22:59:59Z")[0]
        self.assertEqual(row["decision"], "PASS")
        self.assertIn("INSUFFICIENT_BOOK_COVERAGE", row["reason_codes"])

    def test_stale_quote_does_not_penalize_three_fresh_books(self) -> None:
        quotes = tuple(
            MlQuote(1, book, book, captured, 100, 100)
            for book, captured in (
                ("FANDUEL", "2026-09-03T22:00:00Z"),
                ("DRAFTKINGS", "2026-09-03T22:59:00Z"),
                ("BETMGM", "2026-09-03T22:59:00Z"),
                ("CAESARS", "2026-09-03T22:59:00Z"),
            )
        )
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T23:00:00Z", quotes, ()),),
            Path("board.json"), None, None,  # type: ignore[arg-type]
        )
        row = evaluate_moneylines(_authority(), board, evaluated_at="2026-09-03T22:59:59Z")[0]
        self.assertEqual(row["decision"], "PLAY")
        self.assertNotIn("STALE_PRICE", row["reason_codes"])


if __name__ == "__main__":
    unittest.main()
