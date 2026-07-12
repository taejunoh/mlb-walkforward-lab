import json
import tempfile
import unittest
from pathlib import Path

from mlb_backtest.features.offense import derive_slot_weights
from mlb_backtest.manifest import RawStore
from mlb_backtest.prepare import (
    ParticipantBuild,
    ScheduleHistoryBuild,
    TeamCodeBuild,
    VerifiedScheduleEvidence,
    VerifiedStatcastEvidence,
    _aggregate_training_slot_rows,
    assert_pregame_history,
    build_actual_participants,
    build_pregame_history,
    build_source_manifest_index,
    derive_team_code_map,
    derive_training_slot_rows,
    load_verified_schedule_evidence,
    load_verified_statcast_evidence,
    schedule_history_rows,
)
from mlb_backtest.statcast import StatcastData, parse_statcast, statcast_url


ROOT = Path(__file__).parents[1]
STATCAST = ROOT / "tests" / "fixtures" / "statcast_mini.csv"


class PrepareBoundaryTest(unittest.TestCase):
    def test_same_day_and_target_game_history_is_rejected(self):
        parsed = parse_statcast(STATCAST)
        target = {"game_pk": 2001, "official_date": "2024-04-03"}
        assert_pregame_history(target, parsed.pitches)
        same_day = list(parsed.pitches) + [
            dict(parsed.pitches[0], game_date="2024-04-03")
        ]
        with self.assertRaisesRegex(ValueError, "strictly before"):
            assert_pregame_history(target, same_day)
        target_game = list(parsed.pitches) + [
            dict(parsed.pitches[0], game_pk=2001)
        ]
        with self.assertRaisesRegex(ValueError, "target game_pk"):
            assert_pregame_history(target, target_game)

    def test_target_and_history_game_pks_require_positive_exact_integers(self):
        parsed = parse_statcast(STATCAST)
        for invalid in (True, False, "2001", 0, -1):
            with self.subTest(target_game_pk=invalid), self.assertRaisesRegex(
                ValueError, "target game_pk must be a positive integer"
            ):
                assert_pregame_history(
                    {"game_pk": invalid, "official_date": "2024-04-03"},
                    parsed.pitches,
                )

        target = {"game_pk": 2001, "official_date": "2024-04-03"}
        for invalid in (True, False, "1001", 0, -1):
            with self.subTest(history_game_pk=invalid), self.assertRaisesRegex(
                ValueError, "history game_pk must be a positive integer"
            ):
                assert_pregame_history(
                    target,
                    [dict(parsed.pitches[0], game_pk=invalid)],
                )

        with self.assertRaisesRegex(ValueError, "target game_pk"):
            assert_pregame_history(
                target,
                [dict(parsed.pitches[0], game_pk=2001)],
            )

    def test_source_manifest_index_is_sorted_and_hash_backed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            second = store.put("z_source", {"month": 2}, b"z\n")
            first = store.put("a_source", {"month": 1}, b"a\n")
            index = build_source_manifest_index(root)
            self.assertEqual(
                [row["source"] for row in index], ["a_source", "z_source"]
            )
            self.assertEqual(index[0]["sha256"], first.sha256)
            self.assertEqual(index[1]["sha256"], second.sha256)

    def test_source_manifest_index_rejects_symlinks_bad_names_and_duplicate_query(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            real = base / "real"
            RawStore(real).put("source", {"month": 1}, b"a\n")
            linked = base / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaises(ValueError):
                build_source_manifest_index(linked)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "raw"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (root / "source").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                build_source_manifest_index(root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = RawStore(root).put("source", {"month": 1}, b"a\n")
            bad = artifact.manifest_path.with_name("arbitrary.manifest.json")
            bad.write_bytes(artifact.manifest_path.read_bytes())
            with self.assertRaises(RuntimeError):
                build_source_manifest_index(root)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            store.put("source", {"month": 1}, b"a\n")
            store.put("source", {"month": 1}, b"different\n")
            with self.assertRaisesRegex(RuntimeError, "duplicate canonical"):
                build_source_manifest_index(root)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "raw"
            artifact = RawStore(root).put("source", {"month": 1}, b"a\n")
            outside = base / "outside.raw"
            outside.write_bytes(b"a\n")
            artifact.data_path.unlink()
            artifact.data_path.symlink_to(outside)
            with self.assertRaises(ValueError):
                build_source_manifest_index(root)

    def test_actual_participant_build_is_enriched_but_target_events_are_separate(self):
        schedule = [{
            "game_pk": 1,
            "official_date": "2025-05-01",
            "game_type": "R",
            "away_team": "Away",
            "home_team": "Home",
            "away_probable_pitcher_id": 901,
            "home_probable_pitcher_id": 902,
            "away_lineup": [{"player_id": i, "player_name": str(i)} for i in range(1, 10)],
            "home_lineup": [{"player_id": i, "player_name": str(i)} for i in range(11, 20)],
        }]
        pitches = (
            {"game_date": "2025-05-01", "game_pk": 1, "inning": 1, "inning_topbot": "Top", "at_bat_number": 1, "pitch_number": 1, "pitcher_id": 222, "batter_id": 1, "home_team": "HOM", "away_team": "AWY"},
            {"game_date": "2025-05-01", "game_pk": 1, "inning": 1, "inning_topbot": "Bot", "at_bat_number": 2, "pitch_number": 1, "pitcher_id": 111, "batter_id": 11, "home_team": "HOM", "away_team": "AWY"},
        )
        target = StatcastData(pitches, tuple(dict(row, events="single") for row in pitches))
        built = build_actual_participants(schedule, target)
        self.assertIsInstance(built, ParticipantBuild)
        self.assertEqual(len(built.games), 1)
        game = built.games[0]
        self.assertEqual(game["away_actual_starter_id"], 111)
        self.assertEqual(game["home_actual_starter_id"], 222)
        self.assertTrue(game["away_probable_starter_mismatch"])
        self.assertTrue(game["home_probable_starter_mismatch"])
        self.assertEqual(game["away_team_code"], "AWY")
        self.assertEqual(game["home_team_code"], "HOM")
        self.assertEqual(game["away_lineup"], schedule[0]["away_lineup"])
        self.assertNotIn("pitches", built.provenance)
        self.assertNotIn("plate_appearances", built.provenance)
        self.assertEqual(built.provenance["participant_game_count"], 1)
        self.assertRegex(built.provenance["participant_sha256"], r"^[0-9a-f]{64}$")
        wrong_day = StatcastData(tuple(dict(row, game_date="2025-05-02") for row in pitches), ())
        with self.assertRaisesRegex(ValueError, "date"):
            build_actual_participants(schedule, wrong_day)

    def test_team_code_map_is_exactly_30_and_season_scoped(self):
        games = []
        rows = []
        for index in range(15):
            game_pk = 100 + index
            away_number = index * 2
            home_number = away_number + 1
            games.append({
                "game_pk": game_pk,
                "official_date": "2024-06-01",
                "away_team": f"Team {away_number:02d}",
                "home_team": f"Team {home_number:02d}",
            })
            rows.append({
                "game_pk": game_pk,
                "game_date": "2024-06-01",
                "away_team": f"T{away_number:02d}",
                "home_team": f"T{home_number:02d}",
            })
        canceled = {
            "game_pk": 999,
            "official_date": "2024-06-02",
            "status": "Canceled",
            "away_team": "Canceled Away",
            "home_team": "Canceled Home",
        }
        built = derive_team_code_map(games + [canceled], rows, 2024)
        self.assertIsInstance(built, TeamCodeBuild)
        self.assertEqual(len(built.mapping), 30)
        self.assertEqual(built.mapping["Team 00"], "T00")
        self.assertEqual(built.mapping["Team 29"], "T29")
        self.assertEqual(built.unmatched_schedule_games[0]["game_pk"], 999)
        self.assertEqual(built.provenance["unmatched_game_count"], 1)
        self.assertRegex(built.provenance["mapping_sha256"], r"^[0-9a-f]{64}$")
        conflict = rows + [dict(rows[0], away_team="BAD")]
        with self.assertRaises(ValueError):
            derive_team_code_map(games, conflict, 2024)
        with self.assertRaises(ValueError):
            derive_team_code_map(games[:-1], rows[:-1], 2024)
        with self.assertRaisesRegex(ValueError, "season"):
            derive_team_code_map(
                games,
                [dict(row, game_date="2025-06-01") for row in rows],
                2024,
            )
        played_unmatched = dict(canceled, status="Final", away_score=1, home_score=2, away_is_winner=False, home_is_winner=True)
        with self.assertRaisesRegex(ValueError, "Final"):
            derive_team_code_map(games + [played_unmatched], rows, 2024)
        for unsafe in (
            dict(canceled, status="Scheduled"),
            dict(canceled, status="Final - Postponed"),
            dict(canceled, official_date="20240602"),
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                derive_team_code_map(games + [unsafe], rows, 2024)

    def test_training_slot_rows_count_official_starters_and_exclude_2025_and_pinch_hitters(self):
        def game(game_pk, year):
            return {
                "game_pk": game_pk,
                "official_date": f"{year}-04-01",
                "game_type": "R",
                "away_lineup": [{"player_id": game_pk * 100 + i, "player_name": str(i)} for i in range(1, 10)],
                "home_lineup": [{"player_id": game_pk * 100 + 10 + i, "player_name": str(i)} for i in range(1, 10)],
            }

        games = [game(22, 2022), game(23, 2023), game(24, 2024), game(25, 2025)]
        pas = []
        for scheduled in games:
            for side, half in (("away", "Top"), ("home", "Bot")):
                for slot, player in enumerate(scheduled[f"{side}_lineup"], 1):
                    for pa_number in range(10 - slot):
                        pas.append({
                            "game_pk": scheduled["game_pk"],
                            "game_date": scheduled["official_date"],
                            "game_type": "R",
                            "inning_topbot": half,
                            "batter_id": player["player_id"],
                            "at_bat_number": (0 if half == "Top" else 100) + slot * 10 + pa_number,
                            "event_id": f'{scheduled["game_pk"]}:{half}:{slot}:{pa_number}',
                        })
            pas.append(dict(pas[-1], batter_id=999999, at_bat_number=999, event_id=f'{scheduled["game_pk"]}:pinch'))
        with self.assertRaisesRegex(ValueError, "verified full-season"):
            derive_training_slot_rows(games, pas)
        rows = _aggregate_training_slot_rows(games, pas, full_season_mode=False)
        self.assertEqual({row["season"] for row in rows}, {2022, 2023, 2024})
        self.assertEqual(sum(row["pa"] for row in rows), 2 * 45 * 3)
        self.assertEqual(len(rows), 27)
        self.assertEqual(
            _aggregate_training_slot_rows(
                list(reversed(games)),
                list(reversed(pas)),
                full_season_mode=False,
            ),
            rows,
        )
        weights = derive_slot_weights(rows)
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertGreater(weights[1], weights[9])
        with self.assertRaisesRegex(ValueError, "duplicate plate appearance"):
            _aggregate_training_slot_rows(
                games, pas + [dict(pas[0])], full_season_mode=False
            )
        wrong_day = [dict(row, game_date="2024-12-31") if row["game_pk"] == 24 else row for row in pas]
        with self.assertRaisesRegex(ValueError, "date"):
            _aggregate_training_slot_rows(games, wrong_day, full_season_mode=False)
        missing_cell = [
            row
            for row in pas
            if not (
                row["game_pk"] == 22
                and row["batter_id"]
                in {games[0]["away_lineup"][8]["player_id"], games[0]["home_lineup"][8]["player_id"]}
            )
        ]
        with self.assertRaisesRegex(ValueError, "missing training slot"):
            _aggregate_training_slot_rows(
                games, missing_cell, full_season_mode=False
            )

    def test_verified_schedule_evidence_comes_only_from_rawstore_manifest(self):
        payload = (ROOT / "tests" / "fixtures" / "schedule_2025_05_01.json").read_bytes()
        query = {
            "role": "backtest",
            "start": "2025-05-01",
            "end": "2025-05-01",
            "gameType": "R",
            "url": "https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R&startDate=2025-05-01&endDate=2025-05-01&hydrate=probablePitcher%2Clineups%2Cvenue",
        }
        with tempfile.TemporaryDirectory() as td:
            store = RawStore(Path(td))
            artifact = store.put_unique_query("mlb_schedule", query, payload)
            evidence = load_verified_schedule_evidence(td, artifact.manifest_path)
            self.assertIsInstance(evidence, VerifiedScheduleEvidence)
            self.assertEqual(evidence.game_pks, (778100,))
            self.assertEqual(evidence.coverage_start, "2025-05-01")
            self.assertEqual(evidence.coverage_end, "2025-05-01")
            self.assertRegex(evidence.manifest_sha256, r"^[0-9a-f]{64}$")
            fake = artifact.manifest_path.with_name(f'{"a" * 64}.manifest.json')
            with self.assertRaises(ValueError):
                load_verified_schedule_evidence(td, fake)
            with self.assertRaises(TypeError):
                VerifiedScheduleEvidence()

    def test_verified_statcast_evidence_comes_only_from_rawstore_manifests(self):
        payload = STATCAST.read_bytes()
        query = {
            "start": "2024-04-01",
            "end": "2024-04-02",
            "game_type": "R",
            "url": statcast_url("2024-04-01", "2024-04-02"),
        }
        with tempfile.TemporaryDirectory() as td:
            artifact = RawStore(Path(td)).put_unique_query(
                "baseball_savant_statcast", query, payload
            )
            evidence = load_verified_statcast_evidence(td, [artifact.manifest_path])
            self.assertIsInstance(evidence, VerifiedStatcastEvidence)
            self.assertEqual(len(evidence.plate_appearances), 6)
            self.assertEqual(evidence.coverage_ranges, (("2024-04-01", "2024-04-02"),))
            with self.assertRaises(TypeError):
                VerifiedStatcastEvidence()

    def test_schedule_history_build_has_codes_markers_venue_and_coverage(self):
        games = [
            {
                "game_pk": 1,
                "official_date": "2024-04-01",
                "game_type": "R",
                "away_team_code": "AWY",
                "home_team_code": "HOM",
                "venue_id": 31,
                "venue_name": "PNC Park",
                "scheduled_innings": None,
                "doubleheader_marker": "GAME1",
            },
            {
                "game_pk": 2,
                "official_date": "2024-04-02",
                "game_type": "R",
                "away_team_code": "AWY",
                "home_team_code": "HOM",
                "venue_id": 31,
                "venue_name": "PNC Park",
                "scheduled_innings": 7,
                "doubleheader_marker": None,
            },
        ]
        venues = {31: {"venue_city": "Pittsburgh", "venue_latitude": 40.4469, "venue_longitude": -80.0057, "time_zone": "America/New_York"}}
        payload_games = []
        for game in games:
            payload_games.append({
                "gamePk": game["game_pk"],
                "gameType": "R",
                "officialDate": game["official_date"],
                "gameDate": f'{game["official_date"]}T17:00:00Z',
                "status": {"detailedState": "Scheduled"},
                "teams": {
                    "away": {"team": {"id": 1, "name": "Away"}},
                    "home": {"team": {"id": 2, "name": "Home"}},
                },
                "venue": {"id": 31, "name": "PNC Park"},
                **({"scheduledInnings": 7} if game["game_pk"] == 2 else {}),
                **({"doubleHeader": "Y", "gameNumber": 1} if game["game_pk"] == 1 else {}),
            })
        payload = json.dumps({"dates": [{"date": "2024-04-01", "games": payload_games}]}).encode()
        query = {
            "role": "training",
            "start": "2024-04-01",
            "end": "2024-04-02",
            "gameType": "R",
            "url": "https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R&startDate=2024-04-01&endDate=2024-04-02&hydrate=probablePitcher%2Clineups%2Cvenue",
        }
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        artifact = RawStore(Path(temporary.name)).put_unique_query(
            "mlb_schedule", query, payload
        )
        evidence = load_verified_schedule_evidence(
            temporary.name, artifact.manifest_path
        )
        evidence_games = [
            dict(game, away_team_code="AWY", home_team_code="HOM")
            for game in evidence.games
        ]
        built = schedule_history_rows(evidence_games, venues, evidence)
        self.assertIsInstance(built, ScheduleHistoryBuild)
        self.assertEqual(built.coverage_start, "2024-04-01")
        self.assertEqual(built.coverage_end, "2024-04-02")
        self.assertTrue(built.coverage_complete)
        self.assertEqual(built.source_manifest_sha256, evidence.manifest_sha256)
        self.assertEqual(built.expected_game_pks, (1, 2))
        self.assertEqual(built.rows[0]["team_codes"], ["AWY", "HOM"])
        self.assertEqual(built.rows[0]["home_team_code"], "HOM")
        self.assertEqual(built.rows[0]["innings"], 9)
        self.assertEqual(built.rows[1]["innings"], 7)
        self.assertEqual(built.rows[0]["venue_city"], "Pittsburgh")
        with self.assertRaises(ValueError):
            schedule_history_rows(evidence_games + [dict(evidence_games[0], game_pk=9)], venues, evidence)
        with self.assertRaisesRegex(ValueError, "verified schedule"):
            schedule_history_rows(evidence_games[:-1], venues, evidence)
        with self.assertRaisesRegex(ValueError, "verified schedule"):
            schedule_history_rows(
                [dict(evidence_games[0], official_date="2024-04-02"), evidence_games[1]],
                venues,
                evidence,
            )
        missing_null = dict(evidence_games[0])
        missing_null.pop("scheduled_innings")
        with self.assertRaisesRegex(ValueError, "verified schedule"):
            schedule_history_rows([missing_null, evidence_games[1]], venues, evidence)
        with self.assertRaisesRegex(ValueError, "verified schedule"):
            schedule_history_rows(
                [dict(evidence_games[0], forged=True), evidence_games[1]],
                venues,
                evidence,
            )

    def test_schedule_history_build_rejects_forged_complete_evidence(self):
        row = {
            "game_pk": 1,
            "official_date": "2024-04-01",
            "team_codes": ["AWY", "HOM"],
            "home_team_code": "HOM",
            "venue_name": "Park",
            "innings": 9,
        }
        invalid = (
            {"source_manifest_sha256": None, "expected_game_pks": (1,)},
            {"source_manifest_sha256": "a" * 64, "expected_game_pks": (2,)},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ScheduleHistoryBuild(
                    rows=(row,),
                    coverage_start="2024-04-01",
                    coverage_end="2024-04-02",
                    coverage_complete=True,
                    **changes,
                )

    def test_build_pregame_history_is_the_target_event_filtering_boundary(self):
        parsed = parse_statcast(STATCAST)
        target_game = {"game_pk": 1002, "official_date": "2024-04-02"}
        prior_target_pitch = dict(parsed.pitches[0], game_pk=1002, game_date="2024-04-01")
        prior_target_pa = dict(parsed.plate_appearances[0], game_pk=1002, game_date="2024-04-01")
        source = StatcastData(
            parsed.pitches + (prior_target_pitch,),
            parsed.plate_appearances + (prior_target_pa,),
        )
        schedule_build = ScheduleHistoryBuild(
            rows=(
                {
                    "game_pk": 1001,
                    "official_date": "2024-04-01",
                    "team_codes": ["AWY", "HOM"],
                    "home_team_code": "HOM",
                    "venue_name": "Park",
                    "innings": 9,
                },
            ),
            coverage_start="2024-04-01",
            coverage_end="2024-04-02",
            coverage_complete=True,
            source_manifest_sha256="b" * 64,
            expected_game_pks=(1001,),
        )
        history = build_pregame_history(
            source,
            target_game,
            {slot: 1 / 9 for slot in range(1, 10)},
            schedule_build,
        )
        self.assertEqual(history.pitch_game_pks, {1001})
        self.assertEqual(history.pa_game_pks, {1001})
        self.assertTrue(all(row["game_date"] < "2024-04-02" for row in history.pitches))
        self.assertNotIn(1002, history.pitch_game_pks)
        self.assertNotIn(1002, history.pa_game_pks)
        self.assertTrue(history.schedule_history.coverage_complete)


if __name__ == "__main__":
    unittest.main()
