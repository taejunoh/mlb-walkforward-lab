# Postseason ML research runbook

This is an opt-in research path, not an activated daily operation. It does not
change the regular-season default, frozen model, or recurring automation.
Examples below document the verified CLI; running them against live sources,
adopting them in automation, or performing a live smoke check requires a
separate activation decision.

## What this mode means

Select `--competition-scope postseason-research-v1` explicitly. It evaluates
the complete observed target-date slate for official game types `F` (Wild Card),
`D` (Division Series), `L` (League Championship Series), and `W` (World
Series), with no Top-3 quota. Unsupported games observed in the source remain
visible as excluded rows; the mode does not claim to discover every unsupported
event.

The frozen regular-season model, coefficients, windows, and scoring are
unchanged. Recent same-season batting, starter, bullpen, rest, and travel
history admits `R/F/D/L/W` games through D−1 in `America/New_York`. Career
batters-faced and league-ERA inputs retain their regular-season definitions;
they are not postseason-expanded. Coverage must include the full history range
required by the existing windows, not only D−1.

Every report is `RESEARCH_ONLY`, `POSTSEASON_OUT_OF_DOMAIN`, and
`NOT_POSTSEASON_VALIDATED`. `VERIFIED` means input readiness only; it does not
mean the postseason probability is validated or that a wager is eligible.
Market recommendation is always `NOT_EVALUATED` with `PASS —
POSTSEASON_NOT_VALIDATED`. Totals are `NOT_EVALUATED —
POSTSEASON_TOTALS_NOT_SUPPORTED`. There is no postseason `PLAY`, price edge,
or P&L calculation.

## Create a full-slate report and trace

Use the approved worktree as the explicit root and select the intended target
date. This command acquires current source data; it is shown for a future
authorized run, not as evidence that a live run has occurred.

```sh
ROOT="/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix"
TARGET_DATE="YYYY-MM-DD"
PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1 python3 -B -m mlb_backtest.daily_top3 \
  --root "$ROOT" \
  --date "$TARGET_DATE" \
  --competition-scope postseason-research-v1
```

The command writes an immutable JSON report, Markdown report, and bound
`.trace.json` under
`reports/postseason_ml_research/<target-date>/`. Copy the exact trace path from
the report path it prints, replacing the report's `.json` suffix with
`.trace.json`. Do not select a trace by searching for a latest file. Inputs
missing for a game produce an explicit unscored/excluded row, not an invented
probability. Equality with scheduled start is too late for a prospective claim;
no post-start rerun can reconstruct one.

## Record the pregame receipt

`pregame` accepts one exact `--ml-trace` path per invocation. A trace contains
the full slate, so one invocation accounts for all games in that report. The
CLI does not accept an aggregated list of traces. If separately produced traces
must each be recorded, invoke `pregame` separately for each exact trace and
retain every returned receipt; do not replace earlier receipts with a guessed
or auto-discovered “latest” receipt. The first eligible primary claim for a
game/model identity is immutable; later runs may remain diagnostic and do not
replace it.

```sh
ROOT="/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix"
TARGET_DATE="YYYY-MM-DD"
TRACE="reports/postseason_ml_research/$TARGET_DATE/<selected-report-stem>.trace.json"
PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1 python3 -B -m mlb_backtest.prospective_ml_daily_flow pregame \
  --root "$ROOT" \
  --ml-trace "$TRACE" \
  --competition-scope postseason-research-v1
```

Record the exact `receipt_relative` printed by the command. Research receipts,
candidates, and claims are stored separately under
`audit/prospective_ml_postseason_research/`. Do not pass market evidence flags
in research mode. A primary claim is possible only when the existing
prospective participant/readiness checks pass and all required observation,
durable-completion, and claim times are strictly before scheduled start.
Otherwise the snapshot is diagnostic. Never reconstruct a missed prediction
after first pitch.

If a first-claim write is interrupted after reserving its identity, the
incomplete reservation is deliberately not repaired by a fresh invocation.
Raw files or seals without the successful completion index are not a valid
primary claim. Preserve the exact partial receipt and evidence; do not delete
the reservation or repeatedly retry it. Recovery requires a separately scoped
review, and cannot create a backdated pregame claim.

## Postgame, resume, and status

Postgame consumes an exact pregame receipt. It settles recorded claims against
official outcomes; pending games, including games awaiting possible resumption,
remain pending. A final tie or no contest is excluded as non-settleable. Exact
game, type, team, and forecast-lineage mismatches fail rather than being
guessed. Resume only from the exact prior postgame receipt for that same
pregame receipt.

```sh
ROOT="/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix"
PREGAME_RECEIPT="audit/prospective_ml_postseason_research/receipts/<exact-pregame-receipt-sha>.json"
PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1 python3 -B -m mlb_backtest.prospective_ml_daily_flow postgame \
  --root "$ROOT" \
  --pregame-receipt "$PREGAME_RECEIPT" \
  --competition-scope postseason-research-v1
```

For a later resume, add the exact `--previous-postgame-receipt` path printed
by the preceding postgame call. For read-only status, pass the same exact
pregame receipt and, if available, the exact `--postgame-receipt`:

```sh
PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1 python3 -B -m mlb_backtest.prospective_ml_daily_flow status \
  --root "$ROOT" \
  --pregame-receipt "$PREGAME_RECEIPT" \
  --postgame-receipt "audit/prospective_ml_postseason_research/receipts/<exact-postgame-receipt-sha>.json" \
  --competition-scope postseason-research-v1
```

Postgame produces a separate descriptive research summary: sample count, hit
count, accuracy, Brier score, and log loss. With no settled samples, metric
values are unavailable, not zero. These metrics do not join regular-season
primary, calibration, or shadow aggregates; shadow remains
`NOT_EVALUATED_FOR_POSTSEASON`. No evidence threshold automatically validates
the postseason probability.

## Operational boundary

This implementation/documentation task did not run a live smoke check, activate
the mode, or authorize recurring-automation adoption. Do not describe the
automation as postseason-ready. Any future activation must preserve explicit
scope selection, exact trace and receipt links, first-primary immutability,
and the research-only market/totals exclusions.
