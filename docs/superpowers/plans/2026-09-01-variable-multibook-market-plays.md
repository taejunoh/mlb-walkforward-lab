# Variable Multi-Book MLB Market Plays Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separate, offline, full-slate decision layer that evaluates moneyline and total-runs quotes from five New York sportsbooks and emits only gate-qualified `PLAY` rows without a fixed count.

**Architecture:** Preserve the existing `daily_top3` and DraftKings-only totals overlay as immutable V1 producers. Add append-only raw-capture admission, a normalized five-book market board, sealed authority adapters, two pure evaluators, and a trace-committed combined report. Sportsbook collection remains an external authorized operation; evaluator modules never import or call network/browser/model-runner code.

**Tech Stack:** Python 3.11 standard library, `Fraction`/`Decimal`, existing `secure_fs`, existing live/totals validators, JSON Schema Draft 2020-12, `unittest`, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-01-variable-multibook-market-plays-design.md` at commit `34bfe4d`

---

## Global constraints

- Work only in `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`; never access `/Users/taejunoh/Documents`.
- Preserve every existing untracked report, audit record, live observation, and model artifact.
- Do not modify the schemas or semantics of `daily_top3`, `live_top3_*`, or `daily_totals_market_overlay`; they remain historical V1 authorities.
- Do not add sportsbook fields to model reports or use market data to rerun or select a model.
- Supported books are exactly `FANDUEL`, `DRAFTKINGS`, `BETMGM`, `CAESARS`, and `FANATICS`.
- The evaluator accepts no CLI override for date, book, line, price, policy, source URL, clock, game, or model value.
- Every input is read with no-follow identity checks and explicit byte caps. Every output is canonical JSON with exact SHA-256 bindings.
- Successful publication order is sidecar, JSON, Markdown, trace. The trace is the only success commit point.
- No task performs sudo, destructive Git, merge, PR, or a live wager. Network use is outside these implementation tests.

## File map

New focused modules:

```text
mlb_backtest/market_play_contracts.py          exact types, math, policy, reasons
mlb_backtest/multibook_market_capture.py      raw capture/outcome admission
mlb_backtest/multibook_market_board.py        schedule mapping and normalized board
mlb_backtest/market_play_authorities.py       sealed ML/totals/participant readers
mlb_backtest/multibook_ml_evaluator.py        pure ML PLAY/PASS evaluation
mlb_backtest/multibook_totals_evaluator.py    pure total PLAY/PASS evaluation
mlb_backtest/daily_multibook_market_plays.py  combined report, persistence, CLI
```

New closed schemas:

```text
schemas/experimental_multibook_market_raw_capture.schema.json
schemas/experimental_multibook_market_capture_outcome.schema.json
schemas/experimental_multibook_market_board.schema.json
schemas/experimental_multibook_market_plays_policy.schema.json
schemas/experimental_multibook_market_plays_report.schema.json
schemas/experimental_multibook_market_plays_trace.schema.json
```

New tests mirror those boundaries. Do not append thousands of lines to the
existing 2,990-line DraftKings overlay test module.

---

### Task 1: Exact policy, quote types, and shared contracts

**Files:**
- Create: `mlb_backtest/market_play_contracts.py`
- Create: `schemas/experimental_multibook_market_plays_policy.schema.json`
- Create: `tests/test_market_play_contracts.py`

**Interfaces:**

```python
SUPPORTED_BOOK_IDS = (
    "FANDUEL", "DRAFTKINGS", "BETMGM", "CAESARS", "FANATICS"
)

@dataclass(frozen=True, slots=True)
class MlQuote:
    game_pk: int
    book_id: str
    event_id: str
    captured_at_utc: str
    away_american_odds: int
    home_american_odds: int

@dataclass(frozen=True, slots=True)
class TotalQuote:
    game_pk: int
    book_id: str
    event_id: str
    captured_at_utc: str
    line_half_runs: int
    over_american_odds: int
    under_american_odds: int

POLICY = {
    "schema": "PROVISIONAL_MULTIBOOK_MARKET_PLAYS_POLICY_V1",
    "maximum_quote_age_seconds": 300,
    "minimum_book_coverage": 3,
    "minimum_data_completeness": "0.50",
    "minimum_ml_price_margin": "0.05",
    "required_residual_count": 764,
    "minimum_nonpush_support": "0.80",
    "minimum_point_gap_runs": "1.00",
    "minimum_total_support_margin": "0.05",
}
```

Public helpers are `validate_american_price`, `validate_line_half_runs`,
`quoted_break_even_fraction`, `decimal_fraction`, `canonical_utc_ns`,
`quote_age_ns`, `validate_relative_path`, `canonical_utc`, `canonical_json_bytes`,
`content_digest`, and `sidecar_bytes`. Policy decimals are canonical strings
converted to `Fraction`; no binary float controls a gate. Timestamps remain
canonical RFC3339 UTC strings and are parsed directly to integer epoch
nanoseconds; Python `datetime` precision never controls the freshness gate.

- [ ] **Step 1: Write RED exact-boundary and closed-policy tests**

```python
def test_exact_policy_boundaries(self):
    self.assertEqual(decimal_fraction("0.50"), Fraction(1, 2))
    self.assertEqual(decimal_fraction("0.05"), Fraction(1, 20))
    self.assertEqual(quoted_break_even_fraction(400), Fraction(1, 5))
    self.assertEqual(quoted_break_even_fraction(-150), Fraction(3, 5))

def test_quote_age_accepts_300_seconds_not_one_nanosecond_more(self):
    evaluated = "2026-09-01T20:05:00Z"
    self.assertEqual(quote_age_ns("2026-09-01T20:00:00Z", evaluated), 300_000_000_000)
    self.assertGreater(
        quote_age_ns("2026-09-01T19:59:59.999999999Z", evaluated),
        300_000_000_000,
    )

def test_canonical_utc_ns_rejects_noncanonical_or_lossy_times(self):
    for value in (
        "2026-09-01T20:00:00+00:00",
        "2026-09-01T20:00:60Z",
        "2026-09-01T20:00:00.1234567890Z",
        "2026-02-29T20:00:00Z",
        1_788_293_600,
    ):
        with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
            canonical_utc_ns(value)

def test_policy_is_closed_and_has_fixed_books_and_reason_order(self):
    self.assertEqual(tuple(SUPPORTED_BOOK_IDS), EXPECTED_BOOKS)
    self.assertEqual(validate_policy(POLICY), POLICY)
    with self.assertRaises(ValueError):
        validate_policy({**POLICY, "minimum_book_coverage": 2})
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_market_play_contracts -v`

Expected: import failure because `market_play_contracts` does not exist.

- [ ] **Step 3: Implement immutable types, exact helpers, and the closed policy**

```python
def quoted_break_even_fraction(price: int) -> Fraction:
    price = validate_american_price(price)
    return Fraction(100, price + 100) if price > 0 else Fraction(abs(price), abs(price) + 100)

def decimal_fraction(value: object, label: str = "decimal") -> Fraction:
    if not isinstance(value, str) or not DECIMAL_RE.fullmatch(value):
        raise ValueError(f"{label} must be a canonical decimal string")
    return Fraction(Decimal(value))

def quote_age_ns(captured: str, evaluated: str) -> int:
    delta_ns = canonical_utc_ns(evaluated) - canonical_utc_ns(captured)
    if delta_ns < 0:
        raise ValueError("quote timestamp is in the future")
    return delta_ns
```

Implement `canonical_utc_ns` with a full-match parser for
`YYYY-MM-DDTHH:MM:SS[.fraction]Z`, accepting one through nine fractional
digits and scaling the fraction to nanoseconds. Reject offsets, missing `Z`,
leap seconds, invalid calendar values, more than nine fractional digits, and
non-strings. Convert only the whole-second calendar portion with integer-safe
UTC arithmetic, then add the scaled fraction. `canonical_utc` validates the
same grammar and returns the single normalized UTC representation used in
canonical JSON; it must not round or truncate fractional digits.

Use a fixed tuple for reason ordering:

```python
REASON_ORDER = (
    "GAME_STARTED", "INCOMPLETE_PARTICIPANTS", "STARTER_CONFLICT",
    "LATE_SCRATCH", "INSUFFICIENT_BOOK_COVERAGE", "STALE_PRICE",
    "NO_QUOTE", "MODEL_UNSCORED", "DATA_COMPLETENESS_TOO_LOW",
    "PRICE_MARGIN_TOO_LOW", "RESIDUAL_COUNT_MISMATCH",
    "NONPUSH_SUPPORT_TOO_LOW", "POINT_GAP_TOO_SMALL",
    "SUPPORT_MARGIN_TOO_LOW", "AMBIGUOUS_DIRECTION",
)
```

- [ ] **Step 4: Validate the policy schema and run focused tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_market_play_contracts -v
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
Draft202012Validator.check_schema(json.load(open("schemas/experimental_multibook_market_plays_policy.schema.json")))
print("policy schema: PASS")
PY
python3 -m ruff check mlb_backtest/market_play_contracts.py tests/test_market_play_contracts.py
git diff --check
```

Expected: all contract tests and schema validation pass.

- [ ] **Step 5: Commit the pure contract layer**

```bash
git add mlb_backtest/market_play_contracts.py schemas/experimental_multibook_market_plays_policy.schema.json tests/test_market_play_contracts.py
git commit -m "feat: add multibook market play contracts"
```

---

### Task 2: Raw five-book capture and availability outcomes

**Files:**
- Create: `mlb_backtest/multibook_market_capture.py`
- Create: `schemas/experimental_multibook_market_raw_capture.schema.json`
- Create: `schemas/experimental_multibook_market_capture_outcome.schema.json`
- Create: `tests/test_multibook_market_capture.py`

**Interfaces:**

```python
def validate_raw_market_capture(value: object) -> dict[str, object]: ...
def validate_capture_outcome(value: object) -> dict[str, object]: ...
def admit_raw_market_capture(root: Path, raw_capture: Path) -> Path: ...
def record_unavailable_book(root: Path, unavailable_record: Path) -> Path: ...
def load_capture_outcome(trusted: TrustedRoot, relative: Path) -> LoadedCaptureOutcome: ...
```

The external authorized collector supplies files; this module never performs
collection. Raw capture keys are exactly `schema`, `content_sha256`,
`source_book`, `captured_at_utc`, `source_url`, `transport_metadata`, and
`events`. Each event has book event ID, displayed teams, scheduled start, one
paired ML quote, and zero or more paired full-game total quotes. Outcome is a
closed `AVAILABLE` or `UNAVAILABLE` variant for exactly one supported book.

- [ ] **Step 1: Write RED closed-schema and no-follow admission tests**

```python
def test_each_supported_book_admits_one_content_addressed_capture(self):
    for book in SUPPORTED_BOOK_IDS:
        path = write_raw_capture(self.root, book, valid_events())
        outcome = admit_raw_market_capture(self.root, path)
        self.assertTrue(outcome.as_posix().startswith("audit/experimental_multibook_market/capture_outcomes/"))

def test_unavailable_outcome_has_no_raw_path(self):
    result = validate_capture_outcome(unavailable("BETMGM", "SOURCE_UNAVAILABLE"))
    self.assertNotIn("raw_capture_relative", result)

def test_capture_rejects_bad_sidecar_symlink_hardlink_directory_and_oversize(self):
    for attack in CAPTURE_ATTACKS:
        with self.subTest(attack=attack), self.assertRaises((ValueError, OSError)):
            admit_raw_market_capture(self.root, attack.prepare(self.root))
```

- [ ] **Step 2: Run capture tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_market_capture -v`

Expected: import failure because capture admission is absent.

- [ ] **Step 3: Implement bounded admission and append-only outcomes**

Use `open_trusted_root`, `read_regular_snapshot(..., max_bytes=2 * 1024 * 1024)`,
`rebind_regular_snapshot`, and `write_exclusive`. Sidecars are exactly 65 bytes.
Derive the book from the capture body, not a CLI flag. Validate the book-specific
HTTPS hostname from a fixed mapping and retain `source_url` only as provenance.

```python
RAW_PREFIX = PurePosixPath("data/experimental_multibook_market/raw")
OUTCOME_PREFIX = PurePosixPath("audit/experimental_multibook_market/capture_outcomes")

def admit_raw_market_capture(root: Path, raw_capture: Path) -> Path:
    trusted = open_trusted_root(root)
    loaded = _read_capture_and_sidecar(trusted, raw_capture)
    capture = validate_raw_market_capture(json.loads(loaded.contents))
    _require_content_addressed_path(capture, raw_capture)
    outcome = _available_outcome(capture, loaded)
    return _persist_canonical_outcome(trusted, outcome)
```

- [ ] **Step 4: Add identity-race, mixed-book, bad-pair, and unavailable tests**

Test unknown/mixed book IDs, duplicate event IDs, malformed times, future
capture, unpaired ML/totals prices, bad American limits, source-host confusion,
sidecar mismatch, content-addressed filename mismatch, before/open/post identity
changes, root swap, existing-identical reuse, and conflicting append.

- [ ] **Step 5: Run capture/security tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_market_capture -v
python3 -m ruff check mlb_backtest/multibook_market_capture.py tests/test_multibook_market_capture.py
git diff --check
git add mlb_backtest/multibook_market_capture.py schemas/experimental_multibook_market_raw_capture.schema.json schemas/experimental_multibook_market_capture_outcome.schema.json tests/test_multibook_market_capture.py
git commit -m "feat: admit multibook market captures"
```

---

### Task 3: Normalize the official full-slate market board

**Files:**
- Create: `mlb_backtest/multibook_market_board.py`
- Create: `schemas/experimental_multibook_market_board.schema.json`
- Create: `tests/test_multibook_market_board.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class BuiltMarketBoard:
    board: Mapping[str, object]
    board_bytes: bytes
    sidecar_bytes: bytes
    input_snapshots: tuple[RegularSnapshot, ...]

def load_schedule_authority(root: Path, schedule_receipt: Path) -> ScheduleAuthority: ...
def normalize_market_board(root: Path, schedule_receipt: Path, capture_outcomes: Sequence[Path]) -> BuiltMarketBoard: ...
def persist_market_board(root: Path, built: BuiltMarketBoard) -> tuple[Path, Path]: ...
def load_market_board(root: Path, board_path: Path, schedule: ScheduleAuthority) -> MarketBoardAuthority: ...
```

- [ ] **Step 1: Write RED identity-mapping and exact-five-outcome tests**

```python
def test_board_maps_five_book_events_to_game_pk_and_keeps_zero_quote_games(self):
    built = normalize_market_board(self.root, self.schedule_receipt, self.outcomes)
    self.assertEqual([g["game_pk"] for g in built.board["games"]], self.official_game_pks)
    self.assertEqual(len(built.board["capture_outcomes"]), 5)
    self.assertEqual(built.board["games"][-1]["moneylines"], [])

def test_board_rejects_missing_duplicate_or_unknown_book_outcomes(self):
    for outcomes in (self.outcomes[:4], (*self.outcomes[:4], self.outcomes[0]), unknown_book_outcomes()):
        with self.subTest(outcomes=outcomes), self.assertRaises(ValueError):
            normalize_market_board(self.root, self.schedule_receipt, outcomes)
```

- [ ] **Step 2: Run board tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_market_board -v`

Expected: import failure because the board module does not exist.

- [ ] **Step 3: Implement sealed schedule loading and deterministic matching**

Read the `LIVE_SCHEDULE` receipt, manifest, raw payload, and sidecars through
`secure_fs`; call `parse_live_schedule`; bind every byte. Match events using
normalized away/home identity and scheduled start. Reject rather than guess on
ambiguity, reversal, duplicate `(book_id, game_pk)`, unknown game, or start
conflict. The board is date-variable and must not import `FIXED_GAME_PKS`.

```python
def _match_event(event: Mapping[str, object], games: Sequence[Mapping[str, object]]) -> int:
    matches = [game for game in games if _same_teams(event, game) and _same_start(event, game)]
    if len(matches) != 1:
        raise ValueError("market event does not map uniquely to the official slate")
    return int(matches[0]["game_pk"])
```

- [ ] **Step 4: Add mapping/security/append-only tests**

Cover same teams at different starts, reversed home/away, alias collision,
unknown game, duplicate book event, invalid total pair, unavailable books,
all-official-game retention, shuffled raw order, deterministic quote order,
mutated outcome/raw/schedule after read, and output conflicts.

- [ ] **Step 5: Run schema, board, capture regression, and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_market_board tests.test_multibook_market_capture -v
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
Draft202012Validator.check_schema(json.load(open("schemas/experimental_multibook_market_board.schema.json")))
print("board schema: PASS")
PY
python3 -m ruff check mlb_backtest/multibook_market_board.py tests/test_multibook_market_board.py
git diff --check
git add mlb_backtest/multibook_market_board.py schemas/experimental_multibook_market_board.schema.json tests/test_multibook_market_board.py
git commit -m "feat: normalize multibook MLB market board"
```

---

### Task 4: Read sealed ML, totals, and participant authorities

**Files:**
- Create: `mlb_backtest/market_play_authorities.py`
- Create: `tests/test_market_play_authorities.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ParticipantGateEvidence:
    complete: bool
    starter_consistent: bool
    late_scratch: bool
    reason_codes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class MlGameAuthority:
    game_pk: int
    scheduled_start_utc: str
    away_team: str
    home_team: str
    predicted_side: Literal["away", "home"] | None
    pick_probability: Fraction | None
    data_completeness: Fraction | None
    participant: ParticipantGateEvidence
    model_status: Literal["SCORED", "UNSCORED"]
    legacy_primary_rank: int | None

def load_ml_authority(root: Path, ml_trace: Path) -> MlAuthority: ...
def load_totals_authority(root: Path, totals_trace: Path) -> TotalsAuthority: ...
def rebind_authorities(*authorities: EvidenceAuthority) -> None: ...
```

- [ ] **Step 1: Write RED all-slate ML and residual-authority tests**

```python
def test_ml_adapter_includes_scored_not_top3_without_using_rank(self):
    authority = load_ml_authority(self.root, self.ml_trace)
    self.assertEqual(authority.by_game[4].model_status, "SCORED")
    self.assertIsNone(authority.by_game[4].legacy_primary_rank)
    self.assertEqual(authority.by_game[4].pick_probability, Fraction("0.58"))

def test_unscored_primary_population_is_preserved(self):
    authority = load_ml_authority(self.root, self.ml_trace)
    self.assertEqual(authority.by_game[9].model_status, "UNSCORED")

def test_totals_adapter_requires_764_bound_residual_samples(self):
    authority = load_totals_authority(self.root, self.totals_trace)
    self.assertTrue(all(len(game.residual_samples) == 764 for game in authority.games))
```

- [ ] **Step 2: Run authority tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_market_play_authorities -v`

Expected: import failure because authority adapters are absent.

- [ ] **Step 3: Implement the ML primary/trace union**

Validate the legacy primary with `validate_run_payload` and companion score trace
with `validate_trace_payload`. Require the trace's primary path/digest and model
card digest to match. Read both `PRELIMINARY` and `SCORED_NOT_TOP3`; ignore
`primary_rank` for decisions. Recompute completeness from trace qualities with
`overall_completeness`, then convert `Decimal(str(value))` to `Fraction`.

Re-read each `LIVE_PARTICIPANTS` and `LIVE_SCHEDULE` payload/receipt/manifest
named in the primary source bindings. Apply `parse_pregame_boxscore` and
`compare_probable_pitchers`. Missing/duplicate lineups map to incomplete,
probable mismatch maps to starter conflict, and post-start appearance/scratch
evidence maps to late scratch.

- [ ] **Step 4: Implement the totals report/trace/receipt adapter**

Reuse `validate_totals_report`, `validate_totals_trace`, and receipt validators.
Require the trace to bind the exact report and receipt components. Preserve
integer residual arrays and verify count/hash before creating frozen authority
objects. Do not loosen the fixed DraftKings overlay authority; this is a new
date-variable reader with its own trace binding.

- [ ] **Step 5: Add mutation, missing-component, cap, and no-model-run tests**

Patch model builders and HTTP/browser imports to fail if called. Test malformed
primary/trace population, duplicate game, missing `SCORED_NOT_TOP3`, participant
payload mutation, receipt/manifest/sidecar mismatch, residual mismatch, symlink,
hardlink, root swap, oversized components, and rebind failure.

- [ ] **Step 6: Run authority and legacy regression tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_market_play_authorities tests.test_live_top3_contracts tests.test_live_top3_trace tests.test_daily_totals -v
python3 -m ruff check mlb_backtest/market_play_authorities.py tests/test_market_play_authorities.py
git diff --check
git add mlb_backtest/market_play_authorities.py tests/test_market_play_authorities.py
git commit -m "feat: load sealed market play authorities"
```

---

### Task 5: Pure full-slate ML PLAY/PASS evaluation

**Files:**
- Create: `mlb_backtest/multibook_ml_evaluator.py`
- Create: `tests/test_multibook_ml_evaluator.py`

**Interfaces:**

```python
def select_best_ml_quote(predicted_side: str, quotes: Sequence[MlQuote]) -> MlQuote | None: ...
def evaluate_moneylines(
    ml: MlAuthority,
    board: MarketBoardAuthority,
    *,
    evaluated_at: str,
) -> tuple[Mapping[str, object], ...]: ...
```

- [ ] **Step 1: Write RED price, coverage, and variable-count tests**

```python
def test_ml_exact_thresholds_and_best_price(self):
    rows = evaluate_moneylines(ml(completeness="0.50", probability="0.60"), board(
        prices={"FANDUEL": -125, "DRAFTKINGS": -120, "BETMGM": 100}
    ), evaluated_at=NOW)
    self.assertEqual(rows[0]["decision"], "PLAY")
    self.assertEqual(rows[0]["selected_book"], "BETMGM")
    self.assertEqual(rows[0]["price_margin"], "0.10")

def test_two_books_pass_and_three_books_can_play(self):
    self.assertIn("INSUFFICIENT_BOOK_COVERAGE", decision(two_fresh_quotes()).reason_codes)
    self.assertEqual(decision(three_fresh_quotes()).decision, "PLAY")

def test_zero_one_and_five_plays_are_not_truncated_or_filled(self):
    self.assertEqual(play_count(slate_with_qualifiers(0)), 0)
    self.assertEqual(play_count(slate_with_qualifiers(1)), 1)
    self.assertEqual(play_count(slate_with_qualifiers(5)), 5)
```

- [ ] **Step 2: Run ML tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_ml_evaluator -v`

Expected: import failure because the evaluator is absent.

- [ ] **Step 3: Implement common gates, quote filtering, and exact ML margin**

Filter invalid/future/stale quotes before coverage. Zero viable quotes yields
`NO_QUOTE`; one or two yields `INSUFFICIENT_BOOK_COVERAGE`; if stale removal
caused the shortfall, also include `STALE_PRICE`. Select the quote with minimum
exact break-even probability, with fixed book order as a deterministic tie.

```python
margin = ml_game.pick_probability - quoted_break_even_fraction(selected_price)
play = (
    common_reasons == ()
    and ml_game.data_completeness >= Fraction(1, 2)
    and margin >= Fraction(1, 20)
)
```

Every official board game emits one row. Unscored games have no probability or
price margin and carry `MODEL_UNSCORED`.

- [ ] **Step 4: Add complete reason-set and ordering tests**

Cover started game, incomplete participant, starter conflict, late scratch,
exact 300 seconds, 300 seconds plus one nanosecond, 0.50 and immediately below,
exact 0.05 and immediately below, mixed positive/negative odds, opposing paired
price preservation, no-vig non-use, stale-but-three-fresh play, fixed reasons,
no rank field, and deterministic display ordering without truncation.

- [ ] **Step 5: Run ML/contract/authority tests and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_ml_evaluator tests.test_market_play_contracts tests.test_market_play_authorities -v
python3 -m ruff check mlb_backtest/multibook_ml_evaluator.py tests/test_multibook_ml_evaluator.py
git diff --check
git add mlb_backtest/multibook_ml_evaluator.py tests/test_multibook_ml_evaluator.py
git commit -m "feat: evaluate variable moneyline plays"
```

---

### Task 6: Pure multi-line totals PLAY/PASS evaluation

**Files:**
- Create: `mlb_backtest/multibook_totals_evaluator.py`
- Create: `tests/test_multibook_totals_evaluator.py`

**Interfaces:**

```python
def evaluate_total_candidate(game: TotalsGameAuthority, quote: TotalQuote, direction: str) -> TotalCandidate: ...
def select_total_play(candidates: Sequence[TotalCandidate]) -> TotalCandidate | None: ...
def evaluate_totals(
    totals: TotalsAuthority,
    participants: Mapping[int, ParticipantGateEvidence],
    board: MarketBoardAuthority,
    *,
    evaluated_at: str,
) -> tuple[Mapping[str, object], ...]: ...
```

- [ ] **Step 1: Write RED residual, cross-line, and tie-break tests**

```python
def test_each_book_line_direction_is_evaluated_independently(self):
    rows = evaluate_totals(authority(samples=RESIDUALS_764), participants(), board(
        totals={"FANDUEL": (17, -105, -115), "DRAFTKINGS": (18, 110, -130), "BETMGM": (19, 125, -145)}
    ), evaluated_at=NOW)
    self.assertEqual(rows[0]["decision"], "PLAY")
    self.assertEqual((rows[0]["selected_book"], rows[0]["line_half_runs"]), EXPECTED_SELECTION)

def test_exact_total_gates_pass_and_immediately_lower_values_fail(self):
    self.assertEqual(candidate(nonpush="0.80", point_gap="1.00", margin="0.05").decision, "PLAY")
    for field, value in (("nonpush", "0.799"), ("point_gap", "0.999"), ("margin", "0.049")):
        self.assertEqual(candidate(**{field: value}).decision, "PASS")
```

- [ ] **Step 2: Run totals evaluator tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_totals_evaluator -v`

Expected: import failure because the evaluator is absent.

- [ ] **Step 3: Implement integer-count support and exact candidate gates**

Use integer sample counts against integer half-run ticks. Conditional support
excludes pushes. Compute point gap as an exact decimal fraction from the sealed
expected total and line ticks. Evaluate over and under separately for every
book/line.

```python
qualifies = (
    residual_count == 764
    and nonpush_support >= Fraction(4, 5)
    and point_gap >= Fraction(1, 1)
    and support_margin >= Fraction(1, 20)
    and common_reasons == ()
)
```

- [ ] **Step 4: Implement the exact deterministic candidate order**

Sort qualifying candidates by support margin descending, nonpush support
descending, point gap descending, captured time descending, fixed book index,
line ticks, then `OVER` before `UNDER`. Emit at most one total play per game.
If no direction is distinguishable, emit `AMBIGUOUS_DIRECTION` rather than
inventing a selection.

- [ ] **Step 5: Add every-game/reason/independence tests**

Cover 763/764/765 residual counts, integer pushes, half-run no-push, future and
stale prices, three-book coverage after filtering, different lines, exact ties,
zero/one/five-plus plays, all official games once, same-game ML+total allowed,
and no parlay field or wording.

- [ ] **Step 6: Run totals and legacy overlay regressions and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_totals_evaluator tests.test_daily_totals_market_overlay tests.test_experimental_totals tests.test_daily_totals tests.test_experimental_totals_sources -v
python3 -m ruff check mlb_backtest/multibook_totals_evaluator.py tests/test_multibook_totals_evaluator.py
git diff --check
git add mlb_backtest/multibook_totals_evaluator.py tests/test_multibook_totals_evaluator.py
git commit -m "feat: evaluate variable totals plays"
```

---

### Task 7: Closed combined report, trace-last persistence, and Markdown

**Files:**
- Create: `mlb_backtest/daily_multibook_market_plays.py`
- Create: `schemas/experimental_multibook_market_plays_report.schema.json`
- Create: `schemas/experimental_multibook_market_plays_trace.schema.json`
- Create: `tests/test_daily_multibook_market_plays.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class BuiltMarketPlays:
    report: Mapping[str, object]
    report_bytes: bytes
    sidecar_bytes: bytes
    markdown_bytes: bytes
    trace: Mapping[str, object]
    trace_bytes: bytes
    authorities: tuple[EvidenceAuthority, ...]

def validate_market_plays_report(value: object) -> dict[str, object]: ...
def validate_market_plays_trace(value: object) -> dict[str, object]: ...
def build_market_plays(root: Path, ml_trace: Path, totals_trace: Path, board: Path, *, evaluated_clock: Callable[[], str]) -> BuiltMarketPlays: ...
def persist_market_plays(root: Path, built: BuiltMarketPlays) -> tuple[Path, Path, Path, Path]: ...
def render_market_plays_markdown(payload: object) -> bytes: ...
```

- [ ] **Step 1: Write RED closed report/trace and rendering tests**

```python
def test_report_has_every_game_in_both_sections_and_no_rank(self):
    built = build_fixture()
    self.assertEqual(len(built.report["moneylines"]), len(OFFICIAL_GAMES))
    self.assertEqual(len(built.report["totals"]), len(OFFICIAL_GAMES))
    self.assertTrue(all("rank" not in row for section in ("moneylines", "totals") for row in built.report[section]))

def test_markdown_has_variable_play_sections_and_no_parlay_claim(self):
    text = render_market_plays_markdown(report_with_same_game_double_play()).decode()
    for label in ("RESEARCH ONLY", "PROVISIONAL", "NOT BET-VALIDATED", "ML PLAY", "TOTAL PLAY"):
        self.assertIn(label, text)
    self.assertNotIn("parlay", text.lower())
    self.assertNotIn("Top 3", text)
```

- [ ] **Step 2: Run combined-report tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_multibook_market_plays -v`

Expected: import failure because the combined report module is absent.

- [ ] **Step 3: Implement closed report and trace validators**

The report contains exact top-level keys for schema, content digest, labels,
target date, evaluated time, authority refs/digests, policy digest,
`moneylines`, and `totals`. Rows use canonical decimal strings for gate values
and fixed reason arrays. The trace binds the ML primary/trace and participant
components, totals report/trace/receipt, schedule receipt/payload, five capture
outcomes/raw files, board/sidecar, policy, JSON sidecar, and Markdown digest.

- [ ] **Step 4: Implement build and untrusted-transport reconstruction**

`build_market_plays` loads all authorities, takes the evaluation clock once,
evaluates both sections, and freezes canonical bytes. `persist_market_plays`
treats `BuiltMarketPlays` as untrusted: it re-loads all named authorities,
rebuilds using the original sealed evaluation time, and byte-compares the full
bundle before any write.

- [ ] **Step 5: Implement sidecar → JSON → Markdown → trace publication**

```python
write_existing_or_exclusive(trusted, sidecar_path, rebuilt.sidecar_bytes)
write_existing_or_exclusive(trusted, json_path, rebuilt.report_bytes)
write_existing_or_exclusive(trusted, markdown_path, rebuilt.markdown_bytes)
rebind_all_inputs_and_outputs(rebuilt)
write_existing_or_exclusive(trusted, trace_path, rebuilt.trace_bytes)
rebind_all_inputs_and_outputs(rebuilt)
```

Only a valid trace means success. Existing-identical bytes are idempotent;
every conflicting prefix is an integrity failure. A zero-play bundle is final
and is never retried merely because it contains no plays.

- [ ] **Step 6: Add full fault-injection and transport-forgery tests**

Fail each write, each input rebind, the pre-trace rebind, trace write, and final
rebind. Test forged report/Markdown/trace/authority refs, replay against changed
board, symlink/hardlink/directory/FIFO outputs, oversized existing outputs,
existing-identical reuse, prefix conflict, and proof that absence of trace is
not reported as success.

- [ ] **Step 7: Run schemas, combined tests, security regressions, and commit**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_multibook_market_plays tests.test_multibook_ml_evaluator tests.test_multibook_totals_evaluator -v
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
for name in ("experimental_multibook_market_plays_report.schema.json", "experimental_multibook_market_plays_trace.schema.json"):
    Draft202012Validator.check_schema(json.load(open("schemas/" + name)))
print("report schemas: PASS")
PY
python3 -m ruff check mlb_backtest/daily_multibook_market_plays.py tests/test_daily_multibook_market_plays.py
git diff --check
git add mlb_backtest/daily_multibook_market_plays.py schemas/experimental_multibook_market_plays_report.schema.json schemas/experimental_multibook_market_plays_trace.schema.json tests/test_daily_multibook_market_plays.py
git commit -m "feat: persist variable multibook market plays"
```

---

### Task 8: Offline CLI, operator handoff, compatibility, and acceptance

**Files:**
- Modify: `mlb_backtest/multibook_market_capture.py`
- Modify: `mlb_backtest/multibook_market_board.py`
- Modify: `mlb_backtest/daily_multibook_market_plays.py`
- Create: `tests/test_multibook_market_plays_e2e.py`
- Create: `docs/experimental_multibook_market_plays.md`
- Modify: `docs/experimental_live_top3.md`

The external collector handoff is explicit: the collector writes one raw JSON
and sidecar per available book or one unavailable-record JSON per unavailable
book. Repository CLIs admit those local files; they do not scrape sportsbooks.

**Commands:**

```text
python3 -m mlb_backtest.multibook_market_capture admit --root ROOT --raw-capture RELATIVE
python3 -m mlb_backtest.multibook_market_capture unavailable --root ROOT --record RELATIVE
python3 -m mlb_backtest.multibook_market_board admit-board --root ROOT --schedule-receipt RELATIVE --capture-outcome RELATIVE [exactly five]
python3 -m mlb_backtest.daily_multibook_market_plays evaluate --root ROOT --ml-trace RELATIVE --totals-trace RELATIVE --market-board RELATIVE
```

- [ ] **Step 1: Write RED CLI and clean-import tests**

```python
def test_evaluate_rejects_all_policy_and_market_overrides(self):
    for flag in ("--date", "--book", "--line", "--price", "--threshold", "--clock", "--url", "--game"):
        self.assertEqual(main(valid_argv() + [flag, "x"]), 2)

def test_success_prints_only_terminal_trace_relative_path(self):
    status, stdout, stderr = invoke(valid_evaluate_argv())
    self.assertEqual(status, 0)
    self.assertRegex(stdout, r"^reports/experimental_multibook_market_plays/.+\.trace\.json\n$")
    self.assertEqual(stderr, "")

def test_evaluator_imports_without_network_browser_or_model_runner(self):
    blocked = ("urllib.request", "requests", "selenium", "playwright", "sportsbook")
    self.assertCleanImport("mlb_backtest.daily_multibook_market_plays", blocked)
```

- [ ] **Step 2: Run E2E tests and confirm RED**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_multibook_market_plays_e2e -v`

Expected: CLI behavior is absent or incomplete.

- [ ] **Step 3: Implement strict relative-path CLIs and exit mapping**

Use `argparse`; root may be absolute, every evidence argument must be a strict
POSIX relative path below root. Reject absolute paths, `..`, empty components,
backslashes, symlinks, and alternate roots. Success prints one relative path.
Contract/integrity failure returns 2; operational failure and interrupt return
1; success returns 0.

- [ ] **Step 4: Add hermetic full-flow acceptance fixtures**

Create one sealed official schedule/participant set, ML primary+full score trace,
totals report+trace, five capture outcomes, and a market board in a temporary
root. Run capture admission → board admission → evaluation. Assert:

- every official game appears in both sections;
- output counts are driven only by gates for 0, 1, and 5+ scenarios;
- same-game ML and total plays remain independent;
- all source/model bytes are unchanged;
- writes occur only in new multibook namespaces;
- a zero-play run is terminal and idempotent;
- no legacy Top-3 or DraftKings-only artifact changes.

- [ ] **Step 5: Document the external collector handoff and daily runbook**

Document exact raw JSON examples for all five book IDs, unavailable records,
the four CLI commands, freshness/coverage semantics, all reason codes, output
interpretation, and the warning that the evaluator neither collects prices nor
places wagers. Mark `experimental_live_top3` as a preserved model authority,
not the final market decision report.

- [ ] **Step 6: Run the complete focused and regression suite**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_market_play_contracts \
  tests.test_multibook_market_capture \
  tests.test_multibook_market_board \
  tests.test_market_play_authorities \
  tests.test_multibook_ml_evaluator \
  tests.test_multibook_totals_evaluator \
  tests.test_daily_multibook_market_plays \
  tests.test_multibook_market_plays_e2e -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_live_top3_contracts tests.test_live_top3_service \
  tests.test_live_top3_trace tests.test_daily_top3 \
  tests.test_daily_totals_market_overlay tests.test_experimental_totals \
  tests.test_daily_totals tests.test_experimental_totals_sources -v
python3 -m ruff check mlb_backtest tests/test_market_play_contracts.py \
  tests/test_multibook_market_capture.py tests/test_multibook_market_board.py \
  tests/test_market_play_authorities.py tests/test_multibook_ml_evaluator.py \
  tests/test_multibook_totals_evaluator.py tests/test_daily_multibook_market_plays.py \
  tests/test_multibook_market_plays_e2e.py
git diff --check
```

Expected: all new focused tests and listed legacy regressions pass. If broader
discovery still lacks untracked `data/v5_1_bt/locked/predictions.jsonl`, report
that existing environmental blocker separately and do not fabricate it.

- [ ] **Step 7: Verify schemas and namespace cleanliness**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
import json
from jsonschema import Draft202012Validator
from pathlib import Path
for path in sorted(Path("schemas").glob("experimental_multibook_market_*.schema.json")):
    Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
print("multibook schemas: PASS")
PY
git status --short --untracked-files=all
git diff --stat 34bfe4d..HEAD
```

Verify that all pre-existing untracked evidence remains present, no model
module or legacy schema changed, and tracked changes are limited to this plan's
new modules/schemas/tests/docs plus the three named CLI modules.

- [ ] **Step 8: Commit the completed operator flow**

```bash
git add mlb_backtest/multibook_market_capture.py mlb_backtest/multibook_market_board.py mlb_backtest/daily_multibook_market_plays.py tests/test_multibook_market_plays_e2e.py docs/experimental_multibook_market_plays.md docs/experimental_live_top3.md
git commit -m "feat: complete variable multibook market plays"
```

---

## Review gates after every task

Use a fresh implementation agent per task. After each implementation commit:

1. run a spec-compliance review against the task and design;
2. fix every spec issue and re-review;
3. run a separate code-quality/security review;
4. fix every quality issue and re-review; and
5. independently rerun the task's focused tests before starting the next task.

Do not combine Tasks 2–4 or Tasks 5–7 in one worker. Their filesystem authority,
pure evaluation, and persistence trust boundaries must remain independently
reviewable.

## Release condition

Code completion does not fabricate live sportsbook availability. A live release
run requires contemporaneous externally collected captures from at least three
supported books for each market. If fewer than three valid books are available,
the honest final result is `PASS/INSUFFICIENT_BOOK_COVERAGE`, not a forced play.
