import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mlb_backtest.manifest import RawStore
from mlb_backtest.mlb_stats import league_era_url
from mlb_backtest.model_prep import (
    load_league_era_from_raw,
    load_pitcher_bf_range,
    load_statcast_range,
    merge_pitcher_bf_counts,
    safe_target_games,
)
from mlb_backtest.statcast import statcast_url


ROOT = Path(__file__).parents[1]
STATCAST = ROOT / "tests" / "fixtures" / "statcast_mini.csv"
ERA = ROOT / "tests" / "fixtures" / "mlb_pitching_stats.json"


class ModelPreparationSourceTest(unittest.TestCase):
    def test_verified_catalog_prevents_reauditing_bulk_statcast_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                STATCAST.read_bytes(),
            )
            catalog = RawStore(root).verify_manifests()
            with mock.patch.object(
                RawStore,
                "verify_manifests",
                side_effect=AssertionError("bulk audit repeated"),
            ):
                loaded, _hashes = load_statcast_range(
                    root, "2024-04-01", "2024-04-02", catalog=catalog
                )
                counts, _hashes = load_pitcher_bf_range(
                    root, "2024-04-01", "2024-04-02", catalog=catalog
                )
            self.assertTrue(loaded.pitches)
            self.assertTrue(counts)

    def test_career_bf_merges_every_available_prior_training_season(self):
        self.assertEqual(
            merge_pitcher_bf_counts([{10: 40}, {10: 60, 20: 30}, {10: 70}]),
            {10: 170, 20: 30},
        )

    def test_pitcher_career_bf_is_streamed_without_retaining_prior_season_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                STATCAST.read_bytes(),
            )
            counts, hashes = load_pitcher_bf_range(root, "2024-04-01", "2024-04-02")
            self.assertEqual(counts[11], 1)
            self.assertEqual(counts[21], 1)
            self.assertEqual(len(hashes), 1)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": "https://example.invalid/noncanonical"},
                STATCAST.read_bytes(),
            )
            with self.assertRaisesRegex(ValueError, "canonical"):
                load_pitcher_bf_range(root, "2024-04-01", "2024-04-02")

    def test_safe_targets_strip_every_outcome_field_and_neutralize_final_status(self):
        games = [{
            "game_pk": 1,
            "official_date": "2025-05-01",
            "status": "Final",
            "away_score": 1,
            "home_score": 2,
            "away_is_winner": False,
            "home_is_winner": True,
            "away_team": "Away",
            "home_team": "Home",
        }, {
            "game_pk": 2,
            "official_date": "2025-05-02",
            "status": "Postponed",
            "away_team": "Away",
            "home_team": "Home",
        }]
        projected = safe_target_games(games)
        self.assertEqual(projected[0]["status"], "Scheduled")
        self.assertEqual(projected[1]["status"], "Postponed")
        for row in projected:
            self.assertTrue(
                {"away_score", "home_score", "away_is_winner", "home_is_winner"}.isdisjoint(row)
            )

    def test_statcast_loader_requires_verified_contiguous_nonoverlapping_ranges(self):
        payload = STATCAST.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            first = payload.replace(b"2024-04-02", b"2024-04-01")
            second = payload.replace(b"2024-04-01", b"2024-04-02")
            store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-01", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-01")},
                first,
            )
            store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-02", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-02", "2024-04-02")},
                second,
            )
            loaded, hashes = load_statcast_range(root, "2024-04-01", "2024-04-02")
            self.assertGreater(len(loaded.pitches), 0)
            self.assertEqual(len(hashes), 2)

            overlap = store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                payload,
            )
            self.assertTrue(overlap.manifest_path.exists())
            with self.assertRaisesRegex(ValueError, "overlap"):
                load_statcast_range(root, "2024-04-01", "2024-04-02")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-01", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-01")},
                STATCAST.read_bytes(),
            )
            with self.assertRaisesRegex(ValueError, "outside"):
                load_statcast_range(root, "2024-04-01", "2024-04-01")

    def test_league_era_loader_never_fetches_and_binds_manifest_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            query = {
                "stats": "byDateRange", "group": "pitching", "sportIds": 1,
                "gameType": "R", "playerPool": "ALL", "limit": 5000,
                "startDate": "2025-03-18", "endDate": "2025-04-30",
                "url": league_era_url("2025-03-18", "2025-04-30"),
            }
            RawStore(root).put("mlb_stats_pitching", query, ERA.read_bytes())
            records, hashes = load_league_era_from_raw(
                root,
                {2025: "2025-03-18"},
                ["2025-04-30"],
            )
            self.assertEqual(records["2025-04-30"]["source"], "mlb_stats_api")
            self.assertRegex(records["2025-04-30"]["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(hashes, [records["2025-04-30"]["manifest_sha256"]])
            with self.assertRaisesRegex(RuntimeError, "missing"):
                load_league_era_from_raw(root, {2025: "2025-03-18"}, ["2025-05-01"])


if __name__ == "__main__":
    unittest.main()
