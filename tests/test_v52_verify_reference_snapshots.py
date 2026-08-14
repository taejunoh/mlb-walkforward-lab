from __future__ import annotations

import dataclasses
import hashlib
import json
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id
from mlb_backtest.v52_query_plan import QueryBatch, SplitReceipt, _make_batch
from mlb_backtest.v52_seasons import SEASON_BOUNDARIES
from mlb_backtest.v52_source_contracts import (
    canonical_json_bytes,
    query_sidecar,
    recompute_query_sidecar,
)
from mlb_backtest.v52_sources.schedule import canonical_sha256
from tests.test_v52_verify_sources import _normalized_game_authority


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v5_2_normalized_snapshot.schema.json"
RUN_ID = "historical_walkforward_500"
SOURCE = "venue_team_timezone_table"
GRAIN = "effective_dated_venue_team_reference"
RUN_SHA = "2" * 64
CONTRACT_SHA = "1" * 64
SCHEDULE_INVENTORY_SHA = "8" * 64


def _file_sha(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value) + b"\n").hexdigest()


def _team_query(season: int) -> LogicalQuery:
    return LogicalQuery(
        source=SOURCE,
        grain=GRAIN,
        endpoint="https://statsapi.mlb.com/api/v1/teams",
        parameters={
            "sportId": 1,
            "season": season,
            "hydrate": "venue(location)",
        },
        requested_range={"season": season, "entity": "teams"},
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
    )


def _venue_query(venue_id: int) -> LogicalQuery:
    return LogicalQuery(
        source=SOURCE,
        grain=GRAIN,
        endpoint=f"https://statsapi.mlb.com/api/v1/venues/{venue_id}",
        parameters={"hydrate": "location"},
        requested_range={"venue_id": venue_id},
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
    )


def _schedule_query(season: int) -> LogicalQuery:
    first, last = SEASON_BOUNDARIES[season]
    return LogicalQuery(
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        endpoint="https://statsapi.mlb.com/api/v1/schedule",
        parameters={
            "sportId": 1,
            "gameType": "R",
            "startDate": first.isoformat(),
            "endDate": last.isoformat(),
            "hydrate": "probablePitcher,lineups,venue,linescore",
        },
        requested_range={
            "year": season,
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
    )


def _team_raw(season: int) -> bytes:
    return canonical_json_bytes(
        {
            "teams": [
                {
                    "id": 10,
                    "locationName": f"Away {season}",
                    "name": f"Away Club {season}",
                    "venue": {"id": 31},
                },
                {
                    "id": 20,
                    "locationName": f"Home {season}",
                    "name": f"Home Club {season}",
                    "venue": {"id": 31},
                },
            ]
        }
    )


def _venue_raw(venue_id: int) -> bytes:
    return canonical_json_bytes(
        {
            "venues": [
                {
                    "id": venue_id,
                    "location": {
                        "city": f"City {venue_id}",
                        "defaultCoordinates": {
                            "latitude": 40.0 + venue_id / 1000,
                            "longitude": -73.0 - venue_id / 1000,
                        },
                    },
                    "name": f"Venue {venue_id}",
                }
            ]
        }
    )


def _record(query: LogicalQuery, raw: bytes):
    from mlb_backtest.v52_verify_sources import VerifiedQueryRecord

    sidecar = query_sidecar(raw, query, recompute_query_sidecar(query, raw))
    contents = canonical_json_bytes(sidecar) + b"\n"
    return VerifiedQueryRecord(
        query=query,
        query_id=logical_query_id(query),
        query_sidecar_bytes=contents,
        query_sidecar_sha256=hashlib.sha256(contents).hexdigest(),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        storage_mode="FETCHED_RAW",
        retained_raw_bytes=raw,
    )


def _games():
    from mlb_backtest import v52_verify_sources as verifier

    authority = _normalized_game_authority()
    return verifier._build_game_normalized_snapshots(
        RUN_ID,
        authority.records,
        authority.game_inventory,
        authority.timezone_gate,
        authority.schedule_inventory,
    )


def _authority() -> SimpleNamespace:
    from mlb_backtest import v52_verify_sources as verifier
    from mlb_backtest.v52_timezone_review import VerifiedTimezoneGate

    games = _games()
    queries = tuple(_team_query(season) for season in range(2018, 2025)) + tuple(
        _venue_query(venue_id) for venue_id in (31, 99)
    )
    static_queries = tuple(_schedule_query(season) for season in range(2018, 2025))
    records = tuple(
        _record(
            query,
            _team_raw(int(query.requested_range["season"]))
            if query.endpoint.endswith("/teams")
            else _venue_raw(int(query.requested_range["venue_id"])),
        )
        for query in queries
    )
    batch = _make_batch("REFERENCE_INPUTS", SCHEDULE_INVENTORY_SHA, queries)
    static_batch = _make_batch("STATIC_SCHEDULES", None, static_queries)
    reference_batch_sha = verifier._query_batch_artifact_sha256(batch)
    static_batch_sha = verifier._query_batch_artifact_sha256(static_batch)
    graph = verifier._TerminalQueryGraph(
        checkpoint=None,  # type: ignore[arg-type]
        statcast_root_batch=QueryBatch("f" * 64, "REMAINING", None, ()),
        query_batches=(static_batch, batch),
        split_receipts=(),
        split_parent_queries=(),
        completed_queries=static_queries + queries,
        root_queries=static_queries + queries,
        split_leaf_queries=(),
    )

    venue_records = {
        int(record.query.requested_range["venue_id"]): record
        for record in records
        if "venue_id" in record.query.requested_range
    }
    observations = sorted(
        {
            (
                int(game["venue_id"]),
                str(game["official_date"]),
                bool(game["neutral_site"]),
                bool(game["alternate_site"]),
            )
            for snapshot in games
            for game in snapshot.payload["games"]
        }
    )
    observed_rows = [
        {
            "alternate_site": alternate,
            "neutral_site": neutral,
            "official_date": official,
            "venue_id": venue_id,
        }
        for venue_id, official, neutral, alternate in observations
    ]
    venue_identities = [
        recompute_query_sidecar(venue_records[venue_id].query, _venue_raw(venue_id))[
            "venues"
        ][0]
        for venue_id in (31, 99)
    ]
    candidate_rows = [
        {
            "effective_end": "2000-12-31",
            "effective_start": "1900-01-01",
            "review_status": "UNREVIEWED",
            "time_zone": "America/New_York",
            "venue_id": venue_id,
        }
        for venue_id in (31, 99)
    ] + [
        {
            "effective_end": "2024-12-31",
            "effective_start": "2001-01-01",
            "review_status": "UNREVIEWED",
            "time_zone": "America/New_York",
            "venue_id": venue_id,
        }
        for venue_id in (31, 99)
    ]
    mapping_rows = [
        {key: value for key, value in row.items() if key != "review_status"}
        for row in sorted(
            candidate_rows,
            key=lambda row: (
                row["venue_id"],
                row["effective_start"],
                row["effective_end"],
            ),
        )
    ]
    candidate = {
        "acquisition_contract_sha256": CONTRACT_SHA,
        "artifact_kind": "VENUE_TIMEZONE_CANDIDATE",
        "observed_venue_dates": observed_rows,
        "query_batch_ids": sorted((static_batch.batch_id, batch.batch_id)),
        "run_spec_sha256": RUN_SHA,
        "schedule_sidecar_sha256s": sorted(
            {
                str(game["schedule_query_sidecar_sha256"])
                for snapshot in games
                for game in snapshot.payload["games"]
            }
        ),
        "schema_version": 1,
        "timezone_rows": candidate_rows,
        "venue_scope": [31, 99],
        "venue_sidecar_sha256s": sorted(
            record.query_sidecar_sha256 for record in venue_records.values()
        ),
        "venues": venue_identities,
    }
    mapping = {
        "artifact_kind": "VENUE_TIMEZONE_MAPPING",
        "schema_version": 1,
        "timezone_rows": mapping_rows,
    }
    candidate_sha = _file_sha(candidate)
    mapping_sha = _file_sha(mapping)
    candidate_binding = {
        "path": "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json",
        "sha256": candidate_sha,
    }
    reviews = (
        {
            "candidate": candidate_binding,
            "decision": "APPROVED",
            "occurred_at_utc": "2026-07-16T12:00:00.000000Z",
            "receipt_kind": "TIMEZONE_REVIEW",
            "reviewed_venue_ids": [31, 99],
            "reviewer_identity": "coverage-reviewer",
            "reviewer_role": "TIMEZONE_COVERAGE_REVIEW",
            "schema_version": 1,
        },
        {
            "candidate": candidate_binding,
            "decision": "APPROVED",
            "occurred_at_utc": "2026-07-16T12:00:01.000000Z",
            "receipt_kind": "TIMEZONE_REVIEW",
            "reviewed_venue_ids": [31, 99],
            "reviewer_identity": "semantic-reviewer",
            "reviewer_role": "TIMEZONE_SEMANTIC_REVIEW",
            "schema_version": 1,
        },
    )
    review_hashes = tuple(_file_sha(review) for review in reviews)
    review_paths = (
        "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json",
        "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json",
    )
    seal = {
        "candidate": candidate_binding,
        "mapping": {
            "path": "config/v5_2_qd/venue_timezones.json",
            "sha256": mapping_sha,
        },
        "occurred_at_utc": "2026-07-16T12:00:02.000000Z",
        "receipt_kind": "TIMEZONE_MAPPING_SEAL",
        "reviews": [
            {
                "path": path,
                "reviewer_identity": review["reviewer_identity"],
                "reviewer_role": review["reviewer_role"],
                "sha256": digest,
            }
            for path, review, digest in zip(
                review_paths, reviews, review_hashes, strict=True
            )
        ],
        "schema_version": 1,
        "venue_scope": [31, 99],
    }
    gate = VerifiedTimezoneGate(
        candidate=candidate,
        mapping=mapping,
        reviews=reviews,
        seal=seal,
        candidate_sha256=candidate_sha,
        mapping_sha256=mapping_sha,
        review_sha256s=review_hashes,
        seal_sha256=_file_sha(seal),
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
        schedule_inventory_sha256=SCHEDULE_INVENTORY_SHA,
        query_batch_ids=(static_batch.batch_id, batch.batch_id),
        query_batch_sha256s=(static_batch_sha, reference_batch_sha),
    )
    return SimpleNamespace(
        batch=batch,
        games=games,
        gate=gate,
        graph=graph,
        records=records,
        static_batch=static_batch,
    )


def _rebuild_snapshot(snapshot, mapping):
    from mlb_backtest import v52_verify_sources as verifier

    contents = canonical_json_bytes(mapping) + b"\n"
    return verifier.NormalizedSnapshot(
        schema_version=1,
        run_id=str(mapping["run_id"]),
        season=int(mapping["season"]),
        source=str(mapping["source"]),  # type: ignore[arg-type]
        grain=str(mapping["grain"]),  # type: ignore[arg-type]
        status="READY",
        logical_query_ids=tuple(mapping["logical_query_ids"]),
        source_query_sidecar_sha256s=tuple(
            mapping["source_query_sidecar_sha256s"]
        ),
        split_receipt_sha256s=tuple(mapping["split_receipt_sha256s"]),
        payload=mapping["payload"],
        canonical_bytes=contents,
        sha256=hashlib.sha256(contents).hexdigest(),
    )


def _source_game_hash(game: dict[str, object]) -> str:
    projected = dict(game)
    projected.pop("source_game_sha256", None)
    lineage: dict[str, object] = {
        "anchor_sidecar_sha256": projected["schedule_query_sidecar_sha256"],
        "game": projected,
    }
    if projected["played"] is True:
        lineage["boxscore_query_sidecar_sha256"] = projected[
            "boxscore_query_sidecar_sha256"
        ]
    return canonical_sha256(lineage)


def _resign_gate(gate, games, candidate_rows):
    candidate = json.loads(canonical_json_bytes(gate.candidate))
    candidate["observed_venue_dates"] = [
        {
            "alternate_site": alternate,
            "neutral_site": neutral,
            "official_date": official,
            "venue_id": venue_id,
        }
        for venue_id, official, neutral, alternate in sorted(
            {
                (
                    int(game["venue_id"]),
                    str(game["official_date"]),
                    bool(game["neutral_site"]),
                    bool(game["alternate_site"]),
                )
                for snapshot in games
                for game in snapshot.payload["games"]
            }
        )
    ]
    candidate["timezone_rows"] = candidate_rows
    candidate_sha = _file_sha(candidate)
    mapping = {
        "artifact_kind": "VENUE_TIMEZONE_MAPPING",
        "schema_version": 1,
        "timezone_rows": [
            {key: value for key, value in row.items() if key != "review_status"}
            for row in sorted(
                candidate_rows,
                key=lambda row: (
                    row["venue_id"],
                    row["effective_start"],
                    row["effective_end"],
                ),
            )
        ],
    }
    mapping_sha = _file_sha(mapping)
    reviews = []
    for review in gate.reviews:
        item = json.loads(canonical_json_bytes(review))
        item["candidate"]["sha256"] = candidate_sha
        reviews.append(item)
    review_hashes = tuple(_file_sha(review) for review in reviews)
    seal = json.loads(canonical_json_bytes(gate.seal))
    seal["candidate"]["sha256"] = candidate_sha
    seal["mapping"]["sha256"] = mapping_sha
    for binding, digest in zip(seal["reviews"], review_hashes, strict=True):
        binding["sha256"] = digest
    return dataclasses.replace(
        gate,
        candidate=candidate,
        mapping=mapping,
        reviews=tuple(reviews),
        seal=seal,
        candidate_sha256=candidate_sha,
        mapping_sha256=mapping_sha,
        review_sha256s=review_hashes,
        seal_sha256=_file_sha(seal),
    )


class V52VerifyReferenceSnapshotsTest(unittest.TestCase):
    def test_builds_seven_reference_snapshots_with_exact_membership(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        with patch.object(
            verifier,
            "recompute_reference_sidecar",
            wraps=verifier.recompute_reference_sidecar,
        ) as recompute:
            snapshots = verifier._build_reference_normalized_snapshots(
                RUN_ID,
                tuple(reversed(authority.records)),
                authority.graph,
                tuple(reversed(authority.games)),
                authority.gate,
            )

        self.assertEqual(recompute.call_count, 7)
        self.assertEqual(tuple(item.season for item in snapshots), tuple(range(2018, 2025)))
        schema = json.loads(SCHEMA.read_text())
        venue_ids = {
            logical_query_id(_venue_query(31)),
            logical_query_id(_venue_query(99)),
        }
        for snapshot in snapshots:
            self.assertEqual(snapshot.source, SOURCE)
            self.assertEqual(snapshot.grain, GRAIN)
            self.assertEqual(snapshot.split_receipt_sha256s, ())
            self.assertEqual(
                set(snapshot.logical_query_ids),
                {logical_query_id(_team_query(snapshot.season))} | venue_ids,
            )
            self.assertEqual(
                [row["venue_id"] for row in snapshot.payload["venues"]], [31, 99]
            )
            self.assertEqual(len(snapshot.payload["timezone_rows"]), 2)
            self.assertTrue(
                all(
                    row["effective_start"] == "2001-01-01"
                    for row in snapshot.payload["timezone_rows"]
                )
            )
            self.assertEqual(
                snapshot.canonical_bytes,
                canonical_json_bytes(snapshot.as_mapping()) + b"\n",
            )
            self.assertEqual(
                snapshot.sha256,
                hashlib.sha256(snapshot.canonical_bytes).hexdigest(),
            )
            with self.assertRaises(TypeError):
                snapshot.payload["grain"] = "changed"
            jsonschema.validate(snapshot.as_mapping(), schema)

    def test_unplayed_game_still_requires_global_venue_membership(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        missing_99 = tuple(
            record
            for record in authority.records
            if record.query.requested_range.get("venue_id") != 99
        )
        with self.assertRaisesRegex(ValueError, "record|venue|population"):
            verifier._build_reference_normalized_snapshots(
                RUN_ID, missing_99, authority.graph, authority.games, authority.gate
            )

    def test_rule_b_includes_both_effective_rows_used_midseason(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        games = list(authority.games)
        original = games[-1]
        mapping = original.as_mapping()
        unplayed = next(game for game in mapping["payload"]["games"] if not game["played"])
        unplayed["venue_id"] = 31
        unplayed["official_date"] = "2024-08-01"
        unplayed["feature_history_cutoff"] = "2024-07-31"
        unplayed["scheduled_start_utc"] = "2024-08-01T17:05:00Z"
        unplayed["source_game_sha256"] = _source_game_hash(unplayed)
        games[-1] = _rebuild_snapshot(original, mapping)
        candidate_rows = [
            {
                "effective_end": "2024-06-30",
                "effective_start": "2001-01-01",
                "review_status": "UNREVIEWED",
                "time_zone": "America/New_York",
                "venue_id": 31,
            },
            {
                "effective_end": "2024-12-31",
                "effective_start": "2024-07-01",
                "review_status": "UNREVIEWED",
                "time_zone": "America/Chicago",
                "venue_id": 31,
            },
            {
                "effective_end": "2024-12-31",
                "effective_start": "2001-01-01",
                "review_status": "UNREVIEWED",
                "time_zone": "America/New_York",
                "venue_id": 99,
            },
        ]
        gate = _resign_gate(authority.gate, games, candidate_rows)

        snapshots = verifier._build_reference_normalized_snapshots(
            RUN_ID, authority.records, authority.graph, games, gate
        )

        self.assertEqual(
            snapshots[-1].payload["timezone_rows"],
            tuple(
                {key: value for key, value in row.items() if key != "review_status"}
                for row in candidate_rows[:2]
            ),
        )
        self.assertEqual(
            [row["time_zone"] for row in snapshots[-1].payload["observed_venue_date_mappings"]],
            ["America/New_York", "America/Chicago"],
        )

    def test_retained_raw_and_sidecar_attacks_fail_closed(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        first = authority.records[0]
        attacks = (
            dataclasses.replace(first, retained_raw_bytes=None),
            dataclasses.replace(first, retained_raw_bytes=b"{}"),
            dataclasses.replace(first, raw_sha256="0" * 64),
            dataclasses.replace(first, storage_mode="DERIVED_DISPOSITION"),
            dataclasses.replace(first, query_sidecar_bytes=first.query_sidecar_bytes + b" "),
            dataclasses.replace(first, query_sidecar_sha256="0" * 64),
        )
        for attack in attacks:
            records = (attack,) + authority.records[1:]
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID, records, authority.graph, authority.games, authority.gate
                )

    def test_graph_batch_and_split_attacks_fail_closed(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        first_query = authority.graph.completed_queries[0]
        receipt = SplitReceipt(
            "1" * 64,
            "2" * 64,
            "REMAINING",
            authority.batch.batch_id,
            logical_query_id(first_query),
            "ROW_CAP",
            1,
            1,
            "3" * 64,
            ("4" * 64, "5" * 64),
            "2026-07-16T12:00:00.000000Z",
        )
        attacks = (
            dataclasses.replace(
                authority.graph,
                completed_queries=authority.graph.completed_queries[1:],
            ),
            dataclasses.replace(
                authority.graph,
                root_queries=authority.graph.root_queries[1:],
            ),
            dataclasses.replace(
                authority.graph, split_parent_queries=(first_query,)
            ),
            dataclasses.replace(authority.graph, split_receipts=(receipt,)),
            dataclasses.replace(
                authority.graph,
                query_batches=(
                    dataclasses.replace(authority.batch, stage="REMAINING"),
                ),
            ),
        )
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID, authority.records, attack, authority.games, authority.gate
                )

    def test_reauthenticates_both_gate_batches_by_id_not_tuple_position(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        baseline = verifier._build_reference_normalized_snapshots(
            RUN_ID,
            authority.records,
            authority.graph,
            authority.games,
            authority.gate,
        )
        reordered_gate = dataclasses.replace(
            authority.gate,
            query_batch_ids=tuple(reversed(authority.gate.query_batch_ids)),
            query_batch_sha256s=tuple(
                reversed(authority.gate.query_batch_sha256s)
            ),
        )
        reordered = verifier._build_reference_normalized_snapshots(
            RUN_ID,
            authority.records,
            authority.graph,
            authority.games,
            reordered_gate,
        )
        self.assertEqual(
            tuple(snapshot.canonical_bytes for snapshot in reordered),
            tuple(snapshot.canonical_bytes for snapshot in baseline),
        )

        graph_attacks = (
            dataclasses.replace(
                authority.graph, query_batches=(authority.batch,)
            ),
            dataclasses.replace(
                authority.graph,
                query_batches=(
                    authority.static_batch,
                    authority.static_batch,
                    authority.batch,
                ),
            ),
            dataclasses.replace(
                authority.graph,
                query_batches=(
                    dataclasses.replace(
                        authority.static_batch, stage="REMAINING"
                    ),
                    authority.batch,
                ),
            ),
            dataclasses.replace(
                authority.graph,
                query_batches=(
                    dataclasses.replace(
                        authority.static_batch,
                        derived_from_sha256=SCHEDULE_INVENTORY_SHA,
                    ),
                    authority.batch,
                ),
            ),
        )
        for graph in graph_attacks:
            with self.subTest(graph=graph.query_batches), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID,
                    authority.records,
                    graph,
                    authority.games,
                    authority.gate,
                )

        static_id, reference_id = authority.gate.query_batch_ids
        static_hash, reference_hash = authority.gate.query_batch_sha256s
        gate_attacks = (
            dataclasses.replace(
                authority.gate,
                query_batch_ids=(reference_id,),
                query_batch_sha256s=(reference_hash,),
            ),
            dataclasses.replace(
                authority.gate,
                query_batch_ids=(static_id, reference_id, "f" * 64),
                query_batch_sha256s=(static_hash, reference_hash, "e" * 64),
            ),
            dataclasses.replace(
                authority.gate,
                query_batch_ids=(reference_id, static_id),
            ),
            dataclasses.replace(
                authority.gate,
                query_batch_sha256s=(reference_hash, static_hash),
            ),
            dataclasses.replace(
                authority.gate,
                query_batch_sha256s=("0" * 64, reference_hash),
            ),
        )
        for gate in gate_attacks:
            with self.subTest(gate=gate), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID,
                    authority.records,
                    authority.graph,
                    authority.games,
                    gate,
                )

    def test_split_receipt_cannot_intersect_static_or_reference_batch_ids(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        template = SplitReceipt(
            "1" * 64,
            "2" * 64,
            "REMAINING",
            "3" * 64,
            "4" * 64,
            "ROW_CAP",
            1,
            1,
            "5" * 64,
            ("6" * 64, "7" * 64),
            "2026-07-16T12:00:00.000000Z",
        )
        receipt_attacks = (
            dataclasses.replace(
                template, parent_batch_id=authority.static_batch.batch_id
            ),
            dataclasses.replace(
                template, child_batch_id=authority.static_batch.batch_id
            ),
            dataclasses.replace(
                template, parent_batch_id=authority.batch.batch_id
            ),
            dataclasses.replace(
                template, child_batch_id=authority.batch.batch_id
            ),
        )
        for receipt in receipt_attacks:
            graph = dataclasses.replace(
                authority.graph, split_receipts=(receipt,)
            )
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID,
                    authority.records,
                    graph,
                    authority.games,
                    authority.gate,
                )

    def test_malformed_terminal_graph_members_raise_controlled_errors(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        malformed_batch = dataclasses.replace(
            authority.static_batch, queries=(object(),)
        )
        malformed_statcast_root = dataclasses.replace(
            authority.graph.statcast_root_batch, queries=(object(),)
        )
        attacks = (
            dataclasses.replace(
                authority.graph, completed_queries=(object(),)
            ),
            dataclasses.replace(authority.graph, query_batches=(object(),)),
            dataclasses.replace(authority.graph, split_receipts=(object(),)),
            dataclasses.replace(authority.graph, root_queries=(object(),)),
            dataclasses.replace(
                authority.graph, split_parent_queries=(object(),)
            ),
            dataclasses.replace(
                authority.graph, split_leaf_queries=(object(),)
            ),
            dataclasses.replace(
                authority.graph,
                query_batches=(malformed_batch, authority.batch),
            ),
            dataclasses.replace(
                authority.graph, statcast_root_batch=object()
            ),
            dataclasses.replace(
                authority.graph, statcast_root_batch=malformed_statcast_root
            ),
        )
        for graph in attacks:
            with self.subTest(graph=graph), self.assertRaises(
                (TypeError, ValueError)
            ):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID,
                    authority.records,
                    graph,
                    authority.games,
                    authority.gate,
                )

    def test_game_snapshot_population_and_conflicting_flags_fail_closed(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        forged = dataclasses.replace(
            authority.games[0],
            canonical_bytes=authority.games[0].canonical_bytes + b" ",
        )
        game_attacks = (
            authority.games[1:],
            authority.games + (authority.games[0],),
            (forged,) + authority.games[1:],
        )
        for attack in game_attacks:
            with self.subTest(attack=len(attack)), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID, authority.records, authority.graph, attack, authority.gate
                )

        mapping = authority.games[0].as_mapping()
        unplayed = next(game for game in mapping["payload"]["games"] if not game["played"])
        unplayed["venue_id"] = 31
        unplayed["source_game_sha256"] = _source_game_hash(unplayed)
        conflict = _rebuild_snapshot(authority.games[0], mapping)
        with self.assertRaisesRegex(ValueError, "conflicting"):
            verifier._build_reference_normalized_snapshots(
                RUN_ID,
                authority.records,
                authority.graph,
                (conflict,) + authority.games[1:],
                authority.gate,
            )

    def test_timezone_gate_bindings_and_rows_fail_closed(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        bad_mapping = json.loads(canonical_json_bytes(authority.gate.mapping))
        bad_mapping["timezone_rows"][2]["effective_start"] = "2025-01-01"
        attacks = (
            dataclasses.replace(authority.gate, candidate_sha256="0" * 64),
            dataclasses.replace(authority.gate, schedule_inventory_sha256="0" * 64),
            dataclasses.replace(authority.gate, query_batch_ids=("a" * 64, "b" * 64)),
            dataclasses.replace(authority.gate, mapping=bad_mapping),
        )
        for attack in attacks:
            with self.subTest(attack=attack), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID, authority.records, authority.graph, authority.games, attack
                )

        semantic_row_attacks = (
            [
                {
                    "effective_end": "2024-12-31",
                    "effective_start": "2001-01-01",
                    "review_status": "UNREVIEWED",
                    "time_zone": "Not/A_Zone",
                    "venue_id": venue_id,
                }
                for venue_id in (31, 99)
            ],
            [
                {
                    "effective_end": "2024-12-31",
                    "effective_start": "2001-01-01",
                    "review_status": "UNREVIEWED",
                    "time_zone": "America/New_York",
                    "venue_id": 31,
                },
                {
                    "effective_end": "2024-12-31",
                    "effective_start": "2010-01-01",
                    "review_status": "UNREVIEWED",
                    "time_zone": "America/Chicago",
                    "venue_id": 31,
                },
                {
                    "effective_end": "2024-12-31",
                    "effective_start": "2001-01-01",
                    "review_status": "UNREVIEWED",
                    "time_zone": "America/New_York",
                    "venue_id": 99,
                },
            ],
            [
                {
                    "effective_end": "2024-12-31",
                    "effective_start": "2025-01-01",
                    "review_status": "UNREVIEWED",
                    "time_zone": "America/New_York",
                    "venue_id": venue_id,
                }
                for venue_id in (31, 99)
            ],
        )
        for rows in semantic_row_attacks:
            gate = _resign_gate(authority.gate, authority.games, rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                verifier._build_reference_normalized_snapshots(
                    RUN_ID,
                    authority.records,
                    authority.graph,
                    authority.games,
                    gate,
                )

    def test_input_permutations_are_byte_identical(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        baseline = verifier._build_reference_normalized_snapshots(
            RUN_ID, authority.records, authority.graph, authority.games, authority.gate
        )
        permuted_candidate = json.loads(canonical_json_bytes(authority.gate.candidate))
        permuted_candidate["timezone_rows"].reverse()
        candidate_sha = _file_sha(permuted_candidate)
        reviews = []
        for review in authority.gate.reviews:
            item = json.loads(canonical_json_bytes(review))
            item["candidate"]["sha256"] = candidate_sha
            reviews.append(item)
        review_hashes = tuple(_file_sha(item) for item in reviews)
        seal = json.loads(canonical_json_bytes(authority.gate.seal))
        seal["candidate"]["sha256"] = candidate_sha
        for binding, digest in zip(seal["reviews"], review_hashes, strict=True):
            binding["sha256"] = digest
        gate = dataclasses.replace(
            authority.gate,
            candidate=permuted_candidate,
            candidate_sha256=candidate_sha,
            reviews=tuple(reviews),
            review_sha256s=review_hashes,
            seal=seal,
            seal_sha256=_file_sha(seal),
        )
        permuted = verifier._build_reference_normalized_snapshots(
            RUN_ID,
            tuple(reversed(authority.records)),
            authority.graph,
            tuple(reversed(authority.games)),
            gate,
        )
        self.assertEqual(
            tuple(item.canonical_bytes for item in permuted),
            tuple(item.canonical_bytes for item in baseline),
        )

    def test_runtime_and_schema_reject_reference_envelope_attacks(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        snapshot = verifier._build_reference_normalized_snapshots(
            RUN_ID, authority.records, authority.graph, authority.games, authority.gate
        )[0]
        valid = snapshot.as_mapping()
        attacks = []
        for mutate, schema_rejects in (
            (lambda value: value.update(extra=True), True),
            (lambda value: value.update(source="mlb_schedule_game_feed"), True),
            (lambda value: value["split_receipt_sha256s"].append("f" * 64), True),
            (lambda value: value["logical_query_ids"].reverse(), False),
            (
                lambda value: value["payload"]["teams"].append(
                    value["payload"]["teams"][0]
                ),
                True,
            ),
            (lambda value: value["payload"]["timezone_rows"].append(
                {
                    "effective_end": "2000-12-31",
                    "effective_start": "1900-01-01",
                    "time_zone": "America/New_York",
                    "venue_id": 31,
                }
            ), False),
            (
                lambda value: value["payload"][
                    "observed_venue_date_mappings"
                ].reverse(),
                False,
            ),
        ):
            attack = json.loads(canonical_json_bytes(valid))
            mutate(attack)
            attacks.append((attack, schema_rejects))
        schema = json.loads(SCHEMA.read_text())
        for attack, schema_rejects in attacks:
            with self.subTest(runtime=attack), self.assertRaises(ValueError):
                verifier._validate_normalized_snapshot_mapping(attack)
            if schema_rejects:
                with self.subTest(schema=attack), self.assertRaises(
                    jsonschema.ValidationError
                ):
                    jsonschema.validate(attack, schema)

    def test_builder_performs_no_filesystem_or_network_io(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        authority = _authority()
        with (
            patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
            patch.object(Path, "write_bytes", side_effect=AssertionError("write")),
            patch.object(socket, "create_connection", side_effect=AssertionError("network")),
        ):
            snapshots = verifier._build_reference_normalized_snapshots(
                RUN_ID,
                authority.records,
                authority.graph,
                authority.games,
                authority.gate,
            )
        self.assertEqual(len(snapshots), 7)


if __name__ == "__main__":
    unittest.main()
