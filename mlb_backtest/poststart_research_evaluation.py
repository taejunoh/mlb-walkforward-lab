"""Closed, outcome-blind post-start reconstructed research evaluator.

This module deliberately has no collection or live-slate dependency.  It only
rehydrates sealed local snapshots and reuses the pure feature/scoring path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .feature_engine import FeatureHistory
from .live_features import (
    build_reconstructed_raw_game_features,
    prepare_live_feature_history,
)
from .live_model_card import load_frozen_live_card_bytes
from .live_sources import PregameParticipants
from .live_top3_contracts import (
    LiveHistoryBundle,
    SourceBinding,
    canonical_json_bytes,
)
from .scoring import home_probability, predicted_pick
from .scoring_prep import score_raw_game
from .secure_fs import (
    RegularFileSnapshot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)
from .statcast import StatcastData


REPORT_SCHEMA = "EXPERIMENTAL_POSTSTART_RESEARCH_V1"
TRACE_SCHEMA = "EXPERIMENTAL_POSTSTART_RESEARCH_TRACE_V1"
REPORT_ROOT = Path("reports/experimental_poststart_research")
STATUS_LABELS = [
    "POSTSTART_RECONSTRUCTED_RESEARCH",
    "NOT_PROSPECTIVE",
    "NOT_BET_VALIDATED",
]

_HISTORY_SCHEMA = "POSTSTART_RESEARCH_D1_HISTORY_V1"
_LINEUP_SOURCE_SCHEMA = "POSTSTART_RESEARCH_LINEUP_SOURCE_V1"
_SHA_LENGTH = 64
_PITCH_KEYS = frozenset(
    {
        "game_date", "game_pk", "pitcher_id", "batter_id", "events",
        "description", "p_throws", "home_team", "away_team", "inning",
        "inning_topbot", "release_speed", "woba_value", "woba_denom",
        "estimated_woba_using_speedangle", "launch_speed_angle", "at_bat_number",
        "pitch_number", "home_score", "away_score", "outs_when_up", "game_type",
        "event_id",
    }
)
_SCHEDULE_KEYS = frozenset(
    {"game_pk", "official_date", "team_codes", "home_team_code", "venue_name", "innings"}
)
_HISTORY_KEYS = frozenset(
    {
        "schema", "content_sha256", "target_date", "cutoff_date", "cache_authority",
        "source_bindings", "team_code_by_name", "venue_references", "league_era_by_asof",
        "feature_history",
    }
)
_FEATURE_HISTORY_KEYS = frozenset(
    {
        "slot_weights", "statcast_coverage_complete", "pitches", "plate_appearances",
        "schedule_rows", "schedule_coverage_start", "schedule_coverage_end",
        "schedule_coverage_complete",
    }
)
_PROJECTION_KEYS = frozenset(
    {
        "schema", "content_sha256", "target_date", "game_pk", "scheduled_start_utc",
        "away_team", "home_team", "away_team_id", "home_team_id", "venue_id", "venue_name",
        "game_type", "away_starting_nine", "home_starting_nine", "away_starter_id",
        "home_starter_id", "source_relative", "source_sha256", "reconstructed_at_utc",
    }
)
_CAREER_KEYS = frozenset(
    {"schema", "content_sha256", "target_date", "as_of_date", "pitcher_bf"}
)
_LINEUP_SOURCE_KEYS = frozenset(
    {
        "schema", "content_sha256", "target_date", "game_pk", "scheduled_start_utc",
        "away_team", "home_team", "away_team_id", "home_team_id", "away_starting_nine",
        "home_starting_nine", "away_starter_id", "home_starter_id", "source_retrieved_at_utc",
    }
)


@dataclass(frozen=True, slots=True)
class BuiltPoststartResearchEvaluation:
    root: Path
    report: Mapping[str, object]
    report_bytes: bytes
    markdown_bytes: bytes
    trace: Mapping[str, object]
    trace_bytes: bytes
    input_snapshots: tuple[RegularFileSnapshot, ...]


def _fail(code: str) -> None:
    raise ValueError(code)


def _canonical_object(raw: bytes, code: str) -> dict[str, object]:
    if type(raw) is not bytes:
        _fail(code)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail(code)
    if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
        _fail(code)
    return value


def _sha(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(code)
    return value


def _verify_content_digest(value: Mapping[str, object], code: str) -> None:
    digest = _sha(value.get("content_sha256"), code)
    unsigned = dict(value)
    unsigned.pop("content_sha256", None)
    if hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest() != digest:
        _fail(code)


def _canonical_day(value: object, code: str) -> str:
    if not isinstance(value, str):
        _fail(code)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail(code)
    if parsed.isoformat() != value:
        _fail(code)
    return value


def _canonical_utc(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(code)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(code)
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        _fail(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if type(value) is not int or value <= 0:
        _fail(code)
    return value


def _relative_path(value: object, code: str) -> Path:
    if not isinstance(value, str):
        _fail(code)
    path = Path(value)
    if path.is_absolute() or "\\" in value or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        _fail(code)
    return path


def _valid_nine(value: object, code: str) -> tuple[int, ...]:
    if not isinstance(value, list) or len(value) != 9:
        _fail(code)
    result = tuple(_positive_int(player_id, code) for player_id in value)
    if len(set(result)) != 9:
        _fail(code)
    return result


def _validate_rows(
    rows: object, *, keys: frozenset[str], date_key: str, cutoff: str, code: str
) -> list[dict[str, object]]:
    if not isinstance(rows, list):
        _fail(code)
    parsed: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != keys:
            _fail(code)
        _canonical_day(row.get(date_key), code)
        if str(row[date_key]) > cutoff or _positive_int(row.get("game_pk"), code) <= 0:
            _fail(code)
        parsed.append(dict(row))
    return parsed


def _derived_outs(events: object) -> int:
    """Derive the FeatureHistory-only out count from an admitted historical event."""
    if events is None:
        return 0
    if not isinstance(events, str):
        _fail("INVALID_D1_HISTORY")
    one_out = {
        "field_out", "force_out", "sac_bunt", "sac_fly", "strikeout",
        "fielders_choice_out", "pickoff_1b", "pickoff_2b", "pickoff_3b",
        "caught_stealing_2b", "caught_stealing_3b", "caught_stealing_home",
    }
    if events in one_out:
        return 1
    if events in {"double_play", "grounded_into_double_play", "strikeout_double_play"}:
        return 2
    if events == "triple_play":
        return 3
    return 0


def _load_research_d1_history(raw: bytes) -> LiveHistoryBundle:
    """Rehydrate exactly the small D-1 feature authority accepted by research."""
    value = _canonical_object(raw, "INVALID_D1_HISTORY")
    if set(value) != _HISTORY_KEYS or value.get("schema") != _HISTORY_SCHEMA:
        _fail("INVALID_D1_HISTORY")
    _verify_content_digest(value, "INVALID_D1_HISTORY")
    target_date = _canonical_day(value["target_date"], "INVALID_D1_HISTORY")
    cutoff_date = _canonical_day(value["cutoff_date"], "INVALID_D1_HISTORY")
    if date.fromisoformat(cutoff_date).toordinal() != date.fromisoformat(target_date).toordinal() - 1:
        _fail("INVALID_D1_HISTORY")
    feature = value["feature_history"]
    if not isinstance(feature, dict) or set(feature) != _FEATURE_HISTORY_KEYS:
        _fail("INVALID_D1_HISTORY")
    if feature.get("statcast_coverage_complete") is not True or feature.get("schedule_coverage_complete") is not True:
        _fail("INCOMPLETE_D1_HISTORY")
    if _canonical_day(feature.get("schedule_coverage_start"), "INVALID_D1_HISTORY") > cutoff_date:
        _fail("INVALID_D1_HISTORY")
    if _canonical_day(feature.get("schedule_coverage_end"), "INVALID_D1_HISTORY") != cutoff_date:
        _fail("INCOMPLETE_D1_HISTORY")
    raw_weights = feature.get("slot_weights")
    if not isinstance(raw_weights, dict) or set(raw_weights) != {str(slot) for slot in range(1, 10)}:
        _fail("INVALID_D1_HISTORY")
    slot_weights: dict[int, float] = {}
    for slot, weight in raw_weights.items():
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            _fail("INVALID_D1_HISTORY")
        slot_weights[int(slot)] = float(weight)
    pitches = _validate_rows(feature.get("pitches"), keys=_PITCH_KEYS, date_key="game_date", cutoff=cutoff_date, code="AS_OF_VIOLATION")
    pitch_identities = [
        (row["game_date"], row["game_pk"], row["at_bat_number"], row["pitch_number"], row["event_id"])
        for row in pitches
    ]
    if pitch_identities != sorted(pitch_identities) or len(set(pitch_identities)) != len(pitch_identities):
        _fail("INVALID_D1_HISTORY")
    for row in pitches:
        for key in ("pitcher_id", "batter_id", "inning", "at_bat_number", "pitch_number", "outs_when_up"):
            if type(row[key]) is not int or row[key] < (0 if key == "outs_when_up" else 1):
                _fail("INVALID_D1_HISTORY")
        if not isinstance(row["event_id"], str) or not row["event_id"]:
            _fail("INVALID_D1_HISTORY")
    plate_appearances = _validate_rows(feature.get("plate_appearances"), keys=_PITCH_KEYS, date_key="game_date", cutoff=cutoff_date, code="AS_OF_VIOLATION")
    pitch_by_identity = {canonical_json_bytes(row): row for row in pitches}
    event_max_pitch: dict[str, int] = {}
    for row in pitches:
        event_max_pitch[row["event_id"]] = max(event_max_pitch.get(row["event_id"], 0), int(row["pitch_number"]))
    if len({canonical_json_bytes(row) for row in plate_appearances}) != len(plate_appearances):
        _fail("INVALID_D1_HISTORY")
    for row in plate_appearances:
        exact = canonical_json_bytes(row)
        pitch = pitch_by_identity.get(exact)
        if pitch is None or not pitch.get("events") or int(row["pitch_number"]) != event_max_pitch[row["event_id"]]:
            _fail("INVALID_D1_HISTORY")
    schedule_rows = _validate_rows(feature.get("schedule_rows"), keys=_SCHEDULE_KEYS, date_key="official_date", cutoff=cutoff_date, code="AS_OF_VIOLATION")
    for row in schedule_rows:
        if (
            not isinstance(row["team_codes"], list)
            or len(row["team_codes"]) != 2
            or any(not isinstance(code, str) or not code for code in row["team_codes"])
            or not isinstance(row["home_team_code"], str)
            or row["home_team_code"] not in row["team_codes"]
            or not isinstance(row["venue_name"], str)
            or not row["venue_name"]
            or type(row["innings"]) is not int
            or row["innings"] <= 0
        ):
            _fail("INVALID_D1_HISTORY")
        row["team_codes"] = tuple(row["team_codes"])
    raw_venues = value["venue_references"]
    if not isinstance(raw_venues, dict) or not raw_venues:
        _fail("INVALID_D1_HISTORY")
    venues: dict[int, Mapping[str, object]] = {}
    venue_keys = {"venue_name", "venue_city", "venue_latitude", "venue_longitude", "time_zone"}
    for raw_id, ref in raw_venues.items():
        if not isinstance(raw_id, str) or not raw_id.isdigit() or str(int(raw_id)) != raw_id or not isinstance(ref, dict) or set(ref) != venue_keys:
            _fail("INVALID_D1_HISTORY")
        venue_id = _positive_int(int(raw_id), "INVALID_D1_HISTORY")
        if venue_id in venues:
            _fail("INVALID_D1_HISTORY")
        venues[venue_id] = dict(ref)
    bindings_value = value["source_bindings"]
    if not isinstance(bindings_value, list) or len(bindings_value) != 4:
        _fail("INVALID_D1_HISTORY")
    try:
        bindings = tuple(SourceBinding(**item) for item in bindings_value)
    except (TypeError, ValueError):
        _fail("INVALID_D1_HISTORY")
    required_roles = {"HISTORY_SCHEDULE", "HISTORY_STATCAST", "HISTORY_ERA", "HISTORY_VENUE"}
    if {binding.binding_role for binding in bindings} != required_roles or len({binding.binding_role for binding in bindings}) != 4:
        _fail("INVALID_D1_HISTORY")
    for binding in bindings:
        try:
            identity = json.loads(binding.query_identity_json)
        except (TypeError, json.JSONDecodeError):
            _fail("INVALID_D1_HISTORY")
        if not isinstance(identity, dict) or identity.get("as_of_date") != cutoff_date:
            _fail("AS_OF_VIOLATION")
    teams = value["team_code_by_name"]
    era = value["league_era_by_asof"]
    if not isinstance(teams, dict) or not isinstance(era, dict):
        _fail("INVALID_D1_HISTORY")
    try:
        # FeatureHistory needs an internal out-count index.  It is deterministically
        # derived from the admitted historical event, never read from an extra source field.
        feature_pitches = [dict(row, outs_recorded=_derived_outs(row["events"])) for row in pitches]
        feature_pas = [dict(row, outs_recorded=_derived_outs(row["events"])) for row in plate_appearances]
        history = FeatureHistory(
            StatcastData(feature_pitches, feature_pas),
            slot_weights=slot_weights,
            schedule_history=schedule_rows,
            schedule_coverage_start=feature["schedule_coverage_start"],
            schedule_coverage_end=feature["schedule_coverage_end"],
            schedule_coverage_complete=True,
            venue_by_name={
                ref["venue_name"]: {
                    "city": ref["venue_city"], "latitude": ref["venue_latitude"],
                    "longitude": ref["venue_longitude"], "time_zone": ref["time_zone"],
                }
                for ref in venues.values()
            },
            statcast_coverage_complete=True,
        )
        return LiveHistoryBundle(target_date, cutoff_date, history, era, teams, venues, value["cache_authority"], bindings)
    except (TypeError, ValueError):
        _fail("INVALID_D1_HISTORY")


def _load_projection(
    snapshot: RegularFileSnapshot, trusted,
) -> tuple[dict[str, object], RegularFileSnapshot]:
    value = _canonical_object(snapshot.contents, "INVALID_PARTICIPANT_PROJECTION")
    if set(value) != _PROJECTION_KEYS:
        _fail("INVALID_PARTICIPANT_PROJECTION")
    _verify_content_digest(value, "INVALID_PARTICIPANT_PROJECTION")
    _canonical_day(value["target_date"], "INVALID_PARTICIPANT_PROJECTION")
    _positive_int(value["game_pk"], "INVALID_PARTICIPANT_PROJECTION")
    _canonical_utc(value["scheduled_start_utc"], "INVALID_PARTICIPANT_PROJECTION")
    _canonical_utc(value["reconstructed_at_utc"], "INVALID_PARTICIPANT_PROJECTION")
    for key in ("away_team", "home_team", "venue_name"):
        if not isinstance(value[key], str) or not value[key]:
            _fail("INVALID_PARTICIPANT_PROJECTION")
    if value["away_team"] == value["home_team"]:
        _fail("INVALID_PARTICIPANT_PROJECTION")
    for key in ("away_team_id", "home_team_id", "venue_id", "away_starter_id", "home_starter_id"):
        _positive_int(value[key], "INVALID_PARTICIPANT_PROJECTION")
    if value["away_team_id"] == value["home_team_id"] or value["game_type"] != "R":
        _fail("INVALID_PARTICIPANT_PROJECTION")
    _valid_nine(value["away_starting_nine"], "INVALID_PARTICIPANT_PROJECTION")
    _valid_nine(value["home_starting_nine"], "INVALID_PARTICIPANT_PROJECTION")
    source_relative = _relative_path(value["source_relative"], "INVALID_PARTICIPANT_PROJECTION")
    source_sha = _sha(value["source_sha256"], "INVALID_PARTICIPANT_PROJECTION")
    source_snapshot = read_regular_snapshot(trusted, source_relative, "post-start lineup source")
    if source_snapshot is None or hashlib.sha256(source_snapshot.contents).hexdigest() != source_sha:
        _fail("INVALID_PARTICIPANT_PROJECTION")
    source = _canonical_object(source_snapshot.contents, "INVALID_PARTICIPANT_PROJECTION")
    if set(source) != _LINEUP_SOURCE_KEYS or source.get("schema") != _LINEUP_SOURCE_SCHEMA:
        _fail("INVALID_PARTICIPANT_PROJECTION")
    _verify_content_digest(source, "INVALID_PARTICIPANT_PROJECTION")
    _canonical_utc(source["source_retrieved_at_utc"], "INVALID_PARTICIPANT_PROJECTION")
    for key in _LINEUP_SOURCE_KEYS - {"schema", "content_sha256", "source_retrieved_at_utc"}:
        if source.get(key) != value.get(key):
            _fail("INVALID_PARTICIPANT_PROJECTION")
    return value, source_snapshot


def _load_career(raw: bytes, projection: Mapping[str, object], history: LiveHistoryBundle) -> dict[int, int]:
    value = _canonical_object(raw, "INVALID_CAREER_BF")
    if set(value) != _CAREER_KEYS:
        _fail("INVALID_CAREER_BF")
    _verify_content_digest(value, "INVALID_CAREER_BF")
    if _canonical_day(value["target_date"], "INVALID_CAREER_BF") != projection["target_date"]:
        _fail("INVALID_CAREER_BF")
    if _canonical_day(value["as_of_date"], "INVALID_CAREER_BF") > history.cutoff_date:
        _fail("AS_OF_VIOLATION")
    raw_bf = value["pitcher_bf"]
    if not isinstance(raw_bf, dict):
        _fail("INVALID_CAREER_BF")
    converted: dict[int, int] = {}
    for raw_id, bf in raw_bf.items():
        if not isinstance(raw_id, str) or not raw_id.isdigit() or str(int(raw_id)) != raw_id or type(bf) is not int or bf < 0:
            _fail("INVALID_CAREER_BF")
        converted[int(raw_id)] = bf
    starters = {int(projection["away_starter_id"]), int(projection["home_starter_id"])}
    if set(converted) != starters:
        _fail("INVALID_CAREER_BF")
    return converted


def _load_card(raw: bytes, target_date: str):
    card = load_frozen_live_card_bytes(raw)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("INVALID_MODEL_CARD")
    window = payload.get("training_window") if isinstance(payload, dict) else None
    expected = {"configured_start", "configured_end", "actual_feature_min_date", "actual_calibration_min_date", "actual_max_date"}
    if not isinstance(window, dict) or set(window) != expected:
        _fail("INVALID_MODEL_CARD")
    actual_max = _canonical_day(window["actual_max_date"], "INVALID_MODEL_CARD")
    for key in expected - {"actual_max_date"}:
        _canonical_day(window[key], "INVALID_MODEL_CARD")
    if actual_max >= target_date:
        _fail("MODEL_TRAINING_AFTER_TARGET")
    return card


def _snapshot_ref(snapshot: RegularFileSnapshot) -> dict[str, str]:
    return {
        "relative": snapshot.relative.as_posix(),
        "sha256": hashlib.sha256(snapshot.contents).hexdigest(),
    }


def build_poststart_research_evaluation(
    root: Path,
    *,
    history_bundle: Path,
    participant_projection: Path,
    model_card: Path,
    career_bf: Path,
    evaluated_at_utc: str,
) -> BuiltPoststartResearchEvaluation:
    """Build canonical research output from four explicit local authority files."""
    evaluated_at = _canonical_utc(evaluated_at_utc, "INVALID_EVALUATED_AT_UTC")
    relative_inputs = tuple(
        _relative_path(path.as_posix() if isinstance(path, Path) else path, "INVALID_INPUT_PATH")
        for path in (history_bundle, participant_projection, model_card, career_bf)
    )
    if len(set(relative_inputs)) != 4:
        _fail("INVALID_INPUT_PATH")
    root_path = Path(root).absolute()
    with open_trusted_root(root_path) as trusted:
        snapshots = tuple(
            read_regular_snapshot(trusted, relative, "post-start research input")
            for relative in relative_inputs
        )
        if any(snapshot is None for snapshot in snapshots):
            _fail("INVALID_INPUT_PATH")
        history_snapshot, projection_snapshot, card_snapshot, career_snapshot = snapshots
        assert history_snapshot and projection_snapshot and card_snapshot and career_snapshot
        history = _load_research_d1_history(history_snapshot.contents)
        projection, source_snapshot = _load_projection(projection_snapshot, trusted)
        if history.target_date != projection["target_date"]:
            _fail("TARGET_DATE_MISMATCH")
        career = _load_career(career_snapshot.contents, projection, history)
        card = _load_card(card_snapshot.contents, str(projection["target_date"]))
        participants = PregameParticipants(
            game_pk=int(projection["game_pk"]), away_team_id=int(projection["away_team_id"]),
            home_team_id=int(projection["home_team_id"]),
            away_pitcher_list_id=int(projection["away_starter_id"]),
            home_pitcher_list_id=int(projection["home_starter_id"]),
            away_lineup=_valid_nine(projection["away_starting_nine"], "INVALID_PARTICIPANT_PROJECTION"),
            home_lineup=_valid_nine(projection["home_starting_nine"], "INVALID_PARTICIPANT_PROJECTION"),
            complete=True, reason_codes=("PROBABLE_CONSISTENT",),
        )
        game = {
            "game_pk": projection["game_pk"], "official_date": projection["target_date"],
            "game_type": projection["game_type"], "scheduled_start_utc": projection["scheduled_start_utc"],
            "away_team": projection["away_team"], "home_team": projection["home_team"],
            "away_team_id": projection["away_team_id"], "home_team_id": projection["home_team_id"],
            "venue_id": projection["venue_id"], "venue_name": projection["venue_name"],
            "away_probable_pitcher_id": projection["away_starter_id"],
            "home_probable_pitcher_id": projection["home_starter_id"], "away_lineup": [], "home_lineup": [],
        }
        prepared = prepare_live_feature_history(history, target_game_pks=(int(projection["game_pk"]),))
        raw = build_reconstructed_raw_game_features(participants, game, history, career, prepared_history=prepared)
        scored = score_raw_game(raw, card.normalization)
        p_home = home_probability(float(scored["edge"]), card.alpha, card.beta)
        pick = predicted_pick(str(projection["home_team"]), str(projection["away_team"]), p_home)
        all_snapshots = (history_snapshot, projection_snapshot, card_snapshot, career_snapshot, source_snapshot)
    input_refs = {
        "history_bundle": _snapshot_ref(history_snapshot),
        "participant_projection": _snapshot_ref(projection_snapshot),
        "model_card": _snapshot_ref(card_snapshot),
        "career_bf": _snapshot_ref(career_snapshot),
        "lineup_source": _snapshot_ref(source_snapshot),
    }
    feature_sha = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    report: dict[str, object] = {
        "schema": REPORT_SCHEMA, "target_date": projection["target_date"], "game_pk": projection["game_pk"],
        "scheduled_start_utc": projection["scheduled_start_utc"], "evaluated_at_utc": evaluated_at,
        "reconstructed_at_utc": projection["reconstructed_at_utc"], "status_labels": list(STATUS_LABELS),
        "lineup_provenance": "RECONSTRUCTED_FINAL_LINEUP", "starter_provenance": "RECONSTRUCTED_FINAL_STARTER",
        "limitations": ["RECONSTRUCTED_FINAL_LINEUP", "MODEL_ARTIFACT_TIME_UNPROVEN"],
        "input_refs": input_refs, "feature_sha256": feature_sha, "p_home": float(p_home),
        "predicted_side": pick["predicted_side"], "pick_probability": float(pick["pick_probability"]),
        "component_scores": {"away": scored["away_scores"], "home": scored["home_scores"]},
        "qualities": scored["qualities"],
    }
    report["content_sha256"] = hashlib.sha256(canonical_json_bytes(report)).hexdigest()
    report_bytes = canonical_json_bytes(report)
    artifact_digest = hashlib.sha256(report_bytes).hexdigest()
    report_relative = REPORT_ROOT / str(projection["target_date"]) / f"{artifact_digest}.json"
    trace: dict[str, object] = {
        "schema": TRACE_SCHEMA, "report_relative": report_relative.as_posix(), "report_sha256": artifact_digest,
        "input_refs": input_refs, "status_labels": list(STATUS_LABELS),
    }
    trace["content_sha256"] = hashlib.sha256(canonical_json_bytes(trace)).hexdigest()
    markdown = (
        f"# Post-start reconstructed research\n\n"
        f"- Target date: {projection['target_date']}\n"
        f"- Game: {projection['game_pk']}\n"
        f"- Status: {', '.join(STATUS_LABELS)}\n"
        f"- Report SHA-256: {artifact_digest}\n"
    ).encode("utf-8")
    return BuiltPoststartResearchEvaluation(root_path, report, report_bytes, markdown, trace, canonical_json_bytes(trace), all_snapshots)


def persist_poststart_research_evaluation(
    root: Path, built: BuiltPoststartResearchEvaluation
) -> tuple[Path, Path, Path]:
    """Append only the three isolated research artifacts after rebinding inputs."""
    if not isinstance(built, BuiltPoststartResearchEvaluation) or Path(root).absolute() != built.root:
        _fail("INVALID_BUILT_RESEARCH_EVALUATION")
    digest = hashlib.sha256(built.report_bytes).hexdigest()
    target_date = built.report.get("target_date")
    target = _canonical_day(target_date, "INVALID_BUILT_RESEARCH_EVALUATION")
    base = REPORT_ROOT / target / digest
    report_relative = base.with_suffix(".json")
    markdown_relative = base.with_suffix(".md")
    trace_relative = base.with_suffix(".trace.json")
    if built.trace.get("report_relative") != report_relative.as_posix():
        _fail("INVALID_BUILT_RESEARCH_EVALUATION")
    def rebind() -> None:
        for snapshot in built.input_snapshots:
            rebind_regular_snapshot(trusted, snapshot, "post-start research input")
    with open_trusted_root(built.root) as trusted:
        rebind()
        write_exclusive(trusted, report_relative, built.report_bytes, "post-start research report", final_rebind=rebind)
        rebind()
        write_exclusive(trusted, markdown_relative, built.markdown_bytes, "post-start research markdown", final_rebind=rebind)
        rebind()
        write_exclusive(trusted, trace_relative, built.trace_bytes, "post-start research trace", final_rebind=rebind)
    return tuple(built.root / relative for relative in (report_relative, markdown_relative, trace_relative))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build post-start reconstructed research only.")
    commands = parser.add_subparsers(dest="command", required=True)
    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--root", required=True)
    evaluate.add_argument("--history-bundle", required=True)
    evaluate.add_argument("--participant-projection", required=True)
    evaluate.add_argument("--model-card", required=True)
    evaluate.add_argument("--career-bf", required=True)
    args = parser.parse_args(argv)
    built = build_poststart_research_evaluation(
        Path(args.root), history_bundle=Path(args.history_bundle),
        participant_projection=Path(args.participant_projection), model_card=Path(args.model_card),
        career_bf=Path(args.career_bf),
        evaluated_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )
    for path in persist_poststart_research_evaluation(Path(args.root), built):
        print(path.relative_to(Path(args.root).absolute()).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
