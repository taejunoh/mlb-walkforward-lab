"""Read-only adapters for sealed ML, totals, and participant evidence.

The adapters in this module are deliberately boring: they never execute a
model, import a collector, or fetch a URL.  They open the supplied project
root through :mod:`secure_fs`, validate the already-persisted reports, and
retain the exact file snapshots needed for a later authority rebind.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from .daily_totals import (
    validate_totals_model_receipt,
    validate_totals_report,
    validate_totals_trace,
)
from .live_sources import compare_probable_pitchers, parse_pregame_boxscore
from .live_top3_contracts import canonical_json_bytes, validate_run_payload
from .live_top3_trace import validate_trace_payload
from .market_play_contracts import validate_relative_path
from .secure_fs import (
    RegularFileSnapshot,
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
)
from .source_parsers.schedule import parse_schedule


MAX_AUTHORITY_FILE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParticipantGateEvidence:
    """Participant facts used by both pure evaluators."""

    complete: bool
    starter_consistent: bool
    late_scratch: bool
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.complete) is not bool or type(self.starter_consistent) is not bool:
            raise ValueError("participant gate booleans differ")
        if type(self.late_scratch) is not bool:
            raise ValueError("participant late_scratch differs")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason for reason in self.reason_codes
        ):
            raise ValueError("participant reason codes differ")


@dataclass(frozen=True, slots=True)
class MlGameAuthority:
    game_pk: int
    scheduled_start_utc: str
    away_team: str
    home_team: str
    predicted_side: Literal["away", "home"] | None
    pick_probability: Fraction | None
    data_completeness: Fraction | None
    participant: ParticipantGateEvidence
    model_status: Literal["SCORED", "UNSCORED"]
    legacy_primary_rank: int | None


@dataclass(frozen=True, slots=True)
class TotalsGameAuthority:
    game_pk: int
    scheduled_start_utc: str
    away_team: str
    home_team: str
    expected_total: Fraction | None
    residual_samples: tuple[int, ...]
    status: Literal["PROJECTED", "UNPROJECTED"]


class EvidenceAuthority(Protocol):
    """Small common surface accepted by :func:`rebind_authorities`."""

    def rebind(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MlAuthority:
    games: tuple[MlGameAuthority, ...]
    target_date: str
    trace_relative: Path
    _root: Path
    _snapshots: tuple[RegularFileSnapshot, ...]

    @property
    def by_game(self) -> Mapping[int, MlGameAuthority]:
        return MappingProxyType({game.game_pk: game for game in self.games})

    def rebind(self) -> None:
        _rebind_snapshots(self._root, self._snapshots)


@dataclass(frozen=True, slots=True)
class TotalsAuthority:
    games: tuple[TotalsGameAuthority, ...]
    target_date: str
    trace_relative: Path
    _root: Path
    _snapshots: tuple[RegularFileSnapshot, ...]

    @property
    def by_game(self) -> Mapping[int, TotalsGameAuthority]:
        return MappingProxyType({game.game_pk: game for game in self.games})

    def rebind(self) -> None:
        _rebind_snapshots(self._root, self._snapshots)


def _relative(root: Path, value: object, label: str) -> Path:
    """Convert a caller path to a strict path below ``root``."""
    if isinstance(value, str):
        candidate = Path(value)
    elif isinstance(value, Path):
        candidate = value
    else:
        raise ValueError(f"{label} differs")
    root_abs = Path(os.path.abspath(root))
    if candidate.is_absolute():
        candidate_abs = Path(os.path.abspath(candidate))
        try:
            candidate = candidate_abs.relative_to(root_abs)
        except ValueError as exc:
            raise ValueError(f"{label} escapes authority root") from exc
    return validate_relative_path(candidate)


def _json(snapshot: RegularFileSnapshot, label: str) -> dict[str, object]:
    try:
        value = json.loads(snapshot.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _read(root: TrustedRoot, relative: Path, label: str) -> RegularFileSnapshot:
    snapshot = read_regular_snapshot(
        root, relative, label, max_bytes=MAX_AUTHORITY_FILE_BYTES
    )
    if snapshot is None:  # pragma: no cover - missing_ok is not used
        raise ValueError(f"{label} is missing")
    return snapshot


def _read_optional_sidecar(
    root: TrustedRoot,
    snapshots: dict[Path, RegularFileSnapshot],
    relative: Path,
    contents: bytes,
    label: str,
) -> None:
    sidecar_relative = relative.with_suffix(relative.suffix + ".sha256")
    sidecar = read_regular_snapshot(
        root, sidecar_relative, f"{label} sidecar", max_bytes=65, missing_ok=True
    )
    if sidecar is None:
        return
    if sidecar.contents != f"{_digest(contents)}\n".encode("ascii"):
        raise ValueError(f"{label} sidecar differs")
    snapshots[sidecar_relative] = sidecar


def _digest(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _canonical_digest(value: Mapping[str, object]) -> str:
    # The live report's content_sha256 authenticates the canonical body without
    # the self-referential digest field (the same rule used by its validator).
    body = {key: item for key, item in value.items() if key != "content_sha256"}
    return _digest(canonical_json_bytes(body))


def _snapshot_once(
    root: TrustedRoot,
    snapshots: dict[Path, RegularFileSnapshot],
    relative: Path,
    label: str,
    expected_sha: str | None = None,
) -> RegularFileSnapshot:
    snapshot = snapshots.get(relative)
    if snapshot is None:
        snapshot = _read(root, relative, label)
        snapshots[relative] = snapshot
    if expected_sha is not None and _digest(snapshot.contents) != expected_sha:
        raise ValueError(f"{label} digest differs")
    return snapshot


def _source_files(
    root: TrustedRoot,
    snapshots: dict[Path, RegularFileSnapshot],
    source: Mapping[str, object],
    label: str,
) -> None:
    """Read all path/digest pairs in a source identity object.

    Both the live and totals contracts use ``*_relative``/``*_sha256`` pairs;
    handling them generically keeps this adapter strict when a new component
    is added to either contract.
    """
    for key, value in source.items():
        if not key.endswith("_relative") or value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{label} {key} differs")
        relative = _relative(root.path, value, f"{label} {key}")
        digest_key = key.removesuffix("_relative") + "_sha256"
        expected = source.get(digest_key)
        if expected is not None and (
            not isinstance(expected, str) or len(expected) != 64
        ):
            raise ValueError(f"{label} {digest_key} differs")
        _snapshot_once(root, snapshots, relative, f"{label} {key}", expected)


def _read_live_binding_files(
    root: TrustedRoot,
    snapshots: dict[Path, RegularFileSnapshot],
    binding: Mapping[str, object],
) -> None:
    _source_files(root, snapshots, binding, "live source binding")
    receipt_relative = _relative(root.path, binding["receipt_relative"], "receipt")
    receipt_snapshot = _snapshot_once(
        root,
        snapshots,
        receipt_relative,
        "live source receipt",
        str(binding["receipt_sha256"]),
    )
    receipt = _json(receipt_snapshot, "live source receipt")
    if receipt.get("payload_relative") != binding.get("payload_relative"):
        raise ValueError("live receipt payload_relative binding differs")
    if receipt.get("payload_sha256") != binding.get("payload_sha256"):
        raise ValueError("live receipt payload_sha256 binding differs")
    if receipt.get("manifest_sha256") != binding.get("manifest_sha256"):
        raise ValueError("live receipt manifest_sha256 binding differs")
    manifest_relative = receipt.get("manifest_relative")
    if not isinstance(manifest_relative, str):
        raise ValueError("live receipt manifest_relative binding differs")
    _snapshot_once(
        root,
        snapshots,
        _relative(root.path, manifest_relative, "manifest"),
        "live source manifest",
        str(binding["manifest_sha256"]),
    )
    claim = receipt.get("claim_relative")
    claim_sha = receipt.get("claim_sha256")
    if claim is not None:
        if not isinstance(claim, str) or not isinstance(claim_sha, str):
            raise ValueError("live receipt claim binding differs")
        _snapshot_once(
            root,
            snapshots,
            _relative(root.path, claim, "claim"),
            "live source claim",
            claim_sha,
        )


def _participant_evidence(
    schedule: Mapping[int, Mapping[str, object]],
    game_pk: int,
    bindings: Sequence[Mapping[str, object]],
    snapshots: dict[Path, RegularFileSnapshot],
    root: TrustedRoot,
    row_reasons: Sequence[object] = (),
) -> ParticipantGateEvidence:
    reasons: set[str] = {reason for reason in row_reasons if isinstance(reason, str)}
    matching = [
        binding
        for binding in bindings
        if game_pk in binding.get("game_pks", ())
    ]
    if len(matching) != 1:
        reasons.add("MISSING_OR_DUPLICATE_PARTICIPANT_BINDING")
        return ParticipantGateEvidence(False, False, False, tuple(sorted(reasons)))
    binding = matching[0]
    payload = _snapshot_once(
        root,
        snapshots,
        _relative(root.path, binding["payload_relative"], "participant payload"),
        "participant payload",
        str(binding["payload_sha256"]),
    )
    try:
        parsed = parse_pregame_boxscore(payload.contents, game_pk=game_pk)
        parsed = compare_probable_pitchers(schedule[game_pk], parsed)
        reasons.update(parsed.reason_codes)
        complete = bool(parsed.complete)
        starter_consistent = "STARTER_CONFLICT" not in reasons and "MISSING_SCHEDULE_PROBABLE" not in reasons
    except (TypeError, ValueError) as exc:
        reasons.add("PARTICIPANT_PARSE_FAILURE")
        complete = False
        starter_consistent = False
        if str(exc):
            reasons.add(str(exc).upper().replace(" ", "_"))
    late_scratch = any(
        "POSTSTART" in reason or "LATE_SCRATCH" in reason for reason in reasons
    )
    return ParticipantGateEvidence(
        complete,
        starter_consistent,
        late_scratch,
        tuple(sorted(reasons)),
    )


def _fraction(value: object, label: str, *, maximum: Fraction = Fraction(1)) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal, Fraction)):
        raise ValueError(f"{label} differs")
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{label} differs") from exc
    if result < 0 or result > maximum:
        raise ValueError(f"{label} differs")
    return result


def _ml_games(
    report: Mapping[str, object],
    trace: Mapping[str, object],
    schedule: Mapping[int, Mapping[str, object]],
    participant_bindings: Sequence[Mapping[str, object]],
    root: TrustedRoot,
    snapshots: dict[Path, RegularFileSnapshot],
) -> tuple[MlGameAuthority, ...]:
    trace_rows = {int(row["game_pk"]): row for row in trace["games"]}
    ranked = list(report["verified_top3"]) + list(report["preliminary_top3"])
    primary_ranked = {int(row["game_pk"]): row for row in ranked}
    primary_unscored_rows = (*report["unscored_games"], *report["excluded_games"])
    primary_unscored = {int(row["game_pk"]): row for row in primary_unscored_rows}
    if len(primary_unscored) != len(primary_unscored_rows):
        raise ValueError("ML unscored/excluded game population differs")
    if set(primary_ranked) & set(primary_unscored):
        raise ValueError("ML primary game population overlaps")
    games: list[MlGameAuthority] = []
    all_pks = sorted(set(primary_ranked) | set(primary_unscored) | set(trace_rows), key=lambda pk: (
        schedule.get(pk, {}).get("scheduled_start_utc", ""), pk
    ))
    if set(all_pks) != set(schedule):
        raise ValueError("ML all-slate game population differs")
    for game_pk in all_pks:
        if game_pk not in schedule:
            raise ValueError("ML game is absent from LIVE_SCHEDULE")
        game = schedule[game_pk]
        row = trace_rows.get(game_pk)
        primary = primary_ranked.get(game_pk) or primary_unscored.get(game_pk) or {}
        reasons = primary.get("reason_codes", ()) if isinstance(primary, Mapping) else ()
        participant = _participant_evidence(
            schedule, game_pk, participant_bindings, snapshots, root, reasons
        )
        if row is None:
            if primary.get("state") not in {"UNSCORED", "EXCLUDED"}:
                raise ValueError("ML scored primary row is missing from trace")
            predicted_side = None
            probability = None
            completeness = None
            status: Literal["SCORED", "UNSCORED"] = "UNSCORED"
            rank = None
        else:
            side = row["predicted_side"]
            if side == "none":
                predicted_side = None
                probability = None
                status = "UNSCORED"
            elif side in {"away", "home"}:
                predicted_side = side
                probability = _fraction(row["pick_probability"], "pick probability")
                status = "SCORED"
            else:
                raise ValueError("ML predicted_side differs")
            qualities = row.get("qualities")
            if not isinstance(qualities, Mapping):
                raise ValueError("ML qualities differ")
            completeness_value = qualities.get("overall_completeness")
            if completeness_value is None:
                numeric = [value for value in qualities.values() if isinstance(value, (int, float)) and not isinstance(value, bool)]
                completeness_value = min(numeric) if numeric else None
            completeness = None if completeness_value is None else _fraction(completeness_value, "data completeness")
            rank = row.get("primary_rank")
            if rank is not None and (type(rank) is not int or rank <= 0):
                raise ValueError("ML primary rank differs")
            if primary and primary.get("rank") != rank:
                raise ValueError("ML primary/trace rank binding differs")
            if primary.get("state") == "UNSCORED" and status != "UNSCORED":
                raise ValueError("ML unscored primary row was scored")
            if not primary and row.get("disposition") != "SCORED_NOT_TOP3":
                raise ValueError("ML trace-only row disposition differs")
        games.append(
            MlGameAuthority(
                game_pk,
                str(game["scheduled_start_utc"]),
                str(game["away_team"]),
                str(game["home_team"]),
                predicted_side,
                probability,
                completeness,
                participant,
                status,
                rank,
            )
        )
    return tuple(games)


def load_ml_authority(root: Path, ml_trace: Path) -> MlAuthority:
    """Load and bind one legacy Top-3 report plus its all-slate score trace."""
    root = Path(root)
    trace_relative = _relative(root, ml_trace, "ML trace")
    snapshots: dict[Path, RegularFileSnapshot] = {}
    with open_trusted_root(root) as trusted:
        trace_snapshot = _snapshot_once(trusted, snapshots, trace_relative, "ML trace")
        _read_optional_sidecar(trusted, snapshots, trace_relative, trace_snapshot.contents, "ML trace")
        trace_payload = _json(trace_snapshot, "ML trace")
        # validate_trace_payload performs the trace's canonical hash and row math.
        trace_payload = validate_trace_payload(trace_payload)
        primary_relative = _relative(root, trace_payload["primary_report_relative"], "ML primary report")
        primary_snapshot = _snapshot_once(trusted, snapshots, primary_relative, "ML primary report")
        primary_payload = validate_run_payload(_json(primary_snapshot, "ML primary report"))
        if _canonical_digest(primary_payload) != trace_payload["primary_report_content_sha256"]:
            raise ValueError("ML primary report digest binding differs")
        if primary_payload["model_card_sha256"] != trace_payload["model_card_sha256"]:
            raise ValueError("ML model card binding differs")
        bindings = [dict(binding) for binding in primary_payload["source_bindings"]]
        for binding in bindings:
            if binding["binding_role"] in {"LIVE_SCHEDULE", "LIVE_PARTICIPANTS"}:
                _read_live_binding_files(trusted, snapshots, binding)
        schedule_bindings = [b for b in bindings if b["binding_role"] == "LIVE_SCHEDULE"]
        if len(schedule_bindings) != 1:
            raise ValueError("ML LIVE_SCHEDULE binding population differs")
        schedule_binding = schedule_bindings[0]
        schedule_snapshot = _snapshot_once(
            trusted, snapshots,
            _relative(root, schedule_binding["payload_relative"], "schedule payload"),
            "schedule payload", str(schedule_binding["payload_sha256"])
        )
        schedule_rows = parse_schedule(schedule_snapshot.contents)
        schedule = {int(row["game_pk"]): row for row in schedule_rows}
        games = _ml_games(
            primary_payload,
            trace_payload,
            schedule,
            [b for b in bindings if b["binding_role"] == "LIVE_PARTICIPANTS"],
            trusted,
            snapshots,
        )
        return MlAuthority(
            games,
            str(primary_payload["target_date"]),
            trace_relative,
            root,
            tuple(snapshots.values()),
        )


def _totals_components(
    root: TrustedRoot,
    snapshots: dict[Path, RegularFileSnapshot],
    trace_payload: Mapping[str, object],
    report_payload: Mapping[str, object],
) -> None:
    for payload, label in ((trace_payload, "totals trace"), (report_payload, "totals report")):
        for field in ("history_source", "live_source"):
            source = payload.get(field)
            if isinstance(source, Mapping):
                _source_files(root, snapshots, source, f"{label} {field}")


def load_totals_authority(root: Path, totals_trace: Path) -> TotalsAuthority:
    """Load a date-variable totals report and preserve its residual authority."""
    root = Path(root)
    trace_relative = _relative(root, totals_trace, "totals trace")
    snapshots: dict[Path, RegularFileSnapshot] = {}
    with open_trusted_root(root) as trusted:
        trace_snapshot = _snapshot_once(trusted, snapshots, trace_relative, "totals trace")
        _read_optional_sidecar(trusted, snapshots, trace_relative, trace_snapshot.contents, "totals trace")
        trace_payload = validate_totals_trace(_json(trace_snapshot, "totals trace"))
        report_relative = _relative(root, trace_payload["report_relative"], "totals report")
        report_snapshot = _snapshot_once(trusted, snapshots, report_relative, "totals report")
        report_payload = validate_totals_report(_json(report_snapshot, "totals report"))
        if _digest(report_snapshot.contents) != trace_payload["report_sha256"]:
            raise ValueError("totals report digest binding differs")
        if (
            report_payload["model_receipt_relative"] != trace_payload["model_receipt_relative"]
            or report_payload["model_receipt_sha256"] != trace_payload["model_receipt_sha256"]
            or report_payload["source_report_relative"] != trace_payload["source_report_relative"]
            or report_payload["source_report_sha256"] != trace_payload["source_report_sha256"]
        ):
            raise ValueError("totals report component binding differs")
        receipt_relative = _relative(root, trace_payload["model_receipt_relative"], "totals model receipt")
        receipt_snapshot = _snapshot_once(trusted, snapshots, receipt_relative, "totals model receipt", str(trace_payload["model_receipt_sha256"]))
        validate_totals_model_receipt(_json(receipt_snapshot, "totals model receipt"))
        receipt_sidecar_relative = receipt_relative.with_suffix(".sha256")
        receipt_sidecar = _snapshot_once(trusted, snapshots, receipt_sidecar_relative, "totals model receipt sidecar")
        if receipt_sidecar.contents != f"{_digest(receipt_snapshot.contents)}\n".encode("ascii"):
            raise ValueError("totals model receipt sidecar differs")
        source_relative = _relative(root, trace_payload["source_report_relative"], "totals source report")
        source_snapshot = _snapshot_once(trusted, snapshots, source_relative, "totals source report", str(trace_payload["source_report_sha256"]))
        validate_run_payload(_json(source_snapshot, "totals source report"))
        sidecar_relative = report_relative.with_suffix(".sha256")
        sidecar_snapshot = _snapshot_once(trusted, snapshots, sidecar_relative, "totals report sidecar")
        if (
            _digest(sidecar_snapshot.contents) != trace_payload["report_sidecar_sha256"]
            or sidecar_snapshot.contents != f"{_digest(report_snapshot.contents)}\n".encode("ascii")
        ):
            raise ValueError("totals report sidecar binding differs")
        markdown_relative = report_relative.with_suffix(".md")
        markdown_snapshot = _snapshot_once(trusted, snapshots, markdown_relative, "totals markdown")
        if _digest(markdown_snapshot.contents) != trace_payload["markdown_sha256"]:
            raise ValueError("totals markdown binding differs")
        _totals_components(trusted, snapshots, trace_payload, report_payload)
        games: list[TotalsGameAuthority] = []
        for raw in report_payload["games"]:
            samples = raw.get("residual_samples", []) if raw["status"] == "PROJECTED" else []
            if raw["status"] == "PROJECTED":
                if type(raw["residual_count"]) is not int or raw["residual_count"] != 764:
                    raise ValueError("totals residual authority count differs")
                if not isinstance(samples, list) or len(samples) != 764:
                    raise ValueError("totals residual authority differs")
                if any(type(value) is not int or value < 0 for value in samples):
                    raise ValueError("totals residual samples must be integer runs")
            games.append(
                TotalsGameAuthority(
                    int(raw["game_pk"]),
                    str(raw["scheduled_start_utc"]),
                    str(raw.get("away_team_id", "")),
                    str(raw.get("home_team_id", "")),
                    None if raw["status"] != "PROJECTED" else Fraction(str(raw["expected_total"])),
                    tuple(samples),
                    raw["status"],
                )
            )
        return TotalsAuthority(
            tuple(games),
            str(report_payload["target_date"]),
            trace_relative,
            root,
            tuple(snapshots.values()),
        )


def _rebind_snapshots(root: Path, snapshots: Sequence[RegularFileSnapshot]) -> None:
    with open_trusted_root(root) as trusted:
        for snapshot in snapshots:
            rebind_regular_snapshot(
                trusted,
                snapshot,
                f"authority component {snapshot.relative.as_posix()}",
                max_bytes=MAX_AUTHORITY_FILE_BYTES,
            )


def rebind_authorities(*authorities: EvidenceAuthority) -> None:
    """Re-read every component of each authority and fail on any mutation."""
    if not authorities:
        raise ValueError("at least one authority is required")
    for authority in authorities:
        rebind = getattr(authority, "rebind", None)
        if not callable(rebind):
            raise TypeError("authority does not expose a rebind operation")
        rebind()


__all__ = [
    "EvidenceAuthority",
    "MlAuthority",
    "MlGameAuthority",
    "ParticipantGateEvidence",
    "TotalsAuthority",
    "TotalsGameAuthority",
    "load_ml_authority",
    "load_totals_authority",
    "rebind_authorities",
]
