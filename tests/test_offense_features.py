import math
import unittest
from datetime import date

from mlb_backtest.features.offense import (
    WEIGHTS,
    derive_slot_weights,
    offense_score,
    select_prior_hitter_events,
    weighted_lineup_metric,
)


def lineup():
    return [
        {"player_id": 100 + slot, "batting_slot": slot}
        for slot in range(1, 10)
    ]


class OffenseFeaturesTest(unittest.TestCase):
    def test_frozen_subweights_and_golden_score(self):
        self.assertEqual(
            dict(WEIGHTS),
            {
                "platoon_xwoba": 0.50,
                "last14_xwoba": 0.20,
                "bb_minus_k": 0.15,
                "barrel_rate": 0.10,
                "iso": 0.05,
            },
        )
        scores = {
            "platoon_xwoba": 80,
            "last14_xwoba": 70,
            "bb_minus_k": 60,
            "barrel_rate": 50,
            "iso": 40,
        }
        self.assertEqual(offense_score(lineup(), scores), 70.0)

    def test_lineup_requires_nine_unique_integer_ids_and_ordered_slots(self):
        invalid_lineups = (
            lineup()[:8],
            [dict(row) for row in lineup()[:-1]] + [dict(lineup()[-1], player_id=101)],
            [dict(row) for row in lineup()[:-1]] + [dict(lineup()[-1], player_id=True)],
            [dict(row) for row in reversed(lineup())],
            [dict(row) for row in lineup()[:-1]] + [dict(lineup()[-1], batting_slot=8)],
        )
        valid_scores = {name: 50 for name in WEIGHTS}
        for rows in invalid_lineups:
            with self.subTest(rows=rows), self.assertRaisesRegex(
                ValueError, "nine starting hitters"
            ):
                offense_score(rows, valid_scores)

    def test_offense_score_validates_exact_finite_range_metrics(self):
        valid = {name: 50 for name in WEIGHTS}
        invalid_metrics = (
            dict(valid, extra=50),
            {name: value for name, value in valid.items() if name != "iso"},
            dict(valid, iso=False),
            dict(valid, iso=math.inf),
            dict(valid, iso=-1),
            dict(valid, iso=101),
        )
        for metrics in invalid_metrics:
            with self.subTest(metrics=metrics), self.assertRaises(ValueError):
                offense_score(lineup(), metrics)

    def test_recent_fourteen_day_window_excludes_target_and_includes_boundaries(self):
        events = [
            {"event_id": "old", "game_date": "2025-04-16"},
            {"event_id": "first", "game_date": "2025-04-17"},
            {"event_id": "last", "game_date": "2025-04-30"},
            {"event_id": "same-day", "game_date": "2025-05-01"},
        ]
        selected = select_prior_hitter_events(events, date(2025, 5, 1))
        self.assertEqual([row["event_id"] for row in selected], ["last", "first"])

    def test_recent_events_reject_invalid_dates_days_and_duplicate_ids(self):
        with self.assertRaises(ValueError):
            select_prior_hitter_events(
                [{"event_id": "x", "game_date": "bad"}], "2025-05-01"
            )
        with self.assertRaises(ValueError):
            select_prior_hitter_events(
                [
                    {"event_id": "x", "game_date": "2025-04-30"},
                    {"event_id": "x", "game_date": "2025-04-29"},
                ],
                "2025-05-01",
            )
        for days in (0, True, 1.5):
            with self.subTest(days=days), self.assertRaises(ValueError):
                select_prior_hitter_events([], "2025-05-01", days)

    def test_derive_slot_weights_uses_only_frozen_regular_seasons(self):
        rows = [
            {"season": 2022 + slot % 3, "game_type": "R", "batting_slot": slot, "pa": slot}
            for slot in range(1, 10)
        ]
        weights = derive_slot_weights(rows)
        self.assertEqual(set(weights), set(range(1, 10)))
        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertAlmostEqual(weights[9], 9 / 45)

    def test_derive_slot_weights_rejects_boundaries_missing_slots_and_bad_pa(self):
        valid = [
            {"season": 2024, "game_type": "R", "batting_slot": slot, "pa": 1}
            for slot in range(1, 10)
        ]
        invalid_sets = (
            valid + [{"season": 2025, "game_type": "R", "batting_slot": 1, "pa": 1}],
            valid + [{"season": 2021, "game_type": "R", "batting_slot": 1, "pa": 1}],
            valid + [{"season": 2024, "game_type": "P", "batting_slot": 1, "pa": 1}],
            valid[:-1],
            [dict(row, pa=0) if row["batting_slot"] == 1 else row for row in valid],
            [dict(row, pa=True) if row["batting_slot"] == 1 else row for row in valid],
        )
        for rows in invalid_sets:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                derive_slot_weights(rows)

    def test_weighted_lineup_metric_uses_neutral_without_redistribution(self):
        weights = {slot: slot / 45 for slot in range(1, 10)}
        player_metrics = {
            101: {"score": 100, "quality": 1.0},
            109: {"score": 0, "quality": 0.5},
        }
        score, quality = weighted_lineup_metric(
            lineup(), player_metrics, weights, neutral=50
        )
        self.assertAlmostEqual(score, (100 * 1 + 0 * 9 + 50 * 35) / 45)
        self.assertAlmostEqual(quality, (1.0 * 1 + 0.5 * 9) / 45)

    def test_weighted_lineup_metric_rejects_bad_scores_weights_and_neutral(self):
        weights = {slot: 1 / 9 for slot in range(1, 10)}
        invalid_calls = (
            ({101: {"score": True, "quality": 1}}, weights, 50),
            ({101: {"score": 50, "quality": math.nan}}, weights, 50),
            ({}, {**weights, 9: 0.5}, 50),
            ({}, weights, False),
        )
        for metrics, slot_weights, neutral in invalid_calls:
            with self.subTest(metrics=metrics, weights=slot_weights, neutral=neutral):
                with self.assertRaises(ValueError):
                    weighted_lineup_metric(lineup(), metrics, slot_weights, neutral)


if __name__ == "__main__":
    unittest.main()
