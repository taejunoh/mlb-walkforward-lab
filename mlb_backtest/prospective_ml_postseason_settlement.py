"""Scope-isolated official outcomes, correction chains and descriptive metrics."""
from __future__ import annotations

import json
from datetime import datetime, timezone, date
from pathlib import Path
from collections.abc import Mapping

from .market_play_contracts import canonical_json_bytes, canonical_utc, canonical_utc_ns
from .live_sources import fetch_live_http, _validate_http_observation
from .prospective_ml_observation import _relative, _hex
from .prospective_ml_postseason_research import (
    ROOT, REPORT_ROOT, POLICY_KEYS, policy, seal, envelope, sha, read_bytes,
    read_json, write_json, write_bytes, load_claim, load_snapshot, load_receipt,
    identity_lock, game_row, receipt_value, persist_receipt,
)

SETTLEMENT_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_SETTLEMENT_V1"
SUMMARY_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_SUMMARY_V1"
HEAD_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_HEAD_V1"
HEAD_INDEX_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_HEAD_INDEX_V1"
OFFICIAL_SCHEMA = "PROSPECTIVE_ML_POSTSEASON_RESEARCH_OFFICIAL_RESULT_V1"
SETTLEMENT_KEYS = {"schema", *POLICY_KEYS, "claim_relative", "claim_sha256", "snapshot_relative", "snapshot_sha256", "game_pk", "game_type", "away_team_id", "home_team_id", "official_result_receipt_relative", "official_result_receipt_sha256", "result", "previous_head_relative", "revision", "content_sha256"}
SUMMARY_KEYS = {"schema", *POLICY_KEYS, "claim_identities", "settlement_heads", "sample_count", "hit_count", "accuracy", "brier_score", "log_loss", "pending_count", "excluded_count", "failure_count", "shadow_status", "warnings", "content_sha256"}


def parse_result(raw, snapshot):
    if not isinstance(raw, Mapping) or not isinstance(raw.get("dates"), list) or len(raw["dates"]) != 1:
        raise ValueError("research official schedule differs")
    entries = raw["dates"][0].get("games")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], Mapping):
        raise ValueError("research official game differs")
    game = entries[0]
    if type(game.get("gamePk")) is not int or game["gamePk"] != snapshot["game_pk"] or game.get("gameType") != snapshot["game_type"] or game.get("gameType") not in {"F", "D", "L", "W"}:
        raise ValueError("research official game/type identity differs")
    official_date = game.get("officialDate")
    if not isinstance(official_date, str):
        raise ValueError("research official date missing")
    date.fromisoformat(official_date)
    # Existing prospective reconciliation is exact official-date identity;
    # postponement/suspension does not silently admit another target date.
    if official_date != snapshot["target_date"]:
        raise ValueError("research official date differs")
    try:
        away, home = game["teams"]["away"], game["teams"]["home"]
        if any(type(side["team"]["id"]) is not int or side["team"]["id"] != snapshot[key] for side, key in ((away, "away_team_id"), (home, "home_team_id"))):
            raise ValueError("research official team identity differs")
        abstract, detailed = game["status"]["abstractGameState"], game["status"]["detailedState"]
    except (KeyError, TypeError) as exc:
        raise ValueError("research official identity/status missing") from exc
    if not isinstance(abstract, str) or not isinstance(detailed, str):
        raise ValueError("research official status differs")
    state, reason, away_score, home_score, winner = "PENDING", detailed.upper().replace(" ", "_"), None, None, None
    terminal_exclusion = any(token in detailed.upper() for token in ("NO CONTEST", "NO_CONTEST", "CANCEL", "TIE"))
    if terminal_exclusion:
        state = "EXCLUDED"
    elif any(token in detailed.upper() for token in ("SUSPEND", "POSTPON")):
        state = "PENDING"
    elif abstract == "Final":
        a, h = away.get("score"), home.get("score")
        if type(a) is not int or type(h) is not int or min(a, h) < 0:
            raise ValueError("research official final scores differ")
        if a == h:
            state, reason = "EXCLUDED", "FINAL_TIE"
        else:
            aw, hw = away.get("isWinner"), home.get("isWinner")
            if type(aw) is not bool or type(hw) is not bool or aw != (a > h) or hw != (h > a):
                raise ValueError("research official final winner differs")
            state, reason, away_score, home_score, winner = "SETTLED", "FINAL", a, h, "away" if a > h else "home"
    return {"state": state, "reason": reason, "away_score": away_score, "home_score": home_score, "winner": winner, "official_date": official_date}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clock_text(clock):
    value = clock()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("research durable clock must be timezone aware")
        value = value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return canonical_utc(value)


def _url(snapshot):
    return f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={snapshot['target_date']}&gamePks={snapshot['game_pk']}"


OFFICIAL_KEYS = {"schema", *POLICY_KEYS, "game_pk", "game_type", "source", "query_identity", "requested_url", "final_url", "started_at_utc", "response_completed_at_utc", "durable_at_utc", "http_status", "content_type", "etag", "last_modified", "payload_relative", "payload_sha256", "byte_length", "content_sha256"}


def collect_official(root, snapshot, *, fetcher, durable_clock):
    url = _url(snapshot)
    http = _validate_http_observation(fetcher("stats_api", url), source="stats_api", requested_url=url)
    raw = json.loads(http.body)
    parse_result(raw, snapshot)
    payload_sha = sha(http.body)
    payload_path = ROOT / "official_results" / "raw" / f"{payload_sha}.json"
    write_bytes(root, payload_path, http.body, "research official raw")
    write_bytes(root, payload_path.with_suffix(".json.sha256"), f"{payload_sha}\n".encode(), "research official raw seal")
    # Durable publication time is acquired only after the raw write completes.
    durable = _clock_text(durable_clock)
    if canonical_utc_ns(durable) < canonical_utc_ns(http.response_completed_at_utc):
        raise ValueError("research official durable boundary differs")
    value = seal({"schema": OFFICIAL_SCHEMA, **policy(), "game_pk": snapshot["game_pk"], "game_type": snapshot["game_type"], "source": "stats_api", "query_identity": {"competition_scope": "postseason-research-v1", "research_policy_version": "POSTSEASON_ML_RESEARCH_V1", "kind": "postseason_research_official_result", "game_pk": snapshot["game_pk"], "game_type": snapshot["game_type"], "target_date": snapshot["target_date"], "url": url}, "requested_url": http.requested_url, "final_url": http.final_url, "started_at_utc": http.started_at_utc, "response_completed_at_utc": http.response_completed_at_utc, "durable_at_utc": durable, "http_status": http.http_status, "content_type": http.content_type, "etag": http.etag, "last_modified": http.last_modified, "payload_relative": payload_path.as_posix(), "payload_sha256": payload_sha, "byte_length": len(http.body)})
    path = ROOT / "official_results" / "receipts" / f"{sha(canonical_json_bytes(value))}.json"
    write_json(root, path, value, "research official receipt")
    load_official(root, path, snapshot)
    return path


def load_official(root, path, snapshot):
    rel = _relative(root, path, "research official receipt")
    v = envelope(read_json(root, rel, "research official receipt"), OFFICIAL_KEYS, OFFICIAL_SCHEMA)
    if rel != ROOT / "official_results" / "receipts" / f"{sha(canonical_json_bytes(v))}.json":
        raise ValueError("research official receipt path differs")
    url = _url(snapshot)
    query = {"competition_scope": "postseason-research-v1", "research_policy_version": "POSTSEASON_ML_RESEARCH_V1", "kind": "postseason_research_official_result", "game_pk": snapshot["game_pk"], "game_type": snapshot["game_type"], "target_date": snapshot["target_date"], "url": url}
    if v["source"] != "stats_api" or v["http_status"] != 200 or v["requested_url"] != url or v["final_url"] != url or v["query_identity"] != query or any(v[k] != snapshot[k] for k in ("game_pk", "game_type")):
        raise ValueError("research official query/provenance differs")
    for key in ("started_at_utc", "response_completed_at_utc", "durable_at_utc"):
        canonical_utc(v[key])
    if not canonical_utc_ns(v["started_at_utc"]) <= canonical_utc_ns(v["response_completed_at_utc"]) <= canonical_utc_ns(v["durable_at_utc"]):
        raise ValueError("research official time ordering differs")
    if canonical_utc_ns(v["response_completed_at_utc"]) <= canonical_utc_ns(snapshot["observed_at_utc"]):
        raise ValueError("research result predates prospective snapshot")
    raw = read_bytes(root, v["payload_relative"], "research official raw")
    _hex(v["payload_sha256"], "official payload digest")
    if sha(raw) != v["payload_sha256"] or type(v["byte_length"]) is not int or len(raw) != v["byte_length"] or Path(v["payload_relative"]) != ROOT / "official_results" / "raw" / f"{sha(raw)}.json":
        raise ValueError("research official raw binding differs")
    if read_bytes(root, Path(v["payload_relative"]).with_suffix(".json.sha256"), "research official raw seal") != f"{sha(raw)}\n".encode():
        raise ValueError("research official raw seal differs")
    result = parse_result(json.loads(raw), snapshot)
    if read_json(root, rel, "research official receipt") != v or read_bytes(root, v["payload_relative"], "research official raw") != raw:
        raise ValueError("research official evidence changed during admission")
    return v, result


def _validate_record(value):
    v = envelope(value, SETTLEMENT_KEYS, SETTLEMENT_SCHEMA)
    if type(v["revision"]) is not int or v["revision"] < 1 or (v["revision"] == 1) != (v["previous_head_relative"] is None):
        raise ValueError("research settlement revision differs")
    for key in ("claim_sha256", "snapshot_sha256", "official_result_receipt_sha256"):
        _hex(v[key], key)
    result = v["result"]
    if not isinstance(result, dict) or set(result) != {"state", "reason", "away_score", "home_score", "winner", "official_date"} or result["state"] not in {"SETTLED", "PENDING", "EXCLUDED"} or not isinstance(result["reason"], str):
        raise ValueError("research settlement result differs")
    date.fromisoformat(result["official_date"])
    if result["state"] == "SETTLED":
        if any(type(result[k]) is not int or result[k] < 0 for k in ("away_score", "home_score")) or result["away_score"] == result["home_score"] or result["winner"] != ("home" if result["home_score"] > result["away_score"] else "away"):
            raise ValueError("research settlement score differs")
    elif any(result[k] is not None for k in ("away_score", "home_score", "winner")):
        raise ValueError("research nonsettleable score differs")
    return v


def _head_path(identity, revision):
    """Immutable revision index used only to prevent stale/duplicate publication."""
    return ROOT / "settlement_heads" / identity / f"{revision:08d}.json"


def load_head(root, path, *, _seen=None):
    rel = _relative(root, path, "research settlement head")
    seen = set() if _seen is None else _seen
    if rel in seen:
        raise ValueError("research settlement chain cycle")
    seen.add(rel)
    head = envelope(read_json(root, rel, "research settlement head"), {"schema", *POLICY_KEYS, "claim_identity_sha256", "revision", "record_relative", "record_sha256", "previous_head_sha256", "content_sha256"}, HEAD_SCHEMA)
    head_sha = sha(canonical_json_bytes(head))
    index_path = _head_path(head["claim_identity_sha256"], head["revision"])
    expected_path = index_path.with_name(f"{head['revision']:08d}-{head_sha}.json")
    if type(head["revision"]) is not int or head["revision"] < 1 or rel != expected_path:
        raise ValueError("research head path/revision differs")
    index = envelope(read_json(root, index_path, "research revision index"), {"schema", *POLICY_KEYS, "claim_identity_sha256", "revision", "head_relative", "head_sha256", "content_sha256"}, HEAD_INDEX_SCHEMA)
    if index["claim_identity_sha256"] != head["claim_identity_sha256"] or index["revision"] != head["revision"] or index["head_relative"] != rel.as_posix() or index["head_sha256"] != head_sha:
        raise ValueError("research revision index does not pin exact head bytes")
    record_path = _relative(root, head["record_relative"], "research settlement record")
    v = _validate_record(read_json(root, record_path, "research settlement record"))
    if sha(canonical_json_bytes(v)) != head["record_sha256"] or v["revision"] != head["revision"] or record_path != ROOT / "settlements" / head["claim_identity_sha256"] / f"{v['revision']:08d}-{sha(canonical_json_bytes(v))}.json":
        raise ValueError("research settlement head binding differs")
    claim = load_claim(root, v["claim_relative"])
    snapshot = load_snapshot(root, v["snapshot_relative"])
    if head["claim_identity_sha256"] != claim["claim_identity_sha256"] or v["claim_sha256"] != sha(canonical_json_bytes(claim)) or v["snapshot_relative"] != claim["snapshot_relative"] or v["snapshot_sha256"] != claim["snapshot_sha256"] or any(v[k] != snapshot[k] for k in ("game_pk", "game_type", "away_team_id", "home_team_id")):
        raise ValueError("research settlement claim lineage differs")
    official, result = load_official(root, v["official_result_receipt_relative"], snapshot)
    if v["official_result_receipt_sha256"] != sha(canonical_json_bytes(official)) or v["result"] != result:
        raise ValueError("research settlement official binding differs")
    if v["revision"] > 1:
        _hex(head["previous_head_sha256"], "research prior head digest")
        if head["previous_head_sha256"] != sha(read_bytes(root, v["previous_head_relative"], "research prior head")):
            raise ValueError("research correction prior head digest/ancestry differs")
        previous = load_head(root, v["previous_head_relative"], _seen=seen)
        if previous["revision"] != v["revision"] - 1 or previous["claim_relative"] != v["claim_relative"] or previous["snapshot_sha256"] != v["snapshot_sha256"]:
            raise ValueError("research correction chain differs")
        prior_official, _ = load_official(root, previous["official_result_receipt_relative"], snapshot)
        if canonical_utc_ns(official["response_completed_at_utc"]) <= canonical_utc_ns(prior_official["response_completed_at_utc"]) or previous["result"] == v["result"]:
            raise ValueError("research correction is not a newer changed official outcome")
    elif head["previous_head_sha256"] is not None:
        raise ValueError("research first head has unexpected ancestry")
    if read_json(root, rel, "research settlement head") != head or read_json(root, record_path, "research settlement record") != v or read_json(root, index_path, "research revision index") != index:
        raise ValueError("research settlement chain changed during admission")
    return v


def persist_settlement(root, claim_path, official_receipt, *, prior=None, correction=False):
    """Explicit ``correction=True`` is required to revise terminal outcomes.

    Pending heads can advance through normal collection; settled/excluded heads
    are never recollected or revised by automatic postgame resume.
    """
    claim_path = _relative(root, claim_path, "research claim")
    claim = load_claim(root, claim_path)
    s = load_snapshot(root, claim["snapshot_relative"])
    official, result = load_official(root, official_receipt, s)
    identity = claim["claim_identity_sha256"]
    with identity_lock(root, identity):
        previous = None if prior is None else load_head(root, prior)
        if previous is not None:
            if previous["claim_relative"] != claim_path.as_posix():
                raise ValueError("research prior belongs to another claim")
            if previous["result"] == result:
                return _relative(root, prior, "research prior")
            if previous["result"]["state"] in {"SETTLED", "EXCLUDED"} and not correction:
                raise ValueError("terminal research correction requires explicit correction path")
            prior_official, _ = load_official(root, previous["official_result_receipt_relative"], s)
            if canonical_utc_ns(official["response_completed_at_utc"]) <= canonical_utc_ns(prior_official["response_completed_at_utc"]):
                raise ValueError("research correction requires a newer official observation")
            revision = previous["revision"] + 1
        else:
            revision = 1
        v = seal({"schema": SETTLEMENT_SCHEMA, **policy(), "claim_relative": claim_path.as_posix(), "claim_sha256": sha(canonical_json_bytes(claim)), "snapshot_relative": claim["snapshot_relative"], "snapshot_sha256": claim["snapshot_sha256"], "game_pk": s["game_pk"], "game_type": s["game_type"], "away_team_id": s["away_team_id"], "home_team_id": s["home_team_id"], "official_result_receipt_relative": str(_relative(root, official_receipt, "official receipt")), "official_result_receipt_sha256": sha(canonical_json_bytes(official)), "result": result, "previous_head_relative": None if prior is None else str(_relative(root, prior, "research prior")), "revision": revision})
        path = ROOT / "settlements" / identity / f"{revision:08d}-{sha(canonical_json_bytes(v))}.json"
        index_path = _head_path(identity, revision)
        prior_sha = None if prior is None else sha(read_bytes(root, prior, "research prior head"))
        head = seal({"schema": HEAD_SCHEMA, **policy(), "claim_identity_sha256": identity, "revision": revision, "record_relative": path.as_posix(), "record_sha256": sha(canonical_json_bytes(v)), "previous_head_sha256": prior_sha})
        head_sha = sha(canonical_json_bytes(head))
        head_path = index_path.with_name(f"{revision:08d}-{head_sha}.json")
        index = seal({"schema": HEAD_INDEX_SCHEMA, **policy(), "claim_identity_sha256": identity, "revision": revision, "head_relative": head_path.as_posix(), "head_sha256": head_sha})
        def rebind():
            if load_claim(root, claim_path) != claim or load_official(root, official_receipt, s) != (official, result):
                raise ValueError("research settlement sources changed during write")
            if prior is not None and load_head(root, prior) != previous:
                raise ValueError("research settlement prior changed during write")
            if prior is not None and sha(read_bytes(root, prior, "research prior head")) != prior_sha:
                raise ValueError("research settlement prior head digest changed during write")
        # Reserve exact revision identity first. A crash leaves an unsealed
        # reservation, which cannot be admitted or bypassed on stale resume.
        # An exact retry can complete it; a conflicting head never overwrites it.
        index_raw = canonical_json_bytes(index)
        rebind()
        write_bytes(root, index_path, index_raw, "research revision reservation", rebind)
        write_json(root, path, v, "research settlement record", rebind)
        write_json(root, head_path, head, "research settlement head", rebind)
        write_bytes(root, index_path.with_suffix(".json.sha256"), f"{sha(index_raw)}\n".encode(), "research revision index seal", rebind)
        load_head(root, head_path)
        return head_path


def build_summary(root, claims, heads, *, failure_count=0):
    from .prospective_ml_settlement import calibration_metrics
    claims = [str(_relative(root, p, "research claim")) for p in claims]
    heads = [str(_relative(root, p, "research head")) for p in heads]
    if len(set(claims)) != len(claims) or len(set(heads)) != len(heads) or type(failure_count) is not int or failure_count < 0:
        raise ValueError("research summary duplicate/count differs")
    claim_values = {p: load_claim(root, p) for p in claims}
    records = {v["claim_relative"]: v for v in (load_head(root, p) for p in heads)}
    if len(records) != len(heads) or any(p not in claim_values for p in records):
        raise ValueError("research summary head cohort differs")
    data, pending, excluded = [], 0, 0
    for p, claim in claim_values.items():
        record = records.get(p)
        if record is None or record["result"]["state"] == "PENDING":
            pending += 1
        elif record["result"]["state"] == "EXCLUDED":
            excluded += 1
        else:
            snapshot = load_snapshot(root, claim["snapshot_relative"])
            data.append((float(snapshot["p_home"]), record["result"]["winner"] == "home"))
    metrics = calibration_metrics(data)
    return seal({"schema": SUMMARY_SCHEMA, **policy(), "claim_identities": [claim_values[p]["claim_identity_sha256"] for p in claims], "settlement_heads": heads, "sample_count": metrics["n"], "hit_count": metrics["wins"], "accuracy": metrics["accuracy"], "brier_score": metrics["brier"], "log_loss": metrics["log_loss"], "pending_count": pending, "excluded_count": excluded, "failure_count": failure_count, "shadow_status": "NOT_EVALUATED_FOR_POSTSEASON", "warnings": ["Descriptive prospective research only; small samples do not establish postseason calibration.", "PASS — POSTSEASON_NOT_VALIDATED", "NOT_EVALUATED — POSTSEASON_TOTALS_NOT_SUPPORTED"]})


def persist_summary(root, value):
    path = REPORT_ROOT / f"{sha(canonical_json_bytes(value))}.json"
    write_json(root, path, value, "research summary")
    return path


def load_summary(root, path, claims, heads, *, failure_count=0):
    rel = _relative(root, path, "research summary")
    v = envelope(read_json(root, rel, "research summary"), SUMMARY_KEYS, SUMMARY_SCHEMA)
    if rel != REPORT_ROOT / f"{sha(canonical_json_bytes(v))}.json" or v != build_summary(root, claims, heads, failure_count=failure_count):
        raise ValueError("research summary lineage/metrics differs")
    return v


def run_postgame(root, pregame_receipt, *, previous_postgame_receipt=None, fetcher=None, durable_clock=None):
    from .prospective_ml_daily_flow import normalize_root, PostgameResult
    root = normalize_root(root)
    pre_rel = _relative(root, pregame_receipt, "research pregame receipt")
    pre = load_receipt(root, pre_rel)
    if pre["phase"] != "PREGAME" or not pre["workflow_complete"]:
        raise ValueError("research pregame receipt is incomplete")
    previous = None
    if previous_postgame_receipt is not None:
        previous = load_receipt(root, previous_postgame_receipt, pregame=pre)
        if previous["pregame_receipt"] != pre_rel.as_posix():
            raise ValueError("research resume pregame link differs")
    heads = [] if previous is None else list(previous["settlement_heads"])
    records = {load_head(root, path)["claim_relative"]: path for path in heads}
    # Admit all artifacts and stale-head conditions before the first fetch.
    for claim_path in pre["primary_claims"]:
        claim = load_claim(root, claim_path)
        first_head = _head_path(claim["claim_identity_sha256"], 1)
        if claim_path not in records and read_bytes(root, first_head, "research first head", missing_ok=True) is not None:
            raise ValueError("stale research receipt omits settlement head; resume latest receipt")
        if claim_path in records:
            record = load_head(root, records[claim_path])
            next_head = _head_path(claim["claim_identity_sha256"], record["revision"] + 1)
            if read_bytes(root, next_head, "research next head", missing_ok=True) is not None:
                raise ValueError("stale research receipt omits newer settlement head")
    fetcher = fetch_live_http if fetcher is None else fetcher
    durable_clock = _now if durable_clock is None else durable_clock
    rows = [dict(row) for row in (pre["games"] if previous is None else previous["games"])]
    errors = []
    attempted_game = None
    created = _clock_text(durable_clock)
    try:
        for row in rows:
            claim_path = row["claim_relative"]
            if claim_path is None:
                continue
            attempted_game = row
            claim = load_claim(root, claim_path)
            snapshot = load_snapshot(root, claim["snapshot_relative"])
            prior = records.get(claim_path)
            record = None if prior is None else load_head(root, prior)
            if record is not None:
                row.update(state=record["result"]["state"], settlement_head=prior, reason_codes=[record["result"]["reason"]])
            if record is None or record["result"]["state"] == "PENDING":
                official = collect_official(root, snapshot, fetcher=fetcher, durable_clock=durable_clock)
                head = persist_settlement(root, claim_path, official, prior=prior)
                if prior is not None:
                    heads.remove(prior)
                heads.append(head.as_posix())
                record = load_head(root, head)
                records[claim_path] = head.as_posix()
            row.update(state=record["result"]["state"], settlement_head=records[claim_path], reason_codes=[record["result"]["reason"]])
        summary = build_summary(root, pre["primary_claims"], heads)
        summary_path = persist_summary(root, summary)
        v = receipt_value("POSTGAME", created, pre["ml_trace_relative"], pre["ml_trace_sha256"], pregame_receipt=pre_rel.as_posix(), previous_postgame_receipt=None if previous_postgame_receipt is None else _relative(root, previous_postgame_receipt, "research previous receipt").as_posix(), candidates=pre["candidates"], primary_claims=pre["primary_claims"], settlement_heads=heads, report_relative=summary_path.as_posix(), games=rows, workflow_complete=True)
        path = persist_receipt(root, v)
        load_receipt(root, path, pregame=pre)
        return PostgameResult(path, v)
    except Exception as exc:
        game = attempted_game
        if game is not None and game["settlement_head"] is None:
            game.update(state="FAILED", reason_codes=[str(exc)])
        errors.append({"game_pk": None if game is None else game["game_pk"], "reason": str(exc)})
        partial_summary = None
        try:
            partial_summary = persist_summary(root, build_summary(root, pre["primary_claims"], heads, failure_count=len(errors)))
        except (OSError, TypeError, ValueError):
            # Integrity failures cannot acquire a validated descriptive summary.
            # Preserve the error receipt without inventing usable metric evidence.
            pass
        v = receipt_value("POSTGAME_PARTIAL", created, pre["ml_trace_relative"], pre["ml_trace_sha256"], pregame_receipt=pre_rel.as_posix(), previous_postgame_receipt=None if previous_postgame_receipt is None else _relative(root, previous_postgame_receipt, "research previous receipt").as_posix(), candidates=pre["candidates"], primary_claims=pre["primary_claims"], settlement_heads=heads, report_relative=None if partial_summary is None else partial_summary.as_posix(), games=rows, errors=errors)
        persist_receipt(root, v)
        raise
