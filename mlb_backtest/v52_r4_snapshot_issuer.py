"""Exclusive, descriptor-authenticated issuer for the R4 custody snapshot.

This module intentionally has no recovery path: a lock belongs only to the
process that created it and a failed publication remains visible to observers.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
import ctypes
import fcntl
import threading
import time
from cryptography.exceptions import InvalidSignature
from argparse import ArgumentParser
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath
from typing import Protocol, Sequence

from .secure_fs import TrustedRoot, open_trusted_root, read_regular_snapshot, rename_exclusive_at
from .v52_r4_apfs import CUSTODY_UNAVAILABLE
from . import v52_r4_authority as _r4_authority
from .v52_r4_authority import (
    INVENTORY_RELATIVE,
    LOGICAL_RAW_ROOT,
    PRODUCTION_INVENTORY_SHA256,
    R4SourceAuthorityProfile,
    SnapshotSourceClosure,
)
from .v52_source_contracts import canonical_json_bytes
from .v52_r4_signed_anchor import (
    DOMAIN_MESSAGE,
    build_authority_anchor,
    verify_authority_anchor,
)


class IssuanceState(str, Enum):
    OWNER_LOCK_ACQUIRED = "OWNER_LOCK_ACQUIRED"
    OWNER_PREPARING = "OWNER_PREPARING"
    OWNER_SNAPSHOT_PUBLISHED = "OWNER_SNAPSHOT_PUBLISHED"
    OWNER_INVENTORY_PUBLISHED = "OWNER_INVENTORY_PUBLISHED"
    OWNER_RECEIPT_PUBLISHED = "OWNER_RECEIPT_PUBLISHED"
    OWNER_CONTRACT_AUTHENTICATED = "OWNER_CONTRACT_AUTHENTICATED"
    OBSERVER_INCOMPLETE_LOCKED = "OBSERVER_INCOMPLETE_LOCKED"
    FINAL = "FINAL"
    RUNNING = "RUNNING"
    EXECUTABLE = "EXECUTABLE"
    OBSERVER_INCOMPLETE_UNLOCKED = "OBSERVER_INCOMPLETE_UNLOCKED"
    ABSENT = "ABSENT"


@dataclass(frozen=True)
class IssuanceInputs:
    """The closed, caller-supplied anchors for one R4 issuance transaction.

    Artifact locations are deliberately inputs only so their equality to the
    fixed repository layout can be checked before *any* custody mutation.
    They are never accepted as alternative output locations.
    """

    source_root: Path
    source_inventory: Path
    source_approval_logical_root: str
    source_approval_inventory: Path
    run_spec: Path
    contract: Path
    custody_parent: Path
    final_name: str
    audit_root: Path


@dataclass(frozen=True)
class IssuanceResult:
    """Authenticated final locations; no mutable owner is returned."""

    custody_root: Path
    inventory_path: Path
    receipt_path: Path
    contract_path: Path
    run_spec_path: Path
    anchor_path: Path
    signature_path: Path


@dataclass(frozen=True)
class _SourceAuthorityBinding:
    """One private, descriptor-relative source authority binding."""

    inventory_relative: PurePosixPath
    expected_inventory_sha256: str
    raw_relative: PurePosixPath


@dataclass(frozen=True)
class _R4AuthorityLayout:
    """Closed R4 locations derived only from the two authority anchors."""

    repository_root: Path
    custody_parent: Path
    source_authority: R4SourceAuthorityProfile | None = None
    final_name: str = "v5_1_bt_r4"
    inventory_relative: PurePosixPath = PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/protected_raw_inventory_r4.json"
    )
    receipt_relative: PurePosixPath = PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/physical_custody_transition_r4.json"
    )
    anchor_relative: PurePosixPath = PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/physical_custody_authority_anchor_r4.json"
    )
    signature_relative: PurePosixPath = PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "acquisition/physical_custody_authority_anchor_r4.ed25519"
    )
    run_spec_relative: PurePosixPath = PurePosixPath(
        "protocols/v5_2_qd/"
        "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json"
    )
    contract_relative: PurePosixPath = PurePosixPath(
        "protocols/v5_2_qd/"
        "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json"
    )
    checkpoints_relative: PurePosixPath = PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "execution/acquisition_checkpoints"
    )
    terminal_relative: PurePosixPath = PurePosixPath(
        "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
        "execution/terminal_evidence.json"
    )


@dataclass(frozen=True)
class _R4RepositoryBinding:
    """The one repository authority anchor permitted for an owner lifetime."""

    path: Path
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _AuthenticatedSnapshot:
    layout: _R4AuthorityLayout


@dataclass(frozen=True)
class _AuthenticatedAnchor:
    """Signature-verified authority root for all mutable R4 documents."""

    snapshot: _AuthenticatedSnapshot
    document: Mapping[str, object]


@dataclass(frozen=True)
class _AuthenticatedInventory:
    anchor: _AuthenticatedAnchor


@dataclass(frozen=True)
class _AuthenticatedReceipt:
    inventory: _AuthenticatedInventory


@dataclass(frozen=True)
class _AuthenticatedContract:
    receipt: _AuthenticatedReceipt


@dataclass(frozen=True)
class _AuthenticatedCheckpointChain:
    contract: _AuthenticatedContract


@dataclass(frozen=True)
class _AuthenticatedTerminal:
    contract: _AuthenticatedContract


class _R4EvidenceAuthenticator(Protocol):
    """Private opaque-capability boundary for later closed R4 validators."""

    def authenticate_snapshot(
        self, layout: _R4AuthorityLayout, custody_parent_fd: int
    ) -> _AuthenticatedSnapshot | None: ...

    def authenticate_inventory(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
    ) -> _AuthenticatedInventory | None: ...

    def authenticate_anchor(
        self, layout: _R4AuthorityLayout, snapshot: _AuthenticatedSnapshot
    ) -> _AuthenticatedAnchor | None: ...

    def authenticate_receipt(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
        inventory: _AuthenticatedInventory,
    ) -> _AuthenticatedReceipt | None: ...

    def authenticate_contract(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
        inventory: _AuthenticatedInventory,
        receipt: _AuthenticatedReceipt,
    ) -> _AuthenticatedContract | None: ...

    def authenticate_checkpoint_chain(
        self, layout: _R4AuthorityLayout, contract: _AuthenticatedContract
    ) -> _AuthenticatedCheckpointChain | None: ...

    def authenticate_terminal(
        self, layout: _R4AuthorityLayout, contract: _AuthenticatedContract
    ) -> _AuthenticatedTerminal | None: ...


@dataclass(frozen=True)
class SnapshotCopyResult:
    record_count: int
    physical_member_count: int
    logical_binding_count: int
    root_locks: tuple[str, ...]


@dataclass(frozen=True)
class _TestSnapshotCopy:
    """Private small-tree fixture binding; production always carries a closure."""


_APPROVED_LOGICAL_RAW_ROOT = LOGICAL_RAW_ROOT
_APPROVED_SOURCE_INVENTORY = Path(*INVENTORY_RELATIVE.parts)
_APPROVED_SOURCE_INVENTORY_SHA256 = PRODUCTION_INVENTORY_SHA256


def _r4_authority_layout(
    repository_root: Path, custody_parent: Path,
    source_authority: R4SourceAuthorityProfile | None = None,
) -> _R4AuthorityLayout:
    """Derive the only R4 artifact layout; no caller supplies artifact paths."""
    return _R4AuthorityLayout(Path(repository_root), Path(custody_parent), source_authority)


class _UnavailableR4EvidenceAuthenticator:
    """Production fail-closed adapter until the closed Task 4/5 validators land."""

    def authenticate_snapshot(
        self, layout: _R4AuthorityLayout, custody_parent_fd: int
    ) -> _AuthenticatedSnapshot | None:
        if _authenticated_final_root(custody_parent_fd, layout.final_name):
            return _AuthenticatedSnapshot(layout)
        return None

    def authenticate_inventory(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
    ) -> _AuthenticatedInventory | None:
        del layout, snapshot, anchor
        return None

    def authenticate_anchor(
        self, layout: _R4AuthorityLayout, snapshot: _AuthenticatedSnapshot
    ) -> _AuthenticatedAnchor | None:
        del layout, snapshot
        return None

    def authenticate_receipt(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
        inventory: _AuthenticatedInventory,
    ) -> _AuthenticatedReceipt | None:
        del layout, snapshot, anchor, inventory
        return None

    def authenticate_contract(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
        inventory: _AuthenticatedInventory,
        receipt: _AuthenticatedReceipt,
    ) -> _AuthenticatedContract | None:
        del layout, snapshot, anchor, inventory, receipt
        return None

    def authenticate_checkpoint_chain(
        self, layout: _R4AuthorityLayout, contract: _AuthenticatedContract
    ) -> _AuthenticatedCheckpointChain | None:
        del layout, contract
        return None

    def authenticate_terminal(
        self, layout: _R4AuthorityLayout, contract: _AuthenticatedContract
    ) -> _AuthenticatedTerminal | None:
        del layout, contract
        return None


_UNAVAILABLE_R4_AUTHENTICATOR = _UnavailableR4EvidenceAuthenticator()


def _read_canonical_repository_json(repository: Path, relative: PurePosixPath, label: str) -> dict[str, object]:
    """Read one fixed repository-relative JSON file through a root descriptor."""
    try:
        with open_trusted_root(repository, nonblocking=True) as root:
            snapshot = read_regular_snapshot(
                root, Path(*relative.parts), label, nonblocking=True
            )
            root.assert_bound()
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable") from exc
    if snapshot is None:
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable")
    return _canonical_json_from_bytes(snapshot.contents, label)


def _canonical_json_from_bytes(payload: bytes, label: str) -> dict[str, object]:
    """Decode only canonical bytes already bound by an authenticated descriptor."""
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) + b"\n" != payload:
        raise CUSTODY_UNAVAILABLE(f"{label} is not canonical")
    return value


def _anchored_artifact_bytes(
    layout: _R4AuthorityLayout,
    anchor: Mapping[str, object],
    name: str,
    relative: PurePosixPath,
    label: str,
) -> bytes:
    """Read one mutable artifact only after its signed path/hash are fixed."""
    artifacts = anchor.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise CUSTODY_UNAVAILABLE("signed artifact bindings are malformed")
    binding = artifacts.get(name)
    if (
        not isinstance(binding, Mapping)
        or binding.get("path") != relative.as_posix()
        or not isinstance(binding.get("sha256"), str)
    ):
        raise CUSTODY_UNAVAILABLE(f"signed {name} binding differs")
    payload = _read_repository_regular(layout.repository_root, relative, label)
    if hashlib.sha256(payload).hexdigest() != binding["sha256"]:
        raise CUSTODY_UNAVAILABLE(f"signed {name} hash differs")
    return payload


def _verify_signed_amendment_bindings(anchor: Mapping[str, object]) -> None:
    """Bind the approved amendment to the immutable issuer checkout, not output."""
    amendments = anchor.get("amendment")
    if not isinstance(amendments, Mapping):
        raise CUSTODY_UNAVAILABLE("signed amendment bindings are malformed")
    checkout = Path(__file__).resolve().parent.parent
    for name in ("design", "implementation_plan"):
        binding = amendments.get(name)
        if not isinstance(binding, Mapping):
            raise CUSTODY_UNAVAILABLE(f"signed {name} binding differs")
        path = binding.get("path")
        if not isinstance(path, str):
            raise CUSTODY_UNAVAILABLE(f"signed {name} path differs")
        relative = PurePosixPath(path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise CUSTODY_UNAVAILABLE(f"signed {name} path differs")
        payload = _read_repository_regular(checkout, relative, f"R4 amendment {name}")
        if (
            binding.get("sha256") != hashlib.sha256(payload).hexdigest()
            or binding.get("git_blob_id") != _git_blob_id(payload)
        ):
            raise CUSTODY_UNAVAILABLE(f"signed {name} binding differs")


class _LiveR4EvidenceAuthenticator:
    """Closed evidence adapter used by the public issuer and observers.

    This intentionally returns opaque stage tokens only after rereading each
    final pathname through the repository anchor.  No publication-time boolean
    participates in release eligibility.
    """

    def authenticate_snapshot(
        self, layout: _R4AuthorityLayout, custody_parent_fd: int
    ) -> _AuthenticatedSnapshot | None:
        if _authenticated_final_root(custody_parent_fd, layout.final_name):
            return _AuthenticatedSnapshot(layout)
        return None

    def authenticate_anchor(
        self, layout: _R4AuthorityLayout, snapshot: _AuthenticatedSnapshot
    ) -> _AuthenticatedAnchor | None:
        """Verify the detached signature before decoding any output document."""
        try:
            anchor_bytes = _read_repository_regular(
                layout.repository_root, layout.anchor_relative, "R4 authority anchor",
                max_bytes=16_384,
            )
            signature = _read_repository_regular(
                layout.repository_root, layout.signature_relative, "R4 authority signature",
                max_bytes=64,
            )
            profile = _layout_source_authority(layout)
            document = verify_authority_anchor(
                anchor_bytes, signature,
                public_key=profile.anchor_public_key,
                expected_key_id=profile.anchor_key_id,
            )
            _verify_signed_amendment_bindings(document)
            for relative in (layout.anchor_relative, layout.signature_relative):
                _require_frozen_regular_artifact(layout.repository_root, relative)
        except (CUSTODY_UNAVAILABLE, ValueError, InvalidSignature):
            return None
        return _AuthenticatedAnchor(snapshot, document)

    def authenticate_inventory(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
    ) -> _AuthenticatedInventory | None:
        try:
            from .v52_r4_custody_artifacts import validate_r4_inventory

            payload = _anchored_artifact_bytes(
                layout, anchor.document, "inventory", layout.inventory_relative, "R4 inventory"
            )
            value = _canonical_json_from_bytes(payload, "R4 inventory")
            # The issued inventory is evidence, never its own authority.  The
            # fixed protected inventory (or the fixture's private provider)
            # supplies the expected logical closure without an output raw tree.
            if layout.source_authority is None:
                object.__setattr__(
                    layout, "source_authority", _r4_authority._resolve_source_authority()
                )
            validate_r4_inventory(value, profile=_layout_source_authority(layout))
            if value.get("custody_identity_manifest_sha256") != anchor.document.get(
                "custody_identity_manifest_sha256"
            ):
                return None
            _require_frozen_regular_artifact(layout.repository_root, layout.inventory_relative)
        except (CUSTODY_UNAVAILABLE, ValueError):
            return None
        return _AuthenticatedInventory(anchor)

    def authenticate_receipt(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
        inventory: _AuthenticatedInventory,
    ) -> _AuthenticatedReceipt | None:
        try:
            from .v52_r4_custody_artifacts import validate_transition_receipt

            inventory_document = _canonical_json_from_bytes(
                _anchored_artifact_bytes(
                    layout, anchor.document, "inventory", layout.inventory_relative, "R4 inventory"
                ), "R4 inventory"
            )
            receipt = _canonical_json_from_bytes(
                _anchored_artifact_bytes(
                    layout, anchor.document, "receipt", layout.receipt_relative,
                    "R4 transition receipt",
                ), "R4 transition receipt"
            )
            validate_transition_receipt(
                receipt, inventory=inventory_document,
                profile=_layout_source_authority(layout),
            )
            expected = hashlib.sha256(
                canonical_json_bytes(inventory_document) + b"\n"
            ).hexdigest()
            if receipt["r4_inventory"] != {
                "path": layout.inventory_relative.as_posix(), "sha256": expected
            }:
                return None
            comparison = receipt.get("comparison")
            if not isinstance(comparison, Mapping):
                return None
            binding_comparison = comparison.get("binding_comparison")
            if not isinstance(binding_comparison, Mapping) or anchor.document.get(
                "receipt_comparison"
            ) != {
                "source_content_manifest_sha256": comparison.get("source_content_manifest_sha256"),
                "snapshot_content_manifest_sha256": comparison.get("snapshot_content_manifest_sha256"),
                "source_logical_binding_manifest_sha256": binding_comparison.get(
                    "source_logical_binding_manifest_sha256"
                ),
                "snapshot_logical_binding_manifest_sha256": binding_comparison.get(
                    "snapshot_logical_binding_manifest_sha256"
                ),
            }:
                return None
            _require_frozen_regular_artifact(layout.repository_root, layout.receipt_relative)
        except (CUSTODY_UNAVAILABLE, ValueError, KeyError, InvalidSignature):
            return None
        return _AuthenticatedReceipt(inventory)

    def authenticate_contract(
        self,
        layout: _R4AuthorityLayout,
        snapshot: _AuthenticatedSnapshot,
        anchor: _AuthenticatedAnchor,
        inventory: _AuthenticatedInventory,
        receipt: _AuthenticatedReceipt,
    ) -> _AuthenticatedContract | None:
        try:
            from .v52_source_contracts import validate_r4_recovery_acquisition_contract
            from .v52_r4_custody_artifacts import verify_frozen_r4_snapshot

            value = _canonical_json_from_bytes(
                _anchored_artifact_bytes(
                    layout, anchor.document, "contract", layout.contract_relative,
                    "R4 acquisition contract",
                ), "R4 acquisition contract"
            )
            bindings = value.get("bindings")
            if not isinstance(bindings, dict):
                return None
            validate_r4_recovery_acquisition_contract(
                value, bindings, profile=_layout_source_authority(layout)
            )
            inventory_document = _canonical_json_from_bytes(
                _anchored_artifact_bytes(
                    layout, anchor.document, "inventory", layout.inventory_relative, "R4 inventory"
                ), "R4 inventory"
            )
            receipt_document = _canonical_json_from_bytes(
                _anchored_artifact_bytes(
                    layout, anchor.document, "receipt", layout.receipt_relative,
                    "R4 transition receipt",
                ), "R4 transition receipt"
            )
            transition = value.get("physical_custody_transition")
            if not isinstance(transition, dict) or transition.get("r4_inventory") != {
                "path": layout.inventory_relative.as_posix(),
                "sha256": hashlib.sha256(canonical_json_bytes(inventory_document) + b"\n").hexdigest(),
            } or transition.get("receipt") != {
                "path": layout.receipt_relative.as_posix(),
                "sha256": hashlib.sha256(canonical_json_bytes(receipt_document) + b"\n").hexdigest(),
            }:
                return None
            _canonical_json_from_bytes(
                _anchored_artifact_bytes(
                    layout, anchor.document, "run_spec", layout.run_spec_relative, "R4 RunSpec"
                ), "R4 RunSpec"
            )
            for relative in (
                layout.inventory_relative, layout.receipt_relative, layout.contract_relative,
                layout.anchor_relative, layout.signature_relative,
            ):
                _require_frozen_regular_artifact(layout.repository_root, relative)
            # APFS can publish a delayed immutable-metadata ctime after the
            # signed inventory has already observed a stable pair.  Recheck
            # the whole exact snapshot a bounded number of times; a changed
            # member, hash, mode, flag, or identity never becomes accepted
            # unless it again exactly matches the signed inventory.
            for attempt in range(4):
                try:
                    verify_frozen_r4_snapshot(
                        layout.custody_parent / layout.final_name, inventory_document
                    )
                    break
                except CUSTODY_UNAVAILABLE:
                    if attempt == 3:
                        raise
                    time.sleep(0.2)
        except (CUSTODY_UNAVAILABLE, ValueError, KeyError):
            return None
        return _AuthenticatedContract(receipt)

    def authenticate_checkpoint_chain(
        self, layout: _R4AuthorityLayout, contract: _AuthenticatedContract
    ) -> _AuthenticatedCheckpointChain | None:
        return None

    def authenticate_terminal(
        self, layout: _R4AuthorityLayout, contract: _AuthenticatedContract
    ) -> _AuthenticatedTerminal | None:
        return None


_LIVE_R4_AUTHENTICATOR = _LiveR4EvidenceAuthenticator()
_OWNER_FACTORY_TOKEN = object()
_ACTIVE_ISSUANCE_OWNERS: set["IssuanceOwner"] = set()
_ACQUIRE_FORK_LOCK = threading.RLock()
_FORK_CHILD_INVALIDATED = False
_FORK_ENUMERATION_FAILURE_STATUS = 190


def _r4_release_test_hook(stage: str, resource: str) -> None:
    """Private no-op seam for deterministic TemporaryDirectory race tests."""
    del stage, resource


def _layout_source_authority(layout: _R4AuthorityLayout) -> R4SourceAuthorityProfile:
    """Use the transaction profile when present, otherwise fixed production facts."""
    return layout.source_authority or _r4_authority._resolve_source_authority()


def _issuance_authority(
    source_root: Path, source_inventory: Path,
) -> _r4_authority._IssuanceAuthority:
    resolved = _r4_authority._resolve_source_authority(source_root, source_inventory)
    if not isinstance(resolved, _r4_authority._IssuanceAuthority):
        raise CUSTODY_UNAVAILABLE("R4 issuance signer is unavailable")
    return resolved


def _bare_name(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise CUSTODY_UNAVAILABLE(f"unsafe {label}")
    return value


def _validate_cli_source_approval_inventory(raw: object) -> None:
    """Validate relative approval spelling before pathlib can normalize it."""
    if not isinstance(raw, str) or not raw:
        raise CUSTODY_UNAVAILABLE("issuance source approval inventory differs")
    if not raw.startswith("/") and raw != INVENTORY_RELATIVE.as_posix():
        raise CUSTODY_UNAVAILABLE("issuance source approval inventory differs")


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if not all(getattr(os, name, 0) for name in required):
        raise CUSTODY_UNAVAILABLE("descriptor custody filesystem support is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _leaf_flags(access: int) -> int:
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_CLOEXEC", 0):
        raise CUSTODY_UNAVAILABLE("descriptor custody filesystem support is unavailable")
    return access | os.O_NOFOLLOW | os.O_CLOEXEC


def _identity(observed: os.stat_result) -> tuple[int, int, int, int]:
    return (observed.st_dev, observed.st_ino, observed.st_mode, observed.st_nlink)


def _open_directory_no_follow(path: Path, *, nonblocking: bool = False) -> int:
    """Return an independently owned, no-follow descriptor for ``path``."""
    try:
        trusted = open_trusted_root(path, nonblocking=nonblocking)
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("unsafe custody parent") from exc
    try:
        return trusted.dup_root_fd()
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("unsafe custody parent") from exc
    finally:
        trusted.close()


def _stat_named(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("custody path cannot be inspected") from exc


def _require_same_named_descriptor(parent_fd: int, name: str, fd: int) -> os.stat_result:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("custody descriptor changed") from exc
    if _identity(named) != _identity(opened):
        raise CUSTODY_UNAVAILABLE("custody descriptor changed")
    return opened


def _close(fd: int) -> None:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass


def _require_kernel_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (AttributeError, OSError) as exc:
        raise CUSTODY_UNAVAILABLE("issuance kernel lock is unavailable") from exc


def _write_lock_nonce(descriptor: int, nonce: bytes) -> None:
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        view = memoryview(nonce)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("lock nonce write failed")
            view = view[written:]
        os.fsync(descriptor)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("issuance lock nonce cannot be persisted") from exc


def _read_lock_nonce(descriptor: int) -> bytes:
    try:
        if os.fstat(descriptor).st_size != 16:
            raise OSError("lock nonce size differs")
        os.lseek(descriptor, 0, os.SEEK_SET)
        nonce = os.read(descriptor, 16)
        if len(nonce) != 16 or os.read(descriptor, 1):
            raise OSError("lock nonce differs")
        return nonce
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("issuance lock nonce differs") from exc


class IssuanceOwner:
    """The non-transferable in-memory capability represented by one lock fd."""

    __slots__ = (
        "parent_fd",
        "_lock_fd",
        "lock_name",
        "_final_name",
        "_nonce",
        "_lock_identity",
        "_parent_identity",
        "_creator_pid",
        "_factory_token",
        "_rename_completed",
        "_fork_invalid",
        "_registered",
        "_snapshot_published",
        "_bound_closure",
        "_source_authority_profile",
        "_authority_layout",
        "_repository_binding",
        "_authenticator",
    )

    def __init__(
        self,
        parent_fd: int,
        lock_fd: int,
        lock_name: str,
        final_name: str,
        nonce: bytes,
        lock_identity: os.stat_result,
        parent_identity: os.stat_result,
        *,
        factory_token: object,
    ) -> None:
        if factory_token is not _OWNER_FACTORY_TOKEN:
            raise CUSTODY_UNAVAILABLE("issuance owner must be created by lock factory")
        self.parent_fd = parent_fd
        self._lock_fd = lock_fd
        self.lock_name = lock_name
        self._final_name = final_name
        self._nonce = nonce
        self._lock_identity = _identity(lock_identity)
        self._parent_identity = _identity(parent_identity)
        self._creator_pid = os.getpid()
        self._factory_token = factory_token
        self._rename_completed = False
        self._fork_invalid = False
        self._registered = False
        self._snapshot_published = False
        self._bound_closure: SnapshotSourceClosure | _TestSnapshotCopy | None = None
        self._source_authority_profile: R4SourceAuthorityProfile | None = None
        self._authority_layout: _R4AuthorityLayout | None = None
        self._repository_binding: _R4RepositoryBinding | None = None
        self._authenticator: _R4EvidenceAuthenticator = _UNAVAILABLE_R4_AUTHENTICATOR

    @property
    def final_name(self) -> str:
        return self._final_name

    @property
    def temporary_name(self) -> str:
        return f".{self._final_name}.issuance.{self._nonce.hex()}.tmp"

    def __enter__(self) -> "IssuanceOwner":
        if not self.is_live_owner():
            raise CUSTODY_UNAVAILABLE("issuance owner is no longer live")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if self._rename_completed:
            self._close_descriptors()
            return False
        if self.is_live_owner() and not self._snapshot_published:
            try:
                self.abort_before_rename()
            except Exception:
                if exc_type is None:
                    raise
            finally:
                self._close_descriptors()
        else:
            self._close_descriptors()
        return False

    def is_live_owner(self) -> bool:
        """Return whether the original lock descriptor is still our exact lock."""
        if (
            self.parent_fd < 0
            or self._lock_fd < 0
            or len(self._nonce) != 16
            or self._factory_token is not _OWNER_FACTORY_TOKEN
            or self._creator_pid != os.getpid()
            or self._fork_invalid
        ):
            return False
        try:
            opened = _require_same_named_descriptor(
                self.parent_fd, self.lock_name, self._lock_fd
            )
            _require_kernel_lock(self._lock_fd)
            return (
                stat.S_ISREG(opened.st_mode)
                and _identity(opened) == self._lock_identity
                and _read_lock_nonce(self._lock_fd) == self._nonce
            )
        except CUSTODY_UNAVAILABLE:
            return False

    def _require_live_owner(self) -> None:
        if not self.is_live_owner():
            raise CUSTODY_UNAVAILABLE("issuance owner is no longer live")

    def require_pre_rename_owner(self) -> None:
        self._require_live_owner()
        if self._snapshot_published or self._rename_completed:
            raise CUSTODY_UNAVAILABLE("issuance snapshot is already published")

    def _record_snapshot_published(self) -> None:
        self._require_live_owner()
        self._snapshot_published = True

    def configure_r4_authority(self, repository_root: Path, custody_parent: Path) -> None:
        """Bind the fixed R4 layout to this still-live, exact custody owner."""
        self._require_live_owner()
        try:
            supplied = os.stat(custody_parent, follow_symlinks=False)
            held = os.fstat(self.parent_fd)
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("custody parent anchor is unavailable") from exc
        if _identity(supplied) != _identity(held) or not stat.S_ISDIR(supplied.st_mode):
            raise CUSTODY_UNAVAILABLE("custody parent anchor differs")
        root_fd = -1
        try:
            # Bind both the lexical anchor and its descriptor identity.  A
            # later caller must use this same authority, not merely a path
            # which happens to contain an equivalent audit tree.
            with open_trusted_root(repository_root, nonblocking=True) as root:
                root_fd = os.dup(root.root_fd)
                root.assert_bound()
                observed_root = os.fstat(root_fd)
        except (OSError, ValueError) as exc:
            raise CUSTODY_UNAVAILABLE("repository authority anchor is unavailable") from exc
        finally:
            _close(root_fd)
        if not stat.S_ISDIR(observed_root.st_mode):
            raise CUSTODY_UNAVAILABLE("repository authority anchor differs")
        binding = _R4RepositoryBinding(
            Path(repository_root).absolute(), _identity(observed_root)
        )
        if self._repository_binding is not None and binding != self._repository_binding:
            raise CUSTODY_UNAVAILABLE("repository authority anchor differs")
        layout = _r4_authority_layout(
            binding.path, custody_parent, self._source_authority_profile
        )
        if self.final_name != layout.final_name:
            raise CUSTODY_UNAVAILABLE("issuance final name differs from fixed R4 layout")
        self._repository_binding = binding
        self._authority_layout = layout
        self._authenticator = _LIVE_R4_AUTHENTICATOR

    def require_r4_repository_authority(self, repository_root: Path) -> None:
        """Rebind the owner-bound repository root before each artifact step."""
        self._require_live_owner()
        expected = self._repository_binding
        if expected is None or Path(repository_root).absolute() != expected.path:
            raise CUSTODY_UNAVAILABLE("repository authority anchor differs")
        descriptor = -1
        try:
            with open_trusted_root(expected.path, nonblocking=True) as root:
                descriptor = os.dup(root.root_fd)
                root.assert_bound()
                if _identity(os.fstat(descriptor)) != expected.identity:
                    raise CUSTODY_UNAVAILABLE("repository authority anchor differs")
        except (OSError, ValueError) as exc:
            raise CUSTODY_UNAVAILABLE("repository authority anchor is unavailable") from exc
        finally:
            _close(descriptor)

    def state(self) -> IssuanceState:
        self._require_live_owner()
        evidence = _authenticate_r4_evidence(
            self._authority_layout,
            self._authenticator,
            self.parent_fd,
        )
        final_root = evidence.snapshot is not None if self._authority_layout else _authenticated_final_root(self.parent_fd, self.final_name)
        temporary = _stat_named(self.parent_fd, self.temporary_name) is not None
        if not temporary and evidence.contract is not None:
            return IssuanceState.OWNER_CONTRACT_AUTHENTICATED
        if not temporary and evidence.receipt is not None:
            return IssuanceState.OWNER_RECEIPT_PUBLISHED
        if not temporary and evidence.inventory is not None:
            return IssuanceState.OWNER_INVENTORY_PUBLISHED
        if final_root and not temporary:
            return IssuanceState.OWNER_SNAPSHOT_PUBLISHED
        if temporary and not final_root:
            return IssuanceState.OWNER_PREPARING
        return IssuanceState.OWNER_LOCK_ACQUIRED

    def abort_before_rename(self) -> None:
        """Remove only an un-published tree and this capability's exact lock."""
        self._require_live_owner()
        if self._rename_completed or self._snapshot_published or _stat_named(self.parent_fd, self.final_name) is not None:
            raise CUSTODY_UNAVAILABLE("post-rename issuance cannot be aborted")
        temporary = _stat_named(self.parent_fd, self.temporary_name)
        if temporary is not None:
            if not stat.S_ISDIR(temporary.st_mode):
                raise CUSTODY_UNAVAILABLE("issuance temporary root is unsafe")
            _unlink_tree_at(self.parent_fd, self.temporary_name)
        try:
            os.fsync(self.parent_fd)
            _require_same_named_descriptor(self.parent_fd, self.lock_name, self._lock_fd)
            os.unlink(self.lock_name, dir_fd=self.parent_fd)
            os.fsync(self.parent_fd)
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("issuance cleanup failed") from exc
        finally:
            self._close_descriptors()

    def release_after_contract_authentication(self) -> None:
        self._require_live_owner()
        _r4_release_test_hook("before_bundle", "owner_lock")
        bundle = (
            _ReleaseBundle.capture(self)
            if self._authority_layout is not None and self._repository_binding is not None
            else None
        )
        try:
            if bundle is None:
                evidence = _authenticate_r4_evidence(
                    self._authority_layout,
                    self._authenticator,
                    self.parent_fd,
                )
                if evidence.contract is None:
                    raise CUSTODY_UNAVAILABLE("contract is not authenticated")
            if _stat_named(self.parent_fd, self.temporary_name) is not None:
                raise CUSTODY_UNAVAILABLE("issuance temporary root remains")
            if bundle is not None:
                # The release decision is bound to the descriptors retained
                # before authentication, not a later pathname reread.
                bundle.authenticate_bound_authority()
            _r4_release_test_hook("during_bundle_auth", "owner_lock")
            _require_same_named_descriptor(self.parent_fd, self.lock_name, self._lock_fd)
            os.fsync(self.parent_fd)
            # This is the final path rebind immediately before the irreversible
            # unlink.  A swap in any release participant keeps the lock.
            if bundle is not None:
                bundle.rebind(stage="final_rebind")
            _r4_release_test_hook("final_rebind", "owner_lock")
            if bundle is not None:
                # The final hook is deliberately after every participant has
                # rebound.  Enumerate through the held custody descriptor,
                # rather than reopening its path, so a newly-created matching
                # temporary root cannot be hidden between authentication and
                # the irreversible owner-lock unlink.
                bundle.require_no_matching_temporary_roots()
            _require_same_named_descriptor(self.parent_fd, self.lock_name, self._lock_fd)
            os.unlink(self.lock_name, dir_fd=self.parent_fd)
            os.fsync(self.parent_fd)
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("issuance lock release failed") from exc
        finally:
            if bundle is not None:
                bundle.close()
            self._close_descriptors()

    def _close_descriptors(self) -> None:
        if self._registered:
            _ACTIVE_ISSUANCE_OWNERS.discard(self)
            self._registered = False
        lock_fd, parent_fd = self._lock_fd, self.parent_fd
        self._lock_fd = -1
        self.parent_fd = -1
        _close(lock_fd)
        _close(parent_fd)


@dataclass
class _ReleaseBundle:
    """Owned no-follow descriptors that bind every release participant.

    The bundle is intentionally short-lived and is created only after all five
    final static artifacts exist.  It prevents a caller from validating one
    pathname and releasing the owner lock for a replacement published later.
    """

    owner: IssuanceOwner
    entries: list[tuple[int, int, str, tuple[int, int, int, int], str]]
    roots: list[TrustedRoot]

    @classmethod
    def capture(cls, owner: IssuanceOwner) -> "_ReleaseBundle":
        layout = owner._authority_layout
        binding = owner._repository_binding
        if layout is None or binding is None:
            raise CUSTODY_UNAVAILABLE("R4 release authority is unavailable")
        entries: list[tuple[int, int, str, tuple[int, int, int, int], str]] = []
        roots: list[TrustedRoot] = []
        try:
            # Retain snapshot root, RunSpec, all five static finals, and each
            # corresponding parent descriptor.  Parent entries are rechecked
            # alongside the leaf on every release rebind.
            _r4_release_test_hook("before_bundle", "snapshot_root")
            custody_root = open_trusted_root(layout.custody_parent, nonblocking=True)
            roots.append(custody_root)
            if _identity(os.fstat(custody_root.root_fd)) != _identity(os.fstat(owner.parent_fd)):
                raise CUSTODY_UNAVAILABLE("R4 custody parent changed")
            root_fd = os.open(owner.final_name, _directory_flags(), dir_fd=custody_root.root_fd)
            snapshot = _require_same_named_descriptor(custody_root.root_fd, owner.final_name, root_fd)
            entries.append((custody_root.root_fd, root_fd, owner.final_name, _identity(snapshot), "snapshot_root"))
            for relative in (
                layout.run_spec_relative, layout.inventory_relative,
                layout.receipt_relative, layout.contract_relative,
                layout.anchor_relative, layout.signature_relative,
            ):
                _r4_release_test_hook("before_bundle", relative.as_posix())
                _r4_release_test_hook(
                    "before_bundle", f"parent:{binding.path / Path(*relative.parent.parts)}"
                )
                root = open_trusted_root(
                    binding.path / Path(*relative.parent.parts), nonblocking=True
                )
                roots.append(root)
                leaf_fd = os.open(relative.name, _leaf_flags(os.O_RDONLY), dir_fd=root.root_fd)
                observed = _require_same_named_descriptor(root.root_fd, relative.name, leaf_fd)
                if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                    raise CUSTODY_UNAVAILABLE("R4 release artifact is unsafe")
                entries.append((root.root_fd, leaf_fd, relative.name, _identity(observed), relative.as_posix()))
            return cls(owner, entries, roots)
        except (OSError, ValueError) as exc:
            for _parent_fd, leaf_fd, _name, _identity_value, _resource in reversed(entries):
                _close(leaf_fd)
            for root in reversed(roots):
                root.close()
            raise CUSTODY_UNAVAILABLE("R4 release bundle is unavailable") from exc
        except BaseException:
            for _parent_fd, leaf_fd, _name, _identity_value, _resource in reversed(entries):
                _close(leaf_fd)
            for root in reversed(roots):
                root.close()
            raise

    def rebind(self, *, stage: str = "during_bundle_auth") -> None:
        self.owner._require_live_owner()
        layout = self.owner._authority_layout
        binding = self.owner._repository_binding
        if layout is None or binding is None:
            raise CUSTODY_UNAVAILABLE("R4 release authority is unavailable")
        self.owner.require_r4_repository_authority(binding.path)
        for root in self.roots:
            _r4_release_test_hook(stage, f"parent:{root.path}")
            try:
                root.assert_bound()
            except ValueError as exc:
                raise CUSTODY_UNAVAILABLE("R4 release parent changed") from exc
        for parent_fd, leaf_fd, name, expected, resource in self.entries:
            _r4_release_test_hook(stage, resource)
            observed = _require_same_named_descriptor(parent_fd, name, leaf_fd)
            if _identity(observed) != expected:
                raise CUSTODY_UNAVAILABLE("R4 release artifact changed")
        for relative in (
            layout.inventory_relative, layout.receipt_relative, layout.contract_relative,
            layout.anchor_relative, layout.signature_relative,
        ):
            _require_frozen_regular_artifact(binding.path, relative)

    def require_no_matching_temporary_roots(self) -> None:
        """Fail closed if the held custody parent contains another issuance root."""
        if not self.entries:
            raise CUSTODY_UNAVAILABLE("R4 release custody descriptor is unavailable")
        parent_fd, _snapshot_fd, name, _expected, resource = self.entries[0]
        if name != self.owner.final_name or resource != "snapshot_root":
            raise CUSTODY_UNAVAILABLE("R4 release custody descriptor is unavailable")
        try:
            if _identity(os.fstat(parent_fd)) != _identity(os.fstat(self.owner.parent_fd)):
                raise CUSTODY_UNAVAILABLE("R4 custody parent changed")
            if _matching_temporary_exists(parent_fd, self.owner.final_name):
                raise CUSTODY_UNAVAILABLE("issuance temporary root remains")
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("R4 release custody descriptor is unavailable") from exc

    def _bound_payload(self, relative: PurePosixPath, label: str) -> bytes:
        """Read a release artifact from its retained no-follow descriptor."""
        resource = relative.as_posix()
        for _parent_fd, leaf_fd, _name, expected, entry_resource in self.entries:
            if entry_resource != resource:
                continue
            observed = os.fstat(leaf_fd)
            if _identity(observed) != expected or not stat.S_ISREG(observed.st_mode):
                raise CUSTODY_UNAVAILABLE(f"{label} descriptor changed")
            try:
                os.lseek(leaf_fd, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                remaining = observed.st_size
                while remaining:
                    chunk = os.read(leaf_fd, min(remaining, 1 << 20))
                    if not chunk:
                        raise CUSTODY_UNAVAILABLE(f"{label} descriptor truncated")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                if os.read(leaf_fd, 1):
                    raise CUSTODY_UNAVAILABLE(f"{label} descriptor size changed")
            except OSError as exc:
                raise CUSTODY_UNAVAILABLE(f"{label} descriptor is unavailable") from exc
            if _identity(os.fstat(leaf_fd)) != expected:
                raise CUSTODY_UNAVAILABLE(f"{label} descriptor changed")
            return payload
        raise CUSTODY_UNAVAILABLE(f"{label} descriptor is absent")

    def authenticate_bound_authority(self) -> None:
        """Authenticate signature and all hashes from the retained descriptors."""
        layout = self.owner._authority_layout
        if layout is None:
            raise CUSTODY_UNAVAILABLE("R4 release authority is unavailable")
        self.rebind(stage="during_bundle_auth")
        profile = _layout_source_authority(layout)
        try:
            anchor = verify_authority_anchor(
                self._bound_payload(layout.anchor_relative, "R4 authority anchor"),
                self._bound_payload(layout.signature_relative, "R4 authority signature"),
                public_key=profile.anchor_public_key,
                expected_key_id=profile.anchor_key_id,
            )
            _verify_signed_amendment_bindings(anchor)
            artifacts = anchor["artifacts"]
            if not isinstance(artifacts, Mapping):
                raise CUSTODY_UNAVAILABLE("signed artifact bindings are malformed")
            documents: dict[str, dict[str, object]] = {}
            for name, relative, label in (
                ("inventory", layout.inventory_relative, "R4 inventory"),
                ("receipt", layout.receipt_relative, "R4 transition receipt"),
                ("contract", layout.contract_relative, "R4 acquisition contract"),
                ("run_spec", layout.run_spec_relative, "R4 RunSpec"),
            ):
                binding = artifacts.get(name)
                payload = self._bound_payload(relative, label)
                if (
                    not isinstance(binding, Mapping)
                    or binding.get("path") != relative.as_posix()
                    or binding.get("sha256") != hashlib.sha256(payload).hexdigest()
                ):
                    raise CUSTODY_UNAVAILABLE(f"signed {name} hash differs")
                documents[name] = _canonical_json_from_bytes(payload, label)
            from .v52_r4_custody_artifacts import (
                validate_r4_inventory,
                validate_transition_receipt,
                verify_frozen_r4_snapshot,
            )
            from .v52_source_contracts import validate_r4_recovery_acquisition_contract

            inventory = documents["inventory"]
            receipt = documents["receipt"]
            contract = documents["contract"]
            validate_r4_inventory(inventory, profile=profile)
            if inventory.get("custody_identity_manifest_sha256") != anchor.get(
                "custody_identity_manifest_sha256"
            ):
                raise CUSTODY_UNAVAILABLE("signed inventory identity differs")
            validate_transition_receipt(receipt, inventory=inventory, profile=profile)
            comparison = receipt.get("comparison")
            binding_comparison = comparison.get("binding_comparison") if isinstance(comparison, Mapping) else None
            if not isinstance(binding_comparison, Mapping) or anchor.get("receipt_comparison") != {
                "source_content_manifest_sha256": comparison.get("source_content_manifest_sha256"),
                "snapshot_content_manifest_sha256": comparison.get("snapshot_content_manifest_sha256"),
                "source_logical_binding_manifest_sha256": binding_comparison.get("source_logical_binding_manifest_sha256"),
                "snapshot_logical_binding_manifest_sha256": binding_comparison.get("snapshot_logical_binding_manifest_sha256"),
            }:
                raise CUSTODY_UNAVAILABLE("signed receipt comparison differs")
            bindings = contract.get("bindings")
            if not isinstance(bindings, dict):
                raise CUSTODY_UNAVAILABLE("R4 contract bindings are malformed")
            validate_r4_recovery_acquisition_contract(contract, bindings, profile=profile)
            for attempt in range(4):
                try:
                    verify_frozen_r4_snapshot(layout.custody_parent / layout.final_name, inventory)
                    break
                except CUSTODY_UNAVAILABLE:
                    if attempt == 3:
                        raise
                    time.sleep(0.2)
        except (CUSTODY_UNAVAILABLE, InvalidSignature, ValueError, KeyError) as exc:
            # The labels below contain only fixed artifact/protocol names.  A
            # retained-lock failure must name the exact fail-closed check so a
            # release-race audit can distinguish descriptor, binding, and
            # snapshot drift without exposing source payloads or key material.
            raise CUSTODY_UNAVAILABLE(
                f"R4 release descriptor authentication failed: {exc}"
            ) from exc
        self.rebind(stage="during_bundle_auth")

    def close(self) -> None:
        for _parent_fd, leaf_fd, _name, _identity_value, _resource in reversed(self.entries):
            _close(leaf_fd)
        self.entries.clear()
        for root in reversed(self.roots):
            root.close()
        self.roots.clear()


def _identity_key(identity: tuple[int, int, int, int]) -> tuple[int, int, int]:
    return (identity[0], identity[1], stat.S_IFMT(identity[2]))


def _close_inherited_aliases(owners: tuple[IssuanceOwner, ...]) -> None:
    """Close every inherited descriptor alias of a custody parent/lock inode.

    ``/dev/fd`` is snapshotted before closing anything. Standard descriptors are
    considered too, but only close if their exact custody inode/type matches.
    If the platform cannot enumerate, stored owner descriptors are still closed.
    """
    identities = {
        _identity_key(identity)
        for owner in owners
        for identity in (owner._lock_identity, owner._parent_identity)
    }
    descriptors = {fd for owner in owners for fd in (owner._lock_fd, owner.parent_fd) if fd >= 0}
    descriptors.update(_snapshot_open_fd_numbers())
    for descriptor in sorted(descriptors, reverse=True):
        try:
            observed = os.fstat(descriptor)
        except OSError:
            continue
        if (observed.st_dev, observed.st_ino, stat.S_IFMT(observed.st_mode)) in identities:
            _close(descriptor)


def _snapshot_open_fd_numbers() -> set[int]:
    """Return a stable enough `/dev/fd` snapshot or fail before custody use."""
    try:
        return {int(name) for name in os.listdir("/dev/fd") if name.isdecimal()}
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("descriptor alias enumeration is unavailable") from exc


def _before_issuance_fork() -> None:
    _ACQUIRE_FORK_LOCK.acquire()


def _after_parent_issuance_fork() -> None:
    _ACQUIRE_FORK_LOCK.release()


def _invalidate_inherited_issuance_owners() -> None:
    """Child-side at-fork hook: inherited capabilities are never transferable."""
    global _FORK_CHILD_INVALIDATED
    _FORK_CHILD_INVALIDATED = True
    owners = tuple(_ACTIVE_ISSUANCE_OWNERS)
    for owner in owners:
        try:
            owner._fork_invalid = True
            owner._registered = False
        except BaseException:
            continue
    try:
        _close_inherited_aliases(owners)
    except CUSTODY_UNAVAILABLE:
        for owner in owners:
            _close(owner._lock_fd)
            _close(owner.parent_fd)
        _ACTIVE_ISSUANCE_OWNERS.clear()
        os._exit(_FORK_ENUMERATION_FAILURE_STATUS)
    for owner in owners:
        owner._lock_fd = -1
        owner.parent_fd = -1
    _ACTIVE_ISSUANCE_OWNERS.clear()
    try:
        _ACQUIRE_FORK_LOCK.release()
    except RuntimeError:
        pass


try:
    os.register_at_fork(
        before=_before_issuance_fork,
        after_in_parent=_after_parent_issuance_fork,
        after_in_child=_invalidate_inherited_issuance_owners,
    )
except (AttributeError, OSError):
    _FORK_INVALIDATION_AVAILABLE = False
else:
    _FORK_INVALIDATION_AVAILABLE = True


def acquire_issuance_lock(
    parent: Path, final_name: str, *, nonce: bytes | None = None
) -> IssuanceOwner:
    """Serialize acquisition/registration with fork invalidation."""
    with _ACQUIRE_FORK_LOCK:
        return _acquire_issuance_lock(parent, final_name, nonce=nonce)


def _acquire_issuance_lock(
    parent: Path, final_name: str, *, nonce: bytes | None = None
) -> IssuanceOwner:
    """Acquire a one-process issuance capability without following a lock."""
    final_name = _bare_name(final_name, "issuance final name")
    if nonce is None:
        nonce = secrets.token_bytes(16)
    if _FORK_CHILD_INVALIDATED:
        raise CUSTODY_UNAVAILABLE("issuance is unavailable in a fork child")
    if not isinstance(nonce, bytes) or len(nonce) != 16:
        raise CUSTODY_UNAVAILABLE("issuance nonce must contain 16 bytes")
    if hasattr(os, "fork") and not _FORK_INVALIDATION_AVAILABLE:
        raise CUSTODY_UNAVAILABLE("issuance fork invalidation is unavailable")
    _snapshot_open_fd_numbers()
    parent_fd = _open_directory_no_follow(parent)
    parent_stat = os.fstat(parent_fd)
    if not stat.S_ISDIR(parent_stat.st_mode):
        _close(parent_fd)
        raise CUSTODY_UNAVAILABLE("unsafe custody parent")
    lock_name = f".{final_name}.issuance.lock"
    lock_fd = -1
    returned = False
    try:
        if _stat_named(parent_fd, final_name) is not None:
            raise CUSTODY_UNAVAILABLE("issuance final root exists")
        lock_fd = os.open(
            lock_name,
            _leaf_flags(
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NONBLOCK", 0)
            ),
            0o600,
            dir_fd=parent_fd,
        )
        lock_stat = _require_same_named_descriptor(parent_fd, lock_name, lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("issuance lock is unsafe")
        _require_kernel_lock(lock_fd)
        _write_lock_nonce(lock_fd, nonce)
        lock_stat = _require_same_named_descriptor(parent_fd, lock_name, lock_fd)
        if _read_lock_nonce(lock_fd) != nonce:
            raise CUSTODY_UNAVAILABLE("issuance lock nonce differs")
        if _stat_named(parent_fd, final_name) is not None:
            raise CUSTODY_UNAVAILABLE("issuance final root exists")
        if _matching_temporary_exists(parent_fd, final_name):
            raise CUSTODY_UNAVAILABLE("issuance temporary root exists")
        os.fsync(parent_fd)
        owner = IssuanceOwner(
            parent_fd,
            lock_fd,
            lock_name,
            final_name,
            nonce,
            lock_stat,
            parent_stat,
            factory_token=_OWNER_FACTORY_TOKEN,
        )
        _ACTIVE_ISSUANCE_OWNERS.add(owner)
        owner._registered = True
        returned = True
        return owner
    except FileExistsError as exc:
        raise CUSTODY_UNAVAILABLE("issuance lock exists") from exc
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("issuance lock cannot be created") from exc
    finally:
        if not returned:
            if lock_fd >= 0:
                try:
                    _require_same_named_descriptor(parent_fd, lock_name, lock_fd)
                    os.unlink(lock_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except (OSError, CUSTODY_UNAVAILABLE):
                    pass
            _close(lock_fd)
            _close(parent_fd)


def _matching_temporary_exists(parent_fd: int, final_name: str) -> bool:
    prefix = f".{final_name}.issuance."
    suffix = ".tmp"
    try:
        return any(name.startswith(prefix) and name.endswith(suffix) for name in os.listdir(parent_fd))
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("custody parent cannot be listed") from exc


def _authenticated_final_root(parent_fd: int, final_name: str) -> bool:
    descriptor = -1
    try:
        descriptor = os.open(final_name, _directory_flags(), dir_fd=parent_fd)
        observed = _require_same_named_descriptor(parent_fd, final_name, descriptor)
        return stat.S_ISDIR(observed.st_mode)
    except FileNotFoundError:
        return False
    except (OSError, CUSTODY_UNAVAILABLE):
        return False
    finally:
        _close(descriptor)


@dataclass(frozen=True)
class _R4Evidence:
    snapshot: _AuthenticatedSnapshot | None = None
    anchor: _AuthenticatedAnchor | None = None
    inventory: _AuthenticatedInventory | None = None
    receipt: _AuthenticatedReceipt | None = None
    contract: _AuthenticatedContract | None = None
    checkpoints: _AuthenticatedCheckpointChain | None = None
    terminal: _AuthenticatedTerminal | None = None


def _authenticate_r4_evidence(
    layout: _R4AuthorityLayout | None,
    authenticator: _R4EvidenceAuthenticator,
    custody_parent_fd: int,
) -> _R4Evidence:
    """Authenticate the closed authority chain in order, stopping at first gap."""
    if layout is None:
        return _R4Evidence()
    snapshot = authenticator.authenticate_snapshot(layout, custody_parent_fd)
    if snapshot is None:
        return _R4Evidence()
    anchor = authenticator.authenticate_anchor(layout, snapshot)
    if anchor is None:
        return _R4Evidence(snapshot=snapshot)
    inventory = authenticator.authenticate_inventory(layout, snapshot, anchor)
    if inventory is None:
        return _R4Evidence(snapshot=snapshot, anchor=anchor)
    receipt = authenticator.authenticate_receipt(layout, snapshot, anchor, inventory)
    if receipt is None:
        return _R4Evidence(snapshot=snapshot, anchor=anchor, inventory=inventory)
    contract = authenticator.authenticate_contract(layout, snapshot, anchor, inventory, receipt)
    if contract is None:
        return _R4Evidence(
            snapshot=snapshot, anchor=anchor, inventory=inventory, receipt=receipt
        )
    checkpoints = authenticator.authenticate_checkpoint_chain(layout, contract)
    terminal = authenticator.authenticate_terminal(layout, contract)
    return _R4Evidence(snapshot, anchor, inventory, receipt, contract, checkpoints, terminal)


def _relative_entry_exists(repository_root: Path, relative: PurePosixPath) -> bool:
    """No-follow existence check of one exact fixed-layout evidence entry."""
    descriptors: list[int] = []
    try:
        descriptors.append(_open_directory_no_follow(repository_root))
        for part in relative.parts[:-1]:
            descriptor = os.open(part, _directory_flags(), dir_fd=descriptors[-1])
            _require_same_named_descriptor(descriptors[-1], part, descriptor)
            descriptors.append(descriptor)
        return _stat_named(descriptors[-1], relative.name) is not None
    except FileNotFoundError:
        return False
    except (OSError, CUSTODY_UNAVAILABLE):
        # A malformed or unavailable exact authority path is incomplete, not absent.
        return True
    finally:
        for descriptor in reversed(descriptors):
            _close(descriptor)


def _observe_issuance_state(
    layout: _R4AuthorityLayout, authenticator: _R4EvidenceAuthenticator
) -> IssuanceState:
    parent_fd = _open_directory_no_follow(layout.custody_parent)
    try:
        if _stat_named(parent_fd, f".{layout.final_name}.issuance.lock") is not None or _matching_temporary_exists(parent_fd, layout.final_name):
            return IssuanceState.OBSERVER_INCOMPLETE_LOCKED
        evidence = _authenticate_r4_evidence(layout, authenticator, parent_fd)
        if evidence.terminal is not None:
            return IssuanceState.FINAL
        if evidence.checkpoints is not None:
            return IssuanceState.RUNNING
        if evidence.contract is not None:
            return IssuanceState.EXECUTABLE
        final_root = _authenticated_final_root(parent_fd, layout.final_name)
        has_artifact = any(
            _relative_entry_exists(layout.repository_root, relative)
            for relative in (
                layout.inventory_relative,
                layout.receipt_relative,
                layout.contract_relative,
                layout.checkpoints_relative,
                layout.terminal_relative,
            )
        )
        if final_root or has_artifact:
            return IssuanceState.OBSERVER_INCOMPLETE_UNLOCKED
        return IssuanceState.ABSENT
    finally:
        _close(parent_fd)


def observe_issuance_state(repository_root: Path, custody_parent: Path) -> IssuanceState:
    """Observe only the fixed R4 layout derived from repository/custody anchors."""
    return _observe_issuance_state(
        _r4_authority_layout(repository_root, custody_parent),
        _LIVE_R4_AUTHENTICATOR,
    )


def _bind_r4_authority_for_test(
    owner: IssuanceOwner,
    layout: _R4AuthorityLayout,
    authenticator: _R4EvidenceAuthenticator,
) -> None:
    """Private fixture seam; production can bind only the unavailable adapter."""
    owner._require_live_owner()
    try:
        supplied = os.stat(layout.custody_parent, follow_symlinks=False)
        held = os.fstat(owner.parent_fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("custody parent anchor is unavailable") from exc
    if _identity(supplied) != _identity(held) or owner.final_name != layout.final_name:
        raise CUSTODY_UNAVAILABLE("test authority layout differs")
    owner._authority_layout = layout
    owner._authenticator = authenticator


def _unlink_tree_at(parent_fd: int, name: str) -> None:
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        opened = _require_same_named_descriptor(parent_fd, name, descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise CUSTODY_UNAVAILABLE("issuance temporary root is unsafe")
        os.fchmod(descriptor, 0o700)
        _unlink_children(descriptor)
        os.fsync(descriptor)
        _require_same_named_descriptor(parent_fd, name, descriptor)
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("issuance temporary cleanup failed") from exc
    finally:
        _close(descriptor)


def _unlink_children(directory_fd: int) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("issuance temporary cleanup failed") from exc
    for name in names:
        observed = _stat_named(directory_fd, name)
        if observed is None:
            raise CUSTODY_UNAVAILABLE("issuance temporary changed")
        if stat.S_ISDIR(observed.st_mode):
            _unlink_tree_at(directory_fd, name)
        else:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError as exc:
                raise CUSTODY_UNAVAILABLE("issuance temporary cleanup failed") from exc


def _hash_descriptor(fd: int, label: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return digest.hexdigest(), count
            digest.update(chunk)
            count += len(chunk)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE(f"cannot hash {label}") from exc


def _copy_regular_at(source_parent_fd: int, destination_parent_fd: int, name: str) -> None:
    source_fd = destination_fd = -1
    try:
        named = _stat_named(source_parent_fd, name)
        if named is None:
            raise CUSTODY_UNAVAILABLE("source member disappeared")
        if stat.S_ISLNK(named.st_mode):
            raise CUSTODY_UNAVAILABLE("source symlink is forbidden")
        if not stat.S_ISREG(named.st_mode):
            raise CUSTODY_UNAVAILABLE("source special file is forbidden")
        if named.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("source member link count differs")
        source_fd = os.open(
            name,
            _leaf_flags(os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)),
            dir_fd=source_parent_fd,
        )
        opened = _require_same_named_descriptor(source_parent_fd, name, source_fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("source member link count differs")
        destination_fd = os.open(
            name,
            _leaf_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=destination_parent_fd,
        )
        destination_opened = _require_same_named_descriptor(
            destination_parent_fd, name, destination_fd
        )
        if not stat.S_ISREG(destination_opened.st_mode) or destination_opened.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("destination member is unsafe")
        source_hash = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            source_hash.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise CUSTODY_UNAVAILABLE("destination member write failed")
                view = view[written:]
        if os.fstat(source_fd).st_size != size:
            raise CUSTODY_UNAVAILABLE("source member changed while copying")
        os.fsync(destination_fd)
        os.fchmod(destination_fd, 0o444)
        os.fsync(destination_fd)
    except FileExistsError as exc:
        raise CUSTODY_UNAVAILABLE("destination member already exists") from exc
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("descriptor snapshot copy failed") from exc
    finally:
        _close(destination_fd)
        _close(source_fd)
    verify_fd = -1
    try:
        verify_fd = os.open(name, _leaf_flags(os.O_RDONLY), dir_fd=destination_parent_fd)
        verified = _require_same_named_descriptor(destination_parent_fd, name, verify_fd)
        if not stat.S_ISREG(verified.st_mode) or verified.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("destination member is unsafe")
        destination_hash, destination_size = _hash_descriptor(verify_fd, "destination member")
        if (source_hash.hexdigest(), size) != (destination_hash, destination_size):
            raise CUSTODY_UNAVAILABLE("source and destination hashes differ")
    finally:
        _close(verify_fd)


def _copy_tree(source_fd: int, destination_fd: int) -> None:
    try:
        names = sorted(os.listdir(source_fd))
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("source tree cannot be listed") from exc
    for name in names:
        observed = _stat_named(source_fd, name)
        if observed is None:
            raise CUSTODY_UNAVAILABLE("source member disappeared")
        if stat.S_ISLNK(observed.st_mode):
            raise CUSTODY_UNAVAILABLE("source symlink is forbidden")
        if stat.S_ISREG(observed.st_mode):
            _copy_regular_at(source_fd, destination_fd, name)
            continue
        if not stat.S_ISDIR(observed.st_mode):
            raise CUSTODY_UNAVAILABLE("source special file is forbidden")
        child_source_fd = child_destination_fd = -1
        try:
            child_source_fd = os.open(
                name, _directory_flags() | os.O_NONBLOCK, dir_fd=source_fd
            )
            _require_same_named_descriptor(source_fd, name, child_source_fd)
            os.mkdir(name, 0o700, dir_fd=destination_fd)
            child_destination_fd = os.open(name, _directory_flags(), dir_fd=destination_fd)
            _require_same_named_descriptor(destination_fd, name, child_destination_fd)
            _copy_tree(child_source_fd, child_destination_fd)
            os.fchmod(child_destination_fd, 0o555)
            os.fsync(child_destination_fd)
            os.fsync(destination_fd)
        except FileExistsError as exc:
            raise CUSTODY_UNAVAILABLE("destination directory already exists") from exc
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("descriptor snapshot copy failed") from exc
        finally:
            _close(child_destination_fd)
            _close(child_source_fd)


def validate_source_closure(closure: SnapshotSourceClosure) -> SnapshotCopyResult:
    """Derive the approved 1,675/4,711/5,025 logical closure before copying."""
    if not isinstance(closure, SnapshotSourceClosure):
        raise CUSTODY_UNAVAILABLE("source closure is malformed")
    bindings: list[tuple[int, str, PurePosixPath]] = []
    members: set[PurePosixPath] = set()
    for index, record in enumerate(closure.records):
        if not isinstance(record, Mapping):
            raise CUSTODY_UNAVAILABLE("source closure record is malformed")
        for role in ("manifest", "raw", "seal"):
            value = record.get(role)
            if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
                raise CUSTODY_UNAVAILABLE("source closure binding is malformed")
            path = PurePosixPath(value["path"])
            try:
                suffix = path.relative_to(closure.logical_raw_root)
            except ValueError as exc:
                raise CUSTODY_UNAVAILABLE("source closure path escapes logical root") from exc
            if not suffix.parts or any(part in {"", ".", ".."} for part in suffix.parts):
                raise CUSTODY_UNAVAILABLE("source closure path is malformed")
            bindings.append((index, role, path))
            members.add(path)
    if len(closure.records) != 1675 or len(members) != 4711 or len(bindings) != 5025:
        raise CUSTODY_UNAVAILABLE("source closure cardinality differs")
    if tuple(sorted(closure.root_locks)) != (
        ".baseball_savant_statcast.lock",
        ".mlb_schedule.lock",
        ".mlb_stats_pitcher_bf.lock",
        ".mlb_stats_pitching.lock",
    ):
        raise CUSTODY_UNAVAILABLE("source closure root locks differ")
    for shared, count in (
        (
            PurePosixPath("data/v5_1_bt/raw/mlb_stats_pitcher_bf/729bad325d9eafb22883c3318896f2baf877c99b5600fa77aba2bb9f6da6a6e7.raw"),
            85,
        ),
        (
            PurePosixPath("data/v5_1_bt/raw/mlb_stats_pitcher_bf/f8615992094a949312f8a27b4e129e9c14abd006b994818642110733e260d86f.raw"),
            231,
        ),
    ):
        matching = [binding for binding in bindings if binding[2] == shared]
        if len(matching) != count or any(role != "raw" for _, role, _ in matching):
            raise CUSTODY_UNAVAILABLE("source closure shared binding differs")
    return SnapshotCopyResult(1675, 4711, 5025, tuple(sorted(closure.root_locks)))


def _closure_members(closure: SnapshotSourceClosure) -> dict[Path, tuple[str, int]]:
    members: dict[Path, tuple[str, int]] = {}
    for record in closure.records:
        for role in ("manifest", "raw", "seal"):
            value = record[role]
            assert isinstance(value, Mapping)
            path = PurePosixPath(str(value["path"]))
            suffix = Path(*path.relative_to(closure.logical_raw_root).parts)
            sha256 = value.get("sha256")
            byte_size = value.get("byte_size")
            if not isinstance(sha256, str) or not isinstance(byte_size, int):
                raise CUSTODY_UNAVAILABLE("source closure member identity is malformed")
            identity = (sha256, byte_size)
            previous = members.setdefault(suffix, identity)
            if previous != identity:
                raise CUSTODY_UNAVAILABLE("source closure member identity conflicts")
    return members


def _leaf_paths(directory_fd: int, prefix: Path = Path()) -> set[Path]:
    leaves: set[Path] = set()
    for name in os.listdir(directory_fd):
        observed = _stat_named(directory_fd, name)
        if observed is None or stat.S_ISLNK(observed.st_mode):
            raise CUSTODY_UNAVAILABLE("source closure leaf is unsafe")
        relative = prefix / name
        if stat.S_ISDIR(observed.st_mode):
            child = -1
            try:
                child = os.open(name, _directory_flags() | os.O_NONBLOCK, dir_fd=directory_fd)
                _require_same_named_descriptor(directory_fd, name, child)
                leaves.update(_leaf_paths(child, relative))
            finally:
                _close(child)
        elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
            leaves.add(relative)
        else:
            raise CUSTODY_UNAVAILABLE("source closure leaf is unsafe")
    return leaves


def _hash_relative_regular(root_fd: int, relative: Path) -> tuple[str, int]:
    descriptors = [os.dup(root_fd)]
    try:
        for part in relative.parts[:-1]:
            descriptor = os.open(part, _directory_flags() | os.O_NONBLOCK, dir_fd=descriptors[-1])
            _require_same_named_descriptor(descriptors[-1], part, descriptor)
            descriptors.append(descriptor)
        descriptor = os.open(relative.name, _leaf_flags(os.O_RDONLY | os.O_NONBLOCK), dir_fd=descriptors[-1])
        _require_same_named_descriptor(descriptors[-1], relative.name, descriptor)
        descriptors.append(descriptor)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("source closure member is unsafe")
        return _hash_descriptor(descriptor, "source closure member")
    finally:
        _close_all = descriptors
        for descriptor in reversed(_close_all):
            _close(descriptor)


def _validate_closure_tree(source_fd: int, closure: SnapshotSourceClosure) -> dict[Path, tuple[str, int]]:
    members = _closure_members(closure)
    expected = set(members) | {Path(lock) for lock in closure.root_locks}
    if _leaf_paths(source_fd) != expected:
        raise CUSTODY_UNAVAILABLE("source closure members differ")
    for lock in closure.root_locks:
        observed = _stat_named(source_fd, lock)
        if observed is None or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or observed.st_size != 0:
            raise CUSTODY_UNAVAILABLE("source closure lock differs")
    for path, identity in members.items():
        if _hash_relative_regular(source_fd, path) != identity:
            raise CUSTODY_UNAVAILABLE("source closure member hash differs")
    return members


def _populate_snapshot_from_closure(
    owner: IssuanceOwner, source: Path, closure: SnapshotSourceClosure
) -> SnapshotCopyResult:
    """Copy a source tree into only this owner's no-follow temporary sibling."""
    owner.require_pre_rename_owner()
    source_fd = _open_directory_no_follow(source, nonblocking=True)
    try:
        return _populate_snapshot_from_open_source(owner, source_fd, closure)
    finally:
        _close(source_fd)


def _populate_snapshot_from_open_source(
    owner: IssuanceOwner, source_fd: int, closure: SnapshotSourceClosure
) -> SnapshotCopyResult:
    result = validate_source_closure(closure)
    destination_fd = -1
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISDIR(source_stat.st_mode):
            raise CUSTODY_UNAVAILABLE("source root is not a directory")
        members = _validate_closure_tree(source_fd, closure)
        os.mkdir(owner.temporary_name, 0o700, dir_fd=owner.parent_fd)
        destination_fd = os.open(
            owner.temporary_name, _directory_flags(), dir_fd=owner.parent_fd
        )
        _require_same_named_descriptor(owner.parent_fd, owner.temporary_name, destination_fd)
        _copy_tree(source_fd, destination_fd)
        for relative, identity in members.items():
            if _hash_relative_regular(destination_fd, relative) != identity:
                raise CUSTODY_UNAVAILABLE("destination closure member hash differs")
        os.fchmod(destination_fd, 0o555)
        os.fsync(destination_fd)
        os.fsync(owner.parent_fd)
        owner.require_pre_rename_owner()
    except FileExistsError as exc:
        raise CUSTODY_UNAVAILABLE("issuance temporary root already exists") from exc
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("snapshot population failed") from exc
    finally:
        _close(destination_fd)
    return result


def _read_source_closure_from_root(
    root: object, *, inventory_relative: PurePosixPath, expected_sha256: str,
    profile: R4SourceAuthorityProfile | None = None,
) -> SnapshotSourceClosure:
    """Private descriptor-only binding seam; public callers use fixed constants."""
    try:
        snapshot = read_regular_snapshot(
            root,
            Path(*inventory_relative.parts),
            "approved source inventory",
            nonblocking=True,
        )
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("approved source inventory is unavailable") from exc
    if snapshot is None or hashlib.sha256(snapshot.contents).hexdigest() != expected_sha256:
        raise CUSTODY_UNAVAILABLE("approved source inventory digest differs")
    try:
        document = json.loads(snapshot.contents)
    except json.JSONDecodeError as exc:
        raise CUSTODY_UNAVAILABLE("approved source inventory is malformed") from exc
    if (
        not isinstance(document, Mapping)
        or document.get("legacy_root") != (profile.logical_raw_root if profile else _APPROVED_LOGICAL_RAW_ROOT).as_posix()
        or not isinstance(document.get("records"), list)
        or document.get("manifest_count") != 1675
    ):
        raise CUSTODY_UNAVAILABLE("approved source inventory differs")
    try:
        normalized_records, _ = _r4_authority._verify_records(document["records"])
    except ValueError as exc:
        raise CUSTODY_UNAVAILABLE("approved source inventory differs") from exc
    closure = SnapshotSourceClosure(
        normalized_records,
        profile.logical_raw_root if profile else _APPROVED_LOGICAL_RAW_ROOT,
    )
    if profile is not None and tuple(closure.records) != tuple(profile.closure.records):
        raise CUSTODY_UNAVAILABLE("approved source inventory records differ")
    return closure


def _read_source_closure(
    authority_root: Path, *, inventory_relative: Path, expected_sha256: str
) -> SnapshotSourceClosure:
    """Private test seam; public issuance keeps one root capability open."""
    with open_trusted_root(authority_root, nonblocking=True) as root:
        return _read_source_closure_from_root(
            root,
            inventory_relative=PurePosixPath(inventory_relative),
            expected_sha256=expected_sha256,
        )


def _open_relative_directory(root_fd: int, relative: PurePosixPath) -> int:
    descriptors = [os.dup(root_fd)]
    try:
        for part in relative.parts:
            descriptor = os.open(
                part, _directory_flags() | os.O_NONBLOCK, dir_fd=descriptors[-1]
            )
            _require_same_named_descriptor(descriptors[-1], part, descriptor)
            descriptors.append(descriptor)
        result = descriptors.pop()
        return result
    finally:
        for descriptor in reversed(descriptors):
            _close(descriptor)


def _assert_authority_root_bound(root: object) -> None:
    try:
        root.assert_bound()
    except (OSError, ValueError, AttributeError) as exc:
        raise CUSTODY_UNAVAILABLE("source authority root changed") from exc


def _populate_snapshot_from_authority(
    owner: IssuanceOwner,
    authority_root: Path,
    binding: _SourceAuthorityBinding,
) -> SnapshotCopyResult:
    """Hold one root capability across inventory binding and raw traversal."""
    raw_fd = -1
    try:
        with open_trusted_root(authority_root, nonblocking=True) as root:
            closure = _read_source_closure_from_root(
                root,
                inventory_relative=binding.inventory_relative,
                expected_sha256=binding.expected_inventory_sha256,
            )
            _assert_authority_root_bound(root)
            raw_fd = _open_relative_directory(root.root_fd, binding.raw_relative)
            # Hold the raw descriptor derived from the same authenticated root;
            # a lexical authority-path swap cannot redirect this traversal.
            result = _populate_snapshot_from_open_source(owner, raw_fd, closure)
            _assert_authority_root_bound(root)
            owner._bound_closure = closure
            return result
    finally:
        _close(raw_fd)


def _populate_snapshot_from_profile(
    owner: IssuanceOwner, profile: R4SourceAuthorityProfile
) -> SnapshotCopyResult:
    """Copy one resolved profile while retaining a single descriptor root."""
    raw_fd = -1
    try:
        with open_trusted_root(profile.authority_root, nonblocking=True) as root:
            closure = _read_source_closure_from_root(
                root,
                inventory_relative=profile.inventory_relative,
                expected_sha256=profile.inventory_sha256,
                profile=profile,
            )
            _assert_authority_root_bound(root)
            raw_fd = _open_relative_directory(root.root_fd, profile.logical_raw_root)
            result = _populate_snapshot_from_open_source(owner, raw_fd, closure)
            _assert_authority_root_bound(root)
            owner._bound_closure = closure
            owner._source_authority_profile = profile
            return result
    finally:
        _close(raw_fd)


def populate_snapshot(owner: IssuanceOwner, source_authority_root: Path) -> SnapshotCopyResult:
    """Copy only the fixed production source authority; callers cannot override it."""
    root = Path(source_authority_root)
    issuance = _issuance_authority(
        root / Path(*LOGICAL_RAW_ROOT.parts),
        root / Path(*INVENTORY_RELATIVE.parts),
    )
    return _populate_snapshot_from_profile(owner, issuance.profile)


def _populate_snapshot_for_test(owner: IssuanceOwner, source: Path) -> None:
    """Private small-tree fixture helper; it is never a production authority API."""
    owner.require_pre_rename_owner()
    source_fd = destination_fd = -1
    try:
        source_fd = _open_directory_no_follow(source, nonblocking=True)
        os.mkdir(owner.temporary_name, 0o700, dir_fd=owner.parent_fd)
        destination_fd = os.open(owner.temporary_name, _directory_flags(), dir_fd=owner.parent_fd)
        _require_same_named_descriptor(owner.parent_fd, owner.temporary_name, destination_fd)
        _copy_tree(source_fd, destination_fd)
        os.fchmod(destination_fd, 0o555)
        os.fsync(destination_fd)
        os.fsync(owner.parent_fd)
        owner._bound_closure = _TestSnapshotCopy()
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("test snapshot population failed") from exc
    finally:
        _close(destination_fd)
        _close(source_fd)


def publish_owned_snapshot(
    owner: IssuanceOwner, temporary_name: str, final_name: str
) -> None:
    """Atomically publish the owner tree only if its final sibling is absent."""
    temporary_name = _bare_name(temporary_name, "issuance temporary name")
    final_name = _bare_name(final_name, "issuance final name")
    if temporary_name != owner.temporary_name or final_name != owner.final_name:
        raise CUSTODY_UNAVAILABLE("issuance publication names differ from owner")
    owner.require_pre_rename_owner()
    if _stat_named(owner.parent_fd, owner.final_name) is not None:
        raise CUSTODY_UNAVAILABLE("issuance final root exists")
    try:
        rename_exclusive_at(
            temporary_name,
            final_name,
            src_dir_fd=owner.parent_fd,
            dst_dir_fd=owner.parent_fd,
        )
        owner._rename_completed = True
        os.fsync(owner.parent_fd)
        _freeze_owned_snapshot(owner)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("snapshot immutable publication failed") from exc
    owner._record_snapshot_published()


def _set_immutable_flag(descriptor: int) -> None:
    if sys.platform != "darwin":
        raise CUSTODY_UNAVAILABLE("UF_IMMUTABLE is unavailable")
    try:
        function = ctypes.CDLL(None, use_errno=True).fchflags
        function.argtypes = (ctypes.c_int, ctypes.c_uint)
        function.restype = ctypes.c_int
        if function(descriptor, getattr(stat, "UF_IMMUTABLE", 0x00000002)) != 0:
            raise OSError(ctypes.get_errno(), "fchflags failed")
    except (AttributeError, OSError) as exc:
        raise CUSTODY_UNAVAILABLE("UF_IMMUTABLE cannot be set") from exc


def _verify_immutable_flag(descriptor: int) -> None:
    try:
        flags = os.fstat(descriptor).st_flags
    except AttributeError as exc:
        raise CUSTODY_UNAVAILABLE("UF_IMMUTABLE is unavailable") from exc
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("UF_IMMUTABLE cannot be verified") from exc
    if not flags & getattr(stat, "UF_IMMUTABLE", 0x00000002):
        raise CUSTODY_UNAVAILABLE("UF_IMMUTABLE differs")


def _full_fsync(descriptor: int, label: str) -> None:
    """Durably order APFS immutable metadata before any identity capture."""
    if sys.platform != "darwin" or not hasattr(fcntl, "F_FULLFSYNC"):
        raise CUSTODY_UNAVAILABLE(f"{label} full fsync is unavailable")
    try:
        fcntl.fcntl(descriptor, fcntl.F_FULLFSYNC)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} full fsync failed") from exc


def _volume_full_sync(descriptor: int, label: str) -> None:
    """Wait for the custody volume to persist all immutable metadata changes."""
    if sys.platform != "darwin":
        raise CUSTODY_UNAVAILABLE(f"{label} volume full sync is unavailable")
    try:
        function = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).fsync_volume_np
        function.argtypes = (ctypes.c_int, ctypes.c_int)
        function.restype = ctypes.c_int
        if function(descriptor, 0x01 | 0x02) != 0:
            error = ctypes.get_errno()
            if error:
                raise OSError(error, "fsync_volume_np failed")
            raise CUSTODY_UNAVAILABLE(f"{label} volume full sync failed")
    except (AttributeError, OSError) as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} volume full sync failed") from exc


def _freeze_tree(directory_fd: int) -> None:
    """Set/verify every descendant first, deliberately leaving this root last."""
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("snapshot cannot be listed for immutable flag") from exc
    for name in names:
        observed = _stat_named(directory_fd, name)
        if observed is None or stat.S_ISLNK(observed.st_mode):
            raise CUSTODY_UNAVAILABLE("snapshot member changed before immutable flag")
        child_fd = -1
        try:
            if stat.S_ISDIR(observed.st_mode):
                child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                _require_same_named_descriptor(directory_fd, name, child_fd)
                _freeze_tree(child_fd)
            elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
                child_fd = os.open(name, _leaf_flags(os.O_RDONLY), dir_fd=directory_fd)
                _require_same_named_descriptor(directory_fd, name, child_fd)
                _set_immutable_flag(child_fd)
                _verify_immutable_flag(child_fd)
                _full_fsync(child_fd, "snapshot member")
            else:
                raise CUSTODY_UNAVAILABLE("snapshot member is unsafe")
        finally:
            _close(child_fd)
    _set_immutable_flag(directory_fd)
    _verify_immutable_flag(directory_fd)
    _full_fsync(directory_fd, "snapshot directory")


def _verify_snapshot_modes(directory_fd: int) -> None:
    if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o555:
        raise CUSTODY_UNAVAILABLE("published snapshot directory mode differs")
    for name in os.listdir(directory_fd):
        observed = _stat_named(directory_fd, name)
        if observed is None:
            raise CUSTODY_UNAVAILABLE("published snapshot member disappeared")
        descriptor = -1
        try:
            if stat.S_ISDIR(observed.st_mode):
                descriptor = os.open(name, _directory_flags(), dir_fd=directory_fd)
                _require_same_named_descriptor(directory_fd, name, descriptor)
                _verify_snapshot_modes(descriptor)
            elif stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
                if stat.S_IMODE(observed.st_mode) != 0o444:
                    raise CUSTODY_UNAVAILABLE("published snapshot member mode differs")
            else:
                raise CUSTODY_UNAVAILABLE("published snapshot member is unsafe")
        finally:
            _close(descriptor)


def _freeze_owned_snapshot(owner: IssuanceOwner) -> None:
    """Rehash, mode-verify, and immutable-flag the renamed root before acceptance."""
    owner._require_live_owner()
    root_fd = -1
    try:
        root_fd = os.open(owner.final_name, _directory_flags(), dir_fd=owner.parent_fd)
        _require_same_named_descriptor(owner.parent_fd, owner.final_name, root_fd)
        closure = owner._bound_closure
        if closure is None:
            raise CUSTODY_UNAVAILABLE("published snapshot has no bound source closure")
        if isinstance(closure, SnapshotSourceClosure):
            validate_source_closure(closure)
            _validate_closure_tree(root_fd, closure)
            empty_sha256 = hashlib.sha256(b"").hexdigest()
            for lock in closure.root_locks:
                observed = _stat_named(root_fd, lock)
                if (
                    observed is None
                    or stat.S_IMODE(observed.st_mode) != 0o444
                    or _hash_relative_regular(root_fd, Path(lock))
                    != (empty_sha256, 0)
                ):
                    raise CUSTODY_UNAVAILABLE("published snapshot lock differs")
        elif not isinstance(closure, _TestSnapshotCopy):
            raise CUSTODY_UNAVAILABLE("published snapshot has no bound source closure")
        _verify_snapshot_modes(root_fd)
        _freeze_tree(root_fd)
        _full_fsync(root_fd, "snapshot root")
        _full_fsync(owner.parent_fd, "custody parent")
        _volume_full_sync(owner.parent_fd, "custody parent")
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("snapshot immutable publication failed") from exc
    finally:
        _close(root_fd)


def _write_owned_regular_file(
    parent_fd: int, name: str, contents: bytes
) -> os.stat_result:
    name = _bare_name(name, "temporary file name")
    descriptor = -1
    owned: os.stat_result | None = None
    try:
        descriptor = os.open(
            name,
            _leaf_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
            0o600,
            dir_fd=parent_fd,
        )
        opened = _require_same_named_descriptor(parent_fd, name, descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("temporary publication file is unsafe")
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise CUSTODY_UNAVAILABLE("temporary publication write failed")
            view = view[written:]
        os.fsync(descriptor)
        owned = _require_same_named_descriptor(parent_fd, name, descriptor)
    except FileExistsError as exc:
        raise CUSTODY_UNAVAILABLE("temporary publication file already exists") from exc
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("temporary publication write failed") from exc
    finally:
        _close(descriptor)
    if owned is None:
        raise CUSTODY_UNAVAILABLE("temporary publication file is unsafe")
    return owned


def _read_published_regular(parent_fd: int, name: str, payload: bytes) -> str:
    """Reopen the final directory entry and bind its exact canonical bytes."""
    def regular_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_ctime_ns,
            value.st_size,
            stat.S_IMODE(value.st_mode),
            value.st_nlink,
        )

    descriptor = rebound = -1
    try:
        descriptor = os.open(name, _leaf_flags(os.O_RDONLY), dir_fd=parent_fd)
        observed = _require_same_named_descriptor(parent_fd, name, descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("published canonical file is unsafe")
        initial_nlink = observed.st_nlink
        digest, size = _hash_descriptor(descriptor, "published canonical file")
        if size != len(payload) or digest != hashlib.sha256(payload).hexdigest():
            raise CUSTODY_UNAVAILABLE("published canonical file differs")
        # A final-name swap after the descriptor hash must invalidate this
        # publication, even when the original descriptor still has good bytes.
        current = _require_same_named_descriptor(parent_fd, name, descriptor)
        if current.st_nlink != initial_nlink:
            raise CUSTODY_UNAVAILABLE("published canonical file final path changed")
        expected_identity = regular_identity(current)
        if regular_identity(os.fstat(descriptor)) != expected_identity:
            raise CUSTODY_UNAVAILABLE("published canonical file final path changed")
        rebound = os.open(name, _leaf_flags(os.O_RDONLY), dir_fd=parent_fd)
        rebound_stat = _require_same_named_descriptor(parent_fd, name, rebound)
        if regular_identity(rebound_stat) != expected_identity:
            raise CUSTODY_UNAVAILABLE("published canonical file final path changed")
        return digest
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("published canonical file cannot be reopened") from exc
    finally:
        _close(rebound)
        _close(descriptor)


def publish_canonical_json(
    parent_fd: int,
    temporary_name: str,
    final_name: str,
    document: Mapping[str, object],
) -> str:
    """Durably write and exclusively publish one canonical JSON document."""
    if not isinstance(document, Mapping):
        raise CUSTODY_UNAVAILABLE("canonical document must be a mapping")
    # Validate both names before creating the owned temporary entry.  This
    # preserves the fail-closed publication surface for artifact callers.
    temporary_name = _bare_name(temporary_name, "temporary file name")
    final_name = _bare_name(final_name, "final file name")
    payload = canonical_json_bytes(document) + b"\n"
    _write_owned_regular_file(parent_fd, temporary_name, payload)
    try:
        rename_exclusive_at(
            temporary_name,
            final_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except OSError:
        # A namespace swap may occur after any identity check and before an
        # unlink.  Without an exclusive audit-directory capability, retaining
        # this incomplete residue is the only fail-closed policy.
        raise
    return _read_published_regular(parent_fd, final_name, payload)


def provision_custody_parent(custody_parent: Path) -> Path:
    """Create exactly one missing custody directory, never a parent chain.

    A pre-existing directory is accepted only after no-follow reopening.  This
    makes the operation idempotent without treating a symlink or a regular
    file as a provisioned custody anchor.
    """
    parent = Path(custody_parent)
    if not parent.name or parent.name in {".", ".."}:
        raise CUSTODY_UNAVAILABLE("unsafe custody parent")
    ancestor_fd = child_fd = -1
    try:
        ancestor_fd = _open_directory_no_follow(parent.parent, nonblocking=True)
        existing = _stat_named(ancestor_fd, parent.name)
        if existing is None:
            os.mkdir(parent.name, 0o755, dir_fd=ancestor_fd)
            os.fsync(ancestor_fd)
        elif not stat.S_ISDIR(existing.st_mode):
            raise CUSTODY_UNAVAILABLE("custody parent is not a directory")
        child_fd = os.open(parent.name, _directory_flags(), dir_fd=ancestor_fd)
        _require_same_named_descriptor(ancestor_fd, parent.name, child_fd)
        os.fsync(child_fd)
        return parent
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("custody parent cannot be provisioned") from exc
    finally:
        _close(child_fd)
        _close(ancestor_fd)


def _source_authority_from_pair(
    source_root: Path, source_inventory: Path,
    profile: R4SourceAuthorityProfile | None = None,
) -> Path:
    """Derive the one source repository from its fixed raw/inventory pair."""
    raw = Path(source_root).absolute()
    inventory = Path(source_inventory).absolute()
    raw_relative = profile.logical_raw_root if profile is not None else _APPROVED_LOGICAL_RAW_ROOT
    try:
        authority = raw.parents[len(raw_relative.parts) - 1]
    except IndexError as exc:
        raise CUSTODY_UNAVAILABLE("source raw root differs") from exc
    inventory_relative = profile.inventory_relative if profile is not None else PurePosixPath(*_APPROVED_SOURCE_INVENTORY.parts)
    if raw != authority / Path(*raw_relative.parts) or inventory != authority / Path(*inventory_relative.parts):
        raise CUSTODY_UNAVAILABLE("source authority paths differ")
    if profile is not None and authority != profile.authority_root:
        raise CUSTODY_UNAVAILABLE("source authority profile root differs")
    return authority


def _preflight_inputs(
    *, source_root: Path, source_inventory: Path, custody_parent: Path, final_name: str,
    profile: R4SourceAuthorityProfile | None = None,
    output_repository: Path | None = None,
) -> SnapshotCopyResult:
    resolved = profile or _issuance_authority(source_root, source_inventory).profile
    source = _source_authority_from_pair(source_root, source_inventory, resolved)
    if _bare_name(final_name, "issuance final name") != "v5_1_bt_r4":
        raise CUSTODY_UNAVAILABLE("issuance final name differs from fixed R4 layout")
    parent_fd = -1
    try:
        parent_fd = _open_directory_no_follow(Path(custody_parent), nonblocking=True)
        # Source and output repositories are intentionally distinct in normal
        # operation.  Only the RunSpec-derived output repository can contain
        # prior R4 authority evidence, so never let source-side state decide
        # whether this one-shot output transaction may begin.
        observed_repository = Path.cwd() if output_repository is None else Path(output_repository)
        if observe_issuance_state(observed_repository, Path(custody_parent)) is not IssuanceState.ABSENT:
            raise CUSTODY_UNAVAILABLE("custody authority is not absent")
        # The APFS call is deliberately descriptor based; no path/mount lookup
        # is permitted for a production custody anchor.
        from .v52_r4_apfs import apfs_volume_uuid_from_fd

        apfs_volume_uuid_from_fd(parent_fd)
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("custody parent is unavailable") from exc
    finally:
        _close(parent_fd)
    try:
        with open_trusted_root(source, nonblocking=True) as root:
            closure = _read_source_closure_from_root(
                root,
                inventory_relative=resolved.inventory_relative,
                expected_sha256=resolved.inventory_sha256,
                profile=resolved,
            )
            _assert_authority_root_bound(root)
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("source authority is unavailable") from exc
    return validate_source_closure(closure)


def preflight(
    *, source_root: Path, source_inventory: Path, custody_parent: Path, final_name: str
) -> SnapshotCopyResult:
    """Read-only validation for a single exact source/custody pairing."""
    profile = _issuance_authority(source_root, source_inventory).profile
    return _preflight_inputs(
        source_root=source_root,
        source_inventory=source_inventory,
        custody_parent=custody_parent,
        final_name=final_name,
        profile=profile,
    )


def _repository_for_issuance(inputs: IssuanceInputs, profile: R4SourceAuthorityProfile) -> Path:
    run_spec = Path(inputs.run_spec).absolute()
    repository = run_spec.parent.parent.parent
    layout = _r4_authority_layout(repository, Path(inputs.custody_parent))
    source_authority = _source_authority_from_pair(
        inputs.source_root, inputs.source_inventory, profile
    )
    expected_source_inventory = source_authority / Path(*profile.inventory_relative.parts)
    approval_inventory = Path(inputs.source_approval_inventory)
    if approval_inventory.is_absolute():
        supplied_approval_inventory = approval_inventory.absolute()
    else:
        approval_relative = PurePosixPath(str(approval_inventory))
        if (
            approval_relative.is_absolute()
            or approval_relative != profile.inventory_relative
            or any(part in {"", ".", ".."} for part in approval_relative.parts)
        ):
            raise CUSTODY_UNAVAILABLE("issuance source approval inventory differs")
        supplied_approval_inventory = source_authority / Path(*approval_relative.parts)
    expected = {
        "run_spec": repository / Path(*layout.run_spec_relative.parts),
        "contract": repository / Path(*layout.contract_relative.parts),
        "audit_root": repository / Path(*layout.inventory_relative.parent.parts),
        "source_inventory": expected_source_inventory,
        "source_approval_inventory": expected_source_inventory,
    }
    supplied = {
        "run_spec": run_spec,
        "contract": Path(inputs.contract).absolute(),
        "audit_root": Path(inputs.audit_root).absolute(),
        "source_inventory": Path(inputs.source_inventory).absolute(),
        "source_approval_inventory": supplied_approval_inventory,
    }
    if (
        inputs.source_approval_logical_root != profile.logical_raw_root.as_posix()
        or _bare_name(inputs.final_name, "issuance final name") != layout.final_name
        or any(supplied[key] != expected[key] for key in expected)
    ):
        raise CUSTODY_UNAVAILABLE("issuance inputs differ from fixed R4 authority")
    return repository


def _prepare_artifact_parents(repository: Path, layout: _R4AuthorityLayout) -> None:
    """Create only the two fixed repository-relative output parents."""
    try:
        with open_trusted_root(repository, nonblocking=True) as root:
            for relative in (layout.inventory_relative.parent, layout.contract_relative.parent):
                descriptors = [os.dup(root.root_fd)]
                try:
                    for part in relative.parts:
                        existing = _stat_named(descriptors[-1], part)
                        if existing is None:
                            os.mkdir(part, 0o755, dir_fd=descriptors[-1])
                            os.fsync(descriptors[-1])
                        elif not stat.S_ISDIR(existing.st_mode):
                            raise CUSTODY_UNAVAILABLE("R4 artifact parent is not a directory")
                        child = os.open(part, _directory_flags(), dir_fd=descriptors[-1])
                        _require_same_named_descriptor(descriptors[-1], part, child)
                        descriptors.append(child)
                    os.fsync(descriptors[-1])
                finally:
                    for descriptor in reversed(descriptors):
                        _close(descriptor)
            root.assert_bound()
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("R4 artifact parent is unavailable") from exc


def _require_output_authority_absent(
    repository: Path, layout: _R4AuthorityLayout,
) -> None:
    """Reject every final output before creating a parent, lock, or snapshot.

    This deliberately looks below the *output* repository rather than the
    source authority repository.  Existing output is evidence, never a retry
    candidate, so a caller must inspect it manually instead of adopting it.
    """
    for relative in (
        layout.inventory_relative,
        layout.receipt_relative,
        layout.contract_relative,
        layout.anchor_relative,
        layout.signature_relative,
    ):
        if _relative_entry_exists(repository, relative):
            raise CUSTODY_UNAVAILABLE("R4 authority output already exists")


def _read_repository_regular(
    repository: Path, relative: PurePosixPath, label: str, *, max_bytes: int | None = None,
) -> bytes:
    try:
        with open_trusted_root(repository, nonblocking=True) as root:
            snapshot = read_regular_snapshot(
                root, Path(*relative.parts), label, nonblocking=True,
                max_bytes=max_bytes,
            )
            root.assert_bound()
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable") from exc
    if snapshot is None:
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable")
    return snapshot.contents


def _git_blob_id(payload: bytes) -> str:
    """Calculate the Git SHA-1 blob ID without executing a Git subprocess."""
    return hashlib.sha1(
        b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    ).hexdigest()


def _publish_owned_bytes(parent_fd: int, temporary_name: str, final_name: str, payload: bytes) -> str:
    """Exclusive, descriptor-bound publication for a non-JSON static artifact."""
    _write_owned_regular_file(parent_fd, temporary_name, payload)
    try:
        rename_exclusive_at(
            temporary_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
        )
        os.fsync(parent_fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("R4 static artifact cannot be published") from exc
    return _read_published_regular(parent_fd, final_name, payload)


def _freeze_regular_artifact(repository: Path, relative: PurePosixPath) -> None:
    """Freeze one exact regular final entry through its no-follow parent fd."""
    parent_fd = descriptor = -1
    try:
        parent_fd = _open_directory_no_follow(repository / Path(*relative.parent.parts))
        descriptor = os.open(relative.name, _leaf_flags(os.O_RDONLY), dir_fd=parent_fd)
        observed = _require_same_named_descriptor(parent_fd, relative.name, descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("R4 static artifact is unsafe")
        os.fchmod(descriptor, 0o444)
        _set_immutable_flag(descriptor)
        current = _require_same_named_descriptor(parent_fd, relative.name, descriptor)
        if stat.S_IMODE(current.st_mode) != 0o444:
            raise CUSTODY_UNAVAILABLE("R4 static artifact mode differs")
        _verify_immutable_flag(descriptor)
        os.fsync(descriptor)
        os.fsync(parent_fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("R4 static artifact cannot be frozen") from exc
    finally:
        _close(descriptor)
        _close(parent_fd)


def _require_frozen_regular_artifact(repository: Path, relative: PurePosixPath) -> None:
    """Require a stable, immutable 0444 final entry without following it."""
    parent_fd = descriptor = -1
    try:
        parent_fd = _open_directory_no_follow(repository / Path(*relative.parent.parts))
        descriptor = os.open(relative.name, _leaf_flags(os.O_RDONLY), dir_fd=parent_fd)
        observed = _require_same_named_descriptor(parent_fd, relative.name, descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or stat.S_IMODE(observed.st_mode) != 0o444
        ):
            raise CUSTODY_UNAVAILABLE("R4 static artifact custody differs")
        _verify_immutable_flag(descriptor)
        _require_same_named_descriptor(parent_fd, relative.name, descriptor)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("R4 static artifact custody differs") from exc
    finally:
        _close(descriptor)
        _close(parent_fd)


def _publish_signed_anchor(
    owner: IssuanceOwner,
    repository: Path,
    layout: _R4AuthorityLayout,
    issuance: _r4_authority._IssuanceAuthority,
) -> None:
    """Publish the final signature pair while the original owner lock is held."""
    owner._require_live_owner()
    owner.require_r4_repository_authority(repository)
    inventory_bytes = _read_repository_regular(repository, layout.inventory_relative, "R4 inventory")
    receipt_bytes = _read_repository_regular(repository, layout.receipt_relative, "R4 transition receipt")
    contract_bytes = _read_repository_regular(repository, layout.contract_relative, "R4 acquisition contract")
    run_spec_bytes = _read_repository_regular(repository, layout.run_spec_relative, "R4 RunSpec")
    try:
        inventory = _canonical_json_from_bytes(inventory_bytes, "R4 inventory")
        receipt = _canonical_json_from_bytes(receipt_bytes, "R4 transition receipt")
        source_tree = Path(__file__).resolve().parent.parent
        design_relative = PurePosixPath(
            "docs/superpowers/specs/2026-08-05-v5-2-r4-signed-custody-anchor-design.md"
        )
        plan_relative = PurePosixPath(
            "docs/superpowers/plans/2026-08-05-v5-2-r4-signed-custody-anchor.md"
        )
        design_bytes = _read_repository_regular(source_tree, design_relative, "R4 amendment design")
        plan_bytes = _read_repository_regular(source_tree, plan_relative, "R4 amendment plan")
        document = build_authority_anchor(
            key_id=issuance.profile.anchor_key_id,
            inventory=inventory,
            receipt=receipt,
            inventory_bytes=inventory_bytes,
            receipt_bytes=receipt_bytes,
            contract_bytes=contract_bytes,
            run_spec_bytes=run_spec_bytes,
            amendment_design_bytes=design_bytes,
            amendment_design_git_blob_id=_git_blob_id(design_bytes),
            amendment_plan_bytes=plan_bytes,
            amendment_plan_git_blob_id=_git_blob_id(plan_bytes),
        )
        anchor_bytes = canonical_json_bytes(document) + b"\n"
        signature = issuance.sign_anchor_message(DOMAIN_MESSAGE + anchor_bytes)
    except (CUSTODY_UNAVAILABLE, OSError, TypeError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("R4 authority anchor cannot be signed") from exc
    if not isinstance(signature, bytes) or len(signature) != 64:
        raise CUSTODY_UNAVAILABLE("R4 authority anchor signature differs")
    try:
        verify_authority_anchor(
            anchor_bytes, signature,
            public_key=issuance.profile.anchor_public_key,
            expected_key_id=issuance.profile.anchor_key_id,
        )
        with open_trusted_root(repository / Path(*layout.anchor_relative.parent.parts), nonblocking=True) as root:
            _publish_owned_bytes(
                root.root_fd, f".{layout.anchor_relative.name}.{secrets.token_hex(16)}.tmp",
                layout.anchor_relative.name, anchor_bytes,
            )
            _publish_owned_bytes(
                root.root_fd, f".{layout.signature_relative.name}.{secrets.token_hex(16)}.tmp",
                layout.signature_relative.name, signature,
            )
            root.assert_bound()
        for relative in (
            layout.inventory_relative, layout.receipt_relative, layout.contract_relative,
            layout.anchor_relative, layout.signature_relative,
        ):
            _freeze_regular_artifact(repository, relative)
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("R4 authority anchor cannot be published") from exc


def _r4_contract_document(
    repository: Path, layout: _R4AuthorityLayout,
    profile: R4SourceAuthorityProfile,
) -> dict[str, object]:
    """Derive R4's closed contract from the immutable R3 semantic template."""
    template_relative = PurePosixPath(
        "protocols/v5_2_qd/"
        "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
    )
    template = _read_canonical_repository_json(repository, template_relative, "R3 contract template")

    def replace_authority(value: object) -> object:
        if isinstance(value, dict):
            return {key: replace_authority(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_authority(item) for item in value]
        if isinstance(value, str):
            return value.replace(
                "recovery_schedule_conflicts_missing_venue_r3",
                "recovery_schedule_conflicts_missing_venue_r4",
            )
        return value

    document = replace_authority(template)
    if not isinstance(document, dict):
        raise CUSTODY_UNAVAILABLE("R3 contract template is malformed")
    run_spec = _read_canonical_repository_json(repository, layout.run_spec_relative, "R4 RunSpec")
    inventory = _read_canonical_repository_json(repository, layout.inventory_relative, "R4 inventory")
    receipt = _read_canonical_repository_json(repository, layout.receipt_relative, "R4 transition receipt")
    document["run_id"] = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4"
    document["bindings"] = {
        "approved_design": {
            "path": "docs/superpowers/specs/2026-08-04-v5-2-r4-physical-custody-authority-design.md",
            "sha256": "372e001466d1d1fa3109307e0fd69784110abb8e2db71509bbe68447fad41663",
            "git_blob_id": "81c8587ccb433be156b859dc8a7eb170241ca06c",
        },
        "implementation_plan": {
            "path": "docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md",
            "working_tree_sha256": "a8ee2e245fb9a446099dfd862472e72f4f4d77ee9e5850d27a098f60ebebc1d2",
            "git_blob_id": "f6e0cef0b4689ee5c45d495b3c89b46d861a257f",
        },
        "run_spec": {
            "path": layout.run_spec_relative.as_posix(),
            "sha256": hashlib.sha256(canonical_json_bytes(run_spec) + b"\n").hexdigest(),
            "git_blob_id": "d48909ded446d68ace15f6dc927a071808ef9a4a",
        },
    }
    document["legacy_protection"] = dict(document["legacy_protection"])
    document["legacy_protection"]["protected_inventory_path"] = layout.inventory_relative.as_posix()
    document["legacy_protection"]["protected_inventory_sha256"] = hashlib.sha256(canonical_json_bytes(inventory) + b"\n").hexdigest()
    document["physical_custody_transition"] = {
        "source_approval": {
            **dict(profile.source_approval),
        },
        "r4_inventory": {
            "path": layout.inventory_relative.as_posix(),
            "sha256": hashlib.sha256(canonical_json_bytes(inventory) + b"\n").hexdigest(),
        },
        "receipt": {
            "path": layout.receipt_relative.as_posix(),
            "sha256": hashlib.sha256(canonical_json_bytes(receipt) + b"\n").hexdigest(),
        },
    }
    return document


def _publish_contract(
    owner: IssuanceOwner, repository: Path, layout: _R4AuthorityLayout,
    profile: R4SourceAuthorityProfile,
) -> None:
    owner._require_live_owner()
    document = _r4_contract_document(repository, layout, profile)
    from .v52_source_contracts import validate_r4_recovery_acquisition_contract

    bindings = document.get("bindings")
    if not isinstance(bindings, dict):
        raise CUSTODY_UNAVAILABLE("R4 contract bindings are malformed")
    try:
        validate_r4_recovery_acquisition_contract(document, bindings, profile=profile)
        with open_trusted_root(repository / Path(*layout.contract_relative.parent.parts), nonblocking=True) as root:
            publish_canonical_json(
                root.root_fd,
                f".{layout.contract_relative.name}.{secrets.token_hex(16)}.tmp",
                layout.contract_relative.name,
                document,
            )
            root.assert_bound()
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("R4 contract cannot be published") from exc


def issue_and_bind(inputs: IssuanceInputs) -> IssuanceResult:
    """Execute the non-recoverable, single-owner R4 publication transaction."""
    if not isinstance(inputs, IssuanceInputs):
        raise CUSTODY_UNAVAILABLE("issuance inputs are malformed")
    issuance = _issuance_authority(inputs.source_root, inputs.source_inventory)
    profile = issuance.profile
    repository = _repository_for_issuance(inputs, profile)
    layout = _r4_authority_layout(repository, Path(inputs.custody_parent), profile)
    # This is intentionally before source/custody preflight, parent creation,
    # lock acquisition, and every snapshot mutation.
    _require_output_authority_absent(repository, layout)
    _preflight_inputs(
        source_root=inputs.source_root,
        source_inventory=inputs.source_inventory,
        custody_parent=inputs.custody_parent,
        final_name=inputs.final_name,
        profile=profile,
        output_repository=repository,
    )
    _prepare_artifact_parents(repository, layout)
    from .v52_r4_custody_artifacts import publish_inventory, publish_transition_receipt

    with acquire_issuance_lock(Path(inputs.custody_parent), layout.final_name) as owner:
        owner._source_authority_profile = profile
        owner.configure_r4_authority(repository, Path(inputs.custody_parent))
        owner._authenticator = _LIVE_R4_AUTHENTICATOR
        _populate_snapshot_from_profile(owner, profile)
        publish_owned_snapshot(owner, owner.temporary_name, owner.final_name)
        continuation = publish_inventory(
            owner, repository_root=repository, custody_parent=Path(inputs.custody_parent)
        )
        publish_transition_receipt(
            owner, continuation, repository_root=repository, custody_parent=Path(inputs.custody_parent)
        )
        _publish_contract(owner, repository, layout, profile)
        _publish_signed_anchor(owner, repository, layout, issuance)
        # release performs a new full evidence read rather than consulting an
        # in-memory publication flag.
        owner.release_after_contract_authentication()
    result = IssuanceResult(
        Path(inputs.custody_parent) / layout.final_name,
        repository / Path(*layout.inventory_relative.parts),
        repository / Path(*layout.receipt_relative.parts),
        repository / Path(*layout.contract_relative.parts),
        repository / Path(*layout.run_spec_relative.parts),
        repository / Path(*layout.anchor_relative.parts),
        repository / Path(*layout.signature_relative.parts),
    )
    verify(
        custody_root=result.custody_root,
        inventory=result.inventory_path,
        receipt=result.receipt_path,
        run_spec=result.run_spec_path,
        contract=result.contract_path,
    )
    return result


def verify(*, custody_root: Path, inventory: Path, receipt: Path, run_spec: Path, contract: Path) -> IssuanceResult:
    """Read-only verification of the closed issued authority."""
    custody = Path(custody_root)
    repository = Path(run_spec).absolute().parent.parent.parent
    parent = custody.parent
    profile = _r4_authority._resolve_source_authority()
    layout = _r4_authority_layout(repository, parent, profile)
    expected = (
        parent / layout.final_name,
        repository / Path(*layout.inventory_relative.parts),
        repository / Path(*layout.receipt_relative.parts),
        repository / Path(*layout.run_spec_relative.parts),
        repository / Path(*layout.contract_relative.parts),
    )
    if tuple(Path(value).absolute() for value in (custody, inventory, receipt, run_spec, contract)) != tuple(path.absolute() for path in expected):
        raise CUSTODY_UNAVAILABLE("verification inputs differ from fixed R4 authority")
    if _stat_path_exists(parent / f".{layout.final_name}.issuance.lock"):
        raise CUSTODY_UNAVAILABLE("issuance remains locked")
    parent_fd = -1
    try:
        parent_fd = _open_directory_no_follow(parent)
        if _matching_temporary_exists(parent_fd, layout.final_name):
            raise CUSTODY_UNAVAILABLE("issuance temporary root remains")
        evidence = _authenticate_r4_evidence(layout, _LIVE_R4_AUTHENTICATOR, parent_fd)
        if evidence.contract is None:
            raise CUSTODY_UNAVAILABLE("issued R4 authority is incomplete")
    finally:
        _close(parent_fd)
    return IssuanceResult(
        custody, inventory, receipt, contract, run_spec,
        repository / Path(*layout.anchor_relative.parts),
        repository / Path(*layout.signature_relative.parts),
    )


def _stat_path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False


def _parser() -> ArgumentParser:
    parser = ArgumentParser(prog="v52-r4-snapshot-issuer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    provision = subparsers.add_parser("provision-custody-parent")
    provision.add_argument("--custody-parent", required=True)
    check = subparsers.add_parser("preflight")
    for name in ("source-root", "source-inventory", "custody-parent", "final-name"):
        check.add_argument(f"--{name}", required=True)
    issue = subparsers.add_parser("issue-and-bind")
    for name in (
        "source-root", "source-inventory", "source-approval-logical-root",
        "source-approval-inventory", "run-spec", "contract", "custody-parent",
        "final-name", "audit-root",
    ):
        issue.add_argument(f"--{name}", required=True)
    verify_parser = subparsers.add_parser("verify")
    for name in ("custody-root", "inventory", "receipt", "run-spec", "contract"):
        verify_parser.add_argument(f"--{name}", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "provision-custody-parent":
            provision_custody_parent(Path(args.custody_parent))
        elif args.command == "preflight":
            preflight(source_root=Path(args.source_root), source_inventory=Path(args.source_inventory), custody_parent=Path(args.custody_parent), final_name=args.final_name)
        elif args.command == "issue-and-bind":
            _validate_cli_source_approval_inventory(args.source_approval_inventory)
            issue_and_bind(IssuanceInputs(
                Path(args.source_root), Path(args.source_inventory), args.source_approval_logical_root,
                Path(args.source_approval_inventory), Path(args.run_spec), Path(args.contract),
                Path(args.custody_parent), args.final_name, Path(args.audit_root),
            ))
        else:
            verify(custody_root=Path(args.custody_root), inventory=Path(args.inventory), receipt=Path(args.receipt), run_spec=Path(args.run_spec), contract=Path(args.contract))
    except CUSTODY_UNAVAILABLE:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
