from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .execution_registry import (
    AcquisitionCheckpoint,
    load_source_verification_checkpoint,
)
from .v52_game_evidence_inventory import (
    VerifiedGameEvidenceInventory,
    _replace_source_disposition_lineage,
    load_verified_game_evidence_inventory,
    validate_game_evidence_snapshot,
)
from .v52_schedule_inventory import (
    VerifiedScheduleAnchorInventory,
    build_schedule_anchor_provenance,
    load_verified_schedule_anchor_inventory,
)
from .v52_sources.schedule import (
    derived_schedule_sidecar_adapter,
    recompute_game_evidence_snapshot,
)
from .v52_timezone_review import (
    VerifiedTimezoneGate,
    load_verified_timezone_gate,
    validate_timezone_mapping,
)
from .protocols import RunSpec, SourceRunSpec

from .v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    VerifiedEvidenceRead,
    canonical_query_bytes,
    logical_query_id,
)
from .v52_duplicate_gamepk_disposition import (
    DuplicateGamePkDispositionAuthority,
    build_duplicate_disposition_authority,
)
from .v52_query_plan import (
    QueryBatch,
    SourceAuthorityContext,
    SplitReceipt,
    _SOURCE_RUN_SPEC_RELATIVE,
    _RUN_SPEC_RELATIVE,
    _load_query_batch_for_authority,
    _load_static_contracts,
    _make_batch,
    bisect_statcast_query,
    load_query_batch,
    load_split_receipt,
    validate_statcast_query,
)
from .v52_seasons import SEASON_BOUNDARIES, monthly_ranges
from .v52_source_contracts import (
    canonical_json_bytes,
    canonical_sha256,
    query_sidecar,
    recompute_query_sidecar,
)
from .v52_sources.history import recompute_prior_history_sidecar
from .v52_sources.statcast import (
    recompute_statcast_season_snapshot,
    validate_statcast_child_coverage,
)
from .v52_sources.pitching import (
    recompute_career_bf_snapshot,
    recompute_pitching_season_snapshot,
)
from .v52_sources.references import recompute_reference_sidecar
from .v52_timezone_review import (
    _candidate_object as _timezone_candidate_object,
    _mapping_from_candidate as _timezone_mapping_from_candidate,
    _review_object as _timezone_review_object,
    _seal_object as _timezone_seal_object,
)


_STATCAST_ROOT_BATCH_PATH = re.compile(
    r"^audit/v5_2_qd/historical(?:_superseding_565534)?/acquisition/query_batches/"
    r"(?P<batch_id>[0-9a-f]{64})\.json$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_BOXSCORE_ENDPOINT = re.compile(
    r"^https://statsapi\.mlb\.com/api/v1/game/(?P<game_pk>[1-9][0-9]*)/boxscore$"
)
_GAME_SEASONS = tuple(range(2018, 2025))
_SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
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
_HISTORY_PAYLOAD_KEYS = frozenset(
    {
        "games",
        "grain",
        "schema_version",
        "source_game_sha256s",
        "source_snapshot_sha256",
    }
)
_HISTORY_GAME_KEYS = frozenset(
    {
        "away_team_id",
        "completed_innings",
        "doubleheader_order",
        "game_pk",
        "history_cutoff",
        "home_team_id",
        "official_date",
        "projection_sha256",
        "scheduled_innings",
        "scheduled_start_utc",
        "source_game_sha256",
        "terminal_status",
        "venue_id",
    }
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
_CAREER_PAYLOAD_KEYS = frozenset({"grain", "records", "schema_version"})
_CAREER_RECORD_KEYS = frozenset(
    {
        "baseline_batters_faced",
        "baseline_inclusive_end_date",
        "baseline_query_sidecar_sha256",
        "consumer_cutoff_date",
        "game_pk",
        "pitcher_id",
        "statcast_ready_leaf_sidecar_sha256s",
        "statcast_split_receipt_sha256s",
        "total_batters_faced",
        "verified_statcast_bf",
    }
)
_REFERENCE_PAYLOAD_KEYS = frozenset(
    {
        "grain",
        "observed_venue_date_mappings",
        "schema_version",
        "team_payload_sha256s",
        "teams",
        "timezone_rows",
        "venue_payload_sha256s",
        "venues",
    }
)
_PITCHING_RECORD_COMMON_KEYS = frozenset(
    {
        "as_of_date",
        "consumer_cutoff_dates",
        "consuming_game_pks",
        "query_sidecar_sha256",
    }
)
_PITCHING_RECORD_NUMERIC_KEYS = frozenset({"batters_faced", "earned_runs", "outs"})
_OLD_GAME_INVENTORY_KEYS = frozenset(
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
_SOURCE_GAME_INVENTORY_KEYS = _OLD_GAME_INVENTORY_KEYS | {
    "duplicate_gamepk_disposition", "dispositioned_query_ids"
}
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
class AuthorityBinding:
    """A closed path/digest binding shared by replay and source inventory."""

    path: str
    sha256: str

    def as_mapping(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class DuplicateGamePkDispositionBinding:
    """The sole superseding-run schedule disposition authority."""

    query_id: str
    success_receipt: AuthorityBinding
    query_sidecar: AuthorityBinding

    def as_mapping(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "success_receipt": self.success_receipt.as_mapping(),
            "query_sidecar": self.query_sidecar.as_mapping(),
        }


@dataclass(frozen=True)
class NormalizedSnapshot:
    schema_version: Literal[1]
    run_id: str
    season: int
    source: Literal[
        "baseball_savant_statcast",
        "mlb_schedule_game_feed",
        "mlb_stats_pitcher_bf",
        "mlb_stats_pitching",
        "prior_schedule_history",
        "venue_team_timezone_table",
    ]
    grain: Literal[
        "pitch",
        "player_career_batters_faced_reference",
        "season_d1_league_pitching_reference",
        "season_game_evidence_snapshot",
        "verified_game_projection",
        "effective_dated_venue_team_reference",
    ]
    status: Literal["READY"]
    logical_query_ids: tuple[str, ...]
    source_query_sidecar_sha256s: tuple[str, ...]
    split_receipt_sha256s: tuple[str, ...]
    payload: Mapping[str, object]
    canonical_bytes: bytes
    sha256: str
    duplicate_gamepk_disposition: DuplicateGamePkDispositionBinding | None = None
    dispositioned_query_ids: tuple[str, ...] = ()

    def as_mapping(self) -> dict[str, object]:
        return {
            "grain": self.grain,
            "logical_query_ids": list(self.logical_query_ids),
            "payload": _plain_json(self.payload),
            "run_id": self.run_id,
            "schema_version": self.schema_version,
            "season": self.season,
            "source": self.source,
            "source_query_sidecar_sha256s": list(self.source_query_sidecar_sha256s),
            "split_receipt_sha256s": list(self.split_receipt_sha256s),
            "status": self.status,
            **(
                {
                    "duplicate_gamepk_disposition": self.duplicate_gamepk_disposition.as_mapping(),
                    "dispositioned_query_ids": list(self.dispositioned_query_ids),
                }
                if self.duplicate_gamepk_disposition is not None
                else {}
            ),
        }


@dataclass(frozen=True)
class SnapshotReplayInputs:
    """Immutable authorities required to replay the complete source set."""

    run_id: str
    graph: _TerminalQueryGraph
    records: tuple[VerifiedQueryRecord, ...]
    game_snapshots: tuple[NormalizedSnapshot, ...]
    game_inventory: VerifiedGameEvidenceInventory
    timezone_gate: VerifiedTimezoneGate
    schedule_inventory: VerifiedScheduleAnchorInventory
    duplicate_gamepk_disposition: DuplicateGamePkDispositionBinding | None = None
    dispositioned_query_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "dispositioned_query_ids", tuple(self.dispositioned_query_ids))

    @property
    def game_evidence_inventory(self) -> VerifiedGameEvidenceInventory:
        """Backward-compatible descriptive alias for the game authority."""
        return self.game_inventory


def snapshot_sort_key(snapshot: NormalizedSnapshot) -> tuple[object, ...]:
    """Return the canonical ordering key for a normalized snapshot."""
    coverage_start = ""
    coverage_end = ""
    if snapshot.source == "baseball_savant_statcast":
        coverage = snapshot.payload.get("parent_coverage")
        if isinstance(coverage, Mapping):
            coverage_start = coverage.get("inclusive_start", "")
            coverage_end = coverage.get("inclusive_end", "")
    return (
        snapshot.season,
        snapshot.source,
        snapshot.grain,
        coverage_start,
        coverage_end,
    )


def replay_verified_snapshots(inputs: SnapshotReplayInputs) -> tuple[NormalizedSnapshot, ...]:
    """Purely replay all six normalized snapshot families from sealed inputs."""
    if not isinstance(inputs, SnapshotReplayInputs):
        raise TypeError("snapshot replay inputs must be SnapshotReplayInputs")
    schedule = _build_game_normalized_snapshots(
        inputs.run_id,
        inputs.records,
        inputs.game_inventory,
        inputs.timezone_gate,
        inputs.schedule_inventory,
        duplicate_gamepk_disposition=inputs.duplicate_gamepk_disposition,
        dispositioned_query_ids=inputs.dispositioned_query_ids,
    )
    if schedule != inputs.game_snapshots:
        raise ValueError("upstream game snapshot authority differs")
    statcast = _build_statcast_normalized_snapshots(
        inputs.run_id, inputs.records, inputs.graph, schedule
    )
    pitching = _build_pitching_normalized_snapshots(
        inputs.run_id, inputs.records, inputs.graph, schedule
    )
    reference = _build_reference_normalized_snapshots(
        inputs.run_id,
        inputs.records,
        inputs.graph,
        schedule,
        inputs.timezone_gate,
    )
    history = _build_history_normalized_snapshots(inputs.run_id, schedule)
    career = _build_career_bf_normalized_snapshots(
        inputs.run_id, inputs.records, inputs.graph, schedule
    )
    snapshots = tuple(
        sorted(
            (*schedule, *statcast, *pitching, *reference, *history, *career),
            key=snapshot_sort_key,
        )
    )
    if (
        len(snapshots) != 81
        or len({snapshot.sha256 for snapshot in snapshots}) != 81
        or len({(snapshot.season, snapshot.source) for snapshot in snapshots}) != 42
    ):
        raise ValueError("replay cardinality must be 81 unique snapshots and 42 source-seasons")
    return snapshots


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
        not isinstance(item, str) or _SHA256.fullmatch(item) is None for item in value
    ) or value != sorted(set(value)):
        raise ValueError(f"normalized snapshot {label} differ")
    return value


def _validate_normalized_snapshot_mapping(
    value: object,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) not in {
        _NORMALIZED_ENVELOPE_KEYS,
        _NORMALIZED_ENVELOPE_KEYS
        | {"duplicate_gamepk_disposition", "dispositioned_query_ids"},
    }:
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
    query_ids = _sorted_unique_sha256s(envelope.get("logical_query_ids"), "query IDs")
    top_sidecars = _sorted_unique_sha256s(
        envelope.get("source_query_sidecar_sha256s"), "sidecar hashes"
    )
    if len(query_ids) != len(top_sidecars):
        raise ValueError("normalized snapshot query/sidecar counts differ")

    source = envelope.get("source")
    grain = envelope.get("grain")
    disposition = envelope.get("duplicate_gamepk_disposition")
    disposition_ids = envelope.get("dispositioned_query_ids")
    if disposition is not None or disposition_ids is not None:
        if (
            envelope["run_id"] != _SOURCE_RUN_ID
            or season != 2019
            or source != "mlb_schedule_game_feed"
            or grain != "season_game_evidence_snapshot"
            or not isinstance(disposition, Mapping)
            or set(disposition) != {"query_id", "success_receipt", "query_sidecar"}
            or not isinstance(disposition.get("query_id"), str)
            or not isinstance(disposition_ids, list)
            or disposition_ids != [disposition["query_id"]]
        ):
            raise ValueError("normalized snapshot disposition binding differs")
        for binding in (disposition["success_receipt"], disposition["query_sidecar"]):
            if (
                not isinstance(binding, Mapping)
                or set(binding) != {"path", "sha256"}
                or not isinstance(binding.get("path"), str)
                or not isinstance(binding.get("sha256"), str)
                or _SHA256.fullmatch(binding["sha256"]) is None
            ):
                raise ValueError("normalized snapshot disposition binding differs")
    if source == "mlb_schedule_game_feed" and grain == "season_game_evidence_snapshot":
        if (
            envelope["run_id"] == _SOURCE_RUN_ID
            and season == 2019
            and (disposition is None or disposition_ids is None)
        ):
            raise ValueError("normalized source 2019 disposition binding differs")
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
            if envelope["run_id"] == _SOURCE_RUN_ID:
                played = game.get("verification_status") == "VERIFIED"
                provenance_fields = {
                    "raw_game_object_sha256",
                    "normalized_anchor_sha256",
                }
                if (played and not provenance_fields <= set(game)) or (
                    not played and provenance_fields & set(game)
                ):
                    raise ValueError("normalized source game provenance keys differ")
        validate_game_evidence_snapshot(payload, run_id=envelope["run_id"])
        return envelope

    if source == "prior_schedule_history" and grain == "verified_game_projection":
        split_hashes = _sorted_unique_sha256s(
            envelope.get("split_receipt_sha256s"),
            "split receipt hashes",
            allow_empty=True,
        )
        if split_hashes:
            raise ValueError("history split receipt hashes must be empty")
        payload_value = envelope.get("payload")
        if (
            not isinstance(payload_value, Mapping)
            or set(payload_value) != _HISTORY_PAYLOAD_KEYS
        ):
            raise ValueError("normalized snapshot history payload keys differ")
        payload = dict(payload_value)
        games = payload.get("games")
        source_snapshot_sha256 = payload.get("source_snapshot_sha256")
        source_hashes = payload.get("source_game_sha256s")
        if (
            payload.get("schema_version") != 1
            or payload.get("grain") != "verified_game_projection"
            or not isinstance(games, list)
            or not games
            or not isinstance(source_snapshot_sha256, str)
            or _SHA256.fullmatch(source_snapshot_sha256) is None
            or not isinstance(source_hashes, list)
            or any(
                not isinstance(item, str) or _SHA256.fullmatch(item) is None
                for item in source_hashes
            )
            or source_hashes != sorted(set(source_hashes))
        ):
            raise ValueError("normalized snapshot history source hashes differ")

        observed: list[tuple[date, int]] = []
        seen_game_pks: set[int] = set()
        projected_hashes: list[str] = []
        for row_value in games:
            if (
                not isinstance(row_value, Mapping)
                or set(row_value) != _HISTORY_GAME_KEYS
            ):
                raise ValueError("normalized snapshot history projection keys differ")
            row = dict(row_value)
            official = _canonical_day(row.get("official_date"), "history official date")
            cutoff = _canonical_day(row.get("history_cutoff"), "history cutoff")
            game_pk = row.get("game_pk")
            if (
                type(game_pk) is not int
                or game_pk <= 0
                or game_pk in seen_game_pks
                or not SEASON_BOUNDARIES[season][0]
                <= official
                <= SEASON_BOUNDARIES[season][1]
            ):
                raise ValueError("normalized snapshot history gamePk or season differs")
            if cutoff != official - timedelta(days=1):
                raise ValueError("normalized snapshot history D-1 cutoff differs")
            if any(
                type(row.get(field)) is not int or row[field] <= 0
                for field in (
                    "away_team_id",
                    "doubleheader_order",
                    "home_team_id",
                    "venue_id",
                )
            ):
                raise ValueError(
                    "normalized snapshot history positive identity differs"
                )
            if any(
                value is not None and (type(value) is not int or value < 0)
                for value in (
                    row.get("completed_innings"),
                    row.get("scheduled_innings"),
                )
            ):
                raise ValueError("normalized snapshot history innings differ")
            scheduled_start = row.get("scheduled_start_utc")
            if not isinstance(scheduled_start, str) or not scheduled_start.endswith(
                "Z"
            ):
                raise ValueError("normalized snapshot history scheduled start differs")
            try:
                datetime.fromisoformat(scheduled_start[:-1] + "+00:00")
            except ValueError as exc:
                raise ValueError(
                    "normalized snapshot history scheduled start differs"
                ) from exc
            if (
                not isinstance(row.get("terminal_status"), str)
                or not row["terminal_status"]
            ):
                raise ValueError("normalized snapshot history terminal status differs")
            projection_sha256 = row.get("projection_sha256")
            projected = dict(row)
            projected.pop("projection_sha256")
            if projection_sha256 != canonical_sha256(projected):
                raise ValueError("normalized snapshot history projection hash differs")
            source_game_sha256 = row.get("source_game_sha256")
            if (
                not isinstance(source_game_sha256, str)
                or _SHA256.fullmatch(source_game_sha256) is None
            ):
                raise ValueError("normalized snapshot history source hashes differ")
            observed.append((official, game_pk))
            seen_game_pks.add(game_pk)
            projected_hashes.append(source_game_sha256)
        if observed != sorted(set(observed)):
            raise ValueError("normalized snapshot history game order or gamePk differs")
        if source_hashes != sorted(projected_hashes):
            raise ValueError("normalized snapshot history source hashes differ")
        return envelope

    if source == "mlb_stats_pitcher_bf" and grain == (
        "player_career_batters_faced_reference"
    ):
        split_hashes = _sorted_unique_sha256s(
            envelope.get("split_receipt_sha256s"),
            "split receipt hashes",
            allow_empty=True,
        )
        payload_value = envelope.get("payload")
        if (
            not isinstance(payload_value, Mapping)
            or set(payload_value) != _CAREER_PAYLOAD_KEYS
        ):
            raise ValueError("normalized snapshot career payload keys differ")
        payload = dict(payload_value)
        records = payload.get("records")
        if (
            payload.get("schema_version") != 1
            or payload.get("grain") != "player_career_batters_faced_reference"
            or not isinstance(records, list)
            or not records
        ):
            raise ValueError("normalized snapshot career payload identity differs")
        observed: list[tuple[int, int]] = []
        lineage_sidecars: set[str] = set()
        lineage_receipts: set[str] = set()
        for row_value in records:
            if (
                not isinstance(row_value, Mapping)
                or set(row_value) != _CAREER_RECORD_KEYS
            ):
                raise ValueError("normalized snapshot career record keys differ")
            row = dict(row_value)
            pitcher_id = row.get("pitcher_id")
            game_pk = row.get("game_pk")
            cutoff = _canonical_day(
                row.get("consumer_cutoff_date"), "career consumer cutoff"
            )
            baseline_end = _canonical_day(
                row.get("baseline_inclusive_end_date"), "career baseline cutoff"
            )
            baseline = row.get("baseline_batters_faced")
            verified = row.get("verified_statcast_bf")
            total = row.get("total_batters_faced")
            baseline_hash = row.get("baseline_query_sidecar_sha256")
            leaf_hashes = row.get("statcast_ready_leaf_sidecar_sha256s")
            receipt_hashes = row.get("statcast_split_receipt_sha256s")
            if (
                type(pitcher_id) is not int
                or pitcher_id <= 0
                or type(game_pk) is not int
                or game_pk <= 0
                or cutoff.year != season
                or baseline_end
                != (date(2017, 12, 31) if season <= 2021 else date(2021, 12, 31))
                or any(
                    type(value) is not int or value < 0
                    for value in (baseline, verified, total)
                )
                or total != baseline + verified
                or not isinstance(baseline_hash, str)
                or _SHA256.fullmatch(baseline_hash) is None
                or not isinstance(leaf_hashes, list)
                or any(
                    not isinstance(item, str) or _SHA256.fullmatch(item) is None
                    for item in leaf_hashes
                )
                or leaf_hashes != sorted(set(leaf_hashes))
                or not isinstance(receipt_hashes, list)
                or any(
                    not isinstance(item, str) or _SHA256.fullmatch(item) is None
                    for item in receipt_hashes
                )
                or receipt_hashes != sorted(set(receipt_hashes))
            ):
                raise ValueError("normalized snapshot career record identity differs")
            observed.append((pitcher_id, game_pk))
            lineage_sidecars.add(baseline_hash)
            lineage_sidecars.update(leaf_hashes)
            lineage_receipts.update(receipt_hashes)
        if observed != sorted(set(observed)):
            raise ValueError("normalized snapshot career record order differs")
        if top_sidecars != sorted(lineage_sidecars):
            raise ValueError("normalized snapshot career source lineage differs")
        if split_hashes != sorted(lineage_receipts):
            raise ValueError("normalized snapshot career receipt lineage differs")
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
            or payload.get("grain") != "season_d1_league_pitching_reference"
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
                or any(
                    type(game_pk) is not int or game_pk <= 0 for game_pk in consumers
                )
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
                    record.get("disposition") != "PRESEASON_NO_CURRENT_SEASON_PITCHING"
                    or as_of != SEASON_BOUNDARIES[season][0] - timedelta(days=1)
                ):
                    raise ValueError("normalized snapshot pitching disposition differs")
            elif as_of < SEASON_BOUNDARIES[season][0] or any(
                type(record.get(field)) is not int or record[field] < 0
                for field in _PITCHING_RECORD_NUMERIC_KEYS
            ):
                raise ValueError("normalized snapshot pitching metrics differ")
            prior_as_of = as_of
            seen_consumers.update(consumers)
            expected_hashes.append(digest)
        if payload_hashes != expected_hashes:
            raise ValueError("normalized snapshot pitching hash order differs")
        return envelope

    if source == "venue_team_timezone_table" and grain == (
        "effective_dated_venue_team_reference"
    ):
        split_hashes = _sorted_unique_sha256s(
            envelope.get("split_receipt_sha256s"),
            "split receipt hashes",
            allow_empty=True,
        )
        if split_hashes:
            raise ValueError("reference normalized snapshot cannot own split receipts")
        payload_value = envelope.get("payload")
        if (
            not isinstance(payload_value, Mapping)
            or set(payload_value) != _REFERENCE_PAYLOAD_KEYS
        ):
            raise ValueError("normalized snapshot reference payload keys differ")
        payload = dict(payload_value)
        teams = payload.get("teams")
        venues = payload.get("venues")
        timezone_rows = payload.get("timezone_rows")
        observations = payload.get("observed_venue_date_mappings")
        team_hashes = payload.get("team_payload_sha256s")
        venue_hashes = payload.get("venue_payload_sha256s")
        if (
            payload.get("schema_version") != 1
            or payload.get("grain") != "effective_dated_venue_team_reference"
            or not isinstance(team_hashes, list)
            or team_hashes != sorted(set(team_hashes))
            or len(team_hashes) != 1
            or any(
                not isinstance(item, str) or not _SHA256.fullmatch(item)
                for item in team_hashes
            )
            or not isinstance(venue_hashes, list)
            or venue_hashes != sorted(set(venue_hashes))
            or any(
                not isinstance(item, str) or not _SHA256.fullmatch(item)
                for item in venue_hashes
            )
            or not isinstance(teams, list)
            or not teams
            or not isinstance(venues, list)
            or not venues
            or len(query_ids) != 1 + len(venues)
            or len(venue_hashes) != len(venues)
            or not isinstance(timezone_rows, list)
            or not timezone_rows
            or not isinstance(observations, list)
            or not observations
        ):
            raise ValueError("normalized snapshot reference payload identity differs")

        team_ids: list[int] = []
        for row in teams:
            if not isinstance(row, Mapping) or set(row) != {
                "home_venue_id",
                "location_name",
                "team_id",
                "team_name",
            }:
                raise ValueError("normalized snapshot reference team differs")
            team_id = row.get("team_id")
            home_venue_id = row.get("home_venue_id")
            if (
                type(team_id) is not int
                or team_id <= 0
                or type(home_venue_id) is not int
                or home_venue_id <= 0
                or any(
                    not isinstance(row.get(field), str) or not row[field]
                    for field in ("location_name", "team_name")
                )
            ):
                raise ValueError("normalized snapshot reference team identity differs")
            team_ids.append(team_id)
        if team_ids != sorted(set(team_ids)):
            raise ValueError("normalized snapshot reference team order differs")

        venue_ids: list[int] = []
        for row in venues:
            if not isinstance(row, Mapping) or set(row) != {
                "city",
                "latitude",
                "longitude",
                "venue_id",
                "venue_name",
            }:
                raise ValueError("normalized snapshot reference venue differs")
            venue_id = row.get("venue_id")
            latitude = row.get("latitude")
            longitude = row.get("longitude")
            if (
                type(venue_id) is not int
                or venue_id <= 0
                or isinstance(latitude, bool)
                or not isinstance(latitude, (int, float))
                or not -90 <= latitude <= 90
                or isinstance(longitude, bool)
                or not isinstance(longitude, (int, float))
                or not -180 <= longitude <= 180
                or any(
                    not isinstance(row.get(field), str) or not row[field]
                    for field in ("city", "venue_name")
                )
            ):
                raise ValueError("normalized snapshot reference venue identity differs")
            venue_ids.append(venue_id)
        if venue_ids != sorted(set(venue_ids)):
            raise ValueError("normalized snapshot reference venue order differs")

        normalized_timezone_rows: list[tuple[int, date, date, str]] = []
        for row in timezone_rows:
            if not isinstance(row, Mapping) or set(row) != {
                "effective_end",
                "effective_start",
                "time_zone",
                "venue_id",
            }:
                raise ValueError("normalized snapshot reference timezone row differs")
            venue_id = row.get("venue_id")
            first = _canonical_day(
                row.get("effective_start"), "reference timezone effective start"
            )
            last = _canonical_day(
                row.get("effective_end"), "reference timezone effective end"
            )
            name = row.get("time_zone")
            if (
                type(venue_id) is not int
                or venue_id not in venue_ids
                or first > last
                or not isinstance(name, str)
                or not name
            ):
                raise ValueError(
                    "normalized snapshot reference timezone identity differs"
                )
            try:
                ZoneInfo(name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    "normalized snapshot reference timezone must be IANA"
                ) from exc
            normalized_timezone_rows.append((venue_id, first, last, name))
        if normalized_timezone_rows != sorted(set(normalized_timezone_rows)):
            raise ValueError("normalized snapshot reference timezone order differs")
        if {row[0] for row in normalized_timezone_rows} != set(venue_ids):
            raise ValueError("normalized snapshot reference timezone scope differs")
        by_venue: dict[int, list[tuple[int, date, date, str]]] = {}
        for row in normalized_timezone_rows:
            by_venue.setdefault(row[0], []).append(row)
        if any(
            current[1] <= previous[2]
            for rows in by_venue.values()
            for previous, current in zip(rows, rows[1:])
        ):
            raise ValueError("normalized snapshot reference timezone ranges overlap")

        normalized_observations: list[tuple[int, date, str, bool, bool]] = []
        used_timezone_rows: set[tuple[int, date, date, str]] = set()
        for row in observations:
            if not isinstance(row, Mapping) or set(row) != {
                "alternate_site",
                "neutral_site",
                "official_date",
                "time_zone",
                "venue_id",
            }:
                raise ValueError("normalized snapshot reference observation differs")
            venue_id = row.get("venue_id")
            official = _canonical_day(
                row.get("official_date"), "reference observation official date"
            )
            name = row.get("time_zone")
            neutral = row.get("neutral_site")
            alternate = row.get("alternate_site")
            if (
                type(venue_id) is not int
                or venue_id not in venue_ids
                or official.year != season
                or not isinstance(name, str)
                or type(neutral) is not bool
                or type(alternate) is not bool
            ):
                raise ValueError(
                    "normalized snapshot reference observation identity differs"
                )
            matches = [
                timezone_row
                for timezone_row in by_venue[venue_id]
                if timezone_row[1] <= official <= timezone_row[2]
                and timezone_row[3] == name
            ]
            if len(matches) != 1:
                raise ValueError(
                    "normalized snapshot reference observation mapping differs"
                )
            used_timezone_rows.add(matches[0])
            normalized_observations.append(
                (venue_id, official, name, neutral, alternate)
            )
        if normalized_observations != sorted(
            set(normalized_observations), key=lambda row: (row[0], row[1])
        ):
            raise ValueError("normalized snapshot reference observation order differs")
        if used_timezone_rows != set(normalized_timezone_rows):
            raise ValueError(
                "normalized snapshot reference contains unused timezone rows"
            )
        return envelope

    if source != "baseball_savant_statcast" or grain != "pitch":
        raise ValueError("normalized snapshot envelope identity differs")
    split_value = envelope.get("split_receipt_sha256s")
    if (
        not isinstance(split_value, list)
        or any(
            not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
            for digest in split_value
        )
        or len(split_value) != len(set(split_value))
    ):
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
    if [
        item.get("query_sidecar_sha256") for item in canonical_children
    ] != child_hashes:
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
    receipt_by_child = {receipt.child_batch_id: receipt for receipt in ordered_receipts}
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
        if receipt.child_query_ids != child_query_ids or any(
            query.parent_query_id != receipt.parent_query_id
            for query in child_batch.queries
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
    authority: RunSpec | SourceRunSpec | None = None,
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
    batch = (
        _load_query_batch_for_authority(root, batch_id, authority, read_only_progress=True)
        if isinstance(authority, SourceRunSpec)
        else load_query_batch(root, batch_id, read_only_progress=True)
    )
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
    run_spec: RunSpec | SourceRunSpec,
) -> _TerminalQueryGraph:
    raw_relative = _safe_raw_namespace(run_spec)
    checkpoint = load_source_verification_checkpoint(root, run_spec)
    statcast_root_batch = _load_statcast_root_batch(root, checkpoint, run_spec)
    batches = tuple(
        (
            _load_query_batch_for_authority(
                root, batch_id, run_spec, read_only_progress=True
            )
            if isinstance(run_spec, SourceRunSpec)
            else load_query_batch(root, batch_id, read_only_progress=True)
        )
        for batch_id in checkpoint.query_batch_ids
    )
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


def _safe_raw_namespace(run_spec: RunSpec) -> Path:
    """Return the RunSpec raw namespace after lexical path validation.

    The namespace is intentionally validated without resolving it.  The
    verified authority loaders own filesystem identity and symlink checks;
    this helper only prevents path traversal and platform separator tricks
    before constructing the evidence root.
    """
    namespaces = getattr(run_spec, "raw", None)
    namespaces = namespaces.get("namespaces") if isinstance(namespaces, Mapping) else None
    raw_namespace = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(raw_namespace, str) or not raw_namespace:
        raise ValueError("source verification raw namespace differs")
    if "\\" in raw_namespace:
        raise ValueError("source verification raw namespace must use slash separators")
    raw_path = Path(raw_namespace)
    if raw_path.is_absolute():
        raise ValueError("source verification raw namespace must be relative")
    parts = raw_namespace.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("source verification raw namespace contains unsafe segment")
    return raw_path


def _authenticate_authority_paths(
    root: Path,
    run_spec: RunSpec | SourceRunSpec,
    acquisition_contract_path: Path,
) -> SourceAuthorityContext:
    """Authenticate the exact authority pair before touching checkpoints.

    The superseding source authority is intentionally parsed here, but remains
    unusable for replay: checkpoint APIs are still RunSpec-only until Task 6.
    Keeping this authentication boundary ahead of the terminal graph load makes
    cross-pair and outside-path attempts fail before any checkpoint I/O.
    """
    root = Path(os.path.abspath(os.fspath(root)))
    if isinstance(run_spec, RunSpec):
        authority_relative = _RUN_SPEC_RELATIVE
    elif isinstance(run_spec, SourceRunSpec):
        authority_relative = _SOURCE_RUN_SPEC_RELATIVE
    else:
        raise TypeError("run_spec must be a RunSpec or SourceRunSpec")
    try:
        context = _load_static_contracts(
            root / authority_relative,
            Path(acquisition_contract_path),
        )
    except ValueError as exc:
        if isinstance(run_spec, SourceRunSpec):
            raise ValueError("source authority pair differs") from exc
        raise
    if context.repository_root != root or context.authority != run_spec:
        raise ValueError("authority object differs from authenticated repository bytes")
    return context


def _game_inventory_disposition_binding(
    game_inventory: VerifiedGameEvidenceInventory,
    run_id: str,
) -> tuple[DuplicateGamePkDispositionBinding | None, tuple[str, ...]]:
    """Return the sole source-run disposition capability sealed by the inventory."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("game inventory run_id differs")
    payload = getattr(game_inventory, "payload", None)
    if not isinstance(payload, Mapping):
        raise ValueError("game inventory run_id binding differs")
    if isinstance(game_inventory, VerifiedGameEvidenceInventory) and (
        payload.get("run_id") != run_id
    ):
        raise ValueError("game inventory run_id binding differs")
    binding_value = payload.get("duplicate_gamepk_disposition")
    ids_value = payload.get("dispositioned_query_ids")
    if run_id != _SOURCE_RUN_ID:
        if binding_value is not None or ids_value is not None:
            raise ValueError("non-source game inventory must omit disposition authority")
        return None, ()
    if (
        not isinstance(binding_value, Mapping)
        or set(binding_value) != {"query_id", "success_receipt", "query_sidecar"}
        or not isinstance(binding_value.get("query_id"), str)
        or _SHA256.fullmatch(binding_value["query_id"]) is None
        or not isinstance(ids_value, list)
        or ids_value != [binding_value["query_id"]]
    ):
        raise ValueError("source game inventory disposition authority differs")

    def binding(value: object, label: str) -> AuthorityBinding:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"path", "sha256"}
            or not isinstance(value.get("path"), str)
            or not isinstance(value.get("sha256"), str)
            or _SHA256.fullmatch(value["sha256"]) is None
        ):
            raise ValueError(f"source game inventory disposition {label} differs")
        return AuthorityBinding(value["path"], value["sha256"])

    return (
        DuplicateGamePkDispositionBinding(
            binding_value["query_id"],
            binding(binding_value["success_receipt"], "success receipt"),
            binding(binding_value["query_sidecar"], "query sidecar"),
        ),
        (binding_value["query_id"],),
    )


def load_snapshot_replay_inputs(
    root: Path,
    run_spec: RunSpec | SourceRunSpec,
    *,
    acquisition_contract_path: Path,
) -> SnapshotReplayInputs:
    """Load immutable, verified authorities needed for source replay.

    Only terminal registry evidence and the existing read-only authority
    loaders are consulted.  No network access, writes, cleanup, or directory
    scans are performed by this factory.
    """
    # Unit callers historically supplied lightweight RunSpec-shaped doubles;
    # retain that seam while authenticating all real protocol authorities.
    context: SourceAuthorityContext | None = None
    if isinstance(run_spec, (RunSpec, SourceRunSpec)):
        context = _authenticate_authority_paths(
            root, run_spec, acquisition_contract_path
        )
        authenticated_contract_path = context.acquisition_contract_path
        authenticated_root = context.repository_root
        evidence_root = context.raw_root
    else:
        authenticated_contract_path = acquisition_contract_path
        authenticated_root = root
        raw_relative = _safe_raw_namespace(run_spec)  # type: ignore[arg-type]
        evidence_root = Path(root) / raw_relative

    if context is not None:
        raw_relative = _safe_raw_namespace(run_spec)  # type: ignore[arg-type]
    graph = _load_terminal_query_graph(root, run_spec)
    static_batches = tuple(
        batch for batch in graph.query_batches if batch.stage == "STATIC_SCHEDULES"
    )
    if len(static_batches) != 1:
        raise ValueError("source verification requires one STATIC_SCHEDULES batch")

    schedule = load_verified_schedule_anchor_inventory(
        authenticated_root,
        run_spec,
        acquisition_contract_path=authenticated_contract_path,
        static_batch_id=static_batches[0].batch_id,
        evidence_root=evidence_root,
        read_only_progress=True,
    )
    source_loader_kwargs = (
        {
            "run_spec_path": context.authority_path,
            "acquisition_contract_path": context.acquisition_contract_path,
        }
        if context is not None and isinstance(run_spec, SourceRunSpec)
        else {}
    )
    timezone = load_verified_timezone_gate(
        authenticated_root, read_only_progress=True, **source_loader_kwargs
    )
    games = load_verified_game_evidence_inventory(
        authenticated_root, read_only_progress=True, **source_loader_kwargs
    )
    disposition, dispositioned = _game_inventory_disposition_binding(
        games, run_spec.run_id
    )
    store = V52EvidenceStore.open_read_only(evidence_root)
    disposition_authority: DuplicateGamePkDispositionAuthority | None = None
    if isinstance(run_spec, SourceRunSpec):
        if context is None:
            raise ValueError("source replay requires authenticated disposition authority")
        disposition_authority = build_duplicate_disposition_authority(
            authenticated_root,
            source_run_spec_path=context.authority_path,
            acquisition_contract_path=context.acquisition_contract_path,
        )
    records = tuple(
        _recompute_verified_query_records(store, graph.completed_queries)
        if disposition_authority is None
        else _recompute_verified_query_records(
            store,
            graph.completed_queries,
            duplicate_gamepk_disposition_authority=disposition_authority,
        )
    )
    game_snapshots = tuple(
        _build_game_normalized_snapshots(
            run_spec.run_id,
            records,
            games,
            timezone,
            schedule,
            duplicate_gamepk_disposition=disposition,
            dispositioned_query_ids=dispositioned,
        )
    )
    if isinstance(run_spec, SourceRunSpec) and (
        graph.checkpoint.dispositioned_query_ids != dispositioned
    ):
        raise ValueError("source replay checkpoint disposition IDs differ")
    return SnapshotReplayInputs(
        run_spec.run_id,
        graph,
        records,
        game_snapshots,
        games,
        timezone,
        schedule,
        disposition,
        dispositioned,
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
    *,
    duplicate_gamepk_disposition_authority: (
        DuplicateGamePkDispositionAuthority | None
    ) = None,
) -> tuple[VerifiedQueryRecord, ...]:
    records: list[VerifiedQueryRecord] = []
    for query in sorted(queries, key=logical_query_id):
        if (
            duplicate_gamepk_disposition_authority is not None
            and query == duplicate_gamepk_disposition_authority.query
        ):
            record = _read_verified_query_record(
                store,
                query,
                duplicate_gamepk_disposition_authority=(
                    duplicate_gamepk_disposition_authority
                ),
            )
        else:
            record = _read_verified_query_record(store, query)
        records.append(record)
    return tuple(records)


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
        or record.storage_mode not in {"FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"}
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
        or query.endpoint != "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
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
    duplicate_gamepk_disposition: DuplicateGamePkDispositionBinding | None = None,
    dispositioned_query_ids: tuple[str, ...] = (),
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
        or {record.query_sidecar_sha256 for record in records} != set(sidecar_hashes)
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
        **(
            {
                "duplicate_gamepk_disposition": duplicate_gamepk_disposition.as_mapping(),
                "dispositioned_query_ids": list(dispositioned_query_ids),
            }
            if duplicate_gamepk_disposition is not None
            else {}
        ),
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
        duplicate_gamepk_disposition=duplicate_gamepk_disposition,
        dispositioned_query_ids=dispositioned_query_ids,
    )


def _verified_schedule_sidecar(
    record: VerifiedQueryRecord,
    run_id: str,
    duplicate_gamepk_disposition: DuplicateGamePkDispositionBinding | None,
    source_anchors: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], str | None]:
    """Verify ordinary schedule evidence or adapt the one raw-less disposition."""
    if record.storage_mode != "DERIVED_DISPOSITION":
        return _verified_sidecar(record), None
    query = record.query
    if (
        run_id != _SOURCE_RUN_ID
        or duplicate_gamepk_disposition is None
        or record.query_id != logical_query_id(query)
        or record.query_id != duplicate_gamepk_disposition.query_id
        or record.query_sidecar_sha256
        != duplicate_gamepk_disposition.query_sidecar.sha256
        or query.endpoint != "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534"
        or record.raw_sha256 is not None
        or record.retained_raw_bytes is not None
        or not isinstance(record.query_sidecar_bytes, bytes)
        or not record.query_sidecar_bytes.endswith(b"\n")
        or hashlib.sha256(record.query_sidecar_bytes).hexdigest()
        != record.query_sidecar_sha256
    ):
        raise ValueError("source schedule disposition binding differs")
    try:
        published = json.loads(record.query_sidecar_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source schedule disposition sidecar differs") from exc
    if (
        not isinstance(published, dict)
        or canonical_json_bytes(published) + b"\n" != record.query_sidecar_bytes
        or published.get("storage_mode") != "DERIVED_DISPOSITION"
        or published.get("schema_version") != 1
        or published.get("parser_version") != query.parser_version
        or published.get("query_id") != record.query_id
        or published.get("source") != query.source
        or published.get("grain") != query.grain
    ):
        raise ValueError("source schedule disposition sidecar differs")
    adapter = dict(derived_schedule_sidecar_adapter(query, source_anchors))
    return adapter, hashlib.sha256(canonical_json_bytes(adapter) + b"\n").hexdigest()


def _build_game_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    game_inventory: VerifiedGameEvidenceInventory,
    timezone_gate: VerifiedTimezoneGate,
    schedule_inventory: VerifiedScheduleAnchorInventory,
    *,
    duplicate_gamepk_disposition: DuplicateGamePkDispositionBinding | None = None,
    dispositioned_query_ids: tuple[str, ...] = (),
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
    expected_disposition, expected_disposition_ids = _game_inventory_disposition_binding(
        game_inventory, run_id
    )
    if (
        duplicate_gamepk_disposition != expected_disposition
        or tuple(dispositioned_query_ids) != expected_disposition_ids
    ):
        raise ValueError("game snapshot disposition capability differs")
    if run_id == _SOURCE_RUN_ID:
        raw_source_anchors = schedule_inventory.payload.get("anchors")
        if not isinstance(raw_source_anchors, list):
            raise ValueError("source schedule inventory anchors differ")
    else:
        raw_source_anchors = []

    by_season: dict[int, list[tuple[VerifiedQueryRecord, dict[str, object]]]] = {
        season: [] for season in _GAME_SEASONS
    }
    seen_query_ids: set[str] = set()
    for record in records:
        query = record.query
        if record.storage_mode == "DERIVED_DISPOSITION":
            if (
                run_id != _SOURCE_RUN_ID
                or duplicate_gamepk_disposition is None
                or record.query_id != duplicate_gamepk_disposition.query_id
            ):
                raise ValueError("game snapshot schedule disposition differs")
            season = 2019
            sidecar = {}  # Adapted once the exact season anchors are available.
        else:
            sidecar = _verified_sidecar(record)
        if (
            query.source != "mlb_schedule_game_feed"
            or query.grain != "season_schedule_and_game"
        ):
            raise ValueError("game snapshot query source or grain differs")
        boxscore_match = _BOXSCORE_ENDPOINT.fullmatch(query.endpoint)
        if record.storage_mode == "DERIVED_DISPOSITION":
            pass
        elif query.endpoint == _SCHEDULE_ENDPOINT:
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
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in by_season
        ):
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
        schedule_record, _schedule_sidecar = anchors[0]
        if run_id == _SOURCE_RUN_ID and (
            (season == 2019)
            != (schedule_record.storage_mode == "DERIVED_DISPOSITION")
        ):
            raise ValueError("source game snapshot disposition season differs")
        source_anchors = [
            anchor
            for anchor in raw_source_anchors
            if anchor.get("season") == season
        ]
        schedule_sidecar, adapter_sha256 = _verified_schedule_sidecar(
            schedule_record,
            run_id,
            duplicate_gamepk_disposition,
            source_anchors,
        )
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
        season_provenance = (
            build_schedule_anchor_provenance(source_anchors)
            if run_id == _SOURCE_RUN_ID
            else None
        )
        payload = dict(
            recompute_game_evidence_snapshot(
                [schedule_sidecar],
                boxscores,
                schedule_provenance=season_provenance,
            )
        )
        if adapter_sha256 is not None:
            payload = _replace_source_disposition_lineage(
                payload,
                synthetic_sidecar_sha256=adapter_sha256,
                derived_sidecar_sha256=schedule_record.query_sidecar_sha256,
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
    if (
        isinstance(season_sources, (str, bytes))
        or not isinstance(season_sources, Sequence)
        or len(season_sources) != len(_GAME_SEASONS)
    ):
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
    candidate_schedule_hashes = timezone_gate.candidate.get("schedule_sidecar_sha256s")
    if list(candidate_schedule_hashes or ()) != expected_schedule_hashes:
        raise ValueError("timezone schedule sidecar authority differs")
    validate_timezone_mapping(timezone_gate.mapping, schedule_sidecars)

    game_payload = _plain_json(game_inventory.payload)
    if (
        not isinstance(game_payload, dict)
        or set(game_payload)
        != (
            _SOURCE_GAME_INVENTORY_KEYS
            if game_payload.get("run_id") == _SOURCE_RUN_ID
            else _OLD_GAME_INVENTORY_KEYS
        )
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
        or tuple(_binding_sha256(value, "game query batch") for value in query_batches)
        != timezone_gate.query_batch_sha256s
    ):
        raise ValueError("game inventory query batch authority differs")
    if (
        _binding_sha256(
            game_payload.get("schedule_inventory"), "game schedule inventory"
        )
        != schedule_inventory.sha256
    ):
        raise ValueError("game schedule inventory authority differs")
    timezone_binding = game_payload.get("timezone_gate")
    if not isinstance(timezone_binding, Mapping):
        raise ValueError("game timezone authority differs")
    reviews = timezone_binding.get("reviews")
    if (
        isinstance(reviews, (str, bytes))
        or not isinstance(reviews, Sequence)
        or len(reviews) != 2
    ):
        raise ValueError("game timezone review authority differs")
    if (
        _binding_sha256(timezone_binding.get("candidate"), "game timezone candidate")
        != timezone_gate.candidate_sha256
        or _binding_sha256(timezone_binding.get("mapping"), "game timezone mapping")
        != timezone_gate.mapping_sha256
        or tuple(_binding_sha256(value, "game timezone review") for value in reviews)
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
        starter_id for game in played_games for starter_id in game["actual_starter_ids"]
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
        or game_payload.get("source_query_sidecar_sha256s") != expected_sidecar_hashes
    ):
        raise ValueError("game inventory normalized population differs")

    for season, payload in zip(_GAME_SEASONS, payloads, strict=True):
        snapshots.append(
            _normalized_snapshot(
                run_id,
                season,
                snapshot_inputs[season],
                payload,
                (
                    duplicate_gamepk_disposition
                    if season == 2019 else None
                ),
                (
                    dispositioned_query_ids
                    if season == 2019 else ()
                ),
            )
        )
    return tuple(snapshots)


def _validate_history_snapshot_lineage(
    history_snapshot: NormalizedSnapshot,
    game_snapshot: NormalizedSnapshot,
) -> dict[str, object]:
    history_mapping = (
        history_snapshot.as_mapping()
        if isinstance(history_snapshot, NormalizedSnapshot)
        else None
    )
    game_mapping = (
        game_snapshot.as_mapping()
        if isinstance(game_snapshot, NormalizedSnapshot)
        else None
    )
    if history_mapping is None or game_mapping is None:
        raise ValueError("history normalized snapshot lineage differs")
    try:
        history_validated = _validate_normalized_snapshot_mapping(history_mapping)
        game_validated = _validate_normalized_snapshot_mapping(game_mapping)
    except (TypeError, ValueError) as exc:
        raise ValueError("history normalized snapshot lineage differs") from exc
    if (
        game_snapshot.source != "mlb_schedule_game_feed"
        or game_snapshot.grain != "season_game_evidence_snapshot"
        or game_validated != game_mapping
        or game_snapshot.canonical_bytes != canonical_json_bytes(game_mapping) + b"\n"
        or game_snapshot.sha256
        != hashlib.sha256(game_snapshot.canonical_bytes).hexdigest()
        or history_snapshot.source != "prior_schedule_history"
        or history_snapshot.grain != "verified_game_projection"
        or history_validated != history_mapping
        or history_snapshot.canonical_bytes
        != canonical_json_bytes(history_mapping) + b"\n"
        or history_snapshot.sha256
        != hashlib.sha256(history_snapshot.canonical_bytes).hexdigest()
        or history_snapshot.run_id != game_snapshot.run_id
        or history_snapshot.season != game_snapshot.season
    ):
        raise ValueError("history normalized snapshot lineage differs")
    payload = recompute_prior_history_sidecar(
        game_snapshot.payload,
        source_snapshot_sha256=game_snapshot.sha256,
    )
    expected = _history_normalized_snapshot(
        game_snapshot.run_id, game_snapshot, payload
    )
    if (
        history_snapshot.logical_query_ids != game_snapshot.logical_query_ids
        or history_snapshot.source_query_sidecar_sha256s
        != game_snapshot.source_query_sidecar_sha256s
        or history_snapshot.split_receipt_sha256s
        or history_snapshot.canonical_bytes != expected.canonical_bytes
        or history_snapshot.sha256 != expected.sha256
    ):
        raise ValueError("history normalized snapshot lineage differs")
    return history_mapping


def _history_normalized_snapshot(
    run_id: str,
    game_snapshot: NormalizedSnapshot,
    payload: Mapping[str, object],
) -> NormalizedSnapshot:
    if payload.get("source_snapshot_sha256") != game_snapshot.sha256:
        raise ValueError("history upstream source snapshot SHA-256 differs")
    envelope: dict[str, object] = {
        "grain": "verified_game_projection",
        "logical_query_ids": list(game_snapshot.logical_query_ids),
        "payload": _plain_json(payload),
        "run_id": run_id,
        "schema_version": 1,
        "season": game_snapshot.season,
        "source": "prior_schedule_history",
        "source_query_sidecar_sha256s": list(
            game_snapshot.source_query_sidecar_sha256s
        ),
        "split_receipt_sha256s": [],
        "status": "READY",
    }
    envelope = _validate_normalized_snapshot_mapping(envelope)
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    components = set(game_snapshot.logical_query_ids) | set(
        game_snapshot.source_query_sidecar_sha256s
    )
    if digest in components or digest == game_snapshot.sha256:
        raise ValueError("normalized snapshot hash collides with upstream lineage")
    frozen_payload = _freeze_json(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("normalized snapshot payload must freeze to a mapping")
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=game_snapshot.season,
        source="prior_schedule_history",
        grain="verified_game_projection",
        status="READY",
        logical_query_ids=game_snapshot.logical_query_ids,
        source_query_sidecar_sha256s=(game_snapshot.source_query_sidecar_sha256s),
        split_receipt_sha256s=(),
        payload=frozen_payload,
        canonical_bytes=contents,
        sha256=digest,
        duplicate_gamepk_disposition=None,
        dispositioned_query_ids=(),
    )


def _build_history_normalized_snapshots(
    run_id: str,
    game_snapshots: Sequence[NormalizedSnapshot],
) -> tuple[NormalizedSnapshot, ...]:
    """Project seven authenticated game snapshots into prior history without I/O."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("history normalized snapshot run_id differs")
    if isinstance(game_snapshots, (str, bytes)) or not isinstance(
        game_snapshots, Sequence
    ):
        raise TypeError("history game snapshots must be a sequence")

    game_by_season: dict[int, NormalizedSnapshot] = {}
    seen_game_pks: set[int] = set()
    for snapshot in game_snapshots:
        mapping = (
            snapshot.as_mapping() if isinstance(snapshot, NormalizedSnapshot) else None
        )
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.run_id != run_id
            or snapshot.source != "mlb_schedule_game_feed"
            or snapshot.grain != "season_game_evidence_snapshot"
            or snapshot.status != "READY"
            or snapshot.season in game_by_season
            or mapping is None
            or _validate_normalized_snapshot_mapping(mapping) != mapping
            or snapshot.canonical_bytes != canonical_json_bytes(mapping) + b"\n"
            or snapshot.sha256 != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
        ):
            raise ValueError("history game snapshot authority differs")
        opening, closing = SEASON_BOUNDARIES[snapshot.season]
        games = mapping["payload"]["games"]  # type: ignore[index]
        for game in games:
            game_pk = game.get("game_pk")
            official = _canonical_day(
                game.get("official_date"), "history game official date"
            )
            if (
                type(game_pk) is not int
                or game_pk in seen_game_pks
                or game.get("season") != snapshot.season
                or game.get("season_start_date") != opening.isoformat()
                or not opening <= official <= closing
            ):
                raise ValueError("history gamePk or frozen season boundary differs")
            seen_game_pks.add(game_pk)
        game_by_season[snapshot.season] = snapshot
    if set(game_by_season) != set(_GAME_SEASONS):
        raise ValueError("history requires exactly seven 2018-2024 game snapshots")

    snapshots: list[NormalizedSnapshot] = []
    for season in _GAME_SEASONS:
        game_snapshot = game_by_season[season]
        payload = recompute_prior_history_sidecar(
            game_snapshot.payload,
            source_snapshot_sha256=game_snapshot.sha256,
        )
        snapshots.append(_history_normalized_snapshot(run_id, game_snapshot, payload))
    return tuple(snapshots)


def _statcast_normalized_snapshot(
    run_id: str,
    season: int,
    records: Sequence[VerifiedQueryRecord],
    receipt_sha256s: Sequence[str],
    payload: Mapping[str, object],
) -> NormalizedSnapshot:
    query_ids = tuple(sorted(record.query_id for record in records))
    sidecar_hashes = tuple(sorted(record.query_sidecar_sha256 for record in records))
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
        len(completed)
        != sum(
            query.source == "baseball_savant_statcast" and query.grain == "pitch"
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
                raise ValueError(
                    "Statcast split children differ from canonical bisection"
                )

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
            or snapshot.sha256 != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
        ):
            raise ValueError("Statcast game snapshot authority differs")
        game_by_season[snapshot.season] = snapshot
        for game in snapshot.payload["games"]:  # type: ignore[index]
            if game["played"] is not True:  # type: ignore[index]
                continue
            game_pk = game["game_pk"]  # type: ignore[index]
            official = _canonical_day(
                game["official_date"],
                "Statcast played game official_date",  # type: ignore[index]
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
    sidecar_hashes = tuple(sorted(record.query_sidecar_sha256 for record in records))
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

    expected_by_id = pitching_projection(graph.completed_queries, "completed-query")
    if not expected_by_id:
        raise ValueError("pitching terminal graph population is empty")

    root_population = pitching_projection(graph.root_queries, "root-query")
    split_parent_population = pitching_projection(
        graph.split_parent_queries, "split-parent"
    )
    split_leaf_population = pitching_projection(graph.split_leaf_queries, "split-leaf")
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
        mapping = (
            snapshot.as_mapping() if isinstance(snapshot, NormalizedSnapshot) else None
        )
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.run_id != run_id
            or snapshot.source != "mlb_schedule_game_feed"
            or snapshot.grain != "season_game_evidence_snapshot"
            or snapshot.season in game_by_season
            or mapping is None
            or _validate_normalized_snapshot_mapping(mapping) != mapping
            or snapshot.canonical_bytes != canonical_json_bytes(mapping) + b"\n"
            or snapshot.sha256 != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
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
            or (query.endpoint == "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING")
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
        cutoffs = {str(game_pk): cutoff.isoformat() for game_pk in consumers}
        season_open = SEASON_BOUNDARIES[season][0]
        if cutoff < season_open:
            expected_endpoint = "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
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
    if set(record_by_id) != set(expected_by_id) or seen_keys != set(expected_consumers):
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


def _career_normalized_snapshot(
    run_id: str,
    season: int,
    payload: Mapping[str, object],
    record_by_sidecar_hash: Mapping[str, VerifiedQueryRecord],
) -> NormalizedSnapshot:
    rows = payload.get("records")
    if not isinstance(rows, list) or not rows:
        raise ValueError("career normalized snapshot records are missing")
    lineage_hashes = sorted(
        {
            digest
            for row in rows
            for digest in (
                row["baseline_query_sidecar_sha256"],
                *row["statcast_ready_leaf_sidecar_sha256s"],
            )
        }
    )
    if any(digest not in record_by_sidecar_hash for digest in lineage_hashes):
        raise ValueError("career normalized snapshot source lineage is unknown")
    lineage_records = [record_by_sidecar_hash[digest] for digest in lineage_hashes]
    query_ids = tuple(sorted(record.query_id for record in lineage_records))
    sidecar_hashes = tuple(lineage_hashes)
    split_hashes = tuple(
        sorted(
            {digest for row in rows for digest in row["statcast_split_receipt_sha256s"]}
        )
    )
    if len(query_ids) != len(set(query_ids)) or len(query_ids) != len(sidecar_hashes):
        raise ValueError("career normalized snapshot identity is duplicated")
    envelope: dict[str, object] = {
        "grain": "player_career_batters_faced_reference",
        "logical_query_ids": list(query_ids),
        "payload": _plain_json(payload),
        "run_id": run_id,
        "schema_version": 1,
        "season": season,
        "source": "mlb_stats_pitcher_bf",
        "source_query_sidecar_sha256s": list(sidecar_hashes),
        "split_receipt_sha256s": list(split_hashes),
        "status": "READY",
    }
    envelope = _validate_normalized_snapshot_mapping(envelope)
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    if digest in set(query_ids) | set(sidecar_hashes) | set(split_hashes):
        raise ValueError("normalized snapshot hash collides with a component hash")
    frozen_payload = _freeze_json(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("normalized snapshot payload must freeze to a mapping")
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=season,
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        status="READY",
        logical_query_ids=query_ids,
        source_query_sidecar_sha256s=sidecar_hashes,
        split_receipt_sha256s=split_hashes,
        payload=frozen_payload,
        canonical_bytes=contents,
        sha256=digest,
    )


def _validate_career_terminal_graph_shape(graph: _TerminalQueryGraph) -> None:
    query_fields = (
        ("completed_queries", graph.completed_queries),
        ("root_queries", graph.root_queries),
        ("split_parent_queries", graph.split_parent_queries),
        ("split_leaf_queries", graph.split_leaf_queries),
    )
    for label, queries in query_fields:
        if not isinstance(queries, tuple) or any(
            not isinstance(query, LogicalQuery) for query in queries
        ):
            raise TypeError(f"career terminal graph {label} differs")

    if not isinstance(graph.statcast_root_batch, QueryBatch):
        raise TypeError("career terminal graph Statcast root batch differs")
    if not isinstance(graph.query_batches, tuple) or any(
        not isinstance(batch, QueryBatch) for batch in graph.query_batches
    ):
        raise TypeError("career terminal graph query_batches differs")
    for batch in (graph.statcast_root_batch, *graph.query_batches):
        if not isinstance(batch.queries, tuple) or any(
            not isinstance(query, LogicalQuery) for query in batch.queries
        ):
            raise TypeError("career terminal graph batch queries differ")

    if not isinstance(graph.split_receipts, tuple) or any(
        not isinstance(receipt, SplitReceipt) for receipt in graph.split_receipts
    ):
        raise TypeError("career terminal graph split_receipts differs")


def _build_career_bf_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    graph: _TerminalQueryGraph,
    game_snapshots: Sequence[NormalizedSnapshot],
) -> tuple[NormalizedSnapshot, ...]:
    """Build seven snapshots from externally sealed records with internal rebinding.

    A self-consistent ``VerifiedQueryRecord`` is assumed externally sealed by the
    acquisition checkpoint. This pure builder revalidates all internal bindings,
    but cannot identify a semantic forgery that has also been re-signed upstream.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("career normalized snapshot run_id differs")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError("career normalized snapshot records must be a sequence")
    if not isinstance(graph, _TerminalQueryGraph):
        raise TypeError("career terminal query graph authority differs")
    _validate_career_terminal_graph_shape(graph)
    if isinstance(game_snapshots, (str, bytes)) or not isinstance(
        game_snapshots, Sequence
    ):
        raise TypeError("career game snapshots must be a sequence")

    def projection(
        queries: Sequence[LogicalQuery], source: str, grain: str, label: str
    ) -> dict[str, LogicalQuery]:
        result: dict[str, LogicalQuery] = {}
        for query in queries:
            relevant = query.source == source or query.grain == grain
            if not relevant:
                continue
            if query.source != source or query.grain != grain:
                raise ValueError(f"career {label} source or grain differs")
            query_id = logical_query_id(query)
            if query_id in result:
                raise ValueError(f"career {label} population is duplicated")
            result[query_id] = query
        return result

    career_completed = projection(
        graph.completed_queries,
        "mlb_stats_pitcher_bf",
        "player_career_batters_faced_reference",
        "completed-query",
    )
    if not career_completed:
        raise ValueError("career terminal graph population is empty")
    if (
        projection(
            graph.root_queries,
            "mlb_stats_pitcher_bf",
            "player_career_batters_faced_reference",
            "root-query",
        )
        != career_completed
    ):
        raise ValueError("career root/completed query population differs")
    if projection(
        graph.split_parent_queries,
        "mlb_stats_pitcher_bf",
        "player_career_batters_faced_reference",
        "split-parent",
    ) or projection(
        graph.split_leaf_queries,
        "mlb_stats_pitcher_bf",
        "player_career_batters_faced_reference",
        "split-leaf",
    ):
        raise ValueError("career baseline queries cannot be split")
    career_ids = set(career_completed)
    if any(
        receipt.parent_query_id in career_ids
        or set(receipt.child_query_ids).intersection(career_ids)
        for receipt in graph.split_receipts
    ):
        raise ValueError("career baseline query appears in Statcast split lineage")
    batch_career = projection(
        tuple(query for batch in graph.query_batches for query in batch.queries),
        "mlb_stats_pitcher_bf",
        "player_career_batters_faced_reference",
        "query-batch",
    )
    if batch_career != career_completed:
        raise ValueError("career terminal query batch population differs")

    statcast_completed = projection(
        graph.completed_queries,
        "baseball_savant_statcast",
        "pitch",
        "Statcast completed-query",
    )
    if not statcast_completed:
        raise ValueError("career Statcast terminal graph population is empty")
    statcast_roots = projection(
        graph.root_queries,
        "baseball_savant_statcast",
        "pitch",
        "Statcast root-query",
    )
    statcast_parents = projection(
        graph.split_parent_queries,
        "baseball_savant_statcast",
        "pitch",
        "Statcast split-parent",
    )
    statcast_leaves = projection(
        graph.split_leaf_queries,
        "baseball_savant_statcast",
        "pitch",
        "Statcast split-leaf",
    )
    receipt_parent_ids = {receipt.parent_query_id for receipt in graph.split_receipts}
    receipt_terminal_child_ids = {
        child_id
        for receipt in graph.split_receipts
        for child_id in receipt.child_query_ids
        if child_id not in receipt_parent_ids
    }
    if set(statcast_parents) != receipt_parent_ids:
        raise ValueError("career Statcast split-parent classification differs")
    if set(statcast_leaves) != receipt_terminal_child_ids:
        raise ValueError("career Statcast split-leaf classification differs")
    if (
        set(statcast_roots).intersection(statcast_leaves)
        or set(statcast_completed) != set(statcast_roots) | set(statcast_leaves)
    ):
        raise ValueError("career Statcast terminal classification differs")
    statcast_batch_population = projection(
        tuple(query for batch in graph.query_batches for query in batch.queries),
        "baseball_savant_statcast",
        "pitch",
        "Statcast query-batch",
    )
    if set(statcast_batch_population) != (
        set(statcast_roots) | set(statcast_parents) | set(statcast_leaves)
    ):
        raise ValueError("career Statcast query-batch population differs")

    game_by_season: dict[int, NormalizedSnapshot] = {}
    game_season: dict[int, int] = {}
    expected_consumers: dict[tuple[int, str], dict[int, date]] = {}
    for snapshot in game_snapshots:
        mapping = (
            snapshot.as_mapping() if isinstance(snapshot, NormalizedSnapshot) else None
        )
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.run_id != run_id
            or snapshot.source != "mlb_schedule_game_feed"
            or snapshot.grain != "season_game_evidence_snapshot"
            or snapshot.season in game_by_season
            or mapping is None
            or _validate_normalized_snapshot_mapping(mapping) != mapping
            or snapshot.canonical_bytes != canonical_json_bytes(mapping) + b"\n"
            or snapshot.sha256 != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
        ):
            raise ValueError("career game snapshot authority differs")
        game_by_season[snapshot.season] = snapshot
        played_count = 0
        for game in mapping["payload"]["games"]:  # type: ignore[index]
            game_pk = game.get("game_pk")
            if type(game_pk) is not int or game_pk <= 0 or game_pk in game_season:
                raise ValueError("career game population is duplicated")
            game_season[game_pk] = snapshot.season
            if game.get("played") is not True:
                continue
            played_count += 1
            starters = game.get("actual_starter_ids")
            if (
                not isinstance(starters, list)
                or len(starters) != 2
                or len(set(starters)) != 2
                or any(
                    type(pitcher_id) is not int or pitcher_id <= 0
                    for pitcher_id in starters
                )
            ):
                raise ValueError("career played game requires two distinct starters")
            official = _canonical_day(
                game.get("official_date"), "career consumer official date"
            )
            cutoff = official - timedelta(days=1)
            inclusive_end = (
                "2017-12-31" if cutoff <= date(2021, 12, 31) else "2021-12-31"
            )
            for pitcher_id in starters:
                expected_consumers.setdefault((pitcher_id, inclusive_end), {})[
                    game_pk
                ] = cutoff
        if played_count == 0:
            raise ValueError("career requires played consumers in every season")
    if set(game_by_season) != set(_GAME_SEASONS):
        raise ValueError("career requires seven verified game snapshots")

    career_records: dict[str, tuple[VerifiedQueryRecord, dict[str, object]]] = {}
    statcast_records: dict[str, tuple[VerifiedQueryRecord, dict[str, object]]] = {}
    record_by_sidecar_hash: dict[str, VerifiedQueryRecord] = {}
    for record in records:
        if not isinstance(record, VerifiedQueryRecord):
            raise TypeError("career records must be VerifiedQueryRecord values")
        if record.query.source == "mlb_stats_pitcher_bf":
            expected = career_completed
            destination = career_records
        elif record.query.source == "baseball_savant_statcast":
            expected = statcast_completed
            destination = statcast_records
        else:
            raise ValueError("career verified record source population differs")
        sidecar = _verified_sidecar(record)
        if (
            record.query_id not in expected
            or expected[record.query_id] != record.query
            or record.query_id in destination
            or record.query_sidecar_sha256 in record_by_sidecar_hash
        ):
            raise ValueError("career verified record population differs")
        destination[record.query_id] = (record, sidecar)
        record_by_sidecar_hash[record.query_sidecar_sha256] = record
    if set(career_records) != set(career_completed) or set(statcast_records) != set(
        statcast_completed
    ):
        raise ValueError("career READY record coverage differs")

    observed_partitions: set[tuple[int, str]] = set()
    for query_id, (record, sidecar) in career_records.items():
        query = record.query
        requested = dict(query.requested_range)
        pitcher_id = requested.get("pitcher_id")
        inclusive_end = requested.get("inclusive_end")
        if (
            type(pitcher_id) is not int
            or pitcher_id <= 0
            or not isinstance(inclusive_end, str)
        ):
            raise ValueError("career baseline query identity differs")
        key = (pitcher_id, inclusive_end)
        consumers = expected_consumers.get(key)
        if consumers is None or key in observed_partitions:
            raise ValueError("career baseline consumer partition differs")
        consuming_game_pks = sorted(consumers)
        cutoff_dates = {
            str(game_pk): consumers[game_pk].isoformat()
            for game_pk in consuming_game_pks
        }
        expected_requested = {
            "consumer_cutoff_dates": cutoff_dates,
            "consuming_game_pks": consuming_game_pks,
            "pitcher_id": pitcher_id,
            "inclusive_end": inclusive_end,
        }
        expected_parameters = {
            "stats": "byDateRange",
            "group": "pitching",
            "gameType": "R",
            "sportIds": 1,
            "startDate": "1876-01-01",
            "endDate": inclusive_end,
        }
        semantics = sidecar.get("semantics")
        if (
            query.endpoint
            != f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats"
            or query.expected_media_type != "application/json"
            or query.parser_version != "v5.2.1"
            or query.parent_query_id is not None
            or dict(query.parameters) != expected_parameters
            or requested != expected_requested
            or not isinstance(semantics, Mapping)
            or semantics.get("pitcher_id") != pitcher_id
            or semantics.get("inclusive_end_date") != inclusive_end
            or semantics.get("consuming_game_pks") != consuming_game_pks
            or semantics.get("consumer_cutoff_dates") != cutoff_dates
        ):
            raise ValueError("career baseline query/sidecar binding differs")
        observed_partitions.add(key)
    if observed_partitions != set(expected_consumers):
        raise ValueError("career baseline consumer coverage differs")

    payload = recompute_career_bf_snapshot(
        [sidecar for _record, sidecar in career_records.values()],
        statcast_ready_leaf_sidecars=[
            sidecar for _record, sidecar in statcast_records.values()
        ],
        statcast_root_queries=graph.statcast_root_batch.queries,
        statcast_split_receipts=graph.split_receipts,
    )
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ValueError("career aggregate records differ")
    expected_keys = {
        (pitcher_id, game_pk)
        for (pitcher_id, _inclusive_end), consumers in expected_consumers.items()
        for game_pk in consumers
    }
    actual_keys = {(row["pitcher_id"], row["game_pk"]) for row in rows}
    if actual_keys != expected_keys or len(actual_keys) != len(rows):
        raise ValueError("career aggregate consumer population differs")

    snapshots: list[NormalizedSnapshot] = []
    for season in _GAME_SEASONS:
        season_rows = [row for row in rows if game_season[row["game_pk"]] == season]
        season_payload = {
            "schema_version": 1,
            "grain": "player_career_batters_faced_reference",
            "records": season_rows,
        }
        snapshots.append(
            _career_normalized_snapshot(
                run_id, season, season_payload, record_by_sidecar_hash
            )
        )
    return tuple(snapshots)


def _verified_reference_record(
    record: VerifiedQueryRecord,
) -> tuple[dict[str, object], bytes]:
    if not isinstance(record, VerifiedQueryRecord):
        raise TypeError("reference records must be VerifiedQueryRecord values")
    raw = record.retained_raw_bytes
    query_id = logical_query_id(record.query)
    if (
        record.storage_mode not in {"FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"}
        or not isinstance(raw, bytes)
        or not raw
        or record.raw_sha256 != hashlib.sha256(raw).hexdigest()
        or record.query_id != query_id
        or not isinstance(record.query_sidecar_bytes, bytes)
        or not record.query_sidecar_bytes.endswith(b"\n")
        or record.query_sidecar_sha256
        != hashlib.sha256(record.query_sidecar_bytes).hexdigest()
    ):
        raise ValueError("reference retained raw record binding differs")
    expected = query_sidecar(
        raw, record.query, recompute_query_sidecar(record.query, raw)
    )
    expected_bytes = canonical_json_bytes(expected) + b"\n"
    if record.query_sidecar_bytes != expected_bytes:
        raise ValueError("reference query sidecar recomputation differs")
    return expected, raw


def _reference_normalized_snapshot(
    run_id: str,
    season: int,
    records: Sequence[VerifiedQueryRecord],
    payload: Mapping[str, object],
) -> NormalizedSnapshot:
    query_ids = tuple(sorted(record.query_id for record in records))
    sidecar_hashes = tuple(sorted(record.query_sidecar_sha256 for record in records))
    if (
        not query_ids
        or len(query_ids) != len(set(query_ids))
        or len(sidecar_hashes) != len(set(sidecar_hashes))
    ):
        raise ValueError("reference normalized snapshot identity is duplicated")
    envelope: dict[str, object] = {
        "grain": "effective_dated_venue_team_reference",
        "logical_query_ids": list(query_ids),
        "payload": _plain_json(payload),
        "run_id": run_id,
        "schema_version": 1,
        "season": season,
        "source": "venue_team_timezone_table",
        "source_query_sidecar_sha256s": list(sidecar_hashes),
        "split_receipt_sha256s": [],
        "status": "READY",
    }
    envelope = _validate_normalized_snapshot_mapping(envelope)
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    raw_hashes = set(payload["team_payload_sha256s"]) | set(  # type: ignore[arg-type]
        payload["venue_payload_sha256s"]  # type: ignore[arg-type]
    )
    if digest in set(query_ids) | set(sidecar_hashes) | raw_hashes:
        raise ValueError("reference snapshot hash collides with a component hash")
    frozen_payload = _freeze_json(payload)
    if not isinstance(frozen_payload, Mapping):
        raise AssertionError("reference snapshot payload must freeze to a mapping")
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=season,
        source="venue_team_timezone_table",
        grain="effective_dated_venue_team_reference",
        status="READY",
        logical_query_ids=query_ids,
        source_query_sidecar_sha256s=sidecar_hashes,
        split_receipt_sha256s=(),
        payload=frozen_payload,
        canonical_bytes=contents,
        sha256=digest,
    )


def _canonical_file_sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(_plain_json(value)) + b"\n").hexdigest()


def _validate_reference_terminal_graph_shape(
    graph: _TerminalQueryGraph,
) -> None:
    query_fields = (
        ("completed_queries", graph.completed_queries),
        ("root_queries", graph.root_queries),
        ("split_parent_queries", graph.split_parent_queries),
        ("split_leaf_queries", graph.split_leaf_queries),
    )
    for label, queries in query_fields:
        if not isinstance(queries, tuple) or any(
            not isinstance(query, LogicalQuery) for query in queries
        ):
            raise TypeError(f"reference terminal graph {label} differs")

    if not isinstance(graph.statcast_root_batch, QueryBatch):
        raise TypeError("reference terminal graph Statcast root batch differs")
    batches = (graph.statcast_root_batch,) + (
        graph.query_batches if isinstance(graph.query_batches, tuple) else ()
    )
    if not isinstance(graph.query_batches, tuple) or any(
        not isinstance(batch, QueryBatch) for batch in graph.query_batches
    ):
        raise TypeError("reference terminal graph query_batches differs")
    for batch in batches:
        if not isinstance(batch.queries, tuple) or any(
            not isinstance(query, LogicalQuery) for query in batch.queries
        ):
            raise TypeError("reference terminal graph batch queries differ")

    if not isinstance(graph.split_receipts, tuple) or any(
        not isinstance(receipt, SplitReceipt) for receipt in graph.split_receipts
    ):
        raise TypeError("reference terminal graph split_receipts differs")


def _build_reference_normalized_snapshots(
    run_id: str,
    records: Sequence[VerifiedQueryRecord],
    graph: _TerminalQueryGraph,
    game_snapshots: Sequence[NormalizedSnapshot],
    timezone_gate: VerifiedTimezoneGate,
) -> tuple[NormalizedSnapshot, ...]:
    """Build immutable effective-dated reference snapshots without I/O."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("reference normalized snapshot run_id differs")
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        raise TypeError("reference records must be a sequence")
    if not isinstance(graph, _TerminalQueryGraph):
        raise TypeError("reference terminal query graph authority differs")
    _validate_reference_terminal_graph_shape(graph)
    if isinstance(game_snapshots, (str, bytes)) or not isinstance(
        game_snapshots, Sequence
    ):
        raise TypeError("reference game snapshots must be a sequence")
    if not isinstance(timezone_gate, VerifiedTimezoneGate):
        raise TypeError("reference timezone gate authority differs")

    def reference_projection(
        queries: Sequence[LogicalQuery], label: str
    ) -> dict[str, LogicalQuery]:
        projection: dict[str, LogicalQuery] = {}
        for query in queries:
            relevant = query.source == "venue_team_timezone_table" or query.grain == (
                "effective_dated_venue_team_reference"
            )
            if not relevant:
                continue
            if (
                query.source != "venue_team_timezone_table"
                or query.grain != "effective_dated_venue_team_reference"
            ):
                raise ValueError(f"reference {label} source or grain differs")
            query_id = logical_query_id(query)
            if query_id in projection:
                raise ValueError(f"reference {label} population is duplicated")
            projection[query_id] = query
        return projection

    expected_by_id = reference_projection(graph.completed_queries, "completed-query")
    if not expected_by_id:
        raise ValueError("reference terminal graph population is empty")
    if reference_projection(graph.root_queries, "root-query") != expected_by_id:
        raise ValueError("reference root/completed query population differs")
    if reference_projection(graph.split_parent_queries, "split-parent") or (
        reference_projection(graph.split_leaf_queries, "split-leaf")
    ):
        raise ValueError("reference queries cannot use split graph classifications")
    gate_batch_ids = tuple(timezone_gate.query_batch_ids)
    gate_batch_hashes = tuple(timezone_gate.query_batch_sha256s)
    if (
        len(gate_batch_ids) != 2
        or len(set(gate_batch_ids)) != 2
        or len(gate_batch_hashes) != 2
        or any(
            not isinstance(item, str) or _SHA256.fullmatch(item) is None
            for item in (*gate_batch_ids, *gate_batch_hashes)
        )
    ):
        raise ValueError("reference timezone gate batch population differs")
    gate_hash_by_id = dict(zip(gate_batch_ids, gate_batch_hashes, strict=True))
    gate_batches = [
        batch for batch in graph.query_batches if batch.batch_id in gate_hash_by_id
    ]
    canonical_stage_batches = [
        batch
        for batch in graph.query_batches
        if batch.stage in {"STATIC_SCHEDULES", "REFERENCE_INPUTS"}
    ]
    if (
        len(gate_batches) != 2
        or {batch.batch_id for batch in gate_batches} != set(gate_batch_ids)
        or len(canonical_stage_batches) != 2
        or {batch.batch_id for batch in canonical_stage_batches} != set(gate_batch_ids)
    ):
        raise ValueError("reference graph/gate batch population differs")
    batches_by_stage: dict[str, QueryBatch] = {}
    for batch in gate_batches:
        if batch.stage in batches_by_stage:
            raise ValueError("reference graph/gate batch stage is duplicated")
        batches_by_stage[batch.stage] = batch
    if set(batches_by_stage) != {"STATIC_SCHEDULES", "REFERENCE_INPUTS"}:
        raise ValueError("reference graph/gate batch stages differ")
    static_batch = batches_by_stage["STATIC_SCHEDULES"]
    batch = batches_by_stage["REFERENCE_INPUTS"]
    if (
        static_batch.derived_from_sha256 is not None
        or batch.derived_from_sha256 != timezone_gate.schedule_inventory_sha256
        or _make_batch(
            static_batch.stage,
            static_batch.derived_from_sha256,
            static_batch.queries,
        )
        != static_batch
        or _make_batch(batch.stage, batch.derived_from_sha256, batch.queries) != batch
    ):
        raise ValueError("reference canonical gate batch authority differs")
    for authenticated_batch in (static_batch, batch):
        if gate_hash_by_id[authenticated_batch.batch_id] != (
            _query_batch_artifact_sha256(authenticated_batch)
        ):
            raise ValueError("reference query batch hash binding differs")

    protected_query_by_id: dict[str, LogicalQuery] = {}
    for authenticated_batch in (static_batch, batch):
        for query in authenticated_batch.queries:
            query_id = logical_query_id(query)
            if query_id in protected_query_by_id:
                raise ValueError("reference gate batch query population is duplicated")
            protected_query_by_id[query_id] = query

    def graph_query_population(
        queries: Sequence[LogicalQuery], label: str
    ) -> dict[str, LogicalQuery]:
        population: dict[str, LogicalQuery] = {}
        for query in queries:
            query_id = logical_query_id(query)
            if query_id in population:
                raise ValueError(f"reference graph {label} is duplicated")
            population[query_id] = query
        return population

    completed_population = graph_query_population(
        graph.completed_queries, "completed-query population"
    )
    root_population = graph_query_population(
        graph.root_queries, "root-query population"
    )
    split_parent_population = graph_query_population(
        graph.split_parent_queries, "split-parent population"
    )
    split_leaf_population = graph_query_population(
        graph.split_leaf_queries, "split-leaf population"
    )
    if any(
        completed_population.get(query_id) != query
        or root_population.get(query_id) != query
        for query_id, query in protected_query_by_id.items()
    ) or set(protected_query_by_id).intersection(
        set(split_parent_population) | set(split_leaf_population)
    ):
        raise ValueError("reference gate batch terminal classification differs")

    relevant_batches = [
        candidate
        for candidate in graph.query_batches
        if reference_projection(candidate.queries, "query-batch")
    ]
    batch_population = reference_projection(
        tuple(query for batch in graph.query_batches for query in batch.queries),
        "query-batch",
    )
    if batch_population != expected_by_id or relevant_batches != [batch]:
        raise ValueError("reference terminal query batch population differs")

    protected_batch_lineage = set(gate_batch_ids)
    changed = True
    while changed:
        changed = False
        for candidate in graph.query_batches:
            if (
                candidate.derived_from_sha256 in protected_batch_lineage
                and candidate.batch_id not in protected_batch_lineage
            ):
                protected_batch_lineage.add(candidate.batch_id)
                changed = True
    if any(
        receipt.parent_query_id in protected_query_by_id
        or any(
            child_id in protected_query_by_id for child_id in receipt.child_query_ids
        )
        or receipt.parent_batch_id in protected_batch_lineage
        or receipt.child_batch_id in protected_batch_lineage
        for receipt in graph.split_receipts
    ):
        raise ValueError("reference query/batch lineage appears in split receipts")

    if any(
        query.run_spec_sha256 != timezone_gate.run_spec_sha256
        or query.acquisition_contract_sha256
        != timezone_gate.acquisition_contract_sha256
        for authenticated_batch in (static_batch, batch)
        for query in authenticated_batch.queries
    ):
        raise ValueError("reference query RunSpec/acquisition binding differs")

    game_by_season: dict[int, NormalizedSnapshot] = {}
    season_games: dict[int, list[dict[str, object]]] = {
        season: [] for season in _GAME_SEASONS
    }
    observed: dict[tuple[int, str], tuple[bool, bool]] = {}
    observed_teams: dict[int, set[int]] = {season: set() for season in _GAME_SEASONS}
    observed_venues: dict[int, set[int]] = {season: set() for season in _GAME_SEASONS}
    schedule_hashes: set[str] = set()
    seen_game_pks: set[int] = set()
    for snapshot in game_snapshots:
        mapping = (
            snapshot.as_mapping() if isinstance(snapshot, NormalizedSnapshot) else None
        )
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.run_id != run_id
            or snapshot.source != "mlb_schedule_game_feed"
            or snapshot.grain != "season_game_evidence_snapshot"
            or snapshot.season in game_by_season
            or mapping is None
            or _validate_normalized_snapshot_mapping(mapping) != mapping
            or snapshot.canonical_bytes != canonical_json_bytes(mapping) + b"\n"
            or snapshot.sha256 != hashlib.sha256(snapshot.canonical_bytes).hexdigest()
        ):
            raise ValueError("reference game snapshot authority differs")
        game_by_season[snapshot.season] = snapshot
        for raw_game in mapping["payload"]["games"]:  # type: ignore[index]
            game = dict(raw_game)
            game_pk = game.get("game_pk")
            venue_id = game.get("venue_id")
            away = game.get("away_team_id")
            home = game.get("home_team_id")
            official = _canonical_day(
                game.get("official_date"), "reference game official date"
            )
            neutral = game.get("neutral_site")
            alternate = game.get("alternate_site")
            schedule_hash = game.get("schedule_query_sidecar_sha256")
            if (
                type(game_pk) is not int
                or game_pk <= 0
                or game_pk in seen_game_pks
                or type(venue_id) is not int
                or venue_id <= 0
                or type(away) is not int
                or away <= 0
                or type(home) is not int
                or home <= 0
                or official.year != snapshot.season
                or type(neutral) is not bool
                or type(alternate) is not bool
                or not isinstance(schedule_hash, str)
                or _SHA256.fullmatch(schedule_hash) is None
            ):
                raise ValueError("reference game observation differs")
            key = (venue_id, official.isoformat())
            disposition = (neutral, alternate)
            if key in observed and observed[key] != disposition:
                raise ValueError("conflicting alternate/neutral venue disposition")
            observed[key] = disposition
            seen_game_pks.add(game_pk)
            schedule_hashes.add(schedule_hash)
            observed_teams[snapshot.season].update((away, home))
            observed_venues[snapshot.season].add(venue_id)
            season_games[snapshot.season].append(
                {
                    "alternate_site": alternate,
                    "away_team_id": away,
                    "game_pk": game_pk,
                    "home_team_id": home,
                    "neutral_site": neutral,
                    "official_date": official.isoformat(),
                    "venue_id": venue_id,
                }
            )
    if set(game_by_season) != set(_GAME_SEASONS):
        raise ValueError("reference requires seven verified game snapshots")

    record_by_id: dict[str, tuple[VerifiedQueryRecord, dict[str, object], bytes]] = {}
    team_by_season: dict[int, tuple[VerifiedQueryRecord, dict[str, object], bytes]] = {}
    venue_by_id: dict[int, tuple[VerifiedQueryRecord, dict[str, object], bytes]] = {}
    for record in records:
        sidecar, raw = _verified_reference_record(record)
        query = record.query
        query_id = record.query_id
        if (
            query_id not in expected_by_id
            or expected_by_id[query_id] != query
            or query_id in record_by_id
            or query.parent_query_id is not None
            or query.expected_media_type != "application/json"
            or query.parser_version != "v5.2.1"
        ):
            raise ValueError("reference verified record population differs")
        semantics = sidecar.get("semantics")
        if not isinstance(semantics, Mapping):
            raise ValueError("reference sidecar semantics differ")
        if query.endpoint == "https://statsapi.mlb.com/api/v1/teams":
            season = query.requested_range.get("season")
            if (
                type(season) is not int
                or season not in _GAME_SEASONS
                or season in team_by_season
                or dict(query.requested_range) != {"season": season, "entity": "teams"}
            ):
                raise ValueError("reference team query partition differs")
            team_by_season[season] = (record, sidecar, raw)
        else:
            venue_id = query.requested_range.get("venue_id")
            if (
                type(venue_id) is not int
                or venue_id <= 0
                or venue_id in venue_by_id
                or query.endpoint
                != f"https://statsapi.mlb.com/api/v1/venues/{venue_id}"
                or dict(query.requested_range) != {"venue_id": venue_id}
            ):
                raise ValueError("reference venue query partition differs")
            venue_by_id[venue_id] = (record, sidecar, raw)
        record_by_id[query_id] = (record, sidecar, raw)
    if (
        set(record_by_id) != set(expected_by_id)
        or set(team_by_season) != set(_GAME_SEASONS)
        or set(venue_by_id)
        != {venue_id for values in observed_venues.values() for venue_id in values}
    ):
        raise ValueError("reference READY record coverage differs")

    candidate_value = _plain_json(timezone_gate.candidate)
    mapping_value = _plain_json(timezone_gate.mapping)
    reviews_value = tuple(_plain_json(review) for review in timezone_gate.reviews)
    seal_value = _plain_json(timezone_gate.seal)
    if (
        not isinstance(candidate_value, Mapping)
        or not isinstance(mapping_value, Mapping)
        or not all(isinstance(review, Mapping) for review in reviews_value)
        or not isinstance(seal_value, Mapping)
    ):
        raise ValueError("reference timezone gate mappings differ")
    candidate = _timezone_candidate_object(candidate_value)
    expected_mapping = _timezone_mapping_from_candidate(
        candidate, require_resolved=True
    )
    if dict(mapping_value) != expected_mapping:
        raise ValueError("reference timezone mapping differs from candidate")
    if (
        _canonical_file_sha256(candidate) != timezone_gate.candidate_sha256
        or _canonical_file_sha256(dict(mapping_value)) != timezone_gate.mapping_sha256
        or len(reviews_value) != 2
        or len(timezone_gate.review_sha256s) != 2
        or _canonical_file_sha256(dict(seal_value)) != timezone_gate.seal_sha256
    ):
        raise ValueError("reference timezone gate artifact hash differs")
    candidate_binding = {
        "path": "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json",
        "sha256": timezone_gate.candidate_sha256,
    }
    expected_roles = (
        "TIMEZONE_COVERAGE_REVIEW",
        "TIMEZONE_SEMANTIC_REVIEW",
    )
    review_paths = (
        "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
        "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
    )
    review_bindings: list[dict[str, object]] = []
    identities: set[str] = set()
    for review_value, digest, role, path in zip(
        reviews_value,
        timezone_gate.review_sha256s,
        expected_roles,
        review_paths,
        strict=True,
    ):
        review = _timezone_review_object(canonical_json_bytes(review_value) + b"\n")
        if (
            _canonical_file_sha256(review) != digest
            or review["decision"] != "APPROVED"
            or review["reviewer_role"] != role
            or review["candidate"] != candidate_binding
            or review["reviewed_venue_ids"] != candidate["venue_scope"]
        ):
            raise ValueError("reference timezone review authority differs")
        identity = str(review["reviewer_identity"])
        identities.add(identity)
        review_bindings.append(
            {
                "path": path,
                "reviewer_identity": identity,
                "reviewer_role": role,
                "sha256": digest,
            }
        )
    if len(identities) != 2:
        raise ValueError("reference timezone reviewers must be distinct")
    seal = _timezone_seal_object(canonical_json_bytes(seal_value) + b"\n")
    if seal != dict(seal_value) or seal != {
        "candidate": candidate_binding,
        "mapping": {
            "path": "config/v5_2_qd/venue_timezones.json",
            "sha256": timezone_gate.mapping_sha256,
        },
        "occurred_at_utc": seal["occurred_at_utc"],
        "receipt_kind": "TIMEZONE_MAPPING_SEAL",
        "reviews": review_bindings,
        "schema_version": 1,
        "venue_scope": candidate["venue_scope"],
    }:
        raise ValueError("reference timezone seal authority differs")

    expected_observations = [
        {
            "alternate_site": alternate,
            "neutral_site": neutral,
            "official_date": official,
            "venue_id": venue_id,
        }
        for (venue_id, official), (neutral, alternate) in sorted(observed.items())
    ]
    expected_venue_identities = [
        venue_by_id[venue_id][1]["semantics"]["venues"][0]  # type: ignore[index]
        for venue_id in sorted(venue_by_id)
    ]
    expected_venue_hashes = sorted(
        item[0].query_sidecar_sha256 for item in venue_by_id.values()
    )
    if (
        candidate["run_spec_sha256"] != timezone_gate.run_spec_sha256
        or candidate["acquisition_contract_sha256"]
        != timezone_gate.acquisition_contract_sha256
        or candidate["query_batch_ids"] != sorted(gate_batch_ids)
        or candidate["schedule_sidecar_sha256s"] != sorted(schedule_hashes)
        or candidate["venue_scope"] != sorted(venue_by_id)
        or candidate["venue_sidecar_sha256s"] != expected_venue_hashes
        or candidate["venues"] != expected_venue_identities
        or candidate["observed_venue_dates"] != expected_observations
    ):
        raise ValueError("reference timezone candidate source authority differs")

    mapping_rows = expected_mapping["timezone_rows"]
    snapshots: list[NormalizedSnapshot] = []
    for season in _GAME_SEASONS:
        games = sorted(
            season_games[season],
            key=lambda game: (game["official_date"], game["game_pk"]),
        )
        venue_ids = observed_venues[season]
        selected_rows = [
            row
            for row in mapping_rows
            if int(row["venue_id"]) in venue_ids
            and any(
                int(game["venue_id"]) == int(row["venue_id"])
                and _canonical_day(row["effective_start"], "reference timezone start")
                <= _canonical_day(game["official_date"], "reference game date")
                <= _canonical_day(row["effective_end"], "reference timezone end")
                for game in games
            )
        ]
        team_record, _team_sidecar, team_raw = team_by_season[season]
        selected_venues = [venue_by_id[venue_id] for venue_id in sorted(venue_ids)]
        payload = recompute_reference_sidecar(
            [team_raw],
            [item[2] for item in selected_venues],
            selected_rows,
            games,
        )
        if (
            payload["team_payload_sha256s"] != [team_record.raw_sha256]
            or payload["venue_payload_sha256s"]
            != sorted(str(item[0].raw_sha256) for item in selected_venues)
            or {int(row["team_id"]) for row in payload["teams"]}
            < observed_teams[season]
            or [int(row["venue_id"]) for row in payload["venues"]] != sorted(venue_ids)
            or payload["timezone_rows"] != selected_rows
        ):
            raise ValueError("reference seasonal aggregate authority differs")
        direct_records = [team_record] + [item[0] for item in selected_venues]
        snapshots.append(
            _reference_normalized_snapshot(run_id, season, direct_records, payload)
        )
    return tuple(snapshots)


def _read_verified_query_record(
    store: V52EvidenceStore,
    query: LogicalQuery,
    *,
    duplicate_gamepk_disposition_authority: (
        DuplicateGamePkDispositionAuthority | None
    ) = None,
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
            if duplicate_gamepk_disposition_authority is not None:
                if (
                    query.endpoint
                    != "disposition://SCHEDULE_DUPLICATE_GAMEPK_565534"
                    or query != duplicate_gamepk_disposition_authority.query
                ):
                    raise ValueError(
                        "schedule disposition authority query binding differs"
                    )
                semantics = dict(
                    recompute_query_sidecar(
                        query,
                        b"",
                        authority=duplicate_gamepk_disposition_authority,
                    )
                )
            else:
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
                raise ValueError("derived query sidecar semantic recomputation differs")
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

    if duplicate_gamepk_disposition_authority is None:
        store.inspect_verified_success(query, recompute_verified_read)
    else:
        store.inspect_verified_success(
            query,
            recompute_verified_read,
            authority=duplicate_gamepk_disposition_authority,
        )
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
