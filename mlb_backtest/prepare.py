"""Leakage and provenance primitives for the prepared-input boundary."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field as dataclass_field
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .manifest import RawStore, _safe_leaf_bytes
from .sources import parse_schedule, schedule_url


_EVIDENCE_TOKEN = object()
PLAYED_STATUSES = frozenset({"Final", "Completed Early"})


@dataclass(frozen=True, slots=True)
class PreparationValidationPolicy:
    """Production source-completeness thresholds, injectable for tiny replays."""

    minimum_played_games: int = 2400
    expected_team_count: int = 30
    minimum_lineup_games: int = 2400
    minimum_starting_lineup_pa: int = 40_000

    def __post_init__(self) -> None:
        for field_name in (
            "minimum_played_games",
            "expected_team_count",
            "minimum_lineup_games",
            "minimum_starting_lineup_pa",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


PRODUCTION_VALIDATION_POLICY = PreparationValidationPolicy()


@dataclass(frozen=True, init=False)
class VerifiedScheduleEvidence:
    raw_root: Path
    manifest_path: Path
    manifest_sha256: str
    payload_sha256: str
    query: Mapping[str, object]
    coverage_start: str
    coverage_end: str
    game_pks: tuple[int, ...]
    games: tuple[dict[str, object], ...]
    normalized_games_sha256: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise TypeError("VerifiedScheduleEvidence is created only by its loader")

    @classmethod
    def _from_verified(cls, token: object, **values):
        if token is not _EVIDENCE_TOKEN:
            raise TypeError("verified schedule evidence requires loader authority")
        instance = object.__new__(cls)
        for field, value in values.items():
            object.__setattr__(instance, field, value)
        return instance


@dataclass(frozen=True, init=False)
class VerifiedStatcastEvidence:
    raw_root: Path
    manifest_paths: tuple[Path, ...]
    manifest_sha256s: tuple[str, ...]
    payload_sha256s: tuple[str, ...]
    coverage_ranges: tuple[tuple[str, str], ...]
    plate_appearances: tuple["SlotPA", ...]
    plate_appearance_count: int
    normalized_statcast_sha256: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise TypeError("VerifiedStatcastEvidence is created only by its loader")

    @classmethod
    def _from_verified(cls, token: object, **values):
        if token is not _EVIDENCE_TOKEN:
            raise TypeError("verified Statcast evidence requires loader authority")
        instance = object.__new__(cls)
        for field, value in values.items():
            object.__setattr__(instance, field, value)
        return instance


@dataclass(frozen=True, slots=True)
class SlotPA:
    game_pk: int
    game_date: str
    at_bat_number: int
    batter_id: int
    inning_topbot: str
    game_type: str


def _slot_pa_digest(rows: Iterable[SlotPA]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            {
                "at_bat_number": row.at_bat_number,
                "batter_id": row.batter_id,
                "game_date": row.game_date,
                "game_pk": row.game_pk,
                "game_type": row.game_type,
                "inning_topbot": row.inning_topbot,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


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
    provenance: Mapping[str, object] = dataclass_field(default_factory=dict)

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


def _safe_verified_bytes(path: Path, label: str) -> bytes:
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError(f"verified {label} parent must be a real directory")
    return _safe_leaf_bytes(path, parent.resolve(strict=True), label)


def _canonical_games_bytes(games: Iterable[Mapping[str, object]]) -> bytes:
    return json.dumps(
        list(games),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_verified_schedule_evidence(
    raw_root: str | Path,
    manifest_path: str | Path,
    *,
    verified_catalog=None,
) -> VerifiedScheduleEvidence:
    """Bind one canonical schedule query to its verified payload and normalized games."""
    requested = Path(manifest_path)
    if requested.is_symlink():
        raise ValueError("schedule manifest path cannot be a symlink")
    requested_absolute = requested.absolute()
    catalog = (
        RawStore(Path(raw_root)).verify_manifests()
        if verified_catalog is None
        else verified_catalog
    )
    matches = [
        verified
        for verified in catalog
        if Path(verified["manifest_path"]).absolute() == requested_absolute
    ]
    if len(matches) != 1:
        raise ValueError("manifest_path must identify one verified RawStore manifest")
    verified = matches[0]
    manifest = verified["manifest"]
    if manifest.get("source") != "mlb_schedule":
        raise ValueError("verified schedule evidence requires mlb_schedule source")
    query = manifest.get("query")
    if not isinstance(query, dict):
        raise ValueError("verified schedule query must be an object")
    base_query_fields = {"start", "end", "gameType", "url"}
    if set(query) != base_query_fields | {"role"}:
        raise ValueError("verified schedule query fields are not canonical")
    if query["role"] not in {"training", "history", "backtest"}:
        raise ValueError("verified schedule query role is invalid")
    start = _canonical_day(query.get("start"), "schedule query start")
    end = _canonical_day(query.get("end"), "schedule query end")
    if start > end or query.get("gameType") != "R":
        raise ValueError("verified schedule query must be an ordered regular-season range")
    if query.get("url") != schedule_url(start, end):
        raise ValueError("verified schedule query URL is not canonical")
    payload_path = Path(verified["payload_path"])
    payload = _safe_verified_bytes(payload_path, "raw")
    if hashlib.sha256(payload).hexdigest() != manifest.get("sha256"):
        raise RuntimeError("verified schedule payload digest changed")
    games = tuple(parse_schedule(payload))
    schedule_payload = json.loads(payload.decode("utf-8"))
    for index, date_entry in enumerate(schedule_payload["dates"]):
        query_date = _canonical_day(
            date_entry.get("date"), f"schedule payload dates[{index}].date"
        )
        if not start <= query_date <= end:
            raise ValueError("verified schedule payload contains a game outside its query")
    game_pks = tuple(sorted(_positive_id(game.get("game_pk"), "schedule game_pk") for game in games))
    if len(set(game_pks)) != len(game_pks):
        raise ValueError("verified schedule payload contains duplicate game_pk")
    canonical = _canonical_games_bytes(games)
    return VerifiedScheduleEvidence._from_verified(
        _EVIDENCE_TOKEN,
        raw_root=Path(raw_root).absolute(),
        manifest_path=Path(verified["manifest_path"]),
        manifest_sha256=str(verified["manifest_sha256"]),
        payload_sha256=str(manifest["sha256"]),
        query=MappingProxyType(dict(query)),
        coverage_start=start,
        coverage_end=end,
        game_pks=game_pks,
        games=tuple(dict(game) for game in games),
        normalized_games_sha256=hashlib.sha256(canonical).hexdigest(),
    )


def load_verified_statcast_evidence(
    raw_root: str | Path,
    manifest_paths: Iterable[str | Path],
    *,
    verified_catalog=None,
) -> VerifiedStatcastEvidence:
    """Bind verified Statcast manifests to one immutable normalized snapshot."""
    from .statcast import iter_statcast_pitches, statcast_url

    if isinstance(manifest_paths, (str, bytes, Path)):
        raise ValueError("manifest_paths must be an iterable of manifest paths")
    requested = tuple(Path(path) for path in manifest_paths)
    if not requested or len(set(map(str, requested))) != len(requested):
        raise ValueError("manifest_paths must be a nonempty unique collection")
    if any(path.is_symlink() for path in requested):
        raise ValueError("Statcast manifest paths cannot be symlinks")
    requested_by_absolute = {path.absolute(): path for path in requested}
    catalog = (
        RawStore(Path(raw_root)).verify_manifests()
        if verified_catalog is None
        else verified_catalog
    )
    selected = [
        verified
        for verified in catalog
        if Path(verified["manifest_path"]).absolute() in requested_by_absolute
    ]
    if len(selected) != len(requested):
        raise ValueError("every Statcast manifest path must be verified by RawStore")
    ranges = []
    plate_appearances_by_key: dict[tuple[int, int], SlotPA] = {}
    manifest_shas = []
    payload_shas = []
    verified_paths = []
    for verified in sorted(
        selected, key=lambda item: str(item["manifest"]["query"].get("start", ""))
    ):
        manifest = verified["manifest"]
        if manifest.get("source") != "baseball_savant_statcast":
            raise ValueError("verified Statcast evidence has the wrong source")
        query = manifest.get("query")
        if not isinstance(query, dict):
            raise ValueError("verified Statcast query must be an object")
        if set(query) != {"start", "end", "game_type", "url"}:
            raise ValueError("verified Statcast query fields are not canonical")
        start = _canonical_day(query.get("start"), "Statcast query start")
        end = _canonical_day(query.get("end"), "Statcast query end")
        if start > end or query.get("game_type") != "R":
            raise ValueError("verified Statcast query must be an ordered regular-season range")
        if query.get("url") != statcast_url(start, end):
            raise ValueError("verified Statcast query URL is not canonical")
        payload = _safe_verified_bytes(Path(verified["payload_path"]), "raw")
        if hashlib.sha256(payload).hexdigest() != manifest.get("sha256"):
            raise RuntimeError("verified Statcast payload digest changed")
        manifest_terminal_hashes = {}
        for row in iter_statcast_pitches(payload):
            game_date = str(row["game_date"])
            if not start <= game_date <= end:
                raise ValueError("verified Statcast row is outside its query range")
            if not row["events"]:
                continue
            compact = SlotPA(
                game_pk=int(row["game_pk"]),
                game_date=game_date,
                at_bat_number=int(row["at_bat_number"]),
                batter_id=int(row["batter_id"]),
                inning_topbot=str(row["inning_topbot"]),
                game_type=str(row["game_type"]),
            )
            key = (compact.game_pk, compact.at_bat_number)
            terminal_hash = hashlib.sha256(
                json.dumps(
                    row,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            prior_terminal_hash = manifest_terminal_hashes.get(key)
            if prior_terminal_hash is not None:
                if prior_terminal_hash != terminal_hash:
                    raise ValueError(
                        "multiple terminal events for plate appearance "
                        f"{key[0]}:{key[1]}"
                    )
                continue
            prior = plate_appearances_by_key.get(key)
            if prior is not None:
                raise ValueError("verified Statcast manifests overlap or duplicate events")
            plate_appearances_by_key[key] = compact
            manifest_terminal_hashes[key] = terminal_hash
        ranges.append((start, end))
        verified_paths.append(Path(verified["manifest_path"]))
        manifest_shas.append(str(verified["manifest_sha256"]))
        payload_shas.append(str(manifest["sha256"]))
    plate_appearances = sorted(
        plate_appearances_by_key.values(),
        key=lambda row: (row.game_date, row.game_pk, row.at_bat_number)
    )
    normalized_digest = _slot_pa_digest(plate_appearances)
    return VerifiedStatcastEvidence._from_verified(
        _EVIDENCE_TOKEN,
        raw_root=Path(raw_root).absolute(),
        manifest_paths=tuple(verified_paths),
        manifest_sha256s=tuple(manifest_shas),
        payload_sha256s=tuple(payload_shas),
        coverage_ranges=tuple(sorted(ranges)),
        plate_appearances=tuple(plate_appearances),
        plate_appearance_count=len(plate_appearances),
        normalized_statcast_sha256=normalized_digest,
    )


def _rebind_schedule_evidence(
    evidence: VerifiedScheduleEvidence,
    verified_catalog=None,
) -> VerifiedScheduleEvidence:
    if not isinstance(evidence, VerifiedScheduleEvidence):
        raise ValueError("verified schedule evidence is required")
    fresh = load_verified_schedule_evidence(
        evidence.raw_root,
        evidence.manifest_path,
        verified_catalog=verified_catalog,
    )
    identity = (
        "manifest_sha256",
        "payload_sha256",
        "coverage_start",
        "coverage_end",
        "game_pks",
        "normalized_games_sha256",
    )
    if any(getattr(evidence, field, None) != getattr(fresh, field) for field in identity):
        raise ValueError("verified schedule evidence no longer matches RawStore")
    return fresh


def _rebind_statcast_evidence(
    evidence: VerifiedStatcastEvidence,
    verified_catalog=None,
) -> VerifiedStatcastEvidence:
    if not isinstance(evidence, VerifiedStatcastEvidence):
        raise ValueError("verified Statcast evidence is required")
    requested = {path.absolute() for path in evidence.manifest_paths}
    catalog = (
        RawStore(evidence.raw_root).verify_manifests()
        if verified_catalog is None
        else verified_catalog
    )
    selected = [
        verified
        for verified in catalog
        if Path(verified["manifest_path"]).absolute() in requested
    ]
    if len(selected) != len(requested):
        raise ValueError("verified Statcast evidence manifests are missing")
    selected.sort(key=lambda item: str(item["manifest"]["query"].get("start", "")))
    manifest_shas = tuple(str(item["manifest_sha256"]) for item in selected)
    payload_shas = tuple(str(item["manifest"]["sha256"]) for item in selected)
    ranges = tuple(
        (str(item["manifest"]["query"].get("start")), str(item["manifest"]["query"].get("end")))
        for item in selected
    )
    if (
        manifest_shas != evidence.manifest_sha256s
        or payload_shas != evidence.payload_sha256s
        or ranges != evidence.coverage_ranges
        or evidence.plate_appearance_count != len(evidence.plate_appearances)
        or _slot_pa_digest(evidence.plate_appearances)
        != evidence.normalized_statcast_sha256
    ):
        raise ValueError("verified Statcast evidence no longer matches RawStore")
    return evidence


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

    participant_fields = {
        "game_pk",
        "official_date",
        "game_type",
        "scheduled_start_utc",
        "away_team",
        "away_team_id",
        "home_team",
        "home_team_id",
        "away_probable_pitcher_id",
        "away_probable_pitcher_name",
        "home_probable_pitcher_id",
        "home_probable_pitcher_name",
        "venue_id",
        "venue_name",
        "scheduled_innings",
        "doubleheader_marker",
        "away_lineup",
        "home_lineup",
    }
    games = []
    for game in schedule_games:
        if not isinstance(game, Mapping):
            games.append(game)
            continue
        projected = {
            key: value for key, value in game.items() if key in participant_fields
        }
        for field in ("away_lineup", "home_lineup"):
            lineup = projected.get(field)
            if lineup is None:
                continue
            if isinstance(lineup, (str, bytes)) or not isinstance(lineup, Sequence):
                raise ValueError("schedule lineups must be sequences")
            if any(not isinstance(player, Mapping) for player in lineup):
                raise ValueError("schedule lineup players must be mappings")
            projected[field] = [
                {
                    key: value
                    for key, value in player.items()
                    if key in {"player_id", "player_name"}
                }
                for player in lineup
            ]
        games.append(projected)
    games = tuple(games)
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
    *,
    validation_policy: PreparationValidationPolicy = PRODUCTION_VALIDATION_POLICY,
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
    if not isinstance(validation_policy, PreparationValidationPolicy):
        raise ValueError("validation_policy must be a PreparationValidationPolicy")
    expected_teams = validation_policy.expected_team_count
    if len(mapping) != expected_teams or len(reverse) != expected_teams:
        raise ValueError(
            f"team code map must contain exactly {expected_teams} names and codes"
        )
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


def _aggregate_training_slot_rows(
    schedule_games: Iterable[Mapping[str, object]],
    statcast_plate_appearances: object,
    *,
    full_season_mode: bool = False,
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
        if set(sides["away"]) & set(sides["home"]):
            raise ValueError("official lineups contain a player on both teams")
        lineups[game_pk] = (season, official_date, sides)

    from .statcast import StatcastData

    if isinstance(statcast_plate_appearances, StatcastData):
        rows = statcast_plate_appearances.plate_appearances
    elif isinstance(statcast_plate_appearances, (str, bytes, Mapping)):
        raise ValueError("plate appearances must be an iterable of rows")
    else:
        rows = statcast_plate_appearances
    totals: defaultdict[tuple[int, int], int] = defaultdict(int)
    seen_pa = set()
    matched_batters: defaultdict[tuple[int, str], set[int]] = defaultdict(set)
    for row in rows:
        if not isinstance(row, (Mapping, SlotPA)):
            raise ValueError("plate appearance rows must be mappings or SlotPA")
        value = (
            row.get
            if isinstance(row, Mapping)
            else lambda field, default=None: getattr(row, field, default)
        )
        game_pk = _positive_id(value("game_pk"), "plate appearance game_pk")
        at_bat = value("at_bat_number")
        if type(at_bat) is not int or at_bat <= 0:
            raise ValueError("plate appearance at_bat_number must be positive")
        identity = (game_pk, at_bat)
        if identity in seen_pa:
            raise ValueError("duplicate plate appearance")
        seen_pa.add(identity)
        scheduled = lineups.get(game_pk)
        if scheduled is None:
            continue
        if _canonical_day(value("game_date"), "plate appearance game_date") != scheduled[1]:
            raise ValueError("plate appearance game_date does not match schedule date")
        if value("game_type", "R") != "R":
            raise ValueError("training plate appearances must be regular season")
        half = value("inning_topbot")
        if half not in {"Top", "Bot"}:
            raise ValueError("plate appearance inning_topbot must be Top or Bot")
        side = "away" if half == "Top" else "home"
        batter_id = _positive_id(value("batter_id"), "plate appearance batter_id")
        season, _official_date, sides = scheduled
        slot = sides[side].get(batter_id)
        if slot is not None:
            totals[(season, slot)] += 1
            matched_batters[(game_pk, side)].add(batter_id)
    if full_season_mode:
        missing_starters = sorted(
            (game_pk, side, slot, player_id)
            for game_pk, (_season, _official_date, sides) in lineups.items()
            for side in ("away", "home")
            for player_id, slot in sides[side].items()
            if player_id not in matched_batters[(game_pk, side)]
        )
        if missing_starters:
            raise ValueError(
                "played lineup games missing official lineup batters: "
                f"{missing_starters}"
            )
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


def _assert_schedule_pa_completeness(
    schedule_game_dates: Mapping[int, str],
    plate_appearances: Iterable[Mapping[str, object] | SlotPA],
) -> None:
    observed = set()
    for row in plate_appearances:
        value = row.get if isinstance(row, Mapping) else lambda field: getattr(row, field)
        game_pk = _positive_id(value("game_pk"), "plate appearance game_pk")
        if game_pk not in schedule_game_dates:
            continue
        if (
            _canonical_day(value("game_date"), "plate appearance game_date")
            != schedule_game_dates[game_pk]
        ):
            raise ValueError("plate appearance date conflicts with schedule")
        observed.add(game_pk)
    missing = sorted(set(schedule_game_dates) - observed)
    if missing:
        raise ValueError(f"missing played game Statcast PA: {missing}")


def _validate_played_schedule_season(
    games: Iterable[Mapping[str, object]],
    year: int,
    season_open: str,
    season_close: str,
    *,
    validation_policy: PreparationValidationPolicy = PRODUCTION_VALIDATION_POLICY,
) -> tuple[Mapping[str, object], ...]:
    """Return lineup-valid played games after enforcing live season sanity."""
    played_statuses = PLAYED_STATUSES
    unplayed_statuses = {"Postponed", "Canceled", "Cancelled"}
    played = []
    lineup_valid = []
    seen_pks = set()
    teams_by_id = {}
    for game in games:
        if not isinstance(game, Mapping):
            raise ValueError("schedule games must be mappings")
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in seen_pks:
            raise ValueError("schedule season contains duplicate game_pk")
        seen_pks.add(game_pk)
        official = _canonical_day(game.get("official_date"), "schedule official_date")
        if (
            date.fromisoformat(official).year != year
            or not season_open <= official <= season_close
            or game.get("game_type") != "R"
        ):
            raise ValueError(f"schedule row is outside frozen {year} regular season")
        status = game.get("status")
        if status in unplayed_statuses:
            continue
        if status not in played_statuses:
            raise ValueError(f"unsupported completed-season schedule status: {status}")
        away_score, home_score = game.get("away_score"), game.get("home_score")
        away_winner, home_winner = (
            game.get("away_is_winner"),
            game.get("home_is_winner"),
        )
        if (
            type(away_score) is not int
            or away_score < 0
            or type(home_score) is not int
            or home_score < 0
            or away_score == home_score
            or type(away_winner) is not bool
            or type(home_winner) is not bool
            or away_winner == home_winner
            or away_winner != (away_score > home_score)
        ):
            raise ValueError("played schedule outcome is missing or inconsistent")
        played.append(game)
        valid_lineups = True
        for side in ("away", "home"):
            team_id = _positive_id(game.get(f"{side}_team_id"), "schedule team id")
            name = game.get(f"{side}_team")
            if not isinstance(name, str) or not name:
                raise ValueError("schedule team name must be nonempty")
            if team_id in teams_by_id and teams_by_id[team_id] != name:
                raise ValueError("schedule team id maps to conflicting names")
            teams_by_id[team_id] = name
            lineup = game.get(f"{side}_lineup")
            valid_lineups = bool(
                valid_lineups
                and isinstance(lineup, Sequence)
                and not isinstance(lineup, (str, bytes))
                and len(lineup) == 9
                and len(
                    {
                        player.get("player_id")
                        for player in lineup
                        if isinstance(player, Mapping)
                        and type(player.get("player_id")) is int
                        and player["player_id"] > 0
                    }
                )
                == 9
            )
        if valid_lineups:
            lineup_valid.append(game)
    if not isinstance(validation_policy, PreparationValidationPolicy):
        raise ValueError("validation_policy must be a PreparationValidationPolicy")
    if len(played) < validation_policy.minimum_played_games:
        raise ValueError(
            f"frozen {year} schedule requires at least "
            f"{validation_policy.minimum_played_games} played games"
        )
    expected_teams = validation_policy.expected_team_count
    if len(teams_by_id) != expected_teams or len(set(teams_by_id.values())) != expected_teams:
        raise ValueError(
            f"frozen {year} schedule must contain exactly {expected_teams} teams"
        )
    if len(lineup_valid) < validation_policy.minimum_lineup_games:
        raise ValueError(
            f"frozen {year} schedule requires at least "
            f"{validation_policy.minimum_lineup_games} valid 9+9 lineup games"
        )
    return tuple(lineup_valid)


def derive_training_slot_rows(
    schedule_evidence_by_year: Mapping[int, VerifiedScheduleEvidence],
    statcast_evidence: VerifiedStatcastEvidence,
    *,
    verified_catalog=None,
    validation_policy: PreparationValidationPolicy = PRODUCTION_VALIDATION_POLICY,
) -> list[dict[str, object]]:
    """Derive production slot rows only from verified full-season sources."""
    if (
        not isinstance(schedule_evidence_by_year, Mapping)
        or set(schedule_evidence_by_year) != {2022, 2023, 2024}
        or not isinstance(statcast_evidence, VerifiedStatcastEvidence)
    ):
        raise ValueError("verified full-season schedule and Statcast evidence is required")
    frozen = {
        2022: ("2022-04-07", "2022-10-05"),
        2023: ("2023-03-30", "2023-10-01"),
        2024: ("2024-03-20", "2024-09-30"),
    }
    schedule_games_by_pk = {}
    played_game_pks = set()
    played_game_dates = {}
    for year, (season_open, season_close) in frozen.items():
        evidence = (
            _rebind_schedule_evidence(schedule_evidence_by_year[year])
            if verified_catalog is None
            else _rebind_schedule_evidence(
                schedule_evidence_by_year[year], verified_catalog
            )
        )
        if (
            evidence.query.get("role") != "training"
            or evidence.coverage_start != season_open
            or evidence.coverage_end != season_close
        ):
            raise ValueError(f"schedule evidence query must equal frozen {year} season")
        season_games = [
            game
            for game in evidence.games
            if date.fromisoformat(str(game["official_date"])).year == year
        ]
        played_game_pks.update(
            int(game["game_pk"])
            for game in season_games
            if game.get("status") in PLAYED_STATUSES
        )
        for game in season_games:
            if game.get("status") in PLAYED_STATUSES:
                game_pk = int(game["game_pk"])
                if game_pk in played_game_dates:
                    raise ValueError("full-season schedule evidence duplicates played game_pk")
                played_game_dates[game_pk] = str(game["official_date"])
        validation_kwargs = (
            {}
            if validation_policy is PRODUCTION_VALIDATION_POLICY
            else {"validation_policy": validation_policy}
        )
        for game in _validate_played_schedule_season(
            season_games, year, season_open, season_close, **validation_kwargs
        ):
            game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
            if game_pk in schedule_games_by_pk:
                raise ValueError("full-season schedule evidence duplicates game_pk")
            schedule_games_by_pk[game_pk] = game

    statcast_evidence = (
        _rebind_statcast_evidence(statcast_evidence)
        if verified_catalog is None
        else _rebind_statcast_evidence(statcast_evidence, verified_catalog)
    )
    expected_dates = set()
    for first, last in frozen.values():
        cursor = date.fromisoformat(first)
        end = date.fromisoformat(last)
        while cursor <= end:
            expected_dates.add(cursor)
            cursor += timedelta(days=1)
    covered_dates = set()
    for first, last in statcast_evidence.coverage_ranges:
        cursor = date.fromisoformat(first)
        end = date.fromisoformat(last)
        while cursor <= end:
            if cursor in covered_dates:
                raise ValueError("verified Statcast query ranges overlap")
            covered_dates.add(cursor)
            cursor += timedelta(days=1)
    if covered_dates != expected_dates:
        raise ValueError("verified Statcast evidence does not cover frozen seasons")
    for row in statcast_evidence.plate_appearances:
        game_pk = row.game_pk
        if game_pk not in played_game_pks:
            raise ValueError("verified Statcast plate appearance lacks a schedule game")
        if row.game_date != played_game_dates[game_pk]:
            raise ValueError("verified Statcast plate appearance date conflicts with schedule")
    _assert_schedule_pa_completeness(
        played_game_dates,
        statcast_evidence.plate_appearances,
    )
    rows = _aggregate_training_slot_rows(
        schedule_games_by_pk.values(),
        statcast_evidence.plate_appearances,
        full_season_mode=True,
    )
    totals_by_year = defaultdict(int)
    for row in rows:
        totals_by_year[int(row["season"])] += int(row["pa"])
    minimum_pa = validation_policy.minimum_starting_lineup_pa
    if any(totals_by_year[year] < minimum_pa for year in frozen):
        raise ValueError(
            f"each frozen season requires at least {minimum_pa} starting-lineup PA"
        )
    if len(rows) != 27:
        raise ValueError("frozen slot aggregation must contain exactly 27 rows")
    return rows


def schedule_history_rows(
    normalized_games: Iterable[Mapping[str, object]],
    venue_reference: Mapping[int, Mapping[str, object]],
    evidence: VerifiedScheduleEvidence,
    *,
    verified_catalog=None,
) -> ScheduleHistoryBuild:
    """Convert normalized schedules into source-backed ScheduleHistory rows."""
    if not isinstance(venue_reference, Mapping):
        raise ValueError("venue_reference must be a mapping")
    evidence = (
        _rebind_schedule_evidence(evidence)
        if verified_catalog is None
        else _rebind_schedule_evidence(evidence, verified_catalog)
    )
    first, last = evidence.coverage_start, evidence.coverage_end
    expected = set(evidence.game_pks)
    supplied = tuple(normalized_games)
    supplied_by_pk = {}
    for game in supplied:
        if not isinstance(game, Mapping):
            raise ValueError("normalized schedule games must be mappings")
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in supplied_by_pk:
            raise ValueError("duplicate schedule game_pk")
        supplied_by_pk[game_pk] = game
    if set(supplied_by_pk) != expected:
        raise ValueError("passed games do not equal verified schedule game_pks")
    for verified_game in evidence.games:
        supplied_game = supplied_by_pk[int(verified_game["game_pk"])]
        if any(
            key not in supplied_game or supplied_game[key] != value
            for key, value in verified_game.items()
        ):
            raise ValueError("passed games do not equal verified schedule games")
        allowed_enrichment = {
            "away_team_code",
            "home_team_code",
            "away_actual_starter_id",
            "home_actual_starter_id",
            "away_probable_starter_mismatch",
            "home_probable_starter_mismatch",
            "starter_provenance",
        }
        if set(supplied_game) - set(verified_game) - allowed_enrichment:
            raise ValueError("passed games contain unverified schedule fields")
    rows = []
    seen = set()
    status_counts = Counter(str(game.get("status")) for game in evidence.games)
    played_expected = {
        int(game["game_pk"])
        for game in evidence.games
        if game.get("status") in PLAYED_STATUSES
    }
    excluded = sorted(
        (
            int(game["game_pk"]),
            str(game.get("status")),
        )
        for game in evidence.games
        if game.get("status") not in PLAYED_STATUSES
    )
    for game in supplied:
        if not isinstance(game, Mapping):
            raise ValueError("normalized schedule games must be mappings")
        if game.get("game_type") != "R":
            continue
        game_pk = _positive_id(game.get("game_pk"), "schedule game_pk")
        if game_pk in seen:
            raise ValueError("duplicate schedule game_pk")
        seen.add(game_pk)
        if game.get("status") not in PLAYED_STATUSES:
            continue
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
        innings = game.get("completed_innings")
        if innings is not None and (type(innings) is not int or innings <= 0):
            raise ValueError("completed innings must be positive when present")
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
            "final_outs": game.get("final_outs"),
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
    if actual != played_expected:
        raise ValueError(
            f"expected played game_pk set differs; missing={sorted(played_expected - actual)}, "
            f"unexpected={sorted(actual - played_expected)}"
        )
    provenance = {
        "source_manifest_sha256": evidence.manifest_sha256,
        "raw_game_count": len(evidence.games),
        "status_counts": dict(sorted(status_counts.items())),
        "played_game_pks": tuple(sorted(played_expected)),
        "played_game_count": len(played_expected),
        "excluded_unplayed_game_pks": tuple(game_pk for game_pk, _status in excluded),
        "excluded_unplayed_statuses": tuple(status for _game_pk, status in excluded),
    }
    return ScheduleHistoryBuild(
        tuple(rows),
        first,
        last,
        True,
        evidence.manifest_sha256,
        tuple(sorted(played_expected)),
        provenance,
    )


def build_pregame_history(
    statcast_data: object,
    target_game: Mapping[str, object],
    slot_weights: Mapping[int, float],
    schedule_build: ScheduleHistoryBuild,
):
    """Construct one full-source FeatureHistory for strict as-of indexed queries."""
    from .feature_engine import FeatureHistory
    from .statcast import StatcastData

    if not isinstance(target_game, Mapping):
        raise ValueError("target_game must be a mapping")
    _positive_id(target_game.get("game_pk"), "target game_pk")
    _canonical_day(target_game.get("official_date"), "target official_date")
    if not isinstance(schedule_build, ScheduleHistoryBuild):
        raise ValueError("schedule_build must be a ScheduleHistoryBuild")
    if not isinstance(statcast_data, StatcastData):
        raise ValueError("statcast_data must be StatcastData")

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
        statcast_data,
        slot_weights=slot_weights,
        schedule_history=schedule_build.rows,
        schedule_coverage_start=schedule_build.coverage_start,
        schedule_coverage_end=schedule_build.coverage_end,
        schedule_coverage_complete=schedule_build.coverage_complete,
        venue_by_name=venue_by_name,
    )
    return history


__all__ = [
    "ParticipantBuild",
    "PLAYED_STATUSES",
    "ScheduleHistoryBuild",
    "TeamCodeBuild",
    "VerifiedScheduleEvidence",
    "VerifiedStatcastEvidence",
    "_aggregate_training_slot_rows",
    "assert_pregame_history",
    "build_actual_participants",
    "build_source_manifest_index",
    "build_pregame_history",
    "derive_team_code_map",
    "derive_training_slot_rows",
    "load_verified_schedule_evidence",
    "load_verified_statcast_evidence",
    "schedule_history_rows",
]
