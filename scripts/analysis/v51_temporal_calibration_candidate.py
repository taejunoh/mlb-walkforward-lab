#!/usr/bin/env python3
"""A sealed, retrospective-only temporal calibration candidate experiment.

This deliberately does not use the all-years v5.1 calibration stage.  It fits
normalization on 2022 raw features and calibration on rebuilt 2023 edges, then
seals all 2025 candidate predictions before opening any 2025 outcome evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from mlb_backtest.normalize import fit_frozen_calibration  # noqa: E402
from mlb_backtest.scoring import home_probability, predicted_pick  # noqa: E402
from mlb_backtest.scoring_prep import (  # noqa: E402
    build_training_edges,
    compute_normalization,
    score_raw_game,
)


LABEL = "RETROSPECTIVE_CANDIDATE_ONLY_NOT_FOR_LIVE_USE"


@dataclass(frozen=True)
class PreparePins:
    model_card: str
    frozen_inputs: str
    training_raw: str
    training_edges: str
    candidate_raw: str
    candidate_provenance: str


@dataclass(frozen=True)
class EvaluationPins:
    locked: str
    evaluated: str


DEFAULT_PREPARE_PINS = PreparePins(
    model_card="9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759",
    frozen_inputs="745448ff0fee0b1dbde02a4a43ad90fb8e36515611e6070d69d00ddf13c9e7b4",
    training_raw="7869a5379e7ec951d9233bd046528603bc5d7679f37ada02ee1cae9b2fe54dfc",
    training_edges="66cfe5db414c76c625de316556236a172ce0e5db71e28fefc390576438e41318",
    candidate_raw="a1817d4a7e50c77f35fbd03f3f79aa8e629e4b686de88b66c8c8d3877924d4a4",
    candidate_provenance="cdc2eeb8d58f63ea015b1bf05c77b770e18109e4285008d765c861ec2cbf3c3b",
)

DEFAULT_EVALUATION_PINS = EvaluationPins(
    locked="bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb",
    evaluated="db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76",
)


@dataclass(frozen=True)
class Counts:
    raw_2022: int = 2430
    eligible_2022: int = 2422
    raw_2023: int = 2430
    eligible_2023: int = 2415
    raw_2024: int = 2430
    candidate_2025: int = 1972


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


DEPENDENCY_PATHS = {
    "normalize.py": WORKSPACE_ROOT / "mlb_backtest/normalize.py",
    "scoring.py": WORKSPACE_ROOT / "mlb_backtest/scoring.py",
    "scoring_prep.py": WORKSPACE_ROOT / "mlb_backtest/scoring_prep.py",
}


def _dependency_hashes(paths: Mapping[str, Path] = DEPENDENCY_PATHS) -> dict[str, str]:
    if set(paths) != set(DEPENDENCY_PATHS):
        raise ValueError("scoring dependency path map differs")
    return {name: _sha(path) for name, path in sorted(paths.items())}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _year_rows_before_decode(path: Path, year: int) -> list[dict[str, Any]]:
    """Read only requested-date JSON records; excluded labels are never decoded."""
    marker = f'"official_date":"{year}-'.encode()
    return [json.loads(line) for line in path.read_bytes().splitlines() if marker in line]


def _write_unique(path: Path, content: bytes) -> str:
    if path.exists():
        raise FileExistsError(f"refusing overwrite of {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    seal = path.with_name(path.name + ".sha256")
    if seal.exists():
        raise FileExistsError(f"refusing overwrite of {seal}")
    seal.write_text(digest + "\n")
    return digest


def _verify_seal(path: Path, label: str) -> str:
    seal = path.with_name(path.name + ".sha256")
    if not path.is_file() or not seal.is_file():
        raise ValueError(f"{label} seal is missing")
    actual = _sha(path)
    if seal.read_text().strip() != actual:
        raise ValueError(f"{label} seal mismatch")
    return actual


def _prepare_source_paths(root: Path) -> dict[str, Path]:
    data = root / "data/v5_1_bt"
    return {
        "model_card": root / "audit/v5_1_bt/model_card.json",
        "pipeline_model_card": data / "calibration/model_card.json",
        "frozen_inputs": data / "calibration/frozen_inputs.json",
        "training_raw": data / "calibration/training_raw_features.jsonl",
        "training_edges": data / "calibration/training_edges.jsonl",
        "candidate_raw": data / "features/candidate_raw_features.jsonl",
        "candidate_provenance": data / "features/candidate_provenance.json",
    }


def _evaluation_source_paths(root: Path) -> dict[str, Path]:
    data = root / "data/v5_1_bt"
    return {
        "locked": data / "locked/predictions.jsonl",
        "evaluated": data / "outcomes/evaluated/evaluated_predictions.jsonl",
    }


def _verify_prepare_sources(root: Path, pins: PreparePins) -> tuple[dict[str, Path], dict[str, str]]:
    paths = _prepare_source_paths(root)
    expected = {name: getattr(pins, name) for name in PreparePins.__dataclass_fields__}
    observed = {name: _sha(paths[name]) for name in expected}
    if observed != expected:
        raise ValueError(f"prepare source digest mismatch: {observed}")
    card = _json(paths["model_card"])
    pipeline = _json(paths["pipeline_model_card"])
    if card.get("model_version") != "v5.1-BT" or pipeline.get("model_version") != "v5.1-BT":
        raise ValueError("unexpected v5.1 model card")
    referenced = card.get("sha256", {}).get("pipeline_model_card_file")
    if referenced != _sha(paths["pipeline_model_card"]):
        raise ValueError("audit card does not bind pipeline model card")
    provenance = _json(paths["candidate_provenance"])
    if provenance.get("candidate_raw_sha256") != observed["candidate_raw"]:
        raise ValueError("candidate provenance does not bind raw features")
    return paths, observed


def _require_unique_dates(rows: Iterable[Mapping[str, Any]], label: str) -> None:
    ids = [row.get("game_pk") for row in rows]
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
        raise ValueError(f"{label} has an invalid game_pk")
    if len(ids) != len(set(ids)):
        raise ValueError(f"{label} has duplicate game_pk")


def _split_training(raw_rows: list[dict[str, Any]], counts: Counts) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        day = row.get("official_date")
        if not isinstance(day, str) or len(day) != 10:
            raise ValueError("training raw official_date is invalid")
        groups[day[:4]].append(row)
    required = {"2022": counts.raw_2022, "2023": counts.raw_2023, "2024": counts.raw_2024}
    if {year: len(groups[year]) for year in required} != required or set(groups) != set(required):
        raise ValueError("training raw yearly populations differ")
    raw22, raw23 = groups["2022"], groups["2023"]
    if sum(row.get("eligible") is True for row in raw22) != counts.eligible_2022:
        raise ValueError("2022 eligible population differs")
    if sum(row.get("eligible") is True for row in raw23) != counts.eligible_2023:
        raise ValueError("2023 eligible population differs")
    _require_unique_dates(raw22, "2022 raw")
    _require_unique_dates(raw23, "2023 raw")
    return raw22, raw23


def _prediction_rows(raw25: list[dict[str, Any]], normalization: Mapping[str, Any], alpha: float, beta: float) -> list[dict[str, Any]]:
    predictions = []
    for raw in sorted(raw25, key=lambda item: (item["official_date"], item["game_pk"])):
        scored = score_raw_game(raw, normalization)
        edge = float(scored["edge"])
        p_home = home_probability(edge, alpha, beta)
        pick = predicted_pick("HOME", "AWAY", p_home)
        if pick["predicted_side"] not in {"home", "away"}:
            raise ValueError("candidate prediction has no side")
        if not all(math.isfinite(float(value)) for value in (edge, p_home, pick["pick_probability"])):
            raise ValueError("candidate prediction is non-finite")
        predictions.append({
            "game_pk": raw["game_pk"], "official_date": raw["official_date"], "edge": edge,
            "p_home": p_home, "predicted_side": pick["predicted_side"], "pick_probability": pick["pick_probability"],
        })
    return predictions


def prepare(
    root: Path,
    output_dir: Path,
    *,
    pins: PreparePins = DEFAULT_PREPARE_PINS,
    counts: Counts = Counts(),
    dependency_paths: Mapping[str, Path] = DEPENDENCY_PATHS,
) -> dict[str, Any]:
    """Seal all candidate scores without reading locked predictions or outcomes."""
    paths, source_sha = _verify_prepare_sources(root, pins)
    raw_training = _jsonl(paths["training_raw"])
    raw22, raw23 = _split_training(raw_training, counts)
    normalization = compute_normalization(raw22)
    edges23_source = _year_rows_before_decode(paths["training_edges"], 2023)
    if any(set(row) != {"game_pk", "official_date", "edge", "home_win"} for row in edges23_source):
        raise ValueError("2023 outcome authority schema differs")
    labels23 = {row["game_pk"]: row["home_win"] for row in edges23_source}
    if len(labels23) != len(edges23_source):
        raise ValueError("2023 outcome authority has duplicate game_pk")
    edges23 = build_training_edges(raw23, normalization, labels23)
    if len(edges23) != counts.eligible_2023 or set(labels23) != {row["game_pk"] for row in edges23}:
        raise ValueError("2023 raw/outcome key coverage differs")
    alpha, beta, metadata = fit_frozen_calibration(edges23, training_start="2023-01-01", training_end="2023-12-31")
    raw25 = _jsonl(paths["candidate_raw"])
    if len(raw25) != counts.candidate_2025 or any(row.get("eligible") is not True for row in raw25):
        raise ValueError("2025 eligible candidate population differs")
    if any(not "2025-05-01" <= row.get("official_date", "") <= "2025-09-28" for row in raw25):
        raise ValueError("2025 candidate dates differ")
    _require_unique_dates(raw25, "2025 candidate raw")
    predictions = _prediction_rows(raw25, normalization, alpha, beta)
    prediction_bytes = b"".join(
        json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n" for row in predictions
    )
    prediction_sha = _write_unique(output_dir / "candidate_predictions.jsonl", prediction_bytes)
    receipt = {
        "schema": "V51_TEMPORAL_CALIBRATION_CANDIDATE_PREPARE_V1", "label": LABEL,
        "promotion": "NO_PROMOTION", "execution_utc": _utc_now(), "source_root": str(root), "source_sha256": source_sha,
        "local_scoring_code_sha256": _sha(Path(__file__)),
        "scoring_dependency_sha256": _dependency_hashes(dependency_paths),
        "normalization": {"training_year": 2022, "raw_count": len(raw22), "eligible_count": counts.eligible_2022, "value": normalization},
        "calibration": {"training_year": 2023, "training_start": "2023-01-01", "training_end": "2023-12-31", "training_count": metadata["training_count"], "alpha": alpha, "beta": beta, "edge_sha256": hashlib.sha256(b"".join(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n" for row in edges23)).hexdigest()},
        "candidate": {"date_start": "2025-05-01", "date_end": "2025-09-28", "count": len(predictions), "prediction_sha256": prediction_sha},
        "excluded": ["2024 raw features", "2024 labels", "all 2025 outcomes during prepare"],
        "caveats": ["2025 already seen", "reconstructed final lineups are not pristine/live T-45", "candidate changes 2022-only normalization and 2023 calibration together; it is not an isolated alpha/beta treatment effect", "not independently validated, profitable, or promotable"],
    }
    _write_unique(output_dir / "prepare_receipt.json", (json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
    return receipt


def _wilson(wins: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.96
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [centre - radius, centre + radius]


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    wins = sum(row["correct"] for row in rows)
    p_home = [float(row["p_home"]) for row in rows]
    home_win = [int(row["home_win"]) for row in rows]
    return {"N": n, "W": wins, "L": n - wins, "accuracy": wins / n if n else None, "accuracy_wilson_95": _wilson(wins, n), "mean_pick_probability": sum(row["pick_probability"] for row in rows) / n if n else None, "brier": sum((p - y) ** 2 for p, y in zip(p_home, home_win)) / n if n else None, "log_loss": sum(-(y * math.log(min(.999, max(.001, p))) + (1-y) * math.log(1 - min(.999, max(.001, p)))) for p, y in zip(p_home, home_win)) / n if n else None}


def _bins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index in range(10):
        low, high = index / 10, (index + 1) / 10
        members = [row for row in rows if low <= row["pick_probability"] and (row["pick_probability"] < high or index == 9)]
        n, wins = len(members), sum(row["correct"] for row in members)
        mean_p = sum(row["pick_probability"] for row in members) / n if n else None
        observed = wins / n if n else None
        result.append({"bin": f"[{low:.1f}, {high:.1f}{']' if index == 9 else ')'}", "N": n, "W": wins, "mean_probability": mean_p, "observed_win_rate": observed, "observed_wilson_95": _wilson(wins, n), "absolute_gap": abs(mean_p - observed) if n else None})
    return result


def _monthly(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["official_date"][:7]].append(row)
    return {month: _metrics(groups[month]) for month in sorted(groups)}


def _verify_prepared(
    prepared_dir: Path, dependency_paths: Mapping[str, Path] = DEPENDENCY_PATHS
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    receipt_path = prepared_dir / "prepare_receipt.json"
    predictions_path = prepared_dir / "candidate_predictions.jsonl"
    _verify_seal(receipt_path, "prepare receipt")
    prediction_sha = _verify_seal(predictions_path, "prediction")
    receipt = _json(receipt_path)
    if receipt.get("label") != LABEL or receipt.get("promotion") != "NO_PROMOTION":
        raise ValueError("prepare receipt is not retrospective-only")
    if receipt.get("candidate", {}).get("prediction_sha256") != prediction_sha:
        raise ValueError("prediction seal is not bound by prepare receipt")
    if receipt.get("local_scoring_code_sha256") != _sha(Path(__file__)):
        raise ValueError("runner code hash mismatch")
    if receipt.get("scoring_dependency_sha256") != _dependency_hashes(dependency_paths):
        raise ValueError("scoring dependency hash mismatch")
    predictions = _jsonl(predictions_path)
    _require_unique_dates(predictions, "candidate predictions")
    if len(predictions) != receipt["candidate"]["count"]:
        raise ValueError("candidate prediction count differs from receipt")
    return receipt, predictions


def evaluate(
    root: Path,
    prepared_dir: Path,
    output_dir: Path,
    *,
    pins: EvaluationPins = DEFAULT_EVALUATION_PINS,
    fixed_population: int = 1340,
    dependency_paths: Mapping[str, Path] = DEPENDENCY_PATHS,
) -> dict[str, Any]:
    """Evaluate sealed candidates on the fixed 1,340-row locked 2025 population."""
    receipt, predictions = _verify_prepared(prepared_dir, dependency_paths)
    paths = _evaluation_source_paths(root)
    if _sha(paths["locked"]) != pins.locked or _sha(paths["evaluated"]) != pins.evaluated:
        raise ValueError("pinned locked/evaluated source digest mismatch")
    locked, evaluated = _jsonl(paths["locked"]), _jsonl(paths["evaluated"])
    locked_by_key = {row.get("game_pk"): row for row in locked}
    evaluated_by_key = {row.get("game_pk"): row for row in evaluated}
    candidate_by_key = {row["game_pk"]: row for row in predictions}
    if len(locked) != len(locked_by_key) or len(evaluated) != len(evaluated_by_key):
        raise ValueError("locked/evaluated duplicate game_pk")
    if len(locked) != fixed_population or set(locked_by_key) != set(evaluated_by_key) or not set(locked_by_key) <= set(candidate_by_key):
        raise ValueError("locked/evaluated keysets must match and candidates must cover every locked key")
    candidate_rows, baseline_rows = [], []
    for game_pk in sorted(locked_by_key):
        locked_row, evaluated_row, candidate = locked_by_key[game_pk], evaluated_by_key[game_pk], candidate_by_key[game_pk]
        if {key: value for key, value in evaluated_row.items() if key != "outcome"} != locked_row:
            raise ValueError(f"evaluated prediction differs from locked row {game_pk}")
        outcome = evaluated_row.get("outcome", {})
        if outcome.get("status") != "Final":
            raise ValueError("fixed population includes non-final outcome")
        actual_winner = outcome.get("actual_winner")
        home_team = locked_row.get("home_team")
        away_team = locked_row.get("away_team")
        if (
            not isinstance(actual_winner, str)
            or not actual_winner
            or actual_winner not in {home_team, away_team}
        ):
            raise ValueError(
                f"actual_winner {actual_winner!r} does not match locked teams for game_pk {game_pk}"
            )
        home_win = int(actual_winner == home_team)
        for row, p_home, side, pick_p in ((candidate, candidate["p_home"], candidate["predicted_side"], candidate["pick_probability"]), (locked_row, locked_row["p_home"], locked_row["predicted_side"], locked_row["pick_probability"])):
            if side not in {"home", "away"} or not all(math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0 for value in (p_home, pick_p)):
                raise ValueError("prediction probability or side is invalid")
        correct_candidate = bool(home_win) if candidate["predicted_side"] == "home" else not bool(home_win)
        correct_baseline = locked_row.get("predicted_winner") == actual_winner
        candidate_rows.append({**candidate, "home_win": home_win, "correct": correct_candidate})
        baseline_rows.append({"official_date": locked_row["official_date"], "p_home": float(locked_row["p_home"]), "pick_probability": float(locked_row["pick_probability"]), "home_win": home_win, "correct": correct_baseline})
    baseline = _metrics(baseline_rows)
    if fixed_population == 1340 and not (math.isclose(baseline["brier"], .24627075738049958, abs_tol=1e-12) and math.isclose(baseline["log_loss"], .6855527297095908, abs_tol=1e-12)):
        raise ValueError("pinned baseline metric cross-check failed")
    candidate = _metrics(candidate_rows)
    report = {"schema": "V51_TEMPORAL_CALIBRATION_CANDIDATE_EVALUATION_V1", "label": LABEL, "promotion": "NO_PROMOTION", "execution_utc": _utc_now(), "scope": "fixed locked 2025 population; no reranking or threshold selection", "prepare_receipt_sha256": _sha(prepared_dir / "prepare_receipt.json"), "candidate": {**candidate, "fixed_bins": _bins(candidate_rows), "monthly": _monthly(candidate_rows)}, "baseline": {**baseline, "fixed_bins": _bins(baseline_rows), "monthly": _monthly(baseline_rows)}, "integrity": {"fixed_population": fixed_population, "exact_locked_evaluated_keysets": True, "candidate_coverage": True, "fixed_bin_count_sums": sum(item["N"] for item in _bins(candidate_rows)) == fixed_population}, "caveats": ["2025 already seen", "reconstructed final lineups are not pristine/live T-45", "candidate changes 2022-only normalization and 2023 calibration together; it is not an isolated alpha/beta treatment effect", "Wilson intervals are descriptive only", "not independently validated, profitable, or promotable"]}
    _write_unique(output_dir / "evaluation_report.json", (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("prepare", "evaluate"))
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path)
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.source_root, args.output_dir)
    else:
        if args.prepared_dir is None:
            parser.error("evaluate requires --prepared-dir")
        evaluate(args.source_root, args.prepared_dir, args.output_dir)


if __name__ == "__main__":
    main()
