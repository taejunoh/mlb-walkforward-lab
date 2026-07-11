import json
import math
from pathlib import Path


COMPONENTS = {
    "starter",
    "offense",
    "bullpen",
    "environment",
    "defense",
    "schedule",
}
RANKING = [
    "predicted_winner_probability_desc",
    "confidence_desc",
    "absolute_starter_edge_desc",
    "scheduled_start_utc_asc",
    "gamePk_asc",
]


def load_config(path):
    config = json.loads(Path(path).read_text())
    weights = config.get("component_weights")

    if not isinstance(weights, dict) or set(weights) != COMPONENTS:
        raise ValueError("component set must match the frozen model card")
    if not math.isclose(math.fsum(weights.values()), 1.0, abs_tol=1e-12, rel_tol=0.0):
        raise ValueError("component weights must sum to 1")

    frozen_fields = {
        "model_version": "v5.1-BT",
        "protocol": "Backtest",
        "lock_minutes_before_first_pitch": 45,
        "training_window": {"start": "2022-01-01", "end": "2024-12-31"},
        "initial_window": {"start": "2025-05-01", "end": "2025-08-12"},
        "graded_target": 100,
        "probability_clip": [0.10, 0.90],
        "ranking": RANKING,
    }
    for field, expected in frozen_fields.items():
        if config.get(field) != expected:
            raise ValueError(f"{field} must match the frozen model card")

    return config
