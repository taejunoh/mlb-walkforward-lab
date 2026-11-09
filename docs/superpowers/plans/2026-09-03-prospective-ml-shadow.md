# Prospective paired ML shadow implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Follow TDD and keep all changes uncommitted.

**Goal:** Save a research-only frozen candidate prediction beside an existing baseline pregame claim and compare both against the same official outcome.

**Architecture:** Two new modules reuse existing sealed raw traces, secure observation persistence and official settlement validation. No active-model or historical experiment mutation.

**Tech Stack:** Existing Python 3.11, unittest, canonical JSON/SHA-256, Markdown.

**Spec:** docs/superpowers/specs/2026-09-03-prospective-ml-shadow-design.md

## Global Constraints

SHADOW_ONLY and NO_PROMOTION in attachments/reports. Candidate changes both normalization and calibration. No historical prediction/outcome reads for scoring. Actual pre-start persistence, exact raw-input identity, explicit paths, append-only/idempotent first attachment. Same official result and identical FINAL denominator for both models. Null empty metrics; pending and excluded visible. No model/card/market change, no source artifact overwrite, no network/install/commit/push/merge/PR/automation, no Documents/custody/production/forbidden manifest.

## Task 1: Frozen same-input shadow attachment

**Files:** Create mlb_backtest/prospective_ml_shadow.py and tests/test_prospective_ml_shadow.py.

Existing integration APIs: prospective_ml_observation.validate_primary_claim,
load_ml_snapshot; trace validation in live_score_trace; scoring_prep.score_raw_game;
scoring.home_probability and predicted_pick; secure_fs append-only helpers.
Inspect exact current signatures rather than assuming positional arguments.

Public interface:

```python
def load_frozen_shadow_profile(root): ...
def build_shadow_attachment(root, baseline_primary_claim, *, observed_clock): ...
def persist_shadow_attachment(root, built, *, primary_clock): ...
def load_shadow_attachment(root, attachment): ...
```

- [x] Write focused failing tests with real temporary baseline trace/claim fixtures.
  Include exact raw identity, candidate normalization affecting the edge,
  strict pre-start creation and commit, repeated call idempotence, mutated source,
  incorrect baseline probability/identity, nonfinite values and missing sidecar.
  Representative observable assertions:

```python
self.assertNotEqual(float(pair["candidate"]["p_home"]), float(pair["baseline"]["p_home"]))
self.assertEqual(pair["feature_sha256"], expected_raw_digest)
self.assertEqual(pair["promotion"], "NO_PROMOTION")
with self.assertRaises(ValueError):
    persist_shadow_attachment(root, built, primary_clock=lambda: scheduled_start)
```

- [x] Run RED: PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_prospective_ml_shadow -v.
- [x] Implement frozen loader pinned to corrected prepare_receipt.json and .sha256,
  expected SHA 9bc723332b80f36d8786f5258a51ebd247cf7b05879ca2d68a28e02c32ff5805.
  Validate declared year/labels/current dependency hashes. Profile ID is not a model-card ID.
  Use:

```python
scored = score_raw_game(raw_features, profile.normalization)
# Obtain edge from scored using its existing documented shape.
p_home = home_probability(edge, alpha=profile.alpha, beta=profile.beta)
```

- [x] Authenticate baseline claim/snapshot/trace/raw row and all source timing.
  Bind claim/snapshot/trace/feature/profile/code digests, game/date/teams/start,
  baseline and candidate probability/side, observed and durable UTC timestamps.
  Revalidate before append-only commit in audit/prospective_ml_shadow/.
  First attachment fixed per game/baseline-model/profile; exact repeats reuse it.
  No arbitrary clock CLI. CLI snapshot accepts --root and --baseline-primary-claim,
  prints durable attachment path, failures nonzero.
- [x] Run GREEN plus existing observer/integration regression modules.
- [x] Self-review; record full tests and changed files in task report; no commit.

## Task 2: Paired report, usage and fixture end-to-end check

**Files:** Create mlb_backtest/prospective_ml_shadow_report.py, tests/test_prospective_ml_shadow_report.py, docs/prospective_ml_shadow.md.

Consumes Task 1 load_shadow_attachment. Reuse existing authenticated settlement
head-chain resolution and calibration_metrics; no alternate outcome parser.
Keep official result collection as the existing explicit baseline workflow.

Public interface:

```python
def build_shadow_paired_report(root, attachments, settlement_heads): ...
def persist_shadow_paired_report(root, built): ...
def render_shadow_paired_markdown(report): ...
```

- [x] Write RED tests using actual Task 1 attachment plus existing official fixture
  settlement. Required cases: FINAL paired arithmetic, missing head pending,
  valid non-FINAL excluded, unrelated/tampered/conflicting heads reject,
  duplicate game/attachment reject, empty metrics null and model/profile isolation.
  Hand-derived example: home probabilities .7/.4 and outcomes True/False imply
  baseline Brier .125, accuracy 1, log loss .433750283852. Candidate .6/.6 implies
  Brier .26, accuracy .5, log loss .713558177820.
- [x] Run RED: PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_prospective_ml_shadow_report -v.
- [x] Build explicit-path paired cohort report. Bind attachment/head digests,
  reuse one baseline official result for both models, keep pending/excluded
  counts/reasons, include completeness strata and fixed reliability bins with
  mean predicted probability. Compute candidate-minus-baseline deltas; state lower
  Brier/log loss is better but not evidence of profit or validated promotion.
- [x] Persist append-only JSON/sidecar and readable Markdown under reports/prospective_ml_shadow/.
  Rebind all inputs on persistence. CLI report accepts --root,
  repeated --attachment and --settlement-head; no latest/glob discovery.
- [x] Document exact snapshot/report commands, genuine baseline collection/settlement
  reuse, research-only status, no after-start backfill and how to interpret counts/metrics.
- [x] Run GREEN + both new modules and affected existing observer/settlement/report
  integration tests; lint only changed code; self-review/report, no commit.

## Verification and handoff

- [x] Independent scoped spec/code review per task and final cross-module review.
- [x] Controller fresh targeted tests, diff check and unchanged baseline model /
  corrected receipt SHA checks.
- [x] Report implementation result separately from actual prospective sample count.
  No new real claim is created from a stale pregame report just to demonstrate success.
