"""Descriptor-bound access to the protected v5.1 raw tree outside r3."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import weakref
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Mapping

from .v52_legacy import (
    ProtectedRawRecord,
    _Snapshot,
    _register_issued_legacy_evidence,
    _snapshot,
    trusted_legacy_repository_root,
)
from .v52_protected_inventory import (
    _BoundLegacyTree,
    parse_protected_raw_inventory_document,
    read_regular_no_follow,
)
from .v52_query_plan import _R3_RECOVERY_RUN_ID, _load_static_contracts


_LOGICAL_RAW_ROOT = PurePosixPath("data/v5_1_bt/raw")
_INVENTORY_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
_ROOT_LOCKS = frozenset(
    {
        ".baseball_savant_statcast.lock",
        ".mlb_schedule.lock",
        ".mlb_stats_pitcher_bf.lock",
        ".mlb_stats_pitching.lock",
    }
)
_STABLE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


@dataclass(frozen=True)
class ExternalProtectedParentInputs:
    target_root: Path
    physical_raw_root: Path
    target_protected_inventory_path: Path
    run_spec_path: Path
    acquisition_contract_path: Path


@dataclass
class _CapabilityState:
    closed: bool = False


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(metadata, field) for field in _STABLE_FIELDS)


def _read_descriptor(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _close_tree(tree: _BoundLegacyTree) -> None:
    tree.close()


class ExternalProtectedParentCapability:
    """An issued split-root implementation of ``LegacyEvidenceCapability``."""

    __slots__ = (
        "logical_raw_root",
        "inventory_sha256",
        "records",
        "_tree",
        "_lock_identities",
        "_target_root",
        "_inventory_path",
        "_inventory_snapshot",
        "_state",
        "_finalizer",
        "__weakref__",
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("external protected parent capability cannot be subclassed")

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("external protected parent capability is immutable")

    def __init__(
        self,
        *,
        inventory_sha256: str,
        records: tuple[ProtectedRawRecord, ...],
        tree: _BoundLegacyTree,
        lock_identities: tuple[tuple[str, tuple[int, ...]], ...],
        target_root: Path | None = None,
        inventory_path: Path | None = None,
        inventory_snapshot: _Snapshot | None = None,
    ) -> None:
        object.__setattr__(self, "logical_raw_root", _LOGICAL_RAW_ROOT)
        object.__setattr__(self, "inventory_sha256", inventory_sha256)
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "_tree", tree)
        object.__setattr__(self, "_lock_identities", lock_identities)
        object.__setattr__(self, "_target_root", target_root)
        object.__setattr__(self, "_inventory_path", inventory_path)
        object.__setattr__(self, "_inventory_snapshot", inventory_snapshot)
        object.__setattr__(self, "_state", _CapabilityState())
        object.__setattr__(self, "_finalizer", weakref.finalize(self, _close_tree, tree))

    def __repr__(self) -> str:
        return (
            "ExternalProtectedParentCapability("
            f"inventory_sha256={self.inventory_sha256!r}, records={len(self.records)})"
        )

    def __getstate__(self) -> object:
        raise TypeError("external protected parent capabilities are not serializable")

    def __enter__(self) -> "ExternalProtectedParentCapability":
        self._ensure_open()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._state.closed:
            raise RuntimeError("external protected parent capability is closed")

    def _record_for(self, relative_path: PurePosixPath) -> tuple[ProtectedRawRecord, str]:
        if type(relative_path) is not PurePosixPath:
            raise TypeError("protected raw path must be a PurePosixPath")
        for record in self.records:
            for path, kind in (
                (record.manifest_path, "manifest"),
                (record.seal_path, "seal"),
                (record.payload_path, "payload"),
            ):
                if path == relative_path:
                    return record, kind
        raise ValueError("protected raw path is not inventoried")

    @staticmethod
    def _binding(record: ProtectedRawRecord, kind: str) -> tuple[PurePosixPath, str, int, int, int]:
        if kind == "manifest":
            return (
                record.manifest_path,
                record.manifest_sha256,
                record.manifest_byte_size,
                record.manifest_device,
                record.manifest_inode,
            )
        if kind == "seal":
            return (
                record.seal_path,
                record.seal_sha256,
                record.seal_byte_size,
                record.seal_device,
                record.seal_inode,
            )
        return (
            record.payload_path,
            record.payload_sha256,
            record.payload_byte_size,
            record.payload_device,
            record.payload_inode,
        )

    def _read_record(self, record: ProtectedRawRecord, kind: str) -> bytes:
        path, expected_sha, expected_size, expected_device, expected_inode = self._binding(
            record, kind
        )
        try:
            physical = path.relative_to(self.logical_raw_root)
        except ValueError as exc:
            raise RuntimeError("protected raw record escaped logical root") from exc
        if len(physical.parts) != 2 or physical.parts[0] != record.source:
            raise RuntimeError("protected raw record path is invalid")
        physical_path = self._tree.legacy_root / Path(physical.as_posix())
        descriptor, opened = self._tree.open_regular(
            physical_path, "external protected raw object"
        )
        try:
            content = _read_descriptor(descriptor)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        linked = self._tree.assert_regular_bound(
            physical_path, opened, "external protected raw object"
        )
        if (
            opened.st_dev != expected_device
            or opened.st_ino != expected_inode
            or opened.st_size != expected_size
            or hashlib.sha256(content).hexdigest() != expected_sha
            or any(getattr(opened, field) != getattr(after, field) for field in _STABLE_FIELDS)
            or any(getattr(after, field) != getattr(linked, field) for field in _STABLE_FIELDS)
        ):
            raise RuntimeError("external protected raw object binding changed")
        return content

    def _validate_locks(self) -> None:
        observed: list[tuple[str, tuple[int, ...]]] = []
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        for name in sorted(_ROOT_LOCKS):
            try:
                linked = os.stat(name, dir_fd=self._tree.legacy_fd, follow_symlinks=False)
                if not stat.S_ISREG(linked.st_mode) or linked.st_nlink != 1:
                    raise RuntimeError("external root lock is invalid")
                descriptor = os.open(name, flags, dir_fd=self._tree.legacy_fd)
            except OSError as exc:
                raise RuntimeError("external root lock is invalid") from exc
            try:
                opened = os.fstat(descriptor)
                content = _read_descriptor(descriptor)
            finally:
                os.close(descriptor)
            try:
                rebound = os.stat(name, dir_fd=self._tree.legacy_fd, follow_symlinks=False)
            except OSError as exc:
                raise RuntimeError("external root lock changed") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or content
                or _identity(linked) != _identity(opened)
                or _identity(opened) != _identity(rebound)
            ):
                raise RuntimeError("external root lock changed")
            observed.append((name, _identity(opened)))
        if tuple(observed) != self._lock_identities:
            raise RuntimeError("external root lock binding changed")

    def _validate_target_inventory(self) -> None:
        if self._inventory_snapshot is None:
            return
        if self._target_root is None or self._inventory_path is None:
            raise RuntimeError("external protected inventory binding is invalid")
        try:
            current = _snapshot(self._target_root, self._inventory_path)
        except Exception as exc:
            raise RuntimeError("external protected inventory binding changed") from exc
        if (
            current != self._inventory_snapshot
            or hashlib.sha256(current.contents).hexdigest() != self.inventory_sha256
        ):
            raise RuntimeError("external protected inventory binding changed")

    def validate(self) -> None:
        self._ensure_open()
        self._validate_target_inventory()
        self._tree.assert_bound()
        self._validate_locks()
        expected: dict[str, set[str]] = {}
        for record in self.records:
            for path in (record.manifest_path, record.seal_path, record.payload_path):
                physical = path.relative_to(self.logical_raw_root)
                expected.setdefault(record.source, set()).add(physical.parts[1])
        membership = self._tree.source_membership()
        if set(membership) != set(expected) or any(
            set(membership[source]) != expected[source] for source in expected
        ):
            raise RuntimeError("external protected raw membership changed")
        for record in self.records:
            self._read_record(record, "manifest")
            self._read_record(record, "seal")
            self._read_record(record, "payload")
        self._tree.assert_bound()
        self._validate_locks()
        self._validate_target_inventory()

    def rebind(self) -> None:
        self.validate()

    def read_bytes(self, relative_path: PurePosixPath) -> bytes:
        self._ensure_open()
        record, kind = self._record_for(relative_path)
        self.validate()
        content = self._read_record(record, kind)
        self.validate()
        return content

    def open_bound_object(self, relative_path: PurePosixPath) -> BinaryIO:
        return io.BytesIO(self.read_bytes(relative_path))

    def close(self) -> None:
        if not self._state.closed:
            self._state.closed = True
            self._finalizer()


def _record(item: Mapping[str, object]) -> ProtectedRawRecord:
    def object_fields(kind: str) -> tuple[PurePosixPath, str, int, int, int]:
        value = item[kind]
        if not isinstance(value, Mapping):
            raise ValueError("protected inventory record object is invalid")
        fingerprint = value["inode_fingerprint"]
        if not isinstance(fingerprint, Mapping):
            raise ValueError("protected inventory record fingerprint is invalid")
        return (
            PurePosixPath(str(value["path"])),
            str(value["sha256"]),
            int(value["byte_size"]),
            int(fingerprint["device"]),
            int(fingerprint["inode"]),
        )

    manifest = object_fields("manifest")
    seal = object_fields("seal")
    payload = object_fields("raw")
    canonical_query = item["canonical_query"]
    if not isinstance(canonical_query, Mapping):
        raise ValueError("protected inventory canonical query is invalid")
    import json

    return ProtectedRawRecord(
        source=str(item["source"]),
        canonical_query_json=json.dumps(canonical_query, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        manifest_path=manifest[0], manifest_sha256=manifest[1], manifest_byte_size=manifest[2], manifest_device=manifest[3], manifest_inode=manifest[4],
        seal_path=seal[0], seal_sha256=seal[1], seal_byte_size=seal[2], seal_device=seal[3], seal_inode=seal[4],
        payload_path=payload[0], payload_sha256=payload[1], payload_byte_size=payload[2], payload_device=payload[3], payload_inode=payload[4],
    )


def _bind_external_inventory(
    target_root: Path,
    physical_raw_root: Path,
    inventory: Mapping[str, object],
    inventory_sha256: str,
    *,
    inventory_path: Path | None = None,
    inventory_snapshot: _Snapshot | None = None,
) -> ExternalProtectedParentCapability:
    """Private synthetic-test binder; public authority comes from the factory."""
    target = trusted_legacy_repository_root(target_root)
    physical = trusted_legacy_repository_root(physical_raw_root)
    if target == physical or target in physical.parents or physical in target.parents:
        raise ValueError("external protected parent must be disjoint from target")
    if str(inventory.get("legacy_root")) != _LOGICAL_RAW_ROOT.as_posix():
        raise ValueError("external protected inventory logical root differs")
    raw_records = inventory.get("records")
    if not isinstance(raw_records, list):
        raise ValueError("external protected inventory records are invalid")
    records = tuple(sorted((_record(item) for item in raw_records if isinstance(item, Mapping)), key=lambda record: record.manifest_path.as_posix()))
    if len(records) != len(raw_records):
        raise ValueError("external protected inventory records are invalid")
    tree = _BoundLegacyTree(physical, physical, expected_root_locks=set(_ROOT_LOCKS))
    try:
        lock_identities = tuple(
            (name, _identity(os.stat(name, dir_fd=tree.legacy_fd, follow_symlinks=False)))
            for name in sorted(_ROOT_LOCKS)
        )
        capability = ExternalProtectedParentCapability(
            inventory_sha256=inventory_sha256,
            records=records,
            tree=tree,
            lock_identities=lock_identities,
            target_root=target if inventory_snapshot is not None else None,
            inventory_path=inventory_path,
            inventory_snapshot=inventory_snapshot,
        )
        _register_issued_legacy_evidence(capability)
        capability.validate()
        return capability
    except Exception:
        tree.close()
        raise


def build_external_protected_parent_capability(
    inputs: ExternalProtectedParentInputs,
) -> ExternalProtectedParentCapability:
    target = trusted_legacy_repository_root(inputs.target_root)
    expected_inventory = target / _INVENTORY_RELATIVE
    supplied_inventory = (
        Path(inputs.target_protected_inventory_path)
        if Path(inputs.target_protected_inventory_path).is_absolute()
        else target / Path(inputs.target_protected_inventory_path)
    )
    if supplied_inventory != expected_inventory:
        raise ValueError("protected inventory path must be the canonical target path")
    context = _load_static_contracts(inputs.run_spec_path, inputs.acquisition_contract_path)
    if context.repository_root != target or context.run_id != _R3_RECOVERY_RUN_ID:
        raise ValueError("external protected parent is available only for r3")
    contents = read_regular_no_follow(target, expected_inventory, "protected inventory")
    legacy = context.contract.get("legacy_protection")
    if not isinstance(legacy, Mapping):
        raise ValueError("r3 protected inventory authority is invalid")
    expected_sha = legacy.get("protected_inventory_sha256")
    if not isinstance(expected_sha, str) or hashlib.sha256(contents).hexdigest() != expected_sha:
        raise ValueError("r3 protected inventory hash differs")
    inventory = parse_protected_raw_inventory_document(contents)
    inventory_snapshot = _snapshot(target, expected_inventory)
    if inventory_snapshot.contents != contents:
        raise RuntimeError("r3 protected inventory binding changed")
    capability = _bind_external_inventory(
        target,
        inputs.physical_raw_root,
        inventory,
        expected_sha,
        inventory_path=expected_inventory,
        inventory_snapshot=inventory_snapshot,
    )
    return capability


__all__ = [
    "ExternalProtectedParentCapability",
    "ExternalProtectedParentInputs",
    "build_external_protected_parent_capability",
]
