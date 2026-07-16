from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.execution_registry import AcquisitionCheckpoint
from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id
from mlb_backtest.v52_query_plan import (
    QueryBatch,
    SplitReceipt,
    _build_statcast_root_batch,
    bisect_statcast_query,
)
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from mlb_backtest.v52_sources.schedule import canonical_sha256
from mlb_backtest.v52_sources.statcast import recompute_statcast_sidecar
from tests.test_v52_verify_sources import _normalized_game_authority


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
SCHEMA = ROOT / "schemas/v5_2_normalized_snapshot.schema.json"
STATCAST_RAW = ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv"


def _record(query: LogicalQuery, raw: bytes):
    from mlb_backtest.v52_verify_sources import VerifiedQueryRecord

    sidecar = recompute_statcast_sidecar(raw, query)
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


def _game_snapshots():
    from mlb_backtest import v52_verify_sources as verifier

    authority = _normalized_game_authority()
    originals = verifier._build_game_normalized_snapshots(
        "historical_walkforward_500",
        authority.records,
        authority.game_inventory,
        authority.timezone_gate,
        authority.schedule_inventory,
    )
    valid_dates = {2020: "2020-07-24", 2022: "2022-04-08"}
    snapshots = []
    for snapshot in originals:
        payload = snapshot.as_mapping()["payload"]
        if snapshot.season in valid_dates:
            game = next(item for item in payload["games"] if item["played"])
            day = valid_dates[snapshot.season]
            game["official_date"] = day
            game["feature_history_cutoff"] = str(
                date.fromisoformat(day) - timedelta(days=1)
            )
            game["scheduled_start_utc"] = f"{day}T17:05:00Z"
            projection = dict(game)
            projection.pop("source_game_sha256")
            game["source_game_sha256"] = canonical_sha256(
                {
                    "anchor_sidecar_sha256": game[
                        "schedule_query_sidecar_sha256"
                    ],
                    "boxscore_query_sidecar_sha256": game[
                        "boxscore_query_sidecar_sha256"
                    ],
                    "game": projection,
                }
            )
        season_records = tuple(
            record
            for record in authority.records
            if record.query.requested_range.get("year") == snapshot.season
            or record.query.requested_range.get("season") == snapshot.season
        )
        snapshots.append(
            verifier._normalized_snapshot(
                "historical_walkforward_500",
                snapshot.season,
                season_records,
                payload,
            )
        )
    return tuple(snapshots)


def _statcast_authority():
    from mlb_backtest import v52_verify_sources as verifier

    roots = _build_statcast_root_batch(RUN_SPEC, CONTRACT)
    partitions = tuple(
        dataclasses.replace(
            root,
            requested_range={**root.requested_range, "acquisition": "FETCH_REQUIRED"},
            parent_query_id=logical_query_id(root),
        )
        for root in roots.queries
    )
    recursive_parent = next(
        query
        for query in partitions
        if query.requested_range["year"] == 2024
        and query.requested_range["start_date"].endswith("-04-01")
    )
    children = bisect_statcast_query(recursive_parent)
    grandchildren = bisect_statcast_query(children[0])
    first_batch = QueryBatch("1" * 64, "REMAINING", None, partitions)
    child_batch = QueryBatch("2" * 64, "REMAINING", first_batch.batch_id, children)
    grandchild_batch = QueryBatch(
        "5" * 64, "REMAINING", child_batch.batch_id, grandchildren
    )
    receipt = SplitReceipt(
        "3" * 64,
        "4" * 64,
        "REMAINING",
        first_batch.batch_id,
        logical_query_id(recursive_parent),
        "ROW_CAP",
        1,
        1,
        child_batch.batch_id,
        tuple(logical_query_id(query) for query in children),
        "2026-07-16T12:00:00.000000Z",
    )
    nested_receipt = SplitReceipt(
        "7" * 64,
        "8" * 64,
        "REMAINING",
        child_batch.batch_id,
        logical_query_id(children[0]),
        "TIMEOUT",
        2,
        2,
        grandchild_batch.batch_id,
        tuple(logical_query_id(query) for query in grandchildren),
        "2026-07-16T12:00:01.000000Z",
    )
    completed = (
        tuple(query for query in partitions if query != recursive_parent)
        + (children[1],)
        + grandchildren
    )
    receipts = (receipt, nested_receipt)
    checkpoint = AcquisitionCheckpoint(
        4,
        "a" * 64,
        "b" * 64,
        "REMAINING",
        "2026-07-16T12:00:00.000000Z",
        "c" * 40,
        "d" * 64,
        (first_batch.batch_id, child_batch.batch_id, grandchild_batch.batch_id),
        ("9" * 64, "a" * 64, "b" * 64),
        (),
        (),
        tuple(
            item.receipt_sha256
            for item in sorted(receipts, key=lambda item: item.parent_query_id)
        ),
        tuple(sorted(logical_query_id(query) for query in completed)),
        (),
        (),
        tuple(sorted(item.parent_query_id for item in receipts)),
        (),
    )
    graph = verifier._enumerate_completed_query_graph(
        checkpoint,
        (first_batch, child_batch, grandchild_batch),
        receipts,
        roots,
    )
    games = _game_snapshots()
    played_by_date = {
        game["official_date"]: game["game_pk"]
        for snapshot in games
        for game in snapshot.payload["games"]
        if game["played"]
    }
    header = STATCAST_RAW.read_bytes().splitlines(keepends=True)[0]
    records = []
    for query in completed:
        game = next(
            (
                (day, game_pk)
                for day, game_pk in played_by_date.items()
                if query.requested_range["start_date"]
                <= day
                <= query.requested_range["end_date"]
            ),
            None,
        )
        raw = header
        if game is not None:
            day, game_pk = game
            raw = STATCAST_RAW.read_bytes().replace(b"2024-04-02", day.encode()).replace(
                b"9001", str(game_pk).encode()
            )
        records.append(_record(query, raw))
    return roots, graph, tuple(reversed(records)), games


class V52VerifyStatcastSnapshotsTest(unittest.TestCase):
    def test_builds_46_monthly_snapshots_from_ready_leaves_and_receipts(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        roots, graph, records, games = _statcast_authority()
        with patch.object(
            verifier,
            "recompute_statcast_season_snapshot",
            wraps=verifier.recompute_statcast_season_snapshot,
        ) as recompute:
            snapshots = verifier._build_statcast_normalized_snapshots(
                "historical_walkforward_500", records, graph, games
            )

        self.assertEqual(recompute.call_count, 46)
        self.assertEqual(len(snapshots), 46)
        self.assertEqual(
            tuple((item.season, item.payload["parent_coverage"]) for item in snapshots),
            tuple(
                (
                    root.requested_range["year"],
                    {
                        "inclusive_start": root.requested_range["start_date"],
                        "inclusive_end": root.requested_range["end_date"],
                    },
                )
                for root in roots.queries
            ),
        )
        schema = json.loads(SCHEMA.read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        recursive = [item for item in snapshots if item.split_receipt_sha256s]
        self.assertEqual(len(recursive), 1)
        receipt_by_parent = {
            receipt.parent_query_id: receipt.receipt_sha256
            for receipt in graph.split_receipts
        }
        self.assertEqual(
            recursive[0].split_receipt_sha256s,
            tuple(receipt_by_parent[parent] for parent in sorted(receipt_by_parent)),
        )
        self.assertEqual(len(recursive[0].logical_query_ids), 3)
        permuted = verifier._build_statcast_normalized_snapshots(
            "historical_walkforward_500",
            records,
            dataclasses.replace(
                graph, split_receipts=tuple(reversed(graph.split_receipts))
            ),
            games,
        )
        self.assertEqual(
            tuple(item.canonical_bytes for item in snapshots),
            tuple(item.canonical_bytes for item in permuted),
        )
        for snapshot in snapshots:
            envelope = snapshot.as_mapping()
            jsonschema.validate(envelope, schema)
            self.assertEqual(snapshot.source, "baseball_savant_statcast")
            self.assertEqual(snapshot.grain, "pitch")
            self.assertEqual(
                snapshot.canonical_bytes, canonical_json_bytes(envelope) + b"\n"
            )
            self.assertEqual(
                snapshot.sha256,
                hashlib.sha256(snapshot.canonical_bytes).hexdigest(),
            )
            self.assertNotIn(snapshot.sha256, snapshot.logical_query_ids)
            self.assertNotIn(
                snapshot.sha256, snapshot.source_query_sidecar_sha256s
            )

    def test_statcast_builder_and_runtime_fail_closed_on_authority_attacks(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        roots, graph, records, games = _statcast_authority()
        snapshots = verifier._build_statcast_normalized_snapshots(
            "historical_walkforward_500", records, graph, games
        )

        forged_sidecar = json.loads(records[0].query_sidecar_bytes)
        forged_sidecar["semantics"]["split_disposition"] = "SPLIT_REQUIRED"
        forged_contents = canonical_json_bytes(forged_sidecar) + b"\n"
        non_ready = dataclasses.replace(
            records[0],
            query_sidecar_bytes=forged_contents,
            query_sidecar_sha256=hashlib.sha256(forged_contents).hexdigest(),
        )
        root_batch_short = dataclasses.replace(
            roots,
            queries=roots.queries[:-1],
        )
        extra_record = _normalized_game_authority().records[0]
        builder_attacks = (
            (records[:-1], graph, games),
            ((non_ready, *records[1:]), graph, games),
            ((*records, extra_record), graph, games),
            (
                records,
                dataclasses.replace(graph, statcast_root_batch=root_batch_short),
                games,
            ),
            (records, dataclasses.replace(graph, split_receipts=()), games),
            (records, graph, games[:-1]),
        )
        for index, (bad_records, bad_graph, bad_games) in enumerate(builder_attacks):
            with self.subTest(builder_attack=index), self.assertRaises(ValueError):
                verifier._build_statcast_normalized_snapshots(
                    "historical_walkforward_500",
                    bad_records,
                    bad_graph,
                    bad_games,
                )

        runtime_attacks = []
        forged = snapshots[0].as_mapping()
        forged["source"] = "mlb_schedule_game_feed"
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["grain"] = "season_game_evidence_snapshot"
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["season"] = 2019
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["parent_coverage"]["inclusive_end"] = "2018-04-01"
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["source_query_sidecar_sha256s"][0] = "0" * 64
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["child_query_sidecar_sha256s"].append("0" * 64)
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["payload"]["unexpected"] = True
        runtime_attacks.append(forged)
        forged = snapshots[0].as_mapping()
        forged["unexpected"] = True
        runtime_attacks.append(forged)
        for index, attack in enumerate(runtime_attacks):
            with self.subTest(runtime_attack=index), self.assertRaises(ValueError):
                verifier._validate_normalized_snapshot_mapping(attack)

        schema = json.loads(SCHEMA.read_bytes())
        forged = snapshots[0].as_mapping()
        forged["payload"]["unexpected"] = True
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(forged, schema)

    def test_terminal_statcast_leaf_must_retain_partition_disposition(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        _roots, graph, records, games = _statcast_authority()
        original = next(
            record
            for record in records
            if record.query.requested_range["start_date"] == "2018-03-29"
        )
        requested = dict(original.query.requested_range)
        requested.pop("acquisition")
        forged_query = dataclasses.replace(
            original.query,
            requested_range=requested,
        )
        header = STATCAST_RAW.read_bytes().splitlines(keepends=True)[0]
        forged_record = _record(forged_query, header)
        forged_records = tuple(
            forged_record if record is original else record for record in records
        )
        forged_batches = tuple(
            dataclasses.replace(
                batch,
                queries=tuple(
                    forged_query if query == original.query else query
                    for query in batch.queries
                ),
            )
            for batch in graph.query_batches
        )
        forged_graph = dataclasses.replace(
            graph,
            query_batches=forged_batches,
            completed_queries=tuple(
                forged_query if query == original.query else query
                for query in graph.completed_queries
            ),
            root_queries=tuple(
                forged_query if query == original.query else query
                for query in graph.root_queries
            ),
        )

        with self.assertRaisesRegex(ValueError, "acquisition|partition"):
            verifier._build_statcast_normalized_snapshots(
                "historical_walkforward_500",
                forged_records,
                forged_graph,
                games,
            )


if __name__ == "__main__":
    unittest.main()
