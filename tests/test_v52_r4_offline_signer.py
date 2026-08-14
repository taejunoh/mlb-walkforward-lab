"""Regression tests for the descriptor-bound offline R4 signer.

Every swap source and target is asserted to remain inside its temporary root.
No test reads a production custody path or an actual Keychain.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_SEED = bytearray(range(32))
_PUBLIC = Ed25519PrivateKey.from_private_bytes(_SEED).public_key().public_bytes_raw()


class OfflineSignerTests(unittest.TestCase):
    def _tree(self, root: Path, *, leaf: bool = True) -> Path:
        current = root
        for part in ("Library", "Application Support", "mlb-walkforward-lab", "keys"):
            current /= part
            current.mkdir()
            current.chmod(0o700)
        if leaf:
            target = current / "v5_2_r4_custody_anchor.ed25519.seed"
            target.write_bytes(_SEED)
            target.chmod(0o400)
        return current

    def _signer(self):
        from mlb_backtest import v52_r4_offline_signer

        return v52_r4_offline_signer

    def _migration(self):
        path = Path(__file__).resolve().parents[1] / "scripts/migrate_v52_r4_signing_key_to_offline.py"
        spec = importlib.util.spec_from_file_location("_r4_offline_migration_test", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _patch_valid(self, signer, root: Path):
        return patch.multiple(
            signer,
            _os_home=lambda: root,
            PRODUCTION_PUBLIC_KEY=_PUBLIC,
            _is_immutable=lambda _info: True,
        )

    def test_only_production_sign_api_is_public_and_has_no_secret_configuration(self) -> None:
        signer = self._signer()
        self.assertTrue(callable(signer.sign_anchor_message))
        for forbidden in ("read_offline_seed", "write_offline_seed_exclusively", "offline_seed_path"):
            self.assertFalse(hasattr(signer, forbidden))
        self.assertNotIn("HOME", signer.sign_anchor_message.__code__.co_names)

    def test_signs_valid_frozen_leaf_and_zeroizes_read_buffer(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._tree(root)
            captured: list[bytearray] = []
            original = signer._read_seed

            def reading() -> bytearray:
                value = original()
                captured.append(value)
                return value

            with self._patch_valid(signer, root), patch.object(signer, "_read_seed", side_effect=reading):
                signature = signer.sign_anchor_message(b"anchor")
            Ed25519PrivateKey.from_private_bytes(_SEED).public_key().verify(signature, b"anchor")
            self.assertEqual(captured, [bytearray(32)])

    def test_same_inode_post_read_mode_drift_is_rejected(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root)
            target = parent / signer._NAME
            original_read = signer._read_exact

            def drift(fd: int) -> bytearray:
                value = original_read(fd)
                target.chmod(0o600)
                return value

            with self._patch_valid(signer, root), patch.object(signer, "_read_exact", side_effect=drift):
                with self.assertRaisesRegex(RuntimeError, "REBIND"):
                    signer.sign_anchor_message(b"anchor")

    @unittest.skipUnless(hasattr(os, "chflags") and hasattr(stat, "UF_NODUMP"), "Darwin secondary flags required")
    def test_same_inode_post_read_secondary_flag_drift_is_rejected(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root)
            target = parent / signer._NAME
            original_read = signer._read_exact

            def drift(fd: int) -> bytearray:
                value = original_read(fd)
                os.chflags(target, os.stat(target).st_flags | stat.UF_NODUMP)
                return value

            with self._patch_valid(signer, root), patch.object(signer, "_read_exact", side_effect=drift):
                with self.assertRaisesRegex(RuntimeError, "REBIND"):
                    signer.sign_anchor_message(b"anchor")

    def test_missing_symlink_hardlink_owner_mode_size_and_flag_are_rejected(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root)
            target = parent / signer._NAME
            with self._patch_valid(signer, root):
                target.unlink()
                with self.assertRaises(FileNotFoundError):
                    signer.sign_anchor_message(b"x")
                outside = root / "outside"
                outside.write_bytes(_SEED)
                outside.chmod(0o400)
                target.symlink_to(outside)
                with self.assertRaises(OSError):
                    signer.sign_anchor_message(b"x")
                target.unlink()
                os.link(outside, target)
                with self.assertRaisesRegex(RuntimeError, "INVALID"):
                    signer.sign_anchor_message(b"x")
                target.unlink()
                target.write_bytes(b"short")
                target.chmod(0o400)
                with self.assertRaisesRegex(RuntimeError, "INVALID"):
                    signer.sign_anchor_message(b"x")
                target.chmod(0o600)
                target.write_bytes(_SEED)
                with self.assertRaisesRegex(RuntimeError, "INVALID"):
                    signer.sign_anchor_message(b"x")
            with patch.object(signer, "_os_home", return_value=root), patch.object(signer, "_is_immutable", return_value=False):
                target.chmod(0o400)
                with self.assertRaisesRegex(RuntimeError, "INVALID"):
                    signer.sign_anchor_message(b"x")

    def test_parent_and_leaf_swaps_after_open_are_rejected(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root)
            target = parent / signer._NAME
            replacement = parent / "replacement"
            replacement.write_bytes(_SEED)
            replacement.chmod(0o400)

            def swap_leaf() -> None:
                self.assertTrue(target.is_relative_to(root))
                self.assertTrue(replacement.is_relative_to(root))
                os.replace(replacement, target)

            with self._patch_valid(signer, root), patch.object(signer, "_after_open", side_effect=swap_leaf):
                with self.assertRaisesRegex(RuntimeError, "REBIND"):
                    signer.sign_anchor_message(b"x")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root)
            replacement_parent = parent.with_name("keys-replacement")
            parked_parent = parent.with_name("keys-parked")
            replacement_parent.mkdir()
            replacement_parent.chmod(0o700)
            replacement = replacement_parent / signer._NAME
            replacement.write_bytes(_SEED)
            replacement.chmod(0o400)

            def swap_parent() -> None:
                self.assertTrue(parent.is_relative_to(root))
                self.assertTrue(replacement_parent.is_relative_to(root))
                self.assertTrue(parked_parent.is_relative_to(root))
                os.replace(parent, parked_parent)
                os.replace(replacement_parent, parent)

            with self._patch_valid(signer, root), patch.object(signer, "_after_open", side_effect=swap_parent):
                with self.assertRaisesRegex(RuntimeError, "REBIND"):
                    signer.sign_anchor_message(b"x")

    def test_create_is_exclusive_reopens_freezes_and_resumes_valid_seed(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._tree(root, leaf=False)
            calls: list[str] = []
            target = root / "Library/Application Support/mlb-walkforward-lab/keys" / signer._NAME
            original_freeze = signer._fchflags

            def freeze(fd: int, flags: int) -> None:
                calls.append("freeze")
                original_freeze(fd, flags)

            try:
                with patch.multiple(signer, _os_home=lambda: root, PRODUCTION_PUBLIC_KEY=_PUBLIC), patch.object(signer, "_fchflags", side_effect=freeze):
                    signer._create_seed_exclusively(bytearray(_SEED))
                    signer._verify_existing_seed()
                    with self.assertRaises(FileExistsError):
                        signer._create_seed_exclusively(bytearray(_SEED))
            finally:
                if target.exists():
                    os.chflags(target, os.stat(target).st_flags & ~stat.UF_IMMUTABLE)
            self.assertEqual(calls, ["freeze"])

    def test_production_shaped_home_permits_0755_but_rejects_wrong_or_writable_home(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            home.chmod(0o755)
            parent = self._tree(home, leaf=False)
            target = parent / signer._NAME
            try:
                with patch.multiple(signer, _os_home=lambda: home, PRODUCTION_PUBLIC_KEY=_PUBLIC):
                    signer._create_seed_exclusively(bytearray(_SEED))
                    signer._verify_existing_seed()
            finally:
                if target.exists():
                    os.chflags(target, os.stat(target).st_flags & ~stat.UF_IMMUTABLE)

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary).resolve()
            parent = self._tree(home)
            home.chmod(0o775)
            with patch.object(signer, "_os_home", return_value=home), patch.object(signer, "_is_immutable", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "PATH_UNAVAILABLE"):
                    signer.sign_anchor_message(b"home-writable")
            home.chmod(0o755)
            with patch.object(signer, "_os_home", return_value=home), patch.object(signer, "_UID", return_value=os.getuid() + 1), patch.object(signer, "_is_immutable", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "PATH_UNAVAILABLE"):
                    signer.sign_anchor_message(b"wrong-owner")

    def test_creation_failures_leave_artifact_and_never_unlink_replacement(self) -> None:
        signer = self._signer()
        failures = (
            ("_write_all", OSError("partial-write")),
            ("_fchflags", OSError("fchflags")),
            ("fchmod", OSError("fchmod")),
            ("fsync", OSError("fsync")),
            ("_after_open", OSError("reopen")),
        )
        for seam, error in failures:
            with self.subTest(seam=seam), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                parent = self._tree(root, leaf=False)
                patcher = (
                    patch.object(signer, seam, side_effect=error)
                    if seam.startswith("_")
                    else patch.object(signer.os, seam, side_effect=error)
                )
                with patch.multiple(signer, _os_home=lambda: root, PRODUCTION_PUBLIC_KEY=_PUBLIC, _is_immutable=lambda _info: False), patcher:
                    with self.assertRaises(OSError):
                        signer._create_seed_exclusively(bytearray(_SEED))
                self.assertTrue((parent / signer._NAME).exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root, leaf=False)
            target = parent / signer._NAME
            replacement = parent / "replacement"
            replacement.write_bytes(b"R" * 32)
            replacement.chmod(0o400)

            def swap_at_former_cleanup_boundary() -> None:
                self.assertTrue(target.is_relative_to(root))
                self.assertTrue(replacement.is_relative_to(root))
                os.replace(replacement, target)

            with patch.multiple(signer, _os_home=lambda: root, PRODUCTION_PUBLIC_KEY=_PUBLIC, _is_immutable=lambda _info: False), patch.object(signer, "_after_open", side_effect=swap_at_former_cleanup_boundary):
                with self.assertRaisesRegex(RuntimeError, "REBIND"):
                    signer._create_seed_exclusively(bytearray(_SEED))
            self.assertEqual(target.read_bytes(), b"R" * 32)

    def test_final_parent_fsync_swap_must_not_return_success(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root, leaf=False)
            replacement_parent = parent.with_name("keys-replacement")
            parked_parent = parent.with_name("keys-parked")
            replacement_parent.mkdir()
            replacement_parent.chmod(0o700)
            replacement = replacement_parent / signer._NAME
            replacement.write_bytes(b"R" * 32)
            replacement.chmod(0o400)
            original_fsync = signer.os.fsync
            swapped = False

            def final_parent_swap(fd: int) -> None:
                nonlocal swapped
                original_fsync(fd)
                if not swapped and stat.S_ISDIR(os.fstat(fd).st_mode):
                    swapped = True
                    self.assertTrue(parent.is_relative_to(root))
                    self.assertTrue(replacement_parent.is_relative_to(root))
                    self.assertTrue(parked_parent.is_relative_to(root))
                    os.replace(parent, parked_parent)
                    os.replace(replacement_parent, parent)

            try:
                with patch.multiple(signer, _os_home=lambda: root, PRODUCTION_PUBLIC_KEY=_PUBLIC), patch.object(signer.os, "fsync", side_effect=final_parent_swap):
                    with self.assertRaisesRegex(RuntimeError, "REBIND"):
                        signer._create_seed_exclusively(bytearray(_SEED))
                self.assertTrue(swapped)
                self.assertEqual((parent / signer._NAME).read_bytes(), b"R" * 32)
            finally:
                parked_target = parked_parent / signer._NAME
                if parked_target.exists():
                    os.chflags(parked_target, os.stat(parked_target).st_flags & ~stat.UF_IMMUTABLE)

    @unittest.skipUnless(hasattr(os, "chflags") and hasattr(stat, "UF_IMMUTABLE"), "Darwin immutable flags required")
    def test_darwin_fchflags_freezes_and_reopens_a_temporary_seed(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            parent = self._tree(root, leaf=False)
            target = parent / signer._NAME
            try:
                with patch.multiple(signer, _os_home=lambda: root, PRODUCTION_PUBLIC_KEY=_PUBLIC):
                    signer._create_seed_exclusively(bytearray(_SEED))
                    signer._verify_existing_seed()
                self.assertTrue(os.stat(target).st_flags & stat.UF_IMMUTABLE)
            finally:
                if target.exists():
                    os.chflags(target, os.stat(target).st_flags & ~stat.UF_IMMUTABLE)

    def test_public_key_mismatch_and_signature_shape_fail_without_secret_text(self) -> None:
        signer = self._signer()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._tree(root)
            wrong = Ed25519PrivateKey.from_private_bytes(b"x" * 32).public_key().public_bytes_raw()
            with patch.multiple(signer, _os_home=lambda: root, PRODUCTION_PUBLIC_KEY=wrong, _is_immutable=lambda _info: True):
                with self.assertRaisesRegex(RuntimeError, "PUBLIC_KEY_MISMATCH") as raised:
                    signer.sign_anchor_message(b"anchor")
            self.assertNotIn(_SEED.hex(), str(raised.exception))
            with self.assertRaises(TypeError):
                signer.sign_anchor_message("not-bytes")  # type: ignore[arg-type]

    def test_migration_reuses_valid_offline_state_without_reading_legacy(self) -> None:
        migration = self._migration()
        with patch.object(migration, "_read_legacy_exact", side_effect=AssertionError("legacy must not be read")), patch.object(
            migration._offline, "_verify_existing_seed", return_value=None
        ) as verify:
            migration.migrate()
        verify.assert_called_once_with()

    def test_migration_rejects_invalid_existing_target_without_reading_legacy(self) -> None:
        migration = self._migration()
        with patch.object(migration, "_read_legacy_exact", side_effect=AssertionError("legacy must not be read")), patch.object(
            migration._offline, "_verify_existing_seed", side_effect=RuntimeError("R4_OFFLINE_SEED_INVALID")
        ):
            with self.assertRaisesRegex(RuntimeError, "INVALID"):
                migration.migrate()

    def test_migration_keeps_legacy_when_offline_copy_or_verify_fails(self) -> None:
        migration = self._migration()

        class Legacy:
            released = False

            def copy_seed(self):
                return bytearray(_SEED)

            def release(self):
                self.released = True

        legacy = Legacy()
        with patch.object(migration, "_read_legacy_exact", return_value=legacy), patch.object(migration, "PRODUCTION_PUBLIC_KEY", _PUBLIC), patch.object(
            migration._offline, "_verify_existing_seed", side_effect=FileNotFoundError
        ), patch.object(migration._offline, "_create_seed_exclusively", side_effect=OSError("write")):
            with self.assertRaises(OSError):
                migration.migrate()
        self.assertTrue(legacy.released)
        self.assertFalse(hasattr(legacy, "delete_exact"))
