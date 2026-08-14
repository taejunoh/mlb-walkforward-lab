import math
import unittest

from mlb_backtest.scoring_prep import (
    build_candidate_components,
    build_raw_features_by_date,
    build_training_edges,
    compute_normalization,
    prepare_locked_outcomes,
    score_raw_game,
    validate_normalization,
)


def _metric(value, quality=1.0, sample_size=200):
    return {"value": value, "quality": quality, "sample_size": sample_size}


def _raw_game(game_pk, day, shift=0.0):
    starter = {
        "pitcher_id": 10,
        "career_bf": 220,
        "season_xwoba": _metric(0.30 + shift),
        "k_minus_bb": _metric(0.15 - shift),
        "fip": _metric(3.5 + shift),
        "last5_xwoba": _metric(0.31 + shift),
        "expected_ip": _metric(5.5 - shift),
        "velocity_change": _metric(0.5 - shift),
    }
    offense = {
        "players": [{"player_id": i, "batting_slot": i} for i in range(1, 10)],
        "platoon_xwoba": _metric(0.32 - shift),
        "last14_xwoba": _metric(0.33 - shift),
        "bb_minus_k": _metric(-0.10 + shift),
        "barrel_rate": _metric(0.08 - shift),
        "iso": _metric(0.16 - shift),
    }
    bullpen = {
        "season_xwoba": _metric(0.31 + shift),
        "k_minus_bb": _metric(0.12 - shift),
        "last14_xwoba": _metric(0.32 + shift),
        "availability": _metric(0.8 - shift),
    }
    side = {
        "starter": starter,
        "offense": offense,
        "bullpen": bullpen,
        "environment": _metric(50.0, quality=0.0, sample_size=0),
        "defense": _metric(50.0, quality=0.0, sample_size=0),
        "schedule": _metric(55.0, quality=1.0),
    }
    away = {**side, "team_code": "AWY"}
    home = {
        **side,
        "team_code": "HOM",
        "starter": {**starter, "pitcher_id": 20, "career_bf": 180},
    }
    return {
        "game_pk": game_pk,
        "official_date": day,
        "eligible": True,
        "reasons": [],
        "lineup_provenance": "RECONSTRUCTED_FINAL_LINEUP",
        "away": away,
        "home": home,
    }


class ScoringPreparationTest(unittest.TestCase):
    def setUp(self):
        self.training = [
            _raw_game(1, "2022-04-10", -0.02),
            _raw_game(2, "2023-04-10", 0.00),
            _raw_game(3, "2024-04-10", 0.02),
        ]

    def test_normalization_uses_training_values_and_sample_sd(self):
        stats = compute_normalization(self.training)
        values = [-0.02, -0.02, 0.0, 0.0, 0.02, 0.02]
        expected_sd = math.sqrt(sum(value * value for value in values) / 5)
        self.assertAlmostEqual(stats["starter"]["season_xwoba"]["mean"], 0.30)
        self.assertAlmostEqual(
            stats["starter"]["season_xwoba"]["sd"], expected_sd
        )
        self.assertEqual(stats["starter"]["season_xwoba"]["direction"], -1)
        self.assertEqual(stats["offense"]["iso"]["direction"], 1)
        validate_normalization(stats)
        bad = {**stats, "starter": {**stats["starter"]}}
        bad["starter"].pop("fip")
        with self.assertRaisesRegex(ValueError, "normalization"):
            validate_normalization(bad)

    def test_metric_quality_neutralizes_after_directional_scoring(self):
        stats = compute_normalization(self.training)
        target = _raw_game(4, "2025-05-01", -0.02)
        target["home"]["starter"]["season_xwoba"] = _metric(0.28, quality=0.25)
        scored = score_raw_game(target, stats)
        full = score_raw_game(_raw_game(5, "2025-05-02", -0.02), stats)
        self.assertGreater(full["home_scores"]["starter"], 50.0)
        self.assertGreater(scored["home_scores"]["starter"], 50.0)
        self.assertLess(
            scored["home_scores"]["starter"],
            full["home_scores"]["starter"],
        )
        self.assertAlmostEqual(scored["qualities"]["environment"], 0.0)
        self.assertAlmostEqual(scored["home_scores"]["environment"], 50.0)

    def test_environment_and_defense_are_forced_to_frozen_neutral_fallbacks(self):
        stats = compute_normalization(self.training)
        target = _raw_game(4, "2025-05-01")
        for side in ("away", "home"):
            target[side]["environment"] = _metric(99.0, quality=1.0)
            target[side]["defense"] = _metric(1.0, quality=1.0)
        scored = score_raw_game(target, stats)
        for component in ("environment", "defense"):
            self.assertEqual(scored["away_scores"][component], 50.0)
            self.assertEqual(scored["home_scores"][component], 50.0)
            self.assertEqual(scored["qualities"][component], 0.0)

    def test_training_edges_keep_outcomes_separate_and_frozen(self):
        stats = compute_normalization(self.training)
        rows = build_training_edges(
            self.training,
            stats,
            {1: 0, 2: 1, 3: 1},
        )
        self.assertEqual([row["game_pk"] for row in rows], [1, 2, 3])
        self.assertEqual([row["home_win"] for row in rows], [0, 1, 1])
        self.assertTrue(all(set(row) == {"game_pk", "official_date", "edge", "home_win"} for row in rows))

    def test_candidate_contract_contains_only_pipeline_safe_fields(self):
        stats = compute_normalization(self.training)
        game = {
            "game_pk": 4,
            "official_date": "2025-05-01",
            "scheduled_start_utc": "2025-05-01T23:10:00Z",
            "away_team": "Away",
            "home_team": "Home",
            "doubleheader_marker": "GAME1",
        }
        rows = build_candidate_components([game], [_raw_game(4, "2025-05-01")], stats)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["starter_career_bf"], 180)
        self.assertEqual(row["doubleheader_marker"], "GAME1")
        serialized_keys = " ".join(row).lower()
        for forbidden in ("outcome", "result", "market", "winner", "score"):
            if forbidden == "score":
                continue
            self.assertNotIn(forbidden, serialized_keys)
        self.assertEqual(
            set(row),
            {
                "game_pk", "official_date", "scheduled_start_utc", "away_team",
                "home_team", "away_scores", "home_scores", "qualities",
                "starter_career_bf", "critical_failure", "unresolved_context",
                "lineup_provenance", "data_status", "doubleheader_marker",
            },
        )

    def test_history_factory_is_reused_once_for_an_entire_season(self):
        games = [
            {"game_pk": 1, "official_date": "2025-05-01"},
            {"game_pk": 2, "official_date": "2025-05-01"},
            {"game_pk": 3, "official_date": "2025-05-02"},
        ]
        calls = []

        def history_factory(game):
            calls.append(game["official_date"])
            return {"date": game["official_date"]}

        rows = build_raw_features_by_date(
            games,
            history_factory,
            lambda game, history: (game["game_pk"], history["date"]),
        )
        self.assertEqual(calls, ["2025-05-01"])
        self.assertEqual(rows, [(1, "2025-05-01"), (2, "2025-05-01"), (3, "2025-05-01")])

    def test_outcomes_are_prepared_only_from_locked_games_with_d1_baseline(self):
        schedule = [
            {"game_pk": 1, "official_date": "2025-04-30", "away_team": "A", "home_team": "B", "status": "Final", "away_is_winner": True, "home_is_winner": False},
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B", "status": "Final", "away_is_winner": False, "home_is_winner": True},
            {"game_pk": 3, "official_date": "2025-05-01", "away_team": "A", "home_team": "B", "status": "Final", "away_is_winner": True, "home_is_winner": False},
            {"game_pk": 4, "official_date": "2025-05-02", "away_team": "A", "home_team": "B", "status": "Postponed", "away_is_winner": None, "home_is_winner": None},
        ]
        locked = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B"},
            {"game_pk": 3, "official_date": "2025-05-01", "away_team": "A", "home_team": "B"},
            {"game_pk": 4, "official_date": "2025-05-02", "away_team": "A", "home_team": "B"},
        ]
        outcomes, baseline = prepare_locked_outcomes(locked, schedule)
        self.assertEqual([row["status"] for row in outcomes], ["Final", "Final", "VOID"])
        self.assertEqual(outcomes[0]["actual_winner"], "B")
        self.assertEqual(outcomes[2]["actual_winner"], "")
        self.assertEqual(baseline, {2: "A", 3: "A"})

    def test_outcomes_reconcile_stale_postponed_snapshot_to_one_official_final(self):
        schedule = [
            {"game_pk": 1, "official_date": "2025-04-30", "away_team": "A", "home_team": "B", "status": "Final", "away_is_winner": True, "home_is_winner": False},
            {"game_pk": 10, "official_date": "2025-04-30", "away_team": "C", "home_team": "D", "status": "Final", "away_is_winner": True, "home_is_winner": False},
            {"game_pk": 2, "official_date": "2025-05-01", "scheduled_start_utc": "2025-04-11T23:05:00Z", "away_team": "A", "home_team": "B", "status": "Postponed", "away_score": None, "home_score": None, "away_is_winner": None, "home_is_winner": None},
            {"game_pk": 2, "official_date": "2025-05-01", "scheduled_start_utc": "2025-05-01T17:05:00Z", "away_team": "A", "home_team": "B", "status": "Final", "away_score": 2, "home_score": 3, "away_is_winner": False, "home_is_winner": True},
            {"game_pk": 3, "official_date": "2025-05-02", "away_team": "A", "home_team": "C", "status": "Final", "away_is_winner": True, "home_is_winner": False},
        ]
        locked = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B"},
            {"game_pk": 3, "official_date": "2025-05-02", "away_team": "A", "home_team": "C"},
        ]

        outcomes, baseline = prepare_locked_outcomes(locked, schedule)

        self.assertEqual(
            outcomes,
            [
                {"game_pk": 2, "actual_winner": "B", "status": "Final"},
                {"game_pk": 3, "actual_winner": "A", "status": "Final"},
            ],
        )
        self.assertEqual(baseline, {2: "A", 3: "C"})

    def test_outcomes_reject_conflicting_multiple_official_finals(self):
        schedule = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B", "status": "Final", "away_is_winner": True, "home_is_winner": False},
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B", "status": "Completed Early", "away_is_winner": False, "home_is_winner": True},
        ]

        with self.assertRaisesRegex(ValueError, "multiple official Final records"):
            prepare_locked_outcomes([{"game_pk": 2}], schedule)

    def test_outcomes_reject_ambiguous_duplicate_nonfinal_records(self):
        schedule = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B", "status": "Postponed", "away_score": None, "home_score": None, "away_is_winner": None, "home_is_winner": None},
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B", "status": "Cancelled", "away_score": None, "home_score": None, "away_is_winner": None, "home_is_winner": None},
        ]

        with self.assertRaisesRegex(ValueError, "ambiguous duplicate schedule records"):
            prepare_locked_outcomes([{"game_pk": 2}], schedule)

    def test_outcomes_reject_schedule_date_that_differs_from_locked_identity(self):
        schedule = [
            {"game_pk": 2, "official_date": "2025-05-02", "away_team": "A", "home_team": "B", "status": "Final", "away_is_winner": True, "home_is_winner": False},
        ]
        locked = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B"},
        ]

        with self.assertRaisesRegex(ValueError, "schedule identity differs from lock"):
            prepare_locked_outcomes(locked, schedule)

    def test_outcomes_reject_opponent_mismatch_even_when_winner_is_a_locked_team(self):
        schedule = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "C", "status": "Final", "away_is_winner": True, "home_is_winner": False},
        ]
        locked = [
            {"game_pk": 2, "official_date": "2025-05-01", "away_team": "A", "home_team": "B"},
        ]

        with self.assertRaisesRegex(ValueError, "schedule identity differs from lock"):
            prepare_locked_outcomes(locked, schedule)


if __name__ == "__main__":
    unittest.main()
