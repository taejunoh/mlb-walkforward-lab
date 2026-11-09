# Single-book Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the user's chosen one-fresh-book minimum without weakening unrelated eligibility gates.

**Architecture:** Update the existing fixed policy and its two consumers together. Keep immutable output artifacts and source-specific accounting unchanged. No new abstraction or historical loader is needed.

**Tech Stack:** Python unittest, JSON Schema, Markdown.

**Spec:** `docs/superpowers/specs/2026-09-03-single-book-coverage-design.md`.

## Global Constraints

Work only in the existing LFG live-venue-timezone-fix worktree. Preserve all pre-existing edits, particularly docs/experimental_multibook_market_plays.md. No network, package installation, frozen evidence edits, training, full exact suite, sudo, custody/production access, commits, push, merge, reset, or pruning. Do not access /tmp/r4-readonly-tree-manifest.txt. Do not spawn further agents.

### Task 1: Align fixed policy and prove one-book behavior

**Files owned by implementer:**
- `mlb_backtest/market_play_contracts.py`
- `mlb_backtest/multibook_ml_evaluator.py`
- `mlb_backtest/multibook_totals_evaluator.py`
- `schemas/experimental_multibook_market_plays_policy.schema.json`
- `tests/test_multibook_ml_evaluator.py`
- `tests/test_multibook_totals_evaluator.py`
- `tests/test_market_play_contracts.py`
- `docs/experimental_multibook_market_plays.md`

- [ ] Add behavior regressions using existing valid fixtures: one supported paired quote must satisfy coverage and permit PLAY when all other gates pass, for both ML and totals. The original coverage three must fail these tests. Retain meaningful zero-book and stale/duplicate/identity/residual rejection checks.
- [ ] Run focused tests before implementation and record expected RED assertions, not import/setup errors.
- [ ] Apply these exact policy changes, leaving other values unchanged:

```python
# market_play_contracts.POLICY field
"minimum_book_coverage": 1
# Both existing evaluator constants
MIN_BOOK_COVERAGE = 1
```

```json
"minimum_book_coverage": {"const": 1}
```

- [ ] Update current docs from at least three fresh supported books to at least one, explicitly retaining paired-quote validation and unchanged other gates. Avoid editing historical design/evidence records.
- [ ] Run:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_multibook_ml_evaluator tests.test_multibook_totals_evaluator tests.test_market_play_contracts tests.test_daily_multibook_market_plays tests.test_multibook_market_plays_e2e -v
git diff --check
```

- [ ] Self-review and report exact RED/GREEN evidence and touched files. Do not commit.
- [ ] Controller reviews task diff and runs proportionate focused verification before completion.

## Preflight

| Item | Compatibility check |
| --- | --- |
| Task 1 policy/consumer/schema | All specify coverage one; price freshness and other gate values are unchanged. |
| Task 1 tests | One paired book satisfies coverage; zero does not. Fixtures must meet non-coverage gates independently. |
| Task 1 historical data | No output/report rewrite. Current validator remains current-policy-only; archived JSON is not reinterpreted. |

The user explicitly selected one book in this conversation; this is the approved bounded design. Existing isolated worktree and dependencies are reused. No additional approvals or branch operations are needed.
