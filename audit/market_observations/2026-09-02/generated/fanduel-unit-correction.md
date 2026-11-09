# FanDuel capture unit correction — 2026-09-02

The evaluator interprets `line_half_runs` as half-run ticks and computes
`Fraction(line_half_runs, 2)` (`mlb_backtest/multibook_totals_evaluator.py`,
lines 107–108). The earlier admitted FanDuel capture used decimal-tenths
values (85, 80, 75, 90, 65), which are wrong units and are superseded.

The corrected capture preserves the exact FanDuel source prices, event IDs,
and source-displayed pregame times from
`audit/market_observations/2026-09-02/20260902-browser-public-sources.json`.
All listed rows were displayed as `Today` pregame rows; live rows were
excluded. ET times are converted to UTC without replacing the source minute.

| Event ID | Matchup | Source time (ET) | Captured start (UTC) | ML away/home | Source total | Correct ticks |
| --- | --- | --- | --- | --- | --- | ---: |
| 36019406 | Philadelphia Phillies at Arizona Diamondbacks | 3:41 PM | 2026-09-02T19:41:00Z | -130 / +110 | 8.5, -118 / -104 | 17 |
| 36019408 | Seattle Mariners at Boston Red Sox | 4:11 PM | 2026-09-02T20:11:00Z | +122 / -144 | 8.5, -105 / -115 | 17 |
| 36019401 | New York Mets at Tampa Bay Rays | 6:41 PM | 2026-09-02T22:41:00Z | +144 / -172 | 8, -110 / -110 | 16 |
| 36019407 | Toronto Blue Jays at Cleveland Guardians | 6:41 PM | 2026-09-02T22:41:00Z | -132 / +112 | 7.5, +100 / -122 | 15 |
| 36019409 | San Francisco Giants at Pittsburgh Pirates | 6:41 PM | 2026-09-02T22:41:00Z | +128 / -152 | 8.5, -122 / +100 | 17 |
| 36019398 | Milwaukee Brewers at Chicago Cubs | 7:41 PM | 2026-09-02T23:41:00Z | -162 / +136 | 8.5, -104 / -118 | 17 |
| 36019399 | Detroit Tigers at Minnesota Twins | 7:41 PM | 2026-09-02T23:41:00Z | -104 / -112 | 8.5, -118 / -104 | 17 |
| 36019402 | Miami Marlins at Kansas City Royals | 7:41 PM | 2026-09-02T23:41:00Z | -104 / -112 | 9, -105 / -115 | 18 |
| 36019404 | Chicago White Sox at Houston Astros | 8:11 PM | 2026-09-03T00:11:00Z | +120 / -142 | 8.5, -114 / -106 | 17 |
| 36019405 | New York Yankees at Los Angeles Angels | 9:39 PM | 2026-09-03T01:39:00Z | -166 / +140 | 6.5, -112 / -108 | 13 |
| 36019397 | St. Louis Cardinals at Los Angeles Dodgers | 10:11 PM | 2026-09-03T02:11:00Z | +205 / -250 | 7.5, -124 / +102 | 15 |

Active corrected raw capture:
`data/experimental_multibook_market/raw/2ae20dcf0b037ad06fdad0237ec909badd72d71c3fab0053b27010e3f189510e.json`

Active corrected FanDuel outcome:
`audit/experimental_multibook_market/capture_outcomes/e3d93d63c5df7f8e5731eada1e2d66804987204090e38d4c82c8d74796d25f78.json`
