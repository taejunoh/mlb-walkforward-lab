from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.preflight import build_preflight, parse_preflight, protected_inventory
from mlb_backtest.protocols import (
    expected_historical_run,
    load_prospective_spec,
    load_run_spec,
    load_v52_config,
    parse_run_spec,
)
from mlb_backtest.verify_v52_design import (
    APPROVED_DESIGN_COMMIT,
    FINAL_HTML_PATH,
    build_git_inventory,
    historical_execution_status,
    make_tiny_git_fixture,
    main,
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
    verify_preflight_payload,
    verify_task11_phase,
    write_git_inventory,
)
from tests.test_preflight import make_complete_inventory


ROOT = Path(__file__).resolve().parents[1]


class DesignVerificationTest(unittest.TestCase):
    def test_current_deliverables_cross_validate(self) -> None:
        receipt = verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(
            receipt,
            {
                "status": "PASS",
                "diagnostic": {"primary": {"W": 67, "L": 33, "N": 100}},
                "v52": {
                    "model_version": "v5.2-QD",
                    "fitted_model_card_count": 0,
                },
                "preflight": {"status": "BLOCKED_REQUIRED_ACQUISITION"},
                "historical": {"execution_status": "NOT_EXECUTED"},
                "prospective": {
                    "first_permitted_date": "2026-07-13",
                    "observation_status": "NOT_STARTED",
                },
                "immutability": {
                    "approved_commit": "a4aa8d2",
                    "tracked_blob_count": 6,
                    "full_tree_guard": "metadata_path_size_mtime_inode_mode",
                    "small_sealed_guard": "sha256",
                },
                "task11": {
                    "html": "ABSENT",
                    "delivery_receipt": "ABSENT",
                },
                "html": None,
            },
        )
        self.assertFalse(FINAL_HTML_PATH.exists())
        self.assertFalse(
            (ROOT / "reports/v5_1_bt/report_delivery_receipt.json").exists()
        )

    def test_task11_artifacts_are_phase_gated(self) -> None:
        verify_task11_phase(
            {"html": "ABSENT", "delivery_receipt": "ABSENT"},
            delivery_requested=False,
        )
        verify_task11_phase(
            {"html": "PRESENT", "delivery_receipt": "PRESENT"},
            delivery_requested=True,
        )
        for status, requested in (
            ({"html": "PRESENT", "delivery_receipt": "PRESENT"}, False),
            ({"html": "ABSENT", "delivery_receipt": "ABSENT"}, True),
        ):
            with self.assertRaisesRegex(ValueError, "Task 11 delivery artifact"):
                verify_task11_phase(status, delivery_requested=requested)

    def test_verifier_uses_no_network_or_python_write_api(self) -> None:
        before = protected_inventory(ROOT)
        real_builtin_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_subprocess_run = subprocess.run

        def guarded_open(opener):
            def call(file, mode="r", *args, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    raise AssertionError("write-mode open forbidden")
                return opener(file, mode, *args, **kwargs)

            return call

        def guarded_os_open(path, flags, *args, **kwargs):
            write_flags = (
                os.O_WRONLY
                | os.O_RDWR
                | os.O_CREAT
                | os.O_TRUNC
                | os.O_APPEND
                | os.O_EXCL
            )
            if flags & write_flags:
                raise AssertionError("write-mode os.open forbidden")
            return real_os_open(path, flags, *args, **kwargs)

        def guarded_subprocess_run(args, *positional, **kwargs):
            allowed = {
                "ls-tree",
                "ls-files",
                "hash-object",
                "status",
                "rev-parse",
                "cat-file",
            }
            if (
                not isinstance(args, list)
                or len(args) < 2
                or args[0] != "git"
                or args[1] not in allowed
                or "-w" in args
            ):
                raise AssertionError(f"subprocess forbidden: {args!r}")
            return real_subprocess_run(args, *positional, **kwargs)

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
            patch("socket.socket", side_effect=AssertionError("network forbidden")),
            patch("builtins.open", side_effect=guarded_open(real_builtin_open)),
            patch("io.open", side_effect=guarded_open(real_io_open)),
            patch("os.open", side_effect=guarded_os_open),
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
            patch("pathlib.Path.rename", side_effect=AssertionError("write forbidden")),
            patch(
                "pathlib.Path.replace", side_effect=AssertionError("write forbidden")
            ),
            patch("pathlib.Path.unlink", side_effect=AssertionError("write forbidden")),
            patch("pathlib.Path.mkdir", side_effect=AssertionError("write forbidden")),
            patch("pathlib.Path.touch", side_effect=AssertionError("write forbidden")),
            patch(
                "mlb_backtest.verify_v52_design.subprocess.run",
                side_effect=guarded_subprocess_run,
            ),
        ):
            receipt = verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(before, protected_inventory(ROOT))

    def test_main_and_module_cli_success_output_and_failure(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main(["--root", str(ROOT), "--skip-html"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")
        with (
            self.assertRaises(SystemExit) as raised,
            patch("sys.stderr", io.StringIO()),
        ):
            main(["--root", str(ROOT)])
        self.assertEqual(raised.exception.code, 2)

        success = subprocess.run(
            [
                "python3",
                "-m",
                "mlb_backtest.verify_v52_design",
                "--root",
                ".",
                "--skip-html",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout), json.loads(stdout.getvalue()))
        failure = subprocess.run(
            [
                "python3",
                "-m",
                "mlb_backtest.verify_v52_design",
                "--root",
                ".",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(failure.returncode, 2)
        self.assertIn("--outputs is required", failure.stderr)

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

    def test_small_sealed_hash_detects_metadata_preserving_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data/v5_1_bt/locked/predictions.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"abc\n")
            before = protected_inventory(root)
            file_stat = path.stat()
            path.write_bytes(b"xyz\n")
            os.utime(path, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
            after = protected_inventory(root)
            self.assertEqual(
                before["metadata_path_size_mtime_inode_mode"],
                after["metadata_path_size_mtime_inode_mode"],
            )
            self.assertNotEqual(
                before["small_sealed_sha256"], after["small_sealed_sha256"]
            )
            with self.assertRaisesRegex(ValueError, "small sealed digest"):
                verify_protected_snapshot(root, before)

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
        cases = ("markdown", "notebook_source", "notebook_output", "chart_map")
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
                elif case.startswith("notebook"):
                    path = root / "analysis/v5_1_bt/diagnostic_100.ipynb"
                    notebook = json.loads(path.read_text(encoding="utf-8"))
                    if case == "notebook_source":
                        notebook["cells"][1]["source"] = ["print('tamper')\n"]
                    else:
                        notebook["cells"][-1]["outputs"][0]["text"] = ["{}\n"]
                    path.write_text(json.dumps(notebook), encoding="utf-8")
                    verifier = None
                else:
                    path = root / "reports/v5_1_bt/diagnostic_100_chart_map.json"
                    chart_map = json.loads(path.read_text(encoding="utf-8"))
                    chart_map["monthly"]["takeaway"] = "tamper"
                    path.write_text(json.dumps(chart_map), encoding="utf-8")
                    verifier = verify_artifact_agreement
                with self.assertRaises(ValueError):
                    if case.startswith("notebook"):
                        verify_notebook_agreement(ROOT, diagnostic, notebook_path=path)
                    else:
                        assert verifier is not None
                        verifier(root, diagnostic)

    def test_every_notebook_cell_source_output_and_metadata_tamper_fails(self) -> None:
        diagnostic = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(encoding="utf-8")
        )
        canonical_path = ROOT / "analysis/v5_1_bt/diagnostic_100.ipynb"
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        mutations = [
            ("source", index, None) for index in range(len(canonical["cells"]))
        ] + [
            ("output", cell_index, output_index)
            for cell_index, cell in enumerate(canonical["cells"])
            for output_index in range(len(cell["outputs"]))
        ]
        mutations.append(("metadata", None, None))
        for kind, cell_index, output_index in mutations:
            with (
                self.subTest(
                    kind=kind, cell_index=cell_index, output_index=output_index
                ),
                tempfile.TemporaryDirectory() as directory,
            ):
                notebook = json.loads(json.dumps(canonical))
                if kind == "source":
                    notebook["cells"][cell_index]["source"].append("# tamper\n")
                elif kind == "output":
                    notebook["cells"][cell_index]["outputs"][output_index][
                        "text"
                    ].append("tamper\n")
                else:
                    notebook["metadata"]["tamper"] = True
                path = Path(directory) / "diagnostic.ipynb"
                path.write_bytes(canonical_json_bytes(notebook) + b"\n")
                with self.assertRaisesRegex(ValueError, "deterministic execution"):
                    verify_notebook_agreement(ROOT, diagnostic, notebook_path=path)

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
            for mutation in ("const", "additionalProperties", "properties"):
                with self.subTest(mutation=mutation):
                    path = root / "schemas/v5_2_qd.schema.json"
                    shutil.copy2(ROOT / "schemas/v5_2_qd.schema.json", path)
                    schema = json.loads(path.read_text(encoding="utf-8"))
                    if mutation == "const":
                        schema["const"]["model_version"] = "tampered"
                    elif mutation == "additionalProperties":
                        schema["additionalProperties"] = True
                    else:
                        schema["properties"].pop("model_version")
                    path.write_text(json.dumps(schema), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError, "schema/parser disagreement"
                    ):
                        verify_contract_schemas(
                            root, config, historical, prospective, preflight
                        )

    def test_nested_schema_refs_closure_and_condition_tampering_fails(self) -> None:
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
        cases = (
            ("historical_ref", "v5_2_run_spec.schema.json"),
            ("historical_closure", "v5_2_run_spec.schema.json"),
            ("prospective_closure", "v5_2_prospective.schema.json"),
            ("preflight_ref", "v5_2_preflight.schema.json"),
            ("preflight_condition", "v5_2_preflight.schema.json"),
            ("preflight_properties", "v5_2_preflight.schema.json"),
        )
        for case, filename in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copytree(ROOT / "schemas", root / "schemas")
                path = root / "schemas" / filename
                schema = json.loads(path.read_text(encoding="utf-8"))
                if case == "historical_ref":
                    schema["properties"]["folds"]["items"]["$ref"] = "#/$defs/x"
                elif case == "historical_closure":
                    schema["$defs"]["fold"]["additionalProperties"] = True
                elif case == "prospective_closure":
                    schema["properties"]["polling"]["additionalProperties"] = True
                elif case == "preflight_ref":
                    schema["properties"]["protected_tree_after"]["$ref"] = "#/$defs/x"
                elif case == "preflight_condition":
                    schema["$defs"]["query_evidence"].pop("allOf")
                else:
                    schema["$defs"]["source_record"]["properties"].pop("source")
                path.write_bytes(canonical_json_bytes(schema) + b"\n")
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

    def test_forged_preflight_inventory_digest_fails(self) -> None:
        saved = json.loads(
            (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_text(
                encoding="utf-8"
            )
        )
        forged = json.loads(json.dumps(saved))
        forged["generated_from_inventory_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "recomputed preflight"):
            verify_preflight_payload(forged, saved)

    def test_manifest_and_raw_hash_tampering_change_recomputed_preflight(self) -> None:
        for mutation in ("manifest", "raw"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = make_complete_inventory(Path(directory) / "root")
                run = parse_run_spec(expected_historical_run())
                saved = build_preflight(root, run)
                manifest_path = next((root / "inventory").rglob("*.manifest.json"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "manifest":
                    manifest["raw_sha256"] = "0" * 64
                    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
                else:
                    raw_path = root / manifest["raw_path"]
                    contents = raw_path.read_bytes()
                    file_stat = raw_path.stat()
                    raw_path.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
                    os.utime(
                        raw_path,
                        ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns),
                    )
                recomputed = build_preflight(root, run)
                self.assertIn(
                    "RAW_HASH_MISMATCH",
                    {
                        field
                        for season in recomputed["seasons"].values()
                        for source in season["sources"].values()
                        for field in source["missing_critical_fields"]
                    },
                )
                with self.assertRaisesRegex(ValueError, "recomputed preflight"):
                    verify_preflight_payload(saved, recomputed)


if __name__ == "__main__":
    unittest.main()
