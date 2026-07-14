from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import jsonschema

from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"


def _query() -> LogicalQuery:
    return LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/schedule",
        parameters={
            "sportId": 1,
            "gameType": "R",
            "startDate": "2024-03-20",
            "endDate": "2024-09-30",
            "hydrate": "probablePitcher,lineups,venue,linescore",
        },
        requested_range={
            "year": 2024,
            "start_date": "2024-03-20",
            "end_date": "2024-09-30",
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


class V52ScheduleSourceTest(unittest.TestCase):
    def test_schedule_anchor_seals_complete_population_without_boxscores(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        raw = (FIXTURES / "schedule_mini.json").read_bytes()
        sidecar = recompute_schedule_anchor_sidecar(raw, _query())
        self.assertEqual(sidecar["query_id"], logical_query_id(_query()))
        semantics = sidecar["semantics"]
        self.assertEqual(semantics["game_pks"], [9001, 9002])
        self.assertEqual(semantics["played_game_pks"], [9001])
        self.assertEqual(semantics["unplayed_game_pks"], [9002])
        self.assertEqual(semantics["unique_game_count"], 2)
        self.assertEqual(semantics["anchors"][1]["doubleheader_order"], 2)
        self.assertEqual(
            semantics["anchors"][0]["winner_team_id"], 20
        )
        requirements = semantics["child_query_requirements"]
        self.assertEqual(
            [row["game_pk"] for row in requirements["boxscores"]], [9001, 9002]
        )
        self.assertEqual([row["venue_id"] for row in requirements["venues"]], [31, 99])
        self.assertEqual(sidecar, recompute_schedule_anchor_sidecar(raw, _query()))

    def test_schedule_anchor_rejects_wrong_range_duplicate_and_conflict(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        payload = json.loads((FIXTURES / "schedule_mini.json").read_bytes())
        duplicate = copy.deepcopy(payload)
        duplicate["dates"][0]["games"].append(copy.deepcopy(duplicate["dates"][0]["games"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            recompute_schedule_anchor_sidecar(json.dumps(duplicate).encode(), _query())
        conflict = copy.deepcopy(payload)
        extra = copy.deepcopy(conflict["dates"][0]["games"][0])
        extra["teams"]["home"]["team"]["id"] = 21
        conflict["dates"][0]["games"].append(extra)
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            recompute_schedule_anchor_sidecar(json.dumps(conflict).encode(), _query())
        with self.assertRaisesRegex(ValueError, "outside its query"):
            wrong_query = _query()
            recompute_schedule_anchor_sidecar(
                (FIXTURES / "schedule_mini.json").read_bytes(),
                wrong_query.__class__(**{**wrong_query.__dict__, "requested_range": {"year": 2023, "start_date": "2023-03-30", "end_date": "2023-10-01"}, "parameters": {**wrong_query.parameters, "startDate": "2023-03-30", "endDate": "2023-10-01"}}),
            )
        with self.assertRaisesRegex(ValueError, "no regular-season games"):
            recompute_schedule_anchor_sidecar(b'{"dates":[]}', _query())

    def test_game_snapshot_joins_verified_anchors_and_final_participants(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(
            (FIXTURES / "schedule_mini.json").read_bytes(), _query()
        )
        boxscore = (FIXTURES / "boxscore_9001.json").read_bytes()
        snapshot = recompute_game_evidence_snapshot(
            [anchor], {9001: (boxscore, hashlib.sha256(boxscore).hexdigest())}
        )
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        played, unplayed = snapshot["games"]
        self.assertEqual(played["verification_status"], "VERIFIED")
        self.assertEqual(
            played["participant_provenance"], "RECONSTRUCTED_FINAL_PARTICIPANTS"
        )
        self.assertNotEqual(played["participant_provenance"], "T_MINUS_45")
        self.assertEqual(played["actual_starter_ids"], [101, 201])
        self.assertEqual(len(played["away_starting_hitter_ids"]), 9)
        self.assertEqual(len(played["home_starting_hitter_ids"]), 9)
        self.assertEqual(
            unplayed["participant_provenance"], "HISTORICAL_NO_PARTICIPANTS"
        )
        unplayed_raw = b'{"teams":{}}'
        with_unplayed_response = recompute_game_evidence_snapshot(
            [anchor],
            {
                9001: (boxscore, hashlib.sha256(boxscore).hexdigest()),
                9002: (
                    unplayed_raw,
                    hashlib.sha256(unplayed_raw).hexdigest(),
                ),
            },
        )
        self.assertEqual(snapshot, with_unplayed_response)

    def test_game_snapshot_rejects_missing_boxscore_and_identity_disagreement(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(
            (FIXTURES / "schedule_mini.json").read_bytes(), _query()
        )
        with self.assertRaisesRegex(ValueError, "missing boxscore"):
            recompute_game_evidence_snapshot([anchor], {})
        with self.assertRaisesRegex(ValueError, "verified hash"):
            recompute_game_evidence_snapshot(
                [anchor],
                {
                    9001: (
                        (FIXTURES / "boxscore_9001.json").read_bytes(),
                        hashlib.sha256(
                            (FIXTURES / "boxscore_9001.json").read_bytes()
                        ).hexdigest(),
                    ),
                    9002: (b'{"teams":{}}', "0" * 64),
                },
            )
        boxscore = json.loads((FIXTURES / "boxscore_9001.json").read_bytes())
        boxscore["teams"]["home"]["team"]["id"] = 21
        raw = json.dumps(boxscore).encode()
        with self.assertRaisesRegex(ValueError, "disagrees"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: (raw, hashlib.sha256(raw).hexdigest())}
            )

    def test_query_dispatch_seals_one_boxscore_response_independently(self) -> None:
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        query = LogicalQuery(
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint="https://statsapi.mlb.com/api/v1/game/9001/boxscore",
            parameters={},
            requested_range={
                "season": 2024,
                "game_pk": 9001,
                "official_date": "2024-04-02",
            },
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        semantics = recompute_query_sidecar(
            query, (FIXTURES / "boxscore_9001.json").read_bytes()
        )
        self.assertEqual(semantics["game_pk"], 9001)
        self.assertEqual(semantics["actual_starter_ids"], [101, 201])


if __name__ == "__main__":
    unittest.main()
