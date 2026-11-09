"""Pure totals evaluator tests."""

from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from mlb_backtest.market_play_authorities import (
    ParticipantGateEvidence,
    TotalsAuthority,
    TotalsGameAuthority,
)
from mlb_backtest.market_play_contracts import TotalQuote
from mlb_backtest.multibook_market_board import MarketBoardAuthority, MarketBoardGame
from mlb_backtest.multibook_totals_evaluator import (
    evaluate_total_candidate,
    evaluate_totals,
    select_total_play,
)


def _game(*, samples: tuple[float, ...] | None = None, residual_count: object = 764) -> TotalsGameAuthority:
    return TotalsGameAuthority(
        1,
        "2026-09-03T23:00:00Z",
        "Away",
        "Home",
        Fraction("5.00"),
        tuple(5 for _ in range(764)) if samples is None else samples,
        "PROJECTED",
        residual_count,
    )


def _quote(book: str, line: int = 8) -> TotalQuote:
    return TotalQuote(1, book, book, "2026-09-03T22:59:00Z", line, 100, 100)


class MultibookTotalsEvaluatorTests(unittest.TestCase):
    def test_variable_declared_residual_count_allows_770_samples(self) -> None:
        game = _game(samples=tuple(10 for _ in range(770)), residual_count=770)
        candidate = evaluate_total_candidate(game, _quote("FANDUEL"), "OVER")
        self.assertEqual(candidate.decision, "PLAY")
        self.assertEqual(candidate.over_count, 770)

    def test_declared_count_mismatch_is_a_pass_reason(self) -> None:
        game = _game(samples=tuple(10 for _ in range(770)), residual_count=769)
        candidate = evaluate_total_candidate(game, _quote("FANDUEL"), "OVER")
        self.assertEqual(candidate.decision, "PASS")
        self.assertIn("RESIDUAL_COUNT_MISMATCH", candidate.reason_codes)

    def test_malformed_or_boolean_declared_count_is_a_pass_reason(self) -> None:
        for value in (True, "770", 0):
            with self.subTest(value=value):
                candidate = evaluate_total_candidate(
                    _game(samples=tuple(10 for _ in range(770)), residual_count=value),
                    _quote("FANDUEL"),
                    "OVER",
                )
                self.assertEqual(candidate.decision, "PASS")
                self.assertIn("RESIDUAL_COUNT_MISMATCH", candidate.reason_codes)

    def test_malformed_same_length_samples_are_a_row_count_reason(self) -> None:
        samples = tuple(10 for _ in range(769)) + ("bad",)  # type: ignore[assignment]
        game = _game(samples=samples, residual_count=770)
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T23:00:00Z", (), (_quote("FANDUEL"),)),),
            Path("board.json"), None, None,  # type: ignore[arg-type]
        )
        row = evaluate_totals(
            TotalsAuthority((game,), "2026-09-03", Path("trace.json"), Path.cwd(), ()),
            {1: ParticipantGateEvidence(True, True, False, ())},
            board,
            evaluated_at="2026-09-03T22:59:59Z",
        )[0]
        self.assertEqual(row["decision"], "PASS")
        self.assertIn("RESIDUAL_COUNT_MISMATCH", row["reason_codes"])
        self.assertNotIn("AMBIGUOUS_DIRECTION", row["reason_codes"])

    def test_one_supported_paired_quote_can_play_when_other_gates_pass(self) -> None:
        quote = _quote("FANDUEL")
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T23:00:00Z", (), (quote,)),),
            Path("board.json"), None, None,  # type: ignore[arg-type]
        )
        rows = evaluate_totals(
            TotalsAuthority((_game(),), "2026-09-03", Path("trace.json"), Path.cwd(), ()),
            {1: ParticipantGateEvidence(True, True, False, ())},
            board,
            evaluated_at="2026-09-03T22:59:59Z",
        )

        self.assertEqual(rows[0]["decision"], "PLAY")
        self.assertEqual(rows[0]["book_coverage"], 1)
        self.assertEqual(rows[0]["selected_book"], "FANDUEL")
        self.assertEqual(rows[0]["direction"], "OVER")

    def test_each_direction_is_evaluated_and_best_play_selected(self) -> None:
        game = _game()
        candidate = evaluate_total_candidate(game, _quote("FANDUEL"), "OVER")
        self.assertEqual(candidate.decision, "PLAY")
        self.assertEqual(candidate.nonpush_support, Fraction(1))
        self.assertEqual(candidate.point_gap, Fraction(1))
        selected = select_total_play((candidate,))
        self.assertIs(selected, candidate)

    def test_zero_books_and_residual_count_are_pass_reasons(self) -> None:
        game = _game(samples=(1.0,))
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T23:00:00Z", (), ()),),
            Path("board.json"), None, None,  # type: ignore[arg-type]
        )
        rows = evaluate_totals(
            TotalsAuthority((game,), "2026-09-03", Path("trace.json"), Path.cwd(), ()),
            {1: ParticipantGateEvidence(True, True, False, ())},
            board,
            evaluated_at="2026-09-03T22:59:59Z",
        )
        self.assertEqual(rows[0]["decision"], "PASS")
        self.assertIn("RESIDUAL_COUNT_MISMATCH", rows[0]["reason_codes"])
        self.assertIn("NO_QUOTE", rows[0]["reason_codes"])


if __name__ == "__main__":
    unittest.main()
