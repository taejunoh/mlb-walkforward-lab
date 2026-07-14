"""Inventory-bound promotion of protected v5.1 raw evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode

from .v52_evidence import (
    LogicalQuery,
    PublishedEvidence,
    V52EvidenceStore,
    logical_query_id,
)
from .v52_protected_inventory import verify_protected_raw_inventory
from .v52_seasons import SEASON_BOUNDARIES
from .v52_source_contracts import canonical_json_bytes, recompute_query_sidecar


_LEGACY_ROOT = Path("data/v5_1_bt/raw")
_INVENTORY_PATH = Path("audit/v5_1_bt/protected_raw_inventory.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PromotionDisposition(StrEnum):
    PROMOTED = "PROMOTED"
    FETCH_REQUIRED = "FETCH_REQUIRED"
    FATAL_INTEGRITY = "FATAL_INTEGRITY"


class PromotionReasonCode(StrEnum):
    NO_EXACT_PROTECTED_CANDIDATE = "NO_EXACT_PROTECTED_CANDIDATE"
    CURRENT_QUERY_INVALID = "CURRENT_QUERY_INVALID"
    CURRENT_SEMANTIC_CONTRACT_UNSATISFIED = "CURRENT_SEMANTIC_CONTRACT_UNSATISFIED"
    PROTECTED_PATH_INVALID = "PROTECTED_PATH_INVALID"
    PROTECTED_INVENTORY_INVALID = "PROTECTED_INVENTORY_INVALID"
    PROTECTED_MEMBERSHIP_INVALID = "PROTECTED_MEMBERSHIP_INVALID"
    LEGACY_LINK_TYPE_INVALID = "LEGACY_LINK_TYPE_INVALID"
    LEGACY_PATH_IDENTITY_CHANGED = "LEGACY_PATH_IDENTITY_CHANGED"
    LEGACY_MANIFEST_INVALID = "LEGACY_MANIFEST_INVALID"
    LEGACY_FILENAME_INVALID = "LEGACY_FILENAME_INVALID"
    LEGACY_SEAL_INVALID = "LEGACY_SEAL_INVALID"
    LEGACY_RAW_BYTES_INVALID = "LEGACY_RAW_BYTES_INVALID"
    LEGACY_ROW_COUNT_INVALID = "LEGACY_ROW_COUNT_INVALID"
    CATALOG_NOT_UNIQUE = "CATALOG_NOT_UNIQUE"


@dataclass(frozen=True)
class LegacyCandidate:
    source: str
    manifest_path: Path
    manifest_sha256: str
    seal_path: Path
    payload_path: Path
    raw_sha256: str


@dataclass(frozen=True)
class ProtectedLegacyCatalog:
    repository_root: Path
    legacy_root: Path
    protected_inventory_path: Path
    protected_inventory_sha256: str
    protected_inventory_identity: tuple[int, ...]
    candidates: tuple[LegacyCandidate, ...]


@dataclass(frozen=True)
class PromotionDecision:
    query_id: str
    disposition: PromotionDisposition
    reason_codes: tuple[PromotionReasonCode, ...]
    candidate: LegacyCandidate | None


class ProtectedLegacyIntegrityError(RuntimeError):
    def __init__(self, reason_code: PromotionReasonCode) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code.value)


@dataclass(frozen=True)
class _Snapshot:
    contents: bytes
    identity: tuple[int, ...]
    binding: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class _VerifiedRecord:
    candidate: LegacyCandidate
    canonical_query: Mapping[str, object]
    manifest: Mapping[str, object]
    inventory_record: Mapping[str, object]
    payload: bytes


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _trusted_root(root: Path) -> Path:
    supplied = Path(os.path.abspath(os.fspath(root)))
    allowed_system_aliases = {Path("/tmp"), Path("/var")}
    current = Path(supplied.anchor)
    try:
        linked = current.lstat()
        for part in supplied.parts[1:]:
            current /= part
            linked = current.lstat()
            if stat.S_ISLNK(linked.st_mode) and current not in allowed_system_aliases:
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.PROTECTED_PATH_INVALID
                )
    except OSError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PATH_INVALID
        ) from exc
    if stat.S_ISLNK(linked.st_mode) or not stat.S_ISDIR(linked.st_mode):
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.PROTECTED_PATH_INVALID)
    return supplied.resolve(strict=True)


def _exact_path(root: Path, supplied: Path, expected: Path) -> Path:
    candidate = supplied if supplied.is_absolute() else root / supplied
    try:
        linked = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PATH_INVALID
        ) from exc
    if stat.S_ISLNK(linked.st_mode) or resolved != root / expected:
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.PROTECTED_PATH_INVALID)
    return resolved


def _snapshot(root: Path, path: Path) -> _Snapshot:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_PATH_INVALID
        ) from exc
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    leaf_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        filesystem_descriptor = os.open(os.sep, directory_flags)
        descriptors.append(filesystem_descriptor)
        filesystem_identity = _directory_identity(os.fstat(filesystem_descriptor))
        binding = [filesystem_identity]
        directory_bindings: list[tuple[int, str, int, tuple[int, ...]]] = []
        parent_descriptor = filesystem_descriptor

        def open_bound_directory(component: str) -> None:
            nonlocal parent_descriptor
            linked = os.stat(component, dir_fd=parent_descriptor, follow_symlinks=False)
            child_descriptor = os.open(
                component, directory_flags, dir_fd=parent_descriptor
            )
            descriptors.append(child_descriptor)
            opened = os.fstat(child_descriptor)
            if not stat.S_ISDIR(linked.st_mode) or not stat.S_ISDIR(opened.st_mode):
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
                )
            if _directory_identity(linked) != _directory_identity(opened):
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
                )
            expected = _directory_identity(opened)
            binding.append(expected)
            directory_bindings.append(
                (parent_descriptor, component, child_descriptor, expected)
            )
            parent_descriptor = child_descriptor

        for component in root.parts[1:]:
            open_bound_directory(component)
        for component in relative.parts[:-1]:
            open_bound_directory(component)

        leaf_name = relative.name
        linked = os.stat(leaf_name, dir_fd=parent_descriptor, follow_symlinks=False)
        descriptor = os.open(leaf_name, leaf_flags, dir_fd=parent_descriptor)
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
            )
        if _identity(opened) != _identity(linked):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
            )
        chunks = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(leaf_name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _identity(opened) != _identity(after) or _identity(after) != _identity(
            rebound
        ):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
            )
        for parent_fd, name, child_fd, expected in directory_bindings:
            rebound_directory = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _directory_identity(os.fstat(child_fd)) != expected
                or _directory_identity(rebound_directory) != expected
            ):
                raise ProtectedLegacyIntegrityError(
                    PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
                )
        if _directory_identity(os.fstat(filesystem_descriptor)) != filesystem_identity:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
            )
        binding.append(_identity(after))
        return _Snapshot(b"".join(chunks), _identity(after), tuple(binding))
    except ProtectedLegacyIntegrityError:
        raise
    except OSError as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _rebind(root: Path, path: Path, expected: _Snapshot) -> None:
    if _snapshot(root, path) != expected:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )


def _sha(value: object, reason: PromotionReasonCode) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProtectedLegacyIntegrityError(reason)
    return value


def _reason_for_inventory_failure(exc: BaseException) -> PromotionReasonCode:
    message = str(exc).lower()
    if any(
        token in message for token in ("symlink", "hardlink", "regular", "link count")
    ):
        return PromotionReasonCode.LEGACY_LINK_TYPE_INVALID
    if any(token in message for token in ("inode", "identity", "changed")):
        return PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
    if "filename" in message:
        return PromotionReasonCode.LEGACY_FILENAME_INVALID
    if "row_count" in message or "row count" in message:
        return PromotionReasonCode.LEGACY_ROW_COUNT_INVALID
    if "seal" in message or "integrity mismatch" in message:
        return PromotionReasonCode.LEGACY_SEAL_INVALID
    if "raw payload" in message or "raw digest" in message:
        return PromotionReasonCode.LEGACY_RAW_BYTES_INVALID
    if "manifest" in message:
        return PromotionReasonCode.LEGACY_MANIFEST_INVALID
    if any(token in message for token in ("membership", "orphan", "unexpected leaves")):
        return PromotionReasonCode.PROTECTED_MEMBERSHIP_INVALID
    return PromotionReasonCode.PROTECTED_INVENTORY_INVALID


def _assert_inventory_object_identity(
    item: Mapping[str, object], snapshot: _Snapshot
) -> None:
    fingerprint = item.get("inode_fingerprint")
    if (
        not isinstance(fingerprint, Mapping)
        or set(fingerprint) != {"device", "inode"}
        or fingerprint.get("device") != snapshot.identity[0]
        or fingerprint.get("inode") != snapshot.identity[1]
        or item.get("byte_size") != len(snapshot.contents)
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )


def _load_verified_records(
    catalog: ProtectedLegacyCatalog,
) -> tuple[_VerifiedRecord, ...]:
    root = _trusted_root(catalog.repository_root)
    legacy_root = _exact_path(root, root / catalog.legacy_root, _LEGACY_ROOT)
    inventory_path = _exact_path(
        root, root / catalog.protected_inventory_path, _INVENTORY_PATH
    )
    inventory_snapshot = _snapshot(root, inventory_path)
    if inventory_snapshot.identity != catalog.protected_inventory_identity:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )
    if hashlib.sha256(inventory_snapshot.contents).hexdigest() != _sha(
        catalog.protected_inventory_sha256,
        PromotionReasonCode.PROTECTED_INVENTORY_INVALID,
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    try:
        inventory = verify_protected_raw_inventory(root, inventory_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(_reason_for_inventory_failure(exc)) from exc
    _rebind(root, inventory_path, inventory_snapshot)
    if Path(str(inventory.get("legacy_root"))) != _LEGACY_ROOT:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    records = inventory.get("records")
    if not isinstance(records, list):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    verified: list[_VerifiedRecord] = []
    collective_snapshots: list[tuple[Path, _Snapshot]] = [
        (inventory_path, inventory_snapshot)
    ]
    seen: set[tuple[str, bytes]] = set()
    for raw_record in records:
        if not isinstance(raw_record, Mapping):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.PROTECTED_INVENTORY_INVALID
            )
        try:
            source = str(raw_record["source"])
            canonical_query = raw_record["canonical_query"]
            manifest_item = raw_record["manifest"]
            seal_item = raw_record["seal"]
            raw_item = raw_record["raw"]
            if not all(
                isinstance(item, Mapping)
                for item in (manifest_item, seal_item, raw_item)
            ) or not isinstance(canonical_query, Mapping):
                raise TypeError
            manifest_path = _exact_path(
                root,
                root / str(manifest_item["path"]),
                Path(str(manifest_item["path"])),
            )
            seal_path = _exact_path(
                root, root / str(seal_item["path"]), Path(str(seal_item["path"]))
            )
            payload_path = _exact_path(
                root, root / str(raw_item["path"]), Path(str(raw_item["path"]))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.PROTECTED_INVENTORY_INVALID
            ) from exc
        manifest_snapshot = _snapshot(root, manifest_path)
        seal_snapshot = _snapshot(root, seal_path)
        payload_snapshot = _snapshot(root, payload_path)
        _assert_inventory_object_identity(manifest_item, manifest_snapshot)
        _assert_inventory_object_identity(seal_item, seal_snapshot)
        _assert_inventory_object_identity(raw_item, payload_snapshot)
        manifest_sha = _sha(
            manifest_item.get("sha256"), PromotionReasonCode.LEGACY_MANIFEST_INVALID
        )
        seal_sha = _sha(
            seal_item.get("sha256"), PromotionReasonCode.LEGACY_SEAL_INVALID
        )
        raw_sha = _sha(
            raw_item.get("sha256"), PromotionReasonCode.LEGACY_RAW_BYTES_INVALID
        )
        if hashlib.sha256(manifest_snapshot.contents).hexdigest() != manifest_sha:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_MANIFEST_INVALID
            )
        if hashlib.sha256(seal_snapshot.contents).hexdigest() != seal_sha:
            raise ProtectedLegacyIntegrityError(PromotionReasonCode.LEGACY_SEAL_INVALID)
        if seal_snapshot.contents != f"{manifest_sha}\n".encode("ascii"):
            raise ProtectedLegacyIntegrityError(PromotionReasonCode.LEGACY_SEAL_INVALID)
        if hashlib.sha256(payload_snapshot.contents).hexdigest() != raw_sha:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_RAW_BYTES_INVALID
            )
        try:
            manifest = json.loads(manifest_snapshot.contents)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_MANIFEST_INVALID
            ) from exc
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("source") != source
            or manifest.get("query") != canonical_query
            or manifest.get("sha256") != raw_sha
        ):
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_MANIFEST_INVALID
            )
        expected_manifest_name = (
            f"{raw_sha}.{hashlib.sha256(canonical_json_bytes(canonical_query)).hexdigest()}"
            ".manifest.json"
        )
        if manifest_path.name != expected_manifest_name:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_FILENAME_INVALID
            )
        row_count = sum(
            bool(line.strip()) for line in payload_snapshot.contents.splitlines()
        )
        if manifest.get("row_count") != row_count:
            raise ProtectedLegacyIntegrityError(
                PromotionReasonCode.LEGACY_ROW_COUNT_INVALID
            )
        key = (source, canonical_json_bytes(canonical_query))
        if key in seen:
            raise ProtectedLegacyIntegrityError(PromotionReasonCode.CATALOG_NOT_UNIQUE)
        seen.add(key)
        candidate = LegacyCandidate(
            source=source,
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha,
            seal_path=seal_path,
            payload_path=payload_path,
            raw_sha256=raw_sha,
        )
        verified.append(
            _VerifiedRecord(
                candidate=candidate,
                canonical_query=dict(canonical_query),
                manifest=dict(manifest),
                inventory_record=dict(raw_record),
                payload=payload_snapshot.contents,
            )
        )
        collective_snapshots.extend(
            (
                (manifest_path, manifest_snapshot),
                (seal_path, seal_snapshot),
                (payload_path, payload_snapshot),
            )
        )
    for path, snapshot in collective_snapshots:
        _rebind(root, path, snapshot)
    if legacy_root != root / _LEGACY_ROOT:
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.PROTECTED_PATH_INVALID)
    return tuple(
        sorted(verified, key=lambda item: item.candidate.manifest_path.as_posix())
    )


def load_protected_legacy_catalog(
    repository_root: Path,
    *,
    legacy_root: Path,
    protected_inventory_path: Path,
    protected_inventory_sha256: str,
) -> ProtectedLegacyCatalog:
    root = _trusted_root(repository_root)
    _exact_path(root, legacy_root, _LEGACY_ROOT)
    inventory = _exact_path(root, protected_inventory_path, _INVENTORY_PATH)
    _sha(
        protected_inventory_sha256,
        PromotionReasonCode.PROTECTED_INVENTORY_INVALID,
    )
    inventory_snapshot = _snapshot(root, inventory)
    catalog = ProtectedLegacyCatalog(
        repository_root=root,
        legacy_root=_LEGACY_ROOT,
        protected_inventory_path=_INVENTORY_PATH,
        protected_inventory_sha256=protected_inventory_sha256,
        protected_inventory_identity=inventory_snapshot.identity,
        candidates=(),
    )
    records = _load_verified_records(catalog)
    if inventory != root / _INVENTORY_PATH:
        raise ProtectedLegacyIntegrityError(PromotionReasonCode.PROTECTED_PATH_INVALID)
    return ProtectedLegacyCatalog(
        repository_root=root,
        legacy_root=_LEGACY_ROOT,
        protected_inventory_path=_INVENTORY_PATH,
        protected_inventory_sha256=protected_inventory_sha256,
        protected_inventory_identity=inventory_snapshot.identity,
        candidates=tuple(record.candidate for record in records),
    )


def _canonical_day(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _consumer_binding_matches(
    requested: Mapping[str, object],
    *,
    exact_cutoff: str | None = None,
    earliest_cutoff: date | None = None,
) -> bool:
    consumers = requested.get("consuming_game_pks")
    cutoffs = requested.get("consumer_cutoff_dates")
    if (
        not isinstance(consumers, list)
        or not consumers
        or any(
            isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0
            for game_pk in consumers
        )
        or consumers != sorted(set(consumers))
        or not isinstance(cutoffs, Mapping)
        or set(cutoffs) != {str(game_pk) for game_pk in consumers}
    ):
        return False
    for value in cutoffs.values():
        parsed = _canonical_day(value)
        if (
            parsed is None
            or (exact_cutoff is not None and value != exact_cutoff)
            or (earliest_cutoff is not None and parsed < earliest_cutoff)
        ):
            return False
    return True


def _career_baseline_eligible(requested: Mapping[str, object], baseline: str) -> bool:
    baseline_day = _canonical_day(baseline)
    if baseline_day is None or not _consumer_binding_matches(
        requested, earliest_cutoff=baseline_day
    ):
        return False
    cutoffs = requested["consumer_cutoff_dates"]
    if not isinstance(cutoffs, Mapping):
        return False
    boundary = date(2022, 1, 1)
    parsed = tuple(_canonical_day(value) for value in cutoffs.values())
    if any(value is None for value in parsed):
        return False
    if baseline == "2017-12-31":
        return all(value < boundary for value in parsed if value is not None)
    if baseline == "2021-12-31":
        return all(value >= boundary for value in parsed if value is not None)
    return False


def _exact_match(
    query: LogicalQuery,
    record: _VerifiedRecord,
    catalog: ProtectedLegacyCatalog,
) -> bool:
    legacy = record.canonical_query
    if query.parser_version != "v5.2.1":
        return False
    if (
        query.source == "mlb_schedule_game_feed"
        and query.grain == "season_schedule_and_game"
        and query.endpoint == "https://statsapi.mlb.com/api/v1/schedule"
        and record.candidate.source == "mlb_schedule"
    ):
        year = query.requested_range.get("year")
        return (
            set(query.requested_range) == {"year", "start_date", "end_date"}
            and query.expected_media_type == "application/json"
            and query.parent_query_id is None
            and year in {2022, 2023, 2024}
            and query.requested_range.get("start_date")
            == SEASON_BOUNDARIES[year][0].isoformat()
            and query.requested_range.get("end_date")
            == SEASON_BOUNDARIES[year][1].isoformat()
            and legacy.get("role") == "training"
            and legacy.get("gameType") == "R"
            and legacy.get("start") == query.requested_range.get("start_date")
            and legacy.get("end") == query.requested_range.get("end_date")
            and legacy.get("url") == f"{query.endpoint}?{urlencode(query.parameters)}"
            and query.parameters
            == {
                "sportId": 1,
                "gameType": "R",
                "startDate": query.requested_range.get("start_date"),
                "endDate": query.requested_range.get("end_date"),
                "hydrate": "probablePitcher,lineups,venue,linescore",
            }
        )
    if (
        query.source == "baseball_savant_statcast"
        and query.grain == "pitch"
        and query.endpoint == "https://baseballsavant.mlb.com/statcast_search/csv"
        and record.candidate.source == query.source
    ):
        from .statcast import statcast_url
        from .v52_query_plan import validate_statcast_query

        first = query.requested_range.get("start_date")
        last = query.requested_range.get("end_date")
        try:
            validated_first, validated_last = validate_statcast_query(query)
        except (TypeError, ValueError):
            return False
        year = validated_first.year
        season_start, season_end = SEASON_BOUNDARIES[year]
        parent_first = max(season_start, date(year, validated_first.month, 1))
        parent_last = min(
            season_end,
            date(
                year, validated_first.month, monthrange(year, validated_first.month)[1]
            ),
        )
        parent_requested = {
            "year": year,
            "start_date": parent_first.isoformat(),
            "end_date": parent_last.isoformat(),
        }
        parent_parameters = dict(query.parameters)
        parent_parameters["game_date_gt"] = parent_first.isoformat()
        parent_parameters["game_date_lt"] = parent_last.isoformat()
        planned_parent = replace(
            query,
            parameters=parent_parameters,
            requested_range=parent_requested,
            parent_query_id=None,
        )
        return (
            validated_first.year in {2022, 2023, 2024}
            and isinstance(query.parent_query_id, str)
            and bool(_SHA256.fullmatch(query.parent_query_id))
            and query.parent_query_id == logical_query_id(planned_parent)
            and query.requested_range.get("acquisition") == "PROTECTED_LEGACY_REFERENCE"
            and query.requested_range.get("protected_inventory_sha256")
            == catalog.protected_inventory_sha256
            and legacy.get("start") == first
            and legacy.get("end") == last
            and legacy.get("game_type") == "R"
            and legacy.get("url") == statcast_url(str(first), str(last))
        )
    if (
        query.source == "mlb_stats_pitching"
        and query.grain == "season_d1_league_pitching_reference"
        and query.endpoint == "https://statsapi.mlb.com/api/v1/stats"
        and record.candidate.source == query.source
    ):
        season = query.requested_range.get("season")
        start = _canonical_day(query.requested_range.get("start_date"))
        as_of = _canonical_day(query.requested_range.get("as_of_date"))
        return (
            set(query.requested_range)
            == {
                "season",
                "start_date",
                "as_of_date",
                "consuming_game_pks",
                "consumer_cutoff_dates",
                "response_completeness",
            }
            and query.expected_media_type == "application/json"
            and query.parent_query_id is None
            and not isinstance(season, bool)
            and isinstance(season, int)
            and season in {2022, 2023, 2024}
            and start == SEASON_BOUNDARIES[season][0]
            and as_of is not None
            and start <= as_of <= SEASON_BOUNDARIES[season][1]
            and query.requested_range.get("start_date")
            == query.parameters.get("startDate")
            and query.requested_range.get("as_of_date")
            == query.parameters.get("endDate")
            and query.requested_range.get("response_completeness")
            == {
                "limit": 5000,
                "reject_total_splits_at_or_above_limit": True,
            }
            and query.parameters
            == {
                "stats": "byDateRange",
                "group": "pitching",
                "sportIds": 1,
                "gameType": "R",
                "playerPool": "ALL",
                "limit": 5000,
                "startDate": start.isoformat(),
                "endDate": as_of.isoformat(),
            }
            and _consumer_binding_matches(
                query.requested_range,
                exact_cutoff=str(query.requested_range.get("as_of_date")),
            )
            and all(
                legacy.get(key) == query.parameters.get(key)
                for key in (
                    "stats",
                    "group",
                    "sportIds",
                    "gameType",
                    "playerPool",
                    "limit",
                    "startDate",
                    "endDate",
                )
            )
            and (
                legacy.get("url") == f"{query.endpoint}?{urlencode(query.parameters)}"
                and query.requested_range.get("season") in {2022, 2023, 2024}
            )
        )
    if (
        query.source == "mlb_stats_pitcher_bf"
        and query.grain == "player_career_batters_faced_reference"
        and record.candidate.source == query.source
    ):
        baseline = query.requested_range.get("inclusive_end")
        return (
            set(query.requested_range)
            == {
                "pitcher_id",
                "inclusive_end",
                "consuming_game_pks",
                "consumer_cutoff_dates",
            }
            and query.expected_media_type == "application/json"
            and query.parent_query_id is None
            and not isinstance(query.requested_range.get("pitcher_id"), bool)
            and isinstance(query.requested_range.get("pitcher_id"), int)
            and query.requested_range.get("pitcher_id") > 0
            and query.endpoint
            == (
                "https://statsapi.mlb.com/api/v1/people/"
                f"{query.requested_range.get('pitcher_id')}/stats"
            )
            and baseline in {"2017-12-31", "2021-12-31"}
            and query.parameters
            == {
                "stats": "byDateRange",
                "group": "pitching",
                "gameType": "R",
                "sportIds": 1,
                "startDate": "1876-01-01",
                "endDate": baseline,
            }
            and _career_baseline_eligible(query.requested_range, str(baseline))
            and legacy.get("pitcher_id") == query.requested_range.get("pitcher_id")
            and legacy.get("endDate") == query.requested_range.get("inclusive_end")
            and legacy.get("url") == f"{query.endpoint}?{urlencode(query.parameters)}"
            and all(
                legacy.get(key) == query.parameters.get(key)
                for key in (
                    "stats",
                    "group",
                    "sportIds",
                    "gameType",
                    "startDate",
                    "endDate",
                )
            )
        )
    return False


def _decision(
    query: LogicalQuery,
    disposition: PromotionDisposition,
    reason: PromotionReasonCode | None = None,
    candidate: LegacyCandidate | None = None,
) -> PromotionDecision:
    return PromotionDecision(
        query_id=logical_query_id(query),
        disposition=disposition,
        reason_codes=() if reason is None else (reason,),
        candidate=candidate,
    )


def _audit_with_record(
    query: LogicalQuery, catalog: ProtectedLegacyCatalog
) -> tuple[PromotionDecision, _VerifiedRecord | None, Mapping[str, object] | None]:
    try:
        logical_query_id(query)
    except (TypeError, ValueError):
        return (
            PromotionDecision(
                query_id="0" * 64,
                disposition=PromotionDisposition.FATAL_INTEGRITY,
                reason_codes=(PromotionReasonCode.CURRENT_QUERY_INVALID,),
                candidate=None,
            ),
            None,
            None,
        )
    try:
        records = _load_verified_records(catalog)
    except ProtectedLegacyIntegrityError as exc:
        return (
            _decision(query, PromotionDisposition.FATAL_INTEGRITY, exc.reason_code),
            None,
            None,
        )
    verified_candidates = tuple(record.candidate for record in records)
    if catalog.candidates != verified_candidates or len(catalog.candidates) != len(
        set(catalog.candidates)
    ):
        return (
            _decision(
                query,
                PromotionDisposition.FATAL_INTEGRITY,
                PromotionReasonCode.CATALOG_NOT_UNIQUE,
            ),
            None,
            None,
        )
    matches = [record for record in records if _exact_match(query, record, catalog)]
    if len(matches) > 1:
        return (
            _decision(
                query,
                PromotionDisposition.FATAL_INTEGRITY,
                PromotionReasonCode.CATALOG_NOT_UNIQUE,
            ),
            None,
            None,
        )
    if not matches:
        return (
            _decision(
                query,
                PromotionDisposition.FETCH_REQUIRED,
                PromotionReasonCode.NO_EXACT_PROTECTED_CANDIDATE,
            ),
            None,
            None,
        )
    record = matches[0]
    try:
        semantics = recompute_query_sidecar(query, record.payload)
        if (
            not isinstance(semantics, Mapping)
            or not all(isinstance(key, str) for key in semantics)
            or semantics.get("split_disposition") == "SPLIT_REQUIRED"
        ):
            raise ValueError("current semantic contract differs")
        canonical_json_bytes(dict(semantics))
    except (TypeError, ValueError):
        return (
            _decision(
                query,
                PromotionDisposition.FETCH_REQUIRED,
                PromotionReasonCode.CURRENT_SEMANTIC_CONTRACT_UNSATISFIED,
            ),
            None,
            None,
        )
    return (
        _decision(query, PromotionDisposition.PROMOTED, candidate=record.candidate),
        record,
        dict(semantics),
    )


def audit_legacy_candidate(
    query: LogicalQuery, catalog: ProtectedLegacyCatalog
) -> PromotionDecision:
    if not isinstance(query, LogicalQuery):
        raise TypeError("query must be a LogicalQuery")
    if not isinstance(catalog, ProtectedLegacyCatalog):
        raise TypeError("catalog must be a ProtectedLegacyCatalog")
    return _audit_with_record(query, catalog)[0]


def promote_legacy_candidate(
    store: V52EvidenceStore,
    query: LogicalQuery,
    catalog: ProtectedLegacyCatalog,
    decision: PromotionDecision,
) -> PublishedEvidence:
    if not isinstance(store, V52EvidenceStore):
        raise TypeError("store must be a V52EvidenceStore")
    if (
        not isinstance(decision, PromotionDecision)
        or decision.query_id != logical_query_id(query)
        or decision.disposition is not PromotionDisposition.PROMOTED
        or decision.candidate is None
        or decision.reason_codes
    ):
        raise ValueError("promotion decision is not an exact PROMOTED decision")
    current, record, semantics = _audit_with_record(query, catalog)
    if (
        current.disposition is not PromotionDisposition.PROMOTED
        or current.candidate != decision.candidate
        or record is None
        or semantics is None
    ):
        reason = (
            current.reason_codes[0]
            if current.reason_codes
            else PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        )
        raise ProtectedLegacyIntegrityError(reason)
    manifest_item = record.inventory_record["manifest"]
    seal_item = record.inventory_record["seal"]
    raw_item = record.inventory_record["raw"]
    if not all(
        isinstance(item, Mapping) for item in (manifest_item, seal_item, raw_item)
    ):
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.PROTECTED_INVENTORY_INVALID
        )
    reference = {
        "canonical_query": dict(record.canonical_query),
        "legacy_manifest_path": record.candidate.manifest_path.relative_to(
            catalog.repository_root
        ).as_posix(),
        "legacy_manifest_sha256": record.candidate.manifest_sha256,
        "legacy_seal_path": record.candidate.seal_path.relative_to(
            catalog.repository_root
        ).as_posix(),
        "legacy_seal_sha256": seal_item["sha256"],
        "legacy_source": record.candidate.source,
        "parser_version": query.parser_version,
        "payload_path": record.candidate.payload_path.relative_to(
            catalog.repository_root
        ).as_posix(),
        "protected_inventory_path": catalog.protected_inventory_path.as_posix(),
        "protected_inventory_sha256": catalog.protected_inventory_sha256,
        "raw_byte_count": raw_item["byte_size"],
        "raw_row_count": record.manifest["row_count"],
        "raw_sha256": record.candidate.raw_sha256,
        "semantic_sha256": hashlib.sha256(canonical_json_bytes(semantics)).hexdigest(),
        "source": query.source,
    }
    try:
        with store.acquire_query(query):
            return store.publish_legacy_reference(query, reference, semantics)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProtectedLegacyIntegrityError(
            PromotionReasonCode.LEGACY_PATH_IDENTITY_CHANGED
        ) from exc


__all__ = [
    "LegacyCandidate",
    "PromotionDecision",
    "PromotionDisposition",
    "PromotionReasonCode",
    "ProtectedLegacyCatalog",
    "ProtectedLegacyIntegrityError",
    "audit_legacy_candidate",
    "load_protected_legacy_catalog",
    "promote_legacy_candidate",
]
