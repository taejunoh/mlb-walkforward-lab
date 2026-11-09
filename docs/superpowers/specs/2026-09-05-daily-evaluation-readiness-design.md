# Daily Evaluation Readiness Design

## Decision status

**Approved by the user on 2026-09-05; implemented with the frozen-candidate
deferral described below.** The
approved contract admits the literal MLB StatsAPI detailed state `Warmup` only when the
already-existing pre-start evidence gates hold, and makes daily totals construct
a date-matched sealed artifact instead of accepting only 2026-09-01. This
document changes no result, evidence, or production artifact. The approval
includes the rolling-date totals contract and schedule-completeness correction.

## Observed failure and root cause

The sealed 2026-09-05 Daily Top 3 report excluded ATL@PHI (`823419`) and
DET@CLE (`824389`) solely with `PREGAME_STATUS_NOT_ACCEPTED`. Its bound
schedule bytes show `Warmup`/`PW` for ATL at both observations; DET was
`Pre-Game` initially and `Warmup`/`PW` at final revalidation. All relevant
durable receipt times were before the scheduled starts (22:05Z and 22:10Z).

The rejection is a literal two-value allowlist in
`mlb_backtest.live_sources.ACCEPTED_PREGAME_STATUSES`. That allowlist controls
initial participant capture and final schedule revalidation, and is reused by
`live_top3_service`. A separate literal `{Scheduled, Pre-Game}` in
`experimental_totals_sources._closed_target_rows` would still reject the same
authenticated live schedule from the totals path.

The daily-totals command also rejects every target except 2026-09-01. Its
fixed `_WINDOWS` object is part of the model receipt, its candidate-definition
digest, the Python validator, the JSON Schema, tests, and the original
one-day-only plan. Removing only the date guard would make a later-date report
carry stale August final metrics and a stale candidate-definition hash.

## Alternatives considered

1. **Selected — literal `Warmup` plus strict temporal gates.** Add only
   `Warmup` to the shared allowlist and reuse the existing requirement that
   every schedule/participant/final receipt is durable strictly before the
   scheduled start. The service's aggregate boundary still excludes a row when
   the maximum relevant receipt time is equal to or later than start.
2. **Allow all MLB states with `abstractGameState == Live`.** Rejected: the
   same broad state includes in-progress games and does not provide a usable
   no-leakage distinction.
3. **Keep Top 3 permissive but leave totals strict.** Rejected: the exact
   `LIVE_SCHEDULE` source that permits the ML row would be unusable for its
   date-matched totals artifact, leaving internal ML/O-U evaluation incomplete.

For totals date handling, two materially different contracts are possible:

1. **Selected — date-matched rolling development and final diagnostics.** Seal
   the target's D-1 source chain, select from the preceding 61 calendar-day
   development interval, and record diagnostics for the preceding 31-day final
   interval. The receipt commits the exact dates, metrics, residuals, and
   selected candidate for that target.
2. **Rejected — fixed Sep-1 receipt/model with date-specific inference only.**
   A new trace could point to the Sep-1 receipt while projecting a later slate.
   This is smaller mechanically but the candidate, residuals, and diagnostics
   would no longer correspond to the new cutoff. It would turn one artifact
   into an implicit reusable model card without a defined freshness or
   revalidation contract.

The selected rolling contract maintains the current semantics of a
date-matched sealed receipt. It is not claimed to be the only valid modeling
approach, and it does not establish independent forward validation: later runs
overlap earlier diagnostic periods and are retrospective. The report must call
them rolling retrospective diagnostics, never untouched or independent
validation.

## Warmup admission contract

`Warmup` is a pregame *status admission*, not proof of pre-start eligibility.
It may be accepted only through the shared `ACCEPTED_PREGAME_STATUSES` value.
The following existing defenses remain mandatory and must be tested rather
than duplicated with a weaker new clock:

- `observe_live_slate` requires the schedule durable time before start before
  fetching/persisting participant evidence for an admitted game.
- Each admitted participant receipt must also be durable before start.
- `observe_final_game_schedule` requires the final-schedule receipt before
  start before returning a `FinalGameObservation`.
- `build_live_top3_run` computes the maximum relevant receipt boundary and
  excludes it when `boundary >= scheduled_start_utc`.
- The totals source adapter must impose the same strict binding-durable-before-
  scheduled-start check for every target row it independently admits. It must
  not rely on a status string alone or on an excluded Daily Top 3 row.

`In Progress`, `Final`, and every unknown status remain rejected. A game that
actually begins early but is still reported as `Warmup` cannot be distinguished
from source data already trusted by the current Scheduled/Pre-Game protocol;
the invariant therefore remains the sealed scheduled-start boundary, not an
invented actual-start field. This limitation must remain documented as a
research-only gate.

## Date-matched totals model contract

For target date `D`, derive all evaluation boundaries using calendar-day
offsets, with `cutoff = D - 1 day`:

| Purpose | Inclusive interval |
| --- | --- |
| Development evaluation | `D-92` through `D-32` (61 calendar days) |
| Rolling retrospective final diagnostic | `D-31` through `D-1` (31 calendar days) |
| Warm-up metadata | earliest admitted regular-season final for `D.year` through `D-93` |

`D-92` is deliberate: it preserves the approved Sep-1 protocol exactly
(`2026-06-01..2026-07-31` development and `2026-08-01..2026-08-31` final),
then advances each interval by the same number of calendar days for later
slates. “Three calendar months” is not used because month lengths vary and
would silently alter training/evaluation membership and candidate selection.
The earliest admitted regular-season final is evidence-derived solely for the
descriptive warm-up start; it does not inject results later than `D-1`.

Build must derive this window mapping from `D` and the admitted history before
any model calculation. Validation must require the exact derived development
and final bounds, chronological contiguity, fixed candidate hyperparameters,
and a warm-up range ending at `D-93`. The mapping is stored in the receipt and
included in `candidate_definition_sha256`; it is therefore frozen per artifact
rather than a mutable global setting. The existing Sep-1 mapping remains
byte-for-byte valid because derivation reproduces its recorded bounds.

The source report must be for the same `D`, and the history cutoff remains
exactly `D-1`. Historical walk-forward rows continue to use only
`official_date < row_date`; rolling final-diagnostic scores are never used for
candidate selection. The target projection sees no target-day outcome.
If either derived evaluation interval has fewer than 100 admitted rows, fail
before any output write. Do not substitute older windows, backfill a source
report, or rewrite any existing receipt/report/trace.

## Artifact compatibility and scope

Keep `PROVISIONAL_DAILY_TOTALS_MODEL_V1` and
`PROVISIONAL_DAILY_TOTALS_V1`: the V1 receipt already carries every window and
the candidate-definition hash, so target-specific values are self-describing
and reproducible. The schema must relax only the hard-coded warm-up dates to
valid dates; semantic validation in Python enforces the derivation. This keeps
schema and executable contract aligned while preserving every existing Sep-1
artifact.

This work uses already-admitted historical outcomes strictly before the target
date for the existing walk-forward evaluation and candidate selection. It does
not add a market collector, target-day/future/H24 outcome access,
missing-quality/H24 provenance scoring, odds, picks, profitability claims,
live ML-card fitting or promotion, or artifact rewrites. Missing-quality/H24
remain provenance metadata owned by their future workflow; this design does
not upgrade a research report to a betting recommendation.

## Approved schedule-completeness correction

Independent diagnosis found a small loss of known schedule information:
`source_parsers.schedule._doubleheader_marker` maps an explicit StatsAPI
`doubleHeader: "N"` to `None`, the same value used for a missing field. The
feature engine consequently treats known non-doubleheader as unsupported and
uses quality `5/6` (0.8333), not fully known schedule context.

This is a separately tested task because it can change feature quality and
therefore a scored output, even though it changes no learned weights. Preserve
explicit `N` as a closed marker such as `"NO"`; retain `None` only for absent
source evidence. Treat `"NO"` as `doubleheader=False` and
`doubleheader_supported=True`; keep `GAME1`/`GAME2` as known true and missing
as unsupported. Propagate the closed marker through preparation paths rather
than converting it back to absence. Add parser, feature-engine, and
no-weight-change regression tests. Any resulting golden-output change must be
limited to fixtures with explicit `N`, explained as increased source
completeness, and reviewed separately; it is not permission to re-fit or
alter weights.

### Implementation compatibility ruling

Integration tests demonstrated that changing `scoring_prep.py` prevents both
new shadow execution and reloading existing shadow attachments/paired reports:
the frozen profile pins those exact dependency bytes. Retain that file unchanged
and defer only the legacy candidate-preparation `NO` propagation until a new
validated freeze or versioned frozen runner is designed. Parser and live feature
completeness are still repaired. Task 4 is therefore partial, not complete for
the frozen candidate path. No receipt or dependency pin is rewritten.

The shared parser's new closed marker must also be accepted by schedule-history,
candidate, and lock-row validators in `prepare.py`, `pipeline.py`, and
`locking.py`. These validators add only `NO`; arbitrary strings remain rejected.
This compatibility correction changes neither scoring rules nor past artifacts.

### Historical normalization boundary discovered during configured verification

The required parent evidence exists in the declared LFG recovery authority.
After supplying that authority, the formerly blocked recovery test exposed a
real compatibility error: recovery receipts hash the default `parse_schedule`
normalized rows, so globally changing `N` to `NO` changes immutable digests.
The implementation must therefore keep historical/default parsing byte-compatible:
`parse_schedule(raw)` retains `N -> None`. A keyword-only, strictly boolean
`preserve_explicit_non_doubleheader=False` option enables `N -> NO`; only
`parse_live_schedule` explicitly enables it. Live totals uses that same live
adapter. Missing markers remain `None` in both modes, and historical/recovery
call sites remain unchanged. The closed downstream enum acceptances do not
upgrade or rewrite historical normalization. No pinned digest is updated.

## Acceptance criteria

- A pre-start `Warmup` game can produce the same eligible path as `Pre-Game`;
  an at-start/after-start Warmup and all started/terminal statuses cannot.
- The totals adapter admits only pre-start `Scheduled`, `Pre-Game`, or
  `Warmup` source rows and binds the same strict receipt timing.
- A Sep-5 source report produces a receipt whose windows are Jun-5--Aug-4 for
  development and Aug-5--Sep-4 for final, with a Sep-4 cutoff; it never uses
  Sep-5 results.
- The Sep-1 fixture/replay still validates with its existing windows and its
  pre-existing bytes are not changed.
- Focused unit tests, schema validation, affected Top-3/totals regressions,
  Ruff, and `git diff --check` pass before any daily evaluation is run.
