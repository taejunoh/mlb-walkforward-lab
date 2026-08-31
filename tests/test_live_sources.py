import hashlib
import json
import multiprocessing
import os
import queue
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from mlb_backtest import live_history, live_sources, secure_fs
from mlb_backtest.secure_fs import open_trusted_root
from mlb_backtest.live_sources import (
    ACCEPTED_PREGAME_STATUSES,
    FinalGameScheduleDisposition,
    LiveHttpObservation,
    LiveSlateObservation,
    compare_probable_pitchers,
    fetch_live_http,
    live_schedule_url,
    observe_final_game_schedule,
    observe_live_slate,
    parse_live_schedule,
    parse_pregame_boxscore,
)
from mlb_backtest.live_top3_contracts import (
    FinalGameObservation,
    SourceBinding,
    canonical_json_bytes,
)
from mlb_backtest.sources import schedule_url


FIXTURES = Path(__file__).parent / "fixtures" / "live_top3"
SCHEDULE_COMPLETE = FIXTURES / "schedule_complete.json"
SCHEDULE_PARTIAL = FIXTURES / "schedule_partial.json"
BOX_COMPLETE = FIXTURES / "boxscore_complete.json"
BOX_PARTIAL = FIXTURES / "boxscore_partial.json"
BOX_EMPTY_PITCHERS = FIXTURES / "boxscore_pregame_empty_pitchers.json"
BOX_POSTSTART = FIXTURES / "boxscore_poststart_appearance_order.json"
POSTED_LINEUP_SCHEDULE = FIXTURES.parent / "schedule_2025_05_01.json"


class _Response:
    def __init__(
        self,
        body=b"{}",
        *,
        status=200,
        final_url="https://statsapi.mlb.com/api/v1/schedule?sportId=1",
        headers=None,
        chunk_type=bytes,
    ):
        self.body = body
        self.status = status
        self.final_url = final_url
        self.headers = dict(headers or {})
        self.offset = 0
        self.chunk_type = chunk_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self):
        return self.final_url

    def read(self, size=-1):
        if self.offset >= len(self.body):
            return self.chunk_type()
        if size < 0:
            size = len(self.body) - self.offset
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return self.chunk_type(chunk)


class _SequenceOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request.full_url, timeout))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


def _clock(*values):
    instants = iter(values)
    return lambda: next(instants)


def _http(
    url,
    body,
    completed="2026-07-21T15:00:01Z",
    started="2026-07-21T15:00:00Z",
):
    return LiveHttpObservation(
        requested_url=url,
        final_url=url,
        started_at_utc=started,
        response_completed_at_utc=completed,
        http_status=200,
        content_type="application/json",
        etag='"fixture"',
        last_modified=None,
        body=body,
    )


def _posted_lineup_pregame_schedule():
    payload = json.loads(POSTED_LINEUP_SCHEDULE.read_text())
    game = payload["dates"][0]["games"][0]
    game["status"] = {"detailedState": "Scheduled"}
    game["teams"]["away"]["team"] = {"id": 110, "name": "Away Club"}
    game["teams"]["home"]["team"] = {"id": 120, "name": "Home Club"}
    game["teams"]["away"]["probablePitcher"] = {
        "id": 501,
        "fullName": "Away Pitcher",
    }
    game["teams"]["home"]["probablePitcher"] = {
        "id": 601,
        "fullName": "Home Pitcher",
    }
    payload["dates"][0]["games"] = [game]
    return json.dumps(payload).encode()


def _fd_count():
    directory = Path("/dev/fd")
    return len(tuple(directory.iterdir())) if directory.is_dir() else None


def _persist_direct(
    root,
    *,
    body=None,
    observation_id="schedule-direct",
    durable_clock=None,
):
    target_date = "2026-07-21"
    request = schedule_url(target_date, target_date)
    query = {
        "observation_id": observation_id,
        "target_date": target_date,
        "url": request,
    }
    return live_sources._persist_observation(
        Path(root),
        raw_source="mlb_live_schedule",
        source="stats_api",
        binding_role="LIVE_SCHEDULE",
        game_pks=(),
        observation_id=observation_id,
        query=query,
        http=_http(request, SCHEDULE_COMPLETE.read_bytes() if body is None else body),
        durable_clock=durable_clock
        or (lambda: datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc)),
    )


def _persist_final_direct(root, *, observation_id="schedule-direct"):
    target_date = "2026-07-21"
    request = schedule_url(target_date, target_date)
    query = {
        "game_pk": 777001,
        "observation_id": observation_id,
        "target_date": target_date,
        "url": request,
    }
    return live_sources._persist_observation(
        Path(root),
        raw_source="mlb_live_final_schedule",
        source="stats_api",
        binding_role="FINAL_SCHEDULE",
        game_pks=(777001,),
        observation_id=observation_id,
        query=query,
        http=_http(request, SCHEDULE_COMPLETE.read_bytes()),
        durable_clock=lambda: datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc),
    )


def _persist_process_worker(root, observation_id, role, barrier, results):
    try:
        barrier.wait(timeout=10)
        if role == "live":
            persisted = _persist_direct(root, observation_id=observation_id)
        else:
            persisted = _persist_final_direct(root, observation_id=observation_id)
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", persisted.source_binding.binding_role, ""))


def _run_persist_process_pair(root, observation_id, roles):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    processes = [
        context.Process(
            target=_persist_process_worker,
            args=(root, observation_id, role, barrier, results),
        )
        for role in roles
    ]
    try:
        for process in processes:
            process.start()
        barrier.wait(timeout=10)
        outputs = tuple(results.get(timeout=20) for _process in processes)
        for process in processes:
            process.join(timeout=20)
        if any(process.is_alive() for process in processes):
            raise AssertionError("live observation worker did not exit")
        if any(process.exitcode != 0 for process in processes):
            raise AssertionError("live observation worker exited unexpectedly")
        return outputs
    except queue.Empty as exc:
        raise AssertionError("live observation worker returned no result") from exc
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


class LiveSourceTests(unittest.TestCase):
    def test_http_observation_binds_status_final_url_and_selected_headers(self):
        started = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        completed = datetime(2026, 7, 21, 15, 0, 1, tzinfo=timezone.utc)
        url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        response = _Response(
            body=b"{}",
            final_url=url,
            headers={
                "Content-Type": "application/json",
                "Content-Length": "2",
                "ETag": '"abc"',
                "Last-Modified": "Tue, 21 Jul 2026 14:59:59 GMT",
            },
        )
        observed = fetch_live_http(
            "stats_api",
            url,
            opener=lambda *_args, **_kwargs: response,
            clock=_clock(started, completed),
        )
        self.assertEqual(observed.http_status, 200)
        self.assertEqual(observed.etag, '"abc"')
        self.assertEqual(observed.body, b"{}")
        self.assertEqual(observed.started_at_utc, "2026-07-21T15:00:00Z")
        self.assertEqual(observed.response_completed_at_utc, "2026-07-21T15:00:01Z")

    def test_http_fetch_is_bounded_and_source_host_is_exact(self):
        url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        now = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        cases = (
            _Response(status=500, final_url=url),
            _Response(final_url="https://evil.test/schedule"),
            _Response(final_url="https://statsapi.mlb.com:443/schedule"),
            _Response(body=b"12345", final_url=url),
            _Response(body=b"x", final_url=url, headers={"Content-Length": "2"}),
            _Response(body=b"x", final_url=url, chunk_type=bytearray),
        )
        limits = (64, 64, 64, 4, 64, 64)
        for response, limit in zip(cases, limits, strict=True):
            with self.subTest(response=response), self.assertRaises(ValueError):
                fetch_live_http(
                    "stats_api",
                    url,
                    max_bytes=limit,
                    opener=lambda *_args, _response=response, **_kwargs: _response,
                    clock=lambda: now,
                )

    def test_http_fetch_rejects_unsafe_initial_url_before_transport(self):
        now = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        unsafe = (
            "http://statsapi.mlb.com/api/v1/schedule",
            "https://127.0.0.1/internal",
            "https://user@statsapi.mlb.com/api/v1/schedule",
            "https://statsapi.mlb.com:443/api/v1/schedule",
        )
        for url in unsafe:
            with self.subTest(url=url):
                opener = _SequenceOpener()
                with self.assertRaises(ValueError):
                    fetch_live_http(
                        "stats_api", url, opener=opener, clock=lambda: now
                    )
                self.assertEqual(opener.calls, [])

    def test_http_fetch_rejects_unsafe_redirect_before_second_request(self):
        now = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        requested = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        unsafe = (
            "http://statsapi.mlb.com/api/v1/internal",
            "https://127.0.0.1/internal",
            "https://user@statsapi.mlb.com/api/v1/internal",
            "https://statsapi.mlb.com:443/api/v1/internal",
        )
        for location in unsafe:
            with self.subTest(location=location):
                opener = _SequenceOpener(
                    _Response(
                        status=302,
                        final_url=requested,
                        headers={"Location": location},
                    )
                )
                with self.assertRaises(ValueError):
                    fetch_live_http(
                        "stats_api", requested, opener=opener, clock=lambda: now
                    )
                self.assertEqual(opener.calls, [(requested, 90)])

    def test_http_fetch_validates_each_redirect_hop_before_follow(self):
        now = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        requested = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        allowed = "https://statsapi.mlb.com/api/v1/next"
        opener = _SequenceOpener(
            _Response(
                status=302,
                final_url=requested,
                headers={"Location": "/api/v1/next"},
            ),
            _Response(
                status=307,
                final_url=allowed,
                headers={"Location": "//169.254.169.254/latest/meta-data"},
            ),
        )

        with self.assertRaises(ValueError):
            fetch_live_http(
                "stats_api", requested, opener=opener, clock=lambda: now
            )

        self.assertEqual(opener.calls, [(requested, 90), (allowed, 90)])

    def test_http_fetch_follows_allowed_relative_same_host_redirect(self):
        started = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        completed = datetime(2026, 7, 21, 15, 0, 1, tzinfo=timezone.utc)
        requested = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        redirected = "https://statsapi.mlb.com/api/v1/next?game=1"
        opener = _SequenceOpener(
            _Response(
                status=302,
                final_url=requested,
                headers={"Location": "/api/v1/next?game=1"},
            ),
            _Response(
                body=b"{}",
                final_url=redirected,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": "2",
                },
            ),
        )

        observed = fetch_live_http(
            "stats_api",
            requested,
            opener=opener,
            clock=_clock(started, completed),
        )

        self.assertEqual(observed.final_url, redirected)
        self.assertEqual(observed.body, b"{}")
        self.assertEqual(opener.calls, [(requested, 90), (redirected, 90)])

    def test_default_http_opener_installs_no_follow_redirect_handler(self):
        started = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        completed = datetime(2026, 7, 21, 15, 0, 1, tzinfo=timezone.utc)
        requested = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        redirected = "https://statsapi.mlb.com/api/v1/next"
        transport = mock.Mock()
        transport.open.side_effect = (
            _Response(
                status=302,
                final_url=requested,
                headers={"Location": "/api/v1/next"},
            ),
            _Response(body=b"{}", final_url=redirected),
        )

        with patch.object(
            live_sources, "build_opener", return_value=transport
        ) as builder:
            observed = fetch_live_http(
                "stats_api",
                requested,
                clock=_clock(started, completed),
            )

        self.assertEqual(observed.final_url, redirected)
        self.assertEqual(builder.call_count, 2)
        self.assertTrue(
            all(
                isinstance(call.args[0], live_sources._NoFollowRedirectHandler)
                for call in builder.call_args_list
            )
        )
        self.assertEqual(
            [call.args[0].full_url for call in transport.open.call_args_list],
            [requested, redirected],
        )

    def test_http_fetch_rejects_transport_that_already_followed_redirect(self):
        now = datetime(2026, 7, 21, 15, tzinfo=timezone.utc)
        requested = "https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        followed = "https://statsapi.mlb.com/api/v1/already-followed"
        opener = _SequenceOpener(_Response(final_url=followed))

        with self.assertRaisesRegex(ValueError, "redirect"):
            fetch_live_http(
                "stats_api", requested, opener=opener, clock=lambda: now
            )
        self.assertEqual(opener.calls, [(requested, 90)])

    def test_schedule_projection_is_result_free_and_validates_coverage(self):
        games = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["game_pk"], 777001)
        self.assertEqual(games[0]["scheduled_start_utc"], "2026-07-21T23:05:00Z")
        forbidden = {
            "away_score",
            "home_score",
            "away_is_winner",
            "home_is_winner",
            "linescore",
            "plays",
        }
        self.assertFalse(forbidden & set(games[0]))
        self.assertEqual(
            ACCEPTED_PREGAME_STATUSES, frozenset({"Scheduled", "Pre-Game"})
        )

        wrong_date = SCHEDULE_COMPLETE.read_text().replace("2026-07-21", "2026-07-22")
        with self.assertRaises(ValueError):
            parse_live_schedule(wrong_date.encode(), requested_date="2026-07-21")

    def test_live_schedule_url_requests_only_probables_and_venue_hydration(self):
        url = live_schedule_url("2026-07-21", "2026-07-21")

        self.assertIn("hydrate=probablePitcher%2Cvenue", url)
        self.assertNotIn("lineup", url.lower())
        self.assertNotIn("linescore", url.lower())

    def test_real_posted_schedule_lineups_are_forced_out_of_projection(self):
        raw = _posted_lineup_pregame_schedule()
        provider_game = json.loads(raw)["dates"][0]["games"][0]
        self.assertEqual(len(provider_game["lineups"]["awayPlayers"]), 9)
        self.assertEqual(len(provider_game["lineups"]["homePlayers"]), 9)

        projected = parse_live_schedule(raw, requested_date="2025-05-01")

        self.assertEqual(projected[0]["away_lineup"], ())
        self.assertEqual(projected[0]["home_lineup"], ())
        self.assertEqual(projected[0]["away_probable_pitcher_id"], 501)
        self.assertEqual(projected[0]["home_probable_pitcher_id"], 601)
        self.assertNotIn("away_score", projected[0])
        self.assertNotIn("home_score", projected[0])

    def test_posted_schedule_lineups_never_reach_slate_final_or_service(self):
        from tests.test_live_top3_service import _build

        target_date = "2025-05-01"
        schedule = _posted_lineup_pregame_schedule()
        boxscore = BOX_COMPLETE.read_bytes()
        ids = iter(("posted-schedule", "posted-boxscore", "posted-final"))

        def fetcher(_source, url, *, max_bytes):
            body = boxscore if "/boxscore" in url else schedule
            return _http(
                url,
                body,
                started="2025-05-01T12:00:00Z",
                completed="2025-05-01T12:00:01Z",
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            slate = observe_live_slate(
                root,
                target_date,
                fetcher=fetcher,
                observation_id_factory=lambda: next(ids),
                durable_clock=lambda: datetime(
                    2025, 5, 1, 12, 0, 2, tzinfo=timezone.utc
                ),
            )
            final = observe_final_game_schedule(
                root,
                target_date,
                slate.games[0],
                fetcher=fetcher,
                observation_id_factory=lambda: next(ids),
                durable_clock=lambda: datetime(
                    2025, 5, 1, 12, 0, 3, tzinfo=timezone.utc
                ),
            )

        self.assertEqual(slate.games[0]["away_lineup"], ())
        self.assertEqual(slate.games[0]["home_lineup"], ())
        participant = slate.participants[778100]
        self.assertEqual(participant.away_lineup, tuple(range(1001, 1010)))
        self.assertEqual(participant.home_lineup, tuple(range(2001, 2010)))
        self.assertIsInstance(final, FinalGameObservation)
        self.assertEqual(final.projected_game["away_lineup"], ())
        self.assertEqual(final.projected_game["home_lineup"], ())

        result = _build(slate, lambda _game_pk, _target_date: final)
        self.assertEqual(
            [row["game_pk"] for row in result["unscored_games"]],
            [778100],
        )
        self.assertNotIn(
            "TARGET_PARTICIPANT_LEAKAGE",
            result["unscored_games"][0]["reason_codes"],
        )
        self.assertEqual(result["excluded_games"], [])

    def test_schedule_rejects_nonregular_and_duplicate_game_ids(self):
        payload = json.loads(SCHEDULE_COMPLETE.read_text())
        payload["dates"][0]["games"][0]["gameType"] = "P"
        with self.assertRaisesRegex(ValueError, "regular"):
            parse_live_schedule(
                json.dumps(payload).encode(), requested_date="2026-07-21"
            )

        payload = json.loads(SCHEDULE_COMPLETE.read_text())
        payload["dates"][0]["games"].append(payload["dates"][0]["games"][0])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_live_schedule(
                json.dumps(payload).encode(), requested_date="2026-07-21"
            )

    def test_complete_boxscore_projects_pitcher_evidence_and_ordered_nines(self):
        participants = parse_pregame_boxscore(BOX_COMPLETE.read_bytes(), game_pk=777001)
        self.assertEqual(participants.away_pitcher_list_id, 501)
        self.assertEqual(participants.home_pitcher_list_id, 601)
        self.assertEqual(participants.away_lineup, tuple(range(1001, 1010)))
        self.assertEqual(participants.home_lineup, tuple(range(2001, 2010)))
        self.assertTrue(participants.complete)

    def test_partial_empty_and_poststart_boxscores_never_invent_evidence(self):
        partial = parse_pregame_boxscore(BOX_PARTIAL.read_bytes(), game_pk=777001)
        self.assertFalse(partial.complete)
        self.assertEqual(partial.home_lineup, ())
        self.assertIn("MISSING_HOME_LINEUP", partial.reason_codes)

        empty = parse_pregame_boxscore(BOX_EMPTY_PITCHERS.read_bytes(), game_pk=777001)
        self.assertIsNone(empty.away_pitcher_list_id)
        self.assertIn("MISSING_AWAY_PITCHER_LIST", empty.reason_codes)

        poststart = parse_pregame_boxscore(BOX_POSTSTART.read_bytes(), game_pk=777001)
        self.assertEqual(poststart.away_lineup, ())
        self.assertIn("POSTSTART_APPEARANCE_ORDER", poststart.reason_codes)

    def test_schedule_probables_must_agree_with_pitcher_list_evidence(self):
        game = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        participants = parse_pregame_boxscore(BOX_COMPLETE.read_bytes(), game_pk=777001)
        consistent = compare_probable_pitchers(game, participants)
        self.assertTrue(consistent.complete)
        self.assertIn("PROBABLE_CONSISTENT", consistent.reason_codes)

        conflict = compare_probable_pitchers(
            {**game, "home_probable_pitcher_id": 999}, participants
        )
        self.assertFalse(conflict.complete)
        self.assertIn("STARTER_CONFLICT", conflict.reason_codes)

    def test_missing_pitcher_list_is_missing_evidence_not_starter_conflict(self):
        game = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        participants = parse_pregame_boxscore(
            BOX_EMPTY_PITCHERS.read_bytes(), game_pk=777001
        )

        observed = compare_probable_pitchers(game, participants)

        self.assertFalse(observed.complete)
        self.assertIn("MISSING_AWAY_PITCHER_LIST", observed.reason_codes)
        self.assertIn("MISSING_HOME_PITCHER_LIST", observed.reason_codes)
        self.assertNotIn("STARTER_CONFLICT", observed.reason_codes)

    def test_missing_schedule_probable_is_retained_without_consistency(self):
        game = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        participants = parse_pregame_boxscore(BOX_COMPLETE.read_bytes(), game_pk=777001)
        participants = compare_probable_pitchers(game, participants)

        observed = compare_probable_pitchers(
            {**game, "away_probable_pitcher_id": None}, participants
        )

        self.assertFalse(observed.complete)
        self.assertIn("MISSING_SCHEDULE_PROBABLE", observed.reason_codes)
        self.assertNotIn("PROBABLE_CONSISTENT", observed.reason_codes)

    def test_missing_one_side_and_known_opposite_mismatch_retain_both_reasons(self):
        game = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        boxscore = json.loads(BOX_COMPLETE.read_bytes())
        boxscore["teams"]["away"]["pitchers"] = []
        participants = parse_pregame_boxscore(
            json.dumps(boxscore).encode(), game_pk=777001
        )

        observed = compare_probable_pitchers(
            {**game, "home_probable_pitcher_id": 999}, participants
        )

        self.assertFalse(observed.complete)
        self.assertIn("MISSING_AWAY_PITCHER_LIST", observed.reason_codes)
        self.assertIn("STARTER_CONFLICT", observed.reason_codes)

    def test_live_slate_writes_append_only_raw_manifests_receipts_and_bindings(self):
        schedule = SCHEDULE_COMPLETE.read_bytes()
        boxscore = BOX_COMPLETE.read_bytes()
        ids = iter(("schedule-a", "box-a", "schedule-b", "box-b"))
        fetch_count = 0

        def fetcher(_source, url, *, max_bytes):
            nonlocal fetch_count
            self.assertGreater(max_bytes, 0)
            body = boxscore if "/boxscore" in url else schedule
            if fetch_count >= 2:
                body += b" "
            fetch_count += 1
            return _http(url, body)

        durable_times = iter(
            datetime(2026, 7, 21, 15, 0, second, tzinfo=timezone.utc)
            for second in range(2, 6)
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = observe_live_slate(
                root,
                "2026-07-21",
                fetcher=fetcher,
                observation_id_factory=lambda: next(ids),
                durable_clock=lambda: next(durable_times),
            )
            second = observe_live_slate(
                root,
                "2026-07-21",
                fetcher=fetcher,
                observation_id_factory=lambda: next(ids),
                durable_clock=lambda: next(durable_times),
            )

            self.assertEqual(len(first.source_bindings), 2)
            self.assertTrue(
                all(isinstance(item, SourceBinding) for item in first.source_bindings)
            )
            self.assertNotEqual(
                first.source_bindings[0].observation_id,
                second.source_bindings[0].observation_id,
            )
            self.assertNotEqual(
                first.source_bindings[0].payload_sha256,
                second.source_bindings[0].payload_sha256,
            )
            self.assertEqual(
                len(
                    tuple(
                        path
                        for path in root.glob(
                            "audit/experimental_daily/live_receipts/*.json"
                        )
                        if not path.name.endswith(".completion.json")
                    )
                ),
                4,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "data/experimental_daily/live_observations/*/*.manifest.json"
                        )
                    )
                ),
                4,
            )
            receipt = json.loads(
                (root / first.source_bindings[0].receipt_relative).read_text()
            )
            self.assertEqual(
                receipt["payload_sha256"], first.source_bindings[0].payload_sha256
            )
            self.assertEqual(
                json.loads(first.source_bindings[0].query_identity_json)[
                    "observation_id"
                ],
                "schedule-a",
            )

    def test_current_fetch_failure_never_falls_back_to_stale_observation(self):
        calls = 0

        def fetcher(_source, _url, *, max_bytes):
            nonlocal calls
            calls += 1
            raise RuntimeError("current source unavailable")

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, "current source unavailable"):
                observe_live_slate(Path(temporary), "2026-07-21", fetcher=fetcher)
        self.assertEqual(calls, 1)

    def test_final_schedule_refetch_fails_closed_on_start_revision(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        revised_body = (
            SCHEDULE_COMPLETE.read_text().replace("23:05:00Z", "23:35:00Z").encode()
        )
        observation_ids = iter(("final-revised", "final-stable"))
        durable_times = iter(
            (
                datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc),
                datetime(2026, 7, 21, 15, 0, 3, tzinfo=timezone.utc),
            )
        )

        def fetch_revised(_source, url, *, max_bytes):
            return _http(url, revised_body)

        def fetch_stable(_source, url, *, max_bytes):
            return _http(url, SCHEDULE_COMPLETE.read_bytes())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            revised = observe_final_game_schedule(
                root,
                "2026-07-21",
                saved,
                fetcher=fetch_revised,
                observation_id_factory=lambda: next(observation_ids),
                durable_clock=lambda: next(durable_times),
            )
            self.assertIsInstance(revised, FinalGameScheduleDisposition)
            self.assertIn("SCHEDULED_START_CHANGED", revised.reason_codes)

            stable = observe_final_game_schedule(
                root,
                "2026-07-21",
                saved,
                fetcher=fetch_stable,
                observation_id_factory=lambda: next(observation_ids),
                durable_clock=lambda: next(durable_times),
            )
            self.assertIsInstance(stable, FinalGameObservation)
            self.assertEqual(stable.game_pk, 777001)
            self.assertEqual(stable.source_binding.binding_role, "FINAL_SCHEDULE")

    def test_final_schedule_refetch_fails_closed_on_probable_pitcher_name_revision(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        revised_body = SCHEDULE_COMPLETE.read_bytes().replace(
            b"Away Pitcher", b"Replacement Pitcher"
        )

        with tempfile.TemporaryDirectory() as temporary:
            result = observe_final_game_schedule(
                Path(temporary),
                "2026-07-21",
                saved,
                fetcher=lambda _source, url, *, max_bytes: _http(url, revised_body),
                observation_id_factory=lambda: "final-probable-name-revised",
                durable_clock=lambda: datetime(
                    2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                ),
            )
        self.assertIsInstance(result, FinalGameScheduleDisposition)
        self.assertEqual(result.reason_codes, ("PROBABLE_PITCHER_CHANGED",))

    def test_expected_root_binding_rejects_replaced_root_before_fetch(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            replacement = base / "replacement"
            root.mkdir()
            replacement.mkdir()
            with open_trusted_root(root) as trusted:
                expected = trusted.binding
            root.rename(base / "moved-root")
            replacement.rename(root)

            with self.assertRaisesRegex(ValueError, "binding"):
                observe_live_slate(
                    root,
                    "2026-07-21",
                    expected_root_binding=expected,
                    fetcher=lambda *_args, **_kwargs: self.fail(
                        "replaced root must fail before fetch"
                    ),
                )
            self.assertEqual(tuple(root.iterdir()), ())

    def test_live_observers_reject_root_swap_during_fetch_before_outside_write(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        for observer_name in ("slate", "final"):
            with self.subTest(observer=observer_name), tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
                base = Path(temporary)
                root = base / "root"
                moved = base / "moved-root"
                outside = base / "outside"
                root.mkdir()
                outside.mkdir()
                with open_trusted_root(root) as trusted:
                    expected = trusted.binding
                swapped = False

                def fetcher(_source, url, *, max_bytes):
                    nonlocal swapped
                    if not swapped:
                        swapped = True
                        root.rename(moved)
                        root.symlink_to(outside, target_is_directory=True)
                    return _http(url, SCHEDULE_COMPLETE.read_bytes())

                with self.assertRaises(ValueError):
                    if observer_name == "slate":
                        observe_live_slate(
                            root,
                            "2026-07-21",
                            expected_root_binding=expected,
                            fetcher=fetcher,
                        )
                    else:
                        observe_final_game_schedule(
                            root,
                            "2026-07-21",
                            saved,
                            expected_root_binding=expected,
                            fetcher=fetcher,
                        )
                self.assertTrue(swapped)
                self.assertEqual(tuple(outside.iterdir()), ())

    def test_live_observers_reject_root_swap_during_root_open_before_fetch(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        for observer_name in ("slate", "final"):
            with self.subTest(observer=observer_name), tempfile.TemporaryDirectory(
                dir=Path.cwd()
            ) as temporary:
                base = Path(temporary)
                root = base / "root"
                moved = base / "moved-root"
                outside = base / "outside"
                root.mkdir()
                outside.mkdir()
                swapped = False

                def swap(stage, observed_root):
                    nonlocal swapped
                    if stage == "after_existing_root_lstat" and not swapped:
                        swapped = True
                        observed_root.rename(moved)
                        observed_root.symlink_to(outside, target_is_directory=True)

                with patch.object(
                    live_sources, "_live_root_test_hook", side_effect=swap
                ):
                    with self.assertRaises(ValueError):
                        if observer_name == "slate":
                            observe_live_slate(
                                root,
                                "2026-07-21",
                                fetcher=lambda *_args, **_kwargs: self.fail(
                                    "swapped root must fail before fetch"
                                ),
                            )
                        else:
                            observe_final_game_schedule(
                                root,
                                "2026-07-21",
                                saved,
                                fetcher=lambda *_args, **_kwargs: self.fail(
                                    "swapped root must fail before fetch"
                                ),
                            )
                self.assertTrue(swapped)
                self.assertEqual(tuple(outside.iterdir()), ())

    def test_final_observer_rejects_root_swap_during_final_parse(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        original_parse = live_sources.parse_live_schedule
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            moved = base / "moved-root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            swapped = False

            def swap_then_parse(raw, *, requested_date):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    root.rename(moved)
                    root.symlink_to(outside, target_is_directory=True)
                return original_parse(raw, requested_date=requested_date)

            with patch.object(
                live_sources, "parse_live_schedule", side_effect=swap_then_parse
            ):
                with self.assertRaises(ValueError):
                    observe_final_game_schedule(
                        root,
                        "2026-07-21",
                        saved,
                        fetcher=lambda _source, url, *, max_bytes: _http(
                            url, SCHEDULE_COMPLETE.read_bytes()
                        ),
                        observation_id_factory=lambda: "final-parse-root-swap",
                        durable_clock=lambda: datetime(
                            2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                        ),
                    )
            self.assertTrue(swapped)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_root_revalidation_preserves_original_parse_exception(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            moved = base / "moved-root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()

            def swap_then_fail(*_args, **_kwargs):
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
                raise RuntimeError("final parse sentinel")

            with patch.object(
                live_sources, "parse_live_schedule", side_effect=swap_then_fail
            ):
                with self.assertRaisesRegex(RuntimeError, "final parse sentinel"):
                    observe_final_game_schedule(
                        root,
                        "2026-07-21",
                        saved,
                        fetcher=lambda _source, url, *, max_bytes: _http(
                            url, SCHEDULE_COMPLETE.read_bytes()
                        ),
                        observation_id_factory=lambda: "final-parse-error-swap",
                        durable_clock=lambda: datetime(
                            2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                        ),
                    )
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_slate_observer_revalidates_root_after_result_construction(self):
        original_slate = live_sources.LiveSlateObservation
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            moved = base / "moved-root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            swapped = False

            def swap_then_construct(*args, **kwargs):
                nonlocal swapped
                swapped = True
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
                return original_slate(*args, **kwargs)

            with patch.object(
                live_sources,
                "LiveSlateObservation",
                side_effect=swap_then_construct,
            ):
                with self.assertRaises(ValueError):
                    observe_live_slate(
                        root,
                        "2026-07-21",
                        fetcher=lambda _source, url, *, max_bytes: _http(
                            url,
                            BOX_COMPLETE.read_bytes()
                            if "/boxscore" in url
                            else SCHEDULE_COMPLETE.read_bytes(),
                        ),
                        observation_id_factory=iter(
                            ("slate-return-swap", "box-return-swap")
                        ).__next__,
                        durable_clock=lambda: datetime(
                            2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                        ),
                    )
            self.assertTrue(swapped)
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_normalize_live_root_path_maps_only_top_level_platform_symlink(self):
        symlink_stat = type("Stat", (), {"st_mode": stat.S_IFLNK})()
        directory_stat = type("Stat", (), {"st_mode": stat.S_IFDIR})()

        def fake_lstat(path):
            return symlink_stat if Path(path) == Path("/var") else directory_stat

        with (
            patch.object(live_sources.os, "lstat", side_effect=fake_lstat),
            patch.object(live_sources.os, "readlink", return_value="/private/var"),
        ):
            normalized = live_sources.normalize_live_root_path(
                Path("/var/folders/live-root")
            )
        self.assertEqual(normalized, Path("/private/var/folders/live-root"))

    def test_normalize_live_root_path_rejects_arbitrary_top_level_symlink(self):
        symlink_stat = type("Stat", (), {"st_mode": stat.S_IFLNK})()
        directory_stat = type("Stat", (), {"st_mode": stat.S_IFDIR})()

        def fake_lstat(path):
            return symlink_stat if Path(path) == Path("/redirect") else directory_stat

        with (
            patch.object(live_sources.os, "lstat", side_effect=fake_lstat),
            patch.object(live_sources.os, "readlink", return_value="/srv/data"),
        ):
            with self.assertRaisesRegex(ValueError, "ancestry"):
                live_sources.normalize_live_root_path(
                    Path("/redirect/folders/live-root")
                )

    def test_normalize_live_root_path_leaves_nested_symlink_for_strict_rejection(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            nested = base / "nested"
            nested.symlink_to(outside, target_is_directory=True)
            requested = nested / "live-root"

            normalized = live_sources.normalize_live_root_path(requested)

            self.assertEqual(normalized, requested.absolute())
            with self.assertRaises(ValueError):
                open_trusted_root(normalized)

    def test_invalid_final_schedule_is_preserved_and_returns_explicit_disposition(self):
        saved = parse_live_schedule(
            SCHEDULE_COMPLETE.read_bytes(), requested_date="2026-07-21"
        )[0]
        wrong_date = (
            SCHEDULE_COMPLETE.read_text().replace("2026-07-21", "2026-07-22").encode()
        )

        def fetcher(_source, url, *, max_bytes):
            return _http(url, wrong_date)

        with tempfile.TemporaryDirectory() as temporary:
            result = observe_final_game_schedule(
                Path(temporary),
                "2026-07-21",
                saved,
                fetcher=fetcher,
                observation_id_factory=lambda: "final-invalid",
                durable_clock=lambda: datetime(
                    2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                ),
            )
            self.assertIsInstance(result, FinalGameScheduleDisposition)
            self.assertEqual(result.reason_codes, ("OFFICIAL_DATE_CHANGED",))
            self.assertTrue(
                (Path(temporary) / result.source_binding.receipt_relative).is_file()
            )

    def test_persisted_observation_returns_authenticated_snapshot_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = _persist_direct(temporary)

            self.assertEqual(result.payload_bytes, SCHEDULE_COMPLETE.read_bytes())
            self.assertEqual(result.payload_snapshot.contents, result.payload_bytes)
            self.assertEqual(
                hashlib.sha256(result.payload_bytes).hexdigest(),
                result.source_binding.payload_sha256,
            )

    def test_schedule_and_boxscore_receipts_exist_before_invalid_payload_parse(self):
        target_date = "2026-07-21"
        schedule_request = live_schedule_url(target_date, target_date)
        ids = iter(("invalid-schedule",))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
                observe_live_slate(
                    root,
                    target_date,
                    fetcher=lambda _source, url, *, max_bytes: _http(url, b"not json"),
                    observation_id_factory=lambda: next(ids),
                    durable_clock=lambda: datetime(
                        2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                    ),
                )
            receipt = (
                root / "audit/experimental_daily/live_receipts/invalid-schedule.json"
            )
            self.assertTrue(receipt.is_file())
            self.assertEqual(
                json.loads(receipt.read_text())["requested_url"], schedule_request
            )

        ids = iter(("valid-schedule", "invalid-boxscore"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fetcher(_source, url, *, max_bytes):
                body = (
                    b"not json"
                    if "/boxscore" in url
                    else SCHEDULE_COMPLETE.read_bytes()
                )
                return _http(url, body)

            with self.assertRaisesRegex(ValueError, "UTF-8 JSON"):
                observe_live_slate(
                    root,
                    target_date,
                    fetcher=fetcher,
                    observation_id_factory=lambda: next(ids),
                    durable_clock=lambda: datetime(
                        2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                    ),
                )
            self.assertTrue(
                (
                    root
                    / "audit/experimental_daily/live_receipts/invalid-boxscore.json"
                ).is_file()
            )

    def test_same_observation_id_recovers_exact_bytes_but_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = _persist_direct(temporary, observation_id="recoverable")
            second = _persist_direct(temporary, observation_id="recoverable")
            self.assertEqual(first.payload_bytes, second.payload_bytes)
            self.assertEqual(
                first.source_binding.receipt_sha256,
                second.source_binding.receipt_sha256,
            )
            receipt = json.loads(
                (Path(temporary) / first.source_binding.receipt_relative).read_text()
            )
            claim = Path(temporary) / receipt["claim_relative"]
            self.assertEqual(
                hashlib.sha256(claim.read_bytes()).hexdigest(),
                receipt["claim_sha256"],
            )

            manifest_count = len(
                tuple(
                    Path(temporary).glob(
                        "data/experimental_daily/live_observations/*/*.manifest.json"
                    )
                )
            )
            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                side_effect=AssertionError("conflict reached RawStore"),
            ):
                with self.assertRaisesRegex(ValueError, "claim"):
                    _persist_direct(
                        temporary,
                        observation_id="recoverable",
                        body=SCHEDULE_COMPLETE.read_bytes() + b" ",
                    )
            self.assertEqual(
                len(
                    tuple(
                        Path(temporary).glob(
                            "data/experimental_daily/live_observations/*/*.manifest.json"
                        )
                    )
                ),
                manifest_count,
            )

    def test_global_claim_precedes_raw_publication_and_rejects_cross_role_id(self):
        original_put = live_history._CapabilityRawStore.put_unique_query
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def assert_claim_first(store, source, query, payload):
                claim = (
                    root
                    / "audit/experimental_daily/live_observation_claims/shared-id.json"
                )
                self.assertTrue(claim.is_file())
                return original_put(store, source, query, payload)

            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                new=assert_claim_first,
            ):
                _persist_direct(temporary, observation_id="shared-id")

            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                side_effect=AssertionError("cross-role conflict reached RawStore"),
            ):
                with self.assertRaisesRegex(ValueError, "claim"):
                    _persist_final_direct(temporary, observation_id="shared-id")

            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "data/experimental_daily/live_observations/*/*.manifest.json"
                        )
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_receipts/shared-id.json"
                        )
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_observation_claims/shared-id.json"
                        )
                    )
                ),
                1,
            )

    def test_concurrent_same_id_calls_are_globally_serialized(self):
        original_put = live_history._CapabilityRawStore.put_unique_query
        active = 0
        maximum_active = 0
        durable_calls = 0
        state_lock = threading.Lock()

        def tracked_put(store, source, query, payload):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.03)
                return original_put(store, source, query, payload)
            finally:
                with state_lock:
                    active -= 1

        def durable_clock():
            nonlocal durable_calls
            with state_lock:
                durable_calls += 1
            return datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                new=tracked_put,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = tuple(
                        executor.map(
                            lambda _index: _persist_direct(
                                temporary,
                                observation_id="concurrent-id",
                                durable_clock=durable_clock,
                            ),
                            range(2),
                        )
                    )
            self.assertEqual(maximum_active, 1)
            self.assertEqual(durable_calls, 1)
            self.assertEqual(results[0].payload_bytes, results[1].payload_bytes)
            self.assertEqual(results[0].source_binding, results[1].source_binding)
            root = Path(temporary)
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "data/experimental_daily/live_observations/*/*.manifest.json"
                        )
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_observation_claims/concurrent-id.json"
                        )
                    )
                ),
                1,
            )

    def test_multiprocess_exact_same_id_recovers_without_lock_open_race(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(16):
                observation_id = f"multiprocess-exact-{index}"
                outputs = _run_persist_process_pair(
                    temporary, observation_id, ("live", "live")
                )
                self.assertEqual([output[0] for output in outputs], ["ok", "ok"])
                self.assertTrue(
                    (
                        root
                        / "audit/experimental_daily/live_observation_claims"
                        / f"{observation_id}.lock"
                    ).is_file()
                )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "data/experimental_daily/live_observations/*/*.manifest.json"
                        )
                    )
                ),
                16,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_receipts/*.completion.json"
                        )
                    )
                ),
                16,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_observation_claims/*.json"
                        )
                    )
                ),
                16,
            )

    def test_multiprocess_conflicting_role_same_id_rejects_before_second_raw(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(16):
                observation_id = f"multiprocess-conflict-{index}"
                outputs = _run_persist_process_pair(
                    temporary, observation_id, ("live", "final")
                )
                self.assertEqual(
                    sorted(output[0] for output in outputs), ["error", "ok"]
                )
                error = next(output for output in outputs if output[0] == "error")
                self.assertEqual(error[1], "ValueError")
                self.assertIn("claim", error[2])
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "data/experimental_daily/live_observations/*/*.manifest.json"
                        )
                    )
                ),
                16,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_receipts/*.completion.json"
                        )
                    )
                ),
                16,
            )
            self.assertEqual(
                len(
                    tuple(
                        root.glob(
                            "audit/experimental_daily/live_observation_claims/*.json"
                        )
                    )
                ),
                16,
            )

    def test_receipt_write_residue_recovers_only_when_exact(self):
        real_write = live_sources.write_exclusive
        raised = False

        def fail_after_complete_write(*args, **kwargs):
            nonlocal raised
            result = real_write(*args, **kwargs)
            if args[3] == "live observation receipt" and not raised:
                raised = True
                raise RuntimeError("injected post-write failure")
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                live_sources,
                "write_exclusive",
                side_effect=fail_after_complete_write,
            ):
                with self.assertRaisesRegex(RuntimeError, "post-write failure"):
                    _persist_direct(temporary, observation_id="exact-residue")
            recovered = _persist_direct(temporary, observation_id="exact-residue")
            self.assertEqual(recovered.payload_bytes, SCHEDULE_COMPLETE.read_bytes())

        def leave_partial(root, relative, contents, label, **kwargs):
            if label != "live observation receipt":
                return real_write(root, relative, contents, label, **kwargs)
            path = root.path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents[:11])
            raise RuntimeError("injected partial write failure")

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                live_sources, "write_exclusive", side_effect=leave_partial
            ):
                with self.assertRaisesRegex(RuntimeError, "partial write failure"):
                    _persist_direct(temporary, observation_id="partial-residue")
            residue = (
                Path(temporary)
                / "audit/experimental_daily/live_receipts/partial-residue.json"
            )
            before = residue.read_bytes()
            with self.assertRaisesRegex(ValueError, "receipt.*differs"):
                _persist_direct(temporary, observation_id="partial-residue")
            self.assertEqual(residue.read_bytes(), before)

    def test_manifest_exact_metadata_and_seal_are_authenticated(self):
        mutations = {
            "fields": lambda value: value.update({"unexpected": True}),
            "schema": lambda value: value.update({"schema_version": 2}),
            "source": lambda value: value.update({"source": "other"}),
            "query": lambda value: value.update({"query": {"wrong": True}}),
            "payload_sha": lambda value: value.update({"sha256": "0" * 64}),
            "row_count": lambda value: value.update({"row_count": 999}),
            "retrieved": lambda value: value.update(
                {"retrieved_at_utc": "2020-01-01T00:00:00+00:00"}
            ),
        }
        original_put = live_history._CapabilityRawStore.put_unique_query
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:

                def corrupt(store, source, query, payload, _mutate=mutate):
                    artifact = original_put(store, source, query, payload)
                    manifest = json.loads(artifact.manifest_path.read_text())
                    _mutate(manifest)
                    content = json.dumps(
                        manifest, ensure_ascii=False, sort_keys=True, indent=2
                    ).encode()
                    artifact.manifest_path.write_bytes(content)
                    artifact.manifest_path.with_name(
                        artifact.manifest_path.name[:-5] + ".sha256"
                    ).write_bytes(f"{hashlib.sha256(content).hexdigest()}\n".encode())
                    return artifact

                before = _fd_count()
                with patch.object(
                    live_history._CapabilityRawStore,
                    "put_unique_query",
                    new=corrupt,
                ):
                    with self.assertRaises((ValueError, RuntimeError)):
                        _persist_direct(temporary, observation_id=f"manifest-{label}")
                self.assertEqual(_fd_count(), before)

        with tempfile.TemporaryDirectory() as temporary:

            def noncanonical(store, source, query, payload):
                artifact = original_put(store, source, query, payload)
                content = artifact.manifest_path.read_bytes() + b"\n"
                artifact.manifest_path.write_bytes(content)
                artifact.manifest_path.with_name(
                    artifact.manifest_path.name[:-5] + ".sha256"
                ).write_bytes(f"{hashlib.sha256(content).hexdigest()}\n".encode())
                return artifact

            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                new=noncanonical,
            ):
                with self.assertRaisesRegex((ValueError, RuntimeError), "manifest"):
                    _persist_direct(temporary, observation_id="manifest-noncanonical")

        with tempfile.TemporaryDirectory() as temporary:

            def bad_seal(store, source, query, payload):
                artifact = original_put(store, source, query, payload)
                artifact.manifest_path.with_name(
                    artifact.manifest_path.name[:-5] + ".sha256"
                ).write_bytes(b"0" * 64 + b"\n")
                return artifact

            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                new=bad_seal,
            ):
                with self.assertRaisesRegex((ValueError, RuntimeError), "seal"):
                    _persist_direct(temporary, observation_id="manifest-seal")

        with tempfile.TemporaryDirectory() as temporary:

            def bad_name(store, source, query, payload):
                artifact = original_put(store, source, query, payload)
                return replace(
                    artifact,
                    manifest_path=artifact.manifest_path.with_name(
                        "wrong.manifest.json"
                    ),
                )

            with patch.object(
                live_history._CapabilityRawStore,
                "put_unique_query",
                new=bad_name,
            ):
                with self.assertRaisesRegex(ValueError, "manifest path"):
                    _persist_direct(temporary, observation_id="manifest-name")

    def test_parsers_use_authenticated_bytes_despite_payload_path_swap_restore(self):
        schedule = SCHEDULE_COMPLETE.read_bytes()
        original_persist = live_sources._persist_observation
        original_parse = live_sources.parse_live_schedule
        swapped = {}

        def swap_after_persist(*args, **kwargs):
            result = original_persist(*args, **kwargs)
            payload_path = Path(args[0]) / result.source_binding.payload_relative
            backup = payload_path.with_name(payload_path.name + ".backup")
            payload_path.rename(backup)
            payload_path.write_bytes(b"external canary")
            swapped.update(path=payload_path, backup=backup)
            return result

        def parse_and_restore(raw, *, requested_date):
            self.assertEqual(raw, schedule)
            swapped["path"].unlink()
            swapped["backup"].rename(swapped["path"])
            return original_parse(raw, requested_date=requested_date)

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(
                    live_sources,
                    "_persist_observation",
                    side_effect=swap_after_persist,
                ),
                patch.object(
                    live_sources,
                    "parse_live_schedule",
                    side_effect=parse_and_restore,
                ),
            ):
                result = observe_live_slate(
                    Path(temporary),
                    "2026-07-21",
                    fetcher=lambda _source, url, *, max_bytes: _http(
                        url,
                        BOX_COMPLETE.read_bytes() if "/boxscore" in url else schedule,
                    ),
                    observation_id_factory=iter(("swap-schedule", "swap-box")).__next__,
                    durable_clock=lambda: datetime(
                        2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                    ),
                )
            self.assertEqual(result.games[0]["game_pk"], 777001)

    def test_durable_time_is_sampled_after_receipt_and_stored_in_completion(
        self,
    ):
        events = []
        real_write = live_sources.write_exclusive
        real_rebind = live_sources.rebind_regular_snapshot

        def tracked_write(*args, **kwargs):
            if args[3] != "live observation receipt":
                return real_write(*args, **kwargs)
            events.append("write-start")
            result = real_write(*args, **kwargs)
            events.append("write-return")
            return result

        def tracked_rebind(root, expected, label, **kwargs):
            result = real_rebind(root, expected, label, **kwargs)
            if label == "live observation receipt":
                events.append("receipt-rebind")
            return result

        def durable_clock():
            self.assertEqual(events[-1], "receipt-rebind")
            events.append("durable-clock")
            return datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(
                    live_sources, "write_exclusive", side_effect=tracked_write
                ),
                patch.object(
                    live_sources,
                    "rebind_regular_snapshot",
                    side_effect=tracked_rebind,
                ),
            ):
                result = _persist_direct(
                    temporary,
                    observation_id="durable-order",
                    durable_clock=durable_clock,
                )
            receipt = json.loads(
                (Path(temporary) / result.source_binding.receipt_relative).read_text()
            )
            self.assertEqual(events[:4], [
                "write-start",
                "write-return",
                "receipt-rebind",
                "durable-clock",
            ])
            self.assertNotIn("durable_at_utc", receipt)
            self.assertNotIn("pre_receipt_persistence_at_utc", receipt)
            completion = json.loads(
                (
                    Path(temporary)
                    / "audit/experimental_daily/live_receipts"
                    / "durable-order.completion.json"
                ).read_text()
            )
            self.assertEqual(
                completion["receipt_sha256"],
                result.source_binding.receipt_sha256,
            )
            self.assertEqual(
                completion["payload_sha256"],
                result.source_binding.payload_sha256,
            )
            self.assertEqual(
                completion["manifest_sha256"],
                result.source_binding.manifest_sha256,
            )
            self.assertEqual(
                completion["durable_at_utc"], "2026-07-21T15:00:02Z"
            )
            self.assertEqual(
                result.source_binding.durable_at_utc, "2026-07-21T15:00:02Z"
            )

    def test_repeat_observation_reuses_completion_without_resampling_clock(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = _persist_direct(
                temporary,
                observation_id="durable-repeat",
                durable_clock=lambda: datetime(
                    2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc
                ),
            )

            def must_not_resample():
                raise AssertionError("existing completion must own durable time")

            second = _persist_direct(
                temporary,
                observation_id="durable-repeat",
                durable_clock=must_not_resample,
            )
            completion_path = (
                Path(temporary)
                / "audit/experimental_daily/live_receipts"
                / "durable-repeat.completion.json"
            )
            seal_path = completion_path.with_suffix(".sha256")

            self.assertEqual(first.source_binding, second.source_binding)
            self.assertEqual(
                seal_path.read_bytes(),
                f"{hashlib.sha256(completion_path.read_bytes()).hexdigest()}\n".encode(),
            )

    def test_crash_after_receipt_resumes_with_one_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def crash_before_completion():
                raise RuntimeError("crash before completion")

            with self.assertRaisesRegex(RuntimeError, "before completion"):
                _persist_direct(
                    temporary,
                    observation_id="completion-resume",
                    durable_clock=crash_before_completion,
                )
            receipt = (
                root
                / "audit/experimental_daily/live_receipts/completion-resume.json"
            )
            completion = receipt.with_name("completion-resume.completion.json")
            self.assertTrue(receipt.is_file())
            self.assertFalse(completion.exists())

            recovered = _persist_direct(
                temporary,
                observation_id="completion-resume",
                durable_clock=lambda: datetime(
                    2026, 7, 21, 15, 0, 3, tzinfo=timezone.utc
                ),
            )

            self.assertEqual(
                recovered.source_binding.durable_at_utc,
                "2026-07-21T15:00:03Z",
            )
            self.assertEqual(
                len(tuple(receipt.parent.glob("completion-resume.completion.json"))),
                1,
            )

    def test_crash_after_completion_write_recovers_without_resampling(self):
        real_write = live_sources.write_exclusive
        crashed = False

        def crash_after_completion(*args, **kwargs):
            nonlocal crashed
            snapshot = real_write(*args, **kwargs)
            if args[3] == "live observation completion" and not crashed:
                crashed = True
                raise RuntimeError("crash after completion write")
            return snapshot

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                live_sources,
                "write_exclusive",
                side_effect=crash_after_completion,
            ):
                with self.assertRaisesRegex(RuntimeError, "after completion"):
                    _persist_direct(
                        temporary,
                        observation_id="completion-written",
                        durable_clock=lambda: datetime(
                            2026, 7, 21, 15, 0, 4, tzinfo=timezone.utc
                        ),
                    )

            recovered = _persist_direct(
                temporary,
                observation_id="completion-written",
                durable_clock=lambda: self.fail("durable clock was resampled"),
            )
            self.assertEqual(
                recovered.source_binding.durable_at_utc,
                "2026-07-21T15:00:04Z",
            )

    def test_completion_tamper_symlink_and_hardlink_fail_closed(self):
        for attack in ("tamper", "symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                observation_id = f"completion-{attack}"
                completion = (
                    root
                    / "audit/experimental_daily/live_receipts"
                    / f"{observation_id}.completion.json"
                )
                if attack == "tamper":
                    _persist_direct(temporary, observation_id=observation_id)
                    value = json.loads(completion.read_text())
                    value["durable_at_utc"] = "2026-07-21T15:00:09Z"
                    completion.write_bytes(canonical_json_bytes(value))
                else:
                    with self.assertRaisesRegex(RuntimeError, "before completion"):
                        _persist_direct(
                            temporary,
                            observation_id=observation_id,
                            durable_clock=lambda: (_ for _ in ()).throw(
                                RuntimeError("crash before completion")
                            ),
                        )
                    target = root / f"{attack}-target.json"
                    target.write_bytes(b"{}")
                    if attack == "symlink":
                        completion.symlink_to(target)
                    else:
                        os.link(target, completion)

                with self.assertRaises(ValueError):
                    _persist_direct(temporary, observation_id=observation_id)

    def test_root_swap_at_completion_clock_never_writes_outside(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            base = Path(temporary)
            root = base / "root"
            moved = base / "moved-root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()

            def swap_root():
                root.rename(moved)
                root.symlink_to(outside, target_is_directory=True)
                return datetime(2026, 7, 21, 15, 0, 2, tzinfo=timezone.utc)

            with self.assertRaises(ValueError):
                _persist_direct(
                    root,
                    observation_id="completion-root-swap",
                    durable_clock=swap_root,
                )
            self.assertEqual(tuple(outside.iterdir()), ())

    def test_manifest_seal_swap_during_receipt_write_is_detected_and_fds_close(self):
        root_holder = {}
        original_seal = None

        def swap_seal(stage, component=None):
            nonlocal original_seal
            if stage != "after_file_fsync" or "live_receipts/seal-swap.json" not in str(
                component
            ):
                return
            root = root_holder["root"]
            seal = next(
                root.glob(
                    "data/experimental_daily/live_observations/*/*.manifest.sha256"
                )
            )
            backup = seal.with_name(seal.name + ".backup")
            seal.rename(backup)
            seal.write_bytes(b"external canary\n")
            original_seal = (seal, backup)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root_holder["root"] = Path(temporary)
            before = _fd_count()
            try:
                with patch.object(
                    secure_fs, "_secure_fs_test_hook", side_effect=swap_seal
                ):
                    with self.assertRaisesRegex(ValueError, "snapshot|unsafe"):
                        _persist_direct(temporary, observation_id="seal-swap")
            finally:
                if original_seal is not None:
                    seal, backup = original_seal
                    seal.unlink(missing_ok=True)
                    backup.rename(seal)
            self.assertEqual(_fd_count(), before)

    def test_poststart_durable_boundary_fails_before_boxscore_fetch(self):
        fetch_count = 0

        def fetcher(_source, url, *, max_bytes):
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count > 1:
                raise AssertionError("post-start schedule must not fetch boxscore")
            return _http(url, SCHEDULE_COMPLETE.read_bytes())

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "scheduled start"):
                observe_live_slate(
                    Path(temporary),
                    "2026-07-21",
                    fetcher=fetcher,
                    observation_id_factory=lambda: "poststart-boundary",
                    durable_clock=lambda: datetime(
                        2026, 7, 21, 23, 5, tzinfo=timezone.utc
                    ),
                )
            self.assertEqual(fetch_count, 1)
            self.assertTrue(
                (
                    Path(temporary)
                    / "audit/experimental_daily/live_receipts/poststart-boundary.json"
                ).is_file()
            )

    def test_live_slate_deep_freezes_nested_game_values(self):
        nested = {"items": [{"player_id": 1}]}
        slate = LiveSlateObservation(
            "2026-07-21", ({"game_pk": 1, "nested": nested},), {}, ()
        )
        nested["items"][0]["player_id"] = 9
        self.assertEqual(slate.games[0]["nested"]["items"][0]["player_id"], 1)
        with self.assertRaises(TypeError):
            slate.games[0]["nested"]["items"][0]["player_id"] = 2


if __name__ == "__main__":
    unittest.main()
