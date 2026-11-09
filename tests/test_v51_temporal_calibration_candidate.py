import hashlib
import json
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scripts.analysis.v51_temporal_calibration_candidate import (
    Counts,
    EvaluationPins,
    PreparePins,
    evaluate,
    prepare,
)


def metric(value):
    return {"value": value, "quality": 1.0, "sample_size": 200}


def raw_game(game_pk, day, shift):
    def side(code, delta):
        return {
            "team_code": code,
            "starter": {
                "pitcher_id": game_pk * 10 + delta,
                "career_bf": 220,
                "season_xwoba": metric(0.30 + shift * delta),
                "k_minus_bb": metric(0.15 - shift * delta),
                "fip": metric(3.5 + shift * delta),
                "last5_xwoba": metric(0.31 + shift * delta),
                "expected_ip": metric(5.5 - shift * delta),
                "velocity_change": metric(0.5 - shift * delta),
            },
            "offense": {
                "players": [{"player_id": i + delta * 100, "batting_slot": i} for i in range(1, 10)],
                "platoon_xwoba": metric(0.32 - shift * delta),
                "last14_xwoba": metric(0.33 - shift * delta),
                "bb_minus_k": metric(-0.10 + shift * delta),
                "barrel_rate": metric(0.08 - shift * delta),
                "iso": metric(0.16 - shift * delta),
            },
            "bullpen": {
                "season_xwoba": metric(0.31 + shift * delta),
                "k_minus_bb": metric(0.12 - shift * delta),
                "last14_xwoba": metric(0.32 + shift * delta),
                "availability": metric(0.8 - shift * delta),
            },
            "environment": metric(50.0),
            "defense": metric(50.0),
            "schedule": metric(55.0),
        }

    return {
        "game_pk": game_pk,
        "official_date": day,
        "eligible": True,
        "reasons": [],
        "lineup_provenance": "RECONSTRUCTED_FINAL_LINEUP",
        "away": side("AWY", -1),
        "home": side("HOM", 1),
    }


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TemporalCalibrationCandidateTest(unittest.TestCase):
    def make_source(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        data = root / "data/v5_1_bt"
        audit = root / "audit/v5_1_bt"
        raw22 = [raw_game(10 + index, f"2022-04-{10 + index:02d}", 0.01 * (index + 1)) for index in range(3)]
        raw23 = [raw_game(20 + index, f"2023-04-{10 + index:02d}", 0.01 * (index + 1)) for index in range(4)]
        raw24 = [raw_game(30 + index, f"2024-04-{10 + index:02d}", 0.2) for index in range(2)]
        raw25 = [raw_game(40 + index, f"2025-05-{1 + index:02d}", 0.015 * (index + 1)) for index in range(3)]
        training = raw22 + raw23 + raw24
        train_path = data / "calibration/training_raw_features.jsonl"
        write_jsonl(train_path, training)
        edge_path = data / "calibration/training_edges.jsonl"
        write_jsonl(edge_path, [
            {"game_pk": row["game_pk"], "official_date": row["official_date"], "edge": 999.0, "home_win": index % 2}
            for index, row in enumerate(training)
        ])
        raw25_path = data / "features/candidate_raw_features.jsonl"
        write_jsonl(raw25_path, raw25)
        provenance_path = data / "features/candidate_provenance.json"
        provenance_path.write_text(json.dumps({"candidate_raw_sha256": digest(raw25_path)}, sort_keys=True) + "\n")
        frozen_path = data / "calibration/frozen_inputs.json"
        frozen_path.write_text("{}\n")
        pipeline_path = data / "calibration/model_card.json"
        pipeline_path.write_text(json.dumps({"model_version": "v5.1-BT"}) + "\n")
        card_path = audit / "model_card.json"
        card_path.parent.mkdir(parents=True, exist_ok=True)
        card_path.write_text(json.dumps({"model_version": "v5.1-BT", "sha256": {"pipeline_model_card_file": digest(pipeline_path)}}) + "\n")
        locked = []
        evaluated = []
        for index, row in enumerate(raw25):
            p_home = 0.55 + index * 0.01
            locked_row = {
                "game_pk": row["game_pk"], "official_date": row["official_date"], "home_team": "HOM", "away_team": "AWY",
                "p_home": p_home, "predicted_side": "home", "predicted_winner": "HOM", "pick_probability": p_home,
            }
            locked.append(locked_row)
            evaluated.append({**locked_row, "outcome": {"status": "Final", "actual_winner": "HOM" if index % 2 == 0 else "AWY"}})
        locked_path = data / "locked/predictions.jsonl"
        evaluated_path = data / "outcomes/evaluated/evaluated_predictions.jsonl"
        write_jsonl(locked_path, locked)
        write_jsonl(evaluated_path, evaluated)
        prepare_pins = PreparePins(
            model_card=digest(card_path), frozen_inputs=digest(frozen_path), training_raw=digest(train_path), training_edges=digest(edge_path),
            candidate_raw=digest(raw25_path), candidate_provenance=digest(provenance_path),
        )
        evaluation_pins = EvaluationPins(locked=digest(locked_path), evaluated=digest(evaluated_path))
        return temp, root, prepare_pins, evaluation_pins

    def test_prepare_excludes_future_raw_and_uses_rebuilt_2023_edges(self):
        temp, root, prepare_pins, _ = self.make_source()
        self.addCleanup(temp.cleanup)
        out = root / "out"
        receipt = prepare(root, out, pins=prepare_pins, counts=Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3))
        self.assertEqual(receipt["normalization"]["training_year"], 2022)
        self.assertEqual(receipt["calibration"]["training_year"], 2023)
        self.assertEqual(receipt["calibration"]["training_count"], 4)
        self.assertEqual(receipt["candidate"]["count"], 3)
        self.assertRegex(receipt["execution_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(set(receipt["scoring_dependency_sha256"]), {"normalize.py", "scoring.py", "scoring_prep.py"})
        predictions = [json.loads(line) for line in (out / "candidate_predictions.jsonl").read_text().splitlines()]
        self.assertEqual([row["game_pk"] for row in predictions], [40, 41, 42])
        self.assertTrue(all(math.isfinite(row["p_home"]) and 0.1 <= row["p_home"] <= 0.9 for row in predictions))
        self.assertNotEqual(receipt["calibration"]["edge_sha256"], digest(root / "data/v5_1_bt/calibration/training_edges.jsonl"))

    def test_prepare_succeeds_without_locked_or_evaluated_outcome_files(self):
        temp, root, prepare_pins, _ = self.make_source()
        self.addCleanup(temp.cleanup)
        (root / "data/v5_1_bt/locked/predictions.jsonl").unlink()
        (root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl").unlink()
        receipt = prepare(
            root,
            root / "prepared",
            pins=prepare_pins,
            counts=Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3),
        )
        self.assertEqual(receipt["candidate"]["count"], 3)

    def test_evaluate_rejects_invalid_prediction_seal_before_opening_outcomes(self):
        temp, root, prepare_pins, evaluation_pins = self.make_source()
        self.addCleanup(temp.cleanup)
        prepared = root / "prepared"
        prepare(root, prepared, pins=prepare_pins, counts=Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3))
        (prepared / "candidate_predictions.jsonl.sha256").write_text("0" * 64 + "\n")
        (root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "prediction seal"):
            evaluate(root, prepared, root / "evaluation", pins=evaluation_pins, fixed_population=3)

    def test_evaluate_rejects_dependency_mutation_before_opening_outcomes(self):
        temp, root, prepare_pins, evaluation_pins = self.make_source()
        self.addCleanup(temp.cleanup)
        dependencies = {}
        for name in ("normalize.py", "scoring.py", "scoring_prep.py"):
            path = root / "dependencies" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name + "\n")
            dependencies[name] = path
        prepared = root / "prepared"
        prepare(
            root,
            prepared,
            pins=prepare_pins,
            counts=Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3),
            dependency_paths=dependencies,
        )
        dependencies["normalize.py"].write_text("changed\n")
        (root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "dependency hash"):
            evaluate(
                root,
                prepared,
                root / "evaluation",
                pins=evaluation_pins,
                fixed_population=3,
                dependency_paths=dependencies,
            )

    def test_evaluate_rejects_runner_hash_mutation_before_opening_outcomes(self):
        temp, root, prepare_pins, evaluation_pins = self.make_source()
        self.addCleanup(temp.cleanup)
        prepared = root / "prepared"
        prepare(
            root,
            prepared,
            pins=prepare_pins,
            counts=Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3),
        )
        receipt_path = prepared / "prepare_receipt.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["local_scoring_code_sha256"] = "0" * 64
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        (prepared / "prepare_receipt.json.sha256").write_text(digest(receipt_path) + "\n")
        (root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl").unlink()
        with self.assertRaisesRegex(ValueError, "runner code hash"):
            evaluate(root, prepared, root / "evaluation", pins=evaluation_pins, fixed_population=3)

    def test_evaluate_rejects_actual_winner_outside_locked_teams(self):
        temp, root, prepare_pins, evaluation_pins = self.make_source()
        self.addCleanup(temp.cleanup)
        prepared = root / "prepared"
        prepare(
            root,
            prepared,
            pins=prepare_pins,
            counts=Counts(
                raw_2022=3,
                eligible_2022=3,
                raw_2023=4,
                eligible_2023=4,
                raw_2024=2,
                candidate_2025=3,
            ),
        )
        evaluated_path = root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl"
        evaluated = [json.loads(line) for line in evaluated_path.read_text().splitlines()]
        evaluated[0]["outcome"]["actual_winner"] = "UNKNOWN"
        write_jsonl(evaluated_path, evaluated)
        changed_pins = replace(evaluation_pins, evaluated=digest(evaluated_path))

        with self.assertRaisesRegex(ValueError, r"actual_winner.*40"):
            evaluate(root, prepared, root / "evaluation", pins=changed_pins, fixed_population=3)

    def test_evaluate_requires_exact_keysets_and_produces_fixed_bins(self):
        temp, root, prepare_pins, evaluation_pins = self.make_source()
        self.addCleanup(temp.cleanup)
        prepared = root / "prepared"
        prepare(root, prepared, pins=prepare_pins, counts=Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3))
        report = evaluate(root, prepared, root / "evaluation", pins=evaluation_pins, fixed_population=3)
        self.assertEqual(report["candidate"]["N"], 3)
        self.assertRegex(report["execution_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(sum(item["N"] for item in report["candidate"]["fixed_bins"]), 3)
        self.assertEqual(report["baseline"]["N"], 3)
        self.assertTrue((root / "evaluation/evaluation_report.json.sha256").exists())
        with self.assertRaisesRegex(FileExistsError, "refusing overwrite"):
            evaluate(root, prepared, root / "evaluation", pins=evaluation_pins, fixed_population=3)

    def test_2024_raw_and_labels_cannot_change_2022_normalization_or_2023_fit(self):
        temp, root, prepare_pins, _ = self.make_source()
        self.addCleanup(temp.cleanup)
        counts = Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3)
        first = prepare(root, root / "first", pins=prepare_pins, counts=counts)
        training_path = root / "data/v5_1_bt/calibration/training_raw_features.jsonl"
        training = [json.loads(line) for line in training_path.read_text().splitlines()]
        training[-1]["home"]["starter"]["fip"]["value"] = 99.0
        write_jsonl(training_path, training)
        edges_path = root / "data/v5_1_bt/calibration/training_edges.jsonl"
        edges_path.write_bytes(edges_path.read_bytes() + b'{"official_date":"2024-05-01","home_win": not-json}\n')
        changed_pins = replace(prepare_pins, training_raw=digest(training_path), training_edges=digest(edges_path))
        second = prepare(root, root / "second", pins=changed_pins, counts=counts)
        self.assertEqual(first["normalization"]["value"], second["normalization"]["value"])
        self.assertEqual(first["calibration"]["alpha"], second["calibration"]["alpha"])
        self.assertEqual(first["calibration"]["beta"], second["calibration"]["beta"])

    def test_prepare_predictions_are_deterministic_for_identical_pinned_inputs(self):
        temp_a, root_a, pins_a, _ = self.make_source()
        temp_b, root_b, pins_b, _ = self.make_source()
        self.addCleanup(temp_a.cleanup)
        self.addCleanup(temp_b.cleanup)
        counts = Counts(raw_2022=3, eligible_2022=3, raw_2023=4, eligible_2023=4, raw_2024=2, candidate_2025=3)
        prepare(root_a, root_a / "out", pins=pins_a, counts=counts)
        prepare(root_b, root_b / "out", pins=pins_b, counts=counts)
        self.assertEqual((root_a / "out/candidate_predictions.jsonl").read_bytes(), (root_b / "out/candidate_predictions.jsonl").read_bytes())
