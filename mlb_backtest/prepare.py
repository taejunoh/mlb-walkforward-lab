"""Leakage and provenance primitives for the prepared-input boundary."""

from __future__ import annotations

import json
import hashlib
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .manifest import RawStore


@dataclass(frozen=True)
class ParticipantBuild:
    games: tuple[dict[str, object], ...]
    provenance: dict[str, object]


@dataclass(frozen=True)
class ScheduleHistoryBuild:
    rows: tuple[dict[str, object], ...]
    coverage_start: str
    coverage_end: str
    coverage_complete: bool
    source_manifest_sha256: str | None
    expected_game_pks: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.coverage_complete) is not bool:
            raise ValueError("schedule coverage_complete must be boolean")
        first = _canonical_day(self.coverage_start, "schedule coverage_start")
        last = _canonical_day(self.coverage_end, "schedule coverage_end")
        if first > last:
            raise ValueError("schedule coverage_start must not be after coverage_end")
        if (
            not isinstance(self.expected_game_pks, tuple)
            or any(type(value) is not int or value <= 0 for value in self.expected_game_pks)
            or tuple(sorted(set(self.expected_game_pks))) != self.expected_game_pks
        ):
            raise ValueError("expected_game_pks must be sorted unique positive integers")
        actual = set()
        for row in self.rows:
            if not isinstance(row, Mapping):
                raise ValueError("schedule history rows must be mappings")
            game_pk = _positive_id(row.get("game_pk"), "schedule history game_pk")
            official = _canonical_day(
                row.get("official_date"), "schedule history official_date"
            )
            if not first <= official <= last:
                raise ValueError("schedule history row is outside coverage bounds")
            actual.add(game_pk)
        if actual != set(self.expected_game_pks):
            raise ValueError("schedule history rows do not match expected_game_pks")
        valid_hash = bool(
            isinstance(self.source_manifest_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.source_manifest_sha256)
        )
        if self.source_manifest_sha256 is not None and not valid_hash:
            raise ValueError("source manifest SHA must be lowercase SHA-256")
        if self.coverage_complete and not valid_hash:
            raise ValueError("complete schedule coverage requires a valid manifest SHA")


@dataclass(frozen=True)
class TeamCodeBuild:
    mapping: dict[str, str]
    unmatched_schedule_games: tuple[dict[str, object], ...]
    provenance: dict[str, object]


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


def _statcast_rows(statcast_data: object, *, plate_appearances: bool = False) -> tuple:
    from .statcast import StatcastData

    if isinstance(statcast_data, StatcastData):
        return (
            statcast_data.plate_appearances
            if plate_appearances
            else statcast_data.pitches
        )
    if isinstance(statcast_data, (str, bytes, Mapping)):
        raise ValueError("Statcast data must be parsed data or an iterable of rows")
    try:
        rows = tuple(statcast_data)
    except TypeError as exc:
        raise ValueError("Statcast data must be parsed data or an iterable of rows") from exc
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("Statcast rows must be mappings")
    return rows


def _positive_id(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _canonical_day(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    parsed = _date(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return value


def _game_codes(rows: Iterable[Mapping[str, object]]) -> tuple[str, str] | None:
    pairs = set()
    for row in rows:
        away = row.get("away_team")
        home = row.get("home_team")
        if not isinstance(away, str) or not away or not isinstance(home, str) or not home:
            raise ValueError("Statcast team codes must be nonempty strings")
        pairs.add((away, home))
    if not pairs:
        return None
    if len(pairs) != 1:
        raise ValueError("conflicting Statcast team codes for game_pk")
    return next(iter(pairs))


def build_actual_participants(
    schedule_games: Iterable[Mapping[str, object]], statcast_data: object
) -> ParticipantBuild:
    """Enrich schedules from stripped target identities without returning events."""
    from .feature_engine import actual_starter_index, enrich_schedule_game_participants

    games = tuple(schedule_games)
    if any(not isinstance(game, Mapping) for game in games):
        raise ValueError("schedule games must be mappings")
    games_by_pk = {}
    for game in games:
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in games_by_pk:
            raise ValueError("duplicate schedule game_pk")
        games_by_pk[game_pk] = game
    pitches = tuple(
        row
        for row in _statcast_rows(statcast_data)
        if _positive_id(row.get("game_pk"), "Statcast game_pk") in games_by_pk
    )
    pitches_by_game: defaultdict[int, list] = defaultdict(list)
    for row in pitches:
        game_pk = _positive_id(row.get("game_pk"), "Statcast game_pk")
        pitches_by_game[game_pk].append(row)
    starters = actual_starter_index(pitches)
    enriched_games = []
    stripped = []
    for game_pk, raw_game in sorted(games_by_pk.items()):
        official_date = _canonical_day(
            raw_game.get("official_date"), "schedule official_date"
        )
        if any(
            _canonical_day(row.get("game_date"), "Statcast game_date")
            != official_date
            for row in pitches_by_game.get(game_pk, ())
        ):
            raise ValueError("Statcast game_date does not match schedule official_date")
        enriched = enrich_schedule_game_participants(raw_game, starters)
        codes = _game_codes(pitches_by_game.get(game_pk, ()))
        away_code, home_code = codes if codes is not None else (None, None)
        enriched["away_team_code"] = away_code
        enriched["home_team_code"] = home_code
        enriched_games.append(enriched)
        stripped.append(
            {
                "game_pk": game_pk,
                "away_actual_starter_id": enriched["away_actual_starter_id"],
                "home_actual_starter_id": enriched["home_actual_starter_id"],
                "away_team_code": away_code,
                "home_team_code": home_code,
            }
        )
    encoded = json.dumps(
        stripped, allow_nan=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return ParticipantBuild(
        tuple(enriched_games),
        {
            "source": "baseball_savant_statcast",
            "participant_game_count": len(stripped),
            "participant_sha256": hashlib.sha256(encoded).hexdigest(),
        },
    )


def derive_team_code_map(
    schedule_games: Iterable[Mapping[str, object]],
    statcast_data: object,
    year: int,
) -> TeamCodeBuild:
    """Join schedule names to in-season Statcast codes with a strict 30-team bijection."""
    if type(year) is not int or year < 1900:
        raise ValueError("year must be an integer season")
    schedule_by_pk = {}
    for game in schedule_games:
        if not isinstance(game, Mapping):
            raise ValueError("schedule games must be mappings")
        official_date = _canonical_day(
            game.get("official_date"), "schedule official_date"
        )
        game_year = date.fromisoformat(official_date).year
        if game_year != year:
            continue
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in schedule_by_pk:
            raise ValueError("duplicate schedule game_pk")
        schedule_by_pk[game_pk] = game
    codes_by_pk: defaultdict[int, list] = defaultdict(list)
    statcast_game_pks = set()
    for row in _statcast_rows(statcast_data):
        game_pk = _positive_id(row.get("game_pk"), "Statcast game_pk")
        row_date = _canonical_day(row.get("game_date"), "Statcast game_date")
        if date.fromisoformat(row_date).year == year:
            statcast_game_pks.add(game_pk)
        if game_pk in schedule_by_pk:
            if row_date != schedule_by_pk[game_pk].get("official_date"):
                raise ValueError("Statcast game_date must match schedule season and date")
            codes_by_pk[game_pk].append(row)
    mapping = {}
    reverse = {}
    unmatched = []
    matched_game_count = 0
    for game_pk, game in sorted(schedule_by_pk.items()):
        codes = _game_codes(codes_by_pk.get(game_pk, ()))
        if codes is None:
            status = game.get("status")
            status_text = status if isinstance(status, str) else ""
            lowered = status_text.lower()
            has_result = any(
                game.get(field) is not None
                for field in (
                    "away_score",
                    "home_score",
                    "away_is_winner",
                    "home_is_winner",
                )
            )
            if lowered not in {"canceled", "cancelled", "postponed"} or has_result:
                label = status_text or "unknown status"
                raise ValueError(
                    f"unmatched {label} schedule game_pk {game_pk} is not proven unplayed"
                )
            unmatched.append(
                {
                    "game_pk": game_pk,
                    "official_date": game.get("official_date"),
                    "status": status_text,
                    "away_team": game.get("away_team"),
                    "home_team": game.get("home_team"),
                }
            )
            continue
        matched_game_count += 1
        for side, code in zip(("away", "home"), codes):
            name = game.get(f"{side}_team")
            if not isinstance(name, str) or not name:
                raise ValueError("schedule team names must be nonempty strings")
            if name in mapping and mapping[name] != code:
                raise ValueError(f"conflicting team code for {name}")
            if code in reverse and reverse[code] != name:
                raise ValueError(f"team code {code} maps to multiple names")
            mapping[name] = code
            reverse[code] = name
    if len(mapping) != 30 or len(reverse) != 30:
        raise ValueError("team code map must contain exactly 30 names and codes")
    frozen_mapping = dict(sorted(mapping.items()))
    mapping_bytes = json.dumps(
        frozen_mapping, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    provenance = {
        "source": "mlb_schedule+baseball_savant_statcast",
        "season": year,
        "schedule_game_count": len(schedule_by_pk),
        "statcast_game_count": len(statcast_game_pks),
        "matched_game_count": matched_game_count,
        "unmatched_game_count": len(unmatched),
        "unmatched_statcast_game_pks": sorted(statcast_game_pks - set(schedule_by_pk)),
        "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(),
    }
    return TeamCodeBuild(frozen_mapping, tuple(unmatched), provenance)


def derive_training_slot_rows(
    schedule_games: Iterable[Mapping[str, object]],
    statcast_plate_appearances: object,
) -> list[dict[str, object]]:
    """Count 2022-2024 PA by each target game's official starting lineup slot."""
    lineups = {}
    for game in schedule_games:
        if not isinstance(game, Mapping):
            raise ValueError("schedule games must be mappings")
        try:
            official_date = _canonical_day(
                game.get("official_date"), "schedule official_date"
            )
            season = date.fromisoformat(official_date).year
        except ValueError as exc:
            raise ValueError("schedule official_date must be an ISO date") from exc
        if season not in {2022, 2023, 2024}:
            continue
        if game.get("game_type") != "R":
            raise ValueError("training schedule games must be regular season")
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in lineups:
            raise ValueError("duplicate schedule game_pk")
        sides = {}
        for side in ("away", "home"):
            lineup = game.get(f"{side}_lineup")
            if isinstance(lineup, (str, bytes)) or not isinstance(lineup, Sequence) or len(lineup) != 9:
                raise ValueError("training games require official nine-player lineups")
            player_slots = {}
            for slot, player in enumerate(lineup, 1):
                if not isinstance(player, Mapping):
                    raise ValueError("lineup players must be mappings")
                player_id = _positive_id(player.get("player_id"), "lineup player_id")
                if player_id in player_slots:
                    raise ValueError("official lineup contains a duplicate player")
                player_slots[player_id] = slot
            sides[side] = player_slots
        lineups[game_pk] = (season, official_date, sides)

    totals: defaultdict[tuple[int, int], int] = defaultdict(int)
    seen_pa = set()
    rows = _statcast_rows(statcast_plate_appearances, plate_appearances=True)
    for row in rows:
        game_pk = _positive_id(row.get("game_pk"), "plate appearance game_pk")
        at_bat = row.get("at_bat_number")
        if type(at_bat) is not int or at_bat <= 0:
            raise ValueError("plate appearance at_bat_number must be positive")
        identity = (game_pk, at_bat)
        if identity in seen_pa:
            raise ValueError("duplicate plate appearance")
        seen_pa.add(identity)
        scheduled = lineups.get(game_pk)
        if scheduled is None:
            continue
        if _canonical_day(row.get("game_date"), "plate appearance game_date") != scheduled[1]:
            raise ValueError("plate appearance game_date does not match schedule date")
        if row.get("game_type", "R") != "R":
            raise ValueError("training plate appearances must be regular season")
        half = row.get("inning_topbot")
        if half not in {"Top", "Bot"}:
            raise ValueError("plate appearance inning_topbot must be Top or Bot")
        side = "away" if half == "Top" else "home"
        batter_id = _positive_id(row.get("batter_id"), "plate appearance batter_id")
        season, _official_date, sides = scheduled
        slot = sides[side].get(batter_id)
        if slot is not None:
            totals[(season, slot)] += 1
    expected = {
        (season, slot)
        for season in (2022, 2023, 2024)
        for slot in range(1, 10)
    }
    missing = sorted(expected - set(totals))
    if missing:
        raise ValueError(f"missing training slot cells: {missing}")
    return [
        {"season": season, "game_type": "R", "slot": slot, "pa": pa}
        for (season, slot), pa in sorted(totals.items())
    ]


def schedule_history_rows(
    normalized_games: Iterable[Mapping[str, object]],
    venue_reference: Mapping[int, Mapping[str, object]],
    *,
    coverage_start: str,
    coverage_end: str,
    source_manifest_sha256: str | None,
    expected_game_pks: Iterable[int],
) -> ScheduleHistoryBuild:
    """Convert normalized schedules into source-backed ScheduleHistory rows."""
    if not isinstance(venue_reference, Mapping):
        raise ValueError("venue_reference must be a mapping")
    first = _canonical_day(coverage_start, "schedule coverage_start")
    last = _canonical_day(coverage_end, "schedule coverage_end")
    if first > last:
        raise ValueError("schedule coverage_start must not be after coverage_end")
    if isinstance(expected_game_pks, (str, bytes)):
        raise ValueError("expected_game_pks must be an iterable of positive integers")
    try:
        expected = {_positive_id(value, "expected game_pk") for value in expected_game_pks}
    except TypeError as exc:
        raise ValueError("expected_game_pks must be iterable") from exc
    rows = []
    seen = set()
    for game in normalized_games:
        if not isinstance(game, Mapping):
            raise ValueError("normalized schedule games must be mappings")
        if game.get("game_type") != "R":
            continue
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in seen:
            raise ValueError("duplicate schedule game_pk")
        seen.add(game_pk)
        official_text = _canonical_day(
            game.get("official_date"), "schedule official_date"
        )
        if not first <= official_text <= last:
            raise ValueError("schedule row is outside explicit coverage bounds")
        official = date.fromisoformat(official_text)
        away_code, home_code = game.get("away_team_code"), game.get("home_team_code")
        if (
            not isinstance(away_code, str)
            or not away_code
            or not isinstance(home_code, str)
            or not home_code
            or away_code == home_code
        ):
            raise ValueError("schedule history requires distinct team codes")
        innings = game.get("scheduled_innings")
        if innings is None:
            innings = 9
        if type(innings) is not int or innings <= 0:
            raise ValueError("scheduled innings must be positive")
        venue_name = game.get("venue_name")
        if not isinstance(venue_name, str) or not venue_name:
            raise ValueError("schedule history requires venue_name")
        row = {
            "game_pk": game_pk,
            "official_date": official.isoformat(),
            "team_codes": [away_code, home_code],
            "home_team_code": home_code,
            "venue_name": venue_name,
            "innings": innings,
            "doubleheader_marker": game.get("doubleheader_marker"),
        }
        if row["doubleheader_marker"] not in {None, "GAME1", "GAME2"}:
            raise ValueError("doubleheader_marker must be GAME1, GAME2, or None")
        venue_id = game.get("venue_id")
        reference = venue_reference.get(venue_id, {})
        if not isinstance(reference, Mapping):
            raise ValueError("venue reference entries must be mappings")
        if "venue_name" in reference and reference["venue_name"] != venue_name:
            raise ValueError("venue reference name conflicts with schedule")
        for field in (
            "venue_city",
            "venue_latitude",
            "venue_longitude",
            "time_zone",
        ):
            if field in reference:
                row[field] = reference[field]
        for field in ("venue_city", "time_zone"):
            if field in row and (not isinstance(row[field], str) or not row[field]):
                raise ValueError(f"venue reference {field} must be a nonempty string")
        for field, low, high in (
            ("venue_latitude", -90.0, 90.0),
            ("venue_longitude", -180.0, 180.0),
        ):
            if field in row:
                value = row[field]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not low <= float(value) <= high
                ):
                    raise ValueError(f"venue reference {field} is invalid")
        rows.append(row)
    if not rows:
        raise ValueError("schedule history requires at least one regular-season game")
    rows.sort(key=lambda row: (row["official_date"], row["game_pk"]))
    actual = {int(row["game_pk"]) for row in rows}
    if actual != expected:
        raise ValueError(
            f"expected game_pk set differs; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    valid_source_hash = bool(
        isinstance(source_manifest_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", source_manifest_sha256)
    )
    return ScheduleHistoryBuild(
        tuple(rows),
        first,
        last,
        valid_source_hash,
        source_manifest_sha256,
        tuple(sorted(expected)),
    )


def build_pregame_history(
    statcast_data: object,
    target_game: Mapping[str, object],
    slot_weights: Mapping[int, float],
    schedule_build: ScheduleHistoryBuild,
):
    """Construct FeatureHistory only after removing target/same-day/future events."""
    from .feature_engine import FeatureHistory
    from .statcast import StatcastData

    if not isinstance(target_game, Mapping):
        raise ValueError("target_game must be a mapping")
    target_pk = _positive_id(target_game.get("game_pk"), "target game_pk")
    target_date = _canonical_day(
        target_game.get("official_date"), "target official_date"
    )
    if not isinstance(schedule_build, ScheduleHistoryBuild):
        raise ValueError("schedule_build must be a ScheduleHistoryBuild")
    if not isinstance(statcast_data, StatcastData):
        raise ValueError("statcast_data must be StatcastData")

    def eligible(row: Mapping[str, object]) -> bool:
        row_pk = _positive_id(row.get("game_pk"), "Statcast game_pk")
        row_date = _canonical_day(row.get("game_date"), "Statcast game_date")
        return row_date < target_date and row_pk != target_pk

    filtered = StatcastData(
        tuple(row for row in statcast_data.pitches if eligible(row)),
        tuple(row for row in statcast_data.plate_appearances if eligible(row)),
    )
    venue_by_name = {}
    for row in schedule_build.rows:
        name = row.get("venue_name")
        if isinstance(name, str) and name:
            venue_by_name[name] = {
                field: row[field]
                for field in (
                    "venue_city",
                    "venue_latitude",
                    "venue_longitude",
                    "time_zone",
                )
                if field in row
            }
    history = FeatureHistory(
        filtered,
        slot_weights=slot_weights,
        schedule_history=schedule_build.rows,
        schedule_coverage_start=schedule_build.coverage_start,
        schedule_coverage_end=schedule_build.coverage_end,
        schedule_coverage_complete=schedule_build.coverage_complete,
        venue_by_name=venue_by_name,
    )
    assert_pregame_history(target_game, history.pitches)
    assert_pregame_history(target_game, history.plate_appearances)
    return history


__all__ = [
    "ParticipantBuild",
    "ScheduleHistoryBuild",
    "TeamCodeBuild",
    "assert_pregame_history",
    "build_actual_participants",
    "build_source_manifest_index",
    "build_pregame_history",
    "derive_team_code_map",
    "derive_training_slot_rows",
    "schedule_history_rows",
]
