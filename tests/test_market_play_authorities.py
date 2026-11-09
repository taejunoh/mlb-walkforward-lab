"""Contract tests for the sealed market-play authority adapters."""

from __future__ import annotations

import json
import hashlib
import math
import tempfile
import unittest
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from mlb_backtest.market_play_authorities import (
    MlAuthority,
    ParticipantGateEvidence,
    _participant_evidence,
    _ml_games,
    load_ml_authority,
    load_totals_authority,
    rebind_authorities,
)
from mlb_backtest.secure_fs import open_trusted_root
from mlb_backtest.source_parsers.schedule import parse_schedule
from mlb_backtest.market_play_contracts import MlQuote
from mlb_backtest.multibook_market_board import MarketBoardAuthority, MarketBoardGame
from mlb_backtest.multibook_ml_evaluator import evaluate_moneylines


ROOT = Path(__file__).resolve().parents[1]


def _quality_map() -> dict[str, float]:
    return {
        "starter": 0.9,
        "offense": 0.8,
        "bullpen": 0.7,
        "environment": 0.0,
        "defense": 0.0,
        "schedule": 1.0,
    }


def _ml_games_for_quality_test(
    qualities: dict[str, object], *, primary_data_completeness: float = 0.705
):
    report = {
        "verified_top3": [
            {
                "game_pk": 1,
                "state": "PRELIMINARY",
                "rank": 1,
                "reason_codes": [],
                "data_completeness": primary_data_completeness,
            }
        ],
        "preliminary_top3": [],
        "unscored_games": [],
        "excluded_games": [],
    }
    trace = {
        "games": [
            {
                "game_pk": 1,
                "predicted_side": "home",
                "pick_probability": 0.6,
                "qualities": dict(qualities),
                "primary_rank": 1,
                "disposition": "PRELIMINARY",
            },
            {
                "game_pk": 2,
                "predicted_side": "away",
                "pick_probability": 0.6,
                "qualities": dict(qualities),
                "primary_rank": None,
                "disposition": "SCORED_NOT_TOP3",
            },
        ]
    }
    schedule = {
        game_pk: {
            "scheduled_start_utc": f"2026-09-03T{20 + game_pk:02d}:00:00Z",
            "away_team": "Away",
            "home_team": "Home",
        }
        for game_pk in (1, 2)
    }
    return _ml_games(report, trace, schedule, [], object(), {}), report


class MarketPlayAuthorityTests(unittest.TestCase):
    def test_participant_evidence_exposes_authenticated_facts(self) -> None:
        fixtures = ROOT / "tests" / "fixtures" / "live_top3"
        schedule_rows = parse_schedule(
            (fixtures / "schedule_complete.json").read_bytes()
        )
        schedule = {int(row["game_pk"]): row for row in schedule_rows}
        payload = (fixtures / "boxscore_complete.json").read_bytes()
        payload_sha = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            root = Path(temporary)
            relative = Path("participant.json")
            (root / relative).write_bytes(payload)
            binding = {
                "game_pks": (777001,),
                "payload_relative": relative.as_posix(),
                "payload_sha256": payload_sha,
                "receipt_relative": "receipt.json",
                "receipt_sha256": "b" * 64,
                "durable_at_utc": "2026-07-21T15:00:02Z",
            }
            with open_trusted_root(root) as trusted:
                evidence = _participant_evidence(
                    schedule,
                    777001,
                    (binding,),
                    {},
                    trusted,
                )
        self.assertEqual(evidence.away_lineup, tuple(range(1001, 1010)))
        self.assertEqual(evidence.home_lineup, tuple(range(2001, 2010)))
        self.assertEqual(evidence.away_observed_starter_id, 501)
        self.assertEqual(evidence.home_observed_starter_id, 601)
        self.assertEqual(evidence.away_expected_starter_id, 501)
        self.assertEqual(evidence.home_expected_starter_id, 601)
        self.assertEqual(evidence.payload_relative, Path("participant.json"))
        self.assertEqual(evidence.payload_sha256, payload_sha)
        self.assertEqual(evidence.receipt_relative, Path("receipt.json"))
        self.assertEqual(evidence.receipt_sha256, "b" * 64)
        self.assertEqual(evidence.durable_at_utc, "2026-07-21T15:00:02Z")

    def test_missing_participant_binding_does_not_invent_facts(self) -> None:
        schedule = {
            1: {
                "game_pk": 1,
                "away_probable_pitcher_id": 501,
                "home_probable_pitcher_id": 601,
            }
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            with open_trusted_root(Path(temporary)) as trusted:
                evidence = _participant_evidence(
                    schedule, 1, (), {}, trusted
                )
        self.assertFalse(evidence.complete)
        self.assertEqual(evidence.away_lineup, ())
        self.assertEqual(evidence.home_lineup, ())
        self.assertIsNone(evidence.away_observed_starter_id)
        self.assertIsNone(evidence.home_observed_starter_id)
        self.assertIsNone(evidence.away_expected_starter_id)
        self.assertIsNone(evidence.home_expected_starter_id)
        self.assertIsNone(evidence.payload_relative)
        self.assertIsNone(evidence.payload_sha256)
        self.assertIsNone(evidence.receipt_relative)
        self.assertIsNone(evidence.receipt_sha256)
        self.assertIsNone(evidence.durable_at_utc)

    def test_participant_gate_is_immutable_and_exact(self) -> None:
        evidence = ParticipantGateEvidence(True, True, False, ("PROBABLE_CONSISTENT",))
        self.assertTrue(evidence.complete)
        with self.assertRaises((AttributeError, TypeError)):
            evidence.complete = False  # type: ignore[misc]

    def test_ml_adapter_uses_weighted_completeness_for_ranked_and_trace_only_rows(self) -> None:
        games, report = _ml_games_for_quality_test(_quality_map())

        # Hand-derived: .35*.9 + .25*.8 + .20*.7 + .10*0 + .05*0 + .05*1.
        expected = Fraction("0.705")
        self.assertEqual(games[0].data_completeness, expected)
        self.assertEqual(games[1].data_completeness, expected)
        self.assertEqual(
            games[0].data_completeness,
            Fraction(str(report["verified_top3"][0]["data_completeness"])),
        )

    def test_completeness_below_half_remains_an_evaluator_rejection(self) -> None:
        qualities = {name: 0.6 for name in ("starter", "offense", "bullpen")}
        qualities.update({"environment": 0.0, "defense": 0.0, "schedule": 0.0})
        games, _ = _ml_games_for_quality_test(
            qualities, primary_data_completeness=0.48
        )
        games = tuple(
            replace(
                game,
                participant=ParticipantGateEvidence(True, True, False, ()),
            )
            for game in games
        )
        authority = MlAuthority(
            games, "2026-09-03", Path("trace.json"), Path.cwd(), ()
        )
        quotes = tuple(
            MlQuote(1, book, book, "2026-09-03T20:58:00Z", 100, 100)
            for book in ("FANDUEL", "DRAFTKINGS", "BETMGM")
        )
        board = MarketBoardAuthority(
            "2026-09-03",
            (MarketBoardGame(1, "Away", "Home", "2026-09-03T21:00:00Z", quotes, ()),),
            Path("board.json"),
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
        )
        row = evaluate_moneylines(
            authority, board, evaluated_at="2026-09-03T20:59:59Z"
        )[0]
        self.assertEqual(games[0].data_completeness, Fraction("0.48"))
        self.assertEqual(row["decision"], "PASS")
        self.assertEqual(row["reason_codes"], ["DATA_COMPLETENESS_TOO_LOW"])

    def test_ml_adapter_rejects_malformed_quality_mapping(self) -> None:
        invalid = {
            "nonfinite": {**_quality_map(), "starter": math.inf},
            "boolean": {**_quality_map(), "starter": True},
            "missing": {
                name: value
                for name, value in _quality_map().items()
                if name != "starter"
            },
            "extra": {**_quality_map(), "overall_completeness": 0.9},
            "outofrange": {**_quality_map(), "starter": 1.1},
        }
        for label, qualities in invalid.items():
            with self.subTest(label=label), self.assertRaises(ValueError):
                _ml_games_for_quality_test(qualities)

    def test_ml_adapter_unions_scored_not_top3_and_unscored_primary_population(self) -> None:
        traces = sorted(
            ROOT.glob("reports/experimental_daily/*/*.trace.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if not traces:
            self.skipTest("sealed live report fixture is not present")
        trace = json.loads(traces[-1].read_text(encoding="utf-8"))
        primary = json.loads(
            (ROOT / trace["primary_report_relative"]).read_text(encoding="utf-8")
        )
        trace_rows = trace["games"]
        primary_rows = [
            *primary["verified_top3"],
            *primary["preliminary_top3"],
            *primary["unscored_games"],
            *primary["excluded_games"],
        ]
        scored_pks = {
            row["game_pk"] for row in trace_rows if row["predicted_side"] != "none"
        }
        unscored_pks = {
            row["game_pk"] for row in trace_rows if row["predicted_side"] == "none"
        }
        unscored_pks.update(
            row["game_pk"]
            for row in primary_rows
            if row["game_pk"] not in scored_pks and row["game_pk"] not in unscored_pks
        )
        authority = load_ml_authority(ROOT, traces[-1])
        self.assertEqual(authority.model_card_sha256, trace["model_card_sha256"])
        self.assertEqual(
            {game.game_pk for game in authority.games}, scored_pks | unscored_pks
        )
        for row in trace_rows:
            game = authority.by_game[row["game_pk"]]
            self.assertEqual(game.p_home, Fraction(str(row["p_home"])))
        scored = [game for game in authority.games if game.model_status == "SCORED"]
        unscored = [game for game in authority.games if game.model_status == "UNSCORED"]
        self.assertEqual({game.game_pk for game in scored}, scored_pks)
        self.assertEqual({game.game_pk for game in unscored}, unscored_pks)
        trace_only_scored = {
            row["game_pk"]
            for row in trace_rows
            if row["predicted_side"] != "none"
            and row["disposition"] == "SCORED_NOT_TOP3"
        }
        self.assertEqual(
            {
                game.game_pk
                for game in scored
                if game.legacy_primary_rank is None
            },
            trace_only_scored,
        )
        self.assertEqual(
            {game.game_pk for game in unscored if game.pick_probability is None},
            unscored_pks,
        )
        rebind_authorities(authority)

    def test_totals_adapter_preserves_receipt_residual_count(self) -> None:
        traces = sorted(ROOT.glob("reports/experimental_daily_totals/*/*.trace.json"))
        if not traces:
            self.skipTest("sealed totals report fixture is not present")
        trace = json.loads(traces[-1].read_text(encoding="utf-8"))
        receipt = json.loads(
            (ROOT / trace["model_receipt_relative"]).read_text(encoding="utf-8")
        )
        authority = load_totals_authority(ROOT, traces[-1])
        projected = [game for game in authority.games if game.status == "PROJECTED"]
        self.assertTrue(projected)
        self.assertTrue(
            all(
                game.residual_count == receipt["development_residual_count"]
                and len(game.residual_samples) == game.residual_count
                for game in projected
            )
        )
        self.assertIsInstance(projected[0].expected_total, Fraction)
        rebind_authorities(authority)


if __name__ == "__main__":
    unittest.main()
