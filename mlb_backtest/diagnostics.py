from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from types import MappingProxyType
import hashlib
import json
import math
import statistics

from mlb_backtest.metrics import evaluate, wilson
from scipy.stats import fisher_exact, spearmanr


JsonScalar = str | int | float | bool | None
FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]


def deep_freeze(value: object) -> FrozenJson:
    if isinstance(value, dict):
        return MappingProxyType({str(k): deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(v) for v in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported JSON value {type(value).__name__}")


AUDITED_DIGEST_PATHS = MappingProxyType(
    {
        "predictions_contents_sha256": "data/v5_1_bt/locked/predictions.jsonl",
        "lock_manifest_file_sha256": "data/v5_1_bt/locked/lock_manifest.json",
        "source_provenance_file_sha256": "data/v5_1_bt/locked/source_provenance.json",
        "audit_model_card_file_sha256": "audit/v5_1_bt/model_card.json",
        "candidate_raw_features": "data/v5_1_bt/features/candidate_raw_features.jsonl",
        "candidate_components": "data/v5_1_bt/features/candidate_components.jsonl",
        "candidate_provenance": "data/v5_1_bt/features/candidate_provenance.json",
        "scored_candidates": "data/v5_1_bt/features/scored_candidates.jsonl",
        "scored_candidate_provenance": "data/v5_1_bt/features/scored_candidate_provenance.json",
        "outcomes_input": "data/v5_1_bt/outcomes/outcomes_input.jsonl",
        "baseline": "data/v5_1_bt/outcomes/baseline.json",
        "evaluated_predictions": "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl",
        "summary": "data/v5_1_bt/reports/summary.json",
    }
)

SEALED_LOCK_DIGEST_KEYS = frozenset(
    {
        "predictions_contents_sha256",
        "lock_manifest_file_sha256",
        "source_provenance_file_sha256",
    }
)
SEALED_LOCK_METADATA_KEYS = frozenset(
    {
        "created_at_utc",
        "predictions_file",
        "row_count",
        "date_count",
        "first_official_date",
        "last_official_date",
        "top1_count",
        "top3_count",
        "pipeline_model_card_sha256",
    }
)
INPUT_OUTPUT_DIGEST_KEYS = frozenset(AUDITED_DIGEST_PATHS) - (
    SEALED_LOCK_DIGEST_KEYS | {"audit_model_card_file_sha256"}
)


@dataclass(frozen=True)
class V51Evidence:
    root: Path
    evaluated_rows: tuple[Mapping[str, FrozenJson], ...]
    baseline_by_game: Mapping[int, str]
    summary: Mapping[str, FrozenJson]
    lock_audit: Mapping[str, FrozenJson]
    model_card: Mapping[str, FrozenJson]
    candidate_rows: tuple[Mapping[str, FrozenJson], ...]
    locked_prediction_rows: tuple[Mapping[str, FrozenJson], ...]
    manual_audit_text: str
    verified_file_sha256: Mapping[str, str]
    source_file_sha256: Mapping[str, str]
    predictions_content_sha256: str
    evaluated_content_sha256: str


def exact_mcnemar(model_only: int, baseline_only: int) -> float:
    _require_nonnegative_count("model_only", model_only)
    _require_nonnegative_count("baseline_only", baseline_only)
    discordant = model_only + baseline_only
    if discordant == 0:
        return 1.0
    tail = sum(
        math.comb(discordant, k)
        for k in range(min(model_only, baseline_only) + 1)
    ) / (2**discordant)
    return min(1.0, 2.0 * tail)


def paired_table(
    model_correct: Sequence[bool], baseline_correct: Sequence[bool]
) -> dict[str, int]:
    if len(model_correct) != len(baseline_correct):
        raise ValueError("paired correctness vectors differ in length")
    if any(type(value) is not bool for value in model_correct):
        raise ValueError("model correctness vector must contain only bool values")
    if any(type(value) is not bool for value in baseline_correct):
        raise ValueError("baseline correctness vector must contain only bool values")
    pairs = list(zip(model_correct, baseline_correct, strict=True))
    return {
        "both_correct": sum(a and b for a, b in pairs),
        "model_only": sum(a and not b for a, b in pairs),
        "baseline_only": sum(not a and b for a, b in pairs),
        "both_wrong": sum(not a and not b for a, b in pairs),
        "N": len(pairs),
    }


PAIRED_INTERVAL_LABEL = "approximate paired 95% interval (z=1.97)"


def paired_delta_interval(
    model_only: int, baseline_only: int, n: int, z: float = 1.97
) -> tuple[float, float]:
    _require_nonnegative_count("model_only", model_only)
    _require_nonnegative_count("baseline_only", baseline_only)
    if type(n) is not int or n <= 0:
        raise ValueError("n must be a positive integer")
    if model_only + baseline_only > n:
        raise ValueError("discordant count cannot exceed n")
    if (
        isinstance(z, bool)
        or not isinstance(z, (int, float))
        or not math.isfinite(z)
        or z <= 0
    ):
        raise ValueError("z must be a finite positive number")
    delta = (model_only - baseline_only) / n
    variance = ((model_only + baseline_only) / n - delta * delta) / n
    margin = z * math.sqrt(variance)
    return delta - margin, delta + margin


def _require_nonnegative_count(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> tuple[dict[str, object], ...]:
    return _read_jsonl_bytes(path.read_bytes())


def _read_jsonl_bytes(contents: bytes) -> tuple[dict[str, object], ...]:
    return tuple(
        json.loads(line)
        for line in contents.decode("utf-8").splitlines()
        if line.strip()
    )


def audited_digest_map(audit: Mapping[str, object]) -> dict[str, str]:
    try:
        model_card_digest = audit["audit_model_card_file_sha256"]
        sealed_lock = audit["sealed_lock"]
        input_and_output = audit["input_and_output_sha256"]
    except KeyError as exc:
        raise ValueError("audited digest registry is missing a required section") from exc
    if not isinstance(sealed_lock, Mapping) or not isinstance(input_and_output, Mapping):
        raise ValueError("audited digest registry has invalid structure")
    duplicate_digest_keys = (
        set(sealed_lock) & set(input_and_output) & set(AUDITED_DIGEST_PATHS)
    )
    if duplicate_digest_keys:
        raise ValueError("audited digest key appears in more than one section")
    expected_sealed_keys = SEALED_LOCK_METADATA_KEYS | SEALED_LOCK_DIGEST_KEYS
    if set(sealed_lock) != expected_sealed_keys:
        raise ValueError("sealed lock schema differs from the audited contract")
    if set(input_and_output) != INPUT_OUTPUT_DIGEST_KEYS:
        raise ValueError("input/output digest schema differs from the audited contract")
    result = {"audit_model_card_file_sha256": model_card_digest}
    result.update({key: sealed_lock[key] for key in SEALED_LOCK_DIGEST_KEYS})
    result.update({key: input_and_output[key] for key in INPUT_OUTPUT_DIGEST_KEYS})
    if set(result) != set(AUDITED_DIGEST_PATHS):
        raise ValueError("audited digest registry differs from available files")
    if any(not isinstance(value, str) for value in result.values()):
        raise ValueError("audited digest values must be strings")
    return result


def load_v51_evidence(root: Path) -> V51Evidence:
    root = root.resolve()
    audit_path = "audit/v5_1_bt/lock_manifest.json"
    audit_bytes = (root / audit_path).read_bytes()
    audit = json.loads(audit_bytes)
    expected = audited_digest_map(audit)
    captured = {
        name: (root / relative).read_bytes()
        for name, relative in AUDITED_DIGEST_PATHS.items()
    }
    verified = {
        name: hashlib.sha256(captured[name]).hexdigest()
        for name in AUDITED_DIGEST_PATHS
    }
    if verified != expected:
        raise ValueError("authoritative v5.1 evidence hash differs")
    rows = _read_jsonl_bytes(captured["evaluated_predictions"])
    if (
        len(rows) != 1340
        or len({row["game_pk"] for row in rows}) != 1340
        or {row["model_version"] for row in rows} != {"v5.1-BT"}
    ):
        raise ValueError("evaluated v5.1 population differs")
    top1_dates = [row["official_date"] for row in rows if row["top1"] == "Yes"]
    if len(top1_dates) != 100 or len(set(top1_dates)) != 100:
        raise ValueError("Daily Top 1 population differs")
    baseline_raw = json.loads(captured["baseline"])
    baseline = MappingProxyType(
        {int(game_pk): str(team) for game_pk, team in baseline_raw.items()}
    )
    model_card = json.loads(captured["audit_model_card_file_sha256"])
    extra_paths = {
        audit_path: audit_bytes,
        "audit/v5_1_bt/manual_audit.md": (
            root / "audit/v5_1_bt/manual_audit.md"
        ).read_bytes(),
        "config/v5_1_bt.json": (root / "config/v5_1_bt.json").read_bytes(),
    }
    if (
        hashlib.sha256(extra_paths["config/v5_1_bt.json"]).hexdigest()
        != model_card["sha256"]["config_file"]
    ):
        raise ValueError("authoritative v5.1 config hash differs")
    relative_to_key = {relative: name for name, relative in AUDITED_DIGEST_PATHS.items()}
    source_paths = (
        "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl",
        "data/v5_1_bt/reports/summary.json",
        "data/v5_1_bt/outcomes/baseline.json",
        "data/v5_1_bt/locked/predictions.jsonl",
        "data/v5_1_bt/locked/lock_manifest.json",
        audit_path,
        "audit/v5_1_bt/model_card.json",
        "audit/v5_1_bt/manual_audit.md",
        "config/v5_1_bt.json",
        "data/v5_1_bt/features/candidate_raw_features.jsonl",
    )
    source_digests = {}
    for relative in source_paths:
        contents = (
            captured[relative_to_key[relative]]
            if relative in relative_to_key
            else extra_paths[relative]
        )
        source_digests[relative] = hashlib.sha256(contents).hexdigest()
    return V51Evidence(
        root,
        tuple(deep_freeze(row) for row in rows),
        baseline,
        deep_freeze(
            json.loads(captured["summary"])
        ),
        deep_freeze(audit),
        deep_freeze(model_card),
        tuple(
            deep_freeze(row)
            for row in _read_jsonl_bytes(captured["candidate_raw_features"])
        ),
        tuple(
            deep_freeze(row)
            for row in _read_jsonl_bytes(captured["predictions_contents_sha256"])
        ),
        extra_paths["audit/v5_1_bt/manual_audit.md"].decode("utf-8"),
        MappingProxyType(verified),
        MappingProxyType(source_digests),
        verified["predictions_contents_sha256"],
        verified["evaluated_predictions"],
    )


def contract_table(
    rows: object,
    population: str,
    formula: str,
    exclusions: Sequence[str],
    source_ids: Sequence[str],
) -> dict[str, object]:
    return {
        "rows": rows,
        "population": population,
        "formula": formula,
        "exclusions": list(exclusions),
        "source_ids": list(source_ids),
    }


def graded_rows(
    rows: Sequence[Mapping[str, object]], *, top1: bool = False
) -> list[dict[str, object]]:
    return [
        {
            **row,
            "correct": row["predicted_winner"] == row["outcome"]["actual_winner"],
        }
        for row in rows
        if row["outcome"]["status"] == "Final"
        and (not top1 or row["top1"] == "Yes")
    ]


def paired_result(
    rows: Sequence[Mapping[str, object]], picks: Mapping[int, str]
) -> dict[str, object]:
    paired = [
        (
            row["predicted_winner"] == row["outcome"]["actual_winner"],
            picks[row["game_pk"]] == row["outcome"]["actual_winner"],
        )
        for row in rows
        if row["game_pk"] in picks
    ]
    table = paired_table([a for a, _ in paired], [b for _, b in paired])
    model_w = sum(a for a, _ in paired)
    baseline_w = sum(b for _, b in paired)
    n = len(paired)
    return {
        "paired": table,
        "model_accuracy": model_w / n,
        "baseline_accuracy": baseline_w / n,
        "delta": (model_w - baseline_w) / n,
        "paired_delta_interval": list(
            paired_delta_interval(table["model_only"], table["baseline_only"], n)
        ),
        "interval_label": PAIRED_INTERVAL_LABEL,
        "mcnemar_p": exact_mcnemar(
            table["model_only"], table["baseline_only"]
        ),
    }


def baseline_diagnostics(
    rows: Sequence[Mapping[str, object]], season: Mapping[int, str]
) -> dict[str, object]:
    home = {row["game_pk"]: row["home_team"] for row in rows}
    return {
        "home": paired_result(rows, home),
        "season_record": paired_result(rows, season),
        "market": {
            "N": 0,
            "model_accuracy": None,
            "market_accuracy": None,
            "delta": None,
            "roi": None,
            "clv": None,
        },
    }


def rank_diagnostic(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "rank": rank,
            "W": sum(
                bool(row["correct"]) for row in rows if row["daily_rank"] == rank
            ),
            "N": sum(row["daily_rank"] == rank for row in rows),
        }
        for rank in (1, 2, 3)
    ]


def compact_record(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    wins = sum(bool(row["correct"]) for row in rows)
    n = len(rows)
    return {
        "record": {"W": wins, "L": n - wins, "N": n},
        "accuracy": wins / n if n else None,
    }


def month_diagnostic(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    return {
        month: compact_record(
            [row for row in rows if str(row["official_date"]).startswith(month)]
        )
        for month in ("2025-05", "2025-06", "2025-07", "2025-08")
    }


def side_diagnostic(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result = {}
    for side in ("home", "away"):
        compact = compact_record(
            [row for row in rows if row["predicted_side"] == side]
        )
        result[side] = compact["record"] | {"accuracy": compact["accuracy"]}
    return result


def team_diagnostic(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result = {}
    for team in sorted({str(row["predicted_winner"]) for row in rows}):
        values = compact_record(
            [row for row in rows if row["predicted_winner"] == team]
        )
        record = values["record"]
        result[team] = {
            **record,
            "accuracy": values["accuracy"],
            "wilson_95": list(wilson(record["W"], record["N"])),
        }
    return result


def team_concentration(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    counts = Counter(str(row["predicted_winner"]) for row in rows)
    top = counts.most_common(6)
    return {
        "distinct_teams": len(counts),
        "top_six": dict(top),
        "top_six_share": sum(count for _, count in top) / len(rows),
    }


def chosen_side_edge(row: Mapping[str, object], component: str) -> float:
    side_sign = 1.0 if row["predicted_side"] == "home" else -1.0
    return float(row[f"{component}_edge"]) * side_sign


def finite_pairs(
    xs: Sequence[float], ys: Sequence[float]
) -> tuple[list[float], list[float]]:
    if len(xs) != len(ys) or not xs or any(
        not math.isfinite(value) for value in (*xs, *ys)
    ):
        raise ValueError("statistic inputs must be nonempty, paired, and finite")
    return list(xs), list(ys)


def spearman(xs: Sequence[float], ys: Sequence[float]) -> dict[str, float]:
    x, y = finite_pairs(xs, ys)
    result = spearmanr(x, y)
    statistic = float(result.statistic)
    pvalue = float(result.pvalue)
    if not math.isfinite(statistic) or not math.isfinite(pvalue):
        raise ValueError("nonfinite Spearman result")
    return {"rho": statistic, "p": pvalue}


def fisher(table: Sequence[Sequence[int]]) -> float:
    if len(table) != 2 or any(len(row) != 2 for row in table):
        raise ValueError("Fisher table must be 2x2")
    pvalue = float(fisher_exact(table, alternative="two-sided").pvalue)
    if not math.isfinite(pvalue):
        raise ValueError("nonfinite Fisher result")
    return pvalue


def component_summary(
    rows: Sequence[Mapping[str, object]], name: str
) -> dict[str, object]:
    wins = [chosen_side_edge(row, name) for row in rows if row["correct"]]
    losses = [chosen_side_edge(row, name) for row in rows if not row["correct"]]
    return {
        "win_mean": statistics.fmean(wins),
        "loss_mean": statistics.fmean(losses),
    }


def bullpen_halves(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    ordered = sorted(
        rows,
        key=lambda row: (
            abs(chosen_side_edge(row, "bullpen")),
            int(row["game_pk"]),
        ),
    )
    if len(ordered) != 100:
        raise ValueError("bullpen-half diagnostic requires 100 Top 1 rows")
    result = {}
    for label, part in (("lower", ordered[:50]), ("upper", ordered[50:])):
        wins = sum(bool(row["correct"]) for row in part)
        result[label] = {"W": wins, "N": len(part), "accuracy": wins / len(part)}
    return result


def component_diagnostic(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result = {
        name: component_summary(rows, name)
        for name in ("starter", "offense", "bullpen", "schedule")
    }
    bullpen_edges = [chosen_side_edge(row, "bullpen") for row in rows]
    result["bullpen"] |= {
        "absolute_edge_halves": bullpen_halves(rows),
        "spearman": spearman(
            bullpen_edges, [float(bool(row["correct"])) for row in rows]
        ),
    }
    return result


def discrimination(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    outcomes = [float(bool(row["correct"])) for row in rows]
    return {
        "probability_spearman": spearman(
            [float(row["pick_probability"]) for row in rows], outcomes
        ),
        "legacy_confidence_spearman": spearman(
            [float(row["confidence"]) for row in rows], outcomes
        ),
    }


def threshold_results(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    result = {}
    for label, threshold in (("ge_63", 0.63), ("ge_68", 0.68)):
        selected = [
            row for row in rows if float(row["pick_probability"]) >= threshold
        ]
        wins = sum(bool(row["correct"]) for row in selected)
        result[label] = {
            "W": wins,
            "N": len(selected),
            "accuracy": wins / len(selected),
            "post_hoc": True,
        }
    return result


def rank_comparisons(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    dates = sorted({str(row["official_date"]) for row in rows})
    by_date = {
        day: {
            int(row["daily_rank"]): bool(row["correct"])
            for row in rows
            if row["official_date"] == day and int(row["daily_rank"]) <= 3
        }
        for day in dates
    }
    result = []
    for other in (2, 3):
        pairs = [(daily[1], daily[other]) for daily in by_date.values()]
        table = paired_table([x for x, _ in pairs], [y for _, y in pairs])
        result.append(
            {
                "rank1_vs": other,
                "paired": table,
                "p": exact_mcnemar(
                    table["model_only"], table["baseline_only"]
                ),
            }
        )
    return result


def temporal_diagnostic(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    july = [
        row for row in rows if str(row["official_date"]).startswith("2025-07")
    ]
    other = [
        row for row in rows if not str(row["official_date"]).startswith("2025-07")
    ]
    return {
        "july_vs_other_fisher_p": fisher(
            [
                [
                    sum(bool(row["correct"]) for row in july),
                    sum(not bool(row["correct"]) for row in july),
                ],
                [
                    sum(bool(row["correct"]) for row in other),
                    sum(not bool(row["correct"]) for row in other),
                ],
            ]
        )
    }


def calibration_diagnostic(calibration: Mapping[str, object]) -> dict[str, object]:
    source_bins = calibration["bins"]
    total = sum(int(row["N"]) for row in source_bins)
    if total <= 0:
        raise ValueError("calibration diagnostic requires graded rows")
    bins = []
    for source in source_bins:
        mean_brier = source["brier_contribution"]
        contribution = (
            float(mean_brier) * int(source["N"]) / total
            if mean_brier is not None
            else 0.0
        )
        bins.append(
            {
                **source,
                "mean_brier": mean_brier,
                "brier_contribution": contribution,
            }
        )
    return {"bins": bins, "ece": calibration["ece"]}


def quality_diagnostic(
    rows: Sequence[Mapping[str, object]],
    all_candidates: Sequence[Mapping[str, object]],
    card: Mapping[str, object],
) -> dict[str, object]:
    completeness = [float(row["overall_completeness"]) for row in rows]
    all_candidate_states = {
        "N": len(all_candidates),
        "INCOMPLETE": sum(
            row["data_status"] == "INCOMPLETE" for row in all_candidates
        ),
        "High": sum(row["uncertainty"] == "High" for row in all_candidates),
        "Toss-up": sum(row["tier"] == "Toss-up" for row in all_candidates),
    }
    neutral_components = {}
    for component in ("defense", "environment"):
        neutral_components[component] = {
            "N": len(all_candidates),
            "away_score_50": sum(
                float(row[f"away_{component}_score"]) == 50.0
                for row in all_candidates
            ),
            "home_score_50": sum(
                float(row[f"home_{component}_score"]) == 50.0
                for row in all_candidates
            ),
            "quality_zero": sum(
                float(row[f"{component}_quality"]) == 0.0
                for row in all_candidates
            ),
        }
    return {
        "top1_states": {
            "INCOMPLETE": sum(row["data_status"] == "INCOMPLETE" for row in rows),
            "High": sum(row["uncertainty"] == "High" for row in rows),
            "Toss-up": sum(row["tier"] == "Toss-up" for row in rows),
        },
        "completeness": {
            "min": min(completeness),
            "max": max(completeness),
            "mean": statistics.fmean(completeness),
        },
        "all_candidate_states": all_candidate_states,
        "neutral_components": neutral_components,
        "training_eligible_rows": card["counts"]["training_eligible_rows"],
    }


def audit_leakage_evidence(
    candidate_rows: Sequence[Mapping[str, object]],
    card: Mapping[str, object],
    manual_text: str,
    *,
    locked_prediction_rows: Sequence[Mapping[str, object]],
    evaluated_rows: Sequence[Mapping[str, object]],
    lock_audit: Mapping[str, object],
) -> dict[str, object]:
    if len(candidate_rows) != int(card["counts"]["candidate_rows"]):
        raise ValueError("leakage evidence candidate count differs")
    for row in candidate_rows:
        provenance = row["provenance"]
        target = date.fromisoformat(str(row["official_date"]))
        if (
            date.fromisoformat(str(provenance["max_history_date"])) >= target
            or date.fromisoformat(str(provenance["schedule_used_max_date"]))
            >= target
            or provenance["statcast_target_excluded"] is not True
        ):
            raise ValueError("leakage evidence violates D-1/target exclusion")
        if (
            row["lineup_provenance"] != "RECONSTRUCTED_FINAL_LINEUP"
            or row["starter_provenance"] != "RECONSTRUCTED_ACTUAL_STARTER"
        ):
            raise ValueError("leakage evidence participant provenance differs")
    required_manual = (
        "2025-05-01",
        "2025-06-15",
        "2025-08-12",
        "max history date",
        "statcast_target_excluded=true",
        "RECONSTRUCTED_FINAL_LINEUP",
        "RECONSTRUCTED_ACTUAL_STARTER",
    )
    if any(token not in manual_text for token in required_manual):
        raise ValueError("manual audit evidence incomplete")
    if int(card["counts"]["training_eligible_rows"]) != int(
        card["counts"]["calibration_rows"]
    ):
        raise ValueError("calibration pool evidence differs")
    try:
        sealed_lock = lock_audit["sealed_lock"]
        lock_created_at = str(sealed_lock["created_at_utc"])
        sealed_row_count = int(sealed_lock["row_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("lock sequencing evidence is incomplete") from exc
    try:
        parsed_lock_time = datetime.fromisoformat(lock_created_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("lock sequencing evidence is invalid") from exc
    if parsed_lock_time.tzinfo is None or sealed_row_count != len(
        locked_prediction_rows
    ):
        raise ValueError("lock sequencing evidence is invalid")
    forbidden_outcome_fields = {
        "outcome",
        "actual_winner",
        "status",
        "market_snapshot_at",
        "market_source",
        "market_favorite",
        "away_no_vig_probability",
        "home_no_vig_probability",
    }
    if any(forbidden_outcome_fields & set(row) for row in locked_prediction_rows):
        raise ValueError("locked prediction outcome isolation differs")
    if len(locked_prediction_rows) != len(evaluated_rows):
        raise ValueError("locked/evaluated prediction binding differs")
    for locked, evaluated in zip(
        locked_prediction_rows, evaluated_rows, strict=True
    ):
        prediction = {key: value for key, value in evaluated.items() if key != "outcome"}
        if canonical_json_bytes(locked) != canonical_json_bytes(prediction):
            raise ValueError("locked/evaluated prediction binding differs")
    joined_at = lock_audit.get("outcome_joined_at_utc")
    sequence_status = "NOT_TIMESTAMP_PROVEN"
    if joined_at is not None:
        try:
            parsed_join_time = datetime.fromisoformat(
                str(joined_at).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValueError("lock sequencing evidence is invalid") from exc
        if parsed_join_time.tzinfo is None or parsed_join_time <= parsed_lock_time:
            raise ValueError("lock sequencing evidence is invalid")
        sequence_status = "PASS"
    later_revision_rows = sum(
        str(row["provenance"]["raw_source_max_date"])
        > str(row["official_date"])
        for row in candidate_rows
    )
    return {
        "statistical_as_of_safety": {
            "status": "PASS" if sequence_status == "PASS" else "LIMITATION",
            "candidate_rows": len(candidate_rows),
            "rule": (
                "max_history_date and schedule_used_max_date < target; "
                "target Statcast excluded"
            ),
            "calibration_status": "IN_SAMPLE_NOT_OOF",
            "training_eligible_rows": card["counts"]["training_eligible_rows"],
            "calibration_rows": card["counts"]["calibration_rows"],
            "d1_feature_isolation_status": "PASS",
            "locked_prediction_outcome_fields_status": "PASS",
            "lock_to_outcome_join_sequence_status": sequence_status,
            "sequence_limitation": (
                None
                if sequence_status == "PASS"
                else "sealed artifacts contain no outcome-join execution timestamp"
            ),
        },
        "participant_time_limitation": {
            "status": "LIMITATION",
            "lineup": "RECONSTRUCTED_FINAL_LINEUP",
            "starter": "RECONSTRUCTED_ACTUAL_STARTER",
        },
        "source_revision_limitation": {
            "status": "LIMITATION",
            "later_revision_rows": later_revision_rows,
            "rule": (
                "manifest hashes bind retrieved provider state, not historical "
                "database state"
            ),
        },
    }


def leakage_diagnostic(
    evidence: V51Evidence,
) -> dict[str, object]:
    return audit_leakage_evidence(
        evidence.candidate_rows,
        evidence.model_card,
        evidence.manual_audit_text,
        locked_prediction_rows=evidence.locked_prediction_rows,
        evaluated_rows=evidence.evaluated_rows,
        lock_audit=evidence.lock_audit,
    )


def evidence_source_records(evidence: V51Evidence) -> list[dict[str, str]]:
    paths = (
        "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl",
        "data/v5_1_bt/reports/summary.json",
        "data/v5_1_bt/outcomes/baseline.json",
        "data/v5_1_bt/locked/predictions.jsonl",
        "data/v5_1_bt/locked/lock_manifest.json",
        "audit/v5_1_bt/lock_manifest.json",
        "audit/v5_1_bt/model_card.json",
        "audit/v5_1_bt/manual_audit.md",
        "config/v5_1_bt.json",
        "data/v5_1_bt/features/candidate_raw_features.jsonl",
    )
    return [
        {
            "id": path.replace("/", "-").replace(".", "-"),
            "path": path,
            "sha256": evidence.source_file_sha256[path],
        }
        for path in paths
    ]


def build_diagnostic(evidence: V51Evidence) -> dict[str, object]:
    top1 = graded_rows(evidence.evaluated_rows, top1=True)
    all_graded = graded_rows(evidence.evaluated_rows)
    base = evaluate(
        evidence.evaluated_rows, baseline_by_game=evidence.baseline_by_game
    )
    base_primary = base["primary"]
    result = {
        "schema_version": 1,
        "model_version": "v5.1-BT",
        "population": {
            "label": "sealed Daily Top 1",
            "N": 100,
            "dates": ["2025-05-01", "2025-08-12"],
        },
        "primary": {
            "record": {key: base_primary[key] for key in ("W", "L", "N")},
            "accuracy": base_primary["accuracy"],
            "wilson_95": list(base_primary["wilson_95"]),
            "brier": base_primary["brier"],
            "log_loss": base_primary["log_loss"],
            "max_losing_streak": base_primary["max_losing_streak"],
        },
        "baselines": baseline_diagnostics(top1, evidence.baseline_by_game),
        "calibration": calibration_diagnostic(base["calibration"]),
        "discrimination": discrimination(top1),
        "thresholds": threshold_results(top1),
        "rank": rank_diagnostic(all_graded),
        "rank_comparisons": rank_comparisons(all_graded),
        "monthly": month_diagnostic(top1),
        "temporal": temporal_diagnostic(top1),
        "sides": side_diagnostic(top1),
        "teams": team_diagnostic(top1),
        "team_concentration": team_concentration(top1),
        "components": component_diagnostic(top1),
        "data_quality": quality_diagnostic(
            top1, evidence.locked_prediction_rows, evidence.model_card
        ),
        "leakage": leakage_diagnostic(evidence),
        "interpretation": {
            "incremental_value": "NOT_ESTABLISHED",
            "live_reproducibility": "NOT_ESTABLISHED",
            "market_claims_permitted": False,
        },
        "sources": evidence_source_records(evidence),
    }
    result["tables"] = {
        "paired_baselines": contract_table(
            [result["baselines"]["home"], result["baselines"]["season_record"]],
            "Daily Top 1 paired games",
            "paired correctness 2x2; exact McNemar; paired delta",
            ["season-record tie excluded"],
            [
                "data-v5_1_bt-outcomes-evaluated-evaluated_predictions-jsonl",
                "data-v5_1_bt-outcomes-baseline-json",
            ],
        ),
        "calibration": contract_table(
            result["calibration"]["bins"],
            "Daily Top 1 N=100",
            "fixed-bin probability mean, accuracy, gap, Brier contribution",
            [],
            ["data-v5_1_bt-outcomes-evaluated-evaluated_predictions-jsonl"],
        ),
        "rank": contract_table(
            result["rank"],
            "graded daily ranks 1-3",
            "wins and N by immutable daily_rank",
            [],
            ["data-v5_1_bt-outcomes-evaluated-evaluated_predictions-jsonl"],
        ),
        "monthly": contract_table(
            [{"month": key, **value} for key, value in result["monthly"].items()],
            "Daily Top 1 N=100",
            "wins and N by official_date month",
            [],
            ["data-v5_1_bt-outcomes-evaluated-evaluated_predictions-jsonl"],
        ),
        "teams": contract_table(
            [{"team": key, **value} for key, value in result["teams"].items()],
            "Daily Top 1 N=100",
            "wins, N, accuracy, Wilson interval by predicted winner",
            [],
            ["data-v5_1_bt-outcomes-evaluated-evaluated_predictions-jsonl"],
        ),
        "components": contract_table(
            [
                {"component": key, **value}
                for key, value in result["components"].items()
                if key in {"starter", "offense", "bullpen", "schedule"}
            ],
            "Daily Top 1 N=100",
            "chosen-side signed edge mean by outcome",
            [],
            ["data-v5_1_bt-outcomes-evaluated-evaluated_predictions-jsonl"],
        ),
    }
    return result
