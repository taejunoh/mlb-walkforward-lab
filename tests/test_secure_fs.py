import builtins
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlb_backtest import secure_fs


def _fd_count() -> int | None:
    fd_dir = Path("/dev/fd")
    if not fd_dir.is_dir():
        return None
    return len(tuple(fd_dir.iterdir()))


class TrustedRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.path = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_regular_ancestor_replacement_after_component_open_is_rejected(self):
        parent = self.path / "parent"
        root = parent / "root"
        root.mkdir(parents=True)

        def replace(stage: str, component: str | None = None) -> None:
            if stage == "after_component_open" and component == "parent":
                parent.rename(self.path / "original-parent")
                parent.mkdir()

        with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace):
            with self.assertRaisesRegex(ValueError, "unsafe"):
                secure_fs.open_trusted_root(root)

    def test_symlink_ancestor_is_rejected_without_accessing_external_canary(self):
        parent = self.path / "parent"
        root = parent / "root"
        canary = self.path / "canary"
        root.mkdir(parents=True)
        canary.mkdir()
        canary_file = canary / "contents.bin"
        canary_bytes = b"external canary contents"
        canary_file.write_bytes(canary_bytes)
        canary_identity = (canary_file.stat().st_dev, canary_file.stat().st_ino)
        original_open = builtins.open
        original_read = os.read
        original_read_bytes = Path.read_bytes

        def is_canary_path(value: object) -> bool:
            try:
                return os.path.realpath(value) == os.fspath(canary_file)
            except TypeError:
                return False

        def guard_open(file: object, *args: object, **kwargs: object) -> object:
            if is_canary_path(file):
                raise AssertionError("external canary contents were opened")
            return original_open(file, *args, **kwargs)

        def guard_read(fd: int, size: int) -> bytes:
            identity = (os.fstat(fd).st_dev, os.fstat(fd).st_ino)
            if identity == canary_identity:
                raise AssertionError("external canary contents were read")
            return original_read(fd, size)

        def guard_read_bytes(value: Path) -> bytes:
            if is_canary_path(value):
                raise AssertionError("external canary contents were read")
            return original_read_bytes(value)

        def replace(stage: str, component: str | None = None) -> None:
            if stage == "after_component_open" and component == "parent":
                parent.rename(self.path / "original-parent")
                parent.symlink_to(canary, target_is_directory=True)

        with (
            patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace),
            patch.object(secure_fs.os, "read", side_effect=guard_read),
            patch.object(Path, "read_bytes", side_effect=guard_read_bytes),
            patch.object(builtins, "open", side_effect=guard_open),
        ):
            with self.assertRaisesRegex(ValueError, "unsafe"):
                secure_fs.open_trusted_root(root)
        self.assertEqual(canary_file.read_bytes(), canary_bytes)

    def test_expected_binding_rejects_replacement_between_operations(self):
        root = self.path / "root"
        root.mkdir()
        with secure_fs.open_trusted_root(root) as trusted:
            binding = trusted.binding
        root.rename(self.path / "original")
        root.mkdir()
        with self.assertRaisesRegex(
            ValueError, "trusted root binding mismatch: differs from expected"
        ):
            secure_fs.open_trusted_root(root, expected=binding)

    def test_close_is_idempotent_and_use_after_close_fails_closed(self):
        trusted = secure_fs.open_trusted_root(self.path)
        trusted.close()
        reused_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            trusted.close()
            os.fstat(reused_fd)
        finally:
            os.close(reused_fd)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            trusted.assert_bound()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            trusted.dup_root_fd()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            _ = trusted.root_fd

    def test_root_binding_has_immutable_identity_fields(self):
        with secure_fs.open_trusted_root(self.path) as trusted:
            binding = trusted.binding
        self.assertEqual(binding.path, Path(os.path.abspath(self.path)))
        self.assertIsInstance(binding.component_names, tuple)
        self.assertIsInstance(binding.component_identities, tuple)
        with self.assertRaises(AttributeError):
            binding.path = self.path

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_success_and_failure_do_not_leak_file_descriptors(self):
        root = self.path / "root"
        root.mkdir()
        before_success = _fd_count()
        self.assertIsNotNone(before_success)
        with secure_fs.open_trusted_root(root) as trusted:
            trusted.assert_bound()
        self.assertEqual(_fd_count(), before_success)

        with secure_fs.open_trusted_root(root) as trusted:
            expected = trusted.binding
        root.rename(self.path / "original")
        root.mkdir()
        before_failure = _fd_count()
        self.assertIsNotNone(before_failure)
        with self.assertRaisesRegex(
            ValueError, "trusted root binding mismatch: differs from expected"
        ):
            secure_fs.open_trusted_root(root, expected=expected)
        self.assertEqual(_fd_count(), before_failure)


class SecureFilePrimitiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.path = Path(self.tempdir.name)
        self.root_path = self.path / "root"
        self.root_path.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_snapshot_is_exactly_immutable_and_has_full_identity(self):
        snapshot = secure_fs.RegularFileSnapshot(
            Path("state.bin"), b"state", (1, 2, 3, 4, 5, 6, 7)
        )
        self.assertEqual(type(snapshot), secure_fs.RegularFileSnapshot)
        self.assertEqual(
            snapshot.identity, (1, 2, 3, 4, 5, 6, 7)
        )
        with self.assertRaises(AttributeError):
            snapshot.contents = b"changed"

    def test_rejects_unsafe_relative_paths(self):
        invalid = (Path("/absolute"), Path(), Path("."), Path(".."), Path("a/../b"), Path("a\\b"))
        with secure_fs.open_trusted_root(self.root_path) as root:
            for relative in invalid:
                with self.subTest(relative=relative):
                    with self.assertRaisesRegex(ValueError, "unsafe relative path"):
                        secure_fs.read_regular_snapshot(root, relative, "test")
                    with self.assertRaisesRegex(ValueError, "unsafe relative path"):
                        with secure_fs.open_parent(root, relative, create=False):
                            pass

    def test_open_parent_creates_nested_directories_with_private_permissions(self):
        with secure_fs.open_trusted_root(self.root_path) as root:
            with secure_fs.open_parent(root, Path("one/two/file.bin"), create=True) as parent_fd:
                self.assertTrue(stat_is_directory(os.fstat(parent_fd)))
        created = self.root_path / "one" / "two"
        self.assertEqual(created.stat().st_mode & 0o777, 0o700)

    def test_read_rejects_aliases_and_special_files_without_blocking(self):
        regular = self.root_path / "regular"
        regular.write_bytes(b"regular")
        (self.root_path / "symlink").symlink_to(regular)
        os.link(regular, self.root_path / "hardlink")
        os.mkfifo(self.root_path / "fifo")
        (self.root_path / "directory").mkdir()
        sock = socket.socket(socket.AF_UNIX)
        socket_path = self.root_path / "socket"
        try:
            sock.bind(os.fspath(socket_path))
            with secure_fs.open_trusted_root(self.root_path) as root:
                for name in ("symlink", "hardlink", "fifo", "directory", "socket"):
                    with self.subTest(name=name):
                        with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                            secure_fs.read_regular_snapshot(root, Path(name), "test")
        finally:
            sock.close()

    def test_read_is_bounded_and_handles_short_reads(self):
        contents = b"abcdefgh"
        (self.root_path / "data.bin").write_bytes(contents)
        original_read = os.read

        def short_read(fd: int, size: int) -> bytes:
            return original_read(fd, min(size, 2))

        with secure_fs.open_trusted_root(self.root_path) as root:
            with patch.object(secure_fs.os, "read", side_effect=short_read):
                snapshot = secure_fs.read_regular_snapshot(
                    root, Path("data.bin"), "test", max_bytes=len(contents)
                )
            self.assertEqual(snapshot.contents, contents)
            with self.assertRaisesRegex(ValueError, "file exceeds maximum size"):
                secure_fs.read_regular_snapshot(root, Path("data.bin"), "test", max_bytes=3)

    def test_missing_ok_only_suppresses_missing_leaf(self):
        with secure_fs.open_trusted_root(self.root_path) as root:
            self.assertIsNone(
                secure_fs.read_regular_snapshot(root, Path("missing"), "test", missing_ok=True)
            )

    def test_read_rejects_root_replacement_after_contents_are_read(self):
        (self.root_path / "data.bin").write_bytes(b"data")
        original_read = os.read
        moved = self.path / "original-root"
        swapped = False

        def replace_root(fd: int, size: int) -> bytes:
            nonlocal swapped
            result = original_read(fd, size)
            if not swapped:
                swapped = True
                self.root_path.rename(moved)
                self.root_path.mkdir()
            return result

        with secure_fs.open_trusted_root(self.root_path) as root:
            with patch.object(secure_fs.os, "read", side_effect=replace_root):
                with self.assertRaisesRegex(ValueError, "unsafe trusted root binding"):
                    secure_fs.read_regular_snapshot(root, Path("data.bin"), "test")

    def test_rebind_requires_exact_contents_and_identity(self):
        target = self.root_path / "data.bin"
        target.write_bytes(b"one")
        with secure_fs.open_trusted_root(self.root_path) as root:
            expected = secure_fs.read_regular_snapshot(root, Path("data.bin"), "test")
            secure_fs.rebind_regular_snapshot(root, expected, "test")
            target.write_bytes(b"two")
            with self.assertRaisesRegex(ValueError, "regular file snapshot mismatch"):
                secure_fs.rebind_regular_snapshot(root, expected, "test")

    def test_write_is_exclusive_and_handles_short_writes(self):
        original_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            return original_write(fd, data[:2])

        with secure_fs.open_trusted_root(self.root_path) as root:
            with patch.object(secure_fs.os, "write", side_effect=short_write):
                snapshot = secure_fs.write_exclusive(root, Path("nested/data.bin"), b"abcdef", "test")
            self.assertEqual(snapshot.contents, b"abcdef")
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                secure_fs.write_exclusive(root, Path("nested/data.bin"), b"changed", "test")
        self.assertEqual((self.root_path / "nested/data.bin").read_bytes(), b"abcdef")

    def test_write_rejects_non_bytes(self):
        with secure_fs.open_trusted_root(self.root_path) as root:
            with self.assertRaisesRegex(TypeError, "contents must be bytes"):
                secure_fs.write_exclusive(root, Path("data.bin"), bytearray(b"data"), "test")

    def test_write_detects_parent_swap_after_file_fsync(self):
        nested = self.root_path / "nested"
        original_nested = self.root_path / "original-nested"
        nested.mkdir()

        def replace_parent(stage: str, component: str | None = None) -> None:
            if stage == "after_file_fsync":
                nested.rename(original_nested)
                nested.mkdir()

        with secure_fs.open_trusted_root(self.root_path) as root:
            with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace_parent):
                with self.assertRaisesRegex(ValueError, "unsafe parent binding"):
                    secure_fs.write_exclusive(root, Path("nested/data.bin"), b"data", "test")
        self.assertFalse((nested / "data.bin").exists())
        self.assertEqual((original_nested / "data.bin").read_bytes(), b"data")

    def test_after_file_fsync_replacement_keeps_exact_attacker_inode_and_bytes(self):
        target = self.root_path / "data.bin"
        attacker_identity = None

        def replace_leaf(stage: str, component: str | None = None) -> None:
            nonlocal attacker_identity
            if stage == "after_file_fsync":
                target.unlink()
                target.write_bytes(b"attacker")
                result = target.stat()
                attacker_identity = (result.st_dev, result.st_ino)

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace_leaf):
                with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                    secure_fs.write_exclusive(root, Path("data.bin"), b"owned", "test")
            self.assertEqual(_fd_count(), before)
        result = target.stat()
        self.assertEqual((result.st_dev, result.st_ino), attacker_identity)
        self.assertEqual(target.read_bytes(), b"attacker")

    def test_attacker_replacement_after_cleanup_auth_is_never_unlinked(self):
        target = self.root_path / "data.bin"
        original_match = getattr(secure_fs, "_contents_match", None)
        original_unlink = os.unlink
        attacker_identity = None

        def authenticate_then_replace(*args: object, **kwargs: object) -> bool:
            nonlocal attacker_identity
            if original_match is None:
                raise AssertionError("removed cleanup helper was unexpectedly called")
            matched = original_match(*args, **kwargs)
            self.assertTrue(matched)
            original_unlink(target)
            target.write_bytes(b"attacker-after-auth")
            result = target.stat()
            attacker_identity = (result.st_dev, result.st_ino)
            return True

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with (
                patch.object(
                    secure_fs,
                    "_contents_match",
                    side_effect=authenticate_then_replace,
                    create=True,
                ),
                patch.object(secure_fs.os, "unlink", wraps=original_unlink) as unlink,
            ):
                with self.assertRaisesRegex(RuntimeError, "final rebind failed"):
                    secure_fs.write_exclusive(
                        root,
                        Path("data.bin"),
                        b"owned",
                        "test",
                        final_rebind=lambda: (_ for _ in ()).throw(
                            RuntimeError("final rebind failed")
                        ),
                    )
                unlink.assert_not_called()
            self.assertEqual(_fd_count(), before)
        if attacker_identity is None:
            self.assertEqual(target.read_bytes(), b"owned")
        else:
            result = target.stat()
            self.assertEqual((result.st_dev, result.st_ino), attacker_identity)
            self.assertEqual(target.read_bytes(), b"attacker-after-auth")

    def test_final_rebind_failure_never_unlinks_and_preserves_complete_residue(self):
        target = self.root_path / "data.bin"
        original_unlink = os.unlink
        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs.os, "unlink", wraps=original_unlink) as unlink:
                with self.assertRaisesRegex(RuntimeError, "final rebind failed"):
                    secure_fs.write_exclusive(
                        root,
                        Path("data.bin"),
                        b"owned",
                        "test",
                        final_rebind=lambda: (_ for _ in ()).throw(
                            RuntimeError("final rebind failed")
                        ),
                    )
                unlink.assert_not_called()
            self.assertEqual(_fd_count(), before)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                secure_fs.write_exclusive(root, Path("data.bin"), b"retry", "test")
        self.assertEqual(target.read_bytes(), b"owned")

    def test_short_write_error_preserves_partial_residue_and_retry_conflicts(self):
        target = self.root_path / "data.bin"
        original_write = os.write
        calls = 0

        def fail_after_short_write(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                return original_write(fd, data[:3])
            raise OSError("injected write failure")

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs.os, "write", side_effect=fail_after_short_write):
                with self.assertRaisesRegex(OSError, "injected write failure"):
                    secure_fs.write_exclusive(root, Path("data.bin"), b"abcdef", "test")
            self.assertEqual(_fd_count(), before)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                secure_fs.write_exclusive(root, Path("data.bin"), b"retry", "test")
        self.assertEqual(target.read_bytes(), b"abc")

    def test_parent_fsync_failure_preserves_complete_residue(self):
        target = self.root_path / "data.bin"
        original_fsync = os.fsync

        def fail_parent_fsync(fd: int) -> None:
            if stat_is_directory(os.fstat(fd)):
                raise OSError("injected parent fsync failure")
            original_fsync(fd)

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs.os, "fsync", side_effect=fail_parent_fsync):
                with self.assertRaisesRegex(OSError, "injected parent fsync failure"):
                    secure_fs.write_exclusive(root, Path("data.bin"), b"complete", "test")
            self.assertEqual(_fd_count(), before)
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                secure_fs.write_exclusive(root, Path("data.bin"), b"retry", "test")
        self.assertEqual(target.read_bytes(), b"complete")

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_all_file_primitive_success_and_fault_paths_close_descriptors(self):
        before = _fd_count()
        self.assertIsNotNone(before)
        (self.root_path / "read.bin").write_bytes(b"read")
        with secure_fs.open_trusted_root(self.root_path) as root:
            secure_fs.read_regular_snapshot(root, Path("read.bin"), "test")
            secure_fs.write_exclusive(root, Path("write.bin"), b"write", "test")
            with self.assertRaises(FileExistsError):
                secure_fs.write_exclusive(root, Path("write.bin"), b"write", "test")
            with self.assertRaises(ValueError):
                secure_fs.read_regular_snapshot(root, Path("absent"), "test")
        self.assertEqual(_fd_count(), before)


def stat_is_directory(result: os.stat_result) -> bool:
    return bool(result.st_mode & 0o040000)


if __name__ == "__main__":
    unittest.main()
