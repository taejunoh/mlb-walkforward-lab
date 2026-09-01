# Model Improvement Common and Lane A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the authoritative immutable experiment contract, authenticate real V5.2 source evidence, seal common fold artifacts, byte-replay v5.1, and freeze Lane A only after strict H23 improvement on an identical population.

**Architecture:** The RunSpec builder is the only authority for population, source evidence, cutoffs, exclusions, candidate declarations, and output paths; its real checked-in artifact is activated only after V5.2 acquisition supplies exact verified bindings. Common code lives in `mlb_backtest/model_improvement/`, has no legacy runtime side effects, and produces hash-verified immutable artifacts for B, C, and H24. V5.2 source verification is read-only and uses the explicit RunSpec-bound authority paths; missing real 2018–2021 input blocks real H22.

**Tech Stack:** Python 3 standard library, `mlb_backtest.protocols`, `execution_registry`, `v52_verify_sources`, v5.1 scoring/normalization/ranking/metrics, JSON Schema, `unittest`, Ruff, Git.

---

## Mandatory boundary and ownership

Do not execute a real H22, H23, or H24 fold until the source gate authenticates the actual `SOURCES_READY` V5.2 authority and deterministic replay for required 2018–2024 source seasons. A fixture can test parser behavior but can never become a source-gate receipt. Missing 2018–2021 data is a hard failure, never an imputation or neutral source substitute.

Only this plan creates `mlb_backtest/model_improvement/__init__.py`. Lane B/C must import the public API below; H24 alone creates the candidate registry and outcome controller. No `.common` module exists.

```python
from mlb_backtest.model_improvement.contracts import (
    FrozenModelCard, LaneDisposition, MetricPrimitive, PredictionRow, PredictionSeal,
    QualityRecipe, SealedFeatureRow, compute_feature_row_sha256, load_frozen_model_card,
    seal_frozen_model_card, sealed_feature_row_payload,
)
from mlb_backtest.model_improvement.baseline_replay import BaselineReplayReceipt, load_baseline_replay
from mlb_backtest.model_improvement.run_spec import (
    ModelImprovementRunSpec, load_run_spec, load_verified_run_spec, run_spec_sha256,
)
from mlb_backtest.model_improvement.source_gate import (
    SourceGateReceipt, load_source_gate_receipt, require_source_gate, verify_source_gate,
)
from mlb_backtest.model_improvement.sealed_features import (
    FeatureSeal, load_feature_seal, load_sealed_feature_rows, seal_feature_rows,
)
from mlb_backtest.model_improvement.oof_ledger import (
    JoinedLedger, join_oof_outcomes, load_joined_ledger, load_prediction_seal,
    load_joined_prediction_rows, load_prediction_rows, seal_oof_predictions,
)
from mlb_backtest.model_improvement.lane_a import LaneAFoldResult, fit_lane_a_fold, load_lane_a_fold
from mlb_backtest.model_improvement.evaluation import EvaluationReport, evaluate_predictions, load_evaluation_report
from mlb_backtest.model_improvement.lane_disposition import load_lane_disposition, seal_lane_disposition
```

`canonical_json_bytes()` returns sorted UTF-8 compact JSON with exactly one trailing newline. Immutable writers exclusively create, fsync, reread, hash, and reject symlinks/non-regular files. Callers must not append a newline.

**Artifact hash convention:** `artifact_sha256` and `path` are runtime descriptor fields, never fields inside the canonical persisted payload they describe. A writer serializes `payload = artifact.as_mapping()` without those descriptor fields, persists canonical payload bytes, computes `sha256(payload_bytes)`, optionally writes that digest/path in a separate sidecar, then constructs the returned descriptor. Every `as_mapping()` for `FrozenModelCard`, `LaneDisposition`, `PredictionSeal`, `FeatureSeal`, `SourceGateReceipt`, `JoinedLedger`, `BaselineReplayReceipt`, and `EvaluationReport` excludes runtime `artifact_sha256` and `path`. Every loader reads canonical payload bytes from a regular file, recomputes the digest, compares it with the caller’s expected digest and any sidecar digest, validates the closed payload schema and RunSpec path, then constructs the descriptor. No payload contains or hashes its own digest.

**Prediction binding convention:** `PredictionSeal.prediction_contract_sha256` is the immutable RunSpec-declared digest of the scoring, ranking, pick, clipping, and tie-break contract; the sealer validates it against `spec.prediction_contract_sha256` and it is never the prediction artifact digest. `FrozenModelCard.prediction_contract_sha256` deliberately means the bound `PredictionSeal.artifact_sha256`, and `FrozenModelCard.prediction_seal_sha256` is the same prediction artifact digest when both fields are retained by the card schema. This name overlap is intentional only at the card boundary and must be validated explicitly.

## Files and namespaces

| File | Responsibility |
| --- | --- |
| `protocols/model_improvement/parallel_model_result_v1.json` | Complete immutable experiment declaration, created only after verified 2018–2021 acquisition. |
| `schemas/model_improvement/{parallel_model_result_run_spec,source_gate_receipt,feature_seal,prediction_seal,frozen_model_card,lane_disposition}.schema.json` | Closed schemas for every shared artifact. |
| `mlb_backtest/model_improvement/canonical.py` | Canonical serialization and immutable filesystem operations. |
| `mlb_backtest/model_improvement/contracts.py` | Shared frozen types and mapping validation. |
| `mlb_backtest/model_improvement/lane_disposition.py` | Immutable QUALIFIED, ABSENT, or REJECTED lane evidence. |
| `mlb_backtest/model_improvement/run_spec.py` | Closed RunSpec parser, digest, and namespace paths. |
| `mlb_backtest/model_improvement/source_gate.py` | Read-only V5.2 authority authentication. |
| `mlb_backtest/model_improvement/sealed_features.py` | Full component-side result-free feature seals. |
| `mlb_backtest/model_improvement/baseline_replay.py` | Exact v5.1 bytes and receipt. |
| `mlb_backtest/model_improvement/oof_ledger.py` | Prediction-before-outcome sealing and joined ledgers. |
| `mlb_backtest/model_improvement/evaluation.py` | Identical-population metrics and date pairing. |
| `mlb_backtest/model_improvement/lane_a.py` | Rolling-origin calibration and H23 freeze. |

RunSpec `outputs.namespaces` contains exactly `source_gate`, `features`, `baseline`, `oof`, `lane_a`, `lane_b`, `lane_c`, `registry`, `h24_predictions`, and `h24_reports`. Every output is made with `spec.output_path(namespace, relative_name)` and cannot escape `outputs.root`. Within `features`, only `features/common/H22`, `features/common/H23`, `features/common/H24`, `features/lane_c/H22`, `features/lane_c/H23`, and prediction-only `features/lane_c/H24` are admitted; a feature-seal path is exactly `features/{feature_set_id}/{fold_id}/feature_seal.json` with its row file in the same directory.

```text
canonical + contracts -> RunSpec -> source gate -> feature seals + v5.1 replay
  -> OOF prediction/join seals + evaluation -> Lane A H22/H23 freeze
  -> Lane B/C frozen inputs -> H24 registry and outcome work
```

### Task 1: Canonical immutable artifact primitives

**Files:**
- Create: `mlb_backtest/model_improvement/__init__.py`
- Create: `mlb_backtest/model_improvement/canonical.py`
- Create: `tests/test_model_improvement_canonical.py`

- [ ] **Step 1: Write a failing `unittest.TestCase` covering compact sorted bytes, exactly one newline, exclusive creation, identical retry, changed retry, symlink denial, and reread denial.**

```python
class CanonicalArtifactTest(unittest.TestCase):
    def test_immutable_json_is_canonical_and_verified(self):
        self.assertEqual(canonical_json_bytes({"b": 2, "a": 1}), b'{"a":1,"b":2}\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.json"
            self.assertTrue(write_immutable_json(path, {"a": 1}))
            self.assertFalse(write_immutable_json(path, {"a": 1}))
            with self.assertRaisesRegex(ValueError, "immutable artifact differs"):
                write_immutable_json(path, {"a": 2})
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_canonical -v`

Expected: FAIL with `ModuleNotFoundError` for `mlb_backtest.model_improvement`.

- [ ] **Step 3: Implement `canonical_json_bytes(value: object) -> bytes`, `sha256_bytes(value: bytes) -> str`, `read_regular_bytes(path: Path, label: str) -> bytes`, and `write_immutable_json(path: Path, value: object) -> bool`.** Use `json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))`, `os.O_CREAT | os.O_EXCL`, `os.fsync`, and full byte equality after reread.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_canonical -v`

Expected: PASS; changed or non-regular artifacts fail closed.

- [ ] **Step 5: Commit.**

```bash
git add mlb_backtest/model_improvement/__init__.py mlb_backtest/model_improvement/canonical.py tests/test_model_improvement_canonical.py
git commit -m "feat: add immutable model improvement artifacts"
```

### Task 2A: Define pure shared immutable contracts

**Files:**
- Create: `mlb_backtest/model_improvement/contracts.py`
- Create: `mlb_backtest/model_improvement/lane_disposition.py`
- Create: `schemas/model_improvement/lane_disposition.schema.json`
- Create: `tests/test_model_improvement_contracts.py`

- [ ] **Step 1: Write failing, self-contained contract tests.** These tests have no RunSpec, artifact-path, filesystem, or synthetic-spec dependency. Define explicit local factories for fixed, ratio, and weighted recipes; a primitive; a feature-row payload and row; a prediction seal; and a lane disposition. Cover complete component/side maps, finite bounds, row digest recomputation, nested immutability, fresh JSON values from `as_mapping()`, and descriptor-free payload round trips.

```python
_D = "a" * 64
_SIDES = ("away", "home")
_COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")

def make_fixed_quality_recipe(*, fixed_value=0.0):
    return QualityRecipe("fixed", "fixed", None, None, None, fixed_value, (), (), _D)

def make_ratio_quality_recipe():
    return QualityRecipe("ratio", "ratio", "starter", 1.0, 1.0, None, (), (), _D)

def make_weighted_quality_recipe(*, child_weights=(0.4, 0.6)):
    children = (make_ratio_quality_recipe(), make_ratio_quality_recipe())
    return QualityRecipe("weighted", "weighted_sum", "recent_form", None, None, None,
                         children, child_weights, _D)

def make_primitive(recipe=None, *, weight=1.0):
    return MetricPrimitive(60.0, weight, recipe or make_ratio_quality_recipe())

def make_feature_row_payload(*, game_pk=7, target_year=2022, starter_weights=(1.0,), scores=None):
    scores = scores or {side: {component: 60.0 for component in _COMPONENTS} for side in _SIDES}
    qualities = {side: {component: 50.0 for component in _COMPONENTS} for side in _SIDES}
    primitives = {
        side: {component: tuple(make_primitive(weight=weight) for weight in starter_weights)
               for component in _COMPONENTS}
        for side in _SIDES
    }
    return {
        "game_pk": game_pk, "official_date": f"{target_year}-04-01",
        "scheduled_start_utc": f"{target_year}-04-01T19:00:00.000000Z", "target_year": target_year,
        "max_history_date": f"{target_year - 1}-12-31", "source_gate_sha256": _D,
        "source_manifest_sha256": _D, "exclusion_policy_sha256": _D,
        "component_scores": scores, "component_qualities": qualities,
        "baseline_component_scores": scores, "baseline_component_qualities": qualities,
        "metric_primitives": primitives,
        "component_inputs_sha256": {side: {component: _D for component in _COMPONENTS} for side in _SIDES},
        "starter_edge_abs": 0.0,
    }

def make_feature_row(payload=None, **kwargs):
    payload = dict(payload or make_feature_row_payload(**kwargs))
    return SealedFeatureRow(**payload,
        feature_row_sha256=compute_feature_row_sha256(payload))

def make_seal():
    return PredictionSeal("O19", _D, _D, _D, _D, _D, _D, _D, "test/prediction_seal.json")

def make_lane_disposition():
    return LaneDisposition("A", "REJECTED", _D, None, (_D,), ("H23_GATE",), _D,
                           "test/lane_disposition.json")

class ContractTest(unittest.TestCase):
    def test_feature_row_requires_complete_component_sides_and_rematerializable_primitives(self):
        row = make_feature_row(game_pk=7, target_year=2022)
        self.assertEqual(row.component_scores["home"]["starter"], 60.0)
        starter = row.metric_primitives["home"]["starter"][0]
        self.assertEqual(starter.normalized_score, 60.0)
        self.assertEqual(starter.aggregation_weight, 1.0)
        self.assertEqual(starter.quality_recipe.kind, "ratio")
        self.assertEqual(starter.quality_recipe.child_weights, ())
        weighted = make_weighted_quality_recipe()
        self.assertEqual(weighted.child_weights, (0.4, 0.6))
        with self.assertRaisesRegex(ValueError, "child weights"):
            make_weighted_quality_recipe(child_weights=(1.0,))
        with self.assertRaisesRegex(ValueError, "aggregation weights"):
            make_feature_row(game_pk=10, target_year=2022, starter_weights=(0.2, 0.7))
        with self.assertRaisesRegex(ValueError, "component keys differ"):
            make_feature_row(game_pk=8, target_year=2022, scores={"home": {}, "away": {}})

    def test_row_hash_and_recursive_immutability(self):
        payload = make_feature_row_payload(game_pk=7, target_year=2022)
        row = make_feature_row(payload)
        self.assertEqual(row.feature_row_sha256, compute_feature_row_sha256(sealed_feature_row_payload(row)))
        with self.assertRaises(TypeError):
            row.component_scores["home"]["starter"] = 1.0
        with self.assertRaises(TypeError):
            row.metric_primitives["home"]["starter"] = ()
        self.assertIsInstance(row.metric_primitives["home"]["starter"][0].quality_recipe.children, tuple)
        self.assertIsInstance(row.metric_primitives["home"]["starter"][0].quality_recipe.child_weights, tuple)
        exported = row.as_mapping()
        exported["component_scores"]["home"]["starter"] = 1.0
        self.assertEqual(row.component_scores["home"]["starter"], 60.0)

    def test_prediction_seal_and_disposition_payload_round_trip(self):
        seal_payload = make_seal().as_mapping()
        self.assertNotIn("artifact_sha256", seal_payload)
        self.assertNotIn("path", seal_payload)
        self.assertEqual(
            PredictionSeal.from_payload_mapping(seal_payload, make_seal().artifact_sha256, make_seal().path).ledger_id,
            make_seal().ledger_id,
        )
        disposition = make_lane_disposition()
        self.assertEqual(LaneDisposition.from_payload_mapping(
            disposition.as_mapping(), disposition.artifact_sha256, disposition.path), disposition)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_contracts -v`

Expected: FAIL with `ModuleNotFoundError` for `contracts`.

- [ ] **Step 3: Implement exactly these frozen dataclasses, pure validation/projection helpers, and validated `as_mapping()`/`from_payload_mapping(payload, artifact_sha256, path)` methods.** This task is pure: it has no RunSpec or filesystem dependency and does not implement a card/disposition loader or sealer.

```python
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

@dataclass(frozen=True)
class QualityRecipe:
    recipe_id: str
    kind: str
    family: str | None
    numerator: float | None
    baseline_constant: float | None
    fixed_value: float | None
    children: Sequence["QualityRecipe"]
    child_weights: Sequence[float]
    transform_sha256: str

@dataclass(frozen=True)
class MetricPrimitive:
    # metric_score(value, mean, sd, direction) before quality/shrinkage is applied
    normalized_score: float
    aggregation_weight: float
    quality_recipe: QualityRecipe

@dataclass(frozen=True)
class SealedFeatureRow:
    game_pk: int; official_date: str; scheduled_start_utc: str; target_year: int
    max_history_date: str; source_gate_sha256: str; source_manifest_sha256: str
    exclusion_policy_sha256: str; component_scores: Mapping[str, Mapping[str, float]]
    component_qualities: Mapping[str, Mapping[str, float]]
    baseline_component_scores: Mapping[str, Mapping[str, float]]
    baseline_component_qualities: Mapping[str, Mapping[str, float]]
    metric_primitives: Mapping[str, Mapping[str, Sequence[MetricPrimitive]]]
    component_inputs_sha256: Mapping[str, Mapping[str, str]]; starter_edge_abs: float; feature_row_sha256: str

@dataclass(frozen=True)
class PredictionRow:
    model_id: str; game_pk: int; official_date: str; target_year: int; p_home: float
    picked_side: str; pick_probability: float; confidence: float; starter_edge_abs: float
    scheduled_start_utc: str; rank: int; feature_row_sha256: str; model_card_sha256: str

@dataclass(frozen=True)
class PredictionSeal:
    ledger_id: str; run_spec_sha256: str; source_gate_sha256: str
    prediction_contract_sha256: str; model_card_sha256: str; population_sha256: str
    rows_sha256: str; artifact_sha256: str; path: str

@dataclass(frozen=True)
class FrozenModelCard:
    model_id: str; state: str; fold_id: str; run_spec_sha256: str; source_gate_sha256: str
    baseline_replay_sha256: str; parent_card_sha256: str | None; feature_seal_sha256: str
    prediction_contract_sha256: str; prediction_seal_sha256: str; evaluation_sha256: str
    parameters: Mapping[str, object]; artifact_sha256: str; path: str

@dataclass(frozen=True)
class LaneDisposition:
    lane_id: str; state: str; run_spec_sha256: str; parent_card_sha256: str | None
    evidence_sha256: Sequence[str]; reason_codes: Sequence[str]; artifact_sha256: str; path: str
```

Close these exact domains: `MODEL_IDS={"v5.1","A","A+B","A+B+C"}`, `FROZEN_CARD_MODEL_IDS={"A","A+B","A+B+C"}`, `FROZEN_CARD_STATES={"H23_FROZEN"}`, `LANE_IDS={"A","B","C"}`, `LANE_DISPOSITION_STATES={"QUALIFIED","ABSENT","REJECTED"}`, `LEDGER_IDS={"O19","O20","O21","O22","O23","H22","H23","H24"}`, `PICKED_SIDES={"away","home","none"}`, and `QUALITY_FAMILIES={None,"starter","hitter_split","recent_form","bullpen"}`. Enforce frozen-card model/state/fold compatibility, row model ID, picked side, and rank (`0` only for `none`; strictly positive otherwise), and recipe family. Lane-specific parameter keys remain open here but their JSON values are recursively immutable.

Implement `_freeze_json(value)` and `_thaw_json(value)`: mapping inputs become recursive `MappingProxyType`, sequences become tuples, and scalars must be JSON scalars. Apply them to card parameters, feature-row maps, recipe children/weights, and disposition evidence/reasons; `as_mapping()` always returns fresh mutable JSON-compatible values. Validate exact `SIDES = ("away", "home")` and `COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")`, ISO dates/UTC strings, score and quality [0,100], probability [0,1], 64 lowercase hex digests, and the environment/defense recipe fallback of 50/0. `MetricPrimitive.normalized_score` is the pre-quality `normalize.metric_score(value, mean, sd, direction)` result in [0,100], never an already quality-shrunk score. Preserve ordered primitives and require their nonnegative aggregation weights to sum to exactly one under the documented `math.fsum` tolerance. Keep the fixed/ratio/weighted-sum/minimum recipe constraints in this task.

Add `sealed_feature_row_payload(row: SealedFeatureRow | Mapping[str, object]) -> Mapping[str, object>` which projects every persisted row field except `feature_row_sha256` from an immutable row or a fresh JSON row mapping, and `compute_feature_row_sha256(payload: Mapping[str, object]) -> str`, using Task 1 canonical SHA-256. `SealedFeatureRow.__post_init__` must require `feature_row_sha256 == compute_feature_row_sha256(sealed_feature_row_payload(self))`. This digest is separate from `FeatureSeal.rows_sha256`, which remains a digest over fully materialized rows.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_contracts -v`

Expected: PASS; malformed or mutable shared payloads fail before lane code sees them.

- [ ] **Step 5: Commit.**

```bash
git add mlb_backtest/model_improvement/contracts.py mlb_backtest/model_improvement/lane_disposition.py schemas/model_improvement/lane_disposition.schema.json tests/test_model_improvement_contracts.py
git commit -m "feat: define model improvement contracts"
```

### Task 3: Implement the RunSpec builder and disposition persistence

**Files:**
- Create: `schemas/model_improvement/parallel_model_result_run_spec.schema.json`
- Create: `mlb_backtest/model_improvement/run_spec.py`
- Modify: `mlb_backtest/model_improvement/lane_disposition.py`
- Create: `tests/test_model_improvement_run_spec.py`
- Create: `tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json`

- [ ] **Step 1: Write table-driven RED tests for every immutable binding: code revision; source authority paths/digests; D-1/T-45; exclusions; H22/H23/H24 chronology; v5.1 baseline; metrics; output namespaces; Lane B grid/keep/selection; Lane C slots/qualification; H24/2025 firewall; and a dedicated disposition persistence round trip.**

```python
class RunSpecTest(unittest.TestCase):
    def test_exact_contract_is_available_to_sibling_lanes(self):
        spec = load_run_spec(SPEC_PATH)
        self.assertEqual(spec.fold("H22").normalization_years, (2018, 2019, 2020, 2021))
        self.assertEqual(spec.cutoffs, {"history": "D-1", "snapshot": "T-45"})
        self.assertEqual(spec.lane_b.keep_h22_count, 12)
        self.assertEqual(spec.lane_b.h23_order, ("brier", "log_loss", "daily_top1_delta_desc", "candidate_id"))
        self.assertRegex(spec.prediction_contract_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(str(spec.output_path("features", "lane_c/H24/feature_seal.json")).endswith("features/lane_c/H24/feature_seal.json"))
        self.assertTrue(spec.h24_firewall.forbid_outcomes_before_registry)

    def test_lane_disposition_round_trips_only_after_runspec_exists(self):
        spec = load_run_spec(SPEC_PATH)
        disposition = seal_lane_disposition(
            spec, "A", "REJECTED", None, ("a" * 64,), ("H23_GATE",))
        self.assertEqual(
            load_lane_disposition(spec, Path(disposition.path), disposition.artifact_sha256),
            disposition,
        )
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_run_spec tests.test_model_improvement_contracts -v`

Expected: FAIL with `ModuleNotFoundError` for `run_spec`.

- [ ] **Step 3: Add a recursively closed schema, `build_run_spec(raw: Mapping[str, object])`, and a synthetic fixture declaration.** Require top-level fields `schema_version`, `run_id`, `code_revision`, `source_authority`, `cutoffs`, `exclusions`, `folds`, `baseline`, `metrics`, `prediction_contract_sha256`, `lane_b`, `lane_c`, `outputs`, and `h24_firewall`. `prediction_contract_sha256` is the canonical digest of the declared scoring/ranking/pick/clipping/tie-break contract. `source_authority` binds authority root, V5.2 SourceRunSpec, acquisition contract, terminal checkpoint, inventory, and every validation receipt by relative path plus digest. The declaration binds the v5.1 card/digest and every output namespace. The synthetic fixture must have only test fixture paths/digests and must be rejected by real source verification.

For `lane_b`, declare the four baseline weights 35/25/20/5, fixed environment/defense 10/5, integer five-point grid bounded by ±10, nonnegative 85 total searched weight, multipliers `(0.75, 1.0, 1.25)`, exact 210 candidates, H22 `keep_h22_count` 12 ordered `(brier, log_loss, daily_top1_delta_desc, candidate_id)`, and H23 eligibility `(Brier/logloss no worse than A; positive paired Top-1 versus v5.1)` with the same final order. For `lane_c`, declare exactly `environment=(park, weather_roof)` and `defense=(oaa, framing, blocking_throwing)`, H22/H23 coverage with required 100% admitted-or-neutral disposition/replay, and prediction-only H24 feature coverage with no outcome access; every Lane C feature set has parent `A+B`. Metrics are all-game Brier/logloss and date-paired Top-1. H24 forbids outcome access before registry and all 2025 outcomes.

- [ ] **Step 4: Implement `ModelImprovementRunSpec`, `build_run_spec(raw: Mapping[str, object])`, `parse_run_spec(raw: object)`, `load_run_spec(path: Path)`, `load_verified_run_spec(path: Path)`, `run_spec_sha256(spec)`, `spec.fold(fold_id)`, and `spec.output_path(namespace, relative_name)`.** `fold` accepts H22/H23/H24 only; `output_path` accepts ten declared namespaces and a normal relative name without `..`. For `features`, it additionally accepts only `common/H22`, `common/H23`, `common/H24`, `lane_c/H22`, `lane_c/H23`, or prediction-only `lane_c/H24` followed by the expected `feature_seal.json` or `rows.jsonl` filename. Validation recomputes every fold’s rolling OOF chronology and exactly 210 Lane B candidates. The builder freezes all candidate/metric rules before any H22 outcome can be joined.

Add the only disposition persistence APIs after `run_spec.py` exists:

```python
def seal_lane_disposition(
    spec: ModelImprovementRunSpec, lane_id: Literal["A", "B", "C"],
    state: Literal["QUALIFIED", "ABSENT", "REJECTED"],
    parent_card_sha256: str | None, evidence_sha256: Sequence[str],
    reason_codes: Sequence[str],
) -> LaneDisposition

def load_lane_disposition(
    spec: ModelImprovementRunSpec, path: Path, expected_sha256: str,
) -> LaneDisposition
```

`seal_lane_disposition` projects the pure contract payload, writes it under the declared `lane_{a,b,c}` namespace, rereads the regular canonical bytes, hashes those bytes before creating descriptor fields, and returns the loader result. `load_lane_disposition` verifies regular canonical bytes, expected digest/sidecar, the closed schema, exact RunSpec-bound namespace path, and excludes runtime `artifact_sha256`/`path` from its payload. It never accepts a fabricated descriptor.

- [ ] **Step 5: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_run_spec -v`

Expected: PASS; any unknown key or binding mutation fails closed.

- [ ] **Step 6: Commit.**

```bash
git add schemas/model_improvement/parallel_model_result_run_spec.schema.json mlb_backtest/model_improvement/run_spec.py mlb_backtest/model_improvement/lane_disposition.py tests/test_model_improvement_run_spec.py tests/test_model_improvement_contracts.py tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json
git commit -m "feat: define model improvement run spec builder"
```

### Task 4: Authenticate the actual V5.2 source gate

**Files:**
- Create: `schemas/model_improvement/source_gate_receipt.schema.json`
- Create: `mlb_backtest/model_improvement/source_gate.py`
- Create: `tests/test_model_improvement_source_gate.py`

- [ ] **Step 1: Write RED tests rejecting a receipt digest/RunSpec mismatch, asserting persisted source-gate payload/JSON Schema excludes `artifact_sha256` and `path`, and proving the current real H22 preflight raises because verified 2018–2021 sources are absent.**

```python
class SourceGateTest(unittest.TestCase):
    def test_real_h22_preflight_stays_blocked_without_verified_2018_inputs(self):
        with self.assertRaisesRegex(ValueError, "2018|SOURCES_READY|source"):
            verify_source_gate(load_run_spec(SPEC_PATH))

    def test_receipt_loader_checks_expected_digest(self):
        with self.assertRaisesRegex(ValueError, "source gate digest differs"):
            load_source_gate_receipt(FIXTURE_RECEIPT, "0" * 64)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_source_gate -v`

Expected: FAIL with `ModuleNotFoundError` for `source_gate`.

- [ ] **Step 3: Implement persisted `SourceGateReceipt` payload fields `run_spec_sha256`, `source_run_spec_sha256`, `acquisition_contract_sha256`, `checkpoint_sha256`, `inventory_sha256`, `validation_receipts_sha256`, `snapshot_replay_sha256`, `required_years`, and `status`; attach runtime descriptor fields `artifact_sha256` and `path` only after hashing canonical payload bytes. Implement `verify_source_gate(spec)`, `load_source_gate_receipt(path, expected_sha256)`, and `require_source_gate(spec)`.**

`verify_source_gate` must call `protocols.load_source_run_spec`, `execution_registry.validate_sources_ready_evidence_set`, `execution_registry.load_terminal_acquisition_checkpoint`, `v52_verify_sources.load_snapshot_replay_inputs`, and `v52_verify_sources.replay_verified_snapshots` with only paths declared in RunSpec. Check every bound file digest before and after those calls, require the replayed six source families across 2018–2024, then write the receipt only in `source_gate`. It accepts neither arbitrary bytes nor caller-supplied inventory/authority/output path. `require_source_gate` rereads the receipt and reruns verification, requiring equal digest.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_source_gate -v`

Expected: PASS; fixtures validate shape, but absent real 2018–2021 input remains blocked.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/source_gate_receipt.schema.json mlb_backtest/model_improvement/source_gate.py tests/test_model_improvement_source_gate.py
git commit -m "feat: authenticate model improvement source gate"
```

- [ ] **Step 6: Stop real-fold activation until acquisition supplies verified 2018–2021 evidence and exact authority paths/digests.** Do not create `protocols/model_improvement/parallel_model_result_v1.json` with invented or placeholder values.

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_source_gate.SourceGateTest.test_real_h22_preflight_stays_blocked_without_verified_2018_inputs -v`

Expected: PASS because the test observes the source-gate block; the real protocol file remains absent.

- [ ] **Step 7: After the external acquisition prerequisite is verified, invoke `build_run_spec()` with exact authenticated authority paths/digests and exclusively publish `protocols/model_improvement/parallel_model_result_v1.json`.** Immediately call `verify_source_gate()` and require the receipt to bind that exact RunSpec digest before any H22 feature task starts.

### Task 5: Seal full result-free feature primitives

**Files:**
- Create: `schemas/model_improvement/feature_seal.schema.json`
- Create: `mlb_backtest/model_improvement/sealed_features.py`
- Create: `tests/test_model_improvement_sealed_features.py`

- [ ] **Step 1: Write RED tests for six component sides, D-1/T-45 provenance, 2025/future/target outcome rejection, duplicate game rejection, environment/defense 50/0 fallback, and permitted Lane B denominator recipes.**

```python
class SealedFeatureTest(unittest.TestCase):
    def test_seal_rereads_complete_result_free_rows(self):
        seal = seal_feature_rows(spec, "common", "H22", complete_rows)
        self.assertEqual(seal.schema, "MODEL_IMPROVEMENT_FEATURE_SEAL_V1")
        self.assertEqual(seal.fold_id, "H22")
        self.assertNotIn("artifact_sha256", seal.as_mapping())
        self.assertNotIn("path", seal.as_mapping())
        self.assertEqual(load_feature_seal(Path(seal.path), seal.artifact_sha256), seal)
        self.assertEqual(load_sealed_feature_rows(spec, (2022,), seal), tuple(complete_rows))

    def test_outcome_or_future_provenance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outcome|history|cutoff"):
            seal_feature_rows(spec, "common", "H22", [row_with_forbidden_home_win])

    def test_lane_c_feature_seal_requires_parent_and_evidence(self):
        common = seal_feature_rows(spec, "common", "H22", complete_rows)
        lane_c = seal_feature_rows(spec, "lane_c", "H22", complete_rows, common.artifact_sha256, ("b" * 64,))
        self.assertEqual(lane_c.parent_feature_seal_sha256, common.artifact_sha256)
        self.assertEqual(lane_c.evidence_sha256, ("b" * 64,))
        with self.assertRaisesRegex(ValueError, "parent feature seal|evidence"):
            seal_feature_rows(spec, "lane_c", "H22", complete_rows)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_sealed_features -v`

Expected: FAIL with `ModuleNotFoundError` for `sealed_features`.

- [ ] **Step 3: Implement this exact public `FeatureSeal` and the loader/sealer API.**

```python
@dataclass(frozen=True)
class FeatureSeal:
    schema: str
    feature_set_id: str
    fold_id: str
    run_spec_sha256: str
    source_gate_sha256: str
    population_sha256: str
    rows_sha256: str
    parent_feature_seal_sha256: str | None
    evidence_sha256: tuple
    artifact_sha256: str
    path: str
```

Persist exactly `schema="MODEL_IMPROVEMENT_FEATURE_SEAL_V1"`, `feature_set_id`, `fold_id`, `run_spec_sha256`, `source_gate_sha256`, `population_sha256`, `rows_sha256`, `parent_feature_seal_sha256`, and `evidence_sha256`; `artifact_sha256` and `path` are returned runtime descriptors and excluded by `FeatureSeal.as_mapping()`. `feature_set_id` is closed to `common` and `lane_c`. A `common` seal requires `parent_feature_seal_sha256 is None` and empty `evidence_sha256`; a `lane_c` seal requires a nonempty parent common feature-seal digest for the same fold and a nonempty immutable evidence-digest tuple. The exact public signatures are `seal_feature_rows(spec: ModelImprovementRunSpec, feature_set_id: str, fold_id: str, rows: Sequence[SealedFeatureRow], parent_feature_seal_sha256: str | None = None, evidence_sha256: Sequence[str] = ()) -> FeatureSeal`, `load_feature_seal(path: Path, expected_sha256: str) -> FeatureSeal`, and `load_sealed_feature_rows(spec: ModelImprovementRunSpec, years: Sequence[int], seal: FeatureSeal) -> tuple[SealedFeatureRow, ...]`. This contract deliberately has no `target_years` field: callers select rows with the existing `years` parameter, while the accepted year set is validated from each row’s `target_year` and the declared `fold_id`; the loader returns that immutable tuple. `load_feature_seal` validates the structural `features/{feature_set_id}/{fold_id}/` path and common/lane-C parent/evidence shape; `load_sealed_feature_rows` additionally validates the descriptor against the supplied RunSpec, source gate, requested years, and persisted rows. Both recompute payload/row digests and reject a seal from another feature set. Validate source-gate/source-manifest/exclusion bindings, all component input hashes, baseline component score/quality preservation, and exactly earlier history. Validate each `MetricPrimitive` tree and preserve its normalized score, aggregation weight, recursive quality recipe, and child order. The only multiplier-enabled recipe families are existing starter/hitter-split, recent-form, and bullpen denominators; every other family rejects a Lane B multiplier. Seal JSONL rows and the feature-seal payload under `features`, hash/reread the payload, and load it through the same fields before returning.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_sealed_features -v`

Expected: PASS; B has sufficient primitives and C starts with explicit neutral slots.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/feature_seal.schema.json mlb_backtest/model_improvement/sealed_features.py tests/test_model_improvement_sealed_features.py
git commit -m "feat: seal model improvement feature primitives"
```

### Task 6: Replay v5.1 exactly and evaluate identical populations

**Files:**
- Create: `mlb_backtest/model_improvement/baseline_replay.py`
- Create: `mlb_backtest/model_improvement/evaluation.py`
- Create: `tests/test_model_improvement_baseline_replay.py`
- Create: `tests/test_model_improvement_evaluation.py`

- [ ] **Step 1: Write RED tests requiring two byte-identical v5.1 replays and rejecting a missing game before Brier/logloss/date-paired Top-1 metrics are calculated.**

```python
class EvaluationTest(unittest.TestCase):
    def test_top1_pairs_by_date_even_when_models_choose_different_games(self):
        candidate_report, baseline_report = evaluate_predictions(candidate_seal, baseline_seal, outcomes)
        self.assertEqual(candidate_report.all_games_count, 4)
        self.assertEqual(candidate_report.daily_top1_count, 2)
        self.assertEqual(candidate_report.population_sha256, baseline_report.population_sha256)
        self.assertEqual(candidate_report.paired_days_sha256, baseline_report.paired_days_sha256)
        self.assertEqual(baseline_report.daily_top1_delta_vs_baseline, 0)
        self.assertEqual(
            load_evaluation_report(Path(candidate_report.path), candidate_report.artifact_sha256),
            candidate_report,
        )

    def test_mismatched_population_fails_before_metrics(self):
        with self.assertRaisesRegex(ValueError, "identical eligible population"):
            evaluate_predictions(short_candidate_seal, baseline_seal, outcomes)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_baseline_replay tests.test_model_improvement_evaluation -v`

Expected: FAIL with `ModuleNotFoundError` for `baseline_replay` or `evaluation`.

- [ ] **Step 3: Implement `BaselineReplayReceipt`, `replay_v51_predictions(spec, feature_seal)`, `load_baseline_replay(path, expected_sha256)`, `DailyPair`, `EvaluationReport`, `evaluate_predictions(candidate, baseline, outcomes)`, and `load_evaluation_report(path, expected_sha256)`.**

```python
@dataclass(frozen=True)
class EvaluationReport:
    model_id: str
    run_spec_sha256: str
    prediction_sha256: str
    baseline_prediction_sha256: str
    population_sha256: str
    all_games_count: int
    brier: float
    log_loss: float
    daily_top1_count: int
    daily_top1_correct: int
    daily_top1_delta_vs_baseline: int
    max_losing_streak: int
    paired_days_sha256: str
    artifact_sha256: str
    path: str
```

The exact signatures are `evaluate_predictions(candidate: PredictionSeal, baseline: PredictionSeal, outcomes: Mapping[int, int]) -> tuple[EvaluationReport, EvaluationReport]` and `load_evaluation_report(path: Path, expected_sha256: str) -> EvaluationReport`. The returned tuple is ordered candidate, baseline. Both reports bind the same `run_spec_sha256`, population digest, and paired-day digest; the baseline report has `daily_top1_delta_vs_baseline == 0`. Persist all fields through `paired_days_sha256`; treat `artifact_sha256` and `path` as runtime descriptors excluded from payload bytes. The loader rereads the regular canonical payload, verifies its expected/sidecar digest and closed schema, and constructs the descriptor. Replay through frozen v5.1 library scoring, normalization, clipping, pick logic, and `rank_daily`, serialize canonical prediction bytes twice, require equality, and bind feature/card/RunSpec digests. The evaluator requires identical game keys and labels, uses `metrics.brier`/`metrics.log_loss` for all games, pairs independently selected rank-one correctness by official date, computes each report’s correct-date count and streak, and returns the candidate daily Top-1 correctness difference versus baseline. This exact shape supports Lane A strict comparison and Lane B’s no-worse/positive-Top-1 gates without dictionary indexing.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_baseline_replay tests.test_model_improvement_evaluation -v`

Expected: PASS; comparisons are byte-replay and population bound.

- [ ] **Step 5: Commit.**

```bash
git add mlb_backtest/model_improvement/baseline_replay.py mlb_backtest/model_improvement/evaluation.py tests/test_model_improvement_baseline_replay.py tests/test_model_improvement_evaluation.py
git commit -m "feat: replay baseline and evaluate common populations"
```

### Task 7: Seal OOF predictions before joining outcomes

**Files:**
- Create: `schemas/model_improvement/prediction_seal.schema.json`
- Create: `mlb_backtest/model_improvement/oof_ledger.py`
- Create: `tests/test_model_improvement_oof_ledger.py`

- [ ] **Step 1: Write RED tests denying labels in a prediction seal, missing/extra/duplicate joined labels, tampered persisted rows, non-O19-to-O23 IDs, 2025 labels, and a loader that rejects a seal-bound row path whose JSONL bytes, row digest, game uniqueness/order, RunSpec/source/prediction-contract/card binding differ.**

```python
class OofLedgerTest(unittest.TestCase):
    def test_prediction_precedes_verified_outcome_join(self):
        prediction = seal_oof_predictions(spec, "O19", prediction_rows)
        joined = join_oof_outcomes(spec, load_prediction_seal(Path(prediction.path), prediction.artifact_sha256), outcomes)
        self.assertEqual(load_joined_ledger(Path(joined.path), joined.artifact_sha256), joined)

    def test_authoritative_row_loader_rereads_the_bound_jsonl(self):
        seal = seal_oof_predictions(spec, "O19", prediction_rows)
        self.assertEqual(load_prediction_rows(spec, seal), tuple(prediction_rows))

    def test_joined_row_loader_returns_only_verified_label_bindings(self):
        prediction = seal_oof_predictions(spec, "O19", prediction_rows)
        joined = join_oof_outcomes(spec, prediction, outcomes)
        self.assertEqual(load_joined_prediction_rows(spec, joined)[0][1], outcomes[prediction_rows[0].game_pk])
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger -v`

Expected: FAIL with `ModuleNotFoundError` for `oof_ledger`.

- [ ] **Step 3: Implement `JoinedLedger`, `seal_oof_predictions(spec, ledger_id, rows)`, `load_prediction_seal(path, expected_sha256)`, `load_prediction_rows(spec: ModelImprovementRunSpec, seal: PredictionSeal) -> tuple[PredictionRow, ...]`, `load_joined_prediction_rows(spec: ModelImprovementRunSpec, ledger: JoinedLedger) -> tuple[tuple[PredictionRow, int], ...]`, `join_oof_outcomes(spec, prediction, outcomes)`, and `load_joined_ledger(path, expected_sha256)`.** `load_prediction_rows` is the sole public prediction-row loader: derive the row JSONL location from the seal’s declared artifact location, never from an assumption that the descriptor file contains rows; read only regular canonical JSONL bytes; verify `rows_sha256`, population digest, unique ordered game keys, and all RunSpec/source-gate/prediction-contract/model-card bindings before constructing immutable rows. Prediction rows contain no labels. `load_joined_prediction_rows` is the sole public labeled-row accessor: derive its row JSONL from the `JoinedLedger` descriptor, verify regular canonical bytes, the ledger digest, `ledger_id`/target-year RunSpec binding, immutable prediction-seal/source/prediction-contract/card bindings, one ordered unique game per row, and exactly one binary label per game before returning immutable `(PredictionRow, label)` pairs. `join_oof_outcomes` uses only the prediction-row loader, validates one binary label per game, writes a separate immutable joined artifact under `oof`, and returns a digest/path descriptor computed from payload bytes rather than embedded in either prediction or joined payload. Export both row loaders publicly; Lane B must use them rather than descriptor fields or local row readers.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger -v`

Expected: PASS; calibration can consume only verified joined ledgers.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/prediction_seal.schema.json mlb_backtest/model_improvement/oof_ledger.py tests/test_model_improvement_oof_ledger.py
git commit -m "feat: seal model improvement OOF ledgers"
```

### Task 8: Implement frozen-card persistence and Lane A rolling-origin calibration

**Files:**
- Create: `schemas/model_improvement/frozen_model_card.schema.json`
- Modify: `mlb_backtest/model_improvement/contracts.py`
- Create: `mlb_backtest/model_improvement/lane_a.py`
- Create: `tests/test_model_improvement_lane_a.py`

- [ ] **Step 1: Write RED tests for Common frozen-card schema/payload round trips and H22 O19–O21/H23 O19–O22 Lane A calibration, unchanged scores/weights/shrinkage/clipping/pick/rank, H22/H23 prediction seals, and strict H23 lower Brier plus lower logloss.**

```python
class LaneATest(unittest.TestCase):
    def test_common_card_sealer_loader_round_trip(self):
        card = seal_frozen_model_card(spec, "lane_a", "H23/frozen_model_card.json", make_card_payload())
        self.assertEqual(load_frozen_model_card(spec, Path(card.path), card.artifact_sha256), card)

    def test_h23_freezes_only_after_strict_identical_population_improvement(self):
        result = fit_lane_a_fold(spec, "H23", feature_seal, joined_ledgers, baseline_receipt)
        self.assertEqual(result.card.state, "H23_FROZEN")
        self.assertLess(result.evaluation.brier, result.baseline_evaluation.brier)
        self.assertLess(result.evaluation.log_loss, result.baseline_evaluation.log_loss)
        self.assertEqual(result.prediction.prediction_contract_sha256, spec.prediction_contract_sha256)
        self.assertEqual(result.card.prediction_contract_sha256, result.prediction.artifact_sha256)
        self.assertEqual(result.card.prediction_seal_sha256, result.prediction.artifact_sha256)

    def test_h23_non_improvement_emits_rejected_disposition_without_card(self):
        result = fit_lane_a_fold(spec, "H23", feature_seal, worse_joined_ledgers, baseline_receipt)
        self.assertIsNone(result.card)
        self.assertEqual(result.disposition.state, "REJECTED")
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_a -v`

Expected: FAIL with `ModuleNotFoundError` for `lane_a`.

- [ ] **Step 3: Implement the Common closed `frozen_model_card.schema.json`, card persistence, and `LaneAFoldResult` with fields `fold_id`, `card: FrozenModelCard | None`, `disposition: LaneDisposition`, `prediction: PredictionSeal`, `evaluation: EvaluationReport`, and `baseline_evaluation: EvaluationReport`; implement `fit_lane_a_fold(spec, fold_id, feature_seal, joined_ledgers, baseline_replay)` and `load_lane_a_fold(spec, fold_id)`.** The exact card APIs are:

```python
def seal_frozen_model_card(
    spec: ModelImprovementRunSpec,
    namespace: Literal["lane_a", "lane_b", "lane_c"],
    relative_name: str,
    payload: Mapping[str, object],
) -> FrozenModelCard

def load_frozen_model_card(
    spec: ModelImprovementRunSpec, path: Path, expected_sha256: str,
) -> FrozenModelCard
```

The sealer validates the closed model/state/fold schema and namespace/model compatibility, writes a canonical payload with no runtime `artifact_sha256`/`path`, rereads and hashes its persisted bytes before constructing descriptor fields, and returns the loader result. The loader validates the regular canonical bytes, expected/sidecar digest, exact RunSpec namespace/model/fold path, and payload schema before constructing an immutable card. It never discovers cards by scanning or accepts fabricated descriptors; H24 additionally requires the caller to supply its exact registry-pinned path/digest.

For every OOF target use only that fold’s declared prior-season pool, score frozen v5.1 structure, fit `normalize.fit_logistic` only on earlier joined OOF predictions, and seal H22/H23 per-game probabilities/ranks. The `PredictionSeal.prediction_contract_sha256` must equal `spec.prediction_contract_sha256`, never its artifact digest. Replay the v5.1 baseline on exactly those IDs, call `candidate_evaluation, baseline_evaluation = evaluate_predictions(candidate_prediction, baseline_prediction, outcomes)`, and retain those two reports in `LaneAFoldResult`. H22 emits `QUALIFIED` with reason `H22_EVALUATED` and no card. H23 emits the sole `H23_FROZEN` card only when both strict metric comparisons pass; H23 failure emits `REJECTED`, reason code(s) `H23_BRIER_NOT_STRICTLY_LOWER` and/or `H23_LOG_LOSS_NOT_STRICTLY_LOWER`, and no card. A frozen H23 payload binds source gate, feature seal, all OOF ledgers, baseline replay, alpha/beta, evaluation, and the prediction artifact: both card `prediction_contract_sha256` and `prediction_seal_sha256` equal `PredictionSeal.artifact_sha256`. It reads no H24 outcome/metric/parameter.

- [ ] **Step 4: Run GREEN plus existing scoring regressions.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_a tests.test_scoring tests.test_normalize tests.test_ranking tests.test_metrics -v`

Expected: PASS; only OOF calibration is added to frozen scoring behavior.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/frozen_model_card.schema.json mlb_backtest/model_improvement/contracts.py mlb_backtest/model_improvement/lane_a.py tests/test_model_improvement_lane_a.py tests/test_model_improvement_contracts.py
git commit -m "feat: freeze rolling-origin lane a"
```

### Task 9: Publish the package API and verify the Common handoff

**Files:**
- Modify: `mlb_backtest/model_improvement/__init__.py`
- Create: `tests/test_model_improvement_public_api.py`

- [ ] **Step 1: Write an import-only RED test for every symbol in the authoritative public API section.**

```python
class PublicApiTest(unittest.TestCase):
    def test_common_symbols_are_exported(self):
        self.assertTrue(callable(load_verified_run_spec))
        self.assertTrue(callable(require_source_gate))
        self.assertTrue(callable(load_sealed_feature_rows))
        self.assertTrue(callable(seal_oof_predictions))
        self.assertTrue(callable(fit_lane_a_fold))
        self.assertTrue(callable(load_baseline_replay))
        self.assertTrue(callable(load_evaluation_report))
        self.assertTrue(callable(load_frozen_model_card))
        self.assertIsNotNone(SealedFeatureRow)
        self.assertIsNotNone(FrozenModelCard)
        self.assertIsNotNone(BaselineReplayReceipt)
        self.assertIsNotNone(LaneDisposition)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_public_api -v`

Expected: FAIL until all explicit exports are present.

- [ ] **Step 3: Export only the listed public types/functions from `__init__.py`, including `BaselineReplayReceipt`, `load_baseline_replay`, `EvaluationReport`, `evaluate_predictions`, `load_evaluation_report`, `MetricPrimitive`, `LaneDisposition`, `seal_lane_disposition`, `load_lane_disposition`, `seal_frozen_model_card`, `load_frozen_model_card`, and the authoritative `load_prediction_rows`/`load_joined_prediction_rows`; do not re-export legacy `RunSpec`, source acquisition, or outcome loaders.**

- [ ] **Step 4: Run all Common/Lane A suites.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_canonical tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_source_gate tests.test_model_improvement_sealed_features tests.test_model_improvement_baseline_replay tests.test_model_improvement_evaluation tests.test_model_improvement_oof_ledger tests.test_model_improvement_lane_a tests.test_model_improvement_public_api -v`

Expected: PASS; fixture tests pass while real execution remains blocked without verified 2018–2021 sources.

- [ ] **Step 5: Commit.**

```bash
git add mlb_backtest/model_improvement/__init__.py tests/test_model_improvement_public_api.py
git commit -m "feat: publish model improvement common API"
```

## Final verification and sibling contract

- [ ] **Step 1: Run syntax, lint, and regression verification.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q mlb_backtest/model_improvement && ruff check mlb_backtest/model_improvement tests/test_model_improvement_*.py && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_canonical tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_source_gate tests.test_model_improvement_sealed_features tests.test_model_improvement_baseline_replay tests.test_model_improvement_evaluation tests.test_model_improvement_oof_ledger tests.test_model_improvement_lane_a tests.test_model_improvement_public_api tests.test_scoring tests.test_normalize tests.test_ranking tests.test_metrics tests.test_leakage -v`

Expected: PASS with no lint findings; a real source-gate call still errors until verified 2018–2021 artifacts exist.

- [ ] **Step 2: Inspect the implementation patch.**

Run: `git diff --check && git status --short`

Expected: no whitespace error; only files owned by this plan are present.

Lane B reads `spec.lane_b`, shared feature seals including rematerializable metric primitives, the H23-frozen A card, and common evaluation; it publishes either `QUALIFIED` A+B or `REJECTED` evidence through `LaneDisposition` and never owns a registry. Lane C reads exactly `environment=(park, weather_roof)` and `defense=(oaa, framing, blocking_throwing)` from `spec.lane_c`, consumes `SealedFeatureRow` and the A+B card, and emits either `QUALIFIED` A+B+C, `ABSENT` evidence, or `REJECTED` evidence through `LaneDisposition`; it seals H22/H23 coverage features and registry-pinned prediction-only H24 features without H24 outcomes, and never changes package exports. H24 owns the one candidate registry and consumes Common’s RunSpec, source-gate receipt, `BaselineReplayReceipt`, feature/prediction seals, evaluations, dispositions, and frozen cards.

No real H22 is authorized until `verify_source_gate()` validates the actual 2018–2021 source authority and deterministic replay.
