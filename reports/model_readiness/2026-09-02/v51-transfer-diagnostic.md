# v5.1 Retrospective Transfer Diagnostic

This evaluation reads sealed v5.1 2025 rows; it does not fit calibration, set a threshold, or make a promotion claim.

## Scope and orientation

`pick_probability` is the probability of `predicted_side`/`predicted_winner` (home: `p_home`; away: `1-p_home`). `correct` is whether that predicted winner equals the final actual winner.
The frozen card values read and verified here are alpha=0.1030746732466535, beta=0.6128874176132043; no fit is performed.
The window is 2025-05-01 through 2025-08-12 over 100 selection dates; August is partial (12 dates), not a full month.

## Population summaries

| scope | N | W-L | accuracy | Wilson 95% | mean p | Brier | log loss |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| all_final_locked_rows | 1340 | 718-622 | 0.535821 | 0.509054–0.562383 | 0.565661 | 0.246271 | 0.685553 |
| top1_final_rows | 100 | 67-33 | 0.670000 | 0.573052–0.754370 | 0.656114 | 0.221372 | 0.635116 |

## Fixed 0.1 probability bins — all_final_locked_rows

| bin | N | mean p | W | observed win rate | Wilson 95% | abs gap |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| [0.0, 0.1) | 0 | — | 0 | — | — | — |
| [0.1, 0.2) | 0 | — | 0 | — | — | — |
| [0.2, 0.3) | 0 | — | 0 | — | — | — |
| [0.3, 0.4) | 0 | — | 0 | — | — | — |
| [0.4, 0.5) | 0 | — | 0 | — | — | — |
| [0.5, 0.6) | 1032 | 0.544949 | 518 | 0.501938 | 0.471482–0.532380 | 0.043011 |
| [0.6, 0.7) | 298 | 0.632593 | 194 | 0.651007 | 0.595277–0.702893 | 0.018413 |
| [0.7, 0.8) | 10 | 0.708534 | 6 | 0.600000 | 0.312670–0.831822 | 0.108534 |
| [0.8, 0.9) | 0 | — | 0 | — | — | — |
| [0.9, 1.0] | 0 | — | 0 | — | — | — |

## Fixed 0.1 probability bins — top1_final_rows

| bin | N | mean p | W | observed win rate | Wilson 95% | abs gap |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| [0.0, 0.1) | 0 | — | 0 | — | — | — |
| [0.1, 0.2) | 0 | — | 0 | — | — | — |
| [0.2, 0.3) | 0 | — | 0 | — | — | — |
| [0.3, 0.4) | 0 | — | 0 | — | — | — |
| [0.4, 0.5) | 0 | — | 0 | — | — | — |
| [0.5, 0.6) | 3 | 0.587898 | 1 | 0.333333 | 0.061490–0.792345 | 0.254565 |
| [0.6, 0.7) | 87 | 0.652441 | 60 | 0.689655 | 0.586160–0.777110 | 0.037215 |
| [0.7, 0.8) | 10 | 0.708534 | 6 | 0.600000 | 0.312670–0.831822 | 0.108534 |
| [0.8, 0.9) | 0 | — | 0 | — | — | — |
| [0.9, 1.0] | 0 | — | 0 | — | — | — |

## Monthly — all_final_locked_rows

| month | N | W-L | accuracy | mean p | Brier | log loss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025-05 | 411 | 229-182 | 0.557178 | 0.568189 | 0.241837 | 0.676436 |
| 2025-06 | 397 | 216-181 | 0.544081 | 0.566938 | 0.246261 | 0.685526 |
| 2025-07 | 369 | 191-178 | 0.517615 | 0.561149 | 0.250302 | 0.693913 |
| 2025-08 | 163 | 82-81 | 0.503067 | 0.566389 | 0.248348 | 0.689680 |

## Monthly — top1_final_rows

| month | N | W-L | accuracy | mean p | Brier | log loss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2025-05 | 31 | 21-10 | 0.677419 | 0.661147 | 0.215680 | 0.622440 |
| 2025-06 | 30 | 22-8 | 0.733333 | 0.657299 | 0.195623 | 0.580876 |
| 2025-07 | 27 | 14-13 | 0.518519 | 0.650065 | 0.274291 | 0.746044 |
| 2025-08 | 12 | 10-2 | 0.833333 | 0.653758 | 0.181379 | 0.553879 |

## Integrity and caveats

- Locked/evaluated key equality and 1,340-row count: `True`.
- Existing Top1 primary-metric cross-check: `True`.
- Fixed-bin counts sum to their scopes: `True`; arithmetic sanity checks: `True`.
- Retrospective only: preserved source evidence uses reconstructed final lineups/actual starters, later provider revisions, and lacks an outcome-join execution timestamp. It is not a live T-45 validation.
- Wilson intervals assume independent Bernoulli outcomes and ignore same-date/team dependence; they are descriptive uncertainty, not a performance guarantee.
- The all-row result is a locked-slate diagnostic; the Top1 result is selection-conditioned. Neither is market comparison, profitability evidence, a new 60–70% threshold, or a license to change the frozen card.

## Reproduce

```sh
PYTHONDONTWRITEBYTECODE=1 python3 scripts/analysis/v51_transfer_diagnostic.py --source-root '/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/mlb-v5-1-backtest' --json-out reports/model_readiness/2026-09-02/v51-transfer-diagnostic.json --md-out reports/model_readiness/2026-09-02/v51-transfer-diagnostic.md
```

## Source receipts

- `audit/v5_1_bt/model_card.json`: `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`
- `data/v5_1_bt/calibration/training_edges.jsonl`: `66cfe5db414c76c625de316556236a172ce0e5db71e28fefc390576438e41318`
- `data/v5_1_bt/locked/predictions.jsonl`: `bbe584cf0fe3d193b4ad13ee58bdf1a6f9026de7c1ad221ba09d06ff48069dcb`
- `data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl`: `db2b6276396a9d5f24a512bbf684ab6bafa0285e34161a434deaf5054bf2ec76`
