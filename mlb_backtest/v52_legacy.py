"""Inventory-bound promotion of protected v5.1 raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

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
    candidate: LegacyCandidate | None


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
class _VerifiedRecord:
    candidate: LegacyCandidate
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


def _catalog_inventory_record(record: _CatalogRecord) -> dict[str, object]:
    def item(binding: _ProtectedObjectBinding) -> dict[str, object]:
        return {
            "byte_size": binding.byte_size,
            "inode_fingerprint": {
                "device": binding.device,
                "inode": binding.inode,
            },
            "path": binding.relative_path,
            "sha256": binding.sha256,
        }

    return {
        "canonical_query": json.loads(record.canonical_query_json),
        "manifest": item(record.manifest),
        "raw": item(record.raw),
        "rawstore_verified": True,
        "seal": item(record.seal),
        "source": record.candidate.source,
    }


def _revalidate_catalog(catalog: ProtectedLegacyCatalog) -> tuple[Path, _Snapshot]:
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


def _assert_object_binding(
    binding: _ProtectedObjectBinding,
    snapshot: _Snapshot,
    reason: PromotionReasonCode,
) -> None:
    if (
        snapshot.identity[0] != binding.device
        or snapshot.identity[1] != binding.inode
        or len(snapshot.contents) != binding.byte_size
        or hashlib.sha256(snapshot.contents).hexdigest() != binding.sha256
    ):
        raise ProtectedLegacyIntegrityError(reason)


def _verify_selected(
    catalog: ProtectedLegacyCatalog, record: _CatalogRecord
) -> _VerifiedRecord:
    root, inventory_snapshot = _revalidate_catalog(catalog)
    manifest_snapshot = _snapshot(root, record.candidate.manifest_path)
    seal_snapshot = _snapshot(root, record.candidate.seal_path)
    payload_snapshot = _snapshot(root, record.candidate.payload_path)
    _assert_object_binding(
        record.manifest, manifest_snapshot, PromotionReasonCode.LEGACY_MANIFEST_INVALID
    )
    _assert_object_binding(
        record.seal, seal_snapshot, PromotionReasonCode.LEGACY_SEAL_INVALID
    )
    _assert_object_binding(
        record.raw, payload_snapshot, PromotionReasonCode.LEGACY_RAW_BYTES_INVALID
    )
    if seal_snapshot.contents != f"{record.manifest.sha256}\n".encode("ascii"):
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.LEGACY_SEAL_INVALID)
    try:
        manifest = json.loads(manifest_snapshot.contents)
        canonical_query = json.loads(record.canonical_query_json)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_MANIFEST_INVALID
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or not isinstance(canonical_query, Mapping)
        or manifest.get("source") != record.candidate.source
        or manifest.get("query") != canonical_query
        or manifest.get("sha256") != record.raw.sha256
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_MANIFEST_INVALID
        )
    expected_name = (
        f"{record.raw.sha256}."
        f"{hashlib.sha256(canonical_json_bytes(canonical_query)).hexdigest()}"
        ".manifest.json"
    )
    if record.candidate.manifest_path.name != expected_name:
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.LEGACY_FILENAME_INVALID)
    row_count = sum(bool(line.strip()) for line in payload_snapshot.contents.splitlines())
    if manifest.get("row_count") != row_count:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_ROW_COUNT_INVALID
        )
    _rebind(root, root / catalog.protected_inventory_path, inventory_snapshot)
    for path, snapshot in (
        (record.candidate.manifest_path, manifest_snapshot),
        (record.candidate.seal_path, seal_snapshot),
        (record.candidate.payload_path, payload_snapshot),
    ):
        _rebind(root, path, snapshot)
    try:
        current_membership = snapshot_protected_inventory_membership(
            root, parse_protected_raw_inventory_document(inventory_snapshot.contents)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        ) from exc
    if current_membership != catalog.membership:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )
    return _VerifiedRecord(
        candidate=record.candidate,
        canonical_query=dict(canonical_query),
        manifest=dict(manifest),
        inventory_record=_catalog_inventory_record(record),
        payload=payload_snapshot.contents,
    )


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
    record: _CatalogRecord,
    catalog: ProtectedLegacyCatalog,
) -> bool:
    legacy = json.loads(record.canonical_query_json)
    return matches_current_query_to_legacy(
        query,
        legacy_source=record.candidate.source,
        legacy_query=legacy,
        protected_inventory_sha256=catalog.protected_inventory_sha256,
    )


def _decision(
    query: LogicalQuery,
    disposition: PromotionDisposition,
    reason: PromotionReasonCode | None = None,
    candidate: LegacyCandidate | None = None,
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


def _audit_with_record(
    query: LogicalQuery, catalog: ProtectedLegacyCatalog
) -> tuple[PromotionDecision, _VerifiedRecord | None, Mapping[str, object] | None]:
    try:
        _revalidate_catalog(catalog)
    except ProtectedLegacyIntegrityError as exc:
        return (
            PromotionDecision(
                query_id="0" * 64,
                disposition=PromotionDisposition.FATAL_INTEGRITY,
                reason_codes=(exc.reason_code,),
                candidate=None,
            ),
            None,
            None,
        )
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
            record for record in catalog.records if _exact_match(query, record, catalog)
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
    catalog_record = matches[0]
    try:
        record = _verify_selected(catalog, catalog_record)
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


def audit_legacy_candidate(
    query: LogicalQuery, catalog: ProtectedLegacyCatalog
) -> PromotionDecision:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    if not isinstance(catalog, ProtectedLegacyCatalog):
        raise TypeError("catalog must be a ProtectedLegacyCatalog")
    return _audit_with_record(query, catalog)[0]


def audit_legacy_candidate_evidence(
    query: LogicalQuery, catalog: ProtectedLegacyCatalog
) -> LegacyDecisionEvidence:
    """Audit one query and return its immutable legacy decision evidence."""
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    if not isinstance(catalog, ProtectedLegacyCatalog):
        raise TypeError("catalog must be a ProtectedLegacyCatalog")
    decision, record, semantics = _audit_with_record(query, catalog)
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
                    "path": catalog.protected_inventory_path.as_posix(),
                    "sha256": catalog.protected_inventory_sha256,
                }
            ),
        }
    )
    semantic_sha256 = hashlib.sha256(canonical_json_bytes(semantics)).hexdigest()
    return LegacyDecisionEvidence(decision, lineage, semantic_sha256)


def promote_legacy_candidate(
    store: V52EvidenceStore,
    query: LogicalQuery,
    catalog: ProtectedLegacyCatalog,
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
    current, record, semantics = _audit_with_record(query, catalog)
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
        "legacy_manifest_path": record.candidate.manifest_path.relative_to(
            catalog.repository_root
        ).as_posix(),
        "legacy_manifest_sha256": record.candidate.manifest_sha256,
        "legacy_seal_path": record.candidate.seal_path.relative_to(
            catalog.repository_root
        ).as_posix(),
        "legacy_seal_sha256": seal_item["sha256"],
        "legacy_source": record.candidate.source,
        "parser_version": query.parser_version,
        "payload_path": record.candidate.payload_path.relative_to(
            catalog.repository_root
        ).as_posix(),
        "protected_inventory_path": catalog.protected_inventory_path.as_posix(),
        "protected_inventory_sha256": catalog.protected_inventory_sha256,
        "raw_byte_count": raw_item["byte_size"],
        "raw_row_count": record.manifest["row_count"],
        "raw_sha256": record.candidate.raw_sha256,
        "semantic_sha256": hashlib.sha256(canonical_json_bytes(semantics)).hexdigest(),
        "source": query.source,
    }
    try:
        with store.acquire_query(query):
            return store.publish_legacy_reference(query, reference, semantics)
    except ProtectedLegacyPublishReverifyError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PUBLISH_REVERIFY_FAILED
        ) from exc


__all__ = [
    "LegacyCandidate",
    "LegacyDecisionEvidence",
    "LegacyPromotionNotEligibleError",
    "PromotionDecision",
    "PromotionDisposition",
    "PromotionReasonCode",
    "ProtectedLegacyCatalog",
    "ProtectedLegacyIntegrityError",
    "audit_legacy_candidate",
    "audit_legacy_candidate_evidence",
    "load_protected_legacy_catalog",
    "promote_legacy_candidate",
]
