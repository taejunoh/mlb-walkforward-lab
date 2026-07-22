from __future__ import annotations

import copy
import hashlib
import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id
import mlb_backtest.v52_duplicate_gamepk_disposition as disposition_module
from mlb_backtest.v52_duplicate_gamepk_disposition import (
    DISPOSITION_ENDPOINT,
    EXPECTED_OCCURRENCE_COUNTS,
    NORMALIZED_GAMES_SHA256,
    SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256,
    build_duplicate_disposition_authority,
    project_schedule_occurrences,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"
DISPOSITION_FIXTURES = ROOT / "tests/fixtures/v52_duplicate_gamepk_565534"


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


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _assert_contains_no_bytes(test: unittest.TestCase, value: object) -> None:
    if isinstance(value, bytes):
        test.fail("authority or projection retained ephemeral schedule bytes")
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_contains_no_bytes(test, item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_contains_no_bytes(test, item)
    elif hasattr(value, "__dict__"):
        _assert_contains_no_bytes(test, vars(value))


def _authenticated_filtered_view(payload: dict[str, object]) -> dict[str, object]:
    filtered = copy.deepcopy(payload)
    date = filtered["dates"][54]
    del date["games"][5]
    date["totalGames"] -= 1
    filtered["totalGames"] -= 1
    return filtered


def _mutate_565534_predicate(
    payload: dict[str, object], *, retained: bool, field: str
) -> None:
    date_index, game_index = (161, 12) if retained else (54, 5)
    game = payload["dates"][date_index]["games"][game_index]
    if field == "gamePk":
        game["gamePk"] = 565535
    elif field == "gameType":
        game["gameType"] = "S"
    elif field == "season":
        game["season"] = "2020"
    elif field == "officialDate":
        game["officialDate"] = "2019-05-20"
    elif field == "away_team_id":
        game["teams"]["away"]["team"]["id"] = 1
    elif field == "home_team_id":
        game["teams"]["home"]["team"]["id"] = 1
    elif field == "terminal_status":
        game["status"]["detailedState"] = "Scheduled"
    elif field == "away_score":
        game["teams"]["away"]["score"] = 6
    elif field == "home_score":
        game["teams"]["home"]["score"] = 4
    elif field == "winner_team_id":
        game["teams"]["away"]["isWinner"] = False
        game["teams"]["home"]["isWinner"] = True
    elif field == "gameDate":
        game["gameDate"] = "2019-09-07T00:16:00Z"
    elif field == "resumeDate":
        game["resumeDate"] = "2019-09-07T00:16:00Z"
    elif field == "resumeGameDate":
        game["resumeGameDate"] = "2019-09-07"
    elif field == "resumedFrom":
        game["resumedFrom"] = "2019-05-19T17:11:00Z"
    elif field == "resumedFromDate":
        game["resumedFromDate"] = "2019-05-20"
    elif field == "venue_id":
        game["venue"]["id"] = 1
    elif field == "calendar_event_id":
        game["calendarEventID"] = "wrong"
    elif field == "sha256":
        game["description"] = "tampered"
    else:
        raise AssertionError(f"unknown 565534 fixture predicate {field}")


class V52ScheduleSourceTest(unittest.TestCase):
    def test_565534_exact_pair_retains_only_resumed_record_and_binds_disposition(self) -> None:
        from mlb_backtest.source_parsers.schedule import parse_schedule

        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        pair = json.loads((DISPOSITION_FIXTURES / "exact_pair.json").read_text())
        self.assertEqual(authority.query.endpoint, DISPOSITION_ENDPOINT)
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            parse_schedule((ROOT / authority.old_raw_relative).read_bytes())
        self.assertEqual(
            authority.query.requested_range["suppressed_occurrence"]["sha256"],
            pair["first"]["sha256"],
        )
        self.assertEqual(
            authority.query.requested_range["retained_occurrence"]["sha256"],
            pair["retained"]["sha256"],
        )
        projection = project_schedule_occurrences(authority)
        self.assertEqual(projection.suppressed_position, (54, 5))
        self.assertEqual(projection.retained_position, (161, 12))
        retained = next(game for game in projection.normalized_games if game["game_pk"] == 565534)
        self.assertEqual(retained["scheduled_start_utc"], pair["retained"]["game_date"])
        self.assertEqual(retained["venue_id"], pair["retained"]["venue_id"])

    def test_565534_requires_both_complete_fingerprints_and_all_fixed_fields(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        raw_path = ROOT / authority.old_raw_relative
        payload = json.loads(raw_path.read_bytes())
        negative = json.loads(
            (DISPOSITION_FIXTURES / "negative_cases.json").read_text()
        )
        for retained, fixture_key in (
            (False, "changed_suppressed_predicates"),
            (True, "changed_retained_predicates"),
        ):
            for field in negative[fixture_key]:
                changed = copy.deepcopy(payload)
                _mutate_565534_predicate(
                    changed, retained=retained, field=field
                )
                with self.subTest(
                    retained=retained, field=field
                ), self.assertRaisesRegex(ValueError, "565534"):
                    authority.project_payload(changed)

    def test_565534_authority_query_is_json_compatible_and_bound_to_new_contract(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        self.assertEqual(
            logical_query_id(authority.query),
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095",
        )
        self.assertNotEqual(
            logical_query_id(authority.query),
            "a132f270531aa38ca38c7b053fcce2474c3692eca0fbcee2989a734646900fe7",
        )
        self.assertEqual(
            authority.query.acquisition_contract_sha256,
            "3ddf83fdec9a328f5005fe8f62f4f680f4ef81516acd1aef2f90845b11c11d58",
        )

    def test_2019_projection_passes_one_ephemeral_canonical_view_to_public_parser(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        raw = json.loads((ROOT / authority.old_raw_relative).read_bytes())
        expected_view = _authenticated_filtered_view(raw)
        expected_bytes = disposition_module.canonical_json_bytes(expected_view)
        parser_calls: list[tuple[bytes, list[dict]]] = []
        actual_parse_schedule = disposition_module.parse_schedule

        def observe(value: bytes) -> list[dict]:
            self.assertIsInstance(value, bytes)
            parsed = actual_parse_schedule(value)
            parser_calls.append((value, parsed))
            return parsed

        with patch.object(
            disposition_module, "parse_schedule", side_effect=observe
        ):
            projection = project_schedule_occurrences(authority)
        self.assertEqual(len(parser_calls), 1)
        observed_bytes, parsed = parser_calls[0]
        self.assertEqual(observed_bytes, expected_bytes)
        self.assertEqual(json.loads(observed_bytes), expected_view)
        self.assertEqual(_plain_json(projection.normalized_games), parsed)
        _assert_contains_no_bytes(self, authority)
        _assert_contains_no_bytes(self, projection)

    def test_2019_projection_rejects_reordered_public_parser_before_publication(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        forbidden_namespaces = tuple(
            ROOT / relative
            for relative in (
                "data/v5_2_qd/historical_superseding_565534",
                "audit/v5_2_qd/historical_superseding_565534",
                "reports/v5_2_qd/historical_superseding_565534",
            )
        )
        self.assertTrue(all(not path.exists() for path in forbidden_namespaces))
        actual_parse_schedule = disposition_module.parse_schedule

        def reorder(value: bytes) -> list[dict]:
            return list(reversed(actual_parse_schedule(value)))

        with patch.object(disposition_module, "parse_schedule", side_effect=reorder), self.assertRaisesRegex(
            ValueError, "unchanged parse_schedule"
        ):
            project_schedule_occurrences(authority)
        self.assertTrue(all(not path.exists() for path in forbidden_namespaces))

    def test_2019_projection_deep_freezes_nested_normalized_games(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        projection = project_schedule_occurrences(authority)
        game = next(
            row
            for row in projection.normalized_games
            if row["away_lineup"] and row["home_lineup"]
        )
        away_player = game["away_lineup"][0]
        away_key = next(iter(away_player))
        with self.assertRaises(TypeError):
            away_player[away_key] = "mutated"
        with self.assertRaises(AttributeError):
            game["home_lineup"].append({})

    def test_nonordinary_duplicate_gamepk_or_third_special_copy_fails_closed(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        payload = json.loads((ROOT / authority.old_raw_relative).read_bytes())
        third = copy.deepcopy(payload)
        third["dates"][161]["games"].append(copy.deepcopy(third["dates"][161]["games"][12]))
        third["dates"][161]["totalGames"] += 1
        third["totalGames"] += 1
        with self.assertRaisesRegex(ValueError, "565534"):
            authority.project_payload(third)
        irreconcilable = copy.deepcopy(payload)
        duplicate = copy.deepcopy(irreconcilable["dates"][0]["games"][0])
        duplicate["teams"]["home"]["team"]["id"] += 10000
        irreconcilable["dates"][1]["games"].append(duplicate)
        irreconcilable["dates"][1]["totalGames"] += 1
        irreconcilable["totalGames"] += 1
        with self.assertRaisesRegex(ValueError, "duplicate gamePk"):
            authority.project_payload(irreconcilable, expected_counts=None)
        negative = json.loads(
            (DISPOSITION_FIXTURES / "negative_cases.json").read_text()
        )
        for total in negative["rejected_projection_totals"]:
            wrong_total = copy.deepcopy(payload)
            wrong_total["totalGames"] = total
            with self.subTest(total=total), self.assertRaisesRegex(
                ValueError, "occurrence totals"
            ):
                authority.project_payload(wrong_total)

    def test_2019_projection_authenticates_2472_occurrences_42_duplicates_and_2430_unique(self) -> None:
        authority = build_duplicate_disposition_authority(
            ROOT,
            source_run_spec_path=ROOT / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
            acquisition_contract_path=ROOT / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
        )
        projection = project_schedule_occurrences(authority)
        self.assertEqual(dict(projection.occurrence_counts), EXPECTED_OCCURRENCE_COUNTS)
        self.assertEqual(len(projection.normalized_games), 2430)
        self.assertEqual(len(projection.selected_raw_game_object_sha256s), 2430)
        ordinary = json.loads(
            (DISPOSITION_FIXTURES / "ordinary_games.json").read_text()
        )
        self.assertEqual(
            projection.occurrence_counts[
                "ordinary_reconciled_duplicate_game_pks"
            ],
            ordinary["ordinary_pair_count_after_special_suppression"],
        )
        raw = json.loads((ROOT / authority.old_raw_relative).read_bytes())
        for representative in ordinary["representatives"]:
            positions = representative["positions"]
            self.assertTrue(
                all(
                    raw["dates"][date_index]["games"][game_index]["gamePk"]
                    == representative["game_pk"]
                    for date_index, game_index in positions
                )
            )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    _plain_json(projection.normalized_games),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            NORMALIZED_GAMES_SHA256,
        )
        selected = sorted(
            (dict(row) for row in projection.selected_raw_game_object_sha256s),
            key=lambda row: int(row["game_pk"]),
        )
        self.assertEqual(
            hashlib.sha256(
                json.dumps(
                    list(selected), separators=(",", ":"), sort_keys=True
                ).encode()
            ).hexdigest(),
            SELECTED_RAW_GAME_OBJECT_SHA256S_SHA256,
        )
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
