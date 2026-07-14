from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from mlb_backtest.v52_evidence import (
    LogicalQuery,
    canonical_query_bytes,
    logical_query_id,
)
from mlb_backtest.statcast import statcast_url


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"
RUN_SHA = hashlib.sha256(RUN_SPEC.read_bytes()).hexdigest()
CONTRACT_SHA = hashlib.sha256(CONTRACT.read_bytes()).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _write_forged_canonical_batch(
    root: Path,
    *,
    stage: str,
    derived_from_sha256: str | None,
    queries: tuple[LogicalQuery, ...],
) -> str:
    payload = {
        "stage": stage,
        "derived_from_sha256": derived_from_sha256,
        "queries": [json.loads(canonical_query_bytes(query)) for query in queries],
    }
    batch_id = hashlib.sha256(
        _canonical_json_bytes(payload).removesuffix(b"\n")
    ).hexdigest()
    record = {"schema_version": 1, "batch_id": batch_id, **payload}
    batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
    batch_dir.mkdir(parents=True)
    (batch_dir / f"{batch_id}.json").write_bytes(_canonical_json_bytes(record))
    return batch_id


def _parent(start: str, end: str) -> LogicalQuery:
    query = LogicalQuery(
        source="baseball_savant_statcast",
        grain="pitch",
        endpoint="https://baseballsavant.mlb.com/statcast_search/csv",
        parameters={
            "all": "true",
            "type": "details",
            "player_type": "batter",
            "game_date_gt": start,
            "game_date_lt": end,
            "hfGT": "R|",
            "group_by": "name-year",
            "sort_col": "pitches",
            "player_event_sort": "api_p_release_speed",
            "sort_order": "desc",
            "min_pitches": "0",
        },
        requested_range={
            "year": int(start[:4]),
            "start_date": start,
            "end_date": end,
        },
        expected_media_type="text/csv",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256=RUN_SHA,
        acquisition_contract_sha256=CONTRACT_SHA,
    )
    return query


def _legacy(start: str, end: str, **extra: object) -> dict[str, object]:
    return {
        "source": "baseball_savant_statcast",
        "rawstore_verified": True,
        "canonical_query": {
            "start": start,
            "end": end,
            "game_type": "R",
            "url": statcast_url(start, end),
        },
        **extra,
    }


class V52QueryPlanTest(unittest.TestCase):
    def test_static_batch_covers_2018_through_2024_only_and_excludes_2025(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(len(batch.queries), 7)
        self.assertEqual(
            {query.source for query in batch.queries},
            {"mlb_schedule_game_feed"},
        )
        years = {
            int(str(query.requested_range["start_date"])[:4]) for query in batch.queries
        }
        self.assertEqual(years, set(range(2018, 2025)))
        self.assertFalse(
            any("2025" in canonical_query_bytes(q).decode() for q in batch.queries)
        )
        self.assertEqual(batch.stage, "STATIC_SCHEDULES")

    def test_statcast_root_batch_uses_monthly_official_parents_only(self):
        from mlb_backtest.v52_query_plan import build_statcast_root_batch

        batch = build_statcast_root_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(batch.stage, "REMAINING")
        self.assertIsNone(batch.derived_from_sha256)
        self.assertEqual(len(batch.queries), 46)
        self.assertEqual(
            {query.source for query in batch.queries},
            {"baseball_savant_statcast"},
        )
        self.assertEqual(
            {query.endpoint for query in batch.queries},
            {"https://baseballsavant.mlb.com/statcast_search/csv"},
        )
        for query in batch.queries:
            first = date.fromisoformat(str(query.requested_range["start_date"]))
            last = date.fromisoformat(str(query.requested_range["end_date"]))
            self.assertEqual(first.month, last.month)
            self.assertIsNone(query.parent_query_id)
            self.assertEqual(query.parameters["hfGT"], "R|")

    def test_static_and_statcast_root_batches_are_a_lossless_deterministic_split(self):
        from mlb_backtest.v52_query_plan import (
            build_statcast_root_batch,
            build_static_batch,
        )

        static = build_static_batch(RUN_SPEC, CONTRACT)
        statcast = build_statcast_root_batch(RUN_SPEC, CONTRACT)
        self.assertNotEqual(static.batch_id, statcast.batch_id)
        self.assertEqual(static, build_static_batch(RUN_SPEC, CONTRACT))
        self.assertEqual(statcast, build_statcast_root_batch(RUN_SPEC, CONTRACT))
        self.assertEqual(static.stage, "STATIC_SCHEDULES")
        self.assertEqual(statcast.stage, "REMAINING")

        static_ids = {logical_query_id(query) for query in static.queries}
        statcast_ids = {logical_query_id(query) for query in statcast.queries}
        self.assertTrue(static_ids.isdisjoint(statcast_ids))
        combined_ids = sorted(static_ids | statcast_ids)
        self.assertEqual(len(combined_ids), 53)
        population_digest = hashlib.sha256(
            ("\n".join(combined_ids) + "\n").encode()
        ).hexdigest()
        self.assertEqual(
            population_digest,
            "1b1564e1fea24924d1a434443f97f19cfbee3452d5ec1b5b1b397c63fb775b4d",
        )
        for query in (*static.queries, *statcast.queries):
            self.assertEqual(query.run_spec_sha256, RUN_SHA)
            self.assertEqual(query.acquisition_contract_sha256, CONTRACT_SHA)
            reconstructed = LogicalQuery(**json.loads(canonical_query_bytes(query)))
            self.assertEqual(reconstructed, query)

    def test_root_batches_publish_and_load_independently(self):
        from mlb_backtest.v52_query_plan import (
            build_statcast_root_batch,
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )

        batches = (
            build_static_batch(RUN_SPEC, CONTRACT),
            build_statcast_root_batch(RUN_SPEC, CONTRACT),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            paths = [publish_query_batch(root, batch) for batch in batches]
            self.assertEqual(len(set(paths)), 2)
            self.assertEqual(
                [load_query_batch(root, batch.batch_id) for batch in batches],
                list(batches),
            )

    def test_query_batch_construction_rejects_unknown_stage_with_any_population(self):
        from mlb_backtest.v52_query_plan import _make_batch, build_static_batch

        query = build_static_batch(RUN_SPEC, CONTRACT).queries[0]
        for queries in ((), (query,)):
            with self.subTest(query_count=len(queries)), self.assertRaisesRegex(
                ValueError, "stage"
            ):
                _make_batch("TYPO_STAGE", None, queries)

    def test_load_recomputation_rejects_unknown_stage_with_any_population(self):
        from mlb_backtest.v52_query_plan import build_static_batch, load_query_batch

        query = build_static_batch(RUN_SPEC, CONTRACT).queries[0]
        for queries in ((), (query,)):
            with (
                self.subTest(query_count=len(queries)),
                tempfile.TemporaryDirectory() as td,
            ):
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="TYPO_STAGE",
                    derived_from_sha256=None,
                    queries=queries,
                )
                with self.assertRaisesRegex(ValueError, "stage"):
                    load_query_batch(root, batch_id)

    def test_load_recomputation_rejects_semantically_forged_static_root_batches(self):
        from mlb_backtest.v52_query_plan import build_static_batch, load_query_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        queries = batch.queries
        attacks = {
            "derived": ("c" * 64, queries),
            "order": (None, (queries[1], queries[0], *queries[2:])),
            "parent": (
                None,
                (dataclasses.replace(queries[0], parent_query_id="d" * 64), *queries[1:]),
            ),
            "endpoint": (
                None,
                (dataclasses.replace(queries[0], endpoint=queries[0].endpoint + "/x"), *queries[1:]),
            ),
            "grain": (
                None,
                (dataclasses.replace(queries[0], grain="pitch"), *queries[1:]),
            ),
            "media": (
                None,
                (dataclasses.replace(queries[0], expected_media_type="text/csv"), *queries[1:]),
            ),
            "parser": (
                None,
                (dataclasses.replace(queries[0], parser_version="v0"), *queries[1:]),
            ),
            "parameters": (
                None,
                (dataclasses.replace(queries[0], parameters={**queries[0].parameters, "extra": True}), *queries[1:]),
            ),
            "range": (
                None,
                (dataclasses.replace(queries[0], requested_range={**queries[0].requested_range, "extra": True}), *queries[1:]),
            ),
            "binding": (
                None,
                (dataclasses.replace(queries[0], run_spec_sha256="e" * 64), *queries[1:]),
            ),
        }
        for attack, (derived, forged_queries) in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="STATIC_SCHEDULES",
                    derived_from_sha256=derived,
                    queries=tuple(forged_queries),
                )
                with self.assertRaisesRegex(ValueError, "STATIC_SCHEDULES|static"):
                    load_query_batch(root, batch_id)

    def test_load_recomputation_rejects_semantically_forged_statcast_root_batches(self):
        from mlb_backtest.v52_query_plan import (
            build_statcast_root_batch,
            load_query_batch,
        )

        queries = build_statcast_root_batch(RUN_SPEC, CONTRACT).queries
        attacks = {
            "empty": (),
            "coverage": queries[:-1],
            "order": (queries[1], queries[0], *queries[2:]),
            "parent": (
                dataclasses.replace(queries[0], parent_query_id="d" * 64),
                *queries[1:],
            ),
            "parameters": (
                dataclasses.replace(
                    queries[0], parameters={**queries[0].parameters, "hfGT": "P|"}
                ),
                *queries[1:],
            ),
            "range": (
                dataclasses.replace(
                    queries[0],
                    requested_range={**queries[0].requested_range, "end_date": "2018-04-01"},
                    parameters={**queries[0].parameters, "game_date_lt": "2018-04-01"},
                ),
                *queries[1:],
            ),
            "binding": (
                dataclasses.replace(queries[0], acquisition_contract_sha256="e" * 64),
                *queries[1:],
            ),
        }
        for attack, forged_queries in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="REMAINING",
                    derived_from_sha256=None,
                    queries=tuple(forged_queries),
                )
                with self.assertRaisesRegex(ValueError, "REMAINING|Statcast|root"):
                    load_query_batch(root, batch_id)

    def test_remaining_derived_batches_require_valid_parented_statcast_queries(self):
        from mlb_backtest.v52_query_plan import (
            _make_batch,
            bisect_statcast_query,
            build_statcast_root_batch,
            load_query_batch,
        )

        root_query = build_statcast_root_batch(RUN_SPEC, CONTRACT).queries[0]
        left, right = bisect_statcast_query(root_query)
        left_left, left_right = bisect_statcast_query(left)
        valid = _make_batch("REMAINING", "c" * 64, [left_left, left_right, right])
        self.assertTrue(all(query.parent_query_id is not None for query in valid.queries))

        attacks = {
            "empty": (),
            "missing_parent": (root_query,),
            "parameters": (
                dataclasses.replace(
                    left, parameters={**left.parameters, "hfGT": "P|"}
                ),
            ),
            "binding": (
                left,
                dataclasses.replace(right, run_spec_sha256="e" * 64),
            ),
        }
        for attack, forged_queries in attacks.items():
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_id = _write_forged_canonical_batch(
                    root,
                    stage="REMAINING",
                    derived_from_sha256="f" * 64,
                    queries=tuple(forged_queries),
                )
                with self.assertRaisesRegex(ValueError, "REMAINING|Statcast|parent"):
                    load_query_batch(root, batch_id)

    def test_load_recomputation_rejects_static_remaining_source_mixing(self):
        from mlb_backtest.v52_query_plan import (
            build_statcast_root_batch,
            build_static_batch,
            load_query_batch,
        )

        static = build_static_batch(RUN_SPEC, CONTRACT)
        statcast = build_statcast_root_batch(RUN_SPEC, CONTRACT)
        attacks = (
            ("REMAINING", static.queries),
            ("STATIC_SCHEDULES", (*static.queries[:-1], statcast.queries[0])),
        )
        for stage, queries in attacks:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as td:
                payload = {
                    "stage": stage,
                    "derived_from_sha256": None,
                    "queries": [json.loads(canonical_query_bytes(q)) for q in queries],
                }
                batch_id = hashlib.sha256(
                    _canonical_json_bytes(payload).removesuffix(b"\n")
                ).hexdigest()
                record = {"schema_version": 1, "batch_id": batch_id, **payload}
                root = Path(td).resolve()
                batch_dir = (
                    root / "audit/v5_2_qd/historical/acquisition/query_batches"
                )
                batch_dir.mkdir(parents=True)
                (batch_dir / f"{batch_id}.json").write_bytes(
                    _canonical_json_bytes(record)
                )
                with self.assertRaisesRegex(
                    ValueError, "stage|source|STATIC_SCHEDULES|Statcast"
                ):
                    load_query_batch(root, batch_id)

    def test_static_batch_rehashes_the_bound_plan_a_amendment(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        contract = json.loads(CONTRACT.read_bytes())
        contract["bindings"]["plan_a_amendment"]["sha256"] = "0" * 64
        contents = _canonical_json_bytes(contract)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            forged_contract = Path(directory) / "acquisition-contract.json"
            forged_contract.write_bytes(contents)
            with self.assertRaisesRegex(ValueError, "amendment.*hash|binding"):
                build_static_batch(RUN_SPEC, forged_contract)

    def test_static_batch_rehashes_the_bound_design_and_plan(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        cases = (
            ("approved_design", "sha256", "0" * 64, "approved design|binding"),
            (
                "implementation_plan",
                "working_tree_sha256",
                "0" * 64,
                "implementation plan|binding",
            ),
            (
                "implementation_plan",
                "git_blob_id",
                "0" * 40,
                "implementation plan|Git|binding",
            ),
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for index, (binding, key, forged, error) in enumerate(cases):
                with self.subTest(binding=binding, key=key):
                    contract = json.loads(CONTRACT.read_bytes())
                    contract["bindings"][binding][key] = forged
                    contents = _canonical_json_bytes(contract)
                    forged_contract = Path(directory) / f"contract-{index}.json"
                    forged_contract.write_bytes(contents)
                    with self.assertRaisesRegex(ValueError, error):
                        build_static_batch(RUN_SPEC, forged_contract)

    def test_static_batch_rejects_noncanonical_acquisition_contract(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        pretty = (
            json.dumps(
                json.loads(CONTRACT.read_bytes()),
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            noncanonical_contract = Path(directory) / "acquisition-contract.json"
            noncanonical_contract.write_bytes(pretty)
            with self.assertRaisesRegex(ValueError, "canonical JSON"):
                build_static_batch(RUN_SPEC, noncanonical_contract)

    def test_static_batch_requires_the_frozen_acquisition_contract_schema(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        def mutate_statcast_limit(contract):
            contract["response_byte_limits"]["statcast"] = 1

        def mutate_timezone_path(contract):
            contract["timezone_mapping_path"] = "config/v5_2_qd_venue_timezones.json"

        def mutate_completeness_limit(contract):
            contract["response_completeness_limits"]["statcast_data_row_limit"] = 24_999

        def mutate_source_grain(contract):
            source = next(
                item
                for item in contract["sources"]
                if item["name"] == "mlb_stats_pitcher_bf"
            )
            source["grain"] = "forged_grain"

        def add_unknown_key(contract):
            contract["unexpected"] = True

        def mutate_schema_version_to_equal_float(contract):
            contract["schema_version"] = 2.0

        def mutate_split_limit_to_equal_float(contract):
            contract["response_completeness_limits"]["league_pitching_split_limit"] = (
                5000.0
            )

        cases = (
            ("statcast response cap", mutate_statcast_limit),
            ("timezone mapping path", mutate_timezone_path),
            ("response completeness", mutate_completeness_limit),
            ("source grain", mutate_source_grain),
            ("unknown top-level key", add_unknown_key),
            ("float schema version", mutate_schema_version_to_equal_float),
            ("float split limit", mutate_split_limit_to_equal_float),
        )
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            for index, (label, mutate) in enumerate(cases):
                with self.subTest(label=label):
                    contract = json.loads(CONTRACT.read_bytes())
                    mutate(contract)
                    forged_contract = Path(directory) / f"contract-{index}.json"
                    forged_contract.write_bytes(_canonical_json_bytes(contract))
                    with self.assertRaisesRegex(ValueError, "schema|frozen|contract"):
                        build_static_batch(RUN_SPEC, forged_contract)

    def test_static_batch_ignores_poisoned_git_repository_environment(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        poison = {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(ROOT / "missing-objects"),
            "GIT_COMMON_DIR": str(ROOT / "missing-common-dir"),
            "GIT_DIR": str(ROOT / "missing-git-dir"),
            "GIT_INDEX_FILE": str(ROOT / "missing-index"),
            "GIT_OBJECT_DIRECTORY": str(ROOT / "missing-object-dir"),
            "GIT_WORK_TREE": str(ROOT / "missing-work-tree"),
        }
        with mock.patch.dict(os.environ, poison, clear=False):
            batch = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertTrue(batch.queries)

    def test_static_batch_rejects_an_uncommitted_matching_schema(self):
        from mlb_backtest import v52_query_plan as query_plan

        contract = json.loads(CONTRACT.read_bytes())
        contract["response_byte_limits"]["statcast"] = 1
        schema_path = ROOT / "schemas/v5_2_acquisition_contract.schema.json"
        schema = json.loads(schema_path.read_bytes())
        schema["const"] = contract
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            temporary = Path(directory)
            forged_contract = temporary / "contract.json"
            forged_schema = temporary / "schema.json"
            forged_contract.write_bytes(_canonical_json_bytes(contract))
            forged_schema.write_bytes(_canonical_json_bytes(schema))
            relative_schema = forged_schema.relative_to(ROOT)
            with (
                mock.patch.object(
                    query_plan,
                    "_ACQUISITION_SCHEMA_RELATIVE",
                    relative_schema,
                ),
                self.assertRaisesRegex(ValueError, "schema.*Git|blob|committed"),
            ):
                query_plan.build_static_batch(RUN_SPEC, forged_contract)

    def test_bisect_children_bind_parent_and_cover_without_gap_or_overlap(self):
        from mlb_backtest.v52_query_plan import bisect_statcast_query

        parent = _parent("2024-06-01", "2024-06-30")
        left, right = bisect_statcast_query(parent)
        self.assertEqual(left.parent_query_id, logical_query_id(parent))
        self.assertEqual(right.parent_query_id, logical_query_id(parent))
        self.assertEqual(
            left.requested_range,
            {"year": 2024, "start_date": "2024-06-01", "end_date": "2024-06-15"},
        )
        self.assertEqual(
            right.requested_range,
            {"year": 2024, "start_date": "2024-06-16", "end_date": "2024-06-30"},
        )
        self.assertEqual(left.parameters["game_date_lt"], "2024-06-15")
        self.assertEqual(right.parameters["game_date_gt"], "2024-06-16")
        with self.assertRaisesRegex(ValueError, "single-day"):
            bisect_statcast_query(_parent("2024-06-01", "2024-06-01"))

    def test_schedule_reference_batch_deduplicates_boxscore_pitching_team_and_venue_queries(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        inventory = {
            "anchors": [
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 7,
                    "official_date": "2024-04-02",
                    "venue_id": 31,
                    "played": True,
                },
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 7,
                    "official_date": "2024-04-02",
                    "venue_id": 31,
                    "played": True,
                },
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 8,
                    "official_date": "2024-04-02",
                    "venue_id": 31,
                    "played": False,
                },
            ]
        }
        batch = build_schedule_reference_batch(
            inventory,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        sources = [query.source for query in batch.queries]
        self.assertEqual(sources.count("mlb_schedule_game_feed"), 1)
        boxscore = next(
            q for q in batch.queries if q.source == "mlb_schedule_game_feed"
        )
        self.assertEqual(boxscore.requested_range["game_pk"], 7)
        self.assertEqual(sources.count("mlb_stats_pitching"), 1)
        pitching = next(q for q in batch.queries if q.source == "mlb_stats_pitching")
        self.assertEqual(pitching.requested_range["consuming_game_pks"], [7])
        self.assertEqual(
            pitching.requested_range["consumer_cutoff_dates"],
            {"7": "2024-04-01"},
        )
        self.assertEqual(pitching.parameters["limit"], 5000)
        self.assertEqual(
            pitching.requested_range["response_completeness"],
            {"limit": 5000, "reject_total_splits_at_or_above_limit": True},
        )
        self.assertEqual(sum(q.endpoint.endswith("/teams") for q in batch.queries), 1)
        self.assertEqual(sum("/venues/31" in q.endpoint for q in batch.queries), 1)
        self.assertEqual(batch.stage, "REFERENCE_INPUTS")

    def test_schedule_reference_rejects_wrong_start_offseason_and_conflicting_season_rows(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        base = {
            "season": 2024,
            "season_start_date": "2024-03-20",
            "game_pk": 7,
            "official_date": "2024-04-02",
            "venue_id": 31,
            "played": True,
        }
        invalid_inventories = [
            {"anchors": [{**base, "season_start_date": "2024-03-21"}]},
            {"anchors": [{**base, "official_date": "2024-10-01"}]},
            {
                "anchors": [
                    base,
                    {
                        **base,
                        "game_pk": 8,
                        "season_start_date": "2024-03-19",
                    },
                ]
            },
        ]
        for inventory in invalid_inventories:
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                build_schedule_reference_batch(
                    inventory,
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_opening_day_d1_emits_preseason_disposition_not_invalid_stats_query(self):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        inventory = {
            "anchors": [
                {
                    "season": 2024,
                    "season_start_date": "2024-03-20",
                    "game_pk": 1,
                    "official_date": "2024-03-20",
                    "venue_id": 31,
                    "played": True,
                }
            ]
        }
        batch = build_schedule_reference_batch(
            inventory,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        pitching = [q for q in batch.queries if q.source == "mlb_stats_pitching"]
        self.assertEqual(len(pitching), 1)
        self.assertEqual(
            pitching[0].endpoint,
            "disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
        )
        self.assertEqual(
            pitching[0].requested_range["disposition"],
            "PRESEASON_NO_CURRENT_SEASON_PITCHING",
        )
        self.assertEqual(
            pitching[0].requested_range["reason"],
            "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
        )
        self.assertNotIn("startDate", pitching[0].parameters)

    def test_all_unplayed_schedule_population_emits_no_d1_query_or_disposition(self):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        batch = build_schedule_reference_batch(
            {
                "anchors": [
                    {
                        "season": 2024,
                        "season_start_date": "2024-03-20",
                        "game_pk": 1,
                        "official_date": "2024-03-20",
                        "venue_id": 31,
                        "played": False,
                    }
                ]
            },
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertFalse(any(q.source == "mlb_stats_pitching" for q in batch.queries))

    def test_schedule_reference_requires_played_and_rejects_conflicting_duplicate(self):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        base = {
            "season": 2024,
            "season_start_date": "2024-03-20",
            "game_pk": 7,
            "official_date": "2024-04-02",
            "venue_id": 31,
        }
        invalid_anchors = [
            [base],
            [{**base, "played": True}, {**base, "played": False}],
        ]
        for anchors in invalid_anchors:
            with self.subTest(anchors=anchors), self.assertRaises(ValueError):
                build_schedule_reference_batch(
                    {"anchors": anchors},
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_participant_batch_uses_only_verified_actual_starters_and_deduplicates(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        inventory = {
            "games": [
                {
                    "game_pk": 1,
                    "season": 2024,
                    "official_date": "2024-04-02",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 200],
                },
                {
                    "game_pk": 2,
                    "season": 2024,
                    "official_date": "2024-04-02",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 300],
                },
                {
                    "game_pk": 3,
                    "season": 2024,
                    "official_date": "2024-04-02",
                    "played": False,
                    "verification_status": "UNVERIFIED",
                    "participant_provenance": "PROBABLE",
                    "actual_starter_ids": [999],
                },
            ]
        }
        batch = build_participant_derived_batch(
            inventory,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertEqual(batch.stage, "PARTICIPANT_DERIVED")
        self.assertEqual(
            [q.requested_range["pitcher_id"] for q in batch.queries], [100, 200, 300]
        )
        self.assertTrue(
            all(
                q.endpoint
                == f"https://statsapi.mlb.com/api/v1/people/{q.requested_range['pitcher_id']}/stats"
                for q in batch.queries
            )
        )
        self.assertTrue(all("personId" not in q.parameters for q in batch.queries))
        self.assertTrue(
            all(q.parameters["endDate"] == "2021-12-31" for q in batch.queries)
        )
        by_pitcher = {q.requested_range["pitcher_id"]: q for q in batch.queries}
        self.assertEqual(by_pitcher[100].requested_range["consuming_game_pks"], [1, 2])
        self.assertEqual(
            by_pitcher[100].requested_range["consumer_cutoff_dates"],
            {"1": "2024-04-01", "2": "2024-04-01"},
        )
        self.assertEqual(
            by_pitcher[200].requested_range["consumer_cutoff_dates"],
            {"1": "2024-04-01"},
        )

    def test_participant_batch_splits_each_pitcher_by_cutoff_baseline_without_loss(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        inventory = {
            "games": [
                {
                    "game_pk": 11,
                    "season": 2018,
                    "official_date": "2018-03-29",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 200],
                },
                {
                    "game_pk": 22,
                    "season": 2022,
                    "official_date": "2022-04-07",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 300],
                },
            ]
        }
        batch = build_participant_derived_batch(
            inventory,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        observed = [
            (
                query.requested_range["pitcher_id"],
                query.requested_range["inclusive_end"],
                query.requested_range["consuming_game_pks"],
                query.requested_range["consumer_cutoff_dates"],
                query.parameters["endDate"],
                query.endpoint,
            )
            for query in batch.queries
        ]
        self.assertEqual(
            observed,
            [
                (
                    100,
                    "2017-12-31",
                    [11],
                    {"11": "2018-03-28"},
                    "2017-12-31",
                    "https://statsapi.mlb.com/api/v1/people/100/stats",
                ),
                (
                    100,
                    "2021-12-31",
                    [22],
                    {"22": "2022-04-06"},
                    "2021-12-31",
                    "https://statsapi.mlb.com/api/v1/people/100/stats",
                ),
                (
                    200,
                    "2017-12-31",
                    [11],
                    {"11": "2018-03-28"},
                    "2017-12-31",
                    "https://statsapi.mlb.com/api/v1/people/200/stats",
                ),
                (
                    300,
                    "2021-12-31",
                    [22],
                    {"22": "2022-04-06"},
                    "2021-12-31",
                    "https://statsapi.mlb.com/api/v1/people/300/stats",
                ),
            ],
        )
        all_game_pks = [
            game_pk
            for query in batch.queries
            for game_pk in query.requested_range["consuming_game_pks"]
        ]
        self.assertEqual(sorted(all_game_pks), [11, 11, 22, 22])
        query_ids = [logical_query_id(query) for query in batch.queries]
        self.assertEqual(len(query_ids), len(set(query_ids)))
        consumers_by_pitcher: dict[int, list[int]] = {}
        for query in batch.queries:
            consumers_by_pitcher.setdefault(
                int(query.requested_range["pitcher_id"]), []
            ).extend(query.requested_range["consuming_game_pks"])
        self.assertEqual(
            {
                pitcher_id: sorted(game_pks)
                for pitcher_id, game_pks in consumers_by_pitcher.items()
            },
            {100: [11, 22], 200: [11], 300: [22]},
        )

    def test_participant_batch_validates_2018_2024_season_and_game_date_boundaries(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        valid = {
            "games": [
                {
                    "game_pk": 1,
                    "season": 2018,
                    "official_date": "2018-03-29",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [100, 200],
                },
                {
                    "game_pk": 2,
                    "season": 2024,
                    "official_date": "2024-09-30",
                    "played": True,
                    "verification_status": "VERIFIED",
                    "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                    "actual_starter_ids": [300, 400],
                },
            ]
        }
        batch = build_participant_derived_batch(
            valid,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertEqual(
            [query.requested_range["pitcher_id"] for query in batch.queries],
            [100, 200, 300, 400],
        )

        invalid_games = [
            {**valid["games"][0], "season": 2025, "official_date": "2025-03-27"},
            {**valid["games"][0], "season": 2017, "official_date": "2017-04-02"},
            {**valid["games"][0], "season": 2024, "official_date": "2023-04-02"},
            {**valid["games"][0], "season": 2024, "official_date": "not-a-date"},
        ]
        for game in invalid_games:
            with self.subTest(game=game), self.assertRaises(ValueError):
                build_participant_derived_batch(
                    {"games": [game]},
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_participant_batch_rejects_duplicate_games_and_identical_starters(self):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        base = {
            "game_pk": 1,
            "season": 2024,
            "official_date": "2024-04-02",
            "played": True,
            "verification_status": "VERIFIED",
            "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "actual_starter_ids": [100, 200],
        }
        invalid_games = [
            [base, dict(base)],
            [base, {**base, "actual_starter_ids": [300, 400]}],
            [{**base, "actual_starter_ids": [100, 100]}],
            [{key: value for key, value in base.items() if key != "played"}],
        ]
        for games in invalid_games:
            with self.subTest(games=games), self.assertRaises(ValueError):
                build_participant_derived_batch(
                    {"games": games},
                    run_spec_sha256=RUN_SHA,
                    acquisition_contract_sha256=CONTRACT_SHA,
                )

    def test_bisect_and_legacy_share_strict_canonical_monthly_parent_validation(self):
        from mlb_backtest.v52_query_plan import (
            bisect_statcast_query,
            build_legacy_partition_batch,
        )

        valid = _parent("2024-06-01", "2024-06-30")
        invalid = [
            dataclasses.replace(
                valid,
                parameters={k: v for k, v in valid.parameters.items() if k != "hfGT"},
            ),
            dataclasses.replace(
                valid, parameters={**valid.parameters, "extra": "filter"}
            ),
            dataclasses.replace(valid, parameters={**valid.parameters, "hfGT": "P|"}),
            _parent("2025-06-01", "2025-06-30"),
            _parent("2024-06-15", "2024-07-15"),
            dataclasses.replace(
                valid, requested_range={**valid.requested_range, "year": 2023}
            ),
        ]
        for parent in invalid:
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                bisect_statcast_query(parent)
            with self.subTest(parent=parent), self.assertRaises(ValueError):
                build_legacy_partition_batch(
                    [parent], [], protected_inventory_sha256="a" * 64
                )

        left, right = bisect_statcast_query(valid)
        for child in (left, right):
            self.assertEqual(
                set(child.parameters),
                {
                    "all",
                    "type",
                    "player_type",
                    "game_date_gt",
                    "game_date_lt",
                    "hfGT",
                    "group_by",
                    "sort_col",
                    "player_event_sort",
                    "sort_order",
                    "min_pitches",
                },
            )
            self.assertEqual(child.parameters["hfGT"], "R|")
            self.assertEqual(child.parent_query_id, logical_query_id(valid))

    def test_bisect_preserves_valid_promoted_lineage_and_rejects_malformed_metadata(
        self,
    ):
        from mlb_backtest.v52_query_plan import (
            bisect_statcast_query,
            build_legacy_partition_batch,
        )

        parent = _parent("2024-06-01", "2024-06-30")
        promoted = build_legacy_partition_batch(
            [parent],
            [_legacy("2024-06-01", "2024-06-30")],
            protected_inventory_sha256="a" * 64,
        ).queries[0]
        left, right = bisect_statcast_query(promoted)
        for child in (left, right):
            self.assertEqual(
                child.requested_range["protected_inventory_sha256"], "a" * 64
            )
            self.assertEqual(
                child.requested_range["acquisition"],
                "PROTECTED_LEGACY_REFERENCE",
            )
            self.assertEqual(child.parent_query_id, logical_query_id(promoted))

        for malformed in (None, 7, "a" * 63, "G" * 64):
            candidate = dataclasses.replace(
                promoted,
                requested_range={
                    **promoted.requested_range,
                    "protected_inventory_sha256": malformed,
                },
            )
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                bisect_statcast_query(candidate)

        for candidate in (
            dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "protected_inventory_sha256": "a" * 64,
                },
            ),
            dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "acquisition": "FETCH_REQUIRED",
                    "protected_inventory_sha256": "a" * 64,
                },
            ),
            dataclasses.replace(
                parent,
                requested_range={
                    **parent.requested_range,
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                },
            ),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                bisect_statcast_query(candidate)

    def test_legacy_partition_maps_verified_children_and_fetches_only_uncovered_gaps(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_legacy_partition_batch

        parent = _parent("2024-06-01", "2024-06-30")
        catalog = [
            _legacy("2024-06-01", "2024-06-10"),
            _legacy("2024-06-21", "2024-06-30"),
        ]
        batch = build_legacy_partition_batch(
            [parent], catalog, protected_inventory_sha256="a" * 64
        )
        self.assertEqual(batch.derived_from_sha256, "a" * 64)
        self.assertEqual(
            [q.requested_range for q in batch.queries],
            [
                {
                    "year": 2024,
                    "start_date": "2024-06-01",
                    "end_date": "2024-06-10",
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                    "protected_inventory_sha256": "a" * 64,
                },
                {
                    "year": 2024,
                    "start_date": "2024-06-11",
                    "end_date": "2024-06-20",
                    "acquisition": "FETCH_REQUIRED",
                },
                {
                    "year": 2024,
                    "start_date": "2024-06-21",
                    "end_date": "2024-06-30",
                    "acquisition": "PROTECTED_LEGACY_REFERENCE",
                    "protected_inventory_sha256": "a" * 64,
                },
            ],
        )
        self.assertTrue(
            all(q.parent_query_id == logical_query_id(parent) for q in batch.queries)
        )

    def test_legacy_partition_rejects_gap_overlap_and_mixed_parent_child_ambiguity(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_legacy_partition_batch

        parent = _parent("2024-06-01", "2024-06-30")
        overlap = [
            _legacy("2024-06-01", "2024-06-20"),
            _legacy("2024-06-20", "2024-06-30"),
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            build_legacy_partition_batch(
                [parent], overlap, protected_inventory_sha256="a" * 64
            )

        mixed = [
            _legacy("2024-06-01", "2024-06-30"),
            _legacy("2024-06-01", "2024-06-10"),
        ]
        with self.assertRaisesRegex(ValueError, "mixed parent/child"):
            build_legacy_partition_batch(
                [parent], mixed, protected_inventory_sha256="a" * 64
            )

        bad_parent = dataclasses.replace(
            _parent("2024-07-01", "2024-07-31"),
            requested_range={
                "year": 2024,
                "start_date": "2024-07-02",
                "end_date": "2024-07-31",
            },
        )
        with self.assertRaisesRegex(ValueError, "parameters"):
            build_legacy_partition_batch(
                [bad_parent], [], protected_inventory_sha256="a" * 64
            )

    def test_legacy_partition_requires_exact_canonical_url_and_no_duplicate_mapping(
        self,
    ):
        from mlb_backtest.v52_query_plan import build_legacy_partition_batch

        parent = _parent("2024-06-01", "2024-06-30")
        malformed = _legacy("2024-06-01", "2024-06-10")
        malformed["canonical_query"] = {
            **malformed["canonical_query"],
            "url": "https://baseballsavant.mlb.com/statcast_search/csv?wrong=true",
        }
        with self.assertRaisesRegex(ValueError, "canonical URL"):
            build_legacy_partition_batch(
                [parent], [malformed], protected_inventory_sha256="a" * 64
            )

        duplicate = [
            _legacy("2024-06-01", "2024-06-10", raw={"sha256": "1" * 64}),
            _legacy("2024-06-01", "2024-06-10", raw={"sha256": "2" * 64}),
        ]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_legacy_partition_batch(
                [parent], duplicate, protected_inventory_sha256="a" * 64
            )

    def test_legacy_partition_rejects_unsorted_unmapped_and_incompatible_children(self):
        from mlb_backtest.v52_query_plan import build_legacy_partition_batch

        june = _parent("2024-06-01", "2024-06-30")
        july = _parent("2024-07-01", "2024-07-31")
        unsorted = [
            _legacy("2024-06-21", "2024-06-30"),
            _legacy("2024-06-01", "2024-06-10"),
        ]
        with self.assertRaisesRegex(ValueError, "ordered"):
            build_legacy_partition_batch(
                [june], unsorted, protected_inventory_sha256="a" * 64
            )

        for unmapped in (
            _legacy("2024-05-01", "2024-05-31"),
            _legacy("2024-06-25", "2024-07-05"),
            _legacy("2025-06-01", "2025-06-30"),
        ):
            with self.subTest(unmapped=unmapped), self.assertRaises(ValueError):
                build_legacy_partition_batch(
                    [june, july], [unmapped], protected_inventory_sha256="a" * 64
                )

        incompatible = _legacy("2024-06-01", "2024-06-10")
        incompatible["canonical_query"] = {
            **incompatible["canonical_query"],
            "url": statcast_url("2024-06-01", "2024-06-10").replace(
                "hfGT=R%7C", "hfGT=P%7C"
            ),
        }
        with self.assertRaisesRegex(ValueError, "canonical URL"):
            build_legacy_partition_batch(
                [june], [incompatible], protected_inventory_sha256="a" * 64
            )

    def test_batches_and_published_bytes_are_stable_closed_and_immutable(self):
        import mlb_backtest.v52_query_plan as query_plan

        from mlb_backtest.v52_query_plan import (
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )

        first = build_static_batch(RUN_SPEC, CONTRACT)
        second = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = publish_query_batch(root, first)
            original = path.read_bytes()
            self.assertTrue(original.endswith(b"\n"))
            self.assertFalse(original.endswith(b"\n\n"))
            record = json.loads(original)
            self.assertEqual(
                set(record),
                {
                    "schema_version",
                    "batch_id",
                    "stage",
                    "derived_from_sha256",
                    "queries",
                },
            )
            self.assertEqual(publish_query_batch(root, first), path)
            self.assertEqual(path.read_bytes(), original)
            with mock.patch.object(
                query_plan.os,
                "listdir",
                side_effect=AssertionError("batch load must not discover files"),
            ):
                self.assertEqual(load_query_batch(root, first.batch_id), first)
            conflicting = dataclasses.replace(first, stage="REMAINING")
            with self.assertRaisesRegex(ValueError, "REMAINING|Statcast"):
                publish_query_batch(root, conflicting)

    def test_load_query_batch_rejects_id_path_alias_and_canonical_binding_attacks(self):
        from mlb_backtest.v52_query_plan import (
            build_static_batch,
            load_query_batch,
            publish_query_batch,
        )

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        for bad_id in (batch.batch_id.upper(), "../" + batch.batch_id, "g" * 64):
            with self.subTest(bad_id=bad_id), tempfile.TemporaryDirectory() as td:
                with self.assertRaises(ValueError):
                    load_query_batch(Path(td).resolve(), bad_id)

        for attack in ("embedded_id", "unknown", "query", "noncanonical"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                path = publish_query_batch(root, batch)
                record = json.loads(path.read_bytes())
                if attack == "embedded_id":
                    record["batch_id"] = "0" * 64
                    contents = _canonical_json_bytes(record)
                elif attack == "unknown":
                    record["unknown"] = True
                    contents = _canonical_json_bytes(record)
                elif attack == "query":
                    record["queries"][0]["parameters"]["sportId"] = 2.0
                    contents = _canonical_json_bytes(record)
                else:
                    contents = json.dumps(record, indent=2).encode() + b"\n"
                path.write_bytes(contents)
                with self.assertRaises((ValueError, RuntimeError, TypeError)):
                    load_query_batch(root, batch.batch_id)

        for attack in ("symlink", "hardlink"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                path = publish_query_batch(root, batch)
                contents = path.read_bytes()
                path.unlink()
                target = root / "target.json"
                target.write_bytes(contents)
                if attack == "symlink":
                    path.symlink_to(target)
                else:
                    os.link(target, path)
                with self.assertRaises((ValueError, RuntimeError)):
                    load_query_batch(root, batch.batch_id)

    def test_load_query_batch_rejects_ancestor_replacement_during_read(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            query_plan.publish_query_batch(root, batch)
            original_read = query_plan._read_linked_regular
            replaced = False

            def replace_ancestor(*args: object, **kwargs: object):
                nonlocal replaced
                result = original_read(*args, **kwargs)  # type: ignore[arg-type]
                if not replaced:
                    replaced = True
                    (root / "audit").rename(root / "detached-audit")
                    (root / "audit").mkdir()
                return result

            with (
                mock.patch.object(
                    query_plan,
                    "_read_linked_regular",
                    side_effect=replace_ancestor,
                ),
                self.assertRaisesRegex(ValueError, "identity|changed|ancestor"),
            ):
                query_plan.load_query_batch(root, batch.batch_id)

    def test_load_query_batch_rejects_final_file_replacement_during_parse(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = query_plan.publish_query_batch(root, batch)
            original_parse = query_plan._parse_json_object
            replaced = False

            def replace_final(*args: object, **kwargs: object):
                nonlocal replaced
                result = original_parse(*args, **kwargs)  # type: ignore[arg-type]
                if not replaced:
                    replaced = True
                    contents = path.read_bytes()
                    path.unlink()
                    path.write_bytes(contents)
                return result

            with (
                mock.patch.object(
                    query_plan,
                    "_parse_json_object",
                    side_effect=replace_final,
                ),
                self.assertRaisesRegex(ValueError, "batch|identity|changed|replaced"),
            ):
                query_plan.load_query_batch(root, batch.batch_id)

    def test_publish_rejects_symlink_ancestors_and_hardlinked_existing_file(self):
        from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            actual_root = base / "actual"
            actual_root.mkdir()
            linked_root = base / "linked"
            linked_root.symlink_to(actual_root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root"):
                publish_query_batch(linked_root, batch)

        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            actual = base / "actual"
            nested = actual / "nested"
            nested.mkdir(parents=True)
            alias = base / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root"):
                publish_query_batch(alias / "nested", batch)
            self.assertFalse((nested / "audit").exists())

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            outside = root / "outside"
            outside.mkdir()
            (root / "audit").symlink_to(outside, target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                publish_query_batch(root, batch)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = publish_query_batch(root, batch)
            alias = root / "alias"
            os.link(path, alias)
            with self.assertRaisesRegex(ValueError, "unalias"):
                publish_query_batch(root, batch)

    def test_publish_partial_temp_write_or_file_fsync_failure_leaves_no_final(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        for failure in ("write", "fsync"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                destination = (
                    root
                    / "audit/v5_2_qd/historical/acquisition/query_batches"
                    / f"{batch.batch_id}.json"
                )
                original_write = os.write
                original_fsync = os.fsync
                file_calls = 0

                def failing_write(fd, data):
                    nonlocal file_calls
                    if os.path.isfile(f"/dev/fd/{fd}"):
                        file_calls += 1
                        if file_calls == 1:
                            return original_write(fd, bytes(data[:7]))
                        raise OSError("interrupted temp write")
                    return original_write(fd, data)

                def failing_fsync(fd):
                    if os.path.isfile(f"/dev/fd/{fd}"):
                        raise OSError("temp fsync failed")
                    return original_fsync(fd)

                patcher = mock.patch.object(
                    query_plan.os,
                    failure,
                    side_effect=failing_write if failure == "write" else failing_fsync,
                )
                with patcher, self.assertRaises(OSError):
                    query_plan.publish_query_batch(root, batch)
                self.assertFalse(destination.exists())
                batch_dir = destination.parent
                self.assertEqual(
                    list(batch_dir.glob("*.tmp")) if batch_dir.exists() else [], []
                )
                self.assertEqual(
                    query_plan.publish_query_batch(root, batch), destination
                )

    def test_publish_concurrent_identical_batches_are_idempotent(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            barrier = threading.Barrier(6)

            paths = []
            errors = []

            def worker():
                try:
                    barrier.wait(timeout=5)
                    paths.append(query_plan.publish_query_batch(root, batch))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertEqual(len(paths), 6)
            self.assertEqual(len(set(paths)), 1)

    def test_publish_final_file_exists_race_validates_identical_or_conflict(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as seed_td:
            expected = query_plan.publish_query_batch(
                Path(seed_td).resolve(), batch
            ).read_bytes()

        def race(root: Path, competing: bytes):
            def lose_link(_src, dst, *, src_dir_fd, dst_dir_fd, follow_symlinks):
                del src_dir_fd, follow_symlinks
                descriptor = os.open(
                    dst,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=dst_dir_fd,
                )
                try:
                    os.write(descriptor, competing)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                raise FileExistsError(dst)

            with mock.patch.object(query_plan.os, "link", side_effect=lose_link):
                return query_plan.publish_query_batch(root, batch)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            path = race(root, expected)
            self.assertEqual(path.read_bytes(), expected)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaisesRegex(RuntimeError, "differs"):
                race(root, b"{}\n")
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            self.assertEqual(list(batch_dir.glob("*.tmp")), [])

    def test_publish_serializes_the_post_link_window_before_reconciling(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            entered = threading.Event()
            release = threading.Event()
            second_done = threading.Event()
            original_unlink = query_plan._unlink_owned_temp
            pause_lock = threading.Lock()
            paused = False

            def pause_after_link(parent_fd, name, descriptor):
                nonlocal paused
                with pause_lock:
                    should_pause = not paused
                    if should_pause:
                        paused = True
                if should_pause:
                    entered.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test did not release publisher")
                return original_unlink(parent_fd, name, descriptor)

            results = []
            errors = []

            def publish(*, mark_done=False):
                try:
                    results.append(query_plan.publish_query_batch(root, batch))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if mark_done:
                        second_done.set()

            with mock.patch.object(
                query_plan, "_unlink_owned_temp", side_effect=pause_after_link
            ):
                first = threading.Thread(target=publish)
                first.start()
                self.assertTrue(entered.wait(timeout=5))
                second = threading.Thread(target=publish, kwargs={"mark_done": True})
                second.start()
                second_was_blocked = not second_done.wait(timeout=0.1)
                release.set()
                first.join(timeout=10)
                second.join(timeout=10)

            self.assertTrue(second_was_blocked)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            final = results[0]
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertEqual(list(final.parent.glob("*.tmp")), [])

    def test_publish_retry_recovers_only_owned_stale_temp_hardlink(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with (
                mock.patch.object(
                    query_plan,
                    "_unlink_owned_temp",
                    side_effect=OSError("injected crash after link"),
                ),
                self.assertRaisesRegex(OSError, "injected crash"),
            ):
                query_plan.publish_query_batch(root, batch)

            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            final = batch_dir / f"{batch.batch_id}.json"
            stale = list(batch_dir.glob("*.tmp"))
            self.assertTrue(final.is_file())
            self.assertEqual(len(stale), 1)
            self.assertEqual(os.stat(final).st_nlink, 2)
            self.assertEqual(os.stat(stale[0]).st_ino, os.stat(final).st_ino)

            self.assertEqual(query_plan.publish_query_batch(root, batch), final)
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertEqual(list(batch_dir.glob("*.tmp")), [])

    def test_publish_recovery_rejects_conflicting_final_and_temp_without_deleting(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            batch_dir.mkdir(parents=True)
            final = batch_dir / f"{batch.batch_id}.json"
            final.write_bytes(b"{}\n")
            stale = batch_dir / f".{batch.batch_id}.{'b' * 32}.tmp"
            os.link(final, stale)

            with self.assertRaisesRegex(RuntimeError, "differs"):
                query_plan.publish_query_batch(root, batch)
            self.assertEqual(final.read_bytes(), b"{}\n")
            self.assertTrue(stale.exists())
            self.assertEqual(os.stat(final).st_nlink, 2)

    def test_publish_cleans_accumulated_prelink_private_temp_orphans(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as seed_td:
            expected = query_plan.publish_query_batch(
                Path(seed_td).resolve(), batch
            ).read_bytes()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            batch_dir.mkdir(parents=True)
            stale = [
                batch_dir / f".{batch.batch_id}.{'1' * 32}.tmp",
                batch_dir / f".{batch.batch_id}.{'2' * 32}.tmp",
            ]
            stale[0].write_bytes(expected[:17])
            stale[1].write_bytes(expected)

            final = query_plan.publish_query_batch(root, batch)
            self.assertEqual(final.read_bytes(), expected)
            self.assertEqual(os.stat(final).st_nlink, 1)
            self.assertFalse(any(path.exists() for path in stale))

    def test_publish_never_deletes_nonprivate_or_aliased_temp_entries(self):
        import mlb_backtest.v52_query_plan as query_plan

        batch = query_plan.build_static_batch(RUN_SPEC, CONTRACT)
        matching_name = f".{batch.batch_id}.{'c' * 32}.tmp"

        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
            batch_dir.mkdir(parents=True)
            unrelated = batch_dir / ".unrelated.tmp"
            unrelated.write_bytes(b"keep")
            query_plan.publish_query_batch(root, batch)
            self.assertEqual(unrelated.read_bytes(), b"keep")

        for attack in ("symlink", "hardlink", "fifo"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as td:
                root = Path(td).resolve()
                batch_dir = root / "audit/v5_2_qd/historical/acquisition/query_batches"
                batch_dir.mkdir(parents=True)
                candidate = batch_dir / matching_name
                target = root / "target"
                if attack == "symlink":
                    target.write_bytes(b"keep")
                    candidate.symlink_to(target)
                elif attack == "hardlink":
                    target.write_bytes(b"keep")
                    os.link(target, candidate)
                else:
                    os.mkfifo(candidate)

                with self.assertRaises(ValueError):
                    query_plan.publish_query_batch(root, batch)
                self.assertTrue(os.path.lexists(candidate))
                final = batch_dir / f"{batch.batch_id}.json"
                self.assertFalse(final.exists())


if __name__ == "__main__":
    unittest.main()
