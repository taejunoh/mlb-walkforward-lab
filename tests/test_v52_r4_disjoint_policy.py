from __future__ import annotations

import json
import unittest
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"
GENERATION = "v5.2-r4-disjoint-custody-v2"
CUSTODY_PARENT = Path(
    "/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2"
)
FINAL_NAME = "v5_1_bt_r4"
OWNER_LOCK_NAME = ".v5_1_bt_r4.disjoint-v2.issuance.lock"
JOURNAL_NAME = ".v5_1_bt_r4.disjoint-v2.issuance.journal.ndjson"
ARTIFACT_PATHS = {
    "inventory": PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/protected_raw_inventory_r4_disjoint_v2.json"
    ),
    "receipt": PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/physical_custody_transition_r4_disjoint_v2.json"
    ),
    "contract": PurePosixPath(
        "protocols/v5_2_qd/"
        "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4_disjoint_v2.json"
    ),
    "anchor": PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/physical_custody_authority_anchor_r4_disjoint_v2.json"
    ),
    "signature": PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/physical_custody_authority_anchor_r4_disjoint_v2.ed25519"
    ),
    "journal": PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/custody_issuance_journal_r4_disjoint_v2.ndjson"
    ),
}


class DisjointPolicyTests(unittest.TestCase):
    def test_fixed_constants_and_policy(self) -> None:
        from mlb_backtest.v52_r4_disjoint_policy import (
            CUSTODY_PARENT as actual_parent,
            DISJOINT_ARTIFACT_PATHS,
            FINAL_NAME as actual_final,
            GENERATION as actual_generation,
            JOURNAL_NAME as actual_journal,
            OWNER_LOCK_NAME as actual_lock,
            require_disjoint_policy,
        )

        self.assertEqual(actual_generation, GENERATION)
        self.assertEqual(actual_parent, CUSTODY_PARENT)
        self.assertEqual(actual_final, FINAL_NAME)
        self.assertEqual(actual_lock, OWNER_LOCK_NAME)
        self.assertEqual(actual_journal, JOURNAL_NAME)
        self.assertEqual(dict(DISJOINT_ARTIFACT_PATHS), ARTIFACT_PATHS)
        policy = require_disjoint_policy(
            generation=GENERATION,
            custody_parent=CUSTODY_PARENT,
            final_name=FINAL_NAME,
        )
        self.assertEqual(policy.generation, GENERATION)
        self.assertEqual(policy.custody_parent, CUSTODY_PARENT)
        self.assertEqual(policy.final_name, FINAL_NAME)
        self.assertEqual(policy.owner_lock_name, OWNER_LOCK_NAME)
        self.assertEqual(policy.journal_name, JOURNAL_NAME)
        self.assertEqual(dict(policy.artifact_paths), ARTIFACT_PATHS)
        self.assertEqual(policy.temporary_pattern, r"^\.v5_1_bt_r4\.disjoint-v2\.issuance\.[0-9a-f]{32}\.tmp$")
        self.assertTrue(policy.is_temporary_name(".v5_1_bt_r4.disjoint-v2.issuance." + "a" * 32 + ".tmp"))
        self.assertFalse(policy.is_temporary_name(".v5_1_bt_r4.disjoint-v2.issuance." + "A" * 32 + ".tmp"))

    def test_policy_rejects_wrong_omitted_old_and_noncanonical_values(self) -> None:
        from mlb_backtest.v52_r4_disjoint_policy import require_disjoint_policy

        valid = {
            "generation": GENERATION,
            "custody_parent": CUSTODY_PARENT,
            "final_name": FINAL_NAME,
        }
        for key, value in (
            ("generation", "v5.2-r4-physical-custody-v1"),
            ("generation", ""),
            ("custody_parent", Path("/tmp/custody-v2")),
            ("final_name", "v5_1_bt_r4/"),
            ("final_name", "v5_1_bt_r4/../v5_1_bt_r4"),
            ("final_name", "v5_1_bt_r4_old"),
        ):
            candidate = dict(valid)
            candidate[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(Exception):
                require_disjoint_policy(**candidate)
        for key in valid:
            candidate = dict(valid)
            del candidate[key]
            with self.subTest(omitted=key), self.assertRaises(TypeError):
                require_disjoint_policy(**candidate)
        from mlb_backtest.v52_r4_disjoint_policy import require_disjoint_cli_policy
        with self.assertRaises(Exception):
            require_disjoint_cli_policy(
                raw_generation=GENERATION,
                raw_parent=str(CUSTODY_PARENT) + "/",
                raw_final_name=FINAL_NAME,
            )

    def test_policy_is_capability_only_and_deeply_immutable(self) -> None:
        from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE
        from mlb_backtest.v52_r4_disjoint_policy import DisjointCustodyPolicy, TEMPORARY_PATTERN, _POLICY_TOKEN

        with self.assertRaises(CUSTODY_UNAVAILABLE):
            DisjointCustodyPolicy(GENERATION, CUSTODY_PARENT, FINAL_NAME, OWNER_LOCK_NAME, JOURNAL_NAME, ".", TEMPORARY_PATTERN, dict(ARTIFACT_PATHS))
        source = dict(ARTIFACT_PATHS)
        disposable = DisjointCustodyPolicy._from_verified(
            generation=GENERATION, custody_parent=CUSTODY_PARENT, final_name=FINAL_NAME,
            owner_lock_name=OWNER_LOCK_NAME, journal_name=JOURNAL_NAME, temporary_prefix=".",
            temporary_pattern=TEMPORARY_PATTERN, artifact_paths=source,
            _token=_POLICY_TOKEN,
        )
        source["inventory"] = PurePosixPath("mutated")
        self.assertEqual(disposable.artifact_paths["inventory"], ARTIFACT_PATHS["inventory"])
        with self.assertRaises(TypeError):
            disposable.artifact_paths["inventory"] = PurePosixPath("mutated")


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def _sha() -> str:
    return "a" * 64


def _identity() -> dict:
    return {
        "volume_uuid": "b" * 32,
        "device": 1,
        "inode": 2,
        "ctime_ns": 3,
        "file_type": "regular",
        "mode": 292,
        "flags": 0,
        "nlink": 1,
        "byte_size": 1,
        "sha256": _sha(),
    }


class DisjointSchemaTests(unittest.TestCase):
    def _valid_inventory(self) -> dict:
        identity = {"volume_uuid": "b" * 32, "device": 1, "inode": 2, "ctime_ns": 3, "file_type": "directory", "nlink": 1, "mode": 365, "flags": 0}
        member = {"path": "data/v5_1_bt/raw/a/b", "volume_uuid": "b" * 32, "device": 1, "inode": 2, "ctime_ns": 3, "file_type": "regular", "nlink": 1, "byte_size": 0, "sha256": _sha(), "mode": 292, "flags": 0}
        return {
            "schema_version": "v5.2-r4-disjoint-protected-raw-inventory-v1", "kind": "MLB_R4_DISJOINT_PROTECTED_RAW_INVENTORY", "custody_generation": GENERATION, "custody_parent_class": "LOCAL_APFS_NON_FILE_PROVIDER", "final_name": FINAL_NAME,
            "source_approval": {"logical_raw_root": "data/v5_1_bt/raw", "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json", "protected_inventory_sha256": "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6"},
            "operational_journal": {"format": "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1", "sha256": _sha(), "terminal_event_sha256": _sha()},
            "execution_binding": {"executable_sha256": _sha(), "executable_identity": {"volume_uuid": "b" * 32, "device": 1, "inode": 2, "ctime_ns": 3, "file_type": "regular", "mode": 493, "uid": 1, "nlink": 1, "byte_size": 1}, "repository_head": "a" * 40, "repository_tree": "b" * 40, "repository_clean": True, "implementation_blob": "c" * 40, "design_blob": "d" * 40, "plan_blob": "e" * 40, "source_approval_sha256": "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6", "target_volume_uuid": "b" * 32, "supersedes_incomplete_attempt_sha256": _sha()},
            "snapshot": {"volume_uuid": "b" * 32, "root_identity": identity, "manifest_count": 1675, "physical_member_count": 4711, "logical_binding_count": 5025, "root_locks_sha256": _sha()}, "manifest_count": 1675, "physical_member_count": 4711, "logical_binding_count": 5025, "rawstore_verification": "PASS",
            "records": [
                {"source": "baseball_savant_statcast", "canonical_query": {"start": "x", "end": "y", "game_type": "R", "url": "https://example.test/statcast"}, "rawstore_verified": True},
                {"source": "mlb_schedule", "canonical_query": {"start": "x", "end": "y", "gameType": "R", "role": "schedule", "url": "https://example.test/schedule"}, "rawstore_verified": True},
                {"source": "mlb_stats_pitcher_bf", "canonical_query": {"startDate": "x", "endDate": "y", "gameType": "R", "group": "pitcher", "stats": "statsSingleSeason", "url": "https://example.test/bf", "pitcher_id": 1, "sportIds": 1}, "rawstore_verified": True},
                {"source": "mlb_stats_pitching", "canonical_query": {"startDate": "x", "endDate": "y", "gameType": "R", "group": "pitching", "playerPool": "ALL", "stats": "season", "url": "https://example.test/pitching", "limit": 1, "sportIds": 1}, "rawstore_verified": True},
            ] + [{"source": "baseball_savant_statcast", "canonical_query": {"start": "x", "end": "y", "game_type": "R", "url": "https://example.test/statcast"}, "rawstore_verified": True}] * 1671,
            "members": [member] * 4711, "bindings": [{"record_index": 0, "role": "manifest", "path": "data/v5_1_bt/raw/a/b"}] * 5025,
            "root_locks": [{"path": ".baseball_savant_statcast.lock", "volume_uuid": "b" * 32, "device": 1, "inode": 3, "ctime_ns": 4, "file_type": "regular", "nlink": 1, "byte_size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "mode": 292, "flags": 0}, {"path": ".mlb_schedule.lock", "volume_uuid": "b" * 32, "device": 1, "inode": 4, "ctime_ns": 4, "file_type": "regular", "nlink": 1, "byte_size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "mode": 292, "flags": 0}, {"path": ".mlb_stats_pitcher_bf.lock", "volume_uuid": "b" * 32, "device": 1, "inode": 5, "ctime_ns": 4, "file_type": "regular", "nlink": 1, "byte_size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "mode": 292, "flags": 0}, {"path": ".mlb_stats_pitching.lock", "volume_uuid": "b" * 32, "device": 1, "inode": 6, "ctime_ns": 4, "file_type": "regular", "nlink": 1, "byte_size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "mode": 292, "flags": 0}],
            "directories": [{"path": ".", **identity}], "directory_manifest_sha256": _sha(),
        }

    def _valid_receipt(self) -> dict:
        obj = {"path": "data/v5_1_bt/raw/a/b", "source_sha256": _sha(), "snapshot_sha256": _sha(), "byte_size": 0}
        return {
            "schema_version": "v5.2-r4-disjoint-physical-custody-transition-receipt-v1", "kind": "MLB_R4_DISJOINT_PHYSICAL_CUSTODY_TRANSITION", "custody_generation": GENERATION, "final_name": FINAL_NAME,
            "inventory": {"path": str(ARTIFACT_PATHS["inventory"]), "sha256": _sha()}, "target_volume_uuid": "b" * 32,
            "comparison": {"leaf_count": 4715, "record_count": 1675, "physical_member_count": 4711, "logical_binding_count": 5025, "source_content_manifest_sha256": _sha(), "snapshot_content_manifest_sha256": _sha(), "directory_manifest_sha256": _sha(), "objects": [obj] * 4715},
            "operational_journal": {"format": "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1", "path": str(ARTIFACT_PATHS["journal"]), "sha256": _sha(), "terminal_event_sha256": _sha()}, "execution_binding_sha256": _sha(),
        }

    def test_all_v2_schema_documents_are_draft202012_and_closed(self) -> None:
        names = (
            "v5_2_r4_disjoint_custody_journal_record.schema.json",
            "v5_2_r4_disjoint_protected_raw_inventory.schema.json",
            "v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json",
            "v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json",
        )
        for name in names:
            with self.subTest(name=name):
                schema = _schema(name)
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertFalse(schema["additionalProperties"])
                Draft202012Validator.check_schema(schema)

    def test_journal_record_schema_is_closed_and_rejects_old_generation(self) -> None:
        schema = _schema("v5_2_r4_disjoint_custody_journal_record.schema.json")
        value = {
            "journal_version": "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1",
            "custody_generation": GENERATION,
            "sequence": 1,
            "event": "LOCK_ACQUIRE",
            "outcome": "INTENT",
            "previous_event_sha256": "0" * 64,
            "execution_binding_sha256": _sha(),
            "target_volume_uuid": "b" * 32,
            "detail": {"class": "LOCK"},
            "event_sha256": _sha(),
        }
        self.assertTrue(Draft202012Validator(schema).is_valid(value))
        for required in schema["required"]:
            self.assertFalse(
                Draft202012Validator(schema).is_valid({k: v for k, v in value.items() if k != required}),
                required,
            )
        for mutated in (
            {**value, "custody_generation": "v5.2-r4-physical-custody-v1"},
            {**value, "extra": True},
            {k: v for k, v in value.items() if k != "event_sha256"},
            {**value, "event_sha256": "A" * 64},
        ):
            with self.subTest(mutated=mutated):
                self.assertFalse(Draft202012Validator(schema).is_valid(mutated))

    def test_journal_detail_has_no_path_or_old_path(self) -> None:
        schema = _schema("v5_2_r4_disjoint_custody_journal_record.schema.json")
        value = {
            "journal_version": "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1",
            "custody_generation": GENERATION,
            "sequence": 1,
            "event": "LOCK_ACQUIRE",
            "outcome": "INTENT",
            "previous_event_sha256": "0" * 64,
            "execution_binding_sha256": _sha(),
            "target_volume_uuid": "b" * 32,
            "detail": {"class": "LOCK"},
            "event_sha256": _sha(),
        }
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(value))
        self.assertFalse(validator.is_valid({**value, "detail": {"class": "LOCK", "path": "old-r4"}}))

    def test_inventory_schema_has_fixed_counts_paths_and_closed_objects(self) -> None:
        schema = _schema("v5_2_r4_disjoint_protected_raw_inventory.schema.json")
        self.assertEqual(
            schema["properties"]["custody_generation"]["const"], GENERATION
        )
        self.assertEqual(schema["properties"]["final_name"]["const"], FINAL_NAME)
        self.assertEqual(
            schema["$defs"]["operationalJournal"]["properties"]["format"]["const"],
            "MLB_R4_DISJOINT_CUSTODY_JOURNAL_V1",
        )
        for defs in schema.get("$defs", {}).values():
            if isinstance(defs, dict) and defs.get("type") == "object":
                self.assertFalse(defs.get("additionalProperties", True))
        self.assertEqual(
            schema["properties"]["manifest_count"]["const"], 1675
        )
        self.assertEqual(
            schema["properties"]["physical_member_count"]["const"], 4711
        )
        self.assertEqual(
            schema["properties"]["logical_binding_count"]["const"], 5025
        )

    def test_inventory_and_receipt_validate_instances_and_top_level_closure(self) -> None:
        cases = (
            ("v5_2_r4_disjoint_protected_raw_inventory.schema.json", self._valid_inventory(), "source_approval"),
            ("v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json", self._valid_receipt(), "comparison"),
        )
        for name, valid, nested_key in cases:
            with self.subTest(name=name):
                schema = _schema(name)
                validator = Draft202012Validator(schema)
                self.assertTrue(validator.is_valid(valid))
                for required in schema["required"]:
                    self.assertFalse(validator.is_valid({k: v for k, v in valid.items() if k != required}), required)
                self.assertFalse(validator.is_valid({**valid, "extra": True}))
                nested = dict(valid[nested_key])
                nested["extra"] = True
                self.assertFalse(validator.is_valid({**valid, nested_key: nested}))
                wrong = dict(valid)
                wrong["custody_generation"] = "v5.2-r4-physical-custody-v1"
                self.assertFalse(validator.is_valid(wrong))
                if "receipt" in name:
                    old_inventory = dict(valid["inventory"])
                    old_inventory["path"] = "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json"
                    errors = list(validator.iter_errors({**valid, "inventory": old_inventory}))
                    self.assertTrue(errors)
                    self.assertTrue(any(error.validator == "const" for error in errors))
                    old_journal = dict(valid["operational_journal"])
                    old_journal["path"] = "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/custody_issuance_journal_r4.ndjson"
                    errors = list(validator.iter_errors({**valid, "operational_journal": old_journal}))
                    self.assertTrue(errors)
                    self.assertTrue(any(error.validator == "const" for error in errors))
                    traversal = dict(valid)
                    traversal["comparison"] = dict(valid["comparison"])
                    traversal["comparison"]["objects"] = list(valid["comparison"]["objects"])
                    traversal["comparison"]["objects"][0] = dict(traversal["comparison"]["objects"][0], path="data/v5_1_bt/raw/../bad")
                    self.assertFalse(validator.is_valid(traversal))
                    for bad_path in ("data/v5_1_bt/raw/a\\b/c", "data/v5_1_bt/raw/./b", "data/v5_1_bt/raw/../b", "data/v5_1_bt/raw//b"):
                        candidate = dict(valid)
                        candidate["comparison"] = dict(valid["comparison"])
                        candidate["comparison"]["objects"] = list(valid["comparison"]["objects"])
                        candidate["comparison"]["objects"][0] = dict(candidate["comparison"]["objects"][0], path=bad_path)
                        self.assertFalse(validator.is_valid(candidate), bad_path)
                if "inventory" in name:
                    mismatch = dict(valid)
                    mismatch["records"] = list(valid["records"])
                    mismatch["records"][0] = {"source": "baseball_savant_statcast", "canonical_query": {"start": "x", "end": "y", "gameType": "R", "role": "bad", "url": "x"}, "rawstore_verified": True}
                    self.assertFalse(validator.is_valid(mismatch))
                    missing = dict(valid)
                    missing["records"] = list(valid["records"])
                    missing["records"][1] = {"source": "mlb_schedule", "canonical_query": {"start": "x", "end": "y", "gameType": "R", "role": "schedule"}, "rawstore_verified": True}
                    self.assertFalse(validator.is_valid(missing))
                    lock_as_member = dict(valid)
                    lock_as_member["root_locks"] = [valid["members"][0]] * 4
                    self.assertFalse(validator.is_valid(lock_as_member))
                    duplicate_lock = dict(valid)
                    duplicate_lock["root_locks"] = list(valid["root_locks"])
                    duplicate_lock["root_locks"][1] = dict(duplicate_lock["root_locks"][0], inode=99)
                    self.assertFalse(validator.is_valid(duplicate_lock))

    def test_receipt_and_anchor_schema_bind_v2_paths_and_reject_extra_nested_keys(self) -> None:
        for name in (
            "v5_2_r4_disjoint_physical_custody_transition_receipt.schema.json",
            "v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json",
        ):
            with self.subTest(name=name):
                schema = _schema(name)
                text = json.dumps(schema)
                self.assertIn(GENERATION, text)
                self.assertNotIn(
                    "physical_custody_transition_r4.json", text.replace("disjoint_v2", "")
                )
                for defs in schema.get("$defs", {}).values():
                    if isinstance(defs, dict) and defs.get("type") == "object":
                        self.assertFalse(defs.get("additionalProperties", True))

    def test_anchor_requires_source_approval_and_amendment(self) -> None:
        schema = _schema("v5_2_r4_disjoint_physical_custody_authority_anchor.schema.json")
        valid = {
            "schema_version": "v5.2-r4-disjoint-custody-authority-anchor-v1",
            "kind": "MLB_R4_DISJOINT_PHYSICAL_CUSTODY_AUTHORITY",
            "custody_generation": GENERATION,
            "key_id": "ed25519-sha256:" + "a" * 64,
            "artifacts": {
                "inventory": {"path": str(ARTIFACT_PATHS["inventory"]), "sha256": _sha()},
                "receipt": {"path": str(ARTIFACT_PATHS["receipt"]), "sha256": _sha()},
                "contract": {"path": str(ARTIFACT_PATHS["contract"]), "sha256": _sha()},
                "run_spec": {"path": "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json", "sha256": _sha()},
                "journal": {"path": str(ARTIFACT_PATHS["journal"]), "sha256": _sha()},
            },
            "execution_binding": {
                "executable_sha256": _sha(), "executable_identity": {"volume_uuid": "b" * 32, "device": 1, "inode": 2, "ctime_ns": 3, "file_type": "regular", "mode": 493, "uid": 1, "nlink": 1, "byte_size": 1},
                "repository_head": "a" * 40, "repository_tree": "b" * 40, "repository_clean": True, "implementation_blob": "c" * 40, "design_blob": "d" * 40, "plan_blob": "e" * 40, "source_approval_sha256": "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6", "target_volume_uuid": "b" * 32, "supersedes_incomplete_attempt_sha256": _sha(),
            },
            "supersedes_incomplete_attempt": {
                "old_custody_parent": "/Users/taejunoh/Documents/MLB Custody", "old_final_root": "/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4", "old_owner_lock": "/Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock", "observer_state": "OBSERVER_INCOMPLETE_LOCKED", "metadata_inconsistent": True,
                "final_root_identity": {"file_type": "directory", "volume_uuid": "b" * 32, "device": 1, "inode": 2, "ctime_ns": 3, "mode": 365, "flags": 0}, "owner_lock_identity": {"file_type": "regular", "volume_uuid": "b" * 32, "device": 1, "inode": 2, "ctime_ns": 3, "mode": 292, "flags": 0, "size": 0}, "observed_leaf_count": 1, "observed_directory_count": 1, "observed_content_closure_sha256": _sha(),
            },
            "source_approval": {"logical_raw_root": "data/v5_1_bt/raw", "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json", "protected_inventory_sha256": "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6"},
            "amendment": {"design": {"path": "docs/design.md", "sha256": _sha(), "git_blob_id": "a" * 40}, "implementation_plan": {"path": "docs/plan.md", "sha256": _sha(), "git_blob_id": "b" * 40}, "implementation": {"path": "mlb_backtest/v52_r4_disjoint_issuer.py", "sha256": _sha(), "git_blob_id": "c" * 40}},
        }
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(valid))
        for required in schema["required"]:
            self.assertFalse(validator.is_valid({k: v for k, v in valid.items() if k != required}), required)
        self.assertFalse(validator.is_valid({k: v for k, v in valid.items() if k != "source_approval"}))
        self.assertFalse(validator.is_valid({k: v for k, v in valid.items() if k != "amendment"}))
        mutated = dict(valid)
        mutated["custody_generation"] = "v5.2-r4-physical-custody-v1"
        self.assertFalse(validator.is_valid(mutated))
        for artifact, old_path in {
            "inventory": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json",
            "receipt": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json",
            "contract": "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json",
            "journal": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/custody_issuance_journal_r4.ndjson",
            "run_spec": "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r4.json",
        }.items():
            candidate = dict(valid)
            artifacts = {key: dict(value) for key, value in valid["artifacts"].items()}
            artifacts[artifact]["path"] = old_path
            candidate["artifacts"] = artifacts
            errors = list(validator.iter_errors(candidate))
            self.assertTrue(errors, artifact)
            self.assertTrue(any(error.validator == "const" for error in errors), artifact)
        extra_nested = dict(valid["artifacts"])
        extra_nested["extra"] = {"path": "bad", "sha256": _sha()}
        self.assertFalse(validator.is_valid({**valid, "artifacts": extra_nested}))
        self.assertFalse(validator.is_valid({**valid, "extra": True}))

        swapped = dict(valid)
        supersession = dict(valid["supersedes_incomplete_attempt"])
        supersession["final_root_identity"], supersession["owner_lock_identity"] = supersession["owner_lock_identity"], supersession["final_root_identity"]
        swapped["supersedes_incomplete_attempt"] = supersession
        self.assertFalse(validator.is_valid(swapped))
        missing_size = dict(valid)
        supersession = dict(valid["supersedes_incomplete_attempt"])
        owner = dict(supersession["owner_lock_identity"])
        del owner["size"]
        supersession["owner_lock_identity"] = owner
        missing_size["supersedes_incomplete_attempt"] = supersession
        self.assertFalse(validator.is_valid(missing_size))


if __name__ == "__main__":
    unittest.main()
