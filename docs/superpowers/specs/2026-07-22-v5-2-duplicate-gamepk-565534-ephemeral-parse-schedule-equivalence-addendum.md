# MLB v5.2 Duplicate `gamePk` 565534 — Ephemeral `parse_schedule` Equivalence Addendum

**Status:** approved narrow design authority; no production evidence is changed
by this document.

**Scope:** this addendum applies only to the source-only superseding run
`historical_walkforward_500_superseding_565534`. It grants no permission to
alter, resume, or reinterpret the original `historical_walkforward_500` run.

## 1. Governing authorities

This addendum supplements, and otherwise leaves unchanged, these committed
authorities:

| Authority | Repository path | Current working-tree SHA-256 | Current Git blob ID |
|---|---|---|---|
| implementation plan | `docs/superpowers/plans/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run.md` | `1646b02f9e636465534cdb02f688cb6295e7d9fba856572f375b315427f8978f` | `8f0d9dabd438ea0c16e0154fed957278802e8781` |
| approved amendment | `docs/superpowers/specs/2026-07-21-v5-2-duplicate-gamepk-565534-option-1-superseding-run-amendment.md` | `c2da782f68b6ef39f3df88423139173b2c10520ebba7dd623ed294679f478476` | `7df02d3fe0ab4ad27f574f2ca31be2d3e6cc7855` |

The hashes above bind the exact authorities reviewed for this addendum. A
different byte sequence or blob is not covered.

## 2. Conflict and sole override

The amendment requires `project_schedule_occurrences()` to suppress the exact
authenticated `(date_index=54, game_index=5)` occurrence, run the unchanged
public `parse_schedule` over an in-memory view, and compare its 2,430-game
normalized result directly with the independently mirrored reconciliation.
The same amendment says that the recomputer “never serializes replacement raw
bytes”; the plan repeats “Never return or serialize replacement raw bytes.”
Because the unchanged public `parse_schedule` API accepts a path or bytes, the
required in-memory equivalence call cannot be made without serializing that
temporary view to bytes.

This addendum overrides only those absolute no-serialization sentences. Within
the duplicate-disposition recomputation for this one superseding run, the
filtered in-memory view may be serialized as canonical JSON bytes solely to be
passed immediately as the bytes argument to the unchanged public
`parse_schedule`. No other serialization, parser-policy exception, or raw-data
substitution is authorized.

All other requirements of the plan and amendment remain authoritative,
including the exact pair fingerprints, fixed fields, occurrence counts,
ordinary duplicate reconciliation, normalized and selected-provenance digests,
namespace isolation, derived-disposition lifecycle, and fail-closed behavior.

## 3. Ephemeral-bytes boundary

The permitted canonical JSON bytes:

- exist only in process memory during one duplicate-disposition recomputation;
- are used only as the direct argument to unchanged public `parse_schedule`;
- are discarded after that call and are never persisted, published, returned,
  cached, logged, checkpointed, or placed in a receipt or sidecar;
- are never stored in `DuplicateGamePkDispositionAuthority`,
  `ScheduleOccurrenceProjection`, or any other authority/projection object;
- are never treated as fetched raw evidence, a raw object, a content hash
  authority, provenance, or a replacement response; and
- never change or normalize the authenticated old raw bytes, their SHA-256,
  byte count, root/date totals, occurrence positions, or selected raw-object
  hashes.

The immutable quarantined 2019 response remains the sole schedule source
authority. Its authenticated raw SHA-256 remains
`6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0`,
its byte count remains `21182105`, and its raw population remains 2,472 regular
occurrences, 2,430 unique `gamePk` values, and 42 duplicate IDs. The ephemeral
view is a computation input only and has no raw or provenance identity.

`mlb_backtest/source_parsers/schedule.py`, its public `parse_schedule` API, and
its ordinary reconciliation rules remain byte-for-byte unchanged. The
duplicate-disposition module must compare the independently mirrored normalized
population directly against the unchanged `parse_schedule` result in canonical
JSON, including stable first-seen order. Digest comparison alone is not a
substitute for direct normalized-output equality.

## 4. Failure and tamper behavior

Recomputation fails closed before publication if authentication, the exact
special pair, any fixed predicate, any raw/date/duplicate total, ordinary
duplicate reconciliation, direct normalized-output equality, or either
canonical digest differs. Failure must not publish a sidecar, success receipt,
raw object, or any derivative of the ephemeral bytes.

Tests may retain ephemeral bytes only in test-process memory long enough to
assert the call boundary. Test fixtures, failure diagnostics, and temporary
filesystem artifacts must not contain a serialized replacement of the
authenticated 2019 response.

## 5. Superseding-contract binding and rehash gate

Before any registration, disposition publication, checkpoint, registry
creation, acquisition, or other evidence operation for
`historical_walkforward_500_superseding_565534`:

1. Commit this addendum and authenticate its working-tree SHA-256 and committed
   Git blob ID.
2. Add exactly one fourth member to the superseding acquisition contract's
   `bindings`, keyed `ephemeral_parse_schedule_equivalence_addendum`.
3. That member must contain exactly `path`, `sha256`, and `git_blob_id`. Its
   `path` must be
   `docs/superpowers/specs/2026-07-22-v5-2-duplicate-gamepk-565534-ephemeral-parse-schedule-equivalence-addendum.md`; the two digest values must be
   computed from that committed file and must authenticate at every contract
   load and rebind.
4. Update the exact superseding acquisition-contract schema branch and literal
   oracle for the four-member binding object without broadening the default
   branch.
5. Re-serialize and rehash the superseding acquisition contract, then recompute
   every dependent superseding query/receipt identity before any evidence is
   created. No caller-supplied or stale pre-addendum contract hash is accepted.

At approval time, no file exists under the superseding run's data, audit, or
report namespaces. Therefore no superseding evidence requires migration; if
such evidence appears before the binding and rehash gate is complete, stop and
quarantine the operation rather than adopting it.

The following remain byte-identical: the governing plan and amendment above,
`protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json`, the
default `protocols/v5_2_qd/historical_source_acquisition.json`, and every old-run
file under `data/v5_2_qd/historical/` and `audit/v5_2_qd/historical/`. Only the
superseding acquisition contract and its exact schema/oracles may be revised to
bind this addendum.

## 6. Acceptance criteria

Acceptance requires offline tests proving all of the following:

- the exact authenticated 2019 raw is the sole input authority and remains
  byte-identical before and after every success and failure case;
- only the suppressed occurrence is absent from the ephemeral view, while the
  authenticated raw totals and hashes remain unchanged;
- unchanged public `parse_schedule` receives canonical JSON bytes in memory and
  its full normalized output equals the independently mirrored 2,430-game
  output directly and canonically;
- neither the authority nor projection contains the ephemeral bytes, and no
  filesystem, evidence-store, receipt, sidecar, checkpoint, or log receives
  them;
- every pair/totals/digest/output tamper fails before publication;
- `mlb_backtest/source_parsers/schedule.py` has no diff and its existing strict
  parser tests remain green;
- the superseding contract has exactly four authenticated bindings, including
  the exact addendum object defined in §5, while the default contract branch is
  unchanged; and
- the protected old-evidence manifest and all fixed old authority hashes pass
  before and after the test suite, with no network attempt.

## 7. Non-goals

This addendum does not authorize a generic duplicate policy, a `last record
wins` rule, parser or API changes, filtered-raw publication, new raw provenance,
evidence migration, network acquisition, phase advancement, changes to v5.1,
changes to the old v5.2 run, or downstream model/feature/outcome authority. It
does not relax any integrity, namespace, lifecycle, schema, or immutability
requirement outside the single ephemeral `parse_schedule` equivalence call
defined here.
