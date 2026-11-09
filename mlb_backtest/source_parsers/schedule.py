"""Pure, offline MLB schedule parsing and validation."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


OUT_OF_RANGE_UNPLAYED_STATUSES = frozenset({"Postponed", "Canceled", "Cancelled"})


def _mapping(value: Any, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"malformed schedule: {label} must be an object")
    return value


def _list(value: Any, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"malformed schedule: {label} must be a list")
    return value


def _required(mapping: dict, field: str, expected_type: type, label: str) -> Any:
    value = mapping.get(field)
    if isinstance(value, bool) and expected_type is int:
        value = None
    if not isinstance(value, expected_type):
        raise ValueError(
            f"malformed schedule: {label}.{field} must be {expected_type.__name__}"
        )
    if expected_type is str and not value:
        raise ValueError(f"malformed schedule: {label}.{field} cannot be empty")
    if expected_type is int and value <= 0:
        raise ValueError(f"malformed schedule: {label}.{field} must be positive")
    return value


def _optional_person(side: dict, field: str, label: str) -> tuple[int | None, str | None]:
    value = side.get(field)
    if value is None:
        return None, None
    person = _mapping(value, label)
    return (
        _required(person, "id", int, label),
        _required(person, "fullName", str, label),
    )


def _canonical_utc_datetime(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"malformed schedule: {label} must be an ISO datetime")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"malformed schedule: {label} must be an ISO datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"malformed schedule: {label} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _lineup(lineups: dict, field: str, label: str) -> list[dict]:
    value = lineups.get(field)
    if value is None:
        return []
    lineup = _list(value, f"{label}.{field}")
    if len(lineup) != 9:
        raise ValueError(
            f"malformed schedule: {label}.{field} must contain 9 players"
        )
    result = []
    for index, raw_player in enumerate(lineup):
        player = _mapping(raw_player, f"{label}.{field}[{index}]")
        result.append(
            {
                "player_id": _required(
                    player, "id", int, f"{label}.{field}[{index}]"
                ),
                "player_name": _required(
                    player, "fullName", str, f"{label}.{field}[{index}]"
                ),
            }
        )
    return result


def _optional_nonnegative_int(
    mapping: dict, field: str, label: str, *, positive: bool = False
) -> int | None:
    value = mapping.get(field)
    if value is None:
        return None
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise ValueError(f"malformed schedule: {label}.{field} must be a {qualifier} integer")
    return value


def _optional_exact_bool(mapping: dict, field: str, label: str) -> bool | None:
    value = mapping.get(field)
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"malformed schedule: {label}.{field} must be boolean")
    return value


def _doubleheader_marker(
    game: dict, label: str, *, preserve_explicit_non_doubleheader: bool = False
) -> str | None:
    raw = game.get("doubleHeader")
    number = game.get("gameNumber")
    if raw in (None, "N"):
        if number is not None and (type(number) is not int or number <= 0):
            raise ValueError(f"malformed schedule: {label}.gameNumber must be positive")
        if raw == "N" and preserve_explicit_non_doubleheader:
            return "NO"
        return None
    if raw not in {"Y", "S"} or type(number) is not int or number not in {1, 2}:
        raise ValueError(f"malformed schedule: {label} has invalid doubleheader fields")
    return f"GAME{number}"


def _neutral_site_venue_name(game: dict) -> str | None:
    description = game.get("description")
    if not isinstance(description, str) or not description.startswith("MLB at "):
        return None
    venue_name, separator, city = description.removeprefix("MLB at ").partition(" in ")
    if not separator or not venue_name or not city:
        return None
    return venue_name


def _normalize_game(
    raw_game: Any,
    date_entry: dict,
    index: int,
    *,
    preserve_explicit_non_doubleheader: bool = False,
    game_types: tuple[str, ...] | None = ('R',),
) -> dict | None:
    game = _mapping(raw_game, f"game[{index}]")
    game_type = game.get("gameType")
    if game_types is not None and game_type not in game_types:
        return None
    if not isinstance(game_type, str) or not game_type:
        raise ValueError('malformed schedule: game type is required')

    official_date = game.get("officialDate", date_entry.get("date"))
    if not isinstance(official_date, str) or not official_date:
        raise ValueError("malformed schedule: regular game official date is required")
    try:
        parsed_official_date = date.fromisoformat(official_date)
    except ValueError as exc:
        raise ValueError("malformed schedule: official date must be ISO format") from exc
    if parsed_official_date.isoformat() != official_date:
        raise ValueError("malformed schedule: official date must be canonical YYYY-MM-DD")

    teams = _mapping(game.get("teams"), f"game[{index}].teams")
    away = _mapping(teams.get("away"), f"game[{index}].teams.away")
    home = _mapping(teams.get("home"), f"game[{index}].teams.home")
    away_team = _mapping(away.get("team"), f"game[{index}].teams.away.team")
    home_team = _mapping(home.get("team"), f"game[{index}].teams.home.team")
    status = _mapping(game.get("status"), f"game[{index}].status")
    status_name = _required(status, "detailedState", str, f"game[{index}].status")
    away_pitcher_id, away_pitcher_name = _optional_person(
        away, "probablePitcher", f"game[{index}].teams.away.probablePitcher"
    )
    home_pitcher_id, home_pitcher_name = _optional_person(
        home, "probablePitcher", f"game[{index}].teams.home.probablePitcher"
    )
    away_score = _optional_nonnegative_int(away, "score", f"game[{index}].teams.away")
    home_score = _optional_nonnegative_int(home, "score", f"game[{index}].teams.home")
    away_winner = _optional_exact_bool(away, "isWinner", f"game[{index}].teams.away")
    home_winner = _optional_exact_bool(home, "isWinner", f"game[{index}].teams.home")
    status_lower = status_name.lower()
    explicit_tie = "tie" in status_lower
    explicit_exception = explicit_tie or "suspend" in status_lower
    if explicit_tie:
        if (
            away_score is not None
            and home_score is not None
            and away_score != home_score
        ) or (away_winner is True) or (home_winner is True):
            raise ValueError("malformed schedule: explicit tie conflicts with outcome")
    if "final" in status_lower and not explicit_exception:
        if away_score is None or home_score is None:
            raise ValueError("malformed schedule: final game requires both scores")
        if away_winner is None or home_winner is None or away_winner == home_winner:
            raise ValueError("malformed schedule: final game requires exactly one winner")
        if away_score == home_score or away_winner != (away_score > home_score):
            raise ValueError("malformed schedule: final winner is inconsistent with scores")
    lineups_value = game.get("lineups")
    lineups = (
        {}
        if lineups_value is None
        else _mapping(lineups_value, f"game[{index}].lineups")
    )
    completed_innings = None
    final_outs = None
    final_inning_topbot = None
    linescore_value = game.get("linescore")
    if linescore_value is not None:
        linescore = _mapping(linescore_value, f"game[{index}].linescore")
        current_inning = linescore.get("currentInning")
        if current_inning is not None:
            if type(current_inning) is not int or current_inning <= 0:
                raise ValueError("malformed schedule: linescore.currentInning must be positive")
            completed_innings = current_inning
        inning_half = linescore.get("inningHalf")
        if inning_half is not None:
            if inning_half not in {"Top", "Bottom"}:
                raise ValueError(
                    "malformed schedule: linescore half-inning must be Top or Bottom"
                )
            final_inning_topbot = "Top" if inning_half == "Top" else "Bot"
        is_top_inning = linescore.get("isTopInning")
        if is_top_inning is not None:
            if type(is_top_inning) is not bool:
                raise ValueError(
                    "malformed schedule: linescore.isTopInning must be a boolean"
                )
            boolean_half = "Top" if is_top_inning else "Bot"
            if (
                final_inning_topbot is not None
                and final_inning_topbot != boolean_half
            ):
                raise ValueError(
                    "malformed schedule: linescore half-inning fields conflict"
                )
            final_inning_topbot = boolean_half
        outs = linescore.get("outs")
        if outs is not None:
            if type(outs) is not int or not 0 <= outs <= 3:
                raise ValueError(
                    "malformed schedule: linescore.outs must be an integer from 0 to 3"
                )
            final_outs = outs
    if status_name not in {"Final", "Completed Early"}:
        completed_innings = None
        final_outs = None
        final_inning_topbot = None

    venue_value = game.get("venue")
    if venue_value is None:
        venue_id = None
        venue_name = _neutral_site_venue_name(game)
    else:
        venue = _mapping(venue_value, f"game[{index}].venue")
        if venue.get("id") is None and venue.get("name") is None:
            venue_id = None
            venue_name = _neutral_site_venue_name(game)
        else:
            venue_id = _required(venue, "id", int, f"game[{index}].venue")
            venue_name = _required(venue, "name", str, f"game[{index}].venue")

    return {
        "game_pk": _required(game, "gamePk", int, f"game[{index}]"),
        "official_date": official_date,
        "game_type": game_type,
        "scheduled_start_utc": _canonical_utc_datetime(
            game.get("gameDate"), f"game[{index}].gameDate"
        ),
        "status": status_name,
        "away_team": _required(
            away_team, "name", str, f"game[{index}].teams.away.team"
        ),
        "away_team_id": _required(
            away_team, "id", int, f"game[{index}].teams.away.team"
        ),
        "home_team": _required(
            home_team, "name", str, f"game[{index}].teams.home.team"
        ),
        "home_team_id": _required(
            home_team, "id", int, f"game[{index}].teams.home.team"
        ),
        "away_probable_pitcher_id": away_pitcher_id,
        "away_probable_pitcher_name": away_pitcher_name,
        "home_probable_pitcher_id": home_pitcher_id,
        "home_probable_pitcher_name": home_pitcher_name,
        "venue_id": venue_id,
        "venue_name": venue_name,
        "away_score": away_score,
        "home_score": home_score,
        "away_is_winner": away_winner,
        "home_is_winner": home_winner,
        "scheduled_innings": _optional_nonnegative_int(
            game, "scheduledInnings", f"game[{index}]", positive=True
        ),
        "completed_innings": completed_innings,
        "final_outs": final_outs,
        "final_inning_topbot": final_inning_topbot,
        "doubleheader_marker": _doubleheader_marker(
            game,
            f"game[{index}]",
            preserve_explicit_non_doubleheader=preserve_explicit_non_doubleheader,
        ),
        "away_lineup": _lineup(
            lineups, "awayPlayers", f"game[{index}].lineups"
        ),
        "home_lineup": _lineup(
            lineups, "homePlayers", f"game[{index}].lineups"
        ),
    }


def parse_schedule_occurrences(path: str | Path | bytes) -> list[dict]:
    """Return raw regular-season occurrences without reconciling duplicate IDs."""
    try:
        raw = path if isinstance(path, bytes) else Path(path).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed schedule: expected UTF-8 JSON") from exc

    root = _mapping(payload, "root")
    dates = _list(root.get("dates"), "dates")
    occurrences: list[dict] = []
    for date_index, raw_date_entry in enumerate(dates):
        date_entry = _mapping(raw_date_entry, f"dates[{date_index}]")
        raw_games = _list(date_entry.get("games"), f"dates[{date_index}].games")
        for game_index, raw_game in enumerate(raw_games):
            game = _mapping(raw_game, f"dates[{date_index}].games[{game_index}]")
            if game.get("gameType") == "R":
                occurrences.append(game)
    return occurrences


def parse_schedule(
    path: str | Path | bytes, *, preserve_explicit_non_doubleheader: bool = False,
    game_types: tuple[str, ...] | None = ('R',),
) -> list[dict]:
    """Read a UTF-8 MLB schedule JSON file and normalize regular-season games."""
    if type(preserve_explicit_non_doubleheader) is not bool:
        raise ValueError("preserve_explicit_non_doubleheader must be boolean")
    try:
        raw = path if isinstance(path, bytes) else Path(path).read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed schedule: expected UTF-8 JSON") from exc

    root = _mapping(payload, "root")
    dates = _list(root.get("dates"), "dates")
    games_by_pk = {}
    game_order = []
    game_index = 0
    for date_index, raw_date_entry in enumerate(dates):
        date_entry = _mapping(raw_date_entry, f"dates[{date_index}]")
        raw_games = _list(date_entry.get("games"), f"dates[{date_index}].games")
        for raw_game in raw_games:
            game = _normalize_game(
                raw_game,
                date_entry,
                game_index,
                preserve_explicit_non_doubleheader=preserve_explicit_non_doubleheader,
                game_types=game_types,
            )
            game_index += 1
            if game is not None:
                game_pk = game["game_pk"]
                prior = games_by_pk.get(game_pk)
                if prior is None:
                    games_by_pk[game_pk] = game
                    game_order.append(game_pk)
                    continue
                identity_fields = (
                    "game_pk",
                    "away_team_id",
                    "away_team",
                    "home_team_id",
                    "home_team",
                )
                if any(prior[field] != game[field] for field in identity_fields):
                    raise ValueError("malformed schedule: conflicting duplicate gamePk")
                if prior == game:
                    continue
                played = {"Final", "Completed Early"}
                prior_played = prior["status"] in played
                game_played = game["status"] in played
                if prior_played and game_played:
                    comparable_fields = set(prior) | set(game)
                    comparable_fields.discard("scheduled_start_utc")
                    if any(prior.get(field) != game.get(field) for field in comparable_fields):
                        raise ValueError("malformed schedule: conflicting duplicate gamePk")
                    if game["scheduled_start_utc"] < prior["scheduled_start_utc"]:
                        games_by_pk[game_pk] = game
                    continue
                if game_played or not prior_played:
                    games_by_pk[game_pk] = game
    return [games_by_pk[game_pk] for game_pk in game_order]


def validate_schedule_query_coverage(
    path: str | Path | bytes, start: str, end: str, *, game_types: tuple[str, ...] | None = ('R',)
) -> None:
    """Require query-date coverage, allowing only result-free moved cancellations."""
    raw = path if isinstance(path, bytes) else Path(path).read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed schedule: expected UTF-8 JSON") from exc
    root = _mapping(payload, "root")
    dates = _list(root.get("dates"), "dates")
    for date_index, raw_date_entry in enumerate(dates):
        date_entry = _mapping(raw_date_entry, f"dates[{date_index}]")
        query_date = date_entry.get("date")
        try:
            parsed_query_date = date.fromisoformat(query_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed schedule: query date must be ISO format") from exc
        if parsed_query_date.isoformat() != query_date or not start <= query_date <= end:
            raise ValueError("schedule payload contains a game outside its query")
        raw_games = _list(date_entry.get("games"), f"dates[{date_index}].games")
        for game_index, raw_game in enumerate(raw_games):
            game = _mapping(raw_game, f"dates[{date_index}].games[{game_index}]")
            if game_types is not None and game.get("gameType") not in game_types:
                continue
            official_date = game.get("officialDate", query_date)
            if start <= official_date <= end:
                continue
            status = _mapping(
                game.get("status"), f"dates[{date_index}].games[{game_index}].status"
            )
            status_name = _required(
                status,
                "detailedState",
                str,
                f"dates[{date_index}].games[{game_index}].status",
            )
            teams = _mapping(
                game.get("teams"), f"dates[{date_index}].games[{game_index}].teams"
            )
            away = _mapping(
                teams.get("away"),
                f"dates[{date_index}].games[{game_index}].teams.away",
            )
            home = _mapping(
                teams.get("home"),
                f"dates[{date_index}].games[{game_index}].teams.home",
            )
            linescore_value = game.get("linescore")
            linescore = (
                {}
                if linescore_value is None
                else _mapping(
                    linescore_value,
                    f"dates[{date_index}].games[{game_index}].linescore",
                )
            )
            raw_result_fields = (
                away.get("score"),
                home.get("score"),
                away.get("isWinner"),
                home.get("isWinner"),
                linescore.get("currentInning"),
                linescore.get("outs"),
            )
            if status_name not in OUT_OF_RANGE_UNPLAYED_STATUSES or any(
                value is not None for value in raw_result_fields
            ):
                raise ValueError("schedule payload contains a game outside its query")
