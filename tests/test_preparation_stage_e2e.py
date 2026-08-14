import csv
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from mlb_backtest.career_bf import fetch_prior_career_bf_records
from mlb_backtest.config import load_config
from mlb_backtest.manifest import RawStore
from mlb_backtest.mlb_stats import fetch_league_era_records
from mlb_backtest.model_prep import prepare_candidates_stage, prepare_training_stage
from mlb_backtest.pipeline import calibrate_stage, features_stage, lock_stage
from mlb_backtest.prepare import PreparationValidationPolicy
from mlb_backtest.sources import schedule_url
from mlb_backtest.statcast import regular_season_ranges, statcast_url


ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "v5_1_bt.json"
TINY_POLICY = PreparationValidationPolicy(
    minimum_played_games=1,
    expected_team_count=30,
    minimum_lineup_games=1,
    minimum_starting_lineup_pa=1,
)
STATCAST_FIELDS = (
    "pitch_type",
    "game_date",
    "game_pk",
    "pitcher",
    "batter",
    "events",
    "description",
    "p_throws",
    "home_team",
    "away_team",
    "inning",
    "inning_topbot",
    "release_speed",
    "woba_value",
    "woba_denom",
    "estimated_woba_using_speedangle",
    "launch_speed_angle",
    "at_bat_number",
    "pitch_number",
    "home_score",
    "away_score",
    "post_home_score",
    "post_away_score",
    "outs_when_up",
    "game_type",
)
EVENTS = (
    "walk",
    "strikeout",
    "single",
    "walk",
    "double",
    "field_out",
    "home_run",
    "walk",
    "field_out",
)


def _lineup(team_index):
    return [
        {"id": 10_000 + team_index * 100 + slot, "fullName": f"P{team_index}-{slot}"}
        for slot in range(1, 10)
    ]


def _schedule_payload(days, *, target=False):
    dates = []
    for day_index, day in enumerate(days):
        games = []
        for matchup in range(15):
            away, home = 2 * matchup, 2 * matchup + 1
            game_pk = int(day.replace("-", "")) * 100 + matchup
            home_win = (day_index + matchup) % 2 == 0
            games.append(
                {
                    "gamePk": game_pk,
                    "gameType": "R",
                    "officialDate": day,
                    "gameDate": f"{day}T23:{matchup:02d}:00Z",
                    "status": {"detailedState": "Final"},
                    "teams": {
                        "away": {
                            "team": {"id": away + 1, "name": f"Team {away:02d}"},
                            "probablePitcher": {
                                "id": 1000 + away,
                                "fullName": f"SP {away}",
                            },
                            "score": 1 if home_win else 2,
                            "isWinner": not home_win,
                        },
                        "home": {
                            "team": {"id": home + 1, "name": f"Team {home:02d}"},
                            "probablePitcher": {
                                "id": 1000 + home,
                                "fullName": f"SP {home}",
                            },
                            "score": 2 if home_win else 1,
                            "isWinner": home_win,
                        },
                    },
                    "lineups": {
                        "awayPlayers": _lineup(away),
                        "homePlayers": _lineup(home),
                    },
                    "venue": {"id": matchup + 1, "name": f"Park {matchup}"},
                    "scheduledInnings": 1,
                    "linescore": {"currentInning": 1, "outs": 3},
                }
            )
        dates.append({"date": day, "games": games})
    return json.dumps({"dates": dates}, separators=(",", ":"), sort_keys=True).encode()


def _statcast_payload(days):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=STATCAST_FIELDS, lineterminator="\n")
    writer.writeheader()
    for day_index, day in enumerate(days):
        for matchup in range(15):
            away, home = 2 * matchup, 2 * matchup + 1
            game_pk = int(day.replace("-", "")) * 100 + matchup
            at_bat = 0
            for half, batting, pitching in (("Top", away, home), ("Bot", home, away)):
                outs = 0
                starter_pa = 3 + ((day_index + pitching) % 4)
                for slot, base_event in enumerate(EVENTS, 1):
                    at_bat += 1
                    event = base_event
                    mode = (day_index + batting) % 3
                    if mode == 1 and slot in {1, 3}:
                        event = "single" if slot == 1 else "home_run"
                    elif mode == 2 and slot in {4, 7}:
                        event = "single"
                    pitcher = 1000 + pitching if slot <= starter_pa else 2000 + pitching
                    expected = 0.04 + 0.025 * slot + 0.007 * day_index + 0.001 * batting
                    woba = {
                        "walk": 0.7,
                        "single": 0.9,
                        "double": 1.25,
                        "home_run": 2.0,
                    }.get(event, 0.0)
                    pitch_count = (
                        20 if slot <= starter_pa else 6 + (pitching + day_index) % 3
                    )
                    for pitch_number in range(1, pitch_count + 1):
                        terminal = pitch_number == pitch_count
                        writer.writerow(
                            {
                                "pitch_type": "FF",
                                "game_date": day,
                                "game_pk": game_pk,
                                "pitcher": pitcher,
                                "batter": 10_000 + batting * 100 + slot,
                                "events": event if terminal else "",
                                "description": (
                                    "swinging_strike"
                                    if terminal and event == "strikeout"
                                    else "hit_into_play"
                                    if terminal
                                    else "ball"
                                ),
                                "p_throws": "L" if pitching % 2 else "R",
                                "home_team": f"T{home:02d}",
                                "away_team": f"T{away:02d}",
                                "inning": 7,
                                "inning_topbot": half,
                                "release_speed": 88
                                + pitching % 5
                                + day_index * (1 + pitching % 3),
                                "woba_value": woba if terminal else "",
                                "woba_denom": 1 if terminal else "",
                                "estimated_woba_using_speedangle": round(expected, 6)
                                if terminal
                                else "",
                                "launch_speed_angle": (
                                    6 if (slot + batting + day_index) % 3 == 0 else 2
                                )
                                if terminal
                                else "",
                                "at_bat_number": at_bat,
                                "pitch_number": pitch_number,
                                "home_score": 0,
                                "away_score": 0,
                                "post_home_score": 0,
                                "post_away_score": 0,
                                "outs_when_up": outs,
                                "game_type": "R",
                            }
                        )
                    if event in {"field_out", "strikeout"}:
                        outs += 1
    return stream.getvalue().encode()


def _put_schedule(store, role, start, end, days):
    store.put(
        "mlb_schedule",
        {
            "role": role,
            "start": start,
            "end": end,
            "gameType": "R",
            "url": schedule_url(start, end),
        },
        _schedule_payload(days, target=role == "backtest"),
    )


def _put_statcast(store, start, end, days):
    store.put(
        "baseball_savant_statcast",
        {"start": start, "end": end, "game_type": "R", "url": statcast_url(start, end)},
        _statcast_payload(days),
    )


def _seed_raw(root):
    config = load_config(CONFIG)
    store = RawStore(root / "raw")
    era_dates = []
    openings = {}
    for year, (start, end) in zip(
        (2022, 2023, 2024), regular_season_ranges(config["extension_end"])[:3]
    ):
        first = date.fromisoformat(start)
        days = [(first + timedelta(days=offset)).isoformat() for offset in range(7)]
        _put_schedule(store, "training", start, end, days)
        _put_statcast(store, start, end, days)
        openings[year] = start
        era_dates.extend(days[:-1])
    history_days = [f"2025-04-{day:02d}" for day in range(25, 31)]
    target_days = ["2025-05-01"]
    _put_schedule(store, "history", "2025-03-18", "2025-04-30", history_days)
    _put_schedule(
        store,
        "backtest",
        config["initial_window"]["start"],
        config["extension_end"],
        target_days,
    )
    _put_statcast(
        store, "2025-03-18", config["extension_end"], history_days + target_days
    )
    openings[2025] = "2025-03-18"
    era_dates.append("2025-04-30")
    era_payload = json.dumps(
        {
            "stats": [
                {
                    "group": {"displayName": "pitching"},
                    "totalSplits": 1,
                    "splits": [{"stat": {"earnedRuns": 4, "outs": 27}}],
                }
            ]
        }
    ).encode()
    fetch_league_era_records(
        store, openings, era_dates, fetcher=lambda _url: era_payload, retries=()
    )
    fetch_prior_career_bf_records(
        store,
        range(1000, 1030),
        fetcher=lambda _url: b'{"stats":[]}',
        sleeper=lambda _delay: None,
        retries=(),
    )


def _run(root):
    prepare_training_stage(CONFIG, root, validation_policy=TINY_POLICY)
    calibrate_stage(CONFIG, root)
    prepare_candidates_stage(CONFIG, root, validation_policy=TINY_POLICY)
    features_stage(CONFIG, root)
    locked = lock_stage(CONFIG, root)
    return {
        path: (root / path).read_bytes()
        for path in (
            "calibration/training_raw_features.jsonl",
            "calibration/frozen_inputs.json",
            "calibration/model_card.json",
            "features/candidate_raw_features.jsonl",
            "features/candidate_components.jsonl",
            "features/candidate_provenance.json",
            "features/scored_candidate_provenance.json",
            "locked/predictions.jsonl",
            "locked/lock_manifest.json",
            "locked/source_provenance.json",
        )
    }, locked


class PreparationStageE2ETest(unittest.TestCase):
    def test_verified_tiny_sources_run_actual_preparation_stages_deterministically(
        self,
    ):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            source = base / "source"
            _seed_raw(source)
            first, second = base / "first", base / "second"
            shutil.copytree(source / "raw", first / "raw")
            shutil.copytree(source / "raw", second / "raw")
            first_bytes, locked = _run(first)
            second_bytes, _ = _run(second)
            verified_manifest_hashes = {
                item["manifest_sha256"]
                for item in RawStore(first / "raw").verify_manifests()
            }

        deterministic = set(first_bytes) - {"locked/lock_manifest.json"}
        self.assertEqual(
            {name: first_bytes[name] for name in deterministic},
            {name: second_bytes[name] for name in deterministic},
        )
        self.assertEqual(locked["row_count"], 15)
        lock_manifest = json.loads(first_bytes["locked/lock_manifest.json"])
        self.assertEqual(
            lock_manifest["sha256"],
            hashlib.sha256(first_bytes["locked/predictions.jsonl"]).hexdigest(),
        )
        frozen = json.loads(first_bytes["calibration/frozen_inputs.json"])
        candidate_provenance = json.loads(
            first_bytes["features/candidate_provenance.json"]
        )
        self.assertEqual(frozen["schema_version"], 1)
        self.assertEqual(candidate_provenance["schema_version"], 2)
        schedule_history = candidate_provenance["schedule_history"]
        self.assertTrue(schedule_history["coverage_complete"])
        self.assertEqual(
            [child["role"] for child in schedule_history["children"]],
            ["history", "backtest"],
        )
        for child in schedule_history["children"]:
            self.assertEqual(child["excluded_schedule_date_count"], 0)
            self.assertEqual(child["excluded_schedule_date_game_pks"], [])
            self.assertEqual(child["excluded_statcast_date_count"], 0)
            self.assertEqual(child["excluded_statcast_date_game_pks"], [])
        self.assertEqual(
            frozen["normalization_input_sha256"],
            hashlib.sha256(
                first_bytes["calibration/training_raw_features.jsonl"]
            ).hexdigest(),
        )
        self.assertEqual(
            candidate_provenance["candidate_raw_sha256"],
            hashlib.sha256(
                first_bytes["features/candidate_raw_features.jsonl"]
            ).hexdigest(),
        )
        self.assertEqual(
            candidate_provenance["candidate_components_sha256"],
            hashlib.sha256(
                first_bytes["features/candidate_components.jsonl"]
            ).hexdigest(),
        )
        self.assertTrue(
            set(frozen["source_manifest_sha256s"]) <= verified_manifest_hashes
        )
        self.assertTrue(
            set(candidate_provenance["source_manifest_sha256s"])
            <= verified_manifest_hashes
        )
        for name in (
            "features/candidate_raw_features.jsonl",
            "features/candidate_components.jsonl",
            "locked/predictions.jsonl",
        ):
            rows = [json.loads(line) for line in first_bytes[name].splitlines()]
            self.assertTrue(rows)
            if name != "locked/predictions.jsonl":
                forbidden = {
                    "actual_winner",
                    "away_is_winner",
                    "away_score",
                    "home_is_winner",
                    "home_score",
                    "outcome",
                    "result",
                }
                stack = list(rows)
                while stack:
                    value = stack.pop()
                    if isinstance(value, dict):
                        self.assertTrue(forbidden.isdisjoint(value))
                        stack.extend(value.values())
                    elif isinstance(value, list):
                        stack.extend(value)
        raw_rows = [
            json.loads(line)
            for line in first_bytes[
                "features/candidate_raw_features.jsonl"
            ].splitlines()
        ]
        self.assertTrue(
            all(
                row["provenance"]["max_history_date"] < row["official_date"]
                and row["provenance"]["statcast_target_excluded"] is True
                for row in raw_rows
            )
        )


if __name__ == "__main__":
    unittest.main()
