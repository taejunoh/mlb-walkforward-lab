import hashlib
import json
import tempfile
from copy import deepcopy
from dataclasses import replace
import unittest
from pathlib import Path
from unittest import mock

import mlb_backtest.live_top3_service as service
from mlb_backtest.scoring import component_edge
from mlb_backtest.scoring import home_probability, predicted_pick
from mlb_backtest.scoring_prep import score_raw_game
from mlb_backtest.live_top3_service import build_live_top3_run
from mlb_backtest.live_top3_contracts import canonical_json_bytes
from mlb_backtest import daily_top3
from mlb_backtest.secure_fs import open_trusted_root
from mlb_backtest.live_top3_trace import (
    build_trace_payload,
    replay_stored_row,
    validate_trace_payload,
)
from tests.test_live_top3_service import (
    _bundle,
    _card,
    _career_inputs,
    _command_inputs,
    _final_observer,
    _game,
    _raw,
    _scorer,
    _slate,
    _times,
)


class LiveTop3TraceTests(unittest.TestCase):
    def _run(self, games, sink=None):
        career, career_bindings = _career_inputs()
        def scorer(raw, _normalization):
            game_pk = raw["game_pk"]
            names = ("starter", "offense", "bullpen", "environment", "defense", "schedule")
            away_scores = {name: 50.0 for name in names}
            home_scores = dict(away_scores)
            home_scores["starter"] += float(game_pk) / 10.0
            qualities = {name: 0.9 for name in names}
            return {
                "edge": component_edge(home_scores, away_scores),
                "away_scores": away_scores,
                "home_scores": home_scores,
                "away_qualities": qualities,
                "home_qualities": qualities,
                "qualities": qualities,
            }
        with mock.patch.object(service, "build_live_raw_game_features", side_effect=_raw), mock.patch.object(
            service, "score_raw_game", side_effect=scorer
        ):
            return build_live_top3_run(
                slate=_slate(games),
                final_schedule_observer=_final_observer(games),
                history=_bundle(),
                card=_card(),
                career_bf_by_pitcher=career,
                career_bf_bindings=career_bindings,
                command_inputs=_command_inputs(),
                execution_times=_times(),
                trace_sink=sink,
            )

    def test_sink_captures_every_scored_game_and_leaves_primary_unchanged(self):
        games = tuple(_game(game_pk, start="2025-05-01T23:00:00Z") for game_pk in range(1, 5))
        sink = []
        with_sink = self._run(games, sink)
        without_sink = self._run(games)
        self.assertEqual(with_sink, without_sink)
        self.assertEqual([row["game_pk"] for row in sink], [1, 2, 3, 4])
        self.assertEqual(
            sink[0]["feature_sha256"],
            hashlib.sha256(canonical_json_bytes(sink[0]["raw_features"])).hexdigest(),
        )
        self.assertEqual(sink[0]["disposition"], "SCORED_NOT_TOP3")
        self.assertEqual(sink[3]["disposition"], "PRELIMINARY")

    def test_exact_neutral_score_is_captured_as_unscored_without_rank(self):
        game = _game(1, start="2025-05-01T23:00:00Z")
        sink = []
        with mock.patch.object(service, "home_probability", return_value=0.5):
            primary = self._run((game,), sink)
        self.assertEqual(primary["preliminary_top3"], [])
        self.assertEqual(primary["unscored_games"][0]["reason_codes"], ["EXACT_NEUTRAL_SCORE"])
        self.assertEqual(len(sink), 1)
        self.assertIsNone(sink[0]["primary_rank"])
        self.assertEqual(sink[0]["disposition"], "UNSCORED_EXACT_NEUTRAL")
        self.assertEqual(sink[0]["predicted_side"], "none")

    def test_trace_payload_rejects_changed_contribution(self):
        games = (_game(1, start="2025-05-01T23:00:00Z"),)
        sink = []
        primary = self._run(games, sink)
        trace = build_trace_payload(
            primary_report_relative="reports/experimental_daily/2025-05-01/report.json",
            primary_report_content_sha256=primary["content_sha256"],
            model_card_sha256=primary["model_card_sha256"],
            games=sink,
        )
        self.assertEqual(validate_trace_payload(trace), trace)
        trace["games"][0]["contributions"]["starter"] += 1.0
        self._rehash_trace(trace)
        with self.assertRaises(ValueError):
            validate_trace_payload(trace)

    def test_trace_payload_rejects_inconsistent_neutral_record_state(self):
        game = _game(1, start="2025-05-01T23:00:00Z")
        sink = []
        primary = self._run((game,), sink)
        trace = build_trace_payload(
            primary_report_relative="reports/experimental_daily/2025-05-01/report.json",
            primary_report_content_sha256=primary["content_sha256"],
            model_card_sha256=primary["model_card_sha256"],
            games=sink,
        )
        row = trace["games"][0]
        row["p_home"] = 0.6
        row["predicted_side"] = "none"
        row["pick_probability"] = 0.5
        row["primary_rank"] = 1
        row["disposition"] = "PRELIMINARY"
        self._rehash_trace(trace)
        with self.assertRaises(ValueError):
            validate_trace_payload(trace)

    def test_recomputed_digest_tampering_reaches_semantic_validation(self):
        game = _game(1, start="2025-05-01T23:00:00Z")
        sink = []
        primary = self._run((game,), sink)
        trace = build_trace_payload(
            primary_report_relative="reports/experimental_daily/2025-05-01/report.json",
            primary_report_content_sha256=primary["content_sha256"],
            model_card_sha256=primary["model_card_sha256"],
            games=sink,
        )

        raw_tamper = deepcopy(trace)
        raw_tamper["games"][0]["raw_features"]["game_pk"] = 99
        raw_tamper["games"][0]["feature_sha256"] = hashlib.sha256(
            canonical_json_bytes(raw_tamper["games"][0]["raw_features"])
        ).hexdigest()
        self._rehash_trace(raw_tamper)
        with self.assertRaises(ValueError):
            validate_trace_payload(raw_tamper)

        probability_tamper = deepcopy(trace)
        probability_tamper["games"][0]["p_home"] += 0.1
        probability_tamper["games"][0]["pick_probability"] += 0.1
        self._rehash_trace(probability_tamper)
        with self.assertRaises(ValueError):
            replay_stored_row(probability_tamper["games"][0], _card())

        card_tamper = deepcopy(trace)
        card_tamper["games"][0]["model_card_sha256"] = "a" * 64
        self._rehash_trace(card_tamper)
        with self.assertRaises(ValueError):
            validate_trace_payload(card_tamper)
        with self.assertRaises(ValueError):
            replay_stored_row(
                card_tamper["games"][0], _card()
            )
        with self.assertRaises(ValueError):
            replay_stored_row(trace["games"][0], replace(_card(), file_sha256="a" * 64))
        with self.assertRaises(ValueError):
            validate_trace_payload(
                trace,
                expected_primary_report_content_sha256="b" * 64,
            )

    def test_nonfinite_or_boolean_score_trace_numbers_are_rejected_after_rehash(self):
        game = _game(1, start="2025-05-01T23:00:00Z")
        sink = []
        primary = self._run((game,), sink)
        trace = build_trace_payload(
            primary_report_relative="reports/experimental_daily/2025-05-01/report.json",
            primary_report_content_sha256=primary["content_sha256"],
            model_card_sha256=primary["model_card_sha256"],
            games=sink,
        )
        for field, value in (
            ("home_scores", float("nan")),
            ("qualities", float("inf")),
            ("contributions", True),
        ):
            with self.subTest(field=field):
                tampered = deepcopy(trace)
                key = "starter"
                tampered["games"][0][field][key] = value
                if isinstance(value, bool):
                    self._rehash_trace(tampered)
                    with self.assertRaises(ValueError):
                        validate_trace_payload(tampered)
                else:
                    with self.assertRaises(ValueError):
                        validate_trace_payload(tampered)

    @staticmethod
    def _rehash_trace(trace):
        without = {key: value for key, value in trace.items() if key != "content_sha256"}
        trace["content_sha256"] = hashlib.sha256(canonical_json_bytes(without)).hexdigest()

    def test_replay_stored_row_uses_production_scorer(self):
        card = _card()
        def side():
            starter = {
                metric: {"value": 0.0, "quality": 1.0}
                for metric in ("season_xwoba", "k_minus_bb", "fip", "last5_xwoba", "expected_ip", "velocity_change")
            }
            offense = {
                metric: {"value": 0.0, "quality": 1.0}
                for metric in ("platoon_xwoba", "last14_xwoba", "bb_minus_k", "barrel_rate", "iso")
            }
            offense["players"] = [
                {"player_id": index, "batting_slot": index}
                for index in range(1, 10)
            ]
            bullpen = {
                metric: {"value": 0.0, "quality": 1.0}
                for metric in ("season_xwoba", "k_minus_bb", "last14_xwoba", "availability")
            }
            return {
                "game_pk": 42,
                "official_date": "2025-05-01",
                "eligible": True,
                "reasons": [],
                "starter_provenance": "PROBABLE_CONSISTENT",
                "lineup_provenance": "OBSERVED_PREGAME_LINEUP",
                "away": {"starter": starter, "offense": offense, "bullpen": bullpen, "environment": {"value": 50.0, "quality": 1.0}, "defense": {"value": None, "quality": 0.0}, "schedule": {"value": 50.0, "quality": 1.0}},
                "home": {"starter": starter, "offense": offense, "bullpen": bullpen, "environment": {"value": 50.0, "quality": 1.0}, "defense": {"value": None, "quality": 0.0}, "schedule": {"value": 50.0, "quality": 1.0}},
            }
        raw = side()
        scored = score_raw_game(raw, card.normalization)
        p_home = home_probability(scored["edge"], card.alpha, card.beta)
        pick = predicted_pick("Home", "Away", p_home)
        row = {
            "game_pk": 42,
            "target_date": "2025-05-01",
            "away_team": "Away",
            "home_team": "Home",
            "scheduled_start_utc": "2025-05-01T23:00:00Z",
            "raw_features": raw,
            "feature_sha256": hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
            "model_card_sha256": card.file_sha256,
            "away_scores": scored["away_scores"],
            "home_scores": scored["home_scores"],
            "away_qualities": scored["away_qualities"],
            "home_qualities": scored["home_qualities"],
            "qualities": scored["qualities"],
            "contributions": {name: 0.0 for name in ("starter", "offense", "bullpen", "environment", "defense", "schedule")},
            "total_edge": scored["edge"],
            "p_home": p_home,
            "predicted_side": pick["predicted_side"],
            "pick_probability": pick["pick_probability"],
            "primary_rank": 1,
            "disposition": "PRELIMINARY",
        }
        self.assertEqual(replay_stored_row(row, card)["edge"], scored["edge"])

    def test_trace_persistence_binds_primary_and_preserves_it_on_write_failure(self):
        game = _game(1, start="2025-05-01T23:00:00Z")
        sink = []
        primary = self._run((game,), sink)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            json_path, _ = daily_top3.persist_live_top3_report(root, primary)
            with open_trusted_root(root) as trusted:
                trace_path = daily_top3._persist_bound_live_score_trace(
                    root, trusted, primary, sink
                )
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(trace["primary_report_content_sha256"], primary["content_sha256"])
            self.assertEqual(trace["primary_report_relative"], json_path.relative_to(root).as_posix())
            original_primary = json_path.read_bytes()
            trace_path.unlink()
            with mock.patch.object(daily_top3, "write_exclusive", side_effect=OSError("trace failure")):
                with self.assertRaises(OSError):
                    with open_trusted_root(root) as trusted:
                        daily_top3._persist_bound_live_score_trace(
                            root, trusted, {**primary, "content_sha256": primary["content_sha256"]}, sink
                        )
            self.assertEqual(json_path.read_bytes(), original_primary)


if __name__ == "__main__":
    unittest.main()
