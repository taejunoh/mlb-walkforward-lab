# Prospective MLB settlement review — 2026-09-23

Compared all 16 pregame primary claims (model card `9bad89b9…f7912759`) with MLB StatsAPI final scores. The slate was diagnostically 9/16 (56.25%) correct. Fifteen games have formal immutable settlement heads; one game (TOR–BAL, gamePk 824785) is excluded from formal metrics because the existing collector rejected the official response shape. Its official 9/23 final score, Baltimore 4–2, is included only in the 16-game diagnostic comparison.

| gamePk | Matchup | Final | Pregame pick | p(home) | Correct? | Settlement |
|---:|---|---:|---|---:|:---:|---|
| 824223 | WSH @ DET | 4–2 | DET | 0.6349 | No | Formal |
| 824785 | TOR @ BAL | 2–4 | BAL | 0.5653 | Yes | Diagnostic only — collector format blocker |
| 823168 | MIN @ SF | 4–2 | SF | 0.5156 | No | Formal |
| 824784 | TOR @ BAL | 2–4 | BAL | 0.5016 | Yes | Formal |
| 823327 | STL @ PIT | 5–1 | PIT | 0.5898 | No | Formal |
| 823410 | MIL @ PHI | 4–1 | MIL | 0.4282 | Yes | Formal |
| 823492 | TB @ NYY | 2–9 | NYY | 0.5663 | Yes | Formal |
| 824710 | CLE @ BOS | 0–1 | BOS | 0.5428 | Yes | Formal |
| 824868 | CIN @ ATL | 2–3 | ATL | 0.7541 | Yes | Formal |
| 824060 | CWS @ KC | 4–5 | CWS | 0.4591 | No | Formal |
| 824625 | MIA @ CHC | 3–2 | CHC | 0.5905 | No | Formal |
| 822841 | NYM @ TEX | 7–2 | NYM | 0.3870 | Yes | Formal |
| 824301 | AZ @ COL | 5–3 | AZ | 0.4809 | Yes | Formal |
| 824951 | LAA @ ATH | 3–7 | LAA | 0.3726 | No | Formal |
| 823086 | HOU @ SEA | 5–6 | SEA | 0.5821 | Yes | Formal |
| 823894 | SD @ LAD | 5–1 | LAD | 0.6856 | No | Formal |

The 16-game diagnostic metrics are pick accuracy 9/16 (56.25%), `p_home` Brier 0.259753, and log loss 0.712949. On the 15 formally settled games alone, the metrics are 8/15 (53.33%), Brier 0.264473, and log loss 0.722453. These populations are deliberately reported separately; 824785 is not represented as formally settled.

The collector failure is reproducible from its saved StatsAPI response: the exact query `https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-09-23&gamePks=824785` returned both a 9/22 postponed schedule entry and the 9/23 Final entry. The existing parser requires exactly one date bucket and exactly one game, so it returned `official result schedule differs`. Source response was captured at `2026-09-24T20:15:59.2588Z`; the matching 9/23 Final entry is Baltimore Orioles 4, Toronto Blue Jays 2. No raw response was edited or normalized, and no settlement head was created for this game.

A documented direct settlement CLI exists in `docs/experimental_live_top3.md`, but it uses the same official-result parser and cannot admit this two-bucket response. The official result collection design is documented in `docs/prospective_ml_result_collection.md`.

All 16 prospective-ML snapshots have `totals_status: NOT_EVALUATED`; that status applies only to those snapshots. This review did not settle the separate `daily_totals` reports. For a diagnostic comparison, I used the earliest integrity-bound valid pregame report for this slate, timestamped 2026-09-23 09:14:45.528603 UTC. It has 16 projected games, a 2026-09-22 feature cutoff, and predates every scheduled start. Eight later same-date reports were not used to select performance. The selected report and all 16 linked official result payloads were checked locally; report and payload hashes matched their receipts, and each game had exactly one matching 9/23 Final entry. This separate provisional expected-total comparison has MAE 2.885 runs and RMSE 3.430 runs (n=16), descriptive for this slate only.

| gamePk | Matchup | Forecast total | Actual total | Absolute error |
|---:|---|---:|---:|---:|
| 824223 | WSH @ DET | 8.94 | 6 | 2.94 |
| 824785 | TOR @ BAL | 8.03 | 6 | 2.03 |
| 823168 | MIN @ SF | 9.50 | 6 | 3.50 |
| 824784 | TOR @ BAL | 8.03 | 6 | 2.03 |
| 823327 | STL @ PIT | 8.98 | 6 | 2.98 |
| 823410 | MIL @ PHI | 9.27 | 5 | 4.27 |
| 823492 | TB @ NYY | 8.25 | 11 | 2.75 |
| 824710 | CLE @ BOS | 8.76 | 1 | 7.76 |
| 824868 | CIN @ ATL | 9.07 | 5 | 4.07 |
| 824060 | CWS @ KC | 9.59 | 9 | 0.59 |
| 824625 | MIA @ CHC | 10.55 | 5 | 5.55 |
| 822841 | NYM @ TEX | 9.40 | 9 | 0.40 |
| 824301 | AZ @ COL | 9.57 | 8 | 1.57 |
| 824951 | LAA @ ATH | 9.44 | 10 | 0.56 |
| 823086 | HOU @ SEA | 8.68 | 11 | 2.32 |
| 823894 | SD @ LAD | 8.85 | 6 | 2.85 |

These are expected combined runs, not an Over/Under market pick or settlement. The pregame betting pick count was zero, so neither comparison is realized P&L.

Official query URLs and per-response `asof_utc`, primary probabilities, result receipt paths, daily-total comparison rows, and all 15 settlement-head paths are recorded in [the machine-readable review](prospective_ml_settlement.json). MLB results were captured from 2026-09-24 20:15:59Z to 20:16:30Z via the existing daily-flow/result-collector workflow; eight original pregame receipts are under `audit/prospective_ml_daily_flow/2026-09-23/`. The separate daily_totals source is `reports/experimental_daily_totals/2026-09-23/20260923T091445528603Z-db519940b966ed12.json`; it was compared diagnostically only.
