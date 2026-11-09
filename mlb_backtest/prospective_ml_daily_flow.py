"""Thin, explicit coordinator for the prospective ML daily lifecycle.

This module only wires existing admission/reporting APIs together.  It never
discovers a latest artifact, runs a model, or fetches market data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
from typing import Callable, Mapping

from .market_play_contracts import canonical_json_bytes, canonical_utc, canonical_utc_ns
from .prospective_ml_observation import build_ml_observations, persist_ml_observations, load_ml_snapshot, validate_primary_claim, _relative, _sha
from .prospective_ml_shadow import build_shadow_attachment, persist_shadow_attachment, load_shadow_attachment, _candidate_path
from .prospective_ml_settlement import build_calibration_report, persist_calibration_report, _validated_head_chain
from .prospective_ml_shadow_report import build_shadow_paired_report, persist_shadow_paired_report
from .prospective_ml_results import CollectionFailure, collect_results
from .multibook_market_capture import admit_raw_market_capture, record_unavailable_book, load_capture_outcome
from .secure_fs import open_trusted_root, read_regular_snapshot, rebind_regular_snapshot, write_exclusive


RECEIPT_SCHEMA = "PROSPECTIVE_ML_DAILY_FLOW_RECEIPT_V1"
RECEIPT_ROOT = Path("audit/prospective_ml_daily_flow")


def normalize_root(root: Path | str) -> Path:
    """Return a stable absolute project root without changing process cwd."""
    return Path(os.path.abspath(os.fspath(root)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _relative_root(root: Path, value: Path | str, label: str) -> Path:
    return _relative(root, value, label)


def _digest(value: Mapping[str, object]) -> str:
    return _sha(canonical_json_bytes({k: v for k, v in value.items() if k != "content_sha256"}))


def _ref(root: Path, value: Path | str, label: str) -> dict[str, str]:
    rel = _relative_root(root, value, label)
    with open_trusted_root(root) as trusted:
        snap = read_regular_snapshot(trusted, rel, label, max_bytes=64 * 1024 * 1024)
    if snap is None:
        raise ValueError(f"{label} is missing")
    return {"relative": rel.as_posix(), "sha256": _sha(snap.contents)}


def _write_idempotent(root: Path, relative: Path, raw: bytes, label: str) -> None:
    with open_trusted_root(root) as trusted:
        try:
            write_exclusive(trusted, relative, raw, label)
        except FileExistsError:
            old = read_regular_snapshot(trusted, relative, label, max_bytes=max(len(raw), 65))
            if old is None or old.contents != raw:
                raise ValueError(f"{label} differs")
            rebind_regular_snapshot(trusted, old, label, max_bytes=max(len(raw), 65))


def _read_receipt(root: Path, receipt: Path | str, label: str = "daily flow receipt") -> dict[str, object]:
    root = normalize_root(root)
    rel = _relative_root(root, receipt, label)
    with open_trusted_root(root) as trusted:
        snap = read_regular_snapshot(trusted, rel, label, max_bytes=64 * 1024 * 1024)
        sidecar = read_regular_snapshot(trusted, rel.with_suffix(rel.suffix + ".sha256"), f"{label} sidecar", max_bytes=128)
    if snap is None or sidecar is None or sidecar.contents != f"{_sha(snap.contents)}\n".encode("ascii"):
        raise ValueError(f"{label} integrity differs")
    try:
        value = json.loads(snap.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA or value.get("content_sha256") != _digest(value):
        raise ValueError(f"{label} digest differs")
    if canonical_json_bytes(value) != snap.contents:
        raise ValueError(f"{label} is not canonical")
    if not isinstance(value.get("target_date"), str) or rel.parent != RECEIPT_ROOT / value["target_date"] or rel.stem != _sha(snap.contents):
        raise ValueError(f"{label} path differs")
    return value


def _persist_receipt(root: Path, value: dict[str, object]) -> Path:
    value = dict(value)
    value["content_sha256"] = _digest(value)
    raw = canonical_json_bytes(value)
    target_date = str(value["target_date"])
    path = RECEIPT_ROOT / target_date / f"{_sha(raw)}.json"
    _write_idempotent(root, path.with_suffix(".json.sha256"), f"{_sha(raw)}\n".encode("ascii"), "daily flow receipt sidecar")
    _write_idempotent(root, path, raw, "daily flow receipt")
    return path


def _game_rows(root: Path, snapshots: tuple[dict[str, object], ...], candidates: tuple[Path, ...], claims: tuple[Path, ...], attachments: tuple[Path, ...], market_statuses: Mapping[int, str]) -> list[dict[str, object]]:
    claim_by_game: dict[int, Path] = {}
    for claim in claims:
        checked = validate_primary_claim(root, claim)
        claim_by_game[int(checked["game_pk"])] = _relative_root(root, claim, "primary claim")
    attachment_by_game: dict[int, Path] = {}
    for path in attachments:
        value = load_shadow_attachment(root, path)
        attachment_by_game[int(value["game_pk"])] = _relative_root(root, path, "shadow attachment")
    candidate_by_game = {int(snapshot["game_pk"]): _relative_root(root, candidate, "observation candidate") for snapshot, candidate in zip(snapshots, candidates, strict=True)}
    rows = []
    for snapshot in snapshots:
        game = int(snapshot["game_pk"])
        rows.append({
            "game_pk": game,
            "target_date": snapshot["target_date"],
            "scheduled_start_utc": snapshot["scheduled_start_utc"],
            "away_team": snapshot["away_team"],
            "home_team": snapshot["home_team"],
            "snapshot_relative": candidate_by_game[game].as_posix(),
            "primary_claim_relative": None if game not in claim_by_game else claim_by_game[game].as_posix(),
            "shadow_attachment_relative": None if game not in attachment_by_game else attachment_by_game[game].as_posix(),
            "primary_eligible": bool(snapshot["primary_eligible"]),
            "reason_codes": list(snapshot["reason_codes"]),
            "odds_status": market_statuses[game],
        })
    return rows


def _evaluated_market_overlay(game: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the exact evaluated moneyline overlay copied into a snapshot."""
    market = game.get("market")
    if not isinstance(market, Mapping) or market.get("status") == "NOT_EVALUATED":
        return None
    if market.get("status") != "EVALUATED":
        raise ValueError("snapshot market status differs")
    decisions = market.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 1:
        raise ValueError("snapshot market decision differs")
    decision = decisions[0]
    if not isinstance(decision, Mapping) or decision.get("game_pk") != game.get("game_pk"):
        raise ValueError("snapshot market game differs")
    return market


def _market_states(root: Path, outcomes: list[Path], _market_trace: Path | str | None, *, games: tuple[Mapping[str, object], ...]) -> dict[int, str]:
    """Summarize supplied market evidence against exact slate-game identities."""
    states = {int(game["game_pk"]): "MISSING" for game in games}
    evaluated_games: set[int] = set()
    for game in games:
        overlay = _evaluated_market_overlay(game)
        if overlay is not None:
            game_pk = int(game["game_pk"])
            evaluated_games.add(game_pk)
            states[game_pk] = "EVALUATED"
    unavailable = False
    for path in outcomes:
        with open_trusted_root(root) as trusted:
            loaded = load_capture_outcome(trusted, path)
        if loaded.status == "UNAVAILABLE":
            unavailable = True
            continue
        payload = loaded.payload
        events = payload.get("events")
        raw_snapshot = getattr(loaded, "raw_snapshot", None)
        if raw_snapshot is not None:
            try:
                raw = json.loads(raw_snapshot.contents.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("available market outcome raw capture is invalid") from exc
            if isinstance(raw, Mapping):
                events = raw.get("events")
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, Mapping):
                continue
            for game in games:
                if (
                    event.get("away_team") == game.get("away_team")
                    and event.get("home_team") == game.get("home_team")
                    and event.get("scheduled_start_utc") == game.get("scheduled_start_utc")
                    and int(game["game_pk"]) not in evaluated_games
                ):
                    states[int(game["game_pk"])] = "SAVED_UNLINKED"
    if unavailable:
        for game_pk, state in states.items():
            if state == "MISSING" and game_pk not in evaluated_games:
                states[game_pk] = "UNAVAILABLE"
    return states


@dataclass(frozen=True, slots=True)
class PregameResult:
    receipt_relative: Path
    receipt: dict[str, object]


def _persist_pregame_partial(root: Path, built, ml_trace: Path, market_trace: Path | None, outcomes: list[Path], error: Exception, *, persisted=None, attachments: tuple[Path, ...] = (), staged_candidates: tuple[Path, ...] = (), games: list[dict[str, object]] | None = None) -> Path:
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "phase": "PREGAME_PARTIAL",
        "target_date": built.snapshots[0]["target_date"] if built.snapshots else "partial",
        "ml_trace_ref": _ref(root, ml_trace, "ML trace"),
        "market_trace_ref": None if market_trace is None else _ref(root, market_trace, "market trace"),
        "market_outcome_refs": [_ref(root, path, "market outcome") for path in outcomes],
        "primary_claims": [] if persisted is None else [path.as_posix() for path in persisted.primary_claim_paths],
        "observation_candidates": [] if persisted is None else [path.as_posix() for path in persisted.candidate_paths],
        "shadow_attachments": [path.as_posix() for path in attachments],
        "shadow_staged_candidates": [path.as_posix() for path in staged_candidates],
        "games": [] if games is None else games,
        "errors": [str(error)],
        "content_sha256": "",
    }
    receipt["content_sha256"] = _digest(receipt)
    return _persist_receipt(root, receipt)


def run_pregame(
    root: Path | str,
    ml_trace: Path | str,
    *,
    market_trace: Path | str | None = None,
    market_raw_captures: tuple[Path | str, ...] = (),
    market_unavailable_records: tuple[Path | str, ...] = (),
    clock: Callable[[], str] = _now,
    competition_scope: str = "regular-season",
) -> PregameResult:
    if competition_scope == "postseason-research-v1":
        if market_trace is not None or market_raw_captures or market_unavailable_records:
            raise ValueError("market inputs are not supported in postseason research")
        from .prospective_ml_postseason_research import run_research_pregame
        return run_research_pregame(root, ml_trace, clock=clock)
    if competition_scope != "regular-season":
        raise ValueError("competition scope differs")
    root = normalize_root(root)
    if not callable(clock):
        raise TypeError("clock must be callable")
    ml_relative = _relative_root(root, ml_trace, "ML trace")
    market_relative = None if market_trace is None else _relative_root(root, market_trace, "market trace")
    captures = tuple(_relative_root(root, capture, "raw market capture") for capture in market_raw_captures)
    unavailable_records = tuple(_relative_root(root, unavailable, "unavailable market record") for unavailable in market_unavailable_records)
    observed = canonical_utc(clock())
    built = build_ml_observations(root, ml_relative, market_trace=market_relative, observed_clock=observed)
    outcomes: list[Path] = []
    try:
        # Admission is local and append-only; later failures retain its receipts.
        for capture in captures:
            outcomes.append(admit_raw_market_capture(root, capture))
        for unavailable in unavailable_records:
            outcomes.append(record_unavailable_book(root, unavailable))
        persisted = persist_ml_observations(root, built, primary_clock=clock)
    except Exception as exc:
        _persist_pregame_partial(root, built, ml_relative, market_relative, outcomes, exc)
        raise
    attachments: list[Path] = []
    staged_candidates: list[Path] = []
    rows: list[dict[str, object]] = []
    shadow_errors: dict[int, str] = {}
    try:
        for claim in persisted.primary_claim_paths:
            built_shadow = None
            try:
                built_shadow = build_shadow_attachment(root, claim, observed_clock=clock)
                shadow = persist_shadow_attachment(root, built_shadow, primary_clock=clock)
                attachments.append(shadow.attachment_path)
            except Exception as exc:  # retain previous game outputs and report partial progress
                if built_shadow is not None:
                    staged = _candidate_path(built_shadow.attachment)
                    with open_trusted_root(root) as trusted:
                        if read_regular_snapshot(trusted, staged, "shadow staged candidate", max_bytes=64 * 1024 * 1024, missing_ok=True) is not None:
                            staged_candidates.append(staged)
                try:
                    game = int(validate_primary_claim(root, claim)["game_pk"])
                except Exception:
                    game = -1
                shadow_errors[game] = str(exc)
        market_statuses = _market_states(root, outcomes, market_trace, games=persisted.snapshots)
        rows = _game_rows(root, persisted.snapshots, persisted.candidate_paths, persisted.primary_claim_paths, tuple(attachments), market_statuses)
        for row in rows:
            if int(row["game_pk"]) in shadow_errors:
                row["shadow_error"] = shadow_errors[int(row["game_pk"])]
        trace_ref = _ref(root, ml_trace, "ML trace")
        receipt: dict[str, object] = {
            "schema": RECEIPT_SCHEMA,
            "phase": "PREGAME",
            "target_date": built.snapshots[0]["target_date"] if built.snapshots else "",
            "ml_trace_ref": trace_ref,
            "market_trace_ref": None if market_trace is None else _ref(root, market_trace, "market trace"),
            "market_outcome_refs": [_ref(root, path, "market outcome") for path in outcomes],
            "primary_claims": [p.as_posix() for p in persisted.primary_claim_paths],
            "observation_candidates": [p.as_posix() for p in persisted.candidate_paths],
            "shadow_attachments": [p.as_posix() for p in attachments],
            "shadow_staged_candidates": [p.as_posix() for p in staged_candidates],
            "games": rows,
            "errors": {str(k): v for k, v in shadow_errors.items()},
            "content_sha256": "",
        }
        receipt["content_sha256"] = _digest(receipt)
    except Exception as exc:
        _persist_pregame_partial(
            root, built, ml_relative, market_relative, outcomes, exc,
            persisted=persisted, attachments=tuple(attachments),
            staged_candidates=tuple(staged_candidates), games=rows,
        )
        raise
    path = _persist_receipt(root, receipt)
    return PregameResult(path, receipt)


def _validate_pregame(root: Path, receipt: Mapping[str, object]) -> None:
    if receipt.get("phase") != "PREGAME" or not isinstance(receipt.get("target_date"), str):
        raise ValueError("pregame receipt differs")
    trace = receipt.get("ml_trace_ref")
    if not isinstance(trace, Mapping) or set(trace) != {"relative", "sha256"}:
        raise ValueError("pregame ML trace reference differs")
    actual = _ref(root, str(trace["relative"]), "ML trace")
    if dict(trace) != actual:
        raise ValueError("pregame ML trace reference differs")
    claims = receipt.get("primary_claims")
    if not isinstance(claims, list):
        raise ValueError("pregame claims differ")
    checked_claims: dict[int, dict[str, object]] = {}
    for claim in claims:
        checked = validate_primary_claim(root, str(claim))
        game_pk = int(checked["game_pk"])
        if game_pk in checked_claims:
            raise ValueError("duplicate pregame claim")
        checked_claims[game_pk] = checked
    games = receipt.get("games")
    if not isinstance(games, list):
        raise ValueError("pregame games differ")
    seen_games: set[int] = set()
    row_claims: set[str] = set()
    row_attachments: set[str] = set()
    snapshots_by_game: dict[int, dict[str, object]] = {}
    trace_rel = str(trace["relative"])
    trace_sha = str(trace["sha256"])
    for game in games:
        if not isinstance(game, Mapping) or type(game.get("game_pk")) is not int:
            raise ValueError("pregame game differs")
        game_pk = int(game["game_pk"])
        if game_pk in seen_games:
            raise ValueError("duplicate pregame game")
        seen_games.add(game_pk)
        if game.get("target_date") != receipt["target_date"]:
            raise ValueError("pregame game date differs")
        candidate = game.get("snapshot_relative")
        if not isinstance(candidate, str):
            raise ValueError("pregame candidate differs")
        snapshot = load_ml_snapshot(root, candidate)
        if snapshot["game_pk"] != game_pk or snapshot["target_date"] != receipt["target_date"] or snapshot["ml_trace_relative"] != trace_rel or snapshot["ml_trace_sha256"] != trace_sha:
            raise ValueError("pregame candidate run differs")
        snapshots_by_game[game_pk] = snapshot
        claim = game.get("primary_claim_relative")
        if claim is not None:
            checked = validate_primary_claim(root, str(claim))
            if checked["game_pk"] != game_pk or str(claim) not in {str(item) for item in claims}:
                raise ValueError("pregame claim game differs")
            row_claims.add(str(claim))
            if checked["candidate_relative"] != candidate or checked["candidate_sha256"] != _sha(canonical_json_bytes(snapshot)):
                raise ValueError("pregame claim candidate differs")
        attachment = game.get("shadow_attachment_relative")
        if attachment is not None:
            if claim is None:
                raise ValueError("pregame shadow claim differs")
            shadow = load_shadow_attachment(root, str(attachment))
            if shadow["game_pk"] != game_pk:
                raise ValueError("pregame shadow game differs")
            baseline = shadow.get("baseline")
            expected_baseline = {
                "primary_claim_relative": str(claim),
                "primary_claim_sha256": _ref(root, str(claim), "primary claim")["sha256"],
                "snapshot_relative": candidate,
                "snapshot_sha256": _sha(canonical_json_bytes(snapshot)),
                "model_card_sha256": snapshot["model_card_sha256"],
                "ml_trace_relative": snapshot["ml_trace_relative"],
                "ml_trace_sha256": snapshot["ml_trace_sha256"],
            }
            if not isinstance(baseline, Mapping) or any(baseline.get(key) != value for key, value in expected_baseline.items()):
                raise ValueError("pregame shadow baseline differs")
            row_attachments.add(str(attachment))
    expected_claims = [str(game["primary_claim_relative"]) for game in games if game.get("primary_claim_relative") is not None]
    if [str(item) for item in claims] != expected_claims or row_claims != set(expected_claims):
        raise ValueError("pregame claims are not represented by games")
    candidates = receipt.get("observation_candidates")
    expected_candidates = [str(game["snapshot_relative"]) for game in games]
    if not isinstance(candidates, list) or [str(item) for item in candidates] != expected_candidates:
        raise ValueError("pregame candidates are not represented by games")
    for candidate in candidates:
        load_ml_snapshot(root, str(candidate))
    attachments = receipt.get("shadow_attachments")
    expected_attachments = [str(game["shadow_attachment_relative"]) for game in games if game.get("shadow_attachment_relative") is not None]
    if not isinstance(attachments, list) or [str(item) for item in attachments] != expected_attachments or row_attachments != set(expected_attachments):
        raise ValueError("pregame attachments are not represented by games")
    outcome_refs = receipt.get("market_outcome_refs")
    if not isinstance(outcome_refs, list):
        raise ValueError("pregame market outcomes differ")
    seen_outcomes: set[str] = set()
    for ref in outcome_refs:
        if not isinstance(ref, Mapping) or not isinstance(ref.get("relative"), str):
            raise ValueError("pregame market outcome reference differs")
        relative = str(ref["relative"])
        if relative in seen_outcomes:
            raise ValueError("duplicate pregame market outcome")
        seen_outcomes.add(relative)
        if dict(ref) != _ref(root, relative, "market outcome"):
            raise ValueError("pregame market outcome reference differs")
    expected_states = _market_states(
        root, [Path(str(ref["relative"])) for ref in outcome_refs], None,
        games=tuple(snapshots_by_game[int(game["game_pk"])] for game in games),
    )
    for game in games:
        if game.get("odds_status") != expected_states[int(game["game_pk"])]:
            raise ValueError("pregame market status differs")
    evaluated_refs = {
        (str(overlay["trace_relative"]), str(overlay["trace_sha256"]))
        for snapshot in snapshots_by_game.values()
        if (overlay := _evaluated_market_overlay(snapshot)) is not None
    }
    ref = receipt.get("market_trace_ref")
    if not evaluated_refs:
        if ref is not None:
            raise ValueError("pregame market trace reference differs")
    elif len(evaluated_refs) != 1 or not isinstance(ref, Mapping) or dict(ref) != {
        "relative": next(iter(evaluated_refs))[0], "sha256": next(iter(evaluated_refs))[1],
    } or dict(ref) != _ref(root, str(ref["relative"]), "market trace"):
        raise ValueError("pregame market trace reference differs")


@dataclass(frozen=True, slots=True)
class PostgameResult:
    receipt_relative: Path
    receipt: dict[str, object]


def _report_value(root: Path, report: Path | str) -> dict[str, object]:
    rel = _relative_root(root, report, "calibration report")
    with open_trusted_root(root) as trusted:
        snap = read_regular_snapshot(trusted, rel, "calibration report", max_bytes=64 * 1024 * 1024)
    if snap is None:
        raise ValueError("calibration report missing")
    try:
        value = json.loads(snap.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("calibration report invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("calibration report invalid")
    return value


def _validate_postgame(root: Path, receipt: Mapping[str, object], pregame: Mapping[str, object], pregame_rel: Path) -> None:
    expected_pregame = _ref(root, pregame_rel, "pregame receipt")
    if receipt.get("phase") != "POSTGAME" or receipt.get("pregame_receipt_ref") != expected_pregame:
        raise ValueError("postgame receipt run differs")
    baseline_ref = receipt.get("baseline_report_ref")
    if not isinstance(baseline_ref, Mapping) or dict(baseline_ref) != _ref(root, str(baseline_ref.get("relative")), "calibration report"):
        raise ValueError("postgame baseline report reference differs")
    report = _report_value(root, str(baseline_ref["relative"]))
    if report.get("primary_claims") != pregame.get("primary_claims"):
        raise ValueError("postgame baseline claims differ")
    heads = receipt.get("settlement_heads")
    if not isinstance(heads, list) or heads != report.get("settlement_heads"):
        raise ValueError("postgame settlement heads differ")
    expected_snapshots: set[str] = set()
    for claim_path in pregame.get("primary_claims", []):
        claim = validate_primary_claim(root, str(claim_path))
        expected_snapshots.add(str(claim["candidate_sha256"]))
    seen_snapshots: set[str] = set()
    for head in heads:
        record = _validated_head_chain(root, _relative_root(root, str(head), "settlement head"))
        snapshot_sha = str(record["snapshot_sha256"])
        if snapshot_sha not in expected_snapshots:
            raise ValueError("postgame settlement head is unrelated to pregame claims")
        if snapshot_sha in seen_snapshots:
            raise ValueError("duplicate postgame settlement head")
        seen_snapshots.add(snapshot_sha)
    shadow_ref = receipt.get("shadow_report_ref")
    if shadow_ref is not None:
        if not isinstance(shadow_ref, Mapping) or dict(shadow_ref) != _ref(root, str(shadow_ref.get("relative")), "shadow report"):
            raise ValueError("postgame shadow report reference differs")
        with open_trusted_root(root) as trusted:
            snap = read_regular_snapshot(trusted, _relative_root(root, str(shadow_ref["relative"]), "shadow report"), "shadow report", max_bytes=64 * 1024 * 1024)
        if snap is None:
            raise ValueError("shadow report missing")
        try:
            shadow = json.loads(snap.contents.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("shadow report invalid") from exc
        from .prospective_ml_shadow_report import _validate_report
        shadow = _validate_report(shadow)
        expected_attachments = [str(path) for path in pregame.get("shadow_attachments", [])]
        if [ref.get("relative") for ref in shadow["attachment_refs"]] != expected_attachments:
            raise ValueError("postgame shadow attachments differ")


def run_postgame(
    root: Path | str,
    pregame_receipt: Path | str,
    *,
    previous_postgame_receipt: Path | str | None = None,
    fetcher=None,
    durable_clock=None,
    competition_scope: str = "regular-season",
) -> PostgameResult:
    if competition_scope == "postseason-research-v1":
        from .prospective_ml_postseason_research import run_research_postgame
        return run_research_postgame(root, pregame_receipt, previous_postgame_receipt=previous_postgame_receipt, fetcher=fetcher, durable_clock=durable_clock)
    if competition_scope != "regular-season":
        raise ValueError("competition scope differs")
    root = normalize_root(root)
    pregame_rel = _relative_root(root, pregame_receipt, "pregame receipt")
    pregame = _read_receipt(root, pregame_rel, "pregame receipt")
    _validate_pregame(root, pregame)
    previous = None
    if previous_postgame_receipt is not None:
        previous = _read_receipt(root, previous_postgame_receipt, "previous postgame receipt")
        _validate_postgame(root, previous, pregame, pregame_rel)
    claims = [Path(str(item)) for item in pregame["primary_claims"]]
    heads: list[Path] = []
    if previous is not None:
        raw_heads = previous.get("settlement_heads")
        if not isinstance(raw_heads, list):
            raise ValueError("previous settlement heads differ")
        for item in raw_heads:
            path = Path(str(item))
            _validated_head_chain(root, _relative_root(root, path, "settlement head"))
            heads.append(path)
    baseline_path = persist_calibration_report(root, build_calibration_report(root, claims, heads))
    kwargs = {}
    if fetcher is not None:
        kwargs["fetcher"] = fetcher
    if durable_clock is not None:
        kwargs["durable_clock"] = durable_clock
    collector_error = None
    collected = None
    try:
        collected = collect_results(root, baseline_path, **kwargs)
    except CollectionFailure as exc:
        collector_error = str(exc)
        collected = exc.result
    if collected is None or collected.report_relative is None:
        report_path = baseline_path
    else:
        report_path = collected.report_relative
    report = _report_value(root, report_path)
    head_paths = [Path(str(item)) for item in report.get("settlement_heads", [])]
    attachments = [Path(str(item)) for item in pregame.get("shadow_attachments", [])]
    shadow_report_path = None
    shadow_error = None
    try:
        shadow_report_path = persist_shadow_paired_report(root, build_shadow_paired_report(root, attachments, head_paths))
    except Exception as exc:
        shadow_error = str(exc)
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "phase": "POSTGAME",
        "target_date": pregame["target_date"],
        "pregame_receipt_ref": _ref(root, pregame_rel, "pregame receipt"),
        "baseline_report_ref": _ref(root, report_path, "calibration report"),
        "shadow_report_ref": None if shadow_report_path is None else _ref(root, shadow_report_path, "shadow report"),
        "settlement_heads": [p.as_posix() for p in head_paths],
        "dispositions": [] if collected is None else [getattr(item, "__dict__", {}) for item in collected.dispositions],
        "errors": [item for item in (collector_error, shadow_error) if item],
        "workflow_complete": bool(collected is not None and collector_error is None and shadow_error is None),
        "content_sha256": "",
    }
    # slots dataclasses have no __dict__; serialize disposition attributes explicitly.
    if collected is not None:
        receipt["dispositions"] = [{"game_pk": item.game_pk, "snapshot_relative": item.snapshot_relative, "state": item.state, "receipt_relative": item.receipt_relative, "head_relative": item.head_relative, "reason": item.reason} for item in collected.dispositions]
    receipt["content_sha256"] = _digest(receipt)
    path = _persist_receipt(root, receipt)
    return PostgameResult(path, receipt)


def read_status(root: Path | str, pregame_receipt: Path | str, *, postgame_receipt: Path | str | None = None, clock: Callable[[], str] = _now, competition_scope: str = "regular-season") -> dict[str, object]:
    """Read and validate exact receipts without writes or network access."""
    if competition_scope == "postseason-research-v1":
        from .prospective_ml_postseason_research import read_research_status
        return read_research_status(root, pregame_receipt, postgame_receipt=postgame_receipt, clock=clock)
    if competition_scope != "regular-season":
        raise ValueError("competition scope differs")
    root = normalize_root(root)
    pregame_rel = _relative_root(root, pregame_receipt, "pregame receipt")
    pregame = _read_receipt(root, pregame_rel, "pregame receipt")
    _validate_pregame(root, pregame)
    postgame = None
    if postgame_receipt is not None:
        postgame = _read_receipt(root, postgame_receipt, "postgame receipt")
        _validate_postgame(root, postgame, pregame, pregame_rel)
    now = canonical_utc(clock())
    head_games: dict[int, Mapping[str, object]] = {}
    dispositions: dict[int, str] = {}
    postgame_errors: list[object] = []
    if postgame is not None:
        for head in postgame.get("settlement_heads", []):
            record = _validated_head_chain(root, _relative_root(root, str(head), "settlement head"))
            head_games[int(record["result"]["game_pk"])] = record
        raw_dispositions = postgame.get("dispositions")
        if isinstance(raw_dispositions, list):
            for disposition in raw_dispositions:
                if isinstance(disposition, Mapping) and type(disposition.get("game_pk")) is int and isinstance(disposition.get("state"), str):
                    dispositions[int(disposition["game_pk"])] = disposition["state"]
        raw_errors = postgame.get("errors")
        if isinstance(raw_errors, list):
            postgame_errors = list(raw_errors)
    result_rows = []
    complete_count = 0
    for game in pregame["games"]:
        assert isinstance(game, Mapping)
        missing: list[str] = []
        if game.get("primary_claim_relative") is None:
            reasons = game.get("reason_codes") or ["not eligible"]
            missing.append("BASELINE_NOT_PRIMARY " + ",".join(str(item) for item in reasons))
        attachment = game.get("shadow_attachment_relative")
        if attachment is None:
            missing.append("AFTER_START_IMPOSSIBLE" if canonical_utc_ns(now) >= canonical_utc_ns(str(game["scheduled_start_utc"])) else "SHADOW_MISSING")
        odds = str(game.get("odds_status", "MISSING"))
        snapshot = load_ml_snapshot(root, str(game["snapshot_relative"]))
        overlay = _evaluated_market_overlay(snapshot)
        has_selected_price = overlay is not None and overlay["decisions"][0].get("selected_price") is not None
        if odds != "EVALUATED":
            missing.append("ODDS_" + odds)
            if overlay is None:
                missing.append("MARKET_TRACE_MISSING")
        elif not has_selected_price:
            missing.append("ODDS_MISSING")
        elif pregame.get("market_trace_ref") is None:
            missing.append("MARKET_TRACE_MISSING")
        head = head_games.get(int(game["game_pk"]))
        if head is None:
            if dispositions.get(int(game["game_pk"])) == "FAILED":
                missing.append("RESULT_FETCH_FAILED")
            missing.append("RESULT_LINK_MISSING" if postgame is None else "RESULT_LINK_MISSING/PENDING")
        elif head.get("result", {}).get("status") != "FINAL":
            missing.append("RESULT_LINK_MISSING/PENDING")
        if attachment is not None and (head is None or head.get("result", {}).get("status") != "FINAL" or postgame is None or postgame.get("shadow_report_ref") is None):
            missing.append("SHADOW_PAIR_PENDING")
        if not missing:
            missing.append("COMPLETE")
            complete_count += 1
        result_rows.append({"game_pk": game["game_pk"], "missing_steps": missing, "workflow_processed": postgame is not None, "evidence_complete": missing == ["COMPLETE"]})
    return {"schema": "PROSPECTIVE_ML_DAILY_FLOW_STATUS_V1", "pregame_receipt": pregame_rel.as_posix(), "postgame_receipt": None if postgame_receipt is None else _relative_root(root, postgame_receipt, "postgame receipt").as_posix(), "workflow_processed": postgame is not None, "workflow_complete": bool(postgame is not None and postgame.get("workflow_complete") is True), "errors": postgame_errors, "complete_evidence": complete_count == len(result_rows) and bool(result_rows), "games": result_rows}


# Short aliases make the programmatic API mirror the three CLI subcommands.
pregame = run_pregame
postgame = run_postgame
status = read_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coordinate explicit prospective ML daily lifecycle")
    sub = parser.add_subparsers(dest="command", required=True)
    pre = sub.add_parser("pregame")
    pre.add_argument("--root", required=True)
    pre.add_argument("--ml-trace", required=True)
    pre.add_argument("--market-trace")
    pre.add_argument("--market-raw-capture", action="append", default=[])
    pre.add_argument("--market-unavailable-record", action="append", default=[])
    post = sub.add_parser("postgame")
    post.add_argument("--root", required=True)
    post.add_argument("--pregame-receipt", required=True)
    post.add_argument("--previous-postgame-receipt")
    stat = sub.add_parser("status")
    stat.add_argument("--root", required=True)
    stat.add_argument("--pregame-receipt", required=True)
    stat.add_argument("--postgame-receipt")
    for scope_parser in (pre, post, stat):
        scope_parser.add_argument("--competition-scope", choices=("regular-season", "postseason-research-v1"), default="regular-season")
    args = parser.parse_args(argv)
    try:
        if args.command == "pregame":
            result = run_pregame(args.root, args.ml_trace, market_trace=args.market_trace, market_raw_captures=tuple(args.market_raw_capture), market_unavailable_records=tuple(args.market_unavailable_record), competition_scope=args.competition_scope)
            print(json.dumps({"receipt_relative": result.receipt_relative.as_posix(), "games": result.receipt["games"]}, sort_keys=True))
        elif args.command == "postgame":
            result = run_postgame(args.root, args.pregame_receipt, previous_postgame_receipt=args.previous_postgame_receipt, competition_scope=args.competition_scope)
            print(json.dumps({"receipt_relative": result.receipt_relative.as_posix(), "workflow_complete": result.receipt["workflow_complete"]}, sort_keys=True))
        else:
            print(json.dumps(read_status(args.root, args.pregame_receipt, postgame_receipt=args.postgame_receipt, competition_scope=args.competition_scope), sort_keys=True))
        return 0
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
