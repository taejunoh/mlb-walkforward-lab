import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MLB_BASE = "https://statsapi.mlb.com/api/v1"
_USER_AGENT = "mlb-v5.1-backtest/1.0"


def fetch_bytes(url: str, timeout: int = 90) -> bytes:
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def schedule_url(start: str, end: str) -> str:
    query = urlencode(
        {
            "sportId": 1,
            "gameType": "R",
            "startDate": start,
            "endDate": end,
            "hydrate": "probablePitcher,lineups,venue",
        }
    )
    return f"{MLB_BASE}/schedule?{query}"


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


def _normalize_game(raw_game: Any, date_entry: dict, index: int) -> dict | None:
    game = _mapping(raw_game, f"game[{index}]")
    game_type = game.get("gameType")
    if game_type != "R":
        return None

    official_date = game.get("officialDate", date_entry.get("date"))
    if not isinstance(official_date, str) or not official_date:
        raise ValueError("malformed schedule: regular game official date is required")
    try:
        date.fromisoformat(official_date)
    except ValueError as exc:
        raise ValueError("malformed schedule: official date must be ISO format") from exc

    teams = _mapping(game.get("teams"), f"game[{index}].teams")
    away = _mapping(teams.get("away"), f"game[{index}].teams.away")
    home = _mapping(teams.get("home"), f"game[{index}].teams.home")
    away_team = _mapping(away.get("team"), f"game[{index}].teams.away.team")
    home_team = _mapping(home.get("team"), f"game[{index}].teams.home.team")
    status = _mapping(game.get("status"), f"game[{index}].status")
    away_pitcher_id, away_pitcher_name = _optional_person(
        away, "probablePitcher", f"game[{index}].teams.away.probablePitcher"
    )
    home_pitcher_id, home_pitcher_name = _optional_person(
        home, "probablePitcher", f"game[{index}].teams.home.probablePitcher"
    )
    lineups_value = game.get("lineups")
    lineups = (
        {}
        if lineups_value is None
        else _mapping(lineups_value, f"game[{index}].lineups")
    )

    venue_value = game.get("venue")
    if venue_value is None:
        venue_id = venue_name = None
    else:
        venue = _mapping(venue_value, f"game[{index}].venue")
        venue_id = _required(venue, "id", int, f"game[{index}].venue")
        venue_name = _required(venue, "name", str, f"game[{index}].venue")

    return {
        "game_pk": _required(game, "gamePk", int, f"game[{index}]"),
        "official_date": official_date,
        "game_type": "R",
        "scheduled_start_utc": _required(
            game, "gameDate", str, f"game[{index}]"
        ),
        "status": _required(status, "detailedState", str, f"game[{index}].status"),
        "away_team": _required(
            away_team, "name", str, f"game[{index}].teams.away.team"
        ),
        "home_team": _required(
            home_team, "name", str, f"game[{index}].teams.home.team"
        ),
        "away_probable_pitcher_id": away_pitcher_id,
        "away_probable_pitcher_name": away_pitcher_name,
        "home_probable_pitcher_id": home_pitcher_id,
        "home_probable_pitcher_name": home_pitcher_name,
        "venue_id": venue_id,
        "venue_name": venue_name,
        "away_lineup": _lineup(
            lineups, "awayPlayers", f"game[{index}].lineups"
        ),
        "home_lineup": _lineup(
            lineups, "homePlayers", f"game[{index}].lineups"
        ),
    }


def parse_schedule(path: str | Path) -> list[dict]:
    """Read a UTF-8 MLB schedule JSON file and normalize regular-season games."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed schedule: expected UTF-8 JSON") from exc

    root = _mapping(payload, "root")
    dates = _list(root.get("dates"), "dates")
    games = []
    game_index = 0
    for date_index, raw_date_entry in enumerate(dates):
        date_entry = _mapping(raw_date_entry, f"dates[{date_index}]")
        raw_games = _list(date_entry.get("games"), f"dates[{date_index}].games")
        for raw_game in raw_games:
            game = _normalize_game(raw_game, date_entry, game_index)
            game_index += 1
            if game is not None:
                games.append(game)
    return games


def selection_dates(
    games: Iterable[dict], start: str, end: str
) -> list[str]:
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError) as exc:
        raise ValueError("selection date bounds must be ISO dates") from exc
    if start_date > end_date:
        raise ValueError("selection date start must not be after end")

    selected = set()
    for game in games:
        if not isinstance(game, dict):
            raise ValueError("games must contain normalized dictionaries")
        if game.get("game_type") != "R":
            continue
        try:
            official_date = date.fromisoformat(game["official_date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("regular game must have an ISO official_date") from exc
        if start_date <= official_date <= end_date:
            selected.add(official_date.isoformat())
    return sorted(selected)
