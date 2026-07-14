from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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


@dataclass(frozen=True)
class PublishedEvidence:
    query_id: str
    success_receipt_path: Path
    raw_sha256: str
    query_sidecar_sha256: str
    storage_mode: Literal["FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"]


@dataclass(frozen=True)
class QueryLease:
    query_id: str
    source: str
    _store_identity: int
    _descriptor: int
    _owner_thread: int


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
    return {
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


def canonical_query_bytes(query: LogicalQuery) -> bytes:
    return _canonical_json_bytes(_query_record(query))


def logical_query_id(query: LogicalQuery) -> str:
    return hashlib.sha256(canonical_query_bytes(query)).hexdigest()


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
            opened = os.fstat(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                os.close(child)
                raise ValueError("evidence root directory identity differs")
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_directory_at(parent_fd: int, name: str, *, create: bool) -> int:
    _validate_segment(name, "directory name")
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
        os.close(descriptor)
        raise ValueError("evidence directory identity differs")
    return descriptor


def _read_regular_at(
    parent_fd: int,
    name: str,
    label: str,
    *,
    missing_ok: bool = False,
) -> bytes | None:
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
        return b"".join(chunks)
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


def _write_once_at(parent_fd: int, name: str, contents: bytes, label: str) -> None:
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
    return {
        "attempt_number": attempt_number,
        "body_sha256": _sha256_bytes(body) if body is not None else None,
        "bytes_received": len(body) if body is not None else 0,
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


def _before_success_publish() -> None:
    """Deterministic interruption-test hook; production intentionally does nothing."""


class V52EvidenceStore:
    def __init__(self, root: Path) -> None:
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
        try:
            canonical_parent = supplied.parent.resolve(strict=True)
        except OSError as exc:
            raise ValueError("evidence root parent is unavailable") from exc
        self.root = supplied
        self._canonical_root = canonical_parent / supplied.name
        descriptor = _open_root_path(self._canonical_root, create=True)
        os.close(descriptor)
        self._local = threading.local()

    def _root_fd(self) -> int:
        return _open_root_path(self._canonical_root, create=False)

    def _query_fds(self, query: LogicalQuery, *, create: bool) -> tuple[str, list[int]]:
        query_id = logical_query_id(query)
        descriptors: list[int] = []
        root_fd = self._root_fd()
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

    @staticmethod
    def _close_fds(descriptors: list[int]) -> None:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    def _verify_query_chain(self, query: LogicalQuery, descriptors: list[int]) -> None:
        if len(descriptors) != 4:
            raise ValueError("query descriptor chain differs")
        query_id = logical_query_id(query)
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
            for index, name in enumerate(("queries", query.source, query_id)):
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

    def _validate_registered(self, query: LogicalQuery, query_fd: int) -> str:
        query_id = logical_query_id(query)
        contents = _read_regular_at(query_fd, "query.json", "logical query")
        if contents != canonical_query_bytes(query) + b"\n":
            raise RuntimeError("registered logical query differs or is ambiguous")
        for name in ("attempts", "objects", "sidecars"):
            descriptor = _open_directory_at(query_fd, name, create=False)
            os.close(descriptor)
        return query_id

    def register_query(self, query: LogicalQuery) -> str:
        query_id, descriptors = self._query_fds(query, create=True)
        try:
            query_fd = descriptors[-1]
            _write_once_at(
                query_fd,
                "query.json",
                canonical_query_bytes(query) + b"\n",
                "logical query",
            )
            for name in ("attempts", "objects", "sidecars"):
                descriptor = _open_directory_at(query_fd, name, create=True)
                os.close(descriptor)
            self._validate_registered(query, query_fd)
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
        metadata = os.fstat(lease._descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("query lease lock is unsafe")
        return lease

    def _ensure_unpublished(self, query: LogicalQuery) -> None:
        if self.load_success(query) is not None:
            raise RuntimeError("query success is already published and terminal")

    @contextmanager
    def acquire_query(self, query: LogicalQuery) -> Iterator[QueryLease]:
        query_id = self.register_query(query)
        active = self._active_leases()
        if query_id in active:
            raise RuntimeError("query lease is not reentrant")
        _, descriptors = self._query_fds(query, create=False)
        query_fd = descriptors[-1]
        lock_descriptor = -1
        try:
            flags = (
                os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                lock_descriptor = os.open(
                    ".query.lock",
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=query_fd,
                )
                os.fsync(lock_descriptor)
                os.fsync(query_fd)
            except FileExistsError:
                lock_descriptor = os.open(".query.lock", flags, dir_fd=query_fd)
            metadata = os.fstat(lock_descriptor)
            linked = os.stat(".query.lock", dir_fd=query_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != 0
                or _identity(metadata) != _identity(linked)
            ):
                raise ValueError("query lease lock must be an empty unaliased file")
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            self._validate_registered(query, query_fd)
            rebound = os.stat(".query.lock", dir_fd=query_fd, follow_symlinks=False)
            if _identity(os.fstat(lock_descriptor)) != _identity(rebound):
                raise ValueError("query lease lock identity changed")
            lease = QueryLease(
                query_id,
                query.source,
                id(self),
                lock_descriptor,
                threading.get_ident(),
            )
            active[query_id] = lease
            try:
                yield lease
            finally:
                active.pop(query_id, None)
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
                _json_object(
                    _read_regular_at(attempts_fd, name, "attempt receipt"),
                    "attempt receipt",
                )
                receipts.add(number)
            else:
                raise ValueError("attempt directory contains an ambiguous entry")
        if not receipts <= reservations:
            raise ValueError("attempt receipt has no immutable reservation")
        return reservations, receipts

    def next_attempt_number(self, lease: QueryLease) -> int:
        if not isinstance(lease, QueryLease):
            raise TypeError("lease must be a QueryLease")
        active = self._active_leases().get(lease.query_id)
        if active is not lease or lease._owner_thread != threading.get_ident():
            raise RuntimeError("attempt reservation requires the active query lease")
        query = self._query_from_registered_lease(lease)
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
            return number
        finally:
            os.close(attempts_fd)
            self._close_fds(descriptors)

    def _query_from_registered_lease(self, lease: QueryLease) -> LogicalQuery:
        root_fd = self._root_fd()
        descriptors = [root_fd]
        try:
            for name in ("queries", lease.source, lease.query_id):
                child = _open_directory_at(descriptors[-1], name, create=False)
                descriptors.append(child)
            record = _json_object(
                _read_regular_at(descriptors[-1], "query.json", "logical query"),
                "logical query",
            )
            query = LogicalQuery(**record)  # type: ignore[arg-type]
            if logical_query_id(query) != lease.query_id:
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
        self._require_lease(query)
        self._ensure_unpublished(query)
        attempts_fd, descriptors = self._attempts_fd(query)
        try:
            self._validate_attempt_reservation(query, attempt_number, attempts_fd)
            record = _attempt_record(
                logical_query_id(query), attempt_number, capture, outcome
            )
            name = f"{attempt_number:06d}.json"
            _write_once_at(
                attempts_fd,
                name,
                _canonical_file_bytes(record),
                "attempt receipt",
            )
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
        self._ensure_unpublished(query)
        _validate_text(error_classification, "error_classification")
        _validate_capture(capture)
        if capture.body is None:
            raise ValueError("a quarantined attempt must own body bytes")
        query_id = logical_query_id(query)
        attempts_fd, attempt_descriptors = self._attempts_fd(query)
        try:
            self._validate_attempt_reservation(query, attempt_number, attempts_fd)
        finally:
            os.close(attempts_fd)
            self._close_fds(attempt_descriptors)
        body = capture.body
        digest = _sha256_bytes(body)
        root_fd = self._root_fd()
        descriptors = [root_fd]
        try:
            for name in ("quarantine", query.source, query_id):
                child = _open_directory_at(descriptors[-1], name, create=True)
                descriptors.append(child)
            quarantine_fd = descriptors[-1]
            body_name = f"{digest}.raw"
            _write_once_at(quarantine_fd, body_name, body, "quarantined response body")
            attempt_record = _attempt_record(
                query_id,
                attempt_number,
                capture,
                "QUARANTINED",
                error_classification=error_classification,
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
        finally:
            self._close_fds(descriptors)
        classified_capture = AttemptCapture(
            started_at_utc=capture.started_at_utc,
            ended_at_utc=capture.ended_at_utc,
            final_url=capture.final_url,
            http_status=capture.http_status,
            response_headers=capture.response_headers,
            body=capture.body,
            error_classification=error_classification,
        )
        self.record_attempt(
            query,
            attempt_number,
            classified_capture,
            outcome="QUARANTINED",
        )
        return self.root / "quarantine" / query.source / query_id / receipt_name

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

    def _publish_sidecar(
        self,
        query: LogicalQuery,
        raw_sha256: str,
        byte_count: int,
        semantics: Mapping[str, object],
        sidecars_fd: int,
    ) -> tuple[str, str]:
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
        _write_once_at(sidecars_fd, name, contents, "query semantic sidecar")
        return name, digest

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
        return {
            "attempt_history": self._attempt_history(query),
            "attempt_number": attempt_number,
            "grain": query.grain,
            "query_id": query_id,
            "query_path": _relative_path(query.source, query_id, "query.json"),
            "query_sha256": query_id,
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
        self._ensure_unpublished(query)
        _validate_capture(capture)
        if capture.body is None:
            raise ValueError("fetched success requires response bytes")
        if capture.http_status is None or not 200 <= capture.http_status <= 299:
            raise ValueError("fetched success requires a successful HTTP status")
        query_id, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            attempts_fd = _open_directory_at(query_fd, "attempts", create=False)
            objects_fd = _open_directory_at(query_fd, "objects", create=False)
            sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
            try:
                self._validate_attempt_reservation(query, attempt_number, attempts_fd)
                attempt = _attempt_record(query_id, attempt_number, capture, "SUCCESS")
                _write_once_at(
                    attempts_fd,
                    f"{attempt_number:06d}.json",
                    _canonical_file_bytes(attempt),
                    "attempt receipt",
                )
                body = capture.body
                raw_sha256 = _sha256_bytes(body)
                raw_name = f"{raw_sha256}.raw"
                _write_once_at(objects_fd, raw_name, body, "fetched raw object")
                sidecar_name, sidecar_sha256 = self._publish_sidecar(
                    query,
                    raw_sha256,
                    len(body),
                    query_sidecar,
                    sidecars_fd,
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
                _before_success_publish()
                self._verify_query_chain(query, descriptors)
                _write_once_at(
                    query_fd,
                    "success.json",
                    _canonical_file_bytes(manifest),
                    "success receipt",
                )
            finally:
                os.close(sidecars_fd)
                os.close(objects_fd)
                os.close(attempts_fd)
        finally:
            self._close_fds(descriptors)
        return self.verify_success(query)

    def _read_external_payload(self, value: object) -> bytes:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("legacy payload_path must be nonempty text")
        path = Path(value)
        if not path.is_absolute():
            if ".." in path.parts or "\\" in value:
                raise ValueError("legacy payload path traversal is forbidden")
            path = self.root / path
        if path.is_symlink():
            raise ValueError("protected legacy payload cannot be a symlink")
        try:
            path = path.parent.resolve(strict=True) / path.name
        except OSError as exc:
            raise ValueError("protected legacy payload parent is unavailable") from exc
        parent_fd = _open_root_path(path.parent, create=False)
        try:
            return _read_regular_at(parent_fd, path.name, "protected legacy payload")
        finally:
            os.close(parent_fd)

    def _validated_legacy_reference(
        self, legacy_reference: Mapping[str, object]
    ) -> tuple[dict[str, object], bytes]:
        reference = _normalized_json_object(legacy_reference, "legacy_reference")
        required = {"payload_path", "raw_byte_count", "raw_sha256"}
        if not required <= set(reference):
            raise ValueError("legacy_reference lacks payload binding fields")
        digest = _validate_sha256(reference.get("raw_sha256"), "raw_sha256")
        byte_count = reference.get("raw_byte_count")
        if (
            isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            raise ValueError("raw_byte_count must be a nonnegative integer")
        payload = self._read_external_payload(reference.get("payload_path"))
        if len(payload) != byte_count or _sha256_bytes(payload) != digest:
            raise ValueError("protected legacy payload binding differs")
        return reference, payload

    def publish_legacy_reference(
        self,
        query: LogicalQuery,
        legacy_reference: Mapping[str, object],
        query_sidecar: Mapping[str, object],
    ) -> PublishedEvidence:
        self._require_lease(query)
        self._ensure_unpublished(query)
        reference, payload = self._validated_legacy_reference(legacy_reference)
        raw_sha256 = _validate_sha256(reference["raw_sha256"], "raw_sha256")
        query_id, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            objects_fd = _open_directory_at(query_fd, "objects", create=False)
            sidecars_fd = _open_directory_at(query_fd, "sidecars", create=False)
            try:
                reference_contents = _canonical_file_bytes(reference)
                reference_sha256 = _sha256_bytes(reference_contents)
                reference_name = f"{reference_sha256}.legacy-reference.json"
                _write_once_at(
                    objects_fd,
                    reference_name,
                    reference_contents,
                    "protected legacy reference",
                )
                sidecar_name, sidecar_sha256 = self._publish_sidecar(
                    query,
                    raw_sha256,
                    len(payload),
                    query_sidecar,
                    sidecars_fd,
                )
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
                _before_success_publish()
                self._verify_query_chain(query, descriptors)
                _write_once_at(
                    query_fd,
                    "success.json",
                    _canonical_file_bytes(manifest),
                    "success receipt",
                )
            finally:
                os.close(sidecars_fd)
                os.close(objects_fd)
        finally:
            self._close_fds(descriptors)
        return self.verify_success(query)

    def load_success(self, query: LogicalQuery) -> PublishedEvidence | None:
        logical_query_id(query)
        try:
            _, descriptors = self._query_fds(query, create=False)
        except FileNotFoundError:
            return None
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            contents = _read_regular_at(
                query_fd, "success.json", "success receipt", missing_ok=True
            )
            if contents is None:
                return None
        finally:
            self._close_fds(descriptors)
        return self.verify_success(query)

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

    def verify_success(self, query: LogicalQuery) -> PublishedEvidence:
        query_id, descriptors = self._query_fds(query, create=False)
        try:
            query_fd = descriptors[-1]
            self._validate_registered(query, query_fd)
            success_contents = _read_regular_at(
                query_fd, "success.json", "success receipt"
            )
            manifest = _json_object(success_contents, "success receipt")
        finally:
            self._close_fds(descriptors)

        common_keys = {
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
        fetched_keys = common_keys | {"fetched_raw_path"}
        legacy_keys = common_keys | {
            "protected_legacy_reference_path",
            "protected_legacy_reference_sha256",
        }
        mode = manifest.get("storage_mode")
        if mode == "FETCHED_RAW":
            if set(manifest) != fetched_keys:
                raise ValueError("fetched success receipt keys differ")
            attempt_number = manifest.get("attempt_number")
            if (
                isinstance(attempt_number, bool)
                or not isinstance(attempt_number, int)
                or attempt_number < 1
            ):
                raise ValueError("fetched success attempt binding differs")
        elif mode == "PROTECTED_LEGACY_REFERENCE":
            if (
                set(manifest) != legacy_keys
                or manifest.get("attempt_number") is not None
            ):
                raise ValueError("protected success receipt keys differ")
        else:
            raise ValueError("success storage_mode differs")
        if (
            manifest.get("schema_version") != 1
            or manifest.get("query_id") != query_id
            or manifest.get("query_sha256") != query_id
            or manifest.get("source") != query.source
            or manifest.get("grain") != query.grain
            or manifest.get("query_path")
            != _relative_path(query.source, query_id, "query.json")
        ):
            raise ValueError("success query binding differs")
        raw_sha256 = _validate_sha256(manifest.get("raw_sha256"), "success raw_sha256")
        raw_byte_count = manifest.get("raw_byte_count")
        if (
            isinstance(raw_byte_count, bool)
            or not isinstance(raw_byte_count, int)
            or raw_byte_count < 0
        ):
            raise ValueError("success raw_byte_count differs")

        expected_query_prefix = ("queries", query.source, query_id)
        if mode == "FETCHED_RAW":
            raw = self._read_bound_relative(
                manifest["fetched_raw_path"],  # type: ignore[arg-type]
                (*expected_query_prefix, "objects"),
                "fetched raw object",
            )
        else:
            reference_contents = self._read_bound_relative(
                manifest["protected_legacy_reference_path"],  # type: ignore[arg-type]
                (*expected_query_prefix, "objects"),
                "protected legacy reference",
            )
            reference_sha256 = _validate_sha256(
                manifest.get("protected_legacy_reference_sha256"),
                "protected reference SHA-256",
            )
            if _sha256_bytes(reference_contents) != reference_sha256:
                raise ValueError("protected legacy reference hash differs")
            reference = _json_object(reference_contents, "protected legacy reference")
            _, raw = self._validated_legacy_reference(reference)
        if len(raw) != raw_byte_count or _sha256_bytes(raw) != raw_sha256:
            raise ValueError("success raw binding differs")

        sidecar_contents = self._read_bound_relative(
            manifest.get("query_sidecar_path"),  # type: ignore[arg-type]
            (*expected_query_prefix, "sidecars"),
            "query semantic sidecar",
        )
        sidecar_sha256 = _validate_sha256(
            manifest.get("query_sidecar_sha256"), "query sidecar SHA-256"
        )
        if _sha256_bytes(sidecar_contents) != sidecar_sha256:
            raise ValueError("query sidecar hash differs")
        sidecar = _json_object(sidecar_contents, "query semantic sidecar")
        if set(sidecar) != {
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

        history = manifest.get("attempt_history")
        if not isinstance(history, list):
            raise ValueError("attempt history must be an array")
        current_history = self._attempt_history(query)
        if history != current_history:
            raise ValueError("success attempt history binding differs")
        if mode == "FETCHED_RAW" and (
            not history
            or history[-1].get("attempt_number") != manifest.get("attempt_number")
        ):
            raise ValueError("successful attempt is not the history tail")
        return PublishedEvidence(
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


__all__ = [
    "AttemptCapture",
    "LogicalQuery",
    "PublishedEvidence",
    "QueryLease",
    "V52EvidenceStore",
    "canonical_query_bytes",
    "logical_query_id",
]
