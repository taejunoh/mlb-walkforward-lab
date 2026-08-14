from __future__ import annotations

import json
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
R2_MAPPING = (
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/"
    "venue_timezones.json"
)
R2_ENDPOINT = f"repository://{R2_MAPPING}"
R3_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
R3_AUDIT_PREFIX = (
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/"
)
R3_RAW_PREFIX = (
    "data/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r3/raw/"
)
R3_RUN_SHA = "9b0a9dbbf9df8bd621ae4f74dfbe0854e601229cebe404c9bb72015724fdac38"
R3_CONTRACT_SHA = "1e9b72738f8e1599d6c54f388aa09f274e83ff8a9e899bb494121ec3e7e1fd9d"
R3_RUN_PATH = (
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
)
R3_CONTRACT_PATH = (
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
)


def _schema(name: str) -> dict[str, object]:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


class RecoveryR2SchemaMappingTests(unittest.TestCase):
    def test_timezone_schemas_have_a_closed_r3_authority_branch(self) -> None:
        expected_paths = (
            R3_AUDIT_PREFIX + "acquisition/venue_timezone_candidate.json",
            R3_AUDIT_PREFIX + "acquisition/venue_timezones.json",
            R3_AUDIT_PREFIX + "acquisition/timezone_reviews/semantic.json",
            R3_AUDIT_PREFIX + "acquisition/timezone_reviews/coverage.json",
        )
        for name in (
            "v5_2_venue_timezones.schema.json",
            "v5_2_timezone_candidate.schema.json",
            "v5_2_timezone_review_receipt.schema.json",
            "v5_2_timezone_seal_receipt.schema.json",
        ):
            with self.subTest(name=name):
                text = json.dumps(_schema(name), sort_keys=True)
                self.assertIn(R3_RUN_PATH, text)
                self.assertIn(R3_CONTRACT_PATH, text)
                self.assertIn(R3_RUN_SHA, text)
                self.assertIn(R3_CONTRACT_SHA, text)
        seal = json.dumps(
            _schema("v5_2_timezone_seal_receipt.schema.json"), sort_keys=True
        )
        for path in expected_paths:
            self.assertIn(path, seal)

    def test_acquisition_contract_r2_owns_mapping_and_repository_endpoint(self) -> None:
        branch = _schema("v5_2_acquisition_contract.schema.json")["oneOf"][2]
        self.assertEqual(branch["properties"]["timezone_mapping_path"]["const"], R2_MAPPING)
        source = branch["properties"]["sources"]["const"][-1]
        self.assertEqual(source["endpoints"][-1], R2_ENDPOINT)

    def test_acquisition_checkpoint_r2_rejects_shared_config_mapping(self) -> None:
        branch = _schema("v5_2_acquisition_checkpoint.schema.json")["oneOf"][2]
        path_any_of = branch["properties"]["stage_evidence_receipts"]["items"]["properties"]["path"]["anyOf"]
        self.assertNotIn({"pattern": r"^config/v5_2_qd/.+$"}, path_any_of)
        self.assertIn({"const": R2_MAPPING}, path_any_of)

    def test_acquisition_checkpoint_r3_has_its_own_closed_topology_branch(self) -> None:
        branch = _schema("v5_2_acquisition_checkpoint.schema.json")["oneOf"][3]
        properties = branch["properties"]
        self.assertEqual(properties["run_id"]["const"], R3_RUN_ID)
        self.assertEqual(
            properties["run_spec"]["properties"]["path"]["const"],
            "protocols/v5_2_qd/"
            "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json",
        )
        self.assertEqual(
            properties["acquisition_contract"]["properties"]["path"]["const"],
            "protocols/v5_2_qd/"
            "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json",
        )
        self.assertEqual(
            properties["recovery_disposition_records"]["minItems"], 4
        )
        self.assertEqual(
            properties["recovery_disposition_records"]["maxItems"], 4
        )
        self.assertEqual(
            properties["recovery_excluded_game_pks"]["prefixItems"],
            [{"const": 632924}],
        )
        self.assertNotIn(R2_MAPPING, json.dumps(branch, sort_keys=True))
        self.assertIn(R3_AUDIT_PREFIX, json.dumps(branch, sort_keys=True))
        self.assertIn(R3_RAW_PREFIX, json.dumps(branch, sort_keys=True))

    def test_schedule_inventory_r3_has_exact_missing_venue_branch(self) -> None:
        schema = _schema("v5_2_schedule_anchor_inventory.schema.json")
        branch = schema["oneOf"][3]
        properties = branch["properties"]
        self.assertEqual(properties["run_id"]["const"], R3_RUN_ID)
        self.assertEqual(
            properties["run_spec"]["properties"]["path"]["const"],
            "protocols/v5_2_qd/"
            "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json",
        )
        self.assertEqual(
            properties["acquisition_contract"]["properties"]["path"]["const"],
            "protocols/v5_2_qd/"
            "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json",
        )
        self.assertEqual(
            properties["schedule_conflict_dispositions"]["$ref"],
            "#/$defs/r3RecoveryDispositionBindings",
        )
        mapping = schema["$defs"]["r3RecoveryDispositionBindings"]
        self.assertEqual(
            mapping["properties"]["projection_query_ids"]["minItems"], 3
        )
        self.assertEqual(
            mapping["properties"]["projection_query_ids"]["maxItems"], 3
        )
        self.assertEqual(
            mapping["properties"]["record_game_pks"]["prefixItems"],
            [{"const": 565534}, {"const": 630882}, {"const": 633224}, {"const": 632924}],
        )
        self.assertEqual(
            mapping["properties"]["record_to_projection_indexes"]["prefixItems"],
            [{"const": 0}, {"const": 1}, {"const": 2}, {"const": 2}],
        )
        self.assertEqual(
            mapping["properties"]["excluded_game_pks"]["prefixItems"],
            [{"const": 632924}],
        )
        self.assertEqual(
            properties["anchors"]["items"]["$ref"],
            "#/$defs/sourceAnchor",
        )
        season_sources = properties["season_sources"]
        self.assertEqual(len(season_sources["prefixItems"]), 7)
        self.assertEqual(
            [
                item["$ref"]
                for item in season_sources["prefixItems"][1:4]
            ],
            [
                "#/$defs/r3RecoveryDerivedSeasonSource",
                "#/$defs/r3RecoveryDerivedSeasonSource",
                "#/$defs/r3MissingVenueDerivedSeasonSource",
            ],
        )

    def test_game_evidence_r2_binds_authority_owned_mapping(self) -> None:
        branch = _schema("v5_2_game_evidence_inventory.schema.json")["oneOf"][2]
        mapping = branch["properties"]["timezone_gate"]["properties"]["mapping"]
        self.assertEqual(mapping["allOf"][1]["properties"]["path"]["const"], R2_MAPPING)

    def test_timezone_seal_binds_authority_owned_mapping(self) -> None:
        schema = _schema("v5_2_timezone_seal_receipt.schema.json")
        recovery_conditional = next(
            branch
            for branch in schema["allOf"]
            if R2_MAPPING in json.dumps(branch, sort_keys=True)
        )
        historical_conditional = next(
            branch
            for branch in schema["allOf"]
            if branch.get("if", {}).get("not") == {"required": ["authority"]}
        )
        recovery_mapping = recovery_conditional["then"]["properties"]["mapping"]
        historical_mapping = historical_conditional["then"]["properties"]["mapping"]
        top_level = schema["properties"]["mapping"]
        self.assertEqual(
            recovery_mapping["properties"]["path"]["const"], R2_MAPPING
        )
        self.assertEqual(
            historical_mapping["properties"]["path"]["const"],
            "config/v5_2_qd/venue_timezones.json",
        )
        self.assertNotIn("const", top_level["properties"]["path"])

        common = {
            "candidate": {"path": "candidate.json", "sha256": "a" * 64},
            "mapping": {"path": "config/v5_2_qd/venue_timezones.json", "sha256": "b" * 64},
            "occurred_at_utc": "2026-07-29T00:00:00.000000Z",
            "receipt_kind": "TIMEZONE_MAPPING_SEAL",
            "reviews": [
                {"path": "semantic.json", "reviewer_identity": "semantic", "reviewer_role": "TIMEZONE_SEMANTIC_REVIEW", "sha256": "c" * 64},
                {"path": "coverage.json", "reviewer_identity": "coverage", "reviewer_role": "TIMEZONE_COVERAGE_REVIEW", "sha256": "d" * 64},
            ],
            "schema_version": 1,
            "venue_scope": [31],
        }
        jsonschema.validate(common, schema)
        recovery = {
            **common,
            "authority": {
                "acquisition_contract": {
                    "path": "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json",
                    "sha256": "e" * 64,
                },
                "run_spec": {
                    "path": "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json",
                    "sha256": "d90f588c9e6cea58287b36d79ab33bef5c06b40d48e07b053e3ee0a7ebb5785a",
                },
            },
        }
        recovery["candidate"] = {"path": "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/venue_timezone_candidate.json", "sha256": "a" * 64}
        recovery["mapping"] = {"path": R2_MAPPING, "sha256": "b" * 64}
        recovery["reviews"] = [
            {"path": "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/timezone_reviews/semantic.json", "reviewer_identity": "semantic", "reviewer_role": "TIMEZONE_SEMANTIC_REVIEW", "sha256": "c" * 64},
            {"path": "audit/v5_2_qd/historical_recovery_schedule_conflicts_r2/acquisition/timezone_reviews/coverage.json", "reviewer_identity": "coverage", "reviewer_role": "TIMEZONE_COVERAGE_REVIEW", "sha256": "d" * 64},
        ]
        jsonschema.validate(recovery, schema)


if __name__ == "__main__":
    unittest.main()
