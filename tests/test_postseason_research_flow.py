"""Scope dispatch must fail before market or legacy artifact admission."""
import tempfile
import unittest
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from mlb_backtest.prospective_ml_daily_flow import run_pregame, read_status
from mlb_backtest.prospective_ml_daily_flow import run_postgame
from mlb_backtest.live_top3_contracts import LiveHttpObservation
from mlb_backtest.prospective_ml_postseason_research import load_claim, load_snapshot, read_json
from tests.test_postseason_research_daily import build_research_fixture


def official_fetcher(games, *, detail="Final", completed="2026-07-22T01:00:00Z", away_wins=False, tie=False):
    by_pk = {row["game_pk"]: row for row in games}
    def fetch(source, url, **kwargs):
        pk = int(url.rsplit("gamePks=", 1)[1])
        row = by_pk[pk]
        game = {"gamePk": pk, "gameType": row["game_type"], "officialDate": row["target_date"],
                "status": {"abstractGameState": "Final" if detail in {"Final", "No Contest"} else "Live", "detailedState": detail},
                "teams": {"away": {"team": {"id": row["away_team_id"], "name": row["away_team"]}, "score": 5 if tie or away_wins else 2, "isWinner": away_wins and not tie, "probablePitcher": {"id": 99999}},
                          "home": {"team": {"id": row["home_team_id"], "name": row["home_team"]}, "score": 5, "isWinner": not away_wins and not tie}}}
        if away_wins and not tie:
            game["teams"]["away"]["score"] = 6
        body = json.dumps({"dates": [{"date": row["target_date"], "games": [game]}]}).encode()
        return LiveHttpObservation(url, url, completed, completed, 200, "application/json", None, None, body)
    return fetch


def durable():
    return datetime(2026, 7, 22, 1, 0, 3, tzinfo=timezone.utc)


class ResearchDispatchTests(unittest.TestCase):
    def test_market_inputs_are_rejected_before_any_artifact_read(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with self.assertRaisesRegex(ValueError, "market"):
                run_pregame(directory, "missing.trace.json", competition_scope="postseason-research-v1", market_trace="market.json")

    def test_unknown_scope_is_not_legacy_fallback(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with self.assertRaisesRegex(ValueError, "scope"):
                read_status(directory, "missing.json", competition_scope="unknown")

    def test_offline_end_to_end_scores_claims_settles_and_resumes_all_supported_types(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            payload, path, _, _ = build_research_fixture(root, count=4, variant="mixed_history")
            history = next(b for b in payload["source_bindings"] if b["binding_role"] == "HISTORY_SCHEDULE")
            population = json.loads(history["query_identity_json"])["coverage"]["game_identities"]
            self.assertEqual({row["game_type"] for row in population}, {"R", "F"})
            self.assertEqual({g["game_type"] for g in payload["games"]}, {"F", "D", "L", "W"})
            self.assertGreaterEqual(sum(g["state"] == "VERIFIED" for g in payload["games"]), 1)
            pre = run_pregame(root, path.with_suffix(".trace.json"), competition_scope="postseason-research-v1", clock=lambda: "2026-07-21T18:00:00Z")
            self.assertGreaterEqual(len(pre.receipt["primary_claims"]), 1)
            probabilities = [float(load_snapshot(root, load_claim(root, p)["snapshot_relative"])["p_home"]) for p in pre.receipt["primary_claims"]]
            post = run_postgame(root, pre.receipt_relative, competition_scope="postseason-research-v1", fetcher=official_fetcher(payload["games"]), durable_clock=durable)
            summary = read_json(root, post.receipt["report_relative"], "research summary")
            self.assertEqual(summary["sample_count"], len(probabilities))
            self.assertGreaterEqual(summary["sample_count"], 1)
            self.assertEqual(summary["hit_count"], sum(p > .5 for p in probabilities))
            self.assertAlmostEqual(float(summary["brier_score"]), sum((1-p)**2 for p in probabilities)/len(probabilities), places=6)
            self.assertAlmostEqual(float(summary["log_loss"]), sum(-math.log(max(p, 1e-15)) for p in probabilities)/len(probabilities), places=11)
            def no_fetch(*args, **kwargs):
                raise AssertionError("terminal resume must not recollect")
            resume = run_postgame(root, pre.receipt_relative, competition_scope="postseason-research-v1", previous_postgame_receipt=post.receipt_relative, fetcher=no_fetch, durable_clock=durable)
            self.assertEqual(resume.receipt["settlement_heads"], post.receipt["settlement_heads"])
            self.assertEqual(resume.receipt["report_relative"], post.receipt["report_relative"])
            before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
            status = read_status(root, pre.receipt_relative, competition_scope="postseason-research-v1", postgame_receipt=resume.receipt_relative, clock=lambda: "2026-07-22T02:00:00Z")
            self.assertTrue(status["complete_evidence"])
            self.assertEqual(before, sorted(str(p.relative_to(root)) for p in root.rglob("*")))
            self.assertFalse((root / "audit/prospective_ml_observation").exists())
            self.assertFalse((root / "audit/prospective_ml_daily_flow").exists())
            self.assertFalse((root / "reports/prospective_ml_calibration").exists())
            self.assertTrue(all(g["shadow_status"] == "NOT_EVALUATED_FOR_POSTSEASON" for g in status["games"]))
