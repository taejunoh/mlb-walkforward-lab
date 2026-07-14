# MLB v5.2 Plan A Contract Amendment

Date: 2026-07-14  
Status: approved-scope contract correction; no acquisition evidence has been published under these corrected identities.

## Purpose

This amendment resolves implementation contradictions discovered while preflighting Plan A Tasks 5–14. It preserves the approved operating boundaries: Plan A ends at `SOURCES_READY`, fitting remains forbidden, protected v5.1 artifacts remain immutable, and existing user-owned files are preserved.

The original approved design and implementation plan remain immutable inputs. Acquisition contract schema v2 must bind both original documents and this amendment before any query or batch identity is generated.

## 1. Authoritative timezone path

The only production timezone mapping path is:

`config/v5_2_qd/venue_timezones.json`

Task 5 creates candidates and review receipts only in the audit namespace. Task 13 publishes the production mapping after two independent approvals. The flat path `config/v5_2_qd_venue_timezones.json` is forbidden.

## 2. Timezone review and seal

- Review receipts contain separate `reviewer_identity` and `reviewer_role` fields.
- The closed roles are `TIMEZONE_SEMANTIC_REVIEW` and `TIMEZONE_COVERAGE_REVIEW`.
- Exactly one approved receipt for each role is required, and reviewer identities must differ.
- Semantic review covers venue identity, coordinates, IANA timezone, and effective ranges.
- Coverage review proves every observed `(venue_id, official_date)` has exactly one mapping.
- Seal evidence binds candidate hash, final mapping path/hash, both review receipt paths/hashes/identities/roles, exact venue scope, and trusted UTC.
- Candidate rows remain `UNREVIEWED` and may have `time_zone: null`; only resolved `ZoneInfo` rows can seal.

## 3. Legacy promotion decisions

Legacy audit has three dispositions:

- `PROMOTED`: an exact protected candidate passes current parser, schema, semantic, and coverage validation.
- `FETCH_REQUIRED`: no usable exact candidate exists or a non-integrity semantic contract is not satisfied.
- `FATAL_INTEGRITY`: protected inventory, membership, manifest, seal, raw bytes, inode/link type, or catalog uniqueness is inconsistent.

Protected-state integrity failures never fall back to network fetch. Current planned `LogicalQuery` identity is authoritative; legacy query identity is provenance only.

## 4. Evidence and acquisition runner prerequisites

### 4.1 Read-only evidence API

Task 2 must expose verified read APIs that return the registered query, published evidence, raw bytes when applicable, query sidecar, success receipt, and attempt receipts after repeating all no-follow, inode, link-count, path, and hash checks. Enumeration is driven by sealed query batches, never directory discovery.

### 4.2 Derived disposition evidence

`PRESEASON_NO_CURRENT_SEASON_PITCHING` uses storage mode `DERIVED_DISPOSITION`. It has no raw object and no synthetic HTTP attempt. Its sidecar and receipt bind query identity, consumer identities, D-1 cutoff, and reason.

### 4.3 Acquisition stages

The authoritative stage order is:

1. `STATIC_SCHEDULES`: schedule anchors only.
2. `REFERENCE_INPUTS`: played boxscores, played-game D-1 pitching or disposition, teams, and venues.
3. `TIMEZONE_REVIEW`: sealed production timezone mapping gate.
4. `PARTICIPANT_DERIVED`: verified final-starter career BF queries.
5. `REMAINING`: Statcast promotion/fetch/split and final aggregate preparation.

`--stop-after reference-inputs` is a successful exit-zero checkpoint after the stage is fully verified. Checkpoints are append-only and bind prior checkpoint, stage, batch hashes, evidence receipt hashes, split receipts, pending/quarantine population, contract, RunSpec, code commit, and trusted UTC.

### 4.4 Split lineage

Timeout, size-limit, and row-cap splits publish a closed split receipt/batch before child execution. It binds the parent query/range, reason, exhausted attempt receipt path/hash, depth, ordered child queries/ranges, and parent batch hash. Resume reuses the sealed graph and does not refetch the parent. Recursive ancestry is proven by split receipts, not fabricated success sidecars.

### 4.5 Consumer population

D-1 pitching consumers are played games only. Career BF consumers are played games with complete verified final participants. Canceled or unplayed games remain in schedule/history populations but never become pitching or career-BF consumers.

### 4.6 Career endpoint and transport limits

Career BF uses `/api/v1/people/{pitcher_id}/stats` with by-date-range regular-season pitching parameters. League D-1 pitching remains on `/api/v1/stats`, uses `limit=5000`, and fails closed on incomplete or capped split counts.

The acquisition contract fixes response caps: Statcast 256 MiB, schedule 128 MiB, and boxscore/pitching/career/team/venue 16 MiB. Reaching a cap is not success.

## 5. Offline source verification

Task 8 imports no fetch or HTTP module. It uses verified evidence reads and sealed query batches, recomputes normalized snapshots from raw evidence, and publishes closed source inventory, replay receipts, and review selection.

Population classes are distinct:

- complete scheduled population;
- played population;
- verified feature-consumer population.

Statcast is compared with played games. D-1 pitching and career BF are compared with verified feature consumers. Missing Statcast/pitching/career evidence for unplayed games is not an error.

`READY_CANDIDATE` means source rows are semantically ready while the execution registry is still `ACQUIRING`. Inventory bytes do not change during phase transition. Effective readiness is the same inventory authenticated by `SOURCES_READY`; only preflight may then set `may_fit_H22=true`.

## 6. Preflight schema v2

Preflight v2 binds source inventory path/hash, registry phase and head receipt hash, acquisition contract hash, each source-season grain/status/snapshot hash, critical errors, source gate, and corrected acquisition commands. Legacy manifest scanning is diagnostic only. Career BF is never an annual-pitching alias.

`may_fit_H22=true` requires authenticated `SOURCES_READY`, exact registry/inventory binding, fresh offline byte-identical recomputation, all minimum and supporting source contracts ready, zero critical errors, zero network attempts, and absence of fitted cards.

## 7. Plan A phase policy and existing delivery files

- `DESIGN_ACCEPTED`: execution audit only; blocked preflight.
- `ACQUIRING`: raw evidence plus acquisition/execution audit; blocked preflight. Closed normalized source candidates may exist only as verifier-approved `READY_CANDIDATE` artifacts.
- `SOURCES_READY`: raw and normalized sources plus acquisition/execution audit; clean inventory and READY preflight; no features, OOF, cards, locks, outcomes, membership, metrics, or historical reports.
- Later phases fail closed until their plans are implemented.

Existing v5.1 diagnostic HTML and delivery receipt are out of Plan A historical scope and must not be deleted, moved, or regenerated. Validator accepts absent/absent or present/present; present pairs must pass full delivery validation, and mixed presence fails.

## 8. Preserved user-owned untracked files

Before Task 13, the following four pre-existing paths are recorded and must remain byte-for-byte unchanged, unstaged, and uncommitted:

- `mlb_backtest/v52_query_plan 2.py`
- `tests/test_v52_query_plan 2.py`
- `reports/v5_2_qd/walkforward_500_preflight 2.md`
- `schemas/v5_2_preflight.schema 2.json`

Unexpected additional pre-existing untracked paths stop network execution. Runtime sealed outputs use a separate explicit allowlist.

## 9. Prospective missed interval

Plan A never writes the prospective namespace. Dates from the prospective first-permitted date through live collector activation are not reconstructed. At activation, games whose T-50 boundary has passed become `SYSTEM_MISS`; backfill is forbidden. These dates never enter the historical 500 or Plan A source counts.

The Plan A completion receipt records prospective collection as not started/out of scope, the first-permitted date, null activation time, the missed calendar interval through completion, and `backfill_forbidden=true`.

## 10. Plan A completion receipt

Task 14 publishes one immutable receipt at:

`audit/v5_2_qd/historical/execution/completions/plan-a-{receipt_sha256}.json`

It uses canonical JSON plus one final newline, O_EXCL, no-follow, single-link validation, full writes, file and parent fsync, and no mutable latest pointer.

The receipt binds authenticated `SOURCES_READY`, phase receipt, acquisition contract v2 and amendment hashes, RunSpec, source inventory and checksum, preflight artifacts, two clean replays, manual source review, timezone mapping/seal/reviews, batch/checkpoint/split digest, acquisition statistics, quarantine reasons, protected v5.1 start/end fingerprints, code commit, verification/review receipts, elapsed time, fitted-card absence, and prospective handoff metadata.

Publishing this receipt does not advance beyond `SOURCES_READY`. Plan B consumes it as a trust root.

## Ordered implementation prerequisites

1. Bind this amendment in acquisition contract schema v2.
2. Correct career endpoint, authoritative timezone path, played-only consumers, and fixed limits before generating queries.
3. Add verified evidence reads and `DERIVED_DISPOSITION` storage.
4. Reverify Tasks 3–4.
5. Implement Tasks 5–10 under the amended contracts.
6. Run Tasks 11–12 release gates before any network access.
7. Preserve the four user-owned files, execute Task 13, and finish Task 14 without fitting.
