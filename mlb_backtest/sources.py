import math
from datetime import date
from typing import Any, Iterable
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .source_parsers.schedule import (
    OUT_OF_RANGE_UNPLAYED_STATUSES as OUT_OF_RANGE_UNPLAYED_STATUSES,
    parse_schedule as parse_schedule,
    validate_schedule_query_coverage as validate_schedule_query_coverage,
)

MLB_BASE = "https://statsapi.mlb.com/api/v1"
_USER_AGENT = "mlb-v5.1-backtest/1.0"
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def _validate_http_url(url: Any, label: str) -> None:
    if not isinstance(url, str) or not url:
        raise ValueError(f"{label} must be an HTTP(S) URL")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an HTTP(S) URL")


def fetch_bytes(
    url: str,
    timeout: int | float = 90,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> bytes:
    """Fetch legacy source bytes; v5.2 evidence uses ``v52_http.fetch_attempt``."""
    _validate_http_url(url, "url")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
    ):
        raise ValueError("max_bytes must be a positive integer")

    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        _validate_http_url(response.geturl(), "final response URL")
        content_length = response.headers.get("Content-Length")
        declared_bytes = None
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except (TypeError, ValueError) as exc:
                raise ValueError("response has invalid Content-Length") from exc
            if declared_bytes < 0:
                raise ValueError("response has invalid Content-Length")
            if declared_bytes > max_bytes:
                raise ValueError("response exceeds size limit")

        chunks = []
        total = 0
        while True:
            chunk = response.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                raise ValueError("response body must contain bytes")
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("response exceeds size limit")
            chunks.append(chunk)
        if declared_bytes is not None and total != declared_bytes:
            raise ValueError("incomplete response body")
        return b"".join(chunks)


def schedule_url(start: str, end: str) -> str:
    query = urlencode(
        {
            "sportId": 1,
            "gameType": "R",
            "startDate": start,
            "endDate": end,
            "hydrate": "probablePitcher,lineups,venue,linescore",
        }
    )
    return f"{MLB_BASE}/schedule?{query}"


def selection_dates(
    games: Iterable[dict], start: str, end: str
) -> list[str]:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("selection date bounds must be ISO dates") from exc
    if start_date > end_date:
        raise ValueError("selection date start must not be after end")

    selected = set()
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("games must contain normalized dictionaries")
        if game.get("game_type") != "R":
            continue
        try:
            official_date = date.fromisoformat(game["official_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("regular game must have an ISO official_date") from exc
        if start_date <= official_date <= end_date:
            selected.add(official_date.isoformat())
    return sorted(selected)
