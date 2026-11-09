from pathlib import Path
from datetime import datetime, timezone
import json
import shutil
import hashlib
from types import SimpleNamespace

import pytest

from mlb_backtest.live_top3_contracts import LiveHttpObservation, canonical_json_bytes
from mlb_backtest.market_play_contracts import canonical_json_bytes as receipt_json_bytes
from mlb_backtest.prospective_ml_daily_flow import normalize_root
from mlb_backtest.prospective_ml_daily_flow import read_status, run_pregame, run_postgame
import mlb_backtest.prospective_ml_daily_flow as daily_flow
from mlb_backtest.live_top3_trace import build_trace_payload
from mlb_backtest.scoring import WEIGHTS
from mlb_backtest.scoring_prep import score_raw_game
from mlb_backtest.prospective_ml_shadow import load_frozen_shadow_profile
from mlb_backtest.prospective_ml_settlement import build_calibration_report, persist_calibration_report
from tests.prospective_ml_fixtures import sealed_ml_fixture
from tests.test_prospective_ml_shadow import _raw_game
from tests.test_multibook_market_capture import raw_capture
from mlb_backtest.multibook_market_capture import RAW_PREFIX
from mlb_backtest.market_play_contracts import sidecar_bytes


_PROFILE = Path("reports/model_readiness/2026-09-02/temporal_calibration/prepare-corrected/prepare_receipt.json")


def _copy_profile(root: Path) -> None:
    for relative in (_PROFILE, _PROFILE.with_suffix(".json.sha256")):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path.cwd() / relative, destination)


def _shadow_ready_pregame(root: Path, *, clock: str = "2026-09-03T19:45:00Z"):
    _copy_profile(root)
    trace = sealed_ml_fixture(root)
    trace_path = root / trace
    original = json.loads(trace_path.read_text())
    raw = _raw_game()
    profile = load_frozen_shadow_profile(root)
    scored = score_raw_game(raw, profile.normalization)
    row = original["games"][0]
    row.update({
        "raw_features": raw,
        "feature_sha256": hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
        "away_scores": scored["away_scores"],
        "home_scores": scored["home_scores"],
        "qualities": scored["qualities"],
        "contributions": {name: WEIGHTS[name] * (scored["home_scores"][name] - scored["away_scores"][name]) for name in WEIGHTS},
        "total_edge": scored["edge"],
        "edge": scored["edge"],
    })
    rebuilt = build_trace_payload(
        primary_report_relative=original["primary_report_relative"],
        primary_report_content_sha256=original["primary_report_content_sha256"],
        model_card_sha256=original["model_card_sha256"],
        games=[row],
    )
    raw_trace = canonical_json_bytes(rebuilt)
    trace_path.write_bytes(raw_trace)
    trace_path.with_suffix(trace_path.suffix + ".sha256").write_text(hashlib.sha256(raw_trace).hexdigest() + "\n")
    return run_pregame(root, trace, clock=lambda: clock)


def _second_model_run(root: Path):
    """Create a valid same-game run with a distinct model/trace identity."""
    first_trace = root / "reports/experimental_daily/2026-09-03/sealed.trace.json"
    first = json.loads(first_trace.read_text())
    primary = json.loads((root / first["primary_report_relative"]).read_text())
    primary["model_card_sha256"] = "c" * 64
    primary["content_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in primary.items() if k != "content_sha256"})).hexdigest()
    primary_relative = Path("reports/experimental_daily/2026-09-03/sealed-secondary-primary.json")
    (root / primary_relative).write_bytes(canonical_json_bytes(primary))
    (root / primary_relative.with_suffix(".json.sha256")).write_text(hashlib.sha256(canonical_json_bytes(primary)).hexdigest() + "\n")
    row = dict(first["games"][0])
    row["model_card_sha256"] = "c" * 64
    trace = build_trace_payload(primary_report_relative=primary_relative.as_posix(), primary_report_content_sha256=primary["content_sha256"], model_card_sha256="c" * 64, games=[row])
    secondary_relative = Path("reports/experimental_daily/2026-09-03/sealed-secondary.trace.json")
    secondary_raw = canonical_json_bytes(trace)
    (root / secondary_relative).write_bytes(secondary_raw)
    (root / secondary_relative.with_suffix(".json.sha256")).write_text(hashlib.sha256(secondary_raw).hexdigest() + "\n")
    return run_pregame(root, secondary_relative, clock=lambda: "2026-09-03T19:46:00Z")


def _final_http() -> tuple[bytes, callable]:
    payload = canonical_json_bytes({"dates": [{"games": [{"gamePk": 777001, "officialDate": "2026-09-03", "status": {"abstractGameState": "Final", "detailedState": "Final"}, "teams": {"away": {"team": {"name": "Away Club"}, "isWinner": False}, "home": {"team": {"name": "Home Club"}, "isWinner": True}}}]}]})

    def fetcher(_source: str, url: str, **_kwargs: object) -> LiveHttpObservation:
        return LiveHttpObservation(url, url, "2026-09-03T22:59:00Z", "2026-09-03T23:00:00Z", 200, "application/json", None, None, payload)

    return payload, fetcher


def test_normalize_root_returns_absolute_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert normalize_root(".") == tmp_path.resolve()


def test_pregame_records_all_games_and_status_is_read_only(tmp_path: Path) -> None:
    trace = sealed_ml_fixture(tmp_path)
    result = run_pregame(tmp_path, trace, clock=lambda: "2026-09-03T19:45:00Z")
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    status = read_status(tmp_path, result.receipt_relative, clock=lambda: "2026-09-03T19:50:00Z")
    after = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    assert before == after
    assert status["workflow_processed"] is False
    assert status["games"][0]["game_pk"] == 777001
    assert "ODDS_MISSING" in status["games"][0]["missing_steps"]


def test_postgame_uses_explicit_claims_and_persists_terminal_result(tmp_path: Path) -> None:
    pregame = _shadow_ready_pregame(tmp_path)
    _payload, fetcher = _final_http()
    result = run_postgame(tmp_path, pregame.receipt_relative, fetcher=fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    assert result.receipt["phase"] == "POSTGAME"
    assert result.receipt["settlement_heads"]
    assert pregame.receipt["shadow_attachments"]
    assert result.receipt["shadow_report_ref"]
    status = read_status(tmp_path, pregame.receipt_relative, postgame_receipt=result.receipt_relative, clock=lambda: "2026-09-04T00:00:00Z")
    assert status["workflow_processed"] is True
    assert "RESULT_LINK_MISSING/PENDING" not in status["games"][0]["missing_steps"]
    assert "SHADOW_PAIR_PENDING" not in status["games"][0]["missing_steps"]


def test_missing_paired_shadow_report_keeps_shadow_pair_pending(tmp_path: Path) -> None:
    pregame = _shadow_ready_pregame(tmp_path)
    _payload, fetcher = _final_http()
    with pytest.MonkeyPatch.context() as patch:
        def fail_shadow_report(*_args: object, **_kwargs: object) -> object:
            raise ValueError("fixture shadow report failure")

        patch.setattr(daily_flow, "persist_shadow_paired_report", fail_shadow_report)
        postgame = run_postgame(tmp_path, pregame.receipt_relative, fetcher=fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    assert postgame.receipt["shadow_report_ref"] is None
    status = read_status(tmp_path, pregame.receipt_relative, postgame_receipt=postgame.receipt_relative, clock=lambda: "2026-09-04T00:00:00Z")
    assert "SHADOW_PAIR_PENDING" in status["games"][0]["missing_steps"]


def test_final_resume_carries_head_and_does_not_fetch_again(tmp_path: Path) -> None:
    pregame = _shadow_ready_pregame(tmp_path)
    _payload, fetcher = _final_http()
    first = run_postgame(tmp_path, pregame.receipt_relative, fetcher=fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    calls = 0

    def must_not_fetch(*_args: object, **_kwargs: object) -> LiveHttpObservation:
        nonlocal calls
        calls += 1
        raise AssertionError("FINAL result must not be fetched again")

    second = run_postgame(tmp_path, pregame.receipt_relative, previous_postgame_receipt=first.receipt_relative, fetcher=must_not_fetch, durable_clock=lambda: datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc))
    assert calls == 0
    assert second.receipt["settlement_heads"] == first.receipt["settlement_heads"]


def test_equal_and_after_start_preserve_candidate_without_primary_claim(tmp_path: Path) -> None:
    equal = run_pregame(tmp_path / "equal", sealed_ml_fixture(tmp_path / "equal"), clock=lambda: "2026-09-03T20:00:00Z")
    assert equal.receipt["primary_claims"] == []
    assert equal.receipt["observation_candidates"]
    later = run_pregame(tmp_path / "later", sealed_ml_fixture(tmp_path / "later"), clock=lambda: "2026-09-03T20:00:01Z")
    assert later.receipt["primary_claims"] == []
    assert "AFTER_START_IMPOSSIBLE" in read_status(tmp_path / "later", later.receipt_relative, clock=lambda: "2026-09-03T20:01:00Z")["games"][0]["missing_steps"]


def test_mid_processing_start_keeps_diagnostic_candidate_and_partial_receipt(tmp_path: Path) -> None:
    moments = iter(("2026-09-03T19:45:00Z", "2026-09-03T20:00:01Z"))
    result = run_pregame(tmp_path / "mid", sealed_ml_fixture(tmp_path / "mid"), clock=lambda: next(moments))
    assert result.receipt["observation_candidates"]
    assert result.receipt["primary_claims"] == []
    assert "AFTER_START_IMPOSSIBLE" in read_status(tmp_path / "mid", result.receipt_relative, clock=lambda: "2026-09-03T20:01:00Z")["games"][0]["missing_steps"]


def test_shadow_failure_keeps_pregame_receipt_and_records_error(tmp_path: Path) -> None:
    result = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    assert result.receipt["primary_claims"]
    assert result.receipt["shadow_attachments"] == []
    assert result.receipt["errors"]


def test_postgame_receipt_from_another_pregame_run_is_rejected(tmp_path: Path) -> None:
    first = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    second = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:46:00Z")
    _payload, fetcher = _final_http()
    postgame = run_postgame(tmp_path, first.receipt_relative, fetcher=fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError):
        read_status(tmp_path, second.receipt_relative, postgame_receipt=postgame.receipt_relative)


def test_same_game_shadow_from_different_valid_run_is_rejected_before_fetch(tmp_path: Path) -> None:
    first = _shadow_ready_pregame(tmp_path)
    second = _second_model_run(tmp_path)
    forged = dict(first.receipt)
    forged["games"] = [dict(forged["games"][0], shadow_attachment_relative=second.receipt["shadow_attachments"][0])]
    forged["shadow_attachments"] = list(second.receipt["shadow_attachments"])
    forged["content_sha256"] = hashlib.sha256(receipt_json_bytes({k: v for k, v in forged.items() if k != "content_sha256"})).hexdigest()
    raw = receipt_json_bytes(forged)
    forged_path = tmp_path / "audit/prospective_ml_daily_flow/2026-09-03" / (hashlib.sha256(raw).hexdigest() + ".json")
    forged_path.write_bytes(raw)
    forged_path.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    calls = 0

    def must_not_fetch(*_args: object, **_kwargs: object) -> LiveHttpObservation:
        nonlocal calls
        calls += 1
        raise AssertionError("cross-run shadow must fail before result collection")

    with pytest.raises(ValueError):
        run_postgame(tmp_path, forged_path.relative_to(tmp_path), fetcher=must_not_fetch)
    assert calls == 0


def test_partial_market_trace_is_rejected_before_observation_writes(tmp_path: Path) -> None:
    trace = sealed_ml_fixture(tmp_path)
    market_trace = tmp_path / "market.trace.json"
    market_trace.write_text(json.dumps({"moneylines": [{"game_pk": 777001, "decision": "PASS"}]}))
    with pytest.raises(ValueError):
        run_pregame(tmp_path, trace, market_trace=market_trace.relative_to(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    assert not (tmp_path / "audit/prospective_ml_observation").exists()


def test_evaluated_no_quote_is_odds_missing_but_priced_pass_is_evaluated() -> None:
    states = daily_flow._market_states(
        Path.cwd(),
        [],
        None,
        games=(
            {"game_pk": 777001, "market": {"status": "EVALUATED", "decisions": [{"game_pk": 777001, "decision": "PASS", "selected_price": None}]}},
            {"game_pk": 777002, "market": {"status": "EVALUATED", "decisions": [{"game_pk": 777002, "decision": "PASS", "selected_price": -110}]}},
        ),
    )
    assert states == {777001: "EVALUATED", 777002: "EVALUATED"}
    assert daily_flow._evaluated_market_overlay(
        {"game_pk": 777001, "market": {"status": "EVALUATED", "decisions": [{"game_pk": 777001, "selected_price": None}]}}
    )["decisions"][0]["selected_price"] is None


def test_read_status_marks_evaluated_no_quote_incomplete() -> None:
    pregame = {
        "target_date": "2026-09-03",
        "games": [{
            "game_pk": 777001,
            "scheduled_start_utc": "2026-09-03T20:00:00Z",
            "primary_claim_relative": "claim.json",
            "shadow_attachment_relative": "shadow.json",
            "odds_status": "EVALUATED",
            "snapshot_relative": "snapshot.json",
        }],
    }
    snapshot = {
        "game_pk": 777001,
        "market": {
            "status": "EVALUATED",
            "trace_relative": "market.trace.json",
            "trace_sha256": "a" * 64,
            "decisions": [{"game_pk": 777001, "decision": "PASS", "selected_price": None}],
        },
    }
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daily_flow, "_read_receipt", lambda *_args, **_kwargs: pregame)
        patch.setattr(daily_flow, "_validate_pregame", lambda *_args, **_kwargs: None)
        patch.setattr(daily_flow, "load_ml_snapshot", lambda *_args, **_kwargs: snapshot)
        status = read_status(Path.cwd(), "audit/pregame.json", clock=lambda: "2026-09-03T19:00:00Z")
    game = status["games"][0]
    assert "ODDS_MISSING" in game["missing_steps"]
    assert game["evidence_complete"] is False


def test_rehashed_receipt_cannot_falsify_evaluated_market_status(tmp_path: Path) -> None:
    pregame = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    value = dict(pregame.receipt)
    trace = value["ml_trace_ref"]
    value["market_trace_ref"] = trace
    value["games"] = [dict(value["games"][0], odds_status="EVALUATED")]
    value["content_sha256"] = hashlib.sha256(receipt_json_bytes({k: v for k, v in value.items() if k != "content_sha256"})).hexdigest()
    raw = receipt_json_bytes(value)
    forged = tmp_path / "audit/prospective_ml_daily_flow/2026-09-03" / (hashlib.sha256(raw).hexdigest() + ".json")
    forged.write_bytes(raw)
    forged.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    with pytest.raises(ValueError):
        run_postgame(tmp_path, forged.relative_to(tmp_path), fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")))


def test_late_pregame_failure_receipt_keeps_observation_and_market_refs(tmp_path: Path) -> None:
    body = raw_capture()
    raw = canonical_json_bytes(body)
    relative = Path(RAW_PREFIX / f"{body['content_sha256']}.json")
    (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / relative).write_bytes(raw)
    (tmp_path / relative.with_suffix(".sha256")).write_bytes(sidecar_bytes(raw))
    trace = sealed_ml_fixture(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        def fail_market(*_args: object, **_kwargs: object) -> object:
            raise ValueError("late market finalization failure")

        patch.setattr(daily_flow, "_market_states", fail_market)
        with pytest.raises(ValueError):
            run_pregame(tmp_path, trace, market_raw_captures=(relative,), clock=lambda: "2026-09-03T19:45:00Z")
    partials = list((tmp_path / "audit/prospective_ml_daily_flow").rglob("*.json"))
    assert partials
    partial = json.loads(partials[0].read_text())
    assert partial["phase"] == "PREGAME_PARTIAL"
    assert partial["observation_candidates"]
    assert partial["primary_claims"]
    assert partial["market_outcome_refs"]


def test_duplicate_candidate_or_attachment_refs_are_rejected_before_postgame(tmp_path: Path) -> None:
    pregame = _shadow_ready_pregame(tmp_path)
    forged = dict(pregame.receipt)
    forged["observation_candidates"] = list(forged["observation_candidates"]) * 2
    forged["shadow_attachments"] = list(forged["shadow_attachments"]) * 2
    forged["content_sha256"] = hashlib.sha256(receipt_json_bytes({k: v for k, v in forged.items() if k != "content_sha256"})).hexdigest()
    raw = receipt_json_bytes(forged)
    forged_path = tmp_path / "audit/prospective_ml_daily_flow/2026-09-03" / (hashlib.sha256(raw).hexdigest() + ".json")
    forged_path.write_bytes(raw)
    forged_path.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    with pytest.raises(ValueError):
        run_postgame(tmp_path, forged_path.relative_to(tmp_path), fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")))


def test_duplicate_market_outcome_refs_are_rejected_before_postgame(tmp_path: Path) -> None:
    body = raw_capture()
    raw = canonical_json_bytes(body)
    relative = Path(RAW_PREFIX / f"{body['content_sha256']}.json")
    (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / relative).write_bytes(raw)
    (tmp_path / relative.with_suffix(".sha256")).write_bytes(sidecar_bytes(raw))
    pregame = run_pregame(
        tmp_path,
        sealed_ml_fixture(tmp_path),
        market_raw_captures=(relative,),
        clock=lambda: "2026-09-03T19:45:00Z",
    )
    forged = dict(pregame.receipt)
    forged["market_outcome_refs"] = list(forged["market_outcome_refs"]) * 2
    forged["content_sha256"] = hashlib.sha256(receipt_json_bytes({k: v for k, v in forged.items() if k != "content_sha256"})).hexdigest()
    raw_forged = receipt_json_bytes(forged)
    forged_path = tmp_path / "audit/prospective_ml_daily_flow/2026-09-03" / (hashlib.sha256(raw_forged).hexdigest() + ".json")
    forged_path.write_bytes(raw_forged)
    forged_path.with_suffix(".json.sha256").write_text(hashlib.sha256(raw_forged).hexdigest() + "\n")
    with pytest.raises(ValueError, match="duplicate pregame market outcome"):
        run_postgame(tmp_path, forged_path.relative_to(tmp_path), fetcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")))


def test_postgame_head_from_unrelated_same_game_run_is_rejected(tmp_path: Path) -> None:
    first = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    second = _second_model_run(tmp_path)
    _payload, fetcher = _final_http()
    first_postgame = run_postgame(tmp_path, first.receipt_relative, fetcher=fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    second_postgame = run_postgame(tmp_path, second.receipt_relative, fetcher=fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 1, tzinfo=timezone.utc))
    unrelated_head = second_postgame.receipt["settlement_heads"][0]
    forged_report = persist_calibration_report(
        tmp_path,
        build_calibration_report(tmp_path, first.receipt["primary_claims"], [unrelated_head]),
    )
    forged = dict(first_postgame.receipt)
    forged["baseline_report_ref"] = {"relative": forged_report.as_posix(), "sha256": hashlib.sha256((tmp_path / forged_report).read_bytes()).hexdigest()}
    forged["settlement_heads"] = [unrelated_head]
    forged["content_sha256"] = hashlib.sha256(receipt_json_bytes({k: v for k, v in forged.items() if k != "content_sha256"})).hexdigest()
    raw = receipt_json_bytes(forged)
    forged_path = tmp_path / "audit/prospective_ml_daily_flow/2026-09-03" / (hashlib.sha256(raw).hexdigest() + ".json")
    forged_path.write_bytes(raw)
    forged_path.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    with pytest.raises(ValueError):
        read_status(tmp_path, first.receipt_relative, postgame_receipt=forged_path.relative_to(tmp_path))


def test_market_evidence_is_scoped_to_game_when_summarized() -> None:
    available = SimpleNamespace(status="AVAILABLE", payload={"events": [{"away_team": "Away 1", "home_team": "Home 1", "scheduled_start_utc": "2026-09-03T20:00:00Z"}]})
    unavailable = SimpleNamespace(status="UNAVAILABLE", payload={"source_book": "BETMGM"})
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daily_flow, "load_capture_outcome", lambda _trusted, path: available if str(path) == "available" else unavailable)
        states = daily_flow._market_states(
            Path.cwd(),
            [Path("available"), Path("unavailable")],
            None,
            games=(
                {"game_pk": 777001, "away_team": "Away 1", "home_team": "Home 1", "scheduled_start_utc": "2026-09-03T20:00:00Z"},
                {"game_pk": 777002, "away_team": "Away 2", "home_team": "Home 2", "scheduled_start_utc": "2026-09-03T21:00:00Z"},
            ),
        )
    assert states == {777001: "SAVED_UNLINKED", 777002: "UNAVAILABLE"}


def test_partial_postgame_status_exposes_workflow_and_per_game_failure(tmp_path: Path) -> None:
    pregame = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")

    def failing_fetcher(*_args: object, **_kwargs: object) -> LiveHttpObservation:
        raise RuntimeError("fixture transport failure")

    postgame = run_postgame(tmp_path, pregame.receipt_relative, fetcher=failing_fetcher)
    status = read_status(tmp_path, pregame.receipt_relative, postgame_receipt=postgame.receipt_relative, clock=lambda: "2026-09-04T00:00:00Z")
    assert status["workflow_complete"] is False
    assert status["errors"]
    assert "RESULT_LINK_MISSING/PENDING" in status["games"][0]["missing_steps"]


def test_nonfinal_result_remains_pending_without_settlement_head(tmp_path: Path) -> None:
    pregame = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    payload, _unused = _final_http()
    value = json.loads(payload)
    value["dates"][0]["games"][0]["status"] = {"abstractGameState": "Preview", "detailedState": "Postponed"}
    pending_payload = canonical_json_bytes(value)

    def pending_fetcher(_source: str, url: str, **_kwargs: object) -> LiveHttpObservation:
        return LiveHttpObservation(url, url, "2026-09-03T22:59:00Z", "2026-09-03T23:00:00Z", 200, "application/json", None, None, pending_payload)

    postgame = run_postgame(tmp_path, pregame.receipt_relative, fetcher=pending_fetcher, durable_clock=lambda: datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc))
    assert not postgame.receipt["settlement_heads"]
    assert postgame.receipt["dispositions"][0]["state"] == "POSTPONED"
    status = read_status(tmp_path, pregame.receipt_relative, postgame_receipt=postgame.receipt_relative, clock=lambda: "2026-09-04T00:00:00Z")
    assert "RESULT_LINK_MISSING/PENDING" in status["games"][0]["missing_steps"]


def test_market_admission_is_durable_when_ml_build_fails(tmp_path: Path) -> None:
    body = raw_capture()
    raw = canonical_json_bytes(body)
    relative = Path(RAW_PREFIX / f"{body['content_sha256']}.json")
    (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / relative).write_bytes(raw)
    (tmp_path / relative.with_suffix(".sha256")).write_bytes(sidecar_bytes(raw))
    trace = sealed_ml_fixture(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        def fail_persist(*_args: object, **_kwargs: object) -> object:
            raise ValueError("fixture persistence failure")

        patch.setattr(daily_flow, "persist_ml_observations", fail_persist)
        with pytest.raises(ValueError):
            run_pregame(tmp_path, trace, market_raw_captures=(relative,), clock=lambda: "2026-09-03T19:45:00Z")
    outcomes = list((tmp_path / "audit/experimental_multibook_market/capture_outcomes").glob("*.json"))
    receipts = list((tmp_path / "audit/prospective_ml_daily_flow").rglob("*.json"))
    assert outcomes
    assert receipts


def test_forged_rehashed_pregame_reference_is_rejected_without_writes(tmp_path: Path) -> None:
    pregame = run_pregame(tmp_path, sealed_ml_fixture(tmp_path), clock=lambda: "2026-09-03T19:45:00Z")
    path = tmp_path / pregame.receipt_relative
    value = json.loads(path.read_text())
    value["games"][0]["snapshot_relative"] = "audit/prospective_ml_observation/candidates/wrong.json"
    value["content_sha256"] = hashlib.sha256(canonical_json_bytes({k: v for k, v in value.items() if k != "content_sha256"})).hexdigest()
    raw = canonical_json_bytes(value)
    forged = path.with_name(hashlib.sha256(raw).hexdigest() + ".json")
    forged.write_bytes(raw)
    forged.with_suffix(".json.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    before = sorted(item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*"))
    with pytest.raises(ValueError):
        read_status(tmp_path, forged)
    assert before == sorted(item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*"))
