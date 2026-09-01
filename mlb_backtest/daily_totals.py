"""Seal and publish the research-only provisional MLB daily-totals report."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from numbers import Real
from pathlib import Path, PurePosixPath
import sys

from .experimental_totals import (
    CANDIDATE_IDS,
    EvaluationMetrics,
    TotalProjection,
    UnprojectedTotal,
    evaluate_predictions,
    interval_80,
    project_game,
    select_candidate,
    walk_forward_predictions,
)
from .experimental_totals_sources import (
    AdmittedSourceIdentity,
    load_totals_sources,
    load_totals_sources_bound,
)
from .live_top3_contracts import canonical_json_bytes, validate_target_date
from .secure_fs import (
    TrustedRoot,
    open_trusted_root,
    read_regular_snapshot,
    rebind_regular_snapshot,
    write_exclusive,
)


MODEL_SCHEMA = "PROVISIONAL_DAILY_TOTALS_MODEL_V1"
REPORT_SCHEMA = "PROVISIONAL_DAILY_TOTALS_V1"
STATUS_LABELS = ["RESEARCH ONLY", "ONE-SEASON PROVISIONAL", "NOT BET-VALIDATED"]
_MODEL_ROOT = Path("audit/experimental_daily_totals/model")
_REPORT_ROOT = Path("reports/experimental_daily_totals")
_WINDOWS = {
    "warmup_start": "2026-03-25",
    "warmup_end": "2026-05-31",
    "development_start": "2026-06-01",
    "development_end": "2026-07-31",
    "final_start": "2026-08-01",
    "final_end": "2026-08-31",
    "league_days": 30,
    "venue_days": 60,
    "team_shrinkage_games": 10,
    "venue_shrinkage_games": 20,
}
_SOURCE_KEYS = frozenset(
    {
        "binding_role", "binding_sha256", "query_identity_sha256", "claim_relative",
        "claim_sha256", "receipt_relative", "receipt_sha256",
        "receipt_sidecar_relative", "receipt_sidecar_sha256", "completion_relative",
        "completion_sha256", "completion_sidecar_relative", "completion_sidecar_sha256",
        "manifest_relative", "manifest_sha256", "manifest_sidecar_relative",
        "manifest_sidecar_sha256", "payload_relative", "payload_sha256", "byte_length",
    }
)
_METRIC_KEYS = frozenset({"games", "mae", "rmse", "poisson_deviance", "interval_80_coverage"})
_MODEL_KEYS = frozenset(
    {
        "schema", "content_sha256", "status_labels", "source_report_relative",
        "source_report_sha256", "target_date", "cutoff_date", "history_source",
        "live_source", "windows", "candidate_ids", "candidate_definition_sha256",
        "development_metrics", "selected_candidate", "final_metrics",
        "development_residuals", "development_residual_sha256",
        "development_residual_count",
    }
)
_REPORT_KEYS = frozenset(
    {
        "schema", "content_sha256", "status_labels", "target_date", "cutoff_date",
        "model_receipt_relative", "model_receipt_sha256", "source_report_relative",
        "source_report_sha256", "history_source", "live_source", "selected_candidate",
        "development_metrics", "final_metrics", "games",
    }
)
_PROJECTED_GAME_KEYS = frozenset(
    {
        "game_pk", "official_date", "scheduled_start_utc", "away_team_id", "home_team_id",
        "venue_id", "status", "candidate_id", "away_expected_runs", "home_expected_runs",
        "expected_total", "interval_80", "residual_samples", "residual_sha256", "residual_count", "feature_cutoff", "source_digest", "data_quality_flags",
    }
)
_UNPROJECTED_GAME_KEYS = frozenset(
    {"game_pk", "official_date", "scheduled_start_utc", "away_team_id", "home_team_id", "venue_id", "status", "reason", "feature_cutoff", "source_digest", "data_quality_flags"}
)
_TRACE_KEYS = frozenset({
    "schema", "content_sha256", "model_receipt_relative", "model_receipt_sha256",
    "report_relative", "report_sha256", "report_sidecar_sha256", "markdown_sha256",
    "source_report_relative", "source_report_sha256", "history_source", "live_source",
})


@dataclass(frozen=True, slots=True)
class BuiltDailyTotals:
    """One inseparable receipt/report/trace-input bundle ready for publication."""

    model_receipt_bytes: bytes
    report_bytes: bytes
    trace_inputs_bytes: bytes

    @property
    def model_receipt(self) -> dict[str, object]:
        return validate_totals_model_receipt(json.loads(self.model_receipt_bytes))

    @property
    def report(self) -> dict[str, object]:
        return validate_totals_report(json.loads(self.report_bytes))

    @property
    def trace_inputs(self) -> dict[str, object]:
        return _canonical_mapping(json.loads(self.trace_inputs_bytes), "daily totals trace inputs")


def _sha_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _sha_json(value: object) -> str:
    return _sha_bytes(canonical_json_bytes(value))


def _canonical_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    try:
        copied = json.loads(canonical_json_bytes(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is not finite canonical JSON") from exc
    if not isinstance(copied, dict):
        raise ValueError(f"{label} must be an object")
    return copied


def _sha(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} differs")
    return value


def _relative(value: object, label: str, *, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} differs")
    path = PurePosixPath(value)
    if path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} differs")
    if suffix is not None and not value.endswith(suffix):
        raise ValueError(f"{label} differs")
    return value


def _finite(value: object, label: str, *, low: float = 0.0, high: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{label} is out of range")
    return result


def _date(value: object, label: str) -> str:
    try:
        result = validate_target_date(value)
    except ValueError as exc:
        raise ValueError(f"{label} differs") from exc
    return result


def _identity(value: object, role: str) -> dict[str, object]:
    safe = _canonical_mapping(value, f"{role} source")
    if set(safe) != _SOURCE_KEYS or safe["binding_role"] != role:
        raise ValueError(f"{role} source fields differ")
    for field in _SOURCE_KEYS:
        if field.endswith("_sha256"):
            if safe[field] is not None:
                _sha(safe[field], f"{role} source {field}")
        elif field.endswith("_relative"):
            if safe[field] is not None:
                _relative(safe[field], f"{role} source {field}")
    if type(safe["byte_length"]) is not int or safe["byte_length"] < 0:
        raise ValueError(f"{role} source byte_length differs")
    if role == "HISTORY_SCHEDULE":
        if safe["receipt_sidecar_relative"] is None or safe["receipt_sidecar_sha256"] is None:
            raise ValueError("history source receipt sidecar differs")
    elif safe["receipt_sidecar_relative"] is not None or safe["receipt_sidecar_sha256"] is not None:
        raise ValueError("live source receipt sidecar differs")
    return safe


def _metrics(value: object, label: str, *, minimum: int | None = None) -> dict[str, object]:
    safe = _canonical_mapping(value, label)
    if set(safe) != _METRIC_KEYS or type(safe["games"]) is not int or safe["games"] <= 0:
        raise ValueError(f"{label} fields differ")
    if minimum is not None and safe["games"] < minimum:
        raise ValueError(f"{label} requires at least {minimum} games")
    _finite(safe["mae"], f"{label}.mae")
    _finite(safe["rmse"], f"{label}.rmse")
    _finite(safe["poisson_deviance"], f"{label}.poisson_deviance")
    _finite(safe["interval_80_coverage"], f"{label}.interval_80_coverage", high=1.0)
    return safe


def _content_digest(safe: Mapping[str, object]) -> str:
    without = dict(safe)
    without.pop("content_sha256", None)
    return _sha_json(without)


def _residuals(value: object, digest: object, count: object, label: str) -> list[int]:
    if not isinstance(value, list) or any(type(item) is not int or item < 0 for item in value):
        raise ValueError(f"{label} samples differ")
    if type(count) is not int or count != len(value) or not value:
        raise ValueError(f"{label} count differs")
    if _sha(digest, f"{label} sha256") != _sha_json(value):
        raise ValueError(f"{label} digest differs")
    return value


def _signed_residuals(value: object, digest: object, count: object, label: str) -> list[float]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} residuals differ")
    residuals = [_finite(item, f"{label} residual", low=-math.inf) for item in value]
    if type(count) is not int or count != len(residuals):
        raise ValueError(f"{label} count differs")
    if _sha(digest, f"{label} sha256") != _sha_json(value):
        raise ValueError(f"{label} digest differs")
    return residuals


def _discrete_samples(residuals: Sequence[float], mean: float) -> tuple[int, ...]:
    return tuple(sorted(max(0, math.floor(mean + residual + 0.5)) for residual in residuals))


def validate_totals_model_receipt(value: object) -> dict[str, object]:
    """Validate the closed selected-model receipt, including semantic bindings."""
    safe = _canonical_mapping(value, "model receipt")
    if set(safe) != _MODEL_KEYS or safe["schema"] != MODEL_SCHEMA:
        raise ValueError("model receipt fields differ")
    if safe["status_labels"] != STATUS_LABELS:
        raise ValueError("model receipt status labels differ")
    _relative(safe["source_report_relative"], "model receipt source report", suffix=".json")
    _sha(safe["source_report_sha256"], "model receipt source report sha256")
    target = _date(safe["target_date"], "model receipt target date")
    cutoff = _date(safe["cutoff_date"], "model receipt cutoff date")
    if cutoff != (date.fromisoformat(target) - timedelta(days=1)).isoformat():
        raise ValueError("model receipt cutoff date differs")
    _identity(safe["history_source"], "HISTORY_SCHEDULE")
    _identity(safe["live_source"], "LIVE_SCHEDULE")
    if safe["windows"] != _WINDOWS:
        raise ValueError("model receipt windows differ")
    if safe["candidate_ids"] != list(CANDIDATE_IDS):
        raise ValueError("model receipt candidates differ")
    candidate_definition = {"candidate_ids": list(CANDIDATE_IDS), "windows": _WINDOWS}
    if _sha(safe["candidate_definition_sha256"], "model receipt candidate definition") != _sha_json(candidate_definition):
        raise ValueError("model receipt candidate definition differs")
    development = safe["development_metrics"]
    if not isinstance(development, Mapping) or set(development) != set(CANDIDATE_IDS):
        raise ValueError("model receipt development metrics differ")
    for candidate in CANDIDATE_IDS:
        _metrics(development[candidate], f"model receipt development {candidate}", minimum=100)
    if safe["selected_candidate"] not in CANDIDATE_IDS:
        raise ValueError("model receipt selected candidate differs")
    _metrics(safe["final_metrics"], "model receipt final metrics", minimum=100)
    _signed_residuals(safe["development_residuals"], safe["development_residual_sha256"], safe["development_residual_count"], "model receipt development")
    if _sha(safe["content_sha256"], "model receipt content") != _content_digest(safe):
        raise ValueError("model receipt content digest differs")
    return safe


def _game(value: object, target_date: str, cutoff_date: str, source_digest: str, candidate: str) -> dict[str, object]:
    safe = _canonical_mapping(value, "report game")
    status = safe.get("status")
    allowed = _PROJECTED_GAME_KEYS if status == "PROJECTED" else _UNPROJECTED_GAME_KEYS if status == "UNPROJECTED" else frozenset()
    if set(safe) != allowed:
        raise ValueError("report game fields differ")
    for field in ("game_pk", "away_team_id", "home_team_id", "venue_id"):
        if type(safe[field]) is not int or safe[field] <= 0:
            raise ValueError(f"report game {field} differs")
    if safe["away_team_id"] == safe["home_team_id"] or _date(safe["official_date"], "report game date") != target_date:
        raise ValueError("report game identity differs")
    if not isinstance(safe["scheduled_start_utc"], str) or not safe["scheduled_start_utc"].endswith("Z"):
        raise ValueError("report game start differs")
    if safe["feature_cutoff"] != cutoff_date or _sha(safe["source_digest"], "report game source digest") != source_digest:
        raise ValueError("report game source binding differs")
    flags = safe["data_quality_flags"]
    if not isinstance(flags, list) or any(flag not in {"ADMITTED_SCHEDULE", "MODEL_ELIGIBLE", "UNKNOWN_TEAM", "UNKNOWN_VENUE"} for flag in flags):
        raise ValueError("report game data quality flags differ")
    if status == "UNPROJECTED":
        if safe["reason"] not in {"UNKNOWN_TEAM", "UNKNOWN_VENUE"}:
            raise ValueError("report game unprojected reason differs")
        if flags != ["ADMITTED_SCHEDULE", safe["reason"]]:
            raise ValueError("report game data quality flags differ")
        return safe
    if flags != ["ADMITTED_SCHEDULE", "MODEL_ELIGIBLE"]:
        raise ValueError("report game data quality flags differ")
    if safe["candidate_id"] != candidate:
        raise ValueError("report game candidate differs")
    away = _finite(safe["away_expected_runs"], "report game away expected", low=0.000000001)
    home = _finite(safe["home_expected_runs"], "report game home expected", low=0.000000001)
    total = _finite(safe["expected_total"], "report game total expected", low=0.000000001)
    if not math.isclose(total, away + home, abs_tol=1e-12, rel_tol=0.0):
        raise ValueError("report game total differs")
    samples = _residuals(safe["residual_samples"], safe["residual_sha256"], safe["residual_count"], "report game residual")
    interval = safe["interval_80"]
    if not isinstance(interval, list) or len(interval) != 2 or any(type(item) is not int or item < 0 for item in interval) or interval[0] > interval[1] or interval != list(interval_80(tuple(samples))):
        raise ValueError("report game interval differs")
    return safe


def validate_totals_report(value: object) -> dict[str, object]:
    """Validate the closed primary report and all report-visible cross bindings."""
    safe = _canonical_mapping(value, "totals report")
    if set(safe) != _REPORT_KEYS or safe["schema"] != REPORT_SCHEMA:
        raise ValueError("totals report fields differ")
    if safe["status_labels"] != STATUS_LABELS:
        raise ValueError("totals report status labels differ")
    target = _date(safe["target_date"], "totals report target date")
    cutoff = _date(safe["cutoff_date"], "totals report cutoff date")
    if cutoff != (date.fromisoformat(target) - timedelta(days=1)).isoformat():
        raise ValueError("totals report cutoff date differs")
    _relative(safe["model_receipt_relative"], "totals report model receipt", suffix=".json")
    _sha(safe["model_receipt_sha256"], "totals report model receipt sha256")
    _relative(safe["source_report_relative"], "totals report source report", suffix=".json")
    _sha(safe["source_report_sha256"], "totals report source report sha256")
    _identity(safe["history_source"], "HISTORY_SCHEDULE")
    _identity(safe["live_source"], "LIVE_SCHEDULE")
    candidate = safe["selected_candidate"]
    if candidate not in CANDIDATE_IDS:
        raise ValueError("totals report selected candidate differs")
    development = safe["development_metrics"]
    if not isinstance(development, Mapping) or set(development) != set(CANDIDATE_IDS):
        raise ValueError("totals report development metrics differ")
    for candidate_id in CANDIDATE_IDS:
        _metrics(development[candidate_id], f"totals report development {candidate_id}", minimum=100)
    _metrics(safe["final_metrics"], "totals report final metrics", minimum=100)
    if not isinstance(safe["games"], list) or not safe["games"]:
        raise ValueError("totals report games differ")
    history_source = _identity(safe["history_source"], "HISTORY_SCHEDULE")
    games = [_game(game, target, cutoff, history_source["payload_sha256"], candidate) for game in safe["games"]]
    keys = [(game["official_date"], game["scheduled_start_utc"], game["game_pk"]) for game in games]
    if keys != sorted(keys) or len({game["game_pk"] for game in games}) != len(games):
        raise ValueError("totals report game ordering differs")
    if _sha(safe["content_sha256"], "totals report content") != _content_digest(safe):
        raise ValueError("totals report content digest differs")
    return safe


def _source_identity(identity: AdmittedSourceIdentity) -> dict[str, object]:
    raw = asdict(identity)
    raw["binding_role"] = identity.binding.binding_role
    raw.pop("binding", None)
    return raw


def _metrics_payload(metrics: EvaluationMetrics, coverage: float) -> dict[str, object]:
    return {"games": metrics.games, "mae": metrics.mae, "rmse": metrics.rmse, "poisson_deviance": metrics.poisson_deviance, "interval_80_coverage": coverage}


def _per_game_coverage(rows: tuple[tuple[object, TotalProjection], ...], residuals: tuple[float, ...]) -> float:
    """Score each held-out game against samples shifted by that game's own mean."""
    outcomes = []
    for actual, projection in rows:
        lower, upper = interval_80(_discrete_samples(residuals, projection.expected_total))
        observed = actual.away_runs + actual.home_runs
        outcomes.append(int(lower <= observed <= upper))
    return sum(outcomes) / len(outcomes)


def _safe_source_relative(root: Path, report: Path) -> Path:
    if report.is_absolute() or not report.parts or any(part in {"", ".", ".."} or "\\" in part for part in report.parts):
        raise ValueError("source report must be a safe relative path")
    return report


def build_daily_totals(root: Path, target_date: str, source_report: Path) -> BuiltDailyTotals:
    """Build one sealed, but not yet published, provisional totals artifact."""
    target = _date(target_date, "target date")
    source_relative = _safe_source_relative(Path(root), Path(source_report))
    with open_trusted_root(Path(root)) as trusted:
        source_snapshot = read_regular_snapshot(trusted, source_relative, "daily totals source report")
        if source_snapshot is None:
            raise ValueError("daily totals source report differs")
        bundle = load_totals_sources(Path(root), source_relative)
        if bundle.target_date != target or bundle.cutoff_date != (date.fromisoformat(target) - timedelta(days=1)).isoformat():
            raise ValueError("admitted source dates differ")
        if target != "2026-09-01" or bundle.cutoff_date != "2026-08-31":
            raise ValueError("provisional totals fixed windows require 2026-09-01")
        source_sha = _sha_bytes(source_snapshot.contents)
        history = bundle.history_games
        development = walk_forward_predictions(history, CANDIDATE_IDS, _WINDOWS["development_start"], _WINDOWS["development_end"])
        selected = select_candidate(development)
        selected_development = development[selected]
        development_residuals = tuple(
            actual.away_runs + actual.home_runs - projection.expected_total
            for actual, projection in selected_development
        )
        development_metrics = {
            candidate: _metrics_payload(
                evaluate_predictions(rows),
                _per_game_coverage(rows, tuple(actual.away_runs + actual.home_runs - projection.expected_total for actual, projection in rows)),
            )
            for candidate, rows in development.items()
        }
        final_by_candidate = walk_forward_predictions(history, (selected,), _WINDOWS["final_start"], _WINDOWS["final_end"])
        final_rows = final_by_candidate[selected]
        if len(final_rows) < 100:
            raise ValueError("final evaluation requires at least 100 rows")
        final_metrics = _metrics_payload(evaluate_predictions(final_rows), _per_game_coverage(final_rows, development_residuals))
        history_identity = _source_identity(bundle.history_identity)
        live_identity = _source_identity(bundle.live_identity)
        candidate_definition_sha = _sha_json({"candidate_ids": list(CANDIDATE_IDS), "windows": _WINDOWS})
        receipt: dict[str, object] = {
            "schema": MODEL_SCHEMA, "content_sha256": "", "status_labels": STATUS_LABELS,
            "source_report_relative": source_relative.as_posix(), "source_report_sha256": source_sha,
            "target_date": target, "cutoff_date": bundle.cutoff_date,
            "history_source": history_identity, "live_source": live_identity, "windows": _WINDOWS,
            "candidate_ids": list(CANDIDATE_IDS), "candidate_definition_sha256": candidate_definition_sha,
            "development_metrics": development_metrics, "selected_candidate": selected,
            "final_metrics": final_metrics, "development_residuals": list(development_residuals),
            "development_residual_sha256": _sha_json(list(development_residuals)), "development_residual_count": len(development_residuals),
        }
        receipt["content_sha256"] = _content_digest(receipt)
        receipt = validate_totals_model_receipt(receipt)
        receipt_bytes = canonical_json_bytes(receipt)
        receipt_relative = _MODEL_ROOT / f"{receipt['content_sha256']}.json"
        game_rows: list[dict[str, object]] = []
        for game in bundle.target_games:
            result = project_game(history, game, selected)
            base = {"game_pk": game.game_pk, "official_date": game.official_date, "scheduled_start_utc": game.scheduled_start_utc, "away_team_id": game.away_team_id, "home_team_id": game.home_team_id, "venue_id": game.venue_id}
            if isinstance(result, UnprojectedTotal):
                game_rows.append({**base, "status": result.status, "reason": result.reason, "feature_cutoff": bundle.cutoff_date, "source_digest": bundle.history_identity.payload_sha256, "data_quality_flags": ["ADMITTED_SCHEDULE", result.reason]})
                continue
            if not isinstance(result, TotalProjection):
                raise ValueError("projection result differs")
            samples = _discrete_samples(development_residuals, result.expected_total)
            game_rows.append({**base, "status": "PROJECTED", "candidate_id": selected, "away_expected_runs": result.away_expected_runs, "home_expected_runs": result.home_expected_runs, "expected_total": result.expected_total, "interval_80": list(interval_80(samples)), "residual_samples": list(samples), "residual_sha256": _sha_json(list(samples)), "residual_count": len(samples), "feature_cutoff": bundle.cutoff_date, "source_digest": bundle.history_identity.payload_sha256, "data_quality_flags": ["ADMITTED_SCHEDULE", "MODEL_ELIGIBLE"]})
        report: dict[str, object] = {
            "schema": REPORT_SCHEMA, "content_sha256": "", "status_labels": STATUS_LABELS,
            "target_date": target, "cutoff_date": bundle.cutoff_date,
            "model_receipt_relative": receipt_relative.as_posix(), "model_receipt_sha256": _sha_bytes(receipt_bytes),
            "source_report_relative": source_relative.as_posix(), "source_report_sha256": source_sha,
            "history_source": history_identity, "live_source": live_identity, "selected_candidate": selected,
            "development_metrics": development_metrics, "final_metrics": final_metrics,
            "games": sorted(game_rows, key=lambda item: (item["official_date"], item["scheduled_start_utc"], item["game_pk"])),
        }
        report["content_sha256"] = _content_digest(report)
        report = validate_totals_report(report)
        rebind_regular_snapshot(trusted, source_snapshot, "daily totals source report")
    trace_inputs = {"source_report_relative": source_relative.as_posix(), "source_report_sha256": source_sha, "model_receipt_relative": receipt_relative.as_posix()}
    return BuiltDailyTotals(canonical_json_bytes(receipt), canonical_json_bytes(report), canonical_json_bytes(trace_inputs))


def render_totals_markdown(payload: object) -> str:
    """Render a compact research-only representation from a validated report."""
    safe = validate_totals_report(payload)
    lines = ["# Provisional Daily MLB Totals", "", "> **RESEARCH ONLY** · **ONE-SEASON PROVISIONAL** · **NOT BET-VALIDATED**", "", f"- Target date: `{safe['target_date']}`", f"- History cutoff: `{safe['cutoff_date']}`", f"- Selected candidate: `{safe['selected_candidate']}`", f"- Report content SHA-256: `{safe['content_sha256']}`", "", "| Game | Expected total | 80% interval | Status |", "| --- | ---: | --- | --- |"]
    for game in safe["games"]:
        if game["status"] == "PROJECTED":
            lines.append(f"| {game['game_pk']} | {game['expected_total']:.3f} | {game['interval_80'][0]}–{game['interval_80'][1]} | PROJECTED |")
        else:
            lines.append(f"| {game['game_pk']} | — | — | UNPROJECTED ({game['reason']}) |")
    lines.extend(["", "This report contains model research only and no market comparison or wagering recommendation.", ""])
    return "\n".join(lines)


def _existing_or_write(trusted: TrustedRoot, relative: Path, contents: bytes, label: str):
    existing = read_regular_snapshot(trusted, relative, label, missing_ok=True)
    if existing is not None:
        if existing.contents != contents:
            raise FileExistsError(f"{label} identity conflicts")
        return existing
    return write_exclusive(trusted, relative, contents, label)


def _sidecar(contents: bytes) -> bytes:
    return f"{_sha_bytes(contents)}\n".encode("ascii")


def validate_totals_trace(value: object) -> dict[str, object]:
    """Validate the append-only trace commit point and complete source identities."""
    safe = _canonical_mapping(value, "daily totals trace")
    if set(safe) != _TRACE_KEYS or safe["schema"] != "PROVISIONAL_DAILY_TOTALS_TRACE_V1":
        raise ValueError("daily totals trace fields differ")
    for field in ("model_receipt_relative", "report_relative", "source_report_relative"):
        _relative(safe[field], f"daily totals trace {field}", suffix=".json")
    for field in ("content_sha256", "model_receipt_sha256", "report_sha256", "report_sidecar_sha256", "markdown_sha256", "source_report_sha256"):
        _sha(safe[field], f"daily totals trace {field}")
    _identity(safe["history_source"], "HISTORY_SCHEDULE")
    _identity(safe["live_source"], "LIVE_SCHEDULE")
    if safe["content_sha256"] != _content_digest(safe):
        raise ValueError("daily totals trace content digest differs")
    return safe


def _trace_payload(report_relative: Path, model_relative: Path, model_contents: bytes, report_contents: bytes, report_sidecar: bytes, markdown_contents: bytes, source_relative: str, source_sha: str, history_source: object, live_source: object) -> dict[str, object]:
    payload: dict[str, object] = {"schema": "PROVISIONAL_DAILY_TOTALS_TRACE_V1", "content_sha256": "", "model_receipt_relative": model_relative.as_posix(), "model_receipt_sha256": _sha_bytes(model_contents), "report_relative": report_relative.as_posix(), "report_sha256": _sha_bytes(report_contents), "report_sidecar_sha256": _sha_bytes(report_sidecar), "markdown_sha256": _sha_bytes(markdown_contents), "source_report_relative": source_relative, "source_report_sha256": source_sha, "history_source": history_source, "live_source": live_source}
    payload["content_sha256"] = _content_digest(payload)
    return validate_totals_trace(payload)


def persist_daily_totals(root: Path, built: BuiltDailyTotals) -> tuple[Path, Path, Path, Path]:
    """Append receipt sidecar/receipt/report sidecar/report/Markdown/trace in order."""
    if not isinstance(built, BuiltDailyTotals):
        raise ValueError("built daily totals differs")
    receipt = validate_totals_model_receipt(built.model_receipt)
    report = validate_totals_report(built.report)
    receipt_contents = canonical_json_bytes(receipt)
    receipt_relative = _MODEL_ROOT / f"{receipt['content_sha256']}.json"
    if report["model_receipt_relative"] != receipt_relative.as_posix() or report["model_receipt_sha256"] != _sha_bytes(receipt_contents):
        raise ValueError("report model receipt binding differs")
    for field in (
        "status_labels", "target_date", "cutoff_date", "source_report_relative",
        "source_report_sha256", "history_source", "live_source", "selected_candidate",
        "development_metrics", "final_metrics",
    ):
        if report[field] != receipt[field]:
            raise ValueError("report receipt source binding differs")
    expected_trace_inputs = {
        "source_report_relative": report["source_report_relative"],
        "source_report_sha256": report["source_report_sha256"],
        "model_receipt_relative": receipt_relative.as_posix(),
    }
    if _canonical_mapping(built.trace_inputs, "daily totals trace inputs") != expected_trace_inputs:
        raise ValueError("daily totals trace inputs differ")
    root_path = Path(root)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_relative = _REPORT_ROOT / str(report["target_date"]) / f"{now}-{str(report['content_sha256'])[:16]}.json"
    report_contents = canonical_json_bytes(report)
    report_sidecar_contents = _sidecar(report_contents)
    markdown_contents = render_totals_markdown(report).encode("utf-8")
    with open_trusted_root(root_path) as trusted:
        source_snapshot = read_regular_snapshot(trusted, Path(str(report["source_report_relative"])), "daily totals source report")
        if source_snapshot is None or _sha_bytes(source_snapshot.contents) != report["source_report_sha256"]:
            raise ValueError("daily totals source report differs")
        # Re-admit the full evidence chain before the first output write.  The
        # report bytes alone are insufficient because it only names the chain.
        source_relative = Path(str(report["source_report_relative"]))
        admitted = load_totals_sources_bound(root_path, trusted, source_relative)
        if (
            admitted.target_date != report["target_date"]
            or admitted.cutoff_date != report["cutoff_date"]
            or _source_identity(admitted.history_identity) != receipt["history_source"]
            or _source_identity(admitted.live_identity) != receipt["live_source"]
            or _source_identity(admitted.history_identity) != report["history_source"]
            or _source_identity(admitted.live_identity) != report["live_source"]
        ):
            raise ValueError("daily totals admitted source identity differs")
        rebind_regular_snapshot(trusted, source_snapshot, "daily totals source report")
        receipt_sidecar = _existing_or_write(trusted, receipt_relative.with_suffix(".sha256"), _sidecar(receipt_contents), "daily totals model receipt sidecar")
        receipt_snapshot = _existing_or_write(trusted, receipt_relative, receipt_contents, "daily totals model receipt")
        report_sidecar = _existing_or_write(trusted, report_relative.with_suffix(".sha256"), report_sidecar_contents, "daily totals report sidecar")
        report_snapshot = _existing_or_write(trusted, report_relative, report_contents, "daily totals report")
        markdown_snapshot = _existing_or_write(trusted, report_relative.with_suffix(".md"), markdown_contents, "daily totals Markdown")
        trace_contents = canonical_json_bytes(_trace_payload(report_relative, receipt_relative, receipt_contents, report_contents, report_sidecar_contents, markdown_contents, str(report["source_report_relative"]), str(report["source_report_sha256"]), report["history_source"], report["live_source"]))
        trace_snapshot = _existing_or_write(trusted, report_relative.with_suffix(".trace.json"), trace_contents, "daily totals trace")
        for snapshot, label in ((receipt_sidecar, "daily totals model receipt sidecar"), (receipt_snapshot, "daily totals model receipt"), (report_sidecar, "daily totals report sidecar"), (report_snapshot, "daily totals report"), (markdown_snapshot, "daily totals Markdown"), (trace_snapshot, "daily totals trace"), (source_snapshot, "daily totals source report")):
            rebind_regular_snapshot(trusted, snapshot, label)
        persisted_trace = validate_totals_trace(json.loads(trace_snapshot.contents))
        if (
            persisted_trace["history_source"] != report["history_source"]
            or persisted_trace["live_source"] != report["live_source"]
            or persisted_trace["report_sha256"] != _sha_bytes(report_contents)
            or persisted_trace["model_receipt_sha256"] != _sha_bytes(receipt_contents)
        ):
            raise ValueError("daily totals trace binding differs")
        trusted.assert_bound()
    return root_path / receipt_relative, root_path / report_relative, root_path / report_relative.with_suffix(".md"), root_path / report_relative.with_suffix(".trace.json")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m mlb_backtest.daily_totals")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--source-report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        built = build_daily_totals(args.root, args.date, args.source_report)
        paths = persist_daily_totals(args.root, built)
        print(paths[-1])
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, FileExistsError, json.JSONDecodeError) as exc:
        print(f"integrity/contract failure: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"operational failure: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
