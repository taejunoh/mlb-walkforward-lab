"""V2 calibrated baseline replay receipts stay label-free and fail closed."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mlb_backtest.model_improvement.canonical import canonical_json_bytes, write_immutable_json
from mlb_backtest.model_improvement.contracts import (
    MetricPrimitive,
    ProbabilityCalibrationCardV2,
    QualityRecipe,
    SealedFeatureRow,
    compute_feature_row_sha256,
)
from mlb_backtest.model_improvement.oof_ledger_v2 import prediction_paths_v2
from mlb_backtest.model_improvement.run_spec import build_run_spec, run_spec_sha256
from mlb_backtest.model_improvement.sealed_features import seal_feature_rows
from mlb_backtest.model_improvement.source_gate import SourceGateReceipt


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
SHA = "a" * 64
WEIGHTS = {
    "starter": 35,
    "offense": 25,
    "bullpen": 20,
    "environment": 10,
    "defense": 5,
    "schedule": 5,
}
COMPONENTS = tuple(WEIGHTS)


def _component_map(value: float) -> dict[str, dict[str, float]]:
    return {side: {component: value for component in COMPONENTS} for side in ("away", "home")}


def _feature_row(spec, gate, game_pk: int, official_date: str, scheduled_start_utc: str):
    recipe = QualityRecipe("fixed", "fixed", None, None, None, 0.0, (), (), SHA)
    primitive = MetricPrimitive(50.0, 1.0, recipe)
    values = {
        "game_pk": game_pk,
        "official_date": official_date,
        "scheduled_start_utc": scheduled_start_utc,
        "target_year": date.fromisoformat(official_date).year,
        "max_history_date": (date.fromisoformat(official_date) - timedelta(days=1)).isoformat(),
        "source_gate_sha256": gate.artifact_sha256,
        "source_manifest_sha256": spec.source_authority.inventory.sha256,
        "exclusion_policy_sha256": sha256(canonical_json_bytes(spec.as_mapping()["exclusions"])).hexdigest(),
        "component_scores": _component_map(50.0),
        "component_qualities": _component_map(0.0),
        "baseline_component_scores": _component_map(50.0),
        "baseline_component_qualities": _component_map(0.0),
        "metric_primitives": _component_map((primitive,)),
        "component_inputs_sha256": _component_map(SHA),
        "starter_edge_abs": 0.0,
    }
    return SealedFeatureRow(**(values | {"feature_row_sha256": compute_feature_row_sha256(values)}))


class BaselineReplayV2Test(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        raw = json.loads(SYNTHETIC.read_text(encoding="utf-8"))
        raw["outputs"]["root"] = self.temporary.name
        card_path = Path(self.temporary.name) / "v51_card.json"
        card_bytes = canonical_json_bytes({"component_weights_pp": WEIGHTS})
        card_path.write_bytes(card_bytes)
        raw["baseline"]["card"] = {
            "path": str(card_path.relative_to(ROOT)),
            "sha256": sha256(card_bytes).hexdigest(),
            "component_weights_pp": WEIGHTS,
        }
        self.spec = build_run_spec(raw)
        gate_payload = {
            "run_spec_sha256": run_spec_sha256(self.spec), "source_run_spec_sha256": SHA,
            "acquisition_contract_sha256": SHA, "checkpoint_sha256": SHA,
            "inventory_sha256": self.spec.source_authority.inventory.sha256,
            "validation_receipts_sha256": [SHA], "snapshot_replay_sha256": SHA,
            "required_years": list(range(2018, 2025)), "status": "SOURCES_READY",
        }
        gate_path = self.spec.output_path("source_gate", "source_gate_receipt.json")
        gate_digest = sha256(canonical_json_bytes(gate_payload)).hexdigest()
        write_immutable_json(gate_path.with_suffix(".json.sha256"), {"artifact_sha256": gate_digest})
        write_immutable_json(gate_path, gate_payload)
        self.gate = SourceGateReceipt.from_payload_mapping(gate_payload, gate_digest, str(gate_path))
        self.features = (
            _feature_row(self.spec, self.gate, 100, "2022-04-01", "2022-04-01T17:00:00Z"),
            _feature_row(self.spec, self.gate, 101, "2022-04-02", "2022-04-02T17:00:00Z"),
        )
        self.feature_seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.features)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _calibration_card(self, alpha: float = 0.0, beta: float = 1.0):
        pool = self.spec.probability_calibration_policy.pools["C22H"]
        return ProbabilityCalibrationCardV2(
            "v5.1", "baseline", "H22", "C22H", pool.target_year,
            pool.training_start, pool.training_end, run_spec_sha256(self.spec),
            self.gate.artifact_sha256, self.spec.prediction_contract_sha256,
            "/calibration/receipt.json", SHA, "/weights/card.json", self.spec.baseline.card.sha256,
            "/calibration/rows.jsonl", SHA, SHA, "inventory.json",
            self.spec.source_authority.inventory.sha256, {"algorithm": "fit_logistic_v1"},
            alpha, beta, SHA, "/calibration/probability_calibration_card.json",
        )

    def test_replays_calibrated_rows_twice_then_publishes_one_matching_seal_and_receipt(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card(alpha=0.3, beta=0.7)
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
            "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
        ):
            receipt = baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
            loaded = baseline_replay_v2.load_baseline_replay_v2(
                self.spec, self.gate, self.feature_seal, receipt.path, receipt.artifact_sha256
            )
        self.assertEqual(loaded, receipt)
        self.assertEqual(receipt.replay_one_rows_sha256, receipt.replay_two_rows_sha256)
        self.assertEqual(receipt.replay_one_rows_sha256, receipt.prediction_rows_sha256)
        self.assertEqual(receipt.calibration_card_path, card.path)
        self.assertIn("/oof/baseline/v5.1/H22/v2/", receipt.path)

    def test_second_materialization_drift_fails_before_prediction_or_receipt_primary(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch.object(
            baseline_replay_v2, "_materialize_v51_rows", side_effect=((), ())
        ), patch.object(baseline_replay_v2, "seal_oof_predictions_v2", side_effect=AssertionError("seal")), self.assertRaisesRegex(ValueError, "independent replay"):
            baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
        paths = prediction_paths_v2(self.spec, "baseline/v5.1/H22")
        self.assertFalse(paths.seal.exists())

    def test_missing_calibration_authority_has_exact_error_and_zero_output(self):
        from mlb_backtest.model_improvement import baseline_replay_v2
        from mlb_backtest.model_improvement.probability_calibration import CalibrationAuthorityBlockedError

        with patch.object(
            baseline_replay_v2, "require_probability_calibration_card_v2",
            side_effect=CalibrationAuthorityBlockedError(),
        ), self.assertRaisesRegex(RuntimeError, "^BLOCKED_CALIBRATION_AUTHORITY_MISSING$"):
            baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
        self.assertFalse(prediction_paths_v2(self.spec, "baseline/v5.1/H22").seal.exists())

    def test_materialization_uses_direct_weight_authority_and_contract_rank_key(self):
        from mlb_backtest.model_improvement import baseline_replay_v2
        import mlb_backtest.scoring as scoring

        def runtime_feature(game_pk: int, start: str, starter_delta: float):
            home = {component: 50.0 for component in COMPONENTS}
            away = {component: 50.0 for component in COMPONENTS}
            home["starter"] += starter_delta
            return SimpleNamespace(
                game_pk=game_pk, official_date="2022-04-01", target_year=2022,
                scheduled_start_utc=start, starter_edge_abs=abs(starter_delta),
                feature_row_sha256=f"{game_pk:064x}",
                component_scores={"home": home, "away": away},
                component_qualities={
                    "home": {component: 1.0 for component in COMPONENTS},
                    "away": {component: 1.0 for component in COMPONENTS},
                },
            )

        rows = baseline_replay_v2._materialize_v51_rows(
            self.spec,
            (
                runtime_feature(101, "2022-04-01T18:00:00Z", 10.0),
                runtime_feature(100, "2022-04-01T17:00:00Z", 10.0),
                runtime_feature(102, "2022-04-01T19:00:00Z", 0.0),
            ),
            self._calibration_card(),
            baseline_replay_v2.resolve_prediction_weight_authority(self.spec, "v5.1", None),
        )
        with patch.object(scoring, "WEIGHTS", {component: 0.0 for component in COMPONENTS}):
            changed_globals = baseline_replay_v2._materialize_v51_rows(
                self.spec,
                (
                    runtime_feature(101, "2022-04-01T18:00:00Z", 10.0),
                    runtime_feature(100, "2022-04-01T17:00:00Z", 10.0),
                    runtime_feature(102, "2022-04-01T19:00:00Z", 0.0),
                ),
                self._calibration_card(),
                baseline_replay_v2.resolve_prediction_weight_authority(self.spec, "v5.1", None),
            )
        self.assertEqual(rows, changed_globals)
        self.assertEqual([row.rank for row in rows], [2, 1, 0])
        self.assertEqual([row.picked_side for row in rows], ["home", "home", "none"])

    def test_h24_feature_rejects_before_any_artifact_read(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        h24 = replace(self.feature_seal, fold_id="H24")
        with patch.object(
            baseline_replay_v2, "read_regular_bytes", side_effect=AssertionError("read")
        ), self.assertRaises(ValueError):
            baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, h24)

    def test_loader_rejects_forged_spec_before_path_calculation(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        class ForgedSpec:
            def output_path(self, *_args):
                raise AssertionError("output path")

        with self.assertRaises(ValueError):
            baseline_replay_v2.load_baseline_replay_v2(
                ForgedSpec(), self.gate, self.feature_seal, "/not/read.json", SHA
            )

    def test_loader_rejects_receipt_sidecar_tamper(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
            "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
        ):
            receipt = baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
            Path(receipt.path).with_suffix(".json.sha256").write_bytes(
                canonical_json_bytes({"artifact_sha256": SHA})
            )
            with self.assertRaises(ValueError):
                baseline_replay_v2.load_baseline_replay_v2(
                    self.spec, self.gate, self.feature_seal, receipt.path, receipt.artifact_sha256
                )

    def test_module_never_imports_outcomes_or_evaluation(self):
        path = ROOT / "mlb_backtest/model_improvement/baseline_replay_v2.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        names += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any("outcome" in name or "evaluation" in name for name in names))
