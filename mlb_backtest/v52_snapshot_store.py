"""Immutable, canonical source-inventory contract for the v5.2 snapshot set."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import jsonschema
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from .v52_source_contracts import canonical_json_bytes
from .v52_verify_sources import (
    SnapshotReplayInputs,
    NormalizedSnapshot,
    replay_verified_snapshots,
    snapshot_sort_key,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
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
    if not isinstance(inputs, SnapshotStoreInputs):
        raise TypeError("snapshot store inputs must be SnapshotStoreInputs")
    if inputs.current_phase not in {"ACQUIRING", "SOURCES_READY"}:
        raise ValueError("source inventory is only available while acquiring or sources-ready")
    if not isinstance(inputs.replay, SnapshotReplayInputs) or not isinstance(inputs.bindings, AuthorityBindings):
        raise TypeError("snapshot store inputs contain invalid authorities")
    _validate_bindings(inputs.bindings)
    if not isinstance(inputs.replay.run_id, str) or not inputs.replay.run_id:
        raise ValueError("replay run_id must be a nonempty string")
    snapshots = replay_verified_snapshots(inputs.replay)
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
        tuple(sorted(snapshot.source_query_sidecar_sha256s)), tuple(sorted(snapshot.split_receipt_sha256s)),
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
        if not isinstance(values, tuple) or values != tuple(sorted(set(values))):
            raise ValueError(f"{label} must be sorted and unique")
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
    schema = json.loads((Path(__file__).resolve().parent.parent / "schemas" / "v5_2_source_inventory.schema.json").read_text(encoding="utf-8"))
    try:
        Draft202012Validator.check_schema(schema)
        jsonschema.validate(dict(payload), schema, format_checker=FormatChecker())
    except (ValidationError, ValueError) as exc:
        raise ValueError("source inventory schema validation failed") from exc


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


@dataclass(frozen=True)
class _HeldDirectory:
    fd: int
    parent: _HeldDirectory | None
    name: str | None
    path: Path
    device: int
    inode: int


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
    seasons: dict[int, _HeldDirectory] = {}
    sources: dict[tuple[int, str], _HeldDirectory] = {}
    try:
        lock = _acquire_lock(root_dir, inputs.replay.run_id, lock_owner)
    except BaseException:
        _close_directory(root_dir)
        raise
    primary_error: BaseException | None = None
    try:
        replayed, rebuilt = _revalidate_publish_inputs(inventory, inputs)
        expected_snapshots = _snapshot_bytes_by_sha(replayed, inventory)
        raw_inventory = inventory_bytes(inventory)
        inventory_sha256 = hashlib.sha256(raw_inventory).hexdigest()

        for row in inventory.snapshots:
            season = seasons.get(row.season)
            if season is None:
                season = _open_or_create_directory(root_dir, str(row.season), root / str(row.season))
                seasons[row.season] = season
            parent = sources.get((row.season, row.source))
            if parent is None:
                parent = _open_or_create_directory(season, row.source, root / str(row.season) / row.source)
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
        _hook_and_rebind("before_lock_release", root_dir, seasons.values(), sources.values())
        return PublishReceipt(inventory_sha256, len(inventory.snapshots))
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
            _close_directory(root_dir)


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
        return _HeldDirectory(fd, parent, name, path, opened.st_dev, opened.st_ino)
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
            named = os.stat(directory.path, follow_symlinks=False)
        else:
            named = os.stat(directory.name, dir_fd=directory.parent.fd, follow_symlinks=False)
        named_path = os.stat(directory.path, follow_symlinks=False)
        if not _same_directory(named, opened) or not _same_directory(named_path, opened):
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
        lock = _OwnedLock(fd, path, details.st_dev, details.st_ino, nonce, contents)
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
        descriptor = os.fstat(lock.fd)
        _require_regular_single_link(descriptor, "owned lock")
        named = os.stat(lock.path.name, dir_fd=root.fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (lock.device, lock.inode):
            raise PublishError("owned lock identity changed")
        read_fd = os.open(lock.path.name, os.O_RDONLY | _OPEN_COMMON, dir_fd=root.fd)
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
        fd = os.open(name, os.O_RDONLY | _OPEN_COMMON, dir_fd=parent.fd)
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
        fd = os.open(alias, os.O_RDONLY | _OPEN_COMMON, dir_fd=parent.fd)
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
    "inventory_bytes", "parse_inventory", "PublishReceipt", "SnapshotStoreError",
    "PublishConflict", "LockHeld", "PublishError", "publish_snapshot_store",
]
