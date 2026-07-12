import tempfile
import unittest
from pathlib import Path

from mlb_backtest.manifest import RawStore
from mlb_backtest.prepare import assert_pregame_history, build_source_manifest_index
from mlb_backtest.statcast import parse_statcast


ROOT = Path(__file__).parents[1]
STATCAST = ROOT / "tests" / "fixtures" / "statcast_mini.csv"


class PrepareBoundaryTest(unittest.TestCase):
    def test_same_day_and_target_game_history_is_rejected(self):
        parsed = parse_statcast(STATCAST)
        target = {"game_pk": 2001, "official_date": "2024-04-03"}
        assert_pregame_history(target, parsed.pitches)
        same_day = list(parsed.pitches) + [
            dict(parsed.pitches[0], game_date="2024-04-03")
        ]
        with self.assertRaisesRegex(ValueError, "strictly before"):
            assert_pregame_history(target, same_day)
        target_game = list(parsed.pitches) + [
            dict(parsed.pitches[0], game_pk=2001)
        ]
        with self.assertRaisesRegex(ValueError, "target game_pk"):
            assert_pregame_history(target, target_game)

    def test_source_manifest_index_is_sorted_and_hash_backed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            second = store.put("z_source", {"month": 2}, b"z\n")
            first = store.put("a_source", {"month": 1}, b"a\n")
            index = build_source_manifest_index(root)
            self.assertEqual(
                [row["source"] for row in index], ["a_source", "z_source"]
            )
            self.assertEqual(index[0]["sha256"], first.sha256)
            self.assertEqual(index[1]["sha256"], second.sha256)


if __name__ == "__main__":
    unittest.main()
