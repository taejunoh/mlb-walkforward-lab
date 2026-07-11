import copy
import itertools
import math
import unittest

from mlb_backtest.ranking import rank_daily


def row(game_pk, probability=0.60, confidence=8.5, starter_edge=5.0, start="2025-05-01T23:00:00Z"):
    return {
        "game_pk": game_pk,
        "official_date": "2025-05-01",
        "pick_probability": probability,
        "confidence": confidence,
        "starter_edge_abs": starter_edge,
        "scheduled_start_utc": start,
    }


class RankingTest(unittest.TestCase):
    def test_empty_is_allowed(self):
        self.assertEqual(rank_daily([]), [])

    def test_frozen_sort_order_and_flags(self):
        rows = [
            row(6, 0.6001, 1, 1, "2025-05-01T20:00:00Z"),
            row(5, 0.60, 9, 99, "2025-05-01T18:00:00Z"),
            row(4, 0.60, 8.5, 6, "2025-05-01T22:00:00Z"),
            row(3, 0.60, 8.5, 5, "2025-05-01T20:00:01Z"),
            row(2, 0.60, 8.5, 5, "2025-05-01T20:00:00Z"),
            row(1, 0.60, 8.5, 5, "2025-05-01T20:00:00Z"),
        ]
        ranked = rank_daily(rows)
        self.assertEqual([item["game_pk"] for item in ranked], [6, 5, 4, 1, 2, 3])
        self.assertEqual([item["daily_rank"] for item in ranked], [1, 2, 3, 4, 5, 6])
        self.assertEqual([item["top1"] for item in ranked], ["Yes", "No", "No", "No", "No", "No"])
        self.assertEqual(
            [item["top3"] for item in ranked],
            ["Yes", "Yes", "Yes", "No", "No", "No"],
        )

    def test_raw_precision_not_display_rounding_controls_rank(self):
        ranked = rank_daily([row(1, 0.6000001, 10), row(2, 0.6000002, 0)])
        self.assertEqual([item["game_pk"] for item in ranked], [2, 1])

    def test_permutations_are_deterministic_and_input_is_not_mutated(self):
        rows = [row(3), row(1), row(2)]
        before = copy.deepcopy(rows)
        expected = [1, 2, 3]
        for permutation in itertools.permutations(rows):
            self.assertEqual([item["game_pk"] for item in rank_daily(list(permutation))], expected)
        self.assertEqual(rows, before)
        ranked = rank_daily(rows)
        self.assertTrue(all(result is not source for result in ranked for source in rows))

    def test_one_top1_and_up_to_three_top3(self):
        for count in (1, 2, 3, 4):
            ranked = rank_daily([row(index) for index in range(1, count + 1)])
            with self.subTest(count=count):
                self.assertEqual(sum(item["top1"] == "Yes" for item in ranked), 1)
                self.assertEqual(sum(item["top3"] == "Yes" for item in ranked), min(3, count))

    def test_rejects_mixed_dates_duplicate_or_invalid_game_pk(self):
        mixed = row(2)
        mixed["official_date"] = "2025-05-02"
        invalid_sets = [
            [row(1), mixed],
            [row(1), row(1)],
            [row(0)],
            [row(True)],
        ]
        for rows in invalid_sets:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                rank_daily(rows)

    def test_rejects_missing_and_invalid_sort_fields(self):
        missing = row(1)
        del missing["confidence"]
        invalid_rows = [
            missing,
            row(1, True),
            row(1, 0.4999),
            row(1, 0.9001),
            row(1, math.nan),
            row(1, 0.6, -0.1),
            row(1, 0.6, 10.01),
            row(1, 0.6, True),
            row(1, starter_edge=-0.1),
            row(1, starter_edge=math.inf),
            row(1, starter_edge=False),
            row(1, start="2025-05-01T23:00:00"),
            row(1, start="2025-05-01T19:00:00-04:00"),
            row(1, start="bad"),
        ]
        malformed_date = row(1)
        malformed_date["official_date"] = "05/01/2025"
        invalid_rows.append(malformed_date)
        for bad in invalid_rows:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rank_daily([bad])

    def test_rejects_every_unknown_top_level_key(self):
        forbidden = (
            {"market": "ML"},
            {"closing_moneyline": -120},
            {"closing_line": -1.5},
            {"home_price": -110},
            {"vig": 0.05},
            {"favorite": "home"},
            {"arbitrary": "value"},
            {"metadata": {"sportsbook_odds": -110}},
            {"payload": [{"anything": "nested"}]},
        )
        for extra in forbidden:
            bad = row(1)
            bad.update(extra)
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                rank_daily([bad])

    def test_output_contains_only_safe_ranking_metadata(self):
        ranked = rank_daily([row(1)])
        self.assertEqual(
            set(ranked[0]),
            {
                "game_pk",
                "official_date",
                "pick_probability",
                "confidence",
                "starter_edge_abs",
                "scheduled_start_utc",
                "daily_rank",
                "top1",
                "top3",
            },
        )

    def test_requires_rows_to_be_mappings(self):
        with self.assertRaises(ValueError):
            rank_daily([row(1), "not a row"])
        with self.assertRaises(ValueError):
            rank_daily(None)


if __name__ == "__main__":
    unittest.main()
