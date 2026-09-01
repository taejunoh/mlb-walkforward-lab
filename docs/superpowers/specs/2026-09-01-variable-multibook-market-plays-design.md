# Variable Multi-Book MLB Market Plays

## Status and purpose

This design replaces fixed-count `Top 3` presentation with a full-slate,
price-aware `PLAY`/`PASS` decision layer. Every eligible MLB game is evaluated,
and only games that satisfy every fixed gate are labeled `PLAY`. A valid daily
result may contain zero, one, five, or more plays. The system never fills a
quota with a weaker candidate.

Moneyline and total-runs markets are evaluated independently. A game may
qualify in both markets, but the two results remain separate and must not be
presented as a parlay. `PLAY` is a provisional research designation, not a
guarantee of outcome or profit.

The supported New York sportsbook set is fixed to:

```text
FanDuel
DraftKings
BetMGM
Caesars
Fanatics
```

## Selected architecture

The selected approach is one normalized five-book market board consumed by
two independent offline evaluators. Book-specific raw captures remain separate
and append-only, while a normalization step binds valid quotes to MLB
`game_pk`. This preserves source evidence and provides one consistent view for
line shopping.

Alternatives rejected for V1 are:

1. five independent evaluation pipelines, because they duplicate evidence and
   make cross-book timing difficult to compare; and
2. a consensus-only input, because it discards the source book, exact line,
   and exact quoted prices needed for audit and reproduction.

## Hard boundaries

- The ML and totals models do not import or call sportsbook, browser, scraper,
  or HTTP code.
- Market collection is an external operation. Evaluators read only admitted
  local evidence.
- Odds, totals, thresholds, game lists, source names, and evaluation clocks
  cannot be overridden from the evaluation CLI.
- Model reports, model traces, lineup observations, raw market captures, and
  normalized market boards are immutable inputs.
- Every write is append-only and uses no-follow identity checks, canonical JSON,
  exact SHA-256 sidecars, and a trace as the sole success commit point.
- A missing or malformed market quote never causes a model rerun.
- A result with zero `PLAY` rows is valid and terminal.
- The system never places a wager or contacts a sportsbook during evaluation.

## External collection and raw evidence

An external collector obtains pregame markets from each supported sportsbook.
The collector may use an authorized browser session or another separately
approved transport, but that transport remains outside the model and evaluator.
Each book produces one raw, append-only capture containing:

```text
schema
source_book
captured_at_utc
source_url
transport_metadata
events
content_sha256
```

Each event preserves the book's event ID, displayed teams, scheduled start,
moneyline pair, and every displayed full-game total line with paired over and
under American prices. Raw bytes and the exact sidecar are retained. Source
URLs are provenance only and are never opened by the evaluator.

A book failure is recorded as unavailable and does not invalidate successful
captures from other books. A malformed or ambiguous event is rejected locally
rather than guessed.

## Normalized five-book market board

The normalizer admits the five raw captures, revalidates their identities and
sidecars, and maps events to the official MLB slate by `game_pk`. Matching uses
team identities, scheduled start, and the official schedule; team-name-only
matching is insufficient. An ambiguous identity, conflicting start, duplicate
book event, mixed book, unpaired price, or unknown game is rejected.

The normalized board contains all official games for the target date. Each game
contains zero or more closed quote records:

```text
ML quote:
  game_pk, book_id, event_id, captured_at_utc,
  away_american_odds, home_american_odds

Total quote:
  game_pk, book_id, event_id, captured_at_utc, line_half_runs,
  over_american_odds, under_american_odds
```

American prices are signed integers with absolute value in `[100, 10000]`.
Totals are positive integer half-run ticks. Each quote binds its raw capture
path and digest. The board also binds the official schedule observation and all
five capture outcomes, including unavailable books.

## Common pregame gates

A market can be labeled `PLAY` only when all common gates pass:

1. the game has not started;
2. official lineups contain nine unique batters for both teams;
3. both observed pitcher-list starters match the contemporaneous official
   probable pitchers;
4. no participant conflict or late-scratch marker is present;
5. each used quote is at most 300 seconds old at the single sealed evaluation
   time; and
6. at least three distinct supported books have valid paired quotes for that
   market and game.

Exactly 300 seconds is accepted. A quote older than 300 seconds is stale. Book
coverage is computed after invalid and stale quotes are removed. Three books
are sufficient; two are not.

## Moneyline evaluation

The ML evaluator consumes the sealed `v5.1-EXPERIMENTAL-LIVE` game result,
its trace, official participant evidence, and the normalized market board. It
does not rescore the game.

The model-predicted winner is the only ML side considered. In addition to the
common gates:

```text
minimum_data_completeness = 0.50
minimum_price_margin = 0.05
```

For every valid book quote, the evaluator selects the predicted winner's
American price and computes the quoted break-even probability exactly:

```text
positive A: 100 / (A + 100)
negative A: abs(A) / (abs(A) + 100)
price_margin = model_pick_probability - quoted_break_even
```

The best ML quote is the quote with the lowest quoted break-even probability.
The row is `PLAY` only when data completeness is at least `0.50` and the best
quote's price margin is at least `0.05`. Exact equality passes. Gate arithmetic
uses decimal/rational values rather than binary-float threshold comparisons.

The paired opposing price is retained for evidence and market-hold inspection,
but the raw quoted break-even probability, not a no-vig transformation, is used
for the `PLAY` gate.

## Total-runs evaluation

The totals evaluator consumes the sealed daily totals report, its 764 residual
samples per projected game, official participant evidence, and the normalized
market board. It evaluates every valid `(book, line, direction, price)`
combination independently.

The fixed total gates are:

```text
required_residual_count = 764
minimum_nonpush_support = 0.80
minimum_point_gap_runs = 1.00
minimum_support_margin = 0.05
```

For each line, over, under, and push support are computed from integer residual
samples and integer half-run ticks. Conditional support excludes pushes. The
support margin is conditional support minus the selected quote's exact quoted
break-even probability. Exact threshold equality passes.

A combination qualifies only when all common and total gates pass. At most one
total `PLAY` is emitted per game. The selected combination is determined by:

1. highest exact support margin;
2. highest nonpush support;
3. largest model-to-line point gap;
4. newest capture timestamp;
5. fixed book order; and
6. integer line ticks and direction as final deterministic tie-breakers.

Different books may offer different totals. A better price at a worse line is
not compared heuristically; each complete line-price combination must pass on
its own before deterministic selection.

## Decisions and reason codes

Every official game appears in both the ML and totals sections. Each row is
closed as `PLAY` or `PASS`. `PASS` rows carry every applicable reason in fixed
order. The initial reason vocabulary is:

```text
GAME_STARTED
INCOMPLETE_PARTICIPANTS
STARTER_CONFLICT
LATE_SCRATCH
INSUFFICIENT_BOOK_COVERAGE
STALE_PRICE
NO_QUOTE
MODEL_UNSCORED
DATA_COMPLETENESS_TOO_LOW
PRICE_MARGIN_TOO_LOW
RESIDUAL_COUNT_MISMATCH
NONPUSH_SUPPORT_TOO_LOW
POINT_GAP_TOO_SMALL
SUPPORT_MARGIN_TOO_LOW
AMBIGUOUS_DIRECTION
```

Integrity failures in model authority, lineup authority, schedule identity,
raw capture identity, normalized-board identity, or trace binding invalidate
the complete run rather than producing per-game `PASS` rows.

## Output and presentation

The evaluator writes one append-only report bundle containing:

- all-game ML `PLAY`/`PASS` rows;
- all-game totals `PLAY`/`PASS` rows;
- the selected book, line, paired prices, and capture time for each play;
- model probability or residual-sample support;
- quoted break-even probability and exact qualifying margin;
- data completeness and participant provenance;
- all rejection reasons;
- hashes for the model reports and traces, participant evidence, schedule,
  five raw capture outcomes, normalized board, policy, JSON sidecar, and
  Markdown; and
- a trace written last as the sole commit point.

There is no global play-count cap and no `Top 3` label. For readability only,
ML plays are ordered by descending price margin and totals plays by descending
support margin, followed by scheduled start and `game_pk`. This order is not a
rank and does not weaken any gate. ML and totals for the same game remain
separate; the output must not construct or recommend a parlay.

The Markdown prominently labels the result `RESEARCH ONLY`, `PROVISIONAL`, and
`NOT BET-VALIDATED`. It does not claim guaranteed outcomes or profitability.

## Persistence and failure behavior

The publication order is sidecar, JSON, Markdown, then trace. The trace is the
only successful terminal artifact. Existing identical bytes are accepted;
conflicts are integrity failures. After the trace write, every input and output
is rebound and rehashed. A failure before a valid trace leaves diagnostic
evidence but no successful result. A rerun never overwrites or retries a valid
zero-play report merely because no rows qualified.

Operational collection failures remain outside the evaluator. The normalized
board explicitly records unavailable books. The evaluator returns a normal
all-`PASS` result when market coverage is insufficient and returns an integrity
failure when admitted evidence is inconsistent or mutated.

## Verification strategy

Tests use hermetic sealed model, participant, schedule, and five-book market
fixtures. Coverage includes:

- zero, one, and five-or-more qualifying plays;
- no fixed-count truncation or quota filling;
- exact `0.50`, `0.05`, and 300-second threshold acceptance and immediately
  failing neighboring values;
- two-book rejection and three-book acceptance;
- best ML price selection across positive and negative American prices;
- different totals lines across books and deterministic candidate selection;
- same-game independent ML and total plays without parlay construction;
- missing lineups, starter conflict, late scratch, started game, stale quote,
  unavailable book, duplicate event, unknown game, and ambiguous identity;
- symlink, hardlink, file-type, oversized-file, digest, sidecar, rebind, and
  append-only conflict attacks;
- no sportsbook/network imports in either evaluator;
- no model rerun or mutation;
- trace-last fault injection and no partial-success claim; and
- deterministic canonical JSON, Markdown, sidecars, and trace bindings.

A local release check uses contemporaneous operator-authorized raw captures. If
fewer than three valid books are available, the run must honestly produce
`PASS/INSUFFICIENT_BOOK_COVERAGE`; tests never fabricate live availability.

## Success criteria

The feature is complete when one command can consume sealed daily ML and totals
authorities, official complete participants, and an admitted five-book market
board, then publish a trace-committed full-slate result whose number of plays is
determined exclusively by the approved gates. The implementation is not
complete if it still slices to three rows, silently drops games, permits caller
threshold overrides, performs network access in evaluation, or labels a
partial write as successful.
