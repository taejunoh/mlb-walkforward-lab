# Prospective ML Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record one immutable, genuinely pregame ML probability observation per game/model without requiring totals or market availability, then later join supplied official MLB outcomes for descriptive calibration reporting.

**Architecture:** Extend the existing read-only ML authority just enough to expose the authenticated participant facts already parsed from bound `LIVE_PARTICIPANTS` evidence. A new observer writes immutable candidate snapshots and an O_EXCL primary-claim record; snapshots never import outcomes. A separate settlement/report module consumes official-result receipt/raw evidence offline, preserving revisions as immutable records and resolving only explicitly supplied revision heads.

**Tech Stack:** Python 3.11 standard library; existing `market_play_authorities`, live source receipt/binding format, `TrustedRoot`, `read_regular_snapshot`, `rebind_regular_snapshot`, `write_exclusive`, and canonical JSON/SHA-256 helpers; `unittest`; Ruff.

---

## Scope and file map

Do not change model math, frozen cards, completeness/PLAY gates, market policy, totals, collection scheduling, or runtime activation. Do not call external services in tests or in the settlement/report path. There are no commits in this approved work.

```text
Modify  mlb_backtest/market_play_authorities.py
        Expose authenticated p_home/model-card and exact participant fields already
        parsed from the bound schedule/boxscore payloads.
Create  mlb_backtest/prospective_ml_observation.py
        Outcome-free pregame candidate/primary snapshot construction and persistence.
Create  mlb_backtest/prospective_ml_settlement.py
        Offline official-result receipt/raw validation, immutable settlement revisions,
        and calibration-only joined report.
Create  schemas/prospective_ml_observation_snapshot.schema.json
Create  schemas/prospective_ml_observation_settlement.schema.json
Create  schemas/prospective_ml_observation_report.schema.json
Create  tests/test_prospective_ml_observation.py
Create  tests/test_prospective_ml_settlement.py
Modify  docs/experimental_live_top3.md
        Document research-only manual/offline commands and no-backfill boundary.
```

New files live only under `audit/prospective_ml_observation/` and
`reports/prospective_ml_observation/`:

```text
audit/prospective_ml_observation/candidates/{official_date}/{game_pk}/{model_card_sha256}/{snapshot_sha256}.json
audit/prospective_ml_observation/candidates/.../{snapshot_sha256}.sha256
audit/prospective_ml_observation/primary_claims/{game_pk}/{model_card_sha256}.json
audit/prospective_ml_observation/settlements/{snapshot_sha256}/{revision:08d}-{settlement_sha256}.json
audit/prospective_ml_observation/settlement_heads/{snapshot_sha256}/{revision:08d}.json
reports/prospective_ml_observation/{report_sha256}.json
reports/prospective_ml_observation/{report_sha256}.sha256
```

The candidate is immutable evidence. The O_EXCL primary claim is the sole
primary-selection commit: it contains `snapshot_relative`, `snapshot_sha256`,
`game_pk`, `official_date`, `model_card_sha256`, and internally observed
`claimed_at_utc`. A later candidate can never replace it. A settlement head is
also O_EXCL at a fixed snapshot/revision path, so two corrections cannot
silently claim the same next revision.

### Task 1: Enrich the existing sealed ML authority without changing decisions

**Files:**
- Modify: `mlb_backtest/market_play_authorities.py`
- Modify: `tests/test_market_play_authorities.py`

- [x] **Step 1: Add focused failing authority tests**

Build from the existing sealed live fixture and assert that `load_ml_authority()`
returns, for each parsed participant game, exactly these authenticated values:

```python
game = authority.by_game[1]
assert game.p_home == Fraction("0.61")
assert authority.model_card_sha256 == MODEL_CARD_SHA
assert game.participant.away_lineup == tuple(range(201, 210))
assert game.participant.home_lineup == tuple(range(101, 110))
assert game.participant.away_observed_starter_id == 501
assert game.participant.home_observed_starter_id == 601
assert game.participant.away_expected_starter_id == 501
assert game.participant.home_expected_starter_id == 601
assert game.participant.payload_relative.as_posix().endswith(".raw")
assert game.participant.payload_sha256
```

Also make a fixture with a malformed/missing participant binding and assert its
lineups, starter IDs, and payload reference are null/empty while `complete` is
false; it must not invent a participant value.

- [x] **Step 2: Run the authority test RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_market_play_authorities -v
```

Expected: the new authority fields are absent.

- [x] **Step 3: Implement only extracted evidence fields**

Add defaulted fields to `ParticipantGateEvidence` so existing test construction
continues to work:

```python
away_lineup: tuple[int, ...] = ()
home_lineup: tuple[int, ...] = ()
away_observed_starter_id: int | None = None
home_observed_starter_id: int | None = None
away_expected_starter_id: int | None = None
home_expected_starter_id: int | None = None
payload_relative: Path | None = None
payload_sha256: str | None = None
receipt_relative: Path | None = None
receipt_sha256: str | None = None
durable_at_utc: str | None = None
```

Extend `MlGameAuthority` with `p_home: Fraction | None` and `MlAuthority` with
`model_card_sha256: str`. In `_participant_evidence()`, use the already loaded
binding and `parse_pregame_boxscore()`/`compare_probable_pitchers()` result;
copy the ordered IDs and binding references only after their existing digest
checks. In `_ml_games()`, source `p_home` from the validated trace row. Do not
calculate a new probability or alter `complete`, `starter_consistent`,
`late_scratch`, or completeness behavior.

- [x] **Step 4: Run focused authority verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_market_play_authorities tests.test_multibook_market_plays_e2e -v
python3 -m ruff check mlb_backtest/market_play_authorities.py tests/test_market_play_authorities.py
git diff --check
```

Expected: all pass; no existing authority output changes except the newly
available in-memory fields.

### Task 2: Add outcome-isolated ML-first candidate and primary claim

**Files:**
- Create: `mlb_backtest/prospective_ml_observation.py`
- Create: `schemas/prospective_ml_observation_snapshot.schema.json`
- Create: `tests/test_prospective_ml_observation.py`

- [x] **Step 1: Write RED snapshot tests**

Create a real, sealed temporary ML authority fixture rather than mocking
`load_ml_authority()`: use `tests/fixtures/live_top3/schedule_complete*.json`
and `boxscore_complete*.json` to write the exact raw payload, manifest,
receipt, completion/sidecar, primary live report, and scored all-slate trace
that the existing authority validates. Use a fixed clock. Tests must cover:

```python
built = build_ml_observations(root, ml_trace, observed_clock=lambda: "2026-09-03T18:00:00Z")
paths = persist_ml_observations(root, built, primary_clock=lambda: "2026-09-03T18:00:01Z")
assert paths.primary_game_pks == (1,)
row = load_ml_snapshot(root, paths.primary_snapshots[0])
assert row["market_status"] == "NOT_EVALUATED"
assert row["totals_status"] == "NOT_EVALUATED"
assert row["p_home"] == "0.61"
assert row["feature_completeness"] == "0.40"  # still primary if participants are confirmed
assert row["away_lineup"] == list(range(201, 210))
```

Add independent tests for: no market/totals files needed; a valid `<0.50`
model row remains eligible; unscored/non-finite, incomplete lineup, starter
conflict, late scratch, missing participant binding, and observed time at/after
start create no primary claim; a build before start followed by persistence
after start cannot claim primary; same bytes retry is idempotent; a later
distinct candidate is diagnostic; two concurrent calls leave exactly one
primary claim; trace/source mutation before final rebind fails. Add behavioral
boundary seams: make any attempted read beneath settlements/official-results
raise and make a network seam raise; observer build/persist must still succeed.

For optional market evidence, provide an existing valid report/trace fixture
whose ML trace path and SHA exactly match. It is copied as
`market_status: "EVALUATED"` with verbatim report decision fields. A supplied
mismatched/malformed trace raises; absence is `NOT_EVALUATED`, never PASS.

- [x] **Step 2: Run RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_prospective_ml_observation -v
```

Expected: import failure.

- [x] **Step 3: Implement the closed observer API**

Implement exactly:

```python
@dataclass(frozen=True, slots=True)
class BuiltMlObservations:
    ml_trace_relative: Path
    market_trace_relative: Path | None
    recorded_at_utc: str
    candidates: tuple[Mapping[str, object], ...]
    candidate_bytes: tuple[bytes, ...]
    root: Path

@dataclass(frozen=True, slots=True)
class PersistedMlObservations:
    candidate_snapshots: tuple[Path, ...]
    primary_snapshots: tuple[Path, ...]
    diagnostic_snapshots: tuple[Path, ...]
    primary_game_pks: tuple[int, ...]

def build_ml_observations(
    root: Path, ml_trace: Path, *, market_trace: Path | None = None,
    observed_clock: Callable[[], str],
) -> BuiltMlObservations: ...

def persist_ml_observations(
    root: Path, built: BuiltMlObservations, *, primary_clock: Callable[[], str],
) -> PersistedMlObservations: ...

def validate_ml_snapshot(value: object) -> dict[str, object]: ...
def load_ml_snapshot(root: Path, relative: Path) -> dict[str, object]: ...
```

`build_ml_observations()` calls `load_ml_authority()` once, uses
`canonical_utc()` for its internally supplied execution clock, and rebinds the
authority before returning. It never imports `outcomes`, the settlement module,
network/browser code, or totals evaluator. It writes no file.

`persist_ml_observations()` treats `BuiltMlObservations` as untrusted transport:
it rebuilds from its stored relative trace paths and sealed `recorded_at_utc`,
then requires every canonical candidate byte to match before it writes. Each
canonical snapshot has closed fields: schema/content SHA, `recorded_at_utc`,
game/date/team/start identity, model-card SHA, ML trace relative/SHA,
`p_home`, predicted side, pick probability, decimal-string feature completeness,
ordered lineups, expected/observed starter IDs, participant reason codes and
payload/receipt binding paths+hashes, plus `market_status`, `totals_status`, and
nullable optional market-trace/report/board/quote projection. Reject an
eligible row unless probability is finite and `[0,1]`, both lineups are exactly
nine distinct positive IDs, expected and observed starter IDs match, the
existing participant flags are complete/consistent/non-scratch, and
`recorded_at_utc < scheduled_start_utc`. This predicate deliberately does not
test feature completeness or market PLAY.

When `market_trace` is supplied, validate it with
`validate_market_plays_trace()` and `validate_market_plays_report()`, verify
the report's `ml_trace_relative` and SHA against the supplied ML trace bytes,
and copy only the report row for the same game. It must not build a board,
evaluate a quote, or synthesize PASS/PLAY. It is optional at this exact call;
there is no attach/update API afterward.

`persist_ml_observations()` uses `open_trusted_root`, `write_exclusive`,
`read_regular_snapshot`, `rebind_regular_snapshot`, `sidecar_bytes`, and
`content_digest`. Write candidate sidecar then candidate JSON. For each
eligible candidate, attempt the fixed primary-claim path with `write_exclusive`:
the first successful claim is primary; an identical existing claim is an
idempotent primary retry; a different valid existing claim classifies this
candidate as diagnostic. This O_EXCL claim is the anti-backfill/atomicity
boundary—never select by a caller timestamp, directory scan, or overwrite.
Immediately before each sole primary-claim write, read and canonicalize
`primary_clock()` and require it is strictly before that game's start. The
production CLI binds this to the local UTC clock; injection is test-only. A
candidate may already exist if this second check fails, but no late primary
claim may be written. Rebind all ML authority inputs and every output before
return.

- [x] **Step 4: Run observer verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_prospective_ml_observation tests.test_market_play_authorities -v
python3 -m ruff check mlb_backtest/prospective_ml_observation.py tests/test_prospective_ml_observation.py
git diff --check
```

Expected: all pass, including zero-market and low-feature-completeness cases.

### Task 3: Admit official results offline and preserve settlement corrections

**Files:**
- Create: `mlb_backtest/prospective_ml_settlement.py`
- Create: `schemas/prospective_ml_observation_settlement.schema.json`
- Create: `tests/test_prospective_ml_settlement.py`

- [x] **Step 1: Write RED result/settlement tests**

Construct raw final-result bytes plus the existing live-source-style receipt,
manifest/sidecar, and completion evidence in a temp trusted root. The receipt
must carry the stats-API requested/final URL, status 200, response completion
time, durable time, payload path/SHA/length, manifest SHA, receipt SHA, and
canonical query identity; tests must fail if any is caller-only prose or any
path/digest/time is mismatched.

Test `FINAL` with a winner matching the sealed away/home teams; test explicit
`PENDING`, `POSTPONED`, `SUSPENDED`, `TIE`, and `NO_CONTEST`; malformed/unknown
game/date/team/winner rejects. Behavioral seams make scoring, market, and
network entrypoints raise while settlement/report still work from sealed paths.
Settlement may import only the outcome-free observer snapshot reader/validator.
Assert a second identical
settlement is idempotent, a correction needs the exact prior settlement and
creates revision 2, and two different revision-2 attempts cannot both create a
head. No nonfinal status contributes a loss or a calibration row.

- [x] **Step 2: Run RED**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_prospective_ml_settlement -v
```

Expected: import failure.

- [x] **Step 3: Implement receipt reader and immutable settlement API**

Implement exactly:

```python
def load_official_result_evidence(root: Path, receipt: Path) -> OfficialResultEvidence: ...
def build_settlement(root: Path, snapshot: Path, result_receipt: Path, *, prior: Path | None = None) -> BuiltSettlement: ...
def persist_settlement(root: Path, built: BuiltSettlement) -> Path: ...
def validate_settlement(value: object) -> dict[str, object]: ...
def build_calibration_report(root: Path, primary_claims: Sequence[Path], settlement_heads: Sequence[Path]) -> BuiltCalibrationReport: ...
def persist_calibration_report(root: Path, built: BuiltCalibrationReport) -> Path: ...
```

`load_official_result_evidence()` is offline only. Reuse the live-source
receipt/manifest/completion evidence shape and safe reading/rebinding pattern;
accept only `stats_api` HTTPS URLs under the existing source allowlist and an
official-result query identity containing the target `game_pk`. Read the raw
payload through its receipt binding and parse only final game status, official
date, team identity, and winner/terminal state. Do not call
`observe_final_game_schedule()`, `fetch_live_http()`, or any network function.

Settlement fields include snapshot relative/SHA, result receipt/raw/manifest
paths and hashes, terminal state, winner-or-null, result observed/durable UTC,
revision, prior settlement SHA-or-null, and content SHA. A first settlement is
revision 1. A correction must name and validate revision `n-1`; persist its
immutable record before an O_EXCL revision-head record at exactly
`settlement_heads/{snapshot_sha256}/{revision:08d}.json`. The fixed head
contains the chosen record reference; equal retry succeeds, divergent claim
conflicts. Rebind snapshot, source evidence, prior record, record, and head.
No mutable "current result" file and no directory/glob discovery.

### Task 4: Add descriptive report, schema checks, and operator documentation

**Files:**
- Modify: `mlb_backtest/prospective_ml_settlement.py`
- Create: `schemas/prospective_ml_observation_report.schema.json`
- Modify: `tests/test_prospective_ml_settlement.py`
- Modify: `docs/experimental_live_top3.md`

- [x] **Step 1: Write RED report tests**

With exact synthetic primary claims and settlement heads, assert one row per
primary `(game_pk, model_card_sha256)`, revision-head resolution, and hand
calculated metrics:

```python
assert report["all_primary"]["count"] == 2
assert report["all_primary"]["accuracy"] == "0.500000"
assert report["all_primary"]["brier"] == "0.225000"
assertAlmostEqual(
    float(report["all_primary"]["log_loss"]),
    (-math.log(0.70) - math.log(0.40)) / 2,
    places=12,
)
assert report["feature_completeness_lt_0_50"]["count"] == 1
assert report["feature_completeness_gte_0_50"]["count"] == 1
```

The fixture is p_home `.70`/home win and p_home `.40`/home win: one correct
side pick, Brier `(.30**2 + .60**2) / 2`, and the displayed log loss. Use a
test value generated with `math.log` and a documented tolerance; report
canonical output may retain decimal strings.
Add empty-cohort assertions for null metrics (not zero), repeated diagnostics
excluded, separate model cards never pooled, nonfinal states counted in
coverage but excluded from metrics, missing settlement counted pending, and
fixed bins `[0.0,0.1) ... [0.9,1.0]` with a closed final bin.

- [x] **Step 2: Implement the report and closed schema**

`build_calibration_report()` accepts explicit primary-claim and settlement-head
paths—never glob directories. It validates and rebinds each object, rejects
duplicate primary identities or a head that is not a contiguous settlement
chain, selects only `FINAL` rows with a valid winner, and computes accuracy,
Brier, log loss, sample count, and fixed 0.1 reliability bins separately for
each model-card SHA and each of `all_primary`, `feature_completeness_lt_0_50`,
and `feature_completeness_gte_0_50`. Reuse `mlb_backtest.metrics.brier`,
`log_loss`, and `wilson`: validate every probability is finite and in `[0,1]`
before cohort counting; `log_loss` then uses the existing `[0.001, 0.999]`
clipping for exact 0/1 rather than silently dropping those sealed rows or
serializing infinity. Use `Decimal`/canonical strings where possible and fixed
serialization precision for log loss. Include descriptive Wilson-95 bounds for
accuracy and each nonempty reliability bin with a dependence/no-promotion
caveat. Empty metrics are null.
The report has no price, return, ROI, no-vig, O/U, threshold, or promotion
field. Wilson intervals are descriptive uncertainty only.

Persist report sidecar then canonical JSON with `write_exclusive`; include the
explicit claim/head lists and their hashes so a rerun is reproducible. Document
two offline commands: snapshot from a sealed ML trace, then later report from
explicit claims/heads. State that omitted market input is NOT_EVALUATED, that
only post-activation O_EXCL primary claims are prospective, and that market
profit awaits a separately authorized captured-PLAY extension.

- [x] **Step 3: Run final bounded verification**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest \
  tests.test_market_play_authorities \
  tests.test_prospective_ml_observation \
  tests.test_prospective_ml_settlement -v
python3 -m ruff check \
  mlb_backtest/market_play_authorities.py \
  mlb_backtest/prospective_ml_observation.py \
  mlb_backtest/prospective_ml_settlement.py \
  tests/test_market_play_authorities.py \
  tests/test_prospective_ml_observation.py \
  tests/test_prospective_ml_settlement.py
git diff --check
```

Expected: all focused tests, Ruff, and diff check pass. Do not run historical
calibration, outcomes, H24, long suites, external capture, or activation.

## Plan self-review

- Existing ML trace/participant facts are reused and enriched in Task 1; no
  duplicate parser is introduced.
- Task 2 is outcome-free and permits valid low-completeness, no-market ML
  observations while preserving existing market gates.
- Task 3 accepts only receipt-bound official MLB raw evidence and keeps result
  revisions outside immutable pregame snapshots.
- Task 4 counts a claimed game once, keeps model versions/feature strata
  separate, and emits descriptive metrics only. No minimum sample, threshold,
  model promotion, betting return, or O/U evaluation is introduced.

## Execution and review

The user has approved the written design and requested the standing
orchestrator/Terra/Luna workflow. Task 1 has a separate file owner. Tasks 2–4
form one cohesive implementation block with a stable Task 1 API, so they can
start independently without editing Task 1 files. Each completed block receives
specification review followed by code-quality review; controller integration
tests follow. No additional execution-choice prompt or commit is required.

Baseline: 20 authority, market board/capture and daily-report tests passed on
HEAD `3ab6cd4c3ca878fccaaaf155a7adb8c33c4a185d`. Existing dirty edits and all
prior untracked evidence are preserved.

Task 1: independent specification and quality reviews passed. Controller reran
`tests.test_market_play_authorities` and `tests.test_multibook_market_plays_e2e`:
13 tests passed. The quality review's minor fixture-authentication coverage note
is assigned to the real-loader prospective integration tests.

## Final verification — 2026-09-02

Implementation is complete; runtime activation and real prospective capture
are not performed. Specification review and independent final quality review
passed after correcting the concrete admission, provenance, and final-rebind
findings. No model, weights, PLAY gate, collection schedule, or source evidence
was changed; no commit/push/merge/PR was made.

Controller's final command passed **48 tests** across:

- `tests.test_market_play_authorities`
- `tests.test_multibook_market_plays_e2e`
- `tests.test_daily_multibook_market_plays`
- `tests.test_multibook_market_board`
- `tests.test_multibook_market_capture`
- `tests.test_prospective_ml_observation`
- `tests.test_prospective_ml_settlement`
- `tests.test_prospective_ml_integration`
- `tests.test_prospective_ml_report_integrity`
- `tests.test_prospective_ml_persistence_integrity`

Ruff and `git diff --check` passed. All three schemas passed Draft 2020-12
meta-validation; actual synthetic primary, diagnostic, settlement, nonempty,
pending, and empty report artifacts validated. The hermetic CLI smoke exercises
snapshot → settle → report with real sealed loaders. No real game outcome,
historical calibration/holdout, long suite, or external capture was run.

Failure boundary: `write_exclusive` preserves already-created diagnostic
sidecars/records. Tests require an exception and no successful return/committed
settlement head after failed final source revalidation, not deletion or atomic
absence of every JSON file. Independent first-write mutation reproductions
confirmed both settlement and report reject changed sources. Failed evidence
is not a sample and must not be selected by globbing.

Preservation checks: HEAD remains
`3ab6cd4c3ca878fccaaaf155a7adb8c33c4a185d`; the pre-existing multi-book operator
document and both frozen v5.1 model-card SHA-256 digests match the baseline.
The next operational step is a new, truly pregame sealed-input observation;
historical reports cannot be backfilled as prospective observations.
