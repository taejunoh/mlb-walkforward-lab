"""One-way, explicitly authorized migration to the offline R4 custody seed.

Run directly only with ``--execute``.  It performs no network operation and
never prints key material.  The operator must have completed code review and
backed up the Keychain before invoking this script.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import pwd
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Direct invocation is intentional operational API.  It does not make the
# custody location configurable; it merely makes the repository package
# importable when Python launches this file from ``scripts/``.
_REPOSITORY = Path(__file__).resolve().parents[1]
if str(_REPOSITORY) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY))

from mlb_backtest import v52_r4_offline_signer as _offline  # noqa: E402
from mlb_backtest.v52_r4_production_public_key import (  # noqa: E402
    PRODUCTION_KEY_ID,
    PRODUCTION_PUBLIC_KEY,
)


_SERVICE = b"mlb-walkforward-lab.v5_2.r4.custody-anchor"
_ACCOUNT = b"production-ed25519-v1"
_LEGACY_NAME = "login_renamed_1.keychain-db"


def _home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def _legacy_path() -> Path:
    # This is not configurable on purpose: migration has exactly one source.
    return _home() / "Library" / "Keychains" / _LEGACY_NAME


def _validate_legacy_path() -> tuple[Path, tuple[int, int]]:
    """No-follow, pre-use identity check of the one legacy backing file."""
    path = _legacy_path()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    opened: list[int] = [fd]
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            entry = os.stat(component, dir_fd=fd, follow_symlinks=False)
            info = os.fstat(next_fd)
            if not stat.S_ISDIR(info.st_mode) or (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("R4_LEGACY_PATH_REBIND_FAILED")
            opened.append(next_fd)
            fd = next_fd
        leaf = os.open(_LEGACY_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            entry = os.stat(_LEGACY_NAME, dir_fd=fd, follow_symlinks=False)
            info = os.fstat(leaf)
            if not stat.S_ISREG(info.st_mode) or (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino):
                raise RuntimeError("R4_LEGACY_PATH_REBIND_FAILED")
            return path, (info.st_dev, info.st_ino)
        finally:
            os.close(leaf)
    finally:
        for opened_fd in reversed(opened):
            os.close(opened_fd)


def _revalidate_legacy_path(path: Path, expected: tuple[int, int]) -> None:
    actual, identity = _validate_legacy_path()
    if actual != path or identity != expected:
        raise RuntimeError("R4_LEGACY_PATH_REBIND_FAILED")


class _LegacyExact:
    def __init__(self, security: object, foundation: object, keychain: ctypes.c_void_p, item: ctypes.c_void_p, data: ctypes.c_void_p, length: int, path: Path, identity: tuple[int, int]) -> None:
        self.security = security
        self.foundation = foundation
        self.keychain = keychain
        self.item = item
        self.data = data
        self.length = length
        self.path = path
        self.identity = identity

    def copy_seed(self) -> bytearray:
        if not self.data or self.length != 32:
            raise RuntimeError("R4_LEGACY_ITEM_UNAVAILABLE")
        result = bytearray(self.length)
        target = (ctypes.c_ubyte * self.length).from_buffer(result)
        ctypes.memmove(target, self.data, self.length)
        return result

    def release(self) -> None:
        if self.data:
            self.security.SecKeychainItemFreeContent(None, self.data)
            self.data = ctypes.c_void_p()
        if self.item:
            self.foundation.CFRelease(self.item)
            self.item = ctypes.c_void_p()
        if self.keychain:
            self.foundation.CFRelease(self.keychain)
            self.keychain = ctypes.c_void_p()


def _read_legacy_exact() -> _LegacyExact:
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    reference = ctypes.c_void_p
    status = ctypes.c_int32
    security.SecKeychainOpen.argtypes = (ctypes.c_char_p, ctypes.POINTER(reference))
    security.SecKeychainOpen.restype = status
    security.SecKeychainGetPath.argtypes = (reference, ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p)
    security.SecKeychainGetPath.restype = status
    security.SecKeychainFindGenericPassword.argtypes = (
        reference, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(reference),
    )
    security.SecKeychainFindGenericPassword.restype = status
    security.SecKeychainItemFreeContent.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    foundation.CFRelease.argtypes = (ctypes.c_void_p,)
    path, identity = _validate_legacy_path()
    keychain = reference()
    item = reference()
    data = ctypes.c_void_p()
    length = ctypes.c_uint32()
    try:
        if int(security.SecKeychainOpen(os.fsencode(path), ctypes.byref(keychain))) != 0:
            raise RuntimeError("R4_LEGACY_ITEM_UNAVAILABLE")
        returned_length = ctypes.c_uint32(4096)
        returned = ctypes.create_string_buffer(returned_length.value)
        if int(security.SecKeychainGetPath(keychain, ctypes.byref(returned_length), returned)) != 0:
            raise RuntimeError("R4_LEGACY_PATH_REBIND_FAILED")
        if os.path.realpath(os.fsdecode(returned.value)) != os.path.realpath(path):
            raise RuntimeError("R4_LEGACY_PATH_REBIND_FAILED")
        _revalidate_legacy_path(path, identity)
        if int(security.SecKeychainFindGenericPassword(keychain, len(_SERVICE), _SERVICE, len(_ACCOUNT), _ACCOUNT, ctypes.byref(length), ctypes.byref(data), ctypes.byref(item))) != 0 or length.value != 32:
            raise RuntimeError("R4_LEGACY_ITEM_UNAVAILABLE")
        _revalidate_legacy_path(path, identity)
        return _LegacyExact(security, foundation, keychain, item, data, length.value, path, identity)
    except BaseException:
        if data:
            security.SecKeychainItemFreeContent(None, data)
        if item:
            foundation.CFRelease(item)
        if keychain:
            foundation.CFRelease(keychain)
        raise


def migrate() -> None:
    """Copy, freeze, and verify once; the legacy Keychain item is retained."""
    try:
        _offline._verify_existing_seed()
        return
    except FileNotFoundError:
        pass
    except RuntimeError as existing_error:
        # Missing custody directories are unstarted migration, never an
        # instruction to overwrite an invalid/conflicting existing leaf.
        if str(existing_error) != _offline._PATH_ERROR:
            raise

    legacy = _read_legacy_exact()
    seed = bytearray()
    try:
        seed = legacy.copy_seed()
        private = Ed25519PrivateKey.from_private_bytes(seed)
        if private.public_key().public_bytes_raw() != PRODUCTION_PUBLIC_KEY:
            raise RuntimeError("R4_LEGACY_PUBLIC_KEY_MISMATCH")
        _offline._create_seed_exclusively(seed)
        _offline._verify_existing_seed()
    finally:
        _zero = _offline._zero
        _zero(seed)
        legacy.release()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="copy and verify the offline seed; retain legacy Keychain item")
    arguments = parser.parse_args(argv)
    if not arguments.execute:
        parser.error("refusing to migrate without --execute")
    migrate()
    print(f"offline_seed_ready {PRODUCTION_KEY_ID}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
