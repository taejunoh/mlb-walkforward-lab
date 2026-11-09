# 2026-09-24 Internal Model and Public Odds Comparison

There are 0 current betting PICKs. The 4 games with ML model outputs are research PASSes, 3 West Coast games are awaiting lineups, and 7 total-run predictions are for separate research use. Historical inputs were used only through September 23.

The initial pregame predictions for MIL–PHI, CLE–BOS, and CIN–ATL were saved to the ledger at 20:28:51 UTC; TB–NYY was saved at 20:39:04 UTC after a lineup update. All were saved before game start, and the first three were not overwritten. Both pregame flow receipts must be retained to include all four initial predictions.

| Game | New York start | Model home-win probability | Completeness | Away/home ML | Projected total / market line | Decision |
|---|---:|---:|---:|---:|---:|---|
| MIL@PHI | 18:05 | 51.6% | 60.1% | +114/-134 | 9.10/7.5 | PASS (research) |
| CLE@BOS | 18:45 | 54.3% | 44.0% | +108/-126 | 8.54/6.5 | PASS (research) |
| TB@NYY | 19:05 | 57.8% | 59.1% | +128/-152 | 8.37/6.5 | PASS (research) |
| CIN@ATL | 19:15 | 71.5% | 61.3% | +184/-220 | 8.96/7.5 | PASS (research) |
| HOU@ATH | 21:40 | Not produced | — | -162/+136 | 9.36/10.5 | Awaiting lineups; no bet |
| LAA@SEA | 21:40 | Not produced | — | +190/-230 | 8.78/6.5 | Awaiting lineups; no bet |
| SD@LAD | 22:10 | Not produced | — | +146/-174 | 8.77/7.5 | Awaiting lineups; no bet |

## Rationale

- MIL–PHI: PHI has a 51.6% win probability, but the break-even probability at -134 is 57.3%. On the other side, MIL +114 has a model probability of 48.4% versus a break-even probability of 46.7%, a nominal difference of +1.6%p. This is a small, unvalidated edge, so PASS. PHI's bullpen advantage offsets MIL's lineup and starting-pitcher advantages.
- CLE–BOS: BOS at 54.3% versus a 55.8% break-even probability at -126 has no price advantage. Input quality is particularly low on the starting-pitcher side, and overall completeness is 44.0%.
- TB–NYY: Using the updated nine-player lineups for both teams, NYY was modeled at 57.8%. This is below the 60.3% break-even probability at -152; TB +128 is also without a nominal edge (model 42.2% versus break-even 43.9%).
- CIN–ATL: ATL at 71.5% versus the 68.8% break-even probability at -220 has a nominal difference of +2.8%p. Starting pitching, bullpen, and lineup all favor ATL in the model, but with overall completeness of 61.3%, no defense/environment inputs, and limitations in independent probability validation and executable-price verification, this is an observation candidate, not a confirmed pick.
- HOU–ATH, LAA–SEA, SD–LAD: Both teams' lineups were not yet available, so no ML probabilities were fabricated.

The 80% research intervals for projected totals are broad, roughly 3–15 runs (by game, 3–14/4–15). TEAM_VENUE_30 does not incorporate lineups, starting pitchers, or weather; the total gaps above are not over/under probabilities or validated expected returns. O/U is also currently PASS.

Odds were observed on FanDuel's public page at approximately 16:15/16:33 EDT; the book's actual update time and executable price for a NY-logged-in account are unverified. The ATL recheck failed with 403, so the 16:15 observation was retained. The one-book constraint was observed, and the prices were not misrepresented as formally admitted market inputs.

Five games that had already started—STL–PIT, CWS–KC, MIA–CHC, NYM–TEX, ARI–COL—were excluded from new pregame predictions. Full evidence and two-way break-even calculations are recorded in `model-market-review.json`.
