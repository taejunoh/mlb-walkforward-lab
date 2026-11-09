"""Authenticated append-only prospective evidence for one isolated research policy."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from collections.abc import Mapping
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .prospective_ml_daily_flow import PregameResult, PostgameResult

from .market_play_contracts import canonical_json_bytes, canonical_utc, canonical_utc_ns
from .prospective_ml_observation import _relative, _hex, _participant, _finite_probability
from .secure_fs import open_trusted_root, open_parent, read_regular_snapshot, rebind_regular_snapshot, write_exclusive

ROOT = Path("audit/prospective_ml_postseason_research")
REPORT_ROOT = Path("reports/prospective_ml_postseason_research")
SNAPSHOT_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_SNAPSHOT_V1"
CLAIM_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_CLAIM_V1"
CLAIM_INDEX_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_CLAIM_INDEX_V1"
CLAIM_RESERVATION_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_CLAIM_RESERVATION_V1"
RECEIPT_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_RECEIPT_V1"
POLICY_KEYS = {"competition_scope", "research_policy_version", "history_population", "validation_status", "research_status", "domain_status"}
GAME_KEYS = {"game_pk", "game_type", "snapshot_relative", "claim_relative", "state", "reason_codes", "settlement_head", "market_status", "market_reason", "totals_status", "totals_reason", "shadow_status"}
RECEIPT_KEYS = {"schema", *POLICY_KEYS, "phase", "created_at_utc", "pregame_receipt", "previous_postgame_receipt", "ml_trace_relative", "ml_trace_sha256", "candidates", "primary_claims", "settlement_heads", "report_relative", "games", "errors", "workflow_complete", "content_sha256"}
_LOCK = threading.RLock()


def detach(value):
    if isinstance(value, Mapping):
        return {key: detach(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [detach(item) for item in value]
    return value


def policy():
    from .competition_policy import POSTSEASON_ML_RESEARCH_V1, history_population
    return {"competition_scope": "postseason-research-v1", "research_policy_version": "POSTSEASON_ML_RESEARCH_V1", "history_population": history_population(POSTSEASON_ML_RESEARCH_V1), "validation_status": "NOT_POSTSEASON_VALIDATED", "research_status": "RESEARCH_ONLY", "domain_status": "POSTSEASON_OUT_OF_DOMAIN"}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def digest(value):
    return sha(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"}))


def seal(value):
    value = detach(value)
    value["content_sha256"] = digest(value)
    return value


def envelope(value, keys, schema):
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("research artifact fields differ")
    value = detach(value)
    if value["schema"] != schema or any(value[key] != item for key, item in policy().items()) or value["content_sha256"] != digest(value):
        raise ValueError("research artifact policy/schema/digest differs")
    return value


def read_bytes(root, path, label, *, missing_ok=False):
    path = _relative(Path(root), path, label)
    with open_trusted_root(Path(root)) as trusted:
        snapshot = read_regular_snapshot(trusted, path, label, max_bytes=64 * 1024 * 1024, missing_ok=missing_ok)
        if snapshot is None:
            return None
        rebind_regular_snapshot(trusted, snapshot, label, max_bytes=64 * 1024 * 1024)
        return snapshot.contents


def read_json(root, path, label):
    raw = read_bytes(root, path, label)
    value = json.loads(raw)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        raise ValueError(f"{label} is not canonical")
    sidecar = read_bytes(root, Path(path).with_suffix(Path(path).suffix + ".sha256"), label + " seal")
    if sidecar != f"{sha(raw)}\n".encode():
        raise ValueError(f"{label} seal differs")
    return value


def write_bytes(root, path, raw, label, rebind=None):
    with open_trusted_root(Path(root)) as trusted:
        try:
            write_exclusive(trusted, path, raw, label, final_rebind=rebind)
        except FileExistsError:
            old = read_regular_snapshot(trusted, path, label, max_bytes=len(raw))
            if old is None or old.contents != raw:
                raise ValueError(f"{label} conflicts")
            rebind_regular_snapshot(trusted, old, label, max_bytes=len(raw))
            if rebind:
                rebind()
    # write_exclusive's callback runs before directory fsync and final return
    # revalidation. Publication admission must also observe the actual return.
    if rebind:
        rebind()


def write_json(root, path, value, label, rebind=None):
    raw = canonical_json_bytes(value)
    if rebind:
        rebind()
    write_bytes(root, path, raw, label, rebind)
    # A partial raw write is diagnostic evidence, not an authenticated artifact.
    # Never publish the seal until durable write and final admission succeed.
    write_bytes(root, path.with_suffix(path.suffix + ".sha256"), f"{sha(raw)}\n".encode(), label + " seal", rebind)


@contextmanager
def identity_lock(root, identity):
    # A separate policy-bound lock namespace; O_NOFOLLOW and link-count checks
    # prevent lock substitution. File exclusivity remains the cross-process gate.
    from .live_sources import _open_observation_claim_lock, _validate_lock_file
    with _LOCK, open_trusted_root(root) as trusted:
        path = ROOT / "locks" / f"{identity}.lock"
        with open_parent(trusted, path, create=True) as parent:
            fd = _open_observation_claim_lock(parent, path.name)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                _validate_lock_file(fd, parent, path.name)
                trusted.assert_bound()
                yield
                _validate_lock_file(fd, parent, path.name)
                trusted.assert_bound()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)


def claim_identity(game_pk, model_card_sha256):
    return sha(canonical_json_bytes({"game_pk": game_pk, "model_card_sha256": model_card_sha256, "competition_scope": "postseason-research-v1", "research_policy_version": "POSTSEASON_ML_RESEARCH_V1"}))


def _authority(root, trace):
    from .postseason_research_contracts import load_postseason_research
    return load_postseason_research(root, trace)


def _participant_for(root, report, game):
    from .competition_policy import POSTSEASON_ML_RESEARCH_V1
    from .live_sources import parse_live_schedule, parse_pregame_boxscore, compare_probable_pitchers
    empty = {"away_lineup": [], "home_lineup": [], "away_observed_starter_id": None, "home_observed_starter_id": None, "away_expected_starter_id": None, "home_expected_starter_id": None, "payload_relative": None, "payload_sha256": None, "receipt_relative": None, "receipt_sha256": None, "durable_at_utc": None}
    bindings = report["source_bindings"]
    participants = [b for b in bindings if b["binding_role"] == "LIVE_PARTICIPANTS" and game["game_pk"] in b["game_pks"]]
    schedules = [b for b in bindings if b["binding_role"] == "LIVE_SCHEDULE" and game["game_pk"] in b["game_pks"]]
    if len(participants) != 1 or len(schedules) != 1:
        return empty, ["MISSING_PARTICIPANT_EVIDENCE"]
    b = participants[0]
    schedule = parse_live_schedule(read_bytes(root, schedules[0]["payload_relative"], "research schedule"), requested_date=report["target_date"], competition_policy=POSTSEASON_ML_RESEARCH_V1)
    s = next(row for row in schedule if row["game_pk"] == game["game_pk"])
    p = compare_probable_pitchers(s, parse_pregame_boxscore(read_bytes(root, b["payload_relative"], "research participants"), game_pk=game["game_pk"]))
    empty.update({"away_lineup": list(p.away_lineup), "home_lineup": list(p.home_lineup), "away_observed_starter_id": p.away_pitcher_list_id, "home_observed_starter_id": p.home_pitcher_list_id, "away_expected_starter_id": s.get("away_probable_pitcher_id"), "home_expected_starter_id": s.get("home_probable_pitcher_id"), "payload_relative": b["payload_relative"], "payload_sha256": b["payload_sha256"], "receipt_relative": b["receipt_relative"], "receipt_sha256": b["receipt_sha256"], "durable_at_utc": b["durable_at_utc"]})
    reasons = [reason for reason in p.reason_codes if reason != "PROBABLE_CONSISTENT"]
    if not p.complete:
        reasons.append("INCOMPLETE_PARTICIPANTS")
    for side in ("away", "home"):
        if empty[f"{side}_expected_starter_id"] is None or empty[f"{side}_expected_starter_id"] != empty[f"{side}_observed_starter_id"]:
            reasons.append("STARTER_MISMATCH")
    return empty, reasons


def build_snapshot(root, authority, game, observed):
    report = detach(authority.report)
    participant, reasons = _participant_for(root, report, game)
    reasons += [reason for reason in game["reason_codes"] if reason != "PROBABLE_CONSISTENT"]
    if game["state"] != "VERIFIED" or game["predicted_side"] not in {"away", "home"}:
        reasons.append("MODEL_NOT_VERIFIED")
    if game["game_type"] not in {"F", "D", "L", "W"}:
        reasons.append("UNSUPPORTED_GAME_TYPE")
    start = canonical_utc_ns(game["scheduled_start_utc"])
    observation = canonical_utc_ns(observed)
    boundaries = [report["executed_at_utc"], game["observation_boundary_utc"]]
    required_bindings = set(game["source_binding_sha256s"])
    boundaries += [b[k] for b in report["source_bindings"] for k in ("response_completed_at_utc", "durable_at_utc") if b["binding_sha256"] in required_bindings]
    if observation >= start:
        reasons.append("GAME_STARTED")
    for boundary in boundaries:
        if boundary is None:
            reasons.append("MISSING_OBSERVATION_BOUNDARY")
        elif canonical_utc_ns(boundary) >= start:
            reasons.append("SOURCE_AT_OR_AFTER_START")
        elif canonical_utc_ns(boundary) > observation:
            reasons.append("SOURCE_AFTER_OBSERVATION")
    model = {key: game[key] for key in ("p_home", "predicted_side", "pick_probability")}
    if model["predicted_side"] not in {"away", "home"}:
        model = dict.fromkeys(model)
    return seal({"schema": SNAPSHOT_SCHEMA, **policy(), "observed_at_utc": observed, "target_date": game["target_date"], "game_pk": game["game_pk"], "game_type": game["game_type"], "scheduled_start_utc": game["scheduled_start_utc"], "away_team": game["away_team"], "home_team": game["home_team"], "away_team_id": game["away_team_id"], "home_team_id": game["home_team_id"], "model_card_sha256": report["model_card_sha256"], "feature_sha256": game["feature_sha256"], "observation_boundary_utc": game["observation_boundary_utc"], "ml_trace_relative": authority.trace_relative.as_posix(), "ml_trace_sha256": authority.trace_sha256, **model, "data_completeness": game["data_completeness"], "participant": participant, "market": {"status": "NOT_EVALUATED", "trace_relative": None, "trace_sha256": None, "decisions": None}, "market_status": "NOT_EVALUATED", "totals_status": "NOT_EVALUATED", "primary_eligible": not reasons, "reason_codes": sorted(set(reasons))})


SNAPSHOT_KEYS = {"schema", *POLICY_KEYS, "content_sha256", "observed_at_utc", "target_date", "game_pk", "game_type", "scheduled_start_utc", "away_team", "home_team", "away_team_id", "home_team_id", "model_card_sha256", "feature_sha256", "observation_boundary_utc", "ml_trace_relative", "ml_trace_sha256", "p_home", "predicted_side", "pick_probability", "data_completeness", "participant", "market", "market_status", "totals_status", "primary_eligible", "reason_codes"}


def validate_snapshot(value):
    v = envelope(value, SNAPSHOT_KEYS, SNAPSHOT_SCHEMA)
    date.fromisoformat(v["target_date"])
    for key in ("observed_at_utc", "scheduled_start_utc"):
        canonical_utc(v[key])
    if v["observation_boundary_utc"] is not None:
        canonical_utc(v["observation_boundary_utc"])
    for key in ("model_card_sha256", "ml_trace_sha256"):
        _hex(v[key], key)
    if v["feature_sha256"] is not None:
        _hex(v["feature_sha256"], "feature")
    if any(type(v[k]) is not int or v[k] <= 0 for k in ("game_pk", "away_team_id", "home_team_id")) or not isinstance(v["game_type"], str) or not v["game_type"]:
        raise ValueError("research snapshot identity differs")
    if any(not isinstance(v[k], str) or not v[k] for k in ("away_team", "home_team")) or v["away_team_id"] == v["home_team_id"]:
        raise ValueError("research snapshot teams differ")
    if v["p_home"] is None:
        if v["predicted_side"] is not None or v["pick_probability"] is not None:
            raise ValueError("research snapshot score differs")
    else:
        p = _finite_probability(v["p_home"], "probability")
        if v["predicted_side"] != ("home" if p > .5 else "away" if p < .5 else None) or _finite_probability(v["pick_probability"], "pick probability") != max(p, 1-p):
            raise ValueError("research snapshot forecast differs")
    if v["data_completeness"] is not None:
        _finite_probability(v["data_completeness"], "completeness")
    _participant(v["participant"])
    _relative(Path("."), v["ml_trace_relative"], "trace")
    if v["market"] != {"status": "NOT_EVALUATED", "trace_relative": None, "trace_sha256": None, "decisions": None} or v["market_status"] != "NOT_EVALUATED" or v["totals_status"] != "NOT_EVALUATED":
        raise ValueError("research market/totals differs")
    if type(v["primary_eligible"]) is not bool or not isinstance(v["reason_codes"], list) or any(not isinstance(r, str) or not r for r in v["reason_codes"]) or v["primary_eligible"] != (not v["reason_codes"]):
        raise ValueError("research snapshot eligibility differs")
    return v


def load_snapshot(root, path):
    rel = _relative(root, path, "research snapshot")
    v = validate_snapshot(read_json(root, rel, "research snapshot"))
    expected = ROOT / "candidates" / str(v["game_pk"]) / f"{sha(canonical_json_bytes(v))}.json"
    if rel != expected:
        raise ValueError("research snapshot path differs")
    a = _authority(root, v["ml_trace_relative"])
    games = [detach(g) for g in a.report["games"] if g["game_pk"] == v["game_pk"]]
    if len(games) != 1 or build_snapshot(root, a, games[0], v["observed_at_utc"]) != v:
        raise ValueError("research snapshot source lineage differs")
    if read_json(root, rel, "research snapshot") != v:
        raise ValueError("research snapshot changed during admission")
    return v


def load_claim(root, path):
    rel = _relative(root, path, "research claim")
    v = envelope(read_json(root, rel, "research claim"), {"schema", *POLICY_KEYS, "claim_identity_sha256", "game_pk", "game_type", "model_card_sha256", "snapshot_relative", "snapshot_sha256", "claimed_at_utc", "content_sha256"}, CLAIM_SCHEMA)
    if type(v["game_pk"]) is not int or v["game_pk"] <= 0 or v["game_type"] not in {"F", "D", "L", "W"}:
        raise ValueError("research claim game/type differs")
    for key in ("claim_identity_sha256", "model_card_sha256", "snapshot_sha256"):
        _hex(v[key], key)
    identity = claim_identity(v["game_pk"], v["model_card_sha256"])
    claim_sha = sha(canonical_json_bytes(v))
    if v["claim_identity_sha256"] != identity or rel != ROOT / "primary_claims" / identity / f"{claim_sha}.json":
        raise ValueError("research claim identity/path differs")
    index = _load_claim_index(root, identity)
    if index["claim_relative"] != rel.as_posix() or index["claim_sha256"] != claim_sha:
        raise ValueError("research primary index does not pin exact claim bytes")
    s = load_snapshot(root, v["snapshot_relative"])
    if any(v[k] != s[k] for k in ("game_pk", "game_type", "model_card_sha256")) or v["snapshot_sha256"] != sha(canonical_json_bytes(s)) or not s["primary_eligible"]:
        raise ValueError("research claim snapshot differs")
    if not canonical_utc_ns(s["observed_at_utc"]) <= canonical_utc_ns(v["claimed_at_utc"]) < canonical_utc_ns(s["scheduled_start_utc"]):
        raise ValueError("research claim time differs")
    if not canonical_utc_ns(v["claimed_at_utc"]) <= canonical_utc_ns(index["completed_at_utc"]) < canonical_utc_ns(s["scheduled_start_utc"]):
        raise ValueError("research claim durable completion is not strictly pregame")
    if read_json(root, rel, "research claim") != v:
        raise ValueError("research claim changed during admission")
    return v


def _claim_index_path(identity):
    return ROOT / "primary_claims" / f"{identity}.json"


def _load_claim_index(root, identity):
    path = _claim_index_path(identity)
    v = envelope(read_json(root, path, "research primary completion index"), {"schema", *POLICY_KEYS, "claim_identity_sha256", "claim_relative", "claim_sha256", "completed_at_utc", "content_sha256"}, CLAIM_INDEX_SCHEMA)
    _hex(v["claim_sha256"], "research indexed claim digest")
    canonical_utc(v["completed_at_utc"])
    if v["claim_identity_sha256"] != identity or Path(v["claim_relative"]) != ROOT / "primary_claims" / identity / f"{v['claim_sha256']}.json":
        raise ValueError("research primary completion index identity differs")
    return v


def _require_same_primary_identity(current, original):
    # Participant/feature/probability changes are later diagnostic evidence.
    # Official identity cannot silently redefine an already claimed forecast.
    keys = ("game_pk", "target_date", "game_type", "away_team_id", "home_team_id", "competition_scope", "research_policy_version", "model_card_sha256")
    if any(current[key] != original[key] for key in keys):
        raise ValueError("research first-primary identity differs from current candidate")


def game_row(snapshot, candidate, claim=None, *, state=None, head=None, reasons=None):
    return {"game_pk": snapshot["game_pk"], "game_type": snapshot["game_type"], "snapshot_relative": str(candidate), "claim_relative": None if claim is None else str(claim), "state": state or ("PRIMARY" if claim is not None else "DIAGNOSTIC"), "reason_codes": snapshot["reason_codes"] if reasons is None else reasons, "settlement_head": None if head is None else str(head), "market_status": "NOT_EVALUATED", "market_reason": "PASS — POSTSEASON_NOT_VALIDATED", "totals_status": "NOT_EVALUATED", "totals_reason": "POSTSEASON_TOTALS_NOT_SUPPORTED", "shadow_status": "NOT_EVALUATED_FOR_POSTSEASON"}


def receipt_value(phase, created, trace, trace_sha, **changes):
    value = {"schema": RECEIPT_SCHEMA, **policy(), "phase": phase, "created_at_utc": canonical_utc(created), "pregame_receipt": None, "previous_postgame_receipt": None, "ml_trace_relative": str(trace), "ml_trace_sha256": trace_sha, "candidates": [], "primary_claims": [], "settlement_heads": [], "report_relative": None, "games": [], "errors": [], "workflow_complete": False}
    value.update(changes)
    return seal(value)


def persist_receipt(root, value):
    path = ROOT / "receipts" / f"{sha(canonical_json_bytes(value))}.json"
    write_json(root, path, value, "research receipt")
    return path


def run_research_pregame(root: Path | str, ml_trace: Path | str, *, clock: Callable[[], str]) -> PregameResult:
    from .prospective_ml_daily_flow import PregameResult, normalize_root
    root = normalize_root(root)
    observed = canonical_utc(clock())
    authority = _authority(root, ml_trace)
    candidates, claims, rows = [], [], []
    try:
        for game in authority.report["games"]:
            s = validate_snapshot(build_snapshot(root, authority, detach(game), observed))
            candidate = ROOT / "candidates" / str(s["game_pk"]) / f"{sha(canonical_json_bytes(s))}.json"
            def rebind():
                current = _authority(root, authority.trace_relative)
                if detach(current.report) != detach(authority.report) or current.trace_sha256 != authority.trace_sha256:
                    raise ValueError("research authority changed during publication")
            write_json(root, candidate, s, "research snapshot", rebind)
            candidates.append(candidate.as_posix())
            rows.append(game_row(s, candidate))
            claim = None
            if s["primary_eligible"]:
                identity = claim_identity(s["game_pk"], s["model_card_sha256"])
                index_path = _claim_index_path(identity)
                reservation_path = ROOT / "claim_reservations" / f"{identity}.json"
                with identity_lock(root, identity):
                    existing = read_bytes(root, index_path, "research primary completion index", missing_ok=True)
                    if existing is not None:
                        index = _load_claim_index(root, identity)
                        claim_path = Path(index["claim_relative"])
                        existing_claim = load_claim(root, claim_path)
                        _require_same_primary_identity(s, load_snapshot(root, existing_claim["snapshot_relative"]))
                        claim = claim_path
                    else:
                        if read_bytes(root, reservation_path, "research primary reservation", missing_ok=True) is not None:
                            raise ValueError("research primary has incomplete publication reservation")
                        claimed_at = canonical_utc(clock())
                        if canonical_utc_ns(claimed_at) < canonical_utc_ns(observed):
                            raise ValueError("claim clock predates observation")
                        if canonical_utc_ns(claimed_at) < canonical_utc_ns(s["scheduled_start_utc"]):
                            v = seal({"schema": CLAIM_SCHEMA, **policy(), "claim_identity_sha256": identity, "game_pk": s["game_pk"], "game_type": s["game_type"], "model_card_sha256": s["model_card_sha256"], "snapshot_relative": candidate.as_posix(), "snapshot_sha256": sha(canonical_json_bytes(s)), "claimed_at_utc": claimed_at})
                            claim_sha = sha(canonical_json_bytes(v))
                            claim_path = ROOT / "primary_claims" / identity / f"{claim_sha}.json"
                            def claim_rebind():
                                rebind()
                                publication = canonical_utc_ns(clock())
                                if publication < canonical_utc_ns(claimed_at) or publication >= canonical_utc_ns(s["scheduled_start_utc"]):
                                    raise ValueError("research claim publication is not strictly pregame")
                            claim_rebind()
                            reservation = seal({"schema": CLAIM_RESERVATION_SCHEMA, **policy(), "claim_identity_sha256": identity, "claim_relative": claim_path.as_posix(), "claim_sha256": claim_sha})
                            write_json(root, reservation_path, reservation, "research primary reservation", claim_rebind)
                            write_json(root, claim_path, v, "research claim", claim_rebind)
                            # Both forecast bytes and their seal are now durable
                            # and post-return admitted. Only this successful
                            # completion index can make them reader-authoritative.
                            claim_rebind()
                            completed_at = canonical_utc(clock())
                            if not canonical_utc_ns(claimed_at) <= canonical_utc_ns(completed_at) < canonical_utc_ns(s["scheduled_start_utc"]):
                                raise ValueError("research claim durable completion is not strictly pregame")
                            index = seal({"schema": CLAIM_INDEX_SCHEMA, **policy(), "claim_identity_sha256": identity, "claim_relative": claim_path.as_posix(), "claim_sha256": claim_sha, "completed_at_utc": completed_at})
                            write_json(root, index_path, index, "research primary completion index", rebind)
                            claim = claim_path
            if claim is not None:
                claims.append(claim.as_posix())
            reasons = s["reason_codes"] if claim is not None or not s["primary_eligible"] else ["GAME_STARTED_AT_CLAIM"]
            rows[-1] = game_row(s, candidate, claim, reasons=reasons)
        _authority(root, authority.trace_relative)
        v = receipt_value("PREGAME", observed, authority.trace_relative, authority.trace_sha256, candidates=candidates, primary_claims=claims, games=rows, workflow_complete=True)
        path = persist_receipt(root, v)
        load_receipt(root, path)
        return PregameResult(path, v)
    except Exception as exc:
        v = receipt_value("PREGAME_PARTIAL", observed, authority.trace_relative, authority.trace_sha256, candidates=candidates, primary_claims=claims, games=rows, errors=[{"game_pk": None, "reason": str(exc)}])
        persist_receipt(root, v)
        raise


def load_receipt(root, path, *, pregame=None, _seen=None):
    rel = _relative(root, path, "research receipt")
    seen = set() if _seen is None else _seen
    if rel in seen:
        raise ValueError("research receipt chain cycle")
    seen.add(rel)
    v = envelope(read_json(root, rel, "research receipt"), RECEIPT_KEYS, RECEIPT_SCHEMA)
    if rel != ROOT / "receipts" / f"{sha(canonical_json_bytes(v))}.json":
        raise ValueError("research receipt path differs")
    canonical_utc(v["created_at_utc"])
    if v["phase"] not in {"PREGAME", "PREGAME_PARTIAL", "POSTGAME", "POSTGAME_PARTIAL"} or type(v["workflow_complete"]) is not bool:
        raise ValueError("research receipt phase differs")
    for key in ("candidates", "primary_claims", "settlement_heads", "games", "errors"):
        if not isinstance(v[key], list):
            raise ValueError("research receipt collections differ")
    if any(not isinstance(e, dict) or set(e) != {"game_pk", "reason"} or (e["game_pk"] is not None and type(e["game_pk"]) is not int) or not isinstance(e["reason"], str) for e in v["errors"]):
        raise ValueError("research receipt errors differ")
    a = _authority(root, v["ml_trace_relative"])
    if a.trace_sha256 != v["ml_trace_sha256"]:
        raise ValueError("research receipt trace differs")
    snapshots = {p: load_snapshot(root, p) for p in v["candidates"]}
    claims = {p: load_claim(root, p) for p in v["primary_claims"]}
    if len(snapshots) != len(v["candidates"]) or len(claims) != len(v["primary_claims"]) or len(v["games"]) != len(snapshots):
        raise ValueError("research receipt duplicate/missing artifacts")
    authoritative_games = {game["game_pk"] for game in a.report["games"]}
    candidate_games = {snapshot["game_pk"] for snapshot in snapshots.values()}
    if not candidate_games <= authoritative_games or (v["phase"] == "PREGAME" and candidate_games != authoritative_games):
        raise ValueError("research complete slate coverage differs from authority")
    if any(snapshot["ml_trace_relative"] != v["ml_trace_relative"] or snapshot["ml_trace_sha256"] != v["ml_trace_sha256"] for snapshot in snapshots.values()):
        raise ValueError("research current candidate trace projection differs")
    if v["phase"].endswith("PARTIAL") and (v["workflow_complete"] or not v["errors"]):
        raise ValueError("research partial receipt completion differs")
    seen_games = set()
    for row in v["games"]:
        if not isinstance(row, dict) or set(row) != GAME_KEYS or row["snapshot_relative"] not in snapshots:
            raise ValueError("research receipt game differs")
        s = snapshots[row["snapshot_relative"]]
        c = claims.get(row["claim_relative"])
        if row["game_pk"] in seen_games or any(row[k] != s[k] for k in ("game_pk", "game_type")) or (row["claim_relative"] is not None and (c is None or c["game_pk"] != s["game_pk"])):
            raise ValueError("research receipt game identity differs")
        seen_games.add(row["game_pk"])
        if c is not None:
            _require_same_primary_identity(s, load_snapshot(root, c["snapshot_relative"]))
        fixed = game_row(s, row["snapshot_relative"], row["claim_relative"], state=row["state"], head=row["settlement_head"], reasons=row["reason_codes"])
        if row != fixed or row["state"] not in {"PRIMARY", "DIAGNOSTIC", "PENDING", "SETTLED", "EXCLUDED", "FAILED"} or not isinstance(row["reason_codes"], list):
            raise ValueError("research receipt game policy differs")
    if v["phase"].startswith("PREGAME"):
        if any(v[k] is not None for k in ("pregame_receipt", "previous_postgame_receipt", "report_relative")) or v["settlement_heads"]:
            raise ValueError("research pregame references differ")
        if set(snapshots) != {row["snapshot_relative"] for row in v["games"]} or set(claims) != {row["claim_relative"] for row in v["games"] if row["claim_relative"] is not None}:
            raise ValueError("research pregame artifact references differ")
        if v["phase"] == "PREGAME":
            if not v["workflow_complete"] or v["errors"] or any(s["observed_at_utc"] != v["created_at_utc"] for s in snapshots.values()):
                raise ValueError("research pregame completion differs")
            for row in v["games"]:
                s = snapshots[row["snapshot_relative"]]
                if row["state"] != ("PRIMARY" if row["claim_relative"] is not None else "DIAGNOSTIC") or row["reason_codes"] != (s["reason_codes"] if row["claim_relative"] is not None or not s["primary_eligible"] else ["GAME_STARTED_AT_CLAIM"]):
                    raise ValueError("research pregame admission state differs")
    else:
        if pregame is None:
            pregame = load_receipt(root, v["pregame_receipt"])
        if pregame["phase"] != "PREGAME" or any(v[k] != pregame[k] for k in ("ml_trace_relative", "ml_trace_sha256", "candidates", "primary_claims")):
            raise ValueError("research postgame cohort differs")
        if v["previous_postgame_receipt"] is not None:
            previous = load_receipt(root, v["previous_postgame_receipt"], pregame=pregame, _seen=seen)
            if not previous["phase"].startswith("POSTGAME") or previous["pregame_receipt"] != v["pregame_receipt"]:
                raise ValueError("research previous receipt chain differs")
            if canonical_utc_ns(previous["created_at_utc"]) > canonical_utc_ns(v["created_at_utc"]):
                raise ValueError("research receipt chronological chain differs")
        from .prospective_ml_postseason_settlement import load_head, load_summary
        heads = {p: load_head(root, p) for p in v["settlement_heads"]}
        if len(heads) != len(v["settlement_heads"]) or any(h["claim_relative"] not in claims for h in heads.values()):
            raise ValueError("research postgame head cohort differs")
        pregame_rows = {row["game_pk"]: row for row in pregame["games"]}
        for row in v["games"]:
            original = pregame_rows.get(row["game_pk"])
            if original is None or any(row[k] != original[k] for k in ("snapshot_relative", "claim_relative", "game_type")):
                raise ValueError("research postgame candidate/claim row linkage differs")
            if original["claim_relative"] is None and row != original:
                raise ValueError("research diagnostic row changed after pregame")
        if v["report_relative"] is None:
            if v["workflow_complete"]:
                raise ValueError("research postgame summary missing")
        else:
            load_summary(root, v["report_relative"], v["primary_claims"], v["settlement_heads"], failure_count=len(v["errors"]))
        for row in v["games"]:
            head = heads.get(row["settlement_head"])
            if row["settlement_head"] is not None and (head is None or head["game_pk"] != row["game_pk"] or head["result"]["state"] != row["state"]):
                raise ValueError("research receipt settlement differs")
            if head is not None and row["reason_codes"] != [head["result"]["reason"]]:
                raise ValueError("research receipt result reason differs")
        if {row["settlement_head"] for row in v["games"] if row["settlement_head"] is not None} != set(heads):
            raise ValueError("research receipt head rows differ")
        if v["phase"] == "POSTGAME" and (not v["workflow_complete"] or v["errors"] or any(row["claim_relative"] is not None and row["settlement_head"] is None for row in v["games"])):
            raise ValueError("research postgame completion differs")
    if read_json(root, rel, "research receipt") != v:
        raise ValueError("research receipt changed during admission")
    return v


def run_research_postgame(root: Path | str, pregame_receipt: Path | str, *, previous_postgame_receipt: Path | str | None = None, fetcher=None, durable_clock=None) -> PostgameResult:
    from .prospective_ml_postseason_settlement import run_postgame
    return run_postgame(root, pregame_receipt, previous_postgame_receipt=previous_postgame_receipt, fetcher=fetcher, durable_clock=durable_clock)


def read_research_status(root: Path | str, pregame_receipt: Path | str, *, postgame_receipt: Path | str | None = None, clock: Callable[[], str]) -> dict[str, object]:
    from .prospective_ml_daily_flow import normalize_root
    root = normalize_root(root)
    canonical_utc(clock())
    pre_rel = _relative(root, pregame_receipt, "research pregame")
    pre = load_receipt(root, pre_rel)
    if pre["phase"] != "PREGAME":
        raise ValueError("research pregame incomplete")
    post = None
    if postgame_receipt is not None:
        post = load_receipt(root, postgame_receipt, pregame=pre)
        if post["pregame_receipt"] != pre_rel.as_posix():
            raise ValueError("research status receipt link differs")
    rows = pre["games"] if post is None else post["games"]
    complete = bool(pre["primary_claims"] and post is not None and post["workflow_complete"] and all(row["claim_relative"] is None or row["state"] in {"SETTLED", "EXCLUDED"} for row in rows))
    return {"schema": "PROSPECTIVE_ML_POSTSEASON_RESEARCH_STATUS_V1", **policy(), "pregame_receipt": pre_rel.as_posix(), "postgame_receipt": None if postgame_receipt is None else _relative(root, postgame_receipt, "research postgame").as_posix(), "workflow_processed": post is not None, "workflow_complete": bool(post is not None and post["workflow_complete"]), "complete_evidence": complete, "games": rows, "errors": [] if post is None else post["errors"]}
