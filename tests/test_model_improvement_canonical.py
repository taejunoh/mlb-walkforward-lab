import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.model_improvement.canonical import (
    canonical_json_bytes,
    read_regular_bytes,
    sha256_bytes,
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
