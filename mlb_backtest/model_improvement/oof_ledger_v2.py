"""Pure V2 OOF identity and path contracts.

Publication and loading are intentionally introduced by the later Task8A
sealing stage.  This module admits no V1 artifact as executable input.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .run_spec import task8_target_year

if TYPE_CHECKING:
    from .run_spec import ModelImprovementRunSpec


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
