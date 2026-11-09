"""Append-only whole-slate ML evidence ledger.

This is deliberately separate from the prospective observer and settlement
chains.  It reads their sealed evidence, freezes a pregame selection, then
records every game in the authenticated daily slate alongside later results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .daily_multibook_market_plays import (
    validate_market_plays_report,
    validate_market_plays_trace,
)
from .live_sources import _persist_observation, fetch_live_http, new_observation_id
from .live_top3_contracts import LiveHttpObservation, canonical_json_bytes, validate_run_payload
from .live_top3_trace import validate_trace_payload
from .market_play_authorities import MlAuthority, load_ml_authority, rebind_authorities
from .market_play_contracts import canonical_utc_ns, validate_relative_path
from .multibook_market_capture import load_capture_outcome
from .multibook_market_board import _same_teams
from .prospective_ml_observation import _relative, load_ml_snapshot, validate_primary_claim
from .prospective_ml_results import _read_report
from .prospective_ml_settlement import _parse_result, load_official_result_evidence
from .secure_fs import open_trusted_root, read_regular_snapshot, rebind_regular_snapshot, write_exclusive


ROOT = Path("reports/experimental_all_day_ml_ledger")
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CollectedDailyLedger:
    """Durable result locations plus row-level collection failures."""

    json_relative: Path | None
    markdown_relative: Path | None
    target_date: str
    game_count: int
    final_count: int
    pending_count: int
    failures: tuple[str, ...] = ()
    recovery_paths: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_json(self) -> str:
        return json.dumps(
            {
                "json_relative": None if self.json_relative is None else self.json_relative.as_posix(),
                "markdown_relative": None if self.markdown_relative is None else self.markdown_relative.as_posix(),
                "target_date": self.target_date,
                "game_count": self.game_count,
                "final_count": self.final_count,
                "pending_count": self.pending_count,
                "failures": list(self.failures),
                "recovery_paths": list(self.recovery_paths),
            },
            sort_keys=True,
        )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_json(root: Path, relative: Path, label: str) -> tuple[bytes, dict[str, object]]:
    with open_trusted_root(root) as trusted:
        snapshot = read_regular_snapshot(trusted, relative, label, max_bytes=MAX_BYTES)
    if snapshot is None:
        raise ValueError(f"{label} is missing")
    try:
        value = json.loads(snapshot.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is invalid")
    return snapshot.contents, value


def _sidecar(root: Path, relative: Path, raw: bytes, label: str) -> None:
    with open_trusted_root(root) as trusted:
        snapshot = read_regular_snapshot(
            trusted, relative.with_suffix(relative.suffix + ".sha256"), f"{label} sidecar", max_bytes=128
        )
    if snapshot is None or snapshot.contents != f"{_digest(raw)}\n".encode("ascii"):
        raise ValueError(f"{label} sidecar differs")


def _trace_report(root: Path, authority: MlAuthority) -> tuple[bytes, dict[str, object], dict[str, object]]:
    raw, trace = _read_json(root, authority.trace_relative, "ML trace")
    # ML trace sidecars are optional in the established producer contract.
    # ``load_ml_authority`` has already authenticated the trace (and validates
    # a sidecar when one exists), its linked primary report, and live bindings.
    trace = validate_trace_payload(trace)
    report_relative = _relative(root, str(trace["primary_report_relative"]), "ML primary report")
    report_raw, report = _read_json(root, report_relative, "ML primary report")
    report = validate_run_payload(report)
    if _digest(canonical_json_bytes({key: value for key, value in report.items() if key != "content_sha256"})) != str(trace["primary_report_content_sha256"]):
        raise ValueError("ML primary report digest binding differs")
    if report["model_card_sha256"] != authority.model_card_sha256:
        raise ValueError("ML model binding differs")
    return raw, trace, report


def _same_identity(game: object, target_date: str, value: Mapping[str, object]) -> None:
    if (
        value.get("game_pk") != game.game_pk
        or value.get("target_date") is not None and value.get("target_date") != target_date
    ):
        raise ValueError("source game identity differs")
    for field in ("scheduled_start_utc", "away_team", "home_team"):
        if value.get(field) is not None and value.get(field) != getattr(game, field):
            raise ValueError("source game identity differs")


def _same_game_identity(left: object, right: object) -> bool:
    """Fields which must agree across independent daily trace versions."""
    return (
        left.game_pk == right.game_pk
        and left.away_team == right.away_team
        and left.home_team == right.home_team
        and left.scheduled_start_utc == right.scheduled_start_utc
    )


def _primary_snapshot_candidates(
    root: Path, reports: Iterable[Path], authorities: Mapping[Path, tuple[MlAuthority, bytes]]
) -> dict[int, dict[str, object]]:
    """Authenticate supplied calibration reports and retain exact claimed snapshots."""
    candidates: dict[int, dict[str, object]] = {}
    by_trace = {path.as_posix(): (authority, sha) for path, (authority, sha) in authorities.items()}
    for report in reports:
        _, payload = _read_report(root, report)
        for claim_path in payload["primary_claims"]:
            claim = validate_primary_claim(root, Path(str(claim_path)))
            snapshot = load_ml_snapshot(root, Path(str(claim["candidate_relative"])))
            trace_key = str(snapshot["ml_trace_relative"])
            binding = by_trace.get(trace_key)
            if binding is None or snapshot["ml_trace_sha256"] != binding[1]:
                raise ValueError("prospective primary is not bound to supplied ML trace")
            authority, _ = binding
            game = authority.by_game.get(int(snapshot["game_pk"]))
            if game is None:
                raise ValueError("prospective primary game is absent from ML slate")
            _same_identity(game, authority.target_date, snapshot)
            if snapshot["model_card_sha256"] != authority.model_card_sha256:
                raise ValueError("prospective primary model differs")
            start = canonical_utc_ns(str(game.scheduled_start_utc))
            if canonical_utc_ns(str(snapshot["observed_at_utc"])) >= start or canonical_utc_ns(str(claim["claimed_at_utc"])) >= start:
                raise ValueError("prospective primary is not pregame")
            current = candidates.get(game.game_pk)
            candidate = {
                "snapshot": snapshot,
                "claim_relative": Path(str(claim_path)).as_posix(),
                "snapshot_relative": str(claim["candidate_relative"]),
                "snapshot_sha256": str(claim["candidate_sha256"]),
            }
            if current is not None and current != candidate:
                raise ValueError("contradictory prospective primaries")
            candidates[game.game_pk] = candidate
    return candidates


def _historical_candidates(authority: MlAuthority, trace_sha: str, report: Mapping[str, object]) -> dict[int, dict[str, object]]:
    candidates: dict[int, dict[str, object]] = {}
    for state, section in (("VERIFIED", report["verified_top3"]), ("PRELIMINARY", report["preliminary_top3"])):
        for item in section:
            if not isinstance(item, Mapping):
                raise ValueError("ranked report row differs")
            game = authority.by_game.get(int(item["game_pk"]))
            if game is None:
                raise ValueError("ranked report game is absent from slate")
            _same_identity(game, authority.target_date, item)
            if item.get("model_card_sha256") != authority.model_card_sha256:
                raise ValueError("ranked report model differs")
            boundary = item.get("observation_boundary_utc")
            if not isinstance(boundary, str):
                raise ValueError("ranked report observation boundary differs")
            if (
                canonical_utc_ns(boundary) >= canonical_utc_ns(game.scheduled_start_utc)
                or canonical_utc_ns(str(report["executed_at_utc"])) >= canonical_utc_ns(game.scheduled_start_utc)
            ):
                continue
            side = "home" if item.get("predicted_winner") == game.home_team else "away" if item.get("predicted_winner") == game.away_team else None
            if side is None:
                raise ValueError("ranked report side differs")
            candidates[game.game_pk] = {
                "linkage": "HISTORICAL_RANKED_PREGAME",
                "model_card_sha256": authority.model_card_sha256,
                "state": state,
                "predicted_side": side,
                "pick_probability": str(item["pick_probability"]),
                "p_home": None if game.p_home is None else str(float(game.p_home)),
                "trace_relative": authority.trace_relative.as_posix(),
                "trace_sha256": trace_sha,
                "observation_boundary_utc": boundary,
                "executed_at_utc": report["executed_at_utc"],
                "reason_codes": list(item.get("reason_codes", [])),
                "snapshot_relative": None,
            }
    return candidates


def _scored_trace_candidate(
    authority: MlAuthority, trace: Mapping[str, object], trace_sha: str, report: Mapping[str, object], game: object
) -> dict[str, object] | None:
    """Admit an eligible, scored non-Top-3 trace row as historical evidence.

    ``SCORED_NOT_TOP3`` is not the same thing as the report's UNSCORED or
    EXCLUDED classes.  Its number is a genuine pregame forecast when the trace
    proves eligibility and the producer execution predates the saved start.
    """
    row = next((item for item in trace["games"] if item.get("game_pk") == game.game_pk), None)
    if not isinstance(row, Mapping) or row.get("disposition") != "SCORED_NOT_TOP3":
        return None
    raw_features = row.get("raw_features")
    if not isinstance(raw_features, Mapping) or raw_features.get("eligible") is not True:
        return None
    if canonical_utc_ns(str(report["executed_at_utc"])) >= canonical_utc_ns(game.scheduled_start_utc):
        return None
    side = row.get("predicted_side")
    if side not in {"away", "home"} or row.get("p_home") is None or row.get("pick_probability") is None:
        raise ValueError("scored trace forecast differs")
    return {
        "linkage": "HISTORICAL_SCORED_TRACE_PREGAME",
        "model_card_sha256": authority.model_card_sha256,
        "state": "SCORED_NOT_TOP3",
        "predicted_side": side,
        "pick_probability": str(row["pick_probability"]),
        "p_home": str(row["p_home"]),
        "trace_relative": authority.trace_relative.as_posix(),
        "trace_sha256": trace_sha,
        "observation_boundary_utc": report["executed_at_utc"],
        "executed_at_utc": report["executed_at_utc"],
        "reason_codes": [],
        "snapshot_relative": None,
    }


def _exact_primary(candidate: Mapping[str, object]) -> dict[str, object]:
    snapshot = candidate["snapshot"]
    assert isinstance(snapshot, Mapping)
    return {
        "linkage": "EXACT_PROSPECTIVE_PRIMARY",
        "model_card_sha256": snapshot["model_card_sha256"],
        "state": "PRIMARY",
        "predicted_side": snapshot["predicted_side"],
        "pick_probability": snapshot["pick_probability"],
        "p_home": snapshot["p_home"],
        "trace_relative": snapshot["ml_trace_relative"],
        "trace_sha256": snapshot["ml_trace_sha256"],
        "observation_boundary_utc": snapshot["observed_at_utc"],
        "executed_at_utc": None,
        "reason_codes": list(snapshot["reason_codes"]),
        "snapshot_relative": candidate["snapshot_relative"],
        "claim_relative": candidate["claim_relative"],
        "snapshot_sha256": candidate["snapshot_sha256"],
    }


def _authenticate_market(root: Path, market_traces: Iterable[Path]) -> list[dict[str, object]]:
    admitted: list[dict[str, object]] = []
    for supplied in market_traces:
        relative = _relative(root, supplied, "market trace")
        trace_raw, trace = _read_json(root, relative, "market trace")
        trace = validate_market_plays_trace(trace)
        report_relative = _relative(root, str(trace["report_relative"]), "market report")
        report_raw, report = _read_json(root, report_relative, "market report")
        report = validate_market_plays_report(report)
        _sidecar(root, report_relative, report_raw, "market report")
        if _digest(report_raw) != trace["report_sha256"] or _digest(trace_raw) != _digest(canonical_json_bytes(trace)):
            raise ValueError("market trace/report digest binding differs")
        with open_trusted_root(root) as trusted:
            markdown = read_regular_snapshot(trusted, report_relative.with_suffix(".md"), "market markdown", max_bytes=MAX_BYTES)
        if markdown is None or _digest(markdown.contents) != trace["markdown_sha256"]:
            raise ValueError("market markdown binding differs")
        admitted.append({"trace_relative": relative.as_posix(), "trace_sha256": _digest(trace_raw), "report": report, "report_sha256": _digest(report_raw)})
    return admitted


def _capture_market_observations(
    root: Path, outcomes: Iterable[Path], supersession: Path | None, games: Mapping[int, object], target_date: str
) -> tuple[dict[int, list[dict[str, object]]], list[dict[str, object]], list[dict[str, object]], dict[str, object] | None]:
    """Load explicit raw capture outcomes without manufacturing a market play."""
    supplied = tuple(outcomes)
    allowed: set[str] | None = None
    blocked_raw: set[object] = set()
    policy_ref: dict[str, object] | None = None
    if supersession is not None:
        _raw, policy = _read_json(root, supersession, "market supersession")
        required = {"type", "target_date", "source_verification_relative", "active_capture_outcomes", "do_not_use"}
        if set(policy) != required or policy["type"] != "PROVISIONAL_MULTIBOOK_MARKET_ARTIFACT_SUPERSESSION_V1" or policy["target_date"] != target_date:
            raise ValueError("market supersession policy differs")
        verification = validate_relative_path(Path(str(policy["source_verification_relative"])))
        with open_trusted_root(root) as trusted:
            verified = read_regular_snapshot(trusted, verification, "market supersession verification", max_bytes=MAX_BYTES)
        if verified is None:
            raise ValueError("market supersession verification is missing")
        policy_ref = {"relative": supersession.as_posix(), "sha256": _digest(_raw), "verification_relative": verification.as_posix(), "verification_sha256": _digest(verified.contents), "matching_policy": "same normalized away/home, same UTC date, signed start delta within [-60,60] seconds; unique match required"}
        active = policy["active_capture_outcomes"]
        if not isinstance(active, list) or any(not isinstance(item, str) for item in active):
            raise ValueError("market supersession active outcomes differ")
        allowed = set(active)
        blocked = policy["do_not_use"]
        if not isinstance(blocked, list):
            raise ValueError("market supersession blocked outcomes differ")
        forbidden = {item.get("capture_outcome_relative") for item in blocked if isinstance(item, Mapping)}
        blocked_raw = {item.get("raw_capture_relative") for item in blocked if isinstance(item, Mapping)}
        if any(path.as_posix() in forbidden or path.as_posix() not in allowed for path in supplied):
            raise ValueError("market capture outcome is superseded or inactive")
    observations: dict[int, list[dict[str, object]]] = {pk: [] for pk in games}
    admitted: list[dict[str, object]] = []
    unmapped: list[dict[str, object]] = []
    for relative in supplied:
        with open_trusted_root(root) as trusted:
            loaded = load_capture_outcome(trusted, relative)
        if loaded.payload.get("raw_capture_relative") in blocked_raw:
            raise ValueError("market capture raw artifact is superseded")
        admitted.append({"relative": relative.as_posix(), "content_sha256": loaded.payload["content_sha256"], "status": loaded.status, "source_book": loaded.payload["source_book"], "reason": loaded.payload.get("reason")})
        if loaded.status != "AVAILABLE" or loaded.raw_snapshot is None:
            continue
        raw = json.loads(loaded.raw_snapshot.contents.decode("utf-8"))
        capture_time = str(loaded.payload["captured_at_utc"])
        for event in raw["events"]:
            if not isinstance(event, Mapping):
                raise ValueError("market capture event differs")
            candidates = []
            for candidate_pk, candidate in games.items():
                candidate_value = {"away_team": candidate.away_team, "home_team": candidate.home_team, "scheduled_start_utc": candidate.scheduled_start_utc}
                if not _same_teams(event, candidate_value) or str(event["scheduled_start_utc"])[:10] != candidate.scheduled_start_utc[:10]:
                    continue
                delta_seconds = (canonical_utc_ns(event["scheduled_start_utc"]) - canonical_utc_ns(candidate.scheduled_start_utc)) // 1_000_000_000
                if -60 <= delta_seconds <= 60:
                    candidates.append((candidate_pk, delta_seconds))
            if len(candidates) != 1:
                unmapped.append({"source": "RAW_CAPTURE_OUTCOME", "capture_outcome_relative": relative.as_posix(), "raw_capture_relative": loaded.payload["raw_capture_relative"], "source_book": loaded.payload["source_book"], "event": dict(event), "reason": "MARKET_EVENT_AMBIGUOUS" if len(candidates) > 1 else "MARKET_EVENT_UNMAPPED"})
                continue
            game_pk, delta_seconds = candidates[0]
            game = games[game_pk]
            prestart = canonical_utc_ns(capture_time) < canonical_utc_ns(game.scheduled_start_utc)
            moneyline = event["moneyline"]
            for side, field, opposing in (("away", "away_american_odds", "home_american_odds"), ("home", "home_american_odds", "away_american_odds")):
                observations[game_pk].append({"source": "RAW_CAPTURE_OUTCOME", "source_book": loaded.payload["source_book"], "capture_outcome_relative": relative.as_posix(), "capture_outcome_sha256": loaded.payload["content_sha256"], "raw_capture_relative": loaded.payload["raw_capture_relative"], "raw_capture_sha256": loaded.payload["raw_capture_sha256"], "selected_event_id": event["book_event_id"], "predicted_side": side, "selected_price": moneyline[field], "opposing_price": moneyline[opposing], "selected_captured_at_utc": capture_time, "official_scheduled_start_utc": game.scheduled_start_utc, "book_scheduled_start_utc": event["scheduled_start_utc"], "start_delta_seconds": delta_seconds, "prestart": prestart, "rejection_reason": None if prestart else "POST_START_MARKET_EVIDENCE"})
    return observations, admitted, unmapped, policy_ref


def _market_for(game: object, target_date: str, forecast: Mapping[str, object] | None, markets: list[dict[str, object]]) -> tuple[object, list[dict[str, object]]]:
    history: list[dict[str, object]] = []
    no_forecast = forecast is None
    start = canonical_utc_ns(game.scheduled_start_utc)
    qualified: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for source in markets:
        report = source["report"]
        assert isinstance(report, Mapping)
        for raw in report["moneylines"]:
            if not isinstance(raw, Mapping) or raw.get("game_pk") != game.game_pk:
                continue
            row = dict(raw)
            reason: str | None = None
            if no_forecast:
                reason = "NO_SELECTED_FORECAST"
            elif report["target_date"] != target_date or report["ml_trace_relative"] != forecast["trace_relative"] or report["ml_trace_sha256"] != forecast["trace_sha256"]:
                reason = "ML_TRACE_BINDING_MISMATCH"
            elif row.get("predicted_side") != forecast["predicted_side"]:
                reason = "PREDICTED_SIDE_MISMATCH"
            captured = row.get("selected_captured_at_utc")
            if reason is None and not isinstance(captured, str):
                reason = "MISSING_SELECTED_CAPTURE"
            elif reason is None and (canonical_utc_ns(captured) >= start or canonical_utc_ns(str(report["evaluated_at_utc"])) >= start):
                reason = "POST_START_MARKET_EVIDENCE"
            row["ledger_rejection_reason"] = reason
            row["market_trace_relative"] = source["trace_relative"]
            row["market_trace_sha256"] = source["trace_sha256"]
            row["market_report_sha256"] = source["report_sha256"]
            history.append(row)
            if reason is None:
                timing = "AT_OR_BEFORE_FORECAST" if canonical_utc_ns(captured) <= canonical_utc_ns(str(forecast["observation_boundary_utc"])) else "AFTER_FORECAST_BEFORE_START"
                row["availability"] = timing
                qualified.append(((canonical_utc_ns(captured), canonical_utc_ns(str(report["evaluated_at_utc"])), source["report_sha256"], source["trace_relative"]), row))
    if not qualified:
        return None, history, "NO_MARKET_EVIDENCE" if not history else "NO_COMPATIBLE_PREGAME_QUOTE"
    return max(qualified, key=lambda item: item[0])[1], history, None


def _result_value(game: object, target_date: str, evidence: object | None, error: str | None) -> dict[str, object]:
    if error is not None:
        return {"state": "FAILED", "winner": None, "score": None, "receipt_relative": None, "error": error, "forecast_outcome": None}
    if evidence is None:
        return {"state": "PENDING", "winner": None, "score": None, "receipt_relative": None, "error": None, "forecast_outcome": None}
    parsed = _parse_result(evidence.raw)
    if parsed["game_pk"] != game.game_pk or parsed["official_date"] != target_date or parsed["away_team"] != game.away_team or parsed["home_team"] != game.home_team:
        raise ValueError("official result does not bind ML game")
    score = None
    try:
        item = evidence.raw["dates"][0]["games"][0]
        away, home = item["teams"]["away"].get("score"), item["teams"]["home"].get("score")
        if type(away) is int and type(home) is int:
            score = {"away": away, "home": home}
    except (KeyError, IndexError, TypeError):
        pass
    return {"state": parsed["status"], "winner": parsed["winner"], "score": score, "receipt_relative": evidence.receipt_relative.as_posix(), "error": None, "forecast_outcome": None}


def _official_url(game: object, target_date: str) -> str:
    return f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={target_date}&gamePks={game.game_pk}"


def _collect_result(root: Path, game: object, target_date: str, fetcher: Callable[..., LiveHttpObservation], durable_clock: Callable[[], datetime]) -> tuple[object | None, str | None, str | None]:
    receipt_relative: str | None = None
    try:
        url = _official_url(game, target_date)
        http = fetcher("stats_api", url)
        observation_id = new_observation_id()
        persisted = _persist_observation(root, raw_source="mlb_official_result", source="stats_api", binding_role="FINAL_SCHEDULE", game_pks=(game.game_pk,), observation_id=observation_id, query={"game_pk": game.game_pk, "kind": "prospective_ml_official_result", "observation_id": observation_id, "url": url}, http=http, durable_clock=durable_clock)
        receipt_relative = persisted.source_binding.receipt_relative
        return load_official_result_evidence(root, receipt_relative), receipt_relative, None
    except Exception as exc:  # a durable receipt may intentionally precede admission failure
        return None, receipt_relative, str(exc)


def _write_idempotent(root: Path, relative: Path, raw: bytes, label: str, rebind: Callable[[], None]) -> None:
    with open_trusted_root(root) as trusted:
        try:
            write_exclusive(trusted, relative, raw, label, final_rebind=rebind)
        except FileExistsError:
            old = read_regular_snapshot(trusted, relative, label, max_bytes=len(raw))
            if old is None or old.contents != raw:
                raise ValueError(f"{label} differs")
            rebind_regular_snapshot(trusted, old, label, max_bytes=len(raw))


def _markdown(payload: Mapping[str, object]) -> bytes:
    counts = payload["counts"]
    lines = ["# Experimental all-day MLB ML ledger", "", f"Target date: `{payload['target_date']}`", f"Games: {counts['all_games']} · Final: {counts['final_results']} · Pending: {counts['pending_results']} · Errors: {counts['result_error_count']}", "", "| Game | Teams | Class | Forecast | Market | Result |", "| ---: | --- | --- | --- | --- | --- |"]
    for row in payload["games"]:
        forecast = row["selected_forecast"]
        pick = "—" if forecast is None else f"{forecast['predicted_side']} {forecast['pick_probability']}"
        market = row["market"]
        captures = row["market_capture_versions"]
        capture_text = "" if not captures else f"; {captures[0]['source_book']} @ {captures[0]['selected_captured_at_utc']}"
        quote = f"{row['market_reason']}{capture_text}" if market is None else f"{market.get('selected_price')} @ {market.get('selected_captured_at_utc')}"
        result = row["result"]
        lines.append(f"| {row['game_pk']} | {row['away_team']} @ {row['home_team']} | {row['evaluation_class']} | {pick} | {quote} | {result['state']} |")
    lines.extend(["", "All games are retained; evidence classes are not a profitability claim.", ""])
    return "\n".join(lines).encode("utf-8")


def collect_daily_ml_ledger(
    root: Path | str,
    *,
    ml_traces: Iterable[Path | str],
    prospective_reports: Iterable[Path | str] = (),
    market_traces: Iterable[Path | str] = (),
    market_capture_outcomes: Iterable[Path | str] = (),
    market_supersession: Path | str | None = None,
    result_receipts: Mapping[int, Path | str] | None = None,
    fetcher: Callable[..., LiveHttpObservation] = fetch_live_http,
    durable_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CollectedDailyLedger:
    """Collect one bounded official result per full-slate game after admission."""
    root = Path(root)
    traces = tuple(_relative(root, item, "ML trace") for item in ml_traces)
    supplied_reports = tuple(_relative(root, item, "prospective report") for item in prospective_reports)
    supplied_markets = tuple(_relative(root, item, "market trace") for item in market_traces)
    supplied_capture_outcomes = tuple(_relative(root, item, "market capture outcome") for item in market_capture_outcomes)
    supersession = None if market_supersession is None else _relative(root, market_supersession, "market supersession")
    if supplied_capture_outcomes and supersession is None:
        raise ValueError("market capture outcomes require an explicit market supersession selector")
    if not traces or len(set(traces)) != len(traces):
        raise ValueError("ML traces must be explicit and unique")
    authorities = [load_ml_authority(root, trace) for trace in traces]
    target_date = authorities[0].target_date
    if any(authority.target_date != target_date for authority in authorities):
        raise ValueError("ML trace target dates differ")
    trace_info: dict[Path, tuple[MlAuthority, bytes, dict[str, object], dict[str, object]]] = {}
    games: dict[int, object] = {}
    for authority in authorities:
        raw, trace, report = _trace_report(root, authority)
        trace_info[authority.trace_relative] = (authority, raw, trace, report)
        for game in authority.games:
            previous = games.get(game.game_pk)
            if previous is not None and not _same_game_identity(previous, game):
                raise ValueError("ML trace game identity differs")
            games[game.game_pk] = game
    auth_for_primary = {path: (item[0], _digest(item[1])) for path, item in trace_info.items()}
    primaries = _primary_snapshot_candidates(root, supplied_reports, auth_for_primary)
    markets = _authenticate_market(root, supplied_markets)
    capture_observations, capture_sources, capture_unmapped, capture_policy = _capture_market_observations(root, supplied_capture_outcomes, supersession, games, target_date)
    supplied = dict(result_receipts or {})
    if any(type(key) is not int or key not in games for key in supplied):
        raise ValueError("result receipt game is absent from slate")
    evidence: dict[int, object] = {}

    # Selection is fully frozen before any outcome fetch/read beyond the explicit
    # result-receipt authentication above.  Those receipts cannot influence it.
    rows: list[dict[str, object]] = []
    selected: dict[int, dict[str, object] | None] = {}
    versions: dict[int, list[dict[str, object]]] = {}
    for game_pk, game in sorted(games.items(), key=lambda pair: (pair[1].scheduled_start_utc, pair[0])):
        matching = [item for item in trace_info.values() if game_pk in item[0].by_game and _same_game_identity(item[0].by_game[game_pk], game)]
        authority = matching[0][0]
        historical_versions: list[dict[str, object]] = []
        all_versions: list[dict[str, object]] = []
        for trace_authority, raw, trace, report in matching:
            source_game = trace_authority.by_game[game_pk]
            trace_sha = _digest(raw)
            historical = _historical_candidates(trace_authority, trace_sha, report).get(game_pk)
            if historical is None:
                historical = _scored_trace_candidate(trace_authority, trace, trace_sha, report, source_game)
            if historical is not None:
                historical_versions.append(historical)
                all_versions.append(dict(historical))
            else:
                trace_row = next((row for row in trace["games"] if row.get("game_pk") == game_pk), None)
                report_row = next((row for section in ("unscored_games", "excluded_games") for row in report[section] if row.get("game_pk") == game_pk), {})
                all_versions.append({"linkage": "DIAGNOSTIC_TRACE", "model_card_sha256": trace_authority.model_card_sha256, "trace_relative": trace_authority.trace_relative.as_posix(), "trace_sha256": trace_sha, "executed_at_utc": report["executed_at_utc"], "state": None if not isinstance(trace_row, Mapping) else trace_row.get("disposition"), "p_home": None if not isinstance(trace_row, Mapping) else trace_row.get("p_home"), "predicted_side": None if not isinstance(trace_row, Mapping) else trace_row.get("predicted_side"), "pick_probability": None if not isinstance(trace_row, Mapping) else trace_row.get("pick_probability"), "reason_codes": list(report_row.get("reason_codes", []))})
        historical = max(
            historical_versions,
            key=lambda value: (
                canonical_utc_ns(str(value["observation_boundary_utc"])),
                canonical_utc_ns(str(value["executed_at_utc"])),
                str(value["trace_sha256"]),
                str(value["trace_relative"]),
            ),
            default=None,
        )
        primary = primaries.get(game_pk)
        chosen = _exact_primary(primary) if primary is not None else historical
        selected[game_pk] = chosen
        versions[game_pk] = all_versions
        if primary is not None:
            versions[game_pk].append(dict(chosen))
        if chosen is not None:
            klass = "EXACT_PROSPECTIVE_PRIMARY" if primary is not None else str(chosen["linkage"])
        elif any(item[0].by_game[game_pk].model_status == "SCORED" and item[0].by_game[game_pk].p_home is not None for item in matching):
            poststart = any(
                value["linkage"] == "DIAGNOSTIC_TRACE"
                and canonical_utc_ns(str(value["executed_at_utc"])) >= canonical_utc_ns(game.scheduled_start_utc)
                for value in all_versions
            )
            if poststart:
                klass = "POST_START_TRACE_DIAGNOSTIC"
                for value in all_versions:
                    if value["linkage"] == "DIAGNOSTIC_TRACE":
                        value["reason_codes"] = sorted(set(value["reason_codes"]) | {"POST_START_TRACE"})
            else:
                klass = "TRACE_DIAGNOSTIC_UNRANKED"
        else:
            klass = "UNSCORED_OR_EXCLUDED"
        market, market_history, market_reason = _market_for(game, target_date, chosen, markets)
        rows.append({"game_pk": game_pk, "away_team": game.away_team, "home_team": game.home_team, "scheduled_start_utc": game.scheduled_start_utc, "model_card_sha256": None if chosen is None else chosen["model_card_sha256"], "evaluation_class": klass, "diagnostic_trace_p_home": None if game.p_home is None else str(float(game.p_home)), "selected_forecast": chosen, "prediction_versions": versions[game_pk], "market": market, "market_reason": market_reason, "market_versions": market_history, "market_capture_versions": capture_observations[game_pk], "result": None})

    # Explicit outcome evidence is deliberately authenticated only after the
    # deterministic prediction snapshot above, but before any result fetch.
    for game_pk, receipt_path in supplied.items():
        item = load_official_result_evidence(root, receipt_path)
        _result_value(games[game_pk], target_date, item, None)
        evidence[game_pk] = item

    failures: list[str] = []
    recovery: list[str] = []
    for row in rows:
        game = games[int(row["game_pk"])]
        item = evidence.get(game.game_pk)
        error: str | None = None
        receipt: str | None = None
        if item is None:
            if error is None:
                item, receipt, error = _collect_result(root, game, target_date, fetcher, durable_clock)
            if receipt is not None:
                recovery.append(receipt)
            if error is not None:
                failures.append(f"{game.game_pk}: {error}")
        else:
            recovery.append(item.receipt_relative.as_posix())
        try:
            result = _result_value(game, target_date, item, error)
        except ValueError as exc:
            failures.append(f"{game.game_pk}: {exc}")
            result = _result_value(game, target_date, None, str(exc))
            if item is not None:
                result["receipt_relative"] = item.receipt_relative.as_posix()
        if receipt is not None and result["receipt_relative"] is None:
            result["receipt_relative"] = receipt
        forecast = row["selected_forecast"]
        if result["state"] == "FINAL" and forecast is not None:
            result["forecast_outcome"] = "WIN" if result["winner"] == (game.home_team if forecast["predicted_side"] == "home" else game.away_team) else "LOSS"
        row["result"] = result

    final_count = sum(row["result"]["state"] == "FINAL" for row in rows)
    pending_count = sum(row["result"]["state"] not in {"FINAL", "FAILED"} for row in rows)
    payload: dict[str, object] = {"schema": "EXPERIMENTAL_ALL_DAY_ML_LEDGER_V1", "target_date": target_date, "market_capture_policy": capture_policy, "market_capture_outcomes": capture_sources, "market_capture_unmapped": capture_unmapped, "games": rows, "counts": {"all_games": len(rows), "exact_prospective_primary": sum(row["evaluation_class"] == "EXACT_PROSPECTIVE_PRIMARY" for row in rows), "historical_ranked_pregame": sum(row["evaluation_class"] in {"HISTORICAL_RANKED_PREGAME", "HISTORICAL_SCORED_TRACE_PREGAME"} for row in rows), "diagnostic_unranked": sum(row["evaluation_class"] == "TRACE_DIAGNOSTIC_UNRANKED" for row in rows), "post_start_trace_diagnostic": sum(row["evaluation_class"] == "POST_START_TRACE_DIAGNOSTIC" for row in rows), "unscored_or_excluded": sum(row["evaluation_class"] == "UNSCORED_OR_EXCLUDED" for row in rows), "final_results": final_count, "result_error_count": sum(row["result"]["state"] == "FAILED" for row in rows), "pending_results": sum(row["result"]["state"] not in {"FINAL", "FAILED"} for row in rows)}, "content_sha256": ""}
    payload["content_sha256"] = _digest(canonical_json_bytes({key: value for key, value in payload.items() if key != "content_sha256"}))
    raw = canonical_json_bytes(payload)
    digest = _digest(raw)
    json_relative = ROOT / target_date / f"{digest}.json"
    markdown_relative = ROOT / target_date / f"{digest}.md"

    def rebind() -> None:
        rebind_authorities(*authorities)
        for report in supplied_reports:
            _read_report(root, report)
        _authenticate_market(root, supplied_markets)
        _capture_market_observations(root, supplied_capture_outcomes, supersession, games, target_date)

    try:
        _write_idempotent(root, json_relative, raw, "all-day ledger JSON", rebind)
        recovery.append(json_relative.as_posix())
        _write_idempotent(root, markdown_relative, _markdown(payload), "all-day ledger Markdown", rebind)
        recovery.append(markdown_relative.as_posix())
    except Exception as exc:
        failures.append(str(exc))
        return CollectedDailyLedger(json_relative if json_relative.as_posix() in recovery else None, markdown_relative if markdown_relative.as_posix() in recovery else None, target_date, len(rows), final_count, pending_count, tuple(failures), tuple(dict.fromkeys(recovery)))
    return CollectedDailyLedger(json_relative, markdown_relative, target_date, len(rows), final_count, pending_count, tuple(failures), tuple(dict.fromkeys(recovery)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an immutable all-day MLB ML ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--root", required=True)
    collect.add_argument("--ml-trace", action="append", required=True)
    collect.add_argument("--prospective-report", action="append", default=[])
    collect.add_argument("--market-trace", action="append", default=[])
    collect.add_argument("--market-capture-outcome", action="append", default=[])
    collect.add_argument("--market-supersession")
    collect.add_argument("--result-receipt", action="append", default=[])
    args = parser.parse_args(argv)
    receipts: dict[int, str] = {}
    for value in args.result_receipt:
        try:
            game, path = value.split("=", 1)
            game_pk = int(game)
        except ValueError as exc:
            parser.error("--result-receipt must be GAMEPK=RELATIVE_PATH")
            raise AssertionError from exc
        if game_pk in receipts or not path:
            parser.error("duplicate or empty --result-receipt")
        receipts[game_pk] = path
    try:
        result = collect_daily_ml_ledger(Path(args.root), ml_traces=args.ml_trace, prospective_reports=args.prospective_report, market_traces=args.market_trace, market_capture_outcomes=args.market_capture_outcome, market_supersession=args.market_supersession, result_receipts=receipts)
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": str(exc)}, sort_keys=True))
        return 1
    print(result.as_json())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
