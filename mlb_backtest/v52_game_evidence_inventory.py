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
from pathlib import Path
from types import MappingProxyType

from .protocols import RunSpec, SourceRunSpec, parse_run_spec
from .v52_evidence import (
    V52EvidenceStore,
    VerifiedEvidenceRead,
    logical_query_id,
)
from .v52_query_plan import (
    SourceAuthorityContext,
    _context_roots,
    _load_static_contracts,
    _read_repository_file,
    _validate_run_bound_acquisition_contract,
    QueryBatch,
    build_schedule_reference_batch,
    load_query_batch,
)
from .v52_schedule_inventory import load_verified_schedule_anchor_inventory
from .v52_source_contracts import (
    canonical_json_bytes,
    canonical_sha256,
    recompute_query_sidecar,
)
from .v52_sources.schedule import (
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
        _MAPPING_RELATIVE,
        acquisition / "venue_timezone_seal.json",
    )


def _before_game_inventory_final_rebind() -> None:
    """Deterministic verified-load race-test hook; production does nothing."""


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
        for key, hash_key, label, max_bytes in (
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
        ):
            lineage = _bound_snapshot(
                root,
                _manifest_relative(reference_record.get(key), label),
                label,
                max_bytes=max_bytes,
            )
            expected_hash = reference_record.get(hash_key)
            if (
                not isinstance(expected_hash, str)
                or lineage.snapshot.sha256 != expected_hash
            ):
                raise ValueError(f"{label} hash differs")
            snapshots.append(lineage)
    elif mode != "DERIVED_DISPOSITION":
        raise ValueError("game evidence storage mode differs")
    return tuple(snapshots)


def _raw_namespace(run_spec: RunSpec) -> Path:
    namespaces = run_spec.raw.get("namespaces")
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


def _validate_game_projection(snapshot: Mapping[str, object]) -> None:
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
        if parsed_start.date() not in {
            official - timedelta(days=1),
            official,
            official + timedelta(days=1),
        }:
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


def validate_game_evidence_snapshot(snapshot: Mapping[str, object]) -> None:
    """Validate a normalized game snapshot with the inventory authority rules."""
    if not isinstance(snapshot, Mapping):
        raise TypeError("game evidence snapshot must be a mapping")
    _validate_game_projection(snapshot)


def _build_current_authority(
    root: Path,
    *,
    read_only_progress: bool = False,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> _Authority:
    context = _authority_context(root, run_spec_path, acquisition_contract_path)
    # Query-batch/checkpoint machinery remains RunSpec-only until Task 6.  The
    # static pair above is authenticated before this deliberate rejection.
    if isinstance(context.authority, SourceRunSpec):
        raise TypeError("SourceRunSpec query-batch authority is unavailable before Task 6")
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
    run_spec = parse_run_spec(
        _json_object(run_snapshot.contents, "game evidence RunSpec")
    )
    if run_spec.run_id != context.run_id:
        raise ValueError("game evidence RunSpec run_id differs")
    evidence_root = root / _raw_namespace(run_spec)

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

    static_batch = load_query_batch(
        root,
        static_batch_id,
        read_only_progress=read_only_progress,
    )
    reference_expected = build_schedule_reference_batch(
        root,
        context.authority_path,
        acquisition_contract_path=context.acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    reference_batch = load_query_batch(
        root,
        reference_expected.batch_id,
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

    timezone_gate = load_verified_timezone_gate(
        root,
        read_only_progress=read_only_progress,
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
        or tuple(_sha256(snapshot.contents) for snapshot in timezone_snapshots)
        != (
            timezone_gate.candidate_sha256,
            *timezone_gate.review_sha256s,
            timezone_gate.mapping_sha256,
            timezone_gate.seal_sha256,
        )
    ):
        raise ValueError("game evidence timezone authority differs")

    store = (
        V52EvidenceStore.open_read_only(evidence_root)
        if read_only_progress
        else V52EvidenceStore(evidence_root)
    )
    schedule_sidecars: list[Mapping[str, object]] = []
    boxscores: dict[int, tuple[bytes, str]] = {}
    evidence_snapshots: list[_BoundSnapshot] = []
    for query in (*static_batch.queries, *reference_batch.queries):
        progress = store.read_verified_query_progress(query)
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
        verified = store.read_verified_success(query)
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
            _verified_evidence_snapshots(root, evidence_root, verified)
        )
        raw = verified.raw_bytes if verified.raw_bytes is not None else b""
        recomputed = recompute_query_sidecar(query, raw)
        expected_sidecar = dict(recomputed)
        if verified.published_evidence.storage_mode == "DERIVED_DISPOSITION":
            reason = query.requested_range.get("reason")
            if not isinstance(reason, str) or not reason:
                raise ValueError("game evidence derived disposition reason differs")
            expected_sidecar["reason"] = reason
        if expected_sidecar != verified.query_sidecar.get("semantics"):
            raise ValueError("game evidence raw/sidecar semantics differ")
        if query in static_batch.queries:
            if verified.raw_bytes is None:
                raise ValueError("game evidence schedule raw bytes are missing")
            schedule_sidecar = recompute_schedule_anchor_sidecar(
                verified.raw_bytes, query
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

    game_snapshot = dict(recompute_game_evidence_snapshot(schedule_sidecars, boxscores))
    _validate_game_projection(game_snapshot)
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
                    "sha256": timezone_gate.review_sha256s[0],
                },
                {
                    "path": semantic_review_relative.as_posix(),
                    "sha256": timezone_gate.review_sha256s[1],
                },
            ],
            "seal": {
                "path": seal_relative.as_posix(),
                "sha256": timezone_gate.seal_sha256,
            },
        },
    }
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
) -> Mapping[str, object]:
    """Build the deterministic game inventory without publishing it."""
    root = _trusted_game_root(root)
    authority = _build_current_authority(
        root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
    )
    return authority.payload


def publish_game_evidence_inventory(
    root: Path,
    *,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> VerifiedGameEvidenceInventory:
    root = _trusted_game_root(root)
    authority_kwargs = (
        {}
        if run_spec_path is None and acquisition_contract_path is None
        else {
            "run_spec_path": run_spec_path,
            "acquisition_contract_path": acquisition_contract_path,
        }
    )
    authority = _build_current_authority(root, **authority_kwargs)
    inventory_relative = (
        authority.context.acquisition_audit_root.relative_to(root)
        / "game_evidence_inventory.json"
    )
    contents = _canonical_file_bytes(authority.payload)
    if len(contents) > _MAX_ARTIFACT_BYTES:
        raise ValueError("game evidence inventory exceeds its byte-size limit")

    def rebind() -> None:
        current = _build_current_authority(root, **authority_kwargs)
        if current.payload != authority.payload:
            raise ValueError("game participant authority changed before publication")
        _rebind_authority(root, authority)

    _reconcile_inventory_publication(root, contents, rebind, inventory_relative)
    return load_verified_game_evidence_inventory(
        root,
        run_spec_path=run_spec_path,
        acquisition_contract_path=acquisition_contract_path,
    )


def load_verified_game_evidence_inventory(
    root: Path,
    *,
    read_only_progress: bool = False,
    run_spec_path: Path | None = None,
    acquisition_contract_path: Path | None = None,
) -> VerifiedGameEvidenceInventory:
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
    if isinstance(context.authority, SourceRunSpec):
        raise TypeError("SourceRunSpec query-batch authority is unavailable before Task 6")
    inventory_relative = (
        context.acquisition_audit_root.relative_to(root)
        / "game_evidence_inventory.json"
    )
    snapshot = _required_snapshot(root, inventory_relative, "game evidence inventory")
    stored = _json_object(snapshot.contents, "game evidence inventory")
    authority = _build_current_authority(
        root,
        read_only_progress=read_only_progress,
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
