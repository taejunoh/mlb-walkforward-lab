"""Validated, immutable source authority carried through R4 issuance.

The public builder is deliberately only a parser/validator.  It cannot alter
production issuance: the issuer resolves its private provider internally and
never accepts a profile from an API or command-line caller.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from .v52_r4_production_public_key import PRODUCTION_KEY_ID, PRODUCTION_PUBLIC_KEY
from .v52_r4_apfs import CUSTODY_UNAVAILABLE


LOGICAL_RAW_ROOT = PurePosixPath("data/v5_1_bt/raw")
INVENTORY_RELATIVE = PurePosixPath("audit/v5_1_bt/protected_raw_inventory.json")
PRODUCTION_INVENTORY_SHA256 = "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6"
ROOT_LOCKS = (
    ".baseball_savant_statcast.lock",
    ".mlb_schedule.lock",
    ".mlb_stats_pitcher_bf.lock",
    ".mlb_stats_pitching.lock",
)
EXPECTED_COUNTS = (1675, 4711, 5025)
_ROLE_ORDER = ("manifest", "raw", "seal")
_PROFILE_TOKEN = object()


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


@dataclass(frozen=True)
class SnapshotSourceClosure:
    """The exact source records and root locks permitted at the copy boundary."""

    records: Sequence[Mapping[str, object]]
    logical_raw_root: PurePosixPath = LOGICAL_RAW_ROOT
    root_locks: tuple[str, ...] = ROOT_LOCKS


@dataclass(frozen=True)
class R4SourceAuthorityProfile:
    """Immutable capability data derived from one authenticated source inventory."""

    authority_root: Path
    logical_raw_root: PurePosixPath
    inventory_relative: PurePosixPath
    inventory_sha256: str
    source_approval: Mapping[str, str]
    closure: SnapshotSourceClosure
    physical_evidence: tuple[Mapping[str, object], ...]
    physical_closure_sha256: str
    record_count: int
    physical_member_count: int
    logical_binding_count: int
    anchor_public_key: bytes
    anchor_key_id: str
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._token is not _PROFILE_TOKEN:
            raise ValueError("R4 source authority profile must be verified")
        if (
            self.logical_raw_root != LOGICAL_RAW_ROOT
            or self.inventory_relative != INVENTORY_RELATIVE
            or self.closure.logical_raw_root != LOGICAL_RAW_ROOT
            or self.closure.root_locks != ROOT_LOCKS
            or (self.record_count, self.physical_member_count, self.logical_binding_count)
            != EXPECTED_COUNTS
            or len(self.closure.records) != self.record_count
            or len(self.physical_evidence) != self.physical_member_count
            or len(self.inventory_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.inventory_sha256)
            or len(self.physical_closure_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.physical_closure_sha256)
            or not isinstance(self.anchor_public_key, bytes)
            or len(self.anchor_public_key) != 32
            or not isinstance(self.anchor_key_id, str)
            or self.anchor_key_id != "ed25519-sha256:" + hashlib.sha256(self.anchor_public_key).hexdigest()
        ):
            raise ValueError("R4 source authority profile differs")
        expected_approval = {
            "logical_raw_root": LOGICAL_RAW_ROOT.as_posix(),
            "protected_inventory_path": INVENTORY_RELATIVE.as_posix(),
            "protected_inventory_sha256": self.inventory_sha256,
        }
        if dict(self.source_approval) != expected_approval:
            raise ValueError("R4 source authority approval differs")


@dataclass(frozen=True)
class _IssuanceAuthority:
    """Opaque raw-issuance capability; never returned by public validators."""

    profile: R4SourceAuthorityProfile
    sign_anchor_message: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not callable(self.sign_anchor_message):
            raise ValueError("R4 issuance signer is unavailable")


def _production_anchor_public_identity() -> tuple[bytes, str]:
    return PRODUCTION_PUBLIC_KEY, PRODUCTION_KEY_ID


def _artifact(record: Mapping[str, object], role: str) -> dict[str, object]:
    value = record.get(role)
    if not isinstance(value, Mapping) or not {"path", "sha256", "byte_size"}.issubset(value):
        raise ValueError("R4 source authority artifact differs")
    path, digest, size = value["path"], value["sha256"], value["byte_size"]
    if (
        not isinstance(path, str)
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        raise ValueError("R4 source authority artifact differs")
    pure = PurePosixPath(path)
    try:
        suffix = pure.relative_to(LOGICAL_RAW_ROOT)
    except ValueError as exc:
        raise ValueError("R4 source authority artifact root differs") from exc
    if not suffix.parts or len(suffix.parts) != 2 or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("R4 source authority artifact path differs")
    return {"path": path, "sha256": digest, "byte_size": size}


def _verify_records(records: object) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
    if not isinstance(records, list) or len(records) != EXPECTED_COUNTS[0]:
        raise ValueError("R4 source authority record count differs")
    normalized: list[Mapping[str, object]] = []
    evidence: dict[str, Mapping[str, object]] = {}
    bindings = 0
    for record in records:
        if not isinstance(record, Mapping) or set(record) != {"source", "canonical_query", "rawstore_verified", *_ROLE_ORDER}:
            raise ValueError("R4 source authority record differs")
        if not isinstance(record["source"], str) or not isinstance(record["canonical_query"], Mapping) or record["rawstore_verified"] is not True:
            raise ValueError("R4 source authority record differs")
        copied: dict[str, object] = {
            "source": record["source"],
            "canonical_query": dict(record["canonical_query"]),
            "rawstore_verified": True,
        }
        for role in _ROLE_ORDER:
            item = _artifact(record, role)
            copied[role] = item
            previous = evidence.setdefault(item["path"], item)
            if previous != item:
                raise ValueError("R4 source authority physical evidence differs")
            bindings += 1
        normalized.append(_freeze(copied))
    if len(evidence) != EXPECTED_COUNTS[1] or bindings != EXPECTED_COUNTS[2]:
        raise ValueError("R4 source authority closure counts differ")
    return tuple(normalized), tuple(_freeze(evidence[path]) for path in sorted(evidence))


def verify_source_authority_profile(
    source_root: Path, source_inventory: Path,
) -> R4SourceAuthorityProfile:
    """Validate a complete R4 source inventory into immutable capability data.

    This public entry point is production-only and always uses the fixed
    protected inventory digest.  Synthetic issuance must provide an already
    constructed profile through the issuer's private provider seam.
    """
    raw = Path(source_root).absolute()
    inventory = Path(source_inventory).absolute()
    try:
        authority_root = raw.parents[len(LOGICAL_RAW_ROOT.parts) - 1]
    except IndexError as exc:
        raise ValueError("R4 source authority root differs") from exc
    if raw != authority_root / Path(*LOGICAL_RAW_ROOT.parts) or inventory != authority_root / Path(*INVENTORY_RELATIVE.parts):
        raise ValueError("R4 source authority paths differ")
    payload = inventory.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != PRODUCTION_INVENTORY_SHA256:
        raise ValueError("R4 source authority inventory digest differs")
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("R4 source authority inventory is malformed") from exc
    if (
        not isinstance(document, Mapping)
        or not {"legacy_root", "manifest_count", "records"}.issubset(document)
        or document["legacy_root"] != LOGICAL_RAW_ROOT.as_posix()
        or document["manifest_count"] != EXPECTED_COUNTS[0]
        or _canonical_json_bytes(document) not in {payload, payload.rstrip(b"\n")}
    ):
        raise ValueError("R4 source authority inventory differs")
    records, evidence = _verify_records(document["records"])
    evidence_rows = [dict(item) for item in evidence]
    approval = MappingProxyType({
        "logical_raw_root": LOGICAL_RAW_ROOT.as_posix(),
        "protected_inventory_path": INVENTORY_RELATIVE.as_posix(),
        "protected_inventory_sha256": digest,
    })
    anchor_public_key, anchor_key_id = _production_anchor_public_identity()
    return R4SourceAuthorityProfile(
        authority_root=authority_root,
        logical_raw_root=LOGICAL_RAW_ROOT,
        inventory_relative=INVENTORY_RELATIVE,
        inventory_sha256=digest,
        source_approval=approval,
        closure=SnapshotSourceClosure(records),
        physical_evidence=evidence,
        physical_closure_sha256=hashlib.sha256(_canonical_json_bytes(evidence_rows)).hexdigest(),
        record_count=EXPECTED_COUNTS[0],
        physical_member_count=EXPECTED_COUNTS[1],
        logical_binding_count=EXPECTED_COUNTS[2],
        anchor_public_key=anchor_public_key,
        anchor_key_id=anchor_key_id,
        _token=_PROFILE_TOKEN,
    )


def production_source_authority_profile() -> R4SourceAuthorityProfile:
    """Return the fixed, repository-local production profile for validators."""
    repository = Path(__file__).resolve().parent.parent
    return verify_source_authority_profile(
        repository / Path(*LOGICAL_RAW_ROOT.parts),
        repository / Path(*INVENTORY_RELATIVE.parts),
    )


def _resolve_source_authority(
    source_root: Path | None = None, source_inventory: Path | None = None,
) -> R4SourceAuthorityProfile | _IssuanceAuthority:
    """The one private provider for raw issuance and raw-free R4 authority."""
    try:
        if source_root is None and source_inventory is None:
            return production_source_authority_profile()
        if source_root is None or source_inventory is None:
            raise ValueError("source authority mode differs")
        profile = verify_source_authority_profile(source_root, source_inventory)
        from .v52_r4_offline_signer import sign_anchor_message

        return _IssuanceAuthority(profile, sign_anchor_message)
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("approved source inventory is unavailable") from exc


__all__ = [
    "EXPECTED_COUNTS", "INVENTORY_RELATIVE", "LOGICAL_RAW_ROOT", "PRODUCTION_INVENTORY_SHA256",
    "R4SourceAuthorityProfile", "ROOT_LOCKS", "SnapshotSourceClosure", "production_source_authority_profile", "verify_source_authority_profile",
]
