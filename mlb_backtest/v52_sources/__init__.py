"""Grain-specific v5.2 semantic recomputation with lazy public re-exports."""

from importlib import import_module


_EXPORTS = {
    "recompute_career_bf_snapshot": ".pitching",
    "recompute_career_bf_season_snapshot": ".pitching",
    "recompute_career_bf_sidecar": ".pitching",
    "recompute_game_evidence_snapshot": ".schedule",
    "recompute_pitching_season_snapshot": ".pitching",
    "recompute_pitching_sidecar": ".pitching",
    "recompute_prior_history_sidecar": ".history",
    "recompute_reference_sidecar": ".references",
    "recompute_schedule_anchor_sidecar": ".schedule",
    "recompute_statcast_season_snapshot": ".statcast",
    "recompute_statcast_sidecar": ".statcast",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
