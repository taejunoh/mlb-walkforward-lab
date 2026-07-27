from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import inspect
import io
import json
import os
import ast
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from mlb_backtest import v52_source_finalize as cli
from mlb_backtest import v52_source_orchestration as orchestration
from mlb_backtest.execution_registry import AcquisitionCheckpoint, ExecutionRegistryState
from mlb_backtest.protocols import SourceRunSpec, load_source_run_spec
from mlb_backtest.v52_snapshot_store import (
    AuthorityBinding,
    LoadedSnapshotStore,
    build_inventory,
    inventory_bytes,
    publish_snapshot_store,
)
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from mlb_backtest.v52_verify_sources import DuplicateGamePkDispositionBinding
from mlb_backtest.v52_verify_sources import snapshot_sort_key
from tests.v52_snapshot_store_fixture import fake_81, patched_store_replay, store_inputs


ROOT = Path(__file__).resolve().parents[1]

_LOCAL_DISPOSITION_SCHEMA_RELATIVES = (
    Path("schemas/v5_2_clean_source_replay_receipt.schema.json"),
    Path("schemas/v5_2_derived_disposition_sidecar.schema.json"),
    Path("schemas/v5_2_game_evidence_inventory.schema.json"),
    Path("schemas/v5_2_source_inventory.schema.json"),
    Path("schemas/v5_2_source_receipt_commit.schema.json"),
    Path("schemas/v5_2_source_review_receipt.schema.json"),
)
_LOCAL_DISPOSITION_CONTRACT_SHA256 = (
    "48e883db5276abe3a34fb5bf805ad8ab68f31245798398e3bd12a6dd727aeada"
)


def _commit_fixture_changes(root: Path, message: str) -> None:
    """Commit fixture-only changes without inheriting user signing or hooks."""
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "MLB Fixture",
            "GIT_AUTHOR_EMAIL": "mlb-fixture@example.test",
            "GIT_COMMITTER_NAME": "MLB Fixture",
            "GIT_COMMITTER_EMAIL": "mlb-fixture@example.test",
        }
    )
    subprocess.run(
        (
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-qm",
            message,
        ),
        cwd=root,
        check=True,
        env=environment,
    )


def rebind_source_authority_fixture(root: Path) -> None:
    """Bind copied source authority files to the temporary repository head."""
    source_contract = root / (
        "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
    )
    contract = json.loads(source_contract.read_bytes())
    plan_binding = contract["bindings"]["implementation_plan"]
    plan_path = root / plan_binding["path"]
    plan_binding["working_tree_sha256"] = hashlib.sha256(
        plan_path.read_bytes()
    ).hexdigest()
    plan_binding["git_blob_id"] = subprocess.run(
        ("git", "hash-object", str(plan_path)),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_contract_bytes = canonical_json_bytes(contract) + b"\n"
    schema_path = root / "schemas/v5_2_acquisition_contract.schema.json"
    schema = json.loads(schema_path.read_bytes())
    schema["oneOf"][1]["const"] = contract
    schema_bytes = canonical_json_bytes(schema) + b"\n"
    changed_paths: list[Path] = []
    for path, data in (
        (source_contract, source_contract_bytes),
        (schema_path, schema_bytes),
    ):
        if path.read_bytes() == data:
            continue
        path.write_bytes(data)
        changed_paths.append(path)
    if not changed_paths:
        return
    subprocess.run(
        (
            "git",
            "add",
            *(str(path.relative_to(root)) for path in changed_paths),
        ),
        cwd=root,
        check=True,
    )
    _commit_fixture_changes(root, "bind fixture authority")

def inventory_with_different_run_spec_binding(inventory: object) -> object:
    """Keep fixture run identity valid while changing authenticated authority."""
    bindings = dataclasses.replace(
        inventory.bindings,  # type: ignore[attr-defined]
        run_spec=dataclasses.replace(
            inventory.bindings.run_spec,  # type: ignore[attr-defined]
            sha256="0" * 64,
        ),
    )
    return dataclasses.replace(inventory, bindings=bindings)

def _replace_fixture_disposition_constants(
    value: object, replacements: dict[str, str]
) -> object:
    """Rebind copied source schemas to a local disposition fixture."""
    if isinstance(value, dict):
        return {
            key: _replace_fixture_disposition_constants(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_fixture_disposition_constants(item, replacements) for item in value
        ]
    if isinstance(value, str):
        return replacements.get(value, value)
    return value




class CliTests(unittest.TestCase):
    def test_rebind_source_authority_fixture_is_idempotent(self) -> None:
        """A second bind on an already-bound fixture must be a no-op."""
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.addCleanup(fixture.tearDown)
            root, _run_spec = fixture.make_source_repo(Path(directory))

            rebind_source_authority_fixture(root)
            first_head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            first_tree = tree_snapshot(root)

            rebind_source_authority_fixture(root)
            second_head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(second_head, first_head)
            self.assertEqual(tree_snapshot(root), first_tree)

    def test_fixture_commit_ignores_repository_hooks(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.addCleanup(fixture.tearDown)
            root, _run_spec = fixture.make_source_repo(Path(directory))
            hooks = root / ".hooks"
            hooks.mkdir()
            sentinel = root / "hook-ran.txt"
            post_commit = hooks / "post-commit"
            post_commit.write_text(f"#!/bin/sh\nprintf hook-ran > {sentinel}\n")
            post_commit.chmod(0o755)
            subprocess.run(
                ("git", "config", "core.hooksPath", ".hooks"),
                cwd=root,
                check=True,
            )
            marker = root / "fixture-marker.txt"
            marker.write_text("marker\n")
            subprocess.run(("git", "add", marker.name), cwd=root, check=True)

            _commit_fixture_changes(root, "hook isolation fixture")

            self.assertFalse(sentinel.exists())
            self.assertEqual(marker.read_text(), "marker\n")

    def test_superseding_cli_without_registry_is_absent_not_task6_blocked(self) -> None:
        from mlb_backtest.v52_source_orchestration import CommonArgs, build_context
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.addCleanup(fixture.tearDown)
            root, _run_spec = fixture.make_source_repo(Path(directory))
            rebind_source_authority_fixture(root)
            source_run = root / (
                "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
            )
            source_contract = root / (
                "protocols/v5_2_qd/"
                "historical_source_acquisition_superseding_565534.json"
            )

            before = tree_snapshot(root)
            with (
                patch.object(
                    orchestration,
                    "verify_protected_raw_inventory",
                    return_value=object(),
                ),
                patch.object(
                    orchestration,
                    "snapshot_protected_inventory_membership",
                    return_value=(),
                ),
            ):
                context = build_context(
                    CommonArgs(
                        root=root,
                        run_spec=source_run,
                        acquisition_contract=source_contract,
                        protected_inventory=root
                        / "audit/v5_1_bt/protected_raw_inventory.json",
                    )
                )
            self.assertEqual(context.phase, "ABSENT")
            self.assertEqual(
                tuple(blocker.code for blocker in context.blockers),
                ("REGISTRY_ABSENT",),
            )
            self.assertEqual(tree_snapshot(root), before)

    def _run_json(self, argv: list[str]) -> tuple[int, dict[str, object], str]:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout, patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            code = cli.main([*argv, "--json"])
        self.assertEqual(stderr.getvalue(), "")
        value = json.loads(stdout.getvalue())
        self.assertEqual(stdout.getvalue(), cli.canonical_json(value) + "\n")
        return code, value, stdout.getvalue()

    def test_help_lists_all_stages(self) -> None:
        with self.assertRaises(SystemExit) as caught, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        for command in ("status", "preflight", "publish", "review", "replay", "advance"):
            self.assertIn(command, stdout.getvalue())

    def test_json_failure_is_one_canonical_stdout_object(self) -> None:
        with patch.object(
            cli,
            "build_context",
            side_effect=orchestration.FinalizationError("AUTHORITY_MISSING", "missing", ("x",), 3),
        ), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = cli.main(["status", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(stderr.getvalue(), "")
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["status"], "ERROR")
        self.assertEqual(stdout.getvalue(), cli.canonical_json(value) + "\n")

    def test_json_success_serializes_dataclass_paths(self) -> None:
        report = orchestration.StatusReport("ACQUIRING", None, True, False, False, False, ())
        context = orchestration.FinalizationContext.absent(Path("."), ())
        with patch.object(cli, "build_context", return_value=context), patch.object(cli, "read_status", return_value=report), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(cli.main(["status", "--json"]), 0)
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["status"], "OK")
        self.assertEqual(value["phase"], "ACQUIRING")
        self.assertEqual(stdout.getvalue(), cli.canonical_json(value) + "\n")

    def test_parser_requires_write_commit_for_review_and_replay(self) -> None:
        for command, extra in (("review", ["--reviewer-identity", "i", "--reviewer-role", "r", "--decision", "APPROVED", "--query-ids-file", "q", "--write-receipt", "r"]), ("replay", ["--write-receipt", "r"])):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
                code = cli.main([command, "--json", *extra])
            self.assertEqual(code, 2)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(json.loads(stdout.getvalue())["status"], "ERROR")

    def test_conflicting_single_value_option_is_usage_error(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            code = cli.main(["status", "--json", "--root", "a", "--root", "b"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["error_code"], "USAGE_ERROR")

    def test_common_defaults_and_arguments_follow_subcommand(self) -> None:
        namespace = cli._parser().parse_args(
            ["status", "--root", "r", "--run-spec", "s", "--acquisition-contract", "c", "--protected-inventory", "p"]
        )
        self.assertEqual(namespace.root, Path("r"))
        self.assertEqual(namespace.run_spec, Path("s"))
        self.assertEqual(namespace.acquisition_contract, Path("c"))
        self.assertEqual(namespace.protected_inventory, Path("p"))
        defaults = cli._parser().parse_args(["status"])
        self.assertEqual(defaults.root, Path("."))
        self.assertEqual(defaults.run_spec, cli.DEFAULT_RUN_SPEC)
        self.assertEqual(defaults.acquisition_contract, cli.DEFAULT_CONTRACT)
        self.assertEqual(defaults.protected_inventory, cli.DEFAULT_PROTECTED)

    def test_each_subcommand_dispatches_public_service_request(self) -> None:
        context = orchestration.FinalizationContext.absent(Path("."), ())
        with patch.object(cli, "build_context", return_value=context), patch.object(
            cli, "read_status", return_value=orchestration.StatusReport("ABSENT", None, False, False, False, False, ())
        ) as status:
            self.assertEqual(cli.main(["status", "--quiet"]), 0)
            status.assert_called_once_with(context)
        with patch.object(cli, "build_context", return_value=context), patch.object(
            cli, "run_preflight", return_value=orchestration.PreflightReport("ABSENT", "BLOCKED", None, 0, 0, ())
        ) as preflight:
            self.assertEqual(cli.main(["preflight", "--quiet"]), 0)
            preflight.assert_called_once_with(context)
        publish_report = orchestration.PublishReport("PUBLISHED", "a" * 64, 1)
        with patch.object(cli, "build_context", return_value=context), patch.object(
            cli, "publish_inventory", return_value=publish_report
        ) as publish:
            self.assertEqual(cli.main(["publish", "--lock-owner", "owner", "--quiet"]), 0)
            publish.assert_called_once_with(context, lock_owner="owner")
        review_report = orchestration.ReviewReport(Path("review.json"), "a" * 64, "SOURCE_REVIEW", Path("review.commit.json"), "b" * 64)
        with patch.object(cli, "build_context", return_value=context), patch.object(
            cli, "write_review", return_value=review_report
        ) as review:
            self.assertEqual(cli.main(["review", "--reviewer-identity", "id", "--reviewer-role", "role", "--decision", "APPROVED", "--query-ids-file", "query.json", "--write-receipt", "review.json", "--write-commit", "review.commit.json", "--quiet"]), 0)
            request = review.call_args.args[1]
            self.assertEqual(request, orchestration.ReviewRequest("id", "role", "APPROVED", Path("query.json"), Path("review.json"), Path("review.commit.json")))
        replay_report = orchestration.ReplayReport(Path("replay.json"), "a" * 64, "CLEAN_SOURCE_REPLAY", Path("replay.commit.json"), "b" * 64)
        with patch.object(cli, "build_context", return_value=context), patch.object(
            cli, "write_replay", return_value=replay_report
        ) as replay:
            self.assertEqual(cli.main(["replay", "--write-receipt", "replay.json", "--write-commit", "replay.commit.json", "--quiet"]), 0)
            self.assertEqual(replay.call_args.args[1], orchestration.ReplayRequest(Path("replay.json"), Path("replay.commit.json")))
        advance_report = orchestration.AdvanceReport("ADVANCED", "SOURCES_READY", "a" * 64)
        with patch.object(cli, "build_context", return_value=context), patch.object(
            cli, "advance_sources_ready", return_value=advance_report
        ) as advance:
            self.assertEqual(cli.main(["advance", "--review-receipt", "review.commit.json", "--replay-receipt", "replay-one.commit.json", "--replay-receipt", "replay-two.commit.json", "--checkpoint", "checkpoint.json", "--inventory", "inventory.json", "--quiet"]), 0)
            self.assertEqual(advance.call_args.args[1], orchestration.AdvanceRequest((Path("review.commit.json"),), (Path("replay-one.commit.json"), Path("replay-two.commit.json")), Path("checkpoint.json"), Path("inventory.json")))

    def test_all_single_value_options_allow_same_and_reject_conflict(self) -> None:
        for option in cli._SINGLE_VALUE_OPTIONS:
            cli._reject_conflicting_options([option, "same", option, "same"])
            with self.assertRaises(cli.CliUsageError):
                cli._reject_conflicting_options([option, "one", option, "two"])

    def test_advance_requires_at_least_one_review_and_exactly_two_replays(self) -> None:
        context = orchestration.FinalizationContext.absent(Path("."), ())
        base = ["advance", "--checkpoint", "c", "--inventory", "i", "--review-receipt", "review.commit.json"]
        for replay_values in (("one.commit.json",), ("one.commit.json", "two.commit.json", "three.commit.json")):
            argv = [*base]
            for value in replay_values:
                argv.extend(("--replay-receipt", value))
            with patch.object(cli, "build_context", return_value=context), patch.object(cli, "advance_sources_ready") as advance:
                code, value, _ = self._run_json(argv)
            self.assertEqual(code, 2)
            self.assertEqual(value["error_code"], "USAGE_ERROR")
            advance.assert_not_called()

    def test_advance_multiplicity_precedes_context_construction(self) -> None:
        missing = Path("does-not-exist")
        argv = [
            "advance",
            "--root",
            str(missing),
            "--review-receipt",
            "review.commit.json",
            "--replay-receipt",
            "only-one.commit.json",
            "--checkpoint",
            "checkpoint.json",
            "--inventory",
            "inventory.json",
        ]
        with patch.object(cli, "build_context") as build_context:
            code, value, _ = self._run_json(argv)
        self.assertEqual(code, 2)
        self.assertEqual(value["error_code"], "USAGE_ERROR")
        build_context.assert_not_called()

    def test_context_construction_failures_are_stable_authority_errors(self) -> None:
        for failure in (ValueError("invalid root"), PermissionError("denied")):
            with self.subTest(failure=type(failure).__name__), patch.object(
                cli, "build_context", side_effect=failure
            ):
                code, value, _ = self._run_json(
                    ["status", "--root", "unavailable-root"]
                )
            self.assertEqual(code, 3)
            self.assertEqual(value["error_code"], "AUTHORITY_MALFORMED")
            self.assertEqual(
                value["message"], "finalization authority is unavailable or unsafe"
            )
            self.assertEqual(value["paths"], ["unavailable-root"])

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            code, value, _ = self._run_json(
                ["status", "--root", str(missing)]
            )
        self.assertEqual(code, 3)
        self.assertEqual(value["error_code"], "AUTHORITY_MALFORMED")
        self.assertEqual(value["paths"], [str(missing)])

    def test_context_construction_human_error_has_no_traceback_or_exit_one(self) -> None:
        with patch.object(
            cli, "build_context", side_effect=PermissionError("denied")
        ), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch(
            "sys.stderr", new_callable=io.StringIO
        ) as stderr:
            code = cli.main(["status", "--root", "inaccessible", "--quiet"])
        self.assertEqual(code, 3)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("finalization authority is unavailable or unsafe", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_service_error_exit_codes_three_through_seven_are_preserved(self) -> None:
        for exit_code in range(3, 8):
            failure = orchestration.FinalizationError(
                f"SERVICE_{exit_code}", f"failure {exit_code}", ("x",), exit_code
            )
            with self.subTest(exit_code=exit_code), patch.object(
                cli, "build_context", side_effect=failure
            ):
                code, value, _ = self._run_json(["status"])
            self.assertEqual(code, exit_code)
            self.assertEqual(value["error_code"], f"SERVICE_{exit_code}")
            self.assertEqual(value["paths"], ["x"])

            with patch.object(
                cli, "build_context", side_effect=failure
            ), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch(
                "sys.stderr", new_callable=io.StringIO
            ) as stderr:
                human_code = cli.main(["status", "--quiet"])
            self.assertEqual(human_code, exit_code)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), f"error: failure {exit_code}\n")

    def test_json_usage_service_and_keyboard_errors_have_stable_streams(self) -> None:
        code, value, output = self._run_json(["status", "--unknown"])
        self.assertEqual(code, 2)
        self.assertEqual(value["status"], "ERROR")
        self.assertEqual(output.count("\n"), 1)
        error = orchestration.FinalizationError("AUTHORITY_MISSING", "missing", ("x",), 3)
        with patch.object(cli, "build_context", side_effect=error):
            code, value, _ = self._run_json(["status"])
        self.assertEqual(code, 3)
        self.assertEqual(value["paths"], ["x"])
        with patch.object(cli, "build_context", side_effect=KeyboardInterrupt), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(cli.main(["status", "--json"]), 130)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_human_errors_use_stderr_and_quiet_suppresses_success_only(self) -> None:
        with patch.object(cli, "build_context", side_effect=orchestration.FinalizationError("AUTHORITY_MISSING", "missing", ("x",), 3)), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(cli.main(["status", "--quiet"]), 3)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("error: missing", stderr.getvalue())
        context = orchestration.FinalizationContext.absent(Path("."), ())
        report = orchestration.StatusReport("ABSENT", None, False, False, False, False, ())
        with patch.object(cli, "build_context", return_value=context), patch.object(cli, "read_status", return_value=report), patch("sys.stdout", new_callable=io.StringIO) as stdout, patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(cli.main(["status", "--quiet"]), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_human_success_includes_every_result_field_for_all_commands(self) -> None:
        cases = (
            (
                "status",
                "ABSENT",
                orchestration.StatusReport(
                    "ABSENT", None, False, False, False, False,
                    (orchestration.Blocker("B", "blocked", ("x",)),),
                ),
            ),
            (
                "preflight",
                "ACQUIRING",
                orchestration.PreflightReport(
                    "ACQUIRING", "PASS", "a" * 64, 6, 0, ()
                ),
            ),
            (
                "publish",
                "ACQUIRING",
                orchestration.PublishReport("PUBLISHED", "b" * 64, 6),
            ),
            (
                "review",
                "ACQUIRING",
                orchestration.ReviewReport(
                    Path("review.json"), "c" * 64, "SOURCE_REVIEW",
                    Path("review.commit.json"), "d" * 64,
                ),
            ),
            (
                "replay",
                "ACQUIRING",
                orchestration.ReplayReport(
                    Path("replay.json"), "e" * 64, "CLEAN_SOURCE_REPLAY",
                    Path("replay.commit.json"), "f" * 64,
                ),
            ),
            (
                "advance",
                "SOURCES_READY",
                orchestration.AdvanceReport("ADVANCED", "SOURCES_READY", "1" * 64),
            ),
        )
        for command, phase, report in cases:
            with self.subTest(command=command), patch(
                "sys.stdout", new_callable=io.StringIO
            ) as stdout:
                cli._emit_success(
                    command, phase, report, machine=False, quiet=False
                )
            self.assertEqual(
                stdout.getvalue(),
                f"schema_version={cli.SCHEMA_VERSION} command={command} "
                f"status=OK phase={phase} "
                f"result={cli.canonical_json(report)}\n",
            )

    def test_unordered_json_containers_are_stable_across_hash_seeds(self) -> None:
        script = (
            "from pathlib import Path; "
            "from mlb_backtest.v52_source_finalize import canonical_json; "
            "value={'mixed': {Path('p'), 's', 7, "
            "frozenset({('b', 2), ('a', 1)})}}; "
            "print(canonical_json(value))"
        )
        outputs = []
        for seed in ("1", "2", "3", "4", "5", "6", "7", "8"):
            environment = dict(os.environ)
            environment["PYTHONHASHSEED"] = seed
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            outputs.append(completed.stdout)
        self.assertEqual(len(set(outputs)), 1)
        parsed = json.loads(outputs[0])
        self.assertCountEqual(parsed["mixed"], ["p", "s", 7, [["a", 1], ["b", 2]]])
        with self.assertRaises(ValueError):
            cli.canonical_json({float("nan")})

    def test_recursive_json_safe_rendering_and_forbidden_imports(self) -> None:
        payload = {"path": Path("x"), "nested": (Path("y"), orchestration.Blocker("C", "m"))}
        rendered = cli.canonical_json(payload)
        self.assertEqual(json.loads(rendered)["nested"][0], "y")
        tree = ast.parse(inspect.getsource(cli))
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported |= {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if node.module
        }
        self.assertNotIn("v52_http", imported)
        self.assertNotIn("v52_acquire", imported)


class FinalizationEndToEndTests(unittest.TestCase):
    def test_registry_disposition_copy_seam_is_exact_and_local(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            with self._registry_disposition_copy_seam(root) as (
                copy_fixture_artifact,
                expected_pairs,
                artifact_bytes,
                copied_pairs,
                published_sidecar_pairs,
            ):
                with self.assertRaisesRegex(AssertionError, "unexpected"):
                    copy_fixture_artifact(
                        ROOT / "unrelated.json", root / "unrelated.json"
                    )
                for source, target in expected_pairs:
                    self.assertEqual(copy_fixture_artifact(source, target), target)
                    self.assertEqual(target.read_bytes(), artifact_bytes[target])
            self.assertEqual(copied_pairs, list(expected_pairs))
            self.assertEqual(published_sidecar_pairs, [])
            self._assert_local_disposition_artifacts(root, artifact_bytes)

    def test_local_disposition_schema_rebinding_is_explicitly_bounded(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.addCleanup(fixture.tearDown)
            production_query_id = (
                orchestration.execution_registry._DISPOSITIONED_QUERY_ID
            )
            for name in ("first", "second"):
                root, _run_spec = fixture.make_source_repo(Path(directory) / name)
                rebind_source_authority_fixture(root)
                untouched = root / "schemas/v5_2_evidence_manifest.schema.json"
                self.assertFalse(untouched.exists())
                before = {
                    path.relative_to(root): path.read_bytes()
                    for path in (root / "schemas").glob("*.json")
                }
                authority_paths = (
                    Path(
                        "docs/superpowers/plans/"
                        "2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md"
                    ),
                    Path(
                        "protocols/v5_2_qd/"
                        "historical_source_acquisition_superseding_565534.json"
                    ),
                    Path(
                        "protocols/v5_2_qd/"
                        "historical_walkforward_500_superseding_565534.json"
                    ),
                )
                authority_before = {
                    relative: (root / relative).read_bytes()
                    for relative in authority_paths
                }
                self._prepare_local_disposition_fixture(
                    root, synthetic_schema_identity=True
                )
                query_id, sidecar_sha256, sidecar_path, success_path = (
                    self._fixture_disposition_values
                )
                self.assertNotEqual(
                    query_id,
                    production_query_id,
                )
                self.assertEqual(
                    {
                        relative: (root / relative).read_bytes()
                        for relative in authority_paths
                    },
                    authority_before,
                )
                self.assertEqual(
                    {
                        relative
                        for relative, contents in before.items()
                        if (root / relative).read_bytes() != contents
                    },
                    set(_LOCAL_DISPOSITION_SCHEMA_RELATIVES),
                )
                for relative in _LOCAL_DISPOSITION_SCHEMA_RELATIVES:
                    contents = (root / relative).read_text()
                    self.assertIn(query_id, contents)
                    if sidecar_sha256 in before[relative].decode():
                        self.assertIn(sidecar_sha256, contents)
                    if sidecar_path in before[relative].decode():
                        self.assertIn(sidecar_path, contents)
                    if success_path in before[relative].decode():
                        self.assertIn(success_path, contents)
            self.assertEqual(
                self._production_disposition_values,
                (
                    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095",
                    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1",
                    "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                    "query_sidecars/0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1.json",
                    "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                    "mlb_schedule_game_feed/8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095/"
                    "success.json",
                ),
            )

    @contextmanager
    def _registry_disposition_copy_seam(self, root: Path):
        """Install only the three unavailable legacy artifacts for one fixture.

        This seam is deliberately local to ``test_execution_registry``'s call
        site.  It does not patch ``shutil.copy2`` globally, and delegates the
        later generated-sidecar copy to the original copier after the exact
        three fixture artifacts were installed.
        """
        from mlb_backtest import v52_duplicate_gamepk_disposition as disposition
        from tests import test_execution_registry as registry_tests

        artifact_bytes: dict[Path, bytes] = {}
        expected_pairs: list[tuple[Path, Path]] = []
        for relative, artifact_kind in (
            (disposition.OLD_REGISTRATION_RELATIVE, "query_registration"),
            (disposition.OLD_QUARANTINE_RELATIVE, "quarantine_receipt"),
            (disposition.OLD_RAW_RELATIVE, "quarantined_raw"),
        ):
            source = (registry_tests.SOURCE_AUTHORITY_ROOT / relative).resolve()
            target = (root / relative).resolve()
            contents = (
                canonical_json_bytes(
                    {
                        "artifact_kind": artifact_kind,
                        "disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534",
                        "fixture_kind": "LOCAL_UNAUTHENTICATED_SOURCE_FINALIZE",
                        "relative_path": relative.as_posix(),
                        "source_query_id": disposition.OLD_QUERY_ID,
                    }
                )
                + b"\n"
            )
            expected_pairs.append((source, target))
            artifact_bytes[target] = contents

        copied_pairs: list[tuple[Path, Path]] = []
        published_sidecar_pairs: list[tuple[Path, Path]] = []
        original_shutil = registry_tests.shutil
        real_copy2 = original_shutil.copy2
        call_site_shutil = SimpleNamespace()

        def copy_published_sidecar(
            source: object, target: object, *args: object, **kwargs: object
        ) -> Path:
            try:
                query_id, sidecar_sha256, sidecar_path, _success_path = (
                    self._fixture_disposition_values
                )
            except AttributeError as exc:
                raise AssertionError(
                    "published sidecar copy preceded fixture binding"
                ) from exc
            expected_pair = (
                (
                    root
                    / "data/v5_2_qd/historical_superseding_565534/raw/queries"
                    / "mlb_schedule_game_feed"
                    / query_id
                    / "sidecars"
                    / f"{sidecar_sha256}.json"
                ).resolve(),
                (root / sidecar_path).resolve(),
            )
            pair = (Path(source).resolve(), Path(target).resolve())
            if args or kwargs or pair != expected_pair or published_sidecar_pairs:
                raise AssertionError(f"unexpected published sidecar copy: {pair!r}")
            published_sidecar_pairs.append(pair)
            return real_copy2(source, target)

        def copy_fixture_artifact(
            source: object, target: object, *args: object, **kwargs: object
        ) -> Path:
            source_path = Path(source).resolve()
            target_path = Path(target).resolve()
            pair = (source_path, target_path)
            if args or kwargs or pair not in expected_pairs:
                raise AssertionError(f"unexpected fixture copy pair: {pair!r}")
            if pair in copied_pairs or target_path.exists():
                raise AssertionError(f"duplicate fixture copy pair: {pair!r}")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(artifact_bytes[target_path])
            copied_pairs.append(pair)
            if len(copied_pairs) == len(expected_pairs):
                call_site_shutil.copy2 = copy_published_sidecar
            return target_path

        call_site_shutil.copy2 = copy_fixture_artifact
        with patch.object(registry_tests, "shutil", call_site_shutil):
            yield (
                copy_fixture_artifact,
                tuple(expected_pairs),
                artifact_bytes,
                copied_pairs,
                published_sidecar_pairs,
            )
        self.assertEqual(copied_pairs, expected_pairs)

    def _assert_local_disposition_artifacts(
        self, root: Path, artifact_bytes: dict[Path, bytes]
    ) -> None:
        from mlb_backtest import v52_duplicate_gamepk_disposition as disposition

        expected_relatives = {
            disposition.OLD_REGISTRATION_RELATIVE,
            disposition.OLD_QUARANTINE_RELATIVE,
            disposition.OLD_RAW_RELATIVE,
        }
        self.assertEqual(
            {path.relative_to(root) for path in artifact_bytes}, expected_relatives
        )
        for path, expected in artifact_bytes.items():
            relative = path.relative_to(root)
            node = path.lstat()
            self.assertTrue(stat.S_ISREG(node.st_mode), relative)
            self.assertEqual(node.st_nlink, 1, relative)
            self.assertEqual(path.read_bytes(), expected, relative)
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(),
                hashlib.sha256(expected).hexdigest(),
                relative,
            )
            payload = json.loads(expected)
            self.assertEqual(payload["relative_path"], relative.as_posix())
            self.assertEqual(payload["source_query_id"], disposition.OLD_QUERY_ID)
            self.assertEqual(payload["disposition"], "SCHEDULE_DUPLICATE_GAMEPK_565534")

    def _local_disposition_boundary(self) -> dict[Path, object]:
        """Install the fixture-only replacement for protected raw projection.

        The real authority's exact 21MB forensic authentication is covered in
        protected-evidence schedule tests.  This downstream test retains the
        real evidence-store receipt/sidecar verification and all finalization
        gates, but derives that local evidence from the fixed public predicate.
        """
        existing = getattr(self, "_fixture_disposition_authorities", None)
        if existing is not None:
            return existing

        from mlb_backtest import v52_duplicate_gamepk_disposition as disposition

        authorities: dict[Path, object] = {}
        projection = disposition.ScheduleOccurrenceProjection(
            occurrence_counts=disposition.EXPECTED_OCCURRENCE_COUNTS,
            suppressed_position=disposition.SUPPRESSED_POSITION,
            retained_position=disposition.RETAINED_POSITION,
            normalized_games=(),
            selected_raw_game_object_sha256s=(),
            site_flags=(),
        )
        real_build = disposition.build_duplicate_disposition_authority
        real_recompute = disposition.DuplicateGamePkDispositionAuthority.recompute

        def build_fixture_authority(
            root: Path,
            *,
            source_run_spec_path: Path,
            acquisition_contract_path: Path,
        ) -> object:
            fixture = authorities.get(Path(root).resolve())
            if fixture is None:
                return real_build(
                    root,
                    source_run_spec_path=source_run_spec_path,
                    acquisition_contract_path=acquisition_contract_path,
                )
            if fixture.source_run_spec_path != Path(
                source_run_spec_path
            ) or fixture.acquisition_contract_path != Path(acquisition_contract_path):
                raise ValueError("fixture disposition authority path differs")
            return fixture

        def recompute_fixture_authority(authority: object) -> object:
            fixture = authorities.get(authority.repository_root)
            if fixture == authority:
                return projection
            return real_recompute(authority)

        build_patcher = patch.object(
            disposition,
            "build_duplicate_disposition_authority",
            side_effect=build_fixture_authority,
        )
        recompute_patcher = patch.object(
            disposition.DuplicateGamePkDispositionAuthority,
            "recompute",
            side_effect=recompute_fixture_authority,
            autospec=True,
        )
        build_patcher.start()
        recompute_patcher.start()
        self.addCleanup(build_patcher.stop)
        self.addCleanup(recompute_patcher.stop)
        self._fixture_disposition_authorities = authorities
        return authorities

    def _prepare_local_disposition_fixture(
        self,
        root: Path,
        *,
        synthetic_schema_identity: bool = False,
    ) -> None:
        """Bind temporary schemas and registry constants to local evidence."""
        from mlb_backtest import v52_duplicate_gamepk_disposition as disposition
        from mlb_backtest import v52_query_plan
        from mlb_backtest import v52_snapshot_store, v52_verify_sources
        from mlb_backtest.v52_sources import history
        from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id

        production_values = getattr(
            self, "_production_disposition_values", None
        )
        if production_values is None:
            production_values = (
                orchestration.execution_registry._DISPOSITIONED_QUERY_ID,
                orchestration.execution_registry._SOURCE_DISPOSITION_SIDECAR_SHA256,
                orchestration.execution_registry._SOURCE_DISPOSITION_SIDECAR_PATH,
                orchestration.execution_registry._SOURCE_DISPOSITION_SUCCESS_PATH,
            )
            self._production_disposition_values = production_values
        (
            old_query_id,
            old_sidecar_sha256,
            old_sidecar_path,
            old_success_path,
        ) = production_values
        authorities = self._local_disposition_boundary()
        context = v52_query_plan._load_static_contracts(
            root
            / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            root
            / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        query = LogicalQuery(
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=disposition.DISPOSITION_ENDPOINT,
            parameters={},
            requested_range=disposition._requested_range(),
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256=context.authority_sha256,
            acquisition_contract_sha256=(
                _LOCAL_DISPOSITION_CONTRACT_SHA256
                if synthetic_schema_identity
                else context.acquisition_contract_sha256
            ),
        )
        authority = disposition.DuplicateGamePkDispositionAuthority(
            repository_root=root,
            evidence_root=context.raw_root,
            source_run_spec_path=context.authority_path,
            acquisition_contract_path=context.acquisition_contract_path,
            query=query,
            old_raw_relative=disposition.OLD_RAW_RELATIVE,
            _snapshots=(),
        )
        authorities[root.resolve()] = authority
        semantics = disposition._sidecar_semantics(authority, authority.recompute())
        query_id = logical_query_id(query)
        sidecar = {
            "grain": query.grain,
            "parser_version": query.parser_version,
            "query_id": query_id,
            "schema_version": 1,
            "semantics": semantics,
            "source": query.source,
            "storage_mode": "DERIVED_DISPOSITION",
        }
        sidecar_sha256 = hashlib.sha256(
            canonical_json_bytes(sidecar) + b"\n"
        ).hexdigest()
        sidecar_path = (
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            f"query_sidecars/{sidecar_sha256}.json"
        )
        success_path = (
            "data/v5_2_qd/historical_superseding_565534/raw/queries/"
            f"mlb_schedule_game_feed/{query_id}/success.json"
        )
        replacements = {
            old_query_id: query_id,
            old_sidecar_sha256: sidecar_sha256,
            old_sidecar_path: sidecar_path,
            old_success_path: success_path,
        }
        schema_paths = tuple(
            root / relative for relative in _LOCAL_DISPOSITION_SCHEMA_RELATIVES
        )
        self.assertTrue(all(path.is_file() for path in schema_paths))
        for path in schema_paths:
            schema = json.loads(path.read_bytes())
            original = canonical_json_bytes(schema) + b"\n"
            self.assertTrue(
                old_query_id in original.decode()
                or old_sidecar_sha256 in original.decode(),
                path,
            )
            path.write_bytes(
                canonical_json_bytes(
                    _replace_fixture_disposition_constants(schema, replacements)
                )
                + b"\n"
            )
        subprocess.run(
            ("git", "add", *(str(path.relative_to(root)) for path in schema_paths)),
            cwd=root,
            check=True,
        )
        staged = subprocess.run(
            ("git", "diff", "--cached", "--quiet"),
            cwd=root,
            check=False,
        )
        if staged.returncode not in (0, 1):
            raise RuntimeError("fixture schema staged-diff inspection failed")
        if staged.returncode == 1:
            _commit_fixture_changes(root, "bind local disposition fixture")

        sidecar_bytes = canonical_json_bytes(sidecar) + b"\n"
        self.assertEqual(hashlib.sha256(sidecar_bytes).hexdigest(), sidecar_sha256)
        self.assertEqual(query_id, logical_query_id(authority.query))
        self.assertEqual(authority.query.endpoint, disposition.DISPOSITION_ENDPOINT)
        self.assertEqual(
            authority.query.requested_range["disposition"],
            "SCHEDULE_DUPLICATE_GAMEPK_565534",
        )
        values = (query_id, sidecar_sha256, sidecar_path, success_path)
        prior = getattr(self, "_fixture_disposition_values", None)
        if prior is not None:
            self.assertEqual(values, prior)
            return
        self._fixture_disposition_values = values
        schema_path = root / "schemas/v5_2_source_inventory.schema.json"

        def local_inventory_schema_validator() -> object:
            from jsonschema import Draft202012Validator, FormatChecker

            schema = json.loads(schema_path.read_bytes())
            Draft202012Validator.check_schema(schema)
            return Draft202012Validator(schema, format_checker=FormatChecker())

        patchers = (
            *(
                patch.object(orchestration.execution_registry, attribute, value)
                for attribute, value in (
                    ("_DISPOSITIONED_QUERY_ID", query_id),
                    ("_SOURCE_DISPOSITION_SIDECAR_SHA256", sidecar_sha256),
                    ("_SOURCE_DISPOSITION_SIDECAR_PATH", sidecar_path),
                    ("_SOURCE_DISPOSITION_SUCCESS_PATH", success_path),
                )
            ),
            patch.object(history, "TASK10_DISPOSITION_QUERY_ID", query_id),
            patch.object(history, "TASK10_DISPOSITION_SIDECAR_SHA256", sidecar_sha256),
            patch.object(history, "TASK10_DISPOSITION_SIDECAR_PATH", sidecar_path),
            patch.object(history, "TASK10_DISPOSITION_SUCCESS_PATH", success_path),
            patch.object(v52_snapshot_store, "TASK10_DISPOSITION_QUERY_ID", query_id),
            patch.object(v52_verify_sources, "TASK10_DISPOSITION_QUERY_ID", query_id),
            patch.object(
                v52_verify_sources,
                "TASK10_DISPOSITION_SIDECAR_SHA256",
                sidecar_sha256,
            ),
            patch.object(
                v52_snapshot_store,
                "_inventory_schema_validator",
                side_effect=local_inventory_schema_validator,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)


    """The public services complete the staged source gate in a real Git repo."""

    @staticmethod
    def lexists_snapshot(path: Path) -> tuple[int, int, int, int] | None:
        if not os.path.lexists(path):
            return None
        value = path.lstat()
        return (value.st_dev, value.st_ino, value.st_mode, value.st_size)

    @staticmethod
    def source_tree_snapshot(path: Path) -> tuple[tuple[str, int, int, int, int, str | None], ...] | None:
        if not os.path.lexists(path):
            return None
        snapshots: list[tuple[str, int, int, int, int, str | None]] = []
        pending = [path]
        while pending:
            current = pending.pop()
            metadata = current.lstat()
            relative = "." if current == path else current.relative_to(path).as_posix()
            digest = (
                hashlib.sha256(current.read_bytes()).hexdigest()
                if current.is_file() and not current.is_symlink()
                else None
            )
            snapshots.append(
                (
                    relative,
                    metadata.st_mode,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    digest,
                )
            )
            if current.is_dir() and not current.is_symlink():
                pending.extend(current.iterdir())
        return tuple(sorted(snapshots))

    @staticmethod
    def is_within(path: object, target: Path) -> bool:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        candidate = Path(os.fsdecode(path))
        if not candidate.is_absolute():
            return False
        try:
            candidate.relative_to(target)
        except ValueError:
            return False
        return True

    @staticmethod
    def directory_fd_path(descriptor: int) -> Path | None:
        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is not None:
            try:
                value = fcntl.fcntl(descriptor, getpath, b"\0" * 1024)
                if isinstance(value, bytes):
                    return Path(value.split(b"\0", 1)[0].decode())
            except OSError:
                pass
        try:
            return Path(os.readlink(f"/dev/fd/{descriptor}"))
        except OSError:
            return None

    def build_finalization_fixture(
        self, parent: Path
    ) -> tuple[Path, orchestration.CommonArgs, dict[str, Path], object, tuple[object, ...]]:
        from tests.test_execution_registry import ExecutionRegistryTest

        fixture = ExecutionRegistryTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        root, run_spec = fixture.make_repo(parent, remaining_authority=True)
        fixture.initialize(root, run_spec)
        fixture.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
        fixture.install_terminal_checkpoint_stub(root)
        checkpoint = orchestration.load_source_verification_checkpoint(root, run_spec)
        checkpoint_path = root / orchestration._checkpoint_path(checkpoint)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_contents = canonical_json_bytes(
            {"fixture": "terminal-checkpoint"}
        ) + b"\n"
        self.assertEqual(
            hashlib.sha256(checkpoint_contents).hexdigest(), checkpoint.receipt_sha256
        )
        checkpoint_path.write_bytes(checkpoint_contents)

        def authority(relative: str) -> tuple[Path, str]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = canonical_json_bytes({"fixture_authority": relative}) + b"\n"
            path.write_bytes(contents)
            return path, hashlib.sha256(contents).hexdigest()

        schedule_path, schedule_sha256 = authority(
            "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
        )
        game_path, game_sha256 = authority(
            "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json"
        )
        _seal_path, seal_sha256 = authority(
            "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"
        )
        inputs = store_inputs(current_phase="ACQUIRING")
        def rebind_snapshot_run_id(snapshot: object) -> object:
            payload = snapshot.as_mapping()  # type: ignore[attr-defined]
            payload["run_id"] = run_spec.run_id
            contents = canonical_json_bytes(payload) + b"\n"
            return dataclasses.replace(
                snapshot,
                run_id=run_spec.run_id,
                canonical_bytes=contents,
                sha256=hashlib.sha256(contents).hexdigest(),
            )

        snapshots = tuple(
            rebind_snapshot_run_id(snapshot)
            for snapshot in inputs.replay.game_snapshots
        )
        all_snapshots = tuple(
            sorted(
                (rebind_snapshot_run_id(snapshot) for snapshot in fake_81()),
                key=snapshot_sort_key,
            )
        )
        replay = dataclasses.replace(
            inputs.replay,
            run_id=run_spec.run_id,
            game_snapshots=snapshots,
            schedule_inventory=dataclasses.replace(
                inputs.replay.schedule_inventory,
                path=schedule_path,
                sha256=schedule_sha256,
            ),
            game_inventory=dataclasses.replace(
                inputs.replay.game_inventory,
                path=game_path,
                sha256=game_sha256,
            ),
            timezone_gate=dataclasses.replace(
                inputs.replay.timezone_gate,
                seal_sha256=seal_sha256,
            ),
        )
        receipts = root / "audit/v5_2_qd/historical/acquisition"
        request_bundle = {
            "query_document": receipts / "source_reviews/query_ids.json",
            "review_receipt": receipts / "source_reviews/e2e-review.json",
            "review_commit": receipts / "source_reviews/e2e-review.commit.json",
            "replay_one_receipt": receipts / "source_replays/e2e-one.json",
            "replay_one_commit": receipts / "source_replays/e2e-one.commit.json",
            "replay_two_receipt": receipts / "source_replays/e2e-two.json",
            "replay_two_commit": receipts / "source_replays/e2e-two.commit.json",
            "checkpoint": checkpoint_path,
            "inventory": root / "data/v5_2_qd/historical/sources/source_inventory.json",
        }
        return root, orchestration.CommonArgs(root=root), request_bundle, replay, all_snapshots

    def build_source_terminal_fixture(
        self, parent: Path, *, with_disposition: bool = True
    ) -> tuple[Path, orchestration.CommonArgs, object]:
        """Create a bounded superseding source fixture for Task 9 gates.

        The fixture copies the exact SourceRunSpec/contract into a temporary
        Git repository, publishes a real five-stage append-only checkpoint
        chain through the public registry writer, and injects the
        already-verified 81-snapshot replay inputs.  No network work is
        performed here.
        """
        from tests.test_execution_registry import ExecutionRegistryTest

        fixture = ExecutionRegistryTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        root, run_spec = fixture.make_source_repo(parent)
        rebind_source_authority_fixture(root)
        self._prepare_local_disposition_fixture(root)
        fixture.registry.initialize_execution_registry(
            root,
            run_spec,
            design_path=root
            / "docs/superpowers/specs/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md",
            plan_path=root
            / "docs/superpowers/plans/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md",
            acquisition_contract_path=root
            / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        fixture.registry.advance_execution_phase(
            root,
            run_spec,
            expected_phase="DESIGN_ACCEPTED",
            next_phase="ACQUIRING",
            inventory_path=root / "audit/v5_1_bt/protected_raw_inventory.json",
            validation_receipts=(),
        )
        with self._registry_disposition_copy_seam(root) as (
            _copy_fixture_artifact,
            expected_pairs,
            artifact_bytes,
            copied_pairs,
            published_sidecar_pairs,
        ):
            dispositioned_query_id = fixture.install_source_terminal_checkpoint_chain(
                root, run_spec
            )
        self.assertEqual(copied_pairs, list(expected_pairs))
        self.assertEqual(len(published_sidecar_pairs), 1)
        self._assert_local_disposition_artifacts(root, artifact_bytes)
        self.assertEqual(dispositioned_query_id, self._fixture_disposition_values[0])
        self.source_checkpoint_chain = fixture.source_checkpoint_chain
        self.source_checkpoint_paths = fixture.source_checkpoint_paths
        self.source_disposition_success_sha256 = (
            fixture.source_disposition_success_sha256
        )
        self.source_disposition_sidecar_sha256 = (
            fixture.registry._SOURCE_DISPOSITION_SIDECAR_SHA256
        )
        self.source_disposition_sidecar_path = (
            fixture.registry._SOURCE_DISPOSITION_SIDECAR_PATH
        )

        def authority(relative: str) -> tuple[Path, str]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            contents = canonical_json_bytes({"fixture_authority": relative}) + b"\n"
            path.write_bytes(contents)
            return path, hashlib.sha256(contents).hexdigest()

        schedule_path, schedule_sha256 = authority(
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "schedule_anchor_inventory.json"
        )
        game_path, game_sha256 = authority(
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "game_evidence_inventory.json"
        )
        _seal_path, seal_sha256 = authority(
            "audit/v5_2_qd/historical_superseding_565534/acquisition/"
            "venue_timezone_seal.json"
        )

        inputs = store_inputs(current_phase="ACQUIRING")
        source_snapshots = tuple(
            dataclasses.replace(
                snapshot,
                run_id=run_spec.run_id,
                canonical_bytes=(
                    canonical_json_bytes(
                        {**snapshot.as_mapping(), "run_id": run_spec.run_id}
                    )
                    + b"\n"
                ),
            )
            for snapshot in inputs.replay.game_snapshots
        )
        # Recompute the snapshot hashes after rebinding run_id.
        source_snapshots = tuple(
            dataclasses.replace(
                snapshot,
                sha256=hashlib.sha256(snapshot.canonical_bytes).hexdigest(),
            )
            for snapshot in source_snapshots
        )
        disposition = DuplicateGamePkDispositionBinding(
            dispositioned_query_id,
            AuthorityBinding(
                "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                f"mlb_schedule_game_feed/{dispositioned_query_id}/success.json",
                self.source_disposition_success_sha256,
            ),
            AuthorityBinding(
                self.source_disposition_sidecar_path,
                self.source_disposition_sidecar_sha256,
            ),
        )
        replay = dataclasses.replace(
            inputs.replay,
            run_id=run_spec.run_id,
            game_snapshots=source_snapshots,
            schedule_inventory=dataclasses.replace(
                inputs.replay.schedule_inventory,
                path=schedule_path,
                sha256=schedule_sha256,
            ),
            game_inventory=dataclasses.replace(
                inputs.replay.game_inventory,
                path=game_path,
                sha256=game_sha256,
            ),
            timezone_gate=dataclasses.replace(
                inputs.replay.timezone_gate,
                seal_sha256=seal_sha256,
            ),
            duplicate_gamepk_disposition=disposition if with_disposition else None,
            dispositioned_query_ids=(dispositioned_query_id,)
            if with_disposition
            else (),
        )
        common = orchestration.CommonArgs(
            root=root,
            run_spec=root
            / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract=root
            / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
            protected_inventory=root / "audit/v5_1_bt/protected_raw_inventory.json",
        )
        return root, common, replay

    def test_replay_inventory_and_sources_ready_require_disposition_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, common, replay = self.build_source_terminal_fixture(Path(directory))
            self.assertFalse(
                isinstance(
                    orchestration.execution_registry._load_checkpoints_locked,
                    Mock,
                )
            )
            authenticated_checkpoint = (
                orchestration.execution_registry.load_source_verification_checkpoint(
                    root,
                    load_source_run_spec(common.run_spec),
                )
            )
            self.assertEqual(authenticated_checkpoint.stage, "REMAINING")
            self.assertEqual(
                authenticated_checkpoint.receipt_sha256,
                self.source_checkpoint_chain[-1].receipt_sha256,
            )

            # The real loader rejects changed terminal bytes and a reordered
            # membership name.  Exact restoration must make the same terminal
            # checkpoint authenticate again.
            terminal_checkpoint_path = self.source_checkpoint_paths[-1]
            terminal_checkpoint_bytes = terminal_checkpoint_path.read_bytes()
            terminal_checkpoint_path.write_bytes(
                terminal_checkpoint_bytes.rstrip(b"\n") + b" \n"
            )
            with self.assertRaises(ValueError):
                orchestration.execution_registry.load_source_verification_checkpoint(
                    root,
                    load_source_run_spec(common.run_spec),
                )
            terminal_checkpoint_path.write_bytes(terminal_checkpoint_bytes)

            first_checkpoint_path = self.source_checkpoint_paths[0]
            reordered_checkpoint_path = first_checkpoint_path.with_name(
                f"00000009-{self.source_checkpoint_chain[0].receipt_sha256}.json"
            )
            first_checkpoint_path.rename(reordered_checkpoint_path)
            with self.assertRaises(ValueError):
                orchestration.execution_registry.load_source_verification_checkpoint(
                    root,
                    load_source_run_spec(common.run_spec),
                )
            reordered_checkpoint_path.rename(first_checkpoint_path)
            self.assertEqual(
                orchestration.execution_registry.load_source_verification_checkpoint(
                    root,
                    load_source_run_spec(common.run_spec),
                ).receipt_sha256,
                self.source_checkpoint_chain[-1].receipt_sha256,
            )
            self.assertIsNotNone(replay.duplicate_gamepk_disposition)
            self.assertEqual(replay.dispositioned_query_ids, (replay.duplicate_gamepk_disposition.query_id,))
            source_all_snapshots = tuple(
                sorted(
                    (
                        dataclasses.replace(
                            snapshot,
                            run_id=replay.run_id,
                            canonical_bytes=(
                                canonical_json_bytes(
                                    {**snapshot.as_mapping(), "run_id": replay.run_id}
                                )
                                + b"\n"
                            ),
                        )
                        for snapshot in fake_81()
                    ),
                    key=snapshot_sort_key,
                )
            )
            source_all_snapshots = tuple(
                dataclasses.replace(
                    snapshot,
                    sha256=hashlib.sha256(snapshot.canonical_bytes).hexdigest(),
                )
                for snapshot in source_all_snapshots
            )
            # Bind the derived query ID into one retained schedule snapshot so
            # the source inventory's global logical-query union authenticates
            # the terminal disposition exactly as production does.
            first_snapshot = source_all_snapshots[0]
            first_payload = {
                **first_snapshot.as_mapping(),
                "logical_query_ids": [replay.dispositioned_query_ids[0]],
            }
            first_bytes = canonical_json_bytes(first_payload) + b"\n"
            source_all_snapshots = (
                dataclasses.replace(
                    first_snapshot,
                    logical_query_ids=(replay.dispositioned_query_ids[0],),
                    canonical_bytes=first_bytes,
                    sha256=hashlib.sha256(first_bytes).hexdigest(),
                ),
                *source_all_snapshots[1:],
            )
            with patch.object(
                orchestration, "load_snapshot_replay_inputs", return_value=replay
            ), patch(
                "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
                return_value=source_all_snapshots,
            ), patch.object(
                orchestration,
                "verify_protected_raw_inventory",
                return_value=object(),
            ), patch.object(
                orchestration,
                "snapshot_protected_inventory_membership",
                return_value=(),
            ):
                context = orchestration.build_context(common)
            self.assertIsInstance(context.run_spec, SourceRunSpec)
            self.assertEqual(
                context.inventory.dispositioned_query_ids,
                (replay.dispositioned_query_ids[0],),
            )
            self.assertEqual(context.phase, "ACQUIRING")
            # Keep the bounded protected-inventory seam active across
            # publish/preflight rebuilds; the temporary fixture deliberately
            # does not copy the production 1,675-record legacy tree.
            protected_patcher = patch.object(
                orchestration, "verify_protected_raw_inventory", return_value=object()
            )
            membership_patcher = patch.object(
                orchestration,
                "snapshot_protected_inventory_membership",
                return_value=(),
            )
            protected_patcher.start()
            membership_patcher.start()
            self.addCleanup(protected_patcher.stop)
            self.addCleanup(membership_patcher.stop)
            replay_patcher = patch.object(
                orchestration, "load_snapshot_replay_inputs", return_value=replay
            )
            snapshot_patcher = patch(
                "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
                return_value=source_all_snapshots,
            )
            replay_patcher.start()
            snapshot_patcher.start()
            self.addCleanup(replay_patcher.stop)
            self.addCleanup(snapshot_patcher.stop)
            replay_payload = orchestration._clean_replay_payload(
                context, context.inventory, "11111111-1111-4111-8111-111111111111"
            )
            replay_payload.update(
                orchestration._source_disposition_payload_fields(
                    context, context.inventory
                )
            )
            self.assertEqual(
                replay_payload["dispositioned_query_ids"],
                [replay.dispositioned_query_ids[0]],
            )
            self.assertEqual(
                replay_payload["duplicate_gamepk_disposition"],
                replay.duplicate_gamepk_disposition.as_mapping(),
            )

            # Exercise the public source-finalization path against real
            # temporary files, schemas, context rebuilds, and registry lock.
            # The bounded fixture injects verified replay/protected-inventory
            # seams instead of copying the production 500-query/legacy trees.
            source_root = root / (
                "data/v5_2_qd/historical_superseding_565534/sources"
            )
            acquisition_root = root / (
                "audit/v5_2_qd/historical_superseding_565534/acquisition"
            )
            review_root = acquisition_root / "source_reviews"
            replay_root = acquisition_root / "source_replays"
            query_document = review_root / "task9-query-ids.json"
            query_document.parent.mkdir(parents=True, exist_ok=True)
            query_document.write_bytes(
                canonical_json_bytes(
                    {"query_ids": [context.inventory.logical_query_ids[0]]}
                )
                + b"\n"
            )
            publication = orchestration.publish_inventory(
                context, lock_owner="task9-source-e2e"
            )
            self.assertEqual(publication.status, "PUBLISHED")
            self.assertEqual(publication.snapshot_count, 81)
            review = orchestration.write_review(
                context,
                orchestration.ReviewRequest(
                    "independent-source-auditor",
                    "independent-source-auditor",
                    "APPROVED",
                    query_document,
                    review_root / "task9-review.json",
                    review_root / "task9-review.commit.json",
                ),
            )
            replays = tuple(
                orchestration.write_replay(
                    context,
                    orchestration.ReplayRequest(
                        replay_root / f"task9-replay-{index}.json",
                        replay_root / f"task9-replay-{index}.commit.json",
                    ),
                )
                for index in (1, 2)
            )
            for report in (review, *replays):
                payload = json.loads(report.path.read_bytes())
                self.assertEqual(
                    payload["dispositioned_query_ids"],
                    [replay.dispositioned_query_ids[0]],
                )
                self.assertEqual(
                    payload["duplicate_gamepk_disposition"],
                    replay.duplicate_gamepk_disposition.as_mapping(),
                )

            checkpoint_path = root / orchestration._checkpoint_path(
                context.checkpoint, context.authority_context
            )
            inventory_path = source_root / "source_inventory.json"
            advance_request = orchestration.AdvanceRequest(
                (review.commit_path,),
                (replays[0].commit_path, replays[1].commit_path),
                checkpoint_path,
                inventory_path,
            )

            wrong_checkpoint_request = dataclasses.replace(
                advance_request,
                checkpoint=self.source_checkpoint_paths[-2],
            )
            with self.assertRaises(orchestration.FinalizationError):
                orchestration.advance_sources_ready(
                    context,
                    wrong_checkpoint_request,
                )

            # A review or commit marker changed after writing is rejected and
            # does not mutate registry state.  Restore exact bytes and then
            # perform the valid advance.
            original_review = review.path.read_bytes()
            tampered_review = json.loads(original_review)
            tampered_review["dispositioned_query_ids"] = ["0" * 64]
            review.path.write_bytes(canonical_json_bytes(tampered_review) + b"\n")
            with self.assertRaises(orchestration.FinalizationError):
                orchestration.advance_sources_ready(context, advance_request)
            review.path.write_bytes(original_review)

            original_marker = review.commit_path.read_bytes()
            tampered_marker = json.loads(original_marker)
            tampered_marker["duplicate_gamepk_disposition"]["query_id"] = "0" * 64
            review.commit_path.write_bytes(canonical_json_bytes(tampered_marker) + b"\n")
            with self.assertRaises(orchestration.FinalizationError):
                orchestration.advance_sources_ready(context, advance_request)
            review.commit_path.write_bytes(original_marker)

            forged_success_marker = json.loads(original_marker)
            forged_success_marker["duplicate_gamepk_disposition"][
                "success_receipt"
            ]["sha256"] = "f" * 64
            review.commit_path.write_bytes(
                canonical_json_bytes(forged_success_marker) + b"\n"
            )
            with self.assertRaises(orchestration.FinalizationError):
                orchestration.advance_sources_ready(context, advance_request)
            review.commit_path.write_bytes(original_marker)

            advanced = orchestration.advance_sources_ready(context, advance_request)
            self.assertEqual(advanced.phase, "SOURCES_READY")
            state = orchestration.execution_registry.load_execution_registry(
                root, context.run_spec
            )
            self.assertEqual(state.phase, "SOURCES_READY")
            self.assertEqual(
                state.inventory_sha256,
                hashlib.sha256(inventory_path.read_bytes()).hexdigest(),
            )
            fresh = orchestration.build_context(common)
            self.assertEqual(fresh.phase, "SOURCES_READY")
            self.assertEqual(
                fresh.checkpoint.dispositioned_query_ids,
                (replay.dispositioned_query_ids[0],),
            )
            with self.assertRaises(orchestration.FinalizationError):
                orchestration.advance_sources_ready(fresh, advance_request)
            with self.assertRaisesRegex(ValueError, "immediate next|terminal"):
                orchestration.execution_registry.quarantine_execution_transition(
                    root,
                    context.run_spec,
                    attempted_phase="SOURCES_READY",
                    validation_receipts=(review.commit_path,),
                )

            # Every source-only boundary must reject a missing, extra,
            # tampered, cross-run, or stale disposition binding before I/O.
            inventory = context.inventory
            bindings = inventory.bindings
            disposition = bindings.duplicate_gamepk_disposition
            self.assertIsNotNone(disposition)
            invalid_contexts = (
                dataclasses.replace(
                    context,
                    inventory=dataclasses.replace(
                        inventory,
                        bindings=dataclasses.replace(
                            bindings,
                            duplicate_gamepk_disposition=None,
                            dispositioned_query_ids=(),
                        ),
                    ),
                ),
                dataclasses.replace(
                    context,
                    inventory=dataclasses.replace(
                        inventory,
                        bindings=dataclasses.replace(
                            bindings,
                            dispositioned_query_ids=(
                                replay.dispositioned_query_ids[0],
                                "0" * 64,
                            ),
                        ),
                    ),
                ),
                dataclasses.replace(
                    context,
                    inventory=dataclasses.replace(
                        inventory,
                        bindings=dataclasses.replace(
                            bindings,
                            duplicate_gamepk_disposition=dataclasses.replace(
                                disposition,
                                query_sidecar=AuthorityBinding(
                                    disposition.query_sidecar.path,
                                    "f" * 64,
                                ),
                            ),
                        ),
                    ),
                ),
                dataclasses.replace(
                    context,
                    checkpoint=dataclasses.replace(
                        context.checkpoint,
                        dispositioned_query_ids=(),
                    ),
                ),
            )
            for invalid in invalid_contexts:
                with self.assertRaises(orchestration.FinalizationError):
                    orchestration._source_disposition_payload_fields(
                        invalid, invalid.inventory
                    )

            _root, missing_common, missing_replay = self.build_source_terminal_fixture(
                Path(directory) / "missing", with_disposition=False
            )
            with patch.object(
                orchestration,
                "load_snapshot_replay_inputs",
                return_value=missing_replay,
            ), patch(
                "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
                return_value=source_all_snapshots,
            ), patch.object(
                orchestration,
                "verify_protected_raw_inventory",
                return_value=object(),
            ), patch.object(
                orchestration,
                "snapshot_protected_inventory_membership",
                return_value=(),
            ), self.assertRaises(orchestration.FinalizationError) as raised:
                orchestration.build_context(missing_common)
            self.assertIn(
                raised.exception.error_code,
                {"AUTHORITY_BLOCKED", "AUTHORITY_MALFORMED", "CHECKPOINT_AUTHORITY_UNAVAILABLE"},
            )

    def run_finalization_sequence(
        self,
        root: Path,
        common: orchestration.CommonArgs,
        request_bundle: dict[str, Path],
        replay: object,
        snapshots: tuple[object, ...],
    ) -> tuple[orchestration.StatusReport, orchestration.ReviewReport, tuple[orchestration.ReplayReport, orchestration.ReplayReport], orchestration.AdvanceReport, orchestration.StatusReport]:
        with (
            patch.object(orchestration, "load_snapshot_replay_inputs", return_value=replay),
            patched_store_replay(),
            patch(
                "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
                return_value=snapshots,
            ),
        ):
            context = orchestration.build_context(common)
            initial = orchestration.read_status(context)
            self.assertEqual(initial.phase, "ACQUIRING")
            self.assertEqual(orchestration.run_preflight(context).status, "PASS")
            publication = orchestration.publish_inventory(context, lock_owner="e2e")
            self.assertEqual(publication.status, "PUBLISHED")
            self.assertEqual(publication.snapshot_count, 81)
            self.assertEqual(request_bundle["inventory"].read_bytes(), inventory_bytes(context.inventory))
            request_bundle["query_document"].parent.mkdir(parents=True, exist_ok=True)
            request_bundle["query_document"].write_bytes(
                canonical_json_bytes({"query_ids": [context.inventory.logical_query_ids[0]]})
                + b"\n"
            )
            review = orchestration.write_review(
                context,
                orchestration.ReviewRequest(
                    "independent-source-auditor",
                    "independent-source-auditor",
                    "APPROVED",
                    request_bundle["query_document"],
                    request_bundle["review_receipt"],
                    request_bundle["review_commit"],
                ),
            )
            replay_one = orchestration.write_replay(
                context,
                orchestration.ReplayRequest(
                    request_bundle["replay_one_receipt"],
                    request_bundle["replay_one_commit"],
                ),
            )
            replay_two = orchestration.write_replay(
                context,
                orchestration.ReplayRequest(
                    request_bundle["replay_two_receipt"],
                    request_bundle["replay_two_commit"],
                ),
            )
            published_inventory = json.loads(request_bundle["inventory"].read_bytes())
            self.assertEqual(published_inventory["status"], "READY_CANDIDATE")
            self.assertEqual(published_inventory["critical_errors"], [])
            self.assertEqual(published_inventory["network_attempts"], 0)
            self.assertTrue(published_inventory["source_gate_ready"])
            advanced = orchestration.advance_sources_ready(
                context,
                orchestration.AdvanceRequest(
                    (request_bundle["review_commit"],),
                    (request_bundle["replay_one_commit"], request_bundle["replay_two_commit"]),
                    request_bundle["checkpoint"],
                    request_bundle["inventory"],
                ),
            )
            fresh = orchestration.read_status(orchestration.build_context(common))
        return initial, review, (replay_one, replay_two), advanced, fresh

    def test_miniature_workflow_never_writes_real_historical_namespace(self) -> None:
        real_sources = ROOT / "data/v5_2_qd/historical/sources"
        before = self.lexists_snapshot(real_sources)
        before_tree = self.source_tree_snapshot(real_sources)
        source_writes: list[tuple[str, str]] = []
        network_calls: list[str] = []
        original_open = os.open
        original_mkdir = os.mkdir
        original_unlink = os.unlink
        original_rename = os.rename
        original_replace = os.replace

        def forbid_source_write(
            operation: str,
            path: object,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if not isinstance(path, (str, bytes, os.PathLike)):
                return
            candidate = Path(os.fsdecode(path))
            if not candidate.is_absolute():
                if dir_fd is None:
                    candidate = (Path.cwd() / candidate).resolve(strict=False)
                else:
                    base = self.directory_fd_path(dir_fd)
                    if base is None:
                        source_writes.append((operation, f"<unresolved-fd:{dir_fd}>"))
                        raise AssertionError("production source namespace write target unresolved")
                    candidate = (base / candidate).resolve(strict=False)
            if self.is_within(candidate, real_sources):
                source_writes.append((operation, os.fspath(candidate)))
                raise AssertionError("production source namespace write attempted")

        def guarded_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if flags & (
                os.O_WRONLY
                | os.O_RDWR
                | os.O_CREAT
                | os.O_TRUNC
                | os.O_APPEND
            ):
                forbid_source_write("open", path, dir_fd=kwargs.get("dir_fd"))
            return original_open(path, flags, *args, **kwargs)

        def guarded_mkdir(path: object, *args: object, **kwargs: object) -> None:
            forbid_source_write("mkdir", path, dir_fd=kwargs.get("dir_fd"))
            return original_mkdir(path, *args, **kwargs)

        def guarded_unlink(path: object, *args: object, **kwargs: object) -> None:
            forbid_source_write("unlink", path, dir_fd=kwargs.get("dir_fd"))
            return original_unlink(path, *args, **kwargs)

        def guarded_rename(source: object, destination: object, *args: object, **kwargs: object) -> None:
            forbid_source_write(
                "rename-source", source, dir_fd=kwargs.get("src_dir_fd")
            )
            forbid_source_write(
                "rename-destination", destination, dir_fd=kwargs.get("dst_dir_fd")
            )
            return original_rename(source, destination, *args, **kwargs)

        def guarded_replace(source: object, destination: object, *args: object, **kwargs: object) -> None:
            forbid_source_write(
                "replace-source", source, dir_fd=kwargs.get("src_dir_fd")
            )
            forbid_source_write(
                "replace-destination", destination, dir_fd=kwargs.get("dst_dir_fd")
            )
            return original_replace(source, destination, *args, **kwargs)

        def forbid_network(*args: object, **kwargs: object) -> object:
            del args, kwargs
            network_calls.append("network")
            raise AssertionError("E2E network attempted")
        with tempfile.TemporaryDirectory() as directory:
            root, common, request_bundle, replay, snapshots = self.build_finalization_fixture(
                Path(directory)
            )
            with (
                patch.object(os, "open", side_effect=guarded_open),
                patch.object(os, "mkdir", side_effect=guarded_mkdir),
                patch.object(os, "unlink", side_effect=guarded_unlink),
                patch.object(os, "rename", side_effect=guarded_rename),
                patch.object(os, "replace", side_effect=guarded_replace),
                patch.object(socket, "socket", side_effect=forbid_network),
                patch.object(socket, "create_connection", side_effect=forbid_network),
                patch.object(urllib.request, "urlopen", side_effect=forbid_network),
            ):
                anchor = real_sources
                while not os.path.lexists(anchor):
                    anchor = anchor.parent
                relative_sources = real_sources.relative_to(anchor).as_posix()
                descriptor = os.open(
                    anchor,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    for operation in (
                        lambda: os.open(
                            f"{relative_sources}/adversarial-open.json",
                            os.O_WRONLY | os.O_CREAT,
                            0o600,
                            dir_fd=descriptor,
                        ),
                        lambda: os.mkdir(
                            f"{relative_sources}/adversarial-directory", dir_fd=descriptor
                        ),
                        lambda: os.unlink(
                            f"{relative_sources}/adversarial-unlink.json", dir_fd=descriptor
                        ),
                        lambda: os.rename(
                            f"{relative_sources}/adversarial-source.json",
                            root / "adversarial-safe-destination.json",
                            src_dir_fd=descriptor,
                            dst_dir_fd=descriptor,
                        ),
                        lambda: os.rename(
                            root / "adversarial-safe-source.json",
                            f"{relative_sources}/adversarial-destination.json",
                            src_dir_fd=descriptor,
                            dst_dir_fd=descriptor,
                        ),
                        lambda: os.replace(
                            f"{relative_sources}/adversarial-replace-source.json",
                            root / "adversarial-safe-replace-destination.json",
                            src_dir_fd=descriptor,
                            dst_dir_fd=descriptor,
                        ),
                        lambda: os.replace(
                            root / "adversarial-safe-replace-source.json",
                            f"{relative_sources}/adversarial-replace-destination.json",
                            src_dir_fd=descriptor,
                            dst_dir_fd=descriptor,
                        ),
                    ):
                        with self.assertRaisesRegex(AssertionError, "production source"):
                            operation()
                finally:
                    os.close(descriptor)
                self.assertEqual(len(source_writes), 7)
                source_writes.clear()
                initial, review, replays, advanced, fresh = self.run_finalization_sequence(
                    root, common, request_bundle, replay, snapshots
                )
            self.assertTrue(initial.publish_ready)
            self.assertEqual(review.receipt_kind, "SOURCE_REVIEW")
            self.assertEqual(json.loads(review.path.read_bytes())["decision"], "APPROVED")
            execution_ids = [
                json.loads(report.path.read_bytes())["execution_id"]
                for report in replays
            ]
            self.assertEqual(len(set(execution_ids)), 2)
            self.assertTrue(all(uuid.UUID(value).version == 4 for value in execution_ids))
            self.assertTrue(all(json.loads(report.path.read_bytes())["network_attempts"] == 0 for report in replays))
            for path in (
                review.path,
                review.commit_path,
                *(item for report in replays for item in (report.path, report.commit_path)),
            ):
                self.assertEqual(path.read_bytes(), canonical_json_bytes(json.loads(path.read_bytes())) + b"\n")
            self.assertEqual(advanced.phase, "SOURCES_READY")
            self.assertEqual(fresh.phase, "SOURCES_READY")
        self.assertEqual(self.lexists_snapshot(real_sources), before)
        self.assertEqual(self.source_tree_snapshot(real_sources), before_tree)
        self.assertEqual(source_writes, [])
        self.assertEqual(network_calls, [])


def tree_snapshot(root: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    return tuple(
        sorted(
            (
                path.relative_to(root).as_posix(),
                path.lstat().st_mode,
                path.lstat().st_ino,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            )
            for path in root.rglob("*")
        )
    )


class _ContextBaseTests(unittest.TestCase):
    def test_advance_request_and_report_are_frozen_public_contracts(self) -> None:
        request = orchestration.AdvanceRequest(
            (Path("review.commit.json"),),
            (Path("replay-one.commit.json"), Path("replay-two.commit.json")),
            Path("checkpoint.json"),
            Path("source_inventory.json"),
        )
        report = orchestration.AdvanceReport("ADVANCED", "SOURCES_READY", "a" * 64)
        self.assertTrue(dataclasses.is_dataclass(request))
        self.assertTrue(dataclasses.is_dataclass(report))
        self.assertTrue(request.__dataclass_params__.frozen)
        self.assertTrue(report.__dataclass_params__.frozen)
        self.assertIn("advance_sources_ready", orchestration.__all__)

    def test_advance_uses_explicit_ordered_markers_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint = orchestration.AcquisitionCheckpoint(
                0, "c" * 64, None, "REMAINING", "2026-07-20T00:00:00.000000Z",
                "e" * 40, "f" * 64, (), (), (), (), (), (), (), (), (), (),
            )
            checkpoint_path = root / orchestration._checkpoint_path(checkpoint)
            inventory_path = root / "data/v5_2_qd/historical/sources/source_inventory.json"
            checkpoint_path.parent.mkdir(parents=True)
            inventory_path.parent.mkdir(parents=True)
            checkpoint_path.write_bytes(b'{"checkpoint":true}\n')
            inventory_path.write_bytes(b'{"inventory":true}\n')
            markers: list[Path] = []
            for relative, kind, number in (
                ("audit/v5_2_qd/historical/acquisition/source_reviews/review.commit.json", "SOURCE_REVIEW", 1),
                ("audit/v5_2_qd/historical/acquisition/source_replays/replay-one.commit.json", "CLEAN_SOURCE_REPLAY", 2),
                ("audit/v5_2_qd/historical/acquisition/source_replays/replay-two.commit.json", "CLEAN_SOURCE_REPLAY", 3),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(canonical_json_bytes({
                    "receipt_kind": "SOURCE_RECEIPT_COMMIT",
                    "committed_receipt_kind": kind,
                    "source_receipt": {
                        "path": relative.replace(".commit", ""),
                        "sha256": str(number) * 64,
                    },
                }) + b"\n")
                markers.append(path)
            registry = ExecutionRegistryState("ACQUIRING", 1, "a" * 64, "b" * 64)
            context = orchestration.FinalizationContext(
                root, root / "run.json", root / "contract.json", root / "protected.json",
                SimpleNamespace(run_id="run"), registry, checkpoint, object(), object(),
                "ACQUIRING", "e" * 40, (),
            )
            status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
            preflight = orchestration.PreflightReport("ACQUIRING", "PASS", "b" * 64, 1, 0, ())
            advanced = ExecutionRegistryState("SOURCES_READY", 2, "d" * 64, "b" * 64)
            request = orchestration.AdvanceRequest(
                (markers[0],), (markers[1], markers[2]), checkpoint_path, inventory_path
            )
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                patch.object(orchestration.execution_registry, "validate_sources_ready_evidence_set") as validate,
                patch.object(
                    orchestration.execution_registry,
                    "advance_sources_ready_phase",
                    return_value=orchestration.execution_registry.ExecutionPhaseAdvance(
                        advanced, created=True
                    ),
                ) as advance,
            ):
                report = orchestration.advance_sources_ready(context, request)
            self.assertEqual(report, orchestration.AdvanceReport("ADVANCED", "SOURCES_READY", "d" * 64))
            advance.assert_called_once_with(
                root, context.run_spec,
                inventory_path=inventory_path, validation_receipts=tuple(markers),
                expected_checkpoint_path=checkpoint_path,
            )
            validate.assert_called_once_with(
                root, context.run_spec, inventory_path=inventory_path,
                checkpoint_path=checkpoint_path, validation_receipts=tuple(markers),
            )

    def test_advance_reports_existing_from_atomic_gate_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checkpoint = orchestration.AcquisitionCheckpoint(
                0, "c" * 64, None, "REMAINING", "2026-07-20T00:00:00.000000Z",
                "e" * 40, "f" * 64, (), (), (), (), (), (), (), (), (), (),
            )
            checkpoint_path = root / orchestration._checkpoint_path(checkpoint)
            inventory_path = root / "data/v5_2_qd/historical/sources/source_inventory.json"
            checkpoint_path.parent.mkdir(parents=True)
            inventory_path.parent.mkdir(parents=True)
            checkpoint_path.write_bytes(b'{"checkpoint":true}\n')
            inventory_path.write_bytes(b'{"inventory":true}\n')
            markers: list[Path] = []
            for relative, kind, number in (
                ("audit/v5_2_qd/historical/acquisition/source_reviews/review.commit.json", "SOURCE_REVIEW", 1),
                ("audit/v5_2_qd/historical/acquisition/source_replays/replay-one.commit.json", "CLEAN_SOURCE_REPLAY", 2),
                ("audit/v5_2_qd/historical/acquisition/source_replays/replay-two.commit.json", "CLEAN_SOURCE_REPLAY", 3),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(canonical_json_bytes({
                    "receipt_kind": "SOURCE_RECEIPT_COMMIT",
                    "committed_receipt_kind": kind,
                    "source_receipt": {
                        "path": relative.replace(".commit", ""),
                        "sha256": str(number) * 64,
                    },
                }) + b"\n")
                markers.append(path)
            context = orchestration.FinalizationContext(
                root, root / "run.json", root / "contract.json", root / "protected.json",
                SimpleNamespace(run_id="run"),
                ExecutionRegistryState("ACQUIRING", 1, "a" * 64, "b" * 64),
                checkpoint, object(), object(), "ACQUIRING", "e" * 40, (),
            )
            status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
            preflight = orchestration.PreflightReport("ACQUIRING", "PASS", "b" * 64, 1, 0, ())
            result = orchestration.execution_registry.ExecutionPhaseAdvance(
                ExecutionRegistryState("SOURCES_READY", 2, "d" * 64, "b" * 64),
                created=False,
            )
            request = orchestration.AdvanceRequest(
                (markers[0],), (markers[1], markers[2]), checkpoint_path, inventory_path
            )
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                patch.object(orchestration.execution_registry, "validate_sources_ready_evidence_set") as validate,
                patch.object(orchestration.execution_registry, "advance_sources_ready_phase", return_value=result),
            ):
                report = orchestration.advance_sources_ready(context, request)
            self.assertEqual(report, orchestration.AdvanceReport("EXISTING", "SOURCES_READY", "d" * 64))
            validate.assert_called_once()

    def test_generic_advance_preserves_task2_public_signature(self) -> None:
        signature = inspect.signature(orchestration.execution_registry.advance_execution_phase)
        self.assertEqual(
            tuple(signature.parameters),
            ("root", "run_spec", "expected_phase", "next_phase", "inventory_path", "validation_receipts"),
        )

    def test_context_types_are_frozen_with_canonical_defaults(self) -> None:
        args = orchestration.CommonArgs()
        self.assertEqual(args.root, Path("."))
        self.assertEqual(
            args.run_spec,
            Path("protocols/v5_2_qd/historical_walkforward_500.json"),
        )
        self.assertEqual(
            args.acquisition_contract,
            Path("protocols/v5_2_qd/historical_source_acquisition.json"),
        )
        self.assertEqual(
            args.protected_inventory,
            Path("audit/v5_1_bt/protected_raw_inventory.json"),
        )
        context = orchestration.FinalizationContext.absent(ROOT, ())
        for value in (
            args,
            orchestration.Blocker("X", "x"),
            context,
            orchestration.StatusReport("ABSENT", None, False, False, False, False, ()),
            orchestration.PreflightReport("ABSENT", "BLOCKED", None, 0, 0, ()),
        ):
            self.assertTrue(dataclasses.is_dataclass(value))
            self.assertTrue(value.__dataclass_params__.frozen)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            context.phase = "ACQUIRING"  # type: ignore[misc]

    def test_absent_status_context_is_read_only(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            self.addCleanup(fixture.tearDown)
            root, _run_spec = fixture.make_repo(
                Path(directory), remaining_authority=True
            )
            before = tree_snapshot(root)
            context = orchestration.build_context(orchestration.CommonArgs(root=root))
            self.assertEqual(context.phase, "ABSENT")
            self.assertIsNone(context.registry)
            self.assertIsNone(context.checkpoint)
            self.assertIsNone(context.snapshot_inputs)
            self.assertIsNone(context.inventory)
            self.assertIn("REGISTRY_ABSENT", {item.code for item in context.blockers})
            self.assertEqual(tree_snapshot(root), before)
            self.assertFalse((root / "audit/v5_2_qd/historical/finalization").exists())

    def test_safe_helpers_reject_symlink_ancestor_and_path_escapes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "root"
            root.mkdir()
            target = Path(temp) / "target"
            target.mkdir()
            linked = Path(temp) / "linked"
            linked.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                orchestration._trusted_root(linked)
            with self.assertRaises(ValueError):
                orchestration._relative_artifact(root, Path("../escape"), "artifact")
            with self.assertRaises(ValueError):
                orchestration._relative_artifact(root, Path("child\\escape"), "artifact")
            with self.assertRaises(ValueError):
                orchestration._relative_artifact(root, target / "outside", "artifact")

    def test_safe_reader_rejects_symlinks_hardlinks_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            safe = root / "safe.json"
            raw = canonical_json_bytes({"value": 1}) + b"\n"
            safe.write_bytes(raw)
            relative, read = orchestration._read_explicit_artifact(root, safe, "safe")
            self.assertEqual(relative, "safe.json")
            self.assertEqual(read, raw)
            symlink = root / "symlink.json"
            symlink.symlink_to(safe)
            with self.assertRaises(ValueError):
                orchestration._read_explicit_artifact(root, symlink, "symlink")
            hardlink = root / "hardlink.json"
            os.link(safe, hardlink)
            with self.assertRaises(ValueError):
                orchestration._read_explicit_artifact(root, hardlink, "hardlink")
            fifo = root / "fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                orchestration._read_explicit_artifact(root, fifo, "fifo")

    def test_safe_reader_enforces_preflight_and_streaming_byte_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            oversized = root / "oversized.json"
            oversized.write_bytes(b"x" * 65)
            sparse = root / "sparse.json"
            with sparse.open("wb") as handle:
                handle.seek(1024 * 1024)
                handle.write(b"x")
            before_fds = len(os.listdir("/dev/fd"))
            for path in (oversized, sparse):
                with self.subTest(path=path.name), self.assertRaisesRegex(
                    ValueError, "bounded read limit"
                ):
                    orchestration._read_explicit_artifact(
                        root, path, path.name, max_bytes=64
                    )
            growing = root / "growing.json"
            growing.write_bytes(b"x")
            chunks = iter((b"a" * 40, b"b" * 40))
            with patch.object(
                orchestration.os,
                "read",
                side_effect=lambda _fd, _size: next(chunks, b""),
            ), self.assertRaisesRegex(ValueError, "bounded read limit"):
                orchestration._read_explicit_artifact(
                    root, growing, "growing", max_bytes=64
                )
            self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_safe_reader_rejects_ancestor_swap_without_fd_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            anchor = base / "anchor"
            root = anchor / "repo"
            trusted_protocol = root / "protocols/v5_2_qd"
            trusted_protocol.mkdir(parents=True)
            (trusted_protocol / "historical_walkforward_500.json").write_bytes(
                b'{"source":"trusted"}\n'
            )
            external = base / "external"
            external_protocol = external / "repo/protocols/v5_2_qd"
            external_protocol.mkdir(parents=True)
            (external_protocol / "historical_walkforward_500.json").write_bytes(
                b'{"source":"external"}\n'
            )
            moved = base / "moved-anchor"

            def swap_ancestor() -> None:
                anchor.rename(moved)
                anchor.symlink_to(external, target_is_directory=True)

            before_fds = len(os.listdir("/dev/fd"))
            try:
                with patch.object(
                    orchestration,
                    "_after_trusted_root_open",
                    side_effect=swap_ancestor,
                ), patch.object(
                    orchestration,
                    "_canonical_object",
                    wraps=orchestration._canonical_object,
                ) as canonical:
                    with self.assertRaises(orchestration.FinalizationError) as raised:
                        orchestration.build_context(orchestration.CommonArgs(root=root))
                    self.assertEqual(raised.exception.error_code, "AUTHORITY_MALFORMED")
                    self.assertEqual(raised.exception.exit_code, 3)
                    canonical.assert_not_called()
            finally:
                if anchor.is_symlink():
                    anchor.unlink()
                if moved.exists():
                    moved.rename(anchor)
            self.assertEqual(len(os.listdir("/dev/fd")), before_fds)


class AdvanceTests(unittest.TestCase):
    """Explicit source-gate selection is validated before the one advance call."""

    def _fixture(self, root: Path) -> tuple[object, object, list[Path]]:
        checkpoint = orchestration.AcquisitionCheckpoint(
            0, "c" * 64, None, "REMAINING", "2026-07-20T00:00:00.000000Z",
            "e" * 40, "f" * 64, (), (), (), (), (), (), (), (), (), (),
        )
        checkpoint_path = root / orchestration._checkpoint_path(checkpoint)
        inventory_path = root / "data/v5_2_qd/historical/sources/source_inventory.json"
        checkpoint_path.parent.mkdir(parents=True)
        inventory_path.parent.mkdir(parents=True)
        checkpoint_path.write_bytes(b'{"checkpoint":true}\n')
        inventory_path.write_bytes(b'{"inventory":true}\n')
        markers = []
        for relative in (
            "audit/v5_2_qd/historical/acquisition/source_reviews/review.commit.json",
            "audit/v5_2_qd/historical/acquisition/source_replays/replay-one.commit.json",
            "audit/v5_2_qd/historical/acquisition/source_replays/replay-two.commit.json",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"{}\n")
            markers.append(path)
        context = orchestration.FinalizationContext(
            root, root / "run.json", root / "contract.json", root / "protected.json",
            SimpleNamespace(run_id="run"),
            ExecutionRegistryState("ACQUIRING", 1, "a" * 64, "b" * 64),
            checkpoint, object(), object(), "ACQUIRING", "e" * 40, (),
        )
        request = orchestration.AdvanceRequest(
            (markers[0],), (markers[1], markers[2]), checkpoint_path, inventory_path
        )
        return context, request, markers

    def test_invalid_prevalidation_never_calls_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context, request, _markers = self._fixture(root)
            status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
            preflight = orchestration.PreflightReport("ACQUIRING", "PASS", "b" * 64, 1, 0, ())
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                patch.object(orchestration.execution_registry, "validate_sources_ready_evidence_set", side_effect=ValueError("bad receipt")),
                patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as advance,
                self.assertRaises(orchestration.FinalizationError) as raised,
            ):
                orchestration.advance_sources_ready(context, request)
            self.assertEqual(raised.exception.error_code, "RECEIPT_SET_INVALID")
            advance.assert_not_called()

    def test_prevalidation_failures_never_call_advance(self) -> None:
        for message, expected in (
            ("raw receipt differs", "RECEIPT_SET_INVALID"),
            ("review subset differs", "RECEIPT_SET_INVALID"),
            ("replay UUID differs", "RECEIPT_SET_INVALID"),
            ("inventory binding differs", "RECEIPT_SET_INVALID"),
            ("Git binding differs", "AUTHORITY_BLOCKED"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                context, request, _markers = self._fixture(root)
                status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
                preflight = orchestration.PreflightReport("ACQUIRING", "PASS", "b" * 64, 1, 0, ())
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "validate_sources_ready_evidence_set", side_effect=ValueError(message)),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as advance,
                    self.assertRaises(orchestration.FinalizationError) as raised,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(raised.exception.error_code, expected)
                advance.assert_not_called()

    def test_unsafe_marker_never_calls_prevalidator_or_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context, request, markers = self._fixture(root)
            unsafe = markers[0].with_name("linked.commit.json")
            unsafe.symlink_to(markers[0])
            request = dataclasses.replace(request, review_receipts=(unsafe,))
            with (
                patch.object(orchestration.execution_registry, "validate_sources_ready_evidence_set") as validate,
                patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as advance,
                self.assertRaises(orchestration.FinalizationError) as raised,
            ):
                orchestration.advance_sources_ready(context, request)
            self.assertEqual(raised.exception.exit_code, 5)
            validate.assert_not_called()
            advance.assert_not_called()

    def test_actual_prevalidator_authenticates_explicit_markers_before_one_advance(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            root, run_spec = fixture.make_repo(Path(directory))
            try:
                fixture.initialize(root, run_spec)
                fixture.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
                inventory, markers = fixture.write_real_source_evidence(root)
                checkpoint = orchestration.load_source_verification_checkpoint(root, run_spec)
                checkpoint_path = root / orchestration._checkpoint_path(checkpoint)
                state = orchestration.load_execution_registry(root, run_spec)
                context = orchestration.FinalizationContext(
                    root,
                    root / "protocols/v5_2_qd/historical_walkforward_500.json",
                    root / "protocols/v5_2_qd/historical_source_acquisition.json",
                    root / "audit/v5_1_bt/protected_raw_inventory.json",
                    run_spec, state, checkpoint, object(), object(), "ACQUIRING",
                    subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, check=True,
                                   capture_output=True, text=True).stdout.strip(), (),
                )
                request = orchestration.AdvanceRequest(
                    (markers[0],), (markers[1], markers[2]), checkpoint_path, inventory
                )
                status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
                preflight = orchestration.PreflightReport("ACQUIRING", "PASS", "b" * 64, 1, 0, ())
                advanced = ExecutionRegistryState("SOURCES_READY", 2, "d" * 64, "b" * 64)
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(
                        orchestration.execution_registry,
                        "advance_sources_ready_phase",
                        return_value=orchestration.execution_registry.ExecutionPhaseAdvance(
                            advanced, created=True
                        ),
                    ) as advance,
                ):
                    report = orchestration.advance_sources_ready(context, request)
                self.assertEqual(report.status, "ADVANCED")
                advance.assert_called_once_with(
                    root, run_spec,
                    inventory_path=inventory, validation_receipts=markers,
                    expected_checkpoint_path=checkpoint_path,
                )
                review = fixture.source_receipt_from_marker(root, markers[0])
                original_review = review.read_bytes()
                original_marker = markers[0].read_bytes()
                rejected = json.loads(review.read_bytes())
                rejected["decision"] = "REJECTED"
                review.write_bytes(canonical_json_bytes(rejected) + b"\n")
                fixture.rebind_source_marker(markers[0], review)
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as rejected_advance,
                    self.assertRaises(orchestration.FinalizationError) as rejected_error,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(rejected_error.exception.error_code, "RECEIPT_SET_INVALID")
                rejected_advance.assert_not_called()
                review.write_bytes(original_review)
                markers[0].write_bytes(original_marker)
                crossed = json.loads(markers[0].read_bytes())
                crossed["acquisition_contract"]["sha256"] = "0" * 64
                markers[0].write_bytes(canonical_json_bytes(crossed) + b"\n")
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as crossed_advance,
                    self.assertRaises(orchestration.FinalizationError) as crossed_error,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(crossed_error.exception.error_code, "RECEIPT_SET_INVALID")
                crossed_advance.assert_not_called()
                markers[0].write_bytes(original_marker)
                original_payload = json.loads(original_review)
                selection = root / original_payload["query_ids_document"]["path"]
                subset_document = selection.with_name("query_ids-subset.json")
                subset_document.write_bytes(canonical_json_bytes({"query_ids": ["1" * 64]}) + b"\n")
                subset_review = review.with_name("review-subset.json")
                subset_payload = dict(original_payload)
                subset_payload["reviewed_query_ids"] = ["1" * 64]
                subset_payload["query_ids_document"] = {
                    "path": subset_document.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(subset_document.read_bytes()).hexdigest(),
                }
                subset_payload["occurred_at_utc"] = "2026-07-21T00:00:00.000000Z"
                subset_review.write_bytes(canonical_json_bytes(subset_payload) + b"\n")
                subset_marker = markers[0].with_name("review-subset.commit.json")
                subset_marker_payload = json.loads(original_marker)
                subset_marker_payload["source_receipt"] = {
                    "path": subset_review.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(subset_review.read_bytes()).hexdigest(),
                }
                subset_marker_payload["occurred_at_utc"] = "2026-07-21T00:00:01.000000Z"
                subset_marker.write_bytes(canonical_json_bytes(subset_marker_payload) + b"\n")
                subset_request = dataclasses.replace(request, review_receipts=(markers[0], subset_marker))
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as subset_advance,
                    self.assertRaises(orchestration.FinalizationError) as subset_error,
                ):
                    orchestration.advance_sources_ready(context, subset_request)
                self.assertEqual(subset_error.exception.error_code, "RECEIPT_SET_INVALID")
                subset_advance.assert_not_called()
                replay_one = fixture.source_receipt_from_marker(root, markers[1])
                replay_two = fixture.source_receipt_from_marker(root, markers[2])
                original_replay_two = replay_two.read_bytes()
                duplicate_replay = json.loads(replay_two.read_bytes())
                duplicate_replay["execution_id"] = json.loads(replay_one.read_bytes())["execution_id"]
                replay_two.write_bytes(canonical_json_bytes(duplicate_replay) + b"\n")
                fixture.rebind_source_marker(markers[2], replay_two)
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as uuid_advance,
                    self.assertRaises(orchestration.FinalizationError) as uuid_error,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(uuid_error.exception.error_code, "RECEIPT_SET_INVALID")
                uuid_advance.assert_not_called()
                replay_two.write_bytes(original_replay_two)
                fixture.rebind_source_marker(markers[2], replay_two)
                stale_checkpoint = checkpoint_path.with_name("stale-checkpoint.json")
                stale_checkpoint.write_bytes(b'{"stale":true}\n')
                stale_request = dataclasses.replace(request, checkpoint=stale_checkpoint)
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as stale_advance,
                    self.assertRaises(orchestration.FinalizationError) as stale_error,
                ):
                    orchestration.advance_sources_ready(context, stale_request)
                self.assertEqual(stale_error.exception.error_code, "RECEIPT_SET_INVALID")
                stale_advance.assert_not_called()
                inventory_cross = json.loads(original_review)
                inventory_cross["inventory_sha256"] = "0" * 64
                review.write_bytes(canonical_json_bytes(inventory_cross) + b"\n")
                fixture.rebind_source_marker(markers[0], review)
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as inventory_advance,
                    self.assertRaises(orchestration.FinalizationError) as inventory_error,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(inventory_error.exception.error_code, "RECEIPT_SET_INVALID")
                inventory_advance.assert_not_called()
            finally:
                fixture.doCleanups()
                fixture.tearDown()

    def test_missing_directory_glob_and_replay_cardinality_fail_before_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context, request, markers = self._fixture(root)
            directory_marker = markers[0].parent
            cases = (
                (dataclasses.replace(request, review_receipts=(root / "missing.commit.json",)), 5),
                (dataclasses.replace(request, review_receipts=(root / "*.commit.json",)), 5),
                (dataclasses.replace(request, review_receipts=(directory_marker,)), 5),
                (dataclasses.replace(request, replay_receipts=(markers[1],)), 4),  # type: ignore[arg-type]
            )
            for candidate, exit_code in cases:
                with self.subTest(exit_code=exit_code):
                    with (
                        patch.object(orchestration.execution_registry, "validate_sources_ready_evidence_set") as validate,
                        patch.object(orchestration.execution_registry, "advance_sources_ready_phase") as advance,
                        self.assertRaises(orchestration.FinalizationError) as raised,
                    ):
                        orchestration.advance_sources_ready(context, candidate)
                    self.assertEqual(raised.exception.exit_code, exit_code)
                    validate.assert_not_called()
                    advance.assert_not_called()

    def test_atomic_source_gate_rejects_competing_authority_without_writes(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            root, run_spec = fixture.make_repo(Path(directory))
            try:
                fixture.initialize(root, run_spec)
                fixture.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
                inventory, markers = fixture.write_real_source_evidence(root)
                checkpoint = orchestration.load_source_verification_checkpoint(root, run_spec)
                checkpoint_path = root / orchestration._checkpoint_path(checkpoint)
                state = orchestration.load_execution_registry(root, run_spec)
                context = orchestration.FinalizationContext(
                    root, root / "protocols/v5_2_qd/historical_walkforward_500.json",
                    root / "protocols/v5_2_qd/historical_source_acquisition.json",
                    root / "audit/v5_1_bt/protected_raw_inventory.json",
                    run_spec, state, checkpoint, object(), object(), "ACQUIRING",
                    subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, check=True,
                                   capture_output=True, text=True).stdout.strip(), (),
                )
                request = orchestration.AdvanceRequest(
                    (markers[0],), (markers[1], markers[2]), checkpoint_path, inventory
                )
                status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
                preflight = orchestration.PreflightReport("ACQUIRING", "PASS", "b" * 64, 1, 0, ())
                phase_receipts = root / "audit/v5_2_qd/historical/execution/phase_receipts"
                quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
                recovery_root = root / "data/v5_2_qd/historical/evidence/recovery-canary"
                recovery_root.mkdir(parents=True)
                (recovery_root / ".failure-cleanup.intent.tombstone").write_bytes(
                    b'{"recovery":"pending"}\n'
                )
                recovery_before = tree_snapshot(recovery_root)

                def registry_writes() -> tuple[tuple[str, bytes], tuple[str, bytes]]:
                    def contents(directory: Path) -> tuple[tuple[str, bytes], ...]:
                        if not directory.exists():
                            return ()
                        return tuple(sorted(
                            (path.name, path.read_bytes()) for path in directory.iterdir()
                        ))
                    return contents(phase_receipts), contents(quarantine)

                original_validate = orchestration.execution_registry.validate_sources_ready_evidence_set
                checkpoint_loader = orchestration.execution_registry._load_checkpoints_locked
                original_checkpoint_result = checkpoint_loader.return_value
                replacement_bytes = b'{"fixture":"replacement-checkpoint"}\n'
                replacement_digest = hashlib.sha256(replacement_bytes).hexdigest()
                replacement_name = f"00000000-{replacement_digest}.json"
                replacement_path = checkpoint_path.with_name(replacement_name)
                replacement_path.write_bytes(replacement_bytes)
                replacement = dataclasses.replace(
                    checkpoint, receipt_sha256=replacement_digest
                )
                checkpoint_loader.reset_mock()

                def swap_checkpoint(*args: object, **kwargs: object) -> None:
                    original_validate(*args, **kwargs)
                    checkpoint_loader.return_value = (
                        (replacement,),
                        ({"pending_query_ids": [], "quarantined_query_ids": []},),
                        (replacement_name,),
                    )

                before = registry_writes()
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(
                        orchestration.execution_registry,
                        "validate_sources_ready_evidence_set",
                        side_effect=swap_checkpoint,
                    ),
                    self.assertRaises(orchestration.FinalizationError) as swapped,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(swapped.exception.error_code, "RECEIPT_SET_INVALID")
                self.assertEqual(registry_writes(), before)
                self.assertEqual(tree_snapshot(recovery_root), recovery_before)
                self.assertTrue(checkpoint_loader.call_args_list)
                self.assertTrue(all(
                    item.kwargs.get("read_only_progress") is True
                    for item in checkpoint_loader.call_args_list
                ))
                checkpoint_loader.return_value = original_checkpoint_result

                original_marker = markers[0].read_bytes()

                def mutate_evidence(*args: object, **kwargs: object) -> None:
                    original_validate(*args, **kwargs)
                    markers[0].write_bytes(b"{}\n")

                before = registry_writes()
                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(
                        orchestration.execution_registry,
                        "validate_sources_ready_evidence_set",
                        side_effect=mutate_evidence,
                    ),
                    self.assertRaises(orchestration.FinalizationError) as mutated,
                ):
                    orchestration.advance_sources_ready(context, request)
                self.assertEqual(mutated.exception.error_code, "RECEIPT_SET_INVALID")
                self.assertEqual(registry_writes(), before)
                self.assertEqual(tree_snapshot(recovery_root), recovery_before)
                markers[0].write_bytes(original_marker)

                barrier = threading.Barrier(2)
                reports: list[orchestration.AdvanceReport] = []
                failures: list[BaseException] = []

                def concurrent_validate(*args: object, **kwargs: object) -> None:
                    original_validate(*args, **kwargs)
                    barrier.wait(timeout=10)

                def advance() -> None:
                    try:
                        reports.append(orchestration.advance_sources_ready(context, request))
                    except BaseException as exc:
                        failures.append(exc)

                with (
                    patch.object(orchestration, "read_status", return_value=status),
                    patch.object(orchestration, "run_preflight", return_value=preflight),
                    patch.object(
                        orchestration.execution_registry,
                        "validate_sources_ready_evidence_set",
                        side_effect=concurrent_validate,
                    ),
                ):
                    first = threading.Thread(target=advance)
                    second = threading.Thread(target=advance)
                    first.start()
                    second.start()
                    first.join(timeout=120)
                    second.join(timeout=120)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual(failures, [])
                self.assertEqual(sorted(report.status for report in reports), ["ADVANCED", "EXISTING"])
                self.assertEqual(len(registry_writes()[0]), len(before[0]) + 1)
            finally:
                fixture.doCleanups()
                fixture.tearDown()

    def test_sources_ready_service_retry_returns_existing_without_new_receipt(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            root, run_spec = fixture.make_repo(Path(directory))
            try:
                fixture.initialize(root, run_spec)
                fixture.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
                inventory, markers = fixture.write_real_source_evidence(root)
                advanced = orchestration.execution_registry.advance_execution_phase(
                    root, run_spec, expected_phase="ACQUIRING", next_phase="SOURCES_READY",
                    inventory_path=inventory, validation_receipts=markers,
                )
                checkpoint = orchestration.load_source_verification_checkpoint(root, run_spec)
                context = orchestration.FinalizationContext(
                    root, root / "protocols/v5_2_qd/historical_walkforward_500.json",
                    root / "protocols/v5_2_qd/historical_source_acquisition.json",
                    root / "audit/v5_1_bt/protected_raw_inventory.json",
                    run_spec, advanced, checkpoint, object(), object(), "SOURCES_READY",
                    subprocess.run(("git", "rev-parse", "HEAD"), cwd=root, check=True,
                                   capture_output=True, text=True).stdout.strip(), (),
                )
                request = orchestration.AdvanceRequest(
                    (markers[0],), (markers[1], markers[2]),
                    root / orchestration._checkpoint_path(checkpoint), inventory,
                )
                receipts = root / "audit/v5_2_qd/historical/execution/phase_receipts"
                before = tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir()))
                report = orchestration.advance_sources_ready(context, request)
                self.assertEqual(report, orchestration.AdvanceReport("EXISTING", "SOURCES_READY", advanced.receipt_sha256))
                self.assertEqual(tuple(sorted((path.name, path.read_bytes()) for path in receipts.iterdir())), before)
            finally:
                fixture.doCleanups()
                fixture.tearDown()


class StatusAndPreflightTests(unittest.TestCase):
    """The advisory/read-only finalization surface must never select or mutate."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.inputs = store_inputs()
        with patched_store_replay():
            self.inventory = build_inventory(self.inputs)
        self.registry = ExecutionRegistryState("ACQUIRING", 7, "a" * 64, "b" * 64)
        self.checkpoint = AcquisitionCheckpoint(
            9,
            "c" * 64,
            "d" * 64,
            "REMAINING",
            "2026-07-20T00:00:00.000000Z",
            "e" * 40,
            "f" * 64,
            (), (), (), (), (), (), (), (), (), (),
        )
        self.context = orchestration.FinalizationContext(
            self.root,
            self.root / "run.json",
            self.root / "contract.json",
            self.root / "protected.json",
            SimpleNamespace(run_id="run"),
            self.registry,
            self.checkpoint,
            self.inputs,
            self.inventory,
            "ACQUIRING",
            "e" * 40,
            (),
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _published_store(self, root: Path) -> None:
        root.mkdir(parents=True)
        with patched_store_replay():
            publish_snapshot_store(
                root,
                self.inventory,
                self.inputs,
                lock_owner="status-test",
            )

    def _superseding_namespace(self) -> SimpleNamespace:
        return SimpleNamespace(
            repository_root=self.root,
            sources_root=self.root
            / "data/v5_2_qd/historical_superseding_565534/sources",
            acquisition_audit_root=self.root
            / "audit/v5_2_qd/historical_superseding_565534/acquisition",
            execution_audit_root=self.root
            / "audit/v5_2_qd/historical_superseding_565534/execution",
        )

    def test_superseding_review_replay_and_advance_reject_old_paths_before_io(self) -> None:
        context = dataclasses.replace(
            self.context, authority_context=self._superseding_namespace()
        )
        old_review = self.root / "audit/v5_2_qd/historical/acquisition/source_reviews"
        old_replay = self.root / "audit/v5_2_qd/historical/acquisition/source_replays"
        with (
            patch.object(
                orchestration,
                "read_status",
                side_effect=AssertionError("source store I/O reached"),
            ),
            self.assertRaises(orchestration.FinalizationError) as review_error,
        ):
            orchestration.write_review(
                context,
                orchestration.ReviewRequest(
                    "independent-source-auditor",
                    "independent-source-auditor",
                    "APPROVED",
                    old_review / "ids.json",
                    old_review / "review.json",
                    old_review / "review.commit.json",
                ),
            )
        self.assertEqual(review_error.exception.error_code, "PATH_UNSAFE")
        with (
            patch.object(
                orchestration,
                "read_status",
                side_effect=AssertionError("source store I/O reached"),
            ),
            self.assertRaises(orchestration.FinalizationError) as replay_error,
        ):
            orchestration.write_replay(
                context,
                orchestration.ReplayRequest(
                    old_replay / "replay.json",
                    old_replay / "replay.commit.json",
                ),
            )
        self.assertEqual(replay_error.exception.error_code, "PATH_UNSAFE")
        with (
            patch.object(
                orchestration,
                "_read_explicit_artifact",
                side_effect=AssertionError("advance artifact I/O reached"),
            ),
            self.assertRaises(orchestration.FinalizationError) as advance_error,
        ):
            orchestration.advance_sources_ready(
                context,
                orchestration.AdvanceRequest(
                    (old_review / "review.commit.json",),
                    (
                        old_replay / "first.commit.json",
                        old_replay / "second.commit.json",
                    ),
                    self.root
                    / "audit/v5_2_qd/historical/execution/acquisition_checkpoints/"
                    f"{self.checkpoint.sequence:08d}-{self.checkpoint.receipt_sha256}.json",
                    self.root
                    / "data/v5_2_qd/historical/sources/source_inventory.json",
                ),
            )
        self.assertEqual(advance_error.exception.error_code, "PATH_UNSAFE")

    def test_status_and_preflight_symbols_are_public(self) -> None:
        self.assertIn("read_status", orchestration.__all__)
        self.assertIn("run_preflight", orchestration.__all__)

    def test_status_never_selects_receipts_or_infers_phase(self) -> None:
        report = orchestration.read_status(self.context)
        self.assertEqual(report.phase, "ACQUIRING")
        self.assertEqual(report.checkpoint, {
            "sequence": 9,
            "stage": "REMAINING",
            "head": "c" * 64,
            "pending_count": 0,
            "quarantine_count": 0,
        })
        self.assertTrue(report.publish_ready)
        self.assertFalse(report.review_ready)
        self.assertFalse(report.replay_ready)
        self.assertFalse(report.advance_ready)
        self.assertEqual(report.blockers, ())
        rendered = dataclasses.asdict(report)
        self.assertNotIn("selected_review_receipts", rendered)
        self.assertNotIn("selected_replay_receipts", rendered)

    def test_status_requires_authenticated_published_inventory_for_later_gates(self) -> None:
        self._published_store(self.root / "data/v5_2_qd/historical/sources")
        with patched_store_replay():
            report = orchestration.read_status(self.context)
        self.assertTrue(report.publish_ready)
        self.assertTrue(report.review_ready)
        self.assertTrue(report.replay_ready)
        self.assertFalse(report.advance_ready)

    def test_status_rejects_different_authenticated_published_inventory(self) -> None:
        source_root = self.root / "data/v5_2_qd/historical/sources"
        self._published_store(source_root)
        candidate = inventory_with_different_run_spec_binding(self.inventory)
        context = dataclasses.replace(self.context, inventory=candidate)
        self.assertNotEqual(inventory_bytes(self.inventory), inventory_bytes(candidate))
        before = tree_snapshot(self.root)
        with patched_store_replay():
            report = orchestration.read_status(context)
        self.assertEqual(report.phase, "ACQUIRING")
        self.assertFalse(report.publish_ready)
        self.assertFalse(report.review_ready)
        self.assertFalse(report.replay_ready)
        self.assertFalse(report.advance_ready)
        mismatch = tuple(
            blocker
            for blocker in report.blockers
            if blocker.code == "PUBLISHED_INVENTORY_MISMATCH"
        )
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(
            mismatch[0].paths,
            ("data/v5_2_qd/historical/sources",),
        )
        rendered = dataclasses.asdict(report)
        self.assertNotIn("selected_review_receipts", rendered)
        self.assertNotIn("selected_replay_receipts", rendered)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_status_treats_authenticated_empty_source_root_as_unpublished(self) -> None:
        source_root = self.root / "data/v5_2_qd/historical/sources"
        source_root.mkdir(parents=True)
        before = tree_snapshot(self.root)
        report = orchestration.read_status(self.context)
        self.assertTrue(report.publish_ready)
        self.assertFalse(report.review_ready)
        self.assertEqual(report.blockers, ())
        self.assertEqual(tree_snapshot(self.root), before)

    def test_status_reports_nonempty_partial_store_as_unsafe(self) -> None:
        source_root = self.root / "data/v5_2_qd/historical/sources"
        source_root.mkdir(parents=True)
        (source_root / "partial.tmp").write_bytes(b"partial\n")
        before = tree_snapshot(self.root)
        report = orchestration.read_status(self.context)
        self.assertFalse(report.publish_ready)
        self.assertFalse(report.review_ready)
        self.assertIn(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            {blocker.code for blocker in report.blockers},
        )
        self.assertEqual(tree_snapshot(self.root), before)

    def test_status_rejects_existing_source_ancestor_symlink_without_external_read(self) -> None:
        external_data = self.root / "external-data"
        self._published_store(external_data / "v5_2_qd/historical/sources")
        (self.root / "data").symlink_to(external_data, target_is_directory=True)
        before = tree_snapshot(self.root)
        before_fds = len(os.listdir("/dev/fd"))
        with (
            patched_store_replay(),
            patch.object(
                orchestration,
                "load_snapshot_store_from_fd",
                side_effect=AssertionError("external store read"),
                create=True,
            ) as loader,
        ):
            report = orchestration.read_status(self.context)
        self.assertEqual(report.phase, "ACQUIRING")
        self.assertEqual(
            (report.publish_ready, report.review_ready, report.replay_ready, report.advance_ready),
            (False, False, False, False),
        )
        self.assertIn(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            {blocker.code for blocker in report.blockers},
        )
        loader.assert_not_called()
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)
        self.assertEqual(tree_snapshot(self.root), before)

    def test_status_rejects_source_ancestor_swap_after_fd_open(self) -> None:
        data = self.root / "data"
        self._published_store(data / "v5_2_qd/historical/sources")
        external_data = self.root / "external-data"
        self._published_store(external_data / "v5_2_qd/historical/sources")
        moved = self.root / "moved-data"
        before_fds = len(os.listdir("/dev/fd"))

        def swap(stage: str) -> None:
            if stage == "after_source_root_open":
                data.rename(moved)
                data.symlink_to(external_data, target_is_directory=True)

        with (
            patched_store_replay(),
            patch.object(
                orchestration,
                "_source_status_test_hook",
                side_effect=swap,
                create=True,
            ),
            patch.object(
                orchestration,
                "load_snapshot_store_from_fd",
                side_effect=AssertionError("external store read"),
                create=True,
            ) as loader,
        ):
            report = orchestration.read_status(self.context)
        self.assertEqual(
            (report.publish_ready, report.review_ready, report.replay_ready, report.advance_ready),
            (False, False, False, False),
        )
        self.assertIn(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            {blocker.code for blocker in report.blockers},
        )
        loader.assert_not_called()
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_status_rejects_ancestor_swap_before_missing_descendant_open(self) -> None:
        data = self.root / "data"
        data.mkdir()
        external_data = self.root / "external-data"
        external_data.mkdir()
        moved = self.root / "moved-data"
        before_fds = len(os.listdir("/dev/fd"))

        def swap(component: str) -> None:
            if component == "data":
                data.rename(moved)
                data.symlink_to(external_data, target_is_directory=True)

        with patch.object(
            orchestration,
            "_trusted_descendant_test_hook",
            side_effect=swap,
            create=True,
        ):
            report = orchestration.read_status(self.context)
        self.assertEqual(
            (report.publish_ready, report.review_ready, report.replay_ready, report.advance_ready),
            (False, False, False, False),
        )
        self.assertIn(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            {blocker.code for blocker in report.blockers},
        )
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_status_rejects_ancestor_swap_after_capability_load(self) -> None:
        data = self.root / "data"
        self._published_store(data / "v5_2_qd/historical/sources")
        external_data = self.root / "external-data"
        self._published_store(external_data / "v5_2_qd/historical/sources")
        moved = self.root / "moved-data"
        before_fds = len(os.listdir("/dev/fd"))

        def swap(stage: str) -> None:
            if stage == "after_source_store_load":
                data.rename(moved)
                data.symlink_to(external_data, target_is_directory=True)

        with (
            patched_store_replay(),
            patch.object(orchestration, "_source_status_test_hook", side_effect=swap),
        ):
            report = orchestration.read_status(self.context)
        self.assertEqual(
            (report.publish_ready, report.review_ready, report.replay_ready, report.advance_ready),
            (False, False, False, False),
        )
        self.assertIn(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            {blocker.code for blocker in report.blockers},
        )
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_status_blocks_partial_or_non_acquiring_context_without_mutation(self) -> None:
        partial = dataclasses.replace(
            self.context,
            checkpoint=None,
            inventory=None,
            phase="SOURCES_READY",
            blockers=(orchestration.Blocker("PARTIAL", "partial authority"),),
        )
        report = orchestration.read_status(partial)
        self.assertEqual(report.phase, "SOURCES_READY")
        self.assertIsNone(report.checkpoint)
        self.assertEqual(
            (report.publish_ready, report.review_ready, report.replay_ready, report.advance_ready),
            (False, False, False, False),
        )
        self.assertEqual(report.blockers, partial.blockers)

    def test_preflight_is_read_only_and_returns_recomputed_digest(self) -> None:
        before = tree_snapshot(self.root)
        with patch.object(orchestration, "build_context", return_value=self.context) as rebuild:
            report = orchestration.run_preflight(self.context)
        self.assertEqual(report.status, "PASS")
        self.assertEqual(
            report.inventory_sha256,
            hashlib.sha256(inventory_bytes(self.context.inventory)).hexdigest(),
        )
        self.assertEqual(report.snapshot_count, len(self.inventory.snapshots))
        self.assertEqual(report.network_attempts, 0)
        rebuild.assert_called_once_with(
            orchestration.CommonArgs(
                root=self.root,
                run_spec=self.context.run_spec_path,
                acquisition_contract=self.context.acquisition_contract_path,
                protected_inventory=self.context.protected_inventory_path,
            )
        )
        self.assertEqual(tree_snapshot(self.root), before)
        self.assertFalse((self.root / "data/v5_2_qd/historical/sources").exists())

    def test_preflight_never_calls_write_or_git_capabilities(self) -> None:
        with (
            patch.object(orchestration, "build_context", return_value=self.context),
            patch.object(orchestration.os, "mkdir", side_effect=AssertionError("mkdir")) as mkdir,
            patch.object(orchestration.os, "write", side_effect=AssertionError("write")) as write,
            patch.object(orchestration.subprocess, "run", side_effect=AssertionError("git")) as git,
        ):
            report = orchestration.run_preflight(self.context)
        self.assertEqual(report.status, "PASS")
        mkdir.assert_not_called()
        write.assert_not_called()
        git.assert_not_called()

    def test_preflight_returns_blocked_without_rebuild(self) -> None:
        blocked = dataclasses.replace(
            self.context,
            inventory=None,
            blockers=(orchestration.Blocker("REGISTRY_ABSENT", "absent"),),
            phase="ABSENT",
        )
        with patch.object(orchestration, "build_context") as rebuild:
            report = orchestration.run_preflight(blocked)
        self.assertEqual(
            report,
            orchestration.PreflightReport("ABSENT", "BLOCKED", None, 0, 0, blocked.blockers),
        )
        rebuild.assert_not_called()

    def test_preflight_allows_already_sources_ready_context(self) -> None:
        ready = dataclasses.replace(
            self.context,
            registry=dataclasses.replace(self.registry, phase="SOURCES_READY"),
            phase="SOURCES_READY",
        )
        with patch.object(orchestration, "build_context", return_value=ready):
            report = orchestration.run_preflight(ready)
        self.assertEqual(report.phase, "SOURCES_READY")
        self.assertEqual(report.status, "PASS")

    def test_rebuild_rejects_changed_phase_registry_or_checkpoint(self) -> None:
        for changed in (
            dataclasses.replace(self.context, phase="SOURCES_READY"),
            dataclasses.replace(self.context, registry=dataclasses.replace(self.registry, sequence=8)),
            dataclasses.replace(self.context, checkpoint=dataclasses.replace(self.checkpoint, sequence=10)),
            dataclasses.replace(self.context, inventory=None),
        ):
            with self.subTest(changed=changed), patch.object(
                orchestration, "build_context", return_value=changed
            ):
                with self.assertRaises(orchestration.FinalizationError) as raised:
                    orchestration._rebuild_context_inventory(self.context)
            self.assertEqual(raised.exception.error_code, "AUTHORITY_CHANGED")
            self.assertEqual(raised.exception.exit_code, 5)

    def test_preflight_rejects_rebuilt_inventory_byte_change(self) -> None:
        rebound = dataclasses.replace(self.context, inventory=SimpleNamespace())
        with (
            patch.object(orchestration, "build_context", return_value=rebound),
            patch.object(orchestration, "inventory_bytes", side_effect=(b"first\n", b"second\n")),
        ):
            with self.assertRaises(orchestration.FinalizationError) as raised:
                orchestration.run_preflight(self.context)
        self.assertEqual(raised.exception.error_code, "AUTHORITY_CHANGED")
        self.assertEqual(raised.exception.exit_code, 5)

class PublishTests(StatusAndPreflightTests):
    """Public publication uses the preflight authority and fixed source root."""

    def _passing_preflight(self) -> orchestration.PreflightReport:
        return orchestration.PreflightReport("ACQUIRING", "PASS", "0" * 64, 81, 0, ())

    def test_publish_inventory_is_public_and_returns_a_report(self) -> None:
        publish = orchestration.publish_inventory
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ):
            report = publish(self.context, lock_owner="publish-service")
        self.assertEqual(report.status, "PUBLISHED")
        self.assertEqual(report.snapshot_count, 81)
        self.assertEqual(
            (self.root / "data/v5_2_qd/historical/sources/source_inventory.json").read_bytes(),
            inventory_bytes(self.inventory),
        )

    def test_publish_uses_context_sources_root_and_snapshot_adapter(self) -> None:
        authority = self._superseding_namespace()
        context = dataclasses.replace(
            self.context, authority_context=authority
        )
        with (
            patched_store_replay(),
            patch.object(
                orchestration,
                "run_preflight",
                return_value=self._passing_preflight(),
            ),
            patch.object(
                orchestration.v52_snapshot_store,
                "validate_expected_root",
                wraps=orchestration.v52_snapshot_store.validate_expected_root,
            ) as root_adapter,
        ):
            orchestration.publish_inventory(context, lock_owner="source-adapter-test")
        expected = authority.sources_root / "source_inventory.json"
        self.assertTrue(expected.is_file())
        self.assertFalse(
            (
                self.root
                / "data/v5_2_qd/historical/sources/source_inventory.json"
            ).exists()
        )
        root_adapter.assert_called_with(
            authority.sources_root, authority.sources_root
        )

    def test_identical_retry_reports_existing_without_changing_inventory_inode(self) -> None:
        source = self.root / "data/v5_2_qd/historical/sources/source_inventory.json"
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ):
            first = orchestration.publish_inventory(self.context, lock_owner="publish-service")
            before = (source.stat().st_ino, source.stat().st_mtime_ns, source.read_bytes())
            second = orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual(first.status, "PUBLISHED")
        self.assertEqual(second.status, "EXISTING")
        self.assertEqual(before, (source.stat().st_ino, source.stat().st_mtime_ns, source.read_bytes()))

    def test_terminal_checkpoint_and_preflight_block_publication_without_writing(self) -> None:
        pending = dataclasses.replace(self.checkpoint, pending_query_ids=("1" * 64,))
        blocked = dataclasses.replace(self.context, checkpoint=pending)
        with self.assertRaises(orchestration.FinalizationError) as terminal:
            orchestration.publish_inventory(blocked, lock_owner="publish-service")
        self.assertEqual((terminal.exception.error_code, terminal.exception.exit_code), (
            "TERMINAL_CHECKPOINT_REQUIRED", 4
        ))
        with patch.object(
            orchestration,
            "run_preflight",
            return_value=orchestration.PreflightReport("ACQUIRING", "BLOCKED", None, 0, 0, ()),
        ), self.assertRaises(orchestration.FinalizationError) as preflight:
            orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual((preflight.exception.error_code, preflight.exception.exit_code), (
            "PREFLIGHT_BLOCKED", 4
        ))
        self.assertFalse((self.root / "data").exists())

    def test_failed_publish_leaves_safe_empty_fixed_residue_and_retry_converges(self) -> None:
        before_fds = len(os.listdir("/dev/fd"))
        with patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), self.assertRaises(orchestration.FinalizationError) as raised:
            orchestration.publish_inventory(self.context, lock_owner="unsafe owner")
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "PUBLISH_FAILED", 5
        ))
        source_root = self.root / "data/v5_2_qd/historical/sources"
        self.assertTrue(source_root.is_dir())
        self.assertEqual(tuple(source_root.iterdir()), ())
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ):
            first = orchestration.publish_inventory(self.context, lock_owner="safe-retry")
            second = orchestration.publish_inventory(self.context, lock_owner="safe-retry")
        self.assertEqual((first.status, second.status), ("PUBLISHED", "EXISTING"))

    def test_source_root_open_failure_leaves_only_empty_fixed_residue_and_closes_fds(self) -> None:
        for failed_component in ("data", "v5_2_qd", "historical"):
            with self.subTest(component=failed_component), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                before_fds = len(os.listdir("/dev/fd"))

                def fail_after_component(component: str) -> None:
                    if component == failed_component:
                        raise RuntimeError(f"injected after {component}")

                with patch.object(
                    orchestration,
                    "_trusted_descendant_test_hook",
                    side_effect=fail_after_component,
                ), self.assertRaisesRegex(RuntimeError, f"injected after {failed_component}"):
                    orchestration._open_or_create_source_root(root)
                self.assertTrue((root / "data").is_dir())
                self.assertFalse(any(path.is_file() for path in root.rglob("*")))
                self.assertFalse(any(path.name == ".snapshot-store.lock" for path in root.rglob("*")))
                self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_source_root_failure_never_rmdirs_empty_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            moved = root / "owned-data-moved"

            def replace_and_fail(component: str) -> None:
                if component == "data":
                    (root / "data").rename(moved)
                    (root / "data").mkdir()
                    raise RuntimeError("injected replacement")

            with patch.object(
                orchestration,
                "_trusted_descendant_test_hook",
                side_effect=replace_and_fail,
            ), patch.object(
                orchestration.os,
                "rmdir",
                side_effect=AssertionError("rmdir must never be called"),
            ) as rmdir, self.assertRaisesRegex(RuntimeError, "injected replacement"):
                orchestration._open_or_create_source_root(root)
            rmdir.assert_not_called()
            self.assertTrue((root / "data").is_dir())
            self.assertEqual(tuple((root / "data").iterdir()), ())
            self.assertTrue(moved.is_dir())
            self.assertNotEqual((root / "data").stat().st_ino, moved.stat().st_ino)

    def test_postcondition_mismatch_is_a_stable_failure(self) -> None:
        different = inventory_with_different_run_spec_binding(self.inventory)
        with (
            patched_store_replay(),
            patch.object(
                orchestration, "run_preflight", return_value=self._passing_preflight()
            ),
            patch.object(
                orchestration,
                "load_snapshot_store_from_fd",
                return_value=SimpleNamespace(inventory=different),
            ),
            self.assertRaises(orchestration.FinalizationError) as raised,
        ):
            orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual(
            (raised.exception.error_code, raised.exception.exit_code),
            ("POSTCONDITION_FAILED", 5),
        )

    def test_unsafe_existing_inventory_and_stale_lock_fail_closed(self) -> None:
        source_root = self.root / "data/v5_2_qd/historical/sources"
        source_root.mkdir(parents=True)
        external = self.root / "external.json"
        external.write_bytes(b"external\n")
        (source_root / "source_inventory.json").symlink_to(external)
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), self.assertRaises(orchestration.FinalizationError) as unsafe:
            orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual((unsafe.exception.error_code, unsafe.exception.exit_code), (
            "PUBLISH_FAILED", 5
        ))
        (source_root / "source_inventory.json").unlink()
        (source_root / "source_inventory.json").write_bytes(b"not-an-inventory\n")
        os.link(source_root / "source_inventory.json", source_root / "inventory-alias.json")
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), self.assertRaises(orchestration.FinalizationError) as hardlink:
            orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual((hardlink.exception.error_code, hardlink.exception.exit_code), (
            "PUBLISH_FAILED", 5
        ))
        (source_root / "inventory-alias.json").unlink()
        (source_root / "source_inventory.json").unlink()
        (source_root / ".snapshot-store.lock").write_bytes(b"other-owner\n")
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), patch.object(orchestration.time, "sleep", return_value=None), self.assertRaises(
            orchestration.FinalizationError
        ) as stale:
            orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual((stale.exception.error_code, stale.exception.exit_code), (
            "PUBLISH_CONFLICT", 6
        ))

    def test_concurrent_identical_publications_converge_to_published_and_existing(self) -> None:
        import mlb_backtest.v52_snapshot_store as store

        entered = threading.Event()
        release = threading.Event()
        reports: list[orchestration.PublishReport] = []
        failures: list[BaseException] = []

        def hold_before_release(stage: str) -> None:
            if stage == "before_lock_release":
                entered.set()
                release.wait(timeout=3)

        def publish() -> None:
            try:
                reports.append(
                    orchestration.publish_inventory(self.context, lock_owner="concurrent")
                )
            except BaseException as exc:  # surfaced below in the parent test
                failures.append(exc)

        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), patch.object(store, "_publish_test_hook", side_effect=hold_before_release):
            first = threading.Thread(target=publish)
            second = threading.Thread(target=publish)
            first.start()
            self.assertTrue(entered.wait(timeout=3))
            second.start()
            time.sleep(0.04)
            release.set()
            first.join(timeout=10)
            second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(sorted(report.status for report in reports), ["EXISTING", "PUBLISHED"])

    def test_concurrent_fixed_source_root_creation_converges_after_mkdir_race(self) -> None:
        barrier = threading.Barrier(2)
        original_mkdir = orchestration.os.mkdir
        reports: list[orchestration.PublishReport] = []
        failures: list[BaseException] = []

        def raced_mkdir(name: object, *args: object, **kwargs: object) -> None:
            if name == "data":
                barrier.wait(timeout=3)
            original_mkdir(name, *args, **kwargs)

        def publish() -> None:
            try:
                reports.append(
                    orchestration.publish_inventory(self.context, lock_owner="mkdir-race")
                )
            except BaseException as exc:  # asserted below in the parent test
                failures.append(exc)

        before_fds = len(os.listdir("/dev/fd"))
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), patch.object(orchestration.os, "mkdir", side_effect=raced_mkdir):
            first = threading.Thread(target=publish)
            second = threading.Thread(target=publish)
            first.start()
            second.start()
            first.join(timeout=10)
            second.join(timeout=10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(sorted(report.status for report in reports), ["EXISTING", "PUBLISHED"])
        source = self.root / "data/v5_2_qd/historical/sources/source_inventory.json"
        before = (source.stat().st_ino, source.stat().st_mtime_ns, source.read_bytes())
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ):
            retry = orchestration.publish_inventory(self.context, lock_owner="mkdir-race")
        self.assertEqual(retry.status, "EXISTING")
        self.assertEqual(before, (source.stat().st_ino, source.stat().st_mtime_ns, source.read_bytes()))
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_concurrent_different_publication_conflicts_without_changing_winner(self) -> None:
        import mlb_backtest.v52_snapshot_store as store

        bindings = dataclasses.replace(
            self.inputs.bindings,
            run_spec=AuthorityBinding(
                self.inputs.bindings.run_spec.path, "9" * 64
            ),
        )
        other_inputs = dataclasses.replace(self.inputs, bindings=bindings)
        with patched_store_replay():
            other_inventory = build_inventory(other_inputs)
        other_context = dataclasses.replace(
            self.context, snapshot_inputs=other_inputs, inventory=other_inventory
        )
        entered = threading.Event()
        release = threading.Event()
        reports: list[orchestration.PublishReport] = []
        failures: list[BaseException] = []

        def hold_before_release(stage: str) -> None:
            if stage == "before_lock_release":
                entered.set()
                release.wait(timeout=3)

        def publish(context: orchestration.FinalizationContext) -> None:
            try:
                reports.append(
                    orchestration.publish_inventory(context, lock_owner="concurrent")
                )
            except BaseException as exc:  # asserted below in the parent test
                failures.append(exc)

        source = self.root / "data/v5_2_qd/historical/sources/source_inventory.json"
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), patch.object(store, "_publish_test_hook", side_effect=hold_before_release):
            winner = threading.Thread(target=publish, args=(self.context,))
            loser = threading.Thread(target=publish, args=(other_context,))
            winner.start()
            self.assertTrue(entered.wait(timeout=3))
            before = (source.stat().st_ino, source.stat().st_mtime_ns, source.read_bytes())
            loser.start()
            time.sleep(0.04)
            release.set()
            winner.join(timeout=10)
            loser.join(timeout=10)
        self.assertFalse(winner.is_alive())
        self.assertFalse(loser.is_alive())
        self.assertEqual([report.status for report in reports], ["PUBLISHED"])
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], orchestration.FinalizationError)
        error = failures[0]
        assert isinstance(error, orchestration.FinalizationError)
        self.assertEqual((error.error_code, error.exit_code), ("PUBLISH_CONFLICT", 6))
        self.assertEqual(before, (source.stat().st_ino, source.stat().st_mtime_ns, source.read_bytes()))


class ContextTests(_ContextBaseTests):
    def test_canonical_object_rejects_duplicate_and_noncanonical_json(self) -> None:
        with self.assertRaises(orchestration.FinalizationError) as duplicate:
            orchestration._canonical_object(b'{"a":1,"a":2}\n', "authority")
        self.assertEqual(duplicate.exception.error_code, "AUTHORITY_MALFORMED")
        with self.assertRaises(orchestration.FinalizationError):
            orchestration._canonical_object(b'{"b":2, "a":1}\n', "authority")
        self.assertEqual(
            orchestration._canonical_object(canonical_json_bytes({"a": 1}) + b"\n", "authority"),
            {"a": 1},
        )

    def test_build_context_constructs_bound_inputs_before_final_rebind(self) -> None:
        registry = ExecutionRegistryState("ACQUIRING", 4, "a" * 64, "b" * 64)
        checkpoint = AcquisitionCheckpoint(
            7, "c" * 64, None, "REMAINING", "2026-07-20T00:00:00.000000Z",
            "d" * 40, "e" * 64, (), (), (), (), (), (), (), (), (), (),
        )
        replay = SimpleNamespace(
            schedule_inventory=SimpleNamespace(
                path=Path("audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"),
                sha256="f" * 64,
            ),
            game_inventory=SimpleNamespace(
                path=Path("audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json"),
                sha256="0" * 64,
            ),
            timezone_gate=SimpleNamespace(seal_sha256="1" * 64),
        )
        inventory = object()
        order: list[str] = []

        def bind(root: Path, value: Path) -> AuthorityBinding:
            order.append(f"bind:{value.as_posix()}")
            return AuthorityBinding(value.as_posix(), "2" * 64)

        def verified(root: Path, value: Path, expected: str) -> AuthorityBinding:
            order.append(f"verified:{value.as_posix()}")
            return AuthorityBinding(value.as_posix(), expected)

        with (
            patch.object(orchestration, "_load_static_contracts") as static_contracts,
            patch.object(orchestration, "verify_protected_raw_inventory", return_value={}) as protected,
            patch.object(orchestration, "snapshot_protected_inventory_membership", return_value=()) as membership,
            patch.object(orchestration, "load_execution_registry", return_value=registry) as load_registry,
            patch.object(orchestration, "load_source_verification_checkpoint", return_value=checkpoint) as load_checkpoint,
            patch.object(orchestration, "load_snapshot_replay_inputs", return_value=replay) as load_replay,
            patch.object(orchestration, "_binding", side_effect=bind),
            patch.object(orchestration, "_verified_binding", side_effect=verified),
            patch.object(orchestration, "build_inventory", return_value=inventory) as build,
            patch.object(orchestration, "inventory_bytes", return_value=b"inventory\n"),
            patch.object(orchestration, "_git_head", return_value="3" * 40),
        ):
            context = orchestration.build_context(orchestration.CommonArgs(root=ROOT))

        self.assertEqual(context.registry, registry)
        self.assertIs(context.inventory, inventory)
        self.assertEqual(context.phase, "ACQUIRING")
        expected_bindings = [
            "bind:" + str(ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"),
            "bind:" + str(ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"),
            "verified:audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json",
            "verified:audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json",
            "verified:" + str(ROOT / "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"),
        ]
        self.assertEqual(order, expected_bindings * 2)
        self.assertEqual(build.call_count, 2)
        self.assertEqual(
            build.call_args_list[0].args[0].current_phase,
            registry.phase,
        )
        self.assertEqual(
            build.call_args_list[0].args[0].bindings.terminal_checkpoint,
            orchestration.TerminalCheckpointBinding(
                "audit/v5_2_qd/historical/execution/acquisition_checkpoints/00000007-"
                + "c" * 64 + ".json",
                "c" * 64,
                7,
                "REMAINING",
            ),
        )
        self.assertEqual(build.call_args_list[1], build.call_args_list[0])
        run_path = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        contract_path = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
        protected_path = ROOT / "audit/v5_1_bt/protected_raw_inventory.json"
        self.assertEqual(static_contracts.call_args_list, [
            call(run_path, contract_path), call(run_path, contract_path),
        ])
        self.assertEqual(protected.call_args_list, [
            call(ROOT, protected_path), call(ROOT, protected_path),
        ])
        self.assertEqual(membership.call_args_list, [call(ROOT, {}), call(ROOT, {})])
        self.assertEqual(load_registry.call_args_list, [call(ROOT, context.run_spec)] * 2)
        self.assertEqual(load_checkpoint.call_args_list, [call(ROOT, context.run_spec)] * 2)
        self.assertEqual(load_replay.call_args_list, [
            call(ROOT, context.run_spec, acquisition_contract_path=contract_path),
            call(ROOT, context.run_spec, acquisition_contract_path=contract_path),
        ])

    def test_final_rebind_rejects_each_changed_authority(self) -> None:
        registry = ExecutionRegistryState("ACQUIRING", 0, "a" * 64, "b" * 64)
        changed_registry = ExecutionRegistryState("ACQUIRING", 1, "a" * 64, "b" * 64)
        checkpoint = AcquisitionCheckpoint(
            0, "c" * 64, None, "REMAINING", "2026-07-20T00:00:00.000000Z",
            "d" * 40, "e" * 64, (), (), (), (), (), (), (), (), (), (),
        )
        changed_checkpoint = dataclasses.replace(checkpoint, sequence=1)
        replay = SimpleNamespace(
            schedule_inventory=SimpleNamespace(path=Path("schedule.json"), sha256="1" * 64),
            game_inventory=SimpleNamespace(path=Path("games.json"), sha256="2" * 64),
            timezone_gate=SimpleNamespace(seal_sha256="3" * 64),
        )
        for changed in (
            "run_spec", "contract", "run_spec_malformed", "run_spec_noncanonical",
            "contract_malformed", "contract_noncanonical", "membership", "registry",
            "checkpoint", "inventory", "git", "git_missing",
        ):
            final_run_spec = {
                "run_spec": b'{"a":2}\n',
                "run_spec_malformed": b"not-json\n",
                "run_spec_noncanonical": b'{"a":1, "b":2}\n',
            }.get(changed, b'{"a":1}\n')
            final_contract = {
                "contract": b'{"b":3}\n',
                "contract_malformed": b"not-json\n",
                "contract_noncanonical": b'{"a":1, "b":2}\n',
            }.get(changed, b'{"b":2}\n')
            with self.subTest(changed=changed), ExitStack() as stack:
                patchers = (
                    patch.object(
                    orchestration,
                    "_read_explicit_artifact",
                    side_effect=[
                        ("run.json", final_run_spec),
                        ("contract.json", final_contract),
                    ],
                    ),
                    patch.object(orchestration, "_load_static_contracts"),
                    patch.object(orchestration, "verify_protected_raw_inventory", return_value={}),
                    patch.object(
                    orchestration,
                    "snapshot_protected_inventory_membership",
                    return_value=() if changed != "membership" else (("changed",),),
                    ),
                    patch.object(
                    orchestration,
                    "load_execution_registry",
                    return_value=registry if changed != "registry" else changed_registry,
                    ),
                    patch.object(
                    orchestration,
                    "load_source_verification_checkpoint",
                    return_value=checkpoint if changed != "checkpoint" else changed_checkpoint,
                    ),
                    patch.object(orchestration, "load_snapshot_replay_inputs", return_value=replay),
                    patch.object(orchestration, "_binding", return_value=AuthorityBinding("x", "4" * 64)),
                    patch.object(orchestration, "_verified_binding", return_value=AuthorityBinding("y", "5" * 64)),
                    patch.object(orchestration, "build_inventory", return_value=object()),
                    patch.object(
                    orchestration,
                    "inventory_bytes",
                    return_value=b"inventory\n" if changed != "inventory" else b"changed\n",
                    ),
                    patch.object(
                        orchestration,
                        "_git_head",
                        side_effect=(
                            orchestration.FinalizationError(
                                "AUTHORITY_MISSING", "Git head is unavailable", (), 3
                            )
                            if changed == "git_missing"
                            else None
                        ),
                        return_value="f" * 40 if changed != "git" else "0" * 40,
                    ),
                )
                for patcher in patchers:
                    stack.enter_context(patcher)
                with self.assertRaises(orchestration.FinalizationError) as raised:
                    orchestration._rebind_context_authorities(
                        ROOT,
                        ROOT / "run.json",
                        ROOT / "contract.json",
                        ROOT / "protected.json",
                        object(),
                        registry,
                        checkpoint,
                        object(),
                        initial_run_spec_bytes=b'{"a":1}\n',
                        initial_contract_bytes=b'{"b":2}\n',
                        initial_membership=(),
                        initial_inventory_bytes=b"inventory\n",
                        initial_git_head="f" * 40,
                    )
                self.assertEqual(raised.exception.error_code, "AUTHORITY_CHANGED")
                self.assertEqual(raised.exception.exit_code, 5)

class ReviewTests(unittest.TestCase):
    """Explicit independent reviews must bind an audited selection document."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.inputs = store_inputs()
        source_root = self.root / "data/v5_2_qd/historical/sources"
        source_root.mkdir(parents=True)
        with patched_store_replay():
            self.inventory = build_inventory(self.inputs)
            publish_snapshot_store(
                source_root,
                self.inventory,
                self.inputs,
                lock_owner="review-test",
            )
        self.registry = ExecutionRegistryState("ACQUIRING", 7, "a" * 64, "b" * 64)
        self.checkpoint = AcquisitionCheckpoint(
            9, "c" * 64, "d" * 64, "REMAINING",
            "2026-07-20T00:00:00.000000Z", "e" * 40, "f" * 64,
            (), (), (), (), (), (), (), (), (), (),
        )
        self.context = orchestration.FinalizationContext(
            self.root,
            self.root / "run.json",
            self.root / "contract.json",
            self.root / "protected.json",
            SimpleNamespace(run_id="fixture-run"),
            self.registry,
            self.checkpoint,
            self.inputs,
            self.inventory,
            "ACQUIRING",
            "e" * 40,
            (),
        )
        self.query_ids = self.inventory.logical_query_ids[:2]
        self.query_ids_path = (
            self.root
            / "audit/v5_2_qd/historical/acquisition/source_reviews/query_ids.json"
        )
        self.query_ids_path.parent.mkdir(parents=True)
        self.query_ids_path.write_bytes(
            canonical_json_bytes({"query_ids": list(self.query_ids)}) + b"\n"
        )
        self.receipt_path = (
            self.root
            / "audit/v5_2_qd/historical/acquisition/source_reviews/review.json"
        )
        self.commit_path = self.receipt_path.with_name("review.commit.json")
        self.request = orchestration.ReviewRequest(
            "independent-source-auditor",
            "independent-source-auditor",
            "APPROVED",
            self.query_ids_path,
            self.receipt_path,
            self.commit_path,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _write(
        self,
        request: object | None = None,
        *,
        validator_side_effect: object | None = None,
        preflight_side_effect: object | None = None,
    ) -> object:
        preflight = orchestration.PreflightReport(
            "ACQUIRING",
            "PASS",
            hashlib.sha256(inventory_bytes(self.inventory)).hexdigest(),
            len(self.inventory.snapshots),
            0,
            (),
        )
        with (
            patched_store_replay(),
            patch.object(
                orchestration.execution_registry,
                "validate_source_receipt_schema",
                create=True,
            ) as validator,
            patch.object(orchestration, "_utc_now", return_value="2026-07-20T00:00:00.000000Z", create=True),
            patch.object(
                orchestration,
                "run_preflight",
                side_effect=preflight_side_effect or (lambda _context: preflight),
            ),
        ):
            validator.side_effect = validator_side_effect
            return orchestration.write_review(self.context, request or self.request)

    def test_review_receipt_binds_explicit_query_document(self) -> None:
        report = self._write()
        payload = json.loads(report.path.read_bytes())
        self.assertEqual(payload["query_ids_document"], {
            "path": self.query_ids_path.relative_to(self.root).as_posix(),
            "sha256": hashlib.sha256(self.query_ids_path.read_bytes()).hexdigest(),
        })
        self.assertEqual(payload["reviewed_query_ids"], list(self.query_ids))
        self.assertEqual(payload["decision"], "APPROVED")
        self.assertEqual(
            set(payload),
            {
                "schema_version", "receipt_kind", "run_id", "critical_errors",
                "reviewer_identity", "reviewer_role", "decision", "reviewed_query_ids",
                "query_ids_document", "inventory_sha256", "snapshot_sha256s", "run_spec",
                "acquisition_contract", "git_head", "occurred_at_utc",
            },
        )
        self.assertEqual(report.receipt_kind, "SOURCE_REVIEW")
        self.assertEqual(report.commit_path, self.commit_path)
        marker = json.loads(report.commit_path.read_bytes())
        self.assertEqual(marker["receipt_kind"], "SOURCE_RECEIPT_COMMIT")
        self.assertEqual(marker["committed_receipt_kind"], "SOURCE_REVIEW")
        self.assertEqual(marker["source_receipt"], {
            "path": self.receipt_path.relative_to(self.root).as_posix(),
            "sha256": report.sha256,
        })
        self.assertEqual(
            report.commit_sha256,
            hashlib.sha256(report.commit_path.read_bytes()).hexdigest(),
        )

    def test_review_allows_rejected_decision(self) -> None:
        request = dataclasses.replace(self.request, decision="REJECTED")
        report = self._write(request)
        self.assertEqual(json.loads(report.path.read_bytes())["decision"], "REJECTED")

    def test_review_rejects_invalid_reviewer_before_writing(self) -> None:
        for field in ("reviewer_identity", "reviewer_role"):
            with self.subTest(field=field):
                request = dataclasses.replace(self.request, **{field: "source-operator"})
                with self.assertRaisesRegex(orchestration.FinalizationError, "reviewer") as raised:
                    self._write(request)
                self.assertEqual(raised.exception.error_code, "REVIEWER_INVALID")
                self.assertFalse(self.receipt_path.exists())

    def test_review_rejects_noncanonical_or_unknown_query_document(self) -> None:
        for query_ids in (
            list(reversed(self.query_ids)),
            [self.query_ids[0], self.query_ids[0]],
            ["f" * 64],
            ["not-a-sha"],
        ):
            with self.subTest(query_ids=query_ids):
                self.query_ids_path.write_bytes(
                    canonical_json_bytes({"query_ids": query_ids}) + b"\n"
                )
                with self.assertRaises(orchestration.FinalizationError) as raised:
                    self._write()
                self.assertEqual(raised.exception.error_code, "QUERY_IDS_INVALID")
                self.assertFalse(self.receipt_path.exists())

    def test_review_rejects_outside_namespace_and_existing_path(self) -> None:
        outside = dataclasses.replace(
            self.request,
            write_receipt=self.root / "audit/v5_2_qd/historical/acquisition/review.json",
        )
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write(outside)
        self.assertEqual(raised.exception.error_code, "PATH_UNSAFE")
        self._write()
        with self.assertRaises(orchestration.FinalizationError) as conflict:
            self._write()
        self.assertEqual(conflict.exception.exit_code, 6)

    def test_review_commit_marker_conflict_leaves_uncommitted_receipt(self) -> None:
        existing = b'{"existing":true}\n'
        self.commit_path.write_bytes(existing)
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual(raised.exception.exit_code, 6)
        self.assertTrue(self.receipt_path.is_file())
        self.assertEqual(self.commit_path.read_bytes(), existing)

    def test_review_requires_distinct_receipt_and_commit_paths(self) -> None:
        request = dataclasses.replace(
            self.request, write_commit=self.request.write_receipt
        )
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write(request)
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "PATH_UNSAFE", 5
        ))
        self.assertFalse(self.receipt_path.exists())

    def test_review_rejects_symlinked_receipt_ancestor(self) -> None:
        external = self.root / "external-receipts"
        external.mkdir()
        swapped = self.receipt_path.parent / "swapped"
        swapped.symlink_to(external, target_is_directory=True)
        request = dataclasses.replace(
            self.request, write_receipt=swapped / "review.json"
        )
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write(request)
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "PATH_UNSAFE", 5
        ))
        self.assertEqual(list(external.iterdir()), [])

    def test_review_requires_authenticated_published_inventory(self) -> None:
        shutil.rmtree(self.root / "data")
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "PREFLIGHT_BLOCKED", 4
        ))
        self.assertFalse(self.receipt_path.exists())

    def test_review_rejects_unsafe_query_document_file_types(self) -> None:
        external = self.root / "external.json"
        external.write_bytes(self.query_ids_path.read_bytes())
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                candidate = self.query_ids_path.with_name(f"{kind}.json")
                if kind == "symlink":
                    candidate.symlink_to(external)
                elif kind == "hardlink":
                    os.link(self.query_ids_path, candidate)
                else:
                    os.mkfifo(candidate)
                request = dataclasses.replace(self.request, query_ids_file=candidate)
                with self.assertRaises(orchestration.FinalizationError) as raised:
                    self._write(request)
                self.assertEqual(raised.exception.error_code, "QUERY_IDS_INVALID")

    def test_review_rejects_oversized_query_document_without_fd_leak(self) -> None:
        with self.query_ids_path.open("wb") as handle:
            handle.seek(orchestration._MAX_QUERY_IDS_DOCUMENT_BYTES)
            handle.write(b"x")
        before_fds = len(os.listdir("/dev/fd"))
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "QUERY_IDS_INVALID", 3
        ))
        self.assertFalse(self.receipt_path.exists())
        self.assertEqual(len(os.listdir("/dev/fd")), before_fds)

    def test_review_schema_failure_never_creates_a_receipt(self) -> None:
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write(validator_side_effect=ValueError("schema drift"))
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "POSTCONDITION_FAILED", 5
        ))
        self.assertFalse(self.receipt_path.exists())

    def test_review_rebinds_dependencies_before_receipt_write(self) -> None:
        def mutate_after_schema(
            _root: Path, *, schema_name: str, **_kwargs: object
        ) -> None:
            if schema_name == "v5_2_source_review_receipt.schema.json":
                self.query_ids_path.write_bytes(
                    canonical_json_bytes(
                        {"query_ids": list(reversed(self.query_ids))}
                    )
                    + b"\n"
                )

        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write(validator_side_effect=mutate_after_schema)
        self.assertEqual(
            (raised.exception.error_code, raised.exception.exit_code),
            ("POSTCONDITION_FAILED", 5),
        )
        self.assertFalse(self.receipt_path.exists())
        self.assertFalse(self.commit_path.exists())

    def test_review_document_tamper_during_final_rebind_fails_closed(self) -> None:
        preflight = orchestration.PreflightReport(
            "ACQUIRING", "PASS", "x" * 64, len(self.inventory.snapshots), 0, ()
        )
        write_exclusive = orchestration._write_receipt_exclusive

        def mutate_after_write(
            root: Path, path: Path, raw: bytes, expected_prefix: str
        ) -> str:
            relative = write_exclusive(root, path, raw, expected_prefix)
            if path == self.receipt_path:
                self.query_ids_path.write_bytes(
                    canonical_json_bytes({"query_ids": list(reversed(self.query_ids))})
                    + b"\n"
                )
            return relative

        with (
            patch.object(
                orchestration,
                "_write_receipt_exclusive",
                side_effect=mutate_after_write,
            ),
            self.assertRaises(orchestration.FinalizationError) as raised,
        ):
            self._write(preflight_side_effect=lambda _context: preflight)
        self.assertEqual(
            (raised.exception.error_code, raised.exception.exit_code),
            ("POSTCONDITION_FAILED", 5),
        )
        self.assertTrue(self.receipt_path.is_file())
        self.assertFalse(self.commit_path.exists())
        payload = json.loads(self.receipt_path.read_bytes())
        self.assertEqual(payload["reviewed_query_ids"], list(self.query_ids))
        self.assertEqual(
            payload["query_ids_document"]["sha256"],
            hashlib.sha256(
                canonical_json_bytes({"query_ids": list(self.query_ids)}) + b"\n"
            ).hexdigest(),
        )

    def test_review_rebinds_receipt_inode_and_bytes_immediately_before_marker(self) -> None:
        for attack in ("tamper", "replace"):
            with self.subTest(attack=attack):
                displaced = self.receipt_path.with_name("review.displaced.json")
                for candidate in (self.receipt_path, self.commit_path, displaced):
                    if candidate.exists():
                        candidate.unlink()

                def attack_before_marker(
                    _root: Path, *, schema_name: str, **_kwargs: object
                ) -> None:
                    if schema_name != "v5_2_source_receipt_commit.schema.json":
                        return
                    if attack == "tamper":
                        self.receipt_path.write_bytes(b"attacker-tamper\n")
                    else:
                        self.receipt_path.rename(displaced)
                        self.receipt_path.write_bytes(b"attacker-replacement\n")

                with self.assertRaises(orchestration.FinalizationError) as raised:
                    self._write(validator_side_effect=attack_before_marker)
                self.assertEqual(
                    (raised.exception.error_code, raised.exception.exit_code),
                    ("POSTCONDITION_FAILED", 5),
                )
                self.assertFalse(self.commit_path.exists())
                self.assertEqual(
                    self.receipt_path.read_bytes(),
                    (
                        b"attacker-tamper\n"
                        if attack == "tamper"
                        else b"attacker-replacement\n"
                    ),
                )
                if attack == "replace":
                    self.assertTrue(displaced.is_file())
                self.receipt_path.unlink()
                if displaced.exists():
                    displaced.unlink()

    def test_review_rebinds_source_receipt_after_marker_write(self) -> None:
        """A post-O_EXCL source-receipt swap cannot leave a trusted marker."""
        write_exclusive = orchestration._write_receipt_exclusive

        def swap_after_marker(
            root: Path, path: Path, raw: bytes, expected_prefix: str
        ) -> str:
            relative = write_exclusive(root, path, raw, expected_prefix)
            if path == self.commit_path:
                self.receipt_path.write_bytes(b"attacker-replacement\n")
            return relative

        with patch.object(
            orchestration, "_write_receipt_exclusive", side_effect=swap_after_marker
        ), self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual(
            (raised.exception.error_code, raised.exception.exit_code),
            ("POSTCONDITION_FAILED", 5),
        )
        self.assertTrue(self.commit_path.is_file())
        self.assertEqual(self.receipt_path.read_bytes(), b"attacker-replacement\n")

    def test_same_explicit_receipt_path_allows_one_concurrent_writer(self) -> None:
        outcomes: list[object] = []
        gate = threading.Barrier(2)
        preflight = orchestration.PreflightReport(
            "ACQUIRING",
            "PASS",
            hashlib.sha256(inventory_bytes(self.inventory)).hexdigest(),
            len(self.inventory.snapshots),
            0,
            (),
        )

        def write_once() -> None:
            gate.wait()
            try:
                outcomes.append(orchestration.write_review(self.context, self.request))
            except orchestration.FinalizationError as exc:
                outcomes.append(exc)

        with (
            patched_store_replay(),
            patch.object(orchestration.execution_registry, "validate_source_receipt_schema"),
            patch.object(orchestration, "_utc_now", return_value="2026-07-20T00:00:00.000000Z"),
            patch.object(orchestration, "run_preflight", return_value=preflight),
        ):
            threads = [threading.Thread(target=write_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(isinstance(item, orchestration.ReceiptReport) for item in outcomes), 1)
        failures = [item for item in outcomes if isinstance(item, orchestration.FinalizationError)]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].exit_code, 6)


class ReplayTests(unittest.TestCase):
    """Clean replay receipts are explicit, immutable, and network-free."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.inputs = store_inputs()
        source_root = self.root / "data/v5_2_qd/historical/sources"
        source_root.mkdir(parents=True)
        with patched_store_replay():
            self.inventory = build_inventory(self.inputs)
            publish_snapshot_store(
                source_root, self.inventory, self.inputs, lock_owner="replay-test"
            )
        self.registry = ExecutionRegistryState("ACQUIRING", 7, "a" * 64, "b" * 64)
        self.checkpoint = AcquisitionCheckpoint(
            9, "c" * 64, "d" * 64, "REMAINING",
            "2026-07-20T00:00:00.000000Z", "e" * 40, "f" * 64,
            (), (), (), (), (), (), (), (), (), (),
        )
        self.context = orchestration.FinalizationContext(
            self.root,
            self.root / "run.json",
            self.root / "contract.json",
            self.root / "protected.json",
            SimpleNamespace(run_id="fixture-run"),
            self.registry,
            self.checkpoint,
            self.inputs,
            self.inventory,
            "ACQUIRING",
            "e" * 40,
            (),
        )
        replay_root = self.root / "audit/v5_2_qd/historical/acquisition/source_replays"
        self.first_path = replay_root / "first.json"
        self.first_commit = replay_root / "first.commit.json"
        self.second_path = replay_root / "second.json"
        self.second_commit = replay_root / "second.commit.json"
        self.request = orchestration.ReplayRequest(self.first_path, self.first_commit)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _write(self, request: object | None = None) -> object:
        preflight = orchestration.PreflightReport(
            "ACQUIRING",
            "PASS",
            hashlib.sha256(inventory_bytes(self.inventory)).hexdigest(),
            len(self.inventory.snapshots),
            0,
            (),
        )
        status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
        with (
            patched_store_replay(),
            patch.object(orchestration.execution_registry, "validate_source_receipt_schema"),
            patch.object(orchestration, "read_status", return_value=status),
            patch.object(orchestration, "run_preflight", return_value=preflight),
            patch.object(
                orchestration,
                "_utc_now",
                return_value="2026-07-20T00:00:00.000000Z",
            ),
        ):
            return orchestration.write_replay(self.context, request or self.request)

    def test_replay_emits_exact_payload_and_commit_marker(self) -> None:
        report = self._write()
        payload = json.loads(report.path.read_bytes())
        self.assertEqual(
            set(payload),
            {
                "schema_version", "receipt_kind", "run_id", "critical_errors",
                "execution_id", "status", "network_attempts", "reviewed_query_ids",
                "inventory_sha256", "snapshot_sha256s", "run_spec",
                "acquisition_contract", "git_head", "recorded_at_utc",
            },
        )
        self.assertEqual(payload["receipt_kind"], "CLEAN_SOURCE_REPLAY")
        self.assertEqual(payload["network_attempts"], 0)
        self.assertEqual(payload["reviewed_query_ids"], list(self.inventory.logical_query_ids))
        self.assertEqual(report.receipt_kind, "CLEAN_SOURCE_REPLAY")
        self.assertEqual(report.commit_path, self.first_commit)
        marker = json.loads(report.commit_path.read_bytes())
        self.assertEqual(set(marker), {
            "schema_version", "receipt_kind", "committed_receipt_kind", "run_id",
            "critical_errors", "source_receipt", "run_spec", "acquisition_contract",
            "git_head", "occurred_at_utc",
        })
        self.assertEqual(marker["committed_receipt_kind"], "CLEAN_SOURCE_REPLAY")
        self.assertEqual(marker["source_receipt"], {
            "path": self.first_path.relative_to(self.root).as_posix(),
            "sha256": report.sha256,
        })

    def test_two_replays_have_distinct_execution_ids_and_equal_projection(self) -> None:
        first = self._write()
        second = self._write(orchestration.ReplayRequest(self.second_path, self.second_commit))
        first_payload = json.loads(first.path.read_bytes())
        second_payload = json.loads(second.path.read_bytes())
        self.assertNotEqual(first_payload["execution_id"], second_payload["execution_id"])
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertEqual(first_payload["inventory_sha256"], second_payload["inventory_sha256"])
        self.assertEqual(first_payload["network_attempts"], 0)
        self.assertEqual(second_payload["network_attempts"], 0)

    def test_replay_rejects_existing_or_unsafe_destinations_before_writing(self) -> None:
        self.first_path.parent.mkdir(parents=True)
        self.first_path.write_bytes(b'{"existing":true}\n')
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "RECEIPT_CONFLICT", 6
        ))
        same = orchestration.ReplayRequest(self.second_path, self.second_path)
        with self.assertRaises(orchestration.FinalizationError) as unsafe:
            self._write(same)
        self.assertEqual((unsafe.exception.error_code, unsafe.exception.exit_code), (
            "PATH_UNSAFE", 5
        ))

    def test_replay_requires_published_acquiring_authority(self) -> None:
        blocked = orchestration.StatusReport("ACQUIRING", None, True, True, False, False, ())
        with (
            patched_store_replay(),
            patch.object(orchestration, "read_status", return_value=blocked),
            self.assertRaises(orchestration.FinalizationError) as raised,
        ):
            orchestration.write_replay(self.context, self.request)
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "PREFLIGHT_BLOCKED", 4
        ))
        self.assertFalse(self.first_path.exists())

    def test_replay_commit_conflict_leaves_the_uncommitted_receipt(self) -> None:
        self.first_commit.parent.mkdir(parents=True)
        self.first_commit.write_bytes(b'{"existing":true}\n')
        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "RECEIPT_CONFLICT", 6
        ))
        self.assertTrue(self.first_path.is_file())
        self.assertEqual(self.first_commit.read_bytes(), b'{"existing":true}\n')

    def test_replay_mismatch_leaves_no_receipt_or_marker(self) -> None:
        with (
            patch.object(orchestration, "read_status", return_value=orchestration.StatusReport(
                "ACQUIRING", None, True, True, True, False, ()
            )),
            patch.object(orchestration, "run_preflight", return_value=orchestration.PreflightReport(
                "ACQUIRING", "PASS", "x" * 64, len(self.inventory.snapshots), 0, ()
            )),
            patch.object(orchestration, "replay_verified_snapshots", return_value=()),
            self.assertRaises(orchestration.FinalizationError) as raised,
        ):
            orchestration.write_replay(self.context, self.request)
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "REPLAY_MISMATCH", 5
        ))
        self.assertFalse(self.first_path.exists())
        self.assertFalse(self.first_commit.exists())

    def test_replay_rebinds_dependencies_before_receipt_write(self) -> None:
        source_inventory = (
            self.root
            / "data/v5_2_qd/historical/sources/source_inventory.json"
        )
        preflight = orchestration.PreflightReport(
            "ACQUIRING",
            "PASS",
            hashlib.sha256(inventory_bytes(self.inventory)).hexdigest(),
            len(self.inventory.snapshots),
            0,
            (),
        )
        status = orchestration.StatusReport(
            "ACQUIRING", None, True, True, True, False, ()
        )

        def mutate_after_schema(
            _root: Path, *, schema_name: str, **_kwargs: object
        ) -> None:
            if schema_name == "v5_2_clean_source_replay_receipt.schema.json":
                source_inventory.write_bytes(b"attacker replacement\n")

        with (
            patched_store_replay(),
            patch.object(
                orchestration.execution_registry,
                "validate_source_receipt_schema",
                side_effect=mutate_after_schema,
            ),
            patch.object(orchestration, "read_status", return_value=status),
            patch.object(orchestration, "run_preflight", return_value=preflight),
            self.assertRaises(orchestration.FinalizationError) as raised,
        ):
            orchestration.write_replay(self.context, self.request)
        self.assertEqual(
            (raised.exception.error_code, raised.exception.exit_code),
            ("REPLAY_MISMATCH", 5),
        )
        self.assertFalse(self.first_path.exists())
        self.assertFalse(self.first_commit.exists())

    def test_execution_id_is_uuid4_and_precedes_replay_loader(self) -> None:
        order: list[str] = []
        actual = orchestration.replay_verified_snapshots

        def generated() -> str:
            order.append("id")
            return "12345678-1234-4abc-8def-123456789abc"

        def replayed(inputs: object) -> object:
            order.append("replay")
            return actual(inputs)

        with (
            patch.object(orchestration, "_execution_id", side_effect=generated),
            patch.object(orchestration, "replay_verified_snapshots", side_effect=replayed),
        ):
            report = self._write()
        self.assertEqual(order[0], "id")
        self.assertEqual(json.loads(report.path.read_bytes())["execution_id"],
                         "12345678-1234-4abc-8def-123456789abc")
        self.assertRegex(orchestration._execution_id(),
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

    def test_replay_uses_no_network_entry_point(self) -> None:
        with (
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("network")),
            patch.object(socket, "socket", side_effect=AssertionError("network")),
        ):
            self._write()

    def test_replay_rebinds_receipt_inode_and_bytes_before_marker(self) -> None:
        displaced = self.first_path.with_name("first.displaced.json")

        def attack_before_marker(
            _root: Path, *, schema_name: str, **_kwargs: object
        ) -> None:
            if schema_name != "v5_2_source_receipt_commit.schema.json":
                return
            self.first_path.rename(displaced)
            self.first_path.write_bytes(b"attacker-replacement\n")

        preflight = orchestration.PreflightReport(
            "ACQUIRING", "PASS", "x" * 64, len(self.inventory.snapshots), 0, ()
        )
        status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
        with (
            patched_store_replay(),
            patch.object(
                orchestration.execution_registry,
                "validate_source_receipt_schema",
                side_effect=attack_before_marker,
            ),
            patch.object(orchestration, "read_status", return_value=status),
            patch.object(orchestration, "run_preflight", return_value=preflight),
            self.assertRaises(orchestration.FinalizationError) as raised,
        ):
            orchestration.write_replay(self.context, self.request)
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "POSTCONDITION_FAILED", 5
        ))
        self.assertFalse(self.first_commit.exists())
        self.assertEqual(self.first_path.read_bytes(), b"attacker-replacement\n")
        self.assertTrue(displaced.is_file())

    def test_replay_rebinds_source_receipt_after_marker_write(self) -> None:
        """The replay marker repeats its dependency rebind after O_EXCL write."""
        write_exclusive = orchestration._write_receipt_exclusive

        def swap_after_marker(
            root: Path, path: Path, raw: bytes, expected_prefix: str
        ) -> str:
            relative = write_exclusive(root, path, raw, expected_prefix)
            if path == self.first_commit:
                self.first_path.write_bytes(b"attacker-replacement\n")
            return relative

        with patch.object(
            orchestration, "_write_receipt_exclusive", side_effect=swap_after_marker
        ), self.assertRaises(orchestration.FinalizationError) as raised:
            self._write()
        self.assertEqual(
            (raised.exception.error_code, raised.exception.exit_code),
            ("POSTCONDITION_FAILED", 5),
        )
        self.assertTrue(self.first_commit.is_file())
        self.assertEqual(self.first_path.read_bytes(), b"attacker-replacement\n")

    def test_same_paths_allow_one_concurrent_writer(self) -> None:
        outcomes: list[object] = []
        gate = threading.Barrier(2)
        preflight = orchestration.PreflightReport(
            "ACQUIRING",
            "PASS",
            hashlib.sha256(inventory_bytes(self.inventory)).hexdigest(),
            len(self.inventory.snapshots),
            0,
            (),
        )
        status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())

        def write_once() -> None:
            gate.wait()
            try:
                outcomes.append(orchestration.write_replay(self.context, self.request))
            except orchestration.FinalizationError as exc:
                outcomes.append(exc)

        with (
            patched_store_replay(),
            patch.object(orchestration.execution_registry, "validate_source_receipt_schema"),
            patch.object(orchestration, "read_status", return_value=status),
            patch.object(orchestration, "run_preflight", return_value=preflight),
            patch.object(
                orchestration,
                "_utc_now",
                return_value="2026-07-20T00:00:00.000000Z",
            ),
        ):
            threads = [threading.Thread(target=write_once) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sum(isinstance(item, orchestration.ReplayReport) for item in outcomes), 1)
        failures = [item for item in outcomes if isinstance(item, orchestration.FinalizationError)]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].exit_code, 6)


class ReplayGitIntegrationTests(unittest.TestCase):
    def test_write_replay_uses_real_git_bound_schema_validator(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            root, run_spec = fixture.make_repo(Path(directory))
            inputs = store_inputs()
            with patched_store_replay():
                inventory = dataclasses.replace(
                    build_inventory(inputs), run_id=run_spec.run_id
                )
            registry = ExecutionRegistryState("ACQUIRING", 7, "a" * 64, "b" * 64)
            checkpoint = AcquisitionCheckpoint(
                9, "c" * 64, "d" * 64, "REMAINING",
                "2026-07-20T00:00:00.000000Z", "e" * 40, "f" * 64,
                (), (), (), (), (), (), (), (), (), (),
            )
            git_head = subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=root, check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            context = orchestration.FinalizationContext(
                root,
                root / "protocols/v5_2_qd/historical_walkforward_500.json",
                root / "protocols/v5_2_qd/historical_source_acquisition.json",
                root / "audit/v5_1_bt/protected_raw_inventory.json",
                run_spec,
                registry,
                checkpoint,
                inputs,
                inventory,
                "ACQUIRING",
                git_head,
                (),
            )
            source_root = root / "data/v5_2_qd/historical/sources"
            source_root.mkdir(parents=True)
            request = orchestration.ReplayRequest(
                root / "audit/v5_2_qd/historical/acquisition/source_replays/replay.json",
                root / "audit/v5_2_qd/historical/acquisition/source_replays/replay.commit.json",
            )
            preflight = orchestration.PreflightReport(
                "ACQUIRING", "PASS", hashlib.sha256(inventory_bytes(inventory)).hexdigest(),
                len(inventory.snapshots), 0, (),
            )
            status = orchestration.StatusReport("ACQUIRING", None, True, True, True, False, ())
            loaded = LoadedSnapshotStore(inventory, ())
            validator = orchestration.execution_registry.validate_source_receipt_schema
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                patch.object(orchestration, "load_snapshot_store_from_fd", return_value=loaded),
                patch.object(orchestration, "replay_verified_snapshots", return_value=()),
                patch.object(orchestration, "build_inventory", return_value=inventory),
                patch.object(
                    orchestration.execution_registry,
                    "validate_source_receipt_schema", wraps=validator,
                ) as validated,
            ):
                report = orchestration.write_replay(context, request)
            self.assertEqual(report.path, request.write_receipt)
            self.assertEqual(validated.call_count, 2)
            self.assertEqual(validated.call_args_list[0].kwargs["schema_name"],
                             "v5_2_clean_source_replay_receipt.schema.json")
            self.assertEqual(validated.call_args_list[1].kwargs["schema_name"],
                             "v5_2_source_receipt_commit.schema.json")


class ReviewGitIntegrationTests(unittest.TestCase):
    def test_write_review_uses_real_clean_git_bound_schema_validator(self) -> None:
        from tests.test_execution_registry import ExecutionRegistryTest

        with tempfile.TemporaryDirectory() as directory:
            fixture = ExecutionRegistryTest()
            fixture.setUp()
            root, run_spec = fixture.make_repo(Path(directory))
            inputs = store_inputs()
            with patched_store_replay():
                inventory = dataclasses.replace(
                    build_inventory(inputs), run_id=run_spec.run_id
                )
            registry = ExecutionRegistryState("ACQUIRING", 7, "a" * 64, "b" * 64)
            checkpoint = AcquisitionCheckpoint(
                9, "c" * 64, "d" * 64, "REMAINING",
                "2026-07-20T00:00:00.000000Z", "e" * 40, "f" * 64,
                (), (), (), (), (), (), (), (), (), (),
            )
            git_head = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            context = orchestration.FinalizationContext(
                root,
                root / "protocols/v5_2_qd/historical_walkforward_500.json",
                root / "protocols/v5_2_qd/historical_source_acquisition.json",
                root / "audit/v5_1_bt/protected_raw_inventory.json",
                run_spec,
                registry,
                checkpoint,
                inputs,
                inventory,
                "ACQUIRING",
                git_head,
                (),
            )
            query_ids = inventory.logical_query_ids[:2]
            selection = (
                root
                / "audit/v5_2_qd/historical/acquisition/source_reviews/query_ids.json"
            )
            selection.parent.mkdir(parents=True)
            selection.write_bytes(
                canonical_json_bytes({"query_ids": list(query_ids)}) + b"\n"
            )
            receipt = selection.with_name("review.json")
            commit = selection.with_name("review.commit.json")
            request = orchestration.ReviewRequest(
                "independent-source-auditor",
                "independent-source-auditor",
                "APPROVED",
                selection,
                receipt,
                commit,
            )
            preflight = orchestration.PreflightReport(
                "ACQUIRING",
                "PASS",
                hashlib.sha256(inventory_bytes(inventory)).hexdigest(),
                len(inventory.snapshots),
                0,
                (),
            )
            status = orchestration.StatusReport(
                "ACQUIRING", None, True, True, True, False, ()
            )
            validator = orchestration.execution_registry.validate_source_receipt_schema
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                patch.object(
                    orchestration,
                    "_utc_now",
                    return_value="2026-07-20T00:00:00.000000Z",
                ),
                patch.object(
                    orchestration.execution_registry,
                    "validate_source_receipt_schema",
                    wraps=validator,
                ) as validated,
            ):
                report = orchestration.write_review(context, request)
            self.assertEqual(report.path, receipt)
            self.assertEqual(validated.call_count, 2)
            call_kwargs = validated.call_args_list[0].kwargs
            self.assertEqual(
                call_kwargs["schema_name"],
                "v5_2_source_review_receipt.schema.json",
            )
            self.assertEqual(
                call_kwargs["payload"], json.loads(receipt.read_bytes())
            )
            self.assertEqual(
                validated.call_args_list[1].kwargs["schema_name"],
                "v5_2_source_receipt_commit.schema.json",
            )

            def validate_with_extra(*args: object, **kwargs: object) -> None:
                altered = dict(kwargs["payload"])
                altered["unexpected"] = True
                validator(
                    args[0],
                    git_head=kwargs["git_head"],
                    schema_name=kwargs["schema_name"],
                    payload=altered,
                )

            extra_request = dataclasses.replace(
                request, write_receipt=selection.with_name("extra.json")
            )
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                patch.object(
                    orchestration.execution_registry,
                    "validate_source_receipt_schema",
                    side_effect=validate_with_extra,
                ),
                self.assertRaises(orchestration.FinalizationError) as extra_error,
            ):
                orchestration.write_review(context, extra_request)
            self.assertEqual(extra_error.exception.exit_code, 5)
            self.assertFalse(extra_request.write_receipt.exists())

            schema_path = root / "schemas/v5_2_source_review_receipt.schema.json"
            schema_path.write_bytes(b"{}\n")
            drift_request = dataclasses.replace(
                request, write_receipt=selection.with_name("schema-drift.json")
            )
            with (
                patch.object(orchestration, "read_status", return_value=status),
                patch.object(orchestration, "run_preflight", return_value=preflight),
                self.assertRaises(orchestration.FinalizationError) as drift_error,
            ):
                orchestration.write_review(context, drift_request)
            self.assertEqual(drift_error.exception.exit_code, 5)
            self.assertFalse(drift_request.write_receipt.exists())


if __name__ == "__main__":
    unittest.main()
