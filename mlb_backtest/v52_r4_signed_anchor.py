"""Closed Ed25519 authority anchor for R4 physical custody issuance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from jsonschema import Draft202012Validator


DOMAIN_MESSAGE = b"MLB-R4-CUSTODY-ANCHOR-v1\0"
MAX_AUTHORITY_ANCHOR_BYTES = 16_384
SCHEMA_VERSION = "v5.2-r4-physical-custody-authority-anchor-v1"
KIND = "MLB_R4_PHYSICAL_CUSTODY_AUTHORITY"
SOURCE_APPROVAL = MappingProxyType({
    "logical_raw_root": "data/v5_1_bt/raw",
    "protected_inventory_path": "audit/v5_1_bt/protected_raw_inventory.json",
    "protected_inventory_sha256": "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6",
})
_ARTIFACT_PATHS = {
    "inventory": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/protected_raw_inventory_r4.json",
    "receipt": "audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_transition_r4.json",
    "contract": "protocols/v5_2_qd/historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json",
    "run_spec": "protocols/v5_2_qd/historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json",
}
_AMENDMENT_PATHS = {
    "design": "docs/superpowers/specs/2026-08-05-v5-2-r4-signed-custody-anchor-design.md",
    "implementation_plan": "docs/superpowers/plans/2026-08-05-v5-2-r4-signed-custody-anchor.md",
}
_SCHEMA_PATH = Path(__file__).resolve().parent.parent / (
    "schemas/v5_2_r4_physical_custody_authority_anchor.schema.json"
)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list) or isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _sha256(data: bytes, label: str) -> str:
    if not isinstance(data, bytes):
        raise ValueError(f"{label} must be bytes")
    return hashlib.sha256(data).hexdigest()


def _mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} differs")
    return value


def _required_mapping(value: object, required: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not required.issubset(value):
        raise ValueError(f"{label} differs")
    return value


def _digest(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} differs")
    return value


def _blob_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} differs")
    return value


def _key_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("ed25519-sha256:")
        or len(value) != len("ed25519-sha256:") + 64
    ):
        raise ValueError("anchor key ID differs")
    _digest(value.removeprefix("ed25519-sha256:"), "anchor key ID")
    return value


def _validator() -> Draft202012Validator:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("anchor schema is unavailable") from exc
    return Draft202012Validator(schema)


def _validate_document(document: object, *, expected_key_id: str | None = None) -> Mapping[str, object]:
    errors = tuple(_validator().iter_errors(document))
    if errors:
        raise ValueError("authority anchor schema differs")
    doc = _mapping(
        document,
        {
            "schema_version", "kind", "key_id", "source_approval", "artifacts", "amendment",
            "custody_identity_manifest_sha256", "receipt_comparison",
        },
        "authority anchor",
    )
    if doc["schema_version"] != SCHEMA_VERSION or doc["kind"] != KIND:
        raise ValueError("authority anchor constants differ")
    key_id = _key_id(doc["key_id"])
    if expected_key_id is not None and key_id != _key_id(expected_key_id):
        raise ValueError("authority anchor key ID differs")
    approval = _mapping(doc["source_approval"], set(SOURCE_APPROVAL), "source approval")
    if dict(approval) != dict(SOURCE_APPROVAL):
        raise ValueError("source approval differs")
    artifacts = _mapping(doc["artifacts"], set(_ARTIFACT_PATHS), "artifact bindings")
    for name, path in _ARTIFACT_PATHS.items():
        artifact = _mapping(artifacts[name], {"path", "sha256"}, f"{name} binding")
        if artifact["path"] != path:
            raise ValueError(f"{name} path differs")
        _digest(artifact["sha256"], f"{name} SHA-256")
    amendment = _mapping(doc["amendment"], set(_AMENDMENT_PATHS), "amendment bindings")
    for name, path in _AMENDMENT_PATHS.items():
        binding = _mapping(amendment[name], {"path", "sha256", "git_blob_id"}, f"{name} binding")
        if binding["path"] != path:
            raise ValueError(f"{name} path differs")
        _digest(binding["sha256"], f"{name} SHA-256")
        _blob_id(binding["git_blob_id"], f"{name} Git blob ID")
    _digest(doc["custody_identity_manifest_sha256"], "custody identity manifest SHA-256")
    comparison = _mapping(
        doc["receipt_comparison"],
        {
            "source_content_manifest_sha256", "snapshot_content_manifest_sha256",
            "source_logical_binding_manifest_sha256", "snapshot_logical_binding_manifest_sha256",
        },
        "receipt comparison",
    )
    for name, digest in comparison.items():
        _digest(digest, name)
    if (
        comparison["source_content_manifest_sha256"]
        != comparison["snapshot_content_manifest_sha256"]
        or comparison["source_logical_binding_manifest_sha256"]
        != comparison["snapshot_logical_binding_manifest_sha256"]
    ):
        raise ValueError("receipt comparison differs")
    return doc


def render_authority_anchor(document: Mapping[str, object]) -> bytes:
    """Render the exact canonical bytes that are covered by the detached signature."""
    if not isinstance(document, Mapping):
        raise ValueError("authority anchor must be a mapping")
    return _canonical_json_bytes(document) + b"\n"


def build_authority_anchor(
    *,
    key_id: str,
    inventory: Mapping[str, object],
    receipt: Mapping[str, object],
    inventory_bytes: bytes,
    receipt_bytes: bytes,
    contract_bytes: bytes,
    run_spec_bytes: bytes,
    amendment_design_bytes: bytes,
    amendment_design_git_blob_id: str,
    amendment_plan_bytes: bytes,
    amendment_plan_git_blob_id: str,
) -> Mapping[str, object]:
    """Build the closed binding document from already-authenticated artifacts."""
    inventory_map = _required_mapping(inventory, {"custody_identity_manifest_sha256"}, "inventory")
    receipt_map = _required_mapping(receipt, {"comparison"}, "receipt")
    comparison = _required_mapping(
        receipt_map["comparison"],
        {
            "source_content_manifest_sha256", "snapshot_content_manifest_sha256",
            "binding_comparison",
        },
        "receipt comparison",
    )
    binding = _required_mapping(
        comparison["binding_comparison"],
        {"source_logical_binding_manifest_sha256", "snapshot_logical_binding_manifest_sha256"},
        "receipt binding comparison",
    )
    document: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "key_id": _key_id(key_id),
        "source_approval": dict(SOURCE_APPROVAL),
        "artifacts": {
            "inventory": {"path": _ARTIFACT_PATHS["inventory"], "sha256": _sha256(inventory_bytes, "inventory")},
            "receipt": {"path": _ARTIFACT_PATHS["receipt"], "sha256": _sha256(receipt_bytes, "receipt")},
            "contract": {"path": _ARTIFACT_PATHS["contract"], "sha256": _sha256(contract_bytes, "contract")},
            "run_spec": {"path": _ARTIFACT_PATHS["run_spec"], "sha256": _sha256(run_spec_bytes, "run spec")},
        },
        "amendment": {
            "design": {
                "path": _AMENDMENT_PATHS["design"], "sha256": _sha256(amendment_design_bytes, "design"),
                "git_blob_id": amendment_design_git_blob_id,
            },
            "implementation_plan": {
                "path": _AMENDMENT_PATHS["implementation_plan"], "sha256": _sha256(amendment_plan_bytes, "implementation plan"),
                "git_blob_id": amendment_plan_git_blob_id,
            },
        },
        "custody_identity_manifest_sha256": inventory_map["custody_identity_manifest_sha256"],
        "receipt_comparison": {
            "source_content_manifest_sha256": comparison["source_content_manifest_sha256"],
            "snapshot_content_manifest_sha256": comparison["snapshot_content_manifest_sha256"],
            "source_logical_binding_manifest_sha256": binding["source_logical_binding_manifest_sha256"],
            "snapshot_logical_binding_manifest_sha256": binding["snapshot_logical_binding_manifest_sha256"],
        },
    }
    _validate_document(document, expected_key_id=key_id)
    return document


def verify_authority_anchor(
    anchor_bytes: bytes,
    signature: bytes,
    *,
    public_key: bytes,
    expected_key_id: str,
) -> Mapping[str, object]:
    """Verify signature first, then canonical schema and semantic constraints."""
    if (
        not isinstance(anchor_bytes, bytes)
        or len(anchor_bytes) > MAX_AUTHORITY_ANCHOR_BYTES
        or not isinstance(signature, bytes)
        or len(signature) != 64
    ):
        raise InvalidSignature
    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("anchor public key differs")
    expected = _key_id(expected_key_id)
    if expected != "ed25519-sha256:" + hashlib.sha256(public_key).hexdigest():
        raise ValueError("anchor public key ID differs")
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, DOMAIN_MESSAGE + anchor_bytes)
    except InvalidSignature:
        raise
    except ValueError as exc:
        raise InvalidSignature from exc
    try:
        document = json.loads(anchor_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authority anchor is malformed") from exc
    if not isinstance(document, Mapping) or render_authority_anchor(document) != anchor_bytes:
        raise ValueError("authority anchor is not canonical")
    return _freeze(_validate_document(document, expected_key_id=expected))  # type: ignore[return-value]


def verify_production_authority_anchor(
    anchor_bytes: bytes, signature: bytes,
) -> Mapping[str, object]:
    """Verify only with the pinned production public key and key ID."""
    from .v52_r4_production_public_key import PRODUCTION_KEY_ID, PRODUCTION_PUBLIC_KEY

    return verify_authority_anchor(
        anchor_bytes,
        signature,
        public_key=PRODUCTION_PUBLIC_KEY,
        expected_key_id=PRODUCTION_KEY_ID,
    )
