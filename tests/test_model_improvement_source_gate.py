"""TDD coverage for the verified, immutable model-improvement source gate."""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
import mlb_backtest.model_improvement.source_gate as source_gate
from mlb_backtest.model_improvement.run_spec import (
    VerifiedRunSpecActivation,
    build_run_spec,
    load_run_spec,
    load_verified_run_spec,
    run_spec_sha256,
)

from mlb_backtest.model_improvement.source_gate import (  # RED: module does not exist yet.
    SourceGateReceipt,
    load_source_gate_receipt,
    require_source_gate,
    verify_source_gate,
)


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC = ROOT / "tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
SCHEMA = ROOT / "schemas/model_improvement/source_gate_receipt.schema.json"
PRODUCTION = ROOT / "protocols/model_improvement/parallel_model_result_v1.json"
SHA = "a" * 64
FAMILIES = (
    "baseball_savant_statcast",
    "mlb_schedule_game_feed",
    "mlb_stats_pitcher_bf",
    "mlb_stats_pitching",
    "prior_schedule_history",
    "venue_team_timezone_table",
)


class SourceGateTest(unittest.TestCase):
    def raw(self):
        return json.loads(SYNTHETIC.read_text(encoding="utf-8"))

    def git(self, root: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, text=True,
            capture_output=True,
        ).stdout.strip()

    def make_activation(self):
        temporary = tempfile.TemporaryDirectory(dir=ROOT)
        root = Path(temporary.name)
        self.git(root, "init", "-q")
        self.git(root, "config", "user.email", "test@example.invalid")
        self.git(root, "config", "user.name", "Test")
        authority = root / "authority"
        bindings = {
            "source_run_spec": "source/source_run_spec.json",
            "acquisition_contract": "source/acquisition_contract.json",
            "terminal_checkpoint": "source/terminal_checkpoint.json",
            "inventory": "source/inventory.json",
            "validation_receipt": "source/validation_receipt.json",
        }
        values = {}
        for name, relative in bindings.items():
            path = authority / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = canonical_json_bytes({"kind": name})
            path.write_bytes(content)
            values[name] = sha256(content).hexdigest()
        (root / "code.txt").write_text("code\n", encoding="utf-8")
        self.git(root, "add", "authority", "code.txt")
        self.git(root, "commit", "-qm", "C code and source authority")
        parent = self.git(root, "rev-parse", "HEAD")
        raw = self.raw()
        raw["schema_version"] = "PARALLEL_MODEL_RESULT_RUN_SPEC_V1"
        raw["code_revision"] = parent
        raw["source_authority"] = {
            "authority_root": "authority",
            "source_run_spec": {"path": bindings["source_run_spec"], "sha256": values["source_run_spec"]},
            "acquisition_contract": {"path": bindings["acquisition_contract"], "sha256": values["acquisition_contract"]},
            "terminal_checkpoint": {"path": bindings["terminal_checkpoint"], "sha256": values["terminal_checkpoint"]},
            "inventory": {"path": bindings["inventory"], "sha256": values["inventory"]},
            "validation_receipts": [{"path": bindings["validation_receipt"], "sha256": values["validation_receipt"]}],
        }
        output_root = Path(tempfile.mkdtemp(dir=ROOT))
        self.addCleanup(shutil.rmtree, output_root, ignore_errors=True)
        raw["outputs"]["root"] = str(output_root)
        declaration = root / "protocols/model_improvement/parallel_model_result_v1.json"
        declaration.parent.mkdir(parents=True)
        declaration.write_bytes(canonical_json_bytes(raw))
        self.git(root, "add", "protocols/model_improvement/parallel_model_result_v1.json")
        self.git(root, "commit", "-qm", "D declaration")
        return temporary, root, declaration, authority

    def valid_snapshots(self):
        return tuple(
            SimpleNamespace(
                season=year, source=source, grain="fixture", payload={"year": year, "source": source},
            )
            for year in range(2018, 2025)
            for source in FAMILIES
        ) + tuple(
            SimpleNamespace(
                season=2018, source=FAMILIES[0], grain="fixture-b", payload={"extra": index},
            )
            for index in range(39)
        )

    def source_patches(self, snapshots=None, calls=None):
        if snapshots is None:
            snapshots = self.valid_snapshots()
        if calls is None:
            calls = []
        def record(name, result=None):
            def inner(*args, **kwargs):
                calls.append(name)
                return result
            return inner
        return (
            patch("mlb_backtest.model_improvement.source_gate.protocols.load_source_run_spec", record("source", SimpleNamespace(run_id="source"))),
            patch("mlb_backtest.model_improvement.source_gate.execution_registry.validate_sources_ready_evidence_set", record("validate")),
            patch("mlb_backtest.model_improvement.source_gate.execution_registry.load_terminal_acquisition_checkpoint", record("checkpoint", SimpleNamespace(receipt_sha256="c" * 64))),
            patch("mlb_backtest.model_improvement.source_gate.v52_verify_sources.load_snapshot_replay_inputs", record("inputs", SimpleNamespace())),
            patch("mlb_backtest.model_improvement.source_gate.v52_verify_sources.replay_verified_snapshots", record("replay", snapshots)),
        )

    def test_payload_schema_and_loader_are_closed(self):
        payload = {
            "run_spec_sha256": SHA, "source_run_spec_sha256": "b" * 64,
            "acquisition_contract_sha256": "c" * 64, "checkpoint_sha256": "d" * 64,
            "inventory_sha256": "e" * 64, "validation_receipts_sha256": ["f" * 64],
            "snapshot_replay_sha256": "0" * 64, "required_years": list(range(2018, 2025)),
            "status": "SOURCES_READY",
        }
        self.assertNotIn("artifact_sha256", payload)
        self.assertNotIn("path", payload)
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(payload)
        receipt = SourceGateReceipt.from_payload_mapping(payload, "1" * 64, "gate.json")
        self.assertEqual(receipt.validation_receipts_sha256, ("f" * 64,))
        self.assertEqual(receipt.as_mapping(), payload)
        with self.assertRaises(ValueError):
            SourceGateReceipt.from_payload_mapping(payload | {"unknown": True}, "1" * 64, "gate.json")

    def test_absent_real_production_declaration_fails_verified_loading(self):
        with self.assertRaises(ValueError):
            load_verified_run_spec(PRODUCTION)

    def test_verified_activation_publishes_and_requires_exact_second_verification(self):
        temporary, root, declaration, _authority = self.make_activation()
        try:
            spec = load_verified_run_spec(declaration)
            calls = []
            patches = self.source_patches(calls=calls)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                receipt = verify_source_gate(spec)
                self.assertTrue(Path(receipt.path).exists())
                self.assertEqual(receipt.status, "SOURCES_READY")
                required = require_source_gate(spec)
            self.assertEqual(receipt, required)
            self.assertEqual(calls.count("replay"), 2)
        finally:
            temporary.cleanup()

    def test_activation_rejection_happens_before_source_access(self):
        ordinary = load_run_spec(SYNTHETIC)
        fake = dataclasses.replace(
            ordinary, verified_activation=VerifiedRunSpecActivation("/tmp/fake", "0" * 64, run_spec_sha256(ordinary)),
        )
        for presented in (ordinary, build_run_spec(self.raw()), fake):
            calls = []
            patches = self.source_patches(calls=calls)
            with self.subTest(presented=presented), patches[0], patches[1], patches[2], patches[3], patches[4]:
                with self.assertRaises(ValueError):
                    verify_source_gate(presented)
                self.assertEqual(calls, [])

    def test_dirty_activation_and_bad_bound_digest_write_nothing(self):
        temporary, root, declaration, authority = self.make_activation()
        try:
            verified = load_verified_run_spec(declaration)
            (root / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            calls = []
            patches = self.source_patches(calls=calls)
            with patches[0], patches[1], patches[2], patches[3], patches[4], self.assertRaises(ValueError):
                verify_source_gate(verified)
            self.assertEqual(calls, [])
            (root / "dirty.txt").unlink()
            spec = load_verified_run_spec(declaration)
            (authority / "source/inventory.json").write_text("mutated\n", encoding="utf-8")
            patches = self.source_patches(calls=calls)
            with patches[0], patches[1], patches[2], patches[3], patches[4], self.assertRaises(ValueError):
                verify_source_gate(spec)
            self.assertFalse(spec.output_path("source_gate", "source_gate_receipt.json").exists())
        finally:
            temporary.cleanup()

    def test_snapshot_cardinality_year_and_family_fail_closed(self):
        temporary, _root, declaration, _authority = self.make_activation()
        try:
            spec = load_verified_run_spec(declaration)
            bad_cases = (
                self.valid_snapshots()[:-1],
                tuple(item for item in self.valid_snapshots() if item.season != 2018),
                tuple(item for item in self.valid_snapshots() if item.source != FAMILIES[-1]),
            )
            for snapshots in bad_cases:
                patches = self.source_patches(snapshots)
                with self.subTest(size=len(snapshots)), patches[0], patches[1], patches[2], patches[3], patches[4], self.assertRaises(ValueError):
                    verify_source_gate(spec)
                self.assertFalse(spec.output_path("source_gate", "source_gate_receipt.json").exists())
        finally:
            temporary.cleanup()

    def test_post_call_bound_file_mutation_prevents_publication(self):
        temporary, _root, declaration, authority = self.make_activation()
        try:
            spec = load_verified_run_spec(declaration)
            calls = []
            patches = list(self.source_patches(calls=calls))
            def mutate_after_replay(_inputs):
                (authority / "source/inventory.json").write_text("changed\n", encoding="utf-8")
                return self.valid_snapshots()
            patches[-1] = patch(
                "mlb_backtest.model_improvement.source_gate.v52_verify_sources.replay_verified_snapshots",
                mutate_after_replay,
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], self.assertRaises(ValueError):
                verify_source_gate(spec)
            self.assertIn("inputs", calls)
            self.assertFalse(spec.output_path("source_gate", "source_gate_receipt.json").exists())
        finally:
            temporary.cleanup()

    def test_new_primary_is_removed_if_mandatory_sidecar_publish_fails(self):
        temporary, _root, declaration, _authority = self.make_activation()
        try:
            spec = load_verified_run_spec(declaration)
            primary = spec.output_path("source_gate", "source_gate_receipt.json")
            sidecar = primary.with_suffix(primary.suffix + ".sha256")
            real_writer = source_gate.write_immutable_json

            def fail_sidecar(path, value):
                if path == sidecar:
                    raise ValueError("injected sidecar failure")
                return real_writer(path, value)

            patches = self.source_patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
                source_gate, "write_immutable_json", side_effect=fail_sidecar
            ), self.assertRaisesRegex(ValueError, "injected sidecar failure"):
                verify_source_gate(spec)
            self.assertFalse(primary.exists())
            self.assertFalse(sidecar.exists())
        finally:
            temporary.cleanup()

    def test_preexisting_identical_primary_survives_sidecar_publish_failure(self):
        temporary, _root, declaration, _authority = self.make_activation()
        try:
            spec = load_verified_run_spec(declaration)
            patches = self.source_patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                receipt = verify_source_gate(spec)
            primary = Path(receipt.path)
            sidecar = primary.with_suffix(primary.suffix + ".sha256")
            expected_bytes = canonical_json_bytes(receipt.as_mapping())
            sidecar.unlink()
            real_writer = source_gate.write_immutable_json

            def fail_sidecar(path, value):
                if path == sidecar:
                    raise ValueError("injected sidecar failure")
                return real_writer(path, value)

            patches = self.source_patches()
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch.object(
                source_gate, "write_immutable_json", side_effect=fail_sidecar
            ), self.assertRaisesRegex(ValueError, "injected sidecar failure"):
                verify_source_gate(spec)
            self.assertEqual(primary.read_bytes(), expected_bytes)
            self.assertFalse(sidecar.exists())
        finally:
            temporary.cleanup()

    def test_loader_rejects_wrong_digest_bad_path_and_sidecar(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            path = Path(directory) / "gate.json"
            payload = SourceGateReceipt(SHA, "b" * 64, "c" * 64, "d" * 64, "e" * 64, ("f" * 64,), "0" * 64, tuple(range(2018, 2025)), "SOURCES_READY", "1" * 64, str(path)).as_mapping()
            path.write_bytes(canonical_json_bytes(payload))
            digest = sha256(path.read_bytes()).hexdigest()
            path.with_suffix(".json.sha256").write_bytes(canonical_json_bytes({"artifact_sha256": digest}))
            with self.assertRaisesRegex(ValueError, "digest differs"):
                load_source_gate_receipt(path, "0" * 64)
            self.assertEqual(load_source_gate_receipt(path, digest).artifact_sha256, digest)
