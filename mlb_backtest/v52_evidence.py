from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import ContextManager, Literal

from .v52_legacy_relation import matches_current_query_to_legacy
from .v52_protected_inventory import (
    ProtectedMembershipSnapshot,
    parse_protected_raw_inventory_document,
    snapshot_protected_inventory_membership,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASCII_DECIMAL = re.compile(r"^[0-9]+$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"\.[0-9]{6}Z$"
)
_ATTEMPT_NAME = re.compile(r"^(?P<number>[0-9]{6})\.json$")
_RESERVATION_NAME = re.compile(r"^(?P<number>[0-9]{6})\.reservation\.json$")
_SELECTED_HEADERS = {
    "content-type": "Content-Type",
    "content-length": "Content-Length",
    "etag": "ETag",
    "last-modified": "Last-Modified",
    "retry-after": "Retry-After",
}
_ATTEMPT_KEYS = {
    "attempt_number",
    "body_sha256",
    "byte_count",
    "ended_at_utc",
    "error_classification",
    "final_url",
    "http_status",
    "outcome",
    "query_id",
    "response_headers",
    "schema_version",
    "started_at_utc",
}
_ATTEMPT_OUTCOMES = {
    "PERMANENT_HTTP",
    "PERMANENT_NETWORK",
    "QUARANTINED",
    "SPLIT_REQUIRED",
    "SUCCESS",
    "TRANSIENT_HTTP",
    "TRANSIENT_NETWORK",
}
_SPLIT_REASONS = frozenset({"ROW_CAP", "SIZE_LIMIT", "TIMEOUT"})
_FAILURE_EVIDENCE_KEYS = _ATTEMPT_KEYS | {
    "evidence_kind",
    "quarantined_body_path",
    "raw_sha256",
}
_LEGACY_REFERENCE_KEYS = {
    "canonical_query",
    "current_query_id",
    "legacy_source",
    "legacy_manifest_path",
    "legacy_manifest_sha256",
    "legacy_seal_path",
    "legacy_seal_sha256",
    "parser_version",
    "payload_path",
    "protected_inventory_path",
    "protected_inventory_sha256",
    "raw_byte_count",
    "raw_row_count",
    "raw_sha256",
    "semantic_sha256",
    "source",
}
_V52_RAW_RELATIVE = Path("data/v5_2_qd/historical/raw")
_PROTECTED_INVENTORY_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
_DERIVED_ENDPOINT = "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
_DERIVED_REASON = "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START"
_STREAM_CHUNK_BYTES = 1024 * 1024
_RESPONSE_BYTE_LIMITS = {
    "boxscore": 16_777_216,
    "career_bf": 16_777_216,
    "league_pitching": 16_777_216,
    "schedule": 134_217_728,
    "statcast": 268_435_456,
    "team": 16_777_216,
    "venue": 16_777_216,
}
_MAX_RESPONSE_BYTE_COUNT = max(_RESPONSE_BYTE_LIMITS.values())
# The frozen retry policy has five retry delays, hence at most six owned bodies.
_MAX_QUARANTINE_BODIES_PER_QUERY = 6
_FAILURE_INTENT_NAME = "failure-intent"
_FAILURE_INTENT_MARKER = "intent.json"
_FAILURE_INTENT_BODY = "body.raw"
_FAILURE_TRANSACTION_NAME = "failure-transaction"
_FAILURE_TRANSACTION_MARKER = "marker.json"
_FAILURE_CLEANUP_PREFIX = ".failure-cleanup-"
_FAILURE_CLEANUP_NAME = re.compile(
    r"\.failure-cleanup-(?P<kind>intent|transaction)-"
    r"(?P<number>[0-9]{6})-(?P<sha256>[0-9a-f]{64})-"
    r"(?P<nonce>[0-9a-f]{32})"
)
_FAILURE_CLEANUP_PAYLOAD = "payload"
_FAILURE_CLEANUP_CLAIM = re.compile(
    r"\.failure-cleanup-claim-(?P<kind>intent|transaction)-"
    r"(?P<number>[0-9]{6})-(?P<nonce>[0-9a-f]{32})\.json"
)


@dataclass(frozen=True)
class LogicalQuery:
    source: str
    grain: str
    endpoint: str
    parameters: Mapping[str, object]
    requested_range: Mapping[str, object]
    expected_media_type: str
    parser_version: str
    parent_query_id: str | None
    run_spec_sha256: str
    acquisition_contract_sha256: str


@dataclass(frozen=True)
class AttemptCapture:
    started_at_utc: str
    ended_at_utc: str
    final_url: str | None
    http_status: int | None
    response_headers: tuple[tuple[str, str], ...]
    body: bytes | None
    error_classification: str | None
    timed_out: bool = False


@dataclass(frozen=True)
class PublishedEvidence:
    query_id: str
    success_receipt_path: Path
    raw_sha256: str | None
    query_sidecar_sha256: str
    storage_mode: Literal[
        "DERIVED_DISPOSITION",
        "FETCHED_RAW",
        "PROTECTED_LEGACY_REFERENCE",
    ]


@dataclass(frozen=True)
class PersistedAttemptEvidence:
    query_id: str
    attempt_number: int
    outcome: Literal["QUARANTINED", "SPLIT_REQUIRED"]
    attempt_receipt_path: Path
    attempt_receipt_sha256: str
    evidence_receipt_path: Path
    evidence_receipt_sha256: str
    body_path: Path | None
    body_sha256: str | None
    byte_count: int
    error_classification: str


@dataclass(frozen=True)
class VerifiedEvidenceRead:
    registered_query: LogicalQuery
    published_evidence: PublishedEvidence
    raw_bytes: bytes | None
    query_sidecar: Mapping[str, object]
    success_receipt: Mapping[str, object]
    attempt_receipts: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class VerifiedQuarantineRead:
    attempt_number: int
    receipt_path: Path
    receipt_sha256: str
    raw_path: Path
    raw_sha256: str
    raw_byte_count: int
    error_classification: str


@dataclass(frozen=True)
class VerifiedQueryProgress:
    query_id: str
    status: Literal["SUCCESS", "QUARANTINED", "PENDING"]
    registered_query: LogicalQuery | None
    published_evidence: PublishedEvidence | None
    attempt_receipts: tuple[Mapping[str, object], ...]
    pending_attempt_numbers: tuple[int, ...]
    quarantines: tuple[VerifiedQuarantineRead, ...]


@dataclass(frozen=True)
class AttemptRetryState:
    attempt_number: int
    retry_ordinal: int | None
    outcome: str
    error_classification: str | None
    ended_at_utc: str
    retry_after: str | None


@dataclass(frozen=True)
class AttemptBudgetSnapshot:
    reservation_numbers: tuple[int, ...]
    receipt_bindings: tuple[tuple[int, str, str], ...]
    latest_receipt: AttemptRetryState | None


class ProtectedLegacyPublishReverifyError(ValueError):
    """Final protected-lineage revalidation failed during publication."""


@dataclass(frozen=True)
class _RegularSnapshot:
    contents: bytes
    identity: tuple[int, ...]


@dataclass(frozen=True)
class _StreamedRegularSnapshot:
    sha256: str
    byte_count: int
    identity: tuple[int, ...]


@dataclass(frozen=True)
class _RepositoryFileSnapshot:
    label: str
    relative_path: str
    ancestor_identities: tuple[tuple[int, int], ...]
    file: _RegularSnapshot


@dataclass(frozen=True)
class _ValidatedLegacyReference:
    reference: dict[str, object]
    payload: bytes
    lineage_snapshots: tuple[_RepositoryFileSnapshot, ...]
    protected_inventory: Mapping[str, object]
    protected_membership: ProtectedMembershipSnapshot


@dataclass(frozen=True)
class QueryLease:
    query_id: str
    source: str
    _store_identity: int
    _descriptor: int
    _owner_thread: int
    _directory_descriptors: tuple[int, ...]
    _registration_sha256: str


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be canonical JSON compatible") from exc


def _canonical_file_bytes(value: object) -> bytes:
    return _canonical_json_bytes(value) + b"\n"


def _normalized_json_object(
    value: Mapping[str, object], label: str
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be strings")
    try:
        normalized = json.loads(_canonical_json_bytes(dict(value)))
    except ValueError as exc:
        raise ValueError(f"{label} must contain JSON-compatible values") from exc
    if not isinstance(normalized, dict):
        raise ValueError(f"{label} must be a JSON object")
    return normalized


def _validate_segment(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{label} must be a nonempty path segment")
    return value


def _validate_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _query_record(query: LogicalQuery) -> dict[str, object]:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    _validate_segment(query.source, "source")
    _validate_text(query.grain, "grain")
    _validate_text(query.endpoint, "endpoint")
    _validate_text(query.expected_media_type, "expected_media_type")
    _validate_text(query.parser_version, "parser_version")
    if query.parent_query_id is not None:
        _validate_sha256(query.parent_query_id, "parent_query_id")
    _validate_sha256(query.run_spec_sha256, "run_spec_sha256")
    _validate_sha256(
        query.acquisition_contract_sha256,
        "acquisition_contract_sha256",
    )
    parameters = _normalized_json_object(query.parameters, "parameters")
    requested_range = _normalized_json_object(query.requested_range, "requested_range")
    record = {
        "acquisition_contract_sha256": query.acquisition_contract_sha256,
        "endpoint": query.endpoint,
        "expected_media_type": query.expected_media_type,
        "grain": query.grain,
        "parameters": parameters,
        "parent_query_id": query.parent_query_id,
        "parser_version": query.parser_version,
        "requested_range": requested_range,
        "run_spec_sha256": query.run_spec_sha256,
        "source": query.source,
    }
    return record


def canonical_query_bytes(query: LogicalQuery) -> bytes:
    return _canonical_json_bytes(_query_record(query))


def logical_query_id(query: LogicalQuery) -> str:
    return hashlib.sha256(canonical_query_bytes(query)).hexdigest()


def response_byte_limit(query: LogicalQuery) -> int:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    endpoint = query.endpoint
    if query.source == "baseball_savant_statcast" and endpoint == (
        "https://baseballsavant.mlb.com/statcast_search/csv"
    ):
        key = "statcast"
    elif query.source in {"mlb_schedule", "mlb_schedule_game_feed"}:
        if endpoint == "https://statsapi.mlb.com/api/v1/schedule":
            key = "schedule"
        elif re.fullmatch(
            r"https://statsapi\.mlb\.com/api/v1/game/[1-9][0-9]*/boxscore",
            endpoint,
        ):
            key = "boxscore"
        else:
            raise ValueError("query has no frozen response byte limit")
    elif query.source == "mlb_stats_pitching" and endpoint == (
        "https://statsapi.mlb.com/api/v1/stats"
    ):
        key = "league_pitching"
    elif query.source == "mlb_stats_pitcher_bf" and re.fullmatch(
        r"https://statsapi\.mlb\.com/api/v1/people/[1-9][0-9]*/stats",
        endpoint,
    ):
        key = "career_bf"
    elif query.source == "venue_team_timezone_table":
        if endpoint == "https://statsapi.mlb.com/api/v1/teams":
            key = "team"
        elif re.fullmatch(
            r"https://statsapi\.mlb\.com/api/v1/venues/[1-9][0-9]*",
            endpoint,
        ):
            key = "venue"
        else:
            raise ValueError("query has no frozen response byte limit")
    else:
        raise ValueError("query has no frozen response byte limit")
    return _RESPONSE_BYTE_LIMITS[key]


_response_byte_limit = response_byte_limit


def _validate_query_byte_count(query: LogicalQuery, byte_count: object) -> int:
    limit = _response_byte_limit(query)
    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or byte_count > limit
    ):
        raise ValueError("attempt exceeds its source response byte limit")
    return limit


def _registration_contents(query: LogicalQuery, lock_metadata: os.stat_result) -> bytes:
    return _canonical_file_bytes(
        {
            **_query_record(query),
            "lock_identity": {
                "device": lock_metadata.st_dev,
                "inode": lock_metadata.st_ino,
            },
        }
    )


def _decode_registration(contents: bytes) -> tuple[LogicalQuery, dict[str, int]]:
    record = _json_object(contents, "logical query registration")
    lock_identity = record.pop("lock_identity", None)
    if (
        not isinstance(lock_identity, dict)
        or set(lock_identity) != {"device", "inode"}
        or any(
            type(lock_identity[key]) is not int or lock_identity[key] < 0
            for key in lock_identity
        )
    ):
        raise ValueError("logical query lock identity differs")
    try:
        query = LogicalQuery(**record)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("logical query registration keys differ") from exc
    if _query_record(query) != record:
        raise ValueError("logical query registration differs")
    return query, lock_identity


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _regular_read_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_root_path(path: Path, *, create: bool) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if not parts or parts[0] != os.sep:
        raise ValueError("evidence root must resolve to an absolute path")
    current = os.open(os.sep, _directory_flags())
    try:
        for component in parts[1:]:
            _validate_segment(component, "evidence root component")
            child = -1
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    os.fsync(current)
                except FileExistsError:
                    pass
                child = os.open(component, _directory_flags(), dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(
                        "evidence root ancestors must be no-follow directories"
                    ) from exc
                raise
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError("evidence root directory identity differs")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_directory_at(parent_fd: int, name: str, *, create: bool) -> int:
    _validate_segment(name, "directory name")
    descriptor = -1
    try:
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except FileNotFoundError:
            if not create:
                raise
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ValueError(
                    "evidence directory must be a no-follow directory"
                ) from exc
            raise
        opened = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise ValueError("evidence directory identity differs")
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _read_regular_snapshot_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    missing_ok: bool = False,
) -> _RegularSnapshot | None:
    _validate_segment(name, f"{label} filename")
    descriptor = -1
    try:
        descriptor = os.open(name, _regular_read_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RuntimeError(f"{label} is missing") from None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            raise ValueError(f"{label} must be a no-follow regular file") from exc
        raise ValueError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _identity(before) != _identity(linked)
        ):
            raise ValueError(f"{label} must be an unaliased regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(
            rebound
        ):
            raise ValueError(f"{label} identity changed while reading")
        return _RegularSnapshot(b"".join(chunks), _identity(after))
    finally:
        os.close(descriptor)


def _read_regular_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    missing_ok: bool = False,
) -> bytes | None:
    snapshot = _read_regular_snapshot_at(
        parent_fd, name, label, missing_ok=missing_ok
    )
    return None if snapshot is None else snapshot.contents


def _read_regular_snapshot_with_link_counts_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    allowed_link_counts: frozenset[int],
    missing_ok: bool = False,
) -> _RegularSnapshot | None:
    _validate_segment(name, f"{label} filename")
    descriptor = -1
    try:
        descriptor = os.open(name, _regular_read_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RuntimeError(f"{label} is missing") from None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            raise ValueError(f"{label} must be a no-follow regular file") from exc
        raise ValueError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in allowed_link_counts
            or _identity(before) != _identity(linked)
        ):
            raise ValueError(f"{label} link count or identity differs")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, _STREAM_CHUNK_BYTES):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(
            rebound
        ):
            raise ValueError(f"{label} identity changed while reading")
        return _RegularSnapshot(b"".join(chunks), _identity(after))
    finally:
        os.close(descriptor)


def _rebind_regular_snapshot_at(
    parent_fd: int,
    name: str,
    expected: _RegularSnapshot,
    label: str,
) -> None:
    current = _read_regular_snapshot_at(parent_fd, name, label)
    if current != expected:
        raise ValueError(f"{label} identity or bytes changed during verification")


def _read_streamed_regular_snapshot_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    expected_byte_count: int,
    maximum_byte_count: int,
) -> _StreamedRegularSnapshot:
    _validate_segment(name, f"{label} filename")
    descriptor = -1
    try:
        descriptor = os.open(name, _regular_read_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        raise RuntimeError(f"{label} is missing") from None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EISDIR, errno.ENXIO}:
            raise ValueError(f"{label} must be a no-follow regular file") from exc
        raise ValueError(f"{label} cannot be opened safely") from exc
    try:
        before = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _identity(before) != _identity(linked)
        ):
            raise ValueError(f"{label} must be an unaliased regular file")
        if (
            isinstance(expected_byte_count, bool)
            or not isinstance(expected_byte_count, int)
            or expected_byte_count < 0
            or expected_byte_count > maximum_byte_count
            or before.st_size != expected_byte_count
        ):
            raise ValueError(f"{label} response byte limit or declared size differs")
        digest = hashlib.sha256()
        byte_count = 0
        while chunk := os.read(descriptor, _STREAM_CHUNK_BYTES):
            byte_count += len(chunk)
            if byte_count > expected_byte_count:
                raise ValueError(f"{label} exceeded its declared byte count")
            digest.update(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(rebound)
            or byte_count != expected_byte_count
        ):
            raise ValueError(f"{label} identity or size changed while streaming")
        return _StreamedRegularSnapshot(
            sha256=digest.hexdigest(),
            byte_count=byte_count,
            identity=_identity(after),
        )
    finally:
        os.close(descriptor)


def _cleanup_created_file(
    parent_fd: int, descriptor: int, name: str, label: str
) -> None:
    opened = os.fstat(descriptor)
    linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or _identity(opened) != _identity(linked)
    ):
        raise ValueError(f"{label} ownership changed before cleanup")
    os.unlink(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def _write_once_at(
    parent_fd: int,
    name: str,
    contents: bytes,
    label: str,
) -> None:
    existing = _read_regular_at(parent_fd, name, label, missing_ok=True)
    if existing is not None:
        if existing != contents:
            raise RuntimeError(f"immutable {label} differs")
        return
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    created = False
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        created = True
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("immutable publication made no progress")
            view = view[written:]
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError(f"{label} publication was aliased")
        os.fsync(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(opened) != _identity(linked):
            raise ValueError(f"{label} identity changed during publication")
        os.fsync(parent_fd)
        rebound = _read_regular_at(parent_fd, name, label)
        if rebound != contents:
            raise ValueError(f"{label} bytes changed after publication")
    except FileExistsError:
        existing = _read_regular_at(parent_fd, name, label)
        if existing != contents:
            raise RuntimeError(f"immutable {label} differs") from None
    except BaseException:
        if created:
            _cleanup_created_file(parent_fd, descriptor, name, label)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_owned_content_link_at(
    source_fd: int,
    source_name: str,
    source: _RegularSnapshot,
    destination_fd: int,
    destination_name: str,
    contents: bytes,
    label: str,
) -> None:
    if destination_name != f"{_sha256_bytes(contents)}.raw":
        raise ValueError(f"{label} destination name differs")
    destination = _read_regular_snapshot_with_link_counts_at(
        destination_fd,
        destination_name,
        label,
        allowed_link_counts=frozenset({2}),
    )
    if (
        source.identity[3] != 2
        or destination is None
        or source != destination
        or source.contents != contents
    ):
        raise ValueError(f"{label} owned link binding differs")
    rebound_source = _read_regular_snapshot_with_link_counts_at(
        source_fd,
        source_name,
        "failure intent body",
        allowed_link_counts=frozenset({2}),
    )
    rebound_destination = _read_regular_snapshot_with_link_counts_at(
        destination_fd,
        destination_name,
        label,
        allowed_link_counts=frozenset({2}),
    )
    if rebound_source != source or rebound_destination != destination:
        raise ValueError(f"{label} owned link identity changed")


def _publish_content_addressed_regular_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
    contents: bytes,
    label: str,
) -> None:
    source = _read_regular_snapshot_with_link_counts_at(
        source_fd,
        source_name,
        "failure intent body",
        allowed_link_counts=frozenset({1, 2}),
    )
    if source is None or source.contents != contents:
        raise ValueError("failure intent body bytes differ")
    if source.identity[3] == 2:
        _validate_owned_content_link_at(
            source_fd,
            source_name,
            source,
            destination_fd,
            destination_name,
            contents,
            label,
        )
        os.unlink(source_name, dir_fd=source_fd)
        os.fsync(source_fd)
        os.fsync(destination_fd)
        published = _read_regular_snapshot_at(
            destination_fd, destination_name, label
        )
        if (
            published is None
            or published.contents != source.contents
            or published.identity[:2] != source.identity[:2]
        ):
            raise ValueError(f"{label} identity or bytes changed after recovery")
        return
    winner = _read_regular_snapshot_at(
        destination_fd,
        destination_name,
        label,
        missing_ok=True,
    )
    if winner is None:
        try:
            os.link(
                source_name,
                destination_name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            winner = _read_regular_snapshot_at(
                destination_fd, destination_name, label
            )
        else:
            linked_source = _read_regular_snapshot_with_link_counts_at(
                source_fd,
                source_name,
                "failure intent body",
                allowed_link_counts=frozenset({2}),
            )
            if linked_source is None:
                raise RuntimeError("failure intent body is missing after link")
            _validate_owned_content_link_at(
                source_fd,
                source_name,
                linked_source,
                destination_fd,
                destination_name,
                contents,
                label,
            )
            os.unlink(source_name, dir_fd=source_fd)
            os.fsync(source_fd)
            os.fsync(destination_fd)
            published = _read_regular_snapshot_at(
                destination_fd, destination_name, label
            )
            if (
                published is None
                or published.contents != source.contents
                or published.identity[:2] != linked_source.identity[:2]
            ):
                raise ValueError(f"{label} identity or bytes changed after publication")
            return
    if winner is None or winner.contents != contents:
        raise RuntimeError(f"immutable {label} differs")
    _rebind_regular_snapshot_at(
        destination_fd, destination_name, winner, label
    )
    _unlink_exact_regular_at(
        source_fd,
        source_name,
        contents,
        "failure intent body",
    )
    _rebind_regular_snapshot_at(
        destination_fd, destination_name, winner, label
    )


def _json_object(contents: bytes, label: str) -> dict[str, object]:
    if not contents.endswith(b"\n") or contents.endswith(b"\n\n"):
        raise ValueError(f"{label} must have exactly one final newline")
    try:
        value = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid canonical JSON") from exc
    if not isinstance(value, dict) or _canonical_file_bytes(value) != contents:
        raise ValueError(f"{label} is not canonical JSON")
    return value


def _sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _failure_cleanup_tombstone_name(
    kind: Literal["intent", "transaction"],
    attempt_number: int,
    marker_contents: bytes,
    *,
    nonce: str | None = None,
) -> str:
    if nonce is None:
        nonce = os.urandom(16).hex()
    return (
        f"{_FAILURE_CLEANUP_PREFIX}{kind}-{attempt_number:06d}-"
        f"{_sha256_bytes(marker_contents)}-{nonce}"
    )


def _failure_cleanup_claim_name(
    kind: Literal["intent", "transaction"], attempt_number: int, nonce: str
) -> str:
    return f"{_FAILURE_CLEANUP_PREFIX}claim-{kind}-{attempt_number:06d}-{nonce}.json"


def _failure_cleanup_claim_bytes(
    *,
    kind: Literal["intent", "transaction"],
    query_id: str,
    attempt_number: int,
    nonce: str,
    container_name: str,
    marker_contents: bytes,
    evidence_receipt_contents: bytes,
    registration_sha256: str,
    reservation_contents: bytes,
) -> bytes:
    canonical_name = (
        _FAILURE_INTENT_NAME if kind == "intent" else _FAILURE_TRANSACTION_NAME
    )
    return _canonical_file_bytes(
        {
            "attempt_number": attempt_number,
            "canonical_name": canonical_name,
            "container_name": container_name,
            "evidence_receipt_sha256": _sha256_bytes(evidence_receipt_contents),
            "kind": kind,
            "marker_sha256": _sha256_bytes(marker_contents),
            "nonce": nonce,
            "payload_name": _FAILURE_CLEANUP_PAYLOAD,
            "query_id": query_id,
            "query_registration_sha256": registration_sha256,
            "reservation_sha256": _sha256_bytes(reservation_contents),
            "schema_version": 1,
        }
    )


def _failure_transaction_bytes(
    evidence_receipt: Mapping[str, object], body: bytes | None
) -> bytes:
    base = {
        "attempt_number": evidence_receipt["attempt_number"],
        "evidence_receipt": dict(evidence_receipt),
        "query_id": evidence_receipt["query_id"],
        "schema_version": 1,
        "staged_body_byte_count": evidence_receipt["byte_count"],
        "staged_body_sha256": evidence_receipt["raw_sha256"],
        "transaction_kind": "FAILURE_EVIDENCE_PUBLICATION",
    }
    header = {**base, "transaction_id": _sha256_bytes(_canonical_json_bytes(base))}
    return _canonical_file_bytes(header)


def _decode_failure_transaction(contents: bytes) -> dict[str, object]:
    header = _json_object(contents, "failure transaction")
    if set(header) != {
        "attempt_number",
        "evidence_receipt",
        "query_id",
        "schema_version",
        "staged_body_byte_count",
        "staged_body_sha256",
        "transaction_id",
        "transaction_kind",
    }:
        raise ValueError("failure transaction keys differ")
    receipt = header["evidence_receipt"]
    if not isinstance(receipt, dict):
        raise ValueError("failure transaction receipt differs")
    base = {key: header[key] for key in header if key != "transaction_id"}
    if (
        header["schema_version"] != 1
        or header["transaction_kind"] != "FAILURE_EVIDENCE_PUBLICATION"
        or header["transaction_id"] != _sha256_bytes(_canonical_json_bytes(base))
        or header["staged_body_sha256"] != receipt.get("raw_sha256")
        or header["staged_body_byte_count"] != receipt.get("byte_count")
    ):
        raise ValueError("failure transaction authentication differs")
    if (
        receipt.get("query_id") != header["query_id"]
        or receipt.get("attempt_number") != header["attempt_number"]
    ):
        raise ValueError("failure transaction receipt binding differs")
    return receipt


def _failure_intent_bytes(
    query: LogicalQuery,
    capture: AttemptCapture,
    evidence_receipt: Mapping[str, object],
    transaction_contents: bytes,
    registration_sha256: str,
    reservation_contents: bytes,
) -> bytes:
    query_id = str(evidence_receipt["query_id"])
    attempt_number = int(evidence_receipt["attempt_number"])
    transaction = _json_object(transaction_contents, "failure transaction")
    return _canonical_file_bytes(
        {
            "attempt_number": attempt_number,
            "body_byte_count": evidence_receipt["byte_count"],
            "body_sha256": evidence_receipt["raw_sha256"],
            "capture_error_classification": capture.error_classification,
            "capture_timed_out": capture.timed_out,
            "exact_reason": evidence_receipt["error_classification"],
            "final_attempt_path": _relative_path(
                query.source, query_id, "attempts", f"{attempt_number:06d}.json"
            ),
            "final_body_path": evidence_receipt["quarantined_body_path"],
            "final_evidence_path": Path(
                "quarantine", query.source, query_id, f"{attempt_number:06d}.json"
            ).as_posix(),
            "intent_kind": "FAILURE_EVIDENCE_PUBLICATION",
            "query_id": query_id,
            "query_registration_path": _relative_path(
                query.source, query_id, "query.json"
            ),
            "query_registration_sha256": registration_sha256,
            "reservation_path": _relative_path(
                query.source,
                query_id,
                "attempts",
                f"{attempt_number:06d}.reservation.json",
            ),
            "reservation_sha256": _sha256_bytes(reservation_contents),
            "schema_version": 1,
            "transaction_marker": transaction,
            "transaction_sha256": _sha256_bytes(transaction_contents),
        }
    )


def _decode_failure_intent(
    contents: bytes, query: LogicalQuery
) -> tuple[dict[str, object], bytes]:
    intent = _json_object(contents, "failure intent")
    expected_keys = {
        "attempt_number",
        "body_byte_count",
        "body_sha256",
        "capture_error_classification",
        "capture_timed_out",
        "exact_reason",
        "final_attempt_path",
        "final_body_path",
        "final_evidence_path",
        "intent_kind",
        "query_id",
        "query_registration_path",
        "query_registration_sha256",
        "reservation_path",
        "reservation_sha256",
        "schema_version",
        "transaction_marker",
        "transaction_sha256",
    }
    if set(intent) != expected_keys:
        raise ValueError("failure intent keys differ")
    marker = intent["transaction_marker"]
    if not isinstance(marker, dict):
        raise ValueError("failure intent transaction marker differs")
    transaction_contents = _canonical_file_bytes(marker)
    receipt = _decode_failure_transaction(transaction_contents)
    query_id = logical_query_id(query)
    attempt_number = intent["attempt_number"]
    if type(attempt_number) is not int or not 1 <= attempt_number <= 999999:
        raise ValueError("failure intent attempt binding differs")
    if set(receipt) != _FAILURE_EVIDENCE_KEYS:
        raise ValueError("failure intent evidence receipt keys differ")
    projected_attempt = {key: receipt[key] for key in _ATTEMPT_KEYS}
    _validate_attempt_record(projected_attempt, query_id, attempt_number)
    evidence_kind = receipt["evidence_kind"]
    if evidence_kind == "TERMINAL_BODYLESS_FAILURE":
        if (
            projected_attempt["outcome"] != "QUARANTINED"
            or projected_attempt["body_sha256"] is not None
            or projected_attempt["byte_count"] != 0
            or receipt["quarantined_body_path"] is not None
            or receipt["raw_sha256"] is not None
        ):
            raise ValueError("failure intent bodyless evidence binding differs")
    elif evidence_kind == "PARTIAL_SPLIT_RESPONSE":
        if (
            projected_attempt["outcome"] != "SPLIT_REQUIRED"
            or projected_attempt["error_classification"] not in _SPLIT_REASONS
        ):
            raise ValueError("failure intent split evidence binding differs")
        raw_sha256 = projected_attempt["body_sha256"]
        if raw_sha256 is None:
            if (
                projected_attempt["error_classification"] != "TIMEOUT"
                or projected_attempt["byte_count"] != 0
                or receipt["quarantined_body_path"] is not None
                or receipt["raw_sha256"] is not None
            ):
                raise ValueError("failure intent timeout evidence binding differs")
        else:
            raw_sha256 = _validate_sha256(
                raw_sha256, "failure intent body SHA-256"
            )
            if (
                receipt["raw_sha256"] != raw_sha256
                or receipt["quarantined_body_path"]
                != Path(
                    "quarantine", query.source, query_id, f"{raw_sha256}.raw"
                ).as_posix()
            ):
                raise ValueError("failure intent body path binding differs")
    else:
        raise ValueError("failure intent evidence kind differs")
    receipt_name = f"{attempt_number:06d}.json"
    reservation_name = f"{attempt_number:06d}.reservation.json"
    expected = {
        "body_byte_count": receipt.get("byte_count"),
        "body_sha256": receipt.get("raw_sha256"),
        "exact_reason": receipt.get("error_classification"),
        "final_attempt_path": _relative_path(
            query.source, query_id, "attempts", receipt_name
        ),
        "final_body_path": receipt.get("quarantined_body_path"),
        "final_evidence_path": Path(
            "quarantine", query.source, query_id, receipt_name
        ).as_posix(),
        "intent_kind": "FAILURE_EVIDENCE_PUBLICATION",
        "query_id": query_id,
        "query_registration_path": _relative_path(
            query.source, query_id, "query.json"
        ),
        "reservation_path": _relative_path(
            query.source, query_id, "attempts", reservation_name
        ),
        "schema_version": 1,
        "transaction_sha256": _sha256_bytes(transaction_contents),
    }
    if any(intent[key] != value for key, value in expected.items()):
        raise ValueError("failure intent semantic binding differs")
    for key in ("query_registration_sha256", "reservation_sha256"):
        _validate_sha256(intent[key], f"failure intent {key}")
    capture_error = intent["capture_error_classification"]
    timed_out = intent["capture_timed_out"]
    reason = intent["exact_reason"]
    if (capture_error is not None and not isinstance(capture_error, str)) or type(
        timed_out
    ) is not bool:
        raise ValueError("failure intent capture binding differs")
    if reason == "TIMEOUT":
        valid_capture = capture_error == "TRANSIENT_NETWORK" and timed_out
    else:
        valid_capture = capture_error == reason and not timed_out
    if not valid_capture:
        raise ValueError("failure intent capture semantics differ")
    return receipt, transaction_contents


def _validate_staged_transaction_body(
    receipt: Mapping[str, object], body: bytes | None
) -> None:
    digest = receipt.get("raw_sha256")
    if digest is None:
        if body is not None:
            raise ValueError("bodyless failure transaction owns body bytes")
        return
    if body is None:
        return
    if (
        _validate_sha256(digest, "failure transaction body SHA-256")
        != _sha256_bytes(body)
        or receipt.get("byte_count") != len(body)
    ):
        raise ValueError("failure transaction body hash or byte count differs")


def _unlink_exact_regular_at(
    parent_fd: int, name: str, expected: bytes, label: str
) -> None:
    if _read_regular_at(parent_fd, name, label) != expected:
        raise ValueError(f"{label} bytes changed before cleanup")
    descriptor = os.open(name, _regular_read_flags(), dir_fd=parent_fd)
    try:
        _cleanup_created_file(parent_fd, descriptor, name, label)
    finally:
        os.close(descriptor)


def _open_or_publish_failure_directory(
    query_fd: int,
    *,
    final_name: str,
    marker_name: str,
    marker_contents: bytes,
    marker_label: str,
    body_name: str | None = None,
    body: bytes | None = None,
    body_label: str = "failure body",
) -> int:
    try:
        descriptor = _open_directory_at(query_fd, final_name, create=False)
    except FileNotFoundError:
        temporary = f".{final_name}.{os.getpid()}.{threading.get_ident()}"
        temporary_fd = -1
        temporary_identity: tuple[int, int] | None = None
        renamed = False
        try:
            os.mkdir(temporary, 0o700, dir_fd=query_fd)
            linked = os.stat(temporary, dir_fd=query_fd, follow_symlinks=False)
            if not stat.S_ISDIR(linked.st_mode):
                raise ValueError(f"temporary {final_name} is not a directory")
            temporary_identity = (linked.st_dev, linked.st_ino)
            temporary_fd = os.open(temporary, _directory_flags(), dir_fd=query_fd)
            opened = os.fstat(temporary_fd)
            if (
                (opened.st_dev, opened.st_ino) != temporary_identity
                or os.listdir(temporary_fd)
            ):
                raise ValueError(f"temporary {final_name} membership is ambiguous")
            os.fsync(query_fd)
            _write_once_at(
                temporary_fd, marker_name, marker_contents, marker_label
            )
            if body_name is not None and body is not None:
                _write_once_at(temporary_fd, body_name, body, body_label)
            os.fsync(temporary_fd)
            os.rename(
                temporary,
                final_name,
                src_dir_fd=query_fd,
                dst_dir_fd=query_fd,
            )
            renamed = True
            os.fsync(query_fd)
            descriptor = temporary_fd
            temporary_fd = -1
        except BaseException:
            if temporary_fd >= 0 and not renamed:
                opened = os.fstat(temporary_fd)
                linked = os.stat(
                    temporary, dir_fd=query_fd, follow_symlinks=False
                )
                if (
                    temporary_identity is None
                    or (opened.st_dev, opened.st_ino) != temporary_identity
                    or (linked.st_dev, linked.st_ino) != temporary_identity
                ):
                    raise ValueError(
                        f"temporary {final_name} ownership changed before cleanup"
                    )
                for name, expected, label in (
                    (body_name, body, body_label),
                    (marker_name, marker_contents, marker_label),
                ):
                    if name is None:
                        continue
                    existing = _read_regular_at(
                        temporary_fd, name, label, missing_ok=True
                    )
                    if existing is None:
                        continue
                    if expected is None or existing != expected:
                        raise ValueError(
                            f"temporary {final_name} ownership changed before cleanup"
                        )
                    os.unlink(name, dir_fd=temporary_fd)
                if os.listdir(temporary_fd):
                    raise ValueError(
                        f"temporary {final_name} membership changed before cleanup"
                    )
                os.fsync(temporary_fd)
                os.rmdir(temporary, dir_fd=query_fd)
                os.fsync(query_fd)
            raise
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
    allowed = {marker_name}
    if body_name is not None and body is not None:
        allowed.add(body_name)
    if set(os.listdir(descriptor)) != allowed:
        os.close(descriptor)
        raise ValueError(f"immutable {final_name} membership differs")
    marker = _read_regular_at(descriptor, marker_name, marker_label)
    if marker != marker_contents:
        os.close(descriptor)
        raise RuntimeError(f"immutable {marker_label} differs")
    if body_name is not None and body is not None:
        existing_body = _read_regular_at(descriptor, body_name, body_label)
        if existing_body != body:
            os.close(descriptor)
            raise RuntimeError(f"immutable {body_label} differs")
    return descriptor


def _rebind_directory_at(
    parent_fd: int, name: str, descriptor: int, label: str
) -> None:
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} directory identity changed") from exc
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
    ):
        raise ValueError(f"{label} directory identity changed")


def _rename_directory_exclusive_at(
    source: str,
    destination: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    encoded_source = os.fsencode(source)
    encoded_destination = os.fsencode(destination)
    if hasattr(library, "renameatx_np"):
        rename_exclusive = library.renameatx_np
        rename_exclusive.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            src_dir_fd,
            encoded_source,
            dst_dir_fd,
            encoded_destination,
            0x00000004,
        )
    elif hasattr(library, "renameat2"):
        rename_exclusive = library.renameat2
        rename_exclusive.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            src_dir_fd,
            encoded_source,
            dst_dir_fd,
            encoded_destination,
            0x00000001,
        )
    else:
        raise OSError(errno.ENOTSUP, "exclusive directory rename is unavailable")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _publish_cleanup_claim_at(
    query_fd: int, claim_name: str, contents: bytes
) -> None:
    temporary = f".cleanup-claim-{os.urandom(16).hex()}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temporary, flags, 0o600, dir_fd=query_fd)
    temporary_exists = True
    try:
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("cleanup claim publication made no progress")
            view = view[written:]
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("cleanup claim temporary file was aliased")
        os.fsync(descriptor)
        if _read_regular_at(query_fd, temporary, "cleanup claim temporary") != contents:
            raise ValueError("cleanup claim temporary bytes differ")
        os.link(
            temporary,
            claim_name,
            src_dir_fd=query_fd,
            dst_dir_fd=query_fd,
            follow_symlinks=False,
        )
        linked_temporary = os.stat(
            temporary, dir_fd=query_fd, follow_symlinks=False
        )
        if _identity(linked_temporary) != _identity(os.fstat(descriptor)):
            raise ValueError("cleanup claim temporary identity changed")
        os.unlink(temporary, dir_fd=query_fd)
        temporary_exists = False
        os.fsync(query_fd)
        if _read_regular_at(query_fd, claim_name, "cleanup claim") != contents:
            raise ValueError("cleanup claim bytes changed after publication")
    finally:
        try:
            if temporary_exists:
                existing = _read_regular_at(
                    query_fd,
                    temporary,
                    "cleanup claim temporary",
                    missing_ok=True,
                )
                if existing is not None:
                    _unlink_exact_regular_at(
                        query_fd,
                        temporary,
                        contents,
                        "cleanup claim temporary",
                    )
        finally:
            os.close(descriptor)


def _claim_failure_cleanup_container(
    query_fd: int,
    canonical_name: str,
    container_name: str,
) -> tuple[str, int]:
    container_fd = -1
    container_identity: tuple[int, int] | None = None
    created = False
    payload_renamed = False
    try:
        os.mkdir(container_name, 0o700, dir_fd=query_fd)
        created = True
        linked = os.stat(
            container_name, dir_fd=query_fd, follow_symlinks=False
        )
        if not stat.S_ISDIR(linked.st_mode):
            raise ValueError("failure cleanup container must be a directory")
        container_identity = _identity(linked)
        container_fd = os.open(
            container_name, _directory_flags(), dir_fd=query_fd
        )
        opened = os.fstat(container_fd)
        if _identity(opened) != container_identity or os.listdir(container_fd):
            raise ValueError("failure cleanup container ownership differs")
        os.fsync(query_fd)
        _rename_directory_exclusive_at(
            canonical_name,
            _FAILURE_CLEANUP_PAYLOAD,
            src_dir_fd=query_fd,
            dst_dir_fd=container_fd,
        )
        payload_renamed = True
        os.fsync(container_fd)
        os.fsync(query_fd)
        return container_name, container_fd
    except BaseException:
        if created and not payload_renamed and container_fd >= 0:
            opened = os.fstat(container_fd)
            try:
                linked = os.stat(
                    container_name,
                    dir_fd=query_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise ValueError(
                    "failure cleanup container ownership changed"
                ) from exc
            membership = set(os.listdir(container_fd))
            try:
                os.stat(canonical_name, dir_fd=query_fd, follow_symlinks=False)
            except FileNotFoundError:
                canonical_exists = False
            else:
                canonical_exists = True
            if membership == {_FAILURE_CLEANUP_PAYLOAD} and not canonical_exists:
                payload_renamed = True
            elif (
                container_identity is None
                or _identity(opened) != container_identity
                or _identity(linked) != container_identity
                or membership
                or not canonical_exists
            ):
                raise ValueError(
                    "failure cleanup container ownership changed"
                )
            else:
                os.rmdir(container_name, dir_fd=query_fd)
                os.fsync(query_fd)
        if container_fd >= 0:
            os.close(container_fd)
            container_fd = -1
        raise
    finally:
        if container_fd >= 0 and not payload_renamed:
            os.close(container_fd)


def _relative_path(source: str, query_id: str, *parts: str) -> str:
    return Path("queries", source, query_id, *parts).as_posix()


def _validate_capture(capture: AttemptCapture) -> None:
    if not isinstance(capture, AttemptCapture):
        raise TypeError("capture must be an AttemptCapture")
    for value, label in (
        (capture.started_at_utc, "started_at_utc"),
        (capture.ended_at_utc, "ended_at_utc"),
    ):
        if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
            raise ValueError(f"{label} must be canonical UTC")
    if capture.final_url is not None:
        _validate_text(capture.final_url, "final_url")
    if capture.http_status is not None and (
        isinstance(capture.http_status, bool)
        or not isinstance(capture.http_status, int)
        or not 100 <= capture.http_status <= 599
    ):
        raise ValueError("http_status must be an HTTP integer or null")
    if capture.body is not None and not isinstance(capture.body, bytes):
        raise ValueError("body must be bytes or null")
    if capture.error_classification is not None:
        _validate_text(capture.error_classification, "error_classification")
    if not isinstance(capture.response_headers, tuple):
        raise ValueError("response_headers must be a tuple")
    for item in capture.response_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not all(isinstance(value, str) for value in item)
        ):
            raise ValueError("response_headers entries must be string pairs")
    if type(capture.timed_out) is not bool:
        raise ValueError("timed_out must be boolean")
    if capture.timed_out:
        has_response_metadata = (
            capture.final_url is not None
            or capture.http_status is not None
            or bool(capture.response_headers)
        )
        closed_response_metadata = (
            capture.final_url is not None and capture.http_status is not None
        )
        if (
            capture.error_classification != "TRANSIENT_NETWORK"
            or (has_response_metadata and not closed_response_metadata)
            or (not has_response_metadata and capture.body is not None)
        ):
            raise ValueError(
                "timed_out requires a transient network capture with closed response metadata"
            )


def _selected_headers(capture: AttemptCapture) -> dict[str, str]:
    selected: dict[str, str] = {}
    for name, value in capture.response_headers:
        canonical = _SELECTED_HEADERS.get(name.lower())
        if canonical is None:
            continue
        if canonical in selected and selected[canonical] != value:
            raise ValueError(f"ambiguous selected response header: {canonical}")
        selected[canonical] = value
    return dict(sorted(selected.items()))


def _parsed_utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"attempt {label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"attempt {label} timestamp is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"attempt {label} timestamp is invalid")
    return parsed


def _validate_attempt_record(
    record: object, expected_query_id: str, expected_number: int
) -> dict[str, object]:
    if not isinstance(record, dict) or set(record) != _ATTEMPT_KEYS:
        raise ValueError("attempt receipt keys differ")
    if record["schema_version"] != 1:
        raise ValueError("attempt receipt schema version differs")
    if record["query_id"] != expected_query_id:
        raise ValueError("attempt receipt query binding differs")
    if (
        type(record["attempt_number"]) is not int
        or record["attempt_number"] != expected_number
    ):
        raise ValueError("attempt receipt number binding differs")
    started = _parsed_utc_timestamp(record["started_at_utc"], "start")
    ended = _parsed_utc_timestamp(record["ended_at_utc"], "end")
    if ended < started:
        raise ValueError("attempt receipt timestamp order differs")
    final_url = record["final_url"]
    if final_url is not None and (type(final_url) is not str or not final_url):
        raise ValueError("attempt receipt final URL differs")
    status = record["http_status"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ValueError("attempt receipt HTTP status differs")
    byte_count = record["byte_count"]
    if (
        type(byte_count) is not int
        or byte_count < 0
        or byte_count > _MAX_RESPONSE_BYTE_COUNT
    ):
        raise ValueError("attempt receipt exceeds the maximum response byte limit")
    body_sha256 = record["body_sha256"]
    if body_sha256 is None:
        if byte_count != 0:
            raise ValueError("attempt receipt body binding differs")
    else:
        _validate_sha256(body_sha256, "attempt body_sha256")
    headers = record["response_headers"]
    if not isinstance(headers, dict) or not set(headers) <= set(
        _SELECTED_HEADERS.values()
    ):
        raise ValueError("attempt receipt selected headers differ")
    if any(
        type(name) is not str or type(value) is not str
        for name, value in headers.items()
    ):
        raise ValueError("attempt receipt selected headers differ")
    content_length = headers.get("Content-Length")
    if content_length is not None and not _ASCII_DECIMAL.fullmatch(content_length):
        raise ValueError("attempt receipt Content-Length syntax differs")
    outcome = record["outcome"]
    if outcome not in _ATTEMPT_OUTCOMES:
        raise ValueError("attempt receipt outcome differs")
    classification = record["error_classification"]
    if classification is not None and (
        type(classification) is not str or not classification
    ):
        raise ValueError("attempt receipt error classification differs")
    if outcome == "SUCCESS":
        if (
            status is None
            or not 200 <= status <= 299
            or final_url is None
            or body_sha256 is None
            or classification is not None
        ):
            raise ValueError("successful attempt branch semantics differ")
    elif outcome == "QUARANTINED":
        if classification is None:
            raise ValueError("quarantined attempt branch semantics differ")
    elif classification is None:
        raise ValueError("failed attempt classification is missing")
    if outcome.endswith("_HTTP") and (status is None or final_url is None):
        raise ValueError("HTTP attempt branch semantics differ")
    if outcome.endswith("_NETWORK") and status is not None and body_sha256 is None:
        raise ValueError("network attempt branch semantics differ")
    return record


def _attempt_record(
    query_id: str,
    attempt_number: int,
    capture: AttemptCapture,
    outcome: str,
    *,
    error_classification: str | None = None,
) -> dict[str, object]:
    _validate_capture(capture)
    _validate_text(outcome, "outcome")
    classification = (
        error_classification
        if error_classification is not None
        else capture.error_classification
    )
    if classification is not None:
        _validate_text(classification, "error_classification")
    body = capture.body
    record = {
        "attempt_number": attempt_number,
        "body_sha256": _sha256_bytes(body) if body is not None else None,
        "byte_count": len(body) if body is not None else 0,
        "ended_at_utc": capture.ended_at_utc,
        "error_classification": classification,
        "final_url": capture.final_url,
        "http_status": capture.http_status,
        "outcome": outcome,
        "query_id": query_id,
        "response_headers": _selected_headers(capture),
        "schema_version": 1,
        "started_at_utc": capture.started_at_utc,
    }
    _validate_attempt_record(record, query_id, attempt_number)
    return record


class V52EvidenceStore:
    @staticmethod
    def _validated_root(root: Path) -> Path:
        supplied = Path(os.path.abspath(os.fspath(root)))
        for node in (*reversed(supplied.parents), supplied):
            try:
                metadata = node.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ValueError("evidence root ancestor is unavailable") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError("evidence root cannot have a symlink ancestor")
            if node != supplied and not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("evidence root ancestors must be directories")
            if node == supplied and not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("evidence root must be a directory")
        if not supplied.parent.is_dir():
            raise ValueError("evidence root parent is unavailable")
        return supplied

    def __init__(self, root: Path) -> None:
        supplied = self._validated_root(root)
        self.root = supplied
        self._canonical_root = supplied
        descriptor = _open_root_path(self._canonical_root, create=True)
        os.close(descriptor)
        self._local = threading.local()
        self._read_only = False

    @classmethod
    def open_read_only(cls, root: Path) -> V52EvidenceStore:
        supplied = cls._validated_root(root)
        descriptor = _open_root_path(supplied, create=False)
        os.close(descriptor)
        store = cls.__new__(cls)
        store.root = supplied
        store._canonical_root = supplied
        store._local = threading.local()
        store._read_only = True
        return store

    @staticmethod
    def _require_no_failure_recovery_state(query_fd: int) -> None:
        try:
            membership = set(os.listdir(query_fd))
        except OSError as exc:
            raise RuntimeError(
                "read-only evidence membership is unavailable"
            ) from exc
        recovery = {
            name
            for name in membership
            if name in {_FAILURE_INTENT_NAME, _FAILURE_TRANSACTION_NAME}
            or name.startswith(_FAILURE_CLEANUP_PREFIX)
        }
        if recovery:
            raise RuntimeError(
                "read-only evidence requires failure recovery"
            )

    def _root_fd(self) -> int:
        return _open_root_path(self._canonical_root, create=False)

    def _query_fds(
        self,
        query: LogicalQuery,
        *,
        create: bool,
        root_descriptor: int | None = None,
    ) -> tuple[str, list[int]]:
        query_id = logical_query_id(query)
        descriptors: list[int] = []
        root_fd = (
            self._root_fd()
            if root_descriptor is None
            else os.dup(root_descriptor)
        )
        descriptors.append(root_fd)
        try:
            queries_fd = _open_directory_at(root_fd, "queries", create=create)
            descriptors.append(queries_fd)
            source_fd = _open_directory_at(queries_fd, query.source, create=create)
            descriptors.append(source_fd)
            query_fd = _open_directory_at(source_fd, query_id, create=create)
            descriptors.append(query_fd)
            return query_id, descriptors
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    def _probe_progress_namespaces(
        self,
        query: LogicalQuery,
    ) -> tuple[list[int] | None, bool, bool]:
        root_fd = self._root_fd()
        query_descriptors: list[int] | None = None
        quarantine_descriptors: list[int] | None = None
        try:
            try:
                _, query_descriptors = self._query_fds(
                    query,
                    create=False,
                    root_descriptor=root_fd,
                )
            except FileNotFoundError:
                query_descriptors = None
            try:
                _, quarantine_descriptors = self._quarantine_fds(
                    query,
                    root_descriptor=root_fd,
                )
            except FileNotFoundError:
                quarantine_descriptors = None
            quarantine_exists = quarantine_descriptors is not None
            if query_descriptors is None:
                try:
                    _, query_descriptors = self._query_fds(
                        query,
                        create=False,
                        root_descriptor=root_fd,
                    )
                except FileNotFoundError:
                    query_descriptors = None
                if query_descriptors is None:
                    repeated_quarantine: list[int] | None = None
                    try:
                        _, repeated_quarantine = self._quarantine_fds(
                            query,
                            root_descriptor=root_fd,
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        quarantine_exists = True
                    finally:
                        if repeated_quarantine is not None:
                            self._close_fds(repeated_quarantine)
            fresh_root = self._root_fd()
            try:
                opened = os.fstat(root_fd)
                fresh = os.fstat(fresh_root)
                stable = (opened.st_dev, opened.st_ino) == (
                    fresh.st_dev,
                    fresh.st_ino,
                )
            finally:
                os.close(fresh_root)
            if quarantine_descriptors is not None:
                self._close_fds(quarantine_descriptors)
                quarantine_descriptors = None
            if not stable:
                if query_descriptors is not None:
                    self._close_fds(query_descriptors)
                return None, False, False
            return query_descriptors, quarantine_exists, True
        except BaseException:
            if quarantine_descriptors is not None:
                self._close_fds(quarantine_descriptors)
            if query_descriptors is not None:
                self._close_fds(query_descriptors)
            raise
        finally:
            os.close(root_fd)

    def _quarantine_fds(
        self,
        query: LogicalQuery,
        *,
        root_descriptor: int | None = None,
    ) -> tuple[str, list[int]]:
        query_id = logical_query_id(query)
        descriptors: list[int] = []
        root_fd = (
            self._root_fd()
            if root_descriptor is None
            else os.dup(root_descriptor)
        )
        descriptors.append(root_fd)
        try:
            quarantine_fd = _open_directory_at(root_fd, "quarantine", create=False)
            descriptors.append(quarantine_fd)
            source_fd = _open_directory_at(
                quarantine_fd, query.source, create=False
            )
            descriptors.append(source_fd)
            query_fd = _open_directory_at(source_fd, query_id, create=False)
            descriptors.append(query_fd)
            return query_id, descriptors
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise

    @staticmethod
    def _close_fds(descriptors: list[int]) -> None:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    def _verify_query_descriptor_chain(
        self,
        source: str,
        query_id: str,
        descriptors: tuple[int, ...] | list[int],
    ) -> None:
        if len(descriptors) != 4:
            raise ValueError("query descriptor chain differs")
        fresh_root = self._root_fd()
        try:
            if (
                os.fstat(fresh_root).st_dev,
                os.fstat(fresh_root).st_ino,
            ) != (
                os.fstat(descriptors[0]).st_dev,
                os.fstat(descriptors[0]).st_ino,
            ):
                raise ValueError("evidence root identity changed")
        finally:
            os.close(fresh_root)
        try:
            for index, name in enumerate(("queries", source, query_id)):
                opened = os.fstat(descriptors[index + 1])
                linked = os.stat(name, dir_fd=descriptors[index], follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError("query directory identity changed")
        except OSError as exc:
            raise ValueError("query directory identity changed") from exc

    def _verify_quarantine_descriptor_chain(
        self,
        source: str,
        query_id: str,
        descriptors: tuple[int, ...] | list[int],
    ) -> None:
        if len(descriptors) != 4:
            raise ValueError("quarantine descriptor chain differs")
        fresh_root = self._root_fd()
        try:
            if (
                os.fstat(fresh_root).st_dev,
                os.fstat(fresh_root).st_ino,
            ) != (
                os.fstat(descriptors[0]).st_dev,
                os.fstat(descriptors[0]).st_ino,
            ):
                raise ValueError("evidence root identity changed")
        finally:
            os.close(fresh_root)
        try:
            for index, name in enumerate(("quarantine", source, query_id)):
                opened = os.fstat(descriptors[index + 1])
                linked = os.stat(name, dir_fd=descriptors[index], follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError("quarantine directory identity changed")
        except OSError as exc:
            raise ValueError("quarantine directory identity changed") from exc

    def _verify_progress_root_stitch(
        self,
        query_descriptors: tuple[int, ...] | list[int],
        quarantine_descriptors: tuple[int, ...] | list[int] | None,
    ) -> None:
        query_root = os.fstat(query_descriptors[0])
        expected = (query_root.st_dev, query_root.st_ino)
        if quarantine_descriptors is not None:
            quarantine_root = os.fstat(quarantine_descriptors[0])
            if (quarantine_root.st_dev, quarantine_root.st_ino) != expected:
                raise ValueError("query and quarantine root generations differ")
        fresh_root = self._root_fd()
        try:
            fresh = os.fstat(fresh_root)
            if (fresh.st_dev, fresh.st_ino) != expected:
                raise ValueError("progress evidence root generation changed")
        finally:
            os.close(fresh_root)

    def _verify_query_chain(self, query: LogicalQuery, descriptors: list[int]) -> None:
        self._verify_query_descriptor_chain(
            query.source, logical_query_id(query), descriptors
        )

    @staticmethod
    def _open_query_lock(
        query_fd: int,
        *,
        create: bool,
        read_only: bool = False,
    ) -> int:
        if create and read_only:
            raise ValueError("read-only query lock cannot be created")
        flags = (
            (os.O_RDONLY if read_only else os.O_RDWR)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = -1
        try:
            if create:
                try:
                    descriptor = os.open(
                        ".query.lock",
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=query_fd,
                    )
                    os.fsync(descriptor)
                    os.fsync(query_fd)
                except FileExistsError:
                    descriptor = os.open(".query.lock", flags, dir_fd=query_fd)
            else:
                descriptor = os.open(".query.lock", flags, dir_fd=query_fd)
            metadata = os.fstat(descriptor)
            linked = os.stat(".query.lock", dir_fd=query_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != 0
                or _identity(metadata) != _identity(linked)
            ):
                raise ValueError("query lease lock must be an empty unaliased file")
            return descriptor
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            raise

    @staticmethod
    def _verify_registered_lock(
        query_fd: int, descriptor: int, lock_identity: Mapping[str, int]
    ) -> None:
        try:
            metadata = os.fstat(descriptor)
            linked = os.stat(".query.lock", dir_fd=query_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("query registration lock identity changed") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != 0
            or _identity(metadata) != _identity(linked)
            or metadata.st_dev != lock_identity["device"]
            or metadata.st_ino != lock_identity["inode"]
        ):
            raise ValueError("query registration lock identity changed")

    def _validated_registered_query(
        self,
        query: LogicalQuery,
        query_fd: int,
        *,
        lock_descriptor: int | None = None,
    ) -> tuple[str, LogicalQuery, _RegularSnapshot]:
        query_id = logical_query_id(query)
        snapshot = _read_regular_snapshot_at(
            query_fd, "query.json", "logical query"
        )
        if snapshot is None:
            raise RuntimeError("logical query is missing")
        contents = snapshot.contents
        registered_query, lock_identity = _decode_registration(contents)
        if canonical_query_bytes(registered_query) != canonical_query_bytes(query):
            raise RuntimeError("registered logical query differs or is ambiguous")
        owned_descriptor = lock_descriptor is None
        if lock_descriptor is None:
            lock_descriptor = self._open_query_lock(query_fd, create=False)
        try:
            self._verify_registered_lock(query_fd, lock_descriptor, lock_identity)
        finally:
            if owned_descriptor:
                os.close(lock_descriptor)
        for name in ("attempts", "objects", "sidecars"):
            descriptor = _open_directory_at(query_fd, name, create=False)
            os.close(descriptor)
        allowed = {
            ".query.lock",
            "attempts",
            "objects",
            "query.json",
            "sidecars",
            "success.json",
            _FAILURE_INTENT_NAME,
            _FAILURE_TRANSACTION_NAME,
        }
        membership = set(os.listdir(query_fd))
        cleanup_members = {
            name for name in membership if name.startswith(_FAILURE_CLEANUP_PREFIX)
        }
        if any(
            _FAILURE_CLEANUP_NAME.fullmatch(name) is None
            and _FAILURE_CLEANUP_CLAIM.fullmatch(name) is None
            for name in cleanup_members
        ):
            raise ValueError("failure cleanup tombstone name is ambiguous")
        if not membership - cleanup_members <= allowed:
            raise ValueError("query directory membership is ambiguous")
        if logical_query_id(registered_query) != query_id:
            raise RuntimeError("registered logical query identity differs")
        return _sha256_bytes(contents), registered_query, snapshot

    def _validate_registered(
        self,
        query: LogicalQuery,
        query_fd: int,
        *,
        lock_descriptor: int | None = None,
    ) -> str:
        return self._validated_registered_query(
            query, query_fd, lock_descriptor=lock_descriptor
        )[0]

    def _finish_failure_cleanup_tombstones(
        self,
        query: LogicalQuery,
        query_fd: int,
        registration_sha256: str,
    ) -> None:
        try:
            query_membership = set(os.listdir(query_fd))
        except OSError as exc:
            raise ValueError("query directory cannot be listed safely") from exc
        cleanup_names = sorted(
            name
            for name in query_membership
            if name.startswith(_FAILURE_CLEANUP_PREFIX)
        )
        if not cleanup_names:
            return
        parsed: dict[str, tuple[str, int, str]] = {}
        claims: dict[str, tuple[str, int, str, bytes]] = {}
        for name in cleanup_names:
            claim_match = _FAILURE_CLEANUP_CLAIM.fullmatch(name)
            if claim_match is not None:
                kind = claim_match.group("kind")
                if kind in claims:
                    raise ValueError("failure cleanup claims conflict")
                claims[kind] = (
                    name,
                    int(claim_match.group("number")),
                    claim_match.group("nonce"),
                    _read_regular_at(query_fd, name, "failure cleanup claim"),
                )
                continue
            match = _FAILURE_CLEANUP_NAME.fullmatch(name)
            if match is None:
                raise ValueError("failure cleanup tombstone name is ambiguous")
            kind = match.group("kind")
            if kind in parsed:
                raise ValueError("failure cleanup tombstones conflict")
            parsed[kind] = (
                name,
                int(match.group("number")),
                match.group("sha256"),
            )
        if not claims or not set(parsed) <= set(claims):
            raise ValueError("failure cleanup container has no immutable claim")
        attempt_numbers = {
            item[1] for item in (*parsed.values(), *claims.values())
        }
        if len(attempt_numbers) != 1:
            raise ValueError("failure cleanup attempt bindings conflict")
        attempt_number = next(iter(attempt_numbers))

        attempts_fd = _open_directory_at(query_fd, "attempts", create=False)
        quarantine_descriptors: list[int] | None = None
        opened_tombstones: dict[str, tuple[str, int, int, bytes]] = {}
        canonical_intent_fd = -1
        try:
            _, _, attempt_receipts, _ = self._verified_attempt_bundle(
                query, attempts_fd
            )
            committed = next(
                (
                    dict(record)
                    for record in attempt_receipts
                    if record.get("attempt_number") == attempt_number
                ),
                None,
            )
            if committed is None or committed.get("outcome") not in {
                "QUARANTINED",
                "SPLIT_REQUIRED",
            }:
                raise ValueError(
                    "failure cleanup has no committed terminal attempt"
                )
            _, quarantine_descriptors = self._quarantine_fds(query)
            quarantine_fd = quarantine_descriptors[-1]
            self._verified_quarantine_bundle(
                query, quarantine_fd, attempt_receipts
            )
            receipt_name = f"{attempt_number:06d}.json"
            evidence_contents = _read_regular_at(
                quarantine_fd,
                receipt_name,
                "failure cleanup evidence receipt",
            )
            evidence_receipt = _json_object(
                evidence_contents, "failure cleanup evidence receipt"
            )
            if (
                set(evidence_receipt) != _FAILURE_EVIDENCE_KEYS
                or {
                    key: evidence_receipt[key] for key in _ATTEMPT_KEYS
                }
                != committed
            ):
                raise ValueError("failure cleanup evidence binding differs")
            transaction_contents = _failure_transaction_bytes(
                evidence_receipt, None
            )
            reason = evidence_receipt["error_classification"]
            if reason == "TIMEOUT":
                capture_error = "TRANSIENT_NETWORK"
                capture_timed_out = True
            else:
                capture_error = reason
                capture_timed_out = False
            reconstructed_capture = AttemptCapture(
                started_at_utc=str(evidence_receipt["started_at_utc"]),
                ended_at_utc=str(evidence_receipt["ended_at_utc"]),
                final_url=None,
                http_status=None,
                response_headers=(),
                body=None,
                error_classification=str(capture_error),
                timed_out=capture_timed_out,
            )
            reservation_contents = _read_regular_at(
                attempts_fd,
                f"{attempt_number:06d}.reservation.json",
                "failure cleanup attempt reservation",
            )
            intent_contents = _failure_intent_bytes(
                query,
                reconstructed_capture,
                evidence_receipt,
                transaction_contents,
                registration_sha256,
                reservation_contents,
            )
            expected = {
                "intent": (_FAILURE_INTENT_MARKER, intent_contents),
                "transaction": (
                    _FAILURE_TRANSACTION_MARKER,
                    transaction_contents,
                ),
            }
            if "intent" in claims and "transaction" not in claims:
                raise ValueError("failure cleanup transaction claim is missing")
            if "transaction" in claims and "intent" not in claims:
                nonce = os.urandom(16).hex()
                intent_container_name = _failure_cleanup_tombstone_name(
                    "intent", attempt_number, intent_contents, nonce=nonce
                )
                intent_claim_name = _failure_cleanup_claim_name(
                    "intent", attempt_number, nonce
                )
                intent_claim_contents = _failure_cleanup_claim_bytes(
                    kind="intent",
                    query_id=logical_query_id(query),
                    attempt_number=attempt_number,
                    nonce=nonce,
                    container_name=intent_container_name,
                    marker_contents=intent_contents,
                    evidence_receipt_contents=evidence_contents,
                    registration_sha256=registration_sha256,
                    reservation_contents=reservation_contents,
                )
                _publish_cleanup_claim_at(
                    query_fd, intent_claim_name, intent_claim_contents
                )
                claims["intent"] = (
                    intent_claim_name,
                    attempt_number,
                    nonce,
                    intent_claim_contents,
                )
            claim_bindings: dict[str, tuple[str, str, bytes]] = {}
            for kind, (claim_name, _, nonce, claim_contents) in claims.items():
                marker_name, marker_contents = expected[kind]
                del marker_name
                container_name = _failure_cleanup_tombstone_name(
                    kind,  # type: ignore[arg-type]
                    attempt_number,
                    marker_contents,
                    nonce=nonce,
                )
                expected_claim = _failure_cleanup_claim_bytes(
                    kind=kind,  # type: ignore[arg-type]
                    query_id=logical_query_id(query),
                    attempt_number=attempt_number,
                    nonce=nonce,
                    container_name=container_name,
                    marker_contents=marker_contents,
                    evidence_receipt_contents=evidence_contents,
                    registration_sha256=registration_sha256,
                    reservation_contents=reservation_contents,
                )
                if claim_contents != expected_claim:
                    raise ValueError("failure cleanup claim binding differs")
                claim_bindings[kind] = (
                    claim_name,
                    container_name,
                    claim_contents,
                )
            for kind in ("transaction", "intent"):
                if kind in parsed:
                    continue
                canonical_name = (
                    _FAILURE_TRANSACTION_NAME
                    if kind == "transaction"
                    else _FAILURE_INTENT_NAME
                )
                if canonical_name not in query_membership:
                    continue
                marker_name, marker_contents = expected[kind]
                canonical_fd = _open_directory_at(
                    query_fd, canonical_name, create=False
                )
                try:
                    if set(os.listdir(canonical_fd)) != {marker_name}:
                        raise ValueError(
                            "failure cleanup canonical membership differs"
                        )
                    if _read_regular_at(
                        canonical_fd,
                        marker_name,
                        "failure cleanup canonical marker",
                    ) != marker_contents:
                        raise ValueError(
                            "failure cleanup canonical marker differs"
                        )
                    container_name = claim_bindings[kind][1]
                    _, container_fd = _claim_failure_cleanup_container(
                        query_fd, canonical_name, container_name
                    )
                    try:
                        _rebind_directory_at(
                            container_fd,
                            _FAILURE_CLEANUP_PAYLOAD,
                            canonical_fd,
                            f"failure {kind} cleanup payload",
                        )
                    finally:
                        os.close(container_fd)
                finally:
                    os.close(canonical_fd)
                parsed[kind] = (
                    container_name,
                    attempt_number,
                    _sha256_bytes(marker_contents),
                )
                query_membership.remove(canonical_name)
                query_membership.add(container_name)
            for kind, (name, _, digest) in parsed.items():
                marker_name, marker_contents = expected[kind]
                if (
                    name != claim_bindings[kind][1]
                    or digest != _sha256_bytes(marker_contents)
                ):
                    raise ValueError("failure cleanup tombstone binding differs")
                with ExitStack() as provisional:
                    container_fd = _open_directory_at(
                        query_fd, name, create=False
                    )
                    provisional.callback(os.close, container_fd)
                    container_membership = set(os.listdir(container_fd))
                    if not container_membership <= {_FAILURE_CLEANUP_PAYLOAD}:
                        raise ValueError(
                            "failure cleanup tombstone membership differs"
                        )
                    payload_fd = -1
                    if _FAILURE_CLEANUP_PAYLOAD in container_membership:
                        payload_fd = _open_directory_at(
                            container_fd,
                            _FAILURE_CLEANUP_PAYLOAD,
                            create=False,
                        )
                        provisional.callback(os.close, payload_fd)
                        membership = set(os.listdir(payload_fd))
                        if not membership <= {marker_name}:
                            raise ValueError(
                                "failure cleanup payload membership differs"
                            )
                    else:
                        membership = set()
                        canonical_name = (
                            _FAILURE_TRANSACTION_NAME
                            if kind == "transaction"
                            else _FAILURE_INTENT_NAME
                        )
                        if canonical_name in query_membership:
                            payload_fd = _open_directory_at(
                                query_fd, canonical_name, create=False
                            )
                            provisional.callback(os.close, payload_fd)
                            if (
                                set(os.listdir(payload_fd)) != {marker_name}
                                or _read_regular_at(
                                    payload_fd,
                                    marker_name,
                                    "failure cleanup canonical marker",
                                )
                                != marker_contents
                            ):
                                raise ValueError(
                                    "failure cleanup canonical binding differs"
                                )
                            _rename_directory_exclusive_at(
                                canonical_name,
                                _FAILURE_CLEANUP_PAYLOAD,
                                src_dir_fd=query_fd,
                                dst_dir_fd=container_fd,
                            )
                            os.fsync(container_fd)
                            os.fsync(query_fd)
                            _rebind_directory_at(
                                container_fd,
                                _FAILURE_CLEANUP_PAYLOAD,
                                payload_fd,
                                f"failure {kind} cleanup payload",
                            )
                            membership = {marker_name}
                            query_membership.remove(canonical_name)
                    if marker_name in membership:
                        marker_snapshot = _read_regular_snapshot_at(
                            payload_fd,
                            marker_name,
                            f"failure {kind} cleanup marker",
                        )
                        if (
                            marker_snapshot is None
                            or marker_snapshot.contents != marker_contents
                        ):
                            raise ValueError(
                                "failure cleanup marker binding differs"
                            )
                        _rebind_regular_snapshot_at(
                            payload_fd,
                            marker_name,
                            marker_snapshot,
                            f"failure {kind} cleanup marker",
                        )
                    opened_tombstones[kind] = (
                        name,
                        container_fd,
                        payload_fd,
                        marker_contents,
                    )
                    provisional.pop_all()

            has_canonical_intent = _FAILURE_INTENT_NAME in query_membership
            has_canonical_transaction = _FAILURE_TRANSACTION_NAME in query_membership
            if has_canonical_transaction or (
                "intent" in parsed and has_canonical_intent
            ):
                raise ValueError("failure cleanup canonical membership conflicts")
            if "transaction" in parsed and "intent" not in parsed:
                transaction_payload_fd = opened_tombstones["transaction"][2]
                if not has_canonical_intent or transaction_payload_fd < 0:
                    raise ValueError("failure cleanup intent transition is missing")
                canonical_intent_fd = _open_directory_at(
                    query_fd, _FAILURE_INTENT_NAME, create=False
                )
                if set(os.listdir(canonical_intent_fd)) != {_FAILURE_INTENT_MARKER}:
                    raise ValueError("failure intent cleanup membership differs")
                canonical_snapshot = _read_regular_snapshot_at(
                    canonical_intent_fd,
                    _FAILURE_INTENT_MARKER,
                    "failure intent cleanup marker",
                )
                if canonical_snapshot is None or canonical_snapshot.contents != intent_contents:
                    raise ValueError("failure intent cleanup marker differs")
                _rebind_regular_snapshot_at(
                    canonical_intent_fd,
                    _FAILURE_INTENT_MARKER,
                    canonical_snapshot,
                    "failure intent cleanup marker",
                )
            elif "intent" not in parsed and has_canonical_intent:
                raise ValueError("failure cleanup tombstone state differs")
            elif has_canonical_intent:
                raise ValueError("failure cleanup intent membership conflicts")

            for kind in ("transaction", "intent"):
                item = opened_tombstones.get(kind)
                if item is None:
                    continue
                name, container_fd, payload_fd, marker_contents = item
                marker_name = expected[kind][0]
                _rebind_directory_at(
                    query_fd,
                    name,
                    container_fd,
                    f"failure {kind} cleanup container",
                )
                if payload_fd >= 0:
                    _rebind_directory_at(
                        container_fd,
                        _FAILURE_CLEANUP_PAYLOAD,
                        payload_fd,
                        f"failure {kind} cleanup payload",
                    )
                    membership = set(os.listdir(payload_fd))
                    if marker_name in membership:
                        _unlink_exact_regular_at(
                            payload_fd,
                            marker_name,
                            marker_contents,
                            f"failure {kind} cleanup marker",
                        )
                    if os.listdir(payload_fd):
                        raise ValueError(
                            "failure cleanup payload remains nonempty"
                        )
                    os.rmdir(
                        _FAILURE_CLEANUP_PAYLOAD,
                        dir_fd=container_fd,
                    )
                    os.fsync(container_fd)
                if os.listdir(container_fd):
                    raise ValueError("failure cleanup tombstone remains nonempty")
                os.rmdir(name, dir_fd=query_fd)
                os.fsync(query_fd)
            for kind in ("intent", "transaction"):
                claim_name, _, claim_contents = claim_bindings[kind]
                _unlink_exact_regular_at(
                    query_fd,
                    claim_name,
                    claim_contents,
                    f"failure {kind} cleanup claim",
                )
        finally:
            if canonical_intent_fd >= 0:
                os.close(canonical_intent_fd)
            for _, container_fd, payload_fd, _ in opened_tombstones.values():
                if payload_fd >= 0:
                    os.close(payload_fd)
                os.close(container_fd)
            if quarantine_descriptors is not None:
                self._close_fds(quarantine_descriptors)
            os.close(attempts_fd)

    def _finish_failure_cleanup_before_read(
        self,
        query: LogicalQuery,
        descriptors: list[int],
    ) -> None:
        query_fd = descriptors[-1]
        lock_descriptor = self._open_query_lock(query_fd, create=False)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            registration_sha256, _, registration_snapshot = (
                self._validated_registered_query(
                    query, query_fd, lock_descriptor=lock_descriptor
                )
            )
            self._finish_failure_cleanup_tombstones(
                query, query_fd, registration_sha256
            )
            _rebind_regular_snapshot_at(
                query_fd,
                "query.json",
                registration_snapshot,
                "logical query registration",
            )
            self._verify_query_chain(query, descriptors)
        finally:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def register_query(self, query: LogicalQuery) -> str:
        query_id, descriptors = self._query_fds(query, create=True)
        try:
            query_fd = descriptors[-1]
            for name in ("attempts", "objects", "sidecars"):
                descriptor = _open_directory_at(query_fd, name, create=True)
                os.close(descriptor)
            lock_descriptor = self._open_query_lock(query_fd, create=True)
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                try:
                    lock_metadata = os.fstat(lock_descriptor)
                    lock_identity = {
                        "device": lock_metadata.st_dev,
                        "inode": lock_metadata.st_ino,
                    }
                    self._verify_registered_lock(
                        query_fd, lock_descriptor, lock_identity
                    )
                    _write_once_at(
                        query_fd,
                        "query.json",
                        _registration_contents(query, lock_metadata),
                        "logical query registration",
                    )
                    (
                        registration_sha256,
                        registered_query,
                        registration_snapshot,
                    ) = self._validated_registered_query(
                        query, query_fd, lock_descriptor=lock_descriptor
                    )
                    self._verify_registered_lock(
                        query_fd, lock_descriptor, lock_identity
                    )
                    self._finish_failure_cleanup_tombstones(
                        query, query_fd, registration_sha256
                    )
                    (
                        final_registration_sha256,
                        final_registered_query,
                        final_registration_snapshot,
                    ) = self._validated_registered_query(
                        query, query_fd, lock_descriptor=lock_descriptor
                    )
                    if (
                        final_registration_sha256 != registration_sha256
                        or final_registered_query != registered_query
                        or final_registration_snapshot != registration_snapshot
                    ):
                        raise ValueError(
                            "logical query registration changed before return"
                        )
                    self._verify_query_chain(query, descriptors)
                    _rebind_regular_snapshot_at(
                        query_fd,
                        "query.json",
                        registration_snapshot,
                        "logical query registration",
                    )
                finally:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)
            return query_id
        finally:
            self._close_fds(descriptors)

    def _active_leases(self) -> dict[str, QueryLease]:
        leases = getattr(self._local, "leases", None)
        if leases is None:
            leases = {}
            self._local.leases = leases
        return leases

    def _require_lease(self, query: LogicalQuery) -> QueryLease:
        query_id = logical_query_id(query)
        lease = self._active_leases().get(query_id)
        if (
            lease is None
            or lease._store_identity != id(self)
            or lease._owner_thread != threading.get_ident()
        ):
            raise RuntimeError("operation requires the active per-query lease")
        self._verify_lease_binding(lease)
        return lease

    def _verify_lease_binding(self, lease: QueryLease) -> None:
        self._verify_query_descriptor_chain(
            lease.source, lease.query_id, lease._directory_descriptors
        )
        query_fd = lease._directory_descriptors[-1]
        contents = _read_regular_at(query_fd, "query.json", "logical query")
        query, lock_identity = _decode_registration(contents)
        if (
            _sha256_bytes(contents) != lease._registration_sha256
            or logical_query_id(query) != lease.query_id
            or query.source != lease.source
        ):
            raise ValueError("query lease registration identity changed")
        self._verify_registered_lock(query_fd, lease._descriptor, lock_identity)

    def _ensure_unpublished(
        self, query: LogicalQuery, *, authority: object | None = None
    ) -> None:
        if self.load_success(query, authority=authority) is not None:
            raise RuntimeError("query success is already published and terminal")

    def acquire_query(self, query: LogicalQuery) -> ContextManager[QueryLease]:
        return self._acquire_query_context(query)

    @contextmanager
    def _acquire_query_context(self, query: LogicalQuery) -> Iterator[QueryLease]:
        active = self._active_leases()
        if active:
            raise RuntimeError("query lease is not reentrant")
        query_id = self.register_query(query)
        _, descriptors = self._query_fds(query, create=False)
        query_fd = descriptors[-1]
        lock_descriptor = -1
        try:
            lock_descriptor = self._open_query_lock(query_fd, create=False)
            registration_sha256 = self._validate_registered(
                query, query_fd, lock_descriptor=lock_descriptor
            )
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            if (
                self._validate_registered(
                    query, query_fd, lock_descriptor=lock_descriptor
                )
                != registration_sha256
            ):
                raise ValueError("query lease registration identity changed")
            lease = QueryLease(
                query_id,
                query.source,
                id(self),
                lock_descriptor,
                threading.get_ident(),
                tuple(descriptors),
                registration_sha256,
            )
            self._verify_lease_binding(lease)
            active[query_id] = lease
            try:
                yield lease
            finally:
                active.pop(query_id, None)
                try:
                    self._verify_lease_binding(lease)
                finally:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            if lock_descriptor >= 0:
                os.close(lock_descriptor)
            self._close_fds(descriptors)

    def _attempts_fd(self, query: LogicalQuery) -> tuple[int, list[int]]:
        _, descriptors = self._query_fds(query, create=False)
        try:
            self._validate_registered(query, descriptors[-1])
            attempts_fd = _open_directory_at(descriptors[-1], "attempts", create=False)
            return attempts_fd, descriptors
        except BaseException:
            self._close_fds(descriptors)
            raise

    def _attempt_numbers(
        self, attempts_fd: int, expected_query_id: str
    ) -> tuple[set[int], set[int]]:
        reservations: set[int] = set()
        receipts: set[int] = set()
        try:
            names = os.listdir(attempts_fd)
        except OSError as exc:
            raise ValueError("attempt directory cannot be listed safely") from exc
        for name in names:
            reservation = _RESERVATION_NAME.fullmatch(name)
            receipt = _ATTEMPT_NAME.fullmatch(name)
            if reservation:
                number = int(reservation.group("number"))
                value = _json_object(
                    _read_regular_at(attempts_fd, name, "attempt reservation"),
                    "attempt reservation",
                )
                if value != {
                    "attempt_number": number,
                    "query_id": value.get("query_id"),
                    "schema_version": 1,
                }:
                    raise ValueError("attempt reservation keys differ")
                if value.get("query_id") != expected_query_id:
                    raise ValueError("attempt reservation query binding differs")
                reservations.add(number)
            elif receipt:
                number = int(receipt.group("number"))
                record = _json_object(
                    _read_regular_at(attempts_fd, name, "attempt receipt"),
                    "attempt receipt",
                )
                _validate_attempt_record(record, expected_query_id, number)
                receipts.add(number)
            else:
                raise ValueError("attempt directory contains an ambiguous entry")
        if not receipts <= reservations:
            raise ValueError("attempt receipt has no immutable reservation")
        if reservations and reservations != set(range(1, max(reservations) + 1)):
            raise ValueError("attempt reservation order has a gap")
        return reservations, receipts

    def next_attempt_number(self, lease: QueryLease) -> int:
        if not isinstance(lease, QueryLease):
            raise TypeError("lease must be a QueryLease")
        active = self._active_leases().get(lease.query_id)
        if active is not lease or lease._owner_thread != threading.get_ident():
            raise RuntimeError("attempt reservation requires the active query lease")
        self._verify_lease_binding(lease)
        query = self._query_from_registered_lease(lease)
        if self._recover_pending_before_mutation(query) is not None:
            raise RuntimeError(
                "pending failure transaction recovered; attempt reservation refused"
            )
        self._ensure_unpublished(query)
        attempts_fd, descriptors = self._attempts_fd(query)
        try:
            reservations, receipts = self._attempt_numbers(attempts_fd, lease.query_id)
            if receipts - reservations:
                raise ValueError("attempt numbering is inconsistent")
            number = max(reservations | receipts, default=0) + 1
            if number > 999999:
                raise RuntimeError("attempt number space is exhausted")
            record = {
                "attempt_number": number,
                "query_id": lease.query_id,
                "schema_version": 1,
            }
            _write_once_at(
                attempts_fd,
                f"{number:06d}.reservation.json",
                _canonical_file_bytes(record),
                "attempt reservation",
            )
            self._require_lease(query)
            return number
        finally:
            os.close(attempts_fd)
            self._close_fds(descriptors)

    def read_leased_attempt_budget(
        self, lease: QueryLease
    ) -> AttemptBudgetSnapshot:
        if not isinstance(lease, QueryLease):
            raise TypeError("lease must be a QueryLease")
        active = self._active_leases().get(lease.query_id)
        if active is not lease or lease._owner_thread != threading.get_ident():
            raise RuntimeError("attempt budget read requires the active query lease")
        self._verify_lease_binding(lease)
        query = self._query_from_registered_lease(lease)
        attempts_fd, descriptors = self._attempts_fd(query)
        snapshot: AttemptBudgetSnapshot
        try:
            reservations, history, records, artifact_snapshots = (
                self._verified_attempt_bundle(query, attempts_fd)
            )
            expected_members = tuple(
                sorted(
                    (
                        *(f"{number:06d}.reservation.json" for number in reservations),
                        *(f"{int(item['attempt_number']):06d}.json" for item in history),
                    )
                )
            )
            if tuple(sorted(os.listdir(attempts_fd))) != expected_members:
                raise ValueError("attempt budget membership changed while reading")
            for name, artifact_snapshot in artifact_snapshots:
                _rebind_regular_snapshot_at(
                    attempts_fd,
                    name,
                    artifact_snapshot,
                    "attempt budget artifact",
                )
            (
                final_reservations,
                final_history,
                final_records,
                final_artifact_snapshots,
            ) = self._verified_attempt_bundle(query, attempts_fd)
            final_members = tuple(sorted(os.listdir(attempts_fd)))
            if (
                final_reservations != reservations
                or final_history != history
                or final_records != records
                or final_artifact_snapshots != artifact_snapshots
                or final_members != expected_members
            ):
                raise ValueError("attempt budget changed during final replay")
            transient_ordinal = sum(
                record.get("outcome") in {"TRANSIENT_HTTP", "TRANSIENT_NETWORK"}
                for record in records
            )
            latest = records[-1] if records else None
            latest_receipt = None
            if latest is not None:
                headers = latest.get("response_headers")
                if not isinstance(headers, Mapping):
                    raise ValueError("attempt budget latest headers differ")
                outcome = latest.get("outcome")
                classification = latest.get("error_classification")
                ended_at_utc = latest.get("ended_at_utc")
                attempt_number = latest.get("attempt_number")
                if (
                    not isinstance(outcome, str)
                    or classification is not None
                    and not isinstance(classification, str)
                    or not isinstance(ended_at_utc, str)
                    or type(attempt_number) is not int
                ):
                    raise ValueError("attempt budget latest receipt differs")
                retryable = outcome in {"TRANSIENT_HTTP", "TRANSIENT_NETWORK"}
                retry_after = headers.get("Retry-After")
                if retry_after is not None and not isinstance(retry_after, str):
                    raise ValueError("attempt budget Retry-After differs")
                latest_receipt = AttemptRetryState(
                    attempt_number=attempt_number,
                    retry_ordinal=transient_ordinal if retryable else None,
                    outcome=outcome,
                    error_classification=classification,
                    ended_at_utc=ended_at_utc,
                    retry_after=retry_after,
                )
            snapshot = AttemptBudgetSnapshot(
                reservation_numbers=tuple(sorted(reservations)),
                receipt_bindings=tuple(
                    (
                        int(item["attempt_number"]),
                        str(item["path"]),
                        str(item["sha256"]),
                    )
                    for item in history
                ),
                latest_receipt=latest_receipt,
            )
        finally:
            os.close(attempts_fd)
            self._close_fds(descriptors)
        self._require_lease(query)
        return snapshot

    def _query_from_registered_lease(self, lease: QueryLease) -> LogicalQuery:
        root_fd = self._root_fd()
        descriptors = [root_fd]
        try:
            for name in ("queries", lease.source, lease.query_id):
                child = _open_directory_at(descriptors[-1], name, create=False)
                descriptors.append(child)
            contents = _read_regular_at(
                descriptors[-1], "query.json", "logical query registration"
            )
            query, _ = _decode_registration(contents)
            if (
                _sha256_bytes(contents) != lease._registration_sha256
                or logical_query_id(query) != lease.query_id
            ):
                raise RuntimeError("leased logical query identity differs")
            return query
        finally:
            self._close_fds(descriptors)

    def _validate_attempt_reservation(
        self, query: LogicalQuery, attempt_number: int, attempts_fd: int
    ) -> None:
        if (
            isinstance(attempt_number, bool)
            or not isinstance(attempt_number, int)
            or not 1 <= attempt_number <= 999999
        ):
            raise ValueError("attempt_number must be a positive integer")
        reservation = _json_object(
            _read_regular_at(
                attempts_fd,
                f"{attempt_number:06d}.reservation.json",
                "attempt reservation",
            ),
            "attempt reservation",
        )
        expected = {
            "attempt_number": attempt_number,
            "query_id": logical_query_id(query),
            "schema_version": 1,
        }
        if reservation != expected:
            raise ValueError("attempt reservation binding differs")

    def record_attempt(
        self,
        query: LogicalQuery,
        attempt_number: int,
        capture: AttemptCapture,
        *,
        outcome: str,
    ) -> Path:
        if outcome in {"QUARANTINED", "SPLIT_REQUIRED"}:
            raise ValueError(
                f"{outcome} must use its dedicated evidence persistence API"
            )
        self._require_lease(query)
        if self._recover_pending_before_mutation(query) is not None:
            raise RuntimeError(
                "pending failure transaction recovered; generic attempt mutation refused"
            )
        self._ensure_unpublished(query)
        record = _attempt_record(
            logical_query_id(query), attempt_number, capture, outcome
        )
        _validate_query_byte_count(query, record["byte_count"])
        attempts_fd, descriptors = self._attempts_fd(query)
        try:
            self._validate_attempt_reservation(query, attempt_number, attempts_fd)
            self._verified_attempt_bundle(query, attempts_fd)
            name = f"{attempt_number:06d}.json"
            _write_once_at(
                attempts_fd,
                name,
                _canonical_file_bytes(record),
                "attempt receipt",
            )
            self._require_lease(query)
            return self.root / _relative_path(
                query.source, logical_query_id(query), "attempts", name
            )
        finally:
            os.close(attempts_fd)
            self._close_fds(descriptors)

    def quarantine_attempt(
        self,
        query: LogicalQuery,
        attempt_number: int,
        capture: AttemptCapture,
        *,
        error_classification: str,
    ) -> Path:
        self._require_lease(query)
        if self._recover_pending_before_mutation(query) is not None:
            raise RuntimeError(
                "pending failure transaction recovered; quarantine mutation refused"
            )
        self._ensure_unpublished(query)
        _validate_text(error_classification, "error_classification")
        _validate_capture(capture)
        if capture.body is None:
            raise ValueError("a quarantined attempt must own body bytes")
        query_id = logical_query_id(query)
        body = capture.body
        digest = _sha256_bytes(body)
        attempt_record = _attempt_record(
            query_id,
            attempt_number,
            capture,
            "QUARANTINED",
            error_classification=error_classification,
        )
        body_limit = _validate_query_byte_count(query, len(body))
        attempts_fd, attempt_descriptors = self._attempts_fd(query)
        quarantine_descriptors: list[int] | None = None
        try:
            self._validate_attempt_reservation(query, attempt_number, attempts_fd)
            _, _, attempt_receipts, _ = self._verified_attempt_bundle(
                query, attempts_fd
            )
            quarantined_attempts = tuple(
                record
                for record in attempt_receipts
                if record.get("outcome") == "QUARANTINED"
            )
            try:
                _, quarantine_descriptors = self._quarantine_fds(
                    query,
                    root_descriptor=attempt_descriptors[0],
                )
            except FileNotFoundError:
                if quarantined_attempts:
                    raise RuntimeError("quarantine directory is missing") from None
            existing_bodies: dict[str, int] = {}
            if quarantine_descriptors is not None:
                verified, _, _, _ = self._verified_quarantine_bundle(
                    query,
                    quarantine_descriptors[-1],
                    attempt_receipts,
                )
                for item in verified:
                    prior = existing_bodies.setdefault(
                        item.raw_sha256, item.raw_byte_count
                    )
                    if prior != item.raw_byte_count:
                        raise ValueError("shared quarantine body byte count differs")
            projected_byte_count = sum(existing_bodies.values())
            if digest in existing_bodies:
                if existing_bodies[digest] != len(body):
                    raise ValueError("shared quarantine body byte count differs")
            else:
                projected_byte_count += len(body)
            if (
                projected_byte_count
                > body_limit * _MAX_QUARANTINE_BODIES_PER_QUERY
            ):
                raise ValueError("quarantine aggregate response byte limit exceeded")

            if quarantine_descriptors is None:
                quarantine_descriptors = [os.dup(attempt_descriptors[0])]
                for name in ("quarantine", query.source, query_id):
                    child = _open_directory_at(
                        quarantine_descriptors[-1], name, create=True
                    )
                    quarantine_descriptors.append(child)
            quarantine_fd = quarantine_descriptors[-1]
            body_name = f"{digest}.raw"
            if digest not in existing_bodies:
                _write_once_at(
                    quarantine_fd,
                    body_name,
                    body,
                    "quarantined response body",
                )
            receipt = {
                **attempt_record,
                "quarantined_body_path": Path(
                    "quarantine", query.source, query_id, body_name
                ).as_posix(),
                "raw_sha256": digest,
            }
            receipt_name = f"{attempt_number:06d}.json"
            _write_once_at(
                quarantine_fd,
                receipt_name,
                _canonical_file_bytes(receipt),
                "quarantine receipt",
            )
            _write_once_at(
                attempts_fd,
                receipt_name,
                _canonical_file_bytes(attempt_record),
                "attempt receipt",
            )
            self._require_lease(query)
        finally:
            if quarantine_descriptors is not None:
                self._close_fds(quarantine_descriptors)
            os.close(attempts_fd)
            self._close_fds(attempt_descriptors)
        return self.root / "quarantine" / query.source / query_id / receipt_name

    def _persist_failure_evidence(
        self,
        query: LogicalQuery,
        attempt_number: int,
        capture: AttemptCapture,
        *,
        outcome: Literal["QUARANTINED", "SPLIT_REQUIRED"],
        error_classification: str,
        evidence_kind: Literal[
            "TERMINAL_BODYLESS_FAILURE", "PARTIAL_SPLIT_RESPONSE"
        ],
    ) -> PersistedAttemptEvidence:
        self._require_lease(query)
        _validate_text(error_classification, "error_classification")
        _validate_capture(capture)
        body = capture.body
        if evidence_kind == "TERMINAL_BODYLESS_FAILURE":
            if outcome != "QUARANTINED" or body is not None:
                raise ValueError("terminal bodyless evidence must not own body bytes")
        elif outcome != "SPLIT_REQUIRED" or error_classification not in _SPLIT_REASONS:
            raise ValueError("partial split evidence has invalid branch semantics")
        elif error_classification == "TIMEOUT" and (
            not capture.timed_out
            or capture.error_classification != "TRANSIENT_NETWORK"
        ):
            raise ValueError("TIMEOUT split requires a timed_out transport capture")
        elif body is None and error_classification != "TIMEOUT":
            raise ValueError("SIZE_LIMIT and ROW_CAP split evidence require body bytes")
        elif error_classification != "TIMEOUT" and capture.timed_out:
            raise ValueError(
                "timed_out transport capture cannot use a non-timeout split reason"
            )
        elif body is None and (
            not capture.timed_out
            or capture.error_classification != "TRANSIENT_NETWORK"
        ):
            raise ValueError("bodyless TIMEOUT split requires a timed_out capture")
        elif body == b"":
            raise ValueError("partial split response body must be nonempty")

        recovered = self._recover_pending_before_mutation(query)
        if recovered is not None:
            expected_sha256 = _sha256_bytes(body) if body is not None else None
            if (
                recovered.attempt_number == attempt_number
                and recovered.outcome == outcome
                and recovered.error_classification == error_classification
                and recovered.body_sha256 == expected_sha256
                and recovered.byte_count == len(body or b"")
            ):
                return recovered
            raise RuntimeError(
                "pending failure transaction recovered; conflicting failure mutation refused"
            )
        self._ensure_unpublished(query)

        query_id = logical_query_id(query)
        attempt_record = _attempt_record(
            query_id,
            attempt_number,
            capture,
            outcome,
            error_classification=error_classification,
        )
        _validate_query_byte_count(query, attempt_record["byte_count"])
        body_sha256 = _sha256_bytes(body) if body is not None else None
        body_relative = (
            Path(
                "quarantine", query.source, query_id, f"{body_sha256}.raw"
            ).as_posix()
            if body_sha256 is not None
            else None
        )
        evidence_receipt = {
            **attempt_record,
            "evidence_kind": evidence_kind,
            "quarantined_body_path": body_relative,
            "raw_sha256": body_sha256,
        }
        transaction_contents = _failure_transaction_bytes(evidence_receipt, body)
        attempts_fd, attempt_descriptors = self._attempts_fd(query)
        intent_fd = -1
        transaction_fd = -1
        try:
            self._validate_attempt_reservation(query, attempt_number, attempts_fd)
            self._verified_attempt_bundle(query, attempts_fd)
            query_fd = attempt_descriptors[-1]
            registration_sha256 = self._validate_registered(query, query_fd)
            reservation_contents = _read_regular_at(
                attempts_fd,
                f"{attempt_number:06d}.reservation.json",
                "attempt reservation",
            )
            intent_contents = _failure_intent_bytes(
                query,
                capture,
                evidence_receipt,
                transaction_contents,
                registration_sha256,
                reservation_contents,
            )
            intent_fd = _open_or_publish_failure_directory(
                query_fd,
                final_name=_FAILURE_INTENT_NAME,
                marker_name=_FAILURE_INTENT_MARKER,
                marker_contents=intent_contents,
                marker_label="failure intent marker",
                body_name=_FAILURE_INTENT_BODY,
                body=body,
                body_label="failure intent body",
            )
            transaction_fd = _open_or_publish_failure_directory(
                query_fd,
                final_name=_FAILURE_TRANSACTION_NAME,
                marker_name=_FAILURE_TRANSACTION_MARKER,
                marker_contents=transaction_contents,
                marker_label="failure transaction marker",
            )
            return self._finish_failure_transaction(
                query,
                evidence_receipt,
                body,
                transaction_contents,
                intent_contents,
                attempts_fd,
                attempt_descriptors,
                intent_fd,
                transaction_fd,
            )
        finally:
            if transaction_fd >= 0:
                os.close(transaction_fd)
            if intent_fd >= 0:
                os.close(intent_fd)
            os.close(attempts_fd)
            self._close_fds(attempt_descriptors)

    def _finish_failure_transaction(
        self,
        query: LogicalQuery,
        evidence_receipt: dict[str, object],
        body: bytes | None,
        transaction_contents: bytes,
        intent_contents: bytes,
        attempts_fd: int,
        attempt_descriptors: list[int],
        intent_fd: int,
        transaction_fd: int,
    ) -> PersistedAttemptEvidence:
        query_id = logical_query_id(query)
        attempt_number = int(evidence_receipt["attempt_number"])
        intent_snapshot = _read_regular_snapshot_at(
            intent_fd, _FAILURE_INTENT_MARKER, "failure intent marker"
        )
        transaction_snapshot = _read_regular_snapshot_at(
            transaction_fd,
            _FAILURE_TRANSACTION_MARKER,
            "failure transaction marker",
        )
        if (
            intent_snapshot is None
            or intent_snapshot.contents != intent_contents
            or transaction_snapshot is None
            or transaction_snapshot.contents != transaction_contents
        ):
            raise ValueError("failure intent or transaction snapshot differs")
        self._verify_query_chain(query, attempt_descriptors)
        for name, descriptor in (
            (_FAILURE_INTENT_NAME, intent_fd),
            (_FAILURE_TRANSACTION_NAME, transaction_fd),
        ):
            _rebind_directory_at(
                attempt_descriptors[-1],
                name,
                descriptor,
                name,
            )
        quarantine_descriptors = [os.dup(attempt_descriptors[0])]
        try:
            for name in ("quarantine", query.source, query_id):
                child = _open_directory_at(
                    quarantine_descriptors[-1], name, create=True
                )
                quarantine_descriptors.append(child)
            self._verify_progress_root_stitch(
                attempt_descriptors, quarantine_descriptors
            )
            self._verify_quarantine_descriptor_chain(
                query.source, query_id, quarantine_descriptors
            )
            quarantine_fd = quarantine_descriptors[-1]
            body_sha256 = evidence_receipt["raw_sha256"]
            body_relative = evidence_receipt["quarantined_body_path"]
            if body is not None:
                body_name = f"{body_sha256}.raw"
                _publish_content_addressed_regular_at(
                    intent_fd,
                    _FAILURE_INTENT_BODY,
                    quarantine_fd,
                    body_name,
                    body,
                    "persisted failure response body",
                )
            evidence_contents = _canonical_file_bytes(evidence_receipt)
            receipt_name = f"{attempt_number:06d}.json"
            _write_once_at(
                quarantine_fd,
                receipt_name,
                evidence_contents,
                "failure evidence receipt",
            )
            attempt_record = {
                key: evidence_receipt[key] for key in _ATTEMPT_KEYS
            }
            attempt_contents = _canonical_file_bytes(attempt_record)
            _write_once_at(
                attempts_fd,
                receipt_name,
                attempt_contents,
                "attempt receipt",
            )
            _, _, attempt_receipts, _ = self._verified_attempt_bundle(
                query, attempts_fd
            )
            self._verified_quarantine_bundle(
                query, quarantine_fd, attempt_receipts
            )
            self._validate_attempt_reservation(
                query, attempt_number, attempts_fd
            )
            _rebind_regular_snapshot_at(
                intent_fd,
                _FAILURE_INTENT_MARKER,
                intent_snapshot,
                "failure intent marker",
            )
            _rebind_regular_snapshot_at(
                transaction_fd,
                _FAILURE_TRANSACTION_MARKER,
                transaction_snapshot,
                "failure transaction marker",
            )
            reservation_contents = _read_regular_at(
                attempts_fd,
                f"{attempt_number:06d}.reservation.json",
                "attempt reservation",
            )
            decoded_receipt, decoded_transaction = _decode_failure_intent(
                _read_regular_at(
                    intent_fd,
                    _FAILURE_INTENT_MARKER,
                    "failure intent marker",
                ),
                query,
            )
            if (
                decoded_receipt != evidence_receipt
                or decoded_transaction != transaction_contents
                or _read_regular_at(
                    intent_fd,
                    _FAILURE_INTENT_MARKER,
                    "failure intent marker",
                )
                != intent_contents
            ):
                raise ValueError("failure intent changed before commit cleanup")
            intent = _json_object(intent_contents, "failure intent")
            if intent["reservation_sha256"] != _sha256_bytes(
                reservation_contents
            ):
                raise ValueError("failure intent reservation binding differs")
            if intent["query_registration_sha256"] != self._validate_registered(
                query, attempt_descriptors[-1]
            ):
                raise ValueError("failure intent registration binding differs")
            if _read_regular_at(
                transaction_fd,
                _FAILURE_TRANSACTION_MARKER,
                "failure transaction marker",
            ) != transaction_contents:
                raise ValueError("failure transaction marker changed before cleanup")
            self._verify_progress_root_stitch(
                attempt_descriptors, quarantine_descriptors
            )
            self._verify_query_chain(query, attempt_descriptors)
            self._verify_quarantine_descriptor_chain(
                query.source, query_id, quarantine_descriptors
            )
            for name, descriptor in (
                (_FAILURE_INTENT_NAME, intent_fd),
                (_FAILURE_TRANSACTION_NAME, transaction_fd),
            ):
                _rebind_directory_at(
                    attempt_descriptors[-1], name, descriptor, name
                )
            registration_sha256 = str(intent["query_registration_sha256"])
            cleanup: dict[str, tuple[str, str, bytes]] = {}
            for kind, marker_contents in (
                ("transaction", transaction_contents),
                ("intent", intent_contents),
            ):
                nonce = os.urandom(16).hex()
                container_name = _failure_cleanup_tombstone_name(
                    kind,  # type: ignore[arg-type]
                    attempt_number,
                    marker_contents,
                    nonce=nonce,
                )
                claim_name = _failure_cleanup_claim_name(
                    kind, attempt_number, nonce  # type: ignore[arg-type]
                )
                claim_contents = _failure_cleanup_claim_bytes(
                    kind=kind,  # type: ignore[arg-type]
                    query_id=query_id,
                    attempt_number=attempt_number,
                    nonce=nonce,
                    container_name=container_name,
                    marker_contents=marker_contents,
                    evidence_receipt_contents=evidence_contents,
                    registration_sha256=registration_sha256,
                    reservation_contents=reservation_contents,
                )
                _publish_cleanup_claim_at(
                    attempt_descriptors[-1], claim_name, claim_contents
                )
                cleanup[kind] = (claim_name, container_name, claim_contents)
            _, transaction_container_fd = _claim_failure_cleanup_container(
                attempt_descriptors[-1],
                _FAILURE_TRANSACTION_NAME,
                cleanup["transaction"][1],
            )
            try:
                _rebind_directory_at(
                    transaction_container_fd,
                    _FAILURE_CLEANUP_PAYLOAD,
                    transaction_fd,
                    "failure transaction cleanup payload",
                )
            finally:
                os.close(transaction_container_fd)
            _, intent_container_fd = _claim_failure_cleanup_container(
                attempt_descriptors[-1],
                _FAILURE_INTENT_NAME,
                cleanup["intent"][1],
            )
            try:
                _rebind_directory_at(
                    intent_container_fd,
                    _FAILURE_CLEANUP_PAYLOAD,
                    intent_fd,
                    "failure intent cleanup payload",
                )
            finally:
                os.close(intent_container_fd)
            self._finish_failure_cleanup_tombstones(
                query,
                attempt_descriptors[-1],
                registration_sha256,
            )
            self._require_lease(query)
            return PersistedAttemptEvidence(
                query_id=query_id,
                attempt_number=attempt_number,
                outcome=evidence_receipt["outcome"],  # type: ignore[arg-type]
                attempt_receipt_path=self.root
                / _relative_path(
                    query.source, query_id, "attempts", receipt_name
                ),
                attempt_receipt_sha256=_sha256_bytes(attempt_contents),
                evidence_receipt_path=self.root
                / "quarantine"
                / query.source
                / query_id
                / receipt_name,
                evidence_receipt_sha256=_sha256_bytes(evidence_contents),
                body_path=self.root / body_relative
                if isinstance(body_relative, str)
                else None,
                body_sha256=body_sha256
                if isinstance(body_sha256, str)
                else None,
                byte_count=int(evidence_receipt["byte_count"]),
                error_classification=str(
                    evidence_receipt["error_classification"]
                ),
            )
        finally:
            self._close_fds(quarantine_descriptors)

    def _read_staged_failure_body(
        self,
        query: LogicalQuery,
        intent_fd: int,
        receipt: Mapping[str, object],
        *,
        root_descriptor: int,
    ) -> bytes | None:
        snapshot = _read_regular_snapshot_with_link_counts_at(
            intent_fd,
            _FAILURE_INTENT_BODY,
            "failure intent body",
            allowed_link_counts=frozenset({1, 2}),
            missing_ok=True,
        )
        body = None if snapshot is None else snapshot.contents
        _validate_staged_transaction_body(receipt, body)
        if snapshot is not None and snapshot.identity[3] == 2:
            raw_sha256 = _validate_sha256(
                receipt.get("raw_sha256"),
                "failure intent linked body SHA-256",
            )
            _, quarantine_descriptors = self._quarantine_fds(
                query, root_descriptor=root_descriptor
            )
            try:
                _validate_owned_content_link_at(
                    intent_fd,
                    _FAILURE_INTENT_BODY,
                    snapshot,
                    quarantine_descriptors[-1],
                    f"{raw_sha256}.raw",
                    body,
                    "failure intent final body",
                )
            finally:
                self._close_fds(quarantine_descriptors)
        return body

    def recover_failure_attempt(
        self, query: LogicalQuery
    ) -> PersistedAttemptEvidence | None:
        self._require_lease(query)
        query_id, descriptors = self._query_fds(query, create=False)
        del query_id
        attempts_fd = -1
        intent_fd = -1
        transaction_fd = -1
        try:
            try:
                intent_fd = _open_directory_at(
                    descriptors[-1], _FAILURE_INTENT_NAME, create=False
                )
            except FileNotFoundError:
                try:
                    marker_only = _open_directory_at(
                        descriptors[-1], _FAILURE_TRANSACTION_NAME, create=False
                    )
                except FileNotFoundError:
                    return None
                else:
                    os.close(marker_only)
                    raise ValueError(
                        "failure transaction marker has no immutable intent"
                    )
            intent_contents = _read_regular_at(
                intent_fd,
                _FAILURE_INTENT_MARKER,
                "failure intent marker",
            )
            receipt, transaction_contents = _decode_failure_intent(
                intent_contents, query
            )
            body = self._read_staged_failure_body(
                query,
                intent_fd,
                receipt,
                root_descriptor=descriptors[0],
            )
            attempt_number = receipt.get("attempt_number")
            if receipt.get("query_id") != logical_query_id(query) or type(attempt_number) is not int:
                raise ValueError("failure intent query binding differs")
            attempts_fd = _open_directory_at(
                descriptors[-1], "attempts", create=False
            )
            self._validate_attempt_reservation(query, attempt_number, attempts_fd)
            reservation_contents = _read_regular_at(
                attempts_fd,
                f"{attempt_number:06d}.reservation.json",
                "attempt reservation",
            )
            intent = _json_object(intent_contents, "failure intent")
            if intent["reservation_sha256"] != _sha256_bytes(
                reservation_contents
            ):
                raise ValueError("failure intent reservation binding differs")
            if intent["query_registration_sha256"] != self._validate_registered(
                query, descriptors[-1]
            ):
                raise ValueError("failure intent registration binding differs")
            try:
                transaction_fd = _open_or_publish_failure_directory(
                    descriptors[-1],
                    final_name=_FAILURE_TRANSACTION_NAME,
                    marker_name=_FAILURE_TRANSACTION_MARKER,
                    marker_contents=transaction_contents,
                    marker_label="failure transaction marker",
                )
            except RuntimeError as exc:
                raise ValueError(
                    "failure transaction marker differs from immutable intent"
                ) from exc
            marker = _read_regular_at(
                transaction_fd,
                _FAILURE_TRANSACTION_MARKER,
                "failure transaction marker",
            )
            if marker != transaction_contents:
                raise ValueError(
                    "failure transaction marker differs from immutable intent"
                )
            if receipt.get("raw_sha256") is not None and body is None:
                try:
                    _, quarantine_descriptors = self._quarantine_fds(
                        query, root_descriptor=descriptors[0]
                    )
                except FileNotFoundError:
                    raise RuntimeError(
                        "failure intent body is missing"
                    ) from None
                try:
                    final_body = _read_regular_at(
                        quarantine_descriptors[-1],
                        f"{receipt['raw_sha256']}.raw",
                        "failure intent final body",
                    )
                    _validate_staged_transaction_body(receipt, final_body)
                finally:
                    self._close_fds(quarantine_descriptors)
            return self._finish_failure_transaction(
                query,
                receipt,
                body,
                transaction_contents,
                intent_contents,
                attempts_fd,
                descriptors,
                intent_fd,
                transaction_fd,
            )
        finally:
            if attempts_fd >= 0:
                os.close(attempts_fd)
            if transaction_fd >= 0:
                os.close(transaction_fd)
            if intent_fd >= 0:
                os.close(intent_fd)
            self._close_fds(descriptors)

    def _recover_pending_before_mutation(
        self, query: LogicalQuery, *, authority: object | None = None
    ) -> PersistedAttemptEvidence | None:
        recovered = self.recover_failure_attempt(query)
        if recovered is not None:
            self._ensure_unpublished(query, authority=authority)
        return recovered

    def read_verified_split_attempt(
        self, query: LogicalQuery, attempt_number: int
    ) -> PersistedAttemptEvidence:
        query_id = logical_query_id(query)
        _, descriptors = self._query_fds(query, create=False)
        lock_descriptor = -1
        recovery_canary_started = False
        attempts_fd = -1
        quarantine_descriptors: list[int] | None = None
        try:
            query_fd = descriptors[-1]
            lock_descriptor = self._open_query_lock(
                query_fd,
                create=False,
                read_only=self._read_only,
            )
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_SH if self._read_only else fcntl.LOCK_EX,
            )
            (
                registration_sha256,
                registered_query,
                query_snapshot,
            ) = self._validated_registered_query(
                query, query_fd, lock_descriptor=lock_descriptor
            )
            if self._read_only:
                self._require_no_failure_recovery_state(query_fd)
                recovery_canary_started = True
            else:
                self._finish_failure_cleanup_tombstones(
                    query, query_fd, registration_sha256
                )
            attempts_fd = _open_directory_at(query_fd, "attempts", create=False)
            _, _, receipts, attempt_snapshots = self._verified_attempt_bundle(
                query, attempts_fd
            )
            matches = [
                record
                for record in receipts
                if record.get("attempt_number") == attempt_number
            ]
            if len(matches) != 1 or matches[0].get("outcome") != "SPLIT_REQUIRED":
                raise ValueError("verified split attempt is missing")
            record = dict(matches[0])
            _, quarantine_descriptors = self._quarantine_fds(
                query, root_descriptor=descriptors[0]
            )
            _, evidence_snapshots, body_snapshots, _ = (
                self._verified_quarantine_bundle(
                    query, quarantine_descriptors[-1], receipts
                )
            )
            receipt_name = f"{attempt_number:06d}.json"
            evidence_snapshot = dict(evidence_snapshots).get(receipt_name)
            if evidence_snapshot is None:
                raise ValueError("verified split evidence receipt is missing")
            evidence_contents = evidence_snapshot.contents
            evidence = _json_object(evidence_contents, "split evidence receipt")
            if {key: evidence[key] for key in _ATTEMPT_KEYS} != record:
                raise ValueError("split evidence attempt binding differs")
            attempt_snapshot = dict(attempt_snapshots).get(receipt_name)
            if attempt_snapshot is None:
                raise ValueError("verified split attempt receipt is missing")
            for name, snapshot in (
                (receipt_name, attempt_snapshot),
            ):
                _rebind_regular_snapshot_at(
                    attempts_fd, name, snapshot, "split attempt receipt"
                )
            _rebind_regular_snapshot_at(
                quarantine_descriptors[-1],
                receipt_name,
                evidence_snapshot,
                "split evidence receipt",
            )
            raw_sha256 = evidence["raw_sha256"]
            body_name: str | None = None
            body_snapshot: _StreamedRegularSnapshot | None = None
            if isinstance(raw_sha256, str):
                body_name = f"{raw_sha256}.raw"
                body_snapshot = dict(body_snapshots).get(body_name)
                if body_snapshot is None or body_snapshot.sha256 != raw_sha256:
                    raise ValueError("verified split body binding differs")
                rebound_body = os.stat(
                    body_name,
                    dir_fd=quarantine_descriptors[-1],
                    follow_symlinks=False,
                )
                if _identity(rebound_body) != body_snapshot.identity:
                    raise ValueError("verified split body identity changed")
            self._verify_progress_root_stitch(
                descriptors, quarantine_descriptors
            )
            self._verify_query_chain(query, descriptors)
            self._verify_quarantine_descriptor_chain(
                query.source, query_id, quarantine_descriptors
            )
            _rebind_regular_snapshot_at(
                query_fd,
                "query.json",
                query_snapshot,
                "logical query registration",
            )
            (
                final_registration_sha256,
                final_registered_query,
                final_query_snapshot,
            ) = self._validated_registered_query(
                query, query_fd, lock_descriptor=lock_descriptor
            )
            if (
                final_registration_sha256 != registration_sha256
                or final_registered_query != registered_query
                or final_query_snapshot != query_snapshot
            ):
                raise ValueError(
                    "logical query registration changed during split read"
                )
            self._verify_query_chain(query, descriptors)
            _rebind_regular_snapshot_at(
                query_fd,
                "query.json",
                query_snapshot,
                "logical query registration",
            )
            _rebind_regular_snapshot_at(
                attempts_fd,
                receipt_name,
                attempt_snapshot,
                "split attempt receipt",
            )
            _rebind_regular_snapshot_at(
                quarantine_descriptors[-1],
                receipt_name,
                evidence_snapshot,
                "split evidence receipt",
            )
            if body_name is not None and body_snapshot is not None:
                final_body_snapshot = _read_streamed_regular_snapshot_at(
                    quarantine_descriptors[-1],
                    body_name,
                    "split response body",
                    expected_byte_count=body_snapshot.byte_count,
                    maximum_byte_count=_response_byte_limit(query),
                )
                if final_body_snapshot != body_snapshot:
                    raise ValueError(
                        "split response body identity or bytes changed"
                    )
        finally:
            try:
                if recovery_canary_started:
                    self._require_no_failure_recovery_state(descriptors[-1])
            finally:
                if quarantine_descriptors is not None:
                    self._close_fds(quarantine_descriptors)
                if attempts_fd >= 0:
                    os.close(attempts_fd)
                if lock_descriptor >= 0:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    os.close(lock_descriptor)
                self._close_fds(descriptors)
        attempt_path = self.root / _relative_path(
            query.source, query_id, "attempts", f"{attempt_number:06d}.json"
        )
        evidence_path = (
            self.root
            / "quarantine"
            / query.source
            / query_id
            / f"{attempt_number:06d}.json"
        )
        body_relative = evidence["quarantined_body_path"]
        return PersistedAttemptEvidence(
            query_id=query_id,
            attempt_number=attempt_number,
            outcome="SPLIT_REQUIRED",
            attempt_receipt_path=attempt_path,
            attempt_receipt_sha256=_sha256_bytes(attempt_snapshot.contents),
            evidence_receipt_path=evidence_path,
            evidence_receipt_sha256=_sha256_bytes(evidence_contents),
            body_path=self.root / body_relative if isinstance(body_relative, str) else None,
            body_sha256=evidence["raw_sha256"] if isinstance(evidence["raw_sha256"], str) else None,
            byte_count=int(record["byte_count"]),
            error_classification=str(record["error_classification"]),
        )

    def quarantine_bodyless_attempt(
        self,
        query: LogicalQuery,
        attempt_number: int,
        capture: AttemptCapture,
        *,
        error_classification: str,
    ) -> PersistedAttemptEvidence:
        return self._persist_failure_evidence(
            query,
            attempt_number,
            capture,
            outcome="QUARANTINED",
            error_classification=error_classification,
            evidence_kind="TERMINAL_BODYLESS_FAILURE",
        )

    def persist_split_attempt(
        self,
        query: LogicalQuery,
        attempt_number: int,
        capture: AttemptCapture,
        *,
        reason: Literal["ROW_CAP", "SIZE_LIMIT", "TIMEOUT"],
    ) -> PersistedAttemptEvidence:
        if reason not in _SPLIT_REASONS:
            raise ValueError("split reason must be ROW_CAP, SIZE_LIMIT, or TIMEOUT")
        if reason == "TIMEOUT" and isinstance(capture, AttemptCapture) and capture.body == b"":
            capture = replace(capture, body=None)
        return self._persist_failure_evidence(
            query,
            attempt_number,
            capture,
            outcome="SPLIT_REQUIRED",
            error_classification=reason,
            evidence_kind="PARTIAL_SPLIT_RESPONSE",
        )

    def _attempt_history(self, query: LogicalQuery) -> list[dict[str, object]]:
        attempts_fd, descriptors = self._attempts_fd(query)
        try:
            reservations, receipts = self._attempt_numbers(
                attempts_fd, logical_query_id(query)
            )
            del reservations
            history: list[dict[str, object]] = []
            for number in sorted(receipts):
                name = f"{number:06d}.json"
                contents = _read_regular_at(attempts_fd, name, "attempt receipt")
                record = _json_object(contents, "attempt receipt")
                if record.get("attempt_number") != number or record.get(
                    "query_id"
                ) != logical_query_id(query):
                    raise ValueError("attempt history binding differs")
                history.append(
                    {
                        "attempt_number": number,
                        "path": _relative_path(
                            query.source,
                            logical_query_id(query),
                            "attempts",
                            name,
                        ),
                        "sha256": _sha256_bytes(contents),
                    }
                )
            return history
        finally:
            os.close(attempts_fd)
            self._close_fds(descriptors)

    def _verified_attempt_bundle(
        self,
        query: LogicalQuery,
        attempts_fd: int,
    ) -> tuple[
        set[int],
        list[dict[str, object]],
        tuple[Mapping[str, object], ...],
        tuple[tuple[str, _RegularSnapshot], ...],
    ]:
        query_id = logical_query_id(query)
        reservations: set[int] = set()
        receipts: dict[int, tuple[dict[str, object], bytes]] = {}
        artifact_snapshots: list[tuple[str, _RegularSnapshot]] = []
        try:
            names = sorted(os.listdir(attempts_fd))
        except OSError as exc:
            raise ValueError("attempt directory cannot be listed safely") from exc
        for name in names:
            reservation = _RESERVATION_NAME.fullmatch(name)
            receipt = _ATTEMPT_NAME.fullmatch(name)
            if reservation:
                number = int(reservation.group("number"))
                snapshot = _read_regular_snapshot_at(
                    attempts_fd, name, "attempt reservation"
                )
                if snapshot is None:
                    raise RuntimeError("attempt reservation is missing")
                value = _json_object(
                    snapshot.contents,
                    "attempt reservation",
                )
                if value != {
                    "attempt_number": number,
                    "query_id": query_id,
                    "schema_version": 1,
                }:
                    raise ValueError("attempt reservation binding differs")
                reservations.add(number)
                artifact_snapshots.append((name, snapshot))
            elif receipt:
                number = int(receipt.group("number"))
                snapshot = _read_regular_snapshot_at(
                    attempts_fd, name, "attempt receipt"
                )
                if snapshot is None:
                    raise RuntimeError("attempt receipt is missing")
                contents = snapshot.contents
                record = _json_object(contents, "attempt receipt")
                _validate_attempt_record(record, query_id, number)
                _validate_query_byte_count(query, record["byte_count"])
                receipts[number] = (record, contents)
                artifact_snapshots.append((name, snapshot))
            else:
                raise ValueError("attempt directory contains an ambiguous entry")
        if not set(receipts) <= reservations:
            raise ValueError("attempt receipt has no immutable reservation")
        if reservations and reservations != set(range(1, max(reservations) + 1)):
            raise ValueError("attempt reservation order has a gap")
        history: list[dict[str, object]] = []
        records: list[Mapping[str, object]] = []
        for number in sorted(receipts):
            name = f"{number:06d}.json"
            record, contents = receipts[number]
            history.append(
                {
                    "attempt_number": number,
                    "path": _relative_path(
                        query.source, query_id, "attempts", name
                    ),
                    "sha256": _sha256_bytes(contents),
                }
            )
            records.append(record)
        return reservations, history, tuple(records), tuple(artifact_snapshots)

    def _verified_quarantine_bundle(
        self,
        query: LogicalQuery,
        quarantine_fd: int,
        attempt_receipts: tuple[Mapping[str, object], ...],
    ) -> tuple[
        tuple[VerifiedQuarantineRead, ...],
        tuple[tuple[str, _RegularSnapshot], ...],
        tuple[tuple[str, _StreamedRegularSnapshot], ...],
        tuple[str, ...],
    ]:
        query_id = logical_query_id(query)
        body_limit = _response_byte_limit(query)
        evidence_attempts = {
            int(record["attempt_number"]): record
            for record in attempt_receipts
            if record.get("outcome") in {"QUARANTINED", "SPLIT_REQUIRED"}
        }
        try:
            membership = tuple(sorted(os.listdir(quarantine_fd)))
        except OSError as exc:
            raise ValueError("quarantine directory cannot be listed safely") from exc

        validated: list[
            tuple[int, str, _RegularSnapshot, str, str, int, str, bool]
        ] = []
        receipt_snapshots: dict[str, _RegularSnapshot] = {}
        expected_members: set[str] = set()
        declared_bodies: dict[str, int] = {}
        for attempt_number, attempt_record in sorted(evidence_attempts.items()):
            receipt_name = f"{attempt_number:06d}.json"
            expected_members.add(receipt_name)
            receipt_snapshot = _read_regular_snapshot_at(
                quarantine_fd,
                receipt_name,
                "quarantine receipt",
            )
            if receipt_snapshot is None:
                raise RuntimeError("quarantine receipt is missing")
            receipt_snapshots[receipt_name] = receipt_snapshot
            receipt = _json_object(receipt_snapshot.contents, "quarantine receipt")
            receipt_keys = set(receipt)
            if receipt_keys not in {
                frozenset(
                    _ATTEMPT_KEYS
                    | {"quarantined_body_path", "raw_sha256"}
                ),
                frozenset(_FAILURE_EVIDENCE_KEYS),
            }:
                raise ValueError("quarantine receipt keys differ")
            projected_attempt = {
                key: receipt[key]
                for key in _ATTEMPT_KEYS
            }
            _validate_attempt_record(projected_attempt, query_id, attempt_number)
            if projected_attempt != dict(attempt_record):
                raise ValueError("quarantine attempt binding differs")
            evidence_kind = receipt.get("evidence_kind")
            legacy_quarantine = evidence_kind is None
            if legacy_quarantine:
                if projected_attempt["outcome"] != "QUARANTINED":
                    raise ValueError("quarantine outcome binding differs")
            elif evidence_kind == "TERMINAL_BODYLESS_FAILURE":
                if (
                    projected_attempt["outcome"] != "QUARANTINED"
                    or projected_attempt["body_sha256"] is not None
                    or projected_attempt["byte_count"] != 0
                    or receipt["quarantined_body_path"] is not None
                    or receipt["raw_sha256"] is not None
                ):
                    raise ValueError("bodyless failure evidence binding differs")
                continue
            elif evidence_kind == "PARTIAL_SPLIT_RESPONSE":
                if (
                    projected_attempt["outcome"] != "SPLIT_REQUIRED"
                    or projected_attempt["error_classification"]
                    not in _SPLIT_REASONS
                ):
                    raise ValueError("partial split evidence binding differs")
                if projected_attempt["body_sha256"] is None:
                    if (
                        projected_attempt["error_classification"] != "TIMEOUT"
                        or projected_attempt["byte_count"] != 0
                        or receipt["quarantined_body_path"] is not None
                        or receipt["raw_sha256"] is not None
                    ):
                        raise ValueError("bodyless timeout split binding differs")
                    continue
            else:
                raise ValueError("failure evidence kind differs")
            raw_sha256 = _validate_sha256(
                receipt["raw_sha256"], "quarantine raw SHA-256"
            )
            if raw_sha256 != projected_attempt["body_sha256"]:
                raise ValueError("quarantine body hash binding differs")
            body_name = f"{raw_sha256}.raw"
            body_relative = Path(
                "quarantine", query.source, query_id, body_name
            ).as_posix()
            if receipt["quarantined_body_path"] != body_relative:
                raise ValueError("quarantine body path binding differs")
            expected_members.add(body_name)
            raw_byte_count = projected_attempt["byte_count"]
            if (
                type(raw_byte_count) is not int
                or raw_byte_count > body_limit
            ):
                raise ValueError(
                    "quarantine response body exceeds its response byte limit"
                )
            prior_count = declared_bodies.setdefault(body_name, raw_byte_count)
            if prior_count != raw_byte_count:
                raise ValueError("shared quarantine body byte count differs")
            classification = projected_attempt["error_classification"]
            if not isinstance(classification, str):
                raise ValueError("quarantine error classification differs")
            validated.append(
                (
                    attempt_number,
                    receipt_name,
                    receipt_snapshot,
                    raw_sha256,
                    body_relative,
                    raw_byte_count,
                    classification,
                    legacy_quarantine,
                )
            )

        if (
            sum(declared_bodies.values())
            > body_limit * _MAX_QUARANTINE_BODIES_PER_QUERY
        ):
            raise ValueError("quarantine aggregate response byte limit exceeded")
        if set(membership) != expected_members:
            raise ValueError("quarantine directory membership is ambiguous")

        body_snapshots = {
            body_name: _read_streamed_regular_snapshot_at(
                quarantine_fd,
                body_name,
                "quarantined response body",
                expected_byte_count=raw_byte_count,
                maximum_byte_count=body_limit,
            )
            for body_name, raw_byte_count in sorted(declared_bodies.items())
        }
        reads: list[VerifiedQuarantineRead] = []
        for (
            attempt_number,
            receipt_name,
            receipt_snapshot,
            raw_sha256,
            body_relative,
            raw_byte_count,
            classification,
            legacy_quarantine,
        ) in validated:
            body_name = f"{raw_sha256}.raw"
            body_snapshot = body_snapshots[body_name]
            if (
                body_snapshot.sha256 != raw_sha256
                or body_snapshot.byte_count != raw_byte_count
            ):
                raise ValueError("quarantine response body binding differs")
            if legacy_quarantine:
                reads.append(
                    VerifiedQuarantineRead(
                        attempt_number=attempt_number,
                        receipt_path=self.root
                        / "quarantine"
                        / query.source
                        / query_id
                        / receipt_name,
                        receipt_sha256=_sha256_bytes(receipt_snapshot.contents),
                        raw_path=self.root / body_relative,
                        raw_sha256=raw_sha256,
                        raw_byte_count=raw_byte_count,
                        error_classification=classification,
                    )
                )
        return (
            tuple(reads),
            tuple(sorted(receipt_snapshots.items())),
            tuple(sorted(body_snapshots.items())),
            membership,
        )

    def _validated_fetched_success_attempt(
        self,
        query: LogicalQuery,
        attempt_number: int,
        raw_sha256: str,
        raw_byte_count: int,
    ) -> list[dict[str, object]]:
        attempts_fd, descriptors = self._attempts_fd(query)
        try:
            query_id = logical_query_id(query)
            reservations, receipts = self._attempt_numbers(attempts_fd, query_id)
            if (
                not reservations
                or not receipts
                or attempt_number != max(reservations)
                or attempt_number != max(receipts)
            ):
                raise ValueError("successful attempt is not the current finalized tail")
            name = f"{attempt_number:06d}.json"
            record = _json_object(
                _read_regular_at(attempts_fd, name, "successful attempt receipt"),
                "successful attempt receipt",
            )
            _validate_attempt_record(record, query_id, attempt_number)
            if (
                record["outcome"] != "SUCCESS"
                or record["body_sha256"] != raw_sha256
                or record["byte_count"] != raw_byte_count
            ):
                raise ValueError("successful attempt raw binding differs")
        finally:
            os.close(attempts_fd)
            self._close_fds(descriptors)
        return self._attempt_history(query)

    def _sidecar_artifact(
        self,
        query: LogicalQuery,
        raw_sha256: str,
        byte_count: int,
        semantics: Mapping[str, object],
    ) -> tuple[str, str, bytes]:
        semantic_record = _normalized_json_object(semantics, "query_sidecar")
        sidecar = {
            "byte_count": byte_count,
            "grain": query.grain,
            "parser_version": query.parser_version,
            "query_id": logical_query_id(query),
            "raw_sha256": raw_sha256,
            "schema_version": 1,
            "semantics": semantic_record,
            "source": query.source,
        }
        contents = _canonical_file_bytes(sidecar)
        digest = _sha256_bytes(contents)
        name = f"{digest}.json"
        return name, digest, contents

    def _derived_semantics(
        self,
        query: LogicalQuery,
        semantics: Mapping[str, object],
        *,
        authority: object | None = None,
    ) -> dict[str, object]:
        from .v52_seasons import SEASON_BOUNDARIES
        from .v52_source_contracts import recompute_query_sidecar

        if query.endpoint == "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534":
            from .v52_duplicate_gamepk_disposition import (
                DuplicateGamePkDispositionAuthority,
            )

            if not isinstance(authority, DuplicateGamePkDispositionAuthority):
                raise TypeError(
                    "schedule disposition requires its authenticated authority"
                )
            if authority.query != query or authority.evidence_root != self.root:
                raise ValueError("schedule disposition authority binding differs")
            expected = dict(
                recompute_query_sidecar(query, b"", authority=authority)
            )
            supplied = _normalized_json_object(
                semantics, "derived query_sidecar"
            )
            if supplied != expected:
                raise ValueError("derived disposition semantics differ from query")
            return expected

        if authority is not None:
            raise ValueError("preseason disposition must not receive an authority")

        expected_range = {
            "as_of_date",
            "consumer_cutoff_dates",
            "consuming_game_pks",
            "disposition",
            "reason",
            "season",
        }
        if (
            query.source != "mlb_stats_pitching"
            or query.grain != "season_d1_league_pitching_reference"
            or query.endpoint != _DERIVED_ENDPOINT
            or query.parameters != {}
            or query.expected_media_type != "application/json"
            or query.parser_version != "v5.2.1"
            or set(query.requested_range) != expected_range
            or query.requested_range.get("reason") != _DERIVED_REASON
        ):
            raise ValueError("derived disposition query contract differs")
        season = query.requested_range.get("season")
        as_of_text = query.requested_range.get("as_of_date")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in SEASON_BOUNDARIES
            or not isinstance(as_of_text, str)
        ):
            raise ValueError("derived disposition season/date binding differs")
        try:
            as_of = date.fromisoformat(as_of_text)
        except ValueError as exc:
            raise ValueError("derived disposition as_of_date differs") from exc
        if (
            as_of.isoformat() != as_of_text
            or as_of != SEASON_BOUNDARIES[season][0] - timedelta(days=1)
        ):
            raise ValueError(
                "derived disposition as_of_date must equal frozen season start D-1"
            )
        expected = dict(recompute_query_sidecar(query, b""))
        expected["reason"] = _DERIVED_REASON
        supplied = _normalized_json_object(semantics, "derived query_sidecar")
        if supplied != expected:
            raise ValueError("derived disposition semantics differ from query")
        return expected

    def _derived_sidecar_artifact(
        self,
        query: LogicalQuery,
        semantics: Mapping[str, object],
        *,
        authority: object | None = None,
    ) -> tuple[str, str, bytes]:
        semantic_record = self._derived_semantics(
            query, semantics, authority=authority
        )
        sidecar = {
            "grain": query.grain,
            "parser_version": query.parser_version,
            "query_id": logical_query_id(query),
            "schema_version": 1,
            "semantics": semantic_record,
            "source": query.source,
            "storage_mode": "DERIVED_DISPOSITION",
        }
        contents = _canonical_file_bytes(sidecar)
        digest = _sha256_bytes(contents)
        return f"{digest}.json", digest, contents

    @staticmethod
    def _ensure_content_slot(
        directory_fd: int,
        expected_name: str,
        expected_contents: bytes,
        label: str,
    ) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise ValueError(f"{label} inventory cannot be read safely") from exc
        if len(names) > 1:
            for name in names:
                _read_regular_at(directory_fd, name, label)
            raise RuntimeError(f"duplicate ambiguous {label} inventory")
        if names:
            existing_name = names[0]
            existing = _read_regular_at(directory_fd, existing_name, label)
            if existing_name != expected_name or existing != expected_contents:
                raise RuntimeError(f"conflicting ambiguous {label} inventory")

    @staticmethod
    def _ensure_empty_inventory(directory_fd: int, label: str) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise ValueError(f"{label} inventory cannot be read safely") from exc
        for name in names:
            _read_regular_at(directory_fd, name, label)
        if names:
            raise ValueError(f"derived disposition {label} inventory must be empty")

    def _manifest_base(
        self,
        query: LogicalQuery,
        *,
        attempt_number: int | None,
        raw_sha256: str,
        raw_byte_count: int,
        sidecar_name: str,
        sidecar_sha256: str,
        storage_mode: str,
    ) -> dict[str, object]:
        query_id = logical_query_id(query)
        registration_sha256 = self._require_lease(query)._registration_sha256
        return {
            "attempt_history": self._attempt_history(query),
            "attempt_number": attempt_number,
            "grain": query.grain,
            "query_id": query_id,
            "query_path": _relative_path(query.source, query_id, "query.json"),
            "query_sha256": registration_sha256,
            "query_sidecar_path": _relative_path(
                query.source, query_id, "sidecars", sidecar_name
            ),
            "query_sidecar_sha256": sidecar_sha256,
            "raw_byte_count": raw_byte_count,
            "raw_sha256": raw_sha256,
            "schema_version": 1,
            "source": query.source,
            "storage_mode": storage_mode,
        }

    def publish_fetched(
        self,
        query: LogicalQuery,
        attempt_number: int,
        capture: AttemptCapture,
        query_sidecar: Mapping[str, object],
    ) -> PublishedEvidence:
        self._require_lease(query)
        if self._recover_pending_before_mutation(query) is not None:
            raise RuntimeError(
                "pending failure transaction recovered; success publication refused"
            )
        self._ensure_unpublished(query)
        _validate_capture(capture)
        if capture.body is None:
            raise ValueError("fetched success requires response bytes")
        if capture.http_status is None or not 200 <= capture.http_status <= 299:
            raise ValueError("fetched success requires a successful HTTP status")
        attempt = _attempt_record(
            logical_query_id(query), attempt_number, capture, "SUCCESS"
        )
        _validate_query_byte_count(query, attempt["byte_count"])
        query_id, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            with ExitStack() as opened:
                attempts_fd = _open_directory_at(query_fd, "attempts", create=False)
                opened.callback(os.close, attempts_fd)
                objects_fd = _open_directory_at(query_fd, "objects", create=False)
                opened.callback(os.close, objects_fd)
                sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
                opened.callback(os.close, sidecars_fd)
                self._validate_attempt_reservation(query, attempt_number, attempts_fd)
                self._verified_attempt_bundle(query, attempts_fd)
                body = capture.body
                raw_sha256 = _sha256_bytes(body)
                raw_name = f"{raw_sha256}.raw"
                sidecar_name, sidecar_sha256, sidecar_contents = self._sidecar_artifact(
                    query,
                    raw_sha256,
                    len(body),
                    query_sidecar,
                )
                self._ensure_content_slot(objects_fd, raw_name, body, "query object")
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                _write_once_at(
                    attempts_fd,
                    f"{attempt_number:06d}.json",
                    _canonical_file_bytes(attempt),
                    "attempt receipt",
                )
                self._validated_fetched_success_attempt(
                    query, attempt_number, raw_sha256, len(body)
                )
                _write_once_at(objects_fd, raw_name, body, "fetched raw object")
                _write_once_at(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query semantic sidecar",
                )
                self._ensure_content_slot(objects_fd, raw_name, body, "query object")
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                manifest = {
                    **self._manifest_base(
                        query,
                        attempt_number=attempt_number,
                        raw_sha256=raw_sha256,
                        raw_byte_count=len(body),
                        sidecar_name=sidecar_name,
                        sidecar_sha256=sidecar_sha256,
                        storage_mode="FETCHED_RAW",
                    ),
                    "fetched_raw_path": _relative_path(
                        query.source, query_id, "objects", raw_name
                    ),
                }
                self._verify_query_chain(query, descriptors)
                self._require_lease(query)
                current_history = self._validated_fetched_success_attempt(
                    query, attempt_number, raw_sha256, len(body)
                )
                if manifest["attempt_history"] != current_history:
                    raise ValueError(
                        "successful attempt history changed before publish"
                    )
                _write_once_at(
                    query_fd,
                    "success.json",
                    _canonical_file_bytes(manifest),
                    "success receipt",
                )
        finally:
            self._close_fds(descriptors)
        published = self.verify_success(query)
        self._require_lease(query)
        return published

    def _legacy_repository_root(self) -> Path:
        if self.root.parts[-len(_V52_RAW_RELATIVE.parts) :] != _V52_RAW_RELATIVE.parts:
            raise ValueError(
                "protected legacy reference requires the canonical raw root"
            )
        repository = self.root
        for _ in _V52_RAW_RELATIVE.parts:
            repository = repository.parent
        if self.root.relative_to(repository) != _V52_RAW_RELATIVE:
            raise ValueError("protected legacy repository binding differs")
        for node in (*reversed(repository.parents), repository):
            metadata = node.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("protected legacy root has a symlink ancestor")
        return repository

    @staticmethod
    def _validated_relative_path(value: object, label: str) -> Path:
        if (
            type(value) is not str
            or not value
            or value.startswith("/")
            or "\\" in value
            or any(part in {"", ".", ".."} for part in Path(value).parts)
        ):
            raise ValueError(f"{label} path is invalid")
        return Path(value)

    def _read_repository_snapshot(
        self, repository: Path, value: object, label: str
    ) -> _RepositoryFileSnapshot:
        relative = self._validated_relative_path(value, label)
        if not repository.is_absolute():
            raise ValueError("protected legacy repository must be absolute")
        descriptors = [os.open(os.sep, _directory_flags())]
        ancestor_identities: list[tuple[int, int]] = []
        try:
            root_metadata = os.fstat(descriptors[0])
            ancestor_identities.append(
                (root_metadata.st_dev, root_metadata.st_ino)
            )
            for component in (*repository.parts[1:], *relative.parts[:-1]):
                child = _open_directory_at(descriptors[-1], component, create=False)
                descriptors.append(child)
                metadata = os.fstat(child)
                ancestor_identities.append((metadata.st_dev, metadata.st_ino))
            file_snapshot = _read_regular_snapshot_at(
                descriptors[-1], relative.name, label
            )
            if file_snapshot is None:
                raise RuntimeError(f"{label} is missing")
            return _RepositoryFileSnapshot(
                label=label,
                relative_path=relative.as_posix(),
                ancestor_identities=tuple(ancestor_identities),
                file=file_snapshot,
            )
        finally:
            self._close_fds(descriptors)

    def _rebind_legacy_lineage(
        self, expected: tuple[_RepositoryFileSnapshot, ...]
    ) -> None:
        repository = self._legacy_repository_root()
        for snapshot in expected:
            current = self._read_repository_snapshot(
                repository,
                snapshot.relative_path,
                snapshot.label,
            )
            if current != snapshot:
                raise ValueError(
                    f"{snapshot.label} lineage identity or bytes changed"
                )

    def _validated_legacy_reference(
        self,
        query: LogicalQuery,
        legacy_reference: Mapping[str, object],
        semantics: Mapping[str, object] | None,
    ) -> _ValidatedLegacyReference:
        reference = _normalized_json_object(legacy_reference, "legacy_reference")
        if set(reference) != _LEGACY_REFERENCE_KEYS:
            raise ValueError("legacy reference lineage keys differ")
        current_query_id = _validate_sha256(
            reference["current_query_id"], "current_query_id"
        )
        if current_query_id != logical_query_id(query):
            raise ValueError("legacy reference current query identity differs")
        repository = self._legacy_repository_root()
        if (
            reference["protected_inventory_path"]
            != _PROTECTED_INVENTORY_RELATIVE.as_posix()
        ):
            raise ValueError("protected inventory lineage path differs")
        inventory_sha256 = _validate_sha256(
            reference["protected_inventory_sha256"],
            "protected_inventory_sha256",
        )
        try:
            inventory_snapshot = self._read_repository_snapshot(
                repository,
                reference["protected_inventory_path"],
                "protected raw inventory",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                f"protected publisher inventory snapshot failed: {exc}"
            ) from exc
        inventory_bytes = inventory_snapshot.file.contents
        if _sha256_bytes(inventory_bytes) != inventory_sha256:
            raise ProtectedLegacyPublishReverifyError(
                "protected inventory lineage hash differs"
            )
        try:
            inventory = parse_protected_raw_inventory_document(inventory_bytes)
            membership = snapshot_protected_inventory_membership(
                repository, inventory
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                "protected publisher inventory revalidation failed"
            ) from exc
        try:
            rebound_inventory = self._read_repository_snapshot(
                repository,
                reference["protected_inventory_path"],
                "protected raw inventory",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                "protected publisher inventory rebound failed"
            ) from exc
        if rebound_inventory != inventory_snapshot:
            raise ProtectedLegacyPublishReverifyError(
                "protected inventory changed during verification"
            )
        legacy_source = _validate_segment(
            reference["legacy_source"], "legacy_source"
        )
        if (
            reference["source"] != query.source
            or reference["parser_version"] != query.parser_version
        ):
            raise ValueError("legacy reference parser or source binding differs")
        canonical_query = _normalized_json_object(
            reference["canonical_query"],
            "legacy canonical_query",  # type: ignore[arg-type]
        )
        if not matches_current_query_to_legacy(
            query,
            legacy_source=legacy_source,
            legacy_query=canonical_query,
            protected_inventory_sha256=inventory_sha256,
        ):
            raise ValueError("legacy reference current-query binding differs")
        digest = _validate_sha256(reference.get("raw_sha256"), "raw_sha256")
        byte_count = reference.get("raw_byte_count")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise ValueError("raw_byte_count must be a nonnegative integer")
        row_count = reference.get("raw_row_count")
        if type(row_count) is not int or row_count < 0:
            raise ValueError("raw_row_count must be a nonnegative integer")
        for key in (
            "legacy_manifest_sha256",
            "legacy_seal_sha256",
            "semantic_sha256",
        ):
            _validate_sha256(reference[key], key)
        matches = [
            record
            for record in inventory["records"]  # type: ignore[index]
            if record["source"] == legacy_source
            and record["canonical_query"] == canonical_query
            and record["manifest"]["path"] == reference["legacy_manifest_path"]
            and record["manifest"]["sha256"] == reference["legacy_manifest_sha256"]
            and record["seal"]["path"] == reference["legacy_seal_path"]
            and record["seal"]["sha256"] == reference["legacy_seal_sha256"]
            and record["raw"]["path"] == reference["payload_path"]
            and record["raw"]["sha256"] == digest
            and record["raw"]["byte_size"] == byte_count
        ]
        if len(matches) != 1:
            raise ProtectedLegacyPublishReverifyError(
                "legacy reference is not an exact Task0 inventory record"
            )
        try:
            manifest_snapshot = self._read_repository_snapshot(
                repository,
                reference["legacy_manifest_path"],
                "protected legacy manifest",
            )
            seal_snapshot = self._read_repository_snapshot(
                repository,
                reference["legacy_seal_path"],
                "protected legacy seal",
            )
            payload_snapshot = self._read_repository_snapshot(
                repository,
                reference["payload_path"],
                "protected legacy payload",
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                "protected publisher selected lineage snapshot failed"
            ) from exc
        manifest_bytes = manifest_snapshot.file.contents
        seal_bytes = seal_snapshot.file.contents
        payload = payload_snapshot.file.contents
        if _sha256_bytes(manifest_bytes) != reference["legacy_manifest_sha256"]:
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy manifest hash differs"
            )
        if _sha256_bytes(seal_bytes) != reference["legacy_seal_sha256"]:
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy seal hash differs"
            )
        if seal_bytes != f"{_sha256_bytes(manifest_bytes)}\n".encode("ascii"):
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy manifest seal differs"
            )
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy manifest is invalid"
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("source") != legacy_source
            or manifest.get("query") != canonical_query
            or manifest.get("sha256") != digest
            or manifest.get("row_count") != row_count
        ):
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy manifest binding differs"
            )
        if len(payload) != byte_count or _sha256_bytes(payload) != digest:
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy payload binding differs"
            )
        if sum(bool(line.strip()) for line in payload.splitlines()) != row_count:
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy payload row count differs"
            )
        if semantics is not None:
            semantic_record = _normalized_json_object(semantics, "query_sidecar")
            if (
                _sha256_bytes(_canonical_json_bytes(semantic_record))
                != reference["semantic_sha256"]
            ):
                raise ValueError("legacy semantic hash binding differs")
        try:
            rebound_membership = snapshot_protected_inventory_membership(
                repository, inventory
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                "protected publisher membership revalidation failed"
            ) from exc
        if rebound_membership != membership:
            raise ProtectedLegacyPublishReverifyError(
                "protected legacy membership changed during validation"
            )
        return _ValidatedLegacyReference(
            reference=reference,
            payload=payload,
            lineage_snapshots=(
                inventory_snapshot,
                manifest_snapshot,
                seal_snapshot,
                payload_snapshot,
            ),
            protected_inventory=inventory,
            protected_membership=membership,
        )

    def publish_legacy_reference(
        self,
        query: LogicalQuery,
        legacy_reference: Mapping[str, object],
        query_sidecar: Mapping[str, object],
    ) -> PublishedEvidence:
        self._require_lease(query)
        if self._recover_pending_before_mutation(query) is not None:
            raise RuntimeError(
                "pending failure transaction recovered; success publication refused"
            )
        self._ensure_unpublished(query)
        validated = self._validated_legacy_reference(
            query, legacy_reference, query_sidecar
        )
        reference = validated.reference
        payload = validated.payload
        lineage_snapshots = validated.lineage_snapshots
        repository = self._legacy_repository_root()
        raw_sha256 = _validate_sha256(reference["raw_sha256"], "raw_sha256")
        query_id, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            with ExitStack() as opened:
                objects_fd = _open_directory_at(query_fd, "objects", create=False)
                opened.callback(os.close, objects_fd)
                sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
                opened.callback(os.close, sidecars_fd)
                reference_contents = _canonical_file_bytes(reference)
                reference_sha256 = _sha256_bytes(reference_contents)
                reference_name = f"{reference_sha256}.legacy-reference.json"
                sidecar_name, sidecar_sha256, sidecar_contents = self._sidecar_artifact(
                    query,
                    raw_sha256,
                    len(payload),
                    query_sidecar,
                )
                self._ensure_content_slot(
                    objects_fd,
                    reference_name,
                    reference_contents,
                    "query object",
                )
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                _write_once_at(
                    objects_fd,
                    reference_name,
                    reference_contents,
                    "protected legacy reference",
                )
                _write_once_at(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query semantic sidecar",
                )
                self._ensure_content_slot(
                    objects_fd,
                    reference_name,
                    reference_contents,
                    "query object",
                )
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                reference_snapshot = _read_regular_snapshot_at(
                    objects_fd,
                    reference_name,
                    "protected legacy reference",
                )
                if reference_snapshot is None:
                    raise RuntimeError("protected legacy reference is missing")
                sidecar_snapshot = _read_regular_snapshot_at(
                    sidecars_fd,
                    sidecar_name,
                    "query semantic sidecar",
                )
                if sidecar_snapshot is None:
                    raise RuntimeError("query semantic sidecar is missing")
                manifest = {
                    **self._manifest_base(
                        query,
                        attempt_number=None,
                        raw_sha256=raw_sha256,
                        raw_byte_count=len(payload),
                        sidecar_name=sidecar_name,
                        sidecar_sha256=sidecar_sha256,
                        storage_mode="PROTECTED_LEGACY_REFERENCE",
                    ),
                    "protected_legacy_reference_path": _relative_path(
                        query.source,
                        query_id,
                        "objects",
                        reference_name,
                    ),
                    "protected_legacy_reference_sha256": reference_sha256,
                }
                try:
                    self._rebind_legacy_lineage(lineage_snapshots)
                    rebound_membership = (
                        snapshot_protected_inventory_membership(
                            repository,
                            validated.protected_inventory,
                        )
                    )
                    if rebound_membership != validated.protected_membership:
                        raise ValueError(
                            "protected legacy membership changed before publication"
                        )
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ProtectedLegacyPublishReverifyError(
                        "protected publisher lineage revalidation failed"
                    ) from exc
                _rebind_regular_snapshot_at(
                    objects_fd,
                    reference_name,
                    reference_snapshot,
                    "protected legacy reference",
                )
                _rebind_regular_snapshot_at(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_snapshot,
                    "query semantic sidecar",
                )
                self._verify_query_chain(query, descriptors)
                self._require_lease(query)
                _write_once_at(
                    query_fd,
                    "success.json",
                    _canonical_file_bytes(manifest),
                    "success receipt",
                )
        finally:
            self._close_fds(descriptors)
        try:
            published = self.verify_success(query)
        except ProtectedLegacyPublishReverifyError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProtectedLegacyPublishReverifyError(
                "protected publisher success verification failed"
            ) from exc
        self._require_lease(query)
        return published

    def publish_derived_disposition(
        self,
        query: LogicalQuery,
        query_sidecar: Mapping[str, object],
        *,
        authority: object | None = None,
    ) -> PublishedEvidence:
        self._require_lease(query)
        if self._recover_pending_before_mutation(query, authority=authority) is not None:
            raise RuntimeError(
                "pending failure transaction recovered; success publication refused"
            )
        self._ensure_unpublished(query, authority=authority)
        sidecar_name, sidecar_sha256, sidecar_contents = (
            self._derived_sidecar_artifact(
                query, query_sidecar, authority=authority
            )
        )
        query_id, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            registration_sha256 = self._validate_registered(query, query_fd)
            with ExitStack() as opened:
                attempts_fd = _open_directory_at(
                    query_fd, "attempts", create=False
                )
                opened.callback(os.close, attempts_fd)
                objects_fd = _open_directory_at(query_fd, "objects", create=False)
                opened.callback(os.close, objects_fd)
                sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
                opened.callback(os.close, sidecars_fd)
                reservations, receipts = self._attempt_numbers(
                    attempts_fd, query_id
                )
                if reservations or receipts:
                    raise ValueError(
                        "derived disposition cannot own attempts or reservations"
                    )
                self._ensure_empty_inventory(objects_fd, "query object")
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                _write_once_at(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "derived disposition sidecar",
                )
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                sidecar_snapshot = _read_regular_snapshot_at(
                    sidecars_fd,
                    sidecar_name,
                    "derived disposition sidecar",
                )
                if sidecar_snapshot is None:
                    raise RuntimeError("derived disposition sidecar is missing")
                sidecar_descriptor = os.open(
                    sidecar_name,
                    _regular_read_flags(),
                    dir_fd=sidecars_fd,
                )
                opened.callback(os.close, sidecar_descriptor)
                opened_sidecar = os.fstat(sidecar_descriptor)
                linked_sidecar = os.stat(
                    sidecar_name,
                    dir_fd=sidecars_fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(opened_sidecar.st_mode)
                    or opened_sidecar.st_nlink != 1
                    or _identity(opened_sidecar) != _identity(linked_sidecar)
                    or _identity(opened_sidecar) != sidecar_snapshot.identity
                ):
                    raise ValueError(
                        "derived disposition sidecar ownership differs"
                    )
                manifest = {
                    "attempt_history": [],
                    "attempt_number": None,
                    "grain": query.grain,
                    "query_id": query_id,
                    "query_path": _relative_path(
                        query.source, query_id, "query.json"
                    ),
                    "query_sha256": registration_sha256,
                    "query_sidecar_path": _relative_path(
                        query.source, query_id, "sidecars", sidecar_name
                    ),
                    "query_sidecar_sha256": sidecar_sha256,
                    "schema_version": 1,
                    "source": query.source,
                    "storage_mode": "DERIVED_DISPOSITION",
                }
                try:
                    self._verify_query_chain(query, descriptors)
                    self._require_lease(query)
                    reservations, receipts = self._attempt_numbers(
                        attempts_fd, query_id
                    )
                    if reservations or receipts:
                        raise ValueError(
                            "derived disposition attempt inventory changed"
                        )
                    self._ensure_empty_inventory(objects_fd, "query object")
                    _rebind_regular_snapshot_at(
                        sidecars_fd,
                        sidecar_name,
                        sidecar_snapshot,
                        "derived disposition sidecar",
                    )
                    self._derived_semantics(
                        query, query_sidecar, authority=authority
                    )
                    _write_once_at(
                        query_fd,
                        "success.json",
                        _canonical_file_bytes(manifest),
                        "success receipt",
                    )
                except BaseException:
                    try:
                        os.stat(
                            "success.json",
                            dir_fd=query_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        current_sidecar = _read_regular_snapshot_at(
                            sidecars_fd,
                            sidecar_name,
                            "derived disposition sidecar",
                            missing_ok=True,
                        )
                        if (
                            current_sidecar is not None
                            and current_sidecar != sidecar_snapshot
                        ):
                            _cleanup_created_file(
                                sidecars_fd,
                                sidecar_descriptor,
                                sidecar_name,
                                "derived disposition sidecar",
                            )
                    raise
        finally:
            self._close_fds(descriptors)
        published = self.verify_success(query, authority=authority)
        self._require_lease(query)
        return published

    def load_success(
        self, query: LogicalQuery, *, authority: object | None = None
    ) -> PublishedEvidence | None:
        verified = self._read_verified_success(
            query, missing_ok=True, authority=authority
        )
        return None if verified is None else verified.published_evidence

    def _read_bound_relative(
        self, relative: str, expected_prefix: tuple[str, ...], label: str
    ) -> bytes:
        if (
            not isinstance(relative, str)
            or Path(relative).parts[:-1] != expected_prefix
        ):
            raise ValueError(f"{label} path binding differs")
        parts = Path(relative).parts
        root_fd = self._root_fd()
        descriptors = [root_fd]
        try:
            for component in parts[:-1]:
                child = _open_directory_at(descriptors[-1], component, create=False)
                descriptors.append(child)
            return _read_regular_at(descriptors[-1], parts[-1], label)
        finally:
            self._close_fds(descriptors)

    def _verify_published_content_slots(
        self,
        query: LogicalQuery,
        *,
        object_name: str,
        object_contents: bytes,
        sidecar_name: str,
        sidecar_contents: bytes,
    ) -> None:
        _, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            with ExitStack() as opened:
                objects_fd = _open_directory_at(query_fd, "objects", create=False)
                opened.callback(os.close, objects_fd)
                sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
                opened.callback(os.close, sidecars_fd)
                self._ensure_content_slot(
                    objects_fd, object_name, object_contents, "query object"
                )
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
        finally:
            self._close_fds(descriptors)

    def _read_verified_success(
        self,
        query: LogicalQuery,
        *,
        missing_ok: bool,
        authority: object | None = None,
        cleanup_tombstones: bool = True,
        lock_descriptor: int | None = None,
        inspection: Callable[[VerifiedEvidenceRead], None] | None = None,
    ) -> VerifiedEvidenceRead | None:
        try:
            query_id, descriptors = self._query_fds(query, create=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        owned_lock_descriptor = -1
        recovery_canary_started = False
        try:
            query_fd = descriptors[-1]
            active_lock_descriptor = lock_descriptor
            if self._read_only:
                if active_lock_descriptor is None:
                    owned_lock_descriptor = self._open_query_lock(
                        query_fd,
                        create=False,
                        read_only=True,
                    )
                    fcntl.flock(owned_lock_descriptor, fcntl.LOCK_SH)
                    active_lock_descriptor = owned_lock_descriptor
                self._require_no_failure_recovery_state(query_fd)
                recovery_canary_started = True
            if (
                cleanup_tombstones
                and not self._read_only
                and query_id not in self._active_leases()
            ):
                self._finish_failure_cleanup_before_read(query, descriptors)
            registration_sha256, registered_query, query_snapshot = (
                self._validated_registered_query(
                    query,
                    query_fd,
                    lock_descriptor=active_lock_descriptor,
                )
            )
            success_snapshot = _read_regular_snapshot_at(
                query_fd,
                "success.json",
                "success receipt",
                missing_ok=missing_ok,
            )
            if success_snapshot is None:
                return None
            success_contents = success_snapshot.contents
            manifest = _json_object(success_contents, "success receipt")
            raw_common_keys = {
                "attempt_history",
                "attempt_number",
                "grain",
                "query_id",
                "query_path",
                "query_sha256",
                "query_sidecar_path",
                "query_sidecar_sha256",
                "raw_byte_count",
                "raw_sha256",
                "schema_version",
                "source",
                "storage_mode",
            }
            derived_keys = raw_common_keys - {"raw_byte_count", "raw_sha256"}
            mode = manifest.get("storage_mode")
            if mode == "FETCHED_RAW":
                if set(manifest) != raw_common_keys | {"fetched_raw_path"}:
                    raise ValueError("fetched success receipt keys differ")
            elif mode == "PROTECTED_LEGACY_REFERENCE":
                if set(manifest) != raw_common_keys | {
                    "protected_legacy_reference_path",
                    "protected_legacy_reference_sha256",
                }:
                    raise ValueError("protected success receipt keys differ")
            elif mode == "DERIVED_DISPOSITION":
                if set(manifest) != derived_keys:
                    raise ValueError("derived success receipt keys differ")
            else:
                raise ValueError("success storage_mode differs")
            if (
                manifest.get("schema_version") != 1
                or manifest.get("query_id") != query_id
                or manifest.get("query_sha256") != registration_sha256
                or manifest.get("source") != query.source
                or manifest.get("grain") != query.grain
                or manifest.get("query_path")
                != _relative_path(query.source, query_id, "query.json")
            ):
                raise ValueError("success query binding differs")

            with ExitStack() as opened:
                attempts_fd = _open_directory_at(
                    query_fd, "attempts", create=False
                )
                opened.callback(os.close, attempts_fd)
                objects_fd = _open_directory_at(query_fd, "objects", create=False)
                opened.callback(os.close, objects_fd)
                sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
                opened.callback(os.close, sidecars_fd)
                reservations, history, attempt_receipts, attempt_snapshots = (
                    self._verified_attempt_bundle(query, attempts_fd)
                )
                if manifest.get("attempt_history") != history:
                    raise ValueError("success attempt history binding differs")

                legacy_reference_record: dict[str, object] | None = None
                legacy_validation: _ValidatedLegacyReference | None = None
                legacy_lineage_snapshots: tuple[
                    _RepositoryFileSnapshot, ...
                ] = ()
                raw_sha256: str | None
                raw_bytes: bytes | None
                if mode == "FETCHED_RAW":
                    attempt_number = manifest.get("attempt_number")
                    raw_sha256 = _validate_sha256(
                        manifest.get("raw_sha256"), "success raw_sha256"
                    )
                    raw_byte_count = manifest.get("raw_byte_count")
                    receipt_numbers = {
                        int(record["attempt_number"])
                        for record in attempt_receipts
                    }
                    if (
                        isinstance(attempt_number, bool)
                        or not isinstance(attempt_number, int)
                        or not reservations
                        or not receipt_numbers
                        or attempt_number != max(reservations)
                        or attempt_number != max(receipt_numbers)
                    ):
                        raise ValueError(
                            "successful attempt is not the current finalized tail"
                        )
                    final_attempt = attempt_receipts[-1]
                    if (
                        final_attempt.get("outcome") != "SUCCESS"
                        or final_attempt.get("body_sha256") != raw_sha256
                        or final_attempt.get("byte_count") != raw_byte_count
                    ):
                        raise ValueError("successful attempt raw binding differs")
                    object_path = manifest.get("fetched_raw_path")
                    object_name = f"{raw_sha256}.raw"
                    if object_path != _relative_path(
                        query.source, query_id, "objects", object_name
                    ):
                        raise ValueError(
                            "fetched raw content-addressed path binding differs"
                        )
                    object_snapshot = _read_regular_snapshot_at(
                        objects_fd, object_name, "fetched raw object"
                    )
                    if object_snapshot is None:
                        raise RuntimeError("fetched raw object is missing")
                    raw_bytes = object_snapshot.contents
                    object_contents = raw_bytes
                elif mode == "PROTECTED_LEGACY_REFERENCE":
                    if manifest.get("attempt_number") is not None:
                        raise ValueError("protected success attempt binding differs")
                    raw_sha256 = _validate_sha256(
                        manifest.get("raw_sha256"), "success raw_sha256"
                    )
                    raw_byte_count = manifest.get("raw_byte_count")
                    reference_sha256 = _validate_sha256(
                        manifest.get("protected_legacy_reference_sha256"),
                        "protected reference SHA-256",
                    )
                    object_name = (
                        f"{reference_sha256}.legacy-reference.json"
                    )
                    if manifest.get("protected_legacy_reference_path") != _relative_path(
                        query.source, query_id, "objects", object_name
                    ):
                        raise ValueError(
                            "protected reference content-addressed path binding differs"
                        )
                    object_snapshot = _read_regular_snapshot_at(
                        objects_fd, object_name, "protected legacy reference"
                    )
                    if object_snapshot is None:
                        raise RuntimeError("protected legacy reference is missing")
                    object_contents = object_snapshot.contents
                    if _sha256_bytes(object_contents) != reference_sha256:
                        raise ValueError("protected legacy reference hash differs")
                    reference = _json_object(
                        object_contents, "protected legacy reference"
                    )
                    legacy_validation = self._validated_legacy_reference(
                        query, reference, None
                    )
                    legacy_reference_record = legacy_validation.reference
                    raw_bytes = legacy_validation.payload
                    legacy_lineage_snapshots = (
                        legacy_validation.lineage_snapshots
                    )
                else:
                    if (
                        manifest.get("attempt_number") is not None
                        or reservations
                        or attempt_receipts
                    ):
                        raise ValueError(
                            "derived disposition cannot bind attempts or reservations"
                        )
                    self._ensure_empty_inventory(objects_fd, "query object")
                    object_name = None
                    object_contents = None
                    object_snapshot = None
                    raw_sha256 = None
                    raw_byte_count = None
                    raw_bytes = None

                if mode != "DERIVED_DISPOSITION":
                    if (
                        isinstance(raw_byte_count, bool)
                        or not isinstance(raw_byte_count, int)
                        or raw_byte_count < 0
                        or raw_bytes is None
                        or len(raw_bytes) != raw_byte_count
                        or _sha256_bytes(raw_bytes) != raw_sha256
                    ):
                        raise ValueError("success raw binding differs")
                    self._ensure_content_slot(
                        objects_fd,
                        object_name,  # type: ignore[arg-type]
                        object_contents,  # type: ignore[arg-type]
                        "query object",
                    )

                sidecar_sha256 = _validate_sha256(
                    manifest.get("query_sidecar_sha256"),
                    "query sidecar SHA-256",
                )
                sidecar_name = f"{sidecar_sha256}.json"
                if manifest.get("query_sidecar_path") != _relative_path(
                    query.source, query_id, "sidecars", sidecar_name
                ):
                    raise ValueError(
                        "query sidecar content-addressed path binding differs"
                    )
                sidecar_snapshot = _read_regular_snapshot_at(
                    sidecars_fd, sidecar_name, "query semantic sidecar"
                )
                if sidecar_snapshot is None:
                    raise RuntimeError("query semantic sidecar is missing")
                sidecar_contents = sidecar_snapshot.contents
                if _sha256_bytes(sidecar_contents) != sidecar_sha256:
                    raise ValueError("query sidecar hash differs")
                self._ensure_content_slot(
                    sidecars_fd,
                    sidecar_name,
                    sidecar_contents,
                    "query sidecar",
                )
                sidecar = _json_object(
                    sidecar_contents, "query semantic sidecar"
                )
                if mode == "DERIVED_DISPOSITION":
                    if set(sidecar) != {
                        "grain",
                        "parser_version",
                        "query_id",
                        "schema_version",
                        "semantics",
                        "source",
                        "storage_mode",
                    } or any(
                        (
                            sidecar.get("schema_version") != 1,
                            sidecar.get("storage_mode")
                            != "DERIVED_DISPOSITION",
                            sidecar.get("query_id") != query_id,
                            sidecar.get("source") != query.source,
                            sidecar.get("grain") != query.grain,
                            sidecar.get("parser_version") != query.parser_version,
                            not isinstance(sidecar.get("semantics"), Mapping),
                        )
                    ):
                        raise ValueError(
                            "derived disposition sidecar binding differs"
                        )
                    self._derived_semantics(
                        query,
                        sidecar["semantics"],  # type: ignore[arg-type]
                        authority=authority,
                    )
                elif set(sidecar) != {
                    "byte_count",
                    "grain",
                    "parser_version",
                    "query_id",
                    "raw_sha256",
                    "schema_version",
                    "semantics",
                    "source",
                } or any(
                    (
                        sidecar.get("schema_version") != 1,
                        sidecar.get("query_id") != query_id,
                        sidecar.get("source") != query.source,
                        sidecar.get("grain") != query.grain,
                        sidecar.get("parser_version") != query.parser_version,
                        sidecar.get("raw_sha256") != raw_sha256,
                        sidecar.get("byte_count") != raw_byte_count,
                        not isinstance(sidecar.get("semantics"), dict),
                    )
                ):
                    raise ValueError("query sidecar binding differs")
                if (
                    legacy_reference_record is not None
                    and _sha256_bytes(
                        _canonical_json_bytes(sidecar["semantics"])
                    )
                    != legacy_reference_record["semantic_sha256"]
                ):
                    raise ValueError("legacy semantic hash binding differs")
                published = PublishedEvidence(
                    query_id=query_id,
                    success_receipt_path=self.root
                    / "queries"
                    / query.source
                    / query_id
                    / "success.json",
                    raw_sha256=raw_sha256,
                    query_sidecar_sha256=sidecar_sha256,
                    storage_mode=mode,  # type: ignore[arg-type]
                )
                verified_result = VerifiedEvidenceRead(
                    registered_query=registered_query,
                    published_evidence=published,
                    raw_bytes=raw_bytes,
                    query_sidecar=sidecar,
                    success_receipt=manifest,
                    attempt_receipts=attempt_receipts,
                )

                def rebind_verified_read() -> None:
                    for name, descriptor, label in (
                        ("attempts", attempts_fd, "attempt namespace"),
                        ("objects", objects_fd, "query object namespace"),
                        ("sidecars", sidecars_fd, "query sidecar namespace"),
                    ):
                        _rebind_directory_at(
                            query_fd,
                            name,
                            descriptor,
                            label,
                        )
                    (
                        final_reservations,
                        final_history,
                        final_attempt_receipts,
                        final_attempt_snapshots,
                    ) = self._verified_attempt_bundle(query, attempts_fd)
                    if (
                        final_reservations != reservations
                        or final_history != history
                        or final_attempt_receipts != attempt_receipts
                        or final_attempt_snapshots != attempt_snapshots
                    ):
                        raise ValueError(
                            "attempt namespace or history changed during verified read"
                        )
                    if mode == "DERIVED_DISPOSITION":
                        self._ensure_empty_inventory(
                            objects_fd, "query object"
                        )
                    else:
                        self._ensure_content_slot(
                            objects_fd,
                            object_name,  # type: ignore[arg-type]
                            object_contents,  # type: ignore[arg-type]
                            "query object binding",
                        )
                    if object_snapshot is not None and object_name is not None:
                        _rebind_regular_snapshot_at(
                            objects_fd,
                            object_name,
                            object_snapshot,
                            "query object",
                        )
                    self._ensure_content_slot(
                        sidecars_fd,
                        sidecar_name,
                        sidecar_contents,
                        "query sidecar binding",
                    )
                    _rebind_regular_snapshot_at(
                        sidecars_fd,
                        sidecar_name,
                        sidecar_snapshot,
                        "query semantic sidecar",
                    )
                    self._verify_query_chain(query, descriptors)
                    _rebind_regular_snapshot_at(
                        query_fd,
                        "success.json",
                        success_snapshot,
                        "success receipt",
                    )
                    (
                        final_registration_sha256,
                        final_registered_query,
                        final_query_snapshot,
                    ) = self._validated_registered_query(
                        query,
                        query_fd,
                        lock_descriptor=active_lock_descriptor,
                    )
                    if (
                        final_registration_sha256 != registration_sha256
                        or final_registered_query != registered_query
                        or final_query_snapshot != query_snapshot
                    ):
                        raise ValueError(
                            "logical query registration changed during read"
                        )
                    if legacy_lineage_snapshots:
                        self._rebind_legacy_lineage(legacy_lineage_snapshots)
                    if legacy_validation is not None:
                        rebound_membership = (
                            snapshot_protected_inventory_membership(
                                self._legacy_repository_root(),
                                legacy_validation.protected_inventory,
                            )
                        )
                        if (
                            rebound_membership
                            != legacy_validation.protected_membership
                        ):
                            raise ValueError(
                                "protected legacy membership changed during read"
                            )

                if inspection is None:
                    rebind_verified_read()
                else:
                    try:
                        inspection(verified_result)
                    finally:
                        rebind_verified_read()
                return verified_result
        finally:
            try:
                if recovery_canary_started:
                    self._require_no_failure_recovery_state(descriptors[-1])
            finally:
                if owned_lock_descriptor >= 0:
                    fcntl.flock(owned_lock_descriptor, fcntl.LOCK_UN)
                    os.close(owned_lock_descriptor)
                self._close_fds(descriptors)

    def read_verified_success(
        self, query: LogicalQuery, *, authority: object | None = None
    ) -> VerifiedEvidenceRead:
        verified = self._read_verified_success(
            query, missing_ok=False, authority=authority
        )
        if verified is None:
            raise RuntimeError("success receipt is missing")
        return verified

    def inspect_verified_success(
        self,
        query: LogicalQuery,
        inspection: Callable[[VerifiedEvidenceRead], None],
        *,
        authority: object | None = None,
    ) -> VerifiedEvidenceRead:
        if not self._read_only:
            raise ValueError(
                "verified success inspection requires a read-only evidence store"
            )
        if not callable(inspection):
            raise TypeError("inspection must be callable")
        verified = self._read_verified_success(
            query,
            missing_ok=False,
            inspection=inspection,
            authority=authority,
        )
        if verified is None:
            raise RuntimeError("success receipt is missing")
        return verified

    def read_verified_query_progress(
        self, query: LogicalQuery, *, authority: object | None = None
    ) -> VerifiedQueryProgress:
        query_id = logical_query_id(query)
        descriptors: list[int] = []
        for _ in range(2):
            probed_descriptors, quarantine_exists, stable = (
                self._probe_progress_namespaces(query)
            )
            if not stable:
                continue
            if probed_descriptors is not None:
                descriptors = probed_descriptors
                break
            if quarantine_exists:
                raise ValueError(
                    "quarantine directory has no registered query"
                )
            return VerifiedQueryProgress(
                query_id=query_id,
                status="PENDING",
                registered_query=None,
                published_evidence=None,
                attempt_receipts=(),
                pending_attempt_numbers=(),
                quarantines=(),
            )
        else:
            raise RuntimeError("progress evidence root could not be read stably")

        lock_descriptor = -1
        quarantine_descriptors: list[int] | None = None
        try:
            query_fd = descriptors[-1]
            try:
                lock_descriptor = self._open_query_lock(
                    query_fd,
                    create=False,
                    read_only=self._read_only,
                )
            except FileNotFoundError:
                raise RuntimeError("query registration lock is missing") from None
            fcntl.flock(
                lock_descriptor,
                fcntl.LOCK_SH if self._read_only else fcntl.LOCK_EX,
            )
            (
                registration_sha256,
                registered_query,
                query_snapshot,
            ) = self._validated_registered_query(
                query,
                query_fd,
                lock_descriptor=lock_descriptor,
            )
            if self._read_only:
                self._require_no_failure_recovery_state(query_fd)
            else:
                self._finish_failure_cleanup_tombstones(
                    query, query_fd, registration_sha256
                )
            attempts_fd = _open_directory_at(query_fd, "attempts", create=False)
            try:
                (
                    reservations,
                    history,
                    attempt_receipts,
                    attempt_snapshots,
                ) = self._verified_attempt_bundle(query, attempts_fd)
                del history
                try:
                    intent_fd = _open_directory_at(
                        query_fd, _FAILURE_INTENT_NAME, create=False
                    )
                except FileNotFoundError:
                    intent_fd = -1
                if intent_fd < 0:
                    try:
                        marker_only_fd = _open_directory_at(
                            query_fd, _FAILURE_TRANSACTION_NAME, create=False
                        )
                    except FileNotFoundError:
                        marker_only_fd = -1
                    if marker_only_fd >= 0:
                        os.close(marker_only_fd)
                        raise ValueError(
                            "failure transaction marker has no immutable intent"
                        )
                else:
                    try:
                        intent_contents = _read_regular_at(
                            intent_fd,
                            _FAILURE_INTENT_MARKER,
                            "failure intent marker",
                        )
                        transaction_receipt, transaction_contents = (
                            _decode_failure_intent(intent_contents, query)
                        )
                        staged_body = self._read_staged_failure_body(
                            query,
                            intent_fd,
                            transaction_receipt,
                            root_descriptor=descriptors[0],
                        )
                        if (
                            transaction_receipt.get("raw_sha256") is not None
                            and staged_body is None
                        ):
                            try:
                                _, transaction_quarantine = self._quarantine_fds(
                                    query, root_descriptor=descriptors[0]
                                )
                            except FileNotFoundError:
                                raise RuntimeError(
                                    "failure intent body is missing"
                                ) from None
                            try:
                                final_body = _read_regular_at(
                                    transaction_quarantine[-1],
                                    f"{transaction_receipt['raw_sha256']}.raw",
                                    "failure intent final body",
                                )
                                _validate_staged_transaction_body(
                                    transaction_receipt, final_body
                                )
                            finally:
                                self._close_fds(transaction_quarantine)
                        transaction_number = transaction_receipt.get("attempt_number")
                        if (
                            transaction_receipt.get("query_id") != query_id
                            or type(transaction_number) is not int
                            or transaction_number not in reservations
                        ):
                            raise ValueError("failure transaction reservation binding differs")
                        reservation_contents = _read_regular_at(
                            attempts_fd,
                            f"{transaction_number:06d}.reservation.json",
                            "attempt reservation",
                        )
                        intent = _json_object(intent_contents, "failure intent")
                        if intent["reservation_sha256"] != _sha256_bytes(
                            reservation_contents
                        ):
                            raise ValueError(
                                "failure intent reservation binding differs"
                            )
                        if intent["query_registration_sha256"] != registration_sha256:
                            raise ValueError(
                                "failure intent registration binding differs"
                            )
                        try:
                            transaction_fd = _open_directory_at(
                                query_fd,
                                _FAILURE_TRANSACTION_NAME,
                                create=False,
                            )
                        except FileNotFoundError:
                            transaction_fd = -1
                        if transaction_fd >= 0:
                            try:
                                marker = _read_regular_at(
                                    transaction_fd,
                                    _FAILURE_TRANSACTION_MARKER,
                                    "failure transaction marker",
                                )
                                if marker != transaction_contents:
                                    raise ValueError(
                                        "failure transaction marker differs from immutable intent"
                                    )
                            finally:
                                os.close(transaction_fd)
                        projected = {
                            key: transaction_receipt[key] for key in _ATTEMPT_KEYS
                        }
                        _validate_attempt_record(projected, query_id, transaction_number)
                        committed = next(
                            (
                                record
                                for record in attempt_receipts
                                if record.get("attempt_number") == transaction_number
                            ),
                            None,
                        )
                        if committed is not None and dict(committed) != projected:
                            raise ValueError("failure transaction attempt binding differs")
                    finally:
                        os.close(intent_fd)
                    receipt_numbers = {
                        int(record["attempt_number"])
                        for record in attempt_receipts
                    }
                    status = (
                        "QUARANTINED"
                        if committed is not None
                        and committed.get("outcome") == "QUARANTINED"
                        else "PENDING"
                    )
                    return VerifiedQueryProgress(
                        query_id=query_id,
                        status=status,
                        registered_query=registered_query,
                        published_evidence=None,
                        attempt_receipts=attempt_receipts,
                        pending_attempt_numbers=tuple(
                            sorted(reservations - receipt_numbers)
                        ),
                        quarantines=(),
                    )
                success_snapshot = _read_regular_snapshot_at(
                    query_fd,
                    "success.json",
                    "success receipt",
                    missing_ok=True,
                )
                verified_success = (
                    self._read_verified_success(
                        query,
                        missing_ok=False,
                        cleanup_tombstones=False,
                        lock_descriptor=lock_descriptor,
                        authority=authority,
                    )
                    if success_snapshot is not None
                    else None
                )
                if (
                    verified_success is not None
                    and verified_success.attempt_receipts != attempt_receipts
                ):
                    raise ValueError(
                        "success attempt history changed during progress read"
                    )

                quarantine_attempts = tuple(
                    record
                    for record in attempt_receipts
                    if record.get("outcome") in {
                        "QUARANTINED",
                        "SPLIT_REQUIRED",
                    }
                )
                try:
                    _, quarantine_descriptors = self._quarantine_fds(
                        query,
                        root_descriptor=descriptors[0],
                    )
                except FileNotFoundError:
                    if quarantine_attempts:
                        raise RuntimeError(
                            "quarantine directory is missing"
                        ) from None
                    quarantine_descriptors = None
                if quarantine_descriptors is None:
                    quarantines: tuple[VerifiedQuarantineRead, ...] = ()
                    quarantine_snapshots: tuple[
                        tuple[str, _RegularSnapshot], ...
                    ] = ()
                    quarantine_body_snapshots: tuple[
                        tuple[str, _StreamedRegularSnapshot], ...
                    ] = ()
                    quarantine_membership: tuple[str, ...] = ()
                else:
                    if not quarantine_attempts:
                        raise ValueError(
                            "quarantine directory has no bound attempt receipt"
                        )
                    (
                        quarantines,
                        quarantine_snapshots,
                        quarantine_body_snapshots,
                        quarantine_membership,
                    ) = self._verified_quarantine_bundle(
                        query,
                        quarantine_descriptors[-1],
                        attempt_receipts,
                    )

                (
                    final_reservations,
                    final_history,
                    final_attempt_receipts,
                    final_attempt_snapshots,
                ) = self._verified_attempt_bundle(query, attempts_fd)
                if (
                    final_reservations != reservations
                    or final_attempt_receipts != attempt_receipts
                    or final_attempt_snapshots != attempt_snapshots
                ):
                    raise ValueError(
                        "attempt namespace changed during progress read"
                    )
                del final_history
                for name, snapshot in attempt_snapshots:
                    _rebind_regular_snapshot_at(
                        attempts_fd,
                        name,
                        snapshot,
                        "attempt progress artifact",
                    )

                if success_snapshot is None:
                    if (
                        _read_regular_snapshot_at(
                            query_fd,
                            "success.json",
                            "success receipt",
                            missing_ok=True,
                        )
                        is not None
                    ):
                        raise ValueError(
                            "success receipt appeared during progress read"
                        )
                else:
                    _rebind_regular_snapshot_at(
                        query_fd,
                        "success.json",
                        success_snapshot,
                        "success receipt",
                    )
                    if (
                        verified_success is None
                        or _canonical_file_bytes(
                            dict(verified_success.success_receipt)
                        )
                        != success_snapshot.contents
                    ):
                        raise ValueError(
                            "success receipt changed during progress read"
                        )

                if quarantine_descriptors is None:
                    try:
                        _, appeared_descriptors = self._quarantine_fds(
                            query,
                            root_descriptor=descriptors[0],
                        )
                    except FileNotFoundError:
                        pass
                    else:
                        self._close_fds(appeared_descriptors)
                        raise ValueError(
                            "quarantine directory appeared during progress read"
                        )
                else:
                    (
                        final_quarantines,
                        final_quarantine_snapshots,
                        final_quarantine_body_snapshots,
                        final_quarantine_membership,
                    ) = self._verified_quarantine_bundle(
                        query,
                        quarantine_descriptors[-1],
                        attempt_receipts,
                    )
                    if (
                        final_quarantines != quarantines
                        or final_quarantine_snapshots != quarantine_snapshots
                        or final_quarantine_body_snapshots
                        != quarantine_body_snapshots
                        or final_quarantine_membership != quarantine_membership
                    ):
                        raise ValueError(
                            "quarantine namespace changed during progress read"
                        )
                    for name, snapshot in quarantine_snapshots:
                        _rebind_regular_snapshot_at(
                            quarantine_descriptors[-1],
                            name,
                            snapshot,
                            "quarantine receipt",
                        )
                    self._verify_progress_root_stitch(
                        descriptors,
                        quarantine_descriptors,
                    )
                    self._verify_quarantine_descriptor_chain(
                        query.source,
                        query_id,
                        quarantine_descriptors,
                    )

                if quarantine_descriptors is None:
                    self._verify_progress_root_stitch(descriptors, None)

                (
                    final_registration_sha256,
                    final_registered_query,
                    final_query_snapshot,
                ) = self._validated_registered_query(
                    query,
                    query_fd,
                    lock_descriptor=lock_descriptor,
                )
                if (
                    final_registration_sha256 != registration_sha256
                    or final_registered_query != registered_query
                    or final_query_snapshot != query_snapshot
                ):
                    raise ValueError(
                        "logical query registration changed during progress read"
                    )
                self._verify_query_chain(query, descriptors)
                if self._read_only:
                    self._require_no_failure_recovery_state(query_fd)
            finally:
                os.close(attempts_fd)

            receipt_numbers = {
                int(record["attempt_number"])
                for record in attempt_receipts
            }
            pending_attempt_numbers = tuple(sorted(reservations - receipt_numbers))
            if verified_success is not None:
                status: Literal["SUCCESS", "QUARANTINED", "PENDING"] = "SUCCESS"
                published_evidence = verified_success.published_evidence
            elif any(
                record.get("outcome") == "QUARANTINED"
                for record in attempt_receipts
            ):
                status = "QUARANTINED"
                published_evidence = None
            else:
                status = "PENDING"
                published_evidence = None
            return VerifiedQueryProgress(
                query_id=query_id,
                status=status,
                registered_query=registered_query,
                published_evidence=published_evidence,
                attempt_receipts=attempt_receipts,
                pending_attempt_numbers=pending_attempt_numbers,
                quarantines=quarantines,
            )
        finally:
            if quarantine_descriptors is not None:
                self._close_fds(quarantine_descriptors)
            if lock_descriptor >= 0:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(lock_descriptor)
            self._close_fds(descriptors)

    def verify_success(
        self, query: LogicalQuery, *, authority: object | None = None
    ) -> PublishedEvidence:
        return self.read_verified_success(
            query, authority=authority
        ).published_evidence


__all__ = [
    "AttemptBudgetSnapshot",
    "AttemptRetryState",
    "AttemptCapture",
    "LogicalQuery",
    "PersistedAttemptEvidence",
    "PublishedEvidence",
    "ProtectedLegacyPublishReverifyError",
    "QueryLease",
    "V52EvidenceStore",
    "VerifiedEvidenceRead",
    "VerifiedQuarantineRead",
    "VerifiedQueryProgress",
    "canonical_query_bytes",
    "logical_query_id",
    "response_byte_limit",
]
