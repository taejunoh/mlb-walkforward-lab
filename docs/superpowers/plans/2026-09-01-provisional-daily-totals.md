# Provisional Daily MLB Totals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a sealed, research-only expected-total and uncertainty report for the 2026-09-01 MLB slate from the already authenticated D-1 schedule history and live schedule.

**Architecture:** A pure module owns chronological run-rate candidates, metrics, deterministic selection, and discrete residual distributions. A source adapter securely loads the existing validated `daily_top3` report and its exact `HISTORY_SCHEDULE` and `LIVE_SCHEDULE` payload bindings. A separate CLI validates, projects, and append-only publishes JSON, Markdown, trace, and digest without importing or changing ML scoring behavior.

**Tech Stack:** Python 3.11 standard library, existing `secure_fs`, `live_top3_contracts`, schedule parsers, `unittest`, JSON Schema Draft 2020-12, Ruff.

---

## File map

- Create `mlb_backtest/experimental_totals.py`: immutable game rows, rolling candidates, metrics, candidate selection, residual distribution, and optional line comparison.
- Create `mlb_backtest/experimental_totals_sources.py`: secure admission of one existing experimental daily report and its bound history/live schedule bytes.
- Create `mlb_backtest/daily_totals.py`: CLI, closed report validation, append-only persistence, Markdown, and trace.
- Create `schemas/experimental_daily_totals_model.schema.json`: closed selected-model receipt schema.
- Create `schemas/experimental_daily_totals_report.schema.json`: closed primary report schema.
- Create `tests/test_experimental_totals.py`: independent mathematical and chronological oracles.
- Create `tests/test_experimental_totals_sources.py`: source-binding, path, hash, and mutation rejection.
- Create `tests/test_daily_totals.py`: end-to-end CLI and publication contract.

### Task 1: Implement pure chronological totals math

**Files:**
- Create: `mlb_backtest/experimental_totals.py`
- Create: `tests/test_experimental_totals.py`

- [ ] **Step 1: Write RED tests for immutable rows and D-1 chronology**

Define synthetic final games spanning May through August and a Sep-1 target.
The tests must construct only mappings and compare against independently coded
oracles. Include duplicate gamePk, bool-as-int score/team/venue, negative run,
non-final status, target-date history, and future-row mutation cases.

```python
def test_target_or_future_mutation_cannot_change_same_day_prediction(self):
    history = make_history_through("2026-06-15")
    original = walk_forward_predictions(history, CANDIDATES, "2026-06-01", "2026-06-15")
    mutated = history + (make_game("2026-06-16", away_runs=99, home_runs=99),)
    self.assertEqual(
        original,
        walk_forward_predictions(mutated, CANDIDATES, "2026-06-01", "2026-06-15"),
    )
```

- [ ] **Step 2: Run RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_experimental_totals -v
```

Expected: FAIL because `mlb_backtest.experimental_totals` does not exist.

- [ ] **Step 3: Implement closed types and candidate formulas**

Create frozen `FinalTotalGame`, `TargetTotalGame`, `TotalProjection`,
`UnprojectedTotal`, `EvaluationMetrics`, `SelectedTotalsModel`, and
`LineProbabilities` dataclasses. A target result is a closed union: projected
rows have expected-run/distribution fields and no reason; unprojected rows have
only status `UNPROJECTED` plus one enumerated reason (`UNKNOWN_TEAM` or
`UNKNOWN_VENUE`) and no expected-run/distribution fields.
`UNKNOWN_TEAM` means either participant has zero admitted games before the
feature cutoff; `UNKNOWN_VENUE` means both teams are known but the venue has
zero admitted games before the cutoff. Team absence takes precedence when both
conditions hold. Add direct pure and mixed-slate end-to-end RED tests proving
the unknown row is unprojected while valid peer rows still publish.
Expose exactly:

```python
CANDIDATE_IDS = ("LEAGUE_30", "TEAM_30", "TEAM_VENUE_30")

def validate_final_games(rows: object, *, cutoff_date: str) -> tuple[FinalTotalGame, ...]: ...
def validate_target_games(rows: object, *, target_date: str) -> tuple[TargetTotalGame, ...]: ...
def project_game(history: tuple[FinalTotalGame, ...], game: TargetTotalGame, candidate_id: str) -> TotalProjection: ...
def walk_forward_predictions(history: tuple[FinalTotalGame, ...], candidate_ids: tuple[str, ...], start_date: str, end_date: str) -> dict[str, tuple[tuple[FinalTotalGame, TotalProjection], ...]]: ...
```

Implement the design's 30-calendar-day league/team rates, ten-game team
shrinkage, 60-day/twenty-game venue shrinkage, geometric ratios, and strict
`official_date < target_date` filter. Sort by
`(official_date, scheduled_start_utc, game_pk)` and reject duplicates before
any rate is calculated.

- [ ] **Step 4: Add RED metric, selection, and discrete distribution tests**

Use hand-calculated MAE, RMSE, zero/nonzero Poisson deviance, candidate tie,
nearest-rank quantiles, exact 80% interval coverage, non-negative rounding,
integer push, and half-line cases.

```python
def test_integer_line_reports_push(self):
    samples = (7, 8, 8, 9)
    self.assertEqual(
        compare_total_line(samples, 8),
        LineProbabilities(over=0.25, under=0.25, push=0.50),
    )
```

- [ ] **Step 5: Implement metrics and deterministic selection**

Expose exactly:

```python
def evaluate_predictions(rows: tuple[tuple[FinalTotalGame, TotalProjection], ...]) -> EvaluationMetrics: ...
def select_candidate(development: dict[str, tuple[tuple[FinalTotalGame, TotalProjection], ...]]) -> str: ...
def residual_samples(rows: tuple[tuple[FinalTotalGame, TotalProjection], ...], live_mean: float) -> tuple[int, ...]: ...
def compare_total_line(samples: tuple[int, ...], line: int | float) -> LineProbabilities: ...
```

Require at least 100 development rows in `select_candidate`. Eligibility is
strictly lower MAE and deviance than `LEAGUE_30`; sort eligible candidates by
`(mae, poisson_deviance, CANDIDATE_IDS.index(id))`. Implement all finite/range
checks before arithmetic and store no sportsbook field.

- [ ] **Step 6: Run GREEN and static checks**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_experimental_totals -v
python3 -m ruff check mlb_backtest/experimental_totals.py tests/test_experimental_totals.py
git diff --check
```

Expected: all tests and checks PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add mlb_backtest/experimental_totals.py tests/test_experimental_totals.py
git commit -m "feat: model provisional daily totals"
```

### Task 2: Admit exact authenticated schedule sources

**Files:**
- Create: `mlb_backtest/experimental_totals_sources.py`
- Create: `tests/test_experimental_totals_sources.py`

- [ ] **Step 1: Write RED source-admission tests**

Build a temporary trusted root containing a canonical validated daily report,
one `HISTORY_SCHEDULE` binding, one `LIVE_SCHEDULE` binding, their exact raw
payloads, manifests, receipts, and sidecars. Reject:

- zero or duplicate bindings for either role;
- wrong report target/cutoff/model label;
- absolute, `..`, backslash, alias, symlink, or outside-root paths;
- payload byte count/hash mutation and receipt/binding mismatch;
- live report binding `game_pks` unequal to the canonical parsed live
  population; the bulk-history binding instead requires its contract-defined
  empty `game_pks` and derives the admitted population from authenticated raw;
- historical duplicate gamePk, post-cutoff row, or malformed final score;
- target row whose date differs or whose game is not pregame.

```python
def test_loader_rejects_payload_mutation_after_report_validation(self):
    fixture = write_source_fixture(self.root)
    mutate_bytes(fixture.history_payload)
    with self.assertRaisesRegex(ValueError, "history schedule payload differs"):
        load_totals_sources(self.root, fixture.report)
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_experimental_totals_sources -v
```

Expected: FAIL because the source adapter does not exist.

- [ ] **Step 3: Implement one-root-capability admission**

Create frozen `AdmittedSourceIdentity` and `TotalsSourceBundle`. Each source
identity retains every role-defined immutable component and digest. History
uses claim, receipt, receipt sidecar, completion, completion sidecar, manifest,
manifest sidecar, and payload. Live uses the separately located observation
claim, receipt, completion, completion sidecar, manifest, manifest sidecar,
and payload; the existing live-observation contract has no claim or receipt
sidecar, so their absence is required rather than repaired or invented. Each identity
also retains binding digest, payload relative path/digest/byte count, and query
identity digest. The bundle retains both exact `HISTORY_SCHEDULE` and
`LIVE_SCHEDULE` identities, `target_date`, `cutoff_date`, `history_games`, and
`target_games`; no downstream layer may reduce these to payload digests alone.

Expose exactly:

```python
def load_totals_sources(root: Path, report_path: Path) -> TotalsSourceBundle: ...
```

Normalize and bind `root` once with `open_trusted_root`. Securely read and
rebind the report using `read_regular_snapshot`; validate its bytes with
`validate_run_payload`. Reconstruct and validate `SourceBinding` objects from
the report mappings. Derive exact component paths from the role's existing
receipt contract; securely read every component without calling a helper that
can complete or repair missing evidence. Require closed canonical claim and
completion mappings and a completion that binds the same receipt digest.
Validate every sidecar that the role actually defines and require absence of
sidecars the role does not define. Derive or bind the exact manifest filename
from the canonical query identity and payload SHA-256; verify canonical source,
request, payload digest, byte count, and row count fields. Reject missing,
extra, or inconsistent role-defined receipt-chain pieces.

For each selected binding, securely read its `payload_relative`, require exact
`byte_length` and `payload_sha256`, and rebind after parsing. Use
`parse_schedule` for history and
`parse_live_schedule(..., requested_date=target_date)` for target games.
Pass closed projections into Task 1 validators; never return raw JSON.
Require sport ID 1, MLB league identity, regular-season game type `R`, and an
unambiguous game type through the exact authenticated StatsAPI schedule request
(`sportId=1`, `gameType=R`, fixed dates) before admitting rows. If game-level
sport/league fields are present they must agree; StatsAPI payloads that omit
those redundant fields remain admissible because the authenticated request is
the authority. Add fixtures that reject a non-MLB request, spring/postseason
request, and missing or ambiguous request type before any rate calculation.
Raw schedule occurrences may repeat one `gamePk` across moved query dates;
apply the existing official schedule parser first, then require the canonical
parsed population to contain unique `game_pk`. Conflicting duplicates remain a
parser integrity failure. Exclude canonical rows after the cutoff before final
validation. Treat source statuses `Final` and `Completed Early` as completed
scored games and normalize both to the Task 1 internal `Final` status; reject
all other statuses from model history.

- [ ] **Step 4: Add before/open/post mutation tests and run GREEN**

Use existing test hooks or a narrow adapter hook to swap report/payload
identity after the first read. The loader must hold or reread/rebind every
role-defined claim, receipt, sidecar, completion, manifest, and payload after
all dependent reads. Add mutation tests for each component class, not only
report/payload, and require rejection before returning a bundle.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_experimental_totals_sources tests.test_experimental_totals -v
python3 -m ruff check mlb_backtest/experimental_totals_sources.py tests/test_experimental_totals_sources.py
git diff --check
```

Expected: all tests and checks PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add mlb_backtest/experimental_totals_sources.py tests/test_experimental_totals_sources.py
git commit -m "feat: admit provisional totals sources"
```

### Task 3: Seal and render the daily totals report

**Files:**
- Create: `schemas/experimental_daily_totals_model.schema.json`
- Create: `schemas/experimental_daily_totals_report.schema.json`
- Create: `mlb_backtest/daily_totals.py`
- Create: `tests/test_daily_totals.py`

- [ ] **Step 1: Write RED contract and end-to-end tests**

Freeze a deeply immutable `BuiltDailyTotals` value containing canonical bytes
for the selected-model receipt, daily report, and exact trace inputs plus their
fixed relative paths, so callers cannot mutate or substitute a nested mapping
independently.
Freeze the selected-model receipt keys and schema literal
`PROVISIONAL_DAILY_TOTALS_MODEL_V1`, and the primary report keys and schema
literal `PROVISIONAL_DAILY_TOTALS_V1`. The model receipt binds its model ID and
status labels, source report path and byte digest, history binding/payload
digests, cutoff, fixed windows, candidate-definition digest, all development
metrics, selected candidate, one-time final metrics, and canonical raw signed
development residual (`actual_total - predicted_total`) digest/count. Its
required `.sha256` sidecar binds the exact
receipt bytes. The model-receipt basename is selected during build, before
either object is sealed, as `<model-content-sha256>.json` under
`audit/experimental_daily_totals/model/`; its content digest is computed over
the canonical receipt mapping with only that digest field omitted. The primary
report binds this precommitted relative path and exact receipt byte digest. The
model receipt, report, and final trace all cross-bind the complete immutable
claim/receipt/completion/manifest/payload identity for both history and live
sources, not only payload hashes. Their `windows` object contains and validates
all six exact bounds: warm-up `2026-03-25..2026-05-31`, development
`2026-06-01..2026-07-31`, and final `2026-08-01..2026-08-31`.

Test canonical content digests, chronological windows, selected candidate,
development/final metrics, residual digest, source digests, projection rows,
and exact labels. Reject extra/missing keys, NaN/infinity, bool numbers, wrong
dates, noncanonical ordering, content digest mutation, and any betting/odds
field. Validate a canonical valid receipt/report against both JSON schemas.
Schema tests enforce closed structure, ordinary JSON types, nested extra-key
rejection, and projected/unprojected variants. Semantic validators separately
reject bool-as-number, NaN/infinity, and inconsistent recomputed content,
receipt, and source cross-digests because those equalities are not expressible
in Draft 2020-12 JSON Schema.

```python
def test_report_contains_no_betting_claim_or_market_field(self):
    payload = make_valid_report_payload()
    forbidden = {"pick", "odds", "edge", "playable_to", "profitability"}
    self.assertTrue(forbidden.isdisjoint(payload))
    self.assertEqual(validate_totals_report(payload)["status_labels"], [
        "RESEARCH ONLY", "ONE-SEASON PROVISIONAL", "NOT BET-VALIDATED"
    ])
```

- [ ] **Step 2: Run RED**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_daily_totals -v
```

Expected: FAIL because schema and CLI do not exist.

- [ ] **Step 3: Implement closed validation and orchestration**

Expose exactly:

```python
def validate_totals_model_receipt(value: object) -> dict[str, object]: ...
def validate_totals_report(value: object) -> dict[str, object]: ...
def build_daily_totals(root: Path, target_date: str, source_report: Path) -> BuiltDailyTotals: ...
def render_totals_markdown(payload: object) -> str: ...
def persist_daily_totals(root: Path, built: BuiltDailyTotals) -> tuple[Path, Path, Path, Path]: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

`build_daily_totals` calls Task 2 once, requires exact fixed windows, creates
walk-forward development rows for all candidates, selects once, evaluates the
selected candidate on August without reselection, refits only by extending
history through D-1, and returns a projected or closed unprojected result for
every admitted target game. Unknown team/venue produces `UNPROJECTED` without
affecting valid peers; any other projection failure rejects the run. Require at
least 100 exact-window rows independently in development and final evaluation.
Compute and report 80% interval coverage in addition to MAE, RMSE, and Poisson
deviance. For each final or live row, apply the same frozen signed development
residuals to that row's own predicted mean, then discretize and compute its
10th/90th percentiles; final coverage is the fraction of final actual totals
inside their own intervals. Test exact window membership, this independent
coverage oracle, and threshold failures before any write. Bind the canonical
raw residual multiset and each per-game discrete sample multiset by SHA-256.
Order report rows by `(official_date, scheduled_start_utc, game_pk)`. Every row
includes `feature_cutoff`, the bound history source digest, and closed
data-quality flags; unprojected rows contain none of the numeric projection or
sample fields.

The CLI is:

```text
python3 -m mlb_backtest.daily_totals --root ROOT --date YYYY-MM-DD --source-report RELATIVE_PATH
```

The source report must be below root and target-date matched. Exit 0 on a
complete report, 2 on integrity/contract failure, 1 on operational failure,
and 130 on interrupt.

- [ ] **Step 4: Implement append-only logical publication**

Use one held trusted root capability and existing secure-FS primitives.
Publish the model receipt sidecar then receipt under the append-only model
namespace before publishing the daily report. Derive the report basename from
execution UTC plus the first 16 characters of `content_sha256`. Publish JSON
sidecar, JSON primary, Markdown, then trace. The trace is the commit point and
binds the byte SHA-256 of the model receipt, JSON, sidecar, Markdown, and source
report. Its `content_sha256` is calculated over the canonical trace mapping
with that one field omitted; it is not a self-referential raw-file digest.
Reread and rebind every component before returning. An existing byte-identical
component may be reused; any difference fails. The persist function validates
the receipt/report cross-digests inside the single `BuiltDailyTotals` value
before its first write, rereads the source report and re-runs read-only source
admission to compare the complete history/live identities, validates the trace,
and uses only the precommitted model-receipt path. Tests substitute mismatched
objects, identities, or paths and require zero created files.

- [ ] **Step 5: Run focused, regression, schema, and formatting verification**

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_experimental_totals \
  tests.test_experimental_totals_sources \
  tests.test_daily_totals \
  tests.test_live_history \
  tests.test_daily_top3 -v
python3 - <<'PY'
import json
from pathlib import Path
from jsonschema import Draft202012Validator
for name in (
    "schemas/experimental_daily_totals_model.schema.json",
    "schemas/experimental_daily_totals_report.schema.json",
):
    Draft202012Validator.check_schema(json.loads(Path(name).read_text()))
PY
python3 -m ruff check \
  mlb_backtest/experimental_totals.py \
  mlb_backtest/experimental_totals_sources.py \
  mlb_backtest/daily_totals.py \
  tests/test_experimental_totals.py \
  tests/test_experimental_totals_sources.py \
  tests/test_daily_totals.py
git diff --check
```

Expected: unit/regression tests and Ruff PASS; schema is syntactically valid;
diff check is clean.

- [ ] **Step 6: Commit Task 3**

```bash
git add schemas/experimental_daily_totals_model.schema.json \
  schemas/experimental_daily_totals_report.schema.json \
  mlb_backtest/daily_totals.py tests/test_daily_totals.py
git commit -m "feat: publish provisional daily totals"
```

### Task 4: Run the Sep-1 projection and preserve evidence

Run this task only after the reviewed implementation commits are integrated
into the live worktree without modifying, staging, or deleting its existing
untracked evidence. The isolated implementation worktree does not contain the
live root's untracked authenticated schedule artifacts.

**Files:**
- Create append-only: `audit/experimental_daily_totals/model/`
- Create append-only: `reports/experimental_daily_totals/2026-09-01/`

- [ ] **Step 1: Run exactly once against the authenticated first Sep-1 report**

Before running, set `LIVE_ROOT` once to the verified physical absolute live
worktree path and reject any other value. Execute a read-only preflight that
uses that same `LIVE_ROOT`,
prints the exact source-report relative path and byte SHA-256, validates the
full source chain through `load_totals_sources`, and snapshots path/type/size/
SHA-256 for every existing untracked source component. Also snapshot `git
status --porcelain=v1 -uall` and both allowed output namespaces. Refuse to run
unless the only intended write destinations are
`audit/experimental_daily_totals/model/` and
`reports/experimental_daily_totals/2026-09-01/`.

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/taejunoh/.local/bin/python3 \
  -m mlb_backtest.daily_totals \
  --root "$LIVE_ROOT" \
  --date 2026-09-01 \
  --source-report reports/experimental_daily/2026-09-01/20260901T133444985203Z-b3e0217fb57a0ba5.json
```

Expected: exit 0 with 15 projected games and no Over/Under bet claim.

If the process exits without a valid final trace, preserve every partial file,
label the attempt incomplete, and stop. Do not silently choose a new timestamp
or rerun. Recovery requires explicit diagnosis of the preserved attempt first.

- [ ] **Step 2: Independently verify the sealed report**

Load the JSON through `validate_totals_report`, recompute the schema and
content digest, compare Markdown byte-for-byte with
`render_totals_markdown`, verify trace and all sidecar byte hashes, confirm
cutoff `2026-08-31`, target `2026-09-01`, 15 unique gamePk, and no market
fields. Compare the post-run untracked snapshot to the preflight and prove that
all pre-existing source bytes are unchanged and every new path is inside the
two allowed append-only namespaces.

- [ ] **Step 3: Report the projection with limitations**

List each projected total and 80% interval, the selected candidate, June-July
development metrics, August final metrics, and explicit one-season provisional
status. Do not say Over or Under until a timestamped external line is supplied
after model sealing.
