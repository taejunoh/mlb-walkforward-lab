# Model betting readiness repair implementation plan

> **For agentic workers:** Use subagent-driven development for the focused code repair. Preserve existing reports and model cards.

**Goal:** Correct the ML market adapter's completeness calculation, begin honest timestamped market observation, and identify the first executable historical calibration prerequisite.

**Architecture:** Reuse the existing scorer's six-component weighted completeness definition. Keep historical model outputs immutable. Collect market observations separately from predictions; research readiness is not real-money admission. Reuse the existing calibration plan rather than inventing another evaluation population.

**Tech stack:** Python 3.11, unittest, existing scoring and market-admission modules.

**Specification:** User-approved explanation in this task; `docs/superpowers/specs/2026-09-01-variable-multibook-market-plays-design.md`; `docs/superpowers/specs/2026-08-31-parallel-model-result-improvement-design.md`.

## Constraints

- Work in the existing LFG worktree; preserve all pre-existing untracked artifacts.
- No sudo, production/custody access, Documents access, destructive Git, push, merge, or PR.
- Do not read or delete `/tmp/r4-readonly-tree-manifest.txt`.
- Preserve the frozen v5.1 model, component weights, thresholds, and all existing evidence.
- Network observation is authorized; do not manufacture provider event IDs, source timestamps, sportsbook availability, historical prices, or calibration evidence.
- Do not open H24 outcomes or promote a live model in this repair.

## Task 1: Match ML adapter completeness to scoring

**Own:** `mlb_backtest/market_play_authorities.py`, `tests/test_market_play_authorities.py`.

- [x] Add a regression fixture with six component qualities, including zero defense/environment, whose weighted completeness is positive. Cover a ranked game and a trace-only `SCORED_NOT_TOP3` game.
- [x] Run the focused test and verify failure against the current minimum-quality fallback.
- [x] Replace that fallback with `scoring.overall_completeness(qualities)`, using the existing numeric conversion at the authority boundary. Keep the six-key finite/range validation and do not trust an optional aggregate supplied inside the component map.
- [x] Verify literal hand-calculated completeness, equality with primary ranked `data_completeness`, continued rejection below 0.50, and rejection of malformed qualities. Run only affected authority/evaluator/report tests.
- [x] Complete independent specification review, then code-quality review; fix concrete findings and run final checks.

## Task 2: Record real current market observations

**Own:** a new dated namespace under `audit/market_observations/` and collector inputs admitted by the existing market capture module when the source meets its schema.

- [x] Inspect existing capture requirements and collection capabilities.
- [x] Observe each of FanDuel, DraftKings, BetMGM, Caesars, and Fanatics through its public source; record actual collection time, URL, response outcome and source limitations. Record unavailable when a required quote cannot be obtained.
- [x] Preserve comparison-site observations as secondary evidence only. Never relabel them as direct sportsbook captures. No comparison-site data was used for admission.
- [x] Admit only schema-valid direct captures/unavailable records using existing commands. Report missing prerequisites instead of claiming a complete market-backed decision.

## Task 3: Resolve calibration input readiness

**Own:** read-only inventory of existing LFG project data and a short results note in this plan's companion report.

- [x] Inspect the existing 2018–2024 calibration source contracts and locate preserved v5.1 outputs using narrow project paths and manifests, avoiding protected locations. This is a bounded inventory, not a claim that every historical raw source is available.
- [x] Determine which required inputs are present, missing, or unverified and name the first blocked/runnable stage of the existing staged calibration plan.
- [ ] Full staged-source preflight not run: the bounded diagnostic below instead uses already hash-pinned sibling artifacts. No multi-year fetch, full suite, H24 access, or source restoration was performed.
- [x] Report completed work separately from missing model fitting/evaluation/live promotion.

## Completion evidence

Focused regression tests must show corrected completeness without changing model probabilities or admission thresholds. Market evidence must retain provenance and actual timing. Historical readiness must cite concrete artifacts or missing prerequisites. Existing reports must remain unchanged.

## Bounded diagnostic extension (2026-09-02)

The exact v5.1 training edges, locked predictions and evaluated outcomes were located in the existing `mlb-v5-1-backtest` sibling worktree and their pinned SHA-256 digests matched. Missing data in this execution worktree does not mean missing data throughout LFG.

Use those sibling bytes read-only for a frozen-card retrospective transfer diagnostic, without restoring them over any existing source or fitting a new card. Separate all 1,340 locked games from the original daily Top1 selection. Before calculation, fix metrics to accuracy, mean predicted probability, Brier score, log loss, monthly breakdown and 0.1-wide reliability bins with Wilson intervals. Reconcile the existing Top1 diagnostic. Preserve source digests and a rerunnable script under `scripts/analysis/`; write only new results under `reports/model_readiness/2026-09-02/`.

This diagnoses historical probability transfer, not real-time T-45 reproducibility, OOF calibration or betting profitability. No 2025 outcome may be used for fitting or selection, and no H24 model-improvement outcome or live-card promotion is included.

## Verified handoff

- Completeness repair passed RED/GREEN, independent specification and code-quality reviews. Final focused suite: 32 tests passed across authority, ML/totals evaluation, scoring, capture and daily market reporting. Ruff and `git diff --check` passed. Existing model probabilities, weights and the 0.50 completeness threshold were not changed.
- Frozen retrospective diagnostic is in `reports/model_readiness/2026-09-02/v51-transfer-diagnostic.{json,md}` with the rerunnable script in `scripts/analysis/v51_transfer_diagnostic.py`. All 1,340 locked rows: 718 wins, mean prediction 0.565661, observed accuracy 0.535821. Legacy Top1 was separately reconciled (67/100); it is not the full-slate result or a new selection policy.
- Direct browser observation at `2026-09-02T19:36:21.210Z` captured 11 FanDuel pregame events. DraftKings required login; Fanatics transport was blocked; Caesars lacked captured event IDs; BetMGM redirected outside the existing admitted host list. These are four unavailable outcomes, not extra usable books.
- The documented totals example had incorrect units. It now uses 8.5 runs = 17 half-run ticks. An initial incorrectly normalized capture is preserved but explicitly excluded by `audit/market_observations/2026-09-02/generated/market-artifact-supersession.json`. Only that manifest's active five-outcome set is eligible for downstream use; do not glob every historical outcome as active. Corrected raw capture: `2ae20dcf0b037ad06fdad0237ec909badd72d71c3fab0053b27010e3f189510e`; corrected FanDuel outcome: `e3d93d63c5df7f8e5731eada1e2d66804987204090e38d4c82c8d74796d25f78`.
- No full multibook PLAY board was generated: only one book is available, and an admitted schedule snapshot plus a current totals-model trace are still missing. Stored quotes do not yet constitute matched, settled market-performance observations.

## Next model-facing step (not executed here)

The sibling authority also contains pinned pre-normalization features for 2022–2024 (7,290 rows, 7,265 eligible), so a bounded retrospective calibration candidate need not start with full historical reacquisition. Fit normalization using 2022 only, create 2023 edges using that frozen normalization, and fit calibration on those later 2023 edges. Keep that same normalization and calibration fixed for a separately authorized future-period evaluation. Do not refit normalization on 2022–2023 while retaining the old calibration, since that changes the edge scale. No new fit, H24 evaluation or live promotion occurred in this handoff.
