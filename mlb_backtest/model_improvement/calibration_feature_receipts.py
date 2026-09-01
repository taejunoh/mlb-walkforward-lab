"""Read-only parsing of the pre-RunSpec calibration feature authority."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Mapping

from .canonical import canonical_json_bytes, read_regular_bytes
from .contracts import (
    COMPONENTS,
    _require_date,
    _require_number,
    _require_sha256,
    _require_utc,
)
from .run_spec import ModelImprovementRunSpec


_RECEIPT_SCHEMA = "MODEL_IMPROVEMENT_CALIBRATION_FEATURE_RECEIPT_V1"
_ROW_SCHEMA = "MODEL_IMPROVEMENT_CALIBRATION_FEATURE_ROW_V1"
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "model_id",
        "lane_id",
        "fold_id",
        "calibration_key",
        "target_year",
        "training_start",
        "training_end",
        "source_authority_sha256",
        "source_inventory_sha256",
        "game_evidence_inventory_relative_path",
        "game_evidence_inventory_sha256",
        "feature_contract_sha256",
        "feature_code_revision",
        "feature_rows_path",
        "feature_rows_sha256",
        "feature_population_sha256",
    }
)
_ROW_FIELDS = frozenset(
    {
        "schema",
        "model_id",
        "lane_id",
        "fold_id",
        "calibration_key",
        "game_pk",
        "official_date",
        "scheduled_start_utc",
        "max_history_date",
        "target_year",
        "history_start_date",
        "history_end_date",
        "home_team_id",
        "away_team_id",
        "component_scores",
        "component_qualities",
        "source_feature_file_sha256",
        "source_feature_content_sha256",
        "source_authority_sha256",
        "feature_row_sha256",
    }
)


def _exact(value: object, fields: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} differs")
    return value


def _absolute(path: object, label: str) -> str:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\\" in path
        or path != os.path.normpath(path)
    ):
        raise ValueError(f"{label} path differs")
    return path


def _sidecar(path: Path, digest: str, label: str) -> None:
    raw = read_regular_bytes(path, label)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} differs") from exc
    if raw != canonical_json_bytes(payload) or payload != {"artifact_sha256": digest}:
        raise ValueError(f"{label} differs")


def _sides(value: object, label: str) -> Mapping[str, Mapping[str, float]]:
    if not isinstance(value, Mapping) or set(value) != {"home", "away"}:
        raise ValueError(f"{label} differs")
    result: dict[str, Mapping[str, float]] = {}
    for side in ("home", "away"):
        if not isinstance(value[side], Mapping) or set(value[side]) != set(COMPONENTS):
            raise ValueError(f"{label} differs")
        result[side] = {
            component: _require_number(value[side][component], label)
            for component in COMPONENTS
        }
    return result


@dataclass(frozen=True, slots=True)
class CalibrationFeatureRowV1:
    payload: Mapping[str, object]

    @classmethod
    def from_mapping(cls, value: object) -> "CalibrationFeatureRowV1":
        row = _exact(value, _ROW_FIELDS, "calibration feature row")
        if (
            row["schema"] != _ROW_SCHEMA
            or row["model_id"] != "v5.1"
            or row["lane_id"] != "baseline"
        ):
            raise ValueError("calibration feature row identity differs")
        game_pk = row["game_pk"]
        if type(game_pk) is not int or game_pk <= 0:
            raise ValueError("calibration feature row game differs")
        official = _require_date(row["official_date"], "official_date")
        if (
            row["max_history_date"]
            != (date.fromisoformat(official) - timedelta(days=1)).isoformat()
        ):
            raise ValueError("calibration feature row cutoff differs")
        _require_utc(row["scheduled_start_utc"], "scheduled_start_utc")
        for field in ("history_start_date", "history_end_date", "max_history_date"):
            _require_date(row[field], field)
        for field in (
            "source_feature_file_sha256",
            "source_feature_content_sha256",
            "source_authority_sha256",
            "feature_row_sha256",
        ):
            _require_sha256(row[field], field)
        for field in ("home_team_id", "away_team_id"):
            if type(row[field]) is not int or row[field] <= 0:
                raise ValueError("calibration feature row team differs")
        _sides(row["component_scores"], "component_scores")
        _sides(row["component_qualities"], "component_qualities")
        preimage = {key: row[key] for key in _ROW_FIELDS - {"feature_row_sha256"}}
        if (
            sha256(canonical_json_bytes(preimage)).hexdigest()
            != row["feature_row_sha256"]
        ):
            raise ValueError("calibration feature row digest differs")
        return cls(dict(row))

    def as_mapping(self) -> dict[str, object]:
        return dict(self.payload)


@dataclass(frozen=True, slots=True)
class CalibrationFeatureReceiptV1:
    payload: Mapping[str, object]
    artifact_sha256: str
    path: str
    rows: tuple[CalibrationFeatureRowV1, ...]

    @classmethod
    def from_bytes(
        cls, raw: bytes, path: Path, rows_raw: bytes
    ) -> "CalibrationFeatureReceiptV1":
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("calibration feature receipt is not JSON") from exc
        value = _exact(payload, _RECEIPT_FIELDS, "calibration feature receipt")
        if raw != canonical_json_bytes(value) or value["schema"] != _RECEIPT_SCHEMA:
            raise ValueError("calibration feature receipt differs")
        if value["model_id"] != "v5.1" or value["lane_id"] != "baseline":
            raise ValueError("calibration feature receipt identity differs")
        for field in (
            "source_authority_sha256",
            "source_inventory_sha256",
            "game_evidence_inventory_sha256",
            "feature_contract_sha256",
            "feature_rows_sha256",
            "feature_population_sha256",
        ):
            _require_sha256(value[field], field)
        if _absolute(value["feature_rows_path"], "feature rows") != str(
            path.with_name("calibration_feature_rows.jsonl")
        ):
            raise ValueError("calibration feature receipt rows path differs")
        if sha256(rows_raw).hexdigest() != value[
            "feature_rows_sha256"
        ] or not rows_raw.endswith(b"\n"):
            raise ValueError("calibration feature rows digest differs")
        rows = []
        previous = None
        for line in rows_raw.splitlines():
            try:
                item = json.loads(line)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("calibration feature row is not JSON") from exc
            if canonical_json_bytes(item).rstrip(b"\n") != line:
                raise ValueError("calibration feature row differs")
            row = CalibrationFeatureRowV1.from_mapping(item)
            key = (
                row.payload["official_date"],
                row.payload["scheduled_start_utc"],
                row.payload["game_pk"],
            )
            if previous is not None and key <= previous:
                raise ValueError("calibration feature rows are not ordered")
            previous = key
            rows.append(row)
        if (
            sha256(
                canonical_json_bytes([row.payload["game_pk"] for row in rows])
            ).hexdigest()
            != value["feature_population_sha256"]
        ):
            raise ValueError("calibration feature population differs")
        return cls(dict(value), sha256(raw).hexdigest(), str(path), tuple(rows))

    def as_mapping(self) -> dict[str, object]:
        return dict(self.payload)


def load_calibration_feature_receipt(
    spec: ModelImprovementRunSpec, fold_id: str
) -> CalibrationFeatureReceiptV1:
    """Load only the exact RunSpec-pinned, pre-RunSpec receipt and sibling."""
    if (
        not isinstance(spec, ModelImprovementRunSpec)
        or fold_id not in spec.probability_calibration_policy.fold_keys
    ):
        raise ValueError("calibration feature receipt fold differs")
    pin = spec.probability_calibration_policy.calibration_feature_receipts[
        spec.probability_calibration_policy.fold_keys[fold_id]
    ]
    path = Path(pin.path)
    raw = read_regular_bytes(path, "calibration feature receipt")
    if sha256(raw).hexdigest() != pin.sha256:
        raise ValueError("calibration feature receipt digest differs")
    _sidecar(
        path.with_suffix(path.suffix + ".sha256"),
        pin.sha256,
        "calibration feature receipt sidecar",
    )
    rows_path = path.with_name("calibration_feature_rows.jsonl")
    rows_raw = read_regular_bytes(rows_path, "calibration feature rows")
    receipt = CalibrationFeatureReceiptV1.from_bytes(raw, path, rows_raw)
    _sidecar(
        rows_path.with_suffix(rows_path.suffix + ".sha256"),
        receipt.payload["feature_rows_sha256"],
        "calibration feature rows sidecar",
    )
    if (receipt.payload["fold_id"], receipt.payload["calibration_key"]) != (
        fold_id,
        spec.probability_calibration_policy.fold_keys[fold_id],
    ):
        raise ValueError("calibration feature receipt identity differs")
    return receipt
