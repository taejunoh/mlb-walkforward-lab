"""Immutable, canonical source-inventory contract for the v5.2 snapshot set."""

from __future__ import annotations

import hashlib
import json
import re
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


__all__ = [
    "AuthorityBinding", "TerminalCheckpointBinding", "AuthorityBindings", "SnapshotStoreInputs",
    "SnapshotInventoryRow", "SourceSeasonInventory", "SourceInventory", "build_inventory",
    "inventory_bytes", "parse_inventory",
]
