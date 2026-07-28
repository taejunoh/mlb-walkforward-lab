import hashlib
import json
import math
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock
from mlb_backtest.feature_engine import (
    FeatureHistory,
    ScheduleHistory,
    actual_starter_index,
    build_raw_game_features,
    enrich_schedule_game_participants,
    pa_expected_woba,
)
from mlb_backtest.sources import parse_schedule
from mlb_backtest.scoring_prep import build_raw_features_by_date
from mlb_backtest.statcast import StatcastData, parse_statcast
from mlb_backtest.v52_sources.history import EXPECTED_NEUTRALIZATIONS


def pa(
    game_pk,
    day,
    pitcher,
    batter,
    event,
    *,
    hand="R",
    top="Top",
    inning=1,
    xwoba=None,
    woba=0.0,
    denom=1.0,
    lsa=None,
    outs=0,
    home="HOM",
    away="AWY",
    at_bat=1,
    pre_home=0,
    pre_away=0,
    post_home=0,
    post_away=0,
    speed=95.0,
):
    return {
        "game_date": day,
        "game_pk": game_pk,
        "pitcher_id": pitcher,
        "batter_id": batter,
        "events": event,
        "description": "terminal",
        "p_throws": hand,
        "home_team": home,
        "away_team": away,
        "inning": inning,
        "inning_topbot": top,
        "release_speed": speed,
        "woba_value": woba,
        "woba_denom": denom,
        "estimated_woba_using_speedangle": xwoba,
        "launch_speed_angle": lsa,
        "at_bat_number": at_bat,
        "pitch_number": 1,
        "home_score": pre_home,
        "away_score": pre_away,
        "post_home_score": post_home,
        "post_away_score": post_away,
        "outs_on_play": outs,
        "game_type": "R",
        "event_id": f"{game_pk}:{at_bat}",
    }


def data(rows):
    ordered = tuple(
        sorted(rows, key=lambda r: (r["game_date"], r["game_pk"], r["at_bat_number"]))
    )
    return StatcastData(ordered, ordered)


def mapping():
    result = {"Home": "HOM", "Away": "AWY"}
    result.update({f"Team {i}": f"T{i:02d}" for i in range(28)})
    return result


def schedule_neutralization_records() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "game_pk": game_pk,
            "official_date": official_date,
            "team_id": team_id,
            "target_raw_game_object_sha256": raw_sha256,
            "target_normalized_anchor_sha256": anchor_sha256,
            "value": 50.0,
            "sample_size": 0,
            "quality": 0.0,
            "reason_codes": ["schedule_neutral_duplicate_gamepk_dependency"],
        }
        for raw_sha256, anchor_sha256, game_pk, official_date, team_id
        in EXPECTED_NEUTRALIZATIONS
    )


def task10_schedule_rows() -> tuple[dict[str, object], ...]:
    return (
        {
            "game_pk": 565338,
            "official_date": "2019-05-20",
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY",
            "home_team_code": "HOM",
            "away_team_id": 133,
            "home_team_id": 114,
            "venue_name": "Park",
            "innings": 9,
        },
        {
            "game_pk": 565535,
            "official_date": "2019-05-21",
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY",
            "home_team_code": "HOM",
            "away_team_id": 146,
            "home_team_id": 116,
            "venue_name": "Park",
            "innings": 11,
        },
        {
            "game_pk": 566156,
            "official_date": "2019-09-06",
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY",
            "home_team_code": "HOM",
            "away_team_id": 116,
            "home_team_id": 133,
            "venue_name": "Park",
            "innings": 11,
        },
        {
            "game_pk": 566157,
            "official_date": "2019-09-07",
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY",
            "home_team_code": "HOM",
            "away_team_id": 116,
            "home_team_id": 133,
            "venue_name": "Park",
            "innings": 9,
        },
    )


def game(**changes):
    value = {
        "game_pk": 900,
        "official_date": "2025-05-01",
        "game_type": "R",
        "scheduled_start_utc": "2025-05-01T23:00:00Z",
        "status": "Final",
        "away_team": "Away",
        "home_team": "Home",
        "venue_id": 1,
        "venue_name": "Park",
        "away_probable_pitcher_id": 20,
        "home_probable_pitcher_id": 10,
        "away_actual_starter_id": 20,
        "home_actual_starter_id": 10,
        "starter_provenance": "RECONSTRUCTED_ACTUAL_STARTER",
        "away_lineup": [
            {"player_id": 200 + i, "player_name": str(i)} for i in range(1, 10)
        ],
        "home_lineup": [
            {"player_id": 100 + i, "player_name": str(i)} for i in range(1, 10)
        ],
        "doubleheader_marker": "GAME2",
    }
    value.update(changes)
    return value


def era_record(
    as_of="2025-04-30", *, value=4.0, earned_runs=4, outs=27, digest="a" * 64
):
    return {
        "as_of_date": as_of,
        "value": value,
        "earned_runs": earned_runs,
        "outs": outs,
        "source": "mlb_stats_api",
        "manifest_sha256": digest,
    }


def build(target, history, team_map=None, league_era=None):
    return build_raw_game_features(
        target,
        history,
        mapping() if team_map is None else team_map,
        league_era_by_asof={"2025-04-30": era_record()}
        if league_era is None
        else league_era,
    )


def task10_feature_history():
    return FeatureHistory(
        data([]),
        slot_weights={i: 1 / 9 for i in range(1, 10)},
        schedule_history=task10_schedule_rows(),
        schedule_coverage_start="2019-05-01",
        schedule_coverage_end="2019-09-30",
        schedule_coverage_complete=True,
        schedule_neutralizations=schedule_neutralization_records(),
    )


def task10_game(
    game_pk: int,
    official_date: str,
    away_team_id: int,
    home_team_id: int,
    **changes,
):
    return game(
        game_pk=game_pk,
        official_date=official_date,
        away_team_code="AWY",
        home_team_code="HOM",
        away_team_id=away_team_id,
        home_team_id=home_team_id,
        doubleheader_marker=None,
        doubleheader=False,
        **changes,
    )


def build_task10(target, history):
    target_day = date.fromisoformat(target["official_date"])
    as_of = (target_day - timedelta(days=1)).isoformat()
    return build(target, history, league_era={as_of: era_record(as_of)})


class FeatureEngineTest(unittest.TestCase):
    def test_schedule_history_rejects_duck_typed_capability(self):
        expected = {
            "game_pk": 633224,
            "official_date": "2021-07-21",
            "scheduled_start_utc": "2021-09-25T00:10:00Z",
            "schedule_query_sidecar_sha256": "a" * 64,
            "played_date": "2021-09-24",
            "result_available_on": "2021-09-24",
        }

        class StrictCapability:
            def require_boundary(self, candidate):
                self.assertEqual(dict(candidate), row)
                return date(2021, 9, 24)

        capability = StrictCapability()
        capability.assertEqual = self.assertEqual
        row = {
            **expected,
            "team_codes": ["AWY", "HOM"],
            "away_team_code": "AWY", "home_team_code": "HOM",
            "away_team_id": 1, "home_team_id": 2,
            "venue_name": "Park", "innings": 9,
        }
        with self.assertRaisesRegex(ValueError, "availability capability"):
            ScheduleHistory(
                [row], coverage_start="2021-04-01", coverage_end="2021-10-03",
                coverage_complete=True, recovery_dispositions=capability,
            )

    def test_schedule_history_rejects_public_availability_fields(self):
        with self.assertRaisesRegex(ValueError, "unapproved schedule availability"):
            ScheduleHistory(
                [{
                    "game_pk": 9001, "official_date": "2021-07-21",
                    "played_date": "2021-07-21", "result_available_on": "2021-07-21",
                }],
                coverage_start="2021-04-01", coverage_end="2021-10-03",
                coverage_complete=True,
            )

    def test_schedule_history_indexes_resumed_result_on_availability_date(self):
        from tests.v52_recovery_schedule_conflict_fixture import published_recovery_capability

        with published_recovery_capability() as capability:
            entry = next(item for item in capability.entries if item.game_pk == 630882)
            row = {
                "game_pk": 630882, "official_date": "2020-08-09",
                "scheduled_start_utc": entry.retained_scheduled_start_utc,
                "schedule_query_sidecar_sha256": entry.query_sidecar["sha256"],
                "played_date": entry.played_date,
                "result_available_on": entry.result_available_on,
                "team_codes": ["AWY", "HOM"], "away_team_code": "AWY",
                "home_team_code": "HOM", "away_team_id": 1, "home_team_id": 2,
                "venue_name": "Park", "innings": 9,
            }
            history = ScheduleHistory(
                [row], coverage_start="2020-07-23", coverage_end="2020-09-27",
                coverage_complete=True, recovery_dispositions=capability,
            )
            self.assertIsNone(history.max_before(date(2020, 8, 14)))
            self.assertEqual(history.max_before(date(2020, 8, 15)), date(2020, 8, 14))
            resumed = next(item for item in capability.entries if item.game_pk == 633224)
            resumed_history = ScheduleHistory(
                [{
                    **row,
                    "game_pk": 633224,
                    "official_date": "2021-07-21",
                    "scheduled_start_utc": resumed.retained_scheduled_start_utc,
                    "schedule_query_sidecar_sha256": resumed.query_sidecar["sha256"],
                    "played_date": resumed.played_date,
                    "result_available_on": resumed.result_available_on,
                }],
                coverage_start="2021-04-01", coverage_end="2021-10-03",
                coverage_complete=True, recovery_dispositions=capability,
            )
            self.assertIsNone(resumed_history.max_before(date(2021, 9, 24)))
            self.assertEqual(
                resumed_history.max_before(date(2021, 9, 25)), date(2021, 9, 24)
            )

    def test_historical_full_raw_output_characterization(self):
        rows = [
            pa(1, "2025-04-01", 10, 201, "strikeout", xwoba=0.1, outs=1),
            pa(
                1,
                "2025-04-01",
                20,
                101,
                "field_out",
                hand="L",
                xwoba=0.2,
                outs=1,
                top="Bot",
                at_bat=2,
            ),
        ]
        history = FeatureHistory(
            data(rows), slot_weights={slot: 1 / 9 for slot in range(1, 10)}
        )

        raw = build(game(), history)
        canonical = json.dumps(
            raw,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        self.assertEqual(len(canonical), 11_330)
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            "ca27525af187969fe01ecfed740ce8c07e0ebde30143efb0aa24208d450ba6a6",
        )
        self.assertEqual(raw["reasons"], [])
        self.assertEqual(raw["lineup_provenance"], "RECONSTRUCTED_FINAL_LINEUP")
        self.assertEqual(raw["starter_provenance"], "RECONSTRUCTED_ACTUAL_STARTER")

    def test_representative_month_builds_one_history_without_full_pitch_prefix_scans(self):
        rows = [
            pa(1, "2025-04-01", 10, 201, "strikeout", xwoba=0.1, outs=1),
            pa(
                1,
                "2025-04-01",
                20,
                101,
                "field_out",
                hand="L",
                xwoba=0.2,
                outs=1,
                top="Bot",
                at_bat=2,
            ),
        ]
        games = [
            game(
                game_pk=day * 100 + sequence,
                official_date=f"2025-05-{day:02d}",
                scheduled_start_utc=f"2025-05-{day:02d}T{18 + sequence:02d}:00:00Z",
            )
            for day in range(1, 32)
            for sequence in range(3)
        ]
        eras = {
            f"2025-05-{day:02d}": {
                (date(2025, 5, day) - timedelta(days=1)).isoformat(): era_record(
                    as_of=(date(2025, 5, day) - timedelta(days=1)).isoformat()
                )
            }
            for day in range(1, 32)
        }
        history_builds = 0
        full_pitch_prefix_scans = 0
        holder = {}
        original_rows_before = FeatureHistory.rows_before

        def history_factory(_representative):
            nonlocal history_builds
            history_builds += 1
            history = FeatureHistory(
                data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}
            )
            holder["history"] = history
            return history

        def tracked_rows_before(indexed_rows, target):
            nonlocal full_pitch_prefix_scans
            if indexed_rows is holder.get("history").pitches:
                full_pitch_prefix_scans += 1
            return original_rows_before(indexed_rows, target)

        with mock.patch.object(
            FeatureHistory, "rows_before", side_effect=tracked_rows_before
        ):
            prepared = build_raw_features_by_date(
                games,
                history_factory,
                lambda target, history: build_raw_game_features(
                    target, history, mapping(), eras[target["official_date"]]
                ),
            )

        self.assertEqual(len(prepared), 31 * 3)
        self.assertTrue(all(row["eligible"] for row in prepared))
        self.assertEqual(history_builds, 1)
        self.assertEqual(full_pitch_prefix_scans, 0)

    def test_fip_numerator_uses_completed_pas_but_outs_use_every_pitch(self):
        terminal = pa(1, "2025-04-30", 10, 101, "strikeout")
        terminal["outs_recorded"] = 0
        runner_out = dict(
            terminal,
            events="",
            at_bat_number=2,
            event_id="1:2",
            outs_recorded=1,
        )

        raw_fip, bf = FeatureHistory._league_fip(
            [terminal], [terminal, runner_out]
        )

        self.assertEqual(bf, 1)
        self.assertEqual(raw_fip, -6.0)

    def test_game_build_never_copies_or_scans_full_season_for_league_fip(self):
        rows = [
            pa(day, f"2025-04-{day:02d}", 10, 101, "strikeout", outs=1)
            for day in range(1, 29)
        ]
        history = FeatureHistory(
            data(rows), slot_weights={slot: 1 / 9 for slot in range(1, 10)}
        )
        original = history.rows_before

        def bounded_only(candidate, target):
            if candidate is history.pitches:
                raise AssertionError("full-season pitch prefix scanned per game")
            return original(candidate, target)

        history.rows_before = bounded_only
        result = build(game(), history)

        self.assertEqual(result["home"]["starter"]["league_raw_fip"]["sample_size"], 28)

    def test_full_season_history_is_strictly_bounded_as_of_target_date(self):
        prior = [
            pa(1, "2025-04-30", 10, 101, "strikeout", xwoba=0.1, outs=1),
            pa(
                1,
                "2025-04-30",
                20,
                201,
                "field_out",
                hand="L",
                xwoba=0.2,
                outs=1,
                top="Bot",
                at_bat=2,
            ),
        ]
        same_day = pa(
            900,
            "2025-05-01",
            10,
            101,
            "home_run",
            xwoba=2.0,
            at_bat=3,
        )
        future = pa(
            901,
            "2025-05-02",
            20,
            201,
            "home_run",
            hand="L",
            xwoba=2.0,
            top="Bot",
            at_bat=4,
        )
        weights = {i: 1 / 9 for i in range(1, 10)}
        expected = build(game(), FeatureHistory(data(prior), slot_weights=weights))
        actual = build(
            game(),
            FeatureHistory(data([*prior, same_day, future]), slot_weights=weights),
        )

        for side in ("home", "away"):
            self.assertEqual(actual[side], expected[side])
        self.assertEqual(actual["provenance"]["max_history_date"], "2025-04-30")
        self.assertEqual(actual["provenance"]["raw_source_max_date"], "2025-05-02")
        self.assertEqual(actual["provenance"]["history_validation"], "PASSED_AS_OF")

    def test_schedule_coverage_window_controls_supported_subrules(self):
        with self.assertRaises(TypeError):
            ScheduleHistory([])

        stale_row = {
            "game_pk": 1,
            "official_date": "2025-04-01",
            "team_codes": ["HOM"],
            "home_team_code": "HOM",
            "venue_name": "Park",
            "innings": 9,
        }
        stale = build(
            game(doubleheader_marker=None, doubleheader=False),
            FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                schedule_history=[stale_row],
                schedule_coverage_start="2025-04-01",
                schedule_coverage_end="2025-04-01",
                schedule_coverage_complete=True,
            ),
        )
        self.assertEqual(stale["home"]["schedule"]["value"], 50.0)
        self.assertEqual(stale["home"]["schedule"]["quality"], 1 / 6)

        full = build(
            game(doubleheader_marker=None, doubleheader=False),
            FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                schedule_coverage_start="2025-04-28",
                schedule_coverage_end="2025-04-30",
                schedule_coverage_complete=True,
            ),
        )
        self.assertEqual(full["home"]["schedule"]["value"], 62.0)
        self.assertEqual(full["home"]["schedule"]["quality"], 1.0)
        self.assertEqual(full["provenance"]["schedule_coverage_start"], "2025-04-28")
        self.assertEqual(full["provenance"]["schedule_coverage_end"], "2025-04-30")
        self.assertTrue(full["provenance"]["schedule_coverage_complete"])

        partial = build(
            game(doubleheader_marker=None, doubleheader=False),
            FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                schedule_coverage_start="2025-04-28",
                schedule_coverage_end="2025-04-30",
                schedule_coverage_complete=False,
            ),
        )
        self.assertEqual(partial["home"]["schedule"]["value"], 50.0)
        self.assertEqual(partial["home"]["schedule"]["quality"], 1 / 6)

    def test_participant_index_and_schedule_enrichment_make_actual_starter_authoritative(
        self,
    ):
        schedule_game = parse_schedule(
            Path(__file__).parent / "fixtures" / "schedule_2025_05_01.json"
        )[0]
        target_rows = [
            pa(778100, "2025-05-01", 694973, 1, "field_out", outs=1),
            pa(778100, "2025-05-01", 999999, 2, "field_out", top="Bot", at_bat=2),
        ]
        index = actual_starter_index(
            StatcastData(tuple(target_rows), tuple(target_rows))
        )
        enriched = enrich_schedule_game_participants(schedule_game, index)
        self.assertEqual(enriched["home_actual_starter_id"], 694973)
        self.assertEqual(enriched["away_actual_starter_id"], 999999)
        self.assertFalse(enriched["home_probable_starter_mismatch"])
        self.assertTrue(enriched["away_probable_starter_mismatch"])
        self.assertEqual(enriched["starter_provenance"], "RECONSTRUCTED_ACTUAL_STARTER")

        team_map = mapping()
        del team_map["Home"]
        del team_map["Away"]
        team_map["Chicago Cubs"] = "CHC"
        team_map["Pittsburgh Pirates"] = "PIT"
        result = build(
            enriched,
            FeatureHistory(data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}),
            team_map=team_map,
        )
        self.assertTrue(result["eligible"])
        self.assertTrue(result["away_probable_starter_mismatch"])
        self.assertEqual(result["away"]["starter"]["pitcher_id"], 999999)

    def test_source_backed_league_era_produces_cfip_and_final_starter_fip(self):
        rows = [
            pa(1, "2025-04-01", 10, 201, "strikeout", xwoba=0.1, outs=1),
            pa(1, "2025-04-01", 10, 202, "walk", woba=0.7, at_bat=2),
            pa(
                1,
                "2025-04-01",
                20,
                101,
                "field_out",
                hand="L",
                xwoba=0.1,
                outs=1,
                top="Bot",
                at_bat=3,
            ),
        ]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
            league_era={"2025-04-30": era_record(value=4.25, earned_runs=17, outs=108)},
        )
        starter = result["home"]["starter"]
        self.assertEqual(starter["league_era"]["value"], 4.25)
        self.assertAlmostEqual(starter["league_raw_fip"]["value"], 1.5)
        self.assertAlmostEqual(starter["league_cfip"]["value"], 2.75)
        self.assertAlmostEqual(starter["fip"]["value"], 5.75)
        self.assertEqual(
            result["provenance"]["league_era"],
            era_record(value=4.25, earned_runs=17, outs=108),
        )

    def test_missing_or_invalid_league_era_is_explicitly_ineligible(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        missing = build_raw_game_features(game(), history, mapping(), {})
        invalid = build_raw_game_features(
            game(), history, mapping(), {"2025-04-30": era_record(value=4.1)}
        )
        future = build_raw_game_features(
            game(), history, mapping(), {"2025-05-01": era_record(as_of="2025-05-01")}
        )
        full_season = build_raw_game_features(
            game(), history, mapping(), {2025: era_record()}
        )
        self.assertEqual(missing["reasons"], ["missing_league_era_d1"])
        self.assertEqual(invalid["reasons"], ["invalid_league_era_provenance"])
        self.assertEqual(future["reasons"], ["missing_league_era_d1"])
        self.assertEqual(full_season["reasons"], ["missing_league_era_d1"])
        self.assertFalse(missing["eligible"])
        self.assertNotIn("home", missing)

        valid = era_record()
        invalid_records = (
            dict(valid, as_of_date="2025-04-29"),
            dict(valid, earned_runs=-1),
            dict(valid, earned_runs=True),
            dict(valid, outs=0),
            dict(valid, outs=True),
            dict(valid, source="other"),
            dict(valid, manifest_sha256="A" * 64),
            dict(valid, manifest_sha256="a" * 63),
        )
        for record in invalid_records:
            with self.subTest(record=record):
                result = build_raw_game_features(
                    game(), history, mapping(), {"2025-04-30": record}
                )
                self.assertEqual(result["reasons"], ["invalid_league_era_provenance"])

    def test_invalid_target_never_claims_or_runs_history_validation(self):
        poisoned = pa(900, "2025-05-01", 10, 101, "home_run", xwoba=2.0)
        history = FeatureHistory(
            StatcastData((poisoned,), (poisoned,)),
            slot_weights={i: 1 / 9 for i in range(1, 10)},
        )
        for target in (game(game_pk=True), game(official_date="bad-date")):
            with self.subTest(target=target):
                result = build(target, history)
                self.assertFalse(result["eligible"])
                self.assertFalse(result["provenance"]["statcast_target_excluded"])
                self.assertEqual(
                    result["provenance"]["history_validation"],
                    "NOT_RUN_INVALID_TARGET",
                )
                self.assertNotIn("home", result)

    def test_exact_schedule_game_eligibility_validation_reasons(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        duplicate_lineup = game()["home_lineup"]
        duplicate_lineup = [dict(player) for player in duplicate_lineup]
        duplicate_lineup[-1]["player_id"] = duplicate_lineup[0]["player_id"]
        cases = (
            ({"game_type": "P"}, "invalid_game_type"),
            ({"game_pk": True}, "invalid_game_pk"),
            ({"official_date": "May 1"}, "invalid_official_date"),
            (
                {"scheduled_start_utc": "2025-05-01T23:00:00-04:00"},
                "invalid_scheduled_start_utc",
            ),
            ({"status": "Postponed"}, "ineligible_status"),
            ({"status": ""}, "ineligible_status"),
            ({"venue_id": None}, "missing_venue"),
            ({"venue_name": ""}, "missing_venue"),
            ({"away_team": "Home"}, "invalid_teams"),
            ({"home_actual_starter_id": None}, "missing_home_actual_starter"),
            ({"starter_provenance": "PROBABLE"}, "invalid_starter_provenance"),
            ({"home_lineup": duplicate_lineup}, "invalid_home_lineup"),
            ({"away_lineup": game()["away_lineup"][:8]}, "invalid_away_lineup"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                result = build(game(**changes), history)
                self.assertFalse(result["eligible"])
                self.assertIn(reason, result["reasons"])

    def test_actual_starter_ids_drive_raw_starter_and_handedness_features(self):
        rows = [
            pa(1, "2025-04-30", 30, 201, "field_out", hand="L", xwoba=0.1, outs=1),
            pa(
                1,
                "2025-04-30",
                40,
                101,
                "field_out",
                hand="R",
                xwoba=0.2,
                outs=1,
                top="Bot",
                at_bat=2,
            ),
        ]
        result = build(
            game(
                home_probable_pitcher_id=30,
                home_actual_starter_id=30,
                away_probable_pitcher_id=40,
                away_actual_starter_id=40,
            ),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        self.assertEqual(result["home"]["starter"]["pitcher_id"], 30)
        self.assertEqual(result["away"]["starter"]["pitcher_id"], 40)
        self.assertEqual(result["starter_provenance"], "RECONSTRUCTED_ACTUAL_STARTER")

    def test_incomplete_statcast_coverage_is_ineligible_and_explicit(self):
        result = build(
            game(),
            FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                statcast_coverage_complete=False,
            ),
        )
        self.assertFalse(result["eligible"])
        self.assertIn("incomplete_statcast_coverage", result["reasons"])
        self.assertNotIn("home", result)
        self.assertFalse(result["provenance"]["statcast_coverage_complete"])

    def test_as_of_boundary_excludes_target_plate_appearances_and_tracks_provenance(self):
        pitch = pa(1, "2025-04-28", 10, 101, "field_out", xwoba=0.1)
        later_pa = pa(2, "2025-04-30", 10, 102, "field_out", xwoba=0.1)
        history = FeatureHistory(
            StatcastData((pitch,), (pitch, later_pa)),
            slot_weights={i: 1 / 9 for i in range(1, 10)},
        )
        self.assertEqual(
            build(game(), history)["provenance"]["max_history_date"], "2025-04-30"
        )
        target_pa = dict(
            later_pa, game_pk=900, game_date="2025-05-01", event_id="900:1"
        )
        poisoned = FeatureHistory(
            StatcastData((pitch,), (pitch, target_pa)),
            slot_weights={i: 1 / 9 for i in range(1, 10)},
        )
        clean = build(
            game(),
            FeatureHistory(
                StatcastData((pitch,), (pitch,)),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
            ),
        )
        bounded = build(game(), poisoned)
        self.assertEqual(bounded["home"], clean["home"])
        self.assertEqual(bounded["away"], clean["away"])
        self.assertEqual(bounded["provenance"]["raw_source_max_date"], "2025-05-01")
        self.assertEqual(bounded["provenance"]["max_history_date"], "2025-04-28")

    def test_valid_output_discloses_lineup_and_raw_source_provenance(self):
        row = pa(1, "2025-04-30", 10, 101, "field_out", xwoba=0.1, outs=1)
        result = build(
            game(),
            FeatureHistory(data([row]), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        self.assertEqual(result["lineup_provenance"], "RECONSTRUCTED_FINAL_LINEUP")
        self.assertEqual(result["provenance"]["raw_source_max_date"], "2025-04-30")
        self.assertEqual(result["provenance"]["history_validation"], "PASSED_AS_OF")
        self.assertTrue(result["provenance"]["statcast_target_excluded"])

    def test_rate_denominators_and_frozen_reliability_qualities(self):
        rows = [
            pa(1, "2025-04-01", 10, 201, "strikeout", xwoba=0.1, denom=1, outs=1),
            pa(1, "2025-04-01", 10, 202, "walk", woba=0.7, denom=0, at_bat=2),
            pa(
                1, "2025-04-01", 10, 203, "field_out", woba=0, denom=0, outs=1, at_bat=3
            ),
            pa(
                1,
                "2025-04-01",
                20,
                101,
                "field_out",
                hand="L",
                xwoba=0.2,
                outs=1,
                top="Bot",
                at_bat=4,
            ),
            pa(
                1,
                "2025-04-01",
                21,
                102,
                "walk",
                hand="L",
                woba=0.7,
                denom=0,
                top="Bot",
                at_bat=5,
            ),
        ]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        starter = result["home"]["starter"]
        self.assertEqual(starter["k_minus_bb"]["value"], 0.0)
        self.assertEqual(starter["k_minus_bb"]["sample_size"], 3)
        self.assertAlmostEqual(starter["k_minus_bb"]["quality"], 3 / 153)
        self.assertAlmostEqual(starter["season_xwoba"]["quality"], 1 / 151)
        self.assertEqual(starter["fip"]["sample_size"], 3)
        self.assertAlmostEqual(starter["fip"]["quality"], 3 / 153)
        bullpen = result["away"]["bullpen"]
        self.assertAlmostEqual(bullpen["k_minus_bb"]["quality"], 1 / 301)

    def test_lineup_raw_value_averages_present_slots_without_silent_zero_scaling(self):
        rows = [
            pa(
                1,
                "2025-04-30",
                20,
                101,
                "field_out",
                hand="L",
                xwoba=0.4,
                outs=1,
                top="Bot",
            ),
        ]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        metric = result["home"]["offense"]["platoon_xwoba"]
        self.assertEqual(metric["value"], 0.4)
        self.assertAlmostEqual(metric["quality"], (1 / 9) * (1 / 151))
        self.assertEqual(metric["present_weight"], 1 / 9)

    def test_unknown_opposing_starter_hand_neutralizes_only_platoon_metric(self):
        rows = [pa(1, "2025-04-30", 30, 101, "field_out", hand="R", xwoba=0.4, outs=1)]
        result = build(
            game(away_probable_pitcher_id=999, away_actual_starter_id=999),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        offense = result["home"]["offense"]
        self.assertIsNone(offense["platoon_xwoba"]["value"])
        self.assertEqual(offense["platoon_xwoba"]["quality"], 0.0)
        self.assertEqual(offense["last14_xwoba"]["value"], 0.4)

    def test_rest_days_are_full_calendar_days_between_starts(self):
        rows = []
        for game_pk, day in ((1, "2025-04-20"), (2, "2025-04-24"), (3, "2025-04-27")):
            for index in range(6):
                rows.append(
                    pa(
                        game_pk,
                        day,
                        10,
                        200 + index,
                        "field_out",
                        xwoba=0.1,
                        outs=3,
                        at_bat=index + 1,
                    )
                )
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        expected_ip = result["home"]["starter"]["expected_ip"]
        self.assertEqual(expected_ip["value"], 4.5)
        self.assertEqual(expected_ip["sample_size"], 3)
        self.assertEqual(expected_ip["quality"], 0.5)

    def test_velocity_uses_prior_season_baseline_and_excludes_recent_pitches(self):
        rows = [
            pa(
                100 + i,
                "2024-09-01",
                10,
                300 + i,
                "field_out",
                xwoba=0.1,
                outs=1,
                speed=94.0,
                at_bat=1,
            )
            for i in range(50)
        ]
        for i, day in enumerate(
            ("2025-04-01", "2025-04-05", "2025-04-10", "2025-04-15", "2025-04-20")
        ):
            rows.append(
                pa(
                    200 + i,
                    day,
                    10,
                    201 + i,
                    "field_out",
                    xwoba=0.1,
                    outs=1,
                    speed=97.0,
                )
            )
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        velocity = result["home"]["starter"]["velocity_change"]
        self.assertEqual(velocity["value"], 3.0)
        self.assertEqual(velocity["baseline_sample_size"], 50)
        self.assertEqual(velocity["sample_size"], 5)

    def test_starter_career_bf_counts_all_prior_rows_not_only_current_season(self):
        rows = [
            pa(1, "2024-09-01", 10, 301, "field_out", xwoba=0.1, outs=1),
            pa(2, "2025-04-01", 10, 302, "field_out", xwoba=0.1, outs=1),
            pa(3, "2025-04-02", 20, 303, "field_out", xwoba=0.1, outs=1),
        ]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        self.assertEqual(result["home"]["starter"]["career_bf"], 2)
        self.assertEqual(result["away"]["starter"]["career_bf"], 1)

    def test_velocity_falls_back_to_current_season_strictly_before_recent_five(self):
        rows = [
            pa(
                100 + i,
                "2025-03-20",
                10,
                300 + i,
                "field_out",
                xwoba=0.1,
                outs=1,
                speed=93.0,
            )
            for i in range(50)
        ]
        for i, day in enumerate(
            ("2025-04-01", "2025-04-05", "2025-04-10", "2025-04-15", "2025-04-20")
        ):
            rows.append(
                pa(
                    200 + i,
                    day,
                    10,
                    201 + i,
                    "field_out",
                    xwoba=0.1,
                    outs=1,
                    speed=96.0,
                )
            )
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        velocity = result["home"]["starter"]["velocity_change"]
        self.assertEqual(velocity["value"], 3.0)
        self.assertEqual(velocity["baseline_sample_size"], 50)

    def test_leverage_uses_pre_pa_score_margin_not_post_result(self):
        rows = [
            pa(1, "2025-04-30", 10, 201, "field_out", xwoba=0.1, outs=1),
            pa(
                1,
                "2025-04-30",
                11,
                202,
                "field_out",
                xwoba=0.1,
                inning=8,
                pre_home=4,
                pre_away=0,
                post_home=0,
                post_away=0,
                at_bat=2,
            ),
            pa(2, "2025-04-29", 10, 201, "field_out", xwoba=0.1, outs=1),
            pa(
                2,
                "2025-04-29",
                12,
                202,
                "field_out",
                xwoba=0.1,
                inning=8,
                pre_home=0,
                pre_away=0,
                post_home=10,
                post_away=0,
                at_bat=2,
            ),
        ]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        members = result["home"]["bullpen"]["leverage_pool"]["members"]
        self.assertEqual(members, [{"pitcher_id": 12, "batters_faced": 1}])

    def test_bullpen_availability_quality_is_usage_completeness_not_rate_shrinkage(
        self,
    ):
        rows = [
            pa(1, "2025-04-01", 10, 201, "field_out", xwoba=0.1, outs=1),
            pa(1, "2025-04-01", 11, 202, "field_out", xwoba=0.1, inning=8, at_bat=2),
            pa(2, "2025-04-02", 10, 201, "field_out", xwoba=0.1, outs=1),
            pa(2, "2025-04-02", 12, 202, "field_out", xwoba=0.1, inning=8, at_bat=2),
            pa(3, "2025-04-30", 10, 201, "field_out", xwoba=0.1, outs=1),
            pa(3, "2025-04-30", 11, 202, "field_out", xwoba=0.1, inning=6, at_bat=2),
        ]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        availability = result["home"]["bullpen"]["availability"]
        self.assertEqual(availability["value"], 100.0)
        self.assertEqual(availability["quality"], 1.0)

        complete_rows = [row for row in rows if row["pitcher_id"] != 12]
        complete = build(
            game(),
            FeatureHistory(
                data(complete_rows), slot_weights={i: 1 / 9 for i in range(1, 10)}
            ),
        )
        self.assertEqual(complete["home"]["bullpen"]["availability"]["quality"], 1.0)

    def test_rested_leverage_pitcher_has_source_backed_zero_usage_when_coverage_complete(
        self,
    ):
        rows = [
            pa(1, "2025-04-01", 10, 201, "field_out", xwoba=0.1, outs=1),
            pa(1, "2025-04-01", 11, 202, "field_out", xwoba=0.1, inning=8, at_bat=2),
        ]
        complete = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )["home"]["bullpen"]["availability"]
        self.assertEqual(complete["value"], 100.0)
        self.assertEqual(complete["quality"], 1.0)

    def test_home_city_bonus_requires_home_games_at_same_venue_on_d1_d2_d3(self):
        def schedule(home_codes):
            return [
                {
                    "game_pk": index,
                    "official_date": day,
                    "team_codes": ["HOM", "OPP"],
                    "home_team_code": home_code,
                    "venue_name": "Park",
                    "innings": 9,
                }
                for index, (day, home_code) in enumerate(
                    zip(("2025-04-30", "2025-04-29", "2025-04-28"), home_codes), 1
                )
            ]

        def score(home_codes):
            history = FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                schedule_history=schedule(home_codes),
                schedule_coverage_start="2025-04-28",
                schedule_coverage_end="2025-04-30",
                schedule_coverage_complete=True,
            )
            return build(game(doubleheader_marker=None), history)["home"]["schedule"][
                "value"
            ]

        self.assertEqual(score(("HOM", "HOM", "HOM")), 55.0)
        self.assertEqual(score(("HOM", "OPP", "HOM")), 50.0)

    def test_home_city_is_unsupported_when_covered_rows_lack_location_identity(self):
        rows = [
            {
                "game_pk": index,
                "official_date": day,
                "team_codes": ["HOM", "OPP"],
                "innings": 9,
            }
            for index, day in enumerate(("2025-04-30", "2025-04-29", "2025-04-28"), 1)
        ]
        result = build(
            game(doubleheader_marker=None, doubleheader=False),
            FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                schedule_history=rows,
                schedule_coverage_start="2025-04-28",
                schedule_coverage_end="2025-04-30",
                schedule_coverage_complete=True,
            ),
        )
        schedule = result["home"]["schedule"]
        self.assertEqual(schedule["value"], 50.0)
        self.assertEqual(schedule["quality"], 0.5)
        self.assertIn("home_city_unsupported", schedule["flags"])

    def test_schedule_history_indexes_once_for_reasonable_repeated_builds(self):
        class CountingRows:
            def __init__(self, rows):
                self.rows = rows
                self.iterations = 0

            def __iter__(self):
                self.iterations += 1
                return iter(self.rows)

        rows = CountingRows(
            [
                {
                    "game_pk": i + 1,
                    "official_date": f"2024-{1 + (i // 28) % 12:02d}-{1 + i % 28:02d}",
                    "team_codes": [f"T{i % 28:02d}"],
                    "home_team_code": f"T{i % 28:02d}",
                    "venue_name": "Park",
                    "innings": 9,
                }
                for i in range(5000)
            ]
        )
        started = time.perf_counter()
        schedule = ScheduleHistory(
            rows,
            coverage_start="2024-01-01",
            coverage_end="2024-12-31",
            coverage_complete=True,
        )
        history = FeatureHistory(
            data([]),
            slot_weights={i: 1 / 9 for i in range(1, 10)},
            schedule_history=schedule,
        )
        for _ in range(20):
            build(game(doubleheader_marker=None), history)
        elapsed = time.perf_counter() - started
        self.assertEqual(rows.iterations, 1)
        self.assertLess(elapsed, 5.0)

    def test_schedule_provenance_separates_input_from_strictly_prior_used_rows(self):
        schedule = [
            {
                "game_pk": 1,
                "official_date": "2025-04-30",
                "team_codes": ["HOM"],
                "home_team_code": "HOM",
                "venue_name": "Park",
                "innings": 9,
            },
            {
                "game_pk": 900,
                "official_date": "2025-05-01",
                "team_codes": ["HOM", "AWY"],
                "home_team_code": "HOM",
                "venue_name": "Park",
            },
            {
                "game_pk": 901,
                "official_date": "2025-05-02",
                "team_codes": ["HOM"],
                "home_team_code": "HOM",
                "venue_name": "Park",
            },
        ]
        result = build(
            game(doubleheader_marker=None, doubleheader=False),
            FeatureHistory(
                data([]),
                slot_weights={i: 1 / 9 for i in range(1, 10)},
                schedule_history=schedule,
                schedule_coverage_start="2025-04-28",
                schedule_coverage_end="2025-04-30",
                schedule_coverage_complete=True,
            ),
        )
        provenance = result["provenance"]
        self.assertEqual(provenance["schedule_input_max_date"], "2025-05-02")
        self.assertEqual(provenance["schedule_used_max_date"], "2025-04-30")
        self.assertTrue(provenance["target_schedule_present"])
        self.assertTrue(provenance["future_schedule_present"])
        self.assertEqual(result["home"]["schedule"]["value"], 50.0)

    def test_missing_prior_schedule_is_neutral_except_known_doubleheader_rule(self):
        result = build(
            game(doubleheader_marker=None, doubleheader=False),
            FeatureHistory(data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        schedule = result["home"]["schedule"]
        self.assertEqual(schedule["value"], 50.0)
        self.assertAlmostEqual(schedule["quality"], 1 / 6)
        self.assertIn("rest_unsupported", schedule["flags"])

    def test_schedule_history_rejects_malformed_rows(self):
        valid = {
            "game_pk": 1,
            "official_date": "2025-04-30",
            "team_codes": ["HOM"],
            "innings": 9,
        }
        invalid = (
            dict(valid, game_pk=True),
            dict(valid, official_date="bad"),
            dict(valid, team_codes="HOM"),
            dict(valid, innings=-1),
            dict(valid, venue_latitude="north"),
            dict(valid, time_zone=""),
        )
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                ScheduleHistory(
                    [row],
                    coverage_start="2025-04-01",
                    coverage_end="2025-04-30",
                    coverage_complete=True,
                )

    def test_schedule_history_indexes_exact_schedule_neutralizations_separately(self):
        records = schedule_neutralization_records()
        schedule = ScheduleHistory(
            task10_schedule_rows(),
            coverage_start="2019-05-01",
            coverage_end="2019-09-30",
            coverage_complete=True,
            schedule_neutralizations=records,
        )

        self.assertEqual(
            tuple(map(dict, schedule.schedule_neutralizations)),
            tuple(
                {**record, "reason_codes": tuple(record["reason_codes"])}
                for record in records
            ),
        )
        self.assertEqual(
            set(schedule.neutralization_by_key),
            {
                (565338, "2019-05-20", 133),
                (566156, "2019-09-06", 116),
                (566156, "2019-09-06", 133),
            },
        )
        self.assertEqual(
            schedule.neutralization_game_dates,
            frozenset(
                {
                    (565338, "2019-05-20"),
                    (566156, "2019-09-06"),
                }
            ),
        )
        self.assertEqual(
            schedule.team_id_by_game_date_code[(566156, "2019-09-06", "AWY")],
            116,
        )
        self.assertEqual(
            schedule.team_id_by_game_date_code[(566156, "2019-09-06", "HOM")],
            133,
        )
        self.assertEqual(set(schedule.game_dates), {565338, 565535, 566156, 566157})
        self.assertFalse(schedule.has_game(566156, date(2019, 9, 7)))
        with self.assertRaises(TypeError):
            schedule.neutralization_by_key[(1, "2019-01-01", 1)] = {}
        with self.assertRaises(TypeError):
            schedule.team_id_by_game_date_code[(1, "2019-01-01", "AWY")] = 1

    def test_schedule_history_owns_immutable_neutralization_reason_codes(self):
        records = schedule_neutralization_records()
        schedule = ScheduleHistory(
            task10_schedule_rows(),
            coverage_start="2019-05-01",
            coverage_end="2019-09-30",
            coverage_complete=True,
            schedule_neutralizations=records,
        )
        records[0]["reason_codes"].append("caller_mutation")

        stored = schedule.schedule_neutralizations[0]
        indexed = schedule.neutralization_by_key[(566156, "2019-09-06", 116)]
        self.assertEqual(
            stored["reason_codes"],
            ("schedule_neutral_duplicate_gamepk_dependency",),
        )
        self.assertEqual(indexed["reason_codes"], stored["reason_codes"])
        with self.assertRaises(AttributeError):
            stored["reason_codes"].append("stored_mutation")

    def test_schedule_history_materializes_neutralization_iterable_once(self):
        iterations = 0

        def neutralizations():
            nonlocal iterations
            iterations += 1
            yield from schedule_neutralization_records()

        schedule = ScheduleHistory(
            task10_schedule_rows(),
            coverage_start="2019-05-01",
            coverage_end="2019-09-30",
            coverage_complete=True,
            schedule_neutralizations=neutralizations(),
        )

        self.assertEqual(iterations, 1)
        self.assertEqual(
            set(schedule.neutralization_by_key),
            {
                (565338, "2019-05-20", 133),
                (566156, "2019-09-06", 116),
                (566156, "2019-09-06", 133),
            },
        )

    def test_schedule_neutralization_emits_exact_metric_for_both_approved_sides(self):
        raw = build_task10(
            task10_game(566156, "2019-09-06", 116, 133),
            task10_feature_history(),
        )

        expected = {
            "value": 50.0,
            "sample_size": 0,
            "quality": 0.0,
            "flags": ["schedule_neutral_duplicate_gamepk_dependency"],
        }
        self.assertEqual(raw["away"]["schedule"], expected)
        self.assertEqual(raw["home"]["schedule"], expected)

    def test_schedule_neutralization_leaves_unapproved_side_ordinary(self):
        raw = build_task10(
            task10_game(565338, "2019-05-20", 133, 114),
            task10_feature_history(),
        )

        reason = "schedule_neutral_duplicate_gamepk_dependency"
        self.assertEqual(
            raw["away"]["schedule"],
            {
                "value": 50.0,
                "sample_size": 0,
                "quality": 0.0,
                "flags": [reason],
            },
        )
        self.assertNotIn(reason, raw["home"]["schedule"].get("flags", ()))

    def test_schedule_neutralization_requires_authenticated_target_side_bindings(self):
        target = task10_game(566156, "2019-09-06", 116, 133)
        invalid_targets = (
            {key: value for key, value in target.items() if key != "away_team_id"},
            {key: value for key, value in target.items() if key != "away_team_code"},
            dict(target, away_team_id=True),
            dict(target, away_team_id=0),
            dict(target, away_team_id=133),
            dict(target, away_team_code="BAD"),
        )

        for invalid_target in invalid_targets:
            with self.subTest(invalid_target=invalid_target):
                with self.assertRaises(ValueError):
                    build_task10(invalid_target, task10_feature_history())

    def test_schedule_neutralization_does_not_extend_to_known_unapproved_neighbors(self):
        reason = "schedule_neutral_duplicate_gamepk_dependency"
        targets = (
            task10_game(565535, "2019-05-21", 146, 116),
            task10_game(566157, "2019-09-07", 116, 133),
            game(doubleheader_marker=None, doubleheader=False),
        )
        history = task10_feature_history()

        for target in targets:
            with self.subTest(target=target["game_pk"]):
                raw = build_task10(target, history)
                for side in ("away", "home"):
                    self.assertNotIn(reason, raw[side]["schedule"].get("flags", ()))

    def test_schedule_neutralization_ignores_caller_policy_like_target_fields(self):
        target = task10_game(
            566157,
            "2019-09-07",
            116,
            133,
            target_raw_game_object_sha256="0" * 64,
            target_normalized_anchor_sha256="1" * 64,
            reason_codes=["schedule_neutral_duplicate_gamepk_dependency"],
        )
        raw = build_task10(target, task10_feature_history())

        reason = "schedule_neutral_duplicate_gamepk_dependency"
        for side in ("away", "home"):
            self.assertNotIn(reason, raw[side]["schedule"].get("flags", ()))

    def test_schedule_history_rejects_partial_or_incoherent_side_identity(self):
        valid = task10_schedule_rows()[0]
        invalid = (
            {key: value for key, value in valid.items() if key != "away_team_id"},
            dict(valid, away_team_id=True),
            dict(valid, home_team_id=133),
            dict(valid, home_team_code="AWY"),
            dict(valid, team_codes=["HOM", "AWY"]),
        )
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                ScheduleHistory(
                    [row],
                    coverage_start="2019-05-20",
                    coverage_end="2019-05-20",
                    coverage_complete=True,
                )

    def test_large_statcast_history_repeated_builds_use_indexes_reasonably(self):
        rows = [
            pa(
                1000 + i,
                "2025-04-01",
                10000 + i,
                20000 + i,
                "field_out",
                xwoba=0.1,
                outs=1,
                home=f"T{i % 28:02d}",
                away=f"T{(i + 1) % 28:02d}",
            )
            for i in range(20000)
        ]
        started = time.perf_counter()
        history = FeatureHistory(
            data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        for _ in range(20):
            build(game(doubleheader_marker=None), history)
        self.assertLess(time.perf_counter() - started, 5.0)
        self.assertEqual(len(history.pitches_by_pitcher_game), 20000)

    def test_dense_relevant_history_repeated_builds_remain_reasonable(self):
        rows = [
            pa(
                1000 + i // 100,
                f"2025-04-{1 + (i // 100) % 30:02d}",
                10 if i % 100 < 50 else 11,
                101 + i % 9,
                "field_out",
                xwoba=0.2,
                outs=1,
                at_bat=i % 100 + 1,
            )
            for i in range(20000)
        ]
        started = time.perf_counter()
        history = FeatureHistory(
            data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        for _ in range(10):
            build(game(), history)
        self.assertLess(time.perf_counter() - started, 5.0)

    def test_expected_woba_uses_expected_then_source_backed_deterministic_fallbacks(
        self,
    ):
        base = pa(1, "2025-04-01", 1, 2, "single", xwoba=0.42, woba=0.9)
        self.assertEqual(pa_expected_woba(base), 0.42)
        for event, expected in (
            ("walk", 0.7),
            ("intent_walk", 0.7),
            ("hit_by_pitch", 0.72),
            ("strikeout", 0.0),
            ("field_out", 0.0),
            ("other_out", 0.0),
        ):
            self.assertEqual(
                pa_expected_woba(
                    dict(
                        base,
                        events=event,
                        estimated_woba_using_speedangle=None,
                        woba_value=expected,
                    )
                ),
                expected,
            )
        self.assertIsNone(
            pa_expected_woba(
                dict(base, events="single", estimated_woba_using_speedangle=None)
            )
        )
        self.assertIsNone(pa_expected_woba(dict(base, woba_denom=0)))

    def test_blank_event_hbp_is_canonical_for_downstream_batting_metrics(self):
        fixture = Path(__file__).parent / "fixtures" / "statcast_mini.csv"
        payload = fixture.read_text("utf-8-sig").replace(
            "hit_by_pitch,hit_by_pitch", ",hit_by_pitch", 1
        ).encode("utf-8-sig")

        parsed = parse_statcast(payload)
        hbp = next(
            row
            for row in parsed.plate_appearances
            if row["game_pk"] == 1002 and row["at_bat_number"] == 2
        )

        self.assertEqual(hbp["events"], "hit_by_pitch")
        self.assertEqual(pa_expected_woba(hbp), 0.72)
        history_data = StatcastData(
            tuple(dict(row, outs_recorded=0) for row in parsed.pitches),
            tuple(dict(row, outs_recorded=0) for row in parsed.plate_appearances),
        )
        target = game(
            official_date="2024-04-03",
            scheduled_start_utc="2024-04-03T23:00:00Z",
            away_probable_pitcher_id=23,
            home_probable_pitcher_id=13,
            away_actual_starter_id=23,
            home_actual_starter_id=13,
            home_lineup=[{"player_id": 203, "player_name": "HBP"}]
            + [
                {"player_id": 400 + slot, "player_name": str(slot)}
                for slot in range(2, 10)
            ],
        )
        team_map = mapping()
        team_map.update({"Home": "AAA", "Away": "BBB"})
        raw = build_raw_game_features(
            target,
            FeatureHistory(
                history_data, slot_weights={slot: 1 / 9 for slot in range(1, 10)}
            ),
            team_map,
            {"2024-04-02": era_record(as_of="2024-04-02")},
        )
        hitter = raw["home"]["offense"]["players"][0]
        self.assertEqual(hitter["iso"], {"value": None, "sample_size": 0, "quality": 0.0})

    def test_history_exposes_canonical_date_game_player_team_indexes(self):
        rows = [
            pa(2, "2025-04-02", 10, 101, "field_out", xwoba=0.2),
            pa(1, "2025-04-01", 20, 201, "strikeout", xwoba=0.1, top="Bot"),
        ]
        history = FeatureHistory(
            data(list(reversed(rows))), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        self.assertEqual(tuple(history.pa_by_date), ("2025-04-01", "2025-04-02"))
        self.assertEqual(tuple(history.pa_by_game), (1, 2))
        self.assertEqual(tuple(history.pa_by_batter), (101, 201))
        self.assertEqual(tuple(history.pa_by_team), ("AWY", "HOM"))

    def test_builds_raw_metrics_boundaries_roles_outs_and_neutral_missing(self):
        rows = [
            pa(1, "2025-04-01", 10, 201, "strikeout", xwoba=0.10, outs=1),
            pa(1, "2025-04-01", 10, 202, "walk", woba=0.7, at_bat=2),
            pa(
                1,
                "2025-04-01",
                11,
                203,
                "home_run",
                xwoba=1.8,
                woba=2,
                lsa=6,
                inning=7,
                at_bat=3,
            ),
            pa(
                1,
                "2025-04-01",
                20,
                101,
                "field_out",
                hand="L",
                xwoba=0.05,
                outs=1,
                top="Bot",
                at_bat=4,
            ),
            pa(
                2, "2025-04-17", 10, 201, "field_out", xwoba=0.2, outs=1
            ),  # day-14 boundary
            pa(
                2,
                "2025-04-17",
                11,
                101,
                "double",
                xwoba=0.6,
                woba=1.25,
                lsa=6,
                inning=8,
                at_bat=2,
            ),
            pa(3, "2025-04-30", 10, 101, "strikeout", xwoba=0.1, outs=1),
            pa(3, "2025-04-30", 11, 102, "walk", woba=0.7, inning=8, at_bat=2),
            pa(
                4, "2025-03-31", 11, 103, "walk", woba=0.7, inning=8
            ),  # outside prior 30
        ]
        history = FeatureHistory(
            data(rows),
            slot_weights={i: 1 / 9 for i in range(1, 10)},
            schedule_history=[
                {
                    "game_pk": 3,
                    "official_date": "2025-04-30",
                    "team_codes": ["HOM", "AWY"],
                    "venue_city": "Home City",
                    "innings": 10,
                    "venue_latitude": 40.0,
                    "venue_longitude": -74.0,
                    "time_zone": "America/New_York",
                }
            ],
            schedule_coverage_start="2025-04-28",
            schedule_coverage_end="2025-04-30",
            schedule_coverage_complete=True,
        )
        result = build(game(), history)
        self.assertTrue(result["eligible"])
        self.assertNotIn("starter_edge", result)
        self.assertEqual(result["home"]["starter"]["fip_numerator"]["value"], -1.0)
        self.assertEqual(result["home"]["starter"]["outs"]["value"], 3)
        self.assertEqual(result["home"]["starter"]["season_xwoba"]["sample_size"], 4)
        self.assertIsNone(result["home"]["starter"]["velocity_change"]["value"])
        self.assertEqual(result["home"]["offense"]["last14_xwoba"]["sample_size"], 3)
        self.assertEqual(result["home"]["offense"]["players"][3]["missing"], True)
        self.assertEqual(result["home"]["bullpen"]["leverage_pool"]["sample_size"], 3)
        self.assertEqual(result["home"]["bullpen"]["availability"]["value"], 100.0)
        self.assertEqual(result["home"]["schedule"]["value"], 30.0)
        self.assertEqual(
            result["home"]["environment"],
            {
                "value": 50.0,
                "sample_size": 0,
                "quality": 0.0,
                "flags": ["environment_unavailable"],
            },
        )
        self.assertEqual(result["provenance"]["max_history_date"], "2025-04-30")
        self.assertTrue(result["provenance"]["statcast_target_excluded"])
        self.assertTrue(all(math.isfinite(x) for x in self._numbers(result)))

    def test_one_start_is_insufficient_for_velocity_change(self):
        rows = [pa(1, "2025-04-30", 10, 101, "field_out", xwoba=0.2, outs=1)]
        result = build(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
        )
        self.assertEqual(
            result["home"]["starter"]["velocity_change"],
            {
                "value": None,
                "sample_size": 1,
                "quality": 0.0,
                "baseline_sample_size": 0,
            },
        )

    def test_schedule_uses_available_venue_travel_fields(self):
        history = FeatureHistory(
            data([]),
            slot_weights={i: 1 / 9 for i in range(1, 10)},
            schedule_history=[
                {
                    "game_pk": 1,
                    "official_date": "2025-04-30",
                    "team_codes": ["HOM"],
                    "venue_name": "West",
                    "innings": 9,
                }
            ],
            schedule_coverage_start="2025-04-28",
            schedule_coverage_end="2025-04-30",
            schedule_coverage_complete=True,
            venue_by_name={
                "West": {
                    "city": "Los Angeles",
                    "latitude": 34.05,
                    "longitude": -118.24,
                    "time_zone": "America/Los_Angeles",
                },
                "Park": {
                    "city": "New York",
                    "latitude": 40.71,
                    "longitude": -74.01,
                    "time_zone": "America/New_York",
                },
            },
        )
        result = build(game(doubleheader_marker=None), history)
        self.assertEqual(result["home"]["schedule"]["value"], 34.0)
        self.assertEqual(result["home"]["schedule"]["quality"], 4 / 6)
        self.assertEqual(
            result["home"]["schedule"]["flags"],
            ["home_city_unsupported", "doubleheader_unsupported"],
        )

    def test_missing_participants_are_ineligible_with_explicit_reasons(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        result = build(game(home_probable_pitcher_id=None, away_lineup=[]), history)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reasons"], ["missing_away_lineup"])
        self.assertTrue(result["home_probable_starter_mismatch"])
        self.assertEqual(result["lineup_provenance"], "MISSING_OR_INVALID_LINEUP")

        duplicate = [dict(player) for player in game()["home_lineup"]]
        duplicate[-1]["player_id"] = duplicate[0]["player_id"]
        invalid = build(game(home_lineup=duplicate), history)
        self.assertEqual(invalid["lineup_provenance"], "MISSING_OR_INVALID_LINEUP")

    def test_team_mapping_requires_thirty_unique_nonempty_string_pairs(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        bad = mapping()
        bad["Home"] = 123
        with self.assertRaisesRegex(ValueError, "30-team mapping"):
            build(game(), history, bad)

    def test_ignores_target_rows_and_is_byte_deterministic_under_permutation(self):
        rows = [
            pa(1, "2025-04-30", 10, 101, "field_out", xwoba=0.2, outs=1),
            pa(
                1,
                "2025-04-30",
                20,
                201,
                "strikeout",
                xwoba=0.1,
                outs=1,
                top="Bot",
                at_bat=2,
            ),
        ]
        kwargs = {"slot_weights": {i: 1 / 9 for i in range(1, 10)}}
        a = build(game(), FeatureHistory(data(rows), **kwargs))
        b = build(game(), FeatureHistory(data(list(reversed(rows))), **kwargs))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        target = rows + [pa(900, "2025-05-01", 10, 101, "home_run", xwoba=2.0)]
        bounded = build(game(), FeatureHistory(data(target), **kwargs))
        self.assertEqual(bounded["home"], a["home"])
        self.assertEqual(bounded["away"], a["away"])

    @staticmethod
    def _numbers(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from FeatureEngineTest._numbers(item)
        elif isinstance(value, list):
            for item in value:
                yield from FeatureEngineTest._numbers(item)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            yield float(value)


if __name__ == "__main__":
    unittest.main()
