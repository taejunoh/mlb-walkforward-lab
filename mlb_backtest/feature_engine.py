"""Deterministic D-1 raw feature aggregation for MLB matchup replay."""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .features.bullpen import leverage_pool, team_availability
from .features.context import schedule_score
from .features.starter import expected_innings
from .prepare import assert_pregame_history
from .statcast import StatcastData, identify_pitcher_roles


OUT_EVENTS = frozenset(
    {
        "field_out",
        "force_out",
        "grounded_into_double_play",
        "fielders_choice_out",
        "double_play",
        "triple_play",
        "sac_fly",
        "sac_bunt",
        "strikeout",
        "strikeout_double_play",
        "other_out",
    }
)
WALK_EVENTS = frozenset({"walk", "intent_walk", "intentional_walk"})
HIT_EVENTS = MappingProxyType({"single": 1, "double": 2, "triple": 3, "home_run": 4})


def _day(value: object, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc


def _metric(
    value: float | int | None, sample_size: int, quality: float | None = None, **extra
) -> dict:
    if value is not None and (
        isinstance(value, bool) or not math.isfinite(float(value))
    ):
        raise ValueError("raw metric values must be finite or None")
    if quality is None:
        quality = 1.0 if sample_size > 0 and value is not None else 0.0
    result = {"value": value, "sample_size": sample_size, "quality": quality}
    result.update(extra)
    return result


def _reliability(sample_size: int, constant: float) -> float:
    if sample_size <= 0:
        return 0.0
    return sample_size / (sample_size + constant)


def pa_expected_woba(row: Mapping[str, object]) -> float | None:
    """Return result-independent PA expected wOBA when its rate denominator counts."""
    denom = row.get("woba_denom")
    if (
        isinstance(denom, bool)
        or not isinstance(denom, (int, float))
        or not math.isfinite(float(denom))
        or denom <= 0
    ):
        return None
    expected = row.get("estimated_woba_using_speedangle")
    if expected is not None:
        if (
            isinstance(expected, bool)
            or not isinstance(expected, (int, float))
            or not math.isfinite(float(expected))
        ):
            raise ValueError("estimated wOBA must be finite or None")
        return float(expected)
    event = str(row.get("events") or "")
    if event not in WALK_EVENTS | {"hit_by_pitch"} | OUT_EVENTS:
        return None
    value = row.get("woba_value")
    if value is None and event in OUT_EVENTS:
        return 0.0
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


class ScheduleHistory:
    """Canonical schedule rows indexed once by team and team/date."""

    def __init__(
        self,
        rows: Iterable[Mapping[str, object]],
        *,
        coverage_start: str | None,
        coverage_end: str | None,
        coverage_complete: bool,
    ) -> None:
        if not isinstance(coverage_complete, bool):
            raise ValueError("schedule coverage_complete must be boolean")
        try:
            start = (
                date.fromisoformat(coverage_start)
                if coverage_start is not None
                else None
            )
            end = date.fromisoformat(coverage_end) if coverage_end is not None else None
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "schedule coverage bounds must be canonical ISO dates"
            ) from exc
        if (
            (start is not None and start.isoformat() != coverage_start)
            or (end is not None and end.isoformat() != coverage_end)
            or (start is not None and end is not None and start > end)
            or (coverage_complete and (start is None or end is None))
        ):
            raise ValueError("schedule coverage bounds are invalid")
        self.coverage_start = start
        self.coverage_end = end
        self.coverage_complete = coverage_complete
        validated = []
        seen_game_pks = set()
        for raw in rows:
            if not isinstance(raw, Mapping):
                raise ValueError("schedule history rows must be mappings")
            row = dict(raw)
            game_pk = row.get("game_pk")
            if type(game_pk) is not int or game_pk <= 0 or game_pk in seen_game_pks:
                raise ValueError(
                    "schedule history game_pk must be unique positive integers"
                )
            seen_game_pks.add(game_pk)
            raw_date = row.get("official_date", row.get("game_date"))
            scheduled = _day(raw_date, "schedule date")
            if not isinstance(raw_date, str) or scheduled.isoformat() != raw_date:
                raise ValueError("schedule history date must be canonical ISO")
            team_codes = row.get("team_codes")
            if (
                isinstance(team_codes, (str, bytes))
                or not isinstance(team_codes, Sequence)
                or not team_codes
                or any(not isinstance(code, str) or not code for code in team_codes)
                or len(set(team_codes)) != len(team_codes)
            ):
                raise ValueError("schedule history team_codes must be unique strings")
            innings = row.get("innings")
            if innings is not None and (type(innings) is not int or innings < 0):
                raise ValueError(
                    "schedule history innings must be a nonnegative integer"
                )
            home_code = row.get("home_team_code")
            if home_code is not None and (
                not isinstance(home_code, str) or home_code not in team_codes
            ):
                raise ValueError("schedule history home_team_code is invalid")
            venue_name = row.get("venue_name")
            if venue_name is not None and (
                not isinstance(venue_name, str) or not venue_name
            ):
                raise ValueError("schedule history venue_name is invalid")
            for field in ("venue_city", "time_zone"):
                value = row.get(field)
                if value is not None and (not isinstance(value, str) or not value):
                    raise ValueError(f"schedule history {field} is invalid")
            for field, low, high in (
                ("venue_latitude", -90.0, 90.0),
                ("venue_longitude", -180.0, 180.0),
            ):
                value = row.get(field)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or not low <= float(value) <= high
                ):
                    raise ValueError(f"schedule history {field} is invalid")
            validated.append(row)
        canonical = tuple(
            sorted(
                validated,
                key=lambda row: (
                    str(row.get("official_date", row.get("game_date", ""))),
                    int(row.get("game_pk", 0)),
                ),
            )
        )
        by_team: defaultdict[str, list] = defaultdict(list)
        by_team_date: defaultdict[tuple[str, date], list] = defaultdict(list)
        dated = []
        for row in canonical:
            scheduled = _day(
                row.get("official_date", row.get("game_date")), "schedule date"
            )
            dated.append(scheduled)
            for team_code in row.get("team_codes", ()):
                code = str(team_code)
                by_team[code].append(row)
                by_team_date[(code, scheduled)].append(row)
        self.rows = canonical
        self.by_team = MappingProxyType(
            {key: tuple(value) for key, value in sorted(by_team.items())}
        )
        self.by_team_date = MappingProxyType(
            {key: tuple(value) for key, value in sorted(by_team_date.items())}
        )
        self._dates = tuple(dated)
        self.game_dates = MappingProxyType(
            {
                int(row["game_pk"]): _day(
                    row.get("official_date", row.get("game_date")), "schedule date"
                )
                for row in canonical
            }
        )
        self.input_max_date = self._dates[-1] if self._dates else None

    def __iter__(self):
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def max_before(self, target: date | None) -> date | None:
        if not self._dates:
            return None
        if target is None:
            return self._dates[-1]
        index = bisect_left(self._dates, target)
        return self._dates[index - 1] if index else None

    def has_date(self, target: date) -> bool:
        index = bisect_left(self._dates, target)
        return index < len(self._dates) and self._dates[index] == target

    def has_game(self, game_pk: int, target: date) -> bool:
        return self.game_dates.get(game_pk) == target

    def has_future(self, target: date) -> bool:
        return bool(self._dates and self._dates[-1] > target)

    def covers(self, first: date, last: date) -> bool:
        return bool(
            self.coverage_complete
            and self.coverage_start is not None
            and self.coverage_end is not None
            and self.coverage_start <= first <= last <= self.coverage_end
        )


def actual_starter_index(
    statcast_or_pitches: StatcastData | Iterable[Mapping[str, object]],
) -> dict[int, dict[str, int]]:
    """Reduce target-game pitches to authoritative first-pitcher identities only."""
    pitches = (
        statcast_or_pitches.pitches
        if isinstance(statcast_or_pitches, StatcastData)
        else tuple(statcast_or_pitches)
    )
    ordered = sorted(
        pitches,
        key=lambda row: (
            str(row["game_date"]),
            int(row["game_pk"]),
            int(row["inning"]),
            0 if row["inning_topbot"] == "Top" else 1,
            int(row["at_bat_number"]),
            int(row.get("pitch_number", 0)),
        ),
    )
    indexed: defaultdict[int, dict[str, int]] = defaultdict(dict)
    for row in ordered:
        game_pk = int(row["game_pk"])
        side = "home" if row["inning_topbot"] == "Top" else "away"
        indexed[game_pk].setdefault(f"{side}_actual_starter_id", int(row["pitcher_id"]))
    return {game_pk: dict(values) for game_pk, values in sorted(indexed.items())}


def enrich_schedule_game_participants(
    normalized_game: Mapping[str, object],
    starter_index: Mapping[int, Mapping[str, int]],
) -> dict[str, object]:
    """Return a schedule-game copy enriched only with reconstructed participants."""
    if not isinstance(normalized_game, Mapping) or not isinstance(
        starter_index, Mapping
    ):
        raise ValueError("normalized game and starter index must be mappings")
    enriched = dict(normalized_game)
    game_pk = enriched.get("game_pk")
    participants = starter_index.get(game_pk, {}) if type(game_pk) is int else {}
    complete = True
    for side in ("away", "home"):
        actual = participants.get(f"{side}_actual_starter_id")
        if type(actual) is not int or actual <= 0:
            actual = None
            complete = False
        enriched[f"{side}_actual_starter_id"] = actual
        probable = enriched.get(f"{side}_probable_pitcher_id")
        enriched[f"{side}_probable_starter_mismatch"] = (
            actual is not None and probable != actual
        )
    enriched["starter_provenance"] = (
        "RECONSTRUCTED_ACTUAL_STARTER"
        if complete
        else "MISSING_ACTUAL_STARTER_ENRICHMENT"
    )
    return enriched


class FeatureHistory:
    """Canonical, indexed Statcast and schedule history used by raw feature builds."""

    def __init__(
        self,
        statcast: StatcastData,
        *,
        slot_weights: Mapping[int, float],
        schedule_history: Iterable[Mapping[str, object]] | ScheduleHistory = (),
        schedule_coverage_start: str | None = None,
        schedule_coverage_end: str | None = None,
        schedule_coverage_complete: bool = False,
        venue_by_name: Mapping[str, Mapping[str, object]] | None = None,
        statcast_coverage_complete: bool = True,
    ) -> None:
        if not isinstance(statcast, StatcastData):
            raise ValueError("statcast must be StatcastData")
        if not isinstance(statcast_coverage_complete, bool):
            raise ValueError("statcast_coverage_complete must be boolean")
        if set(slot_weights) != set(range(1, 10)):
            raise ValueError("slot weights must contain slots 1 through 9")
        weights = {i: float(slot_weights[i]) for i in range(1, 10)}
        if any(
            not math.isfinite(v) or v < 0 for v in weights.values()
        ) or not math.isclose(sum(weights.values()), 1.0, abs_tol=1e-12):
            raise ValueError("slot weights must be finite, nonnegative, and sum to 1")
        self.slot_weights = MappingProxyType(weights)
        self.statcast_coverage_complete = statcast_coverage_complete
        self.pitches = tuple(sorted(statcast.pitches, key=self._pitch_key))
        self.plate_appearances = tuple(
            sorted(statcast.plate_appearances, key=self._pitch_key)
        )
        self.roles = MappingProxyType(identify_pitcher_roles(self.pitches))
        self.schedule_history = (
            schedule_history
            if isinstance(schedule_history, ScheduleHistory)
            else ScheduleHistory(
                schedule_history,
                coverage_start=schedule_coverage_start,
                coverage_end=schedule_coverage_end,
                coverage_complete=schedule_coverage_complete,
            )
        )
        self.venue_by_name = MappingProxyType(dict(venue_by_name or {}))
        self.pa_by_pitcher = self._index(self.plate_appearances, "pitcher_id")
        self.pa_by_batter = self._index(self.plate_appearances, "batter_id")
        self.pitches_by_pitcher = self._index(self.pitches, "pitcher_id")
        self.pitches_by_pitcher_game = self._pitcher_game_index(self.pitches)
        self.pa_by_date = self._index_value(self.plate_appearances, "game_date", str)
        self.pitches_by_date = self._index_value(self.pitches, "game_date", str)
        self.pa_by_game = self._index_value(self.plate_appearances, "game_pk", int)
        self.pitches_by_game = self._index_value(self.pitches, "game_pk", int)
        self.pa_by_year = self._year_index(self.plate_appearances)
        self.pa_by_team = self._team_index(self.plate_appearances)
        self.pitch_game_pks = frozenset(int(row["game_pk"]) for row in self.pitches)
        self.pa_game_pks = frozenset(
            int(row["game_pk"]) for row in self.plate_appearances
        )
        self.max_pitch_row = (
            max(self.pitches, key=self._pitch_key) if self.pitches else None
        )
        self.max_pa_row = (
            max(self.plate_appearances, key=self._pitch_key)
            if self.plate_appearances
            else None
        )
        self.league_fip_by_year = MappingProxyType(
            {year: self._league_fip(rows) for year, rows in self.pa_by_year.items()}
        )

    @staticmethod
    def _pitch_key(row: Mapping[str, object]) -> tuple:
        return (
            str(row["game_date"]),
            int(row["game_pk"]),
            int(row["at_bat_number"]),
            int(row.get("pitch_number", 0)),
            int(row["pitcher_id"]),
            int(row["batter_id"]),
        )

    @staticmethod
    def _index(rows: Iterable[Mapping[str, object]], field: str) -> Mapping[int, tuple]:
        result: defaultdict[int, list] = defaultdict(list)
        for row in rows:
            result[int(row[field])].append(row)
        return MappingProxyType(
            {key: tuple(value) for key, value in sorted(result.items())}
        )

    @staticmethod
    def _index_value(
        rows: Iterable[Mapping[str, object]], field: str, convert
    ) -> Mapping:
        result: defaultdict[object, list] = defaultdict(list)
        for row in rows:
            result[convert(row[field])].append(row)
        return MappingProxyType(
            {key: tuple(value) for key, value in sorted(result.items())}
        )

    @staticmethod
    def _year_index(rows: Iterable[Mapping[str, object]]) -> Mapping[int, tuple]:
        result: defaultdict[int, list] = defaultdict(list)
        for row in rows:
            result[_day(row["game_date"], "game_date").year].append(row)
        return MappingProxyType(
            {key: tuple(value) for key, value in sorted(result.items())}
        )

    @staticmethod
    def _pitcher_game_index(
        rows: Iterable[Mapping[str, object]],
    ) -> Mapping[tuple[int, int], tuple]:
        result: defaultdict[tuple[int, int], list] = defaultdict(list)
        for row in rows:
            result[(int(row["pitcher_id"]), int(row["game_pk"]))].append(row)
        return MappingProxyType(
            {key: tuple(value) for key, value in sorted(result.items())}
        )

    @staticmethod
    def _league_fip(rows: Sequence[Mapping[str, object]]) -> tuple[float | None, int]:
        numerator = (
            13 * sum(row["events"] == "home_run" for row in rows)
            + 3 * sum(row["events"] in WALK_EVENTS | {"hit_by_pitch"} for row in rows)
            - 2
            * sum(
                row["events"] in {"strikeout", "strikeout_double_play"} for row in rows
            )
        )
        outs = sum(int(row["outs_on_play"]) for row in rows)
        return ((numerator / (outs / 3.0)) if outs else None, len(rows))

    @staticmethod
    def _team_index(rows: Iterable[Mapping[str, object]]) -> Mapping[str, tuple]:
        result: defaultdict[str, list] = defaultdict(list)
        for row in rows:
            team = (
                row["home_team"] if row["inning_topbot"] == "Top" else row["away_team"]
            )
            result[str(team)].append(row)
        return MappingProxyType(
            {key: tuple(value) for key, value in sorted(result.items())}
        )


def _rate(rows: Sequence[Mapping[str, object]]) -> tuple[float | None, int]:
    values = [value for row in rows if (value := pa_expected_woba(row)) is not None]
    return ((math.fsum(values) / len(values)) if values else None, len(values))


def _k_minus_bb(
    rows: Sequence[Mapping[str, object]], *, batting: bool = False
) -> tuple[float | None, int]:
    if not rows:
        return None, 0
    strikeouts = sum(
        str(row.get("events")) in {"strikeout", "strikeout_double_play"} for row in rows
    )
    walks = sum(str(row.get("events")) in WALK_EVENTS for row in rows)
    value = (walks - strikeouts if batting else strikeouts - walks) / len(rows)
    return value, len(rows)


def _starter_component(
    history: FeatureHistory, pitcher_id: int, target: date, league_era: float
) -> dict:
    season = [
        r
        for r in history.pa_by_pitcher.get(pitcher_id, ())
        if _day(r["game_date"], "game_date").year == target.year
    ]
    xwoba, bf = _rate(season)
    season_bf = len(season)
    kbb, kbb_bf = _k_minus_bb(season)
    numerator = (
        13 * sum(r["events"] == "home_run" for r in season)
        + 3 * sum(r["events"] in WALK_EVENTS | {"hit_by_pitch"} for r in season)
        - 2 * sum(r["events"] in {"strikeout", "strikeout_double_play"} for r in season)
    )
    outs = sum(int(r["outs_on_play"]) for r in season)
    league_raw_fip, league_bf = history.league_fip_by_year.get(target.year, (None, 0))
    cfip = league_era - league_raw_fip if league_raw_fip is not None else None
    pitcher_raw_fip = numerator / (outs / 3.0) if outs else None
    fip = (
        pitcher_raw_fip + cfip
        if pitcher_raw_fip is not None and cfip is not None
        else None
    )
    games: defaultdict[int, list] = defaultdict(list)
    for row in season:
        if history.roles.get((int(row["game_pk"]), pitcher_id), {}).get("is_starter"):
            games[int(row["game_pk"])].append(row)
    starts = []
    for game_pk, rows in games.items():
        game_pitches = history.pitches_by_pitcher_game.get((pitcher_id, game_pk), ())
        starts.append(
            {
                "game_pk": game_pk,
                "game_date": rows[0]["game_date"],
                "outs": sum(int(r["outs_on_play"]) for r in rows),
                "pitch_count": len(game_pitches),
                "rows": rows,
            }
        )
    starts.sort(key=lambda s: (s["game_date"], s["game_pk"]), reverse=True)
    latest = starts[:5]
    recent_rows = [r for start in latest for r in start["rows"]]
    last5, last5_bf = _rate(recent_rows)
    rest = (
        max(0, (target - _day(latest[0]["game_date"], "start date")).days - 1)
        if latest
        else 5
    )
    innings = expected_innings(latest, rest)
    pitcher_pitches = history.pitches_by_pitcher.get(pitcher_id, ())
    recent_game_ids = {s["game_pk"] for s in latest}
    recent_velos = [
        float(r["release_speed"])
        for r in pitcher_pitches
        if r.get("release_speed") is not None and int(r["game_pk"]) in recent_game_ids
    ]
    prior_season_velos = [
        float(r["release_speed"])
        for r in pitcher_pitches
        if r.get("release_speed") is not None
        and _day(r["game_date"], "game_date").year == target.year - 1
    ]
    if len(prior_season_velos) >= 50:
        baseline_velos = prior_season_velos
    elif latest:
        recent_start = min(_day(start["game_date"], "start date") for start in latest)
        baseline_velos = [
            float(r["release_speed"])
            for r in pitcher_pitches
            if r.get("release_speed") is not None
            and _day(r["game_date"], "game_date").year == target.year
            and _day(r["game_date"], "game_date") < recent_start
        ]
    else:
        baseline_velos = []
    velo = (
        math.fsum(recent_velos) / len(recent_velos)
        - math.fsum(baseline_velos) / len(baseline_velos)
        if recent_velos and len(baseline_velos) >= 50
        else None
    )
    starter_quality = _reliability(bf, 150)
    fip_quality = _reliability(season_bf, 150)
    last5_quality = _reliability(last5_bf, 100)
    velo_quality = (
        min(
            _reliability(len(recent_velos), 150), _reliability(len(baseline_velos), 150)
        )
        if velo is not None
        else 0.0
    )
    return {
        "pitcher_id": pitcher_id,
        "season_xwoba": _metric(xwoba, bf, starter_quality),
        "k_minus_bb": _metric(kbb, kbb_bf, _reliability(kbb_bf, 150)),
        "fip_numerator": _metric(float(numerator), season_bf, fip_quality),
        "outs": _metric(outs, season_bf, fip_quality),
        "league_era": _metric(league_era, league_bf, _reliability(league_bf, 150)),
        "league_raw_fip": _metric(
            league_raw_fip, league_bf, _reliability(league_bf, 150)
        ),
        "league_cfip": _metric(cfip, league_bf, _reliability(league_bf, 150)),
        "fip": _metric(fip, season_bf, fip_quality),
        "last5_xwoba": _metric(last5, last5_bf, last5_quality),
        "expected_ip": _metric(
            innings.value, len(latest), _reliability(len(latest), 3)
        ),
        "velocity_change": _metric(
            velo,
            len(recent_velos),
            velo_quality,
            baseline_sample_size=len(baseline_velos),
        ),
    }


def _offense_component(
    history: FeatureHistory,
    lineup: Sequence[Mapping[str, object]],
    opponent_hand: str | None,
    target: date,
) -> dict:
    first = target - timedelta(days=14)
    players = []
    aggregates: dict[str, list[tuple[float, float, int]]] = defaultdict(list)
    for slot, player in enumerate(lineup, 1):
        player_id = int(player["player_id"])
        rows = [
            r
            for r in history.pa_by_batter.get(player_id, ())
            if _day(r["game_date"], "game_date").year == target.year
        ]
        platoon = [
            r
            for r in rows
            if opponent_hand is not None and r["p_throws"] == opponent_hand
        ]
        recent = [
            r for r in rows if first <= _day(r["game_date"], "game_date") < target
        ]
        px, pn = _rate(platoon)
        rx, rn = _rate(recent)
        kbb, kn = _k_minus_bb(rows, batting=True)
        batted = [r for r in rows if r.get("launch_speed_angle") is not None]
        barrel = (
            sum(float(r["launch_speed_angle"]) == 6 for r in batted) / len(batted)
            if batted
            else None
        )
        abs_ = [
            r
            for r in rows
            if r["events"]
            not in WALK_EVENTS
            | {"hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf"}
        ]
        hits = sum(r["events"] in HIT_EVENTS for r in abs_)
        total_bases = sum(HIT_EVENTS.get(str(r["events"]), 0) for r in abs_)
        iso = (total_bases / len(abs_) - hits / len(abs_)) if abs_ else None
        metrics = {
            "platoon_xwoba": (px, pn),
            "last14_xwoba": (rx, rn),
            "bb_minus_k": (kbb, kn),
            "barrel_rate": (barrel, len(batted)),
            "iso": (iso, len(abs_)),
        }
        weight = history.slot_weights[slot]
        for name, (value, n) in metrics.items():
            if value is not None:
                aggregates[name].append((weight, float(value), n))
        players.append(
            {
                "player_id": player_id,
                "batting_slot": slot,
                "missing": not rows,
                **{
                    name: _metric(
                        v,
                        n,
                        _reliability(n, 100 if name == "last14_xwoba" else 150),
                    )
                    for name, (v, n) in metrics.items()
                },
            }
        )
    output = {"players": players}
    for name in ("platoon_xwoba", "last14_xwoba", "bb_minus_k", "barrel_rate", "iso"):
        present = {
            p["batting_slot"]: p[name] for p in players if p[name]["value"] is not None
        }
        present_weight = math.fsum(history.slot_weights[s] for s in present)
        value = (
            math.fsum(
                history.slot_weights[s] * float(m["value"]) for s, m in present.items()
            )
            / present_weight
            if present
            else None
        )
        n = sum(int(m["sample_size"]) for m in present.values())
        quality = math.fsum(
            history.slot_weights[s] * float(m["quality"]) for s, m in present.items()
        )
        output[name] = _metric(value, n, quality, present_weight=present_weight)
    return output


def _bullpen_component(history: FeatureHistory, team_code: str, target: date) -> dict:
    rows = [
        r
        for r in history.pa_by_team.get(team_code, ())
        if _day(r["game_date"], "game_date").year == target.year
        and history.roles.get((int(r["game_pk"]), int(r["pitcher_id"])), {}).get(
            "is_reliever"
        )
    ]
    xwoba, bf = _rate(rows)
    kbb, kbf = _k_minus_bb(rows)
    recent = [
        r
        for r in rows
        if target - timedelta(days=14) <= _day(r["game_date"], "game_date") < target
    ]
    last14, lastbf = _rate(recent)
    events = [
        {
            **r,
            "score_margin": int(r["home_score"]) - int(r["away_score"]),
            "batters_faced": 1,
            "is_reliever": True,
        }
        for r in rows
    ]
    pool = leverage_pool(events, target)
    usage = {}
    for reliever in pool:
        pid = reliever["pitcher_id"]
        pitches_by_day = [
            sum(
                1
                for p in history.pitches_by_pitcher.get(pid, ())
                if _day(p["game_date"], "game_date") == target - timedelta(days=offset)
            )
            for offset in (1, 2, 3)
        ]
        if history.statcast_coverage_complete or any(pitches_by_day):
            usage[pid] = pitches_by_day
    availability, aq = team_availability(pool, usage)
    pool_bf = sum(r["batters_faced"] for r in pool)
    return {
        "season_xwoba": _metric(xwoba, bf, _reliability(bf, 300)),
        "k_minus_bb": _metric(kbb, kbf, _reliability(kbf, 300)),
        "last14_xwoba": _metric(last14, lastbf, _reliability(lastbf, 300)),
        "leverage_pool": _metric(
            float(pool_bf), pool_bf, _reliability(pool_bf, 300), members=pool
        ),
        "availability": _metric(availability, pool_bf, aq),
    }


def _venue(row: Mapping[str, object], history: FeatureHistory) -> Mapping[str, object]:
    frozen = history.venue_by_name.get(str(row.get("venue_name", "")), {})
    return {
        "city": row.get("venue_city", frozen.get("city")),
        "latitude": row.get("venue_latitude", frozen.get("latitude")),
        "longitude": row.get("venue_longitude", frozen.get("longitude")),
        "time_zone": row.get("time_zone", frozen.get("time_zone")),
    }


def _travel_fields(
    origin: Mapping[str, object],
    destination: Mapping[str, object],
    game: Mapping[str, object],
) -> tuple[float, int] | None:
    try:
        lat1, lon1 = (
            math.radians(float(origin["latitude"])),
            math.radians(float(origin["longitude"])),
        )
        lat2, lon2 = (
            math.radians(float(destination["latitude"])),
            math.radians(float(destination["longitude"])),
        )
        zone1, zone2 = (
            ZoneInfo(str(origin["time_zone"])),
            ZoneInfo(str(destination["time_zone"])),
        )
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    angle = 2 * math.asin(
        math.sqrt(
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        )
    )
    miles = 3958.7613 * angle
    start = str(game.get("scheduled_start_utc", ""))
    try:
        instant = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except ValueError:
        instant = datetime.combine(
            _day(game["official_date"], "official_date"),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
    offset1 = instant.astimezone(zone1).utcoffset()
    offset2 = instant.astimezone(zone2).utcoffset()
    zones = (
        round(abs((offset2 - offset1).total_seconds()) / 3600)
        if offset1 is not None and offset2 is not None
        else 0
    )
    return miles, zones


def _schedule_component(
    game: Mapping[str, object], history: FeatureHistory, team_code: str, target: date
) -> dict:
    prior = [
        r
        for r in history.schedule_history.by_team.get(team_code, ())
        if _day(r.get("official_date", r.get("game_date")), "schedule date") < target
    ]
    prior.sort(
        key=lambda r: (
            _day(r.get("official_date", r.get("game_date")), "schedule date"),
            int(r["game_pk"]),
        ),
        reverse=True,
    )
    latest = prior[0] if prior else None
    d1_rows = history.schedule_history.by_team_date.get(
        (team_code, target - timedelta(days=1)), ()
    )
    schedule_window_supported = history.schedule_history.covers(
        target - timedelta(days=3), target - timedelta(days=1)
    )
    rest_supported = schedule_window_supported
    rest = rest_supported and not d1_rows
    if schedule_window_supported and d1_rows:
        prior_innings_supported = all(
            type(row.get("innings")) is int for row in d1_rows
        )
        prior_innings = (
            max(int(row["innings"]) for row in d1_rows)
            if prior_innings_supported
            else 0
        )
    elif schedule_window_supported:
        prior_innings_supported = True
        prior_innings = 0
    else:
        prior_innings_supported = False
        prior_innings = 0
    home_days = 0
    destination = _venue(game, history)
    target_venue = game.get("venue_name")
    prior_home_games = []
    for offset in (1, 2, 3):
        candidates = history.schedule_history.by_team_date.get(
            (team_code, target - timedelta(days=offset)), ()
        )
        matching = [
            row
            for row in candidates
            if row.get("home_team_code") == team_code
            and row.get("venue_name") == target_venue
        ]
        if not matching:
            prior_home_games = []
            break
        prior_home_games.append(matching[0])
    home_city_supported = schedule_window_supported
    if len(prior_home_games) == 3:
        home_days = 3
    if isinstance(game.get("doubleheader"), bool):
        doubleheader = bool(game["doubleheader"])
        doubleheader_supported = True
    elif game.get("doubleheader_marker") in {"GAME1", "GAME2"}:
        doubleheader = True
        doubleheader_supported = True
    else:
        doubleheader = False
        doubleheader_supported = False
    if not schedule_window_supported:
        travel = None
    elif rest:
        travel = (0.0, 0)
    else:
        travel = (
            _travel_fields(_venue(latest, history), destination, game)
            if latest
            else None
        )
    flags = []
    if travel is None:
        miles, time_zones = 0.0, 0
        travel_supported = False
        time_zone_supported = False
        flags.extend(
            [
                "travel_neutral_missing_venue_coordinates",
                "time_zones_neutral_missing_venue_coordinates",
            ]
        )
    else:
        miles, time_zones = travel
        travel_supported = True
        time_zone_supported = True
    if not rest_supported:
        flags.append("rest_unsupported")
    if not prior_innings_supported:
        flags.append("prior_innings_unsupported")
    if not home_city_supported:
        flags.append("home_city_unsupported")
    if not doubleheader_supported:
        flags.append("doubleheader_unsupported")
    value = schedule_score(
        rest_day=rest,
        home_days=home_days,
        prior_innings=prior_innings,
        miles=miles,
        time_zones=time_zones,
        doubleheader=doubleheader,
    )
    supported = sum(
        (
            rest_supported,
            prior_innings_supported,
            home_city_supported,
            travel_supported,
            time_zone_supported,
            doubleheader_supported,
        )
    )
    return _metric(value, len(prior), supported / 6.0, flags=flags)


def _max_history_date(history: FeatureHistory, target: date | None) -> str | None:
    dates = []
    for row in (history.max_pitch_row, history.max_pa_row):
        if row is not None:
            dates.append(_day(row["game_date"], "game_date"))
    schedule_max = history.schedule_history.max_before(target)
    if schedule_max is not None:
        dates.append(schedule_max)
    return max(dates).isoformat() if dates else None


def _valid_lineup(value: object) -> bool:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != 9
    ):
        return False
    ids = []
    for player in value:
        if not isinstance(player, Mapping):
            return False
        player_id = player.get("player_id")
        if type(player_id) is not int or player_id <= 0:
            return False
        ids.append(player_id)
    return len(set(ids)) == 9


def _canonical_start(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _league_era_record(
    source: object, target: date
) -> tuple[float | None, dict[str, object] | None, str | None]:
    expected_as_of = (target - timedelta(days=1)).isoformat()
    if not isinstance(source, Mapping) or expected_as_of not in source:
        return None, None, "missing_league_era_d1"
    record = source[expected_as_of]
    required = {
        "as_of_date",
        "value",
        "earned_runs",
        "outs",
        "source",
        "manifest_sha256",
    }
    if not isinstance(record, Mapping) or set(record) != required:
        return None, None, "invalid_league_era_provenance"
    value = record["value"]
    earned_runs = record["earned_runs"]
    outs = record["outs"]
    digest = record["manifest_sha256"]
    valid = (
        record["as_of_date"] == expected_as_of
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value > 0
        and type(earned_runs) is int
        and earned_runs >= 0
        and type(outs) is int
        and outs > 0
        and record["source"] == "mlb_stats_api"
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )
    try:
        derived_value = 27.0 * earned_runs / outs if valid else math.nan
    except OverflowError:
        derived_value = math.nan
    if not valid or not math.isclose(
        float(value), derived_value, rel_tol=0.0, abs_tol=1e-9
    ):
        return None, None, "invalid_league_era_provenance"
    return float(value), dict(record), None


def _eligibility(
    game: Mapping[str, object], league_era_source: object, history: FeatureHistory
) -> tuple[list[str], date | None, float | None, dict[str, object] | None]:
    reasons = []
    if game.get("game_type") != "R":
        reasons.append("invalid_game_type")
    game_pk = game.get("game_pk")
    if type(game_pk) is not int or game_pk <= 0:
        reasons.append("invalid_game_pk")
    try:
        target = _day(game.get("official_date"), "official_date")
    except ValueError:
        target = None
        reasons.append("invalid_official_date")
    if not _canonical_start(game.get("scheduled_start_utc")):
        reasons.append("invalid_scheduled_start_utc")
    status = game.get("status")
    if (
        not isinstance(status, str)
        or not status
        or any(word in status.lower() for word in ("cancel", "postpon"))
    ):
        reasons.append("ineligible_status")
    if (
        type(game.get("venue_id")) is not int
        or game["venue_id"] <= 0
        or not isinstance(game.get("venue_name"), str)
        or not game["venue_name"]
    ):
        reasons.append("missing_venue")
    home_team, away_team = game.get("home_team"), game.get("away_team")
    if (
        not isinstance(home_team, str)
        or not home_team
        or not isinstance(away_team, str)
        or not away_team
        or home_team == away_team
    ):
        reasons.append("invalid_teams")
    for side in ("home", "away"):
        actual_id = game.get(f"{side}_actual_starter_id")
        if type(actual_id) is not int or actual_id <= 0:
            reasons.append(f"missing_{side}_actual_starter")
        lineup = game.get(f"{side}_lineup")
        if not lineup:
            reasons.append(f"missing_{side}_lineup")
        elif not _valid_lineup(lineup):
            reasons.append(f"invalid_{side}_lineup")
    if game.get("starter_provenance") != "RECONSTRUCTED_ACTUAL_STARTER":
        reasons.append("invalid_starter_provenance")
    if not history.statcast_coverage_complete:
        reasons.append("incomplete_statcast_coverage")
    league_era = None
    league_era_provenance = None
    if target is not None:
        league_era, league_era_provenance, era_reason = _league_era_record(
            league_era_source, target
        )
        if era_reason is not None:
            reasons.append(era_reason)
    return reasons, target, league_era, league_era_provenance


def _assert_history(game: Mapping[str, object], history: FeatureHistory) -> None:
    game_pk = int(game["game_pk"])
    pitch_boundary = []
    if game_pk in history.pitch_game_pks:
        pitch_boundary.append(history.pitches_by_game[game_pk][0])
    if history.max_pitch_row is not None:
        pitch_boundary.append(history.max_pitch_row)
    assert_pregame_history(game, pitch_boundary)
    pa_boundary = []
    if game_pk in history.pa_game_pks:
        pa_boundary.append(history.pa_by_game[game_pk][0])
    if history.max_pa_row is not None:
        pa_boundary.append(history.max_pa_row)
    assert_pregame_history(game, pa_boundary)


def build_raw_game_features(
    game: Mapping[str, object],
    history: FeatureHistory,
    team_code_by_name: Mapping[str, str],
    league_era_by_asof: Mapping[str, Mapping[str, object]],
) -> dict:
    """Build raw features for a normalized game enriched with actual starters."""
    if not isinstance(history, FeatureHistory):
        raise ValueError("history must be FeatureHistory")
    if (
        len(team_code_by_name) != 30
        or len(set(team_code_by_name.values())) != 30
        or any(
            not isinstance(name, str)
            or not name
            or not isinstance(code, str)
            or not code
            for name, code in team_code_by_name.items()
        )
    ):
        raise ValueError("team_code_by_name must be an explicit frozen 30-team mapping")
    reasons, target, resolved_league_era, league_era_provenance = _eligibility(
        game, league_era_by_asof, history
    )
    target_identity_valid = (
        target is not None and type(game.get("game_pk")) is int and game["game_pk"] > 0
    )
    history_validation = "NOT_RUN_INVALID_TARGET"
    if target_identity_valid:
        _assert_history(game, history)
        history_validation = "PASSED"
    result = {
        "game_pk": game.get("game_pk"),
        "official_date": target.isoformat()
        if target is not None
        else game.get("official_date"),
        "eligible": not reasons,
        "reasons": reasons,
        "lineup_provenance": (
            "RECONSTRUCTED_FINAL_LINEUP"
            if _valid_lineup(game.get("home_lineup"))
            and _valid_lineup(game.get("away_lineup"))
            else "MISSING_OR_INVALID_LINEUP"
        ),
        "starter_provenance": (
            "RECONSTRUCTED_ACTUAL_STARTER"
            if game.get("starter_provenance") == "RECONSTRUCTED_ACTUAL_STARTER"
            and all(
                type(game.get(f"{side}_actual_starter_id")) is int
                and game[f"{side}_actual_starter_id"] > 0
                for side in ("home", "away")
            )
            else "MISSING_OR_INVALID_ACTUAL_STARTER"
        ),
        "away_probable_starter_mismatch": bool(
            game.get("away_probable_starter_mismatch")
            or game.get("away_probable_pitcher_id")
            != game.get("away_actual_starter_id")
        ),
        "home_probable_starter_mismatch": bool(
            game.get("home_probable_starter_mismatch")
            or game.get("home_probable_pitcher_id")
            != game.get("home_actual_starter_id")
        ),
    }
    raw_source_max_date = _max_history_date(history, target)
    schedule_input_max = history.schedule_history.input_max_date
    schedule_used_max = (
        history.schedule_history.max_before(target) if target is not None else None
    )
    provenance = {
        "max_history_date": raw_source_max_date,
        "raw_source_max_date": raw_source_max_date,
        "statcast_target_excluded": target_identity_valid,
        "history_validation": history_validation,
        "statcast_coverage_complete": history.statcast_coverage_complete,
        "schedule_input_max_date": (
            schedule_input_max.isoformat() if schedule_input_max is not None else None
        ),
        "schedule_used_max_date": (
            schedule_used_max.isoformat() if schedule_used_max is not None else None
        ),
        "target_schedule_present": (
            history.schedule_history.has_game(int(game["game_pk"]), target)
            if target_identity_valid
            else False
        ),
        "future_schedule_present": (
            history.schedule_history.has_future(target) if target is not None else False
        ),
        "schedule_coverage_start": (
            history.schedule_history.coverage_start.isoformat()
            if history.schedule_history.coverage_start is not None
            else None
        ),
        "schedule_coverage_end": (
            history.schedule_history.coverage_end.isoformat()
            if history.schedule_history.coverage_end is not None
            else None
        ),
        "schedule_coverage_complete": history.schedule_history.coverage_complete,
    }
    if league_era_provenance is not None:
        provenance["league_era"] = league_era_provenance
    if reasons:
        result["provenance"] = provenance
        return result
    assert target is not None and resolved_league_era is not None
    hands = {}
    for side in ("home", "away"):
        pid = int(game[f"{side}_actual_starter_id"])
        pitcher_rows = history.pitches_by_pitcher.get(pid, ())
        hands[side] = str(pitcher_rows[-1]["p_throws"]) if pitcher_rows else None
    for side, opponent in (("home", "away"), ("away", "home")):
        name = str(game[f"{side}_team"])
        if name not in team_code_by_name:
            raise ValueError(f"team mapping missing {name}")
        code = team_code_by_name[name]
        result[side] = {
            "team_code": code,
            "starter": _starter_component(
                history,
                int(game[f"{side}_actual_starter_id"]),
                target,
                resolved_league_era,
            ),
            "offense": _offense_component(
                history, game[f"{side}_lineup"], hands[opponent], target
            ),
            "bullpen": _bullpen_component(history, code, target),
            "schedule": _schedule_component(game, history, code, target),
            "environment": _metric(50.0, 0, 0.0, flags=["environment_unavailable"]),
            "defense": _metric(50.0, 0, 0.0, flags=["defense_unavailable"]),
        }
    result["provenance"] = provenance
    return result


__all__ = [
    "FeatureHistory",
    "ScheduleHistory",
    "actual_starter_index",
    "build_raw_game_features",
    "enrich_schedule_game_participants",
    "pa_expected_woba",
]
