from __future__ import annotations

import builtins
import os
import re
import socket
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.request

from mlb_backtest import v52_verify_sources as verifier
from tests.v52_snapshot_store_fixture import fake_81, replay_inputs


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReplayTests(unittest.TestCase):
    def test_replay_uses_exact_sha_and_statcast_parent_coverage_key(self):
        expected = fake_81()
        by_source = {
            source: tuple(snapshot for snapshot in expected if snapshot.source == source)
            for source in {
                snapshot.source for snapshot in expected
            }
        }
        inputs = replay_inputs()
        query_ids = {record.query_id for record in inputs.records}
        sidecar_hashes = {
            record.query_sidecar_sha256 for record in inputs.records
        }
        self.assertEqual(len(query_ids), 81)
        self.assertEqual(len(sidecar_hashes), 81)
        self.assertTrue(all(_SHA256.fullmatch(value) for value in query_ids))
        self.assertTrue(all(_SHA256.fullmatch(value) for value in sidecar_hashes))
        self.assertTrue(query_ids.isdisjoint(sidecar_hashes))
        with patch(
            "mlb_backtest.v52_verify_sources._build_game_normalized_snapshots",
            return_value=by_source["mlb_schedule_game_feed"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_statcast_normalized_snapshots",
            return_value=by_source["baseball_savant_statcast"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_pitching_normalized_snapshots",
            return_value=by_source["mlb_stats_pitching"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_reference_normalized_snapshots",
            return_value=by_source["venue_team_timezone_table"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_history_normalized_snapshots",
            return_value=by_source["prior_schedule_history"],
        ), patch(
            "mlb_backtest.v52_verify_sources._build_career_bf_normalized_snapshots",
            return_value=by_source["mlb_stats_pitcher_bf"],
        ), patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")), patch.object(
            socket, "socket", side_effect=AssertionError("network")
        ), patch.object(
            builtins, "__import__", side_effect=AssertionError("import")
        ), patch.object(
            builtins, "open", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            os, "open", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            Path, "mkdir", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            Path, "write_bytes", side_effect=AssertionError("filesystem I/O")
        ), patch.object(
            Path, "write_text", side_effect=AssertionError("filesystem I/O")
        ):
            replayed = verifier.replay_verified_snapshots(inputs)
        self.assertEqual(len(replayed), 81)
        self.assertEqual(len({snapshot.sha256 for snapshot in replayed}), 81)
        self.assertEqual(
            len({(snapshot.season, snapshot.source) for snapshot in replayed}), 42
        )
        self.assertEqual(replayed, tuple(sorted(replayed, key=verifier.snapshot_sort_key)))
        self.assertEqual(
            len(
                {
                    (
                        snapshot.season,
                        snapshot.payload["parent_coverage"]["inclusive_start"],
                        snapshot.payload["parent_coverage"]["inclusive_end"],
                    )
                    for snapshot in replayed
                    if snapshot.source == "baseball_savant_statcast"
                }
            ),
            46,
        )


if __name__ == "__main__":
    unittest.main()
