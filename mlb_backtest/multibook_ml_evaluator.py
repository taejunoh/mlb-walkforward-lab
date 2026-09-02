"""Pure full-slate moneyline PLAY/PASS evaluation.

Only admitted authority objects and the normalized market board are accepted.
There is intentionally no sportsbook, browser, HTTP, or model-runner import in
this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .market_play_authorities import MlAuthority, MlGameAuthority

from .market_play_contracts import (
    EXPECTED_BOOKS,
    REASON_ORDER,
    MlQuote,
    canonical_utc_ns,
    quoted_break_even_fraction,
)
from .multibook_market_board import MarketBoardAuthority, MarketBoardGame


MAX_QUOTE_AGE_NS = 300 * 1_000_000_000
MIN_BOOK_COVERAGE = 3
MIN_COMPLETENESS = Fraction("0.50")
MIN_PRICE_MARGIN = Fraction("0.05")
_BOOK_ORDER = MappingProxyType({book: index for index, book in enumerate(EXPECTED_BOOKS)})


@dataclass(frozen=True, slots=True)
class MoneylineDecision:
    """Typed view of one emitted moneyline row."""

    game_pk: int
    decision: str
    reason_codes: tuple[str, ...]
    selected_book: str | None
    selected_price: int | None
    price_margin: Fraction | None


def _decimal(value: Fraction | None) -> str | None:
    if value is None:
        return None
    # Keep enough precision for audit output while retaining the policy's
    # familiar two-place representation for exact decimal thresholds.
    with localcontext() as context:
        context.prec = 28
        text = format(Decimal(value.numerator) / Decimal(value.denominator), ".12f")
    text = text.rstrip("0").rstrip(".")
    if "." not in text:
        text += ".00"
    elif len(text.rsplit(".", 1)[1]) == 1:
        text += "0"
    return text


def select_best_ml_quote(predicted_side: str, quotes: Sequence[MlQuote]) -> MlQuote | None:
    """Choose the lowest break-even quote with deterministic book tie-breaks."""
    if predicted_side not in {"away", "home"}:
        raise ValueError("predicted side differs")
    candidates = [quote for quote in quotes if isinstance(quote, MlQuote)]
    if not candidates:
        return None

    def key(quote: MlQuote) -> tuple[Fraction, int, str, str]:
        price = quote.away_american_odds if predicted_side == "away" else quote.home_american_odds
        return (
            quoted_break_even_fraction(price),
            _BOOK_ORDER.get(quote.book_id, len(EXPECTED_BOOKS)),
            quote.book_id,
            quote.event_id,
        )

    return min(candidates, key=key)


def _ordered_reasons(reasons: set[str]) -> tuple[str, ...]:
    unknown = reasons.difference(REASON_ORDER)
    if unknown:
        raise ValueError(f"unknown moneyline reason: {sorted(unknown)}")
    return tuple(reason for reason in REASON_ORDER if reason in reasons)


def _fresh_quotes(
    board_game: MarketBoardGame, predicted_side: str, evaluated_at_ns: int
) -> tuple[tuple[MlQuote, ...], bool]:
    fresh: list[MlQuote] = []
    had_stale = False
    for quote in board_game.moneylines:
        if quote.game_pk != board_game.game_pk:
            raise ValueError("moneyline quote game identity differs")
        if quote.book_id not in EXPECTED_BOOKS:
            raise ValueError("moneyline quote book differs")
        try:
            age = evaluated_at_ns - canonical_utc_ns(quote.captured_at_utc)
        except (TypeError, ValueError) as exc:
            raise ValueError("moneyline quote timestamp differs") from exc
        if age < 0 or age > MAX_QUOTE_AGE_NS:
            had_stale = True
            continue
        fresh.append(quote)
    # A board may contain one quote per book; duplicate books are an authority
    # failure, not a reason to silently pick one.
    books = [quote.book_id for quote in fresh]
    if len(books) != len(set(books)):
        raise ValueError("duplicate moneyline book quote")
    del predicted_side  # the quote pair remains paired; side is selected later.
    return tuple(fresh), had_stale


def _row(
    game: MlGameAuthority,
    *,
    decision: str,
    reasons: set[str],
    quote: MlQuote | None,
    margin: Fraction | None,
    coverage: int,
    evaluated_at: str,
) -> Mapping[str, object]:
    selected_price = None
    opposing_price = None
    if quote is not None:
        if game.predicted_side == "away":
            selected_price, opposing_price = quote.away_american_odds, quote.home_american_odds
        else:
            selected_price, opposing_price = quote.home_american_odds, quote.away_american_odds
    payload = {
        "game_pk": game.game_pk,
        "scheduled_start_utc": game.scheduled_start_utc,
        "away_team": game.away_team,
        "home_team": game.home_team,
        "predicted_side": game.predicted_side,
        "pick_probability": _decimal(game.pick_probability),
        "data_completeness": _decimal(game.data_completeness),
        "model_status": game.model_status,
        "participant_complete": game.participant.complete,
        "starter_consistent": game.participant.starter_consistent,
        "late_scratch": game.participant.late_scratch,
        "decision": decision,
        "reason_codes": list(_ordered_reasons(reasons)),
        "selected_book": None if quote is None else quote.book_id,
        "selected_event_id": None if quote is None else quote.event_id,
        "selected_price": selected_price,
        "opposing_price": opposing_price,
        "selected_captured_at_utc": None if quote is None else quote.captured_at_utc,
        "quoted_break_even": None if selected_price is None else _decimal(quoted_break_even_fraction(selected_price)),
        "price_margin": _decimal(margin),
        "book_coverage": coverage,
        "evaluated_at_utc": evaluated_at,
        "legacy_primary_rank": game.legacy_primary_rank,
    }
    return MappingProxyType(payload)


def evaluate_moneylines(
    ml: MlAuthority,
    board: MarketBoardAuthority,
    *,
    evaluated_at: str,
) -> tuple[Mapping[str, object], ...]:
    """Evaluate every official game without a fixed play quota."""
    from .market_play_authorities import MlAuthority

    if not isinstance(ml, MlAuthority) or not isinstance(board, MarketBoardAuthority):
        raise TypeError("ML authority and market board types differ")
    evaluated_at_ns = canonical_utc_ns(evaluated_at)
    rows: list[Mapping[str, object]] = []
    for board_game in board.games:
        game = ml.by_game.get(board_game.game_pk)
        if game is None:
            raise ValueError("market board game is absent from ML authority")
        if (
            game.away_team != board_game.away_team
            or game.home_team != board_game.home_team
            or game.scheduled_start_utc != board_game.scheduled_start_utc
        ):
            raise ValueError("ML/market board game identity differs")
        reasons: set[str] = set()
        if canonical_utc_ns(game.scheduled_start_utc) <= evaluated_at_ns:
            reasons.add("GAME_STARTED")
        if not game.participant.complete:
            reasons.add("INCOMPLETE_PARTICIPANTS")
        if not game.participant.starter_consistent:
            reasons.add("STARTER_CONFLICT")
        if game.participant.late_scratch:
            reasons.add("LATE_SCRATCH")
        if game.model_status == "UNSCORED" or game.predicted_side is None or game.pick_probability is None:
            reasons.add("MODEL_UNSCORED")
            rows.append(_row(game, decision="PASS", reasons=reasons, quote=None, margin=None, coverage=0, evaluated_at=evaluated_at))
            continue
        if game.data_completeness is None or game.data_completeness < MIN_COMPLETENESS:
            reasons.add("DATA_COMPLETENESS_TOO_LOW")
        fresh, had_stale = _fresh_quotes(board_game, game.predicted_side, evaluated_at_ns)
        coverage = len({quote.book_id for quote in fresh})
        # Stale evidence is diagnostic only when removing it leaves the
        # market below the three-book gate; three fresh books remain playable.
        if had_stale and coverage < MIN_BOOK_COVERAGE:
            reasons.add("STALE_PRICE")
        if coverage == 0:
            reasons.add("NO_QUOTE")
        elif coverage < MIN_BOOK_COVERAGE:
            reasons.add("INSUFFICIENT_BOOK_COVERAGE")
        quote = select_best_ml_quote(game.predicted_side, fresh)
        margin: Fraction | None = None
        if quote is not None:
            selected_price = quote.away_american_odds if game.predicted_side == "away" else quote.home_american_odds
            margin = game.pick_probability - quoted_break_even_fraction(selected_price)
            if margin < MIN_PRICE_MARGIN:
                reasons.add("PRICE_MARGIN_TOO_LOW")
        else:
            reasons.add("NO_QUOTE")
        decision = "PLAY" if not reasons else "PASS"
        rows.append(_row(game, decision=decision, reasons=reasons, quote=quote, margin=margin, coverage=coverage, evaluated_at=evaluated_at))
    return tuple(rows)


__all__ = ["MoneylineDecision", "evaluate_moneylines", "select_best_ml_quote"]
