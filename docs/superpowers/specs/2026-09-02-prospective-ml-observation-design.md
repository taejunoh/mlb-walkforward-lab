# Prospective ML observation — approved narrow design

Status: **USER APPROVED on 2026-09-02; implemented and verified; not activated.**

## Purpose

Measure whether internally produced pregame probabilities match subsequent
results. Reuse existing live model, participant evidence and immutable traces;
do not wait for totals or multibook availability to begin probability-only
observation. This is research recording, not live-model promotion or betting.

The prior temporal-calibration experiment remains retrospective and is not
the active model. No model card, weights, features, scoring or PLAY gate changes.

## Verified starting point

`market_play_authorities.load_ml_authority()` authenticates the existing ML
report/trace and its schedule/participant evidence. Ordered batting lineups
and starter IDs remain in bound `LIVE_PARTICIPANTS` payloads and must be
extracted from those payloads, not inferred from report gate booleans.

The market capture, board and trace-committed PLAY/PASS report already exist.
Current direct-book evidence is incomplete. The active capture selection is
explicitly listed in
`audit/market_observations/2026-09-02/generated/market-artifact-supersession.json`;
superseded wrong-unit captures must not be selected by globbing.

No matching prospective settlement/performance ledger was found. The legacy
`outcomes.join_outcomes()` rewrites evaluated JSONL and is not the appropriate
append-only mechanism. The larger v5.2 prospective protocol is out of scope.

## Alternatives and recommendation

1. **Recommended: ML-first observer.** Reuse a sealed ML trace; record market
   and totals as NOT_EVALUATED when absent. Collect valid probability evidence
   independently of market availability.
2. Require a complete combined market report first. Reuses more report fields,
   but unnecessarily blocks probability recording on missing totals/market inputs.
3. Build the full v5.2 prospective system. Much broader and not needed for this
   narrow measurement step; not selected.

## Snapshot and population

One new small observer reads existing sealed authorities and persists an
append-only snapshot. It copies game identity, model-card digest, probability,
predicted side, numeric feature completeness, exact ordered nine-batter lineups,
expected/observed starter IDs, source paths/digests and recorded UTC timestamps.

Primary observations require valid finite probability, confirmed nine unique
batters on each side, matching starters, no participant conflict/scratch and
valid pregame schedule/revalidation evidence. Persist the new snapshot before
the recorded game start using the execution clock, not a caller-supplied past
time. Preserve existing freshness/revalidation requirements. Do not reconstruct
a past primary record from an old report or claim file hashes independently
prove historical capture time.

Within each game and frozen-model version, only the first eligible snapshot
committed to the observer after activation is primary. Enforce that selection
atomically; exact repeats are idempotent and different later snapshots are
diagnostic only. Record missing/invalid observations and reasons without
inventing a probability or later upgrading the original record.

Feature completeness below 0.50 does not erase a valid probability observation.
Report all-primary and the predeclared <0.50 / >=0.50 strata separately. The
existing 0.50 research-PLAY gate remains unchanged. Do not combine model-card
versions or repeated snapshots as independent games.

## Market boundary

No market input means `NOT_EVALUATED`, with null quote/trace references—not
PASS, PLAY, zero odds, or fabricated evidence. An optional existing market
trace may be copied only in the same snapshot call, after validating its exact
ML-trace binding and contemporaneous timing. Explicitly supplied invalid or
mismatched evidence rejects the call rather than silently becoming absent.
Never append later prices to an existing primary snapshot.

No new PLAY calculation, O/U model, no-vig policy, staking strategy or profit
claim is included. Existing decisions are copied unchanged. Market-return and
O/U settlement calculations are a subsequent scoped extension, not a condition
for completing this ML probability observer.

## Official results and report

Keep outcome ingestion separate from snapshot creation. Admit supplied official
MLB raw result bytes plus their source receipt and digest. The offline join
never calls the live collector or re-scores a game. Validate game/date/team
identity, source provenance, observed time and a non-tied final winner belonging
to the two teams. Unknown/malformed identities reject; nonfinal, suspended,
postponed, tied or no-contest states remain explicit non-evaluable records.
They never become losses or disappear from coverage counts.

Write an immutable settlement that references its snapshot digest. A later
official correction is a separate revision referencing the earlier record;
never overwrite or silently double-count it. Reports must name the input
snapshot/settlement set and resolved revisions, retaining pending/excluded counts.

Compute accuracy, Brier score, log loss and fixed 0.1-wide reliability bins
from joined primary records only, with model and completeness strata. Empty
cohorts have null metrics, not zero error. Sample size and uncertainty remain
visible. No invented minimum sample or automatic promotion threshold.

## Focused acceptance tests

- Valid sealed ML-only input works without any market/totals artifacts.
- Snapshot stage cannot access final-result inputs; no outcome/network import.
- Started, incomplete, stale, scratch/conflicting and malformed inputs cannot
  claim primary admission; exact lineups/source bindings are preserved.
- Concurrent/repeated snapshots produce at most one primary per game/model.
- Authority mutation, missing trace and mismatched optional market trace reject.
- Final-result winner/identity errors reject; pending/exceptional states and
  corrections do not become losses or duplicate observations.
- Known synthetic probabilities reproduce hand-calculated metrics; empty
  samples and repeated/model-version cohorts have correct denominators.
- Relevant existing model/admission tests pass; no long historical suite,
  refitting or protected historical holdout access.

## Boundaries and review

Work only in the existing LFG worktree and new task-owned namespaces. Preserve
all user changes and historical evidence. No Documents, custody/production,
privileged commands, forbidden manifest access, commits, push, merge or PR.
No background scheduler is created by this design. Authorized external capture
can be exercised after implementation; tests are hermetic. Existing pre-activation
records are reference/diagnostic evidence, not newly claimed prospective cases.

Design review checked scope, authority separation, duplicate handling, missing
market semantics and unchanged model/admission gates. The user approved this
written scope with “승인” on 2026-09-02. Implementation proceeds without another
design-approval round; material scope expansion remains excluded.
