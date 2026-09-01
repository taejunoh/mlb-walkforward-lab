"""Pure immutable lane-disposition payload contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import (
    _require_descriptor,
    _require_exact_mapping,
    _require_optional_sha256,
    _require_sequence,
    _require_sha256,
    _require_text,
)


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
