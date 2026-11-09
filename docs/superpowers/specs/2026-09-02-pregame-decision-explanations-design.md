# Pregame decision explanations

## Goal

Make every daily pregame report understandable without weakening the model's
eligibility gates or implying betting value. The report must explain why a row
is `PRELIMINARY`, `VERIFIED`, `UNSCORED`, or `EXCLUDED` using only provenance-
bound fields already present in the validated payload and readiness inputs.

## Scope and non-goals

- Add deterministic, human-readable explanations to the readiness companion
  Markdown report generated for a daily model run.
- Keep the primary JSON schema, scoring formulas, ranking, and public CLI return
  tuple unchanged.
- Do not add sportsbook, odds, O/U, profitability, or wagering language to the
  internal model report. Market overlays remain separate artifacts.
- Do not infer missing participants, probabilities, or causes that are absent
  from validated evidence.

## Output contract

After the existing per-game readiness table, render one `### {game_pk} · {away}
at {home}` section per scheduled game in sealed order. Braced values are runtime
fields, not unresolved placeholders. Each section contains:

1. `판정` — the validated classification (`VERIFIED`, `PRELIMINARY`,
   `UNSCORED`, or `EXCLUDED`), or an explicit not-ranked statement when no
   primary classification exists.
2. `근거` — only available evidence: model probability when a ranked row has
   one, expected and observed starter IDs/provenance, validated lineup counts,
   data-completeness value, and normalized reason codes.
3. `리스크/보류 이유` — schedule-quality flags and missing/invalid input codes;
   for an unscored game, state that no probability was produced.

Every explanation ends with the fixed disclaimer:
`내부 모델은 배당·O/U 입력을 지원하지 않으며, 이 판정은 연구용 사전 상태이지 베팅 추천이나 수익 보장이 아닙니다.`

`UNSCORED` and `EXCLUDED` sections must not contain a fabricated probability,
pick, fair price, edge, or recommendation. A `PRELIMINARY` or `VERIFIED` section
may display the model's existing probability and predicted winner, but must
label it as model output and preserve the report's research-only disclaimer.

## Deterministic rendering

Implement a private renderer helper that accepts the validated primary payload
and readiness rows. It must:

- map reason codes and schedule flags through fixed dictionaries with a stable
  fallback (`원본 코드: {code}`);
- use existing Markdown escaping for team names, player names, and codes;
- preserve the source game order and never sort by prose;
- represent missing participant objects as `알 수 없음`, never as zero;
- include exact report content SHA, target date, observation/binding references,
  and model-card SHA already shown by the companion report;
- produce byte-identical output for identical inputs.

No free-form language-model generation or network access is permitted in the
renderer.

## Data flow and persistence

`run_daily_top3` continues to validate and persist the primary JSON/Markdown
first. It then calls the explanation renderer with the same validated payload
and readiness data and writes the companion using the existing exclusive,
trusted-root persistence path. A companion write failure must propagate and
must not alter the already-persisted primary report. Existing sidecars and
content hashes remain authoritative for the primary artifacts.

## Error handling

- Unknown reason codes or flags are rendered verbatim with the fixed fallback;
  they are not silently dropped.
- Missing required fields in a validated payload remain a validation error; the
  renderer does not repair or guess them.
- Markdown escaping is applied before interpolation so team/player/code text
  cannot break the table or headings.
- No explanation text may convert an unscored row into a scored or recommended
  row.

## Tests and acceptance criteria

Add focused tests that verify:

- each classification receives the correct three-part explanation;
- ranked rows include probability and evidence, while unscored/excluded rows
  explicitly omit probability and pick fields;
- missing participants render as unknown and missing reason codes are preserved;
- schedule flags and unknown codes use deterministic Korean text/fallbacks;
- Markdown escaping handles punctuation and backslashes;
- identical inputs produce identical bytes;
- primary JSON bytes/schema and CLI return values remain unchanged;
- companion persistence remains exclusive and a failed companion write surfaces
  without overwriting an existing file.

The implementation is complete only when the focused readiness/model test suite,
`git diff --check`, and the existing daily report integrity tests pass.
