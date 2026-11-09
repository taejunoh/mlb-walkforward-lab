# Live Pregame Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Explain missing pregame inputs truthfully, verify participant changes, and expose a provenance-bound per-game readiness table.

**Architecture:** Preserve the existing parser, eligibility gates, scoring formulas, V1 report and return tuple. Correct only missing-versus-conflict reasons, then render an additional diagnostic Markdown artifact from the same slate and validated report.

**Tech Stack:** Existing Python 3.11, unittest, experimental daily CLI, TrustedRoot report persistence.

**Spec:** docs/superpowers/specs/2026-08-31-live-pregame-readiness-design.md

## Global Constraints

Use `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix` as explicit cwd for every shell command. No Documents, sudo, installs, worker network/live runs, custody/production/Task12/14, forbidden manifest, push, merge or PR. Preserve all caches/reports/user files and the canonical original. Use apply_patch and PYTHONDONTWRITEBYTECODE=1. Model card, formulas, ranking, maximum three ranked results, V1 JSON key set, run return tuple and safe persistence remain unchanged. No new scheduler, odds, O/U, all-scored export or runtime retry. Workers do not spawn agents. Work sequentially because E2E tests are shared.

Baseline before code changes: `PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_live_sources tests.test_live_top3_service tests.test_live_top3_contracts tests.test_daily_top3 tests.test_daily_top3_e2e` passed 145 tests in 56.949s at source39b733b. Expected negative CLI diagnostics appeared; no unexpected failure.

### Task 1: Missing evidence versus true conflict, including observation transitions

**Ownership:** `mlb_backtest/live_sources.py`, `mlb_backtest/live_top3_service.py`, `tests/test_live_sources.py`, `tests/test_live_top3_service.py`, `tests/test_daily_top3_e2e.py`. Do not modify schemas, model files or report persistence.

- [ ] Add failing regression cases to the real parser/service path: empty observed pitcher list with known probable retains missing flags and no false conflict; missing probable retains `MISSING_SCHEDULE_PROBABLE`; both known IDs unequal retains conflict; missing on one side plus known mismatch on the other retains both; missing lineup remains accurately missing and unscored. Use existing fixture builders, literal expectations and no mocked scoring result for a test purporting to score.
- [ ] Extend the fixture E2E to run the real pipeline in the same temporary root with successive observations: missing participants → complete matching participants and nine-player lineups → final schedule probable changed. Assert zero probabilities first/last, actual preliminary probability in the middle, distinct observation/binding evidence, and no mutation of the first report. This catches stale live observations and accidental gate relaxation. Keep synthetic results out of real reports.
- [ ] Run the new focused tests before production changes and record the expected failures caused by false conflict/collapsed missing reasons.
- [ ] In `compare_probable_pitchers`, construct known valid pairs for comparison and restrict conflict to known unequal IDs:

```python
known_pairs = [
    (expected, observed)
    for expected, observed in pairs
    if type(expected) is int and expected > 0
    and type(observed) is int and observed > 0
]
```

Here `pairs` contains the existing away/home schedule probable and observed pitcher-list IDs. Keep missing schedule detection and existing parser flags. `any(expected != observed for expected, observed in known_pairs)` adds conflict. Only two valid equal pairs earn `PROBABLE_CONSISTENT`; no incomplete or ambiguous evidence may earn it.

- [ ] In `_candidate_failure_reasons`, compute the existing non-probable reason set before the starter gate. If any expected/observed starter is missing or invalid, return that existing reason set, or `{"INCOMPLETE_PREGAME_PARTICIPANTS"}` if it is empty. Do not collapse a known opposite-side mismatch out of that set. For fully known starter mismatch retain the current true-conflict rejection. Before structural lineup-invalid fallback, return existing `MISSING_AWAY_LINEUP`/`MISSING_HOME_LINEUP` evidence when present. Preserve all other eligibility checks and final revalidation.
- [ ] Run focused GREEN and these affected modules once: `PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_live_sources tests.test_live_top3_service tests.test_daily_top3_e2e`. Update only outdated missing-reason test expectations justified by the spec; do not change complete-fixture exact probabilities or golden hashes to hide regression.
- [ ] Run `git diff --check`, self-review, and commit only owned files as `Clarify missing pregame participant diagnostics`. Write report with RED/GREEN commands/output, commit, test results and concerns; return compact status.

### Task 2: Provenance-bound per-game readiness Markdown

**Ownership:** `mlb_backtest/daily_top3.py`, `tests/test_daily_top3.py`, `tests/test_daily_top3_e2e.py`. If rendering requires a new small module, ask controller before adding it. Do not change main JSON schema or scoring/service code.

- [ ] Add RED tests for a readiness companion file and renderer. Verify expected IDs/names, observed pitcher-list IDs without invented names, validated lineup counts, missing observation represented as unknown, Markdown escaping, report digest, observation/binding references and explicit research-only/not-score-readiness wording. Add secure-write tests for no overwrite and failed persistence while preserving existing report bytes.
- [ ] Add a deterministic helper:

```python
def _readiness_relative_path(payload):
    return _report_relative_path(payload, "md").with_suffix(".readiness.md")
```

- [ ] Add `render_live_pregame_readiness_markdown(slate, validated_payload) -> str` using the existing slate games/participant mappings and existing Markdown escaping. Expected values come from schedule `away/home_probable_pitcher_id` and `_name`; observed IDs come from `away/home_pitcher_list_id`; counts are `len(away/home_lineup)`, explicitly labeled validated. Missing participant object is unknown rather than 0. Do not equate complete participant inputs with a pick or complete model eligibility.
- [ ] For each game use the unique matching `LIVE_PARTICIPANTS` source binding's `durable_at_utc` and full `binding_sha256`; expose participant observation identity and source receipt/raw digests. If the game legitimately has no participant observation, show it as unavailable with its schedule/source identity. Include the primary report filename, canonical `content_sha256`, model-card SHA and target date. Show original participant snapshot time separately from later final-schedule rechecks. Use main report classification/reasons when present; otherwise explicitly state not listed in ranked output, without an invented score.
- [ ] Add private bound persistence after the existing primary report persistence in `run_daily_top3`: render using the same `slate` and validated payload; write under the same held `TrustedRoot` using existing `write_exclusive` and secure snapshot readback patterns; validate path/parent like existing reports. Failure must surface normally, not be silently swallowed. Keep the public return `(payload, json_path, markdown_path)` unchanged. Normal CLI output prints the deterministically derived readiness path; preserve the existing `--json-only` output behavior.
- [ ] Extend Task1's transition E2E to assert companion existence, truthful 0/9-or-unknown counts, expected/observed IDs, each observation's exact binding hash/time and report reference. Verify generated companions differ with observations and prior ones remain unchanged. Companion alone must not change primary JSON/Markdown happy-path goldens.
- [ ] Run focused GREEN and `PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_daily_top3 tests.test_daily_top3_e2e tests.test_live_top3_contracts`. Run `git diff --check`, self-review, commit owned files as `Add pregame readiness companion reports`, and report commands/results/commit/concerns.

## Controller completion

Review each task against its brief and diff; resolve findings without scope creep. Final whole-branch integration review after both tasks. Preserve the isolated branch and evidence; no merge, cleanup or push. Verify original source/model-card identity and old report hashes. Generate one fresh official real observation/model run after review to inspect the readiness artifact and distinguish real output from fixtures. Do not promise probabilities when inputs remain incomplete. No duplicate or automatic retry. Deliver paths, tested behavior and actual result.
