from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

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
_MANIFEST_KEYS = {
    "source",
    "query",
    "sha256",
    "retrieved_at_utc",
    "row_count",
    "schema_version",
}
_QUERY_TYPES: dict[str, dict[str, type]] = {
    "baseball_savant_statcast": {
        "end": str,
        "game_type": str,
        "start": str,
        "url": str,
    },
    "mlb_schedule": {
        "end": str,
        "gameType": str,
        "role": str,
        "start": str,
        "url": str,
    },
    "mlb_stats_pitcher_bf": {
        "endDate": str,
        "gameType": str,
        "group": str,
        "pitcher_id": int,
        "sportIds": int,
        "startDate": str,
        "stats": str,
        "url": str,
    },
    "mlb_stats_pitching": {
        "endDate": str,
        "gameType": str,
        "group": str,
        "limit": int,
        "playerPool": str,
        "sportIds": int,
        "startDate": str,
        "stats": str,
        "url": str,
    },
}
_STREAM_CHUNK_BYTES = 1024 * 1024
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")
_NON_STRIPPED_BYTE = re.compile(rb"[^ \t\n\r\v\f]")
_LeafIdentity = tuple[int, int, int, int, int, int, int]
_SourceMembership = dict[str, dict[str, _LeafIdentity]]
ProtectedMembershipSnapshot = tuple[tuple[str, str, _LeafIdentity], ...]


class _NonemptyRowCounter:
    """Count nonempty bytes.splitlines rows without retaining line contents."""

    __slots__ = ("_line_has_content", "_previous_chunk_ended_cr", "_row_count")

    def __init__(self) -> None:
        self._line_has_content = False
        self._previous_chunk_ended_cr = False
        self._row_count = 0

    @property
    def retained_byte_count(self) -> int:
        return 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        offset = 0
        if self._previous_chunk_ended_cr:
            self._previous_chunk_ended_cr = False
            if chunk[0] == 0x0A:
                offset = 1
        for match in _LINE_BREAK.finditer(chunk, offset):
            if not self._line_has_content and _NON_STRIPPED_BYTE.search(
                chunk, offset, match.start()
            ):
                self._line_has_content = True
            self._row_count += int(self._line_has_content)
            self._line_has_content = False
            offset = match.end()
            self._previous_chunk_ended_cr = match.group() == b"\r" and offset == len(
                chunk
            )
        if not self._line_has_content and _NON_STRIPPED_BYTE.search(chunk, offset):
            self._line_has_content = True

    def finish(self) -> int:
        return self._row_count + int(self._line_has_content)


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


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


class _BoundLegacyTree:
    def __init__(self, root: Path, legacy_root: Path) -> None:
        self.root = root
        self.legacy_root = legacy_root
        self._directory_bindings: list[tuple[int, str, int, os.stat_result, str]] = []
        self._source_fds: dict[str, int] = {}
        self._fds: list[int] = []
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            filesystem_fd = os.open(os.sep, directory_flags)
            self._fds.append(filesystem_fd)
            parent_fd = filesystem_fd
            for component in root.parts[1:]:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                self._fds.append(child_fd)
                linked = os.stat(
                    component, dir_fd=parent_fd, follow_symlinks=False
                )
                opened = os.fstat(child_fd)
                self._require_bound_directory(linked, opened, "repository ancestor")
                self._directory_bindings.append(
                    (
                        parent_fd,
                        component,
                        child_fd,
                        opened,
                        "repository ancestor",
                    )
                )
                parent_fd = child_fd
            relative = legacy_root.absolute().relative_to(root)
            for index, component in enumerate(relative.parts):
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                self._fds.append(child_fd)
                linked = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
                label = (
                    "legacy root"
                    if index == len(relative.parts) - 1
                    else ("legacy root ancestor")
                )
                self._require_bound_directory(linked, opened, label)
                self._directory_bindings.append(
                    (parent_fd, component, child_fd, opened, label)
                )
                parent_fd = child_fd
            self.legacy_fd = parent_fd

            root_files: set[str] = set()
            for name in os.listdir(self.legacy_fd):
                linked = os.stat(name, dir_fd=self.legacy_fd, follow_symlinks=False)
                if stat.S_ISDIR(linked.st_mode):
                    if name not in _QUERY_TYPES:
                        raise ValueError("legacy source directory is unsupported")
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
                    root_files.add(name)
            expected_locks = {f".{source}.lock" for source in self._source_fds}
            if root_files != expected_locks:
                raise ValueError("legacy root lock membership is unexpected")
            if not self._source_fds:
                raise ValueError("legacy root contains no supported sources")
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

    def source_membership(self) -> _SourceMembership:
        """Snapshot every source leaf identity through its bound directory FD.

        The local threat boundary cannot distinguish a mutation capable of restoring
        all seven observed metadata fields, including ctime, between snapshots.
        """
        self.assert_bound()
        expected_root_names = set(self._source_fds) | {
            f".{source}.lock" for source in self._source_fds
        }
        if set(os.listdir(self.legacy_fd)) != expected_root_names:
            raise RuntimeError("legacy root membership changed")
        snapshot: _SourceMembership = {}
        for source, source_fd in sorted(self._source_fds.items()):
            names = sorted(os.listdir(source_fd))
            leaves: dict[str, _LeafIdentity] = {}
            for name in names:
                try:
                    metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeError(
                        "legacy source membership changed while listing"
                    ) from exc
                self._require_regular_metadata(metadata, "legacy raw object")
                leaves[name] = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    metadata.st_mode,
                    metadata.st_nlink,
                )
            snapshot[source] = leaves
        self.assert_bound()
        return snapshot

    def _leaf_binding(self, path: Path, label: str) -> tuple[int, str, os.stat_result]:
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


def _stream_object(
    root: Path,
    path: Path,
    label: str,
    *,
    capture_content: bool = False,
    count_rows: bool = False,
    bound_tree: _BoundLegacyTree,
) -> tuple[dict[str, object], bytes | None, int | None]:
    descriptor, opened = bound_tree.open_regular(path, label)
    digest = hashlib.sha256()
    byte_size = 0
    chunks: list[bytes] | None = [] if capture_content else None
    row_counter = _NonemptyRowCounter() if count_rows else None
    try:
        while True:
            chunk = os.read(descriptor, _STREAM_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            byte_size += len(chunk)
            if chunks is not None:
                chunks.append(chunk)
            if row_counter is not None:
                row_counter.feed(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    linked = bound_tree.assert_regular_bound(path, opened, label)
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
    return (
        {
            "byte_size": byte_size,
            "inode_fingerprint": {"device": opened.st_dev, "inode": opened.st_ino},
            "path": relative,
            "sha256": digest.hexdigest(),
        },
        b"".join(chunks) if chunks is not None else None,
        row_counter.finish() if row_counter is not None else None,
    )


def _read_regular(root: Path, path: Path, label: str) -> bytes:
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside repository root") from exc
    if not relative.parts or any(
        component in {"", ".", ".."} for component in relative.parts
    ):
        raise ValueError(f"{label} path is invalid")

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    opened_fds: list[int] = []
    bindings: list[tuple[int, str, int, os.stat_result]] = []
    try:
        parent_fd = os.open(root, directory_flags)
        opened_fds.append(parent_fd)
        for component in relative.parts[:-1]:
            try:
                child_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                linked = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"{label} parent cannot be opened safely") from exc
            opened_fds.append(child_fd)
            opened = os.fstat(child_fd)
            _BoundLegacyTree._require_bound_directory(linked, opened, f"{label} parent")
            bindings.append((parent_fd, component, child_fd, opened))
            parent_fd = child_fd

        leaf = relative.parts[-1]
        try:
            linked_before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"{label} cannot be inspected safely") from exc
        _BoundLegacyTree._require_regular_metadata(linked_before, label)
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(leaf, file_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"{label} cannot be opened safely") from exc
        opened_fds.append(descriptor)
        opened = os.fstat(descriptor)
        _BoundLegacyTree._require_regular_metadata(opened, label)
        if (linked_before.st_dev, linked_before.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise RuntimeError(f"{label} path binding changed while opening")

        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            linked_after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"{label} path binding changed after reading") from exc
        _BoundLegacyTree._require_regular_metadata(linked_after, label)
        for bound_parent, name, child_fd, expected in bindings:
            try:
                linked = os.stat(name, dir_fd=bound_parent, follow_symlinks=False)
                directory = os.fstat(child_fd)
            except OSError as exc:
                raise RuntimeError(f"{label} parent path binding changed") from exc
            _BoundLegacyTree._require_bound_directory(
                linked, directory, f"{label} parent"
            )
            if (directory.st_dev, directory.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise RuntimeError(f"{label} parent path binding changed")

        stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(opened, key) != getattr(after, key) for key in stable_fields):
            raise RuntimeError(f"{label} changed while reading")
        if any(
            getattr(after, key) != getattr(linked_after, key) for key in stable_fields
        ):
            raise RuntimeError(f"{label} changed after reading")
        content = b"".join(chunks)
        if len(content) != opened.st_size:
            raise RuntimeError(f"{label} byte count changed while reading")
        return content
    finally:
        while opened_fds:
            descriptor = opened_fds.pop()
            try:
                os.close(descriptor)
            except OSError:
                pass


def build_protected_raw_inventory(
    root: Path, legacy_root: Path
) -> Mapping[str, object]:
    """Recompute a path-bound inventory of every verified legacy raw record."""
    repository_root = _repository_root(root)
    legacy_path, legacy_relative = _under_root(
        repository_root, Path(legacy_root), "legacy root"
    )
    with _BoundLegacyTree(repository_root, legacy_path) as bound_tree:
        membership = bound_tree.source_membership()
        verified = _streaming_verified_catalog(
            repository_root, legacy_path, membership, bound_tree
        )
        records = _records_from_verified_catalog(repository_root, verified, bound_tree)
        _require_unchanged_membership(bound_tree, membership, "record reads")
        records.sort(key=lambda record: record["manifest"]["path"])
        _require_unchanged_membership(bound_tree, membership, "final return")
    return {
        "legacy_root": legacy_relative,
        "manifest_count": len(records),
        "rawstore_verification": "PASS",
        "records": records,
        "schema_version": 1,
    }


def _validate_source_query(source: object, query: object) -> dict[str, object]:
    if type(source) is not str or source not in _QUERY_TYPES:
        raise ValueError("protected raw source query source is unsupported")
    expected = _QUERY_TYPES[source]
    if type(query) is not dict or set(query) != set(expected):
        raise ValueError(f"protected raw canonical query keys are invalid for {source}")
    for key, expected_type in expected.items():
        if type(query[key]) is not expected_type:
            raise ValueError(
                f"protected raw canonical query type is invalid for {source}.{key}"
            )
    return query


def _parse_manifest(content: bytes, source: str) -> dict[str, Any]:
    """Parse RawStore's closed manifest format without reading its raw payload."""
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("immutable raw manifest is invalid") from exc
    if type(manifest) is not dict or set(manifest) != _MANIFEST_KEYS:
        raise RuntimeError("immutable raw manifest fields mismatch")
    if type(manifest["row_count"]) is not int:
        raise RuntimeError("immutable raw manifest row_count must be an int")
    if type(manifest["schema_version"]) is not int:
        raise RuntimeError("immutable raw manifest schema_version must be an int")
    if type(manifest["sha256"]) is not str or (
        len(manifest["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in manifest["sha256"])
    ):
        raise RuntimeError("immutable raw manifest sha256 is invalid")
    retrieved_at = manifest["retrieved_at_utc"]
    if type(retrieved_at) is not str:
        raise RuntimeError("immutable raw manifest timestamp is invalid")
    try:
        parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
    except ValueError as exc:
        raise RuntimeError("immutable raw manifest timestamp is invalid") from exc
    if parsed_retrieved_at.utcoffset() != timedelta(0):
        raise RuntimeError("immutable raw manifest timestamp is invalid")
    canonical_manifest = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")
    if content != canonical_manifest:
        raise RuntimeError("immutable raw manifest bytes differ")
    if manifest["source"] != source:
        raise RuntimeError("immutable raw manifest source mismatch")
    try:
        _validate_source_query(source, manifest["query"])
    except ValueError as exc:
        raise RuntimeError("immutable raw manifest source query is invalid") from exc
    return manifest


def _streaming_verified_catalog(
    repository_root: Path,
    legacy_path: Path,
    membership: _SourceMembership,
    bound_tree: _BoundLegacyTree,
) -> list[dict[str, Any]]:
    """Verify manifests and seals while deriving exact allowed leaf membership."""
    verified: list[dict[str, Any]] = []
    expected_membership: dict[str, set[str]] = {source: set() for source in membership}
    seen_queries: set[tuple[str, str]] = set()
    for source, names in sorted(membership.items()):
        for manifest_name in sorted(
            name for name in names if name.endswith(".manifest.json")
        ):
            manifest_path = legacy_path / source / manifest_name
            manifest_record, manifest_content, _ = _stream_object(
                repository_root,
                manifest_path,
                "legacy manifest",
                capture_content=True,
                bound_tree=bound_tree,
            )
            assert manifest_content is not None
            manifest = _parse_manifest(manifest_content, source)
            query_bytes = json.dumps(
                manifest["query"],
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            query_digest = hashlib.sha256(query_bytes).hexdigest()
            expected_manifest_name = (
                f"{manifest['sha256']}.{query_digest}.manifest.json"
            )
            if manifest_name != expected_manifest_name:
                raise RuntimeError("immutable raw manifest filename mismatch")
            query_key = (source, query_digest)
            if query_key in seen_queries:
                raise RuntimeError("duplicate canonical raw query provenance")
            seen_queries.add(query_key)

            seal_name = manifest_name[:-5] + ".sha256"
            raw_name = f"{manifest['sha256']}.raw"
            expected_membership[source].update({manifest_name, seal_name, raw_name})
            seal_path = legacy_path / source / seal_name
            seal_record, seal_content, _ = _stream_object(
                repository_root,
                seal_path,
                "legacy manifest seal",
                capture_content=True,
                bound_tree=bound_tree,
            )
            expected_seal = f"{manifest_record['sha256']}\n".encode("ascii")
            if seal_content != expected_seal:
                raise RuntimeError("immutable raw manifest integrity mismatch")
            verified.append(
                {
                    "manifest": manifest,
                    "manifest_content": manifest_content,
                    "manifest_path": manifest_path,
                    "manifest_record": manifest_record,
                    "payload_path": legacy_path / source / raw_name,
                    "seal_content": seal_content,
                    "seal_path": seal_path,
                    "seal_record": seal_record,
                }
            )

    for source, names in membership.items():
        if set(names) != expected_membership[source]:
            raise RuntimeError(
                f"legacy source membership contains orphan or unexpected leaves: {source}"
            )
    _require_unchanged_membership(bound_tree, membership, "catalog reads")
    return verified


def _require_unchanged_membership(
    bound_tree: _BoundLegacyTree,
    expected: _SourceMembership,
    boundary: str,
) -> None:
    if bound_tree.source_membership() != expected:
        raise RuntimeError(
            f"legacy leaf identity or membership changed after {boundary}"
        )


def _records_from_verified_catalog(
    repository_root: Path,
    verified: list[dict[str, Any]],
    bound_tree: _BoundLegacyTree,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    raw_cache: dict[Path, tuple[dict[str, object], int]] = {}
    for item in verified:
        manifest = item["manifest"]
        manifest_path = Path(item["manifest_path"])
        raw_path = Path(item["payload_path"])
        seal_path = Path(item["seal_path"])

        manifest_record, manifest_content, _ = _stream_object(
            repository_root,
            manifest_path,
            "legacy manifest",
            capture_content=True,
            bound_tree=bound_tree,
        )
        seal_record, seal_content, _ = _stream_object(
            repository_root,
            seal_path,
            "legacy manifest seal",
            capture_content=True,
            bound_tree=bound_tree,
        )
        if raw_path not in raw_cache:
            raw_record, _, raw_row_count = _stream_object(
                repository_root,
                raw_path,
                "legacy raw payload",
                count_rows=True,
                bound_tree=bound_tree,
            )
            assert raw_row_count is not None
            raw_cache[raw_path] = (raw_record, raw_row_count)
        raw_record, raw_row_count = raw_cache[raw_path]
        verification_results = (
            manifest_record == item["manifest_record"],
            manifest_content == item["manifest_content"],
            seal_record == item["seal_record"],
            seal_content == item["seal_content"],
            raw_record["sha256"] == manifest["sha256"],
            raw_row_count == manifest["row_count"],
        )
        rawstore_verified = all(verification_results)
        if not rawstore_verified:
            if raw_record["sha256"] != manifest["sha256"]:
                raise RuntimeError("immutable raw payload digest mismatch")
            if raw_row_count != manifest["row_count"]:
                raise RuntimeError("immutable raw manifest row_count mismatch")
            raise RuntimeError("legacy bytes changed after streaming verification")

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

    schema_version = require_integer(inventory["schema_version"], "schema_version", 1)
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
    manifest_count = require_integer(inventory["manifest_count"], "manifest_count", 1)
    if manifest_count != len(inventory["records"]):
        raise ValueError("protected raw inventory manifest count is invalid")
    for record in inventory["records"]:
        if type(record) is not dict or set(record) != _RECORD_KEYS:
            raise ValueError("protected raw inventory record keys are invalid")
        source = require_string(record["source"], "record source")
        _validate_source_query(source, record["canonical_query"])
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
                raise ValueError(f"protected raw inventory {kind} sha256 is invalid")
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


def parse_protected_raw_inventory_document(content: bytes) -> Mapping[str, object]:
    """Parse and validate one canonical protected-inventory document."""
    if not isinstance(content, bytes):
        raise TypeError("protected raw inventory content must be bytes")
    try:
        inventory = _validate_inventory_shape(json.loads(content.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("protected raw inventory must be canonical JSON") from exc
    if content != _canonical_json_bytes(inventory):
        raise ValueError("protected raw inventory must use canonical JSON")
    return inventory


def snapshot_protected_inventory_membership(
    root: Path, inventory: Mapping[str, object]
) -> ProtectedMembershipSnapshot:
    """Bind every protected leaf by metadata without reading payload contents."""
    repository_root = _repository_root(root)
    validated = _validate_inventory_shape(inventory)
    legacy_path = repository_root / str(validated["legacy_root"])
    expected: dict[str, dict[str, Mapping[str, object]]] = {}
    for record in validated["records"]:
        source = str(record["source"])
        source_items = expected.setdefault(source, {})
        for kind in ("manifest", "raw", "seal"):
            item = record[kind]
            relative = Path(str(item["path"]))
            if relative.parent != Path(str(validated["legacy_root"])) / source:
                raise ValueError("protected raw inventory object source path is invalid")
            previous = source_items.setdefault(relative.name, item)
            if previous != item:
                raise ValueError("protected raw inventory object binding conflicts")
    with _BoundLegacyTree(repository_root, legacy_path) as bound_tree:
        membership = bound_tree.source_membership()
        if set(membership) != set(expected):
            raise RuntimeError("protected raw source membership differs")
        for source, leaves in membership.items():
            if set(leaves) != set(expected[source]):
                raise RuntimeError("protected raw leaf membership differs")
            for name, identity in leaves.items():
                item = expected[source][name]
                fingerprint = item["inode_fingerprint"]
                if (
                    identity[0] != fingerprint["device"]
                    or identity[1] != fingerprint["inode"]
                    or identity[2] != item["byte_size"]
                ):
                    raise RuntimeError("protected raw leaf identity differs")
        bound_tree.assert_bound()
    return tuple(
        (source, name, identity)
        for source, leaves in sorted(membership.items())
        for name, identity in sorted(leaves.items())
    )


def verify_protected_raw_inventory(
    root: Path, inventory_path: Path
) -> Mapping[str, object]:
    """Verify canonical inventory bytes and recompute every protected object."""
    repository_root = _repository_root(root)
    path, _ = _under_root(repository_root, Path(inventory_path), "inventory path")
    content = _read_regular(repository_root, path, "protected raw inventory")
    inventory = parse_protected_raw_inventory_document(content)
    rebuilt = build_protected_raw_inventory(
        repository_root, repository_root / inventory["legacy_root"]
    )
    if content != _canonical_json_bytes(rebuilt):
        raise ValueError("protected raw inventory mismatch")
    return inventory


__all__ = [
    "ProtectedMembershipSnapshot",
    "build_protected_raw_inventory",
    "parse_protected_raw_inventory_document",
    "snapshot_protected_inventory_membership",
    "verify_protected_raw_inventory",
]
