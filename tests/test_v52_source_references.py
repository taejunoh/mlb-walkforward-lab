from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import jsonschema

from mlb_backtest.v52_evidence import LogicalQuery


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"


def _inputs() -> tuple[list[bytes], list[bytes], list[dict[str, object]], list[dict[str, object]]]:
    return (
        [(FIXTURES / "team_mini.json").read_bytes()],
        [(FIXTURES / "venues_mini.json").read_bytes()],
        [
            {"venue_id": 31, "effective_start": "2018-01-01", "effective_end": "2024-12-31", "time_zone": "America/New_York"},
            {"venue_id": 99, "effective_start": "2024-01-01", "effective_end": "2024-12-31", "time_zone": "America/Los_Angeles"},
        ],
        [
            {"game_pk": 9001, "official_date": "2024-04-02", "away_team_id": 10, "home_team_id": 20, "venue_id": 31, "neutral_site": False, "alternate_site": False},
            {"game_pk": 9002, "official_date": "2024-04-02", "away_team_id": 10, "home_team_id": 20, "venue_id": 99, "neutral_site": True, "alternate_site": True},
        ],
    )


class V52ReferenceSourceTest(unittest.TestCase):
    def test_reference_snapshot_resolves_every_team_and_observed_venue_date(self) -> None:
        from mlb_backtest.v52_sources.references import recompute_reference_sidecar

        snapshot = recompute_reference_sidecar(*_inputs())
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(snapshot["grain"], "effective_dated_venue_team_reference")
        self.assertEqual([row["team_id"] for row in snapshot["teams"]], [10, 20])
        self.assertEqual([row["venue_id"] for row in snapshot["venues"]], [31, 99])
        self.assertEqual(len(snapshot["observed_venue_date_mappings"]), 2)
        self.assertTrue(snapshot["observed_venue_date_mappings"][1]["neutral_site"])
        self.assertTrue(snapshot["observed_venue_date_mappings"][1]["alternate_site"])
        self.assertNotIn("row_count", snapshot)
        self.assertNotIn("game_pks", snapshot)

    def test_reference_rejects_invalid_zone_overlap_missing_mapping_and_team(self) -> None:
        from mlb_backtest.v52_sources.references import recompute_reference_sidecar

        teams, venues, rows, schedules = _inputs()
        bad_zone = copy.deepcopy(rows)
        bad_zone[0]["time_zone"] = "Mars/Baseball"
        overlap = [*copy.deepcopy(rows), {"venue_id": 31, "effective_start": "2024-04-02", "effective_end": "2024-04-02", "time_zone": "America/New_York"}]
        for bad, message in ((bad_zone, "IANA"), (overlap, "overlap"), (rows[:-1], "exactly one")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                recompute_reference_sidecar(teams, venues, bad, schedules)
        missing_team = copy.deepcopy(schedules)
        missing_team[0]["away_team_id"] = 999
        with self.assertRaisesRegex(ValueError, "team"):
            recompute_reference_sidecar(teams, venues, rows, missing_team)
        missing_site_flag = copy.deepcopy(schedules)
        missing_site_flag[0].pop("neutral_site")
        with self.assertRaisesRegex(ValueError, "neutral_site"):
            recompute_reference_sidecar(teams, venues, rows, missing_site_flag)

    def test_reference_rejects_identity_coordinate_and_date_range_conflicts(self) -> None:
        from mlb_backtest.v52_sources.references import recompute_reference_sidecar

        teams, venues, rows, schedules = _inputs()
        conflicting = json.loads(venues[0])
        conflicting["venues"].append(copy.deepcopy(conflicting["venues"][0]))
        conflicting["venues"][-1]["name"] = "Wrong Park"
        with self.assertRaisesRegex(ValueError, "conflicting venue"):
            recompute_reference_sidecar(teams, [json.dumps(conflicting).encode()], rows, schedules)
        bad_coordinates = json.loads(venues[0])
        bad_coordinates["venues"][0]["location"]["defaultCoordinates"]["latitude"] = 91
        with self.assertRaisesRegex(ValueError, "latitude"):
            recompute_reference_sidecar(teams, [json.dumps(bad_coordinates).encode()], rows, schedules)
        reversed_rows = copy.deepcopy(rows)
        reversed_rows[0]["effective_start"], reversed_rows[0]["effective_end"] = reversed_rows[0]["effective_end"], reversed_rows[0]["effective_start"]
        with self.assertRaisesRegex(ValueError, "effective"):
            recompute_reference_sidecar(teams, venues, reversed_rows, schedules)

    def test_reference_rejects_empty_population_at_runtime_and_schema(self) -> None:
        from mlb_backtest.v52_sources.references import recompute_reference_sidecar

        with self.assertRaisesRegex(ValueError, "nonempty"):
            recompute_reference_sidecar([], [], [], [])
        empty = {
            "grain": "effective_dated_venue_team_reference",
            "observed_venue_date_mappings": [],
            "schema_version": 1,
            "team_payload_sha256s": [],
            "teams": [],
            "timezone_rows": [],
            "venue_payload_sha256s": [],
            "venues": [],
        }
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(empty, json.loads(SCHEMA.read_text()))

    def test_query_dispatch_seals_team_and_venue_responses_without_generic_game_fields(self) -> None:
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        common = {
            "source": "venue_team_timezone_table",
            "grain": "effective_dated_venue_team_reference",
            "expected_media_type": "application/json",
            "parser_version": "v5.2.1",
            "parent_query_id": None,
            "run_spec_sha256": "a" * 64,
            "acquisition_contract_sha256": "b" * 64,
        }
        team_query = LogicalQuery(
            **common,
            endpoint="https://statsapi.mlb.com/api/v1/teams",
            parameters={"sportId": 1, "season": 2024, "hydrate": "venue(location)"},
            requested_range={"season": 2024, "entity": "teams"},
        )
        venue_query = LogicalQuery(
            **common,
            endpoint="https://statsapi.mlb.com/api/v1/venues/31",
            parameters={"hydrate": "location"},
            requested_range={"venue_id": 31},
        )
        team_semantics = recompute_query_sidecar(
            team_query, (FIXTURES / "team_mini.json").read_bytes()
        )
        venue_payload = json.loads((FIXTURES / "venues_mini.json").read_bytes())
        venue_payload["venues"] = venue_payload["venues"][:1]
        venue_semantics = recompute_query_sidecar(
            venue_query, json.dumps(venue_payload).encode()
        )
        self.assertEqual(
            [row["team_id"] for row in team_semantics["teams"]],
            [10, 20],
        )
        self.assertEqual(
            venue_semantics["venues"][0]["venue_id"], 31
        )
        self.assertNotIn("game_pks", team_semantics)
        self.assertNotIn("row_count", venue_semantics)


if __name__ == "__main__":
    unittest.main()
