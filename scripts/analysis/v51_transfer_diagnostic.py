#!/usr/bin/env python3
"""Recompute a retrospective v5.1 probability-transfer diagnostic.

This is deliberately an evaluation-only tool: it reads the sealed v5.1 rows,
verifies their pinned digests, and never fits or changes calibration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


EXPECTED = {
    "model_card": "9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759",
    "training_edges": "66cfe5db414c76c625de316556236a172ce0e5db71e28fefc390576438e41318",
    "locked": "bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb",
    "evaluated": "db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def wilson(wins: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.96
    p = wins / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    radius = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [centre - radius, centre + radius]


def scored(evaluated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in evaluated:
        if row["outcome"]["status"] != "Final":
            continue
        p = float(row["pick_probability"])
        if not 0.0 <= p <= 1.0:
            raise ValueError("pick_probability outside [0, 1]")
        side = row["predicted_side"]
        expected_probability = float(row["p_home"]) if side == "home" else 1.0 - float(row["p_home"])
        if side not in {"home", "away"} or not math.isclose(p, expected_probability, abs_tol=1e-10):
            raise ValueError("pick_probability is not the predicted-side probability")
        if row["predicted_winner"] != row[f"{side}_team"]:
            raise ValueError("predicted side and winner differ")
        output.append(dict(row, correct=row["predicted_winner"] == row["outcome"]["actual_winner"]))
    return output


def summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(items)
    wins = sum(item["correct"] for item in items)
    probabilities = [float(item["pick_probability"]) for item in items]
    return {
        "N": n,
        "W": wins,
        "L": n - wins,
        "accuracy": wins / n if n else None,
        "accuracy_wilson_95": wilson(wins, n),
        "mean_probability": sum(probabilities) / n if n else None,
        "brier": sum((p - int(item["correct"])) ** 2 for p, item in zip(probabilities, items)) / n if n else None,
        "log_loss": sum(-math.log(min(0.999, max(0.001, p)) if item["correct"] else 1.0 - min(0.999, max(0.001, p))) for p, item in zip(probabilities, items)) / n if n else None,
    }


def calibration_bins(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index in range(10):
        low, high = index / 10, (index + 1) / 10
        members = []
        for item in items:
            probability = float(item["pick_probability"])
            if low <= probability and (probability <= high if index == 9 else probability < high):
                members.append(item)
        wins = sum(item["correct"] for item in members)
        n = len(members)
        mean_p = sum(float(item["pick_probability"]) for item in members) / n if n else None
        observed = wins / n if n else None
        result.append({
            "bin": f"[{low:.1f}, {high:.1f}{']' if index == 9 else ')'}",
            "N": n,
            "mean_probability": mean_p,
            "W": wins,
            "observed_win_rate": observed,
            "observed_wilson_95": wilson(wins, n),
            "absolute_gap": abs(mean_p - observed) if n else None,
        })
    return result


def by_month(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[item["official_date"][:7]].append(item)
    return {month: summary(grouped[month]) for month in sorted(grouped)}


def check_sources(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    paths = {
        "model_card": root / "audit/v5_1_bt/model_card.json",
        "training_edges": root / "data/v5_1_bt/calibration/training_edges.jsonl",
        "locked": root / "data/v5_1_bt/locked/predictions.jsonl",
        "evaluated": root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl",
    }
    digests = {name: sha256(path) for name, path in paths.items()}
    if digests != EXPECTED:
        raise ValueError(f"source digest mismatch: {digests}")
    card = json.loads(paths["model_card"].read_text())
    if card["model_version"] != "v5.1-BT" or card["calibration"] != {
        "alpha": 0.10307467324665347,
        "beta": 0.6128874176132043,
    }:
        raise ValueError("unexpected frozen model card")
    training_edges = rows(paths["training_edges"])
    edge_dates = {row["official_date"] for row in training_edges}
    if (len(training_edges), min(edge_dates), max(edge_dates)) != (7265, "2022-04-08", "2024-09-30"):
        raise ValueError("training-edge population differs")
    if any(set(row) != {"game_pk", "official_date", "edge", "home_win"} for row in training_edges):
        raise ValueError("training-edge schema differs")
    locked, evaluated = rows(paths["locked"]), rows(paths["evaluated"])
    locked_by_key = {row["game_pk"]: row for row in locked}
    evaluated_by_key = {row["game_pk"]: row for row in evaluated}
    if len(locked) != len(locked_by_key) or len(evaluated) != len(evaluated_by_key):
        raise ValueError("duplicate game_pk")
    if set(locked_by_key) != set(evaluated_by_key) or len(locked) != 1340:
        raise ValueError("locked and evaluated populations differ")
    for key, locked_row in locked_by_key.items():
        evaluated_prediction = {name: value for name, value in evaluated_by_key[key].items() if name != "outcome"}
        if locked_row != evaluated_prediction:
            raise ValueError(f"locked prediction differs for game_pk {key}")
    return locked, evaluated, digests, card


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# v5.1 Retrospective Transfer Diagnostic",
        "",
        "This evaluation reads sealed v5.1 2025 rows; it does not fit calibration, set a threshold, or make a promotion claim.",
        "",
        "## Scope and orientation",
        "",
        "`pick_probability` is the probability of `predicted_side`/`predicted_winner` (home: `p_home`; away: `1-p_home`). `correct` is whether that predicted winner equals the final actual winner.",
        f"The frozen card values read and verified here are alpha={report['frozen_card']['alpha']:.16f}, beta={report['frozen_card']['beta']:.16f}; no fit is performed.",
        "The window is 2025-05-01 through 2025-08-12 over 100 selection dates; August is partial (12 dates), not a full month.",
        "",
        "## Population summaries",
        "",
        "| scope | N | W-L | accuracy | Wilson 95% | mean p | Brier | log loss |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for scope, values in report["scopes"].items():
        interval = values["accuracy_wilson_95"]
        lines.append(f"| {scope} | {values['N']} | {values['W']}-{values['L']} | {values['accuracy']:.6f} | {interval[0]:.6f}–{interval[1]:.6f} | {values['mean_probability']:.6f} | {values['brier']:.6f} | {values['log_loss']:.6f} |")
    for scope in ("all_final_locked_rows", "top1_final_rows"):
        lines.extend(["", f"## Fixed 0.1 probability bins — {scope}", "", "| bin | N | mean p | W | observed win rate | Wilson 95% | abs gap |", "| --- | ---: | ---: | ---: | ---: | --- | ---: |"])
        for item in report["fixed_bins"][scope]:
            interval = item["observed_wilson_95"]
            interval_text = "—" if interval is None else f"{interval[0]:.6f}–{interval[1]:.6f}"
            mean_p = "—" if item["mean_probability"] is None else f"{item['mean_probability']:.6f}"
            observed = "—" if item["observed_win_rate"] is None else f"{item['observed_win_rate']:.6f}"
            gap = "—" if item["absolute_gap"] is None else f"{item['absolute_gap']:.6f}"
            lines.append(f"| {item['bin']} | {item['N']} | {mean_p} | {item['W']} | {observed} | {interval_text} | {gap} |")
    for scope in ("all_final_locked_rows", "top1_final_rows"):
        lines.extend(["", f"## Monthly — {scope}", "", "| month | N | W-L | accuracy | mean p | Brier | log loss |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for month, values in report["monthly"][scope].items():
            lines.append(f"| {month} | {values['N']} | {values['W']}-{values['L']} | {values['accuracy']:.6f} | {values['mean_probability']:.6f} | {values['brier']:.6f} | {values['log_loss']:.6f} |")
    lines.extend(["", "## Integrity and caveats", "", f"- Locked/evaluated key equality and 1,340-row count: `{report['integrity']['locked_evaluated_key_match']}`.", f"- Existing Top1 primary-metric cross-check: `{report['integrity']['legacy_top1_primary_crosscheck']}`.", f"- Fixed-bin counts sum to their scopes: `{report['integrity']['bin_count_sums_match']}`; arithmetic sanity checks: `{report['integrity']['arithmetic_sanity_checks']}`.", "- Retrospective only: preserved source evidence uses reconstructed final lineups/actual starters, later provider revisions, and lacks an outcome-join execution timestamp. It is not a live T-45 validation.", "- Wilson intervals assume independent Bernoulli outcomes and ignore same-date/team dependence; they are descriptive uncertainty, not a performance guarantee.", "- The all-row result is a locked-slate diagnostic; the Top1 result is selection-conditioned. Neither is market comparison, profitability evidence, a new 60–70% threshold, or a license to change the frozen card.", "", "## Reproduce", "", "```sh", report["rerun_command"], "```", "", "## Source receipts", ""])
    for name, digest in report["source_digests"].items():
        lines.append(f"- `{report['source_paths'][name]}`: `{digest}`")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--md-out", type=Path, required=True)
    args = parser.parse_args()
    _, evaluated, digests, card = check_sources(args.source_root)
    final_rows = scored(evaluated)
    top1_rows = [row for row in final_rows if row["top1"] == "Yes"]
    dates = {row["official_date"] for row in final_rows}
    if len(final_rows) != 1340 or len(top1_rows) != 100 or len(dates) != 100:
        raise ValueError("unexpected final/top1/date counts")
    old = json.loads((args.source_root / "reports/v5_1_bt/diagnostic_100.json").read_text())
    current = summary(top1_rows)
    primary = old["primary"]
    crosscheck = all(math.isclose(current[key], primary[key], abs_tol=1e-12) for key in ("accuracy", "brier", "log_loss")) and all(current[key] == primary["record"][key] for key in ("W", "L", "N"))
    all_bins = calibration_bins(final_rows)
    top1_bins = calibration_bins(top1_rows)
    arithmetic_sanity = math.isclose((1.0 - 1.0) ** 2, 0.0) and math.isclose(
        -math.log(0.5), math.log(2.0)
    )
    report = {
        "schema": "V51_RETROSPECTIVE_TRANSFER_DIAGNOSTIC_V1",
        "method": "frozen chosen-side probabilities; no calibration refit or threshold selection",
        "official_date_range": [min(dates), max(dates)],
        "source_root": str(args.source_root),
        "source_paths": {
            "model_card": "audit/v5_1_bt/model_card.json",
            "training_edges": "data/v5_1_bt/calibration/training_edges.jsonl",
            "locked": "data/v5_1_bt/locked/predictions.jsonl",
            "evaluated": "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl",
        },
        "source_digests": digests,
        "frozen_card": {"alpha": card["calibration"]["alpha"], "beta": card["calibration"]["beta"]},
        "scopes": {"all_final_locked_rows": summary(final_rows), "top1_final_rows": current},
        "fixed_bins": {"all_final_locked_rows": all_bins, "top1_final_rows": top1_bins},
        "monthly": {"all_final_locked_rows": by_month(final_rows), "top1_final_rows": by_month(top1_rows)},
        "integrity": {
            "locked_evaluated_key_match": True,
            "legacy_top1_primary_crosscheck": crosscheck,
            "bin_count_sums_match": sum(item["N"] for item in all_bins) == len(final_rows) and sum(item["N"] for item in top1_bins) == len(top1_rows),
            "arithmetic_sanity_checks": arithmetic_sanity,
        },
        "rerun_command": "PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/v51_transfer_diagnostic.py --source-root '/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/mlb-v5-1-backtest' --json-out reports/model_readiness/2026-09-02/v51-transfer-diagnostic.json --md-out reports/model_readiness/2026-09-02/v51-transfer-diagnostic.md",
        "caveats": ["retrospective, not live T-45 validation", "Top1 is selection-conditioned", "Wilson ignores date/team dependence", "no market comparison, promotion claim, or new threshold"],
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.md_out.parent.mkdir(parents=True, exist_ok=True)
    args.md_out.write_text(markdown(report))


if __name__ == "__main__":
    main()
