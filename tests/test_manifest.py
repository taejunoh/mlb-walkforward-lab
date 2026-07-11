import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.manifest import RawStore


class ManifestTest(unittest.TestCase):
    def test_repeated_put_retains_original_timestamp_and_manifest_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            first = store.put("mlb_schedule", {"date": "2025-05-01"}, b'{"games":[]}')
            original_manifest = first.manifest_path.read_bytes()
            second = store.put("mlb_schedule", {"date": "2025-05-01"}, b'{"games":[]}')
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.data_path.read_bytes(), b'{"games":[]}')
            self.assertEqual(first.retrieved_at_utc, second.retrieved_at_utc)
            self.assertEqual(original_manifest, second.manifest_path.read_bytes())
            self.assertEqual(first.row_count, 1)

    def test_empty_payload_has_zero_rows(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = RawStore(Path(td)).put("source", {}, b"")
            self.assertEqual(artifact.row_count, 0)

    def test_row_count_counts_only_nonempty_lines(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = RawStore(Path(td)).put(
                "source", {}, b'first\n\n   \nsecond\n'
            )
            self.assertEqual(artifact.row_count, 2)

    def test_canonical_query_is_json_compatible_and_order_independent(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            first = store.put(
                "source", {"z": ({"b": 2, "a": 1},), "a": True}, b"row"
            )
            original_manifest = first.manifest_path.read_bytes()
            second = store.put(
                "source", {"a": True, "z": [{"a": 1, "b": 2}]}, b"row"
            )
            manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["query"], {"a": True, "z": [{"a": 1, "b": 2}]})
            self.assertEqual(original_manifest, second.manifest_path.read_bytes())

    def test_tampered_raw_artifact_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            artifact = store.put("source", {}, b"original")
            artifact.data_path.write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "immutable raw artifact"):
                store.put("source", {}, b"original")

    def test_tampered_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            artifact = store.put("source", {}, b"row")
            artifact.manifest_path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "immutable raw manifest"):
                store.put("source", {}, b"row")

    def test_invalid_sources_are_rejected(self):
        invalid_sources = ("", "../escape", "/absolute", "nested/source", "a/../b")
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            for source in invalid_sources:
                with self.subTest(source=source):
                    with self.assertRaises(ValueError):
                        store.put(source, {}, b"row")

    def test_non_dict_query_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                RawStore(Path(td)).put("source", [], b"row")

    def test_non_bytes_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                RawStore(Path(td)).put("source", {}, "row")

    def test_existing_manifest_metadata_mismatches_are_rejected(self):
        mutations = {
            "source": lambda manifest: manifest.__setitem__("source", "other"),
            "query": lambda manifest: manifest.__setitem__("query", {"other": True}),
            "sha256": lambda manifest: manifest.__setitem__("sha256", "0" * 64),
            "schema_version": lambda manifest: manifest.__setitem__("schema_version", 2),
            "row_count": lambda manifest: manifest.__setitem__("row_count", 99),
        }
        for field, mutate in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                store = RawStore(Path(td))
                artifact = store.put("source", {"date": "2025-05-01"}, b"row")
                manifest = json.loads(
                    artifact.manifest_path.read_text(encoding="utf-8")
                )
                mutate(manifest)
                artifact.manifest_path.write_text(
                    json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
                )
                with self.assertRaisesRegex(RuntimeError, "immutable raw manifest"):
                    store.put("source", {"date": "2025-05-01"}, b"row")
