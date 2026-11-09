"""Pure multi-book, multi-line total-runs PLAY/PASS evaluation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .market_play_authorities import ParticipantGateEvidence, TotalsAuthority, TotalsGameAuthority

from .market_play_contracts import (
    EXPECTED_BOOKS,
    REASON_ORDER,
    TotalQuote,
    canonical_utc_ns,
    quoted_break_even_fraction,
)
from .multibook_market_board import MarketBoardAuthority, MarketBoardGame


MAX_QUOTE_AGE_NS = 300 * 1_000_000_000
MIN_BOOK_COVERAGE = 1
MIN_NONPUSH_SUPPORT = Fraction("0.80")
MIN_POINT_GAP = Fraction("1.00")
MIN_SUPPORT_MARGIN = Fraction("0.05")
_BOOK_ORDER = MappingProxyType({book: index for index, book in enumerate(EXPECTED_BOOKS)})


@dataclass(frozen=True, slots=True)
class TotalCandidate:
    game_pk: int
    book_id: str
    event_id: str
    captured_at_utc: str
    line_half_runs: int
    direction: str
    price: int
    nonpush_support: Fraction
    point_gap: Fraction | None
    support_margin: Fraction | None
    push_count: int
    over_count: int
    under_count: int
    decision: str
    reason_codes: tuple[str, ...]


def _decimal(value: Fraction | None) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 28
        text = format(Decimal(value.numerator) / Decimal(value.denominator), ".12f")
    text = text.rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.rsplit(".", 1)[1]) == 1:
        text += "0"
    return text


def _ordered_reasons(reasons: set[str]) -> tuple[str, ...]:
    unknown = reasons.difference(REASON_ORDER)
    if unknown:
        raise ValueError(f"unknown totals reason: {sorted(unknown)}")
    return tuple(reason for reason in REASON_ORDER if reason in reasons)


def _sample_counts(game: TotalsGameAuthority) -> tuple[int, ...]:
    """Convert sealed residual observations to integer run outcomes."""
    if type(game.residual_count) is not int or game.residual_count <= 0:
        return ()
    if len(game.residual_samples) != game.residual_count:
        return ()
    counts: list[int] = []
    for residual in game.residual_samples:
        try:
            exact = Fraction(str(residual))
        except (ValueError, ZeroDivisionError):
            return ()
        if exact.denominator != 1 or exact < 0:
            return ()
        counts.append(exact.numerator)
    return tuple(counts)


def evaluate_total_candidate(
    game, quote: TotalQuote, direction: str
) -> TotalCandidate:
    """Evaluate one exact (book, line, direction, price) combination."""
    from .market_play_authorities import TotalsGameAuthority

    if not isinstance(game, TotalsGameAuthority) or not isinstance(quote, TotalQuote):
        raise TypeError("totals game and quote types differ")
    if direction not in {"OVER", "UNDER"}:
        raise ValueError("totals direction differs")
    if quote.game_pk != game.game_pk:
        raise ValueError("totals quote game identity differs")
    price = quote.over_american_odds if direction == "OVER" else quote.under_american_odds
    reasons: set[str] = set()
    counts = _sample_counts(game)
    if (
        type(game.residual_count) is not int
        or game.residual_count <= 0
        or len(game.residual_samples) != game.residual_count
        or len(counts) != game.residual_count
    ):
        reasons.add("RESIDUAL_COUNT_MISMATCH")
    line_ticks = quote.line_half_runs
    line_runs = Fraction(line_ticks, 2)
    over_count = sum(value > line_runs for value in counts)
    under_count = sum(value < line_runs for value in counts)
    push_count = len(counts) - over_count - under_count
    nonpush = over_count + under_count
    support: Fraction | None = None
    margin: Fraction | None = None
    if nonpush:
        selected = over_count if direction == "OVER" else under_count
        support = Fraction(selected, nonpush)
        margin = support - quoted_break_even_fraction(price)
        if support < MIN_NONPUSH_SUPPORT:
            reasons.add("NONPUSH_SUPPORT_TOO_LOW")
        if margin < MIN_SUPPORT_MARGIN:
            reasons.add("SUPPORT_MARGIN_TOO_LOW")
    else:
        reasons.add("NONPUSH_SUPPORT_TOO_LOW")
    point_gap = None if game.expected_total is None else abs(game.expected_total - line_runs)
    if point_gap is None or point_gap < MIN_POINT_GAP:
        reasons.add("POINT_GAP_TOO_SMALL")
    return TotalCandidate(
        game.game_pk,
        quote.book_id,
        quote.event_id,
        quote.captured_at_utc,
        line_ticks,
        direction,
        price,
        Fraction(0) if support is None else support,
        point_gap,
        margin,
        push_count,
        over_count,
        under_count,
        "PLAY" if not reasons else "PASS",
        _ordered_reasons(reasons),
    )


def select_total_play(candidates: Sequence[TotalCandidate]) -> TotalCandidate | None:
    """Select the strongest qualifying candidate with fixed deterministic ties."""
    plays = [candidate for candidate in candidates if candidate.decision == "PLAY"]
    if not plays:
        return None

    def key(candidate: TotalCandidate) -> tuple[Fraction, Fraction, Fraction, int, int, int, int]:
        return (
            candidate.support_margin or Fraction(0),
            candidate.nonpush_support,
            candidate.point_gap or Fraction(0),
            canonical_utc_ns(candidate.captured_at_utc),
            -_BOOK_ORDER.get(candidate.book_id, len(EXPECTED_BOOKS)),
            -candidate.line_half_runs,
            1 if candidate.direction == "OVER" else 0,
        )

    return max(plays, key=key)


def _fresh_quotes(
    board_game: MarketBoardGame, evaluated_at_ns: int
) -> tuple[tuple[TotalQuote, ...], bool]:
    fresh: list[TotalQuote] = []
    stale = False
    seen: set[tuple[str, int]] = set()
    for quote in board_game.totals:
        if quote.game_pk != board_game.game_pk:
            raise ValueError("total quote game identity differs")
        try:
            age = evaluated_at_ns - canonical_utc_ns(quote.captured_at_utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("total quote timestamp differs") from exc
        if age < 0 or age > MAX_QUOTE_AGE_NS:
            stale = True
            continue
        identity = (quote.book_id, quote.line_half_runs)
        if identity in seen:
            raise ValueError("duplicate total book/line quote")
        seen.add(identity)
        fresh.append(quote)
    return tuple(fresh), stale


def _row(
    game: TotalsGameAuthority,
    *,
    decision: str,
    reasons: set[str],
    candidate: TotalCandidate | None,
    coverage: int,
    evaluated_at: str,
    participant: ParticipantGateEvidence,
) -> Mapping[str, object]:
    return MappingProxyType(
        {
            "game_pk": game.game_pk,
            "scheduled_start_utc": game.scheduled_start_utc,
            "away_team": game.away_team,
            "home_team": game.home_team,
            "model_status": game.status,
            "participant_complete": participant.complete,
            "starter_consistent": participant.starter_consistent,
            "late_scratch": participant.late_scratch,
            "decision": decision,
            "reason_codes": list(_ordered_reasons(reasons)),
            "selected_book": None if candidate is None else candidate.book_id,
            "selected_event_id": None if candidate is None else candidate.event_id,
            "line_half_runs": None if candidate is None else candidate.line_half_runs,
            "direction": None if candidate is None else candidate.direction,
            "selected_price": None if candidate is None else candidate.price,
            "selected_captured_at_utc": None if candidate is None else candidate.captured_at_utc,
            "nonpush_support": None if candidate is None else _decimal(candidate.nonpush_support),
            "point_gap_runs": None if candidate is None else _decimal(candidate.point_gap),
            "support_margin": None if candidate is None else _decimal(candidate.support_margin),
            "push_count": None if candidate is None else candidate.push_count,
            "evaluated_at_utc": evaluated_at,
            "book_coverage": coverage,
        }
    )


def evaluate_totals(
    totals: TotalsAuthority,
    participants: Mapping[int, object],
    board: MarketBoardAuthority,
    *,
    evaluated_at: str,
) -> tuple[Mapping[str, object], ...]:
    """Evaluate every official game and emit at most one total PLAY each."""
    from .market_play_authorities import ParticipantGateEvidence, TotalsAuthority

    if not isinstance(totals, TotalsAuthority) or not isinstance(board, MarketBoardAuthority):
        raise TypeError("totals authority and board types differ")
    if not isinstance(participants, Mapping):
        raise TypeError("participant authority differs")
    evaluated_at_ns = canonical_utc_ns(evaluated_at)
    rows: list[Mapping[str, object]] = []
    for board_game in board.games:
        game = totals.by_game.get(board_game.game_pk)
        if game is None:
            raise ValueError("market board game is absent from totals authority")
        names_bound = not (
            game.away_team.isdecimal() and game.home_team.isdecimal()
        )
        if game.scheduled_start_utc != board_game.scheduled_start_utc or (
            names_bound
            and (
                game.away_team != board_game.away_team
                or game.home_team != board_game.home_team
            )
        ):
            raise ValueError("totals/market board game identity differs")
        participant = participants.get(game.game_pk)
        if not isinstance(participant, ParticipantGateEvidence):
            participant = ParticipantGateEvidence(False, False, False, ("MISSING_PARTICIPANT_AUTHORITY",))
        reasons: set[str] = set()
        if canonical_utc_ns(game.scheduled_start_utc) <= evaluated_at_ns:
            reasons.add("GAME_STARTED")
        if not participant.complete:
            reasons.add("INCOMPLETE_PARTICIPANTS")
        if not participant.starter_consistent:
            reasons.add("STARTER_CONFLICT")
        if participant.late_scratch:
            reasons.add("LATE_SCRATCH")
        sample_counts = _sample_counts(game)
        if (
            game.status != "PROJECTED"
            or type(game.residual_count) is not int
            or game.residual_count <= 0
            or len(game.residual_samples) != game.residual_count
            or len(sample_counts) != game.residual_count
        ):
            reasons.add("RESIDUAL_COUNT_MISMATCH")
        fresh, had_stale = _fresh_quotes(board_game, evaluated_at_ns)
        coverage = len({quote.book_id for quote in fresh})
        if had_stale and coverage < MIN_BOOK_COVERAGE:
            reasons.add("STALE_PRICE")
        if coverage == 0:
            reasons.add("NO_QUOTE")
        elif coverage < MIN_BOOK_COVERAGE:
            reasons.add("INSUFFICIENT_BOOK_COVERAGE")
        candidates = [
            evaluate_total_candidate(game, quote, direction)
            for quote in fresh
            for direction in ("OVER", "UNDER")
        ]
        selected = select_total_play(candidates)
        if candidates:
            if all("NONPUSH_SUPPORT_TOO_LOW" in candidate.reason_codes for candidate in candidates):
                reasons.add("NONPUSH_SUPPORT_TOO_LOW")
            if all(candidate.point_gap is None or candidate.point_gap < MIN_POINT_GAP for candidate in candidates):
                reasons.add("POINT_GAP_TOO_SMALL")
            if all(candidate.support_margin is None or candidate.support_margin < MIN_SUPPORT_MARGIN for candidate in candidates):
                reasons.add("SUPPORT_MARGIN_TOO_LOW")
        if selected is None:
            reasons.add("AMBIGUOUS_DIRECTION") if candidates and not reasons and len(candidates) > 0 else None
            rows.append(_row(game, decision="PASS", reasons=reasons, candidate=None, coverage=coverage, evaluated_at=evaluated_at, participant=participant))
            continue
        decision = "PLAY" if not reasons else "PASS"
        rows.append(_row(game, decision=decision, reasons=reasons, candidate=selected, coverage=coverage, evaluated_at=evaluated_at, participant=participant))
    return tuple(rows)


__all__ = [
    "TotalCandidate",
    "evaluate_total_candidate",
    "evaluate_totals",
    "select_total_play",
]
