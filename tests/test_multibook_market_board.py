from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mlb_backtest.market_play_contracts import canonical_json_bytes, content_digest, sidecar_bytes
from mlb_backtest.multibook_market_board import (
    SCHEDULE_SCHEMA,
    load_market_board,
    load_schedule_authority,
    normalize_market_board,
    persist_market_board,
)
from mlb_backtest.multibook_market_capture import SOURCE_HOSTS, admit_raw_market_capture
from tests.test_multibook_market_capture import raw_capture


class BoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        schedule: dict[str, object] = {
            "schema": SCHEDULE_SCHEMA,
            "content_sha256": "",
            "target_date": "2026-09-02",
            "games": [
                {"game_pk": 1, "away_team": "AAA", "home_team": "BBB", "scheduled_start_utc": "2026-09-02T19:00:00Z"},
                {"game_pk": 2, "away_team": "CCC", "home_team": "DDD", "scheduled_start_utc": "2026-09-02T23:00:00Z"},
            ],
        }
        schedule["content_sha256"] = ""
        schedule["content_sha256"] = content_digest(schedule)
        rel = Path("audit/experimental_multibook_market/schedule/2026-09-02.json")
        path = self.root / rel
        path.parent.mkdir(parents=True)
        raw = canonical_json_bytes(schedule)
        path.write_bytes(raw)
        path.with_suffix(".sha256").write_bytes(sidecar_bytes(raw))
        self.schedule_relative = rel
        self.outcomes: list[Path] = []
        for book in SOURCE_HOSTS:
            payload = raw_capture(book)
            payload["content_sha256"] = ""
            payload["content_sha256"] = content_digest(payload)
            raw_bytes = canonical_json_bytes(payload)
            rel_raw = Path("data/experimental_multibook_market/raw") / f"{payload['content_sha256']}.json"
            raw_path = self.root / rel_raw
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(raw_bytes)
            raw_path.with_suffix(".sha256").write_bytes(sidecar_bytes(raw_bytes))
            self.outcomes.append(admit_raw_market_capture(self.root, rel_raw))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_board_maps_five_book_events_and_keeps_zero_quote_games(self):
        built = normalize_market_board(self.root, self.schedule_relative, self.outcomes)
        self.assertEqual([g["game_pk"] for g in built.board["games"]], [1, 2])
        self.assertEqual(len(built.board["capture_outcomes"]), 5)
        self.assertEqual(built.board["games"][-1]["moneylines"], [])
        persist_market_board(self.root, built)
        schedule = load_schedule_authority(self.root, self.schedule_relative)
        board_path = Path("data/experimental_multibook_market/boards") / f"{built.board['content_sha256']}.json"
        loaded = load_market_board(self.root, board_path, schedule)
        self.assertEqual(len(loaded.games), 2)

    def test_board_rejects_missing_duplicate_or_unknown_book_outcomes(self):
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, self.outcomes[:4])
        with self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_relative, (*self.outcomes[:4], self.outcomes[0]))


if __name__ == "__main__":
    unittest.main()
