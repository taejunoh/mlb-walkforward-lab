from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id
from mlb_backtest.v52_query_plan import QueryBatch
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from mlb_backtest.v52_sources.pitching import recompute_career_bf_sidecar
from tests.test_v52_verify_statcast_snapshots import _statcast_authority


ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "historical_walkforward_500"
SCHEMA = ROOT / "schemas/v5_2_normalized_snapshot.schema.json"
SOURCE = "mlb_stats_pitcher_bf"
GRAIN = "player_career_batters_faced_reference"


def _career_query(
    pitcher_id: int,
    inclusive_end: str,
    consumers: dict[int, date],
) -> LogicalQuery:
    return LogicalQuery(
        source=SOURCE,
        grain=GRAIN,
        endpoint=f"https://statsapi.mlb.com/api/v1/people/{pitcher_id}/stats",
        parameters={
            "stats": "byDateRange",
            "group": "pitching",
            "gameType": "R",
            "sportIds": 1,
            "startDate": "1876-01-01",
            "endDate": inclusive_end,
        },
        requested_range={
            "consumer_cutoff_dates": {
                str(game_pk): consumers[game_pk].isoformat()
                for game_pk in sorted(consumers)
            },
            "consuming_game_pks": sorted(consumers),
            "pitcher_id": pitcher_id,
            "inclusive_end": inclusive_end,
        },
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _career_record(query: LogicalQuery, baseline: int = 0):
    from mlb_backtest.v52_verify_sources import VerifiedQueryRecord

    raw = json.dumps(
        {
            "stats": [
                {
                    "type": {"displayName": "byDateRange"},
                    "group": {"displayName": "pitching"},
                    "splits": [
                        {
                            "stat": {"battersFaced": baseline},
                            "sport": {"id": 1},
                            "team": {"id": 10},
                        },
                        {"stat": {"battersFaced": baseline}, "sport": {"id": 0}},
                    ],
                }
            ]
        },
        separators=(",", ":"),
    ).encode()
    sidecar = recompute_career_bf_sidecar(
        raw,
        query,
        query.requested_range["consuming_game_pks"],
    )
    contents = canonical_json_bytes(sidecar) + b"\n"
    return VerifiedQueryRecord(
        query=query,
        query_id=logical_query_id(query),
        query_sidecar_bytes=contents,
        query_sidecar_sha256=hashlib.sha256(contents).hexdigest(),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        storage_mode="FETCHED_RAW",
        retained_raw_bytes=None,
    )


def _career_authority():
    roots, statcast_graph, statcast_records, games = _statcast_authority()
    consumers: dict[tuple[int, str], dict[int, date]] = defaultdict(dict)
    for snapshot in games:
        for game in snapshot.payload["games"]:
            if not game["played"]:
                continue
            cutoff = date.fromisoformat(game["official_date"]) - timedelta(days=1)
            inclusive_end = (
                "2017-12-31" if cutoff <= date(2021, 12, 31) else "2021-12-31"
            )
            for pitcher_id in game["actual_starter_ids"]:
                consumers[(pitcher_id, inclusive_end)][game["game_pk"]] = cutoff
    queries = tuple(
        _career_query(pitcher_id, inclusive_end, grouped)
        for (pitcher_id, inclusive_end), grouped in sorted(consumers.items())
    )
    career_batch = QueryBatch("6" * 64, "PARTICIPANT_DERIVED", "d" * 64, queries)
    graph = dataclasses.replace(
        statcast_graph,
        query_batches=(*statcast_graph.query_batches, career_batch),
        completed_queries=(*statcast_graph.completed_queries, *queries),
        root_queries=(*statcast_graph.root_queries, *queries),
    )
    baselines = tuple(
        _career_record(
            query,
            baseline=10
            if query.requested_range["inclusive_end"] == "2017-12-31"
            else 20,
        )
        for query in queries
    )
    return roots, graph, (*statcast_records, *reversed(baselines)), games, baselines


class V52VerifyCareerSnapshotsTest(unittest.TestCase):
    def test_global_recompute_is_publicly_exported(self) -> None:
        from mlb_backtest import v52_sources

        self.assertTrue(callable(v52_sources.recompute_career_bf_snapshot))

    def test_many_consumers_share_precomputed_root_coverage_and_cutoff_lineage(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.v52_sources import pitching

        _roots, graph, records, games, _baselines = _career_authority()
        statcast_record_count = sum(
            record.query.source == "baseball_savant_statcast" for record in records
        )
        unique_cutoffs = {
            date.fromisoformat(game["official_date"]) - timedelta(days=1)
            for snapshot in games
            for game in snapshot.payload["games"]
            if game["played"]
        }
        with patch.object(
            pitching,
            "_validate_statcast_roots",
            wraps=pitching._validate_statcast_roots,
        ) as validate_roots, patch.object(
            pitching,
            "_validate_career_statcast_coverage",
            wraps=pitching._validate_career_statcast_coverage,
        ) as legacy_coverage, patch.object(
            pitching,
            "_monthly_root_query",
            wraps=pitching._monthly_root_query,
        ) as rebuild_root, patch.object(
            pitching,
            "_career_lineage_for_cutoff",
            wraps=pitching._career_lineage_for_cutoff,
        ) as build_lineage:
            snapshots = verifier._build_career_bf_normalized_snapshots(
                RUN_ID, records, graph, games
            )

        self.assertEqual(len(snapshots), 7)
        self.assertEqual(validate_roots.call_count, 1)
        self.assertEqual(legacy_coverage.call_count, 0)
        self.assertLessEqual(rebuild_root.call_count, statcast_record_count)
        self.assertEqual(build_lineage.call_count, len(unique_cutoffs))

    def test_builds_seven_sealed_snapshots_with_global_single_scan(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from mlb_backtest.v52_sources import pitching

        roots, graph, records, games, baselines = _career_authority()
        statcast_leaf_count = sum(
            record.query.source == "baseball_savant_statcast" for record in records
        )
        statcast_hash_calls: list[str] = []
        real_hash = pitching._artifact_sha256

        def count_hash(sidecar):
            if sidecar.get("source") == "baseball_savant_statcast":
                statcast_hash_calls.append(str(sidecar["query_id"]))
            return real_hash(sidecar)

        with (
            patch.object(
                pitching,
                "_scan_ready_statcast_leaf",
                wraps=pitching._scan_ready_statcast_leaf,
            ) as scan,
            patch.object(pitching, "_artifact_sha256", side_effect=count_hash),
            patch.object(
                verifier,
                "recompute_career_bf_snapshot",
                wraps=verifier.recompute_career_bf_snapshot,
            ) as recompute,
            patch(
                "pathlib.Path.read_bytes",
                side_effect=AssertionError("builder must not read files"),
            ),
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("builder must not use the network"),
            ),
        ):
            snapshots = verifier._build_career_bf_normalized_snapshots(
                RUN_ID, records, graph, games
            )

        self.assertEqual(recompute.call_count, 1)
        self.assertEqual(scan.call_count, statcast_leaf_count)
        self.assertEqual(len(statcast_hash_calls), statcast_leaf_count)
        self.assertEqual(len(set(statcast_hash_calls)), statcast_leaf_count)
        self.assertEqual(
            tuple(snapshot.season for snapshot in snapshots), tuple(range(2018, 2025))
        )
        self.assertEqual(len(snapshots), 7)
        schema = json.loads(SCHEMA.read_text())
        jsonschema.Draft202012Validator.check_schema(schema)
        for snapshot in snapshots:
            envelope = snapshot.as_mapping()
            jsonschema.validate(envelope, schema)
            self.assertEqual(snapshot.source, SOURCE)
            self.assertEqual(snapshot.grain, GRAIN)
            self.assertEqual(
                snapshot.canonical_bytes, canonical_json_bytes(envelope) + b"\n"
            )
            self.assertEqual(
                snapshot.sha256, hashlib.sha256(snapshot.canonical_bytes).hexdigest()
            )
            self.assertEqual(len(snapshot.payload["records"]), 2)
            self.assertEqual(
                set(snapshot.source_query_sidecar_sha256s),
                {
                    digest
                    for row in snapshot.payload["records"]
                    for digest in (
                        row["baseline_query_sidecar_sha256"],
                        *row["statcast_ready_leaf_sidecar_sha256s"],
                    )
                },
            )
            self.assertEqual(
                set(snapshot.split_receipt_sha256s),
                {
                    digest
                    for row in snapshot.payload["records"]
                    for digest in row["statcast_split_receipt_sha256s"]
                },
            )

        expected_verified = {
            2018: 10,
            2019: 11,
            2020: 12,
            2021: 13,
            2022: 20,
            2023: 21,
            2024: 22,
        }
        for snapshot in snapshots:
            self.assertEqual(
                {row["total_batters_faced"] for row in snapshot.payload["records"]},
                {expected_verified[snapshot.season]},
            )
        self.assertEqual(len(baselines), 4)

        first = snapshots[0].payload["records"][0]
        # The April leaf contains the D-1 boundary even though its response also
        # contains later dates; it is lineage, but same-day/future PA is excluded.
        self.assertEqual(first["verified_statcast_bf"], 0)
        april_2018_hash = next(
            record.query_sidecar_sha256
            for record in records
            if record.query.source == "baseball_savant_statcast"
            and record.query.requested_range["start_date"] == "2018-04-01"
        )
        self.assertIn(april_2018_hash, first["statcast_ready_leaf_sidecar_sha256s"])

        final = snapshots[-1].payload["records"][0]
        receipt_by_parent = {
            receipt.parent_query_id: receipt.receipt_sha256
            for receipt in graph.split_receipts
        }
        self.assertEqual(
            final["statcast_split_receipt_sha256s"],
            tuple(sorted(set(receipt_by_parent.values()))),
        )
        future_split_sibling_hashes = {
            record.query_sidecar_sha256
            for record in records
            if record.query.source == "baseball_savant_statcast"
            and record.query.requested_range["year"] == 2024
            and record.query.requested_range["start_date"] > "2024-04-01"
            and record.query.requested_range["start_date"] < "2024-05-01"
        }
        self.assertFalse(
            future_split_sibling_hashes
            & set(final["statcast_ready_leaf_sidecar_sha256s"])
        )
        # No May-or-later 2024 source can enter an April 1 consumer snapshot.
        future_hashes = {
            record.query_sidecar_sha256
            for record in records
            if record.query.source == "baseball_savant_statcast"
            and record.query.requested_range["year"] == 2024
            and record.query.requested_range["start_date"] >= "2024-05-01"
        }
        self.assertFalse(
            future_hashes & set(final["statcast_ready_leaf_sidecar_sha256s"])
        )

    def test_legacy_wrapper_delegates_once_with_single_leaf_scan_and_global_parity(
        self,
    ) -> None:
        from mlb_backtest.v52_sources import pitching
        from tests.test_v52_source_pitching import (
            _career_addition,
            _career_baseline,
            _planned_statcast_roots,
            _planned_statcast_sidecars,
        )

        cutoff = "2024-09-30"
        baseline = _career_baseline("2021-12-31", cutoff)
        all_leaves = _planned_statcast_sidecars(cutoff)
        all_roots = _planned_statcast_roots(cutoff)
        leaves = _planned_statcast_sidecars(cutoff, start_year=2022)
        roots = [query for query in all_roots if query.requested_range["year"] >= 2022]
        addition = _career_addition(leaves, cutoff)
        addition["statcast_ready_leaf_sidecar_sha256s"] = sorted(
            addition["statcast_ready_leaf_sidecar_sha256s"]
        )
        with patch.object(
            pitching,
            "_build_career_statcast_index",
            wraps=pitching._build_career_statcast_index,
        ) as build_index, patch.object(
            pitching,
            "_scan_ready_statcast_leaf",
            wraps=pitching._scan_ready_statcast_leaf,
        ) as scan:
            wrapped = pitching.recompute_career_bf_season_snapshot(
                [baseline],
                [addition],
                statcast_root_queries=roots,
                statcast_split_receipts=[],
            )
        direct = pitching.recompute_career_bf_snapshot(
            [baseline],
            statcast_ready_leaf_sidecars=all_leaves,
            statcast_root_queries=all_roots,
            statcast_split_receipts=[],
        )
        self.assertEqual(build_index.call_count, 1)
        self.assertEqual(scan.call_count, len(leaves))
        self.assertEqual(wrapped, direct)

        forged = dict(addition)
        forged["statcast_bf"] = 2
        with self.assertRaises(ValueError):
            pitching.recompute_career_bf_season_snapshot(
                [baseline],
                [forged],
                statcast_root_queries=roots,
                statcast_split_receipts=[],
            )

    def test_legacy_wrapper_uses_full_recursive_supply_but_claims_cutoff_subset(
        self,
    ) -> None:
        from mlb_backtest.v52_sources import pitching
        from tests.test_v52_source_pitching import (
            _artifact_sha,
            _career_baseline,
            _planned_statcast_query,
            _recursive_bodyless_timeout_inputs,
            _statcast_source_sidecar,
        )

        cutoff = "2024-04-01"
        baseline = _career_baseline("2021-12-31", cutoff)
        roots, april_supply, receipts, _queries = (
            _recursive_bodyless_timeout_inputs(include_target_pa=True)
        )
        roots = [query for query in roots if query.requested_range["year"] >= 2022]
        may_root = _planned_statcast_query("2024-05-01", "2024-05-31")
        may_supply = _statcast_source_sidecar(
            "2024-05-01",
            "2024-05-31",
            include_target_pa=False,
            query=may_root,
        )
        supply = [
            sidecar
            for sidecar in april_supply
            if sidecar["semantics"]["coverage"]["inclusive_start"] >= "2022-01-01"
        ] + [may_supply]
        relevant_hashes = sorted(
            _artifact_sha(sidecar)
            for sidecar in supply
            if sidecar["semantics"]["coverage"]["inclusive_start"] <= cutoff
        )
        addition = {
            "consumer_cutoff_date": cutoff,
            "game_pk": 9001,
            "pitcher_id": 101,
            "statcast_bf": 0,
            "statcast_ready_leaf_sidecars": supply,
            "statcast_ready_leaf_sidecar_sha256s": relevant_hashes,
        }
        snapshot = pitching.recompute_career_bf_season_snapshot(
            [baseline],
            [addition],
            statcast_root_queries=[*roots, may_root],
            statcast_split_receipts=receipts,
        )
        record = snapshot["records"][0]
        self.assertEqual(record["statcast_ready_leaf_sidecar_sha256s"], relevant_hashes)
        future_hashes = {
            _artifact_sha(sidecar)
            for sidecar in supply
            if sidecar["semantics"]["coverage"]["inclusive_start"] > cutoff
        }
        self.assertFalse(
            future_hashes & set(record["statcast_ready_leaf_sidecar_sha256s"])
        )

    def test_global_api_rejects_wrong_baseline_partition_for_consumer_cutoff(
        self,
    ) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_career_bf_snapshot

        roots, graph, records, _games, baselines = _career_authority()
        sidecars = {
            record.query_id: json.loads(record.query_sidecar_bytes)
            for record in records
        }
        original = next(
            record
            for record in baselines
            if record.query.requested_range["inclusive_end"] == "2021-12-31"
        )
        forged_query = dataclasses.replace(
            original.query,
            parameters={**original.query.parameters, "endDate": "2017-12-31"},
            requested_range={
                **original.query.requested_range,
                "inclusive_end": "2017-12-31",
            },
        )
        forged = _career_record(forged_query, baseline=10)
        baseline_sidecars = [
            json.loads((forged if record is original else record).query_sidecar_bytes)
            for record in baselines
        ]
        statcast_sidecars = [
            sidecars[record.query_id]
            for record in records
            if record.query.source == "baseball_savant_statcast"
        ]
        with self.assertRaises(ValueError):
            recompute_career_bf_snapshot(
                baseline_sidecars,
                statcast_ready_leaf_sidecars=statcast_sidecars,
                statcast_root_queries=roots.queries,
                statcast_split_receipts=graph.split_receipts,
            )

        pre_2022 = json.loads(original.query_sidecar_bytes)
        pre_2022["semantics"]["consumer_cutoff_dates"] = {
            key: "2021-04-01"
            for key in pre_2022["semantics"]["consumer_cutoff_dates"]
        }
        with self.assertRaises(ValueError):
            recompute_career_bf_snapshot(
                [pre_2022],
                statcast_ready_leaf_sidecars=statcast_sidecars,
                statcast_root_queries=roots.queries,
                statcast_split_receipts=graph.split_receipts,
            )

    def test_builder_documents_externally_sealed_record_trust_boundary(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        self.assertIn(
            "externally sealed",
            verifier._build_career_bf_normalized_snapshots.__doc__ or "",
        )

        _roots, graph, records, games, baselines = _career_authority()
        original = baselines[0]
        forged_sidecar = json.loads(original.query_sidecar_bytes)
        forged_sidecar["semantics"]["baseline_batters_faced"] += 1
        forged_bytes = canonical_json_bytes(forged_sidecar) + b"\n"
        resigned = dataclasses.replace(
            original,
            query_sidecar_bytes=forged_bytes,
            query_sidecar_sha256=hashlib.sha256(forged_bytes).hexdigest(),
        )
        forged_records = tuple(
            resigned if record is original else record for record in records
        )
        snapshots = verifier._build_career_bf_normalized_snapshots(
            RUN_ID, forged_records, graph, games
        )
        affected = next(
            row
            for snapshot in snapshots
            for row in snapshot.payload["records"]
            if row["baseline_query_sidecar_sha256"]
            == resigned.query_sidecar_sha256
        )
        self.assertEqual(
            affected["baseline_batters_faced"],
            forged_sidecar["semantics"]["baseline_batters_faced"],
        )

    def test_input_permutations_are_byte_identical(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        _roots, graph, records, games, _baselines = _career_authority()
        original = verifier._build_career_bf_normalized_snapshots(
            RUN_ID, records, graph, games
        )
        permuted = verifier._build_career_bf_normalized_snapshots(
            RUN_ID,
            tuple(reversed(records)),
            dataclasses.replace(
                graph,
                completed_queries=tuple(reversed(graph.completed_queries)),
                root_queries=tuple(reversed(graph.root_queries)),
                split_receipts=tuple(reversed(graph.split_receipts)),
                query_batches=tuple(
                    dataclasses.replace(batch, queries=tuple(reversed(batch.queries)))
                    for batch in reversed(graph.query_batches)
                ),
            ),
            tuple(reversed(games)),
        )
        self.assertEqual(
            tuple(snapshot.canonical_bytes for snapshot in original),
            tuple(snapshot.canonical_bytes for snapshot in permuted),
        )

    def test_rejects_population_lineage_and_game_attacks(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        roots, graph, records, games, baselines = _career_authority()
        statcast = tuple(
            record
            for record in records
            if record.query.source == "baseball_savant_statcast"
        )
        attacks = [
            (records[:-1], graph, games),
            ((*records, records[0]), graph, games),
            (
                tuple(record for record in records if record is not statcast[0]),
                graph,
                games,
            ),
            (records, dataclasses.replace(graph, split_receipts=()), games),
            (
                records,
                dataclasses.replace(
                    graph,
                    statcast_root_batch=dataclasses.replace(
                        roots, queries=roots.queries[:-1]
                    ),
                ),
                games,
            ),
            (records, graph, games[:-1]),
            (
                records,
                dataclasses.replace(
                    graph, split_leaf_queries=graph.split_leaf_queries[:-1]
                ),
                games,
            ),
            (
                records,
                dataclasses.replace(
                    graph,
                    split_parent_queries=(
                        *graph.split_parent_queries,
                        graph.completed_queries[0],
                    ),
                ),
                games,
            ),
            (
                records,
                dataclasses.replace(
                    graph,
                    query_batches=(
                        *graph.query_batches[:-2],
                        graph.query_batches[-1],
                    ),
                ),
                games,
            ),
        ]
        for index, (bad_records, bad_graph, bad_games) in enumerate(attacks):
            with self.subTest(index=index), self.assertRaises(ValueError):
                verifier._build_career_bf_normalized_snapshots(
                    RUN_ID, bad_records, bad_graph, bad_games
                )

        original = baselines[0]
        forged_sidecar = json.loads(original.query_sidecar_bytes)
        forged_sidecar["semantics"]["consuming_game_pks"] = forged_sidecar["semantics"][
            "consuming_game_pks"
        ][:-1]
        forged_bytes = canonical_json_bytes(forged_sidecar) + b"\n"
        forged_record = dataclasses.replace(
            original,
            query_sidecar_bytes=forged_bytes,
            query_sidecar_sha256=hashlib.sha256(forged_bytes).hexdigest(),
        )
        forged_records = tuple(
            forged_record if record is original else record for record in records
        )
        with self.assertRaises(ValueError):
            verifier._build_career_bf_normalized_snapshots(
                RUN_ID, forged_records, graph, games
            )

        game = games[0]
        mapping = game.as_mapping()
        played = next(row for row in mapping["payload"]["games"] if row["played"])
        played["actual_starter_ids"] = [101, 101]
        contents = canonical_json_bytes(mapping) + b"\n"
        forged_game = dataclasses.replace(
            game,
            payload=mapping["payload"],
            canonical_bytes=contents,
            sha256=hashlib.sha256(contents).hexdigest(),
        )
        with self.assertRaises(ValueError):
            verifier._build_career_bf_normalized_snapshots(
                RUN_ID,
                records,
                graph,
                (forged_game, *games[1:]),
            )

    def test_runtime_and_schema_fail_closed(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        _roots, graph, records, games, _baselines = _career_authority()
        snapshot = verifier._build_career_bf_normalized_snapshots(
            RUN_ID, records, graph, games
        )[-1]
        attacks = []
        forged = snapshot.as_mapping()
        forged["unexpected"] = True
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["source"] = "mlb_stats_pitching"
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["unexpected"] = True
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["records"][0]["total_batters_faced"] += 1
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["records"][0]["statcast_ready_leaf_sidecar_sha256s"].append(
            "0" * 64
        )
        attacks.append(forged)
        forged = snapshot.as_mapping()
        leaf_hashes = forged["payload"]["records"][0][
            "statcast_ready_leaf_sidecar_sha256s"
        ]
        self.assertGreater(len(leaf_hashes), 1)
        forged["payload"]["records"][0][
            "statcast_ready_leaf_sidecar_sha256s"
        ] = list(reversed(leaf_hashes))
        attacks.append(forged)
        forged = snapshot.as_mapping()
        receipt_hashes = forged["payload"]["records"][0][
            "statcast_split_receipt_sha256s"
        ]
        self.assertGreater(len(receipt_hashes), 1)
        forged["payload"]["records"][0][
            "statcast_split_receipt_sha256s"
        ] = list(reversed(receipt_hashes))
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["source_query_sidecar_sha256s"] = forged["source_query_sidecar_sha256s"][
            :-1
        ]
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["records"] = list(reversed(forged["payload"]["records"]))
        attacks.append(forged)
        for index, attack in enumerate(attacks):
            with self.subTest(runtime=index), self.assertRaises(ValueError):
                verifier._validate_normalized_snapshot_mapping(attack)

        schema = json.loads(SCHEMA.read_text())
        for index in (0, 1, 2):
            with (
                self.subTest(schema=index),
                self.assertRaises(jsonschema.ValidationError),
            ):
                jsonschema.validate(attacks[index], schema)

    def test_career_builder_rejects_malformed_graph_members_without_attribute_error(
        self,
    ) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        _roots, graph, records, games, _baselines = _career_authority()
        malformed_batch = dataclasses.replace(
            graph.query_batches[0], queries=(object(),)
        )
        attacks = {
            "completed query": dataclasses.replace(
                graph, completed_queries=(object(),)
            ),
            "root batch": dataclasses.replace(graph, statcast_root_batch=object()),
            "query batch": dataclasses.replace(graph, query_batches=(object(),)),
            "batch query": dataclasses.replace(
                graph, query_batches=(malformed_batch,)
            ),
            "receipt": dataclasses.replace(graph, split_receipts=(object(),)),
        }
        for label, malformed in attacks.items():
            with self.subTest(label=label), self.assertRaises((TypeError, ValueError)):
                verifier._build_career_bf_normalized_snapshots(
                    RUN_ID, records, malformed, games
                )


if __name__ == "__main__":
    unittest.main()
