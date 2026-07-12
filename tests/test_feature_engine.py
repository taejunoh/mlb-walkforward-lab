import json
import math
import unittest
from mlb_backtest.feature_engine import (
    FeatureHistory,
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
    post_home=0,
    post_away=0,
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
        "release_speed": 95.0,
        "woba_value": woba,
        "woba_denom": denom,
        "estimated_woba_using_speedangle": xwoba,
        "launch_speed_angle": lsa,
        "at_bat_number": at_bat,
        "pitch_number": 1,
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


class FeatureEngineTest(unittest.TestCase):
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
        result = build_raw_game_features(game(), history, mapping())
        self.assertTrue(result["eligible"])
        self.assertNotIn("starter_edge", result)
        self.assertEqual(result["home"]["starter"]["fip_numerator"]["value"], -1.0)
        self.assertEqual(result["home"]["starter"]["outs"]["value"], 3)
        self.assertEqual(result["home"]["starter"]["season_xwoba"]["sample_size"], 4)
        self.assertIsNotNone(result["home"]["starter"]["velocity_change"]["value"])
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
        result = build_raw_game_features(
            game(),
            FeatureHistory(data(rows), slot_weights={i: 1 / 9 for i in range(1, 10)}),
            mapping(),
        )
        self.assertEqual(
            result["home"]["starter"]["velocity_change"],
            {"value": None, "sample_size": 1, "quality": 0.0},
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
        result = build_raw_game_features(
            game(doubleheader_marker=None), history, mapping()
        )
        self.assertEqual(result["home"]["schedule"]["value"], 34.0)
        self.assertEqual(result["home"]["schedule"]["quality"], 1.0)
        self.assertEqual(result["home"]["schedule"]["flags"], [])

    def test_missing_participants_are_ineligible_with_explicit_reasons(self):
        history = FeatureHistory(
            data([]), slot_weights={i: 1 / 9 for i in range(1, 10)}
        )
        result = build_raw_game_features(
            game(home_probable_pitcher_id=None, away_lineup=[]), history, mapping()
        )
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
            build_raw_game_features(game(), history, bad)

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
        a = build_raw_game_features(
            game(), FeatureHistory(data(rows), **kwargs), mapping()
        )
        b = build_raw_game_features(
            game(), FeatureHistory(data(list(reversed(rows))), **kwargs), mapping()
        )
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))
        target = rows + [pa(900, "2025-05-01", 10, 101, "home_run", xwoba=2.0)]
        with self.assertRaisesRegex(ValueError, "target game_pk|strictly before"):
            build_raw_game_features(
                game(), FeatureHistory(data(target), **kwargs), mapping()
            )

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
