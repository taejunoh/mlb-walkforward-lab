import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mlb_backtest.model_improvement import canonical as canonical_module
from mlb_backtest.model_improvement.canonical import (
    canonical_json_bytes,
    read_regular_bytes,
    sha256_bytes,
    write_immutable_bytes,
    write_immutable_json,
)


class CanonicalArtifactTests(unittest.TestCase):
    def test_canonical_json_is_compact_and_sorted_utf8(self):
        value = {"z": 1, "a": "café", "nested": [True, None]}

        self.assertEqual(
            canonical_json_bytes(value),
            b'{"a":"caf\\u00e9","nested":[true,null],"z":1}\n',
        )

    def test_canonical_json_has_exactly_one_trailing_newline(self):
        encoded = canonical_json_bytes({"line": "value"})

        self.assertTrue(encoded.endswith(b"\n"))
        self.assertFalse(encoded.endswith(b"\n\n"))
        self.assertEqual(encoded.count(b"\n"), 1)

    def test_sha256_bytes_returns_hex_digest(self):
        payload = b"artifact"

        self.assertEqual(sha256_bytes(payload), hashlib.sha256(payload).hexdigest())

    def test_write_immutable_json_exclusively_creates_then_identical_retry(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "artifact.json"

            self.assertTrue(write_immutable_json(path, {"version": 1}))
            self.assertFalse(write_immutable_json(path, {"version": 1}))
            self.assertEqual(path.read_bytes(), b'{"version":1}\n')

    def test_write_immutable_bytes_preserves_exact_jsonl_bytes(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "rows.jsonl"
            contents = b'{"game_pk":2}\n{"game_pk":7}\n'

            self.assertTrue(write_immutable_bytes(path, contents))
            self.assertFalse(write_immutable_bytes(path, contents))
            self.assertEqual(read_regular_bytes(path, "rows"), contents)
            with self.assertRaisesRegex(ValueError, "immutable artifact differs"):
                write_immutable_bytes(path, b'{"game_pk":7}\n')

    def test_write_immutable_bytes_rejects_symlink_and_nonregular_paths(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"target")
            symlink = root / "rows.jsonl"
            symlink.symlink_to(target)
            directory_path = root / "directory"
            directory_path.mkdir()

            with self.assertRaises(ValueError):
                write_immutable_bytes(symlink, b"rows\n")
            with self.assertRaises(ValueError):
                write_immutable_bytes(directory_path, b"rows\n")

    def test_concurrent_identical_writers_publish_one_complete_artifact(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "artifact.json"
            value = {"payload": "x" * 256}
            expected = canonical_json_bytes(value)
            first_blocked = threading.Event()
            release_first = threading.Event()
            first_ident = [None]
            first_write_blocked = [False]
            outcomes = []
            original_write = canonical_module.os.write

            def synchronized_write(descriptor, data):
                if (
                    threading.get_ident() == first_ident[0]
                    and not first_write_blocked[0]
                ):
                    first_write_blocked[0] = True
                    first_blocked.set()
                    self.assertTrue(release_first.wait(timeout=10))
                return original_write(descriptor, data)

            def first_writer():
                first_ident[0] = threading.get_ident()
                try:
                    outcomes.append(write_immutable_json(path, value))
                except BaseException as exc:
                    outcomes.append(exc)

            def second_writer():
                try:
                    outcomes.append(write_immutable_json(path, value))
                except BaseException as exc:
                    outcomes.append(exc)

            canonical_module.os.write = synchronized_write
            first = threading.Thread(target=first_writer)
            second = threading.Thread(target=second_writer)
            try:
                first.start()
                self.assertTrue(first_blocked.wait(timeout=10))
                self.assertFalse(path.exists())
                self.assertTrue(
                    tuple(Path(directory).glob(".artifact.json.tmp-*"))
                )
                second.start()
                second.join(timeout=10)
                self.assertFalse(second.is_alive())
            finally:
                release_first.set()
                first.join(timeout=10)
                canonical_module.os.write = original_write
            self.assertFalse(first.is_alive())
            self.assertEqual(len(outcomes), 2)
            self.assertFalse(
                any(isinstance(outcome, BaseException) for outcome in outcomes)
            )
            self.assertEqual(sorted(outcomes), [False, True])
            self.assertEqual(path.read_bytes(), expected)

    def test_post_reread_rejects_same_length_rewrite_with_restored_mtime(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "artifact.json"
            value = {"payload": "x" * 256}
            original_read = canonical_module.read_regular_bytes
            replacement = b"!" * len(canonical_json_bytes(value))
            tampered = threading.Event()

            def reread_then_tamper(target, label):
                contents = original_read(target, label)
                if target == path and not tampered.is_set():
                    before = path.stat()
                    path.write_bytes(replacement)
                    os.utime(
                        path,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    tampered.set()
                return contents

            canonical_module.read_regular_bytes = reread_then_tamper
            try:
                with self.assertRaises(ValueError):
                    write_immutable_json(path, value)
            finally:
                canonical_module.read_regular_bytes = original_read
            self.assertTrue(tampered.is_set())

    def test_staging_failure_cleans_private_file_without_final_poisoning(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            parent = Path(directory)
            path = parent / "artifact.json"
            value = {"payload": "x" * 4_000_000}
            removed = threading.Event()

            def remove_staging_file():
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    candidates = tuple(parent.glob(".artifact.json.tmp-*"))
                    if candidates:
                        candidates[0].unlink()
                        removed.set()
                        return
                    time.sleep(0.0001)

            remover = threading.Thread(target=remove_staging_file)
            remover.start()
            try:
                with self.assertRaises(ValueError):
                    write_immutable_json(path, value)
            finally:
                remover.join(timeout=30)
            self.assertTrue(removed.is_set())
            self.assertFalse(path.exists())
            self.assertEqual(tuple(parent.glob(".artifact.json.tmp-*")), ())

    def test_write_immutable_json_rejects_changed_retry(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "artifact.json"
            write_immutable_json(path, {"version": 1})

            with self.assertRaisesRegex(ValueError, "immutable artifact differs"):
                write_immutable_json(path, {"version": 2})

    def test_symlink_is_denied_for_read_and_write(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            directory_path = Path(directory)
            target = directory_path / "target.json"
            target.write_bytes(b"target")
            link = directory_path / "link.json"
            link.symlink_to(target)

            with self.assertRaises(ValueError):
                read_regular_bytes(link, "linked artifact")
            with self.assertRaises(ValueError):
                write_immutable_json(link, {"version": 1})

    def test_identical_regular_hardlink_is_admitted(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            directory_path = Path(directory)
            target = directory_path / "target.json"
            target.write_bytes(canonical_json_bytes({"version": 1}))
            link = directory_path / "hardlink.json"
            os.link(target, link)

            self.assertEqual(
                read_regular_bytes(link, "hard-linked artifact"),
                canonical_json_bytes({"version": 1}),
            )
            self.assertFalse(write_immutable_json(link, {"version": 1}))

    def test_non_regular_path_is_denied(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "directory"
            path.mkdir()

            with self.assertRaises(ValueError):
                read_regular_bytes(path, "directory artifact")
            with self.assertRaises(ValueError):
                write_immutable_json(path, {"version": 1})

    def test_reread_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "artifact.json"
            value = {"payload": "x" * 2_000}
            tampering_path = _TamperingPath(
                path, b"!" * len(canonical_json_bytes(value))
            )
            try:
                with self.assertRaises(ValueError):
                    write_immutable_json(tampering_path, value)
            finally:
                path.unlink(missing_ok=True)


class _TamperingPath:
    """PathLike that changes a real file exactly before its second open."""

    def __init__(self, path: Path, replacement: bytes) -> None:
        self.path = path
        self.replacement = replacement
        self.calls = 0

    def __fspath__(self) -> str:
        self.calls += 1
        if self.calls == 2:
            self.path.write_bytes(self.replacement)
        return os.fspath(self.path)

    def __str__(self) -> str:
        return str(self.path)


if __name__ == "__main__":
    unittest.main()
