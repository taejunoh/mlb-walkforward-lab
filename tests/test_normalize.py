import math
import unittest

import numpy as np

from mlb_backtest.normalize import (
    FROZEN_TRAINING_START,
    FROZEN_TRAINING_END,
    _logistic_objective,
    clip,
    fit_frozen_calibration,
    fit_logistic,
    metric_score,
    probability,
    shrink,
    sigmoid,
)


class NormalizeTest(unittest.TestCase):
    def test_frozen_training_start_and_metadata(self):
        self.assertEqual(FROZEN_TRAINING_START, "2022-01-01")
        rows = [
            {"official_date": "2022-01-01", "edge": -1, "home_win": 0},
            {"official_date": "2022-01-02", "edge": 1, "home_win": 1},
        ]
        _, _, metadata = fit_frozen_calibration(rows)
        self.assertEqual(metadata["training_min_date"], "2022-01-01")
        self.assertEqual(metadata["training_max_date"], "2022-01-02")
        self.assertEqual(metadata["training_count"], 2)

        earlier_row = dict(rows[0], official_date="2021-12-31")
        with self.assertRaises(ValueError):
            fit_frozen_calibration([earlier_row, rows[1]])
        with self.assertRaises(ValueError):
            fit_frozen_calibration(
                rows,
                training_start="2021-01-01",
            )

    def test_frozen_training_end_cannot_be_overridden_later(self):
        self.assertEqual(FROZEN_TRAINING_END, "2024-12-31")
        rows = [
            {"official_date": "2025-01-01", "edge": -1, "home_win": 0},
            {"official_date": "2025-01-02", "edge": 1, "home_win": 1},
        ]
        with self.assertRaises(ValueError):
            fit_frozen_calibration(rows, training_end="2025-12-31")
        with self.assertRaises(ValueError):
            fit_frozen_calibration(
                [
                    {"official_date": "2024-01-01", "edge": -1, "home_win": 0},
                    {"official_date": "2024-01-02", "edge": 1, "home_win": 1},
                ],
                training_end="2025-12-31",
            )
        alpha, beta, metadata = fit_frozen_calibration(
            [
                {"official_date": "2023-12-30", "edge": -1, "home_win": 0},
                {"official_date": "2023-12-31", "edge": 1, "home_win": 1},
            ],
            training_end="2023-12-31",
        )
        self.assertTrue(math.isfinite(alpha))
        self.assertGreater(beta, 0.0)
        self.assertEqual(metadata["training_max_date"], "2023-12-31")

    def test_numeric_goldens(self):
        self.assertEqual(clip(3.0, 0.0, 2.0), 2.0)
        self.assertAlmostEqual(shrink(0.320, 150, 0.300, 150), 0.310)
        self.assertAlmostEqual(metric_score(0.310, 0.300, 0.010, -1), 30.0)
        self.assertAlmostEqual(sigmoid(0.0), 0.5)

    def test_shrink_zero_sample_returns_mean(self):
        self.assertEqual(shrink(999.0, 0, 0.3, 10), 0.3)

    def test_shrink_reliability_avoids_overflow(self):
        self.assertEqual(shrink(1.0, 1e308, 0.0, 1e308), 0.5)

    def test_metric_score_direction_and_clipping(self):
        self.assertEqual(metric_score(1.0, 0.0, 1.0, 1), 70.0)
        self.assertEqual(metric_score(1.0, 0.0, 1.0, -1), 30.0)
        self.assertEqual(metric_score(100.0, 0.0, 1.0, 1), 100.0)
        self.assertEqual(metric_score(-100.0, 0.0, 1.0, 1), 0.0)

    def test_numeric_helpers_reject_invalid_nonfinite_and_bool_inputs(self):
        invalid_calls = [
            (clip, (True, 0, 1)),
            (clip, (0, False, 1)),
            (clip, (0, 0, math.inf)),
            (clip, (0, 2, 1)),
            (shrink, (True, 1, 0, 1)),
            (shrink, (1, False, 0, 1)),
            (shrink, (1, -1, 0, 1)),
            (shrink, (1, 1, math.nan, 1)),
            (shrink, (1, 1, 0, 0)),
            (metric_score, (1, 0, 1, True)),
            (metric_score, (1, 0, 0, 1)),
            (metric_score, (1, 0, math.inf, 1)),
            (metric_score, (1, 0, 1, 0)),
            (sigmoid, (False,)),
            (sigmoid, (math.nan,)),
        ]
        for function, args in invalid_calls:
            with self.subTest(function=function.__name__, args=args):
                with self.assertRaises(ValueError):
                    function(*args)

    def test_sigmoid_is_stable_for_extreme_values(self):
        self.assertEqual(sigmoid(1000.0), 1.0)
        self.assertEqual(sigmoid(-1000.0), 0.0)

    def test_logistic_is_deterministic_and_symmetric(self):
        x_values = [-2.0, -1.0, 1.0, 2.0]
        y_values = [0, 0, 1, 1]
        first = fit_logistic(x_values, y_values)
        second = fit_logistic(x_values, y_values)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first[0], 0.0, places=9)
        self.assertGreater(first[1], 0.0)
        self.assertTrue(all(math.isfinite(value) for value in first))

    def test_logistic_fit_is_stable_under_large_x_scaling(self):
        x_values = [-2.0, -1.0, 1.0, 2.0]
        y_values = [0, 1, 0, 1]
        alpha, beta = fit_logistic(x_values, y_values)
        scaled_x = [value * 1e20 for value in x_values]
        scaled_alpha, scaled_beta = fit_logistic(scaled_x, y_values)

        probabilities = [sigmoid(alpha + beta * value) for value in x_values]
        scaled_probabilities = [
            sigmoid(scaled_alpha + scaled_beta * value) for value in scaled_x
        ]
        self.assertTrue(any(abs(value - 0.5) > 0.05 for value in probabilities))
        for expected, actual in zip(probabilities, scaled_probabilities):
            self.assertAlmostEqual(actual, expected, places=5)
        objective = _logistic_objective(x_values, y_values, alpha, beta, 1e-6)
        scaled_objective = _logistic_objective(
            scaled_x, y_values, scaled_alpha, scaled_beta, 1e-6
        )
        self.assertAlmostEqual(scaled_objective, objective, places=5)

    def test_logistic_fit_is_stable_under_large_x_translation(self):
        x_values = [-2.0, -1.0, 1.0, 2.0]
        y_values = [0, 1, 0, 1]
        alpha, beta = fit_logistic(x_values, y_values)
        translated_x = [value + 1e10 for value in x_values]
        translated_alpha, translated_beta = fit_logistic(translated_x, y_values)

        probabilities = [sigmoid(alpha + beta * value) for value in x_values]
        translated_probabilities = [
            sigmoid(translated_alpha + translated_beta * value)
            for value in translated_x
        ]
        self.assertTrue(any(abs(value - 0.5) > 0.05 for value in probabilities))
        for expected, actual in zip(probabilities, translated_probabilities):
            self.assertAlmostEqual(actual, expected, places=5)
        objective = _logistic_objective(x_values, y_values, alpha, beta, 1e-6)
        translated_objective = _logistic_objective(
            translated_x,
            y_values,
            translated_alpha,
            translated_beta,
            1e-6,
        )
        self.assertAlmostEqual(translated_objective, objective, places=5)

    def test_logistic_does_not_penalize_intercept(self):
        alpha, beta = fit_logistic([0.0, 0.0, 0.0, 0.0], [0, 1, 1, 1], l2=100.0)
        self.assertAlmostEqual(alpha, math.log(3.0), places=8)
        self.assertAlmostEqual(beta, 0.0, places=12)

    def test_logistic_objective_uses_l2_beta_squared(self):
        beta = 3.0
        l2 = 0.5
        epsilon = 1e-6
        objective = _logistic_objective([], [], 99.0, beta, l2)
        finite_difference = (
            _logistic_objective([], [], 99.0, beta + epsilon, l2)
            - _logistic_objective([], [], 99.0, beta - epsilon, l2)
        ) / (2.0 * epsilon)
        self.assertAlmostEqual(objective, l2 * beta * beta, places=12)
        self.assertAlmostEqual(finite_difference, 2.0 * l2 * beta, places=8)

    def test_logistic_rejects_invalid_inputs(self):
        invalid_calls = [
            ([0.0], [0]),
            ([0.0, 1.0], [0]),
            ([0.0, math.inf], [0, 1]),
            ([0.0, 1.0], [False, 1]),
            ([0.0, 1.0], [np.bool_(False), 1]),
            ([0.0, 1.0], [0.0, 1.0]),
            ([0.0, 1.0], [0, 2]),
            ([0.0, 1.0], [0, 0]),
        ]
        for x_values, y_values in invalid_calls:
            with self.subTest(x=x_values, y=y_values):
                with self.assertRaises(ValueError):
                    fit_logistic(x_values, y_values)

        for kwargs in (
            {"iterations": 0},
            {"iterations": True},
            {"l2": 0},
            {"l2": math.inf},
            {"tolerance": 0},
            {"tolerance": math.nan},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                fit_logistic([0.0, 1.0], [0, 1], **kwargs)

    def test_frozen_calibration_boundary_and_metadata(self):
        rows = [
            {"game_pk": 1, "official_date": "2024-12-30", "edge": -1, "home_win": 0},
            {"game_pk": 2, "official_date": "2024-12-31", "edge": 1, "home_win": 1},
        ]
        alpha, beta, metadata = fit_frozen_calibration(rows)
        self.assertTrue(math.isfinite(alpha))
        self.assertGreater(beta, 0.0)
        self.assertEqual(
            metadata,
            {
                "training_min_date": "2024-12-30",
                "training_max_date": "2024-12-31",
                "training_count": 2,
                "l2": 1e-6,
            },
        )

        with self.assertRaisesRegex(
            ValueError, "calibration data crosses frozen boundary"
        ):
            fit_frozen_calibration(
                [
                    {"official_date": "2024-12-31", "edge": -1, "home_win": 0},
                    {"official_date": "2025-01-01", "edge": 1, "home_win": 1},
                ]
            )

    def test_frozen_calibration_rejects_malformed_and_duplicate_game_pk(self):
        base = [
            {"game_pk": 1, "official_date": "2024-01-01", "edge": -1, "home_win": 0},
            {"game_pk": 2, "official_date": "2024-01-02", "edge": 1, "home_win": 1},
        ]
        for bad_game_pk in (True, 0, -1, "1"):
            rows = [dict(base[0], game_pk=bad_game_pk), base[1]]
            with self.subTest(game_pk=bad_game_pk), self.assertRaises(ValueError):
                fit_frozen_calibration(rows)
        with self.assertRaisesRegex(ValueError, "duplicate game_pk"):
            fit_frozen_calibration([base[0], dict(base[1], game_pk=1)])

    def test_frozen_calibration_rejects_malformed_rows(self):
        valid = {"official_date": "2024-01-01", "edge": -1, "home_win": 0}
        invalid_rows = [
            [{"official_date": "bad", "edge": -1, "home_win": 0}, valid],
            [{"official_date": "2024-01-01", "edge": math.nan, "home_win": 0}, valid],
            [{"official_date": "2024-01-01", "edge": -1, "home_win": False}, valid],
            [{"official_date": "2024-01-01", "edge": -1}, valid],
        ]
        for rows in invalid_rows:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                fit_frozen_calibration(rows)

    def test_probability_scaling_and_exact_clipping(self):
        self.assertAlmostEqual(probability(0.0, 0.0, 1.0), 0.5)
        self.assertAlmostEqual(probability(10.0, 0.0, 1.0), sigmoid(1.0))
        self.assertEqual(probability(1000.0, 0.0, 1.0), 0.90)
        self.assertEqual(probability(-1000.0, 0.0, 1.0), 0.10)

    def test_probability_rejects_invalid_values_and_bounds(self):
        invalid_calls = [
            (True, 0, 1, (0.1, 0.9)),
            (0, math.inf, 1, (0.1, 0.9)),
            (0, 0, False, (0.1, 0.9)),
            (0, 0, 1, (0.9, 0.1)),
            (0, 0, 1, (0.1, 1.1)),
            (0, 0, 1, (False, 0.9)),
            (0, 0, 1, (0.5,)),
        ]
        for args in invalid_calls:
            with self.subTest(args=args), self.assertRaises(ValueError):
                probability(*args)


if __name__ == "__main__":
    unittest.main()
