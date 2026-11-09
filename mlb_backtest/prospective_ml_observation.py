# ruff: noqa: E701, E702
"""Append-only, outcome-free recording of sealed live-model observations.

This module deliberately has no result-store, collector, or scoring dependency.
It only admits an already sealed ML trace and records what was knowable before a
game starts.  Settlement lives in :mod:`prospective_ml_settlement`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .market_play_contracts import canonical_json_bytes, canonical_utc, canonical_utc_ns, validate_relative_path
from .secure_fs import open_trusted_root, read_regular_snapshot, rebind_regular_snapshot, write_exclusive


SNAPSHOT_SCHEMA = "PROSPECTIVE_ML_OBSERVATION_SNAPSHOT_V1"
AUDIT_ROOT = Path("audit/prospective_ml_observation")
MAX_BYTES = 64 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")
_CLAIM_LOCKS: dict[tuple[str, int, str], threading.Lock] = {}
_CLAIM_LOCKS_GUARD = threading.Lock()
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class BuiltMlObservations:
    root: Path
    ml_trace: Path
    market_trace: Path | None
    observed_clock: str
    snapshots: tuple[dict[str, object], ...]
    snapshot_bytes: tuple[bytes, ...]

    @property
    def candidates(self) -> tuple[dict[str, object], ...]:
        return self.snapshots


@dataclass(frozen=True, slots=True)
class PersistedMlObservations:
    candidate_paths: tuple[Path, ...]
    primary_claim_paths: tuple[Path, ...]
    primary_candidate_paths: tuple[Path, ...]
    snapshots: tuple[dict[str, object], ...]

    @property
    def primary_game_pks(self) -> tuple[int, ...]:
        return tuple(int(path.parent.name) for path in self.primary_claim_paths)

    @property
    def primary_snapshots(self) -> tuple[Path, ...]:
        return self.primary_candidate_paths

    @property
    def candidate_snapshots(self) -> tuple[Path, ...]:
        return self.candidate_paths

    @property
    def diagnostic_snapshots(self) -> tuple[Path, ...]:
        claimed = set(self.primary_candidate_paths)
        return tuple(path for path in self.candidate_paths if path not in claimed)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _relative(root: Path, value: Path | str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        try:
            path = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
        except ValueError as exc:
            raise ValueError(f"{label} escapes root") from exc
    return validate_relative_path(path)


def _hex(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        raise ValueError(f"{label} differs")
    return value


def _json_snapshot(root: Path, relative: Path, label: str) -> tuple[bytes, dict[str, object]]:
    with open_trusted_root(root) as trusted:
        snap = read_regular_snapshot(trusted, relative, label, max_bytes=MAX_BYTES)
    if snap is None:
        raise ValueError(f"{label} is missing")
    try:
        value = json.loads(snap.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return snap.contents, value


def _finite_probability(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} differs")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} differs") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{label} differs")
    return number


def _canonical_digest(value: Mapping[str, object]) -> str:
    return _sha(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"}))


def _participant(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "away_lineup", "home_lineup", "away_observed_starter_id", "home_observed_starter_id",
        "away_expected_starter_id", "home_expected_starter_id", "payload_relative", "payload_sha256",
        "receipt_relative", "receipt_sha256", "durable_at_utc",
    }:
        raise ValueError("snapshot participant differs")
    safe = json.loads(canonical_json_bytes(value))
    for lineup in (safe["away_lineup"], safe["home_lineup"]):
        if not isinstance(lineup, list) or (lineup and (len(lineup) != 9 or len(set(lineup)) != 9 or any(type(x) is not int or x <= 0 for x in lineup))):
            raise ValueError("snapshot lineup differs")
    for key in ("away_observed_starter_id", "home_observed_starter_id", "away_expected_starter_id", "home_expected_starter_id"):
        if safe[key] is not None and (type(safe[key]) is not int or safe[key] <= 0):
            raise ValueError("snapshot starter differs")
    for key in ("payload_relative", "receipt_relative"):
        if safe[key] is not None and not isinstance(safe[key], str):
            raise ValueError("snapshot participant path differs")
        if safe[key] is not None: validate_relative_path(Path(safe[key]))
    for key in ("payload_sha256", "receipt_sha256"):
        if safe[key] is not None: _hex(safe[key], f"snapshot participant {key}")
    if (safe["payload_relative"] is None) != (safe["payload_sha256"] is None) or (safe["receipt_relative"] is None) != (safe["receipt_sha256"] is None): raise ValueError("snapshot participant binding differs")
    if safe["durable_at_utc"] is not None: canonical_utc(safe["durable_at_utc"])
    return safe


def validate_ml_snapshot(value: object) -> dict[str, object]:
    required = {
        "schema", "content_sha256", "observed_at_utc", "target_date", "game_pk", "scheduled_start_utc",
        "away_team", "home_team", "model_card_sha256", "ml_trace_relative", "ml_trace_sha256", "p_home",
        "predicted_side", "pick_probability", "data_completeness", "participant", "market", "market_status", "totals_status", "primary_eligible", "reason_codes",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise ValueError("snapshot fields differ")
    safe = json.loads(canonical_json_bytes(value))
    if safe["schema"] != SNAPSHOT_SCHEMA or safe["content_sha256"] != _canonical_digest(safe):
        raise ValueError("snapshot schema or digest differs")
    canonical_utc(safe["observed_at_utc"]); canonical_utc(safe["scheduled_start_utc"])
    if not isinstance(safe["target_date"], str) or len(safe["target_date"]) != 10:
        raise ValueError("snapshot target date differs")
    if type(safe["game_pk"]) is not int or safe["game_pk"] <= 0 or any(not isinstance(safe[x], str) or not safe[x] for x in ("away_team", "home_team")):
        raise ValueError("snapshot identity differs")
    for key in ("model_card_sha256", "ml_trace_sha256"):
        _hex(safe[key], key)
    if not isinstance(safe["ml_trace_relative"], str):
        raise ValueError("snapshot trace differs")
    validate_relative_path(Path(safe["ml_trace_relative"]))
    if safe["p_home"] is None or safe["predicted_side"] is None or safe["pick_probability"] is None:
        if any(safe[key] is not None for key in ("p_home", "predicted_side", "pick_probability")): raise ValueError("snapshot model fields differ")
    else:
        _finite_probability(safe["p_home"], "p_home")
        if safe["predicted_side"] not in {"away", "home"}: raise ValueError("snapshot predicted side differs")
        p_home = _finite_probability(safe["p_home"], "p_home")
        if _finite_probability(safe["pick_probability"], "pick probability") != (p_home if safe["predicted_side"] == "home" else 1.0 - p_home): raise ValueError("snapshot pick probability differs")
    if safe["data_completeness"] is not None: _finite_probability(safe["data_completeness"], "data completeness")
    _participant(safe["participant"])
    market = safe["market"]
    if not isinstance(market, Mapping) or set(market) != {"status", "trace_relative", "trace_sha256", "decisions"}:
        raise ValueError("snapshot market differs")
    if market["status"] == "NOT_EVALUATED":
        if any(market[k] is not None for k in ("trace_relative", "trace_sha256", "decisions")):
            raise ValueError("not evaluated market differs")
    elif market["status"] == "EVALUATED":
        if not isinstance(market["trace_relative"], str) or not isinstance(market["decisions"], list):
            raise ValueError("copied market differs")
        validate_relative_path(Path(market["trace_relative"])); _hex(market["trace_sha256"], "market trace digest")
    else:
        raise ValueError("snapshot market status differs")
    if safe["market_status"] != market["status"] or safe["totals_status"] != "NOT_EVALUATED":
        raise ValueError("snapshot market/totals status differs")
    if type(safe["primary_eligible"]) is not bool or not isinstance(safe["reason_codes"], list) or any(not isinstance(x, str) or not x for x in safe["reason_codes"]):
        raise ValueError("snapshot eligibility differs")
    return safe


def load_ml_snapshot(root: Path, snapshot: Path | str) -> dict[str, object]:
    root = Path(root); relative = _relative(root, snapshot, "snapshot")
    raw, value = _json_snapshot(root, relative, "snapshot")
    if canonical_json_bytes(value) != raw:
        raise ValueError("snapshot is not canonical")
    return validate_ml_snapshot(value)


def validate_primary_claim(root: Path, claim: Path | str) -> dict[str, object]:
    root = Path(root); relative = _relative(root, claim, "primary claim")
    raw, value = _json_snapshot(root, relative, "primary claim")
    required = {"schema", "game_pk", "model_card_sha256", "candidate_relative", "candidate_sha256", "claimed_at_utc"}
    if set(value) != required or canonical_json_bytes(value) != raw or value.get("schema") != "PROSPECTIVE_ML_PRIMARY_CLAIM_V1": raise ValueError("primary claim differs")
    if type(value.get("game_pk")) is not int or not isinstance(value.get("model_card_sha256"), str): raise ValueError("primary claim differs")
    expected = AUDIT_ROOT / "primary_claims" / str(value["game_pk"]) / f"{value['model_card_sha256']}.json"
    if relative != expected: raise ValueError("primary claim path differs")
    canonical_utc(value["claimed_at_utc"])
    candidate = _relative(root, str(value["candidate_relative"]), "primary candidate")
    snapshot = load_ml_snapshot(root, candidate)
    if value["candidate_sha256"] != _sha(canonical_json_bytes(snapshot)) or snapshot["game_pk"] != value["game_pk"] or snapshot["model_card_sha256"] != value["model_card_sha256"]: raise ValueError("primary claim binding differs")
    return value


def _market(root: Path, trace: Path | None, ml_relative: Path, ml_sha: str, observed: str, game_pk: int) -> dict[str, object]:
    if trace is None:
        return {"status": "NOT_EVALUATED", "trace_relative": None, "trace_sha256": None, "decisions": None}
    relative = _relative(root, trace, "market trace")
    raw, value = _json_snapshot(root, relative, "market trace")
    # Existing trace contracts bind exactly to the ML trace.  Keeping this check
    # local also rejects a superficially valid trace for another ML run.
    from .daily_multibook_market_plays import validate_market_plays_trace, validate_market_plays_report
    trace_value = validate_market_plays_trace(value)
    report_relative = _relative(root, str(trace_value["report_relative"]), "market report")
    report_raw, report = _json_snapshot(root, report_relative, "market report")
    report = validate_market_plays_report(report)
    if _sha(report_raw) != trace_value["report_sha256"] or report["ml_trace_relative"] != ml_relative.as_posix() or report["ml_trace_sha256"] != ml_sha:
        raise ValueError("market trace ML binding differs")
    if canonical_utc_ns(str(report["evaluated_at_utc"])) > canonical_utc_ns(observed):
        raise ValueError("market trace is observed after ML snapshot")
    decisions = [row for row in report["moneylines"] if row.get("game_pk") == game_pk]
    if len(decisions) != 1:
        raise ValueError("market trace game binding differs")
    return {"status": "EVALUATED", "trace_relative": relative.as_posix(), "trace_sha256": _sha(raw), "decisions": decisions}


def _reasons(game: object, observed: str) -> list[str]:
    reasons: list[str] = []
    if game.model_status != "SCORED" or game.p_home is None or game.predicted_side is None or game.pick_probability is None:
        reasons.append("MODEL_UNSCORED")
    participant = game.participant
    if not participant.complete: reasons.append("INCOMPLETE_PARTICIPANTS")
    if not participant.starter_consistent: reasons.append("STARTER_CONFLICT")
    if participant.late_scratch: reasons.append("LATE_SCRATCH")
    if len(participant.away_lineup) != 9 or len(set(participant.away_lineup)) != 9 or len(participant.home_lineup) != 9 or len(set(participant.home_lineup)) != 9:
        reasons.append("INVALID_LINEUPS")
    if any(x is None for x in (participant.away_observed_starter_id, participant.home_observed_starter_id, participant.away_expected_starter_id, participant.home_expected_starter_id)) or participant.away_observed_starter_id != participant.away_expected_starter_id or participant.home_observed_starter_id != participant.home_expected_starter_id:
        reasons.append("STARTER_MISMATCH")
    if not participant.payload_relative or not participant.payload_sha256 or not participant.receipt_relative or not participant.receipt_sha256 or not participant.durable_at_utc:
        reasons.append("MISSING_PREGAME_EVIDENCE")
    else:
        if canonical_utc_ns(participant.durable_at_utc) > canonical_utc_ns(observed): reasons.append("PREGAME_EVIDENCE_AFTER_OBSERVATION")
    if canonical_utc_ns(observed) >= canonical_utc_ns(game.scheduled_start_utc): reasons.append("GAME_STARTED")
    return sorted(set(reasons))


def build_ml_observations(root: Path, ml_trace: Path | str, *, market_trace: Path | str | None = None, observed_clock: str | Callable[[], str]) -> BuiltMlObservations:
    root = Path(root); observed = canonical_utc(observed_clock() if callable(observed_clock) else observed_clock); ml_relative = _relative(root, ml_trace, "ML trace")
    # The sealed-authority adapter imports acquisition helpers for other
    # protocols; keep that dependency out of this outcome-free module import.
    from .market_play_authorities import load_ml_authority
    authority = load_ml_authority(root, ml_relative)
    raw_trace, _ = _json_snapshot(root, ml_relative, "ML trace")
    model_sha = _hex(authority.model_card_sha256, "model card digest")
    values: list[dict[str, object]] = []
    raws: list[bytes] = []
    for game in authority.games:
        p = game.participant
        reasons = _reasons(game, observed)
        row: dict[str, object] = {
            "schema": SNAPSHOT_SCHEMA, "observed_at_utc": observed, "target_date": authority.target_date,
            "game_pk": game.game_pk, "scheduled_start_utc": game.scheduled_start_utc, "away_team": game.away_team, "home_team": game.home_team,
            "model_card_sha256": model_sha, "ml_trace_relative": ml_relative.as_posix(), "ml_trace_sha256": _sha(raw_trace),
            "p_home": None if game.p_home is None else str(float(game.p_home)), "predicted_side": game.predicted_side, "pick_probability": None if game.pick_probability is None else str(float(game.pick_probability)), "data_completeness": None if game.data_completeness is None else str(float(game.data_completeness)),
            "participant": {"away_lineup": list(p.away_lineup), "home_lineup": list(p.home_lineup), "away_observed_starter_id": p.away_observed_starter_id, "home_observed_starter_id": p.home_observed_starter_id, "away_expected_starter_id": p.away_expected_starter_id, "home_expected_starter_id": p.home_expected_starter_id, "payload_relative": None if p.payload_relative is None else p.payload_relative.as_posix(), "payload_sha256": p.payload_sha256, "receipt_relative": None if p.receipt_relative is None else p.receipt_relative.as_posix(), "receipt_sha256": p.receipt_sha256, "durable_at_utc": p.durable_at_utc},
            "market": _market(root, None if market_trace is None else Path(market_trace), ml_relative, _sha(raw_trace), observed, game.game_pk), "market_status": "NOT_EVALUATED" if market_trace is None else "EVALUATED", "totals_status": "NOT_EVALUATED", "primary_eligible": not reasons, "reason_codes": reasons,
        }
        row["content_sha256"] = _canonical_digest(row)
        row = validate_ml_snapshot(row); values.append(row); raws.append(canonical_json_bytes(row))
    return BuiltMlObservations(root, ml_relative, None if market_trace is None else _relative(root, market_trace, "market trace"), observed, tuple(values), tuple(raws))


def _write_idempotent(trusted, relative: Path, contents: bytes, label: str) -> None:
    with _WRITE_LOCK:
        try:
            write_exclusive(trusted, relative, contents, label)
        except FileExistsError:
            current = read_regular_snapshot(trusted, relative, label, max_bytes=len(contents))
            if current is None or current.contents != contents: raise ValueError(f"{label} differs")
            rebind_regular_snapshot(trusted, current, label, max_bytes=len(contents))


def _claim_lock(root: Path, game_pk: int, model_sha: str) -> threading.Lock:
    key = (root.as_posix(), game_pk, model_sha)
    with _CLAIM_LOCKS_GUARD:
        return _CLAIM_LOCKS.setdefault(key, threading.Lock())


def persist_ml_observations(root: Path, built: BuiltMlObservations, *, primary_clock: Callable[[], str]) -> PersistedMlObservations:
    root = Path(root)
    if not callable(primary_clock): raise TypeError("primary_clock must be callable")
    if not isinstance(built, BuiltMlObservations) or Path(built.root) != root: raise ValueError("built observations differ")
    rebuilt = build_ml_observations(root, built.ml_trace, market_trace=built.market_trace, observed_clock=built.observed_clock)
    if rebuilt.snapshot_bytes != built.snapshot_bytes: raise ValueError("built observation bytes differ")
    # Rebind every sealed authority immediately before the append/claim boundary.
    from .market_play_authorities import load_ml_authority, rebind_authorities
    authority = load_ml_authority(root, built.ml_trace); rebind_authorities(authority)
    candidates: list[Path] = []; claims: list[Path] = []; primary_candidates: list[Path] = []
    with open_trusted_root(root) as trusted:
        for row, raw in zip(built.snapshots, built.snapshot_bytes, strict=True):
            authority.rebind()
            row = validate_ml_snapshot(row); digest = _sha(raw)
            if canonical_json_bytes(row) != raw: raise ValueError("built observation transport differs")
            candidate = AUDIT_ROOT / "candidates" / str(row["target_date"]) / str(row["game_pk"]) / str(row["model_card_sha256"]) / f"{digest}.json"
            _write_idempotent(trusted, candidate.with_suffix(".json.sha256"), f"{digest}\n".encode(), "ML observation sidecar")
            _write_idempotent(trusted, candidate, raw, "ML observation candidate")
            candidates.append(candidate)
            primary = canonical_utc(primary_clock())
            eligible_now = bool(row["primary_eligible"]) and canonical_utc_ns(primary) < canonical_utc_ns(str(row["scheduled_start_utc"]))
            if not eligible_now: continue
            claim = AUDIT_ROOT / "primary_claims" / str(row["game_pk"]) / f"{row['model_card_sha256']}.json"
            claim_body = {"schema": "PROSPECTIVE_ML_PRIMARY_CLAIM_V1", "game_pk": row["game_pk"], "model_card_sha256": row["model_card_sha256"], "candidate_relative": candidate.as_posix(), "candidate_sha256": digest, "claimed_at_utc": primary}
            claim_raw = canonical_json_bytes(claim_body)
            with _claim_lock(root, int(row["game_pk"]), str(row["model_card_sha256"])):
                try:
                    # final rebind prevents a poisoned claim if input changes during write.
                    write_exclusive(trusted, claim, claim_raw, "ML primary claim", final_rebind=authority.rebind)
                    claims.append(claim)
                    primary_candidates.append(candidate)
                except FileExistsError:
                    validate_primary_claim(root, claim)
                    current = read_regular_snapshot(trusted, claim, "ML primary claim", max_bytes=MAX_BYTES)
                    if current is None: raise ValueError("ML primary claim missing")
                    try: existing = json.loads(current.contents)
                    except json.JSONDecodeError as exc: raise ValueError("ML primary claim invalid") from exc
                    if canonical_json_bytes(existing) != current.contents or existing.get("schema") != "PROSPECTIVE_ML_PRIMARY_CLAIM_V1" or existing.get("game_pk") != row["game_pk"] or existing.get("model_card_sha256") != row["model_card_sha256"] or existing.get("claimed_at_utc") is None:
                        raise ValueError("ML primary claim invalid")
                    canonical_utc(existing["claimed_at_utc"])
                    if existing.get("candidate_sha256") == digest and existing.get("candidate_relative") == candidate.as_posix():
                        claims.append(claim); primary_candidates.append(candidate)
        authority.rebind()
    return PersistedMlObservations(tuple(candidates), tuple(claims), tuple(primary_candidates), built.snapshots)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record sealed pregame ML observations")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot"); snap.add_argument("--root", required=True); snap.add_argument("--ml-trace", required=True); snap.add_argument("--market-trace")
    args = parser.parse_args(argv)
    root = Path(args.root); now = _now()
    built = build_ml_observations(root, args.ml_trace, market_trace=args.market_trace, observed_clock=now)
    persisted = persist_ml_observations(root, built, primary_clock=_now)
    for path in persisted.candidate_paths: print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
