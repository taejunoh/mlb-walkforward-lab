"""Leakage and provenance primitives for the prepared-input boundary."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping

from .manifest import RawStore


def _date(value: object) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("event game_date must be an ISO date") from exc


def assert_pregame_history(
    game: Mapping[str, object], rows: Iterable[Mapping[str, object]]
) -> None:
    """Reject same-day, future, or target-game event provenance."""
    try:
        target = _date(game["official_date"])
        target_pk = game["game_pk"]
    except KeyError as exc:
        raise ValueError("target game requires official_date and game_pk") from exc
    if type(target_pk) is not int or target_pk <= 0:
        raise ValueError("target game_pk must be a positive integer")
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("history rows must be mappings")
        history_pk = row.get("game_pk")
        if type(history_pk) is not int or history_pk <= 0:
            raise ValueError("history game_pk must be a positive integer")
        if history_pk == target_pk:
            raise ValueError("feature history contains target game_pk")
        if _date(row.get("game_date")) >= target:
            raise ValueError(
                "feature history game_date must be strictly before official_date"
            )


def build_source_manifest_index(raw_root: str | Path) -> list[dict[str, object]]:
    """Verify and index canonical manifest queries and payload hashes."""
    rows = []
    for verified in RawStore(Path(raw_root)).verify_manifests():
        manifest = verified["manifest"]
        rows.append(
            {
                "source": manifest["source"],
                "query": manifest["query"],
                "sha256": manifest["sha256"],
                "row_count": manifest["row_count"],
                "manifest_sha256": verified["manifest_sha256"],
            }
        )
    rows.sort(
        key=lambda row: (
            row["source"],
            json.dumps(row["query"], sort_keys=True),
            row["sha256"],
        )
    )
    return rows


__all__ = ["assert_pregame_history", "build_source_manifest_index"]
