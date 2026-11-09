# Prospective ML shadow comparison

This workflow is research-only. It records a frozen temporal candidate beside
the active baseline using identical pregame raw input. It never promotes the
candidate, creates a candidate model card, or settles a second outcome.

## Exact-path workflow

Run from the repository root and pass every input explicitly. First record the
baseline observer snapshot and retain its returned candidate path. The
observer also persists the baseline primary claim; set the next variable to
that exact persisted path (the current layout is
`audit/prospective_ml_observation/primary_claims/<game_pk>/<model_card_sha256>.json`;
the observer CLI prints candidate paths, so retain the claim path from its
durable output):

```sh
ROOT="$PWD"
ML_TRACE="RELATIVE_SEALED_ML_TRACE"
BASELINE_PRIMARY_CLAIM="RELATIVE_PRIMARY_CLAIM_PERSISTED_BY_OBSERVER"
OBSERVER_CANDIDATE=$(PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_observation snapshot --root "$ROOT" --ml-trace "$ML_TRACE")
printf 'observer candidate: %s\n' "$OBSERVER_CANDIDATE"
```

Create the frozen shadow snapshot before the scheduled game start. The command
returns the committed marker path; use that exact value as the report input.
The marker bytes, rather than an augmented loader record, are hashed into the
report reference:

```sh
SHADOW_ATTACHMENT=$(PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_shadow snapshot --root "$ROOT" --baseline-primary-claim "$BASELINE_PRIMARY_CLAIM")
printf 'shadow attachment: %s\n' "$SHADOW_ATTACHMENT"
```

Before an official result is available, create a pending paired report by
omitting settlement heads:

```sh
PENDING_PAIRED_REPORT=$(PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_shadow_report report --root "$ROOT" --attachment "$SHADOW_ATTACHMENT")
printf 'pending paired report: %s\n' "$PENDING_PAIRED_REPORT"
```

Build the existing baseline settlement report explicitly, then run the one-shot
official result collector against the returned report path only after the
official game is Final. This is a manual operational step; no network request
is executed by this guide itself. The collector owns official receipt capture
and settlement creation; do not invoke the manual `settle` command for the same
result. Save its JSON output: every
`dispositions[].head_relative` is an authenticated head to pass below.

```sh
BASELINE_REPORT=$(PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_settlement report --root "$ROOT" --primary-claim "$BASELINE_PRIMARY_CLAIM")
COLLECTION_JSON=$(PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_results collect --root "$ROOT" --report "$BASELINE_REPORT")
printf '%s\n' "$COLLECTION_JSON"

# Set HEAD_1, HEAD_2, ... to the exact non-null head_relative values returned
# in COLLECTION_JSON, then pass each one explicitly:
PAIRED_REPORT=$(PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m mlb_backtest.prospective_ml_shadow_report report --root "$ROOT" --attachment "$SHADOW_ATTACHMENT" --settlement-head "$HEAD_1")
printf 'paired report: %s\n' "$PAIRED_REPORT"
```

Repeat `--attachment` and `--settlement-head` for a cohort, using only paths
returned by the preceding commands. There is no latest, glob, or automatic
scheduler discovery. An empty invocation is valid and produces a report with
null metrics; it does not fabricate samples.

## Reading the report

`PAIRED` means a validated `FINAL` official result was observed strictly after the
attachment commit marker. `PENDING` means no matching head was supplied yet.
`EXCLUDED` means a matching official head exists but is not final. Pending and
excluded games are not losses and do not enter metric denominators.

Metrics are emitted for each distinct baseline `model_card_sha256` plus
candidate `profile_sha256` cohort, and within each cohort for completeness
strata `< 0.50` and `>= 0.50`; distinct baseline model cards are never combined
into one performance claim. Reliability bins show mean predicted home-win
probability beside observed home-win rate. Brier score and log loss measure
probability error (lower is better); they are not profit, calibration
certification, or promotion evidence. Candidate-minus-baseline deltas use the
same paired outcomes.

There is no after-start backfill: Task 1's fixed marker and source bindings are
validated before report construction. The report reuses the official result
receipt and settlement chain once, rather than letting the shadow claim an
independent outcome.
