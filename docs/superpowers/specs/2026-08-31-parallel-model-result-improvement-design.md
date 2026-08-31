# Parallel model-result improvement design

## Purpose and scope

Improve the frozen MLB moneyline model's probability quality and game-selection ranking through three parallel research lanes governed by one leakage-safe evaluation contract. Work that cannot change a model probability, edge, or ranking—trace presentation, signing, packaging, review bundles, privileged runtime, and general infrastructure—is outside this design.

This is a research-to-candidate workflow, not authorization to promote a model, place a bet, claim profitability, or modify the current v5.1 production card. The current card remains the immutable baseline until a candidate satisfies the common final gate.

## Evidence motivating the work

The current v5.1 calibration fits alpha and beta on 7,265 games from the same eligible pool rather than rolling-origin out-of-fold predictions. Its preserved 2025 diagnostic reports Brier `0.22137165792176453`, log loss `0.6351164919136184`, ECE `0.11508160079200008`, and probability-to-result Spearman rho `0.007735836222660863`. These results do not establish reliable probability magnitude.

The component weights are fixed domain weights rather than fitted ranking weights. In the same 2025 diagnostic, chosen-side bullpen edge is larger for losses than wins (`14.238422182693938` versus `11.752314325820896`) and has rho `-0.1506646197651568`, but that post-hoc N=100 observation is insufficient to reduce or invert the bullpen weight directly.

Environment and defense retain 10% and 5% nominal component weights, but current historical and live feature construction neutralizes both at score 50 and quality 0. The preserved 2025 diagnostic reports both components neutral for all 1,340 candidate rows. Adding them could change ranking, but only if their historical state can be reproduced without future information.

## Immutable population and fold contract

Reuse the approved v5.2 rolling-origin chronology:

| Fold | Target | Prior-season normalization and slot-weight pool | OOF calibration history |
| --- | --- | --- | --- |
| H22 | 2022 regular season | 2018–2021 | 2019 from 2018; 2020 from 2018–2019; 2021 from 2018–2020 |
| H23 | 2023 regular season | 2018–2022 | H22 OOF history plus 2022 from 2018–2021 |
| H24 | 2024 regular season | 2018–2023 | Frozen H23 development outputs plus 2023 from 2018–2022 |

H22 is candidate development, H23 is candidate selection, and H24 is the single common final holdout. No lane may inspect H24 outcomes, H24 aggregate metrics, or any derived label before every candidate definition is frozen. No 2025 outcome may enter normalization, calibration, feature choice, candidate selection, threshold choice, or weight choice.

Before any H22 feature is built, a shared source preflight must prove the required 2018–2024 minimum sources and their as-of boundaries. The currently unproven 2018–2021 inputs are a hard prerequisite. A source gap cannot be filled with target outcomes, final-state substitutions that violate the declared provenance stratum, or reconstructed T-45 evidence.

One immutable `RunSpec` binds the population, fold dates, exclusion rules, cutoffs, source manifests, baseline card, code revision, metrics, candidate-generation rules, and output roots. Every lane consumes that exact `RunSpec`.

## Parallel architecture

### Lane A — rolling-origin probability calibration

Lane A keeps feature construction, component scores, component weights, shrinkage, clipping, selected side, and daily ranking unchanged. It generates sealed OOF edges under H22 and H23 and fits fold-specific logistic alpha/beta only from permitted earlier-season OOF predictions.

Outputs are immutable fold cards, alpha/beta, normalization and slot-weight bindings, per-game probabilities, Brier, log loss, calibration tables, source hashes, and replay receipts. Lane A freezes one calibration candidate before H24.

Lane A advances only if its H23 Brier and log loss both improve over the corresponding v5.1 baseline on the identical eligible population. H24 promotion eligibility requires both metrics to remain lower than v5.1.

### Lane B — component weight and shrinkage candidate

Lane B is the only lane allowed to alter the relative starter, offense, bullpen, and schedule influence. Environment and defense remain neutral in this lane. It consumes the same sealed OOF feature rows as Lane A and never reads a final report as training input.

Candidate generation is declared in the `RunSpec` before H22 outcomes are opened. Environment and defense remain fixed at 10% and 5%. The four searched starter, offense, bullpen, and schedule weights use five-percentage-point increments, remain within ten percentage points of their 35/25/20/5 baselines, are nonnegative, and total 85%, so all six weights retain a unit total. Shrinkage has only one shared evidence-strength multiplier, selected from `{0.75, 1.00, 1.25}` and applied uniformly to the existing starter/hitter-split, recent-form, and bullpen denominators; defense/catcher shrinkage remains unchanged and per-metric shrinkage search is prohibited. Candidates also retain fixed signs within each component, existing probability clipping, and existing deterministic tie-breakers. The search cannot introduce a feature, team rule, home adjustment, cutoff, or outcome-dependent exception.

H22 may generate and reduce the candidate set. H23 selects exactly one candidate. That candidate advances only when H23 Brier and log loss are no worse than Lane A and its paired Daily Top-1 correctness delta versus v5.1 is positive. H23 results may not be used for another search round after selection.

Outputs are the pre-registered candidate ledger, rejected-candidate reasons, one frozen weight/shrinkage card, full H22/H23 rankings and probabilities, paired comparison tables, and replay receipts.

### Lane C — defense and environment source feasibility

Lane C investigates the two currently neutral components without changing the baseline, Lane A, or Lane B. It first produces source contracts and coverage reports for defense, weather, venue, roof, and any required catcher context. Each admitted observation must prove its schema, denominator, query, observation timestamp, target cutoff, and content hash.

Only H22 and H23 data may define transforms, weights within the existing defense/environment allocations, missingness handling, or fallback behavior. Missing or inadmissible data remains score 50 with quality 0; missing weight is never redistributed. Final-state weather, roof, defense, lineup, catcher, or venue information cannot stand in for an unavailable as-of observation.

Coverage passes only when every otherwise eligible H22/H23 game-side/component is explicitly dispositioned as `ADMITTED` or `NEUTRAL`, those counts reconcile to 100% of the expected population, no unknown gap remains, and every admitted observation passes timestamp and deterministic replay checks. If that coverage, timestamp safety, and replay contract does not pass on every required development season, Lane C produces no predictive candidate. In that case `A+B+C` is absent from H24 rather than shown as a distinct model identical to `A+B`. A passing Lane C freezes exactly one feature candidate, composed on top of the frozen `A+B` candidate, before H24.

Outputs are source coverage and exclusion reports, admitted manifests, one optional frozen feature card, H22/H23 candidate predictions, missingness strata, and replay receipts.

## Data flow and isolation

Stages remain physically separate:

```text
raw manifests
  -> normalized fold snapshots
  -> sealed OOF feature rows
  -> lane candidate artifacts
  -> frozen candidate registry
  -> one H24 scoring pass
  -> outcome join and comparison report
```

Each lane owns a distinct append-only output namespace and cannot modify another lane's files. The only shared lane inputs are the immutable `RunSpec`, verified source manifests, sealed feature rows, and explicitly completed OOF artifacts. Feature, scoring, and ranking stages have no import path or filesystem capability for outcome stores. Outcomes open only after predictions and ranks are sealed.

## Parallel execution sequence

1. Publish and verify the shared `RunSpec` and source preflight.
2. Begin resumable, query-specific acquisition of missing 2018–2021 minimum sources.
3. In parallel, build Lane A fold/card machinery, Lane B's pre-registered candidate contract, and Lane C's source adapters and coverage checks.
4. Do not begin a historical fold that depends on an unverified minimum source.
5. After the source gate passes, run H22 development and H23 selection independently in each lane.
6. Freeze at most one candidate from each lane and publish one candidate registry before any H24 outcome access.
7. Execute H24 once for `v5.1`, `A`, `A+B`, and `A+B+C` when Lane C qualified.
8. Publish the common comparison without adjusting a threshold, feature, weight, candidate, or eligibility rule.

## Common H24 promotion gate

All present candidates use the identical H24 eligible-game population and sealed predictions. A candidate is eligible for promotion only when all of the following hold:

- Brier and log loss are each no worse than v5.1; Lane A must lower both.
- Paired Daily Top-1 correctness delta versus v5.1 is positive for ranking-changing candidates.
- Maximum Daily Top-1 losing streak is no worse than v5.1 for ranking-changing candidates.
- Fold boundaries, D-1/T-45 cutoffs, future-data denial, source manifests, candidate registry, prediction seals, outcome join, and byte replay all pass.
- The candidate was frozen before H24 outcome access and received no post-H24 adjustment.

Report effect sizes, paired tables, uncertainty, coverage, and missingness. Do not convert a positive point estimate into a significance, profitability, or betting-value claim. If no candidate clears every applicable gate, v5.1 remains unchanged.

## Failure handling

- Missing or invalid required 2018–2021 input blocks H22 and therefore blocks H23/H24. It never becomes an imputed row.
- Future-season injection, target-outcome access, H24 access before registry sealing, manifest mismatch, or outcome-store capability in a pre-outcome stage invalidates the affected run.
- Duplicate `gamePk`, non-monotonic dates, nonfinite values, schema drift, hash mismatch, and replay mismatch reject the affected candidate or source partition.
- An optional Lane C source failure retains neutral defense/environment and does not invalidate Lane A or B.
- Acquisition retries are bounded, content-addressed, and idempotent. A deterministic interruption before outcomes open may resume from the last verified immutable stage.
- Once H24 outcomes or aggregate metrics are opened, no tuning or corrected rerun is allowed under the same candidate registry. A later attempt requires a new version and a genuinely untouched evaluation period.

## Testing and verification

Required automated coverage includes:

- exact v5.1 byte replay before any candidate comparison;
- table-driven fold boundary, target-season exclusion, D-1/T-45 cutoff, and provenance tests;
- intentional future-season, target-outcome, 2025-outcome, and premature-H24 injections that must fail;
- deterministic candidate generation and rejection under identical inputs;
- nonnegative/unit-sum Lane B weights, fixed signs, fixed clipping, and unchanged tie-breakers;
- Lane C timestamp, denominator, schema, missingness, neutral fallback, and no-weight-redistribution tests;
- a miniature multi-fold fixture with a known allowed winner and deliberate leakage cases;
- H22/H23 freeze and post-freeze mutation rejection;
- one candidate-registry seal before H24 and exact identical-population comparison;
- canonical artifacts, hashes, source manifests, prediction seals, outcome-join timestamps, and full byte replay.

The controller performs one final review of the candidate registry before H24. Independent code and evidence review must find no unresolved Critical or Important issue before any live-card promotion proposal.

## Deliverables

- shared `RunSpec`, source preflight, source manifests, and fold ledger;
- Lane A fold cards and calibration report;
- Lane B candidate ledger, frozen card, and paired ranking report;
- Lane C source/coverage report and optional frozen feature card;
- immutable pre-H24 candidate registry;
- common H24 comparison report and machine-readable metrics;
- replay, leakage, source, and outcome-join verification receipts;
- explicit promotion decision, including `NO_PROMOTION` when gates fail.

## Explicit non-goals

This design does not add an O/U target, sportsbook odds, market-value policy, automatic betting, injury model, production deployment, privileged signer, custody workflow, or general data platform. Moneyline model improvement and leakage-safe evaluation are the only goals.
