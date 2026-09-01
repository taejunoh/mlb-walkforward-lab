"""Read-only admission of source-derived, pre-target calibration authority."""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from mlb_backtest.normalize import fit_logistic

from .canonical import canonical_json_bytes, read_regular_bytes
from .contracts import ProbabilityCalibrationCard, _require_sha256
from .run_spec import ModelImprovementRunSpec, _repository_root, run_spec_sha256


_SCHEMA = "MODEL_IMPROVEMENT_PROBABILITY_CALIBRATION_CARD_V1"
_CARD_FIELDS = frozenset({"schema"}) | (
    frozenset(ProbabilityCalibrationCard.__dataclass_fields__) - {"artifact_sha256", "path"}
)
_ROW_FIELDS = frozenset({
    "game_pk", "official_date", "target_year", "edge", "home_win",
    "feature_provenance_sha256", "game_evidence_sha256",
})


class CalibrationAuthorityBlockedError(RuntimeError):
    """The immutable prior-target authority has not been published yet."""

    code = "BLOCKED_CALIBRATION_AUTHORITY_MISSING"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CalibrationPaths:
    rows: Path
    rows_sidecar: Path
    card: Path
    card_sidecar: Path


def _identity(
    spec: ModelImprovementRunSpec, model_id: str, lane_id: str, fold_id: str,
    candidate_id: str | None,
) -> tuple[str, str, str, str | None]:
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("calibration requires ModelImprovementRunSpec")
    expected = {
        "v5.1": ("baseline", None), "A": ("A", None),
        "A+B": ("B", "required"), "A+B+C": ("C", None),
    }.get(model_id)
    if expected is None or lane_id != expected[0] or fold_id not in spec.probability_calibration_policy.fold_keys:
        raise ValueError("probability calibration identity differs")
    if expected[1] == "required":
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in {".", ".."}:
            raise ValueError("probability calibration candidate differs")
        if not all(part not in candidate_id for part in ("/", "\\")):
            raise ValueError("probability calibration candidate differs")
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate_id):
            raise ValueError("probability calibration candidate differs")
    elif candidate_id is not None:
        raise ValueError("probability calibration candidate differs")
    return model_id, lane_id, fold_id, candidate_id


def calibration_paths(
    spec: ModelImprovementRunSpec, model_id: str, lane_id: str, fold_id: str,
    candidate_id: str | None,
) -> CalibrationPaths:
    """Derive the only admitted calibration card and rows paths."""
    model_id, lane_id, fold_id, candidate_id = _identity(
        spec, model_id, lane_id, fold_id, candidate_id
    )
    policy = spec.probability_calibration_policy
    if model_id == "v5.1":
        values = {"fold_id": fold_id}
        rows_relative = policy.baseline_rows_relative_template.format(**values)
        card_relative = policy.baseline_card_relative_template.format(**values)
    else:
        values = {
            "lane_id": lane_id,
            "candidate_segment": "_none" if candidate_id is None else candidate_id,
            "fold_id": fold_id,
        }
        rows_relative = policy.candidate_rows_relative_template.format(**values)
        card_relative = policy.candidate_card_relative_template.format(**values)
    rows = spec.output_path("calibration", rows_relative)
    card = spec.output_path("calibration", card_relative)
    return CalibrationPaths(rows, rows.with_suffix(rows.suffix + ".sha256"), card, card.with_suffix(card.suffix + ".sha256"))


def _missing(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise ValueError("calibration authority path is unsafe") from exc
    if not stat.S_ISREG(value.st_mode):
        raise ValueError("calibration authority path is unsafe")
    return False


def _canonical_absolute(path: Path | str, label: str) -> Path:
    raw = os.fspath(path)
    candidate = Path(raw)
    if not isinstance(raw, str) or not candidate.is_absolute() or raw != str(candidate) or raw != os.path.normpath(raw):
        raise ValueError(f"{label} path differs")
    return candidate


def _sidecar(path: Path, digest: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if raw != canonical_json_bytes(payload) or payload != {"artifact_sha256": digest}:
        raise ValueError(f"{label} differs")


def _rows(raw: bytes, card: ProbabilityCalibrationCard, spec: ModelImprovementRunSpec) -> list[Mapping[str, object]]:
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("calibration rows differ")
    rows: list[Mapping[str, object]] = []
    previous = -1
    pool = spec.probability_calibration_policy.pools[card.calibration_key]
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("calibration row is not JSON") from exc
        if not isinstance(row, Mapping) or set(row) != _ROW_FIELDS or canonical_json_bytes(row).rstrip(b"\n") != line:
            raise ValueError("calibration row differs")
        game_pk = row["game_pk"]
        if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= previous:
            raise ValueError("calibration rows are not strictly ordered")
        previous = game_pk
        if row["target_year"] != pool.target_year or not isinstance(row["official_date"], str):
            raise ValueError("calibration row target differs")
        if not (pool.training_start <= row["official_date"] <= pool.training_end):
            raise ValueError("calibration row is outside its prior-target pool")
        if isinstance(row["edge"], bool) or not isinstance(row["edge"], (int, float)) or not math.isfinite(float(row["edge"])):
            raise ValueError("calibration edge differs")
        if row["home_win"] not in (0, 1) or isinstance(row["home_win"], bool):
            raise ValueError("calibration outcome differs")
        _require_sha256(row["feature_provenance_sha256"], "feature_provenance_sha256")
        _require_sha256(row["game_evidence_sha256"], "game_evidence_sha256")
        rows.append(row)
    if len(rows) < spec.probability_calibration_policy.fit_spec.min_rows:
        raise ValueError("calibration rows are below minimum")
    if {row["home_win"] for row in rows} != set(spec.probability_calibration_policy.fit_spec.required_classes):
        raise ValueError("calibration rows require both classes")
    if sha256(canonical_json_bytes([row["game_pk"] for row in rows])).hexdigest() != card.calibration_population_sha256:
        raise ValueError("calibration population differs")
    return rows


def _load_once(
    spec: ModelImprovementRunSpec, model_id: str, lane_id: str, fold_id: str,
    candidate_id: str | None,
) -> ProbabilityCalibrationCard:
    paths = calibration_paths(spec, model_id, lane_id, fold_id, candidate_id)
    if any(_missing(path) for path in (paths.rows, paths.rows_sidecar, paths.card, paths.card_sidecar)):
        raise CalibrationAuthorityBlockedError()
    card_raw = read_regular_bytes(paths.card, "probability calibration card")
    digest = sha256(card_raw).hexdigest()
    _sidecar(paths.card_sidecar, digest, "probability calibration card sidecar")
    try:
        persisted = json.loads(card_raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("probability calibration card is not JSON") from exc
    if not isinstance(persisted, Mapping) or card_raw != canonical_json_bytes(persisted):
        raise ValueError("probability calibration card is not canonical JSON")
    if set(persisted) != _CARD_FIELDS or persisted["schema"] != _SCHEMA:
        raise ValueError("probability calibration card schema differs")
    payload = {key: persisted[key] for key in persisted if key != "schema"}
    card = ProbabilityCalibrationCard.from_payload_mapping(payload, digest, str(paths.card))
    if (card.model_id, card.lane_id, card.fold_id, card.candidate_id) != (model_id, lane_id, fold_id, candidate_id):
        raise ValueError("probability calibration card identity differs")
    policy = spec.probability_calibration_policy
    pool = policy.pools[policy.fold_keys[fold_id]]
    if (card.calibration_key, card.target_year, card.training_start, card.training_end) != (policy.fold_keys[fold_id], pool.target_year, pool.training_start, pool.training_end):
        raise ValueError("probability calibration card pool differs")
    if card.run_spec_sha256 != run_spec_sha256(spec) or card.prediction_contract_sha256 != spec.prediction_contract_sha256:
        raise ValueError("probability calibration RunSpec binding differs")
    if canonical_json_bytes(card.as_mapping()["fit_spec"]) != canonical_json_bytes(policy.fit_spec.as_mapping()):
        raise ValueError("probability calibration fit spec differs")
    if str(_canonical_absolute(card.calibration_rows_path, "calibration rows")) != str(paths.rows):
        raise ValueError("calibration rows path differs")
    expected_weight = (
        _repository_root() / spec.baseline.card.path
        if model_id == "v5.1" else _canonical_absolute(card.weight_authority_path, "weight authority")
    )
    if str(_canonical_absolute(card.weight_authority_path, "weight authority")) != str(expected_weight):
        raise ValueError("weight authority path differs")
    if _missing(expected_weight):
        raise CalibrationAuthorityBlockedError()
    if sha256(read_regular_bytes(expected_weight, "weight authority")).hexdigest() != card.weight_authority_sha256:
        raise ValueError("weight authority digest differs")
    evidence_path = _canonical_absolute(card.game_evidence_inventory_path, "game evidence inventory")
    if _missing(evidence_path):
        raise CalibrationAuthorityBlockedError()
    if sha256(read_regular_bytes(evidence_path, "game evidence inventory")).hexdigest() != card.game_evidence_inventory_sha256:
        raise ValueError("game evidence inventory digest differs")
    source_gate_path = spec.output_path("source_gate", "source_gate_receipt.json")
    if _missing(source_gate_path):
        raise CalibrationAuthorityBlockedError()
    if sha256(read_regular_bytes(source_gate_path, "source gate receipt")).hexdigest() != card.source_gate_sha256:
        raise ValueError("source gate digest differs")
    rows_raw = read_regular_bytes(paths.rows, "calibration rows")
    if sha256(rows_raw).hexdigest() != card.calibration_rows_sha256:
        raise ValueError("calibration rows digest differs")
    _sidecar(paths.rows_sidecar, card.calibration_rows_sha256, "calibration rows sidecar")
    rows = _rows(rows_raw, card, spec)
    fit = policy.fit_spec
    alpha, beta = fit_logistic(
        (float(row["edge"]) / fit.edge_divisor for row in rows),
        (int(row["home_win"]) for row in rows), iterations=fit.iterations,
        l2=fit.l2, tolerance=fit.tolerance,
    )
    if not (math.isfinite(alpha) and math.isfinite(beta) and beta > 0):
        raise ValueError("probability calibration fit differs")
    if canonical_json_bytes({"alpha": card.alpha, "beta": card.beta}) != canonical_json_bytes({"alpha": alpha, "beta": beta}):
        raise ValueError("probability calibration coefficients differ")
    return card


def load_probability_calibration_card(
    spec: ModelImprovementRunSpec, model_id: str, lane_id: str, fold_id: str,
    candidate_id: str | None,
) -> ProbabilityCalibrationCard:
    """Securely reread an existing calibration authority twice before return."""
    first = _load_once(spec, model_id, lane_id, fold_id, candidate_id)
    second = _load_once(spec, model_id, lane_id, fold_id, candidate_id)
    if first != second:
        raise ValueError("probability calibration authority changed while reading")
    return second


def require_probability_calibration_card(
    spec: ModelImprovementRunSpec, model_id: str, lane_id: str, fold_id: str,
    candidate_id: str | None,
) -> ProbabilityCalibrationCard:
    """Read-only prerequisite gate used before Task8 V2 opens any output."""
    return load_probability_calibration_card(spec, model_id, lane_id, fold_id, candidate_id)


__all__ = [
    "CalibrationAuthorityBlockedError", "CalibrationPaths", "ProbabilityCalibrationCard",
    "calibration_paths", "load_probability_calibration_card",
    "require_probability_calibration_card",
]
