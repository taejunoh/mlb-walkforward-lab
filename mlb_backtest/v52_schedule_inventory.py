"""Verified, immutable inventory of the seven historical schedule seasons."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .protocols import RunSpec, parse_run_spec
from .v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    VerifiedEvidenceRead,
    logical_query_id,
)
from .v52_query_plan import (
    _ACQUISITION_CONTRACT_RELATIVE,
    _RUN_SPEC_RELATIVE,
    _read_repository_file,
    _validate_run_bound_acquisition_contract,
    load_query_batch,
)
from .v52_sources.schedule import recompute_schedule_anchor_sidecar


_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
_BATCH_RELATIVE = Path("audit/v5_2_qd/historical/acquisition/query_batches")
_ARTIFACT_KIND = "SCHEDULE_ANCHOR_INVENTORY"
_RUN_ID = "historical_walkforward_500"
_SEASONS = tuple(range(2018, 2025))
_MAX_INVENTORY_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ANCHOR_KEYS = frozenset(
    {
        "alternate_site",
        "away_score",
        "away_team_id",
        "completed_innings",
        "doubleheader_order",
        "game_pk",
        "home_score",
        "home_team_id",
        "neutral_site",
        "official_date",
        "played",
        "scheduled_innings",
        "scheduled_start_utc",
        "season",
        "season_start_date",
        "terminal_status",
        "venue_id",
        "winner_team_id",
    }
)
_TOP_LEVEL_KEYS = frozenset(
    {
        "acquisition_contract",
        "anchors",
        "artifact_kind",
        "counts",
        "run_id",
        "run_spec",
        "schema_version",
        "season_sources",
        "static_batch",
        "team_ids",
        "venue_ids",
    }
)
_SEASON_SOURCE_KEYS = frozenset(
    {
        "game_count",
        "inclusive_end",
        "inclusive_start",
        "played_game_count",
        "query_id",
        "query_sidecar",
        "raw_sha256",
        "season",
        "storage_mode",
        "success_receipt",
        "unplayed_game_count",
    }
)


@dataclass(frozen=True)
class VerifiedScheduleAnchorInventory:
    payload: Mapping[str, object]
    path: Path
    sha256: str


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("schedule inventory must be canonical JSON compatible") from exc


def _canonical_file_bytes(value: object) -> bytes:
    return _canonical_json_bytes(value) + b"\n"


def _sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _deep_freeze(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _binding(path: str, sha256: str) -> dict[str, object]:
    if not isinstance(path, str) or not path or Path(path).is_absolute():
        raise ValueError("artifact binding path differs")
    if _SHA256.fullmatch(sha256) is None:
        raise ValueError("artifact binding hash differs")
    return {"path": path, "sha256": sha256}


def _repository_relative(root: Path, path: Path, label: str) -> Path:
    try:
        relative = _absolute(path).relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under the repository root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} path differs")
    return relative


def _authenticate(
    root: Path,
    run_spec: RunSpec,
    acquisition_contract_path: Path,
    evidence_root: Path,
) -> tuple[str, str, str]:
    if not isinstance(run_spec, RunSpec):
        raise TypeError("run_spec must be a RunSpec")
    root = _absolute(root)
    run_contents = _read_repository_file(root, _RUN_SPEC_RELATIVE, "schedule inventory RunSpec")
    contract_contents = _read_repository_file(
        root,
        _ACQUISITION_CONTRACT_RELATIVE,
        "schedule inventory acquisition contract",
    )
    expected_contract = root / _ACQUISITION_CONTRACT_RELATIVE
    if _absolute(acquisition_contract_path) != expected_contract:
        raise ValueError("acquisition contract path differs from the repository contract")
    parsed_run, run_sha, _, contract_sha = _validate_run_bound_acquisition_contract(
        root,
        run_contents,
        contract_contents,
    )
    if parse_run_spec(parsed_run) != run_spec or run_spec.run_id != _RUN_ID:
        raise ValueError("RunSpec object differs from authenticated repository bytes")
    namespaces = run_spec.raw.get("namespaces")
    raw_namespace = namespaces.get("raw") if isinstance(namespaces, Mapping) else None
    if not isinstance(raw_namespace, str):
        raise ValueError("RunSpec raw namespace binding differs")
    raw_relative = Path(raw_namespace)
    if (
        raw_relative.is_absolute()
        or not raw_relative.parts
        or any(part in {"", ".", ".."} for part in raw_relative.parts)
    ):
        raise ValueError("RunSpec raw namespace binding differs")
    if _absolute(evidence_root) != root / raw_relative:
        raise ValueError("evidence root differs from the RunSpec raw namespace")
    return run_sha, contract_sha, raw_relative.as_posix()


def _file_binding(root: Path, relative: Path, label: str) -> dict[str, object]:
    contents = _read_repository_file(root, relative, label)
    return _binding(relative.as_posix(), _sha256_bytes(contents))


def _source_binding(
    root: Path,
    evidence_relative: str,
    query: LogicalQuery,
    verified: VerifiedEvidenceRead,
    anchors: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    published = verified.published_evidence
    receipt_relative = _repository_relative(
        root, published.success_receipt_path, "success receipt"
    )
    receipt_binding = _file_binding(root, receipt_relative, "schedule success receipt")
    manifest = verified.success_receipt
    sidecar_relative_value = manifest.get("query_sidecar_path")
    if not isinstance(sidecar_relative_value, str):
        raise ValueError("schedule success sidecar path binding differs")
    sidecar_relative = Path(evidence_relative) / sidecar_relative_value
    sidecar_binding = _file_binding(root, sidecar_relative, "schedule query sidecar")
    if sidecar_binding["sha256"] != published.query_sidecar_sha256:
        raise ValueError("schedule query sidecar hash binding differs")
    played_count = sum(anchor["played"] is True for anchor in anchors)
    season = query.requested_range["year"]
    return {
        "game_count": len(anchors),
        "inclusive_end": query.requested_range["end_date"],
        "inclusive_start": query.requested_range["start_date"],
        "played_game_count": played_count,
        "query_id": logical_query_id(query),
        "query_sidecar": sidecar_binding,
        "raw_sha256": published.raw_sha256,
        "season": season,
        "storage_mode": published.storage_mode,
        "success_receipt": receipt_binding,
        "unplayed_game_count": len(anchors) - played_count,
    }


def _validate_binding(value: object, label: str) -> None:
    if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{label} binding differs")
    path = value.get("path")
    sha256 = value.get("sha256")
    if (
        not isinstance(path, str)
        or not path
        or Path(path).is_absolute()
        or any(part in {"", ".", ".."} for part in Path(path).parts)
        or not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
    ):
        raise ValueError(f"{label} binding differs")


def _validate_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _TOP_LEVEL_KEYS:
        raise ValueError("schedule inventory top-level keys differ")
    if (
        payload.get("artifact_kind") != _ARTIFACT_KIND
        or payload.get("schema_version") != 1
        or payload.get("run_id") != _RUN_ID
    ):
        raise ValueError("schedule inventory identity differs")
    _validate_binding(payload["run_spec"], "RunSpec")
    _validate_binding(payload["acquisition_contract"], "acquisition contract")
    static_batch = payload.get("static_batch")
    if not isinstance(static_batch, dict) or set(static_batch) != {"batch_id", "path", "sha256"}:
        raise ValueError("schedule inventory static batch binding differs")
    if not isinstance(static_batch.get("batch_id"), str) or _SHA256.fullmatch(static_batch["batch_id"]) is None:
        raise ValueError("schedule inventory static batch ID differs")
    _validate_binding({"path": static_batch.get("path"), "sha256": static_batch.get("sha256")}, "static batch")

    anchors = payload.get("anchors")
    if not isinstance(anchors, list) or not anchors:
        raise ValueError("schedule inventory anchors must be nonempty")
    prior: tuple[int, int] | None = None
    game_pks: set[int] = set()
    expected_team_ids: set[int] = set()
    expected_venue_ids: set[int] = set()
    per_season: dict[int, list[dict[str, object]]] = {season: [] for season in _SEASONS}
    for index, anchor in enumerate(anchors):
        if not isinstance(anchor, dict) or set(anchor) != _ANCHOR_KEYS:
            raise ValueError("schedule inventory anchor keys differ")
        season = _positive_int(anchor.get("season"), f"anchors[{index}].season")
        game_pk = _positive_int(anchor.get("game_pk"), f"anchors[{index}].game_pk")
        if season not in per_season:
            raise ValueError("schedule inventory anchor season differs")
        identity = (season, game_pk)
        if prior is not None and identity <= prior:
            raise ValueError("schedule inventory anchors are not strictly ordered")
        if game_pk in game_pks:
            raise ValueError("schedule inventory gamePk must be globally unique")
        prior = identity
        game_pks.add(game_pk)
        if type(anchor.get("played")) is not bool:
            raise ValueError("schedule inventory played partition differs")
        expected_team_ids.update(
            {
                _positive_int(anchor.get("away_team_id"), "away team ID"),
                _positive_int(anchor.get("home_team_id"), "home team ID"),
            }
        )
        expected_venue_ids.add(_positive_int(anchor.get("venue_id"), "venue ID"))
        per_season[season].append(anchor)

    team_ids = payload.get("team_ids")
    venue_ids = payload.get("venue_ids")
    if team_ids != sorted(expected_team_ids) or venue_ids != sorted(expected_venue_ids):
        raise ValueError("schedule inventory team or venue IDs differ from anchors")
    counts = payload.get("counts")
    played_total = sum(anchor["played"] is True for anchor in anchors)
    expected_counts = {
        "game_count": len(anchors),
        "played_game_count": played_total,
        "season_count": 7,
        "unplayed_game_count": len(anchors) - played_total,
    }
    if counts != expected_counts:
        raise ValueError("schedule inventory counts differ from anchors")
    season_sources = payload.get("season_sources")
    if not isinstance(season_sources, list) or len(season_sources) != 7:
        raise ValueError("schedule inventory requires exactly seven season sources")
    for season, source in zip(_SEASONS, season_sources, strict=True):
        if not isinstance(source, dict) or set(source) != _SEASON_SOURCE_KEYS:
            raise ValueError("schedule inventory season source keys differ")
        _validate_binding(source.get("query_sidecar"), "query sidecar")
        _validate_binding(source.get("success_receipt"), "success receipt")
        rows = per_season[season]
        played = sum(row["played"] is True for row in rows)
        if (
            source.get("season") != season
            or source.get("game_count") != len(rows)
            or source.get("played_game_count") != played
            or source.get("unplayed_game_count") != len(rows) - played
            or source.get("storage_mode") not in {"FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"}
            or not isinstance(source.get("query_id"), str)
            or _SHA256.fullmatch(source["query_id"]) is None
            or not isinstance(source.get("raw_sha256"), str)
            or _SHA256.fullmatch(source["raw_sha256"]) is None
        ):
            raise ValueError("schedule inventory season source partition differs")
    return payload


def build_schedule_anchor_inventory(
    root: Path,
    run_spec: RunSpec,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
) -> Mapping[str, object]:
    root = _absolute(root)
    run_sha, contract_sha, evidence_relative = _authenticate(
        root, run_spec, acquisition_contract_path, evidence_root
    )
    if not isinstance(static_batch_id, str) or _SHA256.fullmatch(static_batch_id) is None:
        raise ValueError("static_batch_id must be a SHA-256")
    batch = load_query_batch(root, static_batch_id)
    if batch.stage != "STATIC_SCHEDULES" or batch.derived_from_sha256 is not None or len(batch.queries) != 7:
        raise ValueError("static batch is not the exact seven-season replay")
    years = tuple(query.requested_range.get("year") for query in batch.queries)
    if years != _SEASONS:
        raise ValueError("static batch seasons differ from the canonical replay")
    if any(
        query.run_spec_sha256 != run_sha
        or query.acquisition_contract_sha256 != contract_sha
        for query in batch.queries
    ):
        raise ValueError("static batch authority binding differs")

    store = V52EvidenceStore(_absolute(evidence_root))
    all_anchors: list[dict[str, object]] = []
    season_sources: list[dict[str, object]] = []
    for query in batch.queries:
        try:
            verified = store.read_verified_success(query)
        except FileNotFoundError as exc:
            raise RuntimeError("schedule verified success is missing") from exc
        if verified.published_evidence.storage_mode not in {
            "FETCHED_RAW",
            "PROTECTED_LEGACY_REFERENCE",
        }:
            raise ValueError("schedule success is not fetched or protected evidence")
        if verified.raw_bytes is None:
            raise ValueError("schedule success lacks verified raw bytes")
        recomputed = recompute_schedule_anchor_sidecar(verified.raw_bytes, query)
        if recomputed != verified.query_sidecar:
            raise ValueError("schedule sidecar differs from raw recomputation")
        semantics = recomputed.get("semantics")
        raw_anchors = semantics.get("anchors") if isinstance(semantics, Mapping) else None
        if not isinstance(raw_anchors, list) or not raw_anchors:
            raise ValueError("schedule sidecar lacks anchors")
        anchors = [dict(anchor) for anchor in raw_anchors if isinstance(anchor, Mapping)]
        if len(anchors) != len(raw_anchors):
            raise ValueError("schedule sidecar anchor differs")
        season_sources.append(
            _source_binding(root, evidence_relative, query, verified, anchors)
        )
        all_anchors.extend(anchors)

    all_anchors.sort(key=lambda anchor: (int(anchor["season"]), int(anchor["game_pk"])))
    team_ids = sorted(
        {
            int(anchor[field])
            for anchor in all_anchors
            for field in ("away_team_id", "home_team_id")
        }
    )
    venue_ids = sorted({int(anchor["venue_id"]) for anchor in all_anchors})
    played_count = sum(anchor["played"] is True for anchor in all_anchors)
    batch_relative = _BATCH_RELATIVE / f"{batch.batch_id}.json"
    payload: dict[str, object] = {
        "acquisition_contract": _file_binding(
            root, _ACQUISITION_CONTRACT_RELATIVE, "schedule acquisition contract"
        ),
        "anchors": all_anchors,
        "artifact_kind": _ARTIFACT_KIND,
        "counts": {
            "game_count": len(all_anchors),
            "played_game_count": played_count,
            "season_count": 7,
            "unplayed_game_count": len(all_anchors) - played_count,
        },
        "run_id": _RUN_ID,
        "run_spec": _file_binding(root, _RUN_SPEC_RELATIVE, "schedule RunSpec"),
        "schema_version": 1,
        "season_sources": season_sources,
        "static_batch": {
            "batch_id": batch.batch_id,
            **_file_binding(root, batch_relative, "schedule static batch"),
        },
        "team_ids": team_ids,
        "venue_ids": venue_ids,
    }
    return _validate_payload(payload)


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_inventory_parent(
    root: Path, *, create: bool
) -> tuple[list[int], int]:
    descriptors = [os.open(root, _directory_flags())]
    try:
        current = descriptors[0]
        for component in _INVENTORY_RELATIVE.parent.parts:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(component, _directory_flags(), dir_fd=current)
            opened = os.fstat(child)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or not stat.S_ISDIR(linked.st_mode)
                or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
            ):
                os.close(child)
                raise ValueError("schedule inventory ancestor identity differs")
            descriptors.append(child)
            current = child
        return descriptors, current
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _read_final(parent_fd: int) -> tuple[bytes, tuple[int, int, int, int]] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(_INVENTORY_RELATIVE.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError("schedule inventory must be a no-follow regular file") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("schedule inventory must be an unaliased regular file")
        if metadata.st_size > _MAX_INVENTORY_BYTES:
            raise ValueError("schedule inventory exceeds its bounded read limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_INVENTORY_BYTES:
                raise ValueError("schedule inventory exceeds its bounded read limit")
            chunks.append(chunk)
        linked = os.stat(_INVENTORY_RELATIVE.name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_nlink)
        if identity != (linked.st_dev, linked.st_ino, linked.st_size, linked.st_nlink):
            raise ValueError("schedule inventory identity changed during read")
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _before_schedule_inventory_publish() -> None:
    """Deterministic interruption/race test hook; production does nothing."""


def _after_schedule_inventory_rebind() -> None:
    """Deterministic post-rebind race-test hook; production does nothing."""


def _after_schedule_inventory_verified_load() -> None:
    """Deterministic final-inode rebind test hook; production does nothing."""


def _read_inventory_contents(root: Path) -> bytes:
    try:
        descriptors, parent_fd = _open_inventory_parent(root, create=False)
    except FileNotFoundError as exc:
        raise ValueError("schedule anchor inventory is missing") from exc
    try:
        value = _read_final(parent_fd)
        if value is None:
            raise ValueError("schedule anchor inventory is missing")
        return value[0]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish_schedule_anchor_inventory(
    root: Path,
    run_spec: RunSpec,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
) -> VerifiedScheduleAnchorInventory:
    root = _absolute(root)
    payload = build_schedule_anchor_inventory(
        root,
        run_spec,
        acquisition_contract_path=acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
    )
    contents = _canonical_file_bytes(payload)
    descriptors, parent_fd = _open_inventory_parent(root, create=True)
    temp_name: str | None = None
    temp_fd = -1
    verified_result: VerifiedScheduleAnchorInventory | None = None
    created_final_identity: tuple[int, int, int, int] | None = None
    expected_final_identity: tuple[int, int, int, int] | None = None
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        existing = _read_final(parent_fd)
        if existing is not None:
            if existing[0] != contents:
                raise RuntimeError("immutable schedule inventory differs")
            expected_final_identity = existing[1]
        else:
            temp_name = f".{_INVENTORY_RELATIVE.name}.{secrets.token_hex(16)}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
            view = memoryview(contents)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("schedule inventory publication made no progress")
                view = view[written:]
            temp_metadata = os.fstat(temp_fd)
            if not stat.S_ISREG(temp_metadata.st_mode) or temp_metadata.st_nlink != 1:
                raise ValueError("schedule inventory temp publication was aliased")
            os.fsync(temp_fd)
            _before_schedule_inventory_publish()
            rebound_payload = build_schedule_anchor_inventory(
                root,
                run_spec,
                acquisition_contract_path=acquisition_contract_path,
                static_batch_id=static_batch_id,
                evidence_root=evidence_root,
            )
            if rebound_payload != payload:
                raise ValueError(
                    "schedule authority or evidence changed before publication"
                )
            _after_schedule_inventory_rebind()
            try:
                os.link(temp_name, _INVENTORY_RELATIVE.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            except FileExistsError:
                raced = _read_final(parent_fd)
                if raced is None or raced[0] != contents:
                    raise RuntimeError("concurrent schedule inventory differs") from None
            else:
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = None
                os.fsync(parent_fd)
            final = _read_final(parent_fd)
            if final is None or final[0] != contents:
                raise ValueError("schedule inventory publication did not bind final bytes")
            expected_final_identity = final[1]
            if temp_name is None:
                created_final_identity = final[1]
        try:
            verified_result = load_verified_schedule_anchor_inventory(
                root,
                run_spec,
                acquisition_contract_path=acquisition_contract_path,
                static_batch_id=static_batch_id,
                evidence_root=evidence_root,
            )
            _after_schedule_inventory_verified_load()
            rebound_final = _read_final(parent_fd)
            if (
                rebound_final is None
                or rebound_final[0] != contents
                or rebound_final[1] != expected_final_identity
            ):
                raise ValueError(
                    "schedule inventory bytes or inode changed after verified load"
                )
        except BaseException:
            if created_final_identity is not None:
                owned = _read_final(parent_fd)
                if (
                    owned is not None
                    and owned[0] == contents
                    and owned[1] == created_final_identity
                ):
                    os.unlink(_INVENTORY_RELATIVE.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
            raise
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileNotFoundError:
                pass
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
    if verified_result is None:
        raise AssertionError("schedule inventory publication was not verified")
    return verified_result


def load_verified_schedule_anchor_inventory(
    root: Path,
    run_spec: RunSpec,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
) -> VerifiedScheduleAnchorInventory:
    root = _absolute(root)
    contents = _read_inventory_contents(root)
    try:
        payload = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("schedule inventory must be UTF-8 JSON") from exc
    if contents != _canonical_file_bytes(payload):
        raise ValueError("schedule inventory must be canonical JSON with one newline")
    validated = _validate_payload(payload)
    current = build_schedule_anchor_inventory(
        root,
        run_spec,
        acquisition_contract_path=acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
    )
    if validated != current:
        raise ValueError("schedule inventory differs from current verified evidence")
    rebound = _read_inventory_contents(root)
    if rebound != contents:
        raise ValueError("schedule inventory bytes changed during verified load")
    frozen = _deep_freeze(validated)
    if not isinstance(frozen, Mapping):
        raise AssertionError("verified schedule inventory must freeze to a mapping")
    return VerifiedScheduleAnchorInventory(
        payload=frozen,
        path=root / _INVENTORY_RELATIVE,
        sha256=_sha256_bytes(contents),
    )


__all__ = [
    "VerifiedScheduleAnchorInventory",
    "build_schedule_anchor_inventory",
    "load_verified_schedule_anchor_inventory",
    "publish_schedule_anchor_inventory",
]
