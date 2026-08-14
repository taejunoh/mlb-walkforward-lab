"""Closed R4 physical-custody inventory and transition receipt artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .secure_fs import (
    DirectorySnapshot,
    RegularFileSnapshot,
    capture_stable_directory_snapshot,
    capture_stable_regular_snapshot,
    open_trusted_root,
    rebind_directory_snapshot,
    rebind_regular_snapshot,
)
from .v52_r4_apfs import (
    CUSTODY_UNAVAILABLE,
    R4DirectoryIdentity,
    capture_directory_identity,
    capture_regular_identity,
    rebind_directory_identity,
)
from .v52_r4_snapshot_issuer import (
    IssuanceOwner, IssuanceState, SnapshotSourceClosure, publish_canonical_json,
)
from .v52_r4_authority import R4SourceAuthorityProfile, production_source_authority_profile
from .v52_source_contracts import canonical_json_bytes


R4_INVENTORY_PATH = Path(
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
    "acquisition/protected_raw_inventory_r4.json"
)
R4_RECEIPT_PATH = Path(
    "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/"
    "acquisition/physical_custody_transition_r4.json"
)
R4_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4"
R4_RUN_SPEC_PATH = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json"
)
LOGICAL_RAW_ROOT = "data/v5_1_bt/raw"
SOURCE_APPROVAL = {
    "logical_raw_root": LOGICAL_RAW_ROOT,
    "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json",
    "protected_inventory_sha256": "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6",
}
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_ROOT_LOCK_NAMES = (
    ".baseball_savant_statcast.lock",
    ".mlb_schedule.lock",
    ".mlb_stats_pitcher_bf.lock",
    ".mlb_stats_pitching.lock",
)
_ROLE_ORDER = ("manifest", "raw", "seal")
_EXPECTED_COUNTS = (1675, 4711, 5025)
_SOURCE_QUERY_KEYS: dict[str, dict[str, type]] = {
    "baseball_savant_statcast": {
        "start": str, "end": str, "game_type": str, "url": str,
    },
    "mlb_schedule": {
        "start": str, "end": str, "gameType": str, "role": str, "url": str,
    },
    "mlb_stats_pitcher_bf": {
        "startDate": str, "endDate": str, "gameType": str, "group": str,
        "stats": str, "url": str, "pitcher_id": int, "sportIds": int,
    },
    "mlb_stats_pitching": {
        "startDate": str, "endDate": str, "gameType": str, "group": str,
        "playerPool": str, "stats": str, "url": str, "limit": int, "sportIds": int,
    },
}
_SOURCE_COUNTS = {
    "baseball_savant_statcast": 213,
    "mlb_schedule": 5,
    "mlb_stats_pitcher_bf": 731,
    "mlb_stats_pitching": 726,
}


@dataclass(frozen=True)
class _PublishedInventory:
    """Private continuation capability bound to one live custody owner."""

    payload_sha256: str
    document: Mapping[str, object]
    repository_root: Path
    repository_identity: DirectorySnapshot
    parent_identity: DirectorySnapshot
    final_snapshot: RegularFileSnapshot
    owner: IssuanceOwner
    profile: R4SourceAuthorityProfile


_PUBLISHED_INVENTORIES: dict[IssuanceOwner, _PublishedInventory] = {}
_CAPTURE_TOKEN = object()
_RUN_SPEC_TOKEN = object()


@dataclass(frozen=True)
class _CapturedCustodySnapshot:
    """Factory-only, owner-bound descriptor capture; never caller-authored maps."""

    owner: IssuanceOwner
    records: tuple[Mapping[str, object], ...]
    members: tuple[Mapping[str, object], ...]
    bindings: tuple[Mapping[str, object], ...]
    root_locks: tuple[Mapping[str, object], ...]
    snapshot: Mapping[str, object]
    ancestor_identities: tuple[Mapping[str, object], ...]
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _CAPTURE_TOKEN:
            raise CUSTODY_UNAVAILABLE("captured custody snapshot is unavailable")


class CapturedCustodySnapshot:
    """Removed public synthetic-input API retained only for clear failure text."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise CUSTODY_UNAVAILABLE("caller-authored custody snapshots are unavailable")


@dataclass(frozen=True)
class _AuthenticatedRunSpec:
    repository_root: Path
    repository_identity: DirectorySnapshot
    parent_identity: DirectorySnapshot
    final_snapshot: RegularFileSnapshot
    sha256: str
    _token: object

    def __post_init__(self) -> None:
        if self._token is not _RUN_SPEC_TOKEN:
            raise CUSTODY_UNAVAILABLE("authenticated RunSpec binding is unavailable")


def render_canonical_document(document: Mapping[str, object]) -> bytes:
    """Render the one canonical artifact representation, including its newline."""
    if not isinstance(document, Mapping):
        raise CUSTODY_UNAVAILABLE("canonical document must be a mapping")
    return canonical_json_bytes(document) + b"\n"


def identity_manifest_sha256(document: Mapping[str, object]) -> str:
    try:
        selected = {
            key: document[key]
            for key in (
                "snapshot", "records", "members", "bindings", "root_locks",
                "ancestor_identities",
            )
        }
    except (KeyError, TypeError) as exc:
        raise CUSTODY_UNAVAILABLE("identity manifest fields are absent") from exc
    return hashlib.sha256(canonical_json_bytes(selected)).hexdigest()


def _closed(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise CUSTODY_UNAVAILABLE(f"{label} is not a closed mapping")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    return value


def _volume_uuid(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 32 or any(ch not in "0123456789abcdef" for ch in value):
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    return value


def _strict_descendant(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    path = PurePosixPath(value)
    root = PurePosixPath(LOGICAL_RAW_ROOT)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    try:
        suffix = path.relative_to(root)
    except ValueError as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} is outside logical raw root") from exc
    if not suffix.parts or len(suffix.parts) != 2:
        raise CUSTODY_UNAVAILABLE(f"{label} is not a canonical physical leaf")
    return value


def _snapshot_relative_directory(value: object, label: str) -> str:
    if value == ".":
        return "."
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    return value


def _validate_object_identity(value: object, label: str) -> Mapping[str, object]:
    item = _closed(value, {"volume_uuid", "inode", "ctime_ns", "file_type", "nlink", "byte_size", "sha256", "mode"}, label)
    _volume_uuid(item["volume_uuid"], f"{label} volume UUID")
    _integer(item["inode"], f"{label} inode", minimum=1)
    _integer(item["ctime_ns"], f"{label} ctime")
    if item["file_type"] != "regular":
        raise CUSTODY_UNAVAILABLE(f"{label} type differs")
    if _integer(item["nlink"], f"{label} link count", minimum=1) != 1:
        raise CUSTODY_UNAVAILABLE(f"{label} hard link differs")
    _integer(item["byte_size"], f"{label} byte size")
    _sha256(item["sha256"], f"{label} sha256")
    if _integer(item["mode"], f"{label} mode") != 0o444:
        raise CUSTODY_UNAVAILABLE(f"{label} mode differs")
    return item


def _validate_directory_identity(value: object, label: str) -> Mapping[str, object]:
    item = _closed(value, {"volume_uuid", "inode", "ctime_ns", "file_type", "nlink", "mode"}, label)
    _volume_uuid(item["volume_uuid"], f"{label} volume UUID")
    _integer(item["inode"], f"{label} inode", minimum=1)
    _integer(item["ctime_ns"], f"{label} ctime")
    if item["file_type"] != "directory":
        raise CUSTODY_UNAVAILABLE(f"{label} type differs")
    _integer(item["nlink"], f"{label} link count", minimum=1)
    if _integer(item["mode"], f"{label} mode") != 0o555:
        raise CUSTODY_UNAVAILABLE(f"{label} mode differs")
    return item


def _profile(profile: R4SourceAuthorityProfile | None) -> R4SourceAuthorityProfile:
    return profile or production_source_authority_profile()


def _validate_source_approval(value: object, profile: R4SourceAuthorityProfile) -> None:
    if dict(_closed(value, set(SOURCE_APPROVAL), "source approval")) != dict(profile.source_approval):
        raise CUSTODY_UNAVAILABLE("source approval differs")


def _source_record(record: Mapping[str, object]) -> dict[str, object]:
    return {
        "source": record["source"],
        "canonical_query": dict(record["canonical_query"]),  # type: ignore[arg-type]
        "rawstore_verified": record["rawstore_verified"],
    }


def _approved_source_records(profile: R4SourceAuthorityProfile) -> tuple[Mapping[str, object], ...]:
    """Use the transaction's already validated profile; never reread a default."""
    return tuple(profile.closure.records)


def _validate_records(records: object, profile: R4SourceAuthorityProfile) -> list[Mapping[str, object]]:
    if not isinstance(records, list) or len(records) != _EXPECTED_COUNTS[0]:
        raise CUSTODY_UNAVAILABLE("record count differs")
    expected = _approved_source_records(profile)
    if len(expected) != len(records):
        raise CUSTODY_UNAVAILABLE("approved source record count differs")
    output: list[Mapping[str, object]] = []
    counts: Counter[str] = Counter()
    for index, item in enumerate(records):
        record = _closed(item, {"source", "canonical_query", "rawstore_verified"}, "record")
        source = record["source"]
        if source not in _SOURCE_QUERY_KEYS or record["rawstore_verified"] is not True:
            raise CUSTODY_UNAVAILABLE("record source metadata differs")
        query = _closed(record["canonical_query"], set(_SOURCE_QUERY_KEYS[source]), "canonical query")
        for key, kind in _SOURCE_QUERY_KEYS[source].items():
            value = query[key]
            if kind is int:
                _integer(value, f"canonical query {key}")
            elif not isinstance(value, str):
                raise CUSTODY_UNAVAILABLE(f"canonical query {key} is malformed")
        if _source_record(expected[index]) != dict(record):
            raise CUSTODY_UNAVAILABLE("records differ from approved source manifest order")
        output.append(record)
        counts[source] += 1
    if dict(counts) != _SOURCE_COUNTS:
        raise CUSTODY_UNAVAILABLE("source branch counts differ")
    return output


def _source_artifacts(profile: R4SourceAuthorityProfile) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    paths: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    for index, record in enumerate(_approved_source_records(profile)):
        if not isinstance(record, Mapping):
            raise CUSTODY_UNAVAILABLE("approved source record is malformed")
        for role in _ROLE_ORDER:
            artifact = record.get(role)
            if not isinstance(artifact, Mapping):
                raise CUSTODY_UNAVAILABLE("approved source artifact is malformed")
            path = _strict_descendant(artifact.get("path"), "approved source artifact path")
            paths.append({"path": path, "byte_size": artifact.get("byte_size"), "sha256": artifact.get("sha256")})
            bindings.append({"record_index": index, "role": role, "path": path})
    return paths, bindings


def _validate_members(members: object, profile: R4SourceAuthorityProfile) -> dict[str, Mapping[str, object]]:
    if not isinstance(members, list) or len(members) != _EXPECTED_COUNTS[1]:
        raise CUSTODY_UNAVAILABLE("physical member count differs")
    result: dict[str, Mapping[str, object]] = {}
    expected_paths, _ = _source_artifacts(profile)
    expected = {item["path"]: item for item in expected_paths}
    previous = ""
    for member in members:
        item = _closed(member, {"path", "volume_uuid", "inode", "ctime_ns", "file_type", "nlink", "byte_size", "sha256", "mode"}, "member")
        path = _strict_descendant(item["path"], "member path")
        if path <= previous or path in result:
            raise CUSTODY_UNAVAILABLE("members are not sorted unique")
        previous = path
        identity = _validate_object_identity({key: item[key] for key in item if key != "path"}, "member")
        source = PurePosixPath(path).relative_to(LOGICAL_RAW_ROOT).parts[0]
        if source not in _SOURCE_QUERY_KEYS:
            raise CUSTODY_UNAVAILABLE("member source differs")
        if path not in expected or item["byte_size"] != expected[path]["byte_size"] or item["sha256"] != expected[path]["sha256"]:
            raise CUSTODY_UNAVAILABLE("member differs from approved source artifact")
        result[path] = identity
    if set(result) != set(expected):
        raise CUSTODY_UNAVAILABLE("member path closure differs")
    return result


def _validate_bindings(bindings: object, members: Mapping[str, Mapping[str, object]], profile: R4SourceAuthorityProfile) -> None:
    if not isinstance(bindings, list) or len(bindings) != _EXPECTED_COUNTS[2]:
        raise CUSTODY_UNAVAILABLE("logical binding count differs")
    _paths, expected = _source_artifacts(profile)
    if bindings != expected:
        raise CUSTODY_UNAVAILABLE("bindings differ from approved source closure or order")
    seen_roles: set[tuple[int, str]] = set()
    seen_unique: dict[str, set[str]] = {"manifest": set(), "seal": set()}
    raw_counts: Counter[str] = Counter()
    for item in bindings:
        binding = _closed(item, {"record_index", "role", "path"}, "binding")
        record_index = _integer(binding["record_index"], "binding record index")
        if record_index >= _EXPECTED_COUNTS[0] or binding["role"] not in _ROLE_ORDER:
            raise CUSTODY_UNAVAILABLE("binding coordinate differs")
        path = _strict_descendant(binding["path"], "binding path")
        if path not in members:
            raise CUSTODY_UNAVAILABLE("binding path is not a member")
        coordinate = (record_index, binding["role"])
        if coordinate in seen_roles:
            raise CUSTODY_UNAVAILABLE("binding role repeats")
        seen_roles.add(coordinate)
        if binding["role"] in seen_unique:
            if path in seen_unique[binding["role"]]:
                raise CUSTODY_UNAVAILABLE("non-raw binding path repeats")
            seen_unique[binding["role"]].add(path)
        else:
            raw_counts[path] += 1
    if len(seen_roles) != _EXPECTED_COUNTS[2]:
        raise CUSTODY_UNAVAILABLE("binding coordinate closure differs")
    # The source-derived reference vector, rather than a duplicate policy, is
    # authoritative: the approved source inventory contributes this exact map.
    expected_raw_counts = Counter(row["path"] for row in expected if row["role"] == "raw")
    if raw_counts != expected_raw_counts:
        raise CUSTODY_UNAVAILABLE("shared raw closure differs")


def _validate_root_locks(locks: object, volume_uuid: str) -> None:
    if not isinstance(locks, list) or len(locks) != 4:
        raise CUSTODY_UNAVAILABLE("root lock count differs")
    previous = ""
    for lock, expected_name in zip(locks, _ROOT_LOCK_NAMES):
        item = _closed(lock, {"path", "volume_uuid", "inode", "ctime_ns", "file_type", "nlink", "byte_size", "sha256", "mode"}, "root lock")
        if item["path"] != expected_name or item["path"] <= previous:
            raise CUSTODY_UNAVAILABLE("root locks are not the exact sorted set")
        previous = expected_name
        _validate_object_identity({key: item[key] for key in item if key != "path"}, "root lock")
        if item["volume_uuid"] != volume_uuid or item["nlink"] != 1 or item["byte_size"] != 0 or item["sha256"] != _EMPTY_SHA256:
            raise CUSTODY_UNAVAILABLE("root lock custody identity differs")


def _validate_snapshot(snapshot: object, locks: list[object]) -> Mapping[str, object]:
    item = _closed(snapshot, {"volume_uuid", "root_identity", "manifest_count", "physical_member_count", "logical_binding_count", "root_locks_sha256"}, "snapshot")
    volume_uuid = _volume_uuid(item["volume_uuid"], "snapshot volume UUID")
    root = _validate_directory_identity(item["root_identity"], "snapshot root identity")
    if root["volume_uuid"] != volume_uuid:
        raise CUSTODY_UNAVAILABLE("snapshot root volume differs")
    if (item["manifest_count"], item["physical_member_count"], item["logical_binding_count"]) != _EXPECTED_COUNTS:
        raise CUSTODY_UNAVAILABLE("snapshot counts differ")
    _sha256(item["root_locks_sha256"], "snapshot root locks sha256")
    if item["root_locks_sha256"] != hashlib.sha256(canonical_json_bytes(locks)).hexdigest():
        raise CUSTODY_UNAVAILABLE("snapshot root locks digest differs")
    return item


def _validate_ancestors(ancestors: object, snapshot: Mapping[str, object], members: Mapping[str, Mapping[str, object]]) -> None:
    if not isinstance(ancestors, list) or not ancestors:
        raise CUSTODY_UNAVAILABLE("ancestor identities are absent")
    expected_paths = {"."}
    for path in members:
        expected_paths.add(PurePosixPath(path).relative_to(LOGICAL_RAW_ROOT).parts[0])
    previous_key: tuple[int, str] | None = None
    actual_paths: set[str] = set()
    for item in ancestors:
        ancestor = _closed(item, {"path", "volume_uuid", "inode", "ctime_ns", "file_type", "nlink", "mode"}, "ancestor identity")
        path = _snapshot_relative_directory(ancestor["path"], "ancestor path")
        order_key = (0 if path == "." else len(PurePosixPath(path).parts), path)
        if previous_key is not None and order_key <= previous_key:
            raise CUSTODY_UNAVAILABLE("ancestor identities are not sorted")
        previous_key = order_key
        identity = _validate_directory_identity({key: ancestor[key] for key in ancestor if key != "path"}, "ancestor identity")
        if identity["volume_uuid"] != snapshot["volume_uuid"]:
            raise CUSTODY_UNAVAILABLE("ancestor volume differs")
        actual_paths.add(path)
        if path == "." and dict(identity) != dict(snapshot["root_identity"]):
            raise CUSTODY_UNAVAILABLE("root ancestor identity differs")
    if not ancestors or ancestors[0]["path"] != "." or actual_paths != expected_paths:
        raise CUSTODY_UNAVAILABLE("ancestor closure differs")


def validate_r4_inventory(
    document: Mapping[str, object], *, profile: R4SourceAuthorityProfile | None = None
) -> None:
    authority = _profile(profile)
    keys = {
        "schema_version", "inventory_kind", "logical_raw_root", "source_approval", "snapshot",
        "manifest_count", "physical_member_count", "logical_binding_count", "rawstore_verification",
        "records", "members", "bindings", "root_locks", "ancestor_identities",
        "custody_identity_manifest_sha256",
    }
    doc = _closed(document, keys, "R4 inventory")
    if doc["schema_version"] != 1 or doc["inventory_kind"] != "v5_2_r4_physical_custody" or doc["logical_raw_root"] != LOGICAL_RAW_ROOT or doc["rawstore_verification"] != "PASS":
        raise CUSTODY_UNAVAILABLE("R4 inventory constants differ")
    _validate_source_approval(doc["source_approval"], authority)
    if (doc["manifest_count"], doc["physical_member_count"], doc["logical_binding_count"]) != _EXPECTED_COUNTS:
        raise CUSTODY_UNAVAILABLE("R4 inventory counts differ")
    _validate_records(doc["records"], authority)
    members = _validate_members(doc["members"], authority)
    _validate_bindings(doc["bindings"], members, authority)
    snapshot = _validate_snapshot(doc["snapshot"], doc["root_locks"])
    if any(member["volume_uuid"] != snapshot["volume_uuid"] for member in members.values()):
        raise CUSTODY_UNAVAILABLE("member volume differs")
    _validate_root_locks(doc["root_locks"], snapshot["volume_uuid"])
    _validate_ancestors(doc["ancestor_identities"], snapshot, members)
    if doc["custody_identity_manifest_sha256"] != identity_manifest_sha256(doc):
        raise CUSTODY_UNAVAILABLE("custody identity manifest digest differs")


def verify_frozen_r4_snapshot(
    custody_root: Path, inventory: Mapping[str, object],
) -> None:
    """Descriptor-rehash the live snapshot against an already trusted inventory.

    This intentionally consumes the signed inventory only as expected physical
    evidence; it never reads an output raw source tree or reconstructs source
    authority from the issued JSON.
    """
    if not isinstance(inventory, Mapping):
        raise CUSTODY_UNAVAILABLE("R4 inventory is unavailable")
    try:
        snapshot = inventory["snapshot"]
        members = inventory["members"]
        locks = inventory["root_locks"]
        ancestors = inventory["ancestor_identities"]
    except KeyError as exc:
        raise CUSTODY_UNAVAILABLE("R4 inventory is unavailable") from exc
    if not isinstance(snapshot, Mapping) or not isinstance(members, list) or not isinstance(locks, list) or not isinstance(ancestors, list):
        raise CUSTODY_UNAVAILABLE("R4 inventory is unavailable")
    expected_members = {
        item.get("path"): item for item in members if isinstance(item, Mapping)
    }
    expected_locks = {
        item.get("path"): item for item in locks if isinstance(item, Mapping)
    }
    expected_ancestors = {
        item.get("path"): item for item in ancestors if isinstance(item, Mapping)
    }
    if len(expected_members) != _EXPECTED_COUNTS[1] or set(expected_locks) != set(_ROOT_LOCK_NAMES):
        raise CUSTODY_UNAVAILABLE("R4 inventory is unavailable")
    root_fd = -1
    try:
        with open_trusted_root(Path(custody_root), nonblocking=True) as root:
            root_fd = root.dup_root_fd()
            root_identity = capture_directory_identity(root_fd)
            _require_immutable_descriptor(root_fd, directory=True)
            root_expected = snapshot.get("root_identity")
            if root_expected != root_identity.__dict__:
                raise CUSTODY_UNAVAILABLE("R4 snapshot root identity differs")
            expected_leaves = {
                PurePosixPath(path).relative_to(LOGICAL_RAW_ROOT)
                for path in expected_members if isinstance(path, str)
            } | {PurePosixPath(name) for name in _ROOT_LOCK_NAMES}
            if _frozen_snapshot_leaves(root_fd) != expected_leaves:
                raise CUSTODY_UNAVAILABLE("R4 snapshot closure differs")
            for logical_path, expected in sorted(expected_members.items()):
                if not isinstance(logical_path, str) or not isinstance(expected, Mapping):
                    raise CUSTODY_UNAVAILABLE("R4 snapshot member differs")
                fd = _open_snapshot_relative(
                    root_fd, PurePosixPath(logical_path).relative_to(LOGICAL_RAW_ROOT), directory=False
                )
                try:
                    observed = _hash_and_capture_regular(fd, logical_path)
                    if observed != dict(expected):
                        raise CUSTODY_UNAVAILABLE(
                            "R4 snapshot member differs: "
                            f"{logical_path} ({_identity_difference(dict(expected), observed)})"
                        )
                finally:
                    os.close(fd)
            for name, expected in expected_locks.items():
                if not isinstance(name, str) or not isinstance(expected, Mapping):
                    raise CUSTODY_UNAVAILABLE("R4 snapshot root lock differs")
                fd = _open_snapshot_relative(root_fd, PurePosixPath(name), directory=False)
                try:
                    observed_lock = _hash_and_capture_regular(fd, name)
                    if observed_lock != dict(expected):
                        raise CUSTODY_UNAVAILABLE(
                            "R4 snapshot root lock differs: "
                            f"{name} ({_identity_difference(dict(expected), observed_lock)})"
                        )
                finally:
                    os.close(fd)
            for path, expected in expected_ancestors.items():
                if not isinstance(path, str) or not isinstance(expected, Mapping):
                    raise CUSTODY_UNAVAILABLE("R4 snapshot ancestor differs")
                fd = root_fd if path == "." else _open_snapshot_relative(
                    root_fd, PurePosixPath(path), directory=True
                )
                try:
                    if _directory_row(path, capture_directory_identity(fd)) != dict(expected):
                        raise CUSTODY_UNAVAILABLE("R4 snapshot ancestor differs")
                finally:
                    if fd != root_fd:
                        os.close(fd)
            rebind_directory_identity(root_fd, root_identity)
            root.assert_bound()
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("R4 snapshot cannot be verified") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def _default_snapshot(members: Sequence[Mapping[str, object]], locks: Sequence[Mapping[str, object]]) -> dict[str, object]:
    volume_uuid = str(members[0]["volume_uuid"])
    root_identity = {"volume_uuid": volume_uuid, "inode": 1, "ctime_ns": 0, "file_type": "directory", "nlink": 1, "mode": 0o555}
    return {
        "volume_uuid": volume_uuid, "root_identity": root_identity,
        "manifest_count": 1675, "physical_member_count": 4711, "logical_binding_count": 5025,
        "root_locks_sha256": hashlib.sha256(canonical_json_bytes(list(locks))).hexdigest(),
    }


def _default_ancestors(snapshot: Mapping[str, object], members: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    root = dict(snapshot["root_identity"])
    result = [{"path": ".", **root}]
    for offset, source in enumerate(sorted({PurePosixPath(str(member["path"])).relative_to(LOGICAL_RAW_ROOT).parts[0] for member in members}), start=2):
        result.append({"path": source, "volume_uuid": snapshot["volume_uuid"], "inode": offset, "ctime_ns": 0, "file_type": "directory", "nlink": 1, "mode": 0o555})
    return result


def _directory_row(path: str, identity: R4DirectoryIdentity) -> dict[str, object]:
    return {"path": path, **identity.__dict__}


def _object_row(path: str, identity: object) -> dict[str, object]:
    if not hasattr(identity, "__dict__"):
        raise CUSTODY_UNAVAILABLE("captured custody identity is malformed")
    return {"path": path, **identity.__dict__}  # type: ignore[union-attr]


def _identity_difference(expected: Mapping[str, object], observed: Mapping[str, object]) -> str:
    """Render metadata-only identity drift for a fail-closed custody audit."""
    differences = []
    for key in sorted(set(expected) | set(observed)):
        if expected.get(key) != observed.get(key):
            differences.append(f"{key}={expected.get(key)!r}->{observed.get(key)!r}")
    return ", ".join(differences) or "unknown"


def _open_snapshot_relative(root_fd: int, relative: PurePosixPath, *, directory: bool) -> int:
    descriptors = [os.dup(root_fd)]
    try:
        for index, part in enumerate(relative.parts):
            last = index == len(relative.parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
            if directory or not last:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=descriptors[-1])
            named = os.stat(part, dir_fd=descriptors[-1], follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (named.st_dev, named.st_ino, named.st_ctime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_ctime_ns
            ):
                os.close(descriptor)
                raise CUSTODY_UNAVAILABLE("frozen snapshot path changed")
            descriptors.append(descriptor)
        result = descriptors.pop()
        return result
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("frozen snapshot path is unavailable") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_immutable_descriptor(fd: int, *, directory: bool) -> None:
    try:
        observed = os.fstat(fd)
        flags = observed.st_flags
    except (AttributeError, OSError) as exc:
        raise CUSTODY_UNAVAILABLE("frozen snapshot immutable flag is unavailable") from exc
    if not flags & getattr(stat, "UF_IMMUTABLE", 0x00000002):
        raise CUSTODY_UNAVAILABLE("frozen snapshot immutable flag differs")
    if directory:
        if not stat.S_ISDIR(observed.st_mode) or stat.S_IMODE(observed.st_mode) != 0o555:
            raise CUSTODY_UNAVAILABLE("frozen snapshot directory differs")
    elif (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or stat.S_IMODE(observed.st_mode) != 0o444
    ):
        raise CUSTODY_UNAVAILABLE("frozen snapshot member differs")


def _hash_and_capture_regular(fd: int, path: str) -> dict[str, object]:
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    identity = capture_regular_identity(fd, digest.hexdigest())
    _require_immutable_descriptor(fd, directory=False)
    return _object_row(path, identity)


def _frozen_snapshot_leaves(directory_fd: int, prefix: PurePosixPath = PurePosixPath(".")) -> set[PurePosixPath]:
    """Enumerate the immutable, no-follow snapshot closure before capture."""
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise CUSTODY_UNAVAILABLE("frozen snapshot cannot be enumerated") from exc
    leaves: set[PurePosixPath] = set()
    for name in names:
        child_fd = -1
        try:
            observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(observed.st_mode):
                child_fd = _open_snapshot_relative(directory_fd, PurePosixPath(name), directory=True)
                _require_immutable_descriptor(child_fd, directory=True)
                leaves.update(_frozen_snapshot_leaves(child_fd, prefix / name))
            elif stat.S_ISREG(observed.st_mode):
                child_fd = _open_snapshot_relative(directory_fd, PurePosixPath(name), directory=False)
                _require_immutable_descriptor(child_fd, directory=False)
                leaves.add(prefix / name)
            else:
                raise CUSTODY_UNAVAILABLE("frozen snapshot member is unsafe")
        except OSError as exc:
            raise CUSTODY_UNAVAILABLE("frozen snapshot member is unavailable") from exc
        finally:
            if child_fd >= 0:
                os.close(child_fd)
    return leaves


def _capture_frozen_snapshot_once(
    owner: IssuanceOwner, *, repository_root: Path, custody_parent: Path
) -> _CapturedCustodySnapshot:
    """Capture the sole frozen owner snapshot through no-follow descriptors."""
    root_fd = -1
    _require_snapshot_owner(owner, repository_root, custody_parent)
    try:
        closure = owner._bound_closure
    except AttributeError as exc:
        raise CUSTODY_UNAVAILABLE("frozen snapshot closure is unavailable") from exc
    if not isinstance(closure, SnapshotSourceClosure):
        raise CUSTODY_UNAVAILABLE("frozen snapshot has no production source closure")
    profile = getattr(owner, "_source_authority_profile", None)
    if not isinstance(profile, R4SourceAuthorityProfile):
        raise CUSTODY_UNAVAILABLE("frozen snapshot source authority is unavailable")
    try:
        # The owner-held closure was produced only after a descriptor-authenticated
        # read of the approved source inventory.  Verify the exact public
        # closure again so a mutable Python object cannot smuggle records in.
        expected_records = _approved_source_records(profile)
        if tuple(closure.records) != expected_records:
            raise CUSTODY_UNAVAILABLE("frozen snapshot source closure differs")
        root_fd = _open_snapshot_relative(
            owner.parent_fd, PurePosixPath(owner.final_name), directory=True
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("frozen snapshot capture is malformed") from exc
    try:
        root_identity = capture_directory_identity(root_fd)
        _require_immutable_descriptor(root_fd, directory=True)
        members: list[Mapping[str, object]] = []
        approved_members = {
            str(artifact["path"]): artifact for artifact in _source_artifacts(profile)[0]
        }
        expected_leaves = {
            PurePosixPath(path).relative_to(LOGICAL_RAW_ROOT)
            for path in approved_members
        } | {PurePosixPath(name) for name in _ROOT_LOCK_NAMES}
        if _frozen_snapshot_leaves(root_fd) != expected_leaves:
            raise CUSTODY_UNAVAILABLE("frozen snapshot closure differs")
        for logical_path, artifact in sorted(approved_members.items()):
            suffix = PurePosixPath(logical_path).relative_to(LOGICAL_RAW_ROOT)
            member_fd = _open_snapshot_relative(root_fd, suffix, directory=False)
            try:
                row = _hash_and_capture_regular(member_fd, logical_path)
            finally:
                os.close(member_fd)
            if row["byte_size"] != artifact["byte_size"] or row["sha256"] != artifact["sha256"]:
                raise CUSTODY_UNAVAILABLE("frozen snapshot member differs from approved closure")
            members.append(row)
        locks: list[Mapping[str, object]] = []
        for name in _ROOT_LOCK_NAMES:
            lock_fd = _open_snapshot_relative(root_fd, PurePosixPath(name), directory=False)
            try:
                locks.append(_hash_and_capture_regular(lock_fd, name))
            finally:
                os.close(lock_fd)
        if any(row["byte_size"] != 0 or row["sha256"] != _EMPTY_SHA256 for row in locks):
            raise CUSTODY_UNAVAILABLE("frozen snapshot root lock differs")
        ancestors = [_directory_row(".", root_identity)]
        for source in sorted(_SOURCE_QUERY_KEYS):
            source_fd = _open_snapshot_relative(root_fd, PurePosixPath(source), directory=True)
            try:
                source_identity = capture_directory_identity(source_fd)
                _require_immutable_descriptor(source_fd, directory=True)
                ancestors.append(_directory_row(source, source_identity))
            finally:
                os.close(source_fd)
        # Rebind the root after every member/hash capture; this detects final
        # name swaps and root metadata mutation before a capability is issued.
        rebind_directory_identity(root_fd, root_identity)
        snapshot = {
            "volume_uuid": root_identity.volume_uuid,
            "root_identity": root_identity.__dict__,
            "manifest_count": 1675,
            "physical_member_count": 4711,
            "logical_binding_count": 5025,
            "root_locks_sha256": hashlib.sha256(canonical_json_bytes(locks)).hexdigest(),
        }
        _require_snapshot_owner(owner, repository_root, custody_parent)
        return _CapturedCustodySnapshot(
            owner=owner,
            records=tuple(_source_record(record) for record in expected_records),
            members=tuple(members),
            bindings=tuple(_source_artifacts(profile)[1]),
            root_locks=tuple(locks),
            snapshot=snapshot,
            ancestor_identities=tuple(ancestors),
            _token=_CAPTURE_TOKEN,
        )
    finally:
        if root_fd >= 0:
            os.close(root_fd)


def capture_frozen_snapshot(
    owner: IssuanceOwner, *, repository_root: Path, custody_parent: Path
) -> _CapturedCustodySnapshot:
    """Capture only a settled exact APFS identity manifest before signing.

    Darwin may publish a delayed ctime update after `fchflags(UF_IMMUTABLE)`.
    Rather than weakening verification, capture the complete immutable
    namespace three times through fresh no-follow descriptors and sign only
    after three identical consecutive manifests.  A continuing transition is
    fail-closed.
    """
    previous: _CapturedCustodySnapshot | None = None
    consecutive_equal = 0
    last_difference = "capture"
    # APFS can defer a whole-directory immutable ctime transition beyond a
    # complete first capture.  The sleep only spaces independent descriptor
    # observations; acceptance still requires two *exact* full manifests.
    # The finite six-second budget fails closed for a continuing transition.
    for attempt in range(30):
        if attempt:
            time.sleep(0.2)
        try:
            current = _capture_frozen_snapshot_once(
                owner, repository_root=repository_root, custody_parent=custody_parent
            )
        except CUSTODY_UNAVAILABLE:
            # A full capture can overlap APFS's delayed immutable metadata
            # commit.  It is never accepted; a later complete pair is needed.
            previous = None
            consecutive_equal = 0
            last_difference = "capture unavailable"
            continue
        if previous is None:
            previous = current
            continue
        differing = tuple(
            name for name in ("snapshot", "members", "root_locks", "ancestor_identities")
            if getattr(previous, name) != getattr(current, name)
        )
        if not differing:
            consecutive_equal += 1
            if consecutive_equal >= 2:
                return current
        else:
            last_difference = ",".join(differing)
            consecutive_equal = 0
        previous = current
    raise CUSTODY_UNAVAILABLE(
        f"frozen snapshot APFS identity did not settle ({last_difference})"
    )


def build_r4_inventory(
    captured: _CapturedCustodySnapshot, *, profile: R4SourceAuthorityProfile | None = None
) -> dict[str, object]:
    authority = _profile(profile)
    if not isinstance(captured, _CapturedCustodySnapshot) or captured._token is not _CAPTURE_TOKEN:
        raise CUSTODY_UNAVAILABLE("captured custody snapshot is malformed")
    records = [_source_record(record) for record in captured.records]
    members = [dict(member) for member in captured.members]
    bindings = [dict(binding) for binding in captured.bindings]
    locks = [dict(lock) for lock in captured.root_locks]
    snapshot = dict(captured.snapshot) if captured.snapshot is not None else _default_snapshot(members, locks)
    ancestors = [dict(item) for item in captured.ancestor_identities] or _default_ancestors(snapshot, members)
    document: dict[str, object] = {
        "schema_version": 1, "inventory_kind": "v5_2_r4_physical_custody", "logical_raw_root": LOGICAL_RAW_ROOT,
        "source_approval": dict(authority.source_approval), "snapshot": snapshot,
        "manifest_count": 1675, "physical_member_count": 4711, "logical_binding_count": 5025,
        "rawstore_verification": "PASS", "records": records, "members": members,
        "bindings": bindings, "root_locks": locks, "ancestor_identities": ancestors,
    }
    document["custody_identity_manifest_sha256"] = identity_manifest_sha256(document)
    validate_r4_inventory(document, profile=authority)
    return document


def _captured_snapshot_fixture(
    records: Sequence[Mapping[str, object]], members: Sequence[Mapping[str, object]],
    bindings: Sequence[Mapping[str, object]], root_locks: Sequence[Mapping[str, object]],
    *, snapshot: Mapping[str, object] | None = None,
    ancestor_identities: Sequence[Mapping[str, object]] = (),
) -> _CapturedCustodySnapshot:
    """Private test-only fixture seam; never used by publication APIs."""
    safe_members = tuple(dict(item) for item in members)
    safe_locks = tuple(dict(item) for item in root_locks)
    safe_snapshot = dict(snapshot) if snapshot is not None else _default_snapshot(safe_members, safe_locks)
    safe_ancestors = tuple(dict(item) for item in ancestor_identities) or tuple(
        _default_ancestors(safe_snapshot, safe_members)
    )
    return _CapturedCustodySnapshot(
        owner=None,  # type: ignore[arg-type]
        records=tuple(dict(item) for item in records),
        members=safe_members,
        bindings=tuple(dict(item) for item in bindings),
        root_locks=safe_locks,
        snapshot=safe_snapshot,
        ancestor_identities=safe_ancestors,
        _token=_CAPTURE_TOKEN,
    )


def _content_manifest_sha256(rows: list[dict[str, object]]) -> str:
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def _binding_manifest_rows(inventory: Mapping[str, object]) -> list[dict[str, object]]:
    members = {member["path"]: member for member in inventory["members"]}  # type: ignore[index]
    return [
        {"record_index": binding["record_index"], "role": binding["role"], "path": binding["path"], "byte_size": members[binding["path"]]["byte_size"], "sha256": members[binding["path"]]["sha256"]}
        for binding in inventory["bindings"]  # type: ignore[index]
    ]


def build_transition_receipt(
    inventory: Mapping[str, object], *, run_spec: _AuthenticatedRunSpec,
    profile: R4SourceAuthorityProfile | None = None,
) -> dict[str, object]:
    authority = _profile(profile)
    validate_r4_inventory(inventory, profile=authority)
    if not isinstance(run_spec, _AuthenticatedRunSpec) or run_spec._token is not _RUN_SPEC_TOKEN:
        raise CUSTODY_UNAVAILABLE("authenticated RunSpec binding is malformed")
    if run_spec.repository_identity.identity != (0, 0, 0, 0, 0, 0):
        _rebind_run_spec_binding(run_spec)
    objects = [
        {"path": member["path"], "byte_size": member["byte_size"], "source_sha256": member["sha256"], "snapshot_sha256": member["sha256"]}
        for member in inventory["members"]  # type: ignore[index]
    ]
    content_rows = [{"path": item["path"], "byte_size": item["byte_size"], "sha256": item["snapshot_sha256"]} for item in objects]
    binding_digest = _content_manifest_sha256(_binding_manifest_rows(inventory))
    receipt: dict[str, object] = {
        "schema_version": 1, "receipt_kind": "R4_PHYSICAL_CUSTODY_TRANSITION", "run_id": R4_RUN_ID,
        "run_spec": {"path": R4_RUN_SPEC_PATH.as_posix(), "sha256": run_spec.sha256},
        "source_approval": dict(authority.source_approval),
        "r4_inventory": {"path": R4_INVENTORY_PATH.as_posix(), "sha256": hashlib.sha256(render_canonical_document(inventory)).hexdigest()},
        "comparison": {"record_count": 1675, "physical_member_count": 4711, "logical_binding_count": 5025,
                       "source_content_manifest_sha256": _content_manifest_sha256(content_rows), "snapshot_content_manifest_sha256": _content_manifest_sha256(content_rows),
                       "objects": objects,
                       "binding_comparison": {"source_logical_binding_manifest_sha256": binding_digest, "snapshot_logical_binding_manifest_sha256": binding_digest}},
        "root_lock_comparisons": [{"path": lock["path"], "byte_size": 0, "source_sha256": _EMPTY_SHA256, "snapshot_sha256": _EMPTY_SHA256} for lock in inventory["root_locks"]],  # type: ignore[index]
        "custody_identity_manifest_sha256": inventory["custody_identity_manifest_sha256"],
    }
    validate_transition_receipt(receipt, inventory=inventory, profile=authority)
    return receipt


def _run_spec_fixture(sha256: str = "b" * 64) -> _AuthenticatedRunSpec:
    """Private validator-test fixture; publication always authenticates from disk."""
    _sha256(sha256, "run spec SHA-256")
    return _AuthenticatedRunSpec(
        Path("."),
        DirectorySnapshot(None, (0, 0, 0, 0, 0, 0)),
        DirectorySnapshot(Path("fixture"), (0, 0, 0, 0, 0, 0)),
        RegularFileSnapshot(Path("fixture"), b"{}\n", (0, 0, 0, 0, 0, 0, 0)),
        sha256,
        _RUN_SPEC_TOKEN,
    )


def validate_transition_receipt(
    receipt: Mapping[str, object], *, inventory: Mapping[str, object],
    profile: R4SourceAuthorityProfile | None = None,
) -> None:
    authority = _profile(profile)
    keys = {"schema_version", "receipt_kind", "run_id", "run_spec", "source_approval", "r4_inventory", "comparison", "root_lock_comparisons", "custody_identity_manifest_sha256"}
    doc = _closed(receipt, keys, "R4 transition receipt")
    if doc["schema_version"] != 1 or doc["receipt_kind"] != "R4_PHYSICAL_CUSTODY_TRANSITION" or doc["run_id"] != R4_RUN_ID:
        raise CUSTODY_UNAVAILABLE("transition receipt constants differ")
    run_spec = _closed(doc["run_spec"], {"path", "sha256"}, "run spec")
    if run_spec["path"] != R4_RUN_SPEC_PATH.as_posix():
        raise CUSTODY_UNAVAILABLE("run spec path differs")
    _sha256(run_spec["sha256"], "run spec SHA-256")
    _validate_source_approval(doc["source_approval"], authority)
    binding = _closed(doc["r4_inventory"], {"path", "sha256"}, "R4 inventory binding")
    if binding["path"] != R4_INVENTORY_PATH.as_posix():
        raise CUSTODY_UNAVAILABLE("R4 inventory path differs")
    _sha256(binding["sha256"], "R4 inventory SHA-256")
    comparison = _closed(doc["comparison"], {"record_count", "physical_member_count", "logical_binding_count", "source_content_manifest_sha256", "snapshot_content_manifest_sha256", "objects", "binding_comparison"}, "comparison")
    if (comparison["record_count"], comparison["physical_member_count"], comparison["logical_binding_count"]) != _EXPECTED_COUNTS:
        raise CUSTODY_UNAVAILABLE("comparison counts differ")
    _sha256(comparison["source_content_manifest_sha256"], "source content manifest SHA-256")
    _sha256(comparison["snapshot_content_manifest_sha256"], "snapshot content manifest SHA-256")
    objects = comparison["objects"]
    if not isinstance(objects, list) or len(objects) != 4711:
        raise CUSTODY_UNAVAILABLE("comparison object count differs")
    previous = ""
    rows: list[dict[str, object]] = []
    for obj in objects:
        item = _closed(obj, {"path", "byte_size", "source_sha256", "snapshot_sha256"}, "comparison object")
        path = _strict_descendant(item["path"], "comparison object path")
        if path <= previous:
            raise CUSTODY_UNAVAILABLE("comparison objects are not sorted unique")
        previous = path
        _integer(item["byte_size"], "comparison byte size")
        source_hash = _sha256(item["source_sha256"], "comparison source hash")
        snapshot_hash = _sha256(item["snapshot_sha256"], "comparison snapshot hash")
        if source_hash != snapshot_hash:
            raise CUSTODY_UNAVAILABLE("comparison object hashes differ")
        rows.append({"path": path, "byte_size": item["byte_size"], "sha256": snapshot_hash})
    if _content_manifest_sha256(rows) != comparison["source_content_manifest_sha256"] or comparison["source_content_manifest_sha256"] != comparison["snapshot_content_manifest_sha256"]:
        raise CUSTODY_UNAVAILABLE("content comparison digest differs")
    binding_comparison = _closed(comparison["binding_comparison"], {"source_logical_binding_manifest_sha256", "snapshot_logical_binding_manifest_sha256"}, "binding comparison")
    source_binding = _sha256(binding_comparison["source_logical_binding_manifest_sha256"], "source binding manifest SHA-256")
    snapshot_binding = _sha256(binding_comparison["snapshot_logical_binding_manifest_sha256"], "snapshot binding manifest SHA-256")
    if source_binding != snapshot_binding:
        raise CUSTODY_UNAVAILABLE("binding comparison hashes differ")
    locks = doc["root_lock_comparisons"]
    if not isinstance(locks, list) or len(locks) != 4:
        raise CUSTODY_UNAVAILABLE("root lock comparison count differs")
    for item, name in zip(locks, _ROOT_LOCK_NAMES):
        lock = _closed(item, {"path", "byte_size", "source_sha256", "snapshot_sha256"}, "root lock comparison")
        if lock["path"] != name or lock["byte_size"] != 0 or lock["source_sha256"] != _EMPTY_SHA256 or lock["snapshot_sha256"] != _EMPTY_SHA256:
            raise CUSTODY_UNAVAILABLE("root lock comparison differs")
    _sha256(doc["custody_identity_manifest_sha256"], "custody identity manifest SHA-256")
    validate_r4_inventory(inventory, profile=authority)
    if (
        binding["sha256"] != hashlib.sha256(render_canonical_document(inventory)).hexdigest()
        or doc["custody_identity_manifest_sha256"]
        != inventory["custody_identity_manifest_sha256"]
    ):
        raise CUSTODY_UNAVAILABLE("receipt inventory binding differs")
    expected_objects = [
        {
            "path": member["path"], "byte_size": member["byte_size"],
            "source_sha256": member["sha256"], "snapshot_sha256": member["sha256"],
        }
        for member in inventory["members"]  # type: ignore[index]
    ]
    if (
        objects != expected_objects
        or source_binding != _content_manifest_sha256(_binding_manifest_rows(inventory))
    ):
        raise CUSTODY_UNAVAILABLE("receipt comparison differs from inventory")


def _regular_identity(observed: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """The complete no-follow regular identity used only for live rebinding."""
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_ctime_ns,
        observed.st_size,
        stat.S_IMODE(observed.st_mode),
        observed.st_nlink,
    )


def _require_regular_identity(
    observed: os.stat_result, expected: tuple[int, int, int, int, int, int], label: str
) -> None:
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or _regular_identity(observed) != expected
    ):
        raise CUSTODY_UNAVAILABLE(f"{label} changed")


def _reread_final_json(parent_fd: int, final_name: str) -> tuple[dict[str, object], str]:
    fd = -1
    try:
        fd = os.open(
            final_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        observed = os.fstat(fd)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise CUSTODY_UNAVAILABLE("published R4 artifact is unsafe")
        initial_nlink = observed.st_nlink
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        if not payload.endswith(b"\n"):
            raise CUSTODY_UNAVAILABLE("published R4 artifact is not canonical newline JSON")
        document = json.loads(payload)
        if not isinstance(document, dict) or render_canonical_document(document) != payload:
            raise CUSTODY_UNAVAILABLE("published R4 artifact differs from canonical bytes")
        after_read = os.fstat(fd)
        if after_read.st_nlink != initial_nlink:
            raise CUSTODY_UNAVAILABLE("published R4 artifact descriptor changed")
        expected = _regular_identity(after_read)
        _require_regular_identity(
            os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False),
            expected,
            "published R4 artifact final path",
        )
        reopened = os.open(
            final_name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            _require_regular_identity(
                os.fstat(reopened), expected, "published R4 artifact final path"
            )
        finally:
            os.close(reopened)
        return document, hashlib.sha256(payload).hexdigest()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CUSTODY_UNAVAILABLE("published R4 artifact cannot be reread") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _open_exact_artifact_parent(repository_root: Path, artifact_path: Path) -> int:
    """Open the exact R4 artifact directory under a separate trusted root."""
    descriptor = -1
    try:
        with open_trusted_root(repository_root, nonblocking=True) as root:
            descriptor = os.dup(root.root_fd)
            for part in artifact_path.parent.parts:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=descriptor,
                )
                named = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                opened = os.fstat(child)
                os.close(descriptor)
                descriptor = child
                if (
                    not stat.S_ISDIR(named.st_mode)
                    or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
                ):
                    raise CUSTODY_UNAVAILABLE("R4 artifact parent path changed")
            root.assert_bound()
            result = descriptor
            descriptor = -1
            return result
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("R4 artifact parent is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _capture_stable_directory_snapshot(
    repository_root: Path, relative: Path | None, label: str
) -> DirectorySnapshot:
    try:
        with open_trusted_root(repository_root, nonblocking=True) as root:
            snapshot = capture_stable_directory_snapshot(root, relative, label)
            root.assert_bound()
            return snapshot
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable") from exc


def _rebind_stable_directory_snapshot(
    repository_root: Path, expected: DirectorySnapshot, label: str
) -> DirectorySnapshot:
    try:
        with open_trusted_root(repository_root, nonblocking=True) as root:
            stable = rebind_directory_snapshot(root, expected, label)
            root.assert_bound()
            return stable
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} changed") from exc


def _repository_identity(repository_root: Path) -> DirectorySnapshot:
    return _capture_stable_directory_snapshot(
        repository_root, None, "repository authority anchor"
    )


def _capture_stable_regular_snapshot(
    repository_root: Path, artifact_path: Path, label: str
) -> RegularFileSnapshot:
    """Capture canonical file bytes after bounded APFS ctime stabilization."""
    try:
        with open_trusted_root(repository_root, nonblocking=True) as root:
            stable = capture_stable_regular_snapshot(
                root, artifact_path, label, nonblocking=True
            )
            root.assert_bound()
            return stable
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable") from exc


def _rebind_stable_regular_snapshot(
    repository_root: Path,
    expected: RegularFileSnapshot,
    label: str,
) -> RegularFileSnapshot:
    """Rebind exactly one final artifact, permitting only secure_fs ctime drift."""
    try:
        with open_trusted_root(repository_root, nonblocking=True) as root:
            stable = rebind_regular_snapshot(root, expected, label)
            root.assert_bound()
            return stable
    except (OSError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE(f"{label} changed") from exc


def authenticate_r4_run_spec(repository_root: Path) -> _AuthenticatedRunSpec:
    """No-follow, canonical and rebound binding of the fixed R4 RunSpec file."""
    try:
        repository = Path(repository_root).absolute()
        repository_identity = _repository_identity(repository)
        parent_identity = _capture_stable_directory_snapshot(
            repository, R4_RUN_SPEC_PATH.parent, "R4 RunSpec parent"
        )
        snapshot = _capture_stable_regular_snapshot(
            repository, R4_RUN_SPEC_PATH, "R4 RunSpec"
        )
        if not snapshot.contents.endswith(b"\n"):
            raise CUSTODY_UNAVAILABLE("R4 RunSpec is malformed")
        document = json.loads(snapshot.contents)
        if (
            not isinstance(document, dict)
            or render_canonical_document(document) != snapshot.contents
        ):
            raise CUSTODY_UNAVAILABLE("R4 RunSpec is malformed")
        from .protocols import expected_r4_recovery_source_run, parse_source_run_spec

        if document != expected_r4_recovery_source_run():
            raise CUSTODY_UNAVAILABLE("R4 RunSpec differs from its exact authority")
        parse_source_run_spec(document)
        parent_identity = _rebind_stable_directory_snapshot(
            repository, parent_identity, "R4 RunSpec parent"
        )
        repository_identity = _rebind_stable_directory_snapshot(
            repository, repository_identity, "repository authority anchor"
        )
        return _AuthenticatedRunSpec(
            repository,
            repository_identity,
            parent_identity,
            snapshot,
            hashlib.sha256(snapshot.contents).hexdigest(),
            _RUN_SPEC_TOKEN,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("R4 RunSpec is unavailable") from exc


def _rebind_run_spec_binding(binding: _AuthenticatedRunSpec) -> None:
    """Refresh one allowed delayed ctime transition on a live RunSpec binding."""
    repository = binding.repository_root
    repository_identity = _rebind_stable_directory_snapshot(
        repository, binding.repository_identity, "repository authority anchor"
    )
    parent_identity = _rebind_stable_directory_snapshot(
        repository, binding.parent_identity, "R4 RunSpec parent"
    )
    stable = _rebind_stable_regular_snapshot(
        repository, binding.final_snapshot, "R4 RunSpec"
    )
    if hashlib.sha256(stable.contents).hexdigest() != binding.sha256:
        raise CUSTODY_UNAVAILABLE("R4 RunSpec changed")
    object.__setattr__(binding, "repository_identity", repository_identity)
    object.__setattr__(binding, "parent_identity", parent_identity)
    object.__setattr__(binding, "final_snapshot", stable)


def _read_exact_inventory(repository_root: Path) -> dict[str, object]:
    descriptor = -1
    try:
        parent = repository_root / R4_INVENTORY_PATH.parent
        with open_trusted_root(parent, nonblocking=True) as root:
            descriptor = os.open(
                R4_INVENTORY_PATH.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root.root_fd,
            )
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise CUSTODY_UNAVAILABLE("published R4 inventory is unsafe")
            initial_nlink = opened.st_nlink
            named = os.stat(
                R4_INVENTORY_PATH.name, dir_fd=root.root_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or (named.st_dev, named.st_ino, stat.S_IMODE(named.st_mode))
                != (opened.st_dev, opened.st_ino, stat.S_IMODE(opened.st_mode))
            ):
                raise CUSTODY_UNAVAILABLE("published R4 inventory final path changed")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = b"".join(chunks)
            document = json.loads(payload)
            if not isinstance(document, dict) or render_canonical_document(document) != payload:
                raise CUSTODY_UNAVAILABLE("published R4 inventory differs from canonical bytes")
            after_read = os.fstat(descriptor)
            if after_read.st_nlink != initial_nlink:
                raise CUSTODY_UNAVAILABLE("published R4 inventory descriptor changed")
            expected = _regular_identity(after_read)
            _require_regular_identity(
                os.stat(R4_INVENTORY_PATH.name, dir_fd=root.root_fd, follow_symlinks=False),
                expected,
                "published R4 inventory final path",
            )
            rebound = os.open(
                R4_INVENTORY_PATH.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root.root_fd,
            )
            try:
                _require_regular_identity(
                    os.fstat(rebound), expected, "published R4 inventory final path"
                )
            finally:
                os.close(rebound)
            root.assert_bound()
            validate_r4_inventory(document)
            return document
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise CUSTODY_UNAVAILABLE("published R4 inventory cannot be reread") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _publish(
    owner: IssuanceOwner,
    artifact_path: Path,
    document: Mapping[str, object],
    validator: Any,
    *,
    repository_root: Path,
) -> str:
    if not isinstance(owner, IssuanceOwner) or not owner.is_live_owner():
        raise CUSTODY_UNAVAILABLE("R4 artifact publisher is not a live issuance owner")
    owner.require_r4_repository_authority(repository_root)
    validator(document)
    final_name = artifact_path.name
    temporary_name = f".{final_name}.{secrets.token_hex(16)}.tmp"
    intended = render_canonical_document(document)
    artifact_parent_fd = -1
    try:
        artifact_parent_fd = _open_exact_artifact_parent(repository_root, artifact_path)
        parent_identity = os.fstat(artifact_parent_fd)
        if not owner.is_live_owner():
            raise CUSTODY_UNAVAILABLE("R4 custody owner changed before artifact publication")
        digest = publish_canonical_json(
            artifact_parent_fd, temporary_name, final_name, document
        )
        reread, observed = _reread_final_json(artifact_parent_fd, final_name)
        validator(reread)
        rebound_parent_fd = _open_exact_artifact_parent(repository_root, artifact_path)
        try:
            rebound_parent = os.fstat(rebound_parent_fd)
            if (
                rebound_parent.st_dev,
                rebound_parent.st_ino,
                stat.S_IMODE(rebound_parent.st_mode),
            ) != (
                parent_identity.st_dev,
                parent_identity.st_ino,
                stat.S_IMODE(parent_identity.st_mode),
            ):
                raise CUSTODY_UNAVAILABLE("R4 artifact parent path changed")
        finally:
            os.close(rebound_parent_fd)
        if not owner.is_live_owner():
            raise CUSTODY_UNAVAILABLE("R4 custody owner changed after artifact publication")
        owner.require_r4_repository_authority(repository_root)
        if observed != digest or observed != hashlib.sha256(intended).hexdigest():
            raise CUSTODY_UNAVAILABLE("published R4 artifact hash differs")
        return observed
    finally:
        if artifact_parent_fd >= 0:
            os.close(artifact_parent_fd)


def _require_snapshot_owner(
    owner: IssuanceOwner, repository_root: Path, custody_parent: Path
) -> None:
    """Narrowly authenticate the live owner at the post-freeze R4 stage."""
    if not isinstance(owner, IssuanceOwner) or not owner.is_live_owner():
        raise CUSTODY_UNAVAILABLE("R4 custody owner is not live")
    try:
        owner.configure_r4_authority(repository_root, custody_parent)
        owner.require_r4_repository_authority(repository_root)
        state = owner.state()
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("R4 custody owner cannot be authenticated") from exc
    if state not in {
        IssuanceState.OWNER_SNAPSHOT_PUBLISHED,
        IssuanceState.OWNER_INVENTORY_PUBLISHED,
        IssuanceState.OWNER_RECEIPT_PUBLISHED,
    }:
        raise CUSTODY_UNAVAILABLE("R4 custody snapshot is not authenticated and frozen")


def _published_inventory_continuation(
    owner: IssuanceOwner, repository_root: Path, digest: str, document: Mapping[str, object],
    profile: R4SourceAuthorityProfile,
) -> _PublishedInventory:
    try:
        repository = Path(repository_root).absolute()
        repository_identity = _repository_identity(repository)
        # This happens after the inventory exclusive rename and parent fsync;
        # the directory capture includes its mandatory stable reread.
        parent_identity = _capture_stable_directory_snapshot(
            repository, R4_INVENTORY_PATH.parent, "R4 inventory parent"
        )
        snapshot = _capture_stable_regular_snapshot(
            repository, R4_INVENTORY_PATH, "published R4 inventory"
        )
        if (
            hashlib.sha256(snapshot.contents).hexdigest() != digest
            or snapshot.contents != render_canonical_document(document)
        ):
            raise CUSTODY_UNAVAILABLE("published R4 inventory changed")
        return _PublishedInventory(
            digest,
            dict(document),
            repository,
            repository_identity,
            parent_identity,
            snapshot,
            owner,
            profile,
        )
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, CUSTODY_UNAVAILABLE):
            raise
        raise CUSTODY_UNAVAILABLE("published R4 inventory is unavailable") from exc


def _rebind_inventory_continuation(
    owner: IssuanceOwner, continuation: _PublishedInventory, repository_root: Path
) -> dict[str, object]:
    if not isinstance(continuation, _PublishedInventory) or continuation.owner is not owner:
        raise CUSTODY_UNAVAILABLE("R4 inventory continuation differs from custody owner")
    repository = Path(repository_root).absolute()
    if continuation.repository_root != repository:
        raise CUSTODY_UNAVAILABLE("R4 inventory repository authority differs")
    repository_identity = _rebind_stable_directory_snapshot(
        repository, continuation.repository_identity, "repository authority anchor"
    )
    parent_identity = _rebind_stable_directory_snapshot(
        repository, continuation.parent_identity, "R4 inventory parent"
    )
    snapshot = _rebind_stable_regular_snapshot(
        repository, continuation.final_snapshot, "published R4 inventory"
    )
    digest = hashlib.sha256(snapshot.contents).hexdigest()
    try:
        document = json.loads(snapshot.contents)
    except json.JSONDecodeError as exc:
        raise CUSTODY_UNAVAILABLE("published R4 inventory changed") from exc
    if (
        digest != continuation.payload_sha256
        or not isinstance(document, dict)
        or snapshot.contents != render_canonical_document(continuation.document)
    ):
        raise CUSTODY_UNAVAILABLE("R4 inventory continuation payload changed")
    object.__setattr__(continuation, "repository_identity", repository_identity)
    object.__setattr__(continuation, "parent_identity", parent_identity)
    object.__setattr__(continuation, "final_snapshot", snapshot)
    validate_r4_inventory(document, profile=continuation.profile)
    return document


def publish_inventory(
    owner: IssuanceOwner,
    *,
    repository_root: Path,
    custody_parent: Path,
) -> _PublishedInventory:
    """Freshly capture and publish the sole owner-bound R4 inventory."""
    _require_snapshot_owner(owner, repository_root, custody_parent)
    captured = capture_frozen_snapshot(
        owner, repository_root=repository_root, custody_parent=custody_parent
    )
    profile = getattr(owner, "_source_authority_profile", None)
    if profile is None:
        profile = production_source_authority_profile()
    if not isinstance(profile, R4SourceAuthorityProfile):
        raise CUSTODY_UNAVAILABLE("R4 source authority is unavailable")
    document = build_r4_inventory(captured, profile=profile)
    digest = _publish(
        owner, R4_INVENTORY_PATH, document,
        lambda value: validate_r4_inventory(value, profile=profile),
        repository_root=repository_root,
    )
    _require_snapshot_owner(owner, repository_root, custody_parent)
    continuation = _published_inventory_continuation(
        owner, repository_root, digest, document, profile
    )
    _PUBLISHED_INVENTORIES[owner] = continuation
    return continuation


def publish_transition_receipt(
    owner: IssuanceOwner,
    continuation: _PublishedInventory,
    *,
    repository_root: Path,
    custody_parent: Path,
) -> str:
    _require_snapshot_owner(owner, repository_root, custody_parent)
    if _PUBLISHED_INVENTORIES.get(owner) is not continuation:
        raise CUSTODY_UNAVAILABLE("R4 inventory was not published by this custody owner")
    authenticated_inventory = _rebind_inventory_continuation(owner, continuation, repository_root)
    run_spec = authenticate_r4_run_spec(repository_root)
    receipt = build_transition_receipt(
        authenticated_inventory, run_spec=run_spec, profile=continuation.profile
    )
    digest = _publish(
        owner, R4_RECEIPT_PATH, receipt,
        lambda value: validate_transition_receipt(
            value, inventory=authenticated_inventory, profile=continuation.profile
        ),
        repository_root=repository_root,
    )
    _require_snapshot_owner(owner, repository_root, custody_parent)
    return digest
