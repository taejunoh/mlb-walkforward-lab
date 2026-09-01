"""Admission tests for immutable, pre-target calibration authority."""

from __future__ import annotations

import unittest
import json
import tempfile
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.run_spec import build_run_spec, load_run_spec, run_spec_sha256


SPEC_PATH = (
    Path(__file__).parent
    / "fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ProbabilityCalibrationPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_run_spec(SPEC_PATH)

    def test_policy_is_closed_and_has_all_seven_distinct_fold_keys(self) -> None:
        policy = self.spec.probability_calibration_policy
        self.assertEqual(
            dict(policy.fold_keys),
            {
                "O19": "C19", "O20": "C20", "O21": "C21", "O22": "C22O",
                "H22": "C22H", "O23": "C23O", "H23": "C23H",
            },
        )
        self.assertEqual(policy.pools["C22O"].training_end, "2021-12-31")
        self.assertEqual(policy.pools["C22H"].training_end, "2021-12-31")
        self.assertEqual(policy.pools["C23O"].target_year, 2023)
        self.assertEqual(policy.pools["C23H"].target_year, 2023)
        self.assertEqual(
            policy.fit_spec.as_mapping(),
            {
                "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1",
                "algorithm": "fit_logistic_v1",
                "edge_divisor": 10.0,
                "iterations": 100,
                "l2": 1e-6,
                "tolerance": 1e-10,
                "min_rows": 2,
                "required_classes": [0, 1],
            },
        )

    def test_calibration_namespace_rejects_noncanonical_shapes(self) -> None:
        self.assertEqual(
            self.spec.output_path(
                "calibration", "baseline/v5.1/O19/calibration_rows.jsonl"
            ).name,
            "calibration_rows.jsonl",
        )
        self.assertEqual(
            self.spec.output_path(
                "calibration", "B/candidate-7/O23/probability_calibration_card.json"
            ).name,
            "probability_calibration_card.json",
        )
        for relative in (
            "baseline/v5.1/O19/unknown.json",
            "baseline/v5.1/H24/calibration_rows.jsonl",
            "A/_none/O19/calibration_rows.jsonl",
            "lane_a/candidate/O19/calibration_rows.jsonl",
            "B/../O19/calibration_rows.jsonl",
            "B//candidate/O19/calibration_rows.jsonl",
        ):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.spec.output_path("calibration", relative)


class ProbabilityCalibrationAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = load_run_spec(SPEC_PATH)

    def test_identity_and_paths_are_derived_not_caller_selected(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            calibration_paths,
        )

        paths = calibration_paths(self.spec, "v5.1", "baseline", "O19", None)
        self.assertTrue(str(paths.rows).endswith("calibration/baseline/v5.1/O19/calibration_rows.jsonl"))
        self.assertTrue(str(paths.card).endswith("calibration/baseline/v5.1/O19/probability_calibration_card.json"))
        self.assertTrue(str(paths.rows_sidecar).endswith("calibration_rows.jsonl.sha256"))
        self.assertTrue(str(paths.card_sidecar).endswith("probability_calibration_card.json.sha256"))
        for identity in (
            ("v5.1", "A", "O19", None),
            ("A+B", "B", "O19", None),
            ("A+B+C", "C", "O19", "candidate"),
            ("H24", "baseline", "H24", None),
        ):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                calibration_paths(self.spec, *identity)

    def test_missing_authority_blocks_before_prediction_output_access(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            CalibrationAuthorityBlockedError,
            require_probability_calibration_card,
        )

        with self.assertRaises(CalibrationAuthorityBlockedError) as raised:
            require_probability_calibration_card(
                self.spec, "v5.1", "baseline", "O19", None
            )
        self.assertEqual(raised.exception.code, "BLOCKED_CALIBRATION_AUTHORITY_MISSING")
        self.assertEqual(str(raised.exception), "BLOCKED_CALIBRATION_AUTHORITY_MISSING")

    def test_card_payload_is_closed_and_rejects_nonfinite_coefficients(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            ProbabilityCalibrationCard,
        )

        digest = "a" * 64
        payload = {
            "model_id": "v5.1", "lane_id": "baseline", "fold_id": "O19",
            "candidate_id": None, "calibration_key": "C19", "target_year": 2019,
            "training_start": "2018-01-01", "training_end": "2018-12-31",
            "run_spec_sha256": digest, "source_gate_sha256": digest,
            "prediction_contract_sha256": digest,
            "weight_authority_path": "/tmp/weights.json", "weight_authority_sha256": digest,
            "calibration_rows_path": "/tmp/calibration_rows.jsonl", "calibration_rows_sha256": digest,
            "calibration_population_sha256": digest,
            "game_evidence_inventory_path": "/tmp/inventory.json",
            "game_evidence_inventory_sha256": digest,
            "fit_spec": self.spec.probability_calibration_policy.fit_spec.as_mapping(),
            "alpha": 0.0, "beta": 1.0,
        }
        card = ProbabilityCalibrationCard.from_payload_mapping(payload, digest, "/tmp/card.json")
        self.assertEqual(card.as_mapping(), payload)
        with self.assertRaises(ValueError):
            ProbabilityCalibrationCard.from_payload_mapping(
                payload | {"beta": float("inf")}, digest, "/tmp/card.json"
            )
        with self.assertRaises(ValueError):
            ProbabilityCalibrationCard.from_payload_mapping(
                payload | {"unknown": True}, digest, "/tmp/card.json"
            )

    def _published_baseline_authority(self, root: Path):
        from mlb_backtest.model_improvement.probability_calibration import calibration_paths
        from mlb_backtest.normalize import fit_logistic

        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        raw["outputs"]["root"] = str(root / "outputs")
        spec = build_run_spec(raw)
        paths = calibration_paths(spec, "v5.1", "baseline", "O19", None)
        paths.rows.parent.mkdir(parents=True)
        source_gate = spec.output_path("source_gate", "source_gate_receipt.json")
        source_gate.parent.mkdir(parents=True)
        source_gate.write_bytes(canonical_json_bytes({"source": "synthetic"}))
        evidence = root / "evidence.json"
        evidence.write_bytes(canonical_json_bytes({"inventory": "synthetic"}))
        rows = [
            {"game_pk": 1, "official_date": "2018-04-01", "target_year": 2019, "edge": -2.0, "home_win": 0, "feature_provenance_sha256": "1" * 64, "game_evidence_sha256": "2" * 64},
            {"game_pk": 2, "official_date": "2018-05-01", "target_year": 2019, "edge": -1.0, "home_win": 0, "feature_provenance_sha256": "3" * 64, "game_evidence_sha256": "4" * 64},
            {"game_pk": 3, "official_date": "2018-06-01", "target_year": 2019, "edge": 1.0, "home_win": 1, "feature_provenance_sha256": "5" * 64, "game_evidence_sha256": "6" * 64},
            {"game_pk": 4, "official_date": "2018-07-01", "target_year": 2019, "edge": 2.0, "home_win": 1, "feature_provenance_sha256": "7" * 64, "game_evidence_sha256": "8" * 64},
        ]
        row_bytes = b"".join(canonical_json_bytes(row) for row in rows)
        alpha, beta = fit_logistic((row["edge"] / 10.0 for row in rows), (row["home_win"] for row in rows), iterations=100, l2=1e-6, tolerance=1e-10)
        weight = REPOSITORY_ROOT / "tests/fixtures/model_improvement/v51_card.json"
        payload = {
            "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V1",
            "model_id": "v5.1", "lane_id": "baseline", "fold_id": "O19", "candidate_id": None,
            "calibration_key": "C19", "target_year": 2019, "training_start": "2018-01-01", "training_end": "2018-12-31",
            "run_spec_sha256": run_spec_sha256(spec), "source_gate_sha256": sha256(source_gate.read_bytes()).hexdigest(),
            "prediction_contract_sha256": spec.prediction_contract_sha256,
            "weight_authority_path": str(weight), "weight_authority_sha256": sha256(weight.read_bytes()).hexdigest(),
            "calibration_rows_path": str(paths.rows), "calibration_rows_sha256": sha256(row_bytes).hexdigest(),
            "calibration_population_sha256": sha256(canonical_json_bytes([1, 2, 3, 4])).hexdigest(),
            "game_evidence_inventory_path": str(evidence), "game_evidence_inventory_sha256": sha256(evidence.read_bytes()).hexdigest(),
            "fit_spec": spec.probability_calibration_policy.fit_spec.as_mapping(), "alpha": alpha, "beta": beta,
        }
        card_bytes = canonical_json_bytes(payload)
        paths.rows.write_bytes(row_bytes)
        paths.rows_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(row_bytes).hexdigest()}))
        paths.card_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(card_bytes).hexdigest()}))
        paths.card.write_bytes(card_bytes)
        return spec, paths, payload, rows

    def test_load_recomputes_and_rereads_exact_published_authority(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import load_probability_calibration_card

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            spec, paths, payload, _ = self._published_baseline_authority(Path(directory))
            card = load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)
            self.assertEqual(card.as_mapping(), {key: value for key, value in payload.items() if key != "schema"})
            self.assertEqual(card.path, str(paths.card))

    def test_present_authority_rejects_rows_and_card_mutations(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import load_probability_calibration_card

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            spec, paths, payload, rows = self._published_baseline_authority(Path(directory))
            attacks = {
                "fit_spec": payload | {"fit_spec": payload["fit_spec"] | {"l2": 0.1}},
                "alpha": payload | {"alpha": payload["alpha"] + 0.01},
                "key": payload | {"calibration_key": "C20"},
                "path": payload | {"calibration_rows_path": str(paths.rows.parent / "other.jsonl")},
            }
            for name, attack in attacks.items():
                with self.subTest(name=name):
                    raw = canonical_json_bytes(attack)
                    paths.card_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(raw).hexdigest()}))
                    paths.card.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)
            # Restore exact card, then make a target-year row with matching digest.
            target_rows = rows[:-1] + [rows[-1] | {"official_date": "2019-01-01"}]
            row_bytes = b"".join(canonical_json_bytes(row) for row in target_rows)
            changed = payload | {"calibration_rows_sha256": sha256(row_bytes).hexdigest()}
            changed_bytes = canonical_json_bytes(changed)
            paths.rows.write_bytes(row_bytes)
            paths.rows_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(row_bytes).hexdigest()}))
            paths.card_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(changed_bytes).hexdigest()}))
            paths.card.write_bytes(changed_bytes)
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)

    def test_present_authority_rejects_bound_inputs_and_reread_drift(self) -> None:
        from mlb_backtest.model_improvement.probability_calibration import (
            _load_once,
            load_probability_calibration_card,
        )

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            spec, paths, payload, rows = self._published_baseline_authority(Path(directory))

            def rewrite_card(changed):
                raw = canonical_json_bytes(changed)
                paths.card.write_bytes(raw)
                paths.card_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(raw).hexdigest()}))

            source_gate = spec.output_path("source_gate", "source_gate_receipt.json")
            source_gate.write_bytes(canonical_json_bytes({"source": "substituted"}))
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)
            source_gate.write_bytes(canonical_json_bytes({"source": "synthetic"}))

            evidence = Path(payload["game_evidence_inventory_path"])
            evidence.write_bytes(canonical_json_bytes({"inventory": "substituted"}))
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)
            evidence.write_bytes(canonical_json_bytes({"inventory": "synthetic"}))

            rewrite_card(payload | {"weight_authority_sha256": "0" * 64})
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)
            rewrite_card(payload)
            paths.rows_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": "0" * 64}))
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)

            duplicate_rows = rows[:-1] + [rows[-1] | {"game_pk": 3}]
            duplicate_bytes = b"".join(canonical_json_bytes(row) for row in duplicate_rows)
            duplicate_card = payload | {"calibration_rows_sha256": sha256(duplicate_bytes).hexdigest()}
            paths.rows.write_bytes(duplicate_bytes)
            paths.rows_sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": sha256(duplicate_bytes).hexdigest()}))
            rewrite_card(duplicate_card)
            with self.assertRaises(ValueError):
                load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)

        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            spec, _, _, _ = self._published_baseline_authority(Path(directory))
            first = _load_once(spec, "v5.1", "baseline", "O19", None)
            changed = first.__class__.from_payload_mapping(
                first.as_mapping() | {"alpha": first.alpha + 1.0},
                first.artifact_sha256, first.path,
            )
            with patch(
                "mlb_backtest.model_improvement.probability_calibration._load_once",
                side_effect=(first, changed),
            ):
                with self.assertRaises(ValueError):
                    load_probability_calibration_card(spec, "v5.1", "baseline", "O19", None)
