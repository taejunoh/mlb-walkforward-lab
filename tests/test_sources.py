import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from mlb_backtest.sources import (
    MLB_BASE,
    fetch_bytes,
    parse_schedule,
    schedule_url,
    selection_dates,
)


FIXTURE = Path(__file__).parent / "fixtures" / "schedule_2025_05_01.json"


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return b"payload"


class SourceTest(unittest.TestCase):
    def test_schedule_url_has_exact_query_semantics(self):
        parsed = urlparse(schedule_url("2025-05-01", "2025-08-12"))

        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", f"{MLB_BASE}/schedule")
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "sportId": ["1"],
                "gameType": ["R"],
                "startDate": ["2025-05-01"],
                "endDate": ["2025-08-12"],
                "hydrate": ["probablePitcher,lineups,venue"],
            },
        )

    @mock.patch("mlb_backtest.sources.urlopen", return_value=_Response())
    def test_fetch_bytes_sets_user_agent_and_timeout(self, mocked_open):
        self.assertEqual(fetch_bytes("https://example.test", timeout=7), b"payload")
        request = mocked_open.call_args.args[0]
        self.assertTrue(request.get_header("User-agent"))
        self.assertEqual(mocked_open.call_args.kwargs, {"timeout": 7})

    def test_regular_season_game_is_fully_normalized(self):
        games = parse_schedule(FIXTURE)

        self.assertEqual(len(games), 1)
        self.assertEqual(
            games[0],
            {
                "game_pk": 778498,
                "official_date": "2025-05-01",
                "game_type": "R",
                "scheduled_start_utc": "2025-05-01T23:10:00Z",
                "status": "Final",
                "away_team": "New York Yankees",
                "home_team": "San Diego Padres",
                "away_probable_pitcher_id": 592450,
                "away_probable_pitcher_name": "Marcus Stroman",
                "home_probable_pitcher_id": 605483,
                "home_probable_pitcher_name": "Yu Darvish",
                "venue_id": 2680,
                "venue_name": "Petco Park",
                "away_lineup": [
                    {"player_id": 1000 + slot, "player_name": f"Away Batter {slot}"}
                    for slot in range(1, 10)
                ],
                "home_lineup": [
                    {"player_id": 2000 + slot, "player_name": f"Home Batter {slot}"}
                    for slot in range(1, 10)
                ],
            },
        )

    def test_nullable_hydrated_fields_are_normalized(self):
        payload = {
            "dates": [{
                "date": "2025-05-02",
                "games": [{
                    "gamePk": 1,
                    "gameType": "R",
                    "gameDate": "2025-05-02T17:00:00Z",
                    "status": {"detailedState": "Scheduled"},
                    "teams": {
                        "away": {"team": {"name": "Away"}},
                        "home": {"team": {"name": "Home"}},
                    },
                }],
            }]
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            game = parse_schedule(path)[0]

        self.assertIsNone(game["away_probable_pitcher_id"])
        self.assertIsNone(game["away_probable_pitcher_name"])
        self.assertIsNone(game["home_probable_pitcher_id"])
        self.assertIsNone(game["home_probable_pitcher_name"])
        self.assertIsNone(game["venue_id"])
        self.assertIsNone(game["venue_name"])
        self.assertEqual(game["away_lineup"], [])
        self.assertEqual(game["home_lineup"], [])

    def test_malformed_payloads_raise_value_error_not_key_error(self):
        payloads = [
            {},
            {"dates": {}},
            {"dates": [{"date": "2025-05-01"}]},
            {"dates": [{"date": "2025-05-01", "games": [{"gameType": "R"}]}]},
            {"dates": [{"date": "2025-05-01", "games": [{
                "gamePk": 1,
                "gameType": "R",
                "gameDate": "2025-05-01T10:00:00Z",
                "status": {},
                "teams": {},
            }]}]},
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            for index, payload in enumerate(payloads):
                with self.subTest(index=index):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        parse_schedule(path)

    def test_invalid_json_and_invalid_utf8_raise_value_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            for content in (b"{", b"\xff"):
                with self.subTest(content=content):
                    path.write_bytes(content)
                    with self.assertRaises(ValueError):
                        parse_schedule(path)

    def test_selection_dates_are_sorted_distinct_regular_dates_in_range(self):
        games = [
            {"official_date": "2025-05-03", "game_type": "R"},
            {"official_date": "2025-05-01", "game_type": "R"},
            {"official_date": "2025-05-03", "game_type": "R"},
            {"official_date": "2025-05-02", "game_type": "S"},
            {"official_date": "2025-04-30", "game_type": "R"},
            {"official_date": "2025-05-04", "game_type": "R"},
        ]

        self.assertEqual(
            selection_dates(games, "2025-05-01", "2025-05-03"),
            ["2025-05-01", "2025-05-03"],
        )


if __name__ == "__main__":
    unittest.main()
