# 2026-09-05 MLB Internal Model Game Evaluation

## Conclusion

Actual betting recommendation: PASS on every game. Price-based candidates for observation: ARI -108, MIN +106, WSH +180, NYY +110. These are mathematical price advantages if the model probabilities are taken at face value; they are not independently validated betting edges.

The internal model ran once and exited 0. Historical-input cutoff: 2026-09-04. Run started at 17:34:54 EDT; output file was produced at 17:45 EDT. FanDuel NY odds were observed at 17:43:19–21 EDT; they do not guarantee a currently executable price or the best price versus other books. Probabilities were produced for 9 games; inputs were missing for 2 games; 4 games were excluded by the status filter. Results for all 15 games, including produced and excluded results, were saved to the prospective observation.

## All games

Both sides of each game's moneyline (ML) were compared, and the side with the higher theoretical EV was shown. This may differ from the team's model win prediction. Difference = model win probability − break-even probability at that price (not a vig-removed probability).

| Game | Compared team | FanDuel | Model win probability | Break-even | Difference | Actual bet | Rationale / limitation |
|---|---|---:|---:|---:|---:|---|---|
| ARI @ HOU | ARI | -108 | 56.2% | 51.9% | +4.3%p | PASS | Starting-pitcher edge is key, but starter-data quality is 30.0%; overall completeness is 45.9% |
| MIN @ CWS | MIN | +106 | 52.0% | 48.5% | +3.5%p | PASS | Starting-pitcher edge; lineup is weaker; overall completeness is 57.8% |
| WSH @ LAD | WSH | +180 | 38.5% | 35.7% | +2.8%p | PASS | Model favors LAD, but -215 is expensive; WSH is a price-side underdog |
| NYY @ SD | NYY | +110 | 50.1% | 47.6% | +2.5%p | PASS | Starting-pitcher and lineup edges, bullpen disadvantage; effectively a 50:50 game |
| LAA @ PIT | LAA | +154 | 40.0% | 39.4% | +0.7%p | PASS | PIT is favored, but the price is expensive; only 0.7%p of price-side margin for LAA |
| BOS @ BAL | BOS | -132 | 57.3% | 56.9% | +0.4%p | PASS | Starting-pitcher, bullpen, and lineup edges, but only 0.4%p of price advantage |
| TOR @ KC | KC | -110 | 52.0% | 52.4% | -0.4%p | PASS | Both sides are below break-even |
| TB @ TEX | TEX | -108 | 51.0% | 51.9% | -0.9%p | PASS | Both sides are below break-even |
| MIL @ CIN | MIL | -156 | 58.8% | 60.9% | -2.1%p | PASS | MIL is more likely to win, but -156 is expensive; both sides have negative EV |
| SF @ NYM | — | — | — | — | — | PASS | Already started — excluded from pregame evaluation |
| CHC @ MIA | — | — | — | — | — | PASS | Already started — excluded from pregame evaluation |
| ATL @ PHI | — | — | — | — | — | PASS | Excluded because the current pipeline does not allow pregame Warmup status |
| DET @ CLE | — | — | — | — | — | PASS | Excluded because the current pipeline does not allow pregame Warmup status |
| STL @ COL | — | — | — | — | — | PASS | STL lineup and pitcher list were missing at collection time |
| ATH @ SEA | — | — | — | — | — | PASS | Both teams' lineups and pitcher lists were missing at collection time |

## Assessment of key candidates

- ARI: Contributions from starting pitching (+4.916), bullpen (+0.836), and lineup (+0.252) favor the away team. However, starter-input quality is 30.0% and overall completeness is 45.9%, so the largest price gap does not imply the highest confidence.
- MIN: Starting pitching (+3.145) and bullpen (+0.288) are favorable, but the lineup (-0.442) favors CWS. A marginal candidate at 52.0% win probability.
- WSH: LAD leads in starting pitching, bullpen, and lineup, with a model win probability of 61.5%. WSH at 38.5% is above the +180 break-even probability of 35.7%, making it a price-based observation candidate only; this is not a judgment that WSH is the stronger team.
- NYY: Starting-pitcher (+1.999) and lineup (+0.601) advantages, bullpen disadvantage (-0.849). The model's 50.1% makes this effectively a coin flip.
- BOS: Leads in starting pitching, bullpen, and lineup, but the price margin at -132 is only 0.4%p and is vulnerable to error.
- PIT/LAD: Their win probabilities are high at 60.0%/61.5%, respectively, but below the 64.8%/68.3% required by -184/-215. A high-win-probability team is not automatically a bet.

Contributions are in model weighted-score units, not win-probability percentage points. Overall data completeness across these 9 games was 45.9–60.5%. Environment and defense inputs had quality 0 / were treated as neutral values. Confirming a public nine-player lineup is not the same as having sufficient historical player features. Independent probability validation and performance-versus-odds validation are not yet sufficient, so every game is classified as a research observation.

## O/U

PASS on all games (not evaluated). The current internal totals execution path only permits 2026-09-01, so it could not produce total-run probabilities for September 5. Market totals are stored in the original market capture below, but are not presented as internal-model picks.

## Sources and validation

- [Internal trace](../../experimental_daily/2026-09-05/20260905T213454805831Z-6714f5b436977514.trace.json)
- [Internal raw output](../../experimental_daily/2026-09-05/20260905T213454805831Z-6714f5b436977514.json)
- [FanDuel NY snapshot](fanduel-reference-2026-09-05T214319Z.json)
- [Official MLB schedule](https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=2026-09-05&hydrate=probablePitcher)

Model and market files were joined one-to-one by team pair to calculate both sides of all 18 ML prices. HTML/MCP charts were not generated or shown because the validator did not accept non-SQL file-based sources; they were replaced with the exact table and calculation formula. Model sources and quality thresholds were not changed.
