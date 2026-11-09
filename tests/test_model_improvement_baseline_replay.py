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

    def _calibration_card(self, alpha: float = 0.0, beta: float = 1.0, fold_id="H22"):
        key = self.spec.probability_calibration_policy.fold_keys[fold_id]
        pool = self.spec.probability_calibration_policy.pools[key]
        return ProbabilityCalibrationCardV2(
            "v5.1", "baseline", fold_id, key, pool.target_year,
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
        replay_one = baseline_replay_v2._materialize_v51_rows(
            self.spec,
            self.features,
            card,
            baseline_replay_v2.resolve_prediction_weight_authority(self.spec, "v5.1", None),
        )
        self.assertGreater(len(replay_one), 1)
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch.object(
            baseline_replay_v2, "_materialize_v51_rows", side_effect=(replay_one, replay_one[:-1])
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

        def runtime_feature(game_pk: int, start: str, starter_delta: float, official_date="2022-04-01"):
            home = {component: 50.0 for component in COMPONENTS}
            away = {component: 50.0 for component in COMPONENTS}
            home["starter"] += starter_delta
            return SimpleNamespace(
                game_pk=game_pk, official_date=official_date, target_year=2022,
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

        multi_date = baseline_replay_v2._materialize_v51_rows(
            self.spec,
            (
                runtime_feature(100, "2022-04-01T17:00:00Z", 10.0),
                runtime_feature(101, "2022-04-01T18:00:00Z", 10.0),
                runtime_feature(102, "2022-04-02T17:00:00Z", 10.0, "2022-04-02"),
                runtime_feature(103, "2022-04-02T18:00:00Z", 0.0, "2022-04-02"),
            ),
            self._calibration_card(),
            baseline_replay_v2.resolve_prediction_weight_authority(self.spec, "v5.1", None),
        )
        self.assertEqual([row.rank for row in multi_date], [1, 2, 1, 0])

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

    def test_prediction_mutation_after_seal_fails_before_receipt_primary(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        original = baseline_replay_v2.seal_oof_predictions_v2

        def seal_then_mutate(*args):
            seal = original(*args)
            prediction_paths_v2(self.spec, seal.ledger_id).rows.write_bytes(b"tampered\n")
            return seal

        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
            "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
        ), patch.object(baseline_replay_v2, "seal_oof_predictions_v2", side_effect=seal_then_mutate), self.assertRaises(ValueError):
            baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
        receipt_path = self.spec.output_path("oof", "baseline/v5.1/H22/v2/baseline_replay_receipt.json")
        self.assertFalse(receipt_path.exists())

    def test_receipt_crash_leaves_only_safe_retry_prerequisites(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        receipt_path = self.spec.output_path("oof", "baseline/v5.1/H22/v2/baseline_replay_receipt.json")
        for crash_at in (1, 2):
            receipt_path.unlink(missing_ok=True)
            receipt_path.with_suffix(".json.sha256").unlink(missing_ok=True)
            calls = 0
            original = baseline_replay_v2.write_immutable_bytes

            def crash_once(path, contents):
                nonlocal calls
                calls += 1
                if calls == crash_at:
                    raise ValueError("receipt crash")
                return original(path, contents)

            with self.subTest(crash_at=crash_at), patch.object(
                baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card
            ), patch(
                "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
            ), patch.object(baseline_replay_v2, "write_immutable_bytes", side_effect=crash_once), self.assertRaisesRegex(ValueError, "receipt crash"):
                baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
            self.assertFalse(receipt_path.exists())
            with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
                "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
            ):
                receipt = baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
            self.assertTrue(Path(receipt.path).is_file())

    def test_unhashable_feature_fold_is_value_error_before_artifact_read(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        object.__setattr__(self.feature_seal, "fold_id", [])
        with patch.object(
            baseline_replay_v2, "read_regular_bytes", side_effect=AssertionError("read")
        ), self.assertRaises(ValueError):
            baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)

    def test_stale_gate_feature_or_calibration_binding_rejects_before_write(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        stale_gate = replace(self.gate, artifact_sha256=SHA)
        stale_feature = replace(self.feature_seal, artifact_sha256=SHA)
        stale_card = self._calibration_card()
        object.__setattr__(stale_card, "prediction_contract_sha256", SHA)
        for gate, feature, calibration in (
            (stale_gate, self.feature_seal, card),
            (self.gate, stale_feature, card),
            (self.gate, self.feature_seal, stale_card),
        ):
            with self.subTest(kind=(gate is stale_gate, feature is stale_feature, calibration is stale_card)), patch.object(
                baseline_replay_v2, "require_probability_calibration_card_v2", return_value=calibration
            ), patch.object(
                baseline_replay_v2, "write_immutable_bytes", side_effect=AssertionError("write")
            ), self.assertRaises(ValueError):
                baseline_replay_v2.replay_v51_predictions_v2(self.spec, gate, feature)

    def test_o19_common_feature_happy_path_uses_o19_calibration_and_namespace(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        features = (
            _feature_row(self.spec, self.gate, 190, "2019-04-01", "2019-04-01T17:00:00Z"),
            _feature_row(self.spec, self.gate, 191, "2019-04-02", "2019-04-02T17:00:00Z"),
        )
        o19_seal = seal_feature_rows(self.spec, self.gate, "common", "O19", features)
        card = self._calibration_card(fold_id="O19")
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
            "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
        ):
            receipt = baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, o19_seal)
        self.assertEqual(receipt.fold_id, "O19")
        self.assertIn("/oof/baseline/v5.1/O19/v2/", receipt.path)

    def test_loader_rejects_absolute_alias_before_receipt_read(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
            "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
        ):
            receipt = baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
        alias = f"{Path(receipt.path).parent}/./baseline_replay_receipt.json"
        with patch.object(
            baseline_replay_v2, "read_regular_bytes", side_effect=AssertionError("read")
        ), self.assertRaises(ValueError):
            baseline_replay_v2.load_baseline_replay_v2(
                self.spec, self.gate, self.feature_seal, alias, receipt.artifact_sha256
            )

    def test_immutable_receipt_conflict_and_missing_prediction_row_are_rejected(self):
        from mlb_backtest.model_improvement import baseline_replay_v2

        card = self._calibration_card()
        with patch.object(baseline_replay_v2, "require_probability_calibration_card_v2", return_value=card), patch(
            "mlb_backtest.model_improvement.oof_ledger_v2.require_probability_calibration_card_v2", return_value=card
        ):
            receipt = baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)
            rows_path = prediction_paths_v2(self.spec, receipt.ledger_id).rows
            rows_bytes = rows_path.read_bytes()
            rows_path.unlink()
            with self.assertRaises(ValueError):
                baseline_replay_v2.load_baseline_replay_v2(
                    self.spec, self.gate, self.feature_seal, receipt.path, receipt.artifact_sha256
                )
            rows_path.write_bytes(rows_bytes)
            Path(receipt.path).write_bytes(b"different receipt\n")
            with self.assertRaises(ValueError):
                baseline_replay_v2.replay_v51_predictions_v2(self.spec, self.gate, self.feature_seal)

    def test_module_never_imports_outcomes_or_evaluation(self):
        path = ROOT / "mlb_backtest/model_improvement/baseline_replay_v2.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        names += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any("outcome" in name or "evaluation" in name for name in names))
