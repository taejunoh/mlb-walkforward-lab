# Market event time matching

The user approved the following bounded rule on September 6, 2026: preserve
source times, allow at most 60 seconds for an unambiguous same-game match,
reject ambiguity, and retain official-time pregame gates. Written-spec review
precedes implementation. No implementation or commit is part of this document.

## Purpose and alternatives

FanDuel's observed MIN–CWS and WSH–LAD starts were one minute later than the
existing official schedule. Exact-only matching rejects those observations.
Ignoring time would permit incorrect matches; manual event overrides would
require repeated judgment. The selected approach is a deterministic, bounded
matching rule with a retained audit trail, not a change to model confidence.

## Matching contract

Only new `PROVISIONAL_MULTIBOOK_MARKET_BOARD_V3` boards use this rule.

1. Use existing ordered away/home normalization and exact official UTC start.
   Accept only one exact candidate; reject multiple exact candidates.
2. If no exact candidate exists, consider all games in the authenticated
   official target-date slate with the same ordered away/home teams. There
   must be exactly one such game, and the absolute source-versus-official
   start difference must be at most 60 seconds, inclusive.
3. Multiple same-team games prohibit the tolerance fallback even when only
   one is within 60 seconds. An exact, unique doubleheader match remains valid.
4. Reject unmapped events, swapped home/away teams, duplicate event identities,
   duplicate book-to-game mappings, and differences greater than 60 seconds.
   Do not guess a local date from a clock-only observation: the raw capture
   must already have an explicitly supported full UTC start.

Use integer or exact duration arithmetic, not rounded minutes or floats.
The official slate determines target-date membership, including games whose
UTC start falls on the following date.

## Original values and audit

The raw capture retains its original `scheduled_start_utc`. The normalized
board game retains the official `scheduled_start_utc`. Neither raw bytes nor
historic boards are rewritten to make a match succeed.

For each mapped source event, retain book ID, source event ID, raw relative
path and SHA-256, source start, official start, signed source-minus-official
delta as integer `delta_nanoseconds`, matched game ID, and rule `EXACT` or
`UNIQUE_ORDERED_TEAMS_60S`. Preserve deterministic ordering and reject audit
entries that do not reconstruct from the authenticated source and schedule.

V3 retains capture-outcome relative path, actual byte SHA-256, sidecar SHA-256,
and book binding. Loading a V3 board reopens its outcome and raw capture using
the existing secure validators, verifies embedded outcomes, quotes and match
audit against those sources, and retains all required snapshots for final
rebind. Missing or modified source evidence rejects the V3 board. Preserve the
existing official schedule evidence chain and daily pre-persistence rebind.

## Compatibility and unchanged gates

V1 exact-five-book and V2 actual-one-through-five-book matching and loading
semantics remain unchanged. V3 supports the same actual-one-through-five-book
coverage as V2. New normalization defaults to V3; explicit legacy versions
continue using exact-only matching. Raw capture and outcome schemas remain
unchanged. No changes to evaluator policies, model probabilities, report/trace
schemas or minimum eligible coverage are included.

Quote collection/provider timestamps must not be refreshed or substituted.
The existing 300-second freshness limit remains. Pregame eligibility continues
to use the official start, never a later sportsbook display time. At the
official start, the existing GAME_STARTED gate must still reject a pregame
play even if the source time is 60 seconds later.

Successful event matching does not establish location eligibility, executable
prices, profitability or independent probability calibration. Observations
with incomplete location verification remain research evidence, not a claim
of a verified executable New York price. Old observation notes are not
automatically promoted into canonical captures by this change.

## Scope and verification

Expected implementation scope: market-board module, board JSON schema,
focused board/capture/daily-consumer integration tests, and usage guide.
Avoid unrelated refactoring or changes to prior dirty files. Work only in the
existing LFG worktree; no Documents, protected/custody/production/recovery
paths, forbidden manifest, installations, network collection, full exact suite,
commits, push or merge are part of implementation.

Required tests: unchanged exact legacy V1/V2; exact V3; both observed +60-second
events; -60-second boundary; values beyond either boundary; swapped teams;
ambiguous doubleheaders; duplicate events/mappings; next-UTC-day slate member;
audit/source/outcome/sidecar tampering at load and final rebind; persistence
round-trip; zero-quote PASS; official-time GAME_STARTED despite later source
start; unchanged freshness timestamps and limits. Use synthetic fixtures for
these tests and retain existing real observations unchanged.
