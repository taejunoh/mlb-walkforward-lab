"""Verified, immutable inventory of the seven historical schedule seasons."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType

from .protocols import RunSpec, SourceRunSpec, parse_run_spec
from .v52_evidence import (
    LogicalQuery,
    V52EvidenceStore,
    VerifiedEvidenceRead,
    logical_query_id,
)
from .v52_query_plan import (
    _ACQUISITION_CONTRACT_RELATIVE,
    _RECOVERY_RUN_SPEC_RELATIVE,
    _RUN_SPEC_RELATIVE,
    _SOURCE_RUN_SPEC_RELATIVE,
    SourceAuthorityContext,
    _load_query_batch_for_authority,
    _load_static_contracts,
    _read_repository_file,
    _validate_run_bound_acquisition_contract,
)
from .v52_schedule_conflict_recovery_disposition import RecoveryDispositionBinding
from .v52_source_contracts import canonical_json_bytes
from .v52_sources.schedule import recompute_schedule_anchor_sidecar


_INVENTORY_RELATIVE = Path(
    "audit/v5_2_qd/historical/acquisition/schedule_anchor_inventory.json"
)
_BATCH_RELATIVE = Path("audit/v5_2_qd/historical/acquisition/query_batches")
_ARTIFACT_KIND = "SCHEDULE_ANCHOR_INVENTORY"
_RUN_ID = "historical_walkforward_500"
_SOURCE_RUN_ID = "historical_walkforward_500_superseding_565534"
_RECOVERY_RUN_ID = "historical_walkforward_500_recovery_schedule_conflicts_r2"
_R3_RECOVERY_RUN_ID = (
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
)
_R3_RECOVERY_RUN_SPEC_RELATIVE = Path(
    "protocols/v5_2_qd/"
    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
)
_SEASONS = tuple(range(2018, 2025))
_MAX_INVENTORY_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ANCHOR_KEYS = frozenset(
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
        "raw_game_object_sha256",
        "venue_id",
        "winner_team_id",
    }
)
_OLD_ANCHOR_KEYS = _SOURCE_ANCHOR_KEYS - {"raw_game_object_sha256"}
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
_RECOVERY_TOP_LEVEL_KEYS = _TOP_LEVEL_KEYS | {"schedule_conflict_dispositions"}
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
_DERIVED_SEASON_SOURCE_KEYS = _SEASON_SOURCE_KEYS | {"occurrence_counts"}
_PLAYED_STATUSES = frozenset({"Final", "Completed Early"})
_RECOVERY_BINDINGS_AUTHORIZATION = object()

# Preserve the module seam used by authority-ordering tests while routing the
# actual read through the authority-explicit namespace selector.
load_query_batch = _load_query_batch_for_authority


@dataclass(frozen=True)
class VerifiedScheduleAnchorInventory:
    payload: Mapping[str, object]
    path: Path
    sha256: str


@dataclass(frozen=True)
class _ScheduleAuthority:
    authority_path: Path
    acquisition_contract_path: Path
    authority_sha256: str
    acquisition_contract_sha256: str
    run_id: str
    raw_root: Path
    acquisition_audit_root: Path
    source_context: SourceAuthorityContext | None


@dataclass(frozen=True)
class RecoveryDispositionBindings:
    """Exact, reauthenticated Task 4 disposition evidence for recovery only."""

    entries: tuple[
        RecoveryDispositionBinding,
        RecoveryDispositionBinding,
        RecoveryDispositionBinding,
    ]
    _authorization: object | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _reauthenticate: Callable[[], "RecoveryDispositionBindings"] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.entries, tuple)
            or len(self.entries) != 3
            or not all(isinstance(entry, RecoveryDispositionBinding) for entry in self.entries)
            or tuple(entry.game_pk for entry in self.entries) != (565534, 630882, 633224)
        ):
            raise ValueError("recovery disposition bindings differ")
        if (
            self._authorization is not None
            and self._authorization is not _RECOVERY_BINDINGS_AUTHORIZATION
        ):
            raise ValueError("recovery disposition authority differs")

    def query_ids(self) -> tuple[str, str, str]:
        return tuple(entry.query_id for entry in self.entries)

    def reauthenticate(self) -> "RecoveryDispositionBindings":
        """Fail closed unless the three published bindings are still identical."""
        if (
            self._authorization is not _RECOVERY_BINDINGS_AUTHORIZATION
            or self._reauthenticate is None
        ):
            raise ValueError("recovery disposition authority differs")
        fresh = self._reauthenticate()
        if (
            not isinstance(fresh, RecoveryDispositionBindings)
            or fresh is self
            or fresh.entries != self.entries
        ):
            raise ValueError("recovery disposition authority differs")
        return fresh

    def require_boundary(self, row: Mapping[str, object]) -> date:
        """Return the only two catalog-authenticated resumed-result dates."""
        if not isinstance(row, Mapping):
            raise ValueError("availability boundary differs")
        try:
            bindings = self.reauthenticate()
        except ValueError as exc:
            raise ValueError("availability boundary differs") from exc
        game_pk = row.get("game_pk")
        if type(game_pk) is not int or game_pk not in {630882, 633224}:
            raise ValueError("availability boundary differs")
        entry = next(entry for entry in bindings.entries if entry.game_pk == game_pk)
        expected_official = {
            630882: "2020-08-09",
            633224: "2021-07-21",
        }[game_pk]
        expected = {
            "official_date": expected_official,
            "scheduled_start_utc": entry.retained_scheduled_start_utc,
            "schedule_query_sidecar_sha256": entry.query_sidecar["sha256"],
            "played_date": entry.played_date,
            "result_available_on": entry.result_available_on,
        }
        if (
            entry.played_date is None
            or entry.result_available_on is None
            or any(row.get(name) != value for name, value in expected.items())
        ):
            raise ValueError("availability boundary differs")
        try:
            return date.fromisoformat(entry.result_available_on)
        except ValueError as exc:  # defensive: binding construction is external.
            raise ValueError("availability boundary differs") from exc

    def as_mapping(self) -> dict[str, object]:
        return {
            "entries": [
                {
                    "game_pk": entry.game_pk,
                    "query_id": entry.query_id,
                    "query_sidecar": dict(entry.query_sidecar),
                    "success_receipt": dict(entry.success_receipt),
                    "retained_scheduled_start_utc": entry.retained_scheduled_start_utc,
                    "played_date": entry.played_date,
                    "result_available_on": entry.result_available_on,
                    "raw_object_sha256s": list(entry.raw_object_sha256s),
                    "selected_raw_game_object_sha256s_sha256": entry.selected_raw_game_object_sha256s_sha256,
                    "normalized_games_sha256": entry.normalized_games_sha256,
                    "sidecar_semantics_sha256": entry.sidecar_semantics_sha256,
                }
                for entry in self.entries
            ],
            "query_ids": list(sorted(self.query_ids())),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        bindings: Sequence[RecoveryDispositionBinding],
    ) -> "RecoveryDispositionBindings":
        del value, bindings
        raise ValueError("recovery disposition authority differs")

    @classmethod
    def from_catalog_mapping(
        cls, value: Mapping[str, object], catalog: object
    ) -> "RecoveryDispositionBindings":
        """Bind a recovery mapping to the catalog's fresh evidence replay."""
        from .v52_schedule_conflict_recovery_disposition import (
            RecoveryDispositionCatalog,
            load_recovery_disposition_bindings,
        )

        if not isinstance(catalog, RecoveryDispositionCatalog):
            raise TypeError("recovery disposition catalog is required")
        bindings = load_recovery_disposition_bindings(catalog)
        expected = cls(tuple(bindings))  # type: ignore[arg-type]
        if canonical_json_bytes(value) != canonical_json_bytes(expected.as_mapping()):
            raise ValueError("recovery disposition bindings differ")
        object.__setattr__(expected, "_authorization", _RECOVERY_BINDINGS_AUTHORIZATION)
        object.__setattr__(
            expected,
            "_reauthenticate",
            lambda: cls.from_catalog_mapping(value, catalog),
        )
        return expected


def _load_required_source_checkpoint(
    root: Path,
    run_spec: SourceRunSpec,
    *,
    static_batch_id: str,
    disposition_query_id: str | None = None,
    disposition_query_ids: Sequence[str] = (),
    static_query_ids: Sequence[str] = (),
    disposition_records: Sequence[Mapping[str, object]] = (),
    excluded_game_pks: Sequence[int] = (),
) -> object:
    from .execution_registry import (
        _current_source_checkpoint_authority,
        load_execution_registry,
        load_latest_acquisition_checkpoint,
        load_terminal_acquisition_checkpoint,
    )

    checkpoint_authority = _current_source_checkpoint_authority()
    if checkpoint_authority is None:
        state = load_execution_registry(root, run_spec)
        if state.phase == "SOURCES_READY":
            checkpoint = load_terminal_acquisition_checkpoint(root, run_spec)
        elif state.phase == "ACQUIRING":
            checkpoint = load_latest_acquisition_checkpoint(root, run_spec)
        else:
            raise ValueError("schedule inventory requires ACQUIRING or SOURCES_READY")
    else:
        checkpoint = checkpoint_authority

    if checkpoint is None:
        raise ValueError(
            "schedule inventory requires the exact completed disposition checkpoint"
        )
    query_batch_ids = tuple(checkpoint.query_batch_ids)
    completed_query_ids = tuple(checkpoint.completed_query_ids)
    pending_query_ids = tuple(checkpoint.pending_query_ids)
    quarantined_query_ids = tuple(checkpoint.quarantined_query_ids)
    split_query_ids = tuple(checkpoint.split_query_ids)
    dispositioned_query_ids = tuple(checkpoint.dispositioned_query_ids)
    if run_spec.run_id == _R3_RECOVERY_RUN_ID:
        expected_static_ids = tuple(static_query_ids)
        expected_projection_ids = tuple(disposition_query_ids)
        expected_records = tuple(dict(record) for record in disposition_records)
        if (
            disposition_query_id is not None
            or len(expected_static_ids) != 7
            or len(set(expected_static_ids)) != 7
            or len(expected_projection_ids) != 3
            or len(set(expected_projection_ids)) != 3
            or any(query_id not in expected_static_ids for query_id in expected_projection_ids)
            or len(expected_records) != 4
            or tuple(record.get("game_pk") for record in expected_records)
            != (565534, 630882, 633224, 632924)
            or tuple(record.get("projection_index") for record in expected_records)
            != (0, 1, 2, 2)
            or tuple(record.get("query_id") for record in expected_records)
            != (*expected_projection_ids, expected_projection_ids[2])
            or tuple(excluded_game_pks) != (632924,)
            or getattr(checkpoint, "recovery_variant", None) != "r3"
            or getattr(checkpoint, "stage", None) != "STATIC_SCHEDULES"
            or query_batch_ids != (static_batch_id,)
            or completed_query_ids != expected_static_ids
            or pending_query_ids
            or quarantined_query_ids
            or split_query_ids
            or dispositioned_query_ids != expected_projection_ids
            or tuple(
                dict(record)
                for record in getattr(checkpoint, "recovery_disposition_records", ())
            )
            != expected_records
            or tuple(
                getattr(checkpoint, "recovery_excluded_game_pks", ())
            )
            != (632924,)
        ):
            raise ValueError(
                "schedule inventory requires the exact completed r3 disposition checkpoint"
            )
        return checkpoint
    if disposition_query_id is None:
        raise ValueError(
            "schedule inventory requires the exact completed disposition checkpoint"
        )
    if (
        not query_batch_ids
        or query_batch_ids[0] != static_batch_id
        or dispositioned_query_ids != (disposition_query_id,)
        or disposition_query_id not in completed_query_ids
        or disposition_query_id in pending_query_ids
        or disposition_query_id in quarantined_query_ids
        or disposition_query_id in split_query_ids
    ):
        raise ValueError(
            "schedule inventory requires the exact completed disposition checkpoint"
        )
    return checkpoint


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
    run_spec: RunSpec | SourceRunSpec,
    acquisition_contract_path: Path,
    evidence_root: Path,
) -> _ScheduleAuthority:
    root = _absolute(root)
    if isinstance(run_spec, RunSpec):
        run_contents = _read_repository_file(
            root, _RUN_SPEC_RELATIVE, "schedule inventory RunSpec"
        )
        contract_contents = _read_repository_file(
            root,
            _ACQUISITION_CONTRACT_RELATIVE,
            "schedule inventory acquisition contract",
        )
        if _absolute(acquisition_contract_path) != root / _ACQUISITION_CONTRACT_RELATIVE:
            raise ValueError(
                "acquisition contract path differs from the repository contract"
            )
        parsed_run, run_sha, _, contract_sha = (
            _validate_run_bound_acquisition_contract(
                root, run_contents, contract_contents
            )
        )
        if parse_run_spec(parsed_run) != run_spec or run_spec.run_id != _RUN_ID:
            raise ValueError(
                "RunSpec object differs from authenticated repository bytes"
            )
        namespaces = run_spec.raw.get("namespaces")
        raw_namespace = (
            namespaces.get("raw") if isinstance(namespaces, Mapping) else None
        )
        if not isinstance(raw_namespace, str):
            raise ValueError("RunSpec raw namespace binding differs")
        raw_relative = Path(raw_namespace)
        if (
            raw_relative.is_absolute()
            or not raw_relative.parts
            or any(part in {"", ".", ".."} for part in raw_relative.parts)
        ):
            raise ValueError("RunSpec raw namespace binding differs")
        raw_root = root / raw_relative
        if _absolute(evidence_root) != raw_root:
            raise ValueError("evidence root differs from the RunSpec raw namespace")
        return _ScheduleAuthority(
            authority_path=root / _RUN_SPEC_RELATIVE,
            acquisition_contract_path=root / _ACQUISITION_CONTRACT_RELATIVE,
            authority_sha256=run_sha,
            acquisition_contract_sha256=contract_sha,
            run_id=_RUN_ID,
            raw_root=raw_root,
            acquisition_audit_root=root / _INVENTORY_RELATIVE.parent,
            source_context=None,
        )
    run_relative = (
        _R3_RECOVERY_RUN_SPEC_RELATIVE
        if isinstance(run_spec, SourceRunSpec) and run_spec.run_id == _R3_RECOVERY_RUN_ID
        else _RECOVERY_RUN_SPEC_RELATIVE
        if isinstance(run_spec, SourceRunSpec) and run_spec.run_id == _RECOVERY_RUN_ID
        else _SOURCE_RUN_SPEC_RELATIVE
        if isinstance(run_spec, SourceRunSpec)
        else _RUN_SPEC_RELATIVE
    )
    context = _load_static_contracts(
        root / run_relative,
        acquisition_contract_path,
    )
    if context.repository_root != root or context.authority != run_spec:
        raise ValueError("RunSpec object differs from authenticated repository bytes")
    if _absolute(evidence_root) != context.raw_root:
        raise ValueError("evidence root differs from the RunSpec raw namespace")
    return _ScheduleAuthority(
        authority_path=context.authority_path,
        acquisition_contract_path=context.acquisition_contract_path,
        authority_sha256=context.authority_sha256,
        acquisition_contract_sha256=context.acquisition_contract_sha256,
        run_id=context.run_id,
        raw_root=context.raw_root,
        acquisition_audit_root=context.acquisition_audit_root,
        source_context=context,
    )


def _recovery_source_context(
    context: _ScheduleAuthority,
) -> SourceAuthorityContext:
    """Return the full, authenticated context required by recovery recomputation."""
    source_context = context.source_context
    if (
        source_context is None
        or source_context.run_id != _RECOVERY_RUN_ID
        or source_context.authority_path != context.authority_path
        or source_context.acquisition_contract_path
        != context.acquisition_contract_path
        or source_context.authority_sha256 != context.authority_sha256
        or source_context.acquisition_contract_sha256
        != context.acquisition_contract_sha256
        or source_context.raw_root != context.raw_root
        or source_context.acquisition_audit_root != context.acquisition_audit_root
    ):
        raise ValueError("recovery source authority context differs")
    return source_context


def _r3_recovery_source_context(context: _ScheduleAuthority) -> SourceAuthorityContext:
    source_context = context.source_context
    if (
        source_context is None
        or source_context.run_id != _R3_RECOVERY_RUN_ID
        or source_context.authority_path != context.authority_path
        or source_context.acquisition_contract_path
        != context.acquisition_contract_path
        or source_context.authority_sha256 != context.authority_sha256
        or source_context.acquisition_contract_sha256
        != context.acquisition_contract_sha256
        or source_context.raw_root != context.raw_root
        or source_context.acquisition_audit_root != context.acquisition_audit_root
    ):
        raise ValueError("r3 recovery source authority context differs")
    return source_context


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


def _derived_source_binding(
    root: Path,
    evidence_relative: str,
    query: LogicalQuery,
    verified: VerifiedEvidenceRead,
    anchors: Sequence[Mapping[str, object]],
    occurrence_counts: Mapping[str, int],
    *,
    season: int = 2019,
) -> dict[str, object]:
    published = verified.published_evidence
    if published.storage_mode != "DERIVED_DISPOSITION" or published.raw_sha256 is not None:
        raise ValueError("derived schedule success must not bind raw evidence")
    receipt_relative = _repository_relative(
        root, published.success_receipt_path, "derived schedule success receipt"
    )
    receipt_binding = _file_binding(root, receipt_relative, "derived schedule success receipt")
    sidecar_relative_value = verified.success_receipt.get("query_sidecar_path")
    if not isinstance(sidecar_relative_value, str):
        raise ValueError("derived schedule sidecar path binding differs")
    sidecar_relative = Path(evidence_relative) / sidecar_relative_value
    sidecar_binding = _file_binding(root, sidecar_relative, "derived schedule sidecar")
    if sidecar_binding["sha256"] != published.query_sidecar_sha256:
        raise ValueError("derived schedule sidecar hash binding differs")
    played_count = sum(anchor["played"] is True for anchor in anchors)
    return {
        "game_count": len(anchors),
        "inclusive_end": query.requested_range["end_date"],
        "inclusive_start": query.requested_range["start_date"],
        "occurrence_counts": dict(occurrence_counts),
        "played_game_count": played_count,
        "query_id": logical_query_id(query),
        "query_sidecar": sidecar_binding,
        "raw_sha256": None,
        "season": season,
        "storage_mode": "DERIVED_DISPOSITION",
        "success_receipt": receipt_binding,
        "unplayed_game_count": len(anchors) - played_count,
    }


def _selected_raw_game_object_sha256s(raw: bytes) -> dict[int, str]:
    """Bind every anchor to the exact raw occurrence selected by the parser."""
    from .source_parsers.schedule import select_schedule_occurrences

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("schedule raw must be UTF-8 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("dates"), list):
        raise ValueError("schedule raw root differs")
    for date_entry in payload["dates"]:
        if not isinstance(date_entry, dict) or not isinstance(date_entry.get("games"), list):
            raise ValueError("schedule raw date differs")
    selected_occurrences = select_schedule_occurrences(raw)
    return {
        game["game_pk"]: _sha256_bytes(_canonical_json_bytes(raw_game))
        for game, (_, _, _, raw_game) in selected_occurrences
    }


def _bind_raw_game_object_sha256s(
    anchors: Sequence[Mapping[str, object]], selected: Mapping[int, str]
) -> list[dict[str, object]]:
    bound: list[dict[str, object]] = []
    for raw_anchor in anchors:
        anchor = dict(raw_anchor)
        game_pk = _positive_int(anchor.get("game_pk"), "schedule anchor gamePk")
        raw_sha256 = selected.get(game_pk)
        if raw_sha256 is None:
            raise ValueError("schedule raw selection lacks an anchor")
        anchor["raw_game_object_sha256"] = raw_sha256
        bound.append(anchor)
    if set(selected) != {int(anchor["game_pk"]) for anchor in bound}:
        raise ValueError("schedule raw selection differs from anchors")
    return bound


def _derived_projection_anchors(
    authority: object,
) -> tuple[list[dict[str, object]], Mapping[str, int]]:
    from .v52_duplicate_gamepk_disposition import (
        DuplicateGamePkDispositionAuthority,
        project_schedule_occurrences,
    )

    if not isinstance(authority, DuplicateGamePkDispositionAuthority):
        raise TypeError("schedule disposition authority differs")
    projection = project_schedule_occurrences(authority)
    selected = {
        _positive_int(row.get("game_pk"), "derived selected gamePk"): row.get(
            "raw_game_object_sha256"
        )
        for row in projection.selected_raw_game_object_sha256s
        if isinstance(row, Mapping)
    }
    site_flags = {
        _positive_int(row.get("game_pk"), "derived site-flag gamePk"): row
        for row in projection.site_flags
        if isinstance(row, Mapping)
    }
    if len(selected) != len(projection.normalized_games) or set(selected) != set(site_flags):
        raise ValueError("derived schedule projection population differs")
    anchors: list[dict[str, object]] = []
    for game in projection.normalized_games:
        if not isinstance(game, Mapping):
            raise ValueError("derived normalized game differs")
        game_pk = _positive_int(game.get("game_pk"), "derived normalized gamePk")
        raw_sha256 = selected.get(game_pk)
        flags = site_flags.get(game_pk)
        if not isinstance(raw_sha256, str) or _SHA256.fullmatch(raw_sha256) is None or flags is None:
            raise ValueError("derived selected raw-object provenance differs")
        status = game.get("status")
        played = status in _PLAYED_STATUSES
        away_is_winner = game.get("away_is_winner")
        home_is_winner = game.get("home_is_winner")
        if played and (type(away_is_winner) is not bool or type(home_is_winner) is not bool or away_is_winner == home_is_winner):
            raise ValueError("derived terminal game winner differs")
        marker = game.get("doubleheader_marker")
        order = int(str(marker).removeprefix("GAME")) if marker else 1
        anchor = {
            "alternate_site": flags.get("alternate_site"),
            "away_score": game.get("away_score"),
            "away_team_id": game.get("away_team_id"),
            "completed_innings": game.get("completed_innings"),
            "doubleheader_order": order,
            "game_pk": game_pk,
            "home_score": game.get("home_score"),
            "home_team_id": game.get("home_team_id"),
            "neutral_site": flags.get("neutral_site"),
            "official_date": game.get("official_date"),
            "played": played,
            "raw_game_object_sha256": raw_sha256,
            "scheduled_innings": game.get("scheduled_innings"),
            "scheduled_start_utc": game.get("scheduled_start_utc"),
            "season": 2019,
            "season_start_date": "2019-03-20",
            "terminal_status": status,
            "venue_id": game.get("venue_id"),
            "winner_team_id": (
                game.get("away_team_id") if away_is_winner else game.get("home_team_id")
            ) if played else None,
        }
        anchors.append(anchor)
    return anchors, projection.occurrence_counts


def _recovery_projection_anchors(
    root: Path,
    authority: object,
    context: SourceAuthorityContext,
) -> tuple[list[dict[str, object]], Mapping[str, int]]:
    """Project one Task 4 parent schedule after its closed suppressions."""
    from .v52_schedule_conflict_recovery_disposition import (
        is_canonical_recovery_disposition_authority,
    )

    if not is_canonical_recovery_disposition_authority(authority):
        raise TypeError("recovery schedule disposition authority differs")
    disposition_semantics = authority.semantics.expected_sidecar_semantics
    if not isinstance(disposition_semantics, Mapping):
        raise ValueError("recovery schedule disposition semantics differ")
    suppressed = disposition_semantics.get("suppressed_occurrences")
    if suppressed is None:
        suppressed = [disposition_semantics.get("suppressed_occurrence")]
    if not isinstance(suppressed, (list, tuple)) or not suppressed:
        raise ValueError("recovery schedule suppressed occurrences differ")
    positions: list[tuple[int, int]] = []
    for item in suppressed:
        if not isinstance(item, Mapping):
            raise ValueError("recovery schedule suppressed occurrence differs")
        date_index = item.get("date_index")
        game_index = item.get("game_index")
        if (
            isinstance(date_index, bool)
            or not isinstance(date_index, int)
            or date_index < 0
            or isinstance(game_index, bool)
            or not isinstance(game_index, int)
            or game_index < 0
        ):
            raise ValueError("recovery schedule suppressed occurrence differs")
        positions.append((date_index, game_index))
    parent = disposition_semantics.get("old_authority")
    raw = parent.get("raw") if isinstance(parent, Mapping) else None
    raw_path = raw.get("path") if isinstance(raw, Mapping) else None
    if not isinstance(raw_path, str):
        raise ValueError("recovery schedule parent raw binding differs")
    raw_bytes = _read_repository_file(root, Path(raw_path), "recovery schedule parent raw")
    try:
        payload = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("recovery schedule parent raw differs") from exc
    dates = payload.get("dates") if isinstance(payload, dict) else None
    if not isinstance(dates, list) or not isinstance(payload.get("totalGames"), int):
        raise ValueError("recovery schedule parent raw differs")
    for date_index, game_index in sorted(positions, reverse=True):
        try:
            date_entry = dates[date_index]
            games = date_entry["games"]
            del games[game_index]
            date_entry["totalGames"] -= 1
            payload["totalGames"] -= 1
        except (IndexError, KeyError, TypeError) as exc:
            raise ValueError("recovery schedule suppressed occurrence differs") from exc
    filtered = canonical_json_bytes(payload)
    sidecar = recompute_schedule_anchor_sidecar(filtered, authority.query, context=context)
    sidecar_semantics = sidecar.get("semantics")
    anchors = (
        sidecar_semantics.get("anchors")
        if isinstance(sidecar_semantics, Mapping)
        else None
    )
    counts = disposition_semantics.get("occurrence_counts")
    if not isinstance(anchors, list) or not isinstance(counts, Mapping):
        raise ValueError("recovery schedule projection differs")
    bound = _bind_raw_game_object_sha256s(
        [dict(anchor) for anchor in anchors if isinstance(anchor, Mapping)],
        _selected_raw_game_object_sha256s(filtered),
    )
    if len(bound) != len(anchors):
        raise ValueError("recovery schedule projection differs")
    special = [anchor for anchor in bound if anchor["game_pk"] == authority.game_pk]
    if len(special) != 1 or special[0]["scheduled_start_utc"] != authority.semantics.retained_scheduled_start_utc:
        raise ValueError("recovery schedule retained anchor differs")
    return bound, dict(counts)


def _r3_recovery_projection_anchors(
    root: Path,
    query: LogicalQuery,
    catalog: object,
    context: SourceAuthorityContext,
) -> tuple[list[dict[str, object]], Mapping[str, object]]:
    """Build the one r3 2021 anchor population from its pre-anchor projection."""
    from .v52_missing_venue_recovery_disposition import (
        R3_RECOVERY_RUN_ID,
        MissingVenueRecoveryDispositionCatalog,
    )
    from .v52_recovery_parent_evidence import build_recovery_parent_catalog
    from .v52_sources.schedule import (
        _recompute_schedule_anchor_sidecar,
        project_r3_missing_venue_schedule,
    )

    if (
        not isinstance(catalog, MissingVenueRecoveryDispositionCatalog)
        or context.run_id != R3_RECOVERY_RUN_ID
        or query != catalog.projection_query_for_game_pk(632924)
        or catalog.as_projection().projection_query_id_for_game_pk(632924)
        != logical_query_id(query)
    ):
        raise ValueError("r3 recovery schedule authority differs")
    configured_parent_root = os.environ.get("MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT") or os.environ.get(
        "MLB_V52_SOURCE_AUTHORITY_ROOT"
    )
    parent_root = (
        Path(configured_parent_root).resolve(strict=True)
        if configured_parent_root
        else _absolute(root)
    )
    parent_catalog = build_recovery_parent_catalog(parent_root)
    parent_raw = parent_catalog.read_raw("superseding_2021_633224")
    projection = project_r3_missing_venue_schedule(parent_raw, query, catalog)
    sidecar = _recompute_schedule_anchor_sidecar(
        projection.raw, query, context=context, r3_projection=projection
    )
    semantics = sidecar.get("semantics")
    raw_anchors = semantics.get("anchors") if isinstance(semantics, Mapping) else None
    population = (
        semantics.get("recovery_disposition")
        if isinstance(semantics, Mapping)
        else None
    )
    if not isinstance(raw_anchors, list) or not isinstance(population, Mapping):
        raise ValueError("r3 recovery schedule projection differs")
    anchors = _bind_raw_game_object_sha256s(
        [dict(anchor) for anchor in raw_anchors if isinstance(anchor, Mapping)],
        _selected_raw_game_object_sha256s(projection.raw),
    )
    if (
        len(anchors) != len(raw_anchors)
        or {int(anchor["game_pk"]) for anchor in anchors}
        & catalog.as_projection().excluded_game_pks
        or any(anchor.get("venue_id") is None for anchor in anchors)
    ):
        raise ValueError("r3 recovery exclusion leaked into schedule anchors")
    return anchors, dict(population)


def _r3_parent_raw(root: Path, record: object) -> bytes:
    from .v52_missing_venue_recovery_disposition import MissingVenueDispositionRecord
    from .v52_recovery_parent_evidence import build_recovery_parent_catalog

    if not isinstance(record, MissingVenueDispositionRecord):
        raise ValueError("r3 recovery parent record differs")
    semantics = record.semantics
    parent = semantics.get("parent_evidence")
    if not isinstance(parent, Mapping):
        raise ValueError("r3 recovery parent binding differs")
    raw_binding = parent.get("raw")
    raw_path = raw_binding.get("path") if isinstance(raw_binding, Mapping) else None
    if not isinstance(raw_path, str):
        raise ValueError("r3 recovery parent binding differs")
    configured_parent_root = os.environ.get("MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT") or os.environ.get(
        "MLB_V52_SOURCE_AUTHORITY_ROOT"
    )
    parent_root = (
        Path(configured_parent_root).resolve(strict=True)
        if configured_parent_root
        else _absolute(root)
    )
    parents = build_recovery_parent_catalog(parent_root)
    matches = tuple(
        name
        for name, parent_authority in parents.parents.items()
        if parent_authority.raw.relative.as_posix() == raw_path
    )
    if len(matches) != 1:
        raise ValueError("r3 recovery parent binding differs")
    return parents.read_raw(matches[0])


def _r3_conflict_projection_anchors(
    root: Path,
    query: LogicalQuery,
    catalog: object,
    context: SourceAuthorityContext,
) -> tuple[list[dict[str, object]], Mapping[str, int]]:
    from .v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )
    from .v52_sources.schedule import (
        _recompute_schedule_anchor_sidecar,
        project_r3_conflict_schedule,
    )

    if (
        not isinstance(catalog, MissingVenueRecoveryDispositionCatalog)
        or context.run_id != _R3_RECOVERY_RUN_ID
        or not query.endpoint.startswith("disposition://")
    ):
        raise ValueError("r3 conflict schedule authority differs")
    records = catalog.records_for_query(query)
    if len(records) != 1:
        raise ValueError("r3 conflict schedule record mapping differs")
    filtered, counts = project_r3_conflict_schedule(
        _r3_parent_raw(root, records[0]), query, catalog
    )
    sidecar = _recompute_schedule_anchor_sidecar(filtered, query, context=context)
    semantics = sidecar.get("semantics")
    raw_anchors = semantics.get("anchors") if isinstance(semantics, Mapping) else None
    if not isinstance(raw_anchors, list):
        raise ValueError("r3 conflict schedule projection differs")
    anchors = _bind_raw_game_object_sha256s(
        [dict(anchor) for anchor in raw_anchors if isinstance(anchor, Mapping)],
        _selected_raw_game_object_sha256s(filtered),
    )
    if len(anchors) != len(raw_anchors) or any(
        anchor.get("venue_id") is None for anchor in anchors
    ):
        raise ValueError("r3 conflict schedule anchor differs")
    return anchors, {str(key): int(value) for key, value in counts.items()}


def _r3_disposition_mapping(catalog: object) -> dict[str, object]:
    from .v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )

    if not isinstance(catalog, MissingVenueRecoveryDispositionCatalog):
        raise ValueError("r3 recovery disposition catalog differs")
    projection = catalog.as_projection()
    if (
        projection.record_game_pks != catalog.game_pks()
        or projection.projection_query_ids != catalog.projection_query_ids()
        or projection.record_to_projection_indexes != catalog.record_to_projection_indexes()
        or projection.excluded_game_pks != catalog.excluded_game_pks()
        or projection.record_game_pks != (565534, 630882, 633224, 632924)
        or projection.record_to_projection_indexes != (0, 1, 2, 2)
        or projection.excluded_game_pks != frozenset({632924})
        or len(projection.projection_query_ids) != 3
    ):
        raise ValueError("r3 recovery disposition topology differs")
    return {
        "excluded_game_pks": sorted(projection.excluded_game_pks),
        "projection_query_ids": list(projection.projection_query_ids),
        "record_game_pks": list(projection.record_game_pks),
        "record_to_projection_indexes": list(projection.record_to_projection_indexes),
    }


def _r3_checkpoint_disposition_records(
    catalog: object,
) -> tuple[dict[str, object], ...]:
    """Bind the inventory gate to the registry's four sealed r3 facts."""
    from .v52_missing_venue_recovery_disposition import (
        MissingVenueRecoveryDispositionCatalog,
    )

    if not isinstance(catalog, MissingVenueRecoveryDispositionCatalog):
        raise ValueError("r3 recovery disposition catalog differs")
    records = tuple(
        {
            "game_pk": record.game_pk,
            "projection_index": record.projection_index,
            "query_id": logical_query_id(record.query),
            "semantics_sha256": record.semantics_sha256,
        }
        for record in catalog.records
    )
    if (
        tuple((record["game_pk"], record["projection_index"]) for record in records)
        != ((565534, 0), (630882, 1), (633224, 2), (632924, 2))
        or tuple(record["query_id"] for record in records)
        != (*catalog.projection_query_ids(), catalog.projection_query_ids()[2])
    ):
        raise ValueError("r3 recovery disposition topology differs")
    return records


def _rebind_recovery_schedule_projections(
    root: Path,
    context: _ScheduleAuthority,
    recovery_projections: Mapping[
        str, tuple[list[dict[str, object]], Mapping[str, int]]
    ],
    season_sources: Sequence[Mapping[str, object]],
    *,
    evidence_relative: Path,
    store: V52EvidenceStore,
) -> tuple[RecoveryDispositionBinding, ...]:
    """Replay recovery parent authority and every derived projection once."""
    from .v52_schedule_conflict_recovery_disposition import (
        build_recovery_schedule_conflict_catalog,
        load_recovery_disposition_bindings,
    )

    rebound_catalog = build_recovery_schedule_conflict_catalog(
        root, context.authority_path, context.acquisition_contract_path
    )
    rebound = load_recovery_disposition_bindings(rebound_catalog)
    source_context = _recovery_source_context(context)
    rebound_authorities = {
        logical_query_id(entry.query): entry for entry in rebound_catalog.entries
    }
    if set(rebound_authorities) != set(recovery_projections):
        raise ValueError("recovery disposition projection population changed")
    for query_id, (initial_anchors, initial_counts) in recovery_projections.items():
        authority = rebound_authorities[query_id]
        rebound_anchors, rebound_counts = _recovery_projection_anchors(
            root, authority, source_context
        )
        if (
            rebound_anchors != initial_anchors
            or dict(rebound_counts) != dict(initial_counts)
        ):
            raise ValueError("recovery disposition projection changed before return")
        query = authority.query
        verified = store.read_verified_success(query, authority=authority)
        source = season_sources[int(query.requested_range["year"]) - 2018]
        expected_source = _derived_source_binding(
            root,
            evidence_relative,
            query,
            verified,
            rebound_anchors,
            rebound_counts,
            season=int(query.requested_range["year"]),
        )
        if source != expected_source:
            raise ValueError("recovery disposition source binding changed before return")
    return tuple(rebound)


def build_schedule_anchor_provenance(
    anchors: Sequence[Mapping[str, object]],
) -> Mapping[int, Mapping[str, object]]:
    """Bind compact source-anchor identity and immutable provenance hashes."""
    result: dict[int, Mapping[str, object]] = {}
    for raw_anchor in anchors:
        if not isinstance(raw_anchor, Mapping):
            raise ValueError("source anchor provenance row differs")
        anchor = dict(raw_anchor)
        if set(anchor) != _SOURCE_ANCHOR_KEYS:
            raise ValueError("source anchor provenance keys differ")
        game_pk = _positive_int(
            anchor.get("game_pk"), "source anchor provenance gamePk"
        )
        if game_pk in result:
            raise ValueError("source anchor provenance contains duplicate gamePk")
        raw_sha256 = anchor.get("raw_game_object_sha256")
        if (
            not isinstance(raw_sha256, str)
            or _SHA256.fullmatch(raw_sha256) is None
        ):
            raise ValueError("source anchor raw-object provenance differs")
        result[game_pk] = MappingProxyType(
            {
                "game_pk": game_pk,
                "official_date": anchor["official_date"],
                "away_team_id": anchor["away_team_id"],
                "home_team_id": anchor["home_team_id"],
                "raw_game_object_sha256": raw_sha256,
                "normalized_anchor_sha256": hashlib.sha256(
                    _canonical_json_bytes(anchor)
                ).hexdigest(),
            }
        )
    return MappingProxyType(result)


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


def _validate_payload(
    payload: object,
    *,
    run_id: str = _RUN_ID,
    recovery_bindings: Sequence[RecoveryDispositionBinding] | None = None,
    r3_catalog: object | None = None,
) -> dict[str, object]:
    expected_top_level_keys = (
        _RECOVERY_TOP_LEVEL_KEYS
        if run_id in {_RECOVERY_RUN_ID, _R3_RECOVERY_RUN_ID}
        else _TOP_LEVEL_KEYS
    )
    if not isinstance(payload, dict) or set(payload) != expected_top_level_keys:
        raise ValueError("schedule inventory top-level keys differ")
    if (
        payload.get("artifact_kind") != _ARTIFACT_KIND
        or payload.get("schema_version") != 1
        or payload.get("run_id") != run_id
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
        expected_anchor_keys = (
            _SOURCE_ANCHOR_KEYS
            if run_id in {_SOURCE_RUN_ID, _RECOVERY_RUN_ID, _R3_RECOVERY_RUN_ID}
            else _OLD_ANCHOR_KEYS
        )
        if not isinstance(anchor, dict) or set(anchor) != expected_anchor_keys:
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
        if run_id in {_SOURCE_RUN_ID, _RECOVERY_RUN_ID, _R3_RECOVERY_RUN_ID} and (
            not isinstance(anchor.get("raw_game_object_sha256"), str)
            or _SHA256.fullmatch(anchor["raw_game_object_sha256"]) is None
        ):
            raise ValueError("schedule inventory raw-object provenance differs")
        expected_team_ids.update(
            {
                _positive_int(anchor.get("away_team_id"), "away team ID"),
                _positive_int(anchor.get("home_team_id"), "home team ID"),
            }
        )
        expected_venue_ids.add(_positive_int(anchor.get("venue_id"), "venue ID"))
        per_season[season].append(anchor)

    if run_id == _SOURCE_RUN_ID:
        from .v52_duplicate_gamepk_disposition import (
            RETAINED_OBJECT_SHA256,
            SUPPRESSED_OBJECT_SHA256,
        )

        if any(
            anchor.get("raw_game_object_sha256") == SUPPRESSED_OBJECT_SHA256
            for anchor in anchors
        ):
            raise ValueError("schedule inventory contains suppressed raw occurrence")

        retained = [anchor for anchor in anchors if anchor["game_pk"] == 565534]
        if len(retained) != 1 or (
            retained[0].get("venue_id") != 10
            or retained[0].get("scheduled_start_utc") != "2019-09-07T00:15:00Z"
            or retained[0].get("raw_game_object_sha256") != RETAINED_OBJECT_SHA256
        ):
            raise ValueError("schedule inventory retained 565534 anchor differs")
    if run_id == _RECOVERY_RUN_ID:
        if recovery_bindings is None:
            raise ValueError("recovery disposition bindings are required")
        bindings = RecoveryDispositionBindings(tuple(recovery_bindings))
        if canonical_json_bytes(payload.get("schedule_conflict_dispositions")) != canonical_json_bytes(
            bindings.as_mapping()
        ):
            raise ValueError("recovery disposition bindings differ")
        by_game = {entry.game_pk: entry for entry in bindings.entries}
        for game_pk in (565534, 630882, 633224):
            matches = [anchor for anchor in anchors if anchor["game_pk"] == game_pk]
            binding = by_game[game_pk]
            if (
                len(matches) != 1
                or matches[0].get("scheduled_start_utc")
                != binding.retained_scheduled_start_utc
                or matches[0].get("raw_game_object_sha256")
                not in binding.raw_object_sha256s
            ):
                raise ValueError("schedule inventory retained recovery anchor differs")
    if run_id == _R3_RECOVERY_RUN_ID:
        if r3_catalog is None or canonical_json_bytes(
            payload.get("schedule_conflict_dispositions")
        ) != canonical_json_bytes(_r3_disposition_mapping(r3_catalog)):
            raise ValueError("r3 recovery disposition bindings differ")
        if 632924 in game_pks:
            raise ValueError("r3 recovery exclusion leaked into schedule anchors")

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
        derived_2019 = run_id == _SOURCE_RUN_ID and season == 2019
        recovery_derived = run_id == _RECOVERY_RUN_ID and season in {2019, 2020, 2021}
        r3_recovery_derived = run_id == _R3_RECOVERY_RUN_ID and season in {2019, 2020, 2021}
        expected_keys = (
            _DERIVED_SEASON_SOURCE_KEYS
            if derived_2019 or recovery_derived or r3_recovery_derived
            else _SEASON_SOURCE_KEYS
        )
        if not isinstance(source, dict) or set(source) != expected_keys:
            raise ValueError("schedule inventory season source keys differ")
        _validate_binding(source.get("query_sidecar"), "query sidecar")
        _validate_binding(source.get("success_receipt"), "success receipt")
        rows = per_season[season]
        played = sum(row["played"] is True for row in rows)
        common_invalid = (
            source.get("season") != season
            or source.get("game_count") != len(rows)
            or source.get("played_game_count") != played
            or source.get("unplayed_game_count") != len(rows) - played
            or not isinstance(source.get("query_id"), str)
            or _SHA256.fullmatch(source["query_id"]) is None
        )
        if derived_2019:
            from .v52_duplicate_gamepk_disposition import EXPECTED_OCCURRENCE_COUNTS

            invalid = (
                common_invalid
                or source.get("storage_mode") != "DERIVED_DISPOSITION"
                or source.get("raw_sha256") is not None
                or source.get("occurrence_counts") != dict(EXPECTED_OCCURRENCE_COUNTS)
            )
        elif recovery_derived:
            if recovery_bindings is None:
                raise ValueError("recovery disposition bindings are required")
            binding = next(
                entry
                for entry in RecoveryDispositionBindings(tuple(recovery_bindings)).entries
                if entry.game_pk == {2019: 565534, 2020: 630882, 2021: 633224}[season]
            )
            invalid = (
                common_invalid
                or source.get("storage_mode") != "DERIVED_DISPOSITION"
                or source.get("raw_sha256") is not None
                or source.get("query_id") != binding.query_id
                or source.get("query_sidecar") != dict(binding.query_sidecar)
                or source.get("success_receipt") != dict(binding.success_receipt)
            )
        elif r3_recovery_derived:
            if r3_catalog is None:
                raise ValueError("r3 recovery disposition catalog is required")
            projection = _r3_disposition_mapping(r3_catalog)
            query_id = projection["projection_query_ids"][season - 2019]
            invalid = (
                common_invalid
                or source.get("storage_mode") != "DERIVED_DISPOSITION"
                or source.get("raw_sha256") is not None
                or source.get("query_id") != query_id
                or not isinstance(source.get("occurrence_counts"), Mapping)
                or (
                    season == 2021
                    and (
                        source["occurrence_counts"].get("excluded_game_pks")
                        != [632924]
                        or source["occurrence_counts"].get("pre_normalized_games_count")
                        != 2430
                        or source["occurrence_counts"].get("post_normalized_games_count")
                        != 2429
                    )
                )
            )
        else:
            invalid = (
                common_invalid
                or source.get("storage_mode")
                not in {"FETCHED_RAW", "PROTECTED_LEGACY_REFERENCE"}
                or not isinstance(source.get("raw_sha256"), str)
                or _SHA256.fullmatch(source["raw_sha256"]) is None
            )
        if invalid:
            raise ValueError("schedule inventory season source partition differs")
    if run_id == _R3_RECOVERY_RUN_ID:
        r3_sidecars = tuple(
            _canonical_json_bytes(source["query_sidecar"])
            for season, source in zip(_SEASONS, season_sources, strict=True)
            if season in {2019, 2020, 2021}
        )
        if len(r3_sidecars) != 3 or len(set(r3_sidecars)) != 3:
            raise ValueError("r3 schedule inventory requires exactly three sidecars")
    return payload


def build_schedule_anchor_inventory(
    root: Path,
    run_spec: RunSpec | SourceRunSpec,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
    read_only_progress: bool = False,
) -> Mapping[str, object]:
    root = _absolute(root)
    context = _authenticate(
        root, run_spec, acquisition_contract_path, evidence_root
    )
    evidence_relative = context.raw_root.relative_to(root).as_posix()
    run_sha = context.authority_sha256
    contract_sha = context.acquisition_contract_sha256
    if not isinstance(static_batch_id, str) or _SHA256.fullmatch(static_batch_id) is None:
        raise ValueError("static_batch_id must be a SHA-256")
    batch = load_query_batch(
        root,
        static_batch_id,
        run_spec,
        read_only_progress=read_only_progress,
    )
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
    disposition_authority = None
    source_checkpoint = None
    derived_verified: VerifiedEvidenceRead | None = None
    derived_anchors: list[dict[str, object]] | None = None
    derived_occurrence_counts: Mapping[str, int] | None = None
    recovery_catalog = None
    recovery_bindings: tuple[RecoveryDispositionBinding, ...] | None = None
    recovery_source_context: SourceAuthorityContext | None = None
    r3_catalog = None
    r3_source_context: SourceAuthorityContext | None = None
    r3_checkpoint_records: tuple[dict[str, object], ...] = ()
    r3_static_query_ids: tuple[str, ...] = ()
    r3_projections: dict[str, tuple[list[dict[str, object]], Mapping[str, object]]] = {}
    recovery_authorities: dict[str, object] = {}
    recovery_projections: dict[
        str, tuple[list[dict[str, object]], Mapping[str, int]]
    ] = {}
    if isinstance(run_spec, SourceRunSpec) and context.run_id == _SOURCE_RUN_ID:
        from .v52_duplicate_gamepk_disposition import (
            build_duplicate_disposition_authority,
        )

        disposition_authority = build_duplicate_disposition_authority(
            root,
            source_run_spec_path=context.authority_path,
            acquisition_contract_path=context.acquisition_contract_path,
        )
        if batch.queries[1] != disposition_authority.query:
            raise ValueError("static batch derived 2019 query differs")
        source_checkpoint = _load_required_source_checkpoint(
            root,
            run_spec,
            static_batch_id=batch.batch_id,
            disposition_query_id=logical_query_id(disposition_authority.query),
        )
    elif context.run_id == _RECOVERY_RUN_ID:
        recovery_source_context = _recovery_source_context(context)
        from .v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
            load_recovery_disposition_bindings,
        )

        recovery_catalog = build_recovery_schedule_conflict_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        recovery_bindings = load_recovery_disposition_bindings(recovery_catalog)
        recovery_authorities = {
            logical_query_id(entry.query): entry for entry in recovery_catalog.entries
        }
        if set(recovery_authorities) != {
            logical_query_id(query)
            for query in batch.queries
            if query.endpoint.startswith("disposition://")
        }:
            raise ValueError("recovery static batch disposition queries differ")
        _before_recovery_schedule_projection()
    elif context.run_id == _R3_RECOVERY_RUN_ID:
        from .v52_missing_venue_recovery_disposition import (
            build_missing_venue_recovery_disposition_catalog,
        )

        r3_source_context = _r3_recovery_source_context(context)
        r3_catalog = build_missing_venue_recovery_disposition_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        projection = _r3_disposition_mapping(r3_catalog)
        disposition_ids = tuple(
            logical_query_id(query)
            for query in batch.queries
            if query.endpoint.startswith("disposition://")
        )
        if tuple(projection["projection_query_ids"]) != disposition_ids:
            raise ValueError("r3 static batch disposition queries differ")
        r3_static_query_ids = tuple(
            sorted(logical_query_id(query) for query in batch.queries)
        )
        r3_checkpoint_records = _r3_checkpoint_disposition_records(r3_catalog)
        source_checkpoint = _load_required_source_checkpoint(
            root,
            run_spec,
            static_batch_id=batch.batch_id,
            disposition_query_ids=tuple(projection["projection_query_ids"]),
            static_query_ids=r3_static_query_ids,
            disposition_records=r3_checkpoint_records,
            excluded_game_pks=(632924,),
        )

    store = (
        V52EvidenceStore.open_read_only(_absolute(evidence_root))
        if read_only_progress
        else V52EvidenceStore(_absolute(evidence_root))
    )
    all_anchors: list[dict[str, object]] = []
    season_sources: list[dict[str, object]] = []
    for query in batch.queries:
        authority = (
            disposition_authority
            if disposition_authority is not None and query == disposition_authority.query
            else None
        )
        recovery_authority = recovery_authorities.get(logical_query_id(query))
        r3_authority = (
            r3_catalog
            if r3_catalog is not None
            and logical_query_id(query) in r3_catalog.as_projection().projection_query_ids
            else None
        )
        if sum(item is not None for item in (authority, recovery_authority, r3_authority)) > 1:
            raise AssertionError("schedule disposition authority branch overlaps")
        try:
            verified = store.read_verified_success(
                query,
                authority=authority or recovery_authority or r3_authority,
                context=r3_source_context if r3_authority is not None else None,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("schedule verified success is missing") from exc
        if authority is not None:
            anchors, occurrence_counts = _derived_projection_anchors(authority)
            if verified.raw_bytes is not None:
                raise ValueError("derived schedule success must not expose raw bytes")
            season_sources.append(
                _derived_source_binding(
                    root,
                    evidence_relative,
                    query,
                    verified,
                    anchors,
                    occurrence_counts,
                )
            )
            derived_verified = verified
            derived_anchors = anchors
            derived_occurrence_counts = occurrence_counts
            all_anchors.extend(anchors)
            continue
        if recovery_authority is not None:
            if recovery_source_context is None:
                raise AssertionError("recovery source authority context was not captured")
            anchors, occurrence_counts = _recovery_projection_anchors(
                root, recovery_authority, recovery_source_context
            )
            recovery_projections[logical_query_id(query)] = (
                anchors,
                occurrence_counts,
            )
            if verified.raw_bytes is not None:
                raise ValueError("recovery derived schedule success must not expose raw bytes")
            season_sources.append(
                _derived_source_binding(
                    root,
                    evidence_relative,
                    query,
                    verified,
                    anchors,
                    occurrence_counts,
                    season=int(query.requested_range["year"]),
                )
            )
            all_anchors.extend(anchors)
            continue
        if r3_catalog is not None and logical_query_id(query) in set(
            r3_catalog.as_projection().projection_query_ids
        ):
            if r3_source_context is None:
                raise AssertionError("r3 source authority context was not captured")
            if query.requested_range.get("year") == 2021:
                anchors, occurrence_counts = _r3_recovery_projection_anchors(
                    root, query, r3_catalog, r3_source_context
                )
            else:
                anchors, occurrence_counts = _r3_conflict_projection_anchors(
                    root, query, r3_catalog, r3_source_context
                )
            r3_projections[logical_query_id(query)] = (anchors, occurrence_counts)
            if (
                verified.published_evidence.storage_mode != "DERIVED_DISPOSITION"
                or verified.raw_bytes is not None
            ):
                raise ValueError("r3 derived schedule success differs")
            season_sources.append(
                _derived_source_binding(
                    root,
                    evidence_relative,
                    query,
                    verified,
                    anchors,
                    occurrence_counts,
                    season=int(query.requested_range["year"]),
                )
            )
            all_anchors.extend(anchors)
            continue
        if verified.published_evidence.storage_mode not in {
            "FETCHED_RAW",
            "PROTECTED_LEGACY_REFERENCE",
        }:
            raise ValueError("schedule success is not fetched or protected evidence")
        if verified.raw_bytes is None:
            raise ValueError("schedule success lacks verified raw bytes")
        recomputed = recompute_schedule_anchor_sidecar(
            verified.raw_bytes,
            query,
            context=recovery_source_context or r3_source_context,
        )
        if recomputed != verified.query_sidecar:
            raise ValueError("schedule sidecar differs from raw recomputation")
        semantics = recomputed.get("semantics")
        raw_anchors = semantics.get("anchors") if isinstance(semantics, Mapping) else None
        if not isinstance(raw_anchors, list) or not raw_anchors:
            raise ValueError("schedule sidecar lacks anchors")
        anchors = [dict(anchor) for anchor in raw_anchors if isinstance(anchor, Mapping)]
        if len(anchors) != len(raw_anchors):
            raise ValueError("schedule sidecar anchor differs")
        if isinstance(run_spec, SourceRunSpec):
            anchors = _bind_raw_game_object_sha256s(
                anchors, _selected_raw_game_object_sha256s(verified.raw_bytes)
            )
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
    acquisition_relative = context.acquisition_audit_root.relative_to(root)
    batch_relative = acquisition_relative / "query_batches" / f"{batch.batch_id}.json"
    payload: dict[str, object] = {
        "acquisition_contract": _file_binding(
            root,
            context.acquisition_contract_path.relative_to(root),
            "schedule acquisition contract",
        ),
        "anchors": all_anchors,
        "artifact_kind": _ARTIFACT_KIND,
        "counts": {
            "game_count": len(all_anchors),
            "played_game_count": played_count,
            "season_count": 7,
            "unplayed_game_count": len(all_anchors) - played_count,
        },
        "run_id": context.run_id,
        "run_spec": _file_binding(
            root, context.authority_path.relative_to(root), "schedule RunSpec"
        ),
        "schema_version": 1,
        "season_sources": season_sources,
        "static_batch": {
            "batch_id": batch.batch_id,
            **_file_binding(root, batch_relative, "schedule static batch"),
        },
        "team_ids": team_ids,
        "venue_ids": venue_ids,
    }
    if recovery_bindings is not None:
        payload["schedule_conflict_dispositions"] = RecoveryDispositionBindings(
            tuple(recovery_bindings)
        ).as_mapping()
    elif r3_catalog is not None:
        payload["schedule_conflict_dispositions"] = _r3_disposition_mapping(r3_catalog)
    validated = _validate_payload(
        payload,
        run_id=context.run_id,
        recovery_bindings=recovery_bindings,
        r3_catalog=r3_catalog,
    )
    if isinstance(run_spec, SourceRunSpec) and context.run_id == _SOURCE_RUN_ID:
        if (
            disposition_authority is None
            or source_checkpoint is None
            or derived_verified is None
            or derived_anchors is None
            or derived_occurrence_counts is None
        ):
            raise AssertionError("source schedule authority was not captured")
        rebound_authority = disposition_authority.reauthenticate()
        rebound_anchors, rebound_counts = _derived_projection_anchors(
            rebound_authority
        )
        if (
            rebound_anchors != derived_anchors
            or dict(rebound_counts) != dict(derived_occurrence_counts)
        ):
            raise ValueError("schedule disposition projection changed before return")
        rebound_verified = store.read_verified_success(
            rebound_authority.query, authority=rebound_authority
        )
        if (
            rebound_verified.published_evidence
            != derived_verified.published_evidence
            or rebound_verified.success_receipt != derived_verified.success_receipt
            or rebound_verified.query_sidecar != derived_verified.query_sidecar
            or rebound_verified.raw_bytes is not None
        ):
            raise ValueError("schedule disposition sidecar changed before return")
        rebound_checkpoint = _load_required_source_checkpoint(
            root,
            run_spec,
            static_batch_id=batch.batch_id,
            disposition_query_id=logical_query_id(rebound_authority.query),
        )
        if rebound_checkpoint != source_checkpoint:
            raise ValueError("schedule disposition checkpoint changed before return")
    if recovery_catalog is not None:
        rebound = _rebind_recovery_schedule_projections(
            root,
            context,
            recovery_projections,
            season_sources,
            evidence_relative=evidence_relative,
            store=store,
        )
        rebound_capability = RecoveryDispositionBindings(tuple(rebound))
        if rebound_capability.as_mapping() != payload[
            "schedule_conflict_dispositions"
        ]:
            raise ValueError("recovery disposition bindings changed before return")
    if r3_catalog is not None:
        rebound = build_missing_venue_recovery_disposition_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        if _r3_disposition_mapping(rebound) != payload["schedule_conflict_dispositions"]:
            raise ValueError("r3 recovery disposition bindings changed before return")
        if set(r3_projections) != set(rebound.as_projection().projection_query_ids):
            raise ValueError("r3 recovery projection population changed")
        for query_id, (initial_anchors, initial_counts) in r3_projections.items():
            rebound_query = next(
                query
                for query in batch.queries
                if logical_query_id(query) == query_id
            )
            if rebound_query.requested_range.get("year") == 2021:
                rebound_anchors, rebound_counts = _r3_recovery_projection_anchors(
                    root, rebound_query, rebound, _r3_recovery_source_context(context)
                )
            else:
                rebound_anchors, rebound_counts = _r3_conflict_projection_anchors(
                    root, rebound_query, rebound, _r3_recovery_source_context(context)
                )
            if rebound_anchors != initial_anchors or dict(rebound_counts) != dict(initial_counts):
                raise ValueError("r3 recovery projection changed before return")
        rebound_checkpoint = _load_required_source_checkpoint(
            root,
            run_spec,
            static_batch_id=batch.batch_id,
            disposition_query_ids=tuple(rebound.as_projection().projection_query_ids),
            static_query_ids=r3_static_query_ids,
            disposition_records=_r3_checkpoint_disposition_records(rebound),
            excluded_game_pks=(632924,),
        )
        if rebound_checkpoint != source_checkpoint:
            raise ValueError("r3 schedule checkpoint changed before return")
    return validated


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _open_inventory_parent(
    root: Path, inventory_relative: Path, *, create: bool
) -> tuple[list[int], int]:
    descriptors = [os.open(root, _directory_flags())]
    try:
        current = descriptors[0]
        for component in inventory_relative.parent.parts:
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


def _read_final(
    parent_fd: int, inventory_name: str
) -> tuple[bytes, tuple[int, int, int, int]] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(inventory_name, flags, dir_fd=parent_fd)
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
        linked = os.stat(inventory_name, dir_fd=parent_fd, follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_nlink)
        if identity != (linked.st_dev, linked.st_ino, linked.st_size, linked.st_nlink):
            raise ValueError("schedule inventory identity changed during read")
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _before_schedule_inventory_publish() -> None:
    """Deterministic interruption/race test hook; production does nothing."""


def _before_recovery_schedule_projection() -> None:
    """Deterministic recovery-parent race-test hook; production does nothing."""


def _after_schedule_inventory_rebind() -> None:
    """Deterministic post-rebind race-test hook; production does nothing."""


def _after_schedule_inventory_verified_load() -> None:
    """Deterministic final-inode rebind test hook; production does nothing."""


def _read_inventory_contents(
    root: Path, inventory_relative: Path = _INVENTORY_RELATIVE
) -> bytes:
    try:
        descriptors, parent_fd = _open_inventory_parent(
            root, inventory_relative, create=False
        )
    except FileNotFoundError as exc:
        raise ValueError("schedule anchor inventory is missing") from exc
    try:
        value = _read_final(parent_fd, inventory_relative.name)
        if value is None:
            raise ValueError("schedule anchor inventory is missing")
        return value[0]
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish_schedule_anchor_inventory(
    root: Path,
    run_spec: RunSpec | SourceRunSpec,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
) -> VerifiedScheduleAnchorInventory:
    root = _absolute(root)
    context = _authenticate(
        root, run_spec, acquisition_contract_path, evidence_root
    )
    inventory_relative = (
        context.acquisition_audit_root.relative_to(root)
        / "schedule_anchor_inventory.json"
    )
    payload = build_schedule_anchor_inventory(
        root,
        run_spec,
        acquisition_contract_path=acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
    )
    contents = _canonical_file_bytes(payload)
    descriptors, parent_fd = _open_inventory_parent(
        root, inventory_relative, create=True
    )
    temp_name: str | None = None
    temp_fd = -1
    verified_result: VerifiedScheduleAnchorInventory | None = None
    created_final_identity: tuple[int, int, int, int] | None = None
    expected_final_identity: tuple[int, int, int, int] | None = None
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        existing = _read_final(parent_fd, inventory_relative.name)
        if existing is not None:
            if existing[0] != contents:
                raise RuntimeError("immutable schedule inventory differs")
            expected_final_identity = existing[1]
        else:
            temp_name = f".{inventory_relative.name}.{secrets.token_hex(16)}.tmp"
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
                os.link(temp_name, inventory_relative.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
            except FileExistsError:
                raced = _read_final(parent_fd, inventory_relative.name)
                if raced is None or raced[0] != contents:
                    raise RuntimeError("concurrent schedule inventory differs") from None
            else:
                os.unlink(temp_name, dir_fd=parent_fd)
                temp_name = None
                os.fsync(parent_fd)
            final = _read_final(parent_fd, inventory_relative.name)
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
            rebound_final = _read_final(parent_fd, inventory_relative.name)
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
                owned = _read_final(parent_fd, inventory_relative.name)
                if (
                    owned is not None
                    and owned[0] == contents
                    and owned[1] == created_final_identity
                ):
                    os.unlink(inventory_relative.name, dir_fd=parent_fd)
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
    run_spec: RunSpec | SourceRunSpec,
    *,
    acquisition_contract_path: Path,
    static_batch_id: str,
    evidence_root: Path,
    read_only_progress: bool = False,
) -> VerifiedScheduleAnchorInventory:
    root = _absolute(root)
    context = _authenticate(
        root, run_spec, acquisition_contract_path, evidence_root
    )
    inventory_relative = (
        context.acquisition_audit_root.relative_to(root)
        / "schedule_anchor_inventory.json"
    )
    r3_checkpoint = None
    r3_checkpoint_batch = None
    r3_checkpoint_static_ids: tuple[str, ...] = ()
    r3_catalog = None
    if context.run_id == _R3_RECOVERY_RUN_ID:
        from .v52_missing_venue_recovery_disposition import (
            build_missing_venue_recovery_disposition_catalog,
        )

        r3_checkpoint_batch = load_query_batch(
            root,
            static_batch_id,
            run_spec,
            read_only_progress=read_only_progress,
        )
        if (
            r3_checkpoint_batch.stage != "STATIC_SCHEDULES"
            or r3_checkpoint_batch.derived_from_sha256 is not None
            or len(r3_checkpoint_batch.queries) != 7
            or tuple(
                query.requested_range.get("year")
                for query in r3_checkpoint_batch.queries
            )
            != _SEASONS
            or any(
                query.run_spec_sha256 != context.authority_sha256
                or query.acquisition_contract_sha256
                != context.acquisition_contract_sha256
                for query in r3_checkpoint_batch.queries
            )
        ):
            raise ValueError("static batch is not the exact seven-season replay")
        r3_catalog = build_missing_venue_recovery_disposition_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        projection = _r3_disposition_mapping(r3_catalog)
        if tuple(
            logical_query_id(query)
            for query in r3_checkpoint_batch.queries
            if query.endpoint.startswith("disposition://")
        ) != tuple(projection["projection_query_ids"]):
            raise ValueError("r3 static batch disposition queries differ")
        r3_checkpoint_static_ids = tuple(
            sorted(logical_query_id(query) for query in r3_checkpoint_batch.queries)
        )
        r3_checkpoint = _load_required_source_checkpoint(
            root,
            run_spec,
            static_batch_id=r3_checkpoint_batch.batch_id,
            disposition_query_ids=tuple(projection["projection_query_ids"]),
            static_query_ids=r3_checkpoint_static_ids,
            disposition_records=_r3_checkpoint_disposition_records(r3_catalog),
            excluded_game_pks=(632924,),
        )
    contents = _read_inventory_contents(root, inventory_relative)
    try:
        payload = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("schedule inventory must be UTF-8 JSON") from exc
    if contents != _canonical_file_bytes(payload):
        raise ValueError("schedule inventory must be canonical JSON with one newline")
    recovery_catalog = None
    recovery_bindings: tuple[RecoveryDispositionBinding, ...] | None = None
    # Keep the checkpoint-authenticated r3 catalog captured before the read.
    if context.run_id == _RECOVERY_RUN_ID:
        from .v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
            load_recovery_disposition_bindings,
        )

        recovery_catalog = build_recovery_schedule_conflict_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        recovery_bindings = load_recovery_disposition_bindings(recovery_catalog)
    elif context.run_id == _R3_RECOVERY_RUN_ID:
        from .v52_missing_venue_recovery_disposition import (
            build_missing_venue_recovery_disposition_catalog,
        )

        if r3_catalog is None:
            r3_catalog = build_missing_venue_recovery_disposition_catalog(
                root, context.authority_path, context.acquisition_contract_path
            )
    validated = _validate_payload(
        payload,
        run_id=context.run_id,
        recovery_bindings=recovery_bindings,
        r3_catalog=r3_catalog,
    )
    current = build_schedule_anchor_inventory(
        root,
        run_spec,
        acquisition_contract_path=acquisition_contract_path,
        static_batch_id=static_batch_id,
        evidence_root=evidence_root,
        read_only_progress=read_only_progress,
    )
    if validated != current:
        raise ValueError("schedule inventory differs from current verified evidence")
    rebound = _read_inventory_contents(root, inventory_relative)
    if rebound != contents:
        raise ValueError("schedule inventory bytes changed during verified load")
    if recovery_catalog is not None:
        from .v52_schedule_conflict_recovery_disposition import (
            build_recovery_schedule_conflict_catalog,
            load_recovery_disposition_bindings,
        )

        rebound_catalog = build_recovery_schedule_conflict_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        RecoveryDispositionBindings.from_catalog_mapping(
            validated.get("schedule_conflict_dispositions"), rebound_catalog
        )
    if r3_catalog is not None:
        from .v52_missing_venue_recovery_disposition import (
            build_missing_venue_recovery_disposition_catalog,
        )

        rebound_catalog = build_missing_venue_recovery_disposition_catalog(
            root, context.authority_path, context.acquisition_contract_path
        )
        if _r3_disposition_mapping(rebound_catalog) != validated.get(
            "schedule_conflict_dispositions"
        ):
            raise ValueError("r3 recovery disposition bindings changed during load")
        if r3_checkpoint is None or r3_checkpoint_batch is None:
            raise AssertionError("r3 schedule checkpoint was not captured")
        rebound_checkpoint = _load_required_source_checkpoint(
            root,
            run_spec,
            static_batch_id=r3_checkpoint_batch.batch_id,
            disposition_query_ids=tuple(
                rebound_catalog.as_projection().projection_query_ids
            ),
            static_query_ids=r3_checkpoint_static_ids,
            disposition_records=_r3_checkpoint_disposition_records(
                rebound_catalog
            ),
            excluded_game_pks=(632924,),
        )
        if rebound_checkpoint != r3_checkpoint:
            raise ValueError("r3 schedule checkpoint changed during verified load")
    frozen = _deep_freeze(validated)
    if not isinstance(frozen, Mapping):
        raise AssertionError("verified schedule inventory must freeze to a mapping")
    return VerifiedScheduleAnchorInventory(
        payload=frozen,
        path=root / inventory_relative,
        sha256=_sha256_bytes(contents),
    )


__all__ = [
    "RecoveryDispositionBindings",
    "VerifiedScheduleAnchorInventory",
    "build_schedule_anchor_inventory",
    "build_schedule_anchor_provenance",
    "load_verified_schedule_anchor_inventory",
    "publish_schedule_anchor_inventory",
]
