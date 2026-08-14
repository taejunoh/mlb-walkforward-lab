import math
import unittest

from mlb_backtest.features.context import (
    DEFENSE_WEIGHTS,
    ENVIRONMENT_WEIGHTS,
    defense_score,
    environment_score,
    schedule_score,
)


class ContextFeaturesTest(unittest.TestCase):
    def test_schedule_adjustments_are_clipped(self):
        self.assertEqual(
            schedule_score(
                rest_day=True,
                home_days=3,
                prior_innings=9,
                miles=0,
                time_zones=0,
                doubleheader=False,
            ),
            67.0,
        )
        self.assertEqual(
            schedule_score(
                rest_day=False,
                home_days=0,
                prior_innings=12,
                miles=1200,
                time_zones=3,
                doubleheader=True,
            ),
            14.0,
        )

    def test_rest_day_suppresses_travel_penalties_and_score_clips(self):
        self.assertEqual(
            schedule_score(
                rest_day=True,
                home_days=0,
                prior_innings=9,
                miles=2000,
                time_zones=4,
                doubleheader=False,
            ),
            62.0,
        )
        self.assertEqual(
            schedule_score(
                rest_day=False,
                home_days=0,
                prior_innings=10,
                miles=1000,
                time_zones=2,
                doubleheader=True,
            ),
            14.0,
        )

    def test_schedule_rejects_bool_nonfinite_and_negative_values(self):
        valid = dict(
            rest_day=False,
            home_days=0,
            prior_innings=9,
            miles=0,
            time_zones=0,
            doubleheader=False,
        )
        invalid = (
            dict(valid, rest_day=0),
            dict(valid, doubleheader=1),
            dict(valid, home_days=True),
            dict(valid, prior_innings=-1),
            dict(valid, miles=math.nan),
            dict(valid, miles=-1),
            dict(valid, time_zones=1.5),
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                schedule_score(**values)

    def test_environment_weights_neutral_missing_and_partial_no_rescale(self):
        self.assertEqual(dict(ENVIRONMENT_WEIGHTS), {"park": 0.60, "weather": 0.40})
        self.assertEqual(environment_score(None, None), (50.0, 0.0))
        self.assertEqual(
            environment_score({"score": 80, "quality": 1.0}, None),
            (68.0, 0.6),
        )

    def test_defense_weights_neutral_missing_and_partial_no_rescale(self):
        self.assertEqual(
            dict(DEFENSE_WEIGHTS),
            {"oaa": 0.55, "framing": 0.30, "catcher": 0.15},
        )
        self.assertEqual(defense_score(None, None, None), (50.0, 0.0))
        self.assertEqual(
            defense_score({"score": 100, "quality": 1.0}, None, None),
            (77.5, 0.55),
        )

    def test_context_components_reject_nonfinite_bool_range_and_shape(self):
        invalid_parts = (
            {"score": True, "quality": 1},
            {"score": math.inf, "quality": 1},
            {"score": -1, "quality": 1},
            {"score": 101, "quality": 1},
            {"score": 50, "quality": math.nan},
            {"score": 50, "quality": -0.1},
            {"score": 50, "quality": 1.1},
            {"score": 50},
            {"score": 50, "quality": 1, "extra": 0},
        )
        for part in invalid_parts:
            with self.subTest(part=part):
                with self.assertRaises(ValueError):
                    environment_score(part, None)
                with self.assertRaises(ValueError):
                    defense_score(part, None, None)


if __name__ == "__main__":
    unittest.main()
