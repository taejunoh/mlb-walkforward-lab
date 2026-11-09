# Prospective ML shadow comparison

Research-only; `NO_PROMOTION`.

- Primary pairs: 4
- Paired Final: 4
- Pending: 0
- Excluded non-Final: 0

| gamePk | status | baseline p(home) | candidate p(home) | reason |
| ---: | --- | ---: | ---: | --- |
| 823327 | PAIRED | 0.589847 | 0.580447 |  |
| 823410 | PAIRED | 0.428168 | 0.431986 |  |
| 824868 | PAIRED | 0.754087 | 0.738054 |  |
| 824060 | PAIRED | 0.459090 | 0.458203 |  |

### Baseline reliability

| probability bin | n | mean predicted home-win probability | observed home-win rate |
| --- | ---: | ---: | ---: |
| [0.4, 0.5) | 2 | 0.443629 | 0.500000 |
| [0.5, 0.6) | 1 | 0.589847 | 0.000000 |
| [0.7, 0.8) | 1 | 0.754087 | 1.000000 |

### Candidate reliability

| probability bin | n | mean predicted home-win probability | observed home-win rate |
| --- | ---: | ---: | ---: |
| [0.4, 0.5) | 2 | 0.445094 | 0.500000 |
| [0.5, 0.6) | 1 | 0.580447 | 0.000000 |
| [0.7, 0.8) | 1 | 0.738054 | 1.000000 |

Brier/log loss are probability errors; lower is better. Reliability bins compare mean predicted home-win probability with observed home-win rate. Pending and non-Final games are not losses. This report is not evidence of profit, calibration, or validated promotion.
