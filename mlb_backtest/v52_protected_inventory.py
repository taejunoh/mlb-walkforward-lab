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


class _BoundLegacyTree:
    def __init__(self, root: Path, legacy_root: Path) -> None:
        self.root = root
        self.legacy_root = legacy_root
        self._directory_bindings: list[
            tuple[int, str, int, os.stat_result, str]
        ] = []
        self._source_fds: dict[str, int] = {}
        self._fds: list[int] = []
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            root_fd = os.open(root, directory_flags)
            self._fds.append(root_fd)
            parent_fd = root_fd
            relative = legacy_root.absolute().relative_to(root)
            for index, component in enumerate(relative.parts):
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                self._fds.append(child_fd)
                linked = os.stat(
                    component, dir_fd=parent_fd, follow_symlinks=False
                )
                opened = os.fstat(child_fd)
                label = "legacy root" if index == len(relative.parts) - 1 else (
                    "legacy root ancestor"
                )
                self._require_bound_directory(linked, opened, label)
                self._directory_bindings.append(
                    (parent_fd, component, child_fd, opened, label)
                )
                parent_fd = child_fd
            self.legacy_fd = parent_fd

            for name in os.listdir(self.legacy_fd):
                linked = os.stat(name, dir_fd=self.legacy_fd, follow_symlinks=False)
                if stat.S_ISDIR(linked.st_mode):
                    source_fd = os.open(name, directory_flags, dir_fd=self.legacy_fd)
                    self._fds.append(source_fd)
                    opened = os.fstat(source_fd)
                    self._require_bound_directory(
                        linked, opened, "legacy source directory"
                    )
                    self._directory_bindings.append(
                        (
                            self.legacy_fd,
                            name,
                            source_fd,
                            opened,
                            "legacy source directory",
                        )
                    )
                    self._source_fds[name] = source_fd
                else:
                    self._require_regular_metadata(linked, "legacy root file")
            self.assert_bound()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _require_bound_directory(
        linked: os.stat_result, opened: os.stat_result, label: str
    ) -> None:
        if not stat.S_ISDIR(linked.st_mode) or not stat.S_ISDIR(opened.st_mode):
            raise ValueError(f"{label} must be a real directory")
        if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError(f"{label} path binding changed")

    @staticmethod
    def _require_regular_metadata(metadata: os.stat_result, label: str) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file")
        if metadata.st_nlink != 1:
            raise ValueError(f"{label} hard link is forbidden")

    def assert_bound(self) -> None:
        for parent_fd, name, child_fd, expected, label in self._directory_bindings:
            try:
                linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
            except OSError as exc:
                raise RuntimeError(f"{label} path binding changed") from exc
            self._require_bound_directory(linked, opened, label)
            if (opened.st_dev, opened.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise RuntimeError(f"{label} path binding changed")

    def _leaf_binding(
        self, path: Path, label: str
    ) -> tuple[int, str, os.stat_result]:
        self.assert_bound()
        try:
            relative = path.absolute().relative_to(self.legacy_root.absolute())
        except ValueError as exc:
            raise ValueError(f"{label} escaped canonical legacy root") from exc
        if len(relative.parts) != 2:
            raise ValueError(f"{label} must be directly under a source directory")
        source, leaf = relative.parts
        if source not in self._source_fds or leaf in {"", ".", ".."}:
            raise ValueError(f"{label} source directory is not bound")
        source_fd = self._source_fds[source]
        try:
            linked = os.stat(leaf, dir_fd=source_fd, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"{label} path binding changed") from exc
        self._require_regular_metadata(linked, label)
        return source_fd, leaf, linked

    def open_regular(self, path: Path, label: str) -> tuple[int, os.stat_result]:
        source_fd, leaf, linked = self._leaf_binding(path, label)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(leaf, flags, dir_fd=source_fd)
        except OSError as exc:
            raise ValueError(f"{label} cannot be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            self._require_regular_metadata(opened, label)
            if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError(f"{label} path binding changed while opening")
            self.assert_bound()
            return descriptor, opened
        except Exception:
            os.close(descriptor)
            raise

    def assert_regular_bound(
        self, path: Path, opened: os.stat_result, label: str
    ) -> os.stat_result:
        _, _, linked = self._leaf_binding(path, label)
        if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError(f"{label} path binding changed after reading")
        return linked

    def close(self) -> None:
        while self._fds:
            descriptor = self._fds.pop()
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "_BoundLegacyTree":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


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


def _stream_object(
    root: Path,
    path: Path,
    label: str,
    *,
    capture_content: bool = False,
    count_rows: bool = False,
    bound_tree: _BoundLegacyTree | None = None,
) -> tuple[dict[str, object], bytes | None, int | None]:
    descriptor, opened = (
        bound_tree.open_regular(path, label)
        if bound_tree is not None
        else _open_regular(path, label)
    )
    digest = hashlib.sha256()
    byte_size = 0
    chunks: list[bytes] | None = [] if capture_content else None
    pending_line = b""
    row_count = 0
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_size += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
            if count_rows:
                lines = (pending_line + chunk).split(b"\n")
                pending_line = lines.pop()
                row_count += sum(bool(line.strip()) for line in lines)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    linked = (
        bound_tree.assert_regular_bound(path, opened, label)
        if bound_tree is not None
        else _validate_regular_leaf(path, label)
    )
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(opened, key) != getattr(after, key) for key in stable_fields):
        raise RuntimeError(f"{label} changed while reading")
    if any(getattr(after, key) != getattr(linked, key) for key in stable_fields):
        raise RuntimeError(f"{label} changed after reading")
    if byte_size != opened.st_size:
        raise RuntimeError(f"{label} byte count changed while reading")
    if count_rows and pending_line.strip():
        row_count += 1
    try:
        relative = path.absolute().relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"{label} must be inside repository root") from exc
    return (
        {
            "byte_size": byte_size,
            "inode_fingerprint": {"device": opened.st_dev, "inode": opened.st_ino},
            "path": relative,
            "sha256": digest.hexdigest(),
        },
        b"".join(chunks) if chunks is not None else None,
        row_count if count_rows else None,
    )


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
    with _BoundLegacyTree(repository_root, legacy_path) as bound_tree:
        verified = RawStore(legacy_path).verify_manifests()
        bound_tree.assert_bound()
        records = _records_from_verified_catalog(
            repository_root, verified, bound_tree
        )
        bound_tree.assert_bound()
    records.sort(key=lambda record: record["manifest"]["path"])
    return {
        "legacy_root": legacy_relative,
        "manifest_count": len(records),
        "rawstore_verification": "PASS",
        "records": records,
        "schema_version": 1,
    }


def _records_from_verified_catalog(
    repository_root: Path,
    verified: list[dict[str, Any]],
    bound_tree: _BoundLegacyTree,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for item in verified:
        manifest = item["manifest"]
        manifest_path = Path(item["manifest_path"])
        raw_path = Path(item["payload_path"])
        seal_path = manifest_path.with_name(manifest_path.name[:-5] + ".sha256")

        manifest_record, manifest_content, _ = _stream_object(
            repository_root,
            manifest_path,
            "legacy manifest",
            capture_content=True,
            bound_tree=bound_tree,
        )
        raw_record, _, raw_row_count = _stream_object(
            repository_root,
            raw_path,
            "legacy raw payload",
            count_rows=True,
            bound_tree=bound_tree,
        )
        seal_record, seal_content, _ = _stream_object(
            repository_root,
            seal_path,
            "legacy manifest seal",
            capture_content=True,
            bound_tree=bound_tree,
        )
        expected_seal = f'{item["manifest_sha256"]}\n'.encode("ascii")
        try:
            streamed_manifest = RawStore._read_manifest(manifest_content)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "legacy manifest changed after RawStore verification"
            ) from exc
        verification_results = (
            manifest_record["sha256"] == item["manifest_sha256"],
            streamed_manifest == manifest,
            streamed_manifest["source"] == manifest["source"],
            streamed_manifest["query"] == manifest["query"],
            raw_record["sha256"] == manifest["sha256"],
            raw_row_count == manifest["row_count"],
            seal_content == expected_seal,
            seal_record["sha256"] == hashlib.sha256(expected_seal).hexdigest(),
        )
        rawstore_verified = all(verification_results)
        if not rawstore_verified:
            raise RuntimeError(
                "legacy bytes changed after RawStore verification"
            )

        records.append(
            {
                "canonical_query": manifest["query"],
                "manifest": manifest_record,
                "raw": raw_record,
                "rawstore_verified": rawstore_verified,
                "seal": seal_record,
                "source": manifest["source"],
            }
        )
    return records


def _validate_inventory_shape(inventory: object) -> dict[str, Any]:
    if not isinstance(inventory, dict) or set(inventory) != _INVENTORY_KEYS:
        raise ValueError("protected raw inventory keys are invalid")

    def require_integer(value: object, label: str, minimum: int) -> int:
        if type(value) is not int:
            raise ValueError(f"protected raw inventory {label} must be an integer")
        if value < minimum:
            raise ValueError(f"protected raw inventory {label} is invalid")
        return value

    def require_string(value: object, label: str) -> str:
        if type(value) is not str or not value:
            raise ValueError(f"protected raw inventory {label} must be a string")
        return value

    schema_version = require_integer(
        inventory["schema_version"], "schema_version", 1
    )
    if schema_version != 1:
        raise ValueError("protected raw inventory schema version is invalid")
    legacy_root = require_string(inventory["legacy_root"], "legacy_root")
    if legacy_root != "data/v5_1_bt/raw":
        raise ValueError("protected raw inventory legacy root is invalid")
    verification = require_string(
        inventory["rawstore_verification"], "rawstore_verification"
    )
    if verification != "PASS":
        raise ValueError("protected raw inventory RawStore result is invalid")
    if type(inventory["records"]) is not list or not inventory["records"]:
        raise ValueError("protected raw inventory records are invalid")
    manifest_count = require_integer(
        inventory["manifest_count"], "manifest_count", 1
    )
    if manifest_count != len(inventory["records"]):
        raise ValueError("protected raw inventory manifest count is invalid")
    for record in inventory["records"]:
        if type(record) is not dict or set(record) != _RECORD_KEYS:
            raise ValueError("protected raw inventory record keys are invalid")
        if type(record["canonical_query"]) is not dict:
            raise ValueError(
                "protected raw inventory canonical_query must be an object"
            )
        require_string(record["source"], "record source")
        if type(record["rawstore_verified"]) is not bool:
            raise ValueError(
                "protected raw inventory rawstore_verified must be a boolean"
            )
        if not record["rawstore_verified"]:
            raise ValueError("protected raw inventory record was not verified")
        for kind in ("manifest", "raw", "seal"):
            item = record[kind]
            if type(item) is not dict or set(item) != _OBJECT_KEYS:
                raise ValueError("protected raw inventory object keys are invalid")
            require_integer(item["byte_size"], f"{kind} byte_size", 0)
            path = require_string(item["path"], f"{kind} path")
            if path.startswith("/") or "\\" in path or ".." in Path(path).parts:
                raise ValueError(f"protected raw inventory {kind} path is invalid")
            digest = require_string(item["sha256"], f"{kind} sha256")
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(
                    f"protected raw inventory {kind} sha256 is invalid"
                )
            fingerprint = item["inode_fingerprint"]
            if type(fingerprint) is not dict or set(fingerprint) != {
                "device",
                "inode",
            }:
                raise ValueError(
                    "protected raw inventory inode fingerprint keys are invalid"
                )
            require_integer(fingerprint["device"], f"{kind} device", 0)
            require_integer(fingerprint["inode"], f"{kind} inode", 1)
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
    if content != _canonical_json_bytes(rebuilt):
        raise ValueError("protected raw inventory mismatch")
    return inventory
