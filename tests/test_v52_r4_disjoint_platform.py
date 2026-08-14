"""Read-only platform preflight tests for the R4 custody parent."""

from __future__ import annotations

import ctypes
import errno
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE, R4DirectoryIdentity
from mlb_backtest import v52_r4_disjoint_platform as platform
from mlb_backtest.v52_r4_disjoint_platform import (
    CUSTODY_FILE_PROVIDER_REJECTED,
    ResourceValues,
    preflight_local_apfs_parent,
)


UUID = "0123456789abcdef0123456789abcdef"
EXPECTED = ResourceValues(False, True, True, False, False)


class FakeInspector:
    """A descriptor-only Darwin boundary; no test uses a real custody root."""

    def __init__(self, root: Path, *, uuid_by_name=None, resources=None, xattrs=None):
        self.root = root
        self.uuid_by_name = uuid_by_name or {}
        self.resources = list(resources or [EXPECTED] * 64)
        self.xattrs = list(xattrs or [()] * 64)
        self.next_fd = 100
        self.fds: dict[int, Path] = {}
        self.closed: list[int] = []
        self.rebind_error: Exception | None = None

    def open_root(self) -> int:
        return self._put(self.root.anchor and Path(self.root.anchor) or self.root)

    def open_component(self, parent_fd: int, name: str) -> int:
        target = self.fds[parent_fd] / name
        mode = target.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise OSError("symlink")
        if not stat.S_ISDIR(mode):
            raise OSError("not a directory")
        return self._put(target)

    def _put(self, path: Path) -> int:
        fd = self.next_fd
        self.next_fd += 1
        self.fds[fd] = path
        return fd

    def close(self, fd: int) -> None:
        self.closed.append(fd)
        self.fds.pop(fd, None)

    def capture(self, fd: int) -> R4DirectoryIdentity:
        path = self.fds[fd]
        name = path.name
        return R4DirectoryIdentity(
            volume_uuid=self.uuid_by_name.get(name, UUID),
            inode=abs(hash(path)) or 1,
            ctime_ns=0,
            file_type="directory",
            nlink=1,
            mode=0o700,
        )

    def rebind(self, fd: int, identity: R4DirectoryIdentity) -> None:
        if self.rebind_error is not None:
            raise self.rebind_error

    def rebind_child(
        self,
        parent_fd: int,
        name: str,
        held_fd: int,
        identity: R4DirectoryIdentity,
    ) -> int:
        self.rebind(held_fd, identity)
        rebound_fd = self.open_component(parent_fd, name)
        if self.capture(rebound_fd) != identity:
            raise CUSTODY_UNAVAILABLE("requested parent edge was replaced")
        self.rebind(held_fd, identity)
        return rebound_fd

    def before_return_rebind(self) -> None:
        pass

    def resource_values(self, fd: int, identity: R4DirectoryIdentity) -> ResourceValues:
        if not self.resources:
            raise CUSTODY_UNAVAILABLE("resource unavailable")
        result = self.resources.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def xattr_names(self, fd: int, identity: R4DirectoryIdentity) -> tuple[bytes, ...]:
        if not self.xattrs:
            raise CUSTODY_UNAVAILABLE("xattr unavailable")
        result = self.xattrs.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class ParentEdgeRaceInspector(FakeInspector):
    """Simulates a requested pathname edge being replaced after a read seam."""

    def __init__(self, root: Path, seam: str) -> None:
        super().__init__(root)
        self.seam = seam
        self.swapped = False
        self.resource_calls = 0
        self.xattr_calls = 0

    def rebind_child(
        self,
        parent_fd: int,
        name: str,
        held_fd: int,
        identity: R4DirectoryIdentity,
    ) -> int:
        if self.swapped:
            raise CUSTODY_UNAVAILABLE("requested parent edge was replaced")
        return self._put(self.fds[parent_fd] / name)

    def resource_values(self, fd: int, identity: R4DirectoryIdentity) -> ResourceValues:
        result = super().resource_values(fd, identity)
        self.resource_calls += 1
        if self.seam == f"after_resource_{self.resource_calls}":
            self.swapped = True
        return result

    def xattr_names(self, fd: int, identity: R4DirectoryIdentity) -> tuple[bytes, ...]:
        result = super().xattr_names(fd, identity)
        self.xattr_calls += 1
        if self.seam == f"after_xattr_{self.xattr_calls}":
            self.swapped = True
        return result

    def before_return_rebind(self) -> None:
        if self.seam == "before_return":
            self.swapped = True


class FakeCFunction:
    def __init__(self, implementation):
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


class FakeCDLL:
    def __init__(self, **functions) -> None:
        self.__dict__.update(functions)


class DisjointPlatformTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve() / "Users" / "tester" / "custody-v2"
        self.base.mkdir(parents=True)
        self.before = self._snapshot()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _snapshot(self) -> set[str]:
        return {str(path.relative_to(self.temporary.name)) for path in Path(self.temporary.name).rglob("*")}

    def _call(self, inspector: FakeInspector, parent: Path | None = None, *, missing=False):
        candidate = parent or self.base
        with patch("mlb_backtest.v52_r4_disjoint_platform._new_inspector", return_value=inspector):
            return preflight_local_apfs_parent(candidate, allow_missing_leaf=missing)

    def _assert_failure_no_writes(self, expected, inspector, **kwargs) -> None:
        before = self._snapshot()
        with self.assertRaises(expected):
            self._call(inspector, **kwargs)
        self.assertEqual(before, self._snapshot())
        self.assertTrue(inspector.closed)

    def test_existing_local_non_provider_parent_is_accepted(self) -> None:
        inspector = FakeInspector(self.base)
        self.assertEqual(UUID, self._call(inspector))
        self.assertEqual(self.before, self._snapshot())
        self.assertFalse(inspector.fds)

    def test_provider_resource_signals_are_rejected(self) -> None:
        for value in (
            ResourceValues(True, True, True, False, False),
            ResourceValues(False, False, True, False, False),
            ResourceValues(False, True, False, False, False),
            ResourceValues(False, True, True, True, False),
            ResourceValues(False, True, True, False, True),
        ):
            with self.subTest(value=value):
                self._assert_failure_no_writes(
                    CUSTODY_FILE_PROVIDER_REJECTED,
                    FakeInspector(self.base, resources=[value]),
                )

    def test_forbidden_xattrs_are_rejected(self) -> None:
        for name in (
            b"com.apple.fileprovider.domain",
            b"com.apple.file-provider-domain-id",
            b"com.apple.file-provider-domain-id.child",
            b"com.apple.icloud.anything",
        ):
            with self.subTest(name=name):
                self._assert_failure_no_writes(
                    CUSTODY_FILE_PROVIDER_REJECTED,
                    FakeInspector(self.base, xattrs=[(name,), (name,)] + [()] * 62),
                )

    def test_non_provider_domain_id_near_miss_is_allowed(self) -> None:
        self.assertEqual(
            UUID,
            self._call(
                FakeInspector(
                    self.base,
                    xattrs=[(b"com.apple.file-provider-domain-ix",)] * 64,
                )
            ),
        )

    def test_unavailable_or_malformed_platform_data_fails_closed(self) -> None:
        cases = (
            FakeInspector(self.base, resources=[CUSTODY_UNAVAILABLE("framework")]),
            FakeInspector(self.base, resources=[object()]),
            FakeInspector(self.base, xattrs=[CUSTODY_UNAVAILABLE("xattr api")]),
            FakeInspector(self.base, xattrs=[(b"no-nul\x00",)]),
            FakeInspector(self.base, xattrs=[(), (b"changed",)]),
        )
        for inspector in cases:
            with self.subTest(inspector=inspector):
                self._assert_failure_no_writes(CUSTODY_UNAVAILABLE, inspector)

    def test_identity_and_resource_contradictions_fail_closed(self) -> None:
        self._assert_failure_no_writes(
            CUSTODY_UNAVAILABLE,
            FakeInspector(self.base, resources=[EXPECTED, ResourceValues(False, True, True, True, False)]),
        )
        inspector = FakeInspector(self.base)
        inspector.rebind_error = CUSTODY_UNAVAILABLE("swap")
        self._assert_failure_no_writes(CUSTODY_UNAVAILABLE, inspector)
        self._assert_failure_no_writes(
            CUSTODY_UNAVAILABLE,
            FakeInspector(self.base, uuid_by_name={"tester": "f" * 32}),
        )
        self._assert_failure_no_writes(
            CUSTODY_UNAVAILABLE,
            FakeInspector(self.base, uuid_by_name={"tester": "NOT-UUID"}),
        )

    def test_parent_edge_swap_is_rejected_after_every_read_seam_and_before_return(self) -> None:
        for seam in (
            "after_resource_1",
            "after_resource_2",
            "after_xattr_1",
            "after_xattr_2",
            "before_return",
        ):
            with self.subTest(seam=seam):
                self._assert_failure_no_writes(
                    CUSTODY_UNAVAILABLE,
                    ParentEdgeRaceInspector(self.base, seam),
                )

    def test_symlink_and_non_directory_components_fail_closed(self) -> None:
        (self.base.parent / "link").symlink_to(self.base)
        self._assert_failure_no_writes(CUSTODY_UNAVAILABLE, FakeInspector(self.base), parent=self.base.parent / "link")
        regular = self.base.parent / "regular"
        regular.write_text("x")
        self._assert_failure_no_writes(CUSTODY_UNAVAILABLE, FakeInspector(self.base), parent=regular)

    def test_missing_component_rules(self) -> None:
        missing_leaf = self.base / "missing"
        self._assert_failure_no_writes(CUSTODY_UNAVAILABLE, FakeInspector(self.base), parent=missing_leaf)
        self.assertEqual(UUID, self._call(FakeInspector(self.base), missing_leaf, missing=True))
        missing_intermediate = self.base / "missing" / "leaf"
        self._assert_failure_no_writes(CUSTODY_UNAVAILABLE, FakeInspector(self.base), parent=missing_intermediate, missing=True)


class DarwinInspectorBoundaryTests(unittest.TestCase):
    """Deterministic tests of the concrete ctypes boundary; no real path is used."""

    @staticmethod
    def _inspector():
        return object.__new__(platform._DarwinInspector)

    def test_descriptor_path_uses_fgetpath_and_fails_closed(self) -> None:
        with patch.object(platform.fcntl, "fcntl", return_value=b"/tmp/disposable\0unused") as call:
            self.assertEqual(b"/tmp/disposable", self._inspector()._descriptor_path(41))
        self.assertEqual((41, platform._F_GETPATH), call.call_args.args[:2])
        with patch.object(platform.fcntl, "fcntl", side_effect=OSError("unavailable")):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self._inspector()._descriptor_path(41)

    def _resource_libraries(self, *, error_value: int | None = None):
        released: list[int] = []
        values = iter((False, True, True, False, False))
        value_ids = iter(range(101, 106))

        create = FakeCFunction(lambda allocator, path, length, directory: 99)

        def copy_property(url, key, value_pointer, error_pointer):
            value_id = next(value_ids)
            ctypes.cast(value_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(value_id)
            ctypes.cast(error_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(
                error_value or 0
            )
            return True

        copy = FakeCFunction(copy_property)
        release = FakeCFunction(lambda value: released.append(getattr(value, "value", value)))
        get_type = FakeCFunction(lambda value: 17)
        boolean_type = FakeCFunction(lambda: 17)
        boolean_value = FakeCFunction(lambda value: next(values))
        core = FakeCDLL(
            CFURLCreateFromFileSystemRepresentation=create,
            CFURLCopyResourcePropertyForKey=copy,
            CFRelease=release,
            CFGetTypeID=get_type,
            CFBooleanGetTypeID=boolean_type,
            CFBooleanGetValue=boolean_value,
        )
        return core, FakeCDLL(), released, create, copy

    def test_resource_values_configure_cf_signatures_extract_booleans_and_release(self) -> None:
        core, foundation, released, create, copy = self._resource_libraries()
        symbols = [ctypes.c_void_p(number) for number in range(1, 7)]
        with (
            patch.object(platform.ctypes, "CDLL", side_effect=(core, foundation)),
            patch.object(platform.ctypes.c_void_p, "in_dll", side_effect=symbols),
        ):
            self.assertEqual(
                ResourceValues(False, True, True, False, False),
                platform._DarwinInspector._darwin_resource_values(b"/tmp/disposable"),
            )
        self.assertEqual(
            (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool),
            create.argtypes,
        )
        self.assertEqual(
            (
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
            ),
            copy.argtypes,
        )
        self.assertEqual([101, 102, 103, 104, 105, 99], released)

    def test_resource_value_errors_and_missing_symbols_fail_closed_and_release(self) -> None:
        core, foundation, released, _, _ = self._resource_libraries(error_value=901)
        symbols = [ctypes.c_void_p(number) for number in range(1, 7)]
        with (
            patch.object(platform.ctypes, "CDLL", side_effect=(core, foundation)),
            patch.object(platform.ctypes.c_void_p, "in_dll", side_effect=symbols),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            platform._DarwinInspector._darwin_resource_values(b"/tmp/disposable")
        self.assertEqual([101, 901, 99], released)

        missing_core = FakeCDLL()
        with (
            patch.object(platform.ctypes, "CDLL", side_effect=(missing_core, FakeCDLL())),
            patch.object(
                platform.ctypes.c_void_p,
                "in_dll",
                side_effect=[ctypes.c_void_p(number) for number in range(1, 7)],
            ),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            platform._DarwinInspector._darwin_resource_values(b"/tmp/disposable")

        with (
            patch.object(platform.ctypes, "CDLL", return_value=FakeCDLL()),
            patch.object(platform.ctypes.c_void_p, "in_dll", side_effect=ValueError("missing key")),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            platform._DarwinInspector._darwin_resource_values(b"/tmp/disposable")

    def test_flistxattr_uses_two_call_sizing_parses_nuls_and_fails_closed(self) -> None:
        names = b"alpha\0beta\0"
        calls: list[tuple[int, int, int]] = []

        def flistxattr(fd, buffer, size, options):
            calls.append((fd, size, options))
            if buffer is None:
                return len(names)
            ctypes.memmove(buffer, names, len(names))
            return len(names)

        function = FakeCFunction(flistxattr)
        with patch.object(platform.ctypes, "CDLL", return_value=FakeCDLL(flistxattr=function)):
            self.assertEqual((b"alpha", b"beta"), self._inspector().xattr_names(55, None))
        self.assertEqual([(55, 0, 0), (55, len(names), 0)], calls)
        self.assertEqual(
            (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int),
            function.argtypes,
        )

        def erange(fd, buffer, size, options):
            if buffer is None:
                return 2
            ctypes.set_errno(errno.ERANGE)
            return -1

        with patch.object(platform.ctypes, "CDLL", return_value=FakeCDLL(flistxattr=FakeCFunction(erange))):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self._inspector().xattr_names(55, None)

        def malformed(fd, buffer, size, options):
            if buffer is None:
                return 4
            ctypes.memmove(buffer, b"oops", 4)
            return 4

        malformed = FakeCFunction(malformed)
        with patch.object(platform.ctypes, "CDLL", return_value=FakeCDLL(flistxattr=malformed)):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self._inspector().xattr_names(55, None)

        with patch.object(platform.ctypes, "CDLL", return_value=FakeCDLL()):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self._inspector().xattr_names(55, None)


if __name__ == "__main__":
    unittest.main()
