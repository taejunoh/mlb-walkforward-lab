"""Task6B baseline-only authenticated outcome contract tests."""

from __future__ import annotations

import ast
import json
import tempfile
import unittest
from hashlib import sha256
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/model_improvement/outcome_receipt.schema.json"


class OutcomeContractTest(unittest.TestCase):
    def test_outcome_row_is_closed_and_derives_only_boolean_outcomes(self) -> None:
        from mlb_backtest.model_improvement.outcomes import OutcomeRow

        row = OutcomeRow(
            game_pk=1,
            official_date="2022-04-01",
            target_year=2022,
            scheduled_start_utc="2022-04-01T17:00:00Z",
            home_win=True,
            source_game_sha256="a" * 64,
        )
        self.assertEqual(
            row.as_mapping(),
            {
                "game_pk": 1,
                "official_date": "2022-04-01",
                "target_year": 2022,
                "scheduled_start_utc": "2022-04-01T17:00:00Z",
                "home_win": True,
                "source_game_sha256": "a" * 64,
            },
        )
        for value in (1, 0, "true", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OutcomeRow(1, "2022-04-01", 2022, "2022-04-01T17:00:00Z", value, "a" * 64)

    def test_receipt_schema_is_closed_and_has_no_runtime_descriptor_fields(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        properties = schema["properties"]
        self.assertNotIn("artifact_sha256", properties)
        self.assertNotIn("path", properties)
        self.assertEqual(schema["additionalProperties"], False)

    def test_module_has_no_candidate_or_evaluation_dependencies(self) -> None:
        path = ROOT / "mlb_backtest/model_improvement/outcomes.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports.extend(node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom))
        self.assertFalse(any("candidate" in name or "evaluation" in name or "task8b" in name.lower() for name in imports))

    def test_verified_game_derivation_rejects_every_nonfinal_or_ambiguous_outcome(self) -> None:
        from mlb_backtest.model_improvement.outcomes import _validate_game

        prediction = SimpleNamespace(
            game_pk=1,
            official_date="2022-04-01",
            target_year=2022,
            scheduled_start_utc="2022-04-01T17:00:00Z",
        )
        game = {
            "game_pk": 1, "official_date": "2022-04-01", "scheduled_start_utc": "2022-04-01T17:00:00Z",
            "season": 2022, "home_team_id": 10, "away_team_id": 20, "winner_team_id": 10,
            "home_score": 4, "away_score": 3, "played": True, "terminal_status": "Final",
            "verification_status": "VERIFIED", "source_game_sha256": "b" * 64,
        }
        self.assertTrue(_validate_game(prediction, game, "H22").home_win)
        invalid = {
            "unplayed": {"played": False},
            "nonterminal": {"terminal_status": "In Progress"},
            "unverified": {"verification_status": "PENDING"},
            "tie": {"away_score": 4},
            "bool score": {"home_score": True},
            "negative": {"away_score": -1},
            "wrong winner": {"winner_team_id": 20},
            "date": {"official_date": "2022-04-02"},
            "utc": {"scheduled_start_utc": "2022-04-01T18:00:00Z"},
            "year": {"season": 2023},
        }
        for name, replacement in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                _validate_game(prediction, game | replacement, "H22")

    def test_outcome_schema_accepts_only_the_persisted_closed_payload(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        payload = {
            "schema": "MODEL_IMPROVEMENT_OUTCOME_RECEIPT_V1", "ledger_id": "baseline/v5.1/H22",
            "fold_id": "H22", "model_id": "v5.1", "run_spec_sha256": "a" * 64,
            "source_gate_sha256": "a" * 64, "source_inventory_sha256": "a" * 64,
            "game_evidence_inventory_relative_path": "audit/game.json",
            "game_evidence_inventory_sha256": "a" * 64, "feature_seal_sha256": "a" * 64,
            "baseline_replay_sha256": "a" * 64, "prediction_seal_sha256": "a" * 64,
            "prediction_rows_sha256": "a" * 64, "prediction_contract_sha256": "a" * 64,
            "scoring_model_card_sha256": "a" * 64, "population_sha256": "a" * 64,
            "outcomes_sha256": "a" * 64,
        }
        Draft202012Validator(schema).validate(payload)
        with self.assertRaises(Exception):
            Draft202012Validator(schema).validate(payload | {"artifact_sha256": "a" * 64})

    def test_authenticated_games_parses_real_source_inventory_and_rejects_derived_binding_substitution(self) -> None:
        """Only the game-evidence path and digest parsed from inventory are admitted."""
        from mlb_backtest.model_improvement import outcomes
        from mlb_backtest.v52_snapshot_store import build_inventory, inventory_bytes, parse_inventory
        from tests.v52_snapshot_store_fixture import patched_store_replay, store_inputs

        with patched_store_replay():
            inventory_bytes_value = inventory_bytes(build_inventory(store_inputs()))
        inventory = parse_inventory(inventory_bytes_value)
        binding = inventory.game_evidence_inventory
        digest = sha256(inventory_bytes_value).hexdigest()
        spec = SimpleNamespace(
            source_authority=SimpleNamespace(
                inventory=SimpleNamespace(path="inventory.json", sha256=digest),
                source_run_spec=SimpleNamespace(path="protocols/run.json"),
                acquisition_contract=SimpleNamespace(path="protocols/contract.json"),
            )
        )
        gate = SimpleNamespace(inventory_sha256=digest)
        game = {
            "game_pk": 1, "official_date": "2022-04-01", "scheduled_start_utc": "2022-04-01T17:00:00Z",
            "season": 2022, "home_team_id": 1, "away_team_id": 2, "winner_team_id": 1,
            "home_score": 1, "away_score": 0, "played": True, "terminal_status": "Final",
            "verification_status": "VERIFIED", "source_game_sha256": "a" * 64,
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            root = Path(directory)

            def read(path, _label):
                if path == root / "inventory.json":
                    return inventory_bytes_value
                if path == root / binding.path:
                    return b"verified inventory bytes"
                raise AssertionError(path)

            verified = SimpleNamespace(path=root / binding.path, sha256=binding.sha256, payload={"games": [game]})
            with patch.object(outcomes, "_source_root", return_value=root), patch.object(
                outcomes, "read_regular_bytes", side_effect=read
            ), patch.object(outcomes, "load_verified_game_evidence_inventory", return_value=verified):
                relative, observed_sha, games = outcomes._authenticated_games(spec, gate)
            self.assertEqual((relative, observed_sha, games[1]), (binding.path, binding.sha256, game))
            for changed in (
                SimpleNamespace(path=root / "elsewhere.json", sha256=binding.sha256, payload={"games": [game]}),
                SimpleNamespace(path=root / binding.path, sha256="f" * 64, payload={"games": [game]}),
                SimpleNamespace(path=root / binding.path, sha256=binding.sha256, payload={"games": [game, game]}),
            ):
                with self.subTest(changed=changed.sha256), patch.object(outcomes, "_source_root", return_value=root), patch.object(
                    outcomes, "read_regular_bytes", side_effect=read
                ), patch.object(outcomes, "load_verified_game_evidence_inventory", return_value=changed), self.assertRaises(ValueError):
                    outcomes._authenticated_games(spec, gate)


class OutcomePublishProtocolTest(unittest.TestCase):
    """Exercise the four-file commit protocol without inventing source labels."""

    def setUp(self) -> None:
        from tests.test_model_improvement_baseline_replay import BaselineReplayV2Test

        self.base = BaselineReplayV2Test(methodName="runTest")
        self.base.setUp()
        self.spec, self.gate, self.feature = self.base.spec, self.base.gate, self.base.feature_seal
        from mlb_backtest.model_improvement.baseline_replay_v2 import BaselineReplayReceiptV2
        from mlb_backtest.model_improvement.contracts import PredictionSealV2
        from mlb_backtest.model_improvement.outcomes import OutcomeRow, _paths
        from mlb_backtest.model_improvement.canonical import canonical_json_bytes
        from mlb_backtest.model_improvement.run_spec import run_spec_sha256

        self.paths = _paths(self.spec, "H22")
        population = sha256(canonical_json_bytes([
            {"game_pk": 100, "official_date": "2022-04-01", "target_year": 2022},
            {"game_pk": 101, "official_date": "2022-04-02", "target_year": 2022},
        ])).hexdigest()
        self.prediction = PredictionSealV2(
            "MODEL_IMPROVEMENT_PREDICTION_SEAL_V2", "baseline/v5.1/H22", "H22", "v5.1",
            run_spec_sha256(self.spec), self.gate.artifact_sha256, self.feature.artifact_sha256,
            self.spec.prediction_contract_sha256, self.spec.baseline.card.sha256, "c" * 64,
            population, None, "e" * 64, "f" * 64,
            str(self.paths.receipt.parent / "prediction_seal.json"),
        )
        self.replay = BaselineReplayReceiptV2(
            "MODEL_IMPROVEMENT_BASELINE_REPLAY_RECEIPT_V2", "baseline/v5.1/H22", "H22", "v5.1",
            run_spec_sha256(self.spec), self.gate.artifact_sha256, self.feature.artifact_sha256,
            self.spec.prediction_contract_sha256, self.spec.baseline.card.sha256,
            "/calibration/card.json", "c" * 64, "d" * 64, "e" * 64, "e" * 64,
            "e" * 64, "e" * 64, "e" * 64, self.prediction.path, self.prediction.artifact_sha256,
            "b" * 64, str(self.paths.receipt.parent / "baseline_replay_receipt.json"),
        )
        self.rows = (
            OutcomeRow(100, "2022-04-01", 2022, "2022-04-01T17:00:00Z", True, "1" * 64),
            OutcomeRow(101, "2022-04-02", 2022, "2022-04-02T17:00:00Z", False, "2" * 64),
        )
        self.materialized = (
            self.gate, self.feature, self.replay, self.prediction, self.rows,
            "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json", "3" * 64,
        )

    def tearDown(self) -> None:
        self.base.tearDown()

    def _patch_authority(self):
        from mlb_backtest.model_improvement import outcomes

        return patch.multiple(
            outcomes,
            _authenticate_gate=lambda *_args: self.gate,
            load_feature_seal=lambda *_args: self.feature,
            load_baseline_replay_v2=lambda *_args: self.replay,
            _materialize=lambda *_args: self.materialized,
            load_prediction_seal_v2=lambda *_args: self.prediction,
        )

    def test_seals_and_reloads_exact_outcome_stream(self) -> None:
        from mlb_backtest.model_improvement import outcomes

        with self._patch_authority():
            receipt = outcomes.seal_outcome_receipt(
                self.spec, self.gate, self.feature, self.replay, self.prediction
            )
            self.assertEqual(
                outcomes.load_outcome_rows(self.spec, self.gate, self.feature, self.replay, receipt),
                self.rows,
            )
        self.assertTrue(Path(receipt.path).is_file())
        self.assertEqual(Path(receipt.path).parent, self.paths.receipt.parent)

    def test_each_commit_crash_leaves_only_an_exact_retry(self) -> None:
        from mlb_backtest.model_improvement import outcomes

        for crash_at in range(1, 5):
            with self.subTest(crash_at=crash_at), self._patch_authority():
                if self.paths.receipt.parent.exists():
                    for artifact in self.paths.receipt.parent.iterdir():
                        artifact.unlink()
                original, calls = outcomes.write_immutable_bytes, 0

                def crash_once(path, contents):
                    nonlocal calls
                    calls += 1
                    if calls == crash_at:
                        raise ValueError("injected crash")
                    return original(path, contents)

                with patch.object(outcomes, "write_immutable_bytes", crash_once), self.assertRaisesRegex(ValueError, "injected crash"):
                    outcomes.seal_outcome_receipt(self.spec, self.gate, self.feature, self.replay, self.prediction)
                self.assertFalse(self.paths.receipt.exists())
                receipt = outcomes.seal_outcome_receipt(self.spec, self.gate, self.feature, self.replay, self.prediction)
                self.assertTrue(Path(receipt.path).is_file())
                for artifact in self.paths.receipt.parent.iterdir():
                    artifact.unlink()

    def test_h24_v1_and_noncommon_inputs_reject_before_artifact_reads(self) -> None:
        from mlb_backtest.model_improvement import outcomes

        h24 = replace(self.feature, fold_id="H24")
        bad_inputs = (
            (h24, self.replay, self.prediction),
            (self.feature, object(), self.prediction),
            (self.feature, self.replay, object()),
        )
        for feature, replay, prediction in bad_inputs:
            with self.subTest(value=type(replay).__name__), patch.object(
                outcomes, "read_regular_bytes", side_effect=AssertionError("read")
            ), self.assertRaises(ValueError):
                outcomes.seal_outcome_receipt(self.spec, self.gate, feature, replay, prediction)

    def test_loader_reauthenticates_before_any_outcome_quartet_read(self) -> None:
        from mlb_backtest.model_improvement import outcomes

        with self._patch_authority():
            receipt = outcomes.seal_outcome_receipt(
                self.spec, self.gate, self.feature, self.replay, self.prediction
            )
        with patch.multiple(
            outcomes,
            _authenticate_gate=lambda *_args: self.gate,
            load_feature_seal=lambda *_args: self.feature,
            load_baseline_replay_v2=lambda *_args: self.replay,
            load_prediction_seal_v2=lambda *_args: self.prediction,
            _materialize=lambda *_args: (_ for _ in ()).throw(ValueError("authority first")),
        ), patch.object(outcomes, "read_regular_bytes", side_effect=AssertionError("outcome read")), self.assertRaisesRegex(ValueError, "authority first"):
            outcomes.load_outcome_receipt(
                self.spec, self.gate, self.feature, self.replay,
                receipt.path, receipt.artifact_sha256,
            )

    def test_loader_detects_authority_mutation_after_rows_read(self) -> None:
        from mlb_backtest.model_improvement import outcomes

        with self._patch_authority():
            receipt = outcomes.seal_outcome_receipt(
                self.spec, self.gate, self.feature, self.replay, self.prediction
            )
        drifted = self.materialized[:-1] + ("4" * 64,)
        with patch.multiple(
            outcomes,
            _authenticate_gate=lambda *_args: self.gate,
            load_feature_seal=lambda *_args: self.feature,
            load_baseline_replay_v2=lambda *_args: self.replay,
            load_prediction_seal_v2=lambda *_args: self.prediction,
            _materialize=lambda *_args: self.materialized,
        ):
            original = outcomes._materialize
            with patch.object(outcomes, "_materialize", side_effect=(self.materialized, drifted)), self.assertRaisesRegex(ValueError, "authority changed"):
                outcomes.load_outcome_receipt(
                    self.spec, self.gate, self.feature, self.replay,
                    receipt.path, receipt.artifact_sha256,
                )
            self.assertIsNotNone(original)

    def test_mutation_between_independent_seal_passes_writes_nothing(self) -> None:
        from mlb_backtest.model_improvement import outcomes

        drifted = self.materialized[:-1] + ("4" * 64,)
        with patch.multiple(
            outcomes,
            _materialize=lambda *_args: self.materialized,
        ):
            with patch.object(outcomes, "_materialize", side_effect=(self.materialized, drifted)), patch.object(
                outcomes, "write_immutable_bytes", side_effect=AssertionError("write")
            ), self.assertRaisesRegex(ValueError, "independent materialization"):
                outcomes.seal_outcome_receipt(
                    self.spec, self.gate, self.feature, self.replay, self.prediction
                )
