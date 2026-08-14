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

from .protocols import SourceRunSpec
from .v52_evidence import LogicalQuery, logical_query_id
from .v52_legacy import (
    LegacyEvidenceCapability,
    LegacyDecisionEvidence,
    PromotionDisposition,
    _Snapshot,
    _protected_inventory_record,
    _rebind,
    _require_issued_legacy_evidence,
    _snapshot,
    _trusted_root,
    adapt_root_local_legacy_catalog,
    audit_legacy_candidates_evidence,
    load_protected_legacy_catalog,
)
from .v52_query_plan import (
    _load_query_batch,
    _load_static_contracts,
    QueryBatch,
    _R3_RECOVERY_RUN_ID,
    SourceAuthorityContext,
    build_legacy_partition_batch_from_catalog,
    build_statcast_root_batch,
    build_statcast_root_batch_for_authority,
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
    "584c16e7df2b582e296d350f5595490c0522519f0c006bfe3b05abb8e69a919c"
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
class LegacyDecisionPreview:
    partition_batch: QueryBatch
    decisions: tuple[Mapping[str, object], ...]
    counts: Mapping[str, object]


@dataclass(frozen=True)
class _Authority:
    root: Path
    payload: Mapping[str, object]
    identities: tuple[tuple[str, tuple[int, ...]], ...]
    context: SourceAuthorityContext
    legacy_evidence: LegacyEvidenceCapability


def _authority_context(
    root: Path,
    run_spec_path: Path | None,
    acquisition_contract_path: Path | None,
) -> SourceAuthorityContext:
    """Authenticate the exact authority pair before touching replay artifacts."""
    supplied_run = Path(run_spec_path) if run_spec_path is not None else root / _RUN_SPEC_RELATIVE
    supplied_contract = (
        Path(acquisition_contract_path)
        if acquisition_contract_path is not None
        else root / _ACQUISITION_CONTRACT_RELATIVE
    )
    for supplied, label in ((supplied_run, "RunSpec"), (supplied_contract, "acquisition contract")):
        text = os.fspath(supplied)
        if (
            not supplied.is_absolute()
            or "\\" in text
            or any(part in {"", ".", ".."} for part in supplied.parts)
            or Path(os.path.abspath(text)) != supplied
        ):
            raise ValueError(f"{label} path differs from the repository contract")
        try:
            supplied.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} path must stay under the repository root") from exc
    context = _load_static_contracts(supplied_run, supplied_contract)
    if context.repository_root != root:
        raise ValueError("authority pair repository root differs")
    return context


def _before_legacy_decision_inventory_authority_rebind() -> None:
    """Test seam immediately before authority identities are rebound."""


def _before_legacy_decision_inventory_capability_rebind() -> None:
    """Test seam before the protected capability's prepublication rebind."""


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


def _in_scope_records(
    legacy_evidence: LegacyEvidenceCapability,
) -> list[Mapping[str, object]]:
    records: list[Mapping[str, object]] = []
    for record in legacy_evidence.records:
        if record.source != "baseball_savant_statcast":
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
            records.append(_protected_inventory_record(record))
    return records


def _candidate_order(record: Mapping[str, object]) -> tuple[str, str, bytes]:
    query = record.get("canonical_query")
    if not isinstance(query, Mapping):
        raise ValueError("legacy candidate canonical query differs")
    first = query.get("start")
    last = query.get("end")
    if not isinstance(first, str) or not isinstance(last, str):
        raise ValueError("legacy candidate range differs")
    return first, last, canonical_json_bytes(_plain(record))


def _decision_record(
    query: LogicalQuery,
    evidence: LegacyDecisionEvidence,
) -> dict[str, object]:
    if evidence.decision.query_id != logical_query_id(query):
        raise RuntimeError("legacy decision query binding differs")
    requested = query.requested_range
    return {
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


def preview_legacy_decisions(
    root_batch: QueryBatch,
    legacy_evidence: LegacyEvidenceCapability,
    *,
    protected_inventory_sha256: str,
) -> LegacyDecisionPreview:
    """Compute the exact legacy partition and closed decisions without writes."""
    if not isinstance(root_batch, QueryBatch) or len(root_batch.queries) != 46:
        raise ValueError("preview requires the fixed 46-query Statcast root batch")
    capability = _require_issued_legacy_evidence(legacy_evidence)
    capability.validate()
    if protected_inventory_sha256 != capability.inventory_sha256:
        raise ValueError("preview protected inventory binding differs")

    expected_partition_size = len(root_batch.queries) + 2
    in_scope = sorted(_in_scope_records(capability), key=_candidate_order)
    selected: list[Mapping[str, object]] = []
    for candidate in in_scope:
        candidate_partition = build_legacy_partition_batch_from_catalog(
            root_batch,
            (candidate,),
            protected_inventory_sha256=protected_inventory_sha256,
        )
        candidate_queries = tuple(
            query
            for query in candidate_partition.queries
            if query.requested_range.get("acquisition") == "PROTECTED_LEGACY_REFERENCE"
        )
        if len(candidate_queries) != 1:
            raise ValueError("legacy candidate does not map to one preview query")
        if len(candidate_partition.queries) != expected_partition_size:
            continue
        query = candidate_queries[0]
        evidence = audit_legacy_candidates_evidence((query,), capability)[0]
        if evidence.decision.query_id != logical_query_id(query):
            raise RuntimeError("legacy preview candidate query binding differs")
        if evidence.decision.disposition is not PromotionDisposition.FETCH_REQUIRED:
            selected.append(candidate)
            break
    if len(selected) != 1:
        raise ValueError("legacy preview has no eligible three-way partition")

    partition = build_legacy_partition_batch_from_catalog(
        root_batch,
        selected,
        protected_inventory_sha256=protected_inventory_sha256,
    )
    if len(partition.queries) != expected_partition_size:
        raise ValueError(
            "legacy preview must split one root into exactly three leaves"
        )
    final_evidence = audit_legacy_candidates_evidence(
        partition.queries, capability
    )
    decisions = [
        _decision_record(query, evidence)
        for query, evidence in zip(partition.queries, final_evidence)
    ]
    decisions.sort(
        key=lambda item: (
            item["range"]["year"],
            item["range"]["start_date"],
            item["range"]["end_date"],
            item["candidate_query_id"],
        )
    )
    disposition_counts = {item.value: 0 for item in PromotionDisposition}
    for decision in decisions:
        disposition = decision["disposition"]
        if not isinstance(disposition, str) or disposition not in disposition_counts:
            raise ValueError("legacy preview disposition differs")
        disposition_counts[disposition] += 1
    unique_ranges = {
        (item["canonical_query"]["start"], item["canonical_query"]["end"])
        for item in in_scope
    }
    counts = {
        "protected_catalog_record_count": len(capability.records),
        "in_scope_statcast_record_count": len(in_scope),
        "out_of_scope_record_count": len(capability.records) - len(in_scope),
        "unique_candidate_range_count": len(unique_ranges),
        "promoted_count": disposition_counts["PROMOTED"],
        "fetch_required_count": disposition_counts["FETCH_REQUIRED"],
        "fatal_integrity_count": disposition_counts["FATAL_INTEGRITY"],
    }
    capability.rebind()
    return LegacyDecisionPreview(
        partition_batch=partition,
        decisions=tuple(_deep_freeze(item) for item in decisions),
        counts=_deep_freeze(counts),  # type: ignore[arg-type]
    )


def _authority_identities(
    legacy_evidence: LegacyEvidenceCapability,
    fixed: tuple[tuple[Path, _Snapshot], ...],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    capability = _require_issued_legacy_evidence(legacy_evidence)
    capability.rebind()
    result = [(path.as_posix(), snapshot.identity) for path, snapshot in fixed]
    for record in capability.records:
        for path, device, inode, byte_size in (
            (
                record.manifest_path,
                record.manifest_device,
                record.manifest_inode,
                record.manifest_byte_size,
            ),
            (
                record.seal_path,
                record.seal_device,
                record.seal_inode,
                record.seal_byte_size,
            ),
            (
                record.payload_path,
                record.payload_device,
                record.payload_inode,
                record.payload_byte_size,
            ),
        ):
            result.append((path.as_posix(), (device, inode, byte_size)))
    capability.rebind()
    return tuple(sorted(result))


def _assemble(
    repository_root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    context: SourceAuthorityContext | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> _Authority:
    root = _trusted_root(repository_root)
    if context is None:
        context = _authority_context(root, run_spec_path, acquisition_contract_path)
    elif context.repository_root != root:
        raise ValueError("authority pair repository root differs")
    run_relative = context.authority_path.relative_to(root)
    contract_relative = context.acquisition_contract_path.relative_to(root)
    run_spec, run_snapshot = _json_snapshot(root, run_relative)
    contract, contract_snapshot = _json_snapshot(root, contract_relative)
    schema, schema_snapshot = _json_snapshot(root, _SCHEMA_RELATIVE)
    if hashlib.sha256(schema_snapshot.contents).hexdigest() != _EXPECTED_SCHEMA_SHA256:
        raise ValueError("legacy decision inventory schema authority differs")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError("legacy decision inventory schema is invalid") from exc
    if run_spec.get("run_id") != context.run_id or contract.get("run_id") != context.run_id:
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
    if legacy_evidence is None:
        if context.run_id == _R3_RECOVERY_RUN_ID:
            raise ValueError("r3 requires authenticated external legacy evidence")
        capability = adapt_root_local_legacy_catalog(
            load_protected_legacy_catalog(
                root,
                legacy_root=root / _LEGACY_ROOT_RELATIVE,
                protected_inventory_path=root / _PROTECTED_INVENTORY_RELATIVE,
                protected_inventory_sha256=protected_sha256,
            )
        )
    else:
        capability = _require_issued_legacy_evidence(legacy_evidence)
    if context.run_id == _R3_RECOVERY_RUN_ID:
        from .v52_external_protected_parent import (
            ExternalProtectedParentCapability,
        )

        if type(capability) is not ExternalProtectedParentCapability:
            raise TypeError("r3 requires external protected parent capability")
    if (
        capability.logical_raw_root.as_posix()
        != _LEGACY_ROOT_RELATIVE.as_posix()
        or capability.inventory_sha256 != protected_sha256
    ):
        raise ValueError("legacy evidence authority differs")
    capability.validate()
    root_expected = (
        build_statcast_root_batch_for_authority(
            root,
            run_spec_path=context.authority_path,
            acquisition_contract_path=context.acquisition_contract_path,
        )
        if isinstance(context.authority, SourceRunSpec)
        else build_statcast_root_batch(root)
    )
    root_batch = _load_query_batch(
        root,
        root_expected.batch_id,
        invoke_authority_hook=False,
        authority=context.authority,
    )
    if root_batch != root_expected or len(root_batch.queries) != 46:
        raise ValueError("Statcast root batch authority differs")
    batch_root_relative = context.acquisition_audit_root.relative_to(root) / "query_batches"
    root_batch_relative = batch_root_relative / f"{root_batch.batch_id}.json"
    root_batch_snapshot = _snapshot(root, root / root_batch_relative)

    preview = preview_legacy_decisions(
        root_batch,
        capability,
        protected_inventory_sha256=protected_sha256,
    )
    payload = {
        "schema_version": 1,
        "receipt_kind": "LEGACY_DECISION_INVENTORY",
        "run_id": context.run_id,
        "run_spec": {
            "path": run_relative.as_posix(),
            "sha256": hashlib.sha256(run_snapshot.contents).hexdigest(),
        },
        "acquisition_contract": {
            "path": contract_relative.as_posix(),
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
        "counts": _plain(preview.counts),
        "decisions": _plain(preview.decisions),
    }
    identities = _authority_identities(
        capability,
        (
            (run_relative, run_snapshot),
            (contract_relative, contract_snapshot),
            (_SCHEMA_RELATIVE, schema_snapshot),
            (_PROTECTED_INVENTORY_RELATIVE, protected_snapshot),
            (root_batch_relative, root_batch_snapshot),
        ),
    )
    capability.rebind()
    return _Authority(
        root,
        _validate_payload(payload, schema),
        identities,
        context,
        capability,
    )


def build_legacy_decision_inventory(
    repository_root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> Mapping[str, object]:
    authority = _assemble(
        repository_root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
        legacy_evidence=legacy_evidence,
    )
    return _validate_authority_payload(authority)


def _rebind_authority(initial: _Authority) -> _Authority:
    initial.legacy_evidence.rebind()
    rebound = _assemble(
        initial.root,
        context=initial.context,
        legacy_evidence=initial.legacy_evidence,
    )
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


def _open_inventory_parent(
    root: Path,
    *,
    create: bool,
    inventory_relative: Path = _LEGACY_DECISION_INVENTORY_RELATIVE,
) -> tuple[list[int], int]:
    descriptors = [os.open(root, _directory_flags())]
    try:
        current = descriptors[0]
        for component in inventory_relative.parent.parts:
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
    inventory_name: str = _LEGACY_DECISION_INVENTORY_RELATIVE.name,
) -> tuple[bytes, tuple[int, int, int, int]] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(
            inventory_name,
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
            inventory_name,
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
    root: Path,
    contents: bytes,
    identity: tuple[int, int, int, int],
    inventory_relative: Path = _LEGACY_DECISION_INVENTORY_RELATIVE,
) -> None:
    descriptors, parent_fd = _open_inventory_parent(
        root, create=False, inventory_relative=inventory_relative
    )
    locked = False
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        locked = True
        current = _read_final_at(parent_fd, inventory_relative.name)
        if current is None or current != (contents, identity):
            return
        os.unlink(inventory_relative.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if locked:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish_legacy_decision_inventory(
    repository_root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> VerifiedLegacyDecisionInventory:
    assembled = _assemble(
        repository_root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
        legacy_evidence=legacy_evidence,
    )
    initial = _Authority(
        assembled.root,
        _validate_authority_payload(assembled),
        assembled.identities,
        assembled.context,
        assembled.legacy_evidence,
    )
    counts = initial.payload["counts"]
    if not isinstance(counts, Mapping) or counts["fatal_integrity_count"]:
        raise RuntimeError("fatal legacy integrity decisions cannot be published")
    contents = _canonical_file(initial.payload)
    inventory_relative = (
        initial.context.acquisition_audit_root.relative_to(initial.root)
        / "legacy_decision_inventory.json"
    )
    descriptors, parent_fd = _open_inventory_parent(
        initial.root, create=True, inventory_relative=inventory_relative
    )
    temp_name: str | None = None
    temp_fd = -1
    locked = False
    created_identity: tuple[int, int, int, int] | None = None
    expected_identity: tuple[int, int, int, int] | None = None
    authority = initial
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        locked = True
        existing = _read_final_at(parent_fd, inventory_relative.name)
        if existing is not None and existing[0] != contents:
            raise RuntimeError("immutable legacy decision inventory differs")
        _before_legacy_decision_inventory_capability_rebind()
        initial.legacy_evidence.rebind()
        if existing is None:
            temp_name = (
                f".{inventory_relative.name}."
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
                    inventory_relative.name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raced = _read_final_at(parent_fd, inventory_relative.name)
                if raced is None or raced[0] != contents:
                    raise RuntimeError(
                        "concurrent immutable legacy decision inventory differs"
                    ) from None
            else:
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = None
                os.fsync(parent_fd)
        final_record = _read_final_at(parent_fd, inventory_relative.name)
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
            authority = _Authority(
                rebound.root,
                rebound_payload,
                rebound.identities,
                rebound.context,
                rebound.legacy_evidence,
            )
        except BaseException:
            if created_identity is not None:
                owned = _read_final_at(parent_fd, inventory_relative.name)
                if owned == (contents, created_identity):
                    os.unlink(inventory_relative.name, dir_fd=parent_fd)
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

    final = authority.root / inventory_relative
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
        verified_authority = _Authority(
            post.root,
            post_payload,
            post.identities,
            post.context,
            post.legacy_evidence,
        )
        return _verified(verified_authority, final, contents)
    except BaseException:
        if created_identity is not None:
            _remove_owned_final(
                authority.root, contents, created_identity, inventory_relative
            )
        raise


def load_verified_legacy_decision_inventory(
    repository_root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> VerifiedLegacyDecisionInventory:
    assembled = _assemble(
        repository_root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
        legacy_evidence=legacy_evidence,
    )
    initial = _Authority(
        assembled.root,
        _validate_authority_payload(assembled),
        assembled.identities,
        assembled.context,
        assembled.legacy_evidence,
    )
    inventory_relative = (
        initial.context.acquisition_audit_root.relative_to(initial.root)
        / "legacy_decision_inventory.json"
    )
    final = initial.root / inventory_relative
    try:
        descriptors, parent_fd = _open_inventory_parent(
            initial.root, create=False, inventory_relative=inventory_relative
        )
    except FileNotFoundError as exc:
        raise ValueError("legacy decision inventory is missing") from exc
    try:
        stored_record = _read_final_at(parent_fd, inventory_relative.name)
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
        final_record = _read_final_at(parent_fd, inventory_relative.name)
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
    authority = _Authority(
        rebound.root,
        rebound_payload,
        rebound.identities,
        rebound.context,
        rebound.legacy_evidence,
    )
    return _verified(authority, final, contents)


def _load_legacy_partition_decision_replay(
    repository_root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> tuple[str, str, tuple[Mapping[str, object], ...]]:
    """Return the sealed decision-file hash, protected hash, and frozen decisions."""
    loader_kwargs: dict[str, object] = {
        "run_spec_path": run_spec_path,
        "acquisition_contract_path": acquisition_contract_path,
    }
    if legacy_evidence is not None:
        loader_kwargs["legacy_evidence"] = legacy_evidence
    verified = load_verified_legacy_decision_inventory(
        repository_root,
        **loader_kwargs,
    )
    protected = verified.payload.get("protected_inventory")
    decisions = verified.payload.get("decisions")
    counts = verified.payload.get("counts")
    if (
        not isinstance(protected, Mapping)
        or not isinstance(protected.get("sha256"), str)
        or not isinstance(decisions, tuple)
        or not all(isinstance(decision, Mapping) for decision in decisions)
        or not isinstance(counts, Mapping)
        or counts.get("fatal_integrity_count") != 0
    ):
        raise ValueError("legacy decision replay authority differs")
    return (
        verified.sha256,
        protected["sha256"],
        tuple(decisions),
    )


__all__ = [
    "LegacyDecisionPreview",
    "VerifiedLegacyDecisionInventory",
    "build_legacy_decision_inventory",
    "load_verified_legacy_decision_inventory",
    "preview_legacy_decisions",
    "publish_legacy_decision_inventory",
]
