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

        before = _fd_count()
        with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace):
            with self.assertRaisesRegex(ValueError, "unsafe"):
                secure_fs.open_trusted_root(root)
        self.assertEqual(_fd_count(), before)

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

        before = _fd_count()
        with (
            patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace),
            patch.object(secure_fs.os, "read", side_effect=guard_read),
            patch.object(Path, "read_bytes", side_effect=guard_read_bytes),
            patch.object(builtins, "open", side_effect=guard_open),
        ):
            with self.assertRaisesRegex(ValueError, "unsafe"):
                secure_fs.open_trusted_root(root)
        self.assertEqual(_fd_count(), before)
        self.assertEqual(canary_file.read_bytes(), canary_bytes)

    def test_expected_binding_rejects_replacement_between_operations(self):
        root = self.path / "root"
        root.mkdir()
        with secure_fs.open_trusted_root(root) as trusted:
            binding = trusted.binding
        root.rename(self.path / "original")
        root.mkdir()
        before = _fd_count()
        with self.assertRaisesRegex(
            ValueError, "trusted root binding mismatch: differs from expected"
        ):
            secure_fs.open_trusted_root(root, expected=binding)
        self.assertEqual(_fd_count(), before)

    def test_close_is_idempotent_and_use_after_close_fails_closed(self):
        before = _fd_count()
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
        self.assertEqual(_fd_count(), before)

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
                    before = _fd_count()
                    with self.assertRaisesRegex(ValueError, "unsafe relative path"):
                        secure_fs.read_regular_snapshot(root, relative, "test")
                    with self.assertRaisesRegex(ValueError, "unsafe relative path"):
                        with secure_fs.open_parent(root, relative, create=False):
                            pass
                    self.assertEqual(_fd_count(), before)

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
                        before = _fd_count()
                        with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                            secure_fs.read_regular_snapshot(root, Path(name), "test")
                        self.assertEqual(_fd_count(), before)
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
            before = _fd_count()
            with self.assertRaisesRegex(ValueError, "file exceeds maximum size"):
                secure_fs.read_regular_snapshot(root, Path("data.bin"), "test", max_bytes=3)
            self.assertEqual(_fd_count(), before)

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
            before = _fd_count()
            with patch.object(secure_fs.os, "read", side_effect=replace_root):
                with self.assertRaisesRegex(ValueError, "unsafe trusted root binding"):
                    secure_fs.read_regular_snapshot(root, Path("data.bin"), "test")
            self.assertEqual(_fd_count(), before)

    def test_read_rejects_ancestor_replacement_before_leaf_read(self):
        nested = self.root_path / "nested"
        original_nested = self.root_path / "original-nested"
        nested.mkdir()
        (nested / "data.bin").write_bytes(b"trusted")
        original_open = os.open
        swapped = False

        def replace_before_leaf_open(
            path: str | bytes,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal swapped
            if path == "data.bin" and dir_fd is not None and not swapped:
                swapped = True
                nested.rename(original_nested)
                nested.mkdir()
                (nested / "data.bin").write_bytes(b"attacker")
            return original_open(path, flags, mode, dir_fd=dir_fd)

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs.os, "open", side_effect=replace_before_leaf_open):
                with self.assertRaisesRegex(ValueError, "unsafe parent binding"):
                    secure_fs.read_regular_snapshot(root, Path("nested/data.bin"), "test")
            self.assertEqual(_fd_count(), before)

    def test_rebind_requires_exact_contents_and_identity(self):
        target = self.root_path / "data.bin"
        target.write_bytes(b"one")
        with secure_fs.open_trusted_root(self.root_path) as root:
            expected = secure_fs.read_regular_snapshot(root, Path("data.bin"), "test")
            secure_fs.rebind_regular_snapshot(root, expected, "test")
            before = target.stat()
            target.write_bytes(b"two")
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
            before_fds = _fd_count()
            with self.assertRaisesRegex(ValueError, "regular file snapshot mismatch"):
                secure_fs.rebind_regular_snapshot(root, expected, "test")
            self.assertEqual(_fd_count(), before_fds)

    def test_write_is_exclusive_and_handles_short_writes(self):
        original_write = os.write

        def short_write(fd: int, data: bytes) -> int:
            return original_write(fd, data[:2])

        with secure_fs.open_trusted_root(self.root_path) as root:
            with patch.object(secure_fs.os, "write", side_effect=short_write):
                snapshot = secure_fs.write_exclusive(root, Path("nested/data.bin"), b"abcdef", "test")
            self.assertEqual(snapshot.contents, b"abcdef")
            before = _fd_count()
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                secure_fs.write_exclusive(root, Path("nested/data.bin"), b"changed", "test")
            self.assertEqual(_fd_count(), before)
        self.assertEqual((self.root_path / "nested/data.bin").read_bytes(), b"abcdef")

    def test_write_retries_one_ctime_only_drift_and_adopts_stable_identity(self):
        target = self.root_path / "data.bin"
        original_read = os.read
        reads = 0

        def change_ctime_once(descriptor: int, amount: int) -> bytes:
            nonlocal reads
            chunk = original_read(descriptor, amount)
            if chunk:
                reads += 1
                if reads == 1:
                    os.chmod(target, 0o600)
            return chunk

        with secure_fs.open_trusted_root(self.root_path) as root, patch.object(
            secure_fs.os, "read", side_effect=change_ctime_once
        ):
            snapshot = secure_fs.write_exclusive(
                root, Path("data.bin"), b"owned", "test"
            )

        self.assertEqual(reads, 3)
        self.assertEqual(snapshot.identity[-1], target.stat().st_ctime_ns)
        self.assertEqual(target.read_bytes(), b"owned")

    def test_write_rejects_second_ctime_drift_during_bounded_retry(self):
        target = self.root_path / "data.bin"
        original_read = os.read
        reads = 0

        def change_ctime_twice(descriptor: int, amount: int) -> bytes:
            nonlocal reads
            chunk = original_read(descriptor, amount)
            if chunk:
                reads += 1
                os.chmod(target, 0o600)
            return chunk

        with secure_fs.open_trusted_root(self.root_path) as root, patch.object(
            secure_fs.os, "read", side_effect=change_ctime_twice
        ):
            with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                secure_fs.write_exclusive(
                    root, Path("data.bin"), b"owned", "test"
                )

        self.assertEqual(reads, 2)
        self.assertEqual(target.read_bytes(), b"owned")

    def test_write_rejects_three_ctimes_observed_in_one_validation(self):
        target = self.root_path / "data.bin"
        original_read = os.read
        original_stat = os.stat
        nonempty_reads = 0
        leaf_stats = 0

        def change_ctime_after_read(descriptor: int, amount: int) -> bytes:
            nonlocal nonempty_reads
            chunk = original_read(descriptor, amount)
            if chunk:
                nonempty_reads += 1
                if nonempty_reads == 1:
                    os.chmod(target, 0o600)
            return chunk

        def change_ctime_before_named_after(path, *args, **kwargs):
            nonlocal leaf_stats
            if path == "data.bin" and kwargs.get("dir_fd") is not None:
                leaf_stats += 1
                if leaf_stats == 2:
                    os.chmod(target, 0o600)
            return original_stat(path, *args, **kwargs)

        with (
            secure_fs.open_trusted_root(self.root_path) as root,
            patch.object(secure_fs.os, "read", side_effect=change_ctime_after_read),
            patch.object(
                secure_fs.os, "stat", side_effect=change_ctime_before_named_after
            ),
        ):
            with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                secure_fs.write_exclusive(
                    root, Path("data.bin"), b"owned", "test"
                )

        self.assertEqual(nonempty_reads, 1)
        self.assertEqual(leaf_stats, 2)
        self.assertEqual(target.read_bytes(), b"owned")

    def test_write_rejects_ctime_drift_in_each_validation_pass(self):
        target = self.root_path / "data.bin"
        original_read = os.read
        nonempty_reads = 0

        def change_ctime_in_each_pass(descriptor: int, amount: int) -> bytes:
            nonlocal nonempty_reads
            chunk = original_read(descriptor, amount)
            if chunk:
                nonempty_reads += 1
                if nonempty_reads in {1, 3}:
                    os.chmod(target, 0o600)
            return chunk

        with secure_fs.open_trusted_root(self.root_path) as root, patch.object(
            secure_fs.os, "read", side_effect=change_ctime_in_each_pass
        ):
            with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                secure_fs.write_exclusive(
                    root, Path("data.bin"), b"owned", "test"
                )

        self.assertEqual(nonempty_reads, 3)
        self.assertEqual(target.read_bytes(), b"owned")

    def test_rebind_stabilizes_one_same_mode_ctime_change_during_full_read(self):
        target = self.root_path / "data.bin"
        original_read = os.read
        nonempty_reads = 0

        def change_ctime_on_rebind(descriptor: int, amount: int) -> bytes:
            nonlocal nonempty_reads
            chunk = original_read(descriptor, amount)
            if chunk:
                nonempty_reads += 1
                if nonempty_reads == 3:
                    os.chmod(target, 0o600)
            return chunk

        with secure_fs.open_trusted_root(self.root_path) as root, patch.object(
            secure_fs.os, "read", side_effect=change_ctime_on_rebind
        ):
            snapshot = secure_fs.write_exclusive(
                root, Path("data.bin"), b"owned", "test"
            )
            secure_fs.rebind_regular_snapshot(root, snapshot, "test")

        self.assertGreaterEqual(nonempty_reads, 4)
        self.assertEqual(target.read_bytes(), b"owned")

    def test_rebind_rejects_preexisting_and_in_read_ctime_drifts(self):
        target = self.root_path / "data.bin"
        target.write_bytes(b"owned")
        target.chmod(0o600)
        original_read = os.read
        nonempty_reads = 0

        def change_ctime_during_rebind(descriptor: int, amount: int) -> bytes:
            nonlocal nonempty_reads
            chunk = original_read(descriptor, amount)
            if chunk:
                nonempty_reads += 1
                if nonempty_reads == 1:
                    os.chmod(target, 0o600)
            return chunk

        with secure_fs.open_trusted_root(self.root_path) as root:
            expected = secure_fs.read_regular_snapshot(
                root, Path("data.bin"), "test"
            )
            os.chmod(target, 0o600)
            with patch.object(
                secure_fs.os, "read", side_effect=change_ctime_during_rebind
            ):
                with self.assertRaises(ValueError):
                    secure_fs.rebind_regular_snapshot(root, expected, "test")

        self.assertGreaterEqual(nonempty_reads, 1)
        self.assertEqual(target.read_bytes(), b"owned")

    def test_write_rejects_non_bytes(self):
        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with self.assertRaisesRegex(TypeError, "contents must be bytes"):
                secure_fs.write_exclusive(root, Path("data.bin"), bytearray(b"data"), "test")
            self.assertEqual(_fd_count(), before)

    def test_write_detects_parent_swap_after_file_fsync(self):
        nested = self.root_path / "nested"
        original_nested = self.root_path / "original-nested"
        nested.mkdir()

        def replace_parent(stage: str, component: str | None = None) -> None:
            if stage == "after_file_fsync":
                nested.rename(original_nested)
                nested.mkdir()

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace_parent):
                with self.assertRaisesRegex(ValueError, "unsafe parent binding"):
                    secure_fs.write_exclusive(root, Path("nested/data.bin"), b"data", "test")
            self.assertEqual(_fd_count(), before)
        self.assertFalse((nested / "data.bin").exists())
        self.assertEqual((original_nested / "data.bin").read_bytes(), b"data")

    def test_write_rejects_same_inode_content_tampering_after_file_fsync(self):
        target = self.root_path / "data.bin"
        original_identity = None

        def tamper_same_inode(stage: str, component: str | None = None) -> None:
            nonlocal original_identity
            if stage == "after_file_fsync":
                before = target.stat()
                original_identity = (before.st_dev, before.st_ino)
                target.write_bytes(b"EVIL!")
                after = target.stat()
                self.assertEqual((after.st_dev, after.st_ino), original_identity)

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=tamper_same_inode):
                with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                    secure_fs.write_exclusive(root, Path("data.bin"), b"owned", "test")
            self.assertEqual(_fd_count(), before)
        result = target.stat()
        self.assertEqual((result.st_dev, result.st_ino), original_identity)
        self.assertEqual(target.read_bytes(), b"EVIL!")

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

    def test_attacker_replacement_before_return_revalidation_is_never_unlinked(self):
        target = self.root_path / "data.bin"
        original_unlink = os.unlink
        attacker_identity = None

        def replace_before_return(stage: str, component: str | None = None) -> None:
            nonlocal attacker_identity
            if stage == "before_return_revalidation":
                original_unlink(target)
                target.write_bytes(b"attacker-before-return")
                result = target.stat()
                attacker_identity = (result.st_dev, result.st_ino)

        with secure_fs.open_trusted_root(self.root_path) as root:
            before = _fd_count()
            with (
                patch.object(
                    secure_fs, "_secure_fs_test_hook", side_effect=replace_before_return
                ),
                patch.object(secure_fs.os, "unlink", wraps=original_unlink) as unlink,
            ):
                with self.assertRaisesRegex(ValueError, "unsafe regular file"):
                    secure_fs.write_exclusive(root, Path("data.bin"), b"owned", "test")
                unlink.assert_not_called()
            self.assertEqual(_fd_count(), before)
        result = target.stat()
        self.assertEqual((result.st_dev, result.st_ino), attacker_identity)
        self.assertEqual(target.read_bytes(), b"attacker-before-return")

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
