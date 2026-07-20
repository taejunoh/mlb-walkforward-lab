from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from mlb_backtest import v52_source_orchestration as orchestration
from mlb_backtest.execution_registry import AcquisitionCheckpoint, ExecutionRegistryState
from mlb_backtest.v52_snapshot_store import (
    AuthorityBinding,
    LoadedSnapshotStore,
    build_inventory,
    inventory_bytes,
    publish_snapshot_store,
)
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from tests.v52_snapshot_store_fixture import patched_store_replay, store_inputs


ROOT = Path(__file__).resolve().parents[1]


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
                patch.object(orchestration.execution_registry, "advance_execution_phase", return_value=advanced) as advance,
            ):
                report = orchestration.advance_sources_ready(context, request)
            self.assertEqual(report, orchestration.AdvanceReport("ADVANCED", "SOURCES_READY", "d" * 64))
            advance.assert_called_once_with(
                root, context.run_spec, expected_phase="ACQUIRING", next_phase="SOURCES_READY",
                inventory_path=inventory_path, validation_receipts=tuple(markers),
            )
            validate.assert_called_once_with(
                root, context.run_spec, inventory_path=inventory_path,
                checkpoint_path=checkpoint_path, validation_receipts=tuple(markers),
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
        before = tree_snapshot(ROOT)
        context = orchestration.build_context(orchestration.CommonArgs(root=ROOT))
        self.assertEqual(context.phase, "ABSENT")
        self.assertIsNone(context.registry)
        self.assertIsNone(context.checkpoint)
        self.assertIsNone(context.snapshot_inputs)
        self.assertIsNone(context.inventory)
        self.assertIn("REGISTRY_ABSENT", {item.code for item in context.blockers})
        self.assertEqual(tree_snapshot(ROOT), before)
        self.assertFalse((ROOT / "audit/v5_2_qd/historical/finalization").exists())

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
                patch.object(orchestration.execution_registry, "advance_execution_phase") as advance,
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
                    patch.object(orchestration.execution_registry, "advance_execution_phase") as advance,
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
                patch.object(orchestration.execution_registry, "advance_execution_phase") as advance,
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
                    patch.object(orchestration.execution_registry, "advance_execution_phase", return_value=advanced) as advance,
                ):
                    report = orchestration.advance_sources_ready(context, request)
                self.assertEqual(report.status, "ADVANCED")
                advance.assert_called_once_with(
                    root, run_spec, expected_phase="ACQUIRING", next_phase="SOURCES_READY",
                    inventory_path=inventory, validation_receipts=markers,
                )
            finally:
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
                        patch.object(orchestration.execution_registry, "advance_execution_phase") as advance,
                        self.assertRaises(orchestration.FinalizationError) as raised,
                    ):
                        orchestration.advance_sources_ready(context, candidate)
                    self.assertEqual(raised.exception.exit_code, exit_code)
                    validate.assert_not_called()
                    advance.assert_not_called()

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
        candidate = dataclasses.replace(self.inventory, run_id="other-valid-run")
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
        different = dataclasses.replace(self.inventory, run_id="different-run")
        with patched_store_replay(), patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), patch.object(
            orchestration, "load_snapshot_store_from_fd", return_value=SimpleNamespace(inventory=different)
        ), self.assertRaises(orchestration.FinalizationError) as raised:
            orchestration.publish_inventory(self.context, lock_owner="publish-service")
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "POSTCONDITION_FAILED", 5
        ))

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

    def test_review_document_tamper_during_final_rebind_fails_closed(self) -> None:
        preflight = orchestration.PreflightReport(
            "ACQUIRING", "PASS", "x" * 64, len(self.inventory.snapshots), 0, ()
        )
        calls = 0

        def mutate_on_final_rebind(_context: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.query_ids_path.write_bytes(
                    canonical_json_bytes({"query_ids": list(reversed(self.query_ids))})
                    + b"\n"
                )
            return preflight

        with self.assertRaises(orchestration.FinalizationError) as raised:
            self._write(preflight_side_effect=mutate_on_final_rebind)
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "POSTCONDITION_FAILED", 5
        ))
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
