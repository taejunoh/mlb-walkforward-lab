from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

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
        requested_range={"start_date": start, "end_date": end},
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
        years = {
            int(str(query.requested_range["start_date"])[:4])
            for query in batch.queries
        }
        self.assertEqual(years, set(range(2018, 2025)))
        self.assertFalse(any("2025" in canonical_query_bytes(q).decode() for q in batch.queries))
        self.assertEqual(batch.stage, "STATIC_SCHEDULES")

    def test_static_batch_uses_only_official_endpoints_and_monthly_statcast_parents(self):
        from mlb_backtest.v52_query_plan import build_static_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        endpoints = {query.endpoint for query in batch.queries}
        self.assertEqual(
            endpoints,
            {
                "https://statsapi.mlb.com/api/v1/schedule",
                "https://baseballsavant.mlb.com/statcast_search/csv",
            },
        )
        statcast = [q for q in batch.queries if q.source == "baseball_savant_statcast"]
        self.assertTrue(statcast)
        for query in statcast:
            first = date.fromisoformat(str(query.requested_range["start_date"]))
            last = date.fromisoformat(str(query.requested_range["end_date"]))
            self.assertEqual(first.month, last.month)
            self.assertIsNone(query.parent_query_id)
            self.assertEqual(query.parameters["hfGT"], "R|")

    def test_bisect_children_bind_parent_and_cover_without_gap_or_overlap(self):
        from mlb_backtest.v52_query_plan import bisect_statcast_query

        parent = _parent("2024-06-01", "2024-06-30")
        left, right = bisect_statcast_query(parent)
        self.assertEqual(left.parent_query_id, logical_query_id(parent))
        self.assertEqual(right.parent_query_id, logical_query_id(parent))
        self.assertEqual(left.requested_range, {"start_date": "2024-06-01", "end_date": "2024-06-15"})
        self.assertEqual(right.requested_range, {"start_date": "2024-06-16", "end_date": "2024-06-30"})
        self.assertEqual(left.parameters["game_date_lt"], "2024-06-15")
        self.assertEqual(right.parameters["game_date_gt"], "2024-06-16")
        with self.assertRaisesRegex(ValueError, "single-day"):
            bisect_statcast_query(_parent("2024-06-01", "2024-06-01"))

    def test_schedule_reference_batch_deduplicates_boxscore_pitching_team_and_venue_queries(self):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        inventory = {
            "anchors": [
                {"season": 2024, "season_start_date": "2024-03-20", "game_pk": 7, "official_date": "2024-04-02", "venue_id": 31},
                {"season": 2024, "season_start_date": "2024-03-20", "game_pk": 7, "official_date": "2024-04-02", "venue_id": 31},
                {"season": 2024, "season_start_date": "2024-03-20", "game_pk": 8, "official_date": "2024-04-02", "venue_id": 31},
            ]
        }
        batch = build_schedule_reference_batch(
            inventory, run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        sources = [query.source for query in batch.queries]
        self.assertEqual(sources.count("mlb_schedule_game_feed"), 2)
        self.assertEqual(sources.count("mlb_stats_pitching"), 1)
        self.assertEqual(
            sum(q.endpoint.endswith("/teams") for q in batch.queries), 1
        )
        self.assertEqual(
            sum("/venues/31" in q.endpoint for q in batch.queries), 1
        )
        self.assertEqual(batch.stage, "REFERENCE_INPUTS")

    def test_opening_day_d1_emits_preseason_disposition_not_invalid_stats_query(self):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        inventory = {"anchors": [{
            "season": 2024,
            "season_start_date": "2024-03-20",
            "game_pk": 1,
            "official_date": "2024-03-20",
            "venue_id": 31,
        }]}
        batch = build_schedule_reference_batch(
            inventory, run_spec_sha256=RUN_SHA,
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
        self.assertNotIn("startDate", pitching[0].parameters)

    def test_participant_batch_uses_only_verified_actual_starters_and_deduplicates(self):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        inventory = {"games": [
            {
                "game_pk": 1,
                "official_date": "2024-04-02",
                "verification_status": "VERIFIED",
                "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                "actual_starter_ids": [100, 200],
            },
            {
                "game_pk": 2,
                "official_date": "2024-04-02",
                "verification_status": "VERIFIED",
                "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                "actual_starter_ids": [100, 300],
            },
            {
                "game_pk": 3,
                "official_date": "2024-04-02",
                "verification_status": "UNVERIFIED",
                "participant_provenance": "PROBABLE",
                "actual_starter_ids": [999],
            },
        ]}
        batch = build_participant_derived_batch(
            inventory, run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertEqual(batch.stage, "PARTICIPANT_DERIVED")
        self.assertEqual(
            [q.parameters["personId"] for q in batch.queries], [100, 200, 300]
        )
        self.assertTrue(all(q.parameters["endDate"] == "2017-12-31" for q in batch.queries))
        self.assertFalse(any(q.parameters.get("personId") == 999 for q in batch.queries))

    def test_legacy_partition_maps_verified_children_and_fetches_only_uncovered_gaps(self):
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
                {"start_date": "2024-06-01", "end_date": "2024-06-10", "acquisition": "PROTECTED_LEGACY_REFERENCE"},
                {"start_date": "2024-06-11", "end_date": "2024-06-20", "acquisition": "FETCH_REQUIRED"},
                {"start_date": "2024-06-21", "end_date": "2024-06-30", "acquisition": "PROTECTED_LEGACY_REFERENCE"},
            ],
        )
        self.assertTrue(all(q.parent_query_id == logical_query_id(parent) for q in batch.queries))

    def test_legacy_partition_rejects_gap_overlap_and_mixed_parent_child_ambiguity(self):
        from mlb_backtest.v52_query_plan import build_legacy_partition_batch

        parent = _parent("2024-06-01", "2024-06-30")
        overlap = [
            _legacy("2024-06-01", "2024-06-20"),
            _legacy("2024-06-20", "2024-06-30"),
        ]
        with self.assertRaisesRegex(ValueError, "overlap"):
            build_legacy_partition_batch([parent], overlap, protected_inventory_sha256="a" * 64)

        mixed = [
            _legacy("2024-06-01", "2024-06-30"),
            _legacy("2024-06-01", "2024-06-10"),
        ]
        with self.assertRaisesRegex(ValueError, "mixed parent/child"):
            build_legacy_partition_batch([parent], mixed, protected_inventory_sha256="a" * 64)

        bad_parent = dataclasses.replace(
            _parent("2024-07-01", "2024-07-31"),
            requested_range={"start_date": "2024-07-02", "end_date": "2024-07-31"},
        )
        with self.assertRaisesRegex(ValueError, "query range"):
            build_legacy_partition_batch([bad_parent], [], protected_inventory_sha256="a" * 64)

    def test_legacy_partition_requires_exact_canonical_url_and_no_duplicate_mapping(self):
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

    def test_batches_and_published_bytes_are_stable_closed_and_immutable(self):
        from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch

        first = build_static_batch(RUN_SPEC, CONTRACT)
        second = build_static_batch(RUN_SPEC, CONTRACT)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = publish_query_batch(root, first)
            original = path.read_bytes()
            self.assertTrue(original.endswith(b"\n"))
            self.assertFalse(original.endswith(b"\n\n"))
            record = json.loads(original)
            self.assertEqual(
                set(record),
                {"schema_version", "batch_id", "stage", "derived_from_sha256", "queries"},
            )
            self.assertEqual(publish_query_batch(root, first), path)
            self.assertEqual(path.read_bytes(), original)
            conflicting = dataclasses.replace(first, stage="REMAINING")
            with self.assertRaisesRegex(ValueError, "batch_id"):
                publish_query_batch(root, conflicting)

    def test_publish_rejects_symlink_ancestors_and_hardlinked_existing_file(self):
        from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch

        batch = build_static_batch(RUN_SPEC, CONTRACT)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            actual_root = base / "actual"
            actual_root.mkdir()
            linked_root = base / "linked"
            linked_root.symlink_to(actual_root, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "root"):
                publish_query_batch(linked_root, batch)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            (root / "audit").symlink_to(outside, target_is_directory=True)
            with self.assertRaises((ValueError, OSError)):
                publish_query_batch(root, batch)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = publish_query_batch(root, batch)
            alias = root / "alias"
            os.link(path, alias)
            with self.assertRaisesRegex(ValueError, "unalias"):
                publish_query_batch(root, batch)


if __name__ == "__main__":
    unittest.main()
