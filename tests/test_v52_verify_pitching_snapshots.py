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
from mlb_backtest.v52_seasons import SEASON_BOUNDARIES
from mlb_backtest.v52_source_contracts import canonical_json_bytes
from mlb_backtest.v52_sources.pitching import recompute_pitching_sidecar
from mlb_backtest.v52_sources.schedule import canonical_sha256
from tests.test_v52_verify_sources import _normalized_game_authority


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas/v5_2_normalized_snapshot.schema.json"
PITCHING_RAW = ROOT / "tests/fixtures/v5_2_qd/sources/pitching_mini.json"
RUN_ID = "historical_walkforward_500"
SOURCE = "mlb_stats_pitching"
GRAIN = "season_d1_league_pitching_reference"
STATS_ENDPOINT = "https://statsapi.mlb.com/api/v1/stats"
DERIVED_ENDPOINT = "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING"
REASON = "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


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


def _game_snapshots():
    from mlb_backtest import v52_verify_sources as verifier

    authority = _normalized_game_authority()
    originals = verifier._build_game_normalized_snapshots(
        RUN_ID,
        authority.records,
        authority.game_inventory,
        authority.timezone_gate,
        authority.schedule_inventory,
    )
    snapshots = []
    for original in originals:
        season = original.season
        payload = original.as_mapping()["payload"]
        played = next(game for game in payload["games"] if game["played"])
        opening_day = SEASON_BOUNDARIES[season][0]
        played["official_date"] = opening_day.isoformat()
        played["feature_history_cutoff"] = (opening_day - timedelta(days=1)).isoformat()
        played["scheduled_start_utc"] = f"{opening_day.isoformat()}T17:05:00Z"
        played["doubleheader_order"] = 1
        played["source_game_sha256"] = _source_game_hash(played)

        if season == 2024:
            later = opening_day + timedelta(days=10)
            first = dict(played)
            first["game_pk"] = 800_062
            first["official_date"] = later.isoformat()
            first["feature_history_cutoff"] = (later - timedelta(days=1)).isoformat()
            first["scheduled_start_utc"] = f"{later.isoformat()}T17:05:00Z"
            first["boxscore_query_sidecar_sha256"] = _digest("2024-doubleheader-1")
            first["doubleheader_order"] = 1
            first["source_game_sha256"] = _source_game_hash(first)
            second = dict(first)
            second["game_pk"] = 800_063
            second["boxscore_query_sidecar_sha256"] = _digest(
                "2024-doubleheader-2"
            )
            second["doubleheader_order"] = 2
            second["source_game_sha256"] = _source_game_hash(second)
            payload["games"] = [played, first, second]

        sidecars = sorted(
            {
                digest
                for game in payload["games"]
                for digest in (
                    game["schedule_query_sidecar_sha256"],
                    game.get("boxscore_query_sidecar_sha256"),
                )
                if digest is not None
            }
        )
        payload["source_query_sidecar_sha256s"] = sidecars
        query_ids = sorted(
            _digest(f"game-query-{season}-{index}")
            for index in range(len(sidecars))
        )
        envelope = {
            "grain": "season_game_evidence_snapshot",
            "logical_query_ids": query_ids,
            "payload": payload,
            "run_id": RUN_ID,
            "schema_version": 1,
            "season": season,
            "source": "mlb_schedule_game_feed",
            "source_query_sidecar_sha256s": sidecars,
            "split_receipt_sha256s": [],
            "status": "READY",
        }
        verifier._validate_normalized_snapshot_mapping(envelope)
        contents = canonical_json_bytes(envelope) + b"\n"
        snapshots.append(
            verifier.NormalizedSnapshot(
                schema_version=1,
                run_id=RUN_ID,
                season=season,
                source="mlb_schedule_game_feed",
                grain="season_game_evidence_snapshot",
                status="READY",
                logical_query_ids=tuple(query_ids),
                source_query_sidecar_sha256s=tuple(sidecars),
                split_receipt_sha256s=(),
                payload=payload,
                canonical_bytes=contents,
                sha256=hashlib.sha256(contents).hexdigest(),
            )
        )
    return tuple(snapshots)


def _pitching_query(
    season: int,
    cutoff: date,
    consumers: list[int],
) -> LogicalQuery:
    cutoffs = {str(game_pk): cutoff.isoformat() for game_pk in consumers}
    season_open = SEASON_BOUNDARIES[season][0]
    if cutoff < season_open:
        endpoint = DERIVED_ENDPOINT
        parameters: dict[str, object] = {}
        requested = {
            "season": season,
            "as_of_date": cutoff.isoformat(),
            "consumer_cutoff_dates": cutoffs,
            "consuming_game_pks": consumers,
            "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
            "reason": REASON,
        }
    else:
        endpoint = STATS_ENDPOINT
        parameters = {
            "stats": "byDateRange",
            "group": "pitching",
            "sportIds": 1,
            "gameType": "R",
            "playerPool": "ALL",
            "limit": 5000,
            "startDate": season_open.isoformat(),
            "endDate": cutoff.isoformat(),
        }
        requested = {
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
    return LogicalQuery(
        source=SOURCE,
        grain=GRAIN,
        endpoint=endpoint,
        parameters=parameters,
        requested_range=requested,
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _record(query: LogicalQuery):
    from mlb_backtest.v52_source_contracts import recompute_query_sidecar
    from mlb_backtest.v52_verify_sources import VerifiedQueryRecord

    if query.endpoint == DERIVED_ENDPOINT:
        semantics = dict(recompute_query_sidecar(query, b""))
        semantics["reason"] = REASON
        sidecar = {
            "grain": GRAIN,
            "parser_version": "v5.2.1",
            "query_id": logical_query_id(query),
            "schema_version": 1,
            "semantics": semantics,
            "source": SOURCE,
            "storage_mode": "DERIVED_DISPOSITION",
        }
        raw_sha256 = None
        storage_mode = "DERIVED_DISPOSITION"
    else:
        sidecar = recompute_pitching_sidecar(
            PITCHING_RAW.read_bytes(),
            query,
            query.requested_range["consuming_game_pks"],
        )
        raw_sha256 = str(sidecar["raw_sha256"])
        storage_mode = "FETCHED_RAW"
    contents = canonical_json_bytes(sidecar) + b"\n"
    return VerifiedQueryRecord(
        query=query,
        query_id=logical_query_id(query),
        query_sidecar_bytes=contents,
        query_sidecar_sha256=hashlib.sha256(contents).hexdigest(),
        raw_sha256=raw_sha256,
        storage_mode=storage_mode,
        retained_raw_bytes=None,
    )


def _pitching_authority():
    from tests.test_v52_verify_statcast_snapshots import _statcast_authority

    games = _game_snapshots()
    grouped: dict[tuple[int, date], list[int]] = defaultdict(list)
    for snapshot in games:
        for game in snapshot.payload["games"]:
            if game["played"]:
                cutoff = date.fromisoformat(game["official_date"]) - timedelta(days=1)
                grouped[(snapshot.season, cutoff)].append(game["game_pk"])
    queries = tuple(
        _pitching_query(season, cutoff, sorted(consumers))
        for (season, cutoff), consumers in sorted(grouped.items())
    )
    batch = QueryBatch("e" * 64, "REFERENCE_INPUTS", None, queries)
    _roots, base_graph, _records, _game_authority = _statcast_authority()
    graph = dataclasses.replace(
        base_graph,
        query_batches=(batch,),
        split_receipts=(),
        split_parent_queries=(),
        completed_queries=queries,
        root_queries=queries,
        split_leaf_queries=(),
    )
    records = tuple(reversed(tuple(_record(query) for query in queries)))
    return graph, records, games


class V52VerifyPitchingSnapshotsTest(unittest.TestCase):
    def test_builds_seven_season_snapshots_with_exact_d1_consumers(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        graph, records, games = _pitching_authority()
        with patch.object(
            verifier,
            "recompute_pitching_season_snapshot",
            wraps=verifier.recompute_pitching_season_snapshot,
        ) as recompute, patch(
            "pathlib.Path.read_bytes",
            side_effect=AssertionError("builder must not read files"),
        ), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("builder must not use the network"),
        ):
            snapshots = verifier._build_pitching_normalized_snapshots(
                RUN_ID, records, graph, games
            )

        self.assertEqual(recompute.call_count, 7)
        self.assertEqual(tuple(item.season for item in snapshots), tuple(range(2018, 2025)))
        self.assertEqual(len(snapshots), 7)
        schema = json.loads(SCHEMA.read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        for snapshot in snapshots:
            envelope = snapshot.as_mapping()
            jsonschema.validate(envelope, schema)
            self.assertEqual(snapshot.source, SOURCE)
            self.assertEqual(snapshot.grain, GRAIN)
            self.assertEqual(snapshot.status, "READY")
            self.assertEqual(snapshot.split_receipt_sha256s, ())
            self.assertEqual(
                snapshot.logical_query_ids, tuple(sorted(snapshot.logical_query_ids))
            )
            self.assertEqual(
                snapshot.source_query_sidecar_sha256s,
                tuple(sorted(snapshot.source_query_sidecar_sha256s)),
            )
            self.assertEqual(
                snapshot.canonical_bytes, canonical_json_bytes(envelope) + b"\n"
            )
            self.assertEqual(
                snapshot.sha256,
                hashlib.sha256(snapshot.canonical_bytes).hexdigest(),
            )
            rows = snapshot.payload["records"]
            self.assertEqual(
                [row["as_of_date"] for row in rows],
                sorted(row["as_of_date"] for row in rows),
            )
            self.assertEqual(
                list(snapshot.payload["query_sidecar_sha256s"]),
                [row["query_sidecar_sha256"] for row in rows],
            )

        season_2024 = snapshots[-1]
        later_row = season_2024.payload["records"][1]
        self.assertEqual(later_row["consuming_game_pks"], (800_062, 800_063))
        self.assertEqual(
            dict(later_row["consumer_cutoff_dates"]),
            {"800062": later_row["as_of_date"], "800063": later_row["as_of_date"]},
        )
        consumed = {
            game_pk
            for snapshot in snapshots
            for row in snapshot.payload["records"]
            for game_pk in row["consuming_game_pks"]
        }
        expected = {
            game["game_pk"]
            for snapshot in games
            for game in snapshot.payload["games"]
            if game["played"]
        }
        self.assertEqual(consumed, expected)
        self.assertFalse(
            {
                game["game_pk"]
                for snapshot in games
                for game in snapshot.payload["games"]
                if not game["played"]
            }
            & consumed
        )

    def test_input_permutations_are_byte_identical(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        graph, records, games = _pitching_authority()
        original = verifier._build_pitching_normalized_snapshots(
            RUN_ID, records, graph, games
        )
        permuted = verifier._build_pitching_normalized_snapshots(
            RUN_ID,
            tuple(reversed(records)),
            dataclasses.replace(
                graph,
                completed_queries=tuple(reversed(graph.completed_queries)),
                query_batches=tuple(
                    dataclasses.replace(batch, queries=tuple(reversed(batch.queries)))
                    for batch in graph.query_batches
                ),
            ),
            tuple(reversed(games)),
        )
        self.assertEqual(
            tuple(snapshot.canonical_bytes for snapshot in original),
            tuple(snapshot.canonical_bytes for snapshot in permuted),
        )

    def test_derived_opening_day_sidecar_is_closed_and_source_specific(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        graph, records, games = _pitching_authority()
        derived = next(record for record in records if record.storage_mode == "DERIVED_DISPOSITION")
        verifier._build_pitching_normalized_snapshots(RUN_ID, records, graph, games)
        with self.assertRaises(ValueError):
            verifier._verified_sidecar(derived)

        attacks = [
            dataclasses.replace(derived, raw_sha256="0" * 64),
            dataclasses.replace(derived, retained_raw_bytes=b""),
            dataclasses.replace(derived, storage_mode="FETCHED_RAW"),
        ]
        for field, value in (
            ("reason", "WRONG"),
            ("as_of_date", "2018-03-27"),
            ("attempt_history", []),
        ):
            sidecar = json.loads(derived.query_sidecar_bytes)
            if field == "attempt_history":
                sidecar[field] = value
            else:
                sidecar["semantics"][field] = value
            contents = canonical_json_bytes(sidecar) + b"\n"
            attacks.append(
                dataclasses.replace(
                    derived,
                    query_sidecar_bytes=contents,
                    query_sidecar_sha256=hashlib.sha256(contents).hexdigest(),
                )
            )
        attacks.append(dataclasses.replace(derived, query_sidecar_bytes=derived.query_sidecar_bytes[:-1]))
        for index, attack in enumerate(attacks):
            forged = tuple(attack if record is derived else record for record in records)
            with self.subTest(index=index), self.assertRaises(ValueError):
                verifier._build_pitching_normalized_snapshots(RUN_ID, forged, graph, games)

    def test_builder_rejects_terminal_population_and_consumer_attacks(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier
        from tests.test_v52_verify_statcast_snapshots import _statcast_authority

        graph, records, games = _pitching_authority()
        _roots, statcast_graph, _statcast_records, _statcast_games = (
            _statcast_authority()
        )
        unrelated_receipt = statcast_graph.split_receipts[0]
        mixed = _normalized_game_authority().records[0]
        original_query = graph.completed_queries[0]
        pitching_query_id = logical_query_id(original_query)
        distinct = dataclasses.replace(
            original_query,
            requested_range={**original_query.requested_range, "fixture": "extra"},
        )
        mixed_projection = dataclasses.replace(original_query, grain="pitch")
        attacks = [
            (records[:-1], graph),
            ((*records, records[0]), graph),
            ((*records, mixed), graph),
            (records, dataclasses.replace(graph, completed_queries=graph.completed_queries[:-1])),
            (records, dataclasses.replace(graph, split_parent_queries=(graph.completed_queries[0],))),
            (records, dataclasses.replace(graph, split_parent_queries=(distinct,))),
            (records, dataclasses.replace(graph, split_leaf_queries=(distinct,))),
            (records, dataclasses.replace(graph, root_queries=(*graph.root_queries, distinct))),
            (records, dataclasses.replace(graph, root_queries=graph.root_queries[:-1])),
            (records, dataclasses.replace(graph, root_queries=(*graph.root_queries, graph.root_queries[0]))),
            (records, dataclasses.replace(graph, root_queries=(*graph.root_queries, mixed_projection))),
            (records, dataclasses.replace(graph, split_parent_queries=(mixed_projection,))),
            (records, dataclasses.replace(graph, split_leaf_queries=(mixed_projection,))),
            (
                records,
                dataclasses.replace(
                    graph,
                    query_batches=(
                        dataclasses.replace(
                            graph.query_batches[0],
                            queries=(*graph.query_batches[0].queries, mixed_projection),
                        ),
                    ),
                ),
            ),
            (
                records,
                dataclasses.replace(
                    graph,
                    split_receipts=(
                        dataclasses.replace(
                            unrelated_receipt,
                            parent_query_id=pitching_query_id,
                        ),
                    ),
                ),
            ),
            (
                records,
                dataclasses.replace(
                    graph,
                    split_receipts=(
                        dataclasses.replace(
                            unrelated_receipt,
                            child_query_ids=(
                                *unrelated_receipt.child_query_ids,
                                pitching_query_id,
                            ),
                        ),
                    ),
                ),
            ),
        ]
        for index, (bad_records, bad_graph) in enumerate(attacks):
            with self.subTest(population=index), self.assertRaises(ValueError):
                verifier._build_pitching_normalized_snapshots(
                    RUN_ID, bad_records, bad_graph, games
                )

        original = records[0]
        consumers = list(original.query.requested_range["consuming_game_pks"])
        requested_attacks = []
        missing = dict(original.query.requested_range)
        missing["consuming_game_pks"] = consumers[:-1]
        missing["consumer_cutoff_dates"] = {
            str(game_pk): missing["as_of_date"] for game_pk in consumers[:-1]
        }
        requested_attacks.append(missing)
        duplicate = dict(original.query.requested_range)
        duplicate["consuming_game_pks"] = [*consumers, consumers[0]]
        requested_attacks.append(duplicate)
        unknown = dict(original.query.requested_range)
        unknown["consuming_game_pks"] = [999_999]
        unknown["consumer_cutoff_dates"] = {"999999": unknown["as_of_date"]}
        requested_attacks.append(unknown)
        wrong_cutoff = dict(original.query.requested_range)
        wrong_cutoff["as_of_date"] = str(date.fromisoformat(wrong_cutoff["as_of_date"]) - timedelta(days=1))
        wrong_cutoff["consumer_cutoff_dates"] = {
            str(game_pk): wrong_cutoff["as_of_date"] for game_pk in consumers
        }
        requested_attacks.append(wrong_cutoff)
        cross_season = dict(original.query.requested_range)
        cross_season["season"] = 2024 if cross_season["season"] != 2024 else 2023
        requested_attacks.append(cross_season)
        for index, requested in enumerate(requested_attacks):
            forged_query = dataclasses.replace(original.query, requested_range=requested)
            try:
                forged_record = _record(forged_query)
            except ValueError:
                forged_record = dataclasses.replace(original, query=forged_query, query_id=logical_query_id(forged_query))
            forged_records = tuple(
                forged_record if record is original else record for record in records
            )
            forged_graph = dataclasses.replace(
                graph,
                completed_queries=tuple(
                    forged_query if query == original.query else query
                    for query in graph.completed_queries
                ),
                root_queries=tuple(
                    forged_query if query == original.query else query
                    for query in graph.root_queries
                ),
                query_batches=tuple(
                    dataclasses.replace(
                        batch,
                        queries=tuple(
                            forged_query if query == original.query else query
                            for query in batch.queries
                        ),
                    )
                    for batch in graph.query_batches
                ),
            )
            with self.subTest(consumer=index), self.assertRaises(ValueError):
                verifier._build_pitching_normalized_snapshots(
                    RUN_ID, forged_records, forged_graph, games
                )

    def test_runtime_and_schema_fail_closed_on_pitching_attacks(self) -> None:
        from mlb_backtest import v52_verify_sources as verifier

        graph, records, games = _pitching_authority()
        snapshot = verifier._build_pitching_normalized_snapshots(
            RUN_ID, records, graph, games
        )[-1]
        attacks = []
        forged = snapshot.as_mapping()
        forged["unexpected"] = True
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["season"] = 2023
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["split_receipt_sha256s"] = ["0" * 64]
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["source_query_sidecar_sha256s"] = list(
            reversed(forged["source_query_sidecar_sha256s"])
        )
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["query_sidecar_sha256s"] = list(
            reversed(forged["payload"]["query_sidecar_sha256s"])
        )
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["records"][0]["consuming_game_pks"].append(999_999)
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["unexpected"] = True
        attacks.append(forged)
        forged = snapshot.as_mapping()
        self.assertIn("disposition", forged["payload"]["records"][0])
        forged["payload"]["records"][0]["earned_runs"] = 0
        attacks.append(forged)
        forged = snapshot.as_mapping()
        self.assertIn("earned_runs", forged["payload"]["records"][1])
        forged["payload"]["records"][1]["disposition"] = (
            "PRESEASON_NO_CURRENT_SEASON_PITCHING"
        )
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["query_sidecar_sha256s"][0] = {"digest": "bad"}
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["records"][0]["consuming_game_pks"].append({})
        attacks.append(forged)
        forged = snapshot.as_mapping()
        forged["payload"]["records"][0]["consuming_game_pks"].append("bad")
        attacks.append(forged)
        for index, attack in enumerate(attacks):
            with self.subTest(index=index), self.assertRaises(ValueError):
                verifier._validate_normalized_snapshot_mapping(attack)

        schema = json.loads(SCHEMA.read_bytes())
        for index in (0, 2, 6, 7, 8):
            attack = attacks[index]
            with self.subTest(schema=index), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(attack, schema)


if __name__ == "__main__":
    unittest.main()
