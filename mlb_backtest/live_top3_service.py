"""Classify, score, and rank one experimental live Daily Top 3 slate."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import date, datetime, timezone

from .live_features import build_live_raw_game_features, _validate_venue_references
from .live_model_card import (
    EXPECTED_MODEL_CARD_SHA256,
    FrozenLiveCard,
)
from .live_sources import (
    ACCEPTED_PREGAME_STATUSES,
    FinalGameScheduleDisposition,
    LiveSlateObservation,
    PregameParticipants,
    _LIVE_SCHEDULE_FIELDS,
)
from .live_top3_contracts import (
    MODEL_LABEL,
    ExecutionTimes,
    FinalGameObservation,
    IntegrityError,
    LiveHistoryBundle,
    SourceBinding,
    canonical_json_bytes,
    validate_run_payload,
)
from .scoring import (
    WEIGHTS,
    component_agreement,
    confidence_score,
    home_probability,
    overall_completeness,
    predicted_pick,
    tier,
    uncertainty,
)
from .scoring_prep import score_raw_game, validate_normalization


_EXPECTED_CARD_SEMANTIC_SHA256 = (
    "136a7997cb7506301d5ea80f290fe46b409854d6c476118b6ceb13e0ea8c358a"
)
_CANDIDATE_LOCAL_FEATURE_REASONS = frozenset(
    {
        "INCOMPLETE_PREGAME_PARTICIPANTS",
        "INVALID_AWAY_LINEUP",
        "INVALID_CAREER_BF",
        "INVALID_HOME_LINEUP",
        "INVALID_VENUE_REFERENCE",
        "MISSING_AWAY_LINEUP",
        "MISSING_AWAY_PITCHER_LIST",
        "MISSING_AWAY_STARTER",
        "MISSING_HOME_LINEUP",
        "MISSING_HOME_PITCHER_LIST",
        "MISSING_HOME_STARTER",
        "MISSING_SCHEDULE_PROBABLE",
        "MISSING_TARGET_VENUE_EVIDENCE",
        "POSTSTART_APPEARANCE_ORDER",
        "STARTER_CONFLICT",
        "VENUE_IDENTITY_MISMATCH",
    }
)


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _card_semantic_sha256(card: FrozenLiveCard) -> str:
    semantics = {
        "model_version": card.model_version,
        "normalization": _plain_json(card.normalization),
        "slot_weights": _plain_json(card.slot_weights),
        "component_weights": _plain_json(card.component_weights),
        "alpha": card.alpha,
        "beta": card.beta,
    }
    return hashlib.sha256(canonical_json_bytes(semantics)).hexdigest()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError) as exc:
        raise ValueError("scheduled start must be canonical UTC") from exc
    if (
        not isinstance(value, str)
        or not value.endswith("Z")
        or parsed.tzinfo is None
        or parsed.utcoffset() != timezone.utc.utcoffset(parsed)
    ):
        raise ValueError("scheduled start must be canonical UTC")
    return parsed


def _binding_identity(binding: SourceBinding) -> Mapping[str, object]:
    if not isinstance(binding, SourceBinding):
        raise IntegrityError("live source binding differs")
    try:
        SourceBinding(**asdict(binding))
        value = json.loads(binding.query_identity_json)
        canonical = (
            canonical_json_bytes(value).decode("utf-8").removesuffix("\n")
        )
    except IntegrityError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IntegrityError("live source binding identity differs") from exc
    if (
        not isinstance(value, Mapping)
        or binding.query_identity_json != canonical
    ):
        raise IntegrityError("live source binding identity differs")
    return value


def _validate_live_binding(
    binding: SourceBinding,
    *,
    role: str,
    target_date: str,
    slate_game_pks: tuple[int, ...],
    game_pk: int | None = None,
) -> None:
    identity = _binding_identity(binding)
    if binding.source != "stats_api" or binding.binding_role != role:
        raise IntegrityError("live source binding role or source differs")
    expected_keys = {"url", "observation_id", "target_date"}
    if role == "LIVE_SCHEDULE":
        expected_scope = slate_game_pks
    else:
        if type(game_pk) is not int or game_pk <= 0:
            raise IntegrityError("live source binding game scope differs")
        expected_keys.add("game_pk")
        expected_scope = (game_pk,)
    if (
        set(identity) != expected_keys
        or identity.get("url") != binding.requested_url
        or identity.get("observation_id") != binding.observation_id
        or identity.get("target_date") != target_date
        or binding.game_pks != expected_scope
        or (role != "LIVE_SCHEDULE" and identity.get("game_pk") != game_pk)
    ):
        raise IntegrityError("live source binding identity or scope differs")


def _validate_slate_live_bindings(
    slate: LiveSlateObservation, slate_game_pks: tuple[int, ...]
) -> None:
    bindings = slate.source_bindings
    if not isinstance(bindings, tuple) or any(
        not isinstance(binding, SourceBinding) for binding in bindings
    ):
        raise IntegrityError("live slate source bindings differ")
    if len({binding.binding_sha256 for binding in bindings}) != len(bindings):
        raise IntegrityError("duplicate source binding digest")
    if len({binding.observation_id for binding in bindings}) != len(bindings):
        raise IntegrityError("duplicate source binding observation id")
    schedules = 0
    participant_counts: dict[int, int] = {}
    for binding in bindings:
        if binding.binding_role == "LIVE_SCHEDULE":
            schedules += 1
            _validate_live_binding(
                binding,
                role="LIVE_SCHEDULE",
                target_date=slate.target_date,
                slate_game_pks=slate_game_pks,
            )
        elif binding.binding_role == "LIVE_PARTICIPANTS":
            if len(binding.game_pks) != 1 or binding.game_pks[0] not in slate_game_pks:
                raise IntegrityError("live participant binding scope differs")
            _validate_live_binding(
                binding,
                role="LIVE_PARTICIPANTS",
                target_date=slate.target_date,
                slate_game_pks=slate_game_pks,
                game_pk=binding.game_pks[0],
            )
            participant_counts[binding.game_pks[0]] = (
                participant_counts.get(binding.game_pks[0], 0) + 1
            )
        else:
            raise IntegrityError("live slate binding role differs")
    if schedules != 1:
        raise IntegrityError("live slate must contain exactly one schedule binding")
    if set(participant_counts) != set(slate.participants) or any(
        count != 1 for count in participant_counts.values()
    ):
        raise IntegrityError("live participant binding cardinality differs")


def _validate_inputs(
    slate: LiveSlateObservation,
    history: LiveHistoryBundle,
    card: FrozenLiveCard,
    career_bf_by_pitcher: Mapping[int, int],
    career_bf_bindings: tuple[SourceBinding, ...],
    command_inputs: Mapping[str, object],
    execution_times: ExecutionTimes,
) -> dict[int, SourceBinding]:
    if not isinstance(slate, LiveSlateObservation) or not slate.games:
        raise ValueError("live slate must contain scheduled games")
    if not isinstance(history, LiveHistoryBundle):
        raise ValueError("history must be a LiveHistoryBundle")
    if slate.target_date != history.target_date:
        raise ValueError("slate and history target dates differ")
    target = date.fromisoformat(slate.target_date)
    if history.cutoff_date != date.fromordinal(target.toordinal() - 1).isoformat():
        raise ValueError("history cutoff differs from target date")
    if (
        not isinstance(card, FrozenLiveCard)
        or card.model_version != "v5.1-BT"
        or card.file_sha256 != EXPECTED_MODEL_CARD_SHA256
        or dict(card.component_weights) != dict(WEIGHTS)
        or isinstance(card.alpha, bool)
        or not isinstance(card.alpha, (int, float))
        or not math.isfinite(float(card.alpha))
        or isinstance(card.beta, bool)
        or not isinstance(card.beta, (int, float))
        or not math.isfinite(float(card.beta))
        or card.beta <= 0
        or _card_semantic_sha256(card) != _EXPECTED_CARD_SEMANTIC_SHA256
    ):
        raise ValueError("frozen live card differs")
    validate_normalization(card.normalization)
    if (
        not isinstance(command_inputs, Mapping)
        or set(command_inputs) != {"date", "model_card"}
        or command_inputs.get("date") != slate.target_date
    ):
        raise ValueError("command inputs differ")
    if not isinstance(execution_times, ExecutionTimes):
        raise ValueError("execution_times must be ExecutionTimes")
    if (
        not isinstance(career_bf_by_pitcher, Mapping)
        or any(
            type(pitcher_id) is not int
            or pitcher_id <= 0
            or type(value) is not int
            or value < 0
            for pitcher_id, value in career_bf_by_pitcher.items()
        )
        or not isinstance(career_bf_bindings, tuple)
        or any(
            not isinstance(binding, SourceBinding)
            or binding.binding_role != "CAREER_BF"
            for binding in career_bf_bindings
        )
    ):
        raise ValueError("career BF inputs differ")
    career_by_pitcher: dict[int, SourceBinding] = {}
    for binding in career_bf_bindings:
        identity = _binding_identity(binding)
        pitcher_id = identity.get("pitcher_id")
        if (
            type(pitcher_id) is not int
            or pitcher_id <= 0
            or identity.get("as_of_date") != history.cutoff_date
            or pitcher_id in career_by_pitcher
        ):
            raise ValueError("career BF binding identity differs")
        career_by_pitcher[pitcher_id] = binding
    if set(career_by_pitcher) != set(career_bf_by_pitcher):
        raise ValueError("career BF values and bindings differ")
    seen: set[int] = set()
    for game in slate.games:
        if not isinstance(game, Mapping):
            raise ValueError("live slate games must be mappings")
        if set(game) != set(_LIVE_SCHEDULE_FIELDS):
            raise IntegrityError("INVALID_INITIAL_GAME_PROJECTION")
        if any(game.get(f"{side}_lineup") for side in ("away", "home")):
            raise IntegrityError("TARGET_PARTICIPANT_LEAKAGE")
        game_pk = game.get("game_pk")
        if type(game_pk) is not int or game_pk <= 0 or game_pk in seen:
            raise ValueError("live slate game_pk differs")
        seen.add(game_pk)
        _parse_utc(game.get("scheduled_start_utc"))
        for field in ("away_team", "home_team"):
            if not isinstance(game.get(field), str) or not game[field]:
                raise ValueError("live slate team identity differs")
    _validate_slate_live_bindings(slate, tuple(sorted(seen)))
    return career_by_pitcher


def _final_mismatch_reasons(
    saved: Mapping[str, object], current: FinalGameObservation
) -> set[str]:
    projected = current.projected_game
    reasons: set[str] = set()
    if current.target_date != saved.get("official_date"):
        reasons.add("OFFICIAL_DATE_CHANGED")
    if any(
        saved.get(field) != projected.get(field)
        for field in (
            "game_pk",
            "away_team_id",
            "home_team_id",
            "away_team",
            "home_team",
        )
    ):
        reasons.add("GAME_IDENTITY_CHANGED")
    if projected.get("official_date") != saved.get("official_date"):
        reasons.add("OFFICIAL_DATE_CHANGED")
    if any(
        saved.get(field) != projected.get(field)
        for field in ("venue_id", "venue_name")
    ):
        reasons.add("VENUE_CHANGED")
    if projected.get("scheduled_start_utc") != saved.get("scheduled_start_utc"):
        reasons.add("SCHEDULED_START_CHANGED")
    if projected.get("status") not in ACCEPTED_PREGAME_STATUSES:
        reasons.add("PREGAME_STATUS_NOT_ACCEPTED")
    if any(
        saved.get(field) != projected.get(field)
        for field in (
            "away_probable_pitcher_id",
            "away_probable_pitcher_name",
            "home_probable_pitcher_id",
            "home_probable_pitcher_name",
        )
    ):
        reasons.add("PROBABLE_PITCHER_CHANGED")
    return reasons


def _validate_final_projection(final: FinalGameObservation) -> None:
    FinalGameObservation(
        final.game_pk,
        final.target_date,
        final.projected_game,
        final.source_binding,
    )
    if set(final.projected_game) != set(_LIVE_SCHEDULE_FIELDS):
        raise IntegrityError("INVALID_FINAL_GAME_PROJECTION")
    if any(
        final.projected_game.get(f"{side}_lineup")
        for side in ("away", "home")
    ):
        raise IntegrityError("TARGET_PARTICIPANT_LEAKAGE")


def _relevant_slate_bindings(
    slate: LiveSlateObservation, game_pk: int
) -> list[SourceBinding]:
    return [
        binding
        for binding in slate.source_bindings
        if game_pk in binding.game_pks
        and binding.binding_role in {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS"}
    ]


def _participant_career_bindings(
    participant: object, career_by_pitcher: Mapping[int, SourceBinding]
) -> list[SourceBinding]:
    if not isinstance(participant, PregameParticipants):
        return []
    pitcher_ids = (participant.away_starter_id, participant.home_starter_id)
    if any(type(pitcher_id) is not int or pitcher_id <= 0 for pitcher_id in pitcher_ids):
        return []
    return [
        career_by_pitcher[pitcher_id]
        for pitcher_id in pitcher_ids
        if pitcher_id in career_by_pitcher
    ]


def _unique_bindings(bindings: list[SourceBinding]) -> list[SourceBinding]:
    return sorted(
        {binding.binding_sha256: binding for binding in bindings}.values(),
        key=lambda binding: binding.binding_sha256,
    )


def _boundary(bindings: list[SourceBinding]) -> tuple[str, datetime]:
    if not bindings:
        raise ValueError("game has no source bindings")
    latest = max(bindings, key=lambda binding: _parse_utc(binding.durable_at_utc))
    return latest.durable_at_utc, _parse_utc(latest.durable_at_utc)


def _valid_reason_code(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value[0].isalpha()
        and value == value.upper()
        and all(character.isalnum() or character == "_" for character in value)
    )


class CandidateFeatureFailure(ValueError):
    """Explicitly candidate-local feature failure safe to map to UNSCORED."""

    def __init__(self, reason_code: str) -> None:
        if (
            not _valid_reason_code(reason_code)
            or reason_code not in _CANDIDATE_LOCAL_FEATURE_REASONS
        ):
            raise IntegrityError(str(reason_code))
        self.reason_code = reason_code
        super().__init__(reason_code)


def _candidate_failure_reasons(
    participant: PregameParticipants,
    game: Mapping[str, object],
    history: LiveHistoryBundle,
    career_bf_by_pitcher: Mapping[int, int],
) -> set[str]:
    if (
        participant.game_pk != game.get("game_pk")
        or participant.away_team_id != game.get("away_team_id")
        or participant.home_team_id != game.get("home_team_id")
    ):
        raise IntegrityError("pregame participant identity differs")
    reasons = participant.reason_codes
    if not isinstance(reasons, tuple) or any(
        not _valid_reason_code(reason) for reason in reasons
    ):
        raise IntegrityError("pregame participant provenance differs")
    nonprobable_reasons = set(reasons) - {"PROBABLE_CONSISTENT"}
    invalid_reasons = nonprobable_reasons - _CANDIDATE_LOCAL_FEATURE_REASONS
    if invalid_reasons:
        raise IntegrityError(sorted(invalid_reasons)[0])
    starters = (
        participant.away_starter_id,
        participant.home_starter_id,
    )
    probable_starters = (
        game.get("away_probable_pitcher_id"),
        game.get("home_probable_pitcher_id"),
    )
    if "STARTER_CONFLICT" in reasons or any(
        type(starter) is not int
        or starter <= 0
        or type(probable) is not int
        or probable <= 0
        or starter != probable
        for starter, probable in zip(starters, probable_starters, strict=True)
    ):
        return {"STARTER_CONFLICT"}
    for side in ("away", "home"):
        lineup = getattr(participant, f"{side}_lineup")
        if (
            not isinstance(lineup, tuple)
            or len(lineup) != 9
            or any(type(player_id) is not int or player_id <= 0 for player_id in lineup)
            or len(set(lineup)) != 9
        ):
            return {f"INVALID_{side.upper()}_LINEUP"}
    if not participant.complete:
        return nonprobable_reasons or {
            "INCOMPLETE_PREGAME_PARTICIPANTS"
        }
    if reasons != ("PROBABLE_CONSISTENT",):
        raise IntegrityError("pregame participant provenance differs")
    venue_id = game.get("venue_id")
    venue = (
        history.venue_references.get(venue_id)
        if type(venue_id) is int
        else None
    )
    if not isinstance(venue, Mapping) or venue.get("venue_name") != game.get(
        "venue_name"
    ):
        return {"MISSING_TARGET_VENUE_EVIDENCE"}
    try:
        _validate_venue_references(game, {venue_id: venue})
    except ValueError as error:
        if error.args != ("INVALID_VENUE_REFERENCE",):
            raise
        return {"INVALID_VENUE_REFERENCE"}
    if any(
        type(career_bf_by_pitcher.get(starter)) is not int
        or career_bf_by_pitcher[starter] < 0
        for starter in starters
    ):
        return {"INVALID_CAREER_BF"}
    return set()


def _unranked_row(
    game: Mapping[str, object],
    target_date: str,
    state: str,
    reasons: set[str],
    bindings: list[SourceBinding],
) -> dict[str, object]:
    return {
        "game_pk": game["game_pk"],
        "target_date": target_date,
        "away_team": game["away_team"],
        "home_team": game["home_team"],
        "state": state,
        "scheduled_start_utc": game["scheduled_start_utc"],
        "reason_codes": sorted(reasons),
        "source_binding_sha256s": sorted(
            {binding.binding_sha256 for binding in bindings}
        ),
    }


def _binding_mapping(binding: SourceBinding) -> dict[str, object]:
    value = json.loads(canonical_json_bytes(asdict(binding)))
    if not isinstance(value, dict):
        raise AssertionError("source binding must serialize as an object")
    return value


def build_live_top3_run(
    *,
    slate: LiveSlateObservation,
    final_schedule_observer: Callable[
        [int, str], FinalGameObservation | FinalGameScheduleDisposition
    ],
    history: LiveHistoryBundle,
    card: FrozenLiveCard,
    career_bf_by_pitcher: Mapping[int, int],
    career_bf_bindings: tuple[SourceBinding, ...],
    command_inputs: Mapping[str, object],
    execution_times: ExecutionTimes,
) -> dict[str, object]:
    """Return one validated, content-hashed experimental run payload."""
    if not callable(final_schedule_observer):
        raise ValueError("final_schedule_observer must be callable")
    career_by_pitcher = _validate_inputs(
        slate,
        history,
        card,
        career_bf_by_pitcher,
        career_bf_bindings,
        command_inputs,
        execution_times,
    )
    all_bindings = list(slate.source_bindings) + list(history.source_bindings)
    all_bindings.extend(career_bf_bindings)
    preliminary: list[dict[str, object]] = []
    unscored: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    slate_game_pks = tuple(sorted(int(game["game_pk"]) for game in slate.games))
    observed_games: list[
        tuple[
            Mapping[str, object],
            object,
            object,
            list[SourceBinding],
            str,
            set[str],
        ]
    ] = []
    scoreable_games: list[
        tuple[
            Mapping[str, object],
            PregameParticipants,
            list[SourceBinding],
            str,
            dict[str, object],
        ]
    ] = []

    for game in sorted(slate.games, key=lambda row: int(row["game_pk"])):
        game_pk = int(game["game_pk"])
        participant = slate.participants.get(game_pk)
        relevant = _relevant_slate_bindings(slate, game_pk)
        relevant.extend(history.source_bindings)
        relevant.extend(_participant_career_bindings(participant, career_by_pitcher))
        exclusion_reasons: set[str] = set()
        if game.get("official_date") != slate.target_date:
            exclusion_reasons.add("OFFICIAL_DATE_CHANGED")
        if game.get("status") not in ACCEPTED_PREGAME_STATUSES:
            exclusion_reasons.add("PREGAME_STATUS_NOT_ACCEPTED")

        final = final_schedule_observer(game_pk, slate.target_date)
        if isinstance(final, FinalGameScheduleDisposition):
            if final.game_pk != game_pk or final.target_date != slate.target_date:
                raise IntegrityError("final schedule disposition identity differs")
            _validate_live_binding(
                final.source_binding,
                role="FINAL_SCHEDULE",
                target_date=slate.target_date,
                slate_game_pks=slate_game_pks,
                game_pk=game_pk,
            )
            relevant.append(final.source_binding)
            all_bindings.append(final.source_binding)
            exclusion_reasons.update(final.reason_codes or ("FINAL_SCHEDULE_INVALID",))
        elif isinstance(final, FinalGameObservation):
            _validate_live_binding(
                final.source_binding,
                role="FINAL_SCHEDULE",
                target_date=slate.target_date,
                slate_game_pks=slate_game_pks,
                game_pk=game_pk,
            )
            _validate_final_projection(final)
            relevant.append(final.source_binding)
            all_bindings.append(final.source_binding)
            exclusion_reasons.update(_final_mismatch_reasons(game, final))
        else:
            exclusion_reasons.add("FINAL_SCHEDULE_INVALID")

        relevant = _unique_bindings(relevant)
        boundary_utc, boundary_time = _boundary(relevant)
        start_time = _parse_utc(str(game["scheduled_start_utc"]))
        if boundary_time >= start_time:
            exclusion_reasons.add("OBSERVATION_AT_OR_AFTER_START")
        observed_games.append(
            (
                game,
                participant,
                final,
                relevant,
                boundary_utc,
                exclusion_reasons,
            )
        )

    if len({binding.binding_sha256 for binding in all_bindings}) != len(all_bindings):
        raise IntegrityError("duplicate source binding digest")
    if len({binding.observation_id for binding in all_bindings}) != len(all_bindings):
        raise IntegrityError("duplicate source binding observation id")

    for (
        game,
        participant,
        final,
        relevant,
        boundary_utc,
        exclusion_reasons,
    ) in observed_games:
        game_pk = int(game["game_pk"])
        if exclusion_reasons:
            if exclusion_reasons == {"PROBABLE_PITCHER_CHANGED"}:
                unscored.append(
                    _unranked_row(
                        game,
                        slate.target_date,
                        "UNSCORED",
                        exclusion_reasons,
                        relevant,
                    )
                )
            else:
                excluded.append(
                    _unranked_row(
                        game,
                        slate.target_date,
                        "EXCLUDED",
                        exclusion_reasons,
                        relevant,
                    )
                )
            continue

        if not isinstance(participant, PregameParticipants):
            unscored.append(
                _unranked_row(
                    game,
                    slate.target_date,
                    "UNSCORED",
                    {"MISSING_PREGAME_PARTICIPANTS"},
                    relevant,
                )
            )
            continue

        candidate_reasons = _candidate_failure_reasons(
            participant,
            game,
            history,
            career_bf_by_pitcher,
        )
        if candidate_reasons:
            unscored.append(
                _unranked_row(
                    game,
                    slate.target_date,
                    "UNSCORED",
                    candidate_reasons,
                    relevant,
                )
            )
            continue
        roles = {binding.binding_role for binding in relevant}
        if not {
            "LIVE_SCHEDULE",
            "LIVE_PARTICIPANTS",
            "FINAL_SCHEDULE",
            "HISTORY_SCHEDULE",
            "HISTORY_STATCAST",
            "HISTORY_ERA",
            "HISTORY_VENUE",
            "CAREER_BF",
        } <= roles or sum(
            binding.binding_role == "CAREER_BF" for binding in relevant
        ) != 2:
            unscored.append(
                _unranked_row(
                    game,
                    slate.target_date,
                    "UNSCORED",
                    {"MISSING_SOURCE_BINDING"},
                    relevant,
                )
            )
            continue
        if any(binding.http_status != 200 for binding in relevant):
            unscored.append(
                _unranked_row(
                    game,
                    slate.target_date,
                    "UNSCORED",
                    {"SOURCE_BINDING_INVALID"},
                    relevant,
                )
            )
            continue
        assert isinstance(final, FinalGameObservation)
        candidate_feature_reason: str | None = None
        try:
            raw = build_live_raw_game_features(
                participant, final, history, career_bf_by_pitcher
            )
        except CandidateFeatureFailure as error:
            if error.reason_code not in _CANDIDATE_LOCAL_FEATURE_REASONS:
                raise IntegrityError(error.reason_code) from error
            candidate_feature_reason = error.reason_code
        except IntegrityError:
            raise
        except ValueError as error:
            if (
                len(error.args) != 1
                or type(error.args[0]) is not str
                or error.args[0] not in _CANDIDATE_LOCAL_FEATURE_REASONS
            ):
                raise
            candidate_feature_reason = error.args[0]
        if candidate_feature_reason is not None:
            unscored.append(
                _unranked_row(
                    game,
                    slate.target_date,
                    "UNSCORED",
                    {candidate_feature_reason},
                    relevant,
                )
            )
            continue
        scoreable_games.append(
            (game, participant, relevant, boundary_utc, raw)
        )

    for game, participant, relevant, boundary_utc, raw in scoreable_games:
        game_pk = int(game["game_pk"])
        scored = score_raw_game(raw, card.normalization)
        p_home = home_probability(scored["edge"], card.alpha, card.beta)
        pick = predicted_pick(game["home_team"], game["away_team"], p_home)
        completeness = overall_completeness(scored["qualities"])
        starter_edge_abs = abs(
            float(scored["home_scores"]["starter"])
            - float(scored["away_scores"]["starter"])
        )
        core_edges = {
            name: float(scored["home_scores"][name])
            - float(scored["away_scores"][name])
            for name in ("starter", "offense", "bullpen")
        }
        agreement = component_agreement(core_edges, pick["predicted_side"])
        confidence = confidence_score(
            completeness,
            scored["qualities"]["starter"],
            scored["qualities"]["offense"],
            scored["qualities"]["bullpen"],
            agreement,
            scored["qualities"]["environment"],
            scored["qualities"]["schedule"],
        )
        starter_career_bf = min(
            raw[side]["starter"]["career_bf"] for side in ("away", "home")
        )
        unresolved_context = any(
            scored["qualities"][name] < 1.0
            for name in ("environment", "defense", "schedule")
        )
        uncertainty_label = uncertainty(
            completeness,
            False,
            starter_career_bf,
            unresolved_context,
        )
        tier(pick["pick_probability"], confidence, uncertainty_label)
        if pick["predicted_side"] == "none":
            unscored.append(
                _unranked_row(
                    game,
                    slate.target_date,
                    "UNSCORED",
                    {"EXACT_NEUTRAL_SCORE"},
                    relevant,
                )
            )
            continue
        preliminary.append(
            {
                "game_pk": game_pk,
                "target_date": slate.target_date,
                "away_team": game["away_team"],
                "home_team": game["home_team"],
                "predicted_winner": pick["predicted_winner"],
                "pick_probability": pick["pick_probability"],
                "pick_probability_display": f"{pick['pick_probability']:.1%}",
                "state": "PRELIMINARY",
                "rank": 0,
                "data_completeness": completeness,
                "starter_edge_abs": starter_edge_abs,
                "starter_provenance": raw["starter_provenance"],
                "lineup_provenance": raw["lineup_provenance"],
                "scheduled_start_utc": game["scheduled_start_utc"],
                "observation_boundary_utc": boundary_utc,
                "reason_codes": sorted(set(participant.reason_codes)),
                "model_card_sha256": card.file_sha256,
                "feature_sha256": hashlib.sha256(
                    canonical_json_bytes(raw)
                ).hexdigest(),
                "source_binding_sha256s": sorted(
                    binding.binding_sha256 for binding in relevant
                ),
            }
        )

    preliminary.sort(
        key=lambda row: (
            -float(row["pick_probability"]),
            -float(row["data_completeness"]),
            -float(row["starter_edge_abs"]),
            str(row["scheduled_start_utc"]),
            int(row["game_pk"]),
        )
    )
    preliminary = preliminary[:3]
    for rank, row in enumerate(preliminary, 1):
        row["rank"] = rank
    unscored.sort(key=lambda row: (row["scheduled_start_utc"], row["game_pk"]))
    excluded.sort(key=lambda row: (row["scheduled_start_utc"], row["game_pk"]))

    unique_bindings = _unique_bindings(all_bindings)
    payload: dict[str, object] = {
        "schema_version": 1,
        "model_label": MODEL_LABEL,
        "target_date": slate.target_date,
        "command_inputs": dict(command_inputs),
        "executed_at_eastern": execution_times.executed_at_eastern,
        "executed_at_utc": execution_times.executed_at_utc,
        "history_cutoff_date": history.cutoff_date,
        "cache_authority": dict(history.cache_authority),
        "model_card_sha256": card.file_sha256,
        "source_bindings": [
            _binding_mapping(binding) for binding in unique_bindings
        ],
        "verified_top3": [],
        "preliminary_top3": preliminary,
        "unscored_games": unscored,
        "excluded_games": excluded,
        "warnings": [],
    }
    payload["content_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return validate_run_payload(payload)


__all__ = ["CandidateFeatureFailure", "build_live_top3_run"]
