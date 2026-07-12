import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.pipeline import (
    calibrate_stage,
    features_stage,
    lock_stage,
    main,
    run_fixture_replay,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mini_replay"


def _jsonl(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


class PipelineTest(unittest.TestCase):
    def _replay(self, output):
        return run_fixture_replay(FIXTURE, Path(output))

    def test_predictions_lock_before_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._replay(root)

            self.assertEqual(result["selection_dates"], 4)
            self.assertEqual(result["top1_count"], 4)
            self.assertTrue((root / "locked" / "lock_manifest.json").is_file())
            self.assertTrue((root / "reports" / "summary.json").is_file())
            self.assertLess(result["lock_mtime_ns"], result["outcome_mtime_ns"])

    def test_repeated_replay_has_identical_locked_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first = self._replay(base / "first")
            second = self._replay(base / "second")

            self.assertEqual(first["locked_sha256"], second["locked_sha256"])
            first_manifest = json.loads(
                (base / "first" / "locked" / "lock_manifest.json").read_text(
                    "utf-8"
                )
            )
            second_manifest = json.loads(
                (base / "second" / "locked" / "lock_manifest.json").read_text(
                    "utf-8"
                )
            )
            self.assertEqual(first["locked_sha256"], first_manifest["sha256"])
            self.assertEqual(first_manifest["sha256"], second_manifest["sha256"])
            self.assertEqual(
                (base / "first" / "locked" / "predictions.jsonl").read_bytes(),
                (base / "second" / "locked" / "predictions.jsonl").read_bytes(),
            )
            self.assertNotIn(
                b"market",
                (base / "first" / "locked" / "predictions.jsonl").read_bytes().lower(),
            )

    def test_doubleheader_is_ranked_as_one_selection_date(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._replay(root)
            rows = _jsonl(root / "locked" / "predictions.jsonl")
            doubleheader = [
                row for row in rows if row["official_date"] == "2025-05-02"
            ]

            self.assertEqual(len(doubleheader), 2)
            self.assertEqual(
                sorted(row["daily_rank"] for row in doubleheader), [1, 2]
            )
            self.assertEqual(
                sum(row["top1"] == "Yes" for row in doubleheader), 1
            )
            self.assertEqual(
                sum(row["top3"] == "Yes" for row in doubleheader), 2
            )
            self.assertEqual(
                {row["data_status"]["doubleheader"] for row in doubleheader},
                {"game1", "game2"},
            )

    def test_missing_environment_and_reconstructed_lineup_are_provenanced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._replay(root)
            rows = {
                row["game_pk"]: row
                for row in _jsonl(root / "locked" / "predictions.jsonl")
            }

            missing_environment = rows[3001]
            self.assertEqual(missing_environment["environment_quality"], 0.0)
            self.assertEqual(
                missing_environment["data_status"], "missing_environment"
            )
            reconstructed = rows[4001]
            self.assertEqual(
                reconstructed["lineup_provenance"], "reconstructed_official_boxscore"
            )
            self.assertEqual(reconstructed["data_status"], "complete")

    def test_void_top1_is_reported_but_not_graded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._replay(root)
            summary = json.loads(
                (root / "reports" / "summary.json").read_text("utf-8")
            )

            self.assertEqual(result["graded_top1"], 3)
            self.assertEqual(result["void_top1"], 1)
            self.assertEqual(summary["primary"]["N"], 3)
            self.assertEqual(summary["void_top1"], 1)
            self.assertEqual(result["graded_count"], 3)
            self.assertEqual(result["void_count"], 1)
            report = (root / "reports" / "summary.json").read_text("utf-8")
            self.assertEqual(report, json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def test_cli_enforces_prerequisites_and_rejects_symlink_stage(self):
        config = Path(__file__).parents[1] / "config" / "v5_1_bt.json"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            with self.assertRaisesRegex(RuntimeError, "missing prerequisite"):
                main(["lock", "--config", str(config), "--data-dir", str(root)])

            real = Path(td) / "real"
            real.mkdir()
            (root / "features").symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                main(["features", "--config", str(config), "--data-dir", str(root)])

    def test_stage_contracts_reject_post_cutoff_and_leaky_candidate_inputs(self):
        config = Path(__file__).parents[1] / "config" / "v5_1_bt.json"
        games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "calibration").mkdir()
            bad_training = [dict(calibration[0], official_date="2025-01-01"), *calibration[1:]]
            (root / "calibration" / "training_edges.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in bad_training), "utf-8"
            )
            with self.assertRaisesRegex(ValueError, "frozen boundary"):
                calibrate_stage(config, root)

        for mutation, message in (
            ({"market_candidate": 0.6}, "unknown"),
            ({"source_game_dates": ["2025-05-01"]}, "same-day"),
            ({"source_game_pks": [1001]}, "target game_pk"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "calibration").mkdir()
                (root / "features").mkdir()
                (root / "calibration" / "training_edges.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in calibration), "utf-8"
                )
                calibrate_stage(config, root)
                candidate = dict(games[0], **mutation)
                (root / "features" / "candidate_components.jsonl").write_text(
                    json.dumps(candidate) + "\n", "utf-8"
                )
                with self.assertRaisesRegex(ValueError, message):
                    features_stage(config, root)

    def test_features_and_lock_do_not_require_outcomes_directory(self):
        config = Path(__file__).parents[1] / "config" / "v5_1_bt.json"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "calibration").mkdir()
            (root / "features").mkdir()
            calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
            games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
            (root / "calibration" / "training_edges.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in calibration), "utf-8"
            )
            (root / "features" / "candidate_components.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in games), "utf-8"
            )
            calibrate_stage(config, root)
            features_stage(config, root)
            lock_stage(config, root)
            self.assertFalse((root / "outcomes").exists())


if __name__ == "__main__":
    unittest.main()
