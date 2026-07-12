"""Frozen normalization and prepared-input builders for the real replay."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType

from .features.bullpen import WEIGHTS as BULLPEN_WEIGHTS
from .features.bullpen import bullpen_score
from .features.offense import WEIGHTS as OFFENSE_WEIGHTS
from .features.offense import offense_score
from .features.starter import WEIGHTS as STARTER_WEIGHTS
from .features.starter import starter_score
from .normalize import metric_score
from .scoring import component_edge, overall_completeness


METRIC_SPECS = MappingProxyType(
    {
        "starter": MappingProxyType(
            {
                "season_xwoba": -1,
                "k_minus_bb": 1,
                "fip": -1,
                "last5_xwoba": -1,
                "expected_ip": 1,
                "velocity_change": 1,
            }
        ),
        "offense": MappingProxyType(
            {
                "platoon_xwoba": 1,
                "last14_xwoba": 1,
                "bb_minus_k": 1,
                "barrel_rate": 1,
                "iso": 1,
            }
        ),
        "bullpen": MappingProxyType(
            {
                "season_xwoba": -1,
                "k_minus_bb": 1,
                "last14_xwoba": -1,
                "availability": 1,
            }
        ),
    }
)


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _metric_part(raw: object, label: str) -> tuple[float | None, float]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{label} must be a raw metric mapping")
    value = raw.get("value")
    if value is not None:
        value = _finite(value, f"{label}.value")
    quality = _finite(raw.get("quality"), f"{label}.quality")
    if not 0.0 <= quality <= 1.0:
        raise ValueError(f"{label}.quality must be between 0 and 1")
    return value, quality


def _eligible_rows(rows: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    result = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("raw feature rows must be mappings")
        if row.get("eligible") is True:
            result.append(row)
    return result


def compute_normalization(
    raw_games: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, dict[str, float | int]]]:
    """Compute frozen means and sample SDs from eligible training teams only."""
    values: dict[tuple[str, str], list[float]] = defaultdict(list)
    games = _eligible_rows(raw_games)
    for game in games:
        day = str(game.get("official_date", ""))
        if not "2022-01-01" <= day <= "2024-12-31":
            raise ValueError("normalization data crosses frozen training boundary")
        for side in ("away", "home"):
            side_data = game.get(side)
            if not isinstance(side_data, Mapping):
                raise ValueError("eligible raw game requires both team feature mappings")
            for component, specs in METRIC_SPECS.items():
                raw_component = side_data.get(component)
                if not isinstance(raw_component, Mapping):
                    raise ValueError(f"missing raw {component} component")
                for metric in specs:
                    value, _quality = _metric_part(
                        raw_component.get(metric), f"{side}.{component}.{metric}"
                    )
                    if value is not None:
                        values[(component, metric)].append(value)
    output: dict[str, dict[str, dict[str, float | int]]] = {}
    for component, specs in METRIC_SPECS.items():
        output[component] = {}
        for metric, direction in specs.items():
            observed = values[(component, metric)]
            if len(observed) < 2:
                raise ValueError(f"normalization requires two values for {component}.{metric}")
            sd = statistics.stdev(observed)
            if not math.isfinite(sd) or sd <= 0:
                raise ValueError(f"normalization SD must be positive for {component}.{metric}")
            output[component][metric] = {
                "mean": statistics.fmean(observed),
                "sd": sd,
                "direction": direction,
                "n": len(observed),
            }
    return output


def validate_normalization(value: object) -> None:
    """Validate the exact frozen metric schema and numeric constants."""
    if not isinstance(value, Mapping) or set(value) != set(METRIC_SPECS):
        raise ValueError("normalization component schema is invalid")
    for component, specs in METRIC_SPECS.items():
        metrics = value[component]
        if not isinstance(metrics, Mapping) or set(metrics) != set(specs):
            raise ValueError("normalization metric schema is invalid")
        for metric, direction in specs.items():
            constants = metrics[metric]
            if not isinstance(constants, Mapping) or set(constants) != {
                "mean", "sd", "direction", "n"
            }:
                raise ValueError("normalization constants schema is invalid")
            _finite(constants["mean"], "normalization mean")
            sd = _finite(constants["sd"], "normalization sd")
            if sd <= 0 or constants["direction"] != direction:
                raise ValueError("normalization direction or SD is invalid")
            n = constants["n"]
            if isinstance(n, bool) or not isinstance(n, int) or n < 2:
                raise ValueError("normalization n is invalid")


def _normalized_part(
    raw: object, constants: Mapping[str, object], label: str
) -> dict[str, float]:
    value, quality = _metric_part(raw, label)
    if value is None:
        return {"score": 50.0, "quality": 0.0}
    if not isinstance(constants, Mapping):
        raise ValueError(f"missing normalization constants for {label}")
    base = metric_score(
        value,
        _finite(constants.get("mean"), f"{label}.mean"),
        _finite(constants.get("sd"), f"{label}.sd"),
        constants.get("direction"),
    )
    return {"score": 50.0 + quality * (base - 50.0), "quality": quality}


def _weighted_quality(parts: Mapping[str, Mapping[str, float]], weights) -> float:
    return math.fsum(weights[name] * parts[name]["quality"] for name in weights)


def _score_side(
    side: Mapping[str, object], normalization: Mapping[str, object]
) -> tuple[dict[str, float], dict[str, float]]:
    starter_raw = side.get("starter")
    offense_raw = side.get("offense")
    bullpen_raw = side.get("bullpen")
    if not all(isinstance(value, Mapping) for value in (starter_raw, offense_raw, bullpen_raw)):
        raise ValueError("raw side requires starter, offense, and bullpen mappings")

    starter_parts = {
        metric: _normalized_part(
            starter_raw[raw_name], normalization["starter"][raw_name],
            f"starter.{raw_name}",
        )
        for metric, raw_name in (
            ("season_xwoba", "season_xwoba"),
            ("k_minus_bb", "k_minus_bb"),
            ("fip", "fip"),
            ("last5_xwoba", "last5_xwoba"),
            ("expected_ip", "expected_ip"),
            ("velo_change", "velocity_change"),
        )
    }
    offense_parts = {
        metric: _normalized_part(
            offense_raw[metric], normalization["offense"][metric],
            f"offense.{metric}",
        )
        for metric in OFFENSE_WEIGHTS
    }
    bullpen_parts = {
        metric: _normalized_part(
            bullpen_raw[metric], normalization["bullpen"][metric],
            f"bullpen.{metric}",
        )
        for metric in BULLPEN_WEIGHTS
    }
    players = offense_raw.get("players")
    if not isinstance(players, Sequence):
        raise ValueError("raw offense requires ordered players")
    lineup = [
        {"player_id": player["player_id"], "batting_slot": player["batting_slot"]}
        for player in players
    ]
    scores = {
        "starter": starter_score(
            {name: part["score"] for name, part in starter_parts.items()}
        ),
        "offense": offense_score(
            lineup, {name: part["score"] for name, part in offense_parts.items()}
        ),
        "bullpen": bullpen_score(bullpen_parts)[0],
    }
    qualities = {
        "starter": _weighted_quality(starter_parts, STARTER_WEIGHTS),
        "offense": _weighted_quality(offense_parts, OFFENSE_WEIGHTS),
        "bullpen": bullpen_score(bullpen_parts)[1],
    }
    for component in ("environment", "defense"):
        _metric_part(side.get(component), component)
        scores[component] = 50.0
        qualities[component] = 0.0
    schedule_value, schedule_quality = _metric_part(side.get("schedule"), "schedule")
    scores["schedule"] = 50.0 if schedule_value is None else schedule_value
    qualities["schedule"] = schedule_quality
    return scores, qualities


def score_raw_game(
    raw_game: Mapping[str, object], normalization: Mapping[str, object]
) -> dict[str, object]:
    if not isinstance(raw_game, Mapping) or raw_game.get("eligible") is not True:
        raise ValueError("only eligible raw games can be scored")
    away_scores, away_quality = _score_side(raw_game["away"], normalization)
    home_scores, home_quality = _score_side(raw_game["home"], normalization)
    qualities = {
        name: min(away_quality[name], home_quality[name]) for name in away_quality
    }
    return {
        "away_scores": away_scores,
        "home_scores": home_scores,
        "away_qualities": away_quality,
        "home_qualities": home_quality,
        "qualities": qualities,
        "edge": component_edge(home_scores, away_scores),
    }


def build_training_edges(
    raw_games: Iterable[Mapping[str, object]],
    normalization: Mapping[str, object],
    home_win_by_game: Mapping[int, int],
) -> list[dict[str, object]]:
    rows = []
    for raw in sorted(
        _eligible_rows(raw_games), key=lambda row: (row["official_date"], row["game_pk"])
    ):
        game_pk = raw.get("game_pk")
        outcome = home_win_by_game.get(game_pk)
        if outcome not in (0, 1) or isinstance(outcome, bool):
            raise ValueError("every eligible training game requires a binary outcome")
        rows.append(
            {
                "game_pk": game_pk,
                "official_date": raw["official_date"],
                "edge": score_raw_game(raw, normalization)["edge"],
                "home_win": outcome,
            }
        )
    return rows


def build_candidate_components(
    games: Iterable[Mapping[str, object]],
    raw_games: Iterable[Mapping[str, object]],
    normalization: Mapping[str, object],
) -> list[dict[str, object]]:
    by_pk = {raw["game_pk"]: raw for raw in _eligible_rows(raw_games)}
    rows = []
    for game in sorted(games, key=lambda row: (row["official_date"], row["game_pk"])):
        raw = by_pk.get(game.get("game_pk"))
        if raw is None:
            continue
        scored = score_raw_game(raw, normalization)
        starter_bf = []
        for side in ("away", "home"):
            value = raw[side]["starter"].get("career_bf")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("raw starter career_bf must be a nonnegative integer")
            starter_bf.append(value)
        completeness = overall_completeness(scored["qualities"])
        row = {
            "game_pk": game["game_pk"],
            "official_date": game["official_date"],
            "scheduled_start_utc": game["scheduled_start_utc"],
            "away_team": game["away_team"],
            "home_team": game["home_team"],
            "away_scores": scored["away_scores"],
            "home_scores": scored["home_scores"],
            "qualities": scored["qualities"],
            "starter_career_bf": min(starter_bf),
            "critical_failure": False,
            "unresolved_context": any(
                scored["qualities"][name] < 1.0
                for name in ("environment", "defense", "schedule")
            ),
            "lineup_provenance": raw["lineup_provenance"],
            "data_status": "COMPLETE" if completeness >= 1.0 else "INCOMPLETE",
        }
        marker = game.get("doubleheader_marker")
        if marker in {"GAME1", "GAME2"}:
            row["doubleheader_marker"] = marker
        rows.append(row)
    return rows


def build_raw_features_by_date(
    games: Iterable[Mapping[str, object]],
    history_factory: Callable[[Mapping[str, object]], object],
    feature_builder: Callable[[Mapping[str, object], object], object],
) -> list[object]:
    """Build one history and reuse its as-of indexes for an entire season."""
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    seen = set()
    for game in games:
        game_pk = game.get("game_pk")
        if game_pk in seen:
            raise ValueError("duplicate target game_pk")
        seen.add(game_pk)
        grouped[str(game.get("official_date"))].append(game)
    if not grouped:
        return []
    first_day = min(grouped)
    history = history_factory(min(grouped[first_day], key=lambda game: game["game_pk"]))
    output = []
    for day in sorted(grouped):
        daily = sorted(grouped[day], key=lambda game: game["game_pk"])
        output.extend(feature_builder(game, history) for game in daily)
    return output


def prepare_locked_outcomes(
    locked_rows: Iterable[Mapping[str, object]],
    schedule_games: Iterable[Mapping[str, object]],
) -> tuple[list[dict[str, object]], dict[int, str]]:
    """Prepare official outcomes and D-1 win-percentage baselines after lock."""
    locked = {row["game_pk"]: row for row in locked_rows}
    records_by_game: dict[object, list[Mapping[str, object]]] = defaultdict(list)
    for game in schedule_games:
        records_by_game[game["game_pk"]].append(game)
    reconciled_games = []
    for game_pk, records in records_by_game.items():
        if len(records) == 1:
            reconciled_games.append(records[0])
            continue
        finals = [
            game
            for game in records
            if game.get("status") in {"Final", "Completed Early"}
        ]
        if len(finals) > 1:
            raise ValueError(f"multiple official Final records for game_pk {game_pk}")
        if len(finals) != 1:
            raise ValueError(f"ambiguous duplicate schedule records for game_pk {game_pk}")
        final = finals[0]
        identity = tuple(final.get(key) for key in ("official_date", "away_team", "home_team"))
        for stale in records:
            if stale is final:
                continue
            stale_identity = tuple(
                stale.get(key) for key in ("official_date", "away_team", "home_team")
            )
            result_free_postponed = (
                stale.get("status") == "Postponed"
                and stale.get("away_score") is None
                and stale.get("home_score") is None
                and stale.get("away_is_winner") is None
                and stale.get("home_is_winner") is None
            )
            if stale_identity != identity or not result_free_postponed:
                raise ValueError(
                    f"ambiguous duplicate schedule records for game_pk {game_pk}"
                )
        reconciled_games.append(final)
    games_by_day: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for game in reconciled_games:
        games_by_day[str(game.get("official_date"))].append(game)
    wins: defaultdict[str, int] = defaultdict(int)
    losses: defaultdict[str, int] = defaultdict(int)
    outcomes = []
    baseline: dict[int, str] = {}
    for day in sorted(games_by_day):
        daily = sorted(games_by_day[day], key=lambda row: row["game_pk"])
        for game in daily:
            game_pk = game["game_pk"]
            if game_pk not in locked:
                continue
            if game.get("status") in {"Final", "Completed Early"}:
                away, home = game["away_team"], game["home_team"]
                away_games = wins[away] + losses[away]
                home_games = wins[home] + losses[home]
                away_pct = wins[away] / away_games if away_games else None
                home_pct = wins[home] / home_games if home_games else None
                if away_pct is not None and home_pct is not None and away_pct != home_pct:
                    baseline[game_pk] = away if away_pct > home_pct else home
                if game.get("away_is_winner") is True and game.get("home_is_winner") is False:
                    winner = away
                elif game.get("home_is_winner") is True and game.get("away_is_winner") is False:
                    winner = home
                else:
                    raise ValueError("official Final outcome is inconsistent")
                outcomes.append(
                    {"game_pk": game_pk, "actual_winner": winner, "status": "Final"}
                )
            else:
                outcomes.append(
                    {"game_pk": game_pk, "actual_winner": "", "status": "VOID"}
                )
        for game in daily:
            if game.get("status") not in {"Final", "Completed Early"}:
                continue
            away, home = game["away_team"], game["home_team"]
            if game.get("away_is_winner") is True and game.get("home_is_winner") is False:
                wins[away] += 1
                losses[home] += 1
            elif game.get("home_is_winner") is True and game.get("away_is_winner") is False:
                wins[home] += 1
                losses[away] += 1
            else:
                raise ValueError("official Final outcome is inconsistent")
    if set(locked) != {row["game_pk"] for row in outcomes}:
        raise ValueError("every locked game must exist in official schedule")
    outcomes.sort(key=lambda row: row["game_pk"])
    return outcomes, dict(sorted(baseline.items()))


__all__ = [
    "METRIC_SPECS",
    "build_candidate_components",
    "build_raw_features_by_date",
    "build_training_edges",
    "compute_normalization",
    "prepare_locked_outcomes",
    "score_raw_game",
    "validate_normalization",
]
