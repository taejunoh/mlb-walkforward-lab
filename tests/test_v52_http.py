from __future__ import annotations

import socket
import unittest
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from unittest import mock

from mlb_backtest.v52_evidence import LogicalQuery


RUN_SHA = "1" * 64
CONTRACT_SHA = "2" * 64
T0 = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _query(
    *,
    source: str = "mlb_schedule_game_feed",
    endpoint: str = "https://statsapi.mlb.com/api/v1/schedule",
    parameters: dict[str, object] | None = None,
    media_type: str = "application/json",
) -> LogicalQuery:
    return LogicalQuery(
        source=source,
        grain="season_schedule_and_game",
        endpoint=endpoint,
        parameters=parameters or {"sportId": 1, "gameType": "R"},
        requested_range={"start_date": "2024-03-20", "end_date": "2024-09-30"},
        expected_media_type=media_type,
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
    )


class _Response:
    def __init__(
        self,
        body: bytes = b"{}",
        *,
        status: int = 200,
        final_url: str = "https://statsapi.mlb.com/api/v1/schedule?sportId=1",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._stream = BytesIO(body)
        self.status = status
        self._final_url = final_url
        self.headers = Message()
        for name, value in (headers or {"Content-Type": "application/json"}).items():
            self.headers[name] = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self):
        return self.status

    def geturl(self):
        return self._final_url

    def read(self, size=-1):
        return self._stream.read(size)


class _FailingBody(BytesIO):
    def __init__(self, failure: OSError) -> None:
        super().__init__(b"")
        self.failure = failure
        self.read_count = 0
        self.close_called = False

    def read(self, _size=-1):
        self.read_count += 1
        if self.read_count == 1:
            return b"part"
        raise self.failure

    def close(self):
        self.close_called = True
        super().close()


class V52HttpTest(unittest.TestCase):
    def _clock(self):
        values = iter((T0, T0.replace(second=1)))
        return lambda: next(values)

    def test_retry_policy_and_transient_http_contract_are_frozen(self):
        from mlb_backtest.v52_http import RetryPolicy, TRANSIENT_HTTP

        self.assertEqual(TRANSIENT_HTTP, frozenset({429, 500, 502, 503, 504}))
        self.assertEqual(RetryPolicy().base_delays, (1, 2, 4, 8, 16))
        self.assertEqual(RetryPolicy().jitter_fraction, 0.25)

    def test_fetch_attempt_captures_final_url_status_and_selected_headers(self):
        from mlb_backtest.v52_http import fetch_attempt

        response = _Response(
            b"{}",
            final_url="https://statsapi.mlb.com/api/v1/schedule?gameType=R&sportId=1",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": "2",
                "ETag": '"abc"',
                "Last-Modified": "Tue, 14 Jul 2026 12:00:00 GMT",
                "Retry-After": "3",
                "X-Ignored": "no",
            },
        )
        capture = fetch_attempt(
            _query(), timeout=5, max_bytes=10, opener=lambda *_a, **_k: response,
            clock=self._clock(),
        )

        self.assertEqual(capture.final_url, response.geturl())
        self.assertEqual(capture.http_status, 200)
        self.assertEqual(
            capture.response_headers,
            (
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", "2"),
                ("ETag", '"abc"'),
                ("Last-Modified", "Tue, 14 Jul 2026 12:00:00 GMT"),
                ("Retry-After", "3"),
            ),
        )
        self.assertEqual(capture.body, b"{}")
        self.assertIsNone(capture.error_classification)
        self.assertEqual(capture.started_at_utc, "2026-07-14T12:00:00.000000Z")
        self.assertEqual(capture.ended_at_utc, "2026-07-14T12:00:01.000000Z")

    def test_content_length_disagreement_is_captured_independently(self):
        from mlb_backtest.v52_http import fetch_attempt

        capture = fetch_attempt(
            _query(), timeout=5, max_bytes=10,
            opener=lambda *_a, **_k: _Response(
                b"{}", headers={"Content-Type": "application/json", "Content-Length": "5"}
            ),
            clock=self._clock(),
        )
        self.assertEqual(capture.body, b"{}")
        self.assertEqual(capture.response_headers[1], ("Content-Length", "5"))
        self.assertEqual(capture.error_classification, "CONTENT_LENGTH_MISMATCH")

    def test_maximum_bytes_preserves_bounded_partial_body_for_quarantine(self):
        from mlb_backtest.v52_http import fetch_attempt

        capture = fetch_attempt(
            _query(), timeout=5, max_bytes=4,
            opener=lambda *_a, **_k: _Response(b"12345"), clock=self._clock(),
        )
        self.assertEqual(capture.body, b"1234")
        self.assertEqual(capture.error_classification, "MAX_BYTES_EXCEEDED")

    def test_http_and_network_failures_are_classified_transient_or_permanent(self):
        from mlb_backtest.v52_http import fetch_attempt

        def http(code: int):
            headers = Message()
            headers["Content-Type"] = "application/json"
            return HTTPError(
                "https://statsapi.mlb.com/api/v1/schedule", code, "bad", headers,
                BytesIO(b'{"error":true}'),
            )

        for failure, expected in (
            (http(503), "TRANSIENT_HTTP"),
            (http(404), "PERMANENT_HTTP"),
            (URLError(socket.timeout("late")), "TRANSIENT_NETWORK"),
            (URLError("unknown url type"), "PERMANENT_NETWORK"),
        ):
            with self.subTest(expected=expected):
                capture = fetch_attempt(
                    _query(), timeout=5, max_bytes=100,
                    opener=mock.Mock(side_effect=failure), clock=self._clock(),
                )
                self.assertEqual(capture.error_classification, expected)

    def test_http_error_is_closed_and_read_failures_become_transient_capture(self):
        from mlb_backtest.v52_http import fetch_attempt

        for failure in (socket.timeout("late body"), OSError("broken body")):
            with self.subTest(failure=failure):
                body = _FailingBody(failure)
                headers = Message()
                headers["Content-Type"] = "application/json"
                headers["Retry-After"] = "5"
                error = HTTPError(
                    "https://statsapi.mlb.com/api/v1/schedule",
                    503,
                    "unavailable",
                    headers,
                    body,
                )
                capture = fetch_attempt(
                    _query(),
                    timeout=5,
                    max_bytes=100,
                    opener=mock.Mock(side_effect=error),
                    clock=self._clock(),
                )
                self.assertTrue(body.close_called)
                self.assertEqual(capture.final_url, error.geturl())
                self.assertEqual(capture.http_status, 503)
                self.assertEqual(capture.response_headers[-1], ("Retry-After", "5"))
                self.assertEqual(capture.body, b"part")
                self.assertEqual(
                    capture.error_classification, "TRANSIENT_NETWORK"
                )

    def test_retry_after_supports_delta_and_http_date(self):
        from mlb_backtest.v52_http import parse_retry_after

        self.assertEqual(parse_retry_after("17", now_utc=T0), 17.0)
        self.assertEqual(
            parse_retry_after("Tue, 14 Jul 2026 12:00:09 GMT", now_utc=T0), 9.0
        )
        self.assertEqual(
            parse_retry_after("Tue, 14 Jul 2026 11:59:59 GMT", now_utc=T0), 0.0
        )
        self.assertIsNone(parse_retry_after("invalid", now_utc=T0))
        self.assertIsNone(parse_retry_after(None, now_utc=T0))

    def test_deterministic_jitter_is_repeatable_and_bounded(self):
        from mlb_backtest.v52_http import deterministic_jitter

        values = [deterministic_jitter("a" * 64, 3, 8.0) for _ in range(3)]
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[1], values[2])
        self.assertGreaterEqual(values[0], 6.0)
        self.assertLessEqual(values[0], 10.0)
        self.assertNotEqual(values[0], deterministic_jitter("a" * 64, 4, 8.0))

    def test_request_url_matches_canonical_endpoint_and_sorted_parameters(self):
        from mlb_backtest.v52_http import fetch_attempt

        opened = mock.Mock(return_value=_Response(final_url=(
            "https://statsapi.mlb.com/api/v1/schedule?gameType=R&hydrate=venue%2Clinescore&sportId=1"
        )))
        query = _query(parameters={"sportId": 1, "hydrate": "venue,linescore", "gameType": "R"})
        fetch_attempt(query, timeout=7, max_bytes=10, opener=opened, clock=self._clock())
        request = opened.call_args.args[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(
            parse_qsl(parsed.query),
            [("gameType", "R"), ("hydrate", "venue,linescore"), ("sportId", "1")],
        )
        self.assertEqual(opened.call_args.kwargs, {"timeout": 7})
        self.assertTrue(request.get_header("User-agent"))

    def test_redirect_host_allowlist_and_expected_content_type_are_enforced(self):
        from mlb_backtest.v52_http import fetch_attempt

        redirected = fetch_attempt(
            _query(), timeout=5, max_bytes=10,
            opener=lambda *_a, **_k: _Response(final_url="https://evil.test/stolen"),
            clock=self._clock(),
        )
        self.assertEqual(redirected.error_classification, "REDIRECT_HOST_NOT_ALLOWED")
        self.assertIsNone(redirected.body)

        compatible = fetch_attempt(
            _query(), timeout=5, max_bytes=10,
            opener=lambda *_a, **_k: _Response(
                headers={"Content-Type": "application/json; charset=UTF-8"}
            ), clock=self._clock(),
        )
        self.assertIsNone(compatible.error_classification)

        incompatible = fetch_attempt(
            _query(), timeout=5, max_bytes=10,
            opener=lambda *_a, **_k: _Response(headers={"Content-Type": "text/html"}),
            clock=self._clock(),
        )
        self.assertEqual(incompatible.error_classification, "CONTENT_TYPE_MISMATCH")
        self.assertEqual(incompatible.body, b"{}")


if __name__ == "__main__":
    unittest.main()
