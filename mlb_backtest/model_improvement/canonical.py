"""Canonical bytes and fail-closed immutable JSON file operations."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def canonical_json_bytes(value: object) -> bytes:
    """Serialize *value* as compact, sorted canonical JSON plus one newline."""
    serialized = json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (serialized + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest for a byte string."""
    return hashlib.sha256(value).hexdigest()


def read_regular_bytes(path: Path, label: str) -> bytes:
    """Read one stable, unaliased regular file, rejecting filesystem races."""
    with _open_parent(path, create=False) as (parent_fd, leaf, _):
        contents, identity = _read_regular_from_parent(parent_fd, leaf, label)
        _assert_parent_path_identity(parent_fd, leaf, identity, label)
        return contents


def write_immutable_json(path: Path, value: object) -> bool:
    """Create a durable JSON artifact once, or verify an existing one exactly.

    ``True`` means this call created the artifact.  ``False`` means an existing
    regular file already contained the exact canonical bytes.
    """
    contents = canonical_json_bytes(value)
    with _open_parent(path, create=True) as (parent_fd, leaf, absolute):
        descriptor = -1
        staging_name: str | None = None
        try:
            descriptor, staging_name = _open_staging(parent_fd, leaf, str(path))
            opened = os.fstat(descriptor)
            _require_regular(opened, str(path))
            _write_all(descriptor, contents, str(path))
            os.fsync(descriptor)
            created_identity = _identity(os.fstat(descriptor))
        except OSError as exc:
            if staging_name is not None:
                _unlink_staging(parent_fd, staging_name)
            raise ValueError(f"unsafe immutable artifact: {path}") from exc
        except BaseException:
            if staging_name is not None:
                _unlink_staging(parent_fd, staging_name)
            raise
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

        try:
            try:
                os.link(
                    staging_name,
                    leaf,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                existing, _ = _read_regular_from_parent(
                    parent_fd, leaf, str(path)
                )
                if existing == contents:
                    return False
                raise ValueError(f"immutable artifact differs: {path}")
            except OSError as exc:
                raise ValueError(f"unsafe immutable artifact: {path}") from exc
            _unlink_staging(parent_fd, staging_name)
            staging_name = None
            os.fsync(parent_fd)
        finally:
            if staging_name is not None:
                _unlink_staging(parent_fd, staging_name)

        # The descriptor is deliberately closed before this path-based reread.
        # This proves that the published name still denotes the bytes we wrote.
        reread = read_regular_bytes(path, str(path))
        if reread != contents:
            raise ValueError(f"immutable artifact differs: {path}")
        _assert_path_identity(absolute, created_identity, str(path))
        os.fsync(parent_fd)
        return True


def _open_staging(parent_fd: int, leaf: str, label: str) -> tuple[int, str]:
    """Create a private regular staging inode under an already-held parent."""
    flags = _leaf_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    for _ in range(32):
        name = f".{leaf}.tmp-{secrets.token_hex(16)}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            _require_regular(os.fstat(descriptor), label)
            return descriptor, name
        except BaseException:
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    raise ValueError(f"could not create immutable staging file: {label}")


def _unlink_staging(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


@contextmanager
def _open_parent(path: Path, *, create: bool) -> Iterator[tuple[int, str, Path]]:
    """Hold a no-follow descriptor chain for the parent directory of *path*."""
    try:
        absolute = Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsafe immutable artifact path: {path}") from exc
    if not absolute.name or absolute.name in {".", ".."}:
        raise ValueError(f"unsafe immutable artifact path: {path}")

    descriptors: list[int] = []
    try:
        descriptors.append(os.open("/", _directory_open_flags()))
        parent_names = absolute.parts[1:-1]
        for name in parent_names:
            try:
                child = _open_directory_child(descriptors[-1], name)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, 0o700, dir_fd=descriptors[-1])
                except FileExistsError:
                    pass
                child = _open_directory_child(descriptors[-1], name)
                os.fsync(descriptors[-1])
            descriptors.append(child)
        _assert_chain(descriptors, parent_names, str(path))
        yield descriptors[-1], absolute.name, absolute
        _assert_chain(descriptors, parent_names, str(path))
    except FileNotFoundError as exc:
        raise ValueError(f"unsafe immutable artifact: {path}") from exc
    except OSError as exc:
        raise ValueError(f"unsafe immutable artifact: {path}") from exc
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _directory_open_flags() -> int:
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not directory or not nofollow or not cloexec:
        raise ValueError("immutable artifact filesystem support is unavailable")
    return os.O_RDONLY | directory | nofollow | cloexec


def _leaf_open_flags(access: int) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not cloexec:
        raise ValueError("immutable artifact filesystem support is unavailable")
    return access | nofollow | cloexec


def _open_directory_child(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(named.st_mode):
            raise ValueError("immutable artifact parent is not a directory")
        if _identity(opened) != _identity(named):
            raise ValueError("immutable artifact parent changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_chain(descriptors: list[int], names: tuple[str, ...], label: str) -> None:
    try:
        for index, name in enumerate(names):
            opened = os.fstat(descriptors[index + 1])
            named = os.stat(name, dir_fd=descriptors[index], follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or _identity(opened) != _identity(named)
            ):
                raise ValueError(f"unsafe immutable artifact parent: {label}")
    except OSError as exc:
        raise ValueError(f"unsafe immutable artifact parent: {label}") from exc


def _read_regular_from_parent(
    parent_fd: int, leaf: str, label: str
) -> tuple[bytes, tuple[int, ...]]:
    descriptor = -1
    try:
        named_before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        _require_regular(named_before, label)
        descriptor = os.open(
            leaf,
            _leaf_open_flags(os.O_RDONLY),
            dir_fd=parent_fd,
        )
        opened_before = os.fstat(descriptor)
        _require_same_file(opened_before, named_before, label)

        return _read_stable_pass(descriptor, parent_fd, leaf, opened_before, label)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"unsafe regular file: {label}") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_stable_pass(
    descriptor: int,
    parent_fd: int,
    leaf: str,
    baseline: os.stat_result,
    label: str,
) -> tuple[bytes, tuple[int, ...]]:
    first = _read_full(descriptor, baseline.st_size, label)
    opened_after = os.fstat(descriptor)
    named_after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    _require_regular(opened_after, label)
    _require_regular(named_after, label)
    if (
        _content_identity(opened_after) != _content_identity(baseline)
        or _content_identity(named_after) != _content_identity(baseline)
    ):
        raise ValueError(f"unsafe regular file changed while reading: {label}")

    second_baseline = os.fstat(descriptor)
    _require_same_file(second_baseline, baseline, label)
    second = _read_full(descriptor, second_baseline.st_size, label)
    opened_final = os.fstat(descriptor)
    named_final = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    _require_regular(opened_final, label)
    _require_regular(named_final, label)
    if (
        _content_identity(opened_final) != _content_identity(baseline)
        or _content_identity(named_final) != _content_identity(baseline)
        or second != first
    ):
        raise ValueError(f"unsafe regular file changed while reading: {label}")
    return first, _identity(opened_final)


def _read_full(descriptor: int, size: int, label: str) -> bytes:
    if size < 0:
        raise ValueError(f"unsafe regular file size: {label}")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
    except OSError as exc:
        raise ValueError(f"unsafe regular file cannot be rewound: {label}") from exc
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError(f"unsafe regular file ended while reading: {label}")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise ValueError(f"unsafe regular file grew while reading: {label}")
    return b"".join(chunks)


def _write_all(descriptor: int, contents: bytes, label: str) -> None:
    offset = 0
    while offset < len(contents):
        written = os.write(descriptor, contents[offset:])
        if written <= 0:
            raise ValueError(f"unsafe immutable artifact write: {label}")
        offset += written


def _require_regular(result: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(result.st_mode):
        raise ValueError(f"unsafe regular file: {label}")


def _require_same_file(
    opened: os.stat_result, named: os.stat_result, label: str
) -> None:
    _require_regular(opened, label)
    _require_regular(named, label)
    if _identity(opened) != _identity(named):
        raise ValueError(f"unsafe regular file identity changed: {label}")


def _identity(result: os.stat_result) -> tuple[int, ...]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _content_identity(result: os.stat_result) -> tuple[int, ...]:
    return _identity(result)


def _assert_parent_path_identity(
    parent_fd: int,
    leaf: str,
    expected: tuple[int, ...],
    label: str,
) -> None:
    try:
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        _require_regular(named, label)
        if _identity(named) != expected:
            raise ValueError(f"unsafe regular file changed after reading: {label}")
    except (OSError, ValueError) as exc:
        raise ValueError(f"unsafe regular file changed after reading: {label}") from exc


def _assert_path_identity(
    absolute: Path, expected: tuple[int, ...], label: str
) -> None:
    try:
        with _open_parent(absolute, create=False) as (parent_fd, leaf, _):
            named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            _require_regular(named, label)
            # Hard-link publication changes ctime while the inode is staged;
            # dev/inode and content metadata must remain bound.
            if _identity(named)[:-1] != expected[:-1]:
                raise ValueError(f"unsafe regular file changed after reading: {label}")
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"unsafe regular file changed after reading: {label}") from exc
