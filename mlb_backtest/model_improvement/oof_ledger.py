"""Task8A's immutable, label-free baseline OOF prediction artifacts."""

from __future__ import annotations

import json
import os
import stat
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Mapping, Sequence

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_bytes
from .contracts import PredictionRow, PredictionSeal, _require_exact_mapping, _require_sha256
from .run_spec import ModelImprovementRunSpec, run_spec_sha256, task8_target_year
from .scoring_cards import resolve_prediction_weight_authority
from .sealed_features import FeatureSeal, load_feature_seal, load_sealed_feature_rows
from .source_gate import SourceGateReceipt, load_source_gate_receipt

if TYPE_CHECKING:
    from .baseline_replay import BaselineReplayReceipt


_SCHEMA = "MODEL_IMPROVEMENT_PREDICTION_SEAL_V1"
_SEAL_FIELDS = frozenset({
    "schema", "ledger_id", "fold_id", "model_id", "candidate_id",
    "run_spec_sha256", "source_gate_sha256", "feature_seal_sha256",
    "prediction_contract_sha256", "scoring_model_card_sha256",
    "population_sha256", "frozen_model_card_sha256", "rows_sha256",
})


def _identity(ledger_id: str) -> str:
    """Accept exactly the Task8A baseline namespace, before any I/O."""
    if not isinstance(ledger_id, str) or ledger_id.count("/") != 2:
        raise ValueError("prediction ledger identity differs")
    scope, model_id, fold_id = ledger_id.split("/")
    if scope != "baseline" or model_id != "v5.1":
        raise ValueError("Task8A requires baseline/v5.1 ledger identity")
    task8_target_year(fold_id)  # rejects H24 and all aliases
    return fold_id


def prediction_paths(
    spec: ModelImprovementRunSpec, ledger_id: str
) -> tuple[Path, Path, Path, Path]:
    """Derive Task8A's four immutable paths without observing the filesystem."""
    if not isinstance(spec, ModelImprovementRunSpec):
        raise ValueError("prediction paths require ModelImprovementRunSpec")
    fold_id = _identity(ledger_id)
    directory = f"baseline/v5.1/{fold_id}"
    rows = spec.output_path("oof", f"{directory}/prediction_rows.jsonl")
    seal = spec.output_path("oof", f"{directory}/prediction_seal.json")
    return rows, rows.with_suffix(".jsonl.sha256"), seal, seal.with_suffix(".json.sha256")


def _canonical_absolute(path: Path | str, label: str) -> Path:
    try:
        raw = os.fspath(path)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} path differs") from exc
    candidate = Path(raw) if isinstance(raw, str) else None
    if (
        not isinstance(raw, str)
        or not raw
        or candidate is None
        or not candidate.is_absolute()
        or "//" in raw
        or raw != str(candidate)
        or raw != os.path.normpath(raw)
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
    existing_primary = _existing_regular_bytes(primary, label)
    existing_sidecar = _existing_regular_bytes(sidecar, f"{label} sidecar")
    digest = sha256(contents).hexdigest()
    if existing_primary is not None:
        if existing_primary != contents:
            raise ValueError(f"immutable {label} differs")
        if existing_sidecar is None:
            raise ValueError(f"{label} primary requires sidecar")
        _load_sidecar(sidecar, digest, f"{label} sidecar")
    elif existing_sidecar is not None:
        _load_sidecar(sidecar, digest, f"{label} sidecar")


def _authenticate_gate(spec: ModelImprovementRunSpec, supplied: SourceGateReceipt) -> SourceGateReceipt:
    if not isinstance(supplied, SourceGateReceipt):
        raise ValueError("prediction seal requires source gate receipt")
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
        raise ValueError("source gate binding differs")
    return loaded


def _authenticate_feature(
    spec: ModelImprovementRunSpec, gate: SourceGateReceipt, supplied: FeatureSeal, fold_id: str
) -> tuple[FeatureSeal, tuple[object, ...]]:
    if not isinstance(supplied, FeatureSeal) or supplied.feature_set_id != "common":
        raise ValueError("Task8A requires a Common FeatureSeal")
    if supplied.fold_id != fold_id:
        raise ValueError("feature seal fold differs")
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
        raise ValueError("feature seal binding differs")
    rows = load_sealed_feature_rows(spec, gate, (task8_target_year(fold_id),), loaded)
    return loaded, rows


def _row_key(row: PredictionRow) -> tuple[str, str, int]:
    return row.official_date, row.scheduled_start_utc, row.game_pk


def _population_digest(rows: Sequence[PredictionRow]) -> str:
    return sha256(canonical_json_bytes([
        {"game_pk": row.game_pk, "official_date": row.official_date, "target_year": row.target_year}
        for row in rows
    ])).hexdigest()


def _rows_bytes(rows: Sequence[PredictionRow]) -> bytes:
    return b"".join(canonical_json_bytes(row.as_mapping()) for row in rows)


def _validate_rows(
    spec: ModelImprovementRunSpec,
    fold_id: str,
    feature_rows: Sequence[object],
    rows: Sequence[PredictionRow],
    card_sha256: str,
) -> tuple[PredictionRow, ...]:
    materialized = tuple(rows)
    if not materialized or not all(isinstance(row, PredictionRow) for row in materialized):
        raise ValueError("prediction rows require a nonempty PredictionRow population")
    if tuple(sorted(materialized, key=_row_key)) != materialized:
        raise ValueError("prediction rows are not in strict order")
    if len({row.game_pk for row in materialized}) != len(materialized):
        raise ValueError("prediction rows contain duplicate game_pk")
    population = {(row.game_pk, row.official_date, row.target_year) for row in materialized}
    if len(population) != len(materialized):
        raise ValueError("prediction rows contain duplicate population key")
    expected_year = task8_target_year(fold_id)
    if len(feature_rows) != len(materialized):
        raise ValueError("prediction and feature populations differ")
    for feature, row in zip(feature_rows, materialized):
        if (
            row.model_id != "v5.1"
            or row.target_year != expected_year
            or date.fromisoformat(row.official_date).year != expected_year
            or row.prediction_contract_sha256 != spec.prediction_contract_sha256
            or row.scoring_model_card_sha256 != card_sha256
            or (row.game_pk, row.official_date, row.scheduled_start_utc, row.target_year, row.feature_row_sha256)
            != (feature.game_pk, feature.official_date, feature.scheduled_start_utc, feature.target_year, feature.feature_row_sha256)
        ):
            raise ValueError("prediction and feature rows differ")
    return materialized


def _authenticate_inputs(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    ledger_id: str,
) -> tuple[str, SourceGateReceipt, FeatureSeal, tuple[object, ...], str]:
    fold_id = _identity(ledger_id)
    if baseline_replay is not None:
        raise ValueError("Task8A baseline prediction does not accept baseline replay")
    gate = _authenticate_gate(spec, source_gate)
    feature, feature_rows = _authenticate_feature(spec, gate, feature_seal, fold_id)
    card = resolve_prediction_weight_authority(spec, "v5.1", None)
    return fold_id, gate, feature, feature_rows, card.sha256


def _seal_from_payload(payload: Mapping[str, object], digest: str, path: Path) -> PredictionSeal:
    values = _require_exact_mapping(payload, _SEAL_FIELDS, "prediction seal")
    if values["schema"] != _SCHEMA:
        raise ValueError("prediction seal schema differs")
    return PredictionSeal.from_payload_mapping(
        {key: value for key, value in values.items() if key != "schema"}, digest, str(path)
    )


def _load_rows(path: Path, expected: str) -> tuple[PredictionRow, ...]:
    raw = read_regular_bytes(path, "prediction rows")
    if sha256(raw).hexdigest() != expected or not raw or not raw.endswith(b"\n"):
        raise ValueError("prediction rows digest or canonical JSONL differs")
    rows: list[PredictionRow] = []
    for line in raw.splitlines(keepends=True):
        if line == b"\n" or not line.endswith(b"\n"):
            raise ValueError("prediction rows are not canonical JSONL")
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("prediction rows are not JSONL") from exc
        if not isinstance(payload, Mapping) or canonical_json_bytes(payload) != line:
            raise ValueError("prediction rows are not canonical JSONL")
        rows.append(PredictionRow.from_payload_mapping(payload))
    return tuple(rows)


def seal_oof_predictions(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    ledger_id: str,
    rows: Sequence[PredictionRow],
) -> PredictionSeal:
    """Publish one baseline OOF seal; the final seal primary is the commit point."""
    fold_id = _identity(ledger_id)
    if baseline_replay is not None:
        raise ValueError("Task8A baseline prediction does not accept baseline replay")
    rows_path, rows_sidecar, seal_path, seal_sidecar = prediction_paths(spec, ledger_id)
    _, gate, feature, feature_rows, card_digest = _authenticate_inputs(
        spec, source_gate, feature_seal, baseline_replay, ledger_id
    )
    ordered = _validate_rows(spec, fold_id, feature_rows, rows, card_digest)
    rows_bytes = _rows_bytes(ordered)
    rows_digest = sha256(rows_bytes).hexdigest()
    provisional = PredictionSeal(
        ledger_id, fold_id, "v5.1", None, run_spec_sha256(spec), gate.artifact_sha256,
        feature.artifact_sha256, spec.prediction_contract_sha256, card_digest,
        _population_digest(ordered), None, rows_digest, "0" * 64, str(seal_path),
    )
    payload = {"schema": _SCHEMA} | provisional.as_mapping()
    payload_bytes = canonical_json_bytes(payload)
    seal_digest = sha256(payload_bytes).hexdigest()
    _preflight(rows_path, rows_bytes, rows_sidecar, "prediction rows")
    _preflight(seal_path, payload_bytes, seal_sidecar, "prediction seal")
    if _existing_regular_bytes(seal_path, "prediction seal") is not None and _existing_regular_bytes(rows_path, "prediction rows") is None:
        raise ValueError("prediction seal primary requires rows primary")
    write_immutable_bytes(rows_sidecar, canonical_json_bytes(_sidecar_payload(rows_digest)))
    write_immutable_bytes(seal_sidecar, canonical_json_bytes(_sidecar_payload(seal_digest)))
    write_immutable_bytes(rows_path, rows_bytes)
    write_immutable_bytes(seal_path, payload_bytes)
    seal = load_prediction_seal(
        spec, gate, feature, None, seal_path, seal_digest
    )
    if seal.as_mapping() != provisional.as_mapping():
        raise ValueError("prediction seal reread differs")
    if load_prediction_rows(spec, gate, feature, None, seal) != ordered:
        raise ValueError("prediction rows reread differs")
    return seal


def _seal_path_from_argument(spec: ModelImprovementRunSpec, path: Path | str) -> tuple[Path, str]:
    provided = _canonical_absolute(path, "prediction seal")
    root = spec.output_path("oof", "baseline/v5.1/H22/prediction_seal.json").parents[3]
    try:
        relative = provided.relative_to(root)
    except ValueError as exc:
        raise ValueError("prediction seal path differs") from exc
    parts = relative.parts
    if len(parts) != 4 or parts[0:2] != ("baseline", "v5.1") or parts[3] != "prediction_seal.json":
        raise ValueError("prediction seal path differs")
    ledger_id = "/".join(parts[:3])
    _identity(ledger_id)
    _, _, expected, _ = prediction_paths(spec, ledger_id)
    _same_descriptor_path(provided, expected, "prediction seal")
    return expected, ledger_id


def load_prediction_seal(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: BaselineReplayReceipt | None,
    path: Path | str,
    expected_sha256: str,
) -> PredictionSeal:
    """Securely load one Task8A final descriptor and all its bound authorities."""
    expected = _require_sha256(expected_sha256, "expected prediction seal sha256")
    seal_path, ledger_id = _seal_path_from_argument(spec, path)
    fold_id, gate, feature, feature_rows, card_digest = _authenticate_inputs(
        spec, source_gate, feature_seal, baseline_replay, ledger_id
    )
    raw = read_regular_bytes(seal_path, "prediction seal")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("prediction seal digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("prediction seal is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("prediction seal is not canonical JSON")
    _load_sidecar(seal_path.with_suffix(".json.sha256"), actual, "prediction seal sidecar")
    seal = _seal_from_payload(payload, actual, seal_path)
    if (
        seal.ledger_id != ledger_id
        or seal.fold_id != fold_id
        or seal.model_id != "v5.1"
        or seal.candidate_id is not None
        or seal.run_spec_sha256 != run_spec_sha256(spec)
        or seal.source_gate_sha256 != gate.artifact_sha256
        or seal.feature_seal_sha256 != feature.artifact_sha256
        or seal.prediction_contract_sha256 != spec.prediction_contract_sha256
        or seal.scoring_model_card_sha256 != card_digest
        or seal.frozen_model_card_sha256 is not None
    ):
        raise ValueError("prediction seal binding differs")
    # The feature rows are intentionally reread above; retain the local binding
    # so the authority cannot be optimized away from this loader.
    if not feature_rows:
        raise ValueError("prediction feature population differs")
    return seal


def load_prediction_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seal: FeatureSeal,
    baseline_replay: object,
    seal: PredictionSeal,
) -> tuple[PredictionRow, ...]:
    """The sole Task8A accessor for the sealed, label-free prediction JSONL."""
    if not isinstance(seal, PredictionSeal):
        raise ValueError("prediction rows require a PredictionSeal")
    fold_id = _identity(seal.ledger_id)
    if baseline_replay is not None:
        raise ValueError("Task8A baseline prediction does not accept baseline replay")
    _, _, seal_path, _ = prediction_paths(spec, seal.ledger_id)
    _same_descriptor_path(seal.path, seal_path, "prediction seal")
    loaded = load_prediction_seal(
        spec, source_gate, feature_seal, None, seal_path, seal.artifact_sha256
    )
    if (
        loaded.as_mapping() != seal.as_mapping()
        or loaded.artifact_sha256 != seal.artifact_sha256
        or loaded.path != seal.path
    ):
        raise ValueError("prediction seal descriptor differs")
    _, gate, feature, feature_rows, card_digest = _authenticate_inputs(
        spec, source_gate, feature_seal, None, seal.ledger_id
    )
    rows_path, rows_sidecar, _, _ = prediction_paths(spec, seal.ledger_id)
    _load_sidecar(rows_sidecar, loaded.rows_sha256, "prediction rows sidecar")
    rows = _load_rows(rows_path, loaded.rows_sha256)
    ordered = _validate_rows(spec, fold_id, feature_rows, rows, card_digest)
    if _rows_bytes(ordered) != _rows_bytes(rows) or _population_digest(rows) != loaded.population_sha256:
        raise ValueError("prediction row population differs")
    if loaded.source_gate_sha256 != gate.artifact_sha256 or loaded.feature_seal_sha256 != feature.artifact_sha256:
        raise ValueError("prediction row binding differs")
    return rows
