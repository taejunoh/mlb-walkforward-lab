from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.preflight import parse_preflight, protected_inventory
from mlb_backtest.protocols import (
    load_prospective_spec,
    load_run_spec,
    load_v52_config,
)
from mlb_backtest.verify_v52_design import (
    APPROVED_DESIGN_COMMIT,
    build_git_inventory,
    historical_execution_status,
    make_tiny_git_fixture,
    observation_namespace_status,
    verify_artifact_agreement,
    verify_contract_schemas,
    verify_design_deliverables,
    verify_leakage_evidence,
    verify_markdown_agreement,
    verify_namespaces_differ,
    verify_notebook_agreement,
    verify_protected_inventory,
    verify_protected_snapshot,
    write_git_inventory,
)


ROOT = Path(__file__).resolve().parents[1]


class DesignVerificationTest(unittest.TestCase):
    def test_current_deliverables_cross_validate(self) -> None:
        receipt = verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["diagnostic"]["primary"], {"W": 67, "L": 33, "N": 100})
        self.assertEqual(receipt["v52"]["fitted_model_card_count"], 0)
        self.assertEqual(receipt["preflight"]["status"], "BLOCKED_REQUIRED_ACQUISITION")
        self.assertEqual(receipt["historical"]["execution_status"], "NOT_EXECUTED")
        self.assertEqual(receipt["prospective"]["observation_status"], "NOT_STARTED")
        self.assertEqual(receipt["immutability"]["approved_commit"], "a4aa8d2")

    def test_verifier_uses_no_network_or_python_write_api(self) -> None:
        before = protected_inventory(ROOT)
        with (
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "http.client.HTTPConnection.request",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "pathlib.Path.write_bytes",
                side_effect=AssertionError("write forbidden"),
            ),
            patch(
                "pathlib.Path.write_text",
                side_effect=AssertionError("write forbidden"),
            ),
            patch("os.replace", side_effect=AssertionError("write forbidden")),
            patch("os.rename", side_effect=AssertionError("write forbidden")),
            patch("os.remove", side_effect=AssertionError("write forbidden")),
            patch("os.unlink", side_effect=AssertionError("write forbidden")),
        ):
            receipt = verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(before, protected_inventory(ROOT))

    def test_mutating_any_sealed_v51_artifact_fails(self) -> None:
        for change in ("mutate", "add", "delete"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                repo = make_tiny_git_fixture(Path(directory))
                inventory = write_git_inventory(repo, "HEAD")
                target = repo / "data/v5_1_bt/locked/predictions.jsonl"
                if change == "mutate":
                    target.write_bytes(b"changed\n")
                elif change == "add":
                    (target.parent / "added.json").write_text("{}\n", encoding="utf-8")
                else:
                    target.unlink()
                with self.assertRaisesRegex(ValueError, "protected v5.1 inventory"):
                    verify_protected_inventory(repo, inventory)

    def test_full_tree_metadata_guard_detects_ignored_addition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_tiny_git_fixture(Path(directory))
            (repo / ".gitignore").write_text("*.bin\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "ignore fixture"], cwd=repo, check=True
            )
            snapshot = protected_inventory(repo)
            (repo / "data/v5_1_bt/locked/ignored.bin").write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "protected evidence metadata"):
                verify_protected_snapshot(repo, snapshot)

    def test_inventory_is_pinned_to_approved_design_commit(self) -> None:
        inventory_path = ROOT / "protocols/v5_2_qd/v51_protected_inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        self.assertEqual(APPROVED_DESIGN_COMMIT, "a4aa8d2")
        self.assertEqual(inventory["approved_commit"], "a4aa8d2")
        self.assertEqual(inventory, build_git_inventory(ROOT, "a4aa8d2"))
        self.assertEqual(
            inventory_path.read_bytes(), canonical_json_bytes(inventory) + b"\n"
        )

    def test_absence_status_is_derived_not_hardcoded(self) -> None:
        self.assertEqual(historical_execution_status(ROOT), "NOT_EXECUTED")
        self.assertEqual(observation_namespace_status(ROOT), "NOT_STARTED")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "audit/v5_2_qd/historical/nested/unexpected.bin"
            path.parent.mkdir(parents=True)
            path.write_text("x", encoding="utf-8")
            self.assertEqual(
                historical_execution_status(root), "PARTIAL_OUTPUT_PRESENT"
            )
            observation = root / "data/v5_2_qd/prospective_2026/observations.jsonl"
            observation.parent.mkdir(parents=True)
            observation.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                observation_namespace_status(root), "PARTIAL_OUTPUT_PRESENT"
            )

    def test_inventory_baseline_cannot_be_repointed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_tiny_git_fixture(Path(directory))
            inventory = write_git_inventory(repo, "HEAD")
            payload = json.loads(inventory.read_text(encoding="utf-8"))
            payload["approved_commit"] = "HEAD~1"
            inventory.write_bytes(canonical_json_bytes(payload) + b"\n")
            with self.assertRaisesRegex(ValueError, "protected v5.1 inventory"):
                verify_protected_inventory(repo, inventory)

    def test_leakage_status_tamper_fails(self) -> None:
        diagnostic = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(encoding="utf-8")
        )
        diagnostic["leakage"]["statistical_as_of_safety"][
            "lock_to_outcome_join_sequence_status"
        ] = "PASS"
        with self.assertRaisesRegex(ValueError, "leakage evidence differs"):
            verify_leakage_evidence(diagnostic)

    def test_cross_artifact_tampering_fails(self) -> None:
        diagnostic = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(encoding="utf-8")
        )
        cases = ("markdown", "notebook", "chart_map")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "reports/v5_1_bt").mkdir(parents=True)
                (root / "analysis/v5_1_bt").mkdir(parents=True)
                for relative in (
                    "reports/v5_1_bt/diagnostic_100.md",
                    "reports/v5_1_bt/diagnostic_100_artifact.json",
                    "reports/v5_1_bt/diagnostic_100_chart_map.json",
                    "analysis/v5_1_bt/diagnostic_100.ipynb",
                ):
                    shutil.copy2(ROOT / relative, root / relative)
                if case == "markdown":
                    path = root / "reports/v5_1_bt/diagnostic_100.md"
                    path.write_text(path.read_text() + "tamper\n", encoding="utf-8")
                    verifier = verify_markdown_agreement
                elif case == "notebook":
                    path = root / "analysis/v5_1_bt/diagnostic_100.ipynb"
                    notebook = json.loads(path.read_text(encoding="utf-8"))
                    notebook["cells"][-1]["outputs"][0]["text"] = ["{}\n"]
                    path.write_text(json.dumps(notebook), encoding="utf-8")
                    verifier = verify_notebook_agreement
                else:
                    path = root / "reports/v5_1_bt/diagnostic_100_chart_map.json"
                    chart_map = json.loads(path.read_text(encoding="utf-8"))
                    chart_map["monthly"]["takeaway"] = "tamper"
                    path.write_text(json.dumps(chart_map), encoding="utf-8")
                    verifier = verify_artifact_agreement
                with self.assertRaises(ValueError):
                    verifier(root, diagnostic)

    def test_schema_const_tampering_fails(self) -> None:
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "schemas").mkdir()
            for name in (
                "v5_2_qd.schema.json",
                "v5_2_run_spec.schema.json",
                "v5_2_prospective.schema.json",
                "v5_2_preflight.schema.json",
            ):
                shutil.copy2(ROOT / "schemas" / name, root / "schemas" / name)
            path = root / "schemas/v5_2_qd.schema.json"
            schema = json.loads(path.read_text(encoding="utf-8"))
            schema["const"]["model_version"] = "tampered"
            path.write_text(json.dumps(schema), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema/parser disagreement"):
                verify_contract_schemas(
                    root, config, historical, prospective, preflight
                )

    def test_historical_and_prospective_namespace_overlap_fails(self) -> None:
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        overlap = replace(prospective, namespace=historical.raw["namespaces"]["locked"])
        with self.assertRaisesRegex(ValueError, "namespaces overlap"):
            verify_namespaces_differ(historical, overlap)


if __name__ == "__main__":
    unittest.main()
