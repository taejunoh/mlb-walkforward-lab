# DraftKings-Only Daily Totals Market Overlay V1

## Status and purpose

This is a deliberately narrow, research-only comparison layer for the sealed
2026-09-01 `PROVISIONAL_DAILY_TOTALS_V1` report.  It consumes an explicit
DraftKings total and paired American prices *after* the internal model report
has been sealed, then deterministically classifies every projected game as
`RESEARCH_PICK` or `PASS`.

It is not part of the total-runs model.  It neither trains on, changes, nor
reruns that model.  It is not a betting service, an automated wager, or a
claim that the sample support is a calibrated win probability.  Its required
labels are `RESEARCH ONLY`, `ONE-SEASON PROVISIONAL`, and `NOT BET-VALIDATED`.

V1 is **DraftKings-only** and covers the exact 15 `PROJECTED` games in the
sealed 2026-09-01 totals report.  A later multi-book design is out of scope.

## Non-goals and hard boundaries

The overlay must not:

- import HTTP, browser, scraper, sportsbook, or network code in its core;
- query DraftKings, fetch a URL, or refresh a quote itself;
- write to or change `mlb_backtest.daily_totals`, its report, its receipt, or
  its trace;
- add market fields to the totals model artifact or use odds as a model
  feature, candidate-selection input, or model-selection tie-breaker;
- call a model sample rate a calibrated probability, fair price, expected
  value, edge, ROI, profitability, or wagering recommendation; or
- silently omit a projected game from the comparison.

Raw market capture is performed outside the core by a human in DraftKings or
by a separately operated external/manual capture procedure.  The core only
admits the resulting local files.  It treats a source URL as provenance, not
as a permission to contact that URL.

## Alternatives considered

1. **Command-line line/price flags or a worksheet.**  This is fast, but does
   not bind the entered prices to a source, time, raw capture, full slate, or
   immutable model report.  It permits accidental transcription mistakes and
   selective omission.
2. **DraftKings snapshot plus a pure overlay (selected).**  A complete,
   append-only snapshot carries the prices and provenance.  A separate pure
   overlay validates it against a sealed model report and records every
   outcome.  This is the smallest auditable design.
3. **Fetch DraftKings inside `daily_totals`.**  Rejected because a mutable
   network dependency would contaminate the model boundary, couple model
   availability to sportsbook availability, and encourage model reruns when a
   price changes.

## Existing authority to consume

The V1 authority is exactly the existing trace-committed report:

```text
report: reports/experimental_daily_totals/2026-09-01/
        20260901T150649125495Z-9fd1b2a4bad5c3f9.json
report SHA-256: 5f8e282b254fe0bdc74fb35bb1a5cd3da212990725bf4166a915dfc5ea745d06
trace: reports/experimental_daily_totals/2026-09-01/
       20260901T150649125495Z-9fd1b2a4bad5c3f9.trace.json
trace SHA-256: ca92083322fb6f0bc836f1c78ead0d60fd4e9f01c9e1e7eb14f46efc764749b6
model receipt: audit/experimental_daily_totals/model/
               e7cbc56c93ce4203d631a5f2ffb51d917331dddd8fb15244e0058b514c8dfee5.json
```

The overlay re-reads each named regular file without following symlinks and
requires all of the following before accepting market input:

1. the model report validates as `PROVISIONAL_DAILY_TOTALS_V1` and its
   `content_sha256` is correct;
2. the report `.sha256` sidecar contains exactly its byte SHA-256 plus one
   newline;
3. the trace validates as `PROVISIONAL_DAILY_TOTALS_TRACE_V1`, has the
   expected SHA-256, and binds the exact report path, report SHA-256, report
   sidecar SHA-256, model-receipt path, and model-receipt SHA-256;
4. the named model receipt and its `.sha256` sidecar validate, and the receipt
   SHA-256 is bound by both report and trace; and
5. the report has target date `2026-09-01`, cutoff `2026-08-31`, selected
   candidate `TEAM_VENUE_30`, and exactly 15 `PROJECTED` rows.  Their `game_pk`
   values form the only permitted V1 game set.

The core records a `comparison_bound_at_utc` only after this trace validation
succeeds.  That is the overlay's market-comparison observation time.  The raw
third-party capture time is separately preserved; the overlay makes no claim
about when DraftKings first published the quote.

## Market-input admission

### Raw capture

Before import, the operator saves an unmodified, local raw capture under:

```text
data/experimental_daily_totals_market/raw/<raw-capture-sha256>.json
data/experimental_daily_totals_market/raw/<raw-capture-sha256>.sha256
```

The bytes may be a manually exported JSON capture or a manually transcribed,
structured capture record.  The capture is external/manual; the importer never
opens its URL.  The importer requires a regular, non-symlink file, stable
before/open/post identity, full-file SHA-256, matching byte count, and an
exact sidecar before it writes any normalized snapshot.

### Normalized snapshot schema

`schemas/experimental_daily_totals_draftkings_snapshot.schema.json` defines
closed schema `PROVISIONAL_DAILY_TOTALS_DK_SNAPSHOT_V1`.  Its canonical JSON
fields are:

```text
schema, content_sha256, target_date, model_trace_relative,
model_trace_sha256, model_report_relative, model_report_sha256,
comparison_bound_at_utc, source_name, book_id, source_url,
capture_method, captured_at_utc, raw_capture_relative, raw_capture_sha256,
raw_capture_byte_length, games
```

The source fields are fixed as follows:

```text
source_name    = "DraftKings"
book_id        = "DRAFTKINGS"
capture_method = "EXTERNAL_MANUAL"
source_url     = an https URL whose host is sportsbook.draftkings.com
```

`comparison_bound_at_utc` and `captured_at_utc` are exact RFC3339 UTC strings
with a trailing `Z`; offset-less, non-UTC, malformed, future, or post-start
captures are invalid.  `captured_at_utc` must be no later than the
`comparison_bound_at_utc` clock reading.  The importer obtains
`comparison_bound_at_utc` after model-trace validation and immediately before
it writes the snapshot.  This records the required order without pretending
that the external raw source is cryptographically timestamped.

`games` has exactly 15 entries and its `game_pk` set exactly equals the 15
projected report game keys.  No duplicate or unknown `game_pk` is permitted.
Each entry is one of these closed variants:

```text
QUOTED:
  game_pk, status="QUOTED", draftkings_event_id, event_label,
  line_half_runs, over_american_odds, under_american_odds

NO_QUOTE:
  game_pk, status="NO_QUOTE", reason="NOT_LISTED"|"UNAVAILABLE"
```

`line_half_runs` is a positive integer measured in half-runs: `19` represents
9.5 and `18` represents 9.0.  Thus V1 accepts only integer and half-run totals
without floating-point equality.  Each American price is a signed integer with
absolute value in `[100, 10000]`; zero and values with absolute value below
100 are rejected.  Both directions share the one DraftKings event, line, and
global snapshot timestamp.  Mixed books, lines, or capture times cannot be
represented.

On success, the importer writes the normalized, hash-addressed admission
record and sidecar only to:

```text
audit/experimental_daily_totals_market/draftkings/<content-sha256>.json
audit/experimental_daily_totals_market/draftkings/<content-sha256>.sha256
```

It is append-only.  Reusing an existing path is allowed only when every byte
is identical; a conflicting existing file is an integrity failure.

## Pure overlay

`mlb_backtest.daily_totals_market_overlay` accepts only a root, the named
sealed totals trace, and the named normalized DraftKings snapshot.  It does
not accept a line, odds, model value, game list, source URL, or threshold as a
command-line override.  It first revalidates all model and snapshot bindings
described above, including no-follow identity checks and sidecars, then reads
the closed policy constant below.

It emits closed schema `PROVISIONAL_DAILY_TOTALS_OVERLAY_V1`, with a new
schema at `schemas/experimental_daily_totals_overlay.schema.json`.  Required
top-level fields are:

```text
schema, content_sha256, status_labels, target_date,
model_report_relative, model_report_sha256, model_trace_relative,
model_trace_sha256, market_snapshot_relative, market_snapshot_sha256,
policy_schema, policy_sha256, evaluated_at_utc, games
```

`games` has exactly the sealed report's 15 game keys in canonical report
order.  Each row retains `game_pk`, model `expected_total`,
`residual_count`, and the market status.  A quoted row additionally records
the half-run line, both observed American prices, the three sample-support
rates, both conditional support margins, selected side or null, final decision,
and explicit reason codes.  A no-quote row contains no invented price or
sample comparison.

Successful output paths are append-only:

```text
reports/experimental_daily_totals_overlay/2026-09-01/<timestamp>-<digest>.json
reports/experimental_daily_totals_overlay/2026-09-01/<timestamp>-<digest>.sha256
reports/experimental_daily_totals_overlay/2026-09-01/<timestamp>-<digest>.md
reports/experimental_daily_totals_overlay/2026-09-01/<timestamp>-<digest>.trace.json
```

The trace is the commit point.  It binds the report, report sidecar, model
trace, model-receipt sidecar, normalized snapshot, snapshot sidecar, policy,
JSON sidecar, and Markdown byte hashes.  JSON sidecar, JSON, Markdown, and
trace are written in that order.  A partial pre-trace write is not a successful
overlay report.

## Frozen sample-support math

For a projected game, let the report's already-hashed discrete residual sample
multiset be `T_1 ... T_N`, where V1 requires `N = 764`, and let `L` equal
`line_half_runs / 2`.

```text
over_support  = count(T_i > L) / N
under_support = count(T_i < L) / N
push_support  = count(T_i = L) / N
```

For a half-run line, `push_support` is exactly zero.  For an integer line it
is reported separately and never treated as a win or loss.

For an American price `A`, the quoted net-profit multiplier for one unit of
risk is:

```text
b = A / 100             when A > 0
b = 100 / abs(A)        when A < 0
quoted_break_even = 1 / (1 + b)
```

For either direction, with its `win_support` and `loss_support`, the
push-excluded support and margin are:

```text
conditional_support = win_support / (win_support + loss_support)
support_margin = conditional_support - quoted_break_even
```

If `win_support + loss_support` is zero, the direction has no conditional
support and must pass.  These are deterministic comparisons of a frozen
residual sample with an observed quoted price.  They are called only
`sample support`, `quoted break-even`, and `support margin`.  They are not
calibrated probabilities and must never be converted to fair odds, expected
value, return, edge, ROI, or profitability claims.

## Deterministic V1 policy

The canonical policy object has schema
`PROVISIONAL_DAILY_TOTALS_OVERLAY_POLICY_V1` and is compiled into the overlay;
its canonical JSON SHA-256 is reported as `policy_sha256`.

```text
required_residual_count = 764
maximum_quote_age_seconds = 900
minimum_nonpush_support = 0.80
minimum_point_gap_runs = 1.00
minimum_support_margin = 0.05
```

The selected direction is the side with the strictly larger support margin.
An exact tie yields `PASS` with `AMBIGUOUS_DIRECTION`.  The selected direction
is `RESEARCH_PICK` only when every condition below holds:

1. the model row is `PROJECTED` and its residual hash/count validate;
2. the row is `QUOTED` and all snapshot bindings validate;
3. `evaluated_at_utc` is before the scheduled start and the quote age is in
   `[0, 900]` seconds;
4. `N == 764` and `win_support + loss_support >= 0.80`;
5. `abs(expected_total - L) >= 1.00`; and
6. the selected side has `support_margin >= 0.05`.

Otherwise it is `PASS`.  Canonical reason codes include
`NO_QUOTE`, `GAME_STARTED`, `QUOTE_STALE`, `RESIDUAL_COUNT_MISMATCH`,
`NONPUSH_SUPPORT_TOO_LOW`, `POINT_GAP_TOO_SMALL`,
`SUPPORT_MARGIN_TOO_LOW`, and `AMBIGUOUS_DIRECTION`.

The point-gap and five-percentage-point support-margin thresholds are
governance guardrails, not empirically validated profitability thresholds.
The Markdown renderer must show `RESEARCH_PICK` and `PASS`, the quoted
DraftKings price, frozen sample support, and applicable warning labels; it must
not use bet-validated or financial-performance language.

## Failure policy

The following are integrity/contract failures: missing or mutated model
authority; invalid report/sidecar/trace/model receipt; model/snapshot binding
mismatch; non-DraftKings source; raw-capture hash or identity mismatch;
malformed timestamp; duplicate, missing, or unknown game key; invalid line or
American price; a snapshot captured after a game begins; output path conflict;
or any core-network import or call.  The command exits nonzero and creates no
overlay trace in these cases.

`NO_QUOTE`, an otherwise valid pregame quote that has become stale, a game that
has started before evaluation, an insufficient point gap, a low support
margin, or an ambiguous direction are policy outcomes, not malformed input.
They produce a trace-committed report with `PASS` and the corresponding reason
code.  A report with zero `RESEARCH_PICK` rows is valid and must not be rerun
just to seek a selection.

## Tests and acceptance criteria

Acceptance requires all of the following.

1. A fixture based on the exact sealed 2026-09-01 report validates all report,
   receipt, sidecar, and trace hashes and requires the exact set of 15
   projected games.
2. Snapshot admission rejects a missing game, duplicate game, unknown game,
   mixed book, mixed line, malformed DraftKings URL, bad raw sidecar, symlink,
   unstable identity, invalid UTC timestamp, post-start capture, invalid
   half-run tick, and invalid American price.
3. Independent calculation fixtures verify Over, Under, and Push support for
   both half and integer lines, including no-push-mass handling.
4. Independent price fixtures verify positive and negative American price
   multipliers, quoted break-even, and conditional support margin.
5. Boundary tests prove that a `0.049999...` support margin or `0.999999...`
   point gap passes no game, while values equal to the fixed thresholds satisfy
   those two gates; each remaining gate is tested independently.
6. Tests prove a stale quote, started game, no quote, and tied side all become
   explicit `PASS` rows rather than disappearing.
7. Given identical sealed model bytes, snapshot bytes, policy, and evaluation
   time, the per-game calculations and decisions are byte-identical.  A
   conflicting append-only destination is rejected.
8. Import-isolation tests fail if the core imports networking, browser,
   sportsbook, or `daily_totals` execution code, and prove it never reruns the
   model.
9. JSON schema validation, focused tests, existing totals tests, Ruff, and
   `git diff --check` pass.
10. Render tests require the three research labels and reject user-facing
    calibrated-probability, fair-odds, expected-value, return, edge, ROI,
    profitability, and wagering-recommendation claims.

Passing this specification permits an auditable, DraftKings-only research
comparison.  It does not establish that any `RESEARCH_PICK` will win or is a
validated wager.
