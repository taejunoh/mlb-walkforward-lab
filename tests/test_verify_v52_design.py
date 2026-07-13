from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
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
    CANONICAL_JSON_FILES,
    DELIVERY_RECEIPT_RELATIVE,
    FINAL_HTML_PATH,
    build_git_inventory,
    fitted_model_card_count,
    historical_execution_status,
    load_canonical_json,
    make_tiny_git_fixture,
    main,
    observation_namespace_status,
    verify_artifact_agreement,
    verify_contract_schemas,
    verify_design_deliverables,
    verify_html_payload,
    validate_delivery_paths,
    validate_delivery_receipt_payload,
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

    def test_every_deterministic_json_rejects_trailing_whitespace(self) -> None:
        for relative in CANONICAL_JSON_FILES:
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                path = root / relative
                path.parent.mkdir(parents=True)
                path.write_bytes((ROOT / relative).read_bytes() + b" ")
                with self.assertRaisesRegex(ValueError, "canonical JSON"):
                    load_canonical_json(root, relative)

    def test_required_json_rejects_external_symlink_and_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            external = base / "external"
            external.mkdir()
            target = external / "config.json"
            target.write_bytes((ROOT / "config/v5_2_qd.json").read_bytes())

            direct_root = base / "direct"
            (direct_root / "config").mkdir(parents=True)
            (direct_root / "config/v5_2_qd.json").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink|regular file"):
                load_canonical_json(direct_root, "config/v5_2_qd.json")

            ancestor_root = base / "ancestor"
            ancestor_root.mkdir()
            (ancestor_root / "config").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink|ancestor"):
                load_canonical_json(ancestor_root, "config/config.json")

            hardlink_root = base / "hardlink"
            (hardlink_root / "config").mkdir(parents=True)
            os.link(target, hardlink_root / "config/v5_2_qd.json")
            with self.assertRaisesRegex(ValueError, "unalias|regular file"):
                load_canonical_json(hardlink_root, "config/v5_2_qd.json")

    def test_namespace_scan_rejects_broken_symlink_special_and_hardlink(self) -> None:
        for kind in ("broken_symlink", "fifo", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                namespace = root / "audit/v5_2_qd/historical"
                namespace.parent.mkdir(parents=True)
                if kind == "broken_symlink":
                    namespace.symlink_to(root / "missing", target_is_directory=True)
                else:
                    namespace.mkdir()
                    path = namespace / "unexpected"
                    if kind == "fifo":
                        os.mkfifo(path)
                        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))
                    else:
                        source = root / "source"
                        source.write_text("x", encoding="utf-8")
                        os.link(source, path)
                with self.assertRaisesRegex(
                    ValueError, "namespace.*symlink|special|hardlink|unexpected"
                ):
                    historical_execution_status(root)

    def test_exit_integrity_recheck_uses_mock_without_touching_sealed_files(
        self,
    ) -> None:
        import mlb_backtest.verify_v52_design as verifier_module

        def sealed_metadata() -> dict[str, tuple[int, ...]]:
            paths = [ROOT / "config/v5_1_bt.json"]
            for relative in ("data/v5_1_bt", "audit/v5_1_bt"):
                paths.extend((ROOT / relative).rglob("*"))
            return {
                path.relative_to(ROOT).as_posix(): (
                    metadata.st_mode,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_nlink,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
                for path in paths
                if (metadata := path.lstat())
            }

        inventory_path = ROOT / "protocols/v5_2_qd/v51_protected_inventory.json"
        entry, evidence = verifier_module.capture_v51_integrity(ROOT, inventory_path)
        exit_fingerprint = json.loads(json.dumps(entry))
        exit_fingerprint["predictions_content_sha256"] = "0" * 64
        before = sealed_metadata()
        with (
            patch(
                "mlb_backtest.verify_v52_design.capture_v51_integrity",
                side_effect=((entry, evidence), (exit_fingerprint, evidence)),
            ),
            self.assertRaisesRegex(ValueError, "v5.1 integrity"),
        ):
            verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(sealed_metadata(), before)

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

    def test_delivery_paths_are_exact_constants(self) -> None:
        html, receipt = validate_delivery_paths(ROOT, FINAL_HTML_PATH.parent)
        self.assertEqual(html, FINAL_HTML_PATH)
        self.assertEqual(receipt, ROOT / DELIVERY_RECEIPT_RELATIVE)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "output path mismatch"):
                validate_delivery_paths(ROOT, Path(directory))

    def test_delivery_receipt_requires_full_installed_contract_shape(self) -> None:
        artifact = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json").read_text(
                encoding="utf-8"
            )
        )
        good = {
            "ok": True,
            "html": str(FINAL_HTML_PATH),
            "stages": {
                "validation": "passed",
                "package": "passed",
                "verification": "passed",
            },
            "counts": {
                "blocks": 17,
                "charts": 4,
                "html": 0,
                "metrics": 0,
                "tables": 2,
            },
            "sourceDialog": "passed",
            "sourceInteraction": "semantic_click",
            "viewports": [1440, 390],
            "timings": {
                "validateAndPackageMs": 1.0,
                "staticChartExtractionMs": 1.0,
                "browserRunMs": 1.0,
                "readerReadyMs": 1.0,
                "contentChecksMs": 1.0,
                "sourceDialogMs": 1.0,
                "responsiveChecksMs": 1.0,
                "verificationMs": 2.0,
                "totalMs": 4.0,
            },
        }
        installed_order = json.dumps(good, separators=(",", ":")).encode() + b"\n"
        sorted_order = canonical_json_bytes(good) + b"\n"
        self.assertNotEqual(installed_order, sorted_order)
        for encoded in (installed_order, installed_order[:-1], sorted_order):
            with self.subTest(encoded=encoded[:20]):
                self.assertEqual(
                    validate_delivery_receipt_payload(encoded, artifact), good
                )
        cases = {
            "minimal": {"ok": True, "stages": good["stages"]},
            "extra": {**good, "artifact_sha256": "0" * 64},
            "cross_run_path": {**good, "html": "/tmp/old-report.html"},
            "wrong_counts": {**good, "counts": {**good["counts"], "charts": 3}},
            "wrong_stage": {
                **good,
                "stages": {**good["stages"], "verification": "structural_only"},
            },
            "wrong_viewports": {**good, "viewports": [1280, 390]},
        }
        for name, payload in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "delivery receipt"),
            ):
                validate_delivery_receipt_payload(
                    canonical_json_bytes(payload) + b"\n", artifact
                )
        for suffix in (b" ", b"\n\n", b"{}", b"\ntrailing"):
            with (
                self.subTest(suffix=suffix),
                self.assertRaisesRegex(ValueError, "delivery receipt"),
            ):
                validate_delivery_receipt_payload(installed_order + suffix, artifact)

    def test_html_verifier_uses_direct_node_and_parses_final_json_line(self) -> None:
        artifact = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json").read_text()
        )
        counts = {
            "blocks": 17,
            "charts": 4,
            "html": 0,
            "metrics": 0,
            "tables": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory).resolve()
            root = temporary_root / "repo"
            output = temporary_root / "outputs"
            artifact_path = root / "reports/v5_1_bt/diagnostic_100_artifact.json"
            receipt_path = root / DELIVERY_RECEIPT_RELATIVE
            html_path = output / "mlb_v5_1_100_pick_diagnostic.html"
            artifact_path.parent.mkdir(parents=True)
            output.mkdir()
            artifact_path.write_bytes(canonical_json_bytes(artifact) + b"\n")
            html_path.write_text("<!doctype html>", encoding="utf-8")
            receipt = {
                "ok": True,
                "html": str(html_path),
                "stages": {
                    "validation": "passed",
                    "package": "passed",
                    "verification": "passed",
                },
                "counts": counts,
                "sourceDialog": "passed",
                "sourceInteraction": "semantic_click",
                "viewports": [1440, 390],
                "timings": {
                    "validateAndPackageMs": 1,
                    "staticChartExtractionMs": 1,
                    "browserRunMs": 1,
                    "readerReadyMs": 1,
                    "contentChecksMs": 1,
                    "sourceDialogMs": 1,
                    "responsiveChecksMs": 1,
                    "verificationMs": 1,
                    "totalMs": 2,
                },
            }
            receipt_path.write_bytes(
                json.dumps(receipt, separators=(",", ":")).encode() + b"\n"
            )
            verifier = {
                "ok": True,
                "counts": counts,
                "html": str(html_path),
                "sourceDialog": "passed",
                "sourceInteraction": "semantic_click",
                "timings": {
                    "browserRunMs": 1,
                    "readerReadyMs": 1,
                    "contentChecksMs": 1,
                    "sourceDialogMs": 1,
                    "responsiveChecksMs": 1,
                    "totalMs": 2,
                },
                "viewports": [1440, 390],
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="npm notice update available\n"
                + json.dumps(verifier, separators=(",", ":"))
                + "\n",
                stderr="",
            )
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                result = verify_html_payload(html_path, artifact_path, receipt_path)
            self.assertEqual(result["sha256"], sha256_file(html_path))
            command = run.call_args.args[0]
            self.assertEqual(command[0], "node")
            self.assertTrue(command[1].endswith("verify_portable_artifact.mjs"))
            self.assertNotIn("npm", command)

            forged = {**verifier, "unexpected": True}
            completed.stdout = json.dumps(forged) + "\n"
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(ValueError, "portable HTML verification"),
            ):
                verify_html_payload(html_path, artifact_path, receipt_path)

            forged_counts = {
                **verifier,
                "counts": {**counts, "metrics": False},
            }
            completed.stdout = json.dumps(forged_counts) + "\n"
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(ValueError, "portable HTML verification"),
            ):
                verify_html_payload(html_path, artifact_path, receipt_path)

    def test_fitted_model_card_scan_rejects_broken_symlink_and_fifo(self) -> None:
        for kind in ("symlink", "broken_symlink", "fifo", "socket", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                base = root / "models/v5_2_qd/folds"
                base.mkdir(parents=True)
                unexpected = base / "unexpected"
                if kind in {"symlink", "hardlink"}:
                    source = root / "source"
                    source.write_text("x", encoding="utf-8")
                    if kind == "symlink":
                        unexpected.symlink_to(source)
                    else:
                        os.link(source, unexpected)
                elif kind == "broken_symlink":
                    unexpected.symlink_to(root / "missing")
                elif kind == "fifo":
                    os.mkfifo(unexpected)
                else:
                    unix_socket = socket.socket(socket.AF_UNIX)
                    self.addCleanup(unix_socket.close)
                    unix_socket.bind(str(unexpected))
                with self.assertRaisesRegex(
                    ValueError, "namespace.*symlink|special|hardlink|unexpected"
                ):
                    fitted_model_card_count(root)

    def test_verifier_uses_no_network_or_python_write_api(self) -> None:
        # Regression defense only: production authority is the verifier's
        # lstat/no-follow input validation and entry/exit integrity recheck.
        before = protected_inventory(ROOT)
        real_builtin_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_popen = subprocess.Popen
        real_subprocess_run = subprocess.run

        def validate_git_invocation(args, cwd, environment):
            allowed_prefixes = {
                "ls-tree": ["git", "ls-tree", "-r", "-z", "--full-tree"],
                "ls-files": ["git", "ls-files", "-z", "--"],
                "hash-object": ["git", "hash-object", "--"],
                "status": [
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--",
                ],
                "rev-parse": ["git", "rev-parse"],
                "cat-file": ["git", "cat-file", "blob"],
            }
            if not isinstance(args, list) or len(args) < 2:
                raise AssertionError(f"subprocess forbidden: {args!r}")
            prefix = allowed_prefixes.get(args[1])
            if prefix is None or args[: len(prefix)] != prefix or "-w" in args:
                raise AssertionError(f"subprocess argv differs: {args!r}")
            if cwd != ROOT:
                raise AssertionError(f"subprocess cwd differs: {cwd!r}")
            expected_environment = os.environ.copy()
            expected_environment["GIT_OPTIONAL_LOCKS"] = "0"
            if environment != expected_environment:
                raise AssertionError("git environment differs")

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
            validate_git_invocation(args, kwargs.get("cwd"), kwargs.get("env"))
            if set(kwargs) != {"cwd", "check", "capture_output", "env"}:
                raise AssertionError(f"subprocess kwargs differ: {kwargs!r}")
            if kwargs["check"] is not False or kwargs["capture_output"] is not True:
                raise AssertionError("subprocess execution mode differs")
            return real_subprocess_run(args, *positional, **kwargs)

        def guarded_popen(args, *positional, **kwargs):
            validate_git_invocation(args, kwargs.get("cwd"), kwargs.get("env"))
            return real_popen(args, *positional, **kwargs)

        patches = (
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
            *(
                patch(name, side_effect=AssertionError("mutation forbidden"))
                for name in (
                    "pathlib.Path.write_bytes",
                    "pathlib.Path.write_text",
                    "pathlib.Path.rename",
                    "pathlib.Path.replace",
                    "pathlib.Path.unlink",
                    "pathlib.Path.mkdir",
                    "pathlib.Path.touch",
                    "pathlib.Path.symlink_to",
                    "pathlib.Path.hardlink_to",
                    "pathlib.Path.chmod",
                    "os.replace",
                    "os.rename",
                    "os.remove",
                    "os.unlink",
                    "os.link",
                    "os.symlink",
                    "os.chmod",
                    "os.fchmod",
                    "os.truncate",
                    "os.ftruncate",
                    "os.system",
                )
            ),
            patch("subprocess.Popen", side_effect=guarded_popen),
            patch(
                "subprocess.check_output",
                side_effect=AssertionError("check_output forbidden"),
            ),
            patch(
                "subprocess.check_call",
                side_effect=AssertionError("check_call forbidden"),
            ),
            patch(
                "mlb_backtest.verify_v52_design.subprocess.run",
                side_effect=guarded_subprocess_run,
            ),
        )
        with ExitStack() as stack:
            for guard in patches:
                stack.enter_context(guard)
            __import__("mlb_backtest.verify_v52_design")
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
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "output path mismatch"),
        ):
            main(["--root", str(ROOT), "--outputs", directory])

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
