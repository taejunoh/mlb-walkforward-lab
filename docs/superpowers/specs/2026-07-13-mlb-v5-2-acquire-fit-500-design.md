# MLB v5.2-QD Hybrid Acquisition, Rolling Fit, and Historical 500 Design

**Status:** Approved approach, implementation design

**Approved strategy:** Acquire missing 2018–2021 evidence from official sources and promote only semantically complete 2022–2024 legacy payloads after byte, manifest, schema, and coverage verification. Any legacy payload that cannot prove the v5.2 contract is fetched again.

**Objective:** Produce verified 2018–2024 source evidence, fit sealed H22/H23/H24 rolling-origin cards, and execute an outcome-isolated historical sample of exactly 500 graded Top 1 dates.

## 1. Scope and non-negotiable outcomes

This phase is complete only when all of the following are true:

1. Every required 2018–2024 source-season contract is `READY`; `may_fit_H22=true`.
2. O19, O20, O21, O22, and O23 out-of-fold ledgers prove prediction sealing before outcome access.
3. H22, H23, and H24 model cards are immutable, independently reproducible, and use only permitted prior seasons.
4. The historical runner seals all candidate features, scores, ranks, and date states before importing target outcomes.
5. The membership ledger contains exactly 500 unique, chronologically selected, graded regular-season dates from 2022–2024 and never uses 2025 or same-date rank-2 replacement.
6. A clean replay from identical source hashes reproduces the model cards, locked rows, membership ledger, metrics, and run manifest byte-for-byte.
7. The final report labels the result only as historical robustness evidence and never as prospective confirmation or a profitability claim.

The prospective 2026 collector remains a separate later phase. Historical and prospective namespaces, cards, locks, outcomes, and reports may never be pooled.

## 2. Canonical namespaces and phase state

The immutable RunSpec remains a declaration of the experiment. It does not mutate from `UNFITTED_BLOCKED_BY_PREFLIGHT`. Actual phase progress is stored separately in an append-only execution registry.

Canonical paths:

- raw object and evidence root: `data/v5_2_qd/historical/raw`
- normalized source snapshots: `data/v5_2_qd/historical/sources`
- result-free features: `data/v5_2_qd/historical/features`
- sealed OOF ledgers: `audit/v5_2_qd/historical/oof`
- fold cards: `models/v5_2_qd/folds/{H22,H23,H24}`
- locked target rows: `data/v5_2_qd/historical/locked`
- outcome store: `data/v5_2_qd/historical/outcomes`
- execution registry and receipts: `audit/v5_2_qd/historical/execution`
- reports and workbook: `reports/v5_2_qd/historical`

Valid execution phases are strictly ordered:

`DESIGN_ACCEPTED -> ACQUIRING -> SOURCES_READY -> OOF_SEALED -> CARDS_SEALED -> TARGET_LOCKED -> OUTCOMES_JOINED -> SAMPLE_500_SEALED -> VERIFIED`

Each transition is an immutable canonical receipt containing the prior receipt hash, current code commit, RunSpec hash, source or output inventory hash, timestamp, and transition-specific validation results. A failed transition writes a quarantine receipt and does not advance the phase.

The cross-artifact verifier becomes phase-aware. It retains the existing design-only checks and adds explicit gates for each later phase. It never infers a phase from the presence of files alone.

## 3. v5.2 evidence contract

### 3.1 Logical query and immutable objects

Every acquisition starts from a canonical logical query. Its ID is the SHA-256 of canonical JSON containing:

- source name and source grain;
- official endpoint and sorted parameters;
- requested inclusive date or entity range;
- expected media type and parser version;
- parent query ID when the query may split;
- the RunSpec and acquisition-plan hashes.

Each attempt writes an append-only receipt with attempt number, request start/end UTC, final URL, HTTP status, selected response headers (`Content-Type`, `Content-Length`, `ETag`, `Last-Modified`, `Retry-After` when present), bytes received, outcome, and error classification. Bodies from unsuccessful or semantically invalid attempts are quarantined by hash and never promoted.

A successful child produces:

- immutable raw bytes or an approved protected-legacy payload reference;
- raw SHA-256 and byte count;
- a canonical semantic sidecar recomputed by parsing the raw bytes;
- sidecar SHA-256;
- parent/child range and split ordering;
- a success receipt binding query, raw, sidecar, and attempt history.

The publisher uses exclusive creation, fsync, per-query `flock`, no-follow path checks, and canonical JSON with exactly one final newline. Symlinks, hard links, special files, ambiguous duplicate queries, gaps, and overlapping split children fail closed.

### 3.2 Protected legacy promotion

Promotion is not a manifest rewrite and does not trust legacy declarations. A promotion worker:

1. verifies the legacy `RawStore` manifest, seal, filename, canonical query, raw hash, row count, and protected v5.1 inventory;
2. parses the legacy raw bytes with the current v5.2 parser;
3. recomputes the v5.2 semantic sidecar and coverage;
4. accepts the payload only if its exact query and semantics satisfy a planned v5.2 child;
5. writes a v5.2 promotion receipt binding the protected legacy path, legacy manifest SHA-256, raw SHA-256, parser version, and new sidecar SHA-256.

Promoted objects use a path-bound protected reference; no symlink or hard link is created. The verifier rereads the protected payload and all legacy seals every time it verifies a promoted object. If any requirement is missing, the exact child is fetched again from the official source.

Expected reusable evidence is 2022–2024 Statcast, schedule data that proves the required fields, and D-1 pitching responses. Reuse is decided per logical child, not per source or season. Career-BF payloads never count as annual league-pitching coverage.

## 4. Grain-specific source contracts

Generic `row_count/gamePk` rules are forbidden. Each source has its own semantic sidecar and referential gate.

### 4.1 MLB schedule and game evidence

Authoritative endpoints are the canonical MLB Stats API season schedule query

`https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R&startDate=S&endDate=E&hydrate=probablePitcher%2Clineups%2Cvenue%2Clinescore`

and the per-game boxscore query

`https://statsapi.mlb.com/api/v1/game/{gamePk}/boxscore`.

The season schedule query anchors every regular-season `gamePk`, official date, scheduled start, teams, venue, doubleheader order, final status, scores, and winner. Per-game boxscore evidence supplies the actual starting pitcher and nine starting hitters for each team when the season schedule response does not prove them. Schedule linescore and boxscore fields jointly prove innings and terminal details; disagreement blocks the game.

The sidecar contains the complete sorted regular-season game population, query coverage, unique `gamePk` count, canceled/unplayed population, fields present, and per-game child hashes. Duplicate IDs, a game outside the season, conflicting teams/dates, or missing terminal participants for a played candidate fail verification.

Historical data does not claim a timestamped T-45 lineup. Played games with complete final participants are `RECONSTRUCTED_FINAL_PARTICIPANTS`. Unplayed or canceled games without participants are ineligible and receive an explicit historical no-participant disposition; the system does not invent a pre-lock versus post-lock cancellation timestamp.

### 4.2 Baseball Savant Statcast

The canonical query uses the existing official details CSV parameters and regular-season filter. Season parents split by month; any child reaching 25,000 data rows, exceeding the bounded response size, or timing out is bisected by date until complete. A single-day capped or oversized response is quarantined and blocks that season.

The parser must recompute the full required schema, unique pitch key, game population, first/last game date, row count, plate-appearance terminal evidence, participant IDs, and per-game terminal digest. Split children must be ordered, nonoverlapping, and collectively cover the parent range.

Statcast coverage is compared with the schedule's played-game subset, not the complete scheduled population. Missing Statcast for a played game blocks that game's feature eligibility; canceled or unplayed schedule games are not required to appear in Statcast.

### 4.3 MLB pitching denominators and career BF

The schedule derives every unique D-1 as-of date required by training, OOF, and target features. For each date the MLB Stats pitching query is cumulative from that season's opening day through D-1. The raw parser recomputes total splits, earned runs, outs, and league ERA and rejects paging or truncation.

This source is reference-grain. Its sidecar contains unique as-of dates and a mapping from every target `gamePk` to the exact D-1 record hash. It does not contain fake game rows.

Career-BF evidence is a separate player/entity grain used only when the feature contract requires the pitcher's career denominator. Its sidecar binds pitcher ID, inclusive end date, raw response, BF, and every consuming game. It cannot satisfy `mlb_stats_pitching` coverage.

### 4.4 Venue, team, and timezone evidence

Canonical reference endpoints are

`https://statsapi.mlb.com/api/v1/teams?sportId=1&season=Y&hydrate=venue(location)`

and

`https://statsapi.mlb.com/api/v1/venues/{venueId}?hydrate=location`.

They provide IDs, names, city, coordinates, and home-venue relationships. Because MLB does not provide an IANA timezone, the repository contains a reviewed, versioned effective-date mapping keyed by venue ID. Neutral and alternate venues observed in the schedule require explicit rows.

The normalized sidecar proves that every schedule `(venue_id, official_date)` resolves to exactly one effective mapping and that every team ID resolves for the season. Missing, overlapping, or ambiguous effective ranges block the affected season.

### 4.5 Prior schedule history

Prior schedule history is a normalized projection of verified schedule/game evidence, not a separate web source. It contains `gamePk`, official date, teams, venue, scheduled and completed innings, terminal status, doubleheader order, and travel/rest inputs. Its manifest binds every source game hash and proves that no target-date performance enters D-1 history features.

## 5. Hybrid acquisition sequence

The acquisition runner is resumable and query-specific.

1. Freeze the accepted design, implementation plan, RunSpec, code commit, and current preflight inventory hashes.
2. Build the 2018–2024 season query plan without network access.
3. Audit and promote eligible 2022–2024 children by protected hash reference.
4. Fetch 2018–2021 season schedules first to establish canonical game/date/team/venue populations.
5. Fetch missing 2018–2021 Statcast children with bounded pacing, `Retry-After`, exponential backoff, deterministic jitter, split receipts, and resume.
6. Derive all required D-1 dates and pitcher IDs, then fetch missing pitching and career-BF evidence.
7. Fetch team/venue reference data and validate the reviewed IANA mapping.
8. Build prior-schedule projections and source-link sidecars.
9. Run semantic source verification twice from a clean process. The second run must make no network requests and reproduce inventory bytes exactly.
10. Rerun preflight. Fitting remains forbidden unless all required 2018–2024 contracts are `READY`, critical errors are zero, and `may_fit_H22=true`.

Network concurrency is bounded per host. The default is one Savant request and two MLB Stats requests in flight. The runner honors server rate signals and can be stopped and resumed without replacing any successful object.

Expected new storage is approximately 2.6–2.9 GB for 2018–2021 plus sidecars, attempt receipts, normalized snapshots, and derived artifacts. The runner checks free space before each season and stops safely below a 20 GB reserve.

## 6. Result-free feature snapshots

Source preparation produces two physically separate stores:

- a result-free source view available to feature, normalization, fitting, scoring, and ranking code;
- an outcome view readable only after the corresponding prediction ledger is sealed.

For target date `D`, all starter, offense, bullpen, schedule, travel, rest, and denominator inputs have maximum history date `D-1`; target-game Statcast and game performance are excluded. Final participant identity may come from the later official record, but only identity is used. Target-game results and performance remain inaccessible.

Every feature row binds source sidecar hashes, participant provenance, maximum input date, exclusion checks, component inputs, neutral/null optional-source behavior, and a canonical row digest. A single as-of violation makes the run `RUN_INVALID`.

## 7. Rolling-origin OOF and fold fitting

Five OOF ledgers are created in this order:

| Ledger | Fit seasons | Target season |
|---|---|---|
| O19 | 2018 | 2019 |
| O20 | 2018–2019 | 2020 |
| O21 | 2018–2020 | 2021 |
| O22 | 2018–2021 | 2022 |
| O23 | 2018–2022 | 2023 |

For each ledger, normalization and slot weights are fit only on its fit seasons using the already permitted fit-season result store. The target-season result namespace remains inaccessible. All target-season eligible predictions and raw edges are sealed before that target outcome namespace is opened. The outcome join then writes a separate immutable calibration ledger. Earlier OOF ledgers are never rewritten when later seasons become available.

Fold cards are:

- H22: target parameters from 2018–2021; alpha/beta from sealed O19–O21; evaluates 2022.
- H23: target parameters from 2018–2022; alpha/beta from sealed O19–O22; evaluates 2023.
- H24: target parameters from 2018–2023; alpha/beta from sealed O19–O23; evaluates 2024.

Each model card binds normalization, slot weights, frozen v5.2 component structure, OOF input and outcome ledger hashes, alpha/beta fitting diagnostics, permitted latest training date, source inventory, code commit, environment lock, and a model-card receipt. H22/H23/H24 are independent cards; no target fold can reuse parameters fit with its own target outcomes.

## 8. Historical target lock and 500 membership

The target runner processes every regular-season date from 2022 through 2024 in ascending official-date order. For each date it:

1. loads the correct H22/H23/H24 card;
2. builds every eligible reconstructed-participant candidate from the result-free store;
3. scores and ranks with unrounded probability, completeness, absolute starter edge, immutable historical `lock_basis_start_utc` copied from the verified schedule/game snapshot, then `gamePk`;
4. records `RUN_INVALID`, `SYSTEM_MISS`, `NO_ELIGIBLE_DATE`, or a single sealed Top 1;
5. seals the full date population, candidate rows, ranking, and source/card hashes.

Probability exactly 0.5 is nondirectional and cannot become the official Top 1. Same-date rank-2 replacement is forbidden.

Only after every target date lock is sealed does the outcome process start. Terminal states follow the frozen seven-day deadline contract. The final membership ledger takes the first 500 chronologically graded Top 1 dates. A VOID date is replaced only by the next later newly graded Top 1 date. Membership is immutable after sealing; later result changes are audit annotations only.

The runner fails rather than entering 2025 or returning fewer than 500 graded dates.

## 9. Metrics, evidence label, and deliverables

The report recomputes accuracy, Wilson interval, Brier score, log loss, ECE, maximum streaks, calibration, rank and probability slices, fold/season/month/team/home-away slices, completeness, component diagnostics, and provenance diagnostics.

Paired comparisons use the exact common populations:

- home baseline `N=500`;
- season-record baseline `N_season`;
- market baseline `N_market` only when strict pregame market evidence exists.

Each paired comparison reports the 2x2 table, accuracy delta, paired confidence interval, and exact McNemar result. The frozen robustness gate assigns only `ROBUSTNESS_SUPPORTED_NOT_CONFIRMED` or `ROBUSTNESS_NOT_SUPPORTED`; integrity failure assigns `RUN_INVALID`.

Required deliverables are source inventories, acquisition receipts, normalized snapshots, OOF ledgers, H22/H23/H24 cards, full target locks, outcome and membership ledgers, a run manifest, JSON/Markdown report, verified workbook, and clean-replay receipt.

## 10. Error handling and recovery

- Transient network failures retry with persisted attempt receipts; permanent HTTP, schema, semantic, or hash failures quarantine the child.
- Resume rereads and verifies every existing object before deciding it is complete.
- An interrupted publish cannot expose a success manifest without all bound files.
- Conflicting payloads for one canonical query are ambiguous evidence and require manual resolution; neither is selected automatically.
- A source verification failure prevents `SOURCES_READY`; a fitting failure prevents card publication; a lock failure prevents outcome access; a membership or metric failure prevents `VERIFIED`.
- Recovery is append-only. Published source bytes, OOF predictions, cards, locks, outcomes, and membership are never edited in place.

## 11. Testing and acceptance

Implementation follows TDD and subagent-driven development. Tests include:

- canonical query IDs, attempt receipts, retry/resume, split coverage, quarantine, atomic publication, and path attacks;
- protected legacy promotion success and raw/manifest/seal/schema/coverage tampering;
- grain-specific schedule, Statcast, pitching, career-BF, venue/timezone, and prior-history semantic recomputation;
- schedule played-subset versus Statcast coverage and referential integrity;
- preflight transition from `BLOCKED_REQUIRED_ACQUISITION` to `READY` without trusting sidecar declarations;
- D-1 and target-game exclusion for every feature family;
- O19–O23 prediction-seal-before-outcome-open behavior and fold-year boundaries;
- H22/H23/H24 card hash, latest-date, OOF, environment, and clean-replay checks;
- full-date ranking, outcome import prohibition, terminal states, VOID replacement, exactly-500 membership, and 2025 exclusion;
- metrics and paired-comparison primary recomputation;
- symlink, hard-link, rollback, concurrency, partial-write, and cross-namespace attacks;
- workbook formula, error scan, and rendered visual inspection.

Acceptance requires all focused and full isolated test modules, Ruff, `git diff --check`, phase-aware source/run verifiers, clean replay, manual audit of three dates per fold, and final artifact validation to pass. Test green status alone cannot promote a phase unless the real source and output inventories satisfy the same gates.

## 12. Explicit decisions

- Hybrid reuse is per child and always semantically reverified.
- Canonical raw root is `data/v5_2_qd/historical/raw`.
- Mutable progress lives outside the immutable RunSpec.
- Preflight and source verification are grain-specific and recompute semantics from raw bytes.
- Schedule and Statcast use a played-game subset relation, not impossible full-population equality.
- The venue timezone mapping is a reviewed effective-dated IANA table bound to official venue evidence.
- Historical final participants are reconstructed identity evidence only, never timestamped T-45 provenance.
- No H22 fitting begins before all required 2018–2024 evidence is `READY`.

## 13. Implementation decomposition

The end-to-end contract is one experiment, but implementation is divided into three gated plans so that no later subsystem can compensate for incomplete earlier evidence:

1. **Plan A — source contracts and hybrid acquisition:** phase registry, grain-aware evidence schemas, semantic verifier, protected legacy promotion, official fetching, normalized snapshots, and `SOURCES_READY` preflight.
2. **Plan B — rolling-origin preparation and fitting:** result-free feature snapshots, O19–O23 seal/open ledgers, and H22/H23/H24 model cards. This plan starts only from Plan A's sealed source inventory.
3. **Plan C — historical lock, outcomes, 500 membership, and reporting:** complete target-date locks, isolated outcome join, terminal resolution, exact membership, metrics, workbook, and clean replay. This plan starts only from sealed fold cards.

Each plan has its own TDD tasks, commits, spec/quality reviews, and completion receipt. Passing a plan does not mark the overall objective complete; only the Section 1 end state does.
