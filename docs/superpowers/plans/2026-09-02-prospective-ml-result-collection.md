# Prospective ML Result Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven-development and test-driven-development. No commits, pushes, refits, live model/card changes or historical/holdout access.

**Goal:** Add one manual command connecting official MLB result capture to the existing immutable prospective settlement/report workflow.

**Architecture:** A narrow `prospective_ml_results` orchestration module consumes an authenticated existing report, reuses the existing bounded source transport and raw/receipt writers, and delegates all evaluation/persistence to `prospective_ml_settlement`. The report's explicit primary/head population is retained; no sample discovery or model execution is added.

**Tech Stack:** Python 3.11, existing source receipts and TrustedRoot helpers, unittest, Ruff.

---

## Task 1 — Outcome collection and orchestration

**Create:** `mlb_backtest/prospective_ml_results.py`

**Create:** `tests/test_prospective_ml_results.py`

**Narrow integration change:** `mlb_backtest/prospective_ml_settlement.py`
(`load_official_result_evidence` only), with focused regressions in
`tests/test_prospective_ml_settlement.py` if needed. Real-helper integration
tests exposed a mismatch between the producer's existing RawArtifact manifest
and the loader's legacy synthetic manifest. Admit the exact existing helper
format with strict bindings; preserve legacy validation and settlement math.

- [x] Write integration tests first using `tests/prospective_ml_fixtures.py` and the existing sealed source fixture. The new command accepts `collect --root ROOT --report RELATIVE_REPORT`; no arbitrary URL, clock override, date override, automatic retry or correction flag.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_prospective_ml_results -v` and record the expected missing-module/behavior failure before implementation.
- [x] Implement one collection API and CLI with an injectable existing HTTP transport for tests. Validate report content hash, file/sidecar integrity and referenced claims/heads before the first request. Derive snapshot paths only from validated primary claims.
- [x] Build the StatsAPI single-game schedule URL from each snapshot's `official_date` and `game_pk`. One bounded request per unresolved game; no broad daily collector, model imports or source re-score.
- [x] Persist the returned observation through the existing live source raw/manifest/receipt/completion helpers. Use a distinct result-capture query kind and observation identity so result evidence cannot collide with pregame evidence. Use existing helper-owned directories rather than introducing an incompatible second receipt format.
- [x] Load saved evidence through `load_official_result_evidence`; require exact snapshot game/date/team identity before any settlement write. Call `build_settlement` and `persist_settlement` only for `Final` state. Retain other states as pending with a visible reason.
- [x] Keep explicitly supplied terminal heads; do not refetch or automatically revise them. Before network, reject unresolved snapshots whose fixed first committed head already exists but was omitted by the supplied stale report. If a supplied report contains a nonterminal settlement head from a different/manual workflow, reject it with guidance to use the existing explicit `settle --prior` workflow rather than introducing automatic corrections.
- [x] Build and persist the next report from the original explicit primary population and the exact successfully committed heads. Print report path, per-game receipt/head paths and pending/failure reasons. Failure exit status is nonzero; partial durable outputs remain inspectable and no success is fabricated.
- [x] Verify behavioral cases: one Final home win produces a real settlement and Brier; away win orientation; live/postponed/no-final remains pending; malformed/non-200/redirect mismatch and wrong game/date/team fail; tampered report fails before network; supplied terminal heads cause zero HTTP calls; stale pending input rejects without a duplicate revision; partial progress and failed persistence preserve prior bytes.
- [x] Run the new tests together with existing prospective observation, settlement, persistence integrity, report integrity and integration tests; run Ruff on the new module/tests only.

## Task 2 — Operator instructions

**Create:** `docs/prospective_ml_result_collection.md`

- [x] Document the exact single command with the current LFG worktree and seven-game pending report; explain that its returned report is the next invocation's input.
- [x] Document Final-only settlement, non-final pending, explicit correction through the existing `settle --prior`, no automatic polling, failure exit/partial output handling, and no ROI/refit/promotion claim.

## Task 3 — Reviews and one live check

- [x] Independent specification review against the approved design; fix concrete misses using failing regressions.
- [x] After specification passes, independent code-quality review of integrity, network boundary, repeat/partial-failure behavior and tests; fix material findings.
- [x] Controller runs affected tests, Ruff and `git diff --check`; compare the pre-existing dirty files, original seven-game report and model/snapshot hashes.
- [x] Run the new command once with the existing report `reports/prospective_ml_observation/8f30a9e94dd3c22ecf2a2d62e962014b5f26a5d862c76fd470360cb19a5d0eeb.json`. Verify returned report/receipt/head references. Do not retry a failed live execution blindly.
- [x] Report actual settled/pending/failed counts in Korean. Preserve all evidence and leave branch uncommitted; do not create an automation.

## Controller baseline

Existing linked worktree: `codex/live-venue-timezone-fix`, HEAD `3ab6cd4c3ca878fccaaaf155a7adb8c33c4a185d`.
Before implementation, 18 existing settlement/report/integration tests passed.
The existing seven-game report SHA is `8f30a9e94dd3c22ecf2a2d62e962014b5f26a5d862c76fd470360cb19a5d0eeb`.
Existing settlement module SHA is `2e4dc41f04a0eca7e601d7d47cd2c27779fd676bd6eb20cf2479bc505f9c4f7b`.
Existing observer module SHA is `e5115dae280e0dd05675068d6ff79bf982aff7457a9e7a9b591ff9897660d255`.

The settlement SHA above is the pre-change baseline. Only the documented
manifest-compatibility boundary may change; prior evidence and prediction
bytes must remain unchanged.

## Review findings tracked

- Authenticate the helper receipt/completion, not only its raw manifest.
- Keep known durable receipt/head paths when either partial-report or final
  report persistence fails; a missing report must remain explicitly null.
- Reproduce both report-save branches and settlement-save failure in hermetic
  tests. A malformed HTTP response alone does not cover disk failure.

## Controller verification before live collection

- Implementation and specification review complete. The command and all
  behavioral cases in Task 1 are implemented; 14 new hermetic tests exercise
  the real persistence and settlement pipeline with only HTTP faked.
- Controller ran 92 tests across result collection, prospective observation,
  settlement, persistence/report integrity, integration and live source helpers:
  all passed in 26.818 seconds. Ruff and `git diff --check` passed.
- Final reviewed module bytes before live capture:
  `prospective_ml_results.py` SHA-256
  `d2d8ad220e9d5db3159be4af332d88eae1133da727482105a3dad4a4571f6886`;
  `prospective_ml_settlement.py` SHA-256
  `69b10194549a9cb734d617d5e0e538ec64cbd03e463fa9e6d9f1e8877c931361`.
- Code-quality review passed after the helper-only canonical URL check and its
  real-helper regression. Single live invocation exited 0: seven captures,
  zero Final outcomes, seven pending, unchanged report path. Exact receipt
  references are recorded in `docs/prospective_ml_result_collection.md`.
- Independent post-run read-only admission passed for all seven official
  receipts and the unchanged report. All seven fixed first heads are absent.
  Captures completed at 2026-09-02 23:30:40–45 UTC. Original seven claims,
  candidates, report/sidecar, trace, observer and pre-existing dirty tracked
  files match their pre-run hashes. HEAD remains unchanged. No automation,
  refit, commit or push was performed.
