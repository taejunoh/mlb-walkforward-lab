import copy
import dataclasses
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE
from mlb_backtest.v52_r4_snapshot_issuer import (
    _populate_snapshot_for_test, acquire_issuance_lock, publish_owned_snapshot,
)
from mlb_backtest.v52_source_contracts import canonical_json_bytes


ROOT = Path(__file__).resolve().parents[1]
VOLUME_UUID = "a" * 32


def _source_records() -> list[dict[str, object]]:
    document = json.loads((ROOT / "audit/v5_1_bt/protected_raw_inventory.json").read_text())
    return document["records"]


def _records_1675() -> list[dict[str, object]]:
    return [
        {"source": row["source"], "canonical_query": row["canonical_query"], "rawstore_verified": row["rawstore_verified"]}
        for row in _source_records()
    ]


def _bindings_5025() -> list[dict[str, object]]:
    return [
        {"record_index": index, "role": role, "path": row[role]["path"]}
        for index, row in enumerate(_source_records())
        for role in ("manifest", "raw", "seal")
    ]


def _members_4711() -> list[dict[str, object]]:
    by_path: dict[str, dict[str, object]] = {}
    for row in _source_records():
        for role in ("manifest", "raw", "seal"):
            artifact = row[role]
            by_path[artifact["path"]] = {
                "path": artifact["path"], "volume_uuid": VOLUME_UUID,
                "inode": 1, "ctime_ns": 0, "file_type": "regular", "nlink": 1,
                "byte_size": artifact["byte_size"], "sha256": artifact["sha256"], "mode": 0o444,
            }
    return [
        {**item, "inode": index + 10}
        for index, item in enumerate(sorted(by_path.values(), key=lambda value: value["path"]))
    ]


def _root_locks_4() -> list[dict[str, object]]:
    empty_sha = hashlib.sha256(b"").hexdigest()
    return [
        {"path": path, "volume_uuid": VOLUME_UUID, "inode": index + 2, "ctime_ns": 0,
         "file_type": "regular", "nlink": 1, "byte_size": 0, "sha256": empty_sha, "mode": 0o444}
        for index, path in enumerate((
            ".baseball_savant_statcast.lock", ".mlb_schedule.lock",
            ".mlb_stats_pitcher_bf.lock", ".mlb_stats_pitching.lock",
        ))
    ]


class R4CustodyArtifactRedTests(unittest.TestCase):
    def test_canonical_renderer_is_available_for_closed_r4_artifacts(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import render_canonical_document

        self.assertEqual(render_canonical_document({"a": 1}), b'{"a":1}\n')

    def test_capture_requires_two_identical_post_freeze_identity_manifests(self) -> None:
        """A delayed APFS ctime must settle before its inventory is signed."""
        from mlb_backtest import v52_r4_custody_artifacts as artifacts

        first = SimpleNamespace(snapshot={"ctime": 1}, members=("a",), root_locks=(), ancestor_identities=())
        changed = SimpleNamespace(snapshot={"ctime": 2}, members=("a",), root_locks=(), ancestor_identities=())
        settled = SimpleNamespace(snapshot={"ctime": 2}, members=("a",), root_locks=(), ancestor_identities=())
        with patch.object(
            artifacts, "_capture_frozen_snapshot_once", side_effect=(first, changed, settled, settled)
        ) as capture, patch.object(artifacts.time, "sleep"):
            result = artifacts.capture_frozen_snapshot(
                object(), repository_root=Path("repository"), custody_parent=Path("custody")
            )
        self.assertIs(result, settled)
        self.assertEqual(capture.call_count, 4)

    def test_capture_fails_closed_when_post_freeze_identity_never_settles(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts

        changing = tuple(
            SimpleNamespace(snapshot={"ctime": index}, members=("a",), root_locks=(), ancestor_identities=())
            for index in range(30)
        )
        with patch.object(
            artifacts, "_capture_frozen_snapshot_once", side_effect=changing
        ) as capture, patch.object(artifacts.time, "sleep"):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, "did not settle"):
                artifacts.capture_frozen_snapshot(
                    object(), repository_root=Path("repository"), custody_parent=Path("custody")
                )
        self.assertEqual(capture.call_count, 30)

    def test_never_settled_diagnostic_preserves_last_changed_component(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts

        changing = tuple(
            SimpleNamespace(snapshot={"ctime": 1}, members=(index,), root_locks=(), ancestor_identities=())
            for index in range(30)
        )
        with patch.object(
            artifacts, "_capture_frozen_snapshot_once", side_effect=changing
        ), patch.object(artifacts.time, "sleep"):
            with self.assertRaisesRegex(CUSTODY_UNAVAILABLE, r"did not settle \(members\)"):
                artifacts.capture_frozen_snapshot(
                    object(), repository_root=Path("repository"), custody_parent=Path("custody")
                )


class R4CustodyArtifactTests(unittest.TestCase):
    def _captured_snapshot(self):
        from mlb_backtest.v52_r4_custody_artifacts import _captured_snapshot_fixture

        return _captured_snapshot_fixture(
            _records_1675(), _members_4711(), _bindings_5025(), _root_locks_4()
        )

    @staticmethod
    def _run_spec():
        from mlb_backtest.v52_r4_custody_artifacts import _run_spec_fixture

        return _run_spec_fixture()

    def _inventory(self) -> dict[str, object]:
        from mlb_backtest.v52_r4_custody_artifacts import build_r4_inventory

        return build_r4_inventory(self._captured_snapshot())

    def test_public_synthetic_capture_and_malformed_builder_inputs_fail_closed(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            CapturedCustodySnapshot, build_r4_inventory,
        )

        with self.assertRaises(CUSTODY_UNAVAILABLE):
            CapturedCustodySnapshot([], [], [], [])
        for malformed in (None, {}, [], "capture"):
            with self.subTest(malformed=type(malformed).__name__):
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    build_r4_inventory(malformed)  # type: ignore[arg-type]

    def test_run_spec_binding_is_fixed_path_canonical_and_rebound(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            R4_RUN_SPEC_PATH, authenticate_r4_run_spec, build_transition_receipt,
        )
        from mlb_backtest.protocols import expected_r4_recovery_source_run

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            repository = Path(temporary)
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                authenticate_r4_run_spec(repository)
            path = repository / R4_RUN_SPEC_PATH
            path.parent.mkdir(parents=True)
            payload = canonical_json_bytes(expected_r4_recovery_source_run()) + b"\n"
            path.write_bytes(payload)
            binding = authenticate_r4_run_spec(repository)
            self.assertEqual(binding.sha256, "4e49bd9a261acb2237d9a0a5712568ec0e236eda2a1427f7a4035d195e242b52")
            path.write_bytes(b'{"changed":true}\n')
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                build_transition_receipt(self._inventory(), run_spec=binding)

    def test_inventory_is_canonical_closed_and_has_exact_full_closure(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            identity_manifest_sha256, render_canonical_document, validate_r4_inventory,
        )

        inventory = self._inventory()
        payload = render_canonical_document(inventory)
        self.assertEqual(payload, canonical_json_bytes(inventory) + b"\n")
        self.assertNotIn(b'"st_dev"', payload)
        self.assertEqual((len(inventory["records"]), len(inventory["members"]), len(inventory["bindings"])), (1675, 4711, 5025))
        self.assertEqual(inventory["custody_identity_manifest_sha256"], identity_manifest_sha256(inventory))
        validate_r4_inventory(inventory)

    def test_transition_receipt_binds_inventory_and_runspec_but_not_contract(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import build_transition_receipt, validate_transition_receipt

        inventory = self._inventory()
        receipt = build_transition_receipt(inventory, run_spec=self._run_spec())
        self.assertEqual(receipt["run_spec"]["sha256"], "b" * 64)
        self.assertNotIn("contract", receipt)
        validate_transition_receipt(receipt, inventory=inventory)

    def test_closed_json_schemas_accept_only_built_documents(self) -> None:
        from jsonschema import Draft202012Validator
        from mlb_backtest.v52_r4_custody_artifacts import build_transition_receipt

        self.assertEqual(sys.version_info[:2], (3, 11))
        inventory = self._inventory()
        receipt = build_transition_receipt(inventory, run_spec=self._run_spec())
        for path, document in (
            (ROOT / "schemas/v5_2_r4_protected_raw_inventory.schema.json", inventory),
            (ROOT / "schemas/v5_2_r4_physical_custody_transition_receipt.schema.json", receipt),
        ):
            with self.subTest(path=path.name):
                errors = list(Draft202012Validator(json.loads(path.read_text())).iter_errors(document))
                self.assertEqual(errors, [])

    def test_inventory_rejects_closed_schema_and_custody_mutations(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import validate_r4_inventory

        inventory = self._inventory()
        mutations = [
            lambda d: d.update(extra=True),
            lambda d: d["records"][0].update(extra=True),
            lambda d: d["records"][0]["canonical_query"].update(extra=True),
            lambda d: d["source_approval"].update(extra=True),
            lambda d: d["snapshot"].update(extra=True),
            lambda d: d["snapshot"]["root_identity"].update(extra=True),
            lambda d: d["records"].__setitem__(slice(0, 2), list(reversed(d["records"][:2]))),
            lambda d: d["members"].__setitem__(slice(0, 2), list(reversed(d["members"][:2]))),
            lambda d: d["members"][0].update(extra=True),
            lambda d: d["bindings"].__setitem__(slice(0, 2), list(reversed(d["bindings"][:2]))),
            lambda d: d["bindings"][0].update(extra=True),
            lambda d: d["root_locks"].reverse(),
            lambda d: d["root_locks"][0].update(extra=True),
            lambda d: d["ancestor_identities"][0].update(extra=True),
            lambda d: d["members"][0].update(path="data/v5_1_bt/raw/../escape.raw"),
            lambda d: d["ancestor_identities"].__setitem__(0, {**d["ancestor_identities"][0], "path": "mlb_schedule"}),
            lambda d: d["members"][0].update(volume_uuid="A" * 32),
            lambda d: d.update(manifest_count=1),
            lambda d: d["root_locks"][0].update(byte_size=1),
            lambda d: d["members"][0].update(mode=0o555),
            lambda d: d["members"][0].update(nlink=2),
            lambda d: d["source_approval"].update(logical_raw_root="wrong"),
            lambda d: d["bindings"][3].update(path=d["bindings"][0]["path"]),
            lambda d: d["bindings"][1].update(path=d["bindings"][4]["path"]),
            lambda d: d["bindings"][0].update(path="data/v5_1_bt/raw/mlb_schedule/not-a-member"),
            lambda d: d.update(custody_identity_manifest_sha256="0" * 64),
            lambda d: d["records"][0]["canonical_query"].update(start=True),
            lambda d: d["records"][218]["canonical_query"].update(pitcher_id=True),
            lambda d: d["records"][218]["canonical_query"].pop("pitcher_id"),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(inventory)
            mutate(changed)
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                validate_r4_inventory(changed)

    def test_inventory_rejects_recomputed_digest_with_wrong_member_and_binding_counts(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import identity_manifest_sha256, validate_r4_inventory

        inventory = self._inventory()
        for key, collection in (("physical_member_count", "members"), ("logical_binding_count", "bindings")):
            changed = copy.deepcopy(inventory)
            changed[collection].pop()
            changed[key] -= 1
            changed["snapshot"][key] -= 1
            changed["custody_identity_manifest_sha256"] = identity_manifest_sha256(changed)
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                validate_r4_inventory(changed)

    def test_inventory_rejects_recomputed_digest_with_hard_linked_member(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import identity_manifest_sha256, validate_r4_inventory

        changed = copy.deepcopy(self._inventory())
        changed["members"][0]["nlink"] = 2
        changed["custody_identity_manifest_sha256"] = identity_manifest_sha256(changed)
        with self.assertRaises(CUSTODY_UNAVAILABLE):
            validate_r4_inventory(changed)

    def test_inventory_rejects_shared_raw_path_with_conflicting_member_identity(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import identity_manifest_sha256, validate_r4_inventory

        changed = copy.deepcopy(self._inventory())
        shared_path = next(
            binding["path"]
            for binding in changed["bindings"]
            if binding["role"] == "raw"
            and sum(row["path"] == binding["path"] for row in changed["bindings"]) > 1
        )
        changed["members"][-1] = {
            **next(member for member in changed["members"] if member["path"] == shared_path),
            "inode": 999999,
        }
        changed["custody_identity_manifest_sha256"] = identity_manifest_sha256(changed)
        with self.assertRaises(CUSTODY_UNAVAILABLE):
            validate_r4_inventory(changed)

    def test_receipt_rejects_extra_property_and_content_digest_mutation(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import build_transition_receipt, validate_transition_receipt

        inventory = self._inventory()
        receipt = build_transition_receipt(inventory, run_spec=self._run_spec())
        with self.assertRaises(TypeError):
            validate_transition_receipt(receipt)
        for mutate in (
            lambda d: d.update(extra=True),
            lambda d: d["run_spec"].update(extra=True),
            lambda d: d["source_approval"].update(extra=True),
            lambda d: d["r4_inventory"].update(extra=True),
            lambda d: d["comparison"].update(extra=True),
            lambda d: d["comparison"]["objects"][0].update(extra=True),
            lambda d: d["comparison"]["binding_comparison"].update(extra=True),
            lambda d: d["root_lock_comparisons"][0].update(extra=True),
            lambda d: d["comparison"].update(source_content_manifest_sha256="0" * 64),
        ):
            changed = copy.deepcopy(receipt)
            mutate(changed)
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                validate_transition_receipt(changed, inventory=inventory)

    def test_self_consistent_receipt_for_different_inventory_is_rejected(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            build_transition_receipt, identity_manifest_sha256, validate_r4_inventory,
            validate_transition_receipt,
        )

        inventory = self._inventory()
        forged_inventory = copy.deepcopy(inventory)
        forged_inventory["members"][0]["inode"] += 1
        forged_inventory["custody_identity_manifest_sha256"] = identity_manifest_sha256(forged_inventory)
        validate_r4_inventory(forged_inventory)
        forged_receipt = build_transition_receipt(forged_inventory, run_spec=self._run_spec())
        with self.assertRaises(CUSTODY_UNAVAILABLE):
            validate_transition_receipt(forged_receipt, inventory=inventory)


class R4CustodyArtifactPublishTests(R4CustodyArtifactTests):
    def _owner(self, root: Path, label: str = "one"):
        custody = root / f"custody-{label}"
        custody.mkdir()
        source = root / f"source-{label}"
        source.mkdir()
        (source / "payload").write_bytes(b"payload")
        owner = acquire_issuance_lock(custody, "v5_1_bt_r4", nonce=(label.encode() * 16)[:16])
        _populate_snapshot_for_test(owner, source)
        publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
        return owner, custody

    @staticmethod
    def _artifact_parent(root: Path, artifact_path: Path) -> None:
        (root / artifact_path.parent).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _close_owner(owner, custody: Path) -> None:
        owner._close_descriptors()
        # Test-only snapshots are deliberately immutable; clear those flags so
        # TemporaryDirectory can remove this fixture tree without residue.
        for candidate in (custody, *custody.rglob("*")):
            try:
                os.chflags(candidate, 0, follow_symlinks=False)
                os.chmod(
                    candidate,
                    0o700 if candidate.is_dir() else 0o600,
                    follow_symlinks=False,
                )
            except OSError:
                pass

    def _publish_inventory_fixture(self, owner, root: Path, custody: Path):
        from mlb_backtest import v52_r4_custody_artifacts as artifacts

        with patch.object(
            artifacts, "capture_frozen_snapshot", return_value=self._captured_snapshot()
        ):
            return artifacts.publish_inventory(
                owner, repository_root=root, custody_parent=custody
            )

    def _publish_receipt_fixture(self, owner, continuation, root: Path, custody: Path):
        from mlb_backtest import v52_r4_custody_artifacts as artifacts

        with patch.object(artifacts, "authenticate_r4_run_spec", return_value=self._run_spec()):
            return artifacts.publish_transition_receipt(
                owner, continuation, repository_root=root, custody_parent=custody
            )

    def test_publish_inventory_rereads_final_no_follow_before_returning_hash(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import R4_INVENTORY_PATH

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            self._artifact_parent(root, R4_INVENTORY_PATH)
            owner, custody = self._owner(root)
            before = os.fstat(owner.parent_fd)
            lock_before = os.fstat(owner._lock_fd)
            try:
                continuation = self._publish_inventory_fixture(owner, root, custody)
                payload = (root / R4_INVENTORY_PATH).read_bytes()
                self.assertEqual(continuation.payload_sha256, hashlib.sha256(payload).hexdigest())
                self.assertEqual(payload[-1:], b"\n")
                self.assertTrue(owner.is_live_owner())
                self.assertEqual((os.fstat(owner.parent_fd).st_dev, os.fstat(owner.parent_fd).st_ino), (before.st_dev, before.st_ino))
                self.assertEqual((os.fstat(owner._lock_fd).st_dev, os.fstat(owner._lock_fd).st_ino), (lock_before.st_dev, lock_before.st_ino))
            finally:
                self._close_owner(owner, custody)

    def test_continuation_adopts_one_delayed_ctime_transition(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts
        from mlb_backtest.v52_r4_custody_artifacts import R4_INVENTORY_PATH

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            self._artifact_parent(root, R4_INVENTORY_PATH)
            owner, custody = self._owner(root)
            try:
                continuation = self._publish_inventory_fixture(owner, root, custody)
                prior = continuation.final_snapshot
                delayed = dataclasses.replace(
                    prior, identity=(*prior.identity[:-1], prior.identity[-1] + 1)
                )
                with patch.object(
                    artifacts, "rebind_regular_snapshot", return_value=delayed
                ):
                    document = artifacts._rebind_inventory_continuation(
                        owner, continuation, root
                    )
                self.assertEqual(document, continuation.document)
                self.assertEqual(continuation.final_snapshot, delayed)
            finally:
                self._close_owner(owner, custody)

    def test_continuation_rejects_same_inode_content_mutation(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts
        from mlb_backtest.v52_r4_custody_artifacts import R4_INVENTORY_PATH

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            self._artifact_parent(root, R4_INVENTORY_PATH)
            owner, custody = self._owner(root)
            try:
                continuation = self._publish_inventory_fixture(owner, root, custody)
                target = root / R4_INVENTORY_PATH
                target.write_bytes(target.read_bytes().replace(b'"PASS"', b'"FAIL"'))
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    artifacts._rebind_inventory_continuation(owner, continuation, root)
            finally:
                self._close_owner(owner, custody)

    def test_apfs_inventory_continuation_rebind_stress_is_stable(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts
        from mlb_backtest.v52_r4_custody_artifacts import R4_INVENTORY_PATH

        for iteration in range(3):
            with self.subTest(iteration=iteration), tempfile.TemporaryDirectory(dir=ROOT) as temporary:
                root = Path(temporary)
                self._artifact_parent(root, R4_INVENTORY_PATH)
                owner, custody = self._owner(root, f"s{iteration}")
                try:
                    continuation = self._publish_inventory_fixture(owner, root, custody)
                    self.assertEqual(
                        artifacts._rebind_inventory_continuation(owner, continuation, root),
                        continuation.document,
                    )
                finally:
                    self._close_owner(owner, custody)

    def test_publish_receipt_rereads_final_no_follow_before_returning_hash(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            R4_INVENTORY_PATH, R4_RECEIPT_PATH,
        )

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            self._artifact_parent(root, R4_INVENTORY_PATH)
            self._artifact_parent(root, R4_RECEIPT_PATH)
            owner, custody = self._owner(root)
            before = os.fstat(owner.parent_fd)
            lock_before = os.fstat(owner._lock_fd)
            try:
                continuation = self._publish_inventory_fixture(owner, root, custody)
                digest = self._publish_receipt_fixture(owner, continuation, root, custody)
                payload = (root / R4_RECEIPT_PATH).read_bytes()
                self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
                self.assertEqual(payload[-1:], b"\n")
                self.assertTrue(owner.is_live_owner())
                self.assertEqual((os.fstat(owner.parent_fd).st_dev, os.fstat(owner.parent_fd).st_ino), (before.st_dev, before.st_ino))
                self.assertEqual((os.fstat(owner._lock_fd).st_dev, os.fstat(owner._lock_fd).st_ino), (lock_before.st_dev, lock_before.st_ino))
            finally:
                self._close_owner(owner, custody)

    def test_publish_conflict_preserves_existing_final_bytes(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import R4_INVENTORY_PATH

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            parent = root / R4_INVENTORY_PATH.parent
            parent.mkdir(parents=True)
            target = parent / R4_INVENTORY_PATH.name
            target.write_bytes(b"existing")
            owner, custody = self._owner(root)
            try:
                with self.assertRaises(OSError):
                    self._publish_inventory_fixture(owner, root, custody)
            finally:
                self._close_owner(owner, custody)
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(len(list(parent.glob(f".{R4_INVENTORY_PATH.name}.*.tmp"))), 1)

    def test_receipt_conflict_preserves_existing_bytes_and_keeps_incomplete_residue(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            R4_INVENTORY_PATH, R4_RECEIPT_PATH,
        )

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            self._artifact_parent(root, R4_INVENTORY_PATH)
            parent = root / R4_RECEIPT_PATH.parent
            target = parent / R4_RECEIPT_PATH.name
            owner, custody = self._owner(root)
            try:
                continuation = self._publish_inventory_fixture(owner, root, custody)
                target.write_bytes(b"existing receipt")
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    self._publish_receipt_fixture(owner, continuation, root, custody)
            finally:
                self._close_owner(owner, custody)
            self.assertEqual(target.read_bytes(), b"existing receipt")
            self.assertEqual(list(parent.glob(f".{R4_RECEIPT_PATH.name}.*.tmp")), [])

    def test_publish_rejects_arbitrary_owner_parent(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import R4_INVENTORY_PATH

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            arbitrary = root / "arbitrary"
            arbitrary.mkdir()
            self._artifact_parent(root, R4_INVENTORY_PATH)
            owner, custody = self._owner(root)
            try:
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    self._publish_inventory_fixture(owner, arbitrary, custody)
            finally:
                self._close_owner(owner, custody)
            self.assertEqual(list(arbitrary.iterdir()), [])

    def test_receipt_rejects_restarted_snapshot_owner(self) -> None:
        from mlb_backtest.v52_r4_custody_artifacts import (
            R4_INVENTORY_PATH, R4_RECEIPT_PATH,
        )

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            self._artifact_parent(root, R4_INVENTORY_PATH)
            self._artifact_parent(root, R4_RECEIPT_PATH)
            first, first_custody = self._owner(root, "first")
            second, second_custody = self._owner(root, "second")
            try:
                continuation = self._publish_inventory_fixture(first, root, first_custody)
                with self.assertRaises(CUSTODY_UNAVAILABLE):
                    self._publish_receipt_fixture(second, continuation, root, second_custody)
            finally:
                self._close_owner(second, second_custody)
                self._close_owner(first, first_custody)

    def test_final_reread_rejects_name_swap_after_descriptor_read(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts
        from mlb_backtest.v52_r4_snapshot_issuer import publish_canonical_json

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            parent = root / "artifact-parent"
            parent.mkdir()
            with acquire_issuance_lock(parent, "artifact", nonce=b"\x07" * 16) as owner:
                publish_canonical_json(owner.parent_fd, ".temp", "final.json", {"a": 1})
                replacement = parent / "replacement.json"
                replacement.write_bytes(b'{"a":2}\n')
                original_read = artifacts.os.read
                swapped = False

                def read_and_swap(fd: int, count: int) -> bytes:
                    nonlocal swapped
                    result = original_read(fd, count)
                    if result and not swapped:
                        swapped = True
                        os.replace(replacement, parent / "final.json")
                    return result

                with patch.object(artifacts.os, "read", side_effect=read_and_swap):
                    with self.assertRaises(CUSTODY_UNAVAILABLE):
                        artifacts._reread_final_json(owner.parent_fd, "final.json")

    def test_final_reread_rejects_hard_link_added_during_read(self) -> None:
        from mlb_backtest import v52_r4_custody_artifacts as artifacts
        from mlb_backtest.v52_r4_snapshot_issuer import publish_canonical_json

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            parent = Path(temporary) / "artifact-parent"
            parent.mkdir()
            with acquire_issuance_lock(parent, "artifact", nonce=b"\x08" * 16) as owner:
                publish_canonical_json(owner.parent_fd, ".temp", "final.json", {"a": 1})
                original_read = artifacts.os.read
                linked = False

                def read_and_link(fd: int, count: int) -> bytes:
                    nonlocal linked
                    result = original_read(fd, count)
                    if result and not linked:
                        linked = True
                        os.link(parent / "final.json", parent / "final.alias")
                    return result

                with patch.object(artifacts.os, "read", side_effect=read_and_link):
                    with self.assertRaises(CUSTODY_UNAVAILABLE):
                        artifacts._reread_final_json(owner.parent_fd, "final.json")

    def test_shared_issuer_reread_rejects_name_swap_after_hash(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            parent = Path(temporary) / "artifact-parent"
            parent.mkdir()
            replacement = parent / "replacement.json"
            replacement.write_bytes(b'{"a":2}\n')
            original_hash = issuer._hash_descriptor
            swapped = False

            def hash_and_swap(fd: int, label: str) -> tuple[str, int]:
                nonlocal swapped
                result = original_hash(fd, label)
                if not swapped:
                    swapped = True
                    os.replace(replacement, parent / "final.json")
                return result

            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with patch.object(issuer, "_hash_descriptor", side_effect=hash_and_swap):
                    with self.assertRaises(CUSTODY_UNAVAILABLE):
                        issuer.publish_canonical_json(descriptor, ".temp", "final.json", {"a": 1})
            finally:
                os.close(descriptor)

    def test_shared_issuer_conflict_keeps_possible_replacement_temp(self) -> None:
        from mlb_backtest import v52_r4_snapshot_issuer as issuer

        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            parent = Path(temporary) / "artifact-parent"
            parent.mkdir()
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                def swap_then_conflict(source: str, destination: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
                    os.unlink(source, dir_fd=src_dir_fd)
                    replacement = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=src_dir_fd)
                    try:
                        os.write(replacement, b"replacement")
                    finally:
                        os.close(replacement)
                    raise FileExistsError("injected conflict")

                with patch.object(issuer, "rename_exclusive_at", side_effect=swap_then_conflict):
                    with self.assertRaises(FileExistsError):
                        issuer.publish_canonical_json(descriptor, ".temp", "final.json", {"a": 1})
                self.assertEqual((parent / ".temp").read_bytes(), b"replacement")
                self.assertFalse((parent / "final.json").exists())
            finally:
                os.close(descriptor)
