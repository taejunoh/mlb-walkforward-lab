from __future__ import annotations

import math
import unittest
from dataclasses import replace
from datetime import date, timedelta

from mlb_backtest.experimental_totals import (
    CANDIDATE_IDS,
    EvaluationMetrics,
    FinalTotalGame,
    LineProbabilities,
    TotalProjection,
    UnprojectedTotal,
    compare_total_line,
    evaluate_predictions,
    interval_80,
    interval_coverage,
    project_game,
    residual_samples,
    select_candidate,
    validate_final_games,
    validate_target_games,
    walk_forward_predictions,
)


def make_game(
    official_date: str,
    *,
    game_pk: int = 1,
    away_team_id: int = 10,
    home_team_id: int = 20,
    venue_id: int = 30,
    away_runs: int = 4,
    home_runs: int = 5,
    status: str = "Final",
) -> dict[str, object]:
    return {
        "game_pk": game_pk,
        "official_date": official_date,
        "scheduled_start_utc": f"{official_date}T18:00:00Z",
        "away_team_id": away_team_id,
        "home_team_id": home_team_id,
        "venue_id": venue_id,
        "away_runs": away_runs,
        "home_runs": home_runs,
        "status": status,
    }


def make_target(
    target_date: str,
    *,
    game_pk: int = 100,
    away_team_id: int = 10,
    home_team_id: int = 20,
    venue_id: int = 30,
    status: str = "Pregame",
) -> dict[str, object]:
    return {
        "game_pk": game_pk,
        "official_date": target_date,
        "scheduled_start_utc": f"{target_date}T18:00:00Z",
        "away_team_id": away_team_id,
        "home_team_id": home_team_id,
        "venue_id": venue_id,
        "status": status,
    }


def make_history_through(end_date: str) -> tuple[dict[str, object], ...]:
    end = date.fromisoformat(end_date)
    return tuple(
        make_game(
            (end - timedelta(days=offset)).isoformat(),
            game_pk=1000 + offset,
            away_runs=3 + offset % 3,
            home_runs=4 + offset % 4,
        )
        for offset in range(45)
    )


class ExperimentalTotalsTests(unittest.TestCase):
    def test_final_rows_are_closed_immutable_and_validate_d1_history(self) -> None:
        valid = make_game("2026-05-31", game_pk=4)
        rows = validate_final_games((valid,), cutoff_date="2026-05-31")
        self.assertEqual(rows[0].game_pk, 4)
        with self.assertRaisesRegex((AttributeError, TypeError), ""):
            rows[0].away_runs = 9  # type: ignore[misc]

        invalid_rows = (
            (dict(valid, game_pk=4), "duplicate game_pk"),
            (dict(valid, away_runs=True), "away_runs"),
            (dict(valid, home_team_id=True), "home_team_id"),
            (dict(valid, venue_id=True), "venue_id"),
            (dict(valid, away_runs=-1), "away_runs"),
            (dict(valid, status="Scheduled"), "status"),
            (dict(valid, official_date="2026-06-01"), "cutoff"),
        )
        for invalid, message in invalid_rows:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_final_games((valid, invalid), cutoff_date="2026-05-31")

    def test_direct_final_game_construction_enforces_closed_score_invariants(self) -> None:
        arguments = (1, "2026-05-31", "2026-05-31T18:00:00Z", 10, 20, 30)
        for away_runs, home_runs in ((True, 4), (-1, 4), (4.0, 4), (4, True)):
            with self.subTest(away_runs=away_runs, home_runs=home_runs):
                with self.assertRaisesRegex(ValueError, "runs"):
                    FinalTotalGame(*arguments, away_runs, home_runs)

    def test_target_validation_is_closed_and_requires_target_date_and_pregame(self) -> None:
        target = make_target("2026-09-01")
        self.assertEqual(
            validate_target_games((target,), target_date="2026-09-01")[0].game_pk,
            100,
        )
        for invalid, message in (
            (dict(target, official_date="2026-09-02"), "target_date"),
            (dict(target, status="Final"), "status"),
            (dict(target, away_team_id=True), "away_team_id"),
            (dict(target, extra="not allowed"), "fields"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    validate_target_games((invalid,), target_date="2026-09-01")

    def test_target_or_future_mutation_cannot_change_same_day_prediction(self) -> None:
        history = validate_final_games(
            make_history_through("2026-06-15"), cutoff_date="2026-06-15"
        )
        original = walk_forward_predictions(
            history, CANDIDATE_IDS, "2026-06-01", "2026-06-15"
        )
        mutated = history + validate_final_games(
            (make_game("2026-06-16", game_pk=9999, away_runs=99, home_runs=99),),
            cutoff_date="2026-06-16",
        )
        self.assertEqual(
            original,
            walk_forward_predictions(
                mutated, CANDIDATE_IDS, "2026-06-01", "2026-06-15"
            ),
        )

    def test_target_date_score_mutation_cannot_change_that_date_prediction(self) -> None:
        history = validate_final_games(
            make_history_through("2026-06-15"), cutoff_date="2026-06-15"
        )
        original = walk_forward_predictions(
            history, ("TEAM_30",), "2026-06-10", "2026-06-10"
        )
        target_actual, target_projection = original["TEAM_30"][0]
        mutated = tuple(
            replace(game, away_runs=99, home_runs=99)
            if game.game_pk == target_actual.game_pk
            else game
            for game in history
        )
        mutated_prediction = walk_forward_predictions(
            mutated, ("TEAM_30",), "2026-06-10", "2026-06-10"
        )["TEAM_30"][0][1]
        self.assertEqual(target_projection, mutated_prediction)

    def test_may_to_august_history_projects_a_september_first_target(self) -> None:
        start = date(2026, 5, 1)
        history = validate_final_games(
            tuple(
                make_game(
                    (start + timedelta(days=offset)).isoformat(),
                    game_pk=5000 + offset,
                    away_runs=4,
                    home_runs=5,
                )
                for offset in range((date(2026, 8, 31) - start).days + 1)
            ),
            cutoff_date="2026-08-31",
        )
        target = validate_target_games(
            (make_target("2026-09-01", game_pk=9999),), target_date="2026-09-01"
        )[0]
        self.assertEqual(
            project_game(history, target, "TEAM_VENUE_30"),
            TotalProjection(
                "PROJECTED",
                "TEAM_VENUE_30",
                4.0 * ((30 * 4 + 10 * 4.5) / 40 / 4.5),
                5.0 * ((30 * 5 + 10 * 4.5) / 40 / 4.5),
                9.083333333333332,
            ),
        )

    def test_candidates_use_independent_shrunk_rate_oracle(self) -> None:
        # The 30-day league mean is away=8, home=3 and per-team=5.5. Team 10
        # scores 8 and allows 2; team 20 scores 3 and allows 7 in each game.
        history = validate_final_games(
            tuple(
                make_game(
                    f"2026-05-{day:02d}",
                    game_pk=day,
                    away_runs=8,
                    home_runs=3,
                )
                for day in range(2, 32)
            ),
            cutoff_date="2026-05-31",
        )
        game = validate_target_games((make_target("2026-06-01"),), target_date="2026-06-01")[0]
        league = project_game(history, game, "LEAGUE_30")
        team = project_game(history, game, "TEAM_30")
        venue = project_game(history, game, "TEAM_VENUE_30")
        self.assertEqual(league, TotalProjection("PROJECTED", "LEAGUE_30", 8.0, 3.0, 11.0))
        scored_10 = (30 * 8 + 10 * 5.5) / 40
        allowed_20 = (30 * 8 + 10 * 5.5) / 40
        scored_20 = (30 * 3 + 10 * 5.5) / 40
        allowed_10 = (30 * 3 + 10 * 5.5) / 40
        expected_away = 8 * math.sqrt((scored_10 / 5.5) * (allowed_20 / 5.5))
        expected_home = 3 * math.sqrt((scored_20 / 5.5) * (allowed_10 / 5.5))
        self.assertAlmostEqual(team.away_expected_runs, expected_away)
        self.assertAlmostEqual(team.home_expected_runs, expected_home)
        # The 60-day venue mean equals the 60-day league mean, so the venue
        # factor is exactly one even after its twenty-game shrinkage.
        self.assertAlmostEqual(venue.away_expected_runs, expected_away)
        self.assertAlmostEqual(venue.home_expected_runs, expected_home)

    def test_unknown_target_is_unprojected_without_blocking_valid_peer(self) -> None:
        history = validate_final_games(
            (make_game("2026-05-31", game_pk=1),), cutoff_date="2026-05-31"
        )
        unknown_team = validate_target_games(
            (make_target("2026-06-01", game_pk=2, away_team_id=999, venue_id=999),),
            target_date="2026-06-01",
        )[0]
        unknown_venue = validate_target_games(
            (make_target("2026-06-01", game_pk=3, venue_id=999),),
            target_date="2026-06-01",
        )[0]
        valid = validate_target_games(
            (make_target("2026-06-01", game_pk=4),), target_date="2026-06-01"
        )[0]
        projections = tuple(
            project_game(history, target, "TEAM_VENUE_30")
            for target in (unknown_team, valid, unknown_venue)
        )
        self.assertEqual(
            projections[0],
            UnprojectedTotal("UNPROJECTED", "UNKNOWN_TEAM"),
        )
        self.assertEqual(
            projections[2],
            UnprojectedTotal("UNPROJECTED", "UNKNOWN_VENUE"),
        )
        self.assertIsInstance(projections[1], TotalProjection)

    def test_metrics_selection_and_residual_distribution_match_oracles(self) -> None:
        history = validate_final_games(
            (
                make_game("2026-06-01", game_pk=1, away_runs=3, home_runs=4),
                make_game("2026-06-02", game_pk=2, away_runs=0, home_runs=6),
            ),
            cutoff_date="2026-06-02",
        )
        projections = (
            (history[0], TotalProjection("PROJECTED", "TEAM_30", 4.0, 4.0, 8.0)),
            (history[1], TotalProjection("PROJECTED", "TEAM_30", 2.0, 3.0, 5.0)),
        )
        metrics = evaluate_predictions(projections)
        expected_deviance = (2 * (7 * math.log(7 / 8) - (7 - 8)) + 2 * (6 * math.log(6 / 5) - (6 - 5))) / 2
        self.assertEqual(metrics.games, 2)
        self.assertAlmostEqual(metrics.mae, 1.0)
        self.assertAlmostEqual(metrics.rmse, 1.0)
        self.assertAlmostEqual(metrics.poisson_deviance, expected_deviance)
        zero_actual = validate_final_games(
            (make_game("2026-06-03", game_pk=3, away_runs=0, home_runs=0),),
            cutoff_date="2026-06-03",
        )[0]
        zero_metrics = evaluate_predictions(
            ((zero_actual, TotalProjection("PROJECTED", "TEAM_30", 2.0, 2.0, 4.0)),)
        )
        self.assertEqual(zero_metrics.poisson_deviance, 8.0)
        self.assertEqual(residual_samples(projections, 8.4), (7, 9))
        clamped_rows = (
            (zero_actual, TotalProjection("PROJECTED", "TEAM_30", 2.5, 2.5, 5.0)),
            (history[0], TotalProjection("PROJECTED", "TEAM_30", 3.25, 3.25, 6.5)),
        )
        self.assertEqual(residual_samples((clamped_rows[0],), 0.1), (0,))
        self.assertEqual(residual_samples((clamped_rows[1],), 7.0), (8,))
        self.assertEqual(interval_80((3, 4, 5, 6, 7, 8, 9, 10, 11, 12)), (3, 11))
        self.assertEqual(interval_coverage((7, 8, 9, 10), (8, 9)), 0.5)
        self.assertEqual(
            compare_total_line((7, 8, 8, 9), 8),
            LineProbabilities(over=0.25, under=0.25, push=0.50),
        )
        self.assertEqual(
            compare_total_line((7, 8, 8, 9), 8.5),
            LineProbabilities(over=0.25, under=0.75, push=0.0),
        )

    def test_selection_requires_100_rows_and_uses_strict_tie_rules(self) -> None:
        game = validate_final_games((make_game("2026-06-01"),), cutoff_date="2026-06-01")[0]
        games = tuple(replace(game, game_pk=index + 1) for index in range(100))
        baseline = tuple(
            (actual, TotalProjection("PROJECTED", "LEAGUE_30", 4.0, 4.0, 8.0))
            for actual in games
        )
        improved = tuple(
            (actual, TotalProjection("PROJECTED", "TEAM_30", 4.0, 5.0, 9.0))
            for actual in games
        )
        equally_improved = tuple(
            (actual, TotalProjection("PROJECTED", "TEAM_VENUE_30", 4.0, 5.0, 9.0))
            for actual in games
        )
        with self.assertRaisesRegex(ValueError, "100"):
            select_candidate({"LEAGUE_30": baseline[:-1], "TEAM_30": improved[:-1], "TEAM_VENUE_30": equally_improved[:-1]})
        self.assertEqual(
            select_candidate(
                {
                    "LEAGUE_30": baseline,
                    "TEAM_30": improved,
                    "TEAM_VENUE_30": equally_improved,
                }
            ),
            "TEAM_30",
        )
        with self.assertRaisesRegex(ValueError, "homogeneous"):
            evaluate_predictions((baseline[0], improved[1]))
        with self.assertRaisesRegex(ValueError, "duplicate game_pk"):
            evaluate_predictions((baseline[0], baseline[0]))
        mismatched = {
            "LEAGUE_30": baseline,
            "TEAM_30": equally_improved,
            "TEAM_VENUE_30": equally_improved,
        }
        with self.assertRaisesRegex(ValueError, "candidate_id"):
            select_candidate(mismatched)
        different_population = tuple(
            (
                replace(actual, game_pk=1000 + index),
                projection,
            )
            for index, (actual, projection) in enumerate(equally_improved)
        )
        with self.assertRaisesRegex(ValueError, "game_pk populations"):
            select_candidate(
                {
                    "LEAGUE_30": baseline,
                    "TEAM_30": improved,
                    "TEAM_VENUE_30": different_population,
                }
            )

    def test_metric_dataclass_is_value_object(self) -> None:
        self.assertEqual(
            EvaluationMetrics(games=1, mae=1.0, rmse=1.0, poisson_deviance=2.0),
            EvaluationMetrics(games=1, mae=1.0, rmse=1.0, poisson_deviance=2.0),
        )


if __name__ == "__main__":
    unittest.main()
