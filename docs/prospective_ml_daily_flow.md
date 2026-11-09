# Prospective ML daily flow

The coordinator links already sealed pregame observations, optional locally
supplied market evidence, official result collection, and the research-only
shadow report. It does not run a model, collect market prices, discover a
latest file, or backfill a missed prediction.

All commands require an explicit project root and exact artifact paths. Receipt
files are immutable and content addressed below
`audit/prospective_ml_daily_flow/<target-date>/`.

## Pregame

```sh
python -m mlb_backtest.prospective_ml_daily_flow pregame \
  --root /path/to/MLB\ Analysis \
  --ml-trace reports/experimental_daily/2026-09-03/sealed.trace.json
```

Market evidence may be admitted in the same invocation, repeatedly:

```sh
  --market-trace reports/.../market_plays.trace.json \
  --market-raw-capture data/experimental_multibook_market/raw/<sha>.json \
  --market-unavailable-record evidence/fanduel-unavailable.json
```

Raw captures are durable but remain `SAVED_UNLINKED` until an explicitly
supplied evaluated market trace links them. An unavailable-book record is
reported as `UNAVAILABLE`; it is never treated as price evidence. Missing
optional market evidence is not fabricated. Every slate game, including
unscored or excluded games, is retained in the receipt.

## Postgame

```sh
python -m mlb_backtest.prospective_ml_daily_flow postgame \
  --root /path/to/MLB\ Analysis \
  --pregame-receipt audit/prospective_ml_daily_flow/2026-09-03/<sha>.json
```

Resume only with an exact prior coordinator receipt:

```sh
  --previous-postgame-receipt audit/prospective_ml_daily_flow/2026-09-03/<prior-sha>.json
```

Postgame uses only the claims and shadow commits named by the pregame receipt.
It builds the existing baseline report, invokes the existing result collector,
and builds the paired shadow report. Final settlements are carried on resume;
non-final results remain pending. No new observation or shadow snapshot is
created after first pitch.

## Status

```sh
python -m mlb_backtest.prospective_ml_daily_flow status \
  --root /path/to/MLB\ Analysis \
  --pregame-receipt audit/prospective_ml_daily_flow/2026-09-03/<sha>.json \
  [--postgame-receipt audit/prospective_ml_daily_flow/2026-09-03/<sha>.json]
```

Status is read-only and performs no network requests or writes. It reports
workflow processing separately from complete evidence, with ordered per-game
stages such as `BASELINE_NOT_PRIMARY`, `SHADOW_MISSING` or
`AFTER_START_IMPOSSIBLE`, `ODDS_MISSING`/`ODDS_SAVED_UNLINKED`/`ODDS_UNAVAILABLE`,
`MARKET_TRACE_MISSING`, pending result links, and `COMPLETE`.
