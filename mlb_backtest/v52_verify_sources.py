from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Sequence

from .execution_registry import (
    AcquisitionCheckpoint,
    load_source_verification_checkpoint,
)
from .v52_game_evidence_inventory import (
    VerifiedGameEvidenceInventory,
    validate_game_evidence_snapshot,
)
from .v52_schedule_inventory import VerifiedScheduleAnchorInventory
from .v52_sources.schedule import recompute_game_evidence_snapshot
from .v52_timezone_review import VerifiedTimezoneGate, validate_timezone_mapping
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
    _make_batch,
    bisect_statcast_query,
    load_query_batch,
    load_split_receipt,
    validate_statcast_query,
)
from .v52_seasons import SEASON_BOUNDARIES, monthly_ranges
from .v52_source_contracts import (
    canonical_json_bytes,
    query_sidecar,
    recompute_query_sidecar,
)
from .v52_sources.statcast import (
    recompute_statcast_season_snapshot,
    validate_statcast_child_coverage,
)
from .v52_sources.pitching import recompute_pitching_season_snapshot


_STATCAST_ROOT_BATCH_PATH = re.compile(
    r"^audit/v5_2_qd/historical/acquisition/query_batches/"
    r"(?P<batch_id>[0-9a-f]{64})\.json$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_BOXSCORE_ENDPOINT = re.compile(
    r"^https://statsapi\.mlb\.com/api/v1/game/(?P<game_pk>[1-9][0-9]*)/boxscore$"
)
_GAME_SEASONS = tuple(range(2018, 2025))
_NORMALIZED_ENVELOPE_KEYS = frozenset(
    {
        "grain",
        "logical_query_ids",
        "payload",
        "run_id",
        "schema_version",
        "season",
        "source",
        "source_query_sidecar_sha256s",
        "split_receipt_sha256s",
        "status",
    }
)
_GAME_PAYLOAD_KEYS = frozenset(
    {"games", "grain", "schema_version", "source_query_sidecar_sha256s"}
)
_STATCAST_PAYLOAD_KEYS = frozenset(
    {
        "child_query_sidecar_sha256s",
        "children",
        "grain",
        "parent_coverage",
        "played_game_pks",
        "schema_version",
        "split_state",
        "statcast_game_pks",
        "unique_pitch_count",
    }
)
_PITCHING_PAYLOAD_KEYS = frozenset(
    {"grain", "query_sidecar_sha256s", "records", "schema_version"}
)
_PITCHING_RECORD_COMMON_KEYS = frozenset(
    {
        "as_of_date",
        "consumer_cutoff_dates",
        "consuming_game_pks",
        "query_sidecar_sha256",
    }
)
_PITCHING_RECORD_NUMERIC_KEYS = frozenset(
    {"batters_faced", "earned_runs", "outs"}
)
_GAME_INVENTORY_KEYS = frozenset(
    {
        "acquisition_contract",
        "artifact_kind",
        "counts",
        "games",
        "query_batches",
        "run_id",
        "run_spec",
        "schedule_inventory",
        "schema_version",
        "source_query_sidecar_sha256s",
        "timezone_gate",
    }
)
_GAME_COUNT_KEYS = frozenset(
    {
        "game_count",
        "played_game_count",
        "unique_actual_starter_count",
        "unplayed_game_count",
        "verified_feature_consumer_game_count",
    }
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


@dataclass(frozen=True)
class VerifiedQueryRecord:
    query: LogicalQuery
    query_id: str
    query_sidecar_bytes: bytes
    query_sidecar_sha256: str
    raw_sha256: str | None
    storage_mode: Literal[
        "DERIVED_DISPOSITION",
        "FETCHED_RAW",
        "PROTECTED_LEGACY_REFERENCE",
    ]
    retained_raw_bytes: bytes | None


@dataclass(frozen=True)
class NormalizedSnapshot:
    schema_version: Literal[1]
    run_id: str
    season: int
    source: Literal[
        "baseball_savant_statcast",
        "mlb_schedule_game_feed",
        "mlb_stats_pitching",
    ]
    grain: Literal[
        "pitch",
        "season_d1_league_pitching_reference",
        "season_game_evidence_snapshot",
    ]
    status: Literal["READY"]
    logical_query_ids: tuple[str, ...]
    source_query_sidecar_sha256s: tuple[str, ...]
    split_receipt_sha256s: tuple[str, ...]
    payload: Mapping[str, object]
    canonical_bytes: bytes
    sha256: str

    def as_mapping(self) -> dict[str, object]:
        return {
            "grain": self.grain,
            "logical_query_ids": list(self.logical_query_ids),
            "payload": _plain_json(self.payload),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "season": self.season,
            "source": self.source,
            "source_query_sidecar_sha256s": list(
                self.source_query_sidecar_sha256s
            ),
            "split_receipt_sha256s": list(self.split_receipt_sha256s),
            "status": self.status,
        }


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _sorted_unique_sha256s(
    value: object,
    label: str,
    *,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"normalized snapshot {label} differ")
    if any(
        not isinstance(item, str) or _SHA256.fullmatch(item) is None
        for item in value
    ) or value != sorted(set(value)):
        raise ValueError(f"normalized snapshot {label} differ")
    return value


def _validate_normalized_snapshot_mapping(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _NORMALIZED_ENVELOPE_KEYS:
        raise ValueError("normalized snapshot envelope keys differ")
    envelope = dict(value)
    season = envelope.get("season")
    if (
        envelope.get("schema_version") != 1
        or not isinstance(envelope.get("run_id"), str)
        or not envelope["run_id"]
        or isinstance(season, bool)
        or not isinstance(season, int)
        or season not in _GAME_SEASONS
        or envelope.get("status") != "READY"
    ):
        raise ValueError("normalized snapshot envelope identity differs")
    query_ids = _sorted_unique_sha256s(
        envelope.get("logical_query_ids"), "query IDs"
    )
    top_sidecars = _sorted_unique_sha256s(
        envelope.get("source_query_sidecar_sha256s"), "sidecar hashes"
    )
    if len(query_ids) != len(top_sidecars):
        raise ValueError("normalized snapshot query/sidecar counts differ")

    source = envelope.get("source")
    grain = envelope.get("grain")
    if source == "mlb_schedule_game_feed" and grain == "season_game_evidence_snapshot":
        split_hashes = _sorted_unique_sha256s(
            envelope.get("split_receipt_sha256s"),
            "split receipt hashes",
            allow_empty=True,
        )
        if split_hashes:
            raise ValueError("game normalized snapshot cannot own split receipts")

        payload_value = envelope.get("payload")
        if (
            not isinstance(payload_value, Mapping)
            or set(payload_value) != _GAME_PAYLOAD_KEYS
        ):
            raise ValueError("normalized snapshot game payload keys differ")
        payload = dict(payload_value)
        payload_sidecars = _sorted_unique_sha256s(
            payload.get("source_query_sidecar_sha256s"),
            "payload sidecar hashes",
        )
        games = payload.get("games")
        if (
            payload.get("schema_version") != 1
            or payload.get("grain") != "season_game_evidence_snapshot"
            or top_sidecars != payload_sidecars
            or not isinstance(games, list)
            or not games
        ):
            raise ValueError("normalized snapshot game payload identity differs")

        for game in games:
            if not isinstance(game, Mapping) or game.get("season") != season:
                raise ValueError("normalized snapshot game season differs")
        validate_game_evidence_snapshot(payload)
        return envelope

    if source == "mlb_stats_pitching" and grain == (
        "season_d1_league_pitching_reference"
    ):
        split_hashes = _sorted_unique_sha256s(
            envelope.get("split_receipt_sha256s"),
            "split receipt hashes",
            allow_empty=True,
        )
        if split_hashes:
            raise ValueError("pitching normalized snapshot cannot own split receipts")
        payload_value = envelope.get("payload")
        if (
            not isinstance(payload_value, Mapping)
            or set(payload_value) != _PITCHING_PAYLOAD_KEYS
        ):
            raise ValueError("normalized snapshot pitching payload keys differ")
        payload = dict(payload_value)
        records = payload.get("records")
        payload_hashes = payload.get("query_sidecar_sha256s")
        if (
            payload.get("schema_version") != 1
            or payload.get("grain")
            != "season_d1_league_pitching_reference"
            or not isinstance(records, list)
            or not records
            or not isinstance(payload_hashes, list)
            or len(records) != len(query_ids)
            or len(payload_hashes) != len(records)
            or any(
                not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
                for digest in payload_hashes
            )
            or len(set(payload_hashes)) != len(payload_hashes)
            or set(payload_hashes) != set(top_sidecars)
        ):
            raise ValueError("normalized snapshot pitching payload identity differs")

        prior_as_of: date | None = None
        seen_consumers: set[int] = set()
        expected_hashes: list[str] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("normalized snapshot pitching record differs")
            keys = set(record)
            numeric = keys == (
                _PITCHING_RECORD_COMMON_KEYS | _PITCHING_RECORD_NUMERIC_KEYS
            )
            derived = keys == (_PITCHING_RECORD_COMMON_KEYS | {"disposition"})
            if not numeric and not derived:
                raise ValueError("normalized snapshot pitching record keys differ")
            as_of = _canonical_day(
                record.get("as_of_date"), "pitching record as-of date"
            )
            consumers = record.get("consuming_game_pks")
            cutoffs = record.get("consumer_cutoff_dates")
            digest = record.get("query_sidecar_sha256")
            if (
                as_of.year != season
                or (prior_as_of is not None and as_of <= prior_as_of)
                or not isinstance(consumers, list)
                or not consumers
                or any(type(game_pk) is not int or game_pk <= 0 for game_pk in consumers)
                or consumers != sorted(set(consumers))
                or seen_consumers.intersection(consumers)
                or not isinstance(cutoffs, Mapping)
                or dict(cutoffs)
                != {str(game_pk): as_of.isoformat() for game_pk in consumers}
                or not isinstance(digest, str)
                or _SHA256.fullmatch(digest) is None
            ):
                raise ValueError("normalized snapshot pitching record identity differs")
            if derived:
                if (
                    record.get("disposition")
                    != "PRESEASON_NO_CURRENT_SEASON_PITCHING"
                    or as_of != SEASON_BOUNDARIES[season][0] - timedelta(days=1)
                ):
                    raise ValueError(
                        "normalized snapshot pitching disposition differs"
                    )
            elif (
                as_of < SEASON_BOUNDARIES[season][0]
                or any(
                    type(record.get(field)) is not int or record[field] < 0
                    for field in _PITCHING_RECORD_NUMERIC_KEYS
                )
            ):
                raise ValueError("normalized snapshot pitching metrics differ")
            prior_as_of = as_of
            seen_consumers.update(consumers)
            expected_hashes.append(digest)
        if payload_hashes != expected_hashes:
            raise ValueError("normalized snapshot pitching hash order differs")
        return envelope

    if source != "baseball_savant_statcast" or grain != "pitch":
        raise ValueError("normalized snapshot envelope identity differs")
    split_value = envelope.get("split_receipt_sha256s")
    if not isinstance(split_value, list) or any(
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        for digest in split_value
    ) or len(split_value) != len(set(split_value)):
        raise ValueError("normalized snapshot split receipt hashes differ")

    payload_value = envelope.get("payload")
    if (
        not isinstance(payload_value, Mapping)
        or set(payload_value) != _STATCAST_PAYLOAD_KEYS
    ):
        raise ValueError("normalized snapshot Statcast payload keys differ")
    payload = dict(payload_value)
    coverage = payload.get("parent_coverage")
    children = payload.get("children")
    child_hashes = payload.get("child_query_sidecar_sha256s")
    played = payload.get("played_game_pks")
    statcast = payload.get("statcast_game_pks")
    if (
        payload.get("schema_version") != 1
        or payload.get("grain") != "pitch"
        or payload.get("split_state") != "READY_COMPLETE"
        or not isinstance(coverage, Mapping)
        or set(coverage) != {"inclusive_start", "inclusive_end"}
        or not isinstance(children, list)
        or not children
        or not isinstance(child_hashes, list)
        or any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in child_hashes
        )
        or len(child_hashes) != len(set(child_hashes))
        or set(child_hashes) != set(top_sidecars)
        or len(child_hashes) != len(query_ids)
        or not isinstance(played, list)
        or played != sorted(set(played))
        or any(type(item) is not int or item <= 0 for item in played)
        or statcast != played
        or type(payload.get("unique_pitch_count")) is not int
        or payload["unique_pitch_count"] < 0
    ):
        raise ValueError("normalized snapshot Statcast payload identity differs")
    first = _canonical_day(coverage.get("inclusive_start"), "Statcast coverage start")
    last = _canonical_day(coverage.get("inclusive_end"), "Statcast coverage end")
    if (
        first.year != season
        or last.year != season
        or (first.year, first.month) != (last.year, last.month)
        or first > last
    ):
        raise ValueError("normalized snapshot Statcast season or coverage differs")
    canonical_children = validate_statcast_child_coverage(coverage, children)
    if [item.get("query_sidecar_sha256") for item in canonical_children] != child_hashes:
        raise ValueError("normalized snapshot Statcast child hashes differ")
    if any(
        set(item)
        != {
            "inclusive_start",
            "inclusive_end",
            "query_sidecar_sha256",
            "split_disposition",
        }
        or item.get("split_disposition") != "READY"
        for item in canonical_children
    ):
        raise ValueError("normalized snapshot Statcast children differ")
    return envelope


def _canonical_day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} differs")
    try:
        result = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    if result.isoformat() != value:
        raise ValueError(f"{label} differs")
    return result


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


def _recompute_verified_query_records(
    store: V52EvidenceStore,
    queries: Sequence[LogicalQuery],
) -> tuple[VerifiedQueryRecord, ...]:
    return tuple(
        _read_verified_query_record(store, query)
        for query in sorted(queries, key=logical_query_id)
    )


def _retain_raw_for_aggregation(query: LogicalQuery) -> bool:
    if (
        query.source != "venue_team_timezone_table"
        or query.grain != "effective_dated_venue_team_reference"
    ):
        return False
    requested = dict(query.requested_range)
    if query.endpoint == "https://statsapi.mlb.com/api/v1/teams":
        season = requested.get("season")
        return (
            not isinstance(season, bool)
            and isinstance(season, int)
            and requested == {"season": season, "entity": "teams"}
        )
    venue_match = re.fullmatch(
        r"https://statsapi\.mlb\.com/api/v1/venues/(?P<venue_id>[1-9][0-9]*)",
        query.endpoint,
    )
    if venue_match is None:
        return False
    venue_id = int(venue_match.group("venue_id"))
    return requested == {"venue_id": venue_id}


def _verified_sidecar(record: VerifiedQueryRecord) -> dict[str, object]:
    if not isinstance(record, VerifiedQueryRecord):
        raise TypeError("normalized snapshot records must be VerifiedQueryRecord")
    query_id = logical_query_id(record.query)
    if (
        record.query_id != query_id
        or record.storage_mode
        not in {"FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"}
        or record.retained_raw_bytes is not None
        or not isinstance(record.query_sidecar_bytes, bytes)
        or not record.query_sidecar_bytes.endswith(b"\n")
        or hashlib.sha256(record.query_sidecar_bytes).hexdigest()
        != record.query_sidecar_sha256
    ):
        raise ValueError("normalized snapshot verified query binding differs")
    try:
        sidecar = json.loads(record.query_sidecar_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("normalized snapshot sidecar is not canonical JSON") from exc
    if (
        not isinstance(sidecar, dict)
        or canonical_json_bytes(sidecar) + b"\n" != record.query_sidecar_bytes
        or sidecar.get("query_id") != query_id
        or sidecar.get("source") != record.query.source
        or sidecar.get("grain") != record.query.grain
        or sidecar.get("raw_sha256") != record.raw_sha256
    ):
        raise ValueError("normalized snapshot query sidecar binding differs")
    return sidecar


def _verified_pitching_sidecar(
    record: VerifiedQueryRecord,
) -> dict[str, object]:
    """Accept the one closed no-HTTP pitching disposition in addition to raw evidence."""
    if not isinstance(record, VerifiedQueryRecord):
        raise TypeError("normalized snapshot records must be VerifiedQueryRecord")
    if record.storage_mode != "DERIVED_DISPOSITION":
        return _verified_sidecar(record)
    query = record.query
    query_id = logical_query_id(query)
    if (
        query.source != "mlb_stats_pitching"
        or query.grain != "season_d1_league_pitching_reference"
        or query.endpoint
        != "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
        or query.requested_range.get("reason")
        != "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START"
        or record.query_id != query_id
        or record.raw_sha256 is not None
        or record.retained_raw_bytes is not None
        or not isinstance(record.query_sidecar_bytes, bytes)
        or not record.query_sidecar_bytes.endswith(b"\n")
        or hashlib.sha256(record.query_sidecar_bytes).hexdigest()
        != record.query_sidecar_sha256
    ):
        raise ValueError("derived pitching verified query binding differs")
    semantics = dict(recompute_query_sidecar(query, b""))
    semantics["reason"] = "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START"
    expected = {
        "grain": query.grain,
        "parser_version": query.parser_version,
        "query_id": query_id,
        "schema_version": 1,
        "semantics": semantics,
        "source": query.source,
        "storage_mode": "DERIVED_DISPOSITION",
    }
    expected_bytes = canonical_json_bytes(expected) + b"\n"
    if record.query_sidecar_bytes != expected_bytes:
        raise ValueError("derived pitching query sidecar binding differs")
    return expected


def _binding_sha256(value: object, label: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} binding differs")
    digest = value.get("sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{label} SHA-256 differs")
    return digest


def _normalized_snapshot(
    run_id: str,
    season: int,
    records: Sequence[VerifiedQueryRecord],
    payload: Mapping[str, object],
) -> NormalizedSnapshot:
    query_ids = tuple(sorted({record.query_id for record in records}))
    hashes_value = payload.get("source_query_sidecar_sha256s")
    if not isinstance(hashes_value, list):
        raise ValueError("game snapshot source sidecar hashes differ")
    sidecar_hashes = tuple(hashes_value)
    if (
        not query_ids
        or sidecar_hashes != tuple(sorted(set(sidecar_hashes)))
        or any(
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            for digest in sidecar_hashes
        )
        or {record.query_sidecar_sha256 for record in records}
        != set(sidecar_hashes)
    ):
        raise ValueError("game snapshot logical query or sidecar population differs")
    envelope: dict[str, object] = {
        "grain": "season_game_evidence_snapshot",
        "logical_query_ids": list(query_ids),
        "payload": _plain_json(payload),
        "run_id": run_id,
        "schema_version": 1,
        "season": season,
        "source": "mlb_schedule_game_feed",
        "source_query_sidecar_sha256s": list(sidecar_hashes),
        "split_receipt_sha256s": [],
        "status": "READY",
    }
    envelope = _validate_normalized_snapshot_mapping(envelope)
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    if digest in set(query_ids) | set(sidecar_hashes):
        raise ValueError("normalized snapshot hash collides with a component hash")
    frozen_payload = _freeze_json(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("normalized snapshot payload must freeze to a mapping")
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=season,
        source="mlb_schedule_game_feed",
        grain="season_game_evidence_snapshot",
        status="READY",
        logical_query_ids=query_ids,
        source_query_sidecar_sha256s=sidecar_hashes,
        split_receipt_sha256s=(),
        payload=frozen_payload,
        canonical_bytes=contents,
        sha256=digest,
    )


def _build_game_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    game_inventory: VerifiedGameEvidenceInventory,
    timezone_gate: VerifiedTimezoneGate,
    schedule_inventory: VerifiedScheduleAnchorInventory,
) -> tuple[NormalizedSnapshot, ...]:
    """Reduce already loader-verified authorities without filesystem access.

    The later integration wrapper owns loading and final authority rebinds. This
    pure reducer only cross-checks the immutable values supplied by those loaders.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("normalized snapshot run_id differs")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError("normalized snapshot records must be a sequence")
    if not isinstance(game_inventory, VerifiedGameEvidenceInventory):
        raise TypeError("game inventory authority differs")
    if not isinstance(timezone_gate, VerifiedTimezoneGate):
        raise TypeError("timezone gate authority differs")
    if not isinstance(schedule_inventory, VerifiedScheduleAnchorInventory):
        raise TypeError("schedule inventory authority differs")

    by_season: dict[int, list[tuple[VerifiedQueryRecord, dict[str, object]]]] = {
        season: [] for season in _GAME_SEASONS
    }
    seen_query_ids: set[str] = set()
    for record in records:
        sidecar = _verified_sidecar(record)
        query = record.query
        if (
            query.source != "mlb_schedule_game_feed"
            or query.grain != "season_schedule_and_game"
        ):
            raise ValueError("game snapshot query source or grain differs")
        boxscore_match = _BOXSCORE_ENDPOINT.fullmatch(query.endpoint)
        if query.endpoint == _SCHEDULE_ENDPOINT:
            season = query.requested_range.get("year")
        elif boxscore_match is not None:
            season = query.requested_range.get("season")
            game_pk = query.requested_range.get("game_pk")
            if (
                isinstance(game_pk, bool)
                or not isinstance(game_pk, int)
                or game_pk != int(boxscore_match.group("game_pk"))
            ):
                raise ValueError("game snapshot boxscore gamePk differs")
        else:
            raise ValueError("game snapshot query endpoint differs")
        if isinstance(season, bool) or not isinstance(season, int) or season not in by_season:
            raise ValueError("game snapshot requires exact 2018-2024 seasons")
        if record.query_id in seen_query_ids:
            raise ValueError("game snapshot query population is duplicated")
        seen_query_ids.add(record.query_id)
        by_season[season].append((record, sidecar))

    schedule_sidecars: list[Mapping[str, object]] = []
    schedule_records: dict[int, VerifiedQueryRecord] = {}
    snapshot_inputs: dict[int, tuple[VerifiedQueryRecord, ...]] = {}
    snapshots: list[NormalizedSnapshot] = []
    payloads: list[Mapping[str, object]] = []
    for season in _GAME_SEASONS:
        season_records = by_season[season]
        anchors = [
            (record, sidecar)
            for record, sidecar in season_records
            if record.query.endpoint == _SCHEDULE_ENDPOINT
        ]
        if len(anchors) != 1:
            raise ValueError("game snapshot requires one schedule query per season")
        schedule_record, schedule_sidecar = anchors[0]
        schedule_records[season] = schedule_record
        schedule_sidecars.append(schedule_sidecar)
        boxscores: dict[int, tuple[bytes, str]] = {}
        for record, _sidecar in season_records:
            if record is schedule_record:
                continue
            game_pk = record.query.requested_range.get("game_pk")
            if (
                isinstance(game_pk, bool)
                or not isinstance(game_pk, int)
                or game_pk in boxscores
            ):
                raise ValueError("game snapshot boxscore population differs")
            boxscores[game_pk] = (
                record.query_sidecar_bytes,
                record.query_sidecar_sha256,
            )
        payload = dict(
            recompute_game_evidence_snapshot([schedule_sidecar], boxscores)
        )
        inputs = tuple(record for record, _sidecar in season_records)
        snapshot_inputs[season] = inputs
        payloads.append(payload)

    schedule_payload = schedule_inventory.payload
    if (
        schedule_payload.get("run_id") != run_id
        or timezone_gate.schedule_inventory_sha256 != schedule_inventory.sha256
    ):
        raise ValueError("schedule and timezone authority binding differs")
    season_sources = schedule_payload.get("season_sources")
    if isinstance(season_sources, (str, bytes)) or not isinstance(
        season_sources, Sequence
    ) or len(season_sources) != len(_GAME_SEASONS):
        raise ValueError("schedule inventory seven-season authority differs")
    for season, value in zip(_GAME_SEASONS, season_sources, strict=True):
        record = schedule_records[season]
        if (
            not isinstance(value, Mapping)
            or value.get("season") != season
            or value.get("query_id") != record.query_id
            or _binding_sha256(
                value.get("query_sidecar"),
                "schedule inventory query sidecar",
            )
            != record.query_sidecar_sha256
        ):
            raise ValueError("schedule inventory season source differs")

    expected_schedule_hashes = sorted(
        record.query_sidecar_sha256 for record in schedule_records.values()
    )
    candidate_schedule_hashes = timezone_gate.candidate.get(
        "schedule_sidecar_sha256s"
    )
    if list(candidate_schedule_hashes or ()) != expected_schedule_hashes:
        raise ValueError("timezone schedule sidecar authority differs")
    validate_timezone_mapping(timezone_gate.mapping, schedule_sidecars)

    game_payload = _plain_json(game_inventory.payload)
    if (
        not isinstance(game_payload, dict)
        or set(game_payload) != _GAME_INVENTORY_KEYS
        or (
        game_payload.get("artifact_kind") != "GAME_EVIDENCE_INVENTORY"
        or game_payload.get("schema_version") != 1
        or game_payload.get("run_id") != run_id
        )
    ):
        raise ValueError("game inventory identity differs")
    if (
        _binding_sha256(game_payload.get("run_spec"), "game RunSpec")
        != timezone_gate.run_spec_sha256
        or _binding_sha256(
            game_payload.get("acquisition_contract"),
            "game acquisition contract",
        )
        != timezone_gate.acquisition_contract_sha256
    ):
        raise ValueError("game inventory protocol authority differs")
    query_batches = game_payload.get("query_batches")
    if (
        not isinstance(query_batches, list)
        or len(query_batches) != 2
        or tuple(
            value.get("batch_id") if isinstance(value, Mapping) else None
            for value in query_batches
        )
        != timezone_gate.query_batch_ids
        or tuple(
            _binding_sha256(value, "game query batch")
            for value in query_batches
        )
        != timezone_gate.query_batch_sha256s
    ):
        raise ValueError("game inventory query batch authority differs")
    if _binding_sha256(
        game_payload.get("schedule_inventory"), "game schedule inventory"
    ) != schedule_inventory.sha256:
        raise ValueError("game schedule inventory authority differs")
    timezone_binding = game_payload.get("timezone_gate")
    if not isinstance(timezone_binding, Mapping):
        raise ValueError("game timezone authority differs")
    reviews = timezone_binding.get("reviews")
    if isinstance(reviews, (str, bytes)) or not isinstance(reviews, Sequence) or len(reviews) != 2:
        raise ValueError("game timezone review authority differs")
    if (
        _binding_sha256(timezone_binding.get("candidate"), "game timezone candidate")
        != timezone_gate.candidate_sha256
        or _binding_sha256(timezone_binding.get("mapping"), "game timezone mapping")
        != timezone_gate.mapping_sha256
        or tuple(
            _binding_sha256(value, "game timezone review") for value in reviews
        )
        != timezone_gate.review_sha256s
        or _binding_sha256(timezone_binding.get("seal"), "game timezone seal")
        != timezone_gate.seal_sha256
    ):
        raise ValueError("game timezone authority binding differs")

    expected_games = [
        _plain_json(game)
        for payload in payloads
        for game in payload["games"]  # type: ignore[index]
    ]
    expected_sidecar_hashes = sorted(
        {
            digest
            for payload in payloads
            for digest in payload["source_query_sidecar_sha256s"]  # type: ignore[index]
        }
    )
    played_games = [game for game in expected_games if game["played"] is True]
    starter_ids = {
        starter_id
        for game in played_games
        for starter_id in game["actual_starter_ids"]
    }
    expected_counts = {
        "game_count": len(expected_games),
        "played_game_count": len(played_games),
        "unique_actual_starter_count": len(starter_ids),
        "unplayed_game_count": len(expected_games) - len(played_games),
        "verified_feature_consumer_game_count": len(played_games),
    }
    counts = game_payload.get("counts")
    if (
        not isinstance(counts, Mapping)
        or set(counts) != _GAME_COUNT_KEYS
        or dict(counts) != expected_counts
        or game_payload.get("games") != expected_games
        or game_payload.get("source_query_sidecar_sha256s")
        != expected_sidecar_hashes
    ):
        raise ValueError("game inventory normalized population differs")

    for season, payload in zip(_GAME_SEASONS, payloads, strict=True):
        snapshots.append(
            _normalized_snapshot(run_id, season, snapshot_inputs[season], payload)
        )
    return tuple(snapshots)


def _statcast_normalized_snapshot(
    run_id: str,
    season: int,
    records: Sequence[VerifiedQueryRecord],
    receipt_sha256s: Sequence[str],
    payload: Mapping[str, object],
) -> NormalizedSnapshot:
    query_ids = tuple(sorted(record.query_id for record in records))
    sidecar_hashes = tuple(
        sorted(record.query_sidecar_sha256 for record in records)
    )
    receipts = tuple(receipt_sha256s)
    if (
        not query_ids
        or len(query_ids) != len(set(query_ids))
        or len(sidecar_hashes) != len(set(sidecar_hashes))
        or len(receipts) != len(set(receipts))
    ):
        raise ValueError("Statcast normalized snapshot identity is duplicated")
    envelope: dict[str, object] = {
        "grain": "pitch",
        "logical_query_ids": list(query_ids),
        "payload": _plain_json(payload),
        "run_id": run_id,
        "schema_version": 1,
        "season": season,
        "source": "baseball_savant_statcast",
        "source_query_sidecar_sha256s": list(sidecar_hashes),
        "split_receipt_sha256s": list(receipts),
        "status": "READY",
    }
    envelope = _validate_normalized_snapshot_mapping(envelope)
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    if digest in set(query_ids) | set(sidecar_hashes) | set(receipts):
        raise ValueError("normalized snapshot hash collides with a component hash")
    frozen_payload = _freeze_json(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("normalized snapshot payload must freeze to a mapping")
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=season,
        source="baseball_savant_statcast",
        grain="pitch",
        status="READY",
        logical_query_ids=query_ids,
        source_query_sidecar_sha256s=sidecar_hashes,
        split_receipt_sha256s=receipts,
        payload=frozen_payload,
        canonical_bytes=contents,
        sha256=digest,
    )


def _build_statcast_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    graph: _TerminalQueryGraph,
    game_snapshots: Sequence[NormalizedSnapshot],
) -> tuple[NormalizedSnapshot, ...]:
    """Build the 46 sealed monthly Statcast candidates without I/O."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("Statcast normalized snapshot run_id differs")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError("Statcast normalized snapshot records must be a sequence")
    if not isinstance(graph, _TerminalQueryGraph):
        raise TypeError("Statcast terminal query graph authority differs")
    if isinstance(game_snapshots, (str, bytes)) or not isinstance(
        game_snapshots, Sequence
    ):
        raise TypeError("Statcast game snapshots must be a sequence")

    root_batch = graph.statcast_root_batch
    roots = root_batch.queries
    expected_ranges = tuple(
        (season, first, last)
        for season in _GAME_SEASONS
        for first, last in monthly_ranges(*SEASON_BOUNDARIES[season])
    )
    if (
        root_batch.stage != "REMAINING"
        or root_batch.derived_from_sha256 is not None
        or len(roots) != 46
        or len(expected_ranges) != 46
        or _make_batch("REMAINING", None, roots) != root_batch
        or len(
            {
                (
                    root.run_spec_sha256,
                    root.acquisition_contract_sha256,
                    root.parser_version,
                )
                for root in roots
            }
        )
        != 1
    ):
        raise ValueError("Statcast requires the canonical ordered 46-root batch")
    root_by_id: dict[str, LogicalQuery] = {}
    for root, (season, first, last) in zip(roots, expected_ranges, strict=True):
        actual_first, actual_last = validate_statcast_query(root)
        root_id = logical_query_id(root)
        if (
            root.parent_query_id is not None
            or set(root.requested_range) != {"year", "start_date", "end_date"}
            or root.requested_range.get("year") != season
            or (actual_first, actual_last) != (first, last)
            or root_id in root_by_id
        ):
            raise ValueError("Statcast monthly root authority differs")
        root_by_id[root_id] = root

    graph_query_by_id: dict[str, LogicalQuery] = {}
    query_batch_by_id: dict[str, QueryBatch] = {}
    for batch in graph.query_batches:
        for query in batch.queries:
            query_id = logical_query_id(query)
            if query_id in graph_query_by_id:
                raise ValueError("Statcast graph query population is duplicated")
            graph_query_by_id[query_id] = query
            query_batch_by_id[query_id] = batch
    statcast_graph = {
        query_id: query
        for query_id, query in graph_query_by_id.items()
        if query.source == "baseball_savant_statcast" or query.grain == "pitch"
    }
    if any(
        query.source != "baseball_savant_statcast" or query.grain != "pitch"
        for query in statcast_graph.values()
    ):
        raise ValueError("Statcast graph source or grain differs")
    completed = {
        logical_query_id(query): query
        for query in graph.completed_queries
        if query.source == "baseball_savant_statcast" or query.grain == "pitch"
    }
    split_parents = {
        logical_query_id(query): query for query in graph.split_parent_queries
    }
    if (
        len(completed) != sum(
            query.source == "baseball_savant_statcast"
            and query.grain == "pitch"
            for query in graph.completed_queries
        )
        or any(
            query.source != "baseball_savant_statcast" or query.grain != "pitch"
            for query in split_parents.values()
        )
        or set(completed) & set(split_parents)
        or set(statcast_graph) != set(completed) | set(split_parents)
    ):
        raise ValueError("Statcast terminal leaf/control population differs")

    receipt_by_parent: dict[str, SplitReceipt] = {}
    batch_by_id = {batch.batch_id: batch for batch in graph.query_batches}
    for receipt in graph.split_receipts:
        if receipt.parent_query_id not in split_parents:
            raise ValueError("Statcast split receipt has an orphan parent")
        if (
            receipt.parent_query_id in receipt_by_parent
            or _SHA256.fullmatch(receipt.receipt_sha256) is None
            or receipt.stage != "REMAINING"
        ):
            raise ValueError("Statcast split receipt binding differs")
        child_batch = batch_by_id.get(receipt.child_batch_id)
        parent_batch = query_batch_by_id.get(receipt.parent_query_id)
        if (
            child_batch is None
            or parent_batch is None
            or receipt.parent_batch_id != parent_batch.batch_id
            or child_batch.derived_from_sha256 != parent_batch.batch_id
            or tuple(logical_query_id(query) for query in child_batch.queries)
            != receipt.child_query_ids
            or any(
                query.parent_query_id != receipt.parent_query_id
                for query in child_batch.queries
            )
        ):
            raise ValueError("Statcast split receipt child binding differs")
        receipt_by_parent[receipt.parent_query_id] = receipt
    if set(receipt_by_parent) != set(split_parents):
        raise ValueError("Statcast split receipt population differs")

    children_by_parent: dict[str, list[LogicalQuery]] = {}
    for query_id, query in statcast_graph.items():
        validate_statcast_query(query)
        if query.requested_range.get("acquisition") not in {
            "FETCH_REQUIRED",
            "PROTECTED_LEGACY_REFERENCE",
        }:
            raise ValueError("Statcast partition acquisition disposition differs")
        parent_id = query.parent_query_id
        if parent_id not in root_by_id and parent_id not in split_parents:
            raise ValueError("Statcast query is orphaned from the monthly roots")
        children_by_parent.setdefault(str(parent_id), []).append(query)
        ancestor = root_by_id.get(str(parent_id)) or split_parents.get(str(parent_id))
        if ancestor is None or any(
            getattr(query, field) != getattr(ancestor, field)
            for field in (
                "source",
                "grain",
                "endpoint",
                "expected_media_type",
                "parser_version",
                "run_spec_sha256",
                "acquisition_contract_sha256",
            )
        ):
            raise ValueError("Statcast descendant contract differs from its parent")
        if query_id in completed and query_id in children_by_parent:
            raise ValueError("Statcast completed query cannot own children")

    for parent_id, parent in (*root_by_id.items(), *split_parents.items()):
        parent_first, parent_last = validate_statcast_query(parent)
        children = sorted(
            children_by_parent.get(parent_id, ()),
            key=lambda query: validate_statcast_query(query),
        )
        validate_statcast_child_coverage(
            {
                "inclusive_start": parent_first.isoformat(),
                "inclusive_end": parent_last.isoformat(),
            },
            [
                {
                    "inclusive_start": validate_statcast_query(query)[0].isoformat(),
                    "inclusive_end": validate_statcast_query(query)[1].isoformat(),
                }
                for query in children
            ],
        )
        if parent_id in split_parents:
            receipt = receipt_by_parent[parent_id]
            ordered = tuple(
                graph_query_by_id[query_id] for query_id in receipt.child_query_ids
            )
            if ordered != bisect_statcast_query(parent):
                raise ValueError("Statcast split children differ from canonical bisection")

    root_for_query: dict[str, str] = {}
    for query_id, query in statcast_graph.items():
        seen = {query_id}
        parent_id = query.parent_query_id
        while parent_id not in root_by_id:
            if parent_id in seen or parent_id not in split_parents:
                raise ValueError("Statcast query ancestry is cyclic or orphaned")
            seen.add(str(parent_id))
            parent_id = split_parents[str(parent_id)].parent_query_id
        root_for_query[query_id] = str(parent_id)

    record_by_id: dict[str, tuple[VerifiedQueryRecord, dict[str, object]]] = {}
    for record in records:
        sidecar = _verified_sidecar(record)
        query_id = record.query_id
        if (
            record.query.source != "baseball_savant_statcast"
            or record.query.grain != "pitch"
            or query_id not in completed
            or completed[query_id] != record.query
            or query_id in record_by_id
        ):
            raise ValueError("Statcast verified record population differs")
        semantics = sidecar.get("semantics")
        first, last = validate_statcast_query(record.query)
        if (
            not isinstance(semantics, Mapping)
            or semantics.get("split_disposition") != "READY"
            or semantics.get("coverage")
            != {
                "inclusive_start": first.isoformat(),
                "inclusive_end": last.isoformat(),
            }
            or semantics.get("canonical_query")
            != json.loads(canonical_query_bytes(record.query))
        ):
            raise ValueError("Statcast READY leaf sidecar binding differs")
        record_by_id[query_id] = (record, sidecar)
    if set(record_by_id) != set(completed):
        raise ValueError("Statcast READY leaf record coverage differs")

    game_by_season: dict[int, NormalizedSnapshot] = {}
    played_by_root: dict[str, list[int]] = {root_id: [] for root_id in root_by_id}
    seen_game_pks: set[int] = set()
    for snapshot in game_snapshots:
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.run_id != run_id
            or snapshot.source != "mlb_schedule_game_feed"
            or snapshot.grain != "season_game_evidence_snapshot"
            or snapshot.season in game_by_season
            or _validate_normalized_snapshot_mapping(snapshot.as_mapping())
            != snapshot.as_mapping()
            or snapshot.canonical_bytes
            != canonical_json_bytes(snapshot.as_mapping()) + b"\n"
            or snapshot.sha256
            != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
        ):
            raise ValueError("Statcast game snapshot authority differs")
        game_by_season[snapshot.season] = snapshot
        for game in snapshot.payload["games"]:  # type: ignore[index]
            if game["played"] is not True:  # type: ignore[index]
                continue
            game_pk = game["game_pk"]  # type: ignore[index]
            official = _canonical_day(
                game["official_date"], "Statcast played game official_date"  # type: ignore[index]
            )
            matches = [
                root_id
                for root_id, root in root_by_id.items()
                if validate_statcast_query(root)[0]
                <= official
                <= validate_statcast_query(root)[1]
            ]
            if (
                type(game_pk) is not int
                or game_pk <= 0
                or game_pk in seen_game_pks
                or len(matches) != 1
            ):
                raise ValueError("played game must map to exactly one Statcast root")
            seen_game_pks.add(game_pk)
            played_by_root[matches[0]].append(game_pk)
    if set(game_by_season) != set(_GAME_SEASONS):
        raise ValueError("Statcast requires seven verified game snapshots")

    snapshots: list[NormalizedSnapshot] = []
    for root in roots:
        root_id = logical_query_id(root)
        leaves = sorted(
            (
                record_by_id[query_id]
                for query_id in completed
                if root_for_query[query_id] == root_id
            ),
            key=lambda item: validate_statcast_query(item[0].query),
        )
        receipt_hashes = tuple(
            receipt_by_parent[parent_id].receipt_sha256
            for parent_id in sorted(
                parent_id
                for parent_id in split_parents
                if root_for_query[parent_id] == root_id
            )
        )
        first, last = validate_statcast_query(root)
        payload = recompute_statcast_season_snapshot(
            {
                "inclusive_start": first.isoformat(),
                "inclusive_end": last.isoformat(),
            },
            [sidecar for _record, sidecar in leaves],
            sorted(played_by_root[root_id]),
        )
        snapshots.append(
            _statcast_normalized_snapshot(
                run_id,
                int(root.requested_range["year"]),
                [record for record, _sidecar in leaves],
                receipt_hashes,
                payload,
            )
        )
    return tuple(snapshots)


def _pitching_normalized_snapshot(
    run_id: str,
    season: int,
    records: Sequence[VerifiedQueryRecord],
    payload: Mapping[str, object],
) -> NormalizedSnapshot:
    query_ids = tuple(sorted(record.query_id for record in records))
    sidecar_hashes = tuple(
        sorted(record.query_sidecar_sha256 for record in records)
    )
    if (
        not query_ids
        or len(query_ids) != len(set(query_ids))
        or len(sidecar_hashes) != len(set(sidecar_hashes))
    ):
        raise ValueError("pitching normalized snapshot identity is duplicated")
    envelope: dict[str, object] = {
        "grain": "season_d1_league_pitching_reference",
        "logical_query_ids": list(query_ids),
        "payload": _plain_json(payload),
        "run_id": run_id,
        "schema_version": 1,
        "season": season,
        "source": "mlb_stats_pitching",
        "source_query_sidecar_sha256s": list(sidecar_hashes),
        "split_receipt_sha256s": [],
        "status": "READY",
    }
    envelope = _validate_normalized_snapshot_mapping(envelope)
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    if digest in set(query_ids) | set(sidecar_hashes):
        raise ValueError("normalized snapshot hash collides with a component hash")
    frozen_payload = _freeze_json(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("normalized snapshot payload must freeze to a mapping")
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=season,
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        status="READY",
        logical_query_ids=query_ids,
        source_query_sidecar_sha256s=sidecar_hashes,
        split_receipt_sha256s=(),
        payload=frozen_payload,
        canonical_bytes=contents,
        sha256=digest,
    )


def _build_pitching_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    graph: _TerminalQueryGraph,
    game_snapshots: Sequence[NormalizedSnapshot],
) -> tuple[NormalizedSnapshot, ...]:
    """Build seven immutable D-1 pitching snapshots without I/O."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("pitching normalized snapshot run_id differs")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError("pitching normalized snapshot records must be a sequence")
    if not isinstance(graph, _TerminalQueryGraph):
        raise TypeError("pitching terminal query graph authority differs")
    if isinstance(game_snapshots, (str, bytes)) or not isinstance(
        game_snapshots, Sequence
    ):
        raise TypeError("pitching game snapshots must be a sequence")

    def pitching_projection(
        queries: Sequence[LogicalQuery], label: str
    ) -> dict[str, LogicalQuery]:
        projection: dict[str, LogicalQuery] = {}
        for query in queries:
            relevant = query.source == "mlb_stats_pitching" or query.grain == (
                "season_d1_league_pitching_reference"
            )
            if not relevant:
                continue
            if (
                query.source != "mlb_stats_pitching"
                or query.grain != "season_d1_league_pitching_reference"
            ):
                raise ValueError(f"pitching {label} source or grain differs")
            query_id = logical_query_id(query)
            if query_id in projection:
                raise ValueError(f"pitching {label} population is duplicated")
            projection[query_id] = query
        return projection

    expected_by_id = pitching_projection(
        graph.completed_queries, "completed-query"
    )
    if not expected_by_id:
        raise ValueError("pitching terminal graph population is empty")

    root_population = pitching_projection(graph.root_queries, "root-query")
    split_parent_population = pitching_projection(
        graph.split_parent_queries, "split-parent"
    )
    split_leaf_population = pitching_projection(
        graph.split_leaf_queries, "split-leaf"
    )
    if root_population != expected_by_id:
        raise ValueError("pitching root/completed query population differs")
    if split_parent_population or split_leaf_population:
        raise ValueError("pitching queries cannot use split graph classifications")
    pitching_query_ids = set(expected_by_id)
    if any(
        receipt.parent_query_id in pitching_query_ids
        or any(child_id in pitching_query_ids for child_id in receipt.child_query_ids)
        for receipt in graph.split_receipts
    ):
        raise ValueError("pitching query IDs cannot appear in split receipts")

    batch_population = pitching_projection(
        tuple(query for batch in graph.query_batches for query in batch.queries),
        "query-batch",
    )
    if batch_population != expected_by_id:
        raise ValueError("pitching terminal query batch population differs")

    game_by_season: dict[int, NormalizedSnapshot] = {}
    expected_consumers: dict[tuple[int, date], list[int]] = {}
    seen_game_pks: set[int] = set()
    for snapshot in game_snapshots:
        mapping = snapshot.as_mapping() if isinstance(snapshot, NormalizedSnapshot) else None
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.run_id != run_id
            or snapshot.source != "mlb_schedule_game_feed"
            or snapshot.grain != "season_game_evidence_snapshot"
            or snapshot.season in game_by_season
            or mapping is None
            or _validate_normalized_snapshot_mapping(mapping) != mapping
            or snapshot.canonical_bytes != canonical_json_bytes(mapping) + b"\n"
            or snapshot.sha256
            != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
        ):
            raise ValueError("pitching game snapshot authority differs")
        game_by_season[snapshot.season] = snapshot
        for game in mapping["payload"]["games"]:  # type: ignore[index]
            game_pk = game["game_pk"]
            if type(game_pk) is not int or game_pk in seen_game_pks:
                raise ValueError("pitching game population is duplicated")
            seen_game_pks.add(game_pk)
            if game["played"] is not True:
                continue
            official = _canonical_day(
                game["official_date"], "pitching consumer official date"
            )
            cutoff = official - timedelta(days=1)
            key = (snapshot.season, cutoff)
            expected_consumers.setdefault(key, []).append(game_pk)
    if set(game_by_season) != set(_GAME_SEASONS):
        raise ValueError("pitching requires seven verified game snapshots")
    expected_consumers = {
        key: sorted(consumers) for key, consumers in expected_consumers.items()
    }
    if {season for season, _cutoff in expected_consumers} != set(_GAME_SEASONS):
        raise ValueError("pitching requires played consumers in every season")

    record_by_id: dict[str, tuple[VerifiedQueryRecord, dict[str, object]]] = {}
    grouped: dict[int, list[tuple[date, VerifiedQueryRecord, dict[str, object]]]] = {
        season: [] for season in _GAME_SEASONS
    }
    seen_keys: set[tuple[int, date]] = set()
    for record in records:
        sidecar = _verified_pitching_sidecar(record)
        query = record.query
        query_id = record.query_id
        if (
            query.source != "mlb_stats_pitching"
            or query.grain != "season_d1_league_pitching_reference"
            or query_id not in expected_by_id
            or expected_by_id[query_id] != query
            or query_id in record_by_id
            or query.parent_query_id is not None
            or query.expected_media_type != "application/json"
            or query.parser_version != "v5.2.1"
            or (
                query.endpoint
                == "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
            )
            != (record.storage_mode == "DERIVED_DISPOSITION")
        ):
            raise ValueError("pitching verified record population differs")
        requested = dict(query.requested_range)
        season = requested.get("season")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in grouped
        ):
            raise ValueError("pitching query season differs")
        cutoff = _canonical_day(requested.get("as_of_date"), "pitching query cutoff")
        key = (season, cutoff)
        consumers = expected_consumers.get(key)
        if consumers is None or key in seen_keys:
            raise ValueError("pitching query consumer partition differs")
        cutoffs = {
            str(game_pk): cutoff.isoformat() for game_pk in consumers
        }
        season_open = SEASON_BOUNDARIES[season][0]
        if cutoff < season_open:
            expected_endpoint = (
                "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
            )
            expected_parameters: dict[str, object] = {}
            expected_requested = {
                "season": season,
                "as_of_date": cutoff.isoformat(),
                "consumer_cutoff_dates": cutoffs,
                "consuming_game_pks": consumers,
                "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
                "reason": "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
            }
            if cutoff != season_open - timedelta(days=1):
                raise ValueError("pitching preseason cutoff differs")
        else:
            expected_endpoint = "https://statsapi.mlb.com/api/v1/stats"
            expected_parameters = {
                "stats": "byDateRange",
                "group": "pitching",
                "sportIds": 1,
                "gameType": "R",
                "playerPool": "ALL",
                "limit": 5000,
                "startDate": season_open.isoformat(),
                "endDate": cutoff.isoformat(),
            }
            expected_requested = {
                "season": season,
                "start_date": season_open.isoformat(),
                "as_of_date": cutoff.isoformat(),
                "consumer_cutoff_dates": cutoffs,
                "consuming_game_pks": consumers,
                "response_completeness": {
                    "limit": 5000,
                    "reject_total_splits_at_or_above_limit": True,
                },
            }
        semantics = sidecar.get("semantics")
        if (
            query.endpoint != expected_endpoint
            or dict(query.parameters) != expected_parameters
            or requested != expected_requested
            or not isinstance(semantics, Mapping)
            or semantics.get("season") != season
            or semantics.get("as_of_date") != cutoff.isoformat()
            or semantics.get("consuming_game_pks") != consumers
            or semantics.get("consumer_cutoff_dates") != cutoffs
        ):
            raise ValueError("pitching D-1 query or sidecar binding differs")
        record_by_id[query_id] = (record, sidecar)
        grouped[season].append((cutoff, record, sidecar))
        seen_keys.add(key)
    if set(record_by_id) != set(expected_by_id) or seen_keys != set(
        expected_consumers
    ):
        raise ValueError("pitching READY record coverage differs")

    snapshots: list[NormalizedSnapshot] = []
    for season in _GAME_SEASONS:
        ordered = sorted(grouped[season], key=lambda item: item[0])
        payload = recompute_pitching_season_snapshot(
            [sidecar for _cutoff, _record, sidecar in ordered]
        )
        snapshots.append(
            _pitching_normalized_snapshot(
                run_id,
                season,
                [record for _cutoff, record, _sidecar in ordered],
                payload,
            )
        )
    return tuple(snapshots)


def _read_verified_query_record(
    store: V52EvidenceStore,
    query: LogicalQuery,
) -> VerifiedQueryRecord:
    if not isinstance(store, V52EvidenceStore) or not store._read_only:
        raise ValueError("source verification requires a read-only evidence store")
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")

    result: VerifiedQueryRecord | None = None

    def recompute_verified_read(verified: VerifiedEvidenceRead) -> None:
        nonlocal result
        published = verified.published_evidence
        query_id = logical_query_id(query)
        if verified.registered_query != query or published.query_id != query_id:
            raise ValueError("verified query identity binding differs")
        if verified.success_receipt.get("storage_mode") != published.storage_mode:
            raise ValueError("verified storage mode binding differs")
        retained_raw: bytes | None = None
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
            raw_sha256 = None
        else:
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
            if _retain_raw_for_aggregation(query):
                retained_raw = raw

        sidecar_bytes = canonical_json_bytes(expected_sidecar) + b"\n"
        sidecar_sha256 = hashlib.sha256(sidecar_bytes).hexdigest()
        if published.query_sidecar_sha256 != sidecar_sha256:
            raise ValueError("verified query sidecar hash binding differs")
        result = VerifiedQueryRecord(
            query=query,
            query_id=query_id,
            query_sidecar_bytes=sidecar_bytes,
            query_sidecar_sha256=sidecar_sha256,
            raw_sha256=raw_sha256,
            storage_mode=published.storage_mode,
            retained_raw_bytes=retained_raw,
        )

    store.inspect_verified_success(query, recompute_verified_read)
    if result is None:
        raise AssertionError("verified success inspection did not produce a result")
    return result


def _read_and_recompute_query(
    store: V52EvidenceStore,
    query: LogicalQuery,
) -> dict[str, object]:
    record = _read_verified_query_record(store, query)
    sidecar = json.loads(record.query_sidecar_bytes)
    if not isinstance(sidecar, dict):
        raise AssertionError("verified query sidecar is not an object")
    return {
        "query_id": record.query_id,
        "query_sidecar": sidecar,
        "raw_sha256": record.raw_sha256,
        "storage_mode": record.storage_mode,
    }
