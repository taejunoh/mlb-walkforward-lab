# Model Improvement Lane B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate exactly 210 preregistered Lane B candidates, select an eligible H23 result, and emit only an optional frozen A+B card or a Common LaneDisposition.

**Architecture:** Lane B runs entirely in mlb_backtest/model_improvement and leaves v5.1 unchanged. It reads Common and Lane A artifacts through explicit paths, seals every candidate decision, and leaves the sole H24 candidate registry to the H24 workflow.

**Tech Stack:** Python 3.11 standard library, dataclasses, unittest, existing normalization/ranking/metrics, canonical JSON/SHA-256, Ruff, Git.

**Spec:** docs/superpowers/specs/2026-08-31-parallel-model-result-improvement-design.md

## Global Constraints

- Consume only ModelImprovementRunSpec/load_run_spec, SourceGateReceipt/load_source_gate_receipt, FeatureSeal/SealedFeatureRow/load_feature_seal/load_sealed_feature_rows, PredictionRow/PredictionSeal/load_prediction_seal/load_prediction_rows, JoinedLedger/load_joined_prediction_rows, FrozenModelCard/seal_frozen_model_card/load_frozen_model_card, LaneDisposition/seal_lane_disposition, canonical_json_bytes, and write_immutable_json.
- Read every Common artifact digest from artifact_sha256 and its immutable location from path; never use a sha256 field. artifact_sha256 is computed from persisted payload bytes; both runtime artifact_sha256 and path are excluded from that payload. A LaneAFoldResult supplies evidence only as result.card, result.prediction, and result.evaluation, never as a digest field on result.
- Do not create/import .common, scan directories, call spec.lane_root, create a registry, or import H24 outcomes.
- All artifact paths are CLI arguments. Do not modify config/v5_1_bt.json, scoring.py, config.py, locking.py, outcomes.py, live_model_card.py, or a v5_2 authority.
- Fixed weights are environment 10pp and defense 5pp. The other four are 5pp increments within ±10pp of v5.1 and all six sum to 100pp. Multiplier is .75, 1.0, or 1.25.
- H22 labels are O19–O21; H23 labels are O19–O22; optional H24 card labels are O19–O23. H24 labels are inaccessible.
- Brier/log loss include every eligible game. Daily Top-1 pairs by official date. Canonical JSON has exactly one trailing newline.

---

## Files and DAG

Create lane_b_candidates.py, lane_b_scoring.py, lane_b_calibration.py, lane_b_evaluation.py, lane_b_ledger.py, lane_b_runner.py in mlb_backtest/model_improvement, with matching tests/test_model_improvement_lane_b_*.py files.

~~~text
verified Common/Lane-A paths with SOURCES_READY source gate -> grid -> score -> candidate OOF calibration
 -> evaluation -> H22 immutable 210-entry ledger -> H23 dependency gate
 -> A+B H23_FROZEN card OR LaneDisposition -> H24 registry outside Lane B
~~~

### Task 1: Preregister the exact candidate grid

**Files:**

- Create: mlb_backtest/model_improvement/lane_b_candidates.py
- Create: tests/test_model_improvement_lane_b_candidates.py

**Interfaces:**

- Produces Candidate, generate_candidates() -> tuple[Candidate, ...], validate_candidate(candidate: Candidate) -> None, candidate_payload(candidate: Candidate) -> dict[str, object].
- Tasks 2–5 consume Candidate.

- [ ] **Step 1: Write the failing TestCase.**

~~~python
import math
import unittest
from mlb_backtest.model_improvement.lane_b_candidates import generate_candidates


class LaneBCandidateTest(unittest.TestCase):
    def test_grid_is_exactly_210(self):
        rows = generate_candidates()
        self.assertEqual(210, len(rows))
        self.assertEqual(210, len({row.candidate_id for row in rows}))
        for row in rows:
            self.assertEqual(10, row.weights_pp["environment"])
            self.assertEqual(5, row.weights_pp["defense"])
            self.assertEqual(100, sum(row.weights_pp.values()))
            self.assertIn(row.shrinkage_multiplier, (.75, 1., 1.25))
~~~

- [ ] **Step 2: Run RED.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_candidates.LaneBCandidateTest -v

Expected: ModuleNotFoundError for lane_b_candidates.

- [ ] **Step 3: Implement grid, IDs, and validation.**

~~~python
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

_GRID = {"starter": (25, 30, 35, 40, 45), "offense": (15, 20, 25, 30, 35),
         "bullpen": (10, 15, 20, 25, 30), "schedule": (0, 5, 10, 15)}
_MULTIPLIERS = ((75, .75), (100, 1.), (125, 1.25))
_KEYS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    weights_pp: Mapping[str, int]
    shrinkage_multiplier: float


def candidate_id(starter, offense, bullpen, schedule, multiplier_bp):
    return f"b-s{starter:02d}-o{offense:02d}-b{bullpen:02d}-sch{schedule:02d}-m{multiplier_bp:03d}"


def generate_candidates():
    rows = []
    for s in _GRID["starter"]:
        for o in _GRID["offense"]:
            for b in _GRID["bullpen"]:
                for q in _GRID["schedule"]:
                    if s + o + b + q == 85:
                        for bp, multiplier in _MULTIPLIERS:
                            rows.append(Candidate(candidate_id(s, o, b, q, bp),
                                MappingProxyType({"starter": s, "offense": o, "bullpen": b,
                                                  "environment": 10, "defense": 5, "schedule": q}), multiplier))
    return tuple(sorted(rows, key=lambda row: row.candidate_id))


def validate_candidate(candidate):
    weights = dict(candidate.weights_pp)
    if not isinstance(candidate, Candidate) or tuple(weights) != _KEYS:
        raise ValueError("candidate type or keys differ")
    if any(type(value) is not int or value < 0 for value in weights.values()):
        raise ValueError("candidate weights differ")
    if weights["environment"] != 10 or weights["defense"] != 5 or sum(weights.values()) != 100:
        raise ValueError("candidate fixed weights or sum differ")
    if any(weights[name] not in _GRID[name] for name in _GRID):
        raise ValueError("candidate grid differs")
    if candidate.shrinkage_multiplier not in (.75, 1., 1.25):
        raise ValueError("candidate multiplier differs")
    if candidate.candidate_id != candidate_id(weights["starter"], weights["offense"], weights["bullpen"],
                                               weights["schedule"], int(candidate.shrinkage_multiplier * 100)):
        raise ValueError("candidate ID differs")


def candidate_payload(candidate):
    validate_candidate(candidate)
    return {"schema_version": "LANE_B_CANDIDATE_V1", "candidate_id": candidate.candidate_id,
            "weights_pp": dict(candidate.weights_pp), "shrinkage_multiplier": candidate.shrinkage_multiplier}
~~~

- [ ] **Step 4: Add changed-fixed-weight test and run GREEN.**

~~~python
    def test_changed_fixed_weight_is_rejected(self):
        from mlb_backtest.model_improvement.lane_b_candidates import Candidate, validate_candidate
        with self.assertRaisesRegex(ValueError, "fixed weights"):
            validate_candidate(Candidate("b-s35-o25-b20-sch05-m100",
                {"starter": 35, "offense": 25, "bullpen": 20, "environment": 9, "defense": 6, "schedule": 5}, 1.))
~~~

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_candidates.LaneBCandidateTest -v

Expected: PASS; 70 valid tuples times 3 multipliers equals 210.

- [ ] **Step 5: Commit.**

~~~bash
git add mlb_backtest/model_improvement/lane_b_candidates.py tests/test_model_improvement_lane_b_candidates.py
git commit -m "feat: preregister lane b candidates"
~~~

### Task 2: Score candidate rows without changing pick semantics

**Files:**

- Create: mlb_backtest/model_improvement/lane_b_scoring.py
- Create: tests/test_model_improvement_lane_b_scoring.py

**Interfaces:**

- Consumes only a FeatureSeal whose feature_set_id is common, SealedFeatureRow.metric_primitives[side][component], component_scores, baseline_component_scores, Candidate, probability, predicted_pick, rank_daily, QualityRecipe, and MetricPrimitive.
- Produces materialize_quality(recipe: QualityRecipe, multiplier: float) -> float, materialize_component(primitives: Sequence[MetricPrimitive], multiplier: float) -> tuple[float, float], materialize_side(primitives: Mapping[str, Sequence[MetricPrimitive]], component_scores: Mapping[str, float], component_qualities: Mapping[str, float], baseline_scores: Mapping[str, float], baseline_qualities: Mapping[str, float], multiplier: float) -> tuple[dict[str, float], dict[str, float]], score_candidate_rows(rows, candidate, alpha, beta) -> tuple[dict[str, object], ...], rank_pickable_rows(rows) -> tuple[dict[str, object], ...].
- No outcome import.

- [ ] **Step 1: Write failing TestCase methods.**

~~~python
import math
import unittest
from mlb_backtest.model_improvement.contracts import QualityRecipe
from mlb_backtest.model_improvement.lane_b_scoring import materialize_quality, rank_pickable_rows


class LaneBScoringTest(unittest.TestCase):
    def test_only_declared_families_receive_multiplier(self):
        recipe = QualityRecipe(recipe_id="starter-ratio", kind="ratio", family="starter",
            numerator=150., baseline_constant=150., fixed_value=None, children=(), child_weights=(), transform_sha256="a" * 64)
        self.assertEqual(150 / 262.5, materialize_quality(recipe, .75))

    def test_weighted_sum_uses_declared_child_weights(self):
        low = QualityRecipe(recipe_id="low", kind="fixed", family=None, numerator=None,
            baseline_constant=None, fixed_value=.2, children=(), child_weights=(), transform_sha256="b" * 64)
        high = QualityRecipe(recipe_id="high", kind="fixed", family=None, numerator=None,
            baseline_constant=None, fixed_value=.8, children=(), child_weights=(), transform_sha256="c" * 64)
        recipe = QualityRecipe(recipe_id="mix", kind="weighted_sum", family=None, numerator=None,
            baseline_constant=None, fixed_value=None, children=(low, high), child_weights=(.25, .75), transform_sha256="d" * 64)
        self.assertEqual(.65, materialize_quality(recipe, 1.))

    def test_multiplier_one_replays_sealed_component_score_exactly(self):
        scores, qualities = materialize_side(self.primitives, self.sealed_component_scores,
            self.sealed_component_qualities, self.baseline_component_scores,
            self.baseline_component_qualities, 1.)
        for key in self.sealed_component_scores:
            self.assertTrue(math.isclose(self.sealed_component_scores[key], scores[key], rel_tol=0., abs_tol=1e-12))
            self.assertTrue(math.isclose(self.sealed_component_qualities[key], qualities[key], rel_tol=0., abs_tol=1e-12))

    def test_exact_half_is_no_pick_not_ranked(self):
        self.assertEqual((), rank_pickable_rows(({"game_pk": 1, "official_date": "2022-04-07",
            "picked_side": "none", "pick_probability": .5},)))
~~~

- [ ] **Step 2: Run RED.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_scoring.LaneBScoringTest -v

Expected: ModuleNotFoundError for lane_b_scoring.

- [ ] **Step 3: Implement every scoring helper.**

~~~python
import math
from collections import defaultdict
from collections.abc import Mapping
from collections.abc import Sequence
from mlb_backtest.model_improvement.contracts import MetricPrimitive, QualityRecipe
from mlb_backtest.normalize import probability
from mlb_backtest.ranking import rank_daily
from mlb_backtest.scoring import predicted_pick
from .lane_b_candidates import validate_candidate

_BASE = {"starter": 150., "hitter_split": 150., "recent_form": 100., "bullpen": 300.}
_COMPONENTS = ("starter", "offense", "bullpen", "environment", "defense", "schedule")


def materialize_quality(recipe: QualityRecipe, multiplier: float) -> float:
    if multiplier not in (.75, 1., 1.25):
        raise ValueError("unregistered multiplier")
    if not isinstance(recipe, QualityRecipe) or not recipe.recipe_id or len(recipe.transform_sha256) != 64:
        raise ValueError("QualityRecipe differs")
    if recipe.kind == "fixed":
        value = float(recipe.fixed_value)
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("fixed quality differs")
        return value
    if recipe.kind == "ratio":
        if recipe.family not in _BASE or recipe.numerator is None or recipe.baseline_constant is None:
            raise ValueError("ratio recipe differs")
        numerator, baseline = float(recipe.numerator), float(recipe.baseline_constant)
        if not math.isfinite(numerator) or not math.isfinite(baseline) or numerator < 0 or baseline <= 0:
            raise ValueError("ratio bounds differ")
        return numerator / (numerator + baseline * multiplier) if numerator else 0.
    if recipe.kind == "minimum" and recipe.children:
        return min(materialize_quality(child, multiplier) for child in recipe.children)
    if recipe.kind == "weighted_sum" and recipe.children:
        if len(recipe.child_weights) != len(recipe.children):
            raise ValueError("weighted_sum child weights differ")
        total = sum(float(weight) for weight in recipe.child_weights)
        if not math.isfinite(total) or total <= 0 or any(float(weight) < 0 for weight in recipe.child_weights):
            raise ValueError("weighted_sum child weights differ")
        return sum(float(weight) * materialize_quality(child, multiplier)
                   for child, weight in zip(recipe.children, recipe.child_weights, strict=True)) / total
    raise ValueError("quality recipe differs")


def materialize_component(primitives: Sequence[MetricPrimitive], multiplier: float) -> tuple[float, float]:
    if not primitives or any(not isinstance(item, MetricPrimitive) for item in primitives):
        raise ValueError("MetricPrimitive sequence differs")
    total = sum(float(item.aggregation_weight) for item in primitives)
    if not math.isfinite(total) or not math.isclose(total, 1., rel_tol=0., abs_tol=1e-12) or any(float(item.aggregation_weight) < 0 for item in primitives):
        raise ValueError("MetricPrimitive weights differ")
    pairs = []
    for item in primitives:
        normalized = float(item.normalized_score)
        if not math.isfinite(normalized) or not 0 <= normalized <= 100:
            raise ValueError("MetricPrimitive normalized scores differ")
        quality = materialize_quality(item.quality_recipe, multiplier)
        pairs.append((float(item.aggregation_weight), 50. + quality * (normalized - 50.), quality))
    return sum(weight * score for weight, score, _ in pairs), sum(weight * quality for weight, _, quality in pairs)


def materialize_side(primitives, component_scores, component_qualities, baseline_scores, baseline_qualities, multiplier):
    if (set(primitives) != set(_COMPONENTS) or set(component_scores) != set(_COMPONENTS)
            or set(component_qualities) != set(_COMPONENTS) or set(baseline_scores) != set(_COMPONENTS)
            or set(baseline_qualities) != set(_COMPONENTS)):
        raise ValueError("component keys differ")
    scores, quality = {"environment": 50., "defense": 50.}, {"environment": 0., "defense": 0.}
    for name in ("starter", "offense", "bullpen", "schedule"):
        expected, expected_quality = float(component_scores[name]), float(component_qualities[name])
        frozen, frozen_quality = float(baseline_scores[name]), float(baseline_qualities[name])
        if (not math.isfinite(expected) or not 0 <= expected <= 100 or not math.isfinite(frozen)
                or not 0 <= frozen <= 100 or not math.isfinite(expected_quality) or not 0 <= expected_quality <= 1
                or not math.isfinite(frozen_quality) or not 0 <= frozen_quality <= 1):
            raise ValueError("sealed component score differs")
        scores[name], quality[name] = materialize_component(primitives[name], multiplier)
        if multiplier == 1. and (not math.isclose(scores[name], expected, rel_tol=0., abs_tol=1e-12)
                or not math.isclose(scores[name], frozen, rel_tol=0., abs_tol=1e-12)
                or not math.isclose(quality[name], expected_quality, rel_tol=0., abs_tol=1e-12)
                or not math.isclose(quality[name], frozen_quality, rel_tol=0., abs_tol=1e-12)):
            raise ValueError("multiplier-one replay differs from sealed v5.1")
    return scores, quality


def score_candidate_rows(rows, candidate, alpha, beta):
    validate_candidate(candidate)
    result = []
    for source in rows:
        away, aq = materialize_side(source.metric_primitives["away"], source.component_scores["away"],
            source.component_qualities["away"], source.baseline_component_scores["away"],
            source.baseline_component_qualities["away"], candidate.shrinkage_multiplier)
        home, hq = materialize_side(source.metric_primitives["home"], source.component_scores["home"],
            source.component_qualities["home"], source.baseline_component_scores["home"],
            source.baseline_component_qualities["home"], candidate.shrinkage_multiplier)
        edge = sum(candidate.weights_pp[name] / 100 * (home[name] - away[name]) for name in _COMPONENTS)
        p_home = probability(edge, alpha, beta, (.10, .90))
        pick = predicted_pick("home", "away", p_home)
        result.append({"game_pk": source.game_pk, "official_date": source.official_date,
            "scheduled_start_utc": source.scheduled_start_utc, "edge": edge, "p_home": p_home,
            "picked_side": pick["predicted_side"],
            "pick_probability": pick["pick_probability"], "starter_edge_abs": abs(home["starter"] - away["starter"]),
            "confidence": 10 * sum(candidate.weights_pp[n] / 100 * min(hq[n], aq[n]) for n in _COMPONENTS)})
    return tuple(sorted(result, key=lambda row: (row["official_date"], row["game_pk"])))


def rank_pickable_rows(rows):
    days = defaultdict(list)
    for row in rows:
        if row["picked_side"] != "none":
            days[row["official_date"]].append(row)
    return tuple(row for day in sorted(days) for row in rank_daily(days[day]))
~~~

- [ ] **Step 4: Add empty-minimum test and run GREEN.**

~~~python
    def test_empty_minimum_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "quality recipe"):
            materialize_quality(QualityRecipe(recipe_id="empty", kind="minimum", family=None,
                numerator=None, baseline_constant=None, fixed_value=None, children=(), child_weights=(),
                transform_sha256="e" * 64), 1.)
~~~

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_scoring.LaneBScoringTest tests.test_scoring tests.test_normalize tests.test_ranking -v

Expected: PASS; p=.5 remains legacy no-pick and environment/defense remain neutral.

- [ ] **Step 5: Commit.**

~~~bash
git add mlb_backtest/model_improvement/lane_b_scoring.py tests/test_model_improvement_lane_b_scoring.py
git commit -m "feat: score lane b candidates"
~~~

### Task 3: Fit rolling candidate-specific OOF calibration

**Files:**

- Create: mlb_backtest/model_improvement/lane_b_calibration.py
- Create: tests/test_model_improvement_lane_b_calibration.py

**Interfaces:**

- Consumes ModelImprovementRunSpec, FeatureSeal, completed JoinedLedger objects, Candidate.
- Produces CandidateFoldCard and `fit_candidate_fold_card(spec, feature_seal, joined_ledgers: Sequence[JoinedLedger], candidate, fold_id) -> CandidateFoldCard`.

- [ ] **Step 1: Write the failing chronology TestCase.**

~~~python
import unittest
from mlb_backtest.model_improvement.lane_b_calibration import fit_candidate_fold_card


class LaneBCalibrationTest(unittest.TestCase):
    def test_h23_uses_o19_to_o22_only(self):
        card = fit_candidate_fold_card(self.spec, self.feature_seal, self.joined_ledgers, self.candidate, "H23")
        self.assertEqual((2019, 2020, 2021, 2022), card.calibration_years)
        self.assertEqual(2023, card.target_year)
~~~

- [ ] **Step 2: Run RED.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_calibration.LaneBCalibrationTest -v

Expected: ModuleNotFoundError for lane_b_calibration.

- [ ] **Step 3: Implement strict OOF fitter.**

~~~python
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from mlb_backtest.normalize import fit_logistic
from mlb_backtest.model_improvement.canonical import canonical_json_bytes
from mlb_backtest.model_improvement.oof_ledger import JoinedLedger, load_joined_prediction_rows
from mlb_backtest.model_improvement.sealed_features import load_sealed_feature_rows
from .lane_b_scoring import score_candidate_rows

_FOLD_YEARS = {"H22": (2019, 2020, 2021), "H23": (2019, 2020, 2021, 2022),
               "H24": (2019, 2020, 2021, 2022, 2023)}


@dataclass(frozen=True)
class CandidateFoldCard:
    lane: str
    fold_id: str
    target_year: int
    calibration_years: tuple[int, ...]
    candidate_id: str
    alpha: float
    beta: float
    calibration_input_sha256: str


def fit_candidate_fold_card(
    spec, feature_seal, joined_ledgers: Sequence[JoinedLedger], candidate, fold_id,
):
    years = _FOLD_YEARS.get(fold_id)
    if years is None or spec.fold(fold_id).target_year <= max(years):
        raise ValueError("fold chronology differs")
    if feature_seal.feature_set_id != "common":
        raise ValueError("FeatureSeal feature_set_id differs")
    if len(joined_ledgers) != len(years):
        raise ValueError("OOF years differ")
    labeled_rows = tuple(
        pair for ledger in joined_ledgers
        for pair in load_joined_prediction_rows(spec, ledger)
    )
    if tuple(sorted({row.target_year for row, _ in labeled_rows})) != years:
        raise ValueError("OOF years differ")
    labels = {row.game_pk: label for row, label in labeled_rows}
    if len(labels) != len(labeled_rows):
        raise ValueError("OOF duplicate games differ")
    if any(value not in (0, 1) for value in labels.values()):
        raise ValueError("OOF labels differ")
    rows = load_sealed_feature_rows(spec, years, feature_seal)
    if {row.game_pk for row in rows} != set(labels):
        raise ValueError("feature and OOF populations differ")
    trial = score_candidate_rows(rows, candidate, 0., 1.)
    alpha, beta = fit_logistic([row["edge"] / 10 for row in trial], [labels[row["game_pk"]] for row in trial])
    payload = {"fold_id": fold_id, "feature_seal_artifact_sha256": feature_seal.artifact_sha256,
               "joined_ledger_artifact_sha256s": [ledger.artifact_sha256 for ledger in joined_ledgers], "rows": trial}
    return CandidateFoldCard("B", fold_id, spec.fold(fold_id).target_year, years, candidate.candidate_id,
                             alpha, beta, sha256(canonical_json_bytes(payload)).hexdigest())
~~~

- [ ] **Step 4: Add target-year, duplicate-game, and missing-feature tests then run GREEN.**

~~~python
    def test_target_year_and_duplicate_games_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "OOF years"):
            fit_candidate_fold_card(self.spec, self.feature_seal, self.target_year_ledgers, self.candidate, "H23")
        with self.assertRaisesRegex(ValueError, "populations differ"):
            fit_candidate_fold_card(self.spec, self.missing_feature_seal, self.joined_ledgers, self.candidate, "H23")
~~~

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_calibration.LaneBCalibrationTest -v

Expected: PASS; only completed OOF labels enter candidate-specific calibration.

- [ ] **Step 5: Commit.**

~~~bash
git add mlb_backtest/model_improvement/lane_b_calibration.py tests/test_model_improvement_lane_b_calibration.py
git commit -m "feat: fit lane b rolling calibration"
~~~

### Task 4: Evaluate all games and date-paired Top-1

**Files:**

- Create: mlb_backtest/model_improvement/lane_b_evaluation.py
- Create: tests/test_model_improvement_lane_b_evaluation.py

**Interfaces:**

- Produces top1_by_date(rows, outcomes) -> dict[str, bool] and evaluate_candidate_against_baseline(candidate_rows, baseline_rows, outcomes) -> dict[str, object].
- Ledger reads all_games.candidate_brier, all_games.candidate_log_loss, daily_top1.delta.

- [ ] **Step 1: Write failing evaluation TestCase.**

~~~python
import unittest
from mlb_backtest.model_improvement.lane_b_evaluation import evaluate_candidate_against_baseline


class LaneBEvaluationTest(unittest.TestCase):
    def test_all_games_and_top1_dates_are_separate_populations(self):
        result = evaluate_candidate_against_baseline(self.candidate_rows, self.baseline_rows, self.outcomes)
        self.assertEqual(4, result["all_games"]["N"])
        self.assertEqual(2, result["daily_top1"]["N"])
~~~

- [ ] **Step 2: Run RED.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_evaluation.LaneBEvaluationTest -v

Expected: ModuleNotFoundError for lane_b_evaluation.

- [ ] **Step 3: Implement metrics and pairing.**

~~~python
from mlb_backtest.diagnostics import paired_table
from mlb_backtest.metrics import brier, log_loss, max_losing_streak


def top1_by_date(rows, outcomes):
    result = {}
    for row in rows:
        if row.get("top1") is True:
            day, game_pk = str(row["official_date"]), row["game_pk"]
            if day in result or game_pk not in outcomes or row["picked_side"] == "none":
                raise ValueError("Daily Top1 rows differ")
            result[day] = (row["picked_side"] == "home") == bool(outcomes[game_pk])
    return result


def evaluate_candidate_against_baseline(candidate_rows, baseline_rows, outcomes):
    candidate, baseline = {r["game_pk"]: r for r in candidate_rows}, {r["game_pk"]: r for r in baseline_rows}
    if len(candidate) != len(candidate_rows) or len(baseline) != len(baseline_rows) or set(candidate) != set(baseline) or set(candidate) != set(outcomes):
        raise ValueError("eligible game population differs")
    keys = sorted(candidate)
    cb = sum(brier(candidate[k]["p_home"], outcomes[k]) for k in keys) / len(keys)
    bb = sum(brier(baseline[k]["p_home"], outcomes[k]) for k in keys) / len(keys)
    cl = sum(log_loss(candidate[k]["p_home"], outcomes[k]) for k in keys) / len(keys)
    bl = sum(log_loss(baseline[k]["p_home"], outcomes[k]) for k in keys) / len(keys)
    ct, bt = top1_by_date(candidate_rows, outcomes), top1_by_date(baseline_rows, outcomes)
    if not ct or set(ct) != set(bt):
        raise ValueError("Daily Top1 date population differs")
    days, pairs = sorted(ct), paired_table([ct[d] for d in sorted(ct)], [bt[d] for d in sorted(bt)])
    return {"all_games": {"N": len(keys), "candidate_brier": cb, "baseline_brier": bb, "brier_delta": cb - bb,
                           "candidate_log_loss": cl, "baseline_log_loss": bl, "log_loss_delta": cl - bl},
            "daily_top1": {"N": len(days), "paired": pairs, "delta": (pairs["model_only"] - pairs["baseline_only"]) / len(days),
                           "candidate_max_losing_streak": max_losing_streak([ct[d] for d in days]),
                           "baseline_max_losing_streak": max_losing_streak([bt[d] for d in days])}}
~~~

- [ ] **Step 4: Add mismatched-population and different-selected-game tests then run GREEN.**

~~~python
    def test_mismatch_and_different_selected_game(self):
        with self.assertRaisesRegex(ValueError, "eligible game population differs"):
            evaluate_candidate_against_baseline(self.candidate_rows[:-1], self.baseline_rows, self.outcomes)
        result = evaluate_candidate_against_baseline(self.different_game_candidate_rows,
                                                     self.baseline_rows, self.outcomes)
        self.assertEqual(2, result["daily_top1"]["N"])
~~~

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_evaluation.LaneBEvaluationTest -v

Expected: PASS; date pairing does not join by selected game_pk.

- [ ] **Step 5: Commit.**

~~~bash
git add mlb_backtest/model_improvement/lane_b_evaluation.py tests/test_model_improvement_lane_b_evaluation.py
git commit -m "feat: evaluate lane b metrics"
~~~

### Task 5: Seal all H22 decisions and select/freeze the optional H23 card

**Files:**

- Create: mlb_backtest/model_improvement/lane_b_ledger.py
- Create: tests/test_model_improvement_lane_b_ledger.py

**Interfaces:**

- Produces h22_order(row), reduce_h22(rows), write_candidate_entry(path, entry), select_h23(spec, rows, lane_a_evaluation: EvaluationReport), build_lane_b_disposition(spec, lane_a_result), freeze_ab_card(spec, selected, lane_a_card, source_gate, baseline_replay, feature_seal, prediction_seal, evaluation: EvaluationReport, parameters), and a QUALIFIED LaneDisposition sealed after a successful Common card freeze.
- H24 consumes only FrozenModelCard or LaneDisposition, not any Lane B registry entry.

- [ ] **Step 1: Write failing H22/H23 TestCase methods.**

~~~python
import unittest
from mlb_backtest.model_improvement.lane_b_ledger import reduce_h22, select_h23, build_lane_b_disposition


class LaneBLedgerTest(unittest.TestCase):
    def test_h22_keeps_twelve_and_labels_invalid_replay(self):
        kept, rejected = reduce_h22(self.rows)
        self.assertEqual(12, len(kept))
        self.assertEqual("H22_INVALID_REPLAY", next(r for r in rejected if r["candidate_id"] == self.invalid_id)["reason"])

    def test_not_frozen_lane_a_is_absent_dependency(self):
        result = build_lane_b_disposition(self.spec, self.lane_a_not_frozen)
        self.assertEqual("ABSENT", result.state)
~~~

- [ ] **Step 2: Run RED.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_ledger.LaneBLedgerTest -v

Expected: ModuleNotFoundError for lane_b_ledger.

- [ ] **Step 3: Implement ledger, ordering, terminal dispositions, and frozen card.**

~~~python
from mlb_backtest.model_improvement.canonical import canonical_json_bytes, read_regular_bytes, sha256_bytes, write_immutable_json
from mlb_backtest.model_improvement.contracts import seal_frozen_model_card
from mlb_backtest.model_improvement.evaluation import EvaluationReport
from mlb_backtest.model_improvement.lane_disposition import seal_lane_disposition
from mlb_backtest.model_improvement.run_spec import run_spec_sha256


def h22_order(row):
    value = row["comparison"]
    return (value["all_games"]["candidate_brier"], value["all_games"]["candidate_log_loss"],
            -value["daily_top1"]["delta"], row["candidate_id"])


def reduce_h22(rows):
    if len(rows) != 210 or len({r["candidate_id"] for r in rows}) != 210:
        raise ValueError("H22 requires all 210 candidate IDs")
    valid = [r for r in rows if r["replay_status"] == "PASS" and r["comparison"]["all_games"]["N"] > 0 and r["comparison"]["daily_top1"]["N"] > 0]
    ids = {r["candidate_id"] for r in sorted(valid, key=h22_order)[:12]}
    kept = [{**r, "status": "KEPT_H22", "reason": "H22_ORDERED_SURVIVOR"} for r in rows if r["candidate_id"] in ids]
    rejected = [{**r, "status": "REJECTED_H22", "reason": "H22_REDUCTION_LIMIT" if r in valid else "H22_INVALID_REPLAY"} for r in rows if r["candidate_id"] not in ids]
    return sorted(kept, key=h22_order), sorted(rejected, key=lambda r: r["candidate_id"])


def write_candidate_entry(path, entry):
    write_immutable_json(path, entry)
    persisted = read_regular_bytes(path, "Lane B candidate entry")
    if persisted != canonical_json_bytes(entry):
        raise RuntimeError("candidate entry canonical reread differs")
    return sha256_bytes(persisted)


def build_lane_b_disposition(spec, lane_a_result):
    parent = lane_a_result.card
    evidence = (lane_a_result.disposition.artifact_sha256,
                lane_a_result.prediction.artifact_sha256,
                lane_a_result.evaluation.artifact_sha256)
    if parent is not None and parent.model_id == "A" and parent.state == "H23_FROZEN":
        raise ValueError("frozen Lane A result must not be absent")
    return seal_lane_disposition(spec, lane_id="B", state="ABSENT",
        parent_card_sha256=None if parent is None else parent.artifact_sha256,
        evidence_sha256=evidence, reason_codes=("LANE_A_NOT_H23_FROZEN",))


def select_h23(spec, rows, lane_a_evaluation: EvaluationReport):
    eligible = [r for r in rows if r["replay_status"] == "PASS"
        and r["comparison"]["all_games"]["candidate_brier"] <= lane_a_evaluation.brier
        and r["comparison"]["all_games"]["candidate_log_loss"] <= lane_a_evaluation.log_loss
        and r["comparison"]["daily_top1"]["delta"] > 0]
    if not eligible:
        return seal_lane_disposition(spec, lane_id="B", state="REJECTED",
            parent_card_sha256=None, evidence_sha256=(lane_a_evaluation.artifact_sha256,),
            reason_codes=("NO_H23_CANDIDATE_CLEARS_GATES",))
    return {**min(eligible, key=h22_order), "status": "SELECTED_H23"}


def freeze_ab_card(spec, selected, lane_a_card, source_gate, baseline_replay, feature_seal, prediction_seal, evaluation, parameters):
    if not isinstance(selected, dict) or selected["status"] != "SELECTED_H23":
        raise ValueError("selected H23 candidate differs")
    if lane_a_card.model_id != "A" or lane_a_card.state != "H23_FROZEN":
        raise ValueError("Lane A parent card is not H23_FROZEN")
    payload = {"model_id": "A+B", "state": "H23_FROZEN", "fold_id": "H23",
        "run_spec_sha256": run_spec_sha256(spec), "source_gate_sha256": source_gate.artifact_sha256,
        "baseline_replay_sha256": baseline_replay.artifact_sha256,
        "parent_card_sha256": lane_a_card.artifact_sha256,
        "feature_seal_sha256": feature_seal.artifact_sha256,
        "prediction_contract_sha256": prediction_seal.artifact_sha256,
        "prediction_seal_sha256": prediction_seal.artifact_sha256,
        "evaluation_sha256": evaluation.artifact_sha256,
        "parameters": {"candidate_id": selected["candidate_id"], **parameters}}
    return seal_frozen_model_card(spec, "lane_b", "H23/frozen_model_card.json", payload)
~~~

- [ ] **Step 4: Add no-qualifier, worse-than-Lane-A, card mutation, and parent-state tests then run GREEN.**

~~~python
    def test_no_qualifier_rejects_and_wrong_parent_cannot_freeze(self):
        result = select_h23(self.spec, self.worse_survivors, self.lane_a_evaluation)
        self.assertEqual("REJECTED", result.state)
        with self.assertRaisesRegex(ValueError, "not H23_FROZEN"):
            freeze_ab_card(self.spec, self.selected, self.lane_a_not_frozen,
                           self.source_gate, self.baseline_replay, self.feature_seal,
                           self.prediction_seal, self.evaluation, self.parameters)
~~~

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_ledger.LaneBLedgerTest -v

Expected: PASS; non-frozen A ends before search, rejected B writes no card, and selection order is Brier, log loss, Top-1 delta descending, candidate ID.

- [ ] **Step 5: Commit.**

~~~bash
git add mlb_backtest/model_improvement/lane_b_ledger.py tests/test_model_improvement_lane_b_ledger.py
git commit -m "feat: seal optional lane b card"
~~~

### Task 6: Explicit-path runner and replay

**Files:**

- Create: mlb_backtest/model_improvement/lane_b_runner.py
- Create: tests/test_model_improvement_lane_b_runner.py

**Interfaces:**

- Produces run_lane_b_h22(run_spec_path, source_gate_path, source_gate_expected_sha256, feature_seal_path, feature_seal_expected_sha256, v51_h22_path, v51_h22_expected_sha256, lane_a_h22_path, lane_a_h22_expected_sha256), run_lane_b_h23(spec, lane_a_result, survivors, lane_a_evaluation, source_gate, baseline_replay, feature_seal, selected_prediction_seal, selected_evaluation, parameters) -> tuple[FrozenModelCard | None, LaneDisposition], verify_lane_b_replay(spec, h22_manifest_path, ab_card_path, expected_card_artifact_sha256).
- H24 receives optional card.path/card.artifact_sha256 and disposition.path/disposition.artifact_sha256; runner does not import candidate_registry.

- [ ] **Step 1: Write failing runner TestCase methods.**

~~~python
import ast
import unittest
from mlb_backtest.model_improvement.lane_b_runner import run_lane_b_h22


class LaneBRunnerTest(unittest.TestCase):
    def test_blocked_gate_fails_before_h22(self):
        with self.assertRaisesRegex(RuntimeError, "source gate is not SOURCES_READY"):
            run_lane_b_h22(self.spec_path, self.blocked_gate_path, self.blocked_gate_artifact_sha256,
                            self.feature_seal_path, self.feature_seal_artifact_sha256,
                            self.v51_h22_path, self.v51_h22_artifact_sha256,
                            self.lane_a_h22_path, self.lane_a_h22_artifact_sha256)

    def test_registry_is_not_imported(self):
        tree = ast.parse(self.runner_source.read_text(encoding="utf-8"))
        names = {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
        self.assertNotIn("mlb_backtest.model_improvement.candidate_registry", names)
~~~

- [ ] **Step 2: Run RED.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_runner.LaneBRunnerTest -v

Expected: ModuleNotFoundError for lane_b_runner.

- [ ] **Step 3: Implement explicit preflight and terminal branch.**

~~~python
from pathlib import Path
from mlb_backtest.model_improvement.contracts import load_frozen_model_card
from mlb_backtest.model_improvement.lane_b_ledger import build_lane_b_disposition, freeze_ab_card, select_h23
from mlb_backtest.model_improvement.oof_ledger import load_prediction_rows, load_prediction_seal
from mlb_backtest.model_improvement.run_spec import load_run_spec
from mlb_backtest.model_improvement.sealed_features import load_feature_seal, load_sealed_feature_rows
from mlb_backtest.model_improvement.source_gate import load_source_gate_receipt
from mlb_backtest.model_improvement.canonical import read_regular_bytes, sha256_bytes
from mlb_backtest.model_improvement.run_spec import run_spec_sha256
from mlb_backtest.model_improvement.lane_disposition import seal_lane_disposition
def run_lane_b_h22(run_spec_path, source_gate_path, source_gate_expected_sha256, feature_seal_path,
                   feature_seal_expected_sha256, v51_h22_path, v51_h22_expected_sha256,
                   lane_a_h22_path, lane_a_h22_expected_sha256):
    spec = load_run_spec(Path(run_spec_path))
    gate = load_source_gate_receipt(Path(source_gate_path), source_gate_expected_sha256)
    features = load_feature_seal(Path(feature_seal_path), feature_seal_expected_sha256)
    feature_rows = load_sealed_feature_rows(spec, (2022,), features)
    v51 = load_prediction_seal(Path(v51_h22_path), v51_h22_expected_sha256)
    lane_a = load_prediction_seal(Path(lane_a_h22_path), lane_a_h22_expected_sha256)
    v51_rows, lane_a_rows = load_prediction_rows(spec, v51), load_prediction_rows(spec, lane_a)
    if gate.as_mapping()["status"] != "SOURCES_READY":
        raise RuntimeError("source gate is not SOURCES_READY")
    if tuple(spec.fold("H22").oof_calibration_years) != (2019, 2020, 2021):
        raise RuntimeError("H22 chronology differs")
    if {r.game_pk for r in v51_rows} != {r.game_pk for r in lane_a_rows} or any(hasattr(r, "home_win") for r in feature_rows):
        raise RuntimeError("sealed input population differs or exposes outcomes")
    return {"run_spec_sha256": run_spec_sha256(spec), "source_gate_artifact_sha256": gate.artifact_sha256,
            "feature_seal_artifact_sha256": features.artifact_sha256,
            "v51_h22_artifact_sha256": v51.artifact_sha256, "lane_a_h22_artifact_sha256": lane_a.artifact_sha256}


def run_lane_b_h23(spec, lane_a_result, survivors, lane_a_evaluation, source_gate, baseline_replay,
                   feature_seal, selected_prediction_seal, selected_evaluation, parameters):
    card = lane_a_result.card
    if card is None or card.model_id != "A" or card.state != "H23_FROZEN":
        return None, build_lane_b_disposition(spec, lane_a_result)
    selected = select_h23(spec, survivors, lane_a_evaluation)
    if not isinstance(selected, dict):
        return None, selected
    candidate_id = selected["candidate_id"]
    selected_rows = load_prediction_rows(spec, selected_prediction_seal)
    if (selected_prediction_seal.ledger_id != "H23"
            or not selected_rows
            or {row.model_id for row in selected_rows} != {candidate_id}
            or {row.target_year for row in selected_rows} != {spec.fold("H23").target_year}
            or selected_evaluation.model_id != candidate_id
            or selected_evaluation.run_spec_sha256 != run_spec_sha256(spec)
            or selected_evaluation.prediction_sha256 != selected_prediction_seal.artifact_sha256):
        raise ValueError("selected Lane B H23 prediction/evaluation binding differs")
    frozen = freeze_ab_card(spec, selected, card, source_gate, baseline_replay, feature_seal,
                            selected_prediction_seal, selected_evaluation, parameters)
    qualified = seal_lane_disposition(spec, lane_id="B", state="QUALIFIED",
        parent_card_sha256=card.artifact_sha256,
        evidence_sha256=(frozen.artifact_sha256, selected_prediction_seal.artifact_sha256,
                         selected_evaluation.artifact_sha256),
        reason_codes=("H23_GATE_PASS",))
    return frozen, qualified


def verify_lane_b_replay(spec, h22_manifest_path, ab_card_path, expected_card_artifact_sha256):
    manifest = read_regular_bytes(Path(h22_manifest_path), "H22 manifest")
    if not manifest.endswith(b"\n") or manifest.endswith(b"\n\n"):
        raise RuntimeError("manifest newline format differs")
    card = load_frozen_model_card(spec, Path(ab_card_path), expected_card_artifact_sha256)
    return {"state": "QUALIFIED", "h22_manifest_artifact_sha256": sha256_bytes(manifest),
            "ab_card_artifact_sha256": card.artifact_sha256, "parent_card_sha256": card.parent_card_sha256,
            "prediction_contract_sha256": card.prediction_contract_sha256}
~~~

- [ ] **Step 4: Add no-card, selected-candidate prediction/evaluation mismatch, duplicate-output, and double-newline replay tests then run GREEN.**

~~~python
    def test_no_card_and_double_newline_replay_are_rejected(self):
        card, disposition = self.runner.run_lane_b_h23(
            self.spec, self.lane_a_rejected_result, self.survivors,
            self.lane_a_rejected_result.evaluation, self.source_gate, self.baseline_replay,
            self.feature_seal, self.lane_b_h23_prediction, self.lane_b_h23_evaluation,
            self.parameters)
        self.assertIsNone(card)
        self.assertEqual("ABSENT", disposition.state)
        self.manifest_path.write_bytes(b"{}\n\n")
        with self.assertRaisesRegex(RuntimeError, "newline format"):
            self.runner.verify_lane_b_replay(self.spec, self.manifest_path, self.card_path,
                                              self.card_artifact_sha256)

    def test_selected_candidate_requires_its_h23_prediction_and_evaluation(self):
        with self.assertRaisesRegex(ValueError, "selected Lane B H23"):
            self.runner.run_lane_b_h23(
                self.spec, self.lane_a_frozen_result, self.survivors,
                self.lane_a_frozen_result.evaluation, self.source_gate,
                self.baseline_replay, self.feature_seal,
                self.wrong_candidate_prediction, self.lane_b_h23_evaluation,
                self.parameters)
~~~

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_runner.LaneBRunnerTest -v

Expected: PASS; no H24 registry import or write occurs.

- [ ] **Step 5: Run all checks and commit.**

Run: PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_model_improvement_lane_b_candidates tests.test_model_improvement_lane_b_scoring tests.test_model_improvement_lane_b_calibration tests.test_model_improvement_lane_b_evaluation tests.test_model_improvement_lane_b_ledger tests.test_model_improvement_lane_b_runner tests.test_scoring tests.test_normalize tests.test_metrics tests.test_ranking tests.test_leakage -v

Expected: PASS; every test is a unittest.TestCase method and v5.1 remains unchanged.

~~~bash
ruff check mlb_backtest/model_improvement tests/test_model_improvement_lane_b_*.py
git diff --check
git add mlb_backtest/model_improvement/lane_b_runner.py tests/test_model_improvement_lane_b_runner.py
git commit -m "feat: orchestrate lane b replay"
~~~

## Final acceptance gates

- Exactly 70 legal weight tuples times three multipliers make 210.
- H22 emits 210 immutable decisions; first 12 replay-valid candidates are ordered Brier asc, log loss asc, date-paired Top-1 delta desc, candidate ID asc. Invalid replay reason is H22_INVALID_REPLAY.
- H23 requires Brier/log loss no worse than Lane A and positive paired Top-1 delta versus v5.1, then uses the same ordering.
- Lane A not H23_FROZEN yields LaneDisposition `state="ABSENT"` with reason `LANE_A_NOT_H23_FROZEN` and does not search. No H23 qualifier yields LaneDisposition REJECTED with reason NO_H23_CANDIDATE_CLEARS_GATES and writes no A+B card.
- Successful A+B has model_id A+B, state H23_FROZEN, Lane A parent-card SHA-256, and prediction-contract SHA-256. H24 registers it later.
- Candidate calibration is O19–O21, O19–O22, O19–O23 for H22, H23, optional H24; exact p=.5 remains no-pick.
