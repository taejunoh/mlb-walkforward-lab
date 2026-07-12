"""Verified-source orchestration for frozen training and candidate preparation."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .manifest import RawStore
from .mlb_stats import fetch_league_era_records
from .statcast import StatcastData, parse_statcast, statcast_url


_OUTCOME_TOKENS = ("score", "winner", "outcome", "result", "market", "odds")
_TARGET_PARTICIPANT_FIELDS = (
    "game_pk", "official_date", "game_type", "scheduled_start_utc",
    "away_team", "away_team_id", "home_team", "home_team_id",
    "away_probable_pitcher_id", "away_probable_pitcher_name",
    "home_probable_pitcher_id", "home_probable_pitcher_name",
    "venue_id", "venue_name", "scheduled_innings", "doubleheader_marker",
    "away_lineup", "home_lineup",
)


def eligible_starter_ids(
    raw_features: Iterable[Mapping[str, object]],
) -> list[int]:
    """Return the exact starter set whose eligible rows feed model artifacts."""
    return sorted(
        {
            int(raw[side]["starter"]["pitcher_id"])
            for raw in raw_features
            if raw.get("eligible") is True
            for side in ("away", "home")
        }
    )


def project_target_participant_inputs(
    games: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Read only pregame participant fields; never scan target result mappings."""
    projected = []
    for source in games:
        if not isinstance(source, Mapping):
            raise ValueError("target schedule games must be mappings")
        row = {
            field: source[field]
            for field in _TARGET_PARTICIPANT_FIELDS
            if field in source
        }
        for lineup_field in ("away_lineup", "home_lineup"):
            lineup = row.get(lineup_field)
            if lineup is None:
                continue
            if isinstance(lineup, (str, bytes)) or not isinstance(lineup, Sequence):
                raise ValueError("target lineups must be sequences")
            row[lineup_field] = [
                {
                    "player_id": player["player_id"],
                    "player_name": player["player_name"],
                }
                for player in lineup
            ]
        row["status"] = "Scheduled"
        projected.append(row)
    return projected


def safe_target_games(
    games: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Project normalized target schedules to a result-free pregame contract."""
    output = []
    for source in games:
        if not isinstance(source, Mapping):
            raise ValueError("target schedule games must be mappings")
        row = {
            key: value
            for key, value in source.items()
            if not any(token in key.lower() for token in _OUTCOME_TOKENS)
        }
        status = source.get("status")
        if isinstance(status, str) and any(
            token in status.lower() for token in ("cancel", "postpon")
        ):
            row["status"] = status
        else:
            row["status"] = "Scheduled"
        output.append(row)
    return output


def _days(start: str, end: str) -> set[date]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last:
        raise ValueError("source range start must not follow end")
    return {first + timedelta(days=offset) for offset in range((last - first).days + 1)}


def load_statcast_range(
    raw_root: str | Path,
    start: str,
    end: str,
    *,
    catalog=None,
) -> tuple[StatcastData, list[str]]:
    """Load exactly one verified, gap-free Statcast partition for a date range."""
    expected = _days(start, end)
    catalog = RawStore(Path(raw_root)).verify_manifests() if catalog is None else catalog
    by_path = {Path(item["manifest_path"]): item for item in catalog}
    selected = [by_path[path] for path in _statcast_manifest_paths(Path(raw_root), start, end, catalog=catalog)]
    observed: set[date] = set()
    pitches = []
    plate_appearances = []
    hashes = []
    for verified in selected:
        query = verified["manifest"]["query"]
        first, last = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
        covered = _days(first.isoformat(), last.isoformat())
        if observed & covered:
            raise ValueError("verified Statcast query ranges overlap")
        observed.update(covered)
        parsed = parse_statcast(Path(verified["payload_path"]))
        if any(
            not first <= date.fromisoformat(str(row["game_date"])) <= last
            for row in parsed.pitches
        ):
            raise ValueError("Statcast row is outside manifest query range")
        pitches.extend(parsed.pitches)
        plate_appearances.extend(parsed.plate_appearances)
        hashes.append(str(verified["manifest_sha256"]))
    if observed != expected:
        raise RuntimeError("missing verified Statcast coverage")
    return StatcastData(tuple(pitches), tuple(plate_appearances)), hashes


def load_pitcher_bf_range(
    raw_root: str | Path,
    start: str,
    end: str,
    *,
    catalog=None,
) -> tuple[dict[int, int], list[str]]:
    """Stream terminal PA rows into compact prior-career BF counts."""
    from collections import defaultdict
    from .statcast import iter_statcast_pitches

    counts: defaultdict[int, int] = defaultdict(int)
    hashes = []
    catalog = RawStore(Path(raw_root)).verify_manifests() if catalog is None else catalog
    verified_by_path = {
        Path(item["manifest_path"]): item
        for item in catalog
    }
    for path in _statcast_manifest_paths(Path(raw_root), start, end, catalog=catalog):
        verified = verified_by_path[path]
        query = verified["manifest"]["query"]
        first, last = date.fromisoformat(query["start"]), date.fromisoformat(query["end"])
        for row in iter_statcast_pitches(Path(verified["payload_path"])):
            if not first <= date.fromisoformat(str(row["game_date"])) <= last:
                raise ValueError("Statcast row is outside manifest query range")
            if row.get("events"):
                counts[int(row["pitcher_id"])] += 1
        hashes.append(str(verified["manifest_sha256"]))
    return dict(sorted(counts.items())), hashes


def merge_pitcher_bf_counts(
    counts_by_range: Iterable[Mapping[int, int]],
) -> dict[int, int]:
    merged: dict[int, int] = {}
    for counts in counts_by_range:
        for pitcher_id, value in counts.items():
            if (
                isinstance(pitcher_id, bool)
                or not isinstance(pitcher_id, int)
                or pitcher_id <= 0
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError("pitcher BF counts must be nonnegative integer mappings")
            merged[pitcher_id] = merged.get(pitcher_id, 0) + value
    return dict(sorted(merged.items()))


def load_league_era_from_raw(
    raw_root: str | Path,
    season_open_by_year: Mapping[int, str],
    as_of_dates: Sequence[str],
    *,
    catalog=None,
) -> tuple[dict[str, dict[str, object]], list[str]]:
    """Load D-1 league ERA records without permitting a network fallback."""

    def missing(_url, **_kwargs):
        raise RuntimeError("missing verified league ERA manifest")

    records = fetch_league_era_records(
        RawStore(Path(raw_root)),
        season_open_by_year,
        as_of_dates,
        fetcher=missing,
        retries=(),
        verified_catalog=catalog,
    )
    hashes = sorted({str(row["manifest_sha256"]) for row in records.values()})
    return records, hashes


def _schedule_evidence(
    raw_root: Path, role: str, start: str, end: str, *, catalog=None
):
    from .prepare import load_verified_schedule_evidence
    from .sources import schedule_url

    expected = {
        "role": role,
        "start": start,
        "end": end,
        "gameType": "R",
        "url": schedule_url(start, end),
    }
    verified_rows = RawStore(raw_root).verify_manifests() if catalog is None else catalog
    matches = []
    for verified in verified_rows:
        if (
            verified["manifest"].get("source") == "mlb_schedule"
            and verified["manifest"].get("query") == expected
        ):
            matches.append(Path(verified["manifest_path"]))
    if len(matches) != 1:
        raise RuntimeError(f"missing or ambiguous verified {role} schedule manifest")
    return load_verified_schedule_evidence(
        raw_root, matches[0], verified_catalog=verified_rows
    )


def _statcast_manifest_paths(
    raw_root: Path, start: str, end: str, *, catalog=None
) -> list[Path]:
    expected = _days(start, end)
    selected = []
    observed: set[date] = set()
    verified_rows = RawStore(raw_root).verify_manifests() if catalog is None else catalog
    for verified in verified_rows:
        manifest = verified["manifest"]
        query = manifest.get("query")
        if manifest.get("source") != "baseball_savant_statcast" or not isinstance(query, dict):
            continue
        try:
            covered = _days(str(query["start"]), str(query["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if covered <= expected:
            if (
                set(query) != {"start", "end", "game_type", "url"}
                or query.get("game_type") != "R"
                or query.get("url") != statcast_url(query["start"], query["end"])
            ):
                raise ValueError("Statcast manifest query is not canonical")
            selected.append((min(covered), max(covered), Path(verified["manifest_path"])))
    selected.sort()
    paths = []
    for first, last, path in selected:
        covered = _days(first.isoformat(), last.isoformat())
        if observed & covered:
            raise ValueError("verified Statcast query ranges overlap")
        observed.update(covered)
        paths.append(path)
    if observed != expected:
        raise RuntimeError("missing verified Statcast coverage")
    return paths


def _safe_participants(schedule_games, statcast, *, target=False):
    from .prepare import build_actual_participants

    source = (
        project_target_participant_inputs(schedule_games)
        if target
        else schedule_games
    )
    return safe_target_games(build_actual_participants(source, statcast).games)


def _history_build(evidence, team_map, venue_reference=None, *, catalog=None):
    from .prepare import schedule_history_rows

    enriched = []
    for game in evidence.games:
        row = dict(game)
        row["away_team_code"] = team_map.mapping[row["away_team"]]
        row["home_team_code"] = team_map.mapping[row["home_team"]]
        enriched.append(row)
    return schedule_history_rows(
        enriched,
        venue_reference or {},
        evidence,
        verified_catalog=catalog,
    )


def build_result_free_schedule_history(
    evidence,
    team_code_by_name: Mapping[str, str],
    statcast: StatcastData,
):
    """Build schedule history without reading target status, scores, or winners."""
    from .prepare import ScheduleHistoryBuild, VerifiedScheduleEvidence

    if not isinstance(evidence, VerifiedScheduleEvidence):
        raise ValueError("verified schedule evidence is required")
    if not isinstance(statcast, StatcastData):
        raise ValueError("verified Statcast data is required")
    played_pks = {
        int(row["game_pk"])
        for row in (*statcast.pitches, *statcast.plate_appearances)
    }
    innings_by_game: dict[int, int] = {}
    for pitch in statcast.pitches:
        game_pk = int(pitch["game_pk"])
        inning = pitch.get("inning")
        if type(inning) is not int or inning <= 0:
            raise ValueError("result-free Statcast inning is invalid")
        innings_by_game[game_pk] = max(innings_by_game.get(game_pk, 0), inning)
    rows = []
    for game in evidence.games:
        game_pk = game["game_pk"]
        if game_pk not in played_pks:
            continue
        away_name, home_name = game["away_team"], game["home_team"]
        if away_name not in team_code_by_name or home_name not in team_code_by_name:
            raise ValueError("result-free schedule history team mapping is incomplete")
        innings = innings_by_game.get(game_pk)
        row = {
            "game_pk": game_pk,
            "official_date": game["official_date"],
            "team_codes": [
                team_code_by_name[away_name],
                team_code_by_name[home_name],
            ],
            "home_team_code": team_code_by_name[home_name],
            "venue_name": game["venue_name"],
            "innings": innings,
            "doubleheader_marker": (
                game["doubleheader_marker"]
                if "doubleheader_marker" in game
                else None
            ),
        }
        rows.append(row)
    rows.sort(key=lambda row: (row["official_date"], row["game_pk"]))
    if not rows:
        raise ValueError("result-free schedule history has no played games")
    expected = tuple(sorted(row["game_pk"] for row in rows))
    return ScheduleHistoryBuild(
        tuple(rows),
        evidence.coverage_start,
        evidence.coverage_end,
        True,
        evidence.manifest_sha256,
        expected,
        {
            "source_manifest_sha256": evidence.manifest_sha256,
            "played_game_count": len(rows),
            "played_game_pks": expected,
            "played_evidence": "STATCAST_GAME_PK",
            "result_fields_accessed": False,
        },
    )


def _combined_history(builds):
    from .prepare import ScheduleHistoryBuild

    builds = list(builds)
    rows = tuple(row for build in builds for row in build.rows)
    expected = tuple(sorted(row["game_pk"] for row in rows))
    aggregate = hashlib.sha256(
        "".join(build.source_manifest_sha256 or "" for build in builds).encode("ascii")
    ).hexdigest()
    return ScheduleHistoryBuild(
        tuple(sorted(rows, key=lambda row: (row["official_date"], row["game_pk"]))),
        min(build.coverage_start for build in builds),
        max(build.coverage_end for build in builds),
        all(build.coverage_complete for build in builds),
        aggregate,
        expected,
        {"source_manifest_sha256s": [build.source_manifest_sha256 for build in builds]},
    )


def _raw_features_for_games(
    statcast,
    games,
    slot_weights,
    history_build,
    team_codes,
    league_era,
):
    from .feature_engine import build_raw_game_features
    from .prepare import build_pregame_history
    from .scoring_prep import build_raw_features_by_date

    return build_raw_features_by_date(
        games,
        lambda representative: build_pregame_history(
            statcast, representative, slot_weights, history_build
        ),
        lambda game, history: build_raw_game_features(
            game, history, team_codes, league_era
        ),
    )


def _training_outcomes(games: Iterable[Mapping[str, object]]) -> dict[int, int]:
    outcomes = {}
    for game in games:
        if game.get("status") not in {"Final", "Completed Early"}:
            continue
        away, home = game.get("away_is_winner"), game.get("home_is_winner")
        if type(away) is not bool or type(home) is not bool or away == home:
            raise ValueError("training schedule outcome is inconsistent")
        outcomes[int(game["game_pk"])] = int(home)
    return outcomes


def prepare_training_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    """Build frozen normalization inputs and training edges from verified raw data."""
    from collections import Counter

    from .career_bf import apply_prior_career_bf, load_prior_career_bf_records
    from .config import load_config
    from .features.offense import derive_slot_weights
    from .pipeline import _canonical_json, _canonical_jsonl, _safe_data_dir, _stage, _write_once
    from .prepare import (
        derive_team_code_map,
        derive_training_slot_rows,
        load_verified_statcast_evidence,
    )
    from .scoring_prep import build_training_edges, compute_normalization
    from .statcast import regular_season_ranges

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    if not (root / "raw").is_dir():
        raise RuntimeError("missing verified raw sources")
    raw_root = _stage(root, "raw", create=False)
    seasons = regular_season_ranges(config["extension_end"])[:3]
    raw_catalog = RawStore(raw_root).verify_manifests()
    schedule_evidence = {
        year: _schedule_evidence(
            raw_root, "training", start, end, catalog=raw_catalog
        )
        for year, (start, end) in zip((2022, 2023, 2024), seasons)
    }
    statcast_paths = [
        path
        for start, end in seasons
        for path in _statcast_manifest_paths(
            raw_root, start, end, catalog=raw_catalog
        )
    ]
    compact = load_verified_statcast_evidence(
        raw_root, statcast_paths, verified_catalog=raw_catalog
    )
    slot_weights = derive_slot_weights(
        derive_training_slot_rows(
            schedule_evidence, compact, verified_catalog=raw_catalog
        )
    )
    raw_features = []
    outcomes = {}
    source_hashes = [
        evidence.manifest_sha256 for evidence in schedule_evidence.values()
    ] + list(compact.manifest_sha256s)
    season_open_by_year = {year: start for year, (start, _end) in zip((2022, 2023, 2024), seasons)}
    completed_bf_by_year = {}
    for year, (start, end) in zip((2022, 2023, 2024), seasons):
        statcast, hashes = load_statcast_range(
            raw_root, start, end, catalog=raw_catalog
        )
        evidence = schedule_evidence[year]
        team_map = derive_team_code_map(evidence.games, statcast, year)
        participants = _safe_participants(evidence.games, statcast)
        history = _history_build(evidence, team_map, catalog=raw_catalog)
        target_games = [game for game in participants if game.get("status") == "Scheduled"]
        as_of_dates = sorted(
            {
                (date.fromisoformat(game["official_date"]) - timedelta(days=1)).isoformat()
                for game in target_games
                if date.fromisoformat(game["official_date"]) > date.fromisoformat(start)
            }
        )
        league_era, era_hashes = load_league_era_from_raw(
            raw_root, season_open_by_year, as_of_dates, catalog=raw_catalog
        )
        season_raw = _raw_features_for_games(
            statcast,
            target_games,
            slot_weights,
            history,
            team_map.mapping,
            league_era,
        )
        prior_completed = merge_pitcher_bf_counts(
            completed_bf_by_year.values()
        )
        for raw in season_raw:
            if raw.get("eligible") is not True:
                continue
            for side in ("away", "home"):
                starter = raw[side]["starter"]
                starter["career_bf"] += prior_completed.get(
                    starter["pitcher_id"], 0
                )
        raw_features.extend(season_raw)
        completed_bf_by_year[year] = dict(
            Counter(int(row["pitcher_id"]) for row in statcast.plate_appearances)
        )
        outcomes.update(_training_outcomes(evidence.games))
        source_hashes.extend(hashes)
        source_hashes.extend(era_hashes)
    career_records, career_hashes = load_prior_career_bf_records(
        raw_root, eligible_starter_ids(raw_features), catalog=raw_catalog
    )
    if not career_hashes:
        raise RuntimeError("missing frozen prior career BF manifests")
    apply_prior_career_bf(raw_features, career_records)
    source_hashes.extend(career_hashes)
    normalization = compute_normalization(raw_features)
    edges = build_training_edges(raw_features, normalization, outcomes)
    calibration = _stage(root, "calibration")
    raw_bytes = _canonical_jsonl(raw_features)
    _write_once(
        calibration / "training_raw_features.jsonl",
        raw_bytes,
        "training raw features",
        root,
    )
    edge_path = _write_once(
        calibration / "training_edges.jsonl",
        _canonical_jsonl(edges),
        "calibration input",
        root,
    )
    frozen = {
        "schema_version": 1,
        "normalization": normalization,
        "slot_weights": {str(key): value for key, value in slot_weights.items()},
        "source_manifest_sha256s": sorted(set(source_hashes)),
        "normalization_input_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "training_raw_count": len(raw_features),
        "max_training_date": max(row["official_date"] for row in edges),
    }
    _write_once(
        calibration / "frozen_inputs.json",
        _canonical_json(frozen),
        "frozen inputs",
        root,
    )
    return edge_path


def fetch_league_era_stage(config_path: str | Path, data_dir: str | Path) -> list[object]:
    """Acquire all schedule-derived D-1 league ERA records into the RawStore."""
    from .config import load_config
    from .pipeline import _safe_data_dir, _stage
    from .statcast import regular_season_ranges

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    if not (root / "raw").is_dir():
        raise RuntimeError("missing verified raw schedules")
    raw_root = _stage(root, "raw", create=False)
    raw_catalog = RawStore(raw_root).verify_manifests()
    ranges = regular_season_ranges(config["extension_end"])
    schedule_sets = [
        _schedule_evidence(
            raw_root, "training", start, end, catalog=raw_catalog
        )
        for start, end in ranges[:3]
    ] + [
        _schedule_evidence(
            raw_root,
            "history",
            "2025-03-18",
            "2025-04-30",
            catalog=raw_catalog,
        ),
        _schedule_evidence(
            raw_root,
            "backtest",
            config["initial_window"]["start"],
            config["extension_end"],
            catalog=raw_catalog,
        ),
    ]
    openings = {
        year: start for year, (start, _end) in zip((2022, 2023, 2024, 2025), ranges)
    }
    as_of_dates = sorted(
        {
            prior.isoformat()
            for evidence in schedule_sets
            for game in evidence.games
            for prior in [date.fromisoformat(game["official_date"]) - timedelta(days=1)]
            if prior >= date.fromisoformat(openings[prior.year])
        }
    )
    before = {
        item["manifest_sha256"]
        for item in raw_catalog
        if item["manifest"].get("source") == "mlb_stats_pitching"
    }
    records = fetch_league_era_records(
        RawStore(raw_root),
        openings,
        as_of_dates,
        verified_catalog=raw_catalog,
    )
    after = {
        item["manifest_sha256"]: item
        for item in RawStore(raw_root).verify_manifests()
        if item["manifest"].get("source") == "mlb_stats_pitching"
    }
    expected = {row["manifest_sha256"] for row in records.values()}
    if not expected <= set(after):
        raise RuntimeError("league ERA acquisition did not publish every record")
    return [after[digest] for digest in sorted(expected - before)]


def fetch_prior_career_bf_stage(
    config_path: str | Path, data_dir: str | Path
) -> dict[int, dict[str, object]]:
    """Fetch frozen through-2021 career BF for reconstructed 2025 starters."""
    from .career_bf import fetch_prior_career_bf_records
    from .config import load_config
    from .feature_engine import actual_starter_index
    from .pipeline import _safe_data_dir, _stage

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    if not (root / "raw").is_dir():
        raise RuntimeError("missing verified raw Statcast sources")
    raw_root = _stage(root, "raw", create=False)
    catalog = RawStore(raw_root).verify_manifests()
    ranges = [
        ("2022-04-07", "2022-10-05"),
        ("2023-03-30", "2023-10-01"),
        ("2024-03-20", "2024-09-30"),
        ("2025-03-18", config["extension_end"]),
    ]
    starters = {}
    for start, end in ranges:
        statcast, _hashes = load_statcast_range(
            raw_root, start, end, catalog=catalog
        )
        starters.update(actual_starter_index(statcast))
    pitcher_ids = sorted(
        {
            pitcher_id
            for values in starters.values()
            for pitcher_id in values.values()
        }
    )
    if not pitcher_ids:
        raise RuntimeError("missing reconstructed 2025 starter identities")
    return fetch_prior_career_bf_records(
        RawStore(raw_root),
        pitcher_ids,
        verified_catalog=catalog,
    )


def prepare_candidates_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    """Build result-free 2025 candidate components from the frozen v2 card."""
    from .career_bf import apply_prior_career_bf, load_prior_career_bf_records
    from .config import load_config
    from .pipeline import (
        _canonical_json,
        _canonical_jsonl,
        _load_model_card,
        _safe_data_dir,
        _stage,
        _write_once,
    )
    from .prepare import derive_team_code_map
    from .scoring_prep import build_candidate_components

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    if (root / "outcomes").exists():
        raise ValueError("prepare-candidates refuses an outcomes stage")
    if not (root / "raw").is_dir():
        raise RuntimeError("missing verified raw sources")
    raw_root = _stage(root, "raw", create=False)
    raw_catalog = RawStore(raw_root).verify_manifests()
    card, _hash = _load_model_card(
        root, config_path, verified_catalog=raw_catalog
    )
    if card["schema_version"] != 2:
        raise ValueError("prepare-candidates requires a frozen v2 model card")
    history_evidence = _schedule_evidence(
        raw_root,
        "history",
        "2025-03-18",
        "2025-04-30",
        catalog=raw_catalog,
    )
    backtest_evidence = _schedule_evidence(
        raw_root,
        "backtest",
        config["initial_window"]["start"],
        config["extension_end"],
        catalog=raw_catalog,
    )
    statcast, statcast_hashes = load_statcast_range(
        raw_root,
        "2025-03-18",
        config["extension_end"],
        catalog=raw_catalog,
    )
    team_map = derive_team_code_map(history_evidence.games, statcast, 2025)
    participants = _safe_participants(
        backtest_evidence.games, statcast, target=True
    )
    history = _combined_history(
        (
            build_result_free_schedule_history(
                history_evidence, team_map.mapping, statcast
            ),
            build_result_free_schedule_history(
                backtest_evidence, team_map.mapping, statcast
            ),
        )
    )
    as_of_dates = sorted(
        {
            (date.fromisoformat(game["official_date"]) - timedelta(days=1)).isoformat()
            for game in participants
        }
    )
    league_era, era_hashes = load_league_era_from_raw(
        raw_root,
        {2025: "2025-03-18"},
        as_of_dates,
        catalog=raw_catalog,
    )
    slot_weights = {int(key): float(value) for key, value in card["slot_weights"].items()}
    raw_features = _raw_features_for_games(
        statcast,
        participants,
        slot_weights,
        history,
        team_map.mapping,
        league_era,
    )
    training_ranges = (
        ("2022-04-07", "2022-10-05"),
        ("2023-03-30", "2023-10-01"),
        ("2024-03-20", "2024-09-30"),
    )
    prior_loaded = [
        load_pitcher_bf_range(
            raw_root, start, end, catalog=raw_catalog
        )
        for start, end in training_ranges
    ]
    prior_bf = merge_pitcher_bf_counts([counts for counts, _hashes in prior_loaded])
    prior_hashes = [digest for _counts, hashes in prior_loaded for digest in hashes]
    for raw in raw_features:
        if raw.get("eligible") is not True:
            continue
        for side in ("away", "home"):
            starter = raw[side]["starter"]
            starter["career_bf"] += prior_bf.get(starter["pitcher_id"], 0)
    starter_ids = eligible_starter_ids(raw_features)
    career_records, career_hashes = load_prior_career_bf_records(
        raw_root, starter_ids, catalog=raw_catalog
    )
    apply_prior_career_bf(raw_features, career_records)
    rows = build_candidate_components(
        participants, raw_features, card["normalization"]
    )
    features = _stage(root, "features")
    raw_bytes = _canonical_jsonl(raw_features)
    component_bytes = _canonical_jsonl(rows)
    _write_once(
        features / "candidate_raw_features.jsonl",
        raw_bytes,
        "candidate raw features",
        root,
    )
    component_path = _write_once(
        features / "candidate_components.jsonl",
        component_bytes,
        "candidate components",
        root,
    )
    provenance = {
        "schema_version": 1,
        "candidate_raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "candidate_components_sha256": hashlib.sha256(component_bytes).hexdigest(),
        "source_manifest_sha256s": sorted(
            {
                history_evidence.manifest_sha256,
                backtest_evidence.manifest_sha256,
                *statcast_hashes,
                *era_hashes,
                *prior_hashes,
                *career_hashes,
            }
        ),
    }
    _write_once(
        features / "candidate_provenance.json",
        _canonical_json(provenance),
        "candidate provenance",
        root,
    )
    return component_path


def prepare_outcomes_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    """Prepare official outcomes and D-1 baseline only after a verified lock exists."""
    from .config import load_config
    from .outcomes import _load_lock
    from .pipeline import (
        _canonical_json,
        _canonical_jsonl,
        _load_model_card,
        _require_raw_schedule,
        _safe_data_dir,
        _stage,
        _verify_locked_source_provenance,
        _write_once,
    )
    from .scoring_prep import prepare_locked_outcomes

    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    lock_manifest = root / "locked" / "lock_manifest.json"
    if not lock_manifest.exists():
        raise RuntimeError("missing verified lock manifest")
    card, model_hash = _load_model_card(root, config_path)
    lock_metadata, locked = _load_lock(lock_manifest, model_hash)
    if card["schema_version"] == 2:
        _verify_locked_source_provenance(root, lock_metadata["sha256"])
    _history_hash, history = _require_raw_schedule(root, config, "history")
    _backtest_hash, backtest = _require_raw_schedule(root, config, "backtest")
    outcomes, baseline = prepare_locked_outcomes(
        locked, [*history.values(), *backtest.values()]
    )
    stage = _stage(root, "outcomes")
    path = _write_once(
        stage / "outcomes_input.jsonl",
        _canonical_jsonl(outcomes),
        "outcomes input",
        root,
    )
    _write_once(
        stage / "baseline.json",
        _canonical_json({str(key): value for key, value in baseline.items()}),
        "baseline mapping",
        root,
    )
    return path


__all__ = [
    "build_result_free_schedule_history",
    "load_league_era_from_raw",
    "load_pitcher_bf_range",
    "load_statcast_range",
    "merge_pitcher_bf_counts",
    "fetch_league_era_stage",
    "fetch_prior_career_bf_stage",
    "prepare_candidates_stage",
    "prepare_outcomes_stage",
    "prepare_training_stage",
    "project_target_participant_inputs",
    "safe_target_games",
]
