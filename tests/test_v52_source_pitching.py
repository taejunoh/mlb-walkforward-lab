from __future__ import annotations

import copy
import json
import hashlib
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import jsonschema

from mlb_backtest.v52_evidence import (
    LogicalQuery,
    canonical_query_bytes,
    logical_query_id,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/v5_2_qd/sources/pitching_mini.json"
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"
RUN_SPEC = ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
CONTRACT = ROOT / "protocols/v5_2_qd/historical_source_acquisition.json"


def _statcast_source_sidecar(
    start: str = "2018-01-01",
    end: str = "2024-04-01",
    *,
    include_target_pa: bool = True,
    query: LogicalQuery | None = None,
    split_disposition: str = "READY",
) -> dict[str, object]:
    if query is None:
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
            requested_range={"year": int(start[:4]), "start_date": start, "end_date": end},
            expected_media_type="text/csv",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
    identity = logical_query_id(query)
    game_pks = [9001] if include_target_pa else []
    pitcher_ids = [101] if include_target_pa else []
    plate_appearances = []
    pitcher_bf = {}
    if include_target_pa:
        plate_appearances = [
            {
                "at_bat_number": 1,
                "batter_id": 2001,
                "events": "strikeout",
                "game_date": end,
                "game_pk": 9001,
                "pitch_number": 3,
                "pitcher_id": 101,
            }
        ]
        pitcher_bf = {"101": 1}
    return {
        "byte_count": 100,
        "grain": "pitch",
        "parser_version": "v5.2.1",
        "query_id": identity,
        "raw_sha256": hashlib.sha256(f"raw:{start}:{end}".encode()).hexdigest(),
        "schema_version": 1,
        "semantics": {
            "coverage": {
                "inclusive_start": start,
                "inclusive_end": end,
            },
            "canonical_query": json.loads(canonical_query_bytes(query)),
            "game_pks": game_pks,
            "plate_appearance_evidence": plate_appearances,
            "pitcher_ids": pitcher_ids,
            "pitcher_batters_faced": pitcher_bf,
            "split_disposition": split_disposition,
        },
        "source": "baseball_savant_statcast",
    }


def _planned_statcast_sidecars(
    cutoff: str,
    *,
    start_year: int = 2018,
    omit_ranges: set[tuple[str, str]] | None = None,
    omit_years: set[int] | None = None,
) -> list[dict[str, object]]:
    from mlb_backtest.v52_query_plan import build_static_batch

    cutoff_date = date.fromisoformat(cutoff)
    omitted_ranges = omit_ranges or set()
    omitted_years = omit_years or set()
    queries = [
        query
        for query in build_static_batch(RUN_SPEC, CONTRACT).queries
        if query.source == "baseball_savant_statcast"
        and start_year <= int(query.requested_range["year"]) <= cutoff_date.year
        and date.fromisoformat(str(query.requested_range["start_date"]))
        <= cutoff_date
        and int(query.requested_range["year"]) not in omitted_years
        and (
            str(query.requested_range["start_date"]),
            str(query.requested_range["end_date"]),
        )
        not in omitted_ranges
    ]
    sidecars = []
    for index, query in enumerate(queries):
        sidecar = _statcast_source_sidecar(
            str(query.requested_range["start_date"]),
            str(query.requested_range["end_date"]),
            include_target_pa=index == len(queries) - 1,
            query=query,
        )
        sidecars.append(sidecar)
    return sidecars


def _planned_statcast_query(start: str, end: str) -> LogicalQuery:
    from mlb_backtest.v52_query_plan import build_static_batch

    for query in build_static_batch(RUN_SPEC, CONTRACT).queries:
        if (
            query.source == "baseball_savant_statcast"
            and query.requested_range.get("start_date") == start
            and query.requested_range.get("end_date") == end
        ):
            return query
    raise AssertionError(f"planned Statcast query is missing: {start}..{end}")


def _statcast_child_query(
    parent: LogicalQuery,
    start: str,
    end: str,
    *,
    parent_query_id: str | None = None,
) -> LogicalQuery:
    return replace(
        parent,
        parameters={
            **parent.parameters,
            "game_date_gt": start,
            "game_date_lt": end,
        },
        requested_range={
            **parent.requested_range,
            "start_date": start,
            "end_date": end,
        },
        parent_query_id=(
            logical_query_id(parent)
            if parent_query_id is None
            else parent_query_id
        ),
    )


def _replace_april_2024_parent_with_children(
    sidecars: list[dict[str, object]],
    *,
    second_start: str = "2024-04-16",
    first_end: str = "2024-04-15",
    parent_query_id: str | None = None,
) -> list[dict[str, object]]:
    parent = _planned_statcast_query("2024-04-01", "2024-04-30")
    left_query = _statcast_child_query(parent, "2024-04-01", first_end)
    right_query = _statcast_child_query(
        parent,
        second_start,
        "2024-04-30",
        parent_query_id=parent_query_id,
    )
    children = [
        _statcast_source_sidecar(
            "2024-04-01",
            first_end,
            include_target_pa=False,
            query=left_query,
        ),
        _statcast_source_sidecar(
            second_start,
            "2024-04-30",
            query=right_query,
        ),
    ]
    return [*sidecars[:-1], *children]


def _legacy_catalog_record(
    parent: LogicalQuery, start: str, end: str
) -> dict[str, object]:
    parameters = {
        **parent.parameters,
        "game_date_gt": start,
        "game_date_lt": end,
    }
    return {
        "source": "baseball_savant_statcast",
        "rawstore_verified": True,
        "canonical_query": {
            "start": start,
            "end": end,
            "game_type": "R",
            "url": f"{parent.endpoint}?{urlencode(parameters)}",
        },
    }


def _task3_legacy_queries(*, protected: bool) -> tuple[LogicalQuery, ...]:
    from mlb_backtest.v52_query_plan import build_legacy_partition_batch

    parent = _planned_statcast_query("2024-04-01", "2024-04-30")
    catalog = (
        [_legacy_catalog_record(parent, "2024-04-01", "2024-04-30")]
        if protected
        else []
    )
    return build_legacy_partition_batch(
        [parent], catalog, protected_inventory_sha256="c" * 64
    ).queries


def _task3_legacy_partition_queries(
    *,
    protected_inventory_sha256: str,
    protected_start: str = "2024-04-01",
    protected_end: str = "2024-04-15",
    parent_start: str = "2024-04-01",
    parent_end: str = "2024-04-30",
) -> tuple[LogicalQuery, ...]:
    from mlb_backtest.v52_query_plan import build_legacy_partition_batch

    parent = _planned_statcast_query(parent_start, parent_end)
    return build_legacy_partition_batch(
        [parent],
        [_legacy_catalog_record(parent, protected_start, protected_end)],
        protected_inventory_sha256=protected_inventory_sha256,
    ).queries


def _task3_fetch_month_queries(
    start: str, end: str, *, protected_inventory_sha256: str
) -> tuple[LogicalQuery, ...]:
    from mlb_backtest.v52_query_plan import build_legacy_partition_batch

    parent = _planned_statcast_query(start, end)
    return build_legacy_partition_batch(
        [parent], [], protected_inventory_sha256=protected_inventory_sha256
    ).queries


def _replace_april_parent_with_query_nodes(
    sidecars: list[dict[str, object]],
    nodes: list[tuple[LogicalQuery, str, bool]],
) -> list[dict[str, object]]:
    replacements = [
        _statcast_source_sidecar(
            str(query.requested_range["start_date"]),
            str(query.requested_range["end_date"]),
            include_target_pa=include_target_pa,
            query=query,
            split_disposition=disposition,
        )
        for query, disposition, include_target_pa in nodes
    ]
    return [*sidecars[:-1], *replacements]


def _replace_month_parent_with_query_nodes(
    sidecars: list[dict[str, object]],
    parent_start: str,
    parent_end: str,
    nodes: list[tuple[LogicalQuery, str, bool]],
) -> list[dict[str, object]]:
    index = next(
        (
            index
            for index, sidecar in enumerate(sidecars)
            if sidecar["semantics"]["coverage"]
            == {"inclusive_start": parent_start, "inclusive_end": parent_end}
        ),
        None,
    )
    if index is None:
        raise AssertionError(f"monthly parent is missing: {parent_start}..{parent_end}")
    replacements = [
        _statcast_source_sidecar(
            str(query.requested_range["start_date"]),
            str(query.requested_range["end_date"]),
            include_target_pa=include_target_pa,
            query=query,
            split_disposition=disposition,
        )
        for query, disposition, include_target_pa in nodes
    ]
    return [*sidecars[:index], *replacements, *sidecars[index + 1 :]]


def _task3_recursive_april_nodes() -> tuple[
    LogicalQuery,
    LogicalQuery,
    LogicalQuery,
    LogicalQuery,
    LogicalQuery,
]:
    from mlb_backtest.v52_query_plan import bisect_statcast_query

    root = _planned_statcast_query("2024-04-01", "2024-04-30")
    left, right = bisect_statcast_query(root)
    left_left, left_right = bisect_statcast_query(left)
    return root, left, left_left, left_right, right


def _artifact_sha(sidecar: dict[str, object]) -> str:
    payload = json.dumps(
        sidecar, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload + b"\n").hexdigest()


def _query(*, source: str = "mlb_stats_pitching", grain: str = "season_d1_league_pitching_reference", requested: dict[str, object] | None = None) -> LogicalQuery:
    requested_range = dict(requested or {"season": 2024, "start_date": "2024-03-20", "as_of_date": "2024-04-01", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}})
    if source == "mlb_stats_pitching":
        requested_range.setdefault(
            "response_completeness",
            {"limit": 5000, "reject_total_splits_at_or_above_limit": True},
        )
        parameters = {
            "stats": "byDateRange", "group": "pitching", "sportIds": 1,
            "gameType": "R", "playerPool": "ALL", "limit": 5000,
            "startDate": requested_range.get("start_date"),
            "endDate": requested_range.get("as_of_date"),
        }
    else:
        parameters = {
            "stats": "byDateRange", "group": "pitching", "gameType": "R",
            "sportIds": 1, "startDate": "1876-01-01",
            "endDate": requested_range.get("inclusive_end"),
        }
    return LogicalQuery(
        source=source,
        grain=grain,
        endpoint=(
            "https://statsapi.mlb.com/api/v1/stats"
            if source == "mlb_stats_pitching"
            else f"https://statsapi.mlb.com/api/v1/people/{requested_range.get('pitcher_id')}/stats"
        ),
        parameters=parameters,
        requested_range=requested_range,
        expected_media_type="application/json",
        parser_version="v5.2.1",
        parent_query_id=None,
        run_spec_sha256="a" * 64,
        acquisition_contract_sha256="b" * 64,
    )


def _career_baseline(inclusive_end: str, cutoff: str) -> dict[str, object]:
    from mlb_backtest.v52_sources.pitching import recompute_career_bf_sidecar

    query = _query(
        source="mlb_stats_pitcher_bf",
        grain="player_career_batters_faced_reference",
        requested={
            "pitcher_id": 101,
            "inclusive_end": inclusive_end,
            "consuming_game_pks": [9001],
            "consumer_cutoff_dates": {"9001": cutoff},
        },
    )
    return dict(recompute_career_bf_sidecar(b'{"stats":[]}', query, [9001]))


def _career_addition(
    sidecars: list[dict[str, object]], cutoff: str, *, statcast_bf: int = 1
) -> dict[str, object]:
    return {
        "pitcher_id": 101,
        "game_pk": 9001,
        "consumer_cutoff_date": cutoff,
        "statcast_bf": statcast_bf,
        "statcast_query_sidecars": sidecars,
        "statcast_query_sidecar_sha256s": [
            _artifact_sha(sidecar) for sidecar in sidecars
        ],
    }


class V52PitchingSourceTest(unittest.TestCase):
    def test_pitching_season_snapshot_seals_exact_as_of_consumers_and_hashes(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_pitching_season_snapshot,
            recompute_pitching_sidecar,
        )

        child = recompute_pitching_sidecar(FIXTURE.read_bytes(), _query(), [9001])
        snapshot = recompute_pitching_season_snapshot([child])
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(snapshot["grain"], "season_d1_league_pitching_reference")
        self.assertEqual(snapshot["records"][0]["consuming_game_pks"], [9001])
        self.assertRegex(snapshot["records"][0]["query_sidecar_sha256"], r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(ValueError, "duplicate as-of"):
            recompute_pitching_season_snapshot([child, child])

    def test_pitching_season_snapshot_rejects_consumer_bound_to_multiple_as_of_dates(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_pitching_season_snapshot,
            recompute_pitching_sidecar,
        )

        first = recompute_pitching_sidecar(FIXTURE.read_bytes(), _query(), [9001])
        second_query = _query(
            requested={
                "season": 2024,
                "start_date": "2024-03-20",
                "as_of_date": "2024-04-02",
                "consuming_game_pks": [9001],
                "consumer_cutoff_dates": {"9001": "2024-04-02"},
            }
        )
        second = recompute_pitching_sidecar(
            FIXTURE.read_bytes(), second_query, [9001]
        )
        with self.assertRaisesRegex(ValueError, "consumer gamePk"):
            recompute_pitching_season_snapshot([first, second])

    def test_career_season_snapshot_requires_verified_statcast_additions(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
            recompute_career_bf_sidecar,
        )

        query = _query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            requested={"pitcher_id": 101, "inclusive_end": "2017-12-31", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-30"}},
        )
        baseline = recompute_career_bf_sidecar(b'{"stats":[]}', query, [9001])
        with self.assertRaisesRegex(ValueError, "Statcast BF"):
            recompute_career_bf_season_snapshot([baseline], [])
        statcast_sidecars = _planned_statcast_sidecars("2024-04-30")
        addition = {
            "pitcher_id": 101,
            "game_pk": 9001,
            "consumer_cutoff_date": "2024-04-30",
            "statcast_bf": 1,
            "statcast_query_sidecars": statcast_sidecars,
            "statcast_query_sidecar_sha256s": [
                _artifact_sha(sidecar) for sidecar in statcast_sidecars
            ],
        }
        snapshot = recompute_career_bf_season_snapshot([baseline], [addition])
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(snapshot["grain"], "player_career_batters_faced_reference")
        self.assertEqual(snapshot["records"][0]["total_batters_faced"], 1)
        wrong_hash = {**addition, "statcast_query_sidecar_sha256s": ["f" * 64]}
        with self.assertRaisesRegex(ValueError, "source hash"):
            recompute_career_bf_season_snapshot([baseline], [wrong_hash])

    def test_career_aggregate_recomputes_and_rejects_forged_statcast_bf(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
            recompute_career_bf_sidecar,
        )

        query = _query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            requested={
                "pitcher_id": 101,
                "inclusive_end": "2017-12-31",
                "consuming_game_pks": [9001],
                "consumer_cutoff_dates": {"9001": "2024-04-30"},
            },
        )
        baseline = recompute_career_bf_sidecar(b'{"stats":[]}', query, [9001])
        statcast_sidecars = _planned_statcast_sidecars("2024-04-30")
        forged = {
            "pitcher_id": 101,
            "game_pk": 9001,
            "consumer_cutoff_date": "2024-04-30",
            "statcast_bf": 600,
            "statcast_query_sidecars": statcast_sidecars,
            "statcast_query_sidecar_sha256s": [
                _artifact_sha(sidecar) for sidecar in statcast_sidecars
            ],
        }
        with self.assertRaisesRegex(ValueError, "Statcast BF.*differs"):
            recompute_career_bf_season_snapshot([baseline], [forged])
        absent = copy.deepcopy(forged)
        absent["statcast_bf"] = 1
        absent_sidecar = absent["statcast_query_sidecars"][-1]
        absent_sidecar["semantics"]["pitcher_ids"] = [102]
        absent_sidecar["semantics"]["pitcher_batters_faced"] = {"102": 1}
        absent_sidecar["semantics"]["plate_appearance_evidence"][0][
            "pitcher_id"
        ] = 102
        absent["statcast_query_sidecar_sha256s"][-1] = _artifact_sha(absent_sidecar)
        with self.assertRaisesRegex(ValueError, "claimed Statcast BF differs"):
            recompute_career_bf_season_snapshot([baseline], [absent])

    def test_career_aggregate_rejects_2017_baseline_with_only_2024_evidence(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2017-12-31", "2024-04-30")
        sidecars = [_statcast_source_sidecar("2024-03-20", "2024-04-30")]
        with self.assertRaisesRegex(ValueError, "whole regular season"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2024-04-30")]
            )
    def test_career_aggregate_rejects_2021_baseline_with_only_2024_evidence(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2021-12-31", "2024-04-30")
        sidecars = [_statcast_source_sidecar("2024-03-20", "2024-04-30")]
        with self.assertRaisesRegex(ValueError, "whole regular season"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2024-04-30")]
            )
        complete = _planned_statcast_sidecars("2024-04-30", start_year=2022)
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(complete, "2024-04-30")]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_rejects_internal_month_gap(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2017-12-31", "2018-05-31")
        sidecars = [
            _statcast_source_sidecar(
                "2018-03-29", "2018-04-30", include_target_pa=False
            ),
            _statcast_source_sidecar("2018-05-02", "2018-05-31"),
        ]
        with self.assertRaisesRegex(ValueError, "regular-season coverage gap"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2018-05-31")]
            )

    def test_career_aggregate_rejects_internal_year_gap(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2020-08-31"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff, omit_years={2019})
        with self.assertRaisesRegex(ValueError, "whole regular season"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff)]
            )

    def test_career_aggregate_accepts_task3_regular_season_query_population(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_accepts_ready_children_covering_monthly_parent(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _replace_april_2024_parent_with_children(
            _planned_statcast_sidecars(cutoff)
        )
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_accepts_task3_fetch_and_protected_legacy_queries(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        for protected in (False, True):
            query = _task3_legacy_queries(protected=protected)[0]
            sidecars = _replace_april_parent_with_query_nodes(
                _planned_statcast_sidecars(cutoff),
                [(query, "READY", True)],
            )
            with self.subTest(protected=protected):
                snapshot = recompute_career_bf_season_snapshot(
                    [baseline], [_career_addition(sidecars, cutoff)]
                )
                self.assertEqual(
                    snapshot["records"][0]["verified_statcast_bf"], 1
                )

    def test_career_aggregate_rejects_malformed_task3_acquisition_metadata(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        fetch = _task3_legacy_queries(protected=False)[0]
        protected = _task3_legacy_queries(protected=True)[0]
        cases = {
            "extra fields": replace(
                fetch,
                requested_range={**fetch.requested_range, "unknown": "value"},
            ),
            "acquisition": replace(
                fetch,
                requested_range={**fetch.requested_range, "acquisition": "UNKNOWN"},
            ),
            "inventory lineage": replace(
                fetch,
                requested_range={
                    **fetch.requested_range,
                    "protected_inventory_sha256": "c" * 64,
                },
            ),
            "protected_inventory_sha256": replace(
                protected,
                requested_range={
                    **protected.requested_range,
                    "protected_inventory_sha256": "bad",
                },
            ),
        }
        for label, query in cases.items():
            sidecars = _replace_april_parent_with_query_nodes(
                _planned_statcast_sidecars(cutoff),
                [(query, "READY", True)],
            )
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                recompute_career_bf_season_snapshot(
                    [baseline], [_career_addition(sidecars, cutoff)]
                )

    def test_career_aggregate_accepts_recursive_task3_split_ancestry(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        root, left, left_left, left_right, right = _task3_recursive_april_nodes()
        sidecars = _replace_april_parent_with_query_nodes(
            _planned_statcast_sidecars(cutoff),
            [
                (root, "SPLIT_REQUIRED", True),
                (left, "SPLIT_REQUIRED", True),
                (left_left, "READY", False),
                (left_right, "READY", False),
                (right, "READY", True),
            ],
        )
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_accepts_same_root_task3_legacy_hybrid(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        protected, fetched = _task3_legacy_partition_queries(
            protected_inventory_sha256="c" * 64
        )
        sidecars = _replace_april_parent_with_query_nodes(
            _planned_statcast_sidecars(cutoff),
            [
                (protected, "READY", False),
                (fetched, "READY", True),
            ],
        )
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_accepts_task3_multi_month_acquisition_variation(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-05-31"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        march_fetch = _task3_fetch_month_queries(
            "2024-03-20",
            "2024-03-31",
            protected_inventory_sha256="c" * 64,
        )
        april_hybrid = _task3_legacy_partition_queries(
            protected_inventory_sha256="c" * 64
        )
        may_protected = _task3_legacy_partition_queries(
            protected_inventory_sha256="c" * 64,
            protected_start="2024-05-01",
            protected_end="2024-05-31",
            parent_start="2024-05-01",
            parent_end="2024-05-31",
        )
        sidecars = _replace_month_parent_with_query_nodes(
            sidecars,
            "2024-03-20",
            "2024-03-31",
            [(march_fetch[0], "READY", False)],
        )
        sidecars = _replace_month_parent_with_query_nodes(
            sidecars,
            "2024-04-01",
            "2024-04-30",
            [(query, "READY", False) for query in april_hybrid],
        )
        sidecars = _replace_month_parent_with_query_nodes(
            sidecars,
            "2024-05-01",
            "2024-05-31",
            [(may_protected[0], "READY", True)],
        )
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_rejects_cross_month_population_contract_mix(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-05-31"
        baseline = _career_baseline("2017-12-31", cutoff)
        may_root = _planned_statcast_query("2024-05-01", "2024-05-31")
        run_b = replace(may_root, run_spec_sha256="d" * 64)
        contract_b = replace(may_root, acquisition_contract_sha256="e" * 64)

        april_protected = _task3_legacy_partition_queries(
            protected_inventory_sha256="c" * 64,
            protected_start="2024-04-01",
            protected_end="2024-04-30",
        )[0]
        may_protected = _task3_legacy_partition_queries(
            protected_inventory_sha256="d" * 64,
            protected_start="2024-05-01",
            protected_end="2024-05-31",
            parent_start="2024-05-01",
            parent_end="2024-05-31",
        )[0]
        protected_mix = _replace_month_parent_with_query_nodes(
            _planned_statcast_sidecars(cutoff),
            "2024-04-01",
            "2024-04-30",
            [(april_protected, "READY", False)],
        )
        protected_mix = _replace_month_parent_with_query_nodes(
            protected_mix,
            "2024-05-01",
            "2024-05-31",
            [(may_protected, "READY", True)],
        )
        cases = {
            "global run_spec_sha256": _replace_month_parent_with_query_nodes(
                _planned_statcast_sidecars(cutoff),
                "2024-05-01",
                "2024-05-31",
                [(run_b, "READY", True)],
            ),
            "global query contract": _replace_month_parent_with_query_nodes(
                _planned_statcast_sidecars(cutoff),
                "2024-05-01",
                "2024-05-31",
                [(contract_b, "READY", True)],
            ),
            "global protected inventory": protected_mix,
        }
        for label, sidecars in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                recompute_career_bf_season_snapshot(
                    [baseline], [_career_addition(sidecars, cutoff)]
                )

    def test_career_aggregate_rejects_mixed_monthly_root_bindings(self) -> None:
        from mlb_backtest.v52_query_plan import bisect_statcast_query
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        root = _planned_statcast_query("2024-04-01", "2024-04-30")
        left, _right = bisect_statcast_query(root)
        other_run_root = replace(root, run_spec_sha256="d" * 64)
        _other_left, other_run_right = bisect_statcast_query(other_run_root)
        other_contract_root = replace(root, acquisition_contract_sha256="e" * 64)
        _contract_left, other_contract_right = bisect_statcast_query(
            other_contract_root
        )
        protected, _fetched = _task3_legacy_partition_queries(
            protected_inventory_sha256="c" * 64
        )
        _fetched_other, protected_other = _task3_legacy_partition_queries(
            protected_inventory_sha256="d" * 64,
            protected_start="2024-04-16",
            protected_end="2024-04-30",
        )
        cases = {
            "single Task3 root": [
                (left, "READY", False),
                (other_run_right, "READY", True),
            ],
            "root contract": [
                (left, "READY", False),
                (other_contract_right, "READY", True),
            ],
            "protected contract": [
                (protected, "READY", False),
                (protected_other, "READY", True),
            ],
        }
        for label, nodes in cases.items():
            sidecars = _replace_april_parent_with_query_nodes(
                _planned_statcast_sidecars(cutoff), nodes
            )
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                recompute_career_bf_season_snapshot(
                    [baseline], [_career_addition(sidecars, cutoff)]
                )

    def test_career_aggregate_rejects_invalid_recursive_split_graphs(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        root, left, left_left, left_right, right = _task3_recursive_april_nodes()
        complete_nodes = [
            (root, "SPLIT_REQUIRED", True),
            (left, "SPLIT_REQUIRED", True),
            (left_left, "READY", False),
            (left_right, "READY", False),
            (right, "READY", True),
        ]
        missing_intermediate = [
            complete_nodes[0],
            *complete_nodes[2:],
        ]
        orphan_leaf = replace(left_left, parent_query_id="f" * 64)
        orphan = [
            complete_nodes[0],
            complete_nodes[1],
            (orphan_leaf, "READY", False),
            complete_nodes[3],
            complete_nodes[4],
        ]
        may_root = _planned_statcast_query("2024-05-01", "2024-05-31")
        wrong_left = replace(left, parent_query_id=logical_query_id(may_root))
        from mlb_backtest.v52_query_plan import bisect_statcast_query

        wrong_left_left, wrong_left_right = bisect_statcast_query(wrong_left)
        wrong_root = [
            complete_nodes[0],
            (may_root, "SPLIT_REQUIRED", False),
            (wrong_left, "SPLIT_REQUIRED", False),
            (wrong_left_left, "READY", False),
            (wrong_left_right, "READY", False),
            complete_nodes[4],
        ]
        cases = {
            "missing intermediate": missing_intermediate,
            "orphan": orphan,
            "wrong root": wrong_root,
            "ambiguous duplicate": [*complete_nodes, complete_nodes[-1]],
            "complete READY leaf cover": [complete_nodes[0]],
        }
        for label, nodes in cases.items():
            sidecars = _replace_april_parent_with_query_nodes(
                _planned_statcast_sidecars(cutoff), nodes
            )
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                recompute_career_bf_season_snapshot(
                    [baseline], [_career_addition(sidecars, cutoff)]
                )

        cycle_sidecars = _replace_april_parent_with_query_nodes(
            _planned_statcast_sidecars(cutoff), complete_nodes
        )
        april_start = len(cycle_sidecars) - len(complete_nodes)
        root_sidecar = cycle_sidecars[april_start]
        left_sidecar = cycle_sidecars[april_start + 1]
        root_sidecar["semantics"]["canonical_query"]["parent_query_id"] = left_sidecar[
            "query_id"
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(cycle_sidecars, cutoff)]
            )

    def test_career_aggregate_rejects_nonready_monthly_parent(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        sidecars[-1]["semantics"]["split_disposition"] = "SPLIT_REQUIRED"
        with self.assertRaisesRegex(ValueError, "terminal READY"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff)]
            )

    def test_career_aggregate_rejects_gapped_overlapping_or_wrong_parent_children(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        planned = _planned_statcast_sidecars(cutoff)
        cases = {
            "gap": _replace_april_2024_parent_with_children(
                planned, second_start="2024-04-17"
            ),
            "overlap": _replace_april_2024_parent_with_children(
                planned, second_start="2024-04-15"
            ),
            "wrong parent": _replace_april_2024_parent_with_children(
                planned, parent_query_id="f" * 64
            ),
        }
        for label, sidecars in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                recompute_career_bf_season_snapshot(
                    [baseline], [_career_addition(sidecars, cutoff)]
                )

    def test_career_aggregate_allows_complete_inseason_rookie_zero_bf(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        final_semantics = sidecars[-1]["semantics"]
        final_semantics["game_pks"] = []
        final_semantics["pitcher_ids"] = []
        final_semantics["plate_appearance_evidence"] = []
        final_semantics["pitcher_batters_faced"] = {}
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff, statcast_bf=0)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 0)
        self.assertEqual(snapshot["records"][0]["total_batters_faced"], 0)

    def test_career_aggregate_rejects_positive_claim_for_absent_rookie(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        final_semantics = sidecars[-1]["semantics"]
        final_semantics["game_pks"] = []
        final_semantics["pitcher_ids"] = []
        final_semantics["plate_appearance_evidence"] = []
        final_semantics["pitcher_batters_faced"] = {}
        with self.assertRaisesRegex(ValueError, "claimed Statcast BF differs"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff, statcast_bf=1)]
            )

    def test_career_aggregate_rejects_task3_within_season_month_omission(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-05-31"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(
            cutoff, omit_ranges={("2024-04-01", "2024-04-30")}
        )
        with self.assertRaisesRegex(ValueError, "regular-season coverage gap"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff)]
            )

    def test_career_aggregate_rejects_task3_whole_season_omission(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-30"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff, omit_years={2019})
        with self.assertRaisesRegex(ValueError, "whole regular season"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff)]
            )

    def test_career_aggregate_rejects_consumer_d1_outside_regular_season(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-03-18"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        with self.assertRaisesRegex(ValueError, "outside the regular season"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff)]
            )

    def test_career_aggregate_accepts_containing_month_and_excludes_future_pa(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-15"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        final_semantics = sidecars[-1]["semantics"]
        before_cutoff = copy.deepcopy(final_semantics["plate_appearance_evidence"][0])
        before_cutoff["at_bat_number"] = 2
        before_cutoff["game_date"] = cutoff
        before_cutoff["game_pk"] = 9002
        final_semantics["plate_appearance_evidence"].append(before_cutoff)
        final_semantics["game_pks"] = [9001, 9002]
        final_semantics["pitcher_batters_faced"] = {"101": 2}
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff, statcast_bf=1)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_rejects_claim_that_includes_after_cutoff_pa(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-04-15"
        baseline = _career_baseline("2017-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff)
        final_semantics = sidecars[-1]["semantics"]
        before_cutoff = copy.deepcopy(final_semantics["plate_appearance_evidence"][0])
        before_cutoff["at_bat_number"] = 2
        before_cutoff["game_date"] = cutoff
        before_cutoff["game_pk"] = 9002
        final_semantics["plate_appearance_evidence"].append(before_cutoff)
        final_semantics["game_pks"] = [9001, 9002]
        final_semantics["pitcher_batters_faced"] = {"101": 2}
        with self.assertRaisesRegex(ValueError, "claimed Statcast BF differs"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, cutoff, statcast_bf=2)]
            )

    def test_career_aggregate_allows_empty_2018_opening_day_addition(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2018-03-28"
        baseline = _career_baseline("2017-12-31", cutoff)
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition([], cutoff, statcast_bf=0)]
        )
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 0)

    def test_career_aggregate_allows_empty_2022_opening_day_addition(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2022-04-06"
        baseline = _career_baseline("2021-12-31", cutoff)
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition([], cutoff, statcast_bf=0)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 0)

    def test_career_aggregate_2024_opening_day_requires_only_2022_and_2023(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        cutoff = "2024-03-19"
        baseline = _career_baseline("2021-12-31", cutoff)
        sidecars = _planned_statcast_sidecars(cutoff, start_year=2022)
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(sidecars, cutoff)]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)
    def test_pitching_sidecar_checks_completeness_totals_and_consumers(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_pitching_sidecar

        sidecar = recompute_pitching_sidecar(FIXTURE.read_bytes(), _query(), [9001])
        semantics = sidecar["semantics"]
        self.assertEqual(semantics["total_splits"], 2)
        self.assertEqual(semantics["earned_runs"], 5)
        self.assertEqual(semantics["outs"], 51)
        self.assertEqual(semantics["batters_faced"], 75)
        self.assertEqual(semantics["as_of_date"], "2024-04-01")
        self.assertEqual(semantics["season_open_date"], "2024-03-20")
        self.assertEqual(semantics["consuming_game_pks"], [9001])
        self.assertNotIn("game_pks", semantics)

    def test_pitching_rejects_paging_truncation_wrong_open_and_non_d1_mapping(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_pitching_sidecar

        payload = json.loads(FIXTURE.read_bytes())
        payload["stats"][0]["totalSplits"] = 3
        with self.assertRaisesRegex(ValueError, "truncated or paged"):
            recompute_pitching_sidecar(json.dumps(payload).encode(), _query(), [9001])
        with self.assertRaisesRegex(ValueError, "season open"):
            recompute_pitching_sidecar(
                FIXTURE.read_bytes(),
                _query(requested={"season": 2024, "start_date": "2024-03-21", "as_of_date": "2024-04-01", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}}),
                [9001],
            )

    def test_pitching_rejects_total_splits_at_response_limit(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_pitching_sidecar

        payload = json.loads(FIXTURE.read_bytes())
        payload["stats"][0]["splits"] = payload["stats"][0]["splits"] * 2500
        payload["stats"][0]["totalSplits"] = 5000
        with self.assertRaisesRegex(ValueError, "limit|capped"):
            recompute_pitching_sidecar(json.dumps(payload).encode(), _query(), [9001])

    def test_career_query_uses_person_path_and_no_person_id_parameter(self) -> None:
        query = _query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            requested={"pitcher_id": 101, "inclusive_end": "2017-12-31", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}},
        )
        self.assertEqual(
            query.endpoint,
            "https://statsapi.mlb.com/api/v1/people/101/stats",
        )
        self.assertNotIn("personId", query.parameters)

    def test_pitching_rejects_missing_task3_consumer_cutoff_binding(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_pitching_sidecar

        query = _query(
            requested={
                "season": 2024,
                "start_date": "2024-03-20",
                "as_of_date": "2024-04-01",
            }
        )
        with self.assertRaisesRegex(ValueError, "consumer"):
            recompute_pitching_sidecar(FIXTURE.read_bytes(), query, [9001])

    def test_preseason_disposition_query_has_response_local_semantics(self) -> None:
        from mlb_backtest.v52_source_contracts import (
            query_sidecar,
            recompute_query_sidecar,
        )
        from mlb_backtest.v52_sources.pitching import recompute_pitching_season_snapshot

        query = LogicalQuery(
            source="mlb_stats_pitching",
            grain="season_d1_league_pitching_reference",
            endpoint="disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
            parameters={},
            requested_range={"season": 2024, "as_of_date": "2024-03-19", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-03-19"}, "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING"},
            expected_media_type="application/json", parser_version="v5.2.1",
            parent_query_id=None, run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        semantics = recompute_query_sidecar(query, b"")
        self.assertEqual(
            semantics["disposition"], "PRESEASON_NO_CURRENT_SEASON_PITCHING"
        )
        self.assertEqual(semantics["consuming_game_pks"], [9001])
        snapshot = recompute_pitching_season_snapshot(
            [query_sidecar(b"", query, semantics)]
        )
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(
            snapshot["records"][0]["disposition"],
            "PRESEASON_NO_CURRENT_SEASON_PITCHING",
        )

    def test_verified_derived_store_sidecar_feeds_pitching_snapshot(self) -> None:
        from mlb_backtest.v52_evidence import V52EvidenceStore
        from mlb_backtest.v52_sources.pitching import (
            recompute_pitching_season_snapshot,
        )

        query = LogicalQuery(
            source="mlb_stats_pitching",
            grain="season_d1_league_pitching_reference",
            endpoint="disposition://PRESEASON_NO_CURRENT_SEASON_PITCHING",
            parameters={},
            requested_range={
                "as_of_date": "2024-03-19",
                "consumer_cutoff_dates": {"9001": "2024-03-19"},
                "consuming_game_pks": [9001],
                "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
                "reason": "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
                "season": 2024,
            },
            expected_media_type="application/json",
            parser_version="v5.2.1",
            parent_query_id=None,
            run_spec_sha256="a" * 64,
            acquisition_contract_sha256="b" * 64,
        )
        semantics = {
            "as_of_date": "2024-03-19",
            "consumer_cutoff_dates": {"9001": "2024-03-19"},
            "consuming_game_pks": [9001],
            "disposition": "PRESEASON_NO_CURRENT_SEASON_PITCHING",
            "reason": "AS_OF_DATE_PRECEDES_REGULAR_SEASON_START",
            "season": 2024,
        }
        with tempfile.TemporaryDirectory() as directory:
            store = V52EvidenceStore(Path(directory).resolve() / "raw")
            with store.acquire_query(query):
                store.publish_derived_disposition(query, semantics)
            verified = store.read_verified_success(query)
            snapshot = recompute_pitching_season_snapshot(
                [verified.query_sidecar]
            )
        jsonschema.validate(snapshot, json.loads(SCHEMA.read_text()))
        self.assertEqual(
            snapshot["records"][0]["disposition"],
            "PRESEASON_NO_CURRENT_SEASON_PITCHING",
        )

    def test_pitching_and_career_cutoffs_must_match_actual_http_parameters(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_sidecar,
            recompute_pitching_sidecar,
        )

        pitching = _query()
        tampered_pitching = pitching.__class__(
            **{**pitching.__dict__, "parameters": {**pitching.parameters, "endDate": "2024-04-30"}}
        )
        with self.assertRaisesRegex(ValueError, "parameters"):
            recompute_pitching_sidecar(FIXTURE.read_bytes(), tampered_pitching, [9001])
        career = _query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            requested={"pitcher_id": 101, "inclusive_end": "2017-12-31", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}},
        )
        tampered_career = career.__class__(
            **{**career.__dict__, "parameters": {**career.parameters, "endDate": "2024-04-30"}}
        )
        with self.assertRaisesRegex(ValueError, "parameters"):
            recompute_career_bf_sidecar(b'{"stats":[]}', tampered_career, [9001])
        with self.assertRaisesRegex(ValueError, "D-1"):
            recompute_pitching_sidecar(
                FIXTURE.read_bytes(),
                _query(requested={"season": 2024, "start_date": "2024-03-20", "as_of_date": "2024-04-02", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}}),
                [9001],
            )

    def test_career_bf_is_distinct_grain_and_binds_cutoff_consumers(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_career_bf_sidecar

        payload = {"stats": [{"type": {"displayName": "byDateRange"}, "group": {"displayName": "pitching"}, "splits": [{"stat": {"battersFaced": 400}, "team": {"id": 10}, "sport": {"id": 1}}, {"stat": {"battersFaced": 400}, "sport": {"id": 0}}]}]}
        query = _query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            requested={"pitcher_id": 101, "inclusive_end": "2017-12-31", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}, "verified_statcast_bf": {"9001": 600}},
        )
        sidecar = recompute_career_bf_sidecar(json.dumps(payload).encode(), query, [9001])
        semantics = sidecar["semantics"]
        self.assertEqual(semantics["pitcher_id"], 101)
        self.assertEqual(semantics["inclusive_end_date"], "2017-12-31")
        self.assertEqual(semantics["baseline_batters_faced"], 400)
        self.assertNotIn("consumer_batters_faced", semantics)
        self.assertNotIn("verified_statcast_bf", semantics)
        self.assertNotEqual(sidecar["grain"], "season_d1_league_pitching_reference")

    def test_career_bf_allows_legacy_2021_only_for_2022_plus_and_proves_cutoff(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_career_bf_sidecar

        payload = b'{"stats":[]}'
        base = {"pitcher_id": 101, "inclusive_end": "2021-12-31", "consuming_game_pks": [9001], "verified_statcast_bf": {"9001": 0}}
        with self.assertRaisesRegex(ValueError, r"2022\+"):
            recompute_career_bf_sidecar(payload, _query(source="mlb_stats_pitcher_bf", grain="player_career_batters_faced_reference", requested={**base, "consumer_cutoff_dates": {"9001": "2021-04-01"}}), [9001])
        with self.assertRaisesRegex(ValueError, "D-1"):
            recompute_career_bf_sidecar(payload, _query(source="mlb_stats_pitcher_bf", grain="player_career_batters_faced_reference", requested={**base, "inclusive_end": "2024-04-02", "consumer_cutoff_dates": {"9001": "2024-04-01"}}), [9001])

    def test_career_bf_accepts_task3_cutoff_query_without_dependent_raw(self) -> None:
        from mlb_backtest.v52_sources.pitching import recompute_career_bf_sidecar

        query = _query(
            source="mlb_stats_pitcher_bf",
            grain="player_career_batters_faced_reference",
            requested={"pitcher_id": 101, "inclusive_end": "2017-12-31", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}},
        )
        sidecar = recompute_career_bf_sidecar(b'{"stats":[]}', query, [9001])
        self.assertEqual(sidecar["semantics"]["baseline_batters_faced"], 0)
        self.assertTrue(
            sidecar["semantics"][
                "requires_verified_statcast_bf_through_consumer_d1"
            ]
        )


if __name__ == "__main__":
    unittest.main()
