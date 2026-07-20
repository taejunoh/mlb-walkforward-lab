"""Authenticated, network-free authorities for v5.2 source finalization.

This module intentionally owns the small shared vocabulary used by all staged
source-finalization commands.  It performs no publication or cleanup.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import v52_timezone_review
from .execution_registry import (
    AcquisitionCheckpoint,
    ExecutionRegistryState,
    load_execution_registry,
    load_source_verification_checkpoint,
)
from .protocols import RunSpec, parse_run_spec
from .v52_protected_inventory import (
    snapshot_protected_inventory_membership,
    verify_protected_raw_inventory,
)
from .v52_query_plan import _load_static_contracts
from .v52_snapshot_store import (
    AuthorityBinding,
    AuthorityBindings,
    SnapshotStoreInputs,
    SourceInventory,
    TerminalCheckpointBinding,
    build_inventory,
    inventory_bytes,
)
from .v52_source_contracts import canonical_json_bytes
from .v52_verify_sources import load_snapshot_replay_inputs


_RUN_SPEC_DEFAULT = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_CONTRACT_DEFAULT = Path("protocols/v5_2_qd/historical_source_acquisition.json")
_PROTECTED_DEFAULT = Path("audit/v5_1_bt/protected_raw_inventory.json")
_CHECKPOINT_ROOT = Path("audit/v5_2_qd/historical/execution/acquisition_checkpoints")
_TIMEZONE_SEAL = Path("audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json")


@dataclass(frozen=True)
class CommonArgs:
    root: Path = Path(".")
    run_spec: Path = _RUN_SPEC_DEFAULT
    acquisition_contract: Path = _CONTRACT_DEFAULT
    protected_inventory: Path = _PROTECTED_DEFAULT


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
    run_spec: RunSpec | None
    registry: ExecutionRegistryState | None
    checkpoint: AcquisitionCheckpoint | None
    snapshot_inputs: SnapshotStoreInputs | None
    inventory: SourceInventory | None
    phase: str
    git_head: str | None
    blockers: tuple[Blocker, ...]

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
    return context


def _trusted_root(value: Path) -> Path:
    """Return an absolute root only after every lexical ancestor is trusted."""
    supplied = Path(os.path.abspath(os.fspath(value)))
    try:
        for node in (*reversed(supplied.parents), supplied):
            metadata = node.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError("finalization root ancestors must be no-follow directories")
    except OSError as exc:
        raise ValueError("finalization root is unavailable") from exc
    return supplied


def _relative_artifact(root: Path, value: Path, label: str) -> tuple[str, Path]:
    trusted = _trusted_root(root)
    supplied = os.fspath(value)
    if "\\" in supplied:
        raise ValueError(f"{label} path must use slash separators")
    candidate = Path(supplied)
    if candidate.is_absolute():
        candidate = Path(os.path.abspath(candidate))
        try:
            relative = candidate.relative_to(trusted)
        except ValueError as exc:
            raise ValueError(f"{label} path must stay under root") from exc
    else:
        relative = candidate
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} path must stay under root")
    return relative.as_posix(), trusted / relative


def _read_explicit_artifact(root: Path, value: Path, label: str) -> tuple[str, bytes]:
    relative, _ = _relative_artifact(root, value, label)
    try:
        safe_relative, contents = v52_timezone_review._read_safe(
            _trusted_root(root), Path(relative), label
        )
    except OSError as exc:
        raise ValueError(f"{label} is unsafe or unreadable") from exc
    return safe_relative.as_posix(), contents


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


def _source_root(root: Path) -> Path:
    return root / "data/v5_2_qd/historical/sources"


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


def _checkpoint_path(checkpoint: AcquisitionCheckpoint) -> Path:
    return _CHECKPOINT_ROOT / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"


def _authority_bindings(
    root: Path,
    run_spec_path: Path,
    contract_path: Path,
    checkpoint: AcquisitionCheckpoint,
    replay: object,
) -> AuthorityBindings:
    schedule = getattr(replay, "schedule_inventory")
    games = getattr(replay, "game_inventory")
    timezone = getattr(replay, "timezone_gate")
    return AuthorityBindings(
        run_spec=_binding(root, run_spec_path),
        acquisition_contract=_binding(root, contract_path),
        terminal_checkpoint=TerminalCheckpointBinding(
            _checkpoint_path(checkpoint).as_posix(),
            checkpoint.receipt_sha256,
            checkpoint.sequence,
            "REMAINING",
        ),
        schedule_inventory=_verified_binding(root, schedule.path, schedule.sha256),
        game_evidence_inventory=_verified_binding(root, games.path, games.sha256),
        timezone_gate=_verified_binding(
            root, root / _TIMEZONE_SEAL, timezone.seal_sha256
        ),
    )


def _authority_changed(message: str) -> FinalizationError:
    return FinalizationError("AUTHORITY_CHANGED", message, (), 5)


def _rebind_context_authorities(
    root: Path,
    run_spec_path: Path,
    contract_path: Path,
    protected_path: Path,
    run_spec: RunSpec,
    registry: ExecutionRegistryState,
    checkpoint: AcquisitionCheckpoint,
    inventory: SourceInventory,
    *,
    initial_run_spec_bytes: bytes,
    initial_contract_bytes: bytes,
    initial_membership: object,
    initial_inventory_bytes: bytes,
    initial_git_head: str,
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
        final_registry = load_execution_registry(root, run_spec)
        final_checkpoint = load_source_verification_checkpoint(root, run_spec)
    except (OSError, ValueError, FileNotFoundError) as exc:
        raise _authority_changed("registry authority changed during context build") from exc
    if final_registry != registry:
        raise _authority_changed("registry changed during context build")
    if final_checkpoint != checkpoint:
        raise _authority_changed("checkpoint changed during context build")

    try:
        replay = load_snapshot_replay_inputs(
            root, run_spec, acquisition_contract_path=contract_path
        )
        rebound = build_inventory(
            SnapshotStoreInputs(
                replay,
                _authority_bindings(root, run_spec_path, contract_path, checkpoint, replay),
                registry.phase,
            )
        )
        final_inventory_bytes = inventory_bytes(rebound)
        final_git_head = _git_head(root)
    except FinalizationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise _authority_changed("source inventory authority changed during context build") from exc
    if final_inventory_bytes != initial_inventory_bytes:
        raise _authority_changed("source inventory changed during context build")
    if final_git_head != initial_git_head:
        raise _authority_changed("Git head changed during context build")


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
    try:
        _, run_spec_bytes = _read_explicit_artifact(root, run_spec_path, "RunSpec")
        _, contract_bytes = _read_explicit_artifact(
            root, contract_path, "acquisition contract"
        )
        _canonical_object(run_spec_bytes, "RunSpec")
        _canonical_object(contract_bytes, "acquisition contract")
        _load_static_contracts(run_spec_path, contract_path)
        run_spec = parse_run_spec(json.loads(run_spec_bytes))
        protected = verify_protected_raw_inventory(root, protected_path)
        membership = snapshot_protected_inventory_membership(root, protected)
    except FinalizationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc

    try:
        registry = load_execution_registry(root, run_spec)
    except FileNotFoundError:
        return FinalizationContext(
            root, run_spec_path, contract_path, protected_path, run_spec, None, None,
            None, None, "ABSENT", None,
            (Blocker("REGISTRY_ABSENT", "execution registry is absent"),),
        )
    except (OSError, ValueError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc

    try:
        checkpoint = load_source_verification_checkpoint(root, run_spec)
    except FileNotFoundError:
        return FinalizationContext(
            root, run_spec_path, contract_path, protected_path, run_spec, registry, None,
            None, None, registry.phase, None,
            (Blocker("CHECKPOINT_UNAVAILABLE", "terminal checkpoint is unavailable"),),
        )
    except (OSError, ValueError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc

    try:
        replay = load_snapshot_replay_inputs(
            root, run_spec, acquisition_contract_path=contract_path
        )
        bindings = _authority_bindings(
            root, run_spec_path, contract_path, checkpoint, replay
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
        )
    except FinalizationError:
        raise
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise FinalizationError("AUTHORITY_MALFORMED", str(exc), (), 3) from exc
    return FinalizationContext(
        root, run_spec_path, contract_path, protected_path, run_spec, registry,
        checkpoint, inputs, inventory, registry.phase, git_head, (),
    )


__all__ = [
    "Blocker", "CommonArgs", "FinalizationContext", "FinalizationError",
    "PreflightReport", "StatusReport", "build_context",
]
