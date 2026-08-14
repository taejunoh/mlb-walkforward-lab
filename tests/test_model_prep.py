import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

import mlb_backtest.statcast as statcast_module

from mlb_backtest.manifest import RawStore
from mlb_backtest.career_bf import (
    fetch_prior_career_bf_records,
    load_prior_career_bf_records,
)
from mlb_backtest.mlb_stats import league_era_url
from mlb_backtest.model_prep import (
    _combined_history,
    eligible_starter_ids,
    build_result_free_schedule_history,
    load_league_era_from_raw,
    load_pitcher_bf_range,
    load_statcast_range,
    merge_pitcher_bf_counts,
    project_target_participant_inputs,
    safe_target_games,
)
from mlb_backtest.statcast import statcast_url
from mlb_backtest.statcast import StatcastData
from mlb_backtest.prepare import VerifiedScheduleEvidence


ROOT = Path(__file__).parents[1]
STATCAST = ROOT / "tests" / "fixtures" / "statcast_mini.csv"
ERA = ROOT / "tests" / "fixtures" / "mlb_pitching_stats.json"


class ModelPreparationSourceTest(unittest.TestCase):
    def test_statcast_loader_closes_terminal_half_from_schedule_state(self):
        payload = (
            Path(__file__).parent / "fixtures" / "statcast_preparation_e2e.csv"
        ).read_bytes()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-02",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                payload,
            )

            loaded, _hashes = load_statcast_range(
                root,
                "2024-04-02",
                "2024-04-02",
                final_outs_by_game={1002: 0},
                final_half_by_game={1002: (2, "Top")},
            )

        self.assertEqual(loaded.pitches[-1]["outs_recorded"], 1)

    def test_statcast_loader_ignores_sealed_capped_parent_when_children_cover_range(self):
        lines = STATCAST.read_bytes().splitlines()

        def payload(day, rows):
            row = lines[1].replace(b"2024-04-01", day.encode("ascii"))
            return b"\n".join([lines[0], *([row] * rows)]) + b"\n"

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            statcast_module, "STATCAST_ROW_CAP", 2
        ):
            root = Path(td)
            store = RawStore(root)
            store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                payload("2024-04-01", 2),
            )
            for day in ("2024-04-01", "2024-04-02"):
                store.put(
                    "baseball_savant_statcast",
                    {"start": day, "end": day, "game_type": "R", "url": statcast_url(day, day)},
                    payload(day, 1),
                )
            loaded, hashes = load_statcast_range(
                root, "2024-04-01", "2024-04-02"
            )

        self.assertEqual(len(loaded.pitches), 2)
        self.assertEqual(len(hashes), 2)

    def test_training_career_sources_include_only_eligible_training_starters(self):
        raw = [
            {
                "eligible": True,
                "away": {"starter": {"pitcher_id": 10}},
                "home": {"starter": {"pitcher_id": 20}},
            },
            {
                "eligible": False,
                "away": {"starter": {"pitcher_id": 30}},
                "home": {"starter": {"pitcher_id": 40}},
            },
        ]

        self.assertEqual(eligible_starter_ids(raw), [10, 20])

    def test_extra_target_career_manifest_cannot_change_frozen_training_bytes(self):
        payload = b'{"stats":[]}'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            fetch_prior_career_bf_records(
                store, [10, 20], fetcher=lambda _url: payload, sleeper=lambda _delay: None
            )
            catalog = store.verify_manifests()
            records, hashes = load_prior_career_bf_records(
                root, [10, 20], catalog=catalog
            )
            before = json.dumps(
                {"records": records, "source_manifest_sha256s": hashes},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()

            fetch_prior_career_bf_records(
                store, [30], fetcher=lambda _url: payload, sleeper=lambda _delay: None
            )
            catalog = store.verify_manifests()
            records, hashes = load_prior_career_bf_records(
                root, [10, 20], catalog=catalog
            )
            after = json.dumps(
                {"records": records, "source_manifest_sha256s": hashes},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()

        self.assertEqual(before, after)

    def test_target_projection_never_reads_result_or_status_fields(self):
        safe = {
            "game_pk": 1,
            "official_date": "2025-05-01",
            "game_type": "R",
            "scheduled_start_utc": "2025-05-01T23:00:00Z",
            "away_team": "Away",
            "away_team_id": 1,
            "home_team": "Home",
            "home_team_id": 2,
            "away_probable_pitcher_id": 10,
            "away_probable_pitcher_name": "A",
            "home_probable_pitcher_id": 20,
            "home_probable_pitcher_name": "H",
            "venue_id": 3,
            "venue_name": "Park",
            "scheduled_innings": 9,
            "doubleheader_marker": None,
            "away_lineup": [{"player_id": i, "player_name": str(i)} for i in range(1, 10)],
            "home_lineup": [{"player_id": i, "player_name": str(i)} for i in range(11, 20)],
        }

        class Poison(dict):
            def __getitem__(self, key):
                if key in {"status", "completed_innings", "away_score", "home_score", "away_is_winner", "home_is_winner", "outcome"}:
                    raise AssertionError(f"forbidden target access: {key}")
                return super().__getitem__(key)

            def items(self):
                raise AssertionError("target mapping must not be scanned")

        first = Poison(**safe, status="Final", completed_innings=9, away_score=1, home_score=2, away_is_winner=False, home_is_winner=True)
        second = Poison(**safe, status="Postponed", completed_innings=18, away_score=99, home_score=0, away_is_winner=True, home_is_winner=False)
        projected_first = project_target_participant_inputs([first])
        projected_second = project_target_participant_inputs([second])
        self.assertEqual(projected_first, projected_second)
        self.assertEqual(projected_first[0]["status"], "Scheduled")
        self.assertTrue(
            {"away_score", "home_score", "away_is_winner", "home_is_winner"}.isdisjoint(projected_first[0])
        )

        evidence = object.__new__(VerifiedScheduleEvidence)
        object.__setattr__(evidence, "coverage_start", "2025-05-01")
        object.__setattr__(evidence, "coverage_end", "2025-05-01")
        object.__setattr__(evidence, "manifest_sha256", "c" * 64)
        object.__setattr__(evidence, "query", {"role": "backtest"})
        object.__setattr__(evidence, "games", (first,))
        played = StatcastData(
            ({"game_pk": 1, "game_date": "2025-05-01", "inning": 11},),
            ({"game_pk": 1, "game_date": "2025-05-01"},),
        )
        history = build_result_free_schedule_history(
            evidence, {"Away": "AWY", "Home": "HOM"}, played
        )
        object.__setattr__(evidence, "games", (second,))
        changed = build_result_free_schedule_history(
            evidence, {"Away": "AWY", "Home": "HOM"}, played
        )
        self.assertEqual(history.rows, changed.rows)
        self.assertEqual(history.rows[0]["innings"], 11)

    def test_result_free_history_binds_rescheduled_game_to_coverage_and_statcast_date(self):
        evidence = object.__new__(VerifiedScheduleEvidence)
        object.__setattr__(evidence, "coverage_start", "2025-04-01")
        object.__setattr__(evidence, "coverage_end", "2025-04-30")
        object.__setattr__(evidence, "manifest_sha256", "c" * 64)
        object.__setattr__(evidence, "query", {"role": "history"})
        valid = {
            "game_pk": 100,
            "official_date": "2025-04-10",
            "away_team": "Away",
            "home_team": "Home",
            "venue_name": "Park",
        }
        rescheduled_original = {
            "game_pk": 778372,
            "official_date": "2025-07-29",
            "away_team": "Toronto Blue Jays",
            "home_team": "Baltimore Orioles",
            "venue_name": "Oriole Park at Camden Yards",
        }
        wrong_statcast_date = {
            "game_pk": 200,
            "official_date": "2025-04-12",
            "away_team": "Away",
            "home_team": "Home",
            "venue_name": "Park",
        }
        object.__setattr__(
            evidence, "games", (valid, wrong_statcast_date, rescheduled_original)
        )
        statcast = StatcastData(
            (
                {"game_pk": 100, "game_date": "2025-04-10", "inning": 9},
                {"game_pk": 200, "game_date": "2025-04-13", "inning": 9},
                {"game_pk": 778372, "game_date": "2025-07-29", "inning": 9},
            ),
            (
                {"game_pk": 100, "game_date": "2025-04-10"},
                {"game_pk": 200, "game_date": "2025-04-13"},
                {"game_pk": 778372, "game_date": "2025-07-29"},
            ),
        )
        team_codes = {
            "Away": "AWY",
            "Home": "HOM",
            "Toronto Blue Jays": "TOR",
            "Baltimore Orioles": "BAL",
        }

        history = build_result_free_schedule_history(evidence, team_codes, statcast)

        self.assertEqual([row["game_pk"] for row in history.rows], [100])
        self.assertEqual(
            history.provenance["excluded_schedule_date_game_pks"], (778372,)
        )
        self.assertEqual(
            history.provenance["excluded_statcast_date_game_pks"], (200,)
        )
        self.assertFalse(history.coverage_complete)

        object.__setattr__(evidence, "games", (valid, rescheduled_original))
        stale_duplicate_only = build_result_free_schedule_history(
            evidence, team_codes, statcast
        )
        self.assertTrue(stale_duplicate_only.coverage_complete)

        object.__setattr__(evidence, "coverage_start", "2025-05-01")
        object.__setattr__(evidence, "coverage_end", "2025-09-28")
        object.__setattr__(evidence, "query", {"role": "backtest"})
        object.__setattr__(evidence, "games", (rescheduled_original,))
        actual_makeup = build_result_free_schedule_history(
            evidence, team_codes, statcast
        )
        self.assertEqual([row["game_pk"] for row in actual_makeup.rows], [778372])
        self.assertTrue(actual_makeup.coverage_complete)

        combined = _combined_history((stale_duplicate_only, actual_makeup))
        children = combined.provenance["children"]
        self.assertEqual([child["role"] for child in children], ["history", "backtest"])
        self.assertEqual(children[0]["excluded_schedule_date_count"], 1)
        self.assertEqual(
            children[0]["excluded_schedule_date_game_pks"], (778372,)
        )
        self.assertEqual(children[0]["excluded_statcast_date_count"], 0)
        self.assertEqual(children[1]["excluded_schedule_date_count"], 0)
        self.assertTrue(combined.provenance["coverage_complete"])

    def test_verified_catalog_prevents_reauditing_bulk_statcast_payloads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                STATCAST.read_bytes(),
            )
            catalog = RawStore(root).verify_manifests()
            with mock.patch.object(
                RawStore,
                "verify_manifests",
                side_effect=AssertionError("bulk audit repeated"),
            ):
                loaded, _hashes = load_statcast_range(
                    root, "2024-04-01", "2024-04-02", catalog=catalog
                )
                counts, _hashes = load_pitcher_bf_range(
                    root, "2024-04-01", "2024-04-02", catalog=catalog
                )
            self.assertTrue(loaded.pitches)
            self.assertTrue(counts)

    def test_career_bf_merges_every_available_prior_training_season(self):
        self.assertEqual(
            merge_pitcher_bf_counts([{10: 40}, {10: 60, 20: 30}, {10: 70}]),
            {10: 170, 20: 30},
        )

    def test_pitcher_career_bf_is_streamed_without_retaining_prior_season_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                STATCAST.read_bytes(),
            )
            counts, hashes = load_pitcher_bf_range(root, "2024-04-01", "2024-04-02")
            self.assertEqual(counts[11], 1)
            self.assertEqual(counts[21], 1)
            self.assertEqual(len(hashes), 1)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            truncated = STATCAST.read_text("utf-8-sig").replace(
                ",strikeout,", ",truncated_pa,", 1
            ).encode("utf-8-sig")
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                truncated,
            )
            counts, _hashes = load_pitcher_bf_range(
                root, "2024-04-01", "2024-04-02"
            )
            self.assertNotIn(11, counts)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            blank_event_hbp = STATCAST.read_text("utf-8-sig").replace(
                "hit_by_pitch,hit_by_pitch", ",hit_by_pitch", 1
            ).encode("utf-8-sig")
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                blank_event_hbp,
            )
            counts, _hashes = load_pitcher_bf_range(
                root, "2024-04-01", "2024-04-02"
            )
            self.assertEqual(counts[23], 1)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": "https://example.invalid/noncanonical"},
                STATCAST.read_bytes(),
            )
            with self.assertRaisesRegex(ValueError, "canonical"):
                load_pitcher_bf_range(root, "2024-04-01", "2024-04-02")

    def test_pitcher_career_bf_collapses_exact_duplicate_pitch_rows(self):
        lines = STATCAST.read_text("utf-8-sig").splitlines()
        payload = ("\n".join(lines + [lines[2]]) + "\n").encode("utf-8-sig")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-01",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-01", "2024-04-02"),
                },
                payload,
            )

            counts, _hashes = load_pitcher_bf_range(
                root, "2024-04-01", "2024-04-02"
            )

        self.assertEqual(counts[11], 1)

    def test_pitcher_career_bf_does_not_use_in_memory_pitch_uniqueness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-01",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-01", "2024-04-02"),
                },
                STATCAST.read_bytes(),
            )

            with mock.patch(
                "mlb_backtest.statcast.iter_unique_statcast_pitches",
                side_effect=AssertionError("BF uniqueness must be disk-backed"),
            ):
                counts, _hashes = load_pitcher_bf_range(
                    root, "2024-04-01", "2024-04-02"
                )

        self.assertEqual(counts[11], 1)

    def test_pitcher_career_bf_rejects_conflicting_duplicate_pitch_rows(self):
        lines = STATCAST.read_text("utf-8-sig").splitlines()
        conflicting = lines[2].replace("86.0", "85.9")
        payload = ("\n".join(lines + [conflicting]) + "\n").encode("utf-8-sig")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-01",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-01", "2024-04-02"),
                },
                payload,
            )

            with self.assertRaisesRegex(ValueError, "conflicting duplicate pitch"):
                load_pitcher_bf_range(root, "2024-04-01", "2024-04-02")

    def test_safe_targets_strip_every_outcome_field_and_neutralize_final_status(self):
        games = [{
            "game_pk": 1,
            "official_date": "2025-05-01",
            "status": "Final",
            "away_score": 1,
            "home_score": 2,
            "away_is_winner": False,
            "home_is_winner": True,
            "away_team": "Away",
            "home_team": "Home",
        }, {
            "game_pk": 2,
            "official_date": "2025-05-02",
            "status": "Postponed",
            "away_team": "Away",
            "home_team": "Home",
        }]
        projected = safe_target_games(games)
        self.assertEqual(projected[0]["status"], "Scheduled")
        self.assertEqual(projected[1]["status"], "Postponed")
        for row in projected:
            self.assertTrue(
                {"away_score", "home_score", "away_is_winner", "home_is_winner"}.isdisjoint(row)
            )

    def test_statcast_loader_requires_verified_contiguous_nonoverlapping_ranges(self):
        payload = STATCAST.read_bytes()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = RawStore(root)
            first = payload.replace(b"2024-04-02", b"2024-04-01")
            second = payload.replace(b"2024-04-01", b"2024-04-02")
            store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-01", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-01")},
                first,
            )
            store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-02", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-02", "2024-04-02")},
                second,
            )
            loaded, hashes = load_statcast_range(root, "2024-04-01", "2024-04-02")
            self.assertGreater(len(loaded.pitches), 0)
            self.assertEqual(len(hashes), 2)

            overlap = store.put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-02", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-02")},
                payload,
            )
            self.assertTrue(overlap.manifest_path.exists())
            with self.assertRaisesRegex(ValueError, "overlap"):
                load_statcast_range(root, "2024-04-01", "2024-04-02")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            RawStore(root).put(
                "baseball_savant_statcast",
                {"start": "2024-04-01", "end": "2024-04-01", "game_type": "R", "url": statcast_url("2024-04-01", "2024-04-01")},
                STATCAST.read_bytes(),
            )
            with self.assertRaisesRegex(ValueError, "outside"):
                load_statcast_range(root, "2024-04-01", "2024-04-01")

    def test_league_era_loader_never_fetches_and_binds_manifest_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            query = {
                "stats": "byDateRange", "group": "pitching", "sportIds": 1,
                "gameType": "R", "playerPool": "ALL", "limit": 5000,
                "startDate": "2025-03-18", "endDate": "2025-04-30",
                "url": league_era_url("2025-03-18", "2025-04-30"),
            }
            RawStore(root).put("mlb_stats_pitching", query, ERA.read_bytes())
            records, hashes = load_league_era_from_raw(
                root,
                {2025: "2025-03-18"},
                ["2025-04-30"],
            )
            self.assertEqual(records["2025-04-30"]["source"], "mlb_stats_api")
            self.assertRegex(records["2025-04-30"]["manifest_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(hashes, [records["2025-04-30"]["manifest_sha256"]])
            with self.assertRaisesRegex(RuntimeError, "missing"):
                load_league_era_from_raw(root, {2025: "2025-03-18"}, ["2025-05-01"])


if __name__ == "__main__":
    unittest.main()
