import ast
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.locking import _canonical_jsonl, lock_predictions
from mlb_backtest.outcomes import join_outcomes
from tests.test_locking import MODEL_CARD_SHA, prediction


def outcomes_dir(locked: Path) -> Path:
    return locked.parent / f"{locked.name}-outcomes"


class LeakageTest(unittest.TestCase):
    def test_outcomes_cannot_join_before_lock_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(RuntimeError, "lock manifest"):
                join_outcomes(
                    Path(td) / "locked" / "missing.json",
                    [{"game_pk": 1, "actual_winner": "Home", "status": "Final"}],
                    MODEL_CARD_SHA,
                    Path(td) / "outcomes",
                )

    def test_verified_outcomes_are_nested_and_predictions_unchanged(self):
        row = prediction()
        outcome = {
            "game_pk": 1,
            "actual_winner": "Home",
            "status": "Final",
            "market_snapshot_at": "2025-05-01T22:15:00Z",
            "market_source": "Example",
            "market_favorite": "Home",
            "away_no_vig_probability": 0.4,
            "home_no_vig_probability": 0.6,
        }
        with tempfile.TemporaryDirectory() as td:
            locked = Path(td) / "locked"
            evaluated = Path(td) / "evaluated"
            manifest = lock_predictions([row], locked, MODEL_CARD_SHA)
            path = join_outcomes(
                locked / "lock_manifest.json",
                [outcome],
                MODEL_CARD_SHA,
                evaluated,
            )
            self.assertEqual(path, evaluated / "evaluated_predictions.jsonl")
            result = json.loads(path.read_text("utf-8"))
            locked_row = json.loads((locked / "predictions.jsonl").read_text("utf-8"))
            self.assertEqual({key: result[key] for key in locked_row}, locked_row)
            self.assertEqual(result["predicted_winner"], "Home")
            self.assertEqual(result["outcome"], outcome)
            self.assertNotIn("actual_winner", set(result) - {"outcome"})
            self.assertEqual(
                manifest,
                json.loads((locked / "lock_manifest.json").read_text("utf-8")),
            )

    def test_outcome_cannot_overwrite_prediction_and_unknown_keys_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([prediction()], root, MODEL_CARD_SHA)
            bad = {
                "game_pk": 1,
                "actual_winner": "Home",
                "status": "Final",
                "predicted_winner": "Away",
            }
            with self.assertRaisesRegex(ValueError, "unknown"):
                join_outcomes(
                    root / "lock_manifest.json",
                    [bad],
                    MODEL_CARD_SHA,
                    outcomes_dir(root),
                )

    def test_rejects_duplicate_unknown_games_and_invalid_status_winner(self):
        bad_sets = (
            [
                {"game_pk": 1, "actual_winner": "Home", "status": "Final"},
                {"game_pk": 1, "actual_winner": "Home", "status": "Final"},
            ],
            [{"game_pk": 2, "actual_winner": "Home", "status": "Final"}],
            [{"game_pk": 1, "actual_winner": "Other", "status": "Final"}],
            [{"game_pk": 1, "actual_winner": "Home", "status": "VOID"}],
            [{"game_pk": 1, "actual_winner": "", "status": "Final"}],
        )
        for outcomes in bad_sets:
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                lock_predictions([prediction()], root, MODEL_CARD_SHA)
                with self.assertRaises(ValueError):
                    join_outcomes(
                        root / "lock_manifest.json",
                        outcomes,
                        MODEL_CARD_SHA,
                        outcomes_dir(root),
                    )

    def test_void_winner_must_be_blank_and_partial_join_is_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([prediction()], root, MODEL_CARD_SHA)
            path = join_outcomes(
                root / "lock_manifest.json",
                [{"game_pk": 1, "actual_winner": "", "status": "VOID"}],
                MODEL_CARD_SHA,
                outcomes_dir(root),
            )
            result = json.loads(path.read_text("utf-8"))
            self.assertEqual(result["outcome"]["status"], "VOID")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([prediction(1, 1), prediction(2, 2)], root, MODEL_CARD_SHA)
            path = join_outcomes(
                root / "lock_manifest.json",
                [{"game_pk": 1, "actual_winner": "Home", "status": "Final"}],
                MODEL_CARD_SHA,
                outcomes_dir(root),
            )
            self.assertEqual(len(path.read_text("utf-8").splitlines()), 1)

    def test_tampered_predictions_manifest_and_model_hash_fail_integrity(self):
        mutations = ("prediction", "sha", "model_hash", "row_count")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                lock_predictions([prediction()], root, MODEL_CARD_SHA)
                manifest_path = root / "lock_manifest.json"
                if mutation == "prediction":
                    (root / "predictions.jsonl").write_bytes(b"{}\n")
                else:
                    manifest = json.loads(manifest_path.read_text("utf-8"))
                    if mutation == "sha":
                        manifest["sha256"] = "b" * 64
                    elif mutation == "model_hash":
                        manifest["model_card_sha256"] = "not-a-hash"
                    else:
                        manifest["row_count"] = True
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "lock manifest|integrity"):
                    join_outcomes(
                        manifest_path,
                        [{"game_pk": 1, "actual_winner": "Home", "status": "Final"}],
                        MODEL_CARD_SHA,
                        outcomes_dir(root),
                    )

    def test_requires_exact_expected_model_card_digest_before_join(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([prediction()], root, MODEL_CARD_SHA)
            outcome = [{"game_pk": 1, "actual_winner": "Home", "status": "Final"}]
            with self.assertRaisesRegex(RuntimeError, "model card|integrity"):
                join_outcomes(
                    root / "lock_manifest.json",
                    outcome,
                    "b" * 64,
                    outcomes_dir(root),
                )
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                join_outcomes(
                    root / "lock_manifest.json",
                    outcome,
                    "INVALID",
                    outcomes_dir(root),
                )

    def test_recomputed_hash_cannot_hide_noncanonical_row_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions(
                [prediction(1, 1), prediction(2, 2)], root, MODEL_CARD_SHA
            )
            predictions_path = root / "predictions.jsonl"
            lines = predictions_path.read_bytes().splitlines(keepends=True)
            predictions_path.write_bytes(b"".join(reversed(lines)))
            manifest_path = root / "lock_manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["sha256"] = hashlib.sha256(
                predictions_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "canonical|integrity"):
                join_outcomes(
                    manifest_path, [], MODEL_CARD_SHA, outcomes_dir(root)
                )

    def test_recomputed_hash_cannot_hide_invalid_locked_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lock_predictions([prediction()], root, MODEL_CARD_SHA)
            predictions_path = root / "predictions.jsonl"
            locked = json.loads(predictions_path.read_text("utf-8"))
            locked["predicted_winner"] = "Away"
            predictions_path.write_bytes(_canonical_jsonl([locked]))
            manifest_path = root / "lock_manifest.json"
            manifest = json.loads(manifest_path.read_text("utf-8"))
            manifest["sha256"] = hashlib.sha256(
                predictions_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "integrity"):
                join_outcomes(
                    manifest_path, [], MODEL_CARD_SHA, outcomes_dir(root)
                )

    def test_output_store_must_be_explicit_and_outside_locked_tree(self):
        with tempfile.TemporaryDirectory() as td:
            locked = Path(td) / "locked"
            lock_predictions([prediction()], locked, MODEL_CARD_SHA)
            outcome = [{"game_pk": 1, "actual_winner": "Home", "status": "Final"}]
            with self.assertRaises(TypeError):
                join_outcomes(locked / "lock_manifest.json", outcome, MODEL_CARD_SHA)
            for output in (locked, locked / "outcomes"):
                with self.subTest(output=output), self.assertRaisesRegex(
                    ValueError, "outside|locked"
                ):
                    join_outcomes(
                        locked / "lock_manifest.json",
                        outcome,
                        MODEL_CARD_SHA,
                        output,
                    )

    def test_oversized_outcome_probability_is_a_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            locked = Path(td) / "locked"
            lock_predictions([prediction()], locked, MODEL_CARD_SHA)
            outcome = {
                "game_pk": 1,
                "actual_winner": "Home",
                "status": "Final",
                "home_no_vig_probability": 10**1000,
            }
            with self.assertRaises(ValueError):
                join_outcomes(
                    locked / "lock_manifest.json",
                    [outcome],
                    MODEL_CARD_SHA,
                    outcomes_dir(locked),
                )

    def test_outcomes_module_has_no_feature_scoring_or_ranking_import(self):
        source_path = Path(__file__).parents[1] / "mlb_backtest" / "outcomes.py"
        tree = ast.parse(source_path.read_text("utf-8"))
        forbidden = {"features", "scoring", "ranking"}
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
        self.assertTrue(imported.isdisjoint(forbidden), imported & forbidden)


if __name__ == "__main__":
    unittest.main()
