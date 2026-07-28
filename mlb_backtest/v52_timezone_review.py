"""Offline review gate for effective-dated observed-venue timezones."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .secure_fs import (
    RegularFileSnapshot as _SafeSnapshot,
    TrustedRoot,
    open_parent as open_trusted_parent,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)
from .v52_evidence import V52EvidenceStore, logical_query_id
from .protocols import SourceRunSpec
from .v52_query_plan import (
    SourceAuthorityContext,
    _load_static_contracts,
    load_query_batch,
    load_query_batch_for_authority,
)
from .v52_source_contracts import QUERY_SIDECAR_KEYS, canonical_json_bytes


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_ROLES = frozenset(
    {"TIMEZONE_SEMANTIC_REVIEW", "TIMEZONE_COVERAGE_REVIEW"}
)
_DECISIONS = frozenset({"APPROVED", "REJECTED"})
_PRODUCTION_MAPPING = Path("config/v5_2_qd/venue_timezones.json")
_ACQUISITION_AUDIT = Path("audit/v5_2_qd/historical/acquisition")
_CANDIDATE_PATH = _ACQUISITION_AUDIT / "venue_timezone_candidate.json"
_COVERAGE_REVIEW_PATH = (
    _ACQUISITION_AUDIT / "timezone_reviews/coverage.json"
)
_SEMANTIC_REVIEW_PATH = (
    _ACQUISITION_AUDIT / "timezone_reviews/semantic.json"
)
_SEAL_PATH = _ACQUISITION_AUDIT / "venue_timezone_seal.json"
_SCHEDULE_INVENTORY_PATH = (
    _ACQUISITION_AUDIT / "schedule_anchor_inventory.json"
)
_RUN_SPEC_PATH = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_ACQUISITION_CONTRACT_PATH = Path(
    "protocols/v5_2_qd/historical_source_acquisition.json"
)
_SOURCE_RUN_SPEC_PATH = Path(
    "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
)
_SOURCE_ACQUISITION_CONTRACT_PATH = Path(
    "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
)
_RECOVERY_ACQUISITION_AUDIT = Path(
    "audit/v5_2_qd/historical_recovery_schedule_conflicts/acquisition"
)
_RECOVERY_RUN_SPEC_PATH = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts.json"
)
_RECOVERY_ACQUISITION_CONTRACT_PATH = Path(
    "protocols/v5_2_qd/"
    "historical_source_acquisition_recovery_schedule_conflicts.json"
)
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"
_VENUE_ENDPOINT_PREFIX = "https://statsapi.mlb.com/api/v1/venues/"
_MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
_MAX_MAPPING_BYTES = 8 * 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_MAX_AUTHORITY_BYTES = 64 * 1024 * 1024
@dataclass(frozen=True)
class VerifiedTimezoneGate:
    candidate: Mapping[str, object]
    mapping: Mapping[str, object]
    reviews: tuple[Mapping[str, object], ...]
    seal: Mapping[str, object]
    candidate_sha256: str
    mapping_sha256: str
    review_sha256s: tuple[str, ...]
    seal_sha256: str
    run_spec_sha256: str
    acquisition_contract_sha256: str
    schedule_inventory_sha256: str
    query_batch_ids: tuple[str, str]
    query_batch_sha256s: tuple[str, str]


@dataclass(frozen=True)
class _CurrentTimezoneAuthority:
    source_projection: Mapping[str, object]
    run_spec_sha256: str
    acquisition_contract_sha256: str
    schedule_inventory_sha256: str
    query_batch_ids: tuple[str, str]
    query_batch_sha256s: tuple[str, str]
    snapshots: tuple[_SafeSnapshot, ...]


@dataclass(frozen=True)
class _TimezonePaths:
    acquisition_audit: Path
    candidate: Path
    coverage_review: Path
    semantic_review: Path
    seal: Path
    schedule_inventory: Path
    run_spec: Path
    acquisition_contract: Path
    query_batch_root: Path


def _timezone_paths(
    context: SourceAuthorityContext | None,
) -> _TimezonePaths:
    if context is None:
        audit = _ACQUISITION_AUDIT
        run_spec = _RUN_SPEC_PATH
        contract = _ACQUISITION_CONTRACT_PATH
    else:
        audit = context.acquisition_audit_root.relative_to(
            context.repository_root
        )
        run_spec = context.authority_path.relative_to(context.repository_root)
        contract = context.acquisition_contract_path.relative_to(
            context.repository_root
        )
        if isinstance(context.authority, SourceRunSpec):
            expected_paths = (
                (
                    Path(
                        "audit/v5_2_qd/"
                        "historical_superseding_565534/acquisition"
                    ),
                    _SOURCE_RUN_SPEC_PATH,
                    _SOURCE_ACQUISITION_CONTRACT_PATH,
                ),
                (
                    _RECOVERY_ACQUISITION_AUDIT,
                    _RECOVERY_RUN_SPEC_PATH,
                    _RECOVERY_ACQUISITION_CONTRACT_PATH,
                ),
            )
        else:
            expected_paths = (
                (
                    _ACQUISITION_AUDIT,
                    _RUN_SPEC_PATH,
                    _ACQUISITION_CONTRACT_PATH,
                ),
            )
        expected = (audit, run_spec, contract)
        if expected not in expected_paths:
            raise ValueError("timezone authority paths differ")
    return _TimezonePaths(
        acquisition_audit=audit,
        candidate=audit / "venue_timezone_candidate.json",
        coverage_review=audit / "timezone_reviews/coverage.json",
        semantic_review=audit / "timezone_reviews/semantic.json",
        seal=audit / "venue_timezone_seal.json",
        schedule_inventory=audit / "schedule_anchor_inventory.json",
        run_spec=run_spec,
        acquisition_contract=contract,
        query_batch_root=audit / "query_batches",
    )


_OLD_TIMEZONE_PATHS = _timezone_paths(None)
_ACTIVE_TIMEZONE_PATHS: ContextVar[_TimezonePaths] = ContextVar(
    "v52_timezone_paths", default=_OLD_TIMEZONE_PATHS
)


def _recovery_authority_binding(
    run_spec_sha256: object, acquisition_contract_sha256: object
) -> dict[str, object]:
    return {
        "acquisition_contract": {
            "path": _RECOVERY_ACQUISITION_CONTRACT_PATH.as_posix(),
            "sha256": _sha256_text(
                acquisition_contract_sha256,
                "recovery acquisition contract SHA-256",
            ),
        },
        "run_spec": {
            "path": _RECOVERY_RUN_SPEC_PATH.as_posix(),
            "sha256": _sha256_text(
                run_spec_sha256, "recovery RunSpec SHA-256"
            ),
        },
    }


def _before_review_final_rebind() -> None:
    """Deterministic review publication race-test hook."""


def _after_mapping_publish_before_seal() -> None:
    """Deterministic mapping/seal interruption-test hook."""


def _before_seal_final_rebind() -> None:
    """Deterministic seal publication race-test hook."""


def _before_timezone_gate_final_rebind() -> None:
    """Deterministic verified-gate race-test hook."""


def _before_timezone_candidate_final_rebind() -> None:
    """Deterministic candidate publication race-test hook."""


def _canonical_file_bytes(value: object) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def _sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _sha256_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _positive(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be nonempty text")
    return value


def _day(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _coordinate(value: object, label: str, lower: float, upper: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not lower <= result <= upper:
        raise ValueError(f"{label} is outside its valid range")
    return result


def _json_object(contents: bytes, label: str, *, canonical: bool) -> dict[str, object]:
    try:
        value = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if canonical and contents != _canonical_file_bytes(value):
        raise ValueError(f"{label} must be canonical JSON with one newline")
    return value


def _sidecar_semantics(
    sidecar: object, *, source: str, grain: str, label: str
) -> Mapping[str, object]:
    if not isinstance(sidecar, Mapping) or set(sidecar) != QUERY_SIDECAR_KEYS:
        raise ValueError(f"{label} sidecar envelope differs")
    if (
        sidecar.get("schema_version") != 1
        or sidecar.get("source") != source
        or sidecar.get("grain") != grain
        or sidecar.get("parser_version") != "v5.2.1"
        or not isinstance(sidecar.get("query_id"), str)
        or not _SHA256.fullmatch(str(sidecar.get("query_id")))
        or not isinstance(sidecar.get("raw_sha256"), str)
        or not _SHA256.fullmatch(str(sidecar.get("raw_sha256")))
        or isinstance(sidecar.get("byte_count"), bool)
        or not isinstance(sidecar.get("byte_count"), int)
        or int(sidecar["byte_count"]) < 0
        or not isinstance(sidecar.get("semantics"), Mapping)
    ):
        raise ValueError(f"{label} sidecar identity differs")
    return sidecar["semantics"]


def _sidecar_hash(sidecar: Mapping[str, object]) -> str:
    return _sha256(_canonical_file_bytes(dict(sidecar)))


def _observed_from_schedules(
    schedule_sidecars: Sequence[Mapping[str, object]],
) -> tuple[dict[tuple[int, str], tuple[bool, bool]], list[str]]:
    if isinstance(schedule_sidecars, (str, bytes)) or not isinstance(
        schedule_sidecars, Sequence
    ) or not schedule_sidecars:
        raise ValueError("schedule sidecars must be a nonempty sequence")
    observed: dict[tuple[int, str], tuple[bool, bool]] = {}
    games: dict[int, tuple[int, str, bool, bool, bool]] = {}
    hashes: list[str] = []
    for sidecar in schedule_sidecars:
        semantics = _sidecar_semantics(
            sidecar,
            source="mlb_schedule_game_feed",
            grain="season_schedule_and_game",
            label="schedule",
        )
        anchors = semantics.get("anchors")
        if not isinstance(anchors, list) or not anchors:
            raise ValueError("schedule sidecar anchors must be nonempty")
        for anchor in anchors:
            if not isinstance(anchor, Mapping):
                raise ValueError("schedule anchor must be a mapping")
            game_pk = _positive(anchor.get("game_pk"), "schedule game_pk")
            venue_id = _positive(anchor.get("venue_id"), "schedule venue_id")
            official = _day(anchor.get("official_date"), "schedule official_date")
            neutral = anchor.get("neutral_site")
            alternate = anchor.get("alternate_site")
            played = anchor.get("played")
            if type(neutral) is not bool or type(alternate) is not bool:
                raise ValueError("schedule neutral/alternate flags must be boolean")
            if type(played) is not bool:
                raise ValueError("schedule played must be boolean")
            game_record = (
                venue_id,
                official.isoformat(),
                neutral,
                alternate,
                played,
            )
            if game_pk in games:
                if games[game_pk] != game_record:
                    raise ValueError("conflicting schedule game observation")
                raise ValueError("duplicate schedule game observation")
            games[game_pk] = game_record
            key = (venue_id, official.isoformat())
            disposition = (neutral, alternate)
            if key in observed and observed[key] != disposition:
                raise ValueError("conflicting observed venue/date disposition")
            observed[key] = disposition
        hashes.append(_sidecar_hash(sidecar))
    if len(hashes) != len(set(hashes)):
        raise ValueError("schedule sidecar population contains duplicates")
    return observed, sorted(hashes)


def _venue_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "city",
        "latitude",
        "longitude",
        "venue_id",
        "venue_name",
    }:
        raise ValueError("venue identity keys differ")
    return {
        "city": _text(value.get("city"), "venue city"),
        "latitude": _coordinate(
            value.get("latitude"), "venue latitude", -90.0, 90.0
        ),
        "longitude": _coordinate(
            value.get("longitude"), "venue longitude", -180.0, 180.0
        ),
        "venue_id": _positive(value.get("venue_id"), "venue id"),
        "venue_name": _text(value.get("venue_name"), "venue name"),
    }


def _venues_from_sidecars(
    venue_sidecars: Sequence[Mapping[str, object]],
) -> tuple[dict[int, dict[str, object]], list[str]]:
    if isinstance(venue_sidecars, (str, bytes)) or not isinstance(
        venue_sidecars, Sequence
    ) or not venue_sidecars:
        raise ValueError("venue sidecars must be a nonempty sequence")
    venues: dict[int, dict[str, object]] = {}
    hashes: list[str] = []
    for sidecar in venue_sidecars:
        semantics = _sidecar_semantics(
            sidecar,
            source="venue_team_timezone_table",
            grain="effective_dated_venue_team_reference",
            label="venue",
        )
        values = semantics.get("venues")
        if not isinstance(values, list) or not values:
            raise ValueError("venue sidecar venues must be nonempty")
        for value in values:
            record = _venue_record(value)
            venue_id = int(record["venue_id"])
            if venue_id in venues and venues[venue_id] != record:
                raise ValueError("conflicting venue identity")
            if venue_id in venues:
                raise ValueError("duplicate venue identity")
            venues[venue_id] = record
        hashes.append(_sidecar_hash(sidecar))
    if len(hashes) != len(set(hashes)):
        raise ValueError("venue sidecar population contains duplicates")
    return venues, sorted(hashes)


def _project_timezone_candidate(
    schedule_sidecars: Sequence[Mapping[str, object]],
    venue_sidecars: Sequence[Mapping[str, object]],
    *,
    query_batch_ids: Sequence[str],
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> Mapping[str, object]:
    if (
        isinstance(query_batch_ids, (str, bytes))
        or not isinstance(query_batch_ids, Sequence)
        or not query_batch_ids
        or list(query_batch_ids) != sorted(set(query_batch_ids))
    ):
        raise ValueError("candidate query_batch_ids must be nonempty sorted unique")
    batch_ids = [
        _sha256_text(value, "candidate query batch ID")
        for value in query_batch_ids
    ]
    run_hash = _sha256_text(run_spec_sha256, "candidate run_spec_sha256")
    contract_hash = _sha256_text(
        acquisition_contract_sha256,
        "candidate acquisition_contract_sha256",
    )
    observed, schedule_hashes = _observed_from_schedules(schedule_sidecars)
    venues, venue_hashes = _venues_from_sidecars(venue_sidecars)
    return _project_timezone_candidate_components(
        observed,
        schedule_hashes,
        venues,
        venue_hashes,
        query_batch_ids=batch_ids,
        run_spec_sha256=run_hash,
        acquisition_contract_sha256=contract_hash,
    )


def _project_timezone_candidate_components(
    observed: Mapping[tuple[int, str], tuple[bool, bool]],
    schedule_hashes: Sequence[str],
    venues: Mapping[int, Mapping[str, object]],
    venue_hashes: Sequence[str],
    *,
    query_batch_ids: Sequence[str],
    run_spec_sha256: str,
    acquisition_contract_sha256: str,
) -> Mapping[str, object]:
    batch_ids = [
        _sha256_text(value, "candidate query batch ID")
        for value in query_batch_ids
    ]
    schedule_hash_values = [
        _sha256_text(value, "candidate schedule sidecar SHA-256")
        for value in schedule_hashes
    ]
    venue_hash_values = [
        _sha256_text(value, "candidate venue sidecar SHA-256")
        for value in venue_hashes
    ]
    if (
        not observed
        or batch_ids != sorted(set(batch_ids))
        or schedule_hash_values != sorted(set(schedule_hash_values))
        or venue_hash_values != sorted(set(venue_hash_values))
    ):
        raise ValueError("candidate component authority differs")
    run_hash = _sha256_text(run_spec_sha256, "candidate run_spec_sha256")
    contract_hash = _sha256_text(
        acquisition_contract_sha256,
        "candidate acquisition_contract_sha256",
    )
    observed_scope = {venue_id for venue_id, _ in observed}
    missing = observed_scope - set(venues)
    extra = set(venues) - observed_scope
    if missing:
        raise ValueError(f"missing official venue identity for venue {min(missing)}")
    if extra:
        raise ValueError(f"extra official venue identity for venue {min(extra)}")
    dates_by_venue: dict[int, list[str]] = {}
    observed_rows = []
    for (venue_id, official), (neutral, alternate) in sorted(observed.items()):
        dates_by_venue.setdefault(venue_id, []).append(official)
        observed_rows.append(
            {
                "alternate_site": alternate,
                "neutral_site": neutral,
                "official_date": official,
                "venue_id": venue_id,
            }
        )
    timezone_rows = [
        {
            "effective_end": max(dates_by_venue[venue_id]),
            "effective_start": min(dates_by_venue[venue_id]),
            "review_status": "UNREVIEWED",
            "time_zone": None,
            "venue_id": venue_id,
        }
        for venue_id in sorted(observed_scope)
    ]
    return {
        "artifact_kind": "VENUE_TIMEZONE_CANDIDATE",
        "acquisition_contract_sha256": contract_hash,
        "observed_venue_dates": observed_rows,
        "query_batch_ids": batch_ids,
        "run_spec_sha256": run_hash,
        "schedule_sidecar_sha256s": schedule_hash_values,
        "schema_version": 1,
        "timezone_rows": timezone_rows,
        "venue_scope": sorted(observed_scope),
        "venue_sidecar_sha256s": venue_hash_values,
        "venues": [venues[venue_id] for venue_id in sorted(venues)],
    }


def _schedule_inventory_components(
    payload: Mapping[str, object],
) -> tuple[dict[tuple[int, str], tuple[bool, bool]], list[str]]:
    anchors = payload.get("anchors")
    season_sources = payload.get("season_sources")
    if (
        not isinstance(anchors, Sequence)
        or isinstance(anchors, (str, bytes))
        or not anchors
        or not isinstance(season_sources, Sequence)
        or isinstance(season_sources, (str, bytes))
        or not season_sources
    ):
        raise ValueError("timezone source schedule inventory differs")
    observed: dict[tuple[int, str], tuple[bool, bool]] = {}
    games: set[int] = set()
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            raise ValueError("timezone source schedule anchor differs")
        game_pk = _positive(anchor.get("game_pk"), "timezone source game_pk")
        if game_pk in games:
            raise ValueError("timezone source schedule game is duplicated")
        games.add(game_pk)
        venue_id = _positive(anchor.get("venue_id"), "timezone source venue ID")
        official = _day(
            anchor.get("official_date"), "timezone source official date"
        ).isoformat()
        neutral = anchor.get("neutral_site")
        alternate = anchor.get("alternate_site")
        if type(neutral) is not bool or type(alternate) is not bool:
            raise ValueError("timezone source observed venue flags differ")
        key = (venue_id, official)
        disposition = (neutral, alternate)
        if key in observed and observed[key] != disposition:
            raise ValueError("timezone source venue disposition conflicts")
        observed[key] = disposition
    sidecar_hashes = []
    for source in season_sources:
        if not isinstance(source, Mapping):
            raise ValueError("timezone source season binding differs")
        query_sidecar = source.get("query_sidecar")
        if not isinstance(query_sidecar, Mapping):
            raise ValueError("timezone source sidecar binding differs")
        sidecar_hashes.append(
            _sha256_text(
                query_sidecar.get("sha256"),
                "timezone source schedule sidecar SHA-256",
            )
        )
    if len(sidecar_hashes) != len(set(sidecar_hashes)):
        raise ValueError("timezone source sidecar population differs")
    return observed, sorted(sidecar_hashes)


def _normalize_timezone_rows(
    rows: object, *, allow_null: bool
) -> dict[int, list[dict[str, object]]]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("timezone_rows must be a nonempty list")
    result: dict[int, list[dict[str, object]]] = {}
    for raw in rows:
        expected = {"effective_end", "effective_start", "time_zone", "venue_id"}
        if allow_null:
            expected.add("review_status")
        if not isinstance(raw, Mapping) or set(raw) != expected:
            raise ValueError("timezone row keys differ")
        venue_id = _positive(raw.get("venue_id"), "timezone venue_id")
        first = _day(raw.get("effective_start"), "timezone effective start")
        last = _day(raw.get("effective_end"), "timezone effective end")
        if first > last:
            raise ValueError("timezone effective range is reversed")
        name = raw.get("time_zone")
        if name is None and allow_null:
            normalized_name = None
        else:
            normalized_name = _text(name, "IANA time zone")
            try:
                ZoneInfo(normalized_name)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    "timezone row must use a valid IANA ZoneInfo name"
                ) from exc
        record = {
            "effective_end": last.isoformat(),
            "effective_start": first.isoformat(),
            "time_zone": normalized_name,
            "venue_id": venue_id,
        }
        if allow_null:
            if raw.get("review_status") != "UNREVIEWED":
                raise ValueError("candidate review_status must remain UNREVIEWED")
            record["review_status"] = "UNREVIEWED"
        result.setdefault(venue_id, []).append(record)
    for venue_id, records in result.items():
        records.sort(key=lambda row: (row["effective_start"], row["effective_end"]))
        for previous, current in zip(records, records[1:]):
            if _day(current["effective_start"], "timezone start") <= _day(
                previous["effective_end"], "timezone end"
            ):
                raise ValueError(
                    f"timezone effective ranges overlap for venue {venue_id}"
                )
    return result


def _coverage(
    rows: dict[int, list[dict[str, object]]],
    observed: Mapping[tuple[int, str], tuple[bool, bool]],
) -> None:
    observed_scope = {venue_id for venue_id, _ in observed}
    extra = set(rows) - observed_scope
    if extra:
        raise ValueError(f"timezone mapping contains extra venue {min(extra)}")
    for venue_id, official_text in observed:
        official = date.fromisoformat(official_text)
        matches = [
            row
            for row in rows.get(venue_id, [])
            if _day(row["effective_start"], "timezone start")
            <= official
            <= _day(row["effective_end"], "timezone end")
        ]
        if len(matches) != 1:
            raise ValueError(
                "every observed venue/date must resolve to exactly one timezone mapping"
            )


def _mapping_object(mapping: object) -> dict[str, object]:
    expected_keys = {
        "artifact_kind",
        "schema_version",
        "timezone_rows",
    }
    if not isinstance(mapping, Mapping) or set(mapping) not in (
        expected_keys,
        expected_keys | {"authority"},
    ):
        raise ValueError("timezone mapping keys differ")
    if (
        mapping.get("artifact_kind") != "VENUE_TIMEZONE_MAPPING"
        or mapping.get("schema_version") != 1
    ):
        raise ValueError("timezone mapping identity differs")
    authority = mapping.get("authority")
    if authority is not None:
        if not isinstance(authority, Mapping):
            raise ValueError("timezone mapping recovery authority differs")
        run_binding = authority.get("run_spec")
        contract_binding = authority.get("acquisition_contract")
        if (
            not isinstance(run_binding, Mapping)
            or not isinstance(contract_binding, Mapping)
            or authority
            != _recovery_authority_binding(
                run_binding.get("sha256"), contract_binding.get("sha256")
            )
        ):
            raise ValueError("timezone mapping recovery authority differs")
    return dict(mapping)


def validate_timezone_mapping(
    mapping: Mapping[str, object],
    schedule_sidecars: Sequence[Mapping[str, object]],
) -> None:
    value = _mapping_object(mapping)
    observed, _ = _observed_from_schedules(schedule_sidecars)
    rows = _normalize_timezone_rows(value["timezone_rows"], allow_null=False)
    _coverage(rows, observed)


def _candidate_object(value: object) -> dict[str, object]:
    expected_keys = {
        "acquisition_contract_sha256",
        "artifact_kind",
        "observed_venue_dates",
        "query_batch_ids",
        "run_spec_sha256",
        "schedule_sidecar_sha256s",
        "schema_version",
        "timezone_rows",
        "venue_scope",
        "venue_sidecar_sha256s",
        "venues",
    }
    if not isinstance(value, Mapping) or set(value) not in (
        expected_keys,
        expected_keys | {"authority"},
    ):
        raise ValueError("timezone candidate keys differ")
    if (
        value.get("artifact_kind") != "VENUE_TIMEZONE_CANDIDATE"
        or value.get("schema_version") != 1
    ):
        raise ValueError("timezone candidate identity differs")
    batch_ids = value.get("query_batch_ids")
    if (
        not isinstance(batch_ids, list)
        or not batch_ids
        or batch_ids != sorted(set(batch_ids))
        or any(
            not isinstance(item, str) or not _SHA256.fullmatch(item)
            for item in batch_ids
        )
    ):
        raise ValueError("timezone candidate query_batch_ids differ")
    _sha256_text(value.get("run_spec_sha256"), "candidate run_spec_sha256")
    _sha256_text(
        value.get("acquisition_contract_sha256"),
        "candidate acquisition_contract_sha256",
    )
    authority = value.get("authority")
    if authority is not None and authority != _recovery_authority_binding(
        value["run_spec_sha256"], value["acquisition_contract_sha256"]
    ):
        raise ValueError("timezone candidate recovery authority differs")
    scope = value.get("venue_scope")
    if not isinstance(scope, list) or scope != sorted(set(scope)) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in scope
    ):
        raise ValueError("timezone candidate venue scope differs")
    for key in ("schedule_sidecar_sha256s", "venue_sidecar_sha256s"):
        hashes = value.get(key)
        if (
            not isinstance(hashes, list)
            or not hashes
            or hashes != sorted(set(hashes))
            or any(not isinstance(item, str) or not _SHA256.fullmatch(item) for item in hashes)
        ):
            raise ValueError(f"timezone candidate {key} differs")
    venues = value.get("venues")
    if not isinstance(venues, list):
        raise ValueError("timezone candidate venue identities differ from scope")
    normalized_venues = [_venue_record(row) for row in venues]
    if [row["venue_id"] for row in normalized_venues] != scope:
        raise ValueError("timezone candidate venue identities differ from scope")
    observed_values = value.get("observed_venue_dates")
    if not isinstance(observed_values, list) or not observed_values:
        raise ValueError("timezone candidate observed venue dates are missing")
    observed: dict[tuple[int, str], tuple[bool, bool]] = {}
    normalized_observed = []
    for raw in observed_values:
        if not isinstance(raw, Mapping) or set(raw) != {
            "alternate_site",
            "neutral_site",
            "official_date",
            "venue_id",
        }:
            raise ValueError("timezone candidate observation keys differ")
        venue_id = _positive(raw.get("venue_id"), "observed venue_id")
        official = _day(raw.get("official_date"), "observed official_date").isoformat()
        neutral = raw.get("neutral_site")
        alternate = raw.get("alternate_site")
        if type(neutral) is not bool or type(alternate) is not bool:
            raise ValueError("observed venue flags must be boolean")
        key = (venue_id, official)
        disposition = (neutral, alternate)
        if key in observed and observed[key] != disposition:
            raise ValueError("conflicting candidate venue observation")
        if key in observed:
            raise ValueError("duplicate candidate venue observation")
        observed[key] = disposition
        normalized_observed.append(dict(raw))
    if normalized_observed != sorted(
        normalized_observed, key=lambda row: (row["venue_id"], row["official_date"])
    ) or {venue_id for venue_id, _ in observed} != set(scope):
        raise ValueError("timezone candidate observation scope/order differs")
    rows = _normalize_timezone_rows(value.get("timezone_rows"), allow_null=True)
    if set(rows) != set(scope):
        raise ValueError("timezone candidate rows differ from venue scope")
    return dict(value)


def _mapping_from_candidate(
    candidate: Mapping[str, object], *, require_resolved: bool
) -> dict[str, object]:
    value = _candidate_object(candidate)
    rows = _normalize_timezone_rows(value["timezone_rows"], allow_null=True)
    projected = []
    for venue_id in sorted(rows):
        for row in rows[venue_id]:
            if row["time_zone"] is None:
                if require_resolved:
                    raise ValueError("timezone candidate contains unresolved time_zone rows")
                continue
            projected.append(
                {
                    "effective_end": row["effective_end"],
                    "effective_start": row["effective_start"],
                    "time_zone": row["time_zone"],
                    "venue_id": venue_id,
                }
            )
    if require_resolved:
        observed = {
            (int(row["venue_id"]), str(row["official_date"])): (
                bool(row["neutral_site"]),
                bool(row["alternate_site"]),
            )
            for row in value["observed_venue_dates"]
        }
        normalized = _normalize_timezone_rows(projected, allow_null=False)
        _coverage(normalized, observed)
    mapping = {
        "artifact_kind": "VENUE_TIMEZONE_MAPPING",
        "schema_version": 1,
        "timezone_rows": projected,
    }
    if "authority" in value:
        mapping["authority"] = value["authority"]
    return mapping


def _relative_from_trusted(root: TrustedRoot, path: Path, label: str) -> Path:
    root_path = root.path
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root_path)
        except ValueError as exc:
            raise ValueError(f"{label} path must stay under root") from exc
    if (
        not candidate.parts
        or any(part in {"", ".", ".."} or "\\" in part for part in candidate.parts)
    ):
        raise ValueError(f"{label} path must stay under root")
    return candidate


def _acquisition_audit_relative(
    root: TrustedRoot, path: Path, label: str
) -> Path:
    relative = _relative_from_trusted(root, path, label)
    prefix = _ACTIVE_TIMEZONE_PATHS.get().acquisition_audit.parts
    if relative.parts[: len(prefix)] != prefix or len(relative.parts) <= len(prefix):
        raise ValueError(
            f"{label} must stay in the acquisition audit namespace"
        )
    return relative


def _read_safe_snapshot_from_trusted(
    root: TrustedRoot,
    path: Path,
    label: str,
    *,
    missing_ok: bool = False,
    max_bytes: int | None = None,
) -> _SafeSnapshot | None:
    relative = _relative_from_trusted(root, path, label)
    return read_regular_snapshot(
        root, relative, label, missing_ok=missing_ok, max_bytes=max_bytes
    )


def _read_safe_from_trusted(
    root: TrustedRoot, path: Path, label: str
) -> tuple[Path, bytes]:
    snapshot = _read_safe_snapshot_from_trusted(root, path, label)
    if snapshot is None:
        raise ValueError(f"{label} is missing")
    return snapshot.relative, snapshot.contents


def _rebind_safe_snapshot_from_trusted(
    root: TrustedRoot,
    expected: _SafeSnapshot,
    label: str,
    *,
    max_bytes: int | None = None,
) -> None:
    try:
        rebind_regular_snapshot(root, expected, label, max_bytes=max_bytes)
    except ValueError as exc:
        raise ValueError(f"{label} identity or bytes changed") from exc


def _write_safe_from_trusted(
    root: TrustedRoot,
    path: Path,
    contents: bytes,
    label: str,
    *,
    final_rebind: Callable[[], None] | None = None,
    after_parent_fsync: Callable[[], None] | None = None,
) -> tuple[Path, _SafeSnapshot]:
    relative = _relative_from_trusted(root, path, label)
    def rebind() -> None:
        if final_rebind is not None:
            final_rebind()
        if after_parent_fsync is not None:
            after_parent_fsync()
        if final_rebind is not None:
            final_rebind()

    snapshot = write_exclusive(
        root, relative, contents, label, final_rebind=rebind
    )
    root.assert_bound()
    return root.path / relative, snapshot


def _reconcile_or_write_from_trusted(
    root: TrustedRoot,
    path: Path,
    contents: bytes,
    label: str,
    *,
    final_rebind: Callable[[], None] | None = None,
    after_parent_fsync: Callable[[], None] | None = None,
) -> tuple[Path, _SafeSnapshot]:
    existing = _read_safe_snapshot_from_trusted(
        root, path, label, missing_ok=True
    )
    if existing is not None:
        if existing.contents != contents:
            raise RuntimeError(f"immutable {label} differs")
        if final_rebind is not None:
            final_rebind()
        if after_parent_fsync is not None:
            after_parent_fsync()
        _rebind_safe_snapshot_from_trusted(root, existing, label)
        if final_rebind is not None:
            final_rebind()
        root.assert_bound()
        return root.path / existing.relative, existing
    published, snapshot = _write_safe_from_trusted(
        root,
        path,
        contents,
        label,
        final_rebind=final_rebind,
        after_parent_fsync=after_parent_fsync,
    )
    if snapshot.contents != contents:
        raise AssertionError(f"{label} created snapshot contents differ")
    _rebind_safe_snapshot_from_trusted(root, snapshot, label)
    return published, snapshot


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return the historical pure regular-file identity projection."""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _relative(root: Path, path: Path, label: str) -> Path:
    """Legacy Path adapter; timezone public operations use trusted helpers."""
    with open_trusted_root(root) as trusted:
        return _relative_from_trusted(trusted, path, label)


def _open_parent(
    root: Path, relative: Path, *, create: bool
) -> tuple[int, list[int]]:
    """Legacy Path adapter returning one caller-owned parent descriptor."""
    with open_trusted_root(root) as trusted:
        with open_trusted_parent(trusted, relative, create=create) as parent:
            duplicate = os.dup(parent)
    return duplicate, [duplicate]


def _read_safe_snapshot(
    root: Path,
    path: Path,
    label: str,
    *,
    missing_ok: bool = False,
    max_bytes: int | None = None,
) -> _SafeSnapshot | None:
    with open_trusted_root(root) as trusted:
        return _read_safe_snapshot_from_trusted(
            trusted,
            path,
            label,
            missing_ok=missing_ok,
            max_bytes=max_bytes,
        )


def _rebind_safe_snapshot(
    root: Path,
    expected: _SafeSnapshot,
    label: str,
    *,
    max_bytes: int | None = None,
) -> None:
    with open_trusted_root(root) as trusted:
        _rebind_safe_snapshot_from_trusted(
            trusted, expected, label, max_bytes=max_bytes
        )


def _write_safe(
    root: Path,
    path: Path,
    contents: bytes,
    label: str,
    *,
    final_rebind: Callable[[], None] | None = None,
    after_parent_fsync: Callable[[], None] | None = None,
) -> tuple[Path, _SafeSnapshot]:
    with open_trusted_root(root) as trusted:
        return _write_safe_from_trusted(
            trusted,
            path,
            contents,
            label,
            final_rebind=final_rebind,
            after_parent_fsync=after_parent_fsync,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _explicit_authority_context(
    root: Path,
    run_spec_path: Path | None,
    acquisition_contract_path: Path | None,
) -> SourceAuthorityContext | None:
    if run_spec_path is None and acquisition_contract_path is None:
        return None
    if run_spec_path is None or acquisition_contract_path is None:
        raise ValueError("run spec and acquisition contract must be supplied together")
    context = _load_static_contracts(run_spec_path, acquisition_contract_path)
    canonical_root = Path(os.path.abspath(os.fspath(root)))
    if context.repository_root != canonical_root:
        raise ValueError("timezone authority repository root differs")
    return context


@contextmanager
def _authority_scope(context: SourceAuthorityContext | None):
    token = _ACTIVE_TIMEZONE_PATHS.set(_timezone_paths(context))
    try:
        yield
    finally:
        _ACTIVE_TIMEZONE_PATHS.reset(token)


def write_timezone_review_receipt(
    root: Path,
    *,
    candidate_path: Path,
    output: Path,
    reviewer_identity: str,
    reviewer_role: str,
    review_scope: Sequence[int] | None = None,
    decision: str,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> Path:
    context = _explicit_authority_context(
        root, run_spec_path, acquisition_contract_path
    )
    with _authority_scope(context), open_trusted_root(root) as trusted:
        result = _write_timezone_review_receipt(
            trusted,
            candidate_path=candidate_path,
            output=output,
            reviewer_identity=reviewer_identity,
            reviewer_role=reviewer_role,
            review_scope=review_scope,
            decision=decision,
        )
        trusted.assert_bound()
        return result


def _write_timezone_review_receipt(
    root: TrustedRoot,
    *,
    candidate_path: Path,
    output: Path,
    reviewer_identity: str,
    reviewer_role: str,
    review_scope: Sequence[int] | None = None,
    decision: str,
) -> Path:
    if reviewer_role not in _ROLES:
        raise ValueError("timezone reviewer role differs")
    identity = _text(reviewer_identity, "reviewer identity")
    if decision not in _DECISIONS:
        raise ValueError("timezone review decision differs")
    candidate_relative = _acquisition_audit_relative(
        root, candidate_path, "timezone candidate"
    )
    output_relative = _acquisition_audit_relative(
        root, output, "timezone review receipt"
    )
    candidate_snapshot = _read_safe_snapshot_from_trusted(
        root, candidate_relative, "timezone candidate"
    )
    if candidate_snapshot is None:
        raise ValueError("timezone candidate is missing")
    relative = candidate_snapshot.relative
    contents = candidate_snapshot.contents
    candidate = _candidate_object(_json_object(contents, "timezone candidate", canonical=True))
    scope = candidate["venue_scope"]
    if review_scope is not None and list(review_scope) != scope:
        raise ValueError("timezone review scope must equal the exact candidate scope")
    if decision == "APPROVED":
        _mapping_from_candidate(candidate, require_resolved=True)
    receipt = {
        "candidate": {"path": relative.as_posix(), "sha256": _sha256(contents)},
        "decision": decision,
        "occurred_at_utc": _utc_now(),
        "receipt_kind": "TIMEZONE_REVIEW",
        "reviewed_venue_ids": scope,
        "reviewer_identity": identity,
        "reviewer_role": reviewer_role,
        "schema_version": 1,
    }
    if "authority" in candidate:
        receipt["authority"] = candidate["authority"]
    path, output_snapshot = _write_safe_from_trusted(
        root,
        output_relative,
        _canonical_file_bytes(receipt),
        "timezone review receipt",
        final_rebind=lambda: _rebind_safe_snapshot_from_trusted(
            root, candidate_snapshot, "timezone candidate"
        ),
    )
    _before_review_final_rebind()
    _rebind_safe_snapshot_from_trusted(
        root, candidate_snapshot, "timezone candidate"
    )
    _rebind_safe_snapshot_from_trusted(
        root, output_snapshot, "timezone review receipt"
    )
    root.assert_bound()
    return path


def _review_object(contents: bytes) -> dict[str, object]:
    value = _json_object(contents, "timezone review receipt", canonical=True)
    expected_keys = {
        "candidate",
        "decision",
        "occurred_at_utc",
        "receipt_kind",
        "reviewed_venue_ids",
        "reviewer_identity",
        "reviewer_role",
        "schema_version",
    }
    if set(value) not in (expected_keys, expected_keys | {"authority"}):
        raise ValueError("timezone review receipt keys differ")
    candidate = value.get("candidate")
    if (
        value.get("schema_version") != 1
        or value.get("receipt_kind") != "TIMEZONE_REVIEW"
        or value.get("reviewer_role") not in _ROLES
        or value.get("decision") not in _DECISIONS
        or not isinstance(value.get("reviewer_identity"), str)
        or not value["reviewer_identity"]
        or not isinstance(value.get("occurred_at_utc"), str)
        or not _UTC.fullmatch(str(value["occurred_at_utc"]))
        or not isinstance(candidate, Mapping)
        or set(candidate) != {"path", "sha256"}
        or not isinstance(candidate.get("path"), str)
        or not isinstance(candidate.get("sha256"), str)
        or not _SHA256.fullmatch(str(candidate.get("sha256")))
    ):
        raise ValueError("timezone review receipt identity differs")
    authority = value.get("authority")
    if authority is not None:
        if not isinstance(authority, Mapping):
            raise ValueError("timezone review receipt recovery authority differs")
        run_binding = authority.get("run_spec")
        contract_binding = authority.get("acquisition_contract")
        if (
            not isinstance(run_binding, Mapping)
            or not isinstance(contract_binding, Mapping)
            or authority
            != _recovery_authority_binding(
                run_binding.get("sha256"), contract_binding.get("sha256")
            )
        ):
            raise ValueError("timezone review receipt recovery authority differs")
    scope = value.get("reviewed_venue_ids")
    if not isinstance(scope, list) or scope != sorted(set(scope)) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in scope
    ):
        raise ValueError("timezone review receipt scope differs")
    return value


def seal_timezone_mapping(
    root: Path,
    *,
    candidate_path: Path,
    review_receipt_paths: Sequence[Path],
    output: Path,
    receipt_output: Path,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> tuple[Path, Path]:
    context = _explicit_authority_context(
        root, run_spec_path, acquisition_contract_path
    )
    with _authority_scope(context), open_trusted_root(root) as trusted:
        result = _seal_timezone_mapping(
            trusted,
            candidate_path=candidate_path,
            review_receipt_paths=review_receipt_paths,
            output=output,
            receipt_output=receipt_output,
        )
        trusted.assert_bound()
        return result


def _seal_timezone_mapping(
    root: TrustedRoot,
    *,
    candidate_path: Path,
    review_receipt_paths: Sequence[Path],
    output: Path,
    receipt_output: Path,
) -> tuple[Path, Path]:
    output_relative = _relative_from_trusted(
        root, output, "timezone mapping output"
    )
    if output_relative != _PRODUCTION_MAPPING:
        raise ValueError("timezone mapping output must use the production logical path")
    candidate_path = _acquisition_audit_relative(
        root, candidate_path, "timezone candidate"
    )
    receipt_output = _acquisition_audit_relative(
        root, receipt_output, "timezone mapping seal receipt"
    )
    candidate_snapshot = _read_safe_snapshot_from_trusted(
        root, candidate_path, "timezone candidate"
    )
    if candidate_snapshot is None:
        raise ValueError("timezone candidate is missing")
    candidate_relative = candidate_snapshot.relative
    candidate_contents = candidate_snapshot.contents
    candidate = _candidate_object(
        _json_object(candidate_contents, "timezone candidate", canonical=True)
    )
    mapping = _mapping_from_candidate(candidate, require_resolved=True)
    scope = candidate["venue_scope"]
    if (
        isinstance(review_receipt_paths, (str, bytes))
        or not isinstance(review_receipt_paths, Sequence)
        or len(review_receipt_paths) != 2
    ):
        raise ValueError("timezone seal requires exactly two review receipts")
    review_bindings = []
    review_snapshots: list[_SafeSnapshot] = []
    identities = set()
    roles = set()
    seen_paths = set()
    candidate_binding = {
        "path": candidate_relative.as_posix(),
        "sha256": _sha256(candidate_contents),
    }
    paths = _ACTIVE_TIMEZONE_PATHS.get()
    recovery_authority = (
        _recovery_authority_binding(
            candidate["run_spec_sha256"],
            candidate["acquisition_contract_sha256"],
        )
        if paths.run_spec == _RECOVERY_RUN_SPEC_PATH
        else None
    )
    if recovery_authority is not None and candidate.get("authority") != recovery_authority:
        raise ValueError("timezone candidate recovery authority differs")
    if mapping.get("authority") != recovery_authority:
        raise ValueError("timezone mapping recovery authority differs")
    for path in review_receipt_paths:
        review_path = _acquisition_audit_relative(
            root, path, "timezone review receipt"
        )
        review_snapshot = _read_safe_snapshot_from_trusted(
            root, review_path, "timezone review receipt"
        )
        if review_snapshot is None:
            raise ValueError("timezone review receipt is missing")
        relative = review_snapshot.relative
        contents = review_snapshot.contents
        if relative in seen_paths:
            raise ValueError("timezone review receipt paths must be distinct")
        seen_paths.add(relative)
        review_snapshots.append(review_snapshot)
        review = _review_object(contents)
        if review["decision"] != "APPROVED":
            raise ValueError("timezone seal requires APPROVED reviews")
        if review["candidate"] != candidate_binding:
            raise ValueError("timezone review candidate hash binding differs")
        if review["reviewed_venue_ids"] != scope:
            raise ValueError("timezone review scope differs from candidate")
        if review.get("authority") != recovery_authority:
            raise ValueError("timezone review recovery authority differs")
        identities.add(review["reviewer_identity"])
        roles.add(review["reviewer_role"])
        review_bindings.append(
            {
                "path": relative.as_posix(),
                "reviewer_identity": review["reviewer_identity"],
                "reviewer_role": review["reviewer_role"],
                "sha256": _sha256(contents),
            }
        )
    if roles != _ROLES:
        raise ValueError("timezone seal requires exactly one review per role")
    if len(identities) != 2:
        raise ValueError("timezone reviewer identity values must be distinct")
    if recovery_authority is None:
        review_bindings.sort(key=lambda item: item["reviewer_role"])
    else:
        review_order = {
            "TIMEZONE_SEMANTIC_REVIEW": 0,
            "TIMEZONE_COVERAGE_REVIEW": 1,
        }
        review_bindings.sort(key=lambda item: review_order[item["reviewer_role"]])
    mapping_contents = _canonical_file_bytes(mapping)
    existing_seal = _read_safe_snapshot_from_trusted(
        root, receipt_output, "timezone mapping seal receipt", missing_ok=True
    )
    occurred_at = _utc_now()
    if existing_seal is not None:
        existing_value = _json_object(
            existing_seal.contents,
            "timezone mapping seal receipt",
            canonical=True,
        )
        existing_occurred = existing_value.get("occurred_at_utc")
        if not isinstance(existing_occurred, str) or not _UTC.fullmatch(
            existing_occurred
        ):
            raise ValueError("timezone mapping seal receipt timestamp differs")
        occurred_at = existing_occurred
    seal = {
        "candidate": candidate_binding,
        "mapping": {
            "path": _PRODUCTION_MAPPING.as_posix(),
            "sha256": _sha256(mapping_contents),
        },
        "occurred_at_utc": occurred_at,
        "receipt_kind": "TIMEZONE_MAPPING_SEAL",
        "reviews": review_bindings,
        "schema_version": 1,
        "venue_scope": scope,
    }
    if recovery_authority is not None:
        seal["authority"] = recovery_authority
    seal_contents = _canonical_file_bytes(seal)
    def rebind_inputs() -> None:
        _rebind_safe_snapshot_from_trusted(
            root, candidate_snapshot, "timezone candidate"
        )
        for snapshot in review_snapshots:
            _rebind_safe_snapshot_from_trusted(
                root, snapshot, "timezone review receipt"
            )

    mapping_path, mapping_snapshot = _reconcile_or_write_from_trusted(
        root,
        output_relative,
        mapping_contents,
        "timezone mapping",
        final_rebind=rebind_inputs,
    )
    _after_mapping_publish_before_seal()

    def rebind_seal_inputs() -> None:
        rebind_inputs()
        _rebind_safe_snapshot_from_trusted(
            root, mapping_snapshot, "timezone mapping"
        )

    seal_path, seal_snapshot = _reconcile_or_write_from_trusted(
        root,
        receipt_output,
        seal_contents,
        "timezone mapping seal receipt",
        final_rebind=rebind_seal_inputs,
    )
    _before_seal_final_rebind()
    rebind_seal_inputs()
    _rebind_safe_snapshot_from_trusted(
        root, seal_snapshot, "timezone mapping seal receipt"
    )
    root.assert_bound()
    return mapping_path, seal_path


def build_timezone_candidate(
    *,
    root: Path,
    evidence_root: Path,
    batch_ids: Sequence[str],
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> Mapping[str, object]:
    context = _explicit_authority_context(
        root, run_spec_path, acquisition_contract_path
    )
    if context is not None:
        if Path(os.path.abspath(os.fspath(evidence_root))) != context.raw_root:
            raise ValueError("timezone evidence root differs from authority")
    with _authority_scope(context), open_trusted_root(root) as trusted:
        result = _build_timezone_candidate(
            trusted,
            evidence_root=evidence_root,
            batch_ids=batch_ids,
            authority_context=context,
        )
        trusted.assert_bound()
        return result


def _build_timezone_candidate(
    root: TrustedRoot,
    *,
    evidence_root: Path,
    batch_ids: Sequence[str],
    authority_context: SourceAuthorityContext | None = None,
) -> Mapping[str, object]:
    if (
        isinstance(batch_ids, (str, bytes))
        or not isinstance(batch_ids, Sequence)
        or not batch_ids
    ):
        raise ValueError("candidate requires one or more sealed batch IDs")
    if len(batch_ids) != len(set(batch_ids)):
        raise ValueError("candidate batch IDs contain a duplicate")
    store = V52EvidenceStore(evidence_root)
    schedule_sidecars: list[Mapping[str, object]] = []
    venue_sidecars: list[Mapping[str, object]] = []
    seen_queries: set[str] = set()
    contributing_batch_ids: set[str] = set()
    stage_batch_ids: dict[str, str] = {}
    binding: tuple[str, str] | None = None
    for batch_id in batch_ids:
        root.assert_bound()
        batch = (
            load_query_batch_for_authority(
                root.path, batch_id, authority_context.authority
            )
            if authority_context is not None
            else load_query_batch(root.path, batch_id)
        )
        root.assert_bound()
        if batch.stage in {"STATIC_SCHEDULES", "REFERENCE_INPUTS"}:
            if batch.stage in stage_batch_ids:
                raise ValueError("candidate stage batch population differs")
            stage_batch_ids[batch.stage] = batch.batch_id
        for query in batch.queries:
            query_id = logical_query_id(query)
            kind: str | None = None
            venue_id: int | None = None
            if (
                query.source == "mlb_schedule_game_feed"
                and query.grain == "season_schedule_and_game"
                and query.endpoint == _SCHEDULE_ENDPOINT
            ):
                kind = "schedule"
            elif (
                query.source == "venue_team_timezone_table"
                and query.grain == "effective_dated_venue_team_reference"
                and query.endpoint.startswith(_VENUE_ENDPOINT_PREFIX)
            ):
                venue_id = _positive(
                    query.requested_range.get("venue_id"), "venue query id"
                )
                if query.endpoint != f"{_VENUE_ENDPOINT_PREFIX}{venue_id}":
                    raise ValueError("candidate venue query endpoint differs")
                kind = "venue"
            if kind is None:
                continue
            required_stage = (
                "STATIC_SCHEDULES" if kind == "schedule" else "REFERENCE_INPUTS"
            )
            if batch.stage != required_stage:
                raise ValueError(
                    f"candidate {kind} query requires {required_stage} batch stage"
                )
            current_binding = (
                query.run_spec_sha256,
                query.acquisition_contract_sha256,
            )
            if binding is None:
                binding = current_binding
            elif binding != current_binding:
                raise ValueError(
                    "candidate relevant query run/contract binding differs"
                )
            contributing_batch_ids.add(batch.batch_id)
            if query_id in seen_queries:
                raise ValueError("candidate query population contains a duplicate")
            seen_queries.add(query_id)
            try:
                root.assert_bound()
                verified = store.read_verified_success(query)
                root.assert_bound()
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                raise ValueError(
                    f"candidate expected query {query_id} is missing or unpublished"
                ) from exc
            if verified.registered_query != query:
                raise ValueError("candidate verified query registration differs")
            if verified.published_evidence.storage_mode not in {
                "FETCHED_RAW",
                "PROTECTED_LEGACY_REFERENCE",
            }:
                raise ValueError(
                    "candidate schedule/venue evidence cannot be derived"
                )
            sidecar = verified.query_sidecar
            if sidecar.get("query_id") != query_id:
                raise ValueError("candidate query sidecar binding differs")
            if kind == "schedule":
                _sidecar_semantics(
                    sidecar,
                    source="mlb_schedule_game_feed",
                    grain="season_schedule_and_game",
                    label="schedule",
                )
                schedule_sidecars.append(sidecar)
                continue
            if kind == "venue" and venue_id is not None:
                semantics = _sidecar_semantics(
                    sidecar,
                    source="venue_team_timezone_table",
                    grain="effective_dated_venue_team_reference",
                    label="venue",
                )
                venues = semantics.get("venues")
                if (
                    not isinstance(venues, list)
                    or len(venues) != 1
                    or not isinstance(venues[0], Mapping)
                    or venues[0].get("venue_id") != venue_id
                ):
                    raise ValueError(
                        "candidate venue sidecar differs from registered query"
                    )
                venue_sidecars.append(sidecar)
                continue
            raise AssertionError("unreachable candidate query classification")
    source_authority = (
        authority_context is not None
        and isinstance(authority_context.authority, SourceRunSpec)
    )
    if (not source_authority and not schedule_sidecars) or not venue_sidecars:
        raise ValueError(
            "candidate requires schedule anchors and venue reference evidence"
        )
    if binding is None:
        raise AssertionError("unreachable candidate provenance binding")
    if authority_context is not None and binding != (
        authority_context.authority_sha256,
        authority_context.acquisition_contract_sha256,
    ):
        raise ValueError("candidate query authority differs from explicit authority")
    if source_authority:
        from .v52_schedule_inventory import (
            load_verified_schedule_anchor_inventory,
        )

        if set(stage_batch_ids) != {"STATIC_SCHEDULES", "REFERENCE_INPUTS"}:
            raise ValueError("source candidate requires exact static/reference batches")
        static_batch_id = stage_batch_ids["STATIC_SCHEDULES"]
        root.assert_bound()
        verified_inventory = load_verified_schedule_anchor_inventory(
            root.path,
            authority_context.authority,
            acquisition_contract_path=authority_context.acquisition_contract_path,
            static_batch_id=static_batch_id,
            evidence_root=authority_context.raw_root,
        )
        root.assert_bound()
        observed, schedule_hashes = _schedule_inventory_components(
            verified_inventory.payload
        )
        venues, venue_hashes = _venues_from_sidecars(venue_sidecars)
        candidate = _project_timezone_candidate_components(
            observed,
            schedule_hashes,
            venues,
            venue_hashes,
            query_batch_ids=sorted(contributing_batch_ids),
            run_spec_sha256=binding[0],
            acquisition_contract_sha256=binding[1],
        )
    else:
        candidate = _project_timezone_candidate(
            schedule_sidecars,
            venue_sidecars,
            query_batch_ids=sorted(contributing_batch_ids),
            run_spec_sha256=binding[0],
            acquisition_contract_sha256=binding[1],
        )
    if (
        authority_context is not None
        and _timezone_paths(authority_context).run_spec == _RECOVERY_RUN_SPEC_PATH
    ):
        candidate = dict(candidate)
        candidate["authority"] = _recovery_authority_binding(
            candidate["run_spec_sha256"],
            candidate["acquisition_contract_sha256"],
        )
    root.assert_bound()
    return candidate


def _validate_replayed_candidate_rows(
    stored: Mapping[str, object],
    generated: Mapping[str, object],
) -> None:
    rows = _normalize_timezone_rows(stored["timezone_rows"], allow_null=True)
    expected = _normalize_timezone_rows(
        generated["timezone_rows"], allow_null=True
    )
    if set(rows) != set(expected):
        raise ValueError("timezone candidate row scope differs")
    observed = {
        (int(row["venue_id"]), str(row["official_date"])): (
            bool(row["neutral_site"]),
            bool(row["alternate_site"]),
        )
        for row in stored["observed_venue_dates"]  # type: ignore[union-attr]
    }
    _coverage(rows, observed)
    if all(row["time_zone"] is not None for values in rows.values() for row in values):
        _mapping_from_candidate(stored, require_resolved=True)


def publish_timezone_candidate(
    *,
    root: Path,
    evidence_root: Path,
    batch_ids: Sequence[str],
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> Path:
    """Publish or reconcile the one canonical timezone-review candidate."""
    context = _explicit_authority_context(
        root, run_spec_path, acquisition_contract_path
    )
    if context is not None:
        if Path(os.path.abspath(os.fspath(evidence_root))) != context.raw_root:
            raise ValueError("timezone evidence root differs from authority")
    with _authority_scope(context), open_trusted_root(root) as trusted:
        if context is not None and isinstance(context.authority, SourceRunSpec):
            from .execution_registry import _using_verified_source_checkpoint

            with _using_verified_source_checkpoint(root, context.authority):
                result = _publish_timezone_candidate(
                    trusted,
                    evidence_root=evidence_root,
                    batch_ids=batch_ids,
                    authority_context=context,
                )
        else:
            result = _publish_timezone_candidate(
                trusted,
                evidence_root=evidence_root,
                batch_ids=batch_ids,
                authority_context=context,
            )
        trusted.assert_bound()
        return result


def _publish_timezone_candidate(
    root: TrustedRoot,
    *,
    evidence_root: Path,
    batch_ids: Sequence[str],
    authority_context: SourceAuthorityContext | None = None,
) -> Path:
    paths = _ACTIVE_TIMEZONE_PATHS.get()
    candidate = _build_timezone_candidate(
        root,
        evidence_root=evidence_root,
        batch_ids=batch_ids,
        authority_context=authority_context,
    )
    contents = _canonical_file_bytes(candidate)
    existing = _read_safe_snapshot_from_trusted(
        root,
        paths.candidate,
        "timezone candidate",
        missing_ok=True,
        max_bytes=_MAX_CANDIDATE_BYTES,
    )
    if existing is not None:
        stored = _candidate_object(
            _json_object(existing.contents, "timezone candidate", canonical=True)
        )
        stored_projection = {
            key: value for key, value in stored.items() if key != "timezone_rows"
        }
        candidate_projection = {
            key: value for key, value in candidate.items() if key != "timezone_rows"
        }
        if stored_projection != candidate_projection:
            raise ValueError("timezone candidate differs from current source authority")
        _validate_replayed_candidate_rows(stored, candidate)
        rebound = _build_timezone_candidate(
            root,
            evidence_root=evidence_root,
            batch_ids=batch_ids,
            authority_context=authority_context,
        )
        if rebound != candidate:
            raise ValueError("timezone candidate authority changed during replay")
        _before_timezone_candidate_final_rebind()
        final = _build_timezone_candidate(
            root,
            evidence_root=evidence_root,
            batch_ids=batch_ids,
            authority_context=authority_context,
        )
        if final != candidate:
            raise ValueError("timezone candidate authority changed during final rebind")
        _rebind_safe_snapshot_from_trusted(
            root,
            existing,
            "timezone candidate",
            max_bytes=_MAX_CANDIDATE_BYTES,
        )
        root.assert_bound()
        return root.path / paths.candidate

    def rebind() -> None:
        current = _build_timezone_candidate(
            root,
            evidence_root=evidence_root,
            batch_ids=batch_ids,
            authority_context=authority_context,
        )
        if current != candidate:
            raise ValueError("timezone candidate authority changed before publication")

    path, snapshot = _reconcile_or_write_from_trusted(
        root,
        paths.candidate,
        contents,
        "timezone candidate",
        final_rebind=rebind,
    )
    _before_timezone_candidate_final_rebind()
    rebind()
    _rebind_safe_snapshot_from_trusted(
        root, snapshot, "timezone candidate", max_bytes=_MAX_CANDIDATE_BYTES
    )
    root.assert_bound()
    return path


def _required_snapshot(
    root: TrustedRoot,
    relative: Path,
    label: str,
    *,
    max_bytes: int,
) -> _SafeSnapshot:
    snapshot = _read_safe_snapshot_from_trusted(
        root,
        relative,
        label,
        max_bytes=max_bytes,
    )
    if snapshot is None:
        raise ValueError(f"{label} is missing")
    return snapshot


def _current_timezone_authority(
    root: TrustedRoot,
    *,
    read_only_progress: bool = False,
    authority_context: SourceAuthorityContext | None = None,
) -> _CurrentTimezoneAuthority:
    from .protocols import parse_run_spec
    from .v52_query_plan import (
        build_schedule_reference_batch,
        load_query_batch,
        load_query_batch_for_authority,
    )
    from .v52_schedule_inventory import (
        _read_inventory_contents,
        load_verified_schedule_anchor_inventory,
    )
    from .v52_sources.references import recompute_venue_query_sidecar

    paths = _timezone_paths(authority_context)
    run_snapshot = _required_snapshot(
        root,
        paths.run_spec,
        "timezone gate RunSpec",
        max_bytes=_MAX_RECEIPT_BYTES,
    )
    contract_snapshot = _required_snapshot(
        root,
        paths.acquisition_contract,
        "timezone gate acquisition contract",
        max_bytes=_MAX_RECEIPT_BYTES,
    )
    if authority_context is None:
        run_spec = parse_run_spec(
            _json_object(
                run_snapshot.contents,
                "timezone gate RunSpec",
                canonical=True,
            )
        )
    else:
        run_spec = authority_context.authority
        if (
            _sha256(run_snapshot.contents) != authority_context.authority_sha256
            or _sha256(contract_snapshot.contents)
            != authority_context.acquisition_contract_sha256
        ):
            raise ValueError("timezone explicit authority bytes differ")
    namespaces = run_spec.raw.get("namespaces")
    raw_namespace = (
        namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    )
    if not isinstance(raw_namespace, str):
        raise ValueError("timezone gate RunSpec raw namespace differs")
    raw_relative = Path(raw_namespace)
    if (
        raw_relative.is_absolute()
        or not raw_relative.parts
        or raw_relative.as_posix() != raw_namespace
        or any(part in {"", ".", ".."} for part in raw_relative.parts)
    ):
        raise ValueError("timezone gate RunSpec raw namespace differs")

    root.assert_bound()
    inventory_contents = _read_inventory_contents(
        root.path, paths.schedule_inventory
    )
    root.assert_bound()
    inventory_record = _json_object(
        inventory_contents,
        "schedule anchor inventory",
        canonical=True,
    )
    static_binding = inventory_record.get("static_batch")
    if not isinstance(static_binding, Mapping):
        raise ValueError("timezone gate static batch binding differs")
    static_batch_id = _sha256_text(
        static_binding.get("batch_id"), "timezone gate static batch ID"
    )
    evidence_root = root.path / raw_relative
    root.assert_bound()
    verified_inventory = load_verified_schedule_anchor_inventory(
        root.path,
        run_spec,
        acquisition_contract_path=root.path / paths.acquisition_contract,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    root.assert_bound()
    inventory_snapshot = _required_snapshot(
        root,
        paths.schedule_inventory,
        "timezone gate schedule inventory",
        max_bytes=_MAX_AUTHORITY_BYTES,
    )
    if _sha256(inventory_snapshot.contents) != verified_inventory.sha256:
        raise ValueError("timezone gate schedule inventory hash differs")
    static_batch_snapshot = _required_snapshot(
        root,
        paths.query_batch_root / f"{static_batch_id}.json",
        "timezone gate STATIC_SCHEDULES batch",
        max_bytes=_MAX_AUTHORITY_BYTES,
    )
    root.assert_bound()
    static_batch = (
        load_query_batch_for_authority(
            root.path,
            static_batch_id,
            run_spec,
            read_only_progress=read_only_progress,
        )
        if authority_context is not None
        else load_query_batch(
            root.path,
            static_batch_id,
            read_only_progress=read_only_progress,
        )
    )
    root.assert_bound()
    root.assert_bound()
    reference_expected = build_schedule_reference_batch(
        root.path,
        root.path / paths.run_spec,
        acquisition_contract_path=root.path / paths.acquisition_contract,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    root.assert_bound()
    reference_batch_snapshot = _required_snapshot(
        root,
        paths.query_batch_root / f"{reference_expected.batch_id}.json",
        "timezone gate REFERENCE_INPUTS batch",
        max_bytes=_MAX_AUTHORITY_BYTES,
    )
    root.assert_bound()
    reference_batch = (
        load_query_batch_for_authority(
            root.path,
            reference_expected.batch_id,
            run_spec,
            read_only_progress=read_only_progress,
        )
        if authority_context is not None
        else load_query_batch(
            root.path,
            reference_expected.batch_id,
            read_only_progress=read_only_progress,
        )
    )
    root.assert_bound()
    if reference_batch != reference_expected:
        raise ValueError("timezone gate REFERENCE_INPUTS authority differs")
    if (
        static_batch.stage != "STATIC_SCHEDULES"
        or reference_batch.stage != "REFERENCE_INPUTS"
        or reference_batch.derived_from_sha256 != verified_inventory.sha256
    ):
        raise ValueError("timezone gate batch/inventory authority differs")

    batch_snapshots = (
        static_batch_snapshot,
        reference_batch_snapshot,
    )

    payload = verified_inventory.payload
    raw_anchors = payload.get("anchors")
    if not isinstance(raw_anchors, Sequence) or not raw_anchors:
        raise ValueError("timezone gate schedule inventory anchors differ")
    observed: dict[tuple[int, str], tuple[bool, bool]] = {}
    for anchor in raw_anchors:
        if not isinstance(anchor, Mapping):
            raise ValueError("timezone gate schedule inventory anchor differs")
        venue_id = _positive(anchor.get("venue_id"), "timezone gate venue ID")
        official = _day(
            anchor.get("official_date"), "timezone gate official date"
        ).isoformat()
        neutral = anchor.get("neutral_site")
        alternate = anchor.get("alternate_site")
        if type(neutral) is not bool or type(alternate) is not bool:
            raise ValueError("timezone gate observed venue flags differ")
        key = (venue_id, official)
        disposition = (neutral, alternate)
        if key in observed and observed[key] != disposition:
            raise ValueError("timezone gate observed venue disposition conflicts")
        observed[key] = disposition

    season_sources = payload.get("season_sources")
    if not isinstance(season_sources, Sequence) or not season_sources:
        raise ValueError("timezone gate schedule source bindings differ")
    schedule_sidecar_sha256s = []
    for source in season_sources:
        if not isinstance(source, Mapping) or not isinstance(
            source.get("query_sidecar"), Mapping
        ):
            raise ValueError("timezone gate schedule source binding differs")
        schedule_sidecar_sha256s.append(
            _sha256_text(
                source["query_sidecar"].get("sha256"),
                "timezone gate schedule sidecar SHA-256",
            )
        )
    if len(schedule_sidecar_sha256s) != len(set(schedule_sidecar_sha256s)):
        raise ValueError("timezone gate schedule sidecar population differs")
    schedule_sidecar_sha256s.sort()

    store = (
        V52EvidenceStore.open_read_only(evidence_root)
        if read_only_progress
        else V52EvidenceStore(evidence_root)
    )
    venue_sidecars: list[Mapping[str, object]] = []
    for query in reference_batch.queries:
        if not (
            query.source == "venue_team_timezone_table"
            and query.grain == "effective_dated_venue_team_reference"
            and query.endpoint.startswith(_VENUE_ENDPOINT_PREFIX)
        ):
            continue
        root.assert_bound()
        verified = store.read_verified_success(query)
        root.assert_bound()
        if verified.registered_query != query or verified.raw_bytes is None:
            raise ValueError("timezone gate venue verified SUCCESS differs")
        recomputed = recompute_venue_query_sidecar(verified.raw_bytes, query)
        if recomputed != verified.query_sidecar:
            raise ValueError("timezone gate venue raw/sidecar semantics differ")
        venue_sidecars.append(recomputed)
    venues, venue_sidecar_sha256s = _venues_from_sidecars(venue_sidecars)
    observed_scope = {venue_id for venue_id, _ in observed}
    if set(venues) != observed_scope:
        raise ValueError("timezone gate venue evidence scope differs")
    observed_rows = [
        {
            "alternate_site": disposition[1],
            "neutral_site": disposition[0],
            "official_date": official,
            "venue_id": venue_id,
        }
        for (venue_id, official), disposition in sorted(observed.items())
    ]
    run_sha = _sha256(run_snapshot.contents)
    contract_sha = _sha256(contract_snapshot.contents)
    source_projection = {
        "acquisition_contract_sha256": contract_sha,
        "artifact_kind": "VENUE_TIMEZONE_CANDIDATE",
        "observed_venue_dates": observed_rows,
        "query_batch_ids": sorted(
            (static_batch.batch_id, reference_batch.batch_id)
        ),
        "run_spec_sha256": run_sha,
        "schedule_sidecar_sha256s": schedule_sidecar_sha256s,
        "schema_version": 1,
        "venue_scope": sorted(observed_scope),
        "venue_sidecar_sha256s": venue_sidecar_sha256s,
        "venues": [venues[venue_id] for venue_id in sorted(venues)],
    }
    if paths.run_spec == _RECOVERY_RUN_SPEC_PATH:
        source_projection["authority"] = _recovery_authority_binding(
            run_sha, contract_sha
        )
    root.assert_bound()
    return _CurrentTimezoneAuthority(
        source_projection=source_projection,
        run_spec_sha256=run_sha,
        acquisition_contract_sha256=contract_sha,
        schedule_inventory_sha256=verified_inventory.sha256,
        query_batch_ids=(static_batch.batch_id, reference_batch.batch_id),
        query_batch_sha256s=tuple(
            _sha256(snapshot.contents) for snapshot in batch_snapshots
        ),  # type: ignore[arg-type]
        snapshots=(
            run_snapshot,
            contract_snapshot,
            inventory_snapshot,
            *batch_snapshots,
        ),
    )


def _seal_object(contents: bytes) -> dict[str, object]:
    value = _json_object(
        contents,
        "timezone mapping seal receipt",
        canonical=True,
    )
    base_keys = {
        "candidate",
        "mapping",
        "occurred_at_utc",
        "receipt_kind",
        "reviews",
        "schema_version",
        "venue_scope",
    }
    if set(value) not in (base_keys, base_keys | {"authority"}):
        raise ValueError("timezone mapping seal receipt keys differ")
    if (
        value.get("schema_version") != 1
        or value.get("receipt_kind") != "TIMEZONE_MAPPING_SEAL"
        or not isinstance(value.get("occurred_at_utc"), str)
        or _UTC.fullmatch(str(value.get("occurred_at_utc"))) is None
        or not isinstance(value.get("candidate"), Mapping)
        or not isinstance(value.get("mapping"), Mapping)
        or not isinstance(value.get("reviews"), list)
        or len(value["reviews"]) != 2
        or not isinstance(value.get("venue_scope"), list)
    ):
        raise ValueError("timezone mapping seal receipt identity differs")
    authority = value.get("authority")
    if authority is not None:
        if (
            not isinstance(authority, Mapping)
            or set(authority) != {"acquisition_contract", "run_spec"}
        ):
            raise ValueError("timezone mapping seal receipt authority differs")
        run_binding = authority.get("run_spec")
        contract_binding = authority.get("acquisition_contract")
        if (
            not isinstance(run_binding, Mapping)
            or not isinstance(contract_binding, Mapping)
            or authority
            != _recovery_authority_binding(
                run_binding.get("sha256"), contract_binding.get("sha256")
            )
        ):
            raise ValueError("timezone mapping seal receipt authority differs")
    return value


def _rebind_timezone_authority(
    root: TrustedRoot,
    authority: _CurrentTimezoneAuthority,
) -> None:
    for snapshot in authority.snapshots:
        _rebind_safe_snapshot_from_trusted(
            root,
            snapshot,
            "timezone source authority",
            max_bytes=_MAX_AUTHORITY_BYTES,
        )
    root.assert_bound()


def load_verified_timezone_gate(
    root: Path,
    *,
    read_only_progress: bool = False,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> VerifiedTimezoneGate:
    context = _explicit_authority_context(
        root, run_spec_path, acquisition_contract_path
    )
    with _authority_scope(context), open_trusted_root(root) as trusted:
        if context is not None and isinstance(context.authority, SourceRunSpec):
            from .execution_registry import _using_verified_source_checkpoint

            with _using_verified_source_checkpoint(root, context.authority):
                result = _load_verified_timezone_gate(
                    trusted,
                    read_only_progress=read_only_progress,
                    authority_context=context,
                )
        else:
            result = _load_verified_timezone_gate(
                trusted,
                read_only_progress=read_only_progress,
                authority_context=context,
            )
        trusted.assert_bound()
        return result


def _load_verified_timezone_gate(
    root: TrustedRoot,
    *,
    read_only_progress: bool = False,
    authority_context: SourceAuthorityContext | None = None,
) -> VerifiedTimezoneGate:
    paths = _timezone_paths(authority_context)
    fixed_snapshots = (
        _required_snapshot(
            root,
            paths.candidate,
            "timezone candidate",
            max_bytes=_MAX_CANDIDATE_BYTES,
        ),
        _required_snapshot(
            root,
            paths.coverage_review,
            "timezone coverage review",
            max_bytes=_MAX_RECEIPT_BYTES,
        ),
        _required_snapshot(
            root,
            paths.semantic_review,
            "timezone semantic review",
            max_bytes=_MAX_RECEIPT_BYTES,
        ),
        _required_snapshot(
            root,
            _PRODUCTION_MAPPING,
            "timezone mapping",
            max_bytes=_MAX_MAPPING_BYTES,
        ),
        _required_snapshot(
            root,
            paths.seal,
            "timezone mapping seal receipt",
            max_bytes=_MAX_RECEIPT_BYTES,
        ),
    )
    candidate_snapshot, *review_and_seal = fixed_snapshots
    coverage_snapshot = review_and_seal[0]
    semantic_snapshot = review_and_seal[1]
    mapping_snapshot = review_and_seal[2]
    seal_snapshot = review_and_seal[3]
    candidate = _candidate_object(
        _json_object(
            candidate_snapshot.contents,
            "timezone candidate",
            canonical=True,
        )
    )
    authority = (
        _current_timezone_authority(
            root,
            read_only_progress=read_only_progress,
            authority_context=authority_context,
        )
        if authority_context is not None
        else _current_timezone_authority(
            root,
            read_only_progress=read_only_progress,
        )
    )
    _rebind_timezone_authority(root, authority)
    candidate_projection = {
        key: value for key, value in candidate.items() if key != "timezone_rows"
    }
    if candidate_projection != authority.source_projection:
        raise ValueError("timezone candidate differs from current source authority")
    mapping = _mapping_from_candidate(candidate, require_resolved=True)
    stored_mapping = _mapping_object(
        _json_object(
            mapping_snapshot.contents,
            "timezone mapping",
            canonical=True,
        )
    )
    if stored_mapping != mapping:
        raise ValueError("timezone mapping differs from resolved candidate")

    candidate_binding = {
        "path": paths.candidate.as_posix(),
        "sha256": _sha256(candidate_snapshot.contents),
    }
    recovery_authority = (
        _recovery_authority_binding(
            authority.run_spec_sha256,
            authority.acquisition_contract_sha256,
        )
        if paths.run_spec == _RECOVERY_RUN_SPEC_PATH
        else None
    )
    if candidate.get("authority") != recovery_authority:
        raise ValueError("timezone candidate recovery authority differs")
    roles_and_reviews = (
        (
            "TIMEZONE_COVERAGE_REVIEW",
            paths.coverage_review,
            coverage_snapshot,
        ),
        (
            "TIMEZONE_SEMANTIC_REVIEW",
            paths.semantic_review,
            semantic_snapshot,
        ),
    )
    reviews: list[dict[str, object]] = []
    identities: set[str] = set()
    review_bindings = []
    for expected_role, expected_path, snapshot in roles_and_reviews:
        review = _review_object(snapshot.contents)
        if (
            review["decision"] != "APPROVED"
            or review["reviewer_role"] != expected_role
            or review["candidate"] != candidate_binding
            or review["reviewed_venue_ids"] != candidate["venue_scope"]
            or review.get("authority") != recovery_authority
        ):
            raise ValueError("timezone review approval authority differs")
        identity = str(review["reviewer_identity"])
        identities.add(identity)
        reviews.append(review)
        review_bindings.append(
            {
                "path": expected_path.as_posix(),
                "reviewer_identity": identity,
                "reviewer_role": expected_role,
                "sha256": _sha256(snapshot.contents),
            }
        )
    if len(identities) != 2:
        raise ValueError("timezone reviewer identity values must be distinct")
    if recovery_authority is not None:
        review_order = {
            "TIMEZONE_SEMANTIC_REVIEW": 0,
            "TIMEZONE_COVERAGE_REVIEW": 1,
        }
        review_bindings.sort(key=lambda item: review_order[item["reviewer_role"]])

    mapping_binding = {
        "path": _PRODUCTION_MAPPING.as_posix(),
        "sha256": _sha256(mapping_snapshot.contents),
    }
    seal = _seal_object(seal_snapshot.contents)
    if paths.run_spec == _RECOVERY_RUN_SPEC_PATH:
        expected_authority = recovery_authority
        if seal.get("authority") != expected_authority:
            raise ValueError("timezone mapping seal recovery authority differs")
    elif "authority" in seal:
        raise ValueError("timezone mapping seal authority differs")
    if (
        seal["candidate"] != candidate_binding
        or seal["mapping"] != mapping_binding
        or seal["reviews"] != review_bindings
        or seal["venue_scope"] != candidate["venue_scope"]
    ):
        raise ValueError("timezone mapping seal authority differs")

    _before_timezone_gate_final_rebind()
    rebound_authority = (
        _current_timezone_authority(
            root,
            read_only_progress=read_only_progress,
            authority_context=authority_context,
        )
        if authority_context is not None
        else _current_timezone_authority(
            root,
            read_only_progress=read_only_progress,
        )
    )
    _rebind_timezone_authority(root, authority)
    _rebind_timezone_authority(root, rebound_authority)
    root.assert_bound()
    if (
        rebound_authority.source_projection != authority.source_projection
        or rebound_authority.run_spec_sha256 != authority.run_spec_sha256
        or rebound_authority.acquisition_contract_sha256
        != authority.acquisition_contract_sha256
        or rebound_authority.schedule_inventory_sha256
        != authority.schedule_inventory_sha256
        or rebound_authority.query_batch_ids != authority.query_batch_ids
        or rebound_authority.query_batch_sha256s
        != authority.query_batch_sha256s
    ):
        raise ValueError("timezone source authority changed during verified load")
    for snapshot, label, max_bytes in (
        (candidate_snapshot, "timezone candidate", _MAX_CANDIDATE_BYTES),
        (
            coverage_snapshot,
            "timezone coverage review",
            _MAX_RECEIPT_BYTES,
        ),
        (
            semantic_snapshot,
            "timezone semantic review",
            _MAX_RECEIPT_BYTES,
        ),
        (mapping_snapshot, "timezone mapping", _MAX_MAPPING_BYTES),
        (
            seal_snapshot,
            "timezone mapping seal receipt",
            _MAX_RECEIPT_BYTES,
        ),
    ):
        _rebind_safe_snapshot_from_trusted(
            root,
            snapshot,
            label,
            max_bytes=max_bytes,
        )
    _rebind_timezone_authority(root, authority)
    _rebind_timezone_authority(root, rebound_authority)
    root.assert_bound()

    frozen_candidate = _deep_freeze(candidate)
    frozen_mapping = _deep_freeze(mapping)
    frozen_reviews = tuple(_deep_freeze(review) for review in reviews)
    frozen_seal = _deep_freeze(seal)
    if (
        not isinstance(frozen_candidate, Mapping)
        or not isinstance(frozen_mapping, Mapping)
        or not all(isinstance(review, Mapping) for review in frozen_reviews)
        or not isinstance(frozen_seal, Mapping)
    ):
        raise AssertionError("verified timezone gate must deep-freeze to mappings")
    root.assert_bound()
    return VerifiedTimezoneGate(
        candidate=frozen_candidate,
        mapping=frozen_mapping,
        reviews=frozen_reviews,  # type: ignore[arg-type]
        seal=frozen_seal,
        candidate_sha256=candidate_binding["sha256"],
        mapping_sha256=mapping_binding["sha256"],
        review_sha256s=tuple(
            binding["sha256"] for binding in review_bindings
        ),  # type: ignore[arg-type]
        seal_sha256=_sha256(seal_snapshot.contents),
        run_spec_sha256=authority.run_spec_sha256,
        acquisition_contract_sha256=authority.acquisition_contract_sha256,
        schedule_inventory_sha256=authority.schedule_inventory_sha256,
        query_batch_ids=authority.query_batch_ids,
        query_batch_sha256s=authority.query_batch_sha256s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    candidate_parser = subparsers.add_parser("candidate")
    candidate_parser.add_argument("--root", type=Path, required=True)
    candidate_parser.add_argument("--evidence-root", type=Path, required=True)
    candidate_parser.add_argument(
        "--batch-id", action="append", required=True
    )
    candidate_parser.add_argument("--output", type=Path, required=True)
    candidate_parser.add_argument(
        "--run-spec", type=Path, default=_RUN_SPEC_PATH
    )
    candidate_parser.add_argument(
        "--acquisition-contract",
        type=Path,
        default=_ACQUISITION_CONTRACT_PATH,
    )
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("--root", type=Path, required=True)
    review_parser.add_argument("--candidate", type=Path, required=True)
    review_parser.add_argument("--output", type=Path, required=True)
    review_parser.add_argument("--reviewer-identity", required=True)
    review_parser.add_argument(
        "--reviewer-role", choices=sorted(_ROLES), required=True
    )
    review_parser.add_argument(
        "--decision", choices=sorted(_DECISIONS), required=True
    )
    review_parser.add_argument("--run-spec", type=Path, default=_RUN_SPEC_PATH)
    review_parser.add_argument(
        "--acquisition-contract",
        type=Path,
        default=_ACQUISITION_CONTRACT_PATH,
    )
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--root", type=Path, required=True)
    seal_parser.add_argument("--candidate", type=Path, required=True)
    seal_parser.add_argument(
        "--review-receipt", type=Path, action="append", required=True
    )
    seal_parser.add_argument("--output", type=Path, required=True)
    seal_parser.add_argument("--receipt-output", type=Path, required=True)
    seal_parser.add_argument("--run-spec", type=Path, default=_RUN_SPEC_PATH)
    seal_parser.add_argument(
        "--acquisition-contract",
        type=Path,
        default=_ACQUISITION_CONTRACT_PATH,
    )
    arguments = parser.parse_args(argv)
    run_spec_path = arguments.root / arguments.run_spec
    acquisition_contract_path = arguments.root / arguments.acquisition_contract
    context = _explicit_authority_context(
        arguments.root, run_spec_path, acquisition_contract_path
    )
    if arguments.command == "candidate":
        if arguments.output != _timezone_paths(context).candidate:
            raise ValueError("timezone candidate output must be the canonical path")
        publish_timezone_candidate(
            root=arguments.root,
            evidence_root=arguments.evidence_root,
            batch_ids=arguments.batch_id,
            run_spec_path=run_spec_path,
            acquisition_contract_path=acquisition_contract_path,
        )
        return 0
    if arguments.command == "review":
        write_timezone_review_receipt(
            arguments.root,
            candidate_path=arguments.candidate,
            output=arguments.output,
            reviewer_identity=arguments.reviewer_identity,
            reviewer_role=arguments.reviewer_role,
            decision=arguments.decision,
            run_spec_path=run_spec_path,
            acquisition_contract_path=acquisition_contract_path,
        )
        return 0
    if arguments.command == "seal":
        seal_timezone_mapping(
            arguments.root,
            candidate_path=arguments.candidate,
            review_receipt_paths=arguments.review_receipt,
            output=arguments.output,
            receipt_output=arguments.receipt_output,
            run_spec_path=run_spec_path,
            acquisition_contract_path=acquisition_contract_path,
        )
        return 0
    raise AssertionError("unreachable timezone review command")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "VerifiedTimezoneGate",
    "build_timezone_candidate",
    "load_verified_timezone_gate",
    "main",
    "publish_timezone_candidate",
    "seal_timezone_mapping",
    "validate_timezone_mapping",
    "write_timezone_review_receipt",
]
