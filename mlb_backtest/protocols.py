from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
import argparse
import errno
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
from zoneinfo import ZoneInfo

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
PROSPECTIVE_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "model_version",
        "first_permitted_date",
        "fit_years",
        "excluded_fit_years",
        "oof_calibration",
        "card_seal",
        "polling",
        "clock",
        "schedule_changes",
        "participant_provenance",
        "official_pool",
        "shadow_pool",
        "outcome_isolation",
        "date_state_precedence",
        "terminal_contract",
        "milestones",
        "evidence_gate",
        "namespace",
        "pooling_forbidden",
        "backfill_forbidden",
        "freeze_pin",
    }
)
POLLING_KEYS = frozenset(
    {
        "T-90_to_before_T-50_seconds",
        "T-50_to_T-45_seconds",
        "qualifying_window_minutes",
        "max_age_at_lock_minutes",
        "max_ntp_offset_seconds",
    }
)
PROVENANCE_CONTRACT_KEYS = frozenset(
    {"official", "partial", "historical_forbidden"}
)
PROSPECTIVE_POLLING = {
    "T-90_to_before_T-50_seconds": 300,
    "T-50_to_T-45_seconds": 60,
    "qualifying_window_minutes": [-50, -45],
    "max_age_at_lock_minutes": 5,
    "max_ntp_offset_seconds": 2,
}
PROSPECTIVE_DATE_PRECEDENCE = [
    "RUN_INVALID",
    "SYSTEM_MISS",
    "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE",
    "NO_VERIFIED_ELIGIBLE_DATE",
    "LOCK_TOP1",
]
CURRENT_APPROVED_DESIGN_COMMIT = "fb728b3"
CURRENT_APPROVED_DESIGN_SHA256 = (
    "551e9f83e8f10ba7d7db3ee2cc756d4e9493eba756dd118babe74b95b513330c"
)
PROSPECTIVE_CONTRACT_CANONICAL_SHA256 = (
    "2f74e5b5bcba1be033f5955310b4f4261d33a7b7bcecdaa39cc2e021553641bf"
)
POSTLOCK_GENESIS_HASH = "0" * 64
POSTLOCK_RECEIPT_GENESIS_HASH = "0" * 64
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


@dataclass(frozen=True)
class ProspectiveSpec:
    run_id: str
    first_permitted_date: date
    fit_years: tuple[int, ...]
    oof_targets: tuple[int, ...]
    polling: Mapping[str, object]
    official_provenance: str
    namespace: str
    raw: Mapping[str, object]


@dataclass(frozen=True)
class ProspectiveSnapshot:
    game_pk: int
    lock_basis_start_utc: str
    observed_scheduled_start_utc: str
    lock_at_utc: str
    receipt_at_utc: str
    successful: bool
    complete_participants: bool
    ntp_offset_seconds: float
    sequence: int


@dataclass(frozen=True)
class PostlockStartChange:
    game_pk: int
    sequence: int
    previous_effective_start_utc: str
    new_effective_start_utc: str
    observed_at_utc: str
    lock_basis_start_utc: str
    prior_event_hash: str
    event_hash: str


@dataclass(frozen=True)
class PostlockChainState:
    game_pk: int
    lock_basis_start_utc: str
    event_count: int
    head_hash: str
    persisted_at_utc: str
    audit_path: str
    event_log_path: str


@dataclass(frozen=True)
class PostlockChainReceipt:
    game_pk: int
    sequence: int
    head_hash: str
    prior_receipt_hash: str
    persisted_at_utc: str
    lock_basis_start_utc: str
    receipt_hash: str
    audit_path: str


@dataclass(frozen=True)
class _CreatedPostlockReceipt:
    path: Path
    device: int
    inode: int
    contents: bytes
    created_directories: tuple[Path, ...]


class _PostlockReceiptRecoveryError(RuntimeError):
    def __init__(self, message: str, recovery_paths: tuple[Path, ...]) -> None:
        super().__init__(message)
        self.recovery_paths = recovery_paths


@dataclass(frozen=True)
class ProspectiveCandidateRow:
    game_pk: int
    official_date: str
    recorded_at_utc: str
    retrospective_backfill: bool
    p_home: float
    predicted_side: str
    verified_snapshot_id: str
    verified_snapshot_sha256: str
    sealed_lock_snapshot_id: str
    sealed_lock_snapshot_sha256: str
    provenance: str
    lock_state: str
    final_snapshot_classification: str
    critical: bool
    eligibility_status: str
    date_state: str
    data_completeness_score: float
    absolute_starter_edge: float
    lock_basis_start_utc: str
    lock_receipt_at_utc: str
    verified_snapshot: ProspectiveSnapshot | None = None
    snapshot_candidates: tuple[ProspectiveSnapshot, ...] = ()


@dataclass(frozen=True)
class ProspectiveTop1:
    official_date: str
    game_pk: int
    predicted_side: str
    pick_probability: float
    official_rank: int
    verified_snapshot_id: str
    verified_snapshot_sha256: str
    final_lock_receipt_at_utc: str


@dataclass(frozen=True)
class ProspectiveCandidateSlate:
    official_date: str
    expected_candidate_ids: tuple[str, ...]
    expected_candidate_count: int
    candidates_sha256: str
    snapshot_lock_receipts: tuple[Mapping[str, object], ...]
    final_lock_receipt_at_utc: str
    all_locks_sealed: bool
    persisted_at_utc: str
    audit_path: str


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


def _build_prospective_literal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": "prospective_2026",
        "model_version": "v5.2-QD",
        "first_permitted_date": "2026-07-13",
        "fit_years": list(range(2018, 2025)),
        "excluded_fit_years": [2025],
        "oof_calibration": [
            {
                "target_year": year,
                "normalization_years": list(range(2018, year)),
            }
            for year in range(2019, 2025)
        ],
        "card_seal": {
            "deadline": "before_first_counted_pick",
            "season_frozen": True,
            "target_year_outcomes_refit_forbidden": True,
        },
        "polling": dict(PROSPECTIVE_POLLING),
        "clock": {
            "basis": "authoritative_observed_start_and_ntp_receipt_time",
            "qualifying_interval_closed": True,
            "timezone_normalization": "UTC",
            "clock_failure_status": "SYSTEM_MISS_CLOCK",
            "snapshot_record_fields": [
                "gamePk",
                "lock_basis_start_utc",
                "observed_scheduled_start_utc",
                "lock_at_utc",
                "receipt_at_utc",
                "successful",
                "complete_participants",
                "ntp_offset_seconds",
                "sequence",
            ],
            "selection": "latest_successful_complete_eligible_snapshot",
        },
        "schedule_changes": {
            "prelock": "invalidate_prior_snapshots_and_restart_countdown",
            "late_reschedule": (
                "TIMESTAMP_PARTIAL_T45:LATE_RESCHEDULE_UNVERIFIED"
            ),
            "postlock_mutable_field": "latest_effective_start_utc",
            "postlock_start_change": {
                "event": "POSTLOCK_START_CHANGE",
                "trigger": (
                    "new_effective_start_utc_differs_from_"
                    "previous_effective_start_utc"
                ),
                "write_mode": "append_immutable_audit_event",
                "latest_effective_start_utc_update": (
                    "only_by_appending_POSTLOCK_START_CHANGE"
                ),
                "rewrite_forbidden": True,
                "delete_forbidden": True,
                "record_fields": [
                    "gamePk",
                    "sequence",
                    "previous_effective_start_utc",
                    "new_effective_start_utc",
                    "observed_at_utc",
                    "lock_basis_start_utc",
                    "prior_event_hash",
                    "event_hash",
                ],
                "hash_chain": "sha256_canonical_json_with_genesis_zero_hash",
                "sealed_state_path_template": (
                    "audit/v5_2_qd/prospective_2026/"
                    "postlock_chain_state/{gamePk}.json"
                ),
                "sealed_state_fields": [
                    "gamePk",
                    "lock_basis_start_utc",
                    "event_count",
                    "head_hash",
                    "persisted_at_utc",
                    "audit_path",
                    "event_log_path",
                ],
                "event_log_path_template": (
                    "audit/v5_2_qd/prospective_2026/"
                    "postlock_start_changes/{gamePk}.jsonl"
                ),
                "event_log_format": (
                    "canonical_jsonl_one_POSTLOCK_START_CHANGE_per_line"
                ),
                "sealed_state_format": "canonical_json_object_newline",
                "receipt_path_template": (
                    "audit/v5_2_qd/prospective_2026/postlock_chain_receipts/"
                    "{gamePk}/{sequence:08d}-{head_hash}.json"
                ),
                "receipt_write": "O_EXCL_fsync_read_only",
                "game_lock_path_template": (
                    "audit/v5_2_qd/prospective_2026/locks/{gamePk}.lock"
                ),
                "local_threat_boundary": (
                    "API-enforced append-only; arbitrary external deletion remains "
                    "detectable only while a later receipt anchor remains"
                ),
                "state_persistence": (
                    "atomic_event_and_sealed_state_publish_fsync_before_ack"
                ),
            },
            "immutable_fields": [
                "lock_basis_start_utc",
                "lock_at_utc",
                "snapshot_qualification",
                "prediction",
                "provenance",
                "official_rank",
                "shadow_rank",
                "top1",
            ],
            "relock_forbidden": True,
        },
        "participant_provenance": {
            "official": "TIMESTAMP_VERIFIED_T45",
            "partial": "TIMESTAMP_PARTIAL_T45",
            "historical_forbidden": "RECONSTRUCTED_FINAL_PARTICIPANTS",
        },
        "official_pool": {
            "provenance": "TIMESTAMP_VERIFIED_T45",
            "rank_field": "official_rank",
            "eligible_only": True,
            "ranking": list(V52_RANKING),
            "ranking_after": "all_final_verified_t45_candidate_locks_sealed",
            "incomplete_or_unverified_rows_affect_rank": False,
            "partial_rows_affect_rank": False,
            "p_home_equal_half_official": False,
            "candidate_record_fields": [
                "gamePk",
                "official_date",
                "recorded_at_utc",
                "retrospective_backfill",
                "p_home",
                "predicted_side",
                "verified_snapshot_id",
                "verified_snapshot_sha256",
                "sealed_lock_snapshot_id",
                "sealed_lock_snapshot_sha256",
                "provenance",
                "lock_state",
                "final_snapshot_classification",
                "critical",
                "eligibility_status",
                "date_state",
                "data_completeness_score",
                "absolute_starter_edge",
                "lock_basis_start_utc",
                "lock_receipt_at_utc",
            ],
            "candidate_date_state": "CANDIDATE_ACTIVE",
            "candidate_top1_preclaim_forbidden": True,
            "selection_gate": (
                "all_candidate_locks_sealed_and_final_lock_receipt_recorded"
            ),
            "top1_output_fields": [
                "official_date",
                "gamePk",
                "predicted_side",
                "pick_probability",
                "official_rank",
                "verified_snapshot_id",
                "verified_snapshot_sha256",
                "final_lock_receipt_at_utc",
            ],
            "slate_manifest_path_template": (
                "audit/v5_2_qd/prospective_2026/"
                "candidate_slates/{official_date}.json"
            ),
            "slate_lock_path_template": (
                "audit/v5_2_qd/prospective_2026/"
                "candidate_slate_locks/{official_date}.lock"
            ),
            "slate_seal_policy": (
                "immutable_sealed_once_idempotent_exact_same_manifest"
            ),
            "slate_manifest_fields": [
                "official_date",
                "expected_candidate_ids",
                "expected_candidate_count",
                "candidates_sha256",
                "snapshot_lock_receipts",
                "final_lock_receipt_at_utc",
                "all_locks_sealed",
                "persisted_at_utc",
                "audit_path",
            ],
            "all_locks_sealed": "derived_from_every_bound_candidate_lock",
            "final_lock_receipt_at_utc": "max_candidate_lock_receipt_at_utc",
        },
        "shadow_pool": {
            "provenance": "TIMESTAMP_PARTIAL_T45",
            "rank_field": "shadow_rank",
            "tier": "UNRATED",
        },
        "outcome_isolation": {
            "locked_namespace": "data/v5_2_qd/prospective_2026/locked",
            "outcomes_namespace": "data/v5_2_qd/prospective_2026/outcomes",
            "feature_and_lock_import_outcomes": False,
            "prediction_ranking_lock_outcome_store_access": False,
            "join_after_all_final_verified_candidate_locks_sealed": True,
            "locked_prediction_rewrite_forbidden": True,
        },
        "date_state_precedence": list(PROSPECTIVE_DATE_PRECEDENCE),
        "terminal_contract": json.loads(canonical_json_bytes(HISTORICAL_TERMINAL)),
        "milestones": [25, 50, 100, 250, 500],
        "evidence_gate": {
            "label": "CONFIRMED_INCREMENTAL_EDGE",
            "minimum_graded": 500,
            "all_integrity_gates_pass": True,
            "accuracy_gt_home": {"required": True, "exact_denominator": 500},
            "accuracy_gt_d1_season_record": {
                "required": True,
                "exact_denominator": "N_season",
            },
            "season_baseline_lower_bound_positive": True,
            "season_baseline_exact_mcnemar_p_lt": 0.05,
            "brier_lt": 0.25,
            "log_loss_lt": 0.6931471806,
            "historical_rows_permitted": False,
        },
        "namespace": "data/v5_2_qd/prospective_2026",
        "pooling_forbidden": True,
        "backfill_forbidden": True,
        "freeze_pin": {
            "approved_design_commit": CURRENT_APPROVED_DESIGN_COMMIT,
            "approved_design_blob_sha256": CURRENT_APPROVED_DESIGN_SHA256,
        },
    }


PROSPECTIVE_LITERAL_ORACLE = deep_freeze(_build_prospective_literal())
if not isinstance(PROSPECTIVE_LITERAL_ORACLE, Mapping):
    raise TypeError("prospective literal oracle freeze failed")


def expected_prospective_spec() -> dict[str, object]:
    return json.loads(canonical_json_bytes(PROSPECTIVE_LITERAL_ORACLE))


def parse_prospective_spec(raw: object) -> ProspectiveSpec:
    obj = require_exact_keys(raw, PROSPECTIVE_REQUIRED_KEYS, "prospective")
    freeze_pin = require_exact_keys(
        obj["freeze_pin"],
        frozenset({"approved_design_commit", "approved_design_blob_sha256"}),
        "prospective.freeze_pin",
    )
    if (
        freeze_pin["approved_design_commit"] != CURRENT_APPROVED_DESIGN_COMMIT
        or freeze_pin["approved_design_blob_sha256"]
        != CURRENT_APPROVED_DESIGN_SHA256
    ):
        raise ValueError("prospective approved design pin differs")
    polling = require_exact_keys(obj["polling"], POLLING_KEYS, "prospective.polling")
    provenance = require_exact_keys(
        obj["participant_provenance"],
        PROVENANCE_CONTRACT_KEYS,
        "prospective.participant_provenance",
    )
    if not isinstance(obj["oof_calibration"], list):
        raise ValueError("prospective.oof_calibration must be a list")
    for index, row in enumerate(obj["oof_calibration"]):
        require_exact_keys(
            row, OOF_ROW_KEYS, f"prospective.oof_calibration[{index}]"
        )
    try:
        _validate_literal(obj, expected_prospective_spec(), "prospective")
    except ValueError as exc:
        if "keys differ" in str(exc):
            raise
        raise ValueError("prospective spec differs from frozen values") from exc
    if (
        hashlib.sha256(canonical_json_bytes(obj)).hexdigest()
        != PROSPECTIVE_CONTRACT_CANONICAL_SHA256
    ):
        raise ValueError("prospective canonical digest differs from frozen contract")
    spec = ProspectiveSpec(
        run_id=str(obj["run_id"]),
        first_permitted_date=date.fromisoformat(str(obj["first_permitted_date"])),
        fit_years=tuple(int(year) for year in obj["fit_years"]),
        oof_targets=tuple(
            int(row["target_year"]) for row in obj["oof_calibration"]
        ),
        polling=deep_freeze(polling),
        official_provenance=str(provenance["official"]),
        namespace=str(obj["namespace"]),
        raw=deep_freeze(obj),
    )
    if (
        spec.first_permitted_date != date(2026, 7, 13)
        or spec.official_provenance != "TIMESTAMP_VERIFIED_T45"
    ):
        raise ValueError("prospective date/provenance differs")
    return spec


def load_prospective_spec(path: Path) -> ProspectiveSpec:
    return parse_prospective_spec(json.loads(path.read_text(encoding="utf-8")))


def eligible_for_prospective_official_pool(row: ProspectiveCandidateRow) -> bool:
    if not isinstance(row, ProspectiveCandidateRow):
        raise ValueError("prospective official pool requires a typed candidate row")
    if (
        isinstance(row.game_pk, bool)
        or not isinstance(row.game_pk, int)
        or row.game_pk <= 0
    ):
        raise ValueError("candidate gamePk must be an exact positive integer")
    try:
        candidate_date = _canonical_date(row.official_date, "official_date")
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate official_date must be canonical") from exc
    if candidate_date < date(2026, 7, 13):
        raise ValueError("candidate official_date precedes first permitted date")
    if not isinstance(row.retrospective_backfill, bool):
        raise ValueError("candidate backfill flag must be boolean")
    if row.retrospective_backfill:
        raise ValueError("candidate retrospective backfill is forbidden")
    probability = finite_number(row.p_home, "candidate.p_home")
    if not 0.1 <= probability <= 0.9 or probability == 0.5:
        raise ValueError("candidate p_home must be directional within [0.1, 0.9]")
    expected_side = "home" if probability > 0.5 else "away"
    if row.predicted_side != expected_side:
        raise ValueError("candidate predicted side is inconsistent with p_home")
    if not isinstance(row.verified_snapshot, ProspectiveSnapshot):
        raise ValueError("candidate must bind a typed verified snapshot")
    if not isinstance(row.snapshot_candidates, tuple) or not all(
        isinstance(snapshot, ProspectiveSnapshot)
        for snapshot in row.snapshot_candidates
    ):
        raise ValueError("candidate snapshot selection history must be a typed tuple")
    if not row.snapshot_candidates:
        raise ValueError("candidate snapshot selection history is empty")
    verified_snapshot = row.verified_snapshot
    classification = classify_snapshot(verified_snapshot)
    selected_snapshot = select_qualifying_snapshot(row.snapshot_candidates)
    if selected_snapshot != verified_snapshot:
        raise ValueError("candidate verified snapshot is not the latest selected snapshot")
    snapshot_raw = _prospective_snapshot_raw(verified_snapshot)
    snapshot_sha256 = hashlib.sha256(canonical_json_bytes(snapshot_raw)).hexdigest()
    snapshot_id = _prospective_snapshot_id(verified_snapshot, snapshot_sha256)
    if (
        not isinstance(row.verified_snapshot_id, str)
        or not row.verified_snapshot_id
        or not isinstance(row.sealed_lock_snapshot_id, str)
        or not isinstance(row.verified_snapshot_sha256, str)
        or not isinstance(row.sealed_lock_snapshot_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", row.verified_snapshot_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", row.sealed_lock_snapshot_sha256)
        or row.verified_snapshot_id != row.sealed_lock_snapshot_id
        or row.verified_snapshot_sha256 != row.sealed_lock_snapshot_sha256
        or row.verified_snapshot_id != snapshot_id
        or row.verified_snapshot_sha256 != snapshot_sha256
    ):
        raise ValueError(
            "candidate canonical verified snapshot binding differs from sealed lock"
        )
    recorded = _canonical_utc_datetime(row.recorded_at_utc, "recorded_at_utc")
    lock_receipt = _canonical_utc_datetime(
        row.lock_receipt_at_utc, "lock_receipt_at_utc"
    )
    lock_basis = _canonical_utc_datetime(
        row.lock_basis_start_utc, "lock_basis_start_utc"
    )
    if lock_receipt != lock_basis - timedelta(minutes=45):
        raise ValueError("candidate lock receipt differs from exact T-45 lock")
    if recorded > lock_receipt or recorded < lock_basis - timedelta(minutes=50):
        raise ValueError(
            "candidate recorded_at is retrospective or outside the verified snapshot window"
        )
    if (
        verified_snapshot.game_pk != row.game_pk
        or verified_snapshot.lock_basis_start_utc != row.lock_basis_start_utc
        or verified_snapshot.lock_at_utc != row.lock_receipt_at_utc
        or verified_snapshot.receipt_at_utc != row.recorded_at_utc
    ):
        raise ValueError("candidate receipt and lock basis differ from verified snapshot")
    basis_official_date = lock_basis.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    ).date()
    if candidate_date != basis_official_date:
        raise ValueError("candidate official_date differs from snapshot lock basis")
    completeness = finite_number(
        row.data_completeness_score, "candidate.data_completeness_score"
    )
    edge = finite_number(row.absolute_starter_edge, "candidate.absolute_starter_edge")
    if not 0 <= completeness <= 10 or edge < 0:
        raise ValueError("candidate ranking inputs are out of bounds")
    if row.date_state == "LOCK_TOP1":
        raise ValueError("candidate cannot preclaim LOCK_TOP1")
    if not isinstance(row.critical, bool) or not all(
        isinstance(value, str)
        for value in (
            row.provenance,
            row.lock_state,
            row.final_snapshot_classification,
            row.eligibility_status,
            row.date_state,
        )
    ):
        raise ValueError("prospective official pool row fields are invalid")
    return (
        row.provenance == "TIMESTAMP_VERIFIED_T45"
        and row.lock_state == "SEALED"
        and row.final_snapshot_classification == classification
        and classification == "TIMESTAMP_VERIFIED_T45"
        and not row.critical
        and row.eligibility_status == "ELIGIBLE"
        and row.date_state == "CANDIDATE_ACTIVE"
    )


CANDIDATE_SLATE_KEYS = frozenset(
    {
        "official_date",
        "expected_candidate_ids",
        "expected_candidate_count",
        "candidates_sha256",
        "snapshot_lock_receipts",
        "final_lock_receipt_at_utc",
        "all_locks_sealed",
        "persisted_at_utc",
        "audit_path",
    }
)
CANDIDATE_LOCK_RECEIPT_KEYS = frozenset(
    {
        "candidate_id",
        "verified_snapshot_id",
        "verified_snapshot_sha256",
        "lock_basis_start_utc",
        "lock_at_utc",
        "receipt_at_utc",
    }
)


def _prospective_snapshot_raw(snapshot: ProspectiveSnapshot) -> dict[str, object]:
    return {
        "gamePk": snapshot.game_pk,
        "lock_basis_start_utc": snapshot.lock_basis_start_utc,
        "observed_scheduled_start_utc": snapshot.observed_scheduled_start_utc,
        "lock_at_utc": snapshot.lock_at_utc,
        "receipt_at_utc": snapshot.receipt_at_utc,
        "successful": snapshot.successful,
        "complete_participants": snapshot.complete_participants,
        "ntp_offset_seconds": snapshot.ntp_offset_seconds,
        "sequence": snapshot.sequence,
    }


def _prospective_snapshot_id(snapshot: ProspectiveSnapshot, digest: str) -> str:
    return f"snapshot:{snapshot.game_pk}:{snapshot.sequence}:{digest}"


def _prospective_candidate_raw(
    candidate: ProspectiveCandidateRow,
) -> dict[str, object]:
    return {
        "gamePk": candidate.game_pk,
        "official_date": candidate.official_date,
        "recorded_at_utc": candidate.recorded_at_utc,
        "retrospective_backfill": candidate.retrospective_backfill,
        "p_home": candidate.p_home,
        "predicted_side": candidate.predicted_side,
        "verified_snapshot_id": candidate.verified_snapshot_id,
        "verified_snapshot_sha256": candidate.verified_snapshot_sha256,
        "sealed_lock_snapshot_id": candidate.sealed_lock_snapshot_id,
        "sealed_lock_snapshot_sha256": candidate.sealed_lock_snapshot_sha256,
        "provenance": candidate.provenance,
        "lock_state": candidate.lock_state,
        "final_snapshot_classification": candidate.final_snapshot_classification,
        "critical": candidate.critical,
        "eligibility_status": candidate.eligibility_status,
        "date_state": candidate.date_state,
        "data_completeness_score": candidate.data_completeness_score,
        "absolute_starter_edge": candidate.absolute_starter_edge,
        "lock_basis_start_utc": candidate.lock_basis_start_utc,
        "lock_receipt_at_utc": candidate.lock_receipt_at_utc,
    }


def _prospective_candidate_id(candidate: ProspectiveCandidateRow) -> str:
    return f"{candidate.game_pk}:{candidate.verified_snapshot_id}"


def _candidate_slate_projection(
    candidates: tuple[ProspectiveCandidateRow, ...],
) -> tuple[str, tuple[str, ...], str, tuple[dict[str, object], ...], str]:
    if not isinstance(candidates, tuple) or not all(
        isinstance(candidate, ProspectiveCandidateRow) for candidate in candidates
    ):
        raise ValueError("prospective candidates must be a typed tuple")
    if not candidates:
        raise ValueError("candidate slate requires at least one eligible candidate")
    dates: set[str] = set()
    game_pks: set[int] = set()
    snapshot_ids: set[str] = set()
    candidate_ids: set[str] = set()
    for candidate in candidates:
        if not eligible_for_prospective_official_pool(candidate):
            raise ValueError("candidate slate contains a candidate that is not eligible")
        dates.add(candidate.official_date)
        if candidate.game_pk in game_pks:
            raise ValueError("candidate slate contains duplicate gamePk")
        if candidate.verified_snapshot_id in snapshot_ids:
            raise ValueError("candidate slate contains duplicate verified snapshot")
        candidate_id = _prospective_candidate_id(candidate)
        if candidate_id in candidate_ids:
            raise ValueError("candidate slate contains duplicate candidate IDs")
        game_pks.add(candidate.game_pk)
        snapshot_ids.add(candidate.verified_snapshot_id)
        candidate_ids.add(candidate_id)
    if len(dates) != 1:
        raise ValueError("candidate slate must contain one official date")
    official_date = next(iter(dates))
    _canonical_date(official_date, "candidate slate official_date")
    ordered = tuple(
        sorted(candidates, key=lambda candidate: _prospective_candidate_id(candidate))
    )
    ordered_ids = tuple(_prospective_candidate_id(candidate) for candidate in ordered)
    candidates_sha256 = hashlib.sha256(
        canonical_json_bytes(
            [_prospective_candidate_raw(candidate) for candidate in ordered]
        )
    ).hexdigest()
    lock_receipts = tuple(
        {
            "candidate_id": _prospective_candidate_id(candidate),
            "verified_snapshot_id": candidate.verified_snapshot_id,
            "verified_snapshot_sha256": candidate.verified_snapshot_sha256,
            "lock_basis_start_utc": candidate.verified_snapshot.lock_basis_start_utc,
            "lock_at_utc": candidate.verified_snapshot.lock_at_utc,
            "receipt_at_utc": candidate.verified_snapshot.receipt_at_utc,
        }
        for candidate in ordered
    )
    final_lock_receipt = max(
        (
            _canonical_utc_datetime(
                candidate.lock_receipt_at_utc, "candidate.lock_receipt_at_utc"
            ),
            candidate.lock_receipt_at_utc,
        )
        for candidate in ordered
    )[1]
    return (
        official_date,
        ordered_ids,
        candidates_sha256,
        lock_receipts,
        final_lock_receipt,
    )


def _candidate_slate_relative_path(official_date: str) -> Path:
    _canonical_date(official_date, "candidate slate official_date")
    return Path(
        "audit/v5_2_qd/prospective_2026/candidate_slates/"
        f"{official_date}.json"
    )


def _candidate_slate_lock_relative_path(official_date: str) -> Path:
    _canonical_date(official_date, "candidate slate lock official_date")
    return Path(
        "audit/v5_2_qd/prospective_2026/candidate_slate_locks/"
        f"{official_date}.lock"
    )


def _candidate_slate_routing_date(
    candidates: tuple[ProspectiveCandidateRow, ...],
) -> str:
    if not isinstance(candidates, tuple) or (
        candidates and not isinstance(candidates[0], ProspectiveCandidateRow)
    ):
        raise ValueError("prospective candidates must be a typed tuple")
    if not candidates:
        raise ValueError("candidate slate requires at least one eligible candidate")
    official_date = candidates[0].official_date
    _canonical_date(official_date, "candidate slate lock official_date")
    return official_date


@contextmanager
def _candidate_slate_seal_lock(root: Path, official_date: str):
    root_path = _reject_symlink_ancestors(root, "candidate slate lock root")
    relative = _candidate_slate_lock_relative_path(official_date)
    lock_path = root_path / relative
    created_directories = _create_postlock_receipt_directories(lock_path.parent)
    _reject_symlink_ancestors(lock_path.parent, "candidate slate lock path")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    try:
        try:
            descriptor = os.open(
                lock_path,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            created = True
        except FileExistsError:
            descriptor = os.open(lock_path, flags)
    except OSError as exc:
        if not lock_path.exists() and not lock_path.is_symlink():
            _remove_created_receipt_directories(created_directories)
        raise ValueError(
            f"candidate slate lock symlink or alias is forbidden: {lock_path}"
        ) from exc
    locked = False
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != 0
        ):
            raise ValueError(
                f"candidate slate lock hardlink or stale alias is forbidden: {lock_path}"
            )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        if created:
            _fsync_directory(lock_path.parent)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield root_path
    finally:
        try:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _candidate_slate_raw(slate: ProspectiveCandidateSlate) -> dict[str, object]:
    return {
        "official_date": slate.official_date,
        "expected_candidate_ids": list(slate.expected_candidate_ids),
        "expected_candidate_count": slate.expected_candidate_count,
        "candidates_sha256": slate.candidates_sha256,
        "snapshot_lock_receipts": [dict(receipt) for receipt in slate.snapshot_lock_receipts],
        "final_lock_receipt_at_utc": slate.final_lock_receipt_at_utc,
        "all_locks_sealed": slate.all_locks_sealed,
        "persisted_at_utc": slate.persisted_at_utc,
        "audit_path": slate.audit_path,
    }


def load_prospective_candidate_slate(
    root: Path, *, official_date: str
) -> ProspectiveCandidateSlate:
    relative = _candidate_slate_relative_path(official_date)
    root_path = _reject_symlink_ancestors(root, "candidate slate root")
    path = root_path / relative
    _reject_symlink_ancestors(path, "candidate slate path")
    contents = _read_regular_unaliased(path, "candidate slate manifest")
    try:
        raw = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate slate manifest is not canonical JSON") from exc
    obj = require_exact_keys(raw, CANDIDATE_SLATE_KEYS, "candidate_slate")
    if contents != canonical_json_bytes(obj) + b"\n":
        raise ValueError("candidate slate manifest bytes are not canonical")
    expected_ids_raw = obj["expected_candidate_ids"]
    receipts_raw = obj["snapshot_lock_receipts"]
    if not isinstance(expected_ids_raw, list) or not all(
        isinstance(candidate_id, str) and candidate_id
        for candidate_id in expected_ids_raw
    ):
        raise ValueError("candidate slate manifest IDs are invalid")
    expected_ids = tuple(expected_ids_raw)
    if tuple(sorted(expected_ids)) != expected_ids or len(set(expected_ids)) != len(
        expected_ids
    ):
        raise ValueError("candidate slate manifest IDs are not unique and sorted")
    if not isinstance(receipts_raw, list):
        raise ValueError("candidate slate manifest lock receipts are invalid")
    receipts: list[Mapping[str, object]] = []
    receipt_times: list[tuple[datetime, str]] = []
    for index, raw_receipt in enumerate(receipts_raw):
        receipt = require_exact_keys(
            raw_receipt,
            CANDIDATE_LOCK_RECEIPT_KEYS,
            f"candidate_slate.snapshot_lock_receipts[{index}]",
        )
        if (
            not isinstance(receipt["candidate_id"], str)
            or not isinstance(receipt["verified_snapshot_id"], str)
            or not isinstance(receipt["verified_snapshot_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", receipt["verified_snapshot_sha256"])
            or not isinstance(receipt["lock_basis_start_utc"], str)
            or not isinstance(receipt["lock_at_utc"], str)
            or not isinstance(receipt["receipt_at_utc"], str)
        ):
            raise ValueError("candidate slate manifest lock receipt is invalid")
        basis = _canonical_utc_datetime(
            receipt["lock_basis_start_utc"], "slate lock basis"
        )
        lock_at = _canonical_utc_datetime(receipt["lock_at_utc"], "slate lock at")
        receipt_at = _canonical_utc_datetime(
            receipt["receipt_at_utc"], "slate snapshot receipt"
        )
        if lock_at != basis - timedelta(minutes=45) or receipt_at > lock_at:
            raise ValueError("candidate slate manifest lock receipt chronology differs")
        receipt_times.append(
            (
                lock_at,
                receipt["lock_at_utc"],
            )
        )
        receipts.append(deep_freeze(dict(receipt)))
    expected_count = obj["expected_candidate_count"]
    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count <= 0
        or expected_count != len(expected_ids)
        or expected_count != len(receipts)
    ):
        raise ValueError("candidate slate manifest count differs")
    if tuple(receipt["candidate_id"] for receipt in receipts) != expected_ids:
        raise ValueError("candidate slate manifest receipt IDs differ")
    if obj["official_date"] != official_date:
        raise ValueError("candidate slate manifest official date differs")
    if obj["audit_path"] != relative.as_posix():
        raise ValueError("candidate slate manifest audit path differs")
    if not isinstance(obj["candidates_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", obj["candidates_sha256"]
    ):
        raise ValueError("candidate slate manifest digest is invalid")
    if obj["all_locks_sealed"] is not True:
        raise ValueError("candidate slate manifest does not seal every lock")
    if not isinstance(obj["final_lock_receipt_at_utc"], str) or (
        obj["final_lock_receipt_at_utc"] != max(receipt_times)[1]
    ):
        raise ValueError("candidate slate manifest final lock receipt differs")
    if not isinstance(obj["persisted_at_utc"], str) or (
        _canonical_utc_datetime(obj["persisted_at_utc"], "slate persisted_at_utc")
        < max(receipt_times)[0]
    ):
        raise ValueError("candidate slate manifest persistence precedes final lock")
    return ProspectiveCandidateSlate(
        official_date=official_date,
        expected_candidate_ids=expected_ids,
        expected_candidate_count=expected_count,
        candidates_sha256=obj["candidates_sha256"],
        snapshot_lock_receipts=tuple(receipts),
        final_lock_receipt_at_utc=obj["final_lock_receipt_at_utc"],
        all_locks_sealed=True,
        persisted_at_utc=obj["persisted_at_utc"],
        audit_path=obj["audit_path"],
    )


def _write_prospective_candidate_slate_locked(
    root_path: Path,
    *,
    routing_date: str,
    candidates: tuple[ProspectiveCandidateRow, ...],
    persisted_at_utc: str,
) -> ProspectiveCandidateSlate:
    (
        official_date,
        candidate_ids,
        candidates_sha256,
        lock_receipts,
        final_lock_receipt,
    ) = _candidate_slate_projection(candidates)
    if official_date != routing_date:
        raise ValueError("candidate slate projection differs from locked official date")
    persisted = _canonical_utc_datetime(persisted_at_utc, "slate persisted_at_utc")
    if persisted < _canonical_utc_datetime(
        final_lock_receipt, "slate final_lock_receipt_at_utc"
    ):
        raise ValueError("candidate slate persistence precedes final lock receipt")
    relative = _candidate_slate_relative_path(official_date)
    slate = ProspectiveCandidateSlate(
        official_date=official_date,
        expected_candidate_ids=candidate_ids,
        expected_candidate_count=len(candidate_ids),
        candidates_sha256=candidates_sha256,
        snapshot_lock_receipts=tuple(
            deep_freeze(receipt) for receipt in lock_receipts
        ),
        final_lock_receipt_at_utc=final_lock_receipt,
        all_locks_sealed=True,
        persisted_at_utc=persisted_at_utc,
        audit_path=relative.as_posix(),
    )
    path = root_path / relative
    _reject_symlink_ancestors(path.parent, "candidate slate path")
    if path.exists() or path.is_symlink():
        existing = load_prospective_candidate_slate(
            root_path, official_date=official_date
        )
        if existing != slate:
            raise ValueError("candidate slate manifest is already sealed and differs")
        return existing

    def verify() -> None:
        loaded = load_prospective_candidate_slate(
            root_path, official_date=official_date
        )
        if loaded != slate:
            raise ValueError("candidate slate postcondition failed")

    _publish_bundle(
        ((path, canonical_json_bytes(_candidate_slate_raw(slate)) + b"\n"),),
        root=root_path,
        post_publish=verify,
    )
    return slate


def write_prospective_candidate_slate(
    root: Path,
    *,
    candidates: tuple[ProspectiveCandidateRow, ...],
    persisted_at_utc: str,
) -> ProspectiveCandidateSlate:
    routing_date = _candidate_slate_routing_date(candidates)
    with _candidate_slate_seal_lock(root, routing_date) as root_path:
        return _write_prospective_candidate_slate_locked(
            root_path,
            routing_date=routing_date,
            candidates=candidates,
            persisted_at_utc=persisted_at_utc,
        )


def select_prospective_top1(
    root: Path,
    *,
    official_date: str,
    candidates: tuple[ProspectiveCandidateRow, ...],
) -> ProspectiveTop1:
    slate = load_prospective_candidate_slate(root, official_date=official_date)
    (
        projected_date,
        candidate_ids,
        candidates_sha256,
        lock_receipts,
        final_lock_receipt,
    ) = _candidate_slate_projection(candidates)
    if projected_date != official_date:
        raise ValueError("Top1 candidates differ from the sealed slate official date")
    if len(candidate_ids) != slate.expected_candidate_count:
        raise ValueError("Top1 candidate count differs from sealed slate")
    if candidate_ids != slate.expected_candidate_ids:
        raise ValueError("Top1 candidate IDs differ from sealed slate")
    if candidates_sha256 != slate.candidates_sha256:
        raise ValueError("Top1 candidate digest differs from sealed slate")
    if tuple(lock_receipts) != tuple(map(dict, slate.snapshot_lock_receipts)):
        raise ValueError("Top1 candidate lock receipts differ from sealed slate")
    if final_lock_receipt != slate.final_lock_receipt_at_utc:
        raise ValueError("Top1 final lock receipt differs from sealed slate")
    if not slate.all_locks_sealed:
        raise ValueError("all candidate locks must be sealed before Top1 selection")
    winner = min(
        candidates,
        key=lambda candidate: ranking_key(
            max(candidate.p_home, 1 - candidate.p_home),
            candidate.data_completeness_score,
            candidate.absolute_starter_edge,
            candidate.lock_basis_start_utc,
            candidate.game_pk,
        ),
    )
    return ProspectiveTop1(
        official_date=winner.official_date,
        game_pk=winner.game_pk,
        predicted_side=winner.predicted_side,
        pick_probability=max(winner.p_home, 1 - winner.p_home),
        official_rank=1,
        verified_snapshot_id=winner.verified_snapshot_id,
        verified_snapshot_sha256=winner.verified_snapshot_sha256,
        final_lock_receipt_at_utc=slate.final_lock_receipt_at_utc,
    )


DATE_DISPOSITIONS = MappingProxyType(
    {
        "run_invalid": "RUN_INVALID",
        "acquisition_failure": "SYSTEM_MISS",
        "clock_failure": "SYSTEM_MISS",
        "partial_only": "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE",
        "empty_verified": "NO_VERIFIED_ELIGIBLE_DATE",
        "lock_top1": "LOCK_TOP1",
        "RUN_INVALID": "RUN_INVALID",
        "SYSTEM_MISS": "SYSTEM_MISS",
        "SYSTEM_MISS_CLOCK": "SYSTEM_MISS",
        "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE": (
            "SHADOW_ONLY_NO_VERIFIED_ELIGIBLE_DATE"
        ),
        "NO_VERIFIED_ELIGIBLE_DATE": "NO_VERIFIED_ELIGIBLE_DATE",
        "LOCK_TOP1": "LOCK_TOP1",
    }
)


def _canonical_utc_datetime(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
        value,
    ):
        raise ValueError(f"{label} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ValueError(
            f"{label} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ"
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{label} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    return parsed


def classify_snapshot(snapshot: ProspectiveSnapshot) -> str:
    if not isinstance(snapshot, ProspectiveSnapshot):
        raise ValueError("snapshot must be a typed ProspectiveSnapshot")
    if (
        isinstance(snapshot.game_pk, bool)
        or not isinstance(snapshot.game_pk, int)
        or snapshot.game_pk <= 0
        or isinstance(snapshot.sequence, bool)
        or not isinstance(snapshot.sequence, int)
        or snapshot.sequence <= 0
    ):
        raise ValueError("snapshot gamePk and sequence must be positive integers")
    if not isinstance(snapshot.successful, bool) or not isinstance(
        snapshot.complete_participants, bool
    ):
        raise ValueError("snapshot success and completeness must be boolean")
    basis = _canonical_utc_datetime(
        snapshot.lock_basis_start_utc, "lock_basis_start_utc"
    )
    scheduled = _canonical_utc_datetime(
        snapshot.observed_scheduled_start_utc,
        "observed_scheduled_start_utc",
    )
    lock_at = _canonical_utc_datetime(snapshot.lock_at_utc, "lock_at_utc")
    receipt = _canonical_utc_datetime(snapshot.receipt_at_utc, "receipt_at_utc")
    basis_official_date = basis.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    ).date()
    if basis_official_date < date(2026, 7, 13):
        raise ValueError("snapshot precedes the prospective first permitted date")
    if scheduled != basis:
        raise ValueError("snapshot scheduled start differs from lock basis")
    if lock_at != basis - timedelta(minutes=45):
        raise ValueError("lock_at_utc must equal lock basis minus 45 minutes")
    if receipt > lock_at:
        raise ValueError("snapshot receipt is after lock")
    if receipt < basis - timedelta(minutes=50):
        raise ValueError("snapshot receipt is outside qualifying window")
    if lock_at - receipt > timedelta(minutes=5):
        raise ValueError("snapshot exceeds maximum freshness at lock")
    offset = finite_number(snapshot.ntp_offset_seconds, "ntp_offset_seconds")
    if abs(offset) > 2:
        return "SYSTEM_MISS_CLOCK"
    if not snapshot.successful:
        return "UNSUCCESSFUL_RESPONSE_REJECTED"
    return (
        "TIMESTAMP_VERIFIED_T45"
        if snapshot.complete_participants
        else "TIMESTAMP_PARTIAL_T45"
    )


def select_qualifying_snapshot(
    candidates: tuple[ProspectiveSnapshot, ...],
) -> ProspectiveSnapshot | None:
    if not isinstance(candidates, tuple) or not all(
        isinstance(candidate, ProspectiveSnapshot) for candidate in candidates
    ):
        raise ValueError("snapshot candidates must be a typed tuple")
    if not candidates:
        return None
    sequences = tuple(candidate.sequence for candidate in candidates)
    if sequences != tuple(sorted(set(sequences))):
        raise ValueError("snapshot candidate sequence must be strictly increasing")
    identity = {
        (
            candidate.game_pk,
            candidate.lock_basis_start_utc,
            candidate.lock_at_utc,
        )
        for candidate in candidates
    }
    if len(identity) != 1:
        raise ValueError("snapshot candidates differ in game or immutable lock basis")
    eligible = [
        candidate
        for candidate in candidates
        if classify_snapshot(candidate) == "TIMESTAMP_VERIFIED_T45"
    ]
    if not eligible:
        return None
    return max(
        eligible,
        key=lambda candidate: (
            _canonical_utc_datetime(candidate.receipt_at_utc, "receipt_at_utc"),
            candidate.sequence,
        ),
    )


def date_disposition(events: object) -> str:
    if not isinstance(events, (list, tuple, set, frozenset)) or not events:
        raise ValueError("prospective date events must be a non-empty list or set")
    active: set[str] = set()
    for event in events:
        if not isinstance(event, str) or event not in DATE_DISPOSITIONS:
            raise ValueError(f"unknown prospective date event: {event}")
        active.add(DATE_DISPOSITIONS[event])
    for disposition in PROSPECTIVE_DATE_PRECEDENCE:
        if disposition in active:
            return disposition
    raise ValueError("prospective date events contain no frozen disposition")


def _postlock_event_payload(event: PostlockStartChange) -> dict[str, object]:
    return {
        "event": "POSTLOCK_START_CHANGE",
        "gamePk": event.game_pk,
        "sequence": event.sequence,
        "previous_effective_start_utc": event.previous_effective_start_utc,
        "new_effective_start_utc": event.new_effective_start_utc,
        "observed_at_utc": event.observed_at_utc,
        "lock_basis_start_utc": event.lock_basis_start_utc,
        "prior_event_hash": event.prior_event_hash,
    }


def _postlock_event_hash(event: PostlockStartChange) -> str:
    return hashlib.sha256(canonical_json_bytes(_postlock_event_payload(event))).hexdigest()


def validate_postlock_start_changes(
    events: tuple[PostlockStartChange, ...],
    *,
    sealed_state: PostlockChainState,
    lock_basis_start_utc: str,
) -> str:
    if not isinstance(events, tuple) or not all(
        isinstance(event, PostlockStartChange) for event in events
    ):
        raise ValueError("post-lock events must be a typed immutable tuple")
    if not isinstance(sealed_state, PostlockChainState):
        raise ValueError("post-lock validation requires an independent sealed state")
    if (
        isinstance(sealed_state.game_pk, bool)
        or not isinstance(sealed_state.game_pk, int)
        or sealed_state.game_pk <= 0
    ):
        raise ValueError("sealed-state gamePk must be an exact positive integer")
    if (
        isinstance(sealed_state.event_count, bool)
        or not isinstance(sealed_state.event_count, int)
        or sealed_state.event_count < 0
    ):
        raise ValueError("sealed-state event count must be an exact nonnegative integer")
    if len(events) != sealed_state.event_count:
        raise ValueError("post-lock event count differs from sealed state")
    if not isinstance(sealed_state.head_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", sealed_state.head_hash
    ):
        raise ValueError("sealed-state head hash must be lowercase SHA-256")
    expected_audit_path = (
        "audit/v5_2_qd/prospective_2026/postlock_chain_state/"
        f"{sealed_state.game_pk}.json"
    )
    expected_event_log_path = (
        "audit/v5_2_qd/prospective_2026/postlock_start_changes/"
        f"{sealed_state.game_pk}.jsonl"
    )
    if sealed_state.audit_path != expected_audit_path:
        raise ValueError("sealed-state audit path differs from canonical namespace")
    if sealed_state.event_log_path != expected_event_log_path:
        raise ValueError("event-log path differs from canonical namespace")
    persisted = _canonical_utc_datetime(
        sealed_state.persisted_at_utc, "sealed_state.persisted_at_utc"
    )
    lock_basis = _canonical_utc_datetime(
        lock_basis_start_utc, "lock_basis_start_utc"
    )
    if sealed_state.lock_basis_start_utc != lock_basis_start_utc:
        raise ValueError("sealed-state lock basis differs from requested lock basis")
    lock_at = lock_basis - timedelta(minutes=45)
    if persisted < lock_at:
        raise ValueError("sealed-state persistence precedes sealed lock")
    prior_hash = POSTLOCK_GENESIS_HASH
    previous_effective = lock_basis_start_utc
    previous_observed: datetime | None = None
    for index, event in enumerate(events, start=1):
        if (
            isinstance(event.game_pk, bool)
            or not isinstance(event.game_pk, int)
            or event.game_pk <= 0
            or isinstance(event.sequence, bool)
            or not isinstance(event.sequence, int)
            or event.sequence <= 0
        ):
            raise ValueError(
                "post-lock event gamePk and sequence must be exact positive integers"
            )
        if event.game_pk != sealed_state.game_pk:
            raise ValueError("post-lock event gamePk differs from sealed lock")
        if event.sequence != index:
            raise ValueError("post-lock event sequence is not contiguous")
        if event.lock_basis_start_utc != lock_basis_start_utc:
            raise ValueError("post-lock event lock basis differs from sealed lock")
        if event.previous_effective_start_utc != previous_effective:
            raise ValueError("post-lock effective-start chain is discontinuous")
        if event.new_effective_start_utc == event.previous_effective_start_utc:
            raise ValueError("post-lock effective start did not change")
        _canonical_utc_datetime(
            event.previous_effective_start_utc,
            "previous_effective_start_utc",
        )
        _canonical_utc_datetime(
            event.new_effective_start_utc, "new_effective_start_utc"
        )
        observed = _canonical_utc_datetime(event.observed_at_utc, "observed_at_utc")
        if observed < lock_at:
            raise ValueError("post-lock event was observed before sealed lock")
        if previous_observed is not None and observed < previous_observed:
            raise ValueError("post-lock observed times must be nondecreasing")
        if event.prior_event_hash != prior_hash:
            raise ValueError("post-lock prior event hash differs")
        calculated = _postlock_event_hash(event)
        if event.event_hash != calculated:
            raise ValueError("post-lock event hash differs")
        prior_hash = calculated
        previous_effective = event.new_effective_start_utc
        previous_observed = observed
    if prior_hash != sealed_state.head_hash:
        raise ValueError("post-lock event head hash differs from sealed state")
    if previous_observed is not None and persisted < previous_observed:
        raise ValueError("sealed-state persistence precedes final event observation")
    return prior_hash


def append_postlock_start_change(
    events: tuple[PostlockStartChange, ...],
    *,
    sealed_state: PostlockChainState,
    game_pk: int,
    previous_effective_start_utc: str,
    new_effective_start_utc: str,
    observed_at_utc: str,
    lock_basis_start_utc: str,
    state_persisted_at_utc: str,
) -> tuple[PostlockStartChange, PostlockChainState]:
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        raise ValueError("post-lock gamePk must be an exact positive integer")
    if not isinstance(sealed_state, PostlockChainState) or game_pk != sealed_state.game_pk:
        raise ValueError("append gamePk differs from independent sealed state")
    head = validate_postlock_start_changes(
        events,
        sealed_state=sealed_state,
        lock_basis_start_utc=lock_basis_start_utc,
    )
    expected_previous = (
        events[-1].new_effective_start_utc if events else lock_basis_start_utc
    )
    if previous_effective_start_utc != expected_previous:
        raise ValueError("post-lock previous effective start differs from current head")
    if new_effective_start_utc == previous_effective_start_utc:
        raise ValueError("post-lock effective start did not change")
    _canonical_utc_datetime(
        previous_effective_start_utc, "previous_effective_start_utc"
    )
    _canonical_utc_datetime(new_effective_start_utc, "new_effective_start_utc")
    observed = _canonical_utc_datetime(observed_at_utc, "observed_at_utc")
    lock_at = _canonical_utc_datetime(
        lock_basis_start_utc, "lock_basis_start_utc"
    ) - timedelta(minutes=45)
    if observed < lock_at:
        raise ValueError("post-lock event was observed before sealed lock")
    persisted = _canonical_utc_datetime(
        state_persisted_at_utc, "state_persisted_at_utc"
    )
    prior_persisted = _canonical_utc_datetime(
        sealed_state.persisted_at_utc, "sealed_state.persisted_at_utc"
    )
    if persisted < observed or persisted < prior_persisted:
        raise ValueError("new sealed-state persistence must not precede event or prior state")
    unsigned = PostlockStartChange(
        game_pk=game_pk,
        sequence=sealed_state.event_count + 1,
        previous_effective_start_utc=previous_effective_start_utc,
        new_effective_start_utc=new_effective_start_utc,
        observed_at_utc=observed_at_utc,
        lock_basis_start_utc=lock_basis_start_utc,
        prior_event_hash=head,
        event_hash="",
    )
    event = replace(unsigned, event_hash=_postlock_event_hash(unsigned))
    appended = (*events, event)
    new_state = PostlockChainState(
        game_pk=game_pk,
        lock_basis_start_utc=lock_basis_start_utc,
        event_count=sealed_state.event_count + 1,
        head_hash=event.event_hash,
        persisted_at_utc=state_persisted_at_utc,
        audit_path=(
            "audit/v5_2_qd/prospective_2026/postlock_chain_state/"
            f"{game_pk}.json"
        ),
        event_log_path=(
            "audit/v5_2_qd/prospective_2026/postlock_start_changes/"
            f"{game_pk}.jsonl"
        ),
    )
    validate_postlock_start_changes(
        appended,
        sealed_state=new_state,
        lock_basis_start_utc=lock_basis_start_utc,
    )
    return event, new_state


POSTLOCK_STATE_KEYS = frozenset(
    {
        "gamePk",
        "lock_basis_start_utc",
        "event_count",
        "head_hash",
        "persisted_at_utc",
        "audit_path",
        "event_log_path",
    }
)
POSTLOCK_EVENT_KEYS = frozenset(
    {
        "event",
        "gamePk",
        "sequence",
        "previous_effective_start_utc",
        "new_effective_start_utc",
        "observed_at_utc",
        "lock_basis_start_utc",
        "prior_event_hash",
        "event_hash",
    }
)
POSTLOCK_RECEIPT_KEYS = frozenset(
    {
        "gamePk",
        "sequence",
        "head_hash",
        "prior_receipt_hash",
        "persisted_at_utc",
        "lock_basis_start_utc",
        "receipt_hash",
        "audit_path",
    }
)


def _postlock_relative_paths(game_pk: int) -> tuple[Path, Path]:
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        raise ValueError("post-lock gamePk must be an exact positive integer")
    return (
        Path(
            "audit/v5_2_qd/prospective_2026/postlock_start_changes/"
            f"{game_pk}.jsonl"
        ),
        Path(
            "audit/v5_2_qd/prospective_2026/postlock_chain_state/"
            f"{game_pk}.json"
        ),
    )


def _reject_symlink_ancestors(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    components = (absolute, *absolute.parents)
    for component in reversed(components):
        if component.exists() or component.is_symlink():
            metadata = component.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"{label} ancestor symlink or path alias is forbidden")
    return absolute


def _postlock_paths(root: Path, game_pk: int) -> tuple[Path, Path, Path]:
    root_input = Path(root)
    if root_input.is_symlink():
        raise ValueError("post-lock root symlink or alias is forbidden")
    root_path = _reject_symlink_ancestors(root_input, "post-lock root")
    log_relative, state_relative = _postlock_relative_paths(game_pk)
    for target in (root_path, root_path / log_relative.parent, root_path / state_relative.parent):
        current = target
        chain: list[Path] = []
        while current != root_path.parent:
            chain.append(current)
            if current == root_path:
                break
            current = current.parent
        for directory in reversed(chain):
            if directory.exists() or directory.is_symlink():
                metadata = directory.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise ValueError("post-lock parent symlink or path alias is forbidden")
    return root_path, root_path / log_relative, root_path / state_relative


def _postlock_lock_path(root: Path, game_pk: int) -> Path:
    root_path, _, _ = _postlock_paths(root, game_pk)
    return root_path / f"audit/v5_2_qd/prospective_2026/locks/{game_pk}.lock"


@contextmanager
def _postlock_game_lock(root: Path, game_pk: int):
    lock_path = _postlock_lock_path(root, game_pk)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_ancestors(lock_path.parent, "post-lock lock path")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("post-lock lock symlink or alias is forbidden") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("post-lock lock symlink or hardlink alias is forbidden")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield lock_path
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _read_regular_unaliased(path: Path, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"{label} is missing at canonical path: {path}") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise ValueError(f"{label} symlink or alias is forbidden: {path}")
    return path.read_bytes()


def _postlock_state_raw(state: PostlockChainState) -> dict[str, object]:
    return {
        "gamePk": state.game_pk,
        "lock_basis_start_utc": state.lock_basis_start_utc,
        "event_count": state.event_count,
        "head_hash": state.head_hash,
        "persisted_at_utc": state.persisted_at_utc,
        "audit_path": state.audit_path,
        "event_log_path": state.event_log_path,
    }


def _postlock_event_raw(event: PostlockStartChange) -> dict[str, object]:
    return {**_postlock_event_payload(event), "event_hash": event.event_hash}


def _postlock_event_log_bytes(events: tuple[PostlockStartChange, ...]) -> bytes:
    return b"".join(canonical_json_bytes(_postlock_event_raw(event)) + b"\n" for event in events)


def _postlock_receipt_payload(receipt: PostlockChainReceipt) -> dict[str, object]:
    return {
        "gamePk": receipt.game_pk,
        "sequence": receipt.sequence,
        "head_hash": receipt.head_hash,
        "prior_receipt_hash": receipt.prior_receipt_hash,
        "persisted_at_utc": receipt.persisted_at_utc,
        "lock_basis_start_utc": receipt.lock_basis_start_utc,
        "audit_path": receipt.audit_path,
    }


def _postlock_receipt_raw(receipt: PostlockChainReceipt) -> dict[str, object]:
    return {**_postlock_receipt_payload(receipt), "receipt_hash": receipt.receipt_hash}


def _postlock_receipt_hash(receipt: PostlockChainReceipt) -> str:
    return hashlib.sha256(canonical_json_bytes(_postlock_receipt_payload(receipt))).hexdigest()


def _postlock_receipt_relative_path(
    game_pk: int, sequence: int, head_hash: str
) -> Path:
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 0
        or not isinstance(head_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", head_hash)
    ):
        raise ValueError("post-lock receipt identity is invalid")
    _postlock_relative_paths(game_pk)
    return Path(
        "audit/v5_2_qd/prospective_2026/postlock_chain_receipts/"
        f"{game_pk}/{sequence:08d}-{head_hash}.json"
    )


def _remove_created_receipt_directories(
    created_directories: tuple[Path, ...],
) -> None:
    for directory in created_directories:
        try:
            directory.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            _fsync_directory(directory)
        _fsync_directory(directory.parent)


def _create_postlock_receipt_directories(parent: Path) -> tuple[Path, ...]:
    missing: list[Path] = []
    candidate = parent
    while not candidate.exists() and not candidate.is_symlink():
        missing.append(candidate)
        candidate = candidate.parent
    metadata = candidate.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(
            f"post-lock receipt parent symlink or alias is forbidden: {candidate}"
        )
    created: list[Path] = []
    try:
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                existing = directory.lstat()
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(
                    existing.st_mode
                ):
                    raise ValueError(
                        "post-lock receipt directory was replaced by an alias: "
                        f"{directory}"
                    )
            else:
                created.append(directory)
                _fsync_directory(directory.parent)
    except BaseException as creation_error:
        created_bottom_up = tuple(reversed(created))
        try:
            _remove_created_receipt_directories(created_bottom_up)
        except BaseException as cleanup_error:
            recovery_paths = tuple(
                directory
                for directory in created_bottom_up
                if directory.exists() or directory.is_symlink()
            )
            raise _PostlockReceiptRecoveryError(
                "post-lock receipt directory creation recovery failed; "
                f"creation error={type(creation_error).__name__}: "
                f"{creation_error}; cleanup error={type(cleanup_error).__name__}: "
                f"{cleanup_error}; recovery paths="
                f"{[str(path) for path in recovery_paths]}",
                recovery_paths,
            ) from cleanup_error
        raise
    return tuple(reversed(created))


def _create_postlock_receipt_exclusive(
    root: Path, receipt: PostlockChainReceipt
) -> _CreatedPostlockReceipt:
    relative = _postlock_receipt_relative_path(
        receipt.game_pk, receipt.sequence, receipt.head_hash
    )
    if receipt.audit_path != relative.as_posix():
        raise ValueError("post-lock receipt audit path differs")
    if receipt.receipt_hash != _postlock_receipt_hash(receipt):
        raise ValueError("post-lock receipt hash differs")
    root_path = _reject_symlink_ancestors(root, "post-lock receipt root")
    path = root_path / relative
    contents = canonical_json_bytes(_postlock_receipt_raw(receipt)) + b"\n"
    created_directories = _create_postlock_receipt_directories(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        _reject_symlink_ancestors(path.parent, "post-lock receipt path")
        descriptor = os.open(path, flags, 0o444)
    except BaseException as open_error:
        try:
            _remove_created_receipt_directories(created_directories)
        except BaseException as cleanup_error:
            recovery_paths = tuple(
                directory
                for directory in created_directories
                if directory.exists() or directory.is_symlink()
            )
            raise _PostlockReceiptRecoveryError(
                "post-lock receipt exclusive open recovery failed at "
                f"{path}; open error={type(open_error).__name__}: {open_error}; "
                f"cleanup error={type(cleanup_error).__name__}: {cleanup_error}; "
                f"recovery paths={[str(item) for item in recovery_paths]}",
                recovery_paths,
            ) from cleanup_error
        raise
    metadata: os.stat_result | None = None
    written = 0
    creation_error: BaseException | None = None
    try:
        metadata = os.fstat(descriptor)
        os.fchmod(descriptor, 0o444)
        while written < len(contents):
            count = os.write(descriptor, contents[written:])
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count <= 0
                or count > len(contents) - written
            ):
                raise OSError(
                    f"post-lock receipt write returned an invalid count at {path}"
                )
            written += count
        os.fsync(descriptor)
    except BaseException as exc:
        creation_error = exc
    close_error: BaseException | None = None
    try:
        os.close(descriptor)
    except BaseException as exc:
        close_error = exc
    if creation_error is not None or close_error is not None:
        if metadata is None:
            try:
                fallback = path.lstat()
                if (
                    not stat.S_ISREG(fallback.st_mode)
                    or fallback.st_nlink != 1
                ):
                    raise ValueError(
                        "new post-lock receipt identity is unavailable or aliased"
                    )
                metadata = fallback
            except BaseException as identity_error:
                raise _PostlockReceiptRecoveryError(
                    "post-lock receipt creation identity recovery failed at "
                    f"{path}; creation error={type(creation_error).__name__}: "
                    f"{creation_error}; close error={type(close_error).__name__}: "
                    f"{close_error}; identity error={type(identity_error).__name__}: "
                    f"{identity_error}; recovery path={path}",
                    (path,),
                ) from identity_error
        created_prefix = _CreatedPostlockReceipt(
            path=path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            contents=contents[:written],
            created_directories=created_directories,
        )
        try:
            _remove_created_postlock_receipt(created_prefix)
        except BaseException as cleanup_error:
            raise _PostlockReceiptRecoveryError(
                "post-lock receipt creation recovery failed at "
                f"{path}; creation error={type(creation_error).__name__}: "
                f"{creation_error}; close error={type(close_error).__name__}: "
                f"{close_error}; cleanup error={type(cleanup_error).__name__}: "
                f"{cleanup_error}; recovery path={path}",
                (path,),
            ) from cleanup_error
        if creation_error is not None and close_error is not None:
            raise RuntimeError(
                "post-lock receipt creation and close failed at "
                f"{path}; creation error={type(creation_error).__name__}: "
                f"{creation_error}; close error={type(close_error).__name__}: "
                f"{close_error}"
            ) from creation_error
        if creation_error is not None:
            raise creation_error
        if close_error is not None:
            raise close_error
    if metadata is None:
        raise RuntimeError(f"post-lock receipt identity was not recorded at {path}")
    return _CreatedPostlockReceipt(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        contents=contents,
        created_directories=created_directories,
    )


def _remove_created_postlock_receipt(created: _CreatedPostlockReceipt) -> None:
    path = created.path
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(
            f"new post-lock receipt disappeared before rollback: {path}"
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_dev != created.device
        or metadata.st_ino != created.inode
    ):
        raise ValueError(
            f"refusing to remove replaced or aliased post-lock receipt: {path}"
        )
    if path.read_bytes() != created.contents:
        raise ValueError(f"refusing to remove modified post-lock receipt: {path}")
    path.unlink()
    _fsync_directory(path.parent)
    _remove_created_receipt_directories(created.created_directories)


def load_postlock_chain_receipts(
    root: Path, *, game_pk: int
) -> tuple[PostlockChainReceipt, ...]:
    root_path, _, _ = _postlock_paths(root, game_pk)
    receipt_dir = (
        root_path
        / f"audit/v5_2_qd/prospective_2026/postlock_chain_receipts/{game_pk}"
    )
    _reject_symlink_ancestors(receipt_dir, "post-lock receipt directory")
    if not receipt_dir.exists() or not receipt_dir.is_dir():
        raise ValueError("post-lock receipt anchor directory is missing")
    paths = tuple(sorted(receipt_dir.iterdir()))
    if not paths:
        raise ValueError("post-lock receipt anchor history is missing")
    receipts: list[PostlockChainReceipt] = []
    prior_receipt_hash = POSTLOCK_RECEIPT_GENESIS_HASH
    for expected_sequence, path in enumerate(paths):
        contents = _read_regular_unaliased(path, "post-lock receipt")
        try:
            raw = json.loads(contents)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("post-lock receipt is not canonical JSON") from exc
        obj = require_exact_keys(raw, POSTLOCK_RECEIPT_KEYS, "postlock_receipt")
        receipt = PostlockChainReceipt(
            game_pk=obj["gamePk"],
            sequence=obj["sequence"],
            head_hash=obj["head_hash"],
            prior_receipt_hash=obj["prior_receipt_hash"],
            persisted_at_utc=obj["persisted_at_utc"],
            lock_basis_start_utc=obj["lock_basis_start_utc"],
            receipt_hash=obj["receipt_hash"],
            audit_path=obj["audit_path"],
        )
        if contents != canonical_json_bytes(_postlock_receipt_raw(receipt)) + b"\n":
            raise ValueError("post-lock receipt bytes are not canonical")
        expected_relative = _postlock_receipt_relative_path(
            game_pk, expected_sequence, receipt.head_hash
        )
        if (
            receipt.game_pk != game_pk
            or isinstance(receipt.sequence, bool)
            or receipt.sequence != expected_sequence
            or receipt.audit_path != expected_relative.as_posix()
            or path != root_path / expected_relative
        ):
            raise ValueError("post-lock receipt gap or identity mismatch")
        if receipt.prior_receipt_hash != prior_receipt_hash:
            raise ValueError("post-lock receipt chain has a gap")
        if receipt.receipt_hash != _postlock_receipt_hash(receipt):
            raise ValueError("post-lock receipt hash differs")
        _canonical_utc_datetime(receipt.persisted_at_utc, "receipt.persisted_at_utc")
        _canonical_utc_datetime(
            receipt.lock_basis_start_utc, "receipt.lock_basis_start_utc"
        )
        prior_receipt_hash = receipt.receipt_hash
        receipts.append(receipt)
    return tuple(receipts)


def load_postlock_chain_state(root: Path, *, game_pk: int) -> PostlockChainState:
    _, _, state_path = _postlock_paths(root, game_pk)
    contents = _read_regular_unaliased(state_path, "post-lock sealed state")
    try:
        raw = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("post-lock sealed state is not canonical JSON") from exc
    obj = require_exact_keys(raw, POSTLOCK_STATE_KEYS, "postlock_chain_state")
    if contents != canonical_json_bytes(obj) + b"\n":
        raise ValueError("post-lock sealed state bytes are not canonical")
    state = PostlockChainState(
        game_pk=obj["gamePk"],
        lock_basis_start_utc=obj["lock_basis_start_utc"],
        event_count=obj["event_count"],
        head_hash=obj["head_hash"],
        persisted_at_utc=obj["persisted_at_utc"],
        audit_path=obj["audit_path"],
        event_log_path=obj["event_log_path"],
    )
    if state.game_pk != game_pk:
        raise ValueError("post-lock sealed state gamePk differs from canonical path")
    return state


def load_postlock_event_log(
    root: Path, *, game_pk: int
) -> tuple[PostlockStartChange, ...]:
    _, log_path, _ = _postlock_paths(root, game_pk)
    contents = _read_regular_unaliased(log_path, "post-lock event log")
    events: list[PostlockStartChange] = []
    if contents:
        if not contents.endswith(b"\n"):
            raise ValueError("post-lock event log must end with canonical newline")
        for index, line in enumerate(contents.splitlines(), start=1):
            try:
                raw = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"post-lock event log row {index} is invalid") from exc
            obj = require_exact_keys(raw, POSTLOCK_EVENT_KEYS, f"postlock_event[{index}]")
            if obj["event"] != "POSTLOCK_START_CHANGE":
                raise ValueError("post-lock event literal differs")
            event = PostlockStartChange(
                game_pk=obj["gamePk"],
                sequence=obj["sequence"],
                previous_effective_start_utc=obj["previous_effective_start_utc"],
                new_effective_start_utc=obj["new_effective_start_utc"],
                observed_at_utc=obj["observed_at_utc"],
                lock_basis_start_utc=obj["lock_basis_start_utc"],
                prior_event_hash=obj["prior_event_hash"],
                event_hash=obj["event_hash"],
            )
            if line != canonical_json_bytes(_postlock_event_raw(event)):
                raise ValueError("post-lock event log row bytes are not canonical")
            events.append(event)
    result = tuple(events)
    if contents != _postlock_event_log_bytes(result):
        raise ValueError("post-lock event log bytes are not canonical")
    return result


def validate_persisted_postlock_chain(
    root: Path, *, game_pk: int
) -> tuple[tuple[PostlockStartChange, ...], PostlockChainState]:
    state = load_postlock_chain_state(root, game_pk=game_pk)
    events = load_postlock_event_log(root, game_pk=game_pk)
    validate_postlock_start_changes(
        events,
        sealed_state=state,
        lock_basis_start_utc=state.lock_basis_start_utc,
    )
    receipts = load_postlock_chain_receipts(root, game_pk=game_pk)
    if len(receipts) != state.event_count + 1:
        raise ValueError("post-lock receipt anchor count differs from sealed state")
    for sequence, receipt in enumerate(receipts):
        expected_head = (
            POSTLOCK_GENESIS_HASH if sequence == 0 else events[sequence - 1].event_hash
        )
        if (
            receipt.sequence != sequence
            or receipt.head_hash != expected_head
            or receipt.lock_basis_start_utc != state.lock_basis_start_utc
        ):
            raise ValueError("post-lock receipt anchor differs from event history")
    if (
        receipts[-1].head_hash != state.head_hash
        or receipts[-1].persisted_at_utc != state.persisted_at_utc
    ):
        raise ValueError("post-lock latest receipt anchor differs from sealed state")
    return events, state


def _receipt_for_state(
    state: PostlockChainState, prior_receipt_hash: str
) -> PostlockChainReceipt:
    relative = _postlock_receipt_relative_path(
        state.game_pk, state.event_count, state.head_hash
    )
    unsigned = PostlockChainReceipt(
        game_pk=state.game_pk,
        sequence=state.event_count,
        head_hash=state.head_hash,
        prior_receipt_hash=prior_receipt_hash,
        persisted_at_utc=state.persisted_at_utc,
        lock_basis_start_utc=state.lock_basis_start_utc,
        receipt_hash="",
        audit_path=relative.as_posix(),
    )
    return replace(unsigned, receipt_hash=_postlock_receipt_hash(unsigned))


def _initialize_postlock_chain_locked(
    root: Path,
    *,
    game_pk: int,
    lock_basis_start_utc: str,
    persisted_at_utc: str,
) -> PostlockChainState:
    root_path, log_path, state_path = _postlock_paths(root, game_pk)
    receipt_dir = (
        root_path
        / f"audit/v5_2_qd/prospective_2026/postlock_chain_receipts/{game_pk}"
    )
    if (
        log_path.exists()
        or log_path.is_symlink()
        or state_path.exists()
        or state_path.is_symlink()
        or receipt_dir.exists()
        or receipt_dir.is_symlink()
    ):
        raise ValueError("post-lock chain already exists or is inconsistent")
    lock_basis = _canonical_utc_datetime(lock_basis_start_utc, "lock_basis_start_utc")
    persisted = _canonical_utc_datetime(persisted_at_utc, "persisted_at_utc")
    if persisted < lock_basis - timedelta(minutes=45):
        raise ValueError("initial chain state precedes sealed lock")
    log_relative, state_relative = _postlock_relative_paths(game_pk)
    state = PostlockChainState(
        game_pk=game_pk,
        lock_basis_start_utc=lock_basis_start_utc,
        event_count=0,
        head_hash=POSTLOCK_GENESIS_HASH,
        persisted_at_utc=persisted_at_utc,
        audit_path=state_relative.as_posix(),
        event_log_path=log_relative.as_posix(),
    )
    receipt = _receipt_for_state(state, POSTLOCK_RECEIPT_GENESIS_HASH)
    created_receipt: _CreatedPostlockReceipt | None = None
    receipt_creation_error: BaseException | None = None
    receipt_creation_recovery_paths: tuple[Path, ...] = ()
    expected_receipt_path = root_path / receipt.audit_path

    def verify() -> None:
        nonlocal created_receipt, receipt_creation_error
        nonlocal receipt_creation_recovery_paths
        try:
            created_receipt = _create_postlock_receipt_exclusive(root_path, receipt)
        except BaseException as exc:
            receipt_creation_error = exc
            if isinstance(exc, _PostlockReceiptRecoveryError):
                receipt_creation_recovery_paths = exc.recovery_paths
            elif expected_receipt_path.exists() or expected_receipt_path.is_symlink():
                receipt_creation_recovery_paths = (expected_receipt_path,)
            raise
        _fsync_directory(created_receipt.path.parent)
        events, loaded = validate_persisted_postlock_chain(root_path, game_pk=game_pk)
        if events or loaded != state:
            raise ValueError("post-lock initialization postcondition failed")

    def rollback_receipt() -> None:
        if created_receipt is None:
            if (
                receipt_creation_error is not None
                and receipt_creation_recovery_paths
            ):
                raise RuntimeError(
                    "post-lock receipt creation rollback remains unresolved; "
                    f"creation error={type(receipt_creation_error).__name__}: "
                    f"{receipt_creation_error}; recovery paths="
                    f"{[str(path) for path in receipt_creation_recovery_paths]}"
                ) from receipt_creation_error
            return
        try:
            _remove_created_postlock_receipt(created_receipt)
        except BaseException as exc:
            raise RuntimeError(
                "post-lock receipt rollback failed at "
                f"{created_receipt.path}: {type(exc).__name__}: {exc}"
            ) from exc

    _publish_bundle(
        (
            (log_path, b""),
            (state_path, canonical_json_bytes(_postlock_state_raw(state)) + b"\n"),
        ),
        root=root_path,
        post_publish=verify,
        rollback_side_effect=rollback_receipt,
    )
    return state


def initialize_postlock_chain(
    root: Path,
    *,
    game_pk: int,
    lock_basis_start_utc: str,
    persisted_at_utc: str,
) -> PostlockChainState:
    with _postlock_game_lock(root, game_pk):
        return _initialize_postlock_chain_locked(
            root,
            game_pk=game_pk,
            lock_basis_start_utc=lock_basis_start_utc,
            persisted_at_utc=persisted_at_utc,
        )


def _append_and_persist_postlock_start_change_locked(
    root: Path,
    *,
    game_pk: int,
    new_effective_start_utc: str,
    observed_at_utc: str,
    lock_basis_start_utc: str,
    state_persisted_at_utc: str,
) -> tuple[PostlockStartChange, PostlockChainState]:
    root_path, log_path, state_path = _postlock_paths(root, game_pk)
    events, sealed_state = validate_persisted_postlock_chain(
        root_path, game_pk=game_pk
    )
    receipts = load_postlock_chain_receipts(root_path, game_pk=game_pk)
    previous_effective_start_utc = (
        events[-1].new_effective_start_utc
        if events
        else sealed_state.lock_basis_start_utc
    )
    event, new_state = append_postlock_start_change(
        events,
        sealed_state=sealed_state,
        game_pk=game_pk,
        previous_effective_start_utc=previous_effective_start_utc,
        new_effective_start_utc=new_effective_start_utc,
        observed_at_utc=observed_at_utc,
        lock_basis_start_utc=lock_basis_start_utc,
        state_persisted_at_utc=state_persisted_at_utc,
    )
    appended = (*events, event)
    receipt = _receipt_for_state(new_state, receipts[-1].receipt_hash)
    created_receipt: _CreatedPostlockReceipt | None = None
    receipt_creation_error: BaseException | None = None
    receipt_creation_recovery_paths: tuple[Path, ...] = ()
    expected_receipt_path = root_path / receipt.audit_path

    def verify() -> None:
        nonlocal created_receipt, receipt_creation_error
        nonlocal receipt_creation_recovery_paths
        try:
            created_receipt = _create_postlock_receipt_exclusive(root_path, receipt)
        except BaseException as exc:
            receipt_creation_error = exc
            if isinstance(exc, _PostlockReceiptRecoveryError):
                receipt_creation_recovery_paths = exc.recovery_paths
            elif expected_receipt_path.exists() or expected_receipt_path.is_symlink():
                receipt_creation_recovery_paths = (expected_receipt_path,)
            raise
        _fsync_directory(created_receipt.path.parent)
        loaded_events, loaded_state = validate_persisted_postlock_chain(
            root_path, game_pk=game_pk
        )
        if loaded_events != appended or loaded_state != new_state:
            raise ValueError("post-lock append postcondition failed")

    def rollback_receipt() -> None:
        if created_receipt is None:
            if (
                receipt_creation_error is not None
                and receipt_creation_recovery_paths
            ):
                raise RuntimeError(
                    "post-lock receipt creation rollback remains unresolved; "
                    f"creation error={type(receipt_creation_error).__name__}: "
                    f"{receipt_creation_error}; recovery paths="
                    f"{[str(path) for path in receipt_creation_recovery_paths]}"
                ) from receipt_creation_error
            return
        try:
            _remove_created_postlock_receipt(created_receipt)
        except BaseException as exc:
            raise RuntimeError(
                "post-lock receipt rollback failed at "
                f"{created_receipt.path}: {type(exc).__name__}: {exc}"
            ) from exc

    _publish_bundle(
        (
            (log_path, _postlock_event_log_bytes(appended)),
            (
                state_path,
                canonical_json_bytes(_postlock_state_raw(new_state)) + b"\n",
            ),
        ),
        root=root_path,
        post_publish=verify,
        rollback_side_effect=rollback_receipt,
    )
    return event, new_state


def append_and_persist_postlock_start_change(
    root: Path,
    *,
    game_pk: int,
    new_effective_start_utc: str,
    observed_at_utc: str,
    lock_basis_start_utc: str,
    state_persisted_at_utc: str,
) -> tuple[PostlockStartChange, PostlockChainState]:
    with _postlock_game_lock(root, game_pk):
        return _append_and_persist_postlock_start_change_locked(
            root,
            game_pk=game_pk,
            new_effective_start_utc=new_effective_start_utc,
            observed_at_utc=observed_at_utc,
            lock_basis_start_utc=lock_basis_start_utc,
            state_persisted_at_utc=state_persisted_at_utc,
        )


def combine_ledgers(left_run_id: str, right_run_id: str) -> None:
    raise ValueError(f"ledger pooling forbidden: {left_run_id} + {right_run_id}")


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
        "approved_design_commit": CURRENT_APPROVED_DESIGN_COMMIT,
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


def _closed_shape_schema(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        keys = sorted(value)
        return {
            "type": "object",
            "required": keys,
            "properties": {
                key: _closed_shape_schema(value[key]) for key in keys
            },
            "additionalProperties": False,
        }
    if isinstance(value, list):
        return {
            "type": "array",
            "items": _closed_shape_schema(value[0]) if value else {},
        }
    return {}


def prospective_schema(raw: Mapping[str, object]) -> dict[str, object]:
    schema = _closed_shape_schema(raw)
    schema.update(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "const": raw,
        }
    )
    return schema


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
        ),
        root=root,
    )


def write_prospective_contract(
    root: Path, *, approved_design_root: Path | None = None
) -> None:
    design_root = (
        Path(__file__).resolve().parents[1]
        if approved_design_root is None
        else approved_design_root.resolve()
    )
    design_digest = hashlib.sha256(approved_design_bytes(design_root)).hexdigest()
    if design_digest != CURRENT_APPROVED_DESIGN_SHA256:
        raise ValueError("prospective approved design pin differs")
    raw = expected_prospective_spec()
    parse_prospective_spec(raw)
    schema = prospective_schema(raw)
    _publish_bundle(
        (
            (
                root / "protocols/v5_2_qd/prospective_2026.json",
                canonical_json_bytes(raw) + b"\n",
            ),
            (
                root / "schemas/v5_2_prospective.schema.json",
                canonical_json_bytes(schema) + b"\n",
            ),
        ),
        root=root,
    )


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _PinnedDirectory:
    path: Path
    descriptor: int
    device: int
    inode: int
    created: bool
    parent_descriptor: int | None
    name: str | None


@dataclass(frozen=True)
class _PinnedBundlePath:
    path: Path
    name: str
    directory: _PinnedDirectory


@dataclass(frozen=True)
class _PinnedTemporary:
    path: Path
    name: str
    directory: _PinnedDirectory


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _open_pinned_directory(
    path: Path,
    name: str,
    parent_descriptor: int,
    *,
    allow_create: bool,
) -> _PinnedDirectory:
    created = False
    try:
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not allow_create:
            raise ValueError(f"protocol bundle root ancestor is missing: {path}")
        os.mkdir(name, 0o755, dir_fd=parent_descriptor)
        created = True
        try:
            descriptor = os.open(
                name, _directory_open_flags(), dir_fd=parent_descriptor
            )
        except BaseException:
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise ValueError(
            f"protocol bundle directory symlink or special path is forbidden: {path}"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise ValueError(f"protocol bundle directory is not regular: {path}")
    return _PinnedDirectory(
        path=path,
        descriptor=descriptor,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        created=created,
        parent_descriptor=parent_descriptor,
        name=name,
    )


def _pin_bundle_paths(
    root: Path, final_paths: tuple[Path, ...]
) -> tuple[tuple[_PinnedDirectory, ...], tuple[_PinnedBundlePath, ...]]:
    lexical_root = Path(os.path.abspath(root))
    lexical_paths = tuple(Path(os.path.abspath(path)) for path in final_paths)
    if len(set(lexical_paths)) != len(lexical_paths):
        raise ValueError("protocol bundle paths must be unique")
    relatives: list[Path] = []
    for path in lexical_paths:
        try:
            relative = path.relative_to(lexical_root)
        except ValueError as exc:
            raise ValueError("protocol bundle output must be lexically under root") from exc
        if not relative.parts:
            raise ValueError("protocol bundle output must be a file under root")
        relatives.append(relative)
    root_path = _normalize_macos_system_alias(lexical_root)
    absolute_paths = tuple(root_path / relative for relative in relatives)

    directories: list[_PinnedDirectory] = []
    directory_by_path: dict[Path, _PinnedDirectory] = {}
    try:
        root_descriptor = os.open("/", _directory_open_flags())
        root_metadata = os.fstat(root_descriptor)
        slash = _PinnedDirectory(
            path=Path("/"),
            descriptor=root_descriptor,
            device=root_metadata.st_dev,
            inode=root_metadata.st_ino,
            created=False,
            parent_descriptor=None,
            name=None,
        )
        directories.append(slash)
        directory_by_path[slash.path] = slash
        current = slash
        current_path = slash.path
        for index, name in enumerate(root_path.parts[1:]):
            current_path = current_path / name
            current = _open_pinned_directory(
                current_path,
                name,
                current.descriptor,
                allow_create=index == len(root_path.parts[1:]) - 1,
            )
            directories.append(current)
            directory_by_path[current_path] = current
        pinned_root = directory_by_path[root_path]
        for parent in sorted(
            {path.parent for path in absolute_paths}, key=lambda value: len(value.parts)
        ):
            relative_parent = parent.relative_to(root_path)
            current = pinned_root
            current_path = root_path
            for name in relative_parent.parts:
                current_path = current_path / name
                existing = directory_by_path.get(current_path)
                if existing is None:
                    existing = _open_pinned_directory(
                        current_path,
                        name,
                        current.descriptor,
                        allow_create=True,
                    )
                    directories.append(existing)
                    directory_by_path[current_path] = existing
                current = existing
        pinned_paths = tuple(
            _PinnedBundlePath(
                path=path,
                name=path.name,
                directory=directory_by_path[path.parent],
            )
            for path in absolute_paths
        )
        for pinned in pinned_paths:
            try:
                metadata = os.stat(
                    pinned.name,
                    dir_fd=pinned.directory.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError(
                    f"protocol bundle output symlink, hardlink, or special file is forbidden: "
                    f"{pinned.path}"
                )
        _verify_pinned_directories(tuple(directories))
        return tuple(directories), pinned_paths
    except BaseException:
        _remove_created_pinned_directories(tuple(directories))
        for directory in reversed(directories):
            os.close(directory.descriptor)
        raise


def _normalize_macos_system_alias(path: Path) -> Path:
    parts = path.parts
    if len(parts) < 2 or parts[0] != "/" or parts[1] not in {"tmp", "var"}:
        return path
    alias = Path("/") / parts[1]
    try:
        metadata = alias.lstat()
    except FileNotFoundError:
        return path
    expected = Path("/private") / parts[1]
    if stat.S_ISLNK(metadata.st_mode) and alias.resolve() == expected:
        return expected.joinpath(*parts[2:])
    return path


def _verify_pinned_directories(directories: tuple[_PinnedDirectory, ...]) -> None:
    for directory in directories:
        try:
            metadata = directory.path.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ValueError(
                f"protocol bundle directory identity changed: {directory.path}"
            ) from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != directory.device
            or metadata.st_ino != directory.inode
        ):
            raise ValueError(
                f"protocol bundle directory identity changed: {directory.path}"
            )


def _remove_created_pinned_directories(
    directories: tuple[_PinnedDirectory, ...],
) -> None:
    for directory in reversed(directories):
        if (
            not directory.created
            or directory.parent_descriptor is None
            or directory.name is None
        ):
            continue
        try:
            metadata = os.stat(
                directory.name,
                dir_fd=directory.parent_descriptor,
                follow_symlinks=False,
            )
            if (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_dev == directory.device
                and metadata.st_ino == directory.inode
            ):
                os.rmdir(directory.name, dir_fd=directory.parent_descriptor)
        except OSError:
            pass


def _read_pinned_output(path: _PinnedBundlePath) -> tuple[bytes, int] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            path.name, flags, dir_fd=path.directory.descriptor
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(
                f"protocol bundle output symlink, hardlink, or special file is forbidden: "
                f"{path.path}"
            )
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks), stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


def _write_pinned_temporary(
    path: _PinnedBundlePath, contents: bytes, *, mode: int
) -> _PinnedTemporary:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(128):
        name = f".{path.name}.{secrets.token_hex(12)}.tmp"
        try:
            descriptor = os.open(
                name,
                flags,
                mode,
                dir_fd=path.directory.descriptor,
            )
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError(f"unable to reserve protocol temporary for {path.path}")
    temporary = _PinnedTemporary(
        path=path.path.parent / name,
        name=name,
        directory=path.directory,
    )
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("protocol temporary write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.unlink(name, dir_fd=path.directory.descriptor)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    return temporary


def _replace_pinned(
    source_name: str,
    destination_name: str,
    *,
    directory_fd: int,
    source_path: Path,
    destination_path: Path,
) -> None:
    del source_path, destination_path
    os.replace(
        source_name,
        destination_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _unlink_pinned(temporary: _PinnedTemporary) -> None:
    try:
        os.unlink(temporary.name, dir_fd=temporary.directory.descriptor)
    except FileNotFoundError:
        pass


def _publish_bundle(
    entries: tuple[tuple[Path, bytes], ...],
    *,
    root: Path,
    post_publish: Callable[[], None] | None = None,
    rollback_side_effect: Callable[[], None] | None = None,
) -> None:
    final_paths = tuple(path for path, _ in entries)
    directories, pinned_paths = _pin_bundle_paths(root, final_paths)
    temporary_paths: list[_PinnedTemporary] = []
    backups: dict[Path, _PinnedTemporary | None] = {}
    replaced: list[_PinnedBundlePath] = []
    recovery_evidence_paths: list[_PinnedTemporary] = []
    preserve_recovery_files = False
    published = False
    modes: dict[Path, int] = {}
    try:
        existing = {
            pinned.path: _read_pinned_output(pinned) for pinned in pinned_paths
        }
        modes = {
            path: value[1] if value is not None else 0o644
            for path, value in existing.items()
        }
        for pinned, (_, contents) in zip(pinned_paths, entries, strict=True):
            temporary_paths.append(
                _write_pinned_temporary(pinned, contents, mode=modes[pinned.path])
            )
        for pinned in pinned_paths:
            value = existing[pinned.path]
            backups[pinned.path] = (
                _write_pinned_temporary(
                    pinned, value[0], mode=modes[pinned.path]
                )
                if value is not None
                else None
            )
        _verify_pinned_directories(directories)
        for temporary_path, final_path in zip(
            temporary_paths, pinned_paths, strict=True
        ):
            _verify_pinned_directories(directories)
            _replace_pinned(
                temporary_path.name,
                final_path.name,
                directory_fd=final_path.directory.descriptor,
                source_path=temporary_path.path,
                destination_path=final_path.path,
            )
            replaced.append(final_path)
        for directory in {path.directory for path in pinned_paths}:
            os.fsync(directory.descriptor)
        _verify_pinned_directories(directories)
        if post_publish is not None:
            post_publish()
        _verify_pinned_directories(directories)
        published = True
    except BaseException as publish_error:
        rollback_errors: list[tuple[str, BaseException]] = []
        side_effect_failed = False
        if rollback_side_effect is not None:
            try:
                rollback_side_effect()
            except BaseException as error:
                side_effect_failed = True
                rollback_errors.append(("rollback_side_effect", error))
        if side_effect_failed:
            for final_path in replaced:
                try:
                    value = _read_pinned_output(final_path)
                    if value is None:
                        raise FileNotFoundError(final_path.path)
                    recovery_evidence_paths.append(
                        _write_pinned_temporary(
                            final_path,
                            value[0],
                            mode=modes[final_path.path],
                        )
                    )
                except BaseException as error:
                    rollback_errors.append((str(final_path.path), error))
        for final_path in reversed(replaced):
            backup_path = backups[final_path.path]
            try:
                if backup_path is None:
                    try:
                        os.unlink(
                            final_path.name,
                            dir_fd=final_path.directory.descriptor,
                        )
                    except FileNotFoundError:
                        pass
                else:
                    _replace_pinned(
                        backup_path.name,
                        final_path.name,
                        directory_fd=final_path.directory.descriptor,
                        source_path=backup_path.path,
                        destination_path=final_path.path,
                    )
            except BaseException as error:
                rollback_errors.append((str(final_path.path), error))
        for directory in {path.directory for path in pinned_paths}:
            try:
                os.fsync(directory.descriptor)
            except BaseException as error:
                rollback_errors.append((str(directory.path), error))
        if rollback_errors:
            preserve_recovery_files = True
            recovery_paths = tuple(
                sorted(
                    str(path.path)
                    for path in (
                        *temporary_paths,
                        *backups.values(),
                        *recovery_evidence_paths,
                    )
                    if path is not None
                )
            )
            for temporary in (
                *temporary_paths,
                *backups.values(),
                *recovery_evidence_paths,
            ):
                if temporary is None:
                    continue
                try:
                    descriptor = os.open(
                        temporary.name,
                        os.O_RDONLY,
                        dir_fd=temporary.directory.descriptor,
                    )
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except FileNotFoundError:
                    continue
                except BaseException as error:
                    rollback_errors.append((str(temporary.path), error))
            for directory in {path.directory for path in pinned_paths}:
                try:
                    os.fsync(directory.descriptor)
                except BaseException as error:
                    rollback_errors.append((str(directory.path), error))
            failures = "; ".join(
                f"{path}: {type(error).__name__}: {error}"
                for path, error in rollback_errors
            )
            raise RuntimeError(
                "v5.2-QD protocol bundle rollback failed; "
                f"recovery paths={list(recovery_paths)}; "
                f"publish error={type(publish_error).__name__}: {publish_error}; "
                f"rollback errors={failures}"
            ) from rollback_errors[0][1]
        raise
    finally:
        if not preserve_recovery_files:
            for temporary_path in (
                *temporary_paths,
                *backups.values(),
                *recovery_evidence_paths,
            ):
                if temporary_path is not None:
                    _unlink_pinned(temporary_path)
        if not published:
            _remove_created_pinned_directories(directories)
        for directory in reversed(directories):
            os.close(directory.descriptor)


def approved_design_bytes(root: Path) -> bytes:
    completed = subprocess.run(
        [
            "git",
            "show",
            f"{CURRENT_APPROVED_DESIGN_COMMIT}:docs/superpowers/specs/"
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
    design_root = (
        Path(os.path.abspath(root))
        if approved_design_root is None
        else approved_design_root.resolve()
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
        "approved_design_commit": CURRENT_APPROVED_DESIGN_COMMIT,
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
        ),
        root=root,
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


def write_protocol_artifacts(
    root: Path, *, approved_design_root: Path | None = None
) -> None:
    write_v52_config_contracts(
        root,
        approved_design_root=approved_design_root,
    )
    write_historical_contract(root)
    write_prospective_contract(
        root,
        approved_design_root=approved_design_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write deterministic unfitted v5.2-QD protocol contracts."
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--approved-design-root", type=Path)
    parser.add_argument("--write-contracts", action="store_true")
    args = parser.parse_args(argv)
    if not args.write_contracts:
        parser.error("--write-contracts is required")
    write_protocol_artifacts(
        args.root.resolve(),
        approved_design_root=args.approved_design_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
