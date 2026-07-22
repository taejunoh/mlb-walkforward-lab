"""Authenticated current-season, result-free D-1 history for Daily Top 3."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .clock import assert_feature_dates
from .live_model_card import FrozenLiveCard
from .live_sources import MAX_JSON_BYTES, ObservationFetcher, fetch_live_http
from .live_top3_contracts import (
    LiveHistoryBundle,
    LiveHttpObservation,
    SourceBinding,
    canonical_json_bytes,
    validate_target_date,
)
from .manifest import RawStore
from .models import RawArtifact
from .mlb_stats import league_era_url, parse_league_era
from .prepare import ScheduleHistoryBuild, build_feature_history, derive_team_code_map
from .source_parsers.pitching import parse_prior_career_bf
from .sources import MLB_BASE, parse_schedule, schedule_url, validate_schedule_query_coverage
from .statcast import (
    MAX_STATCAST_BYTES,
    StatcastData,
    derive_outs_recorded,
    fetch_monthly,
    monthly_ranges,
    parse_statcast,
)
from .secure_fs import (
    TrustedRoot,
    open_parent,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


_HISTORY_NAMESPACE = "experimental_daily/history_cache"
_SCHEDULE_SOURCE = "mlb_schedule"
_STATCAST_SOURCE = "baseball_savant_statcast"
_VENUE_SOURCE = "mlb_live_history_venue"
_ERA_SOURCE = "mlb_live_history_era"
_CAREER_SOURCE = "mlb_live_career_bf"
_RETRY_DELAYS = (1, 2, 4, 8, 16)
_LOCK_OPEN_ATTEMPTS = 16
_HISTORY_LAYOUT_DIRECTORIES = (
    Path("data/experimental_daily/history_cache"),
    Path("audit/experimental_daily/history_receipts"),
    Path("audit/experimental_daily/history_receipt_locks"),
)
_RECEIPT_THREAD_LOCKS_GUARD = threading.Lock()
_RECEIPT_THREAD_LOCKS: dict[
    tuple[str, str], tuple[threading.Lock, int]
] = {}
_ACTIVE_HISTORY_ROOT: ContextVar[TrustedRoot | None] = ContextVar(
    "active_history_root", default=None
)


def _canonical_utc_now() -> str:
    value = datetime.now(timezone.utc)
    base = value.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{value.microsecond:06d}".rstrip("0") + "Z" if value.microsecond else base + "Z"


def _query_json(query: Mapping[str, object]) -> str:
    return canonical_json_bytes(dict(query)).decode("utf-8").removesuffix("\n")


def _observation_id(role: str, query: Mapping[str, object]) -> str:
    digest = hashlib.sha256(_query_json(query).encode("utf-8")).hexdigest()
    return f"history-{role.lower()}-{digest[:32]}"


def _history_root_test_hook(stage: str, root: Path) -> None:
    """No-op seam for deterministic root-swap tests."""


@contextmanager
def _history_root_session(root: Path):
    """Yield one root capability kept alive for the complete public call."""
    lexical = Path(os.path.abspath(root))
    if len(lexical.parts) > 1:
        top = Path(lexical.anchor) / lexical.parts[1]
        top_stat = os.lstat(top)
        if stat.S_ISLNK(top_stat.st_mode):
            resolved_top = top.resolve(strict=True)
            if not resolved_top.is_dir():
                raise ValueError("history root ancestry must contain only directories")
            lexical = resolved_top.joinpath(*lexical.parts[2:])
    missing: list[str] = []
    cursor = lexical
    while True:
        try:
            current = os.lstat(cursor)
        except FileNotFoundError:
            if cursor == cursor.parent:
                raise ValueError("history root has no trusted ancestor") from None
            missing.append(cursor.name)
            cursor = cursor.parent
            continue
        if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
            raise ValueError("history root ancestry must contain only directories")
        break
    _history_root_test_hook("after_existing_root_lstat", lexical)
    with open_trusted_root(cursor) as trusted:
        if missing:
            relative = Path(*reversed(missing)) / ".history-root-anchor"
            with open_parent(trusted, relative, create=True):
                pass
        trusted.assert_bound()
    absolute = cursor.joinpath(*reversed(missing))
    with open_trusted_root(absolute) as trusted:
        _prepare_history_layout_trusted(trusted)
        token = _ACTIVE_HISTORY_ROOT.set(trusted)
        try:
            yield absolute, trusted
        finally:
            _ACTIVE_HISTORY_ROOT.reset(token)


@contextmanager
def _trusted_history_root(root: Path):
    active = _ACTIVE_HISTORY_ROOT.get()
    if active is not None and active.path == root:
        active.assert_bound()
        yield active
        active.assert_bound()
        return
    with open_trusted_root(root) as trusted:
        yield trusted


def _prepare_history_layout_trusted(trusted: TrustedRoot) -> None:
    """Create and pin every fixed cache/audit directory through trusted dir-fds."""
    try:
        for directory in _HISTORY_LAYOUT_DIRECTORIES:
            with open_parent(
                trusted, directory / ".history-layout-anchor", create=True
            ) as directory_descriptor:
                if not stat.S_ISDIR(os.fstat(directory_descriptor).st_mode):
                    raise ValueError(
                        "history layout component must be a directory"
                    )
            trusted.assert_bound()
    except OSError as exc:
        raise ValueError("history layout contains an unsafe component") from exc


class _CapabilityRawStore:
    """Minimal RawStore contract implemented entirely below one TrustedRoot fd."""

    _relative_root = Path("data/experimental_daily/history_cache")

    def __init__(self, root: Path, trusted: TrustedRoot) -> None:
        self.history_root = root
        self.root = root / self._relative_root
        self._trusted = trusted

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8")

    @staticmethod
    def _normalized_query(query: Mapping[str, object]) -> dict[str, object]:
        try:
            return json.loads(
                json.dumps(
                    dict(query),
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("query must contain JSON-compatible values") from exc

    @classmethod
    def _read_manifest(cls, content: bytes) -> dict[str, object]:
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("immutable raw manifest is invalid") from exc
        required = {
            "source", "query", "sha256", "retrieved_at_utc",
            "row_count", "schema_version",
        }
        if not isinstance(manifest, dict) or set(manifest) != required:
            raise RuntimeError("immutable raw manifest fields mismatch")
        if type(manifest["row_count"]) is not int:
            raise RuntimeError("immutable raw manifest row_count must be an int")
        if type(manifest["schema_version"]) is not int:
            raise RuntimeError("immutable raw manifest schema_version must be an int")
        retrieved_at = manifest["retrieved_at_utc"]
        if not isinstance(retrieved_at, str):
            raise RuntimeError("immutable raw manifest timestamp is invalid")
        try:
            parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
        except ValueError as exc:
            raise RuntimeError("immutable raw manifest timestamp is invalid") from exc
        if parsed_retrieved_at.utcoffset() != timedelta(0):
            raise RuntimeError("immutable raw manifest timestamp is invalid")
        if content != cls._json_bytes(manifest):
            raise RuntimeError("immutable raw manifest bytes differ")
        return manifest

    @staticmethod
    def _canonical_manifest_query(value: object) -> object:
        try:
            return json.loads(
                json.dumps(
                    value,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("query must contain JSON-compatible values") from exc

    def _read(
        self,
        relative: Path,
        label: str,
        *,
        max_bytes: int = MAX_STATCAST_BYTES,
        missing_ok: bool = False,
    ) -> bytes | None:
        snapshot = read_regular_snapshot(
            self._trusted,
            relative,
            label,
            max_bytes=max_bytes,
            missing_ok=missing_ok,
        )
        if snapshot is None:
            return None
        rebind_regular_snapshot(
            self._trusted, snapshot, label, max_bytes=max_bytes
        )
        return snapshot.contents

    def _write(self, relative: Path, content: bytes, label: str) -> None:
        snapshot = read_regular_snapshot(
            self._trusted,
            relative,
            label,
            max_bytes=MAX_STATCAST_BYTES,
            missing_ok=True,
        )
        if snapshot is None:
            try:
                snapshot = write_exclusive(
                    self._trusted, relative, content, label
                )
            except FileExistsError:
                snapshot = read_regular_snapshot(
                    self._trusted,
                    relative,
                    label,
                    max_bytes=MAX_STATCAST_BYTES,
                )
        if snapshot is None or snapshot.contents != content:
            raise RuntimeError(f"immutable raw {label} differs")
        rebind_regular_snapshot(
            self._trusted, snapshot, label, max_bytes=MAX_STATCAST_BYTES
        )

    def _store_directory_fd(self) -> int:
        with open_parent(
            self._trusted,
            self._relative_root / ".history-store-anchor",
            create=True,
        ) as descriptor:
            return os.dup(descriptor)

    def verify_manifests(self) -> list[dict[str, object]]:
        store_fd = self._store_directory_fd()
        try:
            source_names = sorted(os.listdir(store_fd))
        finally:
            os.close(store_fd)
        verified: list[dict[str, object]] = []
        seen_queries: set[tuple[str, str]] = set()
        for source in source_names:
            if source.startswith(".") and source.endswith(".lock"):
                continue
            source_relative = self._relative_root / source
            with open_parent(
                self._trusted, source_relative / ".source-anchor", create=False
            ) as source_fd:
                names = sorted(os.listdir(source_fd))
            for name in names:
                if not name.endswith(".manifest.json"):
                    continue
                manifest_relative = source_relative / name
                content = self._read(
                    manifest_relative, "history raw manifest", max_bytes=MAX_JSON_BYTES
                )
                if content is None:
                    raise RuntimeError("immutable raw manifest is missing")
                manifest = self._read_manifest(content)
                if manifest["source"] != source:
                    raise RuntimeError("immutable raw manifest source mismatch")
                query = self._canonical_manifest_query(manifest["query"])
                query_bytes = json.dumps(
                    query, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode()
                query_digest = hashlib.sha256(query_bytes).hexdigest()
                expected_name = (
                    f'{manifest["sha256"]}.{query_digest}.manifest.json'
                )
                if name != expected_name or (source, query_digest) in seen_queries:
                    raise RuntimeError("immutable raw manifest identity mismatch")
                seen_queries.add((source, query_digest))
                seal_relative = manifest_relative.with_name(
                    name.removesuffix(".json") + ".sha256"
                )
                seal = self._read(
                    seal_relative, "history raw manifest seal", max_bytes=MAX_JSON_BYTES
                )
                if seal != f"{hashlib.sha256(content).hexdigest()}\n".encode():
                    raise RuntimeError("immutable raw manifest integrity mismatch")
                payload_relative = source_relative / f'{manifest["sha256"]}.raw'
                payload = self._read(payload_relative, "history raw payload")
                if (
                    payload is None
                    or hashlib.sha256(payload).hexdigest() != manifest["sha256"]
                    or sum(bool(line.strip()) for line in payload.splitlines())
                    != manifest["row_count"]
                ):
                    raise RuntimeError("immutable raw payload digest mismatch")
                verified.append(
                    {
                        "manifest": manifest,
                        "manifest_path": self.history_root / manifest_relative,
                        "manifest_sha256": hashlib.sha256(content).hexdigest(),
                        "payload_path": self.history_root / payload_relative,
                        "payload_bytes": payload,
                    }
                )
        return verified

    def _unlink_exact(self, relative: Path, expected: bytes, label: str) -> None:
        snapshot = read_regular_snapshot(
            self._trusted,
            relative,
            label,
            max_bytes=MAX_JSON_BYTES,
        )
        if snapshot is None or snapshot.contents != expected:
            raise RuntimeError(f"immutable raw {label} differs")
        rebind_regular_snapshot(
            self._trusted, snapshot, label, max_bytes=MAX_JSON_BYTES
        )
        with open_parent(self._trusted, relative, create=False) as parent_fd:
            named = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            identity = (
                named.st_dev,
                named.st_ino,
                named.st_mode,
                named.st_nlink,
                named.st_size,
                named.st_mtime_ns,
                named.st_ctime_ns,
            )
            if identity != snapshot.identity:
                raise RuntimeError(f"immutable raw {label} changed before unlink")
            os.unlink(relative.name, dir_fd=parent_fd)
        self._trusted.assert_bound()

    def _load_manifest_transaction(
        self,
        manifest_relative: Path,
        pending_relative: Path,
        seal_relative: Path,
        expected: Mapping[str, object],
    ) -> dict[str, object] | None:
        manifest_content = self._read(
            manifest_relative,
            "manifest",
            max_bytes=MAX_JSON_BYTES,
            missing_ok=True,
        )
        seal_content = self._read(
            seal_relative, "seal", max_bytes=MAX_JSON_BYTES, missing_ok=True
        )
        pending_content = self._read(
            pending_relative,
            "pending manifest",
            max_bytes=MAX_JSON_BYTES,
            missing_ok=True,
        )
        manifest = None
        if manifest_content is not None:
            if seal_content is None:
                raise RuntimeError("immutable raw manifest integrity seal is missing")
            if seal_content != f"{hashlib.sha256(manifest_content).hexdigest()}\n".encode():
                raise RuntimeError("immutable raw manifest integrity mismatch")
            if pending_content is not None:
                if pending_content != manifest_content:
                    raise RuntimeError("immutable raw manifest pending bytes mismatch")
                self._unlink_exact(
                    pending_relative, pending_content, "pending manifest"
                )
            manifest = self._read_manifest(manifest_content)
        elif seal_content is not None:
            if pending_content is None:
                raise RuntimeError("immutable raw manifest integrity seal is orphaned")
            if seal_content != f"{hashlib.sha256(pending_content).hexdigest()}\n".encode():
                raise RuntimeError("immutable raw manifest integrity mismatch")
            manifest = self._read_manifest(pending_content)
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise RuntimeError("immutable raw manifest metadata mismatch")
            self._write(manifest_relative, pending_content, "manifest")
            self._unlink_exact(
                pending_relative, pending_content, "pending manifest"
            )
        elif pending_content is not None:
            self._unlink_exact(
                pending_relative, pending_content, "pending manifest"
            )
        if manifest is not None and any(
            manifest.get(key) != value for key, value in expected.items()
        ):
            raise RuntimeError("immutable raw manifest metadata mismatch")
        return manifest

    def put_unique_query(
        self, source: str, query: dict[str, object], payload: bytes
    ) -> RawArtifact:
        if (
            not isinstance(source, str)
            or not source
            or source in {".", ".."}
            or "/" in source
            or "\\" in source
        ):
            raise ValueError("source must be a single path segment")
        if not isinstance(query, dict):
            raise ValueError("query must be a dict")
        if not isinstance(payload, bytes):
            raise ValueError("payload must be bytes")
        normalized = self._normalized_query(query)
        digest = hashlib.sha256(payload).hexdigest()
        row_count = sum(bool(line.strip()) for line in payload.splitlines())
        store_fd = self._store_directory_fd()
        descriptor = -1
        try:
            descriptor = _open_receipt_lock(store_fd, f".{source}.lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            matches = [
                row
                for row in self.verify_manifests()
                if row["manifest"]["source"] == source
                and row["manifest"]["query"] == normalized
            ]
            if matches:
                if len(matches) != 1 or matches[0]["manifest"]["sha256"] != digest:
                    raise RuntimeError(
                        "ambiguous raw payload for canonical history query"
                    )
                row = matches[0]
                return RawArtifact(
                    source,
                    digest,
                    Path(row["payload_path"]),
                    Path(row["manifest_path"]),
                    str(row["manifest"]["retrieved_at_utc"]),
                    int(row["manifest"]["row_count"]),
                )
            source_relative = self._relative_root / source
            with open_parent(
                self._trusted, source_relative / ".source-anchor", create=True
            ):
                pass
            query_bytes = json.dumps(
                normalized,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            query_digest = hashlib.sha256(query_bytes).hexdigest()
            payload_relative = source_relative / f"{digest}.raw"
            manifest_relative = source_relative / (
                f"{digest}.{query_digest}.manifest.json"
            )
            pending_relative = manifest_relative.with_name(
                manifest_relative.name.removesuffix(".json") + ".pending"
            )
            seal_relative = manifest_relative.with_name(
                manifest_relative.name.removesuffix(".json") + ".sha256"
            )
            expected = {
                "source": source,
                "query": normalized,
                "sha256": digest,
                "row_count": row_count,
                "schema_version": 1,
            }
            manifest = self._load_manifest_transaction(
                manifest_relative, pending_relative, seal_relative, expected
            )
            self._write(payload_relative, payload, "payload")
            if manifest is None:
                manifest = {
                    **expected,
                    "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                }
                manifest_bytes = self._json_bytes(manifest)
                self._write(
                    pending_relative, manifest_bytes, "pending manifest"
                )
                self._write(
                    seal_relative,
                    f"{hashlib.sha256(manifest_bytes).hexdigest()}\n".encode(),
                    "manifest seal",
                )
                self._write(manifest_relative, manifest_bytes, "manifest")
                self._unlink_exact(
                    pending_relative, manifest_bytes, "pending manifest"
                )
            return RawArtifact(
                source,
                digest,
                self.history_root / payload_relative,
                self.history_root / manifest_relative,
                str(manifest["retrieved_at_utc"]),
                int(manifest["row_count"]),
            )
        finally:
            if descriptor >= 0:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            os.close(store_fd)


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("history path escapes bound root") from exc


def _receipt_path(root: Path, observation_id: str) -> Path:
    return root / "audit/experimental_daily/history_receipts" / f"{observation_id}.json"


def _receipt_claim_path(receipt_path: Path) -> Path:
    return receipt_path.with_suffix(".claim.json")


def _receipt_seal_path(receipt_path: Path) -> Path:
    return receipt_path.with_suffix(".sha256")


def _receipt_completion_path(receipt_path: Path) -> Path:
    return receipt_path.with_suffix(".completion.json")


def _receipt_completion_seal_path(receipt_path: Path) -> Path:
    return receipt_path.with_suffix(".completion.sha256")


def _receipt_json_candidates(root: Path) -> tuple[Path, ...]:
    directory = root / "audit/experimental_daily/history_receipts"
    active = _ACTIVE_HISTORY_ROOT.get()
    if active is not None and active.path == root:
        with open_parent(
            active,
            Path("audit/experimental_daily/history_receipts/.candidate-anchor"),
            create=False,
        ) as directory_fd:
            names = os.listdir(directory_fd)
    elif directory.exists():
        names = [path.name for path in directory.iterdir()]
    else:
        names = []
    return tuple(
        directory / name
        for name in sorted(names)
        if name.startswith("history-")
        and name.endswith(".json")
        and not name.endswith((".claim.json", ".completion.json"))
    )


def _write_immutable(root: Path, path: Path, content: bytes, label: str) -> None:
    """Write once through a bound root, fsyncing and rebinding before return."""
    relative = path.relative_to(root)
    with _trusted_history_root(root) as trusted:
        snapshot = read_regular_snapshot(
            trusted, relative, label, max_bytes=MAX_JSON_BYTES, missing_ok=True
        )
        if snapshot is None:
            try:
                snapshot = write_exclusive(trusted, relative, content, label)
            except FileExistsError:
                snapshot = read_regular_snapshot(
                    trusted, relative, label, max_bytes=MAX_JSON_BYTES
                )
        if snapshot is None or snapshot.contents != content:
            raise ValueError("history receipt differs from authenticated cache")
        rebind_regular_snapshot(trusted, snapshot, label, max_bytes=MAX_JSON_BYTES)


def _read_immutable(
    root: Path,
    path: Path,
    label: str,
    *,
    missing_ok: bool = False,
    max_bytes: int = MAX_JSON_BYTES,
) -> bytes | None:
    relative = path.relative_to(root)
    with _trusted_history_root(root) as trusted:
        snapshot = read_regular_snapshot(
            trusted,
            relative,
            label,
            max_bytes=max_bytes,
            missing_ok=missing_ok,
        )
        if snapshot is None:
            return None
        rebind_regular_snapshot(trusted, snapshot, label, max_bytes=max_bytes)
        return snapshot.contents


_RECEIPT_FIELDS = {
    "binding_role", "byte_length", "content_type", "etag", "final_url",
    "game_pks", "http_status", "last_modified", "manifest_sha256",
    "observation_id", "payload_relative", "payload_sha256",
    "query_identity_json", "requested_url", "response_completed_at_utc", "source",
}


def _validate_receipt_value(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        raise ValueError("history receipt fields differ")
    value = dict(value)
    game_pks = value["game_pks"]
    if not isinstance(game_pks, (list, tuple)):
        raise ValueError("history receipt game_pks differ")
    value["game_pks"] = list(game_pks)
    # SourceBinding performs the remaining closed-contract validation once the
    # authenticated completion time is attached.
    return value


def _receipt_claim_bytes(receipt: Mapping[str, object]) -> bytes:
    normalized = _validate_receipt_value(dict(receipt))
    receipt_bytes = canonical_json_bytes(normalized)
    return canonical_json_bytes(
        {
            "receipt": normalized,
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        }
    )


def _validate_receipt_lock(
    descriptor: int, parent_descriptor: int, name: str
) -> None:
    opened = os.fstat(descriptor)
    named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(named.st_mode)
        or named.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise ValueError("unsafe history receipt publication lock")


def _open_receipt_lock(parent_descriptor: int, name: str) -> int:
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
        except OSError as exc:
            raise ValueError("unsafe history receipt publication lock") from exc
        try:
            _validate_receipt_lock(descriptor, parent_descriptor, name)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    raise ValueError("history receipt publication lock could not be opened") from last_missing


@contextmanager
def _receipt_thread_lock(trusted: TrustedRoot, observation_id: str):
    key = (trusted.path.as_posix(), observation_id)
    with _RECEIPT_THREAD_LOCKS_GUARD:
        entry = _RECEIPT_THREAD_LOCKS.get(key)
        if entry is None:
            lock = threading.Lock()
            references = 0
        else:
            lock, references = entry
        _RECEIPT_THREAD_LOCKS[key] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _RECEIPT_THREAD_LOCKS_GUARD:
            current_lock, references = _RECEIPT_THREAD_LOCKS[key]
            if current_lock is not lock:
                raise RuntimeError("history receipt thread lock identity differs")
            if references == 1:
                del _RECEIPT_THREAD_LOCKS[key]
            else:
                _RECEIPT_THREAD_LOCKS[key] = (lock, references - 1)


@contextmanager
def _receipt_publication_lock(root: Path, observation_id: str):
    relative = (
        Path("audit/experimental_daily/history_receipt_locks")
        / f"{observation_id}.lock"
    )
    descriptor = -1
    with _trusted_history_root(root) as trusted:
        with _receipt_thread_lock(trusted, observation_id):
            try:
                with open_parent(trusted, relative, create=True) as parent_descriptor:
                    descriptor = _open_receipt_lock(
                        parent_descriptor, relative.name
                    )
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    _validate_receipt_lock(
                        descriptor, parent_descriptor, relative.name
                    )
                    trusted.assert_bound()
                    yield
                    _validate_receipt_lock(
                        descriptor, parent_descriptor, relative.name
                    )
                    trusted.assert_bound()
            finally:
                if descriptor >= 0:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)


def _read_receipt_prefix(
    root: Path, receipt_path: Path
) -> tuple[bytes | None, ...]:
    paths = (
        _receipt_claim_path(receipt_path),
        receipt_path,
        _receipt_seal_path(receipt_path),
        _receipt_completion_path(receipt_path),
        _receipt_completion_seal_path(receipt_path),
    )
    labels = (
        "history receipt claim",
        "history receipt",
        "history receipt seal",
        "history receipt completion",
        "history receipt completion seal",
    )
    existing = tuple(
        _read_immutable(root, path, label, missing_ok=True)
        for path, label in zip(paths, labels, strict=True)
    )
    directory = receipt_path.parent
    active = _ACTIVE_HISTORY_ROOT.get()
    if active is not None and active.path == root:
        relative_directory = directory.relative_to(root)
        with open_parent(
            active, relative_directory / ".receipt-directory-anchor", create=False
        ) as directory_fd:
            child_names = os.listdir(directory_fd)
    elif directory.exists():
        child_names = [child.name for child in directory.iterdir()]
    else:
        child_names = []
    if child_names:
        allowed = {path.name for path in paths}
        stem = receipt_path.name.removesuffix(".json")
        extras = [
            child_name
            for child_name in child_names
            if child_name.startswith(f"{stem}.") and child_name not in allowed
        ]
        if extras:
            raise ValueError("history receipt publication prefix has an extra artifact")
    missing_seen = False
    for content in existing:
        if content is None:
            missing_seen = True
        elif missing_seen:
            raise ValueError("history receipt publication prefix has a gap")
    return existing


def _complete_receipt_prefix_locked(
    root: Path,
    receipt_path: Path,
    expected_receipt: Mapping[str, object] | None = None,
) -> tuple[bytes, bytes]:
    """Validate one immutable prefix and publish only its missing suffix."""
    paths = (
        _receipt_claim_path(receipt_path),
        receipt_path,
        _receipt_seal_path(receipt_path),
        _receipt_completion_path(receipt_path),
        _receipt_completion_seal_path(receipt_path),
    )
    labels = (
        "history receipt claim",
        "history receipt",
        "history receipt seal",
        "history receipt completion",
        "history receipt completion seal",
    )
    existing = list(_read_receipt_prefix(root, receipt_path))

    expected = (
        _validate_receipt_value(dict(expected_receipt))
        if expected_receipt is not None
        else None
    )
    claim_bytes = existing[0]
    if claim_bytes is None:
        if expected is None:
            raise ValueError("history receipt claim is missing")
        claim_bytes = _receipt_claim_bytes(expected)
        _write_immutable(root, paths[0], claim_bytes, labels[0])
    try:
        claim = json.loads(claim_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("history receipt claim is invalid") from exc
    if (
        not isinstance(claim, dict)
        or set(claim) != {"receipt", "receipt_sha256"}
        or canonical_json_bytes(claim) != claim_bytes
    ):
        raise ValueError("history receipt claim differs")
    claimed_receipt = _validate_receipt_value(claim["receipt"])
    receipt_bytes = canonical_json_bytes(claimed_receipt)
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    if claim["receipt_sha256"] != receipt_sha256 or (
        expected is not None and claimed_receipt != expected
    ):
        raise ValueError("history receipt claim differs")

    deterministic = (
        claim_bytes,
        receipt_bytes,
        f"{receipt_sha256}\n".encode(),
    )
    for index, content in enumerate(deterministic):
        if existing[index] is None:
            _write_immutable(root, paths[index], content, labels[index])
            existing[index] = content
        elif existing[index] != content:
            raise ValueError(f"{labels[index]} differs")

    completion_bytes = existing[3]
    if completion_bytes is None:
        completion_bytes = canonical_json_bytes(
            {
                "receipt_sha256": receipt_sha256,
                "durable_at_utc": _canonical_utc_now(),
            }
        )
        _write_immutable(root, paths[3], completion_bytes, labels[3])
        existing[3] = completion_bytes
    try:
        completion = json.loads(completion_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("history receipt completion is invalid") from exc
    if (
        not isinstance(completion, dict)
        or set(completion) != {"receipt_sha256", "durable_at_utc"}
        or canonical_json_bytes(completion) != completion_bytes
        or completion["receipt_sha256"] != receipt_sha256
    ):
        raise ValueError("history receipt completion differs")
    completion_seal = f"{hashlib.sha256(completion_bytes).hexdigest()}\n".encode()
    if existing[4] is None:
        _write_immutable(root, paths[4], completion_seal, labels[4])
        existing[4] = completion_seal
    elif existing[4] != completion_seal:
        raise ValueError("history receipt completion seal differs")
    return receipt_bytes, completion_bytes


def _complete_receipt_prefix(
    root: Path,
    receipt_path: Path,
    expected_receipt: Mapping[str, object] | None = None,
) -> tuple[bytes, bytes]:
    observation_id = receipt_path.name.removesuffix(".json")
    if receipt_path != _receipt_path(root, observation_id):
        raise ValueError("history receipt path differs from observation identity")
    with _receipt_publication_lock(root, observation_id):
        return _complete_receipt_prefix_locked(
            root, receipt_path, expected_receipt
        )


def _persist_history_receipt(
    root: Path, receipt_path: Path, receipt: Mapping[str, object]
) -> None:
    _complete_receipt_prefix(root, receipt_path, receipt)


def _http_for(source: str, url: str, fetcher: ObservationFetcher, max_bytes: int) -> LiveHttpObservation:
    observed = fetcher(source, url, max_bytes=max_bytes)  # type: ignore[arg-type]
    if not isinstance(observed, LiveHttpObservation):
        raise ValueError("history fetcher must return LiveHttpObservation")
    if observed.requested_url != url:
        raise ValueError("history source URL differs from requested URL")
    if observed.http_status != 200:
        raise ValueError("history HTTP response status must be 200")
    if type(max_bytes) is not int or max_bytes <= 0 or len(observed.body) > max_bytes:
        raise ValueError("history HTTP response exceeds size limit")
    return observed


def _fetch_with_retry(
    source: str,
    url: str,
    fetcher: ObservationFetcher,
    max_bytes: int,
    sleeper: Callable[[float], None],
) -> LiveHttpObservation:
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return _http_for(source, url, fetcher, max_bytes)
        except HTTPError as exc:
            if (
                exc.code != 429
                and not 500 <= exc.code < 600
            ) or attempt == len(_RETRY_DELAYS):
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == len(_RETRY_DELAYS):
                raise
        sleeper(_RETRY_DELAYS[attempt])
    raise RuntimeError("history retry loop exhausted")


def _history_receipt(
    root: Path,
    *,
    source: str,
    binding_role: str,
    normalized_query: Mapping[str, object],
    game_pks: tuple[int, ...],
    observation_id: str,
    http: LiveHttpObservation,
    payload_path: Path,
    payload_sha256: str,
    manifest_sha256: str,
) -> dict[str, object]:
    return {
        "source": source,
        "binding_role": binding_role,
        "game_pks": list(sorted(set(game_pks))),
        "observation_id": observation_id,
        "query_identity_json": _query_json(normalized_query),
        "requested_url": http.requested_url,
        "final_url": http.final_url,
        "response_completed_at_utc": http.response_completed_at_utc,
        "http_status": http.http_status,
        "content_type": http.content_type,
        "etag": http.etag,
        "last_modified": http.last_modified,
        "payload_sha256": payload_sha256,
        "byte_length": len(http.body),
        "payload_relative": _relative(root, payload_path),
        "manifest_sha256": manifest_sha256,
    }


def _binding_from_receipt_locked(root: Path, receipt_path: Path) -> SourceBinding:
    receipt_bytes, completion_bytes = _complete_receipt_prefix_locked(
        root, receipt_path
    )
    try:
        receipt = json.loads(receipt_bytes.decode("utf-8"))
        completion = json.loads(completion_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("history receipt is invalid") from exc
    receipt = _validate_receipt_value(receipt)
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    payload = root / str(receipt["payload_relative"])
    manifest_sha = str(receipt["manifest_sha256"])
    payload_bytes = _read_immutable(
        root, payload, "history receipt payload", max_bytes=MAX_STATCAST_BYTES
    )
    if (
        payload_bytes is None
        or hashlib.sha256(payload_bytes).hexdigest() != receipt["payload_sha256"]
    ):
        raise ValueError("history receipt payload differs from cache")
    receipt_relative = _relative(root, receipt_path)
    values = dict(receipt)
    values["durable_at_utc"] = completion["durable_at_utc"]
    values["receipt_relative"] = receipt_relative
    values["receipt_sha256"] = receipt_sha256
    values["binding_sha256"] = hashlib.sha256(canonical_json_bytes(values)).hexdigest()
    if not manifest_sha:
        raise ValueError("history receipt manifest differs")
    return SourceBinding(**values)


def _binding_from_receipt(root: Path, receipt_path: Path) -> SourceBinding:
    observation_id = receipt_path.name.removesuffix(".json")
    if receipt_path != _receipt_path(root, observation_id):
        raise ValueError("history receipt path differs from observation identity")
    with _receipt_publication_lock(root, observation_id):
        return _binding_from_receipt_locked(root, receipt_path)


def _verified_artifacts(store: RawStore) -> list[dict[str, object]]:
    if isinstance(store, _CapabilityRawStore):
        return store.verify_manifests()
    root = _store_history_root(store)
    rows = store.verify_manifests()
    for row in rows:
        manifest_path = Path(row["manifest_path"])
        manifest_bytes = _read_immutable(
            root, manifest_path, "history raw manifest"
        )
        seal_path = manifest_path.with_name(
            manifest_path.name.removesuffix(".json") + ".sha256"
        )
        seal_bytes = _read_immutable(root, seal_path, "history raw manifest seal")
        payload_bytes = _read_immutable(
            root,
            Path(row["payload_path"]),
            "history raw payload",
            max_bytes=MAX_STATCAST_BYTES,
        )
        if (
            manifest_bytes is None
            or hashlib.sha256(manifest_bytes).hexdigest() != row["manifest_sha256"]
            or seal_bytes
            != f"{hashlib.sha256(manifest_bytes).hexdigest()}\n".encode()
            or payload_bytes is None
            or hashlib.sha256(payload_bytes).hexdigest()
            != row["manifest"]["sha256"]
        ):
            raise ValueError("history raw artifact identity differs")
    return rows


def _store_history_root(store: RawStore | _CapabilityRawStore) -> Path:
    if isinstance(store, _CapabilityRawStore):
        return store.history_root
    return store.root.parents[2]


def _find_artifact(store: RawStore, source: str, query: Mapping[str, object]) -> dict[str, object] | None:
    encoded = json.dumps(dict(query), sort_keys=True, separators=(",", ":"))
    matches = [
        row for row in _verified_artifacts(store)
        if row["manifest"].get("source") == source
        and json.dumps(row["manifest"].get("query"), sort_keys=True, separators=(",", ":")) == encoded
    ]
    if len(matches) > 1:
        raise ValueError("history cache query is ambiguous")
    return matches[0] if matches else None


def _store_history_observation(
    root: Path,
    store: RawStore,
    *,
    raw_source: str,
    source: str,
    binding_role: str,
    query: Mapping[str, object],
    game_pks: tuple[int, ...],
    fetcher: ObservationFetcher,
    max_bytes: int = MAX_JSON_BYTES,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[bytes, SourceBinding]:
    normalized = dict(query)
    observation_id = _observation_id(binding_role, normalized)
    with _receipt_publication_lock(root, observation_id):
        return _store_history_observation_locked(
            root,
            store,
            raw_source=raw_source,
            source=source,
            binding_role=binding_role,
            query=normalized,
            game_pks=game_pks,
            fetcher=fetcher,
            max_bytes=max_bytes,
            sleeper=sleeper,
        )


def _store_history_observation_locked(
    root: Path,
    store: RawStore,
    *,
    raw_source: str,
    source: str,
    binding_role: str,
    query: Mapping[str, object],
    game_pks: tuple[int, ...],
    fetcher: ObservationFetcher,
    max_bytes: int,
    sleeper: Callable[[float], None],
) -> tuple[bytes, SourceBinding]:
    """Return one immutable artifact and its persisted, reloadable receipt."""
    normalized = dict(query)
    existing = _find_artifact(store, raw_source, normalized)
    observation_id = _observation_id(binding_role, normalized)
    receipt_path = _receipt_path(root, observation_id)
    url = normalized.get("url")
    if not isinstance(url, str):
        raise ValueError("history query URL is required")
    if existing is not None:
        prefix = _read_receipt_prefix(root, receipt_path)
        if prefix[0] is None:
            http = _fetch_with_retry(source, url, fetcher, max_bytes, sleeper)
            payload_path = Path(existing["payload_path"])
            payload = bytes(existing["payload_bytes"])
            if http.body != payload:
                raise ValueError(
                    "history response differs from authenticated raw prefix"
                )
            receipt = _history_receipt(
                root,
                source=source,
                binding_role=binding_role,
                normalized_query=normalized,
                game_pks=game_pks,
                observation_id=observation_id,
                http=http,
                payload_path=payload_path,
                payload_sha256=str(existing["manifest"]["sha256"]),
                manifest_sha256=str(existing["manifest_sha256"]),
            )
            _complete_receipt_prefix_locked(root, receipt_path, receipt)
        binding = _binding_from_receipt_locked(root, receipt_path)
        if (
            binding.binding_role != binding_role
            or binding.source != source
            or binding.query_identity_json != _query_json(normalized)
            or binding.game_pks != tuple(sorted(set(game_pks)))
            or binding.manifest_sha256 != existing["manifest_sha256"]
        ):
            raise ValueError("history cache receipt differs from artifact")
        return bytes(existing["payload_bytes"]), binding

    http = _fetch_with_retry(source, url, fetcher, max_bytes, sleeper)
    artifact = store.put_unique_query(raw_source, normalized, http.body)
    verified = _find_artifact(store, raw_source, normalized)
    if verified is None or Path(verified["payload_path"]) != artifact.data_path:
        raise ValueError("history artifact was not authenticated")
    receipt = _history_receipt(
        root,
        source=source,
        binding_role=binding_role,
        normalized_query=normalized,
        game_pks=game_pks,
        observation_id=observation_id,
        http=http,
        payload_path=artifact.data_path,
        payload_sha256=artifact.sha256,
        manifest_sha256=str(verified["manifest_sha256"]),
    )
    _complete_receipt_prefix_locked(root, receipt_path, receipt)
    return bytes(verified["payload_bytes"]), _binding_from_receipt_locked(
        root, receipt_path
    )


def _schedule_query(year: int, cutoff: date) -> dict[str, object]:
    start = date(year, 1, 1).isoformat()
    return {
        "start": start,
        "end": cutoff.isoformat(),
        "gameType": "R",
        "role": "history",
        "url": schedule_url(start, cutoff.isoformat()),
        "as_of_date": cutoff.isoformat(),
    }


def _load_or_fetch_schedule_history(
    store: RawStore, year: int, cutoff: date, *, fetcher: ObservationFetcher
) -> tuple[tuple[dict[str, object], ...], list[SourceBinding]]:
    root = _store_history_root(store)
    query = _schedule_query(year, cutoff)
    payload, _binding = _store_history_observation(
        root, store, raw_source=_SCHEDULE_SOURCE, source="stats_api",
        binding_role="HISTORY_SCHEDULE", query=query, game_pks=(), fetcher=fetcher,
    )
    validate_schedule_query_coverage(payload, str(query["start"]), str(query["end"]))
    games = tuple(dict(row) for row in parse_schedule(payload))
    if not games:
        raise ValueError("history schedule contains no regular-season games")
    if any(date.fromisoformat(str(row["official_date"])) > cutoff for row in games):
        raise ValueError("same-day leakage detected")
    return games, [_binding]


class _RecordingByteFetcher:
    def __init__(self, target: date, fetcher: ObservationFetcher):
        self.target = target
        self.fetcher = fetcher
        self.observations: dict[str, LiveHttpObservation] = {}

    def __call__(self, url: str, *, max_bytes: int) -> bytes:
        http = _http_for("savant", url, self.fetcher, max_bytes)
        parsed = parse_statcast(http.body)
        if any(date.fromisoformat(str(row["game_date"])) >= self.target for row in parsed.pitches):
            raise ValueError("same-day leakage detected")
        self.observations[url] = http
        return http.body


def _statcast_query_with_asof(query: Mapping[str, object], cutoff: date) -> dict[str, object]:
    result = dict(query)
    result["as_of_date"] = cutoff.isoformat()
    return result


def _persist_statcast_binding(
    root: Path,
    row: Mapping[str, object],
    cutoff: date,
    recorder: _RecordingByteFetcher,
    sleeper: Callable[[float], None],
) -> SourceBinding:
    query = row["manifest"]["query"]
    receipt_query = _statcast_query_with_asof(query, cutoff)
    observation_id = _observation_id("HISTORY_STATCAST", receipt_query)
    with _receipt_publication_lock(root, observation_id):
        return _persist_statcast_binding_locked(
            root, row, cutoff, recorder, sleeper
        )


def _persist_statcast_binding_locked(
    root: Path,
    row: Mapping[str, object],
    cutoff: date,
    recorder: _RecordingByteFetcher,
    sleeper: Callable[[float], None],
) -> SourceBinding:
    """Bind a cached Statcast artifact to this cutoff using persisted HTTP metadata."""
    query = row["manifest"]["query"]
    receipt_query = _statcast_query_with_asof(query, cutoff)
    receipt_path = _receipt_path(root, _observation_id("HISTORY_STATCAST", receipt_query))
    prefix = _read_receipt_prefix(root, receipt_path)
    if prefix[0] is not None:
        binding = _binding_from_receipt_locked(root, receipt_path)
        _validate_statcast_binding(root, binding, row, receipt_query)
        return binding
    observed = recorder.observations.get(str(query["url"]))
    if observed is None:
        for candidate in _receipt_json_candidates(root):
            try:
                prior = _binding_from_receipt(root, candidate)
            except ValueError:
                continue
            if (
                prior.binding_role == "HISTORY_STATCAST"
                and prior.manifest_sha256 == row["manifest_sha256"]
                and prior.requested_url == query["url"]
                and prior.payload_sha256 == row["manifest"]["sha256"]
                and prior.payload_relative == _relative(root, Path(row["payload_path"]))
            ):
                observed = LiveHttpObservation(
                    requested_url=prior.requested_url,
                    final_url=prior.final_url,
                    started_at_utc=prior.response_completed_at_utc,
                    response_completed_at_utc=prior.response_completed_at_utc,
                    http_status=prior.http_status,
                    content_type=prior.content_type,
                    etag=prior.etag,
                    last_modified=prior.last_modified,
                    body=bytes(row["payload_bytes"]),
                )
                break
    payload = bytes(row["payload_bytes"])
    if observed is None:
        observed = _fetch_with_retry(
            "savant",
            str(query["url"]),
            recorder.fetcher,
            MAX_STATCAST_BYTES,
            sleeper,
        )
        parsed = parse_statcast(observed.body)
        if any(
            date.fromisoformat(str(item["game_date"])) >= recorder.target
            for item in parsed.pitches
        ):
            raise ValueError("same-day leakage detected")
    if observed.body != payload:
        raise ValueError("history response differs from authenticated raw prefix")
    receipt = {
        "source": "savant", "binding_role": "HISTORY_STATCAST", "game_pks": (),
        "observation_id": _observation_id("HISTORY_STATCAST", receipt_query),
        "query_identity_json": _query_json(receipt_query), "requested_url": observed.requested_url,
        "final_url": observed.final_url, "response_completed_at_utc": observed.response_completed_at_utc,
        "http_status": observed.http_status, "content_type": observed.content_type,
        "etag": observed.etag, "last_modified": observed.last_modified,
        "payload_sha256": hashlib.sha256(payload).hexdigest(), "byte_length": len(payload),
        "payload_relative": _relative(root, Path(row["payload_path"])),
        "manifest_sha256": row["manifest_sha256"],
    }
    _complete_receipt_prefix_locked(root, receipt_path, receipt)
    binding = _binding_from_receipt_locked(root, receipt_path)
    _validate_statcast_binding(root, binding, row, receipt_query)
    return binding


def _validate_statcast_binding(
    root: Path,
    binding: SourceBinding,
    row: Mapping[str, object],
    receipt_query: Mapping[str, object],
) -> None:
    payload_path = Path(row["payload_path"])
    payload = bytes(row["payload_bytes"])
    expected_observation_id = _observation_id("HISTORY_STATCAST", receipt_query)
    if (
        binding.source != "savant"
        or binding.binding_role != "HISTORY_STATCAST"
        or binding.game_pks
        or binding.observation_id != expected_observation_id
        or binding.query_identity_json != _query_json(receipt_query)
        or binding.requested_url != row["manifest"]["query"]["url"]
        or binding.manifest_sha256 != row["manifest_sha256"]
        or binding.payload_sha256 != row["manifest"]["sha256"]
        or binding.byte_length != len(payload)
        or binding.payload_relative != _relative(root, payload_path)
    ):
        raise ValueError("history Statcast receipt differs from expected artifact")


def _load_or_fetch_statcast(
    store: RawStore, opening: date, cutoff: date, *, fetcher: ObservationFetcher,
    sleeper: Callable[[float], None],
) -> tuple[StatcastData, list[SourceBinding]]:
    if opening > cutoff:
        raise ValueError("season opening date follows D-1 cutoff")
    target = cutoff + timedelta(days=1)
    # Keep the date-generic acquisition boundary explicit; ``fetch_monthly``
    # consumes these same monthly partitions and recursively splits only capped
    # responses.
    requested_months = monthly_ranges(opening.isoformat(), cutoff.isoformat())
    if not requested_months:
        raise ValueError("Statcast monthly range is empty")
    catalog = _verified_artifacts(store)
    known = {
        json.dumps(row["manifest"]["query"], sort_keys=True, separators=(",", ":")): row["manifest"]["sha256"]
        for row in catalog if row["manifest"].get("source") == _STATCAST_SOURCE
    }
    recorder = _RecordingByteFetcher(target, fetcher)
    expected_days = {
        opening + timedelta(days=index)
        for index in range((cutoff - opening).days + 1)
    }
    covered_days: set[date] = set()
    for row in catalog:
        manifest = row["manifest"]
        query = manifest.get("query")
        if manifest.get("source") != _STATCAST_SOURCE or not isinstance(query, dict):
            continue
        try:
            first, last = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
        except (KeyError, TypeError, ValueError):
            continue
        days = {
            first + timedelta(days=index)
            for index in range((last - first).days + 1)
        }
        if days <= expected_days:
            covered_days.update(days)
    missing_days = sorted(expected_days - covered_days)
    missing_ranges: list[tuple[date, date]] = []
    for missing in missing_days:
        if not missing_ranges or missing != missing_ranges[-1][1] + timedelta(days=1):
            missing_ranges.append((missing, missing))
        else:
            missing_ranges[-1] = (missing_ranges[-1][0], missing)
    for missing_start, missing_end in missing_ranges:
        fetch_monthly(
            store,
            missing_start.isoformat(),
            missing_end.isoformat(),
            fetcher=recorder,
            sleeper=sleeper,
            known_queries=known,
            verified_catalog=catalog,
        )
        catalog = _verified_artifacts(store)
    catalog = _verified_artifacts(store)
    candidates: list[tuple[date, date, dict[str, object]]] = []
    for row in catalog:
        manifest = row["manifest"]
        query = manifest.get("query")
        if manifest.get("source") != _STATCAST_SOURCE or not isinstance(query, dict):
            continue
        if set(query) != {"start", "end", "game_type", "url"} or query.get("game_type") != "R":
            continue
        first, last = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
        if opening <= first <= last <= cutoff:
            candidates.append((first, last, row))
    selected: list[dict[str, object]] = []
    cursor = opening
    while cursor <= cutoff:
        options = [item for item in candidates if item[0] == cursor]
        if not options:
            raise ValueError("verified Statcast coverage is incomplete")
        # A later exact range supersedes an older partial prefix on an extended
        # cutoff; selecting one maximal child yields a single non-overlapping
        # partition without deleting historical immutable artifacts.
        first, last, row = max(options, key=lambda item: (item[1], str(item[2]["manifest_path"])))
        selected.append(row)
        cursor = last + timedelta(days=1)
    pitches: list[dict[str, object]] = []
    root = _store_history_root(store)
    for row in sorted(selected, key=lambda value: str(value["manifest"]["query"]["start"])):
        query = row["manifest"]["query"]
        parsed = parse_statcast(bytes(row["payload_bytes"]))
        if any(date.fromisoformat(str(item["game_date"])) >= target for item in parsed.pitches):
            raise ValueError("same-day leakage detected")
        pitches.extend(parsed.pitches)
    bindings = [
        _persist_statcast_binding(root, row, cutoff, recorder, sleeper)
        for row in sorted(selected, key=lambda value: str(value["manifest"]["query"]["start"]))
    ]
    parsed_all = StatcastData(tuple(pitches), tuple())
    by_game = {int(game_pk): game for game_pk, game in _schedule_games_by_pk_from_statcast(pitches).items()}
    del by_game  # Explicitly retain all raw rows; schedule validation happens below.
    return parsed_all, bindings


def _schedule_games_by_pk_from_statcast(pitches: Iterable[Mapping[str, object]]) -> dict[int, str]:
    result: dict[int, str] = {}
    for row in pitches:
        game_pk, game_day = int(row["game_pk"]), str(row["game_date"])
        if game_pk in result and result[game_pk] != game_day:
            raise ValueError("Statcast game_pk has conflicting game dates")
        result[game_pk] = game_day
    return result


def _venue_url(venue_id: int) -> str:
    if type(venue_id) is not int or venue_id <= 0:
        raise ValueError("venue_id must be a positive integer")
    return f"{MLB_BASE}/venues/{venue_id}?" + urlencode({"hydrate": "location"})


def _parse_venue(payload: bytes, venue_id: int) -> dict[str, object]:
    try:
        root = json.loads(payload.decode("utf-8"))
        values = root["venues"]
        raw = values[0]
        location = raw["location"]
        coordinates = location["defaultCoordinates"]
        timezone_id = location["timeZone"]["id"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError("venue reference is malformed") from exc
    if not isinstance(values, list) or len(values) != 1 or raw.get("id") != venue_id:
        raise ValueError("venue reference identity differs")
    name, city = raw.get("name"), location.get("city")
    latitude, longitude = coordinates.get("latitude"), coordinates.get("longitude")
    if not isinstance(name, str) or not name or not isinstance(city, str) or not city:
        raise ValueError("venue reference text differs")
    if type(latitude) not in {int, float} or type(longitude) not in {int, float} or not -90 <= float(latitude) <= 90 or not -180 <= float(longitude) <= 180:
        raise ValueError("venue reference coordinates differ")
    if not isinstance(timezone_id, str) or not timezone_id:
        raise ValueError("venue reference time zone differs")
    try:
        ZoneInfo(timezone_id)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("venue reference requires an IANA time zone") from exc
    return {"venue_name": name, "venue_city": city, "venue_latitude": float(latitude), "venue_longitude": float(longitude), "time_zone": timezone_id}


def _load_or_fetch_venue_references(
    store: RawStore, schedule: Iterable[Mapping[str, object]], cutoff: date, *, fetcher: ObservationFetcher
) -> tuple[dict[int, dict[str, object]], list[SourceBinding]]:
    root = _store_history_root(store)
    venue_ids = sorted({row.get("venue_id") for row in schedule if type(row.get("venue_id")) is int and row["venue_id"] > 0})
    if not venue_ids:
        raise ValueError("history schedule has no authenticated venue identities")
    references, bindings = {}, []
    for venue_id in venue_ids:
        query = {"venue_id": venue_id, "as_of_date": cutoff.isoformat(), "url": _venue_url(venue_id)}
        payload, binding = _store_history_observation(
            root, store, raw_source=_VENUE_SOURCE, source="stats_api", binding_role="HISTORY_VENUE",
            query=query, game_pks=(), fetcher=fetcher,
        )
        references[venue_id] = _parse_venue(payload, venue_id)
        bindings.append(binding)
    return references, bindings


def _build_result_free_live_schedule_history(
    schedule: Iterable[Mapping[str, object]], statcast: StatcastData,
    team_map: Mapping[str, str], venue_refs: Mapping[int, Mapping[str, object]],
    opening: date, cutoff: date, source_manifest_sha256: str,
) -> ScheduleHistoryBuild:
    schedule_by_pk = {int(row["game_pk"]): row for row in schedule}
    statcast_dates: dict[int, str] = {}
    max_inning_by_game: dict[int, int] = {}
    for pitch in statcast.pitches:
        game_pk = int(pitch["game_pk"])
        game_day = str(pitch["game_date"])
        if game_pk in statcast_dates and statcast_dates[game_pk] != game_day:
            raise ValueError("Statcast game_pk has conflicting game dates")
        statcast_dates[game_pk] = game_day
        max_inning_by_game[game_pk] = max(
            max_inning_by_game.get(game_pk, 0), int(pitch["inning"])
        )
    rows = []
    for game_pk, game_day in sorted(statcast_dates.items()):
        game = schedule_by_pk.get(game_pk)
        if game is None or str(game.get("official_date")) != game_day:
            raise ValueError("played Statcast game lacks matching D-1 schedule evidence")
        if not opening.isoformat() <= game_day <= cutoff.isoformat():
            raise ValueError("same-day leakage detected")
        away, home = game.get("away_team"), game.get("home_team")
        venue_id = game.get("venue_id")
        reference = venue_refs.get(venue_id)
        if not isinstance(away, str) or not isinstance(home, str) or away not in team_map or home not in team_map:
            raise ValueError("30-team map is incomplete")
        if not isinstance(reference, Mapping) or reference.get("venue_name") != game.get("venue_name"):
            raise ValueError("authenticated venue reference is missing")
        rows.append({
            "game_pk": game_pk, "official_date": game_day,
            "team_codes": [team_map[away], team_map[home]], "home_team_code": team_map[home],
            "venue_name": game["venue_name"], "innings": max_inning_by_game[game_pk],
            "doubleheader_marker": game.get("doubleheader_marker"), **dict(reference),
        })
    if not rows:
        raise ValueError("result-free schedule history has no Statcast-proven games")
    rows.sort(key=lambda row: (row["official_date"], row["game_pk"]))
    expected = tuple(row["game_pk"] for row in rows)
    return ScheduleHistoryBuild(tuple(rows), opening.isoformat(), cutoff.isoformat(), True, source_manifest_sha256, expected)


def _load_or_fetch_league_era(
    store: RawStore, opening: date, cutoff: date, *, fetcher: ObservationFetcher,
    sleeper: Callable[[float], None],
) -> tuple[dict[str, dict[str, object]], list[SourceBinding]]:
    root = store.root.parents[2]
    query = {"season_open": opening.isoformat(), "as_of_date": cutoff.isoformat(), "url": league_era_url(opening.isoformat(), cutoff.isoformat())}
    payload, binding = _store_history_observation(root, store, raw_source=_ERA_SOURCE, source="stats_api", binding_role="HISTORY_ERA", query=query, game_pks=(), fetcher=fetcher, sleeper=sleeper)
    parsed = parse_league_era(payload)
    return {cutoff.isoformat(): {"as_of_date": cutoff.isoformat(), **parsed, "source": "mlb_stats_api", "manifest_sha256": binding.manifest_sha256}}, [binding]


def _bound_source_dates(history: object, cutoff: date) -> tuple[str, ...]:
    values = [cutoff.isoformat()]
    for row in getattr(history, "pitches", ()):
        values.append(str(row["game_date"]))
    return tuple(values)


def _authenticated_history_catalog(
    store: RawStore, cutoff: date, bindings: Iterable[SourceBinding]
) -> Mapping[str, object]:
    admitted = {binding.manifest_sha256 for binding in bindings}
    rows = []
    for verified in _verified_artifacts(store):
        if verified["manifest_sha256"] not in admitted:
            continue
        rows.append({"source": verified["manifest"]["source"], "query": verified["manifest"]["query"], "manifest_sha256": verified["manifest_sha256"]})
    if {row["manifest_sha256"] for row in rows} != admitted:
        raise ValueError("history catalog differs from bound cutoff evidence")
    rows.sort(key=lambda item: (item["source"], _query_json(item["query"])))
    return {"namespace": _HISTORY_NAMESPACE, "cutoff_date": cutoff.isoformat(), "catalog_sha256": hashlib.sha256(canonical_json_bytes(rows)).hexdigest()}


def build_live_history(
    root: Path, target_date: str, card: FrozenLiveCard, *,
    fetcher: ObservationFetcher = fetch_live_http,
    sleeper: Callable[[float], None] = time.sleep,
) -> LiveHistoryBundle:
    with _history_root_session(Path(root)) as (bound_root, trusted):
        return _build_live_history_bound(
            bound_root,
            trusted,
            target_date,
            card,
            fetcher=fetcher,
            sleeper=sleeper,
        )


def _build_live_history_bound(
    root: Path,
    trusted: TrustedRoot,
    target_date: str,
    card: FrozenLiveCard,
    *,
    fetcher: ObservationFetcher,
    sleeper: Callable[[float], None],
) -> LiveHistoryBundle:
    target = date.fromisoformat(validate_target_date(target_date))
    if not isinstance(card, FrozenLiveCard):
        raise ValueError("card must be a FrozenLiveCard")
    cutoff = target - timedelta(days=1)
    store = _CapabilityRawStore(root, trusted)
    schedule, schedule_bindings = _load_or_fetch_schedule_history(store, target.year, cutoff, fetcher=fetcher)
    opening = min(date.fromisoformat(str(row["official_date"])) for row in schedule)
    raw_statcast, statcast_bindings = _load_or_fetch_statcast(store, opening, cutoff, fetcher=fetcher, sleeper=sleeper)
    # The Statcast data is date-safe before any schedule/result construction.
    assert_feature_dates(target_date, (str(row["game_date"]) for row in raw_statcast.pitches))
    team_map = derive_team_code_map(schedule, raw_statcast, target.year).mapping
    venue_refs, venue_bindings = _load_or_fetch_venue_references(store, schedule, cutoff, fetcher=fetcher)
    schedule_build = _build_result_free_live_schedule_history(
        schedule, raw_statcast, team_map, venue_refs, opening, cutoff,
        schedule_bindings[0].manifest_sha256,
    )
    schedule_by_pk = {int(row["game_pk"]): row for row in schedule}
    final_outs = {
        game_pk: schedule_by_pk[game_pk].get("final_outs")
        for game_pk in (int(row["game_pk"]) for row in schedule_build.rows)
    }
    final_halves = {
        game_pk: (int(schedule_by_pk[game_pk]["completed_innings"]), str(schedule_by_pk[game_pk]["final_inning_topbot"]))
        for game_pk in final_outs
        if type(schedule_by_pk[game_pk].get("completed_innings")) is int
        and schedule_by_pk[game_pk].get("final_inning_topbot") in {"Top", "Bot"}
    }
    statcast = derive_outs_recorded(raw_statcast, final_outs, final_half_by_game=final_halves)
    era, era_bindings = _load_or_fetch_league_era(store, opening, cutoff, fetcher=fetcher, sleeper=sleeper)
    history = build_feature_history(statcast, card.slot_weights, schedule_build)
    assert_feature_dates(target_date, _bound_source_dates(history, cutoff))
    source_bindings = tuple(
        schedule_bindings + statcast_bindings + venue_bindings + era_bindings
    )
    return LiveHistoryBundle(
        target_date=target_date, cutoff_date=cutoff.isoformat(), feature_history=history,
        league_era_by_asof=MappingProxyType(era), team_code_by_name=MappingProxyType(dict(team_map)),
        venue_references=MappingProxyType(dict(venue_refs)),
        cache_authority=_authenticated_history_catalog(store, cutoff, source_bindings),
        source_bindings=source_bindings,
    )


CAREER_START = "1876-01-01"


def live_career_bf_url(pitcher_id: int, as_of: str) -> str:
    if type(pitcher_id) is not int or pitcher_id <= 0:
        raise ValueError("pitcher_id must be a positive integer")
    as_of = validate_target_date(as_of)
    return f"{MLB_BASE}/people/{pitcher_id}/stats?" + urlencode({
        "stats": "byDateRange", "group": "pitching", "gameType": "R", "sportIds": 1,
        "startDate": CAREER_START, "endDate": as_of,
    })


def load_live_career_bf(
    root: Path, target_date: str, pitcher_ids: Iterable[int], *,
    fetcher: ObservationFetcher = fetch_live_http,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[Mapping[int, int], tuple[SourceBinding, ...]]:
    with _history_root_session(Path(root)) as (bound_root, trusted):
        return _load_live_career_bf_bound(
            bound_root,
            trusted,
            target_date,
            pitcher_ids,
            fetcher=fetcher,
            sleeper=sleeper,
        )


def _load_live_career_bf_bound(
    root: Path,
    trusted: TrustedRoot,
    target_date: str,
    pitcher_ids: Iterable[int],
    *,
    fetcher: ObservationFetcher,
    sleeper: Callable[[float], None],
) -> tuple[Mapping[int, int], tuple[SourceBinding, ...]]:
    target = date.fromisoformat(validate_target_date(target_date))
    cutoff = target - timedelta(days=1)
    store = _CapabilityRawStore(root, trusted)
    values: dict[int, int] = {}
    bindings: list[SourceBinding] = []
    for pitcher_id in sorted(set(pitcher_ids)):
        if type(pitcher_id) is not int or pitcher_id <= 0:
            raise ValueError("pitcher_id must be a positive integer")
        request = live_career_bf_url(pitcher_id, cutoff.isoformat())
        query = {"pitcher_id": pitcher_id, "as_of_date": cutoff.isoformat(), "url": request}
        payload, binding = _store_history_observation(root, store, raw_source=_CAREER_SOURCE, source="stats_api", binding_role="CAREER_BF", query=query, game_pks=(), fetcher=fetcher, sleeper=sleeper)
        if date.fromisoformat(query["as_of_date"]) >= target:
            raise ValueError("career BF as_of must precede target date")
        values[pitcher_id] = parse_prior_career_bf(payload, pitcher_id)
        bindings.append(binding)
    return MappingProxyType(values), tuple(bindings)


__all__ = ["build_live_history", "live_career_bf_url", "load_live_career_bf"]
