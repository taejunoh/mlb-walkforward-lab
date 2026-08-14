"""Authenticated, network-free authorities for v5.2 source finalization.

It owns authenticated orchestration for staged commands: read-only context,
status, and preflight checks plus explicit capability-bound publication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from . import execution_registry
from . import secure_fs
from . import v52_snapshot_store
from .execution_registry import (
    AcquisitionCheckpoint,
    ExecutionRegistryState,
    load_execution_registry,
    load_source_verification_checkpoint,
)
from .protocols import RunSpec, SourceRunSpec, parse_run_spec
from .v52_protected_inventory import (
    snapshot_protected_inventory_membership,
    verify_protected_raw_inventory,
)
from .v52_query_plan import SourceAuthorityContext, _load_static_contracts
from .v52_snapshot_store import (
    AuthorityBinding,
    AuthorityBindings,
    LockHeld,
    PublishConflict,
    PublishError,
    SnapshotStoreError,
    SnapshotStoreInputs,
    SourceInventory,
    TerminalCheckpointBinding,
    build_inventory,
    inventory_bytes,
    load_snapshot_store_from_fd,
    publish_snapshot_store_from_fd,
    read_inventory_from_fd,
)
from .v52_schedule_inventory import RecoveryDispositionBindings
from .v52_legacy import LegacyEvidenceCapability
from .v52_external_protected_parent import (
    ExternalProtectedParentInputs,
    build_external_protected_parent_capability,
)
from .v52_source_contracts import canonical_json_bytes
from .v52_verify_sources import (
    NormalizedSnapshot,
    SnapshotReplayInputs,
    load_snapshot_replay_inputs,
    make_r3_snapshot_store_capability,
    rebind_r3_snapshot_store_capability,
    validate_r3_snapshot_store_capability,
)
from . import v52_timezone_review


_RUN_SPEC_DEFAULT = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_CONTRACT_DEFAULT = Path("protocols/v5_2_qd/historical_source_acquisition.json")
_PROTECTED_DEFAULT = Path("audit/v5_1_bt/protected_raw_inventory.json")
_CHECKPOINT_ROOT = Path("audit/v5_2_qd/historical/execution/acquisition_checkpoints")
_TIMEZONE_SEAL = Path("audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json")
_SOURCE_ROOT_RELATIVE = Path("data/v5_2_qd/historical/sources")
_SOURCE_REVIEW_PREFIX = "audit/v5_2_qd/historical/acquisition/source_reviews/"
_SOURCE_REPLAY_PREFIX = "audit/v5_2_qd/historical/acquisition/source_replays/"
_HISTORICAL_AUTHORITY_PAIR = (
    "protocols/v5_2_qd/historical_walkforward_500.json",
    "protocols/v5_2_qd/historical_source_acquisition.json",
)
_SUPERSEDING_565534_AUTHORITY_PAIR = (
    "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
    "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
)
_RECOVERY_SCHEDULE_CONFLICTS_AUTHORITY_PAIR = (
    "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_r2.json",
    "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_r2.json",
)
_RECOVERY_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_r2"
_R3_RECOVERY_AUTHORITY_PAIR = (
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json",
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json",
)
_R3_RECOVERY_RUN_ID = (
    "historical_walkforward_500_"
    "recovery_schedule_conflicts_missing_venue_r3"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_QUERY_IDS_DOCUMENT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class CommonArgs:
    root: Path = Path(".")
    run_spec: Path = _RUN_SPEC_DEFAULT
    acquisition_contract: Path = _CONTRACT_DEFAULT
    protected_inventory: Path = _PROTECTED_DEFAULT
    legacy_root: Path | None = None


@dataclass(frozen=True)
class Blocker:
    code: str
    message: str
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinalizationContext:
    root: Path
    run_spec_path: Path
    acquisition_contract_path: Path
    protected_inventory_path: Path
    run_spec: RunSpec | SourceRunSpec | None
    registry: ExecutionRegistryState | None
    checkpoint: AcquisitionCheckpoint | None
    snapshot_inputs: SnapshotStoreInputs | None
    inventory: SourceInventory | None
    phase: str
    git_head: str | None
    blockers: tuple[Blocker, ...]
    authority_context: SourceAuthorityContext | None = None
    legacy_evidence: LegacyEvidenceCapability | None = None

    def close(self) -> None:
        if self.legacy_evidence is not None:
            self.legacy_evidence.close()

    def __enter__(self) -> "FinalizationContext":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @classmethod
    def absent(
        cls, root: Path, blockers: tuple[Blocker, ...]
    ) -> "FinalizationContext":
        trusted = _trusted_root(root)
        return cls(
            root=trusted,
            run_spec_path=trusted / CommonArgs.run_spec,
            acquisition_contract_path=trusted / CommonArgs.acquisition_contract,
            protected_inventory_path=trusted / CommonArgs.protected_inventory,
            run_spec=None,
            registry=None,
            checkpoint=None,
            snapshot_inputs=None,
            inventory=None,
            phase="ABSENT",
            git_head=None,
            blockers=tuple(blockers),
        )


@dataclass(frozen=True)
class StatusReport:
    phase: str
    checkpoint: Mapping[str, object] | None
    publish_ready: bool
    review_ready: bool
    replay_ready: bool
    advance_ready: bool
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True)
class PreflightReport:
    phase: str
    status: Literal["PASS", "BLOCKED"]
    inventory_sha256: str | None
    snapshot_count: int
    network_attempts: Literal[0]
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True)
class PublishReport:
    status: Literal["PUBLISHED", "EXISTING"]
    inventory_sha256: str
    snapshot_count: int


@dataclass(frozen=True)
class ReviewRequest:
    reviewer_identity: str
    reviewer_role: str
    decision: Literal["APPROVED", "REJECTED"]
    query_ids_file: Path
    write_receipt: Path
    write_commit: Path


@dataclass(frozen=True)
class ReplayRequest:
    write_receipt: Path
    write_commit: Path


@dataclass(frozen=True)
class AdvanceRequest:
    """Explicit SOURCE_RECEIPT_COMMIT marker paths selected by the caller."""

    review_receipts: tuple[Path, ...]
    replay_receipts: tuple[Path, Path]
    checkpoint: Path
    inventory: Path


@dataclass(frozen=True)
class AdvanceReport:
    status: Literal["ADVANCED", "EXISTING"]
    phase: Literal["SOURCES_READY"]
    receipt_sha256: str


@dataclass(frozen=True)
class ReceiptReport:
    path: Path
    sha256: str
    receipt_kind: Literal["SOURCE_REVIEW", "CLEAN_SOURCE_REPLAY"]
    commit_path: Path
    commit_sha256: str


@dataclass(frozen=True)
class _UncommittedReceipt:
    path: Path
    sha256: str
    receipt_kind: Literal[
        "SOURCE_REVIEW", "CLEAN_SOURCE_REPLAY", "SOURCE_RECEIPT_COMMIT"
    ]
    snapshot: v52_timezone_review._SafeSnapshot


ReviewReport = ReceiptReport
ReplayReport = ReceiptReport


class FinalizationError(Exception):
    def __init__(
        self, error_code: str, message: str, paths: Sequence[str], exit_code: int
    ) -> None:
        if exit_code not in {3, 4, 5, 6, 7}:
            raise ValueError("finalization error exit code is invalid")
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.paths = tuple(paths)
        self.exit_code = exit_code


class _ParentAuthorityChanged(FinalizationError):
    """A final rebind proved the external protected parent changed."""


def _require_mutable_context(
    context: FinalizationContext, *, phase: str | frozenset[str]
) -> FinalizationContext:
    allowed = frozenset({phase}) if isinstance(phase, str) else phase
    if context.phase not in allowed:
        names = ",".join(sorted(allowed))
        raise FinalizationError("PHASE_BLOCKED", f"phase must be one of {names}", (), 4)
    if context.blockers:
        raise FinalizationError("AUTHORITY_BLOCKED", "finalization context is blocked", (), 4)
    required = (
        context.run_spec,
        context.registry,
        context.checkpoint,
        context.snapshot_inputs,
        context.inventory,
        context.git_head,
    )
    if any(value is None for value in required):
        raise FinalizationError("AUTHORITY_MISSING", "finalization authority is missing", (), 3)
    _source_disposition_payload_fields(context, context.inventory)
    return context


@dataclass(frozen=True)
class _TrustedRootHandle:
    root: Path
    descriptors: tuple[int, ...]
    component_names: tuple[str, ...]


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


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


def _close_descriptors(descriptors: Sequence[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _open_trusted_root(value: Path) -> _TrustedRootHandle:
    supplied = Path(os.path.abspath(os.fspath(value)))
    descriptors: list[int] = []
    names: list[str] = []
    try:
        current = os.open(os.sep, _directory_flags())
        descriptors.append(current)
        for component in supplied.parts[1:]:
            child = os.open(component, _directory_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError(
                        "finalization root ancestors must be no-follow directories"
                    )
            except BaseException:
                os.close(child)
                raise
            descriptors.append(child)
            names.append(component)
            current = child
        return _TrustedRootHandle(supplied, tuple(descriptors), tuple(names))
    except OSError as exc:
        _close_descriptors(descriptors)
        raise ValueError("finalization root is unavailable or unsafe") from exc
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _assert_trusted_root(handle: _TrustedRootHandle) -> None:
    try:
        for index, component in enumerate(handle.component_names):
            parent = handle.descriptors[index]
            child = handle.descriptors[index + 1]
            opened = os.fstat(child)
            linked = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino)
                != (linked.st_dev, linked.st_ino)
            ):
                raise ValueError("finalization root identity changed")
    except OSError as exc:
        raise ValueError("finalization root identity changed") from exc


def _after_trusted_root_open() -> None:
    """Deterministic ancestor-swap test hook."""


def _trusted_root(value: Path) -> Path:
    """Return an absolute root authenticated by a no-follow descriptor chain."""
    handle = _open_trusted_root(value)
    try:
        _assert_trusted_root(handle)
        return handle.root
    finally:
        _close_descriptors(handle.descriptors)


def _relative_under_root(root: Path, value: Path, label: str) -> tuple[str, Path]:
    supplied = os.fspath(value)
    if "\\" in supplied:
        raise ValueError(f"{label} path must use slash separators")
    candidate = Path(supplied)
    if candidate.is_absolute():
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} path must stay under root") from exc
    else:
        relative = candidate
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} path must stay under root")
    return relative.as_posix(), root / relative


def _relative_artifact(root: Path, value: Path, label: str) -> tuple[str, Path]:
    trusted = _trusted_root(root)
    return _relative_under_root(trusted, value, label)


def _read_explicit_artifact(
    root: Path,
    value: Path,
    label: str,
    *,
    max_bytes: int | None = None,
) -> tuple[str, bytes]:
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError(f"{label} bounded read limit is invalid")
    handle = _open_trusted_root(root)
    opened_directories: list[int] = []
    descriptor = -1
    try:
        _after_trusted_root_open()
        _assert_trusted_root(handle)
        relative, _ = _relative_under_root(handle.root, value, label)
        relative_path = Path(relative)
        current = handle.descriptors[-1]
        for component in relative_path.parent.parts:
            child = os.open(component, _directory_flags(), dir_fd=current)
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError(f"{label} ancestor identity changed")
            except BaseException:
                os.close(child)
                raise
            opened_directories.append(child)
            current = child
        before = os.stat(relative_path.name, dir_fd=current, follow_symlinks=False)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(relative_path.name, flags, dir_fd=current)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise ValueError(f"{label} must be an unaliased regular file")
        if max_bytes is not None and opened.st_size > max_bytes:
            raise ValueError(f"{label} exceeds its bounded read limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise ValueError(f"{label} exceeds its bounded read limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(relative_path.name, dir_fd=current, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(rebound):
            raise ValueError(f"{label} identity changed while reading")
        _assert_trusted_root(handle)
        return relative, b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"{label} is unsafe or unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_descriptors(opened_directories)
        _close_descriptors(handle.descriptors)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _canonical_object(raw: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", f"{label} is not JSON", (), 3) from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != raw:
        raise FinalizationError("AUTHORITY_MALFORMED", f"{label} is not canonical", (), 3)
    return value


def _binding(root: Path, value: Path) -> AuthorityBinding:
    relative, raw = _read_explicit_artifact(root, value, "authority")
    return AuthorityBinding(relative, hashlib.sha256(raw).hexdigest())


def _verified_binding(root: Path, value: Path, expected_sha256: str) -> AuthorityBinding:
    binding = _binding(root, value)
    if binding.sha256 != expected_sha256:
        raise FinalizationError("AUTHORITY_CHANGED", "verified binding changed", (binding.path,), 5)
    return binding


def _sources_relative(
    authority: SourceAuthorityContext | None,
) -> Path:
    if authority is None:
        return _SOURCE_ROOT_RELATIVE
    return authority.sources_root.relative_to(authority.repository_root)


def _acquisition_relative(
    authority: SourceAuthorityContext | None,
) -> Path:
    if authority is None:
        return Path("audit/v5_2_qd/historical/acquisition")
    return authority.acquisition_audit_root.relative_to(authority.repository_root)


def _execution_relative(
    authority: SourceAuthorityContext | None,
) -> Path:
    if authority is None:
        return Path("audit/v5_2_qd/historical/execution")
    return authority.execution_audit_root.relative_to(authority.repository_root)


def _source_root(context: FinalizationContext) -> Path:
    return context.root / _sources_relative(context.authority_context)


def _validated_source_root(context: FinalizationContext) -> tuple[Path, Path]:
    source_root = _source_root(context)
    expected = (
        context.authority_context.sources_root
        if context.authority_context is not None
        else context.root / _SOURCE_ROOT_RELATIVE
    )
    v52_snapshot_store.validate_expected_root(source_root, expected)
    return source_root, source_root.relative_to(context.root)


def _review_prefix(context: FinalizationContext) -> str:
    return (
        _acquisition_relative(context.authority_context) / "source_reviews"
    ).as_posix() + "/"


def _replay_prefix(context: FinalizationContext) -> str:
    return (
        _acquisition_relative(context.authority_context) / "source_replays"
    ).as_posix() + "/"


def _trusted_descendant_test_hook(component: str) -> None:
    """Deterministic component-open race boundary for tests."""


def _open_optional_trusted_descendant(
    root: Path, relative: Path
) -> _TrustedRootHandle | None:
    """Open a fixed descendant through a held no-follow directory chain."""
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("trusted descendant path must be canonical and relative")
    root_handle = _open_trusted_root(root)
    descriptors = list(root_handle.descriptors)
    names = list(root_handle.component_names)
    current = descriptors[-1]
    try:
        for component in relative.parts:
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                partial = _TrustedRootHandle(
                    root_handle.root,
                    tuple(descriptors),
                    tuple(names),
                )
                _assert_trusted_root(partial)
                _close_descriptors(descriptors)
                return None
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError("trusted descendant must use no-follow directories")
            except BaseException:
                os.close(child)
                raise
            descriptors.append(child)
            names.append(component)
            current = child
            _trusted_descendant_test_hook(component)
        return _TrustedRootHandle(
            root_handle.root / relative,
            tuple(descriptors),
            tuple(names),
        )
    except OSError as exc:
        _close_descriptors(descriptors)
        raise ValueError("trusted descendant is unsafe or unavailable") from exc
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _open_or_create_source_root(
    context: FinalizationContext | Path,
) -> _TrustedRootHandle:
    """Return the fixed source target through held no-follow directory FDs only.

    A failed attempt may leave empty fixed directories.  They are safe unpublished
    residue and are intentionally never removed: POSIX has no inode-conditional
    ``rmdir``, so deleting by name could remove an attacker's replacement entry.
    """
    if isinstance(context, FinalizationContext):
        source_relative = _sources_relative(context.authority_context)
        repository_root = context.root
    else:
        source_relative = _SOURCE_ROOT_RELATIVE
        repository_root = Path(context)
    root_handle = _open_trusted_root(repository_root)
    descriptors = list(root_handle.descriptors)
    names = list(root_handle.component_names)
    current = descriptors[-1]
    try:
        for component in source_relative.parts:
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    # A concurrent publisher won creation under this same held parent.
                    # Re-open it no-follow below and apply the normal identity checks.
                    pass
                except OSError as exc:
                    raise ValueError("could not safely create fixed source directory") from exc
                try:
                    child = os.open(component, _directory_flags(), dir_fd=current)
                except OSError as exc:
                    raise ValueError("could not safely open fixed source directory") from exc
            descriptors.append(child)
            names.append(component)
            try:
                opened = os.fstat(child)
                linked = os.stat(component, dir_fd=current, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(linked.st_mode)
                    or (opened.st_dev, opened.st_ino)
                    != (linked.st_dev, linked.st_ino)
                ):
                    raise ValueError("fixed source directory must be a no-follow directory")
            except BaseException:
                raise
            os.fsync(current)
            current = child
            _trusted_descendant_test_hook(component)
        result = _TrustedRootHandle(
            root_handle.root / source_relative,
            tuple(descriptors),
            tuple(names),
        )
        _assert_trusted_root(result)
        return result
    except OSError as exc:
        _close_descriptors(descriptors)
        raise ValueError("fixed source directory is unsafe or unavailable") from exc
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _canonical_inventory_exists(source_root: Path) -> bool:
    """Strict path helper retained for diagnostics; publication uses the FD capability."""
    try:
        details = os.stat(
            source_root / "source_inventory.json", follow_symlinks=False
        )
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FinalizationError(
            "PATH_UNSAFE", "published inventory cannot be safely inspected", (), 5
        ) from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise FinalizationError(
            "PATH_UNSAFE", "published inventory must be a regular single-link file", (), 5
        )
    return True


def _source_status_test_hook(stage: str) -> None:
    """Deterministic source-root namespace race boundary for tests."""


def _git_head(root: Path) -> str:
    try:
        return subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FinalizationError("AUTHORITY_MISSING", "Git head is unavailable", (), 3) from exc


def _checkpoint_path(
    checkpoint: AcquisitionCheckpoint,
    authority: SourceAuthorityContext | None = None,
) -> Path:
    root = _CHECKPOINT_ROOT if authority is None else (
        _execution_relative(authority) / "acquisition_checkpoints"
    )
    return root / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"


def _authority_bindings(
    root: Path,
    run_spec_path: Path,
    contract_path: Path,
    checkpoint: AcquisitionCheckpoint,
    replay: object,
    authority: SourceAuthorityContext | None = None,
) -> AuthorityBindings:
    schedule = getattr(replay, "schedule_inventory")
    games = getattr(replay, "game_inventory")
    timezone = getattr(replay, "timezone_gate")
    is_r3 = getattr(replay, "run_id", None) == _R3_RECOVERY_RUN_ID
    disposition = (
        None
        if is_r3
        else getattr(replay, "duplicate_gamepk_disposition", None)
    )
    dispositioned_query_ids = (
        ()
        if is_r3
        else tuple(getattr(replay, "dispositioned_query_ids", ()))
    )
    terminal = TerminalCheckpointBinding(
        _checkpoint_path(checkpoint, authority).as_posix(),
        checkpoint.receipt_sha256,
        checkpoint.sequence,
        "REMAINING",
    )
    r3_capability = (
        make_r3_snapshot_store_capability(replay, terminal.as_mapping())
        if is_r3
        else None
    )
    return AuthorityBindings(
        run_spec=_binding(root, run_spec_path),
        acquisition_contract=_binding(root, contract_path),
        terminal_checkpoint=terminal,
        schedule_inventory=_verified_binding(root, schedule.path, schedule.sha256),
        game_evidence_inventory=_verified_binding(root, games.path, games.sha256),
        timezone_gate=_verified_binding(
            root,
            root
            / (
                _TIMEZONE_SEAL
                if authority is None
                else _acquisition_relative(authority)
                / "venue_timezone_seal.json"
            ),
            timezone.seal_sha256,
        ),
        duplicate_gamepk_disposition=disposition,
        dispositioned_query_ids=dispositioned_query_ids,
        r3_snapshot_store_capability=r3_capability,
    )


def _authority_changed(message: str) -> FinalizationError:
    return FinalizationError("AUTHORITY_CHANGED", message, (), 5)


def _legacy_evidence_for_context(
    common_args: CommonArgs,
    root: Path,
    protected_path: Path,
    run_spec: RunSpec | SourceRunSpec,
) -> LegacyEvidenceCapability | None:
    is_r3 = (
        isinstance(run_spec, SourceRunSpec)
        and run_spec.run_id == _R3_RECOVERY_RUN_ID
    )
    supplied = common_args.legacy_root
    if supplied is None:
        if is_r3:
            raise FinalizationError(
                "AUTHORITY_MISSING", "r3 requires --legacy-root", (), 3
            )
        return None
    physical = supplied if supplied.is_absolute() else root / supplied
    if not is_r3:
        if physical != root:
            raise FinalizationError(
                "AUTHORITY_BLOCKED",
                "external legacy root is available only for r3",
                (),
                3,
            )
        return None
    try:
        return build_external_protected_parent_capability(
            ExternalProtectedParentInputs(
                target_root=root,
                physical_raw_root=physical,
                target_protected_inventory_path=protected_path,
                run_spec_path=(
                    common_args.run_spec
                    if common_args.run_spec.is_absolute()
                    else root / common_args.run_spec
                ),
                acquisition_contract_path=(
                    common_args.acquisition_contract
                    if common_args.acquisition_contract.is_absolute()
                    else root / common_args.acquisition_contract
                ),
            )
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc


def _legacy_loader_kwargs(
    legacy_evidence: LegacyEvidenceCapability | None,
) -> dict[str, LegacyEvidenceCapability]:
    return {} if legacy_evidence is None else {"legacy_evidence": legacy_evidence}


def _rebind_context_legacy(context: FinalizationContext) -> None:
    if context.legacy_evidence is None:
        return
    try:
        context.legacy_evidence.rebind()
    except (OSError, RuntimeError, ValueError) as exc:
        raise _ParentAuthorityChanged(
            "POSTCONDITION_FAILED", "protected parent changed", (), 5
        ) from exc


def _rebind_context_authorities(
    root: Path,
    run_spec_path: Path,
    contract_path: Path,
    protected_path: Path,
    run_spec: RunSpec | SourceRunSpec,
    registry: ExecutionRegistryState,
    checkpoint: AcquisitionCheckpoint,
    inventory: SourceInventory,
    *,
    initial_run_spec_bytes: bytes,
    initial_contract_bytes: bytes,
    initial_membership: object,
    initial_inventory_bytes: bytes,
    initial_git_head: str,
    authority_context: SourceAuthorityContext | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> None:
    """Re-authenticate all inputs and rebuild the candidate inventory."""
    try:
        _, final_run_spec_bytes = _read_explicit_artifact(root, run_spec_path, "RunSpec")
        _, final_contract_bytes = _read_explicit_artifact(
            root, contract_path, "acquisition contract"
        )
        _canonical_object(final_run_spec_bytes, "RunSpec")
        _canonical_object(final_contract_bytes, "acquisition contract")
        _load_static_contracts(run_spec_path, contract_path)
    except FinalizationError as exc:
        raise _authority_changed(
            f"static authority changed during context build: {exc.message}"
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _authority_changed("static authority changed during context build") from exc
    if final_run_spec_bytes != initial_run_spec_bytes:
        raise _authority_changed("RunSpec changed during context build")
    if final_contract_bytes != initial_contract_bytes:
        raise _authority_changed("acquisition contract changed during context build")

    try:
        protected = verify_protected_raw_inventory(root, protected_path)
        final_membership = snapshot_protected_inventory_membership(root, protected)
    except (OSError, ValueError, RuntimeError) as exc:
        raise _authority_changed("protected inventory changed during context build") from exc
    if final_membership != initial_membership:
        raise _authority_changed("protected membership changed during context build")

    try:
        final_registry = load_execution_registry(
            root, run_spec, **_legacy_loader_kwargs(legacy_evidence)
        )
        final_checkpoint = load_source_verification_checkpoint(
            root, run_spec, **_legacy_loader_kwargs(legacy_evidence)
        )
    except (OSError, ValueError, FileNotFoundError) as exc:
        raise _authority_changed("registry authority changed during context build") from exc
    if final_registry != registry:
        raise _authority_changed("registry changed during context build")
    if final_checkpoint != checkpoint:
        raise _authority_changed("checkpoint changed during context build")

    try:
        replay = load_snapshot_replay_inputs(
            root,
            run_spec,
            acquisition_contract_path=contract_path,
            **_legacy_loader_kwargs(legacy_evidence),
        )
        rebound = build_inventory(
            SnapshotStoreInputs(
                replay,
                _authority_bindings(
                    root,
                    run_spec_path,
                    contract_path,
                    checkpoint,
                    replay,
                    authority_context,
                ),
                registry.phase,
            )
        )
        final_inventory_bytes = inventory_bytes(rebound)
        final_git_head = _git_head(root)
    except FinalizationError as exc:
        if exc.error_code == "AUTHORITY_CHANGED":
            raise
        raise _authority_changed(
            f"source inventory authority changed during context build: {exc.message}"
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _authority_changed("source inventory authority changed during context build") from exc
    if final_inventory_bytes != initial_inventory_bytes:
        raise _authority_changed("source inventory changed during context build")
    if final_git_head != initial_git_head:
        raise _authority_changed("Git head changed during context build")
    if legacy_evidence is not None:
        try:
            legacy_evidence.rebind()
        except (OSError, RuntimeError, ValueError) as exc:
            raise _authority_changed("protected parent changed during context build") from exc


def build_context(common_args: CommonArgs) -> FinalizationContext:
    """Build only authenticated read-only authorities; never create state."""
    root = _trusted_root(common_args.root)
    _, run_spec_path = _relative_artifact(root, common_args.run_spec, "RunSpec")
    _, contract_path = _relative_artifact(
        root, common_args.acquisition_contract, "acquisition contract"
    )
    _, protected_path = _relative_artifact(
        root, common_args.protected_inventory, "protected inventory"
    )
    legacy_evidence: LegacyEvidenceCapability | None = None
    try:
        _, run_spec_bytes = _read_explicit_artifact(root, run_spec_path, "RunSpec")
        _, contract_bytes = _read_explicit_artifact(
            root, contract_path, "acquisition contract"
        )
        _canonical_object(run_spec_bytes, "RunSpec")
        _canonical_object(contract_bytes, "acquisition contract")
        authority_context = _load_static_contracts(run_spec_path, contract_path)
        if isinstance(authority_context, SourceAuthorityContext):
            if authority_context.repository_root != root:
                raise ValueError("source authority repository root differs")
            run_spec = authority_context.authority
        else:
            # Preserve the established unit seam for a mocked static loader.
            run_spec = parse_run_spec(json.loads(run_spec_bytes))
        if isinstance(run_spec, SourceRunSpec) and not isinstance(
            authority_context, SourceAuthorityContext
        ):
            raise FinalizationError(
                "AUTHORITY_MALFORMED",
                "superseding source authority context is unavailable",
                (str(run_spec_path), str(contract_path)),
                3,
            )
        protected = verify_protected_raw_inventory(root, protected_path)
        membership = snapshot_protected_inventory_membership(root, protected)
        legacy_evidence = _legacy_evidence_for_context(
            common_args, root, protected_path, run_spec
        )
    except FinalizationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc

    try:
        registry = load_execution_registry(
            root, run_spec, **_legacy_loader_kwargs(legacy_evidence)
        )
    except FileNotFoundError:
        return FinalizationContext(
            root, run_spec_path, contract_path, protected_path, run_spec, None, None,
            None, None, "ABSENT", None,
            (Blocker("REGISTRY_ABSENT", "execution registry is absent"),),
            authority_context if isinstance(authority_context, SourceAuthorityContext) else None,
            legacy_evidence,
        )
    except (OSError, ValueError) as exc:
        if legacy_evidence is not None:
            legacy_evidence.close()
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc

    try:
        checkpoint = load_source_verification_checkpoint(
            root, run_spec, **_legacy_loader_kwargs(legacy_evidence)
        )
    except FileNotFoundError:
        return FinalizationContext(
            root, run_spec_path, contract_path, protected_path, run_spec, registry, None,
            None, None, registry.phase, None,
            (Blocker("CHECKPOINT_UNAVAILABLE", "terminal checkpoint is unavailable"),),
            authority_context if isinstance(authority_context, SourceAuthorityContext) else None,
            legacy_evidence,
        )
    except (OSError, ValueError) as exc:
        if legacy_evidence is not None:
            legacy_evidence.close()
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc

    try:
        replay = load_snapshot_replay_inputs(
            root, run_spec, acquisition_contract_path=contract_path,
            **_legacy_loader_kwargs(legacy_evidence),
        )
        bindings = _authority_bindings(
            root,
            run_spec_path,
            contract_path,
            checkpoint,
            replay,
            authority_context
            if isinstance(authority_context, SourceAuthorityContext)
            else None,
        )
        inputs = SnapshotStoreInputs(replay, bindings, registry.phase)
        inventory = build_inventory(inputs)
        git_head = _git_head(root)
        candidate_inventory_bytes = inventory_bytes(inventory)
        _rebind_context_authorities(
            root, run_spec_path, contract_path, protected_path, run_spec, registry,
            checkpoint, inventory,
            initial_run_spec_bytes=run_spec_bytes,
            initial_contract_bytes=contract_bytes,
            initial_membership=membership,
            initial_inventory_bytes=candidate_inventory_bytes,
            initial_git_head=git_head,
            authority_context=(
                authority_context
                if isinstance(authority_context, SourceAuthorityContext)
                else None
            ),
            legacy_evidence=legacy_evidence,
        )
    except FinalizationError:
        if legacy_evidence is not None:
            legacy_evidence.close()
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if legacy_evidence is not None:
            legacy_evidence.close()
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc
    if legacy_evidence is not None:
        try:
            legacy_evidence.rebind()
        except (OSError, RuntimeError, ValueError) as exc:
            legacy_evidence.close()
            raise FinalizationError(
                "AUTHORITY_CHANGED", "protected parent changed during context build", (), 5
            ) from exc
    return FinalizationContext(
        root, run_spec_path, contract_path, protected_path, run_spec, registry,
        checkpoint, inputs, inventory, registry.phase, git_head, (),
        authority_context
        if isinstance(authority_context, SourceAuthorityContext)
        else None,
        legacy_evidence,
    )


def _checkpoint_status(checkpoint: AcquisitionCheckpoint | None) -> Mapping[str, object] | None:
    if checkpoint is None:
        return None
    return {
        "sequence": checkpoint.sequence,
        "stage": checkpoint.stage,
        "head": checkpoint.receipt_sha256,
        "pending_count": len(checkpoint.pending_query_ids),
        "quarantine_count": len(checkpoint.quarantined_query_ids),
    }


def _exact_authority_branch(context: FinalizationContext) -> str:
    """Resolve only one immutable RunSpec/contract path pair.

    The ordering is intentional: old authorities never inspect recovery-only
    capabilities, so their receipt bytes retain their historical shape.
    """
    try:
        pair = (
            context.run_spec_path.relative_to(context.root).as_posix(),
            context.acquisition_contract_path.relative_to(context.root).as_posix(),
        )
    except ValueError as exc:
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "source authority paths differ", (), 4
        ) from exc
    if pair == _HISTORICAL_AUTHORITY_PAIR:
        return "historical"
    if pair == _SUPERSEDING_565534_AUTHORITY_PAIR:
        return "superseding_565534"
    if pair == _RECOVERY_SCHEDULE_CONFLICTS_AUTHORITY_PAIR:
        return "recovery_schedule_conflicts"
    if pair == _R3_RECOVERY_AUTHORITY_PAIR:
        return "recovery_schedule_conflicts_missing_venue"
    raise FinalizationError(
        "AUTHORITY_BLOCKED", "source authority path pair differs", (), 4
    )


def _recovery_checkpoint_binding(entry: object) -> dict[str, object]:
    return {
        "game_pk": entry.game_pk,
        "query_id": entry.query_id,
        "query_sidecar": dict(entry.query_sidecar),
        "success_receipt": dict(entry.success_receipt),
        "retained_scheduled_start_utc": entry.retained_scheduled_start_utc,
        "played_date": entry.played_date,
        "result_available_on": entry.result_available_on,
        "raw_object_sha256s": list(entry.raw_object_sha256s),
        "selected_raw_game_object_sha256s_sha256": (
            entry.selected_raw_game_object_sha256s_sha256
        ),
        "normalized_games_sha256": entry.normalized_games_sha256,
        "sidecar_semantics_sha256": entry.sidecar_semantics_sha256,
        "expected_derived_sidecar_sha256": entry.expected_derived_sidecar_sha256,
    }


def _recovery_disposition_payload_fields(
    context: FinalizationContext, inventory: SourceInventory | None
) -> dict[str, object]:
    if inventory is None or context.snapshot_inputs is None or context.checkpoint is None:
        raise FinalizationError(
            "AUTHORITY_MISSING", "recovery disposition authority is missing", (), 3
        )
    catalog = inventory.schedule_conflict_dispositions
    if not isinstance(catalog, RecoveryDispositionBindings):
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "recovery disposition catalog differs", (), 4
        )
    try:
        fresh = catalog.reauthenticate()
    except ValueError as exc:
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "recovery disposition catalog differs", (), 4
        ) from exc
    if not isinstance(fresh, RecoveryDispositionBindings) or fresh is catalog:
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "recovery disposition catalog differs", (), 4
        )
    expected = fresh.as_mapping()
    query_ids = tuple(expected["query_ids"])
    entries = fresh.entries
    checkpoint_bindings = tuple(
        _recovery_checkpoint_binding(entry) for entry in entries
    )
    availability_entries = tuple(
        entry for entry in entries if entry.played_date is not None or entry.result_available_on is not None
    )
    replay = context.snapshot_inputs.replay
    bindings = context.snapshot_inputs.bindings
    if (
        len(entries) != 3
        or tuple(entry.game_pk for entry in entries) != (565534, 630882, 633224)
        or any(
            (entry.game_pk in {630882, 633224})
            != (entry.played_date is not None and entry.result_available_on is not None)
            for entry in entries
        )
        or len(availability_entries) != 2
        or tuple(entry.game_pk for entry in availability_entries) != (630882, 633224)
        or bindings.schedule_conflict_dispositions is None
        or replay.recovery_dispositions is None
        or bindings.schedule_conflict_dispositions.as_mapping() != expected
        or replay.recovery_dispositions.as_mapping() != expected
        or context.checkpoint.stage != "REMAINING"
        or context.checkpoint.dispositioned_query_ids != query_ids
        or tuple(context.checkpoint.recovery_disposition_bindings) != checkpoint_bindings
        or any(
            query_id not in context.checkpoint.completed_query_ids
            for query_id in query_ids
        )
        or context.checkpoint.pending_query_ids
        or context.checkpoint.quarantined_query_ids
        or context.checkpoint.split_query_ids
    ):
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "recovery disposition authority differs", (), 4
        )
    return {"schedule_conflict_dispositions": expected}


def _r3_source_readiness(
    context: FinalizationContext,
    inventory: SourceInventory | None,
) -> dict[str, object]:
    """Authenticate the sealed r3 compact projection for source operations."""
    if (
        inventory is None
        or context.snapshot_inputs is None
        or context.checkpoint is None
    ):
        raise FinalizationError(
            "AUTHORITY_MISSING", "r3 disposition authority is missing", (), 3
        )
    authority = context.authority_context
    inputs = context.snapshot_inputs
    replay = inputs.replay
    bindings = inputs.bindings
    terminal = bindings.terminal_checkpoint
    capability = bindings.r3_snapshot_store_capability
    replay_checkpoint = getattr(getattr(replay, "graph", None), "checkpoint", None)
    if (
        not isinstance(context.run_spec, SourceRunSpec)
        or context.run_spec.run_id != _R3_RECOVERY_RUN_ID
        or not isinstance(authority, SourceAuthorityContext)
        or authority.authority != context.run_spec
        or authority.run_id != _R3_RECOVERY_RUN_ID
        or authority.repository_root != context.root
        or authority.authority_path != context.run_spec_path
        or authority.acquisition_contract_path
        != context.acquisition_contract_path
        or replay.run_id != _R3_RECOVERY_RUN_ID
        or replay_checkpoint != context.checkpoint
        or inventory.run_id != _R3_RECOVERY_RUN_ID
        or inventory.r3_snapshot_store_capability is not capability
        or context.checkpoint.stage != "REMAINING"
        or context.checkpoint.pending_query_ids
        or context.checkpoint.quarantined_query_ids
        or context.checkpoint.split_query_ids
        or terminal.sequence != context.checkpoint.sequence
        or terminal.sha256 != context.checkpoint.receipt_sha256
        or terminal.stage != "REMAINING"
    ):
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "r3 disposition authority differs", (), 4
        )
    try:
        validated = validate_r3_snapshot_store_capability(
            capability,
            replay,
            terminal.as_mapping(),
        )
        expected = {
            "excluded_game_pks": [632924],
            "projection_query_ids": list(replay.dispositioned_query_ids),
            "record_game_pks": [565534, 630882, 633224, 632924],
            "record_to_projection_indexes": [0, 1, 2, 2],
        }
        if (
            canonical_json_bytes(dict(validated))
            != canonical_json_bytes(expected)
            or inventory.as_mapping().get("schedule_conflict_dispositions")
            != expected
            or tuple(replay.dispositioned_query_ids)
            != tuple(context.checkpoint.dispositioned_query_ids)
            or len(replay.dispositioned_query_ids) != 3
            or any(
                query_id not in context.checkpoint.completed_query_ids
                for query_id in replay.dispositioned_query_ids
            )
        ):
            raise ValueError("r3 compact disposition mapping differs")
    except (TypeError, ValueError) as exc:
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "r3 disposition authority differs", (), 4
        ) from exc
    return expected


def _validate_source_readiness(
    context: FinalizationContext,
    inventory: SourceInventory | None,
) -> None:
    """Validate source authority without coupling preflight to receipt shape."""
    if (
        isinstance(context.run_spec, SourceRunSpec)
        and context.run_spec.run_id == _R3_RECOVERY_RUN_ID
    ):
        _r3_source_readiness(context, inventory)
        return
    _source_disposition_payload_fields(context, inventory)


def _source_disposition_payload_fields(
    context: FinalizationContext, inventory: SourceInventory | None
) -> dict[str, object]:
    """Return the closed branch-specific disposition receipt binding."""
    if not isinstance(context.run_spec, SourceRunSpec):
        return {}
    branch = _exact_authority_branch(context)
    if branch == "historical":
        raise FinalizationError(
            "AUTHORITY_BLOCKED", "source authority path pair differs", (), 4
        )
    if branch == "recovery_schedule_conflicts":
        authority = context.authority_context
        if (
            not isinstance(context.run_spec, SourceRunSpec)
            or context.run_spec.run_id != _RECOVERY_RUN_ID
            or not isinstance(authority, SourceAuthorityContext)
            or authority.authority != context.run_spec
            or authority.repository_root != context.root
            or authority.authority_path != context.run_spec_path
            or authority.acquisition_contract_path != context.acquisition_contract_path
        ):
            raise FinalizationError(
                "AUTHORITY_BLOCKED", "recovery source authority differs", (), 4
            )
        return _recovery_disposition_payload_fields(context, inventory)
    if branch == "recovery_schedule_conflicts_missing_venue":
        return {
            "schedule_conflict_dispositions": _r3_source_readiness(
                context, inventory
            )
        }
    if branch != "superseding_565534" or not isinstance(context.run_spec, SourceRunSpec):
        raise FinalizationError("AUTHORITY_BLOCKED", "source authority differs", (), 4)
    authority = context.authority_context
    if (
        not isinstance(authority, SourceAuthorityContext)
        or authority.authority != context.run_spec
        or authority.repository_root != context.root
    ):
        raise FinalizationError(
            "AUTHORITY_BLOCKED",
            "superseding source authority context differs",
            (),
            4,
        )
    if inventory is None or context.snapshot_inputs is None or context.checkpoint is None:
        raise FinalizationError(
            "AUTHORITY_MISSING", "superseding disposition authority is missing", (), 3
        )
    disposition = inventory.duplicate_gamepk_disposition
    query_ids = inventory.dispositioned_query_ids
    replay = context.snapshot_inputs.replay
    bindings = context.snapshot_inputs.bindings
    if (
        disposition is None
        or query_ids != (disposition.query_id,)
        or replay.duplicate_gamepk_disposition != disposition
        or replay.dispositioned_query_ids != query_ids
        or bindings.duplicate_gamepk_disposition != disposition
        or bindings.dispositioned_query_ids != query_ids
        or context.checkpoint.dispositioned_query_ids != query_ids
        or disposition.query_id not in context.checkpoint.completed_query_ids
        or disposition.query_id in context.checkpoint.pending_query_ids
        or disposition.query_id in context.checkpoint.quarantined_query_ids
        or disposition.query_id in context.checkpoint.split_query_ids
    ):
        raise FinalizationError(
            "AUTHORITY_BLOCKED",
            "superseding disposition authority differs from source evidence",
            (),
            4,
        )
    return {
        "duplicate_gamepk_disposition": disposition.as_mapping(),
        "dispositioned_query_ids": list(query_ids),
    }


def _rebind_source_disposition_payload(
    context: FinalizationContext,
    inventory: SourceInventory,
    payload: Mapping[str, object],
) -> None:
    """Keep source-only receipt fields bound through the final write boundary."""
    expected = _source_disposition_payload_fields(context, inventory)
    if not expected:
        return
    if any(payload.get(key) != value for key, value in expected.items()):
        raise FinalizationError(
            "POSTCONDITION_FAILED",
            "superseding disposition receipt binding changed before publication",
            (),
            5,
        )
    if (
        isinstance(context.run_spec, SourceRunSpec)
        and context.run_spec.run_id == _R3_RECOVERY_RUN_ID
    ):
        try:
            inputs = context.snapshot_inputs
            if inputs is None:
                raise ValueError("r3 snapshot inputs are missing")
            capability = inputs.bindings.r3_snapshot_store_capability
            rebind_r3_snapshot_store_capability(
                capability,
                inputs.replay,
                inputs.bindings.terminal_checkpoint.as_mapping(),
            )
        except (TypeError, ValueError) as exc:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "r3 disposition authority changed before publication",
                (),
                5,
            ) from exc


def _has_terminal_checkpoint(checkpoint: AcquisitionCheckpoint | None) -> bool:
    return bool(
        checkpoint is not None
        and checkpoint.stage == "REMAINING"
        and not checkpoint.pending_query_ids
        and not checkpoint.quarantined_query_ids
        and not checkpoint.split_query_ids
    )


def _context_is_ready_for_mutation(context: FinalizationContext) -> bool:
    ready = bool(
        not context.blockers
        and context.run_spec is not None
        and context.registry is not None
        and context.checkpoint is not None
        and context.snapshot_inputs is not None
        and context.inventory is not None
        and context.git_head is not None
        and context.registry.phase == context.phase
        and _has_terminal_checkpoint(context.checkpoint)
    )
    if not ready:
        return False
    try:
        _validate_source_readiness(context, context.inventory)
    except FinalizationError:
        return False
    return True


def _published_inventory_matches(
    context: FinalizationContext,
) -> tuple[bool, Blocker | None]:
    """Authenticate the already-published inventory without repairing it."""
    if context.inventory is None or context.snapshot_inputs is None:
        return False, None
    source_root, source_relative = _validated_source_root(context)
    try:
        _source_status_test_hook("before_source_root_open")
        handle = _open_optional_trusted_descendant(
            context.root, source_relative
        )
        if handle is None:
            return False, None
        try:
            _source_status_test_hook("after_source_root_open")
            _assert_trusted_root(handle)
            if not os.listdir(handle.descriptors[-1]):
                _source_status_test_hook("after_empty_source_root_read")
                _assert_trusted_root(handle)
                if os.listdir(handle.descriptors[-1]):
                    raise ValueError("empty source root changed while reading")
                return False, None
            loaded = load_snapshot_store_from_fd(
                handle.descriptors[-1], context.snapshot_inputs
            )
            _source_status_test_hook("after_source_store_load")
            _assert_trusted_root(handle)
        finally:
            _close_descriptors(handle.descriptors)
    except (SnapshotStoreError, OSError, ValueError, TypeError):
        return False, Blocker(
            "PUBLISHED_INVENTORY_UNAUTHENTICATED",
            "published source inventory is unsafe or unavailable",
            (source_root.relative_to(context.root).as_posix(),),
        )
    if inventory_bytes(loaded.inventory) != inventory_bytes(context.inventory):
        return False, Blocker(
            "PUBLISHED_INVENTORY_MISMATCH",
            "published source inventory differs from the candidate inventory",
            (source_root.relative_to(context.root).as_posix(),),
        )
    return True, None


def read_status(context: FinalizationContext) -> StatusReport:
    """Return advisory readiness without selecting receipts or changing phase."""
    blockers = list(context.blockers)
    base_ready = _context_is_ready_for_mutation(context)
    published_matches = False
    if base_ready and context.phase == "ACQUIRING":
        published_matches, publication_blocker = _published_inventory_matches(context)
        if publication_blocker is not None:
            blockers.append(publication_blocker)
    mutable = base_ready and not blockers and context.phase == "ACQUIRING"
    return StatusReport(
        context.phase,
        _checkpoint_status(context.checkpoint),
        mutable,
        mutable and published_matches,
        mutable and published_matches,
        False,
        tuple(blockers),
    )


def _rebuild_context_inventory(context: FinalizationContext) -> SourceInventory:
    if (
        context.run_spec is None
        or context.registry is None
        or context.checkpoint is None
    ):
        raise FinalizationError(
            "AUTHORITY_MISSING", "finalization authority is missing", (), 3
        )
    _rebind_context_legacy(context)
    try:
        replay = load_snapshot_replay_inputs(
            context.root,
            context.run_spec,
            acquisition_contract_path=context.acquisition_contract_path,
            **_legacy_loader_kwargs(context.legacy_evidence),
        )
        bindings = _authority_bindings(
            context.root,
            context.run_spec_path,
            context.acquisition_contract_path,
            context.checkpoint,
            replay,
            context.authority_context,
        )
        rebound = build_inventory(
            SnapshotStoreInputs(replay, bindings, context.registry.phase)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _authority_changed("source inventory changed during reconstruction") from exc
    _rebind_context_legacy(context)
    return rebound


def run_preflight(context: FinalizationContext) -> PreflightReport:
    """Rebuild and compare the candidate inventory without writing any state."""
    if context.blockers or context.inventory is None:
        return PreflightReport(
            context.phase, "BLOCKED", None, 0, 0, context.blockers
        )
    _validate_source_readiness(context, context.inventory)
    first = inventory_bytes(context.inventory)
    rebound = _rebuild_context_inventory(context)
    _validate_source_readiness(context, rebound)
    second = inventory_bytes(rebound)
    if first != second:
        raise FinalizationError(
            "AUTHORITY_CHANGED", "inventory changed during preflight", (), 5
        )
    return PreflightReport(
        context.phase,
        "PASS",
        hashlib.sha256(first).hexdigest(),
        len(context.inventory.snapshots),
        0,
        (),
    )


def _load_published_inventory_from_handle(
    handle: _TrustedRootHandle, context: FinalizationContext
) -> SourceInventory:
    if context.snapshot_inputs is None:
        raise FinalizationError("AUTHORITY_MISSING", "snapshot inputs are missing", (), 3)
    try:
        _assert_trusted_root(handle)
        loaded = load_snapshot_store_from_fd(
            handle.descriptors[-1], context.snapshot_inputs
        )
        _assert_trusted_root(handle)
        return loaded.inventory
    except LockHeld:
        raise
    except SnapshotStoreError as exc:
        raise FinalizationError(
            "POSTCONDITION_FAILED", "published source inventory is unsafe", (), 5
        ) from exc


def _converge_identical_locked_publish(
    handle: _TrustedRootHandle, context: FinalizationContext, expected: bytes
) -> PublishReport:
    """Wait briefly for a competing publisher, never writing while its lock exists."""
    for _ in range(25):
        try:
            inventory = _load_published_inventory_from_handle(handle, context)
        except LockHeld:
            time.sleep(0.02)
            continue
        except FinalizationError as load_error:
            try:
                _assert_trusted_root(handle)
                inventory = read_inventory_from_fd(
                    handle.descriptors[-1],
                    recovery_dispositions=(
                        context.snapshot_inputs.bindings.schedule_conflict_dispositions
                        if context.snapshot_inputs is not None
                        else None
                    ),
                    r3_inputs=(
                        context.snapshot_inputs
                        if context.snapshot_inputs is not None
                        and context.snapshot_inputs.replay.run_id
                        == _R3_RECOVERY_RUN_ID
                        else None
                    ),
                )
                _assert_trusted_root(handle)
            except LockHeld:
                time.sleep(0.02)
                continue
            except SnapshotStoreError:
                raise load_error
        actual = inventory_bytes(inventory)
        if actual != expected:
            raise FinalizationError(
                "PUBLISH_CONFLICT", "competing publication differs from candidate", (), 6
            )
        return PublishReport(
            "EXISTING", hashlib.sha256(actual).hexdigest(), len(inventory.snapshots)
        )
    raise FinalizationError(
        "PUBLISH_CONFLICT", "source publication lock remains held", (), 6
    )


def publish_inventory(
    context: FinalizationContext, *, lock_owner: str
) -> PublishReport:
    """Publish only the preflighted candidate to the fixed v5.2 source target."""
    _require_mutable_context(context, phase="ACQUIRING")
    if not _has_terminal_checkpoint(context.checkpoint):
        raise FinalizationError(
            "TERMINAL_CHECKPOINT_REQUIRED",
            "publication requires a zero-pending, zero-quarantine REMAINING checkpoint",
            (),
            4,
        )
    preflight = run_preflight(context)
    if (
        preflight.status != "PASS"
        or context.inventory is None
        or context.snapshot_inputs is None
    ):
        raise FinalizationError(
            "PREFLIGHT_BLOCKED", "publication requires a passing preflight", (), 4
        )
    expected = inventory_bytes(context.inventory)
    try:
        _validated_source_root(context)
        handle = _open_or_create_source_root(context)
    except ValueError as exc:
        raise FinalizationError("PATH_UNSAFE", str(exc), (), 5) from exc
    try:
        try:
            outcome = publish_snapshot_store_from_fd(
                handle.descriptors[-1],
                context.inventory,
                context.snapshot_inputs,
                lock_owner=lock_owner,
                **(
                    {"publication_state_verifier": context.legacy_evidence.rebind}
                    if context.legacy_evidence is not None
                    else {}
                ),
            )
        except LockHeld:
            return _converge_identical_locked_publish(handle, context, expected)
        except PublishConflict as exc:
            raise FinalizationError("PUBLISH_CONFLICT", str(exc), (), 6) from exc
        except (PublishError, ValueError, TypeError, OSError) as exc:
            raise FinalizationError("PUBLISH_FAILED", str(exc), (), 5) from exc

        inventory = _load_published_inventory_from_handle(handle, context)
        actual = inventory_bytes(inventory)
        if actual != expected:
            raise FinalizationError(
                "POSTCONDITION_FAILED", "published inventory bytes differ from candidate", (), 5
            )
        status: Literal["PUBLISHED", "EXISTING"] = (
            "PUBLISHED" if outcome.created else "EXISTING"
        )
        return PublishReport(
            status, outcome.receipt.inventory_sha256, outcome.receipt.snapshot_count
        )
    finally:
        _close_descriptors(handle.descriptors)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _execution_id() -> str:
    """Return a fresh canonical UUIDv4 without consulting the filesystem."""
    raw = bytearray(secrets.token_bytes(16))
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def replay_verified_snapshots(
    inputs: SnapshotReplayInputs,
) -> tuple[NormalizedSnapshot, ...]:
    """Late-bind the pure snapshot engine so isolated replay tests stay hermetic."""
    return v52_snapshot_store.replay_verified_snapshots(inputs)


def _validate_receipt_destination(
    root: Path, path: Path, *, expected_prefix: str, label: str
) -> str:
    """Reject non-explicit destinations before replaying any source evidence."""
    try:
        relative = v52_timezone_review._relative(root, path, label)
    except ValueError as exc:
        raise FinalizationError("PATH_UNSAFE", str(exc), (), 5) from exc
    lexical = relative.as_posix()
    if (
        not lexical.startswith(expected_prefix)
        or len(relative.parts) <= len(Path(expected_prefix).parts) - 1
        or relative.name in {"", ".", ".."}
    ):
        raise FinalizationError(
            "PATH_UNSAFE", f"{label} is outside its namespace", (lexical,), 5
        )
    return lexical


def _load_clean_replay_inventory(context: FinalizationContext) -> SourceInventory:
    """Authenticate the published store and reproduce its six-engine projection."""
    if context.snapshot_inputs is None:
        raise FinalizationError("AUTHORITY_MISSING", "snapshot inputs are missing", (), 3)
    _rebind_context_legacy(context)
    try:
        _, source_relative = _validated_source_root(context)
        handle = _open_optional_trusted_descendant(context.root, source_relative)
    except ValueError as exc:
        raise FinalizationError("PATH_UNSAFE", str(exc), (), 5) from exc
    if handle is None:
        raise FinalizationError(
            "REPLAY_MISMATCH", "published source inventory is unavailable", (), 5
        )
    try:
        _assert_trusted_root(handle)
        loaded = load_snapshot_store_from_fd(handle.descriptors[-1], context.snapshot_inputs)
        _assert_trusted_root(handle)
        replayed = replay_verified_snapshots(context.snapshot_inputs.replay)
        rebuilt = build_inventory(context.snapshot_inputs)
    except (LockHeld, SnapshotStoreError, OSError, TypeError, ValueError) as exc:
        raise FinalizationError(
            "REPLAY_MISMATCH", "clean replay projection differs", (), 5
        ) from exc
    finally:
        _close_descriptors(handle.descriptors)
    if (
        replayed != loaded.snapshots
        or inventory_bytes(rebuilt) != inventory_bytes(loaded.inventory)
    ):
        raise FinalizationError(
            "REPLAY_MISMATCH", "clean replay projection differs", (), 5
        )
    _rebind_context_legacy(context)
    return loaded.inventory


def _clean_replay_payload(
    context: FinalizationContext, inventory: SourceInventory, execution_id: str
) -> dict[str, object]:
    if (
        context.run_spec is None
        or context.snapshot_inputs is None
        or context.git_head is None
    ):
        raise FinalizationError("AUTHORITY_MISSING", "replay authority is missing", (), 3)
    return {
        "schema_version": 1,
        "receipt_kind": "CLEAN_SOURCE_REPLAY",
        "run_id": context.run_spec.run_id,
        "critical_errors": [],
        "execution_id": execution_id,
        "status": "PASS",
        "network_attempts": 0,
        "reviewed_query_ids": list(inventory.logical_query_ids),
        "inventory_sha256": hashlib.sha256(inventory_bytes(inventory)).hexdigest(),
        "snapshot_sha256s": list(inventory.snapshot_sha256s),
        "run_spec": context.snapshot_inputs.bindings.run_spec.as_mapping(),
        "acquisition_contract": (
            context.snapshot_inputs.bindings.acquisition_contract.as_mapping()
        ),
        "git_head": context.git_head,
        "recorded_at_utc": _utc_now(),
    }


def _load_query_ids_document(
    root: Path,
    path: Path,
    *,
    expected_prefix: str = _SOURCE_REVIEW_PREFIX,
) -> tuple[tuple[str, ...], dict[str, str]]:
    """Load one explicit, canonical selection from the review namespace."""
    try:
        relative, raw = _read_explicit_artifact(
            root,
            path,
            "review query IDs",
            max_bytes=_MAX_QUERY_IDS_DOCUMENT_BYTES,
        )
        value = _canonical_object(raw, "review query IDs")
    except (FinalizationError, ValueError) as exc:
        raise FinalizationError(
            "QUERY_IDS_INVALID", "review query ID document is invalid", (), 3
        ) from exc
    if not relative.startswith(expected_prefix):
        raise FinalizationError(
            "QUERY_IDS_INVALID",
            "review query ID document is outside review namespace",
            (relative,),
            3,
        )
    selection = value.get("query_ids")
    if (
        set(value) != {"query_ids"}
        or not isinstance(selection, list)
        or not selection
        or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in selection)
        or tuple(selection) != tuple(sorted(set(selection)))
    ):
        raise FinalizationError(
            "QUERY_IDS_INVALID", "query IDs must be sorted unique SHA-256 values", (relative,), 3
        )
    return tuple(selection), {"path": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def _write_receipt_exclusive(
    root: Path, path: Path, raw: bytes, expected_prefix: str
) -> tuple[str, v52_timezone_review._SafeSnapshot]:
    """Append an immutable receipt at one explicit path, never reconciling it."""
    try:
        relative = v52_timezone_review._relative(root, path, "source receipt")
    except ValueError as exc:
        raise FinalizationError("PATH_UNSAFE", str(exc), (), 5) from exc
    lexical = relative.as_posix()
    if (
        not lexical.startswith(expected_prefix)
        or len(relative.parts) <= len(Path(expected_prefix).parts) - 1
        or relative.name in {"", ".", ".."}
    ):
        raise FinalizationError(
            "PATH_UNSAFE", "source receipt is outside its namespace", (lexical,), 5
        )
    try:
        published, snapshot = v52_timezone_review._write_safe(
            root, relative, raw, "source receipt"
        )
        v52_timezone_review._rebind_safe_snapshot(root, snapshot, "source receipt")
    except FileExistsError as exc:
        raise FinalizationError(
            "RECEIPT_CONFLICT", "source receipt path already exists", (lexical,), 6
        ) from exc
    except (OSError, ValueError) as exc:
        raise FinalizationError("PATH_UNSAFE", str(exc), (lexical,), 5) from exc
    return published.relative_to(Path(os.path.abspath(root))).as_posix(), snapshot


def _write_validated_receipt(
    root: Path,
    path: Path,
    payload: Mapping[str, object],
    *,
    schema_name: Literal[
        "v5_2_source_review_receipt.schema.json",
        "v5_2_clean_source_replay_receipt.schema.json",
        "v5_2_source_receipt_commit.schema.json",
    ],
    expected_prefix: str,
    pre_write_rebind: Callable[[], None] | None = None,
    final_rebind: Callable[[], None] | None = None,
    cleanup_on_final_rebind_failure: bool = True,
) -> _UncommittedReceipt:
    kind = payload.get("receipt_kind")
    if kind not in {
        "SOURCE_REVIEW", "CLEAN_SOURCE_REPLAY", "SOURCE_RECEIPT_COMMIT"
    }:
        raise FinalizationError("POSTCONDITION_FAILED", "receipt kind differs", (), 5)
    git_head = payload.get("git_head")
    if not isinstance(git_head, str):
        raise FinalizationError("POSTCONDITION_FAILED", "receipt Git binding differs", (), 5)
    raw = canonical_json_bytes(payload) + b"\n"
    try:
        execution_registry.validate_source_receipt_schema(
            root, git_head=git_head, schema_name=schema_name, payload=payload
        )
    except ValueError as exc:
        raise FinalizationError("POSTCONDITION_FAILED", str(exc), (), 5) from exc
    if pre_write_rebind is not None:
        pre_write_rebind()
    written = _write_receipt_exclusive(root, path, raw, expected_prefix)
    if not isinstance(written, tuple) or len(written) != 2:
        raise FinalizationError("POSTCONDITION_FAILED", "receipt ownership proof is missing", (), 5)
    relative, snapshot = written
    if snapshot.contents != raw:
        raise FinalizationError(
            "POSTCONDITION_FAILED", "source receipt changed", (), 5
        )
    try:
        if final_rebind is not None:
            final_rebind()
        v52_timezone_review._rebind_safe_snapshot(
            root, snapshot, "source receipt", max_bytes=len(raw)
        )
    except _ParentAuthorityChanged:
        _unlink_owned_receipt(root, snapshot)
        raise
    except (FinalizationError, ValueError, OSError):
        if cleanup_on_final_rebind_failure:
            _unlink_owned_receipt(root, snapshot)
        raise
    return _UncommittedReceipt(
        root / relative,
        hashlib.sha256(raw).hexdigest(),
        kind,
        snapshot,
    )


def _open_receipt_cleanup_tombstone(parent_fd: int) -> int:
    for _ in range(16):
        name = f".v52-cleanup-receipt-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        descriptor: int | None = None
        try:
            descriptor = os.open(
                name, secure_fs._directory_open_flags(), dir_fd=parent_fd
            )
            opened = os.fstat(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise ValueError("source receipt cleanup tombstone changed while opening")
            os.fchmod(descriptor, 0o700)
            if os.fstat(descriptor).st_mode & 0o777 != 0o700:
                raise ValueError("source receipt cleanup tombstone permissions differ")
            os.fsync(descriptor)
            os.fsync(parent_fd)
            return descriptor
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            raise
    raise ValueError("could not allocate a unique source receipt cleanup tombstone")


def _read_cleanup_payload(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= maximum:
        chunk = os.read(descriptor, maximum + 1 - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _unlink_owned_receipt(
    root: Path, snapshot: v52_timezone_review._SafeSnapshot
) -> str:
    """Quarantine a failed receipt without a proof-to-unlink race."""
    parent_fd: int | None = None
    tombstone_fd: int | None = None
    descriptors: list[int] = []
    try:
        v52_timezone_review._rebind_safe_snapshot(
            root,
            snapshot,
            "source receipt cleanup",
            max_bytes=len(snapshot.contents),
        )
        parent_fd, descriptors = v52_timezone_review._open_parent(
            root, snapshot.relative, create=False
        )
        tombstone_fd = _open_receipt_cleanup_tombstone(parent_fd)
        try:
            secure_fs.rename_exclusive_at(
                snapshot.relative.name,
                "payload",
                src_dir_fd=parent_fd,
                dst_dir_fd=tombstone_fd,
            )
        except OSError:
            return "DEFERRED"
        os.fsync(tombstone_fd)
        os.fsync(parent_fd)
        owned = False
        payload_fd: int | None = None
        try:
            payload_fd = os.open(
                "payload",
                secure_fs._leaf_open_flags(
                    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                ),
                dir_fd=tombstone_fd,
            )
            payload = os.fstat(payload_fd)
            owned = (
                v52_timezone_review._file_identity(payload)[:-1] == snapshot.identity[:-1]
                and stat.S_ISREG(payload.st_mode)
                and _read_cleanup_payload(payload_fd, len(snapshot.contents))
                == snapshot.contents
            )
        except OSError:
            owned = False
        finally:
            if payload_fd is not None:
                try:
                    os.close(payload_fd)
                except OSError:
                    pass
        if owned:
            return "QUARANTINED_OWNED"
        return "DEFERRED_IN_TOMBSTONE"
    except (OSError, ValueError):
        return "DEFERRED"
    finally:
        if tombstone_fd is not None:
            try:
                os.close(tombstone_fd)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def write_review(context: FinalizationContext, request: ReviewRequest) -> ReviewReport:
    """Record one explicit, independently selected source review.

    Writing is permitted only after the candidate inventory has been published
    and independently re-preflighted; neither receipt discovery nor implicit
    selection is supported.
    """
    context = _require_mutable_context(context, phase="ACQUIRING")
    review_prefix = _review_prefix(context)
    if (
        request.reviewer_identity != "independent-source-auditor"
        or request.reviewer_role != "independent-source-auditor"
    ):
        raise FinalizationError(
            "REVIEWER_INVALID", "reviewer identity or role differs", (), 3
        )
    if request.decision not in {"APPROVED", "REJECTED"}:
        raise FinalizationError("REVIEWER_INVALID", "review decision differs", (), 3)
    receipt_relative = _validate_receipt_destination(
        context.root,
        request.write_receipt,
        expected_prefix=review_prefix,
        label="source review receipt",
    )
    commit_relative = _validate_receipt_destination(
        context.root,
        request.write_commit,
        expected_prefix=review_prefix,
        label="source review commit",
    )
    query_document_relative = _relative_under_root(
        context.root, request.query_ids_file, "review query IDs"
    )[0]
    if not query_document_relative.startswith(review_prefix):
        raise FinalizationError(
            "QUERY_IDS_INVALID",
            "review query ID document is outside review namespace",
            (query_document_relative,),
            3,
        )
    if receipt_relative == commit_relative:
        raise FinalizationError(
            "PATH_UNSAFE", "source receipt and commit marker must be distinct", (), 5
        )
    status = read_status(context)
    if not status.review_ready:
        raise FinalizationError(
            "PREFLIGHT_BLOCKED", "review requires an authenticated published inventory", (), 4
        )
    preflight = run_preflight(context)
    if preflight.status != "PASS":
        raise FinalizationError("PREFLIGHT_BLOCKED", "review requires passing preflight", (), 4)
    query_ids, document = _load_query_ids_document(
        context.root,
        request.query_ids_file,
        expected_prefix=review_prefix,
    )
    if context.inventory is None or context.snapshot_inputs is None or context.run_spec is None or context.git_head is None:
        raise FinalizationError("AUTHORITY_MISSING", "review authority is missing", (), 3)
    if not set(query_ids).issubset(context.inventory.logical_query_ids):
        raise FinalizationError(
            "QUERY_IDS_INVALID", "review selection is outside inventory", (document["path"],), 3
        )
    raw_inventory = inventory_bytes(context.inventory)
    payload: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "SOURCE_REVIEW",
        "run_id": context.run_spec.run_id,
        "critical_errors": [],
        "reviewer_identity": request.reviewer_identity,
        "reviewer_role": request.reviewer_role,
        "decision": request.decision,
        "reviewed_query_ids": list(query_ids),
        "query_ids_document": document,
        "inventory_sha256": hashlib.sha256(raw_inventory).hexdigest(),
        "snapshot_sha256s": list(context.inventory.snapshot_sha256s),
        "run_spec": context.snapshot_inputs.bindings.run_spec.as_mapping(),
        "acquisition_contract": context.snapshot_inputs.bindings.acquisition_contract.as_mapping(),
        "git_head": context.git_head,
        "occurred_at_utc": _utc_now(),
    }
    payload.update(_source_disposition_payload_fields(context, context.inventory))
    # Re-read the explicit selection after calculating the payload, then do a
    # second read-only preflight before making the receipt visible.
    rebound_ids, rebound_document = _load_query_ids_document(
        context.root,
        request.query_ids_file,
        expected_prefix=review_prefix,
    )
    if rebound_ids != query_ids or rebound_document != document:
        raise FinalizationError(
            "POSTCONDITION_FAILED", "review query ID document changed", (document["path"],), 5
        )
    final_preflight = run_preflight(context)
    if final_preflight != preflight:
        raise FinalizationError("POSTCONDITION_FAILED", "preflight changed", (), 5)

    def rebind_dependencies() -> None:
        _rebind_context_legacy(context)
        try:
            final_ids, final_document = _load_query_ids_document(
                context.root,
                request.query_ids_file,
                expected_prefix=review_prefix,
            )
        except FinalizationError as exc:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "review query ID document changed before publication",
                (document["path"],),
                5,
            ) from exc
        if final_ids != query_ids or final_document != document:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "review query ID document changed before publication",
                (document["path"],),
                5,
            )
        if run_preflight(context) != preflight:
            raise FinalizationError(
                "POSTCONDITION_FAILED", "preflight changed before publication", (), 5
            )
        _rebind_source_disposition_payload(context, context.inventory, payload)
        _rebind_context_legacy(context)

    receipt = _write_validated_receipt(
        context.root,
        request.write_receipt,
        payload,
        schema_name="v5_2_source_review_receipt.schema.json",
        expected_prefix=review_prefix,
        pre_write_rebind=rebind_dependencies,
        final_rebind=rebind_dependencies,
    )
    marker_payload: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "SOURCE_RECEIPT_COMMIT",
        "committed_receipt_kind": "SOURCE_REVIEW",
        "run_id": context.run_spec.run_id,
        "critical_errors": [],
        "source_receipt": {
            "path": receipt.path.relative_to(context.root).as_posix(),
            "sha256": receipt.sha256,
        },
        "run_spec": context.snapshot_inputs.bindings.run_spec.as_mapping(),
        "acquisition_contract": (
            context.snapshot_inputs.bindings.acquisition_contract.as_mapping()
        ),
        "git_head": context.git_head,
        "occurred_at_utc": _utc_now(),
    }
    marker_payload.update(_source_disposition_payload_fields(context, context.inventory))

    def rebind_marker_dependencies() -> None:
        rebind_dependencies()
        _rebind_source_disposition_payload(context, context.inventory, marker_payload)
        expected_binding = {
            "path": receipt.path.relative_to(context.root).as_posix(),
            "sha256": receipt.sha256,
        }
        if marker_payload["source_receipt"] != expected_binding:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "source receipt commit binding changed before publication",
                (expected_binding["path"],),
                5,
            )
        try:
            v52_timezone_review._rebind_safe_snapshot(
                context.root,
                receipt.snapshot,
                "committed source receipt",
                max_bytes=len(receipt.snapshot.contents),
            )
        except ValueError as exc:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "source receipt changed before commit publication",
                (expected_binding["path"],),
                5,
            ) from exc
        if hashlib.sha256(receipt.snapshot.contents).hexdigest() != receipt.sha256:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "source receipt hash changed before commit publication",
                (expected_binding["path"],),
                5,
            )

    try:
        marker = _write_validated_receipt(
            context.root,
            request.write_commit,
            marker_payload,
            schema_name="v5_2_source_receipt_commit.schema.json",
            expected_prefix=review_prefix,
            pre_write_rebind=rebind_marker_dependencies,
            final_rebind=rebind_marker_dependencies,
            cleanup_on_final_rebind_failure=False,
        )
    except _ParentAuthorityChanged:
        _unlink_owned_receipt(context.root, receipt.snapshot)
        raise
    return ReceiptReport(
        receipt.path,
        receipt.sha256,
        "SOURCE_REVIEW",
        marker.path,
        marker.sha256,
    )


def write_replay(context: FinalizationContext, request: ReplayRequest) -> ReplayReport:
    """Record one explicit, fully offline reproduction of the published sources."""
    execution_id = _execution_id()
    context = _require_mutable_context(context, phase="ACQUIRING")
    replay_prefix = _replay_prefix(context)
    receipt_path = _validate_receipt_destination(
        context.root,
        request.write_receipt,
        expected_prefix=replay_prefix,
        label="source replay receipt",
    )
    commit_path = _validate_receipt_destination(
        context.root,
        request.write_commit,
        expected_prefix=replay_prefix,
        label="source replay commit",
    )
    if receipt_path == commit_path:
        raise FinalizationError(
            "PATH_UNSAFE", "source receipt and commit marker must be distinct", (), 5
        )
    status = read_status(context)
    if not status.replay_ready:
        raise FinalizationError(
            "PREFLIGHT_BLOCKED",
            "replay requires an authenticated published inventory",
            (),
            4,
        )
    preflight = run_preflight(context)
    if preflight.status != "PASS":
        raise FinalizationError(
            "PREFLIGHT_BLOCKED", "replay requires passing preflight", (), 4
        )
    inventory = _load_clean_replay_inventory(context)
    payload = _clean_replay_payload(context, inventory, execution_id)
    payload.update(_source_disposition_payload_fields(context, inventory))

    def rebind_dependencies() -> None:
        _rebind_context_legacy(context)
        if run_preflight(context) != preflight:
            raise FinalizationError(
                "POSTCONDITION_FAILED", "preflight changed before publication", (), 5
            )
        rebound_inventory = _load_clean_replay_inventory(context)
        if inventory_bytes(rebound_inventory) != inventory_bytes(inventory):
            raise FinalizationError(
                "POSTCONDITION_FAILED", "clean replay changed before publication", (), 5
            )
        _rebind_source_disposition_payload(context, rebound_inventory, payload)
        _rebind_context_legacy(context)

    receipt = _write_validated_receipt(
        context.root,
        request.write_receipt,
        payload,
        schema_name="v5_2_clean_source_replay_receipt.schema.json",
        expected_prefix=replay_prefix,
        pre_write_rebind=rebind_dependencies,
        final_rebind=rebind_dependencies,
    )
    if context.run_spec is None or context.snapshot_inputs is None or context.git_head is None:
        raise FinalizationError("AUTHORITY_MISSING", "replay authority is missing", (), 3)
    marker_payload: dict[str, object] = {
        "schema_version": 1,
        "receipt_kind": "SOURCE_RECEIPT_COMMIT",
        "committed_receipt_kind": "CLEAN_SOURCE_REPLAY",
        "run_id": context.run_spec.run_id,
        "critical_errors": [],
        "source_receipt": {
            "path": receipt.path.relative_to(context.root).as_posix(),
            "sha256": receipt.sha256,
        },
        "run_spec": context.snapshot_inputs.bindings.run_spec.as_mapping(),
        "acquisition_contract": (
            context.snapshot_inputs.bindings.acquisition_contract.as_mapping()
        ),
        "git_head": context.git_head,
        "occurred_at_utc": _utc_now(),
    }
    marker_payload.update(_source_disposition_payload_fields(context, inventory))

    def rebind_marker_dependencies() -> None:
        rebind_dependencies()
        _rebind_source_disposition_payload(context, inventory, marker_payload)
        expected_binding = {
            "path": receipt.path.relative_to(context.root).as_posix(),
            "sha256": receipt.sha256,
        }
        if marker_payload["source_receipt"] != expected_binding:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "source receipt commit binding changed before publication",
                (expected_binding["path"],),
                5,
            )
        try:
            v52_timezone_review._rebind_safe_snapshot(
                context.root,
                receipt.snapshot,
                "committed source receipt",
                max_bytes=len(receipt.snapshot.contents),
            )
        except ValueError as exc:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "source receipt changed before commit publication",
                (expected_binding["path"],),
                5,
            ) from exc
        if hashlib.sha256(receipt.snapshot.contents).hexdigest() != receipt.sha256:
            raise FinalizationError(
                "POSTCONDITION_FAILED",
                "source receipt hash changed before commit publication",
                (expected_binding["path"],),
                5,
            )

    try:
        marker = _write_validated_receipt(
            context.root,
            request.write_commit,
            marker_payload,
            schema_name="v5_2_source_receipt_commit.schema.json",
            expected_prefix=replay_prefix,
            pre_write_rebind=rebind_marker_dependencies,
            final_rebind=rebind_marker_dependencies,
            cleanup_on_final_rebind_failure=False,
        )
    except _ParentAuthorityChanged:
        _unlink_owned_receipt(context.root, receipt.snapshot)
        raise
    return ReplayReport(
        receipt.path,
        receipt.sha256,
        "CLEAN_SOURCE_REPLAY",
        marker.path,
        marker.sha256,
    )


def _advance_input_error(message: str, paths: Sequence[str] = ()) -> FinalizationError:
    return FinalizationError("RECEIPT_SET_INVALID", message, paths, 4)


def advance_sources_ready(
    context: FinalizationContext, request: AdvanceRequest
) -> AdvanceReport:
    """Advance only an explicit, ordered source evidence set to SOURCES_READY."""
    if isinstance(context.run_spec, SourceRunSpec) and context.phase == "SOURCES_READY":
        raise FinalizationError(
            "PHASE_BLOCKED",
            "SOURCES_READY is terminal for the superseding source authority",
            (),
            4,
        )
    context = _require_mutable_context(
        context, phase=frozenset({"ACQUIRING", "SOURCES_READY"})
    )
    if context.run_spec is None or context.registry is None or context.checkpoint is None:
        raise FinalizationError("AUTHORITY_MISSING", "advance authority is missing", (), 3)
    if len(request.review_receipts) < 1 or len(request.replay_receipts) != 2:
        raise _advance_input_error("advance requires at least one review and exactly two replays")

    selected = (*request.review_receipts, *request.replay_receipts)
    try:
        marker_candidates = [
            _relative_under_root(
                context.root, path, "source receipt commit marker"
            )[0]
            for path in selected
        ]
        review_prefix = _review_prefix(context)
        replay_prefix = _replay_prefix(context)
        if any(
            not path.startswith(review_prefix)
            for path in marker_candidates[: len(request.review_receipts)]
        ) or any(
            not path.startswith(replay_prefix)
            for path in marker_candidates[len(request.review_receipts) :]
        ):
            raise FinalizationError(
                "PATH_UNSAFE",
                "source receipt commit marker is outside its namespace",
                marker_candidates,
                5,
            )
        checkpoint_candidate = _relative_under_root(
            context.root, request.checkpoint, "terminal acquisition checkpoint"
        )[0]
        expected_checkpoint = _checkpoint_path(
            context.checkpoint, context.authority_context
        ).as_posix()
        inventory_candidate = _relative_under_root(
            context.root, request.inventory, "source inventory"
        )[0]
        expected_inventory = (
            _sources_relative(context.authority_context) / "source_inventory.json"
        ).as_posix()
        if checkpoint_candidate != expected_checkpoint:
            raise _advance_input_error(
                "terminal checkpoint path differs from authority",
                (checkpoint_candidate,),
            )
        if inventory_candidate != expected_inventory:
            raise _advance_input_error(
                "source inventory path differs from authority",
                (inventory_candidate,),
            )
        marker_paths = [
            _read_explicit_artifact(
                context.root,
                path,
                "source receipt commit marker",
                max_bytes=_MAX_QUERY_IDS_DOCUMENT_BYTES,
            )[0]
            for path in selected
        ]
        checkpoint_relative = _read_explicit_artifact(
            context.root, request.checkpoint, "terminal acquisition checkpoint"
        )[0]
        inventory_relative = _read_explicit_artifact(
            context.root, request.inventory, "source inventory"
        )[0]
    except ValueError as exc:
        raise FinalizationError("PATH_UNSAFE", str(exc), (), 5) from exc
    if len(set(marker_paths)) != len(marker_paths):
        raise _advance_input_error("source receipt commit marker is duplicated", marker_paths)

    if context.phase == "ACQUIRING":
        status = read_status(context)
        if not status.review_ready or not status.replay_ready:
            raise _advance_input_error("published review and replay authority is not ready")
        preflight = run_preflight(context)
        if preflight.status != "PASS":
            raise _advance_input_error("advance requires passing preflight")

    # This read-only diagnostic keeps invalid requests cheap.  The authoritative
    # source gate below repeats validation while holding its exclusive lock, so
    # this preflight can never select the checkpoint or evidence that is sealed.
    try:
        execution_registry.validate_sources_ready_evidence_set(
            context.root,
            context.run_spec,
            inventory_path=context.root / inventory_relative,
            checkpoint_path=context.root / checkpoint_relative,
            validation_receipts=tuple(context.root / relative for relative in marker_paths),
            **_legacy_loader_kwargs(context.legacy_evidence),
        )
    except ValueError as exc:
        message = str(exc)
        error_code = (
            "AUTHORITY_BLOCKED"
            if any(token in message.lower() for token in ("git", "contract", "run spec", "bound code"))
            else "RECEIPT_SET_INVALID"
        )
        raise FinalizationError(error_code, "source receipt set is invalid", (), 4) from exc
    try:
        transition = execution_registry.advance_sources_ready_phase(
            context.root,
            context.run_spec,
            inventory_path=context.root / inventory_relative,
            validation_receipts=tuple(context.root / relative for relative in marker_paths),
            expected_checkpoint_path=context.root / checkpoint_relative,
            **_legacy_loader_kwargs(context.legacy_evidence),
            **(
                {"publication_state_verifier": context.legacy_evidence.rebind}
                if context.legacy_evidence is not None
                else {}
            ),
        )
    except ValueError as exc:
        message = str(exc)
        error_code = (
            "AUTHORITY_BLOCKED"
            if any(token in message.lower() for token in ("git", "contract", "run spec", "bound code"))
            else "RECEIPT_SET_INVALID"
        )
        raise FinalizationError(error_code, "source evidence advance failed", (), 4) from exc
    if isinstance(transition, execution_registry.ExecutionPhaseAdvance):
        state = transition.state
        status = "ADVANCED" if transition.created else "EXISTING"
    else:
        # Compatibility for callers which replace the registry API in tests or
        # adapters.  The real atomic call always requests the richer result.
        state = transition
        status = "EXISTING" if context.registry.phase == "SOURCES_READY" else "ADVANCED"
    if state.phase != "SOURCES_READY":
        raise FinalizationError("POSTCONDITION_FAILED", "advance phase differs", (), 5)
    return AdvanceReport(
        status,
        "SOURCES_READY",
        state.receipt_sha256,
    )


__all__ = [
    "AdvanceReport", "AdvanceRequest", "Blocker", "CommonArgs", "FinalizationContext", "FinalizationError",
    "PreflightReport", "PublishReport", "ReceiptReport", "ReplayReport",
    "ReplayRequest", "ReviewReport", "ReviewRequest", "StatusReport", "build_context",
    "advance_sources_ready", "publish_inventory", "read_status", "run_preflight", "write_replay", "write_review",
]
