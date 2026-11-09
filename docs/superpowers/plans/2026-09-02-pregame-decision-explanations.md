# Pregame Decision Explanations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic Korean explanations of each game's model classification to the existing pregame readiness companion report.

**Architecture:** Keep the validated primary JSON, scoring service, ranking, and public return values unchanged. Add a pure explanation formatter in `daily_top3.py` that consumes already-validated row/readiness fields, then append one explanation block per game to the existing readiness Markdown before exclusive persistence.

**Tech Stack:** Python 3.11 standard library, existing `daily_top3` renderer, `unittest`, `PYTHONDONTWRITEBYTECODE=1`.

---

## Files and responsibilities

- Modify `mlb_backtest/daily_top3.py`: fixed reason/flag dictionaries, a pure `_decision_explanation_lines` helper, and integration into `render_live_pregame_readiness_markdown`.
- Modify `tests/test_daily_top3.py`: unit coverage for every classification, evidence selection, escaping, deterministic bytes, and unknown-code fallback.
- Modify `tests/test_daily_top3_e2e.py`: assert the persisted companion contains explanations while the primary JSON remains unchanged.

### Task 1: Pure explanation formatter

**Files:** `mlb_backtest/daily_top3.py`, `tests/test_daily_top3.py`

- [ ] **Step 1: Add RED tests for ranked and unscored explanations.**

Add a `DecisionExplanationTests` class using the existing `_game`,
`_participants`, `_slate`, and `_build` fixtures. Assert that a ranked row
renders all of the following in its game block:

```python
self.assertIn("판정: `PRELIMINARY (rank 1)`", block)
self.assertIn("근거: San Diego Padres 승리 확률 61.3%", block)
self.assertIn("검증된 라인업 9명/9명", block)
self.assertIn("선발 예상 일치", block)
self.assertIn("리스크/보류 이유: 데이터 완전성 47.0%", block)
self.assertIn("더블헤더 특성 미지원", block)
self.assertIn("내부 모델은 배당·O/U 입력을 지원하지 않으며", block)
```

For an unscored row with missing away lineup and pitcher list, assert
`판정: \`UNSCORED (not ranked)\``, `확률을 산출하지 않음`, both missing-code
translations, and absence of `승리 확률` and `예상 승자` in that game's block.
Add an unknown reason/flag and assert the literal fallback
`원본 코드: UNKNOWN_REASON`.

- [ ] **Step 2: Run the focused RED test.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_daily_top3.DecisionExplanationTests -v
```

Expected: FAIL because `_decision_explanation_lines` does not exist and the
readiness renderer has no explanation blocks.

- [ ] **Step 3: Implement fixed mappings and the pure helper.**

Add these constants:

```python
_REASON_EXPLANATIONS = {
    "PROBABLE_CONSISTENT": "예상 선발과 관측 선발이 일치",
    "MISSING_AWAY_LINEUP": "원정 라인업 누락",
    "MISSING_HOME_LINEUP": "홈 라인업 누락",
    "MISSING_AWAY_PITCHER_LIST": "원정 선발 관측 누락",
    "MISSING_HOME_PITCHER_LIST": "홈 선발 관측 누락",
    "MISSING_SCHEDULE_PROBABLE": "공식 예상 선발 누락",
}
_FLAG_EXPLANATIONS = {
    "doubleheader_unsupported": "더블헤더 특성 미지원",
}
_MODEL_DISCLAIMER = (
    "내부 모델은 배당·O/U 입력을 지원하지 않으며, 이 판정은 연구용 "
    "사전 상태이지 베팅 추천이나 수익 보장이 아닙니다."
)
```

Implement the helper exactly as follows, preserving `_escape_table` for every
interpolated value:

```python
def _decision_explanation_lines(
    game: Mapping[str, object],
    row: Mapping[str, object] | None,
    participant: object,
) -> tuple[str, ...]:
    classification = _readiness_classification(row)
    reasons = tuple(str(code) for code in (row or {}).get("reason_codes", ()))
    reason_text = tuple(
        _REASON_EXPLANATIONS.get(code, f"원본 코드: {code}")
        for code in reasons
    )
    flag_text: list[str] = []
    if row is not None:
        raw_flags = row.get("schedule_quality_flags") or {}
        for side in ("away", "home"):
            for code in raw_flags.get(side, ()):
                flag_text.append(
                    _FLAG_EXPLANATIONS.get(str(code), f"원본 코드: {code}")
                )
    if row is None or row.get("state") in {"UNSCORED", "EXCLUDED"}:
        evidence = "확률을 산출하지 않음"
        risks = (*reason_text, *flag_text) or ("필수 입력 부족",)
    else:
        probability = float(row["pick_probability"])
        away_count = _readiness_observed(participant, "away")[1]
        home_count = _readiness_observed(participant, "home")[1]
        evidence = (
            f'{row["predicted_winner"]} 승리 확률 {probability:.1%}; '
            f"검증된 라인업 {away_count}명/{home_count}명"
        )
        if "PROBABLE_CONSISTENT" in reasons:
            evidence += "; 선발 예상 일치"
        risks = (
            f'데이터 완전성 {float(row["data_completeness"]):.1%}',
            *reason_text,
            *flag_text,
        )
    return (
        f"- 판정: `{_escape_table(classification)}`",
        f"- 근거: {_escape_table(evidence)}",
        f"- 리스크/보류 이유: {_escape_table('; '.join(risks) or 'none')}",
        f"- {_MODEL_DISCLAIMER}",
    )
```

Use `participant=None` when no participant binding exists. Missing counts remain
`unknown`; never convert missing values to zero.

- [ ] **Step 4: Run pure tests and lint.**

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest tests.test_daily_top3.DecisionExplanationTests -v
python3 -m ruff check mlb_backtest/daily_top3.py tests/test_daily_top3.py
git diff --check
```

Expected: all new tests PASS, Ruff clean, and no whitespace errors.

- [ ] **Step 5: Commit the pure formatter.**

```bash
git add mlb_backtest/daily_top3.py tests/test_daily_top3.py
git commit -m "feat: explain pregame decision states"
```

### Task 2: Readiness integration and end-to-end verification

**Files:** `mlb_backtest/daily_top3.py`, `tests/test_daily_top3.py`, `tests/test_daily_top3_e2e.py`

- [ ] **Step 1: Add RED integration assertions.**

Extend the existing readiness renderer test to locate each `### {game_pk}` block
and assert the three labels (`판정`, `근거`, `리스크/보류 이유`) plus the fixed
disclaimer. Extend the E2E test to assert explanations exist for every game,
that an unscored block contains no probability text, and that primary JSON bytes
before and after rendering are identical.

- [ ] **Step 2: Append explanation blocks in sealed game order.**

In `render_live_pregame_readiness_markdown`, immediately after the existing
binding lines for each game, append:

```python
sections.extend(_decision_explanation_lines(game, row, participant))
```

The helper receives `participant` only when its binding exists; otherwise pass
`None`. Keep the existing table, binding lines, headings, and return type
unchanged apart from the new deterministic lines.

- [ ] **Step 3: Run focused and full affected tests.**

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 -m unittest \
  tests.test_daily_top3 tests.test_daily_top3_e2e tests.test_live_top3_contracts
```

Expected: all tests PASS; intentional CLI error-path diagnostics may appear in
captured output, but the process exits 0.

- [ ] **Step 4: Verify persisted artifact and repository safety.**

Inspect the existing readiness report under
`reports/experimental_daily/2026-09-02/` with `sed` and confirm its original
content remains unchanged (it predates this implementation). Generate no new
live run in this task. Run:

```bash
git diff --check
git status --short --branch
```

Only the owned source/test files may be staged by this task; preserve all
existing untracked audit/data/report artifacts.

- [ ] **Step 5: Commit integration.**

```bash
git add mlb_backtest/daily_top3.py tests/test_daily_top3.py tests/test_daily_top3_e2e.py
git commit -m "feat: publish pregame decision explanations"
```

## Completion criteria

- Every scheduled game in a readiness companion has a deterministic explanation
  with 판정, 근거, and 리스크/보류 이유.
- Ranked rows show only existing model evidence; unscored/excluded rows show no
  invented probability or recommendation.
- Primary JSON schema, bytes, scoring, ranking, and public return tuple are
  unchanged.
- Focused readiness tests, affected model tests, Ruff, and `git diff --check`
  pass with bytecode writes disabled.
