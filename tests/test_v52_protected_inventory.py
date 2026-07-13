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


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


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
            "source", {"season": 2022, "kind": "fixture"}, b"row-one\nrow-two\n"
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
            self.assertEqual(record["source"], "source")
            self.assertEqual(
                record["canonical_query"], {"kind": "fixture", "season": 2022}
            )
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
            store.put("z_source", {"season": 2023}, b"z\n")
            store.put("a_source", {"season": 2022}, b"a\n")

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

    def test_verify_rebuilds_inventory_and_rejects_inventory_or_raw_tampering(self) -> None:
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
        original_verify = RawStore.verify_manifests

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
            seal_path = manifest_path.with_name(
                manifest_path.name[:-5] + ".sha256"
            )
            seal_path.write_text(
                hashlib.sha256(content).hexdigest() + "\n", encoding="ascii"
            )

        def mutate_seal(catalog):
            manifest_path = catalog[0]["manifest_path"]
            seal_path = manifest_path.with_name(
                manifest_path.name[:-5] + ".sha256"
            )
            seal_path.write_bytes(b"0" * 64 + b"\n")

        for label, mutation in (
            ("raw", mutate_raw),
            ("manifest", mutate_manifest_and_seal),
            ("seal", mutate_seal),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                legacy_root, _ = self._fixture(root)

                def verify_then_mutate(store):
                    catalog = original_verify(store)
                    mutation(catalog)
                    return catalog

                with (
                    patch.object(
                        RawStore, "verify_manifests", verify_then_mutate
                    ),
                    self.assertRaisesRegex(
                        RuntimeError, "changed after RawStore verification"
                    ),
                ):
                    build_protected_raw_inventory(root, legacy_root)

    def test_build_rejects_source_parent_swapped_to_external_symlink_after_verify(
        self,
    ) -> None:
        original_verify = RawStore.verify_manifests
        with (
            tempfile.TemporaryDirectory() as repository_directory,
            tempfile.TemporaryDirectory() as external_directory,
        ):
            root = Path(repository_directory).resolve()
            external_root = Path(external_directory).resolve()
            legacy_root, _ = self._fixture(root)

            def verify_then_swap_source(store):
                catalog = original_verify(store)
                source_path = legacy_root / "source"
                external_source = external_root / "source"
                source_path.rename(external_source)
                source_path.symlink_to(external_source, target_is_directory=True)
                return catalog

            with (
                patch.object(
                    RawStore, "verify_manifests", verify_then_swap_source
                ),
                self.assertRaisesRegex(
                    (RuntimeError, ValueError), "symlink|path binding|source directory"
                ),
            ):
                build_protected_raw_inventory(root, legacy_root)

    def test_verify_rejects_boolean_for_every_closed_integer_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            legacy_root = root / "data/v5_1_bt/raw"
            RawStore(legacy_root).put("source", {"season": 2022}, b"x")
            inventory = build_protected_raw_inventory(root, legacy_root)
            inventory_path = root / "inventory.json"

            mutations = {
                "schema_version": lambda item: item.__setitem__(
                    "schema_version", True
                ),
                "manifest_count": lambda item: item.__setitem__(
                    "manifest_count", True
                ),
            }
            for object_kind in ("manifest", "raw", "seal"):
                mutations[f"{object_kind}.byte_size"] = (
                    lambda item, kind=object_kind: item["records"][0][kind].__setitem__(
                        "byte_size", True
                    )
                )
                for fingerprint_key in ("device", "inode"):
                    mutations[f"{object_kind}.{fingerprint_key}"] = (
                        lambda item,
                        kind=object_kind,
                        key=fingerprint_key: item["records"][0][kind][
                            "inode_fingerprint"
                        ].__setitem__(key, True)
                    )

            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    invalid = json.loads(json.dumps(inventory))
                    mutate(invalid)
                    inventory_path.write_bytes(_canonical_bytes(invalid))
                    with self.assertRaisesRegex(ValueError, "must be an integer"):
                        verify_protected_raw_inventory(root, inventory_path)

    def test_rejects_noncanonical_inventory_symlink_hardlink_and_special_nodes(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
