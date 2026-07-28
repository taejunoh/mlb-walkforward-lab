from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from jsonschema import Draft202012Validator, FormatChecker
from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.protocols import RunSpec, SourceRunSpec


PHASES: tuple[str, ...] = (
    "DESIGN_ACCEPTED",
    "ACQUIRING",
    "SOURCES_READY",
    "OOF_SEALED",
    "CARDS_SEALED",
    "TARGET_LOCKED",
    "OUTCOMES_JOINED",
    "SAMPLE_500_SEALED",
    "VERIFIED",
)
SOURCE_ONLY_PHASES: tuple[str, ...] = (
    "DESIGN_ACCEPTED",
    "ACQUIRING",
    "SOURCES_READY",
)
Authority = RunSpec | SourceRunSpec
_UNSET = object()
_OLD_QUARANTINED_QUERY_ID = (
    "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8"
)
_DISPOSITIONED_QUERY_ID = (
    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
)
_RECOVERY_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts"
_RECOVERY_RUN_SPEC_PATH = (
    "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts.json"
)
_RECOVERY_CONTRACT_PATH = (
    "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts.json"
)
_RECOVERY_AUDIT_PREFIX = "audit/v5_2_qd/historical_recovery_schedule_conflicts/"
_RECOVERY_RAW_PREFIX = "data/v5_2_qd/historical_recovery_schedule_conflicts/raw/"
_SOURCE_RUN_SPEC_PATH = (
    "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
_SOURCE_CONTRACT_PATH = (
    "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
_SOURCE_AUDIT_PREFIX = "audit/v5_2_qd/historical_superseding_565534/"
_SOURCE_RAW_PREFIX = "data/v5_2_qd/historical_superseding_565534/raw/"
_SOURCE_EXECUTION_RECEIPT = re.compile(
    r"^audit/v5_2_qd/historical_superseding_565534/execution/"
    r"phase_receipts/000001-ACQUIRING-[0-9a-f]{64}\.json$"
)
_SOURCE_QUERY_EVIDENCE = re.compile(
    r"^data/v5_2_qd/historical_superseding_565534/raw/(?:"
    r"queries/[A-Za-z0-9_-]+/(?P<query_id>[0-9a-f]{64})/(?:"
    r"attempts/[0-9]{6}\.json|success\.json)"
    r"|quarantine/[A-Za-z0-9_-]+/(?P<quarantine_query_id>[0-9a-f]{64})/"
    r"[0-9]{6}\.json)$"
)


def _authority_phases(authority: Authority) -> tuple[str, ...]:
    return SOURCE_ONLY_PHASES if isinstance(authority, SourceRunSpec) else PHASES


def _source_inventory_path(authority: Authority) -> str:
    if isinstance(authority, RunSpec):
        return _SOURCE_INVENTORY_PATH
    return (
        _authority_namespace(authority, "sources") / "source_inventory.json"
    ).as_posix()


def _acquisition_prefix(authority: Authority, child: str) -> str:
    if isinstance(authority, RunSpec):
        return f"audit/v5_2_qd/historical/acquisition/{child}/"
    return (
        _authority_namespace(authority, "audit") / "acquisition" / child
    ).as_posix() + "/"


def _checkpoint_progress_is_read_only(
    authority: Authority,
    phase: object,
    checkpoint_head: object | None,
) -> bool:
    phases = _authority_phases(authority)
    if not isinstance(phase, str) or phase not in phases:
        raise ValueError("execution phase authority differs")
    return (
        checkpoint_head is not None
        or phases.index(phase) >= phases.index("SOURCES_READY")
    )

_SUCCESS_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "run_id",
        "sequence",
        "phase",
        "prior_receipt_sha256",
        "recorded_at_utc",
        "git_head",
        "run_spec",
        "approved_design",
        "implementation_plan",
        "acquisition_contract",
        "acquisition_checkpoint_head",
        "task0_lineage",
        "inventory",
        "validation_receipts",
    }
)
_QUARANTINE_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "run_id",
        "attempted_phase",
        "current_phase",
        "current_receipt_sha256",
        "recorded_at_utc",
        "git_head",
        "run_spec",
        "validation_receipts",
    }
)
_AUTOMATIC_QUARANTINE_KEYS = _QUARANTINE_KEYS | {
    "inventory",
    "error_classification",
}
_QUARANTINE_ERROR_CLASSIFICATIONS = frozenset(
    {
        "DIRECT_INVENTORY_MISMATCH",
        "SOURCE_GATE_EVIDENCE_INVALID",
    }
)
_AUTOMATIC_QUARANTINE_CLASSIFICATION_BY_PHASE = {
    "ACQUIRING": "DIRECT_INVENTORY_MISMATCH",
    "SOURCES_READY": "SOURCE_GATE_EVIDENCE_INVALID",
}
_BINDING_KEYS = frozenset({"path", "sha256"})
_CHECKPOINT_HEAD_KEYS = frozenset({"count", "path", "sequence", "sha256", "stage"})
_PLAN_BINDING_KEYS = frozenset({"path", "sha256", "git_blob_id"})
_LINEAGE_KEYS = frozenset({"baseline_inventory", "protected_inventory"})
_SUCCESS_NAME = re.compile(
    r"^(?P<sequence>[0-9]{6})-(?P<phase>[A-Z0-9_]+)-(?P<sha>[0-9a-f]{64})\.json$"
)
_CHECKPOINT_NAME = re.compile(
    r"^(?P<sequence>[0-9]{8})-(?P<sha>[0-9a-f]{64})\.json$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_BOUND_EXECUTION_CODE = (
    "mlb_backtest/diagnostics.py",
    "mlb_backtest/execution_registry.py",
    "mlb_backtest/locking.py",
    "mlb_backtest/metrics.py",
    "mlb_backtest/outcomes.py",
    "mlb_backtest/protocols.py",
    "mlb_backtest/secure_fs.py",
    "mlb_backtest/source_parsers/__init__.py",
    "mlb_backtest/source_parsers/pitching.py",
    "mlb_backtest/source_parsers/schedule.py",
    "mlb_backtest/source_parsers/statcast.py",
    "mlb_backtest/sources.py",
    "mlb_backtest/v52_acquire.py",
    "mlb_backtest/v52_duplicate_gamepk_disposition.py",
    "mlb_backtest/v52_evidence.py",
    "mlb_backtest/v52_game_evidence_inventory.py",
    "mlb_backtest/v52_http.py",
    "mlb_backtest/v52_legacy.py",
    "mlb_backtest/v52_legacy_decision_inventory.py",
    "mlb_backtest/v52_legacy_relation.py",
    "mlb_backtest/v52_protected_inventory.py",
    "mlb_backtest/v52_query_plan.py",
    "mlb_backtest/v52_schedule_inventory.py",
    "mlb_backtest/v52_seasons.py",
    "mlb_backtest/v52_source_contracts.py",
    "mlb_backtest/v52_source_finalize.py",
    "mlb_backtest/v52_source_orchestration.py",
    "mlb_backtest/v52_snapshot_store.py",
    "mlb_backtest/v52_sources/__init__.py",
    "mlb_backtest/v52_sources/history.py",
    "mlb_backtest/v52_sources/pitching.py",
    "mlb_backtest/v52_sources/references.py",
    "mlb_backtest/v52_sources/schedule.py",
    "mlb_backtest/v52_sources/statcast.py",
    "mlb_backtest/v52_timezone_review.py",
    "mlb_backtest/v52_verify_sources.py",
    "mlb_backtest/verify_v52_design.py",
    "schemas/v5_2_acquisition_contract.schema.json",
    "schemas/v5_2_acquisition_checkpoint.schema.json",
    "schemas/v5_2_clean_source_replay_receipt.schema.json",
    "schemas/v5_2_derived_disposition_sidecar.schema.json",
    "schemas/v5_2_execution_receipt.schema.json",
    "schemas/v5_2_game_evidence_inventory.schema.json",
    "schemas/v5_2_legacy_decision_inventory.schema.json",
    "schemas/v5_2_schedule_anchor_inventory.schema.json",
    "schemas/v5_2_source_inventory.schema.json",
    "schemas/v5_2_source_receipt_commit.schema.json",
    "schemas/v5_2_source_review_receipt.schema.json",
    "schemas/v5_2_split_receipt.schema.json",
    "schemas/v5_2_timezone_candidate.schema.json",
    "schemas/v5_2_timezone_review_receipt.schema.json",
    "schemas/v5_2_timezone_seal_receipt.schema.json",
    "schemas/v5_2_venue_timezones.schema.json",
)
ACQUISITION_STAGES: tuple[str, ...] = (
    "STATIC_SCHEDULES",
    "REFERENCE_INPUTS",
    "TIMEZONE_REVIEW",
    "PARTICIPANT_DERIVED",
    "REMAINING",
)
_CHECKPOINT_KEYS = frozenset(
    {
        "acquisition_contract",
        "completed_query_ids",
        "execution_phase",
        "execution_phase_receipt",
        "git_head",
        "pending_query_ids",
        "prior_checkpoint_sha256",
        "quarantined_query_ids",
        "query_batches",
        "query_evidence_receipts",
        "receipt_kind",
        "recorded_at_utc",
        "run_id",
        "run_spec",
        "schema_version",
        "sequence",
        "split_query_ids",
        "split_receipts",
        "stage",
        "stage_evidence_receipts",
    }
)
_SOURCE_CHECKPOINT_KEYS = _CHECKPOINT_KEYS | {"dispositioned_query_ids"}
_RECOVERY_DISPOSITION_BINDING_KEYS = frozenset(
    {
        "game_pk",
        "query_id",
        "query_sidecar",
        "success_receipt",
        "retained_scheduled_start_utc",
        "played_date",
        "result_available_on",
        "raw_object_sha256s",
        "selected_raw_game_object_sha256s_sha256",
        "normalized_games_sha256",
        "sidecar_semantics_sha256",
        "expected_derived_sidecar_sha256",
    }
)
_RECOVERY_CHECKPOINT_KEYS = _CHECKPOINT_KEYS | {
    "dispositioned_query_ids",
    "recovery_disposition_bindings",
}
_SOURCE_INVENTORY_PATH = "data/v5_2_qd/historical/sources/source_inventory.json"
_SOURCE_REVIEW_PREFIX = "audit/v5_2_qd/historical/acquisition/source_reviews/"
_SOURCE_REPLAY_PREFIX = "audit/v5_2_qd/historical/acquisition/source_replays/"
_SOURCE_DISPOSITION_SIDECAR_PATH = (
    "audit/v5_2_qd/historical_superseding_565534/acquisition/query_sidecars/"
    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1.json"
)
_SOURCE_DISPOSITION_SIDECAR_SHA256 = (
    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1"
)
_SOURCE_DISPOSITION_SUCCESS_PATH = (
    "data/v5_2_qd/historical_superseding_565534/raw/queries/"
    "mlb_schedule_game_feed/"
    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095/"
    "success.json"
)
_SCHEDULE_INVENTORY_PATH = (
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
_OPEN_CHECKPOINT_STAGES = frozenset(
    {
        "STATIC_SCHEDULES",
        "REFERENCE_INPUTS",
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }
)
_TIMEZONE_STAGE_EVIDENCE_PATHS = (
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json",
    "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
    "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
    "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json",
    "audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json",
    "config/v5_2_qd/venue_timezones.json",
)


def _timezone_stage_evidence_paths(run_spec: Authority) -> tuple[str, ...]:
    if isinstance(run_spec, SourceRunSpec):
        audit = _authority_namespace(run_spec, "audit")
        return (
            (audit / "acquisition/schedule_anchor_inventory.json").as_posix(),
            (audit / "acquisition/timezone_reviews/coverage.json").as_posix(),
            (audit / "acquisition/timezone_reviews/semantic.json").as_posix(),
            (audit / "acquisition/venue_timezone_candidate.json").as_posix(),
            (audit / "acquisition/venue_timezone_seal.json").as_posix(),
            "config/v5_2_qd/venue_timezones.json",
        )
    return _TIMEZONE_STAGE_EVIDENCE_PATHS
_GAME_EVIDENCE_INVENTORY_PATH = (
    "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json"
)
_PROTECTED_RAW_INVENTORY_PATH = "audit/v5_1_bt/protected_raw_inventory.json"
_LEGACY_DECISION_INVENTORY_PATH = (
    "audit/v5_2_qd/historical/acquisition/legacy_decision_inventory.json"
)
_PARTICIPANT_STAGE_EVIDENCE_PATHS = tuple(
    sorted((*_TIMEZONE_STAGE_EVIDENCE_PATHS, _GAME_EVIDENCE_INVENTORY_PATH))
)


class _ReceiptMembershipError(ValueError):
    """The visible receipt directory cannot be safely changed by this operation."""


class _LateSourceEvidenceError(ValueError):
    """A selected SOURCES_READY dependent artifact changed before publication."""


@dataclass(frozen=True)
class ExecutionRegistryState:
    phase: str
    sequence: int
    receipt_sha256: str
    inventory_sha256: str


@dataclass(frozen=True)
class ExecutionPhaseAdvance:
    """The authenticated result of a phase advance or exact idempotent retry."""

    state: ExecutionRegistryState
    created: bool


@dataclass(frozen=True)
class AcquisitionCheckpoint:
    sequence: int
    receipt_sha256: str
    prior_checkpoint_sha256: str | None
    stage: str
    recorded_at_utc: str
    git_head: str
    execution_receipt_sha256: str
    query_batch_ids: tuple[str, ...]
    query_batch_sha256s: tuple[str, ...]
    query_evidence_receipt_sha256s: tuple[str, ...]
    stage_evidence_receipt_sha256s: tuple[str, ...]
    split_receipt_sha256s: tuple[str, ...]
    completed_query_ids: tuple[str, ...]
    pending_query_ids: tuple[str, ...]
    quarantined_query_ids: tuple[str, ...]
    split_query_ids: tuple[str, ...]
    stage_evidence_receipt_paths: tuple[str, ...]
    dispositioned_query_ids: tuple[str, ...] = ()
    recovery_disposition_bindings: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class _VerifiedSourceCheckpointAuthority:
    run_id: str
    stage: str
    query_batch_ids: tuple[str, ...]
    completed_query_ids: tuple[str, ...]
    pending_query_ids: tuple[str, ...]
    quarantined_query_ids: tuple[str, ...]
    split_query_ids: tuple[str, ...]
    dispositioned_query_ids: tuple[str, ...]
    _seal: object


_SOURCE_CHECKPOINT_CAPABILITY_SEAL = object()
_SOURCE_CHECKPOINT_AUTHORITY: ContextVar[
    _VerifiedSourceCheckpointAuthority | None
] = ContextVar("source_registry_checkpoint_authority", default=None)


@dataclass(frozen=True)
class _RegistryPaths:
    root: Path
    execution_relative: Path
    receipts_relative: Path
    quarantine_relative: Path
    checkpoints_relative: Path
    lock_relative: Path


@dataclass(frozen=True)
class _ArtifactSnapshot:
    path: str
    contents: bytes
    sha256: str
    identity: tuple[int, ...]

    @property
    def binding(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class _GitFileSnapshot:
    artifact: _ArtifactSnapshot
    blob_id: str
    mode: str


@dataclass(frozen=True)
class _GitSnapshot:
    head: str
    files: tuple[_GitFileSnapshot, ...]


@dataclass(frozen=True)
class _RegistryLockBinding:
    lock_descriptor: int
    directory_descriptors: tuple[int, ...]

    @property
    def execution_descriptor(self) -> int:
        return self.directory_descriptors[-1]


def _exact_object(value: object, keys: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} keys differ")
    return value


def _sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _validate_artifact_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or ".." in Path(value).parts
    ):
        raise ValueError(f"{label} path violates the receipt schema")
    return value


def _validate_binding_value(
    value: object, label: str, *, plan: bool = False
) -> Mapping[str, object]:
    binding = _exact_object(
        value, _PLAN_BINDING_KEYS if plan else _BINDING_KEYS, label
    )
    _validate_artifact_path(binding.get("path"), label)
    digest = binding.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} SHA-256 violates the receipt schema")
    if plan:
        blob = binding.get("git_blob_id")
        if not isinstance(blob, str) or not _GIT_HEAD.fullmatch(blob):
            raise ValueError(f"{label} Git blob violates the receipt schema")
    return binding


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


def _root_path(root: Path) -> Path:
    supplied = Path(os.path.abspath(os.fspath(root)))
    try:
        for node in (*reversed(supplied.parents), supplied):
            metadata = node.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    "execution registry root ancestor must be a no-follow directory"
                )
    except OSError as exc:
        raise ValueError("execution registry root is unavailable") from exc
    return supplied


def _authority_namespace(authority: Authority, key: str) -> Path:
    namespaces = authority.raw.get("namespaces")
    value = namespaces.get(key) if isinstance(namespaces, Mapping) else None
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"execution registry {key} namespace differs")
    relative = Path(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"execution registry {key} namespace differs")
    return relative


def _paths(root: Path, run_spec: Authority) -> _RegistryPaths:
    root_path = _root_path(root)
    audit = _authority_namespace(run_spec, "audit")
    expected_audit = (
        Path("audit/v5_2_qd/historical_superseding_565534")
        if isinstance(run_spec, SourceRunSpec)
        else Path("audit/v5_2_qd/historical")
    )
    if audit != expected_audit:
        raise ValueError("execution registry requires the frozen audit namespace")
    execution = audit / "execution"
    return _RegistryPaths(
        root=root_path,
        execution_relative=execution,
        receipts_relative=execution / "phase_receipts",
        quarantine_relative=execution / "quarantine",
        checkpoints_relative=execution / "acquisition_checkpoints",
        lock_relative=execution / ".registry.lock",
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory(root: Path, relative: Path, *, create: bool) -> tuple[int, list[int]]:
    descriptors: list[int] = []
    current = os.open(root, _directory_flags())
    descriptors.append(current)
    try:
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise ValueError("execution registry path traversal is forbidden")
            created = False
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(component, _directory_flags(), dir_fd=current)
            descriptors.append(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ValueError("execution registry directory identity changed")
            if created:
                os.fsync(current)
            current = child
        return current, descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _open_directory_at(
    directory_descriptor: int, relative: Path
) -> tuple[int, list[int]]:
    descriptors = [os.dup(directory_descriptor)]
    current = descriptors[0]
    try:
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise ValueError("execution registry path traversal is forbidden")
            child = os.open(component, _directory_flags(), dir_fd=current)
            descriptors.append(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise ValueError("execution registry directory identity changed")
            current = child
        return current, descriptors
    except BaseException:
        _close_directories(descriptors)
        raise


def _close_directories(descriptors: Sequence[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _relative_path(root: Path, path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        lexical = Path(os.path.abspath(candidate))
        lexical_relative = lexical.relative_to(root)
        current = root
        for component in lexical_relative.parts:
            current = current / component
            component_stat = current.lstat()
            if stat.S_ISLNK(component_stat.st_mode):
                raise ValueError(f"{label} symlink ancestor is forbidden")
        normalized = lexical.resolve(strict=True)
        relative = normalized.relative_to(root)
        if normalized != lexical:
            raise ValueError(f"{label} must not resolve through an alias")
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} must be an existing path under repository root") from exc
    if not relative.parts:
        raise ValueError(f"{label} must be a file")
    return relative


def _read_fd_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_file_at(
    parent_fd: int, name: str, label: str
) -> tuple[bytes, tuple[int, ...]]:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"{label} must be an unaliased regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise ValueError(f"{label} identity changed before read")
        contents = _read_fd_all(descriptor)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        final_opened = os.fstat(descriptor)
        if _identity(after) != _identity(before) or _identity(final_opened) != _identity(before):
            raise ValueError(f"{label} identity changed during read")
        return contents, _identity(final_opened)
    except OSError as exc:
        raise ValueError(f"{label} is unsafe or unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_relative(root: Path, relative: Path, label: str) -> bytes:
    parent_fd, descriptors = _open_directory(root, relative.parent, create=False)
    try:
        return _read_file_at(parent_fd, relative.name, label)[0]
    finally:
        _close_directories(descriptors)


def _read_relative_at(
    directory_descriptor: int, relative: Path, label: str
) -> bytes:
    parent_fd, descriptors = _open_directory_at(
        directory_descriptor, relative.parent
    )
    try:
        return _read_file_at(parent_fd, relative.name, label)[0]
    finally:
        _close_directories(descriptors)


def _read_relative_snapshot_at(
    directory_descriptor: int, relative: Path, label: str
) -> tuple[bytes, tuple[int, ...]]:
    parent_fd, descriptors = _open_directory_at(
        directory_descriptor, relative.parent
    )
    try:
        return _read_file_at(parent_fd, relative.name, label)
    finally:
        _close_directories(descriptors)


def _execution_child(paths: _RegistryPaths, relative: Path) -> Path:
    try:
        child = relative.relative_to(paths.execution_relative)
    except ValueError as exc:
        raise ValueError("execution registry path escaped its namespace") from exc
    if not child.parts:
        raise ValueError("execution registry child path is required")
    return child


def _read_path(root: Path, path: Path, label: str) -> tuple[str, bytes]:
    relative = _relative_path(root, path, label)
    return relative.as_posix(), _read_relative(root, relative, label)


def _capture_artifact(root: Path, path: Path, label: str) -> _ArtifactSnapshot:
    relative = _relative_path(root, path, label)
    parent_fd, descriptors = _open_directory(root, relative.parent, create=False)
    try:
        contents, identity = _read_file_at(parent_fd, relative.name, label)
    finally:
        _close_directories(descriptors)
    lexical = relative.as_posix()
    _validate_artifact_path(lexical, label)
    return _ArtifactSnapshot(
        path=lexical,
        contents=contents,
        sha256=_sha256_bytes(contents),
        identity=identity,
    )


def _verify_artifact_snapshot(
    root: Path, snapshot: _ArtifactSnapshot, label: str
) -> None:
    if _capture_artifact(root, root / snapshot.path, label) != snapshot:
        raise ValueError(f"{label} identity or bytes changed after trusted snapshot")


def _canonical_object(contents: bytes, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(payload, Mapping) or canonical_json_bytes(payload) + b"\n" != contents:
        raise ValueError(f"{label} is not canonical JSON with one newline")
    return payload


def _sha256_list(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be a non-empty unique SHA-256 list")
    return tuple(value)


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"{label} must be a non-empty unique string list")
    return tuple(value)


def _sorted_string_list(value: object, label: str) -> tuple[str, ...]:
    values = _string_list(value, label)
    if tuple(sorted(values)) != values:
        raise ValueError(f"{label} must be canonically sorted")
    return values


@dataclass(frozen=True)
class _SourceInventoryPayload:
    snapshot_sha256s: tuple[str, ...]
    logical_query_ids: tuple[str, ...]
    duplicate_gamepk_disposition: Mapping[str, object] | None
    dispositioned_query_ids: tuple[str, ...]
    terminal_checkpoint: Mapping[str, object] | None
    schedule_conflict_dispositions: Mapping[str, object] | None = None


def _source_inventory_payload(
    contents: bytes, *, run_id: str
) -> _SourceInventoryPayload:
    payload = _canonical_object(contents, "source inventory")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
        or payload.get("status") != "READY_CANDIDATE"
        or payload.get("critical_errors") != []
        or type(payload.get("network_attempts")) is not int
        or payload.get("network_attempts") != 0
        or payload.get("source_gate_ready") is not True
    ):
        raise ValueError("source inventory is not ready for the source gate")
    snapshots = _sha256_list(payload.get("snapshot_sha256s"), "source snapshots")
    query_ids = _sorted_string_list(
        payload.get("logical_query_ids"), "inventory query IDs"
    )
    recovery_run = run_id == _RECOVERY_RUN_ID
    source_run = run_id == "historical_walkforward_500_superseding_565534"
    if recovery_run:
        if {"duplicate_gamepk_disposition", "dispositioned_query_ids"}.intersection(payload):
            raise ValueError("recovery source inventory contains superseding disposition authority")
        catalog = _exact_object(
            payload.get("schedule_conflict_dispositions"),
            frozenset({"entries", "query_ids"}),
            "recovery source inventory disposition catalog",
        )
        entries = catalog.get("entries")
        query_ids = _checkpoint_sorted_sha_list(
            catalog.get("query_ids"), "recovery source inventory disposition query IDs"
        )
        if not isinstance(entries, list) or len(entries) != 3:
            raise ValueError("recovery source inventory requires three disposition bindings")
        expected_games = (565534, 630882, 633224)
        normalized_entries: list[dict[str, object]] = []
        for game_pk, entry in zip(expected_games, entries):
            value = _exact_object(
                entry,
                frozenset({
                    "game_pk", "query_id", "query_sidecar", "success_receipt",
                    "retained_scheduled_start_utc", "played_date", "result_available_on",
                    "raw_object_sha256s", "selected_raw_game_object_sha256s_sha256",
                    "normalized_games_sha256", "sidecar_semantics_sha256",
                }),
                "recovery source inventory disposition binding",
            )
            if value.get("game_pk") != game_pk:
                raise ValueError("recovery source inventory disposition catalog order differs")
            query_id = value.get("query_id")
            if not isinstance(query_id, str) or not _SHA256.fullmatch(query_id):
                raise ValueError("recovery source inventory disposition query differs")
            for name in ("query_sidecar", "success_receipt"):
                _validate_binding_value(value.get(name), "recovery source inventory disposition binding")
            if (
                not isinstance(value.get("retained_scheduled_start_utc"), str)
                or not isinstance(value.get("raw_object_sha256s"), list)
                or not value["raw_object_sha256s"]
                or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in value["raw_object_sha256s"])
                or any(not isinstance(value.get(name), str) or not _SHA256.fullmatch(value[name]) for name in ("selected_raw_game_object_sha256s_sha256", "normalized_games_sha256", "sidecar_semantics_sha256"))
            ):
                raise ValueError("recovery source inventory disposition binding differs")
            expected_available = {
                565534: (None, None),
                630882: ("2020-08-14", "2020-08-14"),
                633224: ("2021-09-24", "2021-09-24"),
            }[game_pk]
            if (value.get("played_date"), value.get("result_available_on")) != expected_available:
                raise ValueError("recovery source inventory availability binding differs")
            normalized_entries.append(dict(value))
        if query_ids != tuple(sorted(str(entry["query_id"]) for entry in normalized_entries)):
            raise ValueError("recovery source inventory disposition query IDs differ")
        terminal = _exact_object(
            payload.get("terminal_checkpoint"),
            frozenset({"path", "sha256", "sequence", "stage"}),
            "recovery source inventory terminal checkpoint",
        )
        _validate_binding_value(
            {"path": terminal.get("path"), "sha256": terminal.get("sha256")},
            "recovery source inventory terminal checkpoint",
        )
        if type(terminal.get("sequence")) is not int or terminal.get("stage") != "REMAINING":
            raise ValueError("recovery source inventory terminal checkpoint differs")
        return _SourceInventoryPayload(
            snapshots, query_ids, None, query_ids, dict(terminal),
            {"entries": normalized_entries, "query_ids": list(query_ids)},
        )
    disposition_present = {
        "duplicate_gamepk_disposition",
        "dispositioned_query_ids",
    }.intersection(payload)
    if not source_run:
        if disposition_present:
            raise ValueError("old source inventory contains disposition authority")
        return _SourceInventoryPayload(snapshots, query_ids, None, (), None)
    if disposition_present != {
        "duplicate_gamepk_disposition",
        "dispositioned_query_ids",
    }:
        raise ValueError("source inventory disposition authority is incomplete")
    disposition = _exact_object(
        payload["duplicate_gamepk_disposition"],
        frozenset({"query_id", "query_sidecar", "success_receipt"}),
        "source inventory disposition",
    )
    if disposition.get("query_id") != _DISPOSITIONED_QUERY_ID:
        raise ValueError("source inventory disposition query differs")
    sidecar = _validate_binding_value(
        disposition.get("query_sidecar"), "source inventory disposition sidecar"
    )
    success = _validate_binding_value(
        disposition.get("success_receipt"), "source inventory disposition success"
    )
    if (
        sidecar != {
            "path": _SOURCE_DISPOSITION_SIDECAR_PATH,
            "sha256": _SOURCE_DISPOSITION_SIDECAR_SHA256,
        }
        or success.get("path") != _SOURCE_DISPOSITION_SUCCESS_PATH
    ):
        raise ValueError("source inventory disposition binding differs")
    dispositioned = _checkpoint_sorted_sha_list(
        payload.get("dispositioned_query_ids"),
        "source inventory dispositioned query IDs",
    )
    if dispositioned != (_DISPOSITIONED_QUERY_ID,) or dispositioned[0] not in query_ids:
        raise ValueError("source inventory dispositioned query IDs differ")
    terminal = _exact_object(
        payload.get("terminal_checkpoint"),
        frozenset({"path", "sha256", "sequence", "stage"}),
        "source inventory terminal checkpoint",
    )
    _validate_binding_value(
        {"path": terminal.get("path"), "sha256": terminal.get("sha256")},
        "source inventory terminal checkpoint",
    )
    if (
        type(terminal.get("sequence")) is not int
        or int(terminal["sequence"]) < 0
        or terminal.get("stage") != "REMAINING"
    ):
        raise ValueError("source inventory terminal checkpoint differs")
    return _SourceInventoryPayload(
        snapshots,
        query_ids,
        dict(disposition),
        dispositioned,
        dict(terminal),
    )


def _capture_verified_source_disposition(
    root: Path,
    disposition_value: object,
) -> tuple[_ArtifactSnapshot, _ArtifactSnapshot]:
    """Capture and semantically authenticate the inode-bound 565534 receipt."""
    disposition = _exact_object(
        disposition_value,
        frozenset({"query_id", "query_sidecar", "success_receipt"}),
        "source inventory disposition",
    )
    sidecar_binding = _validate_binding_value(
        disposition.get("query_sidecar"),
        "source inventory disposition sidecar",
    )
    success_binding = _validate_binding_value(
        disposition.get("success_receipt"),
        "source inventory disposition success",
    )
    if (
        disposition.get("query_id") != _DISPOSITIONED_QUERY_ID
        or sidecar_binding
        != {
            "path": _SOURCE_DISPOSITION_SIDECAR_PATH,
            "sha256": _SOURCE_DISPOSITION_SIDECAR_SHA256,
        }
        or success_binding.get("path") != _SOURCE_DISPOSITION_SUCCESS_PATH
    ):
        raise ValueError("source inventory disposition binding differs")

    sidecar_snapshot = _capture_artifact(
        root,
        root / str(sidecar_binding["path"]),
        "source inventory disposition sidecar",
    )
    success_snapshot = _capture_artifact(
        root,
        root / str(success_binding["path"]),
        "source inventory disposition success",
    )
    if (
        sidecar_snapshot.binding != dict(sidecar_binding)
        or success_snapshot.binding != dict(success_binding)
    ):
        raise ValueError("source inventory disposition artifact binding differs")
    _canonical_object(
        sidecar_snapshot.contents,
        "source inventory disposition sidecar",
    )
    _canonical_object(
        success_snapshot.contents,
        "source inventory disposition success",
    )

    # Lazy imports avoid the query-planner/orchestration import cycle.
    from .v52_duplicate_gamepk_disposition import (
        build_duplicate_disposition_authority,
    )
    from .v52_evidence import V52EvidenceStore, logical_query_id

    authority = build_duplicate_disposition_authority(
        root,
        source_run_spec_path=root / _SOURCE_RUN_SPEC_PATH,
        acquisition_contract_path=root / _SOURCE_CONTRACT_PATH,
    )
    if (
        logical_query_id(authority.query) != _DISPOSITIONED_QUERY_ID
        or authority.evidence_root
        != root / "data/v5_2_qd/historical_superseding_565534/raw"
    ):
        raise ValueError("source inventory disposition authority differs")

    def inspect(verified: object) -> None:
        registered_query = getattr(verified, "registered_query", None)
        published = getattr(verified, "published_evidence", None)
        receipt = getattr(verified, "success_receipt", None)
        query_sidecar = getattr(verified, "query_sidecar", None)
        if (
            registered_query is None
            or published is None
            or not isinstance(receipt, Mapping)
            or not isinstance(query_sidecar, Mapping)
            or logical_query_id(registered_query) != _DISPOSITIONED_QUERY_ID
            or getattr(published, "storage_mode", None) != "DERIVED_DISPOSITION"
            or getattr(published, "query_sidecar_sha256", None)
            != _SOURCE_DISPOSITION_SIDECAR_SHA256
            or receipt.get("query_id") != _DISPOSITIONED_QUERY_ID
            or receipt.get("query_sidecar_sha256")
            != _SOURCE_DISPOSITION_SIDECAR_SHA256
            or receipt.get("storage_mode") != "DERIVED_DISPOSITION"
            or canonical_json_bytes(dict(receipt)) + b"\n"
            != success_snapshot.contents
            or canonical_json_bytes(dict(query_sidecar)) + b"\n"
            != sidecar_snapshot.contents
        ):
            raise ValueError(
                "source inventory disposition verified semantics differ"
            )

    V52EvidenceStore.open_read_only(authority.evidence_root).inspect_verified_success(
        authority.query,
        inspect,
        authority=authority,
    )
    return sidecar_snapshot, success_snapshot


def _validate_bound_schema(
    root: Path,
    trusted_git: _GitSnapshot,
    schema_name: str,
    payload: Mapping[str, object],
) -> _ArtifactSnapshot:
    snapshot = _capture_artifact(
        root,
        root / "schemas" / schema_name,
        "source receipt schema",
    )
    trusted = next(
        (
            item.artifact
            for item in trusted_git.files
            if item.artifact.path == snapshot.path
        ),
        None,
    )
    if trusted is None or snapshot != trusted:
        raise ValueError("source receipt schema trusted binding differs")
    schema = _canonical_object(snapshot.contents, "source receipt schema")
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).validate(payload)
    except Exception as exc:
        raise ValueError("source evidence receipt schema validation failed") from exc
    _verify_artifact_snapshot(root, snapshot, "source receipt schema")
    return snapshot


_SOURCE_RECEIPT_SCHEMAS = frozenset(
    {
        "v5_2_source_review_receipt.schema.json",
        "v5_2_clean_source_replay_receipt.schema.json",
        "v5_2_source_receipt_commit.schema.json",
    }
)


def validate_source_receipt_schema(
    root: Path,
    *,
    git_head: str,
    schema_name: Literal[
        "v5_2_source_review_receipt.schema.json",
        "v5_2_clean_source_replay_receipt.schema.json",
        "v5_2_source_receipt_commit.schema.json",
    ],
    payload: Mapping[str, object],
) -> None:
    """Validate a source receipt only against the current clean bound code.

    This is intentionally a small public trust boundary: receipt writers cannot
    fabricate the registry's private Git snapshot to validate a mutable schema.
    """
    if schema_name not in _SOURCE_RECEIPT_SCHEMAS:
        raise ValueError("source receipt schema is not approved")
    if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
        raise ValueError("source receipt Git binding differs")
    actual_head = _git(root, "rev-parse", "HEAD")
    if actual_head != git_head:
        raise ValueError("source receipt Git binding differs")
    files = tuple(
        _capture_git_file(root, relative, "bound execution code")
        for relative in _BOUND_EXECUTION_CODE
    )
    trusted = _GitSnapshot(actual_head, files)
    _validate_bound_schema(root, trusted, schema_name, payload)
    _verify_git_snapshot(root, trusted)


def _require_source_receipt_disposition(
    payload: Mapping[str, object],
    inventory: _SourceInventoryPayload,
    authority: Authority,
    *,
    label: str,
) -> None:
    if _is_recovery_source_run(authority):
        present = {
            "duplicate_gamepk_disposition",
            "dispositioned_query_ids",
            "schedule_conflict_dispositions",
        }.intersection(payload)
        if present != {"schedule_conflict_dispositions"}:
            raise ValueError(f"{label} recovery disposition authority is incomplete")
        if (
            inventory.schedule_conflict_dispositions is None
            or payload["schedule_conflict_dispositions"]
            != inventory.schedule_conflict_dispositions
        ):
            raise ValueError(f"{label} recovery disposition authority differs")
        return
    source_run = isinstance(authority, SourceRunSpec)
    fields = {"duplicate_gamepk_disposition", "dispositioned_query_ids"}
    present = fields.intersection(payload)
    if not source_run:
        if present:
            raise ValueError(f"{label} contains source disposition authority")
        return
    if inventory.duplicate_gamepk_disposition is None or present != fields:
        raise ValueError(f"{label} disposition authority is incomplete")
    disposition = _exact_object(
        payload["duplicate_gamepk_disposition"],
        frozenset({"query_id", "query_sidecar", "success_receipt"}),
        f"{label} disposition",
    )
    query_ids = _checkpoint_sorted_sha_list(
        payload["dispositioned_query_ids"], f"{label} dispositioned query IDs"
    )
    if (
        dict(disposition) != dict(inventory.duplicate_gamepk_disposition)
        or query_ids != inventory.dispositioned_query_ids
    ):
        raise ValueError(f"{label} disposition authority differs from inventory")


def _uncommitted_source_receipt_payload(
    root: Path,
    contents: bytes,
    *,
    path: str,
    run_id: str,
    inventory_sha256: str,
    snapshot_sha256s: tuple[str, ...],
    git_head: str,
    run_spec: Mapping[str, object],
    acquisition_contract: Mapping[str, object],
    trusted_git: _GitSnapshot,
    authority: Authority,
    source_inventory: _SourceInventoryPayload,
) -> tuple[str, tuple[str, ...], str | None, tuple[_ArtifactSnapshot, ...]]:
    payload = _canonical_object(contents, "source evidence receipt")
    schema_name = (
        "v5_2_source_review_receipt.schema.json"
        if payload.get("receipt_kind") == "SOURCE_REVIEW"
        else "v5_2_clean_source_replay_receipt.schema.json"
    )
    schema_snapshot = _validate_bound_schema(
        root, trusted_git, schema_name, payload
    )
    if (
        payload["run_id"] != run_id
        or payload.get("inventory_sha256") != inventory_sha256
        or tuple(payload["snapshot_sha256s"]) != snapshot_sha256s
    ):
        raise ValueError("source evidence receipt inventory binding failed")
    if payload["git_head"] != git_head:
        raise ValueError("source evidence receipt git binding differs")
    if (
        payload["run_spec"] != run_spec
        or payload["acquisition_contract"] != acquisition_contract
    ):
        raise ValueError("source evidence receipt protocol binding differs")
    _require_source_receipt_disposition(
        payload, source_inventory, authority, label="source evidence receipt"
    )
    kind = payload["receipt_kind"]
    queries = _sorted_string_list(payload["reviewed_query_ids"], "reviewed query IDs")
    review_prefix = _acquisition_prefix(authority, "source_reviews")
    replay_prefix = _acquisition_prefix(authority, "source_replays")
    if kind == "SOURCE_REVIEW":
        if (
            not path.startswith(review_prefix)
            or payload["decision"] != "APPROVED"
        ):
            raise ValueError("independent source review is not approved")
        document_binding = _validate_binding_value(
            payload["query_ids_document"], "review query IDs document"
        )
        if not str(document_binding["path"]).startswith(review_prefix):
            raise ValueError("review query ID document is outside review namespace")
        document = _capture_artifact(
            root,
            root / str(document_binding["path"]),
            "review query IDs document",
        )
        if document.binding != dict(document_binding):
            raise ValueError("review query ID document binding differs")
        selected = _canonical_object(
            document.contents, "review query IDs document"
        )
        selection = selected.get("query_ids")
        if (
            set(selected) != {"query_ids"}
            or not isinstance(selection, list)
            or tuple(selection) != queries
        ):
            raise ValueError("review query ID document selection differs")
        _verify_artifact_snapshot(root, document, "review query IDs document")
        return kind, queries, None, (schema_snapshot, document)
    if kind == "CLEAN_SOURCE_REPLAY":
        if (
            not path.startswith(replay_prefix)
        ):
            raise ValueError("clean source replay is outside replay namespace")
        execution_id = payload["execution_id"]
        if not isinstance(execution_id, str):
            raise ValueError("clean replay execution ID is not canonical UUIDv4")
        try:
            parsed = uuid.UUID(execution_id)
        except ValueError as exc:
            raise ValueError(
                "clean replay execution ID is not canonical UUIDv4"
            ) from exc
        if parsed.version != 4 or str(parsed) != execution_id:
            raise ValueError("clean replay execution ID is not canonical UUIDv4")
        return kind, queries, execution_id, (schema_snapshot,)
    raise ValueError("unknown source evidence receipt kind")


def _source_receipt_payload(
    root: Path,
    contents: bytes,
    *,
    path: str,
    run_id: str,
    inventory_sha256: str,
    snapshot_sha256s: tuple[str, ...],
    git_head: str,
    run_spec: Mapping[str, object],
    acquisition_contract: Mapping[str, object],
    trusted_git: _GitSnapshot,
    authority: Authority,
    source_inventory: _SourceInventoryPayload,
) -> tuple[
    str,
    tuple[str, ...],
    str | None,
    tuple[_ArtifactSnapshot, ...],
    str,
    str,
]:
    """Resolve one explicit commit marker to its immutable source receipt."""
    marker = _canonical_object(contents, "source receipt commit marker")
    marker_schema = _validate_bound_schema(
        root,
        trusted_git,
        "v5_2_source_receipt_commit.schema.json",
        marker,
    )
    committed_kind = marker["committed_receipt_kind"]
    expected_prefix = (
        _acquisition_prefix(authority, "source_reviews")
        if committed_kind == "SOURCE_REVIEW"
        else _acquisition_prefix(authority, "source_replays")
    )
    if not path.startswith(expected_prefix):
        raise ValueError("source receipt commit marker is outside its namespace")
    if (
        marker["run_id"] != run_id
        or marker["git_head"] != git_head
        or marker["run_spec"] != run_spec
        or marker["acquisition_contract"] != acquisition_contract
    ):
        raise ValueError("source receipt commit marker authority binding differs")
    _require_source_receipt_disposition(
        marker, source_inventory, authority, label="source receipt commit marker"
    )
    receipt_binding = _validate_binding_value(
        marker["source_receipt"], "committed source receipt"
    )
    receipt_path = str(receipt_binding["path"])
    if receipt_path == path or not receipt_path.startswith(expected_prefix):
        raise ValueError("source receipt commit marker aliases its source receipt")
    receipt = _capture_artifact(
        root, root / receipt_path, "committed source receipt"
    )
    if receipt.binding != dict(receipt_binding):
        raise ValueError("committed source receipt binding differs")
    kind, queries, execution_id, dependencies = _uncommitted_source_receipt_payload(
            root,
            receipt.contents,
            path=receipt.path,
            run_id=run_id,
            inventory_sha256=inventory_sha256,
            snapshot_sha256s=snapshot_sha256s,
            git_head=git_head,
            run_spec=run_spec,
            acquisition_contract=acquisition_contract,
            trusted_git=trusted_git,
            authority=authority,
            source_inventory=source_inventory,
        )
    if kind != committed_kind:
        raise ValueError("source receipt commit marker kind differs")
    _verify_artifact_snapshot(root, receipt, "committed source receipt")
    return (
        kind,
        queries,
        execution_id,
        (marker_schema, receipt, *dependencies),
        receipt.path,
        receipt.sha256,
    )


def _validate_sources_ready_evidence(
    inventory: _ArtifactSnapshot,
    validation_values: Sequence[_ArtifactSnapshot],
    *,
    run_id: str,
    root: Path,
    base: Mapping[str, object],
    trusted_git: _GitSnapshot,
    authority: Authority,
) -> tuple[_ArtifactSnapshot, ...]:
    if inventory.path != _source_inventory_path(authority):
        raise ValueError("SOURCES_READY requires the canonical source inventory")
    inventory_payload = _canonical_object(inventory.contents, "source inventory")
    inventory_schema: _ArtifactSnapshot | None = None
    if isinstance(authority, SourceRunSpec):
        inventory_schema = _validate_bound_schema(
            root,
            trusted_git,
            "v5_2_source_inventory.schema.json",
            inventory_payload,
        )
    source_inventory = _source_inventory_payload(inventory.contents, run_id=run_id)
    snapshots = source_inventory.snapshot_sha256s
    inventory_query_ids = source_inventory.logical_query_ids
    reviews = 0
    replay_paths: set[str] = set()
    replay_hashes: set[str] = set()
    replay_execution_ids: set[str] = set()
    marker_paths: set[str] = set()
    marker_hashes: set[str] = set()
    receipt_paths: set[str] = set()
    receipt_hashes: set[str] = set()
    dependent_snapshots: list[_ArtifactSnapshot] = (
        [inventory_schema] if inventory_schema is not None else []
    )
    if _is_recovery_source_run(authority):
        recovery_bindings, _ = _recovery_disposition_catalog_bindings(
            _paths(root, authority),
            authority,
            _validate_binding_value(base["run_spec"], "run spec"),
            _validate_binding_value(base["acquisition_contract"], "acquisition contract"),
        )
        expected_catalog = {
            "entries": [
                {
                    key: value
                    for key, value in binding.items()
                    if key != "expected_derived_sidecar_sha256"
                }
                for binding in recovery_bindings
            ],
            "query_ids": sorted(str(binding["query_id"]) for binding in recovery_bindings),
        }
        if source_inventory.schedule_conflict_dispositions != expected_catalog:
            raise ValueError("recovery source inventory disposition catalog differs")
        for binding in recovery_bindings:
            for key in ("query_sidecar", "success_receipt"):
                item = _validate_binding_value(
                    binding[key], "recovery source inventory disposition binding"
                )
                dependent_snapshots.append(
                    _capture_artifact(
                        root,
                        root / str(item["path"]),
                        "recovery source inventory disposition artifact",
                    )
                )
    if source_inventory.duplicate_gamepk_disposition is not None:
        dependent_snapshots.extend(
            _capture_verified_source_disposition(
                root,
                source_inventory.duplicate_gamepk_disposition,
            )
        )
    reviewed_query_ids: tuple[str, ...] | None = None
    seen_replay = False
    run_spec = _validate_binding_value(base["run_spec"], "run spec")
    acquisition_contract = _validate_binding_value(
        base["acquisition_contract"], "acquisition contract"
    )
    git_head = base.get("git_head")
    if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
        raise ValueError("source evidence base Git binding differs")
    for artifact in validation_values:
        (
            kind,
            receipt_queries,
            execution_id,
            dependencies,
            receipt_path,
            receipt_sha256,
        ) = _source_receipt_payload(
            root,
            artifact.contents,
            path=artifact.path,
            run_id=run_id,
            inventory_sha256=inventory.sha256,
            snapshot_sha256s=snapshots,
            git_head=git_head,
            run_spec=run_spec,
            acquisition_contract=acquisition_contract,
            trusted_git=trusted_git,
            authority=authority,
            source_inventory=source_inventory,
        )
        if artifact.path in marker_paths or artifact.sha256 in marker_hashes:
            raise ValueError("source receipt commit marker is duplicated")
        marker_paths.add(artifact.path)
        marker_hashes.add(artifact.sha256)
        if receipt_path in receipt_paths or receipt_sha256 in receipt_hashes:
            raise ValueError("committed source receipt is duplicated")
        receipt_paths.add(receipt_path)
        receipt_hashes.add(receipt_sha256)
        for dependency in dependencies:
            if dependency not in dependent_snapshots:
                dependent_snapshots.append(dependency)
        if kind == "SOURCE_REVIEW":
            if seen_replay:
                raise ValueError("source review markers must precede clean replays")
            if not set(receipt_queries).issubset(inventory_query_ids):
                raise ValueError(
                    "source review query IDs are outside the inventory"
                )
            if reviewed_query_ids is None:
                reviewed_query_ids = receipt_queries
            elif receipt_queries != reviewed_query_ids:
                raise ValueError("source review query ID subsets differ")
            reviews += 1
        else:
            seen_replay = True
            if receipt_queries != inventory_query_ids:
                raise ValueError(
                    "clean source replay query IDs differ from inventory"
                )
            if (
                artifact.path in replay_paths
                or artifact.sha256 in replay_hashes
            ):
                raise ValueError("clean source replay receipt is duplicated")
            replay_paths.add(artifact.path)
            replay_hashes.add(artifact.sha256)
            if execution_id is None or execution_id in replay_execution_ids:
                raise ValueError("clean source replay execution ID is duplicated")
            replay_execution_ids.add(execution_id)
    if (
        reviews < 1
        or len(marker_paths) != reviews + 2
        or len(marker_hashes) != reviews + 2
        or len(receipt_paths) != reviews + 2
        or len(receipt_hashes) != reviews + 2
        or len(replay_paths) != 2
        or len(replay_hashes) != 2
        or len(replay_execution_ids) != 2
    ):
        raise ValueError(
            "SOURCES_READY requires an approved review and two clean replays"
        )
    return tuple(dependent_snapshots)


def _binding(root: Path, path: Path, label: str) -> dict[str, str]:
    return _capture_artifact(root, path, label).binding


def _git(root: Path, *args: str) -> str:
    return _git_bytes(root, *args).decode().strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, env=environment
    )
    if completed.returncode:
        raise ValueError(completed.stderr.decode(errors="replace").strip() or "git evidence unavailable")
    return completed.stdout


def _require_tracked_clean(root: Path, relative: str, label: str) -> None:
    _capture_git_file(root, relative, label)


def _require_bound_execution_code_clean(root: Path) -> None:
    for relative in _BOUND_EXECUTION_CODE:
        _require_tracked_clean(root, relative, "bound execution code")


def _supplemental_authority_binding(
    root: Path, evidence: Mapping[str, object]
) -> Mapping[str, object]:
    contract_binding = _validate_binding_value(
        evidence["acquisition_contract"], "acquisition contract"
    )
    _, contents = _read_path(
        root,
        root / str(contract_binding["path"]),
        "acquisition contract",
    )
    contract = _canonical_object(contents, "acquisition contract")
    if contract.get("schema_version") != 2:
        raise ValueError("acquisition contract must use schema version 2")
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("acquisition contract bindings differ")
    if contract.get("run_id") == "historical_walkforward_500":
        amendment = bindings.get("plan_a_amendment")
        if not isinstance(amendment, Mapping):
            raise ValueError("Plan A amendment binding is missing")
        return _artifact_binding(root, amendment, "Plan A amendment")
    if contract.get("run_id") == "historical_walkforward_500_superseding_565534":
        addendum = bindings.get("ephemeral_parse_schedule_equivalence_addendum")
        if not isinstance(addendum, Mapping):
            raise ValueError("ephemeral parse schedule equivalence addendum binding is missing")
        return _artifact_binding(
            root,
            addendum,
            "ephemeral parse schedule equivalence addendum",
            plan=True,
        )
    raise ValueError("acquisition contract authority differs")


def _git_bound_paths(root: Path, evidence: Mapping[str, object]) -> tuple[str, ...]:
    paths = list(_BOUND_EXECUTION_CODE)
    for key in (
        "run_spec",
        "approved_design",
        "implementation_plan",
        "acquisition_contract",
    ):
        binding = _validate_binding_value(
            evidence[key], key, plan=key == "implementation_plan"
        )
        paths.append(str(binding["path"]))
    lineage = _exact_object(
        evidence["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage"
    )
    for key in ("baseline_inventory", "protected_inventory"):
        paths.append(
            str(_validate_binding_value(lineage[key], key)["path"])
        )
    supplemental = _supplemental_authority_binding(root, evidence)
    paths.append(str(supplemental["path"]))
    return tuple(dict.fromkeys(paths))


def _capture_git_snapshot(
    root: Path, evidence: Mapping[str, object]
) -> _GitSnapshot:
    head = _git(root, "rev-parse", "HEAD")
    if not _GIT_HEAD.fullmatch(head):
        raise ValueError("Git HEAD identity differs")
    expected_bindings: dict[str, Mapping[str, object]] = {}
    for key in (
        "run_spec",
        "approved_design",
        "implementation_plan",
        "acquisition_contract",
    ):
        binding = _validate_binding_value(
            evidence[key], key, plan=key == "implementation_plan"
        )
        expected_bindings[str(binding["path"])] = binding
    supplemental = _supplemental_authority_binding(root, evidence)
    expected_bindings[str(supplemental["path"])] = supplemental
    lineage = _exact_object(
        evidence["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage"
    )
    for key in ("baseline_inventory", "protected_inventory"):
        binding = _validate_binding_value(lineage[key], key)
        expected_bindings[str(binding["path"])] = binding
    files: list[_GitFileSnapshot] = []
    for relative in _git_bound_paths(root, evidence):
        captured = _capture_git_file(root, relative, "bound Git evidence")
        expected = expected_bindings.get(relative)
        if expected is not None and captured.artifact.binding != {
            "path": expected["path"],
            "sha256": expected["sha256"],
        }:
            raise ValueError("bound Git evidence differs from receipt binding")
        if (
            expected is not None
            and "git_blob_id" in expected
            and captured.blob_id != expected["git_blob_id"]
        ):
            raise ValueError("bound Git blob binding differs")
        files.append(captured)
    if _git(root, "rev-parse", "HEAD") != head:
        raise ValueError("Git HEAD changed during trusted snapshot")
    return _GitSnapshot(head=head, files=tuple(files))


def _verify_git_snapshot(root: Path, snapshot: _GitSnapshot) -> None:
    if _git(root, "rev-parse", "HEAD") != snapshot.head:
        raise ValueError("Git HEAD changed after trusted snapshot")
    for expected in snapshot.files:
        current = _capture_git_file(
            root, expected.artifact.path, "bound Git evidence"
        )
        if current != expected:
            raise ValueError("bound Git file identity changed after trusted snapshot")
    if _git(root, "rev-parse", "HEAD") != snapshot.head:
        raise ValueError("Git HEAD changed after trusted snapshot")


def _parse_git_tree_entry(value: str, label: str) -> tuple[str, str]:
    try:
        metadata, _path = value.split("\t", 1)
        mode, kind, blob = metadata.split(" ")
    except ValueError as exc:
        raise ValueError(f"{label} Git tree entry differs") from exc
    if kind != "blob" or mode not in {"100644", "100755"} or not _GIT_HEAD.fullmatch(blob):
        raise ValueError(f"{label} Git tree entry differs")
    return mode, blob


def _parse_git_index_entry(value: str, label: str) -> tuple[str, str]:
    lines = value.splitlines()
    if len(lines) != 1:
        raise ValueError(f"{label} Git index entry differs")
    try:
        metadata, _path = lines[0].split("\t", 1)
        mode, blob, stage = metadata.split(" ")
    except ValueError as exc:
        raise ValueError(f"{label} Git index entry differs") from exc
    if stage != "0" or mode not in {"100644", "100755"} or not _GIT_HEAD.fullmatch(blob):
        raise ValueError(f"{label} Git index entry differs")
    return mode, blob


def _capture_git_file(root: Path, relative: str, label: str) -> _GitFileSnapshot:
    _validate_artifact_path(relative, label)
    head_mode, head_blob = _parse_git_tree_entry(
        _git(root, "ls-tree", "HEAD", "--", relative), label
    )
    index_mode, index_blob = _parse_git_index_entry(
        _git(root, "ls-files", "--stage", "--", relative), label
    )
    artifact = _capture_artifact(root, root / relative, label)
    worktree_mode = "100755" if artifact.identity[2] & 0o111 else "100644"
    head_bytes = _git_bytes(root, "cat-file", "blob", head_blob)
    index_bytes = _git_bytes(root, "cat-file", "blob", index_blob)
    if (
        head_blob != index_blob
        or head_mode != index_mode
        or head_mode != worktree_mode
        or head_bytes != index_bytes
        or head_bytes != artifact.contents
    ):
        raise ValueError(f"{label} HEAD, index, or worktree differs")
    return _GitFileSnapshot(artifact=artifact, blob_id=head_blob, mode=head_mode)


def _before_registry_publish() -> None:
    """Deterministic race-test hook; production intentionally does nothing."""


def _after_registry_publish() -> None:
    """Deterministic late-race test hook; production intentionally does nothing."""


def _after_registry_receipt_read() -> None:
    """Deterministic final-rebind race hook; production intentionally does nothing."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"{label} timestamp differs")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} timestamp differs") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} timestamp differs")


def _cleanup_created_receipt(
    parent_fd: int, descriptor: int, name: str, label: str
) -> None:
    try:
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
    except OSError as exc:
        raise ValueError(f"{label} could not be safely cleaned up") from exc


def _verify_visible_receipt_binding(
    execution_descriptor: int,
    relative: Path,
    parent_fd: int,
    parent_identity: tuple[int, ...],
    descriptor: int,
    label: str,
) -> tuple[int, ...]:
    rebound_parent, descriptors = _open_directory_at(
        execution_descriptor, relative.parent
    )
    try:
        if (
            _identity(os.fstat(parent_fd))[:2] != parent_identity[:2]
            or _identity(os.fstat(rebound_parent))[:2] != parent_identity[:2]
        ):
            raise ValueError(f"{label} parent identity changed after publication")
    finally:
        _close_directories(descriptors)
    opened = os.fstat(descriptor)
    linked = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or _identity(opened) != _identity(linked)
    ):
        raise ValueError(f"{label} identity changed after publication")
    return _identity(opened)


def _verify_published_receipt(
    execution_descriptor: int,
    relative: Path,
    parent_fd: int,
    parent_identity: tuple[int, ...],
    descriptor: int,
    contents: bytes,
    label: str,
    expected_receipt_names: Sequence[str] | None,
    membership_validator: Callable[[Sequence[str]], None] | None,
) -> None:
    """Use the final no-follow read as the operation's return snapshot.

    Concurrent namespace changes after that read are post-check and out of scope.
    """
    opened_identity = _verify_visible_receipt_binding(
        execution_descriptor,
        relative,
        parent_fd,
        parent_identity,
        descriptor,
        label,
    )
    published, published_identity = _read_file_at(
        parent_fd, relative.name, label
    )
    if published_identity != opened_identity or published != contents:
        raise ValueError(f"{label} bytes changed after publication")
    _after_registry_receipt_read()
    rebound_parent, descriptors = _open_directory_at(
        execution_descriptor, relative.parent
    )
    try:
        if (
            _identity(os.fstat(parent_fd))[:2] != parent_identity[:2]
            or _identity(os.fstat(rebound_parent))[:2] != parent_identity[:2]
        ):
            raise ValueError(f"{label} parent identity changed at publication return")
        membership_identity: tuple[int, ...] | None = None
        if expected_receipt_names is not None:
            membership_identity = _identity(os.fstat(rebound_parent))
            try:
                visible_names = sorted(os.listdir(rebound_parent))
            except OSError as exc:
                raise _ReceiptMembershipError(
                    "execution registry receipt membership is unreadable"
                ) from exc
            if membership_validator is None:
                _validate_receipt_name_chain(visible_names)
            else:
                membership_validator(visible_names)
            if tuple(visible_names) != tuple(expected_receipt_names):
                raise _ReceiptMembershipError(
                    "execution registry receipt membership has a gap, fork, or unknown entry"
                )
        final_contents, _ = _read_file_at(
            rebound_parent, relative.name, label
        )
        if (
            membership_identity is not None
            and _identity(os.fstat(rebound_parent)) != membership_identity
        ):
            raise _ReceiptMembershipError(
                "execution registry receipt membership changed during final snapshot"
            )
    finally:
        _close_directories(descriptors)
    if (
        len(final_contents) != len(contents)
        or _sha256_bytes(final_contents) != _sha256_bytes(contents)
        or final_contents != contents
    ):
        raise ValueError(f"{label} bytes changed at publication return")


def _write_exclusive(
    execution_descriptor: int,
    relative: Path,
    contents: bytes,
    label: str,
    verify_after_publish: Callable[[], None],
    expected_receipt_names: Sequence[str] | None = None,
    membership_validator: Callable[[Sequence[str]], None] | None = None,
) -> None:
    parent_fd, descriptors = _open_directory_at(
        execution_descriptor, relative.parent
    )
    descriptor = -1
    created = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(relative.name, flags, 0o600, dir_fd=parent_fd)
        created = True
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("receipt write made no progress")
            view = view[written:]
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} publication was aliased")
        os.fsync(descriptor)
        linked = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(linked) != _identity(metadata):
            raise ValueError(f"{label} identity changed during publication")
        os.fsync(parent_fd)
        parent_identity = _identity(os.fstat(parent_fd))
        _after_registry_publish()
        verify_after_publish()
        _verify_published_receipt(
            execution_descriptor,
            relative,
            parent_fd,
            parent_identity,
            descriptor,
            contents,
            label,
            expected_receipt_names,
            membership_validator,
        )
        verify_after_publish()
    except FileExistsError:
        raise
    except BaseException as exc:
        if created:
            try:
                _cleanup_created_receipt(
                    parent_fd, descriptor, relative.name, label
                )
            except BaseException as cleanup_exc:
                raise ValueError(
                    f"{label} failed publication cleanup ownership verification"
                ) from cleanup_exc
        if isinstance(exc, OSError):
            raise ValueError(f"{label} could not be published safely") from exc
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directories(descriptors)


def _ensure_registry_directories(paths: _RegistryPaths) -> None:
    for relative in (
        paths.receipts_relative,
        paths.quarantine_relative,
        paths.checkpoints_relative,
    ):
        _, descriptors = _open_directory(paths.root, relative, create=True)
        _close_directories(descriptors)


def _open_lock(paths: _RegistryPaths, *, create: bool) -> _RegistryLockBinding:
    parent_fd, descriptors = _open_directory(
        paths.root, paths.execution_relative, create=False
    )
    descriptor = -1
    try:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        created = False
        if create:
            try:
                descriptor = os.open(paths.lock_relative.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                descriptor = os.open(paths.lock_relative.name, flags, dir_fd=parent_fd)
        else:
            descriptor = os.open(paths.lock_relative.name, flags, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        linked = os.stat(paths.lock_relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != 0
            or _identity(metadata) != _identity(linked)
        ):
            raise ValueError("execution registry lock must be an empty unaliased regular file")
        if created:
            os.fsync(descriptor)
            os.fsync(parent_fd)
        return _RegistryLockBinding(descriptor, tuple(descriptors))
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directories(descriptors)
        raise ValueError("execution registry lock is unsafe") from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directories(descriptors)
        raise


def _verify_namespace_binding(
    paths: _RegistryPaths, binding: _RegistryLockBinding
) -> None:
    descriptors = binding.directory_descriptors
    if len(descriptors) != len(paths.execution_relative.parts) + 1:
        raise ValueError("execution registry descriptor chain differs")
    try:
        root_opened = os.fstat(descriptors[0])
        root_linked = os.stat(paths.root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_opened.st_mode)
            or not stat.S_ISDIR(root_linked.st_mode)
            or (root_opened.st_dev, root_opened.st_ino)
            != (root_linked.st_dev, root_linked.st_ino)
        ):
            raise ValueError("execution registry root identity was replaced")
        for index, component in enumerate(paths.execution_relative.parts):
            opened = os.fstat(descriptors[index + 1])
            linked = os.stat(
                component,
                dir_fd=descriptors[index],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (linked.st_dev, linked.st_ino)
            ):
                raise ValueError(
                    "execution registry namespace identity was replaced"
                )
    except OSError as exc:
        raise ValueError(
            "execution registry namespace identity was replaced"
        ) from exc


def _verify_lock_binding(
    paths: _RegistryPaths, binding: _RegistryLockBinding
) -> None:
    _verify_namespace_binding(paths, binding)
    try:
        opened = os.fstat(binding.lock_descriptor)
        linked = os.stat(
            paths.lock_relative.name,
            dir_fd=binding.execution_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != 0
            or _identity(opened) != _identity(linked)
        ):
            raise ValueError("execution registry lock identity was replaced")
    except OSError as exc:
        raise ValueError("execution registry lock identity was replaced") from exc


@contextmanager
def _registry_lock(
    paths: _RegistryPaths, *, create: bool, exclusive: bool
) -> Iterator[_RegistryLockBinding]:
    binding = _open_lock(paths, create=create)
    acquired = False
    try:
        fcntl.flock(
            binding.lock_descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        acquired = True
        _verify_lock_binding(paths, binding)
        yield binding
    finally:
        try:
            if acquired:
                fcntl.flock(binding.lock_descriptor, fcntl.LOCK_UN)
        finally:
            try:
                os.close(binding.lock_descriptor)
            finally:
                _close_directories(binding.directory_descriptors)


def _artifact_binding(root: Path, raw: object, label: str, *, plan: bool = False) -> Mapping[str, object]:
    binding = _validate_binding_value(raw, label, plan=plan)
    path = str(binding["path"])
    digest = str(binding["sha256"])
    actual = _binding(root, root / path, label)
    if actual != {"path": path, "sha256": digest}:
        raise ValueError(f"{label} hash binding differs")
    _require_tracked_clean(root, path, label)
    if plan:
        blob = binding["git_blob_id"]
        if not isinstance(blob, str) or not _GIT_HEAD.fullmatch(blob) or _git(root, "rev-parse", f"HEAD:{path}") != blob:
            raise ValueError(f"{label} Git blob binding differs")
    return binding


def _validate_common_bindings(
    root: Path, payload: Mapping[str, object], run_spec: Authority
) -> None:
    from mlb_backtest.v52_query_plan import _load_static_contracts

    run_binding = _artifact_binding(root, payload["run_spec"], "run spec")
    _, run_bytes = _read_path(root, root / str(run_binding["path"]), "run spec")
    if canonical_json_bytes(run_spec.raw) + b"\n" != run_bytes:
        raise ValueError("caller RunSpec differs from receipt binding")
    _artifact_binding(root, payload["approved_design"], "approved design")
    _artifact_binding(root, payload["implementation_plan"], "implementation plan", plan=True)
    contract_binding = _artifact_binding(root, payload["acquisition_contract"], "acquisition contract")
    context = _load_static_contracts(
        root / str(run_binding["path"]),
        root / str(contract_binding["path"]),
    )
    if context.authority.raw != run_spec.raw:
        raise ValueError("caller RunSpec differs from authenticated authority")
    contract = _canonical_object(
        _read_path(root, root / str(contract_binding["path"]), "acquisition contract")[1],
        "acquisition contract",
    )
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("acquisition contract bindings differ")
    expected_plan = dict(payload["implementation_plan"])
    expected_plan["working_tree_sha256"] = expected_plan.pop("sha256")
    if (
        bindings.get("approved_design") != payload["approved_design"]
        or bindings.get("run_spec") != payload["run_spec"]
        or bindings.get("implementation_plan") != expected_plan
    ):
        raise ValueError("execution receipt differs from acquisition contract bindings")
    _supplemental_authority_binding(root, payload)
    lineage = _exact_object(payload["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage")
    protection = contract.get("legacy_protection")
    if not isinstance(protection, Mapping):
        raise ValueError("acquisition contract legacy protection differs")
    for key, prefix in (("baseline_inventory", "baseline_inventory"), ("protected_inventory", "protected_inventory")):
        value = _artifact_binding(root, lineage[key], f"Task0 {key}")
        if value != {"path": protection.get(f"{prefix}_path"), "sha256": protection.get(f"{prefix}_sha256")}:
            raise ValueError("Task0 lineage differs from acquisition contract")


def _validation_bindings(root: Path, paths: Sequence[Path]) -> list[dict[str, str]]:
    bindings = [_binding(root, path, "validation receipt") for path in paths]
    if len({item["path"] for item in bindings}) != len(bindings):
        raise ValueError("validation receipt paths must be unique")
    return bindings


def _artifact_snapshots(
    root: Path,
    paths: Sequence[Path],
    label: str,
    *,
    allow_duplicate_paths: bool = False,
) -> tuple[_ArtifactSnapshot, ...]:
    snapshots = tuple(_capture_artifact(root, path, label) for path in paths)
    if (
        not allow_duplicate_paths
        and len({snapshot.path for snapshot in snapshots}) != len(snapshots)
    ):
        raise ValueError(f"{label} paths must be unique")
    return snapshots


def _verify_artifact_snapshots(
    root: Path, snapshots: Sequence[_ArtifactSnapshot], label: str
) -> None:
    for snapshot in snapshots:
        _verify_artifact_snapshot(root, snapshot, label)


def _validate_phase_evidence(
    phase: str,
    base: Mapping[str, object],
    inventory: _ArtifactSnapshot,
    validations: Sequence[_ArtifactSnapshot],
    *,
    root: Path,
    run_id: str,
    trusted_git: _GitSnapshot,
    authority: Authority,
) -> tuple[_ArtifactSnapshot, ...]:
    if phase in {"DESIGN_ACCEPTED", "ACQUIRING"}:
        lineage = _exact_object(
            base["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage"
        )
        protected = _validate_binding_value(
            lineage["protected_inventory"], "protected inventory"
        )
        if inventory.binding != dict(protected) or validations:
            raise ValueError(
                f"{phase} must bind the Task0 protected inventory directly"
            )
        return ()
    if phase == "SOURCES_READY":
        return _validate_sources_ready_evidence(
            inventory,
            validations,
            run_id=run_id,
            root=root,
            base=base,
            trusted_git=trusted_git,
            authority=authority,
        )
    raise ValueError(f"execution phase {phase} has no implemented evidence validator")


def _validate_stored_artifacts(
    root: Path,
    values: object,
    label: str,
    *,
    allow_duplicate_paths: bool = False,
) -> None:
    if not isinstance(values, list):
        raise ValueError(f"{label} must be a list")
    seen: set[str] = set()
    for value in values:
        binding = _validate_binding_value(value, label)
        path = binding.get("path")
        digest = binding.get("sha256")
        if (
            not isinstance(path, str)
            or (path in seen and not allow_duplicate_paths)
            or not isinstance(digest, str)
        ):
            raise ValueError(f"{label} binding differs")
        seen.add(path)
        if _binding(root, root / path, label) != dict(binding):
            raise ValueError(f"{label} hash binding differs")


def _snapshots_from_bindings(
    root: Path,
    values: object,
    label: str,
    *,
    allow_duplicate_paths: bool = False,
) -> tuple[_ArtifactSnapshot, ...]:
    if not isinstance(values, list):
        raise ValueError(f"{label} must be a list")
    snapshots: list[_ArtifactSnapshot] = []
    seen: set[str] = set()
    for value in values:
        binding = _validate_binding_value(value, label)
        path = str(binding["path"])
        if path in seen and not allow_duplicate_paths:
            raise ValueError(f"{label} binding differs")
        seen.add(path)
        snapshot = _capture_artifact(root, root / path, label)
        if snapshot.binding != dict(binding):
            raise ValueError(f"{label} hash binding differs")
        snapshots.append(snapshot)
    return tuple(snapshots)


def _validate_receipt_name_chain(
    names: Sequence[str], phases: Sequence[str] = PHASES
) -> None:
    if not names:
        raise FileNotFoundError("execution registry has no phase receipts")
    for expected_sequence, name in enumerate(names):
        match = _SUCCESS_NAME.fullmatch(name)
        if match is None:
            raise _ReceiptMembershipError(
                "execution registry contains an ambiguous or partial receipt"
            )
        sequence = int(match.group("sequence"))
        phase = match.group("phase")
        if (
            sequence != expected_sequence
            or sequence >= len(phases)
            or phase != phases[sequence]
        ):
            raise _ReceiptMembershipError(
                "execution registry chain has a gap, fork, replay, or rollback anchor"
            )


def _receipt_payload(
    contents: bytes,
    expected_sha: str,
    filename_phase: str,
    filename_sequence: int,
    authority: Authority,
) -> Mapping[str, object]:
    if _sha256_bytes(contents) != expected_sha:
        raise ValueError("execution receipt filename hash differs")
    payload = _canonical_object(contents, "execution receipt")
    _validate_success_receipt(payload, authority)
    if (
        payload.get("schema_version") != 1
        or payload.get("receipt_kind") != "PHASE_TRANSITION"
        or payload.get("phase") != filename_phase
        or type(payload.get("sequence")) is not int
        or payload.get("sequence") != filename_sequence
    ):
        raise ValueError("execution receipt identity differs")
    _validate_timestamp(payload.get("recorded_at_utc"), "execution receipt")
    return payload


def _validate_success_receipt(
    payload: object, authority: Authority | None = None
) -> Mapping[str, object]:
    receipt = _exact_object(payload, _SUCCESS_KEYS, "execution receipt")
    phases = _authority_phases(authority) if authority is not None else PHASES
    run_id = authority.run_id if authority is not None else "historical_walkforward_500"
    sequence = receipt.get("sequence")
    phase = receipt.get("phase")
    prior = receipt.get("prior_receipt_sha256")
    git_head = receipt.get("git_head")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "PHASE_TRANSITION"
        or receipt.get("run_id") != run_id
        or type(sequence) is not int
        or not 0 <= sequence < len(phases)
        or phase != phases[sequence]
        or (sequence == 0 and prior is not None)
        or (
            sequence > 0
            and (not isinstance(prior, str) or not _SHA256.fullmatch(prior))
        )
        or not isinstance(git_head, str)
        or not _GIT_HEAD.fullmatch(git_head)
    ):
        raise ValueError("execution receipt violates the receipt schema")
    _validate_timestamp(receipt.get("recorded_at_utc"), "execution receipt")
    _validate_binding_value(receipt.get("run_spec"), "run spec")
    _validate_binding_value(receipt.get("approved_design"), "approved design")
    _validate_binding_value(
        receipt.get("implementation_plan"), "implementation plan", plan=True
    )
    _validate_binding_value(
        receipt.get("acquisition_contract"), "acquisition contract"
    )
    lineage = _exact_object(
        receipt.get("task0_lineage"), _LINEAGE_KEYS, "Task0 lineage"
    )
    _validate_binding_value(lineage.get("baseline_inventory"), "baseline inventory")
    _validate_binding_value(
        lineage.get("protected_inventory"), "protected inventory"
    )
    _validate_binding_value(receipt.get("inventory"), "phase inventory")
    checkpoint_head = receipt.get("acquisition_checkpoint_head")
    if sequence < 2:
        if checkpoint_head is not None:
            raise ValueError("early execution receipt checkpoint head must be null")
    else:
        head = _exact_object(
            checkpoint_head,
            _CHECKPOINT_HEAD_KEYS,
            "acquisition checkpoint head",
        )
        head_sequence = head.get("sequence")
        head_sha = head.get("sha256")
        execution = (
            _authority_namespace(authority, "audit") / "execution"
            if authority is not None
            else Path("audit/v5_2_qd/historical/execution")
        )
        expected_path = (
            (
            (execution / "acquisition_checkpoints").as_posix() + "/"
            f"{head_sequence:08d}-{head_sha}.json"
            )
            if type(head_sequence) is int
            else ""
        )
        if (
            type(head_sequence) is not int
            or head_sequence < 0
            or head.get("count") != head_sequence + 1
            or not isinstance(head_sha, str)
            or not _SHA256.fullmatch(head_sha)
            or head.get("path") != expected_path
            or head.get("stage") != "REMAINING"
        ):
            raise ValueError("acquisition checkpoint head binding differs")
    validations = receipt.get("validation_receipts")
    if not isinstance(validations, list):
        raise ValueError("validation receipts violate the receipt schema")
    paths = [
        str(_validate_binding_value(value, "validation receipt")["path"])
        for value in validations
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("validation receipts violate the receipt schema")
    return receipt


def _validate_quarantine_receipt(
    payload: object, authority: Authority | None = None
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("quarantine receipt keys differ")
    keys = frozenset(payload)
    if keys not in {_QUARANTINE_KEYS, _AUTOMATIC_QUARANTINE_KEYS}:
        raise ValueError("quarantine receipt keys differ")
    receipt = payload
    automatic = keys == _AUTOMATIC_QUARANTINE_KEYS
    phases = _authority_phases(authority) if authority is not None else PHASES
    run_id = authority.run_id if authority is not None else "historical_walkforward_500"
    git_head = receipt.get("git_head")
    current_hash = receipt.get("current_receipt_sha256")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "QUARANTINED_TRANSITION"
        or receipt.get("run_id") != run_id
        or receipt.get("current_phase") not in phases
        or receipt.get("attempted_phase") not in phases
        or (
            automatic
            and (
                receipt.get("error_classification")
                not in _QUARANTINE_ERROR_CLASSIFICATIONS
                or _AUTOMATIC_QUARANTINE_CLASSIFICATION_BY_PHASE.get(
                    str(receipt.get("attempted_phase"))
                )
                != receipt.get("error_classification")
            )
        )
        or not isinstance(current_hash, str)
        or not _SHA256.fullmatch(current_hash)
        or not isinstance(git_head, str)
        or not _GIT_HEAD.fullmatch(git_head)
    ):
        raise ValueError("quarantine receipt violates the receipt schema")
    _validate_timestamp(receipt.get("recorded_at_utc"), "quarantine receipt")
    _validate_binding_value(receipt.get("run_spec"), "run spec")
    if automatic:
        _validate_binding_value(receipt.get("inventory"), "phase inventory")
    validations = receipt.get("validation_receipts")
    if not isinstance(validations, list) or (not automatic and not validations):
        raise ValueError("quarantine receipt requires valid validation evidence")
    paths = [
        str(_validate_binding_value(value, "validation receipt")["path"])
        for value in validations
    ]
    if not automatic and len(paths) != len(set(paths)):
        raise ValueError("validation receipts violate the receipt schema")
    return receipt


def _validate_quarantine_locked(
    paths: _RegistryPaths,
    run_spec: Authority,
    payloads: Sequence[Mapping[str, object]],
    phase_receipt_sha: Mapping[str, str],
    lock_binding: _RegistryLockBinding,
) -> None:
    phases = _authority_phases(run_spec)
    quarantine_relative = _execution_child(paths, paths.quarantine_relative)
    quarantine_fd, descriptors = _open_directory_at(
        lock_binding.execution_descriptor, quarantine_relative
    )
    try:
        names = sorted(os.listdir(quarantine_fd))
    finally:
        _close_directories(descriptors)
    for name in names:
        match = _SUCCESS_NAME.fullmatch(name)
        if match is None:
            raise ValueError("execution quarantine contains an ambiguous or partial receipt")
        contents = _read_relative_at(
            lock_binding.execution_descriptor,
            quarantine_relative / name,
            "quarantine receipt",
        )
        if _sha256_bytes(contents) != match.group("sha"):
            raise ValueError("quarantine receipt filename hash differs")
        payload = _canonical_object(contents, "quarantine receipt")
        _validate_quarantine_receipt(payload, run_spec)
        current_phase = payload.get("current_phase")
        attempted = payload.get("attempted_phase")
        if (
            payload.get("schema_version") != 1
            or payload.get("receipt_kind") != "QUARANTINED_TRANSITION"
            or payload.get("run_id") != run_spec.run_id
            or current_phase not in phases
            or attempted not in phases
            or phases.index(str(attempted)) != phases.index(str(current_phase)) + 1
            or match.group("phase") != attempted
            or int(match.group("sequence")) != phases.index(str(attempted))
            or payload.get("current_receipt_sha256")
            != phase_receipt_sha.get(str(current_phase))
        ):
            raise ValueError("quarantine receipt transition binding differs")
        git_head = payload.get("git_head")
        if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
            raise ValueError("quarantine receipt Git head differs")
        _validate_timestamp(payload.get("recorded_at_utc"), "quarantine receipt")
        if payload.get("run_spec") != payloads[0]["run_spec"]:
            raise ValueError("quarantine receipt RunSpec binding differs")
        if "inventory" in payload:
            inventory = _validate_binding_value(
                payload.get("inventory"), "quarantine inventory"
            )
            if _binding(
                paths.root,
                paths.root / str(inventory["path"]),
                "quarantine inventory",
            ) != dict(inventory):
                raise ValueError("quarantine inventory hash binding differs")
        validation_values = payload.get("validation_receipts")
        if not isinstance(validation_values, list):
            raise ValueError("quarantine receipt validation evidence differs")
        _validate_stored_artifacts(
            paths.root,
            validation_values,
            "validation receipts",
            allow_duplicate_paths="inventory" in payload,
        )


def _load_locked(
    paths: _RegistryPaths,
    run_spec: Authority,
    lock_binding: _RegistryLockBinding,
    *,
    read_only_progress: bool = False,
) -> tuple[ExecutionRegistryState, list[Mapping[str, object]]]:
    _verify_lock_binding(paths, lock_binding)
    _require_bound_execution_code_clean(paths.root)
    receipts_relative = _execution_child(paths, paths.receipts_relative)
    receipt_fd, descriptors = _open_directory_at(
        lock_binding.execution_descriptor, receipts_relative
    )
    try:
        names = sorted(os.listdir(receipt_fd))
    finally:
        _close_directories(descriptors)
    phases = _authority_phases(run_spec)
    _validate_receipt_name_chain(names, phases)
    payloads: list[Mapping[str, object]] = []
    phase_receipt_sha: dict[str, str] = {}
    prior_sha: str | None = None
    stable_bindings: dict[str, object] | None = None
    trusted_git: _GitSnapshot | None = None
    for expected_sequence, name in enumerate(names):
        match = _SUCCESS_NAME.fullmatch(name)
        if match is None:
            raise AssertionError("validated receipt name no longer matches")
        sequence = int(match.group("sequence"))
        phase = match.group("phase")
        receipt_sha = match.group("sha")
        contents = _read_relative_at(
            lock_binding.execution_descriptor,
            receipts_relative / name,
            "execution receipt",
        )
        payload = _receipt_payload(
            contents, receipt_sha, phase, sequence, run_spec
        )
        if payload.get("run_id") != run_spec.run_id or payload.get("prior_receipt_sha256") != prior_sha:
            raise ValueError("execution registry prior hash or run identity differs")
        git_head = payload.get("git_head")
        if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
            raise ValueError("execution receipt Git head differs")
        _validate_common_bindings(paths.root, payload, run_spec)
        current_bindings = {
            key: payload[key]
            for key in (
                "run_spec",
                "approved_design",
                "implementation_plan",
                "acquisition_contract",
                "task0_lineage",
            )
        }
        if stable_bindings is None:
            stable_bindings = current_bindings
            trusted_git = _capture_git_snapshot(paths.root, payload)
        elif current_bindings != stable_bindings:
            raise ValueError("execution registry binding fork differs")
        if trusted_git is None or git_head != trusted_git.head:
            raise ValueError("execution receipt Git HEAD differs from trusted snapshot")
        inventory = _exact_object(payload.get("inventory"), _BINDING_KEYS, "phase inventory")
        inventory_snapshot = _capture_artifact(
            paths.root, paths.root / str(inventory["path"]), "phase inventory"
        )
        if inventory_snapshot.binding != dict(inventory):
            raise ValueError("phase inventory hash binding differs")
        validation_snapshots = _snapshots_from_bindings(
            paths.root, payload.get("validation_receipts"), "validation receipts"
        )
        _validate_phase_evidence(
            phase,
            payloads[0] if payloads else payload,
            inventory_snapshot,
            validation_snapshots,
            root=paths.root,
            run_id=run_spec.run_id,
            trusted_git=trusted_git,
            authority=run_spec,
        )
        payloads.append(payload)
        phase_receipt_sha[phase] = receipt_sha
        prior_sha = receipt_sha
    last = payloads[-1]
    _validate_quarantine_locked(
        paths,
        run_spec,
        payloads,
        phase_receipt_sha,
        lock_binding,
    )
    inventory = _exact_object(last["inventory"], _BINDING_KEYS, "phase inventory")
    if trusted_git is None:
        raise ValueError("execution registry trusted Git snapshot is missing")
    _verify_git_snapshot(paths.root, trusted_git)
    checkpoint_head = last["acquisition_checkpoint_head"]
    if checkpoint_head is not None:
        checkpoints, checkpoint_payloads, checkpoint_names = _load_checkpoints_locked(
                paths,
                run_spec,
                payloads,
                trusted_git,
                lock_binding,
                verify_latest_progress=True,
                read_only_progress=(
                    read_only_progress
                    or _checkpoint_progress_is_read_only(
                        run_spec,
                        last["phase"],
                        checkpoint_head,
                    )
                ),
            )
        authenticated_head = _terminal_checkpoint_head(
            paths,
            checkpoints,
            checkpoint_payloads,
            checkpoint_names,
        )
        if checkpoint_head != authenticated_head:
            raise ValueError("execution receipt checkpoint head binding differs")
    _verify_lock_binding(paths, lock_binding)
    return (
        ExecutionRegistryState(
            phase=str(last["phase"]),
            sequence=int(last["sequence"]),
            receipt_sha256=str(prior_sha),
            inventory_sha256=str(inventory["sha256"]),
        ),
        payloads,
    )


def _initial_evidence(
    root: Path,
    run_spec: Authority,
    design_path: Path,
    plan_path: Path,
    acquisition_contract_path: Path,
) -> dict[str, object]:
    from mlb_backtest.v52_query_plan import _load_static_contracts

    _require_bound_execution_code_clean(root)
    contract_binding = _binding(root, acquisition_contract_path, "acquisition contract")
    _require_tracked_clean(root, contract_binding["path"], "acquisition contract")
    contract = _canonical_object(_read_path(root, acquisition_contract_path, "acquisition contract")[1], "acquisition contract")
    bindings = contract.get("bindings")
    protection = contract.get("legacy_protection")
    if not isinstance(bindings, Mapping) or not isinstance(protection, Mapping):
        raise ValueError("acquisition contract evidence is incomplete")
    run_binding_raw = bindings.get("run_spec")
    if not isinstance(run_binding_raw, Mapping) or not isinstance(run_binding_raw.get("path"), str):
        raise ValueError("acquisition contract RunSpec binding differs")
    context = _load_static_contracts(
        root / str(run_binding_raw["path"]), acquisition_contract_path
    )
    if context.authority.raw != run_spec.raw:
        raise ValueError("caller RunSpec differs from acquisition contract")
    design_binding_raw = bindings.get("approved_design")
    plan_contract = bindings.get("implementation_plan")
    if not isinstance(design_binding_raw, Mapping) or not isinstance(plan_contract, Mapping):
        raise ValueError("design or implementation plan binding differs")
    if _relative_path(root, design_path, "approved design").as_posix() != design_binding_raw.get("path"):
        raise ValueError("approved design path differs from authenticated authority")
    if _relative_path(root, plan_path, "implementation plan").as_posix() != plan_contract.get("path"):
        raise ValueError("implementation plan path differs from authenticated authority")
    design = _binding(root, design_path, "approved design")
    plan = _binding(root, plan_path, "implementation plan")
    run_binding = _binding(root, root / str(run_binding_raw["path"]), "run spec")
    if canonical_json_bytes(run_spec.raw) + b"\n" != _read_path(root, root / run_binding["path"], "run spec")[1]:
        raise ValueError("caller RunSpec differs from acquisition contract")
    plan_with_blob = {**plan, "git_blob_id": plan_contract.get("git_blob_id")}
    lineage = {
        "baseline_inventory": _binding(root, root / str(protection.get("baseline_inventory_path")), "Task0 baseline inventory"),
        "protected_inventory": _binding(root, root / str(protection.get("protected_inventory_path")), "Task0 protected inventory"),
    }
    evidence = {
        "run_spec": run_binding,
        "approved_design": design,
        "implementation_plan": plan_with_blob,
        "acquisition_contract": contract_binding,
        "task0_lineage": lineage,
    }
    probe = {**evidence}
    _validate_common_bindings(root, probe, run_spec)
    return evidence


def _success_contents(
    payload: Mapping[str, object], authority: Authority | None = None
) -> tuple[bytes, str]:
    _validate_success_receipt(payload, authority)
    contents = canonical_json_bytes(payload) + b"\n"
    return contents, _sha256_bytes(contents)


def _success_receipt_name(
    payload: Mapping[str, object], authority: Authority | None = None
) -> str:
    _, digest = _success_contents(payload, authority)
    return f"{int(payload['sequence']):06d}-{payload['phase']}-{digest}.json"


def _publish_locked(
    paths: _RegistryPaths,
    snapshot: _GitSnapshot,
    lock_binding: _RegistryLockBinding,
    relative: Path,
    contents: bytes,
    label: str,
    evidence_snapshots: Sequence[_ArtifactSnapshot] = (),
    *,
    expected_receipt_names: Sequence[str] | None = None,
    membership_validator: Callable[[Sequence[str]], None] | None = None,
    state_verifier: Callable[[], None] | None = None,
) -> None:
    child = _execution_child(paths, relative)
    _before_registry_publish()
    _verify_git_snapshot(paths.root, snapshot)
    _verify_lock_binding(paths, lock_binding)
    _verify_artifact_snapshots(
        paths.root, evidence_snapshots, "dynamic phase evidence"
    )
    if state_verifier is not None:
        state_verifier()

    def verify_after_publish() -> None:
        _verify_git_snapshot(paths.root, snapshot)
        _verify_lock_binding(paths, lock_binding)
        _verify_artifact_snapshots(
            paths.root, evidence_snapshots, "dynamic phase evidence"
        )
        if state_verifier is not None:
            state_verifier()

    _write_exclusive(
        lock_binding.execution_descriptor,
        child,
        contents,
        label,
        verify_after_publish,
        expected_receipt_names,
        membership_validator,
    )


def _publish_quarantine_locked(
    paths: _RegistryPaths,
    run_spec: Authority,
    state: ExecutionRegistryState,
    base: Mapping[str, object],
    attempted_phase: str,
    validations: list[dict[str, str]],
    snapshot: _GitSnapshot,
    lock_binding: _RegistryLockBinding,
    *,
    inventory: Mapping[str, object] | None = None,
    error_classification: str | None = None,
    evidence_snapshots: Sequence[_ArtifactSnapshot] = (),
) -> Path:
    payload = {
        "schema_version": 1,
        "receipt_kind": "QUARANTINED_TRANSITION",
        "run_id": run_spec.run_id,
        "attempted_phase": attempted_phase,
        "current_phase": state.phase,
        "current_receipt_sha256": state.receipt_sha256,
        "recorded_at_utc": _utc_now(),
        "git_head": snapshot.head,
        "run_spec": base["run_spec"],
        "validation_receipts": validations,
    }
    if inventory is not None and error_classification is not None:
        payload["inventory"] = dict(inventory)
        payload["error_classification"] = error_classification
    elif inventory is not None or error_classification is not None:
        raise ValueError("automatic quarantine evidence is incomplete")
    _validate_quarantine_receipt(payload, run_spec)
    contents = canonical_json_bytes(payload) + b"\n"
    digest = _sha256_bytes(contents)
    relative = (
        paths.quarantine_relative
        / f"{state.sequence + 1:06d}-{attempted_phase}-{digest}.json"
    )
    _publish_locked(
        paths,
        snapshot,
        lock_binding,
        relative,
        contents,
        "quarantine receipt",
        evidence_snapshots,
    )
    return paths.root / relative


def _checkpoint_sorted_sha_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a SHA-256 array")
    values = tuple(value)
    if any(
        not isinstance(item, str) or not _SHA256.fullmatch(item)
        for item in values
    ):
        raise ValueError(f"{label} must be a SHA-256 array")
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise ValueError(f"{label} must be sorted and unique")
    return values


def _is_recovery_source_run(run_spec: Authority | None) -> bool:
    return isinstance(run_spec, SourceRunSpec) and run_spec.run_id == _RECOVERY_RUN_ID


def _checkpoint_keys_for_authority(run_spec: Authority | None) -> frozenset[str]:
    if _is_recovery_source_run(run_spec):
        return _RECOVERY_CHECKPOINT_KEYS
    if isinstance(run_spec, SourceRunSpec):
        return _SOURCE_CHECKPOINT_KEYS
    return _CHECKPOINT_KEYS


def _recovery_binding_mapping(binding: object) -> dict[str, object]:
    values = {key: getattr(binding, key) for key in _RECOVERY_DISPOSITION_BINDING_KEYS}
    return {
        "game_pk": values["game_pk"],
        "query_id": values["query_id"],
        "query_sidecar": dict(values["query_sidecar"]),
        "success_receipt": dict(values["success_receipt"]),
        "retained_scheduled_start_utc": values["retained_scheduled_start_utc"],
        "played_date": values["played_date"],
        "result_available_on": values["result_available_on"],
        "raw_object_sha256s": list(values["raw_object_sha256s"]),
        "selected_raw_game_object_sha256s_sha256": values[
            "selected_raw_game_object_sha256s_sha256"
        ],
        "normalized_games_sha256": values["normalized_games_sha256"],
        "sidecar_semantics_sha256": values["sidecar_semantics_sha256"],
        "expected_derived_sidecar_sha256": values["expected_derived_sidecar_sha256"],
    }


def _validate_recovery_disposition_population(
    ids: Sequence[str], bindings: Sequence[object]
) -> tuple[str, str, str]:
    if len(bindings) != 3:
        raise ValueError(
            "recovery checkpoint requires exactly three disposition bindings"
        )
    try:
        expected = tuple(
            sorted(
                str(
                    binding["query_id"]
                    if isinstance(binding, Mapping)
                    else binding.query_id
                )
                for binding in bindings
            )
        )
    except (AttributeError, KeyError) as exc:
        raise ValueError("recovery checkpoint disposition binding differs") from exc
    if len(set(expected)) != 3 or tuple(ids) != expected:
        raise ValueError("recovery checkpoint dispositioned query IDs differ")
    return expected  # type: ignore[return-value]


def _validate_recovery_disposition_bindings(
    value: object,
) -> tuple[Mapping[str, object], ...]:
    bindings = _checkpoint_binding_list(
        value,
        _RECOVERY_DISPOSITION_BINDING_KEYS,
        "recovery checkpoint disposition bindings",
    )
    if len(bindings) not in {0, 3}:
        raise ValueError(
            "recovery checkpoint requires zero or three disposition bindings"
        )
    game_pks: list[int] = []
    for binding in bindings:
        game_pk = binding.get("game_pk")
        query_id = binding.get("query_id")
        if (
            type(game_pk) is not int
            or not isinstance(query_id, str)
            or not _SHA256.fullmatch(query_id)
        ):
            raise ValueError("recovery checkpoint disposition binding differs")
        game_pks.append(game_pk)
        for key in (
            "selected_raw_game_object_sha256s_sha256",
            "normalized_games_sha256",
            "sidecar_semantics_sha256",
            "expected_derived_sidecar_sha256",
        ):
            digest = binding.get(key)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError("recovery checkpoint disposition binding differs")
        raw_digests = binding.get("raw_object_sha256s")
        if (
            not isinstance(raw_digests, list)
            or not raw_digests
            or any(
                not isinstance(digest, str) or not _SHA256.fullmatch(digest)
                for digest in raw_digests
            )
        ):
            raise ValueError(
                "recovery checkpoint disposition raw-object binding differs"
            )
        for key in ("query_sidecar", "success_receipt"):
            _validate_binding_value(
                binding.get(key), "recovery checkpoint disposition binding"
            )
        retained = binding.get("retained_scheduled_start_utc")
        if not isinstance(retained, str):
            raise ValueError("recovery checkpoint disposition binding differs")
        for key in ("played_date", "result_available_on"):
            if binding.get(key) is not None and not isinstance(binding.get(key), str):
                raise ValueError("recovery checkpoint disposition binding differs")
    if bindings and tuple(game_pks) != (565534, 630882, 633224):
        raise ValueError("recovery checkpoint disposition catalog order differs")
    return bindings


def _validate_recovery_checkpoint_population(
    *,
    stage: object,
    completed_query_ids: Sequence[str],
    pending_query_ids: Sequence[str],
    quarantined_query_ids: Sequence[str],
    split_query_ids: Sequence[str],
    dispositioned_query_ids: Sequence[str],
    disposition_bindings: Sequence[object],
) -> None:
    dispositioned = tuple(dispositioned_query_ids)
    if not dispositioned:
        if (
            stage != "STATIC_SCHEDULES"
            or completed_query_ids
            or len(pending_query_ids) != 7
            or quarantined_query_ids
            or split_query_ids
            or disposition_bindings
        ):
            raise ValueError(
                "recovery checkpoint permits an empty catalog only at genesis"
            )
        return
    expected = _validate_recovery_disposition_population(
        dispositioned, disposition_bindings
    )
    if any(query_id not in completed_query_ids for query_id in expected) or any(
        query_id in {*pending_query_ids, *quarantined_query_ids, *split_query_ids}
        for query_id in expected
    ):
        raise ValueError(
            "recovery checkpoint disposition queries must be completed only"
        )
    if stage == "STATIC_SCHEDULES" and (
        len(completed_query_ids) != 7
        or pending_query_ids
        or quarantined_query_ids
        or split_query_ids
    ):
        raise ValueError("recovery terminal STATIC_SCHEDULES population differs")


def _checkpoint_binding_list(
    value: object,
    keys: frozenset[str],
    label: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    result: list[Mapping[str, object]] = []
    for item in value:
        result.append(_exact_object(item, keys, label))
    identities = [canonical_json_bytes(dict(item)) for item in result]
    if len(identities) != len(set(identities)):
        raise ValueError(f"{label} must contain unique bindings")
    return tuple(result)


def _validate_source_checkpoint_bindings(
    receipt: Mapping[str, object],
    batches: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
    stage_evidence: Sequence[Mapping[str, object]],
    splits: Sequence[Mapping[str, object]],
) -> None:
    """Keep every source-run checkpoint binding inside its new authority tree."""
    for key, expected in (
        ("run_spec", _SOURCE_RUN_SPEC_PATH),
        ("acquisition_contract", _SOURCE_CONTRACT_PATH),
    ):
        binding = receipt[key]
        assert isinstance(binding, Mapping)
        if binding["path"] != expected:
            raise ValueError("source checkpoint artifact binding differs")
    execution = receipt["execution_phase_receipt"]
    assert isinstance(execution, Mapping)
    if not _SOURCE_EXECUTION_RECEIPT.fullmatch(str(execution["path"])):
        raise ValueError("source checkpoint artifact binding differs")
    for item in batches:
        if (
            re.fullmatch(
            re.escape(_SOURCE_AUDIT_PREFIX)
            + r"acquisition/query_batches/[0-9a-f]{64}\.json",
            str(item["path"]),
            )
            is None
        ):
            raise ValueError("source checkpoint artifact binding differs")
    for item in evidence:
        path = str(item["path"])
        query_id = str(item["query_id"])
        match = _SOURCE_QUERY_EVIDENCE.fullmatch(path)
        if match is None or query_id not in {
            match.group("query_id"), match.group("quarantine_query_id")
        }:
            raise ValueError("source checkpoint query evidence binding differs")
        kind = str(item["receipt_kind"])
        if (
            kind == "ATTEMPT" and "/attempts/" not in path
        ) or (kind == "SUCCESS" and not path.endswith("/success.json")) or (
            kind == "QUARANTINE" and "/quarantine/" not in path
        ):
            raise ValueError("source checkpoint query evidence binding differs")
    for item in splits:
        if (
            re.fullmatch(
            re.escape(_SOURCE_AUDIT_PREFIX)
            + r"acquisition/split_receipts/[0-9a-f]{64}\.json",
            str(item["path"]),
            )
            is None
        ):
            raise ValueError("source checkpoint artifact binding differs")
    for item in stage_evidence:
        path = str(item["path"])
        if not (
            path.startswith(_SOURCE_AUDIT_PREFIX + "acquisition/")
            or path.startswith(_SOURCE_RAW_PREFIX)
            or path == "audit/v5_1_bt/protected_raw_inventory.json"
            or path.startswith("config/v5_2_qd/")
        ):
            raise ValueError("source checkpoint stage evidence binding differs")


def _validate_recovery_checkpoint_bindings(
    receipt: Mapping[str, object],
    batches: Sequence[Mapping[str, object]],
    evidence: Sequence[Mapping[str, object]],
    stage_evidence: Sequence[Mapping[str, object]],
    splits: Sequence[Mapping[str, object]],
) -> None:
    for key, expected in (
        ("run_spec", _RECOVERY_RUN_SPEC_PATH),
        ("acquisition_contract", _RECOVERY_CONTRACT_PATH),
    ):
        binding = receipt[key]
        assert isinstance(binding, Mapping)
        if binding["path"] != expected:
            raise ValueError("recovery checkpoint artifact binding differs")
    execution = receipt["execution_phase_receipt"]
    assert isinstance(execution, Mapping)
    if (
        re.fullmatch(
            re.escape(_RECOVERY_AUDIT_PREFIX)
            + r"execution/phase_receipts/000001-ACQUIRING-[0-9a-f]{64}\.json",
            str(execution["path"]),
        )
        is None
    ):
        raise ValueError("recovery checkpoint artifact binding differs")
    for item in batches:
        if (
            re.fullmatch(
                re.escape(_RECOVERY_AUDIT_PREFIX)
                + r"acquisition/query_batches/[0-9a-f]{64}\.json",
                str(item["path"]),
            )
            is None
        ):
            raise ValueError("recovery checkpoint artifact binding differs")
    for item in evidence:
        path = str(item["path"])
        query_id = str(item["query_id"])
        match = re.fullmatch(
            re.escape(_RECOVERY_RAW_PREFIX)
            + r"(?:queries/[A-Za-z0-9_-]+/(?P<query_id>[0-9a-f]{64})/(?:attempts/[0-9]{6}\.json|success\.json)|quarantine/[A-Za-z0-9_-]+/(?P<quarantine_query_id>[0-9a-f]{64})/[0-9]{6}\.json)",
            path,
        )
        if match is None or query_id not in {
            match.group("query_id"),
            match.group("quarantine_query_id"),
        }:
            raise ValueError("recovery checkpoint query evidence binding differs")
    for item in splits:
        if (
            re.fullmatch(
                re.escape(_RECOVERY_AUDIT_PREFIX)
                + r"acquisition/split_receipts/[0-9a-f]{64}\.json",
                str(item["path"]),
            )
            is None
        ):
            raise ValueError("recovery checkpoint artifact binding differs")
    for item in stage_evidence:
        path = str(item["path"])
        if not (
            path.startswith(_RECOVERY_AUDIT_PREFIX + "acquisition/")
            or path.startswith(_RECOVERY_RAW_PREFIX)
            or path.startswith("config/v5_2_qd/")
        ):
            raise ValueError("recovery checkpoint stage evidence binding differs")


def _validate_source_disposition_population(
    *,
    completed_query_ids: Sequence[str],
    pending_query_ids: Sequence[str],
    quarantined_query_ids: Sequence[str],
    split_query_ids: Sequence[str],
    dispositioned_query_ids: Sequence[str],
) -> None:
    dispositioned = tuple(dispositioned_query_ids)
    if dispositioned not in ((), (_DISPOSITIONED_QUERY_ID,)):
        raise ValueError("source checkpoint dispositioned query ID differs")
    is_dispositioned = dispositioned == (_DISPOSITIONED_QUERY_ID,)
    if (_DISPOSITIONED_QUERY_ID in completed_query_ids) != is_dispositioned:
        raise ValueError(
            "source checkpoint derived query must be completed iff dispositioned"
        )
    if is_dispositioned and _DISPOSITIONED_QUERY_ID in {
        *pending_query_ids,
        *quarantined_query_ids,
        *split_query_ids,
    }:
        raise ValueError(
            "source checkpoint dispositioned query must be completed only"
        )


def _validate_checkpoint_payload(
    payload: object, run_spec: Authority | None = None
) -> Mapping[str, object]:
    keys = _checkpoint_keys_for_authority(run_spec)
    receipt = _exact_object(payload, keys, "acquisition checkpoint")
    run_id = run_spec.run_id if run_spec is not None else "historical_walkforward_500"
    sequence = receipt.get("sequence")
    prior = receipt.get("prior_checkpoint_sha256")
    stage = receipt.get("stage")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "ACQUISITION_CHECKPOINT"
        or receipt.get("run_id") != run_id
        or receipt.get("execution_phase") != "ACQUIRING"
        or type(sequence) is not int
        or sequence < 0
        or stage not in ACQUISITION_STAGES
        or (sequence == 0 and prior is not None)
        or (
            sequence > 0
            and (not isinstance(prior, str) or not _SHA256.fullmatch(prior))
        )
        or not isinstance(receipt.get("git_head"), str)
        or not _GIT_HEAD.fullmatch(str(receipt.get("git_head")))
    ):
        raise ValueError("acquisition checkpoint identity differs")
    _validate_timestamp(receipt.get("recorded_at_utc"), "acquisition checkpoint")
    _validate_binding_value(receipt.get("run_spec"), "checkpoint RunSpec")
    _validate_binding_value(
        receipt.get("acquisition_contract"), "checkpoint acquisition contract"
    )
    _validate_binding_value(
        receipt.get("execution_phase_receipt"), "checkpoint execution receipt"
    )
    batches = _checkpoint_binding_list(
        receipt.get("query_batches"),
        frozenset({"batch_id", "path", "sha256", "stage"}),
        "checkpoint query batches",
    )
    for batch in batches:
        _validate_sha = batch.get("batch_id")
        if not isinstance(_validate_sha, str) or not _SHA256.fullmatch(_validate_sha):
            raise ValueError("checkpoint batch ID differs")
        _validate_binding_value(
            {"path": batch.get("path"), "sha256": batch.get("sha256")},
            "checkpoint query batch",
        )
        if batch.get("stage") not in ACQUISITION_STAGES:
            raise ValueError("checkpoint batch stage differs")
    evidence = _checkpoint_binding_list(
        receipt.get("query_evidence_receipts"),
        frozenset({"path", "query_id", "receipt_kind", "sha256"}),
        "checkpoint query evidence",
    )
    for item in evidence:
        query_id = item.get("query_id")
        if not isinstance(query_id, str) or not _SHA256.fullmatch(query_id):
            raise ValueError("checkpoint evidence query ID differs")
        if item.get("receipt_kind") not in {"ATTEMPT", "QUARANTINE", "SUCCESS"}:
            raise ValueError("checkpoint evidence kind differs")
        _validate_binding_value(
            {"path": item.get("path"), "sha256": item.get("sha256")},
            "checkpoint query evidence",
        )
    if tuple(
        (str(item["query_id"]), str(item["receipt_kind"]), str(item["path"]))
        for item in evidence
    ) != tuple(
        sorted(
            (
                str(item["query_id"]),
                str(item["receipt_kind"]),
                str(item["path"]),
            )
            for item in evidence
        )
    ):
        raise ValueError("checkpoint query evidence order differs")
    stage_evidence = _checkpoint_binding_list(
        receipt.get("stage_evidence_receipts"),
        _BINDING_KEYS,
        "checkpoint stage evidence",
    )
    for item in stage_evidence:
        _validate_binding_value(item, "checkpoint stage evidence")
    if tuple(str(item["path"]) for item in stage_evidence) != tuple(
        sorted(str(item["path"]) for item in stage_evidence)
    ):
        raise ValueError("checkpoint stage evidence order differs")
    splits = _checkpoint_binding_list(
        receipt.get("split_receipts"),
        frozenset({"parent_query_id", "path", "sha256", "split_receipt_id"}),
        "checkpoint split receipts",
    )
    for item in splits:
        for key in ("parent_query_id", "split_receipt_id"):
            digest = item.get(key)
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ValueError("checkpoint split binding differs")
        _validate_binding_value(
            {"path": item.get("path"), "sha256": item.get("sha256")},
            "checkpoint split receipt",
        )
    if tuple(str(item["parent_query_id"]) for item in splits) != tuple(
        sorted(str(item["parent_query_id"]) for item in splits)
    ):
        raise ValueError("checkpoint split receipt order differs")
    populations = tuple(
        _checkpoint_sorted_sha_list(receipt.get(key), f"checkpoint {key}")
        for key in (
            "completed_query_ids",
            "pending_query_ids",
            "quarantined_query_ids",
            "split_query_ids",
        )
    )
    if sum(len(values) for values in populations) != len(
        set().union(*(set(values) for values in populations))
    ):
        raise ValueError("checkpoint query status populations overlap")
    if _is_recovery_source_run(run_spec):
        _validate_recovery_checkpoint_bindings(
            receipt, batches, evidence, stage_evidence, splits
        )
        _validate_recovery_disposition_bindings(
            receipt.get("recovery_disposition_bindings")
        )
        dispositioned = _checkpoint_sorted_sha_list(
            receipt.get("dispositioned_query_ids"),
            "checkpoint dispositioned_query_ids",
        )
        completed, pending, quarantined, split = populations
        _validate_recovery_checkpoint_population(
            stage=stage,
            completed_query_ids=completed,
            pending_query_ids=pending,
            quarantined_query_ids=quarantined,
            split_query_ids=split,
            dispositioned_query_ids=dispositioned,
            disposition_bindings=receipt["recovery_disposition_bindings"],  # type: ignore[arg-type]
        )
    elif isinstance(run_spec, SourceRunSpec):
        _validate_source_checkpoint_bindings(
            receipt, batches, evidence, stage_evidence, splits
        )
        dispositioned = _checkpoint_sorted_sha_list(
            receipt.get("dispositioned_query_ids"),
            "checkpoint dispositioned_query_ids",
        )
        completed, pending, quarantined, split = populations
        _validate_source_disposition_population(
            completed_query_ids=completed,
            pending_query_ids=pending,
            quarantined_query_ids=quarantined,
            split_query_ids=split,
            dispositioned_query_ids=dispositioned,
        )
        serialized = canonical_json_bytes(dict(receipt))
        if any(
            forbidden.encode() in serialized
            for forbidden in (
                _OLD_QUARANTINED_QUERY_ID,
                "data/v5_2_qd/historical/raw/queries/",
                "data/v5_2_qd/historical/raw/quarantine/",
                "audit/v5_2_qd/historical/acquisition/",
            )
        ):
            raise ValueError("source checkpoint contains old query artifact binding")
    return receipt


def _issue_source_checkpoint_authority(
    checkpoint: Mapping[str, object],
    run_spec: SourceRunSpec,
    lock_binding: _RegistryLockBinding,
) -> _VerifiedSourceCheckpointAuthority:
    """Issue a lexical capability only inside a locked registry replay."""
    if not isinstance(run_spec, SourceRunSpec):
        raise TypeError("source checkpoint capability requires a SourceRunSpec")
    if type(lock_binding) is not _RegistryLockBinding:
        raise TypeError("source checkpoint registry lock authority differs")
    try:
        lock_metadata = os.fstat(lock_binding.lock_descriptor)
        execution_metadata = os.fstat(lock_binding.execution_descriptor)
    except OSError as exc:
        raise ValueError("source checkpoint registry lock is unavailable") from exc
    if not stat.S_ISREG(lock_metadata.st_mode) or not stat.S_ISDIR(
        execution_metadata.st_mode
    ):
        raise ValueError("source checkpoint registry lock identity differs")
    validated = _validate_checkpoint_payload(checkpoint, run_spec)
    batches = validated["query_batches"]
    assert isinstance(batches, list)
    query_batch_ids = tuple(str(item["batch_id"]) for item in batches)
    completed = tuple(validated["completed_query_ids"])  # type: ignore[arg-type]
    pending = tuple(validated["pending_query_ids"])  # type: ignore[arg-type]
    quarantined = tuple(validated["quarantined_query_ids"])  # type: ignore[arg-type]
    split = tuple(validated["split_query_ids"])  # type: ignore[arg-type]
    dispositioned = tuple(validated["dispositioned_query_ids"])  # type: ignore[arg-type]
    if _is_recovery_source_run(run_spec):
        bindings = _validate_recovery_disposition_bindings(
            validated["recovery_disposition_bindings"]
        )
        _validate_recovery_checkpoint_population(
            stage=validated["stage"],
            completed_query_ids=completed,
            pending_query_ids=pending,
            quarantined_query_ids=quarantined,
            split_query_ids=split,
            dispositioned_query_ids=dispositioned,
            disposition_bindings=bindings,
        )
        if not batches or batches[0]["stage"] != "STATIC_SCHEDULES":
            raise ValueError("recovery checkpoint capability bootstrap differs")
        return _VerifiedSourceCheckpointAuthority(
            run_id=run_spec.run_id,
            stage=str(validated["stage"]),
            query_batch_ids=query_batch_ids,
            completed_query_ids=completed,
            pending_query_ids=pending,
            quarantined_query_ids=quarantined,
            split_query_ids=split,
            dispositioned_query_ids=dispositioned,
            _seal=_SOURCE_CHECKPOINT_CAPABILITY_SEAL,
        )
    completed_disposition = (
        dispositioned == (_DISPOSITIONED_QUERY_ID,)
        and _DISPOSITIONED_QUERY_ID in completed
        and _DISPOSITIONED_QUERY_ID not in pending
        and _DISPOSITIONED_QUERY_ID not in quarantined
        and _DISPOSITIONED_QUERY_ID not in split
    )
    pending_disposition = (
        not dispositioned
        and _DISPOSITIONED_QUERY_ID in pending
        and _DISPOSITIONED_QUERY_ID not in completed
        and _DISPOSITIONED_QUERY_ID not in quarantined
        and _DISPOSITIONED_QUERY_ID not in split
    )
    if (
        not batches
        or batches[0]["stage"] != "STATIC_SCHEDULES"
        or not (completed_disposition or pending_disposition)
    ):
        raise ValueError("source checkpoint capability bootstrap differs")
    return _VerifiedSourceCheckpointAuthority(
        run_id=run_spec.run_id,
        stage=str(validated["stage"]),
        query_batch_ids=query_batch_ids,
        completed_query_ids=completed,
        pending_query_ids=pending,
        quarantined_query_ids=quarantined,
        split_query_ids=split,
        dispositioned_query_ids=dispositioned,
        _seal=_SOURCE_CHECKPOINT_CAPABILITY_SEAL,
    )


@contextmanager
def _using_source_checkpoint_authority(
    checkpoint: _VerifiedSourceCheckpointAuthority,
) -> Iterator[None]:
    if (
        type(checkpoint) is not _VerifiedSourceCheckpointAuthority
        or checkpoint._seal is not _SOURCE_CHECKPOINT_CAPABILITY_SEAL
    ):
        raise TypeError("source checkpoint authority capability differs")
    token = _SOURCE_CHECKPOINT_AUTHORITY.set(checkpoint)
    try:
        yield
    finally:
        _SOURCE_CHECKPOINT_AUTHORITY.reset(token)


def _current_source_checkpoint_authority(
) -> _VerifiedSourceCheckpointAuthority | None:
    return _SOURCE_CHECKPOINT_AUTHORITY.get()


@contextmanager
def _using_verified_source_checkpoint(
    root: Path,
    run_spec: SourceRunSpec,
) -> Iterator[None]:
    """Hold one authenticated source checkpoint authority across a replay."""
    if not isinstance(run_spec, SourceRunSpec):
        raise TypeError("verified checkpoint scope requires a SourceRunSpec")
    current = _current_source_checkpoint_authority()
    if current is not None:
        if current.run_id != run_spec.run_id:
            raise ValueError("source checkpoint scope run differs")
        yield
        return

    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        state, registry_payloads = _load_locked(paths, run_spec, lock_binding)
        if state.phase not in {"ACQUIRING", "SOURCES_READY"}:
            raise ValueError(
                "source checkpoint scope requires ACQUIRING or SOURCES_READY"
            )
        snapshot = _capture_git_snapshot(paths.root, registry_payloads[0])
        checkpoints, payloads, _ = _load_checkpoints_locked(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            lock_binding,
            verify_latest_progress=False,
            read_only_progress=_checkpoint_progress_is_read_only(
                run_spec,
                state.phase,
                registry_payloads[-1].get("acquisition_checkpoint_head"),
            ),
        )
        if not checkpoints or not payloads:
            raise ValueError("source checkpoint scope requires a checkpoint")
        authority = _issue_source_checkpoint_authority(
            payloads[-1], run_spec, lock_binding
        )
        with _using_source_checkpoint_authority(authority):
            yield


def _checkpoint_from_payload(
    payload: Mapping[str, object], receipt_sha256: str
) -> AcquisitionCheckpoint:
    batches = payload["query_batches"]
    evidence = payload["query_evidence_receipts"]
    stage_evidence = payload["stage_evidence_receipts"]
    splits = payload["split_receipts"]
    assert isinstance(batches, list)
    assert isinstance(evidence, list)
    assert isinstance(stage_evidence, list)
    assert isinstance(splits, list)
    return AcquisitionCheckpoint(
        sequence=int(payload["sequence"]),
        receipt_sha256=receipt_sha256,
        prior_checkpoint_sha256=payload["prior_checkpoint_sha256"],  # type: ignore[arg-type]
        stage=str(payload["stage"]),
        recorded_at_utc=str(payload["recorded_at_utc"]),
        git_head=str(payload["git_head"]),
        execution_receipt_sha256=str(
            payload["execution_phase_receipt"]["sha256"]  # type: ignore[index]
        ),
        query_batch_ids=tuple(str(item["batch_id"]) for item in batches),
        query_batch_sha256s=tuple(str(item["sha256"]) for item in batches),
        query_evidence_receipt_sha256s=tuple(
            str(item["sha256"]) for item in evidence
        ),
        stage_evidence_receipt_sha256s=tuple(
            str(item["sha256"]) for item in stage_evidence
        ),
        split_receipt_sha256s=tuple(str(item["sha256"]) for item in splits),
        completed_query_ids=tuple(payload["completed_query_ids"]),  # type: ignore[arg-type]
        pending_query_ids=tuple(payload["pending_query_ids"]),  # type: ignore[arg-type]
        quarantined_query_ids=tuple(payload["quarantined_query_ids"]),  # type: ignore[arg-type]
        split_query_ids=tuple(payload["split_query_ids"]),  # type: ignore[arg-type]
        stage_evidence_receipt_paths=tuple(
            str(item["path"]) for item in stage_evidence
        ),
        dispositioned_query_ids=tuple(payload.get("dispositioned_query_ids", ())),
        recovery_disposition_bindings=tuple(
            dict(item)
            for item in payload.get("recovery_disposition_bindings", ())  # type: ignore[union-attr]
        ),
    )


def _checkpoint_semantic_projection(
    payload: Mapping[str, object],
) -> dict[str, object]:
    keys = (
        _RECOVERY_CHECKPOINT_KEYS
        if payload.get("run_id") == _RECOVERY_RUN_ID
        else (
        _SOURCE_CHECKPOINT_KEYS
            if payload.get("run_id") == "historical_walkforward_500_superseding_565534"
        else _CHECKPOINT_KEYS
    )
    )
    return {
        key: payload[key]
        for key in keys
        if key not in {"sequence", "prior_checkpoint_sha256", "recorded_at_utc"}
    }


def _checkpoint_artifact_binding(root: Path, relative: Path, label: str) -> dict[str, str]:
    artifact = _capture_artifact(root, root / relative, label)
    return artifact.binding


def _checkpoint_evidence_root(paths: _RegistryPaths, run_spec: Authority) -> Path:
    return paths.root / _authority_namespace(run_spec, "raw")


def _recovery_disposition_catalog_bindings(
    paths: _RegistryPaths,
    run_spec: SourceRunSpec,
    run_binding: Mapping[str, object],
    contract_binding: Mapping[str, object],
) -> tuple[tuple[Mapping[str, object], ...], Mapping[str, object]]:
    """Reauthenticate Task 4's closed catalog before any checkpoint binding use."""
    if not _is_recovery_source_run(run_spec):
        raise TypeError("recovery disposition catalog requires the recovery authority")
    from mlb_backtest.v52_evidence import logical_query_id
    from mlb_backtest.v52_schedule_conflict_recovery_disposition import (
        build_recovery_schedule_conflict_catalog,
        load_recovery_disposition_bindings,
    )

    catalog = build_recovery_schedule_conflict_catalog(
        paths.root,
        paths.root / str(run_binding["path"]),
        paths.root / str(contract_binding["path"]),
    )
    if any(
        entry.evidence_root != _checkpoint_evidence_root(paths, run_spec)
        for entry in catalog.entries
    ):
        raise ValueError("recovery disposition evidence root differs")
    bindings = tuple(
        _recovery_binding_mapping(item)
        for item in load_recovery_disposition_bindings(catalog)
    )
    if tuple(int(item["game_pk"]) for item in bindings) != (565534, 630882, 633224):
        raise ValueError("recovery disposition catalog order differs")
    authorities = {logical_query_id(entry.query): entry for entry in catalog.entries}
    if tuple(sorted(authorities)) != tuple(
        sorted(str(item["query_id"]) for item in bindings)
    ):
        raise ValueError("recovery disposition catalog query IDs differ")
    return bindings, authorities


def _source_disposition_authority(
    paths: _RegistryPaths,
    run_spec: SourceRunSpec,
    run_binding: Mapping[str, object],
    contract_binding: Mapping[str, object],
) -> object:
    """Bind the one derived disposition query to its new root on every read."""
    from mlb_backtest.v52_duplicate_gamepk_disposition import (
        build_duplicate_disposition_authority,
    )
    from mlb_backtest.v52_evidence import logical_query_id

    authority = build_duplicate_disposition_authority(
        paths.root,
        source_run_spec_path=paths.root / str(run_binding["path"]),
        acquisition_contract_path=paths.root / str(contract_binding["path"]),
    )
    if authority.evidence_root != _checkpoint_evidence_root(paths, run_spec):
        raise ValueError("source disposition evidence root differs")
    if logical_query_id(authority.query) != _DISPOSITIONED_QUERY_ID:
        raise ValueError("source disposition query ID differs")
    return authority


def _checkpoint_split_dag_active_leaves(
    batch_records: Sequence[tuple[str, str, str | None, tuple[str, ...]]],
    receipt_records: Sequence[tuple[str, str, str, str, tuple[str, ...]]],
    split_query_ids: Sequence[str],
) -> set[str]:
    """Validate one checkpoint-bound split DAG and return its active leaves."""
    fixed_prefix_length = 4
    if len(batch_records) < fixed_prefix_length:
        raise ValueError("checkpoint split DAG lacks its fixed batch prefix")
    batch_ids = [record[0] for record in batch_records]
    if len(batch_ids) != len(set(batch_ids)):
        raise ValueError("checkpoint split DAG contains duplicate batch IDs")
    if any(
        stage != "REMAINING"
        for _batch_id, stage, _derived, _query_ids in batch_records[
            fixed_prefix_length:
        ]
    ):
        raise ValueError("checkpoint split DAG suffix batch stage differs")
    batch_index = {batch_id: index for index, batch_id in enumerate(batch_ids)}
    batch_by_id = {record[0]: record for record in batch_records}
    all_query_ids = [
        query_id
        for _batch_id, _stage, _derived, query_ids in batch_records
        for query_id in query_ids
    ]
    if len(all_query_ids) != len(set(all_query_ids)):
        raise ValueError("checkpoint split DAG query occurs in multiple batches")

    receipt_ids = [record[0] for record in receipt_records]
    receipt_parent_ids = [record[2] for record in receipt_records]
    receipt_child_ids = [record[3] for record in receipt_records]
    if (
        len(receipt_ids) != len(set(receipt_ids))
        or len(receipt_parent_ids) != len(set(receipt_parent_ids))
        or len(receipt_child_ids) != len(set(receipt_child_ids))
    ):
        raise ValueError(
            "checkpoint split DAG contains duplicate receipt, parent, or child"
        )
    suffix_ids = set(batch_ids[fixed_prefix_length:])
    if set(receipt_child_ids) != suffix_ids:
        raise ValueError("checkpoint split DAG suffix and receipt children differ")
    supplied_split_ids = tuple(split_query_ids)
    if (
        len(supplied_split_ids) != len(set(supplied_split_ids))
        or set(supplied_split_ids) != set(receipt_parent_ids)
    ):
        raise ValueError("checkpoint split DAG parents differ from split query IDs")

    for (
        _receipt_id,
        parent_batch_id,
        parent_query_id,
        child_batch_id,
        child_query_ids,
    ) in receipt_records:
        if parent_batch_id not in batch_by_id or child_batch_id not in batch_by_id:
            raise ValueError("checkpoint split DAG receipt batch is orphaned")
        parent = batch_by_id[parent_batch_id]
        child = batch_by_id[child_batch_id]
        if batch_index[child_batch_id] < fixed_prefix_length:
            raise ValueError("checkpoint split DAG child cannot be in the fixed prefix")
        if batch_index[parent_batch_id] >= batch_index[child_batch_id]:
            raise ValueError("checkpoint split DAG parent must precede its child")
        if child[2] != parent_batch_id:
            raise ValueError("checkpoint split DAG child derivation differs")
        if parent[3].count(parent_query_id) != 1:
            raise ValueError("checkpoint split DAG parent query binding differs")
        if tuple(child_query_ids) != child[3]:
            raise ValueError("checkpoint split DAG ordered child query IDs differ")

    remaining_query_ids = {
        query_id
        for _batch_id, _stage, _derived, query_ids in batch_records[3:]
        for query_id in query_ids
    }
    if not set(supplied_split_ids) <= remaining_query_ids:
        raise ValueError("checkpoint split DAG parent is outside REMAINING")
    return remaining_query_ids - set(supplied_split_ids)


def _validate_checkpoint_remaining_population(
    *,
    active_leaf_ids: set[str],
    prefix_ids: set[str],
    completed_query_ids: set[str],
    pending_query_ids: set[str],
    quarantined_query_ids: set[str],
) -> None:
    if (
        not prefix_ids <= completed_query_ids
        or pending_query_ids - active_leaf_ids
        or quarantined_query_ids - active_leaf_ids
    ):
        raise ValueError(
            "REMAINING requires a terminal prefix and exact active-leaf state"
        )


def _checkpoint_stage_authority(
    paths: _RegistryPaths,
    run_spec: Authority,
    *,
    stage: str,
    run_binding: Mapping[str, object],
    contract_binding: Mapping[str, object],
    read_only_progress: bool = False,
    source_checkpoint_authority: object | None = None,
) -> tuple[tuple[object, ...], tuple[dict[str, str], ...]]:
    from mlb_backtest.v52_query_plan import (
        _build_schedule_reference_batch_from_inventory,
        build_legacy_partition_batch,
        build_participant_derived_batch,
        build_statcast_root_batch,
        build_statcast_root_batch_for_authority,
        build_static_batch,
        load_query_batch_for_authority,
    )

    run_path = paths.root / str(run_binding["path"])
    contract_path = paths.root / str(contract_binding["path"])
    static_batch = build_static_batch(run_path, contract_path)
    if stage == "STATIC_SCHEDULES":
        return (static_batch,), ()
    if stage not in {
        "REFERENCE_INPUTS",
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }:
        raise ValueError(
            "non-static checkpoint stage authority is unavailable until implemented"
        )
    from mlb_backtest.v52_schedule_inventory import (
        load_verified_schedule_anchor_inventory,
    )

    evidence_root = _checkpoint_evidence_root(paths, run_spec)
    if source_checkpoint_authority is not None:
        with _using_source_checkpoint_authority(source_checkpoint_authority):
            verified = load_verified_schedule_anchor_inventory(
                paths.root,
                run_spec,
                acquisition_contract_path=contract_path,
                static_batch_id=static_batch.batch_id,
                evidence_root=evidence_root,
                read_only_progress=read_only_progress,
            )
    else:
        verified = load_verified_schedule_anchor_inventory(
            paths.root,
            run_spec,
            acquisition_contract_path=contract_path,
            static_batch_id=static_batch.batch_id,
            evidence_root=evidence_root,
            read_only_progress=read_only_progress,
        )
    reference_batch = _build_schedule_reference_batch_from_inventory(
        verified.payload,
        inventory_sha256=verified.sha256,
        run_spec_sha256=str(run_binding["sha256"]),
        acquisition_contract_sha256=str(contract_binding["sha256"]),
    )
    if reference_batch.derived_from_sha256 != verified.sha256:
        raise ValueError("REFERENCE_INPUTS inventory-derived batch binding differs")
    if stage == "REFERENCE_INPUTS":
        schedule_inventory_path = (
            _authority_namespace(run_spec, "audit")
            / "acquisition/schedule_anchor_inventory.json"
        ).as_posix()
        return (
            (static_batch, reference_batch),
            ({"path": schedule_inventory_path, "sha256": verified.sha256},),
        )

    from mlb_backtest.v52_timezone_review import load_verified_timezone_gate

    gate_kwargs = (
        {
            "run_spec_path": run_path,
            "acquisition_contract_path": contract_path,
        }
        if isinstance(run_spec, SourceRunSpec)
        else {}
    )
    if source_checkpoint_authority is not None:
        with _using_source_checkpoint_authority(source_checkpoint_authority):
            gate = load_verified_timezone_gate(
                paths.root,
                read_only_progress=read_only_progress,
                **gate_kwargs,
            )
    else:
        gate = load_verified_timezone_gate(
            paths.root,
            read_only_progress=read_only_progress,
            **gate_kwargs,
        )
    if (
        gate.run_spec_sha256 != run_binding["sha256"]
        or gate.acquisition_contract_sha256 != contract_binding["sha256"]
        or gate.schedule_inventory_sha256 != verified.sha256
        or gate.query_batch_ids
        != (static_batch.batch_id, reference_batch.batch_id)
    ):
        raise ValueError("TIMEZONE_REVIEW gate authority binding differs")
    timezone_paths = _timezone_stage_evidence_paths(run_spec)
    stage_evidence = (
        {"path": timezone_paths[0], "sha256": verified.sha256},
        {
            "path": timezone_paths[1],
            "sha256": gate.review_sha256s[0],
        },
        {
            "path": timezone_paths[2],
            "sha256": gate.review_sha256s[1],
        },
        {
            "path": timezone_paths[3],
            "sha256": gate.candidate_sha256,
        },
        {
            "path": timezone_paths[4],
            "sha256": gate.seal_sha256,
        },
        {
            "path": timezone_paths[5],
            "sha256": gate.mapping_sha256,
        },
    )
    if stage == "TIMEZONE_REVIEW":
        return (static_batch, reference_batch), stage_evidence

    from mlb_backtest.v52_game_evidence_inventory import (
        load_verified_game_evidence_inventory,
    )

    inventory = load_verified_game_evidence_inventory(
        paths.root,
        run_spec_path=run_path,
        acquisition_contract_path=contract_path,
        read_only_progress=read_only_progress,
    )
    participant_batch = build_participant_derived_batch(
        paths.root,
        run_spec_path=run_path,
        acquisition_contract_path=contract_path,
        read_only_progress=read_only_progress,
    )
    if participant_batch.derived_from_sha256 != inventory.sha256 or any(
            query.run_spec_sha256 != run_binding["sha256"]
            or query.acquisition_contract_sha256 != contract_binding["sha256"]
            for query in participant_batch.queries
    ):
        raise ValueError("PARTICIPANT_DERIVED game inventory authority differs")
    try:
        inventory_relative = inventory.path.relative_to(paths.root).as_posix()
    except ValueError as exc:
        raise ValueError("PARTICIPANT_DERIVED game inventory path differs") from exc
    expected_inventory_relative = (
        _authority_namespace(run_spec, "audit")
        / "acquisition/game_evidence_inventory.json"
    ).as_posix()
    if inventory_relative != expected_inventory_relative:
        raise ValueError("PARTICIPANT_DERIVED game inventory path differs")
    participant_stage_evidence = tuple(
        sorted(
            (
                *stage_evidence,
                {
                    "path": inventory_relative,
                    "sha256": inventory.sha256,
                },
            ),
            key=lambda item: item["path"],
        )
    )
    if stage == "PARTICIPANT_DERIVED":
        return (
            (static_batch, reference_batch, participant_batch),
            participant_stage_evidence,
        )

    from mlb_backtest.v52_legacy_decision_inventory import (
        load_verified_legacy_decision_inventory,
    )

    root_expected = (
        build_statcast_root_batch_for_authority(
            paths.root,
            run_spec_path=run_path,
            acquisition_contract_path=contract_path,
        )
        if isinstance(run_spec, SourceRunSpec)
        else build_statcast_root_batch(paths.root)
    )
    root_batch = load_query_batch_for_authority(
        paths.root,
        root_expected.batch_id,
        run_spec,
        read_only_progress=read_only_progress,
    )
    if root_batch != root_expected or len(root_batch.queries) != 46:
        raise ValueError("REMAINING Statcast root authority differs")
    decision = load_verified_legacy_decision_inventory(
        paths.root,
        run_spec_path=run_path,
        acquisition_contract_path=contract_path,
    )
    partition_batch = build_legacy_partition_batch(
        paths.root,
        run_spec_path=run_path,
        acquisition_contract_path=contract_path,
        read_only_progress=read_only_progress,
    )
    if (
        partition_batch.derived_from_sha256 != decision.sha256
        or len(partition_batch.queries) != 48
    ):
        raise ValueError("REMAINING partition decision authority differs")
    protected = decision.payload.get("protected_inventory")
    if not isinstance(protected, Mapping):
        raise ValueError("REMAINING protected inventory authority differs")
    protected_binding = _checkpoint_artifact_binding(
        paths.root,
        Path(_PROTECTED_RAW_INVENTORY_PATH),
        "REMAINING protected raw inventory",
    )
    decision_relative = (
        _authority_namespace(run_spec, "audit")
        / "acquisition/legacy_decision_inventory.json"
    )
    decision_binding = _checkpoint_artifact_binding(
        paths.root,
        decision_relative,
        "REMAINING legacy decision inventory",
    )
    root_relative = (
        _authority_namespace(run_spec, "audit")
        / "acquisition/query_batches"
        / f"{root_batch.batch_id}.json"
    )
    root_binding = _checkpoint_artifact_binding(
        paths.root, root_relative, "REMAINING Statcast root batch"
    )
    if (
        protected_binding["sha256"] != protected.get("sha256")
        or decision_binding["sha256"] != decision.sha256
    ):
        raise ValueError("REMAINING stage evidence lineage differs")
    remaining_stage_evidence = tuple(
        sorted(
            (
                *participant_stage_evidence,
                protected_binding,
                decision_binding,
                root_binding,
            ),
            key=lambda item: item["path"],
        )
    )
    if len(remaining_stage_evidence) != 10:
        raise ValueError("REMAINING requires exactly ten stage evidence bindings")
    return (
        (static_batch, reference_batch, participant_batch, partition_batch),
        remaining_stage_evidence,
    )


def _checkpoint_semantic_snapshots(
    paths: _RegistryPaths,
    semantic: Mapping[str, object],
) -> tuple[_ArtifactSnapshot, ...]:
    bindings: list[Mapping[str, object]] = []
    for key in ("run_spec", "acquisition_contract", "execution_phase_receipt"):
        value = semantic[key]
        assert isinstance(value, Mapping)
        bindings.append(value)
    for key in (
        "query_batches",
        "query_evidence_receipts",
        "stage_evidence_receipts",
        "split_receipts",
    ):
        values = semantic[key]
        assert isinstance(values, list)
        for value in values:
            assert isinstance(value, Mapping)
            bindings.append(value)
    if semantic.get("run_id") == _RECOVERY_RUN_ID:
        recovery_bindings = semantic.get("recovery_disposition_bindings")
        if not isinstance(recovery_bindings, list):
            raise ValueError("recovery checkpoint disposition bindings differ")
        for recovery_binding in recovery_bindings:
            if not isinstance(recovery_binding, Mapping):
                raise ValueError("recovery checkpoint disposition binding differs")
            for key in ("query_sidecar", "success_receipt"):
                artifact = recovery_binding.get(key)
                if not isinstance(artifact, Mapping):
                    raise ValueError("recovery checkpoint disposition binding differs")
                relative = artifact.get("path")
                digest = artifact.get("sha256")
                if (
                    not isinstance(relative, str)
                    or not isinstance(digest, str)
                    or not _SHA256.fullmatch(digest)
                ):
                    raise ValueError("recovery checkpoint disposition binding differs")
                bindings.append(
                    {
                        "path": (
                            Path(_RECOVERY_RAW_PREFIX) / relative
                        ).as_posix(),
                        "sha256": digest,
                    }
                )
    snapshots: list[_ArtifactSnapshot] = []
    seen: set[str] = set()
    for binding in bindings:
        relative = str(binding["path"])
        if relative in seen:
            continue
        seen.add(relative)
        snapshot = _capture_artifact(
            paths.root,
            paths.root / relative,
            "checkpoint bound artifact",
        )
        if snapshot.binding != {
            "path": binding["path"],
            "sha256": binding["sha256"],
        }:
            raise ValueError("checkpoint bound artifact snapshot differs")
        snapshots.append(snapshot)
    return tuple(snapshots)


def _checkpoint_current_semantic(
    paths: _RegistryPaths,
    run_spec: Authority,
    registry_payloads: Sequence[Mapping[str, object]],
    git_snapshot: _GitSnapshot,
    *,
    stage: str,
    query_batch_ids: Sequence[str],
    completed_query_ids: Sequence[str],
    pending_query_ids: Sequence[str],
    quarantined_query_ids: Sequence[str],
    split_query_ids: Sequence[str],
    stage_evidence_receipts: Sequence[Path],
    split_receipt_paths: Sequence[Path],
    dispositioned_query_ids: Sequence[str] = (),
    read_only_progress: bool = False,
    source_checkpoint_authority: object | None = None,
) -> dict[str, object]:
    from mlb_backtest.v52_evidence import V52EvidenceStore, logical_query_id
    from mlb_backtest.v52_query_plan import (
        load_query_batch_for_authority,
        load_split_receipt,
    )
    if stage not in ACQUISITION_STAGES:
        raise ValueError("acquisition checkpoint stage differs")
    base = registry_payloads[0]
    run_binding = dict(
        _validate_binding_value(base["run_spec"], "checkpoint RunSpec")
    )
    contract_binding = dict(
        _validate_binding_value(
            base["acquisition_contract"], "checkpoint acquisition contract"
        )
    )
    batches: list[dict[str, object]] = []
    loaded_batch_sequence: list[object] = []
    loaded_batches: dict[str, object] = {}
    batch_query_ids: dict[str, set[str]] = {}
    queries: dict[str, object] = {}
    prior_stage_index = -1
    for raw_batch_id in query_batch_ids:
        if not isinstance(raw_batch_id, str) or not _SHA256.fullmatch(raw_batch_id):
            raise ValueError("checkpoint batch ID differs")
        if source_checkpoint_authority is not None:
            with _using_source_checkpoint_authority(
                source_checkpoint_authority
            ):
                batch = load_query_batch_for_authority(
                    paths.root,
                    raw_batch_id,
                    run_spec,
                    read_only_progress=read_only_progress,
                )
        else:
            batch = load_query_batch_for_authority(
                paths.root,
                raw_batch_id,
                run_spec,
                read_only_progress=read_only_progress,
            )
        batch_index = ACQUISITION_STAGES.index(batch.stage)
        if batch_index < prior_stage_index or batch_index > ACQUISITION_STAGES.index(stage):
            raise ValueError("checkpoint batch stage order differs")
        prior_stage_index = batch_index
        relative = (
            _authority_namespace(run_spec, "audit")
            / "acquisition/query_batches"
            / f"{batch.batch_id}.json"
        )
        binding = _checkpoint_artifact_binding(
            paths.root, relative, "checkpoint query batch"
        )
        batches.append(
            {"batch_id": batch.batch_id, "stage": batch.stage, **binding}
        )
        loaded_batch_sequence.append(batch)
        loaded_batches[batch.batch_id] = batch
        batch_query_ids[batch.batch_id] = set()
        for query in batch.queries:
            query_id = logical_query_id(query)
            if query_id in queries:
                raise ValueError("checkpoint query appears in multiple batches")
            if (
                query.run_spec_sha256 != run_binding["sha256"]
                or query.acquisition_contract_sha256 != contract_binding["sha256"]
            ):
                raise ValueError("checkpoint query run or contract binding differs")
            queries[query_id] = query
            batch_query_ids[batch.batch_id].add(query_id)
    if not batches or batches[0]["stage"] != "STATIC_SCHEDULES":
        raise ValueError("checkpoint must bind the STATIC_SCHEDULES root batch")
    if stage != "TIMEZONE_REVIEW" and not any(
        batch["stage"] == stage for batch in batches
    ):
        raise ValueError(f"checkpoint {stage} stage lacks its batch")
    authoritative_batches, authoritative_stage_evidence = _checkpoint_stage_authority(
            paths,
            run_spec,
            stage=stage,
            run_binding=run_binding,
            contract_binding=contract_binding,
            read_only_progress=read_only_progress,
            source_checkpoint_authority=source_checkpoint_authority,
        )
    if stage == "REMAINING":
        authority_matches = (
            tuple(loaded_batch_sequence[: len(authoritative_batches)])
            == authoritative_batches
        )
    else:
        authority_matches = tuple(loaded_batch_sequence) == authoritative_batches
    if not authority_matches:
        raise ValueError(f"checkpoint {stage} batch authority differs")

    completed = tuple(sorted(completed_query_ids))
    pending = tuple(sorted(pending_query_ids))
    quarantined = tuple(sorted(quarantined_query_ids))
    split = tuple(sorted(split_query_ids))
    dispositioned = tuple(sorted(dispositioned_query_ids))
    populations = (completed, pending, quarantined, split)
    if any(len(values) != len(set(values)) for values in populations):
        raise ValueError("checkpoint query status contains duplicates")
    union = set().union(*(set(values) for values in populations))
    if sum(len(values) for values in populations) != len(union) or union != set(queries):
        raise ValueError("checkpoint query status partition differs from batches")
    recovery_bindings: tuple[Mapping[str, object], ...] = ()
    recovery_authorities: Mapping[str, object] = {}
    if _is_recovery_source_run(run_spec):
        if dispositioned:
            recovery_bindings, recovery_authorities = (
                _recovery_disposition_catalog_bindings(
                    paths, run_spec, run_binding, contract_binding
                )
            )
        _validate_recovery_checkpoint_population(
            stage=stage,
            completed_query_ids=completed,
            pending_query_ids=pending,
            quarantined_query_ids=quarantined,
            split_query_ids=split,
            dispositioned_query_ids=dispositioned,
            disposition_bindings=recovery_bindings,
        )
    elif isinstance(run_spec, SourceRunSpec):
        _validate_source_disposition_population(
            completed_query_ids=completed,
            pending_query_ids=pending,
            quarantined_query_ids=quarantined,
            split_query_ids=split,
            dispositioned_query_ids=dispositioned,
        )
        if _OLD_QUARANTINED_QUERY_ID in union | set(dispositioned):
            raise ValueError("source checkpoint contains old query artifact binding")
    if stage == "REFERENCE_INPUTS":
        static_ids = batch_query_ids[str(batches[0]["batch_id"])]
        if not static_ids <= set(completed) or split:
            raise ValueError(
                "REFERENCE_INPUTS requires a complete canonical STATIC prefix"
            )
    if stage == "TIMEZONE_REVIEW" and (
        set(completed) != set(queries)
        or pending
        or quarantined
        or split
        or split_receipt_paths
    ):
        raise ValueError(
            "TIMEZONE_REVIEW requires terminal completed STATIC and REFERENCE queries"
        )
    if stage == "PARTICIPANT_DERIVED":
        prefix_ids = set().union(
            *(batch_query_ids[str(batch["batch_id"])] for batch in batches[:2])
        )
        if (
            not prefix_ids <= set(completed)
            or split
            or split_receipt_paths
        ):
            raise ValueError(
                "PARTICIPANT_DERIVED requires a terminal prefix and forbids splits"
            )
    expected_split_paths = {
        _authority_namespace(run_spec, "audit") / "acquisition/split_receipts"
        / f"{parent_id}.json"
        for parent_id in split
    }
    supplied_split_paths = {
        _relative_path(paths.root, supplied, "checkpoint split receipt")
        for supplied in split_receipt_paths
    }
    if supplied_split_paths != expected_split_paths:
        raise ValueError("checkpoint split receipt namespace differs")
    evidence_root = _checkpoint_evidence_root(paths, run_spec)
    split_bindings: list[dict[str, object]] = []
    loaded_split_receipts: list[object] = []
    for parent_id in split:
        relative = (
            _authority_namespace(run_spec, "audit")
            / "acquisition/split_receipts"
            / f"{parent_id}.json"
        )
        receipt = load_split_receipt(
            paths.root,
            parent_id,
            evidence_root=evidence_root,
            read_only_progress=read_only_progress,
        )
        if receipt is None:
            raise ValueError("checkpoint split receipt is missing")
        loaded_split_receipts.append(receipt)
        artifact = _capture_artifact(
            paths.root, paths.root / relative, "checkpoint split receipt"
        )
        if receipt.receipt_sha256 != artifact.sha256:
            raise ValueError("checkpoint split receipt hash differs")
        split_bindings.append(
            {
                "parent_query_id": parent_id,
                "path": artifact.path,
                "sha256": artifact.sha256,
                "split_receipt_id": receipt.split_receipt_id,
            }
        )
    split_bindings.sort(key=lambda item: str(item["parent_query_id"]))
    if {str(item["parent_query_id"]) for item in split_bindings} != set(split):
        raise ValueError("checkpoint split population differs from receipts")
    if stage == "REMAINING":
        active_leaf_ids = _checkpoint_split_dag_active_leaves(
            tuple(
                (
                    batch.batch_id,  # type: ignore[attr-defined]
                    batch.stage,  # type: ignore[attr-defined]
                    batch.derived_from_sha256,  # type: ignore[attr-defined]
                    tuple(
                        logical_query_id(query)
                        for query in batch.queries  # type: ignore[attr-defined]
                    ),
                )
                for batch in loaded_batch_sequence
            ),
            tuple(
                (
                    receipt.split_receipt_id,  # type: ignore[attr-defined]
                    receipt.parent_batch_id,  # type: ignore[attr-defined]
                    receipt.parent_query_id,  # type: ignore[attr-defined]
                    receipt.child_batch_id,  # type: ignore[attr-defined]
                    tuple(receipt.child_query_ids),  # type: ignore[attr-defined]
                )
                for receipt in loaded_split_receipts
            ),
            split,
        )
        prefix_ids = set().union(
            *(batch_query_ids[str(batch["batch_id"])] for batch in batches[:3])
        )
        _validate_checkpoint_remaining_population(
            active_leaf_ids=active_leaf_ids,
            prefix_ids=prefix_ids,
            completed_query_ids=set(completed),
            pending_query_ids=set(pending),
            quarantined_query_ids=set(quarantined),
        )

    namespaces = run_spec.raw["namespaces"]
    assert isinstance(namespaces, Mapping)
    raw_root = evidence_root
    store = (
        (
            V52EvidenceStore.open_read_only(raw_root)
            if read_only_progress
            else V52EvidenceStore(raw_root)
        )
        if raw_root.is_dir()
        else None
    )
    disposition_authority = None
    if isinstance(run_spec, SourceRunSpec) and _DISPOSITIONED_QUERY_ID in queries:
        disposition_authority = _source_disposition_authority(
            paths, run_spec, run_binding, contract_binding
        )
        if queries[_DISPOSITIONED_QUERY_ID] != disposition_authority.query:
            raise ValueError("source disposition registered query differs")
    if (
        not _is_recovery_source_run(run_spec)
        and dispositioned
        and (
        dispositioned != (_DISPOSITIONED_QUERY_ID,)
        or _DISPOSITIONED_QUERY_ID not in queries
        or _DISPOSITIONED_QUERY_ID not in set(completed)
        )
    ):
        raise ValueError("source disposition query population differs")
    evidence: list[dict[str, object]] = []
    for query_id, query in sorted(queries.items()):
        if store is None:
            actual_status = "PENDING"
            attempt_receipts: tuple[Mapping[str, object], ...] = ()
            published = None
            quarantines = ()
        else:
            progress = store.read_verified_query_progress(
                query,
                authority=(
                    recovery_authorities.get(query_id)
                    if _is_recovery_source_run(run_spec)
                    else (
                    disposition_authority
                    if query_id == _DISPOSITIONED_QUERY_ID
                    else None
                    )
                ),
            )  # type: ignore[arg-type]
            actual_status = progress.status
            attempt_receipts = progress.attempt_receipts
            published = progress.published_evidence
            quarantines = progress.quarantines
        if query_id in completed:
            expected_status = "SUCCESS"
        elif query_id in quarantined:
            expected_status = "QUARANTINED"
        else:
            expected_status = "PENDING"
        if actual_status != expected_status:
            raise ValueError("checkpoint query status differs from verified evidence")
        if query_id in split and (
            not attempt_receipts
            or attempt_receipts[-1].get("outcome") != "SPLIT_REQUIRED"
        ):
            raise ValueError("checkpoint split status lacks a split-required attempt")
        source = query.source  # type: ignore[attr-defined]
        for attempt in attempt_receipts:
            number = int(attempt["attempt_number"])
            relative = Path(str(namespaces["raw"])) / "queries" / source / query_id / "attempts" / f"{number:06d}.json"
            binding = _checkpoint_artifact_binding(paths.root, relative, "checkpoint attempt receipt")
            evidence.append({"query_id": query_id, "receipt_kind": "ATTEMPT", **binding})
        if published is not None:
            relative = _relative_path(paths.root, published.success_receipt_path, "checkpoint success receipt")
            binding = _checkpoint_artifact_binding(paths.root, relative, "checkpoint success receipt")
            evidence.append({"query_id": query_id, "receipt_kind": "SUCCESS", **binding})
        for item in quarantines:
            relative = _relative_path(paths.root, item.receipt_path, "checkpoint quarantine receipt")
            binding = _checkpoint_artifact_binding(paths.root, relative, "checkpoint quarantine receipt")
            evidence.append({"query_id": query_id, "receipt_kind": "QUARANTINE", **binding})
    evidence.sort(key=lambda item: (str(item["query_id"]), str(item["receipt_kind"]), str(item["path"])))

    stage_evidence = [
        _capture_artifact(paths.root, supplied, "checkpoint stage evidence").binding
        for supplied in stage_evidence_receipts
    ]
    stage_evidence.sort(key=lambda item: item["path"])
    if len({item["path"] for item in stage_evidence}) != len(stage_evidence):
        raise ValueError("checkpoint stage evidence is duplicated")
    if tuple(stage_evidence) != authoritative_stage_evidence:
        raise ValueError(f"checkpoint {stage} stage evidence authority differs")

    acquiring_payload = registry_payloads[1] if len(registry_payloads) > 1 else None
    if acquiring_payload is None or acquiring_payload.get("phase") != "ACQUIRING":
        raise ValueError("checkpoint requires the authenticated ACQUIRING receipt")
    acquiring_contents = canonical_json_bytes(acquiring_payload) + b"\n"
    acquiring_sha = _sha256_bytes(acquiring_contents)
    acquiring_relative = paths.receipts_relative / f"000001-ACQUIRING-{acquiring_sha}.json"
    execution_receipt = _checkpoint_artifact_binding(
        paths.root, acquiring_relative, "checkpoint ACQUIRING receipt"
    )
    return {
        "acquisition_contract": contract_binding,
        "completed_query_ids": list(completed),
        **(
            {"dispositioned_query_ids": list(dispositioned)}
            if isinstance(run_spec, SourceRunSpec)
            else {}
        ),
        **(
            {
                "recovery_disposition_bindings": [
                    dict(item) for item in recovery_bindings
                ]
            }
            if _is_recovery_source_run(run_spec)
            else {}
        ),
        "execution_phase": "ACQUIRING",
        "execution_phase_receipt": execution_receipt,
        "git_head": git_snapshot.head,
        "pending_query_ids": list(pending),
        "quarantined_query_ids": list(quarantined),
        "query_batches": batches,
        "query_evidence_receipts": evidence,
        "receipt_kind": "ACQUISITION_CHECKPOINT",
        "run_id": run_spec.run_id,
        "run_spec": run_binding,
        "schema_version": 1,
        "split_query_ids": list(split),
        "split_receipts": split_bindings,
        "stage": stage,
        "stage_evidence_receipts": stage_evidence,
    }


def _validate_checkpoint_name_chain(names: Sequence[str]) -> None:
    for expected_sequence, name in enumerate(names):
        match = _CHECKPOINT_NAME.fullmatch(name)
        if match is None or int(match.group("sequence")) != expected_sequence:
            raise _ReceiptMembershipError(
                "acquisition checkpoint chain has a gap, fork, or unknown entry"
            )


def _checkpoint_artifacts_and_population(
    paths: _RegistryPaths,
    run_spec: Authority,
    payload: Mapping[str, object],
    lock_binding: _RegistryLockBinding,
    *,
    read_only_progress: bool = False,
) -> set[str]:
    from mlb_backtest.v52_evidence import V52EvidenceStore, logical_query_id
    from mlb_backtest.v52_query_plan import (
        load_query_batch_for_authority,
        load_split_receipt,
    )

    stage = str(payload["stage"])
    if stage not in _OPEN_CHECKPOINT_STAGES:
        raise ValueError(
            "non-static checkpoint stage authority is unavailable until Task7D"
        )
    query_ids: set[str] = set()
    queries_by_id: dict[str, object] = {}
    evidence_root = _checkpoint_evidence_root(paths, run_spec)
    batch_query_ids: dict[str, set[str]] = {}
    batches = payload["query_batches"]
    assert isinstance(batches, list)
    source_checkpoint_authority = (
        _issue_source_checkpoint_authority(payload, run_spec, lock_binding)
        if isinstance(run_spec, SourceRunSpec)
        else None
    )
    loaded_batch_sequence: list[object] = []
    for item in batches:
        assert isinstance(item, Mapping)
        batch_id = str(item["batch_id"])
        if source_checkpoint_authority is not None:
            with _using_source_checkpoint_authority(source_checkpoint_authority):
                batch = load_query_batch_for_authority(
                    paths.root,
                    batch_id,
                    run_spec,
                    read_only_progress=read_only_progress,
                )
        else:
            batch = load_query_batch_for_authority(
                paths.root,
                batch_id,
                run_spec,
                read_only_progress=read_only_progress,
            )
        if batch.stage != item["stage"]:
            raise ValueError("checkpoint batch stage binding differs")
        loaded_batch_sequence.append(batch)
        expected_relative = (
            _authority_namespace(run_spec, "audit")
            / "acquisition/query_batches"
            / f"{batch_id}.json"
        )
        relative = Path(str(item["path"]))
        if relative != expected_relative:
            raise ValueError("checkpoint query batch namespace differs")
        actual = _checkpoint_artifact_binding(
            paths.root, relative, "checkpoint query batch"
        )
        if actual != {"path": item["path"], "sha256": item["sha256"]}:
            raise ValueError("checkpoint query batch hash binding differs")
        batch_query_ids[batch_id] = set()
        for query in batch.queries:
            query_id = logical_query_id(query)
            if query_id in query_ids:
                raise ValueError("checkpoint query appears in multiple batches")
            query_ids.add(query_id)
            queries_by_id[query_id] = query
            batch_query_ids[batch_id].add(query_id)
    run_binding = payload["run_spec"]
    contract_binding = payload["acquisition_contract"]
    assert isinstance(run_binding, Mapping)
    assert isinstance(contract_binding, Mapping)
    populations = set().union(
        set(payload["completed_query_ids"]),
        set(payload["pending_query_ids"]),
        set(payload["quarantined_query_ids"]),
        set(payload["split_query_ids"]),
    )
    if populations != query_ids:
        raise ValueError("checkpoint query status partition differs from batches")
    dispositioned = tuple(payload.get("dispositioned_query_ids", ()))
    recovery_authorities: Mapping[str, object] = {}
    if _is_recovery_source_run(run_spec):
        serialized_bindings = _validate_recovery_disposition_bindings(
            payload["recovery_disposition_bindings"]
        )
        expected_bindings: tuple[Mapping[str, object], ...] = ()
        if dispositioned:
            expected_bindings, recovery_authorities = (
                _recovery_disposition_catalog_bindings(
                    paths, run_spec, run_binding, contract_binding
                )
            )
            if tuple(dict(item) for item in serialized_bindings) != expected_bindings:
                raise ValueError(
                    "recovery checkpoint disposition catalog binding differs"
                )
        _validate_recovery_checkpoint_population(
            stage=stage,
            completed_query_ids=payload["completed_query_ids"],  # type: ignore[arg-type]
            pending_query_ids=payload["pending_query_ids"],  # type: ignore[arg-type]
            quarantined_query_ids=payload["quarantined_query_ids"],  # type: ignore[arg-type]
            split_query_ids=payload["split_query_ids"],  # type: ignore[arg-type]
            dispositioned_query_ids=dispositioned,
            disposition_bindings=expected_bindings,
        )
    elif isinstance(run_spec, SourceRunSpec):
        _validate_source_disposition_population(
            completed_query_ids=payload["completed_query_ids"],  # type: ignore[arg-type]
            pending_query_ids=payload["pending_query_ids"],  # type: ignore[arg-type]
            quarantined_query_ids=payload["quarantined_query_ids"],  # type: ignore[arg-type]
            split_query_ids=payload["split_query_ids"],  # type: ignore[arg-type]
            dispositioned_query_ids=dispositioned,
        )
    disposition_authority = None
    if isinstance(run_spec, SourceRunSpec) and _DISPOSITIONED_QUERY_ID in queries_by_id:
        disposition_authority = _source_disposition_authority(
            paths, run_spec, run_binding, contract_binding
        )
        if queries_by_id[_DISPOSITIONED_QUERY_ID] != disposition_authority.query:
            raise ValueError("source disposition registered query differs")
    if (
        not _is_recovery_source_run(run_spec)
        and dispositioned
        and (
        dispositioned != (_DISPOSITIONED_QUERY_ID,)
        or _DISPOSITIONED_QUERY_ID not in queries_by_id
        or _DISPOSITIONED_QUERY_ID not in set(payload["completed_query_ids"])
        )
    ):
        raise ValueError("source disposition query population differs")
    authoritative_batches, authoritative_stage_evidence = _checkpoint_stage_authority(
            paths,
            run_spec,
            stage=stage,
            run_binding=run_binding,
            contract_binding=contract_binding,
            read_only_progress=read_only_progress,
        source_checkpoint_authority=(source_checkpoint_authority),
    )
    if stage == "REMAINING":
        authority_matches = (
            tuple(loaded_batch_sequence[: len(authoritative_batches)])
            == authoritative_batches
        )
    else:
        authority_matches = tuple(loaded_batch_sequence) == authoritative_batches
    if not authority_matches:
        raise ValueError(f"checkpoint {stage} batch authority differs")
    if stage == "REFERENCE_INPUTS":
        static_batch_id = str(batches[0]["batch_id"])
        if (
            not batch_query_ids[static_batch_id]
            <= set(payload["completed_query_ids"])
            or payload["split_query_ids"]
            or payload["split_receipts"]
        ):
            raise ValueError(
                "REFERENCE_INPUTS requires a complete canonical STATIC prefix"
            )
    if stage == "TIMEZONE_REVIEW" and (
        set(payload["completed_query_ids"]) != query_ids
        or payload["pending_query_ids"]
        or payload["quarantined_query_ids"]
        or payload["split_query_ids"]
        or payload["split_receipts"]
    ):
        raise ValueError(
            "TIMEZONE_REVIEW requires terminal completed STATIC and REFERENCE queries"
        )
    if stage == "PARTICIPANT_DERIVED":
        prefix_ids = set().union(
            *(batch_query_ids[str(batch["batch_id"])] for batch in batches[:2])
        )
        if (
            not prefix_ids <= set(payload["completed_query_ids"])
            or payload["split_query_ids"]
            or payload["split_receipts"]
        ):
            raise ValueError(
                "PARTICIPANT_DERIVED requires a terminal prefix and forbids splits"
            )
    evidence = payload["query_evidence_receipts"]
    assert isinstance(evidence, list)
    verified_evidence: set[tuple[str, str, str, str]] = set()
    raw_root = _checkpoint_evidence_root(paths, run_spec)
    if raw_root.is_dir():
        store = (
            V52EvidenceStore.open_read_only(raw_root)
            if read_only_progress
            else V52EvidenceStore(raw_root)
        )
        namespaces = run_spec.raw["namespaces"]
        assert isinstance(namespaces, Mapping)
        for query_id, query in sorted(queries_by_id.items()):
            progress = store.read_verified_query_progress(
                query,
                authority=(
                    recovery_authorities.get(query_id)
                    if _is_recovery_source_run(run_spec)
                    else (
                    disposition_authority
                    if query_id == _DISPOSITIONED_QUERY_ID
                    else None
                    )
                ),
            )  # type: ignore[arg-type]
            source = query.source  # type: ignore[attr-defined]
            for attempt in progress.attempt_receipts:
                number = int(attempt["attempt_number"])
                relative = (
                    Path(str(namespaces["raw"]))
                    / "queries"
                    / source
                    / query_id
                    / "attempts"
                    / f"{number:06d}.json"
                )
                binding = _checkpoint_artifact_binding(
                    paths.root, relative, "checkpoint verified attempt receipt"
                )
                verified_evidence.add(
                    (query_id, "ATTEMPT", binding["path"], binding["sha256"])
                )
            if progress.published_evidence is not None:
                relative = _relative_path(
                    paths.root,
                    progress.published_evidence.success_receipt_path,
                    "checkpoint verified success receipt",
                )
                binding = _checkpoint_artifact_binding(
                    paths.root, relative, "checkpoint verified success receipt"
                )
                verified_evidence.add(
                    (query_id, "SUCCESS", binding["path"], binding["sha256"])
                )
            for quarantine in progress.quarantines:
                relative = _relative_path(
                    paths.root,
                    quarantine.receipt_path,
                    "checkpoint verified quarantine receipt",
                )
                binding = _checkpoint_artifact_binding(
                    paths.root, relative, "checkpoint verified quarantine receipt"
                )
                verified_evidence.add(
                    (
                        query_id,
                        "QUARANTINE",
                        binding["path"],
                        binding["sha256"],
                    )
                )
    success_ids: set[str] = set()
    quarantine_ids: set[str] = set()
    for item in evidence:
        assert isinstance(item, Mapping)
        if item["query_id"] not in query_ids:
            raise ValueError("checkpoint evidence query is outside its batches")
        identity = (
            str(item["query_id"]),
            str(item["receipt_kind"]),
            str(item["path"]),
            str(item["sha256"]),
        )
        if identity not in verified_evidence:
            raise ValueError(
                "checkpoint query evidence is not a verified canonical receipt"
            )
        if item["receipt_kind"] == "SUCCESS":
            success_ids.add(str(item["query_id"]))
        elif item["receipt_kind"] == "QUARANTINE":
            quarantine_ids.add(str(item["query_id"]))
        actual = _checkpoint_artifact_binding(
            paths.root, Path(str(item["path"])), "checkpoint query evidence"
        )
        if actual != {"path": item["path"], "sha256": item["sha256"]}:
            raise ValueError("checkpoint query evidence hash binding differs")
    if (
        success_ids != set(payload["completed_query_ids"])
        or quarantine_ids - success_ids
        != set(payload["quarantined_query_ids"])
    ):
        raise ValueError(
            "checkpoint terminal status differs from bound query evidence"
        )
    loaded_split_receipts: list[object] = []
    for key, label in (
        ("stage_evidence_receipts", "checkpoint stage evidence"),
        ("split_receipts", "checkpoint split receipt"),
    ):
        values = payload[key]
        assert isinstance(values, list)
        for item in values:
            assert isinstance(item, Mapping)
            actual = _checkpoint_artifact_binding(
                paths.root, Path(str(item["path"])), label
            )
            if actual != {"path": item["path"], "sha256": item["sha256"]}:
                raise ValueError(f"{label} hash binding differs")
            if key == "split_receipts":
                parent_id = str(item["parent_query_id"])
                expected_path = (
                    _authority_namespace(run_spec, "audit")
                    / "acquisition/split_receipts"
                    / f"{parent_id}.json"
                ).as_posix()
                receipt = load_split_receipt(
                    paths.root,
                    parent_id,
                    evidence_root=evidence_root,
                    read_only_progress=read_only_progress,
                )
                if (
                    str(item["path"]) != expected_path
                    or receipt is None
                    or receipt.receipt_sha256 != item["sha256"]
                    or receipt.split_receipt_id != item["split_receipt_id"]
                    or receipt.parent_query_id != parent_id
                ):
                    raise ValueError("checkpoint split receipt child graph differs")
                loaded_split_receipts.append(receipt)
    if stage == "REMAINING":
        active_leaf_ids = _checkpoint_split_dag_active_leaves(
            tuple(
                (
                    batch.batch_id,  # type: ignore[attr-defined]
                    batch.stage,  # type: ignore[attr-defined]
                    batch.derived_from_sha256,  # type: ignore[attr-defined]
                    tuple(
                        logical_query_id(query)
                        for query in batch.queries  # type: ignore[attr-defined]
                    ),
                )
                for batch in loaded_batch_sequence
            ),
            tuple(
                (
                    receipt.split_receipt_id,  # type: ignore[attr-defined]
                    receipt.parent_batch_id,  # type: ignore[attr-defined]
                    receipt.parent_query_id,  # type: ignore[attr-defined]
                    receipt.child_batch_id,  # type: ignore[attr-defined]
                    tuple(receipt.child_query_ids),  # type: ignore[attr-defined]
                )
                for receipt in loaded_split_receipts
            ),
            payload["split_query_ids"],  # type: ignore[arg-type]
        )
        prefix_ids = set().union(
            *(batch_query_ids[str(batch["batch_id"])] for batch in batches[:3])
        )
        pending_ids = set(payload["pending_query_ids"])
        quarantined_ids = set(payload["quarantined_query_ids"])
        _validate_checkpoint_remaining_population(
            active_leaf_ids=active_leaf_ids,
            prefix_ids=prefix_ids,
            completed_query_ids=set(payload["completed_query_ids"]),
            pending_query_ids=pending_ids,
            quarantined_query_ids=quarantined_ids,
        )
    stage_evidence = payload["stage_evidence_receipts"]
    assert isinstance(stage_evidence, list)
    if tuple(dict(item) for item in stage_evidence) != authoritative_stage_evidence:
        raise ValueError(f"checkpoint {stage} stage evidence authority differs")
    for key, label in (
        ("run_spec", "checkpoint RunSpec"),
        ("acquisition_contract", "checkpoint acquisition contract"),
        ("execution_phase_receipt", "checkpoint execution receipt"),
    ):
        item = payload[key]
        assert isinstance(item, Mapping)
        actual = _checkpoint_artifact_binding(
            paths.root, Path(str(item["path"])), label
        )
        if actual != dict(item):
            raise ValueError(f"{label} hash binding differs")
    return query_ids


def _validate_checkpoint_progression(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
    run_spec: Authority | None = None,
) -> None:
    if isinstance(run_spec, SourceRunSpec):
        if _is_recovery_source_run(run_spec):
            _validate_recovery_checkpoint_population(
                stage=current["stage"],
                completed_query_ids=current["completed_query_ids"],  # type: ignore[arg-type]
                pending_query_ids=current["pending_query_ids"],  # type: ignore[arg-type]
                quarantined_query_ids=current["quarantined_query_ids"],  # type: ignore[arg-type]
                split_query_ids=current["split_query_ids"],  # type: ignore[arg-type]
                dispositioned_query_ids=current["dispositioned_query_ids"],  # type: ignore[arg-type]
                disposition_bindings=current["recovery_disposition_bindings"],  # type: ignore[arg-type]
            )
        else:
            _validate_source_disposition_population(
                completed_query_ids=current["completed_query_ids"],  # type: ignore[arg-type]
                pending_query_ids=current["pending_query_ids"],  # type: ignore[arg-type]
                quarantined_query_ids=current["quarantined_query_ids"],  # type: ignore[arg-type]
                split_query_ids=current["split_query_ids"],  # type: ignore[arg-type]
                dispositioned_query_ids=current["dispositioned_query_ids"],  # type: ignore[arg-type]
            )
        if previous is not None:
            if _is_recovery_source_run(run_spec):
                _validate_recovery_checkpoint_population(
                    stage=previous["stage"],
                    completed_query_ids=previous["completed_query_ids"],  # type: ignore[arg-type]
                    pending_query_ids=previous["pending_query_ids"],  # type: ignore[arg-type]
                    quarantined_query_ids=previous["quarantined_query_ids"],  # type: ignore[arg-type]
                    split_query_ids=previous["split_query_ids"],  # type: ignore[arg-type]
                    dispositioned_query_ids=previous["dispositioned_query_ids"],  # type: ignore[arg-type]
                    disposition_bindings=previous["recovery_disposition_bindings"],  # type: ignore[arg-type]
                )
            else:
                _validate_source_disposition_population(
                    completed_query_ids=previous["completed_query_ids"],  # type: ignore[arg-type]
                    pending_query_ids=previous["pending_query_ids"],  # type: ignore[arg-type]
                    quarantined_query_ids=previous["quarantined_query_ids"],  # type: ignore[arg-type]
                    split_query_ids=previous["split_query_ids"],  # type: ignore[arg-type]
                    dispositioned_query_ids=previous["dispositioned_query_ids"],  # type: ignore[arg-type]
                )
        current_stage = str(current["stage"])
        if current_stage not in ACQUISITION_STAGES:
            raise ValueError("superseding source checkpoint stage differs")
        current_batches = current["query_batches"]
        assert isinstance(current_batches, list)
        if current_stage != "TIMEZONE_REVIEW" and not any(
            isinstance(item, Mapping) and item.get("stage") == current_stage
            for item in current_batches
        ):
            raise ValueError(
                f"source checkpoint {current_stage} stage lacks its batch"
            )
        if previous is None:
            if current_stage != "STATIC_SCHEDULES":
                raise ValueError("first source acquisition checkpoint stage differs")
            return
        if previous is not None:
            previous_stage = str(previous["stage"])
            if previous_stage not in ACQUISITION_STAGES or (
                ACQUISITION_STAGES.index(current_stage)
                not in {
                    ACQUISITION_STAGES.index(previous_stage),
                    ACQUISITION_STAGES.index(previous_stage) + 1,
                }
            ):
                raise ValueError("superseding source checkpoint stage regressed")
            previous_batches = previous["query_batches"]
            assert isinstance(previous_batches, list) and isinstance(current_batches, list)
            if current_batches[: len(previous_batches)] != previous_batches:
                raise ValueError("source checkpoint batch history is not cumulative")
            if current_stage == "TIMEZONE_REVIEW" and previous_stage != "REFERENCE_INPUTS":
                raise ValueError(
                    "TIMEZONE_REVIEW requires the terminal REFERENCE_INPUTS checkpoint"
                )
            if current_stage == "PARTICIPANT_DERIVED" and previous_stage not in {
                "TIMEZONE_REVIEW",
                "PARTICIPANT_DERIVED",
            }:
                raise ValueError(
                    "PARTICIPANT_DERIVED requires the terminal TIMEZONE_REVIEW checkpoint"
                )
            if current_stage == "REMAINING" and previous_stage not in {
                "PARTICIPANT_DERIVED",
                "REMAINING",
            }:
                raise ValueError(
                    "REMAINING requires the terminal PARTICIPANT_DERIVED checkpoint"
                )
            if ACQUISITION_STAGES.index(current_stage) > ACQUISITION_STAGES.index(
                previous_stage
            ) and (
                previous["pending_query_ids"]
                or previous["quarantined_query_ids"]
                or previous["split_query_ids"]
            ):
                raise ValueError("source acquisition checkpoint stage is not fully verified")
            if (
                ACQUISITION_STAGES.index(current_stage)
                > ACQUISITION_STAGES.index(previous_stage)
                and current_stage != "TIMEZONE_REVIEW"
                and not any(
                    isinstance(item, Mapping) and item.get("stage") == current_stage
                    for item in current_batches[len(previous_batches) :]
                )
            ):
                raise ValueError(
                    f"source checkpoint {current_stage} stage lacks a new batch"
                )
            for key in (
                "query_evidence_receipts",
                "stage_evidence_receipts",
                "split_receipts",
            ):
                old = {
                    canonical_json_bytes(dict(item))
                    for item in previous.get(key, ())
                }
                new = {
                    canonical_json_bytes(dict(item))
                    for item in current.get(key, ())
                }
                if not old <= new:
                    raise ValueError("source checkpoint evidence history regressed")
            for key in (
                "completed_query_ids",
                "quarantined_query_ids",
                "split_query_ids",
                "dispositioned_query_ids",
            ):
                if not set(previous.get(key, ())) <= set(current.get(key, ())):
                    raise ValueError("source checkpoint terminal status regressed")
            if str(current["recorded_at_utc"]) < str(previous["recorded_at_utc"]):
                raise ValueError("source acquisition checkpoint trusted UTC regressed")
        return
    current_stage = str(current["stage"])
    current_batches = current["query_batches"]
    assert isinstance(current_batches, list)
    if current_stage != "TIMEZONE_REVIEW" and not any(
        isinstance(item, Mapping) and item.get("stage") == current_stage
        for item in current_batches
    ):
        raise ValueError(
            f"acquisition checkpoint {current_stage} stage lacks its batch"
        )
    evidence_paths = {
        str(item["path"])
        for item in current["stage_evidence_receipts"]  # type: ignore[union-attr]
    }
    if current_stage == "REFERENCE_INPUTS" and (
        "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
        not in evidence_paths
    ):
        raise ValueError(
            "REFERENCE_INPUTS checkpoint lacks schedule anchor evidence"
        )
    if current_stage == "TIMEZONE_REVIEW":
        if evidence_paths != set(_timezone_stage_evidence_paths(run_spec)):
            raise ValueError(
                "TIMEZONE_REVIEW checkpoint lacks its sealed semantic gate evidence"
            )
    if current_stage == "PARTICIPANT_DERIVED":
        if evidence_paths != set(_PARTICIPANT_STAGE_EVIDENCE_PATHS):
            raise ValueError(
                "PARTICIPANT_DERIVED checkpoint lacks exact game inventory evidence"
            )
    if current_stage == "REMAINING" and (
        len(evidence_paths) != 10
        or not {
            *_PARTICIPANT_STAGE_EVIDENCE_PATHS,
            _PROTECTED_RAW_INVENTORY_PATH,
            _LEGACY_DECISION_INVENTORY_PATH,
        }
        <= evidence_paths
    ):
        raise ValueError("REMAINING checkpoint lacks exact planning lineage evidence")
    if previous is None:
        if current_stage != "STATIC_SCHEDULES":
            raise ValueError("first acquisition checkpoint stage differs")
        return
    previous_stage = ACQUISITION_STAGES.index(str(previous["stage"]))
    current_stage_index = ACQUISITION_STAGES.index(current_stage)
    if current_stage == "TIMEZONE_REVIEW" and previous["stage"] != "REFERENCE_INPUTS":
        raise ValueError(
            "TIMEZONE_REVIEW requires the terminal REFERENCE_INPUTS checkpoint"
        )
    if current_stage == "PARTICIPANT_DERIVED" and previous["stage"] not in {
        "TIMEZONE_REVIEW",
        "PARTICIPANT_DERIVED",
    }:
        raise ValueError(
            "PARTICIPANT_DERIVED requires the terminal TIMEZONE_REVIEW checkpoint"
        )
    if current_stage == "REMAINING" and previous["stage"] not in {
        "PARTICIPANT_DERIVED",
        "REMAINING",
    }:
        raise ValueError(
            "REMAINING requires the terminal PARTICIPANT_DERIVED checkpoint"
        )
    if current_stage_index not in {previous_stage, previous_stage + 1}:
        raise ValueError("acquisition checkpoint stage regressed or skipped")
    if current_stage_index > previous_stage and (
        previous["pending_query_ids"]
        or previous["quarantined_query_ids"]
        or previous["split_query_ids"]
    ):
        raise ValueError("acquisition checkpoint stage is not fully verified")
    prior_batches = previous["query_batches"]
    assert isinstance(prior_batches, list) and isinstance(current_batches, list)
    if current_batches[: len(prior_batches)] != prior_batches:
        raise ValueError("acquisition checkpoint batch history is not cumulative")
    if current_stage_index > previous_stage and current_stage != "TIMEZONE_REVIEW":
        new_batches = current_batches[len(prior_batches) :]
        if not any(
            isinstance(item, Mapping) and item.get("stage") == current_stage
            for item in new_batches
        ):
            raise ValueError(
                f"acquisition checkpoint {current_stage} stage lacks a new batch"
            )
    for key in (
        "query_evidence_receipts",
        "stage_evidence_receipts",
        "split_receipts",
    ):
        old = {canonical_json_bytes(item) for item in previous[key]}  # type: ignore[union-attr]
        new = {canonical_json_bytes(item) for item in current[key]}  # type: ignore[union-attr]
        if not old <= new:
            raise ValueError("acquisition checkpoint evidence history regressed")
    for key in (
        "completed_query_ids",
        "quarantined_query_ids",
        "split_query_ids",
    ):
        if not set(previous[key]) <= set(current[key]):  # type: ignore[arg-type]
            raise ValueError("acquisition checkpoint terminal status regressed")
    if str(current["recorded_at_utc"]) < str(previous["recorded_at_utc"]):
        raise ValueError("acquisition checkpoint trusted UTC regressed")


def _load_checkpoints_locked(
    paths: _RegistryPaths,
    run_spec: Authority,
    registry_payloads: Sequence[Mapping[str, object]],
    git_snapshot: _GitSnapshot,
    lock_binding: _RegistryLockBinding,
    *,
    verify_latest_progress: bool,
    read_only_progress: bool = False,
) -> tuple[
    tuple[AcquisitionCheckpoint, ...],
    tuple[Mapping[str, object], ...],
    tuple[str, ...],
]:
    checkpoint_child = _execution_child(paths, paths.checkpoints_relative)
    try:
        checkpoint_fd, descriptors = _open_directory_at(
            lock_binding.execution_descriptor, checkpoint_child
        )
    except FileNotFoundError:
        return (), (), ()
    try:
        names = tuple(sorted(os.listdir(checkpoint_fd)))
    finally:
        _close_directories(descriptors)
    _validate_checkpoint_name_chain(names)
    checkpoints: list[AcquisitionCheckpoint] = []
    payloads: list[Mapping[str, object]] = []
    checkpoint_snapshots: list[tuple[bytes, tuple[int, ...]]] = []
    prior_sha: str | None = None
    previous: Mapping[str, object] | None = None
    base = registry_payloads[0]
    for expected_sequence, name in enumerate(names):
        match = _CHECKPOINT_NAME.fullmatch(name)
        assert match is not None
        receipt_sha = match.group("sha")
        contents, checkpoint_identity = _read_relative_snapshot_at(
            lock_binding.execution_descriptor,
            checkpoint_child / name,
            "acquisition checkpoint",
        )
        if _sha256_bytes(contents) != receipt_sha:
            raise ValueError("acquisition checkpoint filename hash differs")
        payload = _validate_checkpoint_payload(
            _canonical_object(contents, "acquisition checkpoint"), run_spec
        )
        if (
            payload["sequence"] != expected_sequence
            or payload["prior_checkpoint_sha256"] != prior_sha
            or payload["run_id"] != run_spec.run_id
            or payload["run_spec"] != base["run_spec"]
            or payload["acquisition_contract"] != base["acquisition_contract"]
            or payload["git_head"] != git_snapshot.head
        ):
            raise ValueError("acquisition checkpoint chain or binding differs")
        _checkpoint_artifacts_and_population(
            paths,
            run_spec,
            payload,
            lock_binding,
            read_only_progress=read_only_progress,
        )
        _validate_checkpoint_progression(previous, payload, run_spec)
        checkpoints.append(_checkpoint_from_payload(payload, receipt_sha))
        payloads.append(payload)
        checkpoint_snapshots.append((contents, checkpoint_identity))
        prior_sha = receipt_sha
        previous = payload
    if names:
        checkpoint_fd, descriptors = _open_directory_at(
            lock_binding.execution_descriptor, checkpoint_child
        )
        try:
            if tuple(sorted(os.listdir(checkpoint_fd))) != names:
                raise _ReceiptMembershipError(
                    "acquisition checkpoint membership changed during load"
                )
        finally:
            _close_directories(descriptors)
    if payloads and verify_latest_progress:
        latest = payloads[-1]
        source_checkpoint_authority = None
        if isinstance(run_spec, SourceRunSpec):
            source_checkpoint_authority = _issue_source_checkpoint_authority(
                latest, run_spec, lock_binding
            )
        current_semantic = _checkpoint_current_semantic(
            paths,
            run_spec,
            registry_payloads,
            git_snapshot,
            stage=str(latest["stage"]),
            query_batch_ids=[
                str(item["batch_id"]) for item in latest["query_batches"]  # type: ignore[index]
            ],
            completed_query_ids=latest["completed_query_ids"],  # type: ignore[arg-type]
            pending_query_ids=latest["pending_query_ids"],  # type: ignore[arg-type]
            quarantined_query_ids=latest["quarantined_query_ids"],  # type: ignore[arg-type]
            split_query_ids=latest["split_query_ids"],  # type: ignore[arg-type]
            stage_evidence_receipts=[
                paths.root / str(item["path"])
                for item in latest["stage_evidence_receipts"]  # type: ignore[union-attr]
            ],
            split_receipt_paths=[
                paths.root / str(item["path"])
                for item in latest["split_receipts"]  # type: ignore[union-attr]
            ],
            dispositioned_query_ids=latest.get("dispositioned_query_ids", ()),
            read_only_progress=read_only_progress,
            source_checkpoint_authority=source_checkpoint_authority,
        )
        if _checkpoint_semantic_projection(latest) != current_semantic:
            raise ValueError("latest acquisition checkpoint differs from verified progress")
    if names:
        checkpoint_fd, descriptors = _open_directory_at(
            lock_binding.execution_descriptor, checkpoint_child
        )
        try:
            if tuple(sorted(os.listdir(checkpoint_fd))) != names:
                raise _ReceiptMembershipError(
                    "acquisition checkpoint membership changed during final rebind"
                )
        finally:
            _close_directories(descriptors)
        for name, snapshot in zip(names, checkpoint_snapshots):
            if (
                _read_relative_snapshot_at(
                lock_binding.execution_descriptor,
                checkpoint_child / name,
                "acquisition checkpoint final rebind",
                )
                != snapshot
            ):
                raise ValueError(
                    "acquisition checkpoint identity or bytes changed during replay"
                )
    return tuple(checkpoints), tuple(payloads), names


def _terminal_checkpoint_head(
    paths: _RegistryPaths,
    checkpoints: Sequence[AcquisitionCheckpoint],
    payloads: Sequence[Mapping[str, object]],
    names: Sequence[str],
) -> dict[str, object]:
    if not checkpoints or not payloads or len(checkpoints) != len(names):
        raise ValueError("SOURCES_READY requires an acquisition checkpoint chain")
    checkpoint = checkpoints[-1]
    payload = payloads[-1]
    if (
        checkpoint.stage != "REMAINING"
        or payload["pending_query_ids"]
        or payload["quarantined_query_ids"]
    ):
        raise ValueError(
            "SOURCES_READY requires a terminal REMAINING acquisition checkpoint"
        )
    expected_name = f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"
    if names[-1] != expected_name or len(names) != checkpoint.sequence + 1:
        raise ValueError("terminal acquisition checkpoint membership differs")
    relative = paths.checkpoints_relative / expected_name
    binding = _checkpoint_artifact_binding(
        paths.root,
        relative,
        "terminal acquisition checkpoint",
    )
    if binding["sha256"] != checkpoint.receipt_sha256:
        raise ValueError("terminal acquisition checkpoint hash differs")
    return {
        "count": len(names),
        "path": binding["path"],
        "sequence": checkpoint.sequence,
        "sha256": checkpoint.receipt_sha256,
        "stage": checkpoint.stage,
    }


def _require_source_terminal_disposition(
    authority: Authority,
    checkpoint: AcquisitionCheckpoint,
) -> None:
    if not isinstance(authority, SourceRunSpec):
        return
    if _is_recovery_source_run(authority):
        bindings = checkpoint.recovery_disposition_bindings
        expected = _validate_recovery_disposition_population(
            checkpoint.dispositioned_query_ids, bindings
        )
        if (
            checkpoint.stage != "REMAINING"
            or checkpoint.pending_query_ids
            or checkpoint.quarantined_query_ids
            or checkpoint.split_query_ids
            or any(
                query_id not in checkpoint.completed_query_ids for query_id in expected
            )
        ):
            raise ValueError(
                "recovery terminal checkpoint requires seven completed static queries"
            )
        return
    if (
        checkpoint.dispositioned_query_ids != (_DISPOSITIONED_QUERY_ID,)
        or _DISPOSITIONED_QUERY_ID not in checkpoint.completed_query_ids
        or _DISPOSITIONED_QUERY_ID in checkpoint.pending_query_ids
        or _DISPOSITIONED_QUERY_ID in checkpoint.quarantined_query_ids
        or _DISPOSITIONED_QUERY_ID in checkpoint.split_query_ids
    ):
        raise ValueError(
            "superseding SOURCES_READY requires the completed disposition query"
        )


def _require_source_inventory_terminal_checkpoint(
    authority: Authority,
    inventory: _ArtifactSnapshot,
    checkpoint_head: Mapping[str, object],
) -> None:
    """Bind the source inventory's selected checkpoint to the sealed head."""
    if not isinstance(authority, SourceRunSpec):
        return
    source_inventory = _source_inventory_payload(
        inventory.contents, run_id=authority.run_id
    )
    if source_inventory.terminal_checkpoint is None:
        raise ValueError("source inventory terminal checkpoint is missing")
    expected = {
        key: checkpoint_head[key]
        for key in ("path", "sha256", "sequence", "stage")
    }
    if dict(source_inventory.terminal_checkpoint) != expected:
        raise ValueError("source inventory terminal checkpoint differs")


def initialize_execution_registry(
    root: Path,
    run_spec: Authority,
    *,
    design_path: Path,
    plan_path: Path,
    acquisition_contract_path: Path,
) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    phases = _authority_phases(run_spec)
    evidence = _initial_evidence(paths.root, run_spec, design_path, plan_path, acquisition_contract_path)
    _ensure_registry_directories(paths)
    with _registry_lock(paths, create=True, exclusive=True) as lock_binding:
        receipt_fd, descriptors = _open_directory_at(
            lock_binding.execution_descriptor,
            _execution_child(paths, paths.receipts_relative),
        )
        try:
            existing = os.listdir(receipt_fd)
        finally:
            _close_directories(descriptors)
        if existing:
            state, payloads = _load_locked(paths, run_spec, lock_binding)
            expected = {key: evidence[key] for key in evidence}
            actual = {key: payloads[0][key] for key in evidence}
            if state.phase not in phases or actual != expected:
                raise ValueError("execution registry is already initialized and differs")
            return state
        protected = _exact_object(evidence["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage")["protected_inventory"]
        snapshot = _capture_git_snapshot(paths.root, evidence)
        protected_snapshot = _capture_artifact(
            paths.root,
            paths.root / str(protected["path"]),
            "phase inventory",
        )
        if protected_snapshot.binding != dict(protected):
            raise ValueError("protected inventory differs from trusted binding")
        payload = {
            "schema_version": 1,
            "receipt_kind": "PHASE_TRANSITION",
            "run_id": run_spec.run_id,
            "sequence": 0,
            "phase": "DESIGN_ACCEPTED",
            "prior_receipt_sha256": None,
            "recorded_at_utc": _utc_now(),
            "git_head": snapshot.head,
            **evidence,
            "acquisition_checkpoint_head": None,
            "inventory": protected,
            "validation_receipts": [],
        }
        contents, digest = _success_contents(payload, run_spec)
        relative = paths.receipts_relative / f"000000-DESIGN_ACCEPTED-{digest}.json"
        _publish_locked(
            paths,
            snapshot,
            lock_binding,
            relative,
            contents,
            "execution receipt",
            (protected_snapshot,),
            expected_receipt_names=(relative.name,),
        )
        return ExecutionRegistryState("DESIGN_ACCEPTED", 0, digest, str(protected["sha256"]))


def load_execution_registry(root: Path, run_spec: Authority) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        state, _ = _load_locked(paths, run_spec, lock_binding)
        return state


def load_latest_acquisition_checkpoint(
    root: Path,
    run_spec: Authority,
) -> AcquisitionCheckpoint | None:
    """Load the fully verified append-only restart authority.

    A tail deleted before this read cannot be distinguished from a tail that was
    never published until a later immutable checkpoint, phase receipt, or
    completion receipt binds it. Gaps, forks, and deletion during this read fail
    closed.
    """
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        state, registry_payloads = _load_locked(
            paths, run_spec, lock_binding
        )
        snapshot = _capture_git_snapshot(paths.root, registry_payloads[0])
        checkpoints, _, _ = _load_checkpoints_locked(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            lock_binding,
            verify_latest_progress=False,
            read_only_progress=_checkpoint_progress_is_read_only(
                run_spec,
                state.phase,
                registry_payloads[-1].get("acquisition_checkpoint_head"),
            ),
        )
        if not checkpoints and isinstance(run_spec, SourceRunSpec):
            from mlb_backtest.v52_query_plan import build_static_batch

            base = registry_payloads[0]
            run_binding = _validate_binding_value(
                base["run_spec"], "checkpoint RunSpec"
            )
            contract_binding = _validate_binding_value(
                base["acquisition_contract"], "checkpoint acquisition contract"
            )
            static_batch = build_static_batch(
                paths.root / str(run_binding["path"]),
                paths.root / str(contract_binding["path"]),
            )
            static_path = (
                paths.root
                / _authority_namespace(run_spec, "audit")
                / "acquisition/query_batches"
                / f"{static_batch.batch_id}.json"
            )
            try:
                os.lstat(static_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ValueError(
                    "source static batch identity is unavailable"
                ) from exc
            else:
                raise ValueError(
                    "source static batch requires an acquisition checkpoint"
                )
        return checkpoints[-1] if checkpoints else None


def _load_read_only_terminal_acquisition_checkpoint(
    root: Path,
    run_spec: Authority,
    *,
    allowed_phases: frozenset[str],
    phase_error: str,
) -> AcquisitionCheckpoint:
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        state, registry_payloads = _load_locked(
            paths,
            run_spec,
            lock_binding,
            read_only_progress=True,
        )
        if state.phase not in allowed_phases:
            raise ValueError(phase_error)
        snapshot = _capture_git_snapshot(paths.root, registry_payloads[0])
        checkpoints, payloads, names = _load_checkpoints_locked(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            lock_binding,
            verify_latest_progress=True,
            read_only_progress=True,
        )
        _terminal_checkpoint_head(paths, checkpoints, payloads, names)
        _require_source_terminal_disposition(run_spec, checkpoints[-1])
        return checkpoints[-1]


def load_source_verification_checkpoint(
    root: Path,
    run_spec: Authority,
) -> AcquisitionCheckpoint:
    """Read-only authentication of terminal acquisition evidence for Task 8."""
    return _load_read_only_terminal_acquisition_checkpoint(
        root,
        run_spec,
        allowed_phases=frozenset({"ACQUIRING", "SOURCES_READY"}),
        phase_error="source verification requires ACQUIRING or SOURCES_READY",
    )


def load_terminal_acquisition_checkpoint(
    root: Path,
    run_spec: Authority,
) -> AcquisitionCheckpoint:
    """Read-only authentication of the frozen SOURCES_READY checkpoint head."""
    return _load_read_only_terminal_acquisition_checkpoint(
        root,
        run_spec,
        allowed_phases=frozenset({"SOURCES_READY"}),
        phase_error="terminal acquisition replay requires SOURCES_READY",
    )


def validate_sources_ready_evidence_set(
    root: Path,
    run_spec: Authority,
    *,
    inventory_path: Path,
    checkpoint_path: Path,
    validation_receipts: Sequence[Path],
) -> None:
    """Read-only authentication for one explicit SOURCES_READY evidence set.

    This is the service-facing source-gate authority.  It validates the exact
    caller order under the registry's shared lock without publishing a phase
    receipt, creating quarantine evidence, or repairing any state.
    """
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        state, payloads = _load_locked(
            paths, run_spec, lock_binding, read_only_progress=True
        )
        if state.phase not in {"ACQUIRING", "SOURCES_READY"}:
            raise ValueError(
                "source evidence validation requires ACQUIRING or SOURCES_READY"
            )
        base = payloads[0]
        snapshot = _capture_git_snapshot(paths.root, base)
        inventory_snapshot = _capture_artifact(
            paths.root, inventory_path, "phase inventory"
        )
        validation_snapshots = _artifact_snapshots(
            paths.root, validation_receipts, "validation receipt"
        )
        source_evidence_snapshots = _validate_sources_ready_evidence(
            inventory_snapshot,
            validation_snapshots,
            run_id=run_spec.run_id,
            root=paths.root,
            base=base,
            trusted_git=snapshot,
            authority=run_spec,
        )
        checkpoints, checkpoint_payloads, checkpoint_names = _load_checkpoints_locked(
                paths,
                run_spec,
                payloads,
                snapshot,
                lock_binding,
                verify_latest_progress=True,
                read_only_progress=True,
            )
        checkpoint_head = _terminal_checkpoint_head(
            paths, checkpoints, checkpoint_payloads, checkpoint_names
        )
        _require_source_terminal_disposition(run_spec, checkpoints[-1])
        _require_source_inventory_terminal_checkpoint(
            run_spec, inventory_snapshot, checkpoint_head
        )
        supplied_checkpoint = _capture_artifact(
            paths.root, checkpoint_path, "terminal acquisition checkpoint"
        )
        if supplied_checkpoint.binding != {
            "path": checkpoint_head["path"],
            "sha256": checkpoint_head["sha256"],
        }:
            raise ValueError("terminal acquisition checkpoint differs")
        _verify_artifact_snapshots(
            paths.root,
            (
                *validation_snapshots,
                inventory_snapshot,
                supplied_checkpoint,
                *source_evidence_snapshots,
            ),
            "source evidence",
        )
        _verify_git_snapshot(paths.root, snapshot)


def publish_acquisition_checkpoint(
    root: Path,
    run_spec: Authority,
    *,
    expected_prior_checkpoint_sha256: str | None,
    stage: str,
    query_batch_ids: Sequence[str],
    completed_query_ids: Sequence[str],
    pending_query_ids: Sequence[str],
    quarantined_query_ids: Sequence[str],
    split_query_ids: Sequence[str],
    stage_evidence_receipts: Sequence[Path] = (),
    split_receipt_paths: Sequence[Path] = (),
    dispositioned_query_ids: Sequence[str] | object = _UNSET,
) -> AcquisitionCheckpoint:
    if isinstance(run_spec, SourceRunSpec):
        if dispositioned_query_ids is _UNSET:
            raise ValueError(
                "SourceRunSpec checkpoints require dispositioned_query_ids explicitly"
            )
    elif dispositioned_query_ids is not _UNSET:
        raise ValueError(
            "historical checkpoint writer must omit dispositioned_query_ids"
        )
    for values, label in (
        (completed_query_ids, "completed query IDs"),
        (pending_query_ids, "pending query IDs"),
        (quarantined_query_ids, "quarantined query IDs"),
        (split_query_ids, "split query IDs"),
        *(
            ((dispositioned_query_ids, "dispositioned query IDs"),)
            if dispositioned_query_ids is not _UNSET
            else ()
        ),
    ):
        supplied = tuple(values)
        try:
            canonical = tuple(sorted(supplied))
        except TypeError as exc:
            raise ValueError(f"checkpoint {label} order differs") from exc
        if supplied != canonical:
            raise ValueError(f"checkpoint {label} order differs")
    for values, label in (
        (stage_evidence_receipts, "stage evidence"),
        (split_receipt_paths, "split receipt paths"),
    ):
        supplied = tuple(values)
        try:
            canonical = tuple(sorted(supplied, key=os.fspath))
        except TypeError as exc:
            raise ValueError(f"checkpoint {label} order differs") from exc
        if supplied != canonical:
            raise ValueError(f"checkpoint {label} order differs")
    paths = _paths(root, run_spec)
    if expected_prior_checkpoint_sha256 is not None and (
        not isinstance(expected_prior_checkpoint_sha256, str)
        or not _SHA256.fullmatch(expected_prior_checkpoint_sha256)
    ):
        raise ValueError("expected prior checkpoint SHA-256 differs")
    _ensure_registry_directories(paths)
    with _registry_lock(paths, create=False, exclusive=True) as lock_binding:
        state, registry_payloads = _load_locked(paths, run_spec, lock_binding)
        if state.phase != "ACQUIRING":
            raise ValueError(
                "acquisition checkpoints require the authenticated ACQUIRING phase"
            )
        if stage not in _OPEN_CHECKPOINT_STAGES:
            raise ValueError(
                "non-static checkpoint stage authority is unavailable until Task7D"
            )
        snapshot = _capture_git_snapshot(paths.root, registry_payloads[0])
        checkpoints, checkpoint_payloads, names = _load_checkpoints_locked(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            lock_binding,
            verify_latest_progress=False,
        )
        source_checkpoint_authority = None
        if isinstance(run_spec, SourceRunSpec) and checkpoint_payloads:
            source_checkpoint_authority = _issue_source_checkpoint_authority(
                checkpoint_payloads[-1], run_spec, lock_binding
            )
        semantic = _checkpoint_current_semantic(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            stage=stage,
            query_batch_ids=query_batch_ids,
            completed_query_ids=completed_query_ids,
            pending_query_ids=pending_query_ids,
            quarantined_query_ids=quarantined_query_ids,
            split_query_ids=split_query_ids,
            stage_evidence_receipts=stage_evidence_receipts,
            split_receipt_paths=split_receipt_paths,
            dispositioned_query_ids=(
                () if dispositioned_query_ids is _UNSET else dispositioned_query_ids
            ),
            source_checkpoint_authority=source_checkpoint_authority,
        )
        semantic_snapshots = _checkpoint_semantic_snapshots(paths, semantic)

        def verify_checkpoint_semantic() -> None:
            rebound = _checkpoint_current_semantic(
                paths,
                run_spec,
                registry_payloads,
                snapshot,
                stage=stage,
                query_batch_ids=query_batch_ids,
                completed_query_ids=completed_query_ids,
                pending_query_ids=pending_query_ids,
                quarantined_query_ids=quarantined_query_ids,
                split_query_ids=split_query_ids,
                stage_evidence_receipts=stage_evidence_receipts,
                split_receipt_paths=split_receipt_paths,
                dispositioned_query_ids=(
                    () if dispositioned_query_ids is _UNSET else dispositioned_query_ids
                ),
                source_checkpoint_authority=source_checkpoint_authority,
            )
            if rebound != semantic:
                raise ValueError(
                    "acquisition checkpoint semantic authority changed"
                )
        previous_checkpoint = checkpoints[-1] if checkpoints else None
        previous_payload = checkpoint_payloads[-1] if checkpoint_payloads else None
        if previous_payload is not None and (
            _checkpoint_semantic_projection(previous_payload) == semantic
        ):
            if expected_prior_checkpoint_sha256 not in {
                previous_checkpoint.receipt_sha256,  # type: ignore[union-attr]
                previous_checkpoint.prior_checkpoint_sha256,  # type: ignore[union-attr]
            }:
                raise ValueError("acquisition checkpoint stale replay or prior differs")
            verify_checkpoint_semantic()
            return previous_checkpoint  # type: ignore[return-value]
        authenticated_prior = (
            previous_checkpoint.receipt_sha256
            if previous_checkpoint is not None
            else None
        )
        if expected_prior_checkpoint_sha256 != authenticated_prior:
            raise ValueError("acquisition checkpoint stale prior or fork differs")
        sequence = len(checkpoints)
        payload = {
            **semantic,
            "sequence": sequence,
            "prior_checkpoint_sha256": authenticated_prior,
            "recorded_at_utc": _utc_now(),
        }
        _validate_checkpoint_payload(payload, run_spec)
        _validate_checkpoint_progression(previous_payload, payload, run_spec)
        if isinstance(run_spec, SourceRunSpec):
            source_checkpoint_authority = _issue_source_checkpoint_authority(
                payload, run_spec, lock_binding
            )
        contents = canonical_json_bytes(payload) + b"\n"
        digest = _sha256_bytes(contents)
        relative = paths.checkpoints_relative / f"{sequence:08d}-{digest}.json"
        _publish_locked(
            paths,
            snapshot,
            lock_binding,
            relative,
            contents,
            "acquisition checkpoint",
            semantic_snapshots,
            expected_receipt_names=(*names, relative.name),
            membership_validator=_validate_checkpoint_name_chain,
            state_verifier=verify_checkpoint_semantic,
        )
        final_checkpoints, _, final_names = _load_checkpoints_locked(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            lock_binding,
            verify_latest_progress=True,
        )
        if (
            not final_checkpoints
            or final_checkpoints[-1].receipt_sha256 != digest
            or final_names != (*names, relative.name)
        ):
            raise ValueError("acquisition checkpoint publication did not seal its head")
        return final_checkpoints[-1]


def _advance_execution_phase(
    root: Path,
    run_spec: Authority,
    *,
    expected_phase: str,
    next_phase: str,
    inventory_path: Path,
    validation_receipts: Sequence[Path],
    expected_checkpoint_path: Path | None = None,
    atomic_source_gate: bool = False,
    return_transition_result: bool = False,
) -> ExecutionRegistryState | ExecutionPhaseAdvance:
    phases = _authority_phases(run_spec)
    if (
        expected_phase not in phases
        or next_phase not in phases
        or phases.index(next_phase) != phases.index(expected_phase) + 1
    ):
        raise ValueError("execution transition must use the immediate next phase")
    paths = _paths(root, run_spec)
    if next_phase not in {"ACQUIRING", "SOURCES_READY"}:
        raise ValueError("execution transition fails closed until a phase validator exists")

    def transition_result(
        transition_state: ExecutionRegistryState, *, created: bool
    ) -> ExecutionRegistryState | ExecutionPhaseAdvance:
        if return_transition_result:
            return ExecutionPhaseAdvance(transition_state, created)
        return transition_state

    with _registry_lock(paths, create=False, exclusive=True) as lock_binding:
        state, payloads = _load_locked(
            paths,
            run_spec,
            lock_binding,
            read_only_progress=atomic_source_gate,
        )
        # SOURCES_READY is the sole idempotent transition: an interrupted caller
        # may retry only the exact immutable evidence set that sealed the head.
        # Authenticate before branching so a retry can never become a second
        # receipt, a timestamp refresh, or a quarantine side effect.
        if state.phase == next_phase:
            if not (
                expected_phase == "ACQUIRING" and next_phase == "SOURCES_READY"
            ):
                raise ValueError(
                    "execution transition replay or expected phase differs from "
                    "authenticated head"
                )
            base = payloads[0]
            snapshot = _capture_git_snapshot(paths.root, base)
            inventory_snapshot = _capture_artifact(
                paths.root, inventory_path, "phase inventory"
            )
            validation_snapshots = _artifact_snapshots(
                paths.root, validation_receipts, "validation receipt"
            )
            current = payloads[-1]
            expected_inventory = _validate_binding_value(
                current["inventory"], "phase inventory"
            )
            expected_validations = current.get("validation_receipts")
            if not isinstance(expected_validations, list) or any(
                not isinstance(item, Mapping) for item in expected_validations
            ):
                raise ValueError("SOURCES_READY receipt validation bindings differ")
            if (
                inventory_snapshot.binding != dict(expected_inventory)
                or [item.binding for item in validation_snapshots]
                != [dict(item) for item in expected_validations]
            ):
                raise ValueError("SOURCES_READY retry evidence differs")
            source_evidence_snapshots = _validate_phase_evidence(
                next_phase,
                base,
                inventory_snapshot,
                validation_snapshots,
                root=paths.root,
                run_id=run_spec.run_id,
                trusted_git=snapshot,
                authority=run_spec,
            )
            checkpoints, checkpoint_payloads, checkpoint_names = (
                _load_checkpoints_locked(
                    paths,
                    run_spec,
                    payloads,
                    snapshot,
                    lock_binding,
                    verify_latest_progress=True,
                    read_only_progress=atomic_source_gate,
                )
            )
            checkpoint_head = _terminal_checkpoint_head(
                paths, checkpoints, checkpoint_payloads, checkpoint_names
            )
            _require_source_terminal_disposition(run_spec, checkpoints[-1])
            _require_source_inventory_terminal_checkpoint(
                run_spec, inventory_snapshot, checkpoint_head
            )
            if expected_checkpoint_path is not None:
                expected_checkpoint = _capture_artifact(
                    paths.root, expected_checkpoint_path, "expected terminal acquisition checkpoint"
                )
                if expected_checkpoint.binding != {
                    "path": checkpoint_head["path"], "sha256": checkpoint_head["sha256"]
                }:
                    raise ValueError("SOURCES_READY retry checkpoint differs")
            if current.get("acquisition_checkpoint_head") != checkpoint_head:
                raise ValueError("SOURCES_READY retry checkpoint differs")
            _verify_artifact_snapshot(paths.root, inventory_snapshot, "phase inventory")
            _verify_artifact_snapshots(
                paths.root, validation_snapshots, "validation receipt"
            )
            _verify_artifact_snapshots(
                paths.root,
                source_evidence_snapshots,
                "source evidence",
            )
            _verify_git_snapshot(paths.root, snapshot)
            return transition_result(state, created=False)
        if state.phase != expected_phase:
            raise ValueError(
                "execution transition replay or expected phase differs from "
                "authenticated head"
            )
        base = payloads[0]
        snapshot = _capture_git_snapshot(paths.root, base)
        inventory_snapshot = _capture_artifact(
            paths.root, inventory_path, "phase inventory"
        )
        validation_snapshots = _artifact_snapshots(
            paths.root,
            validation_receipts,
            "validation receipt",
            allow_duplicate_paths=True,
        )
        inventory = inventory_snapshot.binding
        validations = [artifact.binding for artifact in validation_snapshots]
        checkpoint_snapshot: _ArtifactSnapshot | None = None
        checkpoint_verifier: Callable[[], None] | None = None
        source_evidence_snapshots: tuple[_ArtifactSnapshot, ...] = ()
        try:
            source_evidence_snapshots = _validate_phase_evidence(
                next_phase,
                base,
                inventory_snapshot,
                validation_snapshots,
                root=paths.root,
                run_id=run_spec.run_id,
                trusted_git=snapshot,
                authority=run_spec,
            )
            checkpoint_head: Mapping[str, object] | None = None
            if next_phase == "SOURCES_READY":
                checkpoints, checkpoint_payloads, checkpoint_names = (
                    _load_checkpoints_locked(
                        paths,
                        run_spec,
                        payloads,
                        snapshot,
                        lock_binding,
                        verify_latest_progress=True,
                        read_only_progress=atomic_source_gate,
                    )
                )
                checkpoint_head = _terminal_checkpoint_head(
                    paths,
                    checkpoints,
                    checkpoint_payloads,
                    checkpoint_names,
                )
                _require_source_terminal_disposition(run_spec, checkpoints[-1])
                _require_source_inventory_terminal_checkpoint(
                    run_spec, inventory_snapshot, checkpoint_head
                )
                checkpoint_snapshot = _capture_artifact(
                    paths.root,
                    paths.root / str(checkpoint_head["path"]),
                    "terminal acquisition checkpoint",
                )
                if checkpoint_snapshot.binding != {
                    "path": checkpoint_head["path"],
                    "sha256": checkpoint_head["sha256"],
                }:
                    raise ValueError(
                        "terminal acquisition checkpoint snapshot differs"
                    )
                if expected_checkpoint_path is not None:
                    expected_checkpoint = _capture_artifact(
                        paths.root, expected_checkpoint_path, "expected terminal acquisition checkpoint"
                    )
                    if expected_checkpoint.binding != checkpoint_snapshot.binding:
                        raise ValueError("terminal acquisition checkpoint differs")

                def verify_checkpoint_state() -> None:
                    verified, verified_payloads, verified_names = (
                        _load_checkpoints_locked(
                            paths,
                            run_spec,
                            payloads,
                            snapshot,
                            lock_binding,
                            verify_latest_progress=True,
                            read_only_progress=atomic_source_gate,
                        )
                    )
                    if (
                        _terminal_checkpoint_head(
                            paths,
                            verified,
                            verified_payloads,
                            verified_names,
                        )
                        != checkpoint_head
                    ):
                        raise ValueError(
                            "terminal acquisition checkpoint state changed"
                        )
                    _require_source_terminal_disposition(run_spec, verified[-1])
                    _require_source_inventory_terminal_checkpoint(
                        run_spec,
                        inventory_snapshot,
                        checkpoint_head,
                    )

                checkpoint_verifier = verify_checkpoint_state
            payload = {
                "schema_version": 1,
                "receipt_kind": "PHASE_TRANSITION",
                "run_id": run_spec.run_id,
                "sequence": state.sequence + 1,
                "phase": next_phase,
                "prior_receipt_sha256": state.receipt_sha256,
                "recorded_at_utc": _utc_now(),
                "git_head": snapshot.head,
                "acquisition_checkpoint_head": checkpoint_head,
                **{
                    key: base[key]
                    for key in (
                        "run_spec",
                        "approved_design",
                        "implementation_plan",
                        "acquisition_contract",
                        "task0_lineage",
                    )
                },
                "inventory": inventory,
                "validation_receipts": validations,
            }
            contents, digest = _success_contents(payload, run_spec)
        except ValueError as exc:
            if atomic_source_gate and next_phase == "SOURCES_READY":
                raise ValueError("atomic source gate evidence failed") from exc
            error_classification = (
                "DIRECT_INVENTORY_MISMATCH"
                if next_phase == "ACQUIRING"
                else "SOURCE_GATE_EVIDENCE_INVALID"
            )
            _publish_quarantine_locked(
                paths,
                run_spec,
                state,
                base,
                next_phase,
                validations,
                snapshot,
                lock_binding,
                inventory=inventory,
                error_classification=error_classification,
                evidence_snapshots=(inventory_snapshot, *validation_snapshots),
            )
            raise ValueError("direct phase artifact evidence failed") from exc
        relative = paths.receipts_relative / f"{state.sequence + 1:06d}-{next_phase}-{digest}.json"
        def verify_source_evidence_and_checkpoint() -> None:
            if source_evidence_snapshots:
                try:
                    _verify_artifact_snapshots(
                        paths.root,
                        source_evidence_snapshots,
                        "late source evidence",
                    )
                except ValueError as exc:
                    raise _LateSourceEvidenceError(
                        "late source evidence changed before publication"
                    ) from exc
            if checkpoint_verifier is not None:
                checkpoint_verifier()

        try:
            _publish_locked(
                paths,
                snapshot,
                lock_binding,
                relative,
                contents,
                "execution receipt",
                (
                    inventory_snapshot,
                    *validation_snapshots,
                    *((checkpoint_snapshot,) if checkpoint_snapshot else ()),
                ),
                expected_receipt_names=(
                    *(_success_receipt_name(item, run_spec) for item in payloads),
                    relative.name,
                ),
                state_verifier=verify_source_evidence_and_checkpoint,
            )
        except _LateSourceEvidenceError as exc:
            try:
                _verify_lock_binding(paths, lock_binding)
                _verify_git_snapshot(paths.root, snapshot)
            except ValueError as trust_exc:
                raise exc from trust_exc
            if atomic_source_gate and next_phase == "SOURCES_READY":
                raise ValueError("atomic source gate evidence failed") from exc
            _publish_quarantine_locked(
                paths,
                run_spec,
                state,
                base,
                next_phase,
                validations,
                snapshot,
                lock_binding,
                inventory=inventory,
                error_classification=(
                    "DIRECT_INVENTORY_MISMATCH"
                    if next_phase == "ACQUIRING"
                    else "SOURCE_GATE_EVIDENCE_INVALID"
                ),
                evidence_snapshots=(inventory_snapshot, *validation_snapshots),
            )
            raise ValueError("direct phase artifact evidence failed") from exc
        return transition_result(
            ExecutionRegistryState(
                next_phase, state.sequence + 1, digest, inventory["sha256"]
            ),
            created=True,
        )


def advance_execution_phase(
    root: Path,
    run_spec: Authority,
    *,
    expected_phase: str,
    next_phase: str,
    inventory_path: Path,
    validation_receipts: Sequence[Path],
) -> ExecutionRegistryState:
    """Advance one generic phase using the original Task2 public contract."""
    result = _advance_execution_phase(
        root, run_spec, expected_phase=expected_phase, next_phase=next_phase,
        inventory_path=inventory_path, validation_receipts=validation_receipts,
    )
    assert isinstance(result, ExecutionRegistryState)
    return result


def advance_sources_ready_phase(
    root: Path,
    run_spec: Authority,
    *,
    inventory_path: Path,
    validation_receipts: Sequence[Path],
    expected_checkpoint_path: Path,
) -> ExecutionPhaseAdvance:
    """Atomically bind explicit source evidence and checkpoint to SOURCES_READY."""
    result = _advance_execution_phase(
        root,
        run_spec,
        expected_phase="ACQUIRING",
        next_phase="SOURCES_READY",
        inventory_path=inventory_path,
        validation_receipts=validation_receipts,
        expected_checkpoint_path=expected_checkpoint_path,
        atomic_source_gate=True,
        return_transition_result=True,
    )
    assert isinstance(result, ExecutionPhaseAdvance)
    return result


def quarantine_execution_transition(
    root: Path,
    run_spec: Authority,
    *,
    attempted_phase: str,
    validation_receipts: Sequence[Path],
) -> Path:
    phases = _authority_phases(run_spec)
    if attempted_phase not in phases:
        raise ValueError(
            "quarantine attempted phase must be the immediate next phase"
        )
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=True) as lock_binding:
        state, payloads = _load_locked(paths, run_spec, lock_binding)
        if (
            state.sequence + 1 >= len(phases)
            or attempted_phase != phases[state.sequence + 1]
        ):
            raise ValueError(
                "quarantine attempted phase must be the immediate next phase"
            )
        validation_snapshots = _artifact_snapshots(
            paths.root, validation_receipts, "validation receipt"
        )
        validations = [snapshot.binding for snapshot in validation_snapshots]
        if not validations:
            raise ValueError("quarantine transition requires validation evidence")
        snapshot = _capture_git_snapshot(paths.root, payloads[0])
        return _publish_quarantine_locked(
            paths,
            run_spec,
            state,
            payloads[0],
            attempted_phase,
            validations,
            snapshot,
            lock_binding,
            evidence_snapshots=validation_snapshots,
        )


# A local-only append log detects an interior deletion while a later receipt survives.
# Coordinated deletion of the entire unanchored local tail is outside this threat model
# until a later task publishes an external anchor.
