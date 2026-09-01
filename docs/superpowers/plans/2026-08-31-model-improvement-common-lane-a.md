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
    FrozenModelCard, MetricPrimitive, PredictionRow, PredictionSeal, ScoringModelCard,
    QualityRecipe, SealedFeatureRow, compute_feature_row_sha256, load_frozen_model_card,
    seal_frozen_model_card, sealed_feature_row_payload,
)
from mlb_backtest.model_improvement.scoring_cards import (
    load_scoring_model_card, resolve_prediction_weight_authority,
    seal_scoring_model_card,
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
from mlb_backtest.model_improvement.canonical import (
    canonical_json_bytes, write_immutable_bytes, write_immutable_json,
)
from mlb_backtest.model_improvement.oof_ledger import (
    JoinedLedger, join_oof_outcomes, load_joined_ledger, load_prediction_seal,
    load_joined_prediction_rows, load_prediction_rows, seal_oof_predictions,
)
from mlb_backtest.model_improvement.lane_a import LaneAFoldResult, fit_lane_a_fold, load_lane_a_fold
from mlb_backtest.model_improvement.evaluation import EvaluationReport, evaluate_predictions, load_evaluation_report
from mlb_backtest.model_improvement.lane_disposition import (
    LANE_DISPOSITION_STATES, LANE_IDS, LaneDisposition, load_lane_disposition,
    seal_lane_disposition,
)
```

`canonical_json_bytes()` returns sorted UTF-8 compact JSON with exactly one trailing newline. Immutable writers exclusively create, fsync, reread, hash, and reject symlinks/non-regular files. Callers must not append a newline.

**Artifact hash convention:** `artifact_sha256` and `path` are runtime descriptor fields, never fields inside the canonical persisted payload they describe. A writer serializes `payload = artifact.as_mapping()` without those descriptor fields, persists canonical payload bytes, computes `sha256(payload_bytes)`, optionally writes that digest/path in a separate sidecar, then constructs the returned descriptor. Every `as_mapping()` for `FrozenModelCard`, `LaneDisposition`, `PredictionSeal`, `FeatureSeal`, `SourceGateReceipt`, `JoinedLedger`, `BaselineReplayReceipt`, and `EvaluationReport` excludes runtime `artifact_sha256` and `path`. Every loader reads canonical payload bytes from a regular file, recomputes the digest, compares it with the caller’s expected digest and any sidecar digest, validates the closed payload schema and RunSpec path, then constructs the descriptor. No payload contains or hashes its own digest.

**Prediction binding convention:** `PredictionRow.prediction_contract_sha256` and `PredictionSeal.prediction_contract_sha256` are always the immutable RunSpec-declared digest of the scoring, ranking, pick, clipping, and tie-break contract; neither is a prediction-artifact digest. Every `PredictionRow` and `PredictionSeal` binds the exact `scoring_model_card_sha256` used to score it. For candidates that digest names a prior, immutable `ScoringModelCard`; for `v5.1` it is the existing canonical baseline-card digest pinned directly in RunSpec, not a new wrapper artifact. H22/H23 seals have no frozen-card digest. H24 seals may additionally carry the registry-pinned `frozen_model_card_sha256`, and only after it proves the same scoring-card digest. A post-evaluation `FrozenModelCard` binds `scoring_model_card_path`/`scoring_model_card_sha256`, its H23 prediction seal, and its evaluation report. This direction—scoring card → prediction → evaluation → frozen card—removes the prior digest cycle.

## Files and namespaces

| File | Responsibility |
| --- | --- |
| `protocols/model_improvement/parallel_model_result_v1.json` | Complete immutable experiment declaration, created only after verified 2018–2021 acquisition in a single declaration-only activation commit immediately after the frozen code-revision commit. |
| `schemas/model_improvement/{parallel_model_result_run_spec,source_gate_receipt,feature_seal,prediction_seal,scoring_model_card,frozen_model_card,lane_disposition}.schema.json` | Closed schemas for every shared artifact. |
| `mlb_backtest/model_improvement/canonical.py` | Canonical serialization and immutable filesystem operations. |
| `mlb_backtest/model_improvement/contracts.py` | Shared frozen types (including `ScoringModelCard`) and pure mapping validation; it imports no scoring-card persistence API. |
| `mlb_backtest/model_improvement/lane_disposition.py` | Immutable QUALIFIED, ABSENT, or REJECTED lane evidence. |
| `mlb_backtest/model_improvement/run_spec.py` | Closed RunSpec parser, digest, and namespace paths. |
| `mlb_backtest/model_improvement/source_gate.py` | Read-only V5.2 authority authentication. |
| `mlb_backtest/model_improvement/sealed_features.py` | Full component-side result-free feature seals. |
| `mlb_backtest/model_improvement/baseline_replay.py` | Exact v5.1 bytes and receipt. |
| `mlb_backtest/model_improvement/scoring_cards.py` | Pre-prediction immutable scoring-card sealing, verified loading, and `resolve_prediction_weight_authority`; it imports the `ScoringModelCard` type from `contracts` but `contracts` never imports it back. |
| `mlb_backtest/model_improvement/oof_ledger.py` | Candidate-qualified prediction-before-outcome sealing and joined ledgers. |
| `mlb_backtest/model_improvement/evaluation.py` | Identical-population metrics and date pairing. |
| `mlb_backtest/model_improvement/lane_a.py` | Rolling-origin calibration and H23 freeze. |

RunSpec `outputs.namespaces` contains exactly `source_gate`, `features`, `baseline`, `oof`, `lane_a`, `lane_b`, `lane_c`, `registry`, `h24_predictions`, and `h24_reports`. Every output is made with `spec.output_path(namespace, relative_name)` and cannot escape `outputs.root`. Within `features`, only `features/common/H22`, `features/common/H23`, `features/common/H24`, `features/lane_c/H22`, `features/lane_c/H23`, and prediction-only `features/lane_c/H24` are admitted; a feature-seal path is exactly `features/{feature_set_id}/{fold_id}/feature_seal.json` with its row file in the same directory.

```text
canonical + contracts -> RunSpec -> source gate -> feature seals + v5.1 replay
  -> scoring cards -> OOF prediction/join seals + evaluation -> Lane A H22/H23 freeze
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
from mlb_backtest.model_improvement.contracts import (
    PredictionSeal, QualityRecipe, MetricPrimitive, ScoringModelCard, SealedFeatureRow,
    compute_feature_row_sha256, sealed_feature_row_payload,
)
from mlb_backtest.model_improvement.lane_disposition import LaneDisposition

_D = "a" * 64
_SIDES = ("away", "home")
_COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")

def make_fixed_quality_recipe(*, fixed_value=0.0):
    return QualityRecipe("fixed", "fixed", None, None, None, fixed_value, (), (), _D)

def make_ratio_quality_recipe(*, numerator=1.0, baseline_constant=1.0):
    return QualityRecipe("ratio", "ratio", "starter", numerator, baseline_constant, None, (), (), _D)

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
    return PredictionSeal("lane_a/A/O19", "O19", "A", None, _D, _D, _D, _D, _D, None, _D, _D, "test/prediction_seal.json")

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
        with self.assertRaisesRegex(ValueError, "fixed value"):
            make_fixed_quality_recipe(fixed_value=1.01)
        with self.assertRaisesRegex(ValueError, "ratio numerator"):
            make_ratio_quality_recipe(numerator=-0.01)
        with self.assertRaisesRegex(ValueError, "baseline constant"):
            make_ratio_quality_recipe(baseline_constant=0.0)
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
    scheduled_start_utc: str; rank: int; feature_row_sha256: str
    prediction_contract_sha256: str; scoring_model_card_sha256: str

@dataclass(frozen=True)
class PredictionSeal:
    ledger_id: str; fold_id: str; model_id: str; candidate_id: str | None
    run_spec_sha256: str; source_gate_sha256: str; prediction_contract_sha256: str
    scoring_model_card_sha256: str; population_sha256: str
    frozen_model_card_sha256: str | None; rows_sha256: str; artifact_sha256: str; path: str

@dataclass(frozen=True)
class ScoringModelCard:
    model_id: str; lane_id: str; fold_id: str; candidate_id: str | None
    run_spec_sha256: str; source_gate_sha256: str; baseline_replay_sha256: str
    parent_card_sha256: str | None; feature_seal_sha256: str
    prediction_contract_sha256: str; calibration_card_sha256: str | None
    candidate_payload_sha256: str | None; parameters: Mapping[str, object]
    artifact_sha256: str; path: str

@dataclass(frozen=True)
class FrozenModelCard:
    model_id: str; state: str; fold_id: str; run_spec_sha256: str; source_gate_sha256: str
    baseline_replay_sha256: str; parent_card_sha256: str | None; feature_seal_sha256: str
    prediction_contract_sha256: str; scoring_model_card_path: str; scoring_model_card_sha256: str
    prediction_seal_sha256: str; evaluation_sha256: str
    parameters: Mapping[str, object]; artifact_sha256: str; path: str

```

`contracts.py` owns `MODEL_IDS={"v5.1","A","A+B","A+B+C"}`, `FROZEN_CARD_MODEL_IDS={"A","A+B","A+B+C"}`, `FROZEN_CARD_STATES={"H23_FROZEN"}`, `FOLD_IDS={"O19","O20","O21","O22","O23","H22","H23","H24"}`, `PICKED_SIDES={"away","home","none"}`, and `QUALITY_FAMILIES={None,"starter","hitter_split","recent_form","bullpen"}`. A `PredictionSeal.ledger_id` is not a bare fold token: it is the canonical scoped identity `"{scope}/{model_or_candidate_id}/{fold_id}"`, where scope is one of `baseline`, `lane_a`, `lane_b`, `lane_c`, or `h24`; e.g. `lane_a/A/O19`, `lane_b/{candidate_id}/O19`, and `h24/A+B/H24`. This makes the 210 Lane-B candidate artifacts non-colliding. `lane_disposition.py` owns `LaneDisposition`, `LANE_IDS={"A","B","C"}`, `LANE_DISPOSITION_STATES={"QUALIFIED","ABSENT","REJECTED"}`, and its seal/load APIs; it imports only pure helpers from `contracts.py` and must not create a circular alias. Enforce scoring-card and frozen-card model/state/fold compatibility, `PredictionRow.model_id`, its required 64-lowercase-hex `prediction_contract_sha256`, the scoped seal identity, picked side, and rank (`0` only for `none`; strictly positive otherwise), and recipe family. Lane-specific parameter keys remain open here but their JSON values are recursively immutable.

Implement `_freeze_json(value)` and `_thaw_json(value)`: mapping inputs become recursive `MappingProxyType`, sequences become tuples, and scalars must be JSON scalars. Apply them to card parameters, feature-row maps, recipe children/weights, and disposition evidence/reasons; `as_mapping()` always returns fresh mutable JSON-compatible values. Validate exact `SIDES = ("away", "home")` and `COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")`, ISO dates/UTC strings, score and quality [0,100], probability [0,1], 64 lowercase hex digests, and the environment/defense recipe fallback of 50/0. `MetricPrimitive.normalized_score` is the pre-quality `normalize.metric_score(value, mean, sd, direction)` result in [0,100], never an already quality-shrunk score. Preserve ordered primitives and require their nonnegative aggregation weights to sum to exactly one under the documented `math.fsum` tolerance. A `fixed` recipe has a finite `fixed_value` in `[0,1]`; a `ratio` recipe has a finite `numerator >= 0` and finite `baseline_constant > 0`. Keep those exact fixed/ratio requirements plus the weighted-sum/minimum constraints in this task.

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

`run_spec.py` owns the non-persisted `VerifiedRunSpecActivation` descriptor and the verified-loader attachment; the RunSpec JSON schema owns no such field. `test_model_improvement_run_spec.py` owns the C→D activation, descriptor-exclusion, CWD-independence, and mutation-rejection cases. Task 4 consumes that verified runtime contract but must not mutate or serialize it.

- [ ] **Step 1: Write table-driven RED tests for every immutable binding: activation/code revision; source authority paths/digests; D-1/T-45; exclusions; H22/H23/H24 chronology; v5.1 baseline; metrics; output namespaces; Lane B grid/keep/selection; Lane C slots/qualification; H24/2025 firewall; and a dedicated disposition persistence round trip.**

```python
import json
import tempfile
from hashlib import sha256
from pathlib import Path

from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.lane_disposition import load_lane_disposition, seal_lane_disposition
from mlb_backtest.model_improvement.run_spec import (
    build_run_spec, load_run_spec, load_verified_run_spec,
)

class RunSpecTest(unittest.TestCase):
    def test_production_activation_is_a_clean_single_declaration_commit_after_code_revision(self):
        # `make_temp_repo` commits implementation as C, then writes the production
        # declaration with code_revision=C and commits only that path as D=HEAD.
        with make_temp_repo() as repo:
            code_revision = repo.commit_code_implementation()
            declaration = repo.write_production_declaration(code_revision=code_revision)
            activation = repo.commit_only(declaration)
            self.assertEqual(repo.head(), activation)
            self.assertEqual(repo.parent(activation), code_revision)
            self.assertEqual(repo.diff_names(code_revision, activation),
                             ("protocols/model_improvement/parallel_model_result_v1.json",))
            # The process CWD deliberately points at another repository.  Verification
            # must derive and query declaration.parent's owning repository instead.
            with make_temp_repo() as unrelated_repo, unrelated_repo.as_process_cwd():
                spec = load_verified_run_spec(declaration)
            self.assertEqual(spec.code_revision, code_revision)
            self.assertIsNotNone(spec.verified_activation)
            self.assertEqual(spec.verified_activation.declaration_path, str(declaration.resolve()))
            self.assertEqual(
                spec.verified_activation.declaration_sha256,
                sha256(repo.git_show_head("protocols/model_improvement/parallel_model_result_v1.json")).hexdigest(),
            )
            self.assertEqual(spec.verified_activation.run_spec_sha256, run_spec_sha256(spec))

    def test_verified_activation_is_runtime_only_and_cannot_change_the_run_spec_digest(self):
        ordinary = load_run_spec(SYNTHETIC_SPEC_PATH)
        self.assertIsNone(ordinary.verified_activation)
        with make_valid_activation_repo() as repo:
            verified = load_verified_run_spec(repo.declaration_path)
            self.assertNotIn("verified_activation", verified.as_mapping())
            self.assertNotIn("verified_activation", verified.persisted_payload())
            self.assertEqual(run_spec_sha256(verified), verified.verified_activation.run_spec_sha256)
            self.assertEqual(
                run_spec_sha256(build_run_spec(verified.as_mapping())),
                run_spec_sha256(verified),
            )

    def test_verified_production_activation_rejects_dirty_untracked_extra_merge_and_wrong_parent(self):
        # Each mutation starts from a fresh valid C -> D fixture above.  Dirty tracked
        # files and untracked files fail before parsing; a second changed path, a merge
        # activation, an activation whose parent is not code_revision, a worktree whose
        # declaration bytes differ from HEAD:path, and a synthetic declaration all fail.
        for mutation in (
            "dirty_tracked", "untracked", "extra_changed_path", "merge_activation",
            "wrong_parent", "worktree_bytes_differ_from_head_blob", "synthetic",
            "wrong_external_repo_path", "cross_root_declaration_path",
        ):
            with self.subTest(mutation=mutation), make_valid_activation_repo() as repo:
                path = repo.apply_activation_mutation(mutation)
                with self.assertRaises(ValueError):
                    load_verified_run_spec(path)

    def test_exact_contract_is_available_to_sibling_lanes(self):
        spec = load_run_spec(SPEC_PATH)
        self.assertEqual(spec.fold("H22").normalization_years, (2018, 2019, 2020, 2021))
        self.assertEqual(spec.cutoffs, {"history": "D-1", "snapshot": "T-45"})
        self.assertEqual(spec.lane_b.keep_h22_count, 12)
        self.assertEqual(spec.lane_b.h23_order, ("brier", "log_loss", "daily_top1_delta_desc", "candidate_id"))
        self.assertRegex(spec.prediction_contract_sha256, r"^[0-9a-f]{64}$")
        self.assertTrue(str(spec.output_path("features", "lane_c/H24/feature_seal.json")).endswith("features/lane_c/H24/feature_seal.json"))
        self.assertTrue(spec.h24_firewall.forbid_outcomes_before_registry)

    def test_prediction_contract_digest_and_closed_mutations(self):
        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            raw["prediction_contract_sha256"],
            sha256(canonical_json_bytes(raw["prediction_contract"])).hexdigest(),
        )
        raw["prediction_contract"]["clip"]["min"] = 0.11
        with self.assertRaisesRegex(ValueError, "prediction contract digest"):
            build_run_spec(raw)
        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        raw["prediction_contract"]["ranking"]["unknown"] = True
        with self.assertRaisesRegex(ValueError, "prediction contract|unknown"):
            build_run_spec(raw)
        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        raw["prediction_contract"]["component_weights_binding"]["sum"] = 99
        raw["prediction_contract_sha256"] = sha256(
            canonical_json_bytes(raw["prediction_contract"])).hexdigest()
        with self.assertRaisesRegex(ValueError, "component weights binding"):
            build_run_spec(raw)

    def test_lane_disposition_round_trips_in_a_temporary_synthetic_output_root(self):
        raw = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            raw["outputs"]["root"] = directory
            spec = build_run_spec(raw)
            disposition = seal_lane_disposition(
                spec, "A", "REJECTED", None, ("a" * 64,), ("H23_GATE",))
            self.assertEqual(
                load_lane_disposition(spec, Path(disposition.path), disposition.artifact_sha256),
                disposition,
            )
```

`make_temp_repo()`/`make_valid_activation_repo()` are test helpers, not production APIs. Each initializes an isolated repository, commits the completed RunSpec implementation as code commit `C`, writes the real production-path declaration with `code_revision=C`, then commits exactly that path as non-merge activation `D`. The mutation helper independently creates: a modified tracked file, an untracked file, an extra changed path in `C..D`, a two-parent merge `D`, a declaration whose `code_revision` is not `D^`, post-commit declaration-byte drift, and a `SYNTHETIC_TEST_ONLY` declaration. It also creates an external non-owning path and a cross-root production-relative path whose own repository has no matching tracked blob; both must fail. The clean activation test switches process CWD to a separate repository and must still pass. `build_run_spec` and `load_run_spec` must return `verified_activation=None`; only `load_verified_run_spec` attaches the runtime descriptor. No test may use the repository under test or infer `code_revision` from a mutable working-tree `HEAD`.

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_run_spec -v`

Expected: FAIL with `ModuleNotFoundError` for `run_spec`.

- [ ] **Step 3: Add a recursively closed schema, `build_run_spec(raw: Mapping[str, object])`, and a synthetic fixture declaration.** Require top-level fields `schema_version`, `run_id`, `code_revision`, `source_authority`, `cutoffs`, `exclusions`, `folds`, `baseline`, `metrics`, `prediction_contract`, `prediction_contract_sha256`, `lane_b`, `lane_c`, `outputs`, and `h24_firewall`. `code_revision` is the immutable full 40-lowercase-hex **code commit**; it is never the declaration activation commit and no `spec_commit`/self-commit field exists. Persist both the closed `prediction_contract` object and its digest; require `prediction_contract_sha256 == sha256(canonical_json_bytes(prediction_contract))`. `ModelImprovementRunSpec`, its raw payload, and `as_mapping()` each retain both fields. Add schema, fixture, and mutation tests for a missing object/digest, any unknown nested key, and each digest/content mismatch.

`prediction_contract` has no extension keys at any level and contains exactly:

```python
{
    "component_order": ["starter", "offense", "bullpen", "environment", "defense", "schedule"],
    "edge_formula": "sum(component_weights_pp[c]/100 * (home_score[c]-away_score[c]) for c in component_order)",
    "completeness_formula": "sum(component_weights_pp[c]/100 * min(home_quality[c],away_quality[c]) for c in component_order)",
    "component_weights_binding": {
        "keys": ["starter", "offense", "bullpen", "environment", "defense", "schedule"],
        "value_type": "integer_nonnegative_pp", "sum": 100,
        "source": "closed_union:RunSpec.baseline.card.component_weights_pp|ScoringModelCard.parameters.component_weights_pp",
    },
    "clip": {"min": 0.10, "max": 0.90},
    "pick": {"home_if": "p_home > 0.5", "away_if": "p_home < 0.5", "none_if": "p_home == 0.5"},
    "ranking": {"tie_order": ["pick_probability:desc", "confidence:desc",
                                "starter_edge_abs:desc", "scheduled_start_utc:asc", "game_pk:asc"]},
}
```

The schema closes `prediction_contract`, `component_weights_binding`, `clip`, `pick`, and `ranking` with `additionalProperties: false`; require the six-component order, the exact edge/completeness formulas, exactly the six weight keys, nonnegative integer pp values summing to 100, and the closed-union source binding shown above. `baseline` is itself recursively closed and contains exactly one recursively closed `card` object with `path`, `sha256`, and `component_weights_pp`; that object directly pins the existing canonical v5.1 card, its digest, and exactly `{"starter":35,"offense":25,"bullpen":20,"environment":10,"defense":5,"schedule":5}`. All baseline readers use only `spec.baseline.card.path`, `spec.baseline.card.sha256`, and `spec.baseline.card.component_weights_pp`; no wrapper is persisted or synthesized. The universal contract never freezes a particular candidate’s weights: `prediction_contract_sha256` hashes only the scoring algorithm and the rule that weights come from exactly one of two sources. For A/A+B/A+B+C, a `ScoringModelCard.parameters.component_weights_pp` attests the actual scoring weights before any prediction. Every `PredictionRow` and `PredictionSeal` must bind the universal digest and the exact `scoring_model_card_sha256`; schema parsing, row producers, and row loaders populate and require `PredictionRow.prediction_contract_sha256 == spec.prediction_contract_sha256 == PredictionSeal.prediction_contract_sha256`. An H24 seal may additionally bind the final frozen-card digest after the registry proves both identities. Require clip `[0.10, 0.90]`, the exact pick rules, and the exact ranking tie order. `source_authority` binds authority root, V5.2 SourceRunSpec, acquisition contract, terminal checkpoint, inventory, and every validation receipt by relative path plus digest. The declaration binds the v5.1 `baseline.card` object and every output namespace. The synthetic fixture contains only test fixture paths/digests and is marked `SYNTHETIC_TEST_ONLY`; `load_verified_run_spec` rejects that marker before Task 4 source-gate work.

The baseline declaration and v5.1 card pin `component_weights_pp={"starter":35,"offense":25,"bullpen":20,"environment":10,"defense":5,"schedule":5}`. For `lane_b`, declare the four baseline weights 35/25/20/5, fixed environment/defense 10/5, integer five-point grid bounded by ±10, nonnegative 85 total searched weight, multipliers `(0.75, 1.0, 1.25)`, exact 210 candidates, H22 `keep_h22_count` 12 ordered `(brier, log_loss, daily_top1_delta_desc, candidate_id)`, and H23 eligibility `(Brier/logloss no worse than A; positive paired Top-1 versus v5.1)` with the same final order. Baseline replay and every Lane B multiplier=`1.0` replay must prove these exact declared/card-bound weights, not merely the universal contract digest. For `lane_c`, declare exactly `environment=(park, weather_roof)` and `defense=(oaa, framing, blocking_throwing)`, H22/H23 coverage with required 100% admitted-or-neutral disposition/replay, and prediction-only H24 feature coverage with no outcome access; every Lane C feature set has parent `A+B`. Metrics are all-game Brier/logloss and date-paired Top-1. H24 forbids outcome access before registry and all 2025 outcomes.

- [ ] **Step 4: Implement `ModelImprovementRunSpec`, `build_run_spec(raw: Mapping[str, object])`, `parse_run_spec(raw: object)`, `load_run_spec(path: Path)`, `load_verified_run_spec(path: Path)`, `run_spec_sha256(spec)`, `spec.fold(fold_id)`, and `spec.output_path(namespace, relative_name)`.** The dataclass/raw payload/`as_mapping()` all include both `prediction_contract` and `prediction_contract_sha256`; the parser closes and hashes the object before accepting its digest. `fold` accepts H22/H23/H24 only; `output_path` accepts exactly the ten declared namespaces and a normal relative name without `..`, always resolves beneath `outputs.root`, and rejects all other namespaces/paths. For `features`, it additionally accepts only `common/H22`, `common/H23`, `common/H24`, `lane_c/H22`, `lane_c/H23`, or prediction-only `lane_c/H24` followed by the expected `feature_seal.json` or `rows.jsonl` filename. Validation recomputes every fold’s rolling OOF chronology and exactly 210 Lane B candidates. The builder freezes all candidate/metric rules before any H22 outcome can be joined.

`VerifiedRunSpecActivation` is a frozen **runtime-only** descriptor with exactly `declaration_path` (the absolute resolved production-declaration path), `declaration_sha256` (SHA-256 of the exact tracked declaration bytes), and `run_spec_sha256` (the semantic canonical RunSpec digest). `ModelImprovementRunSpec.verified_activation: VerifiedRunSpecActivation | None` is optional and defaults to `None`. It is deliberately excluded from the persisted JSON Schema, raw payload, `persisted_payload()`, `as_mapping()`, and `run_spec_sha256`; it is neither a declaration field nor a second persisted artifact, so it cannot introduce a declaration/hash cycle. `build_run_spec` and ordinary `load_run_spec` always return `verified_activation=None`. Only the successful verified-loading path below attaches this descriptor after every activation check has passed.

`load_verified_run_spec(path)` is an activation verifier, not merely a stricter parser. It must first derive `owning_root` with exactly `git -C path.parent rev-parse --show-toplevel`, fail if that command cannot identify a repository, and require `path.resolve() == (Path(owning_root) / "protocols/model_improvement/parallel_model_result_v1.json").resolve()`. It rejects `schema_version == "SYNTHETIC_TEST_ONLY"` and rejects a dirty or untracked **owning-root** worktree. Process CWD is never trusted: every repository query is exactly against `git -C owning_root`—including status, `rev-parse HEAD`, `rev-parse HEAD^`, `show`, and `diff`.

It must securely read the declaration bytes and require byte-for-byte equality with `git -C owning_root show HEAD:protocols/model_improvement/parallel_model_result_v1.json`; the path must therefore be a tracked blob at that owning root's `HEAD`. It requires that `git -C owning_root rev-list --parents -n 1 HEAD` show exactly one parent (no merge), reads that full parent SHA as `git -C owning_root rev-parse HEAD^`, requires `spec.code_revision == HEAD^`, and requires `git -C owning_root diff --name-only spec.code_revision..HEAD` to contain exactly the production declaration path and no other path. Only then attach `VerifiedRunSpecActivation(path.resolve(), sha256(tracked_bytes), run_spec_sha256(parsed_spec))` and return that new verified object. Thus a real declaration is added only in the single declaration-only activation commit immediately after the frozen code commit named by `code_revision`; it never asks the declaration to contain its own commit SHA. It does not call `verify_source_gate` or inspect source evidence; Task 4 owns source verification.

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
git add schemas/model_improvement/parallel_model_result_run_spec.schema.json mlb_backtest/model_improvement/run_spec.py mlb_backtest/model_improvement/lane_disposition.py tests/test_model_improvement_run_spec.py tests/fixtures/model_improvement/parallel_model_result_v1.synthetic.json
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
    def test_absent_real_production_declaration_fails_verified_loading(self):
        with self.assertRaisesRegex(ValueError, "declaration|activation|path"):
            load_verified_run_spec(PRODUCTION_SPEC_PATH)

    def test_valid_activation_reaches_the_2018_source_block_without_writing_a_receipt(self):
        # C -> D is a real clean declaration activation.  Its declared authority is
        # intentionally missing (and a second fixture has a wrong digest), so source
        # validation, not activation provenance, rejects before any receipt is written.
        with make_valid_activation_repo(authority="missing_or_digest_invalid") as repo:
            spec = load_verified_run_spec(repo.declaration_path)
            with self.assertRaisesRegex(ValueError, "2018|SOURCES_READY|source"):
                verify_source_gate(spec)
            self.assertFalse(repo.source_gate_receipt_path.exists())

    def test_source_gate_rejects_ordinary_synthetic_fake_and_dirty_activation_descriptors(self):
        ordinary = load_run_spec(SYNTHETIC_SPEC_PATH)
        synthetic = build_run_spec(SYNTHETIC_RAW)
        fake = dataclasses.replace(
            ordinary,
            verified_activation=VerifiedRunSpecActivation("/tmp/fake", "0" * 64, run_spec_sha256(ordinary)),
        )
        for presented in (ordinary, synthetic, fake):
            with self.subTest(presented=presented):
                with self.assertRaises(ValueError):
                    verify_source_gate(presented)
        with make_valid_activation_repo() as repo:
            spec = load_verified_run_spec(repo.declaration_path)
            repo.make_worktree_dirty()
            with self.assertRaises(ValueError):
                require_source_gate(spec)

    def test_activation_descriptor_is_not_serialized_or_semantically_hashed(self):
        with make_valid_activation_repo() as repo:
            verified = load_verified_run_spec(repo.declaration_path)
            self.assertNotIn("verified_activation", verified.as_mapping())
            self.assertEqual(run_spec_sha256(verified), verified.verified_activation.run_spec_sha256)

    def test_receipt_loader_checks_expected_digest(self):
        with self.assertRaisesRegex(ValueError, "source gate digest differs"):
            load_source_gate_receipt(FIXTURE_RECEIPT, "0" * 64)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_source_gate -v`

Expected: FAIL with `ModuleNotFoundError` for `source_gate`.

- [ ] **Step 3: Implement persisted `SourceGateReceipt` payload fields `run_spec_sha256`, `source_run_spec_sha256`, `acquisition_contract_sha256`, `checkpoint_sha256`, `inventory_sha256`, `validation_receipts_sha256`, `snapshot_replay_sha256`, `required_years`, and `status`; attach runtime descriptor fields `artifact_sha256` and `path` only after hashing canonical payload bytes. Implement `_revalidate_activated_spec(presented)`, `verify_source_gate(spec)`, `load_source_gate_receipt(path, expected_sha256)`, and `require_source_gate(spec)`.**

`_revalidate_activated_spec(presented)` is mandatory as the first operation of both `verify_source_gate` and `require_source_gate`. A supplied runtime descriptor is never trusted: require a non-null `presented.verified_activation`, use its absolute `declaration_path` only to call a **fresh** `load_verified_run_spec`, then require exact equality of (a) fresh and presented activation descriptors (`declaration_path`, tracked-byte `declaration_sha256`, semantic `run_spec_sha256`), (b) `run_spec_sha256(fresh)` and the descriptor's semantic digest, and (c) fresh versus presented canonical `as_mapping()` bytes/digest. Reject ordinary, synthetic, missing, fake, or post-activation-dirty presentations. Thereafter source-gate code uses only the fresh verified object, never `presented` or its descriptor. This revalidation preserves the no-hash-cycle rule because the descriptor remains runtime-only and excluded from the persisted RunSpec payload/hash.

`source_gate_receipt.schema.json`, `SourceGateReceipt.from_payload_mapping`, and the standalone `load_source_gate_receipt(path, expected_sha256)` are intentionally **structural-only**: they canonical-parse/re-hash the payload and sidecar and construct a descriptor, but cannot prove the receipt inventory is bound to a RunSpec they do not receive. `verify_source_gate` must call `_revalidate_activated_spec` first, then call `protocols.load_source_run_spec`, `execution_registry.validate_sources_ready_evidence_set`, `execution_registry.load_terminal_acquisition_checkpoint`, `v52_verify_sources.load_snapshot_replay_inputs`, and `v52_verify_sources.replay_verified_snapshots` with only paths declared in that fresh RunSpec. Check every bound file digest before and after those calls, require the replayed six source families across 2018–2024, construct `inventory_sha256` from exactly `fresh.source_authority.inventory.sha256`, then write the receipt only in `source_gate`. It accepts neither arbitrary bytes nor caller-supplied inventory/authority/output path. Add `_load_bound_receipt(spec, path, expected_sha256)`: after secure structural loading, it requires both `receipt.run_spec_sha256 == run_spec_sha256(spec)` and `receipt.inventory_sha256 == spec.source_authority.inventory.sha256`. `require_source_gate` likewise revalidates first, obtains the fresh verified receipt, calls `_load_bound_receipt` for the persisted receipt, and requires exact persisted payload, path, and digest equality of the two receipts. RED/green boundary cases must prove that a structurally valid receipt with a foreign inventory digest can load standalone but is rejected by `_load_bound_receipt` and `require_source_gate`, while `verify_source_gate` never serializes that foreign digest.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_source_gate -v`

Expected: PASS; fixtures validate shape, but absent real 2018–2021 input remains blocked.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/source_gate_receipt.schema.json mlb_backtest/model_improvement/source_gate.py tests/test_model_improvement_source_gate.py
git commit -m "feat: authenticate model improvement source gate"
```

- [ ] **Step 6: Stop real-fold activation until acquisition supplies verified 2018–2021 evidence and exact authority paths/digests.** Do not create `protocols/model_improvement/parallel_model_result_v1.json` with invented or placeholder values.

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_source_gate.SourceGateTest.test_absent_real_production_declaration_fails_verified_loading -v`

Expected: PASS because verified loading rejects the absent real production declaration; the separate clean C→D fixture test reaches and observes the missing/invalid 2018 source-gate block without writing a receipt.

- [ ] **Step 7: After the external acquisition prerequisite is verified, first commit all completed implementation as the frozen `code_revision` commit. Invoke `build_run_spec()` with that exact parent SHA and authenticated authority paths/digests, then add only `protocols/model_improvement/parallel_model_result_v1.json` in the immediately following single-parent declaration-only activation commit.** From the clean activation checkout, call `load_verified_run_spec()` and then `require_source_gate()`; retain and compare its returned receipt at the real orchestration boundary before any H22 feature task starts. Do not amend the activation commit or add any second path: doing either fails the activation verifier. The activation descriptor is not serialized into the declaration or copied to another artifact.

### Task 5: Seal full result-free feature primitives

**Files:**
- Create: `schemas/model_improvement/feature_seal.schema.json`
- Create: `mlb_backtest/model_improvement/sealed_features.py`
- Create: `tests/test_model_improvement_sealed_features.py`
- Modify: `mlb_backtest/model_improvement/canonical.py`
- Modify: `tests/test_model_improvement_canonical.py`

`rows.jsonl` is an immutable byte artifact, not a JSON array.  Before this
task writes it, extend the Task 1 primitive with
`write_immutable_bytes(path: Path, contents: bytes) -> bool`, using the same
no-follow, staging, fsync, no-replace publication, secure reread, and
concurrent-identical-writer behavior as `write_immutable_json`; make the JSON
writer a canonical-JSON wrapper around that byte primitive.  Add focused
canonical tests for binary/JSONL byte equality, a conflicting existing byte
file, and a symlink/non-regular byte artifact.  Do not duplicate the secure
writer in `sealed_features.py`.

The Task 5 sidecars are mandatory and are not implicit behavior of
`write_immutable_bytes`: `rows.jsonl.sha256` and
`feature_seal.json.sha256` are each canonical JSON with exactly
`{"artifact_sha256": digest}`.  `rows.jsonl.sha256` binds the exact JSONL raw
bytes and `feature_seal.json.sha256` binds the canonical FeatureSeal payload
bytes.  The sealer publishes immutable artifacts in this exact crash-safe
order: rows sidecar, FeatureSeal sidecar, `rows.jsonl` primary,
`feature_seal.json` primary.  The final primary is the commit point; a crash
before it leaves only unreferenced prerequisite artifacts.  On retry, an
existing sidecar without its later primary is an allowed prerequisite and may
only be byte-identical; the publisher resumes at the first absent stage.  An
existing primary requires its exact regular canonical sidecar already to be
present, while a conflicting, missing-required, symlink, or non-regular
artifact/sidecar fails closed and is never deleted, replaced, or repaired.
`load_feature_seal` must require and
canonical-validate its own sidecar; `load_sealed_feature_rows` must derive,
securely read, canonical-validate, and compare both the rows primary and
`rows.jsonl.sha256` before parsing a row.  Add RED tests for missing,
different, symlink, and non-regular sidecars for both paths, publication crash
points/retry, and the guarantee that an orphan rows primary without the final
FeatureSeal primary is not a usable seal.

### Task 5 authority and byte-binding amendment

The row field named `source_manifest_sha256` means the authoritative V5.2
**source inventory aggregate**, not a snapshot-replay receipt.  Its exact
value is `spec.source_authority.inventory.sha256`, the digest of the
RunSpec-bound `source_inventory.json`; `snapshot_replay_sha256` has distinct
replay semantics and must never be substituted.  The Task 4
`SourceGateReceipt.inventory_sha256` must be required to equal that same
RunSpec inventory digest.  No new RunSpec field is necessary because the
closed `source_authority.inventory: {path, sha256}` binding already owns this
authority.  The row's `exclusion_policy_sha256` is exactly
`sha256(canonical_json_bytes(spec.exclusions))`, never a caller-selected
digest.

This requires a narrow Task 3/Task 4 migration before Task 5 implementation:
the RunSpec tests must preserve and test the closed inventory binding and
canonical exclusions projection; the schema/parser/standalone loader remain
structural-only, while `_load_bound_receipt(spec, ...)`,
`require_source_gate(spec)`, and the fresh-spec `verify_source_gate` boundary
reject a receipt whose `inventory_sha256` differs from
`fresh.source_authority.inventory.sha256`.  Keep `snapshot_replay_sha256`
separate.  These are compatibility requirements for the existing Task 3/4
code, not an invitation to add a duplicate aggregate field.

Keep the low-level sealer deterministic, but make the authenticated receipt
an explicit argument so a real caller cannot self-assert a source-gate digest:

```python
seal_feature_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    feature_set_id: str,
    fold_id: str,
    rows: Sequence[SealedFeatureRow],
    parent_feature_seal_sha256: str | None = None,
    evidence_sha256: Sequence[str] = (),
) -> FeatureSeal

load_sealed_feature_rows(
    spec: ModelImprovementRunSpec,
    source_gate: SourceGateReceipt,
    years: Sequence[int],
    seal: FeatureSeal,
) -> tuple[SealedFeatureRow, ...]
```

Neither low-level function calls `require_source_gate()`; that preserves
closed synthetic tests.  Every real orchestration boundary must first call
`verified_gate = require_source_gate(spec)`, securely load the explicit
receipt path/digest supplied to that boundary, require exact payload,
resolved-path, and artifact-digest equality with `verified_gate`, and pass
only `verified_gate` onward.  The two feature functions require
`source_gate.run_spec_sha256 == run_spec_sha256(spec)`,
`source_gate.inventory_sha256 == spec.source_authority.inventory.sha256`,
every row's `source_gate_sha256 == source_gate.artifact_sha256`, every row's
`source_manifest_sha256 == spec.source_authority.inventory.sha256 ==
source_gate.inventory_sha256`, every row's `exclusion_policy_sha256 ==
sha256(canonical_json_bytes(spec.exclusions))`, and
`seal.source_gate_sha256 == source_gate.artifact_sha256`.  Thus the real
path cannot establish authority by supplying matching self-asserted row
digests.  Update downstream Common/Lane A, Lane B, Lane C, and H24 call sites
to pass their already verified receipt; no caller may retain the former
receipt-free signature.

The canonical `rows.jsonl` preimage is the exact concatenation, in strict
ascending `(official_date, scheduled_start_utc, game_pk)` order, of
`canonical_json_bytes(row.as_mapping())` for each row.  It has exactly one
newline per row, no blank lines, and `rows_sha256` is SHA-256 of those exact
raw bytes.  `population_sha256` is SHA-256 of
`canonical_json_bytes([{"game_pk": game_pk, "official_date": official_date,
"target_year": target_year}, ...])` over that same ordered row sequence.
It contains no score, quality, feature hash, result, or outcome value.  Both
sealing and loading recompute both preimages and reject an empty population,
duplicate `game_pk`, duplicate population key, or any non-strict order.
`load_feature_seal` checks only descriptor payload/sidecar/path structure;
the receipt-bound `load_sealed_feature_rows` is the sole public row reader.

- [ ] **Step 1: Write RED tests for six component sides, D-1/T-45 provenance, 2025/future/target outcome rejection, duplicate game rejection, environment/defense 50/0 fallback, and permitted Lane B denominator recipes.**

```python
class SealedFeatureTest(unittest.TestCase):
    def test_seal_rereads_complete_result_free_rows(self):
        seal = seal_feature_rows(spec, verified_gate, "common", "H22", complete_rows)
        self.assertEqual(seal.schema, "MODEL_IMPROVEMENT_FEATURE_SEAL_V1")
        self.assertEqual(seal.fold_id, "H22")
        self.assertNotIn("artifact_sha256", seal.as_mapping())
        self.assertNotIn("path", seal.as_mapping())
        self.assertEqual(load_feature_seal(Path(seal.path), seal.artifact_sha256), seal)
        self.assertEqual(load_sealed_feature_rows(spec, verified_gate, (2022,), seal), tuple(complete_rows))

    def test_outcome_or_future_provenance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "outcome|history|cutoff"):
            seal_feature_rows(spec, verified_gate, "common", "H22", [row_with_forbidden_home_win])

    def test_lane_c_feature_seal_requires_parent_and_evidence(self):
        common = seal_feature_rows(spec, verified_gate, "common", "H22", complete_rows)
        lane_c = seal_feature_rows(spec, verified_gate, "lane_c", "H22", complete_rows, common.artifact_sha256, ("b" * 64,))
        self.assertEqual(lane_c.parent_feature_seal_sha256, common.artifact_sha256)
        self.assertEqual(lane_c.evidence_sha256, ("b" * 64,))
        with self.assertRaisesRegex(ValueError, "parent feature seal|evidence"):
            seal_feature_rows(spec, verified_gate, "lane_c", "H22", complete_rows)
```

Add RED cases for: exact JSONL byte and population preimages; tampered
row/rows bytes, sidecar, and seal payload; duplicate game/key and out-of-order
rows; an empty population; wrong requested years/fold/feature set; a source
gate receipt whose RunSpec or inventory differs; row/seal source-gate mismatch;
source inventory versus snapshot replay substitution; an exclusion projection
mismatch; missing component/side/input hashes; forbidden labels/results in
input or persisted JSONL; noncanonical/blank JSONL lines; and an AST import
denial proving `sealed_features` imports no outcome module or outcome fixture.
The real-boundary test must prove a caller-provided receipt differing in
payload, resolved path, or digest from `require_source_gate(spec)` is rejected
before any rows or seal are written.

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

Persist exactly `schema="MODEL_IMPROVEMENT_FEATURE_SEAL_V1"`, `feature_set_id`, `fold_id`, `run_spec_sha256`, `source_gate_sha256`, `population_sha256`, `rows_sha256`, `parent_feature_seal_sha256`, and `evidence_sha256`; `artifact_sha256` and `path` are returned runtime descriptors and excluded by `FeatureSeal.as_mapping()`. `feature_set_id` is closed to `common` and `lane_c`. A `common` seal requires `parent_feature_seal_sha256 is None` and empty `evidence_sha256`; a `lane_c` seal requires a nonempty parent common feature-seal digest for the same fold and a nonempty immutable evidence-digest tuple. The exact public signatures are the receipt-bound signatures above; this contract deliberately has no `target_years` field: callers select rows with the existing `years` parameter, while the accepted year set is validated from each row’s `target_year` and the declared `fold_id`; the loader returns that immutable tuple. `load_feature_seal` validates the structural `features/{feature_set_id}/{fold_id}/` path and common/lane-C parent/evidence shape; receipt-bound `load_sealed_feature_rows` additionally validates the descriptor against the supplied RunSpec, authenticated source gate, requested years, and persisted rows. Both recompute the exact JSONL rows and label-free population preimages, recompute payload/row digests, and reject a seal from another feature set. Validate the source-gate/source-inventory/exclusion bindings defined above, all component input hashes, baseline component score/quality preservation, and exactly earlier history. Validate each `MetricPrimitive` tree and preserve its normalized score, aggregation weight, recursive quality recipe, and child order. The only multiplier-enabled recipe families are existing starter/hitter-split, recent-form, and bullpen denominators; every other family rejects a Lane B multiplier. Seal JSONL rows and the feature-seal payload under `features`, hash/reread both artifacts and sidecars, and load them through the same fields before returning.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_sealed_features -v`

Expected: PASS; B has sufficient primitives and C starts with explicit neutral slots.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/feature_seal.schema.json mlb_backtest/model_improvement/sealed_features.py tests/test_model_improvement_sealed_features.py mlb_backtest/model_improvement/canonical.py tests/test_model_improvement_canonical.py
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

### Task 7: Persist pre-prediction scoring cards

**Files:**
- Create: `schemas/model_improvement/scoring_model_card.schema.json`
- Create: `mlb_backtest/model_improvement/scoring_cards.py`
- Modify: `mlb_backtest/model_improvement/contracts.py`
- Create: `tests/test_model_improvement_scoring_cards.py`

- [ ] **Step 1: Write RED tests for a candidate scoring card that is sealed before its prediction, rejects a changed weight/calibration/candidate/parent/feature binding, and for the explicit v5.1 exception.**

```python
class ScoringCardTest(unittest.TestCase):
    def test_candidate_card_is_the_pre_prediction_weight_authority(self):
        card = seal_scoring_model_card(spec, "lane_a", "H23/scoring_model_card.json", make_a_payload())
        self.assertEqual(card.prediction_contract_sha256, spec.prediction_contract_sha256)
        self.assertEqual(card.parameters["component_weights_pp"], V51_WEIGHTS)
        self.assertEqual(load_scoring_model_card(spec, Path(card.path), card.artifact_sha256), card)

    def test_baseline_uses_the_existing_runspec_card_without_a_wrapper(self):
        ref = resolve_prediction_weight_authority(spec, model_id="v5.1", scoring_card=None)
        self.assertEqual(ref.path, spec.baseline.card.path)
        self.assertEqual(ref.sha256, spec.baseline.card.sha256)
        self.assertEqual(ref.component_weights_pp, spec.baseline.card.component_weights_pp)
        with self.assertRaisesRegex(ValueError, "v5.1.*scoring card"):
            seal_scoring_model_card(spec, "lane_a", "H23/v51-wrapper.json", make_v51_payload())
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_scoring_cards -v`

Expected: FAIL because the scoring-card API does not exist.

- [ ] **Step 3: Implement `MODEL_IMPROVEMENT_SCORING_MODEL_CARD_V1`, its closed schema, persistence, and the one closed prediction-weight union.**

```python
def seal_scoring_model_card(
    spec: ModelImprovementRunSpec,
    namespace: Literal["lane_a", "lane_b", "lane_c", "h24_predictions"],
    relative_name: str,
    payload: Mapping[str, object],
) -> ScoringModelCard

def load_scoring_model_card(
    spec: ModelImprovementRunSpec, path: Path, expected_sha256: str,
) -> ScoringModelCard

def resolve_prediction_weight_authority(
    spec: ModelImprovementRunSpec, model_id: str, scoring_card: ScoringModelCard | None,
) -> PredictionWeightAuthority
```

Persist exactly `schema="MODEL_IMPROVEMENT_SCORING_MODEL_CARD_V1"`, `model_id`, `lane_id`, `fold_id`, `candidate_id`, `run_spec_sha256`, `source_gate_sha256`, `baseline_replay_sha256`, `parent_card_sha256`, `feature_seal_sha256`, `prediction_contract_sha256`, `calibration_card_sha256`, `candidate_payload_sha256`, and immutable `parameters`; add runtime `artifact_sha256`/`path` only after canonical write, secure reread, and digest validation. `prediction_contract_sha256` must always equal `spec.prediction_contract_sha256`. A candidate card must exist before its H22/H23/H24 prediction seal, must have the exact six integer pp weights totaling 100, and must bind the feature seal, calibration evidence, parent card where applicable, and selected candidate payload where applicable. Lane A has `candidate_id is None` and unchanged baseline weights; Lane B has a nonempty selected/grid candidate ID; Lane C binds the A+B parent and its own feature transform/coverage evidence. H24 uses the declared `h24_predictions/{model_id}/scoring_model_card.json` namespace, `fold_id="H24"`, a required final-card parent, and the exact H24 feature seal; it is created and reread before the registry seals it. Reject `model_id="v5.1"`: baseline uses only the existing canonical card path/digest/weights directly pinned in `spec.baseline.card`; no wrapper card or generated artifact is allowed.

`resolve_prediction_weight_authority` is the only common scorer entry point for this choice. It accepts `(model_id="v5.1", scoring_card=None)` only when `spec.baseline.card.path`, `spec.baseline.card.sha256`, and `spec.baseline.card.component_weights_pp` verify. It accepts `(model_id in {"A","A+B","A+B+C"}, verified scoring_card)` only when its model, contract digest, and weights verify. All other combinations fail closed. This produces a uniform `scoring_model_card_sha256`: baseline uses `spec.baseline.card.sha256`; a candidate uses its `ScoringModelCard.artifact_sha256`.

`ScoringModelCard` remains the pure dataclass defined in `contracts.py`. Only `scoring_cards.py` owns and exports `seal_scoring_model_card`, `load_scoring_model_card`, and `resolve_prediction_weight_authority`; it imports the dataclass from `contracts.py`, while `contracts.py` imports none of those persistence or resolver functions. Every advertised import and package re-export preserves this one-way dependency.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_scoring_cards tests.test_model_improvement_contracts tests.test_model_improvement_run_spec -v`

Expected: PASS; cards are immutable before prediction, and baseline cannot acquire a wrapper.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/scoring_model_card.schema.json mlb_backtest/model_improvement/scoring_cards.py mlb_backtest/model_improvement/contracts.py tests/test_model_improvement_scoring_cards.py tests/test_model_improvement_contracts.py
git commit -m "feat: seal model improvement scoring cards"
```

### Task 8: Seal OOF predictions before joining outcomes

**Files:**
- Create: `schemas/model_improvement/prediction_seal.schema.json`
- Create: `mlb_backtest/model_improvement/oof_ledger.py`
- Create: `tests/test_model_improvement_oof_ledger.py`

- [ ] **Step 1: Write RED tests denying labels in a prediction seal, missing/extra/duplicate joined labels, tampered persisted rows, unscoped or colliding Lane-B candidate IDs, 2025 labels, and a loader that rejects a seal-bound row path whose JSONL bytes, row digest, game uniqueness/order, RunSpec/source/prediction-contract/scoring-card binding differ.**

```python
class OofLedgerTest(unittest.TestCase):
    def test_prediction_precedes_verified_outcome_join(self):
        prediction = seal_oof_predictions(spec, "lane_a/A/O19", prediction_rows)
        joined = join_oof_outcomes(spec, load_prediction_seal(Path(prediction.path), prediction.artifact_sha256), outcomes)
        self.assertEqual(load_joined_ledger(Path(joined.path), joined.artifact_sha256), joined)

    def test_authoritative_row_loader_rereads_the_bound_jsonl(self):
        seal = seal_oof_predictions(spec, "lane_a/A/O19", prediction_rows)
        self.assertEqual(load_prediction_rows(spec, seal), tuple(prediction_rows))
        self.assertEqual(load_prediction_rows(spec, seal)[0].prediction_contract_sha256,
                         spec.prediction_contract_sha256)

    def test_joined_row_loader_returns_only_verified_label_bindings(self):
        prediction = seal_oof_predictions(spec, "lane_a/A/O19", prediction_rows)
        joined = join_oof_outcomes(spec, prediction, outcomes)
        self.assertEqual(load_joined_prediction_rows(spec, joined)[0][1], outcomes[prediction_rows[0].game_pk])
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger -v`

Expected: FAIL with `ModuleNotFoundError` for `oof_ledger`.

- [ ] **Step 3: Implement `JoinedLedger`, `seal_oof_predictions(spec, ledger_id, rows)`, `load_prediction_seal(path, expected_sha256)`, `load_prediction_rows(spec: ModelImprovementRunSpec, seal: PredictionSeal) -> tuple[PredictionRow, ...]`, `load_joined_prediction_rows(spec: ModelImprovementRunSpec, ledger: JoinedLedger) -> tuple[tuple[PredictionRow, int], ...]`, `join_oof_outcomes(spec, prediction, outcomes)`, and `load_joined_ledger(path, expected_sha256)`.** `PredictionRow` schema parsing and every row producer must populate the persisted `prediction_contract_sha256` from `spec.prediction_contract_sha256`; no caller-supplied different value is accepted. `load_prediction_rows` is the sole public prediction-row loader: derive the row JSONL location from the seal’s declared artifact location, never from an assumption that the descriptor file contains rows; read only regular canonical JSONL bytes; verify `rows_sha256`, population digest, unique ordered game keys, and all RunSpec/source-gate/prediction-contract/scoring-card bindings before constructing immutable rows. Each row must have `PredictionRow.prediction_contract_sha256 == spec.prediction_contract_sha256 == seal.prediction_contract_sha256`, and `PredictionRow.scoring_model_card_sha256` must equal the seal field. A nonbaseline card is securely loaded and must exist before sealing; baseline has no wrapper and its field must equal `spec.baseline.card.sha256`. `ledger_id` must decode to the exact `lane_id`, model/candidate identity, and fold; all Lane-B OOF paths are exactly `oof/lane_b/{candidate_id}/{fold_id}/prediction_seal.json` and sibling `prediction_rows.jsonl`/joined artifacts, never a bare `O19`–`O23` path. Prediction rows contain no labels. `load_joined_prediction_rows` is the sole public labeled-row accessor: derive its row JSONL from the `JoinedLedger` descriptor, verify regular canonical bytes, the ledger digest, scoped `ledger_id`/target-year RunSpec binding, immutable prediction-seal/source/prediction-contract/scoring-card bindings, one ordered unique game per row, and exactly one binary label per game before returning immutable `(PredictionRow, label)` pairs. `join_oof_outcomes` uses only the prediction-row loader, validates one binary label per game, writes a separate immutable joined artifact under `oof`, and returns a digest/path descriptor computed from payload bytes rather than embedded in either prediction or joined payload. An H22/H23 seal requires `frozen_model_card_sha256 is None`; H24 handling is owned by the H24 plan. Export both row loaders publicly; Lane B must use them rather than descriptor fields or local row readers.

- [ ] **Step 4: Run GREEN.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_oof_ledger -v`

Expected: PASS; calibration can consume only verified joined ledgers.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/prediction_seal.schema.json mlb_backtest/model_improvement/oof_ledger.py tests/test_model_improvement_oof_ledger.py
git commit -m "feat: seal model improvement OOF ledgers"
```

### Task 9: Implement post-evaluation frozen-card persistence and Lane A rolling-origin calibration

**Files:**
- Create: `schemas/model_improvement/frozen_model_card.schema.json`
- Modify: `mlb_backtest/model_improvement/contracts.py`
- Create: `mlb_backtest/model_improvement/lane_a.py`
- Create: `tests/test_model_improvement_lane_a.py`

- [ ] **Step 1: Write RED tests for Common frozen-card schema/payload round trips, required six-key integer `parameters.component_weights_pp` summing to 100, and H22 O19–O21/H23 O19–O22 Lane A calibration, unchanged scores/weights/shrinkage/clipping/pick/rank, H22/H23 prediction seals, and strict H23 lower Brier plus lower logloss.**

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
        self.assertEqual(result.prediction.scoring_model_card_sha256,
                         result.scoring_card.artifact_sha256)
        self.assertEqual(result.card.prediction_contract_sha256, spec.prediction_contract_sha256)
        self.assertEqual(result.card.scoring_model_card_sha256, result.scoring_card.artifact_sha256)
        self.assertEqual(result.card.prediction_seal_sha256, result.prediction.artifact_sha256)
        self.assertEqual(result.card.parameters["component_weights_pp"],
                         {"starter": 35, "offense": 25, "bullpen": 20,
                          "environment": 10, "defense": 5, "schedule": 5})

    def test_h23_non_improvement_emits_rejected_disposition_without_card(self):
        result = fit_lane_a_fold(spec, "H23", feature_seal, worse_joined_ledgers, baseline_receipt)
        self.assertIsNone(result.card)
        self.assertEqual(result.disposition.state, "REJECTED")
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_a -v`

Expected: FAIL with `ModuleNotFoundError` for `lane_a`.

- [ ] **Step 3: Implement the Common closed `frozen_model_card.schema.json`, post-evaluation card persistence, and `LaneAFoldResult` with fields `fold_id`, `scoring_card: ScoringModelCard`, `card: FrozenModelCard | None`, `disposition: LaneDisposition`, `prediction: PredictionSeal`, `evaluation: EvaluationReport`, and `baseline_evaluation: EvaluationReport`; implement `fit_lane_a_fold(spec, fold_id, feature_seal, joined_ledgers, baseline_replay)` and `load_lane_a_fold(spec, fold_id)`.** The exact card APIs are:

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

The sealer validates the closed model/state/fold schema, namespace/model compatibility, and `parameters.component_weights_pp`: exactly the six component keys in RunSpec order, integer nonnegative pp values totaling 100. It writes a canonical payload with no runtime `artifact_sha256`/`path`, rereads and hashes its persisted bytes before constructing descriptor fields, and returns the loader result. The loader validates the regular canonical bytes, expected/sidecar digest, exact RunSpec namespace/model/fold path, payload schema, and the same card-weight rule before constructing an immutable card. A frozen card must persist `scoring_model_card_path` and `scoring_model_card_sha256`, securely reload that exact preexisting scoring card, and prove its model/fold/RunSpec/source/feature/calibration/candidate/weight bindings match the final card. It then binds the H23 prediction seal and H23 evaluation digest. Generic parameter values remain recursively immutable but lane-specific parameter keys remain lane-owned. It never discovers cards by scanning or accepts fabricated descriptors; H24 additionally requires the caller to supply its exact registry-pinned scoring-card and frozen-card path/digest pairs.

For every OOF target use only that fold’s declared prior-season pool, score frozen v5.1 structure, fit `normalize.fit_logistic` only on earlier joined OOF predictions, then seal the candidate `ScoringModelCard` before sealing its H22/H23 per-game probabilities/ranks. The `PredictionSeal.prediction_contract_sha256` must equal `spec.prediction_contract_sha256`, never its artifact digest, and `PredictionSeal.scoring_model_card_sha256` must equal the exact scoring-card digest whose `component_weights_pp` the scorer used; H22/H23 seals set `frozen_model_card_sha256=None`. Replay the v5.1 baseline on exactly those IDs, call `candidate_evaluation, baseline_evaluation = evaluate_predictions(candidate_prediction, baseline_prediction, outcomes)`, and retain those two reports in `LaneAFoldResult`. H22 emits `QUALIFIED` with reason `H22_EVALUATED` and no final card. H23 emits the sole post-evaluation `H23_FROZEN` card only when both strict metric comparisons pass; H23 failure emits `REJECTED`, reason code(s) `H23_BRIER_NOT_STRICTLY_LOWER` and/or `H23_LOG_LOSS_NOT_STRICTLY_LOWER`, and no final card. The direct baseline RunSpec pin and Lane A scoring card both use unchanged `35/25/20/10/5/5` weights; tests reject any A-card reweighting. A frozen H23 payload binds source gate, feature seal, all OOF ledgers, baseline replay, alpha/beta, its scoring-card path/digest, evaluation, and the prediction artifact: `prediction_contract_sha256 == spec.prediction_contract_sha256`, `prediction_seal_sha256 == PredictionSeal.artifact_sha256`, and `scoring_model_card_sha256 == PredictionSeal.scoring_model_card_sha256`. It reads no H24 outcome/metric/parameter.

- [ ] **Step 4: Run GREEN plus existing scoring regressions.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_a tests.test_scoring tests.test_normalize tests.test_ranking tests.test_metrics -v`

Expected: PASS; only OOF calibration is added to frozen scoring behavior.

- [ ] **Step 5: Commit.**

```bash
git add schemas/model_improvement/frozen_model_card.schema.json mlb_backtest/model_improvement/contracts.py mlb_backtest/model_improvement/lane_a.py tests/test_model_improvement_lane_a.py tests/test_model_improvement_contracts.py
git commit -m "feat: freeze rolling-origin lane a"
```

### Task 10: Publish the package API and verify the Common handoff

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
        self.assertTrue(callable(load_scoring_model_card))
        self.assertTrue(callable(load_frozen_model_card))
        self.assertIsNotNone(SealedFeatureRow)
        self.assertIsNotNone(FrozenModelCard)
        self.assertIsNotNone(ScoringModelCard)
        self.assertIsNotNone(BaselineReplayReceipt)
        self.assertIsNotNone(LaneDisposition)
```

- [ ] **Step 2: Run RED.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_public_api -v`

Expected: FAIL until all explicit exports are present.

- [ ] **Step 3: Export only the listed public types/functions from `__init__.py`, including `BaselineReplayReceipt`, `load_baseline_replay`, `EvaluationReport`, `evaluate_predictions`, `load_evaluation_report`, `MetricPrimitive`, `ScoringModelCard`, `seal_scoring_model_card`, `load_scoring_model_card`, `resolve_prediction_weight_authority`, `LaneDisposition`, `seal_lane_disposition`, `load_lane_disposition`, `seal_frozen_model_card`, `load_frozen_model_card`, and the authoritative `load_prediction_rows`/`load_joined_prediction_rows`. Re-export `ScoringModelCard` from its canonical `contracts` definition, but import/re-export the scoring-card seal/load/resolve functions only from `scoring_cards`; `contracts` never aliases or imports them. These disposition names are re-exported from their canonical `lane_disposition` module, never aliased back through `contracts`; do not re-export legacy `RunSpec`, source acquisition, or outcome loaders.**

- [ ] **Step 4: Run all Common/Lane A suites.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_canonical tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_source_gate tests.test_model_improvement_sealed_features tests.test_model_improvement_baseline_replay tests.test_model_improvement_evaluation tests.test_model_improvement_scoring_cards tests.test_model_improvement_oof_ledger tests.test_model_improvement_lane_a tests.test_model_improvement_public_api -v`

Expected: PASS; fixture tests pass while real execution remains blocked without verified 2018–2021 sources.

- [ ] **Step 5: Commit.**

```bash
git add mlb_backtest/model_improvement/__init__.py tests/test_model_improvement_public_api.py
git commit -m "feat: publish model improvement common API"
```

## Final verification and sibling contract

- [ ] **Step 1: Run syntax, lint, and regression verification.**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q mlb_backtest/model_improvement && ruff check mlb_backtest/model_improvement tests/test_model_improvement_*.py && PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_canonical tests.test_model_improvement_contracts tests.test_model_improvement_run_spec tests.test_model_improvement_source_gate tests.test_model_improvement_sealed_features tests.test_model_improvement_baseline_replay tests.test_model_improvement_evaluation tests.test_model_improvement_scoring_cards tests.test_model_improvement_oof_ledger tests.test_model_improvement_lane_a tests.test_model_improvement_public_api tests.test_scoring tests.test_normalize tests.test_ranking tests.test_metrics tests.test_leakage -v`

Expected: PASS with no lint findings; a real source-gate call still errors until verified 2018–2021 artifacts exist.

- [ ] **Step 2: Inspect the implementation patch.**

Run: `git diff --check && git status --short`

Expected: no whitespace error; only files owned by this plan are present.

Lane B reads `spec.lane_b`, shared feature seals including rematerializable metric primitives, the H23-frozen A card, and common evaluation; it seals each selected candidate scoring card before candidate-qualified prediction paths, then publishes either `QUALIFIED` A+B or `REJECTED` evidence through `LaneDisposition` and never owns a registry. Lane C reads exactly `environment=(park, weather_roof)` and `defense=(oaa, framing, blocking_throwing)` from `spec.lane_c`, consumes `SealedFeatureRow` and the A+B card, seals separate H22-development and H23-selection scoring cards before their corresponding prediction seals, and emits either H22 development evidence or H23 `QUALIFIED` A+B+C/`ABSENT`/`REJECTED` evidence through `LaneDisposition`; only the H23 card/seal/evaluation can produce a frozen card. It seals H22/H23 coverage features and registry-pinned prediction-only H24 features without H24 outcomes, and never changes package exports. H24 owns the one candidate registry and consumes Common’s RunSpec, source-gate receipt, `BaselineReplayReceipt`, feature/prediction seals, evaluations, scoring cards, dispositions, and frozen cards.

No real H22 orchestration is authorized until a fresh `load_verified_run_spec()` followed by `require_source_gate()` validates the actual 2018–2021 source authority and deterministic replay; pure Common fit/validator APIs continue to operate only on already-bound inputs for synthetic tests.
