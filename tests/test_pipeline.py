import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.pipeline import (
    calibrate_stage,
    features_stage,
    lock_stage,
    main,
    outcomes_stage,
    report_stage,
    run_fixture_replay,
)
from mlb_backtest.config import load_config
from mlb_backtest.manifest import RawStore
from mlb_backtest.sources import schedule_url


FIXTURE = Path(__file__).parent / "fixtures" / "mini_replay"
CONFIG = Path(__file__).parents[1] / "config" / "v5_1_bt.json"


def _jsonl(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _copy_raw_schedule(root):
    config = load_config(CONFIG)
    payload = (FIXTURE / "schedule.json").read_bytes()
    store = RawStore(root / "raw")
    for role, window_name in (("training", "training_window"), ("backtest", "initial_window")):
        window = config[window_name]
        store.put(
            "mlb_schedule",
            {
                "role": role,
                "start": window["start"],
                "end": window["end"],
                "url": schedule_url(window["start"], window["end"]),
            },
            payload,
        )


def _prepare_through_lock(root, candidates=None):
    calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
    games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
    _copy_raw_schedule(root)
    _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
    _write_jsonl(
        root / "features" / "candidate_components.jsonl",
        games if candidates is None else candidates,
    )
    calibrate_stage(CONFIG, root)
    features_stage(CONFIG, root)
    lock_stage(CONFIG, root)


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
            self.assertTrue(
                (root / "initial_locked" / "lock_manifest.json").is_file()
            )
            self.assertTrue(
                (
                    root
                    / "initial_outcomes"
                    / "evaluated"
                    / "evaluated_predictions.jsonl"
                ).is_file()
            )
            self.assertLess(result["lock_mtime_ns"], result["outcome_mtime_ns"])
            self.assertLess(
                result["initial_lock_mtime_ns"],
                result["initial_outcome_mtime_ns"],
            )
            self.assertLess(
                result["initial_outcome_mtime_ns"], result["lock_mtime_ns"]
            )
            self.assertEqual(
                result["initial_completion"],
                {
                    "graded_top1": 2,
                    "void_top1": 1,
                    "selection_dates": 3,
                    "target": 3,
                    "complete": False,
                    "dates_needed": 1,
                },
            )
            self.assertEqual(result["extension_dates"], ["2025-05-04"])

    def test_repeated_replay_has_identical_locked_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first = self._replay(base / "first")
            second = self._replay(base / "second")

            self.assertEqual(first["locked_sha256"], second["locked_sha256"])
            self.assertEqual(
                first["initial_locked_sha256"], second["initial_locked_sha256"]
            )
            self.assertNotEqual(
                first["initial_locked_sha256"], first["locked_sha256"]
            )
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
            self.assertEqual(
                (
                    base
                    / "first"
                    / "initial_locked"
                    / "predictions.jsonl"
                ).read_bytes(),
                (
                    base
                    / "second"
                    / "initial_locked"
                    / "predictions.jsonl"
                ).read_bytes(),
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
                sorted(row["scheduled_start_utc"] for row in doubleheader),
                ["2025-05-02T17:05:00Z", "2025-05-02T23:10:00Z"],
            )
            self.assertTrue(
                all(row["data_status"] == "COMPLETE" for row in doubleheader)
            )
            self.assertEqual(
                {row["doubleheader_marker"] for row in doubleheader},
                {"GAME1", "GAME2"},
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
                missing_environment["data_status"], "INCOMPLETE"
            )
            reconstructed = rows[4001]
            self.assertEqual(
                reconstructed["lineup_provenance"], "RECONSTRUCTED_FINAL_LINEUP"
            )
            self.assertEqual(reconstructed["data_status"], "COMPLETE")

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
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            with self.assertRaisesRegex(RuntimeError, "missing prerequisite"):
                main(["lock", "--config", str(CONFIG), "--data-dir", str(root)])

            real = Path(td) / "real"
            real.mkdir()
            (root / "features").symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                main(["features", "--config", str(CONFIG), "--data-dir", str(root)])

    def test_stage_contracts_reject_post_cutoff_and_leaky_candidate_inputs(self):
        games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            (root / "calibration").mkdir()
            bad_training = [dict(calibration[0], official_date="2025-01-01"), *calibration[1:]]
            (root / "calibration" / "training_edges.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in bad_training), "utf-8"
            )
            with self.assertRaisesRegex(ValueError, "frozen boundary"):
                calibrate_stage(CONFIG, root)

        for mutation, message in (
            ({"market_candidate": 0.6}, "unknown"),
            ({"data_status": {"status": "complete", "market": {"favorite": "Home"}}}, "market"),
            ({"lineup_provenance": {"source": "official", "outcome": {"winner": "Home"}}}, "outcome"),
            ({"data_status": "complete"}, "data_status"),
            ({"lineup_provenance": []}, "lineup_provenance"),
            ({"doubleheader_marker": {"slot": 1}}, "doubleheader_marker"),
            ({"source_game_dates": ["2025-05-01"]}, "same-day"),
            ({"source_game_pks": [1001]}, "target game_pk"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _copy_raw_schedule(root)
                (root / "calibration").mkdir()
                (root / "features").mkdir()
                (root / "calibration" / "training_edges.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in calibration), "utf-8"
                )
                calibrate_stage(CONFIG, root)
                candidate = dict(games[0], **mutation)
                (root / "features" / "candidate_components.jsonl").write_text(
                    json.dumps(candidate) + "\n", "utf-8"
                )
                with self.assertRaisesRegex(ValueError, message):
                    features_stage(CONFIG, root)

    def test_calibrate_requires_raw_schedule_provenance(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            with self.assertRaisesRegex(RuntimeError, "raw/schedule"):
                calibrate_stage(CONFIG, root)

    def test_features_require_raw_schedule_provenance(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            _write_jsonl(root / "features" / "candidate_components.jsonl", games)
            calibrate_stage(CONFIG, root)
            schedule_root = root / "raw" / "mlb_schedule"
            schedule_root.rename(root / "raw" / "removed_schedule")
            with self.assertRaisesRegex(RuntimeError, "raw/schedule"):
                features_stage(CONFIG, root)

    def test_outcomes_require_every_locked_top1_outcome(self):
        outcomes = json.loads((FIXTURE / "outcomes.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_through_lock(root)
            locked = _jsonl(root / "locked" / "predictions.jsonl")
            top1_game_pks = {
                row["game_pk"] for row in locked if row["top1"] == "Yes"
            }
            missing = next(iter(top1_game_pks))
            _write_jsonl(
                root / "outcomes" / "outcomes_input.jsonl",
                [row for row in outcomes if row["game_pk"] != missing],
            )
            with self.assertRaisesRegex(ValueError, "missing.*Top 1"):
                outcomes_stage(CONFIG, root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_through_lock(root)
            locked = _jsonl(root / "locked" / "predictions.jsonl")
            top1_game_pks = {
                row["game_pk"] for row in locked if row["top1"] == "Yes"
            }
            _write_jsonl(
                root / "outcomes" / "outcomes_input.jsonl",
                [row for row in outcomes if row["game_pk"] in top1_game_pks],
            )
            evaluated = outcomes_stage(CONFIG, root)
            self.assertEqual(len(_jsonl(evaluated)), len(top1_game_pks))

    def test_report_requires_every_locked_top1_outcome(self):
        outcomes = json.loads((FIXTURE / "outcomes.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_through_lock(root)
            _write_jsonl(root / "outcomes" / "outcomes_input.jsonl", outcomes)
            evaluated_path = outcomes_stage(CONFIG, root)
            evaluated = _jsonl(evaluated_path)
            missing = next(row["game_pk"] for row in evaluated if row["top1"] == "Yes")
            _write_jsonl(
                evaluated_path,
                [row for row in evaluated if row["game_pk"] != missing],
            )
            with self.assertRaisesRegex(ValueError, "missing.*Top 1"):
                report_stage(CONFIG, root)

    def test_features_and_lock_do_not_require_outcomes_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
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
            calibrate_stage(CONFIG, root)
            features_stage(CONFIG, root)
            lock_stage(CONFIG, root)
            self.assertFalse((root / "outcomes").exists())


if __name__ == "__main__":
    unittest.main()
