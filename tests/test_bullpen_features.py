import math
import unittest
from datetime import date

from mlb_backtest.features.bullpen import (
    WEIGHTS,
    bullpen_score,
    leverage_pool,
    reliever_availability,
    team_availability,
)


class BullpenFeaturesTest(unittest.TestCase):
    def test_availability_thresholds(self):
        self.assertEqual(reliever_availability([25, 0, 0]), 0.0)
        self.assertEqual(reliever_availability([18, 0, 0]), 0.5)
        self.assertEqual(reliever_availability([0, 0, 0]), 1.0)
        self.assertEqual(reliever_availability([10, 10, 10]), 0.0)
        self.assertEqual(reliever_availability([0, 20, 20]), 1.0)
        self.assertEqual(reliever_availability([0, 30, 0]), 0.5)

    def test_availability_pads_short_history_and_rejects_invalid_usage(self):
        self.assertEqual(reliever_availability([]), 1.0)
        self.assertEqual(reliever_availability([15]), 0.5)
        for invalid in ([1, 2, 3, 4], [-1], [True], [1.5], "10"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                reliever_availability(invalid)

    def test_leverage_pool_uses_prior_thirty_dates_and_deterministic_ties(self):
        events = [
            self.event("old", "2025-03-31", 9, 0, 99, 100),
            self.event("first", "2025-04-01", 7, 3, 6, 4),
            self.event("p1-a", "2025-04-15", 8, -2, 1, 5),
            self.event("p1-b", "2025-04-30", 9, 1, 1, 2),
            self.event("p2", "2025-04-30", 7, 0, 2, 7),
            self.event("p3", "2025-04-30", 6, 0, 3, 20),
            self.event("p4", "2025-04-30", 7, 4, 4, 20),
            self.event("p5", "2025-04-30", 7, 0, 5, 3),
            self.event("p7", "2025-04-30", 7, 0, 7, 2),
            self.event("p8", "2025-04-30", 7, 0, 8, 1),
            self.event("same", "2025-05-01", 8, 0, 10, 100),
        ]
        expected = [
            {"pitcher_id": 1, "batters_faced": 7},
            {"pitcher_id": 2, "batters_faced": 7},
            {"pitcher_id": 6, "batters_faced": 4},
            {"pitcher_id": 5, "batters_faced": 3},
            {"pitcher_id": 7, "batters_faced": 2},
        ]
        self.assertEqual(leverage_pool(events, date(2025, 5, 1)), expected)
        self.assertEqual(leverage_pool(list(reversed(events)), "2025-05-01"), expected)

    def test_leverage_pool_rejects_duplicate_ids_and_invalid_fields(self):
        duplicate = [
            self.event("x", "2025-04-30", 7, 0, 1, 1),
            self.event("x", "2025-03-01", 7, 0, 2, 1),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate event_id"):
            leverage_pool(duplicate, "2025-05-01")

        valid = self.event("x", "2025-04-30", 7, 0, 1, 1)
        invalid_events = (
            dict(valid, inning=True),
            dict(valid, score_margin=math.nan),
            dict(valid, pitcher_id=True),
            dict(valid, batters_faced=-1),
            dict(valid, is_reliever=1),
        )
        for event in invalid_events:
            with self.subTest(event=event), self.assertRaises(ValueError):
                leverage_pool([event], "2025-05-01")

    def test_team_availability_is_bf_weighted_with_neutral_missing_usage(self):
        pool = [
            {"pitcher_id": 1, "batters_faced": 6},
            {"pitcher_id": 2, "batters_faced": 3},
            {"pitcher_id": 3, "batters_faced": 1},
        ]
        score, quality = team_availability(pool, {1: [0], 2: [25, 0, 0]})
        self.assertEqual(score, 65.0)
        self.assertEqual(quality, 0.9)
        self.assertEqual(team_availability([], {}), (50.0, 0.0))

    def test_team_availability_rejects_bad_pool_and_usage(self):
        invalid_calls = (
            ([{"pitcher_id": 1, "batters_faced": 0}], {}),
            ([{"pitcher_id": True, "batters_faced": 1}], {}),
            (
                [
                    {"pitcher_id": 1, "batters_faced": 1},
                    {"pitcher_id": 1, "batters_faced": 2},
                ],
                {},
            ),
            ([{"pitcher_id": 1, "batters_faced": 1}], {True: [0]}),
        )
        for pool, usage in invalid_calls:
            with self.subTest(pool=pool, usage=usage), self.assertRaises(ValueError):
                team_availability(pool, usage)

    def test_bullpen_frozen_weights_score_and_quality(self):
        self.assertEqual(
            dict(WEIGHTS),
            {
                "season_xwoba": 0.35,
                "k_minus_bb": 0.20,
                "last14_xwoba": 0.15,
                "availability": 0.30,
            },
        )
        parts = {
            "season_xwoba": {"score": 80, "quality": 1.0},
            "k_minus_bb": {"score": 70, "quality": 0.8},
            "last14_xwoba": {"score": 60, "quality": 0.6},
            "availability": {"score": 50, "quality": 0.4},
        }
        self.assertEqual(bullpen_score(parts), (66.0, 0.72))

    def test_bullpen_score_requires_exact_finite_bounded_parts(self):
        valid = {name: {"score": 50, "quality": 1} for name in WEIGHTS}
        invalid = (
            dict(valid, extra={"score": 50, "quality": 1}),
            {name: value for name, value in valid.items() if name != "availability"},
            dict(valid, availability={"score": True, "quality": 1}),
            dict(valid, availability={"score": math.inf, "quality": 1}),
            dict(valid, availability={"score": 50, "quality": -0.1}),
            dict(valid, availability={"score": 50, "quality": 1, "extra": 0}),
        )
        for parts in invalid:
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                bullpen_score(parts)

    @staticmethod
    def event(event_id, game_date, inning, score_margin, pitcher_id, batters_faced):
        return {
            "event_id": event_id,
            "game_date": game_date,
            "inning": inning,
            "score_margin": score_margin,
            "pitcher_id": pitcher_id,
            "batters_faced": batters_faced,
            "is_reliever": True,
        }


if __name__ == "__main__":
    unittest.main()
