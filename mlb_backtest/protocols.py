from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile

from mlb_backtest.diagnostics import (
    canonical_json_bytes,
    deep_freeze,
    sha256_file,
)


V52_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "model_version",
        "protocol",
        "component_weights",
        "internal_metrics",
        "shrinkage_constants",
        "probability_clip",
        "score_rules",
        "ranking",
        "completeness",
        "uncertainty",
        "tier_cascade",
        "eligibility",
        "provenance",
        "change_control",
    }
)
COMPONENT_WEIGHTS = MappingProxyType(
    {
        "starter": 0.35,
        "offense": 0.25,
        "bullpen": 0.20,
        "environment": 0.10,
        "defense": 0.05,
        "schedule": 0.05,
    }
)
EXPECTED_DIRECTIONS = {
    "starter": {
        "season_xwoba": -1,
        "k_minus_bb": 1,
        "fip": -1,
        "last5_xwoba": -1,
        "expected_ip": 1,
        "velo_change": 1,
    },
    "offense": {
        "platoon_xwoba": 1,
        "last14_xwoba": 1,
        "bb_minus_k": 1,
        "barrel_rate": 1,
        "iso": 1,
    },
    "bullpen": {
        "season_xwoba": -1,
        "k_minus_bb": 1,
        "last14_xwoba": -1,
        "availability": 1,
    },
}
INTERNAL_WEIGHTS = MappingProxyType(
    {
        "starter": MappingProxyType(
            {
                "season_xwoba": 0.35,
                "k_minus_bb": 0.20,
                "fip": 0.15,
                "last5_xwoba": 0.15,
                "expected_ip": 0.10,
                "velo_change": 0.05,
            }
        ),
        "offense": MappingProxyType(
            {
                "platoon_xwoba": 0.50,
                "last14_xwoba": 0.20,
                "bb_minus_k": 0.15,
                "barrel_rate": 0.10,
                "iso": 0.05,
            }
        ),
        "bullpen": MappingProxyType(
            {
                "season_xwoba": 0.35,
                "k_minus_bb": 0.20,
                "last14_xwoba": 0.15,
                "availability": 0.30,
            }
        ),
        "environment": MappingProxyType({"park": 0.60, "weather_roof": 0.40}),
        "defense": MappingProxyType(
            {"oaa": 0.55, "framing": 0.30, "blocking_throwing": 0.15}
        ),
    }
)
SCHEDULE_ADJUSTMENTS = MappingProxyType(
    {
        "rest_previous_date": 12,
        "three_days_same_home_city": 5,
        "previous_game_10_plus_innings": -12,
        "travel_1000_plus_no_rest": -10,
        "cross_2_plus_timezones_no_rest": -6,
        "scheduled_doubleheader": -8,
    }
)
_INTERNAL_METRIC_LITERAL_SOURCE = {
    component: {
        metric: {
            "weight": weight,
            "direction": EXPECTED_DIRECTIONS[component][metric],
        }
        for metric, weight in INTERNAL_WEIGHTS[component].items()
    }
    for component in ("starter", "offense", "bullpen")
}
_INTERNAL_METRIC_LITERAL_SOURCE.update(
    {
        "environment": {
            metric: {"weight": weight, "direction": 1}
            for metric, weight in INTERNAL_WEIGHTS["environment"].items()
        },
        "defense": {
            metric: {"weight": weight, "direction": 1}
            for metric, weight in INTERNAL_WEIGHTS["defense"].items()
        },
        "schedule": {
            "start_score": 50,
            "clip": [0, 100],
            "adjustments": dict(SCHEDULE_ADJUSTMENTS),
            "same_date_target_events_forbidden": True,
        },
    }
)
INTERNAL_METRIC_LITERALS = deep_freeze(_INTERNAL_METRIC_LITERAL_SOURCE)
if not isinstance(INTERNAL_METRIC_LITERALS, Mapping):
    raise TypeError("internal metric literal freeze failed")
del _INTERNAL_METRIC_LITERAL_SOURCE
SHRINKAGE_CONSTANTS = MappingProxyType(
    {
        "starter": 150,
        "hitter_split": 150,
        "recent_form": 100,
        "bullpen": 300,
        "defense_catcher": 500,
    }
)
V52_RANKING = (
    "pick_probability_desc",
    "data_completeness_score_desc",
    "absolute_starter_edge_desc",
    "lock_basis_start_utc_asc",
    "gamePk_asc",
)
CHANGE_UNCHANGED = (
    "component_weights",
    "internal_metric_weights_and_signs",
    "shrinkage_constants",
    "clipping_and_neutralization",
    "missing_weight_no_redistribution",
    "score_construction",
    "probability_clip",
    "predicted_side_rule",
    "ranking_order",
    "slot_opportunity_method",
)
CHANGE_CHANGED = (
    "rolling_origin_oof_probability_calibration",
    "confidence_renamed_data_completeness",
    "explicit_participant_provenance",
    "tier_unrated_integrity_gate",
    "historical_and_prospective_sample_separation",
)
CHANGE_FORBIDDEN = (
    "bullpen_weight_or_sign",
    "season_record_feature",
    "probability_cutoff",
    "home_adjustment",
    "any_predictive_change_selected_from_2025",
)

RUN_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "model_version",
        "label",
        "target_count",
        "allowed_target_years",
        "forbidden_target_years",
        "folds",
        "date_queue",
        "selection",
        "terminal_contract",
        "date_state_precedence",
        "outcome_isolation",
        "paired_denominators",
        "evidence_labels",
        "source_requirements",
        "namespaces",
        "model_card_status",
    }
)
FOLD_REQUIRED_KEYS = frozenset(
    {
        "fold_id",
        "target_year",
        "target_dates",
        "normalization_years",
        "oof_calibration",
        "latest_training_date",
        "model_card_path",
        "model_card_status",
    }
)
OOF_ROW_KEYS = frozenset({"target_year", "normalization_years"})
TERMINAL_KEYS = frozenset(
    {
        "timezone",
        "deadline_days",
        "deadline_time",
        "status_mapping",
        "doubleheader_order",
        "replacement_rule",
        "late_result_rule",
    }
)
NAMESPACE_KEYS = frozenset(
    {"raw", "features", "locked", "outcomes", "reports", "audit"}
)
MINIMUM_SOURCE_NAMES = (
    "mlb_schedule_game_feed",
    "baseball_savant_statcast",
    "mlb_stats_pitching",
    "venue_team_timezone_table",
    "prior_schedule_history",
)
OPTIONAL_SOURCE_NAMES = (
    "environment",
    "defense",
    "catcher",
    "injury_restriction",
    "market_odds",
)
HISTORICAL_DATE_QUEUE = {
    "order": "official_date_ascending",
    "start": "2022-opening-day",
    "end": "2024-regular-season-end",
    "exclude": ["spring", "exhibition", "all-star", "postseason", "2025"],
}
HISTORICAL_SELECTION = {
    "eligible_top1_per_date": 1,
    "graded_target": 500,
    "same_date_rank2_replacement": False,
    "void_replacement": "next_later_regular_season_date_with_newly_graded_top1",
}
HISTORICAL_TERMINAL = {
    "timezone": "America/New_York",
    "deadline_days": 7,
    "deadline_time": "23:59:59",
    "status_mapping": {
        "Final_unequal_with_winner": "GRADED",
        "Completed_Early_unequal_with_winner": "GRADED",
        "postlock_canceled_unresolved": "VOID:POSTLOCK_CANCELED",
        "postlock_postponed_unresolved": "VOID:POSTLOCK_POSTPONED",
        "suspended_or_delayed_before_deadline": "PENDING",
        "pending_at_deadline": "VOID:DEADLINE_UNRESOLVED",
        "official_tie": "VOID:TIE",
        "abandoned": "VOID:ABANDONED",
        "no_contest": "VOID:NO_CONTEST",
        "missing_winner": "VOID:MISSING_WINNER",
        "equal_terminal_scores": "VOID:EQUAL_TERMINAL_SCORE",
    },
    "doubleheader_order": ["lock_basis_start_utc", "gamePk"],
    "replacement_rule": "strictly_later_date_never_same_date_rank2_never_2025",
    "late_result_rule": "audit_annotation_only_never_restore_membership",
}
HISTORICAL_DENOMINATORS = {
    "model": 500,
    "home": 500,
    "brier": 500,
    "log_loss": 500,
    "wilson": 500,
    "streak": 500,
    "season": "N_season",
    "market": "N_market",
}
HISTORICAL_OUTCOME_ISOLATION = {
    "feature_and_lock_import_outcomes": False,
    "lock_namespace": "data/v5_2_qd/historical/locked",
    "outcome_namespace": "data/v5_2_qd/historical/outcomes",
    "join_after_all_date_locks_sealed": True,
}
FOLD_LITERALS = (
    {
        "fold_id": "H22",
        "target_year": 2022,
        "target_dates": {"start": "2022-04-07", "end": "2022-10-05"},
        "normalization_years": [2018, 2019, 2020, 2021],
        "oof_calibration": [
            {"target_year": 2019, "normalization_years": [2018]},
            {"target_year": 2020, "normalization_years": [2018, 2019]},
            {
                "target_year": 2021,
                "normalization_years": [2018, 2019, 2020],
            },
        ],
        "latest_training_date": "2021-12-31",
        "model_card_path": "models/v5_2_qd/folds/H22/model_card.json",
        "model_card_status": "UNFITTED_BLOCKED_BY_PREFLIGHT",
    },
    {
        "fold_id": "H23",
        "target_year": 2023,
        "target_dates": {"start": "2023-03-30", "end": "2023-10-01"},
        "normalization_years": [2018, 2019, 2020, 2021, 2022],
        "oof_calibration": [
            {"target_year": year, "normalization_years": list(range(2018, year))}
            for year in range(2019, 2023)
        ],
        "latest_training_date": "2022-12-31",
        "model_card_path": "models/v5_2_qd/folds/H23/model_card.json",
        "model_card_status": "UNFITTED_BLOCKED_BY_PREFLIGHT",
    },
    {
        "fold_id": "H24",
        "target_year": 2024,
        "target_dates": {"start": "2024-03-20", "end": "2024-09-30"},
        "normalization_years": [2018, 2019, 2020, 2021, 2022, 2023],
        "oof_calibration": [
            {"target_year": year, "normalization_years": list(range(2018, year))}
            for year in range(2019, 2024)
        ],
        "latest_training_date": "2023-12-31",
        "model_card_path": "models/v5_2_qd/folds/H24/model_card.json",
        "model_card_status": "UNFITTED_BLOCKED_BY_PREFLIGHT",
    },
)
FOLD_LITERALS = deep_freeze(list(FOLD_LITERALS))
HISTORICAL_DATE_QUEUE = deep_freeze(HISTORICAL_DATE_QUEUE)
HISTORICAL_SELECTION = deep_freeze(HISTORICAL_SELECTION)
HISTORICAL_TERMINAL = deep_freeze(HISTORICAL_TERMINAL)
HISTORICAL_DENOMINATORS = deep_freeze(HISTORICAL_DENOMINATORS)
HISTORICAL_OUTCOME_ISOLATION = deep_freeze(HISTORICAL_OUTCOME_ISOLATION)

WEIGHT_KEYS = frozenset(COMPONENT_WEIGHTS)
SHRINKAGE_KEYS = frozenset(SHRINKAGE_CONSTANTS)
SCORE_RULE_KEYS = frozenset(
    {
        "neutral_score",
        "missing_weight_redistribution",
        "probability_transform",
        "predicted_side",
    }
)
COMPLETENESS_KEYS = frozenset(
    {"formula", "quality_clip", "legacy_audit_field", "agreement_affects_score"}
)
UNCERTAINTY_KEYS = frozenset({"high", "medium", "low"})
PROVENANCE_KEYS = frozenset(
    {"historical", "prospective_official", "prospective_shadow"}
)
CHANGE_CONTROL_KEYS = frozenset(
    {"reset_fields", "presentation_only_fields", "forbidden_2025_derived_changes"}
)
TIER_ROW_KEYS = frozenset(
    {"tier", "critical", "uncertainty", "min_probability", "min_completeness"}
)
ELIGIBILITY_ROW_KEYS = frozenset(
    {"event", "historical", "prospective", "disposition", "reason_code", "run_invalid"}
)

ELIGIBILITY_DISPOSITIONS = MappingProxyType(
    {
        "missing_game_id": ("EXCLUDE", "MISSING_GAME_ID"),
        "invalid_competition": ("EXCLUDE", "INVALID_COMPETITION"),
        "missing_teams": ("EXCLUDE", "MISSING_TEAMS"),
        "missing_start": ("EXCLUDE", "MISSING_START_TIME"),
        "missing_venue": ("EXCLUDE", "MISSING_VENUE"),
        "missing_historical_participants": (
            "EXCLUDE",
            "MISSING_RECONSTRUCTED_PARTICIPANTS",
        ),
        "model_card_or_source_invalid": (
            "RUN_INVALID",
            "MODEL_CARD_OR_SOURCE_INVALID",
        ),
        "partial_t45": ("SHADOW_UNRATED", "PARTIAL_T45_PARTICIPANTS"),
        "provenance_mismatch": ("EXCLUDE", "PROVENANCE_MISMATCH"),
        "missing_required_feature": (
            "EXCLUDE",
            "MISSING_REQUIRED_FEATURE_INPUT",
        ),
        "as_of_violation": ("RUN_INVALID", "AS_OF_VIOLATION"),
        "environment_no_asof": (
            "RETAIN_NEUTRAL",
            "ENV_NEUTRAL_NO_ASOF_SOURCE",
        ),
        "defense_no_asof": (
            "RETAIN_NEUTRAL",
            "DEF_NEUTRAL_NO_ASOF_SOURCE",
        ),
        "prelock_not_scheduled": ("EXCLUDE", "PRELOCK_NOT_SCHEDULED"),
    }
)
ELIGIBILITY_MATRIX = MappingProxyType(
    {
        "missing_game_id": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "MISSING_GAME_ID",
            False,
        ),
        "invalid_competition": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "INVALID_COMPETITION",
            False,
        ),
        "missing_teams": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "MISSING_TEAMS",
            False,
        ),
        "missing_start": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "MISSING_START_TIME",
            False,
        ),
        "missing_venue": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "MISSING_VENUE",
            False,
        ),
        "model_card_or_source_invalid": (
            "RUN_INVALID",
            "RUN_INVALID",
            "RUN_INVALID",
            "MODEL_CARD_OR_SOURCE_INVALID",
            True,
        ),
        "missing_historical_participants": (
            "EXCLUDE",
            "NOT_APPLICABLE",
            "EXCLUDE",
            "MISSING_RECONSTRUCTED_PARTICIPANTS",
            False,
        ),
        "partial_t45": (
            "NOT_APPLICABLE",
            "SHADOW_UNRATED",
            "SHADOW_UNRATED",
            "PARTIAL_T45_PARTICIPANTS",
            False,
        ),
        "provenance_mismatch": (
            "EXCLUDE_FROM_HISTORICAL_POOL",
            "EXCLUDE_FROM_OFFICIAL_POOL",
            "EXCLUDE",
            "PROVENANCE_MISMATCH",
            False,
        ),
        "missing_required_feature": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "MISSING_REQUIRED_FEATURE_INPUT",
            False,
        ),
        "as_of_violation": (
            "RUN_INVALID",
            "RUN_INVALID",
            "RUN_INVALID",
            "AS_OF_VIOLATION",
            True,
        ),
        "environment_no_asof": (
            "RETAIN_NEUTRAL",
            "RETAIN_NEUTRAL",
            "RETAIN_NEUTRAL",
            "ENV_NEUTRAL_NO_ASOF_SOURCE",
            False,
        ),
        "defense_no_asof": (
            "RETAIN_NEUTRAL",
            "RETAIN_NEUTRAL",
            "RETAIN_NEUTRAL",
            "DEF_NEUTRAL_NO_ASOF_SOURCE",
            False,
        ),
        "prelock_not_scheduled": (
            "EXCLUDE",
            "EXCLUDE",
            "EXCLUDE",
            "PRELOCK_NOT_SCHEDULED",
            False,
        ),
    }
)


@dataclass(frozen=True)
class V52Config:
    model_version: str
    component_weights: Mapping[str, float]
    shrinkage_constants: Mapping[str, int]
    probability_clip: tuple[float, float]
    ranking: tuple[str, ...]
    raw: Mapping[str, object]


@dataclass(frozen=True)
class FoldSpec:
    fold_id: str
    target_year: int
    target_dates: tuple[str, str]
    normalization_years: tuple[int, ...]
    oof_calibration: tuple[tuple[int, tuple[int, ...]], ...]
    latest_training_date: str
    model_card_path: str
    model_card_status: str
    raw: Mapping[str, object]


@dataclass(frozen=True)
class RunSpec:
    run_id: str
    target_count: int
    allowed_target_years: tuple[int, ...]
    forbidden_target_years: tuple[int, ...]
    folds: tuple[FoldSpec, ...]
    terminal_contract: Mapping[str, object]
    date_state_precedence: tuple[str, ...]
    raw: Mapping[str, object]


def require_exact_keys(
    value: object, expected: frozenset[str], path: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{path} keys differ; missing={sorted(expected - actual)} "
            f"unknown={sorted(actual - expected)}"
        )
    return value


def _validate_literal(value: object, expected: object, path: str) -> None:
    """Recursively enforce closed keys, list shape, scalar type, and value."""
    if isinstance(expected, Mapping):
        obj = require_exact_keys(value, frozenset(expected), path)
        for key, expected_value in expected.items():
            _validate_literal(obj[key], expected_value, f"{path}.{key}")
        return
    if isinstance(expected, list):
        if not isinstance(value, list) or len(value) != len(expected):
            raise ValueError(f"{path} list shape differs")
        for index, (item, expected_item) in enumerate(
            zip(value, expected, strict=True)
        ):
            _validate_literal(item, expected_item, f"{path}[{index}]")
        return
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{path} frozen value differs")


def parse_fold(raw: object) -> FoldSpec:
    obj = require_exact_keys(raw, FOLD_REQUIRED_KEYS, "fold")
    if not isinstance(obj["oof_calibration"], list):
        raise ValueError("fold.oof_calibration must be a list")
    oof: list[tuple[int, tuple[int, ...]]] = []
    for index, row in enumerate(obj["oof_calibration"]):
        item = require_exact_keys(row, OOF_ROW_KEYS, f"fold.oof_calibration[{index}]")
        if not isinstance(item["normalization_years"], list):
            raise ValueError(
                f"fold.oof_calibration[{index}].normalization_years must be a list"
            )
        oof.append(
            (
                int(item["target_year"]),
                tuple(int(year) for year in item["normalization_years"]),
            )
        )
    target_dates = require_exact_keys(
        obj["target_dates"], frozenset({"start", "end"}), "fold.target_dates"
    )
    if not isinstance(obj["normalization_years"], list):
        raise ValueError("fold.normalization_years must be a list")
    fold = FoldSpec(
        fold_id=str(obj["fold_id"]),
        target_year=int(obj["target_year"]),
        target_dates=(str(target_dates["start"]), str(target_dates["end"])),
        normalization_years=tuple(int(year) for year in obj["normalization_years"]),
        oof_calibration=tuple(oof),
        latest_training_date=str(obj["latest_training_date"]),
        model_card_path=str(obj["model_card_path"]),
        model_card_status=str(obj["model_card_status"]),
        raw=deep_freeze(obj),
    )
    if (
        obj["model_card_status"] != "UNFITTED_BLOCKED_BY_PREFLIGHT"
        or Path(str(obj["model_card_path"])).name != "model_card.json"
    ):
        raise ValueError("fold model-card path/status differs")
    validate_fold(fold)
    return fold


def parse_run_spec(raw: object) -> RunSpec:
    obj = require_exact_keys(raw, RUN_REQUIRED_KEYS, "run")
    require_exact_keys(obj["terminal_contract"], TERMINAL_KEYS, "terminal_contract")
    require_exact_keys(obj["namespaces"], NAMESPACE_KEYS, "namespaces")
    if not isinstance(obj["folds"], list):
        raise ValueError("run.folds must be a list")
    for fold_index, fold_row in enumerate(obj["folds"]):
        fold_obj = require_exact_keys(
            fold_row, FOLD_REQUIRED_KEYS, f"run.folds[{fold_index}]"
        )
        require_exact_keys(
            fold_obj["target_dates"],
            frozenset({"start", "end"}),
            f"run.folds[{fold_index}].target_dates",
        )
        if not isinstance(fold_obj["oof_calibration"], list):
            raise ValueError(f"run.folds[{fold_index}].oof_calibration must be a list")
        for oof_index, oof_row in enumerate(fold_obj["oof_calibration"]):
            require_exact_keys(
                oof_row,
                OOF_ROW_KEYS,
                f"run.folds[{fold_index}].oof_calibration[{oof_index}]",
            )
    try:
        _validate_literal(obj, expected_historical_run(), "run")
    except ValueError as exc:
        raise ValueError("historical run differs from frozen values") from exc
    folds = tuple(parse_fold(row) for row in obj["folds"])
    run = RunSpec(
        run_id=str(obj["run_id"]),
        target_count=int(obj["target_count"]),
        allowed_target_years=tuple(int(year) for year in obj["allowed_target_years"]),
        forbidden_target_years=tuple(
            int(year) for year in obj["forbidden_target_years"]
        ),
        folds=folds,
        terminal_contract=deep_freeze(obj["terminal_contract"]),
        date_state_precedence=tuple(
            str(state) for state in obj["date_state_precedence"]
        ),
        raw=deep_freeze(obj),
    )
    validate_run(run)
    return run


def load_run_spec(path: Path) -> RunSpec:
    return parse_run_spec(json.loads(path.read_text(encoding="utf-8")))


def validate_fold(fold: FoldSpec) -> None:
    if (
        not isinstance(fold.fold_id, str)
        or isinstance(fold.target_year, bool)
        or not isinstance(fold.target_year, int)
        or not isinstance(fold.target_dates, tuple)
        or len(fold.target_dates) != 2
        or not all(isinstance(value, str) for value in fold.target_dates)
        or not isinstance(fold.normalization_years, tuple)
        or not all(
            isinstance(year, int) and not isinstance(year, bool)
            for year in fold.normalization_years
        )
        or not isinstance(fold.oof_calibration, tuple)
        or not isinstance(fold.latest_training_date, str)
        or not isinstance(fold.model_card_path, str)
        or not isinstance(fold.model_card_status, str)
        or not isinstance(fold.raw, Mapping)
    ):
        raise ValueError("fold typed fields are invalid")
    expected_by_id = {str(item["fold_id"]): item for item in FOLD_LITERALS}
    if fold.fold_id not in expected_by_id:
        raise ValueError("unknown fold ID")
    start = _canonical_date(fold.target_dates[0], "fold target start")
    end = _canonical_date(fold.target_dates[1], "fold target end")
    latest_training = _canonical_date(
        fold.latest_training_date, "fold latest training date"
    )
    if start > end:
        raise ValueError("fold target start must not follow end")
    if start.year != fold.target_year or end.year != fold.target_year:
        raise ValueError("fold target dates must match target year")
    if latest_training >= start or latest_training.year >= fold.target_year:
        raise ValueError("fold latest training date must precede target")
    if fold.normalization_years != tuple(sorted(set(fold.normalization_years))):
        raise ValueError(
            "fold normalization years must be strictly ascending and unique"
        )
    if any(year >= fold.target_year for year in fold.normalization_years):
        raise ValueError("normalization contains target or future season")
    if fold.normalization_years != tuple(range(2018, fold.target_year)):
        raise ValueError("fold normalization years differ from exact past range")
    if not all(
        isinstance(row, tuple)
        and len(row) == 2
        and isinstance(row[0], int)
        and not isinstance(row[0], bool)
        and isinstance(row[1], tuple)
        and all(isinstance(year, int) and not isinstance(year, bool) for year in row[1])
        for row in fold.oof_calibration
    ):
        raise ValueError("fold OOF calibration typed fields are invalid")
    oof_targets = tuple(target for target, _ in fold.oof_calibration)
    if oof_targets != tuple(sorted(set(oof_targets))):
        raise ValueError("fold OOF target years must be unique and strictly ascending")
    if oof_targets != tuple(range(2019, fold.target_year)):
        raise ValueError("fold OOF target years differ from exact rolling-origin range")
    for target_year, normalization_years in fold.oof_calibration:
        if any(year >= target_year for year in normalization_years):
            raise ValueError("OOF normalization contains target or future season")
        if normalization_years != tuple(range(2018, target_year)):
            raise ValueError(
                "fold OOF rolling-origin pool differs from exact past range"
            )
    expected = expected_by_id[fold.fold_id]
    try:
        _validate_literal(
            json.loads(canonical_json_bytes(fold.raw)),
            json.loads(canonical_json_bytes(expected)),
            "fold.raw",
        )
    except ValueError as exc:
        raise ValueError("fold raw differs from frozen contract") from exc
    raw = fold.raw
    typed_projection = {
        "fold_id": fold.fold_id,
        "target_year": fold.target_year,
        "target_dates": {"start": fold.target_dates[0], "end": fold.target_dates[1]},
        "normalization_years": list(fold.normalization_years),
        "oof_calibration": [
            {"target_year": target, "normalization_years": list(pool)}
            for target, pool in fold.oof_calibration
        ],
        "latest_training_date": fold.latest_training_date,
        "model_card_path": fold.model_card_path,
        "model_card_status": fold.model_card_status,
    }
    if canonical_json_bytes(typed_projection) != canonical_json_bytes(raw):
        raise ValueError("fold typed fields differ from raw")


def expected_historical_run() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "historical_walkforward_500",
        "model_version": "v5.2-QD",
        "label": "Historical Walk-forward Robustness 500",
        "target_count": 500,
        "allowed_target_years": [2022, 2023, 2024],
        "forbidden_target_years": [2025],
        "folds": json.loads(canonical_json_bytes(FOLD_LITERALS)),
        "date_queue": json.loads(canonical_json_bytes(HISTORICAL_DATE_QUEUE)),
        "selection": json.loads(canonical_json_bytes(HISTORICAL_SELECTION)),
        "terminal_contract": json.loads(canonical_json_bytes(HISTORICAL_TERMINAL)),
        "date_state_precedence": [
            "RUN_INVALID",
            "SYSTEM_MISS",
            "NO_ELIGIBLE_DATE",
            "LOCK_TOP1",
        ],
        "outcome_isolation": json.loads(
            canonical_json_bytes(HISTORICAL_OUTCOME_ISOLATION)
        ),
        "paired_denominators": json.loads(
            canonical_json_bytes(HISTORICAL_DENOMINATORS)
        ),
        "evidence_labels": [
            "RUN_INVALID",
            "ROBUSTNESS_NOT_SUPPORTED",
            "ROBUSTNESS_SUPPORTED_NOT_CONFIRMED",
        ],
        "source_requirements": {
            "minimum": list(MINIMUM_SOURCE_NAMES),
            "optional": list(OPTIONAL_SOURCE_NAMES),
        },
        "namespaces": {
            "raw": "data/v5_2_qd/historical/raw",
            "features": "data/v5_2_qd/historical/features",
            "locked": "data/v5_2_qd/historical/locked",
            "outcomes": "data/v5_2_qd/historical/outcomes",
            "reports": "reports/v5_2_qd/historical",
            "audit": "audit/v5_2_qd/historical",
        },
        "model_card_status": "UNFITTED_BLOCKED_BY_PREFLIGHT",
    }


def validate_run(run: RunSpec) -> None:
    if (
        not isinstance(run.run_id, str)
        or isinstance(run.target_count, bool)
        or not isinstance(run.target_count, int)
        or not isinstance(run.allowed_target_years, tuple)
        or not isinstance(run.forbidden_target_years, tuple)
        or not all(
            isinstance(year, int) and not isinstance(year, bool)
            for year in (*run.allowed_target_years, *run.forbidden_target_years)
        )
        or not isinstance(run.folds, tuple)
        or not all(isinstance(fold, FoldSpec) for fold in run.folds)
        or not isinstance(run.terminal_contract, Mapping)
        or not isinstance(run.date_state_precedence, tuple)
        or not all(isinstance(state, str) for state in run.date_state_precedence)
        or not isinstance(run.raw, Mapping)
    ):
        raise ValueError("run typed fields are invalid")
    if 2025 in run.allowed_target_years or run.forbidden_target_years != (2025,):
        raise ValueError("2025 is excluded")
    fold_ids = tuple(fold.fold_id for fold in run.folds)
    fold_targets = tuple(fold.target_year for fold in run.folds)
    if not all(isinstance(fold_id, str) for fold_id in fold_ids) or not all(
        isinstance(target, int) and not isinstance(target, bool)
        for target in fold_targets
    ):
        raise ValueError("fold identity fields are invalid")
    if len(set(fold_ids)) != len(fold_ids):
        raise ValueError("fold IDs must be unique")
    if len(set(fold_targets)) != len(fold_targets):
        raise ValueError("fold target years must be unique")
    try:
        _validate_literal(
            json.loads(canonical_json_bytes(run.raw)),
            expected_historical_run(),
            "run.raw",
        )
    except ValueError as exc:
        raise ValueError("historical run differs from frozen values") from exc
    raw = run.raw
    if (
        run.run_id != raw["run_id"]
        or run.target_count != raw["target_count"]
        or run.allowed_target_years != tuple(raw["allowed_target_years"])
        or run.forbidden_target_years != tuple(raw["forbidden_target_years"])
        or canonical_json_bytes([fold.raw for fold in run.folds])
        != canonical_json_bytes(raw["folds"])
        or canonical_json_bytes(run.terminal_contract)
        != canonical_json_bytes(raw["terminal_contract"])
        or run.date_state_precedence != tuple(raw["date_state_precedence"])
    ):
        raise ValueError("run typed fields differ from raw")
    if fold_targets != run.allowed_target_years:
        raise ValueError("fold targets differ")
    for fold in run.folds:
        validate_fold(fold)


def _canonical_date(value: str, label: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(f"{label} must be canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be canonical ISO date")
    return parsed


def expected_v52_config() -> dict[str, object]:
    tier_rows = [
        {
            "tier": "UNRATED",
            "critical": True,
            "uncertainty": None,
            "min_probability": None,
            "min_completeness": None,
        },
        {
            "tier": "Toss-up",
            "critical": False,
            "uncertainty": "High_or_probability_below_.54_or_completeness_below_8",
            "min_probability": None,
            "min_completeness": None,
        },
        {
            "tier": "S",
            "critical": False,
            "uncertainty": "not_High",
            "min_probability": 0.68,
            "min_completeness": 9.0,
        },
        {
            "tier": "A",
            "critical": False,
            "uncertainty": "not_High",
            "min_probability": 0.63,
            "min_completeness": 8.5,
        },
        {
            "tier": "B",
            "critical": False,
            "uncertainty": "not_High",
            "min_probability": 0.58,
            "min_completeness": 8.0,
        },
        {
            "tier": "Lean",
            "critical": False,
            "uncertainty": "not_High",
            "min_probability": 0.54,
            "min_completeness": 8.0,
        },
        {
            "tier": "Toss-up",
            "critical": False,
            "uncertainty": "fallback",
            "min_probability": None,
            "min_completeness": None,
        },
    ]
    eligibility = [
        {
            "event": event,
            "historical": values[0],
            "prospective": values[1],
            "disposition": values[2],
            "reason_code": values[3],
            "run_invalid": values[4],
        }
        for event, values in ELIGIBILITY_MATRIX.items()
    ]
    return {
        "schema_version": 1,
        "model_version": "v5.2-QD",
        "protocol": "Quality-and-Data Discipline",
        "component_weights": dict(COMPONENT_WEIGHTS),
        "internal_metrics": json.loads(canonical_json_bytes(INTERNAL_METRIC_LITERALS)),
        "shrinkage_constants": dict(SHRINKAGE_CONSTANTS),
        "probability_clip": [0.10, 0.90],
        "score_rules": {
            "neutral_score": 50,
            "missing_weight_redistribution": False,
            "probability_transform": "sigmoid(alpha+beta*D/10)",
            "predicted_side": ("home if p_home>.5; away if p_home<.5; none if equal"),
        },
        "ranking": list(V52_RANKING),
        "completeness": {
            "formula": (
                "10*(.35*starter+.25*offense+.20*bullpen+.10*environment+"
                ".05*defense+.05*schedule)"
            ),
            "quality_clip": [0, 1],
            "legacy_audit_field": "legacy_selection_quality_score",
            "agreement_affects_score": False,
        },
        "uncertainty": {
            "high": ["critical_integrity_failure", "completeness_below_7.5"],
            "medium": [
                "7.5<=completeness<9.0",
                "starter_prior_career_bf_below_150",
                "unresolved_pregame_context",
            ],
            "low": ["completeness>=9.0", "no_medium_or_high_condition"],
        },
        "tier_cascade": tier_rows,
        "eligibility": eligibility,
        "provenance": {
            "historical": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "prospective_official": "TIMESTAMP_VERIFIED_T45",
            "prospective_shadow": "TIMESTAMP_PARTIAL_T45",
        },
        "change_control": {
            "reset_fields": [
                "predictive_weight",
                "internal_weight",
                "sign",
                "shrinkage",
                "probability_transform",
                "ranking",
                "eligibility",
                "source_definition",
                "missing_value_behavior",
                "tier_threshold",
            ],
            "presentation_only_fields": ["copy", "layout", "formatting"],
            "forbidden_2025_derived_changes": list(CHANGE_FORBIDDEN),
        },
    }


def parse_v52_config(raw: object) -> V52Config:
    obj = require_exact_keys(raw, V52_REQUIRED_KEYS, "config")
    expected = expected_v52_config()
    _validate_literal(obj, expected, "config")
    serialized = canonical_json_bytes(obj)
    forbidden_keys = {
        "normalization",
        "slot_weights",
        "alpha",
        "beta",
        "fitted_model_cards",
    }

    def collect_keys(value: object) -> set[str]:
        if isinstance(value, Mapping):
            return set(value) | {
                nested for child in value.values() for nested in collect_keys(child)
            }
        if isinstance(value, list):
            return {nested for child in value for nested in collect_keys(child)}
        return set()

    if forbidden_keys & collect_keys(obj):
        raise ValueError("unfitted config contains fitted values")
    # Retain a direct serializability check: NaN and Infinity are not canonical JSON.
    if not serialized:
        raise ValueError("config canonical serialization is empty")
    return V52Config(
        model_version="v5.2-QD",
        component_weights=deep_freeze(obj["component_weights"]),
        shrinkage_constants=deep_freeze(obj["shrinkage_constants"]),
        probability_clip=(0.10, 0.90),
        ranking=V52_RANKING,
        raw=deep_freeze(obj),
    )


def load_v52_config(path: Path) -> V52Config:
    return parse_v52_config(json.loads(path.read_text(encoding="utf-8")))


def finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be finite numeric")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{path} must be finite numeric") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{path} must be finite numeric")
    return converted


def data_completeness_score(
    qualities: Mapping[str, float],
    *,
    component_agreement: object = None,
    legacy_selection_quality_score: object = None,
) -> float:
    del component_agreement, legacy_selection_quality_score
    require_exact_keys(qualities, frozenset(COMPONENT_WEIGHTS), "qualities")
    clipped = {
        name: min(
            1.0,
            max(0.0, finite_number(qualities[name], f"qualities.{name}")),
        )
        for name in COMPONENT_WEIGHTS
    }
    return 10.0 * math.fsum(
        COMPONENT_WEIGHTS[name] * clipped[name] for name in COMPONENT_WEIGHTS
    )


def classify_uncertainty(
    completeness: object,
    starter_prior_career_bf: object,
    unresolved_context: object,
    *,
    critical: object,
) -> str:
    score = finite_number(completeness, "completeness")
    bf = finite_number(starter_prior_career_bf, "starter_prior_career_bf")
    if (
        not 0 <= score <= 10
        or bf < 0
        or not isinstance(unresolved_context, bool)
        or not isinstance(critical, bool)
    ):
        raise ValueError("uncertainty inputs out of bounds/type")
    if critical or score < 7.5:
        return "High"
    if score < 9.0 or bf < 150 or unresolved_context:
        return "Medium"
    return "Low"


def read_legacy_confidence(row: Mapping[str, object]) -> dict[str, float]:
    require_exact_keys(row, frozenset({"confidence"}), "legacy_confidence")
    return {
        "legacy_selection_quality_score": finite_number(row["confidence"], "confidence")
    }


def ranking_key(
    probability: float,
    completeness: float,
    starter_edge_abs: float,
    lock_basis_start_utc: str,
    game_pk: int,
) -> tuple[float, float, float, str, int]:
    p = finite_number(probability, "probability")
    q = finite_number(completeness, "completeness")
    edge = finite_number(starter_edge_abs, "starter_edge_abs")
    if not isinstance(lock_basis_start_utc, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        lock_basis_start_utc,
    ):
        raise ValueError(
            "lock_basis_start_utc must be canonical UTC YYYY-MM-DDTHH:MM:SSZ"
        )
    try:
        parsed_start = datetime.strptime(lock_basis_start_utc, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(
            "lock_basis_start_utc must be canonical UTC YYYY-MM-DDTHH:MM:SSZ"
        ) from exc
    if parsed_start.strftime("%Y-%m-%dT%H:%M:%SZ") != lock_basis_start_utc:
        raise ValueError(
            "lock_basis_start_utc must be canonical UTC YYYY-MM-DDTHH:MM:SSZ"
        )
    if (
        not 0.5 <= p <= 0.9
        or not 0 <= q <= 10
        or edge < 0
        or isinstance(game_pk, bool)
        or not isinstance(game_pk, int)
        or game_pk <= 0
    ):
        raise ValueError("ranking inputs out of bounds/type")
    return (-p, -q, -edge, lock_basis_start_utc, game_pk)


def direction_from_p_home(p_home: float) -> tuple[str | None, float, str | None]:
    probability = finite_number(p_home, "p_home")
    if not 0.1 <= probability <= 0.9:
        raise ValueError("p_home outside probability clip")
    if probability == 0.5:
        return None, 0.5, "NO_DIRECTION_AT_50"
    return (
        "home" if probability > 0.5 else "away",
        max(probability, 1 - probability),
        None,
    )


def optional_neutral(component: str) -> tuple[float, float, str]:
    codes = {
        "environment": "ENV_NEUTRAL_NO_ASOF_SOURCE",
        "defense": "DEF_NEUTRAL_NO_ASOF_SOURCE",
    }
    if component not in codes:
        raise ValueError("only environment and defense are optional neutral components")
    return 50.0, 0.0, codes[component]


def eligibility_disposition(event: str) -> tuple[str, str]:
    try:
        return ELIGIBILITY_DISPOSITIONS[event]
    except KeyError as exc:
        raise ValueError(f"unknown eligibility event: {event}") from exc


def classify_v52(
    probability: float, completeness: float, uncertainty: str, *, critical: bool
) -> str:
    probability = finite_number(probability, "probability")
    completeness = finite_number(completeness, "completeness")
    if (
        not 0.5 <= probability <= 0.9
        or not 0 <= completeness <= 10
        or uncertainty not in {"High", "Medium", "Low"}
        or not isinstance(critical, bool)
    ):
        raise ValueError("tier inputs out of bounds/type")
    if critical:
        return "UNRATED"
    if uncertainty == "High" or probability < 0.54 or completeness < 8.0:
        return "Toss-up"
    if probability >= 0.68 and completeness >= 9.0:
        return "S"
    if probability >= 0.63 and completeness >= 8.5:
        return "A"
    if probability >= 0.58:
        return "B"
    if probability >= 0.54:
        return "Lean"
    return "Toss-up"


CHANGE_KEYS = frozenset(
    {
        "schema_version",
        "model_version",
        "unchanged",
        "changed",
        "forbidden_from_2025_diagnostic",
        "config_sha256",
        "approved_design_commit",
        "approved_design_blob_sha256",
    }
)


def parse_change_contract(raw: object) -> Mapping[str, object]:
    obj = require_exact_keys(raw, CHANGE_KEYS, "change_contract")
    expected = {
        "schema_version": 1,
        "model_version": "v5.2-QD",
        "unchanged": list(CHANGE_UNCHANGED),
        "changed": list(CHANGE_CHANGED),
        "forbidden_from_2025_diagnostic": list(CHANGE_FORBIDDEN),
        "approved_design_commit": "a4aa8d2",
    }
    for key, value in expected.items():
        _validate_literal(obj[key], value, f"change_contract.{key}")
    for key in ("config_sha256", "approved_design_blob_sha256"):
        value = obj[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"change_contract.{key} must be lowercase SHA-256")
    return deep_freeze(obj)


def const_schema(
    raw: Mapping[str, object], required: frozenset[str]
) -> dict[str, object]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "const": raw,
        "required": sorted(required),
        "properties": {key: {} for key in sorted(required)},
        "additionalProperties": False,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def write_historical_contract(root: Path) -> None:
    raw = expected_historical_run()
    parse_run_spec(raw)
    schema = const_schema(raw, RUN_REQUIRED_KEYS)
    schema["properties"]["folds"] = {
        "type": "array",
        "items": {"$ref": "#/$defs/fold"},
    }
    schema["$defs"] = {
        "fold": {
            "type": "object",
            "required": sorted(FOLD_REQUIRED_KEYS),
            "properties": {key: {} for key in sorted(FOLD_REQUIRED_KEYS)},
            "additionalProperties": False,
        }
    }
    _publish_bundle(
        (
            (
                root / "protocols/v5_2_qd/historical_walkforward_500.json",
                canonical_json_bytes(raw) + b"\n",
            ),
            (
                root / "schemas/v5_2_run_spec.schema.json",
                canonical_json_bytes(schema) + b"\n",
            ),
        )
    )


def _write_fsynced_temp(path: Path, contents: bytes, *, mode: int) -> Path:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_bundle_directories(final_paths: tuple[Path, ...]) -> tuple[Path, ...]:
    missing: set[Path] = set()
    for final_path in final_paths:
        candidate = final_path.parent
        while not candidate.exists():
            missing.add(candidate)
            candidate = candidate.parent
    created: list[Path] = []
    try:
        for directory in sorted(missing, key=lambda value: len(value.parts)):
            directory.mkdir(exist_ok=True)
            created.append(directory)
    except BaseException:
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise
    return tuple(sorted(missing, key=lambda value: len(value.parts), reverse=True))


def _publish_bundle(entries: tuple[tuple[Path, bytes], ...]) -> None:
    final_paths = tuple(path for path, _ in entries)
    if len(set(final_paths)) != len(final_paths):
        raise ValueError("protocol bundle paths must be unique")
    created_directories = _ensure_bundle_directories(final_paths)
    temporary_paths: list[Path] = []
    backups: dict[Path, Path | None] = {}
    replaced: list[Path] = []
    try:
        modes = {
            path: stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            for path in final_paths
        }
        for path, contents in entries:
            temporary_paths.append(
                _write_fsynced_temp(path, contents, mode=modes[path])
            )
        for path in final_paths:
            backups[path] = (
                _write_fsynced_temp(path, path.read_bytes(), mode=modes[path])
                if path.exists()
                else None
            )
        for temporary_path, final_path in zip(
            temporary_paths, final_paths, strict=True
        ):
            os.replace(temporary_path, final_path)
            replaced.append(final_path)
        for directory in sorted({path.parent for path in final_paths}):
            _fsync_directory(directory)
    except BaseException:
        rollback_error: BaseException | None = None
        for final_path in reversed(replaced):
            backup_path = backups[final_path]
            try:
                if backup_path is None:
                    final_path.unlink(missing_ok=True)
                else:
                    os.replace(backup_path, final_path)
            except BaseException as error:
                rollback_error = rollback_error or error
        for directory in sorted({path.parent for path in final_paths}):
            try:
                _fsync_directory(directory)
            except BaseException as error:
                rollback_error = rollback_error or error
        if rollback_error is not None:
            raise RuntimeError(
                "v5.2-QD protocol bundle rollback failed"
            ) from rollback_error
        raise
    finally:
        for temporary_path in (*temporary_paths, *backups.values()):
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        for directory in created_directories:
            try:
                directory.rmdir()
            except OSError:
                pass


def approved_design_bytes(root: Path) -> bytes:
    completed = subprocess.run(
        [
            "git",
            "show",
            "a4aa8d2:docs/superpowers/specs/"
            "2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md",
        ],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        raise ValueError("approved design blob unavailable")
    return completed.stdout


def write_v52_config_contracts(
    root: Path, *, approved_design_root: Path | None = None
) -> None:
    root = root.resolve()
    design_root = (
        root if approved_design_root is None else approved_design_root.resolve()
    )
    design_contents = approved_design_bytes(design_root)
    config = expected_v52_config()
    parse_v52_config(config)
    config_path = root / "config/v5_2_qd.json"
    config_contents = canonical_json_bytes(config) + b"\n"
    change = {
        "schema_version": 1,
        "model_version": "v5.2-QD",
        "unchanged": list(CHANGE_UNCHANGED),
        "changed": list(CHANGE_CHANGED),
        "forbidden_from_2025_diagnostic": list(CHANGE_FORBIDDEN),
        "config_sha256": hashlib.sha256(config_contents).hexdigest(),
        "approved_design_commit": "a4aa8d2",
        "approved_design_blob_sha256": hashlib.sha256(design_contents).hexdigest(),
    }
    schema_contents = (
        canonical_json_bytes(const_schema(config, V52_REQUIRED_KEYS)) + b"\n"
    )
    contract_contents = canonical_json_bytes(change) + b"\n"
    _publish_bundle(
        (
            (config_path, config_contents),
            (root / "schemas/v5_2_qd.schema.json", schema_contents),
            (
                root / "protocols/v5_2_qd/change_contract.json",
                contract_contents,
            ),
        )
    )


def load_change_contract(
    root: Path, *, approved_design_root: Path | None = None
) -> Mapping[str, object]:
    root = root.resolve()
    design_root = (
        root if approved_design_root is None else approved_design_root.resolve()
    )
    contract = parse_change_contract(
        json.loads(
            (root / "protocols/v5_2_qd/change_contract.json").read_text(
                encoding="utf-8"
            )
        )
    )
    config_path = root / "config/v5_2_qd.json"
    if contract["config_sha256"] != sha256_file(config_path):
        raise ValueError("change contract config digest differs")
    load_v52_config(config_path)
    expected_design_digest = hashlib.sha256(
        approved_design_bytes(design_root)
    ).hexdigest()
    if contract["approved_design_blob_sha256"] != expected_design_digest:
        raise ValueError("change contract design digest differs")
    return contract


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write deterministic unfitted v5.2-QD protocol contracts."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--approved-design-root", type=Path)
    args = parser.parse_args(argv)
    write_v52_config_contracts(
        args.root,
        approved_design_root=args.approved_design_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
