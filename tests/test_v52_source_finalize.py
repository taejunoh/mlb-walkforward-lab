from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mlb_backtest import v52_source_orchestration as orchestration
from mlb_backtest.execution_registry import AcquisitionCheckpoint, ExecutionRegistryState
from mlb_backtest.v52_snapshot_store import AuthorityBinding
from mlb_backtest.v52_source_contracts import canonical_json_bytes


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


class ContextTests(unittest.TestCase):
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
            patch.object(orchestration, "verify_protected_raw_inventory", return_value={}),
            patch.object(orchestration, "snapshot_protected_inventory_membership"),
            patch.object(orchestration, "load_execution_registry", return_value=registry),
            patch.object(orchestration, "load_source_verification_checkpoint", return_value=checkpoint),
            patch.object(orchestration, "load_snapshot_replay_inputs", return_value=replay),
            patch.object(orchestration, "_binding", side_effect=bind),
            patch.object(orchestration, "_verified_binding", side_effect=verified),
            patch.object(orchestration, "build_inventory", return_value=inventory) as build,
            patch.object(orchestration, "_git_head", return_value="3" * 40),
            patch.object(orchestration, "_rebind_context_authorities") as rebind,
        ):
            context = orchestration.build_context(orchestration.CommonArgs(root=ROOT))

        self.assertEqual(context.registry, registry)
        self.assertIs(context.inventory, inventory)
        self.assertEqual(context.phase, "ACQUIRING")
        self.assertEqual(len(order), 5)
        self.assertEqual(order[-3:], [
            "verified:audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json",
            "verified:audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json",
            "verified:" + str(ROOT / "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json"),
        ])
        build.assert_called_once()
        rebind.assert_called_once()


if __name__ == "__main__":
    unittest.main()
