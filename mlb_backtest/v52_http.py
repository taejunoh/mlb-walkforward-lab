from __future__ import annotations

import hashlib
import math
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .v52_evidence import AttemptCapture, LogicalQuery, response_byte_limit


TRANSIENT_HTTP = frozenset({429, 500, 502, 503, 504})

_SELECTED_HEADERS = (
    "Content-Type",
    "Content-Length",
    "ETag",
    "Last-Modified",
    "Retry-After",
)
_READ_CHUNK_BYTES = 64 * 1024
_USER_AGENT = "mlb-v5.2-acquisition/1.0"
DEFAULT_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class RetryPolicy:
    base_delays: tuple[float, ...] = (1, 2, 4, 8, 16)
    jitter_fraction: float = 0.25
    timeout: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise ValueError("timeout must be positive and finite")

    @classmethod
    def from_acquisition_contract(
        cls, contract: Mapping[str, object]
    ) -> RetryPolicy:
        if not isinstance(contract, Mapping):
            raise ValueError("acquisition contract request timeout is missing")
        timeout = contract.get("request_timeout_seconds")
        if type(timeout) is not int or timeout != int(DEFAULT_TIMEOUT_SECONDS):
            raise ValueError("acquisition contract request timeout differs")
        return cls(timeout=float(timeout))


@dataclass(frozen=True)
class FetchLimits:
    timeout: float
    max_bytes: int


def fetch_limits(
    query: LogicalQuery, *, policy: RetryPolicy | None = None
) -> FetchLimits:
    selected = RetryPolicy() if policy is None else policy
    if not isinstance(selected, RetryPolicy):
        raise TypeError("policy must be a RetryPolicy")
    return FetchLimits(
        timeout=selected.timeout,
        max_bytes=response_byte_limit(query),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_retry_after(value: str | None, *, now_utc: datetime) -> float | None:
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Retry-After must be text or null")
    stripped = value.strip()
    if stripped.isascii() and stripped.isdecimal():
        return float(int(stripped))
    try:
        parsed = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    seconds = (
        parsed.astimezone(timezone.utc) - now_utc.astimezone(timezone.utc)
    ).total_seconds()
    return max(0.0, seconds)


def deterministic_jitter(
    query_id: str,
    attempt_number: int,
    base_delay: float,
    fraction: float = 0.25,
) -> float:
    if not isinstance(query_id, str) or not query_id:
        raise ValueError("query_id must be nonempty text")
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number <= 0:
        raise ValueError("attempt_number must be a positive integer")
    if (
        isinstance(base_delay, bool)
        or not isinstance(base_delay, (int, float))
        or not math.isfinite(base_delay)
        or base_delay < 0
    ):
        raise ValueError("base_delay must be nonnegative and finite")
    if (
        isinstance(fraction, bool)
        or not isinstance(fraction, (int, float))
        or not math.isfinite(fraction)
        or not 0 <= fraction <= 1
    ):
        raise ValueError("fraction must be between zero and one")
    digest = hashlib.sha256(
        f"{query_id}:{attempt_number}:{float(base_delay).hex()}".encode("ascii")
    ).digest()
    unit = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    multiplier = 1.0 - float(fraction) + 2.0 * float(fraction) * unit
    return float(base_delay) * multiplier


def _request_url(query: LogicalQuery) -> tuple[str, str]:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    endpoint = urlsplit(query.endpoint)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("query endpoint must be a canonical HTTPS URL")
    if not all(isinstance(key, str) and key for key in query.parameters):
        raise ValueError("query parameter names must be nonempty text")
    pairs: list[tuple[str, object]] = []
    for key in sorted(query.parameters):
        value = query.parameters[key]
        if value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool):
            pairs.append((key, value))
        elif isinstance(value, bool):
            pairs.append((key, str(value).lower()))
        else:
            raise ValueError("query parameters must contain scalar values")
    encoded = urlencode(pairs)
    return (f"{query.endpoint}?{encoded}" if encoded else query.endpoint, endpoint.hostname.lower())


def _headers(response: object) -> tuple[tuple[str, str], ...]:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ()
    result = []
    for name in _SELECTED_HEADERS:
        value = headers.get(name)
        if value is not None:
            result.append((name, str(value)))
    return tuple(result)


def _header_value(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    for candidate, value in headers:
        if candidate == name:
            return value
    return None


def _read_bounded(
    response: object, max_bytes: int
) -> tuple[bytes, bool, BaseException | None]:
    chunks: list[bytes] = []
    total = 0
    exceeded = False
    while True:
        try:
            chunk = response.read(_READ_CHUNK_BYTES)  # type: ignore[attr-defined]
        except (socket.timeout, OSError) as exc:
            return b"".join(chunks), exceeded, exc
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ValueError("response body must contain bytes")
        remaining = max_bytes - total
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            exceeded = True
            break
        chunks.append(chunk)
        total += len(chunk)
        if total == max_bytes:
            try:
                probe = response.read(1)  # type: ignore[attr-defined]
            except (socket.timeout, OSError) as exc:
                return b"".join(chunks), exceeded, exc
            if probe:
                if not isinstance(probe, bytes):
                    raise ValueError("response body must contain bytes")
                exceeded = True
            break
    return b"".join(chunks), exceeded, None


def _network_classification(exc: BaseException) -> tuple[str, bool]:
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "TRANSIENT_NETWORK", True
    if isinstance(reason, (ConnectionError, OSError)):
        return "TRANSIENT_NETWORK", False
    return "PERMANENT_NETWORK", False


def _capture_response(
    response: object,
    *,
    expected_host: str,
    expected_media_type: str,
    max_bytes: int,
) -> tuple[
    str | None,
    int | None,
    tuple[tuple[str, str], ...],
    bytes | None,
    str | None,
    bool,
]:
    final_url = response.geturl()  # type: ignore[attr-defined]
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()  # type: ignore[attr-defined]
    selected = _headers(response)
    parsed_final = urlsplit(final_url)
    if parsed_final.scheme != "https" or parsed_final.hostname != expected_host:
        return final_url, status, selected, None, "REDIRECT_HOST_NOT_ALLOWED", False

    body, exceeded, read_error = _read_bounded(response, max_bytes)
    if read_error is not None:
        classification, timed_out = _network_classification(read_error)
        return final_url, status, selected, body, classification, timed_out
    if isinstance(status, int) and not 200 <= status <= 299:
        classification = "TRANSIENT_HTTP" if status in TRANSIENT_HTTP else "PERMANENT_HTTP"
        return final_url, status, selected, body, classification, False
    if exceeded:
        return final_url, status, selected, body, "MAX_BYTES_EXCEEDED", False

    content_type = _header_value(selected, "Content-Type")
    actual_media_type = content_type.split(";", 1)[0].strip().lower() if content_type else None
    if actual_media_type != expected_media_type.strip().lower():
        return final_url, status, selected, body, "CONTENT_TYPE_MISMATCH", False

    declared = _header_value(selected, "Content-Length")
    if declared is not None:
        if not declared.isascii() or not declared.isdecimal() or int(declared) != len(body):
            return final_url, status, selected, body, "CONTENT_LENGTH_MISMATCH", False
    return final_url, status, selected, body, None, False


def fetch_attempt(
    query: LogicalQuery,
    *,
    timeout: float,
    max_bytes: int,
    opener=urlopen,
    clock=_utc_now,
) -> AttemptCapture:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be positive and finite")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    request_url, expected_host = _request_url(query)
    request = Request(request_url, headers={"User-Agent": _USER_AGENT})
    started = _canonical_utc(clock())
    try:
        timed_out = False
        response = opener(request, timeout=timeout)
        with response:
            final_url, status, headers, body, classification, timed_out = _capture_response(
                response,
                expected_host=expected_host,
                expected_media_type=query.expected_media_type,
                max_bytes=max_bytes,
            )
    except HTTPError as exc:
        with exc:
            final_url, status, headers, body, classification, timed_out = _capture_response(
                exc,
                expected_host=expected_host,
                expected_media_type=query.expected_media_type,
                max_bytes=max_bytes,
            )
    except (URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
        final_url = None
        status = None
        headers = ()
        body = None
        classification, timed_out = _network_classification(exc)
    ended = _canonical_utc(clock())
    return AttemptCapture(
        started_at_utc=started,
        ended_at_utc=ended,
        final_url=final_url,
        http_status=status,
        response_headers=headers,
        body=body,
        error_classification=classification,
        timed_out=timed_out,
    )
