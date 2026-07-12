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
from .sources import fetch_bytes, schedule_url


STAGES = ("raw", "calibration", "features", "locked", "outcomes", "reports")
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
_CANDIDATE_OPTIONAL = frozenset(("source_game_dates", "source_game_pks"))
_CANDIDATE_ALLOWED = _CANDIDATE_REQUIRED | _CANDIDATE_OPTIONAL


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


def _write_once(path: Path, content: bytes, label: str) -> Path:
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    path.parent.mkdir(parents=True, exist_ok=True)
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
        ("backtest", config["initial_window"]),
    ):
        url = schedule_url(window["start"], window["end"])
        artifacts.append(
            store.put(
                "mlb_schedule",
                {"role": role, "start": window["start"], "end": window["end"], "url": url},
                fetch_bytes(url),
            )
        )
    return artifacts


def calibrate_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    config_bytes = _regular_bytes(Path(config_path), "config")
    config = load_config(config_path)
    root = _safe_data_dir(data_dir)
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
    return _write_once(calibration / "model_card.json", _canonical_json(card), "model card")


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


def _validate_candidate(row: Mapping[str, object]) -> dict[str, object]:
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
        official = date.fromisoformat(result["official_date"])
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
    for field in ("lineup_provenance", "data_status"):
        if not isinstance(result[field], (str, dict, list)):
            raise ValueError(f"candidate {field} must be safe JSON provenance")
    dates = result.get("source_game_dates", [])
    pks = result.get("source_game_pks", [])
    if not isinstance(dates, list) or not isinstance(pks, list):
        raise ValueError("candidate provenance lists must be lists")
    for source_date in dates:
        try:
            parsed_date = date.fromisoformat(source_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("source_game_dates must contain ISO dates") from exc
        if parsed_date >= official:
            raise ValueError("same-day or future event data is forbidden")
    if game_pk in pks:
        raise ValueError("target game_pk data is forbidden")
    return result


def features_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    load_config(config_path)
    root = _safe_data_dir(data_dir)
    card, _ = _load_model_card(root, config_path)
    features = _stage(root, "features")
    source = features / "candidate_components.jsonl"
    candidates = [_validate_candidate(row) for row in _jsonl(source, "features/candidate_components.jsonl")]
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
        for component in sorted(_COMPONENTS):
            row[f"away_{component}_score"] = candidate["away_scores"][component]
            row[f"home_{component}_score"] = candidate["home_scores"][component]
            row[f"{component}_edge"] = edges[component]
            row[f"{component}_quality"] = qualities[component]
        predictions.append(row)
    predictions.sort(key=lambda row: (row["official_date"], row["scheduled_start_utc"], row["game_pk"]))
    return _write_once(features / "scored_candidates.jsonl", _canonical_jsonl(predictions), "scored candidates")


def lock_stage(config_path: str | Path, data_dir: str | Path) -> dict[str, object]:
    load_config(config_path)
    root = _safe_data_dir(data_dir)
    _, model_hash = _load_model_card(root, config_path)
    source = root / "features" / "scored_candidates.jsonl"
    candidates = _jsonl(source, "features/scored_candidates.jsonl")
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    by_game: dict[int, dict[str, object]] = {}
    for row in candidates:
        if any("market" in key.lower() or "outcome" in key.lower() for key in row):
            raise ValueError("post-lock fields are forbidden in feature artifacts")
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
    return lock_predictions(locked_rows, _stage(root, "locked"), model_hash)


def outcomes_stage(config_path: str | Path, data_dir: str | Path) -> Path:
    load_config(config_path)
    root = _safe_data_dir(data_dir)
    _, model_hash = _load_model_card(root, config_path)
    locked_manifest = root / "locked" / "lock_manifest.json"
    _regular_bytes(locked_manifest, "locked/lock_manifest.json")
    outcome_root = _stage(root, "outcomes")
    rows = _jsonl(outcome_root / "outcomes_input.jsonl", "outcomes/outcomes_input.jsonl")
    from .outcomes import join_outcomes

    with tempfile.TemporaryDirectory(dir=outcome_root) as temporary_dir:
        generated = join_outcomes(
            locked_manifest, rows, model_hash, Path(temporary_dir) / "evaluated"
        )
        generated_bytes = _regular_bytes(generated, "generated evaluated predictions")
    path = _write_once(
        outcome_root / "evaluated" / "evaluated_predictions.jsonl",
        generated_bytes,
        "evaluated predictions",
    )
    lock_mtime = locked_manifest.stat().st_mtime_ns
    outcome_mtime = path.stat().st_mtime_ns
    if outcome_mtime <= lock_mtime:
        newer = lock_mtime + 1
        os.utime(path, ns=(newer, newer), follow_symlinks=False)
    return path


def report_stage(config_path: str | Path, data_dir: str | Path) -> dict[str, object]:
    load_config(config_path)
    root = _safe_data_dir(data_dir)
    evaluated_path = root / "outcomes" / "evaluated" / "evaluated_predictions.jsonl"
    evaluated = _jsonl(evaluated_path, "evaluated predictions")
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
    _write_once(_stage(root, "reports") / "summary.json", _canonical_json(summary), "summary report")
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
    calibration_rows = _array_fixture(fixture / "calibration.json", "fixture calibration")
    game_rows = _array_fixture(fixture / "games.json", "fixture games")
    calibration = _stage(root, "calibration")
    features = _stage(root, "features")
    _write_once(calibration / "training_edges.jsonl", _canonical_jsonl(calibration_rows), "fixture calibration input")
    _write_once(features / "candidate_components.jsonl", _canonical_jsonl(game_rows), "fixture feature input")
    calibrate_stage(config_path, root)
    features_stage(config_path, root)
    lock_manifest = lock_stage(config_path, root)
    lock_path = root / "locked" / "lock_manifest.json"
    lock_mtime = lock_path.stat().st_mtime_ns

    outcome_rows = _array_fixture(fixture / "outcomes.json", "fixture outcomes")
    outcome_root = _stage(root, "outcomes")
    _write_once(outcome_root / "outcomes_input.jsonl", _canonical_jsonl(outcome_rows), "fixture outcome input")
    evaluated_path = outcomes_stage(config_path, root)
    summary = report_stage(config_path, root)
    prediction_rows = _jsonl(root / "locked" / "predictions.jsonl", "locked predictions")
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
        "lock_sha256": lock_manifest["sha256"],
        "locked_sha256": lock_manifest["sha256"],
        "summary": summary,
    }
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mlb_backtest.pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("fetch", "calibrate", "features", "lock", "outcomes", "report"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", required=True)
        subparser.add_argument("--data-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    commands = {
        "fetch": fetch_stage,
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
