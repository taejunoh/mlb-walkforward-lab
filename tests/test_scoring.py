import math
import unittest
from unittest.mock import patch

from mlb_backtest.scoring import (
    WEIGHTS,
    component_agreement,
    component_edge,
    component_qualities,
    confidence_score,
    full_agreement,
    home_probability,
    overall_completeness,
    predicted_pick,
    tier,
    uncertainty,
)


COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")


class ScoringTest(unittest.TestCase):
    def test_frozen_weights_and_component_edge_golden(self):
        self.assertEqual(
            WEIGHTS,
            {
                "starter": 0.35,
                "offense": 0.25,
                "bullpen": 0.20,
                "environment": 0.10,
                "defense": 0.05,
                "schedule": 0.05,
            },
        )
        home = {name: 60 for name in COMPONENTS}
        away = {name: 50 for name in COMPONENTS}
        self.assertAlmostEqual(component_edge(home, away), 10.0)

    def test_component_edge_validates_exact_keys_and_scores(self):
        valid = {name: 50 for name in COMPONENTS}
        invalid_mappings = [
            {name: 50 for name in COMPONENTS if name != "starter"},
            dict(valid, extra=50),
            dict(valid, starter=True),
            dict(valid, starter=-0.01),
            dict(valid, starter=100.01),
            dict(valid, starter=math.nan),
            dict(valid, starter="50"),
        ]
        for bad in invalid_mappings:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                component_edge(bad, valid)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                component_edge(valid, bad)
        with self.assertRaises(ValueError):
            component_edge([], valid)

    def test_component_quality_weighted_mean_and_validation(self):
        qualities = {
            "starter": 1.0,
            "offense": 0.8,
            "bullpen": 0.6,
            "environment": 0.4,
            "defense": 0.2,
            "schedule": 0.0,
        }
        expected = 0.35 + 0.25 * 0.8 + 0.20 * 0.6 + 0.10 * 0.4 + 0.05 * 0.2
        self.assertAlmostEqual(component_qualities(qualities), expected)
        self.assertAlmostEqual(overall_completeness(qualities), expected)

        invalid = [
            {name: 1.0 for name in COMPONENTS if name != "defense"},
            dict(qualities, extra=0.5),
            dict(qualities, starter=True),
            dict(qualities, starter=-0.1),
            dict(qualities, starter=1.1),
            dict(qualities, starter=math.inf),
        ]
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                component_qualities(bad)

    def test_home_probability_delegates_to_frozen_normalizer(self):
        with patch("mlb_backtest.scoring.normalize.probability", return_value=0.72) as mocked:
            self.assertEqual(home_probability(12.0, -0.2, 1.5), 0.72)
        mocked.assert_called_once_with(12.0, -0.2, 1.5, clip_bounds=(0.10, 0.90))

    def test_home_probability_clips_and_rejects_nonfinite_inputs(self):
        self.assertEqual(home_probability(100.0, 0.0, 100.0), 0.90)
        self.assertEqual(home_probability(-100.0, 0.0, 100.0), 0.10)
        for args in ((math.nan, 0, 1), (0, math.inf, 1), (0, 0, False)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                home_probability(*args)

    def test_predicted_pick_chooses_home_or_away_around_half(self):
        just_home = math.nextafter(0.5, 1.0)
        just_away = math.nextafter(0.5, 0.0)
        self.assertEqual(
            predicted_pick("Home", "Away", just_home),
            {
                "predicted_side": "home",
                "predicted_winner": "Home",
                "pick_probability": just_home,
            },
        )
        self.assertEqual(
            predicted_pick("Home", "Away", just_away),
            {
                "predicted_side": "away",
                "predicted_winner": "Away",
                "pick_probability": 1.0 - just_away,
            },
        )

    def test_predicted_pick_exact_half_is_explicit_no_pick(self):
        self.assertEqual(
            predicted_pick("Home", "Away", 0.50),
            {
                "predicted_side": "none",
                "predicted_winner": "",
                "pick_probability": 0.50,
            },
        )
        edges = {"starter": 2.0, "offense": 1.0, "bullpen": 0.5}
        self.assertEqual(component_agreement(edges, "none"), 0.0)
        self.assertEqual(full_agreement(edges, "none", "Low"), "No")
        self.assertEqual(tier(0.5, 10.0, "Low"), "Toss-up")

    def test_predicted_pick_validates_inputs(self):
        for args in (
            ("", "Away", 0.5),
            ("Home", 1, 0.5),
            ("Home", "Away", True),
            ("Home", "Away", 1.01),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                predicted_pick(*args)

    def test_agreement_fraction_uses_three_strict_signs(self):
        edges = {"starter": 2.0, "offense": 0.0, "bullpen": -1.0}
        self.assertAlmostEqual(component_agreement(edges, "home"), 1 / 3)
        self.assertAlmostEqual(component_agreement(edges, "away"), 1 / 3)
        for bad_edges in (dict(edges, defense=1), dict(edges, starter=math.nan)):
            with self.assertRaises(ValueError):
                component_agreement(bad_edges, "home")
        with self.assertRaises(ValueError):
            component_agreement(edges, "HOME")

    def test_confidence_exact_formula_clipping_and_validation(self):
        self.assertEqual(confidence_score(1, 1, 1, 1, 1, 1, 1), 10.0)
        self.assertEqual(confidence_score(0, 0, 0, 0, 0, 0, 0), 0.0)
        self.assertAlmostEqual(
            confidence_score(0.8, 0.7, 0.6, 0.5, 2 / 3, 0.4, 0.2),
            2.5 * 0.8 + 2 * 0.7 + 1.5 * 0.6 + 1.5 * 0.5 + 1.5 * (2 / 3) + (2 * 0.4 + 0.2) / 3,
        )
        for index in range(7):
            args = [0.5] * 7
            args[index] = math.nan if index else True
            with self.subTest(index=index), self.assertRaises(ValueError):
                confidence_score(*args)
        with self.assertRaises(ValueError):
            confidence_score(1.01, 0, 0, 0, 0, 0, 0)

    def test_numeric_conversion_overflow_is_a_value_error(self):
        with self.assertRaises(ValueError):
            home_probability(10**1000, 0.0, 1.0)

    def test_uncertainty_boundaries_and_validation(self):
        self.assertEqual(uncertainty(0.749999, False, 500, False), "High")
        self.assertEqual(uncertainty(0.75, False, 500, False), "Medium")
        self.assertEqual(uncertainty(0.899999, False, 500, False), "Medium")
        self.assertEqual(uncertainty(0.90, False, 149, False), "Medium")
        self.assertEqual(uncertainty(0.90, False, 150, True), "Medium")
        self.assertEqual(uncertainty(0.90, False, 150, False), "Low")
        self.assertEqual(uncertainty(1.0, True, 500, False), "High")
        invalid = [
            (-0.1, False, 1, False),
            (0.9, 0, 1, False),
            (0.9, False, -1, False),
            (0.9, False, True, False),
            (0.9, False, 1, 0),
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                uncertainty(*args)

    def test_tier_all_boundaries_and_validation(self):
        cases = [
            ((0.90, 10.0, "High"), "Toss-up"),
            ((0.40, 10.0, "Low"), "Toss-up"),
            ((0.539999, 10.0, "Low"), "Toss-up"),
            ((0.54, 7.99, "Low"), "Lean"),
            ((0.579999, 10.0, "Low"), "Lean"),
            ((0.58, 8.0, "Low"), "B"),
            ((0.63, 8.499999, "Low"), "B"),
            ((0.63, 8.5, "Medium"), "A"),
            ((0.68, 8.999999, "Low"), "A"),
            ((0.68, 9.0, "Low"), "S"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(tier(*args), expected)
        for args in ((True, 9, "Low"), (0.6, 11, "Low"), (0.6, 9, "unknown")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                tier(*args)

    def test_full_agreement_requires_all_three_and_not_high(self):
        edges = {"starter": 2.0, "offense": 1.0, "bullpen": 0.5}
        self.assertEqual(full_agreement(edges, "home", "Low"), "Yes")
        self.assertEqual(full_agreement(edges, "home", "High"), "No")
        self.assertEqual(full_agreement(dict(edges, bullpen=0.0), "home", "Low"), "No")
        self.assertEqual(
            full_agreement(
                {key: -value for key, value in edges.items()}, "away", "Medium"
            ),
            "Yes",
        )
        with self.assertRaises(ValueError):
            full_agreement(edges, "home", "unknown")


if __name__ == "__main__":
    unittest.main()
