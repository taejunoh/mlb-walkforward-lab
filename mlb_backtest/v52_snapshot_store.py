"""Immutable, canonical source-inventory contract for the v5.2 snapshot set.

Supported runtimes require POSIX ``dir_fd``, ``O_DIRECTORY``, ``O_NOFOLLOW``, and
``O_NONBLOCK`` behavior.  Immutable leaf reads fail closed when that capability
is unavailable; there is no safe blocking-read fallback for special files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .v52_source_contracts import canonical_json_bytes
from .v52_verify_sources import (
    SnapshotReplayInputs,
    NormalizedSnapshot,
    replay_verified_snapshots,
    snapshot_sort_key,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LOCK_ISSUER = object()
_SOURCES = (
    "baseball_savant_statcast",
    "mlb_schedule_game_feed",
    "mlb_stats_pitcher_bf",
    "mlb_stats_pitching",
    "prior_schedule_history",
    "venue_team_timezone_table",
)
_SOURCE_GRAINS = {
    "baseball_savant_statcast": "pitch",
    "mlb_schedule_game_feed": "season_game_evidence_snapshot",
    "mlb_stats_pitcher_bf": "player_career_batters_faced_reference",
    "mlb_stats_pitching": "season_d1_league_pitching_reference",
    "prior_schedule_history": "verified_game_projection",
    "venue_team_timezone_table": "effective_dated_venue_team_reference",
}
_SEASON_TOTALS = {2018: 12, 2019: 12, 2020: 8, 2021: 12, 2022: 12, 2023: 13, 2024: 12}
_SOURCE_TOTALS = {
    "baseball_savant_statcast": 46,
    "mlb_schedule_game_feed": 7,
    "mlb_stats_pitcher_bf": 7,
    "mlb_stats_pitching": 7,
    "prior_schedule_history": 7,
    "venue_team_timezone_table": 7,
}
_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version", "artifact_kind", "run_id", "status", "critical_errors",
        "network_attempts", "source_gate_ready", "run_spec", "acquisition_contract",
        "terminal_checkpoint", "schedule_inventory", "game_evidence_inventory", "timezone_gate",
        "snapshot_sha256s", "logical_query_ids", "split_receipt_sha256s", "snapshots",
        "source_seasons", "counts",
    }
)


@dataclass(frozen=True)
class AuthorityBinding:
    path: str
    sha256: str

    def as_mapping(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class TerminalCheckpointBinding:
    path: str
    sha256: str
    sequence: int
    stage: Literal["REMAINING"]

    def as_mapping(self) -> dict[str, object]:
        return {"path": self.path, "sequence": self.sequence, "sha256": self.sha256, "stage": self.stage}


@dataclass(frozen=True)
class AuthorityBindings:
    run_spec: AuthorityBinding
    acquisition_contract: AuthorityBinding
    terminal_checkpoint: TerminalCheckpointBinding
    schedule_inventory: AuthorityBinding
    game_evidence_inventory: AuthorityBinding
    timezone_gate: AuthorityBinding


@dataclass(frozen=True)
class SnapshotStoreInputs:
    replay: SnapshotReplayInputs
    bindings: AuthorityBindings
    current_phase: str


@dataclass(frozen=True)
class PublishReceipt:
    inventory_sha256: str
    snapshot_count: int


@dataclass(frozen=True)
class PublishOutcome:
    """Lock-scoped result for a caller-owned directory capability publication."""

    receipt: PublishReceipt
    created: bool


@dataclass(frozen=True)
class LoadedSnapshotStore:
    """A fully revalidated, immutable replay of a published snapshot store."""

    inventory: SourceInventory
    snapshots: tuple[NormalizedSnapshot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))


class SnapshotStoreError(Exception):
    """Base class for immutable snapshot-store publication failures."""


class PublishConflict(SnapshotStoreError):
    """An immutable destination exists with contents other than the requested bytes."""


class LockHeld(SnapshotStoreError):
    """Another publisher owns the store lock."""


class PublishError(SnapshotStoreError):
    """The publication contract or its owned lock could not be safely verified."""


@dataclass(frozen=True)
class _OwnedLock:
    fd: int
    path: Path
    device: int
    inode: int
    nonce: str
    contents: bytes
    run_id: str
    lock_owner: str
    issuer: object


@dataclass(frozen=True)
class SnapshotInventoryRow:
    season: int
    source: str
    grain: str
    status: Literal["READY"]
    snapshot_sha256: str
    relative_path: str
    byte_length: int
    logical_query_ids: tuple[str, ...]
    source_query_sidecar_sha256s: tuple[str, ...]
    split_receipt_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_query_ids", tuple(self.logical_query_ids))
        object.__setattr__(
            self, "source_query_sidecar_sha256s", tuple(self.source_query_sidecar_sha256s)
        )
        object.__setattr__(self, "split_receipt_sha256s", tuple(self.split_receipt_sha256s))

    def as_mapping(self) -> dict[str, object]:
        return {
            "season": self.season, "source": self.source, "grain": self.grain,
            "status": self.status, "snapshot_sha256": self.snapshot_sha256,
            "relative_path": self.relative_path, "byte_length": self.byte_length,
            "logical_query_ids": list(self.logical_query_ids),
            "source_query_sidecar_sha256s": list(self.source_query_sidecar_sha256s),
            "split_receipt_sha256s": list(self.split_receipt_sha256s),
        }


@dataclass(frozen=True)
class SourceSeasonInventory:
    season: int
    source: str
    grain: str
    status: Literal["READY"]
    snapshot_sha256s: tuple[str, ...]
    logical_query_ids: tuple[str, ...]
    split_receipt_sha256s: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_sha256s", tuple(self.snapshot_sha256s))
        object.__setattr__(self, "logical_query_ids", tuple(self.logical_query_ids))
        object.__setattr__(self, "split_receipt_sha256s", tuple(self.split_receipt_sha256s))

    def as_mapping(self) -> dict[str, object]:
        return {
            "season": self.season, "source": self.source, "grain": self.grain,
            "status": self.status, "snapshot_sha256s": list(self.snapshot_sha256s),
            "logical_query_ids": list(self.logical_query_ids),
            "split_receipt_sha256s": list(self.split_receipt_sha256s),
        }


@dataclass(frozen=True)
class SourceInventory:
    schema_version: Literal[1]
    artifact_kind: Literal["V5_2_SOURCE_INVENTORY"]
    run_id: str
    status: Literal["READY_CANDIDATE"]
    critical_errors: tuple[()]
    network_attempts: Literal[0]
    source_gate_ready: Literal[True]
    bindings: AuthorityBindings
    snapshot_sha256s: tuple[str, ...]
    logical_query_ids: tuple[str, ...]
    split_receipt_sha256s: tuple[str, ...]
    snapshots: tuple[SnapshotInventoryRow, ...]
    source_seasons: tuple[SourceSeasonInventory, ...]
    counts: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "critical_errors", tuple(self.critical_errors))
        object.__setattr__(self, "snapshot_sha256s", tuple(self.snapshot_sha256s))
        object.__setattr__(self, "logical_query_ids", tuple(self.logical_query_ids))
        object.__setattr__(self, "split_receipt_sha256s", tuple(self.split_receipt_sha256s))
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "source_seasons", tuple(self.source_seasons))
        object.__setattr__(self, "counts", _freeze_json(self.counts))

    @property
    def run_spec(self) -> AuthorityBinding:
        return self.bindings.run_spec

    @property
    def acquisition_contract(self) -> AuthorityBinding:
        return self.bindings.acquisition_contract

    @property
    def terminal_checkpoint(self) -> TerminalCheckpointBinding:
        return self.bindings.terminal_checkpoint

    @property
    def schedule_inventory(self) -> AuthorityBinding:
        return self.bindings.schedule_inventory

    @property
    def game_evidence_inventory(self) -> AuthorityBinding:
        return self.bindings.game_evidence_inventory

    @property
    def timezone_gate(self) -> AuthorityBinding:
        return self.bindings.timezone_gate

    def as_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_kind": self.artifact_kind,
            "run_id": self.run_id,
            "status": self.status,
            "critical_errors": list(self.critical_errors),
            "network_attempts": self.network_attempts,
            "source_gate_ready": self.source_gate_ready,
            "run_spec": self.run_spec.as_mapping(),
            "acquisition_contract": self.acquisition_contract.as_mapping(),
            "terminal_checkpoint": self.terminal_checkpoint.as_mapping(),
            "schedule_inventory": self.schedule_inventory.as_mapping(),
            "game_evidence_inventory": self.game_evidence_inventory.as_mapping(),
            "timezone_gate": self.timezone_gate.as_mapping(),
            "snapshot_sha256s": list(self.snapshot_sha256s),
            "logical_query_ids": list(self.logical_query_ids),
            "split_receipt_sha256s": list(self.split_receipt_sha256s),
            "snapshots": [row.as_mapping() for row in self.snapshots],
            "source_seasons": [row.as_mapping() for row in self.source_seasons],
            "counts": _plain_json(self.counts),
        }


def build_inventory(inputs: SnapshotStoreInputs) -> SourceInventory:
    """Replay sealed normalized snapshots into the closed source-inventory contract."""
    _validate_build_inputs(inputs)
    return _build_inventory_from_replayed(inputs, tuple(replay_verified_snapshots(inputs.replay)))


def _validate_build_inputs(inputs: SnapshotStoreInputs) -> None:
    if not isinstance(inputs, SnapshotStoreInputs):
        raise TypeError("snapshot store inputs must be SnapshotStoreInputs")
    if inputs.current_phase not in {"ACQUIRING", "SOURCES_READY"}:
        raise ValueError("source inventory is only available while acquiring or sources-ready")
    if not isinstance(inputs.replay, SnapshotReplayInputs) or not isinstance(inputs.bindings, AuthorityBindings):
        raise TypeError("snapshot store inputs contain invalid authorities")
    _validate_bindings(inputs.bindings)
    if not isinstance(inputs.replay.run_id, str) or not inputs.replay.run_id:
        raise ValueError("replay run_id must be a nonempty string")


def _build_inventory_from_replayed(
    inputs: SnapshotStoreInputs, snapshots: tuple[NormalizedSnapshot, ...]
) -> SourceInventory:
    _validate_build_inputs(inputs)
    if tuple(snapshots) != tuple(sorted(snapshots, key=snapshot_sort_key)):
        raise ValueError("replayed snapshots must use canonical snapshot_sort_key order")
    rows = tuple(_row_from_snapshot(snapshot, inputs.replay.run_id) for snapshot in snapshots)
    inventory = SourceInventory(
        1, "V5_2_SOURCE_INVENTORY", inputs.replay.run_id, "READY_CANDIDATE", (), 0, True,
        inputs.bindings,
        tuple(row.snapshot_sha256 for row in rows),
        tuple(sorted({item for row in rows for item in row.logical_query_ids})),
        tuple(sorted({item for row in rows for item in row.split_receipt_sha256s})),
        rows,
        _source_seasons(rows),
        _freeze_counts(rows),
    )
    _validate_inventory(inventory)
    _validate_schema(inventory.as_mapping())
    return inventory


def inventory_bytes(inventory: SourceInventory) -> bytes:
    if not isinstance(inventory, SourceInventory):
        raise TypeError("inventory must be SourceInventory")
    _validate_inventory(inventory)
    payload = inventory.as_mapping()
    _validate_schema(payload)
    return canonical_json_bytes(payload) + b"\n"


def parse_inventory(raw: bytes) -> SourceInventory:
    if not isinstance(raw, bytes):
        raise TypeError("inventory bytes must be bytes")
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_reject_duplicate_object_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("inventory bytes must be valid UTF-8 canonical JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("inventory must be an object")
    canonical = canonical_json_bytes(payload) + b"\n"
    if raw != canonical:
        raise ValueError("inventory bytes are not canonical JSON with one final newline")
    _validate_schema(payload)
    try:
        inventory = _inventory_from_mapping(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("inventory semantic contract is invalid") from exc
    _validate_inventory(inventory)
    return inventory


def _row_from_snapshot(snapshot: NormalizedSnapshot, run_id: str) -> SnapshotInventoryRow:
    if not isinstance(snapshot, NormalizedSnapshot):
        raise TypeError("replay must return normalized snapshots")
    if snapshot.run_id != run_id:
        raise ValueError("snapshot run_id differs from replay run_id")
    if not isinstance(snapshot.canonical_bytes, bytes) or snapshot.canonical_bytes != canonical_json_bytes(snapshot.as_mapping()) + b"\n":
        raise ValueError("snapshot bytes are not canonical")
    if hashlib.sha256(snapshot.canonical_bytes).hexdigest() != snapshot.sha256:
        raise ValueError("snapshot sha256 differs from canonical bytes")
    return SnapshotInventoryRow(
        snapshot.season, snapshot.source, snapshot.grain, snapshot.status, snapshot.sha256,
        f"{snapshot.season}/{snapshot.source}/{snapshot.sha256}.snapshot.json",
        len(snapshot.canonical_bytes), tuple(sorted(snapshot.logical_query_ids)),
        tuple(sorted(snapshot.source_query_sidecar_sha256s)), tuple(snapshot.split_receipt_sha256s),
    )


def _source_seasons(rows: tuple[SnapshotInventoryRow, ...]) -> tuple[SourceSeasonInventory, ...]:
    grouped: dict[tuple[int, str], list[SnapshotInventoryRow]] = {}
    for row in rows:
        grouped.setdefault((row.season, row.source), []).append(row)
    return tuple(
        SourceSeasonInventory(
            season, source, _SOURCE_GRAINS[source], "READY",
            tuple(row.snapshot_sha256 for row in group),
            tuple(sorted({item for row in group for item in row.logical_query_ids})),
            tuple(sorted({item for row in group for item in row.split_receipt_sha256s})),
        )
        for (season, source), group in sorted(grouped.items())
    )


def _freeze_counts(rows: tuple[SnapshotInventoryRow, ...]) -> Mapping[str, object]:
    by_source = {source: sum(row.source == source for row in rows) for source in _SOURCES}
    by_season = {season: sum(row.season == season for row in rows) for season in _SEASON_TOTALS}
    return MappingProxyType({
        "snapshot_count": len(rows), "source_season_count": len({(row.season, row.source) for row in rows}),
        "source_count": len({row.source for row in rows}),
        "logical_query_count": len({item for row in rows for item in row.logical_query_ids}),
        "split_receipt_count": len({item for row in rows for item in row.split_receipt_sha256s}),
        "by_source": MappingProxyType(by_source), "by_season": MappingProxyType(by_season),
    })


def _inventory_from_mapping(payload: Mapping[str, object]) -> SourceInventory:
    if set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("inventory top-level keys differ")
    bindings = AuthorityBindings(
        _binding_from_mapping(payload["run_spec"]), _binding_from_mapping(payload["acquisition_contract"]),
        _terminal_from_mapping(payload["terminal_checkpoint"]), _binding_from_mapping(payload["schedule_inventory"]),
        _binding_from_mapping(payload["game_evidence_inventory"]), _binding_from_mapping(payload["timezone_gate"]),
    )
    snapshots = tuple(_snapshot_row_from_mapping(value) for value in _array(payload["snapshots"], "snapshots"))
    seasons = tuple(_source_season_from_mapping(value) for value in _array(payload["source_seasons"], "source_seasons"))
    counts = _counts_from_mapping(payload["counts"])
    return SourceInventory(
        _literal(payload["schema_version"], int, "schema_version"), _literal(payload["artifact_kind"], str, "artifact_kind"),
        _literal(payload["run_id"], str, "run_id"), _literal(payload["status"], str, "status"),
        tuple(_array(payload["critical_errors"], "critical_errors")), _literal(payload["network_attempts"], int, "network_attempts"),
        _literal(payload["source_gate_ready"], bool, "source_gate_ready"), bindings,
        tuple(_sha_values(payload["snapshot_sha256s"], "snapshot_sha256s")),
        tuple(_sha_values(payload["logical_query_ids"], "logical_query_ids")),
        tuple(_sha_values(payload["split_receipt_sha256s"], "split_receipt_sha256s")), snapshots, seasons, counts,
    )


def _binding_from_mapping(value: object) -> AuthorityBinding:
    value = _mapping(value, "binding")
    if set(value) != {"path", "sha256"}:
        raise ValueError("binding keys differ")
    return AuthorityBinding(_literal(value["path"], str, "path"), _literal(value["sha256"], str, "sha256"))


def _terminal_from_mapping(value: object) -> TerminalCheckpointBinding:
    value = _mapping(value, "terminal checkpoint")
    if set(value) != {"path", "sha256", "sequence", "stage"}:
        raise ValueError("terminal binding keys differ")
    return TerminalCheckpointBinding(_literal(value["path"], str, "path"), _literal(value["sha256"], str, "sha256"), _literal(value["sequence"], int, "sequence"), _literal(value["stage"], str, "stage"))


def _snapshot_row_from_mapping(value: object) -> SnapshotInventoryRow:
    value = _mapping(value, "snapshot row")
    keys = {"season", "source", "grain", "status", "snapshot_sha256", "relative_path", "byte_length", "logical_query_ids", "source_query_sidecar_sha256s", "split_receipt_sha256s"}
    if set(value) != keys:
        raise ValueError("snapshot row keys differ")
    return SnapshotInventoryRow(
        _literal(value["season"], int, "season"), _literal(value["source"], str, "source"), _literal(value["grain"], str, "grain"),
        _literal(value["status"], str, "status"), _literal(value["snapshot_sha256"], str, "snapshot_sha256"),
        _literal(value["relative_path"], str, "relative_path"), _literal(value["byte_length"], int, "byte_length"),
        tuple(_sha_values(value["logical_query_ids"], "logical_query_ids")),
        tuple(_sha_values(value["source_query_sidecar_sha256s"], "source_query_sidecar_sha256s")),
        tuple(_sha_values(value["split_receipt_sha256s"], "split_receipt_sha256s")),
    )


def _source_season_from_mapping(value: object) -> SourceSeasonInventory:
    value = _mapping(value, "source season")
    keys = {"season", "source", "grain", "status", "snapshot_sha256s", "logical_query_ids", "split_receipt_sha256s"}
    if set(value) != keys:
        raise ValueError("source season keys differ")
    return SourceSeasonInventory(
        _literal(value["season"], int, "season"), _literal(value["source"], str, "source"), _literal(value["grain"], str, "grain"), _literal(value["status"], str, "status"),
        tuple(_sha_values(value["snapshot_sha256s"], "snapshot_sha256s")), tuple(_sha_values(value["logical_query_ids"], "logical_query_ids")),
        tuple(_sha_values(value["split_receipt_sha256s"], "split_receipt_sha256s")),
    )


def _counts_from_mapping(value: object) -> Mapping[str, object]:
    value = _mapping(value, "counts")
    keys = {"snapshot_count", "source_season_count", "source_count", "logical_query_count", "split_receipt_count", "by_source", "by_season"}
    if set(value) != keys:
        raise ValueError("counts keys differ")
    by_source = _mapping(value["by_source"], "by_source")
    by_season = _mapping(value["by_season"], "by_season")
    return MappingProxyType({
        key: _literal(value[key], int, key) for key in keys - {"by_source", "by_season"}
    } | {
        "by_source": MappingProxyType({str(key): _literal(item, int, "by_source value") for key, item in by_source.items()}),
        "by_season": MappingProxyType({int(key): _literal(item, int, "by_season value") for key, item in by_season.items()}),
    })


def _validate_inventory(inventory: SourceInventory) -> None:
    if not isinstance(inventory, SourceInventory):
        raise TypeError("inventory must be SourceInventory")
    if (inventory.schema_version, inventory.artifact_kind, inventory.status, inventory.critical_errors, inventory.network_attempts, inventory.source_gate_ready) != (1, "V5_2_SOURCE_INVENTORY", "READY_CANDIDATE", (), 0, True):
        raise ValueError("inventory frozen fields differ")
    if not isinstance(inventory.run_id, str) or not inventory.run_id:
        raise ValueError("inventory run_id differs")
    _validate_bindings(inventory.bindings)
    rows = inventory.snapshots
    if not isinstance(rows, tuple) or len(rows) != 81:
        raise ValueError("inventory must contain 81 snapshot rows")
    if tuple(inventory.snapshot_sha256s) != tuple(row.snapshot_sha256 for row in rows):
        raise ValueError("snapshot sha union/order differs")
    if tuple(rows) != tuple(sorted(rows, key=lambda row: (row.season, row.source, row.grain))):
        raise ValueError("snapshot rows are not canonical order")
    if len({row.snapshot_sha256 for row in rows}) != 81:
        raise ValueError("snapshot sha256s must be unique")
    for row in rows:
        _validate_snapshot_row(row)
    if {row.season for row in rows} != set(_SEASON_TOTALS) or {row.source for row in rows} != set(_SOURCES):
        raise ValueError("closed seasons or sources differ")
    actual_seasons = {season: sum(row.season == season for row in rows) for season in _SEASON_TOTALS}
    actual_sources = {source: sum(row.source == source for row in rows) for source in _SOURCES}
    if actual_seasons != _SEASON_TOTALS or actual_sources != _SOURCE_TOTALS:
        raise ValueError("source or season totals differ")
    logical = tuple(sorted({item for row in rows for item in row.logical_query_ids}))
    splits = tuple(sorted({item for row in rows for item in row.split_receipt_sha256s}))
    sidecars = {item for row in rows for item in row.source_query_sidecar_sha256s}
    all_snapshot_shas = set(inventory.snapshot_sha256s)
    if inventory.logical_query_ids != logical or inventory.split_receipt_sha256s != splits:
        raise ValueError("global component unions differ")
    if all_snapshot_shas & (set(logical) | sidecars | set(splits)):
        raise ValueError("snapshot sha collides with source component")
    if len(logical) != len(set(logical)) or len(splits) != len(set(splits)):
        raise ValueError("global components must be unique")
    expected_seasons = _source_seasons(rows)
    if inventory.source_seasons != expected_seasons or len(inventory.source_seasons) != 42:
        raise ValueError("source-season coverage differs")
    expected_counts = _freeze_counts(rows)
    if _plain_json(inventory.counts) != _plain_json(expected_counts):
        raise ValueError("counts differ from source rows")


def _validate_snapshot_row(row: SnapshotInventoryRow) -> None:
    if not isinstance(row, SnapshotInventoryRow):
        raise TypeError("snapshot inventory rows must be SnapshotInventoryRow")
    if row.season not in _SEASON_TOTALS or row.source not in _SOURCE_GRAINS or row.grain != _SOURCE_GRAINS[row.source] or row.status != "READY":
        raise ValueError("snapshot source/grain/status differs")
    _require_sha(row.snapshot_sha256, "snapshot sha256")
    if row.relative_path != f"{row.season}/{row.source}/{row.snapshot_sha256}.snapshot.json" or not _is_safe_relative_path(row.relative_path):
        raise ValueError("snapshot relative path differs")
    if isinstance(row.byte_length, bool) or not isinstance(row.byte_length, int) or row.byte_length <= 0:
        raise ValueError("snapshot byte length must be positive")
    for values, label, must_equal in (
        (row.logical_query_ids, "logical query ids", True),
        (row.source_query_sidecar_sha256s, "source sidecar sha256s", True),
        (row.split_receipt_sha256s, "split receipt sha256s", False),
    ):
        if not isinstance(values, tuple):
            raise ValueError(f"{label} must be a tuple")
        if (must_equal and values != tuple(sorted(set(values)))) or (
            not must_equal and len(values) != len(set(values))
        ):
            raise ValueError(f"{label} must be {'sorted and ' if must_equal else ''}unique")
        for value in values:
            _require_sha(value, label)
    if not row.logical_query_ids or not row.source_query_sidecar_sha256s:
        raise ValueError("snapshot logical query and sidecar arrays must be nonempty")
    if len(row.logical_query_ids) != len(row.source_query_sidecar_sha256s):
        raise ValueError("logical query and sidecar counts differ")


def _validate_bindings(bindings: AuthorityBindings) -> None:
    if not isinstance(bindings, AuthorityBindings):
        raise TypeError("authority bindings must be AuthorityBindings")
    for binding in (bindings.run_spec, bindings.acquisition_contract, bindings.schedule_inventory, bindings.game_evidence_inventory, bindings.timezone_gate):
        if not isinstance(binding, AuthorityBinding):
            raise TypeError("ordinary authority binding type differs")
        _validate_binding(binding)
    terminal = bindings.terminal_checkpoint
    if not isinstance(terminal, TerminalCheckpointBinding) or terminal.stage != "REMAINING" or isinstance(terminal.sequence, bool) or not isinstance(terminal.sequence, int) or terminal.sequence < 0:
        raise ValueError("terminal checkpoint binding differs")
    _validate_binding(terminal)


def _validate_binding(binding: AuthorityBinding | TerminalCheckpointBinding) -> None:
    if not _is_safe_relative_path(binding.path):
        raise ValueError("authority path is not canonical repository-relative POSIX")
    _require_sha(binding.sha256, "authority sha256")


def _is_safe_relative_path(path: object) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and "\\" not in path
        and not path.startswith("/")
        and not any(ord(character) < 32 or ord(character) == 127 for character in path)
        and all(component not in {"", ".", ".."} for component in path.split("/"))
    )


def _require_sha(value: object, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be lowercase SHA-256")


def _validate_schema(payload: Mapping[str, object]) -> None:
    try:
        _inventory_schema_validator().validate(dict(payload))
    except (ValidationError, ValueError) as exc:
        raise ValueError("source inventory schema validation failed") from exc


@lru_cache(maxsize=1)
def _inventory_schema_validator() -> Draft202012Validator:
    schema = json.loads(
        (Path(__file__).resolve().parent.parent / "schemas" / "v5_2_source_inventory.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be a string-keyed object")
    return value


def _literal(value: object, expected: type, label: str):
    if isinstance(value, bool) and expected is int or not isinstance(value, expected):
        raise TypeError(f"{label} has wrong type")
    return value


def _sha_values(value: object, label: str) -> list[str]:
    values = _array(value, label)
    for item in values:
        _require_sha(item, label)
    return values  # semantic sorted/unique validation happens after construction


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


_OPEN_COMMON = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_OPEN_DIRECTORY = _OPEN_COMMON | getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK: int | None = getattr(os, "O_NONBLOCK", None)
_OPEN_READONLY: int | None = (
    _OPEN_COMMON | _O_NONBLOCK if isinstance(_O_NONBLOCK, int) else None
)


def _readonly_open_flags() -> int:
    if _OPEN_READONLY is None:
        raise PublishError("immutable snapshot-store reads require O_NONBLOCK")
    return os.O_RDONLY | _OPEN_READONLY


@dataclass(frozen=True)
class _HeldDirectory:
    fd: int
    parent: _HeldDirectory | None
    name: str | None
    path: Path
    device: int
    inode: int
    path_bound: bool = True


@dataclass(frozen=True)
class _ReadBinding:
    contents: bytes
    device: int
    inode: int
    mode: int
    nlink: int


def read_inventory(root: Path) -> SourceInventory:
    """Read and canonically parse the committed inventory without mutating the store."""
    held = _open_readonly_root(root)
    try:
        _reject_unowned_lock(held)
        _, _, inventory = _read_inventory_bindings(held)
        _load_test_hook("before_read_inventory_return")
        _reject_unowned_lock(held)
        return inventory
    finally:
        _close_directory(held)


def read_inventory_from_fd(source_root_fd: int) -> SourceInventory:
    """Read the committed inventory relative to a caller-owned directory FD."""
    held = _duplicate_directory_capability(source_root_fd)
    try:
        _reject_unowned_lock(held)
        _, _, inventory = _read_inventory_bindings(held)
        _reject_unowned_lock(held)
        return inventory
    finally:
        _close_directory(held)


def load_snapshot_store(root: Path, inputs: SnapshotStoreInputs) -> LoadedSnapshotStore:
    """Read only an exactly replayable committed store; never repair its residue."""
    held = _open_readonly_root(root)
    try:
        _reject_unowned_lock(held)
        return _load_snapshot_store_held(held, inputs, owned_lock=None)
    finally:
        _close_directory(held)


def load_snapshot_store_from_fd(
    source_root_fd: int, inputs: SnapshotStoreInputs
) -> LoadedSnapshotStore:
    """Load relative to a caller-owned directory FD without closing that FD."""
    if isinstance(source_root_fd, bool) or not isinstance(source_root_fd, int):
        raise TypeError("snapshot store root FD must be an integer")
    try:
        supplied = os.fstat(source_root_fd)
        if not stat.S_ISDIR(supplied.st_mode):
            raise PublishError("snapshot store root FD must name a directory")
        owned_fd = os.dup(source_root_fd)
        opened = os.fstat(owned_fd)
        if not _same_directory(supplied, opened):
            raise PublishError("snapshot store root FD changed while duplicating")
    except PublishError:
        try:
            os.close(owned_fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(owned_fd)
        except (OSError, UnboundLocalError):
            pass
        raise PublishError("snapshot store root FD is unavailable") from exc
    held = _HeldDirectory(
        owned_fd,
        None,
        None,
        Path("<snapshot-store-fd>"),
        opened.st_dev,
        opened.st_ino,
        False,
    )
    try:
        _reject_unowned_lock(held)
        return _load_snapshot_store_held(held, inputs, owned_lock=None)
    finally:
        _close_directory(held)


def _load_snapshot_store_held(
    root: _HeldDirectory,
    inputs: SnapshotStoreInputs,
    *,
    owned_lock: _OwnedLock | None,
) -> LoadedSnapshotStore:
    """The publisher-only path accepts a held lock capability, never a nonce string."""
    _validate_load_inputs(inputs)
    if owned_lock is None:
        _reject_unowned_lock(root)
    else:
        if not isinstance(owned_lock, _OwnedLock):
            raise PublishError("loader lock proof must be an owned lock capability")
        _authenticate_lock(owned_lock, root)

    checksum, inventory_binding, inventory = _read_inventory_bindings(root)
    _validate_loaded_authorities(inventory, inputs)
    directories = _open_inventory_directories(root, inventory)
    try:
        initial_blobs = _read_snapshot_bindings(directories, inventory)
        _validate_blob_bindings(initial_blobs, inventory)
        _load_test_hook("after_initial_reads")

        first_replayed, first_inventory = _fresh_replay_and_inventory(inputs)
        first_expected = _validate_replay_matches_inventory(first_replayed, first_inventory, inventory)
        _validate_blob_bindings(initial_blobs, inventory, first_expected)
        _load_test_hook("between_replays")
        second_replayed, second_inventory = _fresh_replay_and_inventory(inputs)
        second_expected = _validate_replay_matches_inventory(second_replayed, second_inventory, inventory)
        if (
            first_replayed != second_replayed
            or first_inventory != second_inventory
            or first_expected != second_expected
        ):
            raise PublishError("replay changed while loading snapshot store")

        _reject_unowned_lock(root) if owned_lock is None else _authenticate_lock(owned_lock, root)
        final_checksum, final_inventory_binding, final_inventory = _read_inventory_bindings(root)
        if (
            final_checksum != checksum
            or final_inventory_binding != inventory_binding
            or final_inventory != inventory
        ):
            raise PublishError("inventory changed while loading snapshot store")
        final_blobs = _read_snapshot_bindings(directories, inventory)
        if final_blobs != initial_blobs:
            raise PublishError("snapshot blob changed while loading snapshot store")
        _validate_blob_bindings(final_blobs, inventory, first_expected)
        _load_test_hook("after_final_blob_reads")
        frozen_snapshots = _freeze_replayed_snapshots(first_replayed)
        _reject_unowned_lock(root) if owned_lock is None else _authenticate_lock(owned_lock, root)
        return LoadedSnapshotStore(inventory, frozen_snapshots)
    finally:
        _close_inventory_directories(directories)


def _validate_load_inputs(inputs: SnapshotStoreInputs) -> None:
    if not isinstance(inputs, SnapshotStoreInputs):
        raise TypeError("snapshot store inputs must be SnapshotStoreInputs")
    if inputs.current_phase not in {"ACQUIRING", "SOURCES_READY"}:
        raise PublishError("snapshot store can only be loaded while acquiring or sources-ready")
    if not isinstance(inputs.replay, SnapshotReplayInputs) or not isinstance(inputs.bindings, AuthorityBindings):
        raise TypeError("snapshot store inputs contain invalid authorities")
    _validate_bindings(inputs.bindings)


def _validate_loaded_authorities(inventory: SourceInventory, inputs: SnapshotStoreInputs) -> None:
    if inventory.run_id != inputs.replay.run_id or inventory.bindings != inputs.bindings:
        raise PublishError("stored inventory authority bindings differ from inputs")


def _open_readonly_root(root: Path) -> _HeldDirectory:
    if not isinstance(root, Path):
        raise TypeError("snapshot store root must be a pathlib.Path")
    if not root.is_absolute():
        raise ValueError("snapshot store root must be absolute")
    try:
        named = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode):
            raise PublishError("snapshot store root must be an existing directory")
        fd = os.open(root, os.O_RDONLY | _OPEN_DIRECTORY)
        opened = os.fstat(fd)
        if not _same_directory(named, opened):
            raise PublishError("snapshot store root changed while opening")
        return _HeldDirectory(fd, None, None, root, opened.st_dev, opened.st_ino)
    except PublishError:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise PublishError("could not safely open snapshot store root") from exc


def _reject_unowned_lock(root: _HeldDirectory) -> None:
    try:
        _rebind_directory(root)
        os.stat(".snapshot-store.lock", dir_fd=root.fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PublishError("could not inspect snapshot store lock namespace") from exc
    raise LockHeld("snapshot store lock is already held")


def _read_inventory_bindings(root: _HeldDirectory) -> tuple[_ReadBinding, _ReadBinding, SourceInventory]:
    checksum = _read_immutable_binding(root, "source_inventory.sha256")
    if re.fullmatch(rb"[0-9a-f]{64}\n", checksum.contents) is None:
        raise PublishError("inventory checksum must be lowercase SHA-256 plus newline")
    raw_inventory = _read_immutable_binding(root, "source_inventory.json")
    if hashlib.sha256(raw_inventory.contents).hexdigest().encode("ascii") + b"\n" != checksum.contents:
        raise PublishError("inventory checksum differs from inventory bytes")
    try:
        return checksum, raw_inventory, parse_inventory(raw_inventory.contents)
    except (TypeError, ValueError) as exc:
        raise PublishError("stored inventory is invalid") from exc


def _open_inventory_directories(
    root: _HeldDirectory, inventory: SourceInventory
) -> dict[tuple[int, str], _HeldDirectory]:
    directories: dict[tuple[int, str], _HeldDirectory] = {}
    seasons: dict[int, _HeldDirectory] = {}
    try:
        for row in inventory.snapshots:
            season = seasons.get(row.season)
            if season is None:
                season = _open_existing_directory(root, str(row.season), root.path / str(row.season))
                seasons[row.season] = season
            key = (row.season, row.source)
            if key not in directories:
                directories[key] = _open_existing_directory(
                    season, row.source, season.path / row.source
                )
        return directories
    except BaseException:
        _close_inventory_directories(directories)
        for season in reversed(tuple(seasons.values())):
            if all(season is not directory.parent for directory in directories.values()):
                _close_directory(season)
        raise


def _open_existing_directory(parent: _HeldDirectory, name: str, path: Path) -> _HeldDirectory:
    try:
        _rebind_directory(parent)
        fd = os.open(name, os.O_RDONLY | _OPEN_DIRECTORY, dir_fd=parent.fd)
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if not _same_directory(named, opened):
            raise PublishError(f"store directory {name!r} cannot be safely opened")
        return _HeldDirectory(
            fd,
            parent,
            name,
            path,
            opened.st_dev,
            opened.st_ino,
            parent.path_bound,
        )
    except PublishError:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise PublishError(f"store directory {name!r} cannot be safely opened") from exc


def _close_inventory_directories(directories: Mapping[tuple[int, str], _HeldDirectory]) -> None:
    for directory in reversed(tuple(directories.values())):
        _close_directory(directory)
    seasons = {directory.parent for directory in directories.values() if directory.parent is not None}
    for season in seasons:
        _close_directory(season)


def _read_snapshot_bindings(
    directories: Mapping[tuple[int, str], _HeldDirectory], inventory: SourceInventory
) -> tuple[_ReadBinding, ...]:
    return tuple(
        _read_immutable_binding(directories[(row.season, row.source)], Path(row.relative_path).name)
        for row in inventory.snapshots
    )


def _read_immutable_binding(parent: _HeldDirectory, name: str) -> _ReadBinding:
    try:
        _rebind_directory(parent)
        named = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        _require_regular_single_link(named, f"immutable read target {name!r}")
        fd = os.open(name, _readonly_open_flags(), dir_fd=parent.fd)
        try:
            before = os.fstat(fd)
            _require_regular_single_link(before, f"immutable read target {name!r}")
            contents = _read_all(fd)
            after = os.fstat(fd)
            if not _same_file_state(before, after):
                raise PublishError(f"immutable read target changed while reading: {name}")
            _rebind_directory(parent)
            rebound = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            if not _same_file_state(rebound, before):
                raise PublishError(f"immutable read target pathname changed while reading: {name}")
            return _ReadBinding(contents, before.st_dev, before.st_ino, before.st_mode, before.st_nlink)
        finally:
            os.close(fd)
    except PublishError:
        raise
    except (OSError, PublishConflict) as exc:
        raise PublishError(f"immutable read target cannot be safely read: {name}") from exc


def _validate_blob_bindings(
    bindings: tuple[_ReadBinding, ...],
    inventory: SourceInventory,
    expected: Mapping[str, bytes] | None = None,
) -> None:
    if len(bindings) != len(inventory.snapshots):
        raise PublishError("snapshot blob count differs from inventory")
    for binding, row in zip(bindings, inventory.snapshots, strict=True):
        if len(binding.contents) != row.byte_length:
            raise PublishError("snapshot blob length differs from inventory")
        if hashlib.sha256(binding.contents).hexdigest() != row.snapshot_sha256:
            raise PublishError("snapshot blob checksum differs from inventory")
        if expected is not None and binding.contents != expected.get(row.snapshot_sha256):
            raise PublishError("snapshot blob bytes differ from fresh replay")
        try:
            payload = json.loads(binding.contents.decode("utf-8"), object_pairs_hook=_reject_duplicate_object_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PublishError("snapshot blob is not canonical JSON") from exc
        if not isinstance(payload, dict) or binding.contents != canonical_json_bytes(payload) + b"\n":
            raise PublishError("snapshot blob is not canonical JSON")


def _fresh_replay_and_inventory(
    inputs: SnapshotStoreInputs,
) -> tuple[tuple[NormalizedSnapshot, ...], SourceInventory]:
    try:
        replayed = tuple(replay_verified_snapshots(inputs.replay))
        if replayed != tuple(sorted(replayed, key=snapshot_sort_key)):
            raise PublishError("replayed snapshots are not canonically ordered")
        inventory = _build_inventory_from_replayed(inputs, replayed)
    except PublishError:
        raise
    except (TypeError, ValueError) as exc:
        raise PublishError("fresh snapshot replay failed") from exc
    return replayed, inventory


def _validate_replay_matches_inventory(
    replayed: tuple[NormalizedSnapshot, ...], rebuilt: SourceInventory, stored: SourceInventory
) -> Mapping[str, bytes]:
    if rebuilt != stored:
        raise PublishError("stored inventory differs from fresh replay")
    try:
        expected = _expected_snapshot_bytes(replayed, rebuilt)
    except (TypeError, ValueError, PublishError) as exc:
        raise PublishError("fresh replay differs from rebuilt inventory") from exc
    if len(expected) != 81:
        raise PublishError("fresh replay does not contain the closed snapshot set")
    return expected


def _expected_snapshot_bytes(
    replayed: tuple[NormalizedSnapshot, ...], inventory: SourceInventory
) -> Mapping[str, bytes]:
    return _snapshot_bytes_by_sha(replayed, inventory)


def _freeze_replayed_snapshots(
    replayed: tuple[NormalizedSnapshot, ...],
) -> tuple[NormalizedSnapshot, ...]:
    frozen: list[NormalizedSnapshot] = []
    for snapshot in replayed:
        payload = _freeze_json(snapshot.payload)
        if not isinstance(payload, Mapping):
            raise PublishError("replayed snapshot payload must be a mapping")
        copied = NormalizedSnapshot(
            snapshot.schema_version, snapshot.run_id, snapshot.season, snapshot.source,
            snapshot.grain, snapshot.status, tuple(snapshot.logical_query_ids),
            tuple(snapshot.source_query_sidecar_sha256s), tuple(snapshot.split_receipt_sha256s),
            payload, snapshot.canonical_bytes, snapshot.sha256,
        )
        if (
            copied.canonical_bytes != canonical_json_bytes(copied.as_mapping()) + b"\n"
            or hashlib.sha256(copied.canonical_bytes).hexdigest() != copied.sha256
        ):
            raise PublishError("frozen replay snapshot differs from its canonical bytes")
        frozen.append(copied)
    return tuple(frozen)


def _load_test_hook(stage: str) -> None:
    """No-op deterministic race boundary for read-only loader tests."""


def publish_snapshot_store(
    root: Path,
    inventory: SourceInventory,
    inputs: SnapshotStoreInputs,
    *,
    lock_owner: str,
) -> PublishReceipt:
    """Atomically expose a content-addressed snapshot set without replacing any file.

    ``source_inventory.json`` is deliberately linked last: everything before it is
    safe, immutable residue which an identical retry can reuse.
    """
    _validate_publish_arguments(root, inventory, inputs, lock_owner)
    root_dir = _open_root_directory(root)
    try:
        return _publish_snapshot_store_held(
            root_dir, inventory, inputs, lock_owner=lock_owner, detect_committed=False
        ).receipt
    finally:
        _close_directory(root_dir)


def publish_snapshot_store_from_fd(
    source_root_fd: int,
    inventory: SourceInventory,
    inputs: SnapshotStoreInputs,
    *,
    lock_owner: str,
) -> PublishOutcome:
    """Publish from a caller-owned source-root FD without reopening its pathname."""
    _validate_publish_capability_arguments(source_root_fd, inventory, inputs, lock_owner)
    root_dir = _duplicate_directory_capability(source_root_fd)
    try:
        return _publish_snapshot_store_held(
            root_dir, inventory, inputs, lock_owner=lock_owner, detect_committed=True
        )
    finally:
        _close_directory(root_dir)


def _publish_snapshot_store_held(
    root_dir: _HeldDirectory,
    inventory: SourceInventory,
    inputs: SnapshotStoreInputs,
    *,
    lock_owner: str,
    detect_committed: bool,
) -> PublishOutcome:
    """Publish under a held directory capability; this function never opens a path."""
    seasons: dict[int, _HeldDirectory] = {}
    sources: dict[tuple[int, str], _HeldDirectory] = {}
    try:
        lock = _acquire_lock(root_dir, inputs.replay.run_id, lock_owner)
    except BaseException:
        raise
    primary_error: BaseException | None = None
    try:
        replayed, rebuilt = _revalidate_publish_inputs(inventory, inputs)
        expected_snapshots = _snapshot_bytes_by_sha(replayed, inventory)
        raw_inventory = inventory_bytes(inventory)
        inventory_sha256 = hashlib.sha256(raw_inventory).hexdigest()

        if detect_committed and _canonical_inventory_exists_held(root_dir):
            loaded = _load_snapshot_store_held(root_dir, inputs, owned_lock=lock)
            if inventory_bytes(loaded.inventory) != raw_inventory:
                raise PublishConflict("committed inventory bytes differ from requested publication")
            return PublishOutcome(
                PublishReceipt(inventory_sha256, len(inventory.snapshots)), False
            )

        for row in inventory.snapshots:
            season = seasons.get(row.season)
            if season is None:
                season = _open_or_create_directory(
                    root_dir, str(row.season), root_dir.path / str(row.season)
                )
                seasons[row.season] = season
            parent = sources.get((row.season, row.source))
            if parent is None:
                parent = _open_or_create_directory(
                    season, row.source, root_dir.path / str(row.season) / row.source
                )
                sources[(row.season, row.source)] = parent
            _publish_test_hook("after_directory_open_before_temp_publish")
            _rebind_tree(root_dir, seasons.values(), sources.values())
            relative = Path(row.relative_path)
            _publish_immutable(
                parent, relative.name, expected_snapshots[row.snapshot_sha256], inputs.replay.run_id
            )
        _hook_and_rebind("after_blobs", root_dir, seasons.values(), sources.values())

        checksum = inventory_sha256.encode("ascii") + b"\n"
        _publish_immutable(root_dir, "source_inventory.sha256", checksum, inputs.replay.run_id)
        _hook_and_rebind("after_checksum", root_dir, seasons.values(), sources.values())

        _verify_store_members(root_dir, sources, inventory, expected_snapshots, checksum)
        _hook_and_rebind("before_inventory_link", root_dir, seasons.values(), sources.values())
        _publish_immutable(root_dir, "source_inventory.json", raw_inventory, inputs.replay.run_id)
        _hook_and_rebind("after_inventory", root_dir, seasons.values(), sources.values())
        _fsync_held_directory(root_dir)
        _authenticate_lock(lock, root_dir)
        _verify_committed_store(root_dir, sources, inventory, expected_snapshots, raw_inventory, checksum)
        _load_snapshot_store_held(root_dir, inputs, owned_lock=lock)
        _hook_and_rebind("before_lock_release", root_dir, seasons.values(), sources.values())
        return PublishOutcome(PublishReceipt(inventory_sha256, len(inventory.snapshots)), True)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            _release_lock(lock, root_dir)
        except BaseException as cleanup_error:
            # A committed inventory is not enough if this invocation cannot prove it
            # relinquished only the lock it owns.
            if primary_error is not None:
                combined = PublishError("owned lock cleanup failed after primary publication failure")
                combined.primary_error = primary_error  # type: ignore[attr-defined]
                combined.cleanup_error = cleanup_error  # type: ignore[attr-defined]
                raise combined from cleanup_error
            raise PublishError("owned lock cleanup failed") from cleanup_error
        finally:
            for directory in reversed(tuple(sources.values())):
                _close_directory(directory)
            for directory in reversed(tuple(seasons.values())):
                _close_directory(directory)


def _duplicate_directory_capability(source_root_fd: int) -> _HeldDirectory:
    if isinstance(source_root_fd, bool) or not isinstance(source_root_fd, int):
        raise TypeError("snapshot store root FD must be an integer")
    try:
        supplied = os.fstat(source_root_fd)
        if not stat.S_ISDIR(supplied.st_mode):
            raise PublishError("snapshot store root FD must name a directory")
        owned_fd = os.dup(source_root_fd)
        opened = os.fstat(owned_fd)
        if not _same_directory(supplied, opened):
            raise PublishError("snapshot store root FD changed while duplicating")
        return _HeldDirectory(
            owned_fd,
            None,
            None,
            Path("<snapshot-store-fd>"),
            opened.st_dev,
            opened.st_ino,
            False,
        )
    except PublishError:
        try:
            os.close(owned_fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(owned_fd)
        except (OSError, UnboundLocalError):
            pass
        raise PublishError("snapshot store root FD is unavailable") from exc


def _canonical_inventory_exists_held(root: _HeldDirectory) -> bool:
    """Return only a safe committed-inventory presence result under ``root``."""
    try:
        _rebind_directory(root)
        details = os.stat("source_inventory.json", dir_fd=root.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PublishError("could not inspect committed inventory namespace") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise PublishError("committed inventory must be a regular single-link file")
    return True


def _validate_publish_arguments(
    root: Path, inventory: SourceInventory, inputs: SnapshotStoreInputs, lock_owner: str
) -> None:
    if not isinstance(root, Path):
        raise TypeError("snapshot store root must be a pathlib.Path")
    if not root.is_absolute():
        raise ValueError("snapshot store root must be absolute")
    try:
        metadata = os.stat(root, follow_symlinks=False)
    except OSError as exc:
        raise PublishError("snapshot store root is inaccessible") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise PublishError("snapshot store root must be a directory")
    _validate_publish_payload(inventory, inputs, lock_owner)


def _validate_publish_capability_arguments(
    source_root_fd: int, inventory: SourceInventory, inputs: SnapshotStoreInputs, lock_owner: str
) -> None:
    if isinstance(source_root_fd, bool) or not isinstance(source_root_fd, int):
        raise TypeError("snapshot store root FD must be an integer")
    _validate_publish_payload(inventory, inputs, lock_owner)


def _validate_publish_payload(
    inventory: SourceInventory, inputs: SnapshotStoreInputs, lock_owner: str
) -> None:
    if not isinstance(inventory, SourceInventory) or not isinstance(inputs, SnapshotStoreInputs):
        raise TypeError("inventory and inputs must use snapshot-store types")
    if not _is_safe_identity(lock_owner):
        raise ValueError("lock_owner must be a safe nonempty identity")
    if not isinstance(inputs.replay, SnapshotReplayInputs) or not _is_safe_identity(inputs.replay.run_id):
        raise ValueError("replay run_id must be a safe nonempty identity")
    if not _is_safe_identity(inventory.run_id):
        raise ValueError("inventory run_id must be a safe nonempty identity")


def _is_safe_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None
    )


def _revalidate_publish_inputs(
    inventory: SourceInventory, inputs: SnapshotStoreInputs
) -> tuple[tuple[NormalizedSnapshot, ...], SourceInventory]:
    try:
        replayed = tuple(replay_verified_snapshots(inputs.replay))
        if replayed != tuple(sorted(replayed, key=snapshot_sort_key)):
            raise PublishError("replayed snapshots are not canonically ordered")
        rebuilt = build_inventory(inputs)
    except PublishError:
        raise
    except (TypeError, ValueError) as exc:
        raise PublishError("replay or inventory revalidation failed") from exc
    if rebuilt != inventory:
        raise PublishError("supplied inventory differs from replayed inputs")
    if rebuilt.run_id != inputs.replay.run_id:
        raise PublishError("inventory run_id differs from replay run_id")
    return replayed, rebuilt


def _snapshot_bytes_by_sha(
    replayed: tuple[NormalizedSnapshot, ...], inventory: SourceInventory
) -> dict[str, bytes]:
    if len(replayed) != len(inventory.snapshots):
        raise PublishError("replay snapshot count differs from inventory")
    result: dict[str, bytes] = {}
    for snapshot, row in zip(replayed, inventory.snapshots, strict=True):
        if (
            not isinstance(snapshot, NormalizedSnapshot)
            or snapshot.sha256 != row.snapshot_sha256
            or snapshot.run_id != inventory.run_id
            or snapshot.canonical_bytes != canonical_json_bytes(snapshot.as_mapping()) + b"\n"
            or len(snapshot.canonical_bytes) != row.byte_length
            or hashlib.sha256(snapshot.canonical_bytes).hexdigest() != row.snapshot_sha256
        ):
            raise PublishError("replayed snapshot bytes differ from inventory")
        result[row.snapshot_sha256] = snapshot.canonical_bytes
    if len(result) != len(inventory.snapshots):
        raise PublishError("replay snapshot hashes are not unique")
    return result


def _open_root_directory(root: Path) -> _HeldDirectory:
    try:
        fd = os.open(root, os.O_RDONLY | _OPEN_DIRECTORY)
        opened = os.fstat(fd)
        named = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or not _same_directory(named, opened):
            raise PublishError("snapshot store root changed while opening")
        return _HeldDirectory(fd, None, None, root, opened.st_dev, opened.st_ino)
    except PublishError:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise PublishError("could not safely open snapshot store root") from exc


def _open_or_create_directory(parent: _HeldDirectory, name: str, path: Path) -> _HeldDirectory:
    _rebind_directory(parent)
    try:
        os.mkdir(name, 0o700, dir_fd=parent.fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise PublishError(f"could not create store directory {name!r}") from exc
    try:
        fd = os.open(name, os.O_RDONLY | _OPEN_DIRECTORY, dir_fd=parent.fd)
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if not _same_directory(named, opened):
            raise PublishError(f"store directory {name!r} changed while opening")
        os.fsync(fd)
        _fsync_held_directory(parent)
        return _HeldDirectory(
            fd,
            parent,
            name,
            path,
            opened.st_dev,
            opened.st_ino,
            parent.path_bound,
        )
    except PublishError:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise
    except OSError as exc:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise PublishError(f"could not safely open store directory {name!r}") from exc


def _same_directory(named: os.stat_result, opened: os.stat_result) -> bool:
    return stat.S_ISDIR(named.st_mode) and stat.S_ISDIR(opened.st_mode) and (
        named.st_dev,
        named.st_ino,
    ) == (opened.st_dev, opened.st_ino)


def _rebind_directory(directory: _HeldDirectory) -> None:
    try:
        if directory.parent is not None:
            _rebind_directory(directory.parent)
        opened = os.fstat(directory.fd)
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            directory.device,
            directory.inode,
        ):
            raise PublishError("held store directory identity changed")
        if directory.parent is None:
            named = (
                os.stat(directory.path, follow_symlinks=False)
                if directory.path_bound
                else opened
            )
        else:
            named = os.stat(directory.name, dir_fd=directory.parent.fd, follow_symlinks=False)
        if not _same_directory(named, opened):
            raise PublishError("store directory pathname no longer binds held directory")
        if directory.path_bound:
            named_path = os.stat(directory.path, follow_symlinks=False)
            if not _same_directory(named_path, opened):
                raise PublishError("store directory pathname no longer binds held directory")
    except PublishError:
        raise
    except OSError as exc:
        raise PublishError("could not rebind held store directory") from exc


def _rebind_tree(
    root: _HeldDirectory,
    seasons: object,
    sources: object,
) -> None:
    _rebind_directory(root)
    for directory in seasons:  # type: ignore[union-attr]
        _rebind_directory(directory)
    for directory in sources:  # type: ignore[union-attr]
        _rebind_directory(directory)


def _hook_and_rebind(
    stage: str, root: _HeldDirectory, seasons: object, sources: object
) -> None:
    _publish_test_hook(stage)
    _rebind_tree(root, seasons, sources)


def _close_directory(directory: _HeldDirectory) -> None:
    try:
        os.close(directory.fd)
    except OSError:
        pass


def _acquire_lock(root: _HeldDirectory, run_id: str, lock_owner: str) -> _OwnedLock:
    name = ".snapshot-store.lock"
    path = root.path / name
    nonce = secrets.token_hex(32)
    contents = canonical_json_bytes({"lock_owner": lock_owner, "nonce": nonce, "run_id": run_id}) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_COMMON
    try:
        fd = os.open(name, flags, 0o600, dir_fd=root.fd)
    except FileExistsError as exc:
        raise LockHeld("snapshot store lock is already held") from exc
    except OSError as exc:
        raise PublishError("could not acquire snapshot store lock") from exc
    try:
        _write_all(fd, contents)
        details = os.fstat(fd)
        _require_regular_single_link(details, "new lock")
        lock = _OwnedLock(
            fd, path, details.st_dev, details.st_ino, nonce, contents,
            run_id, lock_owner, _LOCK_ISSUER,
        )
        _publish_test_hook("after_lock_write_before_acquire_fsync")
        os.fsync(fd)
        _fsync_held_directory(root)
        return lock
    except BaseException:
        try:
            try:
                _authenticate_lock(lock, root)
                os.unlink(name, dir_fd=root.fd)
                _fsync_held_directory(root)
            except (OSError, SnapshotStoreError, UnboundLocalError):
                pass
        finally:
            os.close(fd)
        raise


def _authenticate_lock(lock: _OwnedLock, root: _HeldDirectory) -> None:
    try:
        _rebind_directory(root)
        expected_path = root.path / ".snapshot-store.lock"
        expected_contents = canonical_json_bytes({
            "lock_owner": lock.lock_owner,
            "nonce": lock.nonce,
            "run_id": lock.run_id,
        }) + b"\n"
        if (
            lock.issuer is not _LOCK_ISSUER
            or lock.path != expected_path
            or lock.path.name != ".snapshot-store.lock"
            or lock.path.parent != root.path
            or not _is_safe_identity(lock.run_id)
            or not _is_safe_identity(lock.lock_owner)
            or not isinstance(lock.nonce, str)
            or re.fullmatch(r"[0-9a-f]{64}", lock.nonce) is None
            or lock.contents != expected_contents
        ):
            raise PublishError("owned lock does not bind the canonical lock namespace")
        descriptor = os.fstat(lock.fd)
        _require_regular_single_link(descriptor, "owned lock")
        named = os.stat(lock.path.name, dir_fd=root.fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (lock.device, lock.inode):
            raise PublishError("owned lock identity changed")
        read_fd = os.open(lock.path.name, _readonly_open_flags(), dir_fd=root.fd)
        try:
            opened = os.fstat(read_fd)
            _require_regular_single_link(opened, "owned lock")
            if (opened.st_dev, opened.st_ino) != (lock.device, lock.inode):
                raise PublishError("owned lock path changed")
            if _read_all(read_fd) != lock.contents:
                raise PublishError("owned lock contents changed")
            final = os.fstat(read_fd)
            rebound = os.stat(lock.path.name, dir_fd=root.fd, follow_symlinks=False)
            if not _same_file_state(final, opened) or not _same_file_state(rebound, opened):
                raise PublishError("owned lock changed during authentication")
        finally:
            os.close(read_fd)
    except PublishError:
        raise
    except OSError as exc:
        raise PublishError("could not authenticate owned lock") from exc


def _release_lock(lock: _OwnedLock, root: _HeldDirectory) -> None:
    try:
        _authenticate_lock(lock, root)
        _publish_test_hook("before_lock_unlink")
        _authenticate_lock(lock, root)
        os.unlink(lock.path.name, dir_fd=root.fd)
        _fsync_held_directory(root)
    except PublishError:
        raise
    except SnapshotStoreError as exc:
        raise PublishError("could not release owned lock") from exc
    except OSError as exc:
        raise PublishError("could not release owned lock") from exc
    finally:
        try:
            os.close(lock.fd)
        except OSError as exc:
            # Closing a descriptor is part of safely completing lock ownership.
            raise PublishError("could not close owned lock") from exc


def _publish_immutable(parent: _HeldDirectory, name: str, expected: bytes, run_id: str) -> None:
    _rebind_directory(parent)
    temp = f".tmp-{run_id}-{secrets.token_hex(16)}"
    fd: int | None = None
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN_COMMON, 0o600, dir_fd=parent.fd)
        _write_all(fd, expected)
        details = os.fstat(fd)
        _require_regular_single_link(details, "temporary publication file")
        os.fsync(fd)
        try:
            os.link(temp, name, src_dir_fd=parent.fd, dst_dir_fd=parent.fd, follow_symlinks=False)
        except FileExistsError:
            _verify_immutable(parent, name, expected, retry_run_id=run_id)
        _unlink_owned_temp(parent, temp, fd, details)
        fd = None
        _fsync_held_directory(parent)
    except PublishConflict:
        raise
    except OSError as exc:
        raise PublishError(f"could not publish immutable file {name!r}") from exc
    finally:
        if fd is not None:
            try:
                details = os.fstat(fd)
                _unlink_owned_temp(parent, temp, fd, details)
            except (OSError, PublishError):
                pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _unlink_owned_temp(
    parent: _HeldDirectory, name: str, fd: int, expected: os.stat_result
) -> None:
    current = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
    descriptor = os.fstat(fd)
    if (
        (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        or (descriptor.st_dev, descriptor.st_ino) != (expected.st_dev, expected.st_ino)
    ):
        raise PublishError("temporary publication file identity changed")
    if not stat.S_ISREG(current.st_mode) or current.st_nlink not in {1, 2}:
        raise PublishError("temporary publication file is no longer a regular owned file")
    os.unlink(name, dir_fd=parent.fd)
    os.close(fd)


def _verify_store_members(
    root: _HeldDirectory,
    sources: Mapping[tuple[int, str], _HeldDirectory],
    inventory: SourceInventory,
    snapshots: Mapping[str, bytes],
    checksum: bytes,
) -> None:
    for row in inventory.snapshots:
        _verify_immutable(sources[(row.season, row.source)], Path(row.relative_path).name, snapshots[row.snapshot_sha256])
    _verify_immutable(root, "source_inventory.sha256", checksum)


def _verify_committed_store(
    root: _HeldDirectory,
    sources: Mapping[tuple[int, str], _HeldDirectory],
    inventory: SourceInventory,
    snapshots: Mapping[str, bytes],
    raw_inventory: bytes,
    checksum: bytes,
) -> None:
    _verify_store_members(root, sources, inventory, snapshots, checksum)
    _verify_immutable(root, "source_inventory.json", raw_inventory)
    try:
        if hashlib.sha256(raw_inventory).hexdigest().encode("ascii") + b"\n" != checksum:
            raise PublishError("inventory checksum differs from inventory bytes")
    except PublishError:
        raise


def _verify_immutable(
    parent: _HeldDirectory, name: str, expected: bytes, *, retry_run_id: str | None = None
) -> None:
    try:
        _rebind_directory(parent)
        named = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode) or named.st_nlink not in {1, 2}:
            raise PublishConflict("immutable publication target must be a regular one- or two-link file")
        fd = os.open(name, _readonly_open_flags(), dir_fd=parent.fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink not in {1, 2}:
                raise PublishConflict("immutable publication target must be a regular one- or two-link file")
            actual = _read_all(fd)
            after = os.fstat(fd)
            if not _same_file_state(before, after):
                raise PublishConflict(f"immutable target changed while reading: {name}")
            _publish_test_hook("after_target_read_before_rebind")
            _rebind_directory(parent)
            rebound = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
            if not _same_file_state(rebound, before):
                raise PublishConflict(f"immutable target pathname changed while reading: {name}")
        finally:
            os.close(fd)
    except PublishConflict:
        raise
    except OSError as exc:
        raise PublishConflict(f"immutable target cannot be safely read: {name}") from exc
    if actual != expected or hashlib.sha256(actual).digest() != hashlib.sha256(expected).digest():
        raise PublishConflict(f"immutable target bytes differ: {name}")
    if before.st_nlink == 2:
        if retry_run_id is None:
            raise PublishConflict(f"immutable target has an untrusted second link: {name}")
        _converge_owned_linked_temp(parent, name, before, expected, retry_run_id)
        _verify_immutable(parent, name, expected)


def _converge_owned_linked_temp(
    parent: _HeldDirectory,
    target_name: str,
    target: os.stat_result,
    expected: bytes,
    run_id: str,
) -> None:
    """Remove only the one authenticated temp hard-link left by a crashed publisher."""
    _rebind_directory(parent)
    prefix = f".tmp-{run_id}-"
    try:
        aliases: list[str] = []
        for entry in os.listdir(parent.fd):
            if entry == target_name:
                continue
            details = os.stat(entry, dir_fd=parent.fd, follow_symlinks=False)
            if (details.st_dev, details.st_ino) == (target.st_dev, target.st_ino):
                aliases.append(entry)
        if len(aliases) != 1 or not aliases[0].startswith(prefix):
            raise PublishConflict("two-link immutable target has no unique owned temp residue")
        alias = aliases[0]
        details = os.stat(alias, dir_fd=parent.fd, follow_symlinks=False)
        if not stat.S_ISREG(details.st_mode) or not _same_file_state(details, target):
            raise PublishConflict("linked temp residue does not exactly bind immutable target")
        fd = os.open(alias, _readonly_open_flags(), dir_fd=parent.fd)
        try:
            opened = os.fstat(fd)
            alias_bytes = _read_all(fd)
            if (
                not _same_file_state(opened, target)
                or alias_bytes != expected
                or hashlib.sha256(alias_bytes).digest() != hashlib.sha256(expected).digest()
            ):
                raise PublishConflict("linked temp residue bytes differ from immutable target")
            rebound = os.stat(alias, dir_fd=parent.fd, follow_symlinks=False)
            if not _same_file_state(rebound, opened):
                raise PublishConflict("linked temp residue changed while reading")
        finally:
            os.close(fd)
        os.unlink(alias, dir_fd=parent.fd)
        _fsync_held_directory(parent)
        rebound_target = os.stat(target_name, dir_fd=parent.fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(rebound_target.st_mode)
            or rebound_target.st_nlink != 1
            or (rebound_target.st_dev, rebound_target.st_ino) != (target.st_dev, target.st_ino)
        ):
            raise PublishConflict("immutable target did not converge after owned temp cleanup")
    except PublishConflict:
        raise
    except OSError as exc:
        raise PublishConflict("could not safely converge linked temp residue") from exc


def _same_file_state(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino, first.st_mode, first.st_nlink) == (
        second.st_dev,
        second.st_ino,
        second.st_mode,
        second.st_nlink,
    )


def _require_regular_single_link(details: os.stat_result, label: str) -> None:
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        raise PublishConflict(f"{label} must be a regular single-link file")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise PublishError("short write while publishing immutable file")
        view = view[written:]


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 131072)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _fsync_held_directory(directory: _HeldDirectory) -> None:
    _rebind_directory(directory)
    os.fsync(directory.fd)


def _publish_test_hook(stage: str) -> None:
    """No-op fault/race boundary used by deterministic publication tests."""


__all__ = [
    "AuthorityBinding", "TerminalCheckpointBinding", "AuthorityBindings", "SnapshotStoreInputs",
    "SnapshotInventoryRow", "SourceSeasonInventory", "SourceInventory", "build_inventory",
    "inventory_bytes", "parse_inventory", "PublishReceipt", "PublishOutcome", "LoadedSnapshotStore",
    "SnapshotStoreError", "PublishConflict", "LockHeld", "PublishError", "read_inventory",
    "read_inventory_from_fd",
    "load_snapshot_store", "load_snapshot_store_from_fd", "publish_snapshot_store",
    "publish_snapshot_store_from_fd",
]
