# Provisional Daily MLB Totals Design

## Status and purpose

This design adds a separate, research-only internal total-runs projection. It
does not change `v5.1-EXPERIMENTAL-LIVE`, does not reinterpret a moneyline
probability as an Over/Under probability, and does not claim profitability.
The first usable output is an expected combined run total plus uncertainty for
the 2026-09-01 slate. An Over/Under verdict requires a separately timestamped
market total observed only after the internal projection is sealed.

The artifact label is exactly `PROVISIONAL_DAILY_TOTALS_V1`. Every report must
display `RESEARCH ONLY`, `ONE-SEASON PROVISIONAL`, and
`NOT BET-VALIDATED`.

## Scope

The first version uses only information that can be reproduced before the
target date from the existing authenticated experimental history cache:

- completed regular-season final scores through target D-1;
- official date, home team, away team, and venue;
- rolling league, team runs-scored, team runs-allowed, and venue rates.

It deliberately excludes probable starters, lineups, weather, roof state,
injuries, bullpen availability, and sportsbook data because the current
historical cache does not prove point-in-time pregame authority for those
fields. Missing fields are not neutral-filled and are not silently inferred.

## Alternatives considered

1. **Chronological shrunk run-rate model (selected).** A small deterministic
   candidate family uses only rolling, D-1 final-score history. It is fast,
   auditable, and can be evaluated today without adding a new dependency.
2. **Poisson or negative-binomial regression.** This can eventually use more
   features, but a same-day implementation would add optimizer and parameter
   authority without enough frozen historical pregame inputs.
3. **Market total adjusted by v5.1 ML components.** Rejected. The v5.1 edge is
   a home-minus-away win signal and has no learned mapping to combined runs.

## Source admission

The loader accepts one existing immutable MLB schedule-history raw object plus
its manifest and completed receipt chain. It revalidates the exact request,
raw SHA-256, byte count, and completion binding before parsing. It rejects
globs, caller-provided game rows, symlinks, non-regular files, mutable identity
changes, and any game whose final score is absent or malformed.

Accepted games must be MLB regular-season games with exactly two positive team
IDs, an official date, a positive venue ID, and non-negative integer home and
away runs. The canonical population is sorted by
`(official_date, scheduled_start_utc, game_pk)`. Duplicate `game_pk` or a date
after the declared cutoff is an integrity failure.

## Chronological protocol

For the 2026-09-01 projection, dates are fixed before evaluation:

- warm-up and rate history: 2026-03-25 through 2026-05-31;
- development evaluation: 2026-06-01 through 2026-07-31;
- untouched final evaluation: 2026-08-01 through 2026-08-31;
- live target: 2026-09-01, using history no later than 2026-08-31.

Every historical prediction is generated walk-forward. A row for date `D`
may use only games with `official_date < D`. The implementation must include a
leakage-injection test proving that changing game `D` or later cannot change
the features or prediction for `D`.

## Candidate family and selection

All windows are calendar-day windows ending at D-1. All candidates predict
away and home expected runs separately, then sum them. `LEAGUE_30` uses the
30-day away and home run means directly. The team candidates shrink each
team's all-game runs-scored and runs-allowed means toward the 30-day league
per-team mean with ten league-equivalent games. They calculate each side as
its league home/away mean multiplied by the geometric mean of the selected
team's shrunk scoring ratio and its opponent's shrunk runs-allowed ratio.
`TEAM_VENUE_30` then multiplies both side means by the square root of a 60-day
venue-total ratio shrunk toward 1.0 with twenty league-equivalent games. Rates
must be finite and strictly positive before log/geometric operations. The
three preregistered candidates are:

1. `LEAGUE_30`: 30-day rolling league home and away means.
2. `TEAM_30`: `LEAGUE_30` plus 30-day team runs-scored and opponent
   runs-allowed rates, each shrunk with ten league-equivalent games.
3. `TEAM_VENUE_30`: `TEAM_30` plus a 60-day venue total-rate factor shrunk with
   twenty league-equivalent games.

No other candidate or tuning value may be added after inspecting development
or final-evaluation results. Selection uses development dates only. A
candidate is eligible only if it has strictly lower MAE and strictly lower
mean Poisson deviance than `LEAGUE_30`. Among eligible candidates select the
lowest MAE, then lowest deviance, then the simpler candidate; if none qualify,
select `LEAGUE_30`. Poisson deviance for observation `y` and positive mean
`mu` is `2 * mu` when `y == 0`, otherwise
`2 * (y * log(y / mu) - (y - mu))`. Development and final windows must each
contain at least 100 admitted games. The August final period is reported once
and never used to change the selected candidate or constants.

## Distribution and uncertainty

The point estimate is `lambda_away + lambda_home`. Uncertainty uses the
selected candidate's strictly prior walk-forward development residuals. For a
live mean `mu` and development residual `r`, one empirical total sample is
`max(0, floor(mu + r + 0.5))`. The sorted discrete samples are immutable and
their canonical bytes are hashed. The report includes their nearest-rank 10th
and 90th percentiles as an 80% interval. It also records development and final
MAE, RMSE, mean Poisson deviance, and 80% interval coverage.

If a market line is compared after sealing, probabilities are exact counts in
that frozen discrete sample multiset divided by its size. Over is `T > L`,
Under is `T < L`, and an integer line additionally reports Push as `T == L`.
The model artifact never embeds or trains on a market line.

## Outputs

All outputs are isolated from the ML pipeline:

```text
audit/experimental_daily_totals/model/<model-receipt>.json
reports/experimental_daily_totals/<target-date>/<timestamp>-<digest>.json
reports/experimental_daily_totals/<target-date>/<timestamp>-<digest>.md
reports/experimental_daily_totals/<target-date>/<timestamp>-<digest>.trace.json
```

The canonical JSON report and required `.sha256` sidecar bind the source
receipt and raw digest, cutoff,
fixed chronological windows, candidate definitions, development selection,
August final metrics, residual-distribution digest, per-game projection, and
content SHA-256. Publication is append-only. JSON sidecar, JSON, Markdown, and
trace are written in that order; the trace is the commit point and binds the
other three byte digests. Any attempt missing that final trace is not a
successful report and is never reused as one.

Each per-game row contains exactly: game identity, teams, venue, scheduled
start, selected candidate ID, away/home expected runs, expected total, 80%
interval, feature cutoff, source digest, and data-quality flags. It contains
no pick, sportsbook, edge, playable-to, or profitability field.

## Failure policy

The command exits without a report when source admission, chronological
coverage, candidate selection, or final evaluation fails. A single target game
with an unknown team or venue is `UNPROJECTED` with an explicit reason while
other valid games may remain. NaN, infinity, negative expected runs, empty
development residuals, a post-cutoff source row, or a mutated input fails the
entire run.

## Testing and acceptance

Acceptance requires:

- closed source and report contracts with canonical round trips;
- D-1 leakage and future-row mutation rejection;
- deterministic candidate selection and byte-identical replay;
- independently calculated MAE, RMSE, Poisson deviance, quantiles, and
  coverage oracles;
- half-line and integer-line probability semantics tested separately;
- no import or write into the existing ML live pipeline;
- focused tests, existing live-history/live-top3 regression tests, Ruff, and
  `git diff --check` all passing.

Passing these checks permits only the label `internal provisional total
projection`. It does not permit `validated O/U bet`, `profitable`, or any
equivalent claim.

## Successor for later daily targets

The approved [2026-09-05 daily-readiness contract](2026-09-05-daily-evaluation-readiness-design.md)
derives sealed development intervals `D-92..D-32` and rolling retrospective
final diagnostics `D-31..D-1` for later target dates. It preserves the Sep-1
intervals and historical artifact; overlapping later diagnostics are not new
independent validation and do not authorize betting.
