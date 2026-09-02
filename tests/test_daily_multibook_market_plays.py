"""Closed report/trace and Markdown tests."""

from __future__ import annotations

import hashlib
import unittest

from mlb_backtest.daily_multibook_market_plays import (
    REPORT_SCHEMA,
    STATUS_LABELS,
    TRACE_SCHEMA,
    render_market_plays_markdown,
    validate_market_plays_report,
    validate_market_plays_trace,
)
from mlb_backtest.market_play_contracts import POLICY, canonical_json_bytes, content_digest


def _report() -> dict[str, object]:
    row = {
        "game_pk": 1,
        "decision": "PLAY",
        "reason_codes": [],
        "predicted_side": "home",
        "pick_probability": "0.60",
    }
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "content_sha256": "",
        "status_labels": STATUS_LABELS,
        "target_date": "2026-09-03",
        "evaluated_at_utc": "2026-09-03T22:59:59Z",
        "policy": POLICY,
        "policy_sha256": hashlib.sha256(canonical_json_bytes(POLICY)).hexdigest(),
        "ml_trace_relative": "input/ml.trace.json",
        "ml_trace_sha256": "a" * 64,
        "totals_trace_relative": "input/totals.trace.json",
        "totals_trace_sha256": "b" * 64,
        "board_relative": "input/board.json",
        "board_sha256": "c" * 64,
        "moneylines": [row],
        "totals": [{"game_pk": 1, "decision": "PASS", "reason_codes": ["NO_QUOTE"]}],
        "warnings": [],
    }
    report["content_sha256"] = content_digest({k: v for k, v in report.items() if k != "content_sha256"})
    return report


class DailyMultibookMarketPlaysTests(unittest.TestCase):
    def test_report_has_both_sections_and_markdown_is_not_a_parlay(self) -> None:
        report = validate_market_plays_report(_report())
        self.assertEqual(len(report["moneylines"]), 1)
        self.assertEqual(len(report["totals"]), 1)
        text = render_market_plays_markdown(report).decode("utf-8")
        for label in ("RESEARCH ONLY", "PROVISIONAL", "NOT BET-VALIDATED", "ML PLAY", "TOTAL PLAY"):
            self.assertIn(label, text)
        self.assertNotIn("parlay", text.lower())
        self.assertNotIn("Top 3", text)

    def test_trace_validator_is_closed_and_digest_bound(self) -> None:
        trace = {
            "schema": TRACE_SCHEMA,
            "content_sha256": "",
            "report_relative": "reports/one.json",
            "report_sha256": "a" * 64,
            "report_sidecar_sha256": "b" * 64,
            "markdown_sha256": "c" * 64,
            "evaluated_at_utc": "2026-09-03T22:59:59Z",
            "policy_sha256": hashlib.sha256(canonical_json_bytes(POLICY)).hexdigest(),
        }
        trace["content_sha256"] = content_digest({k: v for k, v in trace.items() if k != "content_sha256"})
        self.assertEqual(validate_market_plays_trace(trace)["schema"], TRACE_SCHEMA)
        trace["report_sha256"] = "d" * 64
        with self.assertRaises(ValueError):
            validate_market_plays_trace(trace)


if __name__ == "__main__":
    unittest.main()
