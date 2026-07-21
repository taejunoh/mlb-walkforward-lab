# v5.1 Experimental Live Daily Top 3 Design

Date: 2026-07-21  
Status: Approved design

## 1. Objective

Deliver a locally runnable, one-command experimental view of today's MLB Top 3
without waiting for PR #1 hardening Task 14 or for the v5.2 historical 500 run.
The command reuses the frozen v5.1 scoring constants while acquiring current,
pregame observations and current-season D-1 history in a new namespace.

This is an operator MVP for iterative use. It is not a production betting tool,
a continuation of the v5.1 historical 100-pick sample, a v5.2 result, or an
official prospective-validation ledger.

## 2. Operator interface

The initial interface is one local command:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m mlb_backtest.daily_top3
```

The default target date is the current date in `America/New_York`. An explicit
`--date YYYY-MM-DD` option exists for deterministic testing and controlled
same-day operation. The command prints the classified Top 3 rows and the exact
paths of its Markdown and JSON outputs.

The first run may bootstrap the 2026 result-free history cache. Later runs
verify the cache and acquire only missing or changed observations.

## 3. Isolation and labels

The model label is exactly `v5.1-EXPERIMENTAL-LIVE`.

All source observations, receipts, caches, and reports live outside the existing
v5.1 backtest and v5.2 historical/prospective namespaces. The default roots are:

- result-free cache: `data/experimental_daily/`
- observation receipts: `audit/experimental_daily/`
- reports: `reports/experimental_daily/YYYY-MM-DD/`

No row from this namespace may be appended to or pooled with:

- the v5.1 retrospective 100-pick diagnostic;
- the v5.2 historical 500-pick experiment; or
- the formal 2026 prospective ledger.

Every human-readable output states `RESEARCH ONLY`, `NON-OFFICIAL SAMPLE`, and
`PROFITABILITY NOT VALIDATED`. The implementation does not calculate or claim
ROI, CLV, betting actionability, or prospective confirmation.

## 4. Architecture

The live path is new and narrow. It does not route operation through the
historical `mlb_backtest.pipeline` stage CLI.

```text
daily_top3 CLI
  -> frozen-card validator
  -> current schedule and participant snapshot service
  -> D-1 result-free history cache
  -> live feature adapter
  -> existing component scoring, probability, and ranking math
  -> candidate classifier
  -> Markdown and canonical JSON renderer
```

Component boundaries:

1. `daily_top3` parses arguments, invokes the service, chooses documented exit
   codes, and prints results. It contains no source or scoring logic.
2. The frozen-card validator loads normalization constants, batting-slot
   weights, `alpha`, and `beta` from the existing audited v5.1 model card. It
   authenticates the expected model version and digest without applying the
   historical 2025 date-window configuration.
3. The live source service stores the exact response bytes, URL/query,
   observation time, selected HTTP metadata, SHA-256, and parse result for the
   target-date schedule and participant observations.
4. The history service creates a result-free current-season view ending at D-1.
   It may reuse existing generic URL builders, parsers, raw-store primitives,
   feature history, and as-of assertions, but not historical actual-starter
   reconstruction.
5. The live feature adapter preserves announced versus probable starter
   provenance and observed lineup provenance while calling shared pure feature
   calculations. Existing v5.1 historical behavior remains unchanged.
6. Existing scoring and deterministic ranking math is reused after a valid raw
   live feature row exists.
7. The output service writes a new, run-specific report and never overwrites an
   earlier observation.

## 5. Result-free data boundary

For target date `D`, every performance feature uses events completed no later
than `D-1`. Target-date pitches, plate appearances, scores, outcomes, standings
updates produced by target games, and other in-game performance are forbidden.

The live source projection passed to feature and rank code excludes result and
market fields. Outcome, score, odds, and market modules are not imported by the
daily command. The command rejects a candidate if any bound feature input has a
source game date on or after the target date.

The frozen v5.1 model card is read-only. The daily command never recalibrates,
refits, changes component weights, changes shrinkage constants, or writes a new
model card using 2026 observations or outcomes.

The exact admitted model-card file digest is:

```text
9bad89b9f4896a8f8862d3c304fd605921eca2d825fc2340bf51da01f7912759
```

## 6. Candidate states

Candidate states are mutually exclusive. Rankings are independent across
states; a weaker state never changes a stronger state's rank.

### 6.1 `VERIFIED`

A candidate is `VERIFIED` only when:

- the observation was stored before the scheduled start;
- both announced starting pitchers are present;
- each team has exactly nine distinct, ordered announced starting hitters;
- required D-1 history and source receipts pass validation; and
- the scheduled-start identity used for classification still matches the
  current observation.

Here, `VERIFIED` means verified from the saved local pregame observation. It
does not mean `TIMESTAMP_VERIFIED_T45`, independently certified, or admitted to
the formal prospective sample.

### 6.2 `PRELIMINARY`

A candidate is `PRELIMINARY` only when every input required for a truthful score
exists, but one or both starter identities remain explicitly probable rather
than announced.

The output preserves the exact preliminary reasons. Preliminary candidates are
ranked only against other preliminary candidates. Observation relative to the
future formal T-50/T-45 window is reported as metadata but does not change this
experimental state; this command does not claim formal T-45 admission.

### 6.3 `UNSCORED`

A game is `UNSCORED` when a starter, an ordered nine-player lineup, critical
history, or another required scoring input is unavailable or invalid. The
output records reason codes but does not assign a probability or rank.

The MVP does not fabricate lineup positions, reuse a previous game's lineup,
or neutral-fill missing participants. A future `SHADOW` rank requires a
separately approved deterministic projection source and policy.

Games already in progress, final, canceled, or postponed at observation time
are outside all Top 3 populations and receive an explicit disposition.

## 7. Ranking and output

The command produces independent `verified_top3` and `preliminary_top3` arrays,
plus `unscored_games` and excluded-game dispositions. A Top 3 array may contain
zero, one, or two rows; it is never padded.

Each scoreable population uses unrounded values and the frozen order:

1. predicted-winner probability, descending;
2. data completeness, descending;
3. absolute starting-pitcher edge, descending;
4. scheduled start UTC, ascending; and
5. `gamePk`, ascending.

Every selected row includes the target date, game identity, teams, predicted
winner, unrounded and displayed probability, state, rank, data completeness,
starter and lineup provenance, scheduled start, observation time, reason codes,
model-card digest, feature digest, and source-observation digests.

Every run-specific JSON document also binds its schema version, command inputs,
Eastern and UTC execution times, source-cache cutoff, excluded populations,
warnings, and a deterministic content digest. The Markdown report is a rendered
view of that JSON authority.

## 8. Execution flow

One invocation performs these steps in order:

1. resolve and validate the Eastern target date and observed local system time;
2. load and authenticate the frozen v5.1 model card;
3. verify the existing experimental cache;
4. acquire missing current-season D-1 source history;
5. save today's schedule and participant observations with receipts;
6. build only result-free, as-of-safe live features;
7. classify every scheduled game;
8. score and rank the verified and preliminary populations independently;
9. create new JSON and Markdown reports; and
10. print the Top 3 rows, warnings, unscored count, and output paths.

Re-running the command never replaces a prior report. A new observation creates
a new run record so lineup changes and scheduled-start revisions remain visible.

## 9. Failure handling

Global integrity failures produce no ranking and a nonzero exit:

- frozen-card identity or digest mismatch;
- invalid or unsupported target date;
- invalid result-free cache;
- unavailable or invalid target-date schedule population;
- source bytes or receipt mismatch; or
- any target-date leakage into feature inputs.

A validated D-1 cache may be reused during a transient network failure. Today's
schedule, participant, or start-time observations are never silently replaced
by stale values.

Game-specific missing data does not fail the whole slate. It produces
`UNSCORED` with exact reasons. A source failure affecting only one candidate may
downgrade or exclude only that candidate when the remaining slate's authority
is still complete.

No exception path emits a partial Top 3 as successful. A failure report may be
written under the experimental audit namespace, but it is not a ranking report.

## 10. Testing and acceptance

Implementation follows test-first development. Network behavior is tested with
frozen response fixtures; a real same-day smoke run is a separate final gate.

Required automated coverage:

1. frozen-card version/digest and exact scoring-constant loading;
2. a 2026 date works without the historical 2025 candidate-window config;
3. schedule parsing applies an explicit duplicate-identity contract and rejects
   conflicting game identity;
4. D-1 cutoff rejects target-date performance evidence;
5. announced, probable, missing, post-start, and changed-start participant cases map
   to the exact candidate state;
6. missing or partial lineup and missing starter never receive a score;
7. at least four scoreable games reproduce existing scoring math and select the
   deterministic top three;
8. verified and preliminary populations never compete with each other;
9. no array is padded and no unscored row gains probability or rank;
10. network and parse failures cannot use stale current-day observations;
11. JSON schema, Markdown projection, content hashes, and exit codes are stable;
12. imports and produced fields prove outcome, score, odds, and market isolation;
13. repeat runs preserve earlier reports and bind their own observations; and
14. writes remain inside the experimental namespaces.

The MVP becomes locally usable before hardening Task 14 when:

- all focused Daily Top 3 tests and affected regression tests pass;
- static checks and `git diff --check` pass;
- an independent spec review and code-quality review approve the implementation;
- a live read-only acquisition/scoring smoke run for the current date completes
  without outcome access; and
- valid JSON and Markdown reports are produced and manually inspected.

If the live slate lacks enough complete pregame inputs, the smoke run may
legitimately produce fewer than three verified or preliminary rows. That is a
valid operational result, not a reason to invent candidates.

## 11. Parallel work policy

This MVP does not block or replace the existing workstreams:

- PR #1 filesystem and registry hardening continues through Tasks 2-14;
- v5.2 historical source acquisition and semantic fixes continue separately;
- v5.2 OOF fitting, fold cards, target locking, 500 membership, and verification
  remain their own historical experiment; and
- future automation invokes the same one-command service only after the manual
  path is stable.

Shared pure scoring behavior may be reused, but experimental live artifacts and
historical/formal phase authorities remain physically and logically separate.

## 12. Explicit non-goals

The MVP does not include:

- automatic scheduling or background polling;
- a T-50/T-45 formal clock-evidence ledger;
- projected lineups or Shadow ranking;
- outcome joining or retrospective grading;
- odds, ROI, CLV, or betting recommendations;
- v5.2 model fitting or claims;
- multi-user or hostile-filesystem deployment; or
- merging experimental rows into any validation sample.
