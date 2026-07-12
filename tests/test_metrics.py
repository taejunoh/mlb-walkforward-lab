import math
import inspect
import unittest
from datetime import date, timedelta

from mlb_backtest.metrics import (
    brier,
    completion_status,
    evaluate,
    extension_dates,
    log_loss,
    max_losing_streak,
    wilson,
)
from tests.test_locking import prediction


def evaluated(
    game_pk=1,
    rank=1,
    *,
    official_date="2025-05-01",
    winner="Home",
    status="Final",
    probability=0.61234567894,
    market=None,
):
    row = prediction(game_pk, rank, official_date=official_date)
    row["p_home"] = probability
    row["pick_probability"] = probability
    row["tier"] = "B" if probability >= 0.58 else "Lean" if probability >= 0.54 else "Toss-up"
    row["top1"] = "Yes" if rank == 1 else "No"
    row["top3"] = "Yes" if rank <= 3 else "No"
    row["daily_rank"] = rank
    outcome = {"game_pk": game_pk, "actual_winner": winner, "status": status}
    if status == "VOID":
        outcome["actual_winner"] = ""
    if market:
        outcome.update(market)
    row["outcome"] = outcome
    return row


class PrimitiveMetricsTest(unittest.TestCase):
    def test_metric_goldens(self):
        self.assertAlmostEqual(brier(0.60, False), 0.36)
        self.assertAlmostEqual(log_loss(0.70, True), -math.log(0.70))
        low, high = wilson(20, 30)
        self.assertAlmostEqual(low, 0.4877972581, places=9)
        self.assertGreater(high, low)
        self.assertEqual(
            max_losing_streak([True, False, False, True, False]), 2
        )

    def test_wilson_default_is_1_96_and_custom_z_is_used_exactly(self):
        self.assertEqual(inspect.signature(wilson).parameters["z"].default, 1.96)
        low, _ = wilson(20, 30, z=1.959963984540054)
        self.assertAlmostEqual(low, 0.4878005164454384, places=15)

    def test_log_loss_clips_and_all_primitives_validate_strictly(self):
        self.assertAlmostEqual(log_loss(0.0, True), -math.log(0.001))
        self.assertAlmostEqual(log_loss(1.0, False), -math.log(0.001))
        bad_calls = (
            lambda: brier(True, False),
            lambda: brier(math.nan, False),
            lambda: brier(1.1, False),
            lambda: brier(0.5, 1),
            lambda: log_loss(".5", True),
            lambda: wilson(True, 2),
            lambda: wilson(3, 2),
            lambda: wilson(0, 0),
            lambda: max_losing_streak([True, 0]),
        )
        for call in bad_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


class EvaluationTest(unittest.TestCase):
    def test_primary_void_denominators_ordering_and_streak(self):
        rows = [
            evaluated(3, official_date="2025-05-03", winner="Away"),
            evaluated(1, official_date="2025-05-01", winner="Home"),
            evaluated(4, official_date="2025-05-04", status="VOID"),
            evaluated(2, official_date="2025-05-02", winner="Away"),
        ]
        summary = evaluate(rows)
        primary = summary["primary"]
        self.assertEqual((primary["W"], primary["L"], primary["N"]), (1, 2, 3))
        self.assertAlmostEqual(primary["accuracy"], 1 / 3)
        self.assertEqual(primary["max_losing_streak"], 2)
        self.assertEqual(summary["void_top1"], 1)

    def test_subgroups_baselines_bins_and_empty_metrics(self):
        rows = [
            evaluated(1, 1, winner="Home", probability=0.60),
            evaluated(2, 2, winner="Away", probability=0.57),
            evaluated(3, 3, winner="Home", probability=0.53),
            evaluated(4, 4, winner="Away", probability=0.52),
        ]
        rows[1]["full_agreement"] = "No"
        rows[2]["full_agreement"] = "No"
        rows[3]["full_agreement"] = "No"
        summary = evaluate(rows, baseline_by_game={1: "Home"})
        self.assertEqual(summary["subgroups"]["Top3"]["N"], 3)
        self.assertEqual(summary["subgroups"]["58%+"]["N"], 1)
        self.assertEqual(summary["subgroups"]["Full Agreement"]["N"], 1)
        self.assertEqual(summary["subgroups"]["probability_bins"]["50-54"]["N"], 2)
        self.assertEqual(summary["subgroups"]["tiers"]["S"]["N"], 0)
        self.assertIsNone(summary["subgroups"]["tiers"]["S"]["accuracy"])
        self.assertEqual(summary["baselines"]["home"]["N"], 1)
        self.assertEqual(summary["baselines"]["higher_season_win_pct"]["N"], 1)

    def test_market_pairing_pickem_missing_and_brier(self):
        market = {
            "market_snapshot_at": "2025-05-01T22:15:00Z",
            "market_source": "Source",
            "market_favorite": "Away",
            "away_no_vig_probability": 0.55,
            "home_no_vig_probability": 0.45,
        }
        rows = [
            evaluated(1, winner="Home", market=market),
            evaluated(2, official_date="2025-05-02", winner="Away", market={
                "market_snapshot_at": "2025-05-02T22:15:00Z",
                "market_source": "Source",
                "market_favorite": "Away",
            }),
            evaluated(3, official_date="2025-05-03", winner="Home", market={
                "market_snapshot_at": "2025-05-03T22:15:00Z",
                "market_source": "Source",
                "market_favorite": "Pick'em",
            }),
            evaluated(4, official_date="2025-05-04", winner="Away"),
        ]
        paired = evaluate(rows)["paired_market"]
        self.assertEqual(paired["N"], 1)
        self.assertEqual(paired["model_only"], 1)
        self.assertEqual(paired["market_only"], 0)
        self.assertEqual(paired["model_accuracy"], 1.0)
        self.assertEqual(paired["market_accuracy"], 0.0)
        self.assertAlmostEqual(paired["market_brier"], 0.55**2)

    def test_month_and_predicted_team_summaries_use_graded_top1_only(self):
        rows = [
            evaluated(1, official_date="2025-05-01", winner="Home"),
            evaluated(2, official_date="2025-05-02", winner="Away"),
            evaluated(3, official_date="2025-06-01", winner="Home"),
            evaluated(4, official_date="2025-06-02", status="VOID"),
        ]
        rows[2]["predicted_side"] = "away"
        rows[2]["predicted_winner"] = "Away"
        rows[2]["p_home"] = 1.0 - rows[2]["pick_probability"]
        rows[3]["predicted_side"] = "away"
        rows[3]["predicted_winner"] = "Away"
        rows[3]["p_home"] = 1.0 - rows[3]["pick_probability"]
        summary = evaluate(rows)
        self.assertEqual(list(summary["by_month"]), ["2025-05", "2025-06"])
        self.assertEqual(
            summary["by_month"],
            {
                "2025-05": {"W": 1, "L": 1, "N": 2, "accuracy": 0.5},
                "2025-06": {"W": 0, "L": 1, "N": 1, "accuracy": 0.0},
            },
        )
        self.assertEqual(list(summary["by_predicted_team"]), ["Away", "Home"])
        self.assertEqual(
            summary["by_predicted_team"],
            {
                "Away": {"W": 0, "L": 1, "N": 1, "accuracy": 0.0},
                "Home": {"W": 1, "L": 1, "N": 2, "accuracy": 0.5},
            },
        )

    def test_calibration_includes_empty_bins_and_weighted_ece(self):
        rows = [
            evaluated(1, winner="Home", probability=0.60),
            evaluated(2, official_date="2025-05-02", winner="Away", probability=0.60),
        ]
        calibration = evaluate(rows)["calibration"]
        bin_58 = next(item for item in calibration["bins"] if item["bin"] == "58-63")
        bin_68 = next(item for item in calibration["bins"] if item["bin"] == "68-100")
        self.assertEqual(bin_58["N"], 2)
        self.assertAlmostEqual(bin_58["mean_probability"], 0.60)
        self.assertAlmostEqual(bin_58["actual_accuracy"], 0.5)
        self.assertAlmostEqual(bin_58["gap"], 0.1)
        self.assertAlmostEqual(bin_58["brier_contribution"], 0.26)
        self.assertAlmostEqual(calibration["ece"], 0.1)
        self.assertIsNone(bin_68["mean_probability"])
        self.assertIsNone(bin_68["actual_accuracy"])

    def test_rejects_malformed_rows_versions_duplicates_and_top1_dates(self):
        malformed = evaluated()
        malformed["outcome"] = {"game_pk": 1, "actual_winner": "Other", "status": "Final"}
        mixed = [evaluated(), evaluated(2, official_date="2025-05-02")]
        mixed[1]["model_version"] = "v5.2"
        duplicate = [evaluated(), evaluated()]
        no_top1 = [evaluated(2, 2)]
        for rows in ([malformed], mixed, duplicate, no_top1):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                evaluate(rows)

    def test_market_metadata_and_probabilities_are_validated(self):
        invalid_markets = (
            {"market_favorite": "Home"},
            {
                "market_snapshot_at": "x",
                "market_source": "S",
                "market_favorite": "Other",
            },
            {
                "market_snapshot_at": "x",
                "market_source": "S",
                "market_favorite": "Home",
                "home_no_vig_probability": 0.6,
            },
            {
                "market_snapshot_at": "x",
                "market_source": "S",
                "market_favorite": "Home",
                "home_no_vig_probability": 0.6,
                "away_no_vig_probability": 0.5,
            },
            {
                "home_no_vig_probability": 0.6,
                "away_no_vig_probability": 0.4,
            },
            {
                "market_snapshot_at": "x",
                "market_source": "S",
                "market_favorite": "Home",
                "home_no_vig_probability": 1.0,
                "away_no_vig_probability": 0.0,
            },
        )
        for market in invalid_markets:
            with self.subTest(market=market), self.assertRaises(ValueError):
                evaluate([evaluated(market=market)])


class CompletionTest(unittest.TestCase):
    def make_rows(self, final_count, void_count=0):
        rows = []
        start = date(2025, 1, 1)
        for index in range(final_count + void_count):
            day = (start + timedelta(days=index)).isoformat()
            rows.append(
                evaluated(
                    index + 1,
                    official_date=day,
                    status="Final" if index < final_count else "VOID",
                )
            )
        return rows

    def test_completion_requires_100_graded_not_100_selections(self):
        incomplete = completion_status(self.make_rows(99, 1))
        self.assertEqual(
            incomplete,
            {
                "graded_top1": 99,
                "void_top1": 1,
                "selection_dates": 100,
                "target": 100,
                "complete": False,
                "dates_needed": 1,
            },
        )
        self.assertTrue(completion_status(self.make_rows(100))["complete"])

    def test_extension_skips_existing_dates_and_returns_exact_need(self):
        rows = self.make_rows(2, 1)
        schedule = ["2025-01-02", "2025-01-04", "2025-01-05"]
        self.assertEqual(
            extension_dates(rows, schedule, target=4),
            ["2025-01-04", "2025-01-05"],
        )
        with self.assertRaises(ValueError):
            extension_dates(rows, ["2025-01-04"], target=4)
        for target in (True, 0, -1, 1.5):
            with self.subTest(target=target), self.assertRaises(ValueError):
                completion_status([], target=target)


if __name__ == "__main__":
    unittest.main()
