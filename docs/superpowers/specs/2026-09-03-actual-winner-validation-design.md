# Actual-winner validation

**Status:** Approved design on 2026-09-03.

## Goal

Prevent retrospective temporal-calibration metrics from silently treating an
unknown official winner label as an away-team win.

## Behavior

The temporal-calibration candidate evaluator must accept a `Final` row only
when `actual_winner` is exactly that locked row's `home_team` or `away_team`
value. Any other value, including a missing value, empty string, different
case, tie, or unknown label, must raise a deterministic `ValueError` that
identifies the offending game. It must not normalize aliases, infer the
opponent, or exclude the row silently.

Non-Final rows retain their existing behavior. Rows whose winner equals the
locked home-team or away-team value must produce exactly the same labels and
metrics as before. This change affects evaluation-label integrity only; it does
not change the active model card, pregame scoring, probabilities, picks, or
prospective records.

## Implementation boundary

Add the smallest validation at the point where the evaluator converts a Final
row into the binary home-win outcome. Do not refactor unrelated evaluation or
scoring code.

## Verification

Use test-driven development:

1. Add a focused regression test proving an invalid `actual_winner` on a Final
   row raises instead of becoming an away win.
2. Verify the new test fails for the expected pre-fix behavior.
3. Add the minimal production validation.
4. Verify valid home/away behavior remains unchanged and run the focused
   temporal-calibration test module plus Ruff and `git diff --check`.

No historical evaluation rerun, network access, model promotion, commit, push,
merge, or PR is part of this task.
