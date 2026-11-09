# Daily Evaluation Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** User-approved implementation (2026-09-05):
admit strictly pre-start `Warmup` observations and publish only date-matched,
reproducibly sealed internal daily-totals artifacts.

**Architecture:** A single shared pregame-status allowlist remains the only
status authority for live capture and ML revalidation. The totals source adapter
uses that same authority plus its own binding-time check. A pure totals-window
helper derives evaluation dates from the requested target and admitted history;
the receipt, digest, validator, and schema bind those values.

**Tech Stack:** Python 3.11 standard library, `unittest`, JSON Schema Draft
2020-12, existing secure-fs source admission and canonical JSON utilities.

**Execution constraint:** Execute the approved changes. Do not commit, publish,
push, merge, rewrite an artifact, or run a real daily evaluation while the
worktree contains user-owned changes.

---

## File map and ownership

- Modify `mlb_backtest/live_sources.py`: shared status admission and strict
  target-source temporal admission.
- Modify `mlb_backtest/experimental_totals_sources.py`: reuse status authority
  and require the bound live schedule to be strictly pre-start for every target
  row.
- Modify `mlb_backtest/daily_totals.py`: target-derived window helper, receipt
  validation, and build-time use of it.
- Modify `schemas/experimental_daily_totals_model.schema.json`: make
  warm-up date fields shape-valid instead of Sep-1 constants; Python remains
  the semantic authority.
- Modify `tests/test_live_sources.py`, `tests/test_live_top3_service.py`,
  `tests/test_experimental_totals_sources.py`, and `tests/test_daily_totals.py`:
  independent regression tests. Do not update golden values except where the
  later-approved receipt contract intentionally derives a different date value.
- Modify `docs/experimental_live_top3.md` and
  `docs/superpowers/specs/2026-09-01-provisional-daily-totals-design.md` only
  to link the successor contract after its design gate; do not edit existing
  artifacts.

### Task 1: Add `Warmup` as a narrow live pregame state

**Files:**
- Modify: `mlb_backtest/live_sources.py:82`
- Test: `tests/test_live_sources.py`
- Test: `tests/test_live_top3_service.py`

- [ ] **Step 1: Write the failing shared-status and initial-capture tests.**

  Add a test named
  `test_warmup_is_admitted_only_with_a_strictly_prestart_schedule_and_participant_receipt`.
  Build the existing posted-lineup fixture with `detailedState = "Warmup"`, a
  schedule durable timestamp one second before `gameDate`, and participant
  durable timestamp one second before `gameDate`; assert it returns a
  participant entry. In the same test's subcases use an exactly-equal schedule
  durable time and an after-start participant durable time and assert
  `ValueError` matching `at or after scheduled start`.

  Add `test_non_pregame_statuses_do_not_trigger_participant_capture`, with
  `In Progress` and `Final` payloads. Assert no boxscore URL is fetched and
  the game is absent from `slate.participants`.

- [ ] **Step 2: Run the narrow RED tests.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_live_sources.LiveSourceTests.test_warmup_is_admitted_only_with_a_strictly_prestart_schedule_and_participant_receipt \
    tests.test_live_sources.LiveSourceTests.test_non_pregame_statuses_do_not_trigger_participant_capture -v
  ```

  Expected: the Warmup admission assertion fails because the shared allowlist
  lacks `Warmup`; the non-pregame assertion already documents the fail-closed
  behavior.

- [ ] **Step 3: Implement the minimal shared status change.**

  Change the sole status authority to:

  ```python
  ACCEPTED_PREGAME_STATUSES = frozenset({"Scheduled", "Pre-Game", "Warmup"})
  ```

  Do not admit `abstractGameState == "Live"`, status code classes, or any
  additional literal. Keep every call to `_require_pregame_boundary` intact.

- [ ] **Step 4: Add final-observation and service-boundary regression tests.**

  Add `test_final_warmup_before_start_returns_final_observation` using a
  `Warmup` final schedule receipt one second before start; assert an actual
  `FinalGameObservation`. Add
  `test_final_warmup_at_or_after_start_is_an_observation_not_pregame_disposition`;
  assert `FinalGameScheduleDisposition.reason_codes ==
  ("OBSERVATION_NOT_PREGAME",)`.

  In `tests/test_live_top3_service.py`, add
  `test_warmup_final_schedule_remains_excluded_when_any_relevant_receipt_is_at_start`.
  Make both game snapshots `Warmup`, but create one required binding at the
  scheduled instant. Assert no feature-builder invocation and
  `OBSERVATION_AT_OR_AFTER_START` in the excluded reason codes. This proves the
  shared status expansion cannot bypass the aggregate timing gate.

- [ ] **Step 5: Run GREEN for the live contract.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_live_sources tests.test_live_top3_service tests.test_daily_top3_e2e -v
  ```

  Expected: all tests pass; existing Scheduled/Pre-Game probabilities and
  fixture hashes are unchanged.

### Task 2: Make totals source admission use the same pre-start contract

**Files:**
- Modify: `mlb_backtest/experimental_totals_sources.py:14-15,291-306,479-489`
- Test: `tests/test_experimental_totals_sources.py`

- [ ] **Step 1: Write failing totals-admission tests.**

  Add `test_loader_accepts_prestart_warmup_target_schedule` by creating the
  existing authenticated source fixture with `live_status="Warmup"` and a
  `LIVE_SCHEDULE` durable timestamp before the target start. Assert the
  returned target game is `Pregame`.

  Add `test_loader_rejects_warmup_target_schedule_durable_at_start` and
  `test_loader_rejects_started_target_schedule_even_before_start`. The first
  keeps `Warmup` but makes the bound schedule durable exactly equal to the
  target start; expect `ValueError` mentioning pregame timing. The second uses
  `In Progress` with a before-start receipt; expect `ValueError` mentioning
  `not pregame`. These cases independently prove status and clock are both
  required.

- [ ] **Step 2: Run the RED tests.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_experimental_totals_sources.TotalsSourcesTests.test_loader_accepts_prestart_warmup_target_schedule \
    tests.test_experimental_totals_sources.TotalsSourcesTests.test_loader_rejects_warmup_target_schedule_durable_at_start \
    tests.test_experimental_totals_sources.TotalsSourcesTests.test_loader_rejects_started_target_schedule_even_before_start -v
  ```

  Expected: Warmup is rejected by the adapter's duplicate literal allowlist;
  the at-start case exposes that the adapter currently has no binding-time
  guard.

- [ ] **Step 3: Implement source-adapter admission without a second status list.**

  Import `ACCEPTED_PREGAME_STATUSES` and `_require_pregame_boundary` from
  `mlb_backtest.live_sources`. Change `_closed_target_rows` to accept the
  already-validated `SourceBinding` (or perform the equivalent check in
  `_admit_binding` immediately after target rows are parsed), then require:

  ```python
  if game["status"] not in ACCEPTED_PREGAME_STATUSES:
      raise ValueError("live schedule is not pregame")
  _require_pregame_boundary(binding, game)
  ```

  Apply the timing check to every projected raw target game before returning
  any `TargetTotalGame`. Preserve exact payload/claim/receipt/completion/
  manifest validation and all source rebinds. Do not inspect game outcomes or
  call any live API.

- [ ] **Step 4: Run source-admission GREEN.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_experimental_totals_sources tests.test_daily_totals -v
  ```

  Expected: all authenticated-chain, mutation, and pregame tests pass; no
  write is made outside their temporary roots.

### Task 3: Replace fixed totals windows with a sealed target-derived contract

**Files:**
- Modify: `mlb_backtest/daily_totals.py:47-58,255-281,399-445`
- Modify: `schemas/experimental_daily_totals_model.schema.json:23`
- Test: `tests/test_daily_totals.py`

- [ ] **Step 1: Write pure window-contract RED tests.**

  Add `test_derive_totals_windows_preserves_sep1_baseline`:

  ```python
  self.assertEqual(
      derive_totals_windows(date(2026, 9, 1), date(2026, 3, 25)),
      {"warmup_start": "2026-03-25", "warmup_end": "2026-05-31",
       "development_start": "2026-06-01", "development_end": "2026-07-31",
       "final_start": "2026-08-01", "final_end": "2026-08-31",
       "league_days": 30, "venue_days": 60,
       "team_shrinkage_games": 10, "venue_shrinkage_games": 20},
  )
  ```

  Add `test_derive_totals_windows_advances_by_calendar_days_for_sep5`; with
  `D=2026-09-05` and admitted season start `2026-03-25`, assert development
  `2026-06-05..2026-08-04`, final `2026-08-05..2026-09-04`, and warm-up end
  `2026-06-04`. Add `test_window_validator_rejects_calendar_month_substitution`
  using a three-month-style range such as Jun-5..Aug-5; it must fail even if
  it looks plausible.

- [ ] **Step 2: Run window RED.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_daily_totals.DailyTotalsContractTests.test_derive_totals_windows_preserves_sep1_baseline \
    tests.test_daily_totals.DailyTotalsContractTests.test_derive_totals_windows_advances_by_calendar_days_for_sep5 \
    tests.test_daily_totals.DailyTotalsContractTests.test_window_validator_rejects_calendar_month_substitution -v
  ```

  Expected: fail because no target-derived helper exists and receipt validation
  still compares the fixed `_WINDOWS` mapping.

- [ ] **Step 3: Implement the pure helper and semantic receipt validation.**

  Replace `_WINDOWS` with a helper whose only date arithmetic is:

  ```python
  def derive_totals_windows(target: date, history_start: date) -> dict[str, object]:
      final_end = target - timedelta(days=1)
      final_start = target - timedelta(days=31)
      development_end = target - timedelta(days=32)
      development_start = target - timedelta(days=92)
      warmup_end = target - timedelta(days=93)
      if history_start.year != target.year or history_start > warmup_end:
          raise ValueError("insufficient admitted warmup history")
      return {
          "warmup_start": history_start.isoformat(),
          "warmup_end": warmup_end.isoformat(),
          "development_start": development_start.isoformat(),
          "development_end": development_end.isoformat(),
          "final_start": final_start.isoformat(),
          "final_end": final_end.isoformat(),
          "league_days": 30, "venue_days": 60,
          "team_shrinkage_games": 10, "venue_shrinkage_games": 20,
      }
  ```

  In `build_daily_totals`, derive `history_start` from the earliest admitted
  history row in the target year before calculating predictions; use the
  mapping for both walk-forward calls, the receipt `windows`, and
  `candidate_definition_sha256`. Reject an empty/no-target-year history before
  writes. In `validate_totals_model_receipt`, validate fixed 30/60/10/20
  parameters and exact D-relative development/final bounds plus contiguous
  warm-up end; do not reinstate a global constant. Preserve the existing
  100-row hard failures.

- [ ] **Step 4: Align schema shape validation.**

  In `experimental_daily_totals_model.schema.json`, replace only
  `warmup_start` and `warmup_end` `const` values with `{"format":"date"}`.
  Keep closed keys, all metric minima, candidate IDs, and numerical constants.
  Python validation remains responsible for D-relative semantic equality.

- [ ] **Step 5: Add build/no-leakage regression tests.**

  Add `test_build_uses_sep5_windows_and_same_date_source_only` with a
  `TotalsSourceBundle(target_date="2026-09-05", cutoff_date="2026-09-04")`.
  Patch `walk_forward_predictions` and assert calls use
  `("2026-06-05", "2026-08-04")` then
  `("2026-08-05", "2026-09-04")`; assert receipt/report cutoff is Sep-4 and
  its candidate-definition hash changes only through the recorded windows.

  Add `test_build_rejects_source_target_date_mismatch_before_prediction` and
  `test_build_rejects_missing_target_year_warmup_before_prediction`. Assert
  the predictor is not called and no output namespace exists. Add
  `test_existing_sep1_receipt_still_validates` using the current valid fixture
  unmodified. These tests prove no fallback to Sep-1 constants, no target-day
  outcome access, and no old artifact rewrite.

- [ ] **Step 6: Run totals GREEN and static checks.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_daily_totals tests.test_experimental_totals_sources \
    tests.test_daily_totals_market_overlay -v
  /Users/taejunoh/.local/bin/python3 -m ruff check \
    mlb_backtest/live_sources.py mlb_backtest/experimental_totals_sources.py \
    mlb_backtest/daily_totals.py tests/test_live_sources.py \
    tests/test_live_top3_service.py tests/test_experimental_totals_sources.py \
    tests/test_daily_totals.py
  git diff --check
  ```

  Expected: all focused regressions pass, including the overlay's sealed-model
  authority checks. Do not execute a real daily suite or publish an artifact in
  this task.

### Task 4: Preserve explicit non-doubleheader evidence (included in approval)

**Files:**
- Modify: `mlb_backtest/source_parsers/schedule.py:113-124`
- Modify: `mlb_backtest/feature_engine.py:1080-1140`
- Modify: `mlb_backtest/scoring_prep.py:321-324` if it currently discards the
  explicit false marker during preparation
- Test: `tests/test_sources.py`
- Test: `tests/test_feature_engine.py`
- Test: `tests/test_scoring_prep.py`

- [ ] **Step 1: Write parser and feature regression tests before changing marker semantics.**

  Add `test_schedule_preserves_explicit_non_doubleheader_marker` using a raw
  game with `doubleHeader: "N"` and positive `gameNumber`; assert
  `doubleheader_marker == "NO"`. Add a companion missing-field case asserting
  `doubleheader_marker is None`. Retain invalid-number rejection for both
  cases.

  Add `test_explicit_non_doubleheader_is_known_false_schedule_context` to the
  feature-engine fixture. With the same other schedule inputs, assert explicit
  `"NO"` produces `doubleheader=False`, no `doubleheader_unsupported` flag,
  and one additional supported schedule component; assert `None` remains
  neutral/unsupported. Assert `WEIGHTS` and the frozen model-card semantic
  digest are unchanged, so this is an evidence-completeness repair rather than
  a reweighting.

- [ ] **Step 2: Run the regression tests on current behavior.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_sources tests.test_feature_engine tests.test_scoring_prep -v
  ```

  Expected: the explicit-`N` preservation/known-false assertions fail; missing
  evidence and current learned-weight assertions retain their prior behavior.

- [ ] **Step 3: Implement only the three-state propagation.**

  Make `_doubleheader_marker` return `"NO"` for explicit `"N"` and `None`
  only when the field is absent. In the feature engine, recognize `"NO"` as
  known false and supported; recognize `GAME1`/`GAME2` as known true; leave
  `None` unsupported. Preserve `"NO"` in any `scoring_prep` row that carries
  schedule context. Do not modify `schedule_score`, model-card constants,
  `WEIGHTS`, training inputs, or historical artifacts.

- [ ] **Step 4: Run focused regression and impact checks.**

  Run:

  ```bash
  PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
    tests.test_sources tests.test_feature_engine tests.test_scoring_prep \
    tests.test_live_top3_service tests.test_daily_top3_e2e -v
  git diff --check
  ```

  Expected: only explicit-`N` fixtures may change scored/golden output; every
  missing-marker fixture and all model weights remain unchanged. Stop for
  review if any other output changes.

### Task 5: Document successor semantics and hand off safely

**Files:**
- Modify: `docs/experimental_live_top3.md`
- Modify: `docs/superpowers/specs/2026-09-01-provisional-daily-totals-design.md`
- Create: `docs/superpowers/specs/2026-09-05-daily-evaluation-readiness-design.md`
- Create: `docs/superpowers/plans/2026-09-05-daily-evaluation-readiness.md`

- [ ] **Step 1: Update user-facing constraints without changing historical claims.**

  Add one sentence to the live documentation: `Warmup` is accepted only when
  all durable evidence remains strictly before scheduled start; it is not a
  started-game exemption. Add one successor link to the totals design stating
  that later target dates use its sealed D-relative intervals, while the Sep-1
  artifact remains historical evidence.

- [ ] **Step 2: Verify documentation and change scope.**

  Run:

  ```bash
  rg -n 'Warmup|D-92|D-31|at or after scheduled start' \
    docs/experimental_live_top3.md \
    docs/superpowers/specs/2026-09-01-provisional-daily-totals-design.md \
    docs/superpowers/specs/2026-09-05-daily-evaluation-readiness-design.md
  git diff --check
  git status --short
  ```

  Expected: only the explicitly owned source, schema, test, and documentation
  files are modified; no `audit/`, `data/`, or `reports/` artifact is changed.


  Do not commit, publish, push, merge, run a real daily evaluation, or access
  any outcome source. The implementation agent must report the exact focused
  test, Ruff, and diff-check output before a reviewer decides the next step.

## Execution ledger — 2026-09-05

- User approved the complete design, including Task 4; no further design gate
  is required for these changes.
- Existing isolated workspace: `live-venue-timezone-fix`, branch
  `codex/live-venue-timezone-fix`, base HEAD `3ab6cd4`.
- Per the user's parallel-agent instructions, three independent file owners
  implement Tasks 1–2, Task 3, and Task 4 concurrently. Tasks 1–2 stay together
  because they share the status authority. No two implementers own one file.
- The orchestrator owns this ledger and documentation. Existing user edits in
  the live documentation and market-policy files are preserved.
- Each implementer runs focused RED/GREEN tests; separate specification and
  code-quality review precede the final combined regression run.
- Frozen ML card SHA-256 before implementation:
  `9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759`.
- No dependency installation, network collection, real daily evaluation,
  historical artifact rewrite, commit, push, or merge is part of this run.
- Tasks 1–3 and Task 5: implemented and independently reviewed.
- Task 4: parser/live feature and downstream closed-contract compatibility
  implemented; frozen candidate-preparation propagation intentionally deferred.

### Integration findings

- The unchanged overlay test fixture used the current capture clock with fixed
  Sep-1 scheduled starts. On Sep-5 the strict pregame guard correctly rejected
  it. A test-only, deterministic-clock correction is included; the production
  guard is not relaxed.
- `test_schedule_history_indexes_resumed_result_on_availability_date` depends
  on immutable recovery parent evidence absent from this checkout. Its fixture
  requires an explicitly admitted `MLB_V52_SOURCE_AUTHORITY_ROOT`; this work
  will not fabricate evidence, change its validator, or silently skip the test.
- The old frozen shadow candidate pins `scoring_prep.py` bytes. The initial
  marker-propagation repair caused 19 shadow test errors, including existing
  attachment/report loads. Only that new line and its new test were withdrawn:
  the file again matches its pinned SHA
  `b41cbafc4ad13d3cc9f4d1cd16de8a734f23e903b2ffaef246a0e85607e1ef6b`.
  All 36 shadow/report/scoring-prep tests then passed. Task 4 is partial: legacy
  candidate propagation is deferred to a separately validated freeze, while
  parser/live feature repair remains in scope. No receipt or pin was changed.
- Review identified three downstream closed marker validators in `prepare.py`,
  `pipeline.py`, and `locking.py` that must admit `NO` for shared-parser
  compatibility. Narrow acceptance/rejection tests accompany that correction.
- The end-to-end fixture explicitly supplies `doubleHeader: "N"`; its expected
  `doubleheader_unsupported` flags are updated only for that now-known evidence.

### Final verification

Fresh combined command on the final source/test state:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest \
  tests.test_live_sources tests.test_live_top3_service tests.test_daily_top3_e2e \
  tests.test_experimental_totals_sources tests.test_daily_totals \
  tests.test_daily_totals_market_overlay tests.test_sources \
  tests.test_feature_engine tests.test_scoring_prep \
  tests.test_prospective_ml_shadow tests.test_prospective_ml_shadow_report \
  tests.test_prepare tests.test_pipeline tests.test_locking
```

Result: **449 tests in 57.004 seconds: 448 passed, 1 error, 0 assertion
failures, 0 skips; command exit 1.** The sole error is the unchanged
`FeatureEngineTest.test_schedule_history_indexes_resumed_result_on_availability_date`
configuration dependency documented above. This is not an all-green suite or
permission to run a real daily evaluation. No test was disabled to hide it.

- Focused source/test Ruff check: `All checks passed!`.
- `git diff --check`: exit 0.
- Independent specification and quality review passed for the implemented
  scope, including the three downstream marker contracts and explicit-N E2E
  expectation changes. Review accepted the documented frozen-path deferral.
- Model card and restored scoring dependency hashes match the values above.
- The nine pre-existing market-policy/source/test changes retain their
  pre-implementation SHA-256 values. Earlier user changes in the live guide
  were preserved; this task appended successor documentation only.
- Today's Daily Top 3 JSON/trace and pending shadow summary remain byte-identical:
  `3911cadafbbd92c97171e1ff1ce6f415a6674049d0c53cda10ff62ce5ce5be00`,
  `f04bbf037374c1701037f8a33a9d7c22b614f6212a14584343e747cd5b7f3c79`,
  `5f8683602d6e40867b6639606f889d68de1989e9b81a51f2cb7ada00b3c2adf7`.
- Worktree and branch are retained without commit/push/merge. No real
  collection, daily evaluation, model fitting, or outcome settlement ran.

Remaining prerequisites are explicit: supply the correct immutable parent
evidence authority for the one integration test, and separately validate a
new/versioned frozen shadow profile before changing its preparation semantics.
Rolling totals diagnostics do not establish independent probability accuracy
or betting profitability.

### Continuation: configured recovery verification

The user requested continuation. The missing configuration was resolved to the
pre-existing, explicitly scoped LFG authority:
`/Users/taejunoh/Developer/LFG/.recovery/iCloud-Recovery-2026-08-26-160004/MLB Analysis/.worktrees/v52-r2-source-203.REHMfS/repo`.
Its eleven expected parent files concern 2019/2020/2021 schedule recovery, not
the protected H24 dataset. The test reads authenticated originals and builds
synthetic fixtures in temporary directories; it does not rewrite the authority.

The first configured single-test run failed with
`recovery disposition normalized digest differs`. Thus the earlier missing-root
error had masked a genuine shared-parser regression. Default historical
normalization must stay unchanged; only the live adapter opts into explicit
non-doubleheader preservation. The new compatibility regression test and
configured combined run must pass before a real daily totals evaluation.

The live-only opt-in correction passed 86 source/live tests and independent
review. With historical normalization restored, the configured recovery test
advanced past its digest check but encountered a separate ancestor-binding
failure. That check compares device/inode/mode/link-count for absolute path
ancestors. A single retry using a unique worktree-local `TMPDIR` isolates the
synthetic fixture from unrelated shared-temp directory churn without changing
the check or the source authority. The expensive test runs separately; the
other tests in the same 14-module scope are verified without duplicating it.

### Real daily-totals readiness finding

The existing Sep-5 source report has the required Sep-4 cutoff and admitted
history: 771 development rows and 419 final-diagnostic rows. The Sep-5 totals
output namespace is absent. However, its complete bound live schedule includes
games `823577` and `823823` as `In Progress`, with receipt `21:41:40Z` after
their `20:10Z` starts. The current totals loader rejects the entire schedule;
it also requires admitted target IDs to equal all bound schedule IDs. There
is no supported CLI selector that can evaluate only the remaining games.

A fresh full-slate capture would still contain started games and is not a fix.
The proposed follow-up is full-schedule authentication followed by sealed
`EXCLUDED` rows for ineligible games and projection only for strict-prestart
eligible rows. This needs an explicit source/report-contract extension, not
edited old reports or weakened timing checks. It was presented for user
approval; no such contract change or real daily-totals command has run yet.

### Continuation verification outcome (supersedes earlier totals)

The final corrected source/test state passed **452 tests in 52.730 seconds,
exit 0**, across the same fourteen modules above with exactly the named heavy
recovery integration test separated explicitly, not skipped or disabled.
The live-only parser opt-in preserves historical normalization by default;
the E2E golden was checked against unchanged numerical outputs and dispositions.

A diagnostic invocation of the recovery test identified a changed ancestor
tuple at `live-venue-timezone-fix`: device/inode/mode remained unchanged while
directory link count changed from 20 to 21. This identifies the observed field,
not the process responsible, and does not establish evidence-file tampering.
The worktree-local temporary-directory remedy was therefore insufficient.

The final standalone invocation used a unique `TMPDIR` directly beneath
`/Users/taejunoh/Developer/LFG`, outside the active worktree ancestry, with the
explicit recovery authority above and no concurrent test invocation. It still
failed after **456.591 seconds, exit 1**, during final authority reauthentication:
`ValueError: repository root ancestor path binding changed`.
This last invocation did not instrument the changed component, so it cannot be
attributed to the previously observed worktree link count. Moving the fixture
outside that ancestry is not a proven resolution. No further retry was started
and no authentication check was weakened.

Current verified outcome: **452 passed, 1 integration-test error across two
separate runs**. The recovery leg remains unresolved; this is not an all-green
suite or a completion claim. A next diagnostic must identify the exact ancestor
and before/current identity tuples for the final configuration before choosing
another remedy. Historical data or trust pins must not be rewritten to pass it.

Final read-only SHA-256 checks reconfirmed the model card, frozen
`scoring_prep.py`, today's existing Daily Top 3 report/trace, and pending shadow
summary against the hashes recorded above. `git diff --check` passed. No actual
daily-totals output, market collection, settlement, commit, push, or merge was
performed. The mixed-slate `EXCLUDED` contract proposal remains unimplemented
pending the user's design choice.
