"""Contract checks for the sealed provisional daily-totals publisher."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mlb_backtest.daily_totals import (
    BuiltDailyTotals,
    MODEL_SCHEMA,
    REPORT_SCHEMA,
    render_totals_markdown,
    build_daily_totals,
    persist_daily_totals,
    validate_totals_trace,
    validate_totals_model_receipt,
    validate_totals_report,
)
from mlb_backtest.daily_totals import _per_game_coverage
from mlb_backtest.experimental_totals import (
    FinalTotalGame,
    TargetTotalGame,
    TotalProjection,
    UnprojectedTotal,
)
from mlb_backtest.experimental_totals import CANDIDATE_IDS
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
        "windows": {"warmup_start": "2026-03-25", "warmup_end": "2026-05-31", "development_start": "2026-06-01", "development_end": "2026-07-31", "final_start": "2026-08-01", "final_end": "2026-08-31", "league_days": 30, "venue_days": 60, "team_shrinkage_games": 10, "venue_shrinkage_games": 20},
        "candidate_ids": ["LEAGUE_30", "TEAM_30", "TEAM_VENUE_30"],
        "candidate_definition_sha256": "",
        "development_metrics": {candidate: _metrics() for candidate in ("LEAGUE_30", "TEAM_30", "TEAM_VENUE_30")},
        "selected_candidate": "LEAGUE_30",
        "final_metrics": _metrics(),
        "development_residuals": [-1.25, 0.0, 1.5],
        "development_residual_sha256": "",
        "development_residual_count": 3,
    }
    payload["candidate_definition_sha256"] = _sha({"candidate_ids": payload["candidate_ids"], "windows": payload["windows"]})
    payload["development_residual_sha256"] = _sha(payload["development_residuals"])
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
        "games": [{"game_pk": 1, "official_date": "2026-09-01", "scheduled_start_utc": "2026-09-01T16:00:00Z", "away_team_id": 2, "home_team_id": 3, "venue_id": 4, "status": "PROJECTED", "candidate_id": "LEAGUE_30", "away_expected_runs": 3.2, "home_expected_runs": 4.1, "expected_total": 7.3, "interval_80": [5, 7], "residual_samples": [5, 6, 7], "residual_sha256": _sha([5, 6, 7]), "residual_count": 3, "feature_cutoff": "2026-08-31", "source_digest": receipt["history_source"]["payload_sha256"], "data_quality_flags": ["ADMITTED_SCHEDULE", "MODEL_ELIGIBLE"]}] ,
    }
    canonical = dict(payload)
    canonical.pop("content_sha256")
    payload["content_sha256"] = _sha(canonical)
    return payload


def _recontent(payload: dict[str, object]) -> None:
    payload["content_sha256"] = _sha({key: value for key, value in payload.items() if key != "content_sha256"})


def _prediction_rows(candidate: str, start_pk: int, count: int, day: str) -> tuple[tuple[FinalTotalGame, TotalProjection], ...]:
    return tuple(
        (FinalTotalGame(start_pk + index, day, f"{day}T16:00:00Z", 2, 3, 4, 3, 4), TotalProjection("PROJECTED", candidate, 3.0, 4.0, 7.0))
        for index in range(count)
    )


class DailyTotalsContractTests(unittest.TestCase):
    def test_valid_receipt_and_report_are_canonical(self) -> None:
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        self.assertEqual(validate_totals_model_receipt(receipt), receipt)
        self.assertEqual(validate_totals_report(report), report)

    def test_schemas_accept_canonical_payloads_and_reject_nested_invalid_variants(self) -> None:
        schemas = {
            "model": json.loads((Path.cwd() / "schemas/experimental_daily_totals_model.schema.json").read_text()),
            "report": json.loads((Path.cwd() / "schemas/experimental_daily_totals_report.schema.json").read_text()),
        }
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        self.assertEqual(list(Draft202012Validator(schemas["model"]).iter_errors(receipt)), [])
        self.assertEqual(list(Draft202012Validator(schemas["report"]).iter_errors(report)), [])
        report["history_source"]["unexpected"] = "no"
        self.assertTrue(list(Draft202012Validator(schemas["report"]).iter_errors(report)))
        report = make_valid_report_payload()
        report["games"][0]["source_digest"] = 1  # type: ignore[index]
        self.assertTrue(list(Draft202012Validator(schemas["report"]).iter_errors(report)))
        report = make_valid_report_payload()
        report["games"][0]["status"] = "UNPROJECTED"  # type: ignore[index]
        self.assertTrue(list(Draft202012Validator(schemas["report"]).iter_errors(report)))

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

    def test_report_rejects_non_rfc3339_utc_start_and_null_required_identity_path(self) -> None:
        for start in ("not-a-timeZ", "2026-09-01T16:00:00+00:00", 1):
            report = make_valid_report_payload()
            report["games"][0]["scheduled_start_utc"] = start  # type: ignore[index]
            _recontent(report)
            with self.assertRaises(ValueError):
                validate_totals_report(report)
        report = make_valid_report_payload()
        report["history_source"]["claim_relative"] = None
        _recontent(report)
        with self.assertRaises(ValueError):
            validate_totals_report(report)

    def test_receipt_requires_exact_windows_and_minimum_development_final_rows(self) -> None:
        receipt = make_valid_model_receipt()
        receipt["windows"]["warmup_end"] = "2026-05-30"  # type: ignore[index]
        _recontent(receipt)
        with self.assertRaises(ValueError):
            validate_totals_model_receipt(receipt)
        receipt = make_valid_model_receipt()
        receipt["final_metrics"]["games"] = 99  # type: ignore[index]
        _recontent(receipt)
        with self.assertRaises(ValueError):
            validate_totals_model_receipt(receipt)

    def test_unprojected_variant_is_closed(self) -> None:
        report = make_valid_report_payload()
        report["games"] = [{"game_pk": 1, "official_date": "2026-09-01", "scheduled_start_utc": "2026-09-01T16:00:00Z", "away_team_id": 2, "home_team_id": 3, "venue_id": 4, "status": "UNPROJECTED", "reason": "UNKNOWN_TEAM", "feature_cutoff": "2026-08-31", "source_digest": report["history_source"]["payload_sha256"], "data_quality_flags": ["ADMITTED_SCHEDULE", "UNKNOWN_TEAM"]}]
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

    def test_trace_is_closed_and_cross_binds_full_identities(self) -> None:
        report = make_valid_report_payload()
        trace = {
            "schema": "PROVISIONAL_DAILY_TOTALS_TRACE_V1", "content_sha256": "",
            "model_receipt_relative": report["model_receipt_relative"], "model_receipt_sha256": report["model_receipt_sha256"],
            "report_relative": "reports/experimental_daily_totals/2026-09-01/report.json", "report_sha256": "6" * 64,
            "report_sidecar_sha256": "7" * 64, "markdown_sha256": "8" * 64,
            "source_report_relative": report["source_report_relative"], "source_report_sha256": report["source_report_sha256"],
            "history_source": report["history_source"], "live_source": report["live_source"],
        }
        trace["content_sha256"] = _sha({key: value for key, value in trace.items() if key != "content_sha256"})
        self.assertEqual(validate_totals_trace(trace), trace)
        trace["history_source"]["payload_sha256"] = "9" * 64
        with self.assertRaises(ValueError):
            validate_totals_trace(trace)

    def test_built_value_retains_canonical_immutable_bytes(self) -> None:
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        built = BuiltDailyTotals(canonical_json_bytes(receipt), canonical_json_bytes(report), canonical_json_bytes({"source_report_relative": report["source_report_relative"], "source_report_sha256": report["source_report_sha256"], "model_receipt_relative": report["model_receipt_relative"]}))
        exposed = built.report
        exposed["games"][0]["source_digest"] = "0" * 64  # type: ignore[index]
        self.assertEqual(built.report["games"][0]["source_digest"], report["history_source"]["payload_sha256"])

    def test_rows_must_be_chronological_by_date_start_and_game_pk(self) -> None:
        report = make_valid_report_payload()
        row = dict(report["games"][0])  # type: ignore[index]
        row["game_pk"] = 2
        row["scheduled_start_utc"] = "2026-09-01T15:00:00Z"
        report["games"] = [report["games"][0], row]
        report["content_sha256"] = _sha({key: value for key, value in report.items() if key != "content_sha256"})
        with self.assertRaises(ValueError):
            validate_totals_report(report)

    def test_persist_writes_trace_commit_after_re_admission(self) -> None:
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / report["source_report_relative"]
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            source_sha = hashlib.sha256(b"source").hexdigest()
            receipt["source_report_sha256"] = source_sha
            _recontent(receipt)
            report["source_report_sha256"] = source_sha
            report["model_receipt_relative"] = f"audit/experimental_daily_totals/model/{receipt['content_sha256']}.json"
            report["model_receipt_sha256"] = _sha(receipt)
            _recontent(report)
            built = BuiltDailyTotals(canonical_json_bytes(receipt), canonical_json_bytes(report), canonical_json_bytes({"source_report_relative": report["source_report_relative"], "source_report_sha256": source_sha, "model_receipt_relative": report["model_receipt_relative"]}))
            admitted = SimpleNamespace(target_date=report["target_date"], cutoff_date=report["cutoff_date"], history_identity=report["history_source"], live_identity=report["live_source"])
            with patch("mlb_backtest.daily_totals.load_totals_sources_bound", return_value=admitted), patch("mlb_backtest.daily_totals._source_identity", side_effect=lambda value: value):
                _receipt, _report, _markdown, trace_path = persist_daily_totals(root, built)
            trace = validate_totals_trace(json.loads(trace_path.read_text()))
            self.assertEqual(trace["history_source"], report["history_source"])
            self.assertEqual(trace["live_source"], report["live_source"])

    def test_mismatched_receipt_path_creates_no_output_files(self) -> None:
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        report["model_receipt_relative"] = "audit/experimental_daily_totals/model/wrong.json"
        _recontent(report)
        built = BuiltDailyTotals(canonical_json_bytes(receipt), canonical_json_bytes(report), canonical_json_bytes({"source_report_relative": report["source_report_relative"], "source_report_sha256": report["source_report_sha256"], "model_receipt_relative": report["model_receipt_relative"]}))
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                persist_daily_totals(root, built)
            self.assertFalse((root / "audit").exists())
            self.assertFalse((root / "reports").exists())

    def test_mismatched_re_admitted_identity_creates_no_output_files(self) -> None:
        receipt = make_valid_model_receipt()
        report = make_valid_report_payload()
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / report["source_report_relative"]
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            source_sha = hashlib.sha256(b"source").hexdigest()
            receipt["source_report_sha256"] = source_sha
            _recontent(receipt)
            report["source_report_sha256"] = source_sha
            report["model_receipt_relative"] = f"audit/experimental_daily_totals/model/{receipt['content_sha256']}.json"
            report["model_receipt_sha256"] = _sha(receipt)
            _recontent(report)
            built = BuiltDailyTotals(canonical_json_bytes(receipt), canonical_json_bytes(report), canonical_json_bytes({"source_report_relative": report["source_report_relative"], "source_report_sha256": source_sha, "model_receipt_relative": report["model_receipt_relative"]}))
            wrong_history = dict(report["history_source"])
            wrong_history["payload_sha256"] = "9" * 64
            admitted = SimpleNamespace(target_date=report["target_date"], cutoff_date=report["cutoff_date"], history_identity=wrong_history, live_identity=report["live_source"])
            with patch("mlb_backtest.daily_totals.load_totals_sources_bound", return_value=admitted), patch("mlb_backtest.daily_totals._source_identity", side_effect=lambda value: value):
                with self.assertRaises(ValueError):
                    persist_daily_totals(root, built)
            self.assertFalse((root / "audit").exists())
            self.assertFalse((root / "reports/experimental_daily_totals").exists())

    def test_final_coverage_uses_each_prediction_mean_not_one_fixed_interval(self) -> None:
        first = FinalTotalGame(1, "2026-08-01", "2026-08-01T16:00:00Z", 2, 3, 4, 3, 3)
        second = FinalTotalGame(2, "2026-08-02", "2026-08-02T16:00:00Z", 2, 3, 4, 5, 6)
        rows = (
            (first, TotalProjection("PROJECTED", "LEAGUE_30", 1.5, 1.5, 3.0)),
            (second, TotalProjection("PROJECTED", "LEAGUE_30", 5.0, 5.0, 10.0)),
        )
        # Residuals (-2, +2) yield [1, 5] around mean 3 and [8, 12]
        # around mean 10.  Totals 6 and 11 therefore cover 0/1 and 1/1.
        self.assertEqual(_per_game_coverage(rows, (-2.0, 2.0)), 0.5)
        # A single interval fixed around mean 3 would be [1, 5], covering neither.
        self.assertNotEqual(_per_game_coverage(rows, (-2.0, 2.0)), 0.0)

    def test_build_uses_one_bound_loader_and_exact_development_window_before_selection(self) -> None:
        game = FinalTotalGame(1, "2026-06-01", "2026-06-01T16:00:00Z", 2, 3, 4, 3, 4)
        development = {
            candidate: ((game, TotalProjection("PROJECTED", candidate, 3.0, 4.0, 7.0)),)
            for candidate in CANDIDATE_IDS
        }
        bundle = SimpleNamespace(target_date="2026-09-01", cutoff_date="2026-08-31", history_games=(), target_games=(), history_identity=_source("HISTORY_SCHEDULE"), live_identity=_source("LIVE_SCHEDULE"))
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "reports/experimental_daily/2026-09-01/source.json"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            with patch("mlb_backtest.daily_totals.load_totals_sources_bound", return_value=bundle) as loader, patch("mlb_backtest.daily_totals.walk_forward_predictions", return_value=development) as walk:
                with self.assertRaisesRegex(ValueError, "at least 100"):
                    build_daily_totals(root, "2026-09-01", Path("reports/experimental_daily/2026-09-01/source.json"))
            loader.assert_called_once()
            self.assertEqual(loader.call_args.args[0], root)
            self.assertEqual(loader.call_args.args[2], Path("reports/experimental_daily/2026-09-01/source.json"))
            self.assertEqual(walk.call_args.args[2:], ("2026-06-01", "2026-07-31"))

    def test_build_rejects_under_minimum_final_after_exact_final_window(self) -> None:
        development = {candidate: _prediction_rows(candidate, 1, 100, "2026-06-01") for candidate in CANDIDATE_IDS}
        final = {"LEAGUE_30": _prediction_rows("LEAGUE_30", 1001, 99, "2026-08-01")}
        bundle = SimpleNamespace(target_date="2026-09-01", cutoff_date="2026-08-31", history_games=(), target_games=(), history_identity=_source("HISTORY_SCHEDULE"), live_identity=_source("LIVE_SCHEDULE"))
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "reports/experimental_daily/2026-09-01/source.json"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            with patch("mlb_backtest.daily_totals.load_totals_sources_bound", return_value=bundle), patch("mlb_backtest.daily_totals.walk_forward_predictions", side_effect=(development, final)) as walk, patch("mlb_backtest.daily_totals.select_candidate", return_value="LEAGUE_30") as select:
                with self.assertRaisesRegex(ValueError, "final evaluation requires at least 100"):
                    build_daily_totals(root, "2026-09-01", Path("reports/experimental_daily/2026-09-01/source.json"))
            select.assert_called_once_with(development)
            self.assertEqual(walk.call_args_list[1].args[1:], (("LEAGUE_30",), "2026-08-01", "2026-08-31"))
            self.assertFalse((root / "audit").exists())
            self.assertFalse((root / "reports/experimental_daily_totals").exists())

    def test_build_selects_once_and_preserves_every_target_in_chronological_order(self) -> None:
        development = {candidate: _prediction_rows(candidate, 1, 100, "2026-06-01") for candidate in CANDIDATE_IDS}
        final = {"LEAGUE_30": _prediction_rows("LEAGUE_30", 1001, 100, "2026-08-01")}
        targets = (TargetTotalGame(20, "2026-09-01", "2026-09-01T18:00:00Z", 2, 3, 4), TargetTotalGame(10, "2026-09-01", "2026-09-01T16:00:00Z", 5, 6, 7))
        bundle = SimpleNamespace(target_date="2026-09-01", cutoff_date="2026-08-31", history_games=(), target_games=targets, history_identity=SimpleNamespace(payload_sha256="3" * 64), live_identity=SimpleNamespace(payload_sha256="3" * 64))
        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            source = root / "reports/experimental_daily/2026-09-01/source.json"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            projection = TotalProjection("PROJECTED", "LEAGUE_30", 3.0, 4.0, 7.0)
            unprojected = UnprojectedTotal("UNPROJECTED", "UNKNOWN_TEAM")
            with patch("mlb_backtest.daily_totals.load_totals_sources_bound", return_value=bundle), patch("mlb_backtest.daily_totals.walk_forward_predictions", side_effect=(development, final)) as walk, patch("mlb_backtest.daily_totals.select_candidate", return_value="LEAGUE_30") as select, patch("mlb_backtest.daily_totals.project_game", side_effect=(projection, unprojected)), patch("mlb_backtest.daily_totals._source_identity", side_effect=(_source("HISTORY_SCHEDULE"), _source("LIVE_SCHEDULE"))):
                built = build_daily_totals(root, "2026-09-01", Path("reports/experimental_daily/2026-09-01/source.json"))
            select.assert_called_once_with(development)
            self.assertEqual(len(walk.call_args_list), 2)
            self.assertEqual(walk.call_args_list[0].args[2:], ("2026-06-01", "2026-07-31"))
            self.assertEqual(walk.call_args_list[1].args[1:], (("LEAGUE_30",), "2026-08-01", "2026-08-31"))
            self.assertEqual([row["game_pk"] for row in built.report["games"]], [10, 20])
            unknown = built.report["games"][0]
            self.assertEqual(unknown["reason"], "UNKNOWN_TEAM")
            self.assertEqual(unknown["feature_cutoff"], "2026-08-31")
            self.assertEqual(unknown["source_digest"], "3" * 64)
            self.assertEqual(unknown["data_quality_flags"], ["ADMITTED_SCHEDULE", "UNKNOWN_TEAM"])
            self.assertEqual(set(unknown), {"game_pk", "official_date", "scheduled_start_utc", "away_team_id", "home_team_id", "venue_id", "status", "reason", "feature_cutoff", "source_digest", "data_quality_flags"})


if __name__ == "__main__":
    unittest.main()
