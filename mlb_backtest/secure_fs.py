"""Capability-based trusted directory roots for security-sensitive path operations."""

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass(frozen=True)
class RootBinding:
    """The lexical path and directory identities that define a trusted root."""

    path: Path
    component_names: tuple[str, ...]
    component_identities: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class RegularFileSnapshot:
    """The exact regular-file state observed through a trusted root."""

    relative: Path
    contents: bytes
    identity: tuple[int, ...]


_CONSTRUCTOR_TOKEN = object()


class TrustedRoot:
    """An owned descriptor chain from ``/`` to one lexical directory path."""

    def __init__(
        self,
        binding: RootBinding,
        descriptors: tuple[int, ...],
        token: object,
    ) -> None:
        if token is not _CONSTRUCTOR_TOKEN:
            raise RuntimeError("TrustedRoot instances must be opened by open_trusted_root")
        self._binding = binding
        self._descriptors = descriptors

    @property
    def path(self) -> Path:
        return self._binding.path

    @property
    def binding(self) -> RootBinding:
        return self._binding

    @property
    def root_fd(self) -> int:
        self._require_open()
        return self._descriptors[-1]

    def assert_bound(self) -> None:
        """Reject a changed component name or descriptor identity."""
        self._require_open()
        try:
            for index, descriptor in enumerate(self._descriptors):
                descriptor_stat = os.fstat(descriptor)
                actual = _identity(descriptor_stat)
                expected = self._binding.component_identities[index]
                if actual != expected or not stat.S_ISDIR(descriptor_stat.st_mode):
                    raise ValueError("unsafe trusted root binding")
                if index:
                    named = os.stat(
                        self._binding.component_names[index],
                        dir_fd=self._descriptors[index - 1],
                        follow_symlinks=False,
                    )
                    if (
                        _identity(named) != actual
                        or not stat.S_ISDIR(named.st_mode)
                    ):
                        raise ValueError("unsafe trusted root binding")
        except OSError as exc:
            raise ValueError("unsafe trusted root binding") from exc

    def dup_root_fd(self) -> int:
        self._require_open()
        try:
            return os.dup(self._descriptors[-1])
        except OSError as exc:
            raise ValueError("unsafe trusted root descriptor") from exc

    def close(self) -> None:
        """Release the complete descriptor chain. Repeated calls are harmless."""
        descriptors, self._descriptors = self._descriptors, ()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def __enter__(self) -> "TrustedRoot":
        self._require_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

    def _require_open(self) -> None:
        if not self._descriptors:
            raise RuntimeError("trusted root capability is closed")


def _secure_fs_test_hook(stage: str, component: str | None = None) -> None:
    """No-op seam for deterministic filesystem race tests."""


def open_trusted_root(
    path: Path, *, expected: RootBinding | None = None
) -> TrustedRoot:
    """Open and bind a directory chain without resolving any symlinks."""
    flags = _directory_open_flags()
    lexical_path = Path(os.path.abspath(path))
    component_names = ("/", *lexical_path.parts[1:])
    descriptors: list[int] = []
    trusted: TrustedRoot | None = None
    try:
        root_descriptor = os.open("/", flags)
        descriptors.append(root_descriptor)
        root_stat = os.fstat(root_descriptor)
        _require_directory(root_stat)
        identities = [_identity(root_stat)]
        _secure_fs_test_hook("after_component_open", "/")

        for component in component_names[1:]:
            parent_descriptor = descriptors[-1]
            descriptor = os.open(component, flags, dir_fd=parent_descriptor)
            descriptors.append(descriptor)
            opened = os.fstat(descriptor)
            named = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            _require_directory(opened)
            _require_directory(named)
            if _identity(opened) != _identity(named):
                raise ValueError("unsafe trusted root binding")
            identities.append(_identity(opened))
            _secure_fs_test_hook("after_component_open", component)

        binding = RootBinding(
            path=lexical_path,
            component_names=component_names,
            component_identities=tuple(identities),
        )
        trusted = TrustedRoot(binding, tuple(descriptors), _CONSTRUCTOR_TOKEN)
        descriptors = []
        _secure_fs_test_hook("before_return_revalidation")
        trusted.assert_bound()
        if expected is not None and binding != expected:
            raise ValueError("trusted root binding mismatch: differs from expected")
        return trusted
    except (OSError, ValueError) as exc:
        if trusted is not None:
            trusted.close()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        if isinstance(exc, ValueError):
            raise
        raise ValueError("unsafe trusted root") from exc
    except BaseException:
        if trusted is not None:
            trusted.close()
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _directory_open_flags() -> int:
    required_names = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    required_flags = tuple(getattr(os, name, 0) for name in required_names)
    if not all(required_flags):
        raise ValueError("trusted root filesystem support is unavailable")
    return os.O_RDONLY | required_flags[0] | required_flags[1] | required_flags[2]


def _identity(result: os.stat_result) -> tuple[int, int]:
    return (result.st_dev, result.st_ino)


def _require_directory(result: os.stat_result) -> None:
    if not stat.S_ISDIR(result.st_mode):
        raise ValueError("unsafe trusted root binding")


def _validate_relative(relative: Path) -> tuple[str, ...]:
    if not isinstance(relative, Path):
        raise ValueError("unsafe relative path")
    parts = relative.parts
    if (
        relative.is_absolute()
        or not parts
        or any(not part or part in (".", "..") or "\\" in part for part in parts)
    ):
        raise ValueError("unsafe relative path")
    return parts


def _regular_identity(result: os.stat_result) -> tuple[int, ...]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _regular_content_identity(result: os.stat_result) -> tuple[int, ...]:
    return _regular_identity(result)[:-1]


class _CtimeStabilizationBudget:
    def __init__(self) -> None:
        self._transition: frozenset[int] | None = None

    def consume(self, ctimes: set[int], label: str) -> None:
        transition = frozenset(ctimes)
        if len(transition) < 2:
            return
        if len(transition) != 2 or self._transition is not None:
            raise ValueError(f"unsafe regular file: {label}")
        self._transition = transition

    def covers(self, before_ctime: int, after_ctime: int) -> bool:
        return self._transition is not None and {
            before_ctime,
            after_ctime,
        } <= self._transition


def _require_regular_single_link(result: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(result.st_mode) or result.st_nlink != 1:
        raise ValueError(f"unsafe regular file: {label}")


def _leaf_open_flags(access: int) -> int:
    required_names = ("O_NOFOLLOW", "O_CLOEXEC")
    required_flags = tuple(getattr(os, name, 0) for name in required_names)
    if not all(required_flags):
        raise ValueError("trusted root filesystem support is unavailable")
    return access | os.O_NOFOLLOW | os.O_CLOEXEC


def _close_all(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _checked_child(parent_fd: int, name: str) -> int:
    descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_directory(opened)
        _require_directory(named)
        if _identity(opened) != _identity(named):
            raise ValueError("unsafe parent binding")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_parent_chain(descriptors: list[int], names: tuple[str, ...]) -> None:
    try:
        for index, name in enumerate(names):
            opened = os.fstat(descriptors[index + 1])
            named = os.stat(name, dir_fd=descriptors[index], follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or _identity(opened) != _identity(named)
            ):
                raise ValueError("unsafe parent binding")
    except OSError as exc:
        raise ValueError("unsafe parent binding") from exc


def _revalidate_parent_path(root: TrustedRoot, parts: tuple[str, ...], parent_fd: int) -> None:
    """Ensure the held parent is still the directory named below ``root``."""
    descriptors = [root.dup_root_fd()]
    try:
        for name in parts[:-1]:
            descriptors.append(_checked_child(descriptors[-1], name))
        if _identity(os.fstat(descriptors[-1])) != _identity(os.fstat(parent_fd)):
            raise ValueError("unsafe parent binding")
    finally:
        _close_all(descriptors)


@contextmanager
def open_parent(root: TrustedRoot, relative: Path, *, create: bool) -> Iterator[int]:
    """Hold the parent directory of a validated relative leaf name."""
    parts = _validate_relative(relative)
    root.assert_bound()
    descriptors = [root.dup_root_fd()]
    parent_names = parts[:-1]
    try:
        for name in parent_names:
            try:
                child = _checked_child(descriptors[-1], name)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=descriptors[-1])
                child = _checked_child(descriptors[-1], name)
                os.fsync(descriptors[-1])
                os.fsync(child)
            descriptors.append(child)
        _assert_parent_chain(descriptors, parent_names)
        yield descriptors[-1]
        _assert_parent_chain(descriptors, parent_names)
        root.assert_bound()
    finally:
        _close_all(descriptors)


def _read_bounded(descriptor: int, max_bytes: int | None, label: str) -> bytes:
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("file exceeds maximum size")
    chunks: list[bytes] = []
    total = 0
    while True:
        if max_bytes is None:
            amount = 64 * 1024
        else:
            if total > max_bytes:
                raise ValueError("file exceeds maximum size")
            amount = min(64 * 1024, max_bytes + 1 - total)
            if amount == 0:
                break
        chunk = os.read(descriptor, amount)
        if not chunk:
            break
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ValueError("file exceeds maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


def _read_stable_regular_contents(
    parent_fd: int,
    name: str,
    descriptor: int,
    named_before: os.stat_result,
    label: str,
    *,
    max_bytes: int | None,
    ctime_budget: _CtimeStabilizationBudget,
    expected_contents: bytes | None = None,
) -> tuple[bytes, tuple[int, ...]]:
    first_contents: bytes | None = None
    for attempt in range(2):
        if attempt:
            named_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened_before = os.fstat(descriptor)
        _require_regular_single_link(opened_before, label)
        _require_regular_single_link(named_before, label)
        content_identity = _regular_content_identity(opened_before)
        if content_identity != _regular_content_identity(named_before):
            raise ValueError(f"unsafe regular file: {label}")
        if max_bytes is not None and opened_before.st_size > max_bytes:
            raise ValueError("file exceeds maximum size")
        if expected_contents is not None and opened_before.st_size != len(
            expected_contents
        ):
            raise ValueError(f"unsafe regular file: {label}")

        os.lseek(descriptor, 0, os.SEEK_SET)
        contents = _read_bounded(descriptor, max_bytes, label)
        opened_after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_regular_single_link(opened_after, label)
        _require_regular_single_link(named_after, label)
        if any(
            _regular_content_identity(result) != content_identity
            for result in (opened_after, named_after)
        ):
            raise ValueError(f"unsafe regular file: {label}")
        if expected_contents is not None and contents != expected_contents:
            raise ValueError(f"unsafe regular file: {label}")
        if first_contents is not None and contents != first_contents:
            raise ValueError(f"unsafe regular file: {label}")

        identity = _regular_identity(opened_after)
        identities = {
            _regular_identity(result)
            for result in (named_before, opened_before, opened_after, named_after)
        }
        if len(identities) == 1:
            return contents, identity
        if attempt:
            raise ValueError(f"unsafe regular file: {label}")
        ctime_budget.consume({result[-1] for result in identities}, label)
        first_contents = contents
    raise ValueError(f"unsafe regular file: {label}")


def _read_regular_snapshot(
    root: TrustedRoot,
    relative: Path,
    label: str,
    ctime_budget: _CtimeStabilizationBudget,
    *,
    max_bytes: int | None = None,
    missing_ok: bool = False,
) -> RegularFileSnapshot | None:
    """Read one regular, unaliased file and bind its exact observed state."""
    parts = _validate_relative(relative)
    root.assert_bound()
    descriptor = -1
    try:
        with open_parent(root, relative, create=False) as parent_fd:
            try:
                named_before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise
            _require_regular_single_link(named_before, label)
            if max_bytes is not None and named_before.st_size > max_bytes:
                raise ValueError("file exceeds maximum size")
            descriptor = os.open(
                parts[-1],
                _leaf_open_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)),
                dir_fd=parent_fd,
            )
            contents, identity = _read_stable_regular_contents(
                parent_fd,
                parts[-1],
                descriptor,
                named_before,
                label,
                max_bytes=max_bytes,
                ctime_budget=ctime_budget,
            )
        root.assert_bound()
        return RegularFileSnapshot(relative, contents, identity)
    except FileNotFoundError:
        root.assert_bound()
        if missing_ok:
            return None
        raise ValueError(f"unsafe regular file: {label}") from None
    except OSError as exc:
        raise ValueError(f"unsafe regular file: {label}") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def read_regular_snapshot(
    root: TrustedRoot,
    relative: Path,
    label: str,
    *,
    max_bytes: int | None = None,
    missing_ok: bool = False,
) -> RegularFileSnapshot | None:
    """Read one regular, unaliased file and bind its exact observed state."""
    return _read_regular_snapshot(
        root,
        relative,
        label,
        _CtimeStabilizationBudget(),
        max_bytes=max_bytes,
        missing_ok=missing_ok,
    )


def rebind_regular_snapshot(
    root: TrustedRoot,
    expected: RegularFileSnapshot,
    label: str,
    *,
    max_bytes: int | None = None,
) -> None:
    """Require that a previously observed file remains exactly unchanged."""
    if type(expected) is not RegularFileSnapshot:
        raise ValueError("regular file snapshot mismatch")
    ctime_budget = _CtimeStabilizationBudget()
    actual = _read_regular_snapshot(
        root,
        expected.relative,
        label,
        ctime_budget,
        max_bytes=max_bytes,
    )
    if actual == expected:
        return
    if (
        actual is None
        or actual.contents != expected.contents
        or actual.identity[:-1] != expected.identity[:-1]
    ):
        raise ValueError("regular file snapshot mismatch")
    if not ctime_budget.covers(expected.identity[-1], actual.identity[-1]):
        ctime_budget.consume({expected.identity[-1], actual.identity[-1]}, label)
    stable = _read_regular_snapshot(
        root,
        expected.relative,
        label,
        ctime_budget,
        max_bytes=max_bytes,
    )
    if stable != actual:
        raise ValueError("regular file snapshot mismatch")


def _write_all(descriptor: int, contents: bytes, label: str) -> None:
    offset = 0
    while offset < len(contents):
        written = os.write(descriptor, contents[offset:])
        if written <= 0:
            raise ValueError(f"unsafe regular file: {label}")
        offset += written


def _revalidate_written_contents(
    parent_fd: int,
    name: str,
    descriptor: int,
    contents: bytes,
    label: str,
    ctime_budget: _CtimeStabilizationBudget,
) -> tuple[int, ...]:
    named_before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _, identity = _read_stable_regular_contents(
        parent_fd,
        name,
        descriptor,
        named_before,
        label,
        max_bytes=len(contents),
        ctime_budget=ctime_budget,
        expected_contents=contents,
    )
    return identity


def write_exclusive(
    root: TrustedRoot,
    relative: Path,
    contents: bytes,
    label: str,
    *,
    final_rebind: Callable[[], None] | None = None,
) -> RegularFileSnapshot:
    """Create, durably write, and bind a new private regular file."""
    if type(contents) is not bytes:
        raise TypeError("contents must be bytes")
    parts = _validate_relative(relative)
    root.assert_bound()
    descriptor = -1
    parent_fd = -1
    try:
        with open_parent(root, relative, create=True) as held_parent:
            parent_fd = held_parent
            try:
                descriptor = os.open(
                    parts[-1],
                    _leaf_open_flags(os.O_RDWR | os.O_CREAT | os.O_EXCL),
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError as exc:
                raise FileExistsError(f"{label} already exists") from exc
            opened = os.fstat(descriptor)
            _require_regular_single_link(opened, label)
            _write_all(descriptor, contents, label)
            os.fsync(descriptor)
            _secure_fs_test_hook("after_file_fsync", relative.as_posix())
            if final_rebind is not None:
                final_rebind()
            ctime_budget = _CtimeStabilizationBudget()
            root.assert_bound()
            _revalidate_parent_path(root, parts, parent_fd)
            _revalidate_written_contents(
                parent_fd,
                parts[-1],
                descriptor,
                contents,
                label,
                ctime_budget,
            )
            os.fsync(parent_fd)
            _secure_fs_test_hook("before_return_revalidation", relative.as_posix())
            root.assert_bound()
            _revalidate_parent_path(root, parts, parent_fd)
            identity = _revalidate_written_contents(
                parent_fd,
                parts[-1],
                descriptor,
                contents,
                label,
                ctime_budget,
            )
            return RegularFileSnapshot(relative, contents, identity)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
