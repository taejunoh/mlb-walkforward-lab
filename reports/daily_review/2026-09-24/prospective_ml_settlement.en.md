# 2026-09-24 Settlement of Saved Pregame ML Picks

The 4 saved primary claims were settled against official MLB Stats API final results. Selected-winner predictions went 1–3 (25% accuracy). The combined Brier score was 0.312991 and log loss was 0.828898. The sample consists only of the 4 saved primary claims for this date and does not establish model quality or improvement.

| Game (gamePk) | Pregame selected winner (selected probability) | Final score | Result | Input completeness |
| --- | --- | --- | --- | ---: |
| Brewers @ Phillies (823411) | Phillies (home-win probability 0.5165) | 5–1 | Miss | 0.601 |
| Guardians @ Red Sox (824707) | Red Sox (home-win probability 0.5434) | 1–0 | Miss | 0.440 |
| Reds @ Braves (824866) | Braves (home-win probability 0.7155) | 7–6 | Miss | 0.613 |
| Rays @ Yankees (823493) | Yankees (home-win probability 0.5780) | 4–6 | Hit | 0.591 |

Score order is away team–home team. The primary claims for the first three games were saved at 2026-09-24 20:28:51 UTC; the Yankees claim was saved at 20:39:04 UTC. These are claim-save timestamps, not model-trace creation timestamps. The corresponding prediction snapshots refer to `reports/experimental_daily/2026-09-24/20260924T201347463923Z-a4363ad41e70f92a.trace.json` for the earlier games and `reports/experimental_daily/2026-09-24/20260924T203303766913Z-095c7301fcd8a3f4.trace.json` for the additional Yankees claim. Subsequent official results were observed in Stats API `schedule` responses between 2026-09-29 20:33:32 and 20:33:47 UTC and then saved and settled. Per-game request URLs, result receipts, saved snapshots, settlement-head paths, and original-report references are recorded in the accompanying JSON.

## Input and settlement completeness

All 4 games are settled primary claims. Home-win probabilities and pregame selected sides are taken unchanged from the sealed snapshots; this settlement did not regenerate predictions or refit the model. Completeness values ranged from 0.440 to 0.613. These values alone cannot identify which missing input caused an outcome, so no game-specific causal explanations were added.

Betting P&L was not calculated because there is no formally admitted market evidence or betting execution record. Separate `model-market-review` and `public-market-review` materials are preserved. Publicly collected price data is distinct from formal admission status.

As a separate research reference, the 7 pregame projections in the existing initially saved totals trace were compared with official final scores. Mean absolute error of projected totals was 3.276 runs, RMSE was 3.809 runs, and the saved 80% intervals covered 6 of 7 games. This is descriptive statistics from a small single-day sample only. Because no benchmark or betting data is linked, it is not interpreted as an official O/U settlement or betting return. Raw game summaries from the MLB Stats API, request URLs, and observation times in UTC for the other three games are preserved in JSON at `saved_totals_diagnostic.games[].result_source`. The existing totals predictions and the four primary-game result receipts are referenced by explicit paths.

## Reproduction and evidence

Below is the reproduction record for two coordinator CLI calls that have already completed. Do not rerun the same collection. Both exited 0 and returned `workflow_complete=true`; status revalidation confirmed processing complete with 0 errors.

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_daily_flow postgame --root "$PWD" --pregame-receipt audit/prospective_ml_daily_flow/2026-09-24/6d8dc9b9548f6fe1e51d0314f6892c080efc041ceca17c2f0f6ed710bbdc268a.json
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_daily_flow postgame --root "$PWD" --pregame-receipt audit/prospective_ml_daily_flow/2026-09-24/9109030ceaff7cbd66a7e42e90c931923c178e78b2a7d43647b84a76fbe705f0.json
```

Settlement outputs: the 3-claim calibration report `reports/prospective_ml_observation/362f048331f16996f2a44410e3c91d728b01f499e2429b1eee9df0cc4548c59f.json` (0 wins, 3 losses) and the 1-claim report `reports/prospective_ml_observation/0a6515411a69c3e1a0fdbb8c834cce67201001aaec54d813d68f37a3a3343f19.json` (1 win, 0 losses). Coordinator postgame receipts, all four settlement heads, and official-result receipts are recorded under `postgame_receipts` and `games` in `prospective_ml_settlement.json`. The existing `model-market-review.json` and `.md` were not modified.
