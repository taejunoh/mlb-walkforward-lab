# Temporal calibration candidate implementation plan

> **For agentic workers:** Use subagent-driven development and test-driven development. Preserve the existing worktree and all prior artifacts; no commits, network, integration or live-card mutation in this task.

**Goal:** Produce and assess one separate retrospective probability-calibration candidate with normalization and calibration learned on disjoint chronological periods.

**Architecture:** A small two-phase analysis runner reuses the existing scoring primitives. `prepare` fits 2022-only normalization and 2023-only calibration, then seals predictions for available 2025 raw features. `evaluate` checks those seals before joining previously seen 2025 results and comparing with the fixed 1,340-row legacy population. This is not the v5.2 Lane A/H22/H23 protocol and cannot satisfy its H24 promotion gate.

**Tech Stack:** Existing Python 3.11 scoring/normalization code, unittest, JSON/JSONL, SHA-256.

## Approved design and boundaries

The user approved the preceding readiness-repair handoff's time-separated calibration step. Keep the 2024 H24 holdout closed and exclude all 2024 rows from fitting and evaluation. Do not use 2025 for normalization, calibration fitting, threshold/weight/feature choice or candidate selection. Because 2025 results have already been inspected, label all comparison output retrospective and not independent validation. No new betting threshold, Top1 ranking policy, sportsbook collection, O/U model or live promotion is included.

The new 2022-only normalization differs from the old multi-year normalization. The comparison therefore measures the combined normalization-and-calibration candidate, not the isolated causal effect of changing alpha/beta. No additional candidate search or factorial experiment is included.

Use the existing isolated `live-venue-timezone-fix` LFG worktree. The read-only source authority is its sibling `mlb-v5-1-backtest`. Never access Documents, custody/production, privileged runtime or `/tmp/r4-readonly-tree-manifest.txt`. Existing dirty and untracked artifacts belong to the user and remain intact.

## Task 1 — Candidate runner and behavioral tests

**Create:** `scripts/analysis/v51_temporal_calibration_candidate.py`

**Create:** `tests/test_v51_temporal_calibration_candidate.py`

- [x] Add eight focused synthetic tests covering chronological partitions, same-normalization scoring, deterministic bytes, seal mismatch, exact comparison keys/bins, prepare with outcome files absent, and runner/dependency mutation rejection before outcome access. Existing scoring/normalization tests additionally cover their primitives; this is not a claim of exhaustive input fuzzing.
- [x] Implement `prepare` using existing `compute_normalization(raw_2022)`, `build_training_edges(raw_2023, n22, labels_2023)`, `fit_frozen_calibration(..., training_start="2023-01-01", training_end="2023-12-31")`, `score_raw_game(raw_2025, n22)`, `home_probability` and `predicted_pick`. The labels-only training-edge loader filters the recorded date before decoding excluded 2024 records; never reuse old normalized edge values as new edges.
- [x] Authenticate the pinned audit card and referenced pipeline card, frozen inputs, mixed-year training raw/edges and 2025 raw/provenance. Whole-file hashes may cover the existing mixed training files, but only permitted partitions enter calculation. Capture hashes of the local scoring implementation used by the runner. Do not mutate inputs.
- [x] Write append-only candidate receipt and per-game predictions to a new dated namespace. Receipt stores normalization, calibration, split dates/counts, source/code bindings, prediction SHA and explicit `RETROSPECTIVE_CANDIDATE_ONLY_NOT_FOR_LIVE_USE`. Existing output paths are not overwritten.
- [x] Implement `evaluate`: validate candidate receipt/prediction seals, then read the fixed locked/evaluated 2025 artifacts; require exact locked/evaluated keys and candidate coverage. Compare all fixed 1,340 games, not candidate-selected Top1. Keep scoring and fitting independent from outcome evaluation.
- [x] Run focused tests and lint. No full long-running suite.

## Task 2 — Fixed retrospective comparison

**Create:** `reports/model_readiness/2026-09-02/temporal_calibration/` artifacts only.

- [x] Run corrected `prepare` once after reviews/tests, preserving and checking receipt and prediction sidecars before `evaluate`. Earlier executions are explicitly excluded from temporal claims; see corrective record below.
- [x] Run corrected `evaluate` once with no fitting/tuning. Record actual execution timestamps, population/exclusions and source digests. This technical replay does not create an unseen evaluation period.
- [x] Calculate both models' Brier and log loss on the same games, accuracy, mean picked-side probability, fixed 0.1-wide reliability bins and monthly metrics. Include descriptive Wilson intervals with dependence caveats. Verify legacy Brier `0.24627075738049958` and log loss `0.6855527297095908`.
- [x] Do not choose a winner for deployment based on this reused period. Report deterioration as plainly as improvement. Stored 2025 data uses reconstructed final lineups/actual starters and is not a live T-45 replay; there are no matched market-return observations in this experiment.

## Task 3 — Independent review and handoff

- [x] Specification reviewer checks the code and actual artifacts against this exact scope.
- [x] After specification passes, an independent quality reviewer checks leakage boundaries, arithmetic, failure behavior and maintainability; fix concrete issues and recheck.
- [x] Controller runs fresh focused tests and verifies outcome arithmetic and immutable source/card state.
- [x] Prepare concise Korean handoff with inspectable artifacts, separating completed candidate work from remaining untouched evaluation and live admission. Preserve branch/worktree without commit, push, merge or PR.

## Acceptance conditions

The new candidate demonstrably uses only 2022 normalization and 2023 calibration labels; 2024 is never used; 2025 has no influence on fit or policy. Source-bound, finite predictions are sealed before the evaluation command opens outcome bytes. Baseline comparison uses identical keys and independently reproduces legacy metrics. Tests and independent reviews pass, and no existing production card or report is changed. This is a completed experiment, not a claim of profitable or calibrated-live predictions.

## Execution record and corrective review

The first implementation produced `prepare/evaluate` and post-outcome metadata-equivalent `prepare-final/evaluate-final` pairs. Independent arithmetic confirmed identical prediction bytes and parameters and the same 1,340-game comparison. The first evaluator's incorrect equality check between 1,972 candidate keys and 1,340 locked keys was corrected to require candidate coverage while retaining exact locked/evaluated equality. No hyperparameter, feature, threshold or candidate selection changed.

Specification review then found that prepare's combined source-pin loop hashed the 2025 evaluated file before prediction sealing. Both pairs are therefore permanently `LEGACY_RETROSPECTIVE_ONLY_PRESEAL_OUTCOME_READ`, not chronology-valid evidence. Their receipts remain unmodified, including the historical inaccurate exclusion assertion; `RESULTS.md` and `artifact_manifest.json` explicitly withdraw that assertion. Their execution timestamps were not recorded and will not be backfilled.

The repaired runner separates prepare/evaluation pins, succeeds with both evaluation input files absent, and validates receipt/prediction hashes plus runner and direct scoring-dependency hashes before opening outcomes. Future artifacts record actual UTC timestamps. Eight focused synthetic tests pass, including the reproduced absent-outcomes failure and runner/dependency mutation rejection. Specification re-review passed.

After independent code-quality review, the controller may run one fresh `prepare-corrected/evaluate-corrected` technical replay to validate the repaired execution path, without tuning or altering the candidate. This explicitly supersedes the initial single-execution plan only for bug verification; no previous bytes are overwritten and the replay is not a new unseen evaluation period. Its predictions and parameters must match the numeric-reference pair. All older pairs remain permanently legacy-only regardless of replay success.

## Final verification

Both independent reviews passed. The controller executed the single corrected prepare/evaluate pair, checking the preparation sidecars before starting evaluation. Preparation records `2026-09-02T20:42:54Z`; its receipt SHA is `9bc723332b80f36d8786f5258a51ebd247cf7b05879ca2d68a28e02c32ff5805`. Its prediction SHA and alpha/beta equal the legacy numeric reference exactly. See the artifact manifest for the evaluation timestamp and digest.

After that replay, 59 affected tests passed, Ruff passed, and `git diff --check` passed. Independent arithmetic reproduced the baseline/candidate metrics within floating-point tolerance. Existing model cards and user changes were preserved. No live candidate promotion was performed.

One low-priority quality-review issue remains outside this pinned-input experiment: `actual_winner` should eventually reject values other than the home/away team rather than treating an unrecognized value as an away result. The approved source digest fixes the current input; broader-input reuse must add this validation. No further fits, retries, or tuning are part of this completed experiment.
