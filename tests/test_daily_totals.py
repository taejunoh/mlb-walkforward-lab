"""Contract checks for the sealed provisional daily-totals publisher."""

from __future__ import annotations

import hashlib
import math
import unittest

from mlb_backtest.daily_totals import (
    MODEL_SCHEMA,
    REPORT_SCHEMA,
    render_totals_markdown,
    validate_totals_model_receipt,
    validate_totals_report,
)
from mlb_backtest.live_top3_contracts import canonical_json_bytes


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _source(role: str) -> dict[str, object]:
    value: dict[str, object] = {
        "binding_role": role,
        "binding_sha256": "a" * 64,
        "query_identity_sha256": "b" * 64,
        "claim_relative": "audit/claim.json",
        "claim_sha256": "c" * 64,
        "receipt_relative": "audit/receipt.json",
        "receipt_sha256": "d" * 64,
        "receipt_sidecar_relative": "audit/receipt.sha256",
        "receipt_sidecar_sha256": "e" * 64,
        "completion_relative": "audit/receipt.completion.json",
        "completion_sha256": "f" * 64,
        "completion_sidecar_relative": "audit/receipt.completion.sha256",
        "completion_sidecar_sha256": "0" * 64,
        "manifest_relative": "data/manifest.json",
        "manifest_sha256": "1" * 64,
        "manifest_sidecar_relative": "data/manifest.sha256",
        "manifest_sidecar_sha256": "2" * 64,
        "payload_relative": "data/payload.raw",
        "payload_sha256": "3" * 64,
        "byte_length": 1,
    }
    if role == "LIVE_SCHEDULE":
        value["receipt_sidecar_relative"] = None
        value["receipt_sidecar_sha256"] = None
    return value


def _metrics() -> dict[str, object]:
    return {"games": 100, "mae": 1.1, "rmse": 1.3, "poisson_deviance": 0.2, "interval_80_coverage": 0.8}


def make_valid_model_receipt() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": MODEL_SCHEMA,
        "content_sha256": "",
        "status_labels": ["RESEARCH ONLY", "ONE-SEASON PROVISIONAL", "NOT BET-VALIDATED"],
        "source_report_relative": "reports/experimental_daily/2026-09-01/source.json",
        "source_report_sha256": "4" * 64,
        "target_date": "2026-09-01",
        "cutoff_date": "2026-08-31",
        "history_source": _source("HISTORY_SCHEDULE"),
        "live_source": _source("LIVE_SCHEDULE"),
        "windows": {"development_start": "2026-06-01", "development_end": "2026-07-31", "final_start": "2026-08-01", "final_end": "2026-08-31", "league_days": 30, "venue_days": 60, "team_shrinkage_games": 10, "venue_shrinkage_games": 20},
        "candidate_ids": ["LEAGUE_30", "TEAM_30", "TEAM_VENUE_30"],
        "candidate_definition_sha256": "",
        "development_metrics": {candidate: _metrics() for candidate in ("LEAGUE_30", "TEAM_30", "TEAM_VENUE_30")},
        "selected_candidate": "LEAGUE_30",
        "final_metrics": _metrics(),
        "development_residual_samples": [5, 6, 7],
        "development_residual_sha256": "",
        "development_residual_count": 3,
    }
    payload["candidate_definition_sha256"] = _sha({"candidate_ids": payload["candidate_ids"], "windows": payload["windows"]})
    payload["development_residual_sha256"] = _sha(payload["development_residual_samples"])
    canonical = dict(payload)
    canonical.pop("content_sha256")
    payload["content_sha256"] = _sha(canonical)
    return payload


def make_valid_report_payload() -> dict[str, object]:
    receipt = make_valid_model_receipt()
    receipt_bytes = canonical_json_bytes(receipt)
    receipt_relative = f"audit/experimental_daily_totals/model/{receipt['content_sha256']}.json"
    payload: dict[str, object] = {
        "schema": REPORT_SCHEMA,
        "content_sha256": "",
        "status_labels": receipt["status_labels"],
        "target_date": "2026-09-01",
        "cutoff_date": "2026-08-31",
        "model_receipt_relative": receipt_relative,
        "model_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "source_report_relative": receipt["source_report_relative"],
        "source_report_sha256": receipt["source_report_sha256"],
        "history_source": receipt["history_source"],
        "live_source": receipt["live_source"],
        "selected_candidate": "LEAGUE_30",
        "development_metrics": receipt["development_metrics"],
        "final_metrics": receipt["final_metrics"],
        "games": [{"game_pk": 1, "official_date": "2026-09-01", "scheduled_start_utc": "2026-09-01T16:00:00Z", "away_team_id": 2, "home_team_id": 3, "venue_id": 4, "status": "PROJECTED", "candidate_id": "LEAGUE_30", "away_expected_runs": 3.2, "home_expected_runs": 4.1, "expected_total": 7.3, "interval_80": [5, 7], "residual_samples": [5, 6, 7], "residual_sha256": _sha([5, 6, 7]), "residual_count": 3}] ,
    }
    canonical = dict(payload)
    canonical.pop("content_sha256")
    payload["content_sha256"] = _sha(canonical)
    return payload


class DailyTotalsContractTests(unittest.TestCase):
    def test_valid_receipt_and_report_are_canonical(self) -> None:
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        self.assertEqual(validate_totals_model_receipt(receipt), receipt)
        self.assertEqual(validate_totals_report(report), report)

    def test_report_rejects_nonfinite_bool_and_extra_values(self) -> None:
        for value in (True, math.nan, math.inf):
            report = make_valid_report_payload()
            report["final_metrics"] = dict(report["final_metrics"], mae=value)
            with self.assertRaises(ValueError):
                validate_totals_report(report)
        report = make_valid_report_payload()
        report["games"][0]["odds"] = 1  # type: ignore[index]
        with self.assertRaises(ValueError):
            validate_totals_report(report)

    def test_unprojected_variant_is_closed(self) -> None:
        report = make_valid_report_payload()
        report["games"] = [{"game_pk": 1, "official_date": "2026-09-01", "scheduled_start_utc": "2026-09-01T16:00:00Z", "away_team_id": 2, "home_team_id": 3, "venue_id": 4, "status": "UNPROJECTED", "reason": "UNKNOWN_TEAM"}]
        report["content_sha256"] = _sha({key: value for key, value in report.items() if key != "content_sha256"})
        self.assertEqual(validate_totals_report(report)["games"][0]["status"], "UNPROJECTED")

    def test_render_has_no_market_fields(self) -> None:
        rendered = render_totals_markdown(make_valid_report_payload())
        self.assertIn("RESEARCH ONLY", rendered)
        self.assertNotIn("Over", rendered)
        self.assertNotIn("Under", rendered)

    def test_report_contains_no_betting_claim_or_market_field(self) -> None:
        payload = make_valid_report_payload()
        forbidden = {"pick", "odds", "edge", "playable_to", "profitability"}
        self.assertTrue(forbidden.isdisjoint(payload))
        self.assertEqual(validate_totals_report(payload)["status_labels"], ["RESEARCH ONLY", "ONE-SEASON PROVISIONAL", "NOT BET-VALIDATED"])


if __name__ == "__main__":
    unittest.main()
