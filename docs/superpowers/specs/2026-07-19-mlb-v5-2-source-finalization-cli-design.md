# MLB v5.2-QD Historical Source Finalization CLI

**Status:** Approved design for a post-acquisition workflow; implementation
precedes real acquisition

**Purpose:** Define the local, post-acquisition workflow that authenticates the
completed historical evidence graph, publishes the canonical source inventory,
records independent review and replay evidence, and advances the execution
registry only when every irreversible gate is explicit and satisfied.

## 1. Scope and invariants

This design starts after the hybrid acquisition worker has produced successful
query objects, semantic sidecars, split receipts, and a terminal acquisition
checkpoint. It does not acquire data and it never makes a network request.

The finalization service must:

1. Reload every authority input from disk and rebind it to the current commit,
   RunSpec, acquisition contract, and protected inventory.
2. Treat the execution registry and its acquisition checkpoint chain as the
   phase authority. File presence, directory names, and caller assertions never
   imply progress.
3. Publish only canonical, immutable, path-bound artifacts. A retry with the
   exact same canonical bytes is idempotent; a different payload at an existing
   path is a hard conflict.
4. Keep review and replay receipts append-only and explicitly attributable.
5. Require explicit receipt paths when advancing the phase. The service must
   not select a receipt by glob, timestamp, or “latest” convention.

The scope ends at a verified `SOURCES_READY` transition. OOF ledgers, model
cards, target locks, outcomes, reports, and prospective 2026 artifacts remain
separate workflows.

## 2. Components and file boundaries

The implementation has one staged command-line entry point and one shared
service module. The CLI performs argument parsing and rendering only; all
authority loading, validation, publication, and phase-gate decisions live in
the service.

### 2.1 CLI module

`mlb_backtest/v52_source_finalize.py` exposes these subcommands:

`status`, `preflight`, `publish`, `review`, `replay`, and `advance`.

The module is invokable with `python -m mlb_backtest.v52_source_finalize`.
It must not import a network client or provide a path that bypasses the shared
service.

### 2.2 Shared service module

`mlb_backtest/v52_source_orchestration.py` owns the following interfaces:

- `build_context(common_args) -> FinalizationContext`
- `read_status(context) -> StatusReport`
- `run_preflight(context) -> PreflightReport`
- `publish_inventory(context) -> PublishReport`
- `write_review(context, request) -> ReviewReport`
- `write_replay(context, request) -> ReplayReport`
- `advance_sources_ready(context, request) -> AdvanceReport`

`FinalizationContext` is an immutable, path-bound bundle containing the parsed
RunSpec, acquisition contract, protected inventory binding, execution-registry
state, terminal checkpoint (when available), verified query graph, and the
current source-evidence roots. Every public service function receives this
context or constructs it through `build_context`; callers cannot inject a
previous phase, digest, timestamp, or inventory.

### 2.3 Test boundary

`tests/test_v52_source_finalize.py` covers the service and CLI contracts.
Fixtures may construct miniature local evidence graphs for tests, but no test
fixture may be installed as production historical evidence or used to advance
the repository registry.

The two previously implicit evidence payloads become explicit schemas:

- `schemas/v5_2_source_review_receipt.schema.json`
- `schemas/v5_2_clean_source_replay_receipt.schema.json`

### 2.4 Execution-code binding

The finalization CLI and service become execution-phase authorities. Therefore
`mlb_backtest.v52_source_finalize` and
`mlb_backtest.v52_source_orchestration` are additional roots of the computed
repository-local import closure used by the execution registry. The bound set
must contain their complete transitive production closure, including at least
`v52_snapshot_store.py`, `v52_verify_sources.py`, `execution_registry.py`, and
every receipt or inventory schema they load. A local dependency reached only
through a dynamic import must be declared as an explicit closure extra.

The implementation updates `_BOUND_EXECUTION_CODE` to exactly the union of the
acquisition roots, the two finalization roots, approved explicit extras, and
their bound schemas. The registry tests recompute that closure from the real
repository and require exact equality. Separate regression tests dirty or
untrack each new authority module and representative newly reachable
dependencies and prove that publication and phase advancement fail closed.

## 3. Common command arguments

Every subcommand accepts:

- `--root PATH` (default `.`), resolved once and rejected if any ancestor is a
  symlink or non-directory;
- `--run-spec PATH`, defaulting to
  `protocols/v5_2_qd/historical_walkforward_500.json`;
- `--acquisition-contract PATH`, defaulting to
  `protocols/v5_2_qd/historical_source_acquisition.json`;
- `--protected-inventory PATH`, defaulting to
  `audit/v5_1_bt/protected_raw_inventory.json`;
- `--json`, requesting one canonical JSON result on stdout;
- `--quiet`, suppressing human progress text while retaining errors on stderr.

Paths are interpreted relative to `--root` unless absolute. The service
canonicalizes and rechecks every path before opening it. Common arguments are
not authority: a supplied path must exactly match the frozen contract where a
contract path is prescribed.

Command-specific arguments are described below. Unknown options, duplicate
options with conflicting values, and noncanonical query IDs are usage errors.

## 4. Authority data flow

The read path is deliberately one-way:

```text
RunSpec + acquisition contract + approved design/plan
                 |
                 v
protected v5.1 inventory + current code commit
                 |
                 v
execution registry -> terminal acquisition checkpoint
                 |
                 v
query batches -> attempts/raw objects -> semantic sidecars
                 |
                 v
schedule/game inventory + reviewed timezone authority
                 |
                 v
canonical source inventory -> review/replay receipts -> SOURCES_READY
```

The service recomputes hashes and semantic projections from the underlying
objects. It rejects a caller-provided digest, count, phase, timestamp, network
counter, or “complete” assertion that differs from disk evidence. Every
read-only verification performs a final rebind before returning so a path,
inode, or canonical byte change during the read fails closed.

## 5. Phase, checkpoint, and input-factory requirements

`build_context` is the sole input factory. It must:

1. Load and validate the canonical RunSpec and acquisition contract.
2. Verify their approved-design and implementation-plan bindings against the
   current repository commit.
3. Authenticate the protected v5.1 inventory and its raw-store seals.
4. Load the execution registry and require phase `ACQUIRING` for publication,
   review, and replay. `status` and `preflight` may report an absent registry
   without creating one.
5. Load the latest acquisition checkpoint through the registry API. The
   checkpoint must be terminal `REMAINING`, have no pending or quarantined
   query IDs, and bind the complete query-batch graph.
6. Verify schedule anchors, source query batches, split ancestry, attempts,
   semantic sidecars, and source-specific coverage without network access.
7. Require the reviewed timezone mapping and all required reference evidence
   before declaring a source inventory publishable.

The factory returns a failure report rather than creating missing directories.
Only the matching mutating subcommand may create its own finalization artifact,
and only after the factory has completed its read-only checks.

## 6. Subcommand contracts

### 6.1 `status`

`status` is read-only and never creates a registry, checkpoint, inventory, or
receipt. It reports:

- current execution phase and phase-receipt path, or `ABSENT`;
- latest checkpoint sequence, stage, head, pending count, and quarantine count,
  or `ABSENT`;
- existence and authentication status of query batches, schedule inventory,
  timezone reviews/seal, source inventory, replay receipts, and source reviews;
- derived booleans `publish_ready`, `review_ready`, `replay_ready`, and
  `advance_ready`;
- blocking errors with canonical relative paths.

The status report is advisory. It never infers a phase from the booleans.
It may enumerate candidate receipts for diagnostics, but it never returns an
implicitly selected receipt set that `advance` can consume.

### 6.2 `preflight`

`preflight` runs the complete local gate in read-only mode. It validates the
authority graph, protected promotion references, query coverage, source
snapshots, reviewed timezone mapping, canonical JSON, inode/link safety, and
network-attempt count. It returns the recomputed inventory digest when all
checks pass, but does not write that inventory or a receipt.

The command is valid at any phase and reports the phase-specific blockers. It
must not clean quarantine, repair a missing tail, or synthesize a fixture.

### 6.3 `publish`

`publish` requires `--lock-owner ID` and accepts no query IDs or phase value.
The lock owner is transient concurrency metadata recorded only in the existing
safe-lock lifecycle; it is not a durable publisher identity and is not used to
prove reviewer independence. The command requires `ACQUIRING`, a terminal
checkpoint, zero critical source errors, and a successful preflight.
It writes the canonical source snapshots, `data/v5_2_qd/historical/sources/
source_inventory.json`, and its matching `.sha256` receipt using exclusive
creation, fsync, no-follow reads, and final postcondition verification.

If all target bytes already match, the command returns the original publication
binding without changing persistence time. Any existing path with different
bytes or digest is a conflict. Publication does not advance the execution
phase.

### 6.4 `review`

`review` writes one independent source-review receipt. It requires:

- `--reviewer-identity independent-source-auditor`, the identity already
  frozen by the execution-registry gate;
- `--reviewer-role independent-source-auditor`, the frozen independent-review
  role;
- `--decision APPROVED|REJECTED`;
- `--query-ids-file PATH`, a canonical JSON document of the exact form
  `{"query_ids":[...]}` containing one or more explicit query IDs in sorted,
  duplicate-free order; and
- `--write-receipt PATH`, an explicit receipt path under the source-review
  namespace.

Query IDs are explicit authority. The service rejects directory scans,
implicit “all” selection, duplicates, unknown IDs, and IDs outside the verified
terminal graph. Both identity and role must equal the frozen independent-review
values. The CLI does not claim to cryptographically prove separation from the
publisher: operator separation remains a manual irreversible-gate policy. A
review receipt binds the reviewer identity and role, decision, query IDs,
query-IDs document hash, inventory digest, code commit, RunSpec/contract hashes,
and canonical occurrence time.
An `APPROVED` receipt is evidence only; it cannot advance the phase by itself.

The implementation introduces a formal, exact-key source-review receipt schema
and updates the registry validator to require `reviewer_role` as well as the
already-frozen identity. Because no production historical review receipts yet
exist, this is a pre-acquisition contract formalization rather than a migration
of stored evidence. Tests prove that a forged identity, wrong role, omitted
role, or unbound query-ID document is rejected by both the CLI and registry.

### 6.5 `replay`

`replay` performs the clean, network-free source verification pass. It requires
the published inventory and a verified source graph. The caller supplies an
explicit `--write-receipt PATH`; no default path or directory discovery is
allowed.

Exactly two explicitly selected unique replay receipts are required for the
`SOURCES_READY` transition. Each invocation generates a CSPRNG-backed UUIDv4
execution ID before the read-only replay begins. The formal replay-receipt
schema requires the canonical lowercase UUID string, inventory bytes, source
snapshot hashes, complete query graph, code commit, and network-attempt count
(which must be zero). Receipt creation is exclusive: any existing target path
is a conflict, even if its bytes match, because a clean replay is a distinct
execution rather than an idempotent publication retry.

The second selected receipt must have a distinct path, execution ID, and
receipt hash while recomputing the same inventory projection byte-for-byte.
The immutable `SOURCES_READY` phase receipt is the canonical selection record:
it binds exactly the two replay receipt paths and hashes supplied to `advance`.
The replay directory is never scanned to choose evidence. Additional unselected
append-only replay attempts may exist, but they cannot alter the selected pair
or satisfy a later transition. Thus “exactly two” is the cardinality of the
authenticated phase-gate selection, not a global directory-count invariant.
The registry's `_source_receipt_payload` and
`_validate_sources_ready_evidence` validators must enforce the exact replay
schema, require exactly two selected replay bindings, and reject equal execution
IDs as well as equal paths or hashes. These are registry invariants, not merely
CLI/service checks, so a direct `advance_execution_phase` call cannot bypass
them. The UUID is a service-generated run nonce; it distinguishes replay
executions but is not asserted to prove an operating-system process identity.

### 6.6 `advance`

`advance` is the only subcommand that may move the execution registry to
`SOURCES_READY`. It requires:

- `--review-receipt PATH` repeated for the required independent review receipt
  set;
- `--replay-receipt PATH` repeated exactly twice, with two distinct paths;
- an explicit `--checkpoint PATH` naming the terminal checkpoint head; and
- an explicit `--inventory PATH` naming the published source inventory.

Every supplied path is canonicalized, authenticated, and compared with the
context. The command rejects omitted paths, directory paths, glob patterns,
duplicate paths, receipts from another inventory or commit, non-approved
decisions, and a checkpoint that is not the current terminal head. It writes
one immutable `SOURCES_READY` phase receipt binding all supplied receipt hashes,
the inventory hash, checkpoint hash, code commit, and prior phase receipt. A
second exact invocation returns the existing phase state; a differing request
fails without mutation.
At least one approved review receipt is required. If multiple review receipts
are supplied, they must bind the same canonical query-ID subset.

## 7. No-clobber, idempotency, and failure behavior

All finalization writes use exclusive creation and atomic publication. Existing
directories and files are never replaced merely to make a command pass. Exact
publication and phase-transition retries may return the already authenticated
result; replay receipt creation is always a new exclusive execution and never
reuses an existing path. A postcondition failure removes only artifacts created
by the current attempt after exact path, inode, and canonical-byte verification;
recovery evidence is preserved if cleanup cannot be authenticated.

The service serializes per-inventory publication and registry transitions with
the existing safe locks. Concurrent identical calls converge on one immutable
result. Concurrent nonidentical calls produce one winner and one explicit
conflict; neither call can acknowledge an unbound or partial artifact.

## 8. JSON and error contract

With `--json`, every command emits exactly one canonical JSON object on stdout
followed by one newline, for both success and handled failure. Stderr remains
empty for handled failures. Success objects contain `schema_version`, `command`,
`status`, `phase`, and a command-specific `result`. Failure objects contain
`schema_version`, `command`, `status: "ERROR"`, `error_code`, `message`, and
`paths`; secrets, response bodies, and noncanonical tracebacks are excluded.

Without `--json`, human success output is a stable summary of the same fields on
stdout and errors are written to stderr. Exit codes are stable:

| Code | Meaning |
|---:|---|
| 0 | command completed successfully |
| 2 | argument or command-shape error |
| 3 | missing, malformed, or unauthenticated authority |
| 4 | phase/checkpoint gate not satisfied |
| 5 | semantic, hash-chain, path, or postcondition failure |
| 6 | no-clobber conflict or non-idempotent retry |
| 7 | forbidden network or external-write attempt detected |

## 9. Current-code gap at Task 13

The existing `v52_acquire`, `v52_verify_sources`, `v52_timezone_review`, and
execution-registry modules provide most underlying readers and writers, but
there is no single post-acquisition finalization service or staged CLI that
coordinates them. Current commands expose acquisition and source verification
operations separately; status/preflight are not one stable interface, review
selection is not uniformly required to name explicit query IDs, replay does not
enforce the two-unique-receipt contract as one gate, and phase advancement can
be invoked without the finalization command’s explicit receipt-path bundle.

The implementation must close this orchestration gap without weakening any
existing path, hash, checkpoint, or registry gate. It must not broaden Task 13
into network acquisition or create historical evidence while solving the CLI
problem.

Task 13 also formalizes the previously implicit source-review and clean-replay
receipt payloads as exact JSON schemas. These schemas, the staged CLI, the
shared service, and their transitive production imports are all part of the
execution-code binding described in section 2.4.

## 10. Test matrix

The test suite must cover:

- parser/help output for every subcommand and common-argument combination;
- status and preflight on absent, partial, valid, stale, and tampered authority;
- canonical RunSpec/contract/commit/inventory binding and phase gating;
- query-batch graph closure, explicit review query IDs, duplicate/unknown IDs,
  and review receipt binding;
- publish clean success, exact retry, differing retry, interrupted write,
  symlink/hardlink/special-file rejection, and concurrent identical/nonidentical
  calls;
- replay network-attempt rejection, exact two selected unique receipt
  enforcement, byte-identical recomputation, duplicate receipt rejection,
  canonical UUIDv4 execution-ID validation, and distinct execution-ID binding;
- advance explicit-path validation, receipt cross-binding, terminal checkpoint
  requirements, idempotent retry, conflict rejection, and registry hash-chain
  authentication;
- direct execution-registry bypass attempts using a missing or malformed replay
  UUID, a wrong/extra replay key, duplicate selected execution IDs, and any
  replay payload that the formal schema rejects;
- one-end-to-end miniature evidence graph using only a temporary repository,
  with an assertion that no fixture artifact is written under the real
  repository's production historical namespace;
- JSON schema/one-newline output, stable error codes, stderr behavior, and
  absence of secrets or tracebacks in machine output.

## 11. Out of scope and irreversible gates

Out of scope are network acquisition, rate limiting, request retries, parser
development, source-data repair, model fitting, OOF generation, card sealing,
target locking, outcome joins, 500-pick sampling, report publication, and all
prospective 2026 snapshot workflows.

Independent source review and the `SOURCES_READY` phase transition are manual
and irreversible repository-authority gates. The CLI must not impersonate a
reviewer, infer approval from a clean replay, auto-select query IDs, or advance
on a directory’s apparent completeness. A rejected review, missing receipt,
registry conflict, or semantic mismatch requires an explicit new operator
action; it is never silently repaired.

## 12. Exact next sequence

After implementation and review, run the following sequence against the real
repository root:

1. `status --json` to record the current phase and blockers.
2. `preflight --json` to authenticate the terminal acquisition graph with zero
   network attempts.
3. `publish --json --lock-owner ID` to write the canonical source inventory and
   immutable source snapshots, only if preflight is clean.
4. Choose the review set, write its canonical explicit query-ID document, and
   invoke `review` with that file, the independent reviewer identity and role,
   and an explicit receipt path.
5. Run `replay` twice from clean processes, using two explicit distinct receipt
   paths and requiring byte-identical inventories with zero network attempts.
6. Run `preflight --json` again and verify the two replay receipts and review
   receipt hashes.
7. Invoke `advance` with the explicit terminal checkpoint, inventory, review,
   and exactly two replay receipt paths.
8. Run `status --json` and a final read-only preflight; confirm phase
   `SOURCES_READY` and zero critical errors. Any downstream fitting gate remains
   outside this workflow and must be evaluated separately.

Until the missing v5.2 acquisition hierarchy exists, this sequence stops at
steps 1–2 with an explicit no-registry/no-checkpoint report.
