from collections import Counter  # noqa: F401 - used by Task 2 diagnostic builders
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date  # noqa: F401 - used by Task 2 diagnostic builders
from pathlib import Path
from types import MappingProxyType
import hashlib
import json
import math
import statistics  # noqa: F401 - used by Task 2 diagnostic builders

from mlb_backtest.metrics import evaluate, wilson  # noqa: F401 - used by Task 2
from scipy.stats import fisher_exact, spearmanr  # noqa: F401 - used by Task 2


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
    verified_file_sha256: Mapping[str, str]
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
    return tuple(
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
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
    evaluated_path = (
        root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl"
    )
    audit = json.loads(
        (root / "audit/v5_1_bt/lock_manifest.json").read_text(encoding="utf-8")
    )
    expected = audited_digest_map(audit)
    verified = {
        name: sha256_file(root / relative)
        for name, relative in AUDITED_DIGEST_PATHS.items()
    }
    if verified != expected:
        raise ValueError("authoritative v5.1 evidence hash differs")
    rows = read_jsonl(evaluated_path)
    if (
        len(rows) != 1340
        or len({row["game_pk"] for row in rows}) != 1340
        or {row["model_version"] for row in rows} != {"v5.1-BT"}
    ):
        raise ValueError("evaluated v5.1 population differs")
    top1_dates = [row["official_date"] for row in rows if row["top1"] == "Yes"]
    if len(top1_dates) != 100 or len(set(top1_dates)) != 100:
        raise ValueError("Daily Top 1 population differs")
    baseline_raw = json.loads(
        (root / "data/v5_1_bt/outcomes/baseline.json").read_text(encoding="utf-8")
    )
    baseline = MappingProxyType(
        {int(game_pk): str(team) for game_pk, team in baseline_raw.items()}
    )
    return V51Evidence(
        root.resolve(),
        tuple(deep_freeze(row) for row in rows),
        baseline,
        deep_freeze(
            json.loads(
                (root / "data/v5_1_bt/reports/summary.json").read_text(
                    encoding="utf-8"
                )
            )
        ),
        deep_freeze(audit),
        deep_freeze(
            json.loads(
                (root / "audit/v5_1_bt/model_card.json").read_text(encoding="utf-8")
            )
        ),
        MappingProxyType(verified),
        verified["predictions_contents_sha256"],
        verified["evaluated_predictions"],
    )
