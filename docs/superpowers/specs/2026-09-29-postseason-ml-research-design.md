# Postseason ML research evaluation

## Goal and approval status

Allow the frozen internal moneyline model to evaluate upcoming postseason games
for research and to accumulate genuine pregame predictions for later validation.
The user approved ML first, with totals deferred. This written design awaits
review; no implementation or activation is claimed by this document.

An available probability is not a validated postseason probability. Every output
must state `RESEARCH_ONLY`, `POSTSEASON_OUT_OF_DOMAIN`, and
`NOT_POSTSEASON_VALIDATED`. This phase cannot produce a betting `PLAY`.

## Scope and non-goals

- Add an explicit `--competition-scope postseason-research-v1` mode to the
  existing daily ML workflow and its prospective recording/settlement path.
- Preserve the regular-season default, frozen model card, normalization,
  scoring formulas, calibration coefficients, and existing evidence.
- Keep postseason records and performance summaries separate from all existing
  regular-season primary, calibration, and shadow cohorts.
- Do not implement postseason totals, retrain or recalibrate a model, lower
  completeness thresholds, repair unrelated historical records, or reconstruct
  predictions after a game starts.
- Do not place bets, enable market recommendations, migrate old artifacts, or
  update the recurring automation during the design stage.

## Fixed competition policy

Use two explicit policies, not a configurable tournament framework. The new
policy identifier is `POSTSEASON_ML_RESEARCH_V1`; its CLI scope is
`postseason-research-v1`.

| Property | Existing regular-season mode | New postseason research mode |
| --- | --- | --- |
| Target game types | Existing `R` behavior | `F`, `D`, `L`, `W` only |
| Recent batting, starter, bullpen, rest and travel history | Existing behavior | Same-season `R`, `F`, `D`, `L`, `W` through D−1 |
| Career batters faced and league ERA | Existing regular-season definitions | Same regular-season definitions, explicitly disclosed |
| Model and coefficient authority | Existing frozen authority | Identical frozen authority |
| Betting recommendation eligibility | Existing gates, unchanged | Always ineligible: `POSTSEASON_NOT_VALIDATED` |
| Prospective cohort | Existing namespace and identity | Separate postseason namespace and policy-bound identity |

Official MLB game types identify `F` as Wild Card, `D` as Division Series, `L`
as League Championship Series and `W` as World Series. Generic `P`/`C`, unknown
types, and `S`/`A`/`I`/`E` are not admitted in this first version. The complete
research slate means every official target-date game in the four supported
types. Unsupported targets actually observed in source responses remain visible
as excluded rows; this mode does not claim to discover every unsupported event.
No unsupported game is rewritten as `R`.

`D` in a time cutoff means the target calendar date in America/New_York, not
the MLB Division Series code. Existing feature lookback lengths and sufficient
sample rules remain unchanged. The new policy changes the admitted recent-game
population, not the scoring formula or window lengths. It must not silently
substitute regular-season-only history for the combined population.

## Source and history integrity

The competition policy must accompany schedule collection, parsing, normalized
game rows, history coverage, feature preparation and report construction.
Preserve the official `game_type` throughout; validate gamePk, target date,
away/home team identities and type against the bound official schedule.

The new live/history parsing branch admits the explicit population without
changing the old parser's default filtering, normalized records or digests.
Statcast requests and cache bindings must identify both the requested population
and the policy version. An old `R`-only cache cannot certify combined coverage.
Previously verified regular-season raw data may be reused as one contribution
to a newly validated combined input set; its original provenance is retained.

Coverage is checked against official scheduled/played games across every date
needed by the existing feature windows, not just games present in Statcast or
only D−1. A missing earlier postseason game is a coverage gap. A cancelled or
postponed listing is not a played-game requirement; unresolved suspended games
and ambiguous status/date mappings retain the existing fail-closed treatment.
Scope-aware cache keys alone are insufficient without this coverage check.

All performance/history inputs obey the existing D−1 cutoff. Current-day
lineups, starters, venue and schedule observations may be used only as pregame
context under existing freshness and provenance rules. Current-day results and
in-game performance cannot enter features. Career BF and league ERA retain
their regular-season source definitions and are reported as such; no averaging
of ERA values or broader-population substitution is allowed.

## Components and versioned contracts

Add a small immutable competition-policy boundary used by existing collectors
and services. It owns the admitted target/history types and fixed policy label;
it does not own scoring, persistence or sportsbook logic.

| Boundary | Responsibility |
| --- | --- |
| Live sources and parsers | Collect and preserve official type; bind population and policy to observations |
| History and feature preparation | Prove combined coverage and existing time/sample gates; prepare the existing frozen model inputs |
| Daily ML service | Score eligible games; emit a complete research slate and reasons for unscored/excluded games |
| Research artifact loader | Validate the new exact contract and source/trace bindings; never fall back to a V1 interpretation |
| Prospective flow | Record eligible pre-start predictions once, preserving first-primary authority within the research cohort |
| Settlement and review | Join official outcomes to recorded claims and summarize the research cohort separately |

Existing V1 report, trace, snapshot and authority contracts are unchanged. New
research artifacts use separate schema identifiers/version dispatch; existing
V1 loaders must reject them rather than accepting extra fields or silently
dropping scope. A new report/trace/snapshot carries, at minimum:

- `game_type` per game;
- `competition_scope = postseason-research-v1`;
- `research_policy_version = POSTSEASON_ML_RESEARCH_V1`;
- `history_population` describing recent types `R/F/D/L/W`, unchanged window
  definitions, D−1 cutoff and regular-season career BF/league ERA definitions;
- `validation_status = NOT_POSTSEASON_VALIDATED`;
- existing model-card, observation, input, trace and timestamp authority.

These values are validated against each other and included in canonical digest
bindings; display-only labels are insufficient. The exact new schemas and CLI
plumbing belong in the implementation plan. There is no permissive catch-all
schema or automatic upgrade of old records.

Do not edit `normalize.py`, `scoring.py` or `scoring_prep.py` to admit game types:
their source hashes participate in existing model/shadow authority. Scope
adaptation happens before the frozen scoring boundary, with no false `R` label.
If this cannot be done while keeping the frozen algorithm intact, surface that
incompatibility instead of changing weights, hashes or eligibility silently.

## Evaluation, timing and failure states

Report the complete supported target-date slate and any observed unsupported
rows with per-game `PRELIMINARY`, `VERIFIED`, `UNSCORED` or `EXCLUDED` states
under existing readiness semantics. In this mode `VERIFIED`
means input readiness only, never validated postseason performance or betting
eligibility. Zero, one, two or many scored games are valid; no Top3 quota.

Preserve lineup counts/identity, starter identity, venue, complete feature inputs,
durable observation timestamps and the final official pregame schedule recheck.
A game starting during acquisition or evaluation loses pregame recording
eligibility. Equality at the start boundary fails; neither a report timestamp
nor a later replay can make a late observation prospective.

Missing game-specific inputs produce an explicit unscored row, without a guessed
probability. A run-level source/trace digest mismatch, ambiguous scope, malformed
authority or invalid shared-history coverage prevents affected scoring and
prospective claims. Unaffected games may proceed only if their own complete,
independently bound inputs pass the same checks. Persistence or integrity
failure must not be reported as successful recording.

## Market and totals exclusion

Postseason research reports do not enter existing V1 market-play authorities.
The new orchestration branch emits `NOT_EVALUATED` for market recommendation
and a user-facing `PASS — POSTSEASON_NOT_VALIDATED`, irrespective of probability,
lineup completeness or attractive prices. No configuration override enables
`PLAY` in this policy. New research inputs passed to legacy market loaders are
rejected; an attempted transform that strips scope must fail source/trace binding.

This initial phase does not calculate postseason fair-price edges or P&L.
The regular-season one-book policy remains unchanged. Its price verification
cannot establish postseason calibration.

Do not call `daily_totals` with the new ML research contract. Report totals as
`NOT_EVALUATED — POSTSEASON_TOTALS_NOT_SUPPORTED`. Existing regular-season totals
behavior stays intact. A later totals design must separately address rolling
candidate selection, residual population and required sample sizes.

## Prospective records and settlement

Use the separate `audit/prospective_ml_postseason_research/` evidence namespace
and `reports/prospective_ml_postseason_research/` report namespace. Preserve the
existing append-only persistence, receipt validation and first-primary rules.
Research claim identity is bound to gamePk, frozen model digest, competition
scope and research policy version; regular and postseason claims cannot collide.

Only predictions meeting the existing prospective input-readiness and pre-start
timestamp rules receive primary claims. Preliminary displays may remain research
artifacts without qualifying as primary observations. Record the exact input
policy and game type in the claim and its digest-bound forecast lineage.

Settle only from genuine recorded claims after an officially final, settleable
outcome and exact gamePk, game type, away/home team identity and forecast-lineage
matching. Use the existing official game/date reconciliation rules; unresolved
rescheduling ambiguity is not repaired by guessing. Preserve pregame starter
and lineup evidence, but do not require the final player list to equal it:
normal in-game substitutions must not invalidate settlement.

Games still awaiting play or possible resumption remain pending. A terminal
outcome that cannot be settled as a win/loss, including a final tie or no contest,
is explicitly excluded/non-settleable under existing outcome semantics, not
left pending forever. Mismatches remain explicit failures. Retries/resumes must
not create duplicate claims or settlement revisions. Legitimate official outcome
corrections use the existing append-only revision/head rules. Do not fill missed
dates with postgame predictions.

Compute ordinary hit count, Brier score and log loss as descriptive research
metrics when settled samples exist. Display sample count and caveats; zero
eligible samples means unavailable, not zero accuracy. Never merge these into
regular-season `all_primary`, calibration or shadow aggregates, even with the
same model digest. Initial shadow status is `NOT_EVALUATED_FOR_POSTSEASON`;
no automatic evidence threshold promotes the cohort to validated betting use.

## Activation and workspace safety

The default invocation remains regular-season only. After implementation and
regression verification, an explicitly selected postseason scope may be used
for a future eligible slate. Automation adoption is a subsequent explicit
handoff: use this mode for supported postseason targets, skip totals/market
PLAY/shadow promotion, and retain duplicate-run protection and exact receipts.
Do not label the current automation as postseason-ready before that handoff.

Work only under `/Users/taejunoh/Developer/LFG/MLB Analysis/.worktrees/live-venue-timezone-fix`.
Preserve all pre-existing dirty, staged and untracked files. No Documents access,
sudo, push, merge, PR, privileged/custody/production operations, or access to the
prohibited temporary manifest. No public-network run may generate a retroactive
pregame claim. Code implementation starts only after written-spec and plan review.

## Acceptance criteria

1. Existing regular-season CLI defaults, V1 roundtrips, golden artifacts and
   frozen scoring/model authority remain unchanged.
2. Each of `F/D/L/W` survives schedule, source, history, report, trace, snapshot
   and settlement bindings; unsupported and mismatched types fail as specified.
3. Combined-history gaps, missing earlier postseason pitches, mislabeled R-only
   caches and policy/population mismatches cannot pass completeness gates.
4. D−1 cutoffs, missing lineup/starter cases, equality-at-start and a start during
   execution retain fail-closed behavior; no result leakage or late primary claim.
5. Complete-slate outputs support any eligible count, with explanations and no
   fabricated probability for unscored/excluded rows.
6. Even strong model output plus fresh one-book prices cannot produce postseason
   `PLAY`; legacy loader, scope-stripping and cross-policy inputs are rejected.
7. Separate claims survive exact receipt/result joins and resume without duplicates;
   regular and postseason records sharing a model digest never share aggregates.
8. Totals is explicitly unsupported and existing regular totals tests still pass.
9. Focused source/history/feature/daily/prospective/market regressions and
   `git diff --check` pass before activation. A live smoke check uses only a still-
   pregame eligible slate; if none exists, it waits rather than backdating evidence.

## Design evidence

Official game-type definitions: [MLB Stats API game types](https://statsapi.mlb.com/api/v1/gameTypes).
Current implementation boundaries were inspected in `live_sources.py`,
`live_history.py`, `live_features.py`, `source_parsers/schedule.py`,
`source_parsers/statcast.py`, `source_parsers/mlb_stats.py`, `live_model_card.py`,
`live_top3_service.py`, `market_play_authorities.py`, `multibook_ml_evaluator.py`,
`prospective_ml_observation.py`, `prospective_ml_settlement.py`, and
`prospective_ml_shadow.py` under `mlb_backtest/`. The totals deferral follows
`daily_totals.py` and `experimental_totals.py` rolling-selection/residual logic.
