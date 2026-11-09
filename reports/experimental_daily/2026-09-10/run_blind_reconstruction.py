"""One-off, outcome-blind retrospective participant reconstruction for 2026-09-10.

This is analysis evidence, not a production command.  It deliberately uses
the historical reconstructed-participant feature entrypoint rather than the
pregame-only live service: the latter correctly rejects started games.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from mlb_backtest.feature_engine import build_raw_game_features
from mlb_backtest.live_history import build_live_history
from mlb_backtest.live_model_card import load_frozen_live_card_bytes
from mlb_backtest.live_sources import parse_live_schedule
from mlb_backtest.scoring import WEIGHTS, home_probability, predicted_pick
from mlb_backtest.scoring_prep import score_raw_game


ROOT = Path(__file__).resolve().parents[3]
MORNING_REPORT = ROOT / (
    "reports/experimental_daily/2026-09-10/"
    "20260910T095522360063Z-554f9b8760a2e5a2.json"
)
GAME_PKS = (824872, 823413)
SLOTS = tuple(range(100, 1000, 100))
FIELDS = (
    "gamePk,gameData,teams,away,home,abbreviation,liveData,boxscore,"
    "players,id,person,fullName,battingOrder,position,code,pitchers"
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc_now() -> str:
    instant = datetime.now(timezone.utc)
    return instant.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _forbid_network(_source: str, _url: str, *, max_bytes: int):
    raise RuntimeError("D-1 history cache was incomplete; blind reconstruction refused network")


def _positive(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"masked participant {label} differs")
    return value


def _masked_participants(game_pk: int) -> dict[str, object]:
    query = urlencode({"fields": FIELDS})
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live?{query}"
    request = Request(url, headers={"Accept": "application/json"})
    acquired_at_utc = _utc_now()
    with urlopen(request, timeout=30) as response:  # nosec B310: fixed HTTPS allowlist
        if response.status != 200:
            raise RuntimeError("masked participant endpoint status differs")
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("masked participant endpoint exceeds bound")
    try:
        payload = json.loads(raw.decode("utf-8"))
        teams = payload["liveData"]["boxscore"]["teams"]
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("masked participant payload differs") from exc
    result: dict[str, object] = {
        "game_pk": game_pk,
        "acquired_at_utc": acquired_at_utc,
        "masked_url": url,
        "masked_payload_sha256": hashlib.sha256(raw).hexdigest(),
        "sides": {},
    }
    for side in ("away", "home"):
        team = teams.get(side) if isinstance(teams, dict) else None
        if not isinstance(team, dict):
            raise ValueError(f"masked participant {side} team differs")
        pitchers = team.get("pitchers")
        players = team.get("players")
        if not isinstance(pitchers, list) or not pitchers or not isinstance(players, dict):
            raise ValueError(f"masked participant {side} fields differ")
        by_slot: dict[int, dict[str, object]] = {}
        for player in players.values():
            if not isinstance(player, dict):
                raise ValueError(f"masked participant {side} player differs")
            order = player.get("battingOrder")
            if not isinstance(order, str) or not order.isdecimal() or int(order) not in SLOTS:
                continue
            person = player.get("person")
            if not isinstance(person, dict):
                raise ValueError(f"masked participant {side} person differs")
            slot = int(order)
            if slot in by_slot:
                raise ValueError(f"masked participant {side} lineup is ambiguous")
            by_slot[slot] = {
                "player_id": _positive(person.get("id"), f"{side} player id"),
                "player_name": person.get("fullName") if isinstance(person.get("fullName"), str) else None,
                "batting_order": str(slot),
            }
        if tuple(sorted(by_slot)) != SLOTS:
            raise ValueError(f"masked participant {side} lacks exact original nine")
        result["sides"][side] = {
            "starter_id": _positive(pitchers[0], f"{side} starter id"),
            "lineup": [by_slot[slot] for slot in SLOTS],
        }
    return result


def main() -> None:
    morning = json.loads(MORNING_REPORT.read_text(encoding="utf-8"))
    if morning.get("target_date") != "2026-09-10" or morning.get("history_cutoff_date") != "2026-09-09":
        raise ValueError("morning report target/cutoff differs")
    card_path = ROOT / str(morning["command_inputs"]["model_card"])
    card_bytes = card_path.read_bytes()
    card = load_frozen_live_card_bytes(card_bytes)
    if card.file_sha256 != morning.get("model_card_sha256"):
        raise ValueError("morning model card differs")
    live_binding = next(
        binding for binding in morning["source_bindings"]
        if binding.get("binding_role") == "LIVE_SCHEDULE"
    )
    schedule_raw = (ROOT / str(live_binding["payload_relative"])).read_bytes()
    schedule = {
        int(game["game_pk"]): dict(game)
        for game in parse_live_schedule(schedule_raw, requested_date="2026-09-10")
    }
    if set(GAME_PKS) - set(schedule):
        raise ValueError("morning schedule population differs")
    # A cache miss is intentionally a hard failure; D-1 history must be the
    # previously sealed morning authority, never refreshed for this analysis.
    history = build_live_history(ROOT, "2026-09-10", card, fetcher=_forbid_network)
    if dict(history.cache_authority) != dict(morning["cache_authority"]):
        raise ValueError("morning D-1 history authority differs")
    results = []
    for game_pk in GAME_PKS:
        participants = _masked_participants(game_pk)
        source_game = schedule[game_pk]
        reconstructed = dict(source_game)
        for side in ("away", "home"):
            extracted = participants["sides"][side]
            reconstructed[f"{side}_lineup"] = [
                {"player_id": player["player_id"]} for player in extracted["lineup"]
            ]
            reconstructed[f"{side}_actual_starter_id"] = extracted["starter_id"]
        reconstructed["starter_provenance"] = "RECONSTRUCTED_ACTUAL_STARTER"
        raw = build_raw_game_features(
            reconstructed,
            history.feature_history,
            history.team_code_by_name,
            history.league_era_by_asof,
        )
        if raw.get("eligible") is not True:
            raise ValueError(f"reconstructed game {game_pk} is not eligible: {raw.get('reasons')}")
        scored = score_raw_game(raw, card.normalization)
        edge = float(scored["edge"])
        p_home = float(home_probability(edge, card.alpha, card.beta))
        pick = predicted_pick(str(source_game["home_team"]), str(source_game["away_team"]), p_home)
        contributions = {
            component: WEIGHTS[component]
            * (float(scored["home_scores"][component]) - float(scored["away_scores"][component]))
            for component in WEIGHTS
        }
        results.append({
            "game_pk": game_pk,
            "away_team": source_game["away_team"],
            "home_team": source_game["home_team"],
            "scheduled_start_utc": source_game["scheduled_start_utc"],
            "participant_provenance": "RECONSTRUCTED_FINAL_PARTICIPANTS",
            "participant_source": participants,
            "feature_sha256": hashlib.sha256(_canonical_bytes(raw)).hexdigest(),
            "away_component_scores": scored["away_scores"],
            "home_component_scores": scored["home_scores"],
            "component_qualities": scored["qualities"],
            "home_minus_away_contributions": contributions,
            "total_edge": edge,
            "p_home": p_home,
            "predicted_side": pick["predicted_side"],
            "pick_probability": pick["pick_probability"],
        })
    report = {
        "schema": "EXPERIMENTAL_BLIND_RETROSPECTIVE_RECONSTRUCTION_V1",
        "research_only": True,
        "not_prospective": True,
        "not_betting_validated": True,
        "target_date": "2026-09-10",
        "history_cutoff_date": "2026-09-09",
        "reconstruction_generated_at_utc": _utc_now(),
        "model_card_sha256": card.file_sha256,
        "cache_authority": dict(history.cache_authority),
        "morning_report_relative": MORNING_REPORT.relative_to(ROOT).as_posix(),
        "method": (
            "Historical pure feature/scoring path using post-start participant-only "
            "masked fields. No target-date statistics, plays, scores, or outcomes are inputs."
        ),
        "games": results,
    }
    report["content_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(__file__).with_name(f"blind-reconstruction-{stem}-{report['content_sha256'][:16]}.json")
    output.write_bytes(_canonical_bytes(report) + b"\n")
    print(output)


if __name__ == "__main__":
    main()
