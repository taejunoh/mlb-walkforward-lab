"""Pure, chronological run-total projections for the provisional daily report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from numbers import Real


CANDIDATE_IDS = ("LEAGUE_30", "TEAM_30", "TEAM_VENUE_30")
_FINAL_FIELDS = frozenset(
    {
        "game_pk",
        "official_date",
        "scheduled_start_utc",
        "away_team_id",
        "home_team_id",
        "venue_id",
        "away_runs",
        "home_runs",
        "status",
    }
)
_TARGET_FIELDS = _FINAL_FIELDS - {"away_runs", "home_runs"}


@dataclass(frozen=True, slots=True)
class FinalTotalGame:
    game_pk: int
    official_date: str
    scheduled_start_utc: str
    away_team_id: int
    home_team_id: int
    venue_id: int
    away_runs: int
    home_runs: int


@dataclass(frozen=True, slots=True)
class TargetTotalGame:
    game_pk: int
    official_date: str
    scheduled_start_utc: str
    away_team_id: int
    home_team_id: int
    venue_id: int


@dataclass(frozen=True, slots=True)
class TotalProjection:
    status: str
    candidate_id: str
    away_expected_runs: float
    home_expected_runs: float
    expected_total: float


@dataclass(frozen=True, slots=True)
class UnprojectedTotal:
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    games: int
    mae: float
    rmse: float
    poisson_deviance: float


@dataclass(frozen=True, slots=True)
class SelectedTotalsModel:
    candidate_id: str
    development_metrics: EvaluationMetrics


@dataclass(frozen=True, slots=True)
class LineProbabilities:
    over: float
    under: float
    push: float


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _date(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return value


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("scheduled_start_utc must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("scheduled_start_utc must be a UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError("scheduled_start_utc must be a UTC timestamp")
    return value


def _mapping(row: object, fields: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(row, Mapping) or set(row) != fields:
        raise ValueError("row fields are invalid")
    return row


def _validate_history_tuple(history: tuple[FinalTotalGame, ...]) -> None:
    if not isinstance(history, tuple) or any(
        not isinstance(game, FinalTotalGame) for game in history
    ):
        raise ValueError("history must be a tuple of FinalTotalGame")
    game_pks = [game.game_pk for game in history]
    if len(game_pks) != len(set(game_pks)):
        raise ValueError("duplicate game_pk")


def validate_final_games(
    rows: object, *, cutoff_date: str
) -> tuple[FinalTotalGame, ...]:
    """Validate immutable final-score rows through and including ``cutoff_date``."""
    cutoff = _date(cutoff_date, "cutoff_date")
    if isinstance(rows, (str, bytes)):
        raise ValueError("rows must be an iterable of final-game mappings")
    try:
        source_rows = tuple(rows)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("rows must be an iterable of final-game mappings") from exc
    validated: list[FinalTotalGame] = []
    game_pks: set[int] = set()
    for row in source_rows:
        mapping = _mapping(row, _FINAL_FIELDS)
        if mapping["status"] != "Final":
            raise ValueError("status must be Final")
        game = FinalTotalGame(
            game_pk=_positive_int(mapping["game_pk"], "game_pk"),
            official_date=_date(mapping["official_date"], "official_date"),
            scheduled_start_utc=_utc_timestamp(mapping["scheduled_start_utc"]),
            away_team_id=_positive_int(mapping["away_team_id"], "away_team_id"),
            home_team_id=_positive_int(mapping["home_team_id"], "home_team_id"),
            venue_id=_positive_int(mapping["venue_id"], "venue_id"),
            away_runs=_nonnegative_int(mapping["away_runs"], "away_runs"),
            home_runs=_nonnegative_int(mapping["home_runs"], "home_runs"),
        )
        if game.away_team_id == game.home_team_id:
            raise ValueError("away_team_id and home_team_id must differ")
        if game.official_date > cutoff:
            raise ValueError("official_date must not exceed cutoff_date")
        if game.game_pk in game_pks:
            raise ValueError("duplicate game_pk")
        game_pks.add(game.game_pk)
        validated.append(game)
    return tuple(
        sorted(
            validated,
            key=lambda game: (
                game.official_date,
                game.scheduled_start_utc,
                game.game_pk,
            ),
        )
    )


def validate_target_games(
    rows: object, *, target_date: str
) -> tuple[TargetTotalGame, ...]:
    """Validate immutable pregame target rows for exactly one official date."""
    target = _date(target_date, "target_date")
    if isinstance(rows, (str, bytes)):
        raise ValueError("rows must be an iterable of target-game mappings")
    try:
        source_rows = tuple(rows)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError("rows must be an iterable of target-game mappings") from exc
    validated: list[TargetTotalGame] = []
    game_pks: set[int] = set()
    for row in source_rows:
        mapping = _mapping(row, _TARGET_FIELDS)
        if mapping["status"] != "Pregame":
            raise ValueError("status must be Pregame")
        game = TargetTotalGame(
            game_pk=_positive_int(mapping["game_pk"], "game_pk"),
            official_date=_date(mapping["official_date"], "official_date"),
            scheduled_start_utc=_utc_timestamp(mapping["scheduled_start_utc"]),
            away_team_id=_positive_int(mapping["away_team_id"], "away_team_id"),
            home_team_id=_positive_int(mapping["home_team_id"], "home_team_id"),
            venue_id=_positive_int(mapping["venue_id"], "venue_id"),
        )
        if game.away_team_id == game.home_team_id:
            raise ValueError("away_team_id and home_team_id must differ")
        if game.official_date != target:
            raise ValueError("official_date must equal target_date")
        if game.game_pk in game_pks:
            raise ValueError("duplicate game_pk")
        game_pks.add(game.game_pk)
        validated.append(game)
    return tuple(
        sorted(
            validated,
            key=lambda game: (
                game.official_date,
                game.scheduled_start_utc,
                game.game_pk,
            ),
        )
    )


def _prior_games(
    history: tuple[FinalTotalGame, ...], cutoff_date: str, days: int
) -> tuple[FinalTotalGame, ...]:
    cutoff = date.fromisoformat(cutoff_date)
    start = (cutoff - timedelta(days=days)).isoformat()
    return tuple(
        game
        for game in history
        if start <= game.official_date < cutoff_date
    )


def _mean(values: tuple[float, ...], name: str) -> float:
    if not values:
        raise ValueError(f"{name} has no admitted games")
    result = sum(values) / len(values)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and strictly positive")
    return result


def _league_rates(history: tuple[FinalTotalGame, ...], cutoff_date: str) -> tuple[float, float, float]:
    window = _prior_games(history, cutoff_date, 30)
    away = _mean(tuple(float(game.away_runs) for game in window), "league away rate")
    home = _mean(tuple(float(game.home_runs) for game in window), "league home rate")
    per_team = (away + home) / 2.0
    if not math.isfinite(per_team) or per_team <= 0:
        raise ValueError("league per-team rate must be finite and strictly positive")
    return away, home, per_team


def _team_rates(
    history: tuple[FinalTotalGame, ...], cutoff_date: str, team_id: int, league_per_team: float
) -> tuple[float, float]:
    games = tuple(
        game
        for game in _prior_games(history, cutoff_date, 30)
        if team_id in (game.away_team_id, game.home_team_id)
    )
    if not games:
        raise ValueError("team has no admitted games")
    scored = sum(
        game.away_runs if game.away_team_id == team_id else game.home_runs
        for game in games
    )
    allowed = sum(
        game.home_runs if game.away_team_id == team_id else game.away_runs
        for game in games
    )
    denominator = len(games) + 10
    scored_rate = (scored + 10 * league_per_team) / denominator
    allowed_rate = (allowed + 10 * league_per_team) / denominator
    if not all(math.isfinite(value) and value > 0 for value in (scored_rate, allowed_rate)):
        raise ValueError("team rates must be finite and strictly positive")
    return scored_rate, allowed_rate


def _venue_multiplier(
    history: tuple[FinalTotalGame, ...], cutoff_date: str, venue_id: int
) -> float:
    window = _prior_games(history, cutoff_date, 60)
    league_total = _mean(
        tuple(float(game.away_runs + game.home_runs) for game in window),
        "league venue rate",
    )
    venue_games = tuple(game for game in window if game.venue_id == venue_id)
    if not venue_games:
        raise ValueError("venue has no admitted games")
    venue_rate = (
        sum(game.away_runs + game.home_runs for game in venue_games) + 20 * league_total
    ) / (len(venue_games) + 20)
    ratio = venue_rate / league_total
    if not math.isfinite(ratio) or ratio <= 0:
        raise ValueError("venue ratio must be finite and strictly positive")
    return math.sqrt(ratio)


def _has_prior_team(history: tuple[FinalTotalGame, ...], game: TargetTotalGame, team_id: int) -> bool:
    return any(
        prior.official_date < game.official_date
        and team_id in (prior.away_team_id, prior.home_team_id)
        for prior in history
    )


def _has_prior_venue(history: tuple[FinalTotalGame, ...], game: TargetTotalGame) -> bool:
    return any(
        prior.official_date < game.official_date and prior.venue_id == game.venue_id
        for prior in history
    )


def project_game(
    history: tuple[FinalTotalGame, ...], game: TargetTotalGame, candidate_id: str
) -> TotalProjection | UnprojectedTotal:
    """Project one target strictly from final games dated before that target."""
    _validate_history_tuple(history)
    if not isinstance(game, TargetTotalGame):
        raise ValueError("game must be a TargetTotalGame")
    if candidate_id not in CANDIDATE_IDS:
        raise ValueError("candidate_id is not preregistered")
    if not _has_prior_team(history, game, game.away_team_id) or not _has_prior_team(
        history, game, game.home_team_id
    ):
        return UnprojectedTotal("UNPROJECTED", "UNKNOWN_TEAM")
    if not _has_prior_venue(history, game):
        return UnprojectedTotal("UNPROJECTED", "UNKNOWN_VENUE")

    league_away, league_home, league_per_team = _league_rates(history, game.official_date)
    away_expected = league_away
    home_expected = league_home
    if candidate_id in ("TEAM_30", "TEAM_VENUE_30"):
        away_scored, away_allowed = _team_rates(
            history, game.official_date, game.away_team_id, league_per_team
        )
        home_scored, home_allowed = _team_rates(
            history, game.official_date, game.home_team_id, league_per_team
        )
        away_expected *= math.sqrt(
            (away_scored / league_per_team) * (home_allowed / league_per_team)
        )
        home_expected *= math.sqrt(
            (home_scored / league_per_team) * (away_allowed / league_per_team)
        )
    if candidate_id == "TEAM_VENUE_30":
        multiplier = _venue_multiplier(history, game.official_date, game.venue_id)
        away_expected *= multiplier
        home_expected *= multiplier
    expected_total = away_expected + home_expected
    if not all(
        math.isfinite(value) and value > 0
        for value in (away_expected, home_expected, expected_total)
    ):
        raise ValueError("expected runs must be finite and strictly positive")
    return TotalProjection(
        "PROJECTED", candidate_id, away_expected, home_expected, expected_total
    )


def walk_forward_predictions(
    history: tuple[FinalTotalGame, ...],
    candidate_ids: tuple[str, ...],
    start_date: str,
    end_date: str,
) -> dict[str, tuple[tuple[FinalTotalGame, TotalProjection], ...]]:
    """Return leakage-free historical predictions, omitting unprojectable rows."""
    _validate_history_tuple(history)
    start = _date(start_date, "start_date")
    end = _date(end_date, "end_date")
    if start > end:
        raise ValueError("start_date must not follow end_date")
    if not isinstance(candidate_ids, tuple) or not candidate_ids:
        raise ValueError("candidate_ids must be a non-empty tuple")
    if any(candidate_id not in CANDIDATE_IDS for candidate_id in candidate_ids):
        raise ValueError("candidate_ids include an unregistered candidate")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("candidate_ids must be unique")
    ordered = tuple(
        sorted(
            history,
            key=lambda item: (item.official_date, item.scheduled_start_utc, item.game_pk),
        )
    )
    result: dict[str, tuple[tuple[FinalTotalGame, TotalProjection], ...]] = {}
    for candidate_id in candidate_ids:
        rows: list[tuple[FinalTotalGame, TotalProjection]] = []
        for actual in ordered:
            if not start <= actual.official_date <= end:
                continue
            target = TargetTotalGame(
                actual.game_pk,
                actual.official_date,
                actual.scheduled_start_utc,
                actual.away_team_id,
                actual.home_team_id,
                actual.venue_id,
            )
            projection = project_game(ordered, target, candidate_id)
            if isinstance(projection, TotalProjection):
                rows.append((actual, projection))
        result[candidate_id] = tuple(rows)
    return result


def _finite_positive(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return result


def _prediction_rows(
    rows: tuple[tuple[FinalTotalGame, TotalProjection], ...]
) -> tuple[tuple[FinalTotalGame, TotalProjection], ...]:
    if not isinstance(rows, tuple) or not rows:
        raise ValueError("prediction rows must be a non-empty tuple")
    for actual, projection in rows:
        if not isinstance(actual, FinalTotalGame) or not isinstance(projection, TotalProjection):
            raise ValueError("prediction rows have invalid types")
        if projection.status != "PROJECTED" or projection.candidate_id not in CANDIDATE_IDS:
            raise ValueError("prediction projection is invalid")
        _finite_positive(projection.away_expected_runs, "away_expected_runs")
        _finite_positive(projection.home_expected_runs, "home_expected_runs")
        total = _finite_positive(projection.expected_total, "expected_total")
        if not math.isclose(
            total,
            projection.away_expected_runs + projection.home_expected_runs,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("expected_total must equal side means")
    return rows


def evaluate_predictions(
    rows: tuple[tuple[FinalTotalGame, TotalProjection], ...]
) -> EvaluationMetrics:
    """Compute total-run MAE, RMSE, and mean Poisson deviance."""
    checked = _prediction_rows(rows)
    errors = tuple(
        actual.away_runs + actual.home_runs - projection.expected_total
        for actual, projection in checked
    )
    deviances = []
    for (actual, projection), error in zip(checked, errors, strict=True):
        observed = actual.away_runs + actual.home_runs
        mean = projection.expected_total
        deviances.append(
            2 * mean
            if observed == 0
            else 2 * (observed * math.log(observed / mean) - (observed - mean))
        )
    count = len(checked)
    return EvaluationMetrics(
        games=count,
        mae=sum(abs(error) for error in errors) / count,
        rmse=math.sqrt(sum(error * error for error in errors) / count),
        poisson_deviance=sum(deviances) / count,
    )


def select_candidate(
    development: dict[str, tuple[tuple[FinalTotalGame, TotalProjection], ...]]
) -> str:
    """Select only a candidate strictly better than the preregistered baseline."""
    if not isinstance(development, dict) or set(development) != set(CANDIDATE_IDS):
        raise ValueError("development must contain exactly the preregistered candidates")
    metrics = {candidate_id: evaluate_predictions(rows) for candidate_id, rows in development.items()}
    if any(metric.games < 100 for metric in metrics.values()):
        raise ValueError("development requires at least 100 rows per candidate")
    baseline = metrics["LEAGUE_30"]
    eligible = [
        candidate_id
        for candidate_id in CANDIDATE_IDS[1:]
        if metrics[candidate_id].mae < baseline.mae
        and metrics[candidate_id].poisson_deviance < baseline.poisson_deviance
    ]
    if not eligible:
        return "LEAGUE_30"
    return min(
        eligible,
        key=lambda candidate_id: (
            metrics[candidate_id].mae,
            metrics[candidate_id].poisson_deviance,
            CANDIDATE_IDS.index(candidate_id),
        ),
    )


def residual_samples(
    rows: tuple[tuple[FinalTotalGame, TotalProjection], ...], live_mean: float
) -> tuple[int, ...]:
    """Apply strictly prior residuals to a live total and round non-negatively."""
    checked = _prediction_rows(rows)
    mean = _finite_positive(live_mean, "live_mean")
    return tuple(
        sorted(
            max(
                0,
                math.floor(
                    mean + actual.away_runs + actual.home_runs - projection.expected_total + 0.5
                ),
            )
            for actual, projection in checked
        )
    )


def interval_80(samples: tuple[int, ...]) -> tuple[int, int]:
    """Return the nearest-rank 10th and 90th percentiles of discrete samples."""
    if not isinstance(samples, tuple) or not samples:
        raise ValueError("samples must be a non-empty tuple")
    if any(isinstance(sample, bool) or not isinstance(sample, int) or sample < 0 for sample in samples):
        raise ValueError("samples must be nonnegative integers")
    ordered = tuple(sorted(samples))
    return (
        ordered[math.ceil(len(ordered) * 0.10) - 1],
        ordered[math.ceil(len(ordered) * 0.90) - 1],
    )


def interval_coverage(observed: tuple[int, ...], interval: tuple[int, int]) -> float:
    """Return inclusive interval coverage for integer observed totals."""
    if not isinstance(observed, tuple) or not observed:
        raise ValueError("observed must be a non-empty tuple")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in observed):
        raise ValueError("observed must be nonnegative integers")
    if (
        not isinstance(interval, tuple)
        or len(interval) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in interval)
        or interval[0] > interval[1]
    ):
        raise ValueError("interval must contain ordered nonnegative integers")
    lower, upper = interval
    return sum(lower <= value <= upper for value in observed) / len(observed)


def compare_total_line(samples: tuple[int, ...], line: int | float) -> LineProbabilities:
    """Compare a sealed empirical discrete distribution to an optional total line."""
    if not isinstance(samples, tuple) or not samples:
        raise ValueError("samples must be a non-empty tuple")
    if any(isinstance(sample, bool) or not isinstance(sample, int) or sample < 0 for sample in samples):
        raise ValueError("samples must be nonnegative integers")
    if isinstance(line, bool) or not isinstance(line, Real):
        raise ValueError("line must be a finite nonnegative number")
    value = float(line)
    if not math.isfinite(value) or value < 0:
        raise ValueError("line must be a finite nonnegative number")
    count = len(samples)
    return LineProbabilities(
        over=sum(sample > value for sample in samples) / count,
        under=sum(sample < value for sample in samples) / count,
        push=sum(sample == value for sample in samples) / count if value.is_integer() else 0.0,
    )
