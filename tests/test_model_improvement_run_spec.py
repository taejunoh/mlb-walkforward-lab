import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

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
    VerifiedRunSpecActivation,
)


SPEC_PATH = Path(__file__).parent / "fixtures/model_improvement/parallel_model_result_v1.synthetic.json"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPOSITORY_ROOT / "schemas/model_improvement/parallel_model_result_run_spec.schema.json"


class RunSpecTest(unittest.TestCase):
    def raw(self):
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))

    def test_exact_contract_is_available_to_sibling_lanes(self):
        spec = load_run_spec(SPEC_PATH)
        self.assertIsNone(spec.verified_activation)
        self.assertIsNone(build_run_spec(self.raw()).verified_activation)
        self.assertEqual(spec.fold("H22").normalization_years, (2018, 2019, 2020, 2021))
        self.assertEqual(spec.fold("H24").oof_calibration_years, (2019, 2020, 2021, 2022, 2023))
        self.assertEqual(dict(spec.cutoffs), {"history": "D-1", "snapshot": "T-45"})
        self.assertEqual(
            spec.source_authority.inventory.sha256,
            self.raw()["source_authority"]["inventory"]["sha256"],
        )
        expected_exclusions = {
            "excluded_years": [2025],
            "forbidden_outcome_years": [2025],
            "forbid_h24_outcomes": True,
        }
        parsed_exclusions = {
            "excluded_years": list(spec.exclusions["excluded_years"]),
            "forbidden_outcome_years": list(spec.exclusions["forbidden_outcome_years"]),
            "forbid_h24_outcomes": spec.exclusions["forbid_h24_outcomes"],
        }
        self.assertEqual(
            parsed_exclusions, expected_exclusions,
        )
        self.assertEqual(
            sha256(canonical_json_bytes(parsed_exclusions)).hexdigest(),
            "fde54501d2d20653e324d25a45e0a36f3d8dbd4c3f1f5d7bafd9cfe5ab30f207",
        )
        self.assertEqual(spec.lane_b.keep_h22_count, 12)
        self.assertEqual(spec.lane_b.h23_order, ("brier", "log_loss", "daily_top1_delta_desc", "candidate_id"))
        self.assertRegex(spec.prediction_contract_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(str(spec.output_path("features", "lane_c/H24/feature_seal.json")).endswith("features/lane_c/H24/feature_seal.json"))
        self.assertTrue(spec.h24_firewall.forbid_outcomes_before_registry)
        self.assertEqual(run_spec_sha256(spec), sha256(canonical_json_bytes(spec.as_mapping())).hexdigest())

    def test_prediction_contract_digest_and_closed_mutations(self):
        raw = self.raw()
        self.assertEqual(raw["prediction_contract_sha256"], sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest())
        contract = raw["prediction_contract"]
        self.assertEqual(
            contract["confidence_formula"],
            "scoring.confidence_score(completeness, starter_quality, offense_quality, bullpen_quality, component_agreement(starter_edge,offense_edge,bullpen_edge,picked_side), environment_quality, schedule_quality)",
        )
        self.assertEqual(contract["confidence_range"], [0.0, 10.0])
        self.assertEqual(raw["prediction_contract_sha256"], "eda2b44aae29b8936136d0043bd4cea2811da37de1e37e3a1672a48c88e5b155")
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
        for mutation in ("delete", "change"):
            raw = self.raw()
            if mutation == "delete":
                del raw["prediction_contract"]["confidence_formula"]
            else:
                raw["prediction_contract"]["confidence_range"] = [0.0, 1.0]
            raw["prediction_contract_sha256"] = sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest()
            with self.assertRaisesRegex(ValueError, "prediction contract"):
                build_run_spec(raw)
        for index, confidence_range in enumerate(([False, 10.0], [0, 10], [0.0, 10.0])):
            raw = self.raw()
            raw["prediction_contract"]["confidence_range"] = confidence_range
            raw["prediction_contract_sha256"] = sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest()
            if index < 2:
                with self.subTest(confidence_range=confidence_range):
                    with self.assertRaisesRegex(ValueError, "prediction contract"):
                        build_run_spec(raw)
        raw = self.raw()
        raw["prediction_contract"]["confidence_formula"] += " "
        raw["prediction_contract_sha256"] = sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest()
        with self.assertRaisesRegex(ValueError, "prediction contract"):
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

    def test_relative_root_is_repository_anchored_and_symlink_root_is_rejected(self):
        raw = self.raw()
        raw["outputs"]["root"] = "test-output-root"
        spec = build_run_spec(raw)
        expected = REPOSITORY_ROOT / "test-output-root/lane_a/lane_disposition.json"
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            try:
                os.chdir(directory)
                self.assertEqual(spec.output_path("lane_a", "lane_disposition.json"), expected)
            finally:
                os.chdir(original_cwd)
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            root = Path(directory) / "root-link"
            target = Path(directory) / "target"
            target.mkdir()
            root.symlink_to(target, target_is_directory=True)
            raw = self.raw()
            raw["outputs"]["root"] = str(root)
            spec = build_run_spec(raw)
            with self.assertRaisesRegex(ValueError, "unsafe immutable artifact"):
                seal_lane_disposition(spec, "A", "REJECTED", None, ("a" * 64,), ("H23_GATE",))

    def test_lane_b_rejects_boolean_multiplier_before_value_comparison(self):
        raw = self.raw()
        raw["lane_b"]["multipliers"] = [0.75, True, 1.25]
        with self.assertRaisesRegex(ValueError, "canonical finite"):
            build_run_spec(raw)

    def test_schema_closes_expressible_semantics(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema)
        validator.validate(self.raw())
        attacks = []
        raw = self.raw()
        raw["source_authority"]["inventory"]["path"] = "/absolute.json"
        attacks.append(raw)
        raw = self.raw()
        raw["baseline"]["card"]["path"] = "../baseline.json"
        attacks.append(raw)
        raw = self.raw()
        raw["source_authority"]["validation_receipts"].append(dict(raw["source_authority"]["validation_receipts"][0]))
        attacks.append(raw)
        raw = self.raw()
        raw["folds"]["H22"]["target_year"] = 2021
        attacks.append(raw)
        raw = self.raw()
        raw["baseline"]["card"]["component_weights_pp"]["starter"] = 34
        attacks.append(raw)
        raw = self.raw()
        raw["lane_b"]["multipliers"] = [0.75, True, 1.25]
        attacks.append(raw)
        raw = self.raw()
        del raw["prediction_contract"]["confidence_formula"]
        attacks.append(raw)
        raw = self.raw()
        raw["prediction_contract"]["confidence_range"] = [0.0, 1.0]
        attacks.append(raw)
        for attack in attacks:
            with self.subTest(attack=attack):
                self.assertTrue(list(validator.iter_errors(attack)))
        for root in (".", "../x", "output\\root"):
            raw = self.raw()
            raw["outputs"]["root"] = root
            with self.subTest(root=root):
                self.assertTrue(list(validator.iter_errors(raw)))
                with self.assertRaises(ValueError):
                    build_run_spec(raw)

    def test_verified_loader_rejects_synthetic_and_wrong_path_or_head(self):
        with self.assertRaisesRegex(ValueError, "production path"):
            load_verified_run_spec(SPEC_PATH)
        raw = self.raw()
        raw["schema_version"] = "PARALLEL_MODEL_RESULT_RUN_SPEC_V1"
        raw["code_revision"] = "0" * 40
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            path = Path(directory) / "parallel_model_result_v1.json"
            path.write_bytes(canonical_json_bytes(raw))
            with self.assertRaisesRegex(ValueError, "production path"):
                load_verified_run_spec(path)

    def _git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def _activation_repository(self, *, code_revision=None, extra_file=False, merge=False, parent_declaration=False):
        directory = tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT)
        root = Path(directory.name)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "test@example.invalid")
        self._git(root, "config", "user.name", "Test")
        (root / "code.txt").write_text("code\n", encoding="utf-8")
        self._git(root, "add", "code.txt")
        self._git(root, "commit", "-qm", "C code")
        code_commit = self._git(root, "rev-parse", "HEAD")
        declaration = root / "protocols/model_improvement/parallel_model_result_v1.json"
        if parent_declaration:
            raw = self.raw()
            raw["schema_version"] = "PARALLEL_MODEL_RESULT_RUN_SPEC_V1"
            raw["code_revision"] = code_commit
            declaration.parent.mkdir(parents=True)
            declaration.write_bytes(canonical_json_bytes(raw))
            self._git(root, "add", "protocols/model_improvement/parallel_model_result_v1.json")
            self._git(root, "commit", "-qm", "C prior declaration")
            code_commit = self._git(root, "rev-parse", "HEAD")
        raw = self.raw()
        raw["schema_version"] = "PARALLEL_MODEL_RESULT_RUN_SPEC_V1"
        raw["code_revision"] = code_commit if code_revision is None else code_revision
        declaration.parent.mkdir(parents=True, exist_ok=True)
        declaration.write_bytes(canonical_json_bytes(raw))
        self._git(root, "add", "protocols/model_improvement/parallel_model_result_v1.json")
        if extra_file:
            (root / "extra.txt").write_text("extra\n", encoding="utf-8")
            self._git(root, "add", "extra.txt")
        self._git(root, "commit", "-qm", "D declaration")
        if merge:
            parent = self._git(root, "rev-parse", "HEAD^")
            self._git(root, "branch", "other", parent)
            self._git(root, "checkout", "-q", "other")
            (root / "other.txt").write_text("other\n", encoding="utf-8")
            self._git(root, "add", "other.txt")
            self._git(root, "commit", "-qm", "other")
            self._git(root, "checkout", "-q", "master")
            self._git(root, "merge", "--no-ff", "-qm", "merge", "other")
        return directory, root, declaration

    def test_verified_loader_activation_freeze_uses_owning_repository_not_cwd(self):
        activation, root, declaration = self._activation_repository()
        other, other_root, _ = self._activation_repository()
        original_cwd = Path.cwd()
        try:
            os.chdir(other_root)
            spec = load_verified_run_spec(declaration)
            self.assertEqual(spec.code_revision, self._git(root, "rev-parse", "HEAD^"))
            self.assertEqual(
                spec.verified_activation,
                VerifiedRunSpecActivation(
                    str(declaration.resolve()),
                    sha256(declaration.read_bytes()).hexdigest(),
                    run_spec_sha256(spec),
                ),
            )
        finally:
            os.chdir(original_cwd)
            other.cleanup()
            activation.cleanup()

    def test_verified_loader_parses_the_verified_blob_without_a_second_path_read(self):
        activation, _root, declaration = self._activation_repository()
        try:
            with patch("mlb_backtest.model_improvement.run_spec.load_run_spec", side_effect=AssertionError("second path read")):
                self.assertEqual(load_verified_run_spec(declaration).schema_version, "PARALLEL_MODEL_RESULT_RUN_SPEC_V1")
        finally:
            activation.cleanup()

    def test_verified_activation_is_runtime_only_and_semantically_inert(self):
        ordinary = load_run_spec(SPEC_PATH)
        descriptor = VerifiedRunSpecActivation("/tmp/activation.json", "a" * 64, run_spec_sha256(ordinary))
        presented = replace(ordinary, verified_activation=descriptor)
        self.assertNotIn("verified_activation", ordinary.as_mapping())
        self.assertNotIn("verified_activation", presented.as_mapping())
        self.assertEqual(run_spec_sha256(ordinary), run_spec_sha256(presented))
        self.assertEqual(run_spec_sha256(presented), descriptor.run_spec_sha256)
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("verified_activation", schema["properties"])

    def test_verified_activation_descriptor_rejects_noncanonical_values(self):
        for path, declaration_sha256, semantic_sha256 in (
            ("relative.json", "a" * 64, "b" * 64),
            ("/tmp/../activation.json", "a" * 64, "b" * 64),
            ("/tmp/activation.json", "A" * 64, "b" * 64),
            ("/tmp/activation.json", "a" * 64, "B" * 64),
        ):
            with self.subTest(path=path, declaration_sha256=declaration_sha256, semantic_sha256=semantic_sha256):
                with self.assertRaises(ValueError):
                    VerifiedRunSpecActivation(path, declaration_sha256, semantic_sha256)

    def test_verified_loader_rejects_activation_drift_and_non_declaration_commits(self):
        cases = (
            ("dirty", lambda root, path: path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8"), "tracked blob"),
            ("untracked", lambda root, path: (root / "untracked.txt").write_text("x\n", encoding="utf-8"), "clean"),
        )
        for name, mutate, message in cases:
            with self.subTest(name=name):
                activation, root, declaration = self._activation_repository()
                try:
                    mutate(root, declaration)
                    with self.assertRaisesRegex(ValueError, message):
                        load_verified_run_spec(declaration)
                finally:
                    activation.cleanup()
        activation, _root, declaration = self._activation_repository()
        try:
            declaration.write_bytes(declaration.read_bytes().replace(b"\n", b"\r\n"))
            with self.assertRaisesRegex(ValueError, "tracked blob"):
                load_verified_run_spec(declaration)
        finally:
            activation.cleanup()
        activation, _root, declaration = self._activation_repository()
        try:
            declaration.write_bytes(b"\xff\n")
            with self.assertRaisesRegex(ValueError, "tracked blob"):
                load_verified_run_spec(declaration)
        finally:
            activation.cleanup()
        for name, kwargs, message in (
            ("extra", {"extra_file": True}, "declaration-only"),
            ("merge", {"merge": True}, "single-parent"),
            ("wrong-code", {"code_revision": "0" * 40}, "code_revision"),
            ("parent-declaration", {"parent_declaration": True}, "must add"),
        ):
            with self.subTest(name=name):
                activation, _root, declaration = self._activation_repository(**kwargs)
                try:
                    with self.assertRaisesRegex(ValueError, message):
                        load_verified_run_spec(declaration)
                finally:
                    activation.cleanup()
        activation, root, declaration = self._activation_repository()
        cross, cross_root, cross_declaration = self._activation_repository()
        try:
            with self.assertRaisesRegex(ValueError, "production path"):
                load_verified_run_spec(root / "external.json")
            cross_declaration.unlink()
            cross_declaration.symlink_to(declaration)
            with self.assertRaisesRegex(ValueError, "production path"):
                load_verified_run_spec(cross_declaration)
        finally:
            cross.cleanup()
            activation.cleanup()

    def test_lane_disposition_round_trips_in_temporary_synthetic_output_root(self):
        raw = self.raw()
        with tempfile.TemporaryDirectory(dir=REPOSITORY_ROOT) as directory:
            raw["outputs"]["root"] = directory
            spec = build_run_spec(raw)
            disposition = seal_lane_disposition(spec, "A", "REJECTED", None, ("a" * 64,), ("H23_GATE",))
            self.assertEqual(load_lane_disposition(spec, Path(disposition.path), disposition.artifact_sha256), disposition)
            with self.assertRaises(ValueError):
                load_lane_disposition(spec, Path(disposition.path), "b" * 64)
