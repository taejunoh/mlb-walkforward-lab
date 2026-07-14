from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
SCHEMA_PATH = ROOT / "schemas/v5_2_acquisition_contract.schema.json"
DESIGN_PATH = Path(
    "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
)
PLAN_PATH = Path(
    "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md"
)
AMENDMENT_PATH = Path(
    "docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md"
)
RUN_SPEC_PATH = Path("protocols/v5_2_qd/historical_walkforward_500.json")
BASELINE_INVENTORY_PATH = Path("protocols/v5_2_qd/v51_protected_inventory.json")
PROTECTED_RAW_INVENTORY_PATH = Path("audit/v5_1_bt/protected_raw_inventory.json")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_blob(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", f"HEAD:{path.as_posix()}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _walk_key_names(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_key_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_key_names(child)


def _assert_schema_closes_instance(
    test: unittest.TestCase,
    instance: object,
    schema: dict[str, object],
    path: str,
) -> None:
    if isinstance(instance, dict):
        test.assertEqual(schema.get("type"), "object", path)
        test.assertFalse(schema.get("additionalProperties"), path)
        properties = schema.get("properties")
        test.assertIsInstance(properties, dict, path)
        test.assertEqual(set(properties), set(instance), path)
        test.assertEqual(set(schema.get("required", [])), set(instance), path)
        for key, child in instance.items():
            _assert_schema_closes_instance(
                test, child, properties[key], f"{path}.{key}"
            )
    elif isinstance(instance, list):
        test.assertEqual(schema.get("type"), "array", path)
        items = schema.get("items")
        test.assertIsInstance(items, dict, path)
        for index, child in enumerate(instance):
            _assert_schema_closes_instance(
                test, child, items, f"{path}[{index}]"
            )


class V52AcquisitionContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(CONTRACT_PATH.is_file(), "acquisition contract is missing")
        self.assertTrue(SCHEMA_PATH.is_file(), "acquisition contract schema is missing")
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_acquisition_contract_binds_approved_design_plan_and_run_spec(self) -> None:
        bindings = self.contract["bindings"]
        self.assertEqual(
            bindings["approved_design"],
            {"path": DESIGN_PATH.as_posix(), "sha256": _sha256(ROOT / DESIGN_PATH)},
        )
        self.assertEqual(
            bindings["implementation_plan"],
            {
                "git_blob_id": _git_blob(PLAN_PATH),
                "path": PLAN_PATH.as_posix(),
                "working_tree_sha256": _sha256(ROOT / PLAN_PATH),
            },
        )
        self.assertEqual(
            bindings["run_spec"],
            {"path": RUN_SPEC_PATH.as_posix(), "sha256": _sha256(ROOT / RUN_SPEC_PATH)},
        )
        self.assertEqual(
            bindings["plan_a_amendment"],
            {
                "path": AMENDMENT_PATH.as_posix(),
                "sha256": _sha256(ROOT / AMENDMENT_PATH),
            },
        )
        protection = self.contract["legacy_protection"]
        self.assertEqual(
            protection["baseline_inventory_path"],
            BASELINE_INVENTORY_PATH.as_posix(),
        )
        self.assertEqual(
            protection["baseline_inventory_sha256"],
            _sha256(ROOT / BASELINE_INVENTORY_PATH),
        )
        self.assertEqual(
            protection["protected_inventory_path"],
            PROTECTED_RAW_INVENTORY_PATH.as_posix(),
        )
        self.assertEqual(
            protection["protected_inventory_sha256"],
            _sha256(ROOT / PROTECTED_RAW_INVENTORY_PATH),
        )
        for key, value in {
            "design": bindings["approved_design"]["sha256"],
            "plan": bindings["implementation_plan"]["working_tree_sha256"],
            "amendment": bindings["plan_a_amendment"]["sha256"],
            "run_spec": bindings["run_spec"]["sha256"],
            "baseline_inventory": protection["baseline_inventory_sha256"],
            "protected_inventory": protection["protected_inventory_sha256"],
        }.items():
            self.assertRegex(value, SHA256, key)
        self.assertRegex(bindings["implementation_plan"]["git_blob_id"], GIT_OBJECT_ID)

    def test_contract_uses_canonical_raw_root(self) -> None:
        self.assertEqual(
            self.contract["canonical_roots"],
            {
                "acquisition_audit": "audit/v5_2_qd/historical/acquisition",
                "execution_audit": "audit/v5_2_qd/historical/execution",
                "raw": "data/v5_2_qd/historical/raw",
                "sources": "data/v5_2_qd/historical/sources",
            },
        )
        self.assertEqual(
            self.contract["legacy_protection"]["legacy_root"],
            "data/v5_1_bt/raw",
        )

    def test_contract_covers_2018_through_2024_and_forbids_2025(self) -> None:
        self.assertEqual(self.contract["required_years"], list(range(2018, 2025)))
        self.assertEqual(self.contract["forbidden_years"], [2025])
        self.assertEqual(
            self.contract["legacy_protection"]["permitted_years"],
            [2022, 2023, 2024],
        )
        boundaries = self.contract["season_boundaries"]
        self.assertEqual([item["year"] for item in boundaries], list(range(2018, 2025)))
        self.assertEqual(
            boundaries,
            [
                {"end_date": "2018-09-30", "start_date": "2018-03-29", "year": 2018},
                {"end_date": "2019-09-29", "start_date": "2019-03-20", "year": 2019},
                {"end_date": "2020-09-27", "start_date": "2020-07-23", "year": 2020},
                {"end_date": "2021-10-03", "start_date": "2021-04-01", "year": 2021},
                {"end_date": "2022-10-05", "start_date": "2022-04-07", "year": 2022},
                {"end_date": "2023-10-01", "start_date": "2023-03-30", "year": 2023},
                {"end_date": "2024-09-30", "start_date": "2024-03-20", "year": 2024},
            ],
        )

    def test_contract_freezes_sources_concurrency_and_storage_reserve(self) -> None:
        sources = self.contract["sources"]
        self.assertEqual(
            [source["name"] for source in sources],
            [
                "baseball_savant_statcast",
                "mlb_schedule_game_feed",
                "mlb_stats_pitcher_bf",
                "mlb_stats_pitching",
                "prior_schedule_history",
                "venue_team_timezone_table",
            ],
        )
        for source in sources:
            self.assertTrue(source["grain"])
            self.assertTrue(source["endpoints"])
            self.assertRegex(source["parser_version"], r"^v5\.2\.[0-9]+$")
        self.assertEqual(
            self.contract["host_concurrency"],
            {"baseballsavant.mlb.com": 1, "statsapi.mlb.com": 2},
        )
        self.assertEqual(
            self.contract["minimum_free_space_reserve_bytes"], 20 * 1024**3
        )

    def test_contract_v2_freezes_timezone_path_transport_caps_and_completeness(self) -> None:
        self.assertEqual(self.contract["schema_version"], 2)
        self.assertEqual(
            self.contract["timezone_mapping_path"],
            "config/v5_2_qd/venue_timezones.json",
        )
        self.assertNotIn(
            "config/v5_2_qd_venue_timezones.json",
            json.dumps(self.contract, sort_keys=True),
        )
        self.assertEqual(
            self.contract["response_byte_limits"],
            {
                "boxscore": 16_777_216,
                "career_bf": 16_777_216,
                "league_pitching": 16_777_216,
                "schedule": 134_217_728,
                "statcast": 268_435_456,
                "team": 16_777_216,
                "venue": 16_777_216,
            },
        )
        self.assertEqual(
            self.contract["response_completeness_limits"],
            {
                "league_pitching_split_limit": 5000,
                "statcast_data_row_limit": 25000,
            },
        )
        source = next(
            item
            for item in self.contract["sources"]
            if item["name"] == "mlb_stats_pitcher_bf"
        )
        self.assertEqual(
            source["endpoints"],
            ["https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"],
        )

    def test_contract_rejects_unknown_keys_and_contains_no_generated_query_ids(self) -> None:
        self.assertEqual(self.schema["const"], self.contract)
        _assert_schema_closes_instance(self, self.contract, self.schema, "contract")
        unexpected = copy.deepcopy(self.contract)
        unexpected["unexpected"] = True
        self.assertNotEqual(self.schema["const"], unexpected)
        nested = copy.deepcopy(self.contract)
        nested["bindings"]["approved_design"]["unexpected"] = True
        self.assertNotEqual(self.schema["const"], nested)
        self.assertFalse(
            any("query_id" in key for key in _walk_key_names(self.contract)),
            "contract must be hashed before generated query IDs exist",
        )


if __name__ == "__main__":
    unittest.main()
