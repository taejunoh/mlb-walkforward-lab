from __future__ import annotations

import copy
import json
import hashlib
import unittest
from pathlib import Path

import jsonschema

from mlb_backtest.v52_evidence import LogicalQuery


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/v5_2_qd/sources/pitching_mini.json"
SCHEMA = ROOT / "schemas/v5_2_season_source_snapshot.schema.json"


def _statcast_source_sidecar(
    start: str = "2018-01-01",
    end: str = "2024-04-01",
    *,
    include_target_pa: bool = True,
) -> dict[str, object]:
    identity = hashlib.sha256(f"{start}:{end}".encode()).hexdigest()
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
            "game_pks": game_pks,
            "plate_appearance_evidence": plate_appearances,
            "pitcher_ids": pitcher_ids,
            "pitcher_batters_faced": pitcher_bf,
        },
        "source": "baseball_savant_statcast",
    }


def _complete_statcast_sidecars(
    cutoff: str, *, start_year: int = 2018
) -> list[dict[str, object]]:
    cutoff_year = int(cutoff[:4])
    sidecars = []
    for year in range(start_year, cutoff_year + 1):
        end = cutoff if year == cutoff_year else f"{year}-12-31"
        sidecars.append(
            _statcast_source_sidecar(
                f"{year}-01-01",
                end,
                include_target_pa=year == cutoff_year,
            )
        )
    return sidecars


def _artifact_sha(sidecar: dict[str, object]) -> str:
    payload = json.dumps(
        sidecar, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(payload + b"\n").hexdigest()


def _query(*, source: str = "mlb_stats_pitching", grain: str = "season_d1_league_pitching_reference", requested: dict[str, object] | None = None) -> LogicalQuery:
    requested_range = requested or {"season": 2024, "start_date": "2024-03-20", "as_of_date": "2024-04-01", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}}
    if source == "mlb_stats_pitching":
        parameters = {
            "stats": "byDateRange", "group": "pitching", "sportIds": 1,
            "gameType": "R", "playerPool": "ALL", "limit": 5000,
            "startDate": requested_range.get("start_date"),
            "endDate": requested_range.get("as_of_date"),
        }
    else:
        parameters = {
            "personId": requested_range.get("pitcher_id"),
            "stats": "byDateRange", "group": "pitching", "gameType": "R",
            "sportIds": 1, "startDate": "1876-01-01",
            "endDate": requested_range.get("inclusive_end"),
        }
    return LogicalQuery(
        source=source,
        grain=grain,
        endpoint="https://statsapi.mlb.com/api/v1/stats",
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
    sidecars: list[dict[str, object]], cutoff: str
) -> dict[str, object]:
    return {
        "pitcher_id": 101,
        "game_pk": 9001,
        "consumer_cutoff_date": cutoff,
        "statcast_bf": 1,
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
            requested={"pitcher_id": 101, "inclusive_end": "2017-12-31", "consuming_game_pks": [9001], "consumer_cutoff_dates": {"9001": "2024-04-01"}},
        )
        baseline = recompute_career_bf_sidecar(b'{"stats":[]}', query, [9001])
        with self.assertRaisesRegex(ValueError, "Statcast BF"):
            recompute_career_bf_season_snapshot([baseline], [])
        statcast_sidecars = _complete_statcast_sidecars("2024-04-01")
        addition = {
            "pitcher_id": 101,
            "game_pk": 9001,
            "consumer_cutoff_date": "2024-04-01",
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
                "consumer_cutoff_dates": {"9001": "2024-04-01"},
            },
        )
        baseline = recompute_career_bf_sidecar(b'{"stats":[]}', query, [9001])
        statcast_sidecars = _complete_statcast_sidecars("2024-04-01")
        forged = {
            "pitcher_id": 101,
            "game_pk": 9001,
            "consumer_cutoff_date": "2024-04-01",
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
        with self.assertRaisesRegex(ValueError, "target pitcher"):
            recompute_career_bf_season_snapshot([baseline], [absent])

    def test_career_aggregate_rejects_2017_baseline_with_only_2024_evidence(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2017-12-31", "2024-04-01")
        sidecars = [_statcast_source_sidecar("2024-01-01", "2024-04-01")]
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2024-04-01")]
            )
    def test_career_aggregate_rejects_2021_baseline_with_only_2024_evidence(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2021-12-31", "2024-04-01")
        sidecars = [_statcast_source_sidecar("2024-01-01", "2024-04-01")]
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2024-04-01")]
            )
        complete = _complete_statcast_sidecars("2024-04-01", start_year=2022)
        snapshot = recompute_career_bf_season_snapshot(
            [baseline], [_career_addition(complete, "2024-04-01")]
        )
        self.assertEqual(snapshot["records"][0]["verified_statcast_bf"], 1)

    def test_career_aggregate_rejects_internal_month_gap(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2017-12-31", "2018-04-02")
        sidecars = [
            _statcast_source_sidecar(
                "2018-01-01", "2018-03-31", include_target_pa=False
            ),
            _statcast_source_sidecar("2018-04-02", "2018-04-02"),
        ]
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2018-04-02")]
            )

    def test_career_aggregate_rejects_internal_year_gap(self) -> None:
        from mlb_backtest.v52_sources.pitching import (
            recompute_career_bf_season_snapshot,
        )

        baseline = _career_baseline("2017-12-31", "2020-01-01")
        sidecars = [
            _statcast_source_sidecar(
                "2018-01-01", "2018-12-31", include_target_pa=False
            ),
            _statcast_source_sidecar("2020-01-01", "2020-01-01"),
        ]
        with self.assertRaisesRegex(ValueError, "coverage gap"):
            recompute_career_bf_season_snapshot(
                [baseline], [_career_addition(sidecars, "2020-01-01")]
            )
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
