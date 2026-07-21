"""Immutable contracts for the v5.1 experimental live Daily Top 3 output."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, Protocol
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo


MODEL_LABEL = "v5.1-EXPERIMENTAL-LIVE"
CANDIDATE_STATES = frozenset({"VERIFIED", "PRELIMINARY", "UNSCORED", "EXCLUDED"})
BINDING_ROLES = frozenset(
    {
        "LIVE_SCHEDULE",
        "LIVE_PARTICIPANTS",
        "FINAL_SCHEDULE",
        "HISTORY_SCHEDULE",
        "HISTORY_STATCAST",
        "HISTORY_ERA",
        "HISTORY_VENUE",
        "CAREER_BF",
    }
)
LIVE_BINDING_ROLES = frozenset(
    {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}
)
SOURCE_FINAL_HOSTS = MappingProxyType({
    "stats_api": frozenset({"statsapi.mlb.com"}),
    "savant": frozenset({"baseballsavant.mlb.com"}),
})
REQUIRED_RUN_KEYS = frozenset(
    {
        "schema_version",
        "model_label",
        "target_date",
        "command_inputs",
        "executed_at_eastern",
        "executed_at_utc",
        "history_cutoff_date",
        "cache_authority",
        "model_card_sha256",
        "source_bindings",
        "verified_top3",
        "preliminary_top3",
        "unscored_games",
        "excluded_games",
        "warnings",
        "content_sha256",
    }
)

SOURCE_BINDING_KEYS = frozenset(
    {
        "source",
        "binding_role",
        "game_pks",
        "observation_id",
        "query_identity_json",
        "requested_url",
        "final_url",
        "response_completed_at_utc",
        "durable_at_utc",
        "http_status",
        "content_type",
        "etag",
        "last_modified",
        "payload_sha256",
        "byte_length",
        "payload_relative",
        "manifest_sha256",
        "receipt_relative",
        "receipt_sha256",
        "binding_sha256",
    }
)

RANKED_ROW_KEYS = frozenset(
    {
        "game_pk",
        "target_date",
        "away_team",
        "home_team",
        "predicted_winner",
        "pick_probability",
        "pick_probability_display",
        "state",
        "rank",
        "data_completeness",
        "starter_edge_abs",
        "starter_provenance",
        "lineup_provenance",
        "scheduled_start_utc",
        "observation_boundary_utc",
        "reason_codes",
        "model_card_sha256",
        "feature_sha256",
        "source_binding_sha256s",
    }
)

UNRANKED_ROW_KEYS = frozenset(
    {
        "game_pk",
        "target_date",
        "away_team",
        "home_team",
        "state",
        "scheduled_start_utc",
        "reason_codes",
        "source_binding_sha256s",
    }
)

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?Z$"
)
_EASTERN_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?-0[45]:00$"
)


class IntegrityError(ValueError):
    """Authenticated input or output differs from the frozen contract."""


@dataclass(frozen=True)
class SourceBinding:
    source: str
    binding_role: str
    game_pks: tuple[int, ...]
    observation_id: str
    query_identity_json: str
    requested_url: str
    final_url: str
    response_completed_at_utc: str
    durable_at_utc: str
    http_status: int
    content_type: str | None
    etag: str | None
    last_modified: str | None
    payload_sha256: str
    byte_length: int
    payload_relative: str
    manifest_sha256: str
    receipt_relative: str
    receipt_sha256: str
    binding_sha256: str

    def __post_init__(self) -> None:
        try:
            game_pks = tuple(self.game_pks)
        except TypeError as exc:
            raise IntegrityError("source binding game_pks must be sorted unique positive integers") from exc
        object.__setattr__(self, "game_pks", game_pks)
        _validate_source_binding_mapping(asdict(self), verify_hash=True)


@dataclass(frozen=True)
class LiveHistoryBundle:
    target_date: str
    cutoff_date: str
    feature_history: object
    league_era_by_asof: Mapping[str, Mapping[str, object]]
    team_code_by_name: Mapping[str, str]
    venue_references: Mapping[int, Mapping[str, object]]
    cache_authority: Mapping[str, object]
    source_bindings: tuple[SourceBinding, ...]

    def __post_init__(self) -> None:
        target_date = validate_target_date(self.target_date)
        cutoff_date = validate_target_date(self.cutoff_date)
        if cutoff_date != _previous_date(target_date):
            raise IntegrityError("history bundle cutoff differs from target date")
        _validate_cache_authority(self.cache_authority, cutoff_date)
        if not isinstance(self.source_bindings, tuple) or any(
            not isinstance(binding, SourceBinding) for binding in self.source_bindings
        ):
            raise IntegrityError("history bundle source bindings differ")
        required_history_roles = {
            "HISTORY_SCHEDULE",
            "HISTORY_STATCAST",
            "HISTORY_ERA",
            "HISTORY_VENUE",
        }
        roles = {binding.binding_role for binding in self.source_bindings}
        if roles != required_history_roles:
            raise IntegrityError("history bundle binding roles differ")
        for binding in self.source_bindings:
            query_identity = _parse_query_identity_json(binding.query_identity_json)
            if query_identity["as_of_date"] != cutoff_date:
                raise IntegrityError("history bundle query as_of_date differs")
        object.__setattr__(self, "feature_history", _freeze(self.feature_history))
        object.__setattr__(self, "league_era_by_asof", _freeze(self.league_era_by_asof))
        object.__setattr__(self, "team_code_by_name", _freeze(self.team_code_by_name))
        object.__setattr__(self, "venue_references", _freeze(self.venue_references))
        object.__setattr__(self, "cache_authority", _freeze(self.cache_authority))


@dataclass(frozen=True)
class ExecutionTimes:
    executed_at_eastern: str
    executed_at_utc: str

    def __post_init__(self) -> None:
        _validate_execution_times(self.executed_at_eastern, self.executed_at_utc)

    @classmethod
    def capture(
        cls,
        clock: object,
    ) -> "ExecutionTimes":
        instant = clock() if callable(clock) else clock
        if not isinstance(instant, datetime) or instant.tzinfo is None:
            raise ValueError("execution clock must return an aware datetime")
        instant = instant.astimezone(timezone.utc)
        return cls(
            executed_at_eastern=_canonical_eastern(instant),
            executed_at_utc=_canonical_utc(instant),
        )


@dataclass(frozen=True)
class FinalGameObservation:
    game_pk: int
    target_date: str
    projected_game: Mapping[str, object]
    source_binding: SourceBinding

    def __post_init__(self) -> None:
        if type(self.game_pk) is not int or self.game_pk <= 0:
            raise IntegrityError("final observation game_pk differs")
        validate_target_date(self.target_date)
        if not isinstance(self.projected_game, Mapping):
            raise IntegrityError("final observation projected game differs")
        if not isinstance(self.source_binding, SourceBinding):
            raise IntegrityError("final observation source binding differs")
        if self.source_binding.binding_role != "FINAL_SCHEDULE":
            raise IntegrityError("final observation binding role differs")
        if self.game_pk not in self.source_binding.game_pks:
            raise IntegrityError("final observation binding game scope differs")
        projected_game_pk = self.projected_game.get("game_pk")
        if type(projected_game_pk) is not int or projected_game_pk != self.game_pk:
            raise IntegrityError("final observation projected game_pk differs")
        if self.projected_game.get("official_date") != self.target_date:
            raise IntegrityError("final observation official date differs")
        object.__setattr__(self, "projected_game", _freeze(self.projected_game))


@dataclass(frozen=True)
class LiveHttpObservation:
    requested_url: str
    final_url: str
    started_at_utc: str
    response_completed_at_utc: str
    http_status: int
    content_type: str | None
    etag: str | None
    last_modified: str | None
    body: bytes

    def __post_init__(self) -> None:
        requested_source = _validate_source_url(self.requested_url, "requested_url")
        final_source = _validate_source_url(self.final_url, "final_url")
        if requested_source != final_source:
            raise IntegrityError("HTTP observation source authority differs")
        _validate_utc(self.started_at_utc, "started_at_utc")
        _validate_utc(self.response_completed_at_utc, "response_completed_at_utc")
        if _parse_utc(self.started_at_utc) > _parse_utc(self.response_completed_at_utc):
            raise IntegrityError("HTTP observation response completion precedes start")
        if (
            type(self.http_status) is not int
            or not 100 <= self.http_status <= 599
        ):
            raise IntegrityError("HTTP observation status differs")
        for field in ("content_type", "etag", "last_modified"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, str):
                raise IntegrityError(f"HTTP observation {field} differs")
        if type(self.body) is not bytes:
            raise IntegrityError("HTTP observation body differs")


class ObservationFetcher(Protocol):
    def __call__(
        self,
        source: Literal["stats_api", "savant"],
        url: str,
        *,
        max_bytes: int,
    ) -> LiveHttpObservation: ...


class RecordingByteFetcher(Protocol):
    def __call__(
        self,
        source: Literal["stats_api", "savant"],
        url: str,
        *,
        max_bytes: int,
    ) -> bytes: ...


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def canonical_json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("payload must be finite JSON") from exc


def validate_target_date(value: object) -> str:
    if not isinstance(value, str):
        raise IntegrityError("target date must be canonical YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise IntegrityError("target date must be canonical YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise IntegrityError("target date must be canonical YYYY-MM-DD")
    return value


def validate_run_payload(value: object) -> dict[str, object]:
    # JSON Schema checks structure and formats; this Python validator is authoritative
    # for cross-field relationships, authenticated hashes, and ordering semantics.
    if not isinstance(value, Mapping) or set(value) != REQUIRED_RUN_KEYS:
        raise IntegrityError("run payload keys differ")
    try:
        safe = json.loads(canonical_json_bytes(value))
    except ValueError:
        raise
    if not isinstance(safe, dict):
        raise IntegrityError("run payload must be an object")
    if type(safe["schema_version"]) is not int or safe["schema_version"] != 1:
        raise IntegrityError("run payload schema version differs")
    if safe["model_label"] != MODEL_LABEL:
        raise IntegrityError("run payload model label differs")
    target_date = validate_target_date(safe["target_date"])
    cutoff_date = validate_target_date(safe["history_cutoff_date"])
    if cutoff_date != _previous_date(target_date):
        raise IntegrityError("history cutoff differs from target date")
    _validate_command_inputs(safe["command_inputs"], target_date)
    _validate_execution_times(safe["executed_at_eastern"], safe["executed_at_utc"])
    _validate_cache_authority(safe["cache_authority"], cutoff_date)
    _validate_sha(safe["model_card_sha256"], "model_card_sha256")
    bindings = _validate_source_bindings(safe["source_bindings"])
    if safe["verified_top3"] != []:
        raise IntegrityError("verified_top3 is reserved in the MVP")
    _validate_ranked(
        safe["verified_top3"], "VERIFIED", target_date, safe["model_card_sha256"], bindings
    )
    _validate_ranked(
        safe["preliminary_top3"],
        "PRELIMINARY",
        target_date,
        safe["model_card_sha256"],
        bindings,
    )
    _validate_unranked(safe["unscored_games"], "UNSCORED", target_date, bindings)
    _validate_unranked(safe["excluded_games"], "EXCLUDED", target_date, bindings)
    _validate_unique_game_population(safe)
    _validate_warnings(safe["warnings"])
    _validate_content_sha(safe)
    return safe


def _previous_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    return (parsed.fromordinal(parsed.toordinal() - 1)).isoformat()


def _validate_command_inputs(value: object, target_date: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"date", "model_card"}:
        raise IntegrityError("command inputs differ")
    if value["date"] != target_date:
        raise IntegrityError("command input date differs")
    _validate_relative_posix(value["model_card"], "model_card")


def _validate_cache_authority(value: object, cutoff_date: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "namespace",
        "cutoff_date",
        "catalog_sha256",
    }:
        raise IntegrityError("cache authority differs")
    if value["namespace"] != "experimental_daily/history_cache":
        raise IntegrityError("cache namespace differs")
    if value["cutoff_date"] != cutoff_date:
        raise IntegrityError("cache cutoff differs")
    _validate_sha(value["catalog_sha256"], "catalog_sha256")


def _validate_source_bindings(value: object) -> dict[str, Mapping[str, object]]:
    if not isinstance(value, list) or not value:
        raise IntegrityError("source_bindings must be an array")
    observations: set[str] = set()
    hashes: dict[str, Mapping[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise IntegrityError("source binding fields differ")
        _validate_source_binding_mapping(item, verify_hash=True)
        if item["observation_id"] in observations:
            raise IntegrityError("duplicate observation id")
        observations.add(item["observation_id"])
        binding_sha = item["binding_sha256"]
        if binding_sha in hashes:
            raise IntegrityError("duplicate binding_sha256")
        hashes[binding_sha] = item
    return hashes


def _validate_source_binding_mapping(
    item: Mapping[str, object], *, verify_hash: bool
) -> None:
    if set(item) != SOURCE_BINDING_KEYS:
        raise IntegrityError("source binding fields differ")
    source = item["source"]
    if not isinstance(source, str) or source not in SOURCE_FINAL_HOSTS:
        raise IntegrityError("source binding source differs")
    role = item["binding_role"]
    if not isinstance(role, str) or role not in BINDING_ROLES:
        raise IntegrityError("source binding role differs")
    game_pks = item["game_pks"]
    if not isinstance(game_pks, (list, tuple)) or any(
        type(pk) is not int or pk <= 0 for pk in game_pks
    ) or list(game_pks) != sorted(set(game_pks)):
        raise IntegrityError("source binding game_pks must be sorted unique positive integers")
    if not isinstance(item["observation_id"], str) or not item["observation_id"]:
        raise IntegrityError("source binding observation id differs")
    _validate_http_url(item["requested_url"], "requested_url")
    _validate_query_identity_json(
        item["query_identity_json"],
        role,
        item["observation_id"],
        item["requested_url"],
    )
    _validate_final_url(item["final_url"], source)
    _validate_utc(item["response_completed_at_utc"], "response_completed_at_utc")
    _validate_utc(item["durable_at_utc"], "durable_at_utc")
    response = _parse_utc(item["response_completed_at_utc"])
    durable = _parse_utc(item["durable_at_utc"])
    if response > durable:
        raise IntegrityError("source binding durable time precedes response completion")
    if not isinstance(item["http_status"], int) or isinstance(item["http_status"], bool) or not 100 <= item["http_status"] <= 599:
        raise IntegrityError("source binding HTTP status differs")
    for field in ("content_type", "etag", "last_modified"):
        if item[field] is not None and not isinstance(item[field], str):
            raise IntegrityError(f"source binding {field} differs")
    _validate_sha(item["payload_sha256"], "payload_sha256")
    if type(item["byte_length"]) is not int or item["byte_length"] < 0:
        raise IntegrityError("source binding byte length differs")
    _validate_relative_posix(item["payload_relative"], "payload_relative")
    _validate_sha(item["manifest_sha256"], "manifest_sha256")
    _validate_relative_posix(item["receipt_relative"], "receipt_relative")
    _validate_sha(item["receipt_sha256"], "receipt_sha256")
    _validate_sha(item["binding_sha256"], "binding_sha256")
    if verify_hash:
        without = dict(item)
        without.pop("binding_sha256")
        expected = hashlib.sha256(canonical_json_bytes(without)).hexdigest()
        if item["binding_sha256"] != expected:
            raise IntegrityError("binding_sha256 differs")


def _validate_ranked(
    value: object,
    state: str,
    target_date: str,
    model_card_sha256: str,
    bindings: Mapping[str, Mapping[str, object]],
) -> None:
    if not isinstance(value, list) or len(value) > 3:
        raise IntegrityError("ranked population must contain at most three rows")
    ranks: list[int] = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != RANKED_ROW_KEYS:
            raise IntegrityError("ranked row fields differ")
        if row["state"] != state:
            raise IntegrityError("ranked row state differs")
        _validate_common_row(row, target_date)
        if type(row["rank"]) is not int or row["rank"] <= 0:
            raise IntegrityError("rank must be positive integer")
        ranks.append(row["rank"])
        _validate_probability(row["pick_probability"], "pick_probability")
        if not isinstance(row["pick_probability_display"], str) or not row["pick_probability_display"]:
            raise IntegrityError("display probability differs")
        _validate_probability(row["data_completeness"], "data_completeness")
        _validate_finite_number(row["starter_edge_abs"], "starter_edge_abs")
        if row["starter_edge_abs"] < 0:
            raise IntegrityError("starter_edge_abs must be nonnegative")
        for field in ("starter_provenance", "lineup_provenance"):
            if not isinstance(row[field], str) or not row[field]:
                raise IntegrityError(f"{field} differs")
        _validate_utc(row["observation_boundary_utc"], "observation_boundary_utc")
        _validate_sha(row["model_card_sha256"], "model_card_sha256")
        if row["model_card_sha256"] != model_card_sha256:
            raise IntegrityError("ranked row model card differs")
        _validate_sha(row["feature_sha256"], "feature_sha256")
        _validate_ranked_bindings(row, bindings)
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise IntegrityError("ranked population ranks must be independent 1..N")


def _validate_ranked_bindings(
    row: Mapping[str, object], bindings: Mapping[str, Mapping[str, object]]
) -> None:
    refs = row["source_binding_sha256s"]
    _validate_sha_array(refs)
    if not refs:
        raise IntegrityError("ranked row binding references must be nonempty")
    selected = []
    for digest in refs:
        if digest not in bindings:
            raise IntegrityError("ranked row references unknown binding")
        binding = bindings[digest]
        if binding["http_status"] != 200:
            raise IntegrityError("ranked row binding HTTP status differs")
        if binding["game_pks"] and row["game_pk"] not in binding["game_pks"]:
            raise IntegrityError("ranked row binding game scope differs")
        selected.append(binding)
    required = BINDING_ROLES
    roles = {binding["binding_role"] for binding in selected}
    if not required <= roles:
        raise IntegrityError("ranked row lacks required binding roles")
    live_roles = {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}
    for role in live_roles:
        if not any(
            binding["binding_role"] == role and row["game_pk"] in binding["game_pks"]
            for binding in selected
        ):
            raise IntegrityError("ranked row live binding is not game-scoped")
    boundary = max(_parse_utc(binding["durable_at_utc"]) for binding in selected)
    scheduled = _parse_utc(row["scheduled_start_utc"])
    if boundary >= scheduled:
        raise IntegrityError("observation boundary must precede scheduled start")
    if row["observation_boundary_utc"] != _canonical_utc(boundary):
        raise IntegrityError("observation boundary differs from binding durable times")


def _validate_unranked(
    value: object,
    state: str,
    target_date: str,
    bindings: Mapping[str, Mapping[str, object]],
) -> None:
    if not isinstance(value, list):
        raise IntegrityError("unranked population must be an array")
    for row in value:
        if not isinstance(row, Mapping) or set(row) != UNRANKED_ROW_KEYS:
            raise IntegrityError("unranked row fields differ")
        if row["state"] != state:
            raise IntegrityError("unranked row state differs")
        _validate_common_row(row, target_date)
        refs = row["source_binding_sha256s"]
        _validate_sha_array(refs)
        for digest in refs:
            if digest not in bindings:
                raise IntegrityError("unranked row references unknown binding")
            binding = bindings[digest]
            if binding["game_pks"] and row["game_pk"] not in binding["game_pks"]:
                raise IntegrityError("unranked row binding game scope differs")


def _validate_common_row(row: Mapping[str, object], target_date: str) -> None:
    if type(row["game_pk"]) is not int or row["game_pk"] <= 0:
        raise IntegrityError("game_pk must be positive integer")
    if row["target_date"] != target_date:
        raise IntegrityError("row target date differs")
    for field in ("away_team", "home_team"):
        if not isinstance(row[field], str) or not row[field]:
            raise IntegrityError(f"{field} differs")
    _validate_utc(row["scheduled_start_utc"], "scheduled_start_utc")
    _validate_string_array(row["reason_codes"], "reason_codes")


def _validate_unique_game_population(value: Mapping[str, object]) -> None:
    seen: set[int] = set()
    for key in ("verified_top3", "preliminary_top3", "unscored_games", "excluded_games"):
        rows = value[key]
        for row in rows:
            game_pk = row["game_pk"]
            if game_pk in seen:
                raise IntegrityError("duplicate game_pk across populations")
            seen.add(game_pk)


def _validate_content_sha(payload: Mapping[str, object]) -> None:
    digest = payload["content_sha256"]
    _validate_sha(digest, "content_sha256")
    without = dict(payload)
    without.pop("content_sha256")
    expected = hashlib.sha256(canonical_json_bytes(without)).hexdigest()
    if digest != expected:
        raise IntegrityError("content_sha256 differs")


def _validate_warnings(value: object) -> None:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise IntegrityError("warnings must contain non-empty strings")


def _validate_sha_array(value: object) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise IntegrityError("source binding digest array differs")
    if value != sorted(set(value)):
        raise IntegrityError("source binding digest array differs")
    for item in value:
        _validate_sha(item, "source binding digest")


def _validate_string_array(value: object, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise IntegrityError(f"{label} must be sorted and unique")
    if value != sorted(set(value)):
        raise IntegrityError(f"{label} must be sorted and unique")
    if any(not item for item in value):
        raise IntegrityError(f"{label} must contain non-empty strings")


def _validate_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise IntegrityError(f"{label} must be lowercase SHA-256")


def _validate_probability(value: object, label: str) -> None:
    _validate_finite_number(value, label)
    if not 0 <= value <= 1:
        raise IntegrityError(f"{label} must be in [0,1]")


def _validate_finite_number(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise IntegrityError(f"{label} must be finite")


def _validate_http_url(value: object, label: str) -> None:
    if not isinstance(value, str):
        raise IntegrityError(f"{label} must be an HTTP URL")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or value != value.strip():
        raise IntegrityError(f"{label} must be an HTTP URL")


def _validate_source_url(value: object, label: str) -> str:
    _validate_http_url(value, label)
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise IntegrityError(f"{label} authority differs") from exc
    source = next(
        (
            candidate_source
            for candidate_source, hosts in SOURCE_FINAL_HOSTS.items()
            if parsed.netloc in hosts
        ),
        None,
    )
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or source is None
    ):
        raise IntegrityError(f"{label} authority differs")
    return source


def _parse_query_identity_json(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        raise IntegrityError("source binding query identity differs")
    try:
        query_identity = json.loads(value)
        canonical = canonical_json_bytes(query_identity).decode("utf-8").removesuffix("\n")
    except (TypeError, ValueError) as exc:
        raise IntegrityError("source binding query identity differs") from exc
    if not isinstance(query_identity, dict) or not query_identity or value != canonical:
        raise IntegrityError("source binding query identity differs")
    return query_identity

def _validate_query_identity_json(
    value: object, role: str, observation_id: str, requested_url: str
) -> None:
    query_identity = _parse_query_identity_json(value)
    if query_identity.get("url") != requested_url:
        raise IntegrityError("source binding query URL differs")
    query_keys = {
        key.lower().replace("-", "_")
        for key, _value in parse_qsl(urlsplit(requested_url).query, keep_blank_values=True)
    }
    if query_keys & {"observation_id", "observationid"}:
        raise IntegrityError("observation identity must not be added to source URL")
    if role in LIVE_BINDING_ROLES:
        if query_identity.get("observation_id") != observation_id:
            raise IntegrityError("live source binding query identity differs")
        if "as_of_date" in query_identity:
            raise IntegrityError("live source binding query identity includes as_of_date")
    else:
        if "observation_id" in query_identity:
            raise IntegrityError("history source binding query identity differs")
        if "as_of_date" not in query_identity:
            raise IntegrityError("history source binding query identity lacks as_of_date")
        validate_target_date(query_identity["as_of_date"])


def _validate_final_url(value: object, source: str) -> None:
    if _validate_source_url(value, "final_url") != source:
        raise IntegrityError("source binding final authority differs")


def _validate_relative_posix(value: object, label: str) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise IntegrityError(f"{label} must be repository-relative POSIX")
    path = PurePosixPath(value)
    if value.startswith("/") or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise IntegrityError(f"{label} must be repository-relative POSIX")


def _validate_utc(value: object, label: str) -> None:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise IntegrityError(f"{label} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityError(f"{label} must be canonical UTC") from exc
    if _canonical_utc(parsed) != value:
        raise IntegrityError(f"{label} must be canonical UTC")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _validate_execution_times(eastern: object, utc: object) -> None:
    _validate_eastern(eastern)
    _validate_utc(utc, "executed_at_utc")
    if _canonical_utc(datetime.fromisoformat(str(eastern)).astimezone(timezone.utc)) != utc:
        raise IntegrityError("execution ET and UTC identify different instants")


def _validate_eastern(value: object) -> None:
    if not isinstance(value, str) or not _EASTERN_RE.fullmatch(value):
        raise IntegrityError("executed_at_eastern must be canonical Eastern time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise IntegrityError("executed_at_eastern must be canonical Eastern time") from exc
    if parsed.utcoffset() not in {
        timedelta(hours=-4),
        timedelta(hours=-5),
    } or _canonical_eastern(parsed.astimezone(ZoneInfo("America/New_York"))) != value:
        raise IntegrityError("executed_at_eastern must be canonical Eastern time")


def _canonical_utc(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    base = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        return f"{base}.{value.microsecond:06d}".rstrip("0") + "Z"
    return base + "Z"


def _canonical_eastern(value: datetime) -> str:
    eastern = value.astimezone(ZoneInfo("America/New_York"))
    base = eastern.strftime("%Y-%m-%dT%H:%M:%S")
    if eastern.microsecond:
        base += f".{eastern.microsecond:06d}".rstrip("0")
    offset = eastern.strftime("%z")
    return f"{base}{offset[:3]}:{offset[3:]}"


__all__ = [
    "BINDING_ROLES",
    "CANDIDATE_STATES",
    "ExecutionTimes",
    "FinalGameObservation",
    "IntegrityError",
    "LiveHttpObservation",
    "LiveHistoryBundle",
    "LIVE_BINDING_ROLES",
    "MODEL_LABEL",
    "ObservationFetcher",
    "RecordingByteFetcher",
    "REQUIRED_RUN_KEYS",
    "SourceBinding",
    "canonical_json_bytes",
    "validate_run_payload",
    "validate_target_date",
]
