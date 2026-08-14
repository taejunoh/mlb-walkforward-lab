from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
from types import MappingProxyType

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
from mlb_backtest.protocols import (
    MINIMUM_SOURCE_NAMES,
    RunSpec,
    expected_historical_run,
    _publish_bundle,
    parse_run_spec,
    require_exact_keys,
)


MINIMUM_SOURCES = MINIMUM_SOURCE_NAMES
PROTECTED_PREFLIGHT_ROOTS = ("data", "audit", "inventory", "inventory_optional")
SMALL_SEALED_HASH_ROOTS = (
    "data/v5_1_bt/locked",
    "data/v5_1_bt/outcomes",
    "data/v5_1_bt/calibration",
    "audit/v5_1_bt",
)
SOURCE_ALIASES = MappingProxyType(
    {
        "mlb_schedule_game_feed": frozenset({"mlb_schedule_game_feed", "mlb_schedule"}),
        "baseball_savant_statcast": frozenset({"baseball_savant_statcast", "statcast"}),
        "mlb_stats_pitching": frozenset({"mlb_stats_pitching", "mlb_stats_pitcher_bf"}),
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
        "game_pks",
        "schema_fields",
        "missing_critical_fields",
        "duplicate_game_pks",
        "retrieved_at_utc",
        "content_sha256",
        "manifest_sha256",
        "split_children",
        "queries",
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
GAME_GRAIN_SOURCES = frozenset(
    {"mlb_schedule_game_feed", "baseball_savant_statcast", "prior_schedule_history"}
)
REFERENCE_GRAIN_SOURCES = frozenset(
    {"mlb_stats_pitching", "venue_team_timezone_table"}
)
OPTIONAL_REQUIRED_FIELDS = MappingProxyType(
    {
        "market_odds": (
            "gamePk",
            "observed_at_utc",
            "away_no_vig_probability",
            "home_no_vig_probability",
        ),
        "environment": (
            "gamePk",
            "observed_at_utc",
            "temperature_f",
            "wind_mph",
            "roof_status",
        ),
        "defense": ("gamePk", "observed_at_utc", "team_id", "oaa", "opportunities"),
        "catcher": (
            "gamePk",
            "observed_at_utc",
            "player_id",
            "framing",
            "opportunities",
        ),
        "injury_restriction": (
            "gamePk",
            "observed_at_utc",
            "player_id",
            "restriction_status",
        ),
    }
)
QUERY_EVIDENCE_KEYS = frozenset(
    {
        "evidence_kind",
        "source",
        "manifest_path",
        "manifest_sha256",
        "logical_query_id",
        "parent_logical_query_id",
        "split_index",
        "split_count",
        "query",
        "child_range",
    }
)
QUERY_KEYS = frozenset({"season", "start_date", "end_date"})
RANGE_KEYS = frozenset({"start_date", "end_date"})
OPTIONAL_RECORD_KEYS = frozenset({"status", "fallback_status", "queries", "errors"})
INVENTORY_ERROR_KEYS = frozenset({"path", "code", "detail"})
INVENTORY_ERROR_CODES = frozenset(
    {
        "MALFORMED_MANIFEST_JSON",
        "MALFORMED_SIDECAR_JSON",
        "INVALID_MANIFEST_CONTRACT",
        "INVALID_MANIFEST_EVIDENCE",
        "LEGACY_MANIFEST_UNADAPTABLE",
        "RAW_HASH_MISMATCH",
        "RAW_MISSING",
        "SIDECAR_HASH_MISMATCH",
        "MANIFEST_SCHEMA_VERSION",
        "SIDECAR_SCHEMA_VERSION",
        "INVALID_COUNTS",
        "GAME_COUNT_MISMATCH",
        "INVALID_EVENT_DATE_RANGE",
        "EVENT_DATE_RANGE_MISMATCH",
        "MISSING_CRITICAL_FIELDS",
        "DUPLICATE_GAME_PK",
        "INVALID_GAME_PK",
        "QUERY_DATE_RANGE_MISMATCH",
        "INVALID_DATE_RANGE",
        "SPLIT_PARENT_ID_MISMATCH",
        "INVALID_SPLIT_SET",
    }
)
PROTECTED_SNAPSHOT_KEYS = frozenset(
    {"metadata_path_size_mtime_inode_mode", "small_sealed_sha256"}
)
EvidenceFingerprint = tuple[int, int, int, int, int, str]
EvidenceSnapshot = dict[Path, EvidenceFingerprint]
DeclaredInputs = set[Path]


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
        for path in sorted(
            item for item in (root / relative).rglob("*") if item.is_file()
        )
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
    return root / relative


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


def child_logical_query_id(parent: str, split_index: int) -> str:
    return f"{parent}:{split_index}"


def _error(path: Path, root: Path, code: str, detail: str) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "code": code,
        "detail": detail,
    }


def _assert_no_symlink_path(root: Path, path: Path) -> None:
    root_absolute = root.absolute()
    path_absolute = path.absolute()
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise ValueError("inventory path escapes root") from exc
    current = root_absolute
    for part in relative.parts:
        current /= part
        try:
            file_stat = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(file_stat.st_mode):
            raise ValueError(f"inventory symlink forbidden: {current}")


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_mode,
    )


def _verified_file_read(
    root: Path,
    path: Path,
    *,
    collect: bool,
    evidence: EvidenceSnapshot | None = None,
) -> bytes | str:
    _assert_no_symlink_path(root, path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("inventory evidence must be a regular file")
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            digest.update(chunk)
            if collect:
                chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _assert_no_symlink_path(root, path)
    path_stat = os.lstat(path)
    if (
        _file_identity(before) != _file_identity(after)
        or before.st_dev != path_stat.st_dev
        or before.st_ino != path_stat.st_ino
        or before.st_size != path_stat.st_size
        or before.st_mtime_ns != path_stat.st_mtime_ns
        or total != before.st_size
    ):
        raise ValueError("inventory evidence changed during read")
    digest_text = digest.hexdigest()
    if evidence is not None:
        resolved = path.resolve(strict=True)
        fingerprint = (*_file_identity(before), digest_text)
        previous = evidence.get(resolved)
        if previous is not None and previous != fingerprint:
            raise ValueError("input evidence changed between reads")
        evidence[resolved] = fingerprint
    return b"".join(chunks) if collect else digest_text


def _verified_json_bytes(
    root: Path, path: Path, evidence: EvidenceSnapshot | None = None
) -> bytes:
    value = _verified_file_read(root, path, collect=True, evidence=evidence)
    if not isinstance(value, bytes):
        raise AssertionError("verified JSON read did not return bytes")
    return value


def _verified_sha256(
    root: Path, path: Path, evidence: EvidenceSnapshot | None = None
) -> str:
    value = _verified_file_read(root, path, collect=False, evidence=evidence)
    if not isinstance(value, str):
        raise AssertionError("verified digest read did not return text")
    return value


def inventory_existing_manifests(
    root: Path,
    evidence: EvidenceSnapshot | None = None,
    declared_inputs: DeclaredInputs | None = None,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, str], ...]]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    candidates = _manifest_candidates(root)
    for path in candidates:
        inferred_optional = _optional_source_from_path(root, path)
        if inferred_optional is not None:
            continue
        try:
            manifest_bytes = _verified_json_bytes(root, path, evidence)
            raw_manifest = json.loads(manifest_bytes)
            if (
                isinstance(raw_manifest, Mapping)
                and raw_manifest.get("source") in OPTIONAL_SOURCE_CONTRACT
            ) or inferred_optional is not None:
                continue
            manifest = require_exact_keys(
                raw_manifest,
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
            row = _validate_manifest(
                root,
                path,
                manifest,
                manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                evidence=evidence,
                declared_inputs=declared_inputs,
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            code = (
                "MALFORMED_SIDECAR_JSON"
                if str(exc).startswith("sidecar unreadable:")
                else "INVALID_MANIFEST_EVIDENCE"
            )
            errors.append(_error(path, root, code, str(exc)))
            continue
        rows.append(row)
    legacy_rows, legacy_errors = inventory_legacy_manifests(root, evidence)
    return tuple(rows) + legacy_rows, tuple(errors) + legacy_errors


def _validate_manifest(
    root: Path,
    path: Path,
    item: Mapping[str, object],
    *,
    optional_source: str | None = None,
    manifest_sha256: str,
    evidence: EvidenceSnapshot | None = None,
    declared_inputs: DeclaredInputs | None = None,
) -> dict[str, object]:
    failures: list[str] = []
    if item["schema_version"] != 1:
        failures.append("MANIFEST_SCHEMA_VERSION")
    if optional_source is None:
        source = canonical_source(str(item["source"]))
        required_fields = REQUIRED_FIELDS[source]
    else:
        if item["source"] != optional_source:
            raise ValueError("optional manifest source/path mismatch")
        source = optional_source
        required_fields = OPTIONAL_REQUIRED_FIELDS[source]
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
        raise ValueError("QUERY_DATE_RANGE_MISMATCH")
    if start > end or child_start > child_end or child_start < start or child_end > end:
        raise ValueError("INVALID_DATE_RANGE")
    raw_path = safe_relative(root, item["raw_path"])
    sidecar_path = safe_relative(root, item["sidecar_path"])
    if declared_inputs is not None:
        declared_inputs.update(
            {raw_path.resolve(strict=False), sidecar_path.resolve(strict=False)}
        )
    try:
        sidecar_bytes = _verified_json_bytes(root, sidecar_path, evidence)
        sidecar = require_exact_keys(json.loads(sidecar_bytes), SIDECAR_KEYS, "sidecar")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"sidecar unreadable: {type(exc).__name__}") from exc
    if not raw_path.is_file():
        failures.append("RAW_MISSING")
    elif _verified_sha256(root, raw_path, evidence) != item["raw_sha256"]:
        failures.append("RAW_HASH_MISMATCH")
    if hashlib.sha256(sidecar_bytes).hexdigest() != item["sidecar_sha256"]:
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
    missing = sorted(set(required_fields) - set(schema_fields))
    if missing:
        failures.append("MISSING_CRITICAL_FIELDS")
    if len(game_pks) != len(set(game_pks)):
        failures.append("DUPLICATE_GAME_PK")
    if game_count != len(set(game_pks)):
        failures.append("GAME_COUNT_MISMATCH")
    if any(game_pk <= 0 for game_pk in game_pks):
        failures.append("INVALID_GAME_PK")
    expected_parent = parent_logical_query_id(source, year, start, end)
    if item["parent_logical_query_id"] != expected_parent:
        raise ValueError("SPLIT_PARENT_ID_MISMATCH")
    split_index = _strict_int(item["split_index"], "split_index", minimum=1)
    split_count = _strict_int(item["split_count"], "split_count", minimum=1)
    if split_index > split_count:
        failures.append("INVALID_SPLIT_SET")
    if item["logical_query_id"] != child_logical_query_id(
        expected_parent, split_index
    ):
        raise ValueError("SPLIT_LOGICAL_QUERY_ID_MISMATCH")
    return {
        "source": source,
        "query": {"season": year, "start_date": start, "end_date": end},
        "child_range": {"start_date": child_start, "end_date": child_end},
        "logical_query_id": _strict_string(
            item["logical_query_id"], "logical_query_id"
        ),
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
        "retrieved_at_utc": _iso_datetime(item["retrieved_at_utc"], "retrieved_at_utc"),
        "raw_sha256": _sha256(item["raw_sha256"], "raw_sha256"),
        "sidecar_sha256": _sha256(item["sidecar_sha256"], "sidecar_sha256"),
        "manifest_path": path.relative_to(root).as_posix(),
        "manifest_sha256": manifest_sha256,
        "failures": sorted(set(failures)),
    }


def inventory_optional_manifests(
    root: Path,
    evidence: EvidenceSnapshot | None = None,
    declared_inputs: DeclaredInputs | None = None,
) -> tuple[
    dict[str, tuple[dict[str, object], ...]], dict[str, tuple[dict[str, str], ...]]
]:
    rows: dict[str, list[dict[str, object]]] = {
        source: [] for source in OPTIONAL_SOURCE_CONTRACT
    }
    errors: dict[str, list[dict[str, str]]] = {
        source: [] for source in OPTIONAL_SOURCE_CONTRACT
    }
    candidates = _manifest_candidates(root)
    for source in OPTIONAL_SOURCE_CONTRACT:
        for path in candidates:
            inferred = _optional_source_from_path(root, path)
            if inferred is not None and inferred != source:
                continue
            try:
                manifest_bytes = _verified_json_bytes(root, path, evidence)
                raw_manifest = json.loads(manifest_bytes)
                declared = (
                    raw_manifest.get("source")
                    if isinstance(raw_manifest, Mapping)
                    else None
                )
                if declared != source and inferred != source:
                    continue
                manifest = require_exact_keys(
                    raw_manifest,
                    MANIFEST_KEYS,
                    f"optional_manifest:{path.relative_to(root)}",
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors[source].append(
                    _error(path, root, "MALFORMED_MANIFEST_JSON", type(exc).__name__)
                )
                continue
            except ValueError as exc:
                errors[source].append(
                    _error(path, root, "INVALID_MANIFEST_CONTRACT", str(exc))
                )
                continue
            try:
                row = _validate_manifest(
                    root,
                    path,
                    manifest,
                    optional_source=source,
                    manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                    evidence=evidence,
                    declared_inputs=declared_inputs,
                )
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                code = (
                    "MALFORMED_SIDECAR_JSON"
                    if str(exc).startswith("sidecar unreadable:")
                    else "INVALID_MANIFEST_EVIDENCE"
                )
                errors[source].append(_error(path, root, code, str(exc)))
                continue
            rows[source].append(row)
            for failure in row["failures"]:
                errors[source].append(
                    _error(
                        path,
                        root,
                        str(failure),
                        "optional evidence failed integrity validation",
                    )
                )
        _validate_optional_split_sets(root, source, rows[source], errors[source])
    return (
        {source: tuple(items) for source, items in rows.items()},
        {source: tuple(items) for source, items in errors.items()},
    )


def _manifest_candidates(root: Path) -> tuple[Path, ...]:
    if root.is_symlink():
        raise ValueError("inventory root symlink forbidden")
    bases = (
        root / "inventory",
        root / "data/v5_2_qd/raw",
        root / "inventory_optional",
    )
    for base in bases:
        if base.exists() or base.is_symlink():
            _assert_no_symlink_path(root, base)
    return tuple(
        sorted(
            {
                path
                for base in bases
                if base.exists()
                for path in base.rglob("*.manifest.json")
            }
        )
    )


def _optional_source_from_path(root: Path, path: Path) -> str | None:
    parts = set(path.relative_to(root).parts)
    matches = [source for source in OPTIONAL_SOURCE_CONTRACT if source in parts]
    return matches[0] if len(matches) == 1 else None


def _validate_optional_split_sets(
    root: Path,
    source: str,
    rows: Sequence[Mapping[str, object]],
    errors: list[dict[str, str]],
) -> None:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for row in rows:
        grouped.setdefault(str(row["parent_logical_query_id"]), []).append(row)
    for group in grouped.values():
        first = group[0]
        split_counts = {int(row["split_count"]) for row in group}
        split_indices = sorted(int(row["split_index"]) for row in group)
        ranges = sorted(
            (
                str(row["child_range"]["start_date"]),
                str(row["child_range"]["end_date"]),
            )
            for row in group
        )
        query = first["query"]
        valid = (
            len(split_counts) == 1
            and split_indices == list(range(1, next(iter(split_counts)) + 1))
            and ranges_cover(ranges, str(query["start_date"]), str(query["end_date"]))
        )
        if not valid:
            errors.append(
                _error(
                    root / str(first["manifest_path"]),
                    root,
                    "INVALID_SPLIT_SET",
                    f"optional {source} split set is incomplete",
                )
            )
    duplicates = sorted(
        game_pk
        for game_pk, count in Counter(
            game_pk for row in rows for game_pk in row["game_pks"]
        ).items()
        if count > 1
    )
    if duplicates:
        first = rows[0]
        errors.append(
            _error(
                root / str(first["manifest_path"]),
                root,
                "DUPLICATE_GAME_PK",
                f"optional {source} duplicate gamePk values: {duplicates}",
            )
        )


def query_evidence(row: Mapping[str, object]) -> dict[str, object]:
    legacy = row.get("legacy") is True
    query = dict(row["query"])
    child = (
        dict(row["child_range"])
        if not legacy
        else {
            "start_date": max(str(query["start_date"]), f"{query['season']}-01-01"),
            "end_date": min(str(query["end_date"]), f"{query['season']}-12-31"),
        }
    )
    manifest_sha256 = str(row["manifest_sha256"])
    source = str(row["source"])
    logical = (
        f"legacy:{manifest_sha256}:{query['season']}:{source}"
        if legacy
        else str(row["logical_query_id"])
    )
    result = {
        "evidence_kind": "LEGACY_MANIFEST_ONLY" if legacy else "V52_MANIFEST_SIDECAR",
        "source": source,
        "manifest_path": str(row["manifest_path"]),
        "manifest_sha256": manifest_sha256,
        "logical_query_id": logical,
        "parent_logical_query_id": None
        if legacy
        else str(row["parent_logical_query_id"]),
        "split_index": None if legacy else int(row["split_index"]),
        "split_count": None if legacy else int(row["split_count"]),
        "query": query,
        "child_range": child,
    }
    require_exact_keys(result, QUERY_EVIDENCE_KEYS, "query_evidence")
    return result


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
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
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


def _iso_datetime(value: object, field: str) -> str:
    text = _strict_string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{field} must be an aware UTC timestamp")
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
    root: Path, evidence: EvidenceSnapshot | None = None
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, str], ...]]:
    rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    base = root / "data/v5_1_bt/raw"
    candidates: Sequence[Path] = (
        sorted(base.rglob("*.manifest.json")) if base.exists() else ()
    )
    for path in candidates:
        try:
            manifest_bytes = _verified_json_bytes(root, path, evidence)
            raw = json.loads(manifest_bytes)
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
                                "manifest_sha256": hashlib.sha256(
                                    manifest_bytes
                                ).hexdigest(),
                            }
                        )
        except (
            KeyError,
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
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
    return (
        bounds is not None
        and bounds[0] <= f"{year}-12-31"
        and bounds[1] >= f"{year}-01-01"
    )


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
                "observed_start": max(
                    expected_start,
                    min(str(row["query"]["start_date"]) for row in legacy),
                ),
                "observed_end": min(
                    expected_end, max(str(row["query"]["end_date"]) for row in legacy)
                ),
                "logical_query_ids": [],
                "row_count": 0,
                "game_count": 0,
                "game_pks": [],
                "schema_fields": [],
                "missing_critical_fields": ["V52_MANIFEST_SIDECAR_CONTRACT_NOT_PROVEN"],
                "duplicate_game_pks": [],
                "retrieved_at_utc": [],
                "content_sha256": [],
                "manifest_sha256": sorted(
                    {str(row["manifest_sha256"]) for row in legacy}
                ),
                "split_children": sorted({str(row["manifest_path"]) for row in legacy}),
                "queries": sorted(
                    (query_evidence(row) for row in legacy),
                    key=lambda item: (
                        int(item["query"]["season"]),
                        str(item["child_range"]["start_date"]),
                        str(item["manifest_path"]),
                    ),
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
        game_pks = sorted(
            {
                game_pk
                for row in matched
                for game_pk in row["game_pks"]
                if game_pk > 0
            }
        )
        missing_fields = sorted(
            set(REQUIRED_FIELDS[source])
            - {str(field) for row in matched for field in row["schema_fields"]}
        )
        complete = (
            coverage_ok
            and split_ok
            and not failures
            and not duplicates
            and not missing_fields
            and sum(int(row["row_count"]) for row in matched) > 0
            and bool(game_pks)
        )
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
            if sum(int(row["row_count"]) for row in matched) == 0:
                blockers.append("EMPTY_DATASET")
            if not game_pks:
                blockers.append("EMPTY_GAME_PK_COVERAGE")
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
            "game_pks": game_pks,
            "schema_fields": sorted(
                {str(field) for row in matched for field in row["schema_fields"]}
            ),
            "missing_critical_fields": sorted(set(blockers)),
            "duplicate_game_pks": duplicates,
            "retrieved_at_utc": sorted(str(row["retrieved_at_utc"]) for row in matched),
            "content_sha256": sorted(str(row["raw_sha256"]) for row in matched),
            "manifest_sha256": sorted(str(row["manifest_sha256"]) for row in matched),
            "split_children": sorted(str(row["manifest_path"]) for row in matched),
            "queries": sorted(
                (query_evidence(row) for row in matched),
                key=lambda item: (
                    int(item["query"]["season"]),
                    str(item["child_range"]["start_date"]),
                    str(item["manifest_path"]),
                ),
            ),
            "status": "COMPLETE"
            if complete
            else "MISSING"
            if not matched
            else "INVALID",
        }
        require_exact_keys(record, SOURCE_RECORD_KEYS, f"season.{year}.{source}")
        records[source] = record
    _apply_cross_source_game_pk_contract(records)
    return {
        "minimum_contract": (
            "COMPLETE"
            if all(row["status"] == "COMPLETE" for row in records.values())
            else "MISSING"
        ),
        "sources": records,
    }


def _cross_source_game_pk_failures(
    records: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    schedule = records["mlb_schedule_game_feed"]
    if schedule["status"] != "COMPLETE":
        return {}
    anchor = set(schedule["game_pks"])
    failures: dict[str, str] = {}
    for source in GAME_GRAIN_SOURCES - {"mlb_schedule_game_feed"}:
        record = records[source]
        if record["status"] == "COMPLETE" and set(record["game_pks"]) != anchor:
            failures[source] = "GAME_PK_COVERAGE_MISMATCH"
    for source in REFERENCE_GRAIN_SOURCES:
        record = records[source]
        if record["status"] == "COMPLETE" and not set(record["game_pks"]).issubset(
            anchor
        ):
            failures[source] = "GAME_PK_OUTSIDE_SCHEDULE"
    return failures


def _apply_cross_source_game_pk_contract(
    records: Mapping[str, dict[str, object]],
) -> None:
    for source, blocker in _cross_source_game_pk_failures(records).items():
        records[source]["status"] = "INVALID"
        records[source]["missing_critical_fields"] = sorted(
            set(records[source]["missing_critical_fields"]) | {blocker}
        )


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
    rows: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    errors: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> dict[str, dict[str, object]]:
    rows = rows or {}
    errors = errors or {}
    result: dict[str, dict[str, object]] = {}
    for source, absent_status in OPTIONAL_SOURCE_CONTRACT.items():
        source_rows = list(rows.get(source, ()))
        source_errors = [dict(item) for item in errors.get(source, ())]
        if source_errors:
            status, fallback = "PRESENT_INVALID", absent_status
        elif source_rows:
            status, fallback = "PRESENT_VALID", None
        else:
            status, fallback = absent_status, absent_status
        result[source] = {
            "status": status,
            "fallback_status": fallback,
            "queries": sorted(
                (query_evidence(row) for row in source_rows),
                key=lambda item: (
                    str(item["manifest_path"]),
                    str(item["logical_query_id"]),
                ),
            ),
            "errors": source_errors,
        }
    return result


def all_minimum_complete(seasons: Mapping[str, Mapping[str, object]]) -> bool:
    return all(season["minimum_contract"] == "COMPLETE" for season in seasons.values())


def build_preflight(root: Path, run_spec: RunSpec) -> dict[str, object]:
    return _build_preflight(root, run_spec, {}, set())


def _build_preflight(
    root: Path,
    run_spec: RunSpec,
    evidence: EvidenceSnapshot,
    declared_inputs: DeclaredInputs,
) -> dict[str, object]:
    before = protected_inventory(root)
    inventory, inventory_errors = inventory_existing_manifests(
        root, evidence, declared_inputs
    )
    optional_rows, optional_errors = inventory_optional_manifests(
        root, evidence, declared_inputs
    )
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
            canonical_json_bytes(
                {
                    "minimum": inventory,
                    "optional": optional_rows,
                    "optional_errors": optional_errors,
                }
            )
        ).hexdigest(),
        "protected_tree_before": before,
        "protected_tree_after": after,
        "inventory_errors": list(inventory_errors),
        "optional_sources": optional_source_status(optional_rows, optional_errors),
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
        type(obj["schema_version"]) is not int
        or obj["schema_version"] != 1
        or obj["mode"] != "READ_ONLY"
        or obj["run_id"] != "historical_walkforward_500"
    ):
        raise ValueError("preflight identity differs")
    _sha256(obj["generated_from_inventory_sha256"], "generated_from_inventory_sha256")
    if obj["fitted_model_card_namespace"] != (
        "models/v5_2_qd/folds/{H22,H23,H24}/model_card.json"
    ):
        raise ValueError("preflight fitted-card namespace differs")
    if type(obj["fitted_model_cards_present"]) is not bool:
        raise ValueError("preflight fitted-card presence must be boolean")
    _validate_protected_snapshot(obj["protected_tree_before"], "protected_tree_before")
    _validate_protected_snapshot(obj["protected_tree_after"], "protected_tree_after")
    _validate_optional_sources(obj["optional_sources"])
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
            _validate_source_record(item, source, int(year))
        cross_failures = _cross_source_game_pk_failures(sources)
        if cross_failures:
            raise ValueError(
                f"preflight.seasons.{year} cross-source gamePk coverage differs"
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
        _validate_inventory_error(error, f"preflight.errors[{index}]")
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
    expected_ready = (
        all_minimum_complete(result["seasons"]) and not result["inventory_errors"]
    )
    expected_fit = all(
        result["seasons"][str(year)]["minimum_contract"] == "COMPLETE"
        for year in range(2018, 2022)
    )
    if (
        type(result["may_fit_H22"]) is not bool
        or (result["status"] == "READY") != expected_ready
        or result["may_fit_H22"] is not expected_fit
    ):
        raise ValueError("preflight status/fold gate differs")
    if result["protected_tree_before"] != result["protected_tree_after"]:
        raise ValueError("preflight protected-tree snapshots differ")
    return result


def _validate_source_record(item: Mapping[str, object], source: str, year: int) -> None:
    expected_start, expected_end = f"{year}-01-01", f"{year}-12-31"
    if (
        item["source"] != source
        or item["required"] is not True
        or item["expected_start"] != expected_start
        or item["expected_end"] != expected_end
        or item["status"]
        not in {"COMPLETE", "MISSING", "INVALID", "OBSERVED_LEGACY_NOT_V52_COMPLETE"}
    ):
        raise ValueError(f"season {year} source {source} frozen values differ")
    _iso_date(item["expected_start"], "expected_start")
    _iso_date(item["expected_end"], "expected_end")
    observed = (item["observed_start"], item["observed_end"])
    if (observed[0] is None) != (observed[1] is None):
        raise ValueError("observed date range must be both null or both present")
    if observed[0] is not None:
        start = _iso_date(observed[0], "observed_start")
        end = _iso_date(observed[1], "observed_end")
        if start > end or start < expected_start or end > expected_end:
            raise ValueError("observed date range differs")
    for field in ("row_count", "game_count"):
        _strict_int(item[field], field, 0)
    if int(item["game_count"]) > int(item["row_count"]):
        raise ValueError("game_count exceeds row_count")
    game_pks = _integer_list(item["game_pks"], "game_pks")
    if any(value <= 0 for value in game_pks) or game_pks != sorted(set(game_pks)):
        raise ValueError("game_pks must be sorted unique positive integers")
    logical_ids = _strict_sorted_strings(item["logical_query_ids"], "logical_query_ids")
    for index, logical_id in enumerate(logical_ids):
        _sha256(logical_id, f"logical_query_ids[{index}]")
    schema_fields = _strict_sorted_strings(item["schema_fields"], "schema_fields")
    missing = _strict_sorted_strings(
        item["missing_critical_fields"], "missing_critical_fields"
    )
    retrieved = _string_list(item["retrieved_at_utc"], "retrieved_at_utc")
    if any(not value for value in retrieved) or retrieved != sorted(retrieved):
        raise ValueError("retrieved_at_utc must be sorted non-empty strings")
    for index, value in enumerate(retrieved):
        _iso_datetime(value, f"retrieved_at_utc[{index}]")
    content_hashes = _strict_hash_list(item["content_sha256"], "content_sha256")
    manifest_hashes = _strict_hash_list(item["manifest_sha256"], "manifest_sha256")
    children = _strict_path_list(item["split_children"], "split_children")
    duplicates = _integer_list(item["duplicate_game_pks"], "duplicate_game_pks")
    if any(value <= 0 for value in duplicates) or duplicates != sorted(set(duplicates)):
        raise ValueError("duplicate_game_pks must be sorted unique positive integers")
    queries_raw = item["queries"]
    if not isinstance(queries_raw, list):
        raise ValueError("queries must be a list")
    queries = [
        _validate_query_evidence(value, source, year, f"queries[{index}]")
        for index, value in enumerate(queries_raw)
    ]
    if queries != sorted(
        queries,
        key=lambda value: (
            int(value["query"]["season"]),
            str(value["child_range"]["start_date"]),
            str(value["manifest_path"]),
        ),
    ):
        raise ValueError("queries are not canonical")
    if set(children) != {str(value["manifest_path"]) for value in queries}:
        raise ValueError("split_children/query paths differ")
    if set(manifest_hashes) != {str(value["manifest_sha256"]) for value in queries}:
        raise ValueError("manifest/query hashes differ")
    status = str(item["status"])
    kinds = {str(value["evidence_kind"]) for value in queries}
    if status == "MISSING":
        if (
            queries
            or observed != (None, None)
            or item["row_count"] != 0
            or item["game_count"] != 0
            or game_pks
            or not missing
            or logical_ids
            or schema_fields
            or retrieved
            or content_hashes
            or manifest_hashes
            or children
            or duplicates
        ):
            raise ValueError("missing source state differs")
    elif status == "OBSERVED_LEGACY_NOT_V52_COMPLETE":
        if (
            kinds != {"LEGACY_MANIFEST_ONLY"}
            or logical_ids
            or schema_fields
            or retrieved
            or content_hashes
            or item["row_count"] != 0
            or item["game_count"] != 0
            or game_pks
            or missing != ["V52_MANIFEST_SIDECAR_CONTRACT_NOT_PROVEN"]
        ):
            raise ValueError("legacy source state differs")
    else:
        if kinds != {"V52_MANIFEST_SIDECAR"} or not queries:
            raise ValueError("v5.2 source query state differs")
        parents = sorted({str(value["parent_logical_query_id"]) for value in queries})
        if logical_ids != parents:
            raise ValueError("logical query parent summary differs")
        if status == "COMPLETE" and (
            observed != (expected_start, expected_end)
            or item["row_count"] <= 0
            or item["game_count"] <= 0
            or item["game_count"] != len(game_pks)
            or missing
            or duplicates
            or not set(REQUIRED_FIELDS[source]).issubset(schema_fields)
            or len(retrieved) != len(queries)
            or len(content_hashes) != len(queries)
        ):
            raise ValueError("complete source state differs")
        if status == "COMPLETE":
            aggregate = _validate_v52_query_set(
                queries,
                source,
                year,
                f"season.{year}.{source}",
            )
            if observed != aggregate:
                raise ValueError("complete source observed/query range differs")
        if status == "INVALID" and not (missing or duplicates):
            raise ValueError("invalid source lacks blocker")


def _validate_query_evidence(
    raw: object,
    source: str,
    year: int | None,
    path: str,
    *,
    allow_legacy: bool = True,
) -> dict[str, object]:
    item = require_exact_keys(raw, QUERY_EVIDENCE_KEYS, path)
    if not allow_legacy and item["evidence_kind"] != "V52_MANIFEST_SIDECAR":
        raise ValueError(
            f"{path} optional query evidence must be V52_MANIFEST_SIDECAR"
        )
    if item["source"] != source or item["evidence_kind"] not in {
        "V52_MANIFEST_SIDECAR",
        "LEGACY_MANIFEST_ONLY",
    }:
        raise ValueError(f"{path} identity differs")
    _relative_path(item["manifest_path"], f"{path}.manifest_path")
    manifest_hash = _sha256(item["manifest_sha256"], f"{path}.manifest_sha256")
    logical = _strict_string(item["logical_query_id"], f"{path}.logical_query_id")
    query = require_exact_keys(item["query"], QUERY_KEYS, f"{path}.query")
    query_year = _strict_int(query["season"], f"{path}.query.season", 2018)
    if query_year > 2024 or (year is not None and query_year != year):
        raise ValueError(f"{path}.query.season differs")
    query_start = _iso_date(query["start_date"], f"{path}.query.start_date")
    query_end = _iso_date(query["end_date"], f"{path}.query.end_date")
    child = require_exact_keys(item["child_range"], RANGE_KEYS, f"{path}.child_range")
    child_start = _iso_date(child["start_date"], f"{path}.child_range.start_date")
    child_end = _iso_date(child["end_date"], f"{path}.child_range.end_date")
    if (
        query_start > query_end
        or child_start > child_end
        or child_start < query_start
        or child_end > query_end
    ):
        raise ValueError(f"{path} date ranges differ")
    if item["evidence_kind"] == "V52_MANIFEST_SIDECAR":
        parent = _sha256(
            item["parent_logical_query_id"], f"{path}.parent_logical_query_id"
        )
        if parent != parent_logical_query_id(
            source, query_year, query_start, query_end
        ):
            raise ValueError(f"{path} parent query hash differs")
        split_index = _strict_int(item["split_index"], f"{path}.split_index", 1)
        split_count = _strict_int(item["split_count"], f"{path}.split_count", 1)
        if (
            split_index > split_count
            or logical != child_logical_query_id(parent, split_index)
        ):
            raise ValueError(f"{path} split identity differs")
    else:
        if (
            item["parent_logical_query_id"] is not None
            or item["split_index"] is not None
            or item["split_count"] is not None
            or logical != f"legacy:{manifest_hash}:{query_year}:{source}"
        ):
            raise ValueError(f"{path} legacy identity differs")
    return json.loads(json.dumps(dict(item), allow_nan=False))


def _validate_v52_query_set(
    queries: Sequence[Mapping[str, object]],
    source: str,
    year: int,
    path: str,
) -> tuple[str, str]:
    expected_start, expected_end = f"{year}-01-01", f"{year}-12-31"
    if not queries:
        raise ValueError(f"{path} query coverage is empty")
    split_counts = {int(query["split_count"]) for query in queries}
    split_indices = [int(query["split_index"]) for query in queries]
    logical_ids = [str(query["logical_query_id"]) for query in queries]
    parents = {str(query["parent_logical_query_id"]) for query in queries}
    if (
        split_counts != {len(queries)}
        or split_indices != list(range(1, len(queries) + 1))
        or len(logical_ids) != len(set(logical_ids))
        or len(parents) != 1
    ):
        raise ValueError(f"{path} split identity/count/order differs")
    ranges: list[tuple[str, str]] = []
    for query in queries:
        query_contract = query["query"]
        if (
            query_contract["season"] != year
            or query_contract["start_date"] != expected_start
            or query_contract["end_date"] != expected_end
        ):
            raise ValueError(f"{path} query must cover the exact season")
        ranges.append(
            (
                str(query["child_range"]["start_date"]),
                str(query["child_range"]["end_date"]),
            )
        )
    if not ranges_cover(ranges, expected_start, expected_end):
        raise ValueError(f"{path} query coverage is not contiguous")
    return ranges[0][0], ranges[-1][1]


def _validate_optional_sources(raw: object) -> None:
    sources = require_exact_keys(
        raw, frozenset(OPTIONAL_SOURCE_CONTRACT), "optional_sources"
    )
    for source, absent in OPTIONAL_SOURCE_CONTRACT.items():
        item = require_exact_keys(
            sources[source], OPTIONAL_RECORD_KEYS, f"optional_sources.{source}"
        )
        status = item["status"]
        if status not in {absent, "PRESENT_VALID", "PRESENT_INVALID"}:
            raise ValueError(f"optional_sources.{source}.status differs")
        queries_raw = item["queries"]
        if not isinstance(queries_raw, list):
            raise ValueError("optional queries must be a list")
        queries = [
            _validate_query_evidence(
                value,
                source,
                None,
                f"optional_sources.{source}.queries[{index}]",
                allow_legacy=False,
            )
            for index, value in enumerate(queries_raw)
        ]
        if queries != sorted(
            queries,
            key=lambda value: (
                str(value["manifest_path"]),
                str(value["logical_query_id"]),
            ),
        ):
            raise ValueError("optional queries are not canonical")
        errors = item["errors"]
        if not isinstance(errors, list):
            raise ValueError("optional errors must be a list")
        for index, error in enumerate(errors):
            _validate_inventory_error(
                error, f"optional_sources.{source}.errors[{index}]"
            )
        if status == absent and (
            item["fallback_status"] != absent or queries or errors
        ):
            raise ValueError("absent optional state differs")
        if status == "PRESENT_VALID" and (
            item["fallback_status"] is not None or not queries or errors
        ):
            raise ValueError("valid optional state differs")
        if status == "PRESENT_VALID":
            grouped: dict[int, list[Mapping[str, object]]] = {}
            for query in queries:
                grouped.setdefault(int(query["query"]["season"]), []).append(query)
            for year, group in grouped.items():
                ordered = sorted(group, key=lambda value: int(value["split_index"]))
                try:
                    _validate_v52_query_set(
                        ordered,
                        source,
                        year,
                        f"optional_sources.{source}",
                    )
                except ValueError as exc:
                    raise ValueError("optional query coverage differs") from exc
        if status == "PRESENT_INVALID" and (
            item["fallback_status"] != absent or not errors
        ):
            raise ValueError("invalid optional state differs")


def _validate_protected_snapshot(raw: object, path: str) -> None:
    item = require_exact_keys(raw, PROTECTED_SNAPSHOT_KEYS, path)
    metadata = item["metadata_path_size_mtime_inode_mode"]
    hashes = item["small_sealed_sha256"]
    if not isinstance(metadata, Mapping) or not isinstance(hashes, Mapping):
        raise ValueError(f"{path} maps differ")
    for relative, values in metadata.items():
        _relative_path(relative, f"{path}.metadata path")
        if not isinstance(values, list) or len(values) != 4:
            raise ValueError(f"{path} metadata tuple differs")
        for index, value in enumerate(values):
            _strict_int(value, f"{path}.metadata[{index}]", 0)
    for relative, digest in hashes.items():
        _relative_path(relative, f"{path}.hash path")
        _sha256(digest, f"{path}.hash")


def _validate_inventory_error(raw: object, path: str) -> None:
    item = require_exact_keys(raw, INVENTORY_ERROR_KEYS, path)
    _relative_path(item["path"], f"{path}.path")
    if item["code"] not in INVENTORY_ERROR_CODES or not isinstance(
        item["detail"], str
    ):
        raise ValueError(f"{path} fields differ")


def _relative_path(value: object, field: str) -> str:
    text = _strict_string(value, field)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != text:
        raise ValueError(f"{field} must be a canonical relative path")
    return text


def _strict_sorted_strings(value: object, field: str) -> list[str]:
    values = _string_list(value, field)
    if any(not item for item in values) or values != sorted(set(values)):
        raise ValueError(f"{field} must be sorted unique non-empty strings")
    return values


def _strict_hash_list(value: object, field: str) -> list[str]:
    values = _string_list(value, field)
    for index, digest in enumerate(values):
        _sha256(digest, f"{field}[{index}]")
    if values != sorted(set(values)):
        raise ValueError(f"{field} must be sorted unique hashes")
    return values


def _strict_path_list(value: object, field: str) -> list[str]:
    values = _string_list(value, field)
    for index, path in enumerate(values):
        _relative_path(path, f"{field}[{index}]")
    if values != sorted(set(values)):
        raise ValueError(f"{field} must be sorted unique paths")
    return values


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
            f"{name}={record['sources'][name]['status']}"
            for name in MINIMUM_SOURCES
            if record["sources"][name]["status"] != "COMPLETE"
        )
        + " |"
        for year, record in sorted(report["seasons"].items())
    ]
    optional_rows = [
        f"| {source} | {report['optional_sources'][source]['status']} | "
        f"{report['optional_sources'][source]['fallback_status'] or 'none'} | "
        f"{len(report['optional_sources'][source]['queries'])} | "
        f"{len(report['optional_sources'][source]['errors'])} |"
        for source in OPTIONAL_SOURCE_CONTRACT
    ]
    inventory_errors = report["inventory_errors"]
    error_rows = (
        [
            f"| {_markdown_cell(error['path'])} | {_markdown_cell(error['code'])} | "
            f"{_markdown_cell(error['detail'])} |"
            for error in inventory_errors
        ]
        if inventory_errors
        else ["| none | none | none |"]
    )

    def step_rows(name: str) -> list[str]:
        return [
            f"| {_markdown_cell(step['stage'])} | {_markdown_cell(step['status'])} | "
            f"{_markdown_cell(step['prerequisite'])} | {_markdown_cell(step['command'])} |"
            for step in report[name]
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
            "## Required gamePk coverage contract",
            "",
            "Game-grain schedule, Statcast, and prior-schedule sources must expose the exact same nonempty gamePk set per season. Pitching and venue/timezone reference grains must expose a nonempty subset of that schedule set and may not contain extra gamePk values.",
            "",
            "Validation authority: the JSON Schema is a structural and locally semantic mirror. parse_preflight remains authoritative for real calendar validity, ordering and contiguity, cross-source relationships, and hashes derived from file content.",
            "",
            "## Inventory errors",
            "",
            "| Path | Code | Detail |",
            "|---|---|---|",
            *error_rows,
            "",
            "## Optional source evidence",
            "",
            "| Source | Status | Fallback | Queries | Errors |",
            "|---|---|---|---:|---:|",
            *optional_rows,
            "",
            "Optional evidence never blocks minimum-source READY; absent or invalid evidence uses the frozen neutral/null fallback.",
            "",
            "## Acquisition steps",
            "",
            "| Stage | Status | Prerequisite | Command |",
            "|---|---|---|---|",
            *step_rows("acquisition_steps"),
            "",
            "## Execution steps after READY",
            "",
            "| Stage | Status | Prerequisite | Command |",
            "|---|---|---|---|",
            *step_rows("execution_steps_after_ready"),
            "",
            "H22 fitting permitted: " + ("Yes" if report["may_fit_H22"] else "No"),
            "",
        ]
    )


def _markdown_cell(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def preflight_schema_document() -> dict[str, object]:
    hash_schema = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    date_schema = {
        "type": "string",
        "format": "date",
        "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$",
    }
    path_schema = {
        "type": "string",
        "minLength": 1,
        "pattern": r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*\\).+$",
    }
    optional_properties = {
        name: {
            "type": "object",
            "required": sorted(OPTIONAL_RECORD_KEYS),
            "properties": {
                "status": {"enum": [status, "PRESENT_VALID", "PRESENT_INVALID"]},
                "fallback_status": {"enum": [status, None]},
                "queries": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/optional_query_evidence"},
                    "uniqueItems": True,
                },
                "errors": {
                    "type": "array",
                    "items": {"$ref": "#/$defs/inventory_error"},
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "PRESENT_VALID"}}},
                    "then": {
                        "properties": {
                            "fallback_status": {"type": "null"},
                            "queries": {"minItems": 1, "uniqueItems": True},
                            "errors": {"maxItems": 0},
                        }
                    },
                }
            ],
            "additionalProperties": False,
        }
        for name, status in OPTIONAL_SOURCE_CONTRACT.items()
    }
    source_properties = {
        "source": {"enum": list(MINIMUM_SOURCES)},
        "required": {"const": True},
        "expected_start": date_schema,
        "expected_end": date_schema,
        "observed_start": {"anyOf": [date_schema, {"type": "null"}]},
        "observed_end": {"anyOf": [date_schema, {"type": "null"}]},
        "logical_query_ids": {
            "type": "array",
            "items": {"type": "string"},
            "uniqueItems": True,
        },
        "row_count": {"type": "integer", "minimum": 0},
        "game_count": {"type": "integer", "minimum": 0},
        "game_pks": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "uniqueItems": True,
        },
        "schema_fields": {"type": "array", "items": {"type": "string"}},
        "missing_critical_fields": {
            "type": "array",
            "items": {"type": "string"},
        },
        "duplicate_game_pks": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "retrieved_at_utc": {"type": "array", "items": {"type": "string"}},
        "content_sha256": {
            "type": "array",
            "items": hash_schema,
            "uniqueItems": True,
        },
        "manifest_sha256": {
            "type": "array",
            "items": hash_schema,
            "uniqueItems": True,
        },
        "split_children": {
            "type": "array",
            "items": path_schema,
            "uniqueItems": True,
        },
        "queries": {"type": "array", "items": {"$ref": "#/$defs/query_evidence"}},
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
        "generated_from_inventory_sha256": hash_schema,
        "protected_tree_before": {"$ref": "#/$defs/protected_snapshot"},
        "protected_tree_after": {"$ref": "#/$defs/protected_snapshot"},
        "inventory_errors": {
            "type": "array",
            "items": {"$ref": "#/$defs/inventory_error"},
        },
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
        "description": (
            "Structural and locally semantic mirror only. parse_preflight is the "
            "authoritative validator for real calendar dates, cross-array ordering "
            "and contiguity, cross-source coverage, and content-derived hashes."
        ),
        "type": "object",
        "required": sorted(PREFLIGHT_REQUIRED_KEYS),
        "properties": top_properties,
        "additionalProperties": False,
        "$defs": {
            "sha256": hash_schema,
            "season": {
                "type": "object",
                "required": sorted(SEASON_KEYS),
                "properties": season_properties,
                "additionalProperties": False,
            },
            "source_record": {
                "type": "object",
                "description": (
                    "COMPLETE positivity is mirrored locally; parse_preflight "
                    "authoritatively enforces ordering, coverage, and derived counts."
                ),
                "required": sorted(SOURCE_RECORD_KEYS),
                "properties": source_properties,
                "allOf": [
                    {
                        "if": {
                            "properties": {"status": {"const": "COMPLETE"}}
                        },
                        "then": {
                            "properties": {
                                "row_count": {"type": "integer", "minimum": 1},
                                "game_count": {"type": "integer", "minimum": 1},
                                "game_pks": {
                                    "type": "array",
                                    "minItems": 1,
                                    "uniqueItems": True,
                                },
                                "queries": {"minItems": 1, "uniqueItems": True},
                            }
                        },
                    }
                ],
                "additionalProperties": False,
            },
            "step": {
                "type": "object",
                "required": sorted(STEP_KEYS),
                "properties": step_properties,
                "additionalProperties": False,
            },
            "query": {
                "type": "object",
                "required": sorted(QUERY_KEYS),
                "properties": {
                    "season": {"type": "integer", "minimum": 2018, "maximum": 2024},
                    "start_date": date_schema,
                    "end_date": date_schema,
                },
                "additionalProperties": False,
            },
            "date_range": {
                "type": "object",
                "required": sorted(RANGE_KEYS),
                "properties": {"start_date": date_schema, "end_date": date_schema},
                "additionalProperties": False,
            },
            "query_evidence": {
                "type": "object",
                "required": sorted(QUERY_EVIDENCE_KEYS),
                "properties": {
                    "evidence_kind": {
                        "enum": ["V52_MANIFEST_SIDECAR", "LEGACY_MANIFEST_ONLY"]
                    },
                    "source": {
                        "enum": list(MINIMUM_SOURCES) + list(OPTIONAL_SOURCE_CONTRACT)
                    },
                    "manifest_path": path_schema,
                    "manifest_sha256": hash_schema,
                    "logical_query_id": {
                        "anyOf": [
                            {
                                "type": "string",
                                "pattern": "^[0-9a-f]{64}:[1-9][0-9]*$",
                            },
                            {
                                "type": "string",
                                "pattern": (
                                    "^legacy:[0-9a-f]{64}:[0-9]{4}:"
                                    "[a-z0-9_]+$"
                                ),
                            },
                        ]
                    },
                    "parent_logical_query_id": {
                        "anyOf": [hash_schema, {"type": "null"}]
                    },
                    "split_index": {
                        "anyOf": [
                            {"type": "integer", "minimum": 1},
                            {"type": "null"},
                        ]
                    },
                    "split_count": {
                        "anyOf": [
                            {"type": "integer", "minimum": 1},
                            {"type": "null"},
                        ]
                    },
                    "query": {"$ref": "#/$defs/query"},
                    "child_range": {"$ref": "#/$defs/date_range"},
                },
                "allOf": [
                    {
                        "if": {
                            "properties": {
                                "evidence_kind": {
                                    "const": "V52_MANIFEST_SIDECAR"
                                }
                            }
                        },
                        "then": {
                            "properties": {
                                "logical_query_id": {
                                    "type": "string",
                                    "pattern": "^[0-9a-f]{64}:[1-9][0-9]*$",
                                },
                                "parent_logical_query_id": {
                                    "$ref": "#/$defs/sha256"
                                },
                                "split_index": {
                                    "type": "integer",
                                    "minimum": 1,
                                },
                                "split_count": {
                                    "type": "integer",
                                    "minimum": 1,
                                },
                            }
                        },
                    },
                    {
                        "if": {
                            "properties": {
                                "evidence_kind": {"const": "LEGACY_MANIFEST_ONLY"}
                            }
                        },
                        "then": {
                            "properties": {
                                "logical_query_id": {
                                    "type": "string",
                                    "pattern": (
                                        "^legacy:[0-9a-f]{64}:[0-9]{4}:"
                                        "[a-z0-9_]+$"
                                    ),
                                },
                                "parent_logical_query_id": {"type": "null"},
                                "split_index": {"type": "null"},
                                "split_count": {"type": "null"},
                            }
                        },
                    },
                ],
                "additionalProperties": False,
            },
            "optional_query_evidence": {
                "type": "object",
                "required": sorted(QUERY_EVIDENCE_KEYS),
                "properties": {
                    "evidence_kind": {"const": "V52_MANIFEST_SIDECAR"},
                    "source": {"enum": list(OPTIONAL_SOURCE_CONTRACT)},
                    "manifest_path": path_schema,
                    "manifest_sha256": {"$ref": "#/$defs/sha256"},
                    "logical_query_id": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}:[1-9][0-9]*$",
                    },
                    "parent_logical_query_id": {"$ref": "#/$defs/sha256"},
                    "split_index": {"type": "integer", "minimum": 1},
                    "split_count": {"type": "integer", "minimum": 1},
                    "query": {"$ref": "#/$defs/query"},
                    "child_range": {"$ref": "#/$defs/date_range"},
                },
                "additionalProperties": False,
            },
            "inventory_error": {
                "type": "object",
                "required": sorted(INVENTORY_ERROR_KEYS),
                "properties": {
                    "path": path_schema,
                    "code": {"enum": sorted(INVENTORY_ERROR_CODES)},
                    "detail": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "protected_snapshot": {
                "type": "object",
                "required": sorted(PROTECTED_SNAPSHOT_KEYS),
                "properties": {
                    "metadata_path_size_mtime_inode_mode": {
                        "type": "object",
                        "propertyNames": path_schema,
                        "additionalProperties": {
                            "type": "array",
                            "minItems": 4,
                            "maxItems": 4,
                            "items": {"type": "integer", "minimum": 0},
                        },
                    },
                    "small_sealed_sha256": {
                        "type": "object",
                        "propertyNames": path_schema,
                        "additionalProperties": hash_schema,
                    },
                },
                "additionalProperties": False,
            },
        },
    }


def write_preflight(
    root: Path,
    run_path: Path,
    json_path: Path,
    markdown_path: Path,
    schema_path: Path,
) -> None:
    output_paths = (json_path, markdown_path, schema_path)
    _validate_preflight_output_paths(root, run_path, output_paths, {}, set())
    protected_before = protected_inventory(root)
    evidence: EvidenceSnapshot = {}
    declared_inputs: DeclaredInputs = {run_path.resolve(strict=False)}
    run_bytes = _verified_json_bytes(run_path.parent, run_path, evidence)
    report = _build_preflight(
        root,
        parse_run_spec(json.loads(run_bytes)),
        evidence,
        declared_inputs,
    )
    _validate_preflight_output_paths(
        root, run_path, output_paths, evidence, declared_inputs
    )
    _verify_concrete_evidence(evidence)
    entries = (
        (json_path, canonical_json_bytes(report) + b"\n"),
        (markdown_path, render_preflight_markdown(report).encode("utf-8")),
        (schema_path, canonical_json_bytes(preflight_schema_document()) + b"\n"),
    )
    if protected_inventory(root) != protected_before:
        raise RuntimeError("protected evidence changed before preflight publication")

    def verify_postconditions() -> None:
        if protected_inventory(root) != protected_before:
            raise RuntimeError("preflight publication modified protected evidence")
        _verify_concrete_evidence(evidence)

    _publish_bundle(entries, root=root, post_publish=verify_postconditions)


def _validate_preflight_output_paths(
    root: Path,
    run_path: Path,
    output_paths: Sequence[Path],
    evidence: Mapping[Path, EvidenceFingerprint],
    declared_inputs: set[Path],
) -> None:
    lexical_root = Path(os.path.abspath(root))
    protected = tuple(
        (root / name).resolve(strict=False) for name in PROTECTED_PREFLIGHT_ROOTS
    )
    input_evidence = {run_path.resolve(strict=False), *evidence, *declared_inputs}
    input_inodes = {(value[0], value[1]) for value in evidence.values()}
    resolved: list[Path] = []
    for path in output_paths:
        lexical_candidate = Path(os.path.abspath(path))
        try:
            lexical_candidate.relative_to(lexical_root)
        except ValueError as exc:
            raise ValueError(
                "preflight output path must be lexically under preflight root"
            ) from exc
        candidate = path.resolve(strict=False)
        if any(candidate == base or base in candidate.parents for base in protected):
            raise ValueError("preflight output path is inside a protected namespace")
        if candidate in input_evidence:
            raise ValueError("preflight output path aliases input evidence")
        if candidate.exists():
            file_stat = candidate.stat()
            if (file_stat.st_dev, file_stat.st_ino) in input_inodes:
                raise ValueError("preflight output inode aliases input evidence")
        resolved.append(candidate)
    if len(set(resolved)) != len(resolved):
        raise ValueError("preflight output paths must be unique")


def _verify_concrete_evidence(evidence: Mapping[Path, EvidenceFingerprint]) -> None:
    current: EvidenceSnapshot = {}
    for path in evidence:
        _verified_file_read(path.parent, path, collect=False, evidence=current)
    if dict(evidence) != current:
        raise RuntimeError("concrete input evidence changed during preflight action")


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
