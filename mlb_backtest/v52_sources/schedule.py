"""MLB schedule anchors and joined final game evidence."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from ..source_parsers.schedule import (
    parse_schedule,
    parse_schedule_occurrences,
    validate_schedule_query_coverage,
)
from ..v52_evidence import LogicalQuery, logical_query_id
from ..v52_source_contracts import (
    QUERY_SIDECAR_KEYS,
    canonical_json_bytes,
    canonical_sha256,
    query_sidecar,
    require_query,
    require_schedule_query,
)


_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_PLAYED = frozenset({"Final", "Completed Early"})
_STARTING_BATTING_ORDERS = frozenset(
    str(slot) for slot in range(100, 1000, 100)
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SCHEDULE_ANCHOR_KEYS = frozenset(
    {
        "alternate_site",
        "away_score",
        "away_team_id",
        "completed_innings",
        "doubleheader_order",
        "game_pk",
        "home_score",
        "home_team_id",
        "neutral_site",
        "official_date",
        "played",
        "raw_game_object_sha256",
        "scheduled_innings",
        "scheduled_start_utc",
        "season",
        "season_start_date",
        "terminal_status",
        "venue_id",
        "winner_team_id",
    }
)


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


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _json_object(raw: bytes, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes):
        raise TypeError("raw must be bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value


def _official_optional_bool(
    record: Mapping[str, object], field: str, label: str
) -> bool:
    """MLB Stats API omits ordinary false site flags; explicit values stay strict."""
    value = record.get(field, False)
    if type(value) is not bool:
        raise ValueError(f"schedule {label} must be boolean when present")
    return value


def _raw_schedule_metadata(raw: bytes) -> tuple[dict[int, dict[str, bool]], int, int]:
    payload = _json_object(raw, "schedule")
    total_games = payload.get("totalGames")
    if type(total_games) is not int or total_games < 0:
        raise ValueError("schedule totalGames must be a nonnegative integer")
    dates = payload.get("dates")
    if not isinstance(dates, list):
        raise ValueError("schedule dates must be a list")
    site_flags: dict[int, dict[str, bool]] = {}
    date_total_sum = 0
    for day in dates:
        if not isinstance(day, dict) or not isinstance(day.get("games"), list):
            raise ValueError("schedule date games must be a list")
        date_total = day.get("totalGames")
        if type(date_total) is not int or date_total < 0:
            raise ValueError("schedule date totalGames must be a nonnegative integer")
        date_total_sum += date_total
        for game in day["games"]:
            if not isinstance(game, dict) or game.get("gameType") != "R":
                continue
            game_pk = _positive(game.get("gamePk"), "schedule gamePk")
            neutral = _official_optional_bool(game, "neutralSite", "neutralSite")
            alternate = _official_optional_bool(
                game, "isAlternateSite", "isAlternateSite"
            )
            flags = {
                "alternate_site": alternate,
                "neutral_site": neutral,
            }
            prior = site_flags.setdefault(game_pk, flags)
            if prior != flags:
                raise ValueError("schedule duplicate gamePk site flags differ")
        if date_total != len(day["games"]):
            raise ValueError("schedule date totalGames differs from response population")
    raw_occurrences = parse_schedule_occurrences(raw)
    if total_games != date_total_sum or total_games != len(raw_occurrences):
        raise ValueError("schedule totalGames differs from exact parsed population")
    return site_flags, total_games, len(raw_occurrences)


def _schedule_range(query: LogicalQuery) -> tuple[int, date, date]:
    year = query.requested_range.get("year")
    if isinstance(year, bool) or not isinstance(year, int) or year not in range(2018, 2025):
        raise ValueError("schedule query year must be within 2018-2024")
    first = _day(query.requested_range.get("start_date"), "schedule query start")
    last = _day(query.requested_range.get("end_date"), "schedule query end")
    if first > last or first.year != year or last.year != year:
        raise ValueError("schedule query range differs from year")
    expected_parameters = {
        "sportId": 1,
        "gameType": "R",
        "startDate": first.isoformat(),
        "endDate": last.isoformat(),
        "hydrate": "probablePitcher,lineups,venue,linescore",
    }
    if dict(query.parameters) != expected_parameters:
        raise ValueError("schedule query parameters differ from requested range")
    return year, first, last


def recompute_schedule_anchor_sidecar(
    raw: bytes, query: LogicalQuery
) -> Mapping[str, object]:
    """Seal a complete schedule response without depending on boxscores."""
    require_schedule_query(
        query,
        endpoint=_SCHEDULE_ENDPOINT,
        expected_media_type="application/json",
    )
    year, first, last = _schedule_range(query)
    site_flags, declared_total, raw_occurrence_count = _raw_schedule_metadata(raw)
    validate_schedule_query_coverage(raw, first.isoformat(), last.isoformat())
    games = parse_schedule(raw)
    if not games:
        raise ValueError("schedule response contains no regular-season games")
    if raw_occurrence_count != declared_total:
        raise ValueError("schedule raw occurrence population differs from totalGames")
    if any(
        not first <= _day(game["official_date"], "game official_date") <= last
        for game in games
    ):
        raise ValueError("schedule game is outside query range")

    anchors = []
    for game in sorted(games, key=lambda row: int(row["game_pk"])):
        played = game["status"] in _PLAYED
        if played and (
            game["away_score"] is None
            or game["home_score"] is None
            or game["away_is_winner"] == game["home_is_winner"]
        ):
            raise ValueError("terminal schedule game lacks a unique outcome")
        marker = game["doubleheader_marker"]
        order = int(str(marker).removeprefix("GAME")) if marker else 1
        winner_team_id = None
        if played:
            winner_team_id = (
                game["away_team_id"]
                if game["away_is_winner"]
                else game["home_team_id"]
            )
            score_winner_team_id = (
                game["away_team_id"]
                if game["away_score"] > game["home_score"]
                else game["home_team_id"]
            )
            if (
                game["away_score"] == game["home_score"]
                or winner_team_id != score_winner_team_id
            ):
                raise ValueError("terminal schedule winner disagrees with final runs")
        anchor = {
            "away_score": game["away_score"],
            "alternate_site": site_flags[int(game["game_pk"])]["alternate_site"],
            "away_team_id": game["away_team_id"],
            "completed_innings": game["completed_innings"],
            "doubleheader_order": order,
            "game_pk": game["game_pk"],
            "home_score": game["home_score"],
            "home_team_id": game["home_team_id"],
            "neutral_site": site_flags[int(game["game_pk"])]["neutral_site"],
            "official_date": game["official_date"],
            "played": played,
            "scheduled_innings": game["scheduled_innings"],
            "scheduled_start_utc": game["scheduled_start_utc"],
            "season": year,
            "season_start_date": first.isoformat(),
            "terminal_status": game["status"],
            "venue_id": game["venue_id"],
            "winner_team_id": winner_team_id,
        }
        if anchor["venue_id"] is None:
            raise ValueError("regular-season schedule game requires official venue identity")
        anchors.append(anchor)

    game_pks = [int(row["game_pk"]) for row in anchors]
    played_pks = [int(row["game_pk"]) for row in anchors if row["played"]]
    unplayed_pks = [int(row["game_pk"]) for row in anchors if not row["played"]]
    semantics = {
        "anchors": anchors,
        "child_query_requirements": {
            "boxscores": [
                {
                    "endpoint": f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore",
                    "game_pk": game_pk,
                }
                for game_pk in played_pks
            ],
            "venues": [
                {
                    "endpoint": f"https://statsapi.mlb.com/api/v1/venues/{venue_id}",
                    "venue_id": venue_id,
                }
                for venue_id in sorted({int(row["venue_id"]) for row in anchors})
            ],
        },
        "coverage": {
            "inclusive_end": last.isoformat(),
            "inclusive_start": first.isoformat(),
            "season": year,
        },
        "fields_present": sorted(anchors[0]) if anchors else [],
        "game_pks": game_pks,
        "played_game_pks": played_pks,
        "unique_game_count": len(game_pks),
        "unplayed_game_pks": unplayed_pks,
    }
    return query_sidecar(raw, query, semantics)


def _boxscore_side(raw: object, label: str) -> tuple[int, int, list[int]]:
    if not isinstance(raw, dict):
        raise ValueError(f"boxscore {label} must be an object")
    team = raw.get("team")
    pitchers = raw.get("pitchers")
    players = raw.get("players")
    if not isinstance(team, dict):
        raise ValueError(f"boxscore {label} team must be an object")
    team_id = _positive(team.get("id"), f"boxscore {label} team id")
    if not isinstance(pitchers, list) or not pitchers:
        raise ValueError(f"boxscore {label} requires an actual starting pitcher")
    starter = _positive(pitchers[0], f"boxscore {label} starter")
    if not isinstance(players, dict):
        raise ValueError(f"boxscore {label} players must be an object")
    hitters: dict[int, int] = {}
    for player in players.values():
        if not isinstance(player, dict):
            raise ValueError(f"boxscore {label} player must be an object")
        order = player.get("battingOrder")
        if order is None:
            continue
        if not isinstance(order, str) or not order.isdecimal():
            raise ValueError(f"boxscore {label} battingOrder must be decimal")
        if order not in _STARTING_BATTING_ORDERS:
            continue
        slot = int(order)
        if slot in hitters:
            raise ValueError(f"boxscore {label} starting lineup is ambiguous")
        person = player.get("person")
        if not isinstance(person, dict):
            raise ValueError(f"boxscore {label} person must be an object")
        hitters[slot] = _positive(person.get("id"), f"boxscore {label} hitter")
    if set(hitters) != set(range(100, 1000, 100)) or len(set(hitters.values())) != 9:
        raise ValueError(f"boxscore {label} must contain nine unique starting hitters")
    return team_id, starter, [hitters[slot] for slot in sorted(hitters)]


def _boxscore(raw: bytes) -> dict[str, object]:
    payload = _json_object(raw, "boxscore")
    teams = payload.get("teams")
    if not isinstance(teams, dict) or set(teams) != {"away", "home"}:
        raise ValueError("boxscore must contain exactly away and home teams")
    away_team, away_starter, away_hitters = _boxscore_side(teams["away"], "away")
    home_team, home_starter, home_hitters = _boxscore_side(teams["home"], "home")
    if away_team == home_team or away_starter == home_starter:
        raise ValueError("boxscore sides must have distinct teams and starters")
    def runs(side: object, label: str) -> int:
        team_stats = side.get("teamStats") if isinstance(side, dict) else None
        batting = team_stats.get("batting") if isinstance(team_stats, dict) else None
        value = batting.get("runs") if isinstance(batting, dict) else None
        if type(value) is not int or value < 0:
            raise ValueError(f"boxscore {label} requires nonnegative terminal runs")
        return value

    away_score = runs(teams["away"], "away")
    home_score = runs(teams["home"], "home")
    if away_score == home_score:
        raise ValueError("boxscore terminal winner requires unequal final runs")
    winner_team_id = away_team if away_score > home_score else home_team
    return {
        "actual_starter_ids": [away_starter, home_starter],
        "away_starting_hitter_ids": away_hitters,
        "away_team_id": away_team,
        "home_starting_hitter_ids": home_hitters,
        "home_team_id": home_team,
        "away_score": away_score,
        "home_score": home_score,
        "winner_team_id": winner_team_id,
    }


def recompute_boxscore_sidecar(
    raw: bytes, query: LogicalQuery
) -> Mapping[str, object]:
    """Seal exactly one response-local boxscore participant projection."""
    require_query(
        query,
        source="mlb_schedule_game_feed",
        grain="season_schedule_and_game",
        expected_media_type="application/json",
    )
    game_pk = _positive(query.requested_range.get("game_pk"), "boxscore game_pk")
    expected_endpoint = (
        f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
    )
    if query.endpoint != expected_endpoint:
        raise ValueError("boxscore endpoint differs from requested gamePk")
    season = query.requested_range.get("season")
    if isinstance(season, bool) or not isinstance(season, int) or season not in range(2018, 2025):
        raise ValueError("boxscore season must be within 2018-2024")
    official = _day(query.requested_range.get("official_date"), "boxscore official date")
    if official.year != season:
        raise ValueError("boxscore official date differs from season")
    return query_sidecar(
        raw,
        query,
        {"game_pk": game_pk, "official_date": official.isoformat(), **_boxscore(raw)},
    )


def derived_schedule_sidecar_adapter(
    query: LogicalQuery,
    source_anchors: Sequence[Mapping[str, object]],
) -> Mapping[str, object]:
    """Adapt source-owned schedule anchors into the rawless reducer envelope."""
    if isinstance(source_anchors, (str, bytes)) or not isinstance(
        source_anchors, Sequence
    ):
        raise ValueError("source schedule anchors must be a sequence")
    anchors: list[dict[str, object]] = []
    seen: set[int] = set()
    for raw_anchor in source_anchors:
        if not isinstance(raw_anchor, Mapping):
            raise ValueError("source schedule anchor must be an object")
        anchor = dict(raw_anchor)
        if set(anchor) != _SOURCE_SCHEDULE_ANCHOR_KEYS:
            raise ValueError("source schedule anchor keys differ")
        game_pk = _positive(anchor.get("game_pk"), "source schedule anchor gamePk")
        if game_pk in seen:
            raise ValueError("source schedule anchors contain duplicate gamePk")
        seen.add(game_pk)
        away_team_id = _positive(
            anchor.get("away_team_id"), "source schedule away team ID"
        )
        home_team_id = _positive(
            anchor.get("home_team_id"), "source schedule home team ID"
        )
        _positive(anchor.get("venue_id"), "source schedule venue ID")
        _positive(
            anchor.get("doubleheader_order"), "source schedule doubleheader order"
        )
        if (
            not isinstance(anchor.get("raw_game_object_sha256"), str)
            or _SHA256.fullmatch(anchor["raw_game_object_sha256"]) is None
            or type(anchor.get("played")) is not bool
            or type(anchor.get("neutral_site")) is not bool
            or type(anchor.get("alternate_site")) is not bool
            or away_team_id == home_team_id
        ):
            raise ValueError("source schedule anchor values differ")
        season = anchor.get("season")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or season not in range(2018, 2025)
        ):
            raise ValueError("source schedule anchor season differs")
        _day(anchor.get("official_date"), "source schedule official date")
        _day(anchor.get("season_start_date"), "source schedule season start date")
        if season == 2019:
            anchors.append(
                {
                    key: value
                    for key, value in anchor.items()
                    if key != "raw_game_object_sha256"
                }
            )
    if not anchors:
        raise ValueError("source schedule adapter requires 2019 anchors")
    return {
        "byte_count": 0,
        "grain": query.grain,
        "parser_version": query.parser_version,
        "query_id": logical_query_id(query),
        "raw_sha256": hashlib.sha256(b"").hexdigest(),
        "schema_version": 1,
        "semantics": {"anchors": anchors},
        "source": query.source,
    }


def recompute_game_evidence_snapshot(
    schedule_anchors: Sequence[Mapping[str, object]],
    boxscores: Mapping[int, tuple[bytes, str]],
    *,
    schedule_provenance: Mapping[int, Mapping[str, object]] | None = None,
) -> Mapping[str, object]:
    """Join verified schedule anchors with per-played-game boxscore evidence."""
    if isinstance(schedule_anchors, (str, bytes)) or not isinstance(
        schedule_anchors, Sequence
    ):
        raise ValueError("schedule_anchors must be a sequence")
    if not isinstance(boxscores, Mapping):
        raise ValueError("boxscores must be a mapping")
    if not schedule_anchors:
        raise ValueError("schedule game snapshot requires a nonempty anchor population")
    anchors_by_pk: dict[int, tuple[dict[str, object], str]] = {}
    sidecar_hashes: list[str] = []
    for sidecar in schedule_anchors:
        if (
            not isinstance(sidecar, Mapping)
            or set(sidecar) != QUERY_SIDECAR_KEYS
            or sidecar.get("source") != "mlb_schedule_game_feed"
            or sidecar.get("grain") != "season_schedule_and_game"
            or sidecar.get("parser_version") != "v5.2.1"
            or sidecar.get("schema_version") != 1
            or type(sidecar.get("byte_count")) is not int
            or sidecar["byte_count"] < 0
            or not isinstance(sidecar.get("query_id"), str)
            or not _SHA256.fullmatch(sidecar["query_id"])
            or not isinstance(sidecar.get("raw_sha256"), str)
            or not _SHA256.fullmatch(sidecar["raw_sha256"])
        ):
            raise ValueError("schedule anchor must be a canonical Task2 query sidecar")
        semantics = sidecar.get("semantics")
        if not isinstance(semantics, Mapping) or not isinstance(
            semantics.get("anchors"), list
        ):
            raise ValueError("schedule anchor semantics are invalid")
        sidecar_hash = hashlib.sha256(
            canonical_json_bytes(dict(sidecar)) + b"\n"
        ).hexdigest()
        sidecar_hashes.append(sidecar_hash)
        for raw_anchor in semantics["anchors"]:
            if not isinstance(raw_anchor, dict):
                raise ValueError("schedule anchor must be an object")
            game_pk = _positive(raw_anchor.get("game_pk"), "anchor game_pk")
            if (
                type(raw_anchor.get("played")) is not bool
                or type(raw_anchor.get("neutral_site")) is not bool
                or type(raw_anchor.get("alternate_site")) is not bool
            ):
                raise ValueError("schedule anchor disposition flags must be explicit")
            if game_pk in anchors_by_pk:
                raise ValueError("schedule snapshots contain duplicate gamePk")
            anchors_by_pk[game_pk] = (dict(raw_anchor), sidecar_hash)

    if not anchors_by_pk:
        raise ValueError("schedule game snapshot requires a nonempty game population")
    if schedule_provenance is not None:
        if not isinstance(schedule_provenance, Mapping) or set(schedule_provenance) != set(
            anchors_by_pk
        ):
            raise ValueError("schedule provenance population differs")
        for game_pk, (anchor, _anchor_hash) in anchors_by_pk.items():
            provenance = schedule_provenance.get(game_pk)
            if not isinstance(provenance, Mapping) or set(provenance) != {
                "game_pk",
                "official_date",
                "away_team_id",
                "home_team_id",
                "raw_game_object_sha256",
                "normalized_anchor_sha256",
            }:
                raise ValueError("schedule provenance keys differ")
            if any(
                provenance.get(field) != anchor.get(field)
                for field in ("game_pk", "official_date", "away_team_id", "home_team_id")
            ):
                raise ValueError("schedule provenance identity differs")
            for field in ("raw_game_object_sha256", "normalized_anchor_sha256"):
                value = provenance.get(field)
                if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                    raise ValueError("schedule provenance hash differs")

    played_anchor_pks = {
        game_pk for game_pk, (anchor, _hash) in anchors_by_pk.items()
        if anchor.get("played") is True
    }
    extra = set(boxscores) - played_anchor_pks
    if extra:
        raise ValueError("boxscore has no verified schedule anchor")
    verified_boxscores: dict[int, tuple[dict[str, object], str]] = {}
    for game_pk, evidence in boxscores.items():
        if (
            not isinstance(evidence, tuple)
            or len(evidence) != 2
            or not isinstance(evidence[0], bytes)
            or not isinstance(evidence[1], str)
            or hashlib.sha256(evidence[0]).hexdigest() != evidence[1]
        ):
            raise ValueError("boxscore bytes do not match their verified hash")
        try:
            decoded = json.loads(evidence[0])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("boxscore sidecar artifact must be canonical JSON") from exc
        if (
            not isinstance(decoded, dict)
            or set(decoded) != QUERY_SIDECAR_KEYS
            or decoded.get("source") != "mlb_schedule_game_feed"
            or decoded.get("grain") != "season_schedule_and_game"
            or decoded.get("parser_version") != "v5.2.1"
            or decoded.get("schema_version") != 1
            or type(decoded.get("byte_count")) is not int
            or decoded["byte_count"] < 0
            or not isinstance(decoded.get("query_id"), str)
            or not _SHA256.fullmatch(decoded["query_id"])
            or not isinstance(decoded.get("raw_sha256"), str)
            or not _SHA256.fullmatch(decoded["raw_sha256"])
            or canonical_json_bytes(decoded) + b"\n" != evidence[0]
            or not isinstance(decoded.get("semantics"), dict)
            or decoded["semantics"].get("game_pk") != game_pk
        ):
            raise ValueError("boxscore query sidecar lineage differs from gamePk")
        verified_boxscores[game_pk] = (decoded, evidence[1])
        sidecar_hashes.append(evidence[1])
    games = []
    for game_pk, (anchor, anchor_hash) in sorted(anchors_by_pk.items()):
        played = anchor.get("played") is True
        base = {
            **anchor,
            "feature_history_cutoff": (
                _day(anchor.get("official_date"), "anchor official_date")
                - timedelta(days=1)
            ).isoformat(),
        }
        if not played:
            game = {
                **base,
                "actual_starter_ids": [],
                "away_starting_hitter_ids": [],
                "home_starting_hitter_ids": [],
                "participant_provenance": "HISTORICAL_NO_PARTICIPANTS",
                "schedule_query_sidecar_sha256": anchor_hash,
                "verification_status": "INELIGIBLE_UNPLAYED",
            }
            game["source_game_sha256"] = canonical_sha256(
                {"anchor_sidecar_sha256": anchor_hash, "game": game}
            )
            games.append(game)
            continue
        evidence = verified_boxscores.get(game_pk)
        if evidence is None:
            raise ValueError(f"missing boxscore for played game {game_pk}")
        participants = dict(evidence[0]["semantics"])
        if (
            participants["away_team_id"] != anchor.get("away_team_id")
            or participants["home_team_id"] != anchor.get("home_team_id")
        ):
            raise ValueError("boxscore team identity disagrees with schedule anchor")
        if any(
            participants.get(field) != anchor.get(field)
            for field in (
                "official_date",
                "away_score",
                "home_score",
                "winner_team_id",
            )
        ):
            raise ValueError("boxscore terminal evidence disagrees with schedule anchor")
        participants.pop("game_pk", None)
        participants.pop("official_date", None)
        game = {
            **base,
            **participants,
            "boxscore_query_sidecar_sha256": evidence[1],
            "schedule_query_sidecar_sha256": anchor_hash,
            "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "verification_status": "VERIFIED",
        }
        if schedule_provenance is not None:
            provenance = schedule_provenance[game_pk]
            game["raw_game_object_sha256"] = provenance["raw_game_object_sha256"]
            game["normalized_anchor_sha256"] = provenance[
                "normalized_anchor_sha256"
            ]
        game["source_game_sha256"] = canonical_sha256(
            {
                "anchor_sidecar_sha256": anchor_hash,
                "boxscore_query_sidecar_sha256": evidence[1],
                "game": game,
            }
        )
        games.append(game)
    return {
        "games": games,
        "grain": "season_game_evidence_snapshot",
        "schema_version": 1,
        "source_query_sidecar_sha256s": sorted(set(sidecar_hashes)),
    }


__all__ = [
    "derived_schedule_sidecar_adapter",
    "recompute_boxscore_sidecar",
    "recompute_game_evidence_snapshot",
    "recompute_schedule_anchor_sidecar",
]
