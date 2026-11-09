# Postseason ML Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evaluate the complete official postseason slate with the frozen ML model and accumulate separately authenticated, genuinely prospective research evidence, without market recommendations or totals.

**Architecture:** Add one immutable competition policy and explicit research branches around the existing acquisition, feature and scoring boundaries. Keep legacy contracts untouched; a strict research report/trace authority feeds a separate append-only prospective cohort. Three owners may implement against the interfaces below in parallel; integration depends on all three, not on placeholder unscored output.

**Tech Stack:** Existing Python standard-library modules, dataclasses, unittest, canonical SHA-256 JSON, trusted-root filesystem primitives and injected offline fetchers/clocks.

**Spec:** `docs/superpowers/specs/2026-09-29-postseason-ml-research-design.md`

## Global Constraints

- Scope is `postseason-research-v1`; policy is `POSTSEASON_ML_RESEARCH_V1`.
- Target types: `F`, `D`, `L`, `W`; same-season recent history: `R`, `F`, `D`, `L`, `W` through D−1 in `America/New_York`.
- Always display `RESEARCH_ONLY`, `POSTSEASON_OUT_OF_DOMAIN`, `NOT_POSTSEASON_VALIDATED` and `PASS — POSTSEASON_NOT_VALIDATED`.
- Market recommendation is `NOT_EVALUATED`; totals is `NOT_EVALUATED — POSTSEASON_TOTALS_NOT_SUPPORTED`; shadow is `NOT_EVALUATED_FOR_POSTSEASON`.
- Do not edit `normalize.py`, `scoring.py`, `scoring_prep.py`, `live_model_card.py`, frozen model-card artifacts or existing feature formulas/window lengths/sample thresholds.
- Preserve default R behavior and exact existing V1 artifacts/digests; no migration or permissive V1 upgrade.
- Work only under `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`; preserve all pre-existing edits. No Documents access, public-network execution, prohibited temporary-manifest access, sudo, commits, push, merge, PR, privileged/custody/production operations or automation changes.
- User has approved design implementation and tests without another intermediate approval. This plan does not authorize activation or retroactive claims.
- Test artifacts belong in test-managed directories inside the authorized worktree. Do not run the unrestricted repository test suite; coordinator selects safe focused tests.

## Review Focus

- A missing played game earlier than D−1 must invalidate combined-history coverage, even when the D−1 subset is complete (Task 1).
- Empty slates, unsupported observed rows and an exact-neutral score must remain explicit, finite and complete, without inventing a winner or enforcing three picks (Task 2).
- A rewritten report/trace that removes scope must still fail against the immutable raw source query identities (Task 2).
- An acquisition/recording clock equal to scheduled start, or a game starting between reads and persistence, must never yield a primary claim (Tasks 2/3).
- A suspended game that later finishes, a terminal tie/no contest, and an official correction must follow distinct pending/excluded/revision paths without duplicate claims (Task 3).

---

## Shared Interfaces and Ownership

All paths below are relative to the authorized worktree. No worker edits another owner's files. Task 1 owns acquisition/feature modules, Task 2 owns research artifacts/daily entry point, Task 3 owns research prospective modules/daily-flow dispatch. Existing tests remain owned by the corresponding module owner. Ask the coordinator before crossing these boundaries.

Task 1 adds `mlb_backtest/competition_policy.py`:

```python
@dataclass(frozen=True)
class CompetitionPolicy:
    competition_scope: str
    research_policy_version: str | None
    target_game_types: tuple[str, ...]
    recent_game_types: tuple[str, ...]

REGULAR_SEASON_V1: CompetitionPolicy
POSTSEASON_ML_RESEARCH_V1: CompetitionPolicy
def resolve_competition_policy(scope: str) -> CompetitionPolicy: ...
def history_population(policy: CompetitionPolicy) -> dict[str, object]: ...
```

The regular scope name is `regular-season`; its tuples are `('R',)` and its research version is `None`. The research tuples have exact order `('F','D','L','W')` and `('R','F','D','L','W')`. Reject arbitrary policy instances differing from these constants; this is not a tournament framework.

`history_population(POSTSEASON_ML_RESEARCH_V1)` returns exactly:

```json
{"recent_game_types":["R","F","D","L","W"],"season":"TARGET_SEASON","cutoff":"D_MINUS_1_AMERICA_NEW_YORK","window_definitions":"UNCHANGED_FROZEN_V5_1","career_bf_population":"REGULAR_SEASON","league_era_population":"REGULAR_SEASON"}
```

Task 1 appends the keyword-only argument `competition_policy: CompetitionPolicy = REGULAR_SEASON_V1` to these existing functions, leaving every existing positional argument, default and return type unchanged:

- `live_sources.parse_live_schedule`, `observe_live_slate`, `observe_final_game_schedule`.
- `live_history.build_live_history`, `load_live_career_bf` (career BF request population remains R; its new observation binding records the consuming research policy).
- `live_features.prepare_live_feature_history`, `build_live_raw_game_features`; include policy in the prepared authority seal/check so a prepared R bundle cannot be consumed in research mode.

Existing return classes `LiveSlateObservation`, `FinalGameObservation`, `FinalGameScheduleDisposition`, `LiveHistoryBundle`, `SourceBinding`, `PreparedLiveFeatureHistory` remain reusable. Research query identities carry `competition_scope`, `research_policy_version`, `history_population`; regular queries do not gain any fields. Official `game_type` stays intact on all normalized games. Unsupported target rows returned by the source are retained but never trigger participant acquisition/scoring.

Integration clarification: the regular `LiveHistoryBundle` and its V1 validator remain untouched. Task 1 adds `ResearchLiveHistoryBundle` with the same consumed attributes and an exact eight-key research authority: `namespace`, `cutoff_date`, `catalog_sha256`, `competition_scope`, `research_policy_version`, `history_population`, `coverage`, `coverage_sha256`. Its namespace is `experimental_daily/history_cache_postseason_research_v1`; `validate_research_history_authority` strictly verifies the policy, population, complete range evidence and coverage digest. Research-aware return annotations add this type; regular returns remain identical. Never use a false regular namespace or bypass the V1 constructor. Bind the same research policy, population, complete range evidence and coverage digest independently in source query identities. Both research feature preparation and artifact loading verify the authority and source-bound evidence; namespace alone proves no coverage.

Research `VERIFIED` means the existing prospective eligibility predicate passes: scored/non-neutral probability and winner fields, complete authenticated participant evidence, consistent non-null expected/observed starters, no late scratch, two ordered nine-distinct-player lineups, and durable observation before start. The existing prospective predicate has no numerical data-completeness threshold; do not invent one. Research scope/type/source checks and strict final pre-start clocks are additional requirements. `PRELIMINARY` remains diagnostic if that eligibility predicate fails.

Task 2 creates `mlb_backtest/postseason_research_contracts.py` and exports:

```python
@dataclass(frozen=True)
class LoadedPostseasonResearch:
    report: Mapping[str, object]
    trace: Mapping[str, object]
    trace_relative: Path
    trace_sha256: str

def validate_postseason_report(value: object) -> dict[str, object]: ...
def validate_postseason_trace(value: object) -> dict[str, object]: ...
def load_postseason_research(root: Path, trace: Path | str) -> LoadedPostseasonResearch: ...
```

The loader returns detached immutable mappings only after validating report, trace, card and every referenced observation/raw/manifest/receipt/completion file, their canonical digests and schedule/participant/policy joins. Call it again immediately before prospective publication to rebind authority; no mutable cache is authoritative.

Task 2 creates `mlb_backtest/postseason_research_service.py`:

```python
def build_postseason_research_run(
    *, slate: LiveSlateObservation,
    final_schedule_observer: Callable[[int, str], FinalGameObservation | FinalGameScheduleDisposition],
    history: LiveHistoryBundle, card: FrozenLiveCard,
    career_bf_by_pitcher: Mapping[int, int], career_bf_bindings: tuple[SourceBinding, ...],
    command_inputs: Mapping[str, object], execution_times: ExecutionTimes,
    trace_sink: list[dict[str, object]] | None = None,
) -> dict[str, object]: ...
```

`trace_sink` receives one exact trace row per scored game, including an exact-neutral game if existing scorer computes one; report state/winner semantics still match existing neutral handling. Task 2 adds `competition_scope: str = 'regular-season'` as a keyword to `daily_top3.run_daily_top3`, preserving its `(payload, json_path, markdown_path)` return. The research `.trace.json` path is the returned JSON path with suffix replaced by `.trace.json`.

Task 3 creates `mlb_backtest/prospective_ml_postseason_research.py`:

```python
def run_research_pregame(root: Path | str, ml_trace: Path | str,
                         *, clock: Callable[[], str]) -> PregameResult: ...
def run_research_postgame(root: Path | str, pregame_receipt: Path | str,
                          *, previous_postgame_receipt: Path | str | None = None,
                          fetcher=None, durable_clock=None) -> PostgameResult: ...
def read_research_status(root: Path | str, pregame_receipt: Path | str,
                         *, postgame_receipt: Path | str | None = None,
                         clock: Callable[[], str]) -> dict[str, object]: ...
```

Reuse existing `PregameResult`/`PostgameResult` return shapes from `prospective_ml_daily_flow` without a top-level circular import (lazy dispatch or TYPE_CHECKING imports). Task 3 owns a separate settlement helper module if needed, not Task 2's contracts file. Flow functions/CLI accept `competition_scope='regular-season'`; explicit research dispatch occurs before invoking any market/totals/shadow code. A scope inconsistent with artifact schema is an error, not automatic fallback.

## Exact Research Contracts

Task 2 owns report/trace contracts; Task 3 owns snapshot/claim/receipt/settlement/status contracts. Every listed key is required; no additional keys are accepted. Nested structures reused from V1 retain their exact existing field and type validations, not arbitrary dict acceptance. Canonical content SHA-256 excludes only its own `content_sha256` field. File SHA-256 remains a separate byte-level digest.

Common envelope keys `POLICY_KEYS` are `competition_scope`, `research_policy_version`, `history_population`, `validation_status`, `research_status`, `domain_status`; fixed values are the approved scope/policy/population above and `NOT_POSTSEASON_VALIDATED`, `RESEARCH_ONLY`, `POSTSEASON_OUT_OF_DOMAIN`.

- Report schema `POSTSEASON_ML_RESEARCH_REPORT_V1`: `schema`, all `POLICY_KEYS`, `model_label`, `target_date`, `command_inputs`, `executed_at_eastern`, `executed_at_utc`, `history_cutoff_date`, `cache_authority`, `model_card_sha256`, `source_bindings`, `games`, `warnings`, `market_status`, `market_reason`, `totals_status`, `totals_reason`, `content_sha256`. Command keys are exactly `date`, `model_card`, `competition_scope`.
- Report game row: `game_pk`, `game_type`, `target_date`, `away_team_id`, `home_team_id`, `away_team`, `home_team`, `scheduled_start_utc`, `state`, `reason_codes`, `source_binding_sha256s`, `observation_boundary_utc`, `model_card_sha256`, `feature_sha256`, `p_home`, `predicted_side`, `predicted_winner`, `pick_probability`, `data_completeness`, `starter_edge_abs`, `starter_provenance`, `lineup_provenance`, `schedule_quality_flags`. Unavailable model/feature/boundary fields are null, never fabricated; scored numeric fields are finite and probability-consistent. Quality flags are always an array. There is no rank/top-three field.
- Trace schema `POSTSEASON_ML_RESEARCH_TRACE_V1`: `schema`, all `POLICY_KEYS`, `not_a_betting_recommendation` (true), `primary_report_relative`, `primary_report_content_sha256`, `model_card_sha256`, `games`, `content_sha256`.
- Trace row: exactly existing `live_top3_trace._ROW_KEYS` minus `primary_rank`, `disposition`, plus `game_type`, `away_team_id`, `home_team_id`, `state`, `observation_boundary_utc`, `source_binding_sha256s`, `away_qualities`, `home_qualities`, `edge`. Existing optional quality/edge fields are mandatory here. Recompute feature digest and frozen scoring outputs; join to report and official source identity. Never call the V1 trace validator by stripping fields.
- Snapshot schema `PROSPECTIVE_ML_POSTSEASON_RESEARCH_SNAPSHOT_V1`: exactly existing `prospective_ml_observation.validate_ml_snapshot` required keys, plus all `POLICY_KEYS`, `game_type`, `away_team_id`, `home_team_id`, `feature_sha256`, `observation_boundary_utc`. `market` has status `NOT_EVALUATED` and null trace/decision fields. `totals_status` is `NOT_EVALUATED`; non-support reason resides in fixed policy/receipt output. Participant evidence retains existing exact starter/ordered-lineup shape and provenance.
- Claim schema `PROSPECTIVE_ML_POSTSEASON_RESEARCH_CLAIM_V1`: `schema`, all `POLICY_KEYS`, `claim_identity_sha256`, `game_pk`, `game_type`, `model_card_sha256`, `snapshot_relative`, `snapshot_sha256`, `claimed_at_utc`, `content_sha256`. Identity hashes exactly `{game_pk, model_card_sha256, competition_scope, research_policy_version}`. Resolve snapshot to recover and validate full team, date, source and forecast lineage.
- Flow receipt schema `PROSPECTIVE_ML_POSTSEASON_RESEARCH_RECEIPT_V1`: `schema`, all `POLICY_KEYS`, `phase`, `created_at_utc`, `pregame_receipt`, `previous_postgame_receipt`, `ml_trace_relative`, `ml_trace_sha256`, `candidates`, `primary_claims`, `settlement_heads`, `report_relative`, `games`, `errors`, `workflow_complete`, `content_sha256`. Nonapplicable references are null and collections are empty arrays. Candidate/claim/head arrays contain root-relative paths; validate referenced artifacts independently. Game rows have exactly `game_pk`, `game_type`, `snapshot_relative`, `claim_relative`, `state`, `reason_codes`, `settlement_head`, `market_status`, `market_reason`, `totals_status`, `totals_reason`, `shadow_status`. Error rows have exactly `game_pk` (nullable), `reason`.
- Settlement schema `PROSPECTIVE_ML_POSTSEASON_RESEARCH_SETTLEMENT_V1`: `schema`, all `POLICY_KEYS`, `claim_relative`, `claim_sha256`, `snapshot_relative`, `snapshot_sha256`, `game_pk`, `game_type`, `away_team_id`, `home_team_id`, `official_result_receipt_relative`, `official_result_receipt_sha256`, `result`, `previous_head_relative`, `revision`, `content_sha256`. Result keys: `state`, `reason`, `away_score`, `home_score`, `winner`, `official_date`; score/winner fields are null for nonsettleable outcomes. Preserve existing append-only revision/head authentication and outcome/date reconciliation; this is a new envelope, not weaker persistence.
- Summary schema `PROSPECTIVE_ML_POSTSEASON_RESEARCH_SUMMARY_V1`: `schema`, all `POLICY_KEYS`, `claim_identities`, `settlement_heads`, `sample_count`, `hit_count`, `accuracy`, `brier_score`, `log_loss`, `pending_count`, `excluded_count`, `failure_count`, `shadow_status`, `warnings`, `content_sha256`. Metric fields except counts are null with zero settled samples; no price/edge/P&L fields.
- Status schema `PROSPECTIVE_ML_POSTSEASON_RESEARCH_STATUS_V1`: `schema`, all `POLICY_KEYS`, `pregame_receipt`, `postgame_receipt`, `workflow_processed`, `workflow_complete`, `complete_evidence`, `games`, `errors`; game/error rows use the exact receipt shapes above.

## Task 1: Authentic Combined History and Frozen Feature Adapter

**Files:**
- Create: `mlb_backtest/competition_policy.py`, `tests/test_postseason_research_sources.py`, `tests/test_postseason_research_history.py`, `tests/test_postseason_research_features.py`.
- Modify: `mlb_backtest/live_sources.py`, `mlb_backtest/live_history.py`, `mlb_backtest/live_features.py`, and only necessary policy-aware branches in `mlb_backtest/source_parsers/schedule.py`, `mlb_backtest/source_parsers/statcast.py`, `mlb_backtest/statcast.py`. There is no `source_parsers/mlb_stats.py` in this checkout; do not invent that file.
- Reuse test fixtures/helpers from existing live source/history/feature tests without editing their baseline payloads.

**Interfaces:** Produces policy constants, population description and keyword extensions listed above. Consumes existing immutable contract classes; does not change V1 report/trace validators.

- [x] Write failing offline tests: table-driven F/D/L/W target/earlier-history games preserve exact type; R default serialization and query identities are byte-identical. Observed P/C/S/A/I/E/unknown target rows are excluded, never relabeled. Same-season R and postseason rows both alter raw recent components; prior seasons/current-day results do not.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_postseason_research_sources.py'` and the corresponding history/features patterns; confirm new behavior fails before implementation.
- [x] Implement policy-aware schedule/participant/final observers. Bind research source identities/populations before persistence; use research-only raw-source/cache/receipt namespace suffixes, not changed regular hashes. Final recheck compares gamePk/date/team IDs/type and preserves existing durable clock checks. Retain all observed unsupported rows but avoid fetching their boxscores.
- [x] Implement combined-history request, normalized population and full-range coverage. Existing collection spans season opening through D−1, which covers every existing rolling window without changing formulas. Require official schedule coverage and required played gamePk pitch coverage for every collected date; bind date/status/type/team identities and required gamePk set into a canonical coverage digest. Cancelled/postponed games are not played requirements; suspended/unresolved mappings remain fail-closed. The new cache query must include policy/population/coverage identity and cannot certify an R-only artifact as combined data.
- [x] Write/verify coverage regressions for a missing D−4 postseason game, missing pitches in a known earlier game, valid R plus F data, a mislabeled cached R population, stale/tampered coverage digest, duplicate or contradictory gamePk identities, empty schedule, postponed and suspended fixtures. Raw R cache reuse is optional; if reused, retain its original provenance and issue a newly validated combined-set binding.
- [x] Adapt the live feature gate to supported types with policy-bound prepared history. Keep `_build_raw_game_features` → `feature_engine._build_raw_components` → `scoring_prep.score_raw_game` intact; do not route through `feature_engine._eligibility` (R-only) or mutate target labels. Validate policy/population/source binding before rebuilding authoritative feature history. Career BF and league ERA use unchanged R-only source definitions (no averaged ERA).
- [x] Run all three new test patterns and focused existing `test_live_sources.py`, `test_live_history.py`, `test_live_features.py`; confirm default fixture digests and frozen files unchanged. Report results and files to coordinator; no commit.

## Task 2: Complete Research Daily Run and Strict Artifact Authority

**Files:**
- Create: `mlb_backtest/postseason_research_contracts.py`, `mlb_backtest/postseason_research_service.py`, `tests/test_postseason_research_daily.py`, `tests/test_postseason_research_contracts.py`.
- Modify: `mlb_backtest/daily_top3.py`; only if an explicit scope rejection is missing, `mlb_backtest/market_play_authorities.py`.
- Do not edit `live_top3_service.py`, `live_top3_contracts.py`, `live_top3_trace.py`, frozen files or Task 1/3 files.

**Interfaces:** Consumes Task 1 functions using `competition_policy=POSTSEASON_ML_RESEARCH_V1`. Produces the report/trace/service/loader interfaces and exact schemas above for Task 3.

- [x] Write failing tests using detached copies of `tests/fixtures/live_top3/sealed_happy_slate.json` and the existing source fixtures. A complete F/D/L/W slate must yield actual frozen-model probabilities with all four games in the report. Verify populations of zero/one/two/five scored games, missing participant cases, unsupported observed rows and exact-neutral behavior. Verify each scored trace replays to the same numerical output as direct frozen scoring of its raw features.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_postseason_research_daily.py'` and `test_postseason_research_contracts.py`; establish failing behavior.
- [x] Implement the narrow research service using existing readiness and frozen-card semantic checks, feature functions and scoring primitives. Include every observed target row exactly once; no top-three truncation. Shared authority/coverage failures abort affected scoring, never manufacture probabilities. Isolated missing participant/venue/starter inputs produce explicit unscored rows; readiness VERIFIED remains unrelated to postseason validity. Final schedule observer runs under the existing pre-start semantics, with latest durable observation boundary included in report/trace.
- [x] Implement exact validators and trusted filesystem loader. Check source binding scope/population, official type/team/date identity, participants, report-to-trace feature/model/probability agreement, cardinality, digests, paths and source files. Wrong/unknown/extra schema fields fail. Cross-scope and legacy V1 loaders reject the new artifacts. A scope-stripped, rehashed legacy-looking trace/report also fails due to source query-policy binding; add an explicit legacy-authority guard if necessary, without changing valid V1 behavior.
- [x] Add CLI `--competition-scope {regular-season,postseason-research-v1}` defaulting to `regular-season`, and the `run_daily_top3` keyword dispatch. Persist immutable research report/Markdown/trace under `reports/postseason_ml_research/<target_date>/` using the existing content-addressed naming/trusted-root pattern and exact sidecar verification. Default path and bytes stay unchanged. Render warnings, full slate, null model values as unavailable, R-only BF/ERA caveats, PASS reason and unsupported totals; never calculate edges or invoke market/totals.
- [x] Test wrong source type/team, digest drift, policy/population replacement, frozen-card mismatch, tampered trace score, missing trace game, over-counted duplicate games, equality at start and start during acquisition. Patch market/totals routines to raise if called; strong probabilities and supplied fresh market evidence must still be ineligible.
- [x] Run new tests plus focused existing `test_daily_top3.py`, `test_daily_top3_e2e.py`, `test_live_top3_contracts.py`, `test_live_top3_trace.py`, `test_market_play_authorities.py`. Report output to coordinator; no commit.

## Task 3: Isolated Prospective Claim, Settlement and Status Flow

**Files:**
- Create: `mlb_backtest/prospective_ml_postseason_research.py`, `mlb_backtest/prospective_ml_postseason_settlement.py`, `tests/test_postseason_research_prospective.py`, `tests/test_postseason_research_settlement.py`, `tests/test_postseason_research_flow.py`.
- Modify: `mlb_backtest/prospective_ml_daily_flow.py` only for explicit scope CLI/API dispatch and preserving existing default behavior.
- Do not modify regular observation/calibration/shadow persistence or aggregates. Reuse secure filesystem primitives and narrow pure official-outcome parsing/reconciliation helpers, not R-only schema loaders.

**Interfaces:** Consumes `load_postseason_research(root, trace)` and exact Task 2 artifacts. Produces the research flow APIs and snapshot/claim/receipt/settlement/summary/status schemas above. All evidence lives in `audit/prospective_ml_postseason_research/`; all prospective summaries in `reports/prospective_ml_postseason_research/`.

- [x] Write failing deterministic offline pregame tests using Task 2 fixtures/contract builders: a verified future prediction claims exactly once; preliminary/unscored/excluded rows remain diagnostic. Observation, durable completion, current claim clock and every required source boundary must each be strictly before scheduled start. Repeat/run-resume preserves the first eligible primary even if later probability differs.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p 'test_postseason_research_prospective.py'`; confirm failures.
- [x] Implement standalone research validation/claim persistence with scope-bound canonical identity, lock identity and directory layout. Include scope/policy/game type in digest-bound forecast lineage. Rebind trace authority at persistence and read/verify exact existing bytes on retries. Reject missing/tampered receipts, cross-cohort claims, partial/conflicting writes and any late claim. Persist partial failure diagnostics but never return a successful recording receipt after an integrity/persistence failure.
- [x] Implement separate official-result acquisition/settlement. Require exact gamePk, type, team IDs and recorded lineage; retain current official-date/reschedule reconciliation rules. Preserve original participant evidence but permit final player substitutions. Pending/suspended games remain pending; final tie/no contest become explicitly excluded/nonsettleable. Use append-only prior-head/revision chains for real official corrections; retries reuse identical heads. Do not generate new predictions from final results.
- [x] Test F/D/L/W final outcomes, normal substitutions, identity mismatch, unsupported final type, pending→final, suspended→final, final tie, no contest, valid correction, duplicate retry and corrupt receipt/head chain. A regular claim with the same game/model digest must be rejected by research loaders and excluded from the research summary; reverse contamination also fails.
- [x] Compute sample count, hit count, accuracy, ordinary home-outcome Brier score and log loss from validated settled primary heads only. Use existing numerical conventions; zero samples produce null metrics. Show descriptive caveats and `NOT_EVALUATED_FOR_POSTSEASON`; never feed regular `all_primary`, calibration or shadow aggregates and never auto-promote eligibility.
- [x] Add explicit research CLI/API dispatch for pregame/postgame/status in `prospective_ml_daily_flow.py`. Reject market input options in research mode before calling any market path. Emit fixed market/totals/shadow statuses in receipts/status. Resume accepts only same-policy receipts and exact linked artifacts; no automation edits.
- [x] Run all three new patterns, plus focused existing `test_prospective_ml_daily_flow.py`, `test_prospective_ml_observation.py`, `test_prospective_ml_settlement.py`, `test_prospective_ml_results.py`, `test_prospective_ml_report_integrity.py`. Report results to coordinator; no commit.

## Coordinator Integration / Completion Gate

- [x] Verify interface names and exact required key sets across owners before first integrated run; resolve mismatches without weakening contracts.
- [x] Run new end-to-end offline fixture: acquisition of combined history and F/D/L/W slate → frozen features/score → persist/load report+trace → pre-start snapshots+first claims → official final results → settlement+isolated metrics → idempotent resume/status. Require at least one actually scored/claimed/settled supported game, not only unsupported/unscored branches.
- [x] Run safe focused test patterns using `PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -p '<pattern>'`; do not expand to unrestricted discovery. Run `git diff --check`. Confirm the four frozen modules, model card, regular fixture payloads and default artifact identities are unchanged.
- [x] Use `PYTHONDONTWRITEBYTECODE=1 python3 -B -m pytest -p no:cacheprovider` with an explicit safe file list for pytest-function tests (especially `test_prospective_ml_daily_flow.py`); unittest discovering zero tests is not a pass. Existing focused baseline is 348 passing tests in `2026-09-30-postseason-baseline.md`.
- [x] Report implementation/test evidence and remaining risks. Do not call automation ready or perform a live smoke run. Future explicit activation may use only a genuinely still-pregame eligible slate; absence means wait, never backdate.

## Boundary Evidence and Risks

`live_features._build_raw_game_features` already calls participant-neutral `feature_engine._build_raw_components`; `scoring_prep.score_raw_game` only requires eligible raw component inputs. Therefore the inspected boundary allows real postseason scoring without editing frozen math or pretending game type R. The research adapter must validate eligibility before that call; it must not merely set `eligible=True` on rejected historical feature output.

The largest risk is combined-history completeness, not the formula: current D−1-only coverage cannot establish earlier postseason presence. The second is a scope-stripping downgrade through legacy source authority; new schema identifiers alone do not prevent that, hence the mandatory raw-query binding tests. The third is parallel contract drift: the exact interfaces and field sets above are the synchronization boundary, and implementation incompleteness cannot be disguised as perpetual UNSCORED output.
