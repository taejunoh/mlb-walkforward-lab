"""Immutable, result-free feature-row seals for model improvement."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_bytes, write_immutable_json
from .contracts import (
    SealedFeatureRow,
    _require_descriptor,
    _require_exact_mapping,
    _require_optional_sha256,
    _require_sequence,
    _require_sha256,
)
from .run_spec import ModelImprovementRunSpec, run_spec_sha256
from .source_gate import SourceGateReceipt


_SCHEMA = "MODEL_IMPROVEMENT_FEATURE_SEAL_V1"
_FEATURE_SETS = frozenset({"common", "lane_c"})
_FOLDS = frozenset({"H22", "H23", "H24"})
_FIELDS = frozenset({
    "schema", "feature_set_id", "fold_id", "run_spec_sha256",
    "source_gate_sha256", "population_sha256", "rows_sha256",
    "parent_feature_seal_sha256", "evidence_sha256",
})


@dataclass(frozen=True, slots=True)
class FeatureSeal:
    schema: str
    feature_set_id: str
    fold_id: str
    run_spec_sha256: str
    source_gate_sha256: str
    population_sha256: str
    rows_sha256: str
    parent_feature_seal_sha256: str | None
    evidence_sha256: tuple[str, ...]
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            raise ValueError("feature seal schema differs")
        if self.feature_set_id not in _FEATURE_SETS:
            raise ValueError("feature seal feature set differs")
        if self.fold_id not in _FOLDS:
            raise ValueError("feature seal fold differs")
        for field in (
            "run_spec_sha256", "source_gate_sha256", "population_sha256", "rows_sha256",
        ):
            object.__setattr__(self, field, _require_sha256(getattr(self, field), field))
        parent = _require_optional_sha256(
            self.parent_feature_seal_sha256, "parent_feature_seal_sha256"
        )
        evidence = tuple(
            _require_sha256(value, "evidence_sha256")
            for value in _require_sequence(self.evidence_sha256, "evidence_sha256")
        )
        if len(set(evidence)) != len(evidence):
            raise ValueError("feature seal evidence digests must be unique")
        if self.feature_set_id == "common":
            if parent is not None or evidence:
                raise ValueError("common feature seal has parent feature seal or evidence")
        elif parent is None or not evidence:
            raise ValueError("lane_c feature seal requires parent feature seal and evidence")
        artifact, path = _require_descriptor(self.artifact_sha256, self.path)
        object.__setattr__(self, "parent_feature_seal_sha256", parent)
        object.__setattr__(self, "evidence_sha256", evidence)
        object.__setattr__(self, "artifact_sha256", artifact)
        object.__setattr__(self, "path", path)

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "feature_set_id": self.feature_set_id,
            "fold_id": self.fold_id,
            "run_spec_sha256": self.run_spec_sha256,
            "source_gate_sha256": self.source_gate_sha256,
            "population_sha256": self.population_sha256,
            "rows_sha256": self.rows_sha256,
            "parent_feature_seal_sha256": self.parent_feature_seal_sha256,
            "evidence_sha256": list(self.evidence_sha256),
        }

    @classmethod
    def from_payload_mapping(
        cls, payload: Mapping[str, object], artifact_sha256: str, path: str
    ) -> "FeatureSeal":
        return cls(**dict(_require_exact_mapping(payload, _FIELDS, "feature seal payload")), artifact_sha256=artifact_sha256, path=path)


def _sidecar_payload(digest: str) -> dict[str, str]:
    return {"artifact_sha256": _require_sha256(digest, "artifact_sha256")}


def _load_canonical_sidecar(path: Path, expected: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if raw != canonical_json_bytes(payload) or payload != _sidecar_payload(expected):
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


def _preflight_pair(primary: Path, primary_bytes: bytes, sidecar: Path, label: str) -> None:
    existing_primary = _existing_regular_bytes(primary, label)
    existing_sidecar = _existing_regular_bytes(sidecar, f"{label} sidecar")
    digest = sha256(primary_bytes).hexdigest()
    if existing_primary is not None:
        if existing_primary != primary_bytes:
            raise ValueError(f"immutable artifact differs: {primary}")
        if existing_sidecar is None:
            raise ValueError(f"{label} primary requires sidecar")
        _load_canonical_sidecar(sidecar, digest, f"{label} sidecar")
    elif existing_sidecar is not None:
        _load_canonical_sidecar(sidecar, digest, f"{label} sidecar")


def _feature_path(spec: ModelImprovementRunSpec, feature_set_id: str, fold_id: str, name: str) -> Path:
    return spec.output_path("features", f"{feature_set_id}/{fold_id}/{name}")


def _row_key(row: SealedFeatureRow) -> tuple[str, str, int]:
    return row.official_date, row.scheduled_start_utc, row.game_pk


def _population_digest(rows: Sequence[SealedFeatureRow]) -> str:
    return sha256(canonical_json_bytes([
        {"game_pk": row.game_pk, "official_date": row.official_date, "target_year": row.target_year}
        for row in rows
    ])).hexdigest()


def _validate_gate(spec: ModelImprovementRunSpec, source_gate: SourceGateReceipt) -> None:
    if not isinstance(source_gate, SourceGateReceipt):
        raise ValueError("feature seal requires source gate receipt")
    if source_gate.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("source gate RunSpec binding differs")
    if source_gate.inventory_sha256 != spec.source_authority.inventory.sha256:
        raise ValueError("source gate inventory binding differs")


def _validate_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_set_id: str,
    fold_id: str,
    rows: Sequence[SealedFeatureRow],
) -> tuple[SealedFeatureRow, ...]:
    _validate_gate(spec, source_gate)
    if feature_set_id not in _FEATURE_SETS or fold_id not in _FOLDS:
        raise ValueError("feature set or fold differs")
    materialized = tuple(rows)
    if not materialized:
        raise ValueError("feature seal requires a nonempty population")
    if not all(isinstance(row, SealedFeatureRow) for row in materialized):
        raise ValueError("feature seal rows must be SealedFeatureRow values")
    if tuple(sorted(materialized, key=_row_key)) != materialized:
        raise ValueError("feature rows are not in strict order")
    if len({row.game_pk for row in materialized}) != len(materialized):
        raise ValueError("feature rows contain duplicate game_pk")
    population_keys = {(row.game_pk, row.official_date, row.target_year) for row in materialized}
    if len(population_keys) != len(materialized):
        raise ValueError("feature rows contain duplicate population key")
    expected_year = spec.fold(fold_id).target_year
    exclusion = sha256(canonical_json_bytes(spec.as_mapping()["exclusions"])).hexdigest()
    for row in materialized:
        if row.target_year != expected_year or row.target_year in spec.exclusions["excluded_years"]:
            raise ValueError("feature row target year differs")
        if row.official_date != row.scheduled_start_utc[:10]:
            raise ValueError("feature row scheduled date differs")
        if date.fromisoformat(row.max_history_date) >= date.fromisoformat(row.official_date):
            raise ValueError("feature row history cutoff differs")
        if row.source_gate_sha256 != source_gate.artifact_sha256:
            raise ValueError("feature row source gate differs")
        if row.source_manifest_sha256 != spec.source_authority.inventory.sha256:
            raise ValueError("feature row source inventory differs")
        if row.exclusion_policy_sha256 != exclusion:
            raise ValueError("feature row exclusion policy differs")
        if row.component_scores != row.baseline_component_scores or row.component_qualities != row.baseline_component_qualities:
            raise ValueError("feature row baseline component preservation differs")
    return materialized


def _rows_bytes(rows: Sequence[SealedFeatureRow]) -> bytes:
    return b"".join(canonical_json_bytes(row.as_mapping()) for row in rows)


def _load_rows_bytes(path: Path, expected: str) -> tuple[SealedFeatureRow, ...]:
    raw = read_regular_bytes(path, "feature rows")
    if sha256(raw).hexdigest() != expected:
        raise ValueError("feature rows digest differs")
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("feature rows are not canonical JSONL")
    rows: list[SealedFeatureRow] = []
    for line in raw.splitlines(keepends=True):
        if line == b"\n" or not line.endswith(b"\n"):
            raise ValueError("feature rows are not canonical JSONL")
        try:
            payload = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("feature rows are not JSONL") from exc
        if not isinstance(payload, Mapping) or canonical_json_bytes(payload) != line:
            raise ValueError("feature rows are not canonical JSONL")
        rows.append(SealedFeatureRow.from_payload_mapping(payload))
    return tuple(rows)


def _validate_path(seal: FeatureSeal, path: Path) -> None:
    parts = path.parts
    if len(parts) < 4 or tuple(parts[-4:]) != ("features", seal.feature_set_id, seal.fold_id, "feature_seal.json"):
        raise ValueError("feature seal path differs")


def load_feature_seal(path: Path, expected_sha256: str) -> FeatureSeal:
    """Structurally load a canonical seal and its mandatory sidecar."""
    expected = _require_sha256(expected_sha256, "expected_sha256")
    raw = read_regular_bytes(path, "feature seal")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("feature seal digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("feature seal is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("feature seal is not canonical JSON")
    _load_canonical_sidecar(path.with_suffix(path.suffix + ".sha256"), actual, "feature seal sidecar")
    seal = FeatureSeal.from_payload_mapping(payload, actual, str(path))
    _validate_path(seal, path)
    return seal


def seal_feature_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_set_id: str,
    fold_id: str,
    rows: Sequence[SealedFeatureRow],
    parent_feature_seal_sha256: str | None = None,
    evidence_sha256: Sequence[str] = (),
) -> FeatureSeal:
    """Seal one strictly ordered, result-free feature population."""
    materialized = _validate_rows(spec, source_gate, feature_set_id, fold_id, rows)
    evidence = tuple(evidence_sha256)
    if feature_set_id == "common":
        if parent_feature_seal_sha256 is not None or evidence:
            raise ValueError("common feature seal has parent feature seal or evidence")
    elif feature_set_id == "lane_c":
        if parent_feature_seal_sha256 is None or not evidence:
            raise ValueError("lane_c feature seal requires parent feature seal and evidence")
        parent = load_feature_seal(
            _feature_path(spec, "common", fold_id, "feature_seal.json"), parent_feature_seal_sha256
        )
        if (
            parent.feature_set_id != "common"
            or parent.fold_id != fold_id
            or parent.run_spec_sha256 != run_spec_sha256(spec)
            or parent.source_gate_sha256 != source_gate.artifact_sha256
        ):
            raise ValueError("lane_c parent feature seal differs")
    else:
        raise ValueError("feature set differs")
    rows_path = _feature_path(spec, feature_set_id, fold_id, "rows.jsonl")
    seal_path = _feature_path(spec, feature_set_id, fold_id, "feature_seal.json")
    rows_bytes = _rows_bytes(materialized)
    rows_digest = sha256(rows_bytes).hexdigest()
    provisional = FeatureSeal(
        _SCHEMA, feature_set_id, fold_id, run_spec_sha256(spec), source_gate.artifact_sha256,
        _population_digest(materialized), rows_digest, parent_feature_seal_sha256,
        evidence, "0" * 64, str(seal_path),
    )
    payload = provisional.as_mapping()
    payload_bytes = canonical_json_bytes(payload)
    seal_digest = sha256(payload_bytes).hexdigest()
    rows_sidecar = rows_path.with_suffix(rows_path.suffix + ".sha256")
    seal_sidecar = seal_path.with_suffix(seal_path.suffix + ".sha256")
    _preflight_pair(rows_path, rows_bytes, rows_sidecar, "feature rows")
    _preflight_pair(seal_path, payload_bytes, seal_sidecar, "feature seal")
    if (
        _existing_regular_bytes(seal_path, "feature seal") is not None
        and _existing_regular_bytes(rows_path, "feature rows") is None
    ):
        raise ValueError("feature seal primary requires rows primary")
    # Sidecars are explicit prerequisites; the seal primary is the commit point.
    write_immutable_json(rows_sidecar, _sidecar_payload(rows_digest))
    write_immutable_json(seal_sidecar, _sidecar_payload(seal_digest))
    write_immutable_bytes(rows_path, rows_bytes)
    write_immutable_json(seal_path, payload)
    seal = load_feature_seal(seal_path, seal_digest)
    if seal.as_mapping() != payload:
        raise ValueError("feature seal reread differs")
    loaded_rows = _load_rows_bytes(rows_path, rows_digest)
    _validate_rows(spec, source_gate, feature_set_id, fold_id, loaded_rows)
    if _rows_bytes(loaded_rows) != rows_bytes or _population_digest(loaded_rows) != seal.population_sha256:
        raise ValueError("feature rows reread differs")
    return seal


def load_sealed_feature_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    years: Sequence[int],
    seal: FeatureSeal,
) -> tuple[SealedFeatureRow, ...]:
    """Receipt-bound sole public reader for persisted feature rows."""
    if not isinstance(seal, FeatureSeal):
        raise ValueError("feature rows require a FeatureSeal")
    loaded_seal = load_feature_seal(Path(seal.path), seal.artifact_sha256)
    if loaded_seal != seal:
        raise ValueError("feature seal descriptor differs")
    if loaded_seal.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("feature seal RunSpec binding differs")
    if loaded_seal.source_gate_sha256 != source_gate.artifact_sha256:
        raise ValueError("feature seal source gate differs")
    requested = tuple(years)
    if not requested or any(type(year) is not int for year in requested):
        raise ValueError("feature row years differ")
    rows_path = Path(loaded_seal.path).with_name("rows.jsonl")
    _load_canonical_sidecar(rows_path.with_suffix(rows_path.suffix + ".sha256"), loaded_seal.rows_sha256, "feature rows sidecar")
    rows = _load_rows_bytes(rows_path, loaded_seal.rows_sha256)
    _validate_rows(spec, source_gate, loaded_seal.feature_set_id, loaded_seal.fold_id, rows)
    if tuple(sorted(set(requested))) != tuple(sorted({row.target_year for row in rows})):
        raise ValueError("feature row years differ")
    if _population_digest(rows) != loaded_seal.population_sha256:
        raise ValueError("feature row population differs")
    return rows
