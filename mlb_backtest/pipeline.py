"""Deterministic, stage-isolated orchestration for the v5.1 backtest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import load_config
from .locking import lock_predictions
from .manifest import RawStore
from .normalize import fit_frozen_calibration
from .ranking import rank_daily
from .scoring import (
    WEIGHTS,
    component_agreement,
    component_edge,
    confidence_score,
    full_agreement,
    home_probability,
    overall_completeness,
    predicted_pick,
    tier,
    uncertainty,
)
from .sources import fetch_bytes, parse_schedule, schedule_url


STAGES = (
    "raw",
    "calibration",
    "features",
    "initial_locked",
    "initial_outcomes",
    "extension_rounds",
    "locked",
    "outcomes",
    "reports",
)
_CALIBRATION_REQUIRED = frozenset(("official_date", "edge", "home_win"))
_CALIBRATION_ALLOWED = _CALIBRATION_REQUIRED | {"game_pk"}
_COMPONENTS = frozenset(WEIGHTS)
_CANDIDATE_REQUIRED = frozenset(
    (
        "game_pk",
        "official_date",
        "scheduled_start_utc",
        "away_team",
        "home_team",
        "away_scores",
        "home_scores",
        "qualities",
        "starter_career_bf",
        "critical_failure",
        "unresolved_context",
        "lineup_provenance",
        "data_status",
    )
)
_CANDIDATE_OPTIONAL = frozenset(("doubleheader_marker",))
_CANDIDATE_ALLOWED = _CANDIDATE_REQUIRED | _CANDIDATE_OPTIONAL
_FORBIDDEN_TOKENS = (
    "actual_winner",
    "market",
    "outcome",
    "result",
    "moneyline",
    "odds",
)
_DATA_STATUS = frozenset(("COMPLETE", "INCOMPLETE"))
_LINEUP_PROVENANCE = frozenset(
    ("RECONSTRUCTED_FINAL_LINEUP", "TIMESTAMP_VERIFIED_LINEUP")
)


def _backtest_schedule_window(config: Mapping[str, object]) -> dict[str, str]:
    initial = config["initial_window"]
    return {"start": initial["start"], "end": config["extension_end"]}


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _canonical_jsonl(rows: Sequence[Mapping[str, object]]) -> bytes:
    return b"".join(
        (
            json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        for row in rows
    )


def _safe_data_dir(value: str | Path) -> Path:
    root = Path(value)
    if root.is_symlink():
        raise ValueError("data directory cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("data directory must be a real directory")
    resolved_root = root.resolve(strict=True)
    resolved_stages: set[Path] = set()
    for name in STAGES:
        stage = root / name
        if stage.is_symlink():
            raise ValueError(f"stage directory cannot be a symlink: {name}")
        if stage.exists():
            if not stage.is_dir():
                raise ValueError(f"stage path must be a directory: {name}")
            resolved = stage.resolve(strict=True)
            if resolved.parent != resolved_root or resolved in resolved_stages:
                raise ValueError(f"stage path alias is forbidden: {name}")
            resolved_stages.add(resolved)
    return root


def _stage(root: Path, name: str, *, create: bool = True) -> Path:
    path = root / name
    if path.is_symlink():
        raise ValueError(f"stage directory cannot be a symlink: {name}")
    if create:
        path.mkdir(exist_ok=True)
    if not path.is_dir():
        raise ValueError(f"stage path must be a real directory: {name}")
    if path.resolve(strict=True).parent != root.resolve(strict=True):
        raise ValueError(f"stage path alias is forbidden: {name}")
    return path


def _safe_descendant_dir(
    root: Path, relative: str | Path, *, create: bool = True
) -> Path:
    """Walk and validate every directory component below a resolved data root."""
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(part in ("", "..") for part in relative_path.parts):
        raise ValueError("stage descendant must be a relative path without aliases")
    resolved_root = root.resolve(strict=True)
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"stage descendant cannot be a symlink: {relative_path}")
        if not current.exists():
            if not create:
                raise RuntimeError(f"missing stage descendant: {relative_path}")
            current.mkdir()
        if current.is_symlink() or not current.is_dir():
            raise ValueError(f"stage descendant must be a real directory: {relative_path}")
        resolved = current.resolve(strict=True)
        if resolved_root not in resolved.parents:
            raise ValueError(f"stage descendant alias is forbidden: {relative_path}")
    return current


def _regular_bytes(path: Path, label: str) -> bytes:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing prerequisite: {label}") from exc
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular non-symlink file")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError(f"{label} changed while opening")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _jsonl(path: Path, label: str) -> list[dict[str, object]]:
    content = _regular_bytes(path, label)
    rows: list[dict[str, object]] = []
    for number, raw in enumerate(content.splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} line {number} must be UTF-8 JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{label} line {number} must be a JSON object")
        rows.append(row)
    return rows


def _json_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(_regular_bytes(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _require_raw_schedule(
    root: Path, config: Mapping[str, object], role: str
) -> tuple[str, dict[int, dict[str, object]]]:
    """Verify the immutable schedule manifest required by a prepared input stage."""
    window = (
        config["training_window"]
        if role == "training"
        else _backtest_schedule_window(config)
    )
    expected_query = {
        "role": role,
        "start": window["start"],
        "end": window["end"],
        "gameType": "R",
        "url": schedule_url(window["start"], window["end"]),
    }
    folder = root / "raw" / "mlb_schedule"
    if folder.is_symlink() or not folder.is_dir():
        raise RuntimeError(f"missing prerequisite: raw/schedule manifest for {role}")
    matches: list[str] = []
    schedule_indexes: list[dict[int, dict[str, object]]] = []
    for manifest_path in sorted(folder.glob("*.manifest.json")):
        content = _regular_bytes(manifest_path, "raw schedule manifest")
        try:
            manifest = RawStore._read_manifest(content)
        except RuntimeError as exc:
            raise RuntimeError("raw schedule manifest is invalid") from exc
        if manifest.get("query") != expected_query:
            continue
        digest = manifest.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("raw schedule manifest digest is invalid")
        seal_path = manifest_path.with_name(manifest_path.name[:-5] + ".sha256")
        expected_seal = f"{hashlib.sha256(content).hexdigest()}\n".encode("utf-8")
        if _regular_bytes(seal_path, "raw schedule manifest seal") != expected_seal:
            raise RuntimeError("raw schedule manifest seal mismatch")
        payload = _regular_bytes(folder / f"{digest}.raw", "raw schedule payload")
        if hashlib.sha256(payload).hexdigest() != digest:
            raise RuntimeError("raw schedule payload digest mismatch")
        if manifest["row_count"] != sum(
            bool(line.strip()) for line in payload.splitlines()
        ):
            raise RuntimeError("raw schedule manifest row_count mismatch")
        query_bytes = json.dumps(
            expected_query,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        query_digest = hashlib.sha256(query_bytes).hexdigest()
        if manifest_path.name != f"{digest}.{query_digest}.manifest.json":
            raise RuntimeError("raw schedule manifest address mismatch")
        games = parse_schedule(folder / f"{digest}.raw")
        index = {game["game_pk"]: game for game in games}
        if len(index) != len(games):
            raise RuntimeError("raw schedule contains duplicate game_pk values")
        matches.append(digest)
        schedule_indexes.append(index)
    if len(matches) != 1:
        raise RuntimeError(
            f"missing prerequisite: raw/schedule manifest for {role}"
            if not matches
            else f"ambiguous raw/schedule manifests for {role}"
        )
    return matches[0], schedule_indexes[0]


def _write_once(path: Path, content: bytes, label: str, root: Path) -> Path:
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must remain under the data directory") from exc
    safe_parent = _safe_descendant_dir(root, relative_parent)
    if safe_parent != path.parent:
        raise ValueError(f"{label} parent path alias is forbidden")
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    if path.exists():
        if _regular_bytes(path, label) != content:
            raise RuntimeError(f"immutable {label} differs")
        return path
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if _regular_bytes(path, label) != content:
                raise RuntimeError(f"immutable {label} differs")
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _load_model_card(
    root: Path, config_path: str | Path
) -> tuple[dict[str, object], str]:
    path = root / "calibration" / "model_card.json"
    content = _regular_bytes(path, "model card")
    try:
        card = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("model card must be UTF-8 JSON") from exc
    required = {
        "alpha",
        "beta",
        "calibration_input_sha256",
        "config_sha256",
        "model_version",
        "protocol_phase",
        "schema_version",
        "training",
        "training_window",
    }
    if not isinstance(card, dict) or set(card) != required:
        raise ValueError("model card fields are invalid")
    if card["model_version"] != "v5.1-BT" or card["protocol_phase"] != "Backtest":
        raise ValueError("model card identity is invalid")
    if card["schema_version"] != 1:
        raise ValueError("model card schema version is invalid")
    if any(
        isinstance(card[field], bool)
        or not isinstance(card[field], (int, float))
        or not math.isfinite(float(card[field]))
        for field in ("alpha", "beta")
    ):
        raise ValueError("model card coefficients must be finite numbers")
    config = load_config(config_path)
    config_digest = hashlib.sha256(
        _regular_bytes(Path(config_path), "config")
    ).hexdigest()
    if card["config_sha256"] != config_digest:
        raise ValueError("model card config hash does not match current config")
    if card["training_window"] != config["training_window"]:
        raise ValueError("model card training window is not frozen")
    training = card["training"]
    if not isinstance(training, dict) or set(training) != {
        "training_min_date",
        "training_max_date",
        "training_count",
        "l2",
    }:
        raise ValueError("model card training metadata is invalid")
    if not (
        config["training_window"]["start"]
        <= training["training_min_date"]
        <= training["training_max_date"]
        <= config["training_window"]["end"]
    ):
        raise ValueError("model card training metadata crosses frozen boundaries")
    calibration_input = root / "calibration" / "training_edges.jsonl"
    if calibration_input.exists() and card["calibration_input_sha256"] != hashlib.sha256(
        _regular_bytes(calibration_input, "calibration input")
    ).hexdigest():
        raise ValueError("model card calibration input hash mismatch")
    return card, hashlib.sha256(content).hexdigest()


def fetch_stage(config_path: str | Path, data_dir: str | Path) -> list[object]:
    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    raw = _stage(root, "raw")
    store = RawStore(raw)
    artifacts = []
    for role, window in (
        ("training", config["training_window"]),
        ("backtest", _backtest_schedule_window(config)),
    ):
        url = schedule_url(window["start"], window["end"])
        artifacts.append(
            store.put(
                "mlb_schedule",
                {
                    "role": role,
                    "start": window["start"],
                    "end": window["end"],
                    "gameType": "R",
                    "url": url,
                },
                fetch_bytes(url),
            )
        )
    return artifacts


def calibrate_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    """Fit calibration from a Task 11-prepared training_edges JSONL input."""
    config_bytes = _regular_bytes(Path(config_path), "config")
    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    _require_raw_schedule(root, config, "training")
    calibration = _stage(root, "calibration")
    source = calibration / "training_edges.jsonl"
    source_bytes = _regular_bytes(source, "calibration/training_edges.jsonl")
    rows = _jsonl(source, "calibration/training_edges.jsonl")
    for row in rows:
        unknown = set(row) - _CALIBRATION_ALLOWED
        missing = _CALIBRATION_REQUIRED - set(row)
        if unknown or missing:
            raise ValueError(
                f"calibration row contract violation; unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
    alpha, beta, metadata = fit_frozen_calibration(
        rows,
        training_start=config["training_window"]["start"],
        training_end=config["training_window"]["end"],
    )
    card = {
        "alpha": alpha,
        "beta": beta,
        "calibration_input_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "model_version": config["model_version"],
        "protocol_phase": config["protocol"],
        "schema_version": 1,
        "training": metadata,
        "training_window": config["training_window"],
    }
    return _write_once(calibration / "model_card.json", _canonical_json(card), "model card", root)


def _canonical_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("scheduled_start_utc must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("scheduled_start_utc must be canonical UTC") from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if parsed.utcoffset() != timedelta(0) or canonical != value:
        raise ValueError("scheduled_start_utc must be canonical UTC ending in Z")
    return parsed


def _reject_forbidden_nested(value: object, path: str = "candidate") -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} keys must be strings")
            lowered = key.lower()
            token = next((item for item in _FORBIDDEN_TOKENS if item in lowered), None)
            if token is not None:
                raise ValueError(
                    f"unknown forbidden {token} token in candidate input"
                )
            _reject_forbidden_nested(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_nested(nested, f"{path}[{index}]")


def _validate_candidate(row: Mapping[str, object]) -> dict[str, object]:
    _reject_forbidden_nested(row)
    if "source_game_dates" in row:
        raise ValueError("same-day event provenance lists are forbidden")
    if "source_game_pks" in row:
        raise ValueError("target game_pk provenance lists are forbidden")
    unknown = set(row) - _CANDIDATE_ALLOWED
    missing = _CANDIDATE_REQUIRED - set(row)
    if unknown or missing:
        raise ValueError(
            f"candidate contract violation; unknown={sorted(unknown)}, missing={sorted(missing)}"
        )
    result = dict(row)
    game_pk = result["game_pk"]
    if isinstance(game_pk, bool) or not isinstance(game_pk, int) or game_pk <= 0:
        raise ValueError("candidate game_pk must be a positive integer")
    try:
        date.fromisoformat(result["official_date"])
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate official_date must be an ISO date") from exc
    _canonical_utc(result["scheduled_start_utc"])
    for field in ("away_team", "home_team"):
        if not isinstance(result[field], str) or not result[field].strip():
            raise ValueError(f"candidate {field} must be non-empty")
    if result["away_team"] == result["home_team"]:
        raise ValueError("candidate teams must differ")
    for field in ("away_scores", "home_scores", "qualities"):
        value = result[field]
        if not isinstance(value, Mapping) or set(value) != _COMPONENTS:
            raise ValueError(f"candidate {field} must contain exactly six components")
    if not isinstance(result["critical_failure"], bool) or not isinstance(
        result["unresolved_context"], bool
    ):
        raise ValueError("candidate failure/context flags must be bool")
    bf = result["starter_career_bf"]
    if isinstance(bf, bool) or not isinstance(bf, int) or bf < 0:
        raise ValueError("starter_career_bf must be a non-negative integer")
    if not isinstance(result["data_status"], str) or result["data_status"] not in _DATA_STATUS:
        raise ValueError("data_status must be COMPLETE or INCOMPLETE")
    if (
        not isinstance(result["lineup_provenance"], str)
        or result["lineup_provenance"] not in _LINEUP_PROVENANCE
    ):
        raise ValueError(
            "lineup_provenance must be RECONSTRUCTED_FINAL_LINEUP or "
            "TIMESTAMP_VERIFIED_LINEUP"
        )
    if "doubleheader_marker" in result and (
        not isinstance(result["doubleheader_marker"], str)
        or result["doubleheader_marker"] not in ("GAME1", "GAME2")
    ):
        raise ValueError("doubleheader_marker must be GAME1 or GAME2")
    return result


def _bind_candidate_to_schedule(
    candidate: Mapping[str, object],
    schedule_index: Mapping[int, Mapping[str, object]],
    window: Mapping[str, str],
) -> None:
    official_date = candidate["official_date"]
    if not window["start"] <= official_date <= window["end"]:
        raise ValueError("candidate is outside the configured backtest window")
    scheduled = schedule_index.get(candidate["game_pk"])
    if scheduled is None:
        raise ValueError("candidate game_pk is absent from the official schedule")
    for field in (
        "official_date",
        "scheduled_start_utc",
        "away_team",
        "home_team",
    ):
        if candidate[field] != scheduled[field]:
            raise ValueError(f"candidate {field} does not match the official schedule")


def features_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    """Score a Task 11-prepared, pregame-only candidate_components JSONL input."""
    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    _, schedule_index = _require_raw_schedule(root, config, "backtest")
    card, _ = _load_model_card(root, config_path)
    features = _stage(root, "features")
    source = features / "candidate_components.jsonl"
    candidates = [_validate_candidate(row) for row in _jsonl(source, "features/candidate_components.jsonl")]
    for candidate in candidates:
        _bind_candidate_to_schedule(
            candidate, schedule_index, _backtest_schedule_window(config)
        )
    seen: set[int] = set()
    predictions: list[dict[str, object]] = []
    for candidate in candidates:
        game_pk = candidate["game_pk"]
        if game_pk in seen:
            raise ValueError("duplicate candidate game_pk")
        seen.add(game_pk)
        edge = component_edge(candidate["home_scores"], candidate["away_scores"])
        p_home = home_probability(edge, card["alpha"], card["beta"])
        pick = predicted_pick(candidate["home_team"], candidate["away_team"], p_home)
        if pick["predicted_side"] == "none":
            raise ValueError("exactly neutral candidates cannot be locked")
        qualities = candidate["qualities"]
        completeness = overall_completeness(qualities)
        edges = {
            name: float(candidate["home_scores"][name]) - float(candidate["away_scores"][name])
            for name in _COMPONENTS
        }
        agreement = component_agreement(
            {name: edges[name] for name in ("starter", "offense", "bullpen")},
            pick["predicted_side"],
        )
        confidence = confidence_score(
            completeness,
            qualities["starter"],
            qualities["offense"],
            qualities["bullpen"],
            agreement,
            qualities["environment"],
            qualities["schedule"],
        )
        label = uncertainty(
            completeness,
            candidate["critical_failure"],
            candidate["starter_career_bf"],
            candidate["unresolved_context"],
        )
        start = _canonical_utc(candidate["scheduled_start_utc"])
        row = {
            "game_pk": game_pk,
            "official_date": candidate["official_date"],
            "scheduled_start_utc": candidate["scheduled_start_utc"],
            "lock_at_utc": (start - timedelta(minutes=45)).isoformat().replace("+00:00", "Z"),
            "away_team": candidate["away_team"],
            "home_team": candidate["home_team"],
            "predicted_side": pick["predicted_side"],
            "predicted_winner": pick["predicted_winner"],
            "p_home": p_home,
            "pick_probability": pick["pick_probability"],
            "confidence": confidence,
            "uncertainty": label,
            "tier": tier(pick["pick_probability"], confidence, label),
            "full_agreement": full_agreement(
                {name: edges[name] for name in ("starter", "offense", "bullpen")},
                pick["predicted_side"],
                label,
            ),
            "model_version": "v5.1-BT",
            "protocol_phase": "Backtest",
            "starter_edge_abs": abs(edges["starter"]),
            "overall_completeness": completeness,
            "lineup_provenance": candidate["lineup_provenance"],
            "data_status": candidate["data_status"],
        }
        if "doubleheader_marker" in candidate:
            row["doubleheader_marker"] = candidate["doubleheader_marker"]
        for component in sorted(_COMPONENTS):
            row[f"away_{component}_score"] = candidate["away_scores"][component]
            row[f"home_{component}_score"] = candidate["home_scores"][component]
            row[f"{component}_edge"] = edges[component]
            row[f"{component}_quality"] = qualities[component]
        predictions.append(row)
    predictions.sort(key=lambda row: (row["official_date"], row["scheduled_start_utc"], row["game_pk"]))
    return _write_once(features / "scored_candidates.jsonl", _canonical_jsonl(predictions), "scored candidates", root)


def _rank_and_lock(
    candidates: Sequence[Mapping[str, object]],
    output_dir: Path,
    model_hash: str,
) -> dict[str, object]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    by_game: dict[int, dict[str, object]] = {}
    for row in candidates:
        _reject_forbidden_nested(row, "scored candidate")
        game_pk = row.get("game_pk")
        if game_pk in by_game:
            raise ValueError("duplicate scored candidate game_pk")
        by_game[game_pk] = row
        grouped[row["official_date"]].append(
            {
                "game_pk": row["game_pk"],
                "official_date": row["official_date"],
                "pick_probability": row["pick_probability"],
                "confidence": row["confidence"],
                "starter_edge_abs": row["starter_edge_abs"],
                "scheduled_start_utc": row["scheduled_start_utc"],
            }
        )
    locked_rows = []
    for official_date in sorted(grouped):
        for ranked in rank_daily(grouped[official_date]):
            full = dict(by_game[ranked["game_pk"]])
            full.update({key: ranked[key] for key in ("daily_rank", "top1", "top3")})
            locked_rows.append(full)
    return lock_predictions(locked_rows, output_dir, model_hash)


def lock_stage(config_path: str | Path, data_dir: str | Path) -> dict[str, object]:
    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
    _, model_hash = _load_model_card(root, config_path)
    candidates = _jsonl(
        root / "features" / "scored_candidates.jsonl",
        "features/scored_candidates.jsonl",
    )
    initial = config["initial_window"]
    initial_candidates = [
        row
        for row in candidates
        if initial["start"] <= row["official_date"] <= initial["end"]
    ]
    return _rank_and_lock(
        initial_candidates, _stage(root, "locked"), model_hash
    )


def _require_complete_outcomes(
    lock_manifest: Path,
    outcome_rows: Sequence[Mapping[str, object]],
    model_hash: str,
) -> list[dict[str, object]]:
    from .outcomes import _load_lock

    _, predictions = _load_lock(lock_manifest, model_hash)
    counts: dict[object, int] = defaultdict(int)
    for row in outcome_rows:
        if isinstance(row, Mapping):
            game_pk = row.get("game_pk")
            if isinstance(game_pk, int) and not isinstance(game_pk, bool):
                counts[game_pk] += 1
    missing = sorted(
        row["game_pk"] for row in predictions if counts[row["game_pk"]] == 0
    )
    duplicate = sorted(
        row["game_pk"] for row in predictions if counts[row["game_pk"]] > 1
    )
    if missing:
        raise ValueError(f"missing locked outcomes: {missing}")
    if duplicate:
        raise ValueError(f"each locked candidate requires exactly one outcome: {duplicate}")
    return predictions


def _join_complete_outcomes(
    lock_manifest: Path,
    rows: Sequence[Mapping[str, object]],
    model_hash: str,
    output_root: Path,
    label: str,
    root: Path,
) -> Path:
    _require_complete_outcomes(lock_manifest, rows, model_hash)
    from .outcomes import join_outcomes

    with tempfile.TemporaryDirectory(dir=output_root) as temporary_dir:
        generated = join_outcomes(
            lock_manifest, rows, model_hash, Path(temporary_dir) / "evaluated"
        )
        generated_bytes = _regular_bytes(generated, f"generated {label}")
    return _write_once(
        output_root / "evaluated" / "evaluated_predictions.jsonl",
        generated_bytes,
        label,
        root,
    )


def outcomes_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    load_config(config_path)
    root = _safe_data_dir(data_dir)
    _, model_hash = _load_model_card(root, config_path)
    locked_manifest = root / "locked" / "lock_manifest.json"
    _regular_bytes(locked_manifest, "locked/lock_manifest.json")
    outcome_root = _stage(root, "outcomes")
    rows = _jsonl(outcome_root / "outcomes_input.jsonl", "outcomes/outcomes_input.jsonl")
    return _join_complete_outcomes(
        locked_manifest,
        rows,
        model_hash,
        outcome_root,
        "evaluated predictions",
        root,
    )


def report_stage(config_path: str | Path, data_dir: str | Path) -> dict[str, object]:
    load_config(config_path)
    root = _safe_data_dir(data_dir)
    _, model_hash = _load_model_card(root, config_path)
    evaluated_path = root / "outcomes" / "evaluated" / "evaluated_predictions.jsonl"
    evaluated = _jsonl(evaluated_path, "evaluated predictions")
    _require_complete_outcomes(
        root / "locked" / "lock_manifest.json",
        [row.get("outcome", {}) for row in evaluated],
        model_hash,
    )
    baseline_path = root / "outcomes" / "baseline.json"
    baseline_raw = _json_object(baseline_path, "baseline mapping") if baseline_path.exists() else {}
    baseline: dict[int, str] = {}
    for key, team in baseline_raw.items():
        try:
            game_pk = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError("baseline keys must be game_pk integers") from exc
        baseline[game_pk] = team
    from .metrics import evaluate

    summary = evaluate(evaluated, baseline_by_game=baseline)
    _write_once(_stage(root, "reports") / "summary.json", _canonical_json(summary), "summary report", root)
    return summary


def _array_fixture(path: Path, label: str) -> list[dict[str, object]]:
    try:
        value = json.loads(_regular_bytes(path, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be UTF-8 JSON") from exc
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"{label} must be an array of objects")
    return value


def run_fixture_replay(fixture_dir: str | Path, out_dir: str | Path) -> dict[str, object]:
    fixture = Path(fixture_dir)
    root = _safe_data_dir(out_dir)
    config_path = Path(__file__).resolve().parent.parent / "config" / "v5_1_bt.json"
    config = load_config(config_path)
    schedule_payload = _regular_bytes(fixture / "schedule.json", "fixture schedule")
    raw_store = RawStore(_stage(root, "raw"))
    for role, window in (
        ("training", config["training_window"]),
        ("backtest", _backtest_schedule_window(config)),
    ):
        raw_store.put(
            "mlb_schedule",
            {
                "role": role,
                "start": window["start"],
                "end": window["end"],
                "gameType": "R",
                "url": schedule_url(window["start"], window["end"]),
            },
            schedule_payload,
        )
    calibration_rows = _array_fixture(fixture / "calibration.json", "fixture calibration")
    game_rows = _array_fixture(fixture / "games.json", "fixture games")
    calibration = _stage(root, "calibration")
    features = _stage(root, "features")
    _write_once(calibration / "training_edges.jsonl", _canonical_jsonl(calibration_rows), "fixture calibration input", root)
    _write_once(features / "candidate_components.jsonl", _canonical_jsonl(game_rows), "fixture feature input", root)
    calibrate_stage(config_path, root)
    features_stage(config_path, root)
    _, model_hash = _load_model_card(root, config_path)
    scored_rows = _jsonl(
        root / "features" / "scored_candidates.jsonl",
        "fixture scored candidates",
    )
    all_dates = sorted({row["official_date"] for row in scored_rows})
    if len(all_dates) < 4:
        raise ValueError("fixture replay requires three initial dates and an extension date")
    initial_dates = all_dates[:3]
    initial_candidates = [
        row for row in scored_rows if row["official_date"] in initial_dates
    ]
    initial_manifest = _rank_and_lock(
        initial_candidates, _stage(root, "initial_locked"), model_hash
    )
    initial_lock_path = root / "initial_locked" / "lock_manifest.json"
    initial_lock_mtime = initial_lock_path.stat().st_mtime_ns

    # Initial outcomes remain unopened until the immutable three-date lock exists.
    initial_fixture_outcomes = _array_fixture(
        fixture / "initial_outcomes.json", "fixture initial outcomes"
    )
    initial_game_pks = {row["game_pk"] for row in initial_candidates}
    initial_outcome_rows = [
        row for row in initial_fixture_outcomes if row["game_pk"] in initial_game_pks
    ]
    initial_outcome_root = _stage(root, "initial_outcomes")
    _write_once(
        initial_outcome_root / "outcomes_input.jsonl",
        _canonical_jsonl(initial_outcome_rows),
        "fixture initial outcome input",
        root,
    )
    initial_evaluated_path = _join_complete_outcomes(
        initial_lock_path,
        initial_outcome_rows,
        model_hash,
        initial_outcome_root,
        "initial evaluated predictions",
        root,
    )
    initial_evaluated = _jsonl(initial_evaluated_path, "initial evaluated predictions")
    from .metrics import completion_status, extension_dates

    initial_completion = completion_status(initial_evaluated, target=3)
    evaluated = initial_evaluated
    accumulated_outcomes = list(initial_outcome_rows)
    selected_dates = list(initial_dates)
    selected_extension_dates: list[str] = []
    round_lock_shas: list[str] = []
    round_number = 0
    while not completion_status(evaluated, target=3)["complete"]:
        status = completion_status(evaluated, target=3)
        future_dates = [day for day in all_dates if day > max(selected_dates)]
        try:
            batch_dates = extension_dates(evaluated, future_dates, target=3)
        except ValueError as exc:
            raise RuntimeError(
                "schedule dates exhausted before graded target: "
                f"graded={status['graded_top1']} void={status['void_top1']} "
                f"target={status['target']}"
            ) from exc
        round_number += 1
        selected_dates.extend(batch_dates)
        selected_extension_dates.extend(batch_dates)
        cumulative_candidates = [
            row for row in scored_rows if row["official_date"] in selected_dates
        ]
        round_root = Path("extension_rounds") / f"round_{round_number:03d}"
        round_lock_dir = _safe_descendant_dir(root, round_root / "locked")
        round_manifest = _rank_and_lock(
            cumulative_candidates, round_lock_dir, model_hash
        )
        round_lock_shas.append(round_manifest["sha256"])

        # Each date's result is opened only after its cumulative round lock exists.
        for extension_date in batch_dates:
            accumulated_outcomes.extend(
                _array_fixture(
                    fixture
                    / "extension_outcomes_by_date"
                    / f"{extension_date}.json",
                    f"fixture extension outcomes for {extension_date}",
                )
            )
        round_outcome_dir = _safe_descendant_dir(root, round_root / "outcomes")
        round_evaluated_path = _join_complete_outcomes(
            round_lock_dir / "lock_manifest.json",
            accumulated_outcomes,
            model_hash,
            round_outcome_dir,
            f"extension round {round_number} evaluated predictions",
            root,
        )
        evaluated = _jsonl(
            round_evaluated_path,
            f"extension round {round_number} evaluated predictions",
        )

    final_dates = set(selected_dates)
    final_candidates = [
        row for row in scored_rows if row["official_date"] in final_dates
    ]
    lock_manifest = _rank_and_lock(
        final_candidates, _stage(root, "locked"), model_hash
    )
    lock_path = root / "locked" / "lock_manifest.json"
    lock_mtime = lock_path.stat().st_mtime_ns

    final_game_pks = {row["game_pk"] for row in final_candidates}
    final_outcome_rows = [
        row for row in accumulated_outcomes if row["game_pk"] in final_game_pks
    ]
    outcome_root = _stage(root, "outcomes")
    _write_once(outcome_root / "outcomes_input.jsonl", _canonical_jsonl(final_outcome_rows), "fixture outcome input", root)
    evaluated_path = outcomes_stage(config_path, root)
    summary = report_stage(config_path, root)
    prediction_rows = _jsonl(root / "locked" / "predictions.jsonl", "locked predictions")
    initial_prediction_rows = _jsonl(
        root / "initial_locked" / "predictions.jsonl", "initial locked predictions"
    )
    overlapping = [
        row for row in prediction_rows if row["official_date"] in initial_dates
    ]
    if initial_prediction_rows != overlapping:
        raise RuntimeError("initial locked predictions changed in the final lock")
    selected = [row for row in prediction_rows if row["top1"] == "Yes"]
    result = {
        "selection_dates": len({row["official_date"] for row in selected}),
        "top1_count": len(selected),
        "graded_count": summary["primary"]["N"],
        "graded_top1": summary["primary"]["N"],
        "void_count": summary["void_top1"],
        "void_top1": summary["void_top1"],
        "lock_mtime_ns": lock_mtime,
        "outcome_mtime_ns": evaluated_path.stat().st_mtime_ns,
        "initial_lock_mtime_ns": initial_lock_mtime,
        "initial_outcome_mtime_ns": initial_evaluated_path.stat().st_mtime_ns,
        "initial_lock_sha": initial_manifest["sha256"],
        "initial_locked_sha256": initial_manifest["sha256"],
        "extension_dates": selected_extension_dates,
        "round_lock_shas": round_lock_shas,
        "round_hashes": round_lock_shas,
        "initial_completion": initial_completion,
        "final_lock_sha": lock_manifest["sha256"],
        "lock_sha256": lock_manifest["sha256"],
        "locked_sha256": lock_manifest["sha256"],
        "summary": summary,
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mlb_backtest.pipeline",
        description="Run deterministic backtest stages over prepared JSONL inputs.",
        epilog=(
            "calibrate and features require immutable raw schedule manifests. "
            "Preparing training_edges.jsonl and candidate_components.jsonl is "
            "the upstream Task 11 contract, not feature extraction performed here."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in (
        "fetch",
        "fetch-statcast",
        "calibrate",
        "features",
        "lock",
        "outcomes",
        "report",
    ):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", required=True)
        subparser.add_argument("--data-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from .statcast import fetch_statcast_stage

    commands = {
        "fetch": fetch_stage,
        "fetch-statcast": fetch_statcast_stage,
        "calibrate": calibrate_stage,
        "features": features_stage,
        "lock": lock_stage,
        "outcomes": outcomes_stage,
        "report": report_stage,
    }
    commands[args.command](args.config, args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "calibrate_stage",
    "features_stage",
    "fetch_stage",
    "lock_stage",
    "main",
    "outcomes_stage",
    "report_stage",
    "run_fixture_replay",
]
