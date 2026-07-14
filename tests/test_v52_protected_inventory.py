from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.manifest import RawStore
import mlb_backtest.v52_protected_inventory as inventory_module

try:
    from mlb_backtest.v52_protected_inventory import (
        build_protected_raw_inventory,
        verify_protected_raw_inventory,
    )
except ImportError:
    build_protected_raw_inventory = None
    verify_protected_raw_inventory = None


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas/v5_1_protected_raw_inventory.schema.json"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SCHEDULE_QUERY = {
    "end": "2022-10-05",
    "gameType": "R",
    "role": "backtest",
    "start": "2022-04-07",
    "url": (
        "https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R"
        "&startDate=2022-04-07&endDate=2022-10-05"
    ),
}
SAVANT_QUERY = {
    "end": "2022-04-30",
    "game_type": "R",
    "start": "2022-04-01",
    "url": "https://baseballsavant.mlb.com/statcast_search/csv",
}


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class V52ProtectedInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            build_protected_raw_inventory,
            "protected raw inventory builder is missing",
        )
        self.assertIsNotNone(
            verify_protected_raw_inventory,
            "protected raw inventory verifier is missing",
        )

    def _fixture(self, root: Path):
        legacy_root = root / "data/v5_1_bt/raw"
        artifact = RawStore(legacy_root).put(
            "mlb_schedule", dict(SCHEDULE_QUERY), b"row-one\nrow-two\n"
        )
        return legacy_root, artifact

    def test_build_inventories_manifest_seal_and_referenced_raw_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, artifact = self._fixture(root)
            inventory = build_protected_raw_inventory(root, legacy_root)

            self.assertEqual(
                set(inventory),
                {
                    "legacy_root",
                    "manifest_count",
                    "rawstore_verification",
                    "records",
                    "schema_version",
                },
            )
            self.assertEqual(inventory["schema_version"], 1)
            self.assertEqual(inventory["legacy_root"], "data/v5_1_bt/raw")
            self.assertEqual(inventory["manifest_count"], 1)
            self.assertEqual(inventory["rawstore_verification"], "PASS")
            record = inventory["records"][0]
            self.assertEqual(record["source"], "mlb_schedule")
            self.assertEqual(record["canonical_query"], SCHEDULE_QUERY)
            self.assertTrue(record["rawstore_verified"])

            manifest = artifact.manifest_path
            seal = manifest.with_name(manifest.name[:-5] + ".sha256")
            expected_paths = {
                "manifest": manifest.relative_to(root).as_posix(),
                "seal": seal.relative_to(root).as_posix(),
                "raw": artifact.data_path.relative_to(root).as_posix(),
            }
            for kind, expected_path in expected_paths.items():
                item = record[kind]
                metadata = (root / expected_path).stat(follow_symlinks=False)
                self.assertEqual(item["path"], expected_path)
                self.assertEqual(item["sha256"], _sha256(root / expected_path))
                self.assertRegex(item["sha256"], SHA256)
                self.assertEqual(item["byte_size"], metadata.st_size)
                self.assertEqual(
                    item["inode_fingerprint"],
                    {"device": metadata.st_dev, "inode": metadata.st_ino},
                )

    def test_records_are_stably_sorted_and_schema_keys_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root = root / "data/v5_1_bt/raw"
            store = RawStore(legacy_root)
            store.put("mlb_schedule", dict(SCHEDULE_QUERY), b"z\n")
            store.put("baseball_savant_statcast", dict(SAVANT_QUERY), b"a\n")

            first = build_protected_raw_inventory(root, legacy_root)
            second = build_protected_raw_inventory(root, legacy_root)
            paths = [record["manifest"]["path"] for record in first["records"]]
            self.assertEqual(paths, sorted(paths))
            self.assertEqual(first, second)
            self.assertEqual(_canonical_bytes(first), _canonical_bytes(second))

            schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(first))
            self.assertEqual(set(schema["properties"]), set(first))
            record_schema = schema["$defs"]["record"]
            self.assertFalse(record_schema["additionalProperties"])
            self.assertEqual(set(record_schema["required"]), set(first["records"][0]))
            self.assertEqual(set(record_schema["properties"]), set(first["records"][0]))
            object_schema = schema["$defs"]["object"]
            self.assertFalse(object_schema["additionalProperties"])
            self.assertEqual(
                set(object_schema["required"]), set(first["records"][0]["raw"])
            )

    def test_verify_rebuilds_inventory_and_rejects_inventory_or_raw_tampering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, artifact = self._fixture(root)
            inventory = build_protected_raw_inventory(root, legacy_root)
            inventory_path = root / "audit/v5_1_bt/protected_raw_inventory.json"
            inventory_path.parent.mkdir(parents=True)
            inventory_path.write_bytes(_canonical_bytes(inventory))

            self.assertEqual(
                verify_protected_raw_inventory(root, inventory_path), inventory
            )

            tampered = json.loads(json.dumps(inventory))
            tampered["records"][0]["raw"]["sha256"] = "0" * 64
            inventory_path.write_bytes(_canonical_bytes(tampered))
            with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                verify_protected_raw_inventory(root, inventory_path)

            inventory_path.write_bytes(_canonical_bytes(inventory))
            artifact.data_path.write_bytes(b"tampered\n")
            with self.assertRaisesRegex(RuntimeError, "payload digest mismatch"):
                verify_protected_raw_inventory(root, inventory_path)

    def test_build_rejects_mutation_between_rawstore_verification_and_inventory_reads(
        self,
    ) -> None:
        original_records = inventory_module._records_from_verified_catalog

        def mutate_raw(catalog):
            catalog[0]["payload_path"].write_bytes(b"bad-one\nbad-two\n")

        def mutate_manifest_and_seal(catalog):
            manifest_path = catalog[0]["manifest_path"]
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            document["retrieved_at_utc"] = "2000-01-01T00:00:00Z"
            content = json.dumps(
                document, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8")
            manifest_path.write_bytes(content)
            seal_path = manifest_path.with_name(manifest_path.name[:-5] + ".sha256")
            seal_path.write_text(
                hashlib.sha256(content).hexdigest() + "\n", encoding="ascii"
            )

        def mutate_seal(catalog):
            manifest_path = catalog[0]["manifest_path"]
            seal_path = manifest_path.with_name(manifest_path.name[:-5] + ".sha256")
            seal_path.write_bytes(b"0" * 64 + b"\n")

        for label, mutation in (
            ("raw", mutate_raw),
            ("manifest", mutate_manifest_and_seal),
            ("seal", mutate_seal),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                legacy_root, _ = self._fixture(root)

                def catalog_then_mutate(repository_root, catalog, bound_tree):
                    mutation(catalog)
                    return original_records(repository_root, catalog, bound_tree)

                with (
                    patch.object(
                        inventory_module,
                        "_records_from_verified_catalog",
                        side_effect=catalog_then_mutate,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "payload digest mismatch|changed after streaming verification",
                    ),
                ):
                    build_protected_raw_inventory(root, legacy_root)

    def test_build_rejects_raw_identity_change_after_cached_hash(self) -> None:
        original_records = inventory_module._records_from_verified_catalog
        for mutation_kind in ("replace", "rewrite", "rewrite_restore_mtime"):
            with (
                self.subTest(mutation_kind=mutation_kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                legacy_root, _ = self._fixture(root)

                def mutate_after_hash(repository_root, catalog, bound_tree):
                    records = original_records(repository_root, catalog, bound_tree)
                    raw_path = Path(catalog[0]["payload_path"])
                    content = raw_path.read_bytes()
                    before = raw_path.stat(follow_symlinks=False)
                    if mutation_kind == "replace":
                        replacement = raw_path.with_name(raw_path.name + ".replacement")
                        replacement.write_bytes(content)
                        os.replace(replacement, raw_path)
                    elif mutation_kind == "rewrite":
                        raw_path.write_bytes(content[::-1])
                        os.utime(
                            raw_path,
                            ns=(before.st_atime_ns, before.st_mtime_ns + 1),
                        )
                    else:
                        for _ in range(100):
                            raw_path.write_bytes(content[::-1])
                            os.utime(
                                raw_path,
                                ns=(before.st_atime_ns, before.st_mtime_ns),
                            )
                            if (
                                raw_path.stat(follow_symlinks=False).st_ctime_ns
                                != before.st_ctime_ns
                            ):
                                break
                    after = raw_path.stat(follow_symlinks=False)
                    before_six = (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_mode,
                        before.st_nlink,
                    )
                    after_six = (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_mode,
                        after.st_nlink,
                    )
                    if mutation_kind == "rewrite_restore_mtime":
                        self.assertEqual(after_six, before_six)
                        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
                    else:
                        self.assertNotEqual(after_six, before_six)
                    return records

                with (
                    patch.object(
                        inventory_module,
                        "_records_from_verified_catalog",
                        side_effect=mutate_after_hash,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError, "leaf identity|membership changed"
                    ),
                ):
                    build_protected_raw_inventory(root, legacy_root)

    def test_build_rejects_source_parent_swapped_to_external_symlink_after_verify(
        self,
    ) -> None:
        original_records = inventory_module._records_from_verified_catalog
        with (
            tempfile.TemporaryDirectory() as repository_directory,
            tempfile.TemporaryDirectory() as external_directory,
        ):
            root = Path(repository_directory).resolve()
            external_root = Path(external_directory).resolve()
            legacy_root, _ = self._fixture(root)

            def catalog_then_swap_source(repository_root, catalog, bound_tree):
                source_path = legacy_root / "mlb_schedule"
                external_source = external_root / "mlb_schedule"
                source_path.rename(external_source)
                source_path.symlink_to(external_source, target_is_directory=True)
                return original_records(repository_root, catalog, bound_tree)

            with (
                patch.object(
                    inventory_module,
                    "_records_from_verified_catalog",
                    side_effect=catalog_then_swap_source,
                ),
                self.assertRaisesRegex(
                    (RuntimeError, ValueError), "symlink|path binding|source directory"
                ),
            ):
                build_protected_raw_inventory(root, legacy_root)

    def test_build_rejects_orphan_and_unknown_source_leaves(self) -> None:
        orphan_names = {
            "orphan_raw": f"{'0' * 64}.raw",
            "orphan_seal": f"{'1' * 64}.{'2' * 64}.manifest.sha256",
            "unknown_leaf": "unknown.txt",
        }
        for label, leaf_name in orphan_names.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                legacy_root, _ = self._fixture(root)
                (legacy_root / "mlb_schedule" / leaf_name).write_bytes(b"orphan\n")

                with self.assertRaisesRegex(
                    (RuntimeError, ValueError), "membership|orphan|unexpected"
                ):
                    build_protected_raw_inventory(root, legacy_root)

    def test_build_rejects_root_membership_outside_source_lock_model(self) -> None:
        for label, mutate in (
            (
                "extra_root_file",
                lambda legacy_root: (legacy_root / "unexpected.txt").write_bytes(b"x"),
            ),
            (
                "missing_source_lock",
                lambda legacy_root: (legacy_root / ".mlb_schedule.lock").unlink(),
            ),
            (
                "orphan_lock",
                lambda legacy_root: (legacy_root / ".orphan.lock").write_bytes(b""),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                legacy_root, _ = self._fixture(root)
                mutate(legacy_root)

                with self.assertRaisesRegex(
                    (RuntimeError, ValueError), "root lock membership|root membership"
                ):
                    build_protected_raw_inventory(root, legacy_root)

    def test_build_rejects_valid_manifest_set_inserted_after_catalog_snapshot(
        self,
    ) -> None:
        original_records = inventory_module._records_from_verified_catalog
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, _ = self._fixture(root)

            def insert_then_hash(repository_root, verified, bound_tree):
                inserted_query = dict(SCHEDULE_QUERY)
                inserted_query["end"] = "2023-10-01"
                inserted_query["start"] = "2023-03-30"
                inserted_query["url"] = (
                    inserted_query["url"]
                    .replace("2022-04-07", "2023-03-30")
                    .replace("2022-10-05", "2023-10-01")
                )
                RawStore(legacy_root).put("mlb_schedule", inserted_query, b"inserted\n")
                return original_records(repository_root, verified, bound_tree)

            with (
                patch.object(
                    inventory_module,
                    "_records_from_verified_catalog",
                    side_effect=insert_then_hash,
                ),
                self.assertRaisesRegex(
                    (RuntimeError, ValueError), "membership|catalog|changed"
                ),
            ):
                build_protected_raw_inventory(root, legacy_root)

    def test_build_and_verify_do_not_call_rawstore_whole_file_verifier(self) -> None:
        for operation in ("build", "verify"):
            with (
                self.subTest(operation=operation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                legacy_root, _ = self._fixture(root)
                inventory_path = root / "inventory.json"
                if operation == "verify":
                    inventory = build_protected_raw_inventory(root, legacy_root)
                    inventory_path.write_bytes(_canonical_bytes(inventory))

                with patch.object(
                    RawStore,
                    "verify_manifests",
                    side_effect=AssertionError("whole-file verifier called"),
                ):
                    try:
                        if operation == "build":
                            build_protected_raw_inventory(root, legacy_root)
                        else:
                            verify_protected_raw_inventory(root, inventory_path)
                    except AssertionError as exc:
                        self.fail(str(exc))

    def test_unique_raw_payload_is_streamed_once_in_bounded_chunks(self) -> None:
        chunk_limit = 1024 * 1024
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root = root / "data/v5_1_bt/raw"
            store = RawStore(legacy_root)
            payload = b"x" * (2 * chunk_limit + 137)
            first_query = dict(SCHEDULE_QUERY)
            second_query = dict(SCHEDULE_QUERY)
            second_query["end"] = "2023-10-01"
            second_query["start"] = "2023-03-30"
            second_query["url"] = (
                second_query["url"]
                .replace("2022-04-07", "2023-03-30")
                .replace("2022-10-05", "2023-10-01")
            )
            store.put("mlb_schedule", first_query, payload)
            store.put("mlb_schedule", second_query, payload)

            original_stream = inventory_module._stream_object
            original_read = inventory_module.os.read
            raw_streams: list[Path] = []
            requested_sizes: list[int] = []

            def tracking_stream(*args, **kwargs):
                path = Path(args[1])
                if path.suffix == ".raw":
                    raw_streams.append(path)
                return original_stream(*args, **kwargs)

            def tracking_read(descriptor, size):
                requested_sizes.append(size)
                return original_read(descriptor, size)

            with (
                patch.object(
                    inventory_module, "_stream_object", side_effect=tracking_stream
                ),
                patch.object(inventory_module.os, "read", side_effect=tracking_read),
            ):
                build_protected_raw_inventory(root, legacy_root)

            self.assertEqual(raw_streams, [raw_streams[0]])
            self.assertGreaterEqual(requested_sizes.count(chunk_limit), 3)
            self.assertLessEqual(max(requested_sizes), chunk_limit)

    def test_nonempty_row_counter_is_constant_bounded_and_splitlines_equivalent(
        self,
    ) -> None:
        counter_type = getattr(inventory_module, "_NonemptyRowCounter", None)
        self.assertIsNotNone(counter_type, "constant-bounded row counter is missing")
        cases = (
            b"",
            b"alpha",
            b" ",
            b"alpha\n",
            b"alpha\rbeta",
            b"alpha\r\nbeta\r\n",
            b"alpha\r\r\nbeta",
            b" \r\t\r\nalpha\n \t\rbravo",
            b"alpha\vbravo\fcharlie\x1cdelta\x1eecho\x85foxtrot",
        )
        for payload in cases:
            expected = sum(bool(line.strip()) for line in payload.splitlines())
            for chunk_size in (1, 2, 3, 7, 1024):
                with self.subTest(payload=payload, chunk_size=chunk_size):
                    counter = counter_type()
                    retained = []
                    for offset in range(0, len(payload), chunk_size):
                        counter.feed(payload[offset : offset + chunk_size])
                        retained.append(counter.retained_byte_count)
                    self.assertEqual(counter.finish(), expected)
                    self.assertLessEqual(max(retained, default=0), 1)

        counter = counter_type()
        chunk = b"x" * (1024 * 1024)
        for _ in range(8):
            counter.feed(chunk)
            self.assertLessEqual(counter.retained_byte_count, 1)
        self.assertEqual(counter.finish(), 1)

    def test_streamed_row_count_handles_cr_crlf_boundaries_and_whitespace(
        self,
    ) -> None:
        chunk_limit = 1024 * 1024
        cases = {
            "cr_only": b"alpha\rbravo\r \t\rcharlie\r",
            "crlf_boundary": b"x" * (chunk_limit - 1) + b"\r\nnext\r\n",
            "whitespace_only": b" \t\r\n\t\r \nalpha\r\t\r\nbravo\n",
        }
        for label, payload in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                legacy_root = root / "data/v5_1_bt/raw"
                RawStore(legacy_root).put("mlb_schedule", dict(SCHEDULE_QUERY), payload)
                try:
                    inventory = build_protected_raw_inventory(root, legacy_root)
                except RuntimeError as exc:
                    self.fail(
                        f"streamed row count differs from bytes.splitlines: {exc}"
                    )
                self.assertTrue(inventory["records"][0]["rawstore_verified"])

    def test_schema_and_runtime_reject_source_query_unknown_keys_and_wrong_types(
        self,
    ) -> None:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        record_schema = schema["$defs"]["record"]
        supported = {
            "baseball_savant_statcast": {
                "end",
                "game_type",
                "start",
                "url",
            },
            "mlb_schedule": {"end", "gameType", "role", "start", "url"},
            "mlb_stats_pitcher_bf": {
                "endDate",
                "gameType",
                "group",
                "pitcher_id",
                "sportIds",
                "startDate",
                "stats",
                "url",
            },
            "mlb_stats_pitching": {
                "endDate",
                "gameType",
                "group",
                "limit",
                "playerPool",
                "sportIds",
                "startDate",
                "stats",
                "url",
            },
        }
        self.assertEqual(
            set(record_schema["properties"]["source"].get("enum", [])),
            set(supported),
        )
        canonical_schema = record_schema["properties"]["canonical_query"]
        self.assertEqual(len(canonical_schema.get("oneOf", [])), 4)
        for source, keys in supported.items():
            query_schema = schema["$defs"].get(f"{source}_query", {})
            self.assertFalse(query_schema.get("additionalProperties", True), source)
            self.assertEqual(set(query_schema.get("required", [])), keys, source)
            self.assertEqual(set(query_schema.get("properties", {})), keys, source)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, _ = self._fixture(root)
            inventory = build_protected_raw_inventory(root, legacy_root)
            inventory_path = root / "inventory.json"
            invalid_cases = {}
            unknown = json.loads(json.dumps(inventory))
            unknown["records"][0]["canonical_query"]["unknown"] = True
            invalid_cases["unknown"] = unknown
            wrong_type = json.loads(json.dumps(inventory))
            wrong_type["records"][0]["canonical_query"]["start"] = 2022
            invalid_cases["wrong_type"] = wrong_type
            mismatch = json.loads(json.dumps(inventory))
            mismatch["records"][0]["source"] = "baseball_savant_statcast"
            invalid_cases["source_mismatch"] = mismatch

            for label, invalid in invalid_cases.items():
                with self.subTest(label=label):
                    inventory_path.write_bytes(_canonical_bytes(invalid))
                    with self.assertRaisesRegex(
                        ValueError, "canonical query|source query"
                    ):
                        verify_protected_raw_inventory(root, inventory_path)

    def test_verify_rejects_boolean_for_every_closed_integer_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root = root / "data/v5_1_bt/raw"
            RawStore(legacy_root).put("mlb_schedule", dict(SCHEDULE_QUERY), b"x")
            inventory = build_protected_raw_inventory(root, legacy_root)
            inventory_path = root / "inventory.json"

            mutations = {
                "schema_version": lambda item: item.__setitem__("schema_version", True),
                "manifest_count": lambda item: item.__setitem__("manifest_count", True),
            }
            for object_kind in ("manifest", "raw", "seal"):
                mutations[f"{object_kind}.byte_size"] = lambda item, kind=object_kind: (
                    item["records"][0][kind].__setitem__("byte_size", True)
                )
                for fingerprint_key in ("device", "inode"):
                    mutations[f"{object_kind}.{fingerprint_key}"] = (
                        lambda item, kind=object_kind, key=fingerprint_key: item[
                            "records"
                        ][0][kind]["inode_fingerprint"].__setitem__(key, True)
                    )

            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    invalid = json.loads(json.dumps(inventory))
                    mutate(invalid)
                    inventory_path.write_bytes(_canonical_bytes(invalid))
                    with self.assertRaisesRegex(ValueError, "must be an integer"):
                        verify_protected_raw_inventory(root, inventory_path)

    def test_rejects_noncanonical_inventory_symlink_hardlink_and_special_nodes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, artifact = self._fixture(root)
            inventory = build_protected_raw_inventory(root, legacy_root)
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                verify_protected_raw_inventory(root, inventory_path)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, artifact = self._fixture(root)
            manifest = artifact.manifest_path
            original = manifest.read_bytes()
            manifest.unlink()
            target = root / "target"
            target.write_bytes(original)
            manifest.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file|symlink"):
                build_protected_raw_inventory(root, legacy_root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, artifact = self._fixture(root)
            os.link(artifact.data_path, root / "raw-hardlink")
            with self.assertRaisesRegex(ValueError, "hard link"):
                build_protected_raw_inventory(root, legacy_root)

        if hasattr(os, "mkfifo"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                legacy_root, artifact = self._fixture(root)
                seal = artifact.manifest_path.with_name(
                    artifact.manifest_path.name[:-5] + ".sha256"
                )
                seal.unlink()
                os.mkfifo(seal)
                with self.assertRaisesRegex(ValueError, "regular file"):
                    build_protected_raw_inventory(root, legacy_root)

    def test_rejects_inventory_reached_through_symlinked_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root, _ = self._fixture(root)
            inventory = build_protected_raw_inventory(root, legacy_root)
            real_parent = root / "real-audit"
            real_parent.mkdir()
            inventory_path = real_parent / "inventory.json"
            inventory_path.write_bytes(_canonical_bytes(inventory))
            (root / "linked-audit").symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink|real (parent|directory)"):
                verify_protected_raw_inventory(
                    root, root / "linked-audit/inventory.json"
                )

    def test_verify_rejects_inventory_parent_swapped_external_after_under_root(
        self,
    ) -> None:
        original_under_root = inventory_module._under_root
        with (
            tempfile.TemporaryDirectory() as repository_directory,
            tempfile.TemporaryDirectory() as external_directory,
        ):
            root = Path(repository_directory).resolve()
            external_root = Path(external_directory).resolve()
            legacy_root, _ = self._fixture(root)
            inventory = build_protected_raw_inventory(root, legacy_root)
            inventory_path = root / "audit/protected_raw_inventory.json"
            inventory_path.parent.mkdir()
            inventory_path.write_bytes(_canonical_bytes(inventory))

            def under_root_then_swap(repository_root, path, label):
                result = original_under_root(repository_root, path, label)
                if label == "inventory path":
                    audit_path = root / "audit"
                    external_audit = external_root / "audit"
                    audit_path.rename(external_audit)
                    audit_path.symlink_to(external_audit, target_is_directory=True)
                    self.assertFalse(
                        inventory_path.resolve().is_relative_to(root),
                        "reproduction must move the resolved inventory outside root",
                    )
                return result

            with (
                patch.object(
                    inventory_module,
                    "_under_root",
                    side_effect=under_root_then_swap,
                ),
                self.assertRaisesRegex(
                    (RuntimeError, ValueError), "symlink|path binding|inventory parent"
                ),
            ):
                verify_protected_raw_inventory(root, inventory_path)


if __name__ == "__main__":
    unittest.main()
