from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

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
