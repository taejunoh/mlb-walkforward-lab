"""Pure immutable lane-disposition payload contract."""

from __future__ import annotations

from dataclasses import dataclass
import json
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping, Sequence

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_json

from .contracts import (
    _require_descriptor,
    _require_exact_mapping,
    _require_optional_sha256,
    _require_sequence,
    _require_sha256,
    _require_text,
)

if TYPE_CHECKING:
    from .run_spec import ModelImprovementRunSpec


LANE_IDS = frozenset({"A", "B", "C"})
LANE_DISPOSITION_STATES = frozenset({"QUALIFIED", "ABSENT", "REJECTED"})


@dataclass(frozen=True, slots=True)
class LaneDisposition:
    lane_id: str
    state: str
    run_spec_sha256: str
    parent_card_sha256: str | None
    evidence_sha256: tuple[str, ...]
    reason_codes: tuple[str, ...]
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        if self.lane_id not in LANE_IDS or self.state not in LANE_DISPOSITION_STATES:
            raise ValueError("unknown lane disposition")
        object.__setattr__(
            self,
            "run_spec_sha256",
            _require_sha256(self.run_spec_sha256, "run_spec_sha256"),
        )
        object.__setattr__(
            self,
            "parent_card_sha256",
            _require_optional_sha256(self.parent_card_sha256, "parent_card_sha256"),
        )
        evidence = tuple(
            _require_sha256(value, "evidence_sha256")
            for value in _require_sequence(self.evidence_sha256, "evidence_sha256")
        )
        reasons = tuple(
            _require_text(value, "reason_code")
            for value in _require_sequence(self.reason_codes, "reason_codes")
        )
        if not evidence or not reasons:
            raise ValueError("lane disposition requires evidence and reason codes")
        object.__setattr__(self, "evidence_sha256", evidence)
        object.__setattr__(self, "reason_codes", reasons)
        artifact_sha256, path = _require_descriptor(self.artifact_sha256, self.path)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "path", path)

    def as_mapping(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "state": self.state,
            "run_spec_sha256": self.run_spec_sha256,
            "parent_card_sha256": self.parent_card_sha256,
            "evidence_sha256": list(self.evidence_sha256),
            "reason_codes": list(self.reason_codes),
        }

    @classmethod
    def from_payload_mapping(
        cls, payload: Mapping[str, object], artifact_sha256: str, path: str
    ) -> "LaneDisposition":
        fields = frozenset(
            {
                "lane_id",
                "state",
                "run_spec_sha256",
                "parent_card_sha256",
                "evidence_sha256",
                "reason_codes",
            }
        )
        return cls(
            **dict(_require_exact_mapping(payload, fields, "lane disposition payload")),
            artifact_sha256=artifact_sha256,
            path=path,
        )


def _disposition_path(spec: "ModelImprovementRunSpec", lane_id: str) -> Path:
    return spec.output_path(f"lane_{lane_id.lower()}", "lane_disposition.json")


def seal_lane_disposition(
    spec: "ModelImprovementRunSpec",
    lane_id: Literal["A", "B", "C"],
    state: Literal["QUALIFIED", "ABSENT", "REJECTED"],
    parent_card_sha256: str | None,
    evidence_sha256: Sequence[str],
    reason_codes: Sequence[str],
) -> LaneDisposition:
    """Persist one lane disposition under its RunSpec-owned namespace."""
    from .run_spec import run_spec_sha256

    if lane_id not in LANE_IDS or state not in LANE_DISPOSITION_STATES:
        raise ValueError("unknown lane disposition")
    path = _disposition_path(spec, lane_id)
    provisional = LaneDisposition(lane_id, state, run_spec_sha256(spec), parent_card_sha256, tuple(evidence_sha256), tuple(reason_codes), "0" * 64, str(path))
    payload = provisional.as_mapping()
    write_immutable_json(path, payload)
    digest = sha256(canonical_json_bytes(payload)).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    write_immutable_json(sidecar, {"artifact_sha256": digest})
    return load_lane_disposition(spec, path, digest)


def load_lane_disposition(
    spec: "ModelImprovementRunSpec", path: Path, expected_sha256: str,
) -> LaneDisposition:
    """Securely load a canonical, RunSpec-bound disposition and its sidecar."""
    from .run_spec import run_spec_sha256

    expected = _require_sha256(expected_sha256, "expected_sha256")
    raw_bytes = read_regular_bytes(path, "lane disposition")
    digest = sha256(raw_bytes).hexdigest()
    if digest != expected:
        raise ValueError("lane disposition digest differs")
    try:
        payload = json.loads(raw_bytes)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("lane disposition is not JSON") from exc
    if raw_bytes != canonical_json_bytes(payload):
        raise ValueError("lane disposition is not canonical JSON")
    if not isinstance(payload, Mapping):
        raise ValueError("lane disposition payload must be an object")
    disposition = LaneDisposition.from_payload_mapping(payload, digest, str(path))
    if disposition.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("lane disposition RunSpec binding differs")
    if path.resolve() != _disposition_path(spec, disposition.lane_id).resolve():
        raise ValueError("lane disposition path differs")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar_bytes = read_regular_bytes(sidecar, "lane disposition sidecar")
    try:
        sidecar_payload = json.loads(sidecar_bytes)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("lane disposition sidecar is not JSON") from exc
    if sidecar_bytes != canonical_json_bytes(sidecar_payload) or not isinstance(sidecar_payload, Mapping) or set(sidecar_payload) != {"artifact_sha256"} or sidecar_payload["artifact_sha256"] != digest:
        raise ValueError("lane disposition sidecar differs")
    return disposition
