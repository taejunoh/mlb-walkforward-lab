import math
import unittest
from datetime import date

from mlb_backtest.features.starter import (
    WEIGHTS,
    expected_innings,
    select_prior_starts,
    starter_score,
)


class StarterFeaturesTest(unittest.TestCase):
    def test_frozen_subweights_and_golden_score(self):
        self.assertEqual(
            dict(WEIGHTS),
            {
                "season_xwoba": 0.35,
                "k_minus_bb": 0.20,
                "fip": 0.15,
                "last5_xwoba": 0.15,
                "expected_ip": 0.10,
                "velo_change": 0.05,
            },
        )
        scores = {
            "season_xwoba": 80,
            "k_minus_bb": 70,
            "fip": 60,
            "last5_xwoba": 50,
            "expected_ip": 40,
            "velo_change": 30,
        }
        self.assertAlmostEqual(starter_score(scores), 64.0)

    def test_score_requires_exact_keys_and_finite_non_bool_range_values(self):
        valid = {name: 50 for name in WEIGHTS}
        for invalid in (
            dict(valid, extra=50),
            {name: value for name, value in valid.items() if name != "fip"},
            dict(valid, fip=True),
            dict(valid, fip=math.nan),
            dict(valid, fip=101),
            dict(valid, fip=-1),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                starter_score(invalid)

    def test_prior_starts_exclude_target_day_dedupe_and_order_ties(self):
        events = [
            {"game_pk": 1, "game_date": "2025-04-27", "is_starter": True},
            {"game_pk": 2, "game_date": "2025-04-30", "is_starter": True},
            {"game_pk": 3, "game_date": "2025-04-30", "is_starter": True},
            {"game_pk": 3, "game_date": "2025-04-29", "is_starter": True},
            {"game_pk": 4, "game_date": "2025-05-01", "is_starter": True},
            {"game_pk": 5, "game_date": "2025-04-28", "is_starter": False},
            {"game_pk": 6, "game_date": "2025-04-26", "is_starter": True},
            {"game_pk": 7, "game_date": "2025-04-25", "is_starter": True},
            {"game_pk": 8, "game_date": "2025-04-24", "is_starter": True},
        ]
        selected = select_prior_starts(events, date(2025, 5, 1), limit=10)
        self.assertEqual([row["game_pk"] for row in selected], [3, 2, 1, 6, 7])

    def test_prior_starts_validate_dates_and_limit(self):
        with self.assertRaises(ValueError):
            select_prior_starts([], "May 1, 2025")
        with self.assertRaises(ValueError):
            select_prior_starts(
                [{"game_pk": 1, "game_date": "bad", "is_starter": False}],
                "2025-05-01",
            )
        for invalid in (0, -1, True, 1.5):
            with self.subTest(limit=invalid), self.assertRaises(ValueError):
                select_prior_starts([], "2025-05-01", invalid)

    def test_expected_innings_zero_and_small_samples(self):
        zero = expected_innings([], rest_days=5)
        self.assertEqual(zero.value, 5.0)
        self.assertTrue(zero.small_sample)

        two = expected_innings(
            [
                {"outs": 15, "pitch_count": 90},
                {"outs": 12, "pitch_count": 90},
            ],
            rest_days=5,
        )
        self.assertAlmostEqual(two.value, 4.8)
        self.assertTrue(two.small_sample)

    def test_expected_innings_uses_latest_five_and_adjustments(self):
        starts = [
            {"outs": 21, "pitch_count": 60},
            {"outs": 18, "pitch_count": 65},
            {"outs": 15, "pitch_count": 69},
            {"outs": 12, "pitch_count": 70},
            {"outs": 9, "pitch_count": 75},
            {"outs": 3, "pitch_count": 1},
        ]
        result = expected_innings(starts, rest_days=3)
        self.assertEqual(result.value, 3.5)
        self.assertFalse(result.small_sample)

    def test_expected_innings_clips_baseline_and_final_value(self):
        high = expected_innings(
            [{"outs": 27, "pitch_count": 100}] * 5,
            rest_days=5,
        )
        self.assertEqual(high.value, 7.0)
        low = expected_innings(
            [{"outs": 3, "pitch_count": 20}] * 5,
            rest_days=2,
        )
        self.assertEqual(low.value, 2.0)

    def test_expected_innings_rejects_invalid_numeric_inputs(self):
        invalid_starts = (
            [{"outs": True, "pitch_count": 90}],
            [{"outs": 18.5, "pitch_count": 90}],
            [{"outs": 18, "pitch_count": math.inf}],
        )
        for starts in invalid_starts:
            with self.subTest(starts=starts), self.assertRaises(ValueError):
                expected_innings(starts, 5)
        for rest in (True, math.nan, -1):
            with self.subTest(rest=rest), self.assertRaises(ValueError):
                expected_innings([], rest)


if __name__ == "__main__":
    unittest.main()
