"""Pure arithmetic contracts for the DraftKings totals market overlay."""

from __future__ import annotations

import math
import re
import json
import hashlib
from dataclasses import dataclass
from fractions import Fraction
from numbers import Rational, Real
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import MappingProxyType
from urllib.parse import urlsplit

from .live_top3_contracts import canonical_json_bytes, validate_target_date


SNAPSHOT_SCHEMA = "PROVISIONAL_DAILY_TOTALS_DK_SNAPSHOT_V1"
POLICY_SCHEMA = "PROVISIONAL_DAILY_TOTALS_OVERLAY_POLICY_V1"
OVERLAY_SCHEMA = "PROVISIONAL_DAILY_TOTALS_OVERLAY_V1"
TARGET_DATE = "2026-09-01"
_STATUS_LABELS = ("RESEARCH ONLY", "ONE-SEASON PROVISIONAL", "NOT BET-VALIDATED")
STATUS_LABELS = _STATUS_LABELS
FIXED_GAME_PKS = (
    822929, 823340, 824391, 824472, 822687, 824716, 823661, 824070,
    824633, 822854, 824148, 824313, 823984, 825038, 823908,
)
_POLICY_ITEMS = (
    ("schema", POLICY_SCHEMA),
    ("required_residual_count", 764),
    ("maximum_quote_age_seconds", 900),
    ("minimum_nonpush_support", 0.80),
    ("minimum_point_gap_runs", 1.00),
    ("minimum_support_margin", 0.05),
)
_POLICY_AUTHORITY = MappingProxyType(dict(_POLICY_ITEMS))
POLICY = _POLICY_AUTHORITY
_SNAPSHOT_KEYS = frozenset({
    "schema", "content_sha256", "target_date", "model_trace_relative",
    "model_trace_sha256", "model_report_relative", "model_report_sha256",
    "comparison_bound_at_utc", "source_name", "book_id", "source_url",
    "capture_method", "captured_at_utc", "raw_capture_relative",
    "raw_capture_sha256", "raw_capture_byte_length", "games",
})
_QUOTED_SNAPSHOT_KEYS = frozenset({
    "game_pk", "status", "draftkings_event_id", "event_label",
    "line_half_runs", "over_american_odds", "under_american_odds",
})
_NO_QUOTE_SNAPSHOT_KEYS = frozenset({"game_pk", "status", "reason"})
_OVERLAY_KEYS = frozenset({
    "schema", "content_sha256", "status_labels", "target_date",
    "model_report_relative", "model_report_sha256", "model_trace_relative",
    "model_trace_sha256", "market_snapshot_relative", "market_snapshot_sha256",
    "policy_schema", "policy_sha256", "evaluated_at_utc", "games",
})
_QUOTED_OVERLAY_KEYS = frozenset({
    "game_pk", "expected_total", "residual_count", "market_status",
    "line_half_runs", "over_american_odds", "under_american_odds",
    "over_support", "under_support", "push_support",
    "over_conditional_support", "under_conditional_support",
    "over_support_margin", "under_support_margin", "selected_side",
    "decision", "reason_codes",
})
_NO_QUOTE_OVERLAY_KEYS = frozenset({
    "game_pk", "expected_total", "residual_count", "market_status",
    "decision", "reason_codes",
})
_REASON_CODES = (
    "NO_QUOTE", "GAME_STARTED", "QUOTE_STALE", "RESIDUAL_COUNT_MISMATCH",
    "NONPUSH_SUPPORT_TOO_LOW", "POINT_GAP_TOO_SMALL", "SUPPORT_MARGIN_TOO_LOW",
    "AMBIGUOUS_DIRECTION",
)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T"
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.[0-9]{0,5}[1-9])?Z$"
)


def _canonical_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        copied = json.loads(canonical_json_bytes(dict(value)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be finite canonical JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{label} must be an object")
    return copied


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} differs")
    return value


def _content_digest(value: Mapping[str, object]) -> str:
    without = dict(value)
    without.pop("content_sha256", None)
    return hashlib.sha256(canonical_json_bytes(without)).hexdigest()


def _relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"{label} differs")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label} differs")
    if PurePosixPath(value).is_absolute():
        raise ValueError(f"{label} differs")
    return value


def _utc_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or _UTC_RE.fullmatch(value) is None:
        raise ValueError(f"{label} differs")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} differs")
    if parsed.microsecond:
        fractional = f"{parsed.microsecond:06d}".rstrip("0")
        canonical = (
            f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"
            f"T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}.{fractional}Z"
        )
    else:
        canonical = (
            f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"
            f"T{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}Z"
        )
    if value != canonical:
        raise ValueError(f"{label} differs")
    return parsed


def _target(value: object, label: str) -> str:
    try:
        result = validate_target_date(value)
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    if result != TARGET_DATE:
        raise ValueError(f"{label} differs")
    return result


def _finite(value: object, label: str, *, low: float = 0.0, high: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{label} is out of range")
    return result


def _source_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("source_url differs")
    if any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("source_url differs")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("source_url differs") from exc
    if (
        parsed.scheme != "https" or hostname != "sportsbook.draftkings.com"
        or parsed.netloc != "sportsbook.draftkings.com"
        or parsed.username is not None or parsed.password is not None
        or port is not None or ":" in parsed.netloc or "#" in value
        or not parsed.netloc
    ):
        raise ValueError("source_url differs")
    return value


def _snapshot_game(value: object) -> dict[str, object]:
    game = _canonical_mapping(value, "snapshot game")
    if type(game.get("game_pk")) is not int or game["game_pk"] not in FIXED_GAME_PKS:
        raise ValueError("snapshot game key differs")
    status = game.get("status")
    if status == "QUOTED":
        if set(game) != _QUOTED_SNAPSHOT_KEYS:
            raise ValueError("quoted snapshot game fields differ")
        for field in ("draftkings_event_id", "event_label"):
            if not isinstance(game[field], str) or not game[field] or len(game[field]) > 512:
                raise ValueError(f"snapshot game {field} differs")
        validate_line_half_runs(game["line_half_runs"])
        validate_american_price(game["over_american_odds"])
        validate_american_price(game["under_american_odds"])
    elif status == "NO_QUOTE":
        reason = game.get("reason")
        if set(game) != _NO_QUOTE_SNAPSHOT_KEYS or not isinstance(reason, str) or reason not in {"NOT_LISTED", "UNAVAILABLE"}:
            raise ValueError("no-quote snapshot game differs")
    else:
        raise ValueError("snapshot game status differs")
    return game


def validate_draftkings_snapshot(value: object) -> dict[str, object]:
    """Validate and defensively copy a closed DraftKings market snapshot."""
    safe = _canonical_mapping(value, "DraftKings snapshot")
    if set(safe) != _SNAPSHOT_KEYS or safe["schema"] != SNAPSHOT_SCHEMA:
        raise ValueError("snapshot fields differ")
    _target(safe["target_date"], "snapshot target date")
    for field in ("model_trace_relative", "model_report_relative", "raw_capture_relative"):
        _relative(safe[field], f"snapshot {field}")
    for field in ("model_trace_sha256", "model_report_sha256", "raw_capture_sha256"):
        _sha(safe[field], f"snapshot {field}")
    comparison = _utc_timestamp(safe["comparison_bound_at_utc"], "snapshot comparison bound")
    captured = _utc_timestamp(safe["captured_at_utc"], "snapshot captured at")
    if captured > comparison:
        raise ValueError("snapshot capture is after comparison bound")
    if safe["source_name"] != "DraftKings" or safe["book_id"] != "DRAFTKINGS" or safe["capture_method"] != "EXTERNAL_MANUAL":
        raise ValueError("snapshot source identity differs")
    _source_url(safe["source_url"])
    if type(safe["raw_capture_byte_length"]) is not int or safe["raw_capture_byte_length"] <= 0:
        raise ValueError("snapshot raw capture byte length differs")
    games = safe["games"]
    if not isinstance(games, list) or len(games) != len(FIXED_GAME_PKS):
        raise ValueError("snapshot games differ")
    validated_games = [_snapshot_game(game) for game in games]
    game_pks = [game["game_pk"] for game in validated_games]
    if len(set(game_pks)) != len(FIXED_GAME_PKS) or set(game_pks) != set(FIXED_GAME_PKS):
        raise ValueError("snapshot game set differs")
    if _sha(safe["content_sha256"], "snapshot content") != _content_digest(safe):
        raise ValueError("snapshot content digest differs")
    return safe


def validate_overlay_policy(value: object) -> dict[str, object]:
    """Validate and defensively copy the immutable V1 overlay policy."""
    safe = _canonical_mapping(value, "overlay policy")
    if set(safe) != {key for key, _ in _POLICY_ITEMS} or safe != _POLICY_AUTHORITY:
        raise ValueError("overlay policy differs")
    for field in ("required_residual_count", "maximum_quote_age_seconds"):
        if type(safe[field]) is not int:
            raise ValueError("overlay policy integer differs")
    for field in ("minimum_nonpush_support", "minimum_point_gap_runs", "minimum_support_margin"):
        _finite(safe[field], f"overlay policy {field}", low=0.0)
    return safe


def _reason_codes(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(code, str) or code not in _REASON_CODES for code in value):
        raise ValueError("overlay reason codes differ")
    if len(set(value)) != len(value):
        raise ValueError("overlay reason codes are duplicated")
    positions = [_REASON_CODES.index(code) for code in value]
    if positions != sorted(positions):
        raise ValueError("overlay reason codes are out of order")
    return value


def _overlay_common_game(game: Mapping[str, object]) -> None:
    game_pk = game.get("game_pk")
    if type(game_pk) is not int or game_pk not in FIXED_GAME_PKS:
        raise ValueError("overlay game key differs")
    if "expected_total" not in game:
        raise ValueError("overlay expected total differs")
    _finite(game["expected_total"], "overlay expected total")
    residual_count = game.get("residual_count")
    if type(residual_count) is not int or residual_count < 0:
        raise ValueError("overlay residual count differs")


def _overlay_game(value: object) -> dict[str, object]:
    game = _canonical_mapping(value, "overlay game")
    status = game.get("market_status")
    _overlay_common_game(game)
    if status == "NO_QUOTE":
        if set(game) != _NO_QUOTE_OVERLAY_KEYS or game["decision"] != "PASS" or game["reason_codes"] != ["NO_QUOTE"]:
            raise ValueError("no-quote overlay game differs")
        return game
    if status != "QUOTED" or set(game) != _QUOTED_OVERLAY_KEYS:
        raise ValueError("quoted overlay game fields differ")
    validate_line_half_runs(game["line_half_runs"])
    validate_american_price(game["over_american_odds"])
    validate_american_price(game["under_american_odds"])
    rates = [_finite(game[field], f"overlay {field}", high=1.0) for field in ("over_support", "under_support", "push_support")]
    if not math.isclose(sum(rates), 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("overlay support rates are incoherent")
    over_result = price_support(rates[0], rates[1], game["over_american_odds"])
    under_result = price_support(rates[1], rates[0], game["under_american_odds"])
    conditionals = ("over_conditional_support", "under_conditional_support")
    margins = ("over_support_margin", "under_support_margin")
    computed = ((over_result.conditional_support, over_result.support_margin), (under_result.conditional_support, under_result.support_margin))
    for (conditional, margin), (computed_conditional, computed_margin) in zip(zip(conditionals, margins), computed):
        c_value, m_value = game[conditional], game[margin]
        if (c_value is None) != (m_value is None):
            raise ValueError("overlay support availability differs")
        if c_value is not None:
            _finite(c_value, f"overlay {conditional}", high=1.0)
            _finite(m_value, f"overlay {margin}", low=-1.0, high=1.0)
        if c_value != computed_conditional or m_value != computed_margin:
            raise ValueError("overlay derived support differs")
    if (rates[0] + rates[1] == 0.0) != (game["over_conditional_support"] is None):
        raise ValueError("overlay conditional support availability differs")
    selected_side = game["selected_side"]
    if selected_side is not None and (not isinstance(selected_side, str) or selected_side not in {"OVER", "UNDER"}):
        raise ValueError("overlay selected side differs")
    decision = game["decision"]
    if not isinstance(decision, str) or decision not in {"RESEARCH_PICK", "PASS"}:
        raise ValueError("overlay decision differs")
    reasons = _reason_codes(game["reason_codes"])
    selected, _ = select_direction(over_result, under_result)
    if selected_side != selected:
        raise ValueError("overlay selected side differs")
    if selected is None and (decision != "PASS" or "AMBIGUOUS_DIRECTION" not in reasons):
        raise ValueError("overlay ambiguous direction differs")
    if selected is not None and "AMBIGUOUS_DIRECTION" in reasons:
        raise ValueError("overlay ambiguous direction differs")
    if decision == "RESEARCH_PICK" and (selected_side not in {"OVER", "UNDER"} or reasons):
        raise ValueError("overlay research pick consistency differs")
    if decision == "PASS" and not reasons:
        raise ValueError("overlay pass reason codes differ")
    if "NO_QUOTE" in reasons:
        raise ValueError("quoted overlay cannot use no-quote reason")
    if "AMBIGUOUS_DIRECTION" in reasons and selected_side is not None:
        raise ValueError("ambiguous overlay selected side differs")
    if selected_side is None and decision == "RESEARCH_PICK":
        raise ValueError("overlay selected side differs")
    nonpush_support = rates[0] + rates[1]
    point_gap = abs(float(game["expected_total"]) - game["line_half_runs"] / 2.0)
    selected_margin = over_result.support_margin if selected == "OVER" else under_result.support_margin
    if decision == "RESEARCH_PICK":
        if game["residual_count"] != _POLICY_AUTHORITY["required_residual_count"]:
            raise ValueError("overlay residual count gate differs")
        if nonpush_support < _POLICY_AUTHORITY["minimum_nonpush_support"]:
            raise ValueError("overlay nonpush support gate differs")
        if point_gap < _POLICY_AUTHORITY["minimum_point_gap_runs"]:
            raise ValueError("overlay point gap gate differs")
        if selected_margin is None or selected_margin < _POLICY_AUTHORITY["minimum_support_margin"]:
            raise ValueError("overlay support margin gate differs")
    else:
        contradictions = set()
        if game["residual_count"] == _POLICY_AUTHORITY["required_residual_count"]:
            contradictions.add("RESIDUAL_COUNT_MISMATCH")
        if nonpush_support >= _POLICY_AUTHORITY["minimum_nonpush_support"]:
            contradictions.add("NONPUSH_SUPPORT_TOO_LOW")
        if point_gap >= _POLICY_AUTHORITY["minimum_point_gap_runs"]:
            contradictions.add("POINT_GAP_TOO_SMALL")
        if selected_margin is not None and selected_margin >= _POLICY_AUTHORITY["minimum_support_margin"]:
            contradictions.add("SUPPORT_MARGIN_TOO_LOW")
        if selected is not None:
            contradictions.add("AMBIGUOUS_DIRECTION")
        if contradictions.intersection(reasons):
            raise ValueError("overlay reason codes contradict derived facts")
    return game


def validate_overlay_report(value: object) -> dict[str, object]:
    """Validate and defensively copy the closed V1 overlay report."""
    safe = _canonical_mapping(value, "overlay report")
    if set(safe) != _OVERLAY_KEYS or safe["schema"] != OVERLAY_SCHEMA:
        raise ValueError("overlay report fields differ")
    if safe["status_labels"] != list(_STATUS_LABELS):
        raise ValueError("overlay status labels differ")
    _target(safe["target_date"], "overlay target date")
    for field in ("model_report_relative", "model_trace_relative", "market_snapshot_relative"):
        _relative(safe[field], f"overlay {field}")
    for field in ("model_report_sha256", "model_trace_sha256", "market_snapshot_sha256", "policy_sha256"):
        _sha(safe[field], f"overlay {field}")
    if safe["policy_schema"] != POLICY_SCHEMA or safe["policy_sha256"] != _content_digest(_POLICY_AUTHORITY):
        raise ValueError("overlay policy binding differs")
    _utc_timestamp(safe["evaluated_at_utc"], "overlay evaluated at")
    games = safe["games"]
    if not isinstance(games, list) or len(games) != len(FIXED_GAME_PKS):
        raise ValueError("overlay games differ")
    validated_games = [_overlay_game(game) for game in games]
    if [game["game_pk"] for game in validated_games] != list(FIXED_GAME_PKS):
        raise ValueError("overlay game ordering differs")
    if _sha(safe["content_sha256"], "overlay content") != _content_digest(safe):
        raise ValueError("overlay content digest differs")
    return safe


@dataclass(frozen=True, slots=True)
class SupportRates:
    """Unconditional over, under, and push rates for a totals sample."""

    over: float
    under: float
    push: float


@dataclass(frozen=True, slots=True)
class PriceSupport:
    """Conditional support and its margin over a quoted break-even rate."""

    conditional_support: float | None
    quoted_break_even: float
    support_margin: float | None


def validate_american_price(value: object) -> int:
    """Validate and return a DraftKings-style American price."""

    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not (-10_000 <= value <= -100 or 100 <= value <= 10_000)
    ):
        raise ValueError("american price must be an integer from -10000 to -100 or 100 to 10000")
    return value


def validate_line_half_runs(value: object) -> int:
    """Validate and return a positive integer number of half-run ticks."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("line_half_runs must be a positive integer")
    return value


def american_net_profit(price: int) -> float:
    """Return net profit on a one-unit stake for an American price."""

    price = validate_american_price(price)
    if price > 0:
        return price / 100.0
    return 100.0 / abs(price)


def quoted_break_even(price: int) -> float:
    """Return the implied break-even probability for an American price."""

    return 1.0 / (1.0 + american_net_profit(price))


def compare_sample_support(
    samples: tuple[int, ...], line_half_runs: int
) -> SupportRates:
    """Classify sample totals against a half-run encoded totals line."""

    if not isinstance(samples, tuple) or not samples:
        raise ValueError("samples must be a non-empty tuple")
    for sample in samples:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise ValueError("samples must contain nonnegative integers")
    line_half_runs = validate_line_half_runs(line_half_runs)

    over = under = push = 0
    for sample in samples:
        doubled_sample = sample * 2
        if doubled_sample > line_half_runs:
            over += 1
        elif doubled_sample < line_half_runs:
            under += 1
        else:
            push += 1

    count = len(samples)
    return SupportRates(over / count, under / count, push / count)


def _finite_probability(value: object, label: str) -> Real:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite probability")
    if value < 0 or value > 1:
        raise ValueError(f"{label} must be a finite probability between 0 and 1")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite probability") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite probability between 0 and 1")
    return value


def _exact_probability(value: Real) -> Fraction:
    """Represent a validated Real without losing its support value."""

    return Fraction(value)


def price_support(win_support: float, loss_support: float, price: int) -> PriceSupport:
    """Compare conditional directional support with the quoted price."""

    win_support = _finite_probability(win_support, "win_support")
    loss_support = _finite_probability(loss_support, "loss_support")
    use_exact_arithmetic = isinstance(win_support, Rational) or isinstance(loss_support, Rational)
    if use_exact_arithmetic:
        exact_win = _exact_probability(win_support)
        exact_loss = _exact_probability(loss_support)
        directional_support = exact_win + exact_loss
    else:
        exact_win = None
        directional_support = win_support + loss_support
    if directional_support > 1:
        raise ValueError("win_support and loss_support must sum to at most 1")
    price = validate_american_price(price)
    break_even = quoted_break_even(price)

    if directional_support == 0.0:
        return PriceSupport(None, break_even, None)
    if use_exact_arithmetic:
        conditional = float(exact_win / directional_support)
    else:
        conditional = float(win_support / directional_support)
    if not math.isfinite(conditional):
        raise ValueError("conditional support must be finite")
    return PriceSupport(conditional, break_even, conditional - break_even)


def select_direction(
    over: PriceSupport, under: PriceSupport
) -> tuple[str | None, tuple[str, ...]]:
    """Choose the uniquely better-supported side, if one exists."""

    over_margin = over.support_margin
    under_margin = under.support_margin
    if over_margin is not None and (under_margin is None or over_margin > under_margin):
        return "OVER", ()
    if under_margin is not None and (over_margin is None or under_margin > over_margin):
        return "UNDER", ()
    return None, ("AMBIGUOUS_DIRECTION",)
