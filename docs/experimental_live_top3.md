# Experimental Live Daily Top 3 — Operator Guide

> **RESEARCH ONLY · NON-OFFICIAL SAMPLE · PROFITABILITY NOT VALIDATED**

This command is a local research view labeled `v5.1-EXPERIMENTAL-LIVE`. It is
not a betting recommendation, a production service, a formal prospective
validation ledger, a continuation of the v5.1 retrospective sample, or a v5.2
result.

## Run the command

From the canonical worktree:

```bash
cd "/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix"
PYTHONPATH="/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix" python3 -B -m mlb_backtest.daily_top3 --root "/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix"
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
# Prospective ML observation (not activated)

The ML-first observer is an offline, research-only recorder. It accepts an
already sealed Top-3 ML trace and records every scored candidate without
waiting for market or totals evidence. Missing market evidence is stored as
`NOT_EVALUATED`; it is never a PASS, price, or PLAY. It does not change the
existing model, rank, PLAY gate, staking, or promotion policy.

Only a new post-activation O_EXCL primary claim is prospective. Existing or
historical artifacts are preserved as diagnostic/reference evidence and are
never backfilled into a primary claim.

Run a prospective snapshot with the execution clock only (there is no date or
clock override):

```sh
python -m mlb_backtest.prospective_ml_observation snapshot --root ROOT --ml-trace RELATIVE_TRACE
```

The observer writes content-addressed candidates under
`audit/prospective_ml_observation/candidates/` and a single exclusive primary
claim for the first pre-start eligible game/model pair. A later candidate stays
diagnostic. The separate settlement command admits an existing official
StatsAPI receipt and raw payload; it does not fetch, score, or call a collector:

```sh
python -m mlb_backtest.prospective_ml_settlement settle --root ROOT --snapshot RELATIVE_SNAPSHOT --result-receipt RELATIVE_RECEIPT
python -m mlb_backtest.prospective_ml_settlement settle --root ROOT --snapshot RELATIVE_SNAPSHOT --result-receipt RELATIVE_RECEIPT --prior RELATIVE_HEAD
python -m mlb_backtest.prospective_ml_settlement report --root ROOT --primary-claim RELATIVE_CLAIM [--settlement-head RELATIVE_HEAD]
```

Calibration reports retain pending/excluded counts and report `p_home` Brier
and clipped log loss (the existing `[.001, .999]` metric convention), plus
pick accuracy and Wilson descriptive bounds. There is no ROI, O/U, or
market-settlement calculation in this experiment.

Failed writes preserve diagnostic sidecars or record files; file existence is
not proof of a successful operation. A final source-revalidation failure raises
an error and returns no successful result. Settlement reports admit only valid
committed heads and revalidate their cited inputs; never discover samples by
globbing leftover files or delete failed evidence to retry.

### 2026-09-05 pregame and schedule successor

The approved [daily-readiness contract](superpowers/specs/2026-09-05-daily-evaluation-readiness-design.md)
accepts `Warmup` only while every required durable receipt is strictly before
scheduled start; it is not an exemption for a started game. The source may not
identify an early actual start, so this remains a research-only scheduled-time
gate. Explicit schedule `doubleHeader: "N"` is preserved as known false rather
than missing evidence; missing markers remain unsupported. This repairs source
completeness without changing learned weights or rewriting past observations.

This preservation is opt-in for the live schedule adapter. Historical/default
schedule parsing retains its original normalization so immutable recovery
receipts continue to validate; a live fix must not rewrite historical digests.

The experimental shadow candidate pins its scoring dependency bytes, including
`scoring_prep.py`. That file is deliberately kept byte-identical so existing
attachments, paired reports, and the frozen candidate remain readable. The new
`"NO"` marker is not propagated by this legacy candidate-preparation path;
changing it requires a separately validated freeze. A source-completeness
repair is not automatic candidate promotion.
