"""Operator-facing CLI contracts for the offline market-play flow."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mlb_backtest import daily_multibook_market_plays as daily
from mlb_backtest import multibook_market_board as board


class MultibookMarketPlaysCliTests(unittest.TestCase):
    def _invoke(self, callback, argv: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = callback(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_evaluate_rejects_policy_and_market_overrides(self) -> None:
        valid = [
            "evaluate",
            "--root", ".",
            "--ml-trace", "input/ml.trace.json",
            "--totals-trace", "input/totals.trace.json",
            "--market-board", "input/board.json",
        ]
        for flag in ("--date", "--book", "--line", "--price", "--threshold", "--clock", "--url", "--game"):
            status, _, _ = self._invoke(daily.main, valid + [flag, "x"])
            self.assertEqual(status, 2, flag)

    def test_evaluate_success_prints_only_terminal_trace_relative_path(self) -> None:
        valid = [
            "evaluate",
            "--root", ".",
            "--ml-trace", "input/ml.trace.json",
            "--totals-trace", "input/totals.trace.json",
            "--market-board", "input/board.json",
        ]
        expected = Path("reports/experimental_multibook_market_plays/2026-09-02/result.trace.json")
        with patch.object(daily, "build_market_plays", return_value=object()), patch.object(
            daily, "persist_market_plays", return_value=(Path("x.sha256"), Path("x.json"), Path("x.md"), expected)
        ):
            status, stdout, stderr = self._invoke(daily.main, valid)
        self.assertEqual(status, 0)
        self.assertEqual(stdout, expected.as_posix() + "\n")
        self.assertEqual(stderr, "")

    def test_board_cli_accepts_one_outcome_for_v2(self) -> None:
        argv = [
            "admit-board", "--root", ".", "--schedule-receipt", "input/schedule.json",
            "--capture-outcome", "a",
        ]
        with patch.object(board, "normalize_market_board", return_value=object()) as normalize, patch.object(
            board, "persist_market_board", return_value=(Path("board.json"), Path("board.sha256"))
        ):
            status, stdout, stderr = self._invoke(board.main, argv)
        self.assertEqual(status, 0)
        self.assertEqual(stdout, "board.json\n")
        self.assertEqual(stderr, "")
        normalize.assert_called_once()

    def test_board_cli_rejects_more_than_five_outcomes(self) -> None:
        argv = [
            "admit-board", "--root", ".", "--schedule-receipt", "input/schedule.json",
            "--capture-outcome", "a", "b", "c", "d", "e", "f",
        ]
        status, _, _ = self._invoke(board.main, argv)
        self.assertEqual(status, 2)

    def test_board_cli_rejects_repeated_outcome_option(self) -> None:
        argv = [
            "admit-board", "--root", ".", "--schedule-receipt", "input/schedule.json",
            "--capture-outcome", "a", "b", "c", "d", "e",
            "--capture-outcome", "f", "g", "h", "i", "j",
        ]
        status, _, _ = self._invoke(board.main, argv)
        self.assertEqual(status, 2)

    def test_evaluator_import_has_no_network_or_browser_dependency(self) -> None:
        script = """
import builtins
blocked = {'urllib.request', 'requests', 'selenium', 'playwright', 'sportsbook'}
real = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in blocked or any(name.startswith(prefix + '.') for prefix in blocked):
        raise AssertionError(name)
    return real(name, *args, **kwargs)
builtins.__import__ = guarded
import mlb_backtest.daily_multibook_market_plays
import mlb_backtest.multibook_ml_evaluator
import mlb_backtest.multibook_totals_evaluator
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
