"""Small deterministic authorities used by snapshot-store replay tests."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id
from mlb_backtest.v52_game_evidence_inventory import VerifiedGameEvidenceInventory
from mlb_backtest.v52_schedule_inventory import VerifiedScheduleAnchorInventory
from mlb_backtest.v52_timezone_review import VerifiedTimezoneGate
from mlb_backtest.v52_verify_sources import (
    NormalizedSnapshot,
    SnapshotReplayInputs,
    VerifiedQueryRecord,
    _TerminalQueryGraph,
    snapshot_sort_key,
)
from mlb_backtest.v52_source_contracts import canonical_json_bytes


_RUN_ID = "fixture-run"
_SOURCES = (
    "mlb_schedule_game_feed",
    "baseball_savant_statcast",
    "mlb_stats_pitching",
    "venue_team_timezone_table",
    "prior_schedule_history",
    "mlb_stats_pitcher_bf",
)
_STATCAST_MONTHS = (7, 7, 3, 7, 7, 8, 7)


def _fake_snapshot(
    season: int,
    source: str,
    ordinal: int = 0,
    *,
    run_id: str = _RUN_ID,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
) -> NormalizedSnapshot:
    grains = {
        "mlb_schedule_game_feed": "season_game_evidence_snapshot",
        "baseball_savant_statcast": "pitch",
        "mlb_stats_pitching": "season_d1_league_pitching_reference",
        "venue_team_timezone_table": "effective_dated_venue_team_reference",
        "prior_schedule_history": "verified_game_projection",
        "mlb_stats_pitcher_bf": "player_career_batters_faced_reference",
    }
    query_id = f"1{ordinal:063x}"
    sidecar = f"2{ordinal:063x}"
    payload: dict[str, object] = {"fixture_ordinal": ordinal}
    if source == "baseball_savant_statcast":
        payload["parent_coverage"] = {
            "inclusive_start": coverage_start or f"{season}-01-01",
            "inclusive_end": coverage_end or f"{season}-01-31",
        }
    envelope = {
        "grain": grains[source],
        "logical_query_ids": [query_id],
        "payload": payload,
        "run_id": run_id,
        "schema_version": 1,
        "season": season,
        "source": source,
        "source_query_sidecar_sha256s": [sidecar],
        "split_receipt_sha256s": [],
        "status": "READY",
    }
    contents = canonical_json_bytes(envelope) + b"\n"
    digest = hashlib.sha256(contents).hexdigest()
    return NormalizedSnapshot(
        schema_version=1,
        run_id=run_id,
        season=season,
        source=source,  # type: ignore[arg-type]
        grain=grains[source],  # type: ignore[arg-type]
        status="READY",
        logical_query_ids=(query_id,),
        source_query_sidecar_sha256s=(sidecar,),
        split_receipt_sha256s=(),
        payload=MappingProxyType(payload),
        canonical_bytes=contents,
        sha256=digest,
    )


def _fake_81() -> tuple[NormalizedSnapshot, ...]:
    snapshots: list[NormalizedSnapshot] = []
    ordinal = 0
    for season, statcast_count in zip(range(2018, 2025), _STATCAST_MONTHS, strict=True):
        for source in _SOURCES:
            count = statcast_count if source == "baseball_savant_statcast" else 1
            for month_ordinal in range(count):
                ordinal += 1
                snapshots.append(
                    _fake_snapshot(
                        season,
                        source,
                        ordinal,
                        coverage_start=(
                            f"{season}-0{month_ordinal + 1}-01"
                            if source == "baseball_savant_statcast"
                            else None
                        ),
                        coverage_end=(
                            f"{season}-0{month_ordinal + 1}-28"
                            if source == "baseball_savant_statcast"
                            else None
                        ),
                    )
                )
    return tuple(snapshots)


def _fake_query(index: int) -> VerifiedQueryRecord:
    query = LogicalQuery(
        source="fixture",
        grain="fixture",
        endpoint="fixture://query",
        parameters={"index": index},
        requested_range={"index": index},
        expected_media_type="application/json",
        parser_version="fixture",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    query_id = logical_query_id(query)
    sidecar_bytes = canonical_json_bytes({"query_id": query_id}) + b"\n"
    return VerifiedQueryRecord(
        query=query,
        query_id=query_id,
        query_sidecar_bytes=sidecar_bytes,
        query_sidecar_sha256=hashlib.sha256(sidecar_bytes).hexdigest(),
        raw_sha256=None,
        storage_mode="DERIVED_DISPOSITION",
        retained_raw_bytes=None,
    )


def _replay_inputs() -> SnapshotReplayInputs:
    game_snapshots = tuple(
        snapshot
        for snapshot in _fake_81()
        if snapshot.source == "mlb_schedule_game_feed"
    )
    inventory = VerifiedGameEvidenceInventory(MappingProxyType({}), Path("fixture"), "c" * 64)
    schedule = VerifiedScheduleAnchorInventory(MappingProxyType({}), Path("fixture"), "d" * 64)
    timezone = VerifiedTimezoneGate(
        MappingProxyType({}),
        MappingProxyType({}),
        (),
        MappingProxyType({}),
        "e" * 64,
        "f" * 64,
        (),
        "0" * 64,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        ("1" * 64, "2" * 64),
        ("3" * 64, "4" * 64),
    )
    graph = _TerminalQueryGraph(
        checkpoint=object(),
        statcast_root_batch=object(),
        query_batches=(),
        split_receipts=(),
        split_parent_queries=(),
        completed_queries=(),
        root_queries=(),
        split_leaf_queries=(),
    )
    return SnapshotReplayInputs(
        run_id=_RUN_ID,
        graph=graph,
        records=tuple(_fake_query(index) for index in range(81)),
        game_snapshots=game_snapshots,
        game_inventory=inventory,
        timezone_gate=timezone,
        schedule_inventory=schedule,
    )


fake_81 = _fake_81
replay_inputs = _replay_inputs


def store_inputs(*, current_phase: str = "SOURCES_READY"):
    """Return sealed, deterministic inputs for source-inventory tests."""
    from mlb_backtest.v52_snapshot_store import (
        AuthorityBinding,
        AuthorityBindings,
        SnapshotStoreInputs,
        TerminalCheckpointBinding,
    )

    bindings = AuthorityBindings(
        run_spec=AuthorityBinding("audit/v5_2_qd/historical/run_spec.json", "a" * 64),
        acquisition_contract=AuthorityBinding(
            "audit/v5_2_qd/historical/acquisition_contract.json", "b" * 64
        ),
        terminal_checkpoint=TerminalCheckpointBinding(
            "audit/v5_2_qd/historical/acquisition/checkpoint.json", "c" * 64, 81, "REMAINING"
        ),
        schedule_inventory=AuthorityBinding(
            "audit/v5_2_qd/historical/schedule_inventory.json", "d" * 64
        ),
        game_evidence_inventory=AuthorityBinding(
            "audit/v5_2_qd/historical/game_evidence_inventory.json", "e" * 64
        ),
        timezone_gate=AuthorityBinding(
            "audit/v5_2_qd/historical/timezone_gate.json", "f" * 64
        ),
    )
    return SnapshotStoreInputs(replay_inputs(), bindings, current_phase)


@contextmanager
def patched_store_replay():
    """Keep inventory construction hermetic while exercising its replay boundary."""
    with patch(
        "mlb_backtest.v52_snapshot_store.replay_verified_snapshots",
        return_value=tuple(sorted(fake_81(), key=snapshot_sort_key)),
    ):
        yield
