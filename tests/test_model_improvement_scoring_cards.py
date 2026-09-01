"""TDD coverage for immutable pre-prediction scoring-card authorities."""

from __future__ import annotations

import dataclasses
import ast
import json
import shutil
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.contracts import FOLD_IDS
from mlb_backtest.model_improvement.run_spec import build_run_spec, run_spec_sha256
from mlb_backtest.model_improvement.scoring_cards import (
    PredictionWeightAuthority,
    load_scoring_model_card,
    resolve_prediction_weight_authority,
    scoring_model_card_path,
    seal_scoring_model_card,
)
import mlb_backtest.model_improvement.scoring_cards as scoring_cards


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
SCHEMA = ROOT / "schemas/model_improvement/scoring_model_card.schema.json"
SHA = "a" * 64
SHA_B = "b" * 64
V51_WEIGHTS = {
    "starter": 35,
    "offense": 25,
    "bullpen": 20,
    "environment": 10,
    "defense": 5,
    "schedule": 5,
}


def baseline_card_payload() -> dict[str, object]:
    return {"component_weights_pp": V51_WEIGHTS}


class ScoringCardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        raw = json.loads(SYNTHETIC.read_text(encoding="utf-8"))
        raw["outputs"]["root"] = self.temporary.name
        baseline_path = Path(self.temporary.name) / "baseline-card.json"
        baseline_bytes = canonical_json_bytes(baseline_card_payload())
        baseline_path.write_bytes(baseline_bytes)
        raw["baseline"]["card"] = {
            "path": str(baseline_path.relative_to(ROOT)),
            "sha256": sha256(baseline_bytes).hexdigest(),
            "component_weights_pp": V51_WEIGHTS,
        }
        self.spec = build_run_spec(raw)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(
        self,
        *,
        lane_id: str = "A",
        model_id: str = "A",
        fold_id: str = "H23",
        candidate_id: str | None = None,
        candidate_payload_sha256: str | None = None,
        parent_card_sha256: str | None = None,
        calibration_card_sha256: str | None = SHA,
        weights: dict[str, int] | None = None,
    ) -> dict[str, object]:
        return {
            "model_id": model_id,
            "lane_id": lane_id,
            "fold_id": fold_id,
            "candidate_id": candidate_id,
            "run_spec_sha256": run_spec_sha256(self.spec),
            "source_gate_sha256": SHA,
            "baseline_replay_sha256": SHA,
            "parent_card_sha256": parent_card_sha256,
            "feature_seal_sha256": SHA,
            "prediction_contract_sha256": self.spec.prediction_contract_sha256,
            "calibration_card_sha256": calibration_card_sha256,
            "candidate_payload_sha256": candidate_payload_sha256,
            "parameters": {"component_weights_pp": weights or V51_WEIGHTS},
        }

    def test_candidate_card_is_the_pre_prediction_weight_authority(self) -> None:
        card = seal_scoring_model_card(self.spec, self.payload())
        self.assertEqual(card.prediction_contract_sha256, self.spec.prediction_contract_sha256)
        self.assertEqual(card.parameters["component_weights_pp"], V51_WEIGHTS)
        self.assertEqual(
            load_scoring_model_card(self.spec, Path(card.path), card.artifact_sha256), card
        )
        authority = resolve_prediction_weight_authority(self.spec, "A", card)
        self.assertIsInstance(authority, PredictionWeightAuthority)
        self.assertEqual(authority.sha256, card.artifact_sha256)
        self.assertEqual(authority.component_weights_pp, V51_WEIGHTS)

    def test_full_fold_domain_and_closed_derived_path_matrix_are_enforced(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertEqual(set(schema["properties"]["fold_id"]["enum"]), FOLD_IDS)
        candidate = self.payload(
            lane_id="B", model_id="A+B", fold_id="O19", candidate_id="grid_01",
            candidate_payload_sha256=SHA,
        )
        self.assertEqual(
            scoring_model_card_path(self.spec, candidate),
            self.spec.output_path("lane_b", "O19/candidates/grid_01/scoring_model_card.json"),
        )
        matrix = (
            (self.payload(lane_id="A", model_id="A", fold_id="O19"), "lane_a/O19/scoring_model_card.json"),
            (candidate, "lane_b/O19/candidates/grid_01/scoring_model_card.json"),
            (self.payload(lane_id="C", model_id="A+B+C", fold_id="H22", candidate_payload_sha256=SHA, parent_card_sha256=SHA), "lane_c/H22/scoring_model_card.json"),
            (self.payload(lane_id="H24", model_id="A", fold_id="H24", parent_card_sha256=SHA), "h24_predictions/A/scoring_model_card.json"),
            (self.payload(lane_id="H24", model_id="A+B", fold_id="H24", candidate_id="grid_01", candidate_payload_sha256=SHA, parent_card_sha256=SHA), "h24_predictions/A+B/scoring_model_card.json"),
            (self.payload(lane_id="H24", model_id="A+B+C", fold_id="H24", candidate_payload_sha256=SHA, parent_card_sha256=SHA), "h24_predictions/A+B+C/scoring_model_card.json"),
        )
        for payload, relative in matrix:
            with self.subTest(relative=relative):
                self.assertEqual(scoring_model_card_path(self.spec, payload), self.spec.output_path(relative.split("/", 1)[0] if relative.startswith("lane_") else "h24_predictions", relative.split("/", 1)[1] if relative.startswith("lane_") else relative.split("/", 1)[1]))
        with self.assertRaisesRegex(ValueError, "safe candidate_id"):
            seal_scoring_model_card(self.spec, self.payload(lane_id="B", model_id="A+B", candidate_id="../escape", candidate_payload_sha256=SHA))
        card = seal_scoring_model_card(self.spec, candidate)
        wrong_path = self.spec.output_path(
            "lane_b", "O20/candidates/grid_01/scoring_model_card.json"
        )
        wrong_path.parent.mkdir(parents=True)
        shutil.copyfile(card.path, wrong_path)
        shutil.copyfile(f"{card.path}.sha256", f"{wrong_path}.sha256")
        with self.assertRaisesRegex(ValueError, "derived path"):
            load_scoring_model_card(
                self.spec,
                wrong_path,
                card.artifact_sha256,
            )

    def test_closed_schema_and_semantic_matrix_reject_invalid_cards(self) -> None:
        schema = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        persisted = {"schema": "MODEL_IMPROVEMENT_SCORING_MODEL_CARD_V1"} | self.payload()
        schema.validate(persisted)
        for invalid in (
            persisted | {"unexpected": True},
            persisted | {"model_id": "v5.1"},
            persisted | {"fold_id": "bad"},
            persisted | {"parameters": {"component_weights_pp": {"starter": 100}}},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValidationError):
                    schema.validate(invalid)
        invalid_payloads = (
            self.payload(weights={"starter": 34, "offense": 25, "bullpen": 20, "environment": 10, "defense": 5, "schedule": 5}),
            self.payload(prediction_contract_sha256=SHA_B) if False else self.payload() | {"prediction_contract_sha256": SHA_B},
            self.payload(lane_id="C", model_id="A+B+C", fold_id="O19", candidate_payload_sha256=SHA, parent_card_sha256=SHA),
            self.payload(lane_id="H24", model_id="A+B", fold_id="H24", parent_card_sha256=SHA),
        )
        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    seal_scoring_model_card(self.spec, invalid)

    def test_sidecar_is_required_and_an_orphan_sidecar_retry_is_safe(self) -> None:
        payload = self.payload()
        path = scoring_model_card_path(self.spec, payload)
        original = scoring_cards.write_immutable_json

        def fail_primary(candidate_path, value):
            if Path(candidate_path) == path:
                raise ValueError("injected primary failure")
            return original(candidate_path, value)

        with patch.object(scoring_cards, "write_immutable_json", fail_primary):
            with self.assertRaisesRegex(ValueError, "injected"):
                seal_scoring_model_card(self.spec, payload)
        self.assertTrue(path.with_suffix(".json.sha256").is_file())
        self.assertFalse(path.exists())
        card = seal_scoring_model_card(self.spec, payload)
        self.assertEqual(load_scoring_model_card(self.spec, Path(card.path), card.artifact_sha256), card)
        path.with_suffix(".json.sha256").unlink()
        with self.assertRaises(ValueError):
            load_scoring_model_card(self.spec, path, card.artifact_sha256)

    def test_changed_card_bindings_or_conflicting_sidecar_fail_closed(self) -> None:
        card = seal_scoring_model_card(self.spec, self.payload())
        changes = (
            {"calibration_card_sha256": SHA_B},
            {"feature_seal_sha256": SHA_B},
            {"parent_card_sha256": SHA_B},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                seal_scoring_model_card(self.spec, self.payload() | change)
        candidate = self.payload(
            lane_id="B", model_id="A+B", fold_id="H22", candidate_id="grid_01",
            candidate_payload_sha256=SHA,
        )
        seal_scoring_model_card(self.spec, candidate)
        with self.assertRaises(ValueError):
            seal_scoring_model_card(
                self.spec, candidate | {"candidate_payload_sha256": SHA_B}
            )
        sidecar = Path(card.path).with_suffix(".json.sha256")
        sidecar.unlink()
        sidecar.write_bytes(canonical_json_bytes({"artifact_sha256": SHA_B}))
        with self.assertRaises(ValueError):
            seal_scoring_model_card(self.spec, self.payload())

    def test_baseline_uses_the_existing_runspec_card_without_a_wrapper(self) -> None:
        authority = resolve_prediction_weight_authority(self.spec, model_id="v5.1", scoring_card=None)
        self.assertEqual(authority.path, self.spec.baseline.card.path)
        self.assertEqual(authority.sha256, self.spec.baseline.card.sha256)
        self.assertEqual(authority.component_weights_pp, self.spec.baseline.card.component_weights_pp)
        with self.assertRaisesRegex(ValueError, "v5.1.*scoring card"):
            seal_scoring_model_card(self.spec, self.payload(model_id="v5.1"))

    def test_resolver_reloads_baseline_and_candidate_authorities_not_descriptors(self) -> None:
        card = seal_scoring_model_card(self.spec, self.payload())
        path = Path(card.path)
        path.write_bytes(canonical_json_bytes({"tampered": True}))
        with self.assertRaises(ValueError):
            resolve_prediction_weight_authority(self.spec, "A", card)
        baseline = ROOT / self.spec.baseline.card.path
        baseline.write_bytes(canonical_json_bytes({"component_weights_pp": V51_WEIGHTS | {"starter": 34}}))
        with self.assertRaises(ValueError):
            resolve_prediction_weight_authority(self.spec, "v5.1", None)
        fabricated = dataclasses.replace(card, path=str(Path(self.temporary.name) / "forged.json"))
        with self.assertRaises(ValueError):
            resolve_prediction_weight_authority(self.spec, "A", fabricated)

    def test_resolver_accepts_only_the_closed_model_card_presence_union(self) -> None:
        card = seal_scoring_model_card(self.spec, self.payload())
        for model_id, descriptor in (
            ("v5.1", card),
            ("A", None),
            ("A+B", card),
            ("unknown", None),
        ):
            with self.subTest(model_id=model_id, descriptor=descriptor is not None):
                with self.assertRaises(ValueError):
                    resolve_prediction_weight_authority(self.spec, model_id, descriptor)

    def test_scoring_card_module_never_imports_outcome_code(self) -> None:
        module = ROOT / "mlb_backtest/model_improvement/scoring_cards.py"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertFalse(
            any("outcome" in name or "evaluation" in name for name in imported)
        )
