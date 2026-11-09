# Single-book Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development and test-driven-development. Do not commit.

**Goal:** Accept one actual supported-book capture without four invented outcomes.

**Architecture:** New normalization emits `PROVISIONAL_MULTIBOOK_MARKET_BOARD_V2`
with one through five actual unique supported-book outcomes. Read and revalidate
V1 using its original exact-five contract; preserve source authentication and
all evaluator gates. The schema version participates in the existing digest.

**Tech Stack:** Python unittest, JSON Schema, existing canonical JSON and sealed
capture/board persistence. Existing isolated `live-venue-timezone-fix` worktree.

**Approved spec:** `docs/superpowers/specs/2026-09-06-single-book-intake-design.md`.

## Task 1 — Versioned intake and round-trip tests

Owned files: `mlb_backtest/multibook_market_board.py`,
`schemas/experimental_multibook_market_board.schema.json`,
`tests/test_multibook_market_board.py`,
`tests/test_daily_multibook_market_plays.py`,
`tests/test_multibook_market_plays_e2e.py`, and the current usage guide
`docs/experimental_multibook_market_plays.md`. Preserve its existing edits.
Only change another consumer if an observed compatibility failure requires it.

- [x] Add failing real-fixture tests: normalize and persist/reload one captured
  book; assert exactly one retained outcome, no placeholders, all schedule games
  retained and new schema literal V2. Add a daily evaluator round-trip with one
  real fixture capture and all other fixtures legitimately admitted.
- [x] Record RED with focused unittest; catch the old exact-five rejection as
  the expected failure, not an import/setup problem.
- [x] Add explicit normalization schema selection, defaulting to V2. Accept only
  V1 and V2. Keep supported-book validation and duplicate rejection, using:

```python
if schema == "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V1":
    if len(outcomes) != len(BOOKS):
        raise ValueError("V1 requires all supported-book outcomes")
elif schema == "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V2":
    if not 1 <= len(outcomes) <= len(BOOKS):
        raise ValueError("V2 requires one through five actual book outcomes")
else:
    raise ValueError("unsupported market board schema")
```

  Adapt collection/local names to existing functions; do not duplicate existing
  validation. Retain the existing deterministic supported-book ordering, but
  serialize only keys actually present. Do not make absent keys UNAVAILABLE.
  The keyword is `board_schema=BOARD_SCHEMA_V2`; define `BOARD_SCHEMA_V1` and
  `BOARD_SCHEMA_V2` with the exact version strings above.
- [x] In every load/persist reconstruction, pass the stored schema explicitly
  back into normalization; never rebuild a V1 record using the V2 default. No
  caller option may override a stored schema or source digest.
- [x] Update the JSON Schema to accept only V1/V2 with conditional counts: V1
  min/max 5; V2 min 1/max 5. Keep closed keys and existing field validators.
- [x] Cover empty, duplicate and unknown books; one unavailable-only outcome;
  two-to-five outcomes and order invariance; complete V1 round-trip; V1 fewer
  than five rejection; malformed schema and version/digest tampering. Assert
  zero quote coverage remains PASS and paired/stale/identity guards persist.
- [x] Change the existing `--capture-outcome` argument from `nargs=5` to
  `nargs="+"`; enforce the one-through-five count and retain rejection of repeated
  option occurrences. Update current CLI usage to show one outcome argument
  is sufficient. Explain omitted means not attempted and UNAVAILABLE means an
  actual failed attempt. Preserve historical specs/evidence.

Focused command:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
  tests.test_multibook_market_board tests.test_multibook_market_capture \
  tests.test_daily_multibook_market_plays tests.test_multibook_market_plays_e2e \
  tests.test_multibook_ml_evaluator tests.test_multibook_totals_evaluator
```

## Task 2 — Review and verification

- [x] Independent specification review: V1 remains strict and authentic; V2
  retains only actual supplied unique outcomes; daily consumer works with one.
- [x] Independent quality review after specification passes: check all
  reconstruction paths, canonical hashes, deterministic order and test meaning.
- [x] Controller reruns the focused command, affected-file Ruff, JSON Schema
  fixture validation and `git diff --check`. Record exact results.
- [x] Compare pre-existing dirty-file hashes except intentional usage-guide
  edits; preserve old snapshots and reports. Leave branch/worktree uncommitted.

## Exclusions and baseline

Do not add a network collector, official-schedule producer, provider timestamp
requirement, model fit, calibration claim, gate bypass or actual wager. No full
exact suite, recovery test, package installation, Documents/custody/production or
forbidden manifest access. No network, push, merge, PR, reset or cleanup.

Baseline on 2026-09-06: 14 capture/board/daily/E2E tests passed in 0.098 seconds.
The worktree already has user-owned source changes and immutable research data.

## Execution record

- Existing linked workspace confirmed; no new checkout, install or branch action.
- Implementer RED: the new one-book fixture reached the old `exactly five
  capture outcomes are required` rejection. No setup/import failure was used
  as feature evidence.
- Implementer GREEN: 28 focused tests passed in 0.184 seconds.
- Controller initial verification: the six-module focused command above passed
  28 tests in 0.227 seconds; affected Python files passed Ruff and diff-check.
- Hash comparison of pre-existing dirty files found changes only in the
  intentionally updated current usage guide. Independent reviews are pending.
- Specification review requested consumer round-trip, unavailable-only PASS,
  canonical-order and schema/version acceptance coverage. Tests were added
  without further production changes. Controller rerun: 33 tests passed in
  0.642 seconds; Ruff and diff-check passed. Specification re-review is pending.

## Final outcome

Specification re-review and independent final code-quality review both passed.
No critical or important issue remains in the scoped change. The final current
contract accepts one actual book in V2 and preserves strict V1 reconstruction.
The existing Sep-3 minimum-coverage-one policy was pre-existing and hash-unchanged;
it was not modified by this task.

Today's ML shadow report remains SHA-256
`9e61753ee37c4b9af23f88bfb14d6dcdbcf787a18154edd7f83ae3c75bba4068`, and
the totals report remains SHA-256
`b1d40cbb409563746220e999837c41687dd8689097951890d4ca1e5d0e6ed531`.
All pre-existing dirty-file hashes are unchanged except the intentionally updated
usage guide. The code remains uncommitted in the original isolated branch.

This completes intake-contract support, not actual network collection or model
betting validation. No source timestamps, unavailable outcomes or real quotes
were manufactured. The official schedule producer and live collection remain
separate follow-up work.
