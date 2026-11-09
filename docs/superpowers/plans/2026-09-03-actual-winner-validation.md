# Actual-winner Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Reject invalid Final-game winner labels before they can contaminate retrospective temporal-calibration metrics.

**Architecture:** Add one fail-closed validation at the existing outcome-to-label conversion boundary. Keep valid home-team and away-team outcomes byte-for-byte behavior-compatible and cover the failure with a focused synthetic regression.

**Tech Stack:** Python 3.11, `unittest`, existing JSONL fixture helpers, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-actual-winner-validation-design.md`

---

### Task 1: Validate the locked game winner before metric conversion

**Files:**
- Modify: `tests/test_v51_temporal_calibration_candidate.py`
- Modify: `scripts/analysis/v51_temporal_calibration_candidate.py:363-373`

- [x] **Step 1: Add the focused failing regression**

Add this method to `TemporalCalibrationCandidateTest`:

```python
def test_evaluate_rejects_actual_winner_outside_locked_teams(self):
    temp, root, prepare_pins, evaluation_pins = self.make_source()
    self.addCleanup(temp.cleanup)
    prepared = root / "prepared"
    prepare(
        root,
        prepared,
        pins=prepare_pins,
        counts=Counts(
            raw_2022=3,
            eligible_2022=3,
            raw_2023=4,
            eligible_2023=4,
            raw_2024=2,
            candidate_2025=3,
        ),
    )
    evaluated_path = root / "data/v5_1_bt/outcomes/evaluated/evaluated_predictions.jsonl"
    evaluated = [json.loads(line) for line in evaluated_path.read_text().splitlines()]
    evaluated[0]["outcome"]["actual_winner"] = "UNKNOWN"
    write_jsonl(evaluated_path, evaluated)
    changed_pins = replace(evaluation_pins, evaluated=digest(evaluated_path))

    with self.assertRaisesRegex(ValueError, r"actual_winner.*40"):
        evaluate(root, prepared, root / "evaluation", pins=changed_pins, fixed_population=3)
```

- [x] **Step 2: Run RED and confirm the missing validation**

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_v51_temporal_calibration_candidate.TemporalCalibrationCandidateTest.test_evaluate_rejects_actual_winner_outside_locked_teams -v
```

Expected: `FAIL` because `ValueError` is not raised; the existing evaluator silently converts `UNKNOWN` into `home_win = 0`.

- [x] **Step 3: Add the minimal fail-closed validation**

Replace the direct `home_win` conversion with:

```python
actual_winner = outcome.get("actual_winner")
home_team = locked_row.get("home_team")
away_team = locked_row.get("away_team")
if (
    not isinstance(actual_winner, str)
    or not actual_winner
    or actual_winner not in {home_team, away_team}
):
    raise ValueError(
        f"actual_winner {actual_winner!r} does not match locked teams for game_pk {game_pk}"
    )
home_win = int(actual_winner == home_team)
```

Reuse `actual_winner` for the existing baseline-correctness comparison. Do not alter scoring, calibration, metrics, persistence, or valid-label handling.

- [x] **Step 4: Run GREEN and focused regressions**

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m unittest tests.test_v51_temporal_calibration_candidate -v
```

Expected: all tests pass, including the new invalid-winner regression and the existing valid home/away evaluation test.

- [x] **Step 5: Run static verification**

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -B -m ruff check scripts/analysis/v51_temporal_calibration_candidate.py tests/test_v51_temporal_calibration_candidate.py
git diff --check
```

Expected: Ruff prints `All checks passed!`; `git diff --check` exits zero without output.

- [x] **Step 6: Review and hand off without integration side effects**

Independently review the two-file diff for the approved fail-closed contract. Confirm the active model card, prospective records, and prior evaluation artifacts remain unchanged. Do not rerun the historical evaluation, commit, push, merge, or create a PR.
