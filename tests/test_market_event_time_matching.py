from __future__ import annotations

import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from mlb_backtest.market_play_contracts import canonical_json_bytes, content_digest, sidecar_bytes
from mlb_backtest.market_play_authorities import MlAuthority, MlGameAuthority, ParticipantGateEvidence
from mlb_backtest.multibook_ml_evaluator import evaluate_moneylines
from mlb_backtest.multibook_market_board import (
    BOARD_SCHEMA_V1, BOARD_SCHEMA_V2, SCHEDULE_SCHEMA, load_market_board,
    load_schedule_authority, normalize_market_board, persist_market_board,
)
from mlb_backtest.multibook_market_capture import RAW_PREFIX, admit_raw_market_capture
from tests.test_multibook_market_capture import raw_capture


class MarketEventTimeMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.schedule_relative = Path("audit/experimental_multibook_market/schedule/2026-09-02.json")
        self._write_schedule([
            {"game_pk": 10, "away_team": "MIN", "home_team": "CWS", "scheduled_start_utc": "2026-09-02T23:59:30Z"},
            {"game_pk": 20, "away_team": "WSH", "home_team": "LAD", "scheduled_start_utc": "2026-09-03T00:30:00Z"},
        ])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_schedule(self, games: list[dict[str, object]]) -> None:
        schedule: dict[str, object] = {
            "schema": SCHEDULE_SCHEMA, "content_sha256": "", "target_date": "2026-09-02", "games": games,
        }
        schedule["content_sha256"] = content_digest(schedule)
        path = self.root / self.schedule_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        contents = canonical_json_bytes(schedule)
        path.write_bytes(contents)
        path.with_suffix(".sha256").write_bytes(sidecar_bytes(contents))

    def _admit_events(self, events: list[dict[str, object]], book: str = "FANDUEL") -> Path:
        raw = raw_capture(book)
        raw["events"] = events
        raw["content_sha256"] = content_digest({**raw, "content_sha256": ""})
        contents = canonical_json_bytes(raw)
        relative = Path(RAW_PREFIX / f"{raw['content_sha256']}.json")
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        path.with_suffix(".sha256").write_bytes(sidecar_bytes(contents))
        return admit_raw_market_capture(self.root, relative)

    @staticmethod
    def _event(event_id: str, away: str, home: str, start: str) -> dict[str, object]:
        return {
            "book_event_id": event_id, "away_team": away, "home_team": home,
            "scheduled_start_utc": start,
            "moneyline": {"away_american_odds": -110, "home_american_odds": 100},
            "totals": [{"line_half_runs": 85, "over_american_odds": -105, "under_american_odds": -115}],
        }

    def test_v3_accepts_both_observed_plus_sixty_second_cross_utc_day_matches(self):
        outcome = self._admit_events([
            self._event("min-cws", "MIN", "CWS", "2026-09-03T00:00:30Z"),
            self._event("wsh-lad", "WSH", "LAD", "2026-09-03T00:31:00Z"),
        ])
        built = normalize_market_board(self.root, self.schedule_relative, (outcome,))
        self.assertEqual(
            [(entry["source_event_id"], entry["game_pk"], entry["delta_nanoseconds"], entry["rule"]) for entry in built.board["event_match_audit"]],
            [("min-cws", 10, 60_000_000_000, "UNIQUE_ORDERED_TEAMS_60S"), ("wsh-lad", 20, 60_000_000_000, "UNIQUE_ORDERED_TEAMS_60S")],
        )
        self.assertEqual(built.board["games"][0]["scheduled_start_utc"], "2026-09-02T23:59:30Z")

    def test_v3_accepts_minus_sixty_seconds_and_preserves_fractional_nanoseconds(self):
        outcome = self._admit_events([
            self._event("minus", "WSH", "LAD", "2026-09-03T00:29:00Z"),
            self._event("fraction", "MIN", "CWS", "2026-09-03T00:00:29.123456789Z"),
        ])
        built = normalize_market_board(self.root, self.schedule_relative, (outcome,))
        audit = {item["source_event_id"]: item for item in built.board["event_match_audit"]}
        self.assertEqual(audit["minus"]["delta_nanoseconds"], -60_000_000_000)
        self.assertEqual(audit["fraction"]["delta_nanoseconds"], 59_123_456_789)
        self.assertEqual(audit["fraction"]["source_scheduled_start_utc"], "2026-09-03T00:00:29.123456789Z")

    def test_v3_rejects_one_nanosecond_outside_the_time_window(self):
        for start in ("2026-09-03T00:00:30.000000001Z", "2026-09-02T23:58:29.999999999Z"):
            with self.subTest(start=start):
                outcome = self._admit_events([self._event(start, "MIN", "CWS", start)])
                with self.assertRaisesRegex(ValueError, "more than 60 seconds"):
                    normalize_market_board(self.root, self.schedule_relative, (outcome,))

    def test_v1_v2_exact_matching_does_not_admit_the_v3_tolerance(self):
        outcome = self._admit_events([
            self._event("legacy-offset", "MIN", "CWS", "2026-09-03T00:00:30Z")
        ])
        with self.assertRaisesRegex(ValueError, "does not map uniquely"):
            normalize_market_board(self.root, self.schedule_relative, (outcome,), board_schema=BOARD_SCHEMA_V2)
        v1_outcomes = tuple(
            self._admit_events([self._event(f"legacy-{book}", "MIN", "CWS", "2026-09-03T00:00:30Z")], book)
            for book in ("FANDUEL", "DRAFTKINGS", "BETMGM", "CAESARS", "FANATICS")
        )
        with self.assertRaisesRegex(ValueError, "does not map uniquely"):
            normalize_market_board(self.root, self.schedule_relative, v1_outcomes, board_schema=BOARD_SCHEMA_V1)

    def test_v3_rejects_two_events_from_one_book_mapped_to_one_game(self):
        outcome = self._admit_events([
            self._event("first", "MIN", "CWS", "2026-09-02T23:59:30Z"),
            self._event("second", "MIN", "CWS", "2026-09-03T00:00:30Z"),
        ])
        with self.assertRaisesRegex(ValueError, "duplicate book events mapped"):
            normalize_market_board(self.root, self.schedule_relative, (outcome,))

    def test_v3_rejects_unmatched_and_duplicate_source_event_ids(self):
        unmatched = self._admit_events([self._event("unmapped", "TOR", "BOS", "2026-09-02T23:59:30Z")])
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, (unmatched,))
        with self.assertRaisesRegex(ValueError, "duplicate book event ID"):
            self._admit_events([
                self._event("same-id", "MIN", "CWS", "2026-09-02T23:59:30Z"),
                self._event("same-id", "WSH", "LAD", "2026-09-03T00:30:00Z"),
            ])

    def test_v3_uses_official_start_for_game_gate_and_keeps_capture_timestamp_boundary(self):
        outcome = self._admit_events([
            self._event("offset", "MIN", "CWS", "2026-09-03T00:00:30Z")
        ])
        built = normalize_market_board(self.root, self.schedule_relative, (outcome,))
        board_path, _ = persist_market_board(self.root, built)
        board = load_market_board(self.root, board_path, load_schedule_authority(self.root, self.schedule_relative))
        games = tuple(
            MlGameAuthority(
                game.game_pk, game.scheduled_start_utc, game.away_team, game.home_team,
                "home", Fraction("0.60"), Fraction("0.60"),
                ParticipantGateEvidence(True, True, False, ()), "SCORED", None,
            )
            for game in board.games
        )
        authority = MlAuthority(games, "2026-09-02", Path("ml.trace.json"), self.root, ())
        self.assertEqual(board.games[0].moneylines[0].captured_at_utc, "2026-09-02T13:00:00Z")
        at_start = evaluate_moneylines(authority, board, evaluated_at="2026-09-02T23:59:30Z")[0]
        self.assertIn("GAME_STARTED", at_start["reason_codes"])
        at_300_seconds = evaluate_moneylines(authority, board, evaluated_at="2026-09-02T13:05:00Z")[0]
        self.assertNotIn("STALE_PRICE", at_300_seconds["reason_codes"])

    def test_v3_rejects_swapped_teams_and_doubleheader_tolerance_fallback(self):
        outcome = self._admit_events([self._event("swapped", "CWS", "MIN", "2026-09-02T23:59:30Z")])
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, (outcome,))
        self._write_schedule([
            {"game_pk": 10, "away_team": "MIN", "home_team": "CWS", "scheduled_start_utc": "2026-09-02T19:00:00Z"},
            {"game_pk": 11, "away_team": "MIN", "home_team": "CWS", "scheduled_start_utc": "2026-09-02T22:00:00Z"},
        ])
        outcome = self._admit_events([self._event("doubleheader", "MIN", "CWS", "2026-09-02T19:01:00Z")])
        with self.assertRaisesRegex(ValueError, "one ordered-team official candidate"):
            normalize_market_board(self.root, self.schedule_relative, (outcome,))
        exact = self._admit_events([self._event("exact-game-two", "MIN", "CWS", "2026-09-02T22:00:00Z")])
        built = normalize_market_board(self.root, self.schedule_relative, (exact,))
        self.assertEqual(built.board["event_match_audit"][0]["game_pk"], 11)
        self.assertEqual(built.board["event_match_audit"][0]["rule"], "EXACT")


if __name__ == "__main__":
    unittest.main()
