import dataclasses
import hashlib
import json
import multiprocessing
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import mlb_backtest.manifest as manifest_module

from mlb_backtest.manifest import RawStore
from mlb_backtest.models import GameClock, RawArtifact


def _concurrent_put(root, start_event, ready_queue, result_queue):
    ready_queue.put(True)
    if not start_event.wait(timeout=10):
        result_queue.put(("error", "start timeout"))
        return
    try:
        artifact = RawStore(Path(root)).put("source", {"query": "same"}, b"row")
        result_queue.put(
            (
                "ok",
                artifact.retrieved_at_utc,
                artifact.manifest_path.read_bytes(),
            )
        )
    except Exception as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


def _canonical_manifest_bytes(manifest):
    return json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8")


def _canonical_query_sha(query):
    content = json.dumps(
        query,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _rewrite_manifest_and_seal(artifact, manifest):
    content = _canonical_manifest_bytes(manifest)
    artifact.manifest_path.write_bytes(content)
    seal_path = artifact.manifest_path.with_suffix(".sha256")
    seal_path.write_text(
        f"{hashlib.sha256(content).hexdigest()}\n", encoding="utf-8"
    )


class ManifestTest(unittest.TestCase):
    def test_unique_query_reuses_identical_and_rejects_different_payload(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            first = store.put_unique_query("source", {"month": 1}, b"one\n")
            repeated = store.put_unique_query("source", {"month": 1}, b"one\n")
            self.assertEqual(first, repeated)
            with self.assertRaisesRegex(RuntimeError, "ambiguous raw payload"):
                store.put_unique_query("source", {"month": 1}, b"two\n")
            self.assertEqual(
                len(list((Path(td) / "source").glob("*.manifest.json"))), 1
            )

    def test_unique_query_does_not_reread_unrelated_payloads_quadratically(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            nonempty_raw_reads = []
            original = manifest_module._safe_leaf_bytes

            def tracking(*args, **kwargs):
                result = original(*args, **kwargs)
                if len(args) >= 3 and args[2] == "raw" and result is not None:
                    nonempty_raw_reads.append(args[0])
                return result

            with mock.patch(
                "mlb_backtest.manifest._safe_leaf_bytes", side_effect=tracking
            ):
                for chunk in range(29):
                    store.put_unique_query(
                        "source", {"chunk": chunk}, f"payload-{chunk}\n".encode()
                    )
            self.assertEqual(nonempty_raw_reads, [])

    def test_pipeline_records_are_frozen_with_exact_fields(self):
        expected_fields = {
            RawArtifact: (
                "source",
                "sha256",
                "data_path",
                "manifest_path",
                "retrieved_at_utc",
                "row_count",
            ),
            GameClock: (
                "game_pk",
                "official_date",
                "scheduled_start_utc",
                "lock_at_utc",
                "feature_as_of",
            ),
        }
        expected_annotations = {
            RawArtifact: {
                "source": str,
                "sha256": str,
                "data_path": Path,
                "manifest_path": Path,
                "retrieved_at_utc": str,
                "row_count": int,
            },
            GameClock: {
                "game_pk": int,
                "official_date": date,
                "scheduled_start_utc": datetime,
                "lock_at_utc": datetime,
                "feature_as_of": date,
            },
        }
        for record_type, names in expected_fields.items():
            with self.subTest(record_type=record_type.__name__):
                self.assertEqual(
                    tuple(field.name for field in dataclasses.fields(record_type)), names
                )
                self.assertEqual(
                    record_type.__annotations__, expected_annotations[record_type]
                )

        artifact = RawArtifact(
            "source", "digest", Path("data"), Path("manifest"), "timestamp", 1
        )
        clock = GameClock(
            1,
            date(2025, 5, 1),
            datetime(2025, 5, 1, tzinfo=timezone.utc),
            datetime(2025, 5, 1, tzinfo=timezone.utc),
            date(2025, 4, 30),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            artifact.row_count = 2
        with self.assertRaises(dataclasses.FrozenInstanceError):
            clock.game_pk = 2

    def test_digest_path_layout_includes_integrity_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "raw"
            payload = b"row"
            artifact = RawStore(root).put("source", {}, payload)
            folder = root / "source"
            self.assertEqual(
                artifact.sha256, hashlib.sha256(payload).hexdigest()
            )
            self.assertEqual(artifact.data_path, folder / f"{artifact.sha256}.raw")
            query_digest = _canonical_query_sha({})
            self.assertEqual(
                artifact.manifest_path,
                folder / f"{artifact.sha256}.{query_digest}.manifest.json",
            )
            self.assertTrue(artifact.manifest_path.with_suffix(".sha256").is_file())

    def test_manifest_is_canonical_utf8_with_utc_timestamp(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = RawStore(Path(td)).put("source", {"city": "Montréal"}, b"row")
            content = artifact.manifest_path.read_bytes()
            manifest = json.loads(content.decode("utf-8"))
            self.assertEqual(content, _canonical_manifest_bytes(manifest))
            self.assertIn("Montréal".encode("utf-8"), content)
            retrieved_at = datetime.fromisoformat(manifest["retrieved_at_utc"])
            self.assertEqual(retrieved_at.utcoffset().total_seconds(), 0)

    def test_concurrent_writers_return_original_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            ready_queue = context.Queue()
            result_queue = context.Queue()
            processes = [
                context.Process(
                    target=_concurrent_put,
                    args=(td, start_event, ready_queue, result_queue),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready_queue.get(timeout=10))
            start_event.set()
            results = [result_queue.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            self.assertEqual([result[0] for result in results], ["ok", "ok"])
            self.assertEqual(results[0][1:], results[1][1:])

    def test_canonical_timestamp_tampering_breaks_integrity_seal(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            artifact = store.put("source", {}, b"row")
            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            manifest["retrieved_at_utc"] = "2025-01-01T00:00:00+00:00"
            artifact.manifest_path.write_bytes(_canonical_manifest_bytes(manifest))
            with self.assertRaisesRegex(RuntimeError, "manifest integrity"):
                store.put("source", {}, b"row")

    def test_existing_timestamp_must_have_zero_utc_offset(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            artifact = store.put("source", {}, b"row")
            manifest = json.loads(artifact.manifest_path.read_text(encoding="utf-8"))
            manifest["retrieved_at_utc"] = "2025-01-01T01:00:00+01:00"
            _rewrite_manifest_and_seal(artifact, manifest)
            with self.assertRaisesRegex(RuntimeError, "timestamp"):
                store.put("source", {}, b"row")

    def test_bool_manifest_integer_fields_are_rejected(self):
        for field in ("row_count", "schema_version"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as td:
                store = RawStore(Path(td))
                artifact = store.put("source", {}, b"row")
                manifest = json.loads(
                    artifact.manifest_path.read_text(encoding="utf-8")
                )
                manifest[field] = True
                _rewrite_manifest_and_seal(artifact, manifest)
                with self.assertRaisesRegex(RuntimeError, field):
                    store.put("source", {}, b"row")

    def test_existing_root_and_source_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            outside = base / "outside"
            outside.mkdir()

            root_link = base / "root-link"
            root_link.symlink_to(outside, target_is_directory=True)
            with self.subTest(kind="root"):
                with self.assertRaisesRegex(ValueError, "root"):
                    RawStore(root_link).put("source", {}, b"row")

            root = base / "root"
            root.mkdir()
            source_link = root / "source"
            source_link.symlink_to(outside, target_is_directory=True)
            with self.subTest(kind="source"):
                with self.assertRaisesRegex(ValueError, "source"):
                    RawStore(root).put("source", {}, b"row")
            self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_and_nonregular_lock_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            outside = base / "outside.lock"
            outside.write_text("untouched", encoding="utf-8")

            symlink_root = base / "symlink-root"
            symlink_root.mkdir()
            (symlink_root / ".source.lock").symlink_to(outside)
            with self.subTest(kind="symlink"):
                with self.assertRaisesRegex(ValueError, "lock"):
                    RawStore(symlink_root).put("source", {}, b"row")
                self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")
                self.assertFalse((symlink_root / "source").exists())

            directory_root = base / "directory-root"
            directory_root.mkdir()
            (directory_root / ".source.lock").mkdir()
            with self.subTest(kind="directory"):
                with self.assertRaisesRegex(ValueError, "lock"):
                    RawStore(directory_root).put("source", {}, b"row")
                self.assertFalse((directory_root / "source").exists())

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

    def test_same_payload_with_distinct_queries_has_distinct_provenance(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            payload = b"same payload"
            first = store.put("source", {"date": "2025-05-01"}, payload)
            first_again = store.put("source", {"date": "2025-05-01"}, payload)
            second = store.put("source", {"date": "2025-05-02"}, payload)
            second_again = store.put("source", {"date": "2025-05-02"}, payload)

            self.assertEqual(first.data_path, second.data_path)
            folder = Path(td) / "source"
            first_query_digest = _canonical_query_sha({"date": "2025-05-01"})
            self.assertEqual(
                first.manifest_path,
                folder / f"{first.sha256}.{first_query_digest}.manifest.json",
            )
            self.assertEqual(first.manifest_path, first_again.manifest_path)
            self.assertEqual(first.retrieved_at_utc, first_again.retrieved_at_utc)
            query_digest = _canonical_query_sha({"date": "2025-05-02"})
            self.assertEqual(
                second.manifest_path,
                folder / f"{second.sha256}.{query_digest}.manifest.json",
            )
            self.assertEqual(second.manifest_path, second_again.manifest_path)
            self.assertEqual(second.retrieved_at_utc, second_again.retrieved_at_utc)
            self.assertEqual(len(list(folder.glob("*.raw"))), 1)
            self.assertEqual(len(list(folder.glob("*.manifest.json"))), 2)
            first_manifest = json.loads(first.manifest_path.read_text("utf-8"))
            second_manifest = json.loads(second.manifest_path.read_text("utf-8"))
            self.assertEqual(first_manifest["sha256"], first.sha256)
            self.assertEqual(second_manifest["sha256"], second.sha256)
            self.assertEqual(first_manifest["query"], {"date": "2025-05-01"})
            self.assertEqual(second_manifest["query"], {"date": "2025-05-02"})

    def test_provenance_paths_are_independent_of_query_insertion_order(self):
        payload = b"same payload"
        digest = hashlib.sha256(payload).hexdigest()
        first_query = {"date": "2025-05-01", "sport": 1}
        first_query_reordered = {"sport": 1, "date": "2025-05-01"}
        second_query = {"date": "2025-05-02", "sport": 1}
        second_query_reordered = {"sport": 1, "date": "2025-05-02"}

        with (
            tempfile.TemporaryDirectory() as first_td,
            tempfile.TemporaryDirectory() as second_td,
        ):
            first_store = RawStore(Path(first_td))
            first_a = first_store.put("source", first_query, payload)
            first_b = first_store.put("source", second_query, payload)
            first_a_repeat = first_store.put(
                "source", first_query_reordered, payload
            )
            first_b_repeat = first_store.put(
                "source", second_query_reordered, payload
            )

            second_store = RawStore(Path(second_td))
            second_b = second_store.put("source", second_query_reordered, payload)
            second_a = second_store.put("source", first_query_reordered, payload)
            second_b_repeat = second_store.put("source", second_query, payload)
            second_a_repeat = second_store.put("source", first_query, payload)

            first_name = f"{digest}.{_canonical_query_sha(first_query)}.manifest.json"
            second_name = f"{digest}.{_canonical_query_sha(second_query)}.manifest.json"
            self.assertEqual(first_a.manifest_path.name, first_name)
            self.assertEqual(second_a.manifest_path.name, first_name)
            self.assertEqual(first_b.manifest_path.name, second_name)
            self.assertEqual(second_b.manifest_path.name, second_name)
            self.assertEqual(first_a.manifest_path, first_a_repeat.manifest_path)
            self.assertEqual(first_b.manifest_path, first_b_repeat.manifest_path)
            self.assertEqual(second_a.manifest_path, second_a_repeat.manifest_path)
            self.assertEqual(second_b.manifest_path, second_b_repeat.manifest_path)
            self.assertEqual(
                first_a.retrieved_at_utc, first_a_repeat.retrieved_at_utc
            )
            self.assertEqual(
                first_b.retrieved_at_utc, first_b_repeat.retrieved_at_utc
            )
            self.assertEqual(
                second_a.retrieved_at_utc, second_a_repeat.retrieved_at_utc
            )
            self.assertEqual(
                second_b.retrieved_at_utc, second_b_repeat.retrieved_at_utc
            )

    def test_interrupted_manifest_publish_recovers_from_sealed_pending_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            with mock.patch(
                "mlb_backtest.manifest._publish_manifest",
                side_effect=OSError("injected publish failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected publish failure"):
                    store.put("source", {"date": "2025-05-01"}, b"row")

            folder = Path(td) / "source"
            self.assertEqual(list(folder.glob("*.manifest.json")), [])
            pending = list(folder.glob("*.manifest.pending"))
            seals = list(folder.glob("*.manifest.sha256"))
            self.assertEqual(len(pending), 1)
            self.assertEqual(len(seals), 1)

            recovered = store.put("source", {"date": "2025-05-01"}, b"row")
            digest = hashlib.sha256(b"row").hexdigest()
            query_digest = _canonical_query_sha({"date": "2025-05-01"})
            self.assertEqual(
                recovered.manifest_path,
                folder / f"{digest}.{query_digest}.manifest.json",
            )
            original = recovered.manifest_path.read_bytes()
            repeated = store.put("source", {"date": "2025-05-01"}, b"row")
            self.assertEqual(recovered.manifest_path, repeated.manifest_path)
            self.assertEqual(recovered.retrieved_at_utc, repeated.retrieved_at_utc)
            self.assertEqual(original, repeated.manifest_path.read_bytes())
            self.assertEqual(list(folder.glob("*.manifest.pending")), [])

    def test_unsealed_pending_manifest_is_removed_and_recreated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            query = {"date": "2025-05-01"}
            payload = b"row"
            digest = hashlib.sha256(payload).hexdigest()
            query_digest = _canonical_query_sha(query)
            folder = root / "source"
            folder.mkdir()
            pending = folder / f"{digest}.{query_digest}.manifest.pending"
            pending.write_bytes(b"incomplete")

            artifact = store.put("source", query, payload)

            self.assertTrue(artifact.manifest_path.is_file())
            self.assertFalse(pending.exists())

    def test_other_query_does_not_claim_interrupted_query_transaction(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            first_query = {"date": "2025-05-01"}
            second_query = {"date": "2025-05-02"}
            with mock.patch(
                "mlb_backtest.manifest._publish_manifest",
                side_effect=OSError("injected publish failure"),
            ):
                with self.assertRaises(OSError):
                    store.put("source", first_query, b"row")

            folder = Path(td) / "source"
            pending = next(folder.glob("*.manifest.pending"))
            interrupted_manifest = json.loads(pending.read_text("utf-8"))
            interrupted_timestamp = interrupted_manifest["retrieved_at_utc"]

            second = store.put("source", second_query, b"row")

            digest = hashlib.sha256(b"row").hexdigest()
            first_query_digest = _canonical_query_sha(first_query)
            second_query_digest = _canonical_query_sha(second_query)
            self.assertEqual(
                second.manifest_path,
                folder / f"{digest}.{second_query_digest}.manifest.json",
            )
            first = store.put("source", first_query, b"row")
            self.assertEqual(
                first.manifest_path,
                folder / f"{digest}.{first_query_digest}.manifest.json",
            )
            self.assertEqual(first.retrieved_at_utc, interrupted_timestamp)

    def test_query_mismatch_at_query_addressed_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            original_query = {"date": "2025-05-01"}
            artifact = store.put("source", original_query, b"row")
            manifest = json.loads(artifact.manifest_path.read_text("utf-8"))
            manifest["query"] = {"date": "2025-05-02"}
            _rewrite_manifest_and_seal(artifact, manifest)

            with self.assertRaisesRegex(RuntimeError, "metadata"):
                store.put("source", original_query, b"row")

    def test_sealed_pending_metadata_is_validated_before_recovery_publish(self):
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            with mock.patch(
                "mlb_backtest.manifest._publish_manifest",
                side_effect=OSError("injected publish failure"),
            ):
                with self.assertRaises(OSError):
                    store.put("source", {"date": "2025-05-01"}, b"row")

            folder = Path(td) / "source"
            pending = next(folder.glob("*.manifest.pending"))
            seal = next(folder.glob("*.manifest.sha256"))
            manifest = json.loads(pending.read_text("utf-8"))
            manifest["source"] = "other"
            content = _canonical_manifest_bytes(manifest)
            pending.write_bytes(content)
            seal.write_bytes(
                f"{hashlib.sha256(content).hexdigest()}\n".encode("utf-8")
            )

            with self.assertRaisesRegex(RuntimeError, "metadata"):
                store.put("source", {"date": "2025-05-01"}, b"row")
            self.assertEqual(list(folder.glob("*.manifest.json")), [])

    def test_symlinked_raw_manifest_and_seal_leaves_are_rejected(self):
        for leaf in ("raw", "manifest", "seal"):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "store"
                outside = Path(td) / f"outside-{leaf}"
                store = RawStore(root)
                artifact = store.put("source", {"date": "2025-05-01"}, b"row")
                paths = {
                    "raw": artifact.data_path,
                    "manifest": artifact.manifest_path,
                    "seal": artifact.manifest_path.with_suffix(".sha256"),
                }
                attacked = paths[leaf]
                expected = attacked.read_bytes()
                outside.write_bytes(expected)
                attacked.unlink()
                attacked.symlink_to(outside)

                with self.assertRaisesRegex((ValueError, RuntimeError), leaf):
                    store.put("source", {"date": "2025-05-01"}, b"row")
                self.assertEqual(outside.read_bytes(), expected)
                self.assertTrue(attacked.is_symlink())

    def test_nonregular_raw_manifest_and_seal_leaves_are_rejected(self):
        for leaf in ("raw", "manifest", "seal"):
            with self.subTest(leaf=leaf), tempfile.TemporaryDirectory() as td:
                store = RawStore(Path(td))
                artifact = store.put("source", {}, b"row")
                paths = {
                    "raw": artifact.data_path,
                    "manifest": artifact.manifest_path,
                    "seal": artifact.manifest_path.with_suffix(".sha256"),
                }
                attacked = paths[leaf]
                attacked.unlink()
                attacked.mkdir()

                with self.assertRaisesRegex((ValueError, RuntimeError), leaf):
                    store.put("source", {}, b"row")

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
