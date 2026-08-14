"""Descriptor-bound, offline-only R4 custody signer.

The only production entry point is :func:`sign_anchor_message`.  The seed is
never made a public value: migration uses the deliberately private helpers in
this module and keeps it in a mutable buffer for as little time as possible.
"""

from __future__ import annotations

import ctypes
import os
import pwd
import stat
from collections.abc import Iterable
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .v52_r4_production_public_key import PRODUCTION_PUBLIC_KEY


_RELATIVE = ("Library", "Application Support", "mlb-walkforward-lab", "keys")
_NAME = "v5_2_r4_custody_anchor.ed25519.seed"
_PATH_ERROR = "R4_OFFLINE_SEED_PATH_UNAVAILABLE"
_SEED_ERROR = "R4_OFFLINE_SEED_INVALID"
_REBIND_ERROR = "R4_OFFLINE_SEED_REBIND_FAILED"
_PUBLIC_ERROR = "R4_OFFLINE_PUBLIC_KEY_MISMATCH"
_UID = os.getuid
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LEAF_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_LEAF_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_UF_IMMUTABLE = getattr(stat, "UF_IMMUTABLE", 0x00000002)


def _zero(value: bytearray) -> None:
    """Best-effort application-level zeroization of mutable seed material."""
    for index in range(len(value)):
        value[index] = 0


def _os_home() -> Path:
    """Resolve the real account home without trusting HOME or configuration."""
    return Path(pwd.getpwuid(_UID()).pw_dir)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _custody_metadata(info: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    """All endpoint facts whose drift invalidates a custody read/freeze."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        getattr(info, "st_flags", 0),
    )


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _immutable(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_flags", 0) & _UF_IMMUTABLE)


# Test seam only.  Production leaves it as a no-op; it makes descriptor-race
# regression tests deterministic without ever allowing a path/key override.
def _after_open() -> None:
    return None


_is_immutable = _immutable


def _check_dir(info: os.stat_result, *, home: bool, private: bool) -> None:
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(_PATH_ERROR)
    if home and (info.st_uid != _UID() or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
        raise RuntimeError(_PATH_ERROR)
    if private and (info.st_uid != _UID() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError(_PATH_ERROR)


def _home_parts(home: Path) -> tuple[str, ...]:
    absolute = Path(home)
    if not absolute.is_absolute() or any(part in {"", ".", ".."} for part in absolute.parts[1:]):
        raise RuntimeError(_PATH_ERROR)
    return absolute.parts[1:]


def _walk_chain(home: Path, relative: Iterable[str], *, create: bool) -> list[tuple[int, str | None, tuple[int, int], bool, bool]]:
    """Open each path component with no-follow and retain each descriptor.

    The tuple contains the descriptor, its directory-entry name, the inode
    observed at open, and whether that component is required to be private.
    Keeping all descriptors lets later checks prove the complete path did not
    rebind while the seed was opened or frozen.
    """
    root_fd = os.open("/", _DIR_FLAGS)
    try:
        root_info = os.fstat(root_fd)
        _check_dir(root_info, home=False, private=False)
        chain: list[tuple[int, str | None, tuple[int, int], bool, bool]] = [
            (root_fd, None, _identity(root_info), False, False)
        ]
        fd = root_fd
        components = [*(_home_parts(home)), *relative]
        home_length = len(_home_parts(home))
        for index, component in enumerate(components):
            is_home = index == home_length - 1
            is_private = index >= home_length
            try:
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create or index < home_length:
                    raise RuntimeError(_PATH_ERROR) from None
                os.mkdir(component, 0o700, dir_fd=fd)
                os.fsync(fd)
                next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
            info = os.fstat(next_fd)
            _check_dir(info, home=is_home, private=is_private)
            if not _same(entry, info):
                os.close(next_fd)
                raise RuntimeError(_REBIND_ERROR)
            chain.append((next_fd, component, _identity(info), is_home, is_private))
            fd = next_fd
        return chain
    except BaseException:
        # There is no duplicate root descriptor: close every retained fd once.
        for existing, _name, _identity_value, _home, _private in locals().get("chain", []):
            try:
                os.close(existing)
            except OSError:
                pass
        if "chain" not in locals():
            os.close(root_fd)
        raise


def _close_chain(chain: list[tuple[int, str | None, tuple[int, int], bool, bool]]) -> None:
    for fd, _name, _identity_value, _home, _private in reversed(chain):
        try:
            os.close(fd)
        except OSError:
            pass


def _rebind_chain(chain: list[tuple[int, str | None, tuple[int, int], bool, bool]], home: Path) -> None:
    """Validate retained descriptors and independently reopen the full path."""
    for index, (fd, name, expected, is_home, is_private) in enumerate(chain):
        info = os.fstat(fd)
        _check_dir(info, home=is_home, private=is_private)
        if _identity(info) != expected:
            raise RuntimeError(_REBIND_ERROR)
        if index:
            parent = chain[index - 1][0]
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(entry) != expected:
                raise RuntimeError(_REBIND_ERROR)
    fresh = _walk_chain(home, _RELATIVE, create=False)
    try:
        if len(fresh) != len(chain) or any(
            fresh_item[2] != retained_item[2]
            for fresh_item, retained_item in zip(fresh, chain, strict=True)
        ):
            raise RuntimeError(_REBIND_ERROR)
    finally:
        _close_chain(fresh)


def _leaf_matches(parent_fd: int, expected: tuple[int, int]) -> os.stat_result:
    entry = os.stat(_NAME, dir_fd=parent_fd, follow_symlinks=False)
    if _identity(entry) != expected:
        raise RuntimeError(_REBIND_ERROR)
    return entry


def _check_seed(info: os.stat_result, *, frozen: bool) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _UID()
        or stat.S_IMODE(info.st_mode) != 0o400
        or info.st_nlink != 1
        or info.st_size != 32
        or (frozen and not _is_immutable(info))
    ):
        raise RuntimeError(_SEED_ERROR)


def _check_new_leaf(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != _UID()
        or stat.S_IMODE(info.st_mode) != 0o400
        or info.st_nlink != 1
        or info.st_size != 0
    ):
        raise RuntimeError(_SEED_ERROR)


def _read_exact(fd: int) -> bytearray:
    value = bytearray(32)
    offset = 0
    while offset < 32:
        received = os.readv(fd, [memoryview(value)[offset:]])
        if received <= 0:
            _zero(value)
            raise RuntimeError(_SEED_ERROR)
        offset += received
    if os.read(fd, 1):
        _zero(value)
        raise RuntimeError(_SEED_ERROR)
    return value


def _read_seed() -> bytearray:
    """Private retrieval used only by signing and one-shot migration."""
    home = _os_home()
    chain = _walk_chain(home, _RELATIVE, create=False)
    parent = chain[-1][0]
    fd: int | None = None
    seed = bytearray()
    try:
        _rebind_chain(chain, home)
        fd = os.open(_NAME, _LEAF_READ_FLAGS, dir_fd=parent)
        info = os.fstat(fd)
        _after_open()
        _leaf_matches(parent, _identity(info))
        _check_seed(info, frozen=True)
        _rebind_chain(chain, home)
        seed = _read_exact(fd)
        post_read = os.fstat(fd)
        if _custody_metadata(post_read) != _custody_metadata(info):
            raise RuntimeError(_REBIND_ERROR)
        _check_seed(post_read, frozen=True)
        _leaf_matches(parent, _identity(info))
        _rebind_chain(chain, home)
        return seed
    except BaseException:
        _zero(seed)
        raise
    finally:
        if fd is not None:
            os.close(fd)
        _close_chain(chain)


def sign_anchor_message(message: bytes) -> bytes:
    """Sign one already-domain-separated R4 anchor message with the fixed key."""
    if not isinstance(message, bytes):
        raise TypeError("R4 signing message must be bytes")
    seed = _read_seed()
    try:
        private = Ed25519PrivateKey.from_private_bytes(seed)
        if private.public_key().public_bytes_raw() != PRODUCTION_PUBLIC_KEY:
            raise RuntimeError(_PUBLIC_ERROR)
        signature = private.sign(message)
        Ed25519PublicKey.from_public_bytes(PRODUCTION_PUBLIC_KEY).verify(signature, message)
        return signature
    finally:
        _zero(seed)


def _fchflags(fd: int, flags: int) -> None:
    """Use the descriptor syscall; path-based chflags is race-prone."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        fchflags = libc.fchflags
    except AttributeError as exc:  # non-macOS test hosts cannot issue production custody
        raise RuntimeError(_SEED_ERROR) from exc
    fchflags.argtypes = (ctypes.c_int, ctypes.c_uint)
    fchflags.restype = ctypes.c_int
    if fchflags(fd, flags) != 0:
        raise OSError(ctypes.get_errno(), "fchflags failed")


def _write_all(fd: int, value: bytearray) -> None:
    offset = 0
    while offset < len(value):
        written = os.write(fd, memoryview(value)[offset:])
        if written <= 0:
            raise OSError("seed write failed")
        offset += written


def _create_seed_exclusively(seed: bytearray) -> None:
    """Private migration primitive.  It never returns seed material."""
    if not isinstance(seed, bytearray) or len(seed) != 32:
        raise RuntimeError(_SEED_ERROR)
    home = _os_home()
    chain = _walk_chain(home, _RELATIVE, create=True)
    parent = chain[-1][0]
    fd: int | None = None
    try:
        _rebind_chain(chain, home)
        fd = os.open(_NAME, _LEAF_WRITE_FLAGS, 0o400, dir_fd=parent)
        created_info = os.fstat(fd)
        created = _identity(created_info)
        _leaf_matches(parent, created)
        _check_new_leaf(created_info)
        _write_all(fd, seed)
        os.fsync(fd)
        os.fchmod(fd, 0o400)
        os.close(fd)
        fd = None
        _rebind_chain(chain, home)
        # Full no-follow reopen and validation before irreversible freeze.
        fd = os.open(_NAME, _LEAF_READ_FLAGS, dir_fd=parent)
        reopened = os.fstat(fd)
        _after_open()
        _leaf_matches(parent, created)
        if _identity(reopened) != created:
            raise RuntimeError(_REBIND_ERROR)
        _check_seed(reopened, frozen=False)
        _rebind_chain(chain, home)
        before_freeze = _custody_metadata(reopened)
        _fchflags(fd, _UF_IMMUTABLE)
        os.fsync(fd)
        final = os.fstat(fd)
        _leaf_matches(parent, created)
        expected_frozen = (*before_freeze[:6], before_freeze[6] | _UF_IMMUTABLE)
        if _custody_metadata(final) != expected_frozen or not _is_immutable(final):
            raise RuntimeError(_SEED_ERROR)
        _check_seed(final, frozen=True)
        _rebind_chain(chain, home)
        os.fsync(parent)
        # The final durability barrier is itself a race endpoint: require both
        # the named leaf and the complete lexical parent chain to remain bound.
        _leaf_matches(parent, created)
        _rebind_chain(chain, home)
        if _custody_metadata(os.fstat(fd)) != _custody_metadata(final):
            raise RuntimeError(_REBIND_ERROR)
        _check_seed(os.fstat(fd), frozen=True)
    finally:
        if fd is not None:
            os.close(fd)
        _close_chain(chain)


def _verify_existing_seed() -> None:
    """Resume helper: only a complete frozen seed matching the pinned key works."""
    seed = _read_seed()
    try:
        private = Ed25519PrivateKey.from_private_bytes(seed)
        if private.public_key().public_bytes_raw() != PRODUCTION_PUBLIC_KEY:
            raise RuntimeError(_PUBLIC_ERROR)
        message = b"MLB-R4-OFFLINE-MIGRATION-VERIFY-v1"
        signature = private.sign(message)
        Ed25519PublicKey.from_public_bytes(PRODUCTION_PUBLIC_KEY).verify(signature, message)
    finally:
        _zero(seed)
