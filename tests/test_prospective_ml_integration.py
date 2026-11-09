"""Behavioral checks for the hermetic prospective ML observer boundary."""

from __future__ import annotations

import socket
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import mlb_backtest.market_play_authorities as market_play_authorities
import mlb_backtest.prospective_ml_observation as prospective_ml_observation
import mlb_backtest.secure_fs as secure_fs
from mlb_backtest.prospective_ml_observation import (
    build_ml_observations,
    persist_ml_observations,
)

from tests.prospective_ml_fixtures import GAME_PK, sealed_ml_fixture


class ProspectiveMlIntegrationTests(TestCase):
    def _root(self):
        return tempfile.TemporaryDirectory(dir=Path.cwd())

    @staticmethod
    def _eligible(root: Path, observed: str = "2026-09-03T19:45:00Z"):
        trace = sealed_ml_fixture(root)
        return build_ml_observations(
            root,
            trace,
            observed_clock=lambda: observed,
        )

    def test_repeating_same_snapshot_is_idempotent(self):
        with self._root() as temporary:
            root = Path(temporary)
            built = self._eligible(root)

            first = persist_ml_observations(
                root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
            )
            second = persist_ml_observations(
                root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
            )

            self.assertEqual(first.primary_game_pks, (GAME_PK,))
            self.assertEqual(second.primary_game_pks, (GAME_PK,))
            self.assertEqual(first.primary_snapshots, second.primary_snapshots)
            self.assertEqual(first.candidate_snapshots, second.candidate_snapshots)

    def test_concurrent_eligible_snapshots_claim_at_most_one_primary(self):
        with self._root() as temporary:
            root = Path(temporary)
            first = self._eligible(root, "2026-09-03T19:40:00Z")
            second = self._eligible(root, "2026-09-03T19:45:00Z")

            def persist(built):
                return persist_ml_observations(
                    root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                persisted = list(executor.map(persist, (first, second)))

            self.assertEqual(
                sum(len(item.primary_game_pks) for item in persisted), 1
            )
            self.assertEqual(
                len({path for item in persisted for path in item.primary_snapshots}), 1
            )
            self.assertEqual(
                sum(len(item.diagnostic_snapshots) for item in persisted), 1
            )

    def test_primary_claim_is_refused_when_persist_clock_is_after_start(self):
        with self._root() as temporary:
            root = Path(temporary)
            built = self._eligible(root)
            persisted = persist_ml_observations(
                root, built, primary_clock=lambda: "2026-09-03T20:00:01Z"
            )

            self.assertEqual(persisted.primary_game_pks, ())
            self.assertEqual(persisted.primary_snapshots, ())
            self.assertEqual(len(persisted.candidate_snapshots), 1)

    def test_invalid_lineup_and_starter_conflict_cannot_claim_primary(self):
        cases = (
            ({"away": {"players": {}}}, "INVALID_LINEUPS"),
            ({"away": {"pitchers": [999]}}, "STARTER_CONFLICT"),
        )
        for observed, reason in cases:
            with self.subTest(reason=reason), self._root() as temporary:
                root = Path(temporary)
                trace = sealed_ml_fixture(root, observed=observed)
                built = build_ml_observations(
                    root,
                    trace,
                    observed_clock=lambda: "2026-09-03T19:45:00Z",
                )
                self.assertFalse(built.candidates[0]["primary_eligible"])
                self.assertIn(reason, built.candidates[0]["reason_codes"])
                persisted = persist_ml_observations(
                    root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
                )
                self.assertEqual(persisted.primary_game_pks, ())

    def test_stale_pregame_evidence_cannot_claim_primary(self):
        with self._root() as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            built = build_ml_observations(
                root,
                trace,
                observed_clock=lambda: "2026-09-03T19:00:00Z",
            )

            candidate = built.candidates[0]
            self.assertFalse(candidate["primary_eligible"])
            self.assertIn(
                "PREGAME_EVIDENCE_AFTER_OBSERVATION", candidate["reason_codes"]
            )
            persisted = persist_ml_observations(
                root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
            )
            self.assertEqual(persisted.primary_game_pks, ())

    def test_build_and_persist_do_not_read_outcomes_or_open_network(self):
        with self._root() as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            original_read = secure_fs.read_regular_snapshot

            def deny_outcome_reads(trusted, relative, label, **kwargs):
                path = str(relative).lower()
                if any(token in path for token in ("outcome", "official-result", "results")):
                    raise AssertionError(f"outcome read attempted: {relative}")
                return original_read(trusted, relative, label, **kwargs)

            with (
                patch.object(secure_fs, "read_regular_snapshot", side_effect=deny_outcome_reads),
                patch.object(
                    market_play_authorities,
                    "read_regular_snapshot",
                    side_effect=deny_outcome_reads,
                ),
                patch.object(
                    prospective_ml_observation,
                    "read_regular_snapshot",
                    side_effect=deny_outcome_reads,
                ),
                patch.object(socket, "socket", side_effect=AssertionError("network")),
                patch.object(
                    socket, "create_connection", side_effect=AssertionError("network")
                ),
                patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")),
            ):
                built = build_ml_observations(
                    root,
                    trace,
                    observed_clock=lambda: "2026-09-03T19:45:00Z",
                )
                persisted = persist_ml_observations(
                    root, built, primary_clock=lambda: "2026-09-03T19:59:59Z"
                )

            self.assertEqual(persisted.primary_game_pks, (GAME_PK,))


if __name__ == "__main__":
    import unittest

    unittest.main()
