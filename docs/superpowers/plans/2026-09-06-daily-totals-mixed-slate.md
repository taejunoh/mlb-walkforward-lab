# Daily Totals Mixed-Slate Exclusion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Produce one sealed daily totals report that forecasts strict-prestart games and records every other authenticated live-schedule row as an exclusion.

**Architecture:** First authenticate and bind all parsed live IDs, then partition the sealed rows. Keep the V1 model receipt and evaluation pipeline unchanged; add a closed V1 report-game variant. The frozen September 1 market authority remains all-projected-only and rejects exclusions.

**Tech Stack:** Python 3.11, secure_fs, live_sources, experimental_totals, jsonschema, unittest.

**Spec:** docs/superpowers/specs/2026-09-06-daily-totals-mixed-slate-design.md

## Global Constraints

- Full LIVE_SCHEDULE binding IDs are canonical sorted integer IDs from every parsed requested-date row, before eligibility filtering.
- An eligible row has an accepted status and durable_at_utc strictly earlier than scheduled_start_utc.
- EXCLUDED rows have no forecast, feature, candidate, or residual fields, source_digest equals live_source.payload_sha256, and source_observed_at_utc equals the live binding durable_at_utc.
- Existing V1 reports and receipts remain valid. All-excluded reports still run existing history, window, walk-forward, selection, and minimum-row gates before output.
- Do not alter frozen September 1 overlay hashes/authority or make generalized market comparison part of this work.
- Use PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B. No real September 5 write, recovery suite, network, or commit.

---

### Task 1: Authenticate the full slate and create source exclusions

**Files:**
- Modify: mlb_backtest/experimental_totals_sources.py:65-75,295-313,316-520
- Modify: tests/test_experimental_totals_sources.py:26-110,585-632

**Interfaces:**
- Add ExcludedTargetGame(game_pk: int, official_date: str, scheduled_start_utc: str, away_team_id: int, home_team_id: int, venue_id: int, source_status: str, reason: str).
- Add excluded_target_games: tuple[ExcludedTargetGame, ...] to TotalsSourceBundle.
- Extend the existing _closed_target_rows helper to validate all bound IDs before partition and return eligible and excluded target tuples.

- [x] **Step 1: Write RED fixture tests.**

    def test_loader_binds_all_rows_before_partition(self):
        fixture = write_source_fixture(self.root, live_status="In Progress")
        bundle = load_totals_sources(self.root, fixture.report.relative_to(self.root))
        self.assertEqual(bundle.target_games, ())
        self.assertEqual(
            [(x.game_pk, x.source_status, x.reason) for x in bundle.excluded_target_games],
            [(2001, "In Progress", "PREGAME_STATUS_NOT_ACCEPTED")],
        )

    def test_loader_rejects_binding_missing_excluded_row(self):
        fixture = write_source_fixture(self.root, live_status="In Progress")
        _replace_report_binding(fixture.report, "LIVE_SCHEDULE", {"game_pks": []})
        with self.assertRaisesRegex(ValueError, "binding game_pks"):
            load_totals_sources(self.root, fixture.report.relative_to(self.root))

Extend only the test schedule helper for a two-row mixed slate. Add Warmup with durable_at_utc equal to its start and assert OBSERVATION_AT_OR_AFTER_START.

- [x] **Step 2: Run RED.**

    PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_experimental_totals_sources.ExperimentalTotalsSourceTests.test_loader_binds_all_rows_before_partition tests.test_experimental_totals_sources.ExperimentalTotalsSourceTests.test_loader_rejects_binding_missing_excluded_row -v

Expected: FAIL because current code rejects a nonpregame row before it checks the complete bound population.

- [x] **Step 3: Implement the minimal partition.**

Call _validate_schedule_population and parse_live_schedule once. Build a canonical sorted tuple of all game_pk values before any status/time branch. In _admit_binding compare that tuple to binding.game_pks before constructing TotalsSourceBundle. Create PREGAME_STATUS_NOT_ACCEPTED when source status is outside ACCEPTED_PREGAME_STATUSES. For accepted status call _require_pregame_boundary and create OBSERVATION_AT_OR_AFTER_START only for its normal comparison failure; malformed timestamps and binding errors remain failures. Validate eligible mappings through validate_target_games.

- [x] **Step 4: Run GREEN.**

    PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_experimental_totals_sources -v

Expected: PASS, including current binding, postgame, rebind, and strict-boundary tests.

### Task 2: Seal the additive V1 report contract and report construction

**Files:**
- Modify: mlb_backtest/daily_totals.py:82-92,314-388,418-526
- Modify: schemas/experimental_daily_totals_report.schema.json
- Modify: tests/test_daily_totals.py:106-205,276-290,445-550

**Interfaces:**
- Add _EXCLUDED_GAME_KEYS containing game identity, status, source_status, source_observed_at_utc, reason, source_digest, and data_quality_flags only.
- Change _game to take history and live source digests.
- EXCLUDED reasons are PREGAME_STATUS_NOT_ACCEPTED and OBSERVATION_AT_OR_AFTER_START. Flags are exactly ["ADMITTED_SCHEDULE", "EXCLUDED", reason].

- [x] **Step 1: Write RED semantic tests.**

    def test_excluded_row_is_live_bound_and_semantically_true(self):
        report = make_valid_report_payload()
        report["games"] = [excluded_row(
            report, source_status="In Progress",
            reason="PREGAME_STATUS_NOT_ACCEPTED",
        )]
        _recontent(report)
        self.assertEqual(validate_totals_report(report)["games"][0]["status"], "EXCLUDED")

    def test_excluded_row_rejects_mislabelled_reason_digest_and_forecast(self):
        report = excluded_report(source_status="Warmup",
                                 reason="PREGAME_STATUS_NOT_ACCEPTED")
        _recontent(report)
        with self.assertRaises(ValueError):
            validate_totals_report(report)
        report = excluded_report()
        report["games"][0]["source_digest"] = report["history_source"]["payload_sha256"]
        report["games"][0]["expected_total"] = 8.0
        _recontent(report)
        with self.assertRaises(ValueError):
            validate_totals_report(report)

The fixture supplies a live binding durable time at/after the Warmup start for the valid OBSERVATION_AT_OR_AFTER_START case. Assert schema accepts valid exclusion and legacy make_valid_report_payload unchanged.

- [x] **Step 2: Run RED.**

    PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_daily_totals.DailyTotalsContractTests.test_excluded_row_is_live_bound_and_semantically_true -v

Expected: FAIL because EXCLUDED is not yet a report variant.

- [x] **Step 3: Implement schema, semantic checks, build, and renderer.**

Add a third exact games oneOf schema variant. Its required fields are game_pk, official_date, scheduled_start_utc, away_team_id, home_team_id, venue_id, status, source_status, source_observed_at_utc, reason, source_digest, and data_quality_flags. In _game require live digest, exact flags, and reason/status/time consistency. In persist_daily_totals, after load_totals_sources_bound, compare the complete report exclusion set against admitted.excluded_target_games, including source_observed_at_utc equal to admitted.live_identity.binding.durable_at_utc. Preserve projected/unprojected history-source checks.

In build_daily_totals append one exclusion mapping per bundle.excluded_target_games after all existing history/window/walk-forward/minimum/receipt steps. Never call project_game for exclusions. Sort all rows with the current key. Add explicit PROJECTED, UNPROJECTED, and EXCLUDED renderer branches; exclusion output is EXCLUDED (reason; source_status) with em dashes.

- [x] **Step 4: Add and run GREEN mixed/all-excluded tests.**

    def test_all_excluded_has_no_forecast_and_keeps_history_gates(self):
        bundle.target_games, bundle.excluded_target_games = (), (excluded,)
        with patched_build_inputs(bundle), patch("mlb_backtest.daily_totals.project_game") as project:
            built = build_daily_totals(root, "2026-09-05", source_relative)
        project.assert_not_called()
        self.assertNotIn("expected_total", built.report["games"][0])
        self.assertEqual(built.report["games"][0]["source_digest"],
                         live_identity.payload_sha256)

    PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_daily_totals -v

Expected: PASS. Add a separate assertion that an invalid full binding causes no persistence/write attempt.

### Task 3: Preserve downstream market fail-closed behavior

**Files:**
- Modify: tests/test_daily_totals_market_overlay.py:300-390,831-902
- Inspect only: mlb_backtest/daily_totals_market_overlay.py:831-856,934-975,1100-1131

**Interfaces:**
- _fixed_model_gates remains the exact September 1 all-PROJECTED guard.
- No new market schema, policy, raw capture, snapshot, overlay row, or production authority hash is introduced.

- [x] **Step 1: Add a synthetic resealed-authority test.**

    def test_frozen_overlay_rejects_excluded_model_game_before_forecast_access(self):
        trace, _ = self._authority(self.root)
        self._replace_authority_report_game(
            self.root, trace, 822929,
            {"status": "EXCLUDED", "reason": "PREGAME_STATUS_NOT_ACCEPTED"},
        )
        with self.assertRaisesRegex(ValueError, "fixed model game projection differs"):
            load_and_validate_model_authority(self.root, trace)

The helper reseals only temporary fixture report/sidecar/trace and patches its private test authority spec. It omits expected_total and residual fields from that row; no real frozen constant changes.

- [x] **Step 2: Run overlay and focused regression tests.**

    PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_daily_totals_market_overlay -v
    PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_experimental_totals_sources tests.test_daily_totals -v
    /Users/taejunoh/.local/bin/python3 -m ruff check mlb_backtest/experimental_totals_sources.py mlb_backtest/daily_totals.py tests/test_experimental_totals_sources.py tests/test_daily_totals.py tests/test_daily_totals_market_overlay.py
    git diff --check

Expected: all commands exit 0. Report source/contract counts separately from the overlay boundary result; neither is probability validation.

## Plan self-review

Task 1 proves canonical full-population binding before strict partition. Task 2 defines and validates live-source semantic truth, preserves all-excluded history gates, and renders no false forecast. Task 3 ensures the frozen downstream market authority rejects rather than dereferences an exclusion. No broader market model or artifact rewrite is in scope.

## Execution ledger

- The user approved the previously presented mixed-slate exclusion design on
  2026-09-06. This plan records that approved scope; no commit or publication is
  authorized by this implementation step.
- Existing linked worktree verified: `codex/live-venue-timezone-fix` under LFG,
  not a submodule. Prior user changes remain in place. No installation needed.
- Baseline: source admission, daily totals, and frozen overlay modules ran
  **152 tests in 5.298 seconds, OK, exit 0** before new implementation.
- The separate prior recovery integration failure remains unresolved and is
  not rerun by this plan. It is not concealed in these focused test counts.
- Main review clarified that all-ID comparison must happen before eligibility
  branching; persistence must rebind the complete exclusion set and observation
  timestamps. Test sketches above are adapted to existing fixture APIs, with
  independent tests for digest, reason, timestamp, and forecast-field tampering.
- Tasks 1–2 share production interfaces and are assigned to one implementer.
  Task 3 owns only the frozen-overlay regression test and can proceed in
  parallel without altering market production code or policies.

### Final verification — 2026-09-06

- Implementer observed RED for the new source/report contract, then GREEN for
  58 source/report tests. The frozen-overlay regression initially failed at
  the absent EXCLUDED schema; after implementation it passed at the intended
  fixed-model all-PROJECTED rejection, not at a hash mismatch.
- Main ran the three focused modules: **162 tests in 5.042 seconds, OK**.
- Independent specification review identified four missing proofs (mixed
  build, readmission field tampering, all-excluded minimum gates, and invalid
  full-population no-write behavior). They were added, the daily-totals module
  passed **36 tests**, and independent re-review passed all four obligations.
- Final code-quality review: no critical or important issues. Documentation
  was aligned with the retained helper name and exact renderer label.
- Main final regression: **466 tests in 48.570 seconds, OK, exit 0**, covering
  live_sources, live_top3_service, daily_top3_e2e, experimental_totals_sources,
  daily_totals, daily_totals_market_overlay, sources, feature_engine,
  scoring_prep, prospective_ml_shadow, prospective_ml_shadow_report, prepare,
  pipeline, and locking. The one separately unresolved recovery test was
  explicitly removed from this selected run with an assertion that exactly
  one test matched; it was not run or declared passing.
- The first broad invocation used Python stdin and failed four multiprocessing
  tests because spawned children could not open `<stdin>`. The corrected
  invocation used `python3 -B -c` with the same unittest loader/filter, without
  changing production code or bypassing those tests; all 466 then passed.
- Final Ruff check of both changed source files and three changed test files:
  **All checks passed**. `git diff --check`: exit 0.
- Model card, frozen scoring preparation, frozen overlay production code,
  prior Sep-5 prediction report/trace and shadow summary retain their baseline
  SHA-256 values. No real prediction, model training, collection, settlement,
  evidence rewrite, commit, push, merge, or PR was performed.
- Implementation is retained in the existing LFG worktree. This verifies
  mixed-slate processing, not independent probability calibration or betting
  profitability. Actual daily evaluation requires that day's admitted inputs;
  old captures are not new pregame picks.
