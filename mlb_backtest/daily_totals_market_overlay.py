"""Pure arithmetic contracts for the DraftKings totals market overlay."""

from __future__ import annotations

import math
import re
import json
import hashlib
from dataclasses import dataclass
from fractions import Fraction
from numbers import Rational, Real
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import urlsplit

from .live_top3_contracts import canonical_json_bytes, validate_target_date
from .daily_totals import (
    REPORT_SCHEMA,
    validate_totals_model_receipt,
    validate_totals_report,
    validate_totals_trace,
)
from .secure_fs import (
    RegularFileSnapshot,
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


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
_MODEL_REPORT_ROOT = Path("reports/experimental_daily_totals")
_MODEL_RECEIPT_ROOT = Path("audit/experimental_daily_totals/model")
_RAW_ROOT = Path("data/experimental_daily_totals_market/raw")
_SNAPSHOT_ROOT = Path("audit/experimental_daily_totals_market/draftkings")
_OVERLAY_ROOT = Path("reports/experimental_daily_totals_overlay")
_OVERLAY_TRACE_SCHEMA = "PROVISIONAL_DAILY_TOTALS_OVERLAY_TRACE_V1"
_OVERLAY_TRACE_KEYS = frozenset({
    "schema", "content_sha256", "target_date", "evaluated_at_utc",
    "model_report_relative", "model_report_sha256", "model_report_sidecar_sha256",
    "model_trace_relative", "model_trace_sha256", "model_receipt_relative",
    "model_receipt_sha256", "model_receipt_sidecar_sha256",
    "market_snapshot_relative", "market_snapshot_sha256", "market_snapshot_sidecar_sha256",
    "policy_schema", "policy_sha256", "report_relative", "report_sha256",
    "report_sidecar_sha256", "markdown_sha256",
})
_SIDECAR_MAX_BYTES = 65
_RAW_MAX_BYTES = 256 * 1024
_SNAPSHOT_MAX_BYTES = 256 * 1024
_MODEL_MAX_BYTES = 2 * 1024 * 1024
_TRACE_MAX_BYTES = 256 * 1024
_MARKDOWN_MAX_BYTES = 256 * 1024


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
        derived: list[str] = []
        if game["residual_count"] != _POLICY_AUTHORITY["required_residual_count"]:
            derived.append("RESIDUAL_COUNT_MISMATCH")
        if nonpush_support < _POLICY_AUTHORITY["minimum_nonpush_support"]:
            derived.append("NONPUSH_SUPPORT_TOO_LOW")
        if point_gap < _POLICY_AUTHORITY["minimum_point_gap_runs"]:
            derived.append("POINT_GAP_TOO_SMALL")
        if selected_margin is None or selected_margin < _POLICY_AUTHORITY["minimum_support_margin"]:
            derived.append("SUPPORT_MARGIN_TOO_LOW")
        if selected is None:
            derived.append("AMBIGUOUS_DIRECTION")
        non_temporal = [reason for reason in reasons if reason not in {"GAME_STARTED", "QUOTE_STALE"}]
        if non_temporal != derived:
            raise ValueError("overlay reason codes differ from derived facts")
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


def _sha_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _sidecar(contents: bytes) -> bytes:
    return f"{_sha_bytes(contents)}\n".encode("ascii")


def _path(value: Path, label: str) -> Path:
    """Accept only a lexical root-relative path suitable for ``secure_fs``."""
    if not isinstance(value, Path) or value.is_absolute() or "\\" in str(value):
        raise ValueError(f"{label} differs")
    if not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise ValueError(f"{label} differs")
    return value


def _under(relative: Path, parent: Path, label: str) -> Path:
    relative = _path(relative, label)
    if relative.parts[:len(parent.parts)] != parent.parts:
        raise ValueError(f"{label} differs")
    return relative


def _canonical_json(snapshot: RegularFileSnapshot, label: str) -> dict[str, object]:
    try:
        value = json.loads(snapshot.contents)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} differs") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != snapshot.contents:
        raise ValueError(f"{label} differs")
    return value


def _verify_sidecar(snapshot: RegularFileSnapshot, primary: RegularFileSnapshot, label: str) -> None:
    if snapshot.contents != _sidecar(primary.contents):
        raise ValueError(f"{label} differs")


def _existing_or_write(
    trusted: TrustedRoot, relative: Path, contents: bytes, label: str, *, rebind: Callable[[], None] | None = None,
) -> RegularFileSnapshot:
    existing = read_regular_snapshot(trusted, relative, label, max_bytes=len(contents), missing_ok=True)
    if existing is not None:
        if existing.contents != contents:
            raise FileExistsError(f"{label} identity conflicts")
        return existing
    return write_exclusive(trusted, relative, contents, label, final_rebind=rebind)


def _clock(value: object, label: str) -> tuple[datetime, str]:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} differs")
    normalized = value.astimezone(timezone.utc)
    if normalized.microsecond:
        text = normalized.strftime("%Y-%m-%dT%H:%M:%S.") + f"{normalized.microsecond:06d}".rstrip("0") + "Z"
    else:
        text = normalized.strftime("%Y-%m-%dT%H:%M:%SZ")
    return normalized, text


def _frozen(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _frozen(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ModelAuthority:
    """Fully rebound report/trace/receipt evidence used by the overlay."""

    report: Mapping[str, object]
    receipt: Mapping[str, object]
    trace: Mapping[str, object]
    snapshots: tuple[RegularFileSnapshot, ...]


@dataclass(frozen=True, slots=True)
class BuiltOverlay:
    """Immutable validated overlay and all evidence required for trace commit."""

    report: Mapping[str, object]
    report_bytes: bytes
    markdown_bytes: bytes
    trace_bytes: bytes
    input_snapshots: tuple[RegularFileSnapshot, ...]
    report_relative: Path
    report_sidecar_relative: Path
    markdown_relative: Path
    trace_relative: Path


def _rebind_all(trusted: TrustedRoot, snapshots: tuple[RegularFileSnapshot, ...], label: str) -> None:
    for snapshot in snapshots:
        rebind_regular_snapshot(trusted, snapshot, f"{label}: {snapshot.relative.as_posix()}", max_bytes=_read_cap(snapshot.relative))


def _read_cap(relative: Path) -> int:
    """Bound every untrusted evidence read before parsing or comparison."""
    if relative.suffix == ".sha256":
        return _SIDECAR_MAX_BYTES
    if relative.parts[:len(_RAW_ROOT.parts)] == _RAW_ROOT.parts:
        return _RAW_MAX_BYTES
    if relative.parts[:len(_SNAPSHOT_ROOT.parts)] == _SNAPSHOT_ROOT.parts:
        return _SNAPSHOT_MAX_BYTES
    if relative.name.endswith(".trace.json"):
        return _TRACE_MAX_BYTES
    if relative.suffix == ".md":
        return _MARKDOWN_MAX_BYTES
    return _MODEL_MAX_BYTES


def _read_evidence(trusted: TrustedRoot, relative: Path, label: str) -> RegularFileSnapshot:
    snapshot = read_regular_snapshot(trusted, relative, label, max_bytes=_read_cap(relative))
    if snapshot is None:
        raise ValueError(f"{label} differs")
    return snapshot


def _fixed_model_gates(report: Mapping[str, object], receipt: Mapping[str, object]) -> None:
    if (
        report["schema"] != REPORT_SCHEMA
        or report["target_date"] != TARGET_DATE
        or report["cutoff_date"] != "2026-08-31"
        or report["selected_candidate"] != "TEAM_VENUE_30"
        or receipt["target_date"] != TARGET_DATE
        or receipt["cutoff_date"] != "2026-08-31"
        or receipt["selected_candidate"] != "TEAM_VENUE_30"
    ):
        raise ValueError("fixed model authority differs")
    games = report["games"]
    if not isinstance(games, list) or len(games) != len(FIXED_GAME_PKS):
        raise ValueError("fixed model games differ")
    if [game.get("game_pk") for game in games if isinstance(game, Mapping)] != list(FIXED_GAME_PKS):
        raise ValueError("fixed model game ordering differs")
    for game in games:
        if not isinstance(game, Mapping) or game.get("status") != "PROJECTED":
            raise ValueError("fixed model game projection differs")
        samples = game.get("residual_samples")
        if (
            not isinstance(samples, list) or game.get("residual_count") != 764
            or len(samples) != 764
            or game.get("residual_sha256") != _sha_bytes(canonical_json_bytes(samples))
        ):
            raise ValueError("fixed model residual evidence differs")


def load_and_validate_model_authority(root: Path, model_trace: Path) -> ModelAuthority:
    """Securely load the sealed model chain without invoking the model pipeline."""
    root_path = Path(root)
    trace_relative = _under(model_trace, _MODEL_REPORT_ROOT, "model trace path")
    if not trace_relative.name.endswith(".trace.json"):
        raise ValueError("model trace path differs")
    with open_trusted_root(root_path) as trusted:
        trace_snapshot = _read_evidence(trusted, trace_relative, "model trace")
        trace = validate_totals_trace(_canonical_json(trace_snapshot, "model trace"))
        report_relative = _under(Path(str(trace["report_relative"])), _MODEL_REPORT_ROOT, "model report path")
        receipt_relative = _under(Path(str(trace["model_receipt_relative"])), _MODEL_RECEIPT_ROOT, "model receipt path")
        if receipt_relative != _MODEL_RECEIPT_ROOT / f"{Path(receipt_relative).stem}.json":
            raise ValueError("model receipt path differs")
        report_snapshot = _read_evidence(trusted, report_relative, "model report")
        report_sidecar = _read_evidence(trusted, report_relative.with_suffix(".sha256"), "model report sidecar")
        receipt_snapshot = _read_evidence(trusted, receipt_relative, "model receipt")
        receipt_sidecar = _read_evidence(trusted, receipt_relative.with_suffix(".sha256"), "model receipt sidecar")
        report = validate_totals_report(_canonical_json(report_snapshot, "model report"))
        receipt = validate_totals_model_receipt(_canonical_json(receipt_snapshot, "model receipt"))
        _verify_sidecar(report_sidecar, report_snapshot, "model report sidecar")
        _verify_sidecar(receipt_sidecar, receipt_snapshot, "model receipt sidecar")
        if (
            trace["report_relative"] != report_relative.as_posix()
            or trace["report_sha256"] != _sha_bytes(report_snapshot.contents)
            or trace["report_sidecar_sha256"] != _sha_bytes(report_sidecar.contents)
            or trace["model_receipt_relative"] != receipt_relative.as_posix()
            or trace["model_receipt_sha256"] != _sha_bytes(receipt_snapshot.contents)
            or report["model_receipt_relative"] != receipt_relative.as_posix()
            or report["model_receipt_sha256"] != _sha_bytes(receipt_snapshot.contents)
            or receipt_relative.name != f"{receipt['content_sha256']}.json"
        ):
            raise ValueError("model authority binding differs")
        _fixed_model_gates(report, receipt)
        snapshots = (trace_snapshot, report_snapshot, report_sidecar, receipt_snapshot, receipt_sidecar)
        _rebind_all(trusted, snapshots, "model authority")
        trusted.assert_bound()
    return ModelAuthority(_frozen(report), _frozen(receipt), _frozen(trace), snapshots)


def _raw_capture(
    trusted: TrustedRoot, raw_capture: Path, label: str,
) -> tuple[RegularFileSnapshot, RegularFileSnapshot, dict[str, object]]:
    raw_relative = _under(raw_capture, _RAW_ROOT, "raw capture path")
    if raw_relative.parent != _RAW_ROOT or not re.fullmatch(r"[0-9a-f]{64}\.json", raw_relative.name):
        raise ValueError("raw capture path differs")
    raw = _read_evidence(trusted, raw_relative, label)
    sidecar = _read_evidence(trusted, raw_relative.with_suffix(".sha256"), f"{label} sidecar")
    digest = _sha_bytes(raw.contents)
    if raw_relative.stem != digest or sidecar.contents != f"{digest}\n".encode("ascii"):
        raise ValueError("raw capture digest differs")
    try:
        value = json.loads(raw.contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("raw capture differs") from exc
    if not isinstance(value, dict) or set(value) != {"captured_at_utc", "source_url", "games"}:
        raise ValueError("raw capture fields differ")
    _utc_timestamp(value["captured_at_utc"], "raw capture timestamp")
    _source_url(value["source_url"])
    games = value["games"]
    if not isinstance(games, list) or len(games) != len(FIXED_GAME_PKS):
        raise ValueError("raw capture games differ")
    parsed = [_snapshot_game(game) for game in games]
    if [game["game_pk"] for game in parsed] != list(FIXED_GAME_PKS):
        raise ValueError("raw capture game ordering differs")
    return raw, sidecar, {"captured_at_utc": value["captured_at_utc"], "source_url": value["source_url"], "games": parsed}


def admit_draftkings_snapshot(
    root: Path, model_trace: Path, raw_capture: Path, *, comparison_clock: Callable[[], datetime],
) -> Path:
    """Admit one manually captured DraftKings file into an append-only snapshot."""
    authority = load_and_validate_model_authority(root, model_trace)
    root_path = Path(root)
    with open_trusted_root(root_path) as trusted:
        _rebind_all(trusted, authority.snapshots, "model authority")
        raw, raw_sidecar, payload = _raw_capture(trusted, raw_capture, "raw capture")
        # The clock is deliberately the final operation before snapshot assembly.
        comparison, comparison_text = _clock(comparison_clock(), "comparison clock")
        captured = _utc_timestamp(payload["captured_at_utc"], "raw capture timestamp")
        if captured > comparison:
            raise ValueError("raw capture is after comparison bound")
        report_games = authority.report["games"]
        if not isinstance(report_games, tuple):
            raise ValueError("model authority games differ")
        if any(captured >= _utc_timestamp(game["scheduled_start_utc"], "model start") for game in report_games if isinstance(game, Mapping)):
            raise ValueError("raw capture is after scheduled start")
        report_snapshot = authority.snapshots[1]
        trace_snapshot = authority.snapshots[0]
        snapshot: dict[str, object] = {
            "schema": SNAPSHOT_SCHEMA, "content_sha256": "", "target_date": TARGET_DATE,
            "model_trace_relative": trace_snapshot.relative.as_posix(), "model_trace_sha256": _sha_bytes(trace_snapshot.contents),
            "model_report_relative": report_snapshot.relative.as_posix(), "model_report_sha256": _sha_bytes(report_snapshot.contents),
            "comparison_bound_at_utc": comparison_text, "source_name": "DraftKings", "book_id": "DRAFTKINGS",
            "source_url": payload["source_url"], "capture_method": "EXTERNAL_MANUAL", "captured_at_utc": payload["captured_at_utc"],
            "raw_capture_relative": raw.relative.as_posix(), "raw_capture_sha256": _sha_bytes(raw.contents),
            "raw_capture_byte_length": len(raw.contents), "games": payload["games"],
        }
        snapshot["content_sha256"] = _content_digest(snapshot)
        snapshot = validate_draftkings_snapshot(snapshot)
        contents = canonical_json_bytes(snapshot)
        relative = _SNAPSHOT_ROOT / f"{snapshot['content_sha256']}.json"
        sidecar_contents = _sidecar(contents)
        inputs = authority.snapshots + (raw, raw_sidecar)
        _rebind_all(trusted, inputs, "snapshot admission inputs")
        sidecar = _existing_or_write(trusted, relative.with_suffix(".sha256"), sidecar_contents, "market snapshot sidecar", rebind=lambda: _rebind_all(trusted, inputs, "snapshot admission inputs"))
        output = _existing_or_write(trusted, relative, contents, "market snapshot", rebind=lambda: _rebind_all(trusted, inputs + (sidecar,), "snapshot admission inputs"))
        _rebind_all(trusted, inputs + (sidecar, output), "snapshot admission")
        trusted.assert_bound()
    return relative


def _load_snapshot(
    trusted: TrustedRoot, market_snapshot: Path, authority: ModelAuthority,
) -> tuple[RegularFileSnapshot, RegularFileSnapshot, dict[str, object], tuple[RegularFileSnapshot, ...]]:
    relative = _under(market_snapshot, _SNAPSHOT_ROOT, "market snapshot path")
    if relative.parent != _SNAPSHOT_ROOT or not re.fullmatch(r"[0-9a-f]{64}\.json", relative.name):
        raise ValueError("market snapshot path differs")
    primary = _read_evidence(trusted, relative, "market snapshot")
    sidecar = _read_evidence(trusted, relative.with_suffix(".sha256"), "market snapshot sidecar")
    value = validate_draftkings_snapshot(_canonical_json(primary, "market snapshot"))
    _verify_sidecar(sidecar, primary, "market snapshot sidecar")
    if relative.stem != value["content_sha256"] or value["content_sha256"] != _sha_bytes(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"})):
        raise ValueError("market snapshot content path differs")
    if (
        value["model_trace_relative"] != authority.snapshots[0].relative.as_posix()
        or value["model_trace_sha256"] != _sha_bytes(authority.snapshots[0].contents)
        or value["model_report_relative"] != authority.snapshots[1].relative.as_posix()
        or value["model_report_sha256"] != _sha_bytes(authority.snapshots[1].contents)
    ):
        raise ValueError("market snapshot model binding differs")
    raw, raw_sidecar, raw_payload = _raw_capture(trusted, Path(str(value["raw_capture_relative"])), "raw capture")
    if value["raw_capture_sha256"] != _sha_bytes(raw.contents) or value["raw_capture_byte_length"] != len(raw.contents):
        raise ValueError("market snapshot raw binding differs")
    if raw_payload["captured_at_utc"] != value["captured_at_utc"] or raw_payload["source_url"] != value["source_url"] or raw_payload["games"] != value["games"]:
        raise ValueError("market snapshot raw content differs")
    return primary, sidecar, value, (raw, raw_sidecar)


def _overlay_trace(value: object) -> dict[str, object]:
    safe = _canonical_mapping(value, "overlay trace")
    if set(safe) != _OVERLAY_TRACE_KEYS or safe["schema"] != _OVERLAY_TRACE_SCHEMA:
        raise ValueError("overlay trace fields differ")
    _target(safe["target_date"], "overlay trace target date")
    _utc_timestamp(safe["evaluated_at_utc"], "overlay trace evaluated time")
    for field in ("model_report_relative", "model_trace_relative", "model_receipt_relative", "market_snapshot_relative", "report_relative"):
        _relative(safe[field], f"overlay trace {field}")
    for field in _OVERLAY_TRACE_KEYS:
        if field.endswith("sha256") or field == "content_sha256":
            _sha(safe[field], f"overlay trace {field}")
    if safe["policy_schema"] != POLICY_SCHEMA or safe["policy_sha256"] != _content_digest(_POLICY_AUTHORITY):
        raise ValueError("overlay trace policy differs")
    if safe["content_sha256"] != _content_digest(safe):
        raise ValueError("overlay trace content digest differs")
    return safe


def _render_overlay_markdown(report: Mapping[str, object]) -> bytes:
    """Render the sealed research record without outcome or return claims."""
    lines = [
        "# Provisional Daily Totals Overlay",
        "",
        "> RESEARCH ONLY · ONE-SEASON PROVISIONAL · NOT BET-VALIDATED",
        "",
        f"- Target date: {report['target_date']}",
        f"- Evaluated at: {report['evaluated_at_utc']}",
        f"- Overlay content SHA-256: {report['content_sha256']}",
        "",
        "| Game | Market status | Research disposition | Reasons |",
        "| --- | --- | --- | --- |",
    ]
    games = report["games"]
    if not isinstance(games, list):
        raise ValueError("overlay report games differ")
    for game in games:
        if not isinstance(game, Mapping):
            raise ValueError("overlay report game differs")
        reasons = ", ".join(str(reason) for reason in game["reason_codes"])
        lines.append(
            f"| {game['game_pk']} | {game['market_status']} | {game['decision']} | {reasons} |"
        )
    lines.extend(["", "This is a research record, not financial advice.", ""])
    return "\n".join(lines).encode("utf-8")


def build_overlay(
    root: Path, model_trace: Path, market_snapshot: Path, *, evaluated_clock: Callable[[], datetime],
) -> BuiltOverlay:
    """Build a deterministic validated overlay; persistence is a separate commit step."""
    authority = load_and_validate_model_authority(root, model_trace)
    root_path = Path(root)
    with open_trusted_root(root_path) as trusted:
        _rebind_all(trusted, authority.snapshots, "model authority")
        snapshot_file, snapshot_sidecar, snapshot, raw_snapshots = _load_snapshot(trusted, market_snapshot, authority)
        captured = _utc_timestamp(snapshot["captured_at_utc"], "market snapshot capture")
        comparison = _utc_timestamp(snapshot["comparison_bound_at_utc"], "market snapshot comparison")
        report_games = authority.report["games"]
        if not isinstance(report_games, tuple):
            raise ValueError("model authority games differ")
        if captured > comparison or any(captured >= _utc_timestamp(game["scheduled_start_utc"], "model start") for game in report_games if isinstance(game, Mapping)):
            raise ValueError("market snapshot capture ordering differs")
        evaluated, evaluated_text = _clock(evaluated_clock(), "evaluated clock")
        if evaluated < comparison:
            raise ValueError("evaluated clock is before comparison bound")
        snapshot_rows = snapshot["games"]
        if not isinstance(snapshot_rows, list):
            raise ValueError("market snapshot games differ")
        by_game = {row["game_pk"]: row for row in snapshot_rows if isinstance(row, dict)}
        rows: list[dict[str, object]] = []
        for model_game in report_games:
            if not isinstance(model_game, Mapping):
                raise ValueError("model authority game differs")
            pk = model_game["game_pk"]
            market = by_game.get(pk)
            if market is None:
                raise ValueError("market snapshot game differs")
            base = {"game_pk": pk, "expected_total": model_game["expected_total"], "residual_count": model_game["residual_count"], "market_status": market["status"]}
            if market["status"] == "NO_QUOTE":
                rows.append({**base, "decision": "PASS", "reason_codes": ["NO_QUOTE"]})
                continue
            samples = model_game["residual_samples"]
            if not isinstance(samples, tuple) or model_game["residual_sha256"] != _sha_bytes(canonical_json_bytes(samples)):
                raise ValueError("model residual evidence differs")
            rates = compare_sample_support(tuple(samples), market["line_half_runs"])
            over = price_support(rates.over, rates.under, market["over_american_odds"])
            under = price_support(rates.under, rates.over, market["under_american_odds"])
            selected, _ = select_direction(over, under)
            reasons: list[str] = []
            start = _utc_timestamp(model_game["scheduled_start_utc"], "model start")
            if evaluated >= start:
                reasons.append("GAME_STARTED")
            age = (evaluated - captured).total_seconds()
            if age < 0:
                raise ValueError("quote age differs")
            if age > _POLICY_AUTHORITY["maximum_quote_age_seconds"]:
                reasons.append("QUOTE_STALE")
            if model_game["residual_count"] != _POLICY_AUTHORITY["required_residual_count"] or len(samples) != _POLICY_AUTHORITY["required_residual_count"]:
                reasons.append("RESIDUAL_COUNT_MISMATCH")
            if rates.over + rates.under < _POLICY_AUTHORITY["minimum_nonpush_support"]:
                reasons.append("NONPUSH_SUPPORT_TOO_LOW")
            if abs(float(model_game["expected_total"]) - market["line_half_runs"] / 2.0) < _POLICY_AUTHORITY["minimum_point_gap_runs"]:
                reasons.append("POINT_GAP_TOO_SMALL")
            selected_margin = over.support_margin if selected == "OVER" else under.support_margin if selected == "UNDER" else None
            if selected_margin is None or selected_margin < _POLICY_AUTHORITY["minimum_support_margin"]:
                reasons.append("SUPPORT_MARGIN_TOO_LOW")
            if selected is None:
                reasons.append("AMBIGUOUS_DIRECTION")
            row = {**base, "line_half_runs": market["line_half_runs"], "over_american_odds": market["over_american_odds"], "under_american_odds": market["under_american_odds"], "over_support": rates.over, "under_support": rates.under, "push_support": rates.push, "over_conditional_support": over.conditional_support, "under_conditional_support": under.conditional_support, "over_support_margin": over.support_margin, "under_support_margin": under.support_margin, "selected_side": selected, "decision": "RESEARCH_PICK" if not reasons and selected is not None else "PASS", "reason_codes": reasons}
            rows.append(row)
        report_file = _OVERLAY_ROOT / TARGET_DATE / f"{evaluated.strftime('%Y%m%dT%H%M%S%fZ')}-"
        report: dict[str, object] = {"schema": OVERLAY_SCHEMA, "content_sha256": "", "status_labels": list(STATUS_LABELS), "target_date": TARGET_DATE, "model_report_relative": authority.snapshots[1].relative.as_posix(), "model_report_sha256": _sha_bytes(authority.snapshots[1].contents), "model_trace_relative": authority.snapshots[0].relative.as_posix(), "model_trace_sha256": _sha_bytes(authority.snapshots[0].contents), "market_snapshot_relative": snapshot_file.relative.as_posix(), "market_snapshot_sha256": _sha_bytes(snapshot_file.contents), "policy_schema": POLICY_SCHEMA, "policy_sha256": _content_digest(_POLICY_AUTHORITY), "evaluated_at_utc": evaluated_text, "games": rows}
        report["content_sha256"] = _content_digest(report)
        report = validate_overlay_report(report)
        report_bytes = canonical_json_bytes(report)
        report_relative = Path(str(report_file) + str(report["content_sha256"])[:16] + ".json")
        markdown = _render_overlay_markdown(report)
        trace_payload: dict[str, object] = {"schema": _OVERLAY_TRACE_SCHEMA, "content_sha256": "", "target_date": TARGET_DATE, "evaluated_at_utc": evaluated_text, "model_report_relative": authority.snapshots[1].relative.as_posix(), "model_report_sha256": _sha_bytes(authority.snapshots[1].contents), "model_report_sidecar_sha256": _sha_bytes(authority.snapshots[2].contents), "model_trace_relative": authority.snapshots[0].relative.as_posix(), "model_trace_sha256": _sha_bytes(authority.snapshots[0].contents), "model_receipt_relative": authority.snapshots[3].relative.as_posix(), "model_receipt_sha256": _sha_bytes(authority.snapshots[3].contents), "model_receipt_sidecar_sha256": _sha_bytes(authority.snapshots[4].contents), "market_snapshot_relative": snapshot_file.relative.as_posix(), "market_snapshot_sha256": _sha_bytes(snapshot_file.contents), "market_snapshot_sidecar_sha256": _sha_bytes(snapshot_sidecar.contents), "policy_schema": POLICY_SCHEMA, "policy_sha256": _content_digest(_POLICY_AUTHORITY), "report_relative": report_relative.as_posix(), "report_sha256": _sha_bytes(report_bytes), "report_sidecar_sha256": _sha_bytes(_sidecar(report_bytes)), "markdown_sha256": _sha_bytes(markdown)}
        trace_payload["content_sha256"] = _content_digest(trace_payload)
        trace_bytes = canonical_json_bytes(_overlay_trace(trace_payload))
        inputs = authority.snapshots + (snapshot_file, snapshot_sidecar) + raw_snapshots
        _rebind_all(trusted, inputs, "overlay build")
        trusted.assert_bound()
    return BuiltOverlay(_frozen(report), report_bytes, markdown, trace_bytes, inputs, report_relative, report_relative.with_suffix(".sha256"), report_relative.with_suffix(".md"), report_relative.with_suffix(".trace.json"))


def persist_overlay(root: Path, built: BuiltOverlay) -> tuple[Path, Path, Path, Path]:
    """Append sidecar, JSON, Markdown, then trace; trace publication is the commit."""
    if not isinstance(built, BuiltOverlay):
        raise ValueError("built overlay differs")
    try:
        report = validate_overlay_report(json.loads(built.report_bytes))
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("built overlay report differs") from exc
    if canonical_json_bytes(report) != built.report_bytes or _frozen(report) != built.report:
        raise ValueError("built overlay report differs")
    try:
        model_trace = _path(Path(str(report["model_trace_relative"])), "built model trace path")
        market_snapshot = _path(Path(str(report["market_snapshot_relative"])), "built market snapshot path")
        evaluated, _ = _clock(
            _utc_timestamp(report["evaluated_at_utc"], "built evaluated time"),
            "built evaluated time",
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("built overlay input bindings differ") from exc
    root_path = Path(root)
    # The public dataclass is deliberately treated as an untrusted transport.
    # Rebuild its entire authority chain from filesystem evidence before writing.
    expected = build_overlay(
        root_path,
        model_trace,
        market_snapshot,
        evaluated_clock=lambda: evaluated,
    )
    if built != expected:
        raise ValueError("built overlay differs from rebound authority")
    built = expected
    trace = _overlay_trace(json.loads(built.trace_bytes))
    with open_trusted_root(root_path) as trusted:
        _rebind_all(trusted, built.input_snapshots, "overlay persistence inputs")
        sidecar = _existing_or_write(trusted, built.report_sidecar_relative, _sidecar(built.report_bytes), "overlay report sidecar", rebind=lambda: _rebind_all(trusted, built.input_snapshots, "overlay persistence inputs"))
        primary = _existing_or_write(trusted, built.report_relative, built.report_bytes, "overlay report", rebind=lambda: _rebind_all(trusted, built.input_snapshots + (sidecar,), "overlay persistence inputs"))
        markdown = _existing_or_write(trusted, built.markdown_relative, built.markdown_bytes, "overlay Markdown", rebind=lambda: _rebind_all(trusted, built.input_snapshots + (sidecar, primary), "overlay persistence inputs"))
        before_trace = built.input_snapshots + (sidecar, primary, markdown)
        _rebind_all(trusted, before_trace, "overlay pre-trace")
        trace_snapshot = _existing_or_write(trusted, built.trace_relative, built.trace_bytes, "overlay trace", rebind=lambda: _rebind_all(trusted, before_trace, "overlay pre-trace"))
        _rebind_all(trusted, before_trace + (trace_snapshot,), "overlay committed")
        persisted = _overlay_trace(_canonical_json(trace_snapshot, "overlay trace"))
        if persisted != trace:
            raise ValueError("persisted overlay trace differs")
        # Rebind after parsing the commit too: parsing is not a stability proof.
        _rebind_all(trusted, before_trace + (trace_snapshot,), "overlay post-trace")
        trusted.assert_bound()
    return built.report_sidecar_relative, built.report_relative, built.markdown_relative, built.trace_relative
