"""Leakage-safe prior-schedule projections from verified game evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, timedelta

from ..v52_source_contracts import canonical_sha256


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def recompute_prior_history_sidecar(
    schedule_sidecar: Mapping[str, object],
) -> Mapping[str, object]:
    if not isinstance(schedule_sidecar, Mapping):
        raise ValueError("schedule_sidecar must be a mapping")
    if schedule_sidecar.get("grain") != "season_game_evidence_snapshot":
        raise ValueError("prior history requires a season game-evidence snapshot")
    raw_games = schedule_sidecar.get("games")
    if not isinstance(raw_games, list):
        raise ValueError("game-evidence snapshot games must be a list")
    games = []
    source_hashes = []
    seen: set[int] = set()
    for raw in raw_games:
        if not isinstance(raw, Mapping):
            raise ValueError("game evidence rows must be mappings")
        game_pk = _positive(raw.get("game_pk"), "history game_pk")
        if game_pk in seen:
            raise ValueError("prior history contains duplicate gamePk")
        seen.add(game_pk)
        if raw.get("verification_status") not in {
            "VERIFIED",
            "INELIGIBLE_UNPLAYED",
        }:
            raise ValueError("prior history requires verified source games")
        official = _day(raw.get("official_date"), "history official date")
        cutoff = _day(raw.get("feature_history_cutoff"), "history cutoff")
        if cutoff != official - timedelta(days=1):
            raise ValueError("every feature history cutoff must equal D-1")
        source_hash = raw.get("source_game_sha256")
        if not isinstance(source_hash, str) or not _SHA256.fullmatch(source_hash):
            raise ValueError("every projected row must bind a source game hash")
        projection = {
            "away_team_id": _positive(raw.get("away_team_id"), "away team"),
            "completed_innings": raw.get("completed_innings"),
            "doubleheader_order": _positive(
                raw.get("doubleheader_order"), "doubleheader order"
            ),
            "game_pk": game_pk,
            "history_cutoff": cutoff.isoformat(),
            "home_team_id": _positive(raw.get("home_team_id"), "home team"),
            "official_date": official.isoformat(),
            "scheduled_innings": raw.get("scheduled_innings"),
            "scheduled_start_utc": raw.get("scheduled_start_utc"),
            "source_game_sha256": source_hash,
            "terminal_status": raw.get("terminal_status"),
            "venue_id": _positive(raw.get("venue_id"), "venue id"),
        }
        for field in ("completed_innings", "scheduled_innings"):
            value = projection[field]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"history {field} must be a nonnegative integer or null")
        if not isinstance(projection["scheduled_start_utc"], str) or not projection[
            "scheduled_start_utc"
        ]:
            raise ValueError("history scheduled_start_utc must be nonempty text")
        if not isinstance(projection["terminal_status"], str) or not projection[
            "terminal_status"
        ]:
            raise ValueError("history terminal_status must be nonempty text")
        projection["projection_sha256"] = canonical_sha256(projection)
        games.append(projection)
        source_hashes.append(source_hash)
    games.sort(key=lambda row: (row["official_date"], row["game_pk"]))
    return {
        "games": games,
        "grain": "verified_game_projection",
        "schema_version": 1,
        "source_game_sha256s": sorted(source_hashes),
        "source_snapshot_sha256": canonical_sha256(dict(schedule_sidecar)),
    }


__all__ = ["recompute_prior_history_sidecar"]
