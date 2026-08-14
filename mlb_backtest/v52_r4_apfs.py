"""Descriptor-authenticated APFS custody identities for R4 evidence."""

from __future__ import annotations

import ctypes
import errno
import os
import re
import stat
import struct
from dataclasses import dataclass


ATTR_VOL_FSTYPE = 0x00000001
ATTR_VOL_UUID = 0x00040000
ATTR_VOL_FSTYPENAME = 0x00100000
_ATTR_BIT_MAP_COUNT = 5
_ATTR_BUFFER_SIZE = 128
_ATTR_REFERENCE_SIZE = 8
_UUID_SIZE = 16
_C_INT_MAX = (1 << (ctypes.sizeof(ctypes.c_int) * 8 - 1)) - 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_VOLUME_UUID_RE = re.compile(r"[0-9a-f]{32}\Z")


class CUSTODY_UNAVAILABLE(ValueError):
    """Raised when descriptor-authenticated custody cannot be established."""


class _AttrList(ctypes.Structure):
    _fields_ = (
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    )


@dataclass(frozen=True)
class R4ObjectIdentity:
    volume_uuid: str
    inode: int
    ctime_ns: int
    file_type: str
    nlink: int
    byte_size: int
    sha256: str
    mode: int


@dataclass(frozen=True)
class R4DirectoryIdentity:
    volume_uuid: str
    inode: int
    ctime_ns: int
    file_type: str
    nlink: int
    mode: int


def _require_descriptor(fd: object) -> int:
    if (
        isinstance(fd, bool)
        or not isinstance(fd, int)
        or fd < 0
        or fd > _C_INT_MAX
    ):
        raise CUSTODY_UNAVAILABLE("invalid custody descriptor")
    return fd


def _fgetattrlist(fd: int, volume_attributes: int) -> tuple[bytes, bytes]:
    """Read APFS filesystem type and UUID directly from an open descriptor."""
    fd = _require_descriptor(fd)
    attributes = _AttrList(
        _ATTR_BIT_MAP_COUNT,
        0,
        0,
        volume_attributes | ATTR_VOL_FSTYPENAME,
        0,
        0,
        0,
    )
    buffer = ctypes.create_string_buffer(_ATTR_BUFFER_SIZE)
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "fgetattrlist", None)
    if function is None:
        raise CUSTODY_UNAVAILABLE("descriptor custody API is unavailable")
    function.argtypes = (
        ctypes.c_int,
        ctypes.POINTER(_AttrList),
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint,
    )
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        fd,
        ctypes.byref(attributes),
        buffer,
        ctypes.sizeof(buffer),
        0,
    )
    error = ctypes.get_errno()
    if result != 0 or error != 0:
        error = error or errno.EIO
        raise CUSTODY_UNAVAILABLE(
            f"descriptor custody API failed: {os.strerror(error)}"
        )

    raw = buffer.raw
    minimum_size = 4 + 4 + _UUID_SIZE + _ATTR_REFERENCE_SIZE
    reported_size = struct.unpack_from("=I", raw)[0]
    if reported_size < minimum_size or reported_size > len(raw):
        raise CUSTODY_UNAVAILABLE("descriptor custody API returned malformed buffer")

    uuid_offset = 8
    raw_uuid = raw[uuid_offset : uuid_offset + _UUID_SIZE]
    reference_offset = uuid_offset + _UUID_SIZE
    name_relative_offset, name_length = struct.unpack_from(
        "=iI", raw, reference_offset
    )
    name_offset = reference_offset + name_relative_offset
    if (
        name_relative_offset < 0
        or name_length == 0
        or name_offset < 0
        or name_offset + name_length > reported_size
    ):
        raise CUSTODY_UNAVAILABLE("descriptor custody API returned malformed buffer")
    raw_filesystem_type = raw[name_offset : name_offset + name_length]
    if (
        not raw_filesystem_type.endswith(b"\0")
        or b"\0" in raw_filesystem_type[:-1]
    ):
        raise CUSTODY_UNAVAILABLE("descriptor custody API returned malformed buffer")
    filesystem_type = raw_filesystem_type[:-1]
    if not filesystem_type:
        raise CUSTODY_UNAVAILABLE("descriptor custody API returned malformed buffer")
    return filesystem_type, raw_uuid


def apfs_volume_uuid_from_fd(fd: int) -> str:
    """Return the canonical APFS volume UUID for one already-open descriptor."""
    descriptor = _require_descriptor(fd)
    filesystem_type, raw_uuid = _fgetattrlist(
        descriptor, ATTR_VOL_FSTYPE | ATTR_VOL_UUID
    )
    if filesystem_type.lower() != b"apfs":
        raise CUSTODY_UNAVAILABLE("custody filesystem is not APFS")
    if len(raw_uuid) != _UUID_SIZE:
        raise CUSTODY_UNAVAILABLE("custody volume UUID is malformed")
    return raw_uuid.hex()


def _require_canonical_volume_uuid(value: object) -> str:
    if not isinstance(value, str) or _VOLUME_UUID_RE.fullmatch(value) is None:
        raise CUSTODY_UNAVAILABLE("custody volume UUID is malformed")
    return value


def _require_nonnegative_identity_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CUSTODY_UNAVAILABLE(f"custody {label} is malformed")
    return value


def _require_positive_identity_value(value: object, label: str) -> int:
    value = _require_nonnegative_identity_value(value, label)
    if value == 0:
        raise CUSTODY_UNAVAILABLE(f"custody {label} is malformed")
    return value


def _fstat_custody_descriptor(fd: int) -> os.stat_result:
    try:
        return os.fstat(fd)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("invalid custody descriptor") from exc


def _validate_directory_identity(expected: object) -> R4DirectoryIdentity:
    if not isinstance(expected, R4DirectoryIdentity):
        raise CUSTODY_UNAVAILABLE("directory custody identity is malformed")
    _require_canonical_volume_uuid(expected.volume_uuid)
    if expected.file_type != "directory":
        raise CUSTODY_UNAVAILABLE("directory custody identity is malformed")
    _require_positive_identity_value(expected.inode, "inode")
    _require_nonnegative_identity_value(expected.ctime_ns, "ctime")
    _require_positive_identity_value(expected.nlink, "link count")
    mode = _require_nonnegative_identity_value(expected.mode, "mode")
    if mode > 0o7777:
        raise CUSTODY_UNAVAILABLE("custody mode is malformed")
    return expected


def capture_regular_identity(
    fd: int,
    sha256: str,
) -> R4ObjectIdentity:
    """Capture a regular object's immutable R4 custody fields from its descriptor."""
    descriptor = _require_descriptor(fd)
    observed = _fstat_custody_descriptor(descriptor)
    actual_uuid = apfs_volume_uuid_from_fd(descriptor)
    canonical_uuid = _require_canonical_volume_uuid(actual_uuid)
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise CUSTODY_UNAVAILABLE("custody SHA-256 is malformed")
    if not stat.S_ISREG(observed.st_mode):
        raise CUSTODY_UNAVAILABLE("regular custody identity differs")
    inode = _require_nonnegative_identity_value(observed.st_ino, "inode")
    ctime_ns = _require_nonnegative_identity_value(observed.st_ctime_ns, "ctime")
    nlink = _require_nonnegative_identity_value(observed.st_nlink, "link count")
    byte_size = _require_nonnegative_identity_value(observed.st_size, "byte size")
    return R4ObjectIdentity(
        volume_uuid=canonical_uuid,
        inode=inode,
        ctime_ns=ctime_ns,
        file_type="regular",
        nlink=nlink,
        byte_size=byte_size,
        sha256=sha256,
        mode=stat.S_IMODE(observed.st_mode),
    )


def capture_directory_identity(fd: int) -> R4DirectoryIdentity:
    """Capture the complete directory identity from one open APFS descriptor."""
    descriptor = _require_descriptor(fd)
    observed = _fstat_custody_descriptor(descriptor)
    actual_uuid = _require_canonical_volume_uuid(apfs_volume_uuid_from_fd(descriptor))
    if not stat.S_ISDIR(observed.st_mode):
        raise CUSTODY_UNAVAILABLE("directory custody identity differs")
    return R4DirectoryIdentity(
        volume_uuid=actual_uuid,
        inode=_require_positive_identity_value(observed.st_ino, "inode"),
        ctime_ns=_require_nonnegative_identity_value(observed.st_ctime_ns, "ctime"),
        file_type="directory",
        nlink=_require_positive_identity_value(observed.st_nlink, "link count"),
        mode=stat.S_IMODE(observed.st_mode),
    )


def rebind_directory_identity(
    fd: int,
    expected: R4DirectoryIdentity,
) -> None:
    """Reject a directory that no longer has its descriptor-authenticated identity."""
    descriptor = _require_descriptor(fd)
    expected = _validate_directory_identity(expected)
    observed = _fstat_custody_descriptor(descriptor)
    actual_uuid = apfs_volume_uuid_from_fd(descriptor)
    if not stat.S_ISDIR(observed.st_mode):
        raise CUSTODY_UNAVAILABLE("directory custody identity differs")
    if actual_uuid != expected.volume_uuid:
        raise CUSTODY_UNAVAILABLE("directory volume UUID differs")
    if (
        observed.st_ino,
        observed.st_ctime_ns,
        stat.S_IMODE(observed.st_mode),
        observed.st_nlink,
    ) != (
        expected.inode,
        expected.ctime_ns,
        expected.mode,
        expected.nlink,
    ):
        raise CUSTODY_UNAVAILABLE("directory custody identity differs")
