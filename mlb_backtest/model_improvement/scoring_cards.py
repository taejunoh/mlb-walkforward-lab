"""Fail-closed pre-prediction scoring-card persistence and weight resolution."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_json
from .contracts import (
    MODEL_IDS,
    ScoringModelCard,
    _require_component_weights_pp,
    _require_exact_mapping,
    _require_sha256,
)
from .run_spec import ModelImprovementRunSpec, _repository_root, run_spec_sha256


_SCHEMA = "MODEL_IMPROVEMENT_SCORING_MODEL_CARD_V1"
_PAYLOAD_FIELDS = frozenset(
    {
        "model_id",
        "lane_id",
        "fold_id",
        "candidate_id",
        "run_spec_sha256",
        "source_gate_sha256",
        "baseline_replay_sha256",
        "parent_card_sha256",
        "feature_seal_sha256",
        "prediction_contract_sha256",
        "calibration_card_sha256",
        "candidate_payload_sha256",
        "parameters",
    }
)
_PERSISTED_FIELDS = frozenset({"schema"}) | _PAYLOAD_FIELDS


@dataclass(frozen=True, slots=True)
class PredictionWeightAuthority:
    """The sole resolved component-weight authority for one prediction path."""

    model_id: str
    path: str
    sha256: str
    component_weights_pp: Mapping[str, int]

    def __post_init__(self) -> None:
        if self.model_id not in MODEL_IDS:
            raise ValueError("unknown prediction weight model")
        object.__setattr__(self, "sha256", _require_sha256(self.sha256, "weight authority sha256"))
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("weight authority path must be nonempty")
        object.__setattr__(
            self,
            "component_weights_pp",
            _require_component_weights_pp(
                self.component_weights_pp, "weight authority component_weights_pp"
            ),
        )


def _lexical_absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (TypeError, ValueError) as exc:
        raise ValueError("scoring card path differs") from exc


def _payload_card(payload: Mapping[str, object]) -> ScoringModelCard:
    fields = _require_exact_mapping(payload, _PAYLOAD_FIELDS, "scoring model card payload")
    return ScoringModelCard.from_payload_mapping(fields, "0" * 64, "scoring-model-card")


def scoring_model_card_path(
    spec: ModelImprovementRunSpec, payload: Mapping[str, object]
) -> Path:
    """Derive the one allowed primary path from the closed card identity."""
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("scoring card requires ModelImprovementRunSpec")
    card = _payload_card(payload)
    if card.lane_id == "A":
        return spec.output_path("lane_a", f"{card.fold_id}/scoring_model_card.json")
    if card.lane_id == "B":
        assert card.candidate_id is not None
        return spec.output_path(
            "lane_b",
            f"{card.fold_id}/candidates/{card.candidate_id}/scoring_model_card.json",
        )
    if card.lane_id == "C":
        return spec.output_path("lane_c", f"{card.fold_id}/scoring_model_card.json")
    if card.lane_id == "H24":
        return spec.output_path(
            "h24_predictions", f"{card.model_id}/scoring_model_card.json"
        )
    raise ValueError("scoring card lane differs")


def _sidecar_payload(digest: str) -> dict[str, str]:
    return {"artifact_sha256": _require_sha256(digest, "artifact_sha256")}


def _existing_regular_bytes(path: Path, label: str) -> bytes | None:
    try:
        result = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"unsafe {label}") from exc
    if not stat.S_ISREG(result.st_mode):
        raise ValueError(f"unsafe {label}")
    return read_regular_bytes(path, label)


def _load_sidecar(path: Path, digest: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if raw != canonical_json_bytes(value) or value != _sidecar_payload(digest):
        raise ValueError(f"{label} differs")


def _preflight_pair(primary: Path, primary_bytes: bytes, sidecar: Path) -> None:
    existing_primary = _existing_regular_bytes(primary, "scoring card")
    existing_sidecar = _existing_regular_bytes(sidecar, "scoring card sidecar")
    digest = sha256(primary_bytes).hexdigest()
    if existing_primary is not None:
        if existing_primary != primary_bytes:
            raise ValueError("immutable scoring card differs")
        if existing_sidecar is None:
            raise ValueError("scoring card primary requires sidecar")
        _load_sidecar(sidecar, digest, "scoring card sidecar")
    elif existing_sidecar is not None:
        _load_sidecar(sidecar, digest, "scoring card sidecar")


def _validate_bindings(spec: ModelImprovementRunSpec, card: ScoringModelCard) -> None:
    if card.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("scoring card RunSpec binding differs")
    if card.prediction_contract_sha256 != spec.prediction_contract_sha256:
        raise ValueError("scoring card prediction contract binding differs")


def seal_scoring_model_card(
    spec: ModelImprovementRunSpec, payload: Mapping[str, object]
) -> ScoringModelCard:
    """Seal one candidate's immutable pre-prediction scoring authority."""
    card = _payload_card(payload)
    _validate_bindings(spec, card)
    path = scoring_model_card_path(spec, payload)
    persisted = {"schema": _SCHEMA} | card.as_mapping()
    primary_bytes = canonical_json_bytes(persisted)
    digest = sha256(primary_bytes).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    _preflight_pair(path, primary_bytes, sidecar)
    # A sidecar is an explicitly reusable prerequisite.  The primary is the
    # commit point, so a failed primary write leaves only a safe orphan sidecar.
    write_immutable_json(sidecar, _sidecar_payload(digest))
    write_immutable_json(path, persisted)
    return load_scoring_model_card(spec, path, digest)


def load_scoring_model_card(
    spec: ModelImprovementRunSpec, path: Path, expected_sha256: str
) -> ScoringModelCard:
    """Securely load one canonical, sidecar-bound scoring-card descriptor."""
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("scoring card requires ModelImprovementRunSpec")
    expected = _require_sha256(expected_sha256, "expected scoring card sha256")
    raw = read_regular_bytes(path, "scoring card")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("scoring card digest differs")
    try:
        persisted = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("scoring card is not JSON") from exc
    if not isinstance(persisted, Mapping) or raw != canonical_json_bytes(persisted):
        raise ValueError("scoring card is not canonical JSON")
    values = _require_exact_mapping(persisted, _PERSISTED_FIELDS, "scoring model card")
    if values["schema"] != _SCHEMA:
        raise ValueError("scoring card schema differs")
    payload = {key: values[key] for key in _PAYLOAD_FIELDS}
    card = ScoringModelCard.from_payload_mapping(payload, actual, str(path))
    _validate_bindings(spec, card)
    expected_path = scoring_model_card_path(spec, payload)
    if _lexical_absolute(path) != _lexical_absolute(expected_path):
        raise ValueError("scoring card derived path differs")
    _load_sidecar(
        expected_path.with_suffix(expected_path.suffix + ".sha256"),
        actual,
        "scoring card sidecar",
    )
    return card


def _load_baseline_weight_authority(
    spec: ModelImprovementRunSpec,
) -> PredictionWeightAuthority:
    path = _repository_root() / spec.baseline.card.path
    raw = read_regular_bytes(path, "baseline scoring card")
    digest = sha256(raw).hexdigest()
    if digest != spec.baseline.card.sha256:
        raise ValueError("baseline scoring card digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("baseline scoring card is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("baseline scoring card is not canonical JSON")
    baseline = _require_exact_mapping(
        payload, frozenset({"component_weights_pp"}), "baseline scoring card"
    )
    weights = _require_component_weights_pp(
        baseline["component_weights_pp"], "baseline scoring card component_weights_pp"
    )
    if dict(weights) != dict(spec.baseline.card.component_weights_pp):
        raise ValueError("baseline scoring card weights differ")
    return PredictionWeightAuthority("v5.1", spec.baseline.card.path, digest, weights)


def resolve_prediction_weight_authority(
    spec: ModelImprovementRunSpec,
    model_id: str,
    scoring_card: ScoringModelCard | None,
) -> PredictionWeightAuthority:
    """Resolve exactly one direct baseline or securely reread candidate authority."""
    if model_id == "v5.1":
        if scoring_card is not None:
            raise ValueError("v5.1 does not accept a scoring card")
        return _load_baseline_weight_authority(spec)
    if model_id not in {"A", "A+B", "A+B+C"} or not isinstance(
        scoring_card, ScoringModelCard
    ):
        raise ValueError("nonbaseline model requires a scoring card")
    loaded = load_scoring_model_card(
        spec, Path(scoring_card.path), scoring_card.artifact_sha256
    )
    if (
        loaded.as_mapping() != scoring_card.as_mapping()
        or loaded.artifact_sha256 != scoring_card.artifact_sha256
        or _lexical_absolute(Path(loaded.path))
        != _lexical_absolute(Path(scoring_card.path))
    ):
        raise ValueError("scoring card descriptor reload differs")
    if loaded.model_id != model_id:
        raise ValueError("scoring card model differs")
    return PredictionWeightAuthority(
        loaded.model_id,
        loaded.path,
        loaded.artifact_sha256,
        loaded.parameters["component_weights_pp"],
    )
