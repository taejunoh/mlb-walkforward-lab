"""Contract tests for the sealed market-play authority adapters."""

from __future__ import annotations

import unittest
from fractions import Fraction
from pathlib import Path

from mlb_backtest.market_play_authorities import (
    ParticipantGateEvidence,
    load_ml_authority,
    load_totals_authority,
    rebind_authorities,
)


ROOT = Path(__file__).resolve().parents[1]


class MarketPlayAuthorityTests(unittest.TestCase):
    def test_participant_gate_is_immutable_and_exact(self) -> None:
        evidence = ParticipantGateEvidence(True, True, False, ("PROBABLE_CONSISTENT",))
        self.assertTrue(evidence.complete)
        with self.assertRaises((AttributeError, TypeError)):
            evidence.complete = False  # type: ignore[misc]

    def test_ml_adapter_unions_scored_not_top3_and_unscored_primary_population(self) -> None:
        traces = sorted(
            ROOT.glob("reports/experimental_daily/*/*.trace.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if not traces:
            self.skipTest("sealed live report fixture is not present")
        authority = load_ml_authority(ROOT, traces[-1])
        self.assertEqual(len(authority.games), 15)
        scored = [game for game in authority.games if game.model_status == "SCORED"]
        unscored = [game for game in authority.games if game.model_status == "UNSCORED"]
        self.assertEqual(len(scored), 9)
        self.assertEqual(len(unscored), 6)
        self.assertTrue(any(game.legacy_primary_rank is None for game in scored))
        self.assertTrue(any(game.pick_probability is None for game in unscored))
        rebind_authorities(authority)

    def test_totals_adapter_requires_764_bound_residual_samples(self) -> None:
        traces = sorted(ROOT.glob("reports/experimental_daily_totals/*/*.trace.json"))
        if not traces:
            self.skipTest("sealed totals report fixture is not present")
        authority = load_totals_authority(ROOT, traces[-1])
        projected = [game for game in authority.games if game.status == "PROJECTED"]
        self.assertTrue(projected)
        self.assertTrue(all(len(game.residual_samples) == 764 for game in projected))
        self.assertIsInstance(projected[0].expected_total, Fraction)
        rebind_authorities(authority)


if __name__ == "__main__":
    unittest.main()
