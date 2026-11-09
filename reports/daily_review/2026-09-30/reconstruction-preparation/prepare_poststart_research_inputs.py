#!/usr/bin/env python3
"""UNEXECUTED_NOT_PILOT_READY preparation scaffold; do not invoke.

The live history/evaluator binding contracts disagree on source-binding
cardinality. This draft has not acquired target-game data and is not a runnable
pilot. See input-mapping.md and blocker-evidence.json.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from mlb_backtest.live_history import build_live_history, load_live_career_bf
from mlb_backtest.live_model_card import load_frozen_live_card_bytes
from mlb_backtest.live_sources import fetch_live_http
from mlb_backtest.live_top3_contracts import LiveHistoryBundle, SourceBinding, canonical_json_bytes


PREP_ROOT = Path("reports/daily_review/2026-09-30/reconstruction-preparation")
MODEL_CARD = Path("audit/v5_1_bt/model_card.json")
HISTORY_SCHEMA = "POSTSTART_RESEARCH_D1_HISTORY_V1"
SOURCE_SCHEMA = "POSTSTART_RESEARCH_LINEUP_SOURCE_V1"
PROJECTION_SCHEMA = "POSTSTART_RESEARCH_PROJECTION_V1"
CAREER_SCHEMA = "POSTSTART_RESEARCH_CAREER_BF_V1"
REQUIRED_HISTORY_ROLES = {
    "HISTORY_SCHEDULE", "HISTORY_STATCAST", "HISTORY_ERA", "HISTORY_VENUE"
}
PITCH_KEYS = {
    "game_date", "game_pk", "pitcher_id", "batter_id", "events", "description",
    "p_throws", "home_team", "away_team", "inning", "inning_topbot",
    "release_speed", "woba_value", "woba_denom", "estimated_woba_using_speedangle",
    "launch_speed_angle", "at_bat_number", "pitch_number", "home_score",
    "away_score", "outs_when_up", "game_type", "event_id",
}
SCHEDULE_ROW_KEYS = {
    "game_pk", "official_date", "team_codes", "home_team_code", "venue_name", "innings"
}
SCHEDULE_FIELDS = (
    "dates,date,games,gamePk,officialDate,gameDate,gameType,teams,away,home,team,id,name,venue,venue,id,name"
)
BOX_FIELDS = (
    "teams,away,home,team,id,name,players,person,battingOrder,pitchers,jerseyNumber,"
    "parentTeamId,batters,bench,bullpen,teamStats,info,note,stats,seasonStats,"
    "position,status,gameStatus,allPositions"
)
PROVIDER_CONTRACT_VERSION = "MLB_STATSAPI_BOX_SCORE_MASK_V1"
EMPTY_CONTAINER_PATHS = (
    "teams.*.teamStats", "teams.*.info[]", "teams.*.players.*.stats",
    "teams.*.players.*.seasonStats", "teams.*.players.*.position",
    "teams.*.players.*.status", "teams.*.players.*.gameStatus",
    "teams.*.players.*.allPositions[]",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z").rstrip("0").replace(".Z", "Z")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _seal(value: dict[str, object]) -> bytes:
    unsigned = dict(value)
    unsigned.pop("content_sha256", None)
    sealed = {**unsigned, "content_sha256": _sha(canonical_json_bytes(unsigned))}
    return canonical_json_bytes(sealed)


def _json(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}: malformed JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected object")
    return value


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label}: expected positive integer")
    return value


def _only(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not set(value) <= keys:
        raise ValueError(f"{label}: unexpected fields")
    return value


def _fetch(url: str, label: str, contract_version: str) -> tuple[dict[str, object], dict[str, object]]:
    response = fetch_live_http("stats_api", url, max_bytes=64 * 1024 * 1024)
    if response.http_status != 200 or response.requested_url != url or response.final_url != url:
        raise ValueError(f"{label}: HTTP response identity differs")
    body = _json(response.body, label)
    receipt = {
        "requested_url": response.requested_url,
        "final_url": response.final_url,
        "body_sha256": _sha(response.body),
        "http_status": response.http_status,
        "started_at_utc": response.started_at_utc,
        "response_completed_at_utc": response.response_completed_at_utc,
        "field_mask_contract_version": contract_version,
    }
    return body, receipt


def _history_payload(bundle: LiveHistoryBundle, target: str, target_game_pks: set[int]) -> dict[str, object]:
    cutoff = (date.fromisoformat(target).toordinal() - 1)
    cutoff_date = date.fromordinal(cutoff).isoformat()
    if not isinstance(bundle, LiveHistoryBundle):
        raise ValueError("history source returned unexpected bundle type")
    if bundle.target_date != target or bundle.cutoff_date != cutoff_date:
        raise ValueError("history target/cutoff differs")
    roles = [binding.binding_role for binding in bundle.source_bindings]
    if len(bundle.source_bindings) != 4 or set(roles) != REQUIRED_HISTORY_ROLES or len(set(roles)) != 4:
        raise ValueError("history must preserve exactly the original four source bindings")
    for binding in bundle.source_bindings:
        if not isinstance(binding, SourceBinding):
            raise ValueError("history source binding type differs")
        identity = json.loads(binding.query_identity_json)
        if identity.get("as_of_date") != cutoff_date:
            raise ValueError("history source binding as_of_date differs")

    history = bundle.feature_history
    pitches = []
    for row in history.pitches:
        value = dict(row)
        if set(value) - (PITCH_KEYS | {"outs_recorded"}):
            raise ValueError("history pitch has unexpected internal fields")
        value.pop("outs_recorded", None)
        if set(value) != PITCH_KEYS or value["game_date"] > cutoff_date or value["game_pk"] in target_game_pks:
            raise ValueError("history pitch schema/as-of differs")
        pitches.append(value)
    pas = []
    for row in history.plate_appearances:
        value = dict(row)
        if set(value) - (PITCH_KEYS | {"outs_recorded"}):
            raise ValueError("history PA has unexpected internal fields")
        value.pop("outs_recorded", None)
        if set(value) != PITCH_KEYS or value["game_date"] > cutoff_date or value["game_pk"] in target_game_pks:
            raise ValueError("history PA schema/as-of differs")
        pas.append(value)
    schedule = []
    for row in history.schedule_history.rows:
        value = dict(row)
        if any(key in value for key in ("score", "winner", "result", "away_score", "home_score")):
            raise ValueError("history schedule row contains outcome data")
        try:
            projected = {key: value[key] for key in SCHEDULE_ROW_KEYS}
        except KeyError as exc:
            raise ValueError("history schedule row lacks required identity") from exc
        if projected["official_date"] > cutoff_date or projected["game_pk"] in target_game_pks:
            raise ValueError("history schedule row exceeds D-1")
        schedule.append(projected)
    feature = {
        "slot_weights": {str(slot): history.slot_weights[slot] for slot in range(1, 10)},
        "statcast_coverage_complete": history.statcast_coverage_complete,
        "pitches": pitches,
        "plate_appearances": pas,
        "schedule_rows": schedule,
        "schedule_coverage_start": history.schedule_history.coverage_start.isoformat(),
        "schedule_coverage_end": history.schedule_history.coverage_end.isoformat(),
        "schedule_coverage_complete": history.schedule_history.coverage_complete,
    }
    if feature["statcast_coverage_complete"] is not True or feature["schedule_coverage_complete"] is not True or feature["schedule_coverage_end"] != cutoff_date:
        raise ValueError("history completeness differs")
    return {
        "schema": HISTORY_SCHEMA,
        "content_sha256": "",
        "target_date": target,
        "cutoff_date": cutoff_date,
        "cache_authority": dict(bundle.cache_authority),
        "source_bindings": [asdict(binding) for binding in bundle.source_bindings],
        "team_code_by_name": dict(bundle.team_code_by_name),
        "venue_references": {str(key): dict(value) for key, value in bundle.venue_references.items()},
        "league_era_by_asof": {str(key): dict(value) for key, value in bundle.league_era_by_asof.items()},
        "feature_history": feature,
    }


def _schedule_url(target: str) -> str:
    query = urlencode({"sportId": 1, "gameType": "R", "startDate": target,
                       "endDate": target, "fields": SCHEDULE_FIELDS})
    return f"https://statsapi.mlb.com/api/v1/schedule?{query}"


def _schedule_games(payload: dict[str, object], target: str) -> list[dict[str, object]]:
    if set(payload) != {"dates"} or not isinstance(payload["dates"], list):
        raise ValueError("masked schedule root differs")
    games: list[dict[str, object]] = []
    seen: set[int] = set()
    for entry in payload["dates"]:
        entry = _only(entry, {"date", "games"}, "schedule date")
        if entry.get("date") != target or not isinstance(entry.get("games"), list):
            raise ValueError("schedule date differs")
        for item in entry["games"]:
            item = _only(item, {"gamePk", "officialDate", "gameDate", "gameType", "teams", "venue"}, "schedule game")
            pk = _positive(item.get("gamePk"), "schedule gamePk")
            if pk in seen:
                raise ValueError("schedule contains duplicate gamePk for target date")
            seen.add(pk)
            if item.get("gameType") != "R" or item.get("officialDate") != target:
                raise ValueError("schedule target identity differs")
            game_date = item.get("gameDate")
            if not isinstance(game_date, str):
                raise ValueError("schedule gameDate differs")
            scheduled_start = _canonical_utc(game_date)
            local_game_date = datetime.fromisoformat(scheduled_start.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            teams = item.get("teams")
            if not isinstance(teams, dict) or set(teams) != {"away", "home"}:
                raise ValueError("schedule teams differ")
            projected = {"game_pk": pk, "target_date": target, "game_type": "R"}
            for side in ("away", "home"):
                side_obj = _only(teams[side], {"team"}, f"schedule {side}")
                team = _only(side_obj.get("team"), {"id", "name"}, f"schedule {side} team")
                projected[f"{side}_team_id"] = _positive(team.get("id"), f"schedule {side} team id")
                name = team.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("schedule team name differs")
                projected[f"{side}_team"] = name
            if projected["away_team_id"] == projected["home_team_id"]:
                raise ValueError("schedule teams must differ")
            venue = _only(item.get("venue"), {"id", "name"}, "schedule venue")
            projected["venue_id"] = _positive(venue.get("id"), "schedule venue id")
            venue_name = venue.get("name")
            if not isinstance(venue_name, str) or not venue_name:
                raise ValueError("schedule venue name differs")
            projected["venue_name"] = venue_name
            projected["scheduled_start_utc"] = scheduled_start
            projected["scheduled_date_matches_target"] = local_game_date == target
            games.append(projected)
    if not games:
        raise ValueError("target schedule contains no R games")
    return sorted(games, key=lambda game: int(game["game_pk"]))


def _canonical_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled gameDate is malformed") from exc
    if parsed.tzinfo is None:
        raise ValueError("scheduled gameDate must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _game_url(game_pk: int) -> str:
    return f"https://statsapi.mlb.com/api/v1/game/{game_pk}/feed/live?" + urlencode({"fields": FEED_FIELDS})


def _game_data(payload: dict[str, object], game: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    if set(payload) != {"gameData", "liveData"}:
        raise ValueError("masked feed contains unexpected root fields")
    gd = _only(payload["gameData"], {"game", "datetime", "teams", "venue"}, "gameData")
    game_obj = _only(gd.get("game"), {"pk"}, "gameData game")
    if game_obj.get("pk") != game["game_pk"]:
        raise ValueError("feed gamePk differs")
    dt = _only(gd.get("datetime"), {"officialDate", "dateTime"}, "gameData datetime")
    if dt.get("officialDate") != game["target_date"]:
        raise ValueError("feed official date differs from target schedule")
    start = dt.get("dateTime")
    if not isinstance(start, str):
        raise ValueError("feed scheduled datetime missing")
    try:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("feed scheduled datetime malformed") from exc
    if parsed.tzinfo is None:
        raise ValueError("feed scheduled datetime must be timezone-aware")
    start_utc = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    teams = _only(gd.get("teams"), {"away", "home"}, "gameData teams")
    for side in ("away", "home"):
        side_obj = _only(teams[side], {"id", "name"}, f"gameData {side}")
        if side_obj.get("id") != game[f"{side}_team_id"] or side_obj.get("name") != game[f"{side}_team"]:
            raise ValueError("feed team identity differs")
    venue = _only(gd.get("venue"), {"id", "name"}, "gameData venue")
    if venue.get("id") != game["venue_id"] or venue.get("name") != game["venue_name"]:
        raise ValueError("feed venue identity differs")

    live = _only(payload["liveData"], {"boxscore"}, "liveData")
    boxscore = _only(live.get("boxscore"), {"teams"}, "boxscore")
    sides = _only(boxscore.get("teams"), {"away", "home"}, "boxscore teams")
    lineups: dict[str, object] = {}
    starters: dict[str, int] = {}
    for side in ("away", "home"):
        side_box = _only(sides[side], {"team", "pitchers", "players"}, f"boxscore {side}")
        team = _only(side_box.get("team"), {"id"}, f"boxscore {side} team")
        if team.get("id") != game[f"{side}_team_id"]:
            raise ValueError("boxscore team identity differs")
        pitchers = side_box.get("pitchers")
        if not isinstance(pitchers, list) or not pitchers:
            raise ValueError(f"MISSING_{side.upper()}_STARTER")
        pitcher_ids = [_positive(value, f"boxscore {side} pitcher") for value in pitchers]
        if len(pitcher_ids) != len(set(pitcher_ids)):
            raise ValueError(f"AMBIGUOUS_{side.upper()}_STARTER")
        starters[f"{side}_starter_id"] = pitcher_ids[0]
        players = side_box.get("players")
        if not isinstance(players, dict):
            raise ValueError(f"MISSING_{side.upper()}_LINEUP")
        by_order: dict[int, int] = {}
        ambiguous = False
        for player in players.values():
            player = _only(player, {"person", "battingOrder"}, f"boxscore {side} player")
            order = player.get("battingOrder")
            if order is None:
                continue
            person = _only(player.get("person"), {"id"}, f"boxscore {side} person")
            player_id = _positive(person.get("id"), f"boxscore {side} player id")
            if order not in {f"{slot}00" for slot in range(1, 10)}:
                ambiguous = True
                continue
            slot = int(order[0])
            if slot in by_order or player_id in by_order.values():
                ambiguous = True
            by_order[slot] = player_id
        if ambiguous or set(by_order) != set(range(1, 10)) or len(set(by_order.values())) != 9:
            raise ValueError(f"AMBIGUOUS_{side.upper()}_STARTING_NINE")
        lineups[f"{side}_starting_nine"] = [by_order[slot] for slot in range(1, 10)]

    lineup_source = {
        "schema": SOURCE_SCHEMA, "content_sha256": "",
        "target_date": game["target_date"], "game_pk": game["game_pk"],
        "scheduled_start_utc": start_utc, "away_team": game["away_team"],
        "home_team": game["home_team"], "away_team_id": game["away_team_id"],
        "home_team_id": game["home_team_id"],
        "away_starting_nine": lineups["away_starting_nine"],
        "home_starting_nine": lineups["home_starting_nine"],
        "away_starter_id": starters["away_starter_id"],
        "home_starter_id": starters["home_starter_id"],
        "source_retrieved_at_utc": _utc_now(),
    }
    return lineup_source, {"scheduled_start_utc": start_utc}


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)


def prepare(root: Path, target: str, game_pk: int | None, model_card_path: Path) -> Path:
    date.fromisoformat(target)
    card_raw = model_card_path.read_bytes()
    card = load_frozen_live_card_bytes(card_raw)
    history = build_live_history(root, target, card)
    history_bytes = _seal(_history_payload(history, target))
    schedule_url = _schedule_url(target)
    schedule_payload, schedule_fetch = _fetch(schedule_url, "masked schedule")
    games = _schedule_games(schedule_payload, target)
    if game_pk is not None:
        games = [game for game in games if game["game_pk"] == game_pk]
        if not games:
            raise ValueError("selected gamePk is not in the masked target-date R schedule")

    base = PREP_ROOT / target
    history_relative = base / "history.json"
    # Creating the history file is exclusive: reruns need an explicit new output directory/review.
    _write_new(root / history_relative, history_bytes)
    manifest: dict[str, object] = {
        "schema": "POSTSTART_RECONSTRUCTION_PREPARATION_V1",
        "target_date": target,
        "history_bundle": {"relative": history_relative.as_posix(), "sha256": _sha(history_bytes)},
        "model_card": {"relative": model_card_path.as_posix(), "sha256": _sha(card_raw)},
        "schedule_fetch": schedule_fetch,
        "games": [],
    }
    for game in games:
        row: dict[str, object] = {**game, "state": "UNSCORED", "reason_codes": []}
        feed_url = _game_url(int(game["game_pk"]))
        try:
            feed_payload, feed_fetch = _fetch(feed_url, "masked game feed")
            source, schedule = _game_data(feed_payload, game)
            source_bytes = _seal(source)
            source_relative = base / str(game["game_pk"]) / "lineup-source.json"
            _write_new(root / source_relative, source_bytes)
            projection = {
                "schema": PROJECTION_SCHEMA, "content_sha256": "",
                "target_date": target, "game_pk": game["game_pk"],
                "scheduled_start_utc": schedule["scheduled_start_utc"],
                "away_team": game["away_team"], "home_team": game["home_team"],
                "away_team_id": game["away_team_id"], "home_team_id": game["home_team_id"],
                "venue_id": game["venue_id"], "venue_name": game["venue_name"],
                "game_type": "R", "away_starting_nine": source["away_starting_nine"],
                "home_starting_nine": source["home_starting_nine"],
                "away_starter_id": source["away_starter_id"],
                "home_starter_id": source["home_starter_id"],
                "source_relative": source_relative.as_posix(),
                "source_sha256": _sha(source_bytes),
                "reconstructed_at_utc": source["source_retrieved_at_utc"],
            }
            projection_bytes = _seal(projection)
            projection_relative = base / str(game["game_pk"]) / "projection.json"
            _write_new(root / projection_relative, projection_bytes)
            career_values, career_bindings = load_live_career_bf(
                root, target, [source["away_starter_id"], source["home_starter_id"]]
            )
            cutoff = (date.fromordinal(date.fromisoformat(target).toordinal() - 1)).isoformat()
            career = _seal({
                "schema": CAREER_SCHEMA, "content_sha256": "", "target_date": target,
                "as_of_date": cutoff,
                "pitcher_bf": {str(pid): int(career_values[pid]) for pid in sorted(career_values)},
            })
            career_relative = base / str(game["game_pk"]) / "career-bf.json"
            _write_new(root / career_relative, career)
            row.update({
                "state": "READY", "feed_fetch": feed_fetch,
                "lineup_source": {"relative": source_relative.as_posix(), "sha256": _sha(source_bytes)},
                "participant_projection": {"relative": projection_relative.as_posix(), "sha256": _sha(projection_bytes)},
                "career_bf": {"relative": career_relative.as_posix(), "sha256": _sha(career)},
                "career_source_bindings": [asdict(binding) for binding in career_bindings],
            })
        except (KeyError, TypeError, ValueError) as exc:
            row["reason_codes"] = [str(exc) or "RECONSTRUCTION_INPUT_INVALID"]
        manifest["games"].append(row)

    manifest_raw = _seal(manifest)
    manifest_relative = base / "manifest.json"
    _write_new(root / manifest_relative, manifest_raw)
    return root / manifest_relative


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--target-date", required=True)
    parser.add_argument("--game-pk", type=int, help="select one exact target-date game for the reviewed pilot")
    parser.add_argument("--model-card", type=Path, default=MODEL_CARD)
    args = parser.parse_args()
    manifest = prepare(args.root, args.target_date, args.game_pk, args.model_card)
    print(manifest.relative_to(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
