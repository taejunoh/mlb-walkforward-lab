"""Result-free live participant adaptation for shared v5.1 feature math."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .feature_engine import (
    FeatureHistory,
    ScheduleHistory,
    _build_raw_components,
    _canonical_start,
    _league_era_record,
)
from .live_sources import (
    ACCEPTED_PREGAME_STATUSES,
    PregameParticipants,
    _LIVE_SCHEDULE_FIELDS,
)
from .live_top3_contracts import FinalGameObservation, LiveHistoryBundle
from .statcast import StatcastData


_PREPARED_AUTHORITY_SEAL = object()


def _fail(reason_code: str) -> None:
    raise ValueError(reason_code)


def _valid_ordered_nine(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and len(value) == 9
        and all(type(player_id) is int and player_id > 0 for player_id in value)
        and len(set(value)) == 9
    )


def _validate_team_map(value: object) -> None:
    if (
        not isinstance(value, Mapping)
        or len(value) != 30
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(code, str)
            or not code
            for name, code in value.items()
        )
    ):
        _fail("INVALID_TEAM_MAP")
    if len(set(value.values())) != 30:
        _fail("INVALID_TEAM_MAP")


def _validate_venue_authority(
    references: Mapping[int, Mapping[str, object]],
) -> tuple[dict[int, Mapping[str, object]], dict[str, Mapping[str, object]]]:
    if not isinstance(references, Mapping) or not references:
        _fail("INVALID_VENUE_REFERENCE")
    required = {
        "venue_name",
        "venue_city",
        "venue_latitude",
        "venue_longitude",
        "time_zone",
    }
    by_name: dict[str, Mapping[str, object]] = {}
    detached_by_id: dict[int, Mapping[str, object]] = {}
    for venue_id, raw_reference in references.items():
        if (
            type(venue_id) is not int
            or venue_id <= 0
            or not isinstance(raw_reference, Mapping)
            or set(raw_reference) != required
        ):
            _fail("INVALID_VENUE_REFERENCE")
        reference = dict(raw_reference)
        name = reference["venue_name"]
        city = reference["venue_city"]
        latitude = reference["venue_latitude"]
        longitude = reference["venue_longitude"]
        time_zone = reference["time_zone"]
        missing_coordinates = latitude is None and longitude is None
        if (
            not isinstance(name, str)
            or not name
            or name in by_name
            or not isinstance(city, str)
            or not city
            or not missing_coordinates
            and (
                isinstance(latitude, bool)
                or not isinstance(latitude, (int, float))
                or not math.isfinite(float(latitude))
                or not -90 <= float(latitude) <= 90
                or isinstance(longitude, bool)
                or not isinstance(longitude, (int, float))
                or not math.isfinite(float(longitude))
                or not -180 <= float(longitude) <= 180
            )
            or not isinstance(time_zone, str)
            or not time_zone
        ):
            _fail("INVALID_VENUE_REFERENCE")
        try:
            ZoneInfo(time_zone)
        except (ValueError, ZoneInfoNotFoundError):
            _fail("INVALID_VENUE_REFERENCE")
        detached_by_id[venue_id] = reference
        by_name[name] = {
            "venue_id": venue_id,
            "city": city,
            "latitude": None if missing_coordinates else float(latitude),
            "longitude": None if missing_coordinates else float(longitude),
            "time_zone": time_zone,
        }
    return detached_by_id, by_name


def _validate_venue_references(
    game: Mapping[str, object], references: Mapping[int, Mapping[str, object]]
) -> tuple[Mapping[str, object], dict[str, Mapping[str, object]]]:
    detached_by_id, by_name = _validate_venue_authority(references)
    target_venue_id = game.get("venue_id")
    if type(target_venue_id) is not int or target_venue_id <= 0:
        _fail("VENUE_IDENTITY_MISMATCH")
    target = detached_by_id.get(target_venue_id)
    if target is None or target["venue_name"] != game.get("venue_name"):
        _fail("VENUE_IDENTITY_MISMATCH")
    return target, by_name


def _prepare_venue_authority(
    references: Mapping[int, Mapping[str, object]],
) -> tuple[dict[int, Mapping[str, object]], dict[str, Mapping[str, object]]]:
    if not isinstance(references, Mapping) or not references:
        _fail("INVALID_VENUE_REFERENCE")
    by_id: dict[int, Mapping[str, object]] = {}
    by_name: dict[str, Mapping[str, object]] = {}
    for venue_id, reference in references.items():
        try:
            detached, named = _validate_venue_authority({venue_id: reference})
        except ValueError as error:
            if error.args != ("INVALID_VENUE_REFERENCE",):
                raise
            continue
        name = next(iter(named))
        if name in by_name:
            _fail("INVALID_VENUE_REFERENCE")
        by_id.update(detached)
        by_name.update(named)
    if not by_id:
        _fail("INVALID_VENUE_REFERENCE")
    return by_id, by_name


def _detach_history_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                key: _detach_history_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_detach_history_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_detach_history_value(item) for item in value)
    return value


def _detach_history_rows(rows) -> tuple[object, ...]:
    return tuple(_detach_history_value(row) for row in rows)


class _SealedScheduleHistory(ScheduleHistory):
    def __init__(self, *args, **kwargs) -> None:
        self._sealed = False
        super().__init__(*args, **kwargs)
        frozen_rows = tuple(_detach_history_value(row) for row in self.rows)
        by_game_pk = {int(row["game_pk"]): row for row in frozen_rows}
        self.rows = frozen_rows
        self.by_team = MappingProxyType(
            {
                key: tuple(by_game_pk[int(row["game_pk"])] for row in rows)
                for key, rows in self.by_team.items()
            }
        )
        self.by_team_date = MappingProxyType(
            {
                key: tuple(by_game_pk[int(row["game_pk"])] for row in rows)
                for key, rows in self.by_team_date.items()
            }
        )
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("prepared schedule history is read-only")
        super().__setattr__(name, value)

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            return MappingProxyType(object.__getattribute__(self, "__dict__"))
        return super().__getattribute__(name)


class _SealedFeatureHistory(FeatureHistory):
    def __init__(self, *args, **kwargs) -> None:
        self._sealed = False
        super().__init__(*args, **kwargs)
        self.roles = _detach_history_value(self.roles)
        self.venue_by_name = _detach_history_value(self.venue_by_name)
        self._sealed = True

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("prepared feature history is read-only")
        super().__setattr__(name, value)

    def __getattribute__(self, name: str) -> object:
        if name == "__dict__":
            return MappingProxyType(object.__getattribute__(self, "__dict__"))
        return super().__getattribute__(name)


def _validate_history(
    bundle: LiveHistoryBundle,
    venue_by_name: Mapping[str, Mapping[str, object]],
) -> FeatureHistory:
    history = bundle.feature_history
    if not isinstance(history, FeatureHistory):
        _fail("INVALID_HISTORY_BUNDLE")
    cutoff = date.fromisoformat(bundle.cutoff_date)
    if (
        not history.statcast_coverage_complete
        or not history.schedule_history.coverage_complete
        or history.schedule_history.coverage_end != cutoff
    ):
        _fail("INCOMPLETE_D1_HISTORY")
    pitches = _detach_history_rows(history.pitches)
    plate_appearances = _detach_history_rows(history.plate_appearances)
    schedule_rows = _detach_history_rows(history.schedule_history.rows)
    result_fields = {
        "away_score",
        "home_score",
        "away_is_winner",
        "home_is_winner",
        "winner",
        "result",
        "runs",
    }
    for rows, date_field, extraneous_date_field, schedule_kind in (
        (pitches, "game_date", "official_date", False),
        (plate_appearances, "game_date", "official_date", False),
        (schedule_rows, "official_date", "game_date", True),
    ):
        for row in rows:
            if not isinstance(row, Mapping):
                _fail("AS_OF_VIOLATION")
            if extraneous_date_field in row:
                _fail("AS_OF_VIOLATION")
            row_game_pk = row.get("game_pk")
            if type(row_game_pk) is not int or row_game_pk <= 0:
                _fail("AS_OF_VIOLATION")
            raw_day = row.get(date_field)
            try:
                row_day = date.fromisoformat(str(raw_day))
            except ValueError:
                _fail("AS_OF_VIOLATION")
            if (
                not isinstance(raw_day, str)
                or row_day.isoformat() != raw_day
                or row_day > cutoff
                or schedule_kind and result_fields & set(row)
            ):
                _fail("AS_OF_VIOLATION")
            if schedule_kind:
                venue_name = row.get("venue_name")
                venue = venue_by_name.get(str(venue_name))
                if venue is None:
                    _fail("INVALID_VENUE_REFERENCE")
                if "venue_id" in row and row["venue_id"] != venue["venue_id"]:
                    _fail("INVALID_VENUE_REFERENCE")
                for row_field, authority_field in (
                    ("venue_city", "city"),
                    ("venue_latitude", "latitude"),
                    ("venue_longitude", "longitude"),
                    ("time_zone", "time_zone"),
                ):
                    if row_field in row and row[row_field] != venue[authority_field]:
                        _fail("INVALID_VENUE_REFERENCE")
    schedule = history.schedule_history
    try:
        detached_schedule = _SealedScheduleHistory(
            schedule_rows,
            coverage_start=(
                schedule.coverage_start.isoformat()
                if schedule.coverage_start is not None
                else None
            ),
            coverage_end=(
                schedule.coverage_end.isoformat()
                if schedule.coverage_end is not None
                else None
            ),
            coverage_complete=schedule.coverage_complete,
        )
        return _SealedFeatureHistory(
            StatcastData(pitches, plate_appearances),
            slot_weights=dict(history.slot_weights),
            schedule_history=detached_schedule,
            venue_by_name=venue_by_name,
            statcast_coverage_complete=history.statcast_coverage_complete,
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        _fail("INVALID_HISTORY_BUNDLE")


@dataclass(frozen=True)
class PreparedLiveFeatureHistory:
    """Detached authoritative history reusable across one sealed live slate."""

    _bundle: LiveHistoryBundle
    target_game_pks: frozenset[int]
    feature_history: FeatureHistory
    venue_by_id: Mapping[int, Mapping[str, object]]
    venue_by_name: Mapping[str, Mapping[str, object]]
    _seal: object = field(default=None, repr=False, compare=False)


def prepare_live_feature_history(
    bundle: LiveHistoryBundle, *, target_game_pks: tuple[int, ...]
) -> PreparedLiveFeatureHistory:
    """Validate and rebuild raw D-1 authority once for a sealed target slate."""
    if not isinstance(bundle, LiveHistoryBundle):
        _fail("INVALID_HISTORY_BUNDLE")
    if (
        not isinstance(target_game_pks, tuple)
        or any(type(game_pk) is not int or game_pk <= 0 for game_pk in target_game_pks)
        or len(set(target_game_pks)) != len(target_game_pks)
    ):
        _fail("INVALID_TARGET_GAME_PKS")
    sealed_game_pks = frozenset(target_game_pks)
    venue_by_id, venue_by_name = _prepare_venue_authority(
        bundle.venue_references
    )
    history = _validate_history(bundle, venue_by_name)
    historical_game_pks = (
        history.pitch_game_pks
        | history.pa_game_pks
        | frozenset(history.schedule_history.game_dates)
    )
    if sealed_game_pks & historical_game_pks:
        _fail("AS_OF_VIOLATION")
    return PreparedLiveFeatureHistory(
        bundle,
        sealed_game_pks,
        history,
        _detach_history_value(venue_by_id),
        _detach_history_value(venue_by_name),
        _PREPARED_AUTHORITY_SEAL,
    )


def _validate_participants(
    participants: PregameParticipants, game: Mapping[str, object]
) -> dict[str, int]:
    if participants.game_pk != game.get("game_pk"):
        _fail("GAME_IDENTITY_MISMATCH")
    if (
        participants.away_team_id != game.get("away_team_id")
        or participants.home_team_id != game.get("home_team_id")
    ):
        _fail("TEAM_IDENTITY_MISMATCH")
    reasons = participants.reason_codes
    if not isinstance(reasons, tuple) or any(
        not isinstance(reason, str)
        or re.fullmatch(r"[A-Z][A-Z0-9_]*", reason) is None
        for reason in reasons
    ):
        _fail("INVALID_PARTICIPANT_PROVENANCE")
    if "STARTER_CONFLICT" in reasons:
        _fail("STARTER_CONFLICT")
    for side in ("away", "home"):
        lineup = getattr(participants, f"{side}_lineup")
        if not _valid_ordered_nine(lineup):
            _fail(f"INVALID_{side.upper()}_LINEUP")
    starters: dict[str, int] = {}
    for side in ("away", "home"):
        pitcher_id = getattr(participants, f"{side}_pitcher_list_id")
        probable_id = game.get(f"{side}_probable_pitcher_id")
        if (
            type(pitcher_id) is not int
            or pitcher_id <= 0
            or type(probable_id) is not int
            or probable_id <= 0
            or pitcher_id != probable_id
        ):
            _fail("STARTER_CONFLICT")
        starters[side] = pitcher_id
    if not participants.complete:
        for reason in reasons:
            if reason != "PROBABLE_CONSISTENT":
                _fail(reason)
        _fail("INCOMPLETE_PREGAME_PARTICIPANTS")
    if reasons != ("PROBABLE_CONSISTENT",):
        _fail("INVALID_PARTICIPANT_PROVENANCE")
    return starters


def _validate_career_bf(
    values: object, starter_ids: Mapping[str, int]
) -> Mapping[int, int]:
    if not isinstance(values, Mapping):
        _fail("INVALID_CAREER_BF")
    for pitcher_id in starter_ids.values():
        value = values.get(pitcher_id)
        if type(value) is not int or value < 0:
            _fail("INVALID_CAREER_BF")
    return values


def _adapt_game(
    game: Mapping[str, object],
    participants: PregameParticipants,
    venue_reference: Mapping[str, object],
) -> dict[str, object]:
    adapted = dict(game)
    adapted["away_lineup"] = [
        {"player_id": player_id} for player_id in participants.away_lineup
    ]
    adapted["home_lineup"] = [
        {"player_id": player_id} for player_id in participants.home_lineup
    ]
    adapted.update(venue_reference)
    return adapted


def build_live_raw_game_features(
    participants: PregameParticipants,
    final_observation: FinalGameObservation,
    history_bundle: LiveHistoryBundle,
    career_bf_total_by_pitcher: Mapping[int, int],
    *,
    prepared_history: PreparedLiveFeatureHistory | None = None,
) -> dict[str, object]:
    """Build one live raw row from authenticated pregame and exact D-1 inputs."""
    if not isinstance(participants, PregameParticipants):
        _fail("INVALID_PREGAME_PARTICIPANTS")
    if not isinstance(final_observation, FinalGameObservation):
        _fail("INVALID_FINAL_OBSERVATION")
    if not isinstance(history_bundle, LiveHistoryBundle):
        _fail("INVALID_HISTORY_BUNDLE")
    game = final_observation.projected_game
    if set(game) != set(_LIVE_SCHEDULE_FIELDS):
        _fail("INVALID_FINAL_GAME_PROJECTION")
    if any(game.get(f"{side}_lineup") for side in ("away", "home")):
        _fail("TARGET_PARTICIPANT_LEAKAGE")
    if final_observation.target_date != history_bundle.target_date:
        _fail("TARGET_DATE_MISMATCH")
    if game.get("official_date") != history_bundle.target_date:
        _fail("TARGET_DATE_MISMATCH")
    try:
        final_identity = json.loads(
            final_observation.source_binding.query_identity_json
        )
    except (TypeError, json.JSONDecodeError):
        _fail("FINAL_BINDING_IDENTITY_MISMATCH")
    if (
        not isinstance(final_identity, Mapping)
        or final_identity.get("target_date") != history_bundle.target_date
    ):
        _fail("FINAL_BINDING_IDENTITY_MISMATCH")
    if game.get("game_type") != "R":
        _fail("INVALID_GAME_TYPE")
    if game.get("status") not in ACCEPTED_PREGAME_STATUSES:
        _fail("PREGAME_STATUS_NOT_ACCEPTED")
    if not _canonical_start(game.get("scheduled_start_utc")):
        _fail("INVALID_SCHEDULED_START_UTC")
    if (
        not isinstance(game.get("away_team"), str)
        or not game["away_team"]
        or not isinstance(game.get("home_team"), str)
        or not game["home_team"]
        or game["away_team"] == game["home_team"]
        or type(game.get("away_team_id")) is not int
        or game["away_team_id"] <= 0
        or type(game.get("home_team_id")) is not int
        or game["home_team_id"] <= 0
        or game["away_team_id"] == game["home_team_id"]
    ):
        _fail("TEAM_IDENTITY_MISMATCH")
    _validate_team_map(history_bundle.team_code_by_name)
    if any(
        game[f"{side}_team"] not in history_bundle.team_code_by_name
        for side in ("away", "home")
    ):
        _fail("TEAM_IDENTITY_MISMATCH")
    starter_ids = _validate_participants(participants, game)
    career_bf = _validate_career_bf(career_bf_total_by_pitcher, starter_ids)
    legacy_preparation = prepared_history is None
    if legacy_preparation:
        venue, _venue_by_name = _validate_venue_references(
            game, history_bundle.venue_references
        )
        prepared_history = prepare_live_feature_history(
            history_bundle,
            target_game_pks=(final_observation.game_pk,),
        )
    elif (
        not isinstance(prepared_history, PreparedLiveFeatureHistory)
        or prepared_history._seal is not _PREPARED_AUTHORITY_SEAL
        or prepared_history._bundle is not history_bundle
        or final_observation.game_pk not in prepared_history.target_game_pks
    ):
        _fail("INVALID_PREPARED_HISTORY")
    else:
        venue_id = game.get("venue_id")
        venue = (
            prepared_history.venue_by_id.get(venue_id)
            if type(venue_id) is int
            else None
        )
        if venue is None or venue.get("venue_name") != game.get("venue_name"):
            _fail("VENUE_IDENTITY_MISMATCH")
    history = prepared_history.feature_history
    target = date.fromisoformat(history_bundle.target_date)
    resolved_era, era_provenance, era_reason = _league_era_record(
        history_bundle.league_era_by_asof, target
    )
    if era_reason is not None or resolved_era is None or era_provenance is None:
        _fail((era_reason or "invalid_league_era_provenance").upper())
    adapted = _adapt_game(game, participants, venue)
    return _build_raw_components(
        adapted,
        history,
        history_bundle.team_code_by_name,
        resolved_era,
        starter_ids=starter_ids,
        starter_provenance="PROBABLE_CONSISTENT",
        lineup_provenance="OBSERVED_PREGAME_LINEUP",
        league_era_provenance=era_provenance,
        career_bf_total_by_pitcher=career_bf,
    )


__all__ = [
    "PreparedLiveFeatureHistory",
    "build_live_raw_game_features",
    "prepare_live_feature_history",
]
