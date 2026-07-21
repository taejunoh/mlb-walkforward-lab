import os
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

        def replace(stage: str, component: str | None = None) -> None:
            if stage == "after_component_open" and component == "parent":
                parent.rename(self.path / "original-parent")
                parent.symlink_to(canary, target_is_directory=True)

        with patch.object(secure_fs, "_secure_fs_test_hook", side_effect=replace):
            with self.assertRaisesRegex(ValueError, "unsafe"):
                secure_fs.open_trusted_root(root)

    def test_expected_binding_rejects_replacement_between_operations(self):
        root = self.path / "root"
        root.mkdir()
        with secure_fs.open_trusted_root(root) as trusted:
            binding = trusted.binding
        root.rename(self.path / "original")
        root.mkdir()
        with self.assertRaisesRegex(ValueError, "differs from expected"):
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
        with self.assertRaisesRegex(ValueError, "differs from expected"):
            secure_fs.open_trusted_root(root, expected=expected)
        self.assertEqual(_fd_count(), before_failure)


if __name__ == "__main__":
    unittest.main()
