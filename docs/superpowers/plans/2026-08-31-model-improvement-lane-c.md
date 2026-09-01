# Model Improvement Lane C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an offline Lane C preflight that reconciles every eligible H22/H23 game-side/source-slot plus prediction-only H24 game-side/source-slot as `ADMITTED` or `NEUTRAL`, seals separate H22-development and H23-selection A+B+C scoring cards before their own predictions, proves exact as-of/schema/denominator/hash/replay safety without reading H24 outcomes, and emits a `QUALIFIED` disposition with one post-H23-evaluation frozen `A+B+C` card only when the H23 candidate is genuinely distinct from frozen `A+B`.

**Architecture:** Lane C is an additive package under `mlb_backtest/model_improvement/` and consumes only Common’s `ModelImprovementRunSpec`, `SourceGateReceipt`, `FeatureSeal`, `SealedFeatureRow`, generic `LaneDisposition`, and Common’s `FrozenModelCard`/`PredictionSeal`. Lane C resolves every artifact with `spec.output_path("lane_c", relative)` and does not create a second RunSpec or compatibility `.common` module. Optional source acquisition remains a separate change: this plan validates already-sealed evidence and never invents or downloads weather, roof, defense, venue, or catcher data.

**Tech Stack:** Python 3 standard library (`dataclasses`, `datetime`, `hashlib`, `json`, `pathlib`, `typing`), existing Common/Lane A model-improvement APIs, existing `environment_score()`/`defense_score()`, canonical JSON/SHA-256, and `unittest`.

---

## Scope and shared interfaces

The approved design is `docs/superpowers/specs/2026-08-31-parallel-model-result-improvement-design.md`. H22 (2022) is development, H23 (2023) is selection, and H24 (2024) is prediction-only here. Lane C cannot read any outcomes, H24 outcomes, odds, final-state substitutions, or production/live paths.

The following names are owned by Common/Lane A and are dependencies, not new Lane C types:

```python
from mlb_backtest.model_improvement.contracts import (
    FrozenModelCard, MetricPrimitive, PredictionRow, PredictionSeal, ScoringModelCard,
    SealedFeatureRow,
    load_frozen_model_card, seal_frozen_model_card,
)
from mlb_backtest.model_improvement.scoring_cards import (
    load_scoring_model_card, resolve_prediction_weight_authority,
    seal_scoring_model_card,
)
from mlb_backtest.model_improvement.run_spec import (
    ModelImprovementRunSpec, load_verified_run_spec, run_spec_sha256,
)
from mlb_backtest.model_improvement.source_gate import (
    SourceGateReceipt, require_source_gate,
)
from mlb_backtest.model_improvement.sealed_features import (
    FeatureSeal, load_sealed_feature_rows,
)
from mlb_backtest.model_improvement.lane_disposition import (
    LANE_DISPOSITION_STATES, LANE_IDS, LaneDisposition, load_lane_disposition,
    seal_lane_disposition,
)
from pathlib import Path
```

`LaneDisposition` is the generic Common disposition API. Lane C uses only states `QUALIFIED`, `ABSENT`, and `REJECTED`; it does not define a C-specific absence enum. `load_frozen_model_card(spec, path, expected_sha256)` and the Common `PredictionSeal` are the typed APIs consumed by this plan. Runtime artifact descriptors are compared with `.artifact_sha256`; that field is excluded from canonical payloads. If an upstream plan has not published one of these names, resolve that interface in the upstream plan before starting Lane C; do not add aliases here.

Before Lane C starts, Common must publish `LANE_IDS`, `load_sealed_feature_rows(spec, years, seal)` for the separate H22/H23/H24 seals, `load_lane_disposition(spec, path, expected_sha256)`, `seal_lane_disposition(spec, lane_id, state, parent_card_sha256, evidence_sha256, reason_codes)`, `seal_scoring_model_card(spec, namespace, relative_name, payload)`, `load_scoring_model_card(spec, path, expected_sha256)`, `resolve_prediction_weight_authority(spec, model_id, scoring_card)`, `seal_frozen_model_card(spec, namespace, relative_name, payload)`, `load_frozen_model_card(spec, path, expected_sha256)`, `seal_feature_rows(spec, "lane_c", fold_id, rows, parent_feature_seal_sha256, evidence_sha256)`, and `run_spec_sha256(spec)`. `ScoringModelCard` is the pure type from `contracts`; its seal/load/resolve functions come only from `scoring_cards`, and `contracts` must not import them back. Lane C calls these APIs rather than recomputing or weakening upstream seals. Validate `"C" in LANE_IDS` rather than declaring a local lane-ID domain.

The Common `ModelImprovementRunSpec` is the only authority for fold membership, forbidden years, cutoffs, and artifact paths. `lane_c_output_path(spec, relative)` must be exactly:

```python
def lane_c_output_path(spec: ModelImprovementRunSpec, relative: str) -> Path:
    return spec.output_path("lane_c", relative)
```

The exact Lane C slots are fixed and must be represented in the source contract and coverage key:

```python
LANE_C_SLOTS = {
    "environment": ("park", "weather_roof"),
    "defense": ("oaa", "framing", "blocking_throwing"),
}
```

No optional source is currently present in the verified historical inventory. `ADMITTED` fixtures in this plan are in-memory synthetic contract fixtures only; they are not claims that an optional source exists. Real acquisition must be a later plan that publishes raw bytes, `LogicalQuery`, manifest, and sidecar through existing evidence storage.

## File ownership

**Create:**

- `mlb_backtest/model_improvement/lane_c_contracts.py` — fixed slots, `CoverageKey`, `LaneCSourceContract`, `LaneCObservation`, exact cutoff and source-row validation, canonical observation digest.
- `mlb_backtest/model_improvement/lane_c_coverage.py` — RunSpec/FeatureSeal-bound expected population, per-fold reconciliation, coverage report, and candidate qualification predicates.
- `mlb_backtest/model_improvement/lane_c_replay.py` — query/raw/manifest/sidecar hash binding and parser replay; no network.
- `mlb_backtest/model_improvement/lane_c_candidate.py` — validated transforms, fixed context composition, Common `FeatureSeal` sealing for H22/H23/H24, fold-parameterized pre-prediction H22/H23 `ScoringModelCard`/`PredictionSeal` artifacts, H22 development evaluation, and H23-only post-evaluation `FrozenModelCard` plus generic `LaneDisposition` handoff.
- `schemas/model_improvement/lane_c_observation.schema.json` — closed observation schema.
- `schemas/model_improvement/lane_c_coverage.schema.json` — closed coverage/replay schema.
- `tests/test_model_improvement_lane_c_contracts.py`
- `tests/test_model_improvement_lane_c_coverage.py`
- `tests/test_model_improvement_lane_c_replay.py`
- `tests/test_model_improvement_lane_c_candidate.py`
- `tests/test_model_improvement_lane_c_handoff.py`

**Do not create or modify:** `mlb_backtest/model_improvement/__init__.py`, Common/Lane A files, H24/registry files, legacy runtime, source adapters, model CLI, caches, or real output artifacts. Do not create C-specific frozen-card or disposition schemas; the Common card and generic disposition contracts are authoritative.

### Task 1: Define the closed slot and observation contract

**Files:**
- Create: `mlb_backtest/model_improvement/lane_c_contracts.py`
- Create: `schemas/model_improvement/lane_c_observation.schema.json`
- Test: `tests/test_model_improvement_lane_c_contracts.py`

- [ ] **Step 1: Write the failing contract tests.**

```python
import unittest

from mlb_backtest.model_improvement.lane_c_contracts import (
    CoverageKey, LaneCObservation, LaneCSourceContract,
    LANE_C_SLOTS, validate_observation,
)
from mlb_backtest.model_improvement.canonical import (
    read_regular_bytes, sha256_bytes, write_immutable_json,
)


class LaneCContractTest(unittest.TestCase):
    def test_slots_are_exact(self):
        self.assertEqual(LANE_C_SLOTS["environment"], ("park", "weather_roof"))
        self.assertEqual(LANE_C_SLOTS["defense"], ("oaa", "framing", "blocking_throwing"))

    def test_admitted_requires_all_evidence(self):
        observation = LaneCObservation(
            CoverageKey("H22", 2022, 123, "away", "environment", "park"),
            "ADMITTED", 68.0, 1.0, "2022-04-09T16:00:00.000000Z",
            "2022-04-09T23:59:59.999999Z", "a" * 64, "b" * 64,
            "c" * 64, "d" * 64, "e" * 64, 1, None,
        )
        validate_observation(observation, LaneCSourceContract.for_slot("park"))

    def test_neutral_requires_reason_and_no_value(self):
        observation = LaneCObservation(
            CoverageKey("H22", 2022, 123, "home", "defense", "oaa"),
            "NEUTRAL", None, 0.0, None, "2022-04-09T23:59:59.999999Z",
            "f" * 64, None, None, None, None, None, "SOURCE_ABSENT",
        )
        validate_observation(observation, LaneCSourceContract.for_slot("oaa"))
        with self.assertRaisesRegex(ValueError, "reason_code"):
            validate_observation(
                LaneCObservation(**{**observation.__dict__, "reason_code": None}),
                LaneCSourceContract.for_slot("oaa"),
            )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_contracts -v
```

Expected: `ERROR` because `lane_c_contracts` does not exist.

- [ ] **Step 3: Implement the exact contract types and validation.**

```python
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

LaneCParentComponent = Literal["environment", "defense"]
LaneCSourceSlot = Literal["park", "weather_roof", "oaa", "framing", "blocking_throwing"]
LaneCSide = Literal["away", "home"]
LaneCDispositionStatus = Literal["ADMITTED", "NEUTRAL"]
LANE_C_SLOTS = {
    "environment": ("park", "weather_roof"),
    "defense": ("oaa", "framing", "blocking_throwing"),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UTC = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True, order=True)
class CoverageKey:
    fold_id: str
    target_year: int
    game_pk: int
    side: LaneCSide
    component: LaneCParentComponent
    source_slot: LaneCSourceSlot


@dataclass(frozen=True)
class LaneCSourceContract:
    source_slot: LaneCSourceSlot
    required_fields: tuple[str, ...]
    value_field: str
    denominator_field: str
    observed_at_field: str
    cutoff_rule: Literal["D_MINUS_1", "T_MINUS_45"]

    @classmethod
    def for_slot(cls, source_slot: LaneCSourceSlot) -> "LaneCSourceContract":
        definitions = {
            "park": (("gamePk", "venue_id", "park_score", "observed_at_utc", "denominator"), "park_score", "denominator", "D_MINUS_1"),
            "weather_roof": (("gamePk", "temperature_f", "wind_mph", "roof_status", "weather_score", "observed_at_utc", "denominator"), "weather_score", "denominator", "T_MINUS_45"),
            "oaa": (("gamePk", "team_id", "oaa", "opportunities", "observed_at_utc"), "oaa", "opportunities", "D_MINUS_1"),
            "framing": (("gamePk", "team_id", "framing", "opportunities", "observed_at_utc"), "framing", "opportunities", "D_MINUS_1"),
            "blocking_throwing": (("gamePk", "team_id", "blocking_throwing", "opportunities", "observed_at_utc"), "blocking_throwing", "opportunities", "D_MINUS_1"),
        }
        required, value, denominator, rule = definitions[source_slot]
        return cls(source_slot, required, value, denominator, "observed_at_utc", rule)


@dataclass(frozen=True)
class LaneCObservation:
    key: CoverageKey
    disposition: LaneCDispositionStatus
    value: float | None
    quality: float
    observed_at_utc: str | None
    target_cutoff_utc: str
    source_contract_sha256: str
    query_id: str | None
    raw_sha256: str | None
    manifest_sha256: str | None
    sidecar_sha256: str | None
    denominator: float | int | None
    reason_code: str | None


def _timestamp(value: str, name: str) -> datetime:
    try:
        return datetime.strptime(value, _UTC).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be canonical UTC") from exc


def _require_sha(value: str | None, name: str, required: bool) -> None:
    if required and value is None:
        raise ValueError(f"{name} is required")
    if value is not None and not _SHA256.fullmatch(value):
        raise ValueError(f"{name} must be lowercase SHA-256")


def validate_observation(
    observation: LaneCObservation, contract: LaneCSourceContract
) -> None:
    if observation.key.source_slot != contract.source_slot:
        raise ValueError("source slot differs from contract")
    _require_sha(observation.source_contract_sha256, "source_contract_sha256", True)
    _timestamp(observation.target_cutoff_utc, "target_cutoff_utc")
    if observation.disposition == "NEUTRAL":
        if observation.value is not None or observation.quality != 0.0:
            raise ValueError("NEUTRAL observations carry no value or quality")
        if observation.observed_at_utc is not None or observation.denominator is not None:
            raise ValueError("NEUTRAL observations carry no observed evidence")
        if any((observation.query_id, observation.raw_sha256, observation.manifest_sha256, observation.sidecar_sha256)):
            raise ValueError("NEUTRAL observations carry no source evidence")
        if not observation.reason_code:
            raise ValueError("NEUTRAL observations require reason_code")
        return
    if observation.disposition != "ADMITTED":
        raise ValueError("disposition must be ADMITTED or NEUTRAL")
    if observation.value is None or not math.isfinite(observation.value):
        raise ValueError("ADMITTED observations require a finite value")
    if not 0.0 <= observation.quality <= 1.0:
        raise ValueError("quality must be between 0 and 1")
    if observation.denominator is None or isinstance(observation.denominator, bool):
        raise ValueError("ADMITTED observations require a denominator")
    if not math.isfinite(float(observation.denominator)) or observation.denominator <= 0:
        raise ValueError("denominator must be finite and positive")
    if observation.observed_at_utc is None or observation.query_id is None:
        raise ValueError("ADMITTED observations require timestamp and query")
    if _timestamp(observation.observed_at_utc, "observed_at_utc") > _timestamp(observation.target_cutoff_utc, "target_cutoff_utc"):
        raise ValueError("observed_at_utc is after target_cutoff_utc")
    for name, value in (("query_id", observation.query_id), ("raw_sha256", observation.raw_sha256), ("manifest_sha256", observation.manifest_sha256), ("sidecar_sha256", observation.sidecar_sha256)):
        _require_sha(value, name, True)
    if observation.reason_code is not None:
        raise ValueError("ADMITTED observations do not carry a neutral reason")


def canonical_observation_bytes(observation: LaneCObservation) -> bytes:
    payload = {"key": observation.key.__dict__, "disposition": observation.disposition,
               "value": observation.value, "quality": observation.quality,
               "observed_at_utc": observation.observed_at_utc,
               "target_cutoff_utc": observation.target_cutoff_utc,
               "source_contract_sha256": observation.source_contract_sha256,
               "query_id": observation.query_id, "raw_sha256": observation.raw_sha256,
               "manifest_sha256": observation.manifest_sha256,
               "sidecar_sha256": observation.sidecar_sha256,
               "denominator": observation.denominator, "reason_code": observation.reason_code}
    return (json.dumps(payload, allow_nan=False, ensure_ascii=False,
                       separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def observation_sha256(observation: LaneCObservation) -> str:
    return hashlib.sha256(canonical_observation_bytes(observation)).hexdigest()
```

- [ ] **Step 4: Write `lane_c_observation.schema.json` with `additionalProperties: false`, exact slot/component enums, canonical timestamp patterns, SHA-256 patterns, and conditional `ADMITTED`/`NEUTRAL` required/forbidden fields.**

- [ ] **Step 5: Run the contract tests and commit.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_contracts -v
```

Expected: all three tests pass offline.

```bash
git add mlb_backtest/model_improvement/lane_c_contracts.py \
  schemas/model_improvement/lane_c_observation.schema.json \
  tests/test_model_improvement_lane_c_contracts.py
git commit -m "feat: define Lane C source slot contracts"
```

### Task 2: Derive exact RunSpec-bound cutoffs and validate source rows

**Files:**
- Modify: `mlb_backtest/model_improvement/lane_c_contracts.py`
- Modify: `tests/test_model_improvement_lane_c_contracts.py`

- [ ] **Step 1: Add failing `unittest.TestCase` cases for D-1, T-45, future timestamp, wrong fold/year, missing required field, wrong `gamePk`, and nonpositive denominator.**

```python
def test_d_minus_1_and_t_minus_45_are_derived_from_sealed_row(self):
    self.assertEqual(
        expected_target_cutoff_utc(self.spec, self.row, LaneCSourceContract.for_slot("oaa"), "H22"),
        "2022-04-09T23:59:59.999999Z",
    )
    self.assertEqual(
        expected_target_cutoff_utc(self.spec, self.row, LaneCSourceContract.for_slot("weather_roof"), "H22"),
        "2022-04-10T22:25:00.000000Z",
    )
```

Use concrete H22 and H24 `SealedFeatureRow` fixtures with their declared official dates and starts; construct the Common `ModelImprovementRunSpec` from its checked-in synthetic fixture. The test does not pass a caller-selected cutoff.

- [ ] **Step 2: Run the focused test to verify RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_contracts -v
```

Expected: `ImportError` for `expected_target_cutoff_utc` or `validate_source_row`.

- [ ] **Step 3: Implement exact cutoff and source-row validation.**

```python
from datetime import date, time, timedelta
from collections.abc import Mapping

from mlb_backtest.model_improvement.contracts import SealedFeatureRow
from mlb_backtest.model_improvement.run_spec import ModelImprovementRunSpec, run_spec_sha256


def expected_target_cutoff_utc(
    spec: ModelImprovementRunSpec,
    row: SealedFeatureRow,
    contract: LaneCSourceContract,
    fold_id: str,
) -> str:
    if fold_id not in ("H22", "H23", "H24") or row.target_year not in (2022, 2023, 2024):
        raise ValueError("Lane C accepts only H22, H23, and prediction-only H24 sealed rows")
    if spec.fold(fold_id).target_year != row.target_year:
        raise ValueError("row fold differs from ModelImprovementRunSpec")
    if contract.cutoff_rule == "D_MINUS_1":
        cutoff = datetime.combine(
            date.fromisoformat(row.official_date) - timedelta(days=1),
            time.max, tzinfo=timezone.utc,
        )
    elif contract.cutoff_rule == "T_MINUS_45":
        start = datetime.fromisoformat(row.scheduled_start_utc.replace("Z", "+00:00"))
        cutoff = start - timedelta(minutes=45)
    else:
        raise ValueError("unsupported Lane C cutoff rule")
    return cutoff.strftime(_UTC)


def validate_source_row(
    source_row: Mapping[str, object],
    contract: LaneCSourceContract,
    spec: ModelImprovementRunSpec,
    feature_row: SealedFeatureRow,
    fold_id: str,
) -> None:
    missing = tuple(field for field in contract.required_fields if field not in source_row)
    if missing:
        raise ValueError(f"source row missing schema fields: {', '.join(missing)}")
    if source_row.get("gamePk") != feature_row.game_pk:
        raise ValueError("source gamePk differs from sealed feature row")
    observed = _timestamp(source_row[contract.observed_at_field], contract.observed_at_field)
    cutoff = _timestamp(expected_target_cutoff_utc(spec, feature_row, contract, fold_id), "target_cutoff_utc")
    if observed > cutoff:
        raise ValueError("source observation is after exact target cutoff")
    denominator = source_row.get(contract.denominator_field)
    if isinstance(denominator, bool) or denominator is None:
        raise ValueError("source row requires a denominator")
    if not math.isfinite(float(denominator)) or float(denominator) <= 0:
        raise ValueError("source denominator must be finite and positive")


def validate_observation_as_of(
    observation: LaneCObservation,
    contract: LaneCSourceContract,
    spec: ModelImprovementRunSpec,
    feature_row: SealedFeatureRow,
    fold_id: str,
) -> None:
    validate_observation(observation, contract)
    expected = expected_target_cutoff_utc(spec, feature_row, contract, fold_id)
    if observation.target_cutoff_utc != expected:
        raise ValueError("observation target_cutoff_utc differs from exact RunSpec-bound cutoff")
```

Import `date`, `time`, `timedelta`, `Mapping`, `ModelImprovementRunSpec`, and `SealedFeatureRow` in the implementation. This validator is called both during admission and replay; no adapter may bypass it.

- [ ] **Step 4: Run the contract suite and commit the cutoff boundary.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_contracts -v
```

Expected: all cutoff, schema, identity, denominator, and future-timestamp cases pass.

```bash
git add mlb_backtest/model_improvement/lane_c_contracts.py tests/test_model_improvement_lane_c_contracts.py
git commit -m "feat: bind Lane C rows to exact as-of cutoffs"
```

### Task 3: Reconcile the complete H22/H23/H24 prediction-only expected population

**Files:**
- Create: `mlb_backtest/model_improvement/lane_c_coverage.py`
- Create: `schemas/model_improvement/lane_c_coverage.schema.json`
- Test: `tests/test_model_improvement_lane_c_coverage.py`

- [ ] **Step 1: Write failing tests for key shape, per-fold completeness, and all-neutral qualification.**

The test constructs three concrete `SealedFeatureRow` mappings from the Common synthetic fixture, one each for H22, H23, and prediction-only H24, and asserts 10 keys per game (2 sides × 2 environment slots plus 3 defense slots). It supplies one `NEUTRAL` observation for every key and asserts `coverage_complete == True`, `coverage_complete_by_fold == {"H22": True, "H23": True, "H24": True}`, `candidate_qualified == False`, and `admitted_count == 0`. A missing key and an extra key must populate separate `missing_keys` and `unknown_keys` arrays and set `coverage_complete == False`. It must also prove that the pure `validate_lane_c_prerequisites` accepts a correctly bound synthetic spec/receipt without invoking `require_source_gate`, while `run_lane_c_preflight` rejects an ordinary/synthetic RunSpec or an explicit receipt that differs from the receipt returned by `require_source_gate`.

```python
class LaneCCoverageTest(unittest.TestCase):
    def test_all_neutral_is_complete_but_not_qualified(self):
        keys = build_expected_coverage_keys(self.spec, self.feature_seals, self.rows)
        report = reconcile_coverage(
            keys, tuple(neutral_observation(key) for key in keys),
            run_spec=self.spec, feature_seals=self.feature_seals,
            replay_receipt_sha256=self.replay_receipt.artifact_sha256,
        )
        self.assertTrue(report.coverage_complete)
        self.assertEqual(dict(report.coverage_complete_by_fold), {"H22": True, "H23": True, "H24": True})
        self.assertFalse(report.candidate_qualified)
        self.assertEqual(report.admitted_count, 0)
```

- [ ] **Step 2: Run coverage tests to verify RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_coverage -v
```

Expected: `ERROR` because `lane_c_coverage` does not exist.

- [ ] **Step 3: Implement RunSpec/FeatureSeal-bound keys and report.**

```python
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mlb_backtest.model_improvement.contracts import SealedFeatureRow
from mlb_backtest.model_improvement.lane_c_contracts import (
    CoverageKey, LANE_C_SLOTS, LaneCObservation, LaneCSourceContract,
    validate_observation_as_of,
)
from mlb_backtest.model_improvement.run_spec import ModelImprovementRunSpec, run_spec_sha256
from mlb_backtest.model_improvement.sealed_features import FeatureSeal, load_sealed_feature_rows
from mlb_backtest.model_improvement.source_gate import (
    SourceGateReceipt, load_source_gate_receipt, require_source_gate,
)
from mlb_backtest.model_improvement.run_spec import load_verified_run_spec


@dataclass(frozen=True)
class LaneCCoverageReport:
    run_spec_sha256: str
    feature_seal_sha256: Mapping[str, str]
    expected_count: int
    admitted_count: int
    neutral_count: int
    dispositioned_count: int
    coverage_complete_by_fold: Mapping[str, bool]
    missing_keys: tuple[CoverageKey, ...]
    unknown_keys: tuple[CoverageKey, ...]
    duplicate_keys: tuple[CoverageKey, ...]
    replay_failed_keys: tuple[CoverageKey, ...]
    replay_receipt_sha256: str
    replay_complete: bool
    coverage_complete: bool
    candidate_qualified: bool


def run_lane_c_preflight(
    run_spec_path: Path,
    source_gate_path: Path,
    source_gate_expected_sha256: str,
    feature_seals: Mapping[str, FeatureSeal],
) -> tuple[ModelImprovementRunSpec, SourceGateReceipt, tuple[SealedFeatureRow, ...]]:
    """Real orchestration boundary; all lower-level APIs receive its bound inputs."""
    spec = load_verified_run_spec(run_spec_path)
    verified_gate = require_source_gate(spec)
    supplied_gate = load_source_gate_receipt(source_gate_path, source_gate_expected_sha256)
    if (supplied_gate.as_mapping() != verified_gate.as_mapping()
            or Path(supplied_gate.path).resolve() != Path(verified_gate.path).resolve()
            or supplied_gate.artifact_sha256 != verified_gate.artifact_sha256):
        raise ValueError("returned source gate differs from explicit receipt")
    return spec, supplied_gate, validate_lane_c_prerequisites(spec, supplied_gate, feature_seals)


def validate_lane_c_prerequisites(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_seals: Mapping[str, FeatureSeal],
):
    # Pure validator: synthetic tests may construct an already-bound spec/receipt.
    # Only run_lane_c_preflight may run activation or source-gate verification.
    if source_gate.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("SourceGateReceipt is bound to a different RunSpec")
    years = {"H22": 2022, "H23": 2023, "H24": 2024}
    if set(feature_seals) != set(years):
        raise ValueError("Lane C requires H22, H23, and H24 FeatureSeals")
    if any(seal.source_gate_sha256 != source_gate.artifact_sha256 for seal in feature_seals.values()):
        raise ValueError("FeatureSeal is bound to a different SourceGateReceipt")
    rows = tuple(row for fold_id, year in years.items()
                 for row in load_sealed_feature_rows(spec, (year,), feature_seals[fold_id]))
    if any(row.target_year not in years.values() for row in rows):
        raise ValueError("Lane C rows include an unapproved year")
    return rows


def build_expected_coverage_keys(
    spec: ModelImprovementRunSpec,
    feature_seals: Mapping[str, FeatureSeal],
    rows: Sequence[SealedFeatureRow] | None = None,
) -> tuple[CoverageKey, ...]:
    if rows is None:
        rows = tuple(row for fold_id, year in {"H22": 2022, "H23": 2023, "H24": 2024}.items()
                     for row in load_sealed_feature_rows(spec, (year,), feature_seals[fold_id]))
    keys = set()
    for row in rows:
        fold_id = {2022: "H22", 2023: "H23", 2024: "H24"}.get(row.target_year)
        if fold_id is None:
            continue
        if spec.fold(fold_id).target_year != row.target_year:
            raise ValueError("sealed feature fold differs from RunSpec")
        for side in ("away", "home"):
            for component, slots in LANE_C_SLOTS.items():
                for slot in slots:
                    keys.add(CoverageKey(fold_id, row.target_year, row.game_pk,
                                         side, component, slot))
    return tuple(sorted(keys))


def reconcile_coverage(
    expected: Sequence[CoverageKey],
    observations: Sequence[LaneCObservation],
    *,
    run_spec: ModelImprovementRunSpec,
    feature_seals: Mapping[str, FeatureSeal],
    replay_receipt_sha256: str,
    replay_failed_keys: Sequence[CoverageKey] = (),
    sealed_rows: Sequence[SealedFeatureRow] | None = None,
    replay_complete: bool = False,
) -> LaneCCoverageReport:
    if sealed_rows is None:
        sealed_rows = tuple(row for fold_id, year in {"H22": 2022, "H23": 2023, "H24": 2024}.items()
                            for row in load_sealed_feature_rows(run_spec, (year,), feature_seals[fold_id]))
    row_by_game = {row.game_pk: row for row in sealed_rows}
    expected_set = set(expected)
    counts = Counter(item.key for item in observations)
    observed_set = set(counts)
    missing = tuple(sorted(expected_set - observed_set))
    unknown = tuple(sorted(observed_set - expected_set))
    duplicate = tuple(sorted(key for key, count in counts.items() if count > 1))
    for item in observations:
        if item.key in expected_set and item.key not in unknown:
            feature_row = row_by_game.get(item.key.game_pk)
            if feature_row is None:
                raise ValueError("coverage key has no sealed feature row")
            validate_observation_as_of(
                item, LaneCSourceContract.for_slot(item.key.source_slot),
                run_spec, feature_row, item.key.fold_id,
            )
    admitted = sum(item.disposition == "ADMITTED" for item in observations)
    neutral = sum(item.disposition == "NEUTRAL" for item in observations)
    replay_failed = tuple(sorted(replay_failed_keys))
    by_fold = {
        fold: (
            any(key.fold_id == fold for key in expected_set)
            and not any(key.fold_id == fold for key in missing + unknown + duplicate + replay_failed)
        )
        for fold in ("H22", "H23", "H24")
    }
    complete = bool(expected_set) and not missing and not unknown and not duplicate and not replay_failed and admitted + neutral == len(expected_set)
    qualified = complete and all(by_fold.values()) and admitted > 0 and replay_complete
    return LaneCCoverageReport(
        run_spec_sha256=run_spec_sha256(run_spec),
        feature_seal_sha256={fold: feature_seals[fold].artifact_sha256
                            for fold in ("H22", "H23", "H24")},
        expected_count=len(expected_set), admitted_count=admitted, neutral_count=neutral,
        dispositioned_count=admitted + neutral, coverage_complete_by_fold=by_fold,
        missing_keys=missing, unknown_keys=unknown, duplicate_keys=duplicate,
        replay_failed_keys=replay_failed, replay_complete=replay_complete,
        replay_receipt_sha256=replay_receipt_sha256,
        coverage_complete=complete,
        candidate_qualified=qualified,
    )
```

`run_lane_c_preflight` is the only real Lane C orchestration entrypoint: it must start with `load_verified_run_spec`, call `require_source_gate`, securely reload the caller's explicit source-gate path/digest, and require exact payload/path/artifact-digest equality with the receipt returned by `require_source_gate` before it calls the pure validator. `validate_lane_c_prerequisites`, coverage/replay helpers, row materialization, scoring-card/prediction sealing, and fit/evaluation helpers take already-bound `spec`/`SourceGateReceipt` inputs and must not call `require_source_gate`; this keeps their synthetic tests possible without weakening the real entrypoint.

`sha256_sealed_rows()` and `run_spec_sha256()` are the existing Common canonical digest APIs. Do not recompute expected population from optional-source rows. A complete all-neutral report is explicitly not qualified; missing/unknown/duplicate keys are never auto-neutralized. `replay_complete` is true only after every admitted row has passed deterministic replay; an empty `replay_failed_keys` list alone is not sufficient.

- [ ] **Step 4: Write `lane_c_coverage.schema.json` with separate `coverage_complete` and `candidate_qualified` booleans, per-fold H22/H23/H24 statuses, `replay_receipt_sha256`, exact digest fields, and arrays for missing/unknown/duplicate/replay-failed keys.**

- [ ] **Step 5: Run coverage tests and commit.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_coverage -v
```

Expected: all population, per-fold, all-neutral, missing, unknown, and duplicate cases pass.

```bash
git add mlb_backtest/model_improvement/lane_c_coverage.py \
  schemas/model_improvement/lane_c_coverage.schema.json \
  tests/test_model_improvement_lane_c_coverage.py
git commit -m "feat: reconcile Lane C H22 H23 and H24 coverage"
```

### Task 4: Bind all hashes and rerun deterministic replay

**Files:**
- Create: `mlb_backtest/model_improvement/lane_c_replay.py`
- Test: `tests/test_model_improvement_lane_c_replay.py`

- [ ] **Step 1: Write failing replay tests for deterministic equality and each hash boundary.**

The `unittest.TestCase` fixture must construct exact raw bytes, a Common `LogicalQuery`, a sidecar containing `query_id`, `raw_sha256`, `manifest_sha256`, `sidecar_sha256`, and `byte_count`, plus a parser that returns the sealed observation. Assert identical replay hashes on repeated calls and failure for query, raw, manifest, sidecar, byte-count, parser projection, future timestamp, missing schema, and nonpositive denominator mutation. Add a tamper test proving `build_validated_parts(spec, observations, transform, replay_receipt)` rejects an observation absent from the receipt entries and a receipt whose canonical file digest is changed.

```python
def test_replay_revalidates_manifest_and_sidecar(self):
        with self.assertRaisesRegex(ValueError, "manifest_sha256"):
            verify_observation_replay(
            self.spec, self.feature_row, "H22", self.observation, self.raw, self.query,
            {**self.sidecar, "manifest_sha256": "f" * 64}, self.parser,
        )
```

- [ ] **Step 2: Run replay tests to verify RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_replay -v
```

Expected: `ERROR` because `lane_c_replay` does not exist.

- [ ] **Step 3: Implement replay with complete evidence and source-row validation.**

```python
from collections.abc import Callable, Mapping
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mlb_backtest.v52_evidence import LogicalQuery, logical_query_id
from mlb_backtest.model_improvement.contracts import SealedFeatureRow
from mlb_backtest.model_improvement.run_spec import ModelImprovementRunSpec, run_spec_sha256
from mlb_backtest.model_improvement.lane_c_contracts import (
    LaneCObservation, LaneCSourceContract, observation_sha256,
    validate_observation_as_of, validate_source_row,
)
from mlb_backtest.model_improvement.canonical import (
    canonical_json_bytes, read_regular_bytes, sha256_bytes, write_immutable_json,
)

ObservationParser = Callable[
    [bytes, LogicalQuery], tuple[LaneCObservation, Mapping[str, object]]
]


@dataclass(frozen=True)
class LaneCReplayReceipt:
    run_spec_sha256: str
    entries: Mapping[str, str]
    artifact_sha256: str
    path: str


def seal_replay_receipt(
    spec: ModelImprovementRunSpec,
    entries: Mapping[str, str],
) -> LaneCReplayReceipt:
    payload = {
        "schema": "LANE_C_REPLAY_RECEIPT_V1",
        "run_spec_sha256": run_spec_sha256(spec),
        "entries": dict(sorted(entries.items())),
    }
    path = spec.output_path("lane_c", "replay/lane_c_replay_receipt.json")
    write_immutable_json(path, payload)
    digest = sha256_bytes(read_regular_bytes(path, "Lane C replay receipt"))
    return LaneCReplayReceipt(payload["run_spec_sha256"], dict(payload["entries"]), digest, str(path))


def load_replay_receipt(
    spec: ModelImprovementRunSpec,
    receipt: LaneCReplayReceipt,
) -> LaneCReplayReceipt:
    path = Path(receipt.path)
    if path != spec.output_path("lane_c", "replay/lane_c_replay_receipt.json"):
        raise ValueError("replay receipt path differs from RunSpec")
    raw = read_regular_bytes(path, "Lane C replay receipt")
    if sha256_bytes(raw) != receipt.artifact_sha256:
        raise ValueError("replay receipt artifact digest differs")
    payload = json.loads(raw.decode("utf-8"))
    if raw != canonical_json_bytes(payload):
        raise ValueError("replay receipt is not canonical")
    if (set(payload) != {"schema", "run_spec_sha256", "entries"}
            or payload.get("schema") != "LANE_C_REPLAY_RECEIPT_V1"
            or payload.get("run_spec_sha256") != run_spec_sha256(spec)
            or receipt.run_spec_sha256 != run_spec_sha256(spec)
            or payload.get("entries") != dict(receipt.entries)):
        raise ValueError("replay receipt contract differs")
    return receipt


def verify_observation_replay(
    spec: ModelImprovementRunSpec,
    feature_row: SealedFeatureRow,
    fold_id: str,
    expected: LaneCObservation,
    raw: bytes,
    query: LogicalQuery,
    sidecar: Mapping[str, object],
    parser: ObservationParser,
) -> str:
    if sidecar.get("query_id") != logical_query_id(query):
        raise ValueError("query_id differs from LogicalQuery")
    raw_sha = hashlib.sha256(raw).hexdigest()
    if sidecar.get("raw_sha256") != raw_sha or expected.raw_sha256 != raw_sha:
        raise ValueError("raw_sha256 differs from sealed evidence")
    if sidecar.get("byte_count") != len(raw):
        raise ValueError("byte_count differs from raw bytes")
    for name in ("manifest_sha256", "sidecar_sha256"):
        if sidecar.get(name) != getattr(expected, name):
            raise ValueError(f"{name} differs from sealed evidence")
    replayed, source_row = parser(raw, query)
    contract = LaneCSourceContract.for_slot(replayed.key.source_slot)
    validate_source_row(source_row, contract, spec, feature_row, fold_id)
    validate_observation_as_of(replayed, contract, spec, feature_row, fold_id)
    if replayed != expected:
        raise ValueError("replayed observation differs from sealed observation")
    return sha256_bytes(canonical_json_bytes({
        "run_spec_sha256": run_spec_sha256(spec),
        "fold_id": fold_id,
        "feature_row_sha256": feature_row.feature_row_sha256,
        "observation_sha256": observation_sha256(replayed),
        "query_id": logical_query_id(query),
        "raw_sha256": raw_sha,
        "manifest_sha256": expected.manifest_sha256,
        "sidecar_sha256": expected.sidecar_sha256,
    }))
```

The parser adapter must call `validate_source_row(source_row, contract, spec, feature_row, fold_id)` before returning its observation. Collect each returned observation digest with its canonical replay-evidence digest in `entries`, then call `seal_replay_receipt()` once for the complete immutable manifest. The sealer/loader require a nonempty exact mapping of unique 64-character lowercase observation digests to unique 64-character lowercase replay-evidence digests; missing, extra, duplicate, or malformed entries fail. `verify_observation_replay()` receives and binds the matching `ModelImprovementRunSpec`, fold id, and `SealedFeatureRow`; replay cannot rely only on a caller assertion. No HTTP or filesystem discovery is allowed.

- [ ] **Step 4: Run replay tests and commit.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_replay -v
```

Expected: all hash, parser, schema, denominator, timestamp, and deterministic replay cases pass.

```bash
git add mlb_backtest/model_improvement/lane_c_replay.py tests/test_model_improvement_lane_c_replay.py
git commit -m "feat: bind Lane C evidence to deterministic replay"
```

### Task 5: Build validated transforms and the optional A+B+C handoff

**Files:**
- Create: `mlb_backtest/model_improvement/lane_c_candidate.py`
- Test: `tests/test_model_improvement_lane_c_candidate.py`
- Test: `tests/test_model_improvement_lane_c_handoff.py`

- [ ] **Step 1: Write failing tests for fallback, transform binding, digest difference, and generic dispositions.**

The tests must prove:

```python
def test_neutral_parts_keep_score_50_quality_0_without_redistribution(self):
    parts = build_validated_parts(self.spec, self.observations, self.transform, self.replay_receipt)
    result = build_lane_c_candidate(self.spec, self.parent_feature_seals, self.ab_rows,
                                    parts, self.qualified_report, self.replay_receipt)
    self.assertEqual(result[0].component_scores["away"]["environment"], 68.0)
    self.assertEqual(result[0].component_scores["away"]["defense"], 61.0)
    self.assertEqual(result[0].component_qualities["away"]["environment"], 0.6)
    self.assertEqual(result[0].component_qualities["away"]["defense"], 0.55)


def test_all_neutral_and_same_prediction_digest_are_not_qualified(self):
    _, disposition = build_lane_c_fold_result(
        spec=self.spec,
        fold_id="H23",
        coverage=self.complete_neutral_report,
        source_gate=self.source_gate,
        parent_feature_seals=self.parent_feature_seals,
        lane_c_feature_seals=self.lane_c_feature_seals,
        replay_receipt=self.replay_receipt,
        parent_ab_card=self.parent_ab_card,
        parent_ab_prediction=self.ab_prediction,
        lane_c_scoring_card=None,
        lane_c_prediction=None,
        lane_c_evaluation=self.evaluation,
        evidence_sha256=self.evidence_sha256,
        reason_codes=self.reason_codes,
    )
    self.assertEqual(disposition.state, "ABSENT")
    self.assertEqual(disposition.reason_codes, ("NO_ADMITTED_OBSERVATIONS",))


def test_h22_and_h23_use_distinct_fold_bound_cards_and_prediction_paths(self):
    h22_card = seal_lane_c_scoring_card(
        self.spec, "H22", self.source_gate, self.lane_c_feature_seals["H22"],
        self.parent_ab_card, self.qualified_report, self.replay_receipt,
        self.h22_calibration_card_sha256,
    )
    h23_card = seal_lane_c_scoring_card(
        self.spec, "H23", self.source_gate, self.lane_c_feature_seals["H23"],
        self.parent_ab_card, self.qualified_report, self.replay_receipt,
        self.h23_calibration_card_sha256,
    )
    self.assertTrue(h22_card.path.endswith("lane_c/H22/scoring_model_card.json"))
    self.assertTrue(h23_card.path.endswith("lane_c/H23/scoring_model_card.json"))
    self.assertEqual(h22_card.feature_seal_sha256, self.lane_c_feature_seals["H22"].artifact_sha256)
    self.assertEqual(h23_card.feature_seal_sha256, self.lane_c_feature_seals["H23"].artifact_sha256)
    h22_rows = materialize_lane_c_prediction_rows(
        self.spec, "H22", self.lane_c_feature_seals["H22"], h22_card)
    h23_rows = materialize_lane_c_prediction_rows(
        self.spec, "H23", self.lane_c_feature_seals["H23"], h23_card)
    self.assertTrue(h22_rows)
    self.assertTrue(h23_rows)
    for fold_id, card, rows in (("H22", h22_card, h22_rows), ("H23", h23_card, h23_rows)):
        self.assertTrue(all(row.model_id == "A+B+C" for row in rows))
        self.assertTrue(all(row.target_year == self.spec.fold(fold_id).target_year for row in rows))
        self.assertTrue(all(row.scoring_model_card_sha256 == card.artifact_sha256 for row in rows))
        self.assertTrue(all(row.prediction_contract_sha256 == self.spec.prediction_contract_sha256
                            for row in rows))
        self.assertTrue(all("outcome" not in row.as_mapping() and "label" not in row.as_mapping()
                            for row in rows))
        self.assertEqual(len({row.game_pk for row in rows}), len(rows))
    h22_prediction = seal_lane_c_fold_prediction(self.spec, "H22", h22_rows)
    h23_prediction = seal_lane_c_fold_prediction(self.spec, "H23", h23_rows)
    self.assertTrue(h22_prediction.path.endswith("oof/lane_c/A+B+C/H22/prediction_seal.json"))
    self.assertTrue(h23_prediction.path.endswith("oof/lane_c/A+B+C/H23/prediction_seal.json"))
    self.assertIsNone(h22_prediction.frozen_model_card_sha256)
    self.assertIsNone(h23_prediction.frozen_model_card_sha256)

def test_lane_c_materializer_rejects_cross_fold_or_tampered_scoring_authority(self):
    h22_card = seal_lane_c_scoring_card(
        self.spec, "H22", self.source_gate, self.lane_c_feature_seals["H22"],
        self.parent_ab_card, self.qualified_report, self.replay_receipt,
        self.h22_calibration_card_sha256,
    )
    h23_card = seal_lane_c_scoring_card(
        self.spec, "H23", self.source_gate, self.lane_c_feature_seals["H23"],
        self.parent_ab_card, self.qualified_report, self.replay_receipt,
        self.h23_calibration_card_sha256,
    )
    with self.assertRaisesRegex(ValueError, "fold FeatureSeal"):
        materialize_lane_c_prediction_rows(
            self.spec, "H22", self.lane_c_feature_seals["H22"], h23_card)
    with self.assertRaisesRegex(ValueError, "H22 or H23"):
        materialize_lane_c_prediction_rows(
            self.spec, "H24", self.lane_c_feature_seals["H24"], h23_card)

def test_lane_c_scoring_and_materialization_reject_non_lane_c_wrong_fold_and_gate_mismatch(self):
    with self.assertRaisesRegex(ValueError, "feature_set_id"):
        seal_lane_c_scoring_card(
            self.spec, "H22", self.source_gate, self.parent_feature_seals["H22"],
            self.parent_ab_card, self.qualified_report, self.replay_receipt,
            self.h22_calibration_card_sha256,
        )
    with self.assertRaisesRegex(ValueError, "fold"):
        seal_lane_c_scoring_card(
            self.spec, "H22", self.source_gate, self.lane_c_feature_seals["H23"],
            self.parent_ab_card, self.qualified_report, self.replay_receipt,
            self.h22_calibration_card_sha256,
        )
    with self.assertRaisesRegex(ValueError, "source gate"):
        seal_lane_c_scoring_card(
            self.spec, "H22", self.mismatched_source_gate, self.lane_c_feature_seals["H22"],
            self.parent_ab_card, self.qualified_report, self.replay_receipt,
            self.h22_calibration_card_sha256,
        )
    h22_card = seal_lane_c_scoring_card(
        self.spec, "H22", self.source_gate, self.lane_c_feature_seals["H22"],
        self.parent_ab_card, self.qualified_report, self.replay_receipt,
        self.h22_calibration_card_sha256,
    )
    with self.assertRaisesRegex(ValueError, "feature_set_id"):
        materialize_lane_c_prediction_rows(
            self.spec, "H22", self.parent_feature_seals["H22"], h22_card)
    with self.assertRaisesRegex(ValueError, "fold"):
        materialize_lane_c_prediction_rows(
            self.spec, "H22", self.lane_c_feature_seals["H23"], h22_card)
    # Fixture persists a schema-valid H22 card whose source_gate_sha256 is foreign;
    # only materialization can prove its mismatch against the supplied FeatureSeal.
    with self.assertRaisesRegex(ValueError, "source gate"):
        materialize_lane_c_prediction_rows(
            self.spec, "H22", self.lane_c_feature_seals["H22"],
            self.source_gate_mismatch_h22_card,
        )
```

`build_validated_parts()` accepts a `LaneCReplayReceipt` and rejects every observation whose digest is not listed in its immutable entries; neutral rows become score `50.0` and quality `0.0`, never a fabricated optional value. The candidate test must call `seal_lane_c_candidate_rows(spec, parent_common_seals, candidate_rows, coverage, replay_receipt)`, assert separate H22/H23/H24 Common FeatureSeal descriptors, reload each with `load_sealed_feature_rows`, and prove no unsealed row is accepted by materialization or handoff. It must then test `materialize_lane_c_prediction_rows` against the fold-bound seal/card, including row contract/card digests, no labels, deterministic unique population, and rejection of Common/non-`lane_c`, wrong-fold, and card-vs-seal source-gate-mismatched FeatureSeals before `seal_lane_c_fold_prediction` receives only the resulting tuple. Test fixtures are synthetic and in-memory.

- [ ] **Step 2: Run candidate tests to verify RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_candidate -v
```

Expected: `ERROR` because `lane_c_candidate` does not exist.

- [ ] **Step 3: Implement observation-bound transforms and fixed context composition.**

```python
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
import math
from dataclasses import dataclass
from pathlib import Path

from mlb_backtest.model_improvement.contracts import (
    FrozenModelCard, PredictionRow, PredictionSeal, ScoringModelCard, SealedFeatureRow,
    compute_feature_row_sha256, load_frozen_model_card,
    seal_frozen_model_card, sealed_feature_row_payload,
)
from mlb_backtest.model_improvement.scoring_cards import (
    load_scoring_model_card, resolve_prediction_weight_authority,
    seal_scoring_model_card,
)
from mlb_backtest.model_improvement.canonical import (
    canonical_json_bytes, read_regular_bytes, sha256_bytes, write_immutable_json,
)
from mlb_backtest.model_improvement.evaluation import EvaluationReport
from mlb_backtest.model_improvement.lane_c_contracts import (
    CoverageKey, LaneCObservation, LaneCSourceContract, observation_sha256,
    validate_observation,
)
from mlb_backtest.model_improvement.lane_c_coverage import LaneCCoverageReport, coverage_sha256
from mlb_backtest.model_improvement.lane_c_replay import (
    LaneCReplayReceipt, load_replay_receipt,
)
from mlb_backtest.model_improvement.oof_ledger import seal_oof_predictions
from mlb_backtest.model_improvement.lane_disposition import LANE_IDS, LaneDisposition, seal_lane_disposition
from mlb_backtest.model_improvement.run_spec import ModelImprovementRunSpec, run_spec_sha256
from mlb_backtest.model_improvement.sealed_features import (
    FeatureSeal, load_sealed_feature_rows, seal_feature_rows,
)
from mlb_backtest.model_improvement.source_gate import SourceGateReceipt
from mlb_backtest.features.context import defense_score, environment_score
from mlb_backtest.normalize import probability
from mlb_backtest.ranking import rank_daily
from mlb_backtest.scoring import predicted_pick


@dataclass(frozen=True)
class LaneCTransformedPart:
    key: CoverageKey
    score: float
    quality: float
    observation_sha256: str
    source_contract_sha256: str
    query_id: str | None
    raw_sha256: str | None
    manifest_sha256: str | None
    sidecar_sha256: str | None
    replay_receipt_sha256: str


def build_validated_parts(
    spec: ModelImprovementRunSpec,
    observations: Sequence[LaneCObservation],
    transform: Callable[[LaneCObservation], tuple[float, float]],
    replay_receipt: LaneCReplayReceipt,
) -> tuple[LaneCTransformedPart, ...]:
    load_replay_receipt(spec, replay_receipt)
    result = []
    for observation in observations:
        validate_observation(observation, LaneCSourceContract.for_slot(observation.key.source_slot))
        observation_digest = observation_sha256(observation)
        replay_digest = replay_receipt.entries.get(observation_digest)
        if replay_digest is None:
            raise ValueError("observation is absent from sealed replay receipt")
        if observation.disposition == "NEUTRAL":
            result.append(LaneCTransformedPart(
                observation.key, 50.0, 0.0, observation_digest,
                observation.source_contract_sha256, None, None, None, None,
                replay_receipt.artifact_sha256,
            ))
            continue
        score, quality = transform(observation)
        if not math.isfinite(score) or not 0.0 <= score <= 100.0:
            raise ValueError("transformed score must be between 0 and 100")
        if not math.isfinite(quality) or not 0.0 <= quality <= 1.0:
            raise ValueError("transformed quality must be between 0 and 1")
        result.append(LaneCTransformedPart(
            observation.key, score, quality, observation_digest,
            observation.source_contract_sha256, observation.query_id,
            observation.raw_sha256, observation.manifest_sha256,
            observation.sidecar_sha256, replay_receipt.artifact_sha256,
        ))
    return tuple(result)


def lane_c_output_path(spec: ModelImprovementRunSpec, relative: str):
    return spec.output_path("lane_c", relative)
```

`build_lane_c_candidate()` returns `tuple[SealedFeatureRow, ...]` and must accept `spec`, the parent `FeatureSeal` mapping for H22/H23/H24, `Sequence[SealedFeatureRow]`, `Sequence[LaneCTransformedPart]`, a `LaneCCoverageReport`, and the sealed `LaneCReplayReceipt`. It first loads each parent fold’s rows through `load_sealed_feature_rows(spec, (year,), parent_feature_seals[fold_id])`, verifies population/game identity and `row.source_gate_sha256 == parent_feature_seals[fold_id].source_gate_sha256`, then copies each row’s nested `component_scores`, `component_qualities`, `metric_primitives`, and `component_inputs_sha256` maps through `row.as_mapping()`. It rebuilds the row only through Common `sealed_feature_row_payload` and `compute_feature_row_sha256`; it never returns dictionaries such as `away_scores` or `qualities`. It rejects `candidate_qualified == False`, missing transform keys, wrong RunSpec/FeatureSeal/replay-receipt digests, outcome fields, and any transformed part whose observation digest is not present in the immutable replay receipt. It calls the existing `environment_score(park, weather_roof)` and `defense_score(oaa, framing, blocking_throwing)`; missing parts remain score 50/quality 0 and weights are never redistributed. Game-level context completeness is `min(away_context_quality, home_context_quality)` only when materializing prediction rows for ranking; it does not overwrite either shared row map. Candidate transforms are never built from unvalidated observations. The returned rows are not eligible for scoring or handoff until `seal_lane_c_candidate_rows()` creates and reloads the three Common lane_c FeatureSeals.

The row update must be explicit. The nested Common maps are recursively frozen, so start from the fresh JSON values returned by `row.as_mapping()`; never mutate a row map in place. Construct the replacement with the Common digest helper, then assert the same helper’s row projection before returning it:

```python
def build_lane_c_candidate(
    spec: ModelImprovementRunSpec,
    parent_feature_seals: Mapping[str, FeatureSeal],
    ab_rows: Sequence[SealedFeatureRow],
    parts: Sequence[LaneCTransformedPart],
    coverage: LaneCCoverageReport,
    replay_receipt: LaneCReplayReceipt,
) -> tuple[SealedFeatureRow, ...]:
    load_replay_receipt(spec, replay_receipt)
    if fold_id == "H23" and not coverage.candidate_qualified:
        raise ValueError("Lane C candidate is not qualified")
    if coverage.run_spec_sha256 != run_spec_sha256(spec):
        raise ValueError("coverage RunSpec digest differs")
    if coverage.feature_seal_sha256 != {
        fold: parent_feature_seals[fold].artifact_sha256
        for fold in ("H22", "H23", "H24")
    }:
        raise ValueError("coverage FeatureSeal digest differs")
    if coverage.replay_receipt_sha256 != replay_receipt.artifact_sha256:
        raise ValueError("coverage replay receipt digest differs")
    parent_rows = tuple(row for fold_id, year in {"H22": 2022, "H23": 2023, "H24": 2024}.items()
                        for row in load_sealed_feature_rows(spec, (year,), parent_feature_seals[fold_id]))
    parent_by_game = {row.game_pk: row for row in parent_rows}
    by_key = {part.key: part for part in parts}
    output = []
    for row in ab_rows:
        fold_id = {2022: "H22", 2023: "H23", 2024: "H24"}[row.target_year]
        parent_row = parent_by_game.get(row.game_pk)
        if parent_row != row:
            raise ValueError("A+B row is not present in the sealed parent population")
        if row.source_gate_sha256 != parent_feature_seals[fold_id].source_gate_sha256:
            raise ValueError("candidate row source gate differs from parent FeatureSeal")
        scores = {side: dict(values) for side, values in row.component_scores.items()}
        qualities = {side: dict(values) for side, values in row.component_qualities.items()}
        primitives = {side: dict(values) for side, values in row.metric_primitives.items()}
        input_hashes = {side: dict(values) for side, values in row.component_inputs_sha256.items()}
        for side in ("away", "home"):
            for component, slots in LANE_C_SLOTS.items():
                slot_parts = tuple(by_key[CoverageKey(fold_id, row.target_year, row.game_pk,
                                                      side, component, slot)]
                                   for slot in slots)
                if any(part.observation_sha256 not in replay_receipt.entries
                       or part.replay_receipt_sha256 != replay_receipt.artifact_sha256
                       for part in slot_parts):
                    raise ValueError("candidate part lacks replay-verified observation")
                slot_values = {
                    part.key.source_slot: {"score": part.score, "quality": part.quality}
                    for part in slot_parts
                }
                if component == "environment":
                    score, quality = environment_score(
                        slot_values["park"], slot_values["weather_roof"]
                    )
                else:
                    score, quality = defense_score(
                        slot_values["oaa"], slot_values["framing"],
                        slot_values["blocking_throwing"],
                    )
                scores[side][component] = score
                qualities[side][component] = quality
                primitives[side][component] = tuple(
                    make_lane_c_metric_primitives(slot_parts)
                )
                input_hashes[side][component] = sha256_bytes(canonical_json_bytes([
                    {
                        "observation_sha256": part.observation_sha256,
                        "source_contract_sha256": part.source_contract_sha256,
                        "query_id": part.query_id,
                        "raw_sha256": part.raw_sha256,
                        "manifest_sha256": part.manifest_sha256,
                        "sidecar_sha256": part.sidecar_sha256,
                    }
                    for part in slot_parts
                ]))
        payload = row.as_mapping()
        payload.update(component_scores=scores, component_qualities=qualities,
                       metric_primitives=primitives, component_inputs_sha256=input_hashes)
        payload = sealed_feature_row_payload(payload)
        candidate = SealedFeatureRow(
            **payload, feature_row_sha256=compute_feature_row_sha256(payload))
        if candidate.feature_row_sha256 != compute_feature_row_sha256(
                sealed_feature_row_payload(candidate)):
            raise RuntimeError("Common feature-row digest projection differs")
        output.append(candidate)
    return tuple(output)
```

The production implementation uses the exact `CoverageKey(fold_id, row.target_year, row.game_pk, side, component, slot)` lookup and the existing score functions’ signatures. `make_lane_c_metric_primitives(parts: Sequence[LaneCTransformedPart]) -> tuple[MetricPrimitive, ...]` records each part’s normalized score, aggregation weight, quality recipe, and observation/source-contract/query/raw/manifest/sidecar binding in the Common `MetricPrimitive` tree. The fixed environment and defense recipes must use `family=None` (never invent `"environment"` or `"defense"` as a Common quality family). No outcome field is copied or introduced.

Seal the returned rows before any prediction materialization or handoff:

```python
def seal_lane_c_candidate_rows(
    spec: ModelImprovementRunSpec,
    parent_common_seals: Mapping[str, FeatureSeal],
    candidate_rows: Sequence[SealedFeatureRow],
    coverage: LaneCCoverageReport,
    replay_receipt: LaneCReplayReceipt,
) -> Mapping[str, FeatureSeal]:
    load_replay_receipt(spec, replay_receipt)
    years = {"H22": 2022, "H23": 2023, "H24": 2024}
    if set(parent_common_seals) != set(years):
        raise ValueError("Lane C requires H22, H23, and H24 parent FeatureSeals")
    coverage_digest = coverage_sha256(coverage)
    if coverage.replay_receipt_sha256 != replay_receipt.artifact_sha256:
        raise ValueError("coverage replay receipt digest differs")
    by_fold = {
        fold_id: tuple(row for row in candidate_rows
                       if row.target_year == year)
        for fold_id, year in years.items()
    }
    seals = {}
    for fold_id, rows in by_fold.items():
        parent_rows = load_sealed_feature_rows(spec, (years[fold_id],), parent_common_seals[fold_id])
        if {row.game_pk for row in rows} != {row.game_pk for row in parent_rows}:
            raise ValueError(f"Lane C {fold_id} rows do not match parent FeatureSeal population")
        if any(row.source_gate_sha256 != parent_common_seals[fold_id].source_gate_sha256
               for row in rows):
            raise ValueError(f"Lane C {fold_id} row source gate differs from parent FeatureSeal")
        seals[fold_id] = seal_feature_rows(
            spec, "lane_c", fold_id, rows,
            parent_feature_seal_sha256=parent_common_seals[fold_id].artifact_sha256,
            evidence_sha256=(coverage_digest, replay_receipt.artifact_sha256,
                             parent_common_seals[fold_id].artifact_sha256),
        )
    return seals
```

`seal_lane_c_candidate_rows()` creates only Common `FeatureSeal` artifacts using the exact revised `seal_feature_rows(spec, "lane_c", fold_id, rows, parent_feature_seal_sha256=..., evidence_sha256=...)` API; it adds no C-specific seal fields. It creates separate lane_c seals for H22, H23, and prediction-only H24, and includes the immutable replay receipt digest in each seal’s evidence. The parent common seal and coverage digest are carried by the Common seal’s existing source-gate/population/rows bindings, the candidate rows’ `component_inputs_sha256`, and disposition evidence. The returned mapping must be loaded separately with `load_sealed_feature_rows(spec, (2022,), lane_c_feature_seals["H22"])`, `load_sealed_feature_rows(spec, (2023,), lane_c_feature_seals["H23"])`, and `load_sealed_feature_rows(spec, (2024,), lane_c_feature_seals["H24"])`; unsealed candidate rows cannot reach materialization, scoring, or H23/H24 handoff.

- [ ] **Step 4: Seal Lane C's own scoring card before prediction, then implement prediction-digest qualification and generic disposition handoff.**

```python
def seal_lane_c_scoring_card(
    spec: ModelImprovementRunSpec,
    fold_id: str,
    source_gate: SourceGateReceipt,
    lane_c_feature_seal: FeatureSeal,
    parent_ab_card: FrozenModelCard,
    coverage: LaneCCoverageReport,
    replay_receipt: LaneCReplayReceipt,
    calibration_card_sha256: str,
) -> ScoringModelCard:
    if fold_id not in ("H22", "H23"):
        raise ValueError("Lane C scoring cards are pre-prediction H22 or H23 artifacts")
    if lane_c_feature_seal.feature_set_id != "lane_c":
        raise ValueError("Lane C scoring card requires FeatureSeal feature_set_id lane_c")
    if lane_c_feature_seal.fold_id != fold_id:
        raise ValueError("Lane C FeatureSeal fold differs from scoring-card fold")
    if lane_c_feature_seal.source_gate_sha256 != source_gate.artifact_sha256:
        raise ValueError("Lane C FeatureSeal source gate differs from scoring-card source gate")
    parent_weights = dict(parent_ab_card.parameters["component_weights_pp"])
    payload = {
        "model_id": "A+B+C", "lane_id": "C", "fold_id": fold_id, "candidate_id": None,
        "run_spec_sha256": run_spec_sha256(spec),
        "source_gate_sha256": source_gate.artifact_sha256,
        "baseline_replay_sha256": parent_ab_card.baseline_replay_sha256,
        "parent_card_sha256": parent_ab_card.artifact_sha256,
        "feature_seal_sha256": lane_c_feature_seal.artifact_sha256,
        "prediction_contract_sha256": spec.prediction_contract_sha256,
        "calibration_card_sha256": calibration_card_sha256,
        "candidate_payload_sha256": coverage_sha256(coverage),
        "parameters": {
            "component_weights_pp": parent_weights,
            "coverage_sha256": coverage_sha256(coverage),
            "replay_receipt_sha256": replay_receipt.artifact_sha256,
            "fold_feature_seal_sha256": lane_c_feature_seal.artifact_sha256,
        },
    }
    return seal_scoring_model_card(spec, "lane_c", f"{fold_id}/scoring_model_card.json", payload)

def materialize_lane_c_prediction_rows(
    spec: ModelImprovementRunSpec,
    fold_id: str,
    feature_seal: FeatureSeal,
    scoring_card: ScoringModelCard,
) -> tuple[PredictionRow, ...]:
    """Materialize one fold's label-free A+B+C prediction population before sealing."""
    if fold_id not in ("H22", "H23"):
        raise ValueError("Lane C prediction rows are H22 or H23 only")
    target_year = {"H22": 2022, "H23": 2023}[fold_id]
    if spec.fold(fold_id).target_year != target_year:
        raise ValueError("Lane C fold target year differs from RunSpec")
    if feature_seal.feature_set_id != "lane_c":
        raise ValueError("Lane C materialization requires FeatureSeal feature_set_id lane_c")
    if feature_seal.fold_id != fold_id:
        raise ValueError("Lane C FeatureSeal fold differs from materialization fold")

    # This loader is the only row source: it securely rereads the declared FeatureSeal
    # population rather than accepting caller-provided SealedFeatureRow objects.
    sealed_rows = load_sealed_feature_rows(spec, (target_year,), feature_seal)
    verified_card = load_scoring_model_card(
        spec, Path(scoring_card.path), scoring_card.artifact_sha256,
    )
    if (
        verified_card != scoring_card
        or verified_card.model_id != "A+B+C"
        or verified_card.lane_id != "C"
        or verified_card.fold_id != fold_id
        or verified_card.feature_seal_sha256 != feature_seal.artifact_sha256
        or verified_card.source_gate_sha256 != feature_seal.source_gate_sha256
        or verified_card.prediction_contract_sha256 != spec.prediction_contract_sha256
    ):
        raise ValueError("Lane C scoring card differs from its fold FeatureSeal, source gate, or prediction contract")
    authority = resolve_prediction_weight_authority(spec, "A+B+C", verified_card)
    if authority.component_weights_pp != verified_card.parameters["component_weights_pp"]:
        raise ValueError("resolved Lane C weights differ from scoring card")
    if any(
        row.target_year != target_year
        or row.source_gate_sha256 != feature_seal.source_gate_sha256
        for row in sealed_rows
    ):
        raise ValueError("Lane C FeatureSeal rows differ from their fold binding")

    # `alpha`/`beta` are securely reread from the immutable fold-calibration artifact
    # named by verified_card.calibration_card_sha256.  This is the same pre-existing
    # v5.1 normalizer used by Common/Lane B; it is not fitted here and reads no outcomes.
    alpha, beta = load_fold_calibration_parameters(
        spec, fold_id, verified_card.calibration_card_sha256,
    )
    unranked: list[dict[str, object]] = []
    for source in sealed_rows:
        home = source.component_scores["home"]
        away = source.component_scores["away"]
        home_quality = source.component_qualities["home"]
        away_quality = source.component_qualities["away"]
        weights = authority.component_weights_pp
        edge = math.fsum(
            weights[name] / 100 * (home[name] - away[name])
            for name in spec.prediction_contract["component_order"]
        )
        completeness = math.fsum(
            weights[name] / 100 * min(home_quality[name], away_quality[name])
            for name in spec.prediction_contract["component_order"]
        )
        p_home = probability(edge, alpha, beta, (.10, .90))
        pick = predicted_pick("home", "away", p_home)
        unranked.append({
            "game_pk": source.game_pk,
            "official_date": source.official_date,
            "target_year": source.target_year,
            "scheduled_start_utc": source.scheduled_start_utc,
            "p_home": p_home,
            "picked_side": pick["predicted_side"],
            "pick_probability": pick["pick_probability"],
            "confidence": 10.0 * completeness,
            "starter_edge_abs": source.starter_edge_abs,
            "feature_row_sha256": source.feature_row_sha256,
            "prediction_contract_sha256": spec.prediction_contract_sha256,
            "scoring_model_card_sha256": verified_card.artifact_sha256,
        })

    # Common rank_daily supplies the frozen per-date tie order.  Neutral rows are
    # label-free rows too, but have rank 0 and do not enter a pickable ranking group.
    ranks: dict[int, int] = {}
    pickable_by_date: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in unranked:
        if row["picked_side"] != "none":
            pickable_by_date[row["official_date"]].append({
                name: row[name] for name in (
                    "game_pk", "official_date", "pick_probability", "confidence",
                    "starter_edge_abs", "scheduled_start_utc",
                )
            })
    for official_date in sorted(pickable_by_date):
        for ranked in rank_daily(pickable_by_date[official_date]):
            ranks[int(ranked["game_pk"])] = int(ranked["daily_rank"])

    if len({row["game_pk"] for row in unranked}) != len(unranked):
        raise ValueError("Lane C fold population contains duplicate games")
    return tuple(
        PredictionRow(
            model_id="A+B+C", game_pk=int(row["game_pk"]),
            official_date=str(row["official_date"]), target_year=int(row["target_year"]),
            p_home=float(row["p_home"]), picked_side=str(row["picked_side"]),
            pick_probability=float(row["pick_probability"]), confidence=float(row["confidence"]),
            starter_edge_abs=float(row["starter_edge_abs"]),
            scheduled_start_utc=str(row["scheduled_start_utc"]),
            rank=0 if row["picked_side"] == "none" else ranks[int(row["game_pk"])],
            feature_row_sha256=str(row["feature_row_sha256"]),
            prediction_contract_sha256=str(row["prediction_contract_sha256"]),
            scoring_model_card_sha256=str(row["scoring_model_card_sha256"]),
        )
        for row in sorted(unranked, key=lambda item: (
            str(item["official_date"]), str(item["scheduled_start_utc"]), int(item["game_pk"]),
        ))
    )

def seal_lane_c_fold_prediction(
    spec: ModelImprovementRunSpec,
    fold_id: str,
    rows: tuple[PredictionRow, ...],
) -> PredictionSeal:
    """Consume already materialized rows and delegate persistence to the Common sealer."""
    if fold_id not in ("H22", "H23"):
        raise ValueError("Lane C prediction seals are H22 or H23 only")
    target_year = {"H22": 2022, "H23": 2023}[fold_id]
    if (
        len({row.game_pk for row in rows}) != len(rows)
        or any(
            row.model_id != "A+B+C"
            or row.target_year != target_year
            or row.prediction_contract_sha256 != spec.prediction_contract_sha256
            for row in rows
        )
    ):
        raise ValueError("Lane C materialized prediction rows violate fold or contract binding")
    # No FeatureSeal/Card/outcome argument is accepted here: materialization above is
    # the sole scorer and the Common sealer owns all row/seal binding and persistence.
    return seal_oof_predictions(spec, f"lane_c/A+B+C/{fold_id}", rows)

def build_lane_c_fold_result(
    spec: ModelImprovementRunSpec,
    fold_id: str,
    coverage: LaneCCoverageReport,
    source_gate: SourceGateReceipt,
    parent_feature_seals: Mapping[str, FeatureSeal],
    lane_c_feature_seals: Mapping[str, FeatureSeal],
    replay_receipt: LaneCReplayReceipt,
    parent_ab_card: FrozenModelCard,
    parent_ab_prediction: PredictionSeal,
    lane_c_scoring_card: ScoringModelCard | None,
    lane_c_prediction: PredictionSeal | None,
    lane_c_evaluation: EvaluationReport,
    evidence_sha256: Sequence[str],
    reason_codes: Sequence[str],
) -> tuple[FrozenModelCard | None, LaneDisposition]:
    if "C" not in LANE_IDS:
        raise RuntimeError("Common Lane C identifier is unavailable")
    if fold_id not in ("H22", "H23"):
        raise ValueError("Lane C evaluates only H22 development or H23 selection")
    parent_hash = parent_ab_card.artifact_sha256
    evidence = tuple(evidence_sha256)
    load_replay_receipt(spec, replay_receipt)
    if replay_receipt.artifact_sha256 not in evidence:
        evidence = evidence + (replay_receipt.artifact_sha256,)
    if coverage.replay_receipt_sha256 != replay_receipt.artifact_sha256:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("REPLAY_RECEIPT_MISMATCH",))
    if not coverage.coverage_complete_by_fold.get(fold_id, False):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("COVERAGE_INCOMPLETE",))
    if coverage.admitted_count == 0:
        return None, seal_lane_disposition(spec, "C", "ABSENT", parent_hash,
                                           evidence, ("NO_ADMITTED_OBSERVATIONS",))
    if coverage.replay_failed_keys:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("REPLAY_FAILED",))
    if lane_c_prediction is None:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("PREDICTION_SEAL_MISSING",))
    if lane_c_scoring_card is None:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("SCORING_MODEL_CARD_MISSING",))
    if lane_c_prediction.prediction_contract_sha256 != spec.prediction_contract_sha256:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("PREDICTION_CONTRACT_MISMATCH",))
    if (lane_c_prediction.scoring_model_card_sha256 != lane_c_scoring_card.artifact_sha256
            or lane_c_prediction.frozen_model_card_sha256 is not None
            or lane_c_scoring_card.model_id != "A+B+C"
            or lane_c_scoring_card.lane_id != "C"
            or lane_c_scoring_card.fold_id != fold_id
            or lane_c_scoring_card.parent_card_sha256 != parent_hash
            or lane_c_scoring_card.prediction_contract_sha256 != spec.prediction_contract_sha256):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("SCORING_CARD_PREDICTION_MISMATCH",))
    if not coverage.candidate_qualified:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("REPLAY_INCOMPLETE",))
    required_folds = ("H22",) if fold_id == "H22" else ("H22", "H23", "H24")
    if not all(coverage.coverage_complete_by_fold[fold] for fold in required_folds):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("REQUIRED_FOLD_INCOMPLETE",))
    if not set(required_folds).issubset(parent_feature_seals):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("PARENT_FEATURE_SEAL_MISSING",))
    if not set(required_folds).issubset(lane_c_feature_seals):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("CANDIDATE_FEATURE_SEAL_MISSING",))
    if any(seal.source_gate_sha256 != source_gate.artifact_sha256
           for seal in parent_feature_seals.values()):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("SOURCE_GATE_MISMATCH",))
    if any(seal.source_gate_sha256 != source_gate.artifact_sha256
           for seal in lane_c_feature_seals.values()):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("CANDIDATE_FEATURE_SEAL_MISMATCH",))
    if any(seal.run_spec_sha256 != run_spec_sha256(spec)
           for seal in (*parent_feature_seals.values(), *lane_c_feature_seals.values())):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("FEATURE_SEAL_RUN_SPEC_MISMATCH",))
    coverage_digest = coverage_sha256(coverage)
    if any(
        parent_feature_seals[fold].feature_set_id != "common"
        or parent_feature_seals[fold].fold_id != fold
        or lane_c_feature_seals[fold].feature_set_id != "lane_c"
        or lane_c_feature_seals[fold].fold_id != fold
        or lane_c_feature_seals[fold].parent_feature_seal_sha256
            != parent_feature_seals[fold].artifact_sha256
        or coverage_digest not in lane_c_feature_seals[fold].evidence_sha256
        or replay_receipt.artifact_sha256 not in lane_c_feature_seals[fold].evidence_sha256
        for fold in required_folds
    ):
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("FEATURE_SEAL_PARENT_OR_EVIDENCE_MISMATCH",))
    if lane_c_scoring_card.feature_seal_sha256 != lane_c_feature_seals[fold_id].artifact_sha256:
        return None, seal_lane_disposition(spec, "C", "REJECTED", parent_hash,
                                           evidence, ("SCORING_CARD_FOLD_FEATURE_SEAL_MISMATCH",))
    evidence = evidence + tuple(
        lane_c_feature_seals[fold].artifact_sha256 for fold in required_folds
        if lane_c_feature_seals[fold].artifact_sha256 not in evidence
    )
    if fold_id == "H22":
        # H22 is development-only: it seals and evaluates its own pre-prediction card,
        # but it must never create a final A+B+C card or participate in H23 selection.
        return None, seal_lane_disposition(
            spec, "C", "QUALIFIED", parent_hash,
            evidence + (lane_c_scoring_card.artifact_sha256,
                        lane_c_prediction.artifact_sha256,
                        lane_c_evaluation.artifact_sha256),
            ("H22_DEVELOPMENT_EVALUATED",),
        )
    if lane_c_prediction.artifact_sha256 == parent_ab_prediction.artifact_sha256:
        return None, seal_lane_disposition(spec, "C", "ABSENT", parent_hash,
                                           evidence, ("PREDICTION_UNCHANGED",))
    card = seal_lane_c_card(
        spec, source_gate, lane_c_feature_seals, parent_ab_card, parent_ab_prediction, lane_c_scoring_card,
        lane_c_prediction, coverage, evidence, lane_c_evaluation,
    )
    disposition = seal_lane_disposition(
        spec, "C", "QUALIFIED", parent_hash,
        evidence + (card.artifact_sha256,), tuple(reason_codes),
    )
    return card, disposition
```

`seal_lane_c_scoring_card()` is fold-parameterized and runs before every Lane C H22/H23 prediction. It accepts only `fold_id in {"H22","H23"}`, requires the supplied `FeatureSeal.feature_set_id == "lane_c"` and equal `FeatureSeal.fold_id`, and requires `FeatureSeal.source_gate_sha256 == source_gate.artifact_sha256` before it constructs the payload's `source_gate_sha256`. It writes exactly `lane_c/{fold_id}/scoring_model_card.json`, preserves the A+B six-key weights byte-for-byte, and binds the A+B parent, calibration/coverage/replay evidence, and the universal RunSpec contract digest. Common FeatureSeals, cross-fold FeatureSeals, and source-gate mismatches fail before any card write.

`materialize_lane_c_prediction_rows(spec, fold_id, feature_seal, scoring_card) -> tuple[PredictionRow, ...]` is the sole Lane C scorer. It accepts only H22/H23, first requires the supplied `FeatureSeal.feature_set_id == "lane_c"` and equal `FeatureSeal.fold_id`, securely reloads exactly that target year's rows through the fold's seal, then securely reloads the declared `ScoringModelCard` and requires its `source_gate_sha256 == FeatureSeal.source_gate_sha256`. It then calls `resolve_prediction_weight_authority(spec, "A+B+C", verified_card)`. It rejects a wrong model/lane/fold/source-gate/FeatureSeal/RunSpec-contract/card digest, any row outside the fold population, duplicate game, or an authority that differs from the card's six-key integer weights. It obtains normalization parameters only from the already sealed fold-calibration artifact named by `ScoringModelCard.calibration_card_sha256`; fitting, labels, and H24 outcomes are forbidden here.

For every row, the materializer applies the exact Common `prediction_contract`: its declared weighted home-minus-away edge formula, declared weighted minimum-quality completeness formula, frozen normalization clip `[0.10, 0.90]`, and exact `home`/`away`/`none` pick rule. It sets `confidence = 10 * completeness`, carries the sealed `starter_edge_abs` and `feature_row_sha256`, sets `model_id="A+B+C"`, and populates `prediction_contract_sha256=spec.prediction_contract_sha256` and `scoring_model_card_sha256=verified_card.artifact_sha256`. It emits no label/outcome field. It ranks only non-neutral rows using Common `rank_daily` by the RunSpec tie order—`pick_probability desc`, `confidence desc`, `starter_edge_abs desc`, `scheduled_start_utc asc`, `game_pk asc`—assigns neutral rows rank `0`, and returns one deterministic `(official_date, scheduled_start_utc, game_pk)` ordered, unique game population.

`seal_lane_c_fold_prediction(spec, fold_id, rows)` accepts only that already materialized `tuple[PredictionRow, ...]`; it may validate the fold/model/universal-contract/unique-game invariants, but takes no FeatureSeal, card, raw row, calibration, or outcome argument and performs no scoring. It delegates persistence only to Common `seal_oof_predictions`, which writes exactly `oof/lane_c/A+B+C/{fold_id}/prediction_seal.json`; both H22 and H23 `PredictionSeal.frozen_model_card_sha256` values are `None`.

`build_lane_c_fold_result()` is the only evaluation/disposition boundary and always calls `seal_lane_disposition`; it never directly constructs `LaneDisposition`. With `fold_id="H22"`, it permits only development evaluation and returns no `FrozenModelCard`; its H22 scoring card, prediction seal, and evaluation cannot be substituted into or referenced by the final A+B+C card. With `fold_id="H23"`, it requires H22/H23 plus prediction-only H24 coverage/seals complete, at least one admitted row, replay success, a Lane C H23 scoring card and H23 prediction artifact digest different from parent A+B, and a sealed H23 `EvaluationReport`; only that H23 scoring card/seal/evaluation can produce `H23_FROZEN`. It never reads H24 outcomes. `ABSENT` and `REJECTED` never create a frozen card or candidate prediction artifact.

- [ ] **Step 5: Use only exact Common frozen-card and disposition persistence APIs.**

```python
def seal_lane_c_card(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    lane_c_feature_seals: Mapping[str, FeatureSeal],
    parent_ab_card: FrozenModelCard,
    parent_ab_prediction: PredictionSeal,
    lane_c_scoring_card: ScoringModelCard,
    lane_c_prediction: PredictionSeal,
    coverage: LaneCCoverageReport,
    evidence_sha256: Sequence[str],
    lane_c_evaluation: EvaluationReport,
) -> FrozenModelCard:
    if (lane_c_scoring_card.fold_id != "H23"
            or lane_c_scoring_card.feature_seal_sha256 != lane_c_feature_seals["H23"].artifact_sha256
            or lane_c_prediction.fold_id != "H23"):
        raise ValueError("final Lane C card accepts only the H23 scoring card and prediction seal")
    parent_weights = dict(parent_ab_card.parameters["component_weights_pp"])
    if (set(parent_weights) != {"starter", "offense", "bullpen", "environment", "defense", "schedule"}
            or any(type(value) is not int or value < 0 for value in parent_weights.values())
            or sum(parent_weights.values()) != 100):
        raise ValueError("A+B parent component weights differ")
    payload = {
        "model_id": "A+B+C",
        "state": "H23_FROZEN",
        "fold_id": "H23",
        "run_spec_sha256": run_spec_sha256(spec),
        "source_gate_sha256": source_gate.artifact_sha256,
        "baseline_replay_sha256": parent_ab_card.baseline_replay_sha256,
        "parent_card_sha256": parent_ab_card.artifact_sha256,
        "feature_seal_sha256": lane_c_feature_seals["H23"].artifact_sha256,
        "prediction_contract_sha256": spec.prediction_contract_sha256,
        "scoring_model_card_path": lane_c_scoring_card.path,
        "scoring_model_card_sha256": lane_c_scoring_card.artifact_sha256,
        "prediction_seal_sha256": lane_c_prediction.artifact_sha256,
        "evaluation_sha256": lane_c_evaluation.artifact_sha256,
        "parameters": {
            "component_weights_pp": dict(lane_c_scoring_card.parameters["component_weights_pp"]),
            "coverage_sha256": coverage_sha256(coverage),
            "parent_prediction_sha256": parent_ab_prediction.artifact_sha256,
            "evidence_sha256": tuple(evidence_sha256),
        },
    }
    return seal_frozen_model_card(spec, "lane_c", "H23/frozen_model_card.json", payload)
```

Use the Common frozen-card schema and exact `seal_frozen_model_card` payload/sealer/loader contract; do not add a `lane_c_frozen_card.schema.json`, direct mapping write, fabricated descriptor, or C-specific card schema. The Common card must require `state == "H23_FROZEN"`, `model_id == "A+B+C"`, `fold_id == "H23"`, parent A+B card hash, RunSpec `prediction_contract_sha256`, exact H23 Lane C scoring-card path/digest, exact H23 Lane C prediction seal, source-gate/H23-feature-seal/RunSpec/baseline-replay/evaluation hashes, and no outcome fields. The final card may bind the coverage digest as preflight evidence, but it must not reference an H22 scoring card, H22 prediction seal, H22 evaluation, or any substitute for the H23 scoring card/seal/evaluation. `parameters.component_weights_pp` must be byte-for-byte equal to both the A+B parent and the H23 scoring card’s validated six-key weights; Lane C changes feature evidence only and never weights. Before sealing, validate `lane_c_prediction.fold_id == "H23"`, `lane_c_prediction.prediction_contract_sha256 == spec.prediction_contract_sha256`, `lane_c_prediction.scoring_model_card_sha256 == lane_c_scoring_card.artifact_sha256`, and `lane_c_prediction.frozen_model_card_sha256 is None`; the final card is created only after H23 prediction/evaluation and is never used to authorize its own H23 seal. Use the generic `seal_lane_disposition`/`load_lane_disposition(spec, path, expected_sha256)` contract for all three states; do not add a `lane_c_disposition.schema.json`. H24 consumes the exact H24 lane_c `FeatureSeal` and remains outside this card/evaluation implementation.

The Common artifact-hash convention applies unchanged to the C card: `artifact_sha256` and `path` are runtime descriptor fields, excluded from the canonical frozen-card payload. `seal_frozen_model_card` writes/rereads the payload, hashes canonical persisted bytes before creating descriptor fields, and `load_frozen_model_card(spec, path, expected_sha256)` verifies the digest and the RunSpec-bound `lane_c` path.

- [ ] **Step 6: Run candidate tests and commit.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_candidate -v
```

Expected: neutral parts retain score 50/quality 0, no weights redistribute, all-neutral returns `ABSENT`, unchanged predictions return `ABSENT`, and failed coverage returns `REJECTED` with no card.

```bash
git add mlb_backtest/model_improvement/lane_c_candidate.py \
  tests/test_model_improvement_lane_c_candidate.py
git commit -m "feat: qualify Lane C against frozen A+B"
```

### Task 6: Verify handoff and preserve legacy isolation

**Files:**
- Create: `tests/test_model_improvement_lane_c_handoff.py`
- Modify: `mlb_backtest/model_improvement/lane_c_candidate.py`

- [ ] **Step 1: Write failing handoff tests.**

```python
class LaneCHandoffTest(unittest.TestCase):
    # setUp creates parent_feature_seals/lane_c_feature_seals and h24_rows from
    # sealed feature rows only; no outcome or H24 metric fixture is permitted.
    def test_h24_prediction_only_seal_is_required_and_outcome_free(self):
        self.assertIn("H24", self.lane_c_feature_seals)
        self.assertTrue(self.h24_rows)
        self.assertNotIn("home_win", self.h24_rows[0].__dict__)

    def test_complete_all_neutral_seals_absent_not_qualified(self):
        card, disposition = build_lane_c_fold_result(
            self.spec, "H23", self.complete_neutral_report, self.source_gate,
            self.parent_feature_seals, self.lane_c_feature_seals,
            self.replay_receipt,
            self.parent_ab_card, self.parent_ab_prediction,
            None, None, self.evaluation, self.evidence_sha256, self.reason_codes,
        )
        self.assertIsNone(card)
        self.assertEqual(disposition.state, "ABSENT")
        self.assertEqual(disposition.reason_codes, ("NO_ADMITTED_OBSERVATIONS",))

    def test_incomplete_h24_seal_is_rejected_without_card(self):
        card, disposition = build_lane_c_fold_result(
            self.spec, "H23", self.h24_incomplete_report, self.source_gate,
            self.parent_feature_seals, self.lane_c_feature_seals,
            self.replay_receipt,
            self.parent_ab_card, self.parent_ab_prediction,
            self.lane_c_h23_scoring_card, self.lane_c_h23_prediction, self.h23_evaluation, self.evidence_sha256,
            self.reason_codes,
        )
        self.assertIsNone(card)
        self.assertEqual(disposition.state, "REJECTED")
        self.assertEqual(disposition.reason_codes, ("COVERAGE_INCOMPLETE",))

    def test_h22_is_development_only_with_its_own_card_and_seal(self):
        card, disposition = build_lane_c_fold_result(
            self.spec, "H22", self.complete_report, self.source_gate,
            self.parent_feature_seals, self.lane_c_feature_seals,
            self.replay_receipt,
            self.parent_ab_card, self.parent_ab_prediction,
            self.lane_c_h22_scoring_card, self.lane_c_h22_prediction,
            self.h22_evaluation, self.evidence_sha256, self.reason_codes,
        )
        self.assertIsNone(card)
        self.assertEqual(disposition.state, "QUALIFIED")
        self.assertEqual(self.lane_c_h22_scoring_card.fold_id, "H22")
        self.assertEqual(self.lane_c_h22_scoring_card.feature_seal_sha256,
                         self.lane_c_feature_seals["H22"].artifact_sha256)
        self.assertIsNone(self.lane_c_h22_prediction.frozen_model_card_sha256)
        self.assertEqual(self.lane_c_h22_prediction.scoring_model_card_sha256,
                         self.lane_c_h22_scoring_card.artifact_sha256)

    def test_qualified_card_is_h23_frozen_and_parent_bound(self):
        card = seal_lane_c_card(
            self.spec, self.source_gate, self.lane_c_feature_seals, self.parent_ab_card,
            self.parent_ab_prediction, self.lane_c_h23_scoring_card, self.different_lane_c_h23_prediction,
            self.qualified_report, self.evidence_sha256, self.h23_evaluation,
        )
        self.assertEqual(card.state, "H23_FROZEN")
        self.assertEqual(card.parent_card_sha256, self.parent_ab_card.artifact_sha256)
        self.assertEqual(card.parameters["component_weights_pp"],
                         self.parent_ab_card.parameters["component_weights_pp"])
        self.assertNotEqual(card.prediction_seal_sha256, self.parent_ab_prediction.artifact_sha256)
        self.assertEqual(card.scoring_model_card_sha256,
                         self.lane_c_h23_scoring_card.artifact_sha256)
        self.assertEqual(card.prediction_seal_sha256,
                         self.different_lane_c_h23_prediction.artifact_sha256)
```

- [ ] **Step 2: Run handoff tests to verify RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_c_handoff -v
```

Expected: `ERROR` until the generic disposition/card handoff is wired.

- [ ] **Step 3: Add path-bound immutable writes through the Common card and disposition APIs.**

Use `spec.output_path("lane_c", relative)` for Lane C feature, scoring-card, final-card, and disposition paths, and the Common OOF path `oof/lane_c/A+B+C/{fold_id}/prediction_seal.json` for H22/H23 prediction seals. The Common sealers reread canonical bytes and compute the digest before returning; load dispositions only with `load_lane_disposition(spec, Path(disposition.path), disposition.artifact_sha256)`. Do not call H24 outcome/evaluation or candidate-registry code. H22 writes only its development scoring card, prediction seal, evaluation, and generic disposition; H23 alone can write one Common frozen card and generic qualified disposition; `ABSENT`/`REJECTED` writes only the generic disposition.

- [ ] **Step 4: Run all Lane C and adjacent offline regressions.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_model_improvement_lane_c_contracts \
  tests.test_model_improvement_lane_c_coverage \
  tests.test_model_improvement_lane_c_replay \
  tests.test_model_improvement_lane_c_candidate \
  tests.test_model_improvement_lane_c_handoff -v
```

Expected: all Lane C tests pass, including H22/H23/H24 coverage completeness, exact cutoff, complete replay evidence, neutral fallback, no redistribution, rejection of Common/non-`lane_c`, wrong-fold, and source-gate-mismatched FeatureSeals before card write/materialization, secure fold-bound label-free `PredictionRow` materialization with deterministic unique population/ranking, independent H22/H23 scoring-card and prediction-seal bindings, H22 development-only no-final-card behavior, generic `ABSENT`/`REJECTED`, H24 prediction-only sealing, and H23-only `H23_FROZEN` parent binding.

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_context_features tests.test_scoring_prep \
  tests.test_feature_engine tests.test_preflight \
  tests.test_v52_source_references -v
```

Expected: existing neutralization, as-of, required-source, and reference tests remain unchanged.

- [ ] **Step 5: Review the owned diff and commit.**

Run:

```bash
git diff --check
git status --short
```

Expected: only Lane C modules, schemas, and renamed `tests/test_model_improvement_lane_c_*.py` files are changed; `__init__.py`, legacy runtime, Common/Lane A, H24/registry, caches, and unrelated user files are untouched.

```bash
git add mlb_backtest/model_improvement/lane_c_contracts.py \
  mlb_backtest/model_improvement/lane_c_coverage.py \
  mlb_backtest/model_improvement/lane_c_replay.py \
  mlb_backtest/model_improvement/lane_c_candidate.py \
  schemas/model_improvement/lane_c_observation.schema.json \
  schemas/model_improvement/lane_c_coverage.schema.json \
  tests/test_model_improvement_lane_c_contracts.py \
  tests/test_model_improvement_lane_c_coverage.py \
  tests/test_model_improvement_lane_c_replay.py \
  tests/test_model_improvement_lane_c_candidate.py \
  tests/test_model_improvement_lane_c_handoff.py
git commit -m "test: verify Lane C source readiness and handoff"
```

## Dependency DAG and acquisition boundary

```text
Common ModelImprovementRunSpec + SourceGateReceipt + FeatureSeal/SealedFeatureRow
  -> exact Lane C slots/contracts/cutoffs
  -> H22/H23/H24 prediction-only expected keys and ADMITTED/NEUTRAL reconciliation
  -> raw/query/manifest/sidecar replay validation
  -> coverage_complete_by_fold[H22,H23,H24]
  -> candidate_qualified (H22+H23+H24 complete + admitted > 0 + deterministic replay PASS)
  -> observation-bound transforms and fixed context composition
  -> H22 ScoringModelCard + secure H22 FeatureSeal rows -> label-free A+B+C PredictionRows -> H22 prediction seal + development evaluation (no final card)
  -> H23 ScoringModelCard + secure H23 FeatureSeal rows -> label-free A+B+C PredictionRows -> H23 prediction seal + H23 evaluation
  -> H23 PredictionSeal differs from parent A+B PredictionSeal
  -> generic LaneDisposition QUALIFIED + H23_FROZEN FrozenModelCard (H23 inputs only)
     OR generic LaneDisposition ABSENT/REJECTED with reason and digests
  -> H24 consumes exact H24 lane_c FeatureSeal; registry/evaluation remain outside this plan
```

Real source acquisition is a separate plan. Its adapter must publish a `LogicalQuery`, exact raw bytes, manifest, query sidecar, source-contract digest, and parser. The parser must call `validate_source_row()` with the exact `expected_target_cutoff_utc()` derived from the Common RunSpec, supplied fold id, and sealed feature row, then replay must rerun that validation. Until such evidence exists, every optional slot is explicit `NEUTRAL`; no source value, coordinate, final-state weather/roof, defense, or catcher value may be fabricated.

## Self-review against the approved design

- Uses Common `ModelImprovementRunSpec`, `SourceGateReceipt`, `FeatureSeal`, `SealedFeatureRow`, and generic `LaneDisposition`; no legacy RunSpec, `.common` bridge, or `__init__.py` change.
- Uses one RunSpec-bound output root and exact slots: `park`, `weather_roof`, `oaa`, `framing`, `blocking_throwing`.
- Coverage keys include fold, target year, game, side, parent component, and source slot; missing and unknown remain distinct.
- Coverage completeness is separate from candidate qualification. H22, H23, and prediction-only H24 must be complete; at least one observation must be admitted; replay must pass; each H22/H23 Lane C scoring card must precede and bind its own secure, label-free `PredictionRow` population and prediction seal; and only the H23 prediction digest is compared with parent A+B for a final-card decision. H24 outcomes are never read.
- `seal_lane_c_scoring_card` and `materialize_lane_c_prediction_rows` both reject a non-`lane_c` FeatureSeal, a FeatureSeal from another fold, and a FeatureSeal/card/source-gate mismatch before writing/scoring. The materializer is the only Lane C prediction producer: it securely rereads the verified fold FeatureSeal and scoring card, calls the Common weight resolver, populates the RunSpec contract/card digests, and applies the Common edge/completeness/clip/pick/rank tie contract before `seal_lane_c_fold_prediction` consumes its tuple. The seal function never scores or accepts raw rows/card/FeatureSeal/outcomes.
- Every admitted observation proves schema, denominator, exact D-1/T-45 cutoff, query, raw, manifest, sidecar, and source-contract hashes; replay reruns schema/denominator/as-of validation.
- Neutral fallback remains score 50/quality 0 without weight redistribution; game-level context quality uses the minimum away/home component quality.
- H22 is development-only and never produces a `FrozenModelCard`. Qualified output is exactly one post-H23-evaluation `A+B+C` `FrozenModelCard` with `state == H23_FROZEN`, parent A+B hash, RunSpec `prediction_contract_sha256`, and the exact prior H23 Lane C scoring-card path/digest plus H23 prediction/evaluation; failed or non-distinct H23 output is generic `ABSENT`/`REJECTED` with reason and provenance digests.
- All tests use `tests/test_model_improvement_lane_c_*.py`; H24 outcome/registry evaluation and optional data acquisition remain outside this plan, while H24 lane_c feature sealing is required here.
