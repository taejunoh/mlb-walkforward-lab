from __future__ import annotations

import dataclasses
import ctypes
import errno
import os
import stat
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mlb_backtest.v52_r4_apfs import (
    ATTR_VOL_FSTYPE,
    ATTR_VOL_FSTYPENAME,
    ATTR_VOL_UUID,
    CUSTODY_UNAVAILABLE,
    R4DirectoryIdentity,
    apfs_volume_uuid_from_fd,
    capture_regular_identity,
    rebind_directory_identity,
)


def _directory_stat(st_dev: int, st_ino: int, st_ctime_ns: int) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=st_dev,
        st_ino=st_ino,
        st_ctime_ns=st_ctime_ns,
        st_mode=stat.S_IFDIR | 0o555,
        st_nlink=2,
    )


def _regular_stat() -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=42,
        st_ino=9,
        st_ctime_ns=10,
        st_mode=stat.S_IFREG | 0o444,
        st_nlink=1,
        st_size=12,
    )


class R4ApfsTests(unittest.TestCase):
    def test_apfs_volume_uuid_from_fd_returns_lowercase_32_hex(self) -> None:
        with patch(
            "mlb_backtest.v52_r4_apfs._fgetattrlist",
            return_value=(
                b"apfs",
                bytes.fromhex("00112233445566778899aabbccddeeff"),
            ),
        ):
            self.assertEqual(
                apfs_volume_uuid_from_fd(17),
                "00112233445566778899aabbccddeeff",
            )

    def test_apfs_volume_uuid_from_fd_rejects_non_apfs_and_bad_uuid(self) -> None:
        with patch(
            "mlb_backtest.v52_r4_apfs._fgetattrlist",
            return_value=(b"hfs", b"x" * 16),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "APFS"):
                apfs_volume_uuid_from_fd(17)

        with patch(
            "mlb_backtest.v52_r4_apfs._fgetattrlist",
            return_value=(b"apfs", b"x" * 15),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "malformed"):
                apfs_volume_uuid_from_fd(17)

    def test_apfs_volume_uuid_from_fd_requests_fstype_and_uuid(self) -> None:
        with patch(
            "mlb_backtest.v52_r4_apfs._fgetattrlist",
            return_value=(b"APFS", b"x" * 16),
        ) as query:
            apfs_volume_uuid_from_fd(17)
        self.assertEqual(query.call_args.args, (17, ATTR_VOL_FSTYPE | ATTR_VOL_UUID))

    def test_apfs_volume_uuid_from_fd_rejects_bool_and_negative_descriptors(self) -> None:
        for descriptor in (True, -1):
            with self.subTest(descriptor=descriptor):
                with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "invalid custody"):
                    apfs_volume_uuid_from_fd(descriptor)

    def test_apfs_volume_uuid_from_fd_rejects_unrepresentable_descriptors(self) -> None:
        for descriptor in (2**32, 2**63):
            with self.subTest(descriptor=descriptor):
                with patch(
                    "mlb_backtest.v52_r4_apfs._fgetattrlist",
                    return_value=(b"apfs", b"x" * 16),
                ):
                    with self.assertRaisesRegex(
                        CUSTODY_UNAVAILABLE, "invalid custody descriptor"
                    ):
                        apfs_volume_uuid_from_fd(descriptor)

    def test_fgetattrlist_rejects_success_with_nonzero_errno(self) -> None:
        from mlb_backtest.v52_r4_apfs import _fgetattrlist

        class SuccessfulCall:
            argtypes: object
            restype: object

            def __call__(
                self,
                _fd: int,
                _attributes: object,
                buffer: object,
                _buffer_size: int,
                _options: int,
            ) -> int:
                payload = (
                    struct.pack("=II", 40, 26)
                    + bytes.fromhex("00112233445566778899aabbccddeeff")
                    + struct.pack("=iI", 8, 5)
                    + b"apfs\0\0\0\0"
                )
                ctypes.memmove(buffer, payload, len(payload))
                return 0

        function = SuccessfulCall()
        with (
            patch(
                "mlb_backtest.v52_r4_apfs.ctypes.CDLL",
                return_value=SimpleNamespace(fgetattrlist=function),
            ),
            patch("mlb_backtest.v52_r4_apfs.ctypes.get_errno", return_value=errno.EIO),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "failed"):
                _fgetattrlist(17, ATTR_VOL_FSTYPE | ATTR_VOL_UUID)

    def test_fgetattrlist_rejects_missing_symbol(self) -> None:
        from mlb_backtest.v52_r4_apfs import _fgetattrlist

        with patch(
            "mlb_backtest.v52_r4_apfs.ctypes.CDLL",
            return_value=SimpleNamespace(),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "unavailable"):
                _fgetattrlist(17, ATTR_VOL_FSTYPE | ATTR_VOL_UUID)

    def test_fgetattrlist_rejects_malformed_success_buffer(self) -> None:
        from mlb_backtest.v52_r4_apfs import _fgetattrlist

        class SuccessfulCall:
            argtypes: object
            restype: object

            def __call__(self, *_args: object) -> int:
                return 0

        with (
            patch(
                "mlb_backtest.v52_r4_apfs.ctypes.CDLL",
                return_value=SimpleNamespace(fgetattrlist=SuccessfulCall()),
            ),
            patch("mlb_backtest.v52_r4_apfs.ctypes.get_errno", return_value=0),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "malformed buffer"):
                _fgetattrlist(17, ATTR_VOL_FSTYPE | ATTR_VOL_UUID)

    def test_fgetattrlist_reads_valid_descriptor_buffer_and_requests_all_fields(
        self,
    ) -> None:
        from mlb_backtest.v52_r4_apfs import _AttrList, _fgetattrlist

        class SuccessfulCall:
            argtypes: object
            restype: object

            def __init__(self) -> None:
                self.volume_attributes: int | None = None

            def __call__(
                self,
                _fd: int,
                attributes: object,
                buffer: object,
                _buffer_size: int,
                _options: int,
            ) -> int:
                self.volume_attributes = ctypes.cast(
                    attributes, ctypes.POINTER(_AttrList)
                ).contents.volattr
                payload = (
                    struct.pack("=II", 40, 26)
                    + bytes.fromhex("00112233445566778899aabbccddeeff")
                    + struct.pack("=iI", 8, 5)
                    + b"apfs\0\0\0\0"
                )
                ctypes.memmove(buffer, payload, len(payload))
                return 0

        function = SuccessfulCall()
        with (
            patch(
                "mlb_backtest.v52_r4_apfs.ctypes.CDLL",
                return_value=SimpleNamespace(fgetattrlist=function),
            ),
            patch("mlb_backtest.v52_r4_apfs.ctypes.get_errno", return_value=0),
        ):
            filesystem_type, raw_uuid = _fgetattrlist(
                17, ATTR_VOL_FSTYPE | ATTR_VOL_UUID
            )
        self.assertEqual(filesystem_type, b"apfs")
        self.assertEqual(
            raw_uuid, bytes.fromhex("00112233445566778899aabbccddeeff")
        )
        self.assertEqual(
            function.volume_attributes,
            ATTR_VOL_FSTYPE | ATTR_VOL_UUID | ATTR_VOL_FSTYPENAME,
        )
        self.assertIs(function.argtypes[-1], ctypes.c_uint)

    def test_fgetattrlist_rejects_unterminated_filesystem_type(self) -> None:
        from mlb_backtest.v52_r4_apfs import _fgetattrlist

        class SuccessfulCall:
            argtypes: object
            restype: object

            def __call__(self, _fd: int, _attributes: object, buffer: object, *_args: object) -> int:
                payload = (
                    struct.pack("=II", 40, 26)
                    + bytes.fromhex("00112233445566778899aabbccddeeff")
                    + struct.pack("=iI", 8, 4)
                    + b"apfs\0\0\0\0"
                )
                ctypes.memmove(buffer, payload, len(payload))
                return 0

        with (
            patch(
                "mlb_backtest.v52_r4_apfs.ctypes.CDLL",
                return_value=SimpleNamespace(fgetattrlist=SuccessfulCall()),
            ),
            patch("mlb_backtest.v52_r4_apfs.ctypes.get_errno", return_value=0),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "malformed buffer"):
                _fgetattrlist(17, ATTR_VOL_FSTYPE | ATTR_VOL_UUID)

    def test_capture_regular_identity_omits_transient_device_number(self) -> None:
        with (
            patch("mlb_backtest.v52_r4_apfs.os.fstat", return_value=_regular_stat()),
            patch(
                "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                return_value="0" * 32,
            ),
        ):
            identity = capture_regular_identity(17, "a" * 64)
        self.assertEqual(
            dataclasses.asdict(identity),
            {
                "volume_uuid": "0" * 32,
                "inode": 9,
                "ctime_ns": 10,
                "file_type": "regular",
                "nlink": 1,
                "byte_size": 12,
                "sha256": "a" * 64,
                "mode": 0o444,
            },
        )

    def test_capture_regular_identity_rejects_stat_and_volume_overrides(self) -> None:
        for keyword, value in (
            ("stat_result", _regular_stat()),
            ("volume_uuid", "0" * 32),
        ):
            with self.subTest(keyword=keyword):
                with self.assertRaises(TypeError):
                    capture_regular_identity(17, "a" * 64, **{keyword: value})

    def test_capture_regular_identity_translates_closed_descriptor(self) -> None:
        descriptor = os.open(".", os.O_RDONLY)
        os.close(descriptor)
        with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "descriptor") as raised:
            capture_regular_identity(descriptor, "a" * 64)
        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_capture_regular_identity_rejects_unrepresentable_descriptor(self) -> None:
        for descriptor in (2**32, 2**63):
            with self.subTest(descriptor=descriptor):
                with (
                    patch(
                        "mlb_backtest.v52_r4_apfs.os.fstat",
                        return_value=_regular_stat(),
                    ),
                    patch(
                        "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                        return_value="0" * 32,
                    ),
                ):
                    with self.assertRaisesRegex(
                        CUSTODY_UNAVAILABLE, "invalid custody descriptor"
                    ):
                        capture_regular_identity(descriptor, "a" * 64)

    def test_capture_regular_identity_translates_fstat_overflow(self) -> None:
        with patch(
            "mlb_backtest.v52_r4_apfs.os.fstat",
            side_effect=OverflowError("descriptor outside C int range"),
        ):
            with self.assertRaises(CUSTODY_UNAVAILABLE) as raised:
                capture_regular_identity(17, "a" * 64)
        self.assertIsInstance(raised.exception.__cause__, OverflowError)


class R4ApfsRebindTests(unittest.TestCase):
    def test_rebind_ignores_changed_device_but_rejects_changed_volume_uuid(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        with (
            patch(
                "mlb_backtest.v52_r4_apfs.os.fstat",
                return_value=_directory_stat(99, 9, 10),
            ),
            patch(
                "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                return_value="0" * 32,
            ),
        ):
            self.assertIsNone(rebind_directory_identity(11, expected))
        with (
            patch(
                "mlb_backtest.v52_r4_apfs.os.fstat",
                return_value=_directory_stat(99, 9, 10),
            ),
            patch(
                "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                return_value="1" * 32,
            )
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "volume UUID"):
                rebind_directory_identity(11, expected)

    def test_rebind_rejects_regular_file_or_non_directory_expectation(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        with (
            patch(
                "mlb_backtest.v52_r4_apfs.os.fstat",
                return_value=SimpleNamespace(
                    st_ino=9,
                    st_ctime_ns=10,
                    st_mode=stat.S_IFREG | 0o444,
                    st_nlink=1,
                ),
            ),
            patch(
                "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                return_value="0" * 32,
            ),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "directory"):
                rebind_directory_identity(11, expected)

    def test_rebind_rejects_non_directory_expected_type(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "regular", 2, 0o555)
        with (
            patch(
                "mlb_backtest.v52_r4_apfs.os.fstat",
                return_value=_directory_stat(99, 9, 10),
            ),
            patch(
                "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                return_value="0" * 32,
            ),
        ):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "directory"):
                rebind_directory_identity(11, expected)

    def test_rebind_rejects_stat_and_volume_overrides(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        for keyword, value in (
            ("stat_result", _directory_stat(99, 9, 10)),
            ("volume_uuid", "0" * 32),
        ):
            with self.subTest(keyword=keyword):
                with self.assertRaises(TypeError):
                    rebind_directory_identity(11, expected, **{keyword: value})

    def test_rebind_translates_closed_descriptor(self) -> None:
        descriptor = os.open(".", os.O_RDONLY)
        os.close(descriptor)
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "descriptor") as raised:
            rebind_directory_identity(descriptor, expected)
        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_rebind_rejects_unrepresentable_descriptor(self) -> None:
        expected = R4DirectoryIdentity("0" * 32, 9, 10, "directory", 2, 0o555)
        for descriptor in (2**32, 2**63):
            with self.subTest(descriptor=descriptor):
                with (
                    patch(
                        "mlb_backtest.v52_r4_apfs.os.fstat",
                        return_value=_directory_stat(99, 9, 10),
                    ),
                    patch(
                        "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                        return_value="0" * 32,
                    ),
                ):
                    with self.assertRaisesRegex(
                        CUSTODY_UNAVAILABLE, "invalid custody descriptor"
                    ):
                        rebind_directory_identity(descriptor, expected)

    def test_rebind_rejects_malformed_expected_identity(self) -> None:
        cases = (
            (
                R4DirectoryIdentity("X" * 32, 1, 10, "directory", 2, 0o555),
                _directory_stat(99, 1, 10),
            ),
            (
                R4DirectoryIdentity("0" * 32, True, 10, "directory", 2, 0o555),
                _directory_stat(99, 1, 10),
            ),
            (
                R4DirectoryIdentity("0" * 32, 1, False, "directory", 2, 0o555),
                _directory_stat(99, 1, 0),
            ),
            (
                R4DirectoryIdentity("0" * 32, 1, 10, "directory", 0, 0o555),
                SimpleNamespace(
                    st_ino=1,
                    st_ctime_ns=10,
                    st_mode=stat.S_IFDIR | 0o555,
                    st_nlink=0,
                ),
            ),
            (
                R4DirectoryIdentity("0" * 32, 1, 10, "directory", 1, True),
                SimpleNamespace(
                    st_ino=1,
                    st_ctime_ns=10,
                    st_mode=stat.S_IFDIR | 0o001,
                    st_nlink=1,
                ),
            ),
        )
        for expected, observed in cases:
            with self.subTest(expected=expected):
                with (
                    patch(
                        "mlb_backtest.v52_r4_apfs.os.fstat",
                        return_value=observed,
                    ),
                    patch(
                        "mlb_backtest.v52_r4_apfs.apfs_volume_uuid_from_fd",
                        return_value=expected.volume_uuid,
                    ),
                ):
                    with self.assertRaises(CUSTODY_UNAVAILABLE):
                        rebind_directory_identity(11, expected)
