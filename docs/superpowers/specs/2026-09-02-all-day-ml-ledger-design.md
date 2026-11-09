# All-day ML prediction and outcome ledger

## User-approved purpose

The user approved recording the whole day's games, including day games, while
separating evidence quality at evaluation time. Do not confuse exclusion from
the strict prospective cohort with exclusion from the data record.

This extension joins existing predictions and contemporaneous market evidence
to official results. It does not recompute predictions, fit or promote models,
backdate prospective claims, place bets, or create a recurring automation.

## Scope and decision

Keep the current prospective observer/settlement/report unchanged. Add a small
separate all-day ledger and a readable table. This preserves the previously
agreed prospective contract while making day-game records visible.

Alternative rejected: silently admitting old reports as newly captured primary
observations would erase the distinction the user explicitly agreed to retain.
Alternative rejected: changing the whole live prediction/market pipeline is
unnecessary for recording existing evidence.

## Required data behavior

- Keep every game in the supplied authenticated daily slate, including games
  without a forecast, incomplete lineups, missing prices or unfinished results.
- Preserve existing forecast versions, capture timestamps, model identifiers,
  source paths and hashes. Do not pick a historical prediction based on whether
  it won. Apply a documented deterministic pregame selection rule before
  reading outcomes; keep the other versions as history.
- Distinguish exact existing prospective primary records, pregame reports
  retrospectively linked to outcomes, unscored records and post-start-only
  evidence. Missing values stay null with reasons.
- Copy prices only from authenticated, correctly scaled, contemporaneous
  pregame evidence. Preserve book, market, side, price, source time and reference.
  A later price must never become the price available with an earlier forecast.
- Read or collect official MLB results with the existing bounded transport and
  evidence helpers. Validate date/game/team identity; only a valid Final result
  yields a win/loss. Other states remain explicit, never counted as a loss.
- Use append-only/content-addressed outputs. Preserve earlier ledger versions,
  raw responses and all original source files. A repeated run is a new evidence
  version, not a new independent game. No silent latest-file selection.
- Show all games in the human-readable output with prediction, quoted price or
  missing reason, outcome/pending status and evidence class. Keep cohort counts
  separate and do not claim calibrated probabilities or profitability.

## Verification and boundaries

Exercise the real source admission and persistence with fake HTTP in tests.
Cover day-game inclusion, missing forecasts, post-start exclusion from pregame
evaluation, deterministic selection independent of outcomes, bad source hashes,
missing/superseded/mismatched odds, Final identity and nonfinal/error states,
repeat runs and preservation of original bytes. Perform separate specification
and code-quality review, then one live all-day collection using explicit local
inputs. Do not modify the old prospective cohort or its settlement heads.

Work only in the existing LFG linked worktree. No Documents workspace,
custody/production/holdout access, sudo, installs, commits, push, merge or PR.

## Concrete selection and execution contract

Inputs are explicit repeated ML trace paths, optional prospective report and
market trace paths, active raw-capture outcome paths with an explicit supersession
selector, and optional `gamePk=receipt` results. Authenticate all input
references first. The authenticated ML authorities supply the full slate;
different dates or conflicting game/team/start identities reject before fetch.

An exact existing primary snapshot from a validated prospective report takes
precedence. Otherwise select the latest scored, eligible pregame candidate
by `(observation_boundary_utc, executed_at_utc, trace SHA, trace path)` descending.
Both timestamps must strictly precede the saved start. Numeric probabilities
present only in unscored/excluded diagnostic trace rows remain history, not
selected forecasts. Keep all supplied versions and rejection reasons.

Not being in a ranked Top 3 is not an exclusion criterion. An authenticated
`SCORED_NOT_TOP3` trace row with `raw_features.eligible=true` and valid pregame
timestamps qualifies as a historical forecast, just like a ranked preliminary
row. Explicitly unscored/excluded/ineligible numeric diagnostics remain visible
and in history, but do not become evaluable predictions. September 2 PHI at ARI
is an actual scored-not-ranked case and must not be mislabeled unscored.

Copy an authenticated moneyline quote only when its exact ML trace binding and
predicted side match the selected forecast, and its capture and report times
precede the start. Preserve quote history. A quote captured after the forecast
but before start is explicitly `AFTER_FORECAST_BEFORE_START`, never described
as available at forecast time. Missing or incompatible selected quote remains
null, but retain available authenticated market-only observations even for a
game without an eligible forecast; do not throw away those price records.

September 2 has no market PLAY trace. Admit its raw-capture outcomes with the
existing `load_capture_outcome` validator, including raw/sidecar/book/time/count
bindings. Authenticate and bind the explicitly supplied supersession selector
snapshot; require the correct type/date and active outcome membership, and
reject any selected outcome or raw path marked `do_not_use`. Preserve the four
unavailable book outcomes and their reasons as well as the available capture.

These are independent price observations, not a generated PLAY decision or
an invented ML-trace binding. Retain both moneyline sides even without a forecast.
Use a ledger-local event match requiring normalized away/home teams, the same
UTC start date and an absolute start difference of at most 60 seconds, with
exactly one slate match. Otherwise preserve an unmapped reason; do not guess.
Retain both official and book starts, signed delta and the matching policy.
The observed book starts are 60 seconds later; compare pregame capture timing
against the official start, not the book start. Do not alter the existing market
board's exact-time matcher. No edge, ROI or recommendation is inferred here.

Reuse explicitly supplied validated result receipts. For remaining games,
make at most one bounded official request each, using the existing canonical
single-game schedule URL and raw/receipt helpers. Preserve received evidence.
Result failures stay visible on their game rows and cause a nonzero run status;
other rows and successfully saved evidence must remain recoverable. Report
persistence failure must also expose known durable receipt/output paths.

Write versioned content-addressed JSON and a Markdown table in the new
`reports/experimental_all_day_ml_ledger/<date>/` namespace. Existing prospective
claim/head/report files are read-only. Record cohort counts separately; no new
combined accuracy, ROI or model promotion threshold is introduced.
