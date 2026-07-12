"""Post-lock backtest metrics and graded-pick completion rules."""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from numbers import Real

from .locking import _validate_prediction, validate_lock_rows
from .outcomes import _validate_outcome


_TIERS = ("S", "A", "B", "Lean", "Toss-up")
_BINS = (
    ("50-54", 0.50, 0.54, False),
    ("54-58", 0.54, 0.58, False),
    ("58-63", 0.58, 0.63, False),
    ("63-68", 0.63, 0.68, False),
    ("68-100", 0.68, 1.00, True),
)


def _probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("probability must be a finite number")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("probability must be a finite number") from exc
    if not math.isfinite(converted) or not 0.0 <= converted <= 1.0:
        raise ValueError("probability must be between 0 and 1")
    return converted


def _correct(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("correct must be bool")
    return value


def brier(p: object, correct: object) -> float:
    """Return the binary Brier score for a predicted side."""
    probability = _probability(p)
    observed = 1.0 if _correct(correct) else 0.0
    return (probability - observed) ** 2


def log_loss(p: object, correct: object) -> float:
    """Return clipped binary log loss for a predicted side."""
    probability = min(0.999, max(0.001, _probability(p)))
    return -math.log(probability if _correct(correct) else 1.0 - probability)


def wilson(wins: object, n: object, z: object = 1.96) -> tuple[float, float]:
    """Return the two-sided Wilson score interval."""
    if isinstance(wins, bool) or not isinstance(wins, int):
        raise ValueError("wins must be an integer")
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError("n must be an integer")
    if n <= 0 or wins < 0 or wins > n:
        raise ValueError("wins and n must satisfy 0 <= wins <= n and n > 0")
    if isinstance(z, bool) or not isinstance(z, Real):
        raise ValueError("z must be a positive finite number")
    try:
        z_value = float(z)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("z must be a positive finite number") from exc
    if not math.isfinite(z_value) or z_value <= 0:
        raise ValueError("z must be a positive finite number")
    if z_value > math.sqrt(sys.float_info.max):
        raise ValueError("z is too large for a finite Wilson interval")
    proportion = wins / n
    z_squared = z_value * z_value
    if not math.isfinite(z_squared):
        raise ValueError("z is too large for a finite Wilson interval")
    denominator = 1.0 + z_squared / n
    center = (proportion + z_squared / (2.0 * n)) / denominator
    margin = (
        z_value
        * math.sqrt(
            proportion * (1.0 - proportion) / n
            + z_squared / (4.0 * n * n)
        )
        / denominator
    )
    bounds = center - margin, center + margin
    if not all(math.isfinite(bound) for bound in bounds):
        raise ValueError("Wilson interval bounds must be finite")
    return bounds


def max_losing_streak(sequence: Iterable[bool]) -> int:
    """Return the maximum consecutive False values in a result sequence."""
    if sequence is None:
        raise ValueError("sequence must be iterable bool values")
    longest = current = 0
    try:
        for result in sequence:
            if not isinstance(result, bool):
                raise ValueError("sequence must contain only bool values")
            if result:
                current = 0
            else:
                current += 1
                longest = max(longest, current)
    except TypeError as exc:
        raise ValueError("sequence must be iterable bool values") from exc
    return longest


def _validate_target(target: object) -> int:
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0:
        raise ValueError("target must be a positive integer")
    return target


def _market_fields(outcome: Mapping[str, object], row: Mapping[str, object]) -> None:
    metadata = ("market_snapshot_at", "market_source", "market_favorite")
    present = [field in outcome for field in metadata]
    if any(present) and not all(present):
        raise ValueError("market comparison requires snapshot, source, and favorite")
    if all(present):
        if not outcome["market_snapshot_at"] or not outcome["market_source"]:
            raise ValueError("market snapshot and source must be non-empty")
        if outcome["market_favorite"] not in (
            row["away_team"],
            row["home_team"],
            "Pick'em",
        ):
            raise ValueError("market_favorite must equal a locked team or Pick'em")
    probability_fields = ("away_no_vig_probability", "home_no_vig_probability")
    probabilities_present = [field in outcome for field in probability_fields]
    if any(probabilities_present) and not all(probabilities_present):
        raise ValueError("both no-vig probabilities are required")
    if all(probabilities_present):
        if not all(present):
            raise ValueError("no-vig probabilities require complete market metadata")
        away = _probability(outcome["away_no_vig_probability"])
        home = _probability(outcome["home_no_vig_probability"])
        if not 0.0 < away < 1.0 or not 0.0 < home < 1.0:
            raise ValueError("no-vig probabilities must be strictly between zero and one")
        if not math.isclose(away + home, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("no-vig probabilities must sum to one")


def _validated_rows(
    rows: Iterable[Mapping[str, object]], model_version: str
) -> list[dict[str, object]]:
    if not isinstance(model_version, str) or not model_version:
        raise ValueError("model_version must be a non-empty string")
    if rows is None:
        raise ValueError("rows must be iterable mappings")
    try:
        sources = list(rows)
    except TypeError as exc:
        raise ValueError("rows must be iterable mappings") from exc
    validated: list[dict[str, object]] = []
    seen: set[int] = set()
    top1_by_date: dict[str, int] = {}
    for source in sources:
        if not isinstance(source, Mapping) or "outcome" not in source:
            raise ValueError("evaluation rows require a nested outcome")
        prediction = _validate_prediction(
            {key: value for key, value in source.items() if key != "outcome"}
        )
        if prediction["model_version"] != model_version:
            raise ValueError("evaluation row model_version is incorrect or mixed")
        game_pk = prediction["game_pk"]
        if game_pk in seen:
            raise ValueError("duplicate game_pk")
        seen.add(game_pk)
        outcome = _validate_outcome(source["outcome"], prediction)
        if outcome["game_pk"] != game_pk:
            raise ValueError("outcome game_pk must match locked game_pk")
        _market_fields(outcome, prediction)
        combined = dict(prediction, outcome=outcome)
        validated.append(combined)
        if prediction["top1"] == "Yes":
            top1_by_date[prediction["official_date"]] = (
                top1_by_date.get(prediction["official_date"], 0) + 1
            )
        else:
            top1_by_date.setdefault(prediction["official_date"], 0)
    if any(count != 1 for count in top1_by_date.values()):
        raise ValueError("each selection date must contain exactly one top1 row")
    validate_lock_rows(
        [
            {key: value for key, value in row.items() if key != "outcome"}
            for row in validated
        ]
    )
    return sorted(
        validated,
        key=lambda row: (
            row["official_date"],
            row["scheduled_start_utc"],
            row["game_pk"],
        ),
    )


def _graded(rows: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        dict(
            row,
            correct=row["predicted_winner"] == row["outcome"]["actual_winner"],
        )
        for row in rows
        if row["outcome"]["status"] == "Final"
    ]


def _summary(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    results = [bool(row["correct"]) for row in rows]
    wins = sum(results)
    n = len(rows)
    losses = n - wins
    interval = wilson(wins, n) if n else (None, None)
    return {
        "W": wins,
        "L": losses,
        "N": n,
        "accuracy": wins / n if n else None,
        "wilson_95": interval,
        "wilson_low": interval[0],
        "wilson_high": interval[1],
        "brier": (
            sum(brier(row["pick_probability"], row["correct"]) for row in rows)
            / n
            if n
            else None
        ),
        "log_loss": (
            sum(log_loss(row["pick_probability"], row["correct"]) for row in rows)
            / n
            if n
            else None
        ),
        "max_losing_streak": max_losing_streak(results),
    }


def _in_bin(probability: float, low: float, high: float, inclusive: bool) -> bool:
    return low <= probability <= high if inclusive else low <= probability < high


def _baseline_summary(
    rows: Sequence[Mapping[str, object]], picks: Mapping[int, str]
) -> dict[str, object]:
    results = []
    for row in rows:
        pick = picks.get(row["game_pk"])
        if pick is None:
            continue
        results.append(pick == row["outcome"]["actual_winner"])
    wins = sum(results)
    n = len(results)
    return {
        "W": wins,
        "L": n - wins,
        "N": n,
        "accuracy": wins / n if n else None,
    }


def _paired_market(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    paired = []
    probability_scores = []
    for row in rows:
        outcome = row["outcome"]
        favorite = outcome.get("market_favorite")
        if (
            favorite is None
            or favorite == "Pick'em"
            or "away_no_vig_probability" not in outcome
            or "home_no_vig_probability" not in outcome
        ):
            continue
        model_correct = bool(row["correct"])
        market_correct = favorite == outcome["actual_winner"]
        paired.append((model_correct, market_correct))
        probability = outcome[
            "away_no_vig_probability"
            if favorite == row["away_team"]
            else "home_no_vig_probability"
        ]
        probability_scores.append(brier(probability, market_correct))
    n = len(paired)
    model_wins = sum(model for model, _ in paired)
    market_wins = sum(market for _, market in paired)
    model_accuracy = model_wins / n if n else None
    market_accuracy = market_wins / n if n else None
    return {
        "N": n,
        "model_accuracy": model_accuracy,
        "market_accuracy": market_accuracy,
        "delta": (
            model_accuracy - market_accuracy if n else None
        ),
        "model_only": sum(model and not market for model, market in paired),
        "market_only": sum(market and not model for model, market in paired),
        "market_brier_N": len(probability_scores),
        "market_brier": (
            sum(probability_scores) / len(probability_scores)
            if probability_scores
            else None
        ),
    }


def _calibration(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    bins = []
    weighted_gap = 0.0
    for label, low, high, inclusive in _BINS:
        members = [
            row
            for row in rows
            if _in_bin(float(row["pick_probability"]), low, high, inclusive)
        ]
        n = len(members)
        mean_probability = (
            sum(float(row["pick_probability"]) for row in members) / n
            if n
            else None
        )
        actual_accuracy = (
            sum(bool(row["correct"]) for row in members) / n if n else None
        )
        gap = (
            abs(mean_probability - actual_accuracy) if n else None
        )
        if gap is not None:
            weighted_gap += gap * n
        bins.append(
            {
                "bin": label,
                "N": n,
                "mean_probability": mean_probability,
                "actual_accuracy": actual_accuracy,
                "gap": gap,
                "brier_contribution": (
                    sum(
                        brier(row["pick_probability"], row["correct"])
                        for row in members
                    )
                    / n
                    if n
                    else None
                ),
            }
        )
    return {"bins": bins, "ece": weighted_gap / len(rows) if rows else None}


def _compact_groups(
    rows: Sequence[Mapping[str, object]], key
) -> dict[str, dict[str, object]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    result = {}
    for label in sorted(grouped):
        members = grouped[label]
        wins = sum(bool(row["correct"]) for row in members)
        n = len(members)
        result[label] = {
            "W": wins,
            "L": n - wins,
            "N": n,
            "accuracy": wins / n if n else None,
        }
    return result


def evaluate(
    rows: Iterable[Mapping[str, object]],
    model_version: str = "v5.1-BT",
    baseline_by_game: Mapping[int, str] | None = None,
) -> dict[str, object]:
    """Validate evaluated lock rows and return primary and diagnostic summaries."""
    validated = _validated_rows(rows, model_version)
    graded = _graded(validated)
    primary = [row for row in graded if row["top1"] == "Yes"]
    if baseline_by_game is None:
        baseline_by_game = {}
    if not isinstance(baseline_by_game, Mapping):
        raise ValueError("baseline_by_game must be a mapping")
    for game_pk, team in baseline_by_game.items():
        if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
            raise ValueError("baseline game_pk must be a positive integer")
        matching = next((row for row in validated if row["game_pk"] == game_pk), None)
        if matching is not None and team not in (
            matching["away_team"], matching["home_team"]
        ):
            raise ValueError("baseline team must equal a locked home or away team")
        if matching is None and (not isinstance(team, str) or not team):
            raise ValueError("baseline team must be a non-empty string")

    probability_bins = {
        label: _summary(
            [
                row
                for row in graded
                if _in_bin(float(row["pick_probability"]), low, high, inclusive)
            ]
        )
        for label, low, high, inclusive in _BINS
    }
    subgroups = {
        "Top3": _summary([row for row in graded if row["top3"] == "Yes"]),
        "58%+": _summary(
            [row for row in graded if float(row["pick_probability"]) >= 0.58]
        ),
        "Full Agreement": _summary(
            [row for row in graded if row["full_agreement"] == "Yes"]
        ),
        "tiers": {
            tier: _summary([row for row in graded if row["tier"] == tier])
            for tier in _TIERS
        },
        "probability_bins": probability_bins,
    }
    home_picks = {row["game_pk"]: row["home_team"] for row in primary}
    summary = {
        "model_version": model_version,
        "primary": _summary(primary),
        "void_top1": sum(
            row["top1"] == "Yes" and row["outcome"]["status"] == "VOID"
            for row in validated
        ),
        "subgroups": subgroups,
        "baselines": {
            "home": _baseline_summary(primary, home_picks),
            "higher_season_win_pct": _baseline_summary(primary, baseline_by_game),
        },
        "paired_market": _paired_market(primary),
        "calibration": _calibration(primary),
        "by_month": _compact_groups(
            primary, lambda row: str(row["official_date"])[:7]
        ),
        "by_predicted_team": _compact_groups(
            primary, lambda row: str(row["predicted_winner"])
        ),
    }
    return summary


def completion_status(
    rows: Iterable[Mapping[str, object]], target: int = 100
) -> dict[str, object]:
    """Return completion counts based only on graded Daily Top 1 picks."""
    required = _validate_target(target)
    validated = _validated_rows(rows, "v5.1-BT")
    selected = [row for row in validated if row["top1"] == "Yes"]
    graded = sum(row["outcome"]["status"] == "Final" for row in selected)
    void = sum(row["outcome"]["status"] == "VOID" for row in selected)
    dates = len({row["official_date"] for row in selected})
    return {
        "graded_top1": graded,
        "void_top1": void,
        "selection_dates": dates,
        "target": required,
        "complete": graded >= required,
        "dates_needed": max(0, required - graded),
    }


def _canonical_date(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("schedule dates must be canonical ISO dates")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("schedule dates must be canonical ISO dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("schedule dates must be canonical ISO dates")
    return value


def extension_dates(
    rows: Iterable[Mapping[str, object]],
    schedule_dates: Iterable[str],
    target: int = 100,
) -> list[str]:
    """Choose the next unused sorted schedule dates needed to reach target."""
    validated = _validated_rows(rows, "v5.1-BT")
    status = completion_status(validated, target)
    needed = status["dates_needed"]
    try:
        supplied = [_canonical_date(value) for value in schedule_dates]
    except TypeError as exc:
        raise ValueError("schedule_dates must be iterable") from exc
    if supplied != sorted(supplied) or len(supplied) != len(set(supplied)):
        raise ValueError("schedule_dates must be sorted and unique")
    existing = {row["official_date"] for row in validated if row["top1"] == "Yes"}
    current_end = max(existing) if existing else "0001-01-01"
    available = [day for day in supplied if day > current_end and day not in existing]
    if len(available) < needed:
        raise ValueError("insufficient future schedule dates")
    return available[:needed]


next_extension_dates = extension_dates


__all__ = [
    "brier",
    "completion_status",
    "evaluate",
    "extension_dates",
    "log_loss",
    "max_losing_streak",
    "next_extension_dates",
    "wilson",
]
