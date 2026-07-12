import json
import math
import time
import unittest
from mlb_backtest.feature_engine import (
    FeatureHistory,
    ScheduleHistory,
    build_raw_game_features,
    pa_expected_woba,
)
from mlb_backtest.statcast import StatcastData


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


def build(target, history, team_map=None, league_era=None):
    return build_raw_game_features(
        target,
        history,
        mapping() if team_map is None else team_map,
        {2025: 4.0} if league_era is None else league_era,
    )


class FeatureEngineTest(unittest.TestCase):
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
            league_era={2025: 4.25},
        )
        starter = result["home"]["starter"]
        self.assertEqual(starter["league_era"]["value"], 4.25)
        self.assertAlmostEqual(starter["league_raw_fip"]["value"], 1.5)
        self.assertAlmostEqual(starter["league_cfip"]["value"], 2.75)
        self.assertAlmostEqual(starter["fip"]["value"], 5.75)
        numeric = build_raw_game_features(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
            mapping(),
            league_era=4.25,
        )
        self.assertEqual(numeric["home"]["starter"]["fip"]["value"], 5.75)

    def test_missing_or_invalid_league_era_is_explicitly_ineligible(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        missing = build_raw_game_features(game(), history, mapping(), {})
        invalid = build_raw_game_features(game(), history, mapping(), {2025: math.nan})
        self.assertEqual(missing["reasons"], ["missing_league_era"])
        self.assertEqual(invalid["reasons"], ["invalid_league_era"])
        self.assertFalse(missing["eligible"])
        self.assertNotIn("home", missing)

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
            ({"home_probable_pitcher_id": 0}, "missing_home_starter"),
            ({"home_lineup": duplicate_lineup}, "invalid_home_lineup"),
            ({"away_lineup": game()["away_lineup"][:8]}, "invalid_away_lineup"),
        )
        for changes, reason in cases:
            with self.subTest(changes=changes):
                result = build(game(**changes), history)
                self.assertFalse(result["eligible"])
                self.assertIn(reason, result["reasons"])

    def test_asserts_pregame_boundary_for_plate_appearances_and_provenance(self):
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
        with self.assertRaisesRegex(ValueError, "target game_pk|strictly before"):
            build(game(), poisoned)

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
            game(away_probable_pitcher_id=999),
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
            )
            return build(game(doubleheader_marker=None), history)["home"]["schedule"][
                "value"
            ]

        self.assertEqual(score(("HOM", "HOM", "HOM")), 55.0)
        self.assertEqual(score(("HOM", "OPP", "HOM")), 50.0)

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
        schedule = ScheduleHistory(rows)
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
        self.assertTrue(result["provenance"]["target_excluded"])
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
        self.assertEqual(result["home"]["schedule"]["quality"], 1.0)
        self.assertEqual(result["home"]["schedule"]["flags"], [])

    def test_missing_participants_are_ineligible_with_explicit_reasons(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        result = build(game(home_probable_pitcher_id=None, away_lineup=[]), history)
        self.assertFalse(result["eligible"])
        self.assertEqual(
            result["reasons"], ["missing_home_starter", "missing_away_lineup"]
        )

    def test_team_mapping_requires_thirty_unique_nonempty_string_pairs(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        bad = mapping()
        bad["Home"] = 123
        with self.assertRaisesRegex(ValueError, "30-team mapping"):
            build(game(), history, bad)

    def test_rejects_target_rows_and_is_byte_deterministic_under_permutation(self):
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
        with self.assertRaisesRegex(ValueError, "target game_pk|strictly before"):
            build(game(), FeatureHistory(data(target), **kwargs))

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
