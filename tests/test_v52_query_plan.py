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
        self.assertEqual(left.requested_range, {"year": 2024, "start_date": "2024-06-01", "end_date": "2024-06-15"})
        self.assertEqual(right.requested_range, {"year": 2024, "start_date": "2024-06-16", "end_date": "2024-06-30"})
        self.assertEqual(left.parameters["game_date_lt"], "2024-06-15")
        self.assertEqual(right.parameters["game_date_gt"], "2024-06-16")
        with self.assertRaisesRegex(ValueError, "single-day"):
            bisect_statcast_query(_parent("2024-06-01", "2024-06-01"))

    def test_schedule_reference_batch_deduplicates_boxscore_pitching_team_and_venue_queries(self):
        from mlb_backtest.v52_query_plan import build_schedule_reference_batch

        inventory = {
            "anchors": [
                {"season": 2024, "season_start_date": "2024-03-20", "game_pk": 7, "official_date": "2024-04-02", "venue_id": 31, "played": True},
                {"season": 2024, "season_start_date": "2024-03-20", "game_pk": 7, "official_date": "2024-04-02", "venue_id": 31, "played": True},
                {"season": 2024, "season_start_date": "2024-03-20", "game_pk": 8, "official_date": "2024-04-02", "venue_id": 31, "played": False},
            ]
        }
        batch = build_schedule_reference_batch(
            inventory, run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        sources = [query.source for query in batch.queries]
        self.assertEqual(sources.count("mlb_schedule_game_feed"), 1)
        boxscore = next(q for q in batch.queries if q.source == "mlb_schedule_game_feed")
        self.assertEqual(boxscore.requested_range["game_pk"], 7)
        self.assertEqual(sources.count("mlb_stats_pitching"), 1)
        pitching = next(q for q in batch.queries if q.source == "mlb_stats_pitching")
        self.assertEqual(pitching.requested_range["consuming_game_pks"], [7, 8])
        self.assertEqual(
            pitching.requested_range["consumer_cutoff_dates"],
            {"7": "2024-04-01", "8": "2024-04-01"},
        )
        self.assertEqual(
            sum(q.endpoint.endswith("/teams") for q in batch.queries), 1
        )
        self.assertEqual(
            sum("/venues/31" in q.endpoint for q in batch.queries), 1
        )
        self.assertEqual(batch.stage, "REFERENCE_INPUTS")

    def test_schedule_reference_rejects_wrong_start_offseason_and_conflicting_season_rows(self):
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

        inventory = {"anchors": [{
            "season": 2024,
            "season_start_date": "2024-03-20",
            "game_pk": 1,
            "official_date": "2024-03-20",
            "venue_id": 31,
            "played": True,
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

    def test_participant_batch_uses_only_verified_actual_starters_and_deduplicates(self):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        inventory = {"games": [
            {
                "game_pk": 1,
                "season": 2024,
                "official_date": "2024-04-02",
                "verification_status": "VERIFIED",
                "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                "actual_starter_ids": [100, 200],
            },
            {
                "game_pk": 2,
                "season": 2024,
                "official_date": "2024-04-02",
                "verification_status": "VERIFIED",
                "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                "actual_starter_ids": [100, 300],
            },
            {
                "game_pk": 3,
                "season": 2024,
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
        by_pitcher = {q.parameters["personId"]: q for q in batch.queries}
        self.assertEqual(
            by_pitcher[100].requested_range["consuming_game_pks"], [1, 2]
        )
        self.assertEqual(
            by_pitcher[100].requested_range["consumer_cutoff_dates"],
            {"1": "2024-04-01", "2": "2024-04-01"},
        )
        self.assertEqual(
            by_pitcher[200].requested_range["consumer_cutoff_dates"],
            {"1": "2024-04-01"},
        )

    def test_participant_batch_validates_2018_2024_season_and_game_date_boundaries(self):
        from mlb_backtest.v52_query_plan import build_participant_derived_batch

        valid = {"games": [
            {
                "game_pk": 1,
                "season": 2018,
                "official_date": "2018-03-29",
                "verification_status": "VERIFIED",
                "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                "actual_starter_ids": [100, 200],
            },
            {
                "game_pk": 2,
                "season": 2024,
                "official_date": "2024-09-30",
                "verification_status": "VERIFIED",
                "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
                "actual_starter_ids": [300, 400],
            },
        ]}
        batch = build_participant_derived_batch(
            valid,
            run_spec_sha256=RUN_SHA,
            acquisition_contract_sha256=CONTRACT_SHA,
        )
        self.assertEqual(
            [query.parameters["personId"] for query in batch.queries],
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
            "verification_status": "VERIFIED",
            "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "actual_starter_ids": [100, 200],
        }
        invalid_games = [
            [base, dict(base)],
            [base, {**base, "actual_starter_ids": [300, 400]}],
            [{**base, "actual_starter_ids": [100, 100]}],
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
                valid, parameters={k: v for k, v in valid.parameters.items() if k != "hfGT"}
            ),
            dataclasses.replace(valid, parameters={**valid.parameters, "extra": "filter"}),
            dataclasses.replace(valid, parameters={**valid.parameters, "hfGT": "P|"}),
            _parent("2025-06-01", "2025-06-30"),
            _parent("2024-06-15", "2024-07-15"),
            dataclasses.replace(valid, requested_range={**valid.requested_range, "year": 2023}),
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
                    "all", "type", "player_type", "game_date_gt", "game_date_lt",
                    "hfGT", "group_by", "sort_col", "player_event_sort",
                    "sort_order", "min_pitches",
                },
            )
            self.assertEqual(child.parameters["hfGT"], "R|")
            self.assertEqual(child.parent_query_id, logical_query_id(valid))

    def test_bisect_preserves_valid_promoted_lineage_and_rejects_malformed_metadata(self):
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
                {"year": 2024, "start_date": "2024-06-01", "end_date": "2024-06-10", "acquisition": "PROTECTED_LEGACY_REFERENCE", "protected_inventory_sha256": "a" * 64},
                {"year": 2024, "start_date": "2024-06-11", "end_date": "2024-06-20", "acquisition": "FETCH_REQUIRED"},
                {"year": 2024, "start_date": "2024-06-21", "end_date": "2024-06-30", "acquisition": "PROTECTED_LEGACY_REFERENCE", "protected_inventory_sha256": "a" * 64},
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
            requested_range={"year": 2024, "start_date": "2024-07-02", "end_date": "2024-07-31"},
        )
        with self.assertRaisesRegex(ValueError, "parameters"):
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
            "url": statcast_url("2024-06-01", "2024-06-10").replace("hfGT=R%7C", "hfGT=P%7C"),
        }
        with self.assertRaisesRegex(ValueError, "canonical URL"):
            build_legacy_partition_batch(
                [june], [incompatible], protected_inventory_sha256="a" * 64
            )

    def test_batches_and_published_bytes_are_stable_closed_and_immutable(self):
        from mlb_backtest.v52_query_plan import build_static_batch, publish_query_batch

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
                    root / "audit/v5_2_qd/historical/acquisition/query_batches"
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
                self.assertEqual(query_plan.publish_query_batch(root, batch), destination)

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
                    dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
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
            with mock.patch.object(
                query_plan,
                "_unlink_owned_temp",
                side_effect=OSError("injected crash after link"),
            ), self.assertRaisesRegex(OSError, "injected crash"):
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
