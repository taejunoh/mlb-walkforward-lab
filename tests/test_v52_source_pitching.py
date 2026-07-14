from __future__ import annotations

import json
import unittest
from pathlib import Path

from mlb_backtest.v52_evidence import LogicalQuery


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/v5_2_qd/sources/pitching_mini.json"


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


class V52PitchingSourceTest(unittest.TestCase):
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
        from mlb_backtest.v52_source_contracts import recompute_query_sidecar

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
        self.assertEqual(semantics["consumer_batters_faced"], {"9001": 1000})
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
