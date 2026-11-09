# Exploratory cumulative probability check: 2026-09-02 + 2026-09-12

This is a narrow, read-only diagnostic over exactly the two cited all-day
ledgers (18 selected Final forecasts). It is **not** the full project history,
an official prospective calibration cohort, or a betting/ROI result. No primary
claims, predictions, model card, or evaluator code were created or changed.

## Population and orientation

The Sep 2 ledger contributes 13 selected Final pregame forecasts (4 wins, 9
losses); the Sep 12 ledger contributes 5 (3 wins, 2 losses). Selection was
frozen by each ledger's existing policy: latest eligible pregame forecast after
deduplicating supplied snapshots, with exact primary precedence where present.
For Sep 12, the 15:50 and 16:48 snapshots overlap on gamePk 823496 and 824224;
each game is counted once.

`pick_probability` is the selected-side probability: `p_home` for a home pick,
otherwise `1 - p_home`. Correctness is whether the official winner equals that
selected side. Pending and unscored games are outside this denominator.

| date | N | W-L | accuracy | mean selected p | Brier | log loss |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-09-02 | 13 | 4-9 | 0.307692 | 0.5640983239176771 | 0.2713294830854042 | 0.7371987386196466 |
| 2026-09-12 | 5 | 3-2 | 0.600000 | 0.5840074614680619 | 0.20160200372647852 | 0.5940658606408266 |
| **combined** | **18** | **7-11** | **0.388889** | **0.5696286399038951** | **0.2519607388190359** | **0.6974396058477521** |

Using fixed 0.1 selected-probability bins, the combined rows are: 0.5–0.6,
N=12, W=3, mean p=0.5348775971789703, observed rate=0.25; and 0.6–0.7,
N=6, W=4, mean p=0.6391307253537445, observed rate=0.6666666666666666.
The corresponding descriptive weighted absolute gap is 0.19909704522362093.
These small-sample figures are diagnostic only.

Direct row-level check across all 18 rows gives Brier sum
`4.535293298742647` and log-loss sum `12.553912905259539`; division by 18
produces the combined values above. The Sep 2 and Sep 12 Brier sums are
`3.5272832801102547` and `1.0080100186323926`, respectively; their log-loss
sums are `9.583583602055405` and `2.970329303204133`.

## Immutable source ledgers

- `reports/experimental_all_day_ml_ledger/2026-09-02/11869f74a98db326afa5d653f71c40b66f78bfb0341cdfbde75f6da336d5be2d.json`
- `reports/experimental_all_day_ml_ledger/2026-09-12/dfb3cae469850205919c4fc0da59b9183a53366fdf6e912ece976bca0bbb8fce.json`
