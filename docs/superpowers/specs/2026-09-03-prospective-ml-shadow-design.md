# Prospective paired ML shadow comparison

Status: user approved the described same-input baseline/candidate observation, settlement and comparison workflow with “진행하자” on 2026-09-03.

## Scope

Build a manual, offline shadow adapter beside the existing genuine ML observer.
Keep the active model card, daily scorer, market/PLAY gates and all frozen
historical artifacts unchanged. Apply the existing corrected temporal candidate
parameters; do not fit, tune, replay 2025 or claim profitability.

The candidate changes BOTH normalization and calibration. Its source experiment
remains RETROSPECTIVE_CANDIDATE_ONLY_NOT_FOR_LIVE_USE. A new SHADOW_ONLY,
NO_PROMOTION attachment is authorization for prospective research comparison,
not deployment authority or a relabeling of the old experiment.

## Design

1. Load only the corrected prepare receipt and sidecar. Require SHA-256
   9bc723332b80f36d8786f5258a51ebd247cf7b05879ca2d68a28e02c32ff5805,
   the existing labels, 2022 normalization, 2023 calibration and recorded
   current scoring-dependency digests. Never open historical predictions/outcomes.
2. Authenticate an explicitly supplied genuine baseline primary claim and snapshot.
   Rebind its sealed ML trace and scored raw-feature row. Require identical
   game/date/start/home/away identity, baseline probabilities/side and raw-feature
   digest. Rescore that exact raw input with frozen candidate normalization and
   calibration using existing scoring primitives.
3. Persist one append-only shadow attachment per game/baseline-model/candidate
   profile. The existing baseline claim and observation and actual shadow
   persistence time must all be strictly before scheduled start. No caller-supplied
   historical time in CLI; no after-start backfill. Exact repeated calls return the
   existing authenticated attachment rather than producing another primary.
   Check the clock and all source durable times/bindings immediately before
   committing a separate fixed first-attachment marker. Staged prediction files
   alone are not admitted; a failed validation leaves evidence but cannot later
   become a valid pair merely because a changed source was restored.
4. Use the baseline snapshot's existing authenticated official settlement once.
   A separate paired report accepts exact attachment paths and settlement heads;
   no directory-glob/latest selection. Duplicate games or conflicting heads reject.
   Missing heads stay pending; valid non-FINAL results stay explicitly excluded.
   Tampered evidence, unrelated heads or identity mismatches reject.
5. Compare the two probabilities on the exact same FINAL paired games using existing
   accuracy, Brier, log-loss and fixed 0.1 reliability-bin functions. Add mean
   predicted home probability per populated bin for comparison to realized home-win
   frequency. Preserve model/profile cohorts and completeness strata, sample sizes,
   pending/excluded counts and reasons; no mixed-version aggregate claim.
   Empty metrics are null. JSON plus readable Markdown reports explain metric
   direction, uncertainty, and NO_PROMOTION; no automatic winner/promotion threshold.

## File boundaries

- New mlb_backtest/prospective_ml_shadow.py: frozen profile, same-input scoring,
  attachment admission/load/persist and snapshot CLI.
- New mlb_backtest/prospective_ml_shadow_report.py: paired report/Markdown/CLI.
- New tests/test_prospective_ml_shadow.py and tests/test_prospective_ml_shadow_report.py.
- New docs/prospective_ml_shadow.md usage guide.

Reuse existing secure append-only primitives, observer validators, trace validation,
and settlement chain validation. Do not create a second outcome engine or a fake
model card for the shadow candidate. Small shared test fixture additions may use
tests/prospective_ml_fixtures.py if required; preserve its existing content.

## Acceptance

Tests must catch changed normalization not applied, wrong raw row, source mutation,
after-start creation/persistence, duplicate invocation, malformed probability,
unrelated settlement, duplicate pairing, and incorrect metric denominators.
Fixture integration exercises snapshot to official result to paired report without
network. Existing observer/settlement regressions remain passing. The implementation
does not manufacture actual prospective observations from old reports.

## Boundaries

Use only the existing LFG live-venue-timezone-fix worktree. Preserve user changes.
No Documents, custody/production, privileged operations or forbidden manifest access.
No network, installation, historical suite, commits, push, merge, PR or automation.
Testing writes only temporary fixture outputs. Existing model and sealed evidence
digests must remain unchanged. Feature completion is distinct from accumulating
enough future independent evidence to judge candidate quality.
