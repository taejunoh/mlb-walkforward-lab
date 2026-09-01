"""Authenticate the immutable V5.2 sources required by model improvement."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Sequence

from mlb_backtest import execution_registry, protocols, v52_verify_sources

from .canonical import canonical_json_bytes, read_regular_bytes, write_immutable_json
from .contracts import (
    _require_descriptor,
    _require_exact_mapping,
    _require_sequence,
    _require_sha256,
)
from .run_spec import ModelImprovementRunSpec, load_verified_run_spec, run_spec_sha256


_FIELDS = frozenset(
    {
        "run_spec_sha256",
        "source_run_spec_sha256",
        "acquisition_contract_sha256",
        "checkpoint_sha256",
        "inventory_sha256",
        "validation_receipts_sha256",
        "snapshot_replay_sha256",
        "required_years",
        "status",
    }
)
_YEARS = tuple(range(2018, 2025))
_FAMILIES = frozenset(
    {
        "baseball_savant_statcast",
        "mlb_schedule_game_feed",
        "mlb_stats_pitcher_bf",
        "mlb_stats_pitching",
        "prior_schedule_history",
        "venue_team_timezone_table",
    }
)


@dataclass(frozen=True, slots=True)
class SourceGateReceipt:
    run_spec_sha256: str
    source_run_spec_sha256: str
    acquisition_contract_sha256: str
    checkpoint_sha256: str
    inventory_sha256: str
    validation_receipts_sha256: tuple[str, ...]
    snapshot_replay_sha256: str
    required_years: tuple[int, ...]
    status: str
    artifact_sha256: str
    path: str

    def __post_init__(self) -> None:
        for field in (
            "run_spec_sha256", "source_run_spec_sha256",
            "acquisition_contract_sha256", "checkpoint_sha256",
            "inventory_sha256", "snapshot_replay_sha256",
        ):
            object.__setattr__(self, field, _require_sha256(getattr(self, field), field))
        receipts = tuple(
            _require_sha256(value, "validation_receipts_sha256")
            for value in _require_sequence(
                self.validation_receipts_sha256, "validation_receipts_sha256"
            )
        )
        if not receipts:
            raise ValueError("source gate requires validation receipts")
        years = _require_sequence(self.required_years, "required_years")
        if years != _YEARS or any(type(year) is not int for year in years):
            raise ValueError("source gate required years differ")
        if self.status != "SOURCES_READY":
            raise ValueError("source gate status differs")
        artifact_sha256, path = _require_descriptor(self.artifact_sha256, self.path)
        object.__setattr__(self, "validation_receipts_sha256", receipts)
        object.__setattr__(self, "required_years", _YEARS)
        object.__setattr__(self, "artifact_sha256", artifact_sha256)
        object.__setattr__(self, "path", path)

    def as_mapping(self) -> dict[str, object]:
        return {
            "run_spec_sha256": self.run_spec_sha256,
            "source_run_spec_sha256": self.source_run_spec_sha256,
            "acquisition_contract_sha256": self.acquisition_contract_sha256,
            "checkpoint_sha256": self.checkpoint_sha256,
            "inventory_sha256": self.inventory_sha256,
            "validation_receipts_sha256": list(self.validation_receipts_sha256),
            "snapshot_replay_sha256": self.snapshot_replay_sha256,
            "required_years": list(self.required_years),
            "status": self.status,
        }

    @classmethod
    def from_payload_mapping(
        cls, payload: Mapping[str, object], artifact_sha256: str, path: str
    ) -> "SourceGateReceipt":
        values = _require_exact_mapping(payload, _FIELDS, "source gate payload")
        return cls(**dict(values), artifact_sha256=artifact_sha256, path=path)


def _revalidate_activated_spec(presented: ModelImprovementRunSpec) -> ModelImprovementRunSpec:
    """Return a fresh activation, never trusting a caller-held descriptor."""
    if not isinstance(presented, ModelImprovementRunSpec):
        raise ValueError("source gate requires a verified RunSpec activation")
    activation = presented.verified_activation
    if activation is None:
        raise ValueError("source gate requires a verified RunSpec activation")
    fresh = load_verified_run_spec(Path(activation.declaration_path))
    fresh_activation = fresh.verified_activation
    if fresh_activation is None or fresh_activation != activation:
        raise ValueError("source gate activation descriptor differs")
    fresh_digest = run_spec_sha256(fresh)
    if fresh_digest != activation.run_spec_sha256 or fresh_digest != run_spec_sha256(presented):
        raise ValueError("source gate RunSpec activation digest differs")
    if canonical_json_bytes(fresh.as_mapping()) != canonical_json_bytes(presented.as_mapping()):
        raise ValueError("source gate RunSpec declaration differs")
    return fresh


def _authority_root(spec: ModelImprovementRunSpec) -> Path:
    activation = spec.verified_activation
    if activation is None:
        raise ValueError("source gate requires a verified RunSpec activation")
    repository_root = Path(activation.declaration_path).parents[2]
    root = repository_root / spec.source_authority.authority_root
    # ``read_regular_bytes`` will also prove every directory component no-follow.
    if root.is_absolute() is False or not root.parts:
        raise ValueError("source gate authority root differs")
    return root


def _bound_paths(spec: ModelImprovementRunSpec) -> tuple[Path, dict[str, Path]]:
    root = _authority_root(spec)
    authority = spec.source_authority
    entries = {
        "source_run_spec": authority.source_run_spec,
        "acquisition_contract": authority.acquisition_contract,
        "terminal_checkpoint": authority.terminal_checkpoint,
        "inventory": authority.inventory,
    }
    paths: dict[str, Path] = {}
    for name, binding in entries.items():
        paths[name] = root / binding.path
    for index, binding in enumerate(authority.validation_receipts):
        paths[f"validation_receipt_{index}"] = root / binding.path
    return root, paths


def _verify_bound_files(spec: ModelImprovementRunSpec, paths: Mapping[str, Path]) -> None:
    authority = spec.source_authority
    expected: dict[str, str] = {
        "source_run_spec": authority.source_run_spec.sha256,
        "acquisition_contract": authority.acquisition_contract.sha256,
        "terminal_checkpoint": authority.terminal_checkpoint.sha256,
        "inventory": authority.inventory.sha256,
    }
    expected.update(
        {
            f"validation_receipt_{index}": binding.sha256
            for index, binding in enumerate(authority.validation_receipts)
        }
    )
    if set(paths) != set(expected):
        raise ValueError("source gate bound path set differs")
    for name, path in paths.items():
        if sha256(read_regular_bytes(path, f"source gate {name}")).hexdigest() != expected[name]:
            raise ValueError(f"source gate bound digest differs: {name}")


def _snapshot_projection(snapshot: object) -> dict[str, object]:
    if not hasattr(snapshot, "season") or not hasattr(snapshot, "source"):
        raise ValueError("source replay snapshot differs")
    season = getattr(snapshot, "season")
    source = getattr(snapshot, "source")
    if type(season) is not int or source not in _FAMILIES:
        raise ValueError("source replay snapshot differs")
    mapper = getattr(snapshot, "as_mapping", None)
    if callable(mapper):
        value = mapper()
    else:
        value = {
            "season": season,
            "source": source,
            "grain": getattr(snapshot, "grain", None),
            "payload": getattr(snapshot, "payload", None),
        }
    if not isinstance(value, Mapping):
        raise ValueError("source replay snapshot differs")
    return dict(value)


def _snapshot_digest(snapshots: Sequence[object]) -> str:
    if len(snapshots) != 81:
        raise ValueError("source replay requires exactly 81 snapshots")
    projections = tuple(_snapshot_projection(snapshot) for snapshot in snapshots)
    encoded = tuple(canonical_json_bytes(projection) for projection in projections)
    if len(set(encoded)) != 81:
        raise ValueError("source replay snapshots are not unique")
    pairs = {(snapshot["season"], snapshot["source"]) for snapshot in projections}
    expected_pairs = {(year, source) for year in _YEARS for source in _FAMILIES}
    if pairs != expected_pairs or len(pairs) != 42:
        raise ValueError("source replay year/source families differ")
    ordered = [json.loads(value) for value in sorted(encoded)]
    return sha256(canonical_json_bytes(ordered)).hexdigest()


def _receipt_path(spec: ModelImprovementRunSpec) -> Path:
    return spec.output_path("source_gate", "source_gate_receipt.json")


def _load_bound_receipt(spec: ModelImprovementRunSpec, path: Path, digest: str) -> SourceGateReceipt:
    receipt = load_source_gate_receipt(path, digest)
    if receipt.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("source gate RunSpec binding differs")
    if receipt.inventory_sha256 != spec.source_authority.inventory.sha256:
        raise ValueError("source gate inventory binding differs")
    if path.resolve() != _receipt_path(spec).resolve():
        raise ValueError("source gate receipt path differs")
    return receipt


def verify_source_gate(spec: ModelImprovementRunSpec) -> SourceGateReceipt:
    """Freshly authenticate all declared V5.2 source evidence, then seal it."""
    fresh = _revalidate_activated_spec(spec)
    authority_root, paths = _bound_paths(fresh)
    _verify_bound_files(fresh, paths)
    source_spec = protocols.load_source_run_spec(paths["source_run_spec"])
    receipt_paths = tuple(
        paths[f"validation_receipt_{index}"]
        for index in range(len(fresh.source_authority.validation_receipts))
    )
    execution_registry.validate_sources_ready_evidence_set(
        authority_root, source_spec, inventory_path=paths["inventory"],
        checkpoint_path=paths["terminal_checkpoint"], validation_receipts=receipt_paths,
    )
    checkpoint = execution_registry.load_terminal_acquisition_checkpoint(authority_root, source_spec)
    expected_checkpoint = fresh.source_authority.terminal_checkpoint.sha256
    if (
        not isinstance(checkpoint, execution_registry.AcquisitionCheckpoint)
        or checkpoint.receipt_sha256 != expected_checkpoint
    ):
        raise ValueError("source gate terminal checkpoint binding differs")
    inputs = v52_verify_sources.load_snapshot_replay_inputs(
        authority_root, source_spec, acquisition_contract_path=paths["acquisition_contract"]
    )
    replay_digest = _snapshot_digest(v52_verify_sources.replay_verified_snapshots(inputs))
    _verify_bound_files(fresh, paths)
    provisional = SourceGateReceipt(
        run_spec_sha256(fresh), fresh.source_authority.source_run_spec.sha256,
        fresh.source_authority.acquisition_contract.sha256,
        fresh.source_authority.terminal_checkpoint.sha256,
        fresh.source_authority.inventory.sha256,
        tuple(item.sha256 for item in fresh.source_authority.validation_receipts),
        replay_digest, _YEARS, "SOURCES_READY", "0" * 64, str(_receipt_path(fresh)),
    )
    payload = provisional.as_mapping()
    path = _receipt_path(fresh)
    payload_bytes = canonical_json_bytes(payload)
    digest = sha256(payload_bytes).hexdigest()
    # The sidecar is a private precondition; the primary is the sole published
    # commit point.  A sidecar-only residue is not loadable and is safely reused
    # only when a later call proves it contains these exact immutable bytes.
    write_immutable_json(
        path.with_suffix(path.suffix + ".sha256"), {"artifact_sha256": digest}
    )
    write_immutable_json(path, payload)
    return _load_bound_receipt(fresh, path, digest)


def load_source_gate_receipt(path: Path, expected_sha256: str) -> SourceGateReceipt:
    """Load one canonical source-gate artifact with its independent sidecar."""
    expected = _require_sha256(expected_sha256, "expected_sha256")
    raw = read_regular_bytes(path, "source gate receipt")
    actual = sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError("source gate digest differs")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("source gate receipt is not JSON") from exc
    if not isinstance(payload, Mapping) or raw != canonical_json_bytes(payload):
        raise ValueError("source gate receipt is not canonical JSON")
    receipt = SourceGateReceipt.from_payload_mapping(payload, actual, str(path))
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar_raw = read_regular_bytes(sidecar, "source gate sidecar")
    try:
        sidecar_payload = json.loads(sidecar_raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("source gate sidecar is not JSON") from exc
    if (
        sidecar_raw != canonical_json_bytes(sidecar_payload)
        or not isinstance(sidecar_payload, Mapping)
        or set(sidecar_payload) != {"artifact_sha256"}
        or sidecar_payload["artifact_sha256"] != actual
    ):
        raise ValueError("source gate sidecar differs")
    return receipt


def require_source_gate(spec: ModelImprovementRunSpec) -> SourceGateReceipt:
    """Require the prior receipt and prove it again against a fresh activation."""
    fresh = _revalidate_activated_spec(spec)
    path = _receipt_path(fresh)
    existing = _load_bound_receipt(
        fresh, path, sha256(read_regular_bytes(path, "source gate receipt")).hexdigest()
    )
    verified = verify_source_gate(fresh)
    if (
        existing.as_mapping() != verified.as_mapping()
        or existing.artifact_sha256 != verified.artifact_sha256
        or Path(existing.path).resolve() != Path(verified.path).resolve()
    ):
        raise ValueError("source gate second verification differs")
    return verified
