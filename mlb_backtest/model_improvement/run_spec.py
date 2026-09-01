"""Closed, immutable declaration for the parallel model-result experiment."""

from __future__ import annotations

import json
import math
import re
import subprocess
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .canonical import canonical_json_bytes, read_regular_bytes
from .contracts import COMPONENTS, _freeze_json, _thaw_json


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEAD = re.compile(r"^[0-9a-f]{40}$")
_NAMESPACES = (
    "source_gate", "features", "baseline", "calibration", "oof", "lane_a", "lane_b",
    "lane_c", "registry", "h24_predictions", "h24_reports",
)
_CALIBRATION_FOLD_KEYS = {
    "O19": "C19", "O20": "C20", "O21": "C21", "O22": "C22O",
    "H22": "C22H", "O23": "C23O", "H23": "C23H",
}
_CALIBRATION_POOLS = {
    "C19": (2019, "2018-01-01", "2018-12-31"),
    "C20": (2020, "2018-01-01", "2019-12-31"),
    "C21": (2021, "2018-01-01", "2020-12-31"),
    "C22O": (2022, "2018-01-01", "2021-12-31"),
    "C22H": (2022, "2018-01-01", "2021-12-31"),
    "C23O": (2023, "2018-01-01", "2022-12-31"),
    "C23H": (2023, "2018-01-01", "2022-12-31"),
}
_CALIBRATION_FIT_SPEC = {
    "schema": "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1",
    "algorithm": "fit_logistic_v1", "edge_divisor": 10.0,
    "iterations": 100, "l2": 1e-6, "tolerance": 1e-10,
    "min_rows": 2, "required_classes": [0, 1],
}
_CALIBRATION_TEMPLATES = {
    "baseline_rows_relative_template": "baseline/v5.1/{fold_id}/calibration_rows.jsonl",
    "baseline_card_relative_template": "baseline/v5.1/{fold_id}/probability_calibration_card.json",
    "candidate_rows_relative_template": "{lane_id}/{candidate_segment}/{fold_id}/calibration_rows.jsonl",
    "candidate_card_relative_template": "{lane_id}/{candidate_segment}/{fold_id}/probability_calibration_card.json",
}
_ORDER = ("brier", "log_loss", "daily_top1_delta_desc", "candidate_id")
_WEIGHTS = {"starter": 35, "offense": 25, "bullpen": 20, "environment": 10, "defense": 5, "schedule": 5}
_CONTRACT = {
    "component_order": list(COMPONENTS),
    "edge_formula": "sum(component_weights_pp[c]/100 * (home_score[c]-away_score[c]) for c in component_order)",
    "completeness_formula": "sum(component_weights_pp[c]/100 * min(home_quality[c],away_quality[c]) for c in component_order)",
    "confidence_formula": "scoring.confidence_score(completeness, starter_quality, offense_quality, bullpen_quality, component_agreement(starter_edge,offense_edge,bullpen_edge,picked_side), environment_quality, schedule_quality)",
    "confidence_range": [0.0, 10.0],
    "component_weights_binding": {
        "keys": list(COMPONENTS), "value_type": "integer_nonnegative_pp", "sum": 100,
        "source": "closed_union:RunSpec.baseline.card.component_weights_pp|ScoringModelCard.parameters.component_weights_pp",
    },
    "clip": {"min": 0.10, "max": 0.90},
    "pick": {"home_if": "p_home > 0.5", "away_if": "p_home < 0.5", "none_if": "p_home == 0.5"},
    "ranking": {"tie_order": ["pick_probability:desc", "confidence:desc", "starter_edge_abs:desc", "scheduled_start_utc:asc", "game_pk:asc"]},
}


def _exact(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _normal_relative(value: object, label: str) -> str:
    text = _text(value, label)
    path = Path(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a normal relative path")
    return path.as_posix()


def _normal_absolute(value: object, label: str) -> str:
    text = _text(value, label)
    path = Path(text)
    if (
        "\\" in text
        or not path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != text
    ):
        raise ValueError(f"{label} must be a normal absolute path")
    return text


def _output_root(value: object) -> str:
    """Accept an absolute root or a lexical repository-relative root only."""
    text = _text(value, "outputs.root")
    path = Path(text)
    if "\\" in text or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("outputs.root must be a normal path")
    return path.as_posix()


def _array(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _exact_ints(value: object, expected: tuple[int, ...], label: str) -> tuple[int, ...]:
    actual = _array(value, label)
    if any(type(item) is not int for item in actual) or actual != expected:
        raise ValueError(f"{label} differs")
    return actual


@dataclass(frozen=True, slots=True)
class ProbabilityCalibrationFitSpec:
    schema: str
    algorithm: str
    edge_divisor: float
    iterations: int
    l2: float
    tolerance: float
    min_rows: int
    required_classes: tuple[int, ...]

    def as_mapping(self) -> dict[str, object]:
        return {"schema": self.schema, "algorithm": self.algorithm,
                "edge_divisor": self.edge_divisor, "iterations": self.iterations,
                "l2": self.l2, "tolerance": self.tolerance,
                "min_rows": self.min_rows, "required_classes": list(self.required_classes)}


@dataclass(frozen=True, slots=True)
class ProbabilityCalibrationPool:
    target_year: int
    training_start: str
    training_end: str

    def as_mapping(self) -> dict[str, object]:
        return {"target_year": self.target_year, "training_start": self.training_start,
                "training_end": self.training_end}


@dataclass(frozen=True, slots=True)
class ProbabilityCalibrationPolicy:
    schema: str
    fold_keys: Mapping[str, str]
    fit_spec: ProbabilityCalibrationFitSpec
    pools: Mapping[str, ProbabilityCalibrationPool]
    baseline_rows_relative_template: str
    baseline_card_relative_template: str
    candidate_rows_relative_template: str
    candidate_card_relative_template: str

    def as_mapping(self) -> dict[str, object]:
        return {"schema": self.schema, "fold_keys": dict(self.fold_keys),
                "fit_spec": self.fit_spec.as_mapping(),
                "pools": {key: self.pools[key].as_mapping() for key in _CALIBRATION_POOLS},
                **_CALIBRATION_TEMPLATES}


def _probability_calibration_policy(value: object) -> ProbabilityCalibrationPolicy:
    keys = {"schema", "fold_keys", "fit_spec", "pools", *_CALIBRATION_TEMPLATES}
    values = _exact(value, keys, "probability calibration policy")
    if values["schema"] != "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_POLICY_V1":
        raise ValueError("probability calibration policy schema differs")
    fold_keys = _exact(values["fold_keys"], set(_CALIBRATION_FOLD_KEYS), "probability calibration fold keys")
    if dict(fold_keys) != _CALIBRATION_FOLD_KEYS:
        raise ValueError("probability calibration fold keys differ")
    fit = _exact(values["fit_spec"], set(_CALIBRATION_FIT_SPEC), "probability calibration fit spec")
    if canonical_json_bytes(fit) != canonical_json_bytes(_CALIBRATION_FIT_SPEC):
        raise ValueError("probability calibration fit spec differs")
    pools_value = _exact(values["pools"], set(_CALIBRATION_POOLS), "probability calibration pools")
    pools: dict[str, ProbabilityCalibrationPool] = {}
    for key, expected in _CALIBRATION_POOLS.items():
        pool = _exact(pools_value[key], {"target_year", "training_start", "training_end"}, f"probability calibration pool {key}")
        if (pool["target_year"], pool["training_start"], pool["training_end"]) != expected:
            raise ValueError("probability calibration pool differs")
        pools[key] = ProbabilityCalibrationPool(*expected)
    for key, expected in _CALIBRATION_TEMPLATES.items():
        if values[key] != expected:
            raise ValueError("probability calibration path template differs")
    fit_spec = ProbabilityCalibrationFitSpec(
        "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_FIT_SPEC_V1", "fit_logistic_v1",
        10.0, 100, 1e-6, 1e-10, 2, (0, 1),
    )
    return ProbabilityCalibrationPolicy(
        "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_POLICY_V1",
        MappingProxyType(dict(_CALIBRATION_FOLD_KEYS)), fit_spec, MappingProxyType(pools),
        **_CALIBRATION_TEMPLATES,
    )


@dataclass(frozen=True, slots=True)
class PathDigest:
    path: str
    sha256: str

    def as_mapping(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


def _path_digest(value: object, label: str) -> PathDigest:
    values = _exact(value, {"path", "sha256"}, label)
    return PathDigest(_normal_relative(values["path"], f"{label}.path"), _digest(values["sha256"], f"{label}.sha256"))


@dataclass(frozen=True, slots=True)
class SourceAuthority:
    authority_root: str
    source_run_spec: PathDigest
    acquisition_contract: PathDigest
    terminal_checkpoint: PathDigest
    inventory: PathDigest
    validation_receipts: tuple[PathDigest, ...]

    def as_mapping(self) -> dict[str, object]:
        return {
            "authority_root": self.authority_root,
            "source_run_spec": self.source_run_spec.as_mapping(),
            "acquisition_contract": self.acquisition_contract.as_mapping(),
            "terminal_checkpoint": self.terminal_checkpoint.as_mapping(),
            "inventory": self.inventory.as_mapping(),
            "validation_receipts": [item.as_mapping() for item in self.validation_receipts],
        }


def _source_authority(value: object) -> SourceAuthority:
    values = _exact(value, {"authority_root", "source_run_spec", "acquisition_contract", "terminal_checkpoint", "inventory", "validation_receipts"}, "source authority")
    receipts = tuple(_path_digest(item, "source authority.validation_receipts") for item in _array(values["validation_receipts"], "source authority.validation_receipts"))
    if not receipts or len({item.path for item in receipts}) != len(receipts):
        raise ValueError("source authority validation receipts differ")
    return SourceAuthority(
        _normal_relative(values["authority_root"], "source authority.authority_root"),
        _path_digest(values["source_run_spec"], "source authority.source_run_spec"),
        _path_digest(values["acquisition_contract"], "source authority.acquisition_contract"),
        _path_digest(values["terminal_checkpoint"], "source authority.terminal_checkpoint"),
        _path_digest(values["inventory"], "source authority.inventory"), receipts,
    )


@dataclass(frozen=True, slots=True)
class FoldSpec:
    target_year: int
    normalization_years: tuple[int, ...]
    oof_calibration_years: tuple[int, ...]
    prediction_only: bool

    def as_mapping(self) -> dict[str, object]:
        return {"target_year": self.target_year, "normalization_years": list(self.normalization_years), "oof_calibration_years": list(self.oof_calibration_years), "prediction_only": self.prediction_only}


_FOLD_EXPECTATIONS = {
    "H22": FoldSpec(2022, (2018, 2019, 2020, 2021), (2019, 2020, 2021), False),
    "H23": FoldSpec(2023, (2019, 2020, 2021, 2022), (2019, 2020, 2021, 2022), False),
    "H24": FoldSpec(2024, (2020, 2021, 2022, 2023), (2019, 2020, 2021, 2022, 2023), True),
}

# Task8's fixed rolling-origin OOF population and historical fold-year map.
TASK8_FOLD_TARGET_YEAR: Mapping[str, int] = MappingProxyType({
    "O19": 2019, "O20": 2020, "O21": 2021, "O22": 2022, "O23": 2023,
    "H22": 2022, "H23": 2023,
})
TASK8_FOLD_IDS = tuple(TASK8_FOLD_TARGET_YEAR)


def task8_target_year(fold_id: str) -> int:
    try:
        return TASK8_FOLD_TARGET_YEAR[fold_id]
    except KeyError as exc:
        raise ValueError("Task8 fold differs") from exc


def _fold(value: object, fold_id: str) -> FoldSpec:
    values = _exact(value, {"target_year", "normalization_years", "oof_calibration_years", "prediction_only"}, f"fold {fold_id}")
    expected = _FOLD_EXPECTATIONS[fold_id]
    target = values["target_year"]
    if type(target) is not int or target != expected.target_year or type(values["prediction_only"]) is not bool or values["prediction_only"] != expected.prediction_only:
        raise ValueError(f"fold {fold_id} chronology differs")
    return FoldSpec(target, _exact_ints(values["normalization_years"], expected.normalization_years, f"fold {fold_id} normalization chronology"), _exact_ints(values["oof_calibration_years"], expected.oof_calibration_years, f"fold {fold_id} chronology"), expected.prediction_only)


@dataclass(frozen=True, slots=True)
class BaselineCard:
    path: str
    sha256: str
    component_weights_pp: Mapping[str, int]

    def as_mapping(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256, "component_weights_pp": dict(self.component_weights_pp)}


@dataclass(frozen=True, slots=True)
class BaselineSpec:
    card: BaselineCard

    def as_mapping(self) -> dict[str, object]:
        return {"card": self.card.as_mapping()}


def _weights(value: object, label: str, expected: Mapping[str, int] | None = None) -> Mapping[str, int]:
    values = _exact(value, set(COMPONENTS), label)
    if any(type(values[key]) is not int or values[key] < 0 for key in COMPONENTS) or sum(values.values()) != 100:
        raise ValueError(f"{label} must be nonnegative integer pp totaling 100")
    materialized = {key: values[key] for key in COMPONENTS}
    if expected is not None and materialized != dict(expected):
        raise ValueError(f"{label} differs")
    return MappingProxyType(materialized)


def _baseline(value: object) -> BaselineSpec:
    values = _exact(value, {"card"}, "baseline")
    card = _exact(values["card"], {"path", "sha256", "component_weights_pp"}, "baseline card")
    return BaselineSpec(BaselineCard(_normal_relative(card["path"], "baseline card.path"), _digest(card["sha256"], "baseline card.sha256"), _weights(card["component_weights_pp"], "baseline card.component_weights_pp", _WEIGHTS)))


@dataclass(frozen=True, slots=True)
class LaneBSpec:
    base_weights_pp: Mapping[str, int]
    fixed_weights_pp: Mapping[str, int]
    grid: Mapping[str, tuple[int, ...]]
    multipliers: tuple[float, ...]
    candidate_count: int
    keep_h22_count: int
    h22_order: tuple[str, ...]
    h23_eligibility: Mapping[str, str]
    h23_order: tuple[str, ...]

    def as_mapping(self) -> dict[str, object]:
        return {"base_weights_pp": dict(self.base_weights_pp), "fixed_weights_pp": dict(self.fixed_weights_pp), "grid": {key: list(value) for key, value in self.grid.items()}, "multipliers": list(self.multipliers), "candidate_count": self.candidate_count, "keep_h22_count": self.keep_h22_count, "h22_order": list(self.h22_order), "h23_eligibility": dict(self.h23_eligibility), "h23_order": list(self.h23_order)}


def _lane_b(value: object) -> LaneBSpec:
    values = _exact(value, {"base_weights_pp", "fixed_weights_pp", "grid", "multipliers", "candidate_count", "keep_h22_count", "h22_order", "h23_eligibility", "h23_order"}, "Lane B")
    base = _weights(values["base_weights_pp"], "Lane B base weights", _WEIGHTS)
    fixed = _exact(values["fixed_weights_pp"], {"environment", "defense"}, "Lane B fixed weights")
    if fixed != {"environment": 10, "defense": 5}:
        raise ValueError("Lane B fixed weights differ")
    grid_values = _exact(values["grid"], {"starter", "offense", "bullpen", "schedule"}, "Lane B grid")
    wanted = {"starter": (25, 30, 35, 40, 45), "offense": (15, 20, 25, 30, 35), "bullpen": (10, 15, 20, 25, 30), "schedule": (0, 5, 10, 15)}
    grid = {key: _exact_ints(grid_values[key], wanted[key], f"Lane B grid {key}") for key in wanted}
    multipliers = _array(values["multipliers"], "Lane B multipliers")
    if any(type(item) is not float or not math.isfinite(item) for item in multipliers):
        raise ValueError("Lane B multipliers must be canonical finite numbers")
    if multipliers != (0.75, 1.0, 1.25):
        raise ValueError("Lane B multipliers differ")
    candidate_count = values["candidate_count"]
    if type(candidate_count) is not int or candidate_count != 210:
        raise ValueError("Lane B candidate count differs")
    computed = sum(1 for s in grid["starter"] for o in grid["offense"] for b in grid["bullpen"] for q in grid["schedule"] if s + o + b + q == 85) * len(multipliers)
    if computed != candidate_count:
        raise ValueError("Lane B grid does not produce 210 candidates")
    if values["keep_h22_count"] != 12 or type(values["keep_h22_count"]) is not int:
        raise ValueError("Lane B keep_h22_count differs")
    h22 = _array(values["h22_order"], "Lane B h22_order")
    h23 = _array(values["h23_order"], "Lane B h23_order")
    if h22 != _ORDER or h23 != _ORDER or not all(isinstance(item, str) for item in h22):
        raise ValueError("Lane B selection order differs")
    eligibility = _exact(values["h23_eligibility"], {"brier_no_worse_than", "log_loss_no_worse_than", "daily_top1_delta_vs_v51"}, "Lane B h23 eligibility")
    expected = {"brier_no_worse_than": "A", "log_loss_no_worse_than": "A", "daily_top1_delta_vs_v51": "positive"}
    if dict(eligibility) != expected:
        raise ValueError("Lane B h23 eligibility differs")
    return LaneBSpec(base, MappingProxyType(dict(fixed)), MappingProxyType(grid), multipliers, candidate_count, 12, h22, MappingProxyType(expected), h23)


@dataclass(frozen=True, slots=True)
class LaneCSpec:
    parent_model_id: str
    slots: Mapping[str, tuple[str, ...]]
    coverage: Mapping[str, str]

    def as_mapping(self) -> dict[str, object]:
        return {"parent_model_id": self.parent_model_id, "slots": {key: list(value) for key, value in self.slots.items()}, "coverage": dict(self.coverage)}


def _lane_c(value: object) -> LaneCSpec:
    values = _exact(value, {"parent_model_id", "slots", "coverage"}, "Lane C")
    slots_raw = _exact(values["slots"], {"environment", "defense"}, "Lane C slots")
    slots = {"environment": ("park", "weather_roof"), "defense": ("oaa", "framing", "blocking_throwing")}
    if values["parent_model_id"] != "A+B" or any(_array(slots_raw[key], f"Lane C {key}") != wanted for key, wanted in slots.items()):
        raise ValueError("Lane C slots or parent differ")
    coverage = _exact(values["coverage"], {"H22", "H23", "H24"}, "Lane C coverage")
    expected = {"H22": "admitted_or_neutral_100pct", "H23": "admitted_or_neutral_100pct", "H24": "prediction_only_admitted_or_neutral_100pct"}
    if dict(coverage) != expected:
        raise ValueError("Lane C coverage differs")
    return LaneCSpec("A+B", MappingProxyType(slots), MappingProxyType(expected))


@dataclass(frozen=True, slots=True)
class H24Firewall:
    forbid_outcomes_before_registry: bool
    forbidden_outcome_years: tuple[int, ...]
    prediction_only_year: int

    def as_mapping(self) -> dict[str, object]:
        return {"forbid_outcomes_before_registry": self.forbid_outcomes_before_registry, "forbidden_outcome_years": list(self.forbidden_outcome_years), "prediction_only_year": self.prediction_only_year}


def _firewall(value: object) -> H24Firewall:
    values = _exact(value, {"forbid_outcomes_before_registry", "forbidden_outcome_years", "prediction_only_year"}, "H24 firewall")
    if values["forbid_outcomes_before_registry"] is not True or _exact_ints(values["forbidden_outcome_years"], (2025,), "H24 forbidden outcome years") != (2025,) or values["prediction_only_year"] != 2024:
        raise ValueError("H24 firewall differs")
    return H24Firewall(True, (2025,), 2024)


@dataclass(frozen=True, slots=True)
class VerifiedRunSpecActivation:
    declaration_path: str
    declaration_sha256: str
    run_spec_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "declaration_path", _normal_absolute(self.declaration_path, "verified activation declaration_path"))
        object.__setattr__(self, "declaration_sha256", _digest(self.declaration_sha256, "verified activation declaration_sha256"))
        object.__setattr__(self, "run_spec_sha256", _digest(self.run_spec_sha256, "verified activation run_spec_sha256"))


@dataclass(frozen=True, slots=True)
class ModelImprovementRunSpec:
    schema_version: str
    run_id: str
    code_revision: str
    source_authority: SourceAuthority
    cutoffs: Mapping[str, str]
    exclusions: Mapping[str, object]
    folds: Mapping[str, FoldSpec]
    baseline: BaselineSpec
    metrics: Mapping[str, tuple[str, ...]]
    prediction_contract: Mapping[str, object]
    prediction_contract_sha256: str
    probability_calibration_policy: ProbabilityCalibrationPolicy
    lane_b: LaneBSpec
    lane_c: LaneCSpec
    outputs: Mapping[str, object]
    h24_firewall: H24Firewall
    verified_activation: VerifiedRunSpecActivation | None = None

    def fold(self, fold_id: str) -> FoldSpec:
        if fold_id not in _FOLD_EXPECTATIONS:
            raise ValueError("only H22, H23, and H24 folds are available")
        return self.folds[fold_id]

    def output_path(self, namespace: str, relative_name: str) -> Path:
        if namespace not in _NAMESPACES:
            raise ValueError("unknown output namespace")
        if namespace == "calibration" and (
            not isinstance(relative_name, str) or "//" in relative_name
        ):
            raise ValueError("calibration output path differs")
        relative = _normal_relative(relative_name, "output relative name")
        parts = Path(relative).parts
        if namespace == "features":
            allowed = {
                ("common", fold) for fold in ("O19", "O20", "O21", "O22", "O23", "H22", "H23", "H24")
            } | {("lane_c", fold) for fold in ("H22", "H23", "H24")}
            if len(parts) != 3 or tuple(parts[:2]) not in allowed or parts[2] not in {"feature_seal.json", "rows.jsonl"}:
                raise ValueError("feature output path differs")
        if namespace == "calibration":
            allowed_leaves = {"calibration_rows.jsonl", "probability_calibration_card.json"}
            baseline = len(parts) == 4 and parts[:2] == ("baseline", "v5.1") and parts[2] in _CALIBRATION_FOLD_KEYS
            candidate = (
                len(parts) == 4 and parts[0] in {"A", "B", "C"}
                and _candidate_segment(parts[1]) and parts[2] in _CALIBRATION_FOLD_KEYS
            )
            if (not (baseline or candidate)) or parts[-1] not in allowed_leaves:
                raise ValueError("calibration output path differs")
        configured_root = Path(str(self.outputs["root"]))
        root = configured_root if configured_root.is_absolute() else _repository_root() / configured_root
        candidate = root / namespace / relative
        try:
            candidate.relative_to(root / namespace)
        except ValueError as exc:
            raise ValueError("output path escapes its namespace") from exc
        return candidate

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id, "code_revision": self.code_revision,
            "source_authority": self.source_authority.as_mapping(), "cutoffs": dict(self.cutoffs),
            "exclusions": _thaw_json(self.exclusions), "folds": {key: self.folds[key].as_mapping() for key in ("H22", "H23", "H24")},
            "baseline": self.baseline.as_mapping(), "metrics": {key: list(value) for key, value in self.metrics.items()},
            "prediction_contract": _thaw_json(self.prediction_contract), "prediction_contract_sha256": self.prediction_contract_sha256,
            "probability_calibration_policy": self.probability_calibration_policy.as_mapping(),
            "lane_b": self.lane_b.as_mapping(), "lane_c": self.lane_c.as_mapping(),
            "outputs": _thaw_json(self.outputs), "h24_firewall": self.h24_firewall.as_mapping(),
        }


def _candidate_segment(value: object) -> bool:
    return isinstance(value, str) and value not in {".", ".."} and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value))


def build_run_spec(raw: Mapping[str, object]) -> ModelImprovementRunSpec:
    values = _exact(raw, {"schema_version", "run_id", "code_revision", "source_authority", "cutoffs", "exclusions", "folds", "baseline", "metrics", "prediction_contract", "prediction_contract_sha256", "probability_calibration_policy", "lane_b", "lane_c", "outputs", "h24_firewall"}, "run spec")
    if values["schema_version"] not in {"SYNTHETIC_TEST_ONLY", "PARALLEL_MODEL_RESULT_RUN_SPEC_V1"}:
        raise ValueError("unknown schema version")
    code_revision = _text(values["code_revision"], "code_revision")
    if not _HEAD.fullmatch(code_revision):
        raise ValueError("code_revision must be a full lowercase git HEAD")
    cutoffs = _exact(values["cutoffs"], {"history", "snapshot"}, "cutoffs")
    if dict(cutoffs) != {"history": "D-1", "snapshot": "T-45"}:
        raise ValueError("cutoffs differ")
    exclusions = _exact(values["exclusions"], {"excluded_years", "forbidden_outcome_years", "forbid_h24_outcomes"}, "exclusions")
    if _exact_ints(exclusions["excluded_years"], (2025,), "excluded years") != (2025,) or _exact_ints(exclusions["forbidden_outcome_years"], (2025,), "forbidden outcome years") != (2025,) or exclusions["forbid_h24_outcomes"] is not True:
        raise ValueError("exclusions differ")
    folds_raw = _exact(values["folds"], {"H22", "H23", "H24"}, "folds")
    folds = MappingProxyType({fold_id: _fold(folds_raw[fold_id], fold_id) for fold_id in ("H22", "H23", "H24")})
    metrics = _exact(values["metrics"], {"all_game", "daily"}, "metrics")
    if _array(metrics["all_game"], "metrics.all_game") != ("brier", "log_loss") or _array(metrics["daily"], "metrics.daily") != ("top1_date_paired",):
        raise ValueError("metrics differ")
    contract = _freeze_json(values["prediction_contract"])
    if not isinstance(contract, Mapping):
        raise ValueError("prediction contract must be an object")
    contract_digest = _digest(values["prediction_contract_sha256"], "prediction_contract_sha256")
    if contract_digest != sha256(canonical_json_bytes(_thaw_json(contract))).hexdigest():
        raise ValueError("prediction contract digest differs")
    contract_mapping = _thaw_json(contract)
    if isinstance(contract_mapping, Mapping) and contract_mapping.get("component_weights_binding") != _CONTRACT["component_weights_binding"]:
        raise ValueError("component weights binding differs")
    if canonical_json_bytes(values["prediction_contract"]) != canonical_json_bytes(_CONTRACT):
        raise ValueError("prediction contract differs or contains noncanonical values")
    if contract_mapping != _CONTRACT:
        raise ValueError("prediction contract differs or contains unknown keys")
    outputs = _exact(values["outputs"], {"root", "namespaces"}, "outputs")
    root = _output_root(outputs["root"])
    if _array(outputs["namespaces"], "outputs.namespaces") != _NAMESPACES:
        raise ValueError("output namespaces differ")
    return ModelImprovementRunSpec(
        values["schema_version"], _text(values["run_id"], "run_id"), code_revision, _source_authority(values["source_authority"]),
        MappingProxyType(dict(cutoffs)), _freeze_json(exclusions), folds, _baseline(values["baseline"]),
        MappingProxyType({"all_game": ("brier", "log_loss"), "daily": ("top1_date_paired",)}), contract, contract_digest,
        _probability_calibration_policy(values["probability_calibration_policy"]),
        _lane_b(values["lane_b"]), _lane_c(values["lane_c"]), _freeze_json({"root": root, "namespaces": list(_NAMESPACES)}), _firewall(values["h24_firewall"]),
    )


def parse_run_spec(raw: object) -> ModelImprovementRunSpec:
    if not isinstance(raw, Mapping):
        raise ValueError("run spec must be an object")
    return build_run_spec(raw)


def load_run_spec(path: Path) -> ModelImprovementRunSpec:
    return _parse_run_spec_bytes(read_regular_bytes(path, "run spec"))


def _parse_run_spec_bytes(raw_bytes: bytes) -> ModelImprovementRunSpec:
    try:
        raw = json.loads(raw_bytes)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("run spec is not JSON") from exc
    return parse_run_spec(raw)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_at(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("could not read RunSpec owning repository") from exc
    return completed.stdout


def _git_bytes_at(root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=False,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("could not read RunSpec owning repository") from exc
    return completed.stdout


def _git_object_exists(root: Path, object_name: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", object_name],
            capture_output=True,
            text=False,
        )
    except OSError as exc:
        raise ValueError("could not read RunSpec owning repository") from exc
    if completed.returncode == 0:
        return True
    if completed.returncode == 1 or (
        completed.returncode == 128
        and (
            b"does not exist in" in completed.stderr
            or b"exists on disk, but not in" in completed.stderr
        )
    ):
        return False
    raise ValueError("could not read RunSpec owning repository")


def _owning_repository(path: Path) -> Path:
    root_text = _git_at(path.parent, "rev-parse", "--show-toplevel").strip()
    root = Path(root_text)
    if not root.is_absolute():
        raise ValueError("RunSpec owning repository root is not absolute")
    return root


def load_verified_run_spec(path: Path) -> ModelImprovementRunSpec:
    owning_root = _owning_repository(path)
    relative = "protocols/model_improvement/parallel_model_result_v1.json"
    expected = owning_root / relative
    if path.resolve() != expected:
        raise ValueError("verified run spec requires the expected production path")
    head = _git_at(owning_root, "rev-parse", "HEAD").strip()
    parents = _git_at(owning_root, "rev-list", "--parents", "-n", "1", "HEAD").split()
    if not _HEAD.fullmatch(head) or len(parents) != 2 or parents[0] != head or not _HEAD.fullmatch(parents[1]):
        raise ValueError("verified run spec requires a single-parent activation commit")
    parent = _git_at(owning_root, "rev-parse", "HEAD^").strip()
    if parent != parents[1]:
        raise ValueError("activation parent differs")
    changed = _git_at(owning_root, "diff", "--name-only", f"{parent}..{head}").splitlines()
    if changed != [relative]:
        raise ValueError("activation commit is not declaration-only")
    if _git_object_exists(owning_root, f"{parent}:{relative}"):
        raise ValueError("activation commit must add declaration")
    tracked_bytes = _git_bytes_at(owning_root, "show", f"{head}:{relative}")
    if read_regular_bytes(path, "verified run spec") != tracked_bytes:
        raise ValueError("verified run spec differs from tracked blob")
    if _git_at(owning_root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("verified run spec requires a clean worktree")
    spec = _parse_run_spec_bytes(tracked_bytes)
    if spec.schema_version == "SYNTHETIC_TEST_ONLY":
        raise ValueError("synthetic run spec cannot be verified")
    if spec.code_revision != parent:
        raise ValueError("run spec code_revision differs from activation parent")
    return replace(
        spec,
        verified_activation=VerifiedRunSpecActivation(
            str(path.resolve()),
            sha256(tracked_bytes).hexdigest(),
            run_spec_sha256(spec),
        ),
    )


def run_spec_sha256(spec: ModelImprovementRunSpec) -> str:
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("run_spec_sha256 requires ModelImprovementRunSpec")
    return sha256(canonical_json_bytes(spec.as_mapping())).hexdigest()
