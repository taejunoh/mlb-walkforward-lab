from __future__ import annotations

import hashlib

from .v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    VerifiedEvidenceRead,
    logical_query_id,
)
from .v52_source_contracts import query_sidecar, recompute_query_sidecar


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
            raise ValueError(
                "derived disposition source verification is not implemented"
            )
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
