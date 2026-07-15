"""Closed decision inventory for protected v5.1 Statcast evidence."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import fcntl
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from jsonschema import Draft202012Validator, FormatChecker

from .v52_evidence import logical_query_id
from .v52_legacy import (
    PromotionDisposition,
    ProtectedLegacyCatalog,
    _Snapshot,
    _catalog_inventory_record,
    _rebind,
    _snapshot,
    _trusted_root,
    audit_legacy_candidate_evidence,
    load_protected_legacy_catalog,
)
from .v52_query_plan import (
    build_legacy_partition_batch,
    build_statcast_root_batch,
    load_query_batch,
)
from .v52_source_contracts import canonical_json_bytes


_RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_ACQUISITION_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/historical_source_acquisition.json"
)
_PROTECTED_INVENTORY_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
_LEGACY_ROOT_RELATIVE = Path("data/v5_1_bt/raw")
_QUERY_BATCH_ROOT = Path("audit/v5_2_qd/historical/acquisition/query_batches")
_SCHEMA_RELATIVE = Path("schemas/v5_2_legacy_decision_inventory.schema.json")
_LEGACY_DECISION_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/legacy_decision_inventory.json"
)
_PERMITTED_YEARS = (2022, 2023, 2024)
_RUN_ID = "historical_walkforward_500"
_EXPECTED_SCHEMA_SHA256 = (
    "39864790fa275262bbdb932efaa9f52138aa6d29b98881666b01b9fbb80a68e5"
)
_MAX_INVENTORY_BYTES = 8 * 1024 * 1024
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "run_id",
        "run_spec",
        "acquisition_contract",
        "protected_inventory",
        "statcast_root_batch",
        "permitted_legacy_years",
        "root_query_count",
        "counts",
        "decisions",
    }
)
_COUNT_KEYS = frozenset(
    {
        "protected_catalog_record_count",
        "in_scope_statcast_record_count",
        "out_of_scope_record_count",
        "unique_candidate_range_count",
        "promoted_count",
        "fetch_required_count",
        "fatal_integrity_count",
    }
)


@dataclass(frozen=True)
class VerifiedLegacyDecisionInventory:
    path: Path
    sha256: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class _Authority:
    root: Path
    payload: Mapping[str, object]
    identities: tuple[tuple[str, tuple[int, ...]], ...]


def _before_legacy_decision_inventory_authority_rebind() -> None:
    """Test seam immediately before authority identities are rebound."""


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _canonical_file(value: object) -> bytes:
    return canonical_json_bytes(_plain(value)) + b"\n"


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _deep_freeze(child) for key, child in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(child) for child in value)
    return value


def _validate_payload(
    payload: object, schema: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("legacy decision inventory top-level keys differ")
    try:
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    except Exception as exc:
        raise ValueError(
            "legacy decision inventory violates its closed schema"
        ) from exc

    counts = payload.get("counts")
    decisions = payload.get("decisions")
    if not isinstance(counts, dict) or set(counts) != _COUNT_KEYS:
        raise ValueError("legacy decision inventory count keys differ")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("legacy decision inventory decisions must be nonempty")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        raise ValueError(
            "legacy decision inventory counts must be nonnegative integers"
        )
    if counts["protected_catalog_record_count"] != (
        counts["in_scope_statcast_record_count"] + counts["out_of_scope_record_count"]
    ):
        raise ValueError("legacy catalog scope counts do not partition the catalog")
    if (
        counts["unique_candidate_range_count"]
        > counts["in_scope_statcast_record_count"]
    ):
        raise ValueError("unique legacy range count exceeds in-scope records")

    disposition_counts = {item.value: 0 for item in PromotionDisposition}
    identities: set[tuple[str, str]] = set()
    canonical_decisions: set[bytes] = set()
    ordering: list[tuple[object, object, object, object]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("legacy decision must be an object")
        disposition = decision.get("disposition")
        if not isinstance(disposition, str) or disposition not in disposition_counts:
            raise ValueError("legacy decision disposition differs")
        disposition_counts[disposition] += 1
        candidate_query_id = decision.get("candidate_query_id")
        parent_query_id = decision.get("parent_query_id")
        if not isinstance(candidate_query_id, str) or not isinstance(
            parent_query_id, str
        ):
            raise ValueError("legacy decision query IDs differ")
        identity = (parent_query_id, candidate_query_id)
        if identity in identities:
            raise ValueError("legacy decision query binding is duplicated")
        identities.add(identity)
        serialized = canonical_json_bytes(decision)
        if serialized in canonical_decisions:
            raise ValueError("legacy decision is duplicated")
        canonical_decisions.add(serialized)
        decision_range = decision.get("range")
        if not isinstance(decision_range, dict):
            raise ValueError("legacy decision range differs")
        ordering.append(
            (
                decision_range.get("year"),
                decision_range.get("start_date"),
                decision_range.get("end_date"),
                candidate_query_id,
            )
        )
    if ordering != sorted(ordering):
        raise ValueError("legacy decisions are not canonically ordered")
    if len(decisions) != sum(disposition_counts.values()):
        raise ValueError(
            "legacy decision disposition counts do not partition decisions"
        )
    expected_disposition_counts = {
        "PROMOTED": counts["promoted_count"],
        "FETCH_REQUIRED": counts["fetch_required_count"],
        "FATAL_INTEGRITY": counts["fatal_integrity_count"],
    }
    if disposition_counts != expected_disposition_counts:
        raise ValueError("legacy decision disposition counts differ")
    return payload


def _json_snapshot(root: Path, relative: Path) -> tuple[dict[str, object], _Snapshot]:
    snapshot = _snapshot(root, root / relative)
    try:
        value = json.loads(snapshot.contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{relative} is not valid JSON") from exc
    if not isinstance(value, dict) or _canonical_file(value) != snapshot.contents:
        raise ValueError(f"{relative} is not canonical JSON")
    return value, snapshot


def _in_scope_records(catalog: ProtectedLegacyCatalog) -> list[Mapping[str, object]]:
    records: list[Mapping[str, object]] = []
    for record in catalog.records:
        if record.candidate.source != "baseball_savant_statcast":
            continue
        try:
            query = json.loads(record.canonical_query_json)
            start = query["start"]
            end = query["end"]
            start_year = int(start[:4])
            end_year = int(end[:4])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("legacy Statcast canonical query is invalid") from exc
        if start_year in _PERMITTED_YEARS and end_year == start_year:
            records.append(_catalog_inventory_record(record))
    return records


def _authority_identities(
    root: Path,
    catalog: ProtectedLegacyCatalog,
    fixed: tuple[tuple[Path, _Snapshot], ...],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    result = [(path.as_posix(), snapshot.identity) for path, snapshot in fixed]
    for record in catalog.records:
        for path in (
            record.candidate.manifest_path,
            record.candidate.seal_path,
            record.candidate.payload_path,
        ):
            relative = path.relative_to(root)
            result.append((relative.as_posix(), _snapshot(root, path).identity))
    return tuple(sorted(result))


def _assemble(repository_root: Path) -> _Authority:
    root = _trusted_root(repository_root)
    run_spec, run_snapshot = _json_snapshot(root, _RUN_SPEC_RELATIVE)
    contract, contract_snapshot = _json_snapshot(root, _ACQUISITION_CONTRACT_RELATIVE)
    schema, schema_snapshot = _json_snapshot(root, _SCHEMA_RELATIVE)
    if hashlib.sha256(schema_snapshot.contents).hexdigest() != _EXPECTED_SCHEMA_SHA256:
        raise ValueError("legacy decision inventory schema authority differs")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("legacy decision inventory schema is invalid") from exc
    if run_spec.get("run_id") != _RUN_ID or contract.get("run_id") != _RUN_ID:
        raise ValueError("run authority differs")
    legacy = contract.get("legacy_protection")
    if not isinstance(legacy, Mapping):
        raise ValueError("legacy protection contract is missing")
    if (
        legacy.get("legacy_root") != _LEGACY_ROOT_RELATIVE.as_posix()
        or legacy.get("protected_inventory_path")
        != _PROTECTED_INVENTORY_RELATIVE.as_posix()
        or legacy.get("permitted_years") != list(_PERMITTED_YEARS)
    ):
        raise ValueError("legacy protection contract differs")
    protected_sha256 = legacy.get("protected_inventory_sha256")
    if not isinstance(protected_sha256, str):
        raise ValueError("protected inventory hash is missing")

    protected_snapshot = _snapshot(root, root / _PROTECTED_INVENTORY_RELATIVE)
    if hashlib.sha256(protected_snapshot.contents).hexdigest() != protected_sha256:
        raise ValueError("protected inventory hash differs")
    catalog = load_protected_legacy_catalog(
        root,
        legacy_root=root / _LEGACY_ROOT_RELATIVE,
        protected_inventory_path=root / _PROTECTED_INVENTORY_RELATIVE,
        protected_inventory_sha256=protected_sha256,
    )
    root_expected = build_statcast_root_batch(root)
    root_batch = load_query_batch(root, root_expected.batch_id)
    if root_batch != root_expected or len(root_batch.queries) != 46:
        raise ValueError("Statcast root batch authority differs")
    root_batch_relative = _QUERY_BATCH_ROOT / f"{root_batch.batch_id}.json"
    root_batch_snapshot = _snapshot(root, root / root_batch_relative)

    in_scope = _in_scope_records(catalog)
    partition = build_legacy_partition_batch(
        root_batch.queries,
        in_scope,
        protected_inventory_sha256=protected_sha256,
    )
    decisions: list[dict[str, object]] = []
    disposition_counts = {item.value: 0 for item in PromotionDisposition}
    for query in partition.queries:
        evidence = audit_legacy_candidate_evidence(query, catalog)
        if evidence.decision.query_id != logical_query_id(query):
            raise RuntimeError("legacy decision query binding differs")
        requested = query.requested_range
        record = {
            "parent_query_id": query.parent_query_id,
            "candidate_query_id": evidence.decision.query_id,
            "range": {
                "year": requested["year"],
                "start_date": requested["start_date"],
                "end_date": requested["end_date"],
            },
            "disposition": evidence.decision.disposition.value,
            "reason_codes": [item.value for item in evidence.decision.reason_codes],
            "candidate_lineage": _plain(evidence.candidate_lineage),
            "semantic_sha256": evidence.semantic_sha256,
        }
        decisions.append(record)
        disposition_counts[evidence.decision.disposition.value] += 1
    decisions.sort(
        key=lambda item: (
            item["range"]["year"],
            item["range"]["start_date"],
            item["range"]["end_date"],
            item["candidate_query_id"],
        )
    )
    unique_ranges = {
        (item["canonical_query"]["start"], item["canonical_query"]["end"])
        for item in in_scope
    }
    payload = {
        "schema_version": 1,
        "receipt_kind": "LEGACY_DECISION_INVENTORY",
        "run_id": _RUN_ID,
        "run_spec": {
            "path": _RUN_SPEC_RELATIVE.as_posix(),
            "sha256": hashlib.sha256(run_snapshot.contents).hexdigest(),
        },
        "acquisition_contract": {
            "path": _ACQUISITION_CONTRACT_RELATIVE.as_posix(),
            "sha256": hashlib.sha256(contract_snapshot.contents).hexdigest(),
        },
        "protected_inventory": {
            "path": _PROTECTED_INVENTORY_RELATIVE.as_posix(),
            "sha256": protected_sha256,
        },
        "statcast_root_batch": {
            "batch_id": root_batch.batch_id,
            "path": root_batch_relative.as_posix(),
            "sha256": hashlib.sha256(root_batch_snapshot.contents).hexdigest(),
        },
        "permitted_legacy_years": list(_PERMITTED_YEARS),
        "root_query_count": len(root_batch.queries),
        "counts": {
            "protected_catalog_record_count": len(catalog.records),
            "in_scope_statcast_record_count": len(in_scope),
            "out_of_scope_record_count": len(catalog.records) - len(in_scope),
            "unique_candidate_range_count": len(unique_ranges),
            "promoted_count": disposition_counts["PROMOTED"],
            "fetch_required_count": disposition_counts["FETCH_REQUIRED"],
            "fatal_integrity_count": disposition_counts["FATAL_INTEGRITY"],
        },
        "decisions": decisions,
    }
    identities = _authority_identities(
        root,
        catalog,
        (
            (_RUN_SPEC_RELATIVE, run_snapshot),
            (_ACQUISITION_CONTRACT_RELATIVE, contract_snapshot),
            (_SCHEMA_RELATIVE, schema_snapshot),
            (_PROTECTED_INVENTORY_RELATIVE, protected_snapshot),
            (root_batch_relative, root_batch_snapshot),
        ),
    )
    return _Authority(root, _validate_payload(payload, schema), identities)


def build_legacy_decision_inventory(
    repository_root: Path,
) -> Mapping[str, object]:
    authority = _assemble(repository_root)
    return _validate_authority_payload(authority)


def _rebind_authority(initial: _Authority) -> _Authority:
    rebound = _assemble(initial.root)
    if rebound.payload != initial.payload or rebound.identities != initial.identities:
        raise RuntimeError("legacy decision authority changed during verification")
    return rebound


def _expected_schema(root: Path) -> Mapping[str, object]:
    schema, snapshot = _json_snapshot(root, _SCHEMA_RELATIVE)
    if hashlib.sha256(snapshot.contents).hexdigest() != _EXPECTED_SCHEMA_SHA256:
        raise ValueError("legacy decision inventory schema authority differs")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("legacy decision inventory schema is invalid") from exc
    return schema


def _validate_authority_payload(authority: _Authority) -> dict[str, object]:
    plain = _plain(authority.payload)
    if not isinstance(plain, dict):
        raise ValueError("legacy decision authority payload must be an object")
    return _validate_payload(plain, _expected_schema(authority.root))


def _verified(
    authority: _Authority, path: Path, contents: bytes
) -> VerifiedLegacyDecisionInventory:
    return VerifiedLegacyDecisionInventory(
        path=path,
        sha256=hashlib.sha256(contents).hexdigest(),
        payload=_deep_freeze(authority.payload),
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _open_inventory_parent(root: Path, *, create: bool) -> tuple[list[int], int]:
    descriptors = [os.open(root, _directory_flags())]
    try:
        current = descriptors[0]
        for component in _LEGACY_DECISION_INVENTORY_RELATIVE.parent.parts:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(component, _directory_flags(), dir_fd=current)
            opened = os.fstat(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                os.close(child)
                raise ValueError("legacy decision inventory ancestor identity differs")
            descriptors.append(child)
            current = child
        return descriptors, current
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _read_final_at(
    parent_fd: int,
) -> tuple[bytes, tuple[int, int, int, int]] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(
            _LEGACY_DECISION_INVENTORY_RELATIVE.name,
            flags,
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(
            "legacy decision inventory must be a no-follow regular file"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(
                "legacy decision inventory must be an unaliased regular file"
            )
        if metadata.st_size > _MAX_INVENTORY_BYTES:
            raise ValueError("legacy decision inventory exceeds its read limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_INVENTORY_BYTES:
                raise ValueError("legacy decision inventory exceeds its read limit")
            chunks.append(chunk)
        linked = os.stat(
            _LEGACY_DECISION_INVENTORY_RELATIVE.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_nlink,
        )
        if identity != (
            linked.st_dev,
            linked.st_ino,
            linked.st_size,
            linked.st_nlink,
        ):
            raise ValueError("legacy decision inventory identity changed during read")
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _remove_owned_final(
    root: Path, contents: bytes, identity: tuple[int, int, int, int]
) -> None:
    descriptors, parent_fd = _open_inventory_parent(root, create=False)
    locked = False
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        locked = True
        current = _read_final_at(parent_fd)
        if current is None or current != (contents, identity):
            return
        os.unlink(_LEGACY_DECISION_INVENTORY_RELATIVE.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if locked:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish_legacy_decision_inventory(
    repository_root: Path,
) -> VerifiedLegacyDecisionInventory:
    assembled = _assemble(repository_root)
    initial = _Authority(
        assembled.root,
        _validate_authority_payload(assembled),
        assembled.identities,
    )
    counts = initial.payload["counts"]
    if not isinstance(counts, Mapping) or counts["fatal_integrity_count"]:
        raise RuntimeError("fatal legacy integrity decisions cannot be published")
    contents = _canonical_file(initial.payload)
    descriptors, parent_fd = _open_inventory_parent(initial.root, create=True)
    temp_name: str | None = None
    temp_fd = -1
    locked = False
    created_identity: tuple[int, int, int, int] | None = None
    expected_identity: tuple[int, int, int, int] | None = None
    authority = initial
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        locked = True
        existing = _read_final_at(parent_fd)
        if existing is not None and existing[0] != contents:
            raise RuntimeError("immutable legacy decision inventory differs")
        if existing is None:
            temp_name = (
                f".{_LEGACY_DECISION_INVENTORY_RELATIVE.name}."
                f"{secrets.token_hex(16)}.tmp"
            )
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
            view = memoryview(contents)
            written = 0
            while written < len(view):
                count = os.write(temp_fd, view[written:])
                if count <= 0:
                    raise OSError(
                        "legacy decision inventory temp write made no progress"
                    )
                written += count
            os.fsync(temp_fd)
            metadata = os.fstat(temp_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size != len(contents)
            ):
                raise ValueError("legacy decision inventory temp identity differs")

        if existing is None:
            try:
                assert temp_name is not None
                os.link(
                    temp_name,
                    _LEGACY_DECISION_INVENTORY_RELATIVE.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raced = _read_final_at(parent_fd)
                if raced is None or raced[0] != contents:
                    raise RuntimeError(
                        "concurrent immutable legacy decision inventory differs"
                    ) from None
            else:
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = None
                os.fsync(parent_fd)
        final_record = _read_final_at(parent_fd)
        if final_record is None or final_record[0] != contents:
            raise ValueError("legacy decision inventory publication differs")
        expected_identity = final_record[1]
        if existing is None and temp_name is None:
            created_identity = final_record[1]
        try:
            _before_legacy_decision_inventory_authority_rebind()
            rebound = _rebind_authority(initial)
            rebound_payload = _validate_authority_payload(rebound)
            if rebound_payload != initial.payload:
                raise ValueError("legacy decision authority payload changed")
            authority = _Authority(rebound.root, rebound_payload, rebound.identities)
        except BaseException:
            if created_identity is not None:
                owned = _read_final_at(parent_fd)
                if owned == (contents, created_identity):
                    os.unlink(
                        _LEGACY_DECISION_INVENTORY_RELATIVE.name,
                        dir_fd=parent_fd,
                    )
                    os.fsync(parent_fd)
            raise
    finally:
        if locked:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        if temp_fd >= 0:
            os.close(temp_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)

    final = authority.root / _LEGACY_DECISION_INVENTORY_RELATIVE
    try:
        post = _rebind_authority(authority)
        post_payload = _validate_authority_payload(post)
        if post_payload != authority.payload:
            raise ValueError("legacy decision authority changed after publication")
        final_snapshot = _snapshot(authority.root, final)
        if final_snapshot.contents != contents or expected_identity != (
            final_snapshot.identity[0],
            final_snapshot.identity[1],
            final_snapshot.identity[4],
            final_snapshot.identity[3],
        ):
            raise ValueError("legacy decision inventory final identity differs")
        _rebind(authority.root, final, final_snapshot)
        verified_authority = _Authority(post.root, post_payload, post.identities)
        return _verified(verified_authority, final, contents)
    except BaseException:
        if created_identity is not None:
            _remove_owned_final(authority.root, contents, created_identity)
        raise


def load_verified_legacy_decision_inventory(
    repository_root: Path,
) -> VerifiedLegacyDecisionInventory:
    assembled = _assemble(repository_root)
    initial = _Authority(
        assembled.root,
        _validate_authority_payload(assembled),
        assembled.identities,
    )
    final = initial.root / _LEGACY_DECISION_INVENTORY_RELATIVE
    try:
        descriptors, parent_fd = _open_inventory_parent(initial.root, create=False)
    except FileNotFoundError as exc:
        raise ValueError("legacy decision inventory is missing") from exc
    try:
        stored_record = _read_final_at(parent_fd)
        if stored_record is None:
            raise ValueError("legacy decision inventory is missing")
        contents, identity = stored_record
        try:
            stored = json.loads(contents)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("legacy decision inventory is not valid JSON") from exc
        if contents != _canonical_file(stored):
            raise ValueError("legacy decision inventory is not canonical JSON")
        validated_stored = _validate_payload(stored, _expected_schema(initial.root))
        if validated_stored != initial.payload:
            raise ValueError("legacy decision inventory is not exact current replay")
        _before_legacy_decision_inventory_authority_rebind()
        rebound = _rebind_authority(initial)
        rebound_payload = _validate_authority_payload(rebound)
        if rebound_payload != initial.payload:
            raise ValueError("legacy decision authority changed during load")
        final_record = _read_final_at(parent_fd)
        if final_record != (contents, identity):
            raise ValueError("legacy decision inventory changed during load")
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    snapshot = _snapshot(initial.root, final)
    if snapshot.contents != contents or identity != (
        snapshot.identity[0],
        snapshot.identity[1],
        snapshot.identity[4],
        snapshot.identity[3],
    ):
        raise ValueError("legacy decision inventory final identity differs")
    _rebind(initial.root, final, snapshot)
    authority = _Authority(rebound.root, rebound_payload, rebound.identities)
    return _verified(authority, final, contents)


__all__ = [
    "VerifiedLegacyDecisionInventory",
    "build_legacy_decision_inventory",
    "load_verified_legacy_decision_inventory",
    "publish_legacy_decision_inventory",
]
