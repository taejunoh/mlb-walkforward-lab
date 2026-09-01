"""Immutable artifact primitives for model improvement workflows."""

from .canonical import (
    canonical_json_bytes,
    read_regular_bytes,
    sha256_bytes,
    write_immutable_json,
)
from .contracts import PredictionSeal
from .oof_ledger import (
    load_prediction_rows,
    load_prediction_seal,
    seal_oof_predictions,
)

__all__ = [
    "canonical_json_bytes",
    "read_regular_bytes",
    "sha256_bytes",
    "write_immutable_json",
    "PredictionSeal",
    "seal_oof_predictions",
    "load_prediction_seal",
    "load_prediction_rows",
]
