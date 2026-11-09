# Official Live Schedule Market Bridge Implementation Plan

> **For agentic workers:** Use subagent-driven-development and test-driven-development. Preserve the working tree; do not commit.

**Goal:** Connect existing authenticated MLB live schedule observations directly
to the market board without hand-writing a second official schedule receipt.

**Architecture:** Complete the already-approved September 1 variable-market plan,
Task 3 Step 3: read the LIVE_SCHEDULE receipt, manifest, raw payload and existing
sidecars through secure readers, parse the full slate, and bind every input.
Keep the original compact market schedule format readable. The public schedule
loader returns the same game identities for either source format; board V1/V2
intake and evaluator gates remain unchanged.

**Tech Stack:** Python 3.11, existing live-observation validators, secure_fs,
parse_live_schedule, canonical JSON, unittest.

## Scope and file ownership

Implementer owns `mlb_backtest/multibook_market_board.py`, the read-only loader
in `mlb_backtest/live_sources.py`, minimal board-authority registration in
`mlb_backtest/daily_multibook_market_plays.py`, scoped board/live schedule
tests, and the usage-guide bridge section. Preserve the previous
single-book V2 work already present in those files. Main owns this plan.

No network in code/tests. Actual public sportsbook observation is a separate
authorized operation and cannot supply fabricated quotes or source timestamps.
No collector framework, model fit, probability/threshold change, retrospective
rewrite, protected holdout, recovery authority, privileged access, Documents,
forbidden manifest, install, commit, push, merge or full exact suite.

The direct admitted-observation reader belongs in `live_sources.py` so that its
existing `_validate_raw_manifest`, `_validate_observation_completion` and claim
validation remain the semantic owner. LIVE raw payloads have no separate raw
`.sha256`: their digest is bound by receipt and manifest. Preserve manifest and
completion sidecars. Do not require the board's receipt to equal an ML receipt;
that would add a new temporal-coupling policy outside this connection repair.

## Task 1 — Authenticated dual-format schedule loading

- [x] Add a valid synthetic live-observation receipt/manifest/raw fixture using
  existing admission fixture helpers. Confirm that passing its exact receipt
  to `load_schedule_authority` fails on the current compact-only implementation.
- [x] Retain compact V1 schedule receipt+sidecar handling unchanged. Add a
  schema-directed live-observation branch with strict source `stats_api`, role
  `LIVE_SCHEDULE`, successful HTTP/observation status, query target date and
  exact original receipt/manifest/payload bindings. Reject unsupported forms.
- [x] Reuse the existing live validators and `parse_live_schedule` with the
  bound target date. Do not create a second parser or trust caller-supplied
  normalized games. Normalize to game_pk, away_team, home_team and start only;
  keep all official games, including started/finished rows for later PASS gates.
- [x] Make `ScheduleAuthority` expose the exact receipt SHA and all immutable
  input snapshots. Preserve compatibility for existing constructor consumers.
  Every relevant receipt, manifest, payload and required sidecar must survive
  into board persistence/daily evaluation rebind checks.

## Task 2 — Board reconstruction and operational compatibility

- [x] Have normalization use the public schedule authority rather than reparse
  compact-only JSON. Bind `schedule_receipt_sha256` to the actual receipt bytes.
- [x] During board loading verify the stored schedule receipt SHA equals the
  loaded authority SHA, not merely that the relative path matches. Preserve V1
  exact-five and V2 actual-one-through-five board semantics.
- [x] Confirm one captured-book and one unavailable-book test flows using live
  schedule inputs: normalization, persist, load, daily consumer and rebind.
  Retain all schedule games and existing zero-quote PASS behavior.
- [x] Reject incorrect source/role/date, unsuccessful observation, missing or
  modified receipt/manifest/raw/sidecar, malformed/duplicate game IDs, shifted
  schedule identity and mutation after load. Assertions must exercise actual
  behavior; no mock-only acceptance or altered source timestamps.
- [x] Confirm existing compact schedule V1 fixtures and board V1/V2 tests pass.
  Update the guide with the existing LIVE_SCHEDULE receipt path as accepted
  input, explaining that it is reused rather than freshly fetched.

## Task 3 — Independent review and handoff

- [x] Specification review followed by separate code-quality review. Scope only
  this delta; earlier minimum-book-coverage and V2 changes are approved baseline.
- [x] Controller runs focused tests, Ruff and diff-check. Suggested baseline:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
  tests.test_multibook_market_board tests.test_multibook_market_capture \
  tests.test_daily_multibook_market_plays tests.test_multibook_market_plays_e2e
```

- [x] After tests/reviews, exercise read-only loading of the existing Sep-6
  LIVE_SCHEDULE receipt and compare its 15 official identities with the sealed
  daily source. Do not claim a new pregame observation or fresh odds from it.
- [x] Report code completion separately from live sportsbook access. No current
  valid quote means no market-backed recommendation; no old quote is refreshed
  by changing its timestamp.

## Baseline

Existing isolated `codex/live-venue-timezone-fix` worktree, HEAD `3ab6cd4`.
The four focused modules passed 26 tests in 0.560 seconds before this task.
Pre-existing dirty-file hashes were recorded by the controller.

## Review checkpoint

Controller verification of the initial bridge passed 89 focused tests, Ruff,
and diff-check. Specification review found that the daily report retained only
ML/totals authorities, not the market board's full schedule evidence chain.
Complete the already-required daily pre-persistence rebind by retaining the
board authority and adding a read-only rebind method. Add actual daily
build/persist mutation rejection, single-unavailable-book PASS, mixed-slate
retention, and malformed/query/date tests before re-review. No evaluator or
recommendation policy changes are authorized by this repair.

## Verified final outcome

- Independent specification review PASS after the daily full-chain rebind and
  missing adversarial cases were added. Separate code-quality review PASS with
  no Critical, Important or Minor findings.
- Controller rerun: 95 focused tests passed in 22.732 seconds; scoped Ruff and
  repository diff-check passed.
- Read-only real September 6 receipt load: 15 games, all four normalized
  identity fields match the existing sealed daily report, seven evidence
  snapshots rebound successfully. Receipt SHA-256:
  `bcaddde7b2b4770df34980623bcedbf1bb7f1b7b163bb65f95ac75ba5d3b43f3`.
  This check made no network calls or writes.
- All 33 pre-existing dirty files outside the three overlapping task files
  retained their baseline SHA-256. The additional tracked delta is the
  minimal daily board-authority registration/serialization repair.
- Public FanDuel observation is preserved separately at
  `audit/market_observations/2026-09-06/20260906T202056Z-fanduel-public-mlb.json`.
  Only two of its six future events are September 6 events; four are Monday.
  This is NOT an admitted market capture or a recommendation. The book's two
  displayed start times are one minute later than the official schedule,
  which the existing exact-match rule rejects. Location verification was
  incomplete. Do not overwrite observed start times or refresh old quote
  timestamps to force admission.
- No model/evaluator threshold changes, commits, push, merge, full exact suite
  or privileged operations were performed.
