"""Canonical, research-only trace records for live Top 3 scoring.

This module intentionally has no live acquisition or history dependencies.  A
stored row is enough to replay the production scorer against the frozen card.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from numbers import Real
from pathlib import PurePosixPath

from .live_model_card import EXPECTED_MODEL_CARD_SHA256, FrozenLiveCard
from .live_top3_contracts import IntegrityError, canonical_json_bytes
from .scoring import WEIGHTS, home_probability, predicted_pick
from .scoring_prep import score_raw_game


TRACE_SCHEMA_VERSION = 1
RESEARCH_STATUS = "PRELIMINARY_RESEARCH_ONLY"
_TRACE_KEYS = frozenset(
    {
        "schema_version",
        "research_status",
        "not_a_betting_recommendation",
        "primary_report_relative",
        "primary_report_content_sha256",
        "model_card_sha256",
        "games",
        "content_sha256",
    }
)
_ROW_KEYS = frozenset(
    {
        "game_pk",
        "target_date",
        "away_team",
        "home_team",
        "scheduled_start_utc",
        "raw_features",
        "feature_sha256",
        "model_card_sha256",
        "away_scores",
        "home_scores",
        "qualities",
        "contributions",
        "total_edge",
        "p_home",
        "predicted_side",
        "pick_probability",
        "primary_rank",
        "disposition",
    }
)
_OPTIONAL_ROW_KEYS = frozenset({"away_qualities", "home_qualities", "edge"})
_DISPOSITIONS = frozenset(
    {"PRELIMINARY", "SCORED_NOT_TOP3", "UNSCORED_EXACT_NEUTRAL"}
)
_SHA_HEX = frozenset("0123456789abcdef")


def _error(message: str) -> IntegrityError:
    return IntegrityError(f"live score trace {message}")


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in _SHA_HEX for char in value
    ):
        raise _error(f"{label} differs")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _error(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise _error(f"{label} must be a finite number")
    return result


def _bounded(value: object, label: str, low: float, high: float) -> float:
    result = _finite(value, label)
    if not low <= result <= high:
        raise _error(f"{label} is out of range")
    return result


def _validate_raw_json(value: object, label: str = "raw_features") -> None:
    """Reject non-finite numbers while allowing JSON booleans such as eligible."""
    if isinstance(value, float) and not math.isfinite(value):
        raise _error(f"{label} contains a non-finite number")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _error(f"{label} keys differ")
            _validate_raw_json(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_raw_json(item, f"{label}[{index}]")


def _validate_components(value: object, label: str, low: float, high: float) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(WEIGHTS):
        raise _error(f"{label} component keys differ")
    return {name: _bounded(value[name], f"{label}.{name}", low, high) for name in WEIGHTS}


def _close(actual: object, expected: object, label: str) -> None:
    actual_value = _finite(actual, label)
    expected_value = _finite(expected, label)
    if abs(actual_value - expected_value) > 1e-12:
        raise _error(f"{label} differs")


def _canonical_copy(value: object) -> object:
    try:
        return json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise _error("contains non-canonical JSON") from exc


def _validate_row(
    value: object, *, expected_model_card_sha256: str | None = None
) -> dict[str, object]:
    if not isinstance(value, Mapping) or not (
        _ROW_KEYS <= set(value) <= _ROW_KEYS | _OPTIONAL_ROW_KEYS
    ):
        raise _error("game row fields differ")
    row = _canonical_copy(value)
    if not isinstance(row, dict):
        raise _error("game row must be an object")
    if type(row["game_pk"]) is not int or row["game_pk"] <= 0:
        raise _error("game_pk differs")
    if not isinstance(row["target_date"], str) or not row["target_date"]:
        raise _error("target_date differs")
    for field in ("away_team", "home_team", "scheduled_start_utc"):
        if not isinstance(row[field], str) or not row[field]:
            raise _error(f"{field} differs")
    raw_features = row["raw_features"]
    _validate_raw_json(raw_features)
    if (
        not isinstance(raw_features, Mapping)
        or raw_features.get("eligible") is not True
        or raw_features.get("game_pk") != row["game_pk"]
        or raw_features.get("official_date") != row["target_date"]
    ):
        raise _error("raw feature identity or eligibility differs")
    feature_sha = _sha(row["feature_sha256"], "feature_sha256")
    expected_feature_sha = hashlib.sha256(
        canonical_json_bytes(row["raw_features"])
    ).hexdigest()
    if feature_sha != expected_feature_sha:
        raise _error("feature_sha256 differs")
    row_card_sha = _sha(row["model_card_sha256"], "model_card_sha256")
    if expected_model_card_sha256 is not None and row_card_sha != expected_model_card_sha256:
        raise _error("game model card differs from payload")
    away_scores = _validate_components(row["away_scores"], "away_scores", 0.0, 100.0)
    home_scores = _validate_components(row["home_scores"], "home_scores", 0.0, 100.0)
    qualities = _validate_components(row["qualities"], "qualities", 0.0, 1.0)
    for field in ("away_qualities", "home_qualities"):
        if field in row:
            _validate_components(row[field], field, 0.0, 1.0)
    contributions = _validate_components(row["contributions"], "contributions", -100.0, 100.0)
    for name in WEIGHTS:
        _close(
            contributions[name],
            WEIGHTS[name] * (home_scores[name] - away_scores[name]),
            f"contributions.{name}",
        )
    _close(math.fsum(contributions.values()), row["total_edge"], "total_edge")
    _bounded(row["total_edge"], "total_edge", -100.0, 100.0)
    if "edge" in row:
        _close(row["edge"], row["total_edge"], "edge")
    p_home = _bounded(row["p_home"], "p_home", 0.0, 1.0)
    side = row["predicted_side"]
    if side not in {"home", "away", "none"}:
        raise _error("predicted_side differs")
    pick_probability = _bounded(row["pick_probability"], "pick_probability", 0.0, 1.0)
    expected_probability = 0.5 if side == "none" else max(p_home, 1.0 - p_home)
    _close(pick_probability, expected_probability, "pick_probability")
    if (side == "home" and p_home <= 0.5) or (side == "away" and p_home >= 0.5):
        raise _error("predicted_side is inconsistent with p_home")
    rank = row["primary_rank"]
    if rank is not None and (type(rank) is not int or rank <= 0):
        raise _error("primary_rank differs")
    disposition = row["disposition"]
    if disposition not in _DISPOSITIONS:
        raise _error("disposition differs")
    if side == "none":
        if (
            p_home != 0.5
            or rank is not None
            or disposition != "UNSCORED_EXACT_NEUTRAL"
        ):
            raise _error("neutral record state differs")
    elif disposition == "PRELIMINARY":
        if rank is None:
            raise _error("preliminary rank is missing")
    elif disposition == "SCORED_NOT_TOP3":
        if rank is not None:
            raise _error("non-top3 rank differs")
    else:
        raise _error("scored record state differs")
    return row


def build_trace_payload(
    *,
    primary_report_relative: str,
    primary_report_content_sha256: str,
    model_card_sha256: str,
    games: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build and validate one canonical trace sidecar payload."""
    if (
        not isinstance(primary_report_relative, str)
        or not primary_report_relative
        or PurePosixPath(primary_report_relative).is_absolute()
        or "\\" in primary_report_relative
        or any(part in {"", ".", ".."} for part in PurePosixPath(primary_report_relative).parts)
        or not primary_report_relative.endswith(".json")
    ):
        raise _error("primary report path differs")
    _sha(primary_report_content_sha256, "primary report content_sha256")
    _sha(model_card_sha256, "model_card_sha256")
    if not isinstance(games, Sequence) or isinstance(games, (str, bytes)):
        raise _error("games must be an array")
    copied_games = [_canonical_copy(game) for game in games]
    payload: dict[str, object] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "research_status": RESEARCH_STATUS,
        "not_a_betting_recommendation": True,
        "primary_report_relative": primary_report_relative,
        "primary_report_content_sha256": primary_report_content_sha256,
        "model_card_sha256": model_card_sha256,
        "games": copied_games,
    }
    payload["content_sha256"] = hashlib.sha256(
        canonical_json_bytes(payload)
    ).hexdigest()
    return validate_trace_payload(payload)


def validate_trace_payload(
    value: object,
    *,
    expected_primary_report_content_sha256: str | None = None,
    expected_model_card_sha256: str | None = None,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _TRACE_KEYS:
        raise _error("payload keys differ")
    safe = _canonical_copy(value)
    if not isinstance(safe, dict):
        raise _error("payload must be an object")
    if type(safe["schema_version"]) is not int or safe["schema_version"] != TRACE_SCHEMA_VERSION:
        raise _error("schema version differs")
    if safe["research_status"] != RESEARCH_STATUS:
        raise _error("research status differs")
    if safe["not_a_betting_recommendation"] is not True:
        raise _error("betting recommendation flag differs")
    primary = safe["primary_report_relative"]
    if not isinstance(primary, str) or not primary.endswith(".json") or PurePosixPath(primary).is_absolute() or "\\" in primary or any(part in {"", ".", ".."} for part in PurePosixPath(primary).parts):
        raise _error("primary report path differs")
    _sha(safe["primary_report_content_sha256"], "primary report content_sha256")
    _sha(safe["model_card_sha256"], "model_card_sha256")
    if (
        expected_primary_report_content_sha256 is not None
        and safe["primary_report_content_sha256"]
        != expected_primary_report_content_sha256
    ):
        raise _error("primary report content_sha256 binding differs")
    if (
        expected_model_card_sha256 is not None
        and safe["model_card_sha256"] != expected_model_card_sha256
    ):
        raise _error("model card content_sha256 binding differs")
    games = safe["games"]
    if not isinstance(games, list):
        raise _error("games must be an array")
    rows = [
        _validate_row(row, expected_model_card_sha256=safe["model_card_sha256"])
        for row in games
    ]
    game_pks = [row["game_pk"] for row in rows]
    if len(set(game_pks)) != len(game_pks):
        raise _error("game identities differ")
    ranks = [row["primary_rank"] for row in rows if row["primary_rank"] is not None]
    if len(ranks) != len(set(ranks)) or sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise _error("primary ranks differ")
    expected_digest = hashlib.sha256(
        canonical_json_bytes({key: safe[key] for key in safe if key != "content_sha256"})
    ).hexdigest()
    if safe["content_sha256"] != expected_digest:
        raise _error("content_sha256 differs")
    return safe


def replay_stored_row(
    row: Mapping[str, object],
    frozen_card: FrozenLiveCard,
) -> dict[str, object]:
    """Replay one captured row through the production scorer, without I/O."""
    safe = _validate_row(row)
    if not isinstance(frozen_card, FrozenLiveCard):
        raise _error("frozen card differs")
    if safe["model_card_sha256"] != frozen_card.file_sha256:
        raise _error("model card digest differs")
    if frozen_card.file_sha256 != EXPECTED_MODEL_CARD_SHA256:
        raise _error("frozen model card digest differs")
    raw = safe["raw_features"]
    scored = score_raw_game(raw, frozen_card.normalization)
    for field in ("away_scores", "home_scores", "qualities"):
        expected = scored[field]
        actual = safe[field]
        actual_values = _validate_components(actual, field, 0.0, 100.0 if field != "qualities" else 1.0)
        for name in WEIGHTS:
            _close(actual_values[name], expected[name], field + "." + name)
    for field in ("away_qualities", "home_qualities"):
        if field in safe:
            actual_values = _validate_components(safe[field], field, 0.0, 1.0)
            for name in WEIGHTS:
                _close(actual_values[name], scored[field][name], field + "." + name)
    for name in WEIGHTS:
        _close(
            safe["contributions"][name],
            WEIGHTS[name] * (scored["home_scores"][name] - scored["away_scores"][name]),
            f"contributions.{name}",
        )
    _close(safe["total_edge"], scored["edge"], "total_edge")
    if "edge" in safe:
        _close(safe["edge"], scored["edge"], "edge")
    replayed_p_home = home_probability(scored["edge"], frozen_card.alpha, frozen_card.beta)
    _close(safe["p_home"], replayed_p_home, "p_home")
    pick = predicted_pick(safe["home_team"], safe["away_team"], replayed_p_home)
    if safe["predicted_side"] != pick["predicted_side"]:
        raise _error("predicted_side differs on replay")
    _close(safe["pick_probability"], pick["pick_probability"], "pick_probability")
    return {
        "away_scores": scored["away_scores"],
        "home_scores": scored["home_scores"],
        "away_qualities": scored["away_qualities"],
        "home_qualities": scored["home_qualities"],
        "qualities": scored["qualities"],
        "edge": scored["edge"],
        "p_home": replayed_p_home,
        "predicted_side": pick["predicted_side"],
        "pick_probability": pick["pick_probability"],
    }


# Descriptive aliases make the narrow pure API easy to discover for callers.
canonical_trace_payload = build_trace_payload
replay_trace_row = replay_stored_row

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "RESEARCH_STATUS",
    "build_trace_payload",
    "canonical_trace_payload",
    "validate_trace_payload",
    "replay_stored_row",
    "replay_trace_row",
]
