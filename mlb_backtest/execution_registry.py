from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.protocols import RunSpec


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
    "mlb_backtest/career_bf.py",
    "mlb_backtest/config.py",
    "mlb_backtest/diagnostics.py",
    "mlb_backtest/execution_registry.py",
    "mlb_backtest/feature_engine.py",
    "mlb_backtest/features/__init__.py",
    "mlb_backtest/features/bullpen.py",
    "mlb_backtest/features/context.py",
    "mlb_backtest/features/offense.py",
    "mlb_backtest/features/starter.py",
    "mlb_backtest/locking.py",
    "mlb_backtest/manifest.py",
    "mlb_backtest/metrics.py",
    "mlb_backtest/mlb_stats.py",
    "mlb_backtest/model_prep.py",
    "mlb_backtest/models.py",
    "mlb_backtest/normalize.py",
    "mlb_backtest/outcomes.py",
    "mlb_backtest/pipeline.py",
    "mlb_backtest/prepare.py",
    "mlb_backtest/protocols.py",
    "mlb_backtest/ranking.py",
    "mlb_backtest/scoring.py",
    "mlb_backtest/scoring_prep.py",
    "mlb_backtest/sources.py",
    "mlb_backtest/statcast.py",
    "mlb_backtest/verify_v52_design.py",
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
    "mlb_backtest/v52_sources/__init__.py",
    "mlb_backtest/v52_sources/history.py",
    "mlb_backtest/v52_sources/pitching.py",
    "mlb_backtest/v52_sources/references.py",
    "mlb_backtest/v52_sources/schedule.py",
    "mlb_backtest/v52_sources/statcast.py",
    "mlb_backtest/v52_timezone_review.py",
    "schemas/v5_2_acquisition_checkpoint.schema.json",
    "schemas/v5_2_execution_receipt.schema.json",
    "schemas/v5_2_game_evidence_inventory.schema.json",
    "schemas/v5_2_legacy_decision_inventory.schema.json",
    "schemas/v5_2_schedule_anchor_inventory.schema.json",
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
_SOURCE_INVENTORY_PATH = (
    "data/v5_2_qd/historical/sources/source_inventory.json"
)
_SOURCE_REVIEW_PREFIX = (
    "audit/v5_2_qd/historical/acquisition/source_reviews/"
)
_SOURCE_REPLAY_PREFIX = (
    "audit/v5_2_qd/historical/acquisition/source_replays/"
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


@dataclass(frozen=True)
class ExecutionRegistryState:
    phase: str
    sequence: int
    receipt_sha256: str
    inventory_sha256: str


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


def _paths(root: Path, run_spec: RunSpec) -> _RegistryPaths:
    root_path = _root_path(root)
    namespaces = run_spec.raw.get("namespaces")
    if not isinstance(namespaces, Mapping) or namespaces.get("audit") != "audit/v5_2_qd/historical":
        raise ValueError("execution registry requires the frozen historical audit namespace")
    execution = Path("audit/v5_2_qd/historical/execution")
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


def _source_inventory_payload(
    contents: bytes, *, run_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
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
    return (
        _sha256_list(payload.get("snapshot_sha256s"), "source snapshots"),
        _sorted_string_list(payload.get("logical_query_ids"), "inventory query IDs"),
    )


def _source_receipt_payload(
    contents: bytes,
    *,
    path: str,
    run_id: str,
    inventory_sha256: str,
    snapshot_sha256s: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    payload = _canonical_object(contents, "source evidence receipt")
    if (
        type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
        or payload.get("inventory_sha256") != inventory_sha256
        or payload.get("critical_errors") != []
        or _sha256_list(payload.get("snapshot_sha256s"), "receipt snapshots")
        != snapshot_sha256s
    ):
        raise ValueError("source evidence receipt binding failed")
    kind = payload.get("receipt_kind")
    if kind == "SOURCE_REVIEW":
        if (
            not path.startswith(_SOURCE_REVIEW_PREFIX)
            or payload.get("reviewer_identity") != "independent-source-auditor"
            or payload.get("decision") != "APPROVED"
        ):
            raise ValueError("independent source review is not approved")
        return kind, _sorted_string_list(
            payload.get("reviewed_query_ids"), "reviewed query IDs"
        )
    if kind == "CLEAN_SOURCE_REPLAY":
        if (
            not path.startswith(_SOURCE_REPLAY_PREFIX)
            or payload.get("status") != "PASS"
            or type(payload.get("network_attempts")) is not int
            or payload.get("network_attempts") != 0
        ):
            raise ValueError("clean source replay failed")
        return kind, _string_list(
            payload.get("reviewed_query_ids"), "reviewed query IDs"
        )
    raise ValueError("unknown source evidence receipt kind")


def _validate_sources_ready_evidence(
    inventory: _ArtifactSnapshot,
    validation_values: Sequence[_ArtifactSnapshot],
    *,
    run_id: str,
) -> None:
    if inventory.path != _SOURCE_INVENTORY_PATH:
        raise ValueError("SOURCES_READY requires the canonical source inventory")
    snapshots, inventory_query_ids = _source_inventory_payload(
        inventory.contents, run_id=run_id
    )
    reviews = 0
    replay_paths: set[str] = set()
    replay_hashes: set[str] = set()
    reviewed_query_ids: tuple[str, ...] | None = None
    for artifact in validation_values:
        kind, receipt_queries = _source_receipt_payload(
            artifact.contents,
            path=artifact.path,
            run_id=run_id,
            inventory_sha256=inventory.sha256,
            snapshot_sha256s=snapshots,
        )
        if kind == "SOURCE_REVIEW":
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
    if reviews < 1 or len(replay_paths) != 2:
        raise ValueError(
            "SOURCES_READY requires an approved review and two clean replays"
        )


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


def _plan_a_amendment_binding(
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
    amendment = bindings.get("plan_a_amendment")
    if not isinstance(amendment, Mapping):
        raise ValueError("Plan A amendment binding is missing")
    return _artifact_binding(root, amendment, "Plan A amendment")


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
    paths.append(str(_plan_a_amendment_binding(root, evidence)["path"]))
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
    amendment = _plan_a_amendment_binding(root, evidence)
    expected_bindings[str(amendment["path"])] = amendment
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
            raise ValueError("implementation plan Git blob binding differs")
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


def _validate_common_bindings(root: Path, payload: Mapping[str, object], run_spec: RunSpec) -> None:
    run_binding = _artifact_binding(root, payload["run_spec"], "run spec")
    _, run_bytes = _read_path(root, root / str(run_binding["path"]), "run spec")
    if canonical_json_bytes(run_spec.raw) + b"\n" != run_bytes:
        raise ValueError("caller RunSpec differs from receipt binding")
    _artifact_binding(root, payload["approved_design"], "approved design")
    _artifact_binding(root, payload["implementation_plan"], "implementation plan", plan=True)
    contract_binding = _artifact_binding(root, payload["acquisition_contract"], "acquisition contract")
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
    _plan_a_amendment_binding(root, payload)
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
    run_id: str,
) -> None:
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
        return
    if phase == "SOURCES_READY":
        _validate_sources_ready_evidence(
            inventory,
            validations,
            run_id=run_id,
        )
        return
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


def _validate_receipt_name_chain(names: Sequence[str]) -> None:
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
            or sequence >= len(PHASES)
            or phase != PHASES[sequence]
        ):
            raise _ReceiptMembershipError(
                "execution registry chain has a gap, fork, replay, or rollback anchor"
            )


def _receipt_payload(contents: bytes, expected_sha: str, filename_phase: str, filename_sequence: int) -> Mapping[str, object]:
    if _sha256_bytes(contents) != expected_sha:
        raise ValueError("execution receipt filename hash differs")
    payload = _canonical_object(contents, "execution receipt")
    _validate_success_receipt(payload)
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


def _validate_success_receipt(payload: object) -> Mapping[str, object]:
    receipt = _exact_object(payload, _SUCCESS_KEYS, "execution receipt")
    sequence = receipt.get("sequence")
    phase = receipt.get("phase")
    prior = receipt.get("prior_receipt_sha256")
    git_head = receipt.get("git_head")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "PHASE_TRANSITION"
        or receipt.get("run_id") != "historical_walkforward_500"
        or type(sequence) is not int
        or not 0 <= sequence < len(PHASES)
        or phase != PHASES[sequence]
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
        expected_path = (
            "audit/v5_2_qd/historical/execution/acquisition_checkpoints/"
            f"{head_sequence:08d}-{head_sha}.json"
        ) if type(head_sequence) is int else ""
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


def _validate_quarantine_receipt(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("quarantine receipt keys differ")
    keys = frozenset(payload)
    if keys not in {_QUARANTINE_KEYS, _AUTOMATIC_QUARANTINE_KEYS}:
        raise ValueError("quarantine receipt keys differ")
    receipt = payload
    automatic = keys == _AUTOMATIC_QUARANTINE_KEYS
    git_head = receipt.get("git_head")
    current_hash = receipt.get("current_receipt_sha256")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "QUARANTINED_TRANSITION"
        or receipt.get("run_id") != "historical_walkforward_500"
        or receipt.get("current_phase") not in PHASES
        or receipt.get("attempted_phase") not in PHASES
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
    run_spec: RunSpec,
    payloads: Sequence[Mapping[str, object]],
    phase_receipt_sha: Mapping[str, str],
    lock_binding: _RegistryLockBinding,
) -> None:
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
        _validate_quarantine_receipt(payload)
        current_phase = payload.get("current_phase")
        attempted = payload.get("attempted_phase")
        if (
            payload.get("schema_version") != 1
            or payload.get("receipt_kind") != "QUARANTINED_TRANSITION"
            or payload.get("run_id") != run_spec.run_id
            or current_phase not in PHASES
            or attempted not in PHASES
            or PHASES.index(str(attempted)) != PHASES.index(str(current_phase)) + 1
            or match.group("phase") != attempted
            or int(match.group("sequence")) != PHASES.index(str(attempted))
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
    run_spec: RunSpec,
    lock_binding: _RegistryLockBinding,
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
    _validate_receipt_name_chain(names)
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
        payload = _receipt_payload(contents, receipt_sha, phase, sequence)
        if payload.get("run_id") != run_spec.run_id or payload.get("prior_receipt_sha256") != prior_sha:
            raise ValueError("execution registry prior hash or run identity differs")
        git_head = payload.get("git_head")
        if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
            raise ValueError("execution receipt Git head differs")
        _validate_common_bindings(paths.root, payload, run_spec)
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
            run_id=run_spec.run_id,
        )
        current_bindings = {key: payload[key] for key in ("run_spec", "approved_design", "implementation_plan", "acquisition_contract", "task0_lineage")}
        if stable_bindings is None:
            stable_bindings = current_bindings
            trusted_git = _capture_git_snapshot(paths.root, payload)
        elif current_bindings != stable_bindings:
            raise ValueError("execution registry binding fork differs")
        if trusted_git is None or git_head != trusted_git.head:
            raise ValueError("execution receipt Git HEAD differs from trusted snapshot")
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
        checkpoints, checkpoint_payloads, checkpoint_names = (
            _load_checkpoints_locked(
                paths,
                run_spec,
                payloads,
                trusted_git,
                lock_binding,
                verify_latest_progress=True,
            )
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


def _initial_evidence(root: Path, run_spec: RunSpec, design_path: Path, plan_path: Path, acquisition_contract_path: Path) -> dict[str, object]:
    _require_bound_execution_code_clean(root)
    contract_binding = _binding(root, acquisition_contract_path, "acquisition contract")
    _require_tracked_clean(root, contract_binding["path"], "acquisition contract")
    contract = _canonical_object(_read_path(root, acquisition_contract_path, "acquisition contract")[1], "acquisition contract")
    bindings = contract.get("bindings")
    protection = contract.get("legacy_protection")
    if not isinstance(bindings, Mapping) or not isinstance(protection, Mapping):
        raise ValueError("acquisition contract evidence is incomplete")
    design = _binding(root, design_path, "approved design")
    plan = _binding(root, plan_path, "implementation plan")
    run_binding_raw = bindings.get("run_spec")
    if not isinstance(run_binding_raw, Mapping) or not isinstance(run_binding_raw.get("path"), str):
        raise ValueError("acquisition contract RunSpec binding differs")
    run_binding = _binding(root, root / str(run_binding_raw["path"]), "run spec")
    if canonical_json_bytes(run_spec.raw) + b"\n" != _read_path(root, root / run_binding["path"], "run spec")[1]:
        raise ValueError("caller RunSpec differs from acquisition contract")
    plan_contract = bindings.get("implementation_plan")
    if not isinstance(plan_contract, Mapping):
        raise ValueError("implementation plan binding differs")
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


def _success_contents(payload: Mapping[str, object]) -> tuple[bytes, str]:
    _validate_success_receipt(payload)
    contents = canonical_json_bytes(payload) + b"\n"
    return contents, _sha256_bytes(contents)


def _success_receipt_name(payload: Mapping[str, object]) -> str:
    _, digest = _success_contents(payload)
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
    run_spec: RunSpec,
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
    _validate_quarantine_receipt(payload)
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


def _validate_checkpoint_payload(payload: object) -> Mapping[str, object]:
    receipt = _exact_object(payload, _CHECKPOINT_KEYS, "acquisition checkpoint")
    sequence = receipt.get("sequence")
    prior = receipt.get("prior_checkpoint_sha256")
    stage = receipt.get("stage")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "ACQUISITION_CHECKPOINT"
        or receipt.get("run_id") != "historical_walkforward_500"
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
    return receipt


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
    )


def _checkpoint_semantic_projection(
    payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: payload[key]
        for key in _CHECKPOINT_KEYS
        if key not in {"sequence", "prior_checkpoint_sha256", "recorded_at_utc"}
    }


def _checkpoint_artifact_binding(root: Path, relative: Path, label: str) -> dict[str, str]:
    artifact = _capture_artifact(root, root / relative, label)
    return artifact.binding


def _checkpoint_evidence_root(paths: _RegistryPaths, run_spec: RunSpec) -> Path:
    namespaces = run_spec.raw.get("namespaces")
    raw = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(raw, str) or not raw:
        raise ValueError("checkpoint RunSpec raw namespace differs")
    relative = Path(raw)
    if (
        relative.is_absolute()
        or "\\" in raw
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.as_posix() != raw
    ):
        raise ValueError("checkpoint RunSpec raw namespace differs")
    return paths.root / relative


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
    run_spec: RunSpec,
    *,
    stage: str,
    run_binding: Mapping[str, object],
    contract_binding: Mapping[str, object],
) -> tuple[tuple[object, ...], tuple[dict[str, str], ...]]:
    from mlb_backtest.v52_query_plan import (
        build_legacy_partition_batch,
        build_participant_derived_batch,
        build_schedule_reference_batch,
        build_statcast_root_batch,
        build_static_batch,
        load_query_batch,
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
    verified = load_verified_schedule_anchor_inventory(
        paths.root,
        run_spec,
        acquisition_contract_path=contract_path,
        static_batch_id=static_batch.batch_id,
        evidence_root=evidence_root,
    )
    reference_batch = build_schedule_reference_batch(
        paths.root,
        run_path,
        acquisition_contract_path=contract_path,
        static_batch_id=static_batch.batch_id,
        evidence_root=evidence_root,
    )
    if reference_batch.derived_from_sha256 != verified.sha256:
        raise ValueError("REFERENCE_INPUTS inventory-derived batch binding differs")
    if stage == "REFERENCE_INPUTS":
        return (
            (static_batch, reference_batch),
            ({"path": _SCHEDULE_INVENTORY_PATH, "sha256": verified.sha256},),
        )

    from mlb_backtest.v52_timezone_review import load_verified_timezone_gate

    gate = load_verified_timezone_gate(paths.root)
    if (
        gate.run_spec_sha256 != run_binding["sha256"]
        or gate.acquisition_contract_sha256 != contract_binding["sha256"]
        or gate.schedule_inventory_sha256 != verified.sha256
        or gate.query_batch_ids
        != (static_batch.batch_id, reference_batch.batch_id)
    ):
        raise ValueError("TIMEZONE_REVIEW gate authority binding differs")
    stage_evidence = (
        {"path": _TIMEZONE_STAGE_EVIDENCE_PATHS[0], "sha256": verified.sha256},
        {
            "path": _TIMEZONE_STAGE_EVIDENCE_PATHS[1],
            "sha256": gate.review_sha256s[0],
        },
        {
            "path": _TIMEZONE_STAGE_EVIDENCE_PATHS[2],
            "sha256": gate.review_sha256s[1],
        },
        {
            "path": _TIMEZONE_STAGE_EVIDENCE_PATHS[3],
            "sha256": gate.candidate_sha256,
        },
        {
            "path": _TIMEZONE_STAGE_EVIDENCE_PATHS[4],
            "sha256": gate.seal_sha256,
        },
        {
            "path": _TIMEZONE_STAGE_EVIDENCE_PATHS[5],
            "sha256": gate.mapping_sha256,
        },
    )
    if stage == "TIMEZONE_REVIEW":
        return (static_batch, reference_batch), stage_evidence

    from mlb_backtest.v52_game_evidence_inventory import (
        load_verified_game_evidence_inventory,
    )

    inventory = load_verified_game_evidence_inventory(paths.root)
    participant_batch = build_participant_derived_batch(paths.root)
    if (
        participant_batch.derived_from_sha256 != inventory.sha256
        or any(
            query.run_spec_sha256 != run_binding["sha256"]
            or query.acquisition_contract_sha256 != contract_binding["sha256"]
            for query in participant_batch.queries
        )
    ):
        raise ValueError("PARTICIPANT_DERIVED game inventory authority differs")
    participant_stage_evidence = tuple(
        sorted(
            (
                *stage_evidence,
                {
                    "path": _GAME_EVIDENCE_INVENTORY_PATH,
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

    root_expected = build_statcast_root_batch(paths.root)
    root_batch = load_query_batch(paths.root, root_expected.batch_id)
    if root_batch != root_expected or len(root_batch.queries) != 46:
        raise ValueError("REMAINING Statcast root authority differs")
    decision = load_verified_legacy_decision_inventory(paths.root)
    partition_batch = build_legacy_partition_batch(paths.root)
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
    decision_binding = _checkpoint_artifact_binding(
        paths.root,
        Path(_LEGACY_DECISION_INVENTORY_PATH),
        "REMAINING legacy decision inventory",
    )
    root_relative = (
        Path("audit/v5_2_qd/historical/acquisition/query_batches")
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
    run_spec: RunSpec,
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
) -> dict[str, object]:
    from mlb_backtest.v52_evidence import V52EvidenceStore, logical_query_id
    from mlb_backtest.v52_query_plan import (
        load_query_batch,
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
        batch = load_query_batch(paths.root, raw_batch_id)
        batch_index = ACQUISITION_STAGES.index(batch.stage)
        if batch_index < prior_stage_index or batch_index > ACQUISITION_STAGES.index(stage):
            raise ValueError("checkpoint batch stage order differs")
        prior_stage_index = batch_index
        relative = Path(
            "audit/v5_2_qd/historical/acquisition/query_batches"
        ) / f"{batch.batch_id}.json"
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
    authoritative_batches, authoritative_stage_evidence = (
        _checkpoint_stage_authority(
            paths,
            run_spec,
            stage=stage,
            run_binding=run_binding,
            contract_binding=contract_binding,
        )
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
    populations = (completed, pending, quarantined, split)
    if any(len(values) != len(set(values)) for values in populations):
        raise ValueError("checkpoint query status contains duplicates")
    union = set().union(*(set(values) for values in populations))
    if sum(len(values) for values in populations) != len(union) or union != set(queries):
        raise ValueError("checkpoint query status partition differs from batches")
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
        Path("audit/v5_2_qd/historical/acquisition/split_receipts")
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
            Path("audit/v5_2_qd/historical/acquisition/split_receipts")
            / f"{parent_id}.json"
        )
        receipt = load_split_receipt(
            paths.root,
            parent_id,
            evidence_root=evidence_root,
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
    store = V52EvidenceStore(raw_root) if raw_root.is_dir() else None
    evidence: list[dict[str, object]] = []
    for query_id, query in sorted(queries.items()):
        if store is None:
            actual_status = "PENDING"
            attempt_receipts: tuple[Mapping[str, object], ...] = ()
            published = None
            quarantines = ()
        else:
            progress = store.read_verified_query_progress(query)  # type: ignore[arg-type]
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
    run_spec: RunSpec,
    payload: Mapping[str, object],
) -> set[str]:
    from mlb_backtest.v52_evidence import V52EvidenceStore, logical_query_id
    from mlb_backtest.v52_query_plan import load_query_batch, load_split_receipt

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
    loaded_batch_sequence: list[object] = []
    for item in batches:
        assert isinstance(item, Mapping)
        batch_id = str(item["batch_id"])
        batch = load_query_batch(paths.root, batch_id)
        if batch.stage != item["stage"]:
            raise ValueError("checkpoint batch stage binding differs")
        loaded_batch_sequence.append(batch)
        expected_relative = Path(
            "audit/v5_2_qd/historical/acquisition/query_batches"
        ) / f"{batch_id}.json"
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
    authoritative_batches, authoritative_stage_evidence = (
        _checkpoint_stage_authority(
            paths,
            run_spec,
            stage=stage,
            run_binding=run_binding,
            contract_binding=contract_binding,
        )
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
    populations = set().union(
        set(payload["completed_query_ids"]),
        set(payload["pending_query_ids"]),
        set(payload["quarantined_query_ids"]),
        set(payload["split_query_ids"]),
    )
    if populations != query_ids:
        raise ValueError("checkpoint query status partition differs from batches")
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
        store = V52EvidenceStore(raw_root)
        namespaces = run_spec.raw["namespaces"]
        assert isinstance(namespaces, Mapping)
        for query_id, query in sorted(queries_by_id.items()):
            progress = store.read_verified_query_progress(query)  # type: ignore[arg-type]
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
                    "audit/v5_2_qd/historical/acquisition/split_receipts/"
                    f"{parent_id}.json"
                )
                receipt = load_split_receipt(
                    paths.root,
                    parent_id,
                    evidence_root=evidence_root,
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
) -> None:
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
        if evidence_paths != set(_TIMEZONE_STAGE_EVIDENCE_PATHS):
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
    run_spec: RunSpec,
    registry_payloads: Sequence[Mapping[str, object]],
    git_snapshot: _GitSnapshot,
    lock_binding: _RegistryLockBinding,
    *,
    verify_latest_progress: bool,
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
    prior_sha: str | None = None
    previous: Mapping[str, object] | None = None
    base = registry_payloads[0]
    for expected_sequence, name in enumerate(names):
        match = _CHECKPOINT_NAME.fullmatch(name)
        assert match is not None
        receipt_sha = match.group("sha")
        contents = _read_relative_at(
            lock_binding.execution_descriptor,
            checkpoint_child / name,
            "acquisition checkpoint",
        )
        if _sha256_bytes(contents) != receipt_sha:
            raise ValueError("acquisition checkpoint filename hash differs")
        payload = _validate_checkpoint_payload(
            _canonical_object(contents, "acquisition checkpoint")
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
        _checkpoint_artifacts_and_population(paths, run_spec, payload)
        _validate_checkpoint_progression(previous, payload)
        checkpoints.append(_checkpoint_from_payload(payload, receipt_sha))
        payloads.append(payload)
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
        )
        if _checkpoint_semantic_projection(latest) != current_semantic:
            raise ValueError("latest acquisition checkpoint differs from verified progress")
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


def initialize_execution_registry(
    root: Path,
    run_spec: RunSpec,
    *,
    design_path: Path,
    plan_path: Path,
    acquisition_contract_path: Path,
) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
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
            if state.phase not in PHASES or actual != expected:
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
        contents, digest = _success_contents(payload)
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


def load_execution_registry(root: Path, run_spec: RunSpec) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        state, _ = _load_locked(paths, run_spec, lock_binding)
        return state


def load_latest_acquisition_checkpoint(
    root: Path,
    run_spec: RunSpec,
) -> AcquisitionCheckpoint | None:
    """Load the fully verified append-only restart authority.

    A tail deleted before this read cannot be distinguished from a tail that was
    never published until a later immutable checkpoint, phase receipt, or
    completion receipt binds it. Gaps, forks, and deletion during this read fail
    closed.
    """
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False) as lock_binding:
        _, registry_payloads = _load_locked(paths, run_spec, lock_binding)
        snapshot = _capture_git_snapshot(paths.root, registry_payloads[0])
        checkpoints, _, _ = _load_checkpoints_locked(
            paths,
            run_spec,
            registry_payloads,
            snapshot,
            lock_binding,
            verify_latest_progress=False,
        )
        return checkpoints[-1] if checkpoints else None


def publish_acquisition_checkpoint(
    root: Path,
    run_spec: RunSpec,
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
) -> AcquisitionCheckpoint:
    for values, label in (
        (completed_query_ids, "completed query IDs"),
        (pending_query_ids, "pending query IDs"),
        (quarantined_query_ids, "quarantined query IDs"),
        (split_query_ids, "split query IDs"),
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
        _validate_checkpoint_payload(payload)
        _validate_checkpoint_progression(previous_payload, payload)
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


def advance_execution_phase(
    root: Path,
    run_spec: RunSpec,
    *,
    expected_phase: str,
    next_phase: str,
    inventory_path: Path,
    validation_receipts: Sequence[Path],
) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    if expected_phase not in PHASES or next_phase not in PHASES or PHASES.index(next_phase) != PHASES.index(expected_phase) + 1:
        raise ValueError("execution transition must use the immediate next phase")
    if next_phase not in {"ACQUIRING", "SOURCES_READY"}:
        raise ValueError("execution transition fails closed until a phase validator exists")
    with _registry_lock(paths, create=False, exclusive=True) as lock_binding:
        state, payloads = _load_locked(paths, run_spec, lock_binding)
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
        try:
            _validate_phase_evidence(
                next_phase,
                base,
                inventory_snapshot,
                validation_snapshots,
                run_id=run_spec.run_id,
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
                    )
                )
                checkpoint_head = _terminal_checkpoint_head(
                    paths,
                    checkpoints,
                    checkpoint_payloads,
                    checkpoint_names,
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

                def verify_checkpoint_state() -> None:
                    verified, verified_payloads, verified_names = (
                        _load_checkpoints_locked(
                            paths,
                            run_spec,
                            payloads,
                            snapshot,
                            lock_binding,
                            verify_latest_progress=True,
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
            contents, digest = _success_contents(payload)
        except ValueError as exc:
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
                *(_success_receipt_name(item) for item in payloads),
                relative.name,
            ),
            state_verifier=checkpoint_verifier,
        )
        return ExecutionRegistryState(next_phase, state.sequence + 1, digest, inventory["sha256"])


def quarantine_execution_transition(
    root: Path,
    run_spec: RunSpec,
    *,
    attempted_phase: str,
    validation_receipts: Sequence[Path],
) -> Path:
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=True) as lock_binding:
        validation_snapshots = _artifact_snapshots(
            paths.root, validation_receipts, "validation receipt"
        )
        validations = [snapshot.binding for snapshot in validation_snapshots]
        if not validations:
            raise ValueError("quarantine transition requires validation evidence")
        state, payloads = _load_locked(paths, run_spec, lock_binding)
        if state.sequence + 1 >= len(PHASES) or attempted_phase != PHASES[state.sequence + 1]:
            raise ValueError("quarantine attempted phase must be the immediate next phase")
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
