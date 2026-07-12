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

    def test_target_and_history_game_pks_require_positive_exact_integers(self):
        parsed = parse_statcast(STATCAST)
        for invalid in (True, False, "2001", 0, -1):
            with self.subTest(target_game_pk=invalid), self.assertRaisesRegex(
                ValueError, "target game_pk must be a positive integer"
            ):
                assert_pregame_history(
                    {"game_pk": invalid, "official_date": "2024-04-03"},
                    parsed.pitches,
                )

        target = {"game_pk": 2001, "official_date": "2024-04-03"}
        for invalid in (True, False, "1001", 0, -1):
            with self.subTest(history_game_pk=invalid), self.assertRaisesRegex(
                ValueError, "history game_pk must be a positive integer"
            ):
                assert_pregame_history(
                    target,
                    [dict(parsed.pitches[0], game_pk=invalid)],
                )

        with self.assertRaisesRegex(ValueError, "target game_pk"):
            assert_pregame_history(
                target,
                [dict(parsed.pitches[0], game_pk=2001)],
            )

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

    def test_source_manifest_index_rejects_symlinks_bad_names_and_duplicate_query(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "real"
            RawStore(real).put("source", {"month": 1}, b"a\n")
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ValueError):
                build_source_manifest_index(linked)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "raw"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / "source").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                build_source_manifest_index(root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = RawStore(root).put("source", {"month": 1}, b"a\n")
            bad = artifact.manifest_path.with_name("arbitrary.manifest.json")
            bad.write_bytes(artifact.manifest_path.read_bytes())
            with self.assertRaises(RuntimeError):
                build_source_manifest_index(root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            store.put("source", {"month": 1}, b"a\n")
            store.put("source", {"month": 1}, b"different\n")
            with self.assertRaisesRegex(RuntimeError, "duplicate canonical"):
                build_source_manifest_index(root)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "raw"
            artifact = RawStore(root).put("source", {"month": 1}, b"a\n")
            outside = base / "outside.raw"
            outside.write_bytes(b"a\n")
            artifact.data_path.unlink()
            artifact.data_path.symlink_to(outside)
            with self.assertRaises(ValueError):
                build_source_manifest_index(root)


if __name__ == "__main__":
    unittest.main()
