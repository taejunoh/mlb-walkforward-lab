from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, call, patch

import jsonschema

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.execution_registry import AcquisitionCheckpoint
from mlb_backtest.v52_evidence import (
    AttemptCapture,
    LogicalQuery,
    V52EvidenceStore,
    canonical_query_bytes,
    logical_query_id,
)
from mlb_backtest.v52_query_plan import QueryBatch, SplitReceipt
from mlb_backtest.v52_source_contracts import canonical_sha256, recompute_query_sidecar
from tests.test_v52_source_schedule import (
    _box_query,
    _boxscore_raw,
    _query,
    _schedule_raw,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "mlb_backtest/v52_verify_sources.py"
SOURCE_FIXTURES = ROOT / "tests/fixtures/v5_2_qd/sources"
NORMALIZED_SNAPSHOT_SCHEMA = (
    ROOT / "schemas/v5_2_normalized_snapshot.schema.json"
)
_VENUE_GUARD_PAYLOAD = json.loads((SOURCE_FIXTURES / "venues_mini.json").read_bytes())
_VENUE_GUARD_PAYLOAD["venues"] = _VENUE_GUARD_PAYLOAD["venues"][:1]
PARSER_IMPORT_GUARD_CASES = (
    "schedule",
    "statcast",
    "d1_pitching",
    "career_bf",
    "team_venue_reference",
)
PARSER_IMPORT_GUARD_SCRIPT = r"""
import importlib.abc
import sys

BLOCKED = (
    "aiohttp",
    "http.client",
    "httpx",
    "mlb_backtest.sources",
    "mlb_backtest.v52_http",
    "requests",
    "urllib.error",
    "urllib.request",
)


class OfflineImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(
            fullname == blocked or fullname.startswith(blocked + ".")
            for blocked in BLOCKED
        ):
            raise ImportError("NETWORK_MODULE_BLOCKED:" + fullname)
        return None


sys.meta_path.insert(0, OfflineImportGuard())
if len(sys.argv) > 2 and sys.argv[2] == "preload":
    import types

    sys.modules["requests"] = types.ModuleType("requests")

preloaded = sorted(
    module_name
    for module_name, module in sys.modules.items()
    if module is not None
    and any(
        module_name == blocked or module_name.startswith(blocked + ".")
        for blocked in BLOCKED
    )
)
if preloaded:
    raise ImportError("NETWORK_MODULE_PRELOADED:" + ",".join(preloaded))

from mlb_backtest.v52_evidence import LogicalQuery
from mlb_backtest.v52_source_contracts import recompute_query_sidecar


case = sys.argv[1]
if case == "schedule":
    query = LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/schedule",
        parameters={
            "sportId": 1,
            "gameType": "R",
            "startDate": "2024-03-20",
            "endDate": "2024-09-30",
            "hydrate": "probablePitcher,lineups,venue,linescore",
        },
        requested_range={
            "year": 2024,
            "start_date": "2024-03-20",
            "end_date": "2024-09-30",
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    raw = __SCHEDULE_RAW__
    semantics = recompute_query_sidecar(query, raw)
    if semantics["game_pks"] != [9001, 9002]:
        raise AssertionError("schedule parser dispatch differs")
elif case == "statcast":
    query = LogicalQuery(
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
        parameters={
            "all": "true",
            "type": "details",
            "player_type": "batter",
            "game_date_gt": "2024-04-01",
            "game_date_lt": "2024-04-30",
            "hfGT": "R|",
            "group_by": "name-year",
            "sort_col": "pitches",
            "player_event_sort": "api_p_release_speed",
            "sort_order": "desc",
            "min_pitches": "0",
        },
        requested_range={
            "year": 2024,
            "start_date": "2024-04-01",
            "end_date": "2024-04-30",
        },
        expected_media_type="text/csv",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    semantics = recompute_query_sidecar(query, __STATCAST_RAW__)
    if semantics["game_pks"] != [9001]:
        raise AssertionError("Statcast parser dispatch differs")
elif case == "d1_pitching":
    query = LogicalQuery(
        source="mlb_stats_pitching",
        grain="season_d1_league_pitching_reference",
        endpoint="https://statsapi.mlb.com/api/v1/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "sportIds": 1,
            "gameType": "R",
            "playerPool": "ALL",
            "limit": 5000,
            "startDate": "2024-03-20",
            "endDate": "2024-04-01",
        },
        requested_range={
            "season": 2024,
            "start_date": "2024-03-20",
            "as_of_date": "2024-04-01",
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": "2024-04-01"},
            "response_completeness": {
                "limit": 5000,
                "reject_total_splits_at_or_above_limit": True,
            },
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    semantics = recompute_query_sidecar(query, __PITCHING_RAW__)
    if semantics["batters_faced"] != 75:
        raise AssertionError("D-1 pitching parser dispatch differs")
elif case == "career_bf":
    query = LogicalQuery(
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        endpoint="https://statsapi.mlb.com/api/v1/people/101/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "gameType": "R",
            "sportIds": 1,
            "startDate": "1876-01-01",
            "endDate": "2017-12-31",
        },
        requested_range={
            "pitcher_id": 101,
            "inclusive_end": "2017-12-31",
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": "2024-04-30"},
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )
    semantics = recompute_query_sidecar(query, b'{"stats":[]}')
    if semantics["baseline_batters_faced"] != 0:
        raise AssertionError("career BF parser dispatch differs")
elif case == "team_venue_reference":
    common = {
        "source": "venue_team_timezone_table",
        "grain": "effective_dated_venue_team_reference",
        "expected_media_type": "application/json",
        "parser_version": "v5.2.1",
        "parent_query_id": None,
        "run_spec_sha256": "a" * 64,
        "acquisition_contract_sha256": "b" * 64,
    }
    team_query = LogicalQuery(
        **common,
        endpoint="https://statsapi.mlb.com/api/v1/teams",
        parameters={
            "sportId": 1,
            "season": 2024,
            "hydrate": "venue(location)",
        },
        requested_range={"season": 2024, "entity": "teams"},
    )
    venue_query = LogicalQuery(
        **common,
        endpoint="https://statsapi.mlb.com/api/v1/venues/31",
        parameters={"hydrate": "location"},
        requested_range={"venue_id": 31},
    )
    team_semantics = recompute_query_sidecar(team_query, __TEAM_RAW__)
    venue_semantics = recompute_query_sidecar(venue_query, __VENUE_RAW__)
    if [row["team_id"] for row in team_semantics["teams"]] != [10, 20]:
        raise AssertionError("team reference parser dispatch differs")
    if [row["venue_id"] for row in venue_semantics["venues"]] != [31]:
        raise AssertionError("venue reference parser dispatch differs")
else:
    raise AssertionError("unknown parser guard case: " + case)

for module_name, module in tuple(sys.modules.items()):
    if module is None or not module_name.startswith("mlb_backtest"):
        continue
    exposed = {
        name
        for name in vars(module)
        if name in {"fetch", "fetch_attempt", "fetch_bytes", "urlopen"}
        or name.startswith("fetch_")
    }
    if exposed:
        raise AssertionError(
            "NETWORK_SYMBOL_EXPOSED:"
            + module_name
            + ":"
            + ",".join(sorted(exposed))
        )
"""
for _placeholder, _raw in (
    ("__SCHEDULE_RAW__", _schedule_raw()),
    ("__STATCAST_RAW__", (SOURCE_FIXTURES / "statcast_mini.csv").read_bytes()),
    ("__PITCHING_RAW__", (SOURCE_FIXTURES / "pitching_mini.json").read_bytes()),
    ("__TEAM_RAW__", (SOURCE_FIXTURES / "team_mini.json").read_bytes()),
    (
        "__VENUE_RAW__",
        json.dumps(_VENUE_GUARD_PAYLOAD, separators=(",", ":")).encode(),
    ),
):
    PARSER_IMPORT_GUARD_SCRIPT = PARSER_IMPORT_GUARD_SCRIPT.replace(
        _placeholder, repr(_raw)
    )


def _snapshot_tree(
    root: Path,
) -> dict[Path, tuple[int, int, int, int, bytes | None]]:
    snapshot = {}
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        snapshot[path.relative_to(root)] = (
            stat.S_IFMT(metadata.st_mode),
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
        )
    return snapshot


def _owned_schedule_evidence(
    root: Path,
) -> tuple[V52EvidenceStore, LogicalQuery, bytes]:
    raw = _schedule_raw()
    query = _query()
    semantics = recompute_query_sidecar(query, raw)
    store = V52EvidenceStore(root)
    capture = AttemptCapture(
        started_at_utc="2026-07-16T12:00:00.000000Z",
        ended_at_utc="2026-07-16T12:00:01.000000Z",
        final_url=query.endpoint,
        http_status=200,
        response_headers=(
            ("content-type", "application/json"),
            ("content-length", str(len(raw))),
        ),
        body=raw,
        error_classification=None,
    )
    with store.acquire_query(query) as lease:
        store.publish_fetched(
            query,
            store.next_attempt_number(lease),
            capture,
            semantics,
        )
    return store, query, raw


def _owned_fetched_evidence(
    root: Path,
    query: LogicalQuery,
    raw: bytes,
) -> V52EvidenceStore:
    semantics = recompute_query_sidecar(query, raw)
    store = V52EvidenceStore(root)
    capture = AttemptCapture(
        started_at_utc="2026-07-16T12:00:00.000000Z",
        ended_at_utc="2026-07-16T12:00:01.000000Z",
        final_url=query.endpoint,
        http_status=200,
        response_headers=(
            ("content-type", query.expected_media_type),
            ("content-length", str(len(raw))),
        ),
        body=raw,
        error_classification=None,
    )
    with store.acquire_query(query) as lease:
        store.publish_fetched(
            query,
            store.next_attempt_number(lease),
            capture,
            semantics,
        )
    return store


def _forge_sidecar_semantics(root: Path, query: LogicalQuery) -> None:
    verified = V52EvidenceStore.open_read_only(root).read_verified_success(query)
    success_path = verified.published_evidence.success_receipt_path
    success = json.loads(success_path.read_bytes())
    sidecar_path = root / str(success["query_sidecar_path"])
    sidecar = json.loads(sidecar_path.read_bytes())
    sidecar["semantics"]["game_pks"] = [999999]
    sidecar_contents = canonical_json_bytes(sidecar) + b"\n"
    sidecar_sha256 = hashlib.sha256(sidecar_contents).hexdigest()
    replacement = sidecar_path.with_name(f"{sidecar_sha256}.json")
    replacement.write_bytes(sidecar_contents)
    sidecar_path.unlink()
    success["query_sidecar_path"] = replacement.relative_to(root).as_posix()
    success["query_sidecar_sha256"] = sidecar_sha256
    success_path.write_bytes(canonical_json_bytes(success) + b"\n")


def _batch_artifact_sha256(batch: QueryBatch) -> str:
    record = {
        "batch_id": batch.batch_id,
        "derived_from_sha256": batch.derived_from_sha256,
        "queries": [
            json.loads(canonical_query_bytes(item)) for item in batch.queries
        ],
        "schema_version": 1,
        "stage": batch.stage,
    }
    return hashlib.sha256(canonical_json_bytes(record) + b"\n").hexdigest()


def _graph_fixture(*, recursive: bool = False) -> SimpleNamespace:
    def query(label: str, parent: str | None = None) -> LogicalQuery:
        base = _query()
        return dataclasses.replace(
            base,
            parameters={**base.parameters, "fixture": label},
            requested_range={**base.requested_range, "fixture": label},
            parent_query_id=parent,
        )

    root, parent = query("root"), query("parent")
    parent_id = logical_query_id(parent)
    left, right = query("left", parent_id), query("right", parent_id)
    batches = [
        QueryBatch("1" * 64, "REMAINING", None, (root, parent)),
        QueryBatch("2" * 64, "REMAINING", "1" * 64, (left, right)),
    ]

    def receipt(index: int, parent_batch, parent_query, child_batch, depth):
        return SplitReceipt(
            str(5 + index) * 64,
            str(7 + index) * 64,
            "REMAINING",
            parent_batch.batch_id,
            logical_query_id(parent_query),
            "ROW_CAP",
            1,
            depth,
            child_batch.batch_id,
            tuple(logical_query_id(item) for item in child_batch.queries),
            "2026-07-16T12:00:00.000000Z",
        )

    receipts = [receipt(0, batches[0], parent, batches[1], 1)]
    completed = [root, left, right]
    if recursive:
        left_id = logical_query_id(left)
        grand = (query("grand-left", left_id), query("grand-right", left_id))
        batches.append(QueryBatch("3" * 64, "REMAINING", "2" * 64, grand))
        receipts.append(receipt(1, batches[1], left, batches[2], 2))
        completed = [root, right, *grand]
    statcast_root_queries = tuple(
        dataclasses.replace(
            query(f"sealed-statcast-root-{index:02d}"),
            source="baseball_savant_statcast",
            grain="pitch",
            endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            expected_media_type="text/csv",
        )
        for index in range(46)
    )
    statcast_root = QueryBatch(
        "4" * 64,
        "REMAINING",
        None,
        statcast_root_queries,
    )
    statcast_root_sha256 = _batch_artifact_sha256(statcast_root)
    statcast_root_path = (
        "audit/v5_2_qd/historical/acquisition/query_batches/"
        f"{statcast_root.batch_id}.json"
    )
    checkpoint = AcquisitionCheckpoint(
        4,
        "a" * 64,
        "b" * 64,
        "REMAINING",
        "2026-07-16T12:00:00.000000Z",
        "c" * 40,
        "d" * 64,
        tuple(batch.batch_id for batch in batches),
        tuple("e" * 64 for _ in batches),
        (),
        (statcast_root_sha256,),
        tuple(
            item.receipt_sha256
            for item in sorted(receipts, key=lambda x: x.parent_query_id)
        ),
        tuple(sorted(logical_query_id(item) for item in completed)),
        (),
        (),
        tuple(sorted(item.parent_query_id for item in receipts)),
        (statcast_root_path,),
    )
    return SimpleNamespace(
        checkpoint=checkpoint,
        batches=tuple(batches),
        receipts=tuple(receipts),
        root=root,
        parent=parent,
        left=left,
        right=right,
        completed=tuple(completed),
        statcast_root=statcast_root,
        statcast_root_sha256=statcast_root_sha256,
        statcast_root_path=statcast_root_path,
    )


def _normalized_game_authority() -> SimpleNamespace:
    from mlb_backtest.v52_game_evidence_inventory import (
        VerifiedGameEvidenceInventory,
    )
    from mlb_backtest.v52_schedule_inventory import (
        VerifiedScheduleAnchorInventory,
    )
    from mlb_backtest.v52_sources.schedule import (
        recompute_boxscore_sidecar,
        recompute_game_evidence_snapshot,
        recompute_schedule_anchor_sidecar,
    )
    from mlb_backtest.v52_timezone_review import VerifiedTimezoneGate
    from mlb_backtest.v52_verify_sources import VerifiedQueryRecord

    def record(query: LogicalQuery, sidecar: dict[str, object]) -> VerifiedQueryRecord:
        contents = canonical_json_bytes(sidecar) + b"\n"
        return VerifiedQueryRecord(
            query=query,
            query_id=logical_query_id(query),
            query_sidecar_bytes=contents,
            query_sidecar_sha256=hashlib.sha256(contents).hexdigest(),
            raw_sha256=str(sidecar["raw_sha256"]),
            storage_mode="FETCHED_RAW",
            retained_raw_bytes=None,
        )

    base_schedule = recompute_schedule_anchor_sidecar(_schedule_raw(), _query())
    base_boxscore = recompute_boxscore_sidecar(_boxscore_raw(), _box_query())
    records = []
    per_season_payloads = []
    season_sources = []
    schedule_hashes = []
    for season in range(2018, 2025):
        offset = season - 2018
        played_pk, unplayed_pk = 800_001 + offset * 10, 800_002 + offset * 10
        official_date = f"{season}-04-02"
        schedule_query = dataclasses.replace(
            _query(),
            parameters={
                **_query().parameters,
                "startDate": f"{season}-03-20",
                "endDate": f"{season}-09-30",
            },
            requested_range={
                "year": season,
                "start_date": f"{season}-03-20",
                "end_date": f"{season}-09-30",
            },
        )
        schedule_sidecar = json.loads(canonical_json_bytes(base_schedule))
        schedule_sidecar["query_id"] = logical_query_id(schedule_query)
        schedule_sidecar["raw_sha256"] = hashlib.sha256(
            f"schedule-{season}".encode()
        ).hexdigest()
        anchors = schedule_sidecar["semantics"]["anchors"]
        for anchor, game_pk in zip(
            anchors, (played_pk, unplayed_pk), strict=True
        ):
            anchor["game_pk"] = game_pk
            anchor["official_date"] = official_date
            anchor["scheduled_start_utc"] = f"{season}-04-02T17:05:00Z"
            anchor["season"] = season
            anchor["season_start_date"] = f"{season}-03-20"
        schedule_sidecar["semantics"]["game_pks"] = [played_pk, unplayed_pk]
        schedule_sidecar["semantics"]["played_game_pks"] = [played_pk]
        schedule_sidecar["semantics"]["unplayed_game_pks"] = [unplayed_pk]
        schedule_record = record(schedule_query, schedule_sidecar)

        box_query = dataclasses.replace(
            _box_query(),
            endpoint=(
                f"https://statsapi.mlb.com/api/v1/game/{played_pk}/boxscore"
            ),
            requested_range={
                "season": season,
                "game_pk": played_pk,
                "official_date": official_date,
            },
        )
        box_sidecar = json.loads(canonical_json_bytes(base_boxscore))
        box_sidecar["query_id"] = logical_query_id(box_query)
        box_sidecar["raw_sha256"] = hashlib.sha256(
            f"boxscore-{season}".encode()
        ).hexdigest()
        box_sidecar["semantics"]["game_pk"] = played_pk
        box_sidecar["semantics"]["official_date"] = official_date
        box_record = record(box_query, box_sidecar)
        records.extend((box_record, schedule_record))
        schedule_hashes.append(schedule_record.query_sidecar_sha256)

        payload = dict(
            recompute_game_evidence_snapshot(
                [schedule_sidecar],
                {
                    played_pk: (
                        box_record.query_sidecar_bytes,
                        box_record.query_sidecar_sha256,
                    )
                },
            )
        )
        per_season_payloads.append(payload)
        season_sources.append(
            {
                "season": season,
                "query_id": schedule_record.query_id,
                "query_sidecar": {
                    "path": f"fixture/{season}.json",
                    "sha256": schedule_record.query_sidecar_sha256,
                },
            }
        )

    schedule_sha256 = "8" * 64
    mapping_sha256 = "7" * 64
    candidate_sha256 = "6" * 64
    review_sha256s = ("5" * 64, "4" * 64)
    seal_sha256 = "3" * 64
    schedule_inventory = VerifiedScheduleAnchorInventory(
        payload={
            "run_id": "historical_walkforward_500",
            "season_sources": season_sources,
        },
        path=Path("/sealed/schedule.json"),
        sha256=schedule_sha256,
    )
    timezone_gate = VerifiedTimezoneGate(
        candidate={
            "schedule_sidecar_sha256s": sorted(schedule_hashes),
        },
        mapping={
            "artifact_kind": "VENUE_TIMEZONE_MAPPING",
            "schema_version": 1,
            "timezone_rows": [
                {
                    "effective_end": "2024-12-31",
                    "effective_start": "2018-01-01",
                    "time_zone": "America/New_York",
                    "venue_id": venue_id,
                }
                for venue_id in (31, 99)
            ],
        },
        reviews=({}, {}),
        seal={},
        candidate_sha256=candidate_sha256,
        mapping_sha256=mapping_sha256,
        review_sha256s=review_sha256s,
        seal_sha256=seal_sha256,
        run_spec_sha256="2" * 64,
        acquisition_contract_sha256="1" * 64,
        schedule_inventory_sha256=schedule_sha256,
        query_batch_ids=("a" * 64, "b" * 64),
        query_batch_sha256s=("c" * 64, "d" * 64),
    )
    all_games = [
        game
        for payload in per_season_payloads
        for game in payload["games"]
    ]
    all_sidecar_hashes = sorted(
        {
            digest
            for payload in per_season_payloads
            for digest in payload["source_query_sidecar_sha256s"]
        }
    )
    game_inventory = VerifiedGameEvidenceInventory(
        payload={
            "acquisition_contract": {
                "path": "protocols/v5_2_qd/historical_source_acquisition.json",
                "sha256": "1" * 64,
            },
            "artifact_kind": "GAME_EVIDENCE_INVENTORY",
            "counts": {
                "game_count": len(all_games),
                "played_game_count": 7,
                "unique_actual_starter_count": 2,
                "unplayed_game_count": 7,
                "verified_feature_consumer_game_count": 7,
            },
            "games": all_games,
            "query_batches": [
                {
                    "batch_id": "a" * 64,
                    "path": (
                        "audit/v5_2_qd/historical/acquisition/query_batches/"
                        f"{'a' * 64}.json"
                    ),
                    "sha256": "c" * 64,
                    "stage": "STATIC_SCHEDULES",
                },
                {
                    "batch_id": "b" * 64,
                    "path": (
                        "audit/v5_2_qd/historical/acquisition/query_batches/"
                        f"{'b' * 64}.json"
                    ),
                    "sha256": "d" * 64,
                    "stage": "REFERENCE_INPUTS",
                },
            ],
            "run_id": "historical_walkforward_500",
            "run_spec": {
                "path": "protocols/v5_2_qd/historical_walkforward_500.json",
                "sha256": "2" * 64,
            },
            "schedule_inventory": {
                "path": (
                    "audit/v5_2_qd/historical/acquisition/"
                    "schedule_anchor_inventory.json"
                ),
                "sha256": schedule_sha256,
            },
            "schema_version": 1,
            "source_query_sidecar_sha256s": all_sidecar_hashes,
            "timezone_gate": {
                "candidate": {
                    "path": (
                        "audit/v5_2_qd/historical/acquisition/"
                        "venue_timezone_candidate.json"
                    ),
                    "sha256": candidate_sha256,
                },
                "mapping": {
                    "path": "config/v5_2_qd/venue_timezones.json",
                    "sha256": mapping_sha256,
                },
                "reviews": [
                    {
                        "path": (
                            "audit/v5_2_qd/historical/acquisition/"
                            "timezone_reviews/coverage.json"
                        ),
                        "sha256": review_sha256s[0],
                    },
                    {
                        "path": (
                            "audit/v5_2_qd/historical/acquisition/"
                            "timezone_reviews/semantic.json"
                        ),
                        "sha256": review_sha256s[1],
                    },
                ],
                "seal": {
                    "path": (
                        "audit/v5_2_qd/historical/acquisition/"
                        "venue_timezone_seal.json"
                    ),
                    "sha256": seal_sha256,
                },
            },
        },
        path=Path("/sealed/games.json"),
        sha256="9" * 64,
    )
    return SimpleNamespace(
        records=tuple(reversed(records)),
        schedule_inventory=schedule_inventory,
        timezone_gate=timezone_gate,
        game_inventory=game_inventory,
        per_season_payloads=tuple(per_season_payloads),
    )


def _authenticated_source_game_authority() -> SimpleNamespace:
    """Build a sealed seven-season source fixture without mocking the reducer."""
    from mlb_backtest import v52_verify_sources as verifier
    from mlb_backtest.v52_duplicate_gamepk_disposition import (
        DISPOSITION_ENDPOINT,
        _requested_range,
    )
    from mlb_backtest.v52_schedule_inventory import build_schedule_anchor_provenance
    from mlb_backtest.v52_sources.schedule import (
        derived_schedule_sidecar_adapter,
        recompute_game_evidence_snapshot,
    )

    base = _normalized_game_authority()
    source_run_id = "historical_walkforward_500_superseding_565534"
    records = list(base.records)
    schedules = {
        record.query.requested_range["year"]: record
        for record in records
        if record.query.endpoint == "https://statsapi.mlb.com/api/v1/schedule"
    }
    source_anchors = []
    for record in schedules.values():
        for anchor in json.loads(record.query_sidecar_bytes)["semantics"]["anchors"]:
            source_anchors.append(
                {
                    **anchor,
                    "raw_game_object_sha256": hashlib.sha256(
                        f"source-raw-{anchor['game_pk']}".encode()
                    ).hexdigest(),
                }
            )
    old_2019 = schedules[2019]
    disposition_query = dataclasses.replace(
        old_2019.query,
        endpoint=DISPOSITION_ENDPOINT,
        parameters={},
        requested_range=_requested_range(),
    )
    disposition_id = logical_query_id(disposition_query)
    disposition_sidecar = {
        "grain": disposition_query.grain,
        "parser_version": disposition_query.parser_version,
        "query_id": disposition_id,
        "schema_version": 1,
        "semantics": {"disposition": "SCHEDULE_DUPLICATE_GAMEPK_565534"},
        "source": disposition_query.source,
        "storage_mode": "DERIVED_DISPOSITION",
    }
    disposition_bytes = canonical_json_bytes(disposition_sidecar) + b"\n"
    disposition_sha = hashlib.sha256(disposition_bytes).hexdigest()
    disposition_record = dataclasses.replace(
        old_2019,
        query=disposition_query,
        query_id=disposition_id,
        query_sidecar_bytes=disposition_bytes,
        query_sidecar_sha256=disposition_sha,
        raw_sha256=None,
        storage_mode="DERIVED_DISPOSITION",
    )
    records[records.index(old_2019)] = disposition_record
    schedules[2019] = disposition_record
    binding = verifier.DuplicateGamePkDispositionBinding(
        query_id=disposition_id,
        success_receipt=verifier.AuthorityBinding("fixture/source-receipt.json", "e" * 64),
        query_sidecar=verifier.AuthorityBinding("fixture/source-sidecar.json", disposition_sha),
    )

    payloads = []
    season_sources = []
    for season in range(2018, 2025):
        schedule = schedules[season]
        anchors = [anchor for anchor in source_anchors if anchor["season"] == season]
        boxscore = next(
            record
            for record in records
            if record.query.requested_range.get("season") == season
            and record.query.requested_range.get("game_pk") is not None
        )
        game_pk = boxscore.query.requested_range["game_pk"]
        if season == 2019:
            schedule_sidecar = derived_schedule_sidecar_adapter(schedule.query, anchors)
        else:
            schedule_sidecar = json.loads(schedule.query_sidecar_bytes)
        payload = dict(
            recompute_game_evidence_snapshot(
                [schedule_sidecar],
                {game_pk: (boxscore.query_sidecar_bytes, boxscore.query_sidecar_sha256)},
                schedule_provenance=build_schedule_anchor_provenance(anchors),
            )
        )
        if season == 2019:
            adapter_sha = hashlib.sha256(
                canonical_json_bytes(schedule_sidecar) + b"\n"
            ).hexdigest()
            for game in payload["games"]:
                game["schedule_query_sidecar_sha256"] = disposition_sha
                game["source_game_sha256"] = canonical_sha256(
                    {
                        "anchor_sidecar_sha256": disposition_sha,
                        **(
                            {
                                "boxscore_query_sidecar_sha256": game[
                                    "boxscore_query_sidecar_sha256"
                                ]
                            }
                            if game["played"]
                            else {}
                        ),
                        "game": {
                            key: value
                            for key, value in game.items()
                            if key != "source_game_sha256"
                        },
                    }
                )
            payload["source_query_sidecar_sha256s"] = sorted(
                (set(payload["source_query_sidecar_sha256s"]) - {adapter_sha})
                | {disposition_sha}
            )
        payloads.append(payload)
        season_sources.append(
            {
                "season": season,
                "query_id": schedule.query_id,
                "query_sidecar": {
                    "path": f"fixture/source-{season}.json",
                    "sha256": schedule.query_sidecar_sha256,
                },
            }
        )

    schedule_inventory = dataclasses.replace(
        base.schedule_inventory,
        payload={
            "run_id": source_run_id,
            "season_sources": season_sources,
            # Verified schedule inventories deep-freeze JSON arrays at the
            # loader boundary, so source replay receives this exact tuple.
            "anchors": tuple(source_anchors),
        },
    )
    games = [game for payload in payloads for game in payload["games"]]
    game_inventory = dataclasses.replace(
        base.game_inventory,
        payload={
            **base.game_inventory.payload,
            "run_id": source_run_id,
            "games": games,
            "source_query_sidecar_sha256s": sorted(
                {
                    digest
                    for payload in payloads
                    for digest in payload["source_query_sidecar_sha256s"]
                }
            ),
            "duplicate_gamepk_disposition": binding.as_mapping(),
            "dispositioned_query_ids": [disposition_id],
        },
    )
    timezone_gate = dataclasses.replace(
        base.timezone_gate,
        candidate={
            "schedule_sidecar_sha256s": sorted(
                row["query_sidecar"]["sha256"] for row in season_sources
            )
        },
    )
    return SimpleNamespace(
        binding=binding,
        disposition_id=disposition_id,
        expected_payloads=tuple(payloads),
        game_inventory=game_inventory,
        records=tuple(records),
        schedule_inventory=schedule_inventory,
        source_anchors=source_anchors,
        timezone_gate=timezone_gate,
    )


class SnapshotReplayInputFactoryTests(unittest.TestCase):
    def setUp(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        self.verifier = verifier
        fx = _graph_fixture()
        static_batch = dataclasses.replace(fx.batches[0], stage="STATIC_SCHEDULES")
        self.graph = SimpleNamespace(
            **{
                **vars(fx),
                "query_batches": (static_batch, *fx.batches[1:]),
                "completed_queries": fx.completed,
            }
        )
        self.root = Path("/sealed/repository")
        self.evidence_root = self.root / "evidence/raw"
        self.run_spec = SimpleNamespace(
            run_id="historical_walkforward_500",
            raw={"namespaces": {"raw": "evidence/raw"}},
        )
        self.contract_path = self.root / "contract.json"
        self.schedule = object()
        self.games = SimpleNamespace(payload={"run_id": self.run_spec.run_id})
        self.timezone = object()
        self.records = (object(),)
        self.snapshots = (object(),)

    def _patch_factory(self):
        verifier = self.verifier
        return patch.multiple(
            verifier,
            _load_terminal_query_graph=DEFAULT,
            load_verified_schedule_anchor_inventory=DEFAULT,
            load_verified_game_evidence_inventory=DEFAULT,
            load_verified_timezone_gate=DEFAULT,
            _recompute_verified_query_records=DEFAULT,
            _build_game_normalized_snapshots=DEFAULT,
            create=True,
        )

    def test_load_snapshot_replay_inputs_uses_only_verified_readers(self) -> None:
        verifier = self.verifier
        with self._patch_factory() as mocks, patch.object(
            verifier.V52EvidenceStore, "open_read_only", return_value=object()
        ) as open_store, patch.object(
            Path, "resolve", side_effect=AssertionError("must not resolve")
        ):
            mocks["_load_terminal_query_graph"].return_value = self.graph
            mocks["load_verified_schedule_anchor_inventory"].return_value = self.schedule
            mocks["load_verified_game_evidence_inventory"].return_value = self.games
            mocks["load_verified_timezone_gate"].return_value = self.timezone
            mocks["_recompute_verified_query_records"].return_value = self.records
            mocks["_build_game_normalized_snapshots"].return_value = self.snapshots
            result = verifier.load_snapshot_replay_inputs(
                self.root,
                self.run_spec,
                acquisition_contract_path=self.contract_path,
            )

        self.assertIsInstance(result, verifier.SnapshotReplayInputs)
        self.assertEqual(result.records, self.records)
        self.assertEqual(result.game_snapshots, self.snapshots)
        open_store.assert_called_once_with(self.evidence_root)
        mocks["load_verified_schedule_anchor_inventory"].assert_called_once_with(
            self.root,
            self.run_spec,
            acquisition_contract_path=self.contract_path,
            static_batch_id=self.graph.query_batches[0].batch_id,
            evidence_root=self.evidence_root,
            read_only_progress=True,
        )
        mocks["load_verified_timezone_gate"].assert_called_once_with(
            self.root,
            read_only_progress=True,
        )
        mocks["load_verified_game_evidence_inventory"].assert_called_once_with(
            self.root,
            read_only_progress=True,
        )
        store = open_store.return_value
        mocks["_recompute_verified_query_records"].assert_called_once_with(
            store, self.graph.completed_queries
        )
        mocks["_build_game_normalized_snapshots"].assert_called_once_with(
            self.run_spec.run_id,
            self.records,
            self.games,
            self.timezone,
            self.schedule,
            duplicate_gamepk_disposition=None,
            dispositioned_query_ids=(),
        )

    def test_factory_fails_closed_without_exactly_one_static_schedule_batch(self) -> None:
        verifier = self.verifier
        no_static = SimpleNamespace(
            **{
                **vars(self.graph),
                "query_batches": tuple(
                    dataclasses.replace(batch, stage="REMAINING")
                    for batch in self.graph.query_batches
                ),
            }
        )
        two_static = SimpleNamespace(
            **{
                **vars(self.graph),
                "query_batches": tuple(
                    dataclasses.replace(batch, stage="STATIC_SCHEDULES")
                    for batch in self.graph.query_batches
                ),
            }
        )
        for graph in (no_static, two_static):
            with self.subTest(graph=graph), patch.object(
                verifier, "_load_terminal_query_graph", return_value=graph
            ), self.assertRaises(ValueError):
                verifier.load_snapshot_replay_inputs(
                    self.root,
                    self.run_spec,
                    acquisition_contract_path=self.contract_path,
                )

    def test_factory_rejects_unsafe_raw_namespace_lexically(self) -> None:
        verifier = self.verifier
        for raw in ("", "/tmp/raw", "raw\\nested", ".", "raw//nested", "raw/./x", "raw/../x"):
            with self.subTest(raw=raw):
                run_spec = SimpleNamespace(
                    run_id=self.run_spec.run_id,
                    raw={"namespaces": {"raw": raw}},
                )
                with patch.object(
                    verifier, "_load_terminal_query_graph", return_value=self.graph
                ) as graph_loader, self.assertRaises(ValueError):
                    verifier.load_snapshot_replay_inputs(
                        self.root,
                        run_spec,
                        acquisition_contract_path=self.contract_path,
                    )
                graph_loader.assert_not_called()

    def test_factory_propagates_second_load_authority_change(self) -> None:
        verifier = self.verifier
        with self._patch_factory() as mocks:
            mocks["_load_terminal_query_graph"].return_value = self.graph
            mocks["load_verified_schedule_anchor_inventory"].side_effect = ValueError(
                "schedule inventory authority changed during verified load"
            )
            with self.assertRaisesRegex(ValueError, "authority changed"):
                verifier.load_snapshot_replay_inputs(
                    self.root,
                    self.run_spec,
                    acquisition_contract_path=self.contract_path,
                )

    def test_source_offline_replay_authenticates_derived_schedule_read(self) -> None:
        """The real evidence store must receive the disposition capability."""
        from mlb_backtest.protocols import load_source_run_spec
        from mlb_backtest.v52_duplicate_gamepk_disposition import (
            DISPOSITION_ENDPOINT,
            DuplicateGamePkDispositionAuthority,
            _requested_range,
        )

        verifier = self.verifier
        source_run_path = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract_path = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        source_run = load_source_run_spec(source_run_path)
        query = LogicalQuery(
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            endpoint=DISPOSITION_ENDPOINT,
            parameters={},
            requested_range=_requested_range(),
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        semantics = {"authenticated_projection": True}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            evidence_root = (
                root
                / "data/v5_2_qd/historical_superseding_565534/raw"
            )
            evidence_root.parent.mkdir(parents=True)
            authority = DuplicateGamePkDispositionAuthority(
                repository_root=root,
                evidence_root=evidence_root,
                source_run_spec_path=source_run_path,
                acquisition_contract_path=source_contract_path,
                query=query,
                old_raw_relative=Path("sealed/old.raw"),
                _snapshots=(),
            )
            with (
                patch.object(
                    DuplicateGamePkDispositionAuthority,
                    "reauthenticate",
                    autospec=True,
                    side_effect=lambda current: current,
                ),
                patch(
                    "mlb_backtest.v52_source_contracts.recompute_query_sidecar",
                    return_value=semantics,
                ),
            ):
                store = V52EvidenceStore(evidence_root)
                with store.acquire_query(query):
                    published = store.publish_derived_disposition(
                        query, semantics, authority=authority
                    )

            disposition_id = logical_query_id(query)
            binding = {
                "query_id": disposition_id,
                "success_receipt": {
                    "path": "sealed/success.json",
                    "sha256": hashlib.sha256(
                        published.success_receipt_path.read_bytes()
                    ).hexdigest(),
                },
                "query_sidecar": {
                    "path": "sealed/sidecar.json",
                    "sha256": published.query_sidecar_sha256,
                },
            }
            games = SimpleNamespace(
                payload={
                    "duplicate_gamepk_disposition": binding,
                    "dispositioned_query_ids": [disposition_id],
                }
            )
            graph = SimpleNamespace(
                query_batches=(
                    SimpleNamespace(
                        stage="STATIC_SCHEDULES", batch_id="c" * 64
                    ),
                ),
                completed_queries=(query,),
                checkpoint=SimpleNamespace(
                    dispositioned_query_ids=(disposition_id,)
                ),
            )
            context = SimpleNamespace(
                acquisition_contract_path=source_contract_path,
                authority=source_run,
                authority_path=source_run_path,
                raw_root=evidence_root,
                repository_root=root,
            )
            with (
                patch.object(
                    verifier,
                    "_authenticate_authority_paths",
                    return_value=context,
                ),
                patch.object(
                    verifier, "_load_terminal_query_graph", return_value=graph
                ),
                patch.object(
                    verifier,
                    "load_verified_schedule_anchor_inventory",
                    return_value=SimpleNamespace(payload={"anchors": []}),
                ),
                patch.object(
                    verifier,
                    "load_verified_timezone_gate",
                    return_value=object(),
                ),
                patch.object(
                    verifier,
                    "load_verified_game_evidence_inventory",
                    return_value=games,
                ),
                patch.object(
                    verifier, "_build_game_normalized_snapshots", return_value=()
                ) as build_games,
                patch.object(
                    verifier,
                    "recompute_query_sidecar",
                    return_value=semantics,
                ),
                patch(
                    "mlb_backtest.v52_source_contracts.recompute_query_sidecar",
                    return_value=semantics,
                ),
                patch.object(
                    verifier,
                    "build_duplicate_disposition_authority",
                    return_value=authority,
                ) as build_authority,
                patch.object(
                    DuplicateGamePkDispositionAuthority,
                    "reauthenticate",
                    autospec=True,
                    side_effect=lambda current: current,
                ),
            ):
                read_only = V52EvidenceStore.open_read_only(evidence_root)
                with self.assertRaisesRegex(TypeError, "authenticated authority"):
                    verifier._read_verified_query_record(read_only, query)
                wrong_authority = dataclasses.replace(
                    authority,
                    query=dataclasses.replace(query, parent_query_id="f" * 64),
                )
                with self.assertRaisesRegex(ValueError, "authority|query|binding"):
                    verifier._read_verified_query_record(
                        read_only,
                        query,
                        duplicate_gamepk_disposition_authority=wrong_authority,
                    )
                replay = verifier.load_snapshot_replay_inputs(
                    root,
                    source_run,
                    acquisition_contract_path=source_contract_path,
                )

            build_authority.assert_called_once_with(
                root,
                source_run_spec_path=source_run_path,
                acquisition_contract_path=source_contract_path,
            )
            self.assertEqual(replay.dispositioned_query_ids, (disposition_id,))
            self.assertEqual(len(replay.records), 1)
            self.assertEqual(replay.records[0].query_id, disposition_id)
            self.assertEqual(
                replay.records[0].storage_mode, "DERIVED_DISPOSITION"
            )
            self.assertEqual(replay.duplicate_gamepk_disposition.query_id, disposition_id)
            build_games.assert_called_once_with(
                source_run.run_id,
                (replay.records[0],),
                games,
                build_games.call_args.args[3],
                build_games.call_args.args[4],
                duplicate_gamepk_disposition=replay.duplicate_gamepk_disposition,
                dispositioned_query_ids=(disposition_id,),
            )


class V52VerifySourcesTest(unittest.TestCase):
    def test_superseding_replay_inputs_limit_disposition_identity_to_game_2019(
        self,
    ) -> None:
        """Only the 2019 game envelope carries the schedule disposition."""
        from tests.v52_snapshot_store_fixture import fake_81, replay_inputs

        disposition_id = (
            "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
        )
        sidecar_sha = (
            "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1"
        )
        receipt_sha = "8" * 64
        from mlb_backtest.v52_verify_sources import (
            AuthorityBinding,
            DuplicateGamePkDispositionBinding,
        )

        binding = DuplicateGamePkDispositionBinding(
            query_id=disposition_id,
            query_sidecar=AuthorityBinding("sidecar.json", sidecar_sha),
            success_receipt=AuthorityBinding("success.json", receipt_sha),
        )
        snapshots = list(fake_81())
        for index, snapshot in enumerate(snapshots):
            if snapshot.source == "mlb_schedule_game_feed" and snapshot.season == 2019:
                snapshots[index] = dataclasses.replace(
                    snapshot,
                    dispositioned_query_ids=(disposition_id,),
                    duplicate_gamepk_disposition=binding,
                )
        base = replay_inputs()
        replay = dataclasses.replace(
            base,
            run_id="historical_walkforward_500_superseding_565534",
            game_snapshots=tuple(snapshots),
            duplicate_gamepk_disposition=binding,
            dispositioned_query_ids=(disposition_id,),
        )
        self.assertEqual(replay.dispositioned_query_ids, (disposition_id,))
        self.assertEqual(replay.duplicate_gamepk_disposition, binding)
        for snapshot in replay.game_snapshots:
            if snapshot.source == "mlb_schedule_game_feed" and snapshot.season == 2019:
                self.assertEqual(snapshot.dispositioned_query_ids, (disposition_id,))
                self.assertEqual(snapshot.duplicate_gamepk_disposition, binding)
            else:
                self.assertEqual(snapshot.dispositioned_query_ids, ())
                self.assertIsNone(snapshot.duplicate_gamepk_disposition)
        self.assertNotIn(
            "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8",
            replay.dispositioned_query_ids,
        )

    def test_superseding_pair_authenticates_then_reaches_source_graph(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.protocols import load_source_run_spec
        from mlb_backtest.v52_verify_sources import load_snapshot_replay_inputs

        source_run_path = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        source_contract_path = (
            ROOT
            / "protocols/v5_2_qd/"
            "historical_source_acquisition_superseding_565534.json"
        )
        source_run = load_source_run_spec(source_run_path)

        class SourceGraphReached(RuntimeError):
            pass

        with patch.object(
            verifier,
            "_load_terminal_query_graph",
            side_effect=SourceGraphReached("source graph reached"),
        ) as graph_loader, self.assertRaisesRegex(
            SourceGraphReached, "source graph reached"
        ):
            load_snapshot_replay_inputs(
                ROOT,
                source_run,
                acquisition_contract_path=source_contract_path,
            )
        graph_loader.assert_called_once_with(ROOT, source_run)

        with patch.object(
            verifier, "_load_terminal_query_graph"
        ) as graph_loader, self.assertRaisesRegex(
            ValueError, "source authority pair differs"
        ):
            load_snapshot_replay_inputs(
                ROOT,
                source_run,
                acquisition_contract_path=ROOT
                / "protocols/v5_2_qd/historical_source_acquisition.json",
            )
        graph_loader.assert_not_called()

    def test_completed_query_graph_classifies_root_and_split_leaves(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx = _graph_fixture()
        enumerate_graph = getattr(verifier, "_enumerate_completed_query_graph", None)
        self.assertIsNotNone(enumerate_graph, "missing graph enumerator")
        graph = enumerate_graph(
            fx.checkpoint,
            fx.batches,
            fx.receipts,
            fx.statcast_root,
        )
        self.assertIsInstance(graph, verifier._TerminalQueryGraph)
        self.assertIs(graph.checkpoint, fx.checkpoint)
        self.assertEqual(graph.query_batches, fx.batches)
        self.assertIs(graph.statcast_root_batch, fx.statcast_root)
        self.assertEqual(
            graph.split_receipts,
            tuple(
                {item.parent_query_id: item for item in fx.receipts}[parent_id]
                for parent_id in fx.checkpoint.split_query_ids
            ),
        )
        self.assertEqual(graph.split_parent_queries, (fx.parent,))
        self.assertEqual(
            graph.completed_queries, tuple(sorted(fx.completed, key=logical_query_id))
        )
        self.assertEqual(graph.root_queries, (fx.root,))
        self.assertEqual(
            graph.split_leaf_queries,
            tuple(sorted((fx.left, fx.right), key=logical_query_id)),
        )
        self.assertNotIn(fx.parent, graph.completed_queries)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            graph.completed_queries = ()

    def test_completed_query_graph_rejects_population_binding_and_cycles(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx = _graph_fixture()
        cp, (root_batch, child_batch), (receipt,) = (
            fx.checkpoint,
            fx.batches,
            fx.receipts,
        )
        duplicate_batch = dataclasses.replace(child_batch, queries=(fx.root, fx.right))
        duplicate_receipt = dataclasses.replace(
            receipt,
            child_query_ids=tuple(logical_query_id(q) for q in duplicate_batch.queries),
        )
        cyclic_root = dataclasses.replace(
            root_batch, derived_from_sha256=child_batch.batch_id
        )
        back = dataclasses.replace(
            receipt,
            split_receipt_id="9" * 64,
            receipt_sha256="f" * 64,
            parent_batch_id=child_batch.batch_id,
            parent_query_id=logical_query_id(fx.left),
            depth=2,
            child_batch_id=cyclic_root.batch_id,
            child_query_ids=tuple(logical_query_id(q) for q in cyclic_root.queries),
        )
        attacks = (
            (cp, (root_batch, duplicate_batch), (duplicate_receipt,)),
            (
                dataclasses.replace(
                    cp,
                    completed_query_ids=tuple(
                        sorted((*cp.completed_query_ids, logical_query_id(fx.parent)))
                    ),
                ),
                fx.batches,
                fx.receipts,
            ),
            (
                dataclasses.replace(
                    cp, completed_query_ids=cp.completed_query_ids[:-1]
                ),
                fx.batches,
                fx.receipts,
            ),
            (
                dataclasses.replace(
                    cp,
                    completed_query_ids=tuple(
                        sorted((*cp.completed_query_ids, "0" * 64))
                    ),
                ),
                fx.batches,
                fx.receipts,
            ),
            (cp, fx.batches, (dataclasses.replace(receipt, parent_batch_id="0" * 64),)),
            (
                cp,
                fx.batches,
                (
                    dataclasses.replace(
                        receipt,
                        child_query_ids=tuple(reversed(receipt.child_query_ids)),
                    ),
                ),
            ),
            (
                dataclasses.replace(
                    cp,
                    completed_query_ids=tuple(
                        sorted((logical_query_id(fx.root), logical_query_id(fx.right)))
                    ),
                    split_query_ids=tuple(
                        sorted((receipt.parent_query_id, back.parent_query_id))
                    ),
                    split_receipt_sha256s=tuple(
                        sorted((receipt.receipt_sha256, back.receipt_sha256))
                    ),
                ),
                (cyclic_root, child_batch),
                (receipt, back),
            ),
        )
        enumerate_graph = getattr(verifier, "_enumerate_completed_query_graph", None)
        self.assertIsNotNone(enumerate_graph, "missing graph enumerator")
        for index, arguments in enumerate(attacks):
            with self.subTest(index=index), self.assertRaises(ValueError):
                enumerate_graph(*arguments, fx.statcast_root)

    def test_terminal_query_graph_loader_uses_only_sealed_read_only_artifacts(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx, root = _graph_fixture(recursive=True), Path("/sealed/repository")
        run_spec = SimpleNamespace(raw={"namespaces": {"raw": "evidence/raw"}})
        batch_index = {
            item.batch_id: item for item in (*fx.batches, fx.statcast_root)
        }
        receipt_index = {item.parent_query_id: item for item in fx.receipts}
        loader = getattr(verifier, "_load_terminal_query_graph", None)
        self.assertIsNotNone(loader, "missing terminal graph loader")
        with (
            patch.object(
                verifier,
                "load_source_verification_checkpoint",
                return_value=fx.checkpoint,
                create=True,
            ) as checkpoint_loader,
            patch.object(
                verifier,
                "load_query_batch",
                side_effect=lambda _, key, **kw: batch_index[key],
                create=True,
            ) as batch_loader,
            patch.object(
                verifier,
                "load_split_receipt",
                side_effect=lambda _, key, **kw: receipt_index[key],
                create=True,
            ) as receipt_loader,
            patch.object(Path, "glob", side_effect=AssertionError("must not scan")),
            patch.object(Path, "rglob", side_effect=AssertionError("must not scan")),
        ):
            graph = loader(root, run_spec)
        checkpoint_loader.assert_called_once_with(root, run_spec)
        self.assertEqual(
            batch_loader.call_args_list,
            [
                call(root, fx.statcast_root.batch_id, read_only_progress=True),
                *[
                    call(root, item.batch_id, read_only_progress=True)
                    for item in fx.batches
                ],
            ],
        )
        self.assertEqual(
            receipt_loader.call_args_list,
            [
                call(
                    root,
                    key,
                    evidence_root=root / "evidence/raw",
                    read_only_progress=True,
                )
                for key in fx.checkpoint.split_query_ids
            ],
        )
        self.assertEqual(
            graph.completed_queries, tuple(sorted(fx.completed, key=logical_query_id))
        )
        self.assertIs(graph.checkpoint, fx.checkpoint)
        self.assertEqual(graph.query_batches, fx.batches)
        self.assertIs(graph.statcast_root_batch, fx.statcast_root)
        self.assertEqual(
            graph.split_receipts,
            tuple(receipt_index[parent_id] for parent_id in fx.checkpoint.split_query_ids),
        )
        self.assertEqual(
            graph.split_parent_queries,
            tuple(sorted((fx.parent, fx.left), key=logical_query_id)),
        )

    def test_stage_evidence_cannot_bind_a_non_statcast_root_batch(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx, root = _graph_fixture(), Path("/sealed/repository")
        bad = QueryBatch("9" * 64, "REMAINING", None, (fx.root,))
        bad_path = (
            "audit/v5_2_qd/historical/acquisition/query_batches/"
            f"{bad.batch_id}.json"
        )
        checkpoint = dataclasses.replace(
            fx.checkpoint,
            stage_evidence_receipt_paths=(bad_path,),
            stage_evidence_receipt_sha256s=(_batch_artifact_sha256(bad),),
        )

        with patch.object(verifier, "load_query_batch", return_value=bad) as loader:
            with self.assertRaisesRegex(ValueError, "Statcast root batch authority"):
                verifier._load_statcast_root_batch(root, checkpoint)

        loader.assert_called_once_with(root, bad.batch_id, read_only_progress=True)

    def test_terminal_graph_rejects_unsafe_namespace_before_any_authority_read(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        root = Path("/sealed/repository")
        for raw in ("", ".", "raw\\nested", "raw//nested", "raw/../nested"):
            with self.subTest(raw=raw):
                run_spec = SimpleNamespace(raw={"namespaces": {"raw": raw}})
                with (
                    patch.object(
                        verifier, "load_source_verification_checkpoint"
                    ) as checkpoint_loader,
                    patch.object(verifier, "load_query_batch") as batch_loader,
                    patch.object(verifier, "load_split_receipt") as receipt_loader,
                    self.assertRaises(ValueError),
                ):
                    verifier._load_terminal_query_graph(root, run_spec)
                checkpoint_loader.assert_not_called()
                batch_loader.assert_not_called()
                receipt_loader.assert_not_called()

    def test_statcast_root_requires_exact_stage_path_and_artifact_hash(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        fx, root = _graph_fixture(), Path("/sealed/repository")
        attacks = (
            dataclasses.replace(
                fx.checkpoint,
                stage_evidence_receipt_paths=(
                    f"audit/wrong/query_batches/{fx.statcast_root.batch_id}.json",
                ),
            ),
            dataclasses.replace(
                fx.checkpoint,
                stage_evidence_receipt_sha256s=("f" * 64,),
            ),
            dataclasses.replace(
                fx.checkpoint,
                stage_evidence_receipt_paths=(
                    fx.statcast_root_path,
                    fx.statcast_root_path,
                ),
                stage_evidence_receipt_sha256s=(
                    fx.statcast_root_sha256,
                    fx.statcast_root_sha256,
                ),
            ),
            dataclasses.replace(
                fx.checkpoint,
                query_batch_ids=(
                    *fx.checkpoint.query_batch_ids,
                    fx.statcast_root.batch_id,
                ),
            ),
        )
        for index, checkpoint in enumerate(attacks):
            with (
                self.subTest(index=index),
                patch.object(
                    verifier, "load_query_batch", return_value=fx.statcast_root
                ),
                self.assertRaises(ValueError),
            ):
                verifier._load_statcast_root_batch(root, checkpoint)

    def test_completed_query_recompute_is_query_id_sorted(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        queries, store = tuple(reversed(_graph_fixture().completed)), object()
        recompute = getattr(verifier, "_recompute_completed_queries", None)
        self.assertIsNotNone(recompute, "missing completed recompute helper")
        with patch.object(
            verifier,
            "_read_and_recompute_query",
            side_effect=lambda _, q: {"query_id": logical_query_id(q)},
        ) as reader:
            result = recompute(store, queries)
        ordered = tuple(sorted(queries, key=logical_query_id))
        self.assertEqual(
            reader.call_args_list, [call(store, query) for query in ordered]
        )
        self.assertEqual(
            result, tuple({"query_id": logical_query_id(query)} for query in ordered)
        )

    def test_verified_query_records_are_query_id_sorted(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        queries, store = tuple(reversed(_graph_fixture().completed)), object()
        recompute = getattr(verifier, "_recompute_verified_query_records", None)
        self.assertIsNotNone(recompute, "missing verified-record recompute helper")
        with patch.object(
            verifier,
            "_read_verified_query_record",
            side_effect=lambda _, q: SimpleNamespace(
                query_id=logical_query_id(q)
            ),
        ) as reader:
            result = recompute(store, queries)
        ordered = tuple(sorted(queries, key=logical_query_id))
        self.assertEqual(
            reader.call_args_list, [call(store, query) for query in ordered]
        )
        self.assertEqual(
            tuple(record.query_id for record in result),
            tuple(logical_query_id(query) for query in ordered),
        )

    def test_verified_query_record_binds_canonical_sidecar_and_drops_raw(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "raw"
            _, query, raw = _owned_schedule_evidence(root)
            read_only = V52EvidenceStore.open_read_only(root)
            verified = read_only.read_verified_success(query)
            expected_sidecar_bytes = (
                canonical_json_bytes(dict(verified.query_sidecar)) + b"\n"
            )
            before = _snapshot_tree(root)

            record = verifier._read_verified_query_record(read_only, query)

            self.assertTrue(dataclasses.is_dataclass(record))
            with self.assertRaises(dataclasses.FrozenInstanceError):
                record.raw_sha256 = "0" * 64
            self.assertEqual(record.query, query)
            self.assertEqual(record.query_id, logical_query_id(query))
            self.assertEqual(record.query_sidecar_bytes, expected_sidecar_bytes)
            self.assertEqual(
                record.query_sidecar_sha256,
                hashlib.sha256(expected_sidecar_bytes).hexdigest(),
            )
            self.assertEqual(record.raw_sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(record.storage_mode, "FETCHED_RAW")
            self.assertIsNone(record.retained_raw_bytes)
            self.assertEqual(_snapshot_tree(root), before)

    def test_verified_query_record_retains_only_team_and_venue_raw(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        common = {
            "source": "venue_team_timezone_table",
            "grain": "effective_dated_venue_team_reference",
            "expected_media_type": "application/json",
            "parser_version": "v5.2.1",
            "parent_query_id": None,
            "run_spec_sha256": "a" * 64,
            "acquisition_contract_sha256": "b" * 64,
        }
        team_query = LogicalQuery(
            **common,
            endpoint="https://statsapi.mlb.com/api/v1/teams",
            parameters={
                "sportId": 1,
                "season": 2024,
                "hydrate": "venue(location)",
            },
            requested_range={"season": 2024, "entity": "teams"},
        )
        venue_query = LogicalQuery(
            **common,
            endpoint="https://statsapi.mlb.com/api/v1/venues/31",
            parameters={"hydrate": "location"},
            requested_range={"venue_id": 31},
        )
        raw_by_query = (
            (team_query, (SOURCE_FIXTURES / "team_mini.json").read_bytes()),
            (
                venue_query,
                canonical_json_bytes(_VENUE_GUARD_PAYLOAD),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            for index, (query, raw) in enumerate(raw_by_query):
                with self.subTest(endpoint=query.endpoint):
                    root = base / str(index)
                    _owned_fetched_evidence(root, query, raw)
                    record = verifier._read_verified_query_record(
                        V52EvidenceStore.open_read_only(root), query
                    )
                    self.assertEqual(record.retained_raw_bytes, raw)

        schedule = _query()
        forbidden = (
            schedule,
            dataclasses.replace(
                schedule,
                source="baseball_savant_statcast",
                grain="pitch",
                endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
            ),
            dataclasses.replace(
                schedule,
                grain="season_game_evidence",
                endpoint="https://statsapi.mlb.com/api/v1/game/9001/boxscore",
            ),
            dataclasses.replace(
                schedule,
                source="mlb_stats_pitching",
                grain="season_d1_league_pitching_reference",
                endpoint="https://statsapi.mlb.com/api/v1/stats",
            ),
            dataclasses.replace(
                schedule,
                source="mlb_stats_pitcher_bf",
                grain="player_career_batters_faced_reference",
                endpoint="https://statsapi.mlb.com/api/v1/people/101/stats",
            ),
            dataclasses.replace(
                team_query,
                endpoint="https://statsapi.mlb.com/api/v1/not-a-reference",
            ),
        )
        for query in forbidden:
            with self.subTest(source=query.source, grain=query.grain):
                self.assertFalse(verifier._retain_raw_for_aggregation(query))

    def test_builds_seven_closed_game_normalized_snapshots_in_memory(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _normalized_game_authority()
        with patch.object(
            verifier,
            "recompute_game_evidence_snapshot",
            wraps=verifier.recompute_game_evidence_snapshot,
        ) as recompute:
            snapshots = verifier._build_game_normalized_snapshots(
                "historical_walkforward_500",
                authority.records,
                authority.game_inventory,
                authority.timezone_gate,
                authority.schedule_inventory,
            )

        self.assertEqual(recompute.call_count, 7)
        self.assertEqual(tuple(item.season for item in snapshots), tuple(range(2018, 2025)))
        schema = json.loads(NORMALIZED_SNAPSHOT_SCHEMA.read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        for snapshot, expected_payload in zip(
            snapshots, authority.per_season_payloads, strict=True
        ):
            envelope = snapshot.as_mapping()
            jsonschema.validate(envelope, schema)
            self.assertEqual(envelope["payload"], expected_payload)
            self.assertEqual(envelope["schema_version"], 1)
            self.assertEqual(envelope["run_id"], "historical_walkforward_500")
            self.assertEqual(envelope["source"], "mlb_schedule_game_feed")
            self.assertEqual(envelope["grain"], "season_game_evidence_snapshot")
            self.assertEqual(envelope["status"], "READY")
            self.assertEqual(envelope["split_receipt_sha256s"], [])
            self.assertEqual(
                envelope["logical_query_ids"],
                sorted(set(envelope["logical_query_ids"])),
            )
            self.assertEqual(
                envelope["source_query_sidecar_sha256s"],
                sorted(set(envelope["source_query_sidecar_sha256s"])),
            )
            self.assertEqual(
                snapshot.canonical_bytes,
                canonical_json_bytes(envelope) + b"\n",
            )
            self.assertEqual(
                snapshot.sha256,
                hashlib.sha256(snapshot.canonical_bytes).hexdigest(),
            )
            self.assertNotIn(
                snapshot.sha256,
                envelope["source_query_sidecar_sha256s"],
            )
            self.assertNotEqual(snapshot.sha256, authority.game_inventory.sha256)
            with self.assertRaises(dataclasses.FrozenInstanceError):
                snapshot.season = 2025

    def test_source_replay_rejects_synthetic_disposition_provenance(
        self,
    ) -> None:
        """A fixture cannot impersonate the one immutable source disposition."""
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.protocols import SourceRunSpec, load_source_run_spec

        source_run = load_source_run_spec(
            ROOT
            / "protocols/v5_2_qd/"
            "historical_walkforward_500_superseding_565534.json"
        )
        self.assertIsInstance(source_run, SourceRunSpec)

        def replay(fixture: SimpleNamespace, run_id: str | None = None):
            return verifier._build_game_normalized_snapshots(
                source_run.run_id if run_id is None else run_id,
                fixture.records,
                fixture.game_inventory,
                fixture.timezone_gate,
                fixture.schedule_inventory,
                duplicate_gamepk_disposition=fixture.binding,
                dispositioned_query_ids=(fixture.disposition_id,),
            )

        fixture = _authenticated_source_game_authority()
        with self.assertRaises(ValueError):
            replay(fixture)

    @unittest.skipUnless(
        os.environ.get("MLB_RUN_SLOW_SOURCE_REPLAY_INTEGRATION") == "1",
        "requires the fixed protected 3GB corpus; opt in with "
        "MLB_RUN_SLOW_SOURCE_REPLAY_INTEGRATION=1",
    )
    def test_public_source_loader_authenticates_disposition_inventory(self) -> None:
        """Replay real source authority through the public sealed loader boundary.

        This deliberately opt-in integration test rehashes the fixed protected
        3GB legacy corpus while authenticating terminal semantic authority.
        """

        from mlb_backtest.protocols import SourceRunSpec
        from mlb_backtest.v52_acquire import acquire_sources
        from mlb_backtest.v52_verify_sources import load_snapshot_replay_inputs
        from tests.test_v52_acquire_e2e import DeterministicFetcher
        from tests.test_v52_timezone_review import _source_verified_gate_fixture

        protected_raw = ROOT / "data/v5_1_bt/raw"
        self.assertTrue(protected_raw.is_dir())

        with tempfile.TemporaryDirectory() as directory:
            fixture, *_ = _source_verified_gate_fixture(Path(directory))
            fixture_raw = fixture.root / "data/v5_1_bt/raw"
            # The legacy-decision writer verifies this immutable corpus.  Use
            # hard links so the genuine source bytes are available without a
            # multi-gigabyte test copy.
            for source in protected_raw.rglob("*"):
                target = fixture_raw / source.relative_to(protected_raw)
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif source.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.link(source, target)

            acquire_sources(
                fixture.root,
                fixture.run_spec,
                years=range(2018, 2025),
                mode="hybrid",
                resume=True,
                stop_after=None,
                legacy_root=fixture_raw,
                protected_inventory_path=(
                    fixture.root / "audit/v5_1_bt/protected_raw_inventory.json"
                ),
                fetcher=DeterministicFetcher(),
                acquisition_contract_path=fixture.contract_path,
            )
            inputs = load_snapshot_replay_inputs(
                fixture.root,
                fixture.run_spec,
                acquisition_contract_path=fixture.contract_path,
            )

            self.assertIsInstance(fixture.run_spec, SourceRunSpec)
            self.assertEqual(inputs.run_id, fixture.run_spec.run_id)
            self.assertEqual(inputs.graph.checkpoint.stage, "REMAINING")
            self.assertEqual(
                inputs.dispositioned_query_ids,
                (inputs.duplicate_gamepk_disposition.query_id,),
            )
            source_snapshot = next(
                snapshot
                for snapshot in inputs.game_snapshots
                if snapshot.season == 2019
            ).as_mapping()
            self.assertEqual(
                source_snapshot["duplicate_gamepk_disposition"],
                inputs.duplicate_gamepk_disposition.as_mapping(),
            )

    def test_game_snapshot_mapping_validation_rejects_forgery(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _normalized_game_authority()
        snapshots = verifier._build_game_normalized_snapshots(
            "historical_walkforward_500",
            authority.records,
            authority.game_inventory,
            authority.timezone_gate,
            authority.schedule_inventory,
        )
        schema = json.loads(NORMALIZED_SNAPSHOT_SCHEMA.read_bytes())
        forged = snapshots[0].as_mapping()
        forged["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(forged, schema)
        forged = snapshots[0].as_mapping()
        forged["logical_query_ids"].append(forged["logical_query_ids"][0])
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(forged, schema)
        forged = snapshots[0].as_mapping()
        unplayed = next(game for game in forged["payload"]["games"] if not game["played"])
        unplayed["boxscore_query_sidecar_sha256"] = "0" * 64
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(forged, schema)

        runtime_attacks = []
        forged = snapshots[0].as_mapping()
        forged["season"] = 2019
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["source_query_sidecar_sha256s"] = ["0" * 64]
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["logical_query_ids"] = list(reversed(forged["logical_query_ids"]))
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["logical_query_ids"][0] = "not-a-sha256"
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["unexpected"] = True
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        unplayed = next(game for game in forged["payload"]["games"] if not game["played"])
        unplayed["boxscore_query_sidecar_sha256"] = "0" * 64
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        played = next(game for game in forged["payload"]["games"] if game["played"])
        played["away_score"] = played["home_score"]
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        played = next(game for game in forged["payload"]["games"] if game["played"])
        played["winner_team_id"] = played["away_team_id"]
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["games"][0]["source_game_sha256"] = "0" * 64
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["games"][0]["scheduled_start_utc"] = "not-utc"
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["games"][0]["scheduled_start_utc"] = (
            "2018-04-10T17:05:00Z"
        )
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["logical_query_ids"].pop()
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        played = next(game for game in forged["payload"]["games"] if game["played"])
        played["home_starting_hitter_ids"][0] = played[
            "away_starting_hitter_ids"
        ][0]
        source_game = dict(played)
        source_game.pop("source_game_sha256")
        played["source_game_sha256"] = hashlib.sha256(
            canonical_json_bytes(
                {
                    "anchor_sidecar_sha256": played[
                        "schedule_query_sidecar_sha256"
                    ],
                    "boxscore_query_sidecar_sha256": played[
                        "boxscore_query_sidecar_sha256"
                    ],
                    "game": source_game,
                }
            )
        ).hexdigest()
        runtime_attacks.append(forged)
        for index, attack in enumerate(runtime_attacks):
            with self.subTest(runtime_attack=index), self.assertRaises(ValueError):
                verifier._validate_normalized_snapshot_mapping(attack)

    def test_source_game_snapshot_mapping_binds_disposition_and_anchor_exactly(
        self,
    ) -> None:
        """Source-only semantic fields are immutable even after rehashing."""
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.v52_schedule_inventory import (
            _SOURCE_ANCHOR_KEYS,
            build_schedule_anchor_provenance,
        )
        from mlb_backtest.v52_source_contracts import canonical_sha256

        authority = _normalized_game_authority()
        snapshot = verifier._build_game_normalized_snapshots(
            "historical_walkforward_500",
            authority.records,
            authority.game_inventory,
            authority.timezone_gate,
            authority.schedule_inventory,
        )[1].as_mapping()
        disposition = {
            "query_id": (
                "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095"
            ),
            "query_sidecar": {
                "path": (
                    "audit/v5_2_qd/historical_superseding_565534/acquisition/"
                    "query_sidecars/"
                    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1.json"
                ),
                "sha256": (
                    "0adfb798330b6539391d7d2e160ce9807bb411b01d07ea1a313ee110c57482f1"
                ),
            },
            "success_receipt": {
                "path": (
                    "data/v5_2_qd/historical_superseding_565534/raw/queries/"
                    "mlb_schedule_game_feed/"
                    "8b6f8fc8b99613d6af0b05aeff85b9547f62b78024809a1a8b6d13193409b095/"
                    "success.json"
                ),
                "sha256": "8" * 64,
            },
        }

        def reseal(game: dict[str, object]) -> None:
            lineage = {
                "anchor_sidecar_sha256": game[
                    "schedule_query_sidecar_sha256"
                ],
                "game": {
                    key: value
                    for key, value in game.items()
                    if key != "source_game_sha256"
                },
            }
            if game["played"]:
                lineage["boxscore_query_sidecar_sha256"] = game[
                    "boxscore_query_sidecar_sha256"
                ]
            game["source_game_sha256"] = canonical_sha256(lineage)

        snapshot["run_id"] = "historical_walkforward_500_superseding_565534"
        snapshot["duplicate_gamepk_disposition"] = disposition
        snapshot["dispositioned_query_ids"] = [disposition["query_id"]]
        ordinary_schedule = next(
            record
            for record in authority.records
            if (
                record.query.endpoint
                == "https://statsapi.mlb.com/api/v1/schedule"
                and record.query.requested_range.get("year") == snapshot["season"]
            )
        )
        original_query_ids = set(snapshot["logical_query_ids"])
        original_sidecar_sha256s = set(
            snapshot["source_query_sidecar_sha256s"]
        )
        snapshot_records = tuple(
            record
            for record in authority.records
            if record.query_id in original_query_ids
        )
        self.assertEqual(
            {record.query_sidecar_sha256 for record in snapshot_records},
            original_sidecar_sha256s,
        )
        boxscore_records = tuple(
            record
            for record in snapshot_records
            if record.query.endpoint.endswith("/boxscore")
        )
        boxscore_query_ids = original_query_ids - {ordinary_schedule.query_id}
        boxscore_sidecar_sha256s = original_sidecar_sha256s - {
            ordinary_schedule.query_sidecar_sha256
        }
        self.assertEqual(
            {record.query_id for record in boxscore_records},
            boxscore_query_ids,
        )
        self.assertEqual(
            {record.query_sidecar_sha256 for record in boxscore_records},
            boxscore_sidecar_sha256s,
        )
        snapshot["logical_query_ids"] = sorted(
            disposition["query_id"]
            if value == ordinary_schedule.query_id
            else value
            for value in snapshot["logical_query_ids"]
        )
        exact_sidecar = disposition["query_sidecar"]["sha256"]
        exact_sidecars = sorted(
            exact_sidecar
            if value == ordinary_schedule.query_sidecar_sha256
            else value
            for value in snapshot["payload"]["source_query_sidecar_sha256s"]
        )
        snapshot["source_query_sidecar_sha256s"] = exact_sidecars
        snapshot["payload"]["source_query_sidecar_sha256s"] = exact_sidecars
        for game in snapshot["payload"]["games"]:
            game["schedule_query_sidecar_sha256"] = exact_sidecar
            if not game["played"]:
                reseal(game)
                continue
            game["raw_game_object_sha256"] = "a" * 64
            provenance = build_schedule_anchor_provenance(
                [
                    {
                        key: game[key]
                        for key in _SOURCE_ANCHOR_KEYS
                    }
                ]
            )
            game["normalized_anchor_sha256"] = provenance[game["game_pk"]][
                "normalized_anchor_sha256"
            ]
            reseal(game)

        self.assertEqual(
            set(snapshot["logical_query_ids"]) - {disposition["query_id"]},
            boxscore_query_ids,
        )
        self.assertEqual(
            set(snapshot["source_query_sidecar_sha256s"]) - {exact_sidecar},
            boxscore_sidecar_sha256s,
        )
        self.assertNotIn(
            ordinary_schedule.query_id, snapshot["logical_query_ids"]
        )
        self.assertNotIn(
            ordinary_schedule.query_sidecar_sha256,
            snapshot["source_query_sidecar_sha256s"],
        )

        schema = json.loads(NORMALIZED_SNAPSHOT_SCHEMA.read_bytes())
        jsonschema.validate(snapshot, schema)
        self.assertEqual(
            verifier._validate_normalized_snapshot_mapping(snapshot), snapshot
        )

        attacks: dict[str, dict[str, object]] = {}
        for field, value in (
            ("query_id", "0" * 64),
            ("query_sidecar.path", "sealed/other-sidecar.json"),
            ("query_sidecar.sha256", "0" * 64),
            ("success_receipt.path", "sealed/other-success.json"),
            ("success_receipt.sha256", "0" * 64),
            ("dispositioned_query_ids", ["0" * 64]),
        ):
            forged = json.loads(canonical_json_bytes(snapshot))
            if field == "query_id":
                forged["duplicate_gamepk_disposition"]["query_id"] = value
            elif field == "dispositioned_query_ids":
                forged[field] = value
            else:
                binding, key = field.split(".")
                forged["duplicate_gamepk_disposition"][binding][key] = value
            attacks[field] = forged

        for label, forged in attacks.items():
            with self.subTest(boundary="schema", attack=label), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(forged, schema)
            with self.subTest(boundary="runtime", attack=label), self.assertRaises(
                ValueError
            ):
                verifier._validate_normalized_snapshot_mapping(forged)

        lineage_attacks = {}
        missing_query = json.loads(canonical_json_bytes(snapshot))
        missing_query["logical_query_ids"] = sorted(
            "9" * 64 if value == disposition["query_id"] else value
            for value in missing_query["logical_query_ids"]
        )
        lineage_attacks["omitted-disposition-query"] = missing_query

        missing_sidecar = json.loads(canonical_json_bytes(snapshot))
        replacement_sidecar = "9" * 64
        missing_sidecar["source_query_sidecar_sha256s"] = sorted(
            replacement_sidecar if value == exact_sidecar else value
            for value in missing_sidecar["source_query_sidecar_sha256s"]
        )
        missing_sidecar["payload"]["source_query_sidecar_sha256s"] = list(
            missing_sidecar["source_query_sidecar_sha256s"]
        )
        for game in missing_sidecar["payload"]["games"]:
            game["schedule_query_sidecar_sha256"] = replacement_sidecar
            reseal(game)
        lineage_attacks["omitted-disposition-sidecar"] = missing_sidecar

        for label, forged in lineage_attacks.items():
            with self.subTest(boundary="schema", attack=label), self.assertRaises(
                jsonschema.ValidationError
            ):
                jsonschema.validate(forged, schema)
            with self.subTest(boundary="runtime", attack=label), self.assertRaises(
                ValueError
            ):
                verifier._validate_normalized_snapshot_mapping(forged)

        forged = json.loads(canonical_json_bytes(snapshot))
        played = next(game for game in forged["payload"]["games"] if game["played"])
        played["normalized_anchor_sha256"] = "0" * 64
        reseal(played)
        with self.assertRaises(ValueError):
            verifier._validate_normalized_snapshot_mapping(forged)

    def test_game_snapshot_reducer_accepts_disposition_capability_keywords(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _normalized_game_authority()
        binding = verifier.DuplicateGamePkDispositionBinding(
            query_id="a" * 64,
            success_receipt=verifier.AuthorityBinding("fixture/receipt.json", "b" * 64),
            query_sidecar=verifier.AuthorityBinding("fixture/sidecar.json", "c" * 64),
        )
        with self.assertRaises(ValueError):
            verifier._build_game_normalized_snapshots(
                "historical_walkforward_500",
                authority.records,
                authority.game_inventory,
                authority.timezone_gate,
                authority.schedule_inventory,
                duplicate_gamepk_disposition=binding,
                dispositioned_query_ids=(binding.query_id,),
            )

    def test_source_game_snapshot_validation_authenticates_normalized_anchor(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.v52_source_contracts import canonical_sha256

        authority = _normalized_game_authority()
        snapshot = verifier._build_game_normalized_snapshots(
            "historical_walkforward_500",
            authority.records,
            authority.game_inventory,
            authority.timezone_gate,
            authority.schedule_inventory,
        )[0].as_mapping()
        self.assertEqual(
            verifier._validate_normalized_snapshot_mapping(snapshot), snapshot
        )
        snapshot["run_id"] = "historical_walkforward_500_superseding_565534"
        for game in snapshot["payload"]["games"]:
            if not game["played"]:
                continue
            game["raw_game_object_sha256"] = "a" * 64
            game["normalized_anchor_sha256"] = "b" * 64
            game["source_game_sha256"] = canonical_sha256(
                {
                    "anchor_sidecar_sha256": game[
                        "schedule_query_sidecar_sha256"
                    ],
                    "boxscore_query_sidecar_sha256": game[
                        "boxscore_query_sidecar_sha256"
                    ],
                    "game": {
                        key: value
                        for key, value in game.items()
                        if key != "source_game_sha256"
                    },
                }
            )
        with self.assertRaises(ValueError):
            verifier._validate_normalized_snapshot_mapping(snapshot)

    def test_game_normalized_snapshot_authority_mismatches_fail_closed(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        def build(authority: SimpleNamespace) -> object:
            return verifier._build_game_normalized_snapshots(
                "historical_walkforward_500",
                authority.records,
                authority.game_inventory,
                authority.timezone_gate,
                authority.schedule_inventory,
            )

        authority = _normalized_game_authority()
        attacks = []
        attacks.append(dataclasses.replace(authority.game_inventory, payload={
            **authority.game_inventory.payload,
            "games": list(reversed(authority.game_inventory.payload["games"])),
        }))
        attacks.append(dataclasses.replace(authority.game_inventory, payload={
            **authority.game_inventory.payload,
            "source_query_sidecar_sha256s": ["0" * 64],
        }))
        incomplete = dict(authority.game_inventory.payload)
        incomplete.pop("run_spec")
        attacks.append(dataclasses.replace(
            authority.game_inventory,
            payload=incomplete,
        ))
        attacks.append(dataclasses.replace(authority.game_inventory, payload={
            **authority.game_inventory.payload,
            "counts": {
                **authority.game_inventory.payload["counts"],
                "game_count": 999,
            },
        }))
        attacks.append(dataclasses.replace(
            authority.timezone_gate,
            schedule_inventory_sha256="0" * 64,
        ))
        attacks.append(dataclasses.replace(
            authority.timezone_gate,
            mapping={
                "artifact_kind": "VENUE_TIMEZONE_MAPPING",
                "schema_version": 1,
                "timezone_rows": [],
            },
        ))
        attacks.append(dataclasses.replace(authority.schedule_inventory, payload={
            **authority.schedule_inventory.payload,
            "season_sources": [
                {**row, "query_id": "0" * 64}
                if row["season"] == 2018
                else row
                for row in authority.schedule_inventory.payload["season_sources"]
            ],
        }))
        bad_timezone = json.loads(
            canonical_json_bytes(authority.game_inventory.payload)
        )
        bad_timezone["timezone_gate"]["mapping"]["sha256"] = "0" * 64
        attacks.append(dataclasses.replace(
            authority.game_inventory,
            payload=bad_timezone,
        ))
        for index, replacement in enumerate(attacks):
            current = _normalized_game_authority()
            if index in {0, 1, 2, 3, 7}:
                current.game_inventory = replacement
            elif index in {4, 5}:
                current.timezone_gate = replacement
            else:
                current.schedule_inventory = replacement
            with self.subTest(index=index), self.assertRaisesRegex(
                ValueError,
                "game|schedule|timezone|mapping|sidecar|inventory|authority|differ|coverage",
            ):
                build(current)

        authority = _normalized_game_authority()
        with self.assertRaisesRegex(ValueError, "seven|season|2018|2024"):
            verifier._build_game_normalized_snapshots(
                "historical_walkforward_500",
                authority.records[1:],
                authority.game_inventory,
                authority.timezone_gate,
                authority.schedule_inventory,
            )

    def test_derived_disposition_is_independently_recomputed_without_mutation(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from tests.test_v52_evidence import _derived_query, _derived_semantics

        with tempfile.TemporaryDirectory() as directory:
            root, query = Path(directory).resolve() / "raw", _derived_query()
            store = V52EvidenceStore(root)
            with store.acquire_query(query):
                store.publish_derived_disposition(query, _derived_semantics())
            read_only = V52EvidenceStore.open_read_only(root)
            expected_sidecar = dict(
                read_only.read_verified_success(query).query_sidecar
            )
            before = _snapshot_tree(root)
            with patch.object(
                verifier, "recompute_query_sidecar", wraps=recompute_query_sidecar
            ) as parser:
                result = verifier._read_and_recompute_query(read_only, query)
            parser.assert_called_once_with(query, b"")
            self.assertEqual(
                result,
                {
                    "query_id": logical_query_id(query),
                    "query_sidecar": expected_sidecar,
                    "raw_sha256": None,
                    "storage_mode": "DERIVED_DISPOSITION",
                },
            )
            self.assertEqual(_snapshot_tree(root), before)

    def test_verifier_import_graph_has_no_fetch_or_http_client(self) -> None:
        self.assertTrue(MODULE_PATH.is_file(), "source verifier is not implemented")
        tree = ast.parse(MODULE_PATH.read_bytes(), filename=str(MODULE_PATH))
        imported_modules: set[str] = set()
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported_modules.add(node.module or "")
                imported_names.update(alias.name for alias in node.names)

        forbidden_modules = {
            "aiohttp",
            "http.client",
            "httpx",
            "mlb_backtest.sources",
            "mlb_backtest.v52_http",
            "requests",
            "urllib.error",
            "urllib.request",
        }
        self.assertFalse(
            any(
                imported == forbidden or imported.startswith(f"{forbidden}.")
                for imported in imported_modules
                for forbidden in forbidden_modules
            ),
            "offline verifier must not import an HTTP client module",
        )
        self.assertNotIn("v52_http", imported_names)
        self.assertFalse(
            any("fetch" in name.lower() for name in imported_names),
            "offline verifier must not import a fetch function",
        )

        blocked = {name: None for name in forbidden_modules}
        package = importlib.import_module("mlb_backtest")
        original = sys.modules["mlb_backtest.v52_verify_sources"]
        with (
            patch.object(package, "v52_verify_sources", original),
            patch.dict(sys.modules, blocked),
        ):
            sys.modules.pop("mlb_backtest.v52_verify_sources", None)
            imported = importlib.import_module("mlb_backtest.v52_verify_sources")
        self.assertTrue(hasattr(imported, "_read_and_recompute_query"))
        self.assertIs(sys.modules["mlb_backtest.v52_verify_sources"], original)
        self.assertIs(package.v52_verify_sources, original)

    def test_registered_parser_dispatch_imports_no_network_surface(self) -> None:
        for case in PARSER_IMPORT_GUARD_CASES:
            with self.subTest(case=case):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        PARSER_IMPORT_GUARD_SCRIPT,
                        case,
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stderr or completed.stdout,
                )

    def test_parser_guard_rejects_preloaded_blocked_module(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                PARSER_IMPORT_GUARD_SCRIPT,
                "schedule",
                "preload",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("NETWORK_MODULE_PRELOADED:requests", completed.stderr)

    def test_owned_fetched_query_is_reread_and_recomputed_without_mutation(
        self,
    ) -> None:
        from mlb_backtest.v52_verify_sources import _read_and_recompute_query

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "raw"
            _, query, raw = _owned_schedule_evidence(root)
            read_only = V52EvidenceStore.open_read_only(root)
            verified = read_only.read_verified_success(query)
            expected = {
                "query_id": logical_query_id(query),
                "query_sidecar": dict(verified.query_sidecar),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "storage_mode": "FETCHED_RAW",
            }
            before = _snapshot_tree(root)

            first = _read_and_recompute_query(read_only, query)
            second = _read_and_recompute_query(read_only, query)

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertEqual(_snapshot_tree(root), before)

    def test_sidecar_forgery_and_raw_mutation_fail_closed_without_cleanup(
        self,
    ) -> None:
        from mlb_backtest.v52_verify_sources import _read_and_recompute_query

        for mutation in ("sidecar", "raw"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve() / "raw"
                _, query, _ = _owned_schedule_evidence(root)
                if mutation == "sidecar":
                    _forge_sidecar_semantics(root, query)
                    forged = V52EvidenceStore.open_read_only(
                        root
                    ).read_verified_success(query)
                    self.assertEqual(
                        forged.query_sidecar["semantics"]["game_pks"],
                        [999999],
                    )
                else:
                    verified = V52EvidenceStore.open_read_only(
                        root
                    ).read_verified_success(query)
                    raw_path = root / str(verified.success_receipt["fetched_raw_path"])
                    raw_path.write_bytes(raw_path.read_bytes() + b" ")
                before = _snapshot_tree(root)

                with self.assertRaisesRegex(
                    (RuntimeError, ValueError),
                    "raw|sidecar|semantic|hash|identity|binding|changed",
                ):
                    _read_and_recompute_query(
                        V52EvidenceStore.open_read_only(root),
                        query,
                    )

                self.assertEqual(_snapshot_tree(root), before)

    def test_raw_mutation_during_parser_recompute_fails_without_cleanup(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "raw"
            _, query, _ = _owned_schedule_evidence(root)
            verified = V52EvidenceStore.open_read_only(root).read_verified_success(
                query
            )
            raw_path = root / str(verified.success_receipt["fetched_raw_path"])
            before = _snapshot_tree(root)
            attacker_snapshots: list[
                dict[Path, tuple[int, int, int, int, bytes | None]]
            ] = []

            def recompute_then_mutate(
                supplied_query: LogicalQuery,
                raw: bytes,
            ) -> object:
                semantics = recompute_query_sidecar(supplied_query, raw)
                raw_path.write_bytes(raw_path.read_bytes() + b" ")
                attacker_snapshots.append(_snapshot_tree(root))
                return semantics

            with (
                patch.object(
                    v52_verify_sources,
                    "recompute_query_sidecar",
                    side_effect=recompute_then_mutate,
                ),
                self.assertRaisesRegex(
                    (RuntimeError, ValueError),
                    "raw|hash|identity|binding|changed|rebind",
                ),
            ):
                v52_verify_sources._read_and_recompute_query(
                    V52EvidenceStore.open_read_only(root),
                    query,
                )

            self.assertEqual(len(attacker_snapshots), 1)
            self.assertNotEqual(attacker_snapshots[0], before)
            self.assertEqual(_snapshot_tree(root), attacker_snapshots[0])
