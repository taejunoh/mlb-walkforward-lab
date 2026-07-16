from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .execution_registry import (
    AcquisitionCheckpoint,
    load_source_verification_checkpoint,
)
from .protocols import RunSpec

from .v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    VerifiedEvidenceRead,
    canonical_query_bytes,
    logical_query_id,
)
from .v52_query_plan import (
    QueryBatch,
    SplitReceipt,
    load_query_batch,
    load_split_receipt,
)
from .v52_source_contracts import (
    canonical_json_bytes,
    query_sidecar,
    recompute_query_sidecar,
)


_STATCAST_ROOT_BATCH_PATH = re.compile(
    r"^audit/v5_2_qd/historical/acquisition/query_batches/"
    r"(?P<batch_id>[0-9a-f]{64})\.json$"
)


@dataclass(frozen=True)
class _TerminalQueryGraph:
    checkpoint: AcquisitionCheckpoint
    statcast_root_batch: QueryBatch
    query_batches: tuple[QueryBatch, ...]
    split_receipts: tuple[SplitReceipt, ...]
    split_parent_queries: tuple[LogicalQuery, ...]
    completed_queries: tuple[LogicalQuery, ...]
    root_queries: tuple[LogicalQuery, ...]
    split_leaf_queries: tuple[LogicalQuery, ...]


def _enumerate_completed_query_graph(
    checkpoint: AcquisitionCheckpoint,
    batches: Sequence[QueryBatch],
    receipts: Sequence[SplitReceipt],
    statcast_root_batch: QueryBatch,
) -> _TerminalQueryGraph:
    if checkpoint.stage != "REMAINING":
        raise ValueError("source verification requires a REMAINING checkpoint")
    if checkpoint.pending_query_ids or checkpoint.quarantined_query_ids:
        raise ValueError("source verification requires terminal query status")

    batch_ids = tuple(batch.batch_id for batch in batches)
    if (
        batch_ids != checkpoint.query_batch_ids
        or len(batch_ids) != len(set(batch_ids))
        or len(checkpoint.query_batch_sha256s) != len(batch_ids)
    ):
        raise ValueError("terminal query batch binding differs")
    batch_by_id = {batch.batch_id: batch for batch in batches}
    batch_index = {batch_id: index for index, batch_id in enumerate(batch_ids)}

    query_by_id: dict[str, LogicalQuery] = {}
    query_batch: dict[str, str] = {}
    for batch in batches:
        for query in batch.queries:
            query_id = logical_query_id(query)
            if query_id in query_by_id:
                raise ValueError("terminal query occurs in multiple batches")
            query_by_id[query_id] = query
            query_batch[query_id] = batch.batch_id

    split_ids = checkpoint.split_query_ids
    completed_ids = checkpoint.completed_query_ids
    if (
        split_ids != tuple(sorted(split_ids))
        or completed_ids != tuple(sorted(completed_ids))
        or len(split_ids) != len(set(split_ids))
        or len(completed_ids) != len(set(completed_ids))
        or set(split_ids) & set(completed_ids)
        or set(query_by_id) != set(split_ids) | set(completed_ids)
    ):
        raise ValueError("terminal query population binding differs")

    receipt_by_parent: dict[str, SplitReceipt] = {}
    child_batch_ids: set[str] = set()
    split_receipt_ids: set[str] = set()
    for receipt in receipts:
        if (
            receipt.parent_query_id in receipt_by_parent
            or receipt.split_receipt_id in split_receipt_ids
            or receipt.child_batch_id in child_batch_ids
        ):
            raise ValueError("terminal split receipt is duplicated")
        receipt_by_parent[receipt.parent_query_id] = receipt
        split_receipt_ids.add(receipt.split_receipt_id)
        child_batch_ids.add(receipt.child_batch_id)
    if set(receipt_by_parent) != set(split_ids):
        raise ValueError("terminal split receipt parents differ")
    ordered_receipts = tuple(receipt_by_parent[parent_id] for parent_id in split_ids)
    if tuple(receipt.receipt_sha256 for receipt in ordered_receipts) != (
        checkpoint.split_receipt_sha256s
    ):
        raise ValueError("terminal split receipt hash binding differs")

    root_batch_ids = set(batch_ids) - child_batch_ids
    if not root_batch_ids or child_batch_ids != set(batch_ids) - root_batch_ids:
        raise ValueError("terminal split batch population differs")
    batch_depth = {batch_id: 0 for batch_id in root_batch_ids}
    receipt_by_child = {
        receipt.child_batch_id: receipt for receipt in ordered_receipts
    }
    if set(receipt_by_child) != child_batch_ids:
        raise ValueError("terminal split child batches differ")

    for child_batch_id in batch_ids:
        if child_batch_id in root_batch_ids:
            continue
        receipt = receipt_by_child[child_batch_id]
        parent_batch = batch_by_id.get(receipt.parent_batch_id)
        child_batch = batch_by_id.get(child_batch_id)
        parent_query = query_by_id.get(receipt.parent_query_id)
        if (
            parent_batch is None
            or child_batch is None
            or parent_query is None
            or receipt.stage != "REMAINING"
            or parent_batch.stage != "REMAINING"
            or child_batch.stage != "REMAINING"
            or query_batch[receipt.parent_query_id] != parent_batch.batch_id
            or batch_index[parent_batch.batch_id] >= batch_index[child_batch_id]
            or child_batch.derived_from_sha256 != parent_batch.batch_id
            or parent_batch.batch_id not in batch_depth
            or receipt.depth != batch_depth[parent_batch.batch_id] + 1
        ):
            raise ValueError("terminal split parent/child binding differs")
        child_query_ids = tuple(
            logical_query_id(query) for query in child_batch.queries
        )
        if (
            receipt.child_query_ids != child_query_ids
            or any(
                query.parent_query_id != receipt.parent_query_id
                for query in child_batch.queries
            )
        ):
            raise ValueError("terminal split ordered children differ")
        batch_depth[child_batch_id] = receipt.depth

    completed = tuple(query_by_id[query_id] for query_id in completed_ids)
    root_queries = tuple(
        query
        for query in completed
        if query_batch[logical_query_id(query)] in root_batch_ids
    )
    split_leaf_queries = tuple(
        query
        for query in completed
        if query_batch[logical_query_id(query)] in child_batch_ids
    )
    if len(root_queries) + len(split_leaf_queries) != len(completed):
        raise ValueError("terminal completed leaf classification differs")
    return _TerminalQueryGraph(
        checkpoint=checkpoint,
        statcast_root_batch=statcast_root_batch,
        query_batches=tuple(batches),
        split_receipts=ordered_receipts,
        split_parent_queries=tuple(query_by_id[query_id] for query_id in split_ids),
        completed_queries=completed,
        root_queries=root_queries,
        split_leaf_queries=split_leaf_queries,
    )


def _query_batch_artifact_sha256(batch: QueryBatch) -> str:
    record = {
        "batch_id": batch.batch_id,
        "derived_from_sha256": batch.derived_from_sha256,
        "queries": [
            json.loads(canonical_query_bytes(query)) for query in batch.queries
        ],
        "schema_version": 1,
        "stage": batch.stage,
    }
    return hashlib.sha256(canonical_json_bytes(record) + b"\n").hexdigest()


def _load_statcast_root_batch(
    root: Path,
    checkpoint: AcquisitionCheckpoint,
) -> QueryBatch:
    paths = checkpoint.stage_evidence_receipt_paths
    hashes = checkpoint.stage_evidence_receipt_sha256s
    if len(paths) != len(hashes):
        raise ValueError("terminal stage evidence binding differs")
    candidates = [
        (match.group("batch_id"), digest)
        for path, digest in zip(paths, hashes, strict=True)
        if (match := _STATCAST_ROOT_BATCH_PATH.fullmatch(path)) is not None
    ]
    if len(candidates) != 1:
        raise ValueError("terminal Statcast root batch binding differs")
    batch_id, expected_sha256 = candidates[0]
    if batch_id in checkpoint.query_batch_ids:
        raise ValueError("terminal Statcast root batch overlaps checkpoint batches")
    batch = load_query_batch(root, batch_id, read_only_progress=True)
    if (
        batch.batch_id != batch_id
        or batch.stage != "REMAINING"
        or batch.derived_from_sha256 is not None
        or len(batch.queries) != 46
        or any(
            query.source != "baseball_savant_statcast"
            or query.grain != "pitch"
            or query.parent_query_id is not None
            for query in batch.queries
        )
        or _query_batch_artifact_sha256(batch) != expected_sha256
    ):
        raise ValueError("terminal Statcast root batch authority differs")
    return batch


def _load_terminal_query_graph(
    root: Path,
    run_spec: RunSpec,
) -> _TerminalQueryGraph:
    checkpoint = load_source_verification_checkpoint(root, run_spec)
    statcast_root_batch = _load_statcast_root_batch(root, checkpoint)
    batches = tuple(
        load_query_batch(root, batch_id, read_only_progress=True)
        for batch_id in checkpoint.query_batch_ids
    )
    namespaces = run_spec.raw.get("namespaces")
    raw_namespace = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(raw_namespace, str) or not raw_namespace:
        raise ValueError("source verification raw namespace differs")
    raw_relative = Path(raw_namespace)
    if raw_relative.is_absolute() or ".." in raw_relative.parts:
        raise ValueError("source verification raw namespace must be relative")
    evidence_root = root / raw_relative
    receipts = tuple(
        load_split_receipt(
            root,
            parent_id,
            evidence_root=evidence_root,
            read_only_progress=True,
        )
        for parent_id in checkpoint.split_query_ids
    )
    if any(receipt is None for receipt in receipts):
        raise ValueError("terminal split receipt is missing")
    return _enumerate_completed_query_graph(
        checkpoint,
        batches,
        tuple(receipt for receipt in receipts if receipt is not None),
        statcast_root_batch,
    )


def _recompute_completed_queries(
    store: V52EvidenceStore,
    queries: Sequence[LogicalQuery],
) -> tuple[Mapping[str, object], ...]:
    return tuple(
        _read_and_recompute_query(store, query)
        for query in sorted(queries, key=logical_query_id)
    )


def _read_and_recompute_query(
    store: V52EvidenceStore,
    query: LogicalQuery,
) -> dict[str, object]:
    if not isinstance(store, V52EvidenceStore) or not store._read_only:
        raise ValueError("source verification requires a read-only evidence store")
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")

    result: dict[str, object] | None = None

    def recompute_verified_read(verified: VerifiedEvidenceRead) -> None:
        nonlocal result
        published = verified.published_evidence
        query_id = logical_query_id(query)
        if verified.registered_query != query or published.query_id != query_id:
            raise ValueError("verified query identity binding differs")
        if verified.success_receipt.get("storage_mode") != published.storage_mode:
            raise ValueError("verified storage mode binding differs")
        if published.storage_mode == "DERIVED_DISPOSITION":
            if (
                verified.raw_bytes is not None
                or published.raw_sha256 is not None
                or verified.attempt_receipts
                or verified.success_receipt.get("attempt_number") is not None
                or verified.success_receipt.get("attempt_history") != []
            ):
                raise ValueError("derived disposition evidence binding differs")
            reason = query.requested_range.get("reason")
            if reason != "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START":
                raise ValueError("derived disposition reason binding differs")
            semantics = dict(recompute_query_sidecar(query, b""))
            semantics["reason"] = reason
            expected_sidecar = {
                "grain": query.grain,
                "parser_version": query.parser_version,
                "query_id": query_id,
                "schema_version": 1,
                "semantics": semantics,
                "source": query.source,
                "storage_mode": "DERIVED_DISPOSITION",
            }
            if dict(verified.query_sidecar) != expected_sidecar:
                raise ValueError(
                    "derived query sidecar semantic recomputation differs"
                )
            result = {
                "query_id": query_id,
                "query_sidecar": expected_sidecar,
                "raw_sha256": None,
                "storage_mode": published.storage_mode,
            }
            return
        if published.storage_mode not in {
            "FETCHED_RAW",
            "PROTECTED_LEGACY_REFERENCE",
        }:
            raise ValueError("verified storage mode differs")

        raw = verified.raw_bytes
        if not isinstance(raw, bytes):
            raise ValueError("verified raw bytes are required")
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if published.raw_sha256 != raw_sha256:
            raise ValueError("verified raw hash binding differs")

        semantics = recompute_query_sidecar(query, raw)
        expected_sidecar = query_sidecar(raw, query, semantics)
        if dict(verified.query_sidecar) != expected_sidecar:
            raise ValueError("query sidecar semantic recomputation differs")

        result = {
            "query_id": query_id,
            "query_sidecar": expected_sidecar,
            "raw_sha256": raw_sha256,
            "storage_mode": published.storage_mode,
        }

    store.inspect_verified_success(query, recompute_verified_read)
    if result is None:
        raise AssertionError("verified success inspection did not produce a result")
    return result
