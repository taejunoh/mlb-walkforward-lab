"""Darwin-only, read-only custody-parent platform preflight for R4."""

from __future__ import annotations

import ctypes
import fcntl
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .v52_r4_apfs import (
    CUSTODY_UNAVAILABLE,
    R4DirectoryIdentity,
    capture_directory_identity,
    rebind_directory_identity,
)


_VOLUME_UUID_RE = re.compile(r"[0-9a-f]{32}\Z")
_F_GETPATH = 50
_MAXPATHLEN = 1024
_FORBIDDEN_XATTR_PREFIXES = (
    b"com.apple.fileprovider.",
    b"com.apple.file-provider-domain-id",
    b"com.apple.icloud.",
)


class CUSTODY_FILE_PROVIDER_REJECTED(CUSTODY_UNAVAILABLE):
    """Raised for a positive cloud, File Provider, or non-local signal."""


@dataclass(frozen=True)
class ResourceValues:
    is_ubiquitous: bool
    volume_is_local: bool
    volume_is_internal: bool
    volume_is_removable: bool
    volume_is_read_only: bool


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or _VOLUME_UUID_RE.fullmatch(value) is None:
        raise CUSTODY_UNAVAILABLE("custody volume UUID is malformed")
    return value


def _directory_open_flags() -> int:
    required = ("O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise CUSTODY_UNAVAILABLE("safe directory-open API is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class _DarwinInspector:
    """Narrow descriptor-based boundary kept private so tests can replace it."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise CUSTODY_UNAVAILABLE("Darwin custody APIs are unavailable")
        self._directory_flags = _directory_open_flags()

    def open_root(self) -> int:
        return os.open("/", self._directory_flags)

    def open_component(self, parent_fd: int, name: str) -> int:
        return os.open(name, self._directory_flags, dir_fd=parent_fd)

    def close(self, fd: int) -> None:
        os.close(fd)

    def capture(self, fd: int) -> R4DirectoryIdentity:
        return capture_directory_identity(fd)

    def rebind(self, fd: int, identity: R4DirectoryIdentity) -> None:
        """Require a held descriptor still to have its captured identity."""
        rebind_directory_identity(fd, identity)

    def rebind_child(
        self,
        parent_fd: int,
        name: str,
        held_fd: int,
        identity: R4DirectoryIdentity,
    ) -> int:
        """Bind one held child to an already-rebound parent descriptor and name."""
        self.rebind(held_fd, identity)
        try:
            rebound_fd = self.open_component(parent_fd, name)
        except (OSError, OverflowError) as exc:
            raise CUSTODY_UNAVAILABLE("descriptor child cannot be rebound") from exc
        try:
            rebound = capture_directory_identity(rebound_fd)
            if rebound != identity:
                raise CUSTODY_UNAVAILABLE("descriptor child identity differs")
            self.rebind(held_fd, identity)
            return rebound_fd
        except BaseException:
            try:
                os.close(rebound_fd)
            except OSError:
                pass
            raise

    def before_return_rebind(self) -> None:
        """Keep a testable boundary between the last read and the final binding."""

    def _descriptor_path(self, fd: int) -> bytes:
        buffer = ctypes.create_string_buffer(_MAXPATHLEN)
        try:
            result = fcntl.fcntl(fd, _F_GETPATH, buffer)
        except (OSError, ValueError, OverflowError) as exc:
            raise CUSTODY_UNAVAILABLE("descriptor path API is unavailable") from exc
        raw = bytes(result) if isinstance(result, (bytes, bytearray)) else buffer.raw
        nul = raw.find(b"\0")
        if nul <= 0 or nul >= _MAXPATHLEN:
            raise CUSTODY_UNAVAILABLE("descriptor path is malformed")
        path = raw[:nul]
        if not path.startswith(b"/") or b"\0" in path:
            raise CUSTODY_UNAVAILABLE("descriptor path is malformed")
        return path

    def resource_values(self, fd: int, identity: R4DirectoryIdentity) -> ResourceValues:
        path = self._descriptor_path(fd)
        return self._darwin_resource_values(path)

    @staticmethod
    def _darwin_resource_values(path: bytes) -> ResourceValues:
        """Read NSURL keys via CoreFoundation without a path-based fallback."""
        try:
            core = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation",
                use_errno=True,
            )
            foundation = ctypes.CDLL(
                "/System/Library/Frameworks/Foundation.framework/Foundation",
                use_errno=True,
            )
            allocator = ctypes.c_void_p.in_dll(core, "kCFAllocatorDefault")
            keys = tuple(
                ctypes.c_void_p.in_dll(foundation, name)
                for name in (
                    "NSURLIsUbiquitousItemKey",
                    "NSURLVolumeIsLocalKey",
                    "NSURLVolumeIsInternalKey",
                    "NSURLVolumeIsRemovableKey",
                    "NSURLVolumeIsReadOnlyKey",
                )
            )
        except (AttributeError, OSError, ValueError) as exc:
            raise CUSTODY_UNAVAILABLE("NSURL resource-value API is unavailable") from exc

        create_url = getattr(core, "CFURLCreateFromFileSystemRepresentation", None)
        get_property = getattr(core, "CFURLCopyResourcePropertyForKey", None)
        release = getattr(core, "CFRelease", None)
        get_type = getattr(core, "CFGetTypeID", None)
        boolean_type = getattr(core, "CFBooleanGetTypeID", None)
        boolean_value = getattr(core, "CFBooleanGetValue", None)
        if None in (create_url, get_property, release, get_type, boolean_type, boolean_value):
            raise CUSTODY_UNAVAILABLE("NSURL resource-value API is unavailable")
        create_url.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool)
        create_url.restype = ctypes.c_void_p
        get_property.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        get_property.restype = ctypes.c_bool
        release.argtypes = (ctypes.c_void_p,)
        release.restype = None
        get_type.argtypes = (ctypes.c_void_p,)
        get_type.restype = ctypes.c_ulong
        boolean_type.argtypes = ()
        boolean_type.restype = ctypes.c_ulong
        boolean_value.argtypes = (ctypes.c_void_p,)
        boolean_value.restype = ctypes.c_bool

        url = create_url(allocator, path, len(path), True)
        if not url:
            raise CUSTODY_UNAVAILABLE("NSURL cannot be created")
        values: list[bool] = []
        try:
            expected_type = boolean_type()
            for key in keys:
                value = ctypes.c_void_p()
                error = ctypes.c_void_p()
                try:
                    succeeded = get_property(url, key, ctypes.byref(value), ctypes.byref(error))
                    if not succeeded or not value.value or error.value:
                        raise CUSTODY_UNAVAILABLE("NSURL resource value is unavailable")
                    if get_type(value) != expected_type:
                        raise CUSTODY_UNAVAILABLE("NSURL resource value is malformed")
                    values.append(bool(boolean_value(value)))
                finally:
                    if value.value:
                        release(value)
                    if error.value:
                        release(error)
        finally:
            release(url)
        return ResourceValues(*values)

    def xattr_names(self, fd: int, identity: R4DirectoryIdentity) -> tuple[bytes, ...]:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            function = getattr(libc, "flistxattr", None)
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("descriptor xattr API is unavailable") from exc
        if function is None:
            raise CUSTODY_UNAVAILABLE("descriptor xattr API is unavailable")
        function.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
        function.restype = ctypes.c_ssize_t
        ctypes.set_errno(0)
        size = function(fd, None, 0, 0)
        error = ctypes.get_errno()
        if size < 0 or error != 0:
            raise CUSTODY_UNAVAILABLE("descriptor xattr API failed")
        if size == 0:
            return ()
        buffer = ctypes.create_string_buffer(size)
        ctypes.set_errno(0)
        written = function(fd, buffer, size, 0)
        error = ctypes.get_errno()
        if written != size or error != 0:
            raise CUSTODY_UNAVAILABLE("descriptor xattr enumeration changed")
        raw = buffer.raw[:size]
        if not raw.endswith(b"\0"):
            raise CUSTODY_UNAVAILABLE("descriptor xattr enumeration is malformed")
        names = raw[:-1].split(b"\0")
        if not names or any(not name for name in names):
            raise CUSTODY_UNAVAILABLE("descriptor xattr enumeration is malformed")
        return tuple(names)


def _new_inspector() -> _DarwinInspector:
    return _DarwinInspector()


def _validate_resource_values(values: object) -> None:
    if not isinstance(values, ResourceValues) or any(
        type(value) is not bool
        for value in (
            values.is_ubiquitous,
            values.volume_is_local,
            values.volume_is_internal,
            values.volume_is_removable,
            values.volume_is_read_only,
        )
    ):
        raise CUSTODY_UNAVAILABLE("NSURL resource value is malformed")
    if (
        values.is_ubiquitous
        or not values.volume_is_local
        or not values.volume_is_internal
        or values.volume_is_removable
        or values.volume_is_read_only
    ):
        raise CUSTODY_FILE_PROVIDER_REJECTED("custody parent is not a local internal volume")


def _validate_xattrs(names: object) -> tuple[bytes, ...]:
    if not isinstance(names, tuple):
        raise CUSTODY_UNAVAILABLE("descriptor xattr enumeration is malformed")
    parsed: list[bytes] = []
    for name in names:
        if not isinstance(name, bytes) or not name or b"\0" in name:
            raise CUSTODY_UNAVAILABLE("descriptor xattr enumeration is malformed")
        if name.startswith(_FORBIDDEN_XATTR_PREFIXES):
            raise CUSTODY_FILE_PROVIDER_REJECTED("custody parent has File Provider xattrs")
        parsed.append(name)
    if len(set(parsed)) != len(parsed):
        raise CUSTODY_UNAVAILABLE("descriptor xattr enumeration is contradictory")
    return tuple(parsed)


def _rebind_full_ancestry(
    inspector: object,
    root_fd: int,
    ancestry: tuple[tuple[str, int, R4DirectoryIdentity], ...],
) -> None:
    """Bind every requested path edge from retained root to held child identities."""
    rebound_descriptors: list[int] = []
    current_parent = root_fd
    try:
        for name, held_fd, identity in ancestry:
            rebound = inspector.rebind_child(current_parent, name, held_fd, identity)
            rebound_descriptors.append(rebound)
            current_parent = rebound
    except CUSTODY_UNAVAILABLE:
        raise
    except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("custody parent ancestry cannot be rebound") from exc
    finally:
        for fd in reversed(rebound_descriptors):
            try:
                inspector.close(fd)
            except (AttributeError, OSError, OverflowError):
                pass


def _query_component(
    inspector: object,
    root_fd: int,
    ancestry: tuple[tuple[str, int, R4DirectoryIdentity], ...],
    fd: int,
    identity: R4DirectoryIdentity,
) -> None:
    """Make every critical descriptor-derived query twice, with rebinding around it."""
    try:
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        first_resource = inspector.resource_values(fd, identity)
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        _validate_resource_values(first_resource)
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        second_resource = inspector.resource_values(fd, identity)
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        _validate_resource_values(second_resource)
        if first_resource != second_resource:
            raise CUSTODY_UNAVAILABLE("NSURL resource values changed")

        _rebind_full_ancestry(inspector, root_fd, ancestry)
        first_xattrs = inspector.xattr_names(fd, identity)
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        second_xattrs = inspector.xattr_names(fd, identity)
        _rebind_full_ancestry(inspector, root_fd, ancestry)
        first_xattrs = _validate_xattrs(first_xattrs)
        second_xattrs = _validate_xattrs(second_xattrs)
        if first_xattrs != second_xattrs:
            raise CUSTODY_UNAVAILABLE("descriptor xattr names changed")
    except CUSTODY_FILE_PROVIDER_REJECTED:
        raise
    except CUSTODY_UNAVAILABLE:
        raise
    except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("custody platform inspection failed") from exc


def preflight_local_apfs_parent(parent: Path, *, allow_missing_leaf: bool) -> str:
    """Read-only, descriptor-authenticated validation of a local APFS parent path."""
    if not isinstance(parent, Path) or type(allow_missing_leaf) is not bool:
        raise CUSTODY_UNAVAILABLE("custody parent input is malformed")
    if not parent.is_absolute() or any(part == ".." for part in parent.parts):
        raise CUSTODY_UNAVAILABLE("custody parent path is not a safe absolute path")
    components = parent.parts[1:]
    if not components:
        raise CUSTODY_UNAVAILABLE("custody parent path is incomplete")

    inspector = _new_inspector()
    descriptors: list[int] = []
    ancestry: list[tuple[str, int, R4DirectoryIdentity]] = []
    expected_uuid: str | None = None
    missing_leaf = False
    try:
        try:
            descriptors.append(inspector.open_root())
        except (OSError, OverflowError) as exc:
            raise CUSTODY_UNAVAILABLE("custody root cannot be opened") from exc
        for index, component in enumerate(components):
            try:
                fd = inspector.open_component(descriptors[-1], component)
            except FileNotFoundError as exc:
                if allow_missing_leaf and index == len(components) - 1:
                    missing_leaf = True
                    break
                raise CUSTODY_UNAVAILABLE("custody parent component is missing") from exc
            except (OSError, OverflowError) as exc:
                raise CUSTODY_UNAVAILABLE("custody parent component is unsafe") from exc
            descriptors.append(fd)
            try:
                identity = inspector.capture(fd)
                uuid = _canonical_uuid(identity.volume_uuid)
                if not isinstance(identity, R4DirectoryIdentity):
                    raise CUSTODY_UNAVAILABLE("directory custody identity is malformed")
                if expected_uuid is None:
                    expected_uuid = uuid
                elif uuid != expected_uuid:
                    raise CUSTODY_UNAVAILABLE("custody parent crosses APFS volumes")
                ancestry.append((component, fd, identity))
            except CUSTODY_FILE_PROVIDER_REJECTED:
                raise
            except CUSTODY_UNAVAILABLE:
                raise
            except (AttributeError, OSError, OverflowError, TypeError, ValueError) as exc:
                raise CUSTODY_UNAVAILABLE("custody parent identity is unavailable") from exc
        if missing_leaf and expected_uuid is None:
            raise CUSTODY_UNAVAILABLE("custody parent has no existing ancestor")
        if not missing_leaf and len(descriptors) != len(components) + 1:
            raise CUSTODY_UNAVAILABLE("custody parent component is missing")
        if expected_uuid is None:
            raise CUSTODY_UNAVAILABLE("custody parent identity is unavailable")
        immutable_ancestry = tuple(ancestry)
        for _, fd, identity in immutable_ancestry:
            _query_component(
                inspector,
                descriptors[0],
                immutable_ancestry,
                fd,
                identity,
            )
        before_return = getattr(inspector, "before_return_rebind", None)
        if before_return is not None:
            before_return()
        _rebind_full_ancestry(inspector, descriptors[0], immutable_ancestry)
        return expected_uuid
    finally:
        for fd in reversed(descriptors):
            try:
                inspector.close(fd)
            except (OSError, OverflowError, AttributeError):
                pass
