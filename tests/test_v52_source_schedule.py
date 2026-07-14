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


def _schedule_payload() -> dict[str, object]:
    payload = json.loads((FIXTURES / "schedule_mini.json").read_bytes())
    payload["totalGames"] = 2
    payload["dates"][0]["totalGames"] = 2
    payload["dates"][0]["games"][0]["neutralSite"] = False
    payload["dates"][0]["games"][0]["isAlternateSite"] = False
    payload["dates"][0]["games"][1]["neutralSite"] = True
    payload["dates"][0]["games"][1]["isAlternateSite"] = True
    return payload


def _schedule_raw() -> bytes:
    return json.dumps(_schedule_payload(), separators=(",", ":"), sort_keys=True).encode()


def _box_query() -> LogicalQuery:
    return LogicalQuery(
        source="mlb_schedule_game_feed", grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/game/9001/boxscore",
        parameters={}, requested_range={"season": 2024, "game_pk": 9001, "official_date": "2024-04-02"},
        expected_media_type="application/json", parser_version="v5.2.1",
        parent_query_id=None, run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _boxscore_raw() -> bytes:
    payload = json.loads((FIXTURES / "boxscore_9001.json").read_bytes())
    payload["teams"]["away"]["teamStats"] = {"batting": {"runs": 2}}
    payload["teams"]["home"]["teamStats"] = {"batting": {"runs": 4}}
    payload["teams"]["away"]["players"]["ID1011"] = {
        "person": {"id": 1011, "fullName": "Away Substitute"},
        "battingOrder": "101",
    }
    payload["teams"]["home"]["players"]["ID2011"] = {
        "person": {"id": 2011, "fullName": "Home Substitute"},
        "battingOrder": "401",
    }
    payload["teams"]["home"]["players"]["ID2012"] = {
        "person": {"id": 2012, "fullName": "Home Substitute 2"},
        "battingOrder": "902",
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _artifact(sidecar: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(sidecar, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"
    return raw, hashlib.sha256(raw).hexdigest()


class V52ScheduleSourceTest(unittest.TestCase):
    def test_schedule_anchor_seals_complete_population_without_boxscores(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        raw = _schedule_raw()
        sidecar = recompute_schedule_anchor_sidecar(raw, _query())
        self.assertEqual(sidecar["query_id"], logical_query_id(_query()))
        semantics = sidecar["semantics"]
        self.assertEqual(semantics["game_pks"], [9001, 9002])
        self.assertEqual(semantics["played_game_pks"], [9001])
        self.assertEqual(semantics["unplayed_game_pks"], [9002])
        self.assertEqual(semantics["unique_game_count"], 2)
        self.assertEqual(semantics["anchors"][1]["doubleheader_order"], 2)
        self.assertTrue(semantics["anchors"][1]["neutral_site"])
        self.assertTrue(semantics["anchors"][1]["alternate_site"])
        self.assertEqual(
            semantics["anchors"][0]["winner_team_id"], 20
        )
        requirements = semantics["child_query_requirements"]
        self.assertEqual(
            [row["game_pk"] for row in requirements["boxscores"]], [9001]
        )
        self.assertEqual([row["venue_id"] for row in requirements["venues"]], [31, 99])
        self.assertEqual(sidecar, recompute_schedule_anchor_sidecar(raw, _query()))

    def test_schedule_anchor_rejects_wrong_range_duplicate_and_conflict(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        payload = _schedule_payload()
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
                _schedule_raw(),
                wrong_query.__class__(**{**wrong_query.__dict__, "requested_range": {"year": 2023, "start_date": "2023-03-30", "end_date": "2023-10-01"}, "parameters": {**wrong_query.parameters, "startDate": "2023-03-30", "endDate": "2023-10-01"}}),
            )
        with self.assertRaisesRegex(ValueError, "no regular-season games"):
            recompute_schedule_anchor_sidecar(b'{"totalGames":0,"dates":[]}', _query())
        with self.assertRaisesRegex(ValueError, "totalGames"):
            recompute_schedule_anchor_sidecar(
                (FIXTURES / "schedule_mini.json").read_bytes(), _query()
            )
        truncated = _schedule_payload()
        truncated["totalGames"] = 3
        with self.assertRaisesRegex(ValueError, "population"):
            recompute_schedule_anchor_sidecar(json.dumps(truncated).encode(), _query())
        for field in ("neutralSite", "isAlternateSite"):
            non_boolean_site = _schedule_payload()
            non_boolean_site["dates"][0]["games"][0][field] = 0
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                recompute_schedule_anchor_sidecar(
                    json.dumps(non_boolean_site).encode(), _query()
                )

    def test_schedule_anchor_defaults_omitted_official_site_flags_to_false(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_schedule_anchor_sidecar

        payload = _schedule_payload()
        ordinary = payload["dates"][0]["games"][0]
        ordinary.pop("neutralSite")
        ordinary.pop("isAlternateSite")
        sidecar = recompute_schedule_anchor_sidecar(
            json.dumps(payload).encode(), _query()
        )
        ordinary_anchor, special_anchor = sidecar["semantics"]["anchors"]
        self.assertFalse(ordinary_anchor["neutral_site"])
        self.assertFalse(ordinary_anchor["alternate_site"])
        self.assertTrue(special_anchor["neutral_site"])
        self.assertTrue(special_anchor["alternate_site"])

    def test_completed_early_rejects_tied_or_contradictory_winner_evidence(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_boxscore_sidecar,
            recompute_schedule_anchor_sidecar,
        )

        contradiction = _schedule_payload()
        game = contradiction["dates"][0]["games"][0]
        game["status"]["detailedState"] = "Completed Early"
        game["teams"]["away"]["score"] = 5
        game["teams"]["home"]["score"] = 4
        with self.assertRaisesRegex(ValueError, "winner"):
            recompute_schedule_anchor_sidecar(json.dumps(contradiction).encode(), _query())

        tied = json.loads(_boxscore_raw())
        tied["teams"]["away"]["teamStats"]["batting"]["runs"] = 4
        with self.assertRaisesRegex(ValueError, "winner"):
            recompute_boxscore_sidecar(json.dumps(tied).encode(), _box_query())

    def test_game_snapshot_joins_verified_anchors_and_final_participants(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(
            _schedule_raw(), _query()
        )
        from mlb_backtest.v52_sources.schedule import recompute_boxscore_sidecar
        boxscore_sidecar = recompute_boxscore_sidecar(_boxscore_raw(), _box_query())
        boxscore = _artifact(dict(boxscore_sidecar))
        snapshot = recompute_game_evidence_snapshot(
            [anchor], {9001: boxscore}
        )
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        played, unplayed = snapshot["games"]
        self.assertEqual(played["verification_status"], "VERIFIED")
        self.assertEqual(
            played["participant_provenance"], "RECONSTRUCTED_FINAL_PARTICIPANTS"
        )
        self.assertNotEqual(played["participant_provenance"], "T_MINUS_45")
        self.assertEqual(played["actual_starter_ids"], [101, 201])
        self.assertEqual(played["terminal_status"], "Final")
        self.assertEqual(played["completed_innings"], 9)
        self.assertEqual(len(played["away_starting_hitter_ids"]), 9)
        self.assertEqual(len(played["home_starting_hitter_ids"]), 9)
        self.assertEqual(
            unplayed["participant_provenance"], "HISTORICAL_NO_PARTICIPANTS"
        )
        self.assertTrue(unplayed["neutral_site"])
        self.assertRegex(played["schedule_query_sidecar_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(played["boxscore_query_sidecar_sha256"], boxscore[1])
        self.assertEqual(
            set(snapshot["source_query_sidecar_sha256s"]),
            {
                played["schedule_query_sidecar_sha256"],
                played["boxscore_query_sidecar_sha256"],
            },
        )

    def test_game_snapshot_rejects_missing_boxscore_and_identity_disagreement(self) -> None:
        from mlb_backtest.v52_sources.schedule import (
            recompute_game_evidence_snapshot,
            recompute_schedule_anchor_sidecar,
        )

        anchor = recompute_schedule_anchor_sidecar(
            _schedule_raw(), _query()
        )
        with self.assertRaisesRegex(ValueError, "missing boxscore"):
            recompute_game_evidence_snapshot([anchor], {})
        from mlb_backtest.v52_sources.schedule import recompute_boxscore_sidecar
        valid_sidecar = dict(recompute_boxscore_sidecar(_boxscore_raw(), _box_query()))
        valid_bytes, _ = _artifact(valid_sidecar)
        with self.assertRaisesRegex(ValueError, "verified hash"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: (valid_bytes, "0" * 64)},
            )
        no_newline = valid_bytes.removesuffix(b"\n")
        with self.assertRaisesRegex(ValueError, "lineage"):
            recompute_game_evidence_snapshot(
                [anchor],
                {9001: (no_newline, hashlib.sha256(no_newline).hexdigest())},
            )
        wrong_query = copy.deepcopy(valid_sidecar)
        wrong_query["query_id"] = "not-a-query-id"
        with self.assertRaisesRegex(ValueError, "lineage"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: _artifact(wrong_query)}
            )
        wrong_game = copy.deepcopy(valid_sidecar)
        wrong_game["semantics"]["game_pk"] = 9002
        with self.assertRaisesRegex(ValueError, "gamePk"):
            recompute_game_evidence_snapshot(
                [anchor], {9001: _artifact(wrong_game)}
            )
        boxscore = json.loads(_boxscore_raw())
        boxscore["teams"]["home"]["team"]["id"] = 21
        raw = json.dumps(boxscore).encode()
        artifact = _artifact(dict(recompute_boxscore_sidecar(raw, _box_query())))
        with self.assertRaisesRegex(ValueError, "disagrees"):
            recompute_game_evidence_snapshot([anchor], {9001: artifact})
        scores = json.loads(_boxscore_raw())
        scores["teams"]["home"]["teamStats"]["batting"]["runs"] = 3
        score_raw = json.dumps(scores).encode()
        score_artifact = _artifact(dict(recompute_boxscore_sidecar(score_raw, _box_query())))
        with self.assertRaisesRegex(ValueError, "terminal"):
            recompute_game_evidence_snapshot([anchor], {9001: score_artifact})

    def test_query_dispatch_seals_one_boxscore_response_independently(self) -> None:
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

        query = _box_query()
        semantics = recompute_query_sidecar(
            query, _boxscore_raw()
        )
        self.assertEqual(semantics["game_pk"], 9001)
        self.assertEqual(semantics["actual_starter_ids"], [101, 201])
        self.assertNotIn("terminal_status", semantics)
        self.assertNotIn("completed_innings", semantics)

    def test_official_boxscore_ignores_substitution_orders_and_rejects_bad_slots(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_boxscore_sidecar

        sidecar = recompute_boxscore_sidecar(_boxscore_raw(), _box_query())
        self.assertEqual(
            sidecar["semantics"]["away_starting_hitter_ids"],
            list(range(1001, 1010)),
        )
        payload = json.loads(_boxscore_raw())
        duplicate = copy.deepcopy(payload)
        duplicate["teams"]["away"]["players"]["ID1099"] = {
            "person": {"id": 1099},
            "battingOrder": "100",
        }
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            recompute_boxscore_sidecar(json.dumps(duplicate).encode(), _box_query())
        missing = copy.deepcopy(payload)
        missing["teams"]["away"]["players"]["ID1001"].pop("battingOrder")
        with self.assertRaisesRegex(ValueError, "nine unique"):
            recompute_boxscore_sidecar(json.dumps(missing).encode(), _box_query())

    def test_game_snapshot_rejects_empty_schedule_population(self) -> None:
        from mlb_backtest.v52_sources.schedule import recompute_game_evidence_snapshot

        with self.assertRaisesRegex(ValueError, "nonempty"):
            recompute_game_evidence_snapshot([], {})


if __name__ == "__main__":
    unittest.main()
