# ruff: noqa: E701, E702
"""Offline official-result admission, immutable settlement, and calibration reports."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .market_play_contracts import canonical_json_bytes, canonical_utc, canonical_utc_ns, validate_relative_path
from .metrics import brier, log_loss, wilson
from .prospective_ml_observation import _relative, _sha, load_ml_snapshot, validate_ml_snapshot, validate_primary_claim
from .live_sources import _claim_bytes, _validate_observation_completion, _validate_raw_artifact_paths, _validate_raw_manifest
from .live_top3_contracts import LiveHttpObservation, canonical_json_bytes as live_canonical_json_bytes
from .models import RawArtifact
from .secure_fs import open_trusted_root, read_regular_snapshot, rebind_regular_snapshot, write_exclusive


SETTLEMENT_SCHEMA = "PROSPECTIVE_ML_SETTLEMENT_V1"
REPORT_SCHEMA = "PROSPECTIVE_ML_CALIBRATION_REPORT_V1"
ROOT = Path("audit/prospective_ml_observation")
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OfficialResultEvidence:
    root: Path
    receipt_relative: Path
    receipt_sha256: str
    payload_relative: Path
    payload_sha256: str
    manifest_relative: Path
    manifest_sha256: str
    observed_at_utc: str
    durable_at_utc: str
    raw: dict[str, object]

    def rebind(self) -> None:
        with open_trusted_root(self.root) as trusted:
            for path, content, label in ((self.receipt_relative, None, "official receipt"), (self.payload_relative, None, "official payload")):
                snap = read_regular_snapshot(trusted, path, label, max_bytes=MAX_BYTES)
                if snap is None: raise ValueError(f"{label} missing")
                expected = self.receipt_sha256 if path == self.receipt_relative else self.payload_sha256
                if _sha(snap.contents) != expected: raise ValueError(f"{label} differs")


@dataclass(frozen=True, slots=True)
class BuiltSettlement:
    root: Path
    snapshot_relative: Path
    snapshot_sha256: str
    result_receipt: Path
    prior: Path | None
    record: dict[str, object]
    record_bytes: bytes


@dataclass(frozen=True, slots=True)
class PersistedSettlement:
    record_path: Path
    head_path: Path
    record: dict[str, object]


@dataclass(frozen=True, slots=True)
class BuiltCalibrationReport:
    root: Path
    report: dict[str, object]
    report_bytes: bytes


def _canonical_digest(value: Mapping[str, object]) -> str:
    return _sha(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"}))


def _read(root: Path, relative: Path, label: str) -> tuple[bytes, dict[str, object]]:
    with open_trusted_root(root) as trusted:
        snap = read_regular_snapshot(trusted, relative, label, max_bytes=MAX_BYTES)
    if snap is None: raise ValueError(f"{label} missing")
    try: value = json.loads(snap.contents.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ValueError(f"{label} invalid") from exc
    if not isinstance(value, dict): raise ValueError(f"{label} invalid")
    return snap.contents, value


def _parse_result(raw: Mapping[str, object]) -> dict[str, object]:
    games = raw.get("dates")
    if not isinstance(games, list) or len(games) != 1 or not isinstance(games[0], Mapping): raise ValueError("official result schedule differs")
    entries = games[0].get("games")
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], Mapping): raise ValueError("official result game differs")
    game = entries[0]; teams = game.get("teams")
    if type(game.get("gamePk")) is not int or not isinstance(teams, Mapping): raise ValueError("official result identity differs")
    try:
        away = teams["away"]; home = teams["home"]
        away_name = away["team"]["name"]; home_name = home["team"]["name"]
    except (KeyError, TypeError) as exc: raise ValueError("official result teams differ") from exc
    if not isinstance(away_name, str) or not isinstance(home_name, str) or not isinstance(game.get("officialDate"), str): raise ValueError("official result teams differ")
    status = game.get("status"); state = status.get("abstractGameState") if isinstance(status, Mapping) else None
    detailed = status.get("detailedState") if isinstance(status, Mapping) else None
    if not isinstance(state, str) or not isinstance(detailed, str): raise ValueError("official result status differs")
    outcome = "PENDING"
    winner: str | None = None
    if state == "Final":
        away_win, home_win = away.get("isWinner"), home.get("isWinner")
        if type(away_win) is not bool or type(home_win) is not bool or away_win == home_win: raise ValueError("official final winner differs")
        winner = away_name if away_win else home_name; outcome = "FINAL"
    elif any(token in detailed.upper() for token in ("POSTPON", "SUSPEND", "CANCEL", "NO CONTEST", "TIE")):
        outcome = detailed.upper().replace(" ", "_")
    return {"game_pk": game["gamePk"], "official_date": game["officialDate"], "away_team": away_name, "home_team": home_name, "status": outcome, "winner": winner}


def _validate_helper_result_receipt(root: Path, relative: Path, receipt_raw: bytes, value: Mapping[str, object], payload: Path, payload_raw: bytes, manifest_relative: Path) -> str:
    required = {"schema_version", "source", "binding_role", "observation_id", "query_identity_json", "requested_url", "final_url", "started_at_utc", "response_completed_at_utc", "http_status", "content_type", "etag", "last_modified", "payload_sha256", "byte_length", "payload_relative", "manifest_sha256", "manifest_relative", "claim_relative", "claim_sha256"}
    if set(value) != required or live_canonical_json_bytes(value) != receipt_raw or value.get("schema_version") != 1 or value.get("binding_role") != "FINAL_SCHEDULE": raise ValueError("official helper receipt differs")
    observation_id = value.get("observation_id")
    query_text = value.get("query_identity_json")
    if not isinstance(observation_id, str) or not isinstance(query_text, str): raise ValueError("official helper receipt differs")
    try: query = json.loads(query_text)
    except json.JSONDecodeError as exc: raise ValueError("official helper query differs") from exc
    if not isinstance(query, dict) or live_canonical_json_bytes(query).decode("utf-8").removesuffix("\n") != query_text or set(query) != {"game_pk", "kind", "observation_id", "url"} or query.get("kind") != "prospective_ml_official_result" or query.get("observation_id") != observation_id or query.get("url") != value.get("requested_url") or type(query.get("game_pk")) is not int or query["game_pk"] <= 0: raise ValueError("official helper query differs")
    claim_relative = Path("audit/experimental_daily/live_observation_claims") / f"{observation_id}.json"
    if value.get("claim_relative") != claim_relative.as_posix() or not isinstance(value.get("claim_sha256"), str): raise ValueError("official helper claim differs")
    http = LiveHttpObservation(requested_url=value["requested_url"], final_url=value["final_url"], started_at_utc=value["started_at_utc"], response_completed_at_utc=value["response_completed_at_utc"], http_status=value["http_status"], content_type=value["content_type"], etag=value["etag"], last_modified=value["last_modified"], body=payload_raw)
    claim_bytes = _claim_bytes(raw_source="mlb_official_result", source="stats_api", binding_role="FINAL_SCHEDULE", observation_id=observation_id, query_identity_json=query_text, http=http)
    with open_trusted_root(root) as trusted:
        claim = read_regular_snapshot(trusted, claim_relative, "official helper claim", max_bytes=MAX_BYTES)
    if claim is None or claim.contents != claim_bytes or _sha(claim.contents) != value["claim_sha256"]: raise ValueError("official helper claim differs")
    expected = {"schema_version": 1, "source": "stats_api", "binding_role": "FINAL_SCHEDULE", "observation_id": observation_id, "query_identity_json": query_text, "receipt_relative": relative.as_posix(), "receipt_sha256": _sha(receipt_raw), "payload_sha256": _sha(payload_raw), "manifest_sha256": value["manifest_sha256"]}
    completion_relative = relative.with_suffix(".completion.json")
    seal_relative = relative.with_suffix(".completion.sha256")
    with open_trusted_root(root) as trusted:
        completion = read_regular_snapshot(trusted, completion_relative, "official helper completion", max_bytes=MAX_BYTES)
        seal = read_regular_snapshot(trusted, seal_relative, "official helper completion seal", max_bytes=1024)
    if completion is None or seal is None or seal.contents != f"{_sha(completion.contents)}\n".encode("ascii"): raise ValueError("official helper completion seal differs")
    return _validate_observation_completion(completion.contents, expected)


def load_official_result_evidence(root: Path, receipt: Path | str) -> OfficialResultEvidence:
    root = Path(root); relative = _relative(root, receipt, "official receipt")
    receipt_raw, value = _read(root, relative, "official receipt")
    fields = ("source", "requested_url", "final_url", "http_status", "payload_relative", "payload_sha256", "byte_length", "response_completed_at_utc", "query_identity_json", "manifest_relative", "manifest_sha256")
    if any(key not in value for key in fields) or value["source"] != "stats_api" or value["http_status"] != 200 or not isinstance(value["requested_url"], str) or not isinstance(value["final_url"], str) or value["requested_url"] != value["final_url"] or not value["final_url"].startswith("https://statsapi.mlb.com/api/"):
        raise ValueError("official receipt provenance differs")
    payload = _relative(root, str(value["payload_relative"]), "official payload")
    payload_raw, raw = _read(root, payload, "official payload")
    if _sha(payload_raw) != value["payload_sha256"] or value["byte_length"] != len(payload_raw): raise ValueError("official payload binding differs")
    manifest_relative = _relative(root, str(value["manifest_relative"]), "official manifest")
    manifest_raw, manifest = _read(root, manifest_relative, "official manifest")
    if _sha(manifest_raw) != value["manifest_sha256"]: raise ValueError("official manifest binding differs")
    helper_durable = None
    if manifest.get("source") == "mlb_official_result":
        try:
            query = json.loads(value["query_identity_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("official receipt query differs") from exc
        if not isinstance(query, dict): raise ValueError("official receipt query differs")
        artifact = RawArtifact("mlb_official_result", _sha(payload_raw), root / payload, root / manifest_relative, str(manifest.get("retrieved_at_utc")), manifest.get("row_count"))
        expected_payload, expected_manifest, seal = _validate_raw_artifact_paths(root, raw_source="mlb_official_result", query=query, payload=payload_raw, artifact=artifact)
        if payload != expected_payload or manifest_relative != expected_manifest: raise ValueError("official raw artifact path differs")
        with open_trusted_root(root) as trusted:
            seal_snapshot = read_regular_snapshot(trusted, seal, "official raw manifest seal", max_bytes=1024)
        if seal_snapshot is None: raise ValueError("official raw manifest seal missing")
        _validate_raw_manifest(manifest_bytes=manifest_raw, seal_bytes=seal_snapshot.contents, raw_source="mlb_official_result", query=query, payload_bytes=payload_raw, artifact=artifact)
        helper_durable = _validate_helper_result_receipt(root, relative, receipt_raw, value, payload, payload_raw, manifest_relative)
    elif manifest.get("payload_relative") != payload.as_posix() or manifest.get("payload_sha256") != value["payload_sha256"] or manifest.get("byte_length") != len(payload_raw) or manifest.get("source") != value["source"] or manifest.get("query_identity_json") != value["query_identity_json"] or manifest.get("requested_url") != value["requested_url"] or manifest.get("final_url") != value["final_url"]: raise ValueError("official manifest payload binding differs")
    observed = canonical_utc(value["response_completed_at_utc"])
    if helper_durable is None:
        completion = relative.with_suffix(".completion.json")
        _, complete = _read(root, completion, "official completion")
        durable = complete.get("durable_at_utc")
        if complete.get("receipt_sha256") != _sha(receipt_raw) or not isinstance(durable, str): raise ValueError("official completion differs")
        durable = canonical_utc(durable)
    else:
        durable = canonical_utc(helper_durable)
    identity = _parse_result(raw)
    game_pk = identity["game_pk"]
    if helper_durable is not None:
        expected_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={identity['official_date']}&gamePks={game_pk}"
        if value["requested_url"] != expected_url or value["final_url"] != expected_url: raise ValueError("official helper URL differs")
    query = value.get("query_identity_json")
    try: query_value = json.loads(query) if isinstance(query, str) else None
    except json.JSONDecodeError: query_value = None
    if not isinstance(query_value, Mapping) or canonical_json_bytes(query_value).decode() != query or query_value.get("game_pk") != game_pk or str(game_pk) not in value["final_url"]:
        raise ValueError("official receipt game identity differs")
    return OfficialResultEvidence(root, relative, _sha(receipt_raw), payload, _sha(payload_raw), manifest_relative, _sha(manifest_raw), observed, durable, raw)


def validate_settlement(value: object) -> dict[str, object]:
    required = {"schema", "content_sha256", "snapshot_relative", "snapshot_sha256", "revision", "prior_record_relative", "prior_record_sha256", "result", "result_receipt_relative", "result_receipt_sha256", "result_payload_relative", "result_payload_sha256", "result_manifest_relative", "result_manifest_sha256", "result_observed_at_utc", "result_durable_at_utc"}
    if not isinstance(value, Mapping) or set(value) != required: raise ValueError("settlement fields differ")
    safe = json.loads(canonical_json_bytes(value))
    if safe["schema"] != SETTLEMENT_SCHEMA or safe["content_sha256"] != _canonical_digest(safe): raise ValueError("settlement digest differs")
    if type(safe["revision"]) is not int or safe["revision"] <= 0: raise ValueError("settlement revision differs")
    if safe["revision"] == 1:
        if safe["prior_record_sha256"] is not None or safe["prior_record_relative"] is not None: raise ValueError("settlement prior differs")
    elif safe["prior_record_sha256"] is None or safe["prior_record_relative"] is None: raise ValueError("settlement prior differs")
    for key in ("snapshot_relative", "result_receipt_relative", "result_payload_relative", "result_manifest_relative"):
        if not isinstance(safe[key], str): raise ValueError("settlement path differs")
        validate_relative_path(Path(safe[key]))
    for key in ("snapshot_sha256", "result_receipt_sha256", "result_payload_sha256", "result_manifest_sha256"):
        if not isinstance(safe[key], str) or len(safe[key]) != 64: raise ValueError("settlement digest differs")
    if safe["prior_record_sha256"] is not None and (not isinstance(safe["prior_record_sha256"], str) or len(safe["prior_record_sha256"]) != 64): raise ValueError("settlement prior differs")
    if safe["prior_record_relative"] is not None:
        if not isinstance(safe["prior_record_relative"], str): raise ValueError("settlement prior differs")
        validate_relative_path(Path(safe["prior_record_relative"]))
    canonical_utc(safe["result_observed_at_utc"]); canonical_utc(safe["result_durable_at_utc"])
    result = safe["result"]
    if not isinstance(result, Mapping) or result.get("status") not in {"FINAL", "PENDING", "POSTPONED", "SUSPENDED", "TIE", "NO_CONTEST"} and not (isinstance(result.get("status"), str) and result["status"].startswith(("POSTPON", "SUSPEND", "CANCEL"))): raise ValueError("settlement result differs")
    return safe


def build_settlement(root: Path, snapshot: Path | str, result_receipt: Path | str, *, prior: Path | str | None = None) -> BuiltSettlement:
    root = Path(root); snap_rel = _relative(root, snapshot, "snapshot"); snap_raw, snap = _read(root, snap_rel, "snapshot")
    snap = validate_ml_snapshot(snap)
    evidence = load_official_result_evidence(root, result_receipt); result = _parse_result(evidence.raw)
    if canonical_utc_ns(evidence.observed_at_utc) <= canonical_utc_ns(str(snap["observed_at_utc"])): raise ValueError("outcome predates snapshot")
    if result["game_pk"] != snap["game_pk"] or result["official_date"] != snap["target_date"] or result["away_team"] != snap["away_team"] or result["home_team"] != snap["home_team"]: raise ValueError("official result does not bind snapshot")
    prior_rel = None; prior_record_rel = None; prior_sha = None; revision = 1
    if prior is not None:
        prior_rel = _relative(root, prior, "prior settlement head")
        _, prior_head = _read(root, prior_rel, "prior settlement head")
        if not isinstance(prior_head.get("record_relative"), str) or not isinstance(prior_head.get("record_sha256"), str): raise ValueError("prior settlement head differs")
        prior_record_rel = _relative(root, prior_head["record_relative"], "prior settlement")
        prior_raw, prior_value = _read(root, prior_record_rel, "prior settlement")
        prior_value = validate_settlement(prior_value)
        if prior_head["record_sha256"] != _sha(prior_raw) or prior_head.get("revision") != prior_value["revision"]: raise ValueError("prior settlement head differs")
        if prior_value["snapshot_sha256"] != _sha(snap_raw): raise ValueError("prior snapshot differs")
        prior_sha = _sha(prior_raw); revision = int(prior_value["revision"]) + 1
    record: dict[str, object] = {"schema": SETTLEMENT_SCHEMA, "snapshot_relative": snap_rel.as_posix(), "snapshot_sha256": _sha(snap_raw), "revision": revision, "prior_record_relative": None if prior_record_rel is None else prior_record_rel.as_posix(), "prior_record_sha256": prior_sha, "result": result, "result_receipt_relative": evidence.receipt_relative.as_posix(), "result_receipt_sha256": evidence.receipt_sha256, "result_payload_relative": evidence.payload_relative.as_posix(), "result_payload_sha256": evidence.payload_sha256, "result_manifest_relative": evidence.manifest_relative.as_posix(), "result_manifest_sha256": evidence.manifest_sha256, "result_observed_at_utc": evidence.observed_at_utc, "result_durable_at_utc": evidence.durable_at_utc}
    record["content_sha256"] = _canonical_digest(record); record = validate_settlement(record)
    return BuiltSettlement(root, snap_rel, _sha(snap_raw), evidence.receipt_relative, prior_rel, record, canonical_json_bytes(record))


def _write_idempotent(trusted, path: Path, raw: bytes, label: str, final_rebind=None) -> None:
    try: write_exclusive(trusted, path, raw, label, final_rebind=final_rebind)
    except FileExistsError:
        old = read_regular_snapshot(trusted, path, label, max_bytes=len(raw))
        if old is None or old.contents != raw: raise ValueError(f"{label} differs")
        rebind_regular_snapshot(trusted, old, label, max_bytes=len(raw))


def persist_settlement(root: Path, built: BuiltSettlement) -> PersistedSettlement:
    root = Path(root)
    if not isinstance(built, BuiltSettlement) or built.root != root: raise ValueError("built settlement differs")
    # Re-admit both supplied sources; transport objects are never trusted.
    rebuilt = build_settlement(root, built.snapshot_relative, built.result_receipt, prior=built.prior)
    if rebuilt.record_bytes != built.record_bytes: raise ValueError("built settlement differs")
    record = validate_settlement(built.record)
    if canonical_json_bytes(record) != built.record_bytes: raise ValueError("built settlement transport differs")
    sha = _sha(built.record_bytes)
    record_path = ROOT / "settlements" / built.snapshot_sha256 / f"{int(record['revision']):08d}-{sha}.json"
    head_path = ROOT / "settlement_heads" / built.snapshot_sha256 / f"{int(record['revision']):08d}.json"
    head = {"schema": "PROSPECTIVE_ML_SETTLEMENT_HEAD_V1", "snapshot_sha256": built.snapshot_sha256, "revision": record["revision"], "record_relative": record_path.as_posix(), "record_sha256": sha}
    def final_rebind() -> None:
        current = build_settlement(root, built.snapshot_relative, built.result_receipt, prior=built.prior)
        if current.record_bytes != built.record_bytes: raise ValueError("settlement inputs changed during persistence")
    with open_trusted_root(root) as trusted:
        _write_idempotent(trusted, record_path.with_suffix(".json.sha256"), f"{sha}\n".encode(), "settlement sidecar")
        _write_idempotent(trusted, record_path, built.record_bytes, "settlement record", final_rebind)
        _write_idempotent(trusted, head_path, canonical_json_bytes(head), "settlement head", final_rebind)
    return PersistedSettlement(record_path, head_path, record)


def calibration_metrics(rows: Iterable[tuple[float, bool]]) -> dict[str, object]:
    """Metrics for ``(p_home, home_won)`` rows.

    Accuracy grades the implied pick, while Brier/log loss retain the underlying
    home-win probability.  This keeps .70/.40 home-win examples auditable.
    """
    data = list(rows); n = len(data)
    if not n: return {"n": 0, "wins": 0, "losses": 0, "accuracy": None, "brier": None, "log_loss": None, "wilson_95": [None, None], "bins": [{"lower": i / 10, "upper": (i + 1) / 10, "n": 0, "accuracy": None, "wilson_95": [None, None]} for i in range(10)]}
    parsed = [(float(p), correct) for p, correct in data]
    if any(not math.isfinite(p) or not 0 <= p <= 1 or type(correct) is not bool for p, correct in parsed): raise ValueError("calibration row differs")
    wins = sum((p >= .5) == correct for p, correct in parsed); bins = []
    for i in range(10):
        subset = [(p, c) for p, c in parsed if i / 10 <= p < (i + 1) / 10 or (i == 9 and p == 1.0)]
        bin_wins = sum(c for _, c in subset)
        interval = wilson(bin_wins, len(subset)) if subset else (None, None)
        bins.append({"lower": i / 10, "upper": (i + 1) / 10, "n": len(subset), "accuracy": bin_wins / len(subset) if subset else None, "wilson_95": list(interval)})
    return {"n": n, "wins": wins, "losses": n - wins, "accuracy": f"{wins / n:.6f}", "brier": f"{sum(brier(p, c) for p, c in parsed) / n:.6f}", "log_loss": f"{sum(log_loss(p, c) for p, c in parsed) / n:.12f}", "wilson_95": list(wilson(wins, n)), "bins": bins}


def validate_calibration_report(value: object) -> dict[str, object]:
    required = {"schema", "content_sha256", "primary_claims", "settlement_heads", "primary_claim_refs", "settlement_head_refs", "primary_count", "pending_count", "excluded_count", "metrics"}
    if not isinstance(value, Mapping) or set(value) != required: raise ValueError("calibration report fields differ")
    safe = json.loads(canonical_json_bytes(value))
    if safe["schema"] != REPORT_SCHEMA or safe["content_sha256"] != _canonical_digest(safe): raise ValueError("calibration report digest differs")
    for key in ("primary_claims", "settlement_heads"):
        if not isinstance(safe[key], list) or any(not isinstance(path, str) for path in safe[key]): raise ValueError("calibration report sources differ")
        for path in safe[key]: validate_relative_path(Path(path))
    for key, paths in (("primary_claim_refs", safe["primary_claims"]), ("settlement_head_refs", safe["settlement_heads"])):
        refs = safe[key]
        if not isinstance(refs, list) or len(refs) != len(paths): raise ValueError("calibration report source refs differ")
        for ref, path in zip(refs, paths, strict=True):
            if not isinstance(ref, Mapping) or set(ref) != {"relative", "sha256"} or ref["relative"] != path or not isinstance(ref["sha256"], str) or len(ref["sha256"]) != 64: raise ValueError("calibration report source refs differ")
    if any(type(safe[key]) is not int or safe[key] < 0 for key in ("primary_count", "pending_count", "excluded_count")) or not isinstance(safe["metrics"], Mapping): raise ValueError("calibration report metrics differ")
    return safe


def _validated_head_chain(root: Path, head_path: Path) -> dict[str, object]:
    """Resolve one explicitly supplied fixed head without globbing."""
    _, head = _read(root, head_path, "settlement head")
    required = {"schema", "snapshot_sha256", "revision", "record_relative", "record_sha256"}
    if set(head) != required or head["schema"] != "PROSPECTIVE_ML_SETTLEMENT_HEAD_V1" or type(head["revision"]) is not int:
        raise ValueError("settlement head differs")
    record_path = _relative(root, str(head["record_relative"]), "settlement record")
    raw, record = _read(root, record_path, "settlement record"); record = validate_settlement(record)
    if head["record_sha256"] != _sha(raw) or head["snapshot_sha256"] != record["snapshot_sha256"] or head["revision"] != record["revision"]:
        raise ValueError("settlement head differs")
    current = record
    while current["revision"] > 1:
        prior_path = _relative(root, str(current["prior_record_relative"]), "prior settlement")
        prior_raw, prior = _read(root, prior_path, "prior settlement"); prior = validate_settlement(prior)
        if current["prior_record_sha256"] != _sha(prior_raw) or prior["revision"] != current["revision"] - 1 or prior["snapshot_sha256"] != current["snapshot_sha256"]:
            raise ValueError("settlement correction chain differs")
        current = prior
    snapshot = load_ml_snapshot(root, record["snapshot_relative"])
    if _sha(canonical_json_bytes(snapshot)) != record["snapshot_sha256"]:
        raise ValueError("settlement snapshot differs")
    evidence = load_official_result_evidence(root, record["result_receipt_relative"])
    result = _parse_result(evidence.raw)
    if (result != record["result"] or evidence.payload_sha256 != record["result_payload_sha256"] or evidence.receipt_sha256 != record["result_receipt_sha256"] or canonical_utc_ns(evidence.observed_at_utc) <= canonical_utc_ns(str(snapshot["observed_at_utc"]))):
        raise ValueError("settlement result binding differs")
    if result["game_pk"] != snapshot["game_pk"] or result["away_team"] != snapshot["away_team"] or result["home_team"] != snapshot["home_team"] or result["official_date"] != snapshot["target_date"]:
        raise ValueError("settlement result identity differs")
    return record


def build_calibration_report(root: Path, primary_claims: Iterable[Path | str], settlement_heads: Iterable[Path | str]) -> BuiltCalibrationReport:
    root = Path(root); claims = [_relative(root, p, "primary claim") for p in primary_claims]; heads = [_relative(root, p, "settlement head") for p in settlement_heads]
    primary: dict[str, dict[str, object]] = {}
    claim_refs = []
    for path in claims:
        claim_raw, _ = _read(root, path, "primary claim")
        claim_refs.append({"relative": path.as_posix(), "sha256": _sha(claim_raw)})
        claim = validate_primary_claim(root, path); candidate = _relative(root, str(claim["candidate_relative"]), "candidate"); snap = load_ml_snapshot(root, candidate)
        key = f"{snap['game_pk']}:{snap['model_card_sha256']}"
        if key in primary: raise ValueError("duplicate primary game/model")
        primary[key] = snap
    settled: dict[str, dict[str, object]] = {}
    head_refs = []
    for path in heads:
        head_raw, _ = _read(root, path, "settlement head")
        head_refs.append({"relative": path.as_posix(), "sha256": _sha(head_raw)})
        record = _validated_head_chain(root, path)
        if str(record["snapshot_sha256"]) in settled: raise ValueError("conflicting settlement heads")
        settled[str(record["snapshot_sha256"])] = record
    groups: dict[str, list[tuple[float, bool]]] = {"all_primary": [], "completeness_lt_0_50": [], "completeness_ge_0_50": []}
    pending = excluded = 0
    for snap in primary.values():
        model = f"model:{snap['model_card_sha256']}"
        groups.setdefault(model, [])
        groups.setdefault(f"{model}:completeness_lt_0_50", [])
        groups.setdefault(f"{model}:completeness_ge_0_50", [])
        record = settled.get(_sha(canonical_json_bytes(snap)))
        if record is None: pending += 1; continue
        result = record["result"]
        if result["status"] != "FINAL": excluded += 1; continue
        home_won = result["winner"] == snap["home_team"]
        probability = float(snap["p_home"]); groups["all_primary"].append((probability, home_won))
        stratum = "completeness_lt_0_50" if float(snap["data_completeness"]) < .5 else "completeness_ge_0_50"
        groups[stratum].append((probability, home_won))
        groups[model].append((probability, home_won))
        groups.setdefault(f"{model}:{stratum}", []).append((probability, home_won))
    report: dict[str, object] = {"schema": REPORT_SCHEMA, "primary_claims": [p.as_posix() for p in claims], "settlement_heads": [p.as_posix() for p in heads], "primary_claim_refs": claim_refs, "settlement_head_refs": head_refs, "primary_count": len(primary), "pending_count": pending, "excluded_count": excluded, "metrics": {key: calibration_metrics(value) for key, value in sorted(groups.items())}}
    report["content_sha256"] = _canonical_digest(report)
    report = validate_calibration_report(report)
    return BuiltCalibrationReport(root, report, canonical_json_bytes(report))


def persist_calibration_report(root: Path, built: BuiltCalibrationReport) -> Path:
    root = Path(root)
    if not isinstance(built, BuiltCalibrationReport) or built.root != root or _sha(built.report_bytes) != _sha(canonical_json_bytes(built.report)): raise ValueError("built calibration report differs")
    report = validate_calibration_report(built.report)
    rebuilt = build_calibration_report(root, report["primary_claims"], report["settlement_heads"])
    if rebuilt.report_bytes != built.report_bytes:
        raise ValueError("built calibration report differs")
    digest = _sha(built.report_bytes); path = Path("reports/prospective_ml_observation") / f"{digest}.json"
    def final_rebind() -> None:
        current = build_calibration_report(root, report["primary_claims"], report["settlement_heads"])
        if current.report_bytes != built.report_bytes: raise ValueError("calibration inputs changed during persistence")
    with open_trusted_root(root) as trusted:
        _write_idempotent(trusted, path.with_suffix(".json.sha256"), f"{digest}\n".encode(), "calibration report sidecar")
        _write_idempotent(trusted, path, built.report_bytes, "calibration report", final_rebind)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline prospective ML settlement/reporting")
    subs = parser.add_subparsers(dest="command", required=True)
    settle = subs.add_parser("settle"); settle.add_argument("--root", required=True); settle.add_argument("--snapshot", required=True); settle.add_argument("--result-receipt", required=True); settle.add_argument("--prior")
    report = subs.add_parser("report"); report.add_argument("--root", required=True); report.add_argument("--primary-claim", action="append", required=True); report.add_argument("--settlement-head", action="append")
    args = parser.parse_args(argv); root = Path(args.root)
    if args.command == "settle": print(persist_settlement(root, build_settlement(root, args.snapshot, args.result_receipt, prior=args.prior)).record_path.as_posix())
    else: print(persist_calibration_report(root, build_calibration_report(root, args.primary_claim, args.settlement_head or [])).as_posix())
    return 0


if __name__ == "__main__": raise SystemExit(main())
