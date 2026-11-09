# Model Readiness Review — 2026-09-05

## Current conclusion

Operational errors, genuinely small samples, unimplemented inputs, and insufficient independent validation data are distinct problems. Passing code tests does not mean predictive performance has passed validation. This review did not change the existing model card or historical artifacts.

## Work reviewed and blockers

| Area | Confirmed cause | Next work |
|---|---|---|
| Pregame status | ATL/PHI and DET/CLE were Warmup/PW, but the allowlist contains only Scheduled/Pre-Game | Fix the Warmup regression while retaining the existing restriction that games must not have started |
| Schedule feature | Parser discards official doubleHeader=N as None, treating an explicitly ordinary game as unknown | Add transmission/feature regression coverage distinguishing explicit N from truly unknown |
| Totals | Date limited to September 1 (month 9), with evaluation period/receipt/schema also fixed | Decide the meaning of date-specific inference and evaluation period together before changing it |
| Environment/defense | Intentional 50-point/quality-0 fallback; no local input adapter | Do not raise quality without a source/adapter that can reproduce what was knowable historically |
| ARI/HOU starter | HOU814490 career BF58, expected-innings sample 3, etc. are genuinely small samples | Do not turn missing data into a collection error or high confidence |
| Time-split calibration | 2022 normalization/2023 calibration/already-seen 2025 evaluation completed | Do not repeat the same 2025 experiment; freeze the approach and make pregame records for new games |
| v5.2 OOF | No declared certified `source_inventory` or pre-registered `RunSpec` feature producer | Check whether existing LFG data can be reused/certified, then design any required acquisition/production stage |

## Why not rerun the existing experiment

On the existing fixed 1,340-game sample, the baseline was 718 wins/622 losses (53.5821%), while the candidate was 717 wins/623 losses (53.5075%). Brier changed only from 0.2462707574 to 0.2461723072 and log loss from 0.6855527297 to 0.6853621820. The 2025 results are already-seen data, so these figures are retrospective comparisons, not independent validation or proof of profitability. Sources: `reports/model_readiness/2026-09-02/temporal_calibration/RESULTS.md` and `evaluate-corrected/evaluation_report.json`.

The existing v5.1 sibling has 2,430 rows each of 2022/2023/2024 raw features, but this is not equivalent to a v5.2 certified source inventory. This means there is no superseding `source_inventory.json` and sidecar at the declared paths checked; it does not mean historical data does not exist anywhere in LFG. 2024 H24 was not opened.

## Validation in this session

- Temporal calibration candidate + prospective shadow/report: 33 tests, OK.
- Live contracts/service + daily totals: 89 tests, OK.
- Probability calibration + OOF ledger + source gate: 48 tests, OK.
- Total: 170 existing regression/contract tests. These should not be interpreted as RED/GREEN results for a new change or as model-performance validation.
- Current active-card SHA-256: `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`.
- Disk check showed approximately 24 GiB available. Full 2018–2024 reacquisition was not started.

## Boundary between execution and design

The already approved prospective shadow CLI completed a fixed comparison record for today's 9 games before the games were played. `primary_count=9`, `pending_count=9`, `paired_count=0`, `excluded_count=0`; `SHADOW_ONLY/NO_PROMOTION`. The actual SHA-256 for `reports/prospective_ml_shadow/5f8683602d6e40867b6639606f889d68de1989e9b81a51f2cb7ada00b3c2adf7.json` matches its sidecar. Results were not opened or settled.

The new operations/totals contract is separated as a Proposed design in `docs/superpowers/specs/2026-09-05-daily-evaluation-readiness-design.md`. No production code, schema, or tests should change before that design is approved. Historical records, the frozen card, and user dirty changes were preserved. Immediately before completion, the existing tracked diff and active-card SHA were confirmed identical to their starting state. `git diff --check` passed.
