# Market Event Time Matching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Admit V3 market boards that deterministically bind a full-UTC source event to one official game by exact start or a unique ordered-team match within 60 seconds, with replayable source evidence.

**Architecture:** Keep V1 and V2 byte/layout/loading behavior exact-only. Make V3 the `normalize_market_board` default; use the existing `canonical_utc_ns` integer parser to produce an event-match audit and build quotes only from that mapping. V3 loading reopens every embedded capture outcome and raw capture, validates bindings/audit/quotes against those authenticated bytes, and retains all snapshots so the existing daily final rebind detects later mutation.

**Tech Stack:** Python 3 `unittest`, JSON Schema Draft 2020-12, existing `TrustedRoot`/`RegularFileSnapshot` sealed-file helpers, canonical JSON/SHA-256.

**Approved spec:** `docs/superpowers/specs/2026-09-06-market-event-time-matching-design.md`.

---

## File structure and bounded ownership

- Modify: `mlb_backtest/multibook_market_board.py` — V3 constants, exact-nanosecond matcher, audit/source-binding construction, V3 load/rebind validation, and V3 default.
- Modify: `schemas/experimental_multibook_market_board.schema.json` — closed V3 top-level, binding, and audit variants while preserving V1/V2 variants verbatim.
- Modify: `tests/test_multibook_market_board.py` — synthetic V3 normalization, schema, persistence/load/rebind, and tamper coverage.
- Create if useful: `tests/test_market_event_time_matching.py` — scoped V3 behavioral cases using existing synthetic fixtures.
- Modify: `tests/test_multibook_market_plays_e2e.py` and `tests/test_daily_multibook_market_plays.py` only for the V3 daily-consumer/final-persistence assertions.
- Modify: `docs/experimental_multibook_market_plays.md` — document V3 matching/audit and unchanged pregame/freshness semantics.

Do not modify `mlb_backtest/multibook_market_capture.py`, raw/outcome schemas, either evaluator, market-play report/trace schemas, or policy values. Preserve all unrelated dirty worktree edits and synthetic/real evidence.

### Task 1: Implement and prove the V3 sealed matching path

**Files:**
- Modify: `mlb_backtest/multibook_market_board.py`
- Modify: `schemas/experimental_multibook_market_board.schema.json`
- Test: `tests/test_multibook_market_board.py`
- Test: `tests/test_multibook_market_plays_e2e.py`
- Test: `tests/test_daily_multibook_market_plays.py`
- Modify: `docs/experimental_multibook_market_plays.md`

- [x] **Step 1: Add failing V3 fixture tests before production changes.**

  Extend the existing `BoardTests` helpers to produce distinct canonical raw captures and schedule games. First assert the literal V3 schema or missing tolerant matching without importing a nonexistent constant; an import error is not sufficient RED evidence. Change existing legacy assertions to pass `board_schema=BOARD_SCHEMA_V1` or `board_schema=BOARD_SCHEMA_V2` explicitly; reserve the default call for V3. Add focused tests whose assertions make the intended contract observable:

```python
built = normalize_market_board(root, schedule, outcomes)  # default V3
assert built.board["schema"] == "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V3"
assert built.board["games"][0]["scheduled_start_utc"] == official_start
assert built.board["event_match_audit"] == [{
    "book_id": "FANDUEL", "source_event_id": "fd-1",
    "raw_capture_relative": raw_relative, "raw_capture_sha256": raw_sha,
    "source_scheduled_start_utc": source_start,
    "official_scheduled_start_utc": official_start,
    "delta_nanoseconds": 60_000_000_000,
    "game_pk": 1, "rule": "UNIQUE_ORDERED_TEAMS_60S",
}]
```

  Cover: V1/V2 unchanged exact acceptance and a V3 exact `delta_nanoseconds == 0`/`rule == "EXACT"`; each observed `+60s` MIN–CWS and WSH–LAD style event; `-60s`; `+60s + 1ns` and `-60s - 1ns`; a fraction-bearing timestamp proves nanosecond precision; an official target-date slate game whose UTC start is the next day; swapped teams; clock-only/noncanonical starts rejected by the already-supported raw contract; a same-team doubleheader rejects fallback even when one game is within the window, while one unique exact doubleheader match succeeds; unmatched event; two source events mapped to one `(book, game)`; repeated `(book, source_event_id)`; V3 one-to-five distinct books and unavailable/zero-quote all-PASS behavior.

  Add persistence/load tests that mutate one thing at a time after persistence: capture-outcome bytes, outcome sidecar, raw bytes, raw sidecar, an embedded outcome/binding/audit/quote field with a recomputed board digest, and schedule evidence. Assert `load_market_board`, `MarketBoardAuthority.rebind`, or `persist_market_plays` rejects respectively. Add a successful V3 round-trip/daily build-persist case and assert that at `official_start` a quote whose source display start is `official_start + 60s` still yields `GAME_STARTED`; verify provider `captured_at_utc` remains the original value and the existing 300-second boundary remains unchanged.

- [x] **Step 2: Run the new board tests to record RED.**

  Run:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
  tests.test_multibook_market_board -v
```

  Expected: failures identify missing `BOARD_SCHEMA_V3`/V3 default/audit or V3 loading; do not accept a fixture/setup failure as the red result.

- [x] **Step 3: Define the V3 representation and deterministic matcher in `multibook_market_board.py`.**

  Add `BOARD_SCHEMA_V3 = "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V3"` and include it in `BOARD_SCHEMAS`; change only the default argument of `normalize_market_board` and CLI admission to V3. Keep explicitly supplied V1/V2 behavior exactly as it is. Introduce closed, canonical V3 projections named `capture_outcome_bindings` and `event_match_audit`:

```python
# one per supplied outcome, sorted by SUPPORTED_BOOK_IDS order
{
  "book_id": book_id,
  "capture_outcome_relative": outcome.relative.as_posix(),
  "capture_outcome_sha256": hashlib.sha256(outcome.snapshot.contents).hexdigest(),
  "capture_outcome_sidecar_sha256": hashlib.sha256(outcome.sidecar_snapshot.contents).hexdigest(),
}

# one per AVAILABLE raw event, sorted by (book order, source_event_id, game_pk)
{
  "book_id": book_id, "source_event_id": event_id,
  "raw_capture_relative": raw_relative, "raw_capture_sha256": raw_sha,
  "source_scheduled_start_utc": source_start,
  "official_scheduled_start_utc": official_start,
  "delta_nanoseconds": canonical_utc_ns(source_start) - canonical_utc_ns(official_start),
  "game_pk": game_pk, "rule": "EXACT" | "UNIQUE_ORDERED_TEAMS_60S",
}
```

  Implement a V3-only `_match_event_v3(event, games)` using current ordered `_same_teams` and `canonical_utc_ns`; never round or use `datetime`/float arithmetic:

```python
exact = [g for g in games if _same_teams(event, g) and _same_start(event, g)]
if len(exact) == 1:
    return exact[0], "EXACT", 0
if exact:
    raise ValueError("market event exact match is ambiguous")
team_games = [g for g in games if _same_teams(event, g)]
if len(team_games) != 1:
    raise ValueError("market event does not map uniquely to the official slate")
delta_ns = canonical_utc_ns(event["scheduled_start_utc"]) - canonical_utc_ns(team_games[0]["scheduled_start_utc"])
if abs(delta_ns) > 60 * 1_000_000_000:
    raise ValueError("market event start is outside 60-second tolerance")
return team_games[0], "UNIQUE_ORDERED_TEAMS_60S", delta_ns
```

  For V3, map every available raw event once before quote assembly; reject duplicate source-event identity and duplicate `(book_id, game_pk)` mapping; use the map (not `_same_start`) to project paired ML/totals quotes. Emit every official game once using its official fields, even with no quotes. Retain source-event time only in the audit. Include outcome and raw snapshots/sidecars in `BuiltMarketBoard.input_snapshots` as now, and preserve deterministic input/output ordering.

- [x] **Step 4: Make V3 schema and load validation reconstruct the evidence chain.**

  In the board JSON Schema, add a third `oneOf` branch with `schema: {"const": "PROVISIONAL_MULTIBOOK_MARKET_BOARD_V3"}`, one-through-five outcomes, required `capture_outcome_bindings` and `event_match_audit`, strict closed field sets, SHA-256 patterns, an integer `delta_nanoseconds`, and the two literal rule values. Define the two fields at the enclosing property level only so V3 can use them, then make the V1/V2 branches reject either V3-only field; V3 alone has the two new fields. Keep V1/V2 required keys and cardinality unchanged.

  In `load_market_board`, branch by stored schema. For V3, require the exact V3 top-level set, validate sorted/distinct bindings and audit identities, reopen each `capture_outcome_relative` through existing `load_capture_outcome`, compare its embedded payload, source book, byte SHA-256, sidecar SHA-256, and V3 outcome binding, then regenerate the V3 event map/audit from its authenticated raw snapshot and the supplied `ScheduleAuthority`. Require byte-for-byte canonical projection equality for the stored audit and validate every quote is the sole quote projection of its authenticated mapped event (including event ID, capture timestamp, prices, raw path/digest, line/odds). Reject a missing source, altered content/sidecar, stale embedded outcome, audit mismatch, absent/extra quote, or non-deterministic order. Include all reopened outcome/raw snapshots and sidecars in the returned authority chain so `MarketBoardAuthority.rebind()` and `daily.persist_market_plays()` rebind them at final persistence. Leave the V1/V2 loader path unchanged.

- [x] **Step 5: Update the usage guide without widening product claims.**

  State that new default boards are V3; raw capture source start remains original, board games use official start, fallback requires exactly one ordered-team official target-date candidate and absolute delta `<= 60s`, and a doubleheader disables fallback. Describe the two V3 evidence collections and that load/final persistence reopens outcome/raw/sidecar evidence. Explicitly retain: source provider capture timestamps and 300-second freshness are not changed, official start controls `GAME_STARTED`, V1/V2 remain exact-only, no location/executability/calibration claim is added.

- [x] **Step 6: Run focused GREEN verification.**

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
  tests.test_multibook_market_board tests.test_multibook_market_capture \
  tests.test_daily_multibook_market_plays tests.test_multibook_market_plays_e2e \
  tests.test_multibook_ml_evaluator tests.test_multibook_totals_evaluator -v
python3 -m json.tool schemas/experimental_multibook_market_board.schema.json >/dev/null
git diff --check
```

  Expected: all named tests pass, schema parses, and no whitespace errors. Do not run the full exact suite, install packages, collect network data, create/modify evidence outside synthetic test roots, or commit.

### Task 2: Independent review and handoff verification

**Files:**
- Review only: all Task 1 files and `docs/superpowers/specs/2026-09-06-market-event-time-matching-design.md`

- [x] **Step 1: Perform a fresh specification review.**

  Verify each approved contract against implementation/tests: V3-only tolerance/default, unique exact versus unique ordered-team fallback, inclusive exact nanoseconds at both boundaries, whole official target-date slate (including next-UTC-day members), raw/official time retention, unique audit/mapping identities, capture/outcome/raw/sidecar reopening, source snapshot rebind, and no changes to raw/outcome/evaluator/report/policy schemas. Flag any mismatch with exact file/line and correct it before proceeding.

- [x] **Step 2: Perform an independent quality review.**

  Inspect the V3-to-quote projection and reload path for a bypass in which a recomputed board digest could point quotes, audit, or embedded outcomes at unauthenticated bytes. Confirm V1/V2 did not begin reopening/rebuilding under V3 rules; inspect canonical sort keys, checked integer arithmetic, all snapshot inclusion, and final `persist_market_plays` rebind behavior. Correct only scoped V3 issues.

- [x] **Step 3: Re-run the Task 1 focused command and report the evidence.**

  Report exact test count/duration and `git diff --check` result, list intentional files only, and leave the worktree uncommitted. No further user approval, network work, full suite, push, merge, or protected-path access is required.

## Acceptance matrix

| Requirement | Proof |
| --- | --- |
| Legacy V1/V2 exact-only | Existing V1/V2 tests plus explicitly passed legacy schemas |
| V3 exact and ±60 seconds | Synthetic V3 audit/rule/delta tests, including fractional nanoseconds |
| No tolerant ambiguity | Doubleheader, swapped-team, unmapped, duplicate identity/mapping tests |
| Official time remains decisive | Board official time assertion and daily `GAME_STARTED` test at official start |
| Evidence remains replayable | Load/rebind/final-persist and each source/outcome/sidecar/audit tamper test |
| Existing policy unaffected | Zero quote PASS and 300-second freshness tests; evaluator/policy files untouched |

## Exclusions

No raw capture or capture-outcome schema change; no model, probability, location, pricing, evaluator, report/trace, coverage-policy, collection, or historical-evidence rewrite. No commit, install, network, full exact suite, protected/custody/production/recovery/Documents access, or access to the forbidden manifest.

## Execution checkpoints

- Baseline: 37 focused tests passed in 1.351 seconds in the existing isolated
  worktree, HEAD `3ab6cd4c3ca878fccaaaf155a7adb8c33c4a185d`.
- Behavioral RED: default normalization returned V2 instead of the required
  literal V3 schema before implementation.
- Initial controller verification: 54 focused tests passed in 1.069 seconds;
  scoped Ruff, schema JSON parsing and diff-check passed. Existing daily ML,
  totals and public observation bytes were unchanged.
- Initial specification review found V3 load cardinality omitted from the
  one-through-five condition and missing approved integration/adversarial
  tests. These findings must be repaired and independently re-reviewed before
  completion; passing the initial focused run is not completion evidence.

## Final verification and disposition

- Specification review PASS after the V3 zero-outcome load regression and all
  required V3 evaluator/load/rebind assertions were corrected.
- Independent quality review PASS: no Critical or Important findings; one
  stale V2-default comment was identified for V3 wording cleanup.
- Controller focused matrix (including capture and live-schedule bridge):
  61 tests passed in 1.195 seconds. Scoped Ruff and diff-check passed.
- The saved FanDuel observation, daily ML report and daily totals report
  retained their original SHA-256 values. No timestamps or source event
  times in those artifacts were rewritten, and no observation note was
  admitted automatically as a market capture.
- V3 now permits the approved bounded match with source replay; it does not
  establish a fresh/current executable sportsbook price, location validation,
  model calibration or a betting recommendation.
- Leave the existing worktree and all changes uncommitted and unmerged.
  No full exact suite, network collection or protected-path operation occurred.
