"""Exact postseason research contracts and filesystem-bound scoring authority."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
from types import MappingProxyType

from .competition_policy import POSTSEASON_ML_RESEARCH_V1, history_population
from .live_model_card import EXPECTED_MODEL_CARD_SHA256, load_frozen_live_card_bytes
from .live_top3_contracts import (IntegrityError, MODEL_LABEL, canonical_json_bytes,
    _validate_source_bindings, _validate_execution_times, _validate_relative_posix,
    _validate_utc, _parse_utc, _validate_sha, _validate_sha_array, _validate_string_array,
    _validate_probability, _validate_content_sha, validate_target_date)
from .live_top3_trace import _ROW_KEYS, _validate_components, _validate_raw_json, _close
from .scoring import WEIGHTS, home_probability, overall_completeness, predicted_pick
from .scoring_prep import score_raw_game
from .secure_fs import open_trusted_root, open_parent, read_regular_snapshot, rebind_regular_snapshot

POLICY_KEYS = frozenset({"competition_scope", "research_policy_version", "history_population", "validation_status", "research_status", "domain_status"})
REPORT_KEYS = POLICY_KEYS | {"schema", "model_label", "target_date", "command_inputs", "executed_at_eastern", "executed_at_utc", "history_cutoff_date", "cache_authority", "model_card_sha256", "source_bindings", "games", "warnings", "market_status", "market_reason", "totals_status", "totals_reason", "content_sha256"}
GAME_KEYS = frozenset({"game_pk", "game_type", "target_date", "away_team_id", "home_team_id", "away_team", "home_team", "scheduled_start_utc", "state", "reason_codes", "source_binding_sha256s", "observation_boundary_utc", "model_card_sha256", "feature_sha256", "p_home", "predicted_side", "predicted_winner", "pick_probability", "data_completeness", "starter_edge_abs", "starter_provenance", "lineup_provenance", "schedule_quality_flags"})
TRACE_KEYS = POLICY_KEYS | {"schema", "not_a_betting_recommendation", "primary_report_relative", "primary_report_content_sha256", "model_card_sha256", "games", "content_sha256"}
TRACE_ROW_KEYS = (_ROW_KEYS - {"primary_rank", "disposition"}) | {"game_type", "away_team_id", "home_team_id", "state", "observation_boundary_utc", "source_binding_sha256s", "away_qualities", "home_qualities", "edge"}
CACHE_KEYS = frozenset({"namespace", "cutoff_date", "catalog_sha256", "competition_scope", "research_policy_version", "history_population", "coverage", "coverage_sha256"})
_MODEL_FIELDS = ("model_card_sha256", "feature_sha256", "p_home", "predicted_side", "predicted_winner", "pick_probability", "data_completeness", "starter_edge_abs", "starter_provenance", "lineup_provenance")


@dataclass(frozen=True)
class LoadedPostseasonResearch:
    report: Mapping[str, object]
    trace: Mapping[str, object]
    trace_relative: Path
    trace_sha256: str


def policy_envelope():
    return {"competition_scope": "postseason-research-v1", "research_policy_version": "POSTSEASON_ML_RESEARCH_V1",
            "history_population": history_population(POSTSEASON_ML_RESEARCH_V1),
            "validation_status": "NOT_POSTSEASON_VALIDATED", "research_status": "RESEARCH_ONLY", "domain_status": "POSTSEASON_OUT_OF_DOMAIN"}


def _policy(value):
    expected = policy_envelope()
    if any(value.get(key) != item for key, item in expected.items()):
        raise IntegrityError("postseason research policy differs")


def _copy(value, keys, label):
    if not isinstance(value, Mapping) or set(value) != keys:
        raise IntegrityError(f"postseason {label} fields differ")
    try:
        return json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise IntegrityError(f"postseason {label} JSON differs") from exc


def _coverage(value, cutoff):
    coverage = _copy(value, {"start_date", "cutoff_date", "game_identities", "played_game_pks"}, "coverage")
    if validate_target_date(coverage["start_date"]) > cutoff or coverage["cutoff_date"] != cutoff:
        raise IntegrityError("postseason coverage range differs")
    identities = coverage["game_identities"]
    if not isinstance(identities, list):
        raise IntegrityError("postseason coverage identities differ")
    seen = set()
    for identity in identities:
        if not isinstance(identity, dict) or set(identity) != {"game_pk", "official_date", "status", "game_type", "away_team_id", "home_team_id", "doubleheader_marker"}:
            raise IntegrityError("postseason coverage identity fields differ")
        for key in ("game_pk", "away_team_id", "home_team_id"):
            if type(identity[key]) is not int or identity[key] <= 0:
                raise IntegrityError("postseason coverage identity differs")
        if identity["game_pk"] in seen or identity["game_type"] not in POSTSEASON_ML_RESEARCH_V1.recent_game_types:
            raise IntegrityError("postseason coverage game population differs")
        seen.add(identity["game_pk"])
        if not coverage["start_date"] <= validate_target_date(identity["official_date"]) <= cutoff:
            raise IntegrityError("postseason coverage date differs")
        if not isinstance(identity["status"], str) or not identity["status"] or identity["doubleheader_marker"] is not None and not isinstance(identity["doubleheader_marker"], str):
            raise IntegrityError("postseason coverage status differs")
    played = coverage["played_game_pks"]
    if not isinstance(played, list) or played != sorted(set(played)) or any(type(pk) is not int or pk not in seen for pk in played):
        raise IntegrityError("postseason played coverage differs")
    return coverage


def validate_policy_query(identity, role, target_date):
    if not isinstance(identity, Mapping):
        raise IntegrityError("postseason source query differs")
    for key in ("competition_scope", "research_policy_version", "history_population"):
        if identity.get(key) != policy_envelope()[key]:
            raise IntegrityError("postseason source query policy differs")
    cutoff = date.fromordinal(date.fromisoformat(target_date).toordinal() - 1).isoformat()
    policy_keys = {"competition_scope", "research_policy_version", "history_population"}
    from .live_sources import live_schedule_url, boxscore_url
    if role in {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}:
        keys = {"url", "observation_id", "target_date", "competition_scope", "research_policy_version", "history_population"}
        if role != "LIVE_SCHEDULE":
            keys.add("game_pk")
            keys.update(("game_type", "away_team_id", "home_team_id"))
        if set(identity) != keys or identity["target_date"] != target_date:
            raise IntegrityError("postseason live source query identity differs")
        expected_url = live_schedule_url(target_date, target_date, competition_policy=POSTSEASON_ML_RESEARCH_V1)
        if role != "LIVE_SCHEDULE":
            for key in ("game_pk", "away_team_id", "home_team_id"):
                if type(identity[key]) is not int or identity[key] <= 0:
                    raise IntegrityError("postseason live query game identity differs")
            if identity["game_type"] not in POSTSEASON_ML_RESEARCH_V1.target_game_types:
                raise IntegrityError("postseason live query game type differs")
        if role == "LIVE_PARTICIPANTS":
            expected_url = boxscore_url(identity["game_pk"])
    elif identity.get("as_of_date") != cutoff:
        raise IntegrityError("postseason history source cutoff differs")
    else:
        common = policy_keys | {"url", "as_of_date"}
        extras = {"HISTORY_SCHEDULE": {"start", "end", "gameType", "role", "coverage", "coverage_sha256", "coverage_game_pks", "schedule_coverage_sha256"},
                  "HISTORY_STATCAST": {"start", "end", "game_type", "coverage", "coverage_sha256", "coverage_game_pks", "schedule_coverage_sha256"},
                  "HISTORY_ERA": {"season_open"}, "HISTORY_VENUE": {"venue_id"}, "CAREER_BF": {"pitcher_id"}}
        if role not in extras or set(identity) != common | extras[role]:
            raise IntegrityError("postseason history query fields differ")
        from .live_history import live_career_bf_url, _venue_url
        if role == "CAREER_BF":
            expected_url = live_career_bf_url(identity["pitcher_id"], cutoff)
        elif role == "HISTORY_VENUE":
            if type(identity["venue_id"]) is not int or identity["venue_id"] <= 0:
                raise IntegrityError("postseason venue query identity differs")
            expected_url = _venue_url(identity["venue_id"])
        elif role == "HISTORY_ERA":
            from .mlb_stats import league_era_url
            if validate_target_date(identity["season_open"]) > cutoff or identity["season_open"][:4] != target_date[:4]:
                raise IntegrityError("postseason ERA query range differs")
            expected_url = league_era_url(identity["season_open"], cutoff)
        elif role == "HISTORY_STATCAST":
            from .statcast import statcast_url
            if identity["game_type"] != "R,F,D,L,W" or not f"{target_date[:4]}-01-01" <= validate_target_date(identity["start"]) <= validate_target_date(identity["end"]) <= cutoff:
                raise IntegrityError("postseason Statcast query range/population differs")
            expected_url = statcast_url(identity["start"], identity["end"], game_types=POSTSEASON_ML_RESEARCH_V1.recent_game_types)
        else:
            from urllib.parse import urlencode
            from .sources import MLB_BASE
            if identity["start"] != f"{target_date[:4]}-01-01" or identity["end"] != cutoff or identity["gameType"] != "R,F,D,L,W" or identity["role"] != "history":
                raise IntegrityError("postseason schedule query range/population differs")
            expected_url = MLB_BASE + "/schedule?" + urlencode({"sportId": 1, "gameType": "R,F,D,L,W", "startDate": identity["start"], "endDate": cutoff, "hydrate": "probablePitcher,venue,linescore"})
    if identity.get("url") != expected_url:
        raise IntegrityError("postseason source query URL/population differs")
    if role in {"HISTORY_SCHEDULE", "HISTORY_STATCAST"}:
        coverage = _coverage(identity.get("coverage"), cutoff)
        digest = hashlib.sha256(canonical_json_bytes(coverage)).hexdigest()
        if identity.get("coverage_sha256") != digest or identity.get("schedule_coverage_sha256") != digest or identity.get("coverage_game_pks") != coverage["played_game_pks"]:
            raise IntegrityError("postseason source coverage digest differs")


def _identity(row, target=None):
    for field in ("game_pk", "away_team_id", "home_team_id"):
        if type(row[field]) is not int or row[field] <= 0:
            raise IntegrityError(f"postseason {field} differs")
    if row["away_team_id"] == row["home_team_id"]:
        raise IntegrityError("postseason teams overlap")
    validate_target_date(row["target_date"])
    if target is not None and row["target_date"] != target:
        raise IntegrityError("postseason target date differs")
    for field in ("game_type", "away_team", "home_team"):
        if not isinstance(row[field], str) or not row[field]:
            raise IntegrityError(f"postseason {field} differs")
    _validate_utc(row["scheduled_start_utc"], "scheduled_start_utc")
    if row["observation_boundary_utc"] is not None:
        _validate_utc(row["observation_boundary_utc"], "observation_boundary_utc")
    _validate_sha_array(row["source_binding_sha256s"])
    if row["state"] not in {"VERIFIED", "PRELIMINARY", "UNSCORED", "EXCLUDED"}:
        raise IntegrityError("postseason state differs")


def _consumed_binding_closure(bindings, game_pk, starter_ids=()):
    """Derive consumed evidence from admitted roles/identities, never row refs."""
    closure = [binding for binding in bindings
               if binding["binding_role"].startswith("HISTORY_") or
               binding["binding_role"] == "LIVE_SCHEDULE" and game_pk in binding["game_pks"] or
               binding["binding_role"] in {"LIVE_PARTICIPANTS", "FINAL_SCHEDULE"} and list(binding["game_pks"]) == [game_pk]]
    if len(starter_ids) == 2 and all(type(pk) is int and pk > 0 for pk in starter_ids):
        closure.extend(binding for binding in bindings if binding["binding_role"] == "CAREER_BF" and
                       json.loads(binding["query_identity_json"])["pitcher_id"] in starter_ids)
    return sorted(closure, key=lambda binding: binding["binding_sha256"])


def validate_postseason_report(value):
    safe = _copy(value, REPORT_KEYS, "report")
    _policy(safe)
    if safe["schema"] != "POSTSEASON_ML_RESEARCH_REPORT_V1" or safe["model_label"] != MODEL_LABEL:
        raise IntegrityError("postseason report schema/model differs")
    target = validate_target_date(safe["target_date"])
    cutoff = date.fromordinal(date.fromisoformat(target).toordinal() - 1).isoformat()
    if safe["history_cutoff_date"] != cutoff:
        raise IntegrityError("postseason history cutoff differs")
    commands = _copy(safe["command_inputs"], {"date", "model_card", "competition_scope"}, "commands")
    if commands["date"] != target or commands["competition_scope"] != safe["competition_scope"]:
        raise IntegrityError("postseason commands differ")
    _validate_relative_posix(commands["model_card"], "model_card")
    _validate_execution_times(safe["executed_at_eastern"], safe["executed_at_utc"])
    cache = _copy(safe["cache_authority"], CACHE_KEYS, "cache authority")
    if cache["namespace"] != "experimental_daily/history_cache_postseason_research_v1" or cache["cutoff_date"] != cutoff:
        raise IntegrityError("postseason cache namespace/cutoff differs")
    for key in ("competition_scope", "research_policy_version", "history_population"):
        if cache[key] != safe[key]:
            raise IntegrityError("postseason cache policy differs")
    from .live_history import validate_research_history_authority
    validate_research_history_authority(cache, cutoff)
    coverage = _coverage(cache["coverage"], cutoff)
    if cache["coverage_sha256"] != hashlib.sha256(canonical_json_bytes(coverage)).hexdigest():
        raise IntegrityError("postseason cache coverage digest differs")
    _validate_sha(cache["catalog_sha256"], "catalog_sha256")
    if safe["model_card_sha256"] != EXPECTED_MODEL_CARD_SHA256:
        raise IntegrityError("postseason frozen card differs")
    bindings = _validate_source_bindings(safe["source_bindings"])
    for binding in bindings.values():
        query = json.loads(binding["query_identity_json"])
        validate_policy_query(query, binding["binding_role"], target)
        if binding["source"] != ("savant" if binding["binding_role"] == "HISTORY_STATCAST" else "stats_api"):
            raise IntegrityError("postseason source role differs")
        if binding["binding_role"] in {"HISTORY_SCHEDULE", "HISTORY_STATCAST"} and query["coverage"] != coverage:
            raise IntegrityError("postseason history coverage population differs")
        if binding["binding_role"] == "HISTORY_ERA" and query["season_open"] != coverage["start_date"]:
            raise IntegrityError("postseason ERA opening cutoff differs")
    schedules = [b for b in bindings.values() if b["binding_role"] == "LIVE_SCHEDULE"]
    if len(schedules) != 1:
        raise IntegrityError("postseason schedule cardinality differs")
    if not isinstance(safe["games"], list):
        raise IntegrityError("postseason games must be array")
    seen = set()
    for row in safe["games"]:
        if not isinstance(row, dict) or set(row) != GAME_KEYS:
            raise IntegrityError("postseason game fields differ")
        _identity(row, target)
        if row["game_pk"] in seen:
            raise IntegrityError("postseason duplicate game")
        seen.add(row["game_pk"])
        _validate_string_array(row["reason_codes"], "reason_codes")
        _validate_string_array(row["schedule_quality_flags"], "schedule_quality_flags")
        relevant = [bindings[digest] for digest in row["source_binding_sha256s"] if digest in bindings]
        if len(relevant) != len(row["source_binding_sha256s"]) or not relevant:
            raise IntegrityError("postseason game source references differ")
        noncareer = _consumed_binding_closure(bindings.values(), row["game_pk"])
        referenced_careers = [binding for binding in relevant if binding["binding_role"] == "CAREER_BF"]
        expected_refs = sorted(binding["binding_sha256"] for binding in noncareer + referenced_careers)
        if row["source_binding_sha256s"] != expected_refs or len(referenced_careers) > 2 or row["p_home"] is not None and len(referenced_careers) != 2:
            raise IntegrityError("postseason consumed evidence closure differs")
        if row["observation_boundary_utc"] != max((b["durable_at_utc"] for b in relevant), key=_parse_utc):
            raise IntegrityError("postseason observation boundary differs")
        if row["game_type"] not in POSTSEASON_ML_RESEARCH_V1.target_game_types and (row["state"] != "EXCLUDED" or "UNSUPPORTED_GAME_TYPE" not in row["reason_codes"]):
            raise IntegrityError("postseason unsupported row disposition differs")
        if row["p_home"] is None:
            if any(row[field] is not None for field in _MODEL_FIELDS) or row["state"] not in {"UNSCORED", "EXCLUDED"}:
                raise IntegrityError("postseason unavailable model fields differ")
        else:
            _validate_probability(row["p_home"], "p_home")
            _validate_probability(row["pick_probability"], "pick_probability")
            _validate_probability(row["data_completeness"], "data_completeness")
            if isinstance(row["starter_edge_abs"], bool) or not isinstance(row["starter_edge_abs"], (int, float)) or not math.isfinite(row["starter_edge_abs"]) or not 0 <= row["starter_edge_abs"] <= 100:
                raise IntegrityError("postseason starter edge differs")
            _validate_sha(row["feature_sha256"], "feature_sha256")
            if row["model_card_sha256"] != safe["model_card_sha256"] or row["game_type"] not in POSTSEASON_ML_RESEARCH_V1.target_game_types:
                raise IntegrityError("postseason scored model/type differs")
            pick = predicted_pick(row["home_team"], row["away_team"], row["p_home"])
            if pick["predicted_side"] == "none":
                pick["predicted_winner"] = None
            if any(row[key] != pick[key] for key in ("predicted_side", "predicted_winner", "pick_probability")):
                raise IntegrityError("postseason prediction consistency differs")
            if row["predicted_side"] == "none":
                if row["state"] != "UNSCORED" or row["reason_codes"] != ["EXACT_NEUTRAL_SCORE"]:
                    raise IntegrityError("postseason neutral state differs")
            elif row["state"] not in {"VERIFIED", "PRELIMINARY"}:
                raise IntegrityError("postseason scored state differs")
            for field in ("starter_provenance", "lineup_provenance"):
                if not isinstance(row[field], str) or not row[field]:
                    raise IntegrityError("postseason participant provenance differs")
            if _parse_utc(row["observation_boundary_utc"]) >= _parse_utc(row["scheduled_start_utc"]):
                raise IntegrityError("postseason scored observation is late")
    if schedules[0]["game_pks"] != sorted(seen):
        raise IntegrityError("postseason complete slate population differs")
    rows_by_pk = {row["game_pk"]: row for row in safe["games"]}
    for binding in bindings.values():
        if binding["binding_role"] in {"LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}:
            query = json.loads(binding["query_identity_json"])
            pk = query["game_pk"]
            if binding["game_pks"] != [pk] or pk not in rows_by_pk or any(query[key] != rows_by_pk[pk][key] for key in ("game_type", "away_team_id", "home_team_id")):
                raise IntegrityError("postseason per-game query official identity differs")
    for field, expected in {"market_status": "NOT_EVALUATED", "market_reason": "POSTSEASON_NOT_VALIDATED", "totals_status": "NOT_EVALUATED", "totals_reason": "POSTSEASON_TOTALS_NOT_SUPPORTED"}.items():
        if safe[field] != expected:
            raise IntegrityError("postseason market/totals exclusion differs")
    _validate_string_array(safe["warnings"], "warnings")
    _validate_content_sha(safe)
    return safe


def validate_postseason_trace(value):
    safe = _copy(value, TRACE_KEYS, "trace")
    _policy(safe)
    if safe["schema"] != "POSTSEASON_ML_RESEARCH_TRACE_V1" or safe["not_a_betting_recommendation"] is not True:
        raise IntegrityError("postseason trace schema differs")
    _validate_relative_posix(safe["primary_report_relative"], "primary_report_relative")
    _validate_sha(safe["primary_report_content_sha256"], "primary_report_content_sha256")
    if safe["model_card_sha256"] != EXPECTED_MODEL_CARD_SHA256 or not isinstance(safe["games"], list):
        raise IntegrityError("postseason trace model/games differs")
    seen = set()
    for row in safe["games"]:
        if not isinstance(row, dict) or set(row) != TRACE_ROW_KEYS:
            raise IntegrityError("postseason trace row fields differ")
        _identity(row)
        if row["game_pk"] in seen or row["game_type"] not in POSTSEASON_ML_RESEARCH_V1.target_game_types:
            raise IntegrityError("postseason trace game population differs")
        seen.add(row["game_pk"])
        raw = row["raw_features"]
        _validate_raw_json(raw)
        if not isinstance(raw, dict) or raw.get("eligible") is not True or raw.get("game_pk") != row["game_pk"] or raw.get("official_date") != row["target_date"]:
            raise IntegrityError("postseason raw feature identity differs")
        if row["feature_sha256"] != hashlib.sha256(canonical_json_bytes(raw)).hexdigest() or row["model_card_sha256"] != safe["model_card_sha256"]:
            raise IntegrityError("postseason trace feature/card digest differs")
        for field in ("away_scores", "home_scores", "qualities", "away_qualities", "home_qualities", "contributions"):
            _validate_components(row[field], field, -100 if field == "contributions" else 0, 1 if "qualities" in field else 100)
        for component in WEIGHTS:
            _close(row["contributions"][component], WEIGHTS[component] * (row["home_scores"][component] - row["away_scores"][component]), component)
        _close(row["total_edge"], math.fsum(row["contributions"].values()), "total_edge")
        _close(row["edge"], row["total_edge"], "edge")
        _validate_probability(row["p_home"], "p_home")
        pick = predicted_pick(row["home_team"], row["away_team"], row["p_home"])
        if row["predicted_side"] != pick["predicted_side"] or row["pick_probability"] != pick["pick_probability"]:
            raise IntegrityError("postseason trace prediction differs")
        if row["state"] != "UNSCORED" and row["predicted_side"] == "none" or row["state"] == "UNSCORED" and row["predicted_side"] != "none" or row["state"] == "EXCLUDED":
            raise IntegrityError("postseason trace state differs")
        if row["observation_boundary_utc"] is None or _parse_utc(row["observation_boundary_utc"]) >= _parse_utc(row["scheduled_start_utc"]):
            raise IntegrityError("postseason trace boundary differs")
    _validate_content_sha(safe)
    return safe


def build_postseason_trace(*, primary_report_relative, report, games):
    value = {"schema": "POSTSEASON_ML_RESEARCH_TRACE_V1", **policy_envelope(), "not_a_betting_recommendation": True,
             "primary_report_relative": str(primary_report_relative), "primary_report_content_sha256": report["content_sha256"],
             "model_card_sha256": report["model_card_sha256"], "games": list(games)}
    value["content_sha256"] = hashlib.sha256(canonical_json_bytes(value)).hexdigest()
    return validate_postseason_trace(value)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _relative(root, value):
    supplied = Path(value)
    if supplied.is_absolute():
        try:
            supplied = supplied.relative_to(root)
        except ValueError as exc:
            raise IntegrityError("postseason path escapes root") from exc
    _validate_relative_posix(supplied.as_posix(), "research path")
    return supplied


def load_postseason_research(root: Path, trace: Path | str) -> LoadedPostseasonResearch:
    """Read/rebind every immutable input; a fresh call is publication authority."""
    root = Path(os.path.abspath(root))
    relative = _relative(root, trace)
    snapshots = {}
    with open_trusted_root(root) as trusted:
        def read(path, expected=None, *, canonical=False):
            path = _relative(root, path)
            snapshot = snapshots.get(path)
            if snapshot is None:
                snapshot = read_regular_snapshot(trusted, path, "postseason research evidence", max_bytes=256 * 1024 * 1024)
                if snapshot is None:
                    raise IntegrityError("postseason evidence missing")
                snapshots[path] = snapshot
            if expected is not None and hashlib.sha256(snapshot.contents).hexdigest() != expected:
                raise IntegrityError("postseason evidence file digest differs")
            if canonical:
                parsed = json.loads(snapshot.contents)
                if canonical_json_bytes(parsed) != snapshot.contents:
                    raise IntegrityError("postseason evidence canonical bytes differ")
                return parsed
            return snapshot.contents

        trace_bytes = read(relative)
        trace_value = validate_postseason_trace(json.loads(trace_bytes))
        if canonical_json_bytes(trace_value) != trace_bytes:
            raise IntegrityError("postseason trace canonical bytes differ")
        report_relative = _relative(root, trace_value["primary_report_relative"])
        report_bytes = read(report_relative)
        report = validate_postseason_report(json.loads(report_bytes))
        if canonical_json_bytes(report) != report_bytes or report["content_sha256"] != trace_value["primary_report_content_sha256"] or report["model_card_sha256"] != trace_value["model_card_sha256"]:
            raise IntegrityError("postseason report/trace binding differs")
        if relative != report_relative.with_suffix(".trace.json") or report_relative.parts[:2] != ("reports", "postseason_ml_research") or report_relative.parts[2] != report["target_date"]:
            raise IntegrityError("postseason report/trace path differs")
        for path in (relative, report_relative):
            if read(path.with_suffix(path.suffix + ".sha256")) != f"{hashlib.sha256(read(path)).hexdigest()}\n".encode():
                raise IntegrityError("postseason artifact sidecar differs")
        stamp = report["executed_at_utc"].replace("-", "").replace(":", "").replace(".", "")
        if report_relative.name != f"{stamp}-{report['content_sha256'][:16]}.json":
            raise IntegrityError("postseason content-addressed report name differs")
        card = load_frozen_live_card_bytes(read(report["command_inputs"]["model_card"], report["model_card_sha256"]))
        payloads, manifests = {}, {}
        for binding in report["source_bindings"]:
            payload = read(binding["payload_relative"], binding["payload_sha256"])
            if len(payload) != binding["byte_length"]:
                raise IntegrityError("postseason source payload length differs")
            receipt_path = _relative(root, binding["receipt_relative"])
            receipt = read(receipt_path, binding["receipt_sha256"], canonical=True)
            is_live = binding["binding_role"] in {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE"}
            if is_live:
                receipt_keys = {"schema_version", "source", "binding_role", "observation_id", "query_identity_json", "requested_url", "final_url", "started_at_utc", "response_completed_at_utc", "http_status", "content_type", "etag", "last_modified", "payload_sha256", "byte_length", "payload_relative", "manifest_sha256", "manifest_relative", "claim_relative", "claim_sha256"}
                if set(receipt) != receipt_keys or receipt["schema_version"] != 1:
                    raise IntegrityError("postseason live receipt fields differ")
                namespace = "live_receipts_postseason_research_v1"
            else:
                from .live_history import _validate_receipt_value
                _validate_receipt_value(receipt)
                if receipt["game_pks"] != binding["game_pks"]:
                    raise IntegrityError("postseason history receipt population differs")
                namespace = "history_receipts_postseason_research_v1"
            if receipt_path != Path("audit/experimental_daily") / namespace / f"{binding['observation_id']}.json":
                raise IntegrityError("postseason receipt path identity differs")
            for key in ("source", "binding_role", "observation_id", "query_identity_json", "requested_url", "final_url", "response_completed_at_utc", "http_status", "content_type", "etag", "last_modified", "payload_sha256", "byte_length", "payload_relative", "manifest_sha256"):
                if receipt.get(key) != binding[key]:
                    raise IntegrityError("postseason source receipt binding differs")
            query = json.loads(binding["query_identity_json"])
            validate_policy_query(query, binding["binding_role"], report["target_date"])
            manifest_path = receipt.get("manifest_relative")
            if manifest_path is None:
                payload_path = _relative(root, binding["payload_relative"])
                found = []
                with open_parent(trusted, payload_path, create=False) as descriptor:
                    for name in sorted(os.listdir(descriptor)):
                        if name.startswith(binding["payload_sha256"] + ".") and name.endswith(".manifest.json"):
                            candidate = payload_path.parent / name
                            contents = read(candidate)
                            if hashlib.sha256(contents).hexdigest() == binding["manifest_sha256"]:
                                found.append(candidate)
                if len(found) != 1:
                    raise IntegrityError("postseason source manifest population differs")
                manifest_path = found[0]
            manifest_bytes = read(manifest_path, binding["manifest_sha256"])
            manifest = json.loads(manifest_bytes)
            if set(manifest) != {"source", "query", "sha256", "retrieved_at_utc", "row_count", "schema_version"} or manifest.get("sha256") != binding["payload_sha256"] or manifest.get("schema_version") != 1 or not isinstance(manifest.get("query"), dict):
                raise IntegrityError("postseason raw manifest identity differs")
            manifest_query = manifest["query"]
            for key in ("competition_scope", "research_policy_version", "history_population"):
                if manifest_query.get(key) != query[key]:
                    raise IntegrityError("postseason raw source policy differs")
            if manifest_query.get("url") != binding["requested_url"]:
                raise IntegrityError("postseason raw source URL differs")
            sources = {"LIVE_SCHEDULE": "mlb_live_schedule", "LIVE_PARTICIPANTS": "mlb_live_boxscore", "FINAL_SCHEDULE": "mlb_live_final_schedule", "HISTORY_SCHEDULE": "mlb_schedule", "HISTORY_STATCAST": "baseball_savant_statcast", "HISTORY_ERA": "mlb_live_history_era", "HISTORY_VENUE": "mlb_live_history_venue", "CAREER_BF": "mlb_live_career_bf"}
            source = sources[binding["binding_role"]] + "_postseason_research_v1"
            if manifest["source"] != source or manifest_query != query:
                raise IntegrityError("postseason raw manifest source/query differs")
            folder = Path("data/experimental_daily") / ("live_observations" if is_live else "history_cache_postseason_research_v1") / source
            expected_payload = folder / f"{binding['payload_sha256']}.raw"
            query_sha = hashlib.sha256(canonical_json_bytes(query).rstrip(b"\n")).hexdigest()
            expected_manifest = folder / f"{binding['payload_sha256']}.{query_sha}.manifest.json"
            if Path(binding["payload_relative"]) != expected_payload or Path(manifest_path) != expected_manifest:
                raise IntegrityError("postseason raw artifact path identity differs")
            from .live_sources import _validate_raw_manifest
            from .models import RawArtifact
            artifact = RawArtifact(source, binding["payload_sha256"], root / expected_payload, root / expected_manifest,
                                   manifest["retrieved_at_utc"], manifest["row_count"])
            seal = Path(manifest_path).with_name(Path(manifest_path).name.removesuffix(".json") + ".sha256")
            if read(seal) != f"{binding['manifest_sha256']}\n".encode():
                raise IntegrityError("postseason manifest seal differs")
            _validate_raw_manifest(manifest_bytes=manifest_bytes, seal_bytes=read(seal), raw_source=source,
                                   query=query, payload_bytes=payload, artifact=artifact)
            completion_path = receipt_path.with_name(receipt_path.name.removesuffix(".json") + ".completion.json")
            completion = read(completion_path, canonical=True)
            if completion.get("durable_at_utc") != binding["durable_at_utc"] or completion.get("receipt_sha256") != binding["receipt_sha256"]:
                raise IntegrityError("postseason completion binding differs")
            if read(completion_path.with_suffix(".sha256")) != f"{hashlib.sha256(read(completion_path)).hexdigest()}\n".encode():
                raise IntegrityError("postseason completion seal differs")
            if is_live:
                from .live_sources import _validate_observation_completion
                _validate_observation_completion(read(completion_path), {"schema_version": 1, "source": binding["source"], "binding_role": binding["binding_role"], "observation_id": binding["observation_id"], "query_identity_json": binding["query_identity_json"], "receipt_relative": binding["receipt_relative"], "receipt_sha256": binding["receipt_sha256"], "payload_sha256": binding["payload_sha256"], "manifest_sha256": binding["manifest_sha256"]})
                if receipt.get("claim_relative") is not None:
                    from .live_sources import _claim_bytes, _validate_http_observation
                    from .live_top3_contracts import LiveHttpObservation
                    http = LiveHttpObservation(binding["requested_url"], binding["final_url"], receipt["started_at_utc"], binding["response_completed_at_utc"], binding["http_status"], binding["content_type"], binding["etag"], binding["last_modified"], payload)
                    _validate_http_observation(http, source=binding["source"], requested_url=binding["requested_url"])
                    expected_claim_path = Path("audit/experimental_daily/live_observation_claims_postseason_research_v1") / f"{binding['observation_id']}.json"
                    claim = read(receipt["claim_relative"], receipt["claim_sha256"])
                    expected_claim = _claim_bytes(raw_source=source, source=binding["source"], binding_role=binding["binding_role"], observation_id=binding["observation_id"], query_identity_json=binding["query_identity_json"], http=http)
                    if Path(receipt["claim_relative"]) != expected_claim_path or claim != expected_claim:
                        raise IntegrityError("postseason live claim differs")
            else:
                if set(completion) != {"receipt_sha256", "durable_at_utc"}:
                    raise IntegrityError("postseason history completion fields differ")
                claim_path = receipt_path.with_suffix(".claim.json")
                claim = read(claim_path, canonical=True)
                if claim != {"receipt": receipt, "receipt_sha256": binding["receipt_sha256"]} or read(receipt_path.with_suffix(".sha256")) != f"{binding['receipt_sha256']}\n".encode():
                    raise IntegrityError("postseason history receipt seal/claim differs")
            payloads[binding["binding_sha256"]] = payload
            manifests[binding["manifest_sha256"]] = manifest
        _validate_loaded_sources(report, trace_value, card, payloads, manifests)
        for snapshot in snapshots.values():
            rebind_regular_snapshot(trusted, snapshot, "postseason research evidence", max_bytes=256 * 1024 * 1024)
        return LoadedPostseasonResearch(_freeze(report), _freeze(trace_value), relative, hashlib.sha256(trace_bytes).hexdigest())


def _validate_loaded_sources(report, trace, card, payloads, manifests):
    from .live_sources import parse_live_schedule, parse_pregame_boxscore, compare_probable_pitchers
    bindings = {b["binding_sha256"]: b for b in report["source_bindings"]}
    schedule_binding = next(b for b in bindings.values() if b["binding_role"] == "LIVE_SCHEDULE")
    schedule = {g["game_pk"]: g for g in parse_live_schedule(payloads[schedule_binding["binding_sha256"]], requested_date=report["target_date"], competition_policy=POSTSEASON_ML_RESEARCH_V1)}
    if set(schedule) != {row["game_pk"] for row in report["games"]}:
        raise IntegrityError("postseason official slate population differs")
    trace_rows = {row["game_pk"]: row for row in trace["games"]}
    scored = {row["game_pk"] for row in report["games"] if row["p_home"] is not None}
    if set(trace_rows) != scored:
        raise IntegrityError("postseason scored trace cardinality differs")
    history_bindings = [b for b in bindings.values() if b["binding_role"].startswith("HISTORY_")]
    catalog = [{"source": manifests[b["manifest_sha256"]]["source"], "query": manifests[b["manifest_sha256"]]["query"], "manifest_sha256": b["manifest_sha256"]} for b in history_bindings]
    catalog.sort(key=lambda row: (row["source"], canonical_json_bytes(row["query"]).decode().removesuffix("\n")))
    if report["cache_authority"]["catalog_sha256"] != hashlib.sha256(canonical_json_bytes(catalog)).hexdigest():
        raise IntegrityError("postseason history catalog differs")
    history, careers = _reconstruct_history(report, card, payloads)
    from .live_features import prepare_live_feature_history, build_live_raw_game_features
    from .live_top3_contracts import SourceBinding, FinalGameObservation
    prepared = prepare_live_feature_history(history, target_game_pks=tuple(sorted(schedule)), competition_policy=POSTSEASON_ML_RESEARCH_V1)
    for row in report["games"]:
        game, pk = schedule[row["game_pk"]], row["game_pk"]
        for field in ("game_type", "away_team_id", "home_team_id", "away_team", "home_team", "scheduled_start_utc"):
            if row[field] != game[field]:
                raise IntegrityError("postseason official game identity differs")
        if row["target_date"] != game["official_date"]:
            raise IntegrityError("postseason official date differs")
        participants = [b for b in bindings.values() if b["binding_role"] == "LIVE_PARTICIPANTS" and b["game_pks"] == [pk]]
        finals = [b for b in bindings.values() if b["binding_role"] == "FINAL_SCHEDULE" and b["game_pks"] == [pk]]
        if len(participants) > 1 or len(finals) > 1:
            raise IntegrityError("postseason participant/final cardinality differs")
        observed = None if not participants else compare_probable_pitchers(game, parse_pregame_boxscore(payloads[participants[0]["binding_sha256"]], game_pk=pk))
        starters = () if observed is None else (observed.away_starter_id, observed.home_starter_id)
        closure = _consumed_binding_closure(bindings.values(), pk, starters)
        if row["source_binding_sha256s"] != [binding["binding_sha256"] for binding in closure]:
            raise IntegrityError("postseason authenticated consumed evidence closure differs")
        boundary = max((binding["durable_at_utc"] for binding in closure), key=_parse_utc)
        if row["observation_boundary_utc"] != boundary:
            raise IntegrityError("postseason authenticated observation boundary differs")
        if pk not in trace_rows:
            continue
        traced = trace_rows[pk]
        for key in ("game_pk", "game_type", "target_date", "away_team_id", "home_team_id", "away_team", "home_team", "scheduled_start_utc", "state", "observation_boundary_utc", "source_binding_sha256s", "model_card_sha256", "feature_sha256", "p_home", "predicted_side", "pick_probability"):
            if traced[key] != row[key]:
                raise IntegrityError("postseason report/trace row agreement differs")
        if len(participants) != 1 or len(finals) != 1:
            raise IntegrityError("postseason participant/final cardinality differs")
        if not observed.complete or observed.reason_codes != ("PROBABLE_CONSISTENT",) or observed.away_team_id != game["away_team_id"] or observed.home_team_id != game["home_team_id"]:
            raise IntegrityError("postseason participant official identity differs")
        final_games = parse_live_schedule(payloads[finals[0]["binding_sha256"]], requested_date=report["target_date"], competition_policy=POSTSEASON_ML_RESEARCH_V1)
        final_game = next((g for g in final_games if g["game_pk"] == pk), None)
        if final_game != game:
            raise IntegrityError("postseason final schedule agreement differs")
        raw = traced["raw_features"]
        final = FinalGameObservation(pk, report["target_date"], final_game, SourceBinding(**finals[0]))
        recomputed = build_live_raw_game_features(observed, final, history, careers, prepared_history=prepared, competition_policy=POSTSEASON_ML_RESEARCH_V1)
        if canonical_json_bytes(raw) != canonical_json_bytes(recomputed):
            raise IntegrityError("postseason source-to-feature replay differs")
        if raw.get("starter_provenance") != row["starter_provenance"] or raw.get("lineup_provenance") != row["lineup_provenance"]:
            raise IntegrityError("postseason participant feature provenance differs")
        scored_raw = score_raw_game(raw, card.normalization)
        for field in ("away_scores", "home_scores", "away_qualities", "home_qualities", "qualities"):
            for component in WEIGHTS:
                _close(traced[field][component], scored_raw[field][component], field + "." + component)
        _close(traced["edge"], scored_raw["edge"], "edge")
        probability = home_probability(scored_raw["edge"], card.alpha, card.beta)
        _close(row["p_home"], probability, "p_home replay")
        _close(row["data_completeness"], overall_completeness(scored_raw["qualities"]), "completeness replay")
        _close(row["starter_edge_abs"], abs(scored_raw["home_scores"]["starter"] - scored_raw["away_scores"]["starter"]), "starter replay")


def _reconstruct_history(report, card, payloads):
    """Replay authenticated raw inputs through the same feature boundary, no I/O."""
    from .live_history import (ResearchLiveHistoryBundle, _research_schedule, _parse_venue,
        _build_result_free_live_schedule_history)
    from .live_top3_contracts import SourceBinding
    from .prepare import derive_team_code_map, build_feature_history
    from .statcast import parse_statcast, StatcastData, derive_outs_recorded
    from .mlb_stats import parse_league_era
    from .source_parsers.pitching import parse_prior_career_bf
    target = date.fromisoformat(report["target_date"])
    cutoff = date.fromisoformat(report["history_cutoff_date"])
    bindings = report["source_bindings"]
    def matching(role):
        return [binding for binding in bindings if binding["binding_role"] == role]
    schedule_bindings = matching("HISTORY_SCHEDULE")
    if len(schedule_bindings) != 1:
        raise IntegrityError("postseason history schedule cardinality differs")
    schedule, coverage = _research_schedule(payloads[schedule_bindings[0]["binding_sha256"]], target)
    if coverage != report["cache_authority"]["coverage"]:
        raise IntegrityError("postseason official history coverage differs")
    pitches, seen, ranges = [], set(), []
    for binding in matching("HISTORY_STATCAST"):
        query = json.loads(binding["query_identity_json"])
        ranges.append((query["start"], query["end"]))
        parsed = parse_statcast(payloads[binding["binding_sha256"]], game_types=POSTSEASON_ML_RESEARCH_V1.recent_game_types)
        for pitch in parsed.pitches:
            identity = (pitch["game_pk"], pitch["at_bat_number"], pitch["pitch_number"])
            if identity in seen or not query["start"] <= pitch["game_date"] <= query["end"]:
                raise IntegrityError("postseason Statcast chunk coverage differs")
            seen.add(identity)
            pitches.append(pitch)
    ranges.sort()
    next_date = date.fromisoformat(coverage["start_date"])
    for first, last in ranges:
        if date.fromisoformat(first) != next_date or first > last:
            raise IntegrityError("postseason Statcast range gap/overlap")
        next_date = date.fromordinal(date.fromisoformat(last).toordinal() + 1)
    if next_date != date.fromordinal(cutoff.toordinal() + 1):
        raise IntegrityError("postseason Statcast cutoff coverage differs")
    by_pk = {game["game_pk"]: game for game in schedule}
    if {pitch["game_pk"] for pitch in pitches} != set(coverage["played_game_pks"]):
        raise IntegrityError("postseason full history pitch population differs")
    for pitch in pitches:
        game = by_pk[pitch["game_pk"]]
        if pitch["game_type"] != game["game_type"] or pitch["game_date"] != game["official_date"]:
            raise IntegrityError("postseason history pitch identity differs")
    raw_statcast = StatcastData(tuple(pitches), ())
    team_map = derive_team_code_map(schedule, raw_statcast, target.year).mapping
    venues = {}
    for binding in matching("HISTORY_VENUE"):
        identity = json.loads(binding["query_identity_json"])
        venue_id = identity["venue_id"]
        if venue_id in venues:
            raise IntegrityError("postseason venue population differs")
        venues[venue_id] = _parse_venue(payloads[binding["binding_sha256"]], venue_id)
    opening = date.fromisoformat(coverage["start_date"])
    schedule_build = _build_result_free_live_schedule_history(schedule, raw_statcast, team_map, venues, opening, cutoff,
        schedule_bindings[0]["manifest_sha256"], competition_policy=POSTSEASON_ML_RESEARCH_V1)
    final_outs = {row["game_pk"]: by_pk[row["game_pk"]].get("final_outs") for row in schedule_build.rows}
    final_halves = {pk: (by_pk[pk]["completed_innings"], by_pk[pk]["final_inning_topbot"]) for pk in final_outs
                    if type(by_pk[pk].get("completed_innings")) is int and by_pk[pk].get("final_inning_topbot") in {"Top", "Bot"}}
    statcast = derive_outs_recorded(raw_statcast, final_outs, final_half_by_game=final_halves)
    eras = matching("HISTORY_ERA")
    if len(eras) != 1:
        raise IntegrityError("postseason ERA cardinality differs")
    era = {cutoff.isoformat(): {"as_of_date": cutoff.isoformat(), **parse_league_era(payloads[eras[0]["binding_sha256"]]),
                              "source": "mlb_stats_api", "manifest_sha256": eras[0]["manifest_sha256"]}}
    history_bindings = tuple(SourceBinding(**binding) for binding in bindings if binding["binding_role"].startswith("HISTORY_"))
    history = ResearchLiveHistoryBundle(report["target_date"], cutoff.isoformat(), build_feature_history(statcast, card.slot_weights, schedule_build),
        era, team_map, venues, report["cache_authority"], history_bindings)
    careers = {}
    for binding in matching("CAREER_BF"):
        pk = json.loads(binding["query_identity_json"])["pitcher_id"]
        if pk in careers:
            raise IntegrityError("postseason career pitcher population differs")
        careers[pk] = parse_prior_career_bf(payloads[binding["binding_sha256"]], pk)
    return history, careers
