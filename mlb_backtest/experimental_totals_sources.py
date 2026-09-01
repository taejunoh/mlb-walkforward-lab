"""Read-only admission of the authenticated schedule inputs for daily totals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qsl, urlsplit

from .experimental_totals import validate_final_games, validate_target_games
from .live_sources import parse_live_schedule
from .live_top3_contracts import SourceBinding, canonical_json_bytes, validate_run_payload
from .secure_fs import (
    RegularFileSnapshot,
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
)
from .sources import OUT_OF_RANGE_UNPLAYED_STATUSES, parse_schedule


_MAX_JSON_BYTES = 64 * 1024 * 1024
_SHA256_BYTES = 1024
_HISTORY_ROLE = "HISTORY_SCHEDULE"
_LIVE_ROLE = "LIVE_SCHEDULE"
_MLB_LEAGUE_IDS = frozenset({103, 104})


@dataclass(frozen=True)
class AdmittedSourceIdentity:
    """Every immutable artifact that proves one admitted schedule payload."""

    binding: SourceBinding
    binding_sha256: str
    query_identity_sha256: str
    claim_relative: str
    claim_sha256: str
    receipt_relative: str
    receipt_sha256: str
    receipt_sidecar_relative: str | None
    receipt_sidecar_sha256: str | None
    completion_relative: str
    completion_sha256: str
    completion_sidecar_relative: str
    completion_sidecar_sha256: str
    manifest_relative: str
    manifest_sha256: str
    manifest_sidecar_relative: str
    manifest_sidecar_sha256: str
    payload_relative: str
    payload_sha256: str
    byte_length: int


@dataclass(frozen=True)
class TotalsSourceBundle:
    """Closed schedule rows and the complete chain by which they were admitted."""

    target_date: str
    cutoff_date: str
    history_identity: AdmittedSourceIdentity
    live_identity: AdmittedSourceIdentity
    history_games: tuple[object, ...]
    target_games: tuple[object, ...]


def _totals_sources_test_hook(stage: str, relative: Path) -> None:
    """No-op seam for evidence replacement tests."""


def _relative_below(root: TrustedRoot, path: Path, label: str) -> Path:
    supplied = Path(path)
    if supplied.is_absolute():
        raise ValueError(f"{label} is unsafe")
    if (
        not supplied.parts
        or any(part in {"", ".", ".."} or "\\" in part for part in supplied.parts)
    ):
        raise ValueError(f"{label} is unsafe")
    return supplied


def _strict_relative(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} differs")
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or path.as_posix() != value
        or any(part in {"", ".", ".."} or "\\" in part for part in path.parts)
    ):
        raise ValueError(f"{label} differs")
    return path


def _read(
    trusted: TrustedRoot,
    relative: Path,
    label: str,
    *,
    max_bytes: int = _MAX_JSON_BYTES,
    missing_ok: bool = False,
) -> RegularFileSnapshot | None:
    snapshot = read_regular_snapshot(
        trusted, relative, label, max_bytes=max_bytes, missing_ok=missing_ok
    )
    if snapshot is not None:
        _totals_sources_test_hook("after_snapshot", relative)
    return snapshot


def _snapshot_digest(snapshot: RegularFileSnapshot) -> str:
    return hashlib.sha256(snapshot.contents).hexdigest()


def _load_canonical_object(contents: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if not isinstance(value, dict) or canonical_json_bytes(value) != contents:
        raise ValueError(f"{label} differs")
    return value


def _selected_binding(report: Mapping[str, object], role: str) -> SourceBinding:
    raw_bindings = report.get("source_bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("report source bindings differ")
    selected: list[SourceBinding] = []
    for raw in raw_bindings:
        if not isinstance(raw, Mapping):
            raise ValueError("report source bindings differ")
        try:
            binding = SourceBinding(**dict(raw))
        except (TypeError, ValueError) as exc:
            raise ValueError("report source binding differs") from exc
        if binding.binding_role == role:
            selected.append(binding)
    if len(selected) != 1:
        raise ValueError(f"report requires exactly one {role} binding")
    return selected[0]


def _expected_paths(binding: SourceBinding) -> tuple[Path | None, Path, Path, Path]:
    role = binding.binding_role
    if role == _HISTORY_ROLE:
        receipt = Path("audit/experimental_daily/history_receipts") / f"{binding.observation_id}.json"
        claim = receipt.with_suffix(".claim.json")
        source = "mlb_schedule"
        folder = Path("data/experimental_daily/history_cache") / source
    elif role == _LIVE_ROLE:
        receipt = Path("audit/experimental_daily/live_receipts") / f"{binding.observation_id}.json"
        claim = None
        source = "mlb_live_schedule"
        folder = Path("data/experimental_daily/live_observations") / source
    else:  # pragma: no cover - all callers select a fixed role
        raise ValueError("schedule binding role differs")
    if binding.receipt_relative != receipt.as_posix():
        raise ValueError("schedule receipt path differs")
    payload = folder / f"{binding.payload_sha256}.raw"
    if binding.payload_relative != payload.as_posix():
        raise ValueError("schedule payload path differs")
    query_digest = hashlib.sha256(binding.query_identity_json.encode("utf-8")).hexdigest()
    manifest = folder / f"{binding.payload_sha256}.{query_digest}.manifest.json"
    return claim, receipt, payload, manifest


def _validate_schedule_request(
    binding: SourceBinding, query_identity: Mapping[str, object], *, target_date: str, cutoff_date: str
) -> None:
    if binding.source != "stats_api" or binding.requested_url != binding.final_url:
        raise ValueError("schedule request differs")
    parsed = urlsplit(binding.requested_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "statsapi.mlb.com"
        or parsed.path != "/api/v1/schedule"
    ):
        raise ValueError("schedule request differs")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _value in pairs}) != len(pairs):
        raise ValueError("schedule request differs")
    values = dict(pairs)
    if values.get("sportId") != "1" or values.get("gameType") != "R":
        raise ValueError("schedule request differs")
    if binding.binding_role == _HISTORY_ROLE:
        expected_start = f"{cutoff_date[:4]}-01-01"
        if (
            values.get("startDate") != expected_start
            or values.get("endDate") != cutoff_date
            or query_identity.get("as_of_date") != cutoff_date
            or query_identity.get("start") != expected_start
            or query_identity.get("end") != cutoff_date
            or query_identity.get("gameType") != "R"
            or query_identity.get("role") != "history"
        ):
            raise ValueError("history schedule request differs")
    elif (
        values.get("startDate") != target_date
        or values.get("endDate") != target_date
        or query_identity.get("target_date") != target_date
    ):
        raise ValueError("live schedule request differs")


def _require_sidecar(
    snapshot: RegularFileSnapshot | None, expected_digest: str, label: str
) -> RegularFileSnapshot:
    if snapshot is None or snapshot.contents != f"{expected_digest}\n".encode("ascii"):
        raise ValueError(f"{label} differs")
    return snapshot


def _require_absent(
    trusted: TrustedRoot, relative: Path, label: str
) -> None:
    if _read(
        trusted, relative, label, max_bytes=_SHA256_BYTES, missing_ok=True
    ) is not None:
        raise ValueError(f"{label} differs")


def _validate_schedule_population(raw: bytes, *, role: str, expected_date: str) -> None:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("schedule payload is invalid") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("dates"), list):
        raise ValueError("schedule payload is invalid")
    for date_entry in payload["dates"]:
        if not isinstance(date_entry, dict) or not isinstance(date_entry.get("games"), list):
            raise ValueError("schedule payload is invalid")
        for game in date_entry["games"]:
            if not isinstance(game, dict) or game.get("gameType") != "R":
                raise ValueError("schedule game type differs")
            game_pk = game.get("gamePk")
            if type(game_pk) is not int or game_pk <= 0:
                raise ValueError("schedule gamePk differs")
            sport = game.get("sport")
            if sport is not None and (
                not isinstance(sport, dict) or type(sport.get("id")) is not int
                or sport["id"] != 1
            ):
                raise ValueError("schedule sport differs")
            league = game.get("league")
            if league is not None and (
                not isinstance(league, dict) or type(league.get("id")) is not int
                or league["id"] not in _MLB_LEAGUE_IDS
            ):
                raise ValueError("schedule league differs")
            official_date = game.get("officialDate", date_entry.get("date"))
            if role == _LIVE_ROLE and official_date != expected_date:
                raise ValueError("live schedule date differs")


def _closed_history_rows(
    raw: bytes, cutoff_date: str, start_date: str
) -> tuple[object, ...]:
    _validate_schedule_population(raw, role=_HISTORY_ROLE, expected_date=cutoff_date)
    rows = []
    for game in parse_schedule(raw):
        if game["official_date"] < start_date:
            raise ValueError("history schedule date differs")
        if game["official_date"] > cutoff_date:
            if game["status"] in OUT_OF_RANGE_UNPLAYED_STATUSES:
                continue
            raise ValueError("history schedule date differs")
        if game["status"] not in {"Final", "Completed Early"}:
            raise ValueError("history schedule is not final")
        rows.append({
            "game_pk": game["game_pk"],
            "official_date": game["official_date"],
            "scheduled_start_utc": game["scheduled_start_utc"],
            "away_team_id": game["away_team_id"],
            "home_team_id": game["home_team_id"],
            "venue_id": game["venue_id"],
            "away_runs": game["away_score"],
            "home_runs": game["home_score"],
            "status": "Final",
        })
    return tuple(validate_final_games(rows, cutoff_date=cutoff_date))


def _closed_target_rows(raw: bytes, target_date: str) -> tuple[object, ...]:
    _validate_schedule_population(raw, role=_LIVE_ROLE, expected_date=target_date)
    rows = []
    for game in parse_live_schedule(raw, requested_date=target_date):
        if game["status"] not in {"Scheduled", "Pre-Game"}:
            raise ValueError("live schedule is not pregame")
        rows.append({
            "game_pk": game["game_pk"],
            "official_date": game["official_date"],
            "scheduled_start_utc": game["scheduled_start_utc"],
            "away_team_id": game["away_team_id"],
            "home_team_id": game["home_team_id"],
            "venue_id": game["venue_id"],
            "status": "Pregame",
        })
    return tuple(validate_target_games(rows, target_date=target_date))


def _admit_binding(
    trusted: TrustedRoot, binding: SourceBinding, *, target_date: str, cutoff_date: str
) -> tuple[AdmittedSourceIdentity, tuple[object, ...], tuple[RegularFileSnapshot, ...]]:
    try:
        query = json.loads(binding.query_identity_json)
    except json.JSONDecodeError as exc:
        raise ValueError("schedule query identity differs") from exc
    if (
        not isinstance(query, dict)
        or canonical_json_bytes(query).decode("utf-8").rstrip("\n")
        != binding.query_identity_json
    ):
        raise ValueError("schedule query identity differs")
    _validate_schedule_request(
        binding, query, target_date=target_date, cutoff_date=cutoff_date
    )
    claim_relative, receipt_relative, payload_relative, manifest_relative = _expected_paths(binding)
    receipt_sidecar_relative = receipt_relative.with_suffix(".sha256")
    completion_relative = receipt_relative.with_suffix(".completion.json")
    completion_sidecar_relative = receipt_relative.with_suffix(".completion.sha256")
    manifest_sidecar_relative = manifest_relative.with_name(manifest_relative.name[:-5] + ".sha256")

    receipt = _read(trusted, receipt_relative, "schedule receipt")
    if receipt is None:
        raise ValueError("schedule receipt chain is incomplete")
    receipt_value = _load_canonical_object(receipt.contents, "schedule receipt")
    if binding.binding_role == _LIVE_ROLE:
        claim_relative = _strict_relative(
            receipt_value.get("claim_relative"), "live schedule claim path"
        )
    assert claim_relative is not None
    claim = _read(trusted, claim_relative, "schedule receipt claim")
    receipt_sidecar = _read(
        trusted, receipt_sidecar_relative, "schedule receipt seal",
        max_bytes=_SHA256_BYTES, missing_ok=binding.binding_role == _LIVE_ROLE,
    )
    completion = _read(trusted, completion_relative, "schedule receipt completion")
    completion_sidecar = _read(trusted, completion_sidecar_relative, "schedule receipt completion seal", max_bytes=_SHA256_BYTES)
    manifest = _read(trusted, manifest_relative, "schedule manifest")
    manifest_sidecar = _read(
        trusted, manifest_sidecar_relative, "schedule manifest seal", max_bytes=_SHA256_BYTES
    )
    payload = _read(trusted, payload_relative, "schedule payload")
    required = (claim, receipt, completion, completion_sidecar, manifest, manifest_sidecar, payload)
    if binding.binding_role == _HISTORY_ROLE:
        required += (receipt_sidecar,)
    if any(item is None for item in required):
        raise ValueError("schedule receipt chain is incomplete")
    assert claim is not None and receipt is not None and completion is not None
    assert completion_sidecar is not None and manifest is not None and payload is not None
    if binding.binding_role == _LIVE_ROLE and receipt_sidecar is not None:
        raise ValueError("live schedule has an unexpected sidecar")
    if binding.binding_role == _LIVE_ROLE:
        _require_absent(
            trusted, claim_relative.with_suffix(".sha256"),
            "live schedule claim sidecar",
        )

    if len(payload.contents) != binding.byte_length or _snapshot_digest(payload) != binding.payload_sha256:
        prefix = "history" if binding.binding_role == _HISTORY_ROLE else "live"
        raise ValueError(f"{prefix} schedule payload differs")
    try:
        manifest_value = json.loads(manifest.contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("schedule manifest differs") from exc
    if (
        not isinstance(manifest_value, dict)
        or json.dumps(manifest_value, ensure_ascii=False, sort_keys=True, indent=2).encode()
        != manifest.contents
    ):
        raise ValueError("schedule manifest differs")
    raw_source = "mlb_schedule" if binding.binding_role == _HISTORY_ROLE else "mlb_live_schedule"
    row_count = sum(bool(line.strip()) for line in payload.contents.splitlines())
    expected_manifest = {
        "source": raw_source,
        "query": query,
        "sha256": binding.payload_sha256,
        "retrieved_at_utc": manifest_value.get("retrieved_at_utc"),
        "row_count": row_count,
        "schema_version": 1,
    }
    retrieved_at = manifest_value.get("retrieved_at_utc")
    if not isinstance(retrieved_at, str):
        raise ValueError("schedule manifest differs")
    try:
        parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
    except ValueError as exc:
        raise ValueError("schedule manifest differs") from exc
    if (
        parsed_retrieved_at.tzinfo is None
        or parsed_retrieved_at.utcoffset() != timedelta(0)
        or parsed_retrieved_at.isoformat() != retrieved_at
        or type(manifest_value.get("schema_version")) is not int
        or manifest_value.get("schema_version") != 1
        or type(manifest_value.get("row_count")) is not int
    ):
        raise ValueError("schedule manifest differs")
    if manifest_value != expected_manifest or _snapshot_digest(manifest) != binding.manifest_sha256:
        raise ValueError("schedule manifest differs")
    manifest_sidecar = _require_sidecar(
        manifest_sidecar, binding.manifest_sha256, "schedule manifest seal"
    )

    receipt_digest = _snapshot_digest(receipt)
    if receipt_digest != binding.receipt_sha256:
        raise ValueError("schedule receipt differs")
    common_receipt = {
        "source": binding.source,
        "binding_role": binding.binding_role,
        "game_pks": list(binding.game_pks),
        "observation_id": binding.observation_id,
        "query_identity_json": binding.query_identity_json,
        "requested_url": binding.requested_url,
        "final_url": binding.final_url,
        "response_completed_at_utc": binding.response_completed_at_utc,
        "http_status": binding.http_status,
        "content_type": binding.content_type,
        "etag": binding.etag,
        "last_modified": binding.last_modified,
        "payload_sha256": binding.payload_sha256,
        "byte_length": binding.byte_length,
        "payload_relative": binding.payload_relative,
        "manifest_sha256": binding.manifest_sha256,
    }
    if binding.binding_role == _HISTORY_ROLE:
        if receipt_value != common_receipt:
            raise ValueError("history schedule receipt differs")
        claim_value = _load_canonical_object(claim.contents, "history schedule claim")
        if claim_value != {"receipt": common_receipt, "receipt_sha256": receipt_digest}:
            raise ValueError("history schedule claim differs")
        assert receipt_sidecar is not None
        _require_sidecar(receipt_sidecar, receipt_digest, "history schedule receipt seal")
        completion_value = _load_canonical_object(completion.contents, "history schedule completion")
        if completion_value != {"receipt_sha256": receipt_digest, "durable_at_utc": binding.durable_at_utc}:
            raise ValueError("history schedule completion differs")
    else:
        claim_value = _load_canonical_object(claim.contents, "live schedule claim")
        claim_fields = dict(common_receipt)
        for field in ("game_pks", "payload_relative", "manifest_sha256"):
            claim_fields.pop(field)
        expected_claim = {
            "schema_version": 1, "raw_source": raw_source, **claim_fields,
            "started_at_utc": claim_value.get("started_at_utc"),
        }
        if claim_value != expected_claim:
            raise ValueError("live schedule claim differs")
        live_receipt = dict(common_receipt)
        live_receipt.pop("game_pks")
        expected_receipt = {
            "schema_version": 1, **live_receipt,
            "manifest_relative": manifest_relative.as_posix(),
            "claim_relative": claim_relative.as_posix(),
            "claim_sha256": _snapshot_digest(claim),
            "started_at_utc": claim_value["started_at_utc"],
        }
        if receipt_value != expected_receipt:
            raise ValueError("live schedule receipt differs")
        completion_value = _load_canonical_object(completion.contents, "live schedule completion")
        expected_completion = {
            "schema_version": 1, "source": binding.source,
            "binding_role": binding.binding_role, "observation_id": binding.observation_id,
            "query_identity_json": binding.query_identity_json,
            "receipt_relative": binding.receipt_relative, "receipt_sha256": receipt_digest,
            "payload_sha256": binding.payload_sha256, "manifest_sha256": binding.manifest_sha256,
            "durable_at_utc": binding.durable_at_utc,
        }
        if completion_value != expected_completion:
            raise ValueError("live schedule completion differs")
    _require_sidecar(completion_sidecar, _snapshot_digest(completion), "schedule completion seal")

    rows = (
        _closed_history_rows(
            payload.contents, cutoff_date, f"{cutoff_date[:4]}-01-01"
        )
        if binding.binding_role == _HISTORY_ROLE
        else _closed_target_rows(payload.contents, target_date)
    )
    if binding.binding_role == _HISTORY_ROLE:
        if binding.game_pks:
            raise ValueError("history schedule binding game_pks differs")
    elif tuple(sorted(game.game_pk for game in rows)) != binding.game_pks:
        raise ValueError("live schedule binding game_pks differs")
    snapshots = (
        claim, receipt, completion, completion_sidecar, manifest, manifest_sidecar,
        payload,
    )
    if receipt_sidecar is not None:
        snapshots += (receipt_sidecar,)
    _totals_sources_test_hook("before_rebind", payload_relative)
    for snapshot in snapshots:
        rebind_regular_snapshot(trusted, snapshot, "schedule source evidence", max_bytes=_MAX_JSON_BYTES)
    identity = AdmittedSourceIdentity(
        binding=binding, binding_sha256=binding.binding_sha256,
        query_identity_sha256=hashlib.sha256(binding.query_identity_json.encode()).hexdigest(),
        claim_relative=claim_relative.as_posix(), claim_sha256=_snapshot_digest(claim),
        receipt_relative=receipt_relative.as_posix(), receipt_sha256=receipt_digest,
        receipt_sidecar_relative=receipt_sidecar_relative.as_posix() if receipt_sidecar is not None else None,
        receipt_sidecar_sha256=_snapshot_digest(receipt_sidecar) if receipt_sidecar else None,
        completion_relative=completion_relative.as_posix(), completion_sha256=_snapshot_digest(completion),
        completion_sidecar_relative=completion_sidecar_relative.as_posix(), completion_sidecar_sha256=_snapshot_digest(completion_sidecar),
        manifest_relative=manifest_relative.as_posix(), manifest_sha256=_snapshot_digest(manifest),
        manifest_sidecar_relative=manifest_sidecar_relative.as_posix(),
        manifest_sidecar_sha256=_snapshot_digest(manifest_sidecar),
        payload_relative=payload_relative.as_posix(), payload_sha256=_snapshot_digest(payload), byte_length=len(payload.contents),
    )
    return identity, rows, snapshots


def load_totals_sources(root: Path, report_path: Path) -> TotalsSourceBundle:
    """Admit exactly one report's D-1 and live MLB schedule evidence, read-only."""
    with open_trusted_root(Path(root)) as trusted:
        report_relative = _relative_below(trusted, report_path, "report path")
        report_snapshot = _read(trusted, report_relative, "daily report")
        if report_snapshot is None:
            raise ValueError("daily report differs")
        report = _load_canonical_object(report_snapshot.contents, "daily report")
        validated = validate_run_payload(report)
        history_binding = _selected_binding(validated, _HISTORY_ROLE)
        live_binding = _selected_binding(validated, _LIVE_ROLE)
        history_identity, history_games, history_snapshots = _admit_binding(
            trusted, history_binding,
            target_date=str(validated["target_date"]), cutoff_date=str(validated["history_cutoff_date"]),
        )
        live_identity, target_games, live_snapshots = _admit_binding(
            trusted, live_binding,
            target_date=str(validated["target_date"]), cutoff_date=str(validated["history_cutoff_date"]),
        )
        _totals_sources_test_hook("before_final_rebind", report_relative)
        for snapshot in history_snapshots + live_snapshots:
            rebind_regular_snapshot(
                trusted, snapshot, "schedule source evidence", max_bytes=_MAX_JSON_BYTES
            )
        rebind_regular_snapshot(trusted, report_snapshot, "daily report", max_bytes=_MAX_JSON_BYTES)
        trusted.assert_bound()
        _require_absent(
            trusted,
            Path(live_identity.receipt_relative).with_suffix(".sha256"),
            "live schedule receipt sidecar",
        )
        _require_absent(
            trusted,
            Path(live_identity.claim_relative).with_suffix(".sha256"),
            "live schedule claim sidecar",
        )
        trusted.assert_bound()
        return TotalsSourceBundle(
            target_date=str(validated["target_date"]), cutoff_date=str(validated["history_cutoff_date"]),
            history_identity=history_identity, live_identity=live_identity,
            history_games=history_games, target_games=target_games,
        )
