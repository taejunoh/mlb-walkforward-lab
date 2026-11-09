"""Closed-input contract tests for post-start reconstructed research."""

import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from mlb_backtest.live_top3_contracts import canonical_json_bytes
from tests.test_live_features import CUTOFF, GAME_PK, TARGET, _binding, _era, _team_map


def _sealed(value: dict[str, object]) -> bytes:
    unsigned = dict(value)
    unsigned.pop("content_sha256", None)
    value["content_sha256"] = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
    return canonical_json_bytes(value)


def _pitch(*, pitcher: int, batter: int, event_id: str, top: str) -> dict[str, object]:
    return {
        "game_date": CUTOFF, "game_pk": 1, "pitcher_id": pitcher,
        "batter_id": batter, "events": "field_out", "description": "terminal",
        "p_throws": "R", "home_team": "HOM", "away_team": "AWY", "inning": 1,
        "inning_topbot": top, "release_speed": 95.0, "woba_value": 0.0,
        "woba_denom": 1.0, "estimated_woba_using_speedangle": 0.2,
        "launch_speed_angle": 2.0, "at_bat_number": int(event_id), "pitch_number": 1,
        "home_score": 0, "away_score": 0, "outs_when_up": 0, "game_type": "R",
        "event_id": event_id,
    }


def _write_fixture(root: Path) -> tuple[Path, Path, Path, Path]:
    source = {
        "schema": "POSTSTART_RESEARCH_LINEUP_SOURCE_V1", "content_sha256": "",
        "target_date": TARGET, "game_pk": GAME_PK,
        "scheduled_start_utc": "2025-05-01T23:00:00Z", "away_team": "Away",
        "home_team": "Home", "away_team_id": 110, "home_team_id": 120,
        "away_starting_nine": list(range(201, 210)), "home_starting_nine": list(range(101, 110)),
        "away_starter_id": 501, "home_starter_id": 601,
        "source_retrieved_at_utc": "2025-05-02T03:00:00Z",
    }
    source_path = root / "inputs/lineup-source.json"
    source_path.parent.mkdir(parents=True)
    source_bytes = _sealed(source)
    source_path.write_bytes(source_bytes)
    projection = {
        "schema": "POSTSTART_RESEARCH_PROJECTION_V1", "content_sha256": "",
        "target_date": TARGET, "game_pk": GAME_PK,
        "scheduled_start_utc": "2025-05-01T23:00:00Z", "away_team": "Away",
        "home_team": "Home", "away_team_id": 110, "home_team_id": 120,
        "venue_id": 33, "venue_name": "Park", "game_type": "R",
        "away_starting_nine": list(range(201, 210)), "home_starting_nine": list(range(101, 110)),
        "away_starter_id": 501, "home_starter_id": 601,
        "source_relative": "inputs/lineup-source.json",
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "reconstructed_at_utc": "2025-05-02T03:00:00Z",
    }
    pitches = [
        _pitch(pitcher=501, batter=101, event_id="1", top="Top"),
        _pitch(pitcher=601, batter=201, event_id="2", top="Bot"),
    ]
    history = {
        "schema": "POSTSTART_RESEARCH_D1_HISTORY_V1", "content_sha256": "",
        "target_date": TARGET, "cutoff_date": CUTOFF,
        "cache_authority": {"namespace": "experimental_daily/history_cache", "cutoff_date": CUTOFF, "catalog_sha256": "e" * 64},
        "source_bindings": [asdict(_binding(role, index)) for index, role in enumerate(("HISTORY_SCHEDULE", "HISTORY_STATCAST", "HISTORY_ERA", "HISTORY_VENUE"), 1)],
        "team_code_by_name": _team_map(),
        "venue_references": {"33": {"venue_name": "Park", "venue_city": "New York", "venue_latitude": 40.8296, "venue_longitude": -73.9262, "time_zone": "America/New_York"}},
        "league_era_by_asof": _era(),
        "feature_history": {
            "slot_weights": {str(slot): 1 / 9 for slot in range(1, 10)},
            "statcast_coverage_complete": True, "pitches": pitches, "plate_appearances": pitches,
            "schedule_rows": [], "schedule_coverage_start": CUTOFF,
            "schedule_coverage_end": CUTOFF, "schedule_coverage_complete": True,
        },
    }
    career = {
        "schema": "POSTSTART_RESEARCH_CAREER_BF_V1", "content_sha256": "",
        "target_date": TARGET, "as_of_date": CUTOFF, "pitcher_bf": {"501": 1234, "601": 2345},
    }
    paths = (root / "inputs/history.json", root / "inputs/projection.json", root / "inputs/card.json", root / "inputs/career.json")
    paths[0].write_bytes(_sealed(history))
    paths[1].write_bytes(_sealed(projection))
    paths[2].write_bytes((Path(__file__).parents[1] / "audit/v5_1_bt/model_card.json").read_bytes())
    paths[3].write_bytes(_sealed(career))
    return tuple(path.relative_to(root) for path in paths)


class PoststartResearchEvaluationImportTests(unittest.TestCase):
    def test_public_builder_is_available(self):
        from mlb_backtest.poststart_research_evaluation import (
            build_poststart_research_evaluation,
        )

        self.assertTrue(callable(build_poststart_research_evaluation))


class PoststartResearchEvaluationTests(unittest.TestCase):
    def test_builds_outcome_blind_reconstructed_report_from_closed_inputs(self):
        from mlb_backtest.poststart_research_evaluation import (
            STATUS_LABELS, build_poststart_research_evaluation, persist_poststart_research_evaluation,
        )

        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as temporary:
            root = Path(temporary)
            history, projection, card, career = _write_fixture(root)
            built = build_poststart_research_evaluation(
                root, history_bundle=history, participant_projection=projection,
                model_card=card, career_bf=career, evaluated_at_utc="2026-09-13T18:00:00Z",
            )
            self.assertEqual(built.report["status_labels"], STATUS_LABELS)
            self.assertEqual(built.report["lineup_provenance"], "RECONSTRUCTED_FINAL_LINEUP")
            self.assertIn("MODEL_ARTIFACT_TIME_UNPROVEN", built.report["limitations"])
            self.assertNotIn("winner", built.report)
            report_path, markdown_path, trace_path = persist_poststart_research_evaluation(root, built)
            self.assertTrue(report_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertTrue(trace_path.is_file())

    def test_rejects_outcome_field_before_build(self):
        from mlb_backtest.poststart_research_evaluation import build_poststart_research_evaluation

        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as temporary:
            root = Path(temporary)
            history, projection, card, career = _write_fixture(root)
            value = json.loads((root / projection).read_bytes())
            value["score"] = 1
            (root / projection).write_bytes(_sealed(value))
            with self.assertRaisesRegex(ValueError, "INVALID_PARTICIPANT_PROJECTION"):
                build_poststart_research_evaluation(
                    root, history_bundle=history, participant_projection=projection,
                    model_card=card, career_bf=career, evaluated_at_utc="2026-09-13T18:00:00Z",
                )

    def test_persistence_rebinds_a_changed_input_before_writing(self):
        from mlb_backtest.poststart_research_evaluation import (
            build_poststart_research_evaluation, persist_poststart_research_evaluation,
        )

        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as temporary:
            root = Path(temporary)
            history, projection, card, career = _write_fixture(root)
            built = build_poststart_research_evaluation(
                root, history_bundle=history, participant_projection=projection,
                model_card=card, career_bf=career, evaluated_at_utc="2026-09-13T18:00:00Z",
            )
            path = root / career
            path.write_bytes(path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "snapshot"):
                persist_poststart_research_evaluation(root, built)
            self.assertFalse((root / "reports").exists())

    def test_history_loader_rejects_fabricated_terminal_pa(self):
        from mlb_backtest.poststart_research_evaluation import _load_research_d1_history

        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as temporary:
            root = Path(temporary)
            history, _projection, _card, _career = _write_fixture(root)
            value = json.loads((root / history).read_bytes())
            value["feature_history"]["plate_appearances"][0]["event_id"] = "fabricated"
            with self.assertRaisesRegex(ValueError, "INVALID_D1_HISTORY"):
                _load_research_d1_history(_sealed(value))

    def test_rejects_career_bf_after_d1_cutoff(self):
        from mlb_backtest.poststart_research_evaluation import build_poststart_research_evaluation

        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as temporary:
            root = Path(temporary)
            history, projection, card, career = _write_fixture(root)
            value = json.loads((root / career).read_bytes())
            value["as_of_date"] = TARGET
            (root / career).write_bytes(_sealed(value))
            with self.assertRaisesRegex(ValueError, "AS_OF_VIOLATION"):
                build_poststart_research_evaluation(
                    root, history_bundle=history, participant_projection=projection,
                    model_card=card, career_bf=career, evaluated_at_utc="2026-09-13T18:00:00Z",
                )

    def test_cli_does_not_open_network_or_accept_result_receipt(self):
        from mlb_backtest.poststart_research_evaluation import main

        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1]) as temporary:
            root = Path(temporary)
            history, projection, card, career = _write_fixture(root)
            argv = ["evaluate", "--root", str(root), "--history-bundle", str(history),
                    "--participant-projection", str(projection), "--model-card", str(card),
                    "--career-bf", str(career)]
            output = io.StringIO()
            with patch("socket.socket", side_effect=AssertionError("network access")), patch(
                "urllib.request.urlopen", side_effect=AssertionError("network access")
            ), redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            paths = output.getvalue().splitlines()
            self.assertEqual(len(paths), 3)
            self.assertTrue(all(path.startswith("reports/experimental_poststart_research/") for path in paths))
            with self.assertRaises(SystemExit):
                main([*argv, "--result-receipt", "forbidden"])
