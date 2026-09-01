# DraftKings-Only Daily Totals Market Overlay V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separate, append-only, research-only comparison layer that reads the sealed 2026-09-01 totals report and an explicit local DraftKings capture, then classifies all 15 games as `RESEARCH_PICK` or `PASS`.

**Architecture:** Keep the totals model and its artifacts immutable. A pure overlay module owns closed contracts, American-price arithmetic, frozen residual-sample support, and the fixed policy; secure adapters admit local raw captures and bind every comparison to the sealed model trace; the CLI only selects local paths and writes a trace-committed overlay report.

**Tech Stack:** Python 3.11 standard library, existing `secure_fs`, `daily_totals` validators, `live_top3_contracts.canonical_json_bytes`, `unittest`, JSON Schema Draft 2020-12, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-01-daily-totals-market-overlay-design.md` (commit `1b41e51`)

## Global Constraints

- V1 is DraftKings-only; source fields are exactly `source_name="DraftKings"`, `book_id="DRAFTKINGS"`, and `capture_method="EXTERNAL_MANUAL"`.
- The core never imports or calls HTTP, browser, scraper, sportsbook, or network code and never opens `source_url`.
- The core never changes or reruns `mlb_backtest.daily_totals`, its report, receipt, or trace, and never adds market fields to those artifacts.
- The fixed model authority is the existing trace-committed report for target `2026-09-01`, cutoff `2026-08-31`, candidate `TEAM_VENUE_30`, exactly 15 `PROJECTED` games, and residual count `764` per game.
- Every local input and output is canonical JSON with exact SHA-256 sidecars; regular files are opened with no-follow identity checks through `secure_fs`.
- The only successful output namespaces are `audit/experimental_daily_totals_market/draftkings/` and `reports/experimental_daily_totals_overlay/2026-09-01/`.
- A valid outcome is a comparison of frozen sample support and quoted break-even; it is never called a calibrated probability, fair price, expected value, return, edge, ROI, profitability, or wagering recommendation.
- All 15 sealed projected games must appear in every snapshot and overlay; `NO_QUOTE` is explicit and never silently omitted.
- No task accesses `/Users/taejunoh/Documents`, custody/production paths, `/tmp/r4-readonly-tree-manifest.txt`, or performs sudo, push, merge, PR, or destructive Git operations.

---

### Task 1: Pure contracts and frozen-support arithmetic

**Files:**
- Create: `mlb_backtest/daily_totals_market_overlay.py`
- Test: `tests/test_daily_totals_market_overlay.py`

**Interfaces:**
- Produces `validate_american_price(value: object) -> int`, `validate_line_half_runs(value: object) -> int`, `american_net_profit(price: int) -> float`, `quoted_break_even(price: int) -> float`, `compare_sample_support(samples: tuple[int, ...], line_half_runs: int) -> SupportRates`, `price_support(win_support: float, loss_support: float, price: int) -> PriceSupport`, and `select_direction(over: PriceSupport, under: PriceSupport) -> tuple[str | None, tuple[str, ...]]`.
- Produces frozen dataclasses `SupportRates(over: float, under: float, push: float)` and `PriceSupport(conditional_support: float | None, quoted_break_even: float, support_margin: float | None)`.
- `SupportRates` uses `line_half_runs / 2` only for comparison; no binary floating-point equality is used for line identity.

- [ ] **Step 1: Write independent RED tests for price and line validation**

```python
def test_price_rules_accept_signed_integer_range(self):
    self.assertEqual(validate_american_price(100), 100)
    self.assertEqual(validate_american_price(-10000), -10000)
    for value in (0, 99, -99, 100.0, True, "-110", 10001, -10001):
        with self.subTest(value=value), self.assertRaises(ValueError):
            validate_american_price(value)

def test_half_run_ticks_are_positive_integers(self):
    self.assertEqual(validate_line_half_runs(19), 19)
    for value in (0, -1, 18.5, True, "19"):
        with self.subTest(value=value), self.assertRaises(ValueError):
            validate_line_half_runs(value)
```

- [ ] **Step 2: Run the focused RED test**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.PureMathTests -v`

Expected: FAIL because `mlb_backtest.daily_totals_market_overlay` and `validate_american_price` do not yet exist.

- [ ] **Step 3: Add the minimal immutable math types and functions**

```python
@dataclass(frozen=True, slots=True)
class SupportRates:
    over: float
    under: float
    push: float

def american_net_profit(price: int) -> float:
    return price / 100 if price > 0 else 100 / abs(price)

def quoted_break_even(price: int) -> float:
    return 1 / (1 + american_net_profit(price))

def compare_sample_support(samples: tuple[int, ...], line_half_runs: int) -> SupportRates:
    line = line_half_runs / 2
    count = len(samples)
    return SupportRates(
        over=sum(sample * 2 > line_half_runs for sample in samples) / count,
        under=sum(sample * 2 < line_half_runs for sample in samples) / count,
        push=sum(sample * 2 == line_half_runs for sample in samples) / count,
    )
```

Validate `samples` as a non-empty tuple of nonnegative integers, require exactly 764 only in the policy gate, validate prices before arithmetic, and reject `win_support + loss_support == 0` with `conditional_support=None` and `support_margin=None`.

- [ ] **Step 4: Add independent support and direction tests**

```python
def test_support_handles_integer_and_half_lines(self):
    self.assertEqual(compare_sample_support((7, 8, 8, 9), 16), SupportRates(.25, .25, .50))
    self.assertEqual(compare_sample_support((7, 8, 8, 9), 17), SupportRates(.25, .75, 0.0))

def test_price_support_uses_push_excluded_denominator(self):
    result = price_support(.75, .25, -110)
    self.assertAlmostEqual(result.quoted_break_even, 110 / 210)
    self.assertAlmostEqual(result.conditional_support, .75)
    self.assertAlmostEqual(result.support_margin, .75 - 110 / 210)

def test_tied_margins_are_ambiguous(self):
    self.assertEqual(select_direction(price_support(.5, .5, -110), price_support(.5, .5, -110)), (None, ("AMBIGUOUS_DIRECTION",)))
```

- [ ] **Step 5: Run tests, Ruff, and commit the pure layer**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.PureMathTests -v`

Expected: all pure math tests PASS.

Run: `python3 -m ruff check mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git diff --check`

Commit: `git add mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git commit -m "feat: add totals market overlay math"`

### Task 2: Three closed JSON schemas and validators

**Files:**
- Modify: `mlb_backtest/daily_totals_market_overlay.py`
- Create: `schemas/experimental_daily_totals_draftkings_snapshot.schema.json`
- Create: `schemas/experimental_daily_totals_overlay_policy.schema.json`
- Create: `schemas/experimental_daily_totals_overlay.schema.json`
- Test: `tests/test_daily_totals_market_overlay.py`

**Interfaces:**
- Produces `validate_draftkings_snapshot(value: object) -> dict[str, object]`, `validate_overlay_policy(value: object) -> dict[str, object]`, and `validate_overlay_report(value: object) -> dict[str, object]`.
- Snapshot closed keys are `schema`, `content_sha256`, `target_date`, `model_trace_relative`, `model_trace_sha256`, `model_report_relative`, `model_report_sha256`, `comparison_bound_at_utc`, `source_name`, `book_id`, `source_url`, `capture_method`, `captured_at_utc`, `raw_capture_relative`, `raw_capture_sha256`, `raw_capture_byte_length`, and `games`.
- Snapshot game variants are exact `QUOTED` keys (`game_pk`, `status`, `draftkings_event_id`, `event_label`, `line_half_runs`, `over_american_odds`, `under_american_odds`) and exact `NO_QUOTE` keys (`game_pk`, `status`, `reason`).
- Overlay quoted rows contain `game_pk`, `expected_total`, `residual_count`, `market_status`, `line_half_runs`, `over_american_odds`, `under_american_odds`, `over_support`, `under_support`, `push_support`, `over_conditional_support`, `under_conditional_support`, `over_support_margin`, `under_support_margin`, `selected_side`, `decision`, and `reason_codes`; no-quote rows contain `game_pk`, `expected_total`, `residual_count`, `market_status`, `decision`, and `reason_codes`.

- [ ] **Step 1: Write RED schema/validator tests**

```python
def test_all_three_schemas_are_valid_and_closed(self):
    for path in (SNAPSHOT_SCHEMA, POLICY_SCHEMA, OVERLAY_SCHEMA):
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertEqual(schema["additionalProperties"], False)

def test_snapshot_rejects_mixed_book_duplicate_missing_unknown_and_bad_price(self):
    for mutated in (mixed_book(), duplicate_game(), missing_game(), unknown_game(), bad_price()):
        with self.subTest(mutated=mutated), self.assertRaises(ValueError):
            validate_draftkings_snapshot(mutated)
```

- [ ] **Step 2: Run the RED schema test**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.SchemaTests -v`

Expected: FAIL because the three schema files and validator functions are absent.

- [ ] **Step 3: Implement the fixed policy and strict validators**

```python
POLICY = {
    "schema": "PROVISIONAL_DAILY_TOTALS_OVERLAY_POLICY_V1",
    "required_residual_count": 764,
    "maximum_quote_age_seconds": 900,
    "minimum_nonpush_support": 0.80,
    "minimum_point_gap_runs": 1.00,
    "minimum_support_margin": 0.05,
}
```

Require exact canonical keys, exact schema strings, RFC3339 UTC `Z` timestamps, DraftKings host `sportsbook.draftkings.com`, 15 unique game keys, matching target date, integer half-run ticks, signed prices with absolute value 100–10000, and closed row variants. Require overlay rows to be in sealed report order and to use only the fixed reason codes `NO_QUOTE`, `GAME_STARTED`, `QUOTE_STALE`, `RESIDUAL_COUNT_MISMATCH`, `NONPUSH_SUPPORT_TOO_LOW`, `POINT_GAP_TOO_SMALL`, `SUPPORT_MARGIN_TOO_LOW`, and `AMBIGUOUS_DIRECTION`.

- [ ] **Step 4: Run schema, boundary, and canonical-round-trip tests**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.SchemaTests -v`

Expected: PASS, including exact-threshold acceptance and `0.049999...`/`0.999999...` rejection.

Run: `python3 -m ruff check mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git diff --check`

Commit: `git add mlb_backtest/daily_totals_market_overlay.py schemas/experimental_daily_totals_draftkings_snapshot.schema.json schemas/experimental_daily_totals_overlay_policy.schema.json schemas/experimental_daily_totals_overlay.schema.json tests/test_daily_totals_market_overlay.py && git commit -m "feat: add closed totals overlay contracts"`

### Task 3: Secure local admission, build, and trace-committed persistence

**Files:**
- Modify: `mlb_backtest/daily_totals_market_overlay.py`
- Test: `tests/test_daily_totals_market_overlay.py`

**Interfaces:**
- Produces `admit_draftkings_snapshot(root: Path, model_trace: Path, raw_capture: Path, *, comparison_clock: Callable[[], datetime]) -> Path`.
- Produces `build_overlay(root: Path, model_trace: Path, market_snapshot: Path, *, evaluated_clock: Callable[[], datetime]) -> BuiltOverlay` and `persist_overlay(root: Path, built: BuiltOverlay) -> tuple[Path, Path, Path, Path]`.
- `BuiltOverlay` stores canonical JSON bytes, Markdown bytes, and trace bytes; the trace is the only commit point.

- [ ] **Step 1: Write RED secure-admission tests**

```python
def test_raw_capture_sidecar_identity_and_url_are_required(self):
    with self.assertRaises(ValueError):
        admit_draftkings_snapshot(root, model_trace, raw_capture_without_sidecar, comparison_clock=fixed_clock)

def test_importer_never_opens_the_source_url(self):
    capture = valid_raw_capture(source_url="https://sportsbook.draftkings.com/event/1")
    self.assertEqual(admit_draftkings_snapshot(root, trace, capture, comparison_clock=fixed_clock).parent.name, "draftkings")
```

- [ ] **Step 2: Run the RED secure-admission tests**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.SecurePersistenceTests -v`

Expected: FAIL because admission and persistence functions are absent.

- [ ] **Step 3: Implement no-follow raw admission and model binding**

Read raw capture and sidecar with `open_trusted_root`, `read_regular_snapshot`, and `rebind_regular_snapshot`; require the raw object keys `captured_at_utc`, `source_url`, and `games`, then add fixed DraftKings fields and raw identity fields. First validate the complete sealed model report, model receipt, report sidecar, model sidecar, and trace against the named authority; obtain `comparison_bound_at_utc` only after that validation and immediately before writing the normalized snapshot. Reject captured timestamps after the comparison clock or any scheduled start, malformed timestamps, unknown/missing/duplicate game keys, or conflicting append-only destinations.

- [ ] **Step 4: Implement overlay construction and ordered append-only publication**

Load the normalized snapshot and sealed report without following symlinks, rebind every snapshot and model file after full reads, and evaluate all 15 rows. For each quoted game, use the report’s `residual_samples`, enforce `residual_count == 764`, compute support and push, compute both quoted break-evens and push-excluded support margins, then apply the fixed policy. For each no-quote game emit `PASS` with `NO_QUOTE` and no invented price/support fields. Write snapshot sidecar before snapshot, then overlay JSON sidecar, JSON, Markdown, and trace in order; the trace must bind model report/sidecars, model trace, model receipt, market snapshot/sidecar, policy, JSON sidecar, and Markdown hashes.

```python
def persist_overlay(root: Path, built: BuiltOverlay) -> tuple[Path, Path, Path, Path]:
    snapshot = validate_overlay_report(built.report)
    report_path = append_only_overlay_path(root, snapshot)
    write_existing_or_exclusive(root, report_path.with_suffix(".sha256"), sidecar(built.report_bytes))
    write_existing_or_exclusive(root, report_path, built.report_bytes)
    write_existing_or_exclusive(root, report_path.with_suffix(".md"), built.markdown_bytes)
    trace_path = report_path.with_suffix(".trace.json")
    write_existing_or_exclusive(root, trace_path, built.trace_bytes)
    rebind_all_inputs_and_outputs(root, built)
    return report_path.with_suffix(".sha256"), report_path, report_path.with_suffix(".md"), trace_path
```

- [ ] **Step 5: Run focused persistence/security tests and commit**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.SecurePersistenceTests -v`

Expected: PASS for symlink, unstable identity, bad sidecar, post-start capture, conflicting append, trace-before-commit, and unchanged model artifact cases.

Run: `python3 -m ruff check mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git diff --check`

Commit: `git add mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git commit -m "feat: securely persist totals overlay"`

### Task 4: CLI and research-only Markdown rendering

**Files:**
- Modify: `mlb_backtest/daily_totals_market_overlay.py`
- Test: `tests/test_daily_totals_market_overlay.py`

**Interfaces:**
- Produces `render_overlay_markdown(payload: object) -> str` and `main(argv: Sequence[str] | None = None) -> int`.
- The CLI has exactly two subcommands: `admit-snapshot --root ROOT --model-trace TRACE --raw-capture RAW` and `compare --root ROOT --model-trace TRACE --market-snapshot SNAPSHOT`.
- `compare` accepts no line, price, game, URL, threshold, or clock override; evaluation time comes from the module clock.

- [ ] **Step 1: Write RED CLI/renderer tests**

```python
def test_compare_rejects_manual_line_or_price_overrides(self):
    self.assertNotEqual(main(["compare", "--root", str(root), "--model-trace", "trace.json", "--market-snapshot", "snapshot.json", "--line", "9.5"]), 0)

def test_markdown_has_required_labels_and_no_financial_claims(self):
    text = render_overlay_markdown(valid_overlay)
    for label in ("RESEARCH ONLY", "ONE-SEASON PROVISIONAL", "NOT BET-VALIDATED", "RESEARCH_PICK", "PASS"):
        self.assertIn(label, text)
    for forbidden in ("calibrated", "fair odds", "expected value", "return", "edge", "ROI", "profitability", "wagering recommendation"):
        self.assertNotIn(forbidden.lower(), text.lower())
```

- [ ] **Step 2: Run the RED CLI test**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.CliTests -v`

Expected: FAIL because the subcommands and renderer are absent.

- [ ] **Step 3: Add the minimal parser and renderer**

Use `argparse` with the exact two subcommands and required paths. Resolve all supplied paths below `--root`, reject `..`, absolute paths, backslashes, and alternate roots, call `admit_draftkings_snapshot` or `build_overlay`/`persist_overlay`, print only the resulting local path, and return exit 0. Map contract/integrity errors to exit 2 and operational errors to exit 1.

Render one row for every game in report order. Identify it by `game_pk`, show DraftKings American prices, line, sample support, push support, conditional support margins, `RESEARCH_PICK`/`PASS`, and reason codes. Use only the labels required by the spec and explicit research warning text; never use forbidden financial-performance or wagering language. Do not invent a matchup label for `NO_QUOTE` rows, whose closed input variant intentionally contains no `event_label`.

- [ ] **Step 4: Run CLI and Markdown tests and commit**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay.CliTests -v`

Expected: PASS for both subcommands, no-option override rejection, all-game rendering, no-quote rendering, and forbidden-language rejection.

Run: `python3 -m ruff check mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git diff --check`

Commit: `git add mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git commit -m "feat: add totals overlay CLI"`

### Task 5: Integration, security isolation, and full acceptance suite

**Files:**
- Modify: `tests/test_daily_totals_market_overlay.py`
- Modify: `mlb_backtest/daily_totals_market_overlay.py` only if an integration defect is found

**Interfaces:**
- Committed tests consume a hermetic fixture derived from the exact 2026-09-01 authority: it preserves the fixed report/trace/receipt schemas, hashes, candidate, 15 `game_pk` values, report order, and 764-sample rows without depending on untracked local evidence.
- A separate local acceptance command consumes the exact existing authority at `reports/experimental_daily_totals/2026-09-01/20260901T150649125495Z-9fd1b2a4bad5c3f9.json`, its trace, and its model receipt under the current LFG root.
- Tests and local acceptance must preserve all pre-existing untracked evidence and must not write into `audit/experimental_daily_totals/`, `reports/experimental_daily_totals/`, or any model namespace.

- [ ] **Step 1: Add the exact 15-game integration fixture assertions**

```python
def test_exact_authority_is_15_projected_games_and_overlay_covers_all(self):
    root, report_path = make_exact_authority_fixture(self.tmp_path)
    report = load_and_validate_model_authority(root, report_path)
    self.assertEqual(report["target_date"], "2026-09-01")
    self.assertEqual(report["cutoff_date"], "2026-08-31")
    self.assertEqual(report["selected_candidate"], "TEAM_VENUE_30")
    self.assertEqual(len(report["games"]), 15)
    self.assertTrue(all(game["status"] == "PROJECTED" for game in report["games"]))
```

The fixture builder must write internally consistent report, report sidecar,
model receipt, model-receipt sidecar, and trace bytes into the test temporary
directory.  It must use the exact 15 `game_pk` values and candidate/count
constants from the sealed authority, while using compact deterministic sample
arrays of length 764 so the test remains self-contained in a fresh clone.

- [ ] **Step 2: Add import-isolation and immutability tests**

Patch `urllib`, browser, and sportsbook import hooks to raise, import the overlay module in a clean interpreter, and assert it succeeds without those imports. Snapshot the model report, model receipt, model trace, and pre-existing untracked status before and after a comparison; assert byte-identical contents, unchanged SHA-256 values, and no writes outside the two overlay namespaces. Assert a zero-pick valid report is accepted once and is not retried.

- [ ] **Step 3: Run the complete focused and regression suite**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals_market_overlay -v`

Expected: PASS for all math, schema, admission, secure persistence, CLI, renderer, isolation, and exact-authority tests.

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_experimental_totals tests.test_daily_totals tests.test_experimental_totals_sources -v`

Expected: existing totals tests remain fully PASS and their report still rejects market fields.

Run the exact local authority acceptance only when the named untracked trace is
present; this is a release command, not a committed-test dependency:

Capture `SNAPSHOT_REL` from the successful `admit-snapshot` command's sole
stdout line, then run:

`test -n "${SNAPSHOT_REL:?set SNAPSHOT_REL to the exact path printed by admit-snapshot}" && PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_totals_market_overlay compare --root "$PWD" --model-trace reports/experimental_daily_totals/2026-09-01/20260901T150649125495Z-9fd1b2a4bad5c3f9.trace.json --market-snapshot "$SNAPSHOT_REL"`

Expected after a valid operator-supplied snapshot: exit 0 and one new
trace-committed overlay under `reports/experimental_daily_totals_overlay/2026-09-01/`.
If the snapshot has not yet been admitted, record this release check as
`BLOCKED_INPUT_ABSENT`; do not make the unit suite depend on it and do not
fabricate a snapshot.

- [ ] **Step 4: Run schema, static, and clean-boundary checks**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
from pathlib import Path
for name in ("experimental_daily_totals_draftkings_snapshot.schema.json", "experimental_daily_totals_overlay_policy.schema.json", "experimental_daily_totals_overlay.schema.json"):
    Draft202012Validator.check_schema(json.loads((Path("schemas") / name).read_text(encoding="utf-8")))
print("schemas: PASS")
PY`

Run: `python3 -m ruff check mlb_backtest/daily_totals_market_overlay.py tests/test_daily_totals_market_overlay.py && git diff --check`

Verify: `git status --short --untracked-files=all` lists only the intended overlay implementation/schema/test files plus unchanged pre-existing user evidence, and `git diff --stat` contains no totals-model edits.

- [ ] **Step 5: Self-review the spec and commit only the verified implementation**

Check every spec requirement against Tasks 1–5: exact DraftKings source contract, raw and normalized identities, UTC ordering, all 15 games, no quote/stale/started outcomes, 764 samples, integer/half-line support, American price boundaries, fixed policy thresholds, all trace bindings, no network imports, immutable model artifacts, no forbidden language, and all acceptance tests. Resolve only concrete defects found by those checks.

Run: `git diff --check && git status --short --untracked-files=all`

Commit: `git add mlb_backtest/daily_totals_market_overlay.py schemas/experimental_daily_totals_draftkings_snapshot.schema.json schemas/experimental_daily_totals_overlay_policy.schema.json schemas/experimental_daily_totals_overlay.schema.json tests/test_daily_totals_market_overlay.py && git commit -m "feat: complete DraftKings totals market overlay"`
