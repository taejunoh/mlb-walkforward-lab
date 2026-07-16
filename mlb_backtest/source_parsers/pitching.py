"""Pure, offline MLB pitching reference parsing."""

from __future__ import annotations

import json
from pathlib import Path


def _pitcher_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("pitcher_id must be a positive integer")
    return value


def parse_prior_career_bf(source: str | Path | bytes, pitcher_id: int) -> int:
    """Return the unique all-sport aggregate, avoiding duplicated team splits."""
    _pitcher_id(pitcher_id)
    raw = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("career BF payload must be UTF-8 JSON") from exc
    if not isinstance(payload, dict) or "stats" not in payload:
        raise ValueError("career BF payload root is invalid")
    stats = payload["stats"]
    if stats == []:
        return 0
    if not isinstance(stats, list) or len(stats) != 1 or not isinstance(stats[0], dict):
        raise ValueError("career BF payload must contain one stats group")
    group = stats[0]
    if (
        not isinstance(group.get("type"), dict)
        or group["type"].get("displayName") != "byDateRange"
        or not isinstance(group.get("group"), dict)
        or group["group"].get("displayName") != "pitching"
        or not isinstance(group.get("splits"), list)
    ):
        raise ValueError("career BF stats group is invalid")
    if group["splits"] == []:
        return 0
    team_bf = []
    team_ids = []
    aggregates = []
    for split in group["splits"]:
        if not isinstance(split, dict):
            raise ValueError("career BF split is invalid")
        sport = split.get("sport")
        if not isinstance(sport, dict) or type(sport.get("id")) is not int:
            raise ValueError("career BF split sport is invalid")
        stat = split.get("stat")
        bf = stat.get("battersFaced") if isinstance(stat, dict) else None
        if type(bf) is not int or bf < 0:
            raise ValueError("career BF split battersFaced is invalid")
        if sport["id"] == 1:
            team = split.get("team")
            team_id = team.get("id") if isinstance(team, dict) else None
            if type(team_id) is not int or team_id <= 0:
                raise ValueError("career BF MLB team split is invalid")
            team_bf.append(bf)
            team_ids.append(team_id)
        elif sport["id"] == 0:
            aggregate_team = split.get("team")
            if aggregate_team is None:
                aggregate_team_id = None
            elif (
                not isinstance(aggregate_team, dict)
                or type(aggregate_team.get("id")) is not int
                or aggregate_team["id"] <= 0
            ):
                raise ValueError("career BF aggregate split is invalid")
            else:
                aggregate_team_id = aggregate_team["id"]
            aggregates.append((bf, aggregate_team_id))
        else:
            raise ValueError("career BF split contains a non-MLB sport")
    if len(aggregates) != 1 or not team_bf:
        raise ValueError("career BF requires exactly one aggregate split")
    total = sum(team_bf)
    aggregate_bf, aggregate_team_id = aggregates[0]
    if aggregate_bf != total:
        raise ValueError("career BF aggregate does not match MLB team splits")
    if aggregate_team_id is not None and (
        len(set(team_ids)) != 1 or aggregate_team_id != team_ids[0]
    ):
        raise ValueError("career BF aggregate team is inconsistent")
    return total


def _payload_bytes(source: str | Path | bytes) -> bytes:
    return source if isinstance(source, bytes) else Path(source).read_bytes()


def parse_league_era(source: str | Path | bytes) -> dict[str, int | float]:
    """Validate one complete pitching group and derive ERA from integer outs."""
    try:
        payload = json.loads(_payload_bytes(source).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("MLB pitching stats must be UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("MLB pitching stats root must be an object")
    stats = payload.get("stats")
    if not isinstance(stats, list) or len(stats) != 1:
        raise ValueError("MLB pitching stats must contain exactly one stats group")
    group = stats[0]
    if not isinstance(group, dict):
        raise ValueError("MLB pitching stats group must be an object")
    group_meta = group.get("group")
    if (
        not isinstance(group_meta, dict)
        or group_meta.get("displayName") != "pitching"
    ):
        raise ValueError("MLB pitching stats group must be pitching")
    total_splits = group.get("totalSplits")
    splits = group.get("splits")
    if type(total_splits) is not int or total_splits < 0:
        raise ValueError("MLB pitching totalSplits must be a nonnegative integer")
    if not isinstance(splits, list) or total_splits != len(splits):
        raise ValueError("MLB pitching stats are truncated or paged")
    earned_runs = 0
    outs = 0
    for index, split in enumerate(splits):
        if not isinstance(split, dict) or not isinstance(split.get("stat"), dict):
            raise ValueError(f"MLB pitching split {index} must contain a stat object")
        stat = split["stat"]
        er = stat.get("earnedRuns")
        split_outs = stat.get("outs")
        if type(er) is not int or er < 0:
            raise ValueError("earnedRuns must be a nonnegative integer")
        if type(split_outs) is not int or split_outs < 0:
            raise ValueError("outs must be a nonnegative integer")
        earned_runs += er
        outs += split_outs
    if outs <= 0:
        raise ValueError("MLB pitching stats must contain positive total outs")
    return {
        "earned_runs": earned_runs,
        "outs": outs,
        "value": 27.0 * earned_runs / outs,
    }
