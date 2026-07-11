import math
from collections.abc import Mapping
from datetime import date
from numbers import Integral, Real
from typing import Iterable, Sequence


FROZEN_TRAINING_START = "2022-01-01"
FROZEN_TRAINING_END = "2024-12-31"


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be a finite number")
    return converted


def clip(value: float, low: float, high: float) -> float:
    value = _finite_number(value, "value")
    low = _finite_number(low, "low")
    high = _finite_number(high, "high")
    if low > high:
        raise ValueError("low must not exceed high")
    return min(max(value, low), high)


def shrink(observed: float, n: float, mean: float, k: float) -> float:
    observed = _finite_number(observed, "observed")
    n = _finite_number(n, "n")
    mean = _finite_number(mean, "mean")
    k = _finite_number(k, "k")
    if n < 0:
        raise ValueError("n must be non-negative")
    if k <= 0:
        raise ValueError("k must be positive")
    if n == 0:
        return mean
    if n >= k:
        reliability = 1.0 / (1.0 + k / n)
    else:
        ratio = n / k
        reliability = ratio / (1.0 + ratio)
    return reliability * observed + (1.0 - reliability) * mean


def metric_score(value: float, mean: float, sd: float, direction: int) -> float:
    value = _finite_number(value, "value")
    mean = _finite_number(mean, "mean")
    sd = _finite_number(sd, "sd")
    if sd <= 0:
        raise ValueError("sd must be positive")
    if isinstance(direction, bool) or direction not in (-1, 1):
        raise ValueError("direction must be exactly +1 or -1")
    z_score = clip((value - mean) / sd, -2.5, 2.5)
    return clip(50.0 + 20.0 * direction * z_score, 0.0, 100.0)


def sigmoid(x: float) -> float:
    x = _finite_number(x, "x")
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exponential = math.exp(x)
    return exponential / (1.0 + exponential)


def _logistic_objective(
    x_values: Sequence[float],
    y_values: Sequence[int],
    alpha: float,
    beta: float,
    l2: float,
) -> float:
    loss = 0.0
    for x_value, y_value in zip(x_values, y_values):
        linear = alpha + beta * x_value
        loss += max(linear, 0.0) - y_value * linear + math.log1p(math.exp(-abs(linear)))
    return loss + l2 * beta * beta


def fit_logistic(
    x_values: Iterable[float],
    y_values: Iterable[int],
    iterations: int = 100,
    l2: float = 1e-6,
    tolerance: float = 1e-10,
) -> tuple[float, float]:
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations <= 0
    ):
        raise ValueError("iterations must be a positive integer")
    l2 = _finite_number(l2, "l2")
    tolerance = _finite_number(tolerance, "tolerance")
    if l2 <= 0:
        raise ValueError("l2 must be positive")
    if tolerance <= 0:
        raise ValueError("tolerance must be positive")

    try:
        x_items = list(x_values)
        y_items = list(y_values)
    except TypeError as exc:
        raise ValueError("x_values and y_values must be iterable") from exc
    if len(x_items) != len(y_items) or len(x_items) < 2:
        raise ValueError("x_values and y_values must have equal length of at least 2")
    x_data = [_finite_number(value, "x value") for value in x_items]
    if any(
        isinstance(value, bool)
        or not isinstance(value, Integral)
        or value not in (0, 1)
        for value in y_items
    ):
        raise ValueError("y values must be binary integers 0 or 1")
    y_data = [int(value) for value in y_items]
    if len(set(y_data)) != 2:
        raise ValueError("both outcome classes must be present")

    x_scale = max(1.0, max(abs(value) for value in x_data))
    inverse_scale = 1.0 / x_scale
    normalized_x = [value * inverse_scale for value in x_data]
    max_normalized_x = max(abs(value) for value in normalized_x)
    scaled_l2 = l2 * inverse_scale * inverse_scale

    alpha = math.log(sum(y_data) / (len(y_data) - sum(y_data)))
    gamma = 0.0
    objective = _logistic_objective(
        normalized_x, y_data, alpha, gamma, scaled_l2
    )

    for _ in range(iterations):
        gradient_alpha = 0.0
        gradient_gamma = 2.0 * scaled_l2 * gamma
        hessian_aa = 0.0
        hessian_ag = 0.0
        hessian_gg = 2.0 * scaled_l2
        for x_value, y_value in zip(normalized_x, y_data):
            fitted = sigmoid(alpha + gamma * x_value)
            residual = fitted - y_value
            weight = fitted * (1.0 - fitted)
            gradient_alpha += residual
            gradient_gamma += residual * x_value
            hessian_aa += weight
            hessian_ag += weight * x_value
            hessian_gg += weight * x_value * x_value

        determinant = hessian_aa * hessian_gg - hessian_ag * hessian_ag
        if determinant <= 0 or not math.isfinite(determinant):
            break
        step_alpha = (
            hessian_gg * gradient_alpha - hessian_ag * gradient_gamma
        ) / determinant
        step_gamma = (
            hessian_aa * gradient_gamma - hessian_ag * gradient_alpha
        ) / determinant
        step_size = max(
            abs(step_alpha),
            abs(step_gamma) * max_normalized_x,
        )
        if step_size <= tolerance:
            break

        step_scale = 1.0
        accepted = False
        while step_scale >= 2.0**-30:
            candidate_alpha = alpha - step_scale * step_alpha
            candidate_gamma = gamma - step_scale * step_gamma
            candidate_objective = _logistic_objective(
                normalized_x,
                y_data,
                candidate_alpha,
                candidate_gamma,
                scaled_l2,
            )
            if candidate_objective <= objective:
                alpha = candidate_alpha
                gamma = candidate_gamma
                objective = candidate_objective
                accepted = True
                break
            step_scale *= 0.5
        if not accepted:
            break
        if step_scale * step_size <= tolerance:
            break

    beta = gamma * inverse_scale
    if not math.isfinite(alpha) or not math.isfinite(beta):
        raise ValueError("logistic calibration did not produce finite coefficients")
    return alpha, beta


def fit_frozen_calibration(
    rows: Iterable[Mapping[str, object]],
    training_end: str = FROZEN_TRAINING_END,
    training_start: str = FROZEN_TRAINING_START,
) -> tuple[float, float, dict[str, object]]:
    try:
        boundary = date.fromisoformat(training_end)
    except (TypeError, ValueError) as exc:
        raise ValueError("training_end must be an ISO date") from exc
    if boundary > date.fromisoformat(FROZEN_TRAINING_END):
        raise ValueError("training_end exceeds frozen boundary")
    try:
        start_boundary = date.fromisoformat(training_start)
    except (TypeError, ValueError) as exc:
        raise ValueError("training_start must be an ISO date") from exc
    if start_boundary < date.fromisoformat(FROZEN_TRAINING_START):
        raise ValueError("training_start precedes frozen boundary")
    if start_boundary > boundary:
        raise ValueError("training_start must not exceed training_end")
    try:
        row_items = list(rows)
    except TypeError as exc:
        raise ValueError("rows must be iterable") from exc

    seen_game_pks: set[int] = set()
    dates: list[date] = []
    edges: list[float] = []
    outcomes: list[object] = []
    for row in row_items:
        if not isinstance(row, Mapping):
            raise ValueError("calibration rows must be mappings")
        try:
            official_date = date.fromisoformat(row["official_date"])
            edge = row["edge"]
            outcome = row["home_win"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("calibration row is malformed") from exc
        if official_date > boundary:
            raise ValueError("calibration data crosses frozen boundary")
        if official_date < start_boundary:
            raise ValueError("calibration data crosses frozen boundary")
        if "game_pk" in row:
            game_pk = row["game_pk"]
            if (
                isinstance(game_pk, bool)
                or not isinstance(game_pk, int)
                or game_pk <= 0
            ):
                raise ValueError("game_pk must be a positive integer")
            if game_pk in seen_game_pks:
                raise ValueError("duplicate game_pk in calibration data")
            seen_game_pks.add(game_pk)
        dates.append(official_date)
        edges.append(_finite_number(edge, "edge") / 10.0)
        outcomes.append(outcome)

    alpha, beta = fit_logistic(edges, outcomes)
    return alpha, beta, {
        "training_min_date": min(dates).isoformat(),
        "training_max_date": max(dates).isoformat(),
        "training_count": len(row_items),
        "l2": 1e-6,
    }


def probability(
    edge: float,
    alpha: float,
    beta: float,
    clip_bounds: tuple[float, float] = (0.10, 0.90),
) -> float:
    edge = _finite_number(edge, "edge")
    alpha = _finite_number(alpha, "alpha")
    beta = _finite_number(beta, "beta")
    try:
        if len(clip_bounds) != 2:
            raise ValueError("clip_bounds must contain exactly two values")
        low = _finite_number(clip_bounds[0], "lower clip bound")
        high = _finite_number(clip_bounds[1], "upper clip bound")
    except (TypeError, IndexError) as exc:
        raise ValueError("clip_bounds must contain exactly two values") from exc
    if low < 0 or high > 1 or low >= high:
        raise ValueError("clip bounds must satisfy 0 <= low < high <= 1")
    return clip(sigmoid(alpha + beta * edge / 10.0), low, high)
