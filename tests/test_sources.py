import json
import tempfile
import threading
import unittest
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
from mlb_backtest.source_parsers.schedule import select_schedule_occurrences


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
    def test_fetch_bytes_public_signature_remains_backward_compatible(self):
        self.assertEqual(
            str(inspect.signature(fetch_bytes)),
            "(url: str, timeout: int | float = 90, max_bytes: int = 67108864) -> bytes",
        )

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
                "hydrate": ["probablePitcher,lineups,venue,linescore"],
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

    def test_fetch_bytes_rejects_short_http_body_against_content_length(self):
        class ShortBodyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) + 5))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), ShortBodyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            with self.assertRaisesRegex(ValueError, "incomplete response"):
                fetch_bytes(f"http://{host}:{port}/schedule")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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
                "away_team_id": 112,
                "home_team": "Pittsburgh Pirates",
                "home_team_id": 134,
                "away_probable_pitcher_id": 607067,
                "away_probable_pitcher_name": "Colin Rea",
                "home_probable_pitcher_id": 694973,
                "home_probable_pitcher_name": "Paul Skenes",
                "venue_id": 31,
                "venue_name": "PNC Park",
                "away_score": 2,
                "home_score": 4,
                "away_is_winner": False,
                "home_is_winner": True,
                "scheduled_innings": None,
                "completed_innings": None,
                "final_outs": None,
                "final_inning_topbot": None,
                "doubleheader_marker": None,
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

    def test_explicit_non_doubleheader_remains_unknown_for_historical_parser(self):
        payload = json.loads(FIXTURE.read_bytes())
        raw_game = payload["dates"][0]["games"][0]
        raw_game["doubleHeader"] = "N"
        raw_game["gameNumber"] = 1

        games = parse_schedule(json.dumps(payload).encode())

        self.assertIsNone(games[0]["doubleheader_marker"])

    def test_explicit_non_doubleheader_can_be_preserved_as_closed_marker(self):
        payload = json.loads(FIXTURE.read_bytes())
        raw_game = payload["dates"][0]["games"][0]
        raw_game["doubleHeader"] = "N"
        raw_game["gameNumber"] = 1

        games = parse_schedule(
            json.dumps(payload).encode(), preserve_explicit_non_doubleheader=True
        )

        self.assertEqual(games[0]["doubleheader_marker"], "NO")

    def test_missing_doubleheader_fields_remain_unknown(self):
        payload = json.loads(FIXTURE.read_bytes())
        raw_game = payload["dates"][0]["games"][0]
        raw_game.pop("doubleHeader", None)
        raw_game.pop("gameNumber", None)

        games = parse_schedule(json.dumps(payload).encode())

        self.assertIsNone(games[0]["doubleheader_marker"])

    def test_invalid_game_number_is_rejected_for_non_doubleheader(self):
        payload = json.loads(FIXTURE.read_bytes())
        raw_game = payload["dates"][0]["games"][0]
        raw_game["doubleHeader"] = "N"
        raw_game["gameNumber"] = 0

        with self.assertRaisesRegex(ValueError, "gameNumber must be positive"):
            parse_schedule(json.dumps(payload).encode())

    def test_preserve_explicit_non_doubleheader_requires_a_boolean(self):
        for value in (None, 1, "true"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                parse_schedule(FIXTURE, preserve_explicit_non_doubleheader=value)

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
                        "away": {"team": {"id": 1, "name": "Away"}},
                        "home": {"team": {"id": 2, "name": "Home"}},
                    },
                    "venue": {"link": "/api/v1/venues/null"},
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
        self.assertIsNone(game["away_score"])
        self.assertIsNone(game["home_score"])
        self.assertIsNone(game["away_is_winner"])
        self.assertIsNone(game["home_is_winner"])

    def test_missing_venue_uses_official_neutral_site_description_name(self):
        payload = {
            "dates": [{
                "date": "2022-08-11",
                "games": [{
                    "gamePk": 663023,
                    "gameType": "R",
                    "gameDate": "2022-08-11T23:15:00Z",
                    "status": {"detailedState": "Scheduled"},
                    "teams": {
                        "away": {"team": {"id": 112, "name": "Chicago Cubs"}},
                        "home": {"team": {"id": 113, "name": "Cincinnati Reds"}},
                    },
                    "venue": {"link": "/api/v1/venues/null"},
                    "description": "MLB at Field of Dreams in Dyersville, IA",
                }],
            }]
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            game = parse_schedule(path)[0]

        self.assertIsNone(game["venue_id"])
        self.assertEqual(game["venue_name"], "Field of Dreams")

    def test_final_outcome_requires_scores_and_exactly_one_consistent_winner(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        game = payload["dates"][0]["games"][0]
        invalid = []
        missing_score = json.loads(json.dumps(payload))
        del missing_score["dates"][0]["games"][0]["teams"]["away"]["score"]
        invalid.append(missing_score)
        tied = json.loads(json.dumps(payload))
        tied_game = tied["dates"][0]["games"][0]
        tied_game["teams"]["away"]["score"] = tied_game["teams"]["home"]["score"]
        invalid.append(tied)
        both_winners = json.loads(json.dumps(payload))
        both_winners["dates"][0]["games"][0]["teams"]["away"]["isWinner"] = True
        invalid.append(both_winners)
        wrong_winner = json.loads(json.dumps(payload))
        wrong_game = wrong_winner["dates"][0]["games"][0]
        wrong_game["teams"]["away"]["score"] = 9
        invalid.append(wrong_winner)
        bool_score = json.loads(json.dumps(payload))
        bool_score["dates"][0]["games"][0]["teams"]["away"]["score"] = True
        invalid.append(bool_score)
        self.assertEqual(game["teams"]["home"]["score"], 4)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        parse_schedule(path)

    def test_explicit_tie_and_suspension_do_not_invent_a_winner(self):
        tied = json.loads(FIXTURE.read_text(encoding="utf-8"))
        tied_game = tied["dates"][0]["games"][0]
        tied_game["status"]["detailedState"] = "Final: Tied"
        tied_game["teams"]["away"]["score"] = 4
        tied_game["teams"]["away"]["isWinner"] = False
        tied_game["teams"]["home"]["isWinner"] = False
        suspended = json.loads(json.dumps(tied))
        suspended_game = suspended["dates"][0]["games"][0]
        suspended_game["status"]["detailedState"] = "Suspended"
        for side in ("away", "home"):
            suspended_game["teams"][side].pop("score")
            suspended_game["teams"][side].pop("isWinner")
        inconsistent_tie = json.loads(json.dumps(tied))
        inconsistent_tie["dates"][0]["games"][0]["teams"]["away"]["score"] = 5
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(tied), encoding="utf-8")
            game = parse_schedule(path)[0]
            self.assertEqual((game["away_score"], game["home_score"]), (4, 4))
            self.assertFalse(game["away_is_winner"])
            self.assertFalse(game["home_is_winner"])
            path.write_text(json.dumps(suspended), encoding="utf-8")
            suspended_game = parse_schedule(path)[0]
            self.assertIsNone(suspended_game["away_score"])
            self.assertIsNone(suspended_game["home_score"])
            path.write_text(json.dumps(inconsistent_tie), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "tie"):
                parse_schedule(path)

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

    def test_official_date_requires_canonical_extended_iso_format(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["dates"][0]["games"][0]["officialDate"] = "20250501"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "official date"):
                parse_schedule(path)

    def test_completed_game_uses_actual_linescore_innings(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["dates"][0]["games"][0]["linescore"] = {"currentInning": 12}
        payload["dates"][0]["games"][0]["scheduledInnings"] = 7
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            game = parse_schedule(path)[0]
        self.assertEqual(game["completed_innings"], 12)
        self.assertEqual(game["scheduled_innings"], 7)

    def test_completed_game_preserves_final_linescore_outs(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["dates"][0]["games"][0]["linescore"] = {
            "currentInning": 10,
            "outs": 1,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            game = parse_schedule(path)[0]
        self.assertEqual(game["final_outs"], 1)

        payload["dates"][0]["games"][0]["linescore"]["outs"] = 4
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "linescore.outs"):
                parse_schedule(path)

    def test_completed_game_preserves_and_validates_final_half_inning(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        linescore = payload["dates"][0]["games"][0]["linescore"] = {
            "currentInning": 5,
            "inningHalf": "Bottom",
            "isTopInning": False,
            "outs": 0,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(parse_schedule(path)[0]["final_inning_topbot"], "Bot")

            linescore["isTopInning"] = True
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "linescore.*half-inning"):
                parse_schedule(path)

    def test_rescheduled_duplicate_game_pk_prefers_played_record_but_rejects_conflicts(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        final = payload["dates"][0]["games"][0]
        postponed = json.loads(json.dumps(final))
        postponed["status"]["detailedState"] = "Postponed"
        for side in ("away", "home"):
            postponed["teams"][side].pop("score")
            postponed["teams"][side].pop("isWinner")
        payload["dates"][0]["games"].insert(0, postponed)
        conflicting = json.loads(json.dumps(payload))
        conflicting_final = json.loads(json.dumps(final))
        conflicting_final["teams"]["away"]["score"] = 9
        conflicting_final["teams"]["away"]["isWinner"] = True
        conflicting_final["teams"]["home"]["isWinner"] = False
        conflicting["dates"][0]["games"].append(conflicting_final)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            games = parse_schedule(path)
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["status"], "Final")
            path.write_text(json.dumps(conflicting), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
                parse_schedule(path)

    def test_selected_occurrences_retain_first_raw_for_equal_normalized_duplicates(self):
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        first = payload["dates"][0]["games"][0]
        first["calendarEventID"] = "first-raw-object"
        second = json.loads(json.dumps(first))
        second["calendarEventID"] = "second-raw-object"
        payload["dates"][0]["games"].append(second)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            selected = select_schedule_occurrences(path)

        self.assertEqual(len(selected), 1)
        normalized, (date_index, game_index, _, raw_game) = selected[0]
        self.assertEqual((date_index, game_index), (0, 0))
        self.assertEqual(raw_game["calendarEventID"], "first-raw-object")
        self.assertEqual(parse_schedule(FIXTURE)[0], normalized)

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
