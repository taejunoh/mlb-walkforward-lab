"""Focused contracts for immutable ML settlement and calibration."""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.market_play_contracts import canonical_json_bytes
import mlb_backtest.prospective_ml_observation as prospective_ml_observation
import mlb_backtest.prospective_ml_settlement as prospective_ml_settlement
from mlb_backtest.prospective_ml_observation import (
    build_ml_observations,
    persist_ml_observations,
)
from mlb_backtest.prospective_ml_settlement import calibration_metrics
from mlb_backtest.prospective_ml_settlement import (
    build_calibration_report,
    build_settlement,
    persist_calibration_report,
    persist_settlement,
)
from tests.prospective_ml_fixtures import sealed_ml_fixture


def _official_receipt(
    root: Path,
    *,
    home_won: bool = True,
    abstract_state: str = "Final",
    detailed_state: str = "Final",
    game_pk: int = 777001,
    away_name: str = "Away Club",
    home_name: str = "Home Club",
    winner_flags: tuple[bool, bool] | None = None,
    source: str = "stats_api",
) -> Path:
    payload_relative = Path("data/synthetic/final.raw")
    payload = {
        "dates": [{"games": [{"gamePk": game_pk, "officialDate": "2026-09-03", "status": {"abstractGameState": abstract_state, "detailedState": detailed_state}, "teams": {"away": {"team": {"name": away_name}, "isWinner": (winner_flags or (not home_won, home_won))[0]}, "home": {"team": {"name": home_name}, "isWinner": (winner_flags or (not home_won, home_won))[1]}}}]}]
    }
    raw = canonical_json_bytes(payload)
    (root / payload_relative).parent.mkdir(parents=True, exist_ok=True)
    (root / payload_relative).write_bytes(raw)
    manifest_relative = Path("audit/synthetic/final.manifest.json")
    requested_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&gamePk={game_pk}"
    query_identity_json = canonical_json_bytes({"game_pk": game_pk}).decode()
    manifest_raw = canonical_json_bytes({"payload_relative": payload_relative.as_posix(), "payload_sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw), "source": source, "query_identity_json": query_identity_json, "requested_url": requested_url, "final_url": requested_url})
    (root / manifest_relative).parent.mkdir(parents=True, exist_ok=True)
    (root / manifest_relative).write_bytes(manifest_raw)
    receipt_relative = Path("audit/synthetic/final.json")
    receipt = {"source": source, "requested_url": requested_url, "final_url": requested_url, "http_status": 200, "payload_relative": payload_relative.as_posix(), "payload_sha256": hashlib.sha256(raw).hexdigest(), "byte_length": len(raw), "response_completed_at_utc": "2026-09-03T23:00:00Z", "query_identity_json": query_identity_json, "manifest_relative": manifest_relative.as_posix(), "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest()}
    receipt_raw = canonical_json_bytes(receipt)
    (root / receipt_relative).parent.mkdir(parents=True, exist_ok=True)
    (root / receipt_relative).write_bytes(receipt_raw)
    completion = {"receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(), "durable_at_utc": "2026-09-03T23:00:01Z"}
    (root / receipt_relative.with_suffix(".completion.json")).write_bytes(canonical_json_bytes(completion))
    return receipt_relative


def _rewrite_receipt(root: Path, receipt: Path, value: dict[str, object]) -> None:
    """Rewrite a synthetic receipt while keeping its completion binding valid."""
    raw = canonical_json_bytes(value)
    (root / receipt).write_bytes(raw)
    completion = {"receipt_sha256": hashlib.sha256(raw).hexdigest(), "durable_at_utc": "2026-09-03T23:00:01Z"}
    (root / receipt.with_suffix(".completion.json")).write_bytes(canonical_json_bytes(completion))


class ProspectiveMlSettlementTests(unittest.TestCase):
    def test_hand_fixture_metrics_match_documented_probability_semantics(self) -> None:
        metrics = calibration_metrics(
            ((0.7, True), (0.4, True))
        )
        self.assertEqual(metrics["n"], 2)
        self.assertEqual(metrics["accuracy"], "0.500000")
        self.assertEqual(metrics["brier"], "0.225000")
        self.assertAlmostEqual(float(metrics["log_loss"]), (-__import__("math").log(.7) - __import__("math").log(.4)) / 2)

    def test_admitted_final_result_persists_settlement_and_nonempty_report(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            persisted = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
            settlement = persist_settlement(root, build_settlement(root, persisted.candidate_paths[0], _official_receipt(root)))
            report = build_calibration_report(root, persisted.primary_claim_paths, [settlement.head_path])
            self.assertEqual(report.report["metrics"]["all_primary"]["n"], 1)
            self.assertEqual(report.report["metrics"]["all_primary"]["accuracy"], "1.000000")
            self.assertTrue((root / persist_calibration_report(root, report)).is_file())

    def test_correction_requires_prior_head_and_creates_next_revision(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            observed = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            primary = persist_ml_observations(root, observed, primary_clock=lambda: "2026-09-03T19:59:59Z")
            first = persist_settlement(root, build_settlement(root, primary.candidate_paths[0], _official_receipt(root, home_won=True)))
            # A correction must name the fixed previous head, never a record path.
            with self.assertRaises(ValueError):
                build_settlement(root, primary.candidate_paths[0], _official_receipt(root, home_won=False), prior=first.record_path)
            second = persist_settlement(root, build_settlement(root, primary.candidate_paths[0], _official_receipt(root, home_won=False), prior=first.head_path))
            self.assertEqual(second.record["revision"], 2)

    def test_pending_primary_has_null_metrics_not_a_loss(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
            report = build_calibration_report(root, primary.primary_claim_paths, [])
            self.assertEqual(report.report["pending_count"], 1)
            self.assertIsNone(report.report["metrics"]["all_primary"]["brier"])

    def test_nonfinal_states_are_explicitly_excluded_not_counted_as_losses(self) -> None:
        states = (
            ("Preview", "Scheduled", "PENDING"),
            ("Preview", "Postponed", "POSTPONED"),
            ("Preview", "Suspended", "SUSPENDED"),
            ("Preview", "Tie", "TIE"),
            ("Preview", "No Contest", "NO_CONTEST"),
        )
        for abstract_state, detailed_state, expected_status in states:
            with self.subTest(status=expected_status), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace = sealed_ml_fixture(root)
                observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
                primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
                receipt = _official_receipt(root, abstract_state=abstract_state, detailed_state=detailed_state)
                settlement = persist_settlement(root, build_settlement(root, primary.candidate_paths[0], receipt))
                self.assertEqual(settlement.record["result"]["status"], expected_status)
                self.assertIsNone(settlement.record["result"]["winner"])
                report = build_calibration_report(root, primary.primary_claim_paths, [settlement.head_path])
                metrics = report.report["metrics"]["all_primary"]
                self.assertEqual(report.report["excluded_count"], 1)
                self.assertEqual(metrics["n"], 0)
                self.assertEqual(metrics["wins"], 0)
                self.assertEqual(metrics["losses"], 0)
                self.assertIsNone(metrics["accuracy"])
                self.assertIsNone(metrics["brier"])
                model_sha = "a" * 64
                for model_key in (
                    f"model:{model_sha}",
                    f"model:{model_sha}:completeness_lt_0_50",
                    f"model:{model_sha}:completeness_ge_0_50",
                ):
                    with self.subTest(status=expected_status, metric=model_key):
                        self.assertEqual(report.report["metrics"][model_key]["n"], 0)
                        self.assertIsNone(report.report["metrics"][model_key]["accuracy"])

    def test_malformed_result_identity_winner_and_provenance_are_rejected(self) -> None:
        cases = (
            {"winner_flags": (True, True)},
            {"away_name": "Wrong Away"},
            {"game_pk": 777002},
        )
        for options in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace = sealed_ml_fixture(root)
                observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
                primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
                receipt = _official_receipt(root, **options)
                with self.assertRaises(ValueError):
                    build_settlement(root, primary.candidate_paths[0], receipt)

    def test_receipt_requires_exact_requested_url_and_canonical_query(self) -> None:
        cases = ("missing_requested_url", "mismatched_requested_url", "noncanonical_query")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace = sealed_ml_fixture(root)
                observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
                primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
                receipt = _official_receipt(root)
                receipt_value = json.loads((root / receipt).read_text())
                if case == "missing_requested_url":
                    receipt_value.pop("requested_url")
                elif case == "mismatched_requested_url":
                    receipt_value["requested_url"] = receipt_value["final_url"].replace("777001", "777002")
                else:
                    noncanonical_query = '{"game_pk": 777001}'
                    receipt_value["query_identity_json"] = noncanonical_query
                    manifest = root / receipt_value["manifest_relative"]
                    manifest_value = json.loads(manifest.read_text())
                    manifest_value["query_identity_json"] = noncanonical_query
                    manifest_raw = canonical_json_bytes(manifest_value)
                    manifest.write_bytes(manifest_raw)
                    receipt_value["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
                _rewrite_receipt(root, receipt, receipt_value)
                with self.assertRaises(ValueError):
                    build_settlement(root, primary.candidate_paths[0], receipt)

    def test_manifest_provenance_must_bind_receipt_identity(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
            receipt = _official_receipt(root)
            receipt_value = json.loads((root / receipt).read_text())
            manifest = root / receipt_value["manifest_relative"]
            manifest_value = json.loads(manifest.read_text())
            manifest_value["source"] = "unrelated_source"
            manifest_raw = canonical_json_bytes(manifest_value)
            manifest.write_bytes(manifest_raw)
            receipt_value["manifest_sha256"] = hashlib.sha256(manifest_raw).hexdigest()
            _rewrite_receipt(root, receipt, receipt_value)
            with self.assertRaises(ValueError):
                build_settlement(root, primary.candidate_paths[0], receipt)

        for tamper in ("source", "manifest"):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                root = Path(temporary)
                trace = sealed_ml_fixture(root)
                observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
                primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
                receipt = _official_receipt(root)
                if tamper == "source":
                    receipt_value = json.loads((root / receipt).read_text())
                    receipt_value["source"] = "untrusted_source"
                    (root / receipt).write_bytes(canonical_json_bytes(receipt_value))
                else:
                    manifest = root / "audit/synthetic/final.manifest.json"
                    manifest.write_bytes(canonical_json_bytes({"payload_relative": "data/synthetic/final.raw", "payload_sha256": "0" * 64, "byte_length": 1}))
                with self.assertRaises(ValueError):
                    build_settlement(root, primary.candidate_paths[0], receipt)

    def test_conflicting_revision_heads_are_rejected_and_exact_retry_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            observation = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            primary = persist_ml_observations(root, observation, primary_clock=lambda: "2026-09-03T19:59:59Z")
            first = persist_settlement(root, build_settlement(root, primary.candidate_paths[0], _official_receipt(root, home_won=True)))
            corrected = build_settlement(root, primary.candidate_paths[0], _official_receipt(root, home_won=False), prior=first.head_path)
            second = persist_settlement(root, corrected)
            retry = persist_settlement(root, corrected)
            self.assertEqual(retry.record_path, second.record_path)
            self.assertEqual(retry.head_path, second.head_path)
            with self.assertRaises(ValueError):
                build_calibration_report(root, primary.primary_claim_paths, [first.head_path, second.head_path])

    def test_cli_snapshot_settle_report_smoke_uses_sealed_synthetic_paths(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            snapshot_stdout = io.StringIO()
            with patch.object(
                prospective_ml_observation, "_now", side_effect=["2026-09-03T19:45:00Z", "2026-09-03T19:59:59Z"]
            ), redirect_stdout(snapshot_stdout):
                self.assertEqual(
                    prospective_ml_observation.main(
                        ["snapshot", "--root", str(root), "--ml-trace", trace.as_posix()]
                    ),
                    0,
                )
            candidate = Path(snapshot_stdout.getvalue().strip())
            self.assertTrue((root / candidate).is_file())
            receipt = _official_receipt(root)
            settlement_stdout = io.StringIO()
            with redirect_stdout(settlement_stdout):
                self.assertEqual(
                    prospective_ml_settlement.main(
                        ["settle", "--root", str(root), "--snapshot", candidate.as_posix(), "--result-receipt", receipt.as_posix()]
                    ),
                    0,
                )
            record = Path(settlement_stdout.getvalue().strip())
            record_value = json.loads((root / record).read_text())
            snapshot_sha = record_value["snapshot_sha256"]
            head = Path(
                f"audit/prospective_ml_observation/settlement_heads/{snapshot_sha}/00000001.json"
            )
            report_stdout = io.StringIO()
            claim = Path(
                "audit/prospective_ml_observation/primary_claims/777001/"
                + "a" * 64
                + ".json"
            )
            with redirect_stdout(report_stdout):
                self.assertEqual(
                    prospective_ml_settlement.main(
                        ["report", "--root", str(root), "--primary-claim", claim.as_posix(), "--settlement-head", head.as_posix()]
                    ),
                    0,
                )
            self.assertTrue((root / Path(report_stdout.getvalue().strip())).is_file())


if __name__ == "__main__":
    unittest.main()
