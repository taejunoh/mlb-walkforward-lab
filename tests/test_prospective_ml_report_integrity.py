from __future__ import annotations

import tempfile
import unittest
import hashlib
from pathlib import Path

from mlb_backtest.market_play_contracts import canonical_json_bytes
from mlb_backtest.prospective_ml_observation import build_ml_observations, persist_ml_observations
from mlb_backtest.prospective_ml_settlement import build_calibration_report
from tests.prospective_ml_fixtures import sealed_ml_fixture


class ReportIntegrityTests(unittest.TestCase):
    def test_fake_primary_claim_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            built = build_ml_observations(root, trace, observed_clock=lambda: "2026-09-03T19:45:00Z")
            persisted = persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T19:59:59Z")
            fake = root / "fake.json"
            fake.write_bytes(canonical_json_bytes({"candidate_relative": persisted.primary_snapshots[0].as_posix()}))
            with self.assertRaises(ValueError):
                build_calibration_report(root, [Path("fake.json")], [])

    def test_pending_model_cohorts_are_visible_with_null_metrics(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            built = build_ml_observations(root, trace, observed_clock=lambda: "2026-09-03T19:45:00Z")
            persisted = persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T19:59:59Z")
            report = build_calibration_report(root, persisted.primary_claim_paths, [])
            self.assertEqual(
                report.report["primary_claim_refs"][0]["sha256"],
                hashlib.sha256((root / persisted.primary_claim_paths[0]).read_bytes()).hexdigest(),
            )
            model = "model:" + "a" * 64
            self.assertIn(model, report.report["metrics"])
            self.assertIsNone(report.report["metrics"][model]["brier"])
