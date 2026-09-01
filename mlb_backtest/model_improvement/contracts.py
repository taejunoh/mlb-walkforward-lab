"""Pure immutable payload contracts for model-improvement artifacts."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from types import MappingProxyType
from typing import Mapping

from .canonical import canonical_json_bytes


MODEL_IDS = frozenset({"v5.1", "A", "A+B", "A+B+C"})
FROZEN_CARD_MODEL_IDS = frozenset({"A", "A+B", "A+B+C"})
FROZEN_CARD_STATES = frozenset({"H23_FROZEN"})
LEDGER_IDS = frozenset({"O19", "O20", "O21", "O22", "O23", "H22", "H23", "H24"})
PICKED_SIDES = frozenset({"away", "home", "none"})
QUALITY_FAMILIES = frozenset(
    {None, "starter", "hitter_split", "recent_form", "bullpen"}
)
SIDES = ("away", "home")
COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_UTC_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?Z$"
)
_MULTIPLIER_FAMILIES = QUALITY_FAMILIES - {None}


def _require_exact_mapping(
    value: object, keys: frozenset[str], label: str
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _require_sequence(value: object, label: str) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_optional_sha256(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_sha256(value, label)


def _require_number(
    value: object,
    label: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite")
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and numeric < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return numeric


def _require_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_year(value: object, label: str) -> int:
    year = _require_positive_int(value, label)
    if year < 1876 or year > 9999:
        raise ValueError(f"{label} must be a sensible baseball year")
    return year


def _require_date(value: object, label: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(f"{label} must be an ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    return value


def _require_utc(value: object, label: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    try:
        datetime.strptime(
            value,
            "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ",
        )
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC timestamp") from exc
    return value


def _freeze_json(value: object, active_container_ids: set[int] | None = None) -> object:
    """Recursively freeze JSON-compatible values at a public boundary."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if active_container_ids is None:
        active_container_ids = set()
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_container_ids:
            raise ValueError("JSON values must not be cyclic")
        active_container_ids.add(container_id)
        frozen: dict[str, object] = {}
        try:
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                frozen[key] = _freeze_json(item, active_container_ids)
            return MappingProxyType(frozen)
        finally:
            active_container_ids.remove(container_id)
    if isinstance(value, (list, tuple)):
        container_id = id(value)
        if container_id in active_container_ids:
            raise ValueError("JSON values must not be cyclic")
        active_container_ids.add(container_id)
        try:
            return tuple(_freeze_json(item, active_container_ids) for item in value)
        finally:
            active_container_ids.remove(container_id)
    raise ValueError("value is not JSON-compatible")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _require_json_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    frozen = _freeze_json(value)
    assert isinstance(frozen, Mapping)
    return frozen


@dataclass(frozen=True, slots=True)
class QualityRecipe:
    recipe_id: str
    kind: str
    family: str | None
    numerator: float | None
    baseline_constant: float | None
    fixed_value: float | None
    children: tuple["QualityRecipe", ...]
    child_weights: tuple[float, ...]
    transform_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "recipe_id", _require_text(self.recipe_id, "recipe_id")
        )
        if self.kind not in {"fixed", "ratio", "weighted_sum", "minimum"}:
            raise ValueError("unknown recipe kind")
        if self.family not in QUALITY_FAMILIES:
            raise ValueError("unknown recipe family")
        object.__setattr__(
            self,
            "transform_sha256",
            _require_sha256(self.transform_sha256, "transform_sha256"),
        )
        children = _require_sequence(self.children, "children")
        if not all(isinstance(child, QualityRecipe) for child in children):
            raise ValueError("recipe children must be QualityRecipe values")
        weights = tuple(
            _require_number(weight, "child weight", 0.0)
            for weight in _require_sequence(self.child_weights, "child_weights")
        )
        object.__setattr__(self, "children", children)
        object.__setattr__(self, "child_weights", weights)
        if self.kind == "fixed":
            if (
                self.family is not None
                or self.numerator is not None
                or self.baseline_constant is not None
                or children
                or weights
            ):
                raise ValueError("fixed recipe has incompatible fields")
            object.__setattr__(
                self,
                "fixed_value",
                _require_number(self.fixed_value, "fixed_value", 0.0, 1.0),
            )
        elif self.kind == "ratio":
            if (
                self.family not in _MULTIPLIER_FAMILIES
                or self.fixed_value is not None
                or children
                or weights
            ):
                raise ValueError("ratio recipe has incompatible fields")
            object.__setattr__(
                self,
                "numerator",
                _require_number(self.numerator, "numerator", 0.0),
            )
            object.__setattr__(
                self,
                "baseline_constant",
                _require_number(
                    self.baseline_constant,
                    "baseline_constant",
                    minimum=math.nextafter(0.0, 1.0),
                ),
            )
        elif self.kind == "weighted_sum":
            if (
                self.numerator is not None
                or self.baseline_constant is not None
                or self.fixed_value is not None
                or not children
                or len(children) != len(weights)
                or math.fsum(weights) <= 0.0
            ):
                raise ValueError("weighted_sum recipe has incompatible fields")
        else:
            if (
                self.numerator is not None
                or self.baseline_constant is not None
                or self.fixed_value is not None
                or not children
                or weights
            ):
                raise ValueError("minimum recipe has incompatible fields")

    def as_mapping(self) -> dict[str, object]:
        return {
            "recipe_id": self.recipe_id,
            "kind": self.kind,
            "family": self.family,
            "numerator": self.numerator,
            "baseline_constant": self.baseline_constant,
            "fixed_value": self.fixed_value,
            "children": [child.as_mapping() for child in self.children],
            "child_weights": list(self.child_weights),
            "transform_sha256": self.transform_sha256,
        }

    @classmethod
    def from_payload_mapping(cls, payload: Mapping[str, object]) -> "QualityRecipe":
        fields = frozenset(cls.__dataclass_fields__)
        values = _require_exact_mapping(payload, fields, "quality recipe payload")
        data = dict(values)
        data["children"] = tuple(
            cls.from_payload_mapping(child)
            for child in _require_sequence(data["children"], "children")
            if isinstance(child, Mapping)
        )
        if len(data["children"]) != len(
            _require_sequence(values["children"], "children")
        ):
            raise ValueError("recipe children must be objects")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class MetricPrimitive:
    normalized_score: float
    aggregation_weight: float
    quality_recipe: QualityRecipe

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "normalized_score",
            _require_number(self.normalized_score, "normalized_score", 0.0, 100.0),
        )
        object.__setattr__(
            self,
            "aggregation_weight",
            _require_number(self.aggregation_weight, "aggregation_weight", 0.0),
        )
        if not isinstance(self.quality_recipe, QualityRecipe):
            raise ValueError("quality_recipe must be a QualityRecipe")

    def as_mapping(self) -> dict[str, object]:
        return {
            "normalized_score": self.normalized_score,
            "aggregation_weight": self.aggregation_weight,
            "quality_recipe": self.quality_recipe.as_mapping(),
        }

    @classmethod
    def from_payload_mapping(cls, payload: Mapping[str, object]) -> "MetricPrimitive":
        values = _require_exact_mapping(
            payload, frozenset(cls.__dataclass_fields__), "metric primitive payload"
        )
        recipe = values["quality_recipe"]
        return cls(
            values["normalized_score"],
            values["aggregation_weight"],
            QualityRecipe.from_payload_mapping(
                recipe if isinstance(recipe, Mapping) else {}
            ),
        )


def _freeze_component_numbers(
    value: object, label: str
) -> Mapping[str, Mapping[str, float]]:
    sides = _require_exact_mapping(value, frozenset(SIDES), label)
    frozen: dict[str, Mapping[str, float]] = {}
    for side in SIDES:
        components = _require_exact_mapping(
            sides[side], frozenset(COMPONENTS), f"{label}.{side}"
        )
        frozen[side] = MappingProxyType(
            {
                component: _require_number(
                    components[component], f"{label}.{side}.{component}", 0.0, 100.0
                )
                for component in COMPONENTS
            }
        )
    return MappingProxyType(frozen)


def _freeze_component_digests(
    value: object, label: str
) -> Mapping[str, Mapping[str, str]]:
    sides = _require_exact_mapping(value, frozenset(SIDES), label)
    frozen: dict[str, Mapping[str, str]] = {}
    for side in SIDES:
        components = _require_exact_mapping(
            sides[side], frozenset(COMPONENTS), f"{label}.{side}"
        )
        frozen[side] = MappingProxyType(
            {
                component: _require_sha256(
                    components[component], f"{label}.{side}.{component}"
                )
                for component in COMPONENTS
            }
        )
    return MappingProxyType(frozen)


def _freeze_metric_primitives(
    value: object,
) -> Mapping[str, Mapping[str, tuple[MetricPrimitive, ...]]]:
    sides = _require_exact_mapping(value, frozenset(SIDES), "metric_primitives")
    frozen: dict[str, Mapping[str, tuple[MetricPrimitive, ...]]] = {}
    for side in SIDES:
        components = _require_exact_mapping(
            sides[side], frozenset(COMPONENTS), f"metric_primitives.{side}"
        )
        side_values: dict[str, tuple[MetricPrimitive, ...]] = {}
        for component in COMPONENTS:
            raw_primitives = components[component]
            if not isinstance(raw_primitives, (tuple, list)) or not raw_primitives:
                raise ValueError("each component needs metric primitives")
            primitives = tuple(raw_primitives)
            if not all(isinstance(item, MetricPrimitive) for item in primitives):
                raise ValueError(
                    "metric_primitives must contain MetricPrimitive values"
                )
            if not math.isclose(
                math.fsum(item.aggregation_weight for item in primitives),
                1.0,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                raise ValueError("metric primitive weights must sum to one")
            side_values[component] = primitives
        frozen[side] = MappingProxyType(side_values)
    return MappingProxyType(frozen)


_FEATURE_FIELDS = frozenset(
    {
        "game_pk",
        "official_date",
        "scheduled_start_utc",
        "target_year",
        "max_history_date",
        "source_gate_sha256",
        "source_manifest_sha256",
        "exclusion_policy_sha256",
        "component_scores",
        "component_qualities",
        "baseline_component_scores",
        "baseline_component_qualities",
        "metric_primitives",
        "component_inputs_sha256",
        "starter_edge_abs",
        "feature_row_sha256",
    }
)
_FEATURE_HASH_FIELDS = _FEATURE_FIELDS - {"feature_row_sha256"}


@dataclass(frozen=True, slots=True)
class SealedFeatureRow:
    game_pk: int
    official_date: str
    scheduled_start_utc: str
    target_year: int
    max_history_date: str
    source_gate_sha256: str
    source_manifest_sha256: str
    exclusion_policy_sha256: str
    component_scores: Mapping[str, Mapping[str, float]]
    component_qualities: Mapping[str, Mapping[str, float]]
    baseline_component_scores: Mapping[str, Mapping[str, float]]
    baseline_component_qualities: Mapping[str, Mapping[str, float]]
    metric_primitives: Mapping[str, Mapping[str, tuple[MetricPrimitive, ...]]]
    component_inputs_sha256: Mapping[str, Mapping[str, str]]
    starter_edge_abs: float
    feature_row_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "game_pk", _require_positive_int(self.game_pk, "game_pk")
        )
        object.__setattr__(
            self, "official_date", _require_date(self.official_date, "official_date")
        )
        object.__setattr__(
            self,
            "scheduled_start_utc",
            _require_utc(self.scheduled_start_utc, "scheduled_start_utc"),
        )
        object.__setattr__(
            self, "target_year", _require_year(self.target_year, "target_year")
        )
        object.__setattr__(
            self,
            "max_history_date",
            _require_date(self.max_history_date, "max_history_date"),
        )
        for field in (
            "source_gate_sha256",
            "source_manifest_sha256",
            "exclusion_policy_sha256",
            "feature_row_sha256",
        ):
            object.__setattr__(
                self, field, _require_sha256(getattr(self, field), field)
            )
        for field in (
            "component_scores",
            "component_qualities",
            "baseline_component_scores",
            "baseline_component_qualities",
        ):
            object.__setattr__(
                self, field, _freeze_component_numbers(getattr(self, field), field)
            )
        object.__setattr__(
            self, "metric_primitives", _freeze_metric_primitives(self.metric_primitives)
        )
        object.__setattr__(
            self,
            "component_inputs_sha256",
            _freeze_component_digests(
                self.component_inputs_sha256, "component_inputs_sha256"
            ),
        )
        object.__setattr__(
            self,
            "starter_edge_abs",
            _require_number(self.starter_edge_abs, "starter_edge_abs", 0.0),
        )
        self._validate_neutral_environment_defense_fallbacks()
        if self.feature_row_sha256 != compute_feature_row_sha256(
            sealed_feature_row_payload(self)
        ):
            raise ValueError(
                "feature_row_sha256 does not match the feature row payload"
            )

    def _validate_neutral_environment_defense_fallbacks(self) -> None:
        for side in SIDES:
            for component in ("environment", "defense"):
                primitives = self.metric_primitives[side][component]
                if not all(
                    primitive.quality_recipe.kind == "fixed"
                    and primitive.quality_recipe.fixed_value == 0.0
                    for primitive in primitives
                ):
                    continue
                for score_map, quality_map in (
                    (self.component_scores, self.component_qualities),
                    (self.baseline_component_scores, self.baseline_component_qualities),
                ):
                    if (
                        score_map[side][component] != 50.0
                        or quality_map[side][component] != 0.0
                    ):
                        raise ValueError(
                            "neutral environment and defense fallbacks must be 50/0"
                        )

    def as_mapping(self) -> dict[str, object]:
        return {
            "game_pk": self.game_pk,
            "official_date": self.official_date,
            "scheduled_start_utc": self.scheduled_start_utc,
            "target_year": self.target_year,
            "max_history_date": self.max_history_date,
            "source_gate_sha256": self.source_gate_sha256,
            "source_manifest_sha256": self.source_manifest_sha256,
            "exclusion_policy_sha256": self.exclusion_policy_sha256,
            "component_scores": _thaw_json(self.component_scores),
            "component_qualities": _thaw_json(self.component_qualities),
            "baseline_component_scores": _thaw_json(self.baseline_component_scores),
            "baseline_component_qualities": _thaw_json(
                self.baseline_component_qualities
            ),
            "metric_primitives": {
                side: {
                    component: [
                        item.as_mapping()
                        for item in self.metric_primitives[side][component]
                    ]
                    for component in COMPONENTS
                }
                for side in SIDES
            },
            "component_inputs_sha256": _thaw_json(self.component_inputs_sha256),
            "starter_edge_abs": self.starter_edge_abs,
            "feature_row_sha256": self.feature_row_sha256,
        }

    @classmethod
    def from_payload_mapping(cls, payload: Mapping[str, object]) -> "SealedFeatureRow":
        values = _require_exact_mapping(
            payload, _FEATURE_FIELDS, "sealed feature row payload"
        )
        data = dict(values)
        metric_values = _require_exact_mapping(
            data["metric_primitives"], frozenset(SIDES), "metric_primitives"
        )
        materialized: dict[str, dict[str, tuple[MetricPrimitive, ...]]] = {}
        for side in SIDES:
            components = _require_exact_mapping(
                metric_values[side],
                frozenset(COMPONENTS),
                f"metric_primitives.{side}",
            )
            materialized[side] = {}
            for component in COMPONENTS:
                primitives = _require_sequence(
                    components[component], f"metric_primitives.{side}.{component}"
                )
                materialized[side][component] = tuple(
                    MetricPrimitive.from_payload_mapping(item) for item in primitives
                )
        data["metric_primitives"] = materialized
        return cls(**data)


def sealed_feature_row_payload(
    row: SealedFeatureRow | Mapping[str, object],
) -> Mapping[str, object]:
    """Return the exact hash projection, never including its own digest."""
    payload: Mapping[str, object]
    if isinstance(row, SealedFeatureRow):
        payload = row.as_mapping()
    elif isinstance(row, Mapping):
        payload = row
    else:
        raise ValueError("feature row must be a SealedFeatureRow or mapping")
    _require_exact_mapping(payload, _FEATURE_FIELDS, "feature row payload")
    return _feature_hash_projection(payload)


def _feature_hash_projection(payload: Mapping[str, object]) -> dict[str, object]:
    projection = {field: _thaw_json(payload[field]) for field in _FEATURE_HASH_FIELDS}
    projection["metric_primitives"] = _metric_primitives_payload(
        projection["metric_primitives"]
    )
    return projection


def _metric_primitives_payload(
    value: object,
) -> dict[str, dict[str, list[dict[str, object]]]]:
    """Convert in-memory primitive objects to their persisted JSON form."""
    if not isinstance(value, Mapping):
        raise ValueError("metric_primitives must be an object")
    result: dict[str, dict[str, list[dict[str, object]]]] = {}
    for side, components in value.items():
        if not isinstance(side, str) or not isinstance(components, Mapping):
            raise ValueError("metric_primitives must be nested objects")
        result[side] = {}
        for component, primitives in components.items():
            if not isinstance(component, str) or not isinstance(
                primitives, (list, tuple)
            ):
                raise ValueError("metric_primitives must contain lists")
            result[side][component] = [
                item.as_mapping() if isinstance(item, MetricPrimitive) else item
                for item in primitives
            ]
    return result


def compute_feature_row_sha256(payload: Mapping[str, object]) -> str:
    _require_exact_mapping(payload, _FEATURE_HASH_FIELDS, "feature row hash payload")
    projection = _feature_hash_projection(payload)
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _require_descriptor(artifact_sha256: object, path: object) -> tuple[str, str]:
    return _require_sha256(artifact_sha256, "artifact_sha256"), _require_text(
        path, "path"
    )


@dataclass(frozen=True, slots=True)
class PredictionRow:
    model_id: str
    game_pk: int
    official_date: str
    target_year: int
    p_home: float
    picked_side: str
    pick_probability: float
    confidence: float
    starter_edge_abs: float
    scheduled_start_utc: str
    rank: int
    feature_row_sha256: str
    model_card_sha256: str

    def __post_init__(self) -> None:
        if self.model_id not in MODEL_IDS:
            raise ValueError("unknown model_id")
        object.__setattr__(
            self, "game_pk", _require_positive_int(self.game_pk, "game_pk")
        )
        object.__setattr__(
            self, "official_date", _require_date(self.official_date, "official_date")
        )
        object.__setattr__(
            self, "target_year", _require_year(self.target_year, "target_year")
        )
        object.__setattr__(
            self, "p_home", _require_number(self.p_home, "p_home", 0.0, 1.0)
        )
        if self.picked_side not in PICKED_SIDES:
            raise ValueError("unknown picked_side")
        object.__setattr__(
            self,
            "pick_probability",
            _require_number(self.pick_probability, "pick_probability", 0.0, 1.0),
        )
        object.__setattr__(
            self, "confidence", _require_number(self.confidence, "confidence", 0.0, 1.0)
        )
        object.__setattr__(
            self,
            "starter_edge_abs",
            _require_number(self.starter_edge_abs, "starter_edge_abs", 0.0),
        )
        object.__setattr__(
            self,
            "scheduled_start_utc",
            _require_utc(self.scheduled_start_utc, "scheduled_start_utc"),
        )
        if (
            isinstance(self.rank, bool)
            or not isinstance(self.rank, int)
            or (self.rank == 0) != (self.picked_side == "none")
            or self.rank < 0
        ):
            raise ValueError("rank must be zero iff picked_side is none")
        for field in ("feature_row_sha256", "model_card_sha256"):
            object.__setattr__(
                self, field, _require_sha256(getattr(self, field), field)
            )

    def as_mapping(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}

    @classmethod
    def from_payload_mapping(cls, payload: Mapping[str, object]) -> "PredictionRow":
        return cls(
            **dict(
                _require_exact_mapping(
                    payload,
                    frozenset(cls.__dataclass_fields__),
                    "prediction row payload",
                )
            )
        )


@dataclass(frozen=True, slots=True)
class PredictionSeal:
    ledger_id: str
    run_spec_sha256: str
    source_gate_sha256: str
    prediction_contract_sha256: str
    model_card_sha256: str
    population_sha256: str
    rows_sha256: str
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        if self.ledger_id not in LEDGER_IDS:
            raise ValueError("unknown ledger_id")
        for field in (
            "run_spec_sha256",
            "source_gate_sha256",
            "prediction_contract_sha256",
            "model_card_sha256",
            "population_sha256",
            "rows_sha256",
        ):
            object.__setattr__(
                self, field, _require_sha256(getattr(self, field), field)
            )
        artifact_sha256, path = _require_descriptor(self.artifact_sha256, self.path)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "path", path)

    def as_mapping(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
            if field not in {"artifact_sha256", "path"}
        }

    @classmethod
    def from_payload_mapping(
        cls, payload: Mapping[str, object], artifact_sha256: str, path: str
    ) -> "PredictionSeal":
        fields = frozenset(cls.__dataclass_fields__) - {"artifact_sha256", "path"}
        return cls(
            **dict(_require_exact_mapping(payload, fields, "prediction seal payload")),
            artifact_sha256=artifact_sha256,
            path=path,
        )


@dataclass(frozen=True, slots=True)
class FrozenModelCard:
    model_id: str
    state: str
    fold_id: str
    run_spec_sha256: str
    source_gate_sha256: str
    baseline_replay_sha256: str
    parent_card_sha256: str | None
    feature_seal_sha256: str
    prediction_contract_sha256: str
    prediction_seal_sha256: str
    evaluation_sha256: str
    parameters: Mapping[str, object]
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        if (
            self.model_id not in FROZEN_CARD_MODEL_IDS
            or self.state not in FROZEN_CARD_STATES
            or self.fold_id != "H23"
        ):
            raise ValueError(
                "frozen card model, state, and fold must be H23 compatible"
            )
        for field in (
            "run_spec_sha256",
            "source_gate_sha256",
            "baseline_replay_sha256",
            "feature_seal_sha256",
            "prediction_contract_sha256",
            "prediction_seal_sha256",
            "evaluation_sha256",
        ):
            object.__setattr__(
                self, field, _require_sha256(getattr(self, field), field)
            )
        object.__setattr__(
            self,
            "parent_card_sha256",
            _require_optional_sha256(self.parent_card_sha256, "parent_card_sha256"),
        )
        object.__setattr__(
            self, "parameters", _require_json_mapping(self.parameters, "parameters")
        )
        artifact_sha256, path = _require_descriptor(self.artifact_sha256, self.path)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "path", path)

    def as_mapping(self) -> dict[str, object]:
        return {
            field: (
                _thaw_json(self.parameters)
                if field == "parameters"
                else getattr(self, field)
            )
            for field in self.__dataclass_fields__
            if field not in {"artifact_sha256", "path"}
        }

    @classmethod
    def from_payload_mapping(
        cls, payload: Mapping[str, object], artifact_sha256: str, path: str
    ) -> "FrozenModelCard":
        fields = frozenset(cls.__dataclass_fields__) - {"artifact_sha256", "path"}
        return cls(
            **dict(
                _require_exact_mapping(payload, fields, "frozen model card payload")
            ),
            artifact_sha256=artifact_sha256,
            path=path,
        )
