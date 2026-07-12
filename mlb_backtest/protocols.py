from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
import argparse
import hashlib
import json
import math
import subprocess

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
INTERNAL_METRIC_LITERALS = {
    component: {
        metric: {
            "weight": weight,
            "direction": EXPECTED_DIRECTIONS[component][metric],
        }
        for metric, weight in INTERNAL_WEIGHTS[component].items()
    }
    for component in ("starter", "offense", "bullpen")
}
INTERNAL_METRIC_LITERALS.update(
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
        for index, (item, expected_item) in enumerate(zip(value, expected, strict=True)):
            _validate_literal(item, expected_item, f"{path}[{index}]")
        return
    if type(value) is not type(expected) or value != expected:
        raise ValueError(f"{path} frozen value differs")


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
        "internal_metrics": INTERNAL_METRIC_LITERALS,
        "shrinkage_constants": dict(SHRINKAGE_CONSTANTS),
        "probability_clip": [0.10, 0.90],
        "score_rules": {
            "neutral_score": 50,
            "missing_weight_redistribution": False,
            "probability_transform": "sigmoid(alpha+beta*D/10)",
            "predicted_side": (
                "home if p_home>.5; away if p_home<.5; none if equal"
            ),
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
    forbidden_keys = {"normalization", "slot_weights", "alpha", "beta", "fitted_model_cards"}

    def collect_keys(value: object) -> set[str]:
        if isinstance(value, Mapping):
            return set(value) | {
                nested
                for child in value.values()
                for nested in collect_keys(child)
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
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{path} must be finite numeric")
    return float(value)


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
    if (
        not 0.5 <= p <= 0.9
        or not 0 <= q <= 10
        or edge < 0
        or not isinstance(lock_basis_start_utc, str)
        or not lock_basis_start_utc
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
    config = expected_v52_config()
    parse_v52_config(config)
    config_path = root / "config/v5_2_qd.json"
    write_json(config_path, config)
    change = {
        "schema_version": 1,
        "model_version": "v5.2-QD",
        "unchanged": list(CHANGE_UNCHANGED),
        "changed": list(CHANGE_CHANGED),
        "forbidden_from_2025_diagnostic": list(CHANGE_FORBIDDEN),
        "config_sha256": sha256_file(config_path),
        "approved_design_commit": "a4aa8d2",
        "approved_design_blob_sha256": hashlib.sha256(
            approved_design_bytes(design_root)
        ).hexdigest(),
    }
    write_json(
        root / "schemas/v5_2_qd.schema.json",
        const_schema(config, V52_REQUIRED_KEYS),
    )
    write_json(root / "protocols/v5_2_qd/change_contract.json", change)


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
