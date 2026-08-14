"""Inventory-bound promotion of protected v5.1 raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import weakref
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from pathlib import PurePosixPath
from typing import BinaryIO, Mapping, Protocol, Sequence

from .v52_evidence import (
    LogicalQuery,
    PublishedEvidence,
    ProtectedLegacyPublishReverifyError,
    V52EvidenceStore,
    logical_query_id,
)
from .v52_legacy_relation import matches_current_query_to_legacy
from .v52_protected_inventory import (
    ProtectedMembershipSnapshot,
    parse_protected_raw_inventory_document,
    snapshot_protected_inventory_membership,
    verify_protected_raw_inventory,
)
from .v52_source_contracts import canonical_json_bytes, recompute_query_sidecar


_LEGACY_ROOT = Path("data/v5_1_bt/raw")
_INVENTORY_PATH = Path("audit/v5_1_bt/protected_raw_inventory.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PromotionDisposition(StrEnum):
    PROMOTED = "PROMOTED"
    FETCH_REQUIRED = "FETCH_REQUIRED"
    FATAL_INTEGRITY = "FATAL_INTEGRITY"


class PromotionReasonCode(StrEnum):
    NO_EXACT_PROTECTED_CANDIDATE = "NO_EXACT_PROTECTED_CANDIDATE"
    CURRENT_QUERY_INVALID = "CURRENT_QUERY_INVALID"
    CURRENT_PARSER_CONTRACT_UNSATISFIED = (
        "CURRENT_PARSER_CONTRACT_UNSATISFIED"
    )
    CURRENT_SEMANTIC_CONTRACT_UNSATISFIED = "CURRENT_SEMANTIC_CONTRACT_UNSATISFIED"
    PROTECTED_PATH_INVALID = "PROTECTED_PATH_INVALID"
    PROTECTED_INVENTORY_INVALID = "PROTECTED_INVENTORY_INVALID"
    PROTECTED_MEMBERSHIP_INVALID = "PROTECTED_MEMBERSHIP_INVALID"
    LEGACY_LINK_TYPE_INVALID = "LEGACY_LINK_TYPE_INVALID"
    LEGACY_PATH_IDENTITY_CHANGED = "LEGACY_PATH_IDENTITY_CHANGED"
    LEGACY_MANIFEST_INVALID = "LEGACY_MANIFEST_INVALID"
    LEGACY_FILENAME_INVALID = "LEGACY_FILENAME_INVALID"
    LEGACY_SEAL_INVALID = "LEGACY_SEAL_INVALID"
    LEGACY_RAW_BYTES_INVALID = "LEGACY_RAW_BYTES_INVALID"
    LEGACY_ROW_COUNT_INVALID = "LEGACY_ROW_COUNT_INVALID"
    CATALOG_NOT_UNIQUE = "CATALOG_NOT_UNIQUE"
    PROTECTED_PUBLISH_REVERIFY_FAILED = "PROTECTED_PUBLISH_REVERIFY_FAILED"


@dataclass(frozen=True)
class LegacyCandidate:
    source: str
    manifest_path: Path
    manifest_sha256: str
    seal_path: Path
    payload_path: Path
    raw_sha256: str


@dataclass(frozen=True)
class ProtectedLegacyCatalog:
    repository_root: Path
    legacy_root: Path
    protected_inventory_path: Path
    protected_inventory_sha256: str
    protected_inventory_identity: tuple[int, ...]
    candidates: tuple[LegacyCandidate, ...]
    records: tuple[_CatalogRecord, ...]
    membership: ProtectedMembershipSnapshot


@dataclass(frozen=True)
class PromotionDecision:
    query_id: str
    disposition: PromotionDisposition
    reason_codes: tuple[PromotionReasonCode, ...]
    candidate: ProtectedRawRecord | None


@dataclass(frozen=True)
class LegacyDecisionEvidence:
    decision: PromotionDecision
    candidate_lineage: Mapping[str, object] | None
    semantic_sha256: str | None


class ProtectedLegacyIntegrityError(RuntimeError):
    def __init__(self, reason_code: PromotionReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


class LegacyPromotionNotEligibleError(RuntimeError):
    def __init__(self, reason_code: PromotionReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


@dataclass(frozen=True)
class _Snapshot:
    contents: bytes
    identity: tuple[int, ...]
    binding: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class _ProtectedObjectBinding:
    relative_path: str
    sha256: str
    byte_size: int
    device: int
    inode: int


@dataclass(frozen=True)
class _CatalogRecord:
    candidate: LegacyCandidate
    canonical_query_json: str
    manifest: _ProtectedObjectBinding
    seal: _ProtectedObjectBinding
    raw: _ProtectedObjectBinding


@dataclass(frozen=True)
class LogicalProtectedRawRecord:
    """Logical (content-only) identity of one protected raw artifact.

    R4 custody keeps this value separate from the R3 device/inode binding.  A
    record is deliberately limited to its canonical logical path, content
    digest, and byte size; physical identity is supplied by the R4 custody
    layer alongside it.
    """

    path: PurePosixPath
    sha256: str
    byte_size: int

    def __post_init__(self) -> None:
        if type(self.path) is not PurePosixPath:
            raise ValueError("logical protected raw path must be a PurePosixPath")
        if (
            self.path.is_absolute()
            or not self.path.parts
            or any(
                part in {"", ".", ".."} or "\\" in part
                for part in self.path.parts
            )
            or self.path.as_posix() == "."
        ):
            raise ValueError("logical protected raw path is not canonical")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise ValueError("logical protected raw sha256 is invalid")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 0
        ):
            raise ValueError("logical protected raw byte_size is invalid")

    def as_mapping(self) -> dict[str, object]:
        return {
            "path": self.path.as_posix(),
            "sha256": self.sha256,
            "byte_size": self.byte_size,
        }


@dataclass(frozen=True)
class ProtectedRawRecord:
    """Canonical, logical identity of one protected legacy record."""

    source: str
    canonical_query_json: str
    manifest_path: PurePosixPath
    manifest_sha256: str
    manifest_byte_size: int
    manifest_device: int
    manifest_inode: int
    seal_path: PurePosixPath
    seal_sha256: str
    seal_byte_size: int
    seal_device: int
    seal_inode: int
    payload_path: PurePosixPath
    payload_sha256: str
    payload_byte_size: int
    payload_device: int
    payload_inode: int


class LegacyEvidenceCapability(Protocol):
    logical_raw_root: PurePosixPath
    inventory_sha256: str
    records: tuple[ProtectedRawRecord, ...]

    def validate(self) -> None: ...

    def rebind(self) -> None: ...

    def read_bytes(self, relative_path: PurePosixPath) -> bytes: ...

    def open_bound_object(self, relative_path: PurePosixPath) -> BinaryIO: ...

    def close(self) -> None: ...


_ISSUED_LEGACY_EVIDENCE: dict[int, weakref.ReferenceType[object]] = {}


def _register_issued_legacy_evidence(
    capability: LegacyEvidenceCapability,
) -> LegacyEvidenceCapability:
    identity = id(capability)

    def discard(_reference: weakref.ReferenceType[object]) -> None:
        _ISSUED_LEGACY_EVIDENCE.pop(identity, None)

    _ISSUED_LEGACY_EVIDENCE[identity] = weakref.ref(capability, discard)
    return capability


def _is_issued_legacy_evidence(capability: object) -> bool:
    reference = _ISSUED_LEGACY_EVIDENCE.get(id(capability))
    return reference is not None and reference() is capability


def _public_record(record: _CatalogRecord) -> ProtectedRawRecord:
    def path(binding: _ProtectedObjectBinding) -> PurePosixPath:
        return PurePosixPath(binding.relative_path)

    return ProtectedRawRecord(
        source=record.candidate.source,
        canonical_query_json=record.canonical_query_json,
        manifest_path=path(record.manifest),
        manifest_sha256=record.manifest.sha256,
        manifest_byte_size=record.manifest.byte_size,
        manifest_device=record.manifest.device,
        manifest_inode=record.manifest.inode,
        seal_path=path(record.seal),
        seal_sha256=record.seal.sha256,
        seal_byte_size=record.seal.byte_size,
        seal_device=record.seal.device,
        seal_inode=record.seal.inode,
        payload_path=path(record.raw),
        payload_sha256=record.raw.sha256,
        payload_byte_size=record.raw.byte_size,
        payload_device=record.raw.device,
        payload_inode=record.raw.inode,
    )


@dataclass(frozen=True)
class RootLocalLegacyEvidenceAdapter:
    """Issue the existing root-local catalog through the capability boundary."""

    catalog: ProtectedLegacyCatalog

    @property
    def logical_raw_root(self) -> PurePosixPath:
        return PurePosixPath(_LEGACY_ROOT.as_posix())

    @property
    def inventory_sha256(self) -> str:
        return self.catalog.protected_inventory_sha256

    @property
    def records(self) -> tuple[ProtectedRawRecord, ...]:
        return tuple(_public_record(record) for record in self.catalog.records)

    def validate(self) -> None:
        _revalidate_root_local_catalog(self.catalog)

    def rebind(self) -> None:
        self.validate()

    def _relative_path(self, relative_path: PurePosixPath) -> Path:
        if type(relative_path) is not PurePosixPath:
            raise TypeError("protected raw path must be a PurePosixPath")
        allowed = {
            path
            for record in self.records
            for path in (record.manifest_path, record.seal_path, record.payload_path)
        }
        if relative_path not in allowed:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.PROTECTED_PATH_INVALID
            )
        return self.catalog.repository_root / Path(relative_path.as_posix())

    def read_bytes(self, relative_path: PurePosixPath) -> bytes:
        root, _ = _revalidate_root_local_catalog(self.catalog)
        snapshot = _snapshot(root, self._relative_path(relative_path))
        _revalidate_root_local_catalog(self.catalog)
        return snapshot.contents

    def open_bound_object(self, relative_path: PurePosixPath) -> BinaryIO:
        return BytesIO(self.read_bytes(relative_path))

    def close(self) -> None:
        return None


def adapt_root_local_legacy_catalog(
    catalog: ProtectedLegacyCatalog,
) -> RootLocalLegacyEvidenceAdapter:
    if type(catalog) is not ProtectedLegacyCatalog:
        raise TypeError("catalog must be an issued ProtectedLegacyCatalog")
    _revalidate_root_local_catalog(catalog)
    return _register_issued_legacy_evidence(RootLocalLegacyEvidenceAdapter(catalog))


def _require_issued_legacy_evidence(
    capability: LegacyEvidenceCapability,
) -> LegacyEvidenceCapability:
    if (
        type(capability) is RootLocalLegacyEvidenceAdapter
        and _is_issued_legacy_evidence(capability)
    ):
        return capability
    try:
        from .v52_external_protected_parent import ExternalProtectedParentCapability
    except ImportError:
        ExternalProtectedParentCapability = None  # type: ignore[assignment,misc]
    if (
        ExternalProtectedParentCapability is not None
        and type(capability) is ExternalProtectedParentCapability
        and _is_issued_legacy_evidence(capability)
    ):
        return capability
    raise TypeError("legacy evidence capability was not issued by a trusted factory")


@dataclass(frozen=True)
class _VerifiedRecord:
    candidate: ProtectedRawRecord
    canonical_query: Mapping[str, object]
    manifest: Mapping[str, object]
    inventory_record: Mapping[str, object]
    payload: bytes


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _trusted_root(root: Path) -> Path:
    supplied = Path(os.path.abspath(os.fspath(root)))
    allowed_system_aliases = {Path("/tmp"), Path("/var")}
    current = Path(supplied.anchor)
    try:
        linked = current.lstat()
        for part in supplied.parts[1:]:
            current /= part
            linked = current.lstat()
            if stat.S_ISLNK(linked.st_mode) and current not in allowed_system_aliases:
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.PROTECTED_PATH_INVALID
                )
    except OSError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PATH_INVALID
        ) from exc
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.PROTECTED_PATH_INVALID)
    return supplied.resolve(strict=True)


def trusted_legacy_repository_root(root: Path) -> Path:
    """Return the authenticated canonical repository root for legacy reads."""
    return _trusted_root(root)


def _exact_path(root: Path, supplied: Path, expected: Path) -> Path:
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        linked = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PATH_INVALID
        ) from exc
    if stat.S_ISLNK(linked.st_mode) or resolved != root / expected:
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.PROTECTED_PATH_INVALID)
    return resolved


def _snapshot(root: Path, path: Path) -> _Snapshot:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PATH_INVALID
        ) from exc
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    leaf_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        filesystem_descriptor = os.open(os.sep, directory_flags)
        descriptors.append(filesystem_descriptor)
        filesystem_identity = _directory_identity(os.fstat(filesystem_descriptor))
        binding = [filesystem_identity]
        directory_bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
        parent_descriptor = filesystem_descriptor

        def open_bound_directory(component: str) -> None:
            nonlocal parent_descriptor
            linked = os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
            child_descriptor = os.open(
                component, directory_flags, dir_fd=parent_descriptor
            )
            descriptors.append(child_descriptor)
            opened = os.fstat(child_descriptor)
            if not stat.S_ISDIR(linked.st_mode) or not stat.S_ISDIR(opened.st_mode):
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
                )
            if _directory_identity(linked) != _directory_identity(opened):
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
                )
            expected = _directory_identity(opened)
            binding.append(expected)
            directory_bindings.append(
                (parent_descriptor, component, child_descriptor, expected)
            )
            parent_descriptor = child_descriptor

        for component in root.parts[1:]:
            open_bound_directory(component)
        for component in relative.parts[:-1]:
            open_bound_directory(component)

        leaf_name = relative.name
        linked = os.stat(leaf_name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(leaf_name, leaf_flags, dir_fd=parent_descriptor)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
            )
        if _identity(opened) != _identity(linked):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
            )
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(leaf_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _identity(opened) != _identity(after) or _identity(after) != _identity(
            rebound
        ):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
            )
        for parent_fd, name, child_fd, expected in directory_bindings:
            rebound_directory = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _directory_identity(os.fstat(child_fd)) != expected
                or _directory_identity(rebound_directory) != expected
            ):
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
                )
        if _directory_identity(os.fstat(filesystem_descriptor)) != filesystem_identity:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
            )
        binding.append(_identity(after))
        return _Snapshot(b"".join(chunks), _identity(after), tuple(binding))
    except ProtectedLegacyIntegrityError:
        raise
    except OSError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _rebind(root: Path, path: Path, expected: _Snapshot) -> None:
    if _snapshot(root, path) != expected:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )


def _sha(value: object, reason: PromotionReasonCode) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProtectedLegacyIntegrityError(reason)
    return value


def _object_binding(item: Mapping[str, object]) -> _ProtectedObjectBinding:
    fingerprint = item.get("inode_fingerprint")
    if not isinstance(fingerprint, Mapping):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    try:
        return _ProtectedObjectBinding(
            relative_path=str(item["path"]),
            sha256=str(item["sha256"]),
            byte_size=int(item["byte_size"]),
            device=int(fingerprint["device"]),
            inode=int(fingerprint["inode"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        ) from exc


def _catalog_records(
    root: Path, inventory: Mapping[str, object]
) -> tuple[_CatalogRecord, ...]:
    raw_records = inventory.get("records")
    if not isinstance(raw_records, list):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    records: list[_CatalogRecord] = []
    seen: set[tuple[str, str]] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.PROTECTED_INVENTORY_INVALID
            )
        canonical_query = raw_record.get("canonical_query")
        if not isinstance(canonical_query, Mapping):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.PROTECTED_INVENTORY_INVALID
            )
        source = str(raw_record.get("source"))
        canonical_query_json = json.dumps(
            canonical_query,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        key = (source, canonical_query_json)
        if key in seen:
            raise ProtectedLegacyIntegrityError(PromotionReasonCode.CATALOG_NOT_UNIQUE)
        seen.add(key)
        try:
            manifest = _object_binding(raw_record["manifest"])
            seal = _object_binding(raw_record["seal"])
            raw = _object_binding(raw_record["raw"])
        except (KeyError, TypeError) as exc:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.PROTECTED_INVENTORY_INVALID
            ) from exc
        manifest_path = _exact_path(
            root, root / manifest.relative_path, Path(manifest.relative_path)
        )
        seal_path = _exact_path(root, root / seal.relative_path, Path(seal.relative_path))
        payload_path = _exact_path(root, root / raw.relative_path, Path(raw.relative_path))
        records.append(
            _CatalogRecord(
                candidate=LegacyCandidate(
                    source=source,
                    manifest_path=manifest_path,
                    manifest_sha256=manifest.sha256,
                    seal_path=seal_path,
                    payload_path=payload_path,
                    raw_sha256=raw.sha256,
                ),
                canonical_query_json=canonical_query_json,
                manifest=manifest,
                seal=seal,
                raw=raw,
            )
        )
    return tuple(sorted(records, key=lambda item: item.manifest.relative_path))


def _protected_inventory_record(record: ProtectedRawRecord) -> dict[str, object]:
    def item(
        path: PurePosixPath,
        sha256: str,
        byte_size: int,
        device: int,
        inode: int,
    ) -> dict[str, object]:
        return {
            "byte_size": byte_size,
            "inode_fingerprint": {"device": device, "inode": inode},
            "path": path.as_posix(),
            "sha256": sha256,
        }

    return {
        "canonical_query": json.loads(record.canonical_query_json),
        "manifest": item(
            record.manifest_path,
            record.manifest_sha256,
            record.manifest_byte_size,
            record.manifest_device,
            record.manifest_inode,
        ),
        "raw": item(
            record.payload_path,
            record.payload_sha256,
            record.payload_byte_size,
            record.payload_device,
            record.payload_inode,
        ),
        "rawstore_verified": True,
        "seal": item(
            record.seal_path,
            record.seal_sha256,
            record.seal_byte_size,
            record.seal_device,
            record.seal_inode,
        ),
        "source": record.source,
    }


def _revalidate_root_local_catalog(
    catalog: ProtectedLegacyCatalog,
) -> tuple[Path, _Snapshot]:
    root = _trusted_root(catalog.repository_root)
    _exact_path(root, root / catalog.legacy_root, _LEGACY_ROOT)
    inventory_path = _exact_path(
        root, root / catalog.protected_inventory_path, _INVENTORY_PATH
    )
    inventory_snapshot = _snapshot(root, inventory_path)
    if inventory_snapshot.identity != catalog.protected_inventory_identity:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )
    if hashlib.sha256(inventory_snapshot.contents).hexdigest() != _sha(
        catalog.protected_inventory_sha256,
        PromotionReasonCode.PROTECTED_INVENTORY_INVALID,
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    if catalog.candidates != tuple(record.candidate for record in catalog.records) or len(
        catalog.candidates
    ) != len(set(catalog.candidates)):
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.CATALOG_NOT_UNIQUE)
    try:
        inventory = parse_protected_raw_inventory_document(inventory_snapshot.contents)
        membership = snapshot_protected_inventory_membership(root, inventory)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        ) from exc
    if membership != catalog.membership:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )
    _rebind(root, inventory_path, inventory_snapshot)
    return root, inventory_snapshot


def _capability_integrity_error(exc: Exception) -> ProtectedLegacyIntegrityError:
    if isinstance(exc, ProtectedLegacyIntegrityError):
        return exc
    return ProtectedLegacyIntegrityError(
        PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
    )


def _revalidate_catalog(
    legacy_evidence: LegacyEvidenceCapability,
) -> LegacyEvidenceCapability:
    capability = _require_issued_legacy_evidence(legacy_evidence)
    try:
        capability.validate()
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ProtectedLegacyIntegrityError):
            raise
        raise _capability_integrity_error(exc) from exc
    return capability


def _read_capability_bytes(
    legacy_evidence: LegacyEvidenceCapability,
    relative_path: PurePosixPath,
) -> bytes:
    try:
        return legacy_evidence.read_bytes(relative_path)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ProtectedLegacyIntegrityError):
            raise
        raise _capability_integrity_error(exc) from exc


def _assert_public_object_binding(
    contents: bytes,
    *,
    expected_sha256: str,
    expected_byte_size: int,
    reason: PromotionReasonCode,
) -> None:
    if (
        len(contents) != expected_byte_size
        or hashlib.sha256(contents).hexdigest() != expected_sha256
    ):
        raise ProtectedLegacyIntegrityError(reason)


def _verify_selected_revalidated(
    legacy_evidence: LegacyEvidenceCapability,
    record: ProtectedRawRecord,
) -> _VerifiedRecord:
    manifest_bytes = _read_capability_bytes(
        legacy_evidence, record.manifest_path
    )
    seal_bytes = _read_capability_bytes(legacy_evidence, record.seal_path)
    payload = _read_capability_bytes(legacy_evidence, record.payload_path)
    _assert_public_object_binding(
        manifest_bytes,
        expected_sha256=record.manifest_sha256,
        expected_byte_size=record.manifest_byte_size,
        reason=PromotionReasonCode.LEGACY_MANIFEST_INVALID,
    )
    _assert_public_object_binding(
        seal_bytes,
        expected_sha256=record.seal_sha256,
        expected_byte_size=record.seal_byte_size,
        reason=PromotionReasonCode.LEGACY_SEAL_INVALID,
    )
    _assert_public_object_binding(
        payload,
        expected_sha256=record.payload_sha256,
        expected_byte_size=record.payload_byte_size,
        reason=PromotionReasonCode.LEGACY_RAW_BYTES_INVALID,
    )
    if seal_bytes != f"{record.manifest_sha256}\n".encode("ascii"):
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.LEGACY_SEAL_INVALID)
    try:
        manifest = json.loads(manifest_bytes)
        canonical_query = json.loads(record.canonical_query_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_MANIFEST_INVALID
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(canonical_query, Mapping)
        or manifest.get("source") != record.source
        or manifest.get("query") != canonical_query
        or manifest.get("sha256") != record.payload_sha256
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_MANIFEST_INVALID
        )
    expected_name = (
        f"{record.payload_sha256}."
        f"{hashlib.sha256(canonical_json_bytes(canonical_query)).hexdigest()}"
        ".manifest.json"
    )
    if record.manifest_path.name != expected_name:
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.LEGACY_FILENAME_INVALID)
    row_count = sum(bool(line.strip()) for line in payload.splitlines())
    if manifest.get("row_count") != row_count:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_ROW_COUNT_INVALID
        )
    try:
        legacy_evidence.rebind()
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ProtectedLegacyIntegrityError):
            raise
        raise _capability_integrity_error(exc) from exc
    return _VerifiedRecord(
        candidate=record,
        canonical_query=dict(canonical_query),
        manifest=dict(manifest),
        inventory_record=_protected_inventory_record(record),
        payload=payload,
    )


def _verify_selected(
    legacy_evidence: LegacyEvidenceCapability,
    record: ProtectedRawRecord,
) -> _VerifiedRecord:
    capability = _revalidate_catalog(legacy_evidence)
    return _verify_selected_revalidated(capability, record)


def load_protected_legacy_catalog(
    repository_root: Path,
    *,
    legacy_root: Path,
    protected_inventory_path: Path,
    protected_inventory_sha256: str,
) -> ProtectedLegacyCatalog:
    root = _trusted_root(repository_root)
    _exact_path(root, legacy_root, _LEGACY_ROOT)
    inventory_path = _exact_path(root, protected_inventory_path, _INVENTORY_PATH)
    expected_sha = _sha(
        protected_inventory_sha256,
        PromotionReasonCode.PROTECTED_INVENTORY_INVALID,
    )
    inventory_snapshot = _snapshot(root, inventory_path)
    if hashlib.sha256(inventory_snapshot.contents).hexdigest() != expected_sha:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    try:
        inventory = verify_protected_raw_inventory(root, inventory_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        ) from exc
    _rebind(root, inventory_path, inventory_snapshot)
    records = _catalog_records(root, inventory)
    try:
        membership = snapshot_protected_inventory_membership(root, inventory)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        ) from exc
    _rebind(root, inventory_path, inventory_snapshot)
    return ProtectedLegacyCatalog(
        repository_root=root,
        legacy_root=_LEGACY_ROOT,
        protected_inventory_path=_INVENTORY_PATH,
        protected_inventory_sha256=expected_sha,
        protected_inventory_identity=inventory_snapshot.identity,
        candidates=tuple(record.candidate for record in records),
        records=records,
        membership=membership,
    )


def _exact_match(
    query: LogicalQuery,
    record: ProtectedRawRecord,
    legacy_evidence: LegacyEvidenceCapability,
) -> bool:
    legacy = json.loads(record.canonical_query_json)
    return matches_current_query_to_legacy(
        query,
        legacy_source=record.source,
        legacy_query=legacy,
        protected_inventory_sha256=legacy_evidence.inventory_sha256,
    )


def _decision(
    query: LogicalQuery,
    disposition: PromotionDisposition,
    reason: PromotionReasonCode | None = None,
    candidate: ProtectedRawRecord | None = None,
) -> PromotionDecision:
    return PromotionDecision(
        query_id=logical_query_id(query),
        disposition=disposition,
        reason_codes=() if reason is None else (reason,),
        candidate=candidate,
    )


def _current_query_fields_valid(query: LogicalQuery) -> bool:
    parameters = query.parameters
    requested = query.requested_range
    if not isinstance(parameters, Mapping) or not isinstance(requested, Mapping):
        return False
    if query.source == "mlb_schedule_game_feed":
        return (
            type(requested.get("year")) is int
            and isinstance(requested.get("start_date"), str)
            and isinstance(requested.get("end_date"), str)
            and type(parameters.get("sportId")) is int
            and isinstance(parameters.get("gameType"), str)
            and isinstance(parameters.get("startDate"), str)
            and isinstance(parameters.get("endDate"), str)
            and isinstance(parameters.get("hydrate"), str)
        )
    if query.source == "mlb_stats_pitching":
        response = requested.get("response_completeness")
        return (
            type(requested.get("season")) is int
            and isinstance(requested.get("start_date"), str)
            and isinstance(requested.get("as_of_date"), str)
            and isinstance(requested.get("consuming_game_pks"), list)
            and isinstance(requested.get("consumer_cutoff_dates"), Mapping)
            and isinstance(response, Mapping)
            and type(parameters.get("sportIds")) is int
            and type(parameters.get("limit")) is int
            and all(
                isinstance(parameters.get(key), str)
                for key in (
                    "stats",
                    "group",
                    "gameType",
                    "playerPool",
                    "startDate",
                    "endDate",
                )
            )
        )
    if query.source == "mlb_stats_pitcher_bf":
        return (
            type(requested.get("pitcher_id")) is int
            and isinstance(requested.get("inclusive_end"), str)
            and isinstance(requested.get("consuming_game_pks"), list)
            and isinstance(requested.get("consumer_cutoff_dates"), Mapping)
            and type(parameters.get("sportIds")) is int
            and all(
                isinstance(parameters.get(key), str)
                for key in (
                    "stats",
                    "group",
                    "gameType",
                    "startDate",
                    "endDate",
                )
            )
        )
    if query.source == "baseball_savant_statcast":
        try:
            from .v52_query_plan import validate_statcast_query

            validate_statcast_query(query)
        except (TypeError, ValueError):
            return False
    return True


def _audit_revalidated(
    query: LogicalQuery,
    legacy_evidence: LegacyEvidenceCapability,
) -> tuple[PromotionDecision, _VerifiedRecord | None, Mapping[str, object] | None]:
    try:
        query_id = logical_query_id(query)
    except (TypeError, ValueError):
        return (
            PromotionDecision(
                query_id="0" * 64,
                disposition=PromotionDisposition.FETCH_REQUIRED,
                reason_codes=(PromotionReasonCode.CURRENT_QUERY_INVALID,),
                candidate=None,
            ),
            None,
            None,
        )
    if query.parser_version != "v5.2.1":
        return (
            PromotionDecision(
                query_id=query_id,
                disposition=PromotionDisposition.FETCH_REQUIRED,
                reason_codes=(
                    PromotionReasonCode.CURRENT_PARSER_CONTRACT_UNSATISFIED,
                ),
                candidate=None,
            ),
            None,
            None,
        )
    if not _current_query_fields_valid(query):
        return (
            PromotionDecision(
                query_id=query_id,
                disposition=PromotionDisposition.FETCH_REQUIRED,
                reason_codes=(PromotionReasonCode.CURRENT_QUERY_INVALID,),
                candidate=None,
            ),
            None,
            None,
        )
    try:
        matches = [
            record
            for record in legacy_evidence.records
            if _exact_match(query, record, legacy_evidence)
        ]
    except (KeyError, TypeError, ValueError, OverflowError):
        return (
            PromotionDecision(
                query_id=query_id,
                disposition=PromotionDisposition.FETCH_REQUIRED,
                reason_codes=(PromotionReasonCode.CURRENT_QUERY_INVALID,),
                candidate=None,
            ),
            None,
            None,
        )
    if len(matches) > 1:
        return (
            _decision(
                query,
                PromotionDisposition.FATAL_INTEGRITY,
                PromotionReasonCode.CATALOG_NOT_UNIQUE,
            ),
            None,
            None,
        )
    if not matches:
        return (
            _decision(
                query,
                PromotionDisposition.FETCH_REQUIRED,
                PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,
            ),
            None,
            None,
        )
    protected_record = matches[0]
    try:
        record = _verify_selected_revalidated(
            legacy_evidence,
            protected_record,
        )
    except ProtectedLegacyIntegrityError as exc:
        return (
            _decision(query, PromotionDisposition.FATAL_INTEGRITY, exc.reason_code),
            None,
            None,
        )
    try:
        semantics = recompute_query_sidecar(query, record.payload)
        if (
            not isinstance(semantics, Mapping)
            or not all(isinstance(key, str) for key in semantics)
            or semantics.get("split_disposition") == "SPLIT_REQUIRED"
        ):
            raise ValueError("current semantic contract differs")
        canonical_json_bytes(dict(semantics))
    except (TypeError, ValueError):
        return (
            _decision(
                query,
                PromotionDisposition.FETCH_REQUIRED,
                PromotionReasonCode.CURRENT_SEMANTIC_CONTRACT_UNSATISFIED,
            ),
            None,
            None,
        )
    return (
        _decision(query, PromotionDisposition.PROMOTED, candidate=record.candidate),
        record,
        dict(semantics),
    )


def _fatal_audit(
    reason: PromotionReasonCode,
) -> tuple[PromotionDecision, None, None]:
    return (
        PromotionDecision(
            query_id="0" * 64,
            disposition=PromotionDisposition.FATAL_INTEGRITY,
            reason_codes=(reason,),
            candidate=None,
        ),
        None,
        None,
    )


def _audit_with_record(
    query: LogicalQuery,
    legacy_evidence: LegacyEvidenceCapability,
) -> tuple[PromotionDecision, _VerifiedRecord | None, Mapping[str, object] | None]:
    try:
        capability = _revalidate_catalog(legacy_evidence)
    except ProtectedLegacyIntegrityError as exc:
        return _fatal_audit(exc.reason_code)
    return _audit_revalidated(query, capability)


def audit_legacy_candidate(
    query: LogicalQuery,
    legacy_evidence: LegacyEvidenceCapability,
) -> PromotionDecision:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    capability = _require_issued_legacy_evidence(legacy_evidence)
    return _audit_with_record(query, capability)[0]


def _decision_evidence(
    audited: tuple[
        PromotionDecision,
        _VerifiedRecord | None,
        Mapping[str, object] | None,
    ],
    legacy_evidence: LegacyEvidenceCapability,
) -> LegacyDecisionEvidence:
    decision, record, semantics = audited
    if (
        decision.disposition is not PromotionDisposition.PROMOTED
        or record is None
        or semantics is None
    ):
        return LegacyDecisionEvidence(decision, None, None)

    inventory_record = record.inventory_record
    manifest = inventory_record.get("manifest")
    seal = inventory_record.get("seal")
    raw = inventory_record.get("raw")
    if not all(isinstance(item, Mapping) for item in (manifest, seal, raw)):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    assert isinstance(manifest, Mapping)
    assert isinstance(seal, Mapping)
    assert isinstance(raw, Mapping)
    lineage = MappingProxyType(
        {
            "source": record.candidate.source,
            "manifest": MappingProxyType(
                {"path": manifest["path"], "sha256": manifest["sha256"]}
            ),
            "seal": MappingProxyType({"path": seal["path"], "sha256": seal["sha256"]}),
            "raw": MappingProxyType(
                {
                    "path": raw["path"],
                    "sha256": raw["sha256"],
                    "byte_size": raw["byte_size"],
                    "row_count": record.manifest["row_count"],
                }
            ),
            "protected_inventory": MappingProxyType(
                {
                    "path": _INVENTORY_PATH.as_posix(),
                    "sha256": legacy_evidence.inventory_sha256,
                }
            ),
        }
    )
    semantic_sha256 = hashlib.sha256(canonical_json_bytes(semantics)).hexdigest()
    return LegacyDecisionEvidence(decision, lineage, semantic_sha256)


def audit_legacy_candidate_evidence(
    query: LogicalQuery,
    legacy_evidence: LegacyEvidenceCapability,
) -> LegacyDecisionEvidence:
    """Audit one query and return its immutable legacy decision evidence."""
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    capability = _require_issued_legacy_evidence(legacy_evidence)
    return _decision_evidence(_audit_with_record(query, capability), capability)


def audit_legacy_candidates_evidence(
    queries: Sequence[LogicalQuery],
    legacy_evidence: LegacyEvidenceCapability,
) -> tuple[LegacyDecisionEvidence, ...]:
    """Audit an immutable query set under one start/end catalog authentication."""
    if not isinstance(queries, Sequence) or not all(
        isinstance(query, LogicalQuery) for query in queries
    ):
        raise TypeError("queries must contain LogicalQuery values")
    if not queries:
        raise ValueError("queries must be nonempty")
    capability = _require_issued_legacy_evidence(legacy_evidence)
    try:
        capability = _revalidate_catalog(capability)
    except ProtectedLegacyIntegrityError as exc:
        fatal = _decision_evidence(_fatal_audit(exc.reason_code), capability)
        return tuple(fatal for _query in queries)
    audited = tuple(
        _audit_revalidated(query, capability)
        for query in queries
    )
    try:
        _revalidate_catalog(capability)
    except ProtectedLegacyIntegrityError as exc:
        fatal = _decision_evidence(_fatal_audit(exc.reason_code), capability)
        return tuple(fatal for _query in queries)
    return tuple(_decision_evidence(item, capability) for item in audited)


def promote_legacy_candidate(
    store: V52EvidenceStore,
    query: LogicalQuery,
    legacy_evidence: LegacyEvidenceCapability,
    decision: PromotionDecision,
) -> PublishedEvidence:
    if not isinstance(store, V52EvidenceStore):
        raise TypeError("store must be a V52EvidenceStore")
    if (
        not isinstance(decision, PromotionDecision)
        or decision.query_id != logical_query_id(query)
        or decision.disposition is not PromotionDisposition.PROMOTED
        or decision.candidate is None
        or decision.reason_codes
    ):
        raise ValueError("promotion decision is not an exact PROMOTED decision")
    capability = _require_issued_legacy_evidence(legacy_evidence)
    current, record, semantics = _audit_with_record(query, capability)
    if current.disposition is PromotionDisposition.FATAL_INTEGRITY:
        raise ProtectedLegacyIntegrityError(current.reason_codes[0])
    if current.disposition is PromotionDisposition.FETCH_REQUIRED:
        raise LegacyPromotionNotEligibleError(current.reason_codes[0])
    if (
        current.candidate != decision.candidate
        or record is None
        or semantics is None
    ):
        raise LegacyPromotionNotEligibleError(
            PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE
        )
    manifest_item = record.inventory_record["manifest"]
    seal_item = record.inventory_record["seal"]
    raw_item = record.inventory_record["raw"]
    if not all(
        isinstance(item, Mapping) for item in (manifest_item, seal_item, raw_item)
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    reference = {
        "canonical_query": dict(record.canonical_query),
        "current_query_id": logical_query_id(query),
        "legacy_manifest_path": record.candidate.manifest_path.as_posix(),
        "legacy_manifest_sha256": record.candidate.manifest_sha256,
        "legacy_seal_path": record.candidate.seal_path.as_posix(),
        "legacy_seal_sha256": seal_item["sha256"],
        "legacy_source": record.candidate.source,
        "parser_version": query.parser_version,
        "payload_path": record.candidate.payload_path.as_posix(),
        "protected_inventory_path": _INVENTORY_PATH.as_posix(),
        "protected_inventory_sha256": capability.inventory_sha256,
        "raw_byte_count": raw_item["byte_size"],
        "raw_row_count": record.manifest["row_count"],
        "raw_sha256": record.candidate.payload_sha256,
        "semantic_sha256": hashlib.sha256(canonical_json_bytes(semantics)).hexdigest(),
        "source": query.source,
    }
    try:
        with store.acquire_query(query):
            try:
                capability.rebind()
            except (OSError, RuntimeError, ValueError) as exc:
                if isinstance(exc, ProtectedLegacyIntegrityError):
                    raise
                raise _capability_integrity_error(exc) from exc
            return store.publish_legacy_reference(query, reference, semantics)
    except ProtectedLegacyPublishReverifyError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PUBLISH_REVERIFY_FAILED
        ) from exc


__all__ = [
    "LegacyEvidenceCapability",
    "LegacyCandidate",
    "LegacyDecisionEvidence",
    "LegacyPromotionNotEligibleError",
    "LogicalProtectedRawRecord",
    "PromotionDecision",
    "PromotionDisposition",
    "PromotionReasonCode",
    "ProtectedLegacyCatalog",
    "ProtectedLegacyIntegrityError",
    "ProtectedRawRecord",
    "RootLocalLegacyEvidenceAdapter",
    "adapt_root_local_legacy_catalog",
    "audit_legacy_candidate",
    "audit_legacy_candidate_evidence",
    "audit_legacy_candidates_evidence",
    "load_protected_legacy_catalog",
    "promote_legacy_candidate",
    "trusted_legacy_repository_root",
]
