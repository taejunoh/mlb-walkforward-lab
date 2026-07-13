from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .manifest import RawStore


_INVENTORY_KEYS = {
    "legacy_root",
    "manifest_count",
    "rawstore_verification",
    "records",
    "schema_version",
}
_RECORD_KEYS = {
    "canonical_query",
    "manifest",
    "raw",
    "rawstore_verified",
    "seal",
    "source",
}
_OBJECT_KEYS = {"byte_size", "inode_fingerprint", "path", "sha256"}


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _repository_root(root: Path) -> Path:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("root must be a real directory")
    return root.resolve(strict=True)


def _under_root(root: Path, path: Path, label: str) -> tuple[Path, str]:
    candidate = path if path.is_absolute() else root / path
    try:
        linked_parent = os.stat(candidate.parent, follow_symlinks=False)
        resolved_parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} parent cannot be inspected safely") from exc
    if not stat.S_ISDIR(linked_parent.st_mode):
        raise ValueError(f"{label} parent must be a real directory")
    if resolved_parent != candidate.parent.absolute():
        raise ValueError(f"{label} parent must not contain a symlink")
    try:
        relative = candidate.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must be inside repository root") from exc
    if relative in {"", "."}:
        raise ValueError(f"{label} must not be repository root")
    return candidate, relative


def _validate_legacy_tree(legacy_root: Path, root: Path) -> None:
    linked_root = os.stat(legacy_root, follow_symlinks=False)
    if not stat.S_ISDIR(linked_root.st_mode) or legacy_root.is_symlink():
        raise ValueError("legacy root must be a real directory")
    if legacy_root.resolve(strict=True).parent == legacy_root.resolve(strict=True):
        raise ValueError("legacy root is invalid")
    try:
        legacy_root.resolve(strict=True).relative_to(root)
    except ValueError as exc:
        raise ValueError("legacy root must be inside repository root") from exc

    for child in legacy_root.iterdir():
        linked = os.stat(child, follow_symlinks=False)
        if child.is_symlink():
            raise ValueError("legacy tree symlink is forbidden")
        if stat.S_ISDIR(linked.st_mode):
            if child.resolve(strict=True).parent != legacy_root.resolve(strict=True):
                raise ValueError("legacy source directory escaped legacy root")
            for leaf in child.iterdir():
                _validate_regular_leaf(leaf, "legacy raw object")
        else:
            _validate_regular_leaf(child, "legacy root file")


def _validate_regular_leaf(path: Path, label: str) -> os.stat_result:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected safely") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise ValueError(f"{label} hard link is forbidden")
    return metadata


def _open_regular(path: Path, label: str) -> tuple[int, os.stat_result]:
    before = _validate_regular_leaf(path, label)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR}:
            raise ValueError(f"{label} must be a regular file") from exc
        raise ValueError(f"{label} cannot be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if opened.st_nlink != 1:
            raise ValueError(f"{label} hard link is forbidden")
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError(f"{label} changed while opening")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _stream_object(root: Path, path: Path, label: str) -> dict[str, object]:
    descriptor, opened = _open_regular(path, label)
    digest = hashlib.sha256()
    byte_size = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_size += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    linked = _validate_regular_leaf(path, label)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(opened, key) != getattr(after, key) for key in stable_fields):
        raise RuntimeError(f"{label} changed while reading")
    if any(getattr(after, key) != getattr(linked, key) for key in stable_fields):
        raise RuntimeError(f"{label} changed after reading")
    if byte_size != opened.st_size:
        raise RuntimeError(f"{label} byte count changed while reading")
    try:
        relative = path.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must be inside repository root") from exc
    return {
        "byte_size": byte_size,
        "inode_fingerprint": {"device": opened.st_dev, "inode": opened.st_ino},
        "path": relative,
        "sha256": digest.hexdigest(),
    }


def _read_regular(root: Path, path: Path, label: str) -> bytes:
    descriptor, opened = _open_regular(path, label)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    linked = _validate_regular_leaf(path, label)
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(opened, key) != getattr(after, key) for key in stable_fields):
        raise RuntimeError(f"{label} changed while reading")
    if any(getattr(after, key) != getattr(linked, key) for key in stable_fields):
        raise RuntimeError(f"{label} changed after reading")
    content = b"".join(chunks)
    if len(content) != opened.st_size:
        raise RuntimeError(f"{label} byte count changed while reading")
    try:
        path.absolute().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside repository root") from exc
    return content


def build_protected_raw_inventory(
    root: Path, legacy_root: Path
) -> Mapping[str, object]:
    """Recompute a path-bound inventory of every verified legacy RawStore record."""
    repository_root = _repository_root(root)
    legacy_path, legacy_relative = _under_root(
        repository_root, Path(legacy_root), "legacy root"
    )
    _validate_legacy_tree(legacy_path, repository_root)

    verified = RawStore(legacy_path).verify_manifests()
    object_cache: dict[Path, dict[str, object]] = {}
    records: list[dict[str, object]] = []
    for item in verified:
        manifest = item["manifest"]
        manifest_path = Path(item["manifest_path"])
        raw_path = Path(item["payload_path"])
        seal_path = manifest_path.with_name(manifest_path.name[:-5] + ".sha256")

        def object_record(path: Path, label: str) -> dict[str, object]:
            canonical = path.absolute()
            if canonical not in object_cache:
                object_cache[canonical] = _stream_object(
                    repository_root, path, label
                )
            return dict(object_cache[canonical])

        records.append(
            {
                "canonical_query": manifest["query"],
                "manifest": object_record(manifest_path, "legacy manifest"),
                "raw": object_record(raw_path, "legacy raw payload"),
                "rawstore_verified": True,
                "seal": object_record(seal_path, "legacy manifest seal"),
                "source": manifest["source"],
            }
        )
    records.sort(key=lambda record: record["manifest"]["path"])
    return {
        "legacy_root": legacy_relative,
        "manifest_count": len(records),
        "rawstore_verification": "PASS",
        "records": records,
        "schema_version": 1,
    }


def _validate_inventory_shape(inventory: object) -> dict[str, Any]:
    if not isinstance(inventory, dict) or set(inventory) != _INVENTORY_KEYS:
        raise ValueError("protected raw inventory keys are invalid")
    if inventory["schema_version"] != 1:
        raise ValueError("protected raw inventory schema version is invalid")
    if inventory["rawstore_verification"] != "PASS":
        raise ValueError("protected raw inventory RawStore result is invalid")
    if not isinstance(inventory["records"], list):
        raise ValueError("protected raw inventory records are invalid")
    if inventory["manifest_count"] != len(inventory["records"]):
        raise ValueError("protected raw inventory manifest count is invalid")
    for record in inventory["records"]:
        if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
            raise ValueError("protected raw inventory record keys are invalid")
        if record["rawstore_verified"] is not True:
            raise ValueError("protected raw inventory record was not verified")
        for kind in ("manifest", "raw", "seal"):
            item = record[kind]
            if not isinstance(item, dict) or set(item) != _OBJECT_KEYS:
                raise ValueError("protected raw inventory object keys are invalid")
    return inventory


def verify_protected_raw_inventory(
    root: Path, inventory_path: Path
) -> Mapping[str, object]:
    """Verify canonical inventory bytes and recompute every protected object."""
    repository_root = _repository_root(root)
    path, _ = _under_root(repository_root, Path(inventory_path), "inventory path")
    content = _read_regular(repository_root, path, "protected raw inventory")
    try:
        inventory = _validate_inventory_shape(json.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("protected raw inventory must be canonical JSON") from exc
    if content != _canonical_json_bytes(inventory):
        raise ValueError("protected raw inventory must use canonical JSON")
    rebuilt = build_protected_raw_inventory(
        repository_root, repository_root / inventory["legacy_root"]
    )
    if inventory != rebuilt:
        raise ValueError("protected raw inventory mismatch")
    return inventory
