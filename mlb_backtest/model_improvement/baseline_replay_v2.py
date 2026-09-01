"""Authenticated, calibrated V2 baseline replay receipts.

This module deliberately has no outcome or evaluation dependency.  It proves
that two fresh baseline materializations agree before publishing the matching
Task8A prediction seal and its own receipt.
"""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

from mlb_backtest.scoring import component_agreement, confidence_score, home_probability

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_bytes
from .contracts import (
    PredictionRowV2,
    PredictionSealV2,
    ProbabilityCalibrationCardV2,
    _require_exact_mapping,
    _require_sha256,
)
from .oof_ledger_v2 import (
    _authenticate_feature,
    _authenticate_gate,
    load_prediction_rows_v2,
    load_prediction_seal_v2,
    prediction_paths_v2,
    seal_oof_predictions_v2,
)
from .probability_calibration import require_probability_calibration_card_v2
from .run_spec import ModelImprovementRunSpec, run_spec_sha256, task8_target_year
from .scoring_cards import PredictionWeightAuthority, resolve_prediction_weight_authority
from .sealed_features import FeatureSeal
from .source_gate import SourceGateReceipt


_SCHEMA = "MODEL_IMPROVEMENT_BASELINE_REPLAY_RECEIPT_V2"
_FIELDS = frozenset(
    {
        "schema", "ledger_id", "fold_id", "model_id", "run_spec_sha256",
        "source_gate_sha256", "feature_seal_sha256", "prediction_contract_sha256",
        "scoring_model_card_sha256", "calibration_card_path",
        "calibration_card_sha256", "population_sha256", "replay_one_rows_sha256",
        "replay_two_rows_sha256", "replay_one_bytes_sha256",
        "replay_two_bytes_sha256", "prediction_rows_sha256",
        "prediction_seal_path", "prediction_seal_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class BaselineReplayReceiptV2:
    """The closed Task6A descriptor; its primary is the replay commit point."""

    schema: str
    ledger_id: str
    fold_id: str
    model_id: str
    run_spec_sha256: str
    source_gate_sha256: str
    feature_seal_sha256: str
    prediction_contract_sha256: str
    scoring_model_card_sha256: str
    calibration_card_path: str
    calibration_card_sha256: str
    population_sha256: str
    replay_one_rows_sha256: str
    replay_two_rows_sha256: str
    replay_one_bytes_sha256: str
    replay_two_bytes_sha256: str
    prediction_rows_sha256: str
    prediction_seal_path: str
    prediction_seal_sha256: str
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError("V2 baseline replay schema differs")
        if self.model_id != "v5.1":
            raise ValueError("V2 baseline replay model differs")
        if not isinstance(self.fold_id, str):
            raise ValueError("V2 baseline replay fold differs")
        try:
            task8_target_year(self.fold_id)
        except ValueError as exc:
            raise ValueError("V2 baseline replay fold differs") from exc
        if self.ledger_id != f"baseline/v5.1/{self.fold_id}":
            raise ValueError("V2 baseline replay ledger_id differs")
        for field in (
            "run_spec_sha256", "source_gate_sha256", "feature_seal_sha256",
            "prediction_contract_sha256", "scoring_model_card_sha256",
            "calibration_card_sha256", "population_sha256",
            "replay_one_rows_sha256", "replay_two_rows_sha256",
            "replay_one_bytes_sha256", "replay_two_bytes_sha256",
            "prediction_rows_sha256", "prediction_seal_sha256", "artifact_sha256",
        ):
            object.__setattr__(self, field, _require_sha256(getattr(self, field), field))
        for field in ("calibration_card_path", "prediction_seal_path", "path"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.startswith("/"):
                raise ValueError(f"V2 baseline replay {field} differs")
        if (
            self.replay_one_rows_sha256 != self.replay_two_rows_sha256
            or self.replay_one_rows_sha256 != self.replay_one_bytes_sha256
            or self.replay_one_rows_sha256 != self.replay_two_bytes_sha256
            or self.replay_one_rows_sha256 != self.prediction_rows_sha256
        ):
            raise ValueError("V2 baseline replay stream digests differ")

    def as_mapping(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
            if field not in {"artifact_sha256", "path"}
        }

    @classmethod
    def from_payload_mapping(
        cls, payload: Mapping[str, object], artifact_sha256: str, path: str
    ) -> "BaselineReplayReceiptV2":
        return cls(
            **dict(_require_exact_mapping(payload, _FIELDS, "V2 baseline replay receipt")),
            artifact_sha256=artifact_sha256,
            path=path,
        )


def _canonical_absolute(path: Path | str, label: str) -> Path:
    try:
        raw = os.fspath(path)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} path differs") from exc
    candidate = Path(raw) if isinstance(raw, str) else None
    if (
        not isinstance(raw, str) or not raw or candidate is None
        or not candidate.is_absolute() or "//" in raw or raw != str(candidate)
        or raw != os.path.normpath(raw)
    ):
        raise ValueError(f"{label} path differs")
    return candidate


def _fold_from_feature_seal(feature_seal: object) -> str:
    if not isinstance(feature_seal, FeatureSeal) or feature_seal.feature_set_id != "common":
        raise ValueError("V2 baseline replay requires a Common FeatureSeal")
    if not isinstance(feature_seal.fold_id, str):
        raise ValueError("V2 baseline replay fold differs")
    try:
        task8_target_year(feature_seal.fold_id)
    except ValueError as exc:
        raise ValueError("V2 baseline replay fold differs") from exc
    return feature_seal.fold_id


def _receipt_path(spec: ModelImprovementRunSpec, fold_id: str) -> Path:
    # The Task8 identity parser has already rejected H24/nonbaseline before this
    # derived path is ever observed.
    return spec.output_path("oof", f"baseline/v5.1/{fold_id}/v2/baseline_replay_receipt.json")


def _receipt_path_from_argument(
    spec: ModelImprovementRunSpec, path: Path | str
) -> tuple[Path, str]:
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("V2 baseline replay requires ModelImprovementRunSpec")
    supplied = _canonical_absolute(path, "V2 baseline replay receipt")
    root = spec.output_path("oof", "baseline/v5.1/H22/v2/baseline_replay_receipt.json").parents[5]
    try:
        parts = supplied.relative_to(root / "oof").parts
    except ValueError as exc:
        raise ValueError("V2 baseline replay receipt path differs") from exc
    if (
        len(parts) != 5 or parts[:2] != ("baseline", "v5.1")
        or parts[3:] != ("v2", "baseline_replay_receipt.json")
    ):
        raise ValueError("V2 baseline replay receipt path differs")
    fold_id = parts[2]
    try:
        task8_target_year(fold_id)
    except ValueError as exc:
        raise ValueError("V2 baseline replay receipt path differs") from exc
    expected = _receipt_path(spec, fold_id)
    if str(supplied) != str(expected):
        raise ValueError("V2 baseline replay receipt path differs")
    return expected, fold_id


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
    previous = _existing_regular_bytes(primary, label)
    previous_sidecar = _existing_regular_bytes(sidecar, f"{label} sidecar")
    digest = sha256(contents).hexdigest()
    if previous is not None:
        if previous != contents:
            raise ValueError(f"immutable {label} differs")
        if previous_sidecar is None:
            raise ValueError(f"{label} primary requires sidecar")
        _load_sidecar(sidecar, digest, f"{label} sidecar")
    elif previous_sidecar is not None:
        _load_sidecar(sidecar, digest, f"{label} sidecar")


def _rows_bytes(rows: Sequence[PredictionRowV2]) -> bytes:
    return b"".join(canonical_json_bytes(row.as_mapping()) for row in rows)


def _population_digest(rows: Sequence[PredictionRowV2]) -> str:
    return sha256(canonical_json_bytes([
        {"game_pk": row.game_pk, "official_date": row.official_date, "target_year": row.target_year}
        for row in rows
    ])).hexdigest()


def _authenticate_calibration(
    spec: ModelImprovementRunSpec,
    gate: SourceGateReceipt,
    fold_id: str,
) -> tuple[ProbabilityCalibrationCardV2, PredictionWeightAuthority]:
    card = require_probability_calibration_card_v2(spec, fold_id)
    authority = resolve_prediction_weight_authority(spec, "v5.1", None)
    pool = spec.probability_calibration_policy.pools[
        spec.probability_calibration_policy.fold_keys[fold_id]
    ]
    if not isinstance(card, ProbabilityCalibrationCardV2) or (
        card.model_id, card.lane_id, card.fold_id, card.calibration_key,
        card.target_year, card.training_start, card.training_end,
    ) != (
        "v5.1", "baseline", fold_id,
        spec.probability_calibration_policy.fold_keys[fold_id], pool.target_year,
        pool.training_start, pool.training_end,
    ):
        raise ValueError("V2 baseline replay calibration identity differs")
    if (
        card.run_spec_sha256 != run_spec_sha256(spec)
        or card.source_gate_sha256 != gate.artifact_sha256
        or card.prediction_contract_sha256 != spec.prediction_contract_sha256
        or card.weight_authority_sha256 != authority.sha256
        or not math.isfinite(card.alpha) or not math.isfinite(card.beta) or card.beta <= 0
    ):
        raise ValueError("V2 baseline replay calibration binding differs")
    if _canonical_absolute(card.path, "V2 calibration card") != Path(card.path):
        raise ValueError("V2 baseline replay calibration path differs")
    return card, authority


def _authenticate_inputs(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
) -> tuple[str, SourceGateReceipt, FeatureSeal, tuple[object, ...], ProbabilityCalibrationCardV2, PredictionWeightAuthority]:
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("V2 baseline replay requires ModelImprovementRunSpec")
    fold_id = _fold_from_feature_seal(feature_seal)
    gate = _authenticate_gate(spec, source_gate)
    feature, feature_rows = _authenticate_feature(spec, gate, feature_seal, fold_id)
    card, authority = _authenticate_calibration(spec, gate, fold_id)
    return fold_id, gate, feature, feature_rows, card, authority


def _materialize_v51_rows(
    spec: ModelImprovementRunSpec,
    feature_rows: Sequence[object],
    calibration: ProbabilityCalibrationCardV2,
    weights: PredictionWeightAuthority,
) -> tuple[PredictionRowV2, ...]:
    """Derive a fresh, ordered label-free baseline stream from authenticated rows."""
    draft: list[tuple[object, float, str, float, float]] = []
    for feature in feature_rows:
        edge = math.fsum(
            weights.component_weights_pp[component] / 100.0
            * (feature.component_scores["home"][component] - feature.component_scores["away"][component])
            for component in weights.component_weights_pp
        )
        p_home = home_probability(edge, calibration.alpha, calibration.beta)
        if p_home > 0.5:
            picked_side, pick_probability = "home", p_home
        elif p_home < 0.5:
            picked_side, pick_probability = "away", 1.0 - p_home
        else:
            picked_side, pick_probability = "none", 0.5
        qualities = {
            component: min(
                feature.component_qualities["home"][component],
                feature.component_qualities["away"][component],
            )
            for component in weights.component_weights_pp
        }
        completeness = math.fsum(
            weights.component_weights_pp[component] / 100.0 * qualities[component]
            for component in weights.component_weights_pp
        )
        core_edges = {
            component: feature.component_scores["home"][component]
            - feature.component_scores["away"][component]
            for component in ("starter", "offense", "bullpen")
        }
        confidence = confidence_score(
            completeness, qualities["starter"], qualities["offense"],
            qualities["bullpen"], component_agreement(core_edges, picked_side),
            qualities["environment"], qualities["schedule"],
        )
        draft.append((feature, p_home, picked_side, pick_probability, confidence))
    rank_groups: dict[str, list[int]] = {}
    for index, value in enumerate(draft):
        if value[2] != "none":
            rank_groups.setdefault(value[0].official_date, []).append(index)
    ranks: dict[int, int] = {}
    for indices in rank_groups.values():
        ordered_indices = sorted(
            indices,
            key=lambda index: (
                -draft[index][3], -draft[index][4], -draft[index][0].starter_edge_abs,
                draft[index][0].scheduled_start_utc, draft[index][0].game_pk,
            ),
        )
        ranks.update({index: rank for rank, index in enumerate(ordered_indices, start=1)})
    return tuple(
        PredictionRowV2(
            "v5.1", feature.game_pk, feature.official_date, feature.target_year,
            p_home, picked_side, pick_probability, confidence, feature.starter_edge_abs,
            feature.scheduled_start_utc, ranks.get(index, 0), feature.feature_row_sha256,
            spec.prediction_contract_sha256, weights.sha256, calibration.artifact_sha256,
        )
        for index, (feature, p_home, picked_side, pick_probability, confidence) in enumerate(draft)
    )


def _replay_equal(left: Sequence[PredictionRowV2], right: Sequence[PredictionRowV2]) -> bool:
    left_rows, right_rows = tuple(left), tuple(right)
    left_bytes, right_bytes = _rows_bytes(left_rows), _rows_bytes(right_rows)
    return (
        bool(left_rows)
        and left_rows == right_rows
        and tuple(row.as_mapping() for row in left_rows) == tuple(row.as_mapping() for row in right_rows)
        and left_bytes == right_bytes
        and sha256(left_bytes).hexdigest() == sha256(right_bytes).hexdigest()
        and _population_digest(left_rows) == _population_digest(right_rows)
    )


def _receipt_from_payload(payload: Mapping[str, object], digest: str, path: Path) -> BaselineReplayReceiptV2:
    values = _require_exact_mapping(payload, _FIELDS, "V2 baseline replay receipt")
    return BaselineReplayReceiptV2.from_payload_mapping(values, digest, str(path))


def replay_v51_predictions_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
) -> BaselineReplayReceiptV2:
    """Publish one receipt only after two independent calibrated replays agree."""
    fold_id, gate, feature, feature_rows, calibration, authority = _authenticate_inputs(
        spec, source_gate, feature_seal
    )
    replay_one = _materialize_v51_rows(spec, feature_rows, calibration, authority)
    (
        second_fold, second_gate, second_feature, second_feature_rows,
        second_calibration, second_authority,
    ) = _authenticate_inputs(spec, gate, feature)
    replay_two = _materialize_v51_rows(
        spec, second_feature_rows, second_calibration, second_authority
    )
    if (
        second_fold != fold_id
        or second_gate.artifact_sha256 != gate.artifact_sha256
        or second_feature.artifact_sha256 != feature.artifact_sha256
        or second_calibration.artifact_sha256 != calibration.artifact_sha256
        or second_authority.sha256 != authority.sha256
        or not _replay_equal(replay_one, replay_two)
    ):
        raise ValueError("V2 baseline independent replay differs")
    ledger_id = f"baseline/v5.1/{fold_id}"
    seal = seal_oof_predictions_v2(spec, gate, feature, None, ledger_id, replay_one)
    paths = prediction_paths_v2(spec, ledger_id)
    if not isinstance(seal, PredictionSealV2) or seal.path != str(paths.seal):
        raise ValueError("V2 baseline replay prediction seal differs")
    row_bytes = _rows_bytes(replay_one)
    rows_digest = sha256(row_bytes).hexdigest()
    if (
        seal.rows_sha256 != rows_digest or seal.population_sha256 != _population_digest(replay_one)
        or seal.calibration_card_sha256 != calibration.artifact_sha256
        or seal.scoring_model_card_sha256 != authority.sha256
    ):
        raise ValueError("V2 baseline replay prediction binding differs")
    (
        final_fold, final_gate, final_feature, _, final_calibration, final_authority,
    ) = _authenticate_inputs(spec, gate, feature)
    if (
        final_fold != fold_id or final_gate.artifact_sha256 != gate.artifact_sha256
        or final_feature.artifact_sha256 != feature.artifact_sha256
        or final_calibration.artifact_sha256 != calibration.artifact_sha256
        or final_authority.sha256 != authority.sha256
    ):
        raise ValueError("V2 baseline replay authority changed before receipt")
    committed_seal = load_prediction_seal_v2(
        spec, final_gate, final_feature, None, paths.seal, seal.artifact_sha256
    )
    committed_rows = load_prediction_rows_v2(
        spec, final_gate, final_feature, None, committed_seal
    )
    if (
        committed_seal.as_mapping() != seal.as_mapping()
        or committed_seal.artifact_sha256 != seal.artifact_sha256
        or committed_seal.path != seal.path
        or committed_rows != replay_one
    ):
        raise ValueError("V2 baseline replay prediction changed before receipt")
    receipt_path = _receipt_path(spec, fold_id)
    provisional = BaselineReplayReceiptV2(
        _SCHEMA, ledger_id, fold_id, "v5.1", run_spec_sha256(spec), gate.artifact_sha256,
        feature.artifact_sha256, spec.prediction_contract_sha256, authority.sha256,
        calibration.path, calibration.artifact_sha256, seal.population_sha256,
        rows_digest, rows_digest, rows_digest, rows_digest, seal.rows_sha256,
        str(paths.seal), seal.artifact_sha256, "0" * 64, str(receipt_path),
    )
    payload_bytes = canonical_json_bytes(provisional.as_mapping())
    digest = sha256(payload_bytes).hexdigest()
    sidecar = receipt_path.with_suffix(receipt_path.suffix + ".sha256")
    _preflight(receipt_path, payload_bytes, sidecar, "V2 baseline replay receipt")
    write_immutable_bytes(sidecar, canonical_json_bytes(_sidecar_payload(digest)))
    write_immutable_bytes(receipt_path, payload_bytes)
    loaded = load_baseline_replay_v2(spec, gate, feature, receipt_path, digest)
    if loaded.as_mapping() != provisional.as_mapping():
        raise ValueError("V2 baseline replay receipt reread differs")
    return loaded


def load_baseline_replay_v2(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    path: Path | str,
    expected_sha256: str,
) -> BaselineReplayReceiptV2:
    """Securely reload one receipt and re-prove every input and replay claim."""
    expected = _require_sha256(expected_sha256, "expected V2 baseline replay sha256")
    receipt_path, fold_id = _receipt_path_from_argument(spec, path)
    bound_fold, gate, feature, feature_rows, calibration, authority = _authenticate_inputs(
        spec, source_gate, feature_seal
    )
    if bound_fold != fold_id:
        raise ValueError("V2 baseline replay feature fold differs")
    raw = read_regular_bytes(receipt_path, "V2 baseline replay receipt")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("V2 baseline replay receipt digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("V2 baseline replay receipt is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("V2 baseline replay receipt is not canonical JSON")
    _load_sidecar(receipt_path.with_suffix(receipt_path.suffix + ".sha256"), actual, "V2 baseline replay receipt sidecar")
    receipt = _receipt_from_payload(payload, actual, receipt_path)
    ledger_id = f"baseline/v5.1/{fold_id}"
    paths = prediction_paths_v2(spec, ledger_id)
    if (
        receipt.ledger_id != ledger_id or receipt.fold_id != fold_id
        or receipt.run_spec_sha256 != run_spec_sha256(spec)
        or receipt.source_gate_sha256 != gate.artifact_sha256
        or receipt.feature_seal_sha256 != feature.artifact_sha256
        or receipt.prediction_contract_sha256 != spec.prediction_contract_sha256
        or receipt.scoring_model_card_sha256 != authority.sha256
        or receipt.calibration_card_path != calibration.path
        or receipt.calibration_card_sha256 != calibration.artifact_sha256
        or receipt.prediction_seal_path != str(paths.seal)
    ):
        raise ValueError("V2 baseline replay receipt binding differs")
    seal = load_prediction_seal_v2(
        spec, gate, feature, None, paths.seal, receipt.prediction_seal_sha256
    )
    rows = load_prediction_rows_v2(spec, gate, feature, None, seal)
    replay_one = _materialize_v51_rows(spec, feature_rows, calibration, authority)
    (
        second_fold, second_gate, second_feature, second_feature_rows,
        second_calibration, second_authority,
    ) = _authenticate_inputs(spec, gate, feature)
    replay_two = _materialize_v51_rows(
        spec, second_feature_rows, second_calibration, second_authority
    )
    row_bytes = _rows_bytes(rows)
    digest = sha256(row_bytes).hexdigest()
    if (
        second_fold != fold_id or second_gate.artifact_sha256 != gate.artifact_sha256
        or second_feature.artifact_sha256 != feature.artifact_sha256
        or second_calibration.artifact_sha256 != calibration.artifact_sha256
        or second_authority.sha256 != authority.sha256
        or not _replay_equal(replay_one, replay_two)
        or rows != replay_one
        or receipt.population_sha256 != _population_digest(rows)
        or receipt.replay_one_rows_sha256 != digest
        or receipt.replay_two_rows_sha256 != digest
        or receipt.replay_one_bytes_sha256 != digest
        or receipt.replay_two_bytes_sha256 != digest
        or receipt.prediction_rows_sha256 != seal.rows_sha256
        or receipt.prediction_rows_sha256 != digest
        or seal.population_sha256 != receipt.population_sha256
        or seal.calibration_card_sha256 != receipt.calibration_card_sha256
    ):
        raise ValueError("V2 baseline replay receipt replay differs")
    after_raw = read_regular_bytes(receipt_path, "V2 baseline replay receipt")
    if after_raw != raw or sha256(after_raw).hexdigest() != expected:
        raise ValueError("V2 baseline replay receipt changed while reading")
    _load_sidecar(receipt_path.with_suffix(receipt_path.suffix + ".sha256"), expected, "V2 baseline replay receipt sidecar")
    final_seal = load_prediction_seal_v2(
        spec, second_gate, second_feature, None, paths.seal, receipt.prediction_seal_sha256
    )
    final_rows = load_prediction_rows_v2(spec, second_gate, second_feature, None, final_seal)
    if final_seal != seal or final_rows != rows:
        raise ValueError("V2 baseline replay prediction changed while reading")
    return receipt
