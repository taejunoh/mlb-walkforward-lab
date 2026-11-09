"""Network-free, source-authenticated postseason daily research scenarios."""
from __future__ import annotations

import csv
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from mlb_backtest import daily_top3, live_history, live_sources
from mlb_backtest.live_top3_contracts import LiveHttpObservation
from mlb_backtest.scoring import home_probability
from mlb_backtest.scoring_prep import score_raw_game
from mlb_backtest.live_model_card import load_frozen_live_card

TARGET = "2026-07-21"
FIXTURES = Path(__file__).parent / "fixtures"
CARD = Path(__file__).parents[1] / "audit/v5_1_bt/model_card.json"


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 21, 10, 0, 2, tzinfo=timezone.utc)
        return value if tz is not None else value.replace(tzinfo=None)


class ResearchFetcher:
    def __init__(self, count=4, variant=None):
        self.count, self.variant = count, variant
        self.current_schedule_calls = 0
        self.calls = []

    def __call__(self, source, url, *, max_bytes):
        self.calls.append((source, url))
        query, path = parse_qs(urlsplit(url).query), urlsplit(url).path
        completed = "2026-07-21T10:00:01Z"
        if source == "savant":
            with (FIXTURES / "statcast_mini.csv").open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                next(reader)
                template = next(reader)
                fields = reader.fieldnames
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for index in range(15):
                row = dict(template)
                row.update(game_date="2026-07-20", game_pk=str(910000 + index),
                           home_team=f"H{index:02}", away_team=f"A{index:02}",
                           pitcher=str(1000 + index), batter=str(2000 + index))
                if self.variant == "mixed_history" and index == 1:
                    row["game_type"] = "F"
                if index == 1:
                    row.update(events="home_run", description="hit_into_play", woba_value="2", woba_denom="1")
                writer.writerow(row)
            body = output.getvalue().encode()
        elif "/schedule" in path and query.get("startDate") != [TARGET]:
            self.assert_population(query, "R,F,D,L,W")
            games = []
            for index in range(15):
                games.append({"gamePk": 910000 + index, "gameType": "F" if self.variant == "mixed_history" and index == 1 else "R", "officialDate": "2026-07-20",
                              "gameDate": "2026-07-20T19:00:00Z", "status": {"detailedState": "Final"},
                              "teams": {"away": {"team": {"id": 100 + index, "name": f"Away {index}"}, "score": 1, "isWinner": False},
                                        "home": {"team": {"id": 200 + index, "name": f"Home {index}"}, "score": 2, "isWinner": True}},
                              "linescore": {"currentInning": 1, "inningHalf": "Top", "outs": 0},
                              "venue": {"id": 1, "name": "Fixture Park"}, "doubleHeader": "N", "gameNumber": 1})
            body = json.dumps({"dates": [{"date": "2026-07-20", "games": games}]}).encode()
        elif "/schedule" in path:
            self.assert_population(query, "F,D,L,W")
            self.current_schedule_calls += 1
            payload = json.loads((FIXTURES / "live_top3/sealed_happy_slate.json").read_bytes())
            games = payload["dates"][0]["games"][:self.count]
            for index, game in enumerate(games):
                game["gameType"] = ("F", "D", "L", "W")[index % 4]
            if self.variant == "unsupported" and games:
                games[-1]["gameType"] = "P"
            if self.variant == "missing_starter" and games:
                games[0]["teams"]["away"].pop("probablePitcher", None)
            if self.variant == "alternate_starter" and games:
                games[0]["teams"]["away"]["probablePitcher"] = {"id": 1001, "fullName": "Alternate Away Starter"}
            if self.variant == "final_type" and self.current_schedule_calls > 1:
                games[0]["gameType"] = "D"
            if self.variant == "final_team" and self.current_schedule_calls > 1:
                games[0]["teams"]["away"]["team"]["id"] = 999
            body = json.dumps({"dates": [{"date": TARGET, "games": games}]}).encode()
            completed = "2026-07-21T16:00:01Z"
        elif "/boxscore" in path:
            index = int(path.split("/")[4]) - 880001
            def players(base, count=9):
                return {f"ID{base + slot}": {"person": {"id": base + slot}, "battingOrder": f"{slot}00"}
                        for slot in range(1, count + 1)}
            body = json.dumps({"teams": {
                "away": {"team": {"id": 100 + index}, "pitchers": [1001 if self.variant == "alternate_starter" and index == 0 else 1000 + index], "players": players(11000 + index * 20)},
                "home": {"team": {"id": 200 + index}, "pitchers": [3000 + index],
                         "players": players(12000 + index * 20, 2 if self.variant == "partial" and index == 0 else 9)},
            }}).encode()
            completed = "2026-07-21T16:00:01Z"
        elif "/venues/" in path:
            body = json.dumps({"venues": [{"id": 1, "name": "Fixture Park", "location": {
                "city": "Fixture City", "defaultCoordinates": {"latitude": 40.0, "longitude": -74.0}},
                "timeZone": {"id": "America/New_York"}}]}).encode()
        elif "/people/" in path:
            if query.get("gameType") != ["R"]:
                raise AssertionError("career BF remains R-only")
            pitcher = int(path.split("/")[4])
            body = json.dumps({"stats": [{"type": {"displayName": "byDateRange"}, "group": {"displayName": "pitching"},
                "splits": [{"stat": {"battersFaced": 4000 + pitcher}, "team": {"id": 1}, "sport": {"id": 1}},
                           {"stat": {"battersFaced": 4000 + pitcher}, "sport": {"id": 0}}]}]}).encode()
            completed = "2026-07-21T16:00:01Z"
        elif path == "/api/v1/stats":
            body = (FIXTURES / "mlb_pitching_stats.json").read_bytes()
        else:
            raise AssertionError(f"unexpected sealed request: {url}")
        return LiveHttpObservation(url, url, completed, completed, 200, "application/json", '"research"', None, body)

    @staticmethod
    def assert_population(query, population):
        if query.get("gameType") != [population]:
            raise AssertionError(f"wrong research schedule population: {query}")


def build_research_fixture(root: Path, *, count=4, variant=None, observation_prefix="postseason-fixture"):
    card = root / "audit/v5_1_bt/model_card.json"
    card.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CARD, card)
    fetcher = ResearchFetcher(count, variant)
    counter = iter(range(10000))
    ids = lambda: f"{observation_prefix}-{next(counter)}"
    durable = lambda: datetime(2026, 7, 21, 23, 5 if variant == "at_start" else 0, 0 if variant == "at_start" else 2, tzinfo=timezone.utc) if variant == "at_start" else datetime(2026, 7, 21, 16, 0, 2, tzinfo=timezone.utc)
    slate_defaults = dict(live_sources.observe_live_slate.__kwdefaults__ or {})
    final_defaults = dict(live_sources.observe_final_game_schedule.__kwdefaults__ or {})
    slate_defaults.update(observation_id_factory=ids, durable_clock=durable)
    def final_clock():
        return datetime(2026, 7, 21, 23, 5, tzinfo=timezone.utc) if variant == "start_during" else durable()
    final_defaults.update(observation_id_factory=ids, durable_clock=final_clock)
    history_ticks = iter(range(10000))
    def history_clock():
        if variant == "mixed_precision":
            return "2026-07-21T17:00:02.12Z" if next(history_ticks) % 2 == 0 else "2026-07-21T17:00:02.1Z"
        return "2026-07-21T17:00:02Z"
    with (mock.patch.object(live_sources, "new_observation_id", side_effect=ids),
          mock.patch.object(live_history, "_canonical_utc_now", side_effect=history_clock),
          mock.patch.object(live_history, "datetime", FixtureDateTime),
          mock.patch.object(live_sources.observe_live_slate, "__kwdefaults__", slate_defaults),
          mock.patch.object(live_sources.observe_final_game_schedule, "__kwdefaults__", final_defaults)):
        result = daily_top3.run_daily_top3(root, TARGET, Path("audit/v5_1_bt/model_card.json"),
            fetcher=fetcher, utc_clock=lambda: datetime(2026, 7, 21, 16, tzinfo=timezone.utc),
            competition_scope="postseason-research-v1")
    return (*result, fetcher)


class PostseasonDailyTests(unittest.TestCase):
    def test_complete_slate_scores_four_supported_types_with_real_frozen_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload, path, markdown, _ = build_research_fixture(root)
            self.assertEqual([r["game_type"] for r in payload["games"]], ["F", "D", "L", "W"])
            self.assertEqual(len(payload["games"]), 4)
            trace = json.loads(path.with_suffix(".trace.json").read_bytes())
            self.assertEqual(len(trace["games"]), 4)
            card = load_frozen_live_card(root / "audit/v5_1_bt/model_card.json")
            for row in trace["games"]:
                scored = score_raw_game(row["raw_features"], card.normalization)
                self.assertAlmostEqual(row["p_home"], home_probability(scored["edge"], card.alpha, card.beta), places=14)
            self.assertIn("PASS — POSTSEASON_NOT_VALIDATED", markdown.read_text())
            self.assertEqual(payload["market_status"], "NOT_EVALUATED")
            self.assertEqual(payload["totals_reason"], "POSTSEASON_TOTALS_NOT_SUPPORTED")

    def test_zero_one_two_five_games_have_no_top3_quota(self):
        for count in (0, 1, 2, 5):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                payload, path, _, _ = build_research_fixture(Path(temporary), count=count)
                self.assertEqual(len(payload["games"]), count)
                self.assertEqual(len(json.loads(path.with_suffix(".trace.json").read_bytes())["games"]), count)
                self.assertTrue(all("rank" not in row for row in payload["games"]))

    def test_candidate_missing_inputs_explicit_and_unsupported_not_acquired(self):
        for variant in ("partial", "missing_starter", "unsupported"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                payload, _, _, fetcher = build_research_fixture(Path(temporary), variant=variant)
                row = payload["games"][-1 if variant == "unsupported" else 0]
                self.assertEqual(row["state"], "EXCLUDED" if variant == "unsupported" else "UNSCORED")
                self.assertIsNone(row["p_home"])
                self.assertIsNone(row["feature_sha256"])
                if variant == "unsupported":
                    self.assertFalse(any("/game/880004/boxscore" in url for _, url in fetcher.calls))

    def test_equality_and_start_during_acquisition_excluded(self):
        for variant in ("at_start", "start_during"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                payload, _, _, _ = build_research_fixture(Path(temporary), count=1, variant=variant)
                self.assertEqual(payload["games"][0]["state"], "EXCLUDED")
                self.assertIsNone(payload["games"][0]["p_home"])

    def test_final_type_and_team_identity_changes_fail_closed(self):
        for variant in ("final_type", "final_team"):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as temporary:
                payload, _, _, _ = build_research_fixture(Path(temporary), count=1, variant=variant)
                self.assertEqual(payload["games"][0]["state"], "EXCLUDED")

    def test_cli_default_and_explicit_scope(self):
        self.assertEqual(daily_top3.parse_args(["--date", TARGET]).competition_scope, "regular-season")
        self.assertEqual(daily_top3.parse_args(["--date", TARGET, "--competition-scope", "postseason-research-v1"]).competition_scope, "postseason-research-v1")

    def test_actual_frozen_exact_neutral_has_no_winner_at_pure_service_boundary(self):
        from mlb_backtest import postseason_research_service as service
        from mlb_backtest.scoring import WEIGHTS
        self.assertTrue(hasattr(service, "_scored_game_fields"), "pure score-to-report mapping is missing")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report, path, _, _ = build_research_fixture(root, count=1)
            trace = json.loads(path.with_suffix(".trace.json").read_bytes())
            raw = deepcopy(trace["games"][0]["raw_features"])
            # Synthetic pure-scoring fixture, deliberately not source-authoritative.
            raw["home"] = deepcopy(raw["away"])
            raw["away"]["schedule"]["value"] = 50.0
            raw["home"]["schedule"]["value"] = 50.0
            card = load_frozen_live_card(root / "audit/v5_1_bt/model_card.json")
            raw["home"]["schedule"]["value"] += (-card.alpha * 10.0 / card.beta) / WEIGHTS["schedule"]
            scored = score_raw_game(raw, card.normalization)
            self.assertEqual(home_probability(scored["edge"], card.alpha, card.beta), 0.5)
            mapped = service._scored_game_fields(report["games"][0], raw, scored, card)
            self.assertEqual(mapped["state"], "UNSCORED")
            self.assertEqual(mapped["p_home"], 0.5)
            self.assertEqual(mapped["predicted_side"], "none")
            self.assertIsNone(mapped["predicted_winner"])
            self.assertEqual(mapped["reason_codes"], ["EXACT_NEUTRAL_SCORE"])

    def test_market_totals_and_shadow_are_never_invoked(self):
        with (mock.patch("mlb_backtest.multibook_ml_evaluator.evaluate_moneylines", side_effect=AssertionError("market invoked")),
              mock.patch("mlb_backtest.daily_totals.build_daily_totals", side_effect=AssertionError("totals invoked")),
              mock.patch("mlb_backtest.prospective_ml_shadow.build_shadow_attachment", side_effect=AssertionError("shadow invoked")),
              tempfile.TemporaryDirectory() as temporary):
            report, _, _, _ = build_research_fixture(Path(temporary), count=1)
            self.assertEqual(report["market_reason"], "POSTSEASON_NOT_VALIDATED")
