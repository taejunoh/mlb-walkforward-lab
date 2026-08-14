# Frozen Acquisition-Schema Test Alignment Design

**Status:** Approved design; repository-local test and fixture alignment only.

**Baseline:** Expected candidate `5b8e4a7` (`fix: bind reachable R4 execution modules`).

**Decision:** Align the five acquisition-schema test failures/errors with the
already normative production schema, contract, and fail-closed verifier. This
document authorizes no production schema change, contract change, verifier
relaxation, evidence operation, or custody operation.

## Gate result and problem statement

The exact gate at the baseline collected **1,480** tests and reported
**4 failures, 6 errors, and 2 skips**. The ten non-skip defects split into two
clusters:

| Cluster | Count | Disposition |
| --- | ---: | --- |
| Frozen acquisition-schema alignment | 5 | In scope for this design |
| Chromium runtime/bundle | 5 | Explicitly out of scope; no changes here |

The schema cluster is a test-contract mismatch, not evidence that production
acceptance is too strict. Two test-only patterns create the mismatch:

1. Legacy and source-batch fixtures rewrite a local `oneOf` branch (branch 0 or
   branch 1) and its hash after fixture construction. The rewrite makes a
   fixture-local schema appear authoritative while bypassing the existing,
   tightly path-bound in-memory seam that production tests use to represent
   external legacy evidence and source batches.
2. The R2-era query-plan oracle still searches for a stale substring. R4 was
   added after that oracle and, like R2 and R3, is a distinct successor
   authority with its own run ID, contract ID, paths, and hashes; a substring
   assertion cannot prove the selected successor and can accidentally accept
   an unrelated generation.

## Scope

Only the five schema-cluster tests and the test helpers they directly exercise
may change. The work is limited to:

- generalizing and reusing the existing tightly path-bound in-memory fixture
  seam for legacy and source-batch contexts;
- representing an absent external context without the fixture-only
  `remaining_authority` branch;
- replacing the stale R2 substring oracle with an explicit successor-ID oracle;
  and
- adding a selected-branch rewrite-rejection regression.

The five Chromium failures remain a separate, explicitly excluded work item.
No Chromium executable, bundle, environment, snapshot, or browser test is
changed by this alignment.

## Normative authority model

The production trusted acquisition schema and contract are the authority. The
production verifier's fail-closed behavior is the acceptance rule. Tests must
construct inputs that exercise those rules and must reject inputs that would
not be accepted by production. A fixture, local schema copy, branch rewrite,
or oracle string is never an authority substitute.

The alignment therefore preserves these boundaries:

- closed schemas, exact branch ordering, fixed paths, hashes, and successor
  identities remain production-owned values;
- external legacy/source evidence is represented only by the existing
  path-bound in-memory capability seam, with the same validation and rebind
  callbacks used by the production-facing tests;
- absent context is an absent capability and must fail closed at the same
  boundary as production, rather than being converted into a synthetic
  `remaining_authority` result; and
- no fixture may copy, synthesize, or commit production custody/evidence
  artifacts to make a gate pass.

## Approved design

### 1. Reuse the path-bound in-memory seam

Extend the existing test-only `make_repo`/bound-capability helpers in
`tests/test_execution_registry.py` and the corresponding source-finalization
fixture helpers in `tests/test_v52_source_finalize.py`. The seam remains
strictly bound to the expected repository-relative paths and descriptor-like
identity checks; only its in-memory records are generalized so one fixture can
describe:

- a legacy input context;
- a source-batch context; or
- an absent context.

Legacy and source-batch tests must consume the seam's in-memory capability and
its `read_bytes()`/`rebind()` behavior. They must not mutate a copied schema
after committing it, install a new loader, or bypass the production validator.
The existing production schema and contract files remain untouched.

### 2. Remove the `remaining_authority` fixture dependency

Delete the fixture branch whose only purpose is to rewrite branch 0 or branch 1
of `v5_2_acquisition_contract.schema.json` when `remaining_authority=True`.
Callers that need no external context construct the same seam with no bound
capability. The assertion is then the production result: loading, validating,
or advancing with absent context fails closed with the existing authority/input
error. A test may still provide an explicitly bound capability for the positive
legacy/source-batch case, but it must not infer authority from a boolean or a
local schema hash.

### 3. Make successor IDs explicit in the R2 oracle

Replace substring matching with an exact table keyed by successor identity. The
accepted run IDs are exactly:

```text
historical_walkforward_500_recovery_schedule_conflicts_r2
historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3
historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4
```

For each ID, the oracle must assert the corresponding exact RunSpec path,
acquisition-contract path, authority pair, and successor-specific batch-root
selection. The R2/R3/R4 hashes are compared as exact values obtained from the
bound fixture; no `in`, prefix, suffix, or substring test is sufficient. A
forged or mixed-generation ID/path/hash tuple is rejected before any batch or
source result is accepted.

### 4. Add selected-branch rewrite rejection

Keep the schema's closed `oneOf` branch set and add a regression that mutates
the **selected** branch's authoritative `const` or required/hash value in the
fixture-local copy. The frozen-schema verifier must reject that mutation. The
test must also demonstrate that the untouched canonical fixture remains
accepted and that changing an unselected, unrelated legacy suffix cannot make
the selected branch pass. This is a regression against branch-index drift, not
permission to accept a rewritten branch.

## Invariants and forbidden behavior

The implementation and tests must preserve all of the following:

- no production schema, acquisition contract, or fail-closed verifier
  relaxation;
- no new trusted path, loader, selector, or evidence-authority bypass;
- no production evidence synthesis, copying, deletion, or commit;
- no use of a fixture-local branch rewrite as an authority proof;
- no Chromium changes, even if the full gate still reports its five excluded
  defects;
- no reads, writes, deletes, or metadata operations against the forbidden
  custody/authority locations or the protected temporary manifest named in the
  task handoff; and
- all fixture filesystem work remains disposable, read-only with respect to
  repository authority, and confined to the test's temporary directory.

## Success criteria

The design is implemented successfully when all of these are true:

1. The five identified schema-cluster tests pass with zero failures and zero
   errors, including the selected-branch rewrite regression.
2. Legacy and source-batch positive tests use the generalized in-memory seam;
   absent-context tests fail closed without `remaining_authority`.
3. The R2 oracle names and checks the exact R2, R3, and R4 successor IDs and
   their exact authority pairs; no stale substring oracle remains.
4. The production schema, contract, verifier, evidence, and Chromium inputs
   are byte-for-byte unchanged by this work.
5. The exact gate can be rerun from an authenticated environment and reports
   the schema cluster cleared; the five Chromium defects remain explicitly
   attributable to the separately excluded cluster rather than being hidden
   or reclassified.

## Focused verification commands

Run these four short commands from the repository root. The first two target
the schema cluster only; they do not select Chromium tests.

```bash
python3 -m pytest -q tests/test_execution_registry.py -k 'schema or fixture or branch'
python3 -m pytest -q tests/test_v52_query_plan.py tests/test_v52_source_finalize.py -k 'schema or authority or successor or legacy or source_batch or absent'
ruff check tests/test_execution_registry.py tests/test_v52_query_plan.py tests/test_v52_source_finalize.py
git diff --check && git diff --stat && git status --short
```

The exact 1,480-test gate remains an externally authenticated gate because its
inputs include protected evidence that this design neither opens nor creates.

## Alternatives considered and rejected

| Alternative | Rejection reason |
| --- | --- |
| Relax the production schema, contract, or verifier to accept the fixtures | Would turn a test mismatch into an authority downgrade and violate fail-closed acceptance. |
| Keep rewriting branch 0/1 and update expected hashes | Preserves a local untrusted schema as a fake authority and hides branch-index drift. |
| Add a new trusted loader or path for fixtures | Expands the trust boundary and bypasses the existing path-bound capability seam. |
| Keep `remaining_authority=True` for absent context | Conflates absence with authority and permits a fixture-only success path that production rejects. |
| Retain substring matching for R2 | Cannot distinguish R2 from R3/R4 successors and can accept a mixed-generation tuple. |
| Fix the five Chromium failures in this change | Expands scope beyond the approved schema cluster and makes the gate result harder to attribute. |
| Synthesize or copy custody/evidence artifacts locally | Violates evidence integrity and the read-only recovery boundary. |

This design is therefore a narrow test-contract correction: make fixtures
truthful to the existing production authority model, make successor selection
exact, and preserve every fail-closed boundary.
