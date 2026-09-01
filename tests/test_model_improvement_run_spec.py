import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.lane_disposition import (
    load_lane_disposition,
    seal_lane_disposition,
)
from mlb_backtest.model_improvement.run_spec import (
    build_run_spec,
    load_run_spec,
    load_verified_run_spec,
    run_spec_sha256,
)


SPEC_PATH = Path(__file__).parent / "fixtures/model_improvement/parallel_model_result_v1.synthetic.json"


class RunSpecTest(unittest.TestCase):
    def raw(self):
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    def test_exact_contract_is_available_to_sibling_lanes(self):
        spec = load_run_spec(SPEC_PATH)
        self.assertEqual(spec.fold("H22").normalization_years, (2018, 2019, 2020, 2021))
        self.assertEqual(spec.fold("H24").oof_calibration_years, (2019, 2020, 2021, 2022, 2023))
        self.assertEqual(dict(spec.cutoffs), {"history": "D-1", "snapshot": "T-45"})
        self.assertEqual(spec.lane_b.keep_h22_count, 12)
        self.assertEqual(spec.lane_b.h23_order, ("brier", "log_loss", "daily_top1_delta_desc", "candidate_id"))
        self.assertRegex(spec.prediction_contract_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(str(spec.output_path("features", "lane_c/H24/feature_seal.json")).endswith("features/lane_c/H24/feature_seal.json"))
        self.assertTrue(spec.h24_firewall.forbid_outcomes_before_registry)
        self.assertEqual(run_spec_sha256(spec), sha256(canonical_json_bytes(spec.as_mapping())).hexdigest())

    def test_prediction_contract_digest_and_closed_mutations(self):
        raw = self.raw()
        self.assertEqual(raw["prediction_contract_sha256"], sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest())
        raw["prediction_contract"]["clip"]["min"] = 0.11
        with self.assertRaisesRegex(ValueError, "prediction contract digest"):
            build_run_spec(raw)
        raw = self.raw()
        raw["prediction_contract"]["ranking"]["unknown"] = True
        with self.assertRaisesRegex(ValueError, "prediction contract|unknown"):
            build_run_spec(raw)
        raw = self.raw()
        raw["prediction_contract"]["component_weights_binding"]["sum"] = 99
        raw["prediction_contract_sha256"] = sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest()
        with self.assertRaisesRegex(ValueError, "component weights binding"):
            build_run_spec(raw)

    def test_all_nested_bindings_and_paths_fail_closed(self):
        raw = self.raw()
        raw["source_authority"]["inventory"]["unknown"] = True
        with self.assertRaisesRegex(ValueError, "source authority"):
            build_run_spec(raw)
        raw = self.raw()
        raw["folds"]["H22"]["oof_calibration_years"] = [2018, 2019, 2020]
        with self.assertRaisesRegex(ValueError, "chronology"):
            build_run_spec(raw)
        raw = self.raw()
        raw["lane_b"]["candidate_count"] = 209
        with self.assertRaisesRegex(ValueError, "Lane B"):
            build_run_spec(raw)
        spec = load_run_spec(SPEC_PATH)
        for namespace, relative in (("unknown", "x.json"), ("features", "../x.json"), ("features", "common/H22/nope.json")):
            with self.assertRaises(ValueError):
                spec.output_path(namespace, relative)

    def test_verified_loader_rejects_synthetic_and_wrong_path_or_head(self):
        with self.assertRaisesRegex(ValueError, "production path"):
            load_verified_run_spec(SPEC_PATH)
        raw = self.raw()
        raw["schema_version"] = "PARALLEL_MODEL_RESULT_RUN_SPEC_V1"
        raw["code_revision"] = "0" * 40
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parallel_model_result_v1.json"
            path.write_bytes(canonical_json_bytes(raw))
            with self.assertRaisesRegex(ValueError, "production path"):
                load_verified_run_spec(path)

    def test_lane_disposition_round_trips_in_temporary_synthetic_output_root(self):
        raw = self.raw()
        with tempfile.TemporaryDirectory() as directory:
            raw["outputs"]["root"] = directory
            spec = build_run_spec(raw)
            disposition = seal_lane_disposition(spec, "A", "REJECTED", None, ("a" * 64,), ("H23_GATE",))
            self.assertEqual(load_lane_disposition(spec, Path(disposition.path), disposition.artifact_sha256), disposition)
            with self.assertRaises(ValueError):
                load_lane_disposition(spec, Path(disposition.path), "b" * 64)
