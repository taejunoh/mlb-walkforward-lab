from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"
QUERY_SIDECAR_SCHEMA = ROOT / "schemas/v5_2_query_sidecar.schema.json"


def _schedule_snapshot() -> dict[str, object]:
    game = {
        "game_pk": 9001,
        "season": 2024,
        "official_date": "2024-04-02",
        "scheduled_start_utc": "2024-04-02T17:05:00Z",
        "away_team_id": 10,
        "away_score": 2,
        "away_starting_hitter_ids": list(range(1001, 1010)),
        "actual_starter_ids": [101, 201],
        "boxscore_query_sidecar_sha256": "e" * 64,
        "home_team_id": 20,
        "home_score": 4,
        "home_starting_hitter_ids": list(range(2001, 2010)),
        "venue_id": 31,
        "scheduled_innings": 9,
        "completed_innings": 9,
        "terminal_status": "Final",
        "doubleheader_order": 1,
        "verification_status": "VERIFIED",
        "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
        "played": True,
        "neutral_site": False,
        "alternate_site": False,
        "source_game_sha256": "c" * 64,
        "schedule_query_sidecar_sha256": "d" * 64,
        "feature_history_cutoff": "2024-04-01",
        "winner_team_id": 20,
    }
    return {
        "schema_version": 1,
        "grain": "season_game_evidence_snapshot",
        "games": [game],
        "source_query_sidecar_sha256s": ["d" * 64],
    }


class V52HistorySourceTest(unittest.TestCase):
    def test_prior_history_binds_every_source_game_hash_and_d1_cutoff(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        snapshot = recompute_prior_history_sidecar(_schedule_snapshot())
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(snapshot["grain"], "verified_game_projection")
        self.assertEqual(snapshot["source_game_sha256s"], ["c" * 64])
        self.assertEqual(snapshot["games"][0]["history_cutoff"], "2024-04-01")
        self.assertRegex(snapshot["games"][0]["projection_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(snapshot, recompute_prior_history_sidecar(_schedule_snapshot()))

    def test_prior_history_rejects_missing_hash_non_d1_and_unverified_game(self) -> None:
        from mlb_backtest.v52_sources.history import recompute_prior_history_sidecar

        for mutation, message in (({"source_game_sha256": None}, "source game hash"), ({"feature_history_cutoff": "2024-04-02"}, "D-1"), ({"verification_status": "BLOCKED"}, "verified")):
            snapshot = copy.deepcopy(_schedule_snapshot())
            snapshot["games"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, message):
                recompute_prior_history_sidecar(snapshot)

    def test_season_snapshot_schema_is_closed_and_grain_specific(self) -> None:
        schema = json.loads(SCHEMA.read_text())
        self.assertIn("oneOf", schema)
        encoded = json.dumps(schema, sort_keys=True)
        self.assertNotIn('"row_count"', encoded)
        snapshot = _schedule_snapshot()
        snapshot["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(snapshot, schema)
        incomplete = _schedule_snapshot()
        del incomplete["games"][0]["actual_starter_ids"]
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(incomplete, schema)

    def test_dispatch_returns_task2_compatible_query_envelope(self) -> None:
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar
        from mlb_backtest.v52_evidence import LogicalQuery

        raw = (ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv").read_bytes()
        query = LogicalQuery(
            source="baseball_savant_statcast", grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            parameters={"all": "true", "type": "details", "player_type": "batter", "game_date_gt": "2024-04-01", "game_date_lt": "2024-04-30", "hfGT": "R|", "group_by": "name-year", "sort_col": "pitches", "player_event_sort": "api_p_release_speed", "sort_order": "desc", "min_pitches": "0"},
            requested_range={"year": 2024, "start_date": "2024-04-01", "end_date": "2024-04-30"},
            expected_media_type="text/csv", parser_version="v5.2.1",
            parent_query_id=None, run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        semantics = recompute_query_sidecar(query, raw)
        self.assertNotIn("semantics", semantics)
        from mlb_backtest.v52_source_contracts import query_sidecar

        published_shape = query_sidecar(raw, query, semantics)
        jsonschema.validate(
            published_shape, json.loads(QUERY_SIDECAR_SCHEMA.read_text())
        )
        self.assertEqual(published_shape["semantics"], semantics)
        self.assertEqual(
            published_shape["raw_sha256"], hashlib.sha256(raw).hexdigest()
        )


if __name__ == "__main__":
    unittest.main()
