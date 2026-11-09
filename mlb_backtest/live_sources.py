"""Fail-closed capture of result-free experimental live MLB observations."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .live_top3_contracts import (
    FinalGameObservation,
    LiveHttpObservation,
    SourceBinding,
    canonical_json_bytes,
    validate_target_date,
)
from .models import RawArtifact
from .competition_policy import CompetitionPolicy, REGULAR_SEASON_V1, policy_identity, research_source, validate_competition_policy
from .secure_fs import (
    RegularFileSnapshot,
    RootBinding,
    TrustedRoot,
    open_trusted_root,
    open_parent,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)
from .source_parsers.schedule import (
    OUT_OF_RANGE_UNPLAYED_STATUSES,
    parse_schedule,
    validate_schedule_query_coverage,
)
from .sources import MLB_BASE


MAX_JSON_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_MAX_PITCHER_LIST = 30
_LOCK_OPEN_ATTEMPTS = 16
_LAYOUT_OPEN_ATTEMPTS = 16
_MAX_HTTP_REDIRECTS = 5
_HTTP_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_OBSERVATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CANONICAL_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_RAW_MANIFEST_KEYS = frozenset(
    {
        "source",
        "query",
        "sha256",
        "retrieved_at_utc",
        "row_count",
        "schema_version",
    }
)
_CLAIM_THREAD_LOCKS_GUARD = threading.Lock()
_CLAIM_THREAD_LOCKS: dict[tuple[str, str], tuple[threading.Lock, int]] = {}
_LIVE_LAYOUT_DIRECTORIES = (
    Path("data/experimental_daily/live_observations"),
    Path("audit/experimental_daily/live_observation_claims"),
    Path("audit/experimental_daily/live_receipts"),
)

ALLOWED_FINAL_HOSTS = MappingProxyType(
    {
        "stats_api": frozenset({"statsapi.mlb.com"}),
        "savant": frozenset({"baseballsavant.mlb.com"}),
    }
)
ACCEPTED_PREGAME_STATUSES = frozenset({"Scheduled", "Pre-Game", "Warmup"})
_LIVE_SCHEDULE_FIELDS = (
    "game_pk",
    "official_date",
    "game_type",
    "scheduled_start_utc",
    "status",
    "away_team",
    "away_team_id",
    "home_team",
    "home_team_id",
    "away_probable_pitcher_id",
    "away_probable_pitcher_name",
    "home_probable_pitcher_id",
    "home_probable_pitcher_name",
    "venue_id",
    "venue_name",
    "doubleheader_marker",
    "away_lineup",
    "home_lineup",
)


def _live_root_test_hook(stage: str, root: Path) -> None:
    """No-op seam for deterministic root-swap tests."""


def normalize_live_root_path(root: Path) -> Path:
    """Normalize only a platform-provided top-level symlink such as ``/var``."""
    lexical = Path(os.path.abspath(root))
    if len(lexical.parts) <= 1:
        return lexical
    top = Path(lexical.anchor) / lexical.parts[1]
    top_stat = os.lstat(top)
    if not stat.S_ISLNK(top_stat.st_mode):
        return lexical
    if top != Path("/var"):
        raise ValueError("live root ancestry must contain only directories")
    target = Path(os.readlink(top))
    resolved_top = target if target.is_absolute() else top.parent / target
    if resolved_top != Path("/private/var"):
        raise ValueError("live root ancestry must contain only directories")
    resolved_stat = os.lstat(resolved_top)
    if not stat.S_ISDIR(resolved_stat.st_mode) or stat.S_ISLNK(resolved_stat.st_mode):
        raise ValueError("live root ancestry must contain only directories")
    return resolved_top.joinpath(*lexical.parts[2:])


def _prepare_live_layout_trusted(trusted: TrustedRoot) -> None:
    """Create the fixed live directories exclusively below a held root fd."""
    try:
        for directory in _LIVE_LAYOUT_DIRECTORIES:
            for _attempt in range(_LAYOUT_OPEN_ATTEMPTS):
                try:
                    with open_parent(
                        trusted, directory / ".live-layout-anchor", create=True
                    ) as descriptor:
                        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                            raise ValueError(
                                "live layout component must be a directory"
                            )
                    break
                except FileExistsError:
                    continue
            else:
                raise ValueError("live layout could not be prepared")
            trusted.assert_bound()
    except OSError as exc:
        raise ValueError("live layout contains an unsafe component") from exc


@contextmanager
def _live_root_session(root: Path, expected_root_binding: RootBinding | None):
    """Hold one verified root capability for a complete live observation call."""
    lexical = normalize_live_root_path(Path(root))
    if expected_root_binding is None:
        missing: list[str] = []
        cursor = lexical
        while True:
            try:
                current = os.lstat(cursor)
            except FileNotFoundError:
                if cursor == cursor.parent:
                    raise ValueError("live root has no trusted ancestor") from None
                missing.append(cursor.name)
                cursor = cursor.parent
                continue
            if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
                raise ValueError("live root ancestry must contain only directories")
            break
        _live_root_test_hook("after_existing_root_lstat", lexical)
        with open_trusted_root(cursor) as trusted:
            if missing:
                with open_parent(
                    trusted,
                    Path(*reversed(missing)) / ".live-root-anchor",
                    create=True,
                ):
                    pass
            trusted.assert_bound()
    with open_trusted_root(
        lexical, expected=expected_root_binding
    ) as trusted:
        _prepare_live_layout_trusted(trusted)
        try:
            yield trusted
        except BaseException:
            try:
                trusted.assert_bound()
            except BaseException:
                pass
            raise
        else:
            trusted.assert_bound()


def _capability_raw_store(root: Path, trusted: TrustedRoot) -> object:
    """Reuse the fd-only immutable store with the live-observation namespace."""
    # Import lazily: live_history imports this module for fetch primitives.
    from .live_history import _CapabilityRawStore

    store = _CapabilityRawStore(root, trusted)
    store._relative_root = Path("data/experimental_daily/live_observations")
    store.root = root / store._relative_root
    return store


class ObservationFetcher(Protocol):
    def __call__(
        self,
        source: Literal["stats_api", "savant"],
        url: str,
        *,
        max_bytes: int,
    ) -> LiveHttpObservation: ...


@dataclass(frozen=True)
class PregameParticipants:
    game_pk: int
    away_team_id: int
    home_team_id: int
    away_pitcher_list_id: int | None
    home_pitcher_list_id: int | None
    away_lineup: tuple[int, ...]
    home_lineup: tuple[int, ...]
    complete: bool
    reason_codes: tuple[str, ...]

    @property
    def away_starter_id(self) -> int | None:
        """Compatibility label; this remains pitcher-list evidence, not an announcement."""
        return self.away_pitcher_list_id

    @property
    def home_starter_id(self) -> int | None:
        """Compatibility label; this remains pitcher-list evidence, not an announcement."""
        return self.home_pitcher_list_id


@dataclass(frozen=True)
class LiveSlateObservation:
    target_date: str
    games: tuple[Mapping[str, object], ...]
    participants: Mapping[int, PregameParticipants]
    source_bindings: tuple[SourceBinding, ...]
    initial_exclusion_reasons: Mapping[int, tuple[str, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        validate_target_date(self.target_date)
        object.__setattr__(
            self,
            "games",
            tuple(_deep_freeze(game) for game in self.games),
        )
        object.__setattr__(
            self, "participants", MappingProxyType(dict(self.participants))
        )
        if not isinstance(self.initial_exclusion_reasons, Mapping):
            raise ValueError("initial exclusion reasons differ")
        try:
            normalized = {
                game_pk: tuple(reasons)
                for game_pk, reasons in self.initial_exclusion_reasons.items()
            }
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("initial exclusion reasons differ") from exc
        allowed = {int(game["game_pk"]) for game in self.games}
        if (
            any(
                type(game_pk) is not int or game_pk not in allowed
                for game_pk in normalized
            )
            or any(
                reasons != ("OBSERVATION_AT_OR_AFTER_START",)
                for reasons in normalized.values()
            )
        ):
            raise ValueError("initial exclusion reasons differ")
        object.__setattr__(
            self, "initial_exclusion_reasons", MappingProxyType(normalized)
        )


@dataclass(frozen=True)
class FinalGameScheduleDisposition:
    game_pk: int
    target_date: str
    disposition: str
    reason_codes: tuple[str, ...]
    source_binding: SourceBinding


@dataclass(frozen=True)
class PersistedObservation:
    source_binding: SourceBinding
    payload_bytes: bytes
    payload_snapshot: RegularFileSnapshot


@dataclass(frozen=True)
class LoadedLiveSchedule:
    """A read-only, fully rebound LIVE_SCHEDULE observation."""

    target_date: str
    games: tuple[dict[str, object], ...]
    receipt_relative: Path
    receipt_sha256: str
    input_snapshots: tuple[RegularFileSnapshot, ...]


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_utc(value: datetime, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be an aware datetime")
    normalized = value.astimezone(timezone.utc)
    base = normalized.strftime("%Y-%m-%dT%H:%M:%S")
    if normalized.microsecond:
        base += f".{normalized.microsecond:06d}".rstrip("0")
    return base + "Z"


def _validate_source_url(
    source: Literal["stats_api", "savant"], url: object, label: str
) -> str:
    if source not in ALLOWED_FINAL_HOSTS or not isinstance(url, str) or not url:
        raise ValueError(f"{label} differs")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} authority differs") from exc
    if (
        parsed.scheme != "https"
        or parsed.netloc not in ALLOWED_FINAL_HOSTS[source]
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or url != url.strip()
    ):
        raise ValueError(f"{label} authority differs")
    return url


def _read_bounded_response(response: object, *, max_bytes: int) -> bytes:
    status = getattr(response, "status", None)
    if type(status) is not int or status != 200:
        raise ValueError("live HTTP response status must be 200")
    headers = getattr(response, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        raise ValueError("live HTTP response headers differ")
    declared_value = headers.get("Content-Length")
    declared = None
    if declared_value is not None:
        if not isinstance(declared_value, str) or not re.fullmatch(
            r"[0-9]+", declared_value
        ):
            raise ValueError("response has invalid Content-Length")
        declared = int(declared_value)
        if declared > max_bytes:
            raise ValueError("response exceeds size limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(_READ_CHUNK_BYTES)
        if type(chunk) is not bytes:
            raise ValueError("response body must contain bytes")
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("response exceeds size limit")
        chunks.append(chunk)
    if declared is not None and total != declared:
        raise ValueError("incomplete response body")
    return b"".join(chunks)


class _NoFollowRedirectHandler(HTTPRedirectHandler):
    """Return redirect responses so the live allowlist can inspect each hop."""

    @staticmethod
    def _return_response(_request, response, _code, _message, _headers):
        return response

    http_error_301 = _return_response
    http_error_302 = _return_response
    http_error_303 = _return_response
    http_error_307 = _return_response
    http_error_308 = _return_response


def _default_live_opener(request: Request, *, timeout: int | float) -> object:
    return build_opener(_NoFollowRedirectHandler()).open(request, timeout=timeout)


def _redirect_location(headers: object) -> str:
    if headers is None or not hasattr(headers, "get"):
        raise ValueError("live HTTP redirect headers differ")
    if hasattr(headers, "get_all"):
        values = headers.get_all("Location")
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError("live HTTP redirect Location differs")
        location = values[0]
    else:
        location = headers.get("Location")
    if (
        not isinstance(location, str)
        or not location
        or location != location.strip()
        or "\r" in location
        or "\n" in location
    ):
        raise ValueError("live HTTP redirect Location differs")
    return location


def fetch_live_http(
    source: Literal["stats_api", "savant"],
    url: str,
    *,
    timeout: int | float = 90,
    max_bytes: int = MAX_JSON_BYTES,
    opener: Callable[..., object] | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LiveHttpObservation:
    """Fetch one bounded response and bind its exact source metadata."""
    requested_url = _validate_source_url(source, url, "requested URL")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    started_at = _canonical_utc(clock(), "fetch clock")
    transport = _default_live_opener if opener is None else opener
    current_url = requested_url
    for redirects_followed in range(_MAX_HTTP_REDIRECTS + 1):
        request = Request(
            current_url, headers={"User-Agent": "mlb-experimental-live/1.0"}
        )
        response = transport(request, timeout=timeout)
        with response:
            final_url = _validate_source_url(source, response.geturl(), "final URL")
            if final_url != current_url:
                raise ValueError("live HTTP transport followed an unvalidated redirect")
            status = getattr(response, "status", None)
            if status in _HTTP_REDIRECT_STATUSES:
                if redirects_followed == _MAX_HTTP_REDIRECTS:
                    raise ValueError("live HTTP response has too many redirects")
                location = _redirect_location(getattr(response, "headers", None))
                current_url = _validate_source_url(
                    source,
                    urljoin(current_url, location),
                    "redirect URL",
                )
                continue
            body = _read_bounded_response(response, max_bytes=max_bytes)
            completed_at = _canonical_utc(clock(), "fetch clock")
            headers = response.headers
            return LiveHttpObservation(
                requested_url=requested_url,
                final_url=final_url,
                started_at_utc=started_at,
                response_completed_at_utc=completed_at,
                http_status=response.status,
                content_type=headers.get("Content-Type"),
                etag=headers.get("ETag"),
                last_modified=headers.get("Last-Modified"),
                body=body,
            )
    raise RuntimeError("unreachable live HTTP redirect state")


def _decode_object(raw: bytes, label: str) -> dict[str, object]:
    if type(raw) is not bytes:
        raise ValueError(f"{label} must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed {label}: expected UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"malformed {label}: root must be an object")
    return value


def live_schedule_url(start: str, end: str, *, competition_policy: CompetitionPolicy = REGULAR_SEASON_V1) -> str:
    """Build the result-free live schedule request used by both observations."""
    query = urlencode(
        {
            "sportId": 1,
            "gameType": ','.join(validate_competition_policy(competition_policy).target_game_types),
            "startDate": start,
            "endDate": end,
            "hydrate": "probablePitcher,venue",
        }
    )
    return f"{MLB_BASE}/schedule?{query}"


def _validate_raw_live_schedule(payload: dict[str, object], *, competition_policy: CompetitionPolicy = REGULAR_SEASON_V1) -> None:
    dates = payload.get("dates")
    if not isinstance(dates, list):
        raise ValueError("malformed live schedule: dates must be a list")
    seen: set[int] = set()
    for entry in dates:
        if not isinstance(entry, dict) or not isinstance(entry.get("games"), list):
            raise ValueError("malformed live schedule: games must be a list")
        for game in entry["games"]:
            if not isinstance(game, dict):
                raise ValueError("malformed live schedule: game must be an object")
            if competition_policy == REGULAR_SEASON_V1 and game.get("gameType") != "R":
                raise ValueError("live schedule result must contain only regular games")
            game_pk = game.get("gamePk")
            if type(game_pk) is not int or game_pk <= 0:
                raise ValueError("malformed live schedule: invalid gamePk")
            if game_pk in seen:
                raise ValueError("malformed live schedule: duplicate gamePk")
            seen.add(game_pk)


def parse_live_schedule(
    raw: bytes, *, requested_date: str, competition_policy: CompetitionPolicy = REGULAR_SEASON_V1
) -> tuple[dict[str, object], ...]:
    """Project the schedule onto a closed, result-free field whitelist."""
    requested_date = validate_target_date(requested_date)
    payload = _decode_object(raw, "live schedule")
    competition_policy = validate_competition_policy(competition_policy)
    _validate_raw_live_schedule(payload, competition_policy=competition_policy)
    types = ('R',) if competition_policy == REGULAR_SEASON_V1 else None
    validate_schedule_query_coverage(raw, requested_date, requested_date, game_types=types)
    games = parse_schedule(raw, preserve_explicit_non_doubleheader=True, game_types=types)
    projected: list[dict[str, object]] = []
    for game in games:
        if (
            game["official_date"] != requested_date
            and game["status"] not in OUT_OF_RANGE_UNPLAYED_STATUSES
        ):
            raise ValueError("live schedule official date differs from requested date")
        if competition_policy == REGULAR_SEASON_V1 and game["game_type"] != "R":
            raise ValueError("live schedule result must contain only regular games")
        if not _CANONICAL_UTC_RE.fullmatch(str(game["scheduled_start_utc"])):
            raise ValueError("live schedule start must be canonical UTC")
        projection = {field: game[field] for field in _LIVE_SCHEDULE_FIELDS}
        projection["away_lineup"] = ()
        projection["home_lineup"] = ()
        projected.append(projection)
    return tuple(projected)


def _positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"malformed boxscore: {label} must be a positive integer")
    return value


def _parse_side(
    raw: object, side: str
) -> tuple[int, int | None, tuple[int, ...], tuple[str, ...]]:
    if not isinstance(raw, dict):
        raise ValueError(f"malformed boxscore: teams.{side} must be an object")
    team = raw.get("team")
    if not isinstance(team, dict):
        raise ValueError(f"malformed boxscore: teams.{side}.team must be an object")
    team_id = _positive_int(team.get("id"), f"teams.{side}.team.id")
    pitchers = raw.get("pitchers")
    if not isinstance(pitchers, list) or len(pitchers) > _MAX_PITCHER_LIST:
        raise ValueError(f"malformed boxscore: teams.{side}.pitchers differs")
    pitcher_ids = tuple(
        _positive_int(value, f"teams.{side}.pitchers") for value in pitchers
    )
    if len(set(pitcher_ids)) != len(pitcher_ids):
        raise ValueError(f"malformed boxscore: teams.{side}.pitchers has duplicates")

    players = raw.get("players")
    if not isinstance(players, dict):
        raise ValueError(f"malformed boxscore: teams.{side}.players must be an object")
    by_order: dict[int, int] = {}
    appearance_order = False
    for player in players.values():
        if not isinstance(player, dict):
            raise ValueError(f"malformed boxscore: teams.{side}.players differs")
        batting_order = player.get("battingOrder")
        if batting_order is None:
            continue
        person = player.get("person")
        if not isinstance(person, dict):
            raise ValueError(f"malformed boxscore: teams.{side}.person differs")
        player_id = _positive_int(person.get("id"), f"teams.{side}.person.id")
        if not isinstance(batting_order, str) or batting_order not in {
            f"{index}00" for index in range(1, 10)
        }:
            appearance_order = True
            continue
        slot = int(batting_order[0])
        if slot in by_order or player_id in by_order.values():
            appearance_order = True
            continue
        by_order[slot] = player_id

    lineup = (
        tuple(by_order[index] for index in range(1, 10))
        if not appearance_order and set(by_order) == set(range(1, 10))
        else ()
    )
    reasons: list[str] = []
    if not pitcher_ids:
        reasons.append(f"MISSING_{side.upper()}_PITCHER_LIST")
    if not lineup:
        reasons.append(f"MISSING_{side.upper()}_LINEUP")
    if appearance_order:
        reasons.append("POSTSTART_APPEARANCE_ORDER")
    return team_id, pitcher_ids[0] if pitcher_ids else None, lineup, tuple(reasons)


def parse_pregame_boxscore(raw: bytes, *, game_pk: int) -> PregameParticipants:
    """Extract only pregame team, pitcher-list, and batting-order evidence."""
    game_pk = _positive_int(game_pk, "game_pk")
    payload = _decode_object(raw, "boxscore")
    teams = payload.get("teams")
    if not isinstance(teams, dict):
        raise ValueError("malformed boxscore: teams must be an object")
    away_id, away_pitcher, away_lineup, away_reasons = _parse_side(
        teams.get("away"), "away"
    )
    home_id, home_pitcher, home_lineup, home_reasons = _parse_side(
        teams.get("home"), "home"
    )
    reasons = tuple(sorted(set((*away_reasons, *home_reasons))))
    complete = (
        away_pitcher is not None
        and home_pitcher is not None
        and len(away_lineup) == 9
        and len(home_lineup) == 9
        and not reasons
    )
    return PregameParticipants(
        game_pk,
        away_id,
        home_id,
        away_pitcher,
        home_pitcher,
        away_lineup,
        home_lineup,
        complete,
        reasons,
    )


def compare_probable_pitchers(
    game: Mapping[str, object], participants: PregameParticipants
) -> PregameParticipants:
    """Require contemporaneous schedule probables to agree with pitcher-list evidence."""
    if game.get("game_pk") != participants.game_pk:
        raise ValueError("participant game identity differs")
    if (
        game.get("away_team_id") != participants.away_team_id
        or game.get("home_team_id") != participants.home_team_id
    ):
        raise ValueError("participant team identity differs")
    scheduled = (
        game.get("away_probable_pitcher_id"),
        game.get("home_probable_pitcher_id"),
    )
    observed = (
        participants.away_pitcher_list_id,
        participants.home_pitcher_list_id,
    )
    reasons = set(participants.reason_codes)
    reasons.discard("PROBABLE_CONSISTENT")
    if any(value is None for value in scheduled):
        reasons.add("MISSING_SCHEDULE_PROBABLE")
    pairs = list(zip(scheduled, observed, strict=True))
    known_pairs = [
        (expected, observed)
        for expected, observed in pairs
        if type(expected) is int and expected > 0
        and type(observed) is int and observed > 0
    ]
    if any(expected != observed for expected, observed in known_pairs):
        reasons.add("STARTER_CONFLICT")
    elif len(known_pairs) == 2 and all(
        expected == observed for expected, observed in known_pairs
    ):
        reasons.add("PROBABLE_CONSISTENT")
    complete = (
        participants.complete
        and "STARTER_CONFLICT" not in reasons
        and "MISSING_SCHEDULE_PROBABLE" not in reasons
    )
    return replace(participants, complete=complete, reason_codes=tuple(sorted(reasons)))


def boxscore_url(game_pk: int) -> str:
    return f"{MLB_BASE}/game/{_positive_int(game_pk, 'game_pk')}/boxscore"


def new_observation_id() -> str:
    return uuid.uuid4().hex


def _policy_observation_id(value: str, policy: CompetitionPolicy) -> str:
    suffix = '.postseason-research-v1' if policy != REGULAR_SEASON_V1 else ''
    return _validate_observation_id(value + suffix)


def _live_audit_namespace(name: str, observation_id: str) -> Path:
    suffix = '_postseason_research_v1' if observation_id.endswith('.postseason-research-v1') else ''
    return Path('audit/experimental_daily') / (name + suffix)


def _validate_observation_id(value: object) -> str:
    if not isinstance(value, str) or not _OBSERVATION_ID_RE.fullmatch(value):
        raise ValueError("observation id differs")
    return value


def _validate_http_observation(
    http: object,
    *,
    source: Literal["stats_api", "savant"],
    requested_url: str,
) -> LiveHttpObservation:
    if not isinstance(http, LiveHttpObservation):
        raise ValueError("fetcher must return a LiveHttpObservation")
    _validate_source_url(source, http.requested_url, "observed requested URL")
    _validate_source_url(source, http.final_url, "observed final URL")
    if http.requested_url != requested_url:
        raise ValueError("observed requested URL differs")
    if http.http_status != 200:
        raise ValueError("live HTTP response status must be 200")
    if len(http.body) > MAX_JSON_BYTES:
        raise ValueError("response exceeds size limit")
    return http


def _normalize_query(query: dict[str, object]) -> dict[str, object]:
    try:
        encoded = json.dumps(
            query,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("observation query must contain finite JSON") from exc
    if not isinstance(normalized, dict):
        raise ValueError("observation query must be an object")
    return normalized


def _query_bytes(query: dict[str, object]) -> bytes:
    return json.dumps(
        query,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _raw_manifest_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode(
        "utf-8"
    )


def _validate_raw_artifact_paths(
    root: Path,
    *,
    raw_source: str,
    query: dict[str, object],
    payload: bytes,
    artifact: RawArtifact,
) -> tuple[Path, Path, Path]:
    digest = hashlib.sha256(payload).hexdigest()
    query_digest = hashlib.sha256(_query_bytes(query)).hexdigest()
    folder = root / "data/experimental_daily/live_observations" / raw_source
    expected_payload = folder / f"{digest}.raw"
    expected_manifest = folder / f"{digest}.{query_digest}.manifest.json"
    if artifact.source != raw_source or artifact.sha256 != digest:
        raise ValueError("raw artifact identity differs")
    if Path(os.path.abspath(artifact.data_path)) != expected_payload:
        raise ValueError("raw payload path differs")
    if Path(os.path.abspath(artifact.manifest_path)) != expected_manifest:
        raise ValueError("raw manifest path differs")
    seal = expected_manifest.with_name(expected_manifest.name[:-5] + ".sha256")
    return (
        expected_payload.relative_to(root),
        expected_manifest.relative_to(root),
        seal.relative_to(root),
    )


def _validate_raw_manifest(
    *,
    manifest_bytes: bytes,
    seal_bytes: bytes,
    raw_source: str,
    query: dict[str, object],
    payload_bytes: bytes,
    artifact: RawArtifact,
) -> str:
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("raw manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != _RAW_MANIFEST_KEYS:
        raise ValueError("raw manifest fields differ")
    if manifest_bytes != _raw_manifest_bytes(manifest):
        raise ValueError("raw manifest canonical bytes differ")
    digest = hashlib.sha256(payload_bytes).hexdigest()
    row_count = sum(bool(line.strip()) for line in payload_bytes.splitlines())
    if manifest["source"] != raw_source:
        raise ValueError("raw manifest source differs")
    if manifest["query"] != query:
        raise ValueError("raw manifest query differs")
    if manifest["sha256"] != digest:
        raise ValueError("raw manifest payload sha differs")
    if type(manifest["row_count"]) is not int or manifest["row_count"] != row_count:
        raise ValueError("raw manifest row_count differs")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("raw manifest schema_version differs")
    retrieved_at = manifest["retrieved_at_utc"]
    if not isinstance(retrieved_at, str) or retrieved_at != artifact.retrieved_at_utc:
        raise ValueError("raw manifest retrieved time differs")
    try:
        parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
    except ValueError as exc:
        raise ValueError("raw manifest retrieved time differs") from exc
    if (
        parsed_retrieved_at.tzinfo is None
        or parsed_retrieved_at.utcoffset() != timedelta(0)
    ):
        raise ValueError("raw manifest retrieved time differs")
    if artifact.row_count != row_count:
        raise ValueError("raw artifact row_count differs")
    expected_seal = f"{hashlib.sha256(manifest_bytes).hexdigest()}\n".encode()
    if seal_bytes != expected_seal:
        raise ValueError("raw manifest seal differs")
    return hashlib.sha256(manifest_bytes).hexdigest()


def _build_source_binding(
    *,
    source: Literal["stats_api", "savant"],
    binding_role: str,
    game_pks: tuple[int, ...],
    observation_id: str,
    query_identity_json: str,
    http: LiveHttpObservation,
    durable_at_utc: str,
    payload_sha256: str,
    byte_length: int,
    payload_relative: str,
    manifest_sha256: str,
    receipt_relative: str,
    receipt_sha256: str,
) -> SourceBinding:
    values: dict[str, object] = {
        "source": source,
        "binding_role": binding_role,
        "game_pks": tuple(sorted(set(game_pks))),
        "observation_id": observation_id,
        "query_identity_json": query_identity_json,
        "requested_url": http.requested_url,
        "final_url": http.final_url,
        "response_completed_at_utc": http.response_completed_at_utc,
        "durable_at_utc": durable_at_utc,
        "http_status": http.http_status,
        "content_type": http.content_type,
        "etag": http.etag,
        "last_modified": http.last_modified,
        "payload_sha256": payload_sha256,
        "byte_length": byte_length,
        "payload_relative": payload_relative,
        "manifest_sha256": manifest_sha256,
        "receipt_relative": receipt_relative,
        "receipt_sha256": receipt_sha256,
    }
    values["binding_sha256"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    return SourceBinding(**values)  # type: ignore[arg-type]


def _rescope_persisted_observation(
    persisted: PersistedObservation, game_pks: tuple[int, ...]
) -> PersistedObservation:
    values = asdict(persisted.source_binding)
    values.pop("binding_sha256")
    values["game_pks"] = tuple(sorted(set(game_pks)))
    values["binding_sha256"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    return replace(persisted, source_binding=SourceBinding(**values))


def _validate_lock_file(descriptor: int, parent_descriptor: int, name: str) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise ValueError("unsafe live observation claim lock")


def _open_observation_claim_lock(parent_descriptor: int, name: str) -> int:
    access_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    last_missing: FileNotFoundError | None = None
    for _attempt in range(_LOCK_OPEN_ATTEMPTS):
        try:
            try:
                descriptor = os.open(
                    name,
                    access_flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                descriptor = os.open(
                    name,
                    access_flags,
                    dir_fd=parent_descriptor,
                )
        except FileNotFoundError as exc:
            last_missing = exc
            continue
        try:
            _validate_lock_file(descriptor, parent_descriptor, name)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    raise ValueError(
        "live observation claim lock could not be opened"
    ) from last_missing


@contextmanager
def _observation_claim_thread_lock(trusted: TrustedRoot, observation_id: str):
    key = (trusted.path.as_posix(), observation_id)
    with _CLAIM_THREAD_LOCKS_GUARD:
        entry = _CLAIM_THREAD_LOCKS.get(key)
        if entry is None:
            lock = threading.Lock()
            references = 0
        else:
            lock, references = entry
        _CLAIM_THREAD_LOCKS[key] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _CLAIM_THREAD_LOCKS_GUARD:
            current_lock, references = _CLAIM_THREAD_LOCKS[key]
            if current_lock is not lock:
                raise RuntimeError("live observation claim lock identity differs")
            if references == 1:
                del _CLAIM_THREAD_LOCKS[key]
            else:
                _CLAIM_THREAD_LOCKS[key] = (lock, references - 1)


@contextmanager
def _observation_claim_lock(trusted: TrustedRoot, observation_id: str):
    relative = (
        _live_audit_namespace('live_observation_claims', observation_id)
        / f"{observation_id}.lock"
    )
    descriptor = -1
    with _observation_claim_thread_lock(trusted, observation_id):
        try:
            with open_parent(trusted, relative, create=True) as parent_descriptor:
                descriptor = _open_observation_claim_lock(
                    parent_descriptor, relative.name
                )
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                _validate_lock_file(descriptor, parent_descriptor, relative.name)
                trusted.assert_bound()
                yield
                _validate_lock_file(descriptor, parent_descriptor, relative.name)
                trusted.assert_bound()
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)


def _claim_bytes(
    *,
    raw_source: str,
    source: Literal["stats_api", "savant"],
    binding_role: str,
    observation_id: str,
    query_identity_json: str,
    http: LiveHttpObservation,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": 1,
            "raw_source": raw_source,
            "source": source,
            "binding_role": binding_role,
            "observation_id": observation_id,
            "query_identity_json": query_identity_json,
            "requested_url": http.requested_url,
            "final_url": http.final_url,
            "started_at_utc": http.started_at_utc,
            "response_completed_at_utc": http.response_completed_at_utc,
            "http_status": http.http_status,
            "content_type": http.content_type,
            "etag": http.etag,
            "last_modified": http.last_modified,
            "payload_sha256": hashlib.sha256(http.body).hexdigest(),
            "byte_length": len(http.body),
        }
    )


def _acquire_observation_claim(
    trusted: TrustedRoot,
    *,
    observation_id: str,
    claim_bytes: bytes,
) -> tuple[Path, RegularFileSnapshot]:
    relative = (
        _live_audit_namespace('live_observation_claims', observation_id)
        / f"{observation_id}.json"
    )
    snapshot = read_regular_snapshot(
        trusted,
        relative,
        "live observation claim",
        max_bytes=MAX_JSON_BYTES,
        missing_ok=True,
    )
    if snapshot is None:
        try:
            snapshot = write_exclusive(
                trusted,
                relative,
                claim_bytes,
                "live observation claim",
            )
        except FileExistsError:
            snapshot = read_regular_snapshot(
                trusted,
                relative,
                "live observation claim",
                max_bytes=MAX_JSON_BYTES,
            )
    if snapshot is None or snapshot.contents != claim_bytes:
        raise ValueError("live observation claim differs from expected identity")
    rebind_regular_snapshot(
        trusted,
        snapshot,
        "live observation claim",
        max_bytes=MAX_JSON_BYTES,
    )
    return relative, snapshot


_LIVE_COMPLETION_FIELDS = frozenset(
    {
        "schema_version",
        "source",
        "binding_role",
        "observation_id",
        "query_identity_json",
        "receipt_relative",
        "receipt_sha256",
        "payload_sha256",
        "manifest_sha256",
        "durable_at_utc",
    }
)


def _validate_observation_completion(
    content: bytes, expected: Mapping[str, object]
) -> str:
    try:
        completion = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("live observation completion is invalid") from exc
    if (
        not isinstance(completion, dict)
        or set(completion) != _LIVE_COMPLETION_FIELDS
        or canonical_json_bytes(completion) != content
        or any(completion.get(key) != value for key, value in expected.items())
    ):
        raise ValueError("live observation completion differs")
    durable_at = completion.get("durable_at_utc")
    if not isinstance(durable_at, str) or not _CANONICAL_UTC_RE.fullmatch(durable_at):
        raise ValueError("live observation completion durable time differs")
    normalized = f"{durable_at[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("live observation completion durable time differs") from exc
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("live observation completion durable time differs")
    return durable_at


def _acquire_observation_completion(
    trusted: TrustedRoot,
    *,
    receipt_relative: Path,
    expected: Mapping[str, object],
    durable_clock: Callable[[], datetime],
    final_rebind: Callable[[], None],
) -> str:
    stem = receipt_relative.name.removesuffix(".json")
    completion_relative = receipt_relative.with_name(f"{stem}.completion.json")
    seal_relative = receipt_relative.with_name(f"{stem}.completion.sha256")
    completion_snapshot = read_regular_snapshot(
        trusted,
        completion_relative,
        "live observation completion",
        max_bytes=MAX_JSON_BYTES,
        missing_ok=True,
    )
    seal_snapshot = read_regular_snapshot(
        trusted,
        seal_relative,
        "live observation completion seal",
        max_bytes=1024,
        missing_ok=True,
    )
    if completion_snapshot is None:
        if seal_snapshot is not None:
            raise ValueError("live observation completion seal is orphaned")
        durable_at = _canonical_utc(durable_clock(), "durable clock")
        completion_bytes = canonical_json_bytes(
            {**dict(expected), "durable_at_utc": durable_at}
        )
        try:
            completion_snapshot = write_exclusive(
                trusted,
                completion_relative,
                completion_bytes,
                "live observation completion",
                final_rebind=final_rebind,
            )
        except FileExistsError:
            completion_snapshot = read_regular_snapshot(
                trusted,
                completion_relative,
                "live observation completion",
                max_bytes=MAX_JSON_BYTES,
            )
    if completion_snapshot is None:
        raise ValueError("live observation completion is missing")
    durable_at = _validate_observation_completion(
        completion_snapshot.contents, expected
    )
    expected_seal = (
        f"{hashlib.sha256(completion_snapshot.contents).hexdigest()}\n".encode()
    )

    def rebind_completion_evidence() -> None:
        final_rebind()
        rebind_regular_snapshot(
            trusted,
            completion_snapshot,
            "live observation completion",
            max_bytes=MAX_JSON_BYTES,
        )

    if seal_snapshot is None:
        try:
            seal_snapshot = write_exclusive(
                trusted,
                seal_relative,
                expected_seal,
                "live observation completion seal",
                final_rebind=rebind_completion_evidence,
            )
        except FileExistsError:
            seal_snapshot = read_regular_snapshot(
                trusted,
                seal_relative,
                "live observation completion seal",
                max_bytes=1024,
            )
    if seal_snapshot is None or seal_snapshot.contents != expected_seal:
        raise ValueError("live observation completion seal differs")
    rebind_completion_evidence()
    rebind_regular_snapshot(
        trusted,
        seal_snapshot,
        "live observation completion seal",
        max_bytes=1024,
    )
    return durable_at


def load_live_schedule_observation(
    root: Path, receipt_relative: Path
) -> LoadedLiveSchedule:
    """Load one admitted LIVE_SCHEDULE receipt without fetching or writing.

    This intentionally mirrors the persistence chain: receipt, optional claim,
    raw payload, manifest and seal, then completion and its seal are all read
    through one trusted root and rebound before being returned to another
    authority consumer.
    """
    receipt_relative = Path(receipt_relative)
    with open_trusted_root(root) as trusted:
        receipt_snapshot = read_regular_snapshot(
            trusted, receipt_relative, "live schedule receipt", max_bytes=MAX_JSON_BYTES
        )
        assert receipt_snapshot is not None
        receipt = _decode_object(receipt_snapshot.contents, "live schedule receipt")
        optional_claim = {"claim_relative", "claim_sha256"}
        required = {
            "schema_version", "source", "binding_role", "observation_id",
            "query_identity_json", "requested_url", "final_url", "started_at_utc",
            "response_completed_at_utc", "http_status", "content_type", "etag",
            "last_modified", "payload_sha256", "byte_length", "payload_relative",
            "manifest_sha256", "manifest_relative",
        }
        keys = set(receipt)
        if keys != required and keys != required | optional_claim:
            raise ValueError("live schedule receipt fields differ")
        if canonical_json_bytes(receipt) != receipt_snapshot.contents:
            raise ValueError("live schedule receipt is not canonical")
        if receipt["schema_version"] != 1 or receipt["source"] != "stats_api" or receipt["binding_role"] != "LIVE_SCHEDULE":
            raise ValueError("live schedule receipt identity differs")
        observation_id = _validate_observation_id(receipt["observation_id"])
        expected_receipt = Path("audit/experimental_daily/live_receipts") / f"{observation_id}.json"
        if receipt_relative != expected_receipt:
            raise ValueError("live schedule receipt path differs")
        query_identity_json = receipt["query_identity_json"]
        if not isinstance(query_identity_json, str):
            raise ValueError("live schedule query identity differs")
        try:
            query = json.loads(query_identity_json)
        except json.JSONDecodeError as exc:
            raise ValueError("live schedule query identity differs") from exc
        if not isinstance(query, dict):
            raise ValueError("live schedule query identity differs")
        query = _normalize_query(query)
        target_date = query.get("target_date")
        if not isinstance(target_date, str):
            raise ValueError("live schedule query target date differs")
        target_date = validate_target_date(target_date)
        expected_url = live_schedule_url(target_date, target_date)
        expected_query = {
            "observation_id": observation_id,
            "target_date": target_date,
            "url": expected_url,
        }
        expected_query_json = canonical_json_bytes(expected_query).decode("utf-8").removesuffix("\n")
        if query != expected_query or query_identity_json != expected_query_json:
            raise ValueError("live schedule query identity differs")
        if receipt["requested_url"] != expected_url or receipt["final_url"] != expected_url:
            raise ValueError("live schedule URL differs")
        for timestamp in (receipt["started_at_utc"], receipt["response_completed_at_utc"]):
            if not isinstance(timestamp, str) or _CANONICAL_UTC_RE.fullmatch(timestamp) is None:
                raise ValueError("live schedule receipt time differs")
        if _utc_instant(receipt["started_at_utc"], "live schedule start time") > _utc_instant(receipt["response_completed_at_utc"], "live schedule response time"):
            raise ValueError("live schedule receipt time differs")
        payload_sha256 = receipt["payload_sha256"]
        if not isinstance(payload_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None:
            raise ValueError("live schedule payload sha differs")
        expected_payload = Path("data/experimental_daily/live_observations/mlb_live_schedule") / f"{payload_sha256}.raw"
        query_sha256 = hashlib.sha256(_query_bytes(query)).hexdigest()
        expected_manifest = expected_payload.parent / f"{payload_sha256}.{query_sha256}.manifest.json"
        if receipt["payload_relative"] != expected_payload.as_posix() or receipt["manifest_relative"] != expected_manifest.as_posix():
            raise ValueError("live schedule artifact path differs")
        payload_snapshot = read_regular_snapshot(
            trusted, expected_payload, "live schedule raw payload", max_bytes=MAX_JSON_BYTES
        )
        manifest_snapshot = read_regular_snapshot(
            trusted, expected_manifest, "live schedule raw manifest", max_bytes=MAX_JSON_BYTES
        )
        manifest_seal_relative = expected_manifest.with_name(expected_manifest.name[:-5] + ".sha256")
        manifest_seal_snapshot = read_regular_snapshot(
            trusted, manifest_seal_relative, "live schedule raw manifest seal", max_bytes=1024
        )
        assert payload_snapshot is not None and manifest_snapshot is not None and manifest_seal_snapshot is not None
        if hashlib.sha256(payload_snapshot.contents).hexdigest() != payload_sha256 or receipt["byte_length"] != len(payload_snapshot.contents):
            raise ValueError("live schedule payload binding differs")
        manifest = _decode_object(manifest_snapshot.contents, "live schedule raw manifest")
        artifact = RawArtifact(
            "mlb_live_schedule", payload_sha256, trusted.path / expected_payload,
            trusted.path / expected_manifest, str(manifest.get("retrieved_at_utc")),
            manifest.get("row_count") if type(manifest.get("row_count")) is int else -1,
        )
        manifest_sha256 = _validate_raw_manifest(
            manifest_bytes=manifest_snapshot.contents,
            seal_bytes=manifest_seal_snapshot.contents,
            raw_source="mlb_live_schedule", query=query,
            payload_bytes=payload_snapshot.contents, artifact=artifact,
        )
        if receipt["manifest_sha256"] != manifest_sha256:
            raise ValueError("live schedule manifest binding differs")
        http = LiveHttpObservation(
            requested_url=str(receipt["requested_url"]), final_url=str(receipt["final_url"]),
            started_at_utc=str(receipt["started_at_utc"]),
            response_completed_at_utc=str(receipt["response_completed_at_utc"]),
            http_status=receipt["http_status"], content_type=receipt["content_type"],
            etag=receipt["etag"], last_modified=receipt["last_modified"], body=payload_snapshot.contents,
        )
        _validate_http_observation(http, source="stats_api", requested_url=expected_url)
        snapshots: list[RegularFileSnapshot] = [receipt_snapshot]
        if optional_claim <= keys:
            claim_relative = receipt["claim_relative"]
            claim_sha256 = receipt["claim_sha256"]
            if not isinstance(claim_relative, str) or not isinstance(claim_sha256, str):
                raise ValueError("live schedule claim binding differs")
            if claim_relative != (Path("audit/experimental_daily/live_observation_claims") / f"{observation_id}.json").as_posix():
                raise ValueError("live schedule claim path differs")
            claim_snapshot = read_regular_snapshot(
                trusted, Path(claim_relative), "live schedule claim", max_bytes=MAX_JSON_BYTES
            )
            assert claim_snapshot is not None
            expected_claim = _claim_bytes(
                raw_source="mlb_live_schedule", source="stats_api", binding_role="LIVE_SCHEDULE",
                observation_id=observation_id, query_identity_json=query_identity_json, http=http,
            )
            if claim_snapshot.contents != expected_claim or hashlib.sha256(claim_snapshot.contents).hexdigest() != claim_sha256:
                raise ValueError("live schedule claim binding differs")
            snapshots.append(claim_snapshot)
        receipt_sha256 = hashlib.sha256(receipt_snapshot.contents).hexdigest()
        completion_relative = receipt_relative.with_name(f"{receipt_relative.name.removesuffix('.json')}.completion.json")
        completion_snapshot = read_regular_snapshot(
            trusted, completion_relative, "live schedule completion", max_bytes=MAX_JSON_BYTES
        )
        completion_seal_snapshot = read_regular_snapshot(
            trusted, completion_relative.with_suffix(".sha256"), "live schedule completion seal", max_bytes=1024
        )
        assert completion_snapshot is not None and completion_seal_snapshot is not None
        durable_at = _validate_observation_completion(completion_snapshot.contents, {
            "schema_version": 1, "source": "stats_api", "binding_role": "LIVE_SCHEDULE",
            "observation_id": observation_id, "query_identity_json": query_identity_json,
            "receipt_relative": receipt_relative.as_posix(), "receipt_sha256": receipt_sha256,
            "payload_sha256": payload_sha256, "manifest_sha256": manifest_sha256,
        })
        if _utc_instant(http.response_completed_at_utc, "live schedule response time") > _utc_instant(durable_at, "live schedule durable time"):
            raise ValueError("live schedule completion precedes response")
        expected_completion_seal = f"{hashlib.sha256(completion_snapshot.contents).hexdigest()}\n".encode()
        if completion_seal_snapshot.contents != expected_completion_seal:
            raise ValueError("live schedule completion seal differs")
        snapshots.extend((payload_snapshot, manifest_snapshot, manifest_seal_snapshot, completion_snapshot, completion_seal_snapshot))
        parsed_games = parse_live_schedule(
            payload_snapshot.contents, requested_date=target_date
        )
        target_games = tuple(
            game for game in parsed_games if game["official_date"] == target_date
        )
        games = tuple(
            {
                "game_pk": game["game_pk"], "away_team": game["away_team"],
                "home_team": game["home_team"], "scheduled_start_utc": game["scheduled_start_utc"],
            }
            for game in target_games
        )
        games = tuple(sorted(games, key=lambda game: int(game["game_pk"])))
        for snapshot in snapshots:
            rebind_regular_snapshot(trusted, snapshot, "live schedule input", max_bytes=MAX_JSON_BYTES)
        return LoadedLiveSchedule(target_date, games, receipt_relative, receipt_sha256, tuple(snapshots))


def _persist_observation(
    root: Path,
    *,
    trusted: TrustedRoot | None = None,
    raw_source: str,
    source: Literal["stats_api", "savant"],
    binding_role: str,
    game_pks: tuple[int, ...],
    observation_id: str,
    query: dict[str, object],
    http: LiveHttpObservation,
    durable_clock: Callable[[], datetime],
) -> PersistedObservation:
    if trusted is None:
        with _live_root_session(root, None) as held_root:
            return _persist_observation(
                held_root.path,
                trusted=held_root,
                raw_source=raw_source,
                source=source,
                binding_role=binding_role,
                game_pks=game_pks,
                observation_id=observation_id,
                query=query,
                http=http,
                durable_clock=durable_clock,
            )
    observation_id = _validate_observation_id(observation_id)
    trusted.assert_bound()
    root = trusted.path
    normalized_query = _normalize_query(query)
    if (
        normalized_query.get("observation_id") != observation_id
        or normalized_query.get("url") != http.requested_url
    ):
        raise ValueError("raw observation query identity differs")
    receipt_relative = (
        _live_audit_namespace('live_receipts', observation_id) / f"{observation_id}.json"
    )
    query_identity_json = (
        canonical_json_bytes(normalized_query).decode("utf-8").removesuffix("\n")
    )
    claim_bytes = _claim_bytes(
        raw_source=raw_source,
        source=source,
        binding_role=binding_role,
        observation_id=observation_id,
        query_identity_json=query_identity_json,
        http=http,
    )
    raw_store = _capability_raw_store(root, trusted)
    if normalized_query.get('competition_scope') == 'postseason-research-v1':
        for name in ('live_receipts','live_observation_claims'):
            with open_parent(trusted, _live_audit_namespace(name, observation_id) / '.anchor', create=True):
                pass
    with _observation_claim_lock(trusted, observation_id):
            claim_relative, claim_snapshot = _acquire_observation_claim(
                trusted,
                observation_id=observation_id,
                claim_bytes=claim_bytes,
            )
            claim_sha256 = hashlib.sha256(claim_snapshot.contents).hexdigest()
            artifact = raw_store.put_unique_query(  # type: ignore[attr-defined]
                raw_source, normalized_query, http.body
            )
            payload_relative, manifest_relative, seal_relative = (
                _validate_raw_artifact_paths(
                    root,
                    raw_source=raw_source,
                    query=normalized_query,
                    payload=http.body,
                    artifact=artifact,
                )
            )
            payload_snapshot = read_regular_snapshot(
                trusted,
                payload_relative,
                "live raw payload",
                max_bytes=MAX_JSON_BYTES,
            )
            manifest_snapshot = read_regular_snapshot(
                trusted,
                manifest_relative,
                "live raw manifest",
                max_bytes=MAX_JSON_BYTES,
            )
            seal_snapshot = read_regular_snapshot(
                trusted, seal_relative, "live raw manifest seal", max_bytes=1024
            )
            if (
                payload_snapshot is None
                or manifest_snapshot is None
                or seal_snapshot is None
            ):
                raise ValueError("live raw artifact is missing")
            if payload_snapshot.contents != http.body:
                raise ValueError("live raw payload bytes differ")
            manifest_sha256 = _validate_raw_manifest(
                manifest_bytes=manifest_snapshot.contents,
                seal_bytes=seal_snapshot.contents,
                raw_source=raw_source,
                query=normalized_query,
                payload_bytes=payload_snapshot.contents,
                artifact=artifact,
            )
            receipt = {
                "schema_version": 1,
                "source": source,
                "binding_role": binding_role,
                "observation_id": observation_id,
                "query_identity_json": query_identity_json,
                "requested_url": http.requested_url,
                "final_url": http.final_url,
                "started_at_utc": http.started_at_utc,
                "response_completed_at_utc": http.response_completed_at_utc,
                "http_status": http.http_status,
                "content_type": http.content_type,
                "etag": http.etag,
                "last_modified": http.last_modified,
                "payload_sha256": artifact.sha256,
                "byte_length": len(payload_snapshot.contents),
                "payload_relative": payload_relative.as_posix(),
                "manifest_sha256": manifest_sha256,
                "manifest_relative": manifest_relative.as_posix(),
                "claim_relative": claim_relative.as_posix(),
                "claim_sha256": claim_sha256,
            }
            receipt_bytes = canonical_json_bytes(receipt)

            def rebind_evidence() -> None:
                rebind_regular_snapshot(
                    trusted,
                    claim_snapshot,
                    "live observation claim",
                    max_bytes=MAX_JSON_BYTES,
                )
                rebind_regular_snapshot(
                    trusted,
                    payload_snapshot,
                    "live raw payload",
                    max_bytes=MAX_JSON_BYTES,
                )
                rebind_regular_snapshot(
                    trusted,
                    manifest_snapshot,
                    "live raw manifest",
                    max_bytes=MAX_JSON_BYTES,
                )
                rebind_regular_snapshot(
                    trusted,
                    seal_snapshot,
                    "live raw manifest seal",
                    max_bytes=1024,
                )

            receipt_snapshot = read_regular_snapshot(
                trusted,
                receipt_relative,
                "live observation receipt",
                max_bytes=MAX_JSON_BYTES,
                missing_ok=True,
            )
            if receipt_snapshot is None:
                try:
                    receipt_snapshot = write_exclusive(
                        trusted,
                        receipt_relative,
                        receipt_bytes,
                        "live observation receipt",
                        final_rebind=rebind_evidence,
                    )
                except FileExistsError:
                    receipt_snapshot = read_regular_snapshot(
                        trusted,
                        receipt_relative,
                        "live observation receipt",
                        max_bytes=MAX_JSON_BYTES,
                    )
            if receipt_snapshot is None or receipt_snapshot.contents != receipt_bytes:
                raise ValueError("live observation receipt differs from expected bytes")
            rebind_evidence()
            rebind_regular_snapshot(
                trusted,
                receipt_snapshot,
                "live observation receipt",
                max_bytes=MAX_JSON_BYTES,
            )
            receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()

            def rebind_receipt_evidence() -> None:
                rebind_evidence()
                rebind_regular_snapshot(
                    trusted,
                    receipt_snapshot,
                    "live observation receipt",
                    max_bytes=MAX_JSON_BYTES,
                )

            durable_at = _acquire_observation_completion(
                trusted,
                receipt_relative=receipt_relative,
                expected={
                    "schema_version": 1,
                    "source": source,
                    "binding_role": binding_role,
                    "observation_id": observation_id,
                    "query_identity_json": query_identity_json,
                    "receipt_relative": receipt_relative.as_posix(),
                    "receipt_sha256": receipt_sha256,
                    "payload_sha256": artifact.sha256,
                    "manifest_sha256": manifest_sha256,
                },
                durable_clock=durable_clock,
                final_rebind=rebind_receipt_evidence,
            )
            binding = _build_source_binding(
                source=source,
                binding_role=binding_role,
                game_pks=game_pks,
                observation_id=observation_id,
                query_identity_json=query_identity_json,
                http=http,
                durable_at_utc=durable_at,
                payload_sha256=artifact.sha256,
                byte_length=len(payload_snapshot.contents),
                payload_relative=payload_relative.as_posix(),
                manifest_sha256=manifest_sha256,
                receipt_relative=receipt_relative.as_posix(),
                receipt_sha256=receipt_sha256,
            )
            return PersistedObservation(
                binding, payload_snapshot.contents, payload_snapshot
            )


def _utc_instant(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} differs")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} differs")
    return parsed.astimezone(timezone.utc)


class _PregameBoundaryError(ValueError):
    """A valid observation receipt crossed its game's scheduled start."""


def _require_pregame_boundary(
    binding: SourceBinding, game: Mapping[str, object]
) -> None:
    durable = _utc_instant(binding.durable_at_utc, "durable observation time")
    scheduled_start = _utc_instant(game.get("scheduled_start_utc"), "scheduled start")
    if durable >= scheduled_start:
        raise _PregameBoundaryError(
            "live observation became durable at or after scheduled start"
        )


def observe_live_slate(
    root: Path,
    target_date: str,
    *,
    fetcher: ObservationFetcher = fetch_live_http,
    observation_id_factory: Callable[[], str] = new_observation_id,
    durable_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    expected_root_binding: RootBinding | None = None,
    competition_policy: CompetitionPolicy = REGULAR_SEASON_V1,
) -> LiveSlateObservation:
    target_date = validate_target_date(target_date)
    competition_policy = validate_competition_policy(competition_policy)
    with _live_root_session(root, expected_root_binding) as trusted:
        request = live_schedule_url(target_date, target_date, competition_policy=competition_policy)
        http = _validate_http_observation(
            fetcher("stats_api", request, max_bytes=MAX_JSON_BYTES),
            source="stats_api",
            requested_url=request,
        )
        schedule_id = _policy_observation_id(observation_id_factory(), competition_policy)
        schedule_query = {
            **policy_identity(competition_policy),
            "observation_id": schedule_id,
            "target_date": target_date,
            "url": request,
        }
        schedule_persisted = _persist_observation(
            trusted.path,
            trusted=trusted,
            raw_source=research_source('mlb_live_schedule', competition_policy),
            source="stats_api",
            binding_role="LIVE_SCHEDULE",
            game_pks=(),
            observation_id=schedule_id,
            query=schedule_query,
            http=http,
            durable_clock=durable_clock,
        )
        games = parse_live_schedule(
            schedule_persisted.payload_bytes, requested_date=target_date,
            **({} if competition_policy == REGULAR_SEASON_V1 else {'competition_policy': competition_policy}),
        )
        schedule_persisted = _rescope_persisted_observation(
            schedule_persisted,
            tuple(sorted(int(game["game_pk"]) for game in games)),
        )
        schedule_binding = schedule_persisted.source_binding
        initial_exclusions: dict[int, tuple[str, ...]] = {}
        for game in games:
            if game['game_type'] not in competition_policy.target_game_types:
                continue
            if game["status"] in ACCEPTED_PREGAME_STATUSES:
                try:
                    _require_pregame_boundary(schedule_binding, game)
                except _PregameBoundaryError:
                    initial_exclusions[int(game["game_pk"])] = (
                        "OBSERVATION_AT_OR_AFTER_START",
                    )
        bindings = [schedule_binding]
        participants: dict[int, PregameParticipants] = {}
        for game in games:
            if game['game_type'] not in competition_policy.target_game_types:
                continue
            if game["status"] not in ACCEPTED_PREGAME_STATUSES:
                continue
            game_pk = int(game["game_pk"])
            if game_pk in initial_exclusions:
                continue
            request = boxscore_url(game_pk)
            http = _validate_http_observation(
                fetcher("stats_api", request, max_bytes=MAX_JSON_BYTES),
                source="stats_api",
                requested_url=request,
            )
            observation_id = _policy_observation_id(observation_id_factory(), competition_policy)
            query = {
                **policy_identity(competition_policy),
                "game_pk": game_pk,
                "observation_id": observation_id,
                "target_date": target_date,
                "url": request,
            }
            if competition_policy != REGULAR_SEASON_V1:
                query.update({key: game[key] for key in ('game_type','away_team_id','home_team_id')})
            persisted = _persist_observation(
                trusted.path,
                trusted=trusted,
                raw_source=research_source('mlb_live_boxscore', competition_policy),
                source="stats_api",
                binding_role="LIVE_PARTICIPANTS",
                game_pks=(game_pk,),
                observation_id=observation_id,
                query=query,
                http=http,
                durable_clock=durable_clock,
            )
            try:
                _require_pregame_boundary(persisted.source_binding, game)
            except _PregameBoundaryError:
                initial_exclusions[game_pk] = (
                    "OBSERVATION_AT_OR_AFTER_START",
                )
                continue
            parsed = compare_probable_pitchers(
                game,
                parse_pregame_boxscore(persisted.payload_bytes, game_pk=game_pk),
            )
            participants[game_pk] = parsed
            bindings.append(persisted.source_binding)
        return LiveSlateObservation(
            target_date,
            games,
            participants,
            tuple(bindings),
            initial_exclusions,
        )


def _final_mismatch_reasons(
    saved: Mapping[str, object], current: Mapping[str, object]
) -> tuple[str, ...]:
    reasons: list[str] = []
    if any(
        saved.get(field) != current.get(field)
        for field in (
            "game_pk",
            "game_type",
            "away_team_id",
            "home_team_id",
            "away_team",
            "home_team",
        )
    ):
        reasons.append("GAME_IDENTITY_CHANGED")
    if saved.get("official_date") != current.get("official_date"):
        reasons.append("OFFICIAL_DATE_CHANGED")
    if any(
        saved.get(field) != current.get(field) for field in ("venue_id", "venue_name")
    ):
        reasons.append("VENUE_CHANGED")
    if current.get("status") not in ACCEPTED_PREGAME_STATUSES:
        reasons.append("PREGAME_STATUS_NOT_ACCEPTED")
    if saved.get("scheduled_start_utc") != current.get("scheduled_start_utc"):
        reasons.append("SCHEDULED_START_CHANGED")
    if any(
        saved.get(field) != current.get(field)
        for field in (
            "away_probable_pitcher_id",
            "away_probable_pitcher_name",
            "home_probable_pitcher_id",
            "home_probable_pitcher_name",
        )
    ):
        reasons.append("PROBABLE_PITCHER_CHANGED")
    return tuple(reasons)


def _invalid_final_schedule_reasons(
    raw: bytes, *, game_pk: int, target_date: str
) -> tuple[str, ...]:
    try:
        payload = _decode_object(raw, "live schedule")
        dates = payload.get("dates")
        if not isinstance(dates, list):
            return ("FINAL_SCHEDULE_INVALID",)
        for entry in dates:
            if not isinstance(entry, dict) or not isinstance(entry.get("games"), list):
                continue
            for game in entry["games"]:
                if not isinstance(game, dict) or game.get("gamePk") != game_pk:
                    continue
                official_date = game.get("officialDate", entry.get("date"))
                if official_date != target_date:
                    return ("OFFICIAL_DATE_CHANGED",)
    except ValueError:
        pass
    return ("FINAL_SCHEDULE_INVALID",)


def observe_final_game_schedule(
    root: Path,
    target_date: str,
    saved_game: Mapping[str, object],
    *,
    fetcher: ObservationFetcher = fetch_live_http,
    observation_id_factory: Callable[[], str] = new_observation_id,
    durable_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    expected_root_binding: RootBinding | None = None,
    competition_policy: CompetitionPolicy = REGULAR_SEASON_V1,
) -> FinalGameObservation | FinalGameScheduleDisposition:
    target_date = validate_target_date(target_date)
    competition_policy = validate_competition_policy(competition_policy)
    game_pk = _positive_int(saved_game.get("game_pk"), "game_pk")
    with _live_root_session(root, expected_root_binding) as trusted:
        request = live_schedule_url(target_date, target_date, competition_policy=competition_policy)
        http = _validate_http_observation(
            fetcher("stats_api", request, max_bytes=MAX_JSON_BYTES),
            source="stats_api",
            requested_url=request,
        )
        observation_id = _policy_observation_id(observation_id_factory(), competition_policy)
        query = {
            **policy_identity(competition_policy),
            "game_pk": game_pk,
            "observation_id": observation_id,
            "target_date": target_date,
            "url": request,
        }
        if competition_policy != REGULAR_SEASON_V1:
            query.update({key: saved_game[key] for key in ('game_type','away_team_id','home_team_id')})
        persisted = _persist_observation(
            trusted.path,
            trusted=trusted,
            raw_source=research_source('mlb_live_final_schedule', competition_policy),
            source="stats_api",
            binding_role="FINAL_SCHEDULE",
            game_pks=(game_pk,),
            observation_id=observation_id,
            query=query,
            http=http,
            durable_clock=durable_clock,
        )
        binding = persisted.source_binding
        try:
            games = parse_live_schedule(
                persisted.payload_bytes, requested_date=target_date,
                **({} if competition_policy == REGULAR_SEASON_V1 else {'competition_policy': competition_policy}),
            )
        except ValueError:
            return FinalGameScheduleDisposition(
                game_pk,
                target_date,
                "UNSCORED",
                _invalid_final_schedule_reasons(
                    persisted.payload_bytes,
                    game_pk=game_pk,
                    target_date=target_date,
                ),
                binding,
            )
        current = next((game for game in games if game["game_pk"] == game_pk), None)
        if current is None:
            return FinalGameScheduleDisposition(
                game_pk,
                target_date,
                "UNSCORED",
                ("GAME_MISSING_AT_FINAL_CHECK",),
                binding,
            )
        try:
            _require_pregame_boundary(binding, current)
        except ValueError:
            return FinalGameScheduleDisposition(
                game_pk,
                target_date,
                "UNSCORED",
                ("OBSERVATION_NOT_PREGAME",),
                binding,
            )
        reasons = _final_mismatch_reasons(saved_game, current)
        if reasons:
            return FinalGameScheduleDisposition(
                game_pk, target_date, "UNSCORED", reasons, binding
            )
        return FinalGameObservation(game_pk, target_date, current, binding)


__all__ = [
    "ACCEPTED_PREGAME_STATUSES",
    "ALLOWED_FINAL_HOSTS",
    "FinalGameScheduleDisposition",
    "LiveHttpObservation",
    "LoadedLiveSchedule",
    "LiveSlateObservation",
    "MAX_JSON_BYTES",
    "ObservationFetcher",
    "PersistedObservation",
    "PregameParticipants",
    "RootBinding",
    "boxscore_url",
    "compare_probable_pitchers",
    "fetch_live_http",
    "live_schedule_url",
    "load_live_schedule_observation",
    "new_observation_id",
    "normalize_live_root_path",
    "observe_final_game_schedule",
    "observe_live_slate",
    "parse_live_schedule",
    "parse_pregame_boxscore",
]
