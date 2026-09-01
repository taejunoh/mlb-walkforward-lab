"""Documentation contracts for the staged OOF prediction/evaluation boundary."""

from __future__ import annotations

import ast
import inspect
import json
import tempfile
import unittest
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mlb_backtest.model_improvement.canonical import canonical_json_bytes, write_immutable_json
from mlb_backtest.model_improvement.contracts import (
    COMPONENTS,
    MetricPrimitive,
    PredictionRow,
    QualityRecipe,
    SealedFeatureRow,
    compute_feature_row_sha256,
)
from mlb_backtest.model_improvement.run_spec import build_run_spec, run_spec_sha256
from mlb_backtest.model_improvement.sealed_features import seal_feature_rows
from mlb_backtest.model_improvement.source_gate import SourceGateReceipt


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
SCHEMA = ROOT / "schemas/model_improvement/prediction_seal.schema.json"
SHA = "a" * 64
WEIGHTS = {
    "starter": 35,
    "offense": 25,
    "bullpen": 20,
    "environment": 10,
    "defense": 5,
    "schedule": 5,
}
PLAN_PATHS = (
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-common-lane-a.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-lane-a.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-lane-b.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-lane-c.md",
    ROOT / "docs/superpowers/plans/2026-08-31-model-improvement-h24-integration.md",
)


class OofLedgerDocumentationContractTest(unittest.TestCase):
    """Keep consumer plans on the receipt-bound, label-free Task8 contract."""

    STAGES = (
        "Task8A Common OOF FeatureSeal admission + baseline label-free seals/loaders",
        "Task6A baseline replay receipt after its matching baseline seal",
        "Task8B nonbaseline label-free seals/loaders after same-fold replay receipt",
        "Task6B SourceInventory → GameEvidence outcomes, reports, and final pair",
    )
    RECEIPT_BOUND_APIS = (
        "seal_oof_predictions(spec, source_gate, feature_seal, baseline_replay",
        "load_prediction_seal(spec, source_gate, feature_seal, baseline_replay",
        "load_prediction_rows(spec, source_gate, feature_seal, baseline_replay",
        "evaluate_predictions(spec, candidate, baseline, outcome_receipt)",
        "load_evaluation_pair(spec, path, expected_sha256)",
    )
    DOC_API_REQUIREMENTS = {
        PLAN_PATHS[0]: RECEIPT_BOUND_APIS
        + (
            "OutcomeReceipt",
            "seal_outcome_receipt",
            "load_outcome_receipt",
            "load_outcome_rows",
            "load_evaluation_report",
        ),
        PLAN_PATHS[1]: RECEIPT_BOUND_APIS
        + ("OutcomeReceipt", "load_outcome_receipt", "load_outcome_rows"),
        PLAN_PATHS[2]: RECEIPT_BOUND_APIS
        + ("OutcomeReceipt", "load_outcome_receipt", "load_outcome_rows"),
        PLAN_PATHS[3]: RECEIPT_BOUND_APIS
        + ("OutcomeReceipt", "load_outcome_receipt", "load_outcome_rows"),
        PLAN_PATHS[4]: RECEIPT_BOUND_APIS
        + (
            "OutcomeReceipt",
            "seal_outcome_receipt",
            "load_outcome_receipt",
            "load_outcome_rows",
            "open_h24_outcomes_once",
            "OutcomeCapability",
        ),
    }
    H24_CANONICAL_OUTCOME_SEAL_CALL = (
        "seal_outcome_receipt(spec, source_gate, common_feature_seal, baseline_replay, baseline_prediction_seal)"
    )
    H24_OPEN_ONCE_REQUIREMENTS = (
        "authorize_h24_outcome_open",
        "capability.consume_once()",
        "direct controller invocation without capability raises OutcomeCapabilityRequired",
        "second wrapper call raises SAME_REGISTRY_RETRY_FORBIDDEN",
        "ledger remains OUTCOMES_OPENED",
        "capability is not a Common API argument",
        "CLI/controller calls only open_h24_outcomes_once",
    )

    def test_consumer_plans_name_only_the_receipt_bound_task8_task6_interfaces(self):
        texts = [path.read_text(encoding="utf-8") for path in PLAN_PATHS]
        forbidden = (
            "join_oof_outcomes(",
            "JoinedLedger",
            "load_joined_ledger(",
            "load_joined_prediction_rows(",
            "outcomes: Mapping",
            "labels:",
            "seal_oof_predictions(spec, ledger_id, rows)",
            "load_prediction_rows(spec, seal)",
            "evaluate_predictions(... outcomes)",
            "evaluate_predictions(spec, source_gate, candidate, baseline, outcomes)",
            "seal_h24_outcome_receipt",
            "H24OutcomeRow",
            "EvaluatedPredictionRow",
            "outcome_rows: Sequence",
            "Sequence[H24OutcomeRow]",
            "Sequence[EvaluatedPredictionRow]",
            "source_inventory, game_evidence_inventory",
            "source_inventory: SourceInventory",
            "game_evidence_inventory: GameEvidenceInventory",
        )
        for path, text in zip(PLAN_PATHS, texts):
            self.assertFalse(any(token in text for token in forbidden), path)
            self.assertNotRegex(text, r"\bsource_inventory\s*(?::|=|,|\))", path)
            self.assertNotRegex(text, r"\bgame_evidence_inventory\s*(?::|=|,|\))", path)

    def test_consumer_plans_preserve_the_staged_order_and_receipt_bindings(self):
        for path in PLAN_PATHS:
            text = " ".join(path.read_text(encoding="utf-8").split())
            positions = [text.index(stage) for stage in self.STAGES]
            self.assertEqual(positions, sorted(positions), path)
            for api in self.DOC_API_REQUIREMENTS[path]:
                self.assertIn(api, text, path)

        for path in PLAN_PATHS[1:]:
            text = path.read_text(encoding="utf-8")
            self.assertIn("outcome_receipt_path", text, path)
            self.assertIn("outcome_receipt_sha256", text, path)
            self.assertRegex(text, r"(?:never[^\n]*outcomes_sha256|outcomes_sha256[^\n]*never)", path)

    def test_h24_uses_the_canonical_common_outcome_sealer_call(self):
        text = " ".join(PLAN_PATHS[-1].read_text(encoding="utf-8").split()).replace("`", "")
        self.assertIn(self.H24_CANONICAL_OUTCOME_SEAL_CALL, text)
        self.assertLess(text.index("capability.consume_once()"), text.index(self.H24_CANONICAL_OUTCOME_SEAL_CALL))
        self.assertIn(
            "capability.consume_once() outcome_receipt = " + self.H24_CANONICAL_OUTCOME_SEAL_CALL,
            text,
        )
        self.assertNotRegex(text, r"seal_outcome_receipt\([^)]*capability")
        for requirement in self.H24_OPEN_ONCE_REQUIREMENTS:
            self.assertIn(requirement, text)

    def test_h24_cli_has_no_outcome_file_or_fixture_input(self):
        text = PLAN_PATHS[-1].read_text(encoding="utf-8")
        task7 = text[text.index("### Task 7:") : text.index("### Task 8:")]
        for obsolete in (
            "outcome file",
            "outcome_file",
            "outcome fixture",
            "outcome_fixture",
            "tests/fixtures/",
            "--outcome-file",
            "--outcomes-path",
        ):
            self.assertNotIn(obsolete, task7)
        self.assertIn("Outcomes derive internally from the verified SourceInventory → GameEvidence", task7)
        self.assertIn("open_h24_outcomes_once", task7)


def _component_map(value):
    return {
        side: {component: value for component in COMPONENTS}
        for side in ("away", "home")
    }


def _feature_row(spec, gate, game_pk, official_date, scheduled_start_utc):
    recipe = QualityRecipe("fixed", "fixed", None, None, None, 0.0, (), (), SHA)
    primitive = MetricPrimitive(50.0, 1.0, recipe)
    values = {
        "game_pk": game_pk,
        "official_date": official_date,
        "scheduled_start_utc": scheduled_start_utc,
        "target_year": date.fromisoformat(official_date).year,
        "max_history_date": (
            date.fromisoformat(official_date) - timedelta(days=1)
        ).isoformat(),
        "source_gate_sha256": gate.artifact_sha256,
        "source_manifest_sha256": spec.source_authority.inventory.sha256,
        "exclusion_policy_sha256": sha256(
            canonical_json_bytes(spec.as_mapping()["exclusions"])
        ).hexdigest(),
        "component_scores": _component_map(50.0),
        "component_qualities": _component_map(0.0),
        "baseline_component_scores": _component_map(50.0),
        "baseline_component_qualities": _component_map(0.0),
        "metric_primitives": _component_map((primitive,)),
        "component_inputs_sha256": _component_map(SHA),
        "starter_edge_abs": 0.0,
    }
    return SealedFeatureRow(
        **(values | {"feature_row_sha256": compute_feature_row_sha256(values)})
    )


class BaselineOofLedgerTest(unittest.TestCase):
    """Task8A seals only the direct-card, label-free v5.1 population."""

    def setUp(self):
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
            "run_spec_sha256": run_spec_sha256(self.spec),
            "source_run_spec_sha256": SHA,
            "acquisition_contract_sha256": SHA,
            "checkpoint_sha256": SHA,
            "inventory_sha256": self.spec.source_authority.inventory.sha256,
            "validation_receipts_sha256": [SHA],
            "snapshot_replay_sha256": SHA,
            "required_years": list(range(2018, 2025)),
            "status": "SOURCES_READY",
        }
        gate_path = self.spec.output_path("source_gate", "source_gate_receipt.json")
        gate_digest = sha256(canonical_json_bytes(gate_payload)).hexdigest()
        write_immutable_json(gate_path.with_suffix(".json.sha256"), {"artifact_sha256": gate_digest})
        write_immutable_json(gate_path, gate_payload)
        self.gate = SourceGateReceipt.from_payload_mapping(
            gate_payload, gate_digest, str(gate_path)
        )
        features = (
            _feature_row(self.spec, self.gate, 100, "2022-04-01", "2022-04-01T17:00:00Z"),
            _feature_row(self.spec, self.gate, 101, "2022-04-02", "2022-04-02T17:00:00Z"),
        )
        self.feature_seal = seal_feature_rows(
            self.spec, self.gate, "common", "H22", features
        )
        card_digest = self.spec.baseline.card.sha256
        self.rows = tuple(
            PredictionRow(
                "v5.1", row.game_pk, row.official_date, row.target_year,
                0.6, "home", 0.6, 0.0, row.starter_edge_abs,
                row.scheduled_start_utc, index, row.feature_row_sha256,
                self.spec.prediction_contract_sha256, card_digest,
            )
            for index, row in enumerate(features, start=1)
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_seals_closed_label_free_baseline_rows_and_reloads_them(self):
        from mlb_backtest.model_improvement.oof_ledger import (
            load_prediction_rows,
            load_prediction_seal,
            prediction_paths,
            seal_oof_predictions,
        )

        seal = seal_oof_predictions(
            self.spec, self.gate, self.feature_seal, None, "baseline/v5.1/H22", self.rows
        )
        rows_path, rows_sidecar, seal_path, seal_sidecar = prediction_paths(
            self.spec, "baseline/v5.1/H22"
        )
        self.assertEqual(rows_path.name, "prediction_rows.jsonl")
        self.assertEqual(rows_sidecar.name, "prediction_rows.jsonl.sha256")
        self.assertEqual(seal_path.name, "prediction_seal.json")
        self.assertEqual(seal_sidecar.name, "prediction_seal.json.sha256")
        self.assertEqual(
            load_prediction_seal(
                self.spec, self.gate, self.feature_seal, None, seal_path, seal.artifact_sha256
            ),
            seal,
        )
        self.assertEqual(
            load_prediction_rows(self.spec, self.gate, self.feature_seal, None, seal),
            self.rows,
        )
        self.assertNotIn("label", rows_path.read_text(encoding="utf-8"))
        self.assertNotIn("outcome", rows_path.read_text(encoding="utf-8"))

    def test_rejects_nonbaseline_h24_and_baseline_replay_before_filesystem_access(self):
        from mlb_backtest.model_improvement import oof_ledger

        attempts = (
            ("baseline/v5.1/H24", None),
            ("lane_a/A/H22", None),
            ("baseline/v5.1/H22", object()),
        )
        for ledger_id, replay in attempts:
            with self.subTest(ledger_id=ledger_id), patch.object(
                oof_ledger, "read_regular_bytes", side_effect=AssertionError("read")
            ):
                with self.assertRaises(ValueError):
                    oof_ledger.seal_oof_predictions(
                        self.spec, self.gate, self.feature_seal, replay, ledger_id, self.rows
                    )

    def test_rejects_unsorted_duplicate_and_empty_populations(self):
        from mlb_backtest.model_improvement.oof_ledger import seal_oof_predictions

        for rows in ((), tuple(reversed(self.rows)), (self.rows[0], self.rows[0])):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                seal_oof_predictions(
                    self.spec, self.gate, self.feature_seal, None, "baseline/v5.1/H22", rows
                )

    def test_each_commit_crash_leaves_only_a_safe_exact_retry(self):
        from mlb_backtest.model_improvement import oof_ledger

        for crash_at in range(1, 5):
            with self.subTest(crash_at=crash_at):
                directory = self.spec.output_path(
                    "oof", "baseline/v5.1/H22/prediction_seal.json"
                ).parent
                if directory.exists():
                    for path in directory.iterdir():
                        path.unlink()
                original = oof_ledger.write_immutable_bytes
                calls = 0

                def crash_once(path, contents):
                    nonlocal calls
                    calls += 1
                    if calls == crash_at:
                        raise ValueError("injected crash")
                    return original(path, contents)

                with patch.object(oof_ledger, "write_immutable_bytes", crash_once):
                    with self.assertRaisesRegex(ValueError, "injected crash"):
                        oof_ledger.seal_oof_predictions(
                            self.spec, self.gate, self.feature_seal, None,
                            "baseline/v5.1/H22", self.rows,
                        )
                self.assertFalse((directory / "prediction_seal.json").exists())
                seal = oof_ledger.seal_oof_predictions(
                    self.spec, self.gate, self.feature_seal, None,
                    "baseline/v5.1/H22", self.rows,
                )
                self.assertTrue(Path(seal.path).is_file())

    def test_aliased_prediction_descriptor_is_rejected_before_its_read(self):
        from mlb_backtest.model_improvement import oof_ledger

        seal = oof_ledger.seal_oof_predictions(
            self.spec, self.gate, self.feature_seal, None, "baseline/v5.1/H22", self.rows
        )
        aliases = (
            f"{Path(seal.path).parent}/./prediction_seal.json",
            f"/{seal.path}",
        )
        for alias in aliases:
            with self.subTest(alias=alias), patch.object(
                oof_ledger, "read_regular_bytes", side_effect=AssertionError("read")
            ), self.assertRaises(ValueError):
                oof_ledger.load_prediction_seal(
                    self.spec, self.gate, self.feature_seal, None, alias, seal.artifact_sha256
                )

    def test_seal_loader_rejects_missing_or_tampered_prediction_rows(self):
        from mlb_backtest.model_improvement import oof_ledger

        for corruption in ("missing rows", "missing sidecar", "tampered rows", "tampered sidecar"):
            with self.subTest(corruption=corruption):
                directory = self.spec.output_path(
                    "oof", "baseline/v5.1/H22/prediction_seal.json"
                ).parent
                if directory.exists():
                    for artifact in directory.iterdir():
                        artifact.unlink()
                seal = oof_ledger.seal_oof_predictions(
                    self.spec, self.gate, self.feature_seal, None,
                    "baseline/v5.1/H22", self.rows,
                )
                rows_path, rows_sidecar, _, _ = oof_ledger.prediction_paths(
                    self.spec, seal.ledger_id
                )
                if corruption == "missing rows":
                    rows_path.unlink()
                elif corruption == "missing sidecar":
                    rows_sidecar.unlink()
                elif corruption == "tampered rows":
                    rows_path.write_bytes(b"\n")
                else:
                    rows_sidecar.write_bytes(
                        canonical_json_bytes({"artifact_sha256": SHA})
                    )
                with self.assertRaises(ValueError):
                    oof_ledger.load_prediction_seal(
                        self.spec, self.gate, self.feature_seal, None,
                        seal.path, seal.artifact_sha256,
                    )

    def test_seal_loader_recomputes_prediction_population(self):
        from mlb_backtest.model_improvement import oof_ledger

        seal = oof_ledger.seal_oof_predictions(
            self.spec, self.gate, self.feature_seal, None,
            "baseline/v5.1/H22", self.rows,
        )
        path = Path(seal.path)
        payload = json.loads(path.read_bytes())
        payload["population_sha256"] = SHA
        raw = canonical_json_bytes(payload)
        digest = sha256(raw).hexdigest()
        path.write_bytes(raw)
        path.with_suffix(".json.sha256").write_bytes(
            canonical_json_bytes({"artifact_sha256": digest})
        )
        with self.assertRaises(ValueError):
            oof_ledger.load_prediction_seal(
                self.spec, self.gate, self.feature_seal, None, path, digest
            )

    def test_all_task8_public_replay_parameters_are_typed_receipts(self):
        from mlb_backtest.model_improvement import oof_ledger

        for function in (
            oof_ledger.seal_oof_predictions,
            oof_ledger.load_prediction_seal,
            oof_ledger.load_prediction_rows,
        ):
            annotation = inspect.signature(function).parameters["baseline_replay"].annotation
            self.assertIn("BaselineReplayReceipt", str(annotation))
            self.assertIn("None", str(annotation))

    def test_schema_is_closed_and_module_has_no_outcome_or_evaluation_imports(self):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        payload = {
            "schema": "MODEL_IMPROVEMENT_PREDICTION_SEAL_V1",
            "ledger_id": "baseline/v5.1/H22", "fold_id": "H22", "model_id": "v5.1",
            "candidate_id": None, "run_spec_sha256": SHA, "source_gate_sha256": SHA,
            "feature_seal_sha256": SHA, "prediction_contract_sha256": SHA,
            "scoring_model_card_sha256": SHA, "population_sha256": SHA,
            "frozen_model_card_sha256": None, "rows_sha256": SHA,
        }
        Draft202012Validator(schema).validate(payload)
        with self.assertRaises(Exception):
            Draft202012Validator(schema).validate(payload | {"label": 1})
        tree = ast.parse((ROOT / "mlb_backtest/model_improvement/oof_ledger.py").read_text())
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any("outcome" in name or "evaluation" in name for name in imports))


if __name__ == "__main__":
    unittest.main()
