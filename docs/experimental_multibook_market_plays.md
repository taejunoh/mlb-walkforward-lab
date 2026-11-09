# Provisional multi-book MLB market plays

> **RESEARCH ONLY · PROVISIONAL · NOT BET-VALIDATED**

This is an offline decision layer over already-admitted local evidence. It does
not collect sportsbook prices, open a browser, run the model, place wagers, or
guarantee a result. The default V3 board intake accepts one to five actual,
distinct supported-book outcomes. Omit books that were not attempted; record
`UNAVAILABLE` only for an actual collection failure. Legacy V1 boards still
require one outcome for each supported New York book: `FANDUEL`,
`DRAFTKINGS`, `BETMGM`, `CAESARS`, and `FANATICS`.

## V3 event identity and evidence

V3 maps an available source event first by ordered away/home teams and an exact
UTC start. If there is no exact start, it permits a source start at most 60
seconds before or after the official UTC start only when exactly one official
game in the entire slate has those ordered teams. The comparison is integer
nanoseconds, inclusive at exactly 60 seconds; it never rounds a time, infers a
clock-only value, reverses teams, or chooses between doubleheader candidates.
V1 and V2 retain exact-start matching.

Each V3 board preserves every authenticated capture-outcome binding and an
event-match audit record (source event ID, original source and official starts,
integer delta, mapping rule, and raw-capture digest). Loading reopens the bound
outcomes and raw captures, rechecks their sidecars and bytes, then regenerates
the entire projection. Thus the source evidence remains as captured; the board
does not rewrite raw timestamps or captures. This is an integrity and matching
contract, not a statement about location, collector executability, or model
calibration.

## Handoff format

## Official schedule bridge

`--schedule-receipt` also accepts an existing admitted StatsAPI
`LIVE_SCHEDULE` receipt under `audit/experimental_daily/live_receipts/`. The
board reuses that receipt's immutable raw schedule, manifest, claim (when
present), completion evidence, and sidecars; it does not fetch a replacement
schedule or create a second official receipt. Only the exact target-date
schedule request is accepted, and every official game in that payload remains
on the board, including games already started or final, so downstream gates can
return `PASS` rather than silently removing them.

An available raw capture is canonical JSON under
`data/experimental_multibook_market/raw/` and its filename is the SHA-256 of
the bytes. A minimal event looks like this (the collector must include the
complete closed fields required by the schema):

```json
{
  "schema": "PROVISIONAL_MULTIBOOK_MARKET_RAW_CAPTURE_V1",
  "content_sha256": "<sha256-of-canonical-body>",
  "source_book": "FANDUEL",
  "captured_at_utc": "2026-09-02T18:00:00Z",
  "source_url": "https://sportsbook.fanduel.com/mlb",
  "transport_metadata": {"collector": "external"},
  "events": [{
    "book_event_id": "book-event-123",
    "away_team": "NYY",
    "home_team": "BOS",
    "scheduled_start_utc": "2026-09-02T23:10:00Z",
    "moneyline": {"away_american_odds": -110, "home_american_odds": -110},
    "totals": [{"line_half_runs": 17, "over_american_odds": -110, "under_american_odds": -110}]
  }]
}
```

`line_half_runs` is twice the total in runs: 8.5 runs is `17`, and 8 runs is
`16`.

If an attempted book collection fails, write an unavailable record and admit it
rather than substituting another source:

```json
{
  "schema": "PROVISIONAL_MULTIBOOK_MARKET_UNAVAILABLE_V1",
  "source_book": "FANATICS",
  "recorded_at_utc": "2026-09-02T18:00:00Z",
  "reason": "SOURCE_UNAVAILABLE",
  "detail": "collector did not receive a local response"
}
```

## Offline commands

All evidence arguments are strict POSIX-relative paths below `ROOT`. The
commands never accept date, book, line, price, threshold, URL, clock, game, or
model overrides.

```bash
python3 -m mlb_backtest.multibook_market_capture admit \
  --root ROOT --raw-capture data/experimental_multibook_market/raw/<sha>.json
python3 -m mlb_backtest.multibook_market_capture unavailable \
  --root ROOT --record audit/collector/fanatics-unavailable.json
python3 -m mlb_backtest.multibook_market_board admit-board \
  --root ROOT \
  --schedule-receipt audit/experimental_multibook_market/schedule/2026-09-02.json \
  --capture-outcome \
    audit/experimental_multibook_market/capture_outcomes/<fanduel>.json
python3 -m mlb_backtest.daily_multibook_market_plays evaluate \
  --root ROOT \
  --ml-trace audit/experimental_daily/2026-09-02/live-score.trace.json \
  --totals-trace reports/experimental_daily_totals/2026-09-02/report.trace.json \
  --market-board data/experimental_multibook_market/boards/<sha>.json
```

The board command prints the admitted board path. The evaluation command prints
exactly one terminal trace path on success. Exit `0` means the trace-committed
bundle is valid (including an all-PASS result), `2` means a contract or
integrity failure, and `1` means an operational failure or interruption.

## Interpreting results

Every official game appears once in both the ML and total sections. A `PLAY`
requires the common pregame gates, at least one fresh supported book, and the
market-specific exact-margin gates. Each selected quote remains a paired quote
with both sides of its market present; all other gates are unchanged. `PASS`
rows retain all applicable
reason codes in fixed order. The supported reasons are:

`GAME_STARTED`, `INCOMPLETE_PARTICIPANTS`, `STARTER_CONFLICT`, `LATE_SCRATCH`,
`INSUFFICIENT_BOOK_COVERAGE`, `STALE_PRICE`, `NO_QUOTE`, `MODEL_UNSCORED`,
`DATA_COMPLETENESS_TOO_LOW`, `PRICE_MARGIN_TOO_LOW`,
`RESIDUAL_COUNT_MISMATCH`, `NONPUSH_SUPPORT_TOO_LOW`, `POINT_GAP_TOO_SMALL`,
`SUPPORT_MARGIN_TOO_LOW`, and `AMBIGUOUS_DIRECTION`.

ML and total plays for the same game are independent decisions; the report does
not construct parlays or a fixed-size Top 3. Existing V1 Top 3 and
DraftKings-only totals artifacts remain preserved model authorities.
