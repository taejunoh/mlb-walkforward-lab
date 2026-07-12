# Preparation Mini E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove a production-shaped Statcast CSV stored in `RawStore` can feed raw feature preparation and the complete calibrate, candidate-component, score, and lock chain.

**Architecture:** Add one fixture-only replay test that uses production loaders/builders and existing immutable mini-replay schedule manifests. A narrow test helper fills deliberately sparse fixture metrics only after the real raw builder runs; production completeness rules remain unchanged.

**Tech Stack:** Python `unittest`, `RawStore`, Statcast parser, feature engine, scoring preparation, pipeline stages.

## Global Constraints

- Do not relax production schedule, Statcast, normalization, provenance, or lock validation.
- Do not modify files owned by the concurrent Statcast-outs task.

---

### Task 1: Source-to-lock fixture replay

**Files:**
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `RawStore.put`, `load_statcast_range`, `FeatureHistory`, `build_raw_game_features`, `compute_normalization`, `build_training_edges`, `build_candidate_components`, `calibrate_stage`, `features_stage`, `lock_stage`.
- Produces: `PipelineTest.test_production_shaped_source_preparation_reaches_lock`.

- [x] **Step 1: Write the failing test**

Add a replay that seeds verified schedule and Statcast manifests, builds one real raw row, materializes a small deterministic normalization sample under an explicit fixture-only helper, and invokes every downstream production builder/stage.

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_pipeline.PipelineTest.test_production_shaped_source_preparation_reaches_lock`

Expected: FAIL until the fixture helper and complete provenance artifacts are present.

- [x] **Step 3: Complete the fixture-only assembly**

Write canonical training raw, edges, frozen inputs, candidate raw/components/provenance, then call calibration, scoring, and lock stages without patching production validators.

- [x] **Step 4: Run regression checks**

Run: `python3 -m unittest tests.test_pipeline` and `ruff check tests/test_pipeline.py`.

Expected: PASS.

- [x] **Step 5: Commit**

Run: `git add docs/superpowers/plans/2026-07-12-preparation-mini-e2e.md tests/test_pipeline.py && git commit -m 'test preparation sources through prediction lock'`.
