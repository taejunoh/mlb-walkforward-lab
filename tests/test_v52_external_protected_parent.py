from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch


_LOCKS = {
    ".baseball_savant_statcast.lock",
    ".mlb_schedule.lock",
    ".mlb_stats_pitcher_bf.lock",
    ".mlb_stats_pitching.lock",
}
_LOGICAL_ROOT = PurePosixPath("data/v5_1_bt/raw")
ROOT = Path(__file__).resolve().parents[1]
_R3_RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
_R3_CONTRACT = ROOT / "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
_R3_INVENTORY = ROOT / "audit/v5_1_bt/protected_raw_inventory.json"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"


@contextmanager
def _no_mutations() -> object:
    """Reject filesystem mutation calls while exercising the real capability."""

    def reject(operation: str, *_args: object, **_kwargs: object) -> object:
        raise AssertionError(f"unexpected filesystem mutation: {operation}")

    original_open = os.open

    def guarded_open(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        mutation_flags = (
            getattr(os, "O_WRONLY", 0)
            | getattr(os, "O_RDWR", 0)
            | getattr(os, "O_CREAT", 0)
            | getattr(os, "O_TRUNC", 0)
            | getattr(os, "O_APPEND", 0)
        )
        if flags & mutation_flags:
            raise AssertionError(f"unexpected mutating os.open flags: {flags:#x}")
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def reject_write_mode(mode: object) -> None:
        if isinstance(mode, str) and any(flag in mode for flag in "wax+"):
            raise AssertionError(f"unexpected mutating open mode: {mode!r}")

    original_builtin_open = builtins.open
    original_io_open = io.open

    def guarded_builtin_open(
        file: object, mode: object = "r", *args: object, **kwargs: object
    ) -> object:
        reject_write_mode(mode)
        return original_builtin_open(file, mode, *args, **kwargs)

    def guarded_io_open(
        file: object, mode: object = "r", *args: object, **kwargs: object
    ) -> object:
        reject_write_mode(mode)
        return original_io_open(file, mode, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(os, "open", guarded_open))
        stack.enter_context(patch.object(builtins, "open", guarded_builtin_open))
        stack.enter_context(patch.object(io, "open", guarded_io_open))
        for name in (
            "link",
            "symlink",
            "rename",
            "replace",
            "unlink",
            "remove",
            "mkdir",
            "makedirs",
        ):
            stack.enter_context(patch.object(os, name, lambda *args, _name=name, **kwargs: reject(_name, *args, **kwargs)))
        for name in ("copy", "copy2", "copyfile", "copytree", "move"):
            stack.enter_context(patch.object(shutil, name, lambda *args, _name=name, **kwargs: reject(_name, *args, **kwargs)))
        for name in (
            "write_bytes",
            "write_text",
            "touch",
            "mkdir",
            "unlink",
            "rename",
            "replace",
        ):
            stack.enter_context(patch.object(Path, name, lambda *args, _name=name, **kwargs: reject(f"Path.{_name}", *args, **kwargs)))
        yield


def _inventory(raw_root: Path) -> tuple[bytes, PurePosixPath, bytes]:
    source = "mlb_schedule"
    payload = b'{"schedule":true}\n'
    directory = raw_root / source
    directory.mkdir(parents=True)
    raw_name = f"{hashlib.sha256(payload).hexdigest()}.raw"
    raw_path = directory / raw_name
    raw_path.write_bytes(payload)
    query = {
        "end": "2024-09-30",
        "gameType": "R",
        "role": "schedule",
        "start": "2024-03-20",
        "url": "https://statsapi.mlb.com/api/v1/schedule",
    }
    manifest = _canonical(
        {
            "query": query,
            "retrieved_at_utc": "2024-10-01T00:00:00+00:00",
            "row_count": 1,
            "schema_version": 1,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "source": source,
        }
    )
    query_digest = hashlib.sha256(
        json.dumps(query, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    manifest_path = directory / f"{hashlib.sha256(payload).hexdigest()}.{query_digest}.manifest.json"
    manifest_path.write_bytes(manifest)
    seal_path = directory / f"{hashlib.sha256(payload).hexdigest()}.{query_digest}.manifest.sha256"
    seal_path.write_bytes(f"{hashlib.sha256(manifest).hexdigest()}\n".encode("ascii"))
    for name in _LOCKS:
        (raw_root / name).write_bytes(b"")

    def item(path: Path) -> dict[str, object]:
        metadata = path.stat()
        return {
            "byte_size": metadata.st_size,
            "inode_fingerprint": {"device": metadata.st_dev, "inode": metadata.st_ino},
            "path": (_LOGICAL_ROOT / source / path.name).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    return (
        _canonical(
            {
                "legacy_root": _LOGICAL_ROOT.as_posix(),
                "manifest_count": 1,
                "rawstore_verification": "PASS",
                "records": [{"canonical_query": query, "manifest": item(manifest_path), "raw": item(raw_path), "rawstore_verified": True, "seal": item(seal_path), "source": source}],
                "schema_version": 1,
            }
        ),
        PurePosixPath(_LOGICAL_ROOT / source / raw_path.name),
        payload,
    )


class ExternalProtectedParentTests(unittest.TestCase):
    def _bound(self, directory: str) -> tuple[object, Path, Path, bytes, PurePosixPath]:
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        target = Path(directory) / "target"
        raw_root = Path(directory) / "parent" / "raw"
        target.mkdir(parents=True)
        content, relative, payload = _inventory(raw_root)
        capability = _bind_external_inventory(
            target, raw_root, parse_protected_raw_inventory_document(content), hashlib.sha256(content).hexdigest()
        )
        self.addCleanup(capability.close)
        return capability, target, raw_root, content, relative

    def test_private_split_root_binder_reads_authenticated_descriptors(self) -> None:
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            raw_root = Path(directory) / "parent" / "raw"
            target.mkdir(parents=True)
            content, relative, payload = _inventory(raw_root)
            capability = _bind_external_inventory(
                target_root=target,
                physical_raw_root=raw_root,
                inventory=parse_protected_raw_inventory_document(content),
                inventory_sha256=hashlib.sha256(content).hexdigest(),
            )
            self.addCleanup(capability.close)
            self.assertEqual(capability.read_bytes(relative), payload)
            capability.validate()

    def test_private_binder_rejects_copy_hardlink_symlink_fifo_and_missing(self) -> None:
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        for mutation in ("copy", "hardlink", "symlink", "fifo", "missing"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "target"
                raw_root = Path(directory) / "parent" / "raw"
                target.mkdir(parents=True)
                content, relative, _ = _inventory(raw_root)
                leaf = raw_root / Path(*relative.relative_to(_LOGICAL_ROOT).parts)
                if mutation == "copy":
                    replacement = leaf.with_name("replacement.raw")
                    shutil.copyfile(leaf, replacement)
                    replacement.replace(leaf)
                elif mutation == "hardlink":
                    os.link(leaf, leaf.with_name("linked.raw"))
                elif mutation == "symlink":
                    leaf.unlink()
                    leaf.symlink_to("elsewhere.raw")
                elif mutation == "fifo":
                    leaf.unlink()
                    os.mkfifo(leaf)
                else:
                    leaf.unlink()
                with self.assertRaises((RuntimeError, ValueError)):
                    _bind_external_inventory(target, raw_root, parse_protected_raw_inventory_document(content), hashlib.sha256(content).hexdigest())

    def test_private_binder_rejects_record_ancestor_and_lock_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability, _, raw_root, _, relative = self._bound(directory)
            leaf = raw_root / Path(*relative.relative_to(_LOGICAL_ROOT).parts)
            replacement = leaf.with_name("replacement.raw")
            replacement.write_bytes(leaf.read_bytes())
            replacement.replace(leaf)
            with self.assertRaises(RuntimeError):
                capability.rebind()

        with tempfile.TemporaryDirectory() as directory:
            capability, _, raw_root, _, _ = self._bound(directory)
            lock = raw_root / ".mlb_schedule.lock"
            lock.write_bytes(b"changed")
            with self.assertRaises(RuntimeError):
                capability.validate()

        with tempfile.TemporaryDirectory() as directory:
            capability, _, raw_root, _, _ = self._bound(directory)
            lock = raw_root / ".mlb_schedule.lock"
            replacement = lock.with_name("replacement.lock")
            replacement.write_bytes(b"")
            replacement.replace(lock)
            with self.assertRaises(RuntimeError):
                capability.validate()

        with tempfile.TemporaryDirectory() as directory:
            capability, _, raw_root, _, _ = self._bound(directory)
            moved = raw_root.with_name("moved")
            raw_root.replace(moved)
            raw_root.mkdir()
            with self.assertRaises(RuntimeError):
                capability.rebind()

    def test_factory_rejects_noncanonical_inventory_path_and_overlap(self) -> None:
        from mlb_backtest.v52_external_protected_parent import (
            ExternalProtectedParentInputs,
            _bind_external_inventory,
            build_external_protected_parent_capability,
        )
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            with self.assertRaises(ValueError):
                build_external_protected_parent_capability(
                    ExternalProtectedParentInputs(
                        target, target / "raw", target / "not-inventory.json", target / "run.json", target / "contract.json"
                    )
                )
            content, _, _ = _inventory(target / "raw")
            with self.assertRaises(ValueError):
                _bind_external_inventory(target, target / "raw", parse_protected_raw_inventory_document(content), hashlib.sha256(content).hexdigest())

    def test_public_factory_rejects_exact_r3_target_parent_overlap_and_noncanonical_inventory(self) -> None:
        from mlb_backtest.v52_external_protected_parent import (
            ExternalProtectedParentInputs,
            build_external_protected_parent_capability,
        )

        exact = dict(
            target_root=ROOT,
            target_protected_inventory_path=_R3_INVENTORY,
            run_spec_path=_R3_RUN_SPEC,
            acquisition_contract_path=_R3_CONTRACT,
        )
        for physical in (ROOT.parent, ROOT / "audit"):
            with self.subTest(physical_raw_root=physical), self.assertRaises(ValueError):
                build_external_protected_parent_capability(
                    ExternalProtectedParentInputs(
                        physical_raw_root=physical,
                        **exact,
                    )
                )
        with self.assertRaises(ValueError):
            build_external_protected_parent_capability(
                ExternalProtectedParentInputs(
                    physical_raw_root=ROOT.parent,
                    target_protected_inventory_path=(
                        ROOT / "audit/v5_1_bt/../v5_1_bt/protected_raw_inventory.json"
                    ),
                    run_spec_path=_R3_RUN_SPEC,
                    acquisition_contract_path=_R3_CONTRACT,
                    target_root=ROOT,
                )
            )

    def test_private_binder_rejects_link_special_missing_and_extra_members(self) -> None:
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            raw_root = Path(directory) / "parent" / "raw"
            target.mkdir(parents=True)
            content, _, _ = _inventory(raw_root)
            inventory = parse_protected_raw_inventory_document(content)
            (raw_root / "mlb_schedule" / "extra.raw").write_bytes(b"extra")
            with self.assertRaises((RuntimeError, ValueError)):
                _bind_external_inventory(target, raw_root, inventory, hashlib.sha256(content).hexdigest())

    def test_closed_capability_fails_closed_and_does_not_expose_parent_path(self) -> None:
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            raw_root = Path(directory) / "parent" / "raw"
            target.mkdir(parents=True)
            content, relative, _ = _inventory(raw_root)
            capability = _bind_external_inventory(target, raw_root, parse_protected_raw_inventory_document(content), hashlib.sha256(content).hexdigest())
            self.assertNotIn(os.fspath(raw_root), repr(capability))
            capability.close()
            for operation in (
                capability.validate,
                capability.rebind,
                lambda: capability.read_bytes(relative),
                lambda: capability.open_bound_object(relative),
            ):
                with self.assertRaises(RuntimeError):
                    operation()

    def test_context_manager_open_bound_object_and_target_inventory_swap_fail_closed(self) -> None:
        from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
        from mlb_backtest.v52_legacy import _snapshot
        from mlb_backtest.v52_protected_inventory import parse_protected_raw_inventory_document

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            raw_root = Path(directory) / "parent" / "raw"
            inventory_path = target / "audit/v5_1_bt/protected_raw_inventory.json"
            inventory_path.parent.mkdir(parents=True)
            target = target.resolve()
            inventory_path = target / "audit/v5_1_bt/protected_raw_inventory.json"
            content, relative, payload = _inventory(raw_root)
            inventory_path.write_bytes(content)
            capability = _bind_external_inventory(
                target,
                raw_root,
                parse_protected_raw_inventory_document(content),
                hashlib.sha256(content).hexdigest(),
                inventory_path=inventory_path,
                inventory_snapshot=_snapshot(target, inventory_path),
            )
            with capability as bound:
                with bound.open_bound_object(relative) as handle:
                    self.assertEqual(handle.read(), payload)
            with self.assertRaises(RuntimeError):
                capability.open_bound_object(relative)

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            raw_root = Path(directory) / "parent" / "raw"
            inventory_path = target / "audit/v5_1_bt/protected_raw_inventory.json"
            inventory_path.parent.mkdir(parents=True)
            target = target.resolve()
            inventory_path = target / "audit/v5_1_bt/protected_raw_inventory.json"
            content, _, _ = _inventory(raw_root)
            inventory_path.write_bytes(content)
            capability = _bind_external_inventory(
                target,
                raw_root,
                parse_protected_raw_inventory_document(content),
                hashlib.sha256(content).hexdigest(),
                inventory_path=inventory_path,
                inventory_snapshot=_snapshot(target, inventory_path),
            )
            self.addCleanup(capability.close)
            replacement = inventory_path.with_name("replacement.json")
            replacement.write_bytes(content)
            replacement.replace(inventory_path)
            with self.assertRaises(RuntimeError):
                capability.rebind()

    def test_issued_capability_admission_rejects_forged_duck_subclass_and_reader_replacement(self) -> None:
        from mlb_backtest.v52_external_protected_parent import ExternalProtectedParentCapability
        from mlb_backtest.v52_legacy import _require_issued_legacy_evidence

        with tempfile.TemporaryDirectory() as directory:
            capability, _, _, _, _ = self._bound(directory)
            with self.assertRaises(TypeError):
                _require_issued_legacy_evidence(SimpleNamespace())
            forged = ExternalProtectedParentCapability(
                inventory_sha256=capability.inventory_sha256,
                records=capability.records,
                tree=capability._tree,
                lock_identities=capability._lock_identities,
            )
            self.addCleanup(forged.close)
            with self.assertRaises(TypeError):
                _require_issued_legacy_evidence(forged)

            with self.assertRaises(TypeError):
                class ForgedSubclass(ExternalProtectedParentCapability):
                    pass

            with self.assertRaises(AttributeError):
                capability.read_bytes = lambda _path: b"forged"
            self.assertIs(_require_issued_legacy_evidence(capability), capability)

    def test_lock_fifo_swap_uses_nonblocking_open_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capability, _, raw_root, _, _ = self._bound(directory)
            lock = raw_root / ".mlb_schedule.lock"
            observed_flags: list[int] = []
            original_open = os.open

            def swap_to_fifo(name: str, flags: int, *args: object, **kwargs: object) -> int:
                if name == lock.name and kwargs.get("dir_fd") == capability._tree.legacy_fd:
                    observed_flags.append(flags)
                    lock.unlink()
                    os.mkfifo(lock)
                    raise OSError("FIFO swap intercepted without opening it")
                return original_open(name, flags, *args, **kwargs)

            with patch("mlb_backtest.v52_external_protected_parent.os.open", swap_to_fifo):
                with self.assertRaises(RuntimeError):
                    capability.validate()
            self.assertEqual(len(observed_flags), 1)
            self.assertNotEqual(observed_flags[0] & getattr(os, "O_NONBLOCK", 0), 0)

    @unittest.skipUnless(
        os.environ.get("MLB_V52_RUN_SLOW_INTEGRATION") == "1",
        "set MLB_V52_RUN_SLOW_INTEGRATION=1 to authenticate the real protected parent",
    )
    def test_real_protected_parent_authenticates_4711_objects_without_writes(self) -> None:
        from mlb_backtest.v52_external_protected_parent import (
            ExternalProtectedParentInputs,
            build_external_protected_parent_capability,
        )
        from mlb_backtest.v52_legacy import _snapshot
        from mlb_backtest.v52_protected_inventory import (
            parse_protected_raw_inventory_document,
            read_regular_no_follow,
            snapshot_protected_inventory_membership,
        )

        authority_root = Path(os.environ["MLB_V52_SOURCE_AUTHORITY_ROOT"]).resolve(
            strict=True
        )
        physical_raw_root = authority_root / Path(_LOGICAL_ROOT)
        inventory = parse_protected_raw_inventory_document(
            read_regular_no_follow(ROOT, _R3_INVENTORY, "target protected inventory")
        )

        def lock_snapshot() -> tuple[tuple[str, int, int, int, int, int, int, int], ...]:
            return tuple(
                (
                    name,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_nlink,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
                for name in sorted(_LOCKS)
                for metadata in [
                    os.stat(physical_raw_root / name, follow_symlinks=False)
                ]
            )

        target_paths = (_R3_INVENTORY, _R3_RUN_SPEC, _R3_CONTRACT)
        target_logical_raw_root = ROOT / Path(_LOGICAL_ROOT)
        self.assertFalse(os.path.lexists(target_logical_raw_root))
        target_before = tuple(_snapshot(ROOT, path) for path in target_paths)
        parent_before = snapshot_protected_inventory_membership(authority_root, inventory)
        self.assertEqual(len(parent_before), 4711)
        locks_before = lock_snapshot()
        self.assertEqual({item[0] for item in locks_before}, _LOCKS)
        for _name, _device, _inode, mode, nlink, size, _mtime, _ctime in locks_before:
            self.assertTrue(stat.S_ISREG(mode))
            self.assertEqual(nlink, 1)
            self.assertEqual(size, 0)

        inputs = ExternalProtectedParentInputs(
            target_root=ROOT,
            physical_raw_root=physical_raw_root,
            target_protected_inventory_path=_R3_INVENTORY,
            run_spec_path=_R3_RUN_SPEC,
            acquisition_contract_path=_R3_CONTRACT,
        )
        with _no_mutations():
            with build_external_protected_parent_capability(inputs) as capability:
                self.assertEqual(len(capability.records), inventory["manifest_count"])
                self.assertEqual(len(capability.records), 1675)
                bound_paths = {
                    path
                    for record in capability.records
                    for path in (
                        record.manifest_path,
                        record.seal_path,
                        record.payload_path,
                    )
                }
                self.assertEqual(len(bound_paths), 4711)
                expected_families = {
                    "baseball_savant_statcast",
                    "mlb_schedule",
                    "mlb_stats_pitcher_bf",
                    "mlb_stats_pitching",
                }
                observed_families = {record.source for record in capability.records}
                self.assertEqual(observed_families, expected_families)
                for family in sorted(observed_families):
                    record = next(
                        record
                        for record in capability.records
                        if record.source == family
                    )
                    with capability.open_bound_object(record.payload_path) as bound_object:
                        self.assertIsInstance(bound_object.read(1), bytes)
                capability.rebind()

        self.assertEqual(tuple(_snapshot(ROOT, path) for path in target_paths), target_before)
        self.assertFalse(os.path.lexists(target_logical_raw_root))
        self.assertEqual(
            snapshot_protected_inventory_membership(authority_root, inventory),
            parent_before,
        )
        self.assertEqual(lock_snapshot(), locks_before)
