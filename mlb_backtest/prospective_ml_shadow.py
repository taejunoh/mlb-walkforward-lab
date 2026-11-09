"""Append-only, same-input temporal-candidate shadow attachments.

This module never changes a live card or creates another settlement.  It
attaches a frozen temporal candidate probability to an already genuine,
pre-start baseline primary claim.  A later paired reporter can use the
baseline snapshot's single authenticated settlement for both probabilities.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .live_top3_contracts import canonical_json_bytes, validate_run_payload
from .live_top3_trace import validate_trace_payload
from .market_play_authorities import load_ml_authority
from .market_play_contracts import canonical_utc, canonical_utc_ns
from .prospective_ml_observation import (
    _relative,
    _sha,
    load_ml_snapshot,
    validate_primary_claim,
)
from .scoring import home_probability, predicted_pick
from .scoring_prep import score_raw_game, validate_normalization
from .secure_fs import open_trusted_root, read_regular_snapshot, rebind_regular_snapshot, write_exclusive


PROFILE_RECEIPT = Path(
    "reports/model_readiness/2026-09-02/temporal_calibration/"
    "prepare-corrected/prepare_receipt.json"
)
EXPECTED_RECEIPT_SHA256 = "9bc723332b80f36d8786f5258a51ebd247cf7b05879ca2d68a28e02c32ff5805"
ATTACHMENT_SCHEMA = "PROSPECTIVE_ML_SHADOW_TEMPORAL_ATTACHMENT_V1"
ATTACHMENT_ROOT = Path("audit/prospective_ml_shadow/attachments")
CANDIDATE_ROOT = Path("audit/prospective_ml_shadow/candidates")
COMMIT_SCHEMA = "PROSPECTIVE_ML_SHADOW_TEMPORAL_COMMIT_V1"
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FrozenShadowProfile:
    """The fixed 2022-normalization/2023-calibration candidate identity."""

    receipt_relative: Path
    receipt_sha256: str
    profile_sha256: str
    normalization: Mapping[str, object]
    alpha: float
    beta: float
    dependency_sha256: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class BuiltShadowAttachment:
    root: Path
    baseline_primary_claim: Path
    observed_at_utc: str
    attachment: dict[str, object]
    attachment_bytes: bytes


@dataclass(frozen=True, slots=True)
class PersistedShadowAttachment:
    attachment_path: Path
    attachment: dict[str, object]


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _dependency_sha256() -> dict[str, str]:
    root = _workspace_root()
    paths = {
        "normalize.py": root / "mlb_backtest/normalize.py",
        "scoring.py": root / "mlb_backtest/scoring.py",
        "scoring_prep.py": root / "mlb_backtest/scoring_prep.py",
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def _read_bytes(root: Path, relative: Path, label: str, *, max_bytes: int = MAX_BYTES) -> bytes:
    with open_trusted_root(root) as trusted:
        snapshot = read_regular_snapshot(trusted, relative, label, max_bytes=max_bytes)
        if snapshot is None:
            raise ValueError(f"{label} is missing")
        rebind_regular_snapshot(trusted, snapshot, label, max_bytes=max_bytes)
        return snapshot.contents


def _read_json(root: Path, relative: Path, label: str) -> tuple[bytes, dict[str, object]]:
    raw = _read_bytes(root, relative, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return raw, value


def _finite_probability(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} differs")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} differs") from exc
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} differs")
    return result


def load_frozen_shadow_profile(root: Path | str) -> FrozenShadowProfile:
    """Load only the fixed corrected receipt; no historic predictions/outcomes."""
    root = Path(root)
    raw = _read_bytes(root, PROFILE_RECEIPT, "corrected temporal receipt")
    sidecar = _read_bytes(
        root,
        PROFILE_RECEIPT.with_suffix(PROFILE_RECEIPT.suffix + ".sha256"),
        "corrected temporal receipt sidecar",
        max_bytes=128,
    )
    digest = _sha(raw)
    if digest != EXPECTED_RECEIPT_SHA256 or sidecar != f"{digest}\n".encode("ascii"):
        raise ValueError("corrected temporal receipt digest differs")
    try:
        receipt = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("corrected temporal receipt is invalid") from exc
    if not isinstance(receipt, Mapping):
        raise ValueError("corrected temporal receipt differs")
    if receipt.get("label") != "RETROSPECTIVE_CANDIDATE_ONLY_NOT_FOR_LIVE_USE" or receipt.get("promotion") != "NO_PROMOTION":
        raise ValueError("corrected temporal receipt is not shadow-only")
    normalization = receipt.get("normalization")
    calibration = receipt.get("calibration")
    if not isinstance(normalization, Mapping) or normalization.get("training_year") != 2022 or not isinstance(calibration, Mapping) or calibration.get("training_year") != 2023:
        raise ValueError("corrected temporal receipt partition differs")
    values = normalization.get("value")
    try:
        validate_normalization(values)
    except (TypeError, ValueError) as exc:
        raise ValueError("corrected temporal normalization differs") from exc
    alpha = _finite_probability(calibration.get("alpha"), "corrected temporal alpha")
    beta = _finite_probability(calibration.get("beta"), "corrected temporal beta")
    if beta <= 0.0:
        raise ValueError("corrected temporal beta differs")
    dependencies = receipt.get("scoring_dependency_sha256")
    if not isinstance(dependencies, Mapping) or set(dependencies) != {"normalize.py", "scoring.py", "scoring_prep.py"}:
        raise ValueError("corrected temporal dependencies differ")
    expected_dependencies = _dependency_sha256()
    if dict(dependencies) != expected_dependencies:
        raise ValueError("corrected temporal scoring dependencies differ")
    profile_body = {
        "schema": "FROZEN_TEMPORAL_SHADOW_PROFILE_V1",
        "receipt_sha256": digest,
        "normalization": values,
        "alpha": alpha,
        "beta": beta,
        "dependency_sha256": expected_dependencies,
    }
    return FrozenShadowProfile(
        PROFILE_RECEIPT,
        digest,
        _sha(canonical_json_bytes(profile_body)),
        values,
        alpha,
        beta,
        expected_dependencies,
    )


def _trace_row(root: Path, snapshot: Mapping[str, object]) -> tuple[dict[str, object], str]:
    trace_relative = _relative(root, str(snapshot["ml_trace_relative"]), "baseline ML trace")
    raw, trace = _read_json(root, trace_relative, "baseline ML trace")
    if _sha(raw) != snapshot["ml_trace_sha256"]:
        raise ValueError("baseline ML trace digest differs")
    trace = validate_trace_payload(trace)
    matches = [row for row in trace["games"] if row["game_pk"] == snapshot["game_pk"]]
    if len(matches) != 1:
        raise ValueError("baseline ML trace game differs")
    row = matches[0]
    for key in ("target_date", "scheduled_start_utc", "away_team", "home_team"):
        snapshot_key = "target_date" if key == "target_date" else key
        if row[key] != snapshot[snapshot_key]:
            raise ValueError("baseline ML trace identity differs")
    for key in ("p_home", "pick_probability"):
        if _finite_probability(row[key], f"trace {key}") != _finite_probability(snapshot[key], f"baseline {key}"):
            raise ValueError("baseline probability differs from trace")
    if row["predicted_side"] != snapshot["predicted_side"]:
        raise ValueError("baseline predicted side differs from trace")
    raw_features = row["raw_features"]
    if _sha(canonical_json_bytes(raw_features)) != row["feature_sha256"]:
        raise ValueError("baseline feature digest differs")
    return row, _sha(raw)


def _source_durable_times(root: Path, snapshot: Mapping[str, object]) -> tuple[str, ...]:
    """Return every bound live-source durable instant for the trace's report."""
    trace_relative = _relative(root, str(snapshot["ml_trace_relative"]), "baseline ML trace")
    trace_raw, trace = _read_json(root, trace_relative, "baseline ML trace")
    if _sha(trace_raw) != snapshot["ml_trace_sha256"]:
        raise ValueError("baseline ML trace digest differs")
    trace = validate_trace_payload(trace)
    report_relative = _relative(root, str(trace["primary_report_relative"]), "baseline ML report")
    report_raw, report = _read_json(root, report_relative, "baseline ML report")
    report = validate_run_payload(report)
    digest = _sha(canonical_json_bytes({key: item for key, item in report.items() if key != "content_sha256"}))
    if digest != trace["primary_report_content_sha256"] or _sha(report_raw) != _sha(canonical_json_bytes(report)):
        raise ValueError("baseline ML report binding differs")
    times = []
    for binding in report["source_bindings"]:
        durable = binding.get("durable_at_utc")
        if not isinstance(durable, str):
            raise ValueError("baseline source durable time differs")
        times.append(canonical_utc(durable))
    if not times:
        raise ValueError("baseline source durable population differs")
    return tuple(times)


def _before_start(value: str, start: object, label: str) -> None:
    if canonical_utc_ns(value) >= canonical_utc_ns(start):
        raise ValueError(f"{label} is not before scheduled start")


def _authority_game(root: Path, snapshot: Mapping[str, object]):
    """Rebind current sealed source evidence to the already-primary snapshot."""
    authority = load_ml_authority(root, Path(str(snapshot["ml_trace_relative"])))
    source_game = authority.by_game.get(int(snapshot["game_pk"]))
    if (
        authority.target_date != snapshot["target_date"]
        or authority.model_card_sha256 != snapshot["model_card_sha256"]
        or source_game is None
        or source_game.scheduled_start_utc != snapshot["scheduled_start_utc"]
        or source_game.away_team != snapshot["away_team"]
        or source_game.home_team != snapshot["home_team"]
    ):
        raise ValueError("baseline authority game differs")
    if (
        source_game.p_home is None
        or source_game.predicted_side != snapshot["predicted_side"]
        or source_game.pick_probability is None
        or float(source_game.p_home) != _finite_probability(snapshot["p_home"], "baseline p_home")
        or float(source_game.pick_probability)
        != _finite_probability(snapshot["pick_probability"], "baseline pick probability")
    ):
        raise ValueError("baseline authority probability differs")
    return source_game


def _candidate_from_raw(
    raw_features: Mapping[str, object], profile: FrozenShadowProfile, snapshot: Mapping[str, object]
) -> dict[str, object]:
    scored = score_raw_game(raw_features, profile.normalization)
    edge = float(scored["edge"])
    p_home = home_probability(edge, profile.alpha, profile.beta)
    pick = predicted_pick(str(snapshot["home_team"]), str(snapshot["away_team"]), p_home)
    return {
        "edge": edge,
        "p_home": p_home,
        "predicted_side": pick["predicted_side"],
        "pick_probability": pick["pick_probability"],
    }


def _candidate_path(value: Mapping[str, object]) -> Path:
    baseline = value["baseline"]
    profile = value["profile"]
    assert isinstance(baseline, Mapping) and isinstance(profile, Mapping)
    return CANDIDATE_ROOT / str(value["target_date"]) / str(value["game_pk"]) / str(baseline["model_card_sha256"]) / str(profile["profile_sha256"]) / f"{value['content_sha256']}.json"


def _commit_path(value: Mapping[str, object]) -> Path:
    baseline = value["baseline"]
    profile = value["profile"]
    assert isinstance(baseline, Mapping) and isinstance(profile, Mapping)
    return ATTACHMENT_ROOT / str(value["target_date"]) / str(value["game_pk"]) / str(baseline["model_card_sha256"]) / f"{profile['profile_sha256']}.json"


def _digest_body(value: Mapping[str, object]) -> str:
    return _sha(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"}))


def _validate_attachment(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("shadow attachment differs")
    safe = json.loads(canonical_json_bytes(value))
    required = {
        "schema", "content_sha256", "shadow_status", "promotion", "observed_at_utc", "staged_at_utc",
        "target_date", "game_pk", "scheduled_start_utc", "away_team", "home_team", "feature_sha256",
        "baseline", "candidate", "profile",
    }
    if set(safe) != required or safe["schema"] != ATTACHMENT_SCHEMA or safe["content_sha256"] != _digest_body(safe):
        raise ValueError("shadow attachment differs")
    if safe["shadow_status"] != "SHADOW_ONLY" or safe["promotion"] != "NO_PROMOTION":
        raise ValueError("shadow attachment admission differs")
    canonical_utc(safe["observed_at_utc"])
    canonical_utc(safe["staged_at_utc"])
    canonical_utc(safe["scheduled_start_utc"])
    _before_start(str(safe["observed_at_utc"]), safe["scheduled_start_utc"], "shadow observation")
    _before_start(str(safe["staged_at_utc"]), safe["scheduled_start_utc"], "shadow staged record")
    if canonical_utc_ns(safe["staged_at_utc"]) < canonical_utc_ns(safe["observed_at_utc"]):
        raise ValueError("shadow attachment staged time differs")
    if type(safe["game_pk"]) is not int or safe["game_pk"] <= 0 or not all(isinstance(safe[key], str) and safe[key] for key in ("target_date", "away_team", "home_team", "feature_sha256")):
        raise ValueError("shadow attachment identity differs")
    if not isinstance(safe["baseline"], Mapping) or set(safe["baseline"]) != {"primary_claim_relative", "primary_claim_sha256", "snapshot_relative", "snapshot_sha256", "model_card_sha256", "ml_trace_relative", "ml_trace_sha256", "p_home", "predicted_side", "pick_probability"}:
        raise ValueError("shadow attachment baseline differs")
    if not isinstance(safe["candidate"], Mapping) or set(safe["candidate"]) != {"edge", "p_home", "predicted_side", "pick_probability"}:
        raise ValueError("shadow attachment candidate differs")
    if not isinstance(safe["profile"], Mapping) or set(safe["profile"]) != {"receipt_relative", "receipt_sha256", "profile_sha256", "normalization_training_year", "calibration_training_year", "alpha", "beta", "dependency_sha256"}:
        raise ValueError("shadow attachment profile differs")
    for part in (safe["baseline"], safe["candidate"]):
        p_home = _finite_probability(part["p_home"], "shadow attachment probability")
        side = part["predicted_side"]
        pick = _finite_probability(part["pick_probability"], "shadow attachment pick probability")
        if side not in {"home", "away", "none"} or pick != (0.5 if side == "none" else p_home if side == "home" else 1.0 - p_home):
            raise ValueError("shadow attachment pick differs")
    if not math.isfinite(float(safe["candidate"]["edge"])):
        raise ValueError("shadow attachment edge differs")
    if safe["profile"]["normalization_training_year"] != 2022 or safe["profile"]["calibration_training_year"] != 2023:
        raise ValueError("shadow attachment profile partition differs")
    return safe


def build_shadow_attachment(
    root: Path | str,
    baseline_primary_claim: Path | str,
    *,
    observed_clock: str | Callable[[], str],
) -> BuiltShadowAttachment:
    """Build one pre-start attachment from an authenticated baseline claim."""
    root = Path(root)
    observed = canonical_utc(observed_clock() if callable(observed_clock) else observed_clock)
    claim_relative = _relative(root, baseline_primary_claim, "baseline primary claim")
    claim_raw, claim = _read_json(root, claim_relative, "baseline primary claim")
    claim = validate_primary_claim(root, claim_relative)
    snapshot_relative = _relative(root, str(claim["candidate_relative"]), "baseline snapshot")
    snapshot_raw, _ = _read_json(root, snapshot_relative, "baseline snapshot")
    snapshot = load_ml_snapshot(root, snapshot_relative)
    if _sha(snapshot_raw) != claim["candidate_sha256"]:
        raise ValueError("baseline primary snapshot digest differs")
    profile = load_frozen_shadow_profile(root)
    # This revalidates the report, trace, schedule and participant evidence.
    source_game = _authority_game(root, snapshot)
    row, trace_sha = _trace_row(root, snapshot)
    start = str(snapshot["scheduled_start_utc"])
    _before_start(str(claim["claimed_at_utc"]), start, "baseline primary claim")
    _before_start(str(snapshot["observed_at_utc"]), start, "baseline observation")
    _before_start(observed, start, "shadow observation")
    if canonical_utc_ns(claim["claimed_at_utc"]) > canonical_utc_ns(observed):
        raise ValueError("shadow observation predates baseline primary claim")
    if canonical_utc_ns(observed) < canonical_utc_ns(snapshot["observed_at_utc"]):
        raise ValueError("shadow observation predates baseline observation")
    durable_source = source_game.participant.durable_at_utc
    if durable_source is None or canonical_utc_ns(durable_source) > canonical_utc_ns(observed):
        raise ValueError("shadow observation predates pregame evidence")
    if any(canonical_utc_ns(value) > canonical_utc_ns(observed) for value in _source_durable_times(root, snapshot)):
        raise ValueError("shadow observation predates bound source evidence")
    raw_features = row["raw_features"]
    candidate = _candidate_from_raw(raw_features, profile, snapshot)
    attachment: dict[str, object] = {
        "schema": ATTACHMENT_SCHEMA,
        "shadow_status": "SHADOW_ONLY",
        "promotion": "NO_PROMOTION",
        "observed_at_utc": observed,
        "staged_at_utc": observed,
        "target_date": snapshot["target_date"],
        "game_pk": snapshot["game_pk"],
        "scheduled_start_utc": start,
        "away_team": snapshot["away_team"],
        "home_team": snapshot["home_team"],
        "feature_sha256": row["feature_sha256"],
        "baseline": {
            "primary_claim_relative": claim_relative.as_posix(),
            "primary_claim_sha256": _sha(claim_raw),
            "snapshot_relative": snapshot_relative.as_posix(),
            "snapshot_sha256": _sha(snapshot_raw),
            "model_card_sha256": snapshot["model_card_sha256"],
            "ml_trace_relative": snapshot["ml_trace_relative"],
            "ml_trace_sha256": snapshot["ml_trace_sha256"],
            "p_home": snapshot["p_home"],
            "predicted_side": snapshot["predicted_side"],
            "pick_probability": snapshot["pick_probability"],
        },
        "candidate": candidate,
        "profile": {
            "receipt_relative": profile.receipt_relative.as_posix(),
            "receipt_sha256": profile.receipt_sha256,
            "profile_sha256": profile.profile_sha256,
            "normalization_training_year": 2022,
            "calibration_training_year": 2023,
            "alpha": profile.alpha,
            "beta": profile.beta,
            "dependency_sha256": dict(profile.dependency_sha256),
        },
    }
    attachment["content_sha256"] = _digest_body(attachment)
    attachment = _validate_attachment(attachment)
    # The trace digest is carried by the baseline snapshot and rechecked above.
    if trace_sha != snapshot["ml_trace_sha256"]:
        raise ValueError("baseline trace binding differs")
    return BuiltShadowAttachment(root, claim_relative, observed, attachment, canonical_json_bytes(attachment))


def _write_idempotent(trusted, relative: Path, raw: bytes, label: str, *, final_rebind=None) -> None:
    try:
        write_exclusive(trusted, relative, raw, label, final_rebind=final_rebind)
    except FileExistsError:
        current = read_regular_snapshot(trusted, relative, label, max_bytes=len(raw))
        if current is None or current.contents != raw:
            raise ValueError(f"{label} differs")
        rebind_regular_snapshot(trusted, current, label, max_bytes=len(raw))


def _load_staged_candidate(root: Path, relative: Path) -> dict[str, object]:
    """Validate a content-addressed candidate; callers must still require commit."""
    raw, value = _read_json(root, relative, "shadow attachment")
    sidecar = _read_bytes(
        root,
        relative.with_suffix(relative.suffix + ".sha256"),
        "shadow attachment sidecar",
        max_bytes=128,
    )
    if sidecar != f"{_sha(raw)}\n".encode("ascii"):
        raise ValueError("shadow attachment sidecar differs")
    if canonical_json_bytes(value) != raw:
        raise ValueError("shadow attachment is not canonical")
    value = _validate_attachment(value)
    if _candidate_path(value) != relative:
        raise ValueError("shadow attachment path differs")
    baseline = value["baseline"]
    assert isinstance(baseline, Mapping)
    claim_relative = _relative(root, str(baseline["primary_claim_relative"]), "baseline primary claim")
    claim_raw, claim = _read_json(root, claim_relative, "baseline primary claim")
    claim = validate_primary_claim(root, claim_relative)
    if _sha(claim_raw) != baseline["primary_claim_sha256"]:
        raise ValueError("shadow attachment primary claim binding differs")
    snapshot_relative = _relative(root, str(baseline["snapshot_relative"]), "baseline snapshot")
    snapshot_raw, _ = _read_json(root, snapshot_relative, "baseline snapshot")
    snapshot = load_ml_snapshot(root, snapshot_relative)
    if (
        _sha(snapshot_raw) != baseline["snapshot_sha256"]
        or claim.get("candidate_relative") != snapshot_relative.as_posix()
        or claim.get("candidate_sha256") != baseline["snapshot_sha256"]
    ):
        raise ValueError("shadow attachment baseline snapshot binding differs")
    for key in ("target_date", "game_pk", "scheduled_start_utc", "away_team", "home_team", "model_card_sha256", "ml_trace_relative", "ml_trace_sha256", "p_home", "predicted_side", "pick_probability"):
        source = snapshot[key] if key in snapshot else None
        target = value[key] if key in value else baseline[key]
        if source != target:
            raise ValueError("shadow attachment baseline identity differs")
    _before_start(str(claim["claimed_at_utc"]), snapshot["scheduled_start_utc"], "baseline primary claim")
    _before_start(str(snapshot["observed_at_utc"]), snapshot["scheduled_start_utc"], "baseline observation")
    source_game = _authority_game(root, snapshot)
    durable_source = source_game.participant.durable_at_utc
    if durable_source is None or canonical_utc_ns(durable_source) > canonical_utc_ns(value["observed_at_utc"]):
        raise ValueError("shadow attachment predates pregame evidence")
    if any(canonical_utc_ns(item) > canonical_utc_ns(value["observed_at_utc"]) for item in _source_durable_times(root, snapshot)):
        raise ValueError("shadow attachment predates bound source evidence")
    if canonical_utc_ns(value["observed_at_utc"]) < canonical_utc_ns(snapshot["observed_at_utc"]):
        raise ValueError("shadow attachment predates baseline observation")
    if canonical_utc_ns(value["observed_at_utc"]) < canonical_utc_ns(claim["claimed_at_utc"]):
        raise ValueError("shadow attachment predates baseline primary claim")
    row, _ = _trace_row(root, snapshot)
    if row["feature_sha256"] != value["feature_sha256"]:
        raise ValueError("shadow attachment feature binding differs")
    profile = load_frozen_shadow_profile(root)
    expected_profile = {
        "receipt_relative": profile.receipt_relative.as_posix(),
        "receipt_sha256": profile.receipt_sha256,
        "profile_sha256": profile.profile_sha256,
        "normalization_training_year": 2022,
        "calibration_training_year": 2023,
        "alpha": profile.alpha,
        "beta": profile.beta,
        "dependency_sha256": dict(profile.dependency_sha256),
    }
    if value["profile"] != expected_profile:
        raise ValueError("shadow attachment frozen profile differs")
    raw_features = row["raw_features"]
    if not isinstance(raw_features, Mapping):
        raise ValueError("shadow attachment raw features differ")
    if value["candidate"] != _candidate_from_raw(raw_features, profile, snapshot):
        raise ValueError("shadow attachment candidate score differs")
    return value


def _commit_digest(value: Mapping[str, object]) -> str:
    return _sha(canonical_json_bytes({key: item for key, item in value.items() if key != "content_sha256"}))


def load_shadow_attachment(root: Path | str, attachment: Path | str) -> dict[str, object]:
    """Load only a committed attachment; staged candidates are never admitted."""
    root = Path(root)
    relative = _relative(root, attachment, "shadow attachment commit")
    raw, commit = _read_json(root, relative, "shadow attachment commit")
    if canonical_json_bytes(commit) != raw:
        raise ValueError("shadow attachment commit differs")
    required = {"schema", "content_sha256", "candidate_relative", "candidate_sha256", "claimed_at_utc"}
    if set(commit) != required or commit.get("schema") != COMMIT_SCHEMA or commit.get("content_sha256") != _commit_digest(commit):
        raise ValueError("shadow attachment commit differs")
    canonical_utc(commit["claimed_at_utc"])
    candidate_relative = _relative(root, str(commit["candidate_relative"]), "shadow candidate")
    candidate_raw = _read_bytes(root, candidate_relative, "shadow candidate")
    if _sha(candidate_raw) != commit["candidate_sha256"]:
        raise ValueError("shadow attachment commit candidate binding differs")
    candidate = _load_staged_candidate(root, candidate_relative)
    if _commit_path(candidate) != relative:
        raise ValueError("shadow attachment commit path differs")
    _before_start(str(commit["claimed_at_utc"]), candidate["scheduled_start_utc"], "shadow primary commit")
    if canonical_utc_ns(commit["claimed_at_utc"]) < max(
        canonical_utc_ns(candidate["observed_at_utc"]),
        canonical_utc_ns(candidate["staged_at_utc"]),
    ):
        raise ValueError("shadow attachment commit predates candidate evidence")
    result = dict(candidate)
    result["commit_claimed_at_utc"] = commit["claimed_at_utc"]
    return result


def _source_body(value: Mapping[str, object]) -> dict[str, object]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"staged_at_utc", "content_sha256"}
    }


def _existing_attachment_for_claim(
    root: Path, baseline_primary_claim: Path | str
) -> PersistedShadowAttachment | None:
    """Resolve an existing first attachment without creating a late forecast."""
    claim_relative = _relative(root, baseline_primary_claim, "baseline primary claim")
    claim = validate_primary_claim(root, claim_relative)
    snapshot_relative = _relative(root, str(claim["candidate_relative"]), "baseline snapshot")
    snapshot = load_ml_snapshot(root, snapshot_relative)
    profile = load_frozen_shadow_profile(root)
    relative = (
        ATTACHMENT_ROOT
        / str(snapshot["target_date"])
        / str(snapshot["game_pk"])
        / str(snapshot["model_card_sha256"])
        / f"{profile.profile_sha256}.json"
    )
    with open_trusted_root(root) as trusted:
        existing = read_regular_snapshot(
            trusted, relative, "shadow attachment", max_bytes=MAX_BYTES, missing_ok=True
        )
    if existing is None:
        return None
    return PersistedShadowAttachment(relative, load_shadow_attachment(root, relative))


def persist_shadow_attachment(
    root: Path | str,
    built: BuiltShadowAttachment,
    *,
    primary_clock: Callable[[], str],
) -> PersistedShadowAttachment:
    """Append the first valid same-input attachment, never a late replacement."""
    root = Path(root)
    if not isinstance(built, BuiltShadowAttachment) or built.root != root or not callable(primary_clock):
        raise ValueError("built shadow attachment differs")
    attachment = _validate_attachment(built.attachment)
    if canonical_json_bytes(attachment) != built.attachment_bytes:
        raise ValueError("shadow attachment transport differs")
    relative = _commit_path(attachment)
    with open_trusted_root(root) as trusted:
        existing = read_regular_snapshot(trusted, relative, "shadow attachment", max_bytes=MAX_BYTES, missing_ok=True)
        if existing is not None:
            rebind_regular_snapshot(trusted, existing, "shadow attachment", max_bytes=MAX_BYTES)
            prior = load_shadow_attachment(root, relative)
            if prior["baseline"] != attachment["baseline"] or prior["profile"] != attachment["profile"] or prior["feature_sha256"] != attachment["feature_sha256"]:
                raise ValueError("shadow attachment identity conflicts")
            return PersistedShadowAttachment(relative, prior)
        # This first sample gates staging only. It is deliberately not recorded
        # as pair-commit time; the marker gate samples the clock again below.
        _before_start(canonical_utc(primary_clock()), attachment["scheduled_start_utc"], "shadow candidate stage")
        candidate_raw = canonical_json_bytes(attachment)
        candidate_relative = _candidate_path(attachment)
        _write_idempotent(
            trusted,
            candidate_relative.with_suffix(".json.sha256"),
            f"{_sha(candidate_raw)}\n".encode("ascii"),
            "shadow candidate sidecar",
        )
        _write_idempotent(
            trusted,
            candidate_relative,
            candidate_raw,
            "shadow candidate",
        )
        # The marker is written only after a final, pre-commit source seal. A
        # failure leaves the content-addressed candidate diagnostic-only.
        rebuilt = build_shadow_attachment(
            root, built.baseline_primary_claim, observed_clock=built.observed_at_utc
        )
        if _source_body(rebuilt.attachment) != _source_body(attachment):
            raise ValueError("shadow attachment sources changed")
        claimed_at = canonical_utc(primary_clock())
        _before_start(claimed_at, attachment["scheduled_start_utc"], "shadow primary commit")
        commit: dict[str, object] = {
            "schema": COMMIT_SCHEMA,
            "candidate_relative": candidate_relative.as_posix(),
            "candidate_sha256": _sha(candidate_raw),
            "claimed_at_utc": claimed_at,
        }
        commit["content_sha256"] = _commit_digest(commit)
        commit_raw = canonical_json_bytes(commit)
        _write_idempotent(trusted, relative, commit_raw, "shadow attachment commit")
    result = dict(attachment)
    result["commit_claimed_at_utc"] = claimed_at
    return PersistedShadowAttachment(relative, result)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append a frozen temporal shadow attachment")
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--root", required=True)
    snapshot.add_argument("--baseline-primary-claim", required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(args.root)
        existing = _existing_attachment_for_claim(root, args.baseline_primary_claim)
        if existing is not None:
            print(existing.attachment_path.as_posix())
            return 0
        now = _now()
        built = build_shadow_attachment(root, args.baseline_primary_claim, observed_clock=now)
        persisted = persist_shadow_attachment(root, built, primary_clock=_now)
        print(persisted.attachment_path.as_posix())
        return 0
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
