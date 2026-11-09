"""TOCTOU checks for immutable prospective settlement/report publication."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import mlb_backtest.prospective_ml_settlement as prospective_ml_settlement
from mlb_backtest.prospective_ml_observation import build_ml_observations, persist_ml_observations
from mlb_backtest.prospective_ml_settlement import (
    build_calibration_report,
    build_settlement,
    persist_calibration_report,
    persist_settlement,
)
from tests.prospective_ml_fixtures import sealed_ml_fixture
from tests.test_prospective_ml_settlement import _official_receipt


class ProspectiveMlPersistenceIntegrityTests(TestCase):
    def _primary(self, root: Path):
        trace = sealed_ml_fixture(root)
        built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
        return persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T19:59:59Z")

    @staticmethod
    def _json_files(root: Path, relative: str) -> list[Path]:
        directory = root / relative
        return list(directory.rglob("*.json")) if directory.is_dir() else []

    def test_settlement_rebind_rejects_result_receipt_mutated_during_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            primary = self._primary(root)
            receipt = _official_receipt(root)
            built = build_settlement(root, primary.candidate_snapshots[0], receipt)
            original_write = prospective_ml_settlement.write_exclusive
            calls = 0

            def mutate_then_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    (root / receipt).write_bytes(b"{}")
                return original_write(*args, **kwargs)

            with patch.object(prospective_ml_settlement, "write_exclusive", side_effect=mutate_then_write):
                with self.assertRaises(ValueError):
                    persist_settlement(root, built)
            self.assertGreaterEqual(calls, 2)
            # The record may be an orphan after a post-write rebind failure;
            # retained evidence is diagnostic, while the fixed head is the
            # publication boundary and must not exist.
            self.assertEqual(
                len(self._json_files(root, "audit/prospective_ml_observation/settlements")),
                1,
            )
            self.assertEqual(self._json_files(root, "audit/prospective_ml_observation/settlement_heads"), [])
            with self.assertRaises(ValueError):
                build_settlement(root, primary.candidate_snapshots[0], receipt)

    def test_report_rebind_rejects_primary_claim_mutated_during_write(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            primary = self._primary(root)
            receipt = _official_receipt(root)
            settled = persist_settlement(root, build_settlement(root, primary.candidate_snapshots[0], receipt))
            built = build_calibration_report(root, primary.primary_claim_paths, [settled.head_path])
            claim = root / primary.primary_claim_paths[0]
            original_write = prospective_ml_settlement.write_exclusive
            calls = 0

            def mutate_then_write(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    claim.write_bytes(json.dumps({}).encode())
                return original_write(*args, **kwargs)

            with patch.object(prospective_ml_settlement, "write_exclusive", side_effect=mutate_then_write):
                with self.assertRaises(ValueError):
                    persist_calibration_report(root, built)
            self.assertGreaterEqual(calls, 2)
            # The failed report bytes remain diagnostic evidence; the mutated
            # claim cannot be re-admitted, so no successful publication exists.
            self.assertEqual(
                len(self._json_files(root, "reports/prospective_ml_observation")),
                1,
            )
            with self.assertRaises(ValueError):
                build_calibration_report(root, primary.primary_claim_paths, [settled.head_path])


if __name__ == "__main__":
    import unittest

    unittest.main()
