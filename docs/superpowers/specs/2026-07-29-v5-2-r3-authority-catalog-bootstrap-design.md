# v5.2 R3 Authority Catalog Bootstrap Design

**Status:** Approved direction A on 2026-07-29  
**Scope:** Correct the Task 1 trust boundary for the approved r3 missing-venue recovery run without changing any r2 or protected evidence byte.

## Problem

The first Task 1 implementation attempted to build the r3 disposition catalog before the tracked r3 RunSpec, acquisition contract, and query-plan authority existed. Tests therefore injected synthetic authenticated contexts and three-query static batches.

That ordering created the wrong trust boundary:

- the real static batch contains seven season queries, not three injected projection queries;
- caller-supplied `LogicalQuery` objects could omit or mutate identity fields;
- Python-private construction tokens could be discovered and reused;
- frozen dataclasses still retained mutable nested dictionaries after validation; and
- local validation patches could not prove that the public factory consumed the real sealed r3 authority.

The catalog must be derived from authenticated repository artifacts, not from caller assertions that data is authenticated.

## Decision

Move the minimum Task 4 authority bootstrap ahead of Task 1 and replace the injected catalog builder with one public factory:

```python
build_missing_venue_recovery_disposition_catalog(
    root: Path,
    run_spec_path: Path,
    acquisition_contract_path: Path,
) -> MissingVenueRecoveryDispositionCatalog
```

The bootstrap and catalog replacement are one atomic implementation stage. Task 1 is not complete until the real public factory succeeds against the tracked r3 authority and rejects every tested mutation.

## Authority Bootstrap

The bootstrap creates only the authority needed to produce and authenticate the real seven-query static schedule batch:

- the canonical r3 RunSpec;
- the canonical r3 acquisition contract;
- the exact r3 branch in `protocols.py`;
- the exact r3 query-plan authority/static table and schema-index mapping;
- the minimum RunSpec and acquisition-contract schema branches required to load those two artifacts.

The r3 identity remains:

- run ID `historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3`;
- parser and policy version `v5.2.3` for recovery disposition;
- source years 2018 through 2024;
- four disposition records `(565534, 630882, 633224, 632924)`;
- three derived disposition projection queries;
- shared 2021 projection for records 633224 and 632924; and
- exact exclusion set `{632924}`.

All downstream inventory, snapshot, checkpoint, receipt, timezone, and finalization schema branches remain in the later sealing task. The bootstrap does not execute acquisition or create runtime r3 evidence.

R2 RunSpec, contract, static table, query IDs, batch ID, schemas, and runtime namespaces remain byte-for-byte unchanged.

## Public Factory Data Flow

The public factory performs these steps in order:

1. Load and authenticate the exact r3 RunSpec and acquisition contract from their tracked paths.
2. Call the production `build_static_batch()` path.
3. Validate the complete seven-query batch:
   - stage is `STATIC_SCHEDULES`;
   - no derived parent is present;
   - batch ID is exact;
   - count and order are exactly 2018 through 2024;
   - each query ID is exact; and
   - canonical query bytes match the sealed table.
4. Select the three disposition projection queries from the authenticated seven-query batch.
5. Load the protected parent evidence through the production parent-evidence reader.
6. Authenticate the 632924 registration, attempt, quarantine receipt, raw file, raw position, canonical no-LF object digest, game identity, teams, score, terminal status, and exact null-venue object.
7. Construct and return the immutable four-record/three-query catalog.

No public or test-facing production builder accepts a caller-supplied context, static batch, parent catalog, semantics mapping, or free-form run ID. Pure validation helpers may accept values for mutation testing, but the public factory always obtains those values itself and validates them fully.

## Canonical Immutable Value Model

Python naming conventions and hidden tokens are not integrity boundaries. Correctness comes from canonical equality and immutable storage.

The r3 catalog stores nested query and semantics content as canonical JSON bytes, not caller-owned dictionaries:

- canonical query bytes include source, grain, endpoint, parameters, full requested range, media type, parser version, parent query ID, RunSpec SHA, and contract SHA;
- canonical record-semantics bytes include all parent paths, sizes, hashes, occurrence position, canonical object digest, action/reason, shared-query mapping, and exclusion data;
- records, projection query IDs, and record-to-query indexes are tuples;
- excluded game IDs are a `frozenset`; and
- accessors that need a `LogicalQuery` or plain mapping reconstruct a fresh value from canonical bytes.

Constructors revalidate exact sealed templates. A direct constructor may create only a value canonically identical to the factory result. Mutating an input dictionary after construction cannot change catalog state.

## Shared Projection Capability

Task 2 and Task 3 consume a small read-only projection capability:

```python
@dataclass(frozen=True)
class RecoveryDispositionProjection:
    record_game_pks: tuple[int, ...]
    projection_query_ids: tuple[str, ...]
    record_to_projection_indexes: tuple[int, ...]
    excluded_game_pks: frozenset[int]
```

R2 adapts to:

```text
records: (565534, 630882, 633224)
indexes: (0, 1, 2)
excluded: {}
```

R3 adapts to:

```text
records: (565534, 630882, 633224, 632924)
indexes: (0, 1, 2, 2)
excluded: {632924}
```

Existing r2 public methods and query IDs do not change.

## Failure Behavior

The factory fails before returning a catalog when any of these differ:

- RunSpec, contract, Git binding, or tracked path;
- static batch count, order, stage, batch ID, query ID, or any canonical query field;
- a fourth or standalone 632924 projection query;
- record order or record-to-query mapping;
- parent evidence path, size, hash, or occurrence position;
- the 632924 canonical raw-object digest or authenticated game semantics;
- action, reason, parser/policy version, shared-query mapping, or exclusion set; or
- canonical catalog template.

It never infers a venue or timezone and never weakens the r2 fail-closed venue rule.

## Test Strategy

The implementation follows RED–GREEN TDD:

1. The real public factory must fail before the r3 bootstrap exists.
2. After bootstrap, the public factory must build four records, three projections, indexes `(0, 1, 2, 2)`, and exclusion `{632924}` from the protected root.
3. A three-query static batch is rejected.
4. Every field of each of the seven canonical queries is mutation-tested, including non-projection queries.
5. Batch ID, order, duplicate queries, query IDs, and a standalone 632924 query are rejected.
6. Parent path/hash/size/position/object/game/venue mutations are rejected.
7. Constructor and post-construction aliasing tests prove that mutable input changes cannot affect the catalog.
8. R2 catalog, query IDs, static table, and projection adapter remain exact.
9. Ruff, Python compilation, canonical JSON, schema validation, and `git diff --check` pass.
10. Spec-compliance and code-quality reviews must both report no P0/P1/P2 before Task 2 begins.

## Preservation and Execution Boundary

This stage only creates source code, tests, and canonical protocol/schema bootstrap files. It does not write r3 runtime evidence or touch the protected source root.

Before the later single r3 run, the preservation manifest will recheck:

- r2 RunSpec, contract, evidence, and runtime namespaces;
- four protected parent trees;
- 28 protected old-run files;
- v5.1 inventory, model, and calibration artifacts; and
- all previously recorded parent and preflight hashes.

The current diagnostic exact-suite run that started at `cdde3c5` is not final evidence because its worktree HEAD changed while it was running. Final exact verification will use a clean, detached worktree at the reviewed r3 HEAD.
