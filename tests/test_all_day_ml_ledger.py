"""Contracts for the append-only whole-slate ML evidence ledger."""

from __future__ import annotations

import json
import hashlib
import copy
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.live_top3_contracts import LiveHttpObservation, canonical_json_bytes
from mlb_backtest.prospective_ml_observation import (
    build_ml_observations,
    persist_ml_observations,
)
from mlb_backtest.prospective_ml_settlement import build_calibration_report, persist_calibration_report
from mlb_backtest.market_play_contracts import content_digest, sidecar_bytes
from mlb_backtest.multibook_market_capture import RAW_PREFIX, SOURCE_HOSTS, admit_raw_market_capture
from tests.prospective_ml_fixtures import AWAY_TEAM, GAME_PK, HOME_TEAM, sealed_ml_fixture


def _clock() -> datetime:
    return datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)


def _result_http(
    url: str,
    *,
    final: bool = True,
    game_pk: int = GAME_PK,
    away_team: str = AWAY_TEAM,
    home_team: str = HOME_TEAM,
    official_date: str = "2026-09-03",
) -> LiveHttpObservation:
    state = "Final" if final else "Preview"
    payload = canonical_json_bytes(
        {
            "dates": [{"games": [{
                "gamePk": game_pk,
                "officialDate": official_date,
                "status": {"abstractGameState": state, "detailedState": state},
                "teams": {
                    "away": {"team": {"name": away_team}, "isWinner": False},
                    "home": {"team": {"name": home_team}, "isWinner": True},
                },
            }]}],
        }
    )
    return LiveHttpObservation(
        requested_url=url,
        final_url=url,
        started_at_utc="2026-09-03T22:59:00Z",
        response_completed_at_utc="2026-09-03T23:00:00Z",
        http_status=200,
        content_type="application/json",
        etag=None,
        last_modified=None,
        body=payload,
    )


def _extend_sealed_fixture_slate(
    root: Path,
    trace: Path,
    extras: list[tuple[int, str, str, str, float]],
) -> None:
    """Add scored trace rows to the fixture's authenticated one-game slate."""
    schedule_relative = Path(
        "data/experimental_daily/live_observations/mlb_live_schedule/777001.schedule.raw"
    )
    schedule = json.loads((root / schedule_relative).read_text())
    target_date = str(schedule["dates"][0]["date"])
    base_game = copy.deepcopy(schedule["dates"][0]["games"][0])
    base_row = json.loads(
        (root / trace).read_text()
    )["games"][0]
    for index, (game_pk, away_team, home_team, start, p_home) in enumerate(extras, start=2):
        game = copy.deepcopy(base_game)
        game["gamePk"] = game_pk
        game["gameDate"] = start
        game["gameNumber"] = index
        game["teams"]["away"]["team"] = {"id": 110 + index, "name": away_team}
        game["teams"]["home"]["team"] = {"id": 120 + index, "name": home_team}
        game["teams"]["away"]["probablePitcher"] = {"id": 500 + index, "fullName": f"Away Pitcher {index}"}
        game["teams"]["home"]["probablePitcher"] = {"id": 600 + index, "fullName": f"Home Pitcher {index}"}
        schedule["dates"][0]["games"].append(game)

    schedule_raw = canonical_json_bytes(schedule)
    schedule_sha = hashlib.sha256(schedule_raw).hexdigest()
    (root / schedule_relative).write_bytes(schedule_raw)
    (root / schedule_relative.with_suffix(schedule_relative.suffix + ".sha256")).write_bytes(f"{schedule_sha}\n".encode())

    manifest_relative = Path("audit/experimental_daily/live_receipts/777001.schedule.manifest.json")
    manifest_raw = canonical_json_bytes({"payload_sha256": schedule_sha})
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    (root / manifest_relative).write_bytes(manifest_raw)
    (root / manifest_relative.with_suffix(manifest_relative.suffix + ".sha256")).write_bytes(f"{manifest_sha}\n".encode())

    receipt_relative = Path("audit/experimental_daily/live_receipts/777001.schedule.json")
    receipt = {
        "manifest_relative": manifest_relative.as_posix(),
        "manifest_sha256": manifest_sha,
        "payload_relative": schedule_relative.as_posix(),
        "payload_sha256": schedule_sha,
    }
    receipt_raw = canonical_json_bytes(receipt)
    receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
    (root / receipt_relative).write_bytes(receipt_raw)
    (root / receipt_relative.with_suffix(receipt_relative.suffix + ".sha256")).write_bytes(f"{receipt_sha}\n".encode())

    primary_relative = Path(f"reports/experimental_daily/{target_date}/sealed-primary.json")
    primary = json.loads((root / primary_relative).read_text())
    all_game_pks = [GAME_PK, *(spec[0] for spec in extras)]
    for binding in primary["source_bindings"]:
        if binding["binding_role"] != "LIVE_SCHEDULE":
            continue
        binding["game_pks"] = sorted(all_game_pks)
        binding["byte_length"] = len(schedule_raw)
        binding["payload_sha256"] = schedule_sha
        binding["manifest_sha256"] = manifest_sha
        binding["receipt_sha256"] = receipt_sha
        binding.pop("binding_sha256")
        binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes(binding)).hexdigest()
    primary["content_sha256"] = hashlib.sha256(
        canonical_json_bytes({key: value for key, value in primary.items() if key != "content_sha256"})
    ).hexdigest()
    primary_raw = canonical_json_bytes(primary)
    (root / primary_relative).write_bytes(primary_raw)
    (root / primary_relative.with_suffix(primary_relative.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(primary_raw).hexdigest()}\n".encode()
    )

    trace_value = json.loads((root / trace).read_text())
    trace_value["primary_report_content_sha256"] = primary["content_sha256"]
    for game_pk, away_team, home_team, start, p_home in extras:
        row = copy.deepcopy(base_row)
        row.update(
            {
                "game_pk": game_pk,
                "away_team": away_team,
                "home_team": home_team,
                "scheduled_start_utc": start,
                "p_home": p_home,
                "predicted_side": "home" if p_home > 0.5 else "away",
                "pick_probability": max(p_home, 1.0 - p_home),
                "disposition": "SCORED_NOT_TOP3",
            }
        )
        row["raw_features"] = {"eligible": True, "game_pk": game_pk, "official_date": target_date}
        row["feature_sha256"] = hashlib.sha256(canonical_json_bytes(row["raw_features"])).hexdigest()
        trace_value["games"].append(row)
    trace_value["content_sha256"] = hashlib.sha256(
        canonical_json_bytes({key: value for key, value in trace_value.items() if key != "content_sha256"})
    ).hexdigest()
    trace_raw = canonical_json_bytes(trace_value)
    (root / trace).write_bytes(trace_raw)
    (root / trace.with_suffix(trace.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(trace_raw).hexdigest()}\n".encode()
    )


def _admit_raw_capture(root: Path, events: list[dict[str, object]], captured_at: str) -> Path:
    host = next(iter(SOURCE_HOSTS["FANDUEL"]))
    raw: dict[str, object] = {
        "schema": "PROVISIONAL_MULTIBOOK_MARKET_RAW_CAPTURE_V1",
        "content_sha256": "",
        "source_book": "FANDUEL",
        "captured_at_utc": captured_at,
        "source_url": f"https://{host}/mlb/today",
        "transport_metadata": {"fixture": "yes"},
        "events": events,
    }
    raw["content_sha256"] = content_digest(raw)
    relative = Path(RAW_PREFIX / f"{raw['content_sha256']}.json")
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    raw_bytes = canonical_json_bytes(raw)
    (root / relative).write_bytes(raw_bytes)
    (root / relative.with_suffix(".sha256")).write_bytes(sidecar_bytes(raw_bytes))
    return admit_raw_market_capture(root, relative)


def _write_alternate_ml_version(root: Path, trace: Path) -> Path:
    """Create a second authenticated version for the same game identity."""
    trace_value = json.loads((root / trace).read_text())
    primary_relative = Path(str(trace_value["primary_report_relative"]))
    primary = json.loads((root / primary_relative).read_text())
    row = trace_value["games"][0]
    target_date = str(trace_value["games"][0]["target_date"])
    start = str(row["scheduled_start_utc"])

    # Make the first version explicitly unscored, leaving the game identity intact.
    row["p_home"] = 0.5
    row["predicted_side"] = "none"
    row["pick_probability"] = 0.5
    row["disposition"] = "UNSCORED_EXACT_NEUTRAL"
    primary["unscored_games"] = [
        {
            "game_pk": GAME_PK,
            "target_date": target_date,
            "away_team": AWAY_TEAM,
            "home_team": HOME_TEAM,
            "state": "UNSCORED",
            "scheduled_start_utc": start,
            "reason_codes": [],
            "source_binding_sha256s": [],
        }
    ]
    primary["content_sha256"] = hashlib.sha256(
        canonical_json_bytes({key: value for key, value in primary.items() if key != "content_sha256"})
    ).hexdigest()
    primary_raw = canonical_json_bytes(primary)
    (root / primary_relative).write_bytes(primary_raw)
    (root / primary_relative.with_suffix(primary_relative.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(primary_raw).hexdigest()}\n".encode()
    )
    trace_value["primary_report_content_sha256"] = primary["content_sha256"]
    trace_value["content_sha256"] = hashlib.sha256(
        canonical_json_bytes({key: value for key, value in trace_value.items() if key != "content_sha256"})
    ).hexdigest()
    trace_raw = canonical_json_bytes(trace_value)
    (root / trace).write_bytes(trace_raw)
    (root / trace.with_suffix(trace.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(trace_raw).hexdigest()}\n".encode()
    )

    # Copy the participant receipt chain to distinct paths so authority refs differ.
    original_binding = next(
        binding for binding in primary["source_bindings"] if binding["binding_role"] == "LIVE_PARTICIPANTS"
    )
    old_payload = Path(str(original_binding["payload_relative"]))
    old_manifest = Path(str(original_binding["receipt_relative"])).with_name(
        Path(str(original_binding["receipt_relative"])).name.replace(".json", ".manifest.json")
    )
    old_receipt = Path(str(original_binding["receipt_relative"]))
    new_payload = old_payload.with_name("777001.participants.alt.raw")
    new_manifest = old_manifest.with_name("777001.participants.alt.manifest.json")
    new_receipt = old_receipt.with_name("777001.participants.alt.json")
    payload_raw = (root / old_payload).read_bytes()
    (root / new_payload).write_bytes(payload_raw)
    (root / new_payload.with_suffix(new_payload.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(payload_raw).hexdigest()}\n".encode()
    )
    manifest_raw = canonical_json_bytes({"payload_sha256": hashlib.sha256(payload_raw).hexdigest()})
    manifest_sha = hashlib.sha256(manifest_raw).hexdigest()
    (root / new_manifest).write_bytes(manifest_raw)
    (root / new_manifest.with_suffix(new_manifest.suffix + ".sha256")).write_bytes(f"{manifest_sha}\n".encode())
    receipt = {
        "manifest_relative": new_manifest.as_posix(),
        "manifest_sha256": manifest_sha,
        "payload_relative": new_payload.as_posix(),
        "payload_sha256": hashlib.sha256(payload_raw).hexdigest(),
    }
    receipt_raw = canonical_json_bytes(receipt)
    receipt_sha = hashlib.sha256(receipt_raw).hexdigest()
    (root / new_receipt).write_bytes(receipt_raw)
    (root / new_receipt.with_suffix(new_receipt.suffix + ".sha256")).write_bytes(f"{receipt_sha}\n".encode())

    alternate_primary = copy.deepcopy(primary)
    alternate_primary["model_card_sha256"] = "c" * 64
    alternate_primary["unscored_games"] = []
    alternate_primary["executed_at_utc"] = "2026-09-03T19:50:00Z"
    alternate_primary["executed_at_eastern"] = "2026-09-03T15:50:00-04:00"
    alternate_binding = next(
        binding for binding in alternate_primary["source_bindings"] if binding["binding_role"] == "LIVE_PARTICIPANTS"
    )
    alternate_binding.update(
        {
            "observation_id": "participants-alt-777001",
            "query_identity_json": canonical_json_bytes(
                {"observation_id": "participants-alt-777001", "url": "https://statsapi.mlb.com/api/v1/game/777001/boxscore"}
            ).decode().removesuffix("\n"),
            "payload_relative": new_payload.as_posix(),
            "receipt_relative": new_receipt.as_posix(),
            "receipt_sha256": receipt_sha,
            "manifest_sha256": manifest_sha,
        }
    )
    alternate_binding.pop("binding_sha256")
    alternate_binding["binding_sha256"] = hashlib.sha256(canonical_json_bytes(alternate_binding)).hexdigest()
    alternate_primary["content_sha256"] = hashlib.sha256(
        canonical_json_bytes({key: value for key, value in alternate_primary.items() if key != "content_sha256"})
    ).hexdigest()
    alternate_primary_relative = Path(f"reports/experimental_daily/{target_date}/alternate-primary.json")
    alternate_primary_raw = canonical_json_bytes(alternate_primary)
    (root / alternate_primary_relative).write_bytes(alternate_primary_raw)
    (root / alternate_primary_relative.with_suffix(alternate_primary_relative.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(alternate_primary_raw).hexdigest()}\n".encode()
    )

    alternate_row = copy.deepcopy(row)
    alternate_row.update(
        {
            "model_card_sha256": "c" * 64,
            "p_home": 0.3,
            "predicted_side": "away",
            "pick_probability": 0.7,
            "disposition": "SCORED_NOT_TOP3",
        }
    )
    alternate_trace = {
        **trace_value,
        "primary_report_relative": alternate_primary_relative.as_posix(),
        "primary_report_content_sha256": alternate_primary["content_sha256"],
        "model_card_sha256": "c" * 64,
        "games": [alternate_row],
        "content_sha256": "",
    }
    alternate_trace["content_sha256"] = hashlib.sha256(
        canonical_json_bytes({key: value for key, value in alternate_trace.items() if key != "content_sha256"})
    ).hexdigest()
    alternate_trace_relative = Path(f"reports/experimental_daily/{target_date}/alternate.trace.json")
    alternate_trace_raw = canonical_json_bytes(alternate_trace)
    (root / alternate_trace_relative).write_bytes(alternate_trace_raw)
    (root / alternate_trace_relative.with_suffix(alternate_trace_relative.suffix + ".sha256")).write_bytes(
        f"{hashlib.sha256(alternate_trace_raw).hexdigest()}\n".encode()
    )
    return alternate_trace_relative


def _write_capture_selector(root: Path, outcomes: list[Path], target_date: str) -> Path:
    selector = Path(f"audit/market_observations/{target_date}/selector.json")
    (root / selector).parent.mkdir(parents=True, exist_ok=True)
    (root / selector).write_bytes(
        canonical_json_bytes(
            {
                "type": "PROVISIONAL_MULTIBOOK_MARKET_ARTIFACT_SUPERSESSION_V1",
                "target_date": target_date,
                "source_verification_relative": f"audit/market_observations/{target_date}/verification.txt",
                "active_capture_outcomes": [outcome.as_posix() for outcome in outcomes],
                "do_not_use": [],
            }
        )
    )
    (root / f"audit/market_observations/{target_date}/verification.txt").write_text("fixture", encoding="utf-8")
    return selector


class AllDayMlLedgerTests(unittest.TestCase):
    def test_whole_slate_selects_eligible_unranked_pregame_trace(self) -> None:
        """SCORED_NOT_TOP3 is a historical forecast, not an unscored omission."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            calls: list[str] = []

            def fetcher(_source: str, url: str, **_: object) -> LiveHttpObservation:
                calls.append(url)
                return _result_http(url)

            collected = collect_daily_ml_ledger(
                root, ml_traces=[trace], fetcher=fetcher, durable_clock=_clock
            )

            self.assertEqual(len(calls), 1)
            payload = json.loads((root / collected.json_relative).read_text())
            row = payload["games"][0]
            self.assertEqual(row["game_pk"], GAME_PK)
            self.assertEqual(row["evaluation_class"], "HISTORICAL_SCORED_TRACE_PREGAME")
            self.assertEqual(row["selected_forecast"]["linkage"], "HISTORICAL_SCORED_TRACE_PREGAME")
            self.assertEqual(row["diagnostic_trace_p_home"], "0.7")
            self.assertEqual(row["result"]["state"], "FINAL")
            self.assertEqual(row["result"]["forecast_outcome"], "WIN")

    def test_authenticated_primary_is_selected_before_outcome_read(self) -> None:
        """A valid claimed snapshot is exact primary linkage, not a backdated label."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            built = build_ml_observations(root, trace, observed_clock="2026-09-03T19:45:00Z")
            persisted = persist_ml_observations(
                root, built, primary_clock=lambda: "2026-09-03T19:50:00Z"
            )
            report = persist_calibration_report(
                root, build_calibration_report(root, persisted.primary_claim_paths, [])
            )
            collected = collect_daily_ml_ledger(
                root, ml_traces=[trace], prospective_reports=[report],
                fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock,
            )

            payload = json.loads((root / collected.json_relative).read_text())
            forecast = payload["games"][0]["selected_forecast"]
            self.assertEqual(forecast["linkage"], "EXACT_PROSPECTIVE_PRIMARY")
            self.assertEqual(forecast["snapshot_relative"], persisted.primary_candidate_paths[0].as_posix())
            self.assertEqual(payload["games"][0]["result"]["forecast_outcome"], "WIN")

    def test_trace_input_order_does_not_change_historical_selection(self) -> None:
        """Tie-breaking must use authenticated timestamps/hash/path, never argv order."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            alternate = trace.with_name("alternate.trace.json")
            (root / alternate).write_bytes((root / trace).read_bytes())
            (root / alternate.with_suffix(".json.sha256")).write_bytes((root / trace.with_suffix(".json.sha256")).read_bytes())
            first = collect_daily_ml_ledger(root, ml_traces=[trace, alternate], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            second = collect_daily_ml_ledger(root, ml_traces=[alternate, trace], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            one = json.loads((root / first.json_relative).read_text())["games"][0]["selected_forecast"]
            two = json.loads((root / second.json_relative).read_text())["games"][0]["selected_forecast"]
            self.assertEqual(one, two)
            self.assertEqual(len(json.loads((root / first.json_relative).read_text())["games"][0]["prediction_versions"]), 2)

    def test_invalid_explicit_source_rejects_before_http(self) -> None:
        """Authentication must complete before the bounded result transport starts."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            (root / trace).write_text("{}", encoding="utf-8")
            calls = 0

            def fetcher(_source: str, _url: str, **_: object) -> LiveHttpObservation:
                nonlocal calls
                calls += 1
                raise AssertionError("must not fetch")

            with self.assertRaises(ValueError):
                collect_daily_ml_ledger(root, ml_traces=[trace], fetcher=fetcher, durable_clock=_clock)
            self.assertEqual(calls, 0)

    def test_post_start_scored_trace_is_visible_diagnostic_not_evaluable(self) -> None:
        """An otherwise scored trace at exactly start must never become a forecast."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            trace_value = json.loads((root / trace).read_text())
            report = Path(trace_value["primary_report_relative"])
            report_value = json.loads((root / report).read_text())
            report_value["executed_at_utc"] = "2026-09-03T20:00:00Z"
            report_value["executed_at_eastern"] = "2026-09-03T16:00:00-04:00"
            report_value["content_sha256"] = hashlib.sha256(canonical_json_bytes({key: value for key, value in report_value.items() if key != "content_sha256"})).hexdigest()
            report_raw = canonical_json_bytes(report_value)
            (root / report).write_bytes(report_raw)
            (root / report.with_suffix(".json.sha256")).write_bytes(f"{hashlib.sha256(report_raw).hexdigest()}\n".encode())
            trace_value["primary_report_content_sha256"] = report_value["content_sha256"]
            trace_value["content_sha256"] = hashlib.sha256(canonical_json_bytes({key: value for key, value in trace_value.items() if key != "content_sha256"})).hexdigest()
            trace_raw = canonical_json_bytes(trace_value)
            (root / trace).write_bytes(trace_raw)
            (root / trace.with_suffix(".json.sha256")).write_bytes(f"{hashlib.sha256(trace_raw).hexdigest()}\n".encode())
            result = collect_daily_ml_ledger(root, ml_traces=[trace], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            row = json.loads((root / result.json_relative).read_text())["games"][0]
            self.assertEqual(row["evaluation_class"], "POST_START_TRACE_DIAGNOSTIC")
            self.assertIsNone(row["selected_forecast"])
            self.assertIn("POST_START_TRACE", row["prediction_versions"][0]["reason_codes"])

    def test_supplied_receipt_is_read_after_selection_freeze(self) -> None:
        """Outcome parsing cannot decide which frozen prediction is selected."""
        from mlb_backtest.live_sources import _persist_observation
        from mlb_backtest.prospective_ml_settlement import load_official_result_evidence
        import mlb_backtest.all_day_ml_ledger as ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-09-03&gamePks=777001"
            persisted = _persist_observation(root, raw_source="mlb_official_result", source="stats_api", binding_role="FINAL_SCHEDULE", game_pks=(GAME_PK,), observation_id="a" * 32, query={"game_pk": GAME_PK, "kind": "prospective_ml_official_result", "observation_id": "a" * 32, "url": url}, http=_result_http(url), durable_clock=_clock)
            order: list[str] = []
            real_market = ledger._market_for

            def market_then_record(*args, **kwargs):
                order.append("selection")
                return real_market(*args, **kwargs)

            def receipt_then_record(*args, **kwargs):
                order.append("receipt")
                return load_official_result_evidence(*args, **kwargs)

            with patch.object(ledger, "_market_for", side_effect=market_then_record), patch.object(ledger, "load_official_result_evidence", side_effect=receipt_then_record):
                ledger.collect_daily_ml_ledger(root, ml_traces=[trace], result_receipts={GAME_PK: persisted.source_binding.receipt_relative}, fetcher=lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("must not fetch")), durable_clock=_clock)
            self.assertLess(order.index("selection"), order.index("receipt"))

    def test_nonfinal_and_identity_failure_remain_visible_without_fabricated_loss(self) -> None:
        """Only a matching Final result may become a scored forecast outcome."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            pending = collect_daily_ml_ledger(
                root, ml_traces=[trace],
                fetcher=lambda _source, url, **_: _result_http(url, final=False), durable_clock=_clock,
            )
            pending_row = json.loads((root / pending.json_relative).read_text())["games"][0]
            self.assertEqual(pending_row["result"]["state"], "PENDING")
            self.assertIsNone(pending_row["result"]["forecast_outcome"])

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            wrong = _result_http("https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-09-03&gamePks=777001")
            value = json.loads(wrong.body)
            value["dates"][0]["games"][0]["teams"]["home"]["team"]["name"] = "Wrong Club"
            wrong = replace(wrong, body=canonical_json_bytes(value))
            result = collect_daily_ml_ledger(root, ml_traces=[trace], fetcher=lambda *_args, **_kw: wrong, durable_clock=_clock)
            payload = json.loads((root / result.json_relative).read_text())
            row = payload["games"][0]
            self.assertEqual(row["result"]["state"], "FAILED")
            self.assertIsNone(row["result"]["forecast_outcome"])

    def test_repeat_preserves_inputs_and_markdown_failure_exposes_json_recovery(self) -> None:
        """A later report-write failure must not erase an already durable JSON ledger."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger
        import mlb_backtest.all_day_ml_ledger as ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            original = (root / trace).read_bytes()
            first = collect_daily_ml_ledger(root, ml_traces=[trace], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            second = collect_daily_ml_ledger(root, ml_traces=[trace], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            self.assertEqual((root / trace).read_bytes(), original)
            self.assertTrue((root / first.json_relative).exists())
            self.assertTrue((root / second.json_relative).exists())

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            real_write = ledger.write_exclusive

            def fail_markdown(trusted, relative, raw, label, **kwargs):
                if str(relative).endswith(".md"):
                    raise OSError("simulated markdown disk failure")
                return real_write(trusted, relative, raw, label, **kwargs)

            with patch.object(ledger, "write_exclusive", side_effect=fail_markdown):
                result = collect_daily_ml_ledger(root, ml_traces=[trace], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            self.assertFalse(result.ok)
            self.assertIsNotNone(result.json_relative)
            self.assertTrue((root / result.json_relative).exists())

    def test_full_slate_keeps_rows_and_durable_receipts_after_one_result_failure(self) -> None:
        """One bounded result failure must not discard later successful games."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        specs = [
            (777002, "Away Club 2", "Home Club 2", "2026-09-03T21:00:00Z", 0.6),
            (777003, "Away Club 3", "Home Club 3", "2026-09-03T22:00:00Z", 0.65),
        ]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            _extend_sealed_fixture_slate(root, trace, specs)
            calls: list[int] = []

            def fetcher(_source: str, url: str, **_: object) -> LiveHttpObservation:
                game_pk = int(url.rsplit("=", 1)[1])
                calls.append(game_pk)
                if game_pk == 777002:
                    raise OSError("simulated result transport failure")
                away = AWAY_TEAM if game_pk == GAME_PK else f"Away Club {game_pk - 777000}"
                home = HOME_TEAM if game_pk == GAME_PK else f"Home Club {game_pk - 777000}"
                return _result_http(url, game_pk=game_pk, away_team=away, home_team=home)

            collected = collect_daily_ml_ledger(
                root, ml_traces=[trace], fetcher=fetcher, durable_clock=_clock
            )
            self.assertEqual(calls, [GAME_PK, 777002, 777003])
            self.assertFalse(collected.ok)
            self.assertTrue(collected.json_relative is not None)
            self.assertTrue((root / collected.json_relative).exists())
            payload = json.loads((root / collected.json_relative).read_text())
            self.assertEqual([row["game_pk"] for row in payload["games"]], [GAME_PK, 777002, 777003])
            self.assertEqual([row["result"]["state"] for row in payload["games"]], ["FINAL", "FAILED", "FINAL"])
            self.assertEqual(payload["counts"]["all_games"], 3)
            self.assertEqual(payload["counts"]["result_error_count"], 1)
            self.assertEqual(collected.pending_count, payload["counts"]["pending_results"])
            self.assertEqual(collected.pending_count, 0)
            self.assertEqual(payload["games"][0]["result"]["forecast_outcome"], "WIN")
            self.assertEqual(payload["games"][2]["result"]["forecast_outcome"], "WIN")
            for row in (payload["games"][0], payload["games"][2]):
                receipt = row["result"]["receipt_relative"]
                self.assertIsInstance(receipt, str)
                self.assertTrue((root / receipt).exists())
            self.assertTrue(any("777002: simulated result transport failure" in failure for failure in collected.failures))

    def test_ambiguous_and_post_start_raw_captures_remain_history_only(self) -> None:
        """Raw capture matching is strict, and post-start evidence is retained but rejected."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        specs = [
            (777002, AWAY_TEAM, HOME_TEAM, "2026-09-02T20:00:30Z", 0.6),
            (777003, "Away Club 3", "Home Club 3", "2026-09-02T21:00:00Z", 0.65),
        ]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root, start="2026-09-02T20:00:00Z")
            _extend_sealed_fixture_slate(root, trace, specs)
            ambiguous_event = {
                "book_event_id": "fd-ambiguous",
                "away_team": AWAY_TEAM,
                "home_team": HOME_TEAM,
                "scheduled_start_utc": "2026-09-02T20:00:15Z",
                "moneyline": {"away_american_odds": 120, "home_american_odds": -140},
                "totals": [],
            }
            post_start_event = {
                "book_event_id": "fd-post-start",
                "away_team": "Away Club 3",
                "home_team": "Home Club 3",
                "scheduled_start_utc": "2026-09-02T21:00:00Z",
                "moneyline": {"away_american_odds": 130, "home_american_odds": -150},
                "totals": [],
            }
            outcome = _admit_raw_capture(
                root, [ambiguous_event, post_start_event], "2026-09-02T21:05:00Z"
            )
            selector = _write_capture_selector(root, [outcome], "2026-09-02")

            def fetcher(_source: str, url: str, **_: object) -> LiveHttpObservation:
                game_pk = int(url.rsplit("=", 1)[1])
                if game_pk == GAME_PK:
                    return _result_http(url, official_date="2026-09-02")
                return _result_http(
                    url,
                    game_pk=game_pk,
                    away_team=f"Away Club {game_pk - 777000}",
                    home_team=f"Home Club {game_pk - 777000}",
                    official_date="2026-09-02",
                )

            result = collect_daily_ml_ledger(
                root,
                ml_traces=[trace],
                market_capture_outcomes=[outcome],
                market_supersession=selector,
                fetcher=fetcher,
                durable_clock=_clock,
            )
            payload = json.loads((root / result.json_relative).read_text())
            self.assertEqual(payload["market_capture_unmapped"][0]["reason"], "MARKET_EVENT_AMBIGUOUS")
            rows = {row["game_pk"]: row for row in payload["games"]}
            self.assertEqual(rows[GAME_PK]["market_capture_versions"], [])
            post_start = rows[777003]["market_capture_versions"]
            self.assertEqual(len(post_start), 2)
            self.assertTrue(all(not quote["prestart"] for quote in post_start))
            self.assertTrue(all(quote["rejection_reason"] == "POST_START_MARKET_EVIDENCE" for quote in post_start))
            self.assertIsNone(rows[777003]["market"])

    def test_same_game_trace_versions_retain_metadata_and_select_latest_pregame(self) -> None:
        """Independent versions may differ in model, status, and participant refs."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            alternate = _write_alternate_ml_version(root, trace)
            result = collect_daily_ml_ledger(
                root,
                ml_traces=[trace, alternate],
                fetcher=lambda _source, url, **_: _result_http(url),
                durable_clock=_clock,
            )
            payload = json.loads((root / result.json_relative).read_text())
            row = payload["games"][0]
            versions = row["prediction_versions"]
            self.assertEqual(len(versions), 2)
            self.assertEqual(row["selected_forecast"]["p_home"], "0.3")
            self.assertEqual(row["selected_forecast"]["predicted_side"], "away")
            self.assertEqual(row["model_card_sha256"], "c" * 64)
            self.assertEqual([version["model_card_sha256"] for version in versions], ["a" * 64, "c" * 64])
            self.assertEqual(versions[0]["state"], "UNSCORED_EXACT_NEUTRAL")
            self.assertEqual(versions[1]["state"], "SCORED_NOT_TOP3")
            self.assertEqual(versions[0]["trace_relative"], trace.as_posix())
            self.assertEqual(versions[1]["trace_relative"], alternate.as_posix())

    def test_producer_trace_without_sidecar_is_admitted_but_tampered_sidecar_rejects(self) -> None:
        """Producer traces may omit a sidecar, while a present bad sidecar remains fatal."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root)
            trace_sidecar = trace.with_suffix(trace.suffix + ".sha256")
            (root / trace_sidecar).unlink()
            collected = collect_daily_ml_ledger(
                root, ml_traces=[trace], fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock
            )
            self.assertTrue(collected.ok)
            (root / trace_sidecar).write_bytes(b"0" * 64 + b"\n")
            with self.assertRaises(ValueError):
                collect_daily_ml_ledger(
                    root,
                    ml_traces=[trace],
                    fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no HTTP")),
                    durable_clock=_clock,
                )

    def test_capture_outcome_maps_plus_sixty_seconds_and_rejects_superseded_input(self) -> None:
        """Raw odds remain market-only evidence and the selector is authoritative."""
        from mlb_backtest.all_day_ml_ledger import collect_daily_ml_ledger

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            trace = sealed_ml_fixture(root, start="2026-09-02T20:00:00Z")
            host = next(iter(SOURCE_HOSTS["FANDUEL"]))
            raw = {
                "schema": "PROVISIONAL_MULTIBOOK_MARKET_RAW_CAPTURE_V1", "content_sha256": "",
                "source_book": "FANDUEL", "captured_at_utc": "2026-09-02T00:00:00Z",
                "source_url": f"https://{host}/mlb/today", "transport_metadata": {"fixture": "yes"},
                "events": [{"book_event_id": "fd-777001", "away_team": AWAY_TEAM, "home_team": HOME_TEAM,
                            "scheduled_start_utc": "2026-09-02T20:01:00Z",
                            "moneyline": {"away_american_odds": 120, "home_american_odds": -140}, "totals": []}],
            }
            raw["content_sha256"] = content_digest(raw)
            raw_relative = Path(RAW_PREFIX / f"{raw['content_sha256']}.json")
            (root / raw_relative).parent.mkdir(parents=True)
            raw_bytes = canonical_json_bytes(raw)
            (root / raw_relative).write_bytes(raw_bytes)
            (root / raw_relative.with_suffix(".sha256")).write_bytes(sidecar_bytes(raw_bytes))
            outcome = admit_raw_market_capture(root, raw_relative)
            with self.assertRaises(ValueError):
                collect_daily_ml_ledger(
                    root, ml_traces=[trace], market_capture_outcomes=[outcome],
                    fetcher=lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("no HTTP")), durable_clock=_clock,
                )
            selector = Path("audit/market_observations/2026-09-02/selector.json")
            (root / selector).parent.mkdir(parents=True, exist_ok=True)
            (root / selector).write_bytes(canonical_json_bytes({"type": "PROVISIONAL_MULTIBOOK_MARKET_ARTIFACT_SUPERSESSION_V1", "target_date": "2026-09-02", "source_verification_relative": "audit/market_observations/2026-09-02/verification.txt", "active_capture_outcomes": [outcome.as_posix()], "do_not_use": []}))
            (root / "audit/market_observations/2026-09-02/verification.txt").write_text("fixture", encoding="utf-8")
            result = collect_daily_ml_ledger(root, ml_traces=[trace], market_capture_outcomes=[outcome], market_supersession=selector, fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            payload = json.loads((root / result.json_relative).read_text())
            row = payload["games"][0]
            self.assertEqual(len(row["market_capture_versions"]), 2)
            self.assertEqual(row["market_capture_versions"][0]["start_delta_seconds"], 60)
            self.assertIsNone(row["market"])
            self.assertEqual(payload["market_capture_policy"]["relative"], selector.as_posix())
            self.assertEqual(payload["counts"]["result_error_count"], 1)
            too_late = dict(raw)
            too_late["events"] = [dict(raw["events"][0])]
            too_late["events"][0]["scheduled_start_utc"] = "2026-09-02T20:02:00Z"
            too_late["content_sha256"] = ""
            too_late["content_sha256"] = content_digest(too_late)
            late_relative = Path(RAW_PREFIX / f"{too_late['content_sha256']}.json")
            late_bytes = canonical_json_bytes(too_late)
            (root / late_relative).write_bytes(late_bytes)
            (root / late_relative.with_suffix(".sha256")).write_bytes(sidecar_bytes(late_bytes))
            late_outcome = admit_raw_market_capture(root, late_relative)
            selector_value = json.loads((root / selector).read_text())
            selector_value["active_capture_outcomes"].append(late_outcome.as_posix())
            (root / selector).write_bytes(canonical_json_bytes(selector_value))
            unmapped = collect_daily_ml_ledger(root, ml_traces=[trace], market_capture_outcomes=[outcome, late_outcome], market_supersession=selector, fetcher=lambda _source, url, **_: _result_http(url), durable_clock=_clock)
            unmapped_payload = json.loads((root / unmapped.json_relative).read_text())
            self.assertEqual(unmapped_payload["market_capture_unmapped"][0]["reason"], "MARKET_EVENT_UNMAPPED")
            selector_value["do_not_use"] = [{"raw_capture_relative": raw_relative.as_posix(), "capture_outcome_relative": "not-used", "reason": "SUPERSEDED_WRONG_UNITS", "detail": "fixture"}]
            (root / selector).write_bytes(canonical_json_bytes(selector_value))
            with self.assertRaises(ValueError):
                collect_daily_ml_ledger(root, ml_traces=[trace], market_capture_outcomes=[outcome], market_supersession=selector, fetcher=lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("no HTTP")), durable_clock=_clock)
            selector_value = json.loads((root / selector).read_text())
            selector_value["active_capture_outcomes"] = []
            (root / selector).write_bytes(canonical_json_bytes(selector_value))
            with self.assertRaises(ValueError):
                collect_daily_ml_ledger(root, ml_traces=[trace], market_capture_outcomes=[outcome], market_supersession=selector, fetcher=lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("no HTTP")), durable_clock=_clock)
