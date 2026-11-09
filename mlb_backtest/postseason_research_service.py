"""Complete-slate postseason research using the unchanged frozen scorer."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import hashlib
import json

from .competition_policy import POSTSEASON_ML_RESEARCH_V1
from .live_features import prepare_live_feature_history, build_live_raw_game_features
from .live_model_card import EXPECTED_MODEL_CARD_SHA256, FrozenLiveCard
from .live_sources import ACCEPTED_PREGAME_STATUSES, FinalGameScheduleDisposition, LiveSlateObservation, PregameParticipants, _LIVE_SCHEDULE_FIELDS
from .live_top3_contracts import ExecutionTimes, FinalGameObservation, IntegrityError, LiveHistoryBundle, MODEL_LABEL, SourceBinding, canonical_json_bytes
from .live_top3_service import (_boundary, _candidate_failure_reasons, _card_semantic_sha256,
    _EXPECTED_CARD_SEMANTIC_SHA256, _CANDIDATE_LOCAL_FEATURE_REASONS,
    _final_mismatch_reasons, _validate_final_projection, _participant_career_bindings,
    _relevant_slate_bindings, _unique_bindings, _binding_mapping, _parse_utc, _plain_json)
from .scoring import WEIGHTS, home_probability, overall_completeness, predicted_pick
from .scoring_prep import score_raw_game, validate_normalization
from .postseason_research_contracts import policy_envelope, validate_policy_query, validate_postseason_report, _consumed_binding_closure


def _validate_inputs(slate, history, card, careers, career_bindings, commands, times):
    from .live_history import ResearchLiveHistoryBundle
    if not isinstance(slate, LiveSlateObservation) or not isinstance(history, ResearchLiveHistoryBundle):
        raise IntegrityError("research slate/history differs")
    if slate.target_date != history.target_date or history.cutoff_date != date.fromordinal(date.fromisoformat(slate.target_date).toordinal() - 1).isoformat():
        raise IntegrityError("research history cutoff differs")
    if not isinstance(card, FrozenLiveCard) or card.file_sha256 != EXPECTED_MODEL_CARD_SHA256 or _card_semantic_sha256(card) != _EXPECTED_CARD_SEMANTIC_SHA256:
        raise IntegrityError("frozen live card differs")
    validate_normalization(card.normalization)
    if not isinstance(times, ExecutionTimes) or set(commands) != {"date", "model_card", "competition_scope"} or commands["date"] != slate.target_date or commands["competition_scope"] != "postseason-research-v1":
        raise IntegrityError("research command inputs differ")
    game_pks = tuple(sorted(game["game_pk"] for game in slate.games))
    if len(game_pks) != len(set(game_pks)):
        raise IntegrityError("research duplicate games")
    for game in slate.games:
        if set(game) != set(_LIVE_SCHEDULE_FIELDS) or type(game["game_pk"]) is not int or game["game_pk"] <= 0 or any(game.get(f"{side}_lineup") for side in ("away", "home")):
            raise IntegrityError("research initial game projection differs")
        _parse_utc(game["scheduled_start_utc"])
    bindings = (*slate.source_bindings, *history.source_bindings, *career_bindings)
    for binding in bindings:
        if not isinstance(binding, SourceBinding):
            raise IntegrityError("research source binding differs")
        SourceBinding(**_binding_mapping(binding))
        validate_policy_query(json.loads(binding.query_identity_json), binding.binding_role, slate.target_date)
    schedules = [b for b in slate.source_bindings if b.binding_role == "LIVE_SCHEDULE"]
    if len(schedules) != 1 or schedules[0].game_pks != game_pks:
        raise IntegrityError("research slate source population differs")
    participant_pks = [b.game_pks[0] for b in slate.source_bindings if b.binding_role == "LIVE_PARTICIPANTS" and len(b.game_pks) == 1]
    if len(participant_pks) != len(set(participant_pks)) or set(participant_pks) != set(slate.participants):
        raise IntegrityError("research participant source population differs")
    career_by_pitcher = {}
    for binding in career_bindings:
        identity = json.loads(binding.query_identity_json)
        pk = identity.get("pitcher_id")
        if binding.binding_role != "CAREER_BF" or type(pk) is not int or pk <= 0 or pk in career_by_pitcher:
            raise IntegrityError("research career source identity differs")
        career_by_pitcher[pk] = binding
    if set(career_by_pitcher) != set(careers) or any(type(value) is not int or value < 0 for value in careers.values()):
        raise IntegrityError("research career values differ")
    return career_by_pitcher, game_pks


def build_postseason_research_run(*, slate, final_schedule_observer, history, card,
                                career_bf_by_pitcher, career_bf_bindings, command_inputs,
                                execution_times, trace_sink=None):
    if not callable(final_schedule_observer) or trace_sink is not None and not isinstance(trace_sink, list):
        raise IntegrityError("research observer/trace sink differs")
    career_by_pitcher, game_pks = _validate_inputs(slate, history, card, career_bf_by_pitcher, career_bf_bindings, command_inputs, execution_times)
    all_bindings = [*slate.source_bindings, *history.source_bindings, *career_bf_bindings]
    prepared = prepare_live_feature_history(history, target_game_pks=game_pks, competition_policy=POSTSEASON_ML_RESEARCH_V1)
    rows = []
    for game in sorted(slate.games, key=lambda row: int(row["game_pk"])):
        pk = game["game_pk"]
        participant = slate.participants.get(pk)
        relevant = _relevant_slate_bindings(slate, pk) + list(history.source_bindings)
        relevant.extend(_participant_career_bindings(participant, career_by_pitcher))
        reasons = set(slate.initial_exclusion_reasons.get(pk, ()))
        if game["game_type"] not in POSTSEASON_ML_RESEARCH_V1.target_game_types:
            reasons.add("UNSUPPORTED_GAME_TYPE")
        if game["official_date"] != slate.target_date:
            reasons.add("OFFICIAL_DATE_CHANGED")
        if game["status"] not in ACCEPTED_PREGAME_STATUSES:
            reasons.add("PREGAME_STATUS_NOT_ACCEPTED")
        final = None
        if not reasons:
            final = final_schedule_observer(pk, slate.target_date)
            if isinstance(final, (FinalGameObservation, FinalGameScheduleDisposition)):
                if final.game_pk != pk or final.target_date != slate.target_date or final.source_binding.binding_role != "FINAL_SCHEDULE" or final.source_binding.game_pks != (pk,):
                    raise IntegrityError("research final identity differs")
                validate_policy_query(json.loads(final.source_binding.query_identity_json), "FINAL_SCHEDULE", slate.target_date)
                relevant.append(final.source_binding)
                all_bindings.append(final.source_binding)
                if isinstance(final, FinalGameScheduleDisposition):
                    reasons.update(final.reason_codes or ("FINAL_SCHEDULE_INVALID",))
                else:
                    _validate_final_projection(final)
                    reasons.update(_final_mismatch_reasons(game, final))
                    if game["game_type"] != final.projected_game["game_type"]:
                        reasons.add("GAME_TYPE_CHANGED")
            else:
                reasons.add("FINAL_SCHEDULE_INVALID")
        relevant = _unique_bindings(relevant)
        starters = () if not isinstance(participant, PregameParticipants) else (participant.away_starter_id, participant.home_starter_id)
        closure = _consumed_binding_closure([_binding_mapping(binding) for binding in all_bindings], pk, starters)
        if [binding.binding_sha256 for binding in relevant] != [binding["binding_sha256"] for binding in closure]:
            raise IntegrityError("research consumed evidence closure differs")
        boundary, boundary_time = _boundary(relevant)
        if boundary_time >= _parse_utc(game["scheduled_start_utc"]):
            reasons.add("OBSERVATION_AT_OR_AFTER_START")
        row = {"game_pk": pk, "game_type": game["game_type"], "target_date": slate.target_date,
               "away_team_id": game["away_team_id"], "home_team_id": game["home_team_id"],
               "away_team": game["away_team"], "home_team": game["home_team"],
               "scheduled_start_utc": game["scheduled_start_utc"], "state": "UNSCORED", "reason_codes": [],
               "source_binding_sha256s": sorted(b.binding_sha256 for b in relevant),
               "observation_boundary_utc": boundary, "model_card_sha256": None, "feature_sha256": None,
               "p_home": None, "predicted_side": None, "predicted_winner": None, "pick_probability": None,
               "data_completeness": None, "starter_edge_abs": None, "starter_provenance": None,
               "lineup_provenance": None, "schedule_quality_flags": []}
        rows.append(row)
        if reasons:
            row["state"] = "UNSCORED" if reasons == {"PROBABLE_PITCHER_CHANGED"} else "EXCLUDED"
        elif not isinstance(participant, PregameParticipants):
            reasons.add("MISSING_PREGAME_PARTICIPANTS")
        else:
            reasons.update(_candidate_failure_reasons(participant, game, history, career_bf_by_pitcher))
        if not reasons:
            roles = {b.binding_role for b in relevant}
            if not {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS", "FINAL_SCHEDULE", "HISTORY_SCHEDULE", "HISTORY_STATCAST", "HISTORY_ERA", "HISTORY_VENUE", "CAREER_BF"} <= roles or sum(b.binding_role == "CAREER_BF" for b in relevant) != 2:
                reasons.add("MISSING_SOURCE_BINDING")
            elif any(b.http_status != 200 for b in relevant):
                reasons.add("SOURCE_BINDING_INVALID")
        raw = None
        if not reasons:
            try:
                raw = build_live_raw_game_features(participant, final, history, career_bf_by_pitcher, prepared_history=prepared, competition_policy=POSTSEASON_ML_RESEARCH_V1)
            except ValueError as error:
                if len(error.args) != 1 or error.args[0] not in _CANDIDATE_LOCAL_FEATURE_REASONS:
                    raise
                reasons.add(error.args[0])
        if raw is None:
            row["reason_codes"] = sorted(reasons)
            continue
        scored = score_raw_game(raw, card.normalization)
        row.update(_scored_game_fields(game, raw, scored, card))
        if trace_sink is not None:
            trace_sink.append({k: row[k] for k in ("game_pk", "game_type", "target_date", "away_team_id", "home_team_id", "away_team", "home_team", "scheduled_start_utc", "state", "observation_boundary_utc", "source_binding_sha256s", "feature_sha256", "model_card_sha256", "p_home", "predicted_side", "pick_probability")} | {
                "raw_features": _plain_json(raw), **{key: _plain_json(scored[key]) for key in ("away_scores", "home_scores", "away_qualities", "home_qualities", "qualities")},
                "contributions": {name: WEIGHTS[name] * (scored["home_scores"][name] - scored["away_scores"][name]) for name in WEIGHTS},
                "total_edge": scored["edge"], "edge": scored["edge"]})
    if len(all_bindings) != len(_unique_bindings(all_bindings)) or len({b.observation_id for b in all_bindings}) != len(all_bindings):
        raise IntegrityError("research duplicate source bindings")
    payload = {"schema": "POSTSEASON_ML_RESEARCH_REPORT_V1", **policy_envelope(), "model_label": MODEL_LABEL,
               "target_date": slate.target_date, "command_inputs": dict(command_inputs),
               "executed_at_eastern": execution_times.executed_at_eastern, "executed_at_utc": execution_times.executed_at_utc,
               "history_cutoff_date": history.cutoff_date, "cache_authority": _plain_json(history.cache_authority),
               "model_card_sha256": card.file_sha256, "source_bindings": [_binding_mapping(b) for b in _unique_bindings(all_bindings)],
               "games": rows, "warnings": sorted(["RESEARCH_ONLY", "POSTSEASON_OUT_OF_DOMAIN", "NOT_POSTSEASON_VALIDATED", "CAREER_BF_REGULAR_SEASON", "LEAGUE_ERA_REGULAR_SEASON", "VERIFIED_MEANS_INPUT_READINESS_ONLY"]),
               "market_status": "NOT_EVALUATED", "market_reason": "POSTSEASON_NOT_VALIDATED",
               "totals_status": "NOT_EVALUATED", "totals_reason": "POSTSEASON_TOTALS_NOT_SUPPORTED"}
    payload["content_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return validate_postseason_report(payload)


def _scored_game_fields(game, raw, scored, card):
    """Pure frozen-score mapping, including the genuine exact-neutral branch."""
    probability = home_probability(scored["edge"], card.alpha, card.beta)
    pick = predicted_pick(game["home_team"], game["away_team"], probability)
    neutral = pick["predicted_side"] == "none"
    return {"state": "UNSCORED" if neutral else "VERIFIED",
            "reason_codes": ["EXACT_NEUTRAL_SCORE"] if neutral else ["PROBABLE_CONSISTENT"],
            "model_card_sha256": card.file_sha256, "feature_sha256": hashlib.sha256(canonical_json_bytes(raw)).hexdigest(),
            "p_home": probability, **pick, "predicted_winner": None if neutral else pick["predicted_winner"],
            "data_completeness": overall_completeness(scored["qualities"]),
            "starter_edge_abs": abs(scored["home_scores"]["starter"] - scored["away_scores"]["starter"]),
            "starter_provenance": raw["starter_provenance"], "lineup_provenance": raw["lineup_provenance"],
            "schedule_quality_flags": sorted({flag for side in ("away", "home") for flag in raw[side]["schedule"].get("flags", [])})}
