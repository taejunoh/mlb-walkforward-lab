"""Pure V2 OOF identity and path contracts.

Publication and loading are intentionally introduced by the later Task8A
sealing stage.  This module admits no V1 artifact as executable input.
"""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from mlb_backtest.scoring import home_probability

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_bytes
from .contracts import (
    PredictionRowV2,
    PredictionSealV2,
    ProbabilityCalibrationCardV2,
    _require_exact_mapping,
    _require_sha256,
)
from .probability_calibration import require_probability_calibration_card_v2
from .run_spec import ModelImprovementRunSpec, task8_target_year
from .run_spec import run_spec_sha256
from .scoring_cards import resolve_prediction_weight_authority
from .sealed_features import FeatureSeal, load_feature_seal, load_sealed_feature_rows
from .source_gate import SourceGateReceipt, load_source_gate_receipt

if TYPE_CHECKING:
    from .baseline_replay_v2 import BaselineReplayReceiptV2


_SCHEMA = "MODEL_IMPROVEMENT_PREDICTION_SEAL_V2"
_SEAL_FIELDS = frozenset({
    "schema", "ledger_id", "fold_id", "model_id", "run_spec_sha256",
    "source_gate_sha256", "feature_seal_sha256", "prediction_contract_sha256",
    "scoring_model_card_sha256", "calibration_card_sha256",
    "population_sha256", "frozen_model_card_sha256", "rows_sha256",
})


@dataclass(frozen=True, slots=True)
class PredictionPaths:
    """The four immutable V2 artifacts for one baseline Task8 identity."""

    rows: Path
    rows_sidecar: Path
    seal: Path
    seal_sidecar: Path


def _baseline_fold_from_ledger_id(ledger_id: object) -> str:
    if not isinstance(ledger_id, str):
        raise ValueError("V2 prediction ledger_id must be a string")
    parts = ledger_id.split("/")
    if len(parts) != 3 or parts[:2] != ["baseline", "v5.1"]:
        raise ValueError("V2 prediction ledger_id differs")
    fold_id = parts[2]
    try:
        task8_target_year(fold_id)
    except ValueError as exc:
        raise ValueError("V2 prediction ledger_id differs") from exc
    if ledger_id != f"baseline/v5.1/{fold_id}":
        raise ValueError("V2 prediction ledger_id differs")
    return fold_id


def prediction_paths_v2(
    spec: ModelImprovementRunSpec, ledger_id: str
) -> PredictionPaths:
    """Derive the only V2 artifact locations without observing the filesystem."""
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("V2 prediction paths require ModelImprovementRunSpec")
    fold_id = _baseline_fold_from_ledger_id(ledger_id)
    base = f"baseline/v5.1/{fold_id}/v2"
    rows = spec.output_path("oof", f"{base}/prediction_rows.jsonl")
    seal = spec.output_path("oof", f"{base}/prediction_seal.json")
    return PredictionPaths(
        rows=rows,
        rows_sidecar=spec.output_path("oof", f"{base}/prediction_rows.jsonl.sha256"),
        seal=seal,
        seal_sidecar=spec.output_path("oof", f"{base}/prediction_seal.json.sha256"),
    )


def _canonical_absolute(path: Path | str, label: str) -> Path:
    try:
        raw = os.fspath(path)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} path differs") from exc
    candidate = Path(raw) if isinstance(raw, str) else None
    if (
        not isinstance(raw, str) or not raw or candidate is None
        or not candidate.is_absolute() or "//" in raw
        or raw != str(candidate) or raw != os.path.normpath(raw)
    ):
        raise ValueError(f"{label} path differs")
    return candidate


def _same_descriptor_path(provided: Path | str, expected: Path, label: str) -> Path:
    candidate = _canonical_absolute(provided, label)
    if str(candidate) != str(expected):
        raise ValueError(f"{label} path differs")
    return candidate


def _sidecar_payload(digest: str) -> dict[str, str]:
    return {"artifact_sha256": _require_sha256(digest, "artifact_sha256")}


def _load_sidecar(path: Path, digest: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if raw != canonical_json_bytes(payload) or payload != _sidecar_payload(digest):
        raise ValueError(f"{label} differs")


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


def _preflight(primary: Path, contents: bytes, sidecar: Path, label: str) -> None:
    old_primary = _existing_regular_bytes(primary, label)
    old_sidecar = _existing_regular_bytes(sidecar, f"{label} sidecar")
    digest = sha256(contents).hexdigest()
    if old_primary is not None:
        if old_primary != contents:
            raise ValueError(f"immutable {label} differs")
        if old_sidecar is None:
            raise ValueError(f"{label} primary requires sidecar")
        _load_sidecar(sidecar, digest, f"{label} sidecar")
    elif old_sidecar is not None:
        _load_sidecar(sidecar, digest, f"{label} sidecar")


def _authenticate_gate(spec: ModelImprovementRunSpec, supplied: SourceGateReceipt) -> SourceGateReceipt:
    if not isinstance(supplied, SourceGateReceipt):
        raise ValueError("V2 prediction seal requires source gate receipt")
    expected_path = spec.output_path("source_gate", "source_gate_receipt.json")
    _same_descriptor_path(supplied.path, expected_path, "source gate")
    loaded = load_source_gate_receipt(expected_path, supplied.artifact_sha256)
    if (
        loaded.as_mapping() != supplied.as_mapping()
        or loaded.artifact_sha256 != supplied.artifact_sha256
        or loaded.path != supplied.path
        or loaded.run_spec_sha256 != run_spec_sha256(spec)
        or loaded.inventory_sha256 != spec.source_authority.inventory.sha256
    ):
        raise ValueError("V2 source gate binding differs")
    return loaded


def _authenticate_feature(
    spec: ModelImprovementRunSpec, gate: SourceGateReceipt, supplied: FeatureSeal, fold_id: str
) -> tuple[FeatureSeal, tuple[object, ...]]:
    if not isinstance(supplied, FeatureSeal) or supplied.feature_set_id != "common":
        raise ValueError("V2 Task8A requires a Common FeatureSeal")
    if supplied.fold_id != fold_id:
        raise ValueError("V2 feature seal fold differs")
    expected_path = spec.output_path("features", f"common/{fold_id}/feature_seal.json")
    _same_descriptor_path(supplied.path, expected_path, "feature seal")
    loaded = load_feature_seal(expected_path, supplied.artifact_sha256)
    if (
        loaded.as_mapping() != supplied.as_mapping()
        or loaded.artifact_sha256 != supplied.artifact_sha256
        or loaded.path != supplied.path
        or loaded.run_spec_sha256 != run_spec_sha256(spec)
        or loaded.source_gate_sha256 != gate.artifact_sha256
    ):
        raise ValueError("V2 feature seal binding differs")
    return loaded, load_sealed_feature_rows(spec, gate, (task8_target_year(fold_id),), loaded)


def _authenticate_calibration(
    spec: ModelImprovementRunSpec, gate: SourceGateReceipt, fold_id: str
) -> tuple[ProbabilityCalibrationCardV2, str]:
    card = require_probability_calibration_card_v2(spec, fold_id)
    if not isinstance(card, ProbabilityCalibrationCardV2):
        raise ValueError("V2 prediction calibration card differs")
    pool = spec.probability_calibration_policy.pools[
        spec.probability_calibration_policy.fold_keys[fold_id]
    ]
    weights = resolve_prediction_weight_authority(spec, "v5.1", None)
    if (
        card.model_id != "v5.1" or card.lane_id != "baseline" or card.fold_id != fold_id
        or card.calibration_key != spec.probability_calibration_policy.fold_keys[fold_id]
        or (card.target_year, card.training_start, card.training_end)
        != (pool.target_year, pool.training_start, pool.training_end)
        or card.run_spec_sha256 != run_spec_sha256(spec)
        or card.source_gate_sha256 != gate.artifact_sha256
        or card.prediction_contract_sha256 != spec.prediction_contract_sha256
        or card.weight_authority_sha256 != weights.sha256
        or not math.isfinite(card.alpha) or not math.isfinite(card.beta) or card.beta <= 0
    ):
        raise ValueError("V2 prediction calibration binding differs")
    return card, weights.sha256


def _row_key(row: PredictionRowV2) -> tuple[str, str, int]:
    return row.official_date, row.scheduled_start_utc, row.game_pk


def _population_digest(rows: Sequence[PredictionRowV2]) -> str:
    return sha256(canonical_json_bytes([
        {"game_pk": row.game_pk, "official_date": row.official_date, "target_year": row.target_year}
        for row in rows
    ])).hexdigest()


def _rows_bytes(rows: Sequence[PredictionRowV2]) -> bytes:
    return b"".join(canonical_json_bytes(row.as_mapping()) for row in rows)


def _feature_edge(feature: object, weights: Mapping[str, int]) -> float:
    return math.fsum(
        weights[component] / 100.0
        * (feature.component_scores["home"][component] - feature.component_scores["away"][component])
        for component in weights
    )


def _validate_rows(
    spec: ModelImprovementRunSpec, fold_id: str, feature_rows: Sequence[object],
    rows: Sequence[PredictionRowV2], weight_sha256: str, calibration: ProbabilityCalibrationCardV2,
) -> tuple[PredictionRowV2, ...]:
    if isinstance(rows, (str, bytes, bytearray)):
        raise ValueError("V2 prediction rows require a row sequence")
    try:
        materialized = tuple(rows)
    except TypeError as exc:
        raise ValueError("V2 prediction rows require a row sequence") from exc
    if not materialized or not all(isinstance(row, PredictionRowV2) for row in materialized):
        raise ValueError("V2 prediction rows require a nonempty PredictionRowV2 population")
    if tuple(sorted(materialized, key=_row_key)) != materialized:
        raise ValueError("V2 prediction rows are not in strict order")
    if len({row.game_pk for row in materialized}) != len(materialized):
        raise ValueError("V2 prediction rows contain duplicate game_pk")
    if len({(row.game_pk, row.official_date, row.target_year) for row in materialized}) != len(materialized):
        raise ValueError("V2 prediction rows contain duplicate population key")
    if len(feature_rows) != len(materialized):
        raise ValueError("V2 prediction and feature populations differ")
    expected_year = task8_target_year(fold_id)
    authority = resolve_prediction_weight_authority(spec, "v5.1", None)
    if authority.sha256 != weight_sha256:
        raise ValueError("V2 prediction weight authority changed while reading")
    weights = authority.component_weights_pp
    for feature, row in zip(feature_rows, materialized):
        expected_probability = home_probability(_feature_edge(feature, weights), calibration.alpha, calibration.beta)
        if (
            row.model_id != "v5.1" or row.target_year != expected_year
            or date.fromisoformat(row.official_date).year != expected_year
            or row.prediction_contract_sha256 != spec.prediction_contract_sha256
            or row.scoring_model_card_sha256 != weight_sha256
            or row.calibration_card_sha256 != calibration.artifact_sha256
            or row.p_home != expected_probability
            or (row.game_pk, row.official_date, row.scheduled_start_utc, row.target_year, row.feature_row_sha256)
            != (feature.game_pk, feature.official_date, feature.scheduled_start_utc, feature.target_year, feature.feature_row_sha256)
        ):
            raise ValueError("V2 prediction and authority rows differ")
    return materialized


def _authenticate_inputs(
    spec: ModelImprovementRunSpec, source_gate: SourceGateReceipt, feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None, ledger_id: str,
) -> tuple[str, SourceGateReceipt, FeatureSeal, tuple[object, ...], ProbabilityCalibrationCardV2, str]:
    fold_id = _baseline_fold_from_ledger_id(ledger_id)
    if baseline_replay is not None:
        raise ValueError("V2 Task8A baseline prediction does not accept baseline replay")
    gate = _authenticate_gate(spec, source_gate)
    feature, feature_rows = _authenticate_feature(spec, gate, feature_seal, fold_id)
    calibration, weight_sha256 = _authenticate_calibration(spec, gate, fold_id)
    return fold_id, gate, feature, feature_rows, calibration, weight_sha256


def _seal_from_payload(payload: Mapping[str, object], digest: str, path: Path) -> PredictionSealV2:
    values = _require_exact_mapping(payload, _SEAL_FIELDS, "V2 prediction seal")
    if values["schema"] != _SCHEMA:
        raise ValueError("V2 prediction seal schema differs")
    return PredictionSealV2.from_payload_mapping(values, digest, str(path))


def _load_rows(path: Path, expected: str) -> tuple[PredictionRowV2, ...]:
    raw = read_regular_bytes(path, "V2 prediction rows")
    if (
        sha256(raw).hexdigest() != expected or not raw or not raw.endswith(b"\n")
        or b"\r" in raw or b"\n\n" in raw
    ):
        raise ValueError("V2 prediction rows digest or canonical JSONL differs")
    rows: list[PredictionRowV2] = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            raise ValueError("V2 prediction rows are not canonical JSONL")
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("V2 prediction rows are not JSONL") from exc
        if not isinstance(payload, Mapping) or canonical_json_bytes(payload) != line:
            raise ValueError("V2 prediction rows are not canonical JSONL")
        rows.append(PredictionRowV2.from_payload_mapping(payload))
    return tuple(rows)


def _load_bound_prediction_rows(
    spec: ModelImprovementRunSpec, fold_id: str, feature_rows: Sequence[object],
    calibration: ProbabilityCalibrationCardV2, weight_sha256: str, seal: PredictionSealV2,
) -> tuple[PredictionRowV2, ...]:
    paths = prediction_paths_v2(spec, seal.ledger_id)
    _load_sidecar(paths.rows_sidecar, seal.rows_sha256, "V2 prediction rows sidecar")
    rows = _load_rows(paths.rows, seal.rows_sha256)
    ordered = _validate_rows(spec, fold_id, feature_rows, rows, weight_sha256, calibration)
    if (
        _rows_bytes(ordered) != _rows_bytes(rows)
        or sha256(_rows_bytes(rows)).hexdigest() != seal.rows_sha256
        or _population_digest(rows) != seal.population_sha256
    ):
        raise ValueError("V2 prediction row population differs")
    return rows


def seal_oof_predictions_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None,
    ledger_id: str,
    rows: Sequence[PredictionRowV2],
) -> PredictionSealV2:
    """Publish one calibrated baseline V2 seal; its primary is the commit point."""
    fold_id = _baseline_fold_from_ledger_id(ledger_id)
    if baseline_replay is not None:
        raise ValueError("V2 Task8A baseline prediction does not accept baseline replay")
    paths = prediction_paths_v2(spec, ledger_id)
    _, gate, feature, feature_rows, calibration, weight_sha256 = _authenticate_inputs(
        spec, source_gate, feature_seal, baseline_replay, ledger_id
    )
    ordered = _validate_rows(spec, fold_id, feature_rows, rows, weight_sha256, calibration)
    rows_bytes = _rows_bytes(ordered)
    rows_digest = sha256(rows_bytes).hexdigest()
    provisional = PredictionSealV2(
        _SCHEMA, ledger_id, fold_id, "v5.1", run_spec_sha256(spec), gate.artifact_sha256,
        feature.artifact_sha256, spec.prediction_contract_sha256, weight_sha256,
        calibration.artifact_sha256, _population_digest(ordered), None, rows_digest,
        "0" * 64, str(paths.seal),
    )
    payload_bytes = canonical_json_bytes(provisional.as_mapping())
    seal_digest = sha256(payload_bytes).hexdigest()
    _preflight(paths.rows, rows_bytes, paths.rows_sidecar, "V2 prediction rows")
    _preflight(paths.seal, payload_bytes, paths.seal_sidecar, "V2 prediction seal")
    if _existing_regular_bytes(paths.seal, "V2 prediction seal") is not None and _existing_regular_bytes(paths.rows, "V2 prediction rows") is None:
        raise ValueError("V2 prediction seal primary requires rows primary")
    write_immutable_bytes(paths.rows_sidecar, canonical_json_bytes(_sidecar_payload(rows_digest)))
    write_immutable_bytes(paths.seal_sidecar, canonical_json_bytes(_sidecar_payload(seal_digest)))
    write_immutable_bytes(paths.rows, rows_bytes)
    (
        _, final_gate, final_feature, final_feature_rows,
        final_calibration, final_weight_sha256,
    ) = _authenticate_inputs(
        spec, source_gate, feature_seal, baseline_replay, ledger_id
    )
    final_rows = _validate_rows(
        spec, fold_id, final_feature_rows, rows, final_weight_sha256, final_calibration
    )
    final_rows_bytes = _rows_bytes(final_rows)
    final_provisional = PredictionSealV2(
        _SCHEMA, ledger_id, fold_id, "v5.1", run_spec_sha256(spec),
        final_gate.artifact_sha256, final_feature.artifact_sha256,
        spec.prediction_contract_sha256, final_weight_sha256,
        final_calibration.artifact_sha256, _population_digest(final_rows), None,
        sha256(final_rows_bytes).hexdigest(), "0" * 64, str(paths.seal),
    )
    final_payload_bytes = canonical_json_bytes(final_provisional.as_mapping())
    if (
        final_rows_bytes != rows_bytes
        or final_provisional.as_mapping() != provisional.as_mapping()
        or final_payload_bytes != payload_bytes
        or sha256(final_payload_bytes).hexdigest() != seal_digest
    ):
        raise ValueError("V2 prediction authority changed before commit")
    write_immutable_bytes(paths.seal, payload_bytes)
    seal = load_prediction_seal_v2(spec, gate, feature, None, paths.seal, seal_digest)
    if seal.as_mapping() != provisional.as_mapping():
        raise ValueError("V2 prediction seal reread differs")
    if load_prediction_rows_v2(spec, gate, feature, None, seal) != ordered:
        raise ValueError("V2 prediction rows reread differs")
    return seal


def _seal_path_from_argument(spec: ModelImprovementRunSpec, path: Path | str) -> tuple[Path, str]:
    provided = _canonical_absolute(path, "V2 prediction seal")
    root = prediction_paths_v2(spec, "baseline/v5.1/H22").seal.parents[4]
    try:
        relative = provided.relative_to(root)
    except ValueError as exc:
        raise ValueError("V2 prediction seal path differs") from exc
    parts = relative.parts
    if len(parts) != 5 or parts[:2] != ("baseline", "v5.1") or parts[3:] != ("v2", "prediction_seal.json"):
        raise ValueError("V2 prediction seal path differs")
    ledger_id = "/".join(parts[:3])
    _baseline_fold_from_ledger_id(ledger_id)
    expected = prediction_paths_v2(spec, ledger_id).seal
    _same_descriptor_path(provided, expected, "V2 prediction seal")
    return expected, ledger_id


def load_prediction_seal_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None,
    path: Path | str,
    expected_sha256: str,
) -> PredictionSealV2:
    """Securely load one calibrated V2 descriptor and all bound authorities."""
    expected = _require_sha256(expected_sha256, "expected V2 prediction seal sha256")
    seal_path, ledger_id = _seal_path_from_argument(spec, path)
    fold_id, gate, feature, feature_rows, calibration, weight_sha256 = _authenticate_inputs(
        spec, source_gate, feature_seal, baseline_replay, ledger_id
    )
    raw = read_regular_bytes(seal_path, "V2 prediction seal")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("V2 prediction seal digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("V2 prediction seal is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("V2 prediction seal is not canonical JSON")
    paths = prediction_paths_v2(spec, ledger_id)
    _load_sidecar(paths.seal_sidecar, actual, "V2 prediction seal sidecar")
    seal = _seal_from_payload(payload, actual, seal_path)
    if (
        seal.ledger_id != ledger_id or seal.fold_id != fold_id or seal.model_id != "v5.1"
        or seal.run_spec_sha256 != run_spec_sha256(spec)
        or seal.source_gate_sha256 != gate.artifact_sha256
        or seal.feature_seal_sha256 != feature.artifact_sha256
        or seal.prediction_contract_sha256 != spec.prediction_contract_sha256
        or seal.scoring_model_card_sha256 != weight_sha256
        or seal.calibration_card_sha256 != calibration.artifact_sha256
        or seal.frozen_model_card_sha256 is not None
    ):
        raise ValueError("V2 prediction seal binding differs")
    _load_bound_prediction_rows(spec, fold_id, feature_rows, calibration, weight_sha256, seal)
    after = _authenticate_inputs(spec, gate, feature, None, ledger_id)
    if (
        after[1].artifact_sha256 != gate.artifact_sha256
        or after[2].artifact_sha256 != feature.artifact_sha256
        or after[4].artifact_sha256 != calibration.artifact_sha256
        or after[5] != weight_sha256
        or read_regular_bytes(seal_path, "V2 prediction seal") != raw
    ):
        raise ValueError("V2 prediction authority changed while reading")
    if _load_bound_prediction_rows(
        spec, fold_id, after[3], after[4], after[5], seal
    ) != _load_bound_prediction_rows(
        spec, fold_id, feature_rows, calibration, weight_sha256, seal
    ):
        raise ValueError("V2 prediction rows changed while reading")
    final_raw = read_regular_bytes(seal_path, "V2 prediction seal")
    if final_raw != raw or sha256(final_raw).hexdigest() != expected:
        raise ValueError("V2 prediction seal changed while reading")
    _load_sidecar(paths.seal_sidecar, expected, "V2 prediction seal sidecar")
    if read_regular_bytes(seal_path, "V2 prediction seal") != final_raw:
        raise ValueError("V2 prediction seal changed while reading")
    return seal


def load_prediction_rows_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceiptV2 | None,
    seal: PredictionSealV2,
) -> tuple[PredictionRowV2, ...]:
    """The sole V2 accessor for calibrated label-free prediction rows."""
    if not isinstance(seal, PredictionSealV2):
        raise ValueError("V2 prediction rows require a PredictionSealV2")
    fold_id = _baseline_fold_from_ledger_id(seal.ledger_id)
    if baseline_replay is not None:
        raise ValueError("V2 Task8A baseline prediction does not accept baseline replay")
    paths = prediction_paths_v2(spec, seal.ledger_id)
    _same_descriptor_path(seal.path, paths.seal, "V2 prediction seal")
    loaded = load_prediction_seal_v2(
        spec, source_gate, feature_seal, None, paths.seal, seal.artifact_sha256
    )
    if (
        loaded.as_mapping() != seal.as_mapping()
        or loaded.artifact_sha256 != seal.artifact_sha256
        or loaded.path != seal.path
    ):
        raise ValueError("V2 prediction seal descriptor differs")
    _, gate, feature, feature_rows, calibration, weight_sha256 = _authenticate_inputs(
        spec, source_gate, feature_seal, None, seal.ledger_id
    )
    rows = _load_bound_prediction_rows(
        spec, fold_id, feature_rows, calibration, weight_sha256, loaded
    )
    if loaded.source_gate_sha256 != gate.artifact_sha256 or loaded.feature_seal_sha256 != feature.artifact_sha256:
        raise ValueError("V2 prediction row binding differs")
    return rows
