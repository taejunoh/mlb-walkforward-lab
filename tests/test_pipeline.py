import hashlib
import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mlb_backtest.career_bf import fetch_prior_career_bf_records
from mlb_backtest.pipeline import (
    _load_model_card,
    _verify_candidate_provenance,
    _write_jsonl_once,
    calibrate_stage,
    features_stage,
    fetch_stage,
    lock_stage,
    main,
    outcomes_stage,
    report_stage,
    run_fixture_replay,
)
from mlb_backtest.config import load_config
from mlb_backtest.manifest import RawStore
from mlb_backtest.feature_engine import FeatureHistory, build_raw_game_features
from mlb_backtest.model_prep import (
    load_statcast_range,
    prepare_candidates_stage,
    prepare_training_stage,
)
from mlb_backtest.sources import schedule_url
from mlb_backtest.statcast import parse_statcast, regular_season_ranges, statcast_url
from mlb_backtest.scoring_prep import (
    METRIC_SPECS,
    build_training_edges,
    compute_normalization,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mini_replay"
CONFIG = Path(__file__).parents[1] / "config" / "v5_1_bt.json"


def _jsonl(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _copy_raw_schedule(root):
    config = load_config(CONFIG)
    payload = (FIXTURE / "schedule.json").read_bytes()
    store = RawStore(root / "raw")
    for start, end in regular_season_ranges(config["extension_end"])[:3]:
        store.put(
            "mlb_schedule",
            {
                "role": "training",
                "start": start,
                "end": end,
                "gameType": "R",
                "url": schedule_url(start, end),
            },
            b'{"dates":[]}',
        )
    extension_query = {
        "role": "backtest",
        "start": config["initial_window"]["start"],
        "end": config["extension_end"],
        "gameType": "R",
        "url": schedule_url(
            config["initial_window"]["start"], config["extension_end"]
        ),
    }
    store.put("mlb_schedule", extension_query, payload)


def _fixture_training_rows(source_raw):
    """Expand one real sparse-source row only for deterministic E2E calibration."""
    rows = []
    for index in range(6):
        row = deepcopy(source_raw)
        row["game_pk"] = 2000 + index
        row["official_date"] = f"2024-04-{3 + index:02d}"
        for side_index, side in enumerate(("away", "home")):
            for component_index, (component, specs) in enumerate(
                METRIC_SPECS.items()
            ):
                for metric_index, metric in enumerate(specs):
                    row[side][component][metric] = {
                        "value": 1.0
                        + component_index
                        + metric_index / 10
                        + index * (0.2 + side_index * 0.15),
                        "quality": 1.0,
                        "sample_size": 200,
                    }
            row[side]["starter"]["career_bf"] = 500
            for component in ("environment", "defense", "schedule"):
                row[side][component] = {
                    "value": 50.0 + index + side_index,
                    "quality": 1.0,
                    "sample_size": 200,
                }
        rows.append(row)
    return rows


def _prepared_training_row(game_pk, official_date, index):
    row = {
        "eligible": True,
        "game_pk": game_pk,
        "official_date": official_date,
        "lineup_provenance": "RECONSTRUCTED_FINAL_LINEUP",
    }
    for side_index, side in enumerate(("away", "home")):
        side_row = {}
        for component_index, (component, specs) in enumerate(METRIC_SPECS.items()):
            side_row[component] = {
                metric: {
                    "value": 1.0
                    + component_index
                    + metric_index / 10
                    + index * 0.4
                    + side_index * 0.2,
                    "quality": 1.0,
                    "sample_size": 200,
                }
                for metric_index, metric in enumerate(specs)
            }
        side_row["starter"].update(
            pitcher_id=13 if side == "away" else 23,
            career_bf=100,
        )
        side_row["offense"]["players"] = [
            {"player_id": side_index * 100 + slot, "batting_slot": slot}
            for slot in range(1, 10)
        ]
        for component in ("environment", "defense", "schedule"):
            side_row[component] = {
                "value": 50.0 + index + side_index,
                "quality": 1.0,
                "sample_size": 200,
            }
        row[side] = side_row
    return row


def _prepare_through_lock(root, candidates=None):
    calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
    games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
    _copy_raw_schedule(root)
    _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
    _write_jsonl(
        root / "features" / "candidate_components.jsonl",
        games if candidates is None else candidates,
    )
    calibrate_stage(CONFIG, root)
    features_stage(CONFIG, root)
    lock_stage(CONFIG, root)


class PipelineTest(unittest.TestCase):
    def test_canonical_jsonl_is_streamed_atomically_with_incremental_digest(self):
        rows = [{"b": 2, "a": 1}, {"text": "한글"}]
        expected = (
            '{"a":1,"b":2}\n{"text":"한글"}\n'.encode("utf-8")
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path, digest = _write_jsonl_once(
                root / "features" / "rows.jsonl", rows, "rows", root
            )
            repeated, repeated_digest = _write_jsonl_once(
                path, rows, "rows", root
            )
            self.assertEqual(path.read_bytes(), expected)

        self.assertEqual(repeated, path)
        self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
        self.assertEqual(repeated_digest, digest)

    def test_irrelevant_2025_career_manifest_cannot_change_frozen_artifact_bytes(self):
        seasons = regular_season_ranges(load_config(CONFIG)["extension_end"])[:3]
        fixture = (
            Path(__file__).parent / "fixtures" / "statcast_preparation_e2e.csv"
        ).read_text("utf-8")
        game_specs = [
            (2201, "2022-04-07", False),
            (2301, "2023-03-30", True),
            (2401, "2024-03-20", False),
        ]
        prepared = {
            game_pk: _prepared_training_row(game_pk, day, index)
            for index, (game_pk, day, _home_win) in enumerate(game_specs)
        }

        def seed_raw(root):
            _copy_raw_schedule(root)
            store = RawStore(root / "raw")
            for (start, end), (game_pk, day, _home_win) in zip(
                seasons, game_specs
            ):
                payload = fixture.replace("2024-04-02", day).replace(
                    ",1002,", f",{game_pk},"
                )
                store.put(
                    "baseball_savant_statcast",
                    {
                        "start": start,
                        "end": end,
                        "game_type": "R",
                        "url": statcast_url(start, end),
                    },
                    payload.encode("utf-8"),
                )
            fetch_prior_career_bf_records(
                store,
                [13, 23],
                fetcher=lambda _url: b'{"stats":[]}',
                sleeper=lambda _delay: None,
            )

        def run(root, *, extra_target_manifest):
            store = RawStore(root / "raw")
            if extra_target_manifest:
                fetch_prior_career_bf_records(
                    store,
                    [999],
                    fetcher=lambda _url: b'{"stats":[]}',
                    sleeper=lambda _delay: None,
                )

            catalog = store.verify_manifests()
            schedule_hashes = {
                item["manifest"]["query"]["start"]: item["manifest_sha256"]
                for item in catalog
                if item["manifest"].get("source") == "mlb_schedule"
                and item["manifest"]["query"].get("role") == "training"
            }
            statcast_hashes = tuple(
                sorted(
                    item["manifest_sha256"]
                    for item in catalog
                    if item["manifest"].get("source")
                    == "baseball_savant_statcast"
                )
            )
            evidence = {}
            for (start, _end), (game_pk, day, home_win) in zip(
                seasons, game_specs
            ):
                evidence[start] = SimpleNamespace(
                    manifest_sha256=schedule_hashes[start],
                    games=(
                        {
                            "game_pk": game_pk,
                            "official_date": day,
                            "status": "Final",
                            "away_is_winner": not home_win,
                            "home_is_winner": home_win,
                            "final_outs": 3,
                        },
                    ),
                )

            def schedule_evidence(_raw, _role, start, _end, **_kwargs):
                return evidence[start]

            def participants(games, _statcast, **_kwargs):
                return [dict(games[0], status="Scheduled")]

            def raw_features(_statcast, games, *_args, **_kwargs):
                return [deepcopy(prepared[int(games[0]["game_pk"])])]

            with (
                mock.patch(
                    "mlb_backtest.model_prep.parse_statcast", wraps=parse_statcast
                ) as parser,
                mock.patch(
                    "mlb_backtest.model_prep._schedule_evidence",
                    side_effect=schedule_evidence,
                ),
                mock.patch(
                    "mlb_backtest.prepare.load_verified_statcast_evidence",
                    return_value=SimpleNamespace(
                        manifest_sha256s=statcast_hashes
                    ),
                ),
                mock.patch(
                    "mlb_backtest.prepare.derive_training_slot_rows",
                    return_value=[],
                ),
                mock.patch(
                    "mlb_backtest.features.offense.derive_slot_weights",
                    return_value={slot: 1 / 9 for slot in range(1, 10)},
                ),
                mock.patch(
                    "mlb_backtest.prepare.derive_team_code_map",
                    return_value=SimpleNamespace(mapping={}),
                ),
                mock.patch(
                    "mlb_backtest.model_prep._safe_participants",
                    side_effect=participants,
                ),
                mock.patch(
                    "mlb_backtest.model_prep._history_build", return_value=object()
                ),
                mock.patch(
                    "mlb_backtest.model_prep._raw_features_for_games",
                    side_effect=raw_features,
                ),
                mock.patch(
                    "mlb_backtest.model_prep.load_league_era_from_raw",
                    return_value=({}, []),
                ),
            ):
                prepare_training_stage(CONFIG, root)
                calibrate_stage(CONFIG, root)
            self.assertEqual(parser.call_count, 3)
            return (
                (root / "calibration" / "frozen_inputs.json").read_bytes(),
                (root / "calibration" / "model_card.json").read_bytes(),
            )

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            seed_raw(source)
            without_extra_root = base / "without-extra"
            with_extra_root = base / "with-extra"
            shutil.copytree(source / "raw", without_extra_root / "raw")
            shutil.copytree(source / "raw", with_extra_root / "raw")
            without_extra = run(without_extra_root, extra_target_manifest=False)
            with_extra = run(with_extra_root, extra_target_manifest=True)

        self.assertEqual(without_extra, with_extra)

    def test_production_shaped_source_preparation_reaches_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            statcast_fixture = (
                Path(__file__).parent / "fixtures" / "statcast_preparation_e2e.csv"
            )
            RawStore(root / "raw").put(
                "baseball_savant_statcast",
                {
                    "start": "2024-04-02",
                    "end": "2024-04-02",
                    "game_type": "R",
                    "url": statcast_url("2024-04-02", "2024-04-02"),
                },
                statcast_fixture.read_bytes(),
            )
            statcast, _hashes = load_statcast_range(
                root / "raw",
                "2024-04-02",
                "2024-04-02",
                final_outs_by_game={1002: 3},
            )
            weights = {slot: 1 / 9 for slot in range(1, 10)}
            history = FeatureHistory(statcast, slot_weights=weights)
            target = {
                "game_pk": 2000,
                "official_date": "2024-04-03",
                "game_type": "R",
                "scheduled_start_utc": "2024-04-03T23:00:00Z",
                "status": "Scheduled",
                "away_team": "Away",
                "home_team": "Home",
                "venue_id": 1,
                "venue_name": "Park",
                "away_probable_pitcher_id": 23,
                "home_probable_pitcher_id": 13,
                "away_actual_starter_id": 23,
                "home_actual_starter_id": 13,
                "starter_provenance": "RECONSTRUCTED_ACTUAL_STARTER",
                "away_lineup": [
                    {"player_id": 102 + slot, "player_name": str(slot)}
                    for slot in range(1, 10)
                ],
                "home_lineup": [
                    {"player_id": 202 + slot, "player_name": str(slot)}
                    for slot in range(1, 10)
                ],
            }
            team_map = {"Away": "AWY", "Home": "HOM"}
            team_map.update({f"Team {i}": f"T{i:02d}" for i in range(28)})
            raw = build_raw_game_features(
                target,
                history,
                team_map,
                {
                    "2024-04-02": {
                        "as_of_date": "2024-04-02",
                        "value": 4.0,
                        "earned_runs": 4,
                        "outs": 27,
                        "source": "mlb_stats_api",
                        "manifest_sha256": "a" * 64,
                    }
                },
            )
            self.assertTrue(raw["eligible"])

            training_rows = _fixture_training_rows(raw)
            normalization = compute_normalization(training_rows)
            outcomes = {row["game_pk"]: index % 2 for index, row in enumerate(training_rows)}
            edges = build_training_edges(training_rows, normalization, outcomes)
            calibration = root / "calibration"
            _write_jsonl(calibration / "training_raw_features.jsonl", training_rows)
            _write_jsonl(calibration / "training_edges.jsonl", edges)
            raw_bytes = (calibration / "training_raw_features.jsonl").read_bytes()
            source_hashes = sorted(
                item["manifest_sha256"]
                for item in RawStore(root / "raw").verify_manifests()
            )
            (calibration / "frozen_inputs.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "normalization": normalization,
                        "slot_weights": {str(key): value for key, value in weights.items()},
                        "source_manifest_sha256s": source_hashes,
                        "normalization_input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                        "training_raw_count": len(training_rows),
                        "max_training_date": training_rows[-1]["official_date"],
                    },
                    sort_keys=True,
                )
                + "\n",
                "utf-8",
            )
            calibrate_stage(CONFIG, root)

            game = json.loads((FIXTURE / "games.json").read_text("utf-8"))[0]
            candidate_game = {
                key: game[key]
                for key in (
                    "game_pk",
                    "official_date",
                    "scheduled_start_utc",
                    "away_team",
                    "home_team",
                )
            }
            candidate_raw = deepcopy(training_rows[-1])
            candidate_raw.update(
                game_pk=game["game_pk"], official_date=game["official_date"]
            )
            candidate_raw["lineup_provenance"] = "RECONSTRUCTED_FINAL_LINEUP"
            store = RawStore(root / "raw")
            config = load_config(CONFIG)
            payload = statcast_fixture.read_text("utf-8").replace(
                "2024-04-02", game["official_date"]
            ).replace(",1002,", f',{game["game_pk"]},')
            store.put(
                "baseball_savant_statcast",
                {
                    "start": "2025-03-18",
                    "end": config["extension_end"],
                    "game_type": "R",
                    "url": statcast_url("2025-03-18", config["extension_end"]),
                },
                payload.encode("utf-8"),
            )
            fetch_prior_career_bf_records(
                store,
                [13, 23],
                fetcher=lambda _url: b'{"stats":[]}',
                sleeper=lambda _delay: None,
            )
            schedule_digest = next(
                item["manifest_sha256"]
                for item in store.verify_manifests()
                if item["manifest"].get("source") == "mlb_schedule"
            )
            candidate_evidence = SimpleNamespace(
                manifest_sha256=schedule_digest,
                games=(
                    {
                        **candidate_game,
                        "status": "Final",
                        "final_outs": 3,
                    },
                ),
            )
            with (
                mock.patch(
                    "mlb_backtest.model_prep._schedule_evidence",
                    return_value=candidate_evidence,
                ),
                mock.patch(
                    "mlb_backtest.prepare.derive_team_code_map",
                    return_value=SimpleNamespace(mapping=team_map),
                ),
                mock.patch(
                    "mlb_backtest.model_prep._safe_participants",
                    return_value=[candidate_game],
                ),
                mock.patch(
                    "mlb_backtest.model_prep.build_result_free_schedule_history",
                    return_value=object(),
                ),
                mock.patch(
                    "mlb_backtest.model_prep._combined_history",
                    return_value=object(),
                ),
                mock.patch(
                    "mlb_backtest.model_prep.load_league_era_from_raw",
                    return_value=({}, []),
                ),
                mock.patch(
                    "mlb_backtest.model_prep._raw_features_for_games",
                    return_value=[candidate_raw],
                ),
                mock.patch(
                    "mlb_backtest.model_prep.load_pitcher_bf_range",
                    return_value=({}, []),
                ),
            ):
                prepare_candidates_stage(CONFIG, root)
            features_stage(CONFIG, root)
            locked = lock_stage(CONFIG, root)

            self.assertEqual(locked["row_count"], 1)
            self.assertTrue((root / "locked" / "lock_manifest.json").is_file())

    def test_candidate_provenance_binds_components_raw_and_source_manifests(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            artifact = RawStore(root / "raw").put("source", {"x": 1}, b"raw\n")
            manifest_hash = hashlib.sha256(artifact.manifest_path.read_bytes()).hexdigest()
            features = root / "features"
            features.mkdir()
            raw_bytes = b'{"raw":1}\n'
            component_bytes = b'{"component":1}\n'
            (features / "candidate_raw_features.jsonl").write_bytes(raw_bytes)
            (features / "candidate_components.jsonl").write_bytes(component_bytes)
            provenance = {
                "schema_version": 1,
                "candidate_raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                "candidate_components_sha256": hashlib.sha256(component_bytes).hexdigest(),
                "source_manifest_sha256s": [manifest_hash],
            }
            (features / "candidate_provenance.json").write_text(
                json.dumps(provenance, indent=2, sort_keys=True) + "\n", "utf-8"
            )
            self.assertRegex(_verify_candidate_provenance(root), r"^[0-9a-f]{64}$")
            (features / "candidate_components.jsonl").write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ValueError, "components hash"):
                _verify_candidate_provenance(root)

    def test_calibration_freezes_normalization_slots_and_source_hashes_in_v2_card(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        frozen = {
            "schema_version": 1,
            "normalization": {
                component: {
                    metric: {"mean": 0.0, "sd": 1.0, "direction": direction, "n": 6}
                    for metric, direction in specs.items()
                }
                for component, specs in METRIC_SPECS.items()
            },
            "slot_weights": {str(i): 1 / 9 for i in range(1, 10)},
            "source_manifest_sha256s": ["a" * 64, "b" * 64],
            "normalization_input_sha256": "c" * 64,
            "training_raw_count": 3,
            "max_training_date": "2024-09-30",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            raw_training = b'{"raw":"training"}\n'
            (root / "calibration" / "training_raw_features.jsonl").write_bytes(raw_training)
            source_hashes = sorted(
                item["manifest_sha256"]
                for item in RawStore(root / "raw").verify_manifests()
            )
            frozen["normalization_input_sha256"] = hashlib.sha256(raw_training).hexdigest()
            frozen["source_manifest_sha256s"] = source_hashes
            (root / "calibration" / "frozen_inputs.json").write_text(
                json.dumps(frozen, sort_keys=True) + "\n", "utf-8"
            )
            path = calibrate_stage(CONFIG, root)
            card = json.loads(path.read_text("utf-8"))
            self.assertEqual(card["schema_version"], 2)
            self.assertEqual(card["normalization"], frozen["normalization"])
            self.assertEqual(card["slot_weights"], frozen["slot_weights"])
            self.assertEqual(card["source_manifest_sha256s"], frozen["source_manifest_sha256s"])
            self.assertLessEqual(card["max_training_date"], "2024-12-31")
            _load_model_card(root, CONFIG)
            (root / "calibration" / "training_raw_features.jsonl").write_bytes(b"tampered\n")
            with self.assertRaisesRegex(ValueError, "normalization input hash"):
                _load_model_card(root, CONFIG)
    def _replay(self, output):
        return run_fixture_replay(FIXTURE, Path(output))

    def test_predictions_lock_before_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._replay(root)

            self.assertEqual(result["selection_dates"], 5)
            self.assertEqual(result["top1_count"], 5)
            self.assertTrue((root / "locked" / "lock_manifest.json").is_file())
            self.assertTrue((root / "reports" / "summary.json").is_file())
            self.assertTrue(
                (root / "initial_locked" / "lock_manifest.json").is_file()
            )
            self.assertTrue(
                (
                    root
                    / "initial_outcomes"
                    / "evaluated"
                    / "evaluated_predictions.jsonl"
                ).is_file()
            )
            self.assertLess(result["lock_mtime_ns"], result["outcome_mtime_ns"])
            self.assertLess(
                result["initial_lock_mtime_ns"],
                result["initial_outcome_mtime_ns"],
            )
            self.assertLess(
                result["initial_outcome_mtime_ns"], result["lock_mtime_ns"]
            )
            self.assertEqual(
                result["initial_completion"],
                {
                    "graded_top1": 2,
                    "void_top1": 1,
                    "selection_dates": 3,
                    "target": 3,
                    "complete": False,
                    "dates_needed": 1,
                },
            )
            self.assertEqual(
                result["extension_dates"], ["2025-05-04", "2025-05-05"]
            )

    def test_repeated_replay_has_identical_locked_sha256(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first = self._replay(base / "first")
            second = self._replay(base / "second")

            self.assertEqual(first["locked_sha256"], second["locked_sha256"])
            self.assertEqual(
                first["initial_locked_sha256"], second["initial_locked_sha256"]
            )
            self.assertEqual(first["round_lock_shas"], second["round_lock_shas"])
            self.assertEqual(len(first["round_lock_shas"]), 2)
            self.assertNotEqual(
                first["initial_locked_sha256"], first["locked_sha256"]
            )
            first_manifest = json.loads(
                (base / "first" / "locked" / "lock_manifest.json").read_text(
                    "utf-8"
                )
            )
            second_manifest = json.loads(
                (base / "second" / "locked" / "lock_manifest.json").read_text(
                    "utf-8"
                )
            )
            self.assertEqual(first["locked_sha256"], first_manifest["sha256"])
            self.assertEqual(first_manifest["sha256"], second_manifest["sha256"])
            self.assertEqual(
                (base / "first" / "locked" / "predictions.jsonl").read_bytes(),
                (base / "second" / "locked" / "predictions.jsonl").read_bytes(),
            )
            self.assertNotIn(
                b"market",
                (base / "first" / "locked" / "predictions.jsonl").read_bytes().lower(),
            )
            self.assertEqual(
                (
                    base
                    / "first"
                    / "initial_locked"
                    / "predictions.jsonl"
                ).read_bytes(),
                (
                    base
                    / "second"
                    / "initial_locked"
                    / "predictions.jsonl"
                ).read_bytes(),
            )

    def test_doubleheader_is_ranked_as_one_selection_date(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._replay(root)
            rows = _jsonl(root / "locked" / "predictions.jsonl")
            doubleheader = [
                row for row in rows if row["official_date"] == "2025-05-02"
            ]

            self.assertEqual(len(doubleheader), 2)
            self.assertEqual(
                sorted(row["daily_rank"] for row in doubleheader), [1, 2]
            )
            self.assertEqual(
                sum(row["top1"] == "Yes" for row in doubleheader), 1
            )
            self.assertEqual(
                sum(row["top3"] == "Yes" for row in doubleheader), 2
            )
            self.assertEqual(
                sorted(row["scheduled_start_utc"] for row in doubleheader),
                ["2025-05-02T17:05:00Z", "2025-05-02T23:10:00Z"],
            )
            self.assertTrue(
                all(row["data_status"] == "COMPLETE" for row in doubleheader)
            )
            self.assertEqual(
                {row["doubleheader_marker"] for row in doubleheader},
                {"GAME1", "GAME2"},
            )

    def test_missing_environment_and_reconstructed_lineup_are_provenanced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._replay(root)
            rows = {
                row["game_pk"]: row
                for row in _jsonl(root / "locked" / "predictions.jsonl")
            }

            missing_environment = rows[3001]
            self.assertEqual(missing_environment["environment_quality"], 0.0)
            self.assertEqual(
                missing_environment["data_status"], "INCOMPLETE"
            )
            reconstructed = rows[4001]
            self.assertEqual(
                reconstructed["lineup_provenance"], "RECONSTRUCTED_FINAL_LINEUP"
            )
            self.assertEqual(reconstructed["data_status"], "COMPLETE")

    def test_void_top1_is_reported_but_not_graded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = self._replay(root)
            summary = json.loads(
                (root / "reports" / "summary.json").read_text("utf-8")
            )

            self.assertEqual(result["graded_top1"], 3)
            self.assertEqual(result["void_top1"], 2)
            self.assertEqual(summary["primary"]["N"], 3)
            self.assertEqual(summary["void_top1"], 2)
            self.assertEqual(result["graded_count"], 3)
            self.assertEqual(result["void_count"], 2)
            report = (root / "reports" / "summary.json").read_text("utf-8")
            self.assertEqual(report, json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    def test_cli_enforces_prerequisites_and_rejects_symlink_stage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "data"
            with self.assertRaisesRegex(RuntimeError, "missing prerequisite"):
                main(["lock", "--config", str(CONFIG), "--data-dir", str(root)])

            real = Path(td) / "real"
            real.mkdir()
            (root / "features").symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                main(["features", "--config", str(CONFIG), "--data-dir", str(root)])

    def test_preparation_cli_commands_enforce_source_and_lock_boundaries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaisesRegex(RuntimeError, "missing"):
                main(["prepare-training", "--config", str(CONFIG), "--data-dir", str(root)])
            with self.assertRaisesRegex(RuntimeError, "missing"):
                main(["fetch-era", "--config", str(CONFIG), "--data-dir", str(root)])
            with self.assertRaisesRegex(RuntimeError, "missing"):
                main(["fetch-career-bf", "--config", str(CONFIG), "--data-dir", str(root)])
            with self.assertRaisesRegex(RuntimeError, "missing"):
                main(["prepare-features", "--config", str(CONFIG), "--data-dir", str(root)])
            with self.assertRaisesRegex(RuntimeError, "lock"):
                main(["prepare-outcomes", "--config", str(CONFIG), "--data-dir", str(root)])

    def test_fetch_covers_frozen_extension_end(self):
        config = load_config(CONFIG)
        expected_ranges = [
            *regular_season_ranges(config["extension_end"])[:3],
            ("2025-03-18", "2025-04-30"),
            (config["initial_window"]["start"], config["extension_end"]),
        ]
        with tempfile.TemporaryDirectory() as td, mock.patch(
            "mlb_backtest.pipeline.fetch_bytes", return_value=b'{"dates":[]}'
        ) as mocked_fetch:
            root = Path(td)
            fetch_stage(CONFIG, root)
            schedule_queries = [
                row["manifest"]["query"]
                for row in RawStore(root / "raw").verify_manifests()
                if row["manifest"]["source"] == "mlb_schedule"
            ]
            self.assertEqual(len(schedule_queries), 5)
            self.assertTrue(all(query["gameType"] == "R" for query in schedule_queries))
            self.assertEqual(
                sorted((query["start"], query["end"]) for query in schedule_queries),
                sorted(expected_ranges),
            )
            history = [query for query in schedule_queries if query["role"] == "history"]
            self.assertEqual(
                [(query["start"], query["end"]) for query in history],
                [("2025-03-18", "2025-04-30")],
            )
            self.assertNotIn(
                (config["training_window"]["start"], config["training_window"]["end"]),
                expected_ranges,
            )

        self.assertEqual(
            [call.args[0] for call in mocked_fetch.call_args_list],
            [
                *[
                    schedule_url(start, end)
                    for start, end in regular_season_ranges(config["extension_end"])[:3]
                ],
                schedule_url("2025-03-18", "2025-04-30"),
                schedule_url(
                    config["initial_window"]["start"],
                    config["extension_end"],
                ),
            ],
        )

    def test_stage_contracts_reject_post_cutoff_and_leaky_candidate_inputs(self):
        games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            (root / "calibration").mkdir()
            bad_training = [dict(calibration[0], official_date="2025-01-01"), *calibration[1:]]
            (root / "calibration" / "training_edges.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in bad_training), "utf-8"
            )
            with self.assertRaisesRegex(ValueError, "frozen boundary"):
                calibrate_stage(CONFIG, root)

        for mutation, message in (
            ({"market_candidate": 0.6}, "unknown"),
            ({"data_status": {"status": "complete", "market": {"favorite": "Home"}}}, "market"),
            ({"lineup_provenance": {"source": "official", "outcome": {"winner": "Home"}}}, "outcome"),
            ({"data_status": "complete"}, "data_status"),
            ({"lineup_provenance": []}, "lineup_provenance"),
            ({"doubleheader_marker": {"slot": 1}}, "doubleheader_marker"),
            ({"source_game_dates": ["2025-05-01"]}, "same-day"),
            ({"source_game_pks": [1001]}, "target game_pk"),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _copy_raw_schedule(root)
                (root / "calibration").mkdir()
                (root / "features").mkdir()
                (root / "calibration" / "training_edges.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in calibration), "utf-8"
                )
                calibrate_stage(CONFIG, root)
                candidate = dict(games[0], **mutation)
                (root / "features" / "candidate_components.jsonl").write_text(
                    json.dumps(candidate) + "\n", "utf-8"
                )
                with self.assertRaisesRegex(ValueError, message):
                    features_stage(CONFIG, root)

    def test_calibrate_requires_raw_schedule_provenance(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            with self.assertRaisesRegex(RuntimeError, "raw/schedule"):
                calibrate_stage(CONFIG, root)

    def test_features_require_raw_schedule_provenance(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            _write_jsonl(root / "features" / "candidate_components.jsonl", games)
            calibrate_stage(CONFIG, root)
            schedule_root = root / "raw" / "mlb_schedule"
            schedule_root.rename(root / "raw" / "removed_schedule")
            with self.assertRaisesRegex(RuntimeError, "raw/schedule"):
                features_stage(CONFIG, root)

    def test_outcomes_require_every_locked_game_outcome(self):
        outcomes = json.loads((FIXTURE / "outcomes.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_through_lock(root)
            locked = _jsonl(root / "locked" / "predictions.jsonl")
            missing = next(
                row["game_pk"] for row in locked if row["top1"] == "No"
            )
            _write_jsonl(
                root / "outcomes" / "outcomes_input.jsonl",
                [row for row in outcomes if row["game_pk"] != missing],
            )
            with self.assertRaisesRegex(ValueError, "missing locked outcomes"):
                outcomes_stage(CONFIG, root)

    def test_report_requires_every_locked_game_outcome(self):
        outcomes = json.loads((FIXTURE / "outcomes.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _prepare_through_lock(root)
            locked = _jsonl(root / "locked" / "predictions.jsonl")
            _write_jsonl(root / "outcomes" / "outcomes_input.jsonl", outcomes)
            evaluated_path = outcomes_stage(CONFIG, root)
            evaluated = _jsonl(evaluated_path)
            missing = next(
                row["game_pk"] for row in locked if row["top1"] == "No"
            )
            _write_jsonl(
                evaluated_path,
                [row for row in evaluated if row["game_pk"] != missing],
            )
            with self.assertRaisesRegex(ValueError, "missing locked outcomes"):
                report_stage(CONFIG, root)

    def test_outcomes_reject_nested_evaluated_directory_symlink(self):
        outcomes = json.loads((FIXTURE / "outcomes.json").read_text("utf-8"))
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            root = base / "data"
            outside = base / "outside"
            outside.mkdir()
            _prepare_through_lock(root)
            _write_jsonl(root / "outcomes" / "outcomes_input.jsonl", outcomes)
            (root / "outcomes" / "evaluated").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(ValueError, "symlink"):
                outcomes_stage(CONFIG, root)
            self.assertEqual(list(outside.iterdir()), [])

    def test_features_require_candidate_to_match_raw_official_schedule(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
        mutations = (
            dict(games[0], home_team="Different Club"),
            dict(
                games[0],
                game_pk=9999,
                away_team="Arbitrary Away",
                home_team="Arbitrary Home",
            ),
            dict(
                games[0],
                game_pk=9998,
                official_date="2025-08-13",
                scheduled_start_utc="2025-08-13T23:10:00Z",
            ),
        )
        for candidate in mutations:
            with self.subTest(game_pk=candidate["game_pk"]), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _copy_raw_schedule(root)
                _write_jsonl(
                    root / "calibration" / "training_edges.jsonl", calibration
                )
                _write_jsonl(
                    root / "features" / "candidate_components.jsonl", [candidate]
                )
                calibrate_stage(CONFIG, root)
                with self.assertRaisesRegex(ValueError, "official schedule|window"):
                    features_stage(CONFIG, root)

    def test_schedule_backed_candidate_after_initial_window_is_extension_eligible(self):
        calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
        template = json.loads((FIXTURE / "games.json").read_text("utf-8"))[0]
        august = dict(
            template,
            game_pk=8101,
            official_date="2025-08-13",
            scheduled_start_utc="2025-08-13T23:05:00Z",
            away_team="Seattle Mariners",
            home_team="Baltimore Orioles",
        )
        september = dict(
            template,
            game_pk=9901,
            official_date="2025-09-29",
            scheduled_start_utc="2025-09-29T23:05:00Z",
            away_team="Toronto Blue Jays",
            home_team="Tampa Bay Rays",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            _write_jsonl(root / "features" / "candidate_components.jsonl", [august])
            calibrate_stage(CONFIG, root)
            features_stage(CONFIG, root)
            self.assertEqual(
                _jsonl(root / "features" / "scored_candidates.jsonl")[0]["game_pk"],
                8101,
            )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            _write_jsonl(
                root / "features" / "candidate_components.jsonl", [september]
            )
            calibrate_stage(CONFIG, root)
            with self.assertRaisesRegex(ValueError, "window"):
                features_stage(CONFIG, root)

    def test_initial_lock_cutoff_excludes_post_august_12_without_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            calibration = json.loads(
                (FIXTURE / "calibration.json").read_text("utf-8")
            )
            games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
            games.append(
                dict(
                    games[0],
                    game_pk=8101,
                    official_date="2025-08-13",
                    scheduled_start_utc="2025-08-13T23:05:00Z",
                    away_team="Seattle Mariners",
                    home_team="Baltimore Orioles",
                )
            )
            _copy_raw_schedule(root)
            _write_jsonl(root / "calibration" / "training_edges.jsonl", calibration)
            _write_jsonl(root / "features" / "candidate_components.jsonl", games)
            calibrate_stage(CONFIG, root)
            features_stage(CONFIG, root)
            lock_stage(CONFIG, root)
            initial = _jsonl(root / "locked" / "predictions.jsonl")
            self.assertTrue(
                all(row["official_date"] <= "2025-08-12" for row in initial)
            )
            self.assertNotIn(8101, {row["game_pk"] for row in initial})

    def test_fixture_replay_extends_past_repeated_void_until_target(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._replay(Path(td))

            self.assertEqual(
                result["extension_dates"], ["2025-05-04", "2025-05-05"]
            )
            self.assertEqual(result["graded_top1"], 3)
            self.assertEqual(result["void_top1"], 2)
            self.assertEqual(result["selection_dates"], 5)

    def test_fixture_replay_schedule_exhaustion_is_explicit_and_writes_no_report(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fixture = base / "fixture"
            output = base / "output"
            shutil.copytree(FIXTURE, fixture)
            for path in sorted(
                (fixture / "extension_outcomes_by_date").glob("*.json")
            ):
                extension = json.loads(path.read_text("utf-8"))
                for row in extension:
                    row.update(actual_winner="", status="VOID")
                path.write_text(
                    json.dumps(extension, indent=2) + "\n", encoding="utf-8"
                )

            with self.assertRaisesRegex(RuntimeError, "schedule.*exhaust"):
                run_fixture_replay(fixture, output)
            self.assertFalse((output / "reports" / "summary.json").exists())

    def test_features_and_lock_do_not_require_outcomes_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _copy_raw_schedule(root)
            (root / "calibration").mkdir()
            (root / "features").mkdir()
            calibration = json.loads((FIXTURE / "calibration.json").read_text("utf-8"))
            games = json.loads((FIXTURE / "games.json").read_text("utf-8"))
            (root / "calibration" / "training_edges.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in calibration), "utf-8"
            )
            (root / "features" / "candidate_components.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in games), "utf-8"
            )
            calibrate_stage(CONFIG, root)
            features_stage(CONFIG, root)
            lock_stage(CONFIG, root)
            self.assertFalse((root / "outcomes").exists())


if __name__ == "__main__":
    unittest.main()
