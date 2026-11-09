# Game-local exclusion and post-start research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a single game that crosses its scheduled-start boundary be reported as excluded without blocking valid games in the same live slate, and add an explicitly non-prospective, outcome-blind reconstructed research evaluator.

**Architecture:** The live path records a closed, game-keyed initial exclusion reason on `LiveSlateObservation`; `build_live_top3_run()` consumes that reason before final revalidation or feature construction. The research path is a new, isolated command/report family that reuses only pure D-1 feature/scoring helpers with a reconstructed participant projection; it cannot produce a Daily Top 3 artifact, prospective claim, market decision, result, or calibration row.

**Tech Stack:** Python 3, dataclasses, immutable/canonical JSON contracts, `unittest`, existing secure filesystem helpers and frozen v5.1 scoring helpers.

**Spec:** `docs/superpowers/specs/2026-09-13-game-local-evaluation-design.md`

## Global Constraints

- Work only in the LFG `live-venue-timezone-fix` worktree. Do not access forbidden recovery, custody, production, or Documents paths.
- Preserve all pre-existing dirty edits. `mlb_backtest/live_sources.py` is already dirty: inspect the current diff before editing and make only the owned hunk described below.
- Do not backdate clocks, bypass a pregame gate, mutate a prior report/trace/claim, read an outcome/settlement/market input, or run a new live collection in this task.
- `Warmup` remains pregame-admissible only when every relevant evidence receipt is strictly before the saved scheduled start. At or after start is an `EXCLUDED` game, never a prediction.
- The post-start evaluator must label every output `POSTSTART_RECONSTRUCTED_RESEARCH`, `NOT_PROSPECTIVE`, `NOT_BET_VALIDATED`, and `RECONSTRUCTED_FINAL_LINEUP`; absence of independently timestamped pre-start model evidence must add `MODEL_ARTIFACT_TIME_UNPROVEN`.
- This is a shared, extensively dirty worktree. No commits, staging, reverts, or broad cleanup in this task; reviewers compare the scoped before/after diff. Run tests and static checks only.

## File structure

- `mlb_backtest/live_sources.py` — carry an initial, per-game time-boundary exclusion instead of throwing for that one game while retaining global source validation.
- `mlb_backtest/live_top3_service.py` — merge the initial local reason and skip final observation/feature/trace work for that game.
- `mlb_backtest/live_features.py` — expose a narrowly scoped pure reconstructed-feature builder that reuses D-1 component construction without accepting target-day results.
- `mlb_backtest/poststart_research_evaluation.py` — new explicit-input, canonical, append-only outcome-blind research evaluator and CLI.
- `schemas/experimental_poststart_research.schema.json` — closed report schema for the new namespace.
- `docs/poststart_research_evaluation.md` — operator boundary and exact invocation documentation.
- `tests/test_live_sources.py`, `tests/test_live_top3_service.py`, `tests/test_daily_top3_e2e.py` — game-local pregame-boundary regression coverage.
- `tests/test_live_features.py`, `tests/test_poststart_research_evaluation.py` — reconstructed-feature and isolated report contracts.

---

### Task 1: Preserve the live batch when one game becomes ineligible

**Files:**
- Modify: `mlb_backtest/live_sources.py:243-259,1596-1680`
- Modify: `mlb_backtest/live_top3_service.py:538-760`
- Test: `tests/test_live_sources.py`
- Test: `tests/test_live_top3_service.py`
- Test: `tests/test_daily_top3_e2e.py`

**Interfaces:**
- Produces `LiveSlateObservation.initial_exclusion_reasons: Mapping[int, tuple[str, ...]]`, defaulting to an empty immutable mapping so all existing four-argument constructors remain valid.
- `observe_live_slate(...) -> LiveSlateObservation` records only `OBSERVATION_AT_OR_AFTER_START` for a well-formed, accepted-status game whose initial schedule receipt is at/after its own saved start; it does not fetch that game's participant boxscore.
- `PregameBoundaryError(ValueError)` is raised only by `_require_pregame_boundary`; source observation catches only that subtype. Other `ValueError`s remain global integrity failures.
- `build_live_top3_run(...)` reads `slate.initial_exclusion_reasons[game_pk]` before calling `final_schedule_observer` and emits a normal `_unranked_row(..., state="EXCLUDED", ...)` with no probability, rank, or trace row.

- [ ] **Step 1: Add a failing live-source test for a mixed slate.**

  In `tests/test_live_sources.py`, add a two-game schedule fixture where both games have `status="Warmup"`, game 1 starts before the injected `durable_clock`, and game 2 starts after it. Make the fetcher fail if its boxscore URL is requested for game 1 and return an ordinary valid pregame boxscore for game 2.

  ```python
  slate = observe_live_slate(
      root, "2025-05-01", fetcher=fetcher,
      durable_clock=lambda: datetime(2025, 5, 1, 19, 0, tzinfo=timezone.utc),
  )
  self.assertEqual(
      slate.initial_exclusion_reasons[game_1_pk],
      ("OBSERVATION_AT_OR_AFTER_START",),
  )
  self.assertIn(game_2_pk, slate.participants)
  self.assertNotIn(game_1_pk, slate.participants)
  ```

- [ ] **Step 2: Run the new source test and confirm the current batch aborts.**

  Run:

  ```bash
  python3 -B -m pytest -q -p no:cacheprovider \
    tests/test_live_sources.py -k mixed_warmup_slate_localizes_at_start_schedule
  ```

  Expected: FAIL with the current boundary exception before a `LiveSlateObservation` is returned.

- [ ] **Step 3: Add the closed local-exclusion field and localize only the scheduled-boundary failure.**

  Append this field to `LiveSlateObservation` (after the existing fields, with a default), and freeze/validate it in `__post_init__`:

  ```python
  initial_exclusion_reasons: Mapping[int, tuple[str, ...]] = field(
      default_factory=dict
  )

  # in __post_init__
  allowed = {int(game["game_pk"]) for game in self.games}
  normalized = {
      game_pk: tuple(reasons)
      for game_pk, reasons in self.initial_exclusion_reasons.items()
  }
  if (
      any(type(game_pk) is not int or game_pk not in allowed for game_pk in normalized)
      or any(reasons != ("OBSERVATION_AT_OR_AFTER_START",) for reasons in normalized.values())
  ):
      raise ValueError("initial exclusion reasons differ")
  object.__setattr__(self, "initial_exclusion_reasons", MappingProxyType(normalized))
  ```

  Declare the narrow exception adjacent to `_require_pregame_boundary` and have that helper raise it for its at-or-after condition only; do not change its clock comparison:

  ```python
  class PregameBoundaryError(ValueError):
      """A valid game crossed its own pregame evidence boundary."""

  # _require_pregame_boundary(...)
  raise PregameBoundaryError("live observation became durable at or after scheduled start")
  ```

  In `observe_live_slate`, replace the unconditional initial schedule loop with a game-local map. Do not catch parsing, HTTP, root-binding, persistence, or schema exceptions.

  ```python
  initial_exclusions: dict[int, tuple[str, ...]] = {}
  for game in games:
      if game["status"] not in ACCEPTED_PREGAME_STATUSES:
          continue
      try:
          _require_pregame_boundary(schedule_binding, game)
      except PregameBoundaryError:
          initial_exclusions[int(game["game_pk"])] = (
              "OBSERVATION_AT_OR_AFTER_START",
          )
  ```

  Skip the participant fetch for a game in `initial_exclusions`. Keep the existing participant-boundary check; if that check raises `PregameBoundaryError` after persistence, add the same game-local reason and do not place the participant or its binding in the returned slate. Return:

  ```python
  return LiveSlateObservation(
      target_date, games, participants, tuple(bindings), initial_exclusions
  )
  ```

  Import `field` from `dataclasses`; do not alter `ACCEPTED_PREGAME_STATUSES`.

- [ ] **Step 4: Add the failing service behavior test before wiring the field.**

  In `tests/test_live_top3_service.py`, construct a slate with game 1’s `initial_exclusion_reasons={1: ("OBSERVATION_AT_OR_AFTER_START",)}` and game 2 fully scoreable. Use an observer that raises `AssertionError` if called for game 1.

  ```python
  result = _build(slate, observe, feature_builder=feature)
  self.assertEqual(calls, [(2, TARGET)])
  self.assertEqual(
      result["excluded_games"][0]["reason_codes"],
      ["OBSERVATION_AT_OR_AFTER_START"],
  )
  self.assertEqual([row["game_pk"] for row in result["preliminary_top3"]], [2])
  self.assertNotIn(1, [row["game_pk"] for row in trace_sink])
  ```

- [ ] **Step 5: Short-circuit initial exclusions in the service without weakening global validation.**

  In the first per-game loop of `build_live_top3_run`, initialize reasons from the frozen map and skip the final schedule call only for that game:

  ```python
  exclusion_reasons = set(slate.initial_exclusion_reasons.get(game_pk, ()))
  # Retain date/status checks; they add diagnostic reasons to the same game.
  if exclusion_reasons:
      observed_games.append((game, participant, None, relevant, boundary_utc, exclusion_reasons))
      continue
  final = final_schedule_observer(game_pk, slate.target_date)
  ```

  Update the local `observed_games` annotation to allow `None` for its final item. The existing second loop already emits `EXCLUDED` when `exclusion_reasons` is nonempty; its `continue` guarantees it never reaches feature construction. Do not add a broad `try/except` around `final_schedule_observer`; an invalid final projection, corrupt source binding, or transport failure remains global.

- [ ] **Step 6: Run focused tests and add boundary/regression cases.**

  Add tests for participant receipt time exactly at start and after start (both local exclusion), one microsecond before start (normal path), a non-accepted `In Progress` status (existing `PREGAME_STATUS_NOT_ACCEPTED` behavior), and an invalid shared schedule schema (still raises). In `tests/test_daily_top3_e2e.py`, use a mixed fixture and assert the output persists a valid report containing both the excluded game and a scored game.

  Run:

  ```bash
  python3 -B -m pytest -q -p no:cacheprovider \
    tests/test_live_sources.py tests/test_live_top3_service.py tests/test_daily_top3_e2e.py
  ```

  Expected: PASS; the new mixed-slate tests prove game-local behavior and existing integrity tests still raise.

- [ ] **Step 7: Inspect the owned diff; do not commit.**

  Run:

  ```bash
  git diff --check -- mlb_backtest/live_sources.py mlb_backtest/live_top3_service.py \
    tests/test_live_sources.py tests/test_live_top3_service.py tests/test_daily_top3_e2e.py
  git diff -- mlb_backtest/live_sources.py
  ```

  Expected: no whitespace errors; the `live_sources.py` diff contains only the initial-exclusion data flow and does not overwrite another worker’s changes.

### Task 2: Build the explicit outcome-blind reconstructed research evaluator

**Files:**
- Modify: `mlb_backtest/live_features.py:448-559`
- Create: `mlb_backtest/poststart_research_evaluation.py`
- Create: `schemas/experimental_poststart_research.schema.json`
- Create: `docs/poststart_research_evaluation.md`
- Test: `tests/test_live_features.py`
- Create: `tests/test_poststart_research_evaluation.py`

**Interfaces:**
- `build_reconstructed_raw_game_features(participants: PregameParticipants, game: Mapping[str, object], history_bundle: LiveHistoryBundle, career_bf_total_by_pitcher: Mapping[int, int], *, prepared_history: PreparedLiveFeatureHistory) -> dict[str, object]` returns the same D-1 raw component shape as `build_live_raw_game_features`, but calls `_build_raw_components(..., starter_provenance="RECONSTRUCTED_FINAL_STARTER", lineup_provenance="RECONSTRUCTED_FINAL_LINEUP", ...)` and never checks/presents a pregame status.
- `build_poststart_research_evaluation(root: Path, *, history_bundle: Path, participant_projection: Path, model_card: Path, career_bf: Path, evaluated_at_utc: str) -> BuiltPoststartResearchEvaluation` accepts only explicit project-relative, hashed inputs and builds canonical bytes in memory.
- `persist_poststart_research_evaluation(root: Path, built: BuiltPoststartResearchEvaluation) -> tuple[Path, Path, Path]` writes JSON, Markdown, and trace under `reports/experimental_poststart_research/<target_date>/`; no command takes result, odds, settlement, or primary-claim arguments.
- Version 1 has **no fetcher, schedule aggregator, current-state lookup, or live-run dependency**. The caller supplies a validated local D-1 bundle, frozen card, reconstructed projection, and local career-BF mapping; the evaluator only reads those snapshots and calls existing pure feature/scoring functions.
- `_load_research_d1_history(raw: bytes) -> LiveHistoryBundle` is a private, one-purpose JSON loader. It is not a generic serializer and never calls `build_live_history()` or a raw-source fetcher.

- [ ] **Step 1: Write a failing pure-feature test that proves provenance differs but D-1 scoring inputs remain reusable.**

  In `tests/test_live_features.py`, reuse a valid sealed `LiveHistoryBundle`, a complete `PregameParticipants`, a complete projected game, and prepared history. Call the new function and assert both provenance markers while asserting target-date rows still reject:

  ```python
  raw = build_reconstructed_raw_game_features(
      participants, game, bundle, career_bf, prepared_history=prepared,
  )
  self.assertEqual(raw["away"]["lineup"]["provenance"], "RECONSTRUCTED_FINAL_LINEUP")
  self.assertEqual(raw["away"]["starter"]["provenance"], "RECONSTRUCTED_FINAL_STARTER")
  with self.assertRaisesRegex(ValueError, "TARGET_DATE_MISMATCH"):
      build_reconstructed_raw_game_features(
          participants, {**game, "official_date": "2025-05-02"}, bundle,
          career_bf, prepared_history=prepared,
      )
  ```

- [ ] **Step 2: Run the feature test and confirm the function is absent.**

  Run:

  ```bash
  python3 -B -m pytest -q -p no:cacheprovider \
    tests/test_live_features.py -k reconstructed_features_keep_d1_boundary
  ```

  Expected: FAIL with an import error for `build_reconstructed_raw_game_features`.

- [ ] **Step 3: Extract only the shared validation/component construction needed for reconstructed research.**

  Keep `build_live_raw_game_features()` unchanged in behavior. Factor its shared post-validation body into a private helper taking `game`, `participants`, `history_bundle`, `career_bf_total_by_pitcher`, `prepared_history`, `starter_provenance`, and `lineup_provenance`. The reconstructed public wrapper must enforce all of the following before calling that helper:

  ```python
  if not isinstance(participants, PregameParticipants):
      _fail("INVALID_PREGAME_PARTICIPANTS")
  if not isinstance(history_bundle, LiveHistoryBundle):
      _fail("INVALID_HISTORY_BUNDLE")
  if game.get("official_date") != history_bundle.target_date:
      _fail("TARGET_DATE_MISMATCH")
  if game.get("game_type") != "R":
      _fail("INVALID_GAME_TYPE")
  if any(game.get(f"{side}_lineup") for side in ("away", "home")):
      _fail("TARGET_PARTICIPANT_LEAKAGE")
  ```

  Reuse the existing identity/team/venue/career-BF/D-1-history checks. Do not call `FinalGameObservation`, `ACCEPTED_PREGAME_STATUSES`, `observe_final_game_schedule`, or any result parser in the reconstructed wrapper. Add the function to `__all__`.

- [ ] **Step 4: Write failing evaluator contract and non-interaction tests before creating the evaluator.**

  In `tests/test_poststart_research_evaluation.py`, build small sealed local fixtures for a D-1 history bundle, frozen card, career-BF mapping, and a complete reconstructed projection. The test invokes the public builder with only those relative paths; do not use a fetcher fixture.

  ```python
  built = build_poststart_research_evaluation(
      root, history_bundle=history_path, participant_projection=projection_path,
      model_card=card_path, career_bf=career_bf_path,
      evaluated_at_utc="2026-09-13T18:00:00Z",
  )
  self.assertEqual(built.report["status_labels"], STATUS_LABELS)
  self.assertEqual(built.report["lineup_provenance"], "RECONSTRUCTED_FINAL_LINEUP")
  self.assertIn("MODEL_ARTIFACT_TIME_UNPROVEN", built.report["limitations"])
  self.assertNotIn("winner", built.report)
  ```

  Add negative tests that add `"score": 1` to the projection, use an ineligible nine, use a target-date history row, add a schedule score/result key, set a Career BF `as_of_date` after D-1, alter a bound input after build but before persist, and pass an unexpected CLI argument such as `--result-receipt`. Add a loader test for a non-terminal or fabricated PA row and one for a binding whose `as_of_date` differs from the cutoff. Each must fail before output write.

  Add an actual non-interaction sentinel: monkeypatch `socket.socket` and `urllib.request.urlopen` to raise `AssertionError`, invoke the CLI with its four local input files, and assert they were not called. Snapshot the protected report roots before the command; after persistence, assert the only newly created files are the JSON/Markdown/trace under `reports/experimental_poststart_research/<target_date>/`, with no primary-claim, settlement, calibration, or ledger write.

  ```python
  with patch("socket.socket", side_effect=AssertionError("network access")), \
       patch("urllib.request.urlopen", side_effect=AssertionError("network access")):
      self.assertEqual(main(["evaluate", "--root", str(root), ...]), 0)
  self.assertEqual(new_files - before_files, expected_research_paths)
  ```

  Run this test first and expect collection/import failure because the module does not yet exist.

- [ ] **Step 5: Create the new evaluator around explicit, closed local research inputs.**

  Define these closed constants and dataclass in `mlb_backtest/poststart_research_evaluation.py`:

  ```python
  REPORT_SCHEMA = "EXPERIMENTAL_POSTSTART_RESEARCH_V1"
  TRACE_SCHEMA = "EXPERIMENTAL_POSTSTART_RESEARCH_TRACE_V1"
  REPORT_ROOT = Path("reports/experimental_poststart_research")
  STATUS_LABELS = [
      "POSTSTART_RECONSTRUCTED_RESEARCH", "NOT_PROSPECTIVE", "NOT_BET_VALIDATED",
  ]

  @dataclass(frozen=True, slots=True)
  class BuiltPoststartResearchEvaluation:
      root: Path
      report: Mapping[str, object]
      report_bytes: bytes
      markdown_bytes: bytes
      trace: Mapping[str, object]
      trace_bytes: bytes
      input_snapshots: tuple[RegularFileSnapshot, ...]
  ```

  Add only this local replay-authority loader in the same module. Its input is a canonical JSON object with exactly this envelope (all nested objects are canonical JSON, and `content_sha256` is the SHA-256 of the object with that field omitted):

  ```python
  {
      "schema": "POSTSTART_RESEARCH_D1_HISTORY_V1",
      "content_sha256": "...",
      "target_date": "YYYY-MM-DD",
      "cutoff_date": "YYYY-MM-DD",
      "cache_authority": {"namespace": "experimental_daily/history_cache", ...},
      "source_bindings": [{...SourceBinding fields...}],
      "team_code_by_name": {"Team name": "ABC", ...},
      "venue_references": {"123": {
          "venue_name": "...", "venue_city": "...",
          "venue_latitude": 0.0, "venue_longitude": 0.0,
          "time_zone": "America/..."
      }},
      "league_era_by_asof": {"YYYY-MM-DD": {...}},
      "feature_history": {
          "slot_weights": {"1": 0.0, ..., "9": 0.0},
          "statcast_coverage_complete": True,
          "pitches": [{...normalized D-1 pitch row...}],
          "plate_appearances": [{...terminal pitch rows...}],
          "schedule_rows": [{
              "game_pk": 1, "official_date": "YYYY-MM-DD",
              "team_codes": ["AAA", "BBB"], "home_team_code": "BBB",
              "venue_name": "...", "innings": 9
          }],
          "schedule_coverage_start": "YYYY-MM-DD",
          "schedule_coverage_end": "YYYY-MM-DD",
          "schedule_coverage_complete": True
      }
  }
  ```

  The loader must require exactly the listed envelope/feature-history keys; convert venue JSON keys to positive `int`, slot keys to `int`, and each binding through `SourceBinding(**value)`. It then constructs exactly:

  ```python
  history = FeatureHistory(
      StatcastData(pitches, plate_appearances),
      slot_weights=slot_weights,
      schedule_history=schedule_rows,
      schedule_coverage_start=coverage_start,
      schedule_coverage_end=coverage_end,
      schedule_coverage_complete=True,
      venue_by_name={ref["venue_name"]: {"city": ref["venue_city"],
          "latitude": ref["venue_latitude"], "longitude": ref["venue_longitude"],
          "time_zone": ref["time_zone"]} for ref in venues.values()},
      statcast_coverage_complete=True,
  )
  return LiveHistoryBundle(target_date, cutoff_date, history, era, teams,
                           venues, cache_authority, bindings)
  ```

  Before those constructors, verify `cutoff_date == target_date - 1`, exactly four bindings with the four roles (`HISTORY_SCHEDULE`, `HISTORY_STATCAST`, `HISTORY_ERA`, `HISTORY_VENUE`) once each, and every binding query's `as_of_date == cutoff_date`. Require coverage end exactly equal to the cutoff and reject any pitch, PA, or schedule row with date later than it, a target `game_pk`, a schedule result/winner/score field, or a row key outside its allowlist. The normalized pitch allowlist is exactly `game_date`, `game_pk`, `pitcher_id`, `batter_id`, `events`, `description`, `p_throws`, `home_team`, `away_team`, `inning`, `inning_topbot`, `release_speed`, `woba_value`, `woba_denom`, `estimated_woba_using_speedangle`, `launch_speed_angle`, `at_bat_number`, `pitch_number`, `home_score`, `away_score`, `outs_when_up`, `game_type`, and `event_id`; historical `home_score`/`away_score` are retained because the existing bullpen availability feature calculates historical score margin. They are allowed only in rows dated no later than D-1; no target-day score or post-start statistic can pass this loader.

  Validate pitch identities are unique and ordered, then validate each `plate_appearances` row is an exact terminal completed row selected from those pitches (same `event_id`, no fabricated PA). This is the smallest closed reconstruction needed by `FeatureHistory`; do not introduce a reusable object serialization framework or persist/fetch raw provider payloads.

  Require a canonical participant-projection object with exactly:

  ```python
  {
      "schema", "content_sha256", "target_date", "game_pk",
      "scheduled_start_utc", "away_team", "home_team",
      "away_team_id", "home_team_id", "venue_id", "venue_name", "game_type",
      "away_starting_nine", "home_starting_nine", "away_starter_id", "home_starter_id",
      "source_relative", "source_sha256", "reconstructed_at_utc"
  }
  ```

  Validate exact digest, exactly nine unique positive IDs per side, distinct teams, positive starter IDs, strict project-relative source path/hash, and canonical UTC timestamps. Read `source_relative` as a separate trusted snapshot and require its bytes to be canonical JSON for the same **closed lineup-source schema** (identity, scheduled start, two nine-player lists, two starter IDs, source-retrieved timestamp, and hash only). Reject a provider boxscore, an object with extra keys, or a source/projection identity or digest mismatch. This prevents a full post-start payload containing a score, later stats, or outcomes from being accepted merely because the projection copied selected fields.

  Reject every unknown projection/source field; that rejects `score`, `winner`, `outcome`, `odds`, `market`, `player_stats`, and target-day feature material by construction.

  Require separate exact local JSON objects for the sealed D-1 history bundle and career-BF input. The career object must contain only `schema`, `content_sha256`, `target_date`, `as_of_date`, and `pitcher_bf`; require canonical dates, `target_date == projection.target_date`, `as_of_date <= history_bundle.cutoff_date`, and nonnegative integer BF values for exactly the two reconstructed starter IDs. Reject `as_of_date > cutoff_date` before raw features are built, so no target-day career value can enter runtime scoring. Rehydrate the history object only through `_load_research_d1_history` above and the career object to `Mapping[int, int]`; reject target-date history rows before feature construction. Parse the bound model-card bytes with `load_frozen_live_card_bytes`, then separately require the card's canonical `training_window` mapping and `actual_max_date < target_date`; reject missing, malformed, or post-target training metadata. The mandatory `MODEL_ARTIFACT_TIME_UNPROVEN` limitation is only about absent pre-start publication time—it never permits post-target training. Rehydrate the projection to a `PregameParticipants` object only after copying the reconstructed starter IDs into the neutral game projection's probable-pitcher fields. This is an adapter for pure feature compatibility, not a claim that the lineup was observed pregame.

  The report must bind the model-card, history-bundle, career-BF, and participant-projection bytes by relative path and SHA-256; emit `limitations` containing exactly `RECONSTRUCTED_FINAL_LINEUP` and `MODEL_ARTIFACT_TIME_UNPROVEN`. Do not inspect file mtime. It may include `p_home`, predicted side, pick probability, feature digest, component scores, qualities, scheduled start, and actual `reconstructed_at_utc`; it must not contain rank, decision, `PLAY`, result, score, winner, ROI, CLV, settlement, or primary claim fields.

- [ ] **Step 6: Implement canonical build, persistence, and a deliberately narrow CLI.**

  Use `open_trusted_root`, `read_regular_snapshot`, `rebind_regular_snapshot`, `write_exclusive`, and `canonical_json_bytes` exactly as existing append-only report producers do. The CLI exposes only:

  ```bash
  python -m mlb_backtest.poststart_research_evaluation evaluate \
    --root ROOT --history-bundle RELATIVE_PATH \
    --participant-projection RELATIVE_PATH --model-card RELATIVE_PATH \
    --career-bf RELATIVE_PATH
  ```

  Use the real UTC clock for `evaluated_at_utc`; do not accept a clock/date/start override. Before every write, rebind all input snapshots. Persist paths based on the canonical report digest and emit the three relative paths. The trace contains report relative path/hash, input refs, and the same status labels; it has no result-related reference. Do not import or call a live collector, schedule API, or network client.

- [ ] **Step 7: Add the JSON schema and operator documentation.**

  Make `schemas/experimental_poststart_research.schema.json` reject extra properties at the top level and require the report identity, status labels, input references, reconstructed time, provenance, limitations, feature digest, probabilities, and content digest. It must not define an outcome/market/stake property.

  In `docs/poststart_research_evaluation.md`, state that this command is post-start reconstructed research; the source lineup is not timestamp-proven; it cannot be a prospective observation, calibration input, historical official play, or betting recommendation; it never reads results/settlements/markets. Include the exact CLI above and state that a source artifact with a score/winner/result field is rejected.

- [ ] **Step 8: Run the isolated research tests and integrity checks.**

  Run:

  ```bash
  python3 -B -m pytest -q -p no:cacheprovider \
    tests/test_live_features.py tests/test_poststart_research_evaluation.py
  python3 -B -m json.tool \
    schemas/experimental_poststart_research.schema.json >/dev/null
  ```

  Expected: PASS; no test performs a network request or reads an outcome fixture.

### Task 3: Integrate contract review and verify no protected workflow changes

**Files:**
- Modify: `tests/test_daily_top3_e2e.py` only if Task 1’s live-slate fixture requires the new field
- Modify: `tests/test_prospective_ml_observation.py` only to add an explicit non-interaction regression
- Test: `tests/test_daily_top3.py`
- Test: `tests/test_prospective_ml_observation.py`
- Test: `tests/test_daily_multibook_market_plays.py`

**Interfaces:**
- Task 1’s `LiveSlateObservation.initial_exclusion_reasons` remains optional and contains only the closed time-boundary code.
- Task 2’s post-start command is isolated in behavior: its public CLI accepts only four local input paths, its import graph has no live collector or prospective/settlement/ledger module, and its only writes are its three new research artifacts.

- [ ] **Step 1: Add behavior-level isolation regression tests before final verification.**

  Keep the Task 2 socket/URL guard, then parse the module with `ast` and assert its actual import targets exclude live collection and prospective/settlement/ledger modules. Do not assert arbitrary source substrings or comments.

  ```python
  tree = ast.parse(module_path.read_text(encoding="utf-8"))
  targets = {
      alias.name for node in ast.walk(tree)
      if isinstance(node, (ast.Import, ast.ImportFrom))
      for alias in node.names
  }
  self.assertFalse(targets & {
      "live_history", "live_sources", "prospective_ml_observation",
      "prospective_ml_settlement", "all_day_ml_ledger",
  })
  ```

  In the CLI sentinel test, pass a valid closed source artifact and assert that input snapshot reads are restricted to the projection, its closed lineup source, history, card, and career-BF paths. Assert the report's top-level keys against an explicit allowed-key set and assert `new_files` exactly equals the JSON, Markdown, and trace paths; this checks effects rather than textual vocabulary.

- [ ] **Step 2: Run the boundary tests and confirm they fail before the isolation implementation is complete.**

  Run:

  ```bash
  python3 -B -m pytest -q -p no:cacheprovider \
    tests/test_poststart_research_evaluation.py -k 'noninteraction or import_boundary'
  ```

  Expected: FAIL until the new module and its closed-input implementation exist.

- [ ] **Step 3: Verify all affected workflows without a live invocation.**

  Run:

  ```bash
  python3 -B -m pytest -q -p no:cacheprovider \
    tests/test_live_sources.py tests/test_live_top3_service.py tests/test_daily_top3.py \
    tests/test_daily_top3_e2e.py tests/test_live_features.py \
    tests/test_poststart_research_evaluation.py \
    tests/test_prospective_ml_observation.py tests/test_daily_multibook_market_plays.py
  ruff check mlb_backtest/live_sources.py mlb_backtest/live_top3_service.py \
    mlb_backtest/live_features.py mlb_backtest/poststart_research_evaluation.py \
    tests/test_live_sources.py tests/test_live_top3_service.py \
    tests/test_live_features.py tests/test_poststart_research_evaluation.py
  git diff --check
  ```

  Expected: all tests and Ruff pass; no `daily_top3` live command is executed, so no current-day observation, result, or market artifact is created.

- [ ] **Step 4: Perform an explicit preservation audit; do not commit.**

  Run:

  ```bash
  git status --short
  git diff --name-only
  ```

  Confirm changes are limited to the Task 1/2 files and this plan’s listed tests/docs/schema. Report any unrelated dirty paths as pre-existing; do not revert, stage, commit, push, merge, or create a PR.

## Plan self-review

- Spec coverage: Task 1 localizes only scheduled-time boundary failures, retains global integrity failures, preserves Warmup semantics, and proves no excluded game gets a feature/trace score. Task 2 creates only an explicit, append-only outcome-blind reconstructed report with D-1 reuse and mandatory temporal/provenance limitations. Task 3 proves the two output families remain isolated and performs no new live run.
- Placeholder scan: no deferred implementation markers or generic error-handling instructions remain; each behavior names its exact reason code, interface, or test command.
- Type consistency: `initial_exclusion_reasons` is the sole live local-exclusion interface; Task 2’s builder and persistence names are consistently `build_poststart_research_evaluation` and `persist_poststart_research_evaluation`.
