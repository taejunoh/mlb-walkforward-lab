"""Leakage and provenance primitives for the prepared-input boundary."""

from __future__ import annotations

import hashlib
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
    if isinstance(target_pk, bool) or not isinstance(target_pk, int) or target_pk <= 0:
        raise ValueError("target game_pk must be a positive integer")
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("history rows must be mappings")
        if row.get("game_pk") == target_pk:
            raise ValueError("feature history contains target game_pk")
        if _date(row.get("game_date")) >= target:
            raise ValueError(
                "feature history game_date must be strictly before official_date"
            )


def build_source_manifest_index(raw_root: str | Path) -> list[dict[str, object]]:
    """Verify and index canonical manifest queries and payload hashes."""
    root = Path(raw_root)
    rows = []
    for manifest_path in sorted(root.glob("*/*.manifest.json")):
        content = manifest_path.read_bytes()
        manifest = RawStore._read_manifest(content)
        seal_path = manifest_path.with_name(manifest_path.name[:-5] + ".sha256")
        expected_seal = (hashlib.sha256(content).hexdigest() + "\n").encode("ascii")
        if seal_path.read_bytes() != expected_seal:
            raise RuntimeError("raw source manifest seal mismatch")
        payload = manifest_path.parent / f'{manifest["sha256"]}.raw'
        if hashlib.sha256(payload.read_bytes()).hexdigest() != manifest["sha256"]:
            raise RuntimeError("raw source payload digest mismatch")
        rows.append(
            {
                "source": manifest["source"],
                "query": manifest["query"],
                "sha256": manifest["sha256"],
                "row_count": manifest["row_count"],
                "manifest_sha256": hashlib.sha256(content).hexdigest(),
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
