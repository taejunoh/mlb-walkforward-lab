"""Authenticate the frozen v5.1 card used by experimental live Top 3."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Real
from pathlib import Path
from types import MappingProxyType

from .scoring import WEIGHTS as SCORING_WEIGHTS
from .scoring_prep import validate_normalization


EXPECTED_MODEL_CARD_SHA256 = (
    "9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759"
)
_SLOT_KEYS = frozenset(str(slot) for slot in range(1, 10))


@dataclass(frozen=True)
class FrozenLiveCard:
    model_version: str
    file_sha256: str
    normalization: Mapping[str, object]
    slot_weights: Mapping[int, float]
    component_weights: Mapping[str, float]
    alpha: float
    beta: float


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"frozen {label} differs")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"frozen {label} differs")
    return result


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _normalization(value: object) -> Mapping[str, object]:
    validate_normalization(value)
    if not isinstance(value, Mapping):
        raise ValueError("frozen normalization differs")
    for metrics in value.values():
        if not isinstance(metrics, Mapping):
            raise ValueError("frozen normalization differs")
        for constants in metrics.values():
            if not isinstance(constants, Mapping):
                raise ValueError("frozen normalization differs")
            _finite(constants["mean"], "normalization mean")
            sd = _finite(constants["sd"], "normalization sd")
            direction = constants["direction"]
            n = constants["n"]
            if sd <= 0.0:
                raise ValueError("frozen normalization sd differs")
            if type(direction) is not int or direction not in {-1, 1}:
                raise ValueError("frozen normalization direction differs")
            if type(n) is not int or n <= 0:
                raise ValueError("frozen normalization n differs")
    frozen = _deep_freeze(value)
    if not isinstance(frozen, Mapping):
        raise AssertionError("frozen normalization must be a mapping")
    return frozen


def _slot_weights(value: object) -> Mapping[int, float]:
    if not isinstance(value, Mapping) or set(value) != _SLOT_KEYS:
        raise ValueError("frozen slot weights differ")
    weights = {int(key): _finite(weight, "slot weights") for key, weight in value.items()}
    if math.fsum(weights.values()) != 1.0:
        raise ValueError("frozen slot weights differ")
    return MappingProxyType(weights)


def _component_weights(value: object) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(SCORING_WEIGHTS):
        raise ValueError("frozen component weights differ")
    weights = {key: _finite(weight, "component weights") for key, weight in value.items()}
    if weights != dict(SCORING_WEIGHTS):
        raise ValueError("frozen component weights differ")
    return MappingProxyType(weights)


def _calibration(value: object) -> tuple[float, float]:
    if not isinstance(value, Mapping) or set(value) != {"alpha", "beta"}:
        raise ValueError("frozen calibration differs")
    alpha = _finite(value["alpha"], "calibration")
    beta = _finite(value["beta"], "calibration")
    if beta <= 0.0:
        raise ValueError("frozen calibration differs")
    return alpha, beta


def load_frozen_live_card_bytes(raw: bytes) -> FrozenLiveCard:
    """Authenticate and parse the exact supplied v5.1 model-card bytes."""
    if type(raw) is not bytes:
        raise ValueError("frozen model card bytes differ")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_MODEL_CARD_SHA256:
        raise ValueError("frozen model card digest differs")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen model card JSON differs") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("frozen model card differs")
    if payload.get("model_version") != "v5.1-BT":
        raise ValueError("frozen model card version differs")
    normalization = _normalization(payload.get("normalization"))
    alpha, beta = _calibration(payload.get("calibration"))
    return FrozenLiveCard(
        model_version="v5.1-BT",
        file_sha256=digest,
        normalization=normalization,
        slot_weights=_slot_weights(payload.get("slot_weights")),
        component_weights=_component_weights(payload.get("component_weights")),
        alpha=alpha,
        beta=beta,
    )


def load_frozen_live_card(path: str | Path) -> FrozenLiveCard:
    """Load the one admitted v5.1 card without applying its historical windows."""
    return load_frozen_live_card_bytes(Path(path).read_bytes())
