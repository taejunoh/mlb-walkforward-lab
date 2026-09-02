# Experimental Live Daily Top 3 — Operator Guide

> **RESEARCH ONLY · NON-OFFICIAL SAMPLE · PROFITABILITY NOT VALIDATED**

This command is a local research view labeled `v5.1-EXPERIMENTAL-LIVE`. It is
not a betting recommendation, a production service, a formal prospective
validation ledger, a continuation of the v5.1 retrospective sample, or a v5.2
result.

## Run the command

From the canonical worktree:

```bash
cd "/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest"
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3
```

The default target is today's date in `America/New_York`. For controlled
same-day operation, add `--date YYYY-MM-DD`. Do not use an earlier date to
simulate a historical or prospective result.

## Bootstrap and later runs

The first run authenticates the frozen v5.1 card and bootstraps a separate,
result-free current-season history cache through D-1. It acquires schedule,
Statcast, league-ERA, venue/time-zone, and required starter career-BF evidence
before using current-day observations. This can take many minutes.

Later runs verify the existing cache and acquire only missing D-1 history. They
still fetch fresh current-day schedule and participant observations and perform
an immediate final schedule revalidation. A verified cache may be reused after
a transient history-network failure only for the same target/cutoff contract;
current-day observations are never silently replaced with stale data.

The D-1 boundary is exact. The set of terminal, played `gamePk` values in the
authenticated D-1 schedule must equal the set present in D-1 Statcast. A played
game with zero Savant rows has no exception. If Savant has not yet published
those rows, the command fails closed, writes no ranking report, preserves the
attempt evidence, and should be rerun after the source catches up.

## Candidate states

- `VERIFIED` is reserved. Under this MVP, `verified_top3` is always an empty
  array; schedule and boxscore agreement does not prove an officially announced
  starter.
- `PRELIMINARY` requires every scoring input, consistent probable starters,
  two complete ordered nine-player pregame lineups, and all relevant durable
  receipts before the scheduled start.
- `UNSCORED` records a game-specific missing or conflicting input and assigns
  no probability or rank.
- `EXCLUDED` records a noneligible status or population disposition, including
  started, final, postponed, canceled, suspended, or unknown-status games.

Each Top 3 array may contain zero through three rows and is never padded. A
valid report with fewer than three Preliminary rows is a valid operational
result.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Valid report written, including a report with fewer than three rows. |
| `1` | Operational/source failure; no partial Top 3 is successful. |
| `2` | Integrity or contract failure. |
| `130` | Keyboard interruption. |

## Artifacts

- D-1 cache and immutable raw artifacts: `data/experimental_daily/`
- Append-only observation receipts and failure evidence: `audit/experimental_daily/`
- Successful run reports: `reports/experimental_daily/YYYY-MM-DD/`

Successful reports are append-only canonical JSON plus Markdown rendered from
that JSON. There is no mutable `latest` file and reruns do not overwrite an
earlier report. If lineups are missing, wait for teams to post them and rerun
the same command; the new observation receives a new identity.

Each successful run also writes a sibling `<primary-stem>.trace.json` sidecar.
It contains the already-computed raw feature rows, component scores, signed
home-minus-away contributions, probabilities, and explicit ranked/non-ranked
dispositions for reproducibility research. The sidecar is canonically hashed,
bound to the actual primary report and frozen model-card digests, and is not a
second prediction report. It remains preliminary research evidence only: it
does not prove historical as-of reconstruction, accuracy, calibration, value,
or a betting recommendation. A trace write failure is reported after any
already-written primary/readiness artifacts are preserved; old reports are
never retrofitted.

## Limits

The command never reads target-date outcomes into features, refits the card,
calculates ROI or CLV, validates profitability, or establishes betting
actionability. It does not perform v5.2 acquisition, fitting, source
finalization, or `SOURCES_READY`; it does not merge its rows into any v5.1,
v5.2, or formal prospective sample. Automatic polling, projected lineups,
official-starter verification, and formal T-45 admission are outside this MVP.

Task 9 implementation is ready, but the first successful live report is still
pending external Savant publication. No successful report has yet been
generated, and this guide does not claim live-smoke completion.

## Relationship to the multi-book market layer

This V1 Top 3 report remains a preserved model authority. It is not replaced
by, and does not itself contain, sportsbook prices. The separate
`experimental_multibook_market_plays` evaluator consumes this report's sealed
ML trace together with the sealed totals trace, participant evidence, and a
five-book normalized board. Its output is full-slate `PLAY`/`PASS` research
evidence, with no fixed Top 3 quota and no wagering guarantee. See
`docs/experimental_multibook_market_plays.md` for the offline collector
handoff and runbook.
