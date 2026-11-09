"""Focused contracts for the outcome-free prospective observer."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from mlb_backtest.market_play_contracts import canonical_json_bytes
from mlb_backtest.prospective_ml_observation import (
    build_ml_observations,
    persist_ml_observations,
    validate_ml_snapshot,
)
from tests.prospective_ml_fixtures import sealed_ml_fixture


class ProspectiveMlSnapshotTests(unittest.TestCase):
    def test_observer_import_cannot_read_results_or_network_clients(self) -> None:
        script = """
import builtins
blocked = {'urllib.request', 'requests', 'selenium', 'playwright', 'mlb_backtest.outcomes'}
real = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in blocked or any(name.startswith(prefix + '.') for prefix in blocked):
        raise AssertionError(name)
    return real(name, *args, **kwargs)
builtins.__import__ = guarded
import mlb_backtest.prospective_ml_observation
"""
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=Path.cwd(), capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sealed_ml_only_low_completeness_claims_primary(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root, p_home=.7, completeness=.4)
            built = build_ml_observations(
                root, trace, observed_clock="2026-09-03T19:45:00Z"
            )
            self.assertEqual(len(built.snapshots), 1)
            self.assertTrue(built.snapshots[0]["primary_eligible"])
            self.assertEqual(built.snapshots[0]["market"]["status"], "NOT_EVALUATED")
            persisted = persist_ml_observations(
                root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
            )
            self.assertEqual(len(persisted.candidate_paths), 1)
            self.assertEqual(len(persisted.primary_claim_paths), 1)

    def test_mutated_sealed_trace_rejects_built_transport(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:00:00Z")
            (root / trace).write_bytes(b"{}")
            with self.assertRaises(ValueError):
                persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T19:59:59Z")

    def test_tampered_participant_receipt_rejects_actual_authority_load(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            receipt = root / "audit/experimental_daily/live_receipts/777001.participants.json"
            receipt.write_bytes(b"{}")
            with self.assertRaises(ValueError):
                build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")

    def test_malformed_existing_primary_claim_is_not_a_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            claim = root / "audit/prospective_ml_observation/primary_claims/777001" / ("a" * 64 + ".json")
            claim.parent.mkdir(parents=True)
            claim.write_bytes(canonical_json_bytes({"schema": "PROSPECTIVE_ML_PRIMARY_CLAIM_V1", "game_pk": 777001, "model_card_sha256": "a" * 64, "claimed_at_utc": "2026-09-03T19:59:00Z"}))
            with self.assertRaises(ValueError):
                persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T19:59:59Z")

    def test_snapshot_requires_its_canonical_digest_and_not_evaluated_market(self) -> None:
        row = {
            "schema": "PROSPECTIVE_ML_OBSERVATION_SNAPSHOT_V1",
            "observed_at_utc": "2026-09-03T19:00:00Z",
            "target_date": "2026-09-03",
            "game_pk": 1,
            "scheduled_start_utc": "2026-09-03T20:00:00Z",
            "away_team": "Away",
            "home_team": "Home",
            "model_card_sha256": "a" * 64,
            "ml_trace_relative": "reports/x.trace.json",
            "ml_trace_sha256": "b" * 64,
            "p_home": "0.7",
            "predicted_side": "home",
            "pick_probability": "0.7",
            "data_completeness": "0.4",
            "participant": {"away_lineup": list(range(1, 10)), "home_lineup": list(range(11, 20)), "away_observed_starter_id": 20, "home_observed_starter_id": 21, "away_expected_starter_id": 20, "home_expected_starter_id": 21, "payload_relative": "data/p.json", "payload_sha256": "c" * 64, "receipt_relative": "audit/p.json", "receipt_sha256": "d" * 64, "durable_at_utc": "2026-09-03T18:59:00Z"},
            "market": {"status": "NOT_EVALUATED", "trace_relative": None, "trace_sha256": None, "decisions": None},
            "market_status": "NOT_EVALUATED", "totals_status": "NOT_EVALUATED",
            "primary_eligible": True,
            "reason_codes": [],
        }
        row["content_sha256"] = hashlib.sha256(canonical_json_bytes(row)).hexdigest()
        self.assertEqual(validate_ml_snapshot(row)["game_pk"], 1)
        row["market"]["status"] = "PASS"
        with self.assertRaises(ValueError):
            validate_ml_snapshot(row)


if __name__ == "__main__":
    unittest.main()
