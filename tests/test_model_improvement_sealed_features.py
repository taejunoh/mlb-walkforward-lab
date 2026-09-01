"""TDD coverage for immutable, result-free feature rows."""

from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.contracts import (
    COMPONENTS,
    MetricPrimitive,
    QualityRecipe,
    SealedFeatureRow,
    compute_feature_row_sha256,
)
from mlb_backtest.model_improvement.run_spec import build_run_spec, run_spec_sha256
from mlb_backtest.model_improvement.source_gate import SourceGateReceipt
from mlb_backtest.model_improvement.sealed_features import (
    FeatureSeal,
    load_feature_seal,
    load_sealed_feature_rows,
    seal_feature_rows,
)
import mlb_backtest.model_improvement.sealed_features as sealed_features


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
SCHEMA = ROOT / "schemas/model_improvement/feature_seal.schema.json"
SHA = "a" * 64
INVENTORY = "d" * 64


def component_map(value):
    return {
        side: {component: value for component in COMPONENTS}
        for side in ("away", "home")
    }


def fixed_recipe():
    return QualityRecipe("fixed", "fixed", None, None, None, 0.0, (), (), SHA)


def feature_row(spec, gate, game_pk: int, date: str, start: str) -> SealedFeatureRow:
    primitive = MetricPrimitive(50.0, 1.0, fixed_recipe())
    values = {
        "game_pk": game_pk,
        "official_date": date,
        "scheduled_start_utc": start,
        "target_year": 2022,
        "max_history_date": "2022-03-31",
        "source_gate_sha256": gate.artifact_sha256,
        "source_manifest_sha256": spec.source_authority.inventory.sha256,
        "exclusion_policy_sha256": sha256(canonical_json_bytes(spec.as_mapping()["exclusions"])).hexdigest(),
        "component_scores": component_map(50.0),
        "component_qualities": component_map(0.0),
        "baseline_component_scores": component_map(50.0),
        "baseline_component_qualities": component_map(0.0),
        "metric_primitives": component_map((primitive,)),
        "component_inputs_sha256": component_map(SHA),
        "starter_edge_abs": 0.0,
    }
    return SealedFeatureRow(
        **(values | {"feature_row_sha256": compute_feature_row_sha256(values)})
    )


def changed_row(row: SealedFeatureRow, **changes: object) -> SealedFeatureRow:
    values = row.as_mapping() | changes
    values.pop("feature_row_sha256")
    payload = values | {"feature_row_sha256": compute_feature_row_sha256(values)}
    return SealedFeatureRow.from_payload_mapping(payload)


class SealedFeatureTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        raw = json.loads(SYNTHETIC.read_text(encoding="utf-8"))
        raw["outputs"]["root"] = self.temporary.name
        self.spec = build_run_spec(raw)
        digest = run_spec_sha256(self.spec)
        self.gate = SourceGateReceipt(
            digest, SHA, SHA, SHA, INVENTORY, (SHA,), SHA,
            tuple(range(2018, 2025)), "SOURCES_READY", "b" * 64,
            str(Path(self.temporary.name) / "gate.json"),
        )
        self.rows = (
            feature_row(self.spec, self.gate, 100, "2022-04-01", "2022-04-01T17:00:00Z"),
            feature_row(self.spec, self.gate, 101, "2022-04-02", "2022-04-02T17:00:00Z"),
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_schema_and_feature_seal_loader_are_closed(self):
        payload = {
            "schema": "MODEL_IMPROVEMENT_FEATURE_SEAL_V1", "feature_set_id": "common",
            "fold_id": "H22", "run_spec_sha256": SHA, "source_gate_sha256": SHA,
            "population_sha256": SHA, "rows_sha256": SHA,
            "parent_feature_seal_sha256": None, "evidence_sha256": [],
        }
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
        seal = FeatureSeal.from_payload_mapping(payload, SHA, "features/common/H22/feature_seal.json")
        self.assertEqual(seal.as_mapping(), payload)
        with self.assertRaises(ValueError):
            FeatureSeal.from_payload_mapping(payload | {"extra": True}, SHA, "x")

    def test_seal_rereads_ordered_result_free_rows_and_exact_jsonl(self):
        seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        self.assertEqual(seal.schema, "MODEL_IMPROVEMENT_FEATURE_SEAL_V1")
        self.assertNotIn("artifact_sha256", seal.as_mapping())
        self.assertNotIn("path", seal.as_mapping())
        expected = b"".join(canonical_json_bytes(row.as_mapping()) for row in self.rows)
        rows_path = Path(seal.path).with_name("rows.jsonl")
        self.assertEqual(rows_path.read_bytes(), expected)
        self.assertEqual(load_feature_seal(Path(seal.path), seal.artifact_sha256), seal)
        self.assertEqual(
            load_sealed_feature_rows(self.spec, self.gate, (2022,), seal), self.rows
        )

    def test_rejects_wrong_gate_history_order_and_requested_year(self):
        wrong_gate = SourceGateReceipt(
            "c" * 64, SHA, SHA, SHA, INVENTORY, (SHA,), SHA,
            tuple(range(2018, 2025)), "SOURCES_READY", "b" * 64, "gate.json",
        )
        with self.assertRaisesRegex(ValueError, "RunSpec"):
            seal_feature_rows(self.spec, wrong_gate, "common", "H22", self.rows)
        reversed_rows = tuple(reversed(self.rows))
        with self.assertRaisesRegex(ValueError, "order"):
            seal_feature_rows(self.spec, self.gate, "common", "H22", reversed_rows)
        seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        with self.assertRaisesRegex(ValueError, "years"):
            load_sealed_feature_rows(self.spec, self.gate, (2023,), seal)

    def test_lane_c_requires_common_parent_and_evidence(self):
        common = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        lane_c = seal_feature_rows(
            self.spec, self.gate, "lane_c", "H22", self.rows,
            common.artifact_sha256, ("c" * 64,),
        )
        self.assertEqual(lane_c.parent_feature_seal_sha256, common.artifact_sha256)
        with self.assertRaisesRegex(ValueError, "parent feature seal|evidence"):
            seal_feature_rows(self.spec, self.gate, "lane_c", "H22", self.rows)

    def test_missing_or_different_sidecar_rejects_the_artifact(self):
        seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        sidecar = Path(seal.path).with_suffix(".json.sha256")
        sidecar.unlink()
        with self.assertRaises(ValueError):
            load_feature_seal(Path(seal.path), seal.artifact_sha256)

    def test_rows_sidecar_and_source_inventory_binding_fail_closed(self):
        seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        rows_path = Path(seal.path).with_name("rows.jsonl")
        rows_sidecar = rows_path.with_suffix(".jsonl.sha256")
        rows_sidecar.unlink()
        with self.assertRaises(ValueError):
            load_sealed_feature_rows(self.spec, self.gate, (2022,), seal)
        bad_rows = (changed_row(self.rows[0], source_manifest_sha256=SHA), self.rows[1])
        with self.assertRaisesRegex(ValueError, "source inventory"):
            seal_feature_rows(self.spec, self.gate, "common", "H22", bad_rows)

    def test_sidecar_prerequisites_resume_after_rows_primary_failure(self):
        original = sealed_features.write_immutable_bytes

        def fail_rows(path, contents):
            if Path(path).name == "rows.jsonl":
                raise ValueError("injected rows primary failure")
            return original(path, contents)

        with patch.object(sealed_features, "write_immutable_bytes", fail_rows):
            with self.assertRaisesRegex(ValueError, "injected"):
                seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        directory = self.spec.output_path("features", "common/H22/feature_seal.json").parent
        self.assertTrue((directory / "rows.jsonl.sha256").is_file())
        self.assertTrue((directory / "feature_seal.json.sha256").is_file())
        self.assertFalse((directory / "rows.jsonl").exists())
        self.assertFalse((directory / "feature_seal.json").exists())
        seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        self.assertTrue(Path(seal.path).is_file())

    def test_nonregular_sidecar_is_never_repaired(self):
        seal = seal_feature_rows(self.spec, self.gate, "common", "H22", self.rows)
        sidecar = Path(seal.path).with_suffix(".json.sha256")
        target = sidecar.with_name("sidecar-target")
        target.write_text("target", encoding="utf-8")
        sidecar.unlink()
        sidecar.symlink_to(target)
        with self.assertRaises(ValueError):
            load_feature_seal(Path(seal.path), seal.artifact_sha256)
        self.assertTrue(sidecar.is_symlink())


if __name__ == "__main__":
    unittest.main()
