"""Verified, immutable game and participant evidence inventory."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import fcntl
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .v52_legacy import LegacyEvidenceCapability

from .protocols import RunSpec, SourceRunSpec, parse_run_spec
from .v52_evidence import (
    V52EvidenceStore,
    VerifiedEvidenceRead,
    logical_query_id,
)
from .v52_query_plan import (
    SourceAuthorityContext,
    _context_roots,
    _load_query_batch_for_authority,
    _load_static_contracts,
    _read_repository_file,
    _validate_run_bound_acquisition_contract,
    QueryBatch,
    build_schedule_reference_batch,
    load_query_batch,
)
from .v52_schedule_inventory import (
    RecoveryDispositionBindings,
    _SOURCE_ANCHOR_KEYS,
    build_schedule_anchor_provenance,
    load_verified_schedule_anchor_inventory,
)
from .v52_source_contracts import (
    canonical_json_bytes,
    canonical_sha256,
    recompute_query_sidecar,
)
from .v52_sources.schedule import (
    derived_schedule_sidecar_adapter,
    recompute_boxscore_sidecar,
    recompute_game_evidence_snapshot,
    recompute_schedule_anchor_sidecar,
)
from .v52_timezone_review import (
    _file_identity,
    _open_parent,
    _read_safe_snapshot,
    _rebind_safe_snapshot,
    load_verified_timezone_gate,
)


_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/game_evidence_inventory.json"
)
_SCHEDULE_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
_BATCH_ROOT = Path("audit/v5_2_qd/historical/acquisition/query_batches")
_RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
_ACQUISITION_CONTRACT_RELATIVE = Path(
    "protocols/v5_2_qd/historical_source_acquisition.json"
)
_CANDIDATE_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/venue_timezone_candidate.json"
)
_COVERAGE_REVIEW_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/timezone_reviews/coverage.json"
)
_SEMANTIC_REVIEW_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/timezone_reviews/semantic.json"
)
_MAPPING_RELATIVE = Path("config/v5_2_qd/venue_timezones.json")
_SEAL_RELATIVE = Path("audit/v5_2_qd/historical/acquisition/venue_timezone_seal.json")
_RUN_ID = "historical_walkforward_500"
_SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
_RECOVERY_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_r2"
_R3_RECOVERY_RUN_ID = (
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
)
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMON_GAME_KEYS = frozenset(
    {
        "actual_starter_ids",
        "alternate_site",
        "away_score",
        "away_starting_hitter_ids",
        "away_team_id",
        "completed_innings",
        "doubleheader_order",
        "feature_history_cutoff",
        "game_pk",
        "home_score",
        "home_starting_hitter_ids",
        "home_team_id",
        "neutral_site",
        "official_date",
        "participant_provenance",
        "played",
        "schedule_query_sidecar_sha256",
        "scheduled_innings",
        "scheduled_start_utc",
        "season",
        "season_start_date",
        "source_game_sha256",
        "terminal_status",
        "venue_id",
        "verification_status",
        "winner_team_id",
    }
)


@dataclass(frozen=True)
class VerifiedGameEvidenceInventory:
    payload: Mapping[str, object]
    path: Path
    sha256: str


@dataclass(frozen=True)
class _BoundSnapshot:
    snapshot: _ArtifactToken
    label: str
    max_bytes: int


@dataclass(frozen=True)
class _ArtifactToken:
    relative: Path
    identity: tuple[int, ...]
    size: int
    sha256: str


@dataclass(frozen=True)
class _Authority:
    payload: dict[str, object]
    snapshots: tuple[_BoundSnapshot, ...]
    context: SourceAuthorityContext
    r3_catalog: object | None = None
    r3_parent_fingerprint: str | None = None
    legacy_evidence: LegacyEvidenceCapability | None = None


def _authority_context(
    root: Path,
    run_spec_path: Path | None,
    acquisition_contract_path: Path | None,
) -> SourceAuthorityContext:
    """Authenticate the exact authority pair before replay/checkpoint I/O."""
    supplied_run = Path(run_spec_path) if run_spec_path is not None else root / _RUN_SPEC_RELATIVE
    supplied_contract = (
        Path(acquisition_contract_path)
        if acquisition_contract_path is not None
        else root / _ACQUISITION_CONTRACT_RELATIVE
    )
    for supplied, label in ((supplied_run, "RunSpec"), (supplied_contract, "acquisition contract")):
        text = os.fspath(supplied)
        if (
            not supplied.is_absolute()
            or "\\" in text
            or any(part in {"", ".", ".."} for part in supplied.parts)
            or Path(os.path.abspath(text)) != supplied
        ):
            raise ValueError(f"{label} path differs from the repository contract")
        try:
            supplied.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} path must stay under the repository root") from exc
    try:
        context = _load_static_contracts(supplied_run, supplied_contract)
    except ValueError:
        # Historical unit fixtures predate the design/plan files authenticated
        # by the full static loader. Preserve their old default behavior while
        # retaining strict static authentication for explicit authority paths.
        if run_spec_path is not None or acquisition_contract_path is not None:
            raise
        run_contents = _read_repository_file(
            root, _RUN_SPEC_RELATIVE, "game evidence RunSpec"
        )
        contract_contents = _read_repository_file(
            root,
            _ACQUISITION_CONTRACT_RELATIVE,
            "game evidence acquisition contract",
        )
        run_raw, run_sha, contract, contract_sha = (
            _validate_run_bound_acquisition_contract(
                root, run_contents, contract_contents
            )
        )
        authority = parse_run_spec(run_raw)
        if authority.run_id != _RUN_ID or contract.get("run_id") != _RUN_ID:
            raise ValueError("game evidence legacy authority run_id differs")
        roots = _context_roots(root, authority, contract)
        context = SourceAuthorityContext(
            repository_root=root,
            authority=authority,
            authority_path=supplied_run,
            authority_sha256=run_sha,
            acquisition_contract_path=supplied_contract,
            acquisition_contract_sha256=contract_sha,
            contract=contract,
            run_id=authority.run_id,
            raw_root=roots[0],
            sources_root=roots[1],
            acquisition_audit_root=roots[2],
            execution_audit_root=roots[3],
            report_root=roots[4],
        )
    if context.repository_root != root:
        raise ValueError("authority pair repository root differs")
    return context


def _timezone_authority_relatives(
    context: SourceAuthorityContext,
) -> tuple[Path, Path, Path, Path, Path]:
    acquisition = context.acquisition_audit_root.relative_to(
        context.repository_root
    )
    return (
        acquisition / "venue_timezone_candidate.json",
        acquisition / "timezone_reviews/coverage.json",
        acquisition / "timezone_reviews/semantic.json",
        (
            acquisition / "venue_timezones.json"
            if context.authority.run_id in {_RECOVERY_RUN_ID, _R3_RECOVERY_RUN_ID}
            else _MAPPING_RELATIVE
        ),
        acquisition / "venue_timezone_seal.json",
    )


def _timezone_review_hashes_by_role(
    timezone_gate: object,
    coverage_contents: bytes,
    semantic_contents: bytes,
) -> dict[str, str]:
    """Bind timezone review hashes to their reviewer role, never tuple order."""
    reviews = getattr(timezone_gate, "reviews", None)
    review_hashes = getattr(timezone_gate, "review_sha256s", None)
    if (
        not isinstance(reviews, tuple)
        or len(reviews) != 2
        or not isinstance(review_hashes, tuple)
        or len(review_hashes) != 2
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in review_hashes
        )
    ):
        raise ValueError("game evidence timezone review authority differs")
    expected = {
        "TIMEZONE_COVERAGE_REVIEW": _sha256(coverage_contents),
        "TIMEZONE_SEMANTIC_REVIEW": _sha256(semantic_contents),
    }
    roles = [
        review.get("reviewer_role") if isinstance(review, Mapping) else None
        for review in reviews
    ]
    if set(roles) != set(expected) or set(review_hashes) != set(expected.values()):
        raise ValueError("game evidence timezone review authority differs")
    return expected


def _before_game_inventory_final_rebind() -> None:
    """Deterministic verified-load race-test hook; production does nothing."""


def _admit_legacy_evidence(
    legacy_evidence: LegacyEvidenceCapability | None,
) -> LegacyEvidenceCapability | None:
    if legacy_evidence is None:
        return None
    from .v52_legacy import _require_issued_legacy_evidence

    capability = _require_issued_legacy_evidence(legacy_evidence)
    capability.validate()
    return capability


def _context_legacy_evidence(
    root: Path,
    context: SourceAuthorityContext,
    legacy_evidence: LegacyEvidenceCapability | None,
) -> LegacyEvidenceCapability | None:
    if context.run_id == _R3_RECOVERY_RUN_ID:
        from .v52_external_protected_parent import (
            ExternalProtectedParentCapability,
        )

        if (
            type(legacy_evidence) is not ExternalProtectedParentCapability
            or legacy_evidence._target_root != root
        ):
            raise TypeError(
                "exact r3 game evidence requires an issued external legacy capability"
            )
        return legacy_evidence
    if legacy_evidence is not None:
        return legacy_evidence
    protection = context.contract.get("legacy_protection")
    if not isinstance(protection, Mapping):
        return None
    legacy_root = protection.get("legacy_root")
    inventory_path = protection.get("protected_inventory_path")
    inventory_sha256 = protection.get("protected_inventory_sha256")
    if (
        legacy_root != "data/v5_1_bt/raw"
        or inventory_path != "audit/v5_1_bt/protected_raw_inventory.json"
        or not isinstance(inventory_sha256, str)
        or _SHA256.fullmatch(inventory_sha256) is None
    ):
        raise ValueError("game evidence legacy protection authority differs")
    local_root = root / legacy_root
    local_inventory = root / inventory_path
    if not local_root.is_dir() or not local_inventory.is_file():
        return None
    from .v52_legacy import (
        adapt_root_local_legacy_catalog,
        load_protected_legacy_catalog,
    )

    return adapt_root_local_legacy_catalog(
        load_protected_legacy_catalog(
            root,
            legacy_root=local_root,
            protected_inventory_path=local_inventory,
            protected_inventory_sha256=inventory_sha256,
        )
    )


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


def _trusted_game_root(root: Path) -> Path:
    supplied = Path(os.path.abspath(os.fspath(root)))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(os.sep, flags)
    try:
        for component in supplied.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ValueError(
                    "game evidence root has an unavailable or symlinked ancestor"
                ) from exc
            opened = os.fstat(child)
            try:
                linked = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                os.close(child)
                raise
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                os.close(child)
                raise ValueError(
                    "game evidence root ancestor identity or symlink differs"
                )
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    return supplied


def _required_snapshot(
    root: Path,
    relative: Path,
    label: str,
    *,
    max_bytes: int = _MAX_ARTIFACT_BYTES,
):
    snapshot = _read_safe_snapshot(
        root,
        relative,
        label,
        max_bytes=max_bytes,
    )
    if snapshot is None:
        raise ValueError(f"{label} is missing")
    return snapshot


def _root_relative(root: Path, path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under the repository root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} path differs")
    return relative


def _manifest_relative(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} path differs")
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{label} path differs")
    return relative


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_or_none(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer or null")
    return value


def _canonical_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical date")
    return parsed


def _positive_ids(value: object, count: int, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError(f"{label} participant projection is incomplete")
    try:
        ids = [_positive_int(item, label) for item in value]
    except ValueError as exc:
        raise ValueError(f"{label} participant identity differs") from exc
    if len(set(ids)) != len(ids):
        raise ValueError(f"{label} participant IDs must be unique")
    return ids


def _json_object(contents: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    if contents != _canonical_file_bytes(value):
        raise ValueError(f"{label} must be canonical JSON with one newline")
    return value


def _binding(relative: Path, contents: bytes) -> dict[str, object]:
    return {"path": relative.as_posix(), "sha256": _sha256(contents)}


def _token_from_snapshot(snapshot: object) -> _ArtifactToken:
    contents = snapshot.contents
    return _ArtifactToken(
        relative=snapshot.relative,
        identity=snapshot.identity,
        size=len(contents),
        sha256=_sha256(contents),
    )


def _read_artifact_token(
    root: Path,
    relative: Path,
    label: str,
    *,
    max_bytes: int,
) -> _ArtifactToken:
    parent, descriptors = _open_parent(root, relative, create=False)
    descriptor = -1
    try:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(relative.name, flags, dir_fd=parent)
        except OSError as exc:
            raise ValueError(f"{label} must be a no-follow regular file") from exc
        before = os.fstat(descriptor)
        linked = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _file_identity(before) != _file_identity(linked)
        ):
            raise ValueError(f"{label} must be an unaliased regular file")
        if before.st_size > max_bytes:
            raise ValueError(f"{label} exceeds its bounded read limit")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"{label} exceeds its bounded read limit")
            digest.update(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(rebound)
            or total != after.st_size
        ):
            raise ValueError(f"{label} identity changed while hashing")
        return _ArtifactToken(
            relative=relative,
            identity=_file_identity(after),
            size=total,
            sha256=digest.hexdigest(),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for item in reversed(descriptors):
            os.close(item)


def _bound_snapshot(
    root: Path,
    relative: Path,
    label: str,
    *,
    max_bytes: int = _MAX_ARTIFACT_BYTES,
) -> _BoundSnapshot:
    return _BoundSnapshot(
        snapshot=_read_artifact_token(root, relative, label, max_bytes=max_bytes),
        label=label,
        max_bytes=max_bytes,
    )


def _verified_evidence_snapshots(
    root: Path,
    evidence_root: Path,
    verified: VerifiedEvidenceRead,
    *,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> tuple[_BoundSnapshot, ...]:
    manifest = verified.success_receipt
    evidence_prefix = _root_relative(root, evidence_root, "game evidence raw namespace")
    snapshots: list[_BoundSnapshot] = []

    success_relative = _root_relative(
        root,
        verified.published_evidence.success_receipt_path,
        "game evidence success receipt",
    )
    success = _bound_snapshot(root, success_relative, "game evidence success receipt")
    expected_success = _canonical_file_bytes(dict(manifest))
    if success.snapshot.size != len(
        expected_success
    ) or success.snapshot.sha256 != _sha256(expected_success):
        raise ValueError("game evidence success receipt bytes differ")
    snapshots.append(success)

    for key, hash_key, label, max_bytes in (
        (
            "query_path",
            "query_sha256",
            "game evidence registered query",
            _MAX_ARTIFACT_BYTES,
        ),
        (
            "query_sidecar_path",
            "query_sidecar_sha256",
            "game evidence query sidecar",
            _MAX_ARTIFACT_BYTES,
        ),
    ):
        relative = evidence_prefix / _manifest_relative(manifest.get(key), label)
        bound = _bound_snapshot(root, relative, label, max_bytes=max_bytes)
        expected_hash = manifest.get(hash_key)
        if not isinstance(expected_hash, str) or bound.snapshot.sha256 != expected_hash:
            raise ValueError(f"{label} hash differs")
        snapshots.append(bound)

    history = manifest.get("attempt_history")
    if not isinstance(history, list):
        raise ValueError("game evidence attempt history differs")
    for record in history:
        if not isinstance(record, Mapping):
            raise ValueError("game evidence attempt receipt binding differs")
        relative = evidence_prefix / _manifest_relative(
            record.get("path"), "game evidence attempt receipt"
        )
        bound = _bound_snapshot(root, relative, "game evidence attempt receipt")
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str) or bound.snapshot.sha256 != expected_hash:
            raise ValueError("game evidence attempt receipt hash differs")
        snapshots.append(bound)

    mode = verified.published_evidence.storage_mode
    if mode == "FETCHED_RAW":
        relative = evidence_prefix / _manifest_relative(
            manifest.get("fetched_raw_path"), "game evidence fetched raw object"
        )
        bound = _bound_snapshot(
            root,
            relative,
            "game evidence fetched raw object",
            max_bytes=_MAX_EVIDENCE_BYTES,
        )
        if (
            verified.raw_bytes is None
            or bound.snapshot.size != len(verified.raw_bytes)
            or bound.snapshot.sha256 != manifest.get("raw_sha256")
        ):
            raise ValueError("game evidence fetched raw object differs")
        snapshots.append(bound)
    elif mode == "PROTECTED_LEGACY_REFERENCE":
        relative = evidence_prefix / _manifest_relative(
            manifest.get("protected_legacy_reference_path"),
            "game evidence protected legacy reference",
        )
        reference_contents = _required_snapshot(
            root,
            relative,
            "game evidence protected legacy reference",
        )
        reference = _BoundSnapshot(
            snapshot=_token_from_snapshot(reference_contents),
            label="game evidence protected legacy reference",
            max_bytes=_MAX_ARTIFACT_BYTES,
        )
        if reference.snapshot.sha256 != manifest.get(
            "protected_legacy_reference_sha256"
        ):
            raise ValueError("game evidence protected legacy reference hash differs")
        reference_record = _json_object(
            reference_contents.contents,
            "game evidence protected legacy reference",
        )
        snapshots.append(reference)
        lineage_bindings = (
            (
                "protected_inventory_path",
                "protected_inventory_sha256",
                "game evidence protected raw inventory",
                _MAX_ARTIFACT_BYTES,
            ),
            (
                "legacy_manifest_path",
                "legacy_manifest_sha256",
                "game evidence protected legacy manifest",
                _MAX_ARTIFACT_BYTES,
            ),
            (
                "legacy_seal_path",
                "legacy_seal_sha256",
                "game evidence protected legacy seal",
                _MAX_ARTIFACT_BYTES,
            ),
            (
                "payload_path",
                "raw_sha256",
                "game evidence protected legacy payload",
                _MAX_EVIDENCE_BYTES,
            ),
        )
        if legacy_evidence is None:
            raise TypeError(
                "protected game evidence requires an issued legacy capability"
            )

        from .v52_legacy import _require_issued_legacy_evidence

        capability = _require_issued_legacy_evidence(legacy_evidence)
        capability.validate()
        if capability.inventory_sha256 != reference_record.get(
            "protected_inventory_sha256"
        ):
            raise ValueError("game evidence protected inventory capability differs")
        inventory_key, inventory_hash_key, inventory_label, inventory_max = (
            lineage_bindings[0]
        )
        inventory_lineage = _bound_snapshot(
            root,
            _manifest_relative(reference_record.get(inventory_key), inventory_label),
            inventory_label,
            max_bytes=inventory_max,
        )
        if inventory_lineage.snapshot.sha256 != reference_record.get(
            inventory_hash_key
        ):
            raise ValueError(f"{inventory_label} hash differs")
        snapshots.append(inventory_lineage)
        for key, hash_key, label, max_bytes in lineage_bindings[1:]:
            logical = PurePosixPath(
                _manifest_relative(reference_record.get(key), label).as_posix()
            )
            with capability.open_bound_object(logical) as opened:
                contents = opened.read(max_bytes + 1)
            expected_hash = reference_record.get(hash_key)
            if (
                len(contents) > max_bytes
                or not isinstance(expected_hash, str)
                or _sha256(contents) != expected_hash
            ):
                raise ValueError(f"{label} hash differs")
        capability.rebind()
    elif mode != "DERIVED_DISPOSITION":
        raise ValueError("game evidence storage mode differs")
    return tuple(snapshots)


def _raw_namespace(run_spec: RunSpec | SourceRunSpec) -> Path:
    namespaces = (
        run_spec.raw.get("namespaces")
        if isinstance(run_spec, RunSpec)
        else run_spec.namespaces
    )
    value = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(value, str):
        raise ValueError("game evidence RunSpec raw namespace differs")
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("game evidence RunSpec raw namespace differs")
    return relative


def _load_authority_batch(
    root: Path,
    batch_id: str,
    authority: RunSpec | SourceRunSpec,
    *,
    read_only_progress: bool,
) -> QueryBatch:
    """Load a batch only from the namespace selected by its sealed authority.

    The historical public seam remains in place for the original RunSpec.
    The superseding run must never probe the historical namespace as a
    fallback, so it always uses the authority-explicit loader.
    """
    if isinstance(authority, SourceRunSpec):
        return _load_query_batch_for_authority(
            root, batch_id, authority, read_only_progress=read_only_progress
        )
    return load_query_batch(root, batch_id, read_only_progress=read_only_progress)


def _source_disposition_binding(
    schedule_payload: Mapping[str, object],
    disposition_query_id: str,
) -> dict[str, object]:
    """Extract the one closed disposition binding from source schedule authority."""
    sources = schedule_payload.get("season_sources")
    if not isinstance(sources, list) or len(sources) != 7:
        raise ValueError("source schedule disposition season sources differ")
    source = sources[1]
    if (
        not isinstance(source, Mapping)
        or source.get("season") != 2019
        or source.get("storage_mode") != "DERIVED_DISPOSITION"
        or source.get("raw_sha256") is not None
        or source.get("query_id") != disposition_query_id
    ):
        raise ValueError("source schedule disposition binding differs")
    success = source.get("success_receipt")
    sidecar = source.get("query_sidecar")
    for value, label in ((success, "success receipt"), (sidecar, "query sidecar")):
        if (
            not isinstance(value, Mapping)
            or set(value) != {"path", "sha256"}
            or not isinstance(value.get("path"), str)
            or not isinstance(value.get("sha256"), str)
            or _SHA256.fullmatch(value["sha256"]) is None
        ):
            raise ValueError(f"source schedule disposition {label} differs")
    return {
        "query_id": disposition_query_id,
        "success_receipt": dict(success),
        "query_sidecar": dict(sidecar),
    }


def _replace_source_disposition_lineage(
    snapshot: Mapping[str, object],
    *,
    synthetic_sidecar_sha256: str,
    derived_sidecar_sha256: str,
) -> dict[str, object]:
    """Bind the 2019 projected games to the actual derived sidecar.

    ``recompute_game_evidence_snapshot`` deliberately accepts only raw-backed
    schedule sidecars.  A source disposition has no raw bytes, so a temporary
    shape-valid adapter is used for the parser reduction and every resulting
    2019 lineage digest is then recomputed against the authenticated derived
    sidecar SHA-256.  The adapter digest is never published.
    """
    result = json.loads(_canonical_file_bytes(snapshot))
    if not isinstance(result, dict) or not isinstance(result.get("games"), list):
        raise ValueError("source game evidence projection differs")
    for game in result["games"]:
        if not isinstance(game, dict) or game.get("season") != 2019:
            continue
        if game.get("schedule_query_sidecar_sha256") != synthetic_sidecar_sha256:
            raise ValueError("source game disposition adapter lineage differs")
        game["schedule_query_sidecar_sha256"] = derived_sidecar_sha256
        lineage: dict[str, object] = {
            "anchor_sidecar_sha256": derived_sidecar_sha256,
            "game": {key: value for key, value in game.items() if key != "source_game_sha256"},
        }
        if game.get("played") is True:
            boxscore = game.get("boxscore_query_sidecar_sha256")
            if not isinstance(boxscore, str) or _SHA256.fullmatch(boxscore) is None:
                raise ValueError("source game disposition boxscore lineage differs")
            lineage["boxscore_query_sidecar_sha256"] = boxscore
        game["source_game_sha256"] = canonical_sha256(lineage)
    hashes = result.get("source_query_sidecar_sha256s")
    if not isinstance(hashes, list) or synthetic_sidecar_sha256 not in hashes:
        raise ValueError("source game disposition sidecar population differs")
    result["source_query_sidecar_sha256s"] = sorted(
        (set(hashes) - {synthetic_sidecar_sha256}) | {derived_sidecar_sha256}
    )
    return result


def _recovery_schedule_adapter(
    query: object,
    anchors: object,
    *,
    season: int,
) -> Mapping[str, object]:
    """Adapt an authenticated recovery inventory partition for the v5.2.1 joiner."""
    if not isinstance(anchors, list):
        raise ValueError("recovery schedule inventory anchors differ")
    projected: list[dict[str, object]] = []
    for raw in anchors:
        if not isinstance(raw, Mapping) or raw.get("season") != season:
            continue
        if set(raw) != _SOURCE_ANCHOR_KEYS:
            raise ValueError("recovery schedule inventory anchor keys differ")
        projected.append(
            {key: value for key, value in raw.items() if key != "raw_game_object_sha256"}
        )
    if not projected:
        raise ValueError("recovery schedule adapter requires season anchors")
    if not hasattr(query, "grain") or not hasattr(query, "source"):
        raise TypeError("recovery schedule adapter query differs")
    return {
        "byte_count": 0,
        "grain": query.grain,
        "parser_version": "v5.2.1",
        "query_id": logical_query_id(query),
        "raw_sha256": hashlib.sha256(b"").hexdigest(),
        "schema_version": 1,
        "semantics": {"anchors": projected},
        "source": query.source,
    }


def _r3_recovery_catalog_details(
    catalog: object,
) -> tuple[dict[str, object], dict[int, Mapping[str, object]]]:
    """Return only the sealed r3 4:3 transport and resumed-result boundaries."""
    from .v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )

    if type(catalog) is not MissingVenueRecoveryDispositionCatalog:
        raise ValueError("r3 recovery disposition catalog differs")
    projection = catalog.as_projection()
    if (
        projection.record_game_pks != (565534, 630882, 633224, 632924)
        or projection.record_to_projection_indexes != (0, 1, 2, 2)
        or projection.excluded_game_pks != frozenset({632924})
        or len(projection.projection_query_ids) != 3
        or projection.projection_query_ids != catalog.projection_query_ids()
    ):
        raise ValueError("r3 recovery disposition topology differs")
    records = tuple(catalog.records)
    if tuple(record.game_pk for record in records) != projection.record_game_pks:
        raise ValueError("r3 recovery disposition records differ")
    availability: dict[int, Mapping[str, object]] = {}
    for record in records:
        if record.game_pk not in {630882, 633224}:
            continue
        semantics = record.semantics
        if (
            not isinstance(semantics, Mapping)
            or not isinstance(semantics.get("played_date"), str)
            or not isinstance(semantics.get("result_available_on"), str)
        ):
            raise ValueError("r3 recovery availability authority differs")
        availability[record.game_pk] = semantics
    if set(availability) != {630882, 633224}:
        raise ValueError("r3 recovery availability authority differs")
    return (
        {
            "excluded_game_pks": [632924],
            "projection_query_ids": list(projection.projection_query_ids),
            "record_game_pks": list(projection.record_game_pks),
            "record_to_projection_indexes": list(
                projection.record_to_projection_indexes
            ),
        },
        availability,
    )


def _build_r3_recovery_catalog(
    root: Path, context: SourceAuthorityContext
) -> object:
    """Reconstruct the closed r3 catalog from the current protected authority."""
    from .v52_missing_venue_recovery_disposition import (
        build_missing_venue_recovery_disposition_catalog,
    )

    return build_missing_venue_recovery_disposition_catalog(
        root, context.authority_path, context.acquisition_contract_path
    )


def _parent_catalog_snapshot(catalog: object, label: str) -> dict[str, object]:
    """Capture one reauthenticated protected-parent catalog without path aliases."""
    from .v52_recovery_parent_evidence import RecoveryParentCatalog

    if type(catalog) is not RecoveryParentCatalog:
        raise ValueError(f"{label} parent catalog differs")
    rebound = catalog.reauthenticate()
    if rebound != catalog:
        raise ValueError(f"{label} parent catalog changed during reauthentication")
    root = _directory_identity_snapshot(catalog.repository_root, f"{label} root")
    directories: dict[str, dict[str, object]] = {}
    parents: list[dict[str, object]] = []
    for name, parent in catalog.parents.items():
        artifacts = []
        for artifact_name in ("registration", "attempt", "quarantine", "raw"):
            artifact = getattr(parent, artifact_name)
            if artifact is None:
                continue
            path = catalog.repository_root / artifact.relative
            try:
                node = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"{label} parent artifact cannot be statted") from exc
            if (
                stat.S_ISLNK(node.st_mode)
                or not stat.S_ISREG(node.st_mode)
                or node.st_nlink != 1
                or (node.st_dev, node.st_ino) != (artifact.device, artifact.inode)
                or node.st_size != artifact.byte_count
            ):
                raise ValueError(f"{label} parent artifact identity differs")
            directory_relative = artifact.relative.parent.as_posix()
            directories.setdefault(
                directory_relative,
                _directory_membership_snapshot(
                    catalog.repository_root / artifact.relative.parent,
                    f"{label} parent artifact directory",
                ),
            )
            artifacts.append(
                {
                    "kind": artifact_name,
                    "path": artifact.relative.as_posix(),
                    "sha256": artifact.sha256,
                    "byte_count": artifact.byte_count,
                    "device": artifact.device,
                    "inode": artifact.inode,
                    "mtime_ns": node.st_mtime_ns,
                    "ctime_ns": node.st_ctime_ns,
                }
            )
        parents.append(
            {
                "name": name,
                "query_id": parent.query_id,
                "artifacts": artifacts,
            }
        )
    return {
        "kind": catalog.catalog_kind,
        "repository_root": os.fspath(catalog.repository_root),
        "root_identity": root,
        "directories": directories,
        "parents": parents,
    }


def _directory_identity_snapshot(path: Path, label: str) -> dict[str, int]:
    """Return a no-follow directory identity suitable for a later rebind."""
    try:
        node = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{label} cannot be statted") from exc
    if stat.S_ISLNK(node.st_mode) or not stat.S_ISDIR(node.st_mode):
        raise ValueError(f"{label} must be a real directory")
    return {
        "device": node.st_dev,
        "inode": node.st_ino,
        "mtime_ns": node.st_mtime_ns,
        "ctime_ns": node.st_ctime_ns,
    }


def _directory_membership_snapshot(path: Path, label: str) -> dict[str, object]:
    """Bind a small protected directory's identity and complete no-follow view."""
    identity = _directory_identity_snapshot(path, label)
    try:
        names = sorted(os.listdir(path))
    except OSError as exc:
        raise ValueError(f"{label} membership cannot be listed") from exc
    members: list[dict[str, object]] = []
    for name in names:
        try:
            node = os.stat(path / name, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"{label} membership cannot be statted") from exc
        kind = (
            "directory"
            if stat.S_ISDIR(node.st_mode)
            else "regular"
            if stat.S_ISREG(node.st_mode)
            else "other"
        )
        members.append(
            {
                "name": name,
                "kind": kind,
                "device": node.st_dev,
                "inode": node.st_ino,
                "size": node.st_size,
                "mtime_ns": node.st_mtime_ns,
                "ctime_ns": node.st_ctime_ns,
            }
        )
    # Recheck the directory after enumerating it so a mutation cannot make an
    # internally inconsistent fingerprint look authoritative.
    if _directory_identity_snapshot(path, label) != identity:
        raise ValueError(f"{label} identity changed during membership snapshot")
    return {"identity": identity, "members": members}


def _no_follow_directory_path(path: Path, label: str) -> Path:
    """Resolve no user-selected protected-root component through a symlink."""
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            node = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"{label} cannot be statted") from exc
        if stat.S_ISLNK(node.st_mode) or not stat.S_ISDIR(node.st_mode):
            raise ValueError(f"{label} must not contain a symlink")
    return absolute


def _r3_protected_parent_root(root: Path) -> Path:
    """Select the configured protected root without resolving a symlink alias."""
    from .v52_missing_venue_recovery_disposition import _protected_parent_root

    configured = os.environ.get("MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT") or os.environ.get(
        "MLB_V52_SOURCE_AUTHORITY_ROOT"
    )
    if configured is not None:
        supplied = _no_follow_directory_path(
            Path(configured), "r3 protected parent evidence root"
        )
        resolved = _protected_parent_root(root)
        if resolved != supplied:
            raise ValueError("r3 protected parent evidence root differs")
        return supplied
    return _protected_parent_root(root)


def _r3_catalog_parent_fingerprint(root: Path, catalog: object) -> str:
    """Bind r3's sealed catalog to the final protected-parent filesystem state.

    The disposition object stores canonical semantics but deliberately does not
    retain live protected-file identities.  Capture both closed parent catalogs
    here so an A->B->A replacement remains visible through ctime/mtime even
    when the original bytes and inode happen to be restored.
    """
    from .v52_recovery_parent_evidence import (
        build_missing_venue_parent_catalog,
        build_recovery_parent_catalog,
    )

    dispositions, availability = _r3_recovery_catalog_details(catalog)
    protected_root = _r3_protected_parent_root(root)
    recovery = build_recovery_parent_catalog(protected_root)
    missing_venue = build_missing_venue_parent_catalog(protected_root)
    before = {
        "catalog": dispositions,
        "availability": {
            str(game_pk): {
                "played_date": semantics["played_date"],
                "result_available_on": semantics["result_available_on"],
            }
            for game_pk, semantics in sorted(availability.items())
        },
        "recovery": _parent_catalog_snapshot(recovery, "r3 recovery"),
        "missing_venue": _parent_catalog_snapshot(
            missing_venue, "r3 missing-venue"
        ),
    }
    # Reauthenticate after all stat snapshots; this closes a replacement that
    # occurs while the fingerprint is being assembled.
    if recovery.reauthenticate() != recovery or missing_venue.reauthenticate() != missing_venue:
        raise ValueError("r3 protected parent authority changed during fingerprint")
    return canonical_sha256(before)


def _replace_recovery_disposition_lineage(
    snapshot: Mapping[str, object],
    replacements: Mapping[int, tuple[str, str]],
    recovery_dispositions: RecoveryDispositionBindings | None = None,
    _r3_availability: Mapping[int, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Replace private adapters with the three authenticated Task 4 sidecars."""
    if recovery_dispositions is not None:
        recovery_dispositions = recovery_dispositions.reauthenticate()
    if _r3_availability is not None and set(_r3_availability) != {630882, 633224}:
        raise ValueError("r3 recovery availability authority differs")
    result = json.loads(_canonical_file_bytes(snapshot))
    if not isinstance(result, dict) or not isinstance(result.get("games"), list):
        raise ValueError("recovery game evidence projection differs")
    synthetic = {adapter for adapter, _derived in replacements.values()}
    derived = {derived for _adapter, derived in replacements.values()}
    for game in result["games"]:
        if not isinstance(game, dict):
            raise ValueError("recovery game evidence row differs")
        if _r3_availability is not None and game.get("game_pk") == 632924:
            raise ValueError("r3 recovery exclusion leaked into game evidence")
        replacement = replacements.get(game.get("season"))
        if replacement is None:
            continue
        adapter_sha256, derived_sha256 = replacement
        if game.get("schedule_query_sidecar_sha256") != adapter_sha256:
            raise ValueError("recovery game disposition adapter lineage differs")
        game["schedule_query_sidecar_sha256"] = derived_sha256
        if game.get("game_pk") in {630882, 633224}:
            if recovery_dispositions is not None:
                binding = next(
                    entry
                    for entry in recovery_dispositions.entries
                    if entry.game_pk == game["game_pk"]
                )
                game["played_date"] = binding.played_date
                game["result_available_on"] = binding.result_available_on
                recovery_dispositions.require_boundary(game)
            elif _r3_availability is not None:
                availability = _r3_availability[game["game_pk"]]
                game["played_date"] = availability["played_date"]
                game["result_available_on"] = availability["result_available_on"]
            else:
                raise ValueError("recovery availability capability is required")
        lineage: dict[str, object] = {
            "anchor_sidecar_sha256": derived_sha256,
            "game": {
                key: value
                for key, value in game.items()
                if key != "source_game_sha256"
            },
        }
        if game.get("played") is True:
            boxscore = game.get("boxscore_query_sidecar_sha256")
            if not isinstance(boxscore, str) or _SHA256.fullmatch(boxscore) is None:
                raise ValueError("recovery game disposition boxscore lineage differs")
            lineage["boxscore_query_sidecar_sha256"] = boxscore
        game["source_game_sha256"] = canonical_sha256(lineage)
    hashes = result.get("source_query_sidecar_sha256s")
    if not isinstance(hashes, list) or not synthetic <= set(hashes):
        raise ValueError("recovery game disposition sidecar population differs")
    result["source_query_sidecar_sha256s"] = sorted(
        (set(hashes) - synthetic) | derived
    )
    return result


def _batch_binding(
    batch: QueryBatch,
    snapshot: object,
    batch_root_relative: Path = _BATCH_ROOT,
) -> dict[str, object]:
    contents = getattr(snapshot, "contents")
    return {
        "batch_id": batch.batch_id,
        "path": (batch_root_relative / f"{batch.batch_id}.json").as_posix(),
        "sha256": _sha256(contents),
        "stage": batch.stage,
    }


def _validate_game_projection(
    snapshot: Mapping[str, object],
    *,
    run_id: str,
    recovery_dispositions: RecoveryDispositionBindings | None = None,
    r3_catalog: object | None = None,
) -> None:
    if (
        recovery_dispositions is not None
        and not isinstance(recovery_dispositions, RecoveryDispositionBindings)
    ):
        raise ValueError("recovery game availability capability is required")
    if recovery_dispositions is not None:
        recovery_dispositions = recovery_dispositions.reauthenticate()
    r3_availability: Mapping[int, Mapping[str, object]] | None = None
    if r3_catalog is not None:
        _r3_dispositions, r3_availability = _r3_recovery_catalog_details(r3_catalog)
    elif run_id == _R3_RECOVERY_RUN_ID:
        raise ValueError("r3 game availability requires its sealed catalog")
    if set(snapshot) != {
        "games",
        "grain",
        "schema_version",
        "source_query_sidecar_sha256s",
    }:
        raise ValueError("game participant projection keys differ")
    games = snapshot.get("games")
    sidecars = snapshot.get("source_query_sidecar_sha256s")
    if (
        snapshot.get("schema_version") != 1
        or snapshot.get("grain") != "season_game_evidence_snapshot"
        or not isinstance(games, list)
        or not games
        or not isinstance(sidecars, list)
        or not sidecars
    ):
        raise ValueError("game participant evidence snapshot is incomplete")
    prior_game_pk: int | None = None
    seen: set[int] = set()
    projected_sidecars: set[str] = set()
    for row in games:
        if not isinstance(row, dict):
            raise ValueError("game participant evidence row differs")
        played = row.get("played")
        expected_keys = _COMMON_GAME_KEYS | (
            {"boxscore_query_sidecar_sha256"} if played is True else set()
        )
        recovery_boundary = (
            run_id == _RECOVERY_RUN_ID and row.get("game_pk") in {630882, 633224}
        )
        r3_boundary = (
            run_id == _R3_RECOVERY_RUN_ID
            and row.get("game_pk") in {630882, 633224}
        )
        if recovery_boundary or r3_boundary:
            expected_keys |= {"played_date", "result_available_on"}
        if run_id == _SOURCE_RUN_ID and played is True:
            expected_keys |= {
                "raw_game_object_sha256",
                "normalized_anchor_sha256",
            }
        if set(row) != expected_keys:
            raise ValueError("game participant projection field keys differ")
        season = row.get("season")
        game_pk = row.get("game_pk")
        if (
            isinstance(season, bool)
            or not isinstance(season, int)
            or isinstance(game_pk, bool)
            or not isinstance(game_pk, int)
            or game_pk <= 0
            or season not in range(2018, 2025)
        ):
            raise ValueError("game participant identity differs")
        away_team_id = _positive_int(row.get("away_team_id"), "away team ID")
        home_team_id = _positive_int(row.get("home_team_id"), "home team ID")
        if away_team_id == home_team_id:
            raise ValueError("game participant team identity differs")
        _positive_int(row.get("venue_id"), "game venue ID")
        _positive_int(row.get("doubleheader_order"), "doubleheader order")
        if (
            type(row.get("neutral_site")) is not bool
            or type(row.get("alternate_site")) is not bool
        ):
            raise ValueError("game participant site flags differ")
        official = _canonical_date(row.get("official_date"), "official date")
        if recovery_boundary:
            if recovery_dispositions is None:
                raise ValueError("recovery game availability capability is required")
            # This validates the exact gamePk, official date, retained UTC start,
            # derived-sidecar hash, and both resume-date fields after a fresh
            # catalog reauthentication.  It is the sole admission path for the
            # deliberately non-local UTC start of a resumed game.
            recovery_dispositions.require_boundary(row)
        if r3_availability is not None:
            if game_pk == 632924:
                raise ValueError("r3 recovery exclusion leaked into game evidence")
            if r3_boundary:
                availability = r3_availability[game_pk]
                if (
                    row.get("played_date") != availability.get("played_date")
                    or row.get("result_available_on")
                    != availability.get("result_available_on")
                ):
                    raise ValueError("r3 recovery availability boundary differs")
        season_start = _canonical_date(
            row.get("season_start_date"), "season start date"
        )
        cutoff = _canonical_date(
            row.get("feature_history_cutoff"), "feature cutoff date"
        )
        if (
            official.year != season
            or season_start.year != season
            or season_start > official
            or cutoff != official - timedelta(days=1)
        ):
            raise ValueError("game participant date cross-fields differ")
        scheduled_start = row.get("scheduled_start_utc")
        if not isinstance(scheduled_start, str) or not scheduled_start.endswith("Z"):
            raise ValueError("scheduled start date-time differs")
        try:
            parsed_start = datetime.fromisoformat(
                scheduled_start.removesuffix("Z") + "+00:00"
            )
        except ValueError as exc:
            raise ValueError("scheduled start date-time is invalid") from exc
        ordinary_start_date = parsed_start.date() in {
            official - timedelta(days=1),
            official,
            official + timedelta(days=1),
        }
        if not ordinary_start_date and not (recovery_boundary or r3_boundary):
            raise ValueError("scheduled start date differs from official date")
        for field in (
            "away_score",
            "home_score",
            "completed_innings",
            "scheduled_innings",
        ):
            _nonnegative_or_none(row.get(field), field)
        terminal_status = row.get("terminal_status")
        if not isinstance(terminal_status, str) or not terminal_status:
            raise ValueError("game participant terminal status differs")
        for field in ("schedule_query_sidecar_sha256", "source_game_sha256"):
            value = row.get(field)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise ValueError(f"game participant {field} differs")
        if run_id == _SOURCE_RUN_ID and played is True:
            for field in ("raw_game_object_sha256", "normalized_anchor_sha256"):
                value = row.get(field)
                if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                    raise ValueError(f"game participant {field} differs")
            validate_source_normalized_anchor_provenance(row)
        projected_sidecars.add(str(row["schedule_query_sidecar_sha256"]))
        if prior_game_pk is not None and game_pk <= prior_game_pk:
            raise ValueError("game participant inventory is not strictly ordered")
        if game_pk in seen:
            raise ValueError("game participant inventory contains duplicate gamePk")
        prior_game_pk = game_pk
        seen.add(game_pk)
        starters = row.get("actual_starter_ids")
        away_hitters = row.get("away_starting_hitter_ids")
        home_hitters = row.get("home_starting_hitter_ids")
        if not all(
            isinstance(value, list) for value in (starters, away_hitters, home_hitters)
        ):
            raise ValueError("game participant lists differ")
        if played is True:
            _positive_ids(starters, 2, "actual starter")
            away_hitter_ids = _positive_ids(away_hitters, 9, "away starting hitter")
            home_hitter_ids = _positive_ids(home_hitters, 9, "home starting hitter")
            if (
                set(away_hitter_ids) & set(home_hitter_ids)
                or row.get("participant_provenance")
                != "RECONSTRUCTED_FINAL_PARTICIPANTS"
                or row.get("verification_status") != "VERIFIED"
            ):
                raise ValueError(
                    "played game participant starter/hitter projection is incomplete"
                )
            boxscore_hash = row.get("boxscore_query_sidecar_sha256")
            if (
                not isinstance(boxscore_hash, str)
                or _SHA256.fullmatch(boxscore_hash) is None
            ):
                raise ValueError("played game boxscore sidecar identity differs")
            projected_sidecars.add(boxscore_hash)
            away_score = row.get("away_score")
            home_score = row.get("home_score")
            winner = row.get("winner_team_id")
            if (
                isinstance(away_score, bool)
                or not isinstance(away_score, int)
                or isinstance(home_score, bool)
                or not isinstance(home_score, int)
                or away_score == home_score
                or winner != (away_team_id if away_score > home_score else home_team_id)
            ):
                raise ValueError("played game outcome cross-fields differ")
        elif played is False:
            if (
                starters
                or away_hitters
                or home_hitters
                or row.get("participant_provenance") != "HISTORICAL_NO_PARTICIPANTS"
                or row.get("verification_status") != "INELIGIBLE_UNPLAYED"
                or row.get("away_score") is not None
                or row.get("home_score") is not None
                or row.get("winner_team_id") is not None
            ):
                raise ValueError("unplayed game participant projection differs")
        else:
            raise ValueError("game participant played partition differs")
        source_hash = row["source_game_sha256"]
        source_game = dict(row)
        del source_game["source_game_sha256"]
        lineage = {
            "anchor_sidecar_sha256": row["schedule_query_sidecar_sha256"],
            "game": source_game,
        }
        if played is True:
            lineage["boxscore_query_sidecar_sha256"] = row[
                "boxscore_query_sidecar_sha256"
            ]
        if canonical_sha256(lineage) != source_hash:
            raise ValueError("game participant source projection hash differs")
    if sidecars != sorted(set(sidecars)) or any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in sidecars
    ):
        raise ValueError("game participant source sidecar population differs")
    if sidecars != sorted(projected_sidecars):
        raise ValueError("game participant source sidecar coverage differs")


def validate_source_normalized_anchor_provenance(game: Mapping[str, object]) -> None:
    """Require a source anchor digest to bind the closed schedule projection."""
    if not isinstance(game, Mapping):
        raise ValueError("source game normalized anchor provenance differs")
    try:
        anchor = {key: game[key] for key in _SOURCE_ANCHOR_KEYS}
        provenance = build_schedule_anchor_provenance([anchor])
        expected = provenance[anchor["game_pk"]]["normalized_anchor_sha256"]
    except (KeyError, ValueError) as exc:
        raise ValueError("source game normalized anchor provenance differs") from exc
    if game.get("normalized_anchor_sha256") != expected:
        raise ValueError("source game normalized anchor provenance differs")


def validate_game_evidence_snapshot(
    snapshot: Mapping[str, object],
    *,
    run_id: str,
    recovery_dispositions: RecoveryDispositionBindings | None = None,
    r3_catalog: object | None = None,
) -> None:
    """Validate a normalized game snapshot with the inventory authority rules."""
    if not isinstance(snapshot, Mapping):
        raise TypeError("game evidence snapshot must be a mapping")
    _validate_game_projection(
        snapshot,
        run_id=run_id,
        recovery_dispositions=recovery_dispositions,
        r3_catalog=r3_catalog,
    )


def _build_current_authority(
    root: Path,
    *,
    read_only_progress: bool = False,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> _Authority:
    capability = _admit_legacy_evidence(legacy_evidence)
    context = _authority_context(root, run_spec_path, acquisition_contract_path)
    capability = _context_legacy_evidence(root, context, capability)
    authority_relative = context.authority_path.relative_to(root)
    contract_relative = context.acquisition_contract_path.relative_to(root)
    acquisition_relative = context.acquisition_audit_root.relative_to(root)
    schedule_inventory_relative = acquisition_relative / "schedule_anchor_inventory.json"
    batch_root_relative = acquisition_relative / "query_batches"
    (
        candidate_relative,
        coverage_review_relative,
        semantic_review_relative,
        mapping_relative,
        seal_relative,
    ) = _timezone_authority_relatives(context)
    run_snapshot = _required_snapshot(root, authority_relative, "game evidence RunSpec")
    contract_snapshot = _required_snapshot(
        root,
        contract_relative,
        "game evidence acquisition contract",
    )
    run_spec = (
        parse_run_spec(_json_object(run_snapshot.contents, "game evidence RunSpec"))
        if isinstance(context.authority, RunSpec)
        else context.authority
    )
    if run_spec.run_id != context.run_id:
        raise ValueError("game evidence RunSpec run_id differs")
    evidence_root = context.raw_root

    schedule_snapshot = _required_snapshot(
        root,
        schedule_inventory_relative,
        "game evidence schedule inventory",
    )
    schedule_record = _json_object(
        schedule_snapshot.contents, "game evidence schedule inventory"
    )
    static_record = schedule_record.get("static_batch")
    static_batch_id = (
        static_record.get("batch_id") if isinstance(static_record, Mapping) else None
    )
    if (
        not isinstance(static_batch_id, str)
        or _SHA256.fullmatch(static_batch_id) is None
    ):
        raise ValueError("game evidence static batch authority differs")
    verified_schedule = load_verified_schedule_anchor_inventory(
        root,
        run_spec,
        acquisition_contract_path=context.acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    if verified_schedule.sha256 != _sha256(schedule_snapshot.contents):
        raise ValueError("game evidence schedule inventory hash differs")
    recovery_dispositions: RecoveryDispositionBindings | None = None
    recovery_authorities: dict[str, object] = {}
    r3_catalog = None
    r3_dispositions: dict[str, object] | None = None
    r3_availability: dict[int, Mapping[str, object]] | None = None
    r3_parent_fingerprint: str | None = None
    r3_authorities: set[str] = set()
    if context.run_id == _RECOVERY_RUN_ID:
        from .v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
        )

        catalog = build_recovery_schedule_conflict_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        recovery_dispositions = RecoveryDispositionBindings.from_catalog_mapping(
            verified_schedule.payload.get("schedule_conflict_dispositions"), catalog
        )
        recovery_authorities = {
            logical_query_id(authority.query): authority for authority in catalog.entries
        }
    elif context.run_id == _R3_RECOVERY_RUN_ID:
        r3_catalog = _build_r3_recovery_catalog(root, context)
        r3_dispositions, r3_availability = _r3_recovery_catalog_details(r3_catalog)
        if canonical_json_bytes(
            verified_schedule.payload.get("schedule_conflict_dispositions")
        ) != canonical_json_bytes(r3_dispositions):
            raise ValueError("game evidence r3 disposition bindings differ")
        r3_authorities = set(r3_dispositions["projection_query_ids"])
        r3_parent_fingerprint = _r3_catalog_parent_fingerprint(root, r3_catalog)

    static_batch = _load_authority_batch(
        root, static_batch_id, context.authority, read_only_progress=read_only_progress
    )
    reference_expected = build_schedule_reference_batch(
        root,
        context.authority_path,
        acquisition_contract_path=context.acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    reference_batch = _load_authority_batch(
        root,
        reference_expected.batch_id,
        context.authority,
        read_only_progress=read_only_progress,
    )
    if (
        static_batch.stage != "STATIC_SCHEDULES"
        or reference_batch != reference_expected
        or reference_batch.stage != "REFERENCE_INPUTS"
        or reference_batch.derived_from_sha256 != verified_schedule.sha256
    ):
        raise ValueError("game evidence query batch authority differs")
    static_batch_snapshot = _required_snapshot(
        root,
        batch_root_relative / f"{static_batch.batch_id}.json",
        "game evidence STATIC_SCHEDULES batch",
    )
    reference_batch_snapshot = _required_snapshot(
        root,
        batch_root_relative / f"{reference_batch.batch_id}.json",
        "game evidence REFERENCE_INPUTS batch",
    )

    timezone_kwargs = (
        {
            "run_spec_path": context.authority_path,
            "acquisition_contract_path": context.acquisition_contract_path,
        }
        if isinstance(context.authority, SourceRunSpec)
        else {}
    )
    timezone_gate = load_verified_timezone_gate(
        root, read_only_progress=read_only_progress, **timezone_kwargs
    )
    timezone_snapshots = tuple(
        _required_snapshot(root, relative, label)
        for relative, label in (
            (candidate_relative, "game evidence timezone candidate"),
            (coverage_review_relative, "game evidence timezone coverage review"),
            (semantic_review_relative, "game evidence timezone semantic review"),
            (mapping_relative, "game evidence timezone mapping"),
            (seal_relative, "game evidence timezone seal"),
        )
    )
    timezone_review_hashes = _timezone_review_hashes_by_role(
        timezone_gate,
        timezone_snapshots[1].contents,
        timezone_snapshots[2].contents,
    )
    if (
        timezone_gate.run_spec_sha256 != _sha256(run_snapshot.contents)
        or timezone_gate.acquisition_contract_sha256
        != _sha256(contract_snapshot.contents)
        or timezone_gate.schedule_inventory_sha256 != verified_schedule.sha256
        or timezone_gate.query_batch_ids
        != (static_batch.batch_id, reference_batch.batch_id)
        or timezone_gate.query_batch_sha256s
        != (
            _sha256(static_batch_snapshot.contents),
            _sha256(reference_batch_snapshot.contents),
        )
        or _sha256(timezone_snapshots[0].contents) != timezone_gate.candidate_sha256
        or _sha256(timezone_snapshots[3].contents) != timezone_gate.mapping_sha256
        or _sha256(timezone_snapshots[4].contents) != timezone_gate.seal_sha256
    ):
        raise ValueError("game evidence timezone authority differs")

    store = (
        V52EvidenceStore.open_read_only(
            evidence_root, legacy_evidence=capability
        )
        if read_only_progress
        else V52EvidenceStore(evidence_root, legacy_evidence=capability)
    )
    schedule_sidecars: list[Mapping[str, object]] = []
    boxscores: dict[int, tuple[bytes, str]] = {}
    evidence_snapshots: list[_BoundSnapshot] = []
    disposition_authority = None
    disposition_verified: VerifiedEvidenceRead | None = None
    disposition_binding: dict[str, object] | None = None
    disposition_adapter_sha256: str | None = None
    recovery_adapters: dict[int, tuple[str, str]] = {}
    r3_adapters: dict[int, tuple[str, str]] = {}
    schedule_provenance: Mapping[int, Mapping[str, object]] | None = None
    if isinstance(context.authority, SourceRunSpec) and context.run_id == _SOURCE_RUN_ID:
        from .v52_duplicate_gamepk_disposition import (
            build_duplicate_disposition_authority,
        )

        disposition_authority = build_duplicate_disposition_authority(
            root,
            source_run_spec_path=context.authority_path,
            acquisition_contract_path=context.acquisition_contract_path,
        )
        if static_batch.queries[1] != disposition_authority.query:
            raise ValueError("game evidence source disposition static query differs")
        disposition_binding = _source_disposition_binding(
            verified_schedule.payload, logical_query_id(disposition_authority.query)
        )
    for query in (*static_batch.queries, *reference_batch.queries):
        query_authority = (
            disposition_authority
            if disposition_authority is not None and query == disposition_authority.query
            else None
        )
        if query_authority is None:
            query_authority = recovery_authorities.get(logical_query_id(query))
        if query_authority is None and logical_query_id(query) in r3_authorities:
            query_authority = r3_catalog
        r3_context = (
            context
            if r3_catalog is not None and query_authority is r3_catalog
            else None
        )
        progress = (
            store.read_verified_query_progress(
                query, authority=query_authority, context=r3_context
            )
            if r3_context is not None
            else store.read_verified_query_progress(query, authority=query_authority)
        )
        if (
            progress.status != "SUCCESS"
            or progress.registered_query != query
            or progress.published_evidence is None
            or progress.pending_attempt_numbers
            or progress.quarantines
        ):
            raise ValueError(
                "game evidence query progress is not an exact terminal success"
            )
        verified = (
            store.read_verified_success(
                query,
                authority=query_authority,
                context=r3_context,
            )
            if r3_context is not None
            else store.read_verified_success(query, authority=query_authority)
        )
        if (
            verified.registered_query != query
            or progress.published_evidence != verified.published_evidence
            or progress.attempt_receipts != verified.attempt_receipts
            or verified.query_sidecar.get("source") != query.source
            or verified.query_sidecar.get("grain") != query.grain
            or verified.query_sidecar.get("parser_version") != query.parser_version
            or verified.query_sidecar.get("schema_version") != 1
            or verified.query_sidecar.get("query_id") != logical_query_id(query)
        ):
            raise ValueError("game evidence verified sidecar envelope differs")
        evidence_snapshots.extend(
            _verified_evidence_snapshots(
                root,
                evidence_root,
                verified,
                legacy_evidence=capability,
            )
        )
        raw = verified.raw_bytes if verified.raw_bytes is not None else b""
        if query_authority is not None:
            expected_sidecar = dict(
                recompute_query_sidecar(
                    query,
                    b"",
                    authority=query_authority,
                    **({"context": r3_context} if r3_context is not None else {}),
                )
            )
        elif context.run_id in {_RECOVERY_RUN_ID, _R3_RECOVERY_RUN_ID}:
            expected_sidecar = dict(
                recompute_query_sidecar(query, raw, context=context)
            )
        else:
            expected_sidecar = dict(recompute_query_sidecar(query, raw))
        if (
            verified.published_evidence.storage_mode == "DERIVED_DISPOSITION"
            and query_authority is None
        ):
            reason = query.requested_range.get("reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError("game evidence derived disposition reason differs")
            expected_sidecar["reason"] = reason
        if expected_sidecar != verified.query_sidecar.get("semantics"):
            raise ValueError("game evidence raw/sidecar semantics differ")
        if (
            disposition_authority is not None
            and query == disposition_authority.query
        ):
            if verified.raw_bytes is not None or disposition_binding is None:
                raise ValueError("game evidence source disposition raw authority differs")
            source_anchors = verified_schedule.payload.get("anchors")
            if not isinstance(source_anchors, list):
                raise ValueError("game evidence source disposition anchors differ")
            adapter = derived_schedule_sidecar_adapter(query, source_anchors)
            schedule_provenance = build_schedule_anchor_provenance(source_anchors)
            disposition_adapter_sha256 = _sha256(_canonical_file_bytes(adapter))
            schedule_sidecars.append(adapter)
            disposition_verified = verified
        elif logical_query_id(query) in recovery_authorities:
            if verified.raw_bytes is not None or recovery_dispositions is None:
                raise ValueError("game evidence recovery disposition raw authority differs")
            source_anchors = verified_schedule.payload.get("anchors")
            season = query.requested_range.get("year")
            if not isinstance(source_anchors, list) or season not in {2019, 2020, 2021}:
                raise ValueError("game evidence recovery disposition anchors differ")
            adapter = _recovery_schedule_adapter(query, source_anchors, season=season)
            adapter_sha256 = _sha256(_canonical_file_bytes(adapter))
            binding = next(
                entry
                for entry in recovery_dispositions.entries
                if entry.query_id == logical_query_id(query)
            )
            if verified.published_evidence.query_sidecar_sha256 != binding.query_sidecar["sha256"]:
                raise ValueError("game evidence recovery disposition sidecar differs")
            recovery_adapters[season] = (
                adapter_sha256,
                binding.query_sidecar["sha256"],
            )
            schedule_sidecars.append(adapter)
        elif logical_query_id(query) in r3_authorities:
            if verified.raw_bytes is not None or r3_dispositions is None:
                raise ValueError("game evidence r3 disposition raw authority differs")
            source_anchors = verified_schedule.payload.get("anchors")
            season = query.requested_range.get("year")
            if not isinstance(source_anchors, list) or season not in {2019, 2020, 2021}:
                raise ValueError("game evidence r3 disposition anchors differ")
            adapter = _recovery_schedule_adapter(query, source_anchors, season=season)
            adapter_sha256 = _sha256(_canonical_file_bytes(adapter))
            r3_adapters[season] = (
                adapter_sha256,
                verified.published_evidence.query_sidecar_sha256,
            )
            schedule_sidecars.append(adapter)
        elif query in static_batch.queries:
            if verified.raw_bytes is None:
                raise ValueError("game evidence schedule raw bytes are missing")
            schedule_sidecar = recompute_schedule_anchor_sidecar(
                verified.raw_bytes,
                query,
                context=(
                    context
                    if context.run_id in {_RECOVERY_RUN_ID, _R3_RECOVERY_RUN_ID}
                    else None
                ),
            )
            if schedule_sidecar != verified.query_sidecar:
                raise ValueError("game evidence schedule sidecar differs")
            schedule_sidecars.append(schedule_sidecar)
        elif "/game/" in query.endpoint and query.endpoint.endswith("/boxscore"):
            if verified.raw_bytes is None:
                raise ValueError("game evidence boxscore raw bytes are missing")
            boxscore_sidecar = recompute_boxscore_sidecar(verified.raw_bytes, query)
            if boxscore_sidecar != verified.query_sidecar:
                raise ValueError("game evidence boxscore sidecar differs")
            game_pk = query.requested_range.get("game_pk")
            if isinstance(game_pk, bool) or not isinstance(game_pk, int):
                raise ValueError("game evidence boxscore gamePk differs")
            sidecar_bytes = _canonical_file_bytes(boxscore_sidecar)
            boxscores[game_pk] = (sidecar_bytes, _sha256(sidecar_bytes))

    if schedule_provenance is None:
        game_snapshot = dict(recompute_game_evidence_snapshot(schedule_sidecars, boxscores))
    else:
        game_snapshot = dict(
            recompute_game_evidence_snapshot(
                schedule_sidecars,
                boxscores,
                schedule_provenance=schedule_provenance,
            )
        )
    if disposition_verified is not None:
        if disposition_adapter_sha256 is None:
            raise AssertionError("source disposition adapter hash was not captured")
        game_snapshot = _replace_source_disposition_lineage(
            game_snapshot,
            synthetic_sidecar_sha256=disposition_adapter_sha256,
            derived_sidecar_sha256=disposition_verified.published_evidence.query_sidecar_sha256,
        )
        if disposition_authority is None:
            raise AssertionError("source disposition authority was not captured")
        rebound_disposition = disposition_authority.reauthenticate()
        rebound_verified = store.read_verified_success(
            rebound_disposition.query, authority=rebound_disposition
        )
        if (
            rebound_verified.published_evidence != disposition_verified.published_evidence
            or rebound_verified.success_receipt != disposition_verified.success_receipt
            or rebound_verified.query_sidecar != disposition_verified.query_sidecar
            or rebound_verified.raw_bytes is not None
        ):
            raise ValueError("game evidence source disposition changed before return")
    if recovery_dispositions is not None:
        if set(recovery_adapters) != {2019, 2020, 2021}:
            raise ValueError("game evidence recovery disposition adapter population differs")
        game_snapshot = _replace_recovery_disposition_lineage(
            game_snapshot, recovery_adapters, recovery_dispositions
        )
        from .v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
        )

        rebound_catalog = build_recovery_schedule_conflict_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        rebound = RecoveryDispositionBindings.from_catalog_mapping(
            recovery_dispositions.as_mapping(), rebound_catalog
        )
        if rebound.as_mapping() != recovery_dispositions.as_mapping():
            raise ValueError("game evidence recovery disposition bindings changed")
    if r3_catalog is not None:
        if set(r3_adapters) != {2019, 2020, 2021} or r3_availability is None:
            raise ValueError("game evidence r3 disposition adapter population differs")
        game_snapshot = _replace_recovery_disposition_lineage(
            game_snapshot, r3_adapters, _r3_availability=r3_availability
        )
        if any(
            not isinstance(game, Mapping)
            or game.get("game_pk") == 632924
            or not isinstance(game.get("venue_id"), int)
            or game["venue_id"] <= 0
            for game in game_snapshot.get("games", [])
        ):
            raise ValueError("r3 recovery exclusion leaked into game evidence")
        rebound_catalog = _build_r3_recovery_catalog(root, context)
        rebound_dispositions, rebound_availability = _r3_recovery_catalog_details(
            rebound_catalog
        )
        rebound_parent_fingerprint = _r3_catalog_parent_fingerprint(
            root, rebound_catalog
        )
        if (
            rebound_dispositions != r3_dispositions
            or rebound_availability != r3_availability
            or rebound_parent_fingerprint != r3_parent_fingerprint
        ):
            raise ValueError("game evidence r3 disposition bindings changed")
        r3_catalog = rebound_catalog
        r3_parent_fingerprint = rebound_parent_fingerprint
    _validate_game_projection(
        game_snapshot,
        run_id=context.run_id,
        recovery_dispositions=recovery_dispositions,
        r3_catalog=r3_catalog,
    )
    games = game_snapshot["games"]
    if not isinstance(games, list):
        raise AssertionError("validated game evidence games must be a list")
    played = [row for row in games if isinstance(row, dict) and row["played"]]
    starter_ids = {starter for row in played for starter in row["actual_starter_ids"]}
    payload = {
        "acquisition_contract": _binding(
            contract_relative, contract_snapshot.contents
        ),
        "artifact_kind": "GAME_EVIDENCE_INVENTORY",
        "counts": {
            "game_count": len(games),
            "played_game_count": len(played),
            "unique_actual_starter_count": len(starter_ids),
            "unplayed_game_count": len(games) - len(played),
            "verified_feature_consumer_game_count": len(played),
        },
        "games": games,
        "query_batches": [
            _batch_binding(static_batch, static_batch_snapshot, batch_root_relative),
            _batch_binding(reference_batch, reference_batch_snapshot, batch_root_relative),
        ],
        "run_id": context.run_id,
        "run_spec": _binding(authority_relative, run_snapshot.contents),
        "schedule_inventory": _binding(
            schedule_inventory_relative, schedule_snapshot.contents
        ),
        "schema_version": 1,
        "source_query_sidecar_sha256s": game_snapshot["source_query_sidecar_sha256s"],
        "timezone_gate": {
            "candidate": {
                "path": candidate_relative.as_posix(),
                "sha256": timezone_gate.candidate_sha256,
            },
            "mapping": {
                "path": mapping_relative.as_posix(),
                "sha256": timezone_gate.mapping_sha256,
            },
            "reviews": [
                {
                    "path": coverage_review_relative.as_posix(),
                    "sha256": timezone_review_hashes[
                        "TIMEZONE_COVERAGE_REVIEW"
                    ],
                },
                {
                    "path": semantic_review_relative.as_posix(),
                    "sha256": timezone_review_hashes[
                        "TIMEZONE_SEMANTIC_REVIEW"
                    ],
                },
            ],
            "seal": {
                "path": seal_relative.as_posix(),
                "sha256": timezone_gate.seal_sha256,
            },
        },
    }
    if disposition_binding is not None:
        payload["duplicate_gamepk_disposition"] = disposition_binding
        payload["dispositioned_query_ids"] = [disposition_binding["query_id"]]
    if recovery_dispositions is not None:
        payload["schedule_conflict_dispositions"] = recovery_dispositions.as_mapping()
        payload["dispositioned_query_ids"] = list(
            sorted(recovery_dispositions.query_ids())
        )
    if r3_dispositions is not None:
        payload["schedule_conflict_dispositions"] = r3_dispositions
        payload["dispositioned_query_ids"] = list(
            r3_dispositions["projection_query_ids"]
        )
    return _Authority(
        payload=payload,
        snapshots=(
            _BoundSnapshot(
                _token_from_snapshot(run_snapshot),
                "game evidence RunSpec",
                _MAX_ARTIFACT_BYTES,
            ),
            _BoundSnapshot(
                _token_from_snapshot(contract_snapshot),
                "game evidence acquisition contract",
                _MAX_ARTIFACT_BYTES,
            ),
            _BoundSnapshot(
                _token_from_snapshot(schedule_snapshot),
                "game evidence schedule inventory",
                _MAX_ARTIFACT_BYTES,
            ),
            _BoundSnapshot(
                _token_from_snapshot(static_batch_snapshot),
                "game evidence STATIC_SCHEDULES batch",
                _MAX_ARTIFACT_BYTES,
            ),
            _BoundSnapshot(
                _token_from_snapshot(reference_batch_snapshot),
                "game evidence REFERENCE_INPUTS batch",
                _MAX_ARTIFACT_BYTES,
            ),
            *(
                _BoundSnapshot(
                    _token_from_snapshot(snapshot), label, _MAX_ARTIFACT_BYTES
                )
                for snapshot, label in zip(
                    timezone_snapshots,
                    (
                        "game evidence timezone candidate",
                        "game evidence timezone coverage review",
                        "game evidence timezone semantic review",
                        "game evidence timezone mapping",
                        "game evidence timezone seal",
                    ),
                    strict=True,
                )
            ),
            *evidence_snapshots,
        ),
        context=context,
        r3_catalog=r3_catalog,
        r3_parent_fingerprint=r3_parent_fingerprint,
        legacy_evidence=capability,
    )


def _rebind_authority(root: Path, authority: _Authority) -> None:
    for bound in authority.snapshots:
        current = _read_artifact_token(
            root,
            bound.snapshot.relative,
            bound.label,
            max_bytes=bound.max_bytes,
        )
        if current != bound.snapshot:
            raise ValueError(f"{bound.label} identity or digest changed")
    if authority.legacy_evidence is not None:
        from .v52_legacy import _require_issued_legacy_evidence

        _require_issued_legacy_evidence(authority.legacy_evidence).rebind()
    if authority.r3_catalog is None:
        if authority.r3_parent_fingerprint is not None:
            raise ValueError("game evidence r3 parent authority binding differs")
        return
    if (
        authority.context.run_id != _R3_RECOVERY_RUN_ID
        or authority.r3_parent_fingerprint is None
    ):
        raise ValueError("game evidence r3 parent authority binding differs")
    rebound_catalog = _build_r3_recovery_catalog(root, authority.context)
    if (
        _r3_catalog_parent_fingerprint(root, rebound_catalog)
        != authority.r3_parent_fingerprint
    ):
        raise ValueError("game evidence r3 protected parent authority changed")
    if _r3_recovery_catalog_details(rebound_catalog) != _r3_recovery_catalog_details(
        authority.r3_catalog
    ):
        raise ValueError("game evidence r3 disposition authority changed")


def _read_inventory_at(
    parent: int,
    inventory_name: str = _INVENTORY_RELATIVE.name,
) -> tuple[bytes, tuple[int, ...]] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(inventory_name, flags, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(
            "game evidence inventory must be a no-follow regular file"
        ) from exc
    try:
        before = os.fstat(descriptor)
        linked = os.stat(inventory_name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _file_identity(before) != _file_identity(linked)
            or before.st_size > _MAX_ARTIFACT_BYTES
        ):
            raise ValueError("game evidence inventory identity or size differs")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_ARTIFACT_BYTES:
                raise ValueError(
                    "game evidence inventory exceeds its bounded read limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        rebound = os.stat(
            inventory_name, dir_fd=parent, follow_symlinks=False
        )
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(after) != _file_identity(rebound)
            or total != after.st_size
        ):
            raise ValueError("game evidence inventory identity changed while reading")
        return b"".join(chunks), _file_identity(after)
    finally:
        os.close(descriptor)


def _remove_owned_inventory(
    root: Path,
    created_identity: tuple[int, ...],
    inventory_relative: Path = _INVENTORY_RELATIVE,
) -> None:
    try:
        parent, descriptors = _open_parent(root, inventory_relative, create=False)
    except BaseException:
        return
    locked = False
    try:
        fcntl.flock(parent, fcntl.LOCK_EX)
        locked = True
        current = _read_inventory_at(parent, inventory_relative.name)
        if current is None or current[1] != created_identity:
            return
        linked = os.stat(
            inventory_relative.name,
            dir_fd=parent,
            follow_symlinks=False,
        )
        if _file_identity(linked) != created_identity:
            return
        os.unlink(inventory_relative.name, dir_fd=parent)
        os.fsync(parent)
    except BaseException:
        return
    finally:
        if locked:
            try:
                fcntl.flock(parent, fcntl.LOCK_UN)
            except OSError:
                pass
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _reconcile_inventory_publication(
    root: Path,
    contents: bytes,
    final_rebind,
    inventory_relative: Path = _INVENTORY_RELATIVE,
) -> None:
    parent, descriptors = _open_parent(root, inventory_relative, create=True)
    temp_name = f".{inventory_relative.name}.{secrets.token_hex(16)}.tmp"
    temp_descriptor = -1
    locked = False
    linked_by_this_call = False
    created_final_identity: tuple[int, ...] | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        temp_descriptor = os.open(temp_name, flags, 0o600, dir_fd=parent)
        view = memoryview(contents)
        written = 0
        while written < len(view):
            count = os.write(temp_descriptor, view[written:])
            if count <= 0:
                raise OSError("game evidence inventory temp write made no progress")
            written += count
        os.fsync(temp_descriptor)
        temp_metadata = os.fstat(temp_descriptor)
        if (
            not stat.S_ISREG(temp_metadata.st_mode)
            or temp_metadata.st_nlink != 1
            or temp_metadata.st_size != len(contents)
        ):
            raise ValueError("game evidence inventory temp identity differs")
        final_rebind()
        fcntl.flock(parent, fcntl.LOCK_EX)
        locked = True
        existing = _read_inventory_at(parent, inventory_relative.name)
        if existing is not None:
            if existing[0] != contents:
                raise RuntimeError(
                    "concurrent immutable game evidence inventory differs"
                )
        else:
            try:
                os.link(
                    temp_name,
                    inventory_relative.name,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raced = _read_inventory_at(parent, inventory_relative.name)
                if raced is None or raced[0] != contents:
                    raise RuntimeError(
                        "concurrent immutable game evidence inventory differs"
                    ) from None
            else:
                linked_by_this_call = True
                os.unlink(temp_name, dir_fd=parent)
                temp_name = ""
                os.fsync(parent)
        if temp_name:
            os.unlink(temp_name, dir_fd=parent)
            temp_name = ""
            os.fsync(parent)
        final = _read_inventory_at(parent, inventory_relative.name)
        if final is None or final[0] != contents:
            raise ValueError("atomic game evidence inventory publication differs")
        if linked_by_this_call:
            created_final_identity = final[1]
    finally:
        if locked:
            fcntl.flock(parent, fcntl.LOCK_UN)
        if temp_name:
            try:
                os.unlink(temp_name, dir_fd=parent)
                os.fsync(parent)
            except FileNotFoundError:
                pass
        if temp_descriptor >= 0:
            os.close(temp_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    try:
        final_rebind()
    except BaseException:
        if created_final_identity is not None:
            _remove_owned_inventory(root, created_final_identity, inventory_relative)
        raise


def build_game_evidence_inventory(
    root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> Mapping[str, object]:
    """Build the deterministic game inventory without publishing it."""
    capability = _admit_legacy_evidence(legacy_evidence)
    root = _trusted_game_root(root)
    authority = _build_current_authority(
        root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
        legacy_evidence=capability,
    )
    return authority.payload


def publish_game_evidence_inventory(
    root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> VerifiedGameEvidenceInventory:
    capability = _admit_legacy_evidence(legacy_evidence)
    root = _trusted_game_root(root)
    authority_kwargs = (
        {}
        if run_spec_path is None and acquisition_contract_path is None
        else {
            "run_spec_path": run_spec_path,
            "acquisition_contract_path": acquisition_contract_path,
        }
    )
    authority = _build_current_authority(
        root, legacy_evidence=capability, **authority_kwargs
    )
    inventory_relative = (
        authority.context.acquisition_audit_root.relative_to(root)
        / "game_evidence_inventory.json"
    )
    contents = _canonical_file_bytes(authority.payload)
    if len(contents) > _MAX_ARTIFACT_BYTES:
        raise ValueError("game evidence inventory exceeds its byte-size limit")

    def rebind() -> None:
        current = _build_current_authority(
            root, legacy_evidence=capability, **authority_kwargs
        )
        if current.payload != authority.payload:
            raise ValueError("game participant authority changed before publication")
        _rebind_authority(root, authority)

    _reconcile_inventory_publication(root, contents, rebind, inventory_relative)
    return load_verified_game_evidence_inventory(
        root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
        legacy_evidence=capability,
    )


def load_verified_game_evidence_inventory(
    root: Path,
    *,
    read_only_progress: bool = False,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
    legacy_evidence: LegacyEvidenceCapability | None = None,
) -> VerifiedGameEvidenceInventory:
    capability = _admit_legacy_evidence(legacy_evidence)
    root = _trusted_game_root(root)
    authority_kwargs = (
        {}
        if run_spec_path is None and acquisition_contract_path is None
        else {
            "run_spec_path": run_spec_path,
            "acquisition_contract_path": acquisition_contract_path,
        }
    )
    context = _authority_context(root, run_spec_path, acquisition_contract_path)
    inventory_relative = (
        context.acquisition_audit_root.relative_to(root)
        / "game_evidence_inventory.json"
    )
    snapshot = _required_snapshot(root, inventory_relative, "game evidence inventory")
    stored = _json_object(snapshot.contents, "game evidence inventory")
    authority = _build_current_authority(
        root,
        read_only_progress=read_only_progress,
        legacy_evidence=capability,
        **authority_kwargs,
    )
    if stored != authority.payload:
        raise ValueError(
            "game evidence inventory differs from current verified participant authority"
        )
    _before_game_inventory_final_rebind()
    rebound = _build_current_authority(
        root,
        read_only_progress=read_only_progress,
        legacy_evidence=capability,
        **authority_kwargs,
    )
    if rebound.payload != authority.payload:
        raise ValueError("game participant authority changed during verified load")
    _rebind_authority(root, authority)
    _rebind_safe_snapshot(
        root,
        snapshot,
        "game evidence inventory",
        max_bytes=_MAX_ARTIFACT_BYTES,
    )
    frozen = _deep_freeze(stored)
    if not isinstance(frozen, Mapping):
        raise AssertionError(
            "verified game evidence inventory must freeze to a mapping"
        )
    return VerifiedGameEvidenceInventory(
        payload=frozen,
        path=root / inventory_relative,
        sha256=_sha256(snapshot.contents),
    )


__all__ = [
    "VerifiedGameEvidenceInventory",
    "build_game_evidence_inventory",
    "load_verified_game_evidence_inventory",
    "publish_game_evidence_inventory",
    "validate_game_evidence_snapshot",
]
