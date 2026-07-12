from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
from mlb_backtest.protocols import (
    MINIMUM_SOURCE_NAMES,
    RunSpec,
    expected_historical_run,
    load_run_spec,
    parse_run_spec,
    require_exact_keys,
)


MINIMUM_SOURCES = MINIMUM_SOURCE_NAMES
PROTECTED_PREFLIGHT_ROOTS = ("data", "audit")
SMALL_SEALED_HASH_ROOTS = (
    "data/v5_1_bt/locked",
    "data/v5_1_bt/outcomes",
    "data/v5_1_bt/calibration",
    "audit/v5_1_bt",
)
SOURCE_ALIASES = MappingProxyType(
    {
        "mlb_schedule_game_feed": frozenset(
            {"mlb_schedule_game_feed", "mlb_schedule"}
        ),
        "baseball_savant_statcast": frozenset(
            {"baseball_savant_statcast", "statcast"}
        ),
        "mlb_stats_pitching": frozenset(
            {"mlb_stats_pitching", "mlb_stats_pitcher_bf"}
        ),
        "venue_team_timezone_table": frozenset(
            {
                "venue_team_timezone_table",
                "mlb_venue_team_timezone",
                "venue_timezone",
            }
        ),
        "prior_schedule_history": frozenset(
            {"prior_schedule_history", "mlb_schedule_history"}
        ),
    }
)
REQUIRED_FIELDS = MappingProxyType(
    {
        "mlb_schedule_game_feed": (
            "gamePk",
            "officialDate",
            "gameType",
            "teams",
            "venue",
            "gameDate",
            "status",
        ),
        "baseball_savant_statcast": (
            "game_pk",
            "game_date",
            "batter",
            "pitcher",
            "events",
            "estimated_woba_using_speedangle",
        ),
        "mlb_stats_pitching": ("season", "earnedRuns", "outs", "battersFaced"),
        "venue_team_timezone_table": (
            "venue_id",
            "team_id",
            "timezone",
            "effective_start",
            "effective_end",
        ),
        "prior_schedule_history": (
            "gamePk",
            "officialDate",
            "away_team_id",
            "home_team_id",
            "venue_id",
            "innings",
            "status",
        ),
    }
)
MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "source",
        "logical_query_id",
        "parent_logical_query_id",
        "query",
        "child_range",
        "raw_path",
        "raw_sha256",
        "sidecar_path",
        "sidecar_sha256",
        "retrieved_at_utc",
        "split_index",
        "split_count",
    }
)
SIDECAR_KEYS = frozenset(
    {
        "schema_version",
        "schema_fields",
        "row_count",
        "game_count",
        "game_pks",
        "event_dates",
    }
)
PREFLIGHT_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "run_id",
        "mode",
        "status",
        "generated_from_inventory_sha256",
        "protected_tree_before",
        "protected_tree_after",
        "inventory_errors",
        "seasons",
        "optional_sources",
        "may_fit_H22",
        "fitted_model_cards_present",
        "fitted_model_card_namespace",
        "acquisition_steps",
        "execution_steps_after_ready",
    }
)
SEASON_KEYS = frozenset({"minimum_contract", "sources"})
STEP_KEYS = frozenset({"stage", "status", "command", "prerequisite"})
SOURCE_RECORD_KEYS = frozenset(
    {
        "source",
        "required",
        "expected_start",
        "expected_end",
        "observed_start",
        "observed_end",
        "logical_query_ids",
        "row_count",
        "game_count",
        "schema_fields",
        "missing_critical_fields",
        "duplicate_game_pks",
        "retrieved_at_utc",
        "content_sha256",
        "manifest_sha256",
        "split_children",
        "status",
    }
)
OPTIONAL_SOURCE_CONTRACT = MappingProxyType(
    {
        "market_odds": "ABSENT_NULL",
        "environment": "ABSENT_NEUTRAL",
        "defense": "ABSENT_NEUTRAL",
        "catcher": "ABSENT_NEUTRAL",
        "injury_restriction": "ABSENT_NULL",
    }
)
INVENTORY_ERROR_KEYS = frozenset({"path", "code", "detail"})


def protected_inventory(root: Path) -> dict[str, object]:
    metadata: dict[str, list[int]] = {}
    for relative in PROTECTED_PREFLIGHT_ROOTS:
        base = root / relative
        if not base.exists():
            continue
        for path in sorted(item for item in base.rglob("*") if item.is_file()):
            file_stat = path.stat(follow_symlinks=False)
            metadata[path.relative_to(root).as_posix()] = [
                file_stat.st_size,
                file_stat.st_mtime_ns,
                file_stat.st_ino,
                file_stat.st_mode,
            ]
    hashes = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for relative in SMALL_SEALED_HASH_ROOTS
        if (root / relative).exists()
        for path in sorted(item for item in (root / relative).rglob("*") if item.is_file())
    }
    return {
        "metadata_path_size_mtime_inode_mode": metadata,
        "small_sealed_sha256": hashes,
    }


def planned_step(stage: str, command: str, prerequisite: str) -> dict[str, str]:
    return {
        "stage": stage,
        "status": "NOT_IMPLEMENTED_PLANNED",
        "command": command,
        "prerequisite": prerequisite,
    }


def safe_relative(root: Path, raw: object) -> Path:
    if not isinstance(raw, str):
        raise ValueError("manifest path must be a string")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("manifest path escapes root")
    result = (root / relative).resolve()
    resolved_root = root.resolve()
    if result != resolved_root and resolved_root not in result.parents:
        raise ValueError("manifest path escapes root")
    return result


def canonical_source(alias: str) -> str:
    matches = [name for name, aliases in SOURCE_ALIASES.items() if alias in aliases]
    if len(matches) != 1:
        raise ValueError(f"unknown or ambiguous source alias: {alias}")
    return matches[0]


def parent_logical_query_id(source: str, year: int, start: str, end: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "source": source,
                "season": year,
                "start_date": start,
                "end_date": end,
            }
        )
    ).hexdigest()


def _error(path: Path, root: Path, code: str, detail: str) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "code": code,
        "detail": detail,
    }


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def inventory_existing_manifests(
    root: Path,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, str], ...]]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    candidates = sorted(
        path
        for base in (root / "inventory", root / "data/v5_2_qd/raw")
        if base.exists()
        for path in base.rglob("*.manifest.json")
    )
    for path in candidates:
        try:
            manifest = require_exact_keys(
                _read_json(path),
                MANIFEST_KEYS,
                f"manifest:{path.relative_to(root)}",
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(
                _error(path, root, "MALFORMED_MANIFEST_JSON", type(exc).__name__)
            )
            continue
        except ValueError as exc:
            errors.append(_error(path, root, "INVALID_MANIFEST_CONTRACT", str(exc)))
            continue
        try:
            row = _validate_manifest(root, path, manifest)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            code = (
                "MALFORMED_SIDECAR_JSON"
                if str(exc).startswith("sidecar unreadable:")
                else "INVALID_MANIFEST_EVIDENCE"
            )
            errors.append(_error(path, root, code, str(exc)))
            continue
        rows.append(row)
    legacy_rows, legacy_errors = inventory_legacy_manifests(root)
    return tuple(rows) + legacy_rows, tuple(errors) + legacy_errors


def _validate_manifest(
    root: Path, path: Path, item: Mapping[str, object]
) -> dict[str, object]:
    failures: list[str] = []
    if item["schema_version"] != 1:
        failures.append("MANIFEST_SCHEMA_VERSION")
    source = canonical_source(str(item["source"]))
    query = require_exact_keys(
        item["query"],
        frozenset({"season", "start_date", "end_date"}),
        "manifest.query",
    )
    child = require_exact_keys(
        item["child_range"],
        frozenset({"start_date", "end_date"}),
        "manifest.child_range",
    )
    year = _strict_int(query["season"], "query.season", minimum=2018)
    start = _iso_date(query["start_date"], "query.start_date")
    end = _iso_date(query["end_date"], "query.end_date")
    child_start = _iso_date(child["start_date"], "child_range.start_date")
    child_end = _iso_date(child["end_date"], "child_range.end_date")
    if start != f"{year}-01-01" or end != f"{year}-12-31":
        failures.append("QUERY_DATE_RANGE_MISMATCH")
    if start > end or child_start > child_end or child_start < start or child_end > end:
        failures.append("INVALID_DATE_RANGE")
    raw_path = safe_relative(root, item["raw_path"])
    sidecar_path = safe_relative(root, item["sidecar_path"])
    try:
        sidecar = require_exact_keys(_read_json(sidecar_path), SIDECAR_KEYS, "sidecar")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sidecar unreadable: {type(exc).__name__}") from exc
    if not raw_path.is_file():
        failures.append("RAW_MISSING")
    elif sha256_file(raw_path) != item["raw_sha256"]:
        failures.append("RAW_HASH_MISMATCH")
    if sha256_file(sidecar_path) != item["sidecar_sha256"]:
        failures.append("SIDECAR_HASH_MISMATCH")
    if sidecar["schema_version"] != 1:
        failures.append("SIDECAR_SCHEMA_VERSION")
    row_count = _strict_int(sidecar["row_count"], "sidecar.row_count", minimum=0)
    game_count = _strict_int(sidecar["game_count"], "sidecar.game_count", minimum=0)
    if game_count > row_count:
        failures.append("INVALID_COUNTS")
    schema_fields = _string_list(sidecar["schema_fields"], "sidecar.schema_fields")
    game_pks = _integer_list(sidecar["game_pks"], "sidecar.game_pks")
    event_dates = _string_list(sidecar["event_dates"], "sidecar.event_dates")
    if len(event_dates) != 2:
        failures.append("INVALID_EVENT_DATE_RANGE")
    elif event_dates != [child_start, child_end]:
        failures.append("EVENT_DATE_RANGE_MISMATCH")
    missing = sorted(set(REQUIRED_FIELDS[source]) - set(schema_fields))
    if missing:
        failures.append("MISSING_CRITICAL_FIELDS")
    if len(game_pks) != len(set(game_pks)):
        failures.append("DUPLICATE_GAME_PK")
    if any(game_pk <= 0 for game_pk in game_pks):
        failures.append("INVALID_GAME_PK")
    expected_parent = parent_logical_query_id(source, year, start, end)
    if item["parent_logical_query_id"] != expected_parent:
        failures.append("SPLIT_PARENT_ID_MISMATCH")
    split_index = _strict_int(item["split_index"], "split_index", minimum=1)
    split_count = _strict_int(item["split_count"], "split_count", minimum=1)
    if split_index > split_count:
        failures.append("INVALID_SPLIT_SET")
    return {
        "source": source,
        "query": {"season": year, "start_date": start, "end_date": end},
        "child_range": {"start_date": child_start, "end_date": child_end},
        "logical_query_id": _strict_string(item["logical_query_id"], "logical_query_id"),
        "parent_logical_query_id": _strict_string(
            item["parent_logical_query_id"], "parent_logical_query_id"
        ),
        "split_index": split_index,
        "split_count": split_count,
        "row_count": row_count,
        "game_count": game_count,
        "game_pks": game_pks,
        "event_dates": event_dates,
        "schema_fields": schema_fields,
        "retrieved_at_utc": _strict_string(
            item["retrieved_at_utc"], "retrieved_at_utc"
        ),
        "raw_sha256": _sha256(item["raw_sha256"], "raw_sha256"),
        "sidecar_sha256": _sha256(item["sidecar_sha256"], "sidecar_sha256"),
        "manifest_path": path.relative_to(root).as_posix(),
        "manifest_sha256": sha256_file(path),
        "failures": sorted(set(failures)),
    }


def _strict_int(value: object, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _strict_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _sha256(value: object, field: str) -> str:
    text = _strict_string(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return text


def _iso_date(value: object, field: str) -> str:
    text = _strict_string(value, field)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must be a canonical ISO date")
    return text


def _string_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string list")
    return list(value)


def _integer_list(value: object, field: str) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ValueError(f"{field} must be an integer list")
    return list(value)


def inventory_legacy_manifests(
    root: Path,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, str], ...]]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    base = root / "data/v5_1_bt/raw"
    candidates: Sequence[Path] = sorted(base.rglob("*.manifest.json")) if base.exists() else ()
    for path in candidates:
        try:
            raw = _read_json(path)
            if not isinstance(raw, Mapping):
                raise ValueError("legacy manifest is not an object")
            query = raw["query"]
            legacy_alias = str(raw["source"])
            sources = (
                ("mlb_schedule_game_feed", "prior_schedule_history")
                if legacy_alias == "mlb_schedule"
                else (canonical_source(legacy_alias),)
            )
            bounds = date_boundaries(query)
            if bounds is None:
                raise ValueError("legacy query lacks bounds")
            start_year = date.fromisoformat(bounds[0]).year
            end_year = date.fromisoformat(bounds[1]).year
            for source in sources:
                for year in range(max(2018, start_year), min(2024, end_year) + 1):
                    if query_overlaps_year(query, year):
                        rows.append(
                            {
                                "source": source,
                                "query": {
                                    "season": year,
                                    "start_date": bounds[0],
                                    "end_date": bounds[1],
                                },
                                "legacy": True,
                                "manifest_path": path.relative_to(root).as_posix(),
                                "manifest_sha256": sha256_file(path),
                            }
                        )
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(
                _error(path, root, "LEGACY_MANIFEST_UNADAPTABLE", type(exc).__name__)
            )
    return tuple(rows), tuple(errors)


def date_boundaries(query: object) -> tuple[str, str] | None:
    if not isinstance(query, Mapping):
        return None
    start = query.get("start") or query.get("start_date") or query.get("startDate")
    end = query.get("end") or query.get("end_date") or query.get("endDate")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    date.fromisoformat(start)
    date.fromisoformat(end)
    return start, end


def query_overlaps_year(query: object, year: int) -> bool:
    bounds = date_boundaries(query)
    return bounds is not None and bounds[0] <= f"{year}-12-31" and bounds[1] >= f"{year}-01-01"


def fitted_cards_present(root: Path) -> bool:
    return any(
        (root / "models/v5_2_qd/folds" / fold / "model_card.json").exists()
        for fold in ("H22", "H23", "H24")
    )


def exact_acquisition_steps(
    seasons: Mapping[str, object],
) -> list[dict[str, str]]:
    del seasons
    return [
        planned_step(
            "acquire-raw-2018-2024",
            "PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_acquire --years 2018:2024 --output data/v5_2_qd/raw",
            "approved deferred execution plan",
        ),
        planned_step(
            "verify-source-coverage",
            "PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_sources --root data/v5_2_qd/raw",
            "acquire-raw-2018-2024 complete",
        ),
    ]


def exact_execution_steps(run_spec: RunSpec) -> list[dict[str, str]]:
    if run_spec.run_id != "historical_walkforward_500":
        raise ValueError("execution plan requires historical_walkforward_500")
    run_path = "protocols/v5_2_qd/historical_walkforward_500.json"
    return [
        planned_step(
            "fit-fold-cards",
            f"PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_fit --run-spec {run_path}",
            "preflight status READY",
        ),
        planned_step(
            "lock-historical-500",
            f"PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_run --run-spec {run_path} --stop-after lock",
            "H22/H23/H24 cards sealed",
        ),
        planned_step(
            "join-outcomes",
            f"PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_run --run-spec {run_path} --resume outcomes",
            "all candidate/date locks sealed",
        ),
        planned_step(
            "report-and-verify",
            "PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.v52_verify_run --run-id historical_walkforward_500",
            "exactly 500 graded dates resolved",
        ),
    ]


def assess_season(
    year: int, inventory: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    expected_start, expected_end = f"{year}-01-01", f"{year}-12-31"
    records: dict[str, object] = {}
    for source in MINIMUM_SOURCES:
        all_matched = [
            row
            for row in inventory
            if row["source"] == source and int(row["query"]["season"]) == year
        ]
        legacy = [row for row in all_matched if row.get("legacy") is True]
        matched = [row for row in all_matched if row.get("legacy") is not True]
        if not matched and legacy:
            record = {
                "source": source,
                "required": True,
                "expected_start": expected_start,
                "expected_end": expected_end,
                "observed_start": min(row["query"]["start_date"] for row in legacy),
                "observed_end": max(row["query"]["end_date"] for row in legacy),
                "logical_query_ids": [],
                "row_count": 0,
                "game_count": 0,
                "schema_fields": [],
                "missing_critical_fields": [
                    "V52_MANIFEST_SIDECAR_CONTRACT_NOT_PROVEN"
                ],
                "duplicate_game_pks": [],
                "retrieved_at_utc": [],
                "content_sha256": [],
                "manifest_sha256": sorted(
                    {str(row["manifest_sha256"]) for row in legacy}
                ),
                "split_children": sorted(
                    {str(row["manifest_path"]) for row in legacy}
                ),
                "status": "OBSERVED_LEGACY_NOT_V52_COMPLETE",
            }
            require_exact_keys(record, SOURCE_RECORD_KEYS, f"season.{year}.{source}")
            records[source] = record
            continue
        observed_dates = sorted(
            (str(row["child_range"]["start_date"]), str(row["child_range"]["end_date"]))
            for row in matched
        )
        parents = {str(row["parent_logical_query_id"]) for row in matched}
        split_counts = {int(row["split_count"]) for row in matched}
        split_indices = sorted(int(row["split_index"]) for row in matched)
        coverage_ok = ranges_cover(observed_dates, expected_start, expected_end)
        split_ok = (
            len(parents) == 1
            and len(split_counts) == 1
            and split_indices == list(range(1, next(iter(split_counts)) + 1))
        )
        failures = sorted(
            {str(failure) for row in matched for failure in row["failures"]}
        )
        duplicates = sorted(
            game_pk
            for game_pk, count in Counter(
                game_pk for row in matched for game_pk in row["game_pks"]
            ).items()
            if count > 1
        )
        missing_fields = sorted(
            set(REQUIRED_FIELDS[source])
            - {str(field) for row in matched for field in row["schema_fields"]}
        )
        complete = coverage_ok and split_ok and not failures and not duplicates and not missing_fields
        blockers = missing_fields + failures
        if not matched:
            blockers = ["manifest-bound full-season evidence"]
        else:
            if not coverage_ok:
                blockers.append("INCOMPLETE_DATE_RANGE")
            if not split_ok:
                blockers.append("INVALID_SPLIT_SET")
            if duplicates:
                blockers.append("DUPLICATE_GAME_PK_ACROSS_SPLITS")
        record = {
            "source": source,
            "required": True,
            "expected_start": expected_start,
            "expected_end": expected_end,
            "observed_start": observed_dates[0][0] if observed_dates else None,
            "observed_end": observed_dates[-1][1] if observed_dates else None,
            "logical_query_ids": sorted(parents),
            "row_count": sum(int(row["row_count"]) for row in matched),
            "game_count": sum(int(row["game_count"]) for row in matched),
            "schema_fields": sorted(
                {str(field) for row in matched for field in row["schema_fields"]}
            ),
            "missing_critical_fields": sorted(set(blockers)),
            "duplicate_game_pks": duplicates,
            "retrieved_at_utc": sorted(
                str(row["retrieved_at_utc"]) for row in matched
            ),
            "content_sha256": sorted(str(row["raw_sha256"]) for row in matched),
            "manifest_sha256": sorted(
                str(row["manifest_sha256"]) for row in matched
            ),
            "split_children": sorted(
                str(row["manifest_path"]) for row in matched
            ),
            "status": "COMPLETE" if complete else "MISSING" if not matched else "INVALID",
        }
        require_exact_keys(record, SOURCE_RECORD_KEYS, f"season.{year}.{source}")
        records[source] = record
    return {
        "minimum_contract": (
            "COMPLETE"
            if all(row["status"] == "COMPLETE" for row in records.values())
            else "MISSING"
        ),
        "sources": records,
    }


def ranges_cover(
    ranges: Sequence[tuple[str, str]], expected_start: str, expected_end: str
) -> bool:
    if not ranges or ranges[0][0] != expected_start or ranges[-1][1] != expected_end:
        return False
    previous_end: date | None = None
    for start_text, end_text in ranges:
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        if start > end or (
            previous_end is not None and start != previous_end + timedelta(days=1)
        ):
            return False
        previous_end = end
    return True


def optional_source_status(
    inventory: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, str]]:
    del inventory
    return {name: {"status": status} for name, status in OPTIONAL_SOURCE_CONTRACT.items()}


def all_minimum_complete(seasons: Mapping[str, Mapping[str, object]]) -> bool:
    return all(season["minimum_contract"] == "COMPLETE" for season in seasons.values())


def build_preflight(root: Path, run_spec: RunSpec) -> dict[str, object]:
    before = protected_inventory(root)
    inventory, inventory_errors = inventory_existing_manifests(root)
    seasons = {str(year): assess_season(year, inventory) for year in range(2018, 2025)}
    after = protected_inventory(root)
    if before != after:
        raise RuntimeError("preflight modified protected evidence")
    payload = {
        "schema_version": 1,
        "run_id": run_spec.run_id,
        "mode": "READ_ONLY",
        "status": (
            "READY"
            if all_minimum_complete(seasons) and not inventory_errors
            else "BLOCKED_REQUIRED_ACQUISITION"
        ),
        "seasons": seasons,
        "generated_from_inventory_sha256": hashlib.sha256(
            canonical_json_bytes(inventory)
        ).hexdigest(),
        "protected_tree_before": before,
        "protected_tree_after": after,
        "inventory_errors": list(inventory_errors),
        "optional_sources": optional_source_status(inventory),
        "may_fit_H22": all(
            seasons[str(year)]["minimum_contract"] == "COMPLETE"
            for year in range(2018, 2022)
        ),
        "fitted_model_cards_present": fitted_cards_present(root),
        "fitted_model_card_namespace": (
            "models/v5_2_qd/folds/{H22,H23,H24}/model_card.json"
        ),
        "acquisition_steps": exact_acquisition_steps(seasons),
        "execution_steps_after_ready": exact_execution_steps(run_spec),
    }
    return parse_preflight(payload)


def parse_preflight(raw: object) -> dict[str, object]:
    obj = require_exact_keys(raw, PREFLIGHT_REQUIRED_KEYS, "preflight")
    if (
        obj["schema_version"] != 1
        or obj["mode"] != "READ_ONLY"
        or obj["run_id"] != "historical_walkforward_500"
    ):
        raise ValueError("preflight identity differs")
    if obj["fitted_model_card_namespace"] != (
        "models/v5_2_qd/folds/{H22,H23,H24}/model_card.json"
    ):
        raise ValueError("preflight fitted-card namespace differs")
    if not isinstance(obj["fitted_model_cards_present"], bool):
        raise ValueError("preflight fitted-card presence must be boolean")
    if obj["optional_sources"] != optional_source_status(()):
        raise ValueError("preflight optional-source neutral/null contract differs")
    seasons = require_exact_keys(
        obj["seasons"], frozenset(map(str, range(2018, 2025))), "preflight.seasons"
    )
    for year in map(str, range(2018, 2025)):
        season = require_exact_keys(
            seasons[year], SEASON_KEYS, f"preflight.seasons.{year}"
        )
        if season["minimum_contract"] not in {"COMPLETE", "MISSING"}:
            raise ValueError(f"preflight.seasons.{year}.minimum_contract differs")
        sources = require_exact_keys(
            season["sources"],
            frozenset(MINIMUM_SOURCES),
            f"preflight.seasons.{year}.sources",
        )
        for source, record in sources.items():
            item = require_exact_keys(
                record,
                SOURCE_RECORD_KEYS,
                f"preflight.seasons.{year}.sources.{source}",
            )
            if (
                item["source"] != source
                or item["required"] is not True
                or item["expected_start"] != f"{year}-01-01"
                or item["expected_end"] != f"{year}-12-31"
                or item["status"]
                not in {
                    "COMPLETE",
                    "MISSING",
                    "INVALID",
                    "OBSERVED_LEGACY_NOT_V52_COMPLETE",
                }
            ):
                raise ValueError(
                    f"preflight.seasons.{year}.sources.{source} frozen values differ"
                )
        expected_minimum = (
            "COMPLETE"
            if all(record["status"] == "COMPLETE" for record in sources.values())
            else "MISSING"
        )
        if season["minimum_contract"] != expected_minimum:
            raise ValueError(f"preflight.seasons.{year} minimum contract differs")
    errors = obj["inventory_errors"]
    if not isinstance(errors, list):
        raise ValueError("preflight.inventory_errors must be a list")
    for index, error in enumerate(errors):
        require_exact_keys(error, INVENTORY_ERROR_KEYS, f"preflight.errors[{index}]")
    acquisition = _step_list(obj["acquisition_steps"], "preflight.acquisition_steps")
    execution = _step_list(
        obj["execution_steps_after_ready"], "preflight.execution_steps_after_ready"
    )
    if acquisition != exact_acquisition_steps(seasons):
        raise ValueError("preflight acquisition steps differ")
    expected_execution = exact_execution_steps(
        parse_run_spec(expected_historical_run())
    )
    if execution != expected_execution:
        raise ValueError("preflight execution steps differ")
    try:
        result = json.loads(json.dumps(obj, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("preflight must be finite JSON") from exc
    if result["status"] not in {"READY", "BLOCKED_REQUIRED_ACQUISITION"}:
        raise ValueError("preflight status differs")
    expected_ready = all_minimum_complete(result["seasons"]) and not result[
        "inventory_errors"
    ]
    expected_fit = all(
        result["seasons"][str(year)]["minimum_contract"] == "COMPLETE"
        for year in range(2018, 2022)
    )
    if (result["status"] == "READY") != expected_ready or result[
        "may_fit_H22"
    ] is not expected_fit:
        raise ValueError("preflight status/fold gate differs")
    if result["protected_tree_before"] != result["protected_tree_after"]:
        raise ValueError("preflight protected-tree snapshots differ")
    return result


def _step_list(value: object, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    result: list[dict[str, str]] = []
    for index, step in enumerate(value):
        item = require_exact_keys(step, STEP_KEYS, f"{path}[{index}]")
        if item["status"] != "NOT_IMPLEMENTED_PLANNED" or any(
            not isinstance(item[key], str) for key in STEP_KEYS
        ):
            raise ValueError("preflight must not claim unimplemented CLI is executable")
        result.append(dict(item))
    return result


def render_preflight_markdown(report: Mapping[str, object]) -> str:
    rows = [
        f"| {year} | {record['minimum_contract']} | "
        + ", ".join(
            f"{name}={source['status']}"
            for name, source in record["sources"].items()
            if source["status"] != "COMPLETE"
        )
        + " |"
        for year, record in sorted(report["seasons"].items())
    ]
    return "\n".join(
        [
            "# v5.2-QD Historical 500 Source Preflight",
            "",
            f"Status: **{report['status']}**",
            "",
            "| Season | Minimum contract | Blocking source status |",
            "|---|---|---|",
            *rows,
            "",
            "Optional sources remain neutral/null and do not block READY.",
            "",
            "H22 fitting permitted: " + ("Yes" if report["may_fit_H22"] else "No"),
            "",
        ]
    )


def preflight_schema_document() -> dict[str, object]:
    optional_properties = {
        name: {
            "type": "object",
            "required": ["status"],
            "properties": {"status": {"const": status}},
            "additionalProperties": False,
        }
        for name, status in OPTIONAL_SOURCE_CONTRACT.items()
    }
    source_properties = {
        "source": {"enum": list(MINIMUM_SOURCES)},
        "required": {"const": True},
        "expected_start": {"type": "string"},
        "expected_end": {"type": "string"},
        "observed_start": {"type": ["string", "null"]},
        "observed_end": {"type": ["string", "null"]},
        "logical_query_ids": {"type": "array", "items": {"type": "string"}},
        "row_count": {"type": "integer", "minimum": 0},
        "game_count": {"type": "integer", "minimum": 0},
        "schema_fields": {"type": "array", "items": {"type": "string"}},
        "missing_critical_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
        "duplicate_game_pks": {"type": "array", "items": {"type": "integer"}},
        "retrieved_at_utc": {"type": "array", "items": {"type": "string"}},
        "content_sha256": {"type": "array", "items": {"type": "string"}},
        "manifest_sha256": {"type": "array", "items": {"type": "string"}},
        "split_children": {"type": "array", "items": {"type": "string"}},
        "status": {
            "enum": [
                "COMPLETE",
                "MISSING",
                "INVALID",
                "OBSERVED_LEGACY_NOT_V52_COMPLETE",
            ]
        },
    }
    step_properties = {
        "stage": {"type": "string"},
        "status": {"const": "NOT_IMPLEMENTED_PLANNED"},
        "command": {"type": "string"},
        "prerequisite": {"type": "string"},
    }
    season_source_properties = {
        source: {"$ref": "#/$defs/source_record"} for source in MINIMUM_SOURCES
    }
    season_properties = {
        "minimum_contract": {"enum": ["COMPLETE", "MISSING"]},
        "sources": {
            "type": "object",
            "required": list(MINIMUM_SOURCES),
            "properties": season_source_properties,
            "additionalProperties": False,
        },
    }
    top_properties = {
        "schema_version": {"const": 1},
        "run_id": {"const": "historical_walkforward_500"},
        "mode": {"const": "READ_ONLY"},
        "status": {"enum": ["READY", "BLOCKED_REQUIRED_ACQUISITION"]},
        "generated_from_inventory_sha256": {"type": "string"},
        "protected_tree_before": {"type": "object"},
        "protected_tree_after": {"type": "object"},
        "inventory_errors": {"type": "array"},
        "seasons": {
            "type": "object",
            "required": list(map(str, range(2018, 2025))),
            "properties": {
                str(year): {"$ref": "#/$defs/season"} for year in range(2018, 2025)
            },
            "additionalProperties": False,
        },
        "optional_sources": {
            "type": "object",
            "required": list(OPTIONAL_SOURCE_CONTRACT),
            "properties": optional_properties,
            "additionalProperties": False,
        },
        "may_fit_H22": {"type": "boolean"},
        "fitted_model_cards_present": {"type": "boolean"},
        "fitted_model_card_namespace": {
            "const": "models/v5_2_qd/folds/{H22,H23,H24}/model_card.json"
        },
        "acquisition_steps": {"type": "array", "items": {"$ref": "#/$defs/step"}},
        "execution_steps_after_ready": {
            "type": "array",
            "items": {"$ref": "#/$defs/step"},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": sorted(PREFLIGHT_REQUIRED_KEYS),
        "properties": top_properties,
        "additionalProperties": False,
        "$defs": {
            "season": {
                "type": "object",
                "required": sorted(SEASON_KEYS),
                "properties": season_properties,
                "additionalProperties": False,
            },
            "source_record": {
                "type": "object",
                "required": sorted(SOURCE_RECORD_KEYS),
                "properties": source_properties,
                "additionalProperties": False,
            },
            "step": {
                "type": "object",
                "required": sorted(STEP_KEYS),
                "properties": step_properties,
                "additionalProperties": False,
            },
            "minimum_sources": list(MINIMUM_SOURCES),
            "statuses": ["READY", "BLOCKED_REQUIRED_ACQUISITION"],
        },
    }


def write_preflight(
    root: Path,
    run_path: Path,
    json_path: Path,
    markdown_path: Path,
    schema_path: Path,
) -> None:
    report = build_preflight(root, load_run_spec(run_path))
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_bytes(canonical_json_bytes(report) + b"\n")
    markdown_path.write_text(render_preflight_markdown(report), encoding="utf-8")
    schema_path.write_bytes(canonical_json_bytes(preflight_schema_document()) + b"\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--run-spec", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    args = parser.parse_args(argv)
    write_preflight(
        args.root.resolve(),
        args.run_spec,
        args.json,
        args.markdown,
        args.schema,
    )
    return os.EX_OK


if __name__ == "__main__":
    raise SystemExit(main())
