# 2026-09-15 Pregame Re-evaluation

Experimental internal model. Not an independently calibrated or validated hit rate, and not a definitive betting recommendation.

- ML run: started 2026-09-15T21:53:16.995286Z, exit code 0.
- ML report: `20260915T215316995286Z-43740c88b46a282a.json`.
- Full scored trace: `20260915T215316995286Z-43740c88b46a282a.trace.json`.
- ML: 12 games scored, 3 not scored, 0 excluded.
- Pregame observer: observed at 22:04:05.726293Z. All 15 candidates, including 12 eligible primary claims, were stored at 22:04:05.975145Z–22:04:06.202386Z. All were before the earliest scheduled start at 22:40Z. This is not an official v5.2 sample.
- FanDuel public-price observation: 22:05:04Z (18:05:04 EDT). This does not guarantee the current price or that it could be executed.
- Raw odds: `data/experimental_multibook_market/raw/28af90080721ef9ee37b5b6971abf87d60fa80aefcf4b3be34ce7d4220a5efea.json`.
- The total-units error in the initial capture was not used and was preserved. The new capture above confirmed the 8.5→17 and 7.5→15 conversions.

The difference is model win probability minus the breakeven win probability at the listed price, in percentage points (pp). It is not expected return or empirical hit rate. The following is an arithmetic ML comparison, not an integrated automated PLAY/PASS result.

| Game | Model-favored team | Win probability | FanDuel ML | Breakeven | Difference | Completeness |
|---|---|---:|---:|---:|---:|---:|
| LAD–CIN | LAD | 73.6% | -245 | 71.0% | +2.6pp | 58.4% |
| ATH–TB | TB | 57.8% | -230 | 69.7% | -11.9pp | 60.3% |
| CWS–CLE | CLE | 52.4% | -152 | 60.3% | -7.9pp | 47.5% |
| MIL–PIT | MIL | 63.4% | -215 | 68.3% | -4.9pp | 58.0% |
| PHI–WSH | PHI | 64.7% | -235 | 70.1% | -5.4pp | 47.5% |
| DET–TOR | DET | 53.1% | +124 | 44.6% | +8.5pp | 54.8% |
| BAL–NYM | NYM | 53.5% | -132 | 56.9% | -3.4pp | 57.8% |
| ATL–CHC | CHC | 53.6% | -136 | 57.6% | -4.1pp | 59.6% |
| NYY–MIN | NYY | 59.1% | -168 | 62.7% | -3.6pp | 56.5% |
| SF–STL | STL | 52.9% | -174 | 63.5% | -10.6pp | 53.2% |
| BOS–TEX | TEX | 59.7% | -142 | 58.7% | +1.0pp | 55.4% |
| KC–HOU | HOU | 56.1% | Not observed | — | — | 60.3% |

DET has the largest model edge from offense, with starter and bullpen signals pointing slightly in the same direction. LAD leads in starter, offense, and bullpen, but its difference at the observed price is below the 5pp threshold. PHI and MIL have starter edges, but their prices are expensive relative to model win probability. PHI/CLE completeness is also below the 0.50 threshold. Defense and environment input quality is 0 for all ML rows, with neutral values used; this remains a limitation.

SD–COL favored SD, SEA–LAA favored SEA, and MIA–ARI did not meet lineup/pitcher-roster requirements in either model observation. Do not interpret those as PASS because the teams had no value.

## Integrated decision blocker

The totals run exited 0 and produced `reports/experimental_daily_totals/2026-09-15/20260915T220420131844Z-05eb2c46742236b1.trace.json`. New board admission also exited 0.

The integrated `daily_multibook_market_plays evaluate` exited 2. `market_play_authorities.py` requires residual_count and sample length to be fixed at 764, but each of today's 15 PROJECTED totals rows has 770. Due to this input-contract mismatch, no automated ML/O-U PLAY/PASS was generated. No historical 764-row data substitution, sample deletion, rerun, or source change was made.

DET is a candidate for further review, but a live betting decision is on hold. The O/U automated final decision is also on hold. The already stored ML predictions can be compared with future actual results.
