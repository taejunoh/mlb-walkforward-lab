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
    def __init__(
        self,
        payload=b"payload",
        final_url="https://example.test/final",
        content_length=None,
    ):
        self.payload = payload
        self.final_url = final_url
        self.offset = 0
        self.read_sizes = []
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.final_url

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size < 0:
            chunk = self.payload[self.offset :]
            self.offset = len(self.payload)
            return chunk
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


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

    def test_fetch_bytes_rejects_unsafe_initial_and_final_urls(self):
        with mock.patch("mlb_backtest.sources.urlopen") as mocked_open:
            for url in ("file:///etc/passwd", "ftp://example.test/data", "relative"):
                with self.subTest(initial=url), self.assertRaises(ValueError):
                    fetch_bytes(url)
            mocked_open.assert_not_called()

        response = _Response(final_url="file:///etc/passwd")
        with mock.patch("mlb_backtest.sources.urlopen", return_value=response):
            with self.assertRaises(ValueError):
                fetch_bytes("https://example.test/redirect")
        self.assertEqual(response.read_sizes, [])

    def test_fetch_bytes_validates_timeout_and_max_bytes(self):
        invalid_timeouts = (0, -1, float("inf"), float("nan"), True, "90")
        for timeout in invalid_timeouts:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                fetch_bytes("https://example.test", timeout=timeout)

        invalid_limits = (0, -1, True, 1.5)
        for max_bytes in invalid_limits:
            with self.subTest(max_bytes=max_bytes), self.assertRaises(ValueError):
                fetch_bytes("https://example.test", max_bytes=max_bytes)

    def test_fetch_bytes_rejects_declared_and_streamed_oversize_payloads(self):
        declared = _Response(payload=b"x", content_length=5)
        with mock.patch("mlb_backtest.sources.urlopen", return_value=declared):
            with self.assertRaisesRegex(ValueError, "size limit"):
                fetch_bytes("https://example.test", max_bytes=4)
        self.assertEqual(declared.read_sizes, [])

        streamed = _Response(payload=b"12345")
        with mock.patch("mlb_backtest.sources.urlopen", return_value=streamed):
            with self.assertRaisesRegex(ValueError, "size limit"):
                fetch_bytes("https://example.test", max_bytes=4)
        self.assertTrue(streamed.read_sizes)

    def test_regular_season_game_is_fully_normalized(self):
        games = parse_schedule(FIXTURE)

        self.assertEqual(len(games), 1)
        self.assertEqual(
            games[0],
            {
                "game_pk": 778100,
                "official_date": "2025-05-01",
                "game_type": "R",
                "scheduled_start_utc": "2025-05-01T16:35:00Z",
                "status": "Final",
                "away_team": "Chicago Cubs",
                "home_team": "Pittsburgh Pirates",
                "away_probable_pitcher_id": 607067,
                "away_probable_pitcher_name": "Colin Rea",
                "home_probable_pitcher_id": 694973,
                "home_probable_pitcher_name": "Paul Skenes",
                "venue_id": 31,
                "venue_name": "PNC Park",
                "away_lineup": [
                    {"player_id": 664023, "player_name": "Ian Happ"},
                    {"player_id": 663656, "player_name": "Kyle Tucker"},
                    {"player_id": 673548, "player_name": "Seiya Suzuki"},
                    {"player_id": 683737, "player_name": "Michael Busch"},
                    {"player_id": 663538, "player_name": "Nico Hoerner"},
                    {"player_id": 691718, "player_name": "Pete Crow-Armstrong"},
                    {"player_id": 665804, "player_name": "Miguel Amaya"},
                    {"player_id": 621020, "player_name": "Dansby Swanson"},
                    {"player_id": 670032, "player_name": "Nicky Lopez"},
                ],
                "home_lineup": [
                    {"player_id": 665833, "player_name": "Oneil Cruz"},
                    {"player_id": 668804, "player_name": "Bryan Reynolds"},
                    {"player_id": 457705, "player_name": "Andrew McCutchen"},
                    {"player_id": 665839, "player_name": "Enmanuel Valdez"},
                    {"player_id": 663647, "player_name": "Ke'Bryan Hayes"},
                    {"player_id": 672744, "player_name": "Alexander Canario"},
                    {"player_id": 624428, "player_name": "Adam Frazier"},
                    {"player_id": 669707, "player_name": "Jared Triolo"},
                    {"player_id": 680779, "player_name": "Henry Davis"},
                ],
            },
        )

    def test_official_top_level_lineups_normalize_nine_ids_and_names(self):
        game = parse_schedule(FIXTURE)[0]

        self.assertEqual(len(game["away_lineup"]), 9)
        self.assertEqual(len(game["home_lineup"]), 9)
        self.assertEqual(
            [player["player_id"] for player in game["away_lineup"]],
            [
                664023,
                663656,
                673548,
                683737,
                663538,
                691718,
                665804,
                621020,
                670032,
            ],
        )
        self.assertEqual(
            [player["player_name"] for player in game["home_lineup"]],
            [
                "Oneil Cruz",
                "Bryan Reynolds",
                "Andrew McCutchen",
                "Enmanuel Valdez",
                "Ke'Bryan Hayes",
                "Alexander Canario",
                "Adam Frazier",
                "Jared Triolo",
                "Henry Davis",
            ],
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

    def test_scheduled_start_offset_is_normalized_to_canonical_utc(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["dates"][0]["games"][0]["gameDate"] = (
            "2025-05-01T19:10:00-04:00"
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            game = parse_schedule(path)[0]

        self.assertEqual(game["scheduled_start_utc"], "2025-05-01T23:10:00Z")

    def test_malformed_and_naive_scheduled_starts_raise_value_error(self):
        for start in ("not-a-time", "2025-05-01T23:10:00"):
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload["dates"][0]["games"][0]["gameDate"] = start
            with self.subTest(start=start), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "schedule.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ValueError):
                    parse_schedule(path)

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
