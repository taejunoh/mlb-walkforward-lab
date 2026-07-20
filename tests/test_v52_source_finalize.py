from __future__ import annotations

import dataclasses
import hashlib
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from mlb_backtest import v52_source_orchestration as orchestration
from mlb_backtest.execution_registry import AcquisitionCheckpoint, ExecutionRegistryState
from mlb_backtest.v52_snapshot_store import (
    AuthorityBinding,
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

    def test_status_reports_unsafe_published_store_as_a_blocker(self) -> None:
        source_root = self.root / "data/v5_2_qd/historical/sources"
        source_root.mkdir(parents=True)
        before = tree_snapshot(self.root)
        report = orchestration.read_status(self.context)
        self.assertFalse(report.publish_ready)
        self.assertFalse(report.review_ready)
        self.assertIn(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            {blocker.code for blocker in report.blockers},
        )
        blocker = next(
            item
            for item in report.blockers
            if item.code == "PUBLISHED_INVENTORY_UNAUTHENTICATED"
        )
        self.assertEqual(
            blocker.message,
            "published source inventory is unsafe or unavailable",
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

    def test_failed_publish_cleans_only_empty_fixed_directories(self) -> None:
        with patch.object(
            orchestration, "run_preflight", return_value=self._passing_preflight()
        ), self.assertRaises(orchestration.FinalizationError) as raised:
            orchestration.publish_inventory(self.context, lock_owner="unsafe owner")
        self.assertEqual((raised.exception.error_code, raised.exception.exit_code), (
            "PUBLISH_FAILED", 5
        ))
        self.assertFalse((self.root / "data").exists())

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


if __name__ == "__main__":
    unittest.main()
