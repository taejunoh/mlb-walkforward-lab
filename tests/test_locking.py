import hashlib
import json
import math
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.locking import LOCK_REQUIRED_FIELDS, lock_predictions


MODEL_CARD_SHA = "a" * 64


def prediction(game_pk=1, rank=1, *, official_date="2025-05-01"):
    return {
        "game_pk": game_pk,
        "official_date": official_date,
        "scheduled_start_utc": f"{official_date}T23:00:00Z",
        "lock_at_utc": f"{official_date}T22:15:00Z",
        "away_team": "Away",
        "home_team": "Home",
        "predicted_side": "home",
        "predicted_winner": "Home",
        "p_home": 0.61234567894,
        "pick_probability": 0.61234567894,
        "confidence": 8.25,
        "uncertainty": "Low",
        "tier": "B",
        "full_agreement": "Yes",
        "model_version": "v5.1-BT",
        "protocol_phase": "Backtest",
        "daily_rank": rank,
        "top1": "Yes" if rank == 1 else "No",
        "top3": "Yes" if rank <= 3 else "No",
        "starter_edge_abs": 4.125,
        "overall_completeness": 0.95,
    }


def _concurrent_lock(root, start_event, ready_queue, result_queue):
    ready_queue.put(True)
    if not start_event.wait(timeout=10):
        result_queue.put(("error", "start timeout"))
        return
    try:
        manifest = lock_predictions([prediction()], Path(root), MODEL_CARD_SHA)
        result_queue.put(
            ("ok", manifest["sha256"], manifest["created_at_utc"])
        )
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


class LockingTest(unittest.TestCase):
    def test_concurrent_identical_writers_share_one_stable_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            ready_queue = context.Queue()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_lock,
                    args=(td, start_event, ready_queue, result_queue),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready_queue.get(timeout=10))
            start_event.set()
            results = [result_queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(results[0][0], "ok", results)
            self.assertEqual(results[0], results[1])

    def test_required_schema_is_explicit_and_lock_writes_integrity_manifest(self):
        self.assertEqual(set(prediction()), set(LOCK_REQUIRED_FIELDS))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = lock_predictions([prediction()], root, MODEL_CARD_SHA)
            payload = (root / "predictions.jsonl").read_bytes()
            self.assertEqual(manifest["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(manifest["row_count"], 1)
            self.assertEqual(manifest["model_version"], "v5.1-BT")
            self.assertEqual(manifest["protocol_phase"], "Backtest")
            self.assertEqual(manifest["model_card_sha256"], MODEL_CARD_SHA)
            self.assertEqual(manifest["schema_version"], 1)
            self.assertRegex(manifest["created_at_utc"], r"Z$")
            self.assertEqual(
                manifest,
                json.loads((root / "lock_manifest.json").read_text("utf-8")),
            )

    def test_hash_and_bytes_are_stable_across_order_and_directories(self):
        rows = [prediction(2, 2), prediction(1, 1)]
        with tempfile.TemporaryDirectory() as td:
            one = Path(td) / "one"
            two = Path(td) / "two"
            first = lock_predictions(rows, one, MODEL_CARD_SHA)
            second = lock_predictions(list(reversed(rows)), two, MODEL_CARD_SHA)
            self.assertEqual(first["sha256"], second["sha256"])
            self.assertEqual(
                (one / "predictions.jsonl").read_bytes(),
                (two / "predictions.jsonl").read_bytes(),
            )

    def test_recursive_floats_are_unquoted_numbers_with_exactly_ten_places(self):
        row = prediction()
        row.update(
            {
                "home_starter_score": 55.0,
                "away_starter_score": 45.0,
                "starter_edge": 10.0,
                "starter_quality": 0.8,
                "data_status": {"quality": 0.125},
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([row], root, MODEL_CARD_SHA)
            payload = (root / "predictions.jsonl").read_bytes()
            self.assertIn(b'"p_home":0.6123456789', payload)
            self.assertIn(b'"home_starter_score":55.0000000000', payload)
            self.assertIn(b'"quality":0.1250000000', payload)
            self.assertNotIn(b'"50.0000000000"', payload)
            self.assertTrue(payload.endswith(b"\n"))

    def test_identical_rerun_returns_original_stable_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = lock_predictions([prediction()], root, MODEL_CARD_SHA)
            second = lock_predictions([prediction()], root, MODEL_CARD_SHA)
            self.assertEqual(first, second)

    def test_differing_rerun_or_model_card_hash_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([prediction()], root, MODEL_CARD_SHA)
            changed = prediction()
            changed["confidence"] = 8.5
            with self.assertRaises(RuntimeError):
                lock_predictions([changed], root, MODEL_CARD_SHA)
            with self.assertRaises(RuntimeError):
                lock_predictions([prediction()], root, "b" * 64)

    def test_rejects_unknown_market_outcome_and_arbitrary_keys(self):
        for key in (
            "actual_winner",
            "status",
            "market_favorite",
            "home_no_vig_probability",
            "closing_moneyline",
            "arbitrary",
        ):
            row = prediction()
            row[key] = "forbidden"
            with self.subTest(key=key), tempfile.TemporaryDirectory() as td:
                with self.assertRaisesRegex(ValueError, "unknown"):
                    lock_predictions([row], Path(td), MODEL_CARD_SHA)

    def test_rejects_identity_probability_rank_and_lock_inconsistency(self):
        changes = (
            ("model_version", "v5.2"),
            ("protocol_phase", "Live"),
            ("predicted_winner", "Away"),
            ("pick_probability", 0.8),
            ("top1", "No"),
            ("top3", "No"),
            ("lock_at_utc", "2025-05-01T22:14:59Z"),
            ("confidence", True),
            ("p_home", math.nan),
            ("overall_completeness", 1.1),
        )
        for key, value in changes:
            row = prediction()
            row[key] = value
            with self.subTest(key=key), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    lock_predictions([row], Path(td), MODEL_CARD_SHA)

    def test_rejects_probability_that_rounds_to_no_pick_in_locked_bytes(self):
        row = prediction()
        row["p_home"] = 0.50000000001
        row["pick_probability"] = 0.50000000001
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "precision"):
                lock_predictions([row], Path(td), MODEL_CARD_SHA)

        row = prediction()
        row["p_home"] = 0.6123456789498
        row["pick_probability"] = 0.6123456789502
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "precision"):
                lock_predictions([row], Path(td), MODEL_CARD_SHA)

    def test_requires_unique_games_and_one_top1_per_date(self):
        invalid = (
            [prediction(1, 1), prediction(1, 2)],
            [prediction(1, 2)],
            [prediction(1, 1), prediction(2, 1)],
        )
        for rows in invalid:
            with self.subTest(rows=rows), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    lock_predictions(rows, Path(td), MODEL_CARD_SHA)

    def test_symlink_and_nonregular_leaf_paths_are_rejected(self):
        for leaf in ("predictions.jsonl", "lock_manifest.json", ".lock"):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "lock"
                root.mkdir()
                outside = Path(td) / "outside"
                outside.write_text("untouched", encoding="utf-8")
                (root / leaf).symlink_to(outside)
                with self.assertRaises((ValueError, RuntimeError)):
                    lock_predictions([prediction()], root, MODEL_CARD_SHA)
                self.assertEqual(outside.read_text("utf-8"), "untouched")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "lock"
            root.mkdir()
            (root / "predictions.jsonl").mkdir()
            with self.assertRaises((ValueError, RuntimeError)):
                lock_predictions([prediction()], root, MODEL_CARD_SHA)

    def test_rejects_bool_and_nonfinite_recursively(self):
        for value in (True, math.inf, -math.inf, math.nan):
            row = prediction()
            row["data_status"] = {"value": value}
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    lock_predictions([row], Path(td), MODEL_CARD_SHA)


if __name__ == "__main__":
    unittest.main()
