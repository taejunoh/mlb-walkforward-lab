# Task 13 Self-Contained Chromium Runtime Design

**Status:** Approved design (2026-08-13).  This document specifies the
Chromium admission and snapshot contract needed to remove the five known Task
13 browser failures.  It is a design boundary; it does not provision a
runtime, change evidence, or authorize access to a protected custody tree.

## Problem and evidence

The current exact runner accepts an executable and hashes that executable plus
the executable's immediate parent directory.  It then scans and copies that
parent directory into a runner-owned snapshot.  This is a sound model for a
self-contained headless-shell directory, but it is not a model for a macOS
application bundle.

All five current Chromium failures have the same parent-only snapshot cause.
The affected tests and observed categories are:

| Test | Current category | Why it is misleading or failing |
| --- | --- | --- |
| `tests.test_verify_v52_design.DesignVerificationTest.test_exit_integrity_recheck_uses_mock_without_touching_sealed_files` | FAIL | The portable browser invocation fails first, so the intended exit-integrity assertion is never reached. |
| `tests.test_verify_v52_design.DesignVerificationTest.test_final_boundary_rechecks_scope_and_forbidden_namespaces` | FAIL | The same browser failure masks the final scope/forbidden-namespace recheck. |
| `tests.test_verify_v52_design.DesignVerificationTest.test_current_deliverables_cross_validate` | ERROR | Browser exits before verification completes (`browser_failed`, exit code `none`). |
| `tests.test_verify_v52_design.DesignVerificationTest.test_main_and_module_cli_success_output_and_failure` | ERROR | Browser exits before verification completes (`browser_failed`, exit code `none`). |
| `tests.test_verify_v52_design.DesignVerificationTest.test_verifier_uses_no_network_or_python_write_api` | ERROR | Browser exits before verification completes (`browser_failed`, exit code `none`). |

The two failures are currently masked because
`verify_design_deliverables()` invokes the portable HTML verifier before the
exit-integrity and final scope checks (the call is around
`mlb_backtest/verify_v52_design.py` line 1689, before the checks around 1694
and 1719).  The three errors report the same child-process condition:
`browser_failed: Chromium exited before verification completed (code none)`.
The retained exact-run evidence recorded 1,480 selected tests, four failures,
six errors, and two skips; the five Chromium tests are the browser-related
portion of that result and must all become green.

The root cause is structural.  For a path such as
`Google Chrome.app/Contents/MacOS/Google Chrome`, the current `source.parent`
is only `Contents/MacOS`.  The snapshot therefore omits the application
bundle's `Contents/Frameworks`, `Contents/Resources`, helper executables,
embedded libraries, and other runtime members.  The copied executable starts
without its closure and exits before the HTML verifier can run.

## Decision

Retain the current CLI triple and the current self-contained parent-directory
snapshot model:

```text
--chromium-executable PATH
--chromium-sha256 EXECUTABLE_SHA256
--chromium-bundle-sha256 BUNDLE_SHA256
```

The runner accepts only a self-contained runtime directory in which the
executable and the complete parent-directory manifest are authenticated by
the caller.  A macOS `.app/Contents/MacOS/*` launcher is rejected at
admission, before any browser test runs, with this stable fail-closed error:

```text
chromium .app bundle launchers are unsupported; provide a self-contained runtime directory
```

The error text is deliberately path-independent so callers and tests can rely
on it without exposing an untrusted path.  The rejection applies whenever an
absolute normalized executable path contains a component ending in `.app`
(case-insensitive), including nested application bundles.  It is checked
before executable metadata or content hashing; an invalid `.app` path cannot
fall through to a generic browser failure.

The accepted source has these properties under the existing scanner:

- every ancestor from the filesystem anchor through the executable's parent
  is opened without following symlinks;
- the executable is an executable regular file with exactly one link;
- the immediate parent is a complete closed runtime directory, and every
  recursively scanned member is a directory or a regular single-link file;
- no symlinks, hard-linked files, special files, unsafe names, or path escapes
  are present;
- the executable digest matches `--chromium-sha256`; and
- the canonical sorted manifest of every parent-directory member (relative
  path, type, mode, size, and file digest) hashes to
  `--chromium-bundle-sha256`.

After admission, the existing runner-owned snapshot copy remains mandatory.
The source is scanned and copied through no-follow descriptors, the source is
rescanned to detect mutation, and the snapshot is validated before the child
environment is built and again after the child exits.  The child receives the
validated snapshot path, never the caller's path.

## Authority and security model

The three CLI values are caller-supplied authentication claims, not authority
by themselves.  Authentication is the conjunction of the held no-follow
filesystem observations, the executable byte digest, and the complete parent
manifest digest.  A matching filename, a documented hash, a macOS quarantine
state, a user immutable bit, or an environment variable is not an authority
source.

The current descriptor and fingerprint protections remain in force:

1. Normalize and validate the absolute source path without resolving through
   symlinks.
2. Capture executable metadata, hash through an `O_NOFOLLOW` descriptor, and
   compare pre-/post-hash fingerprints.
3. Open the parent directory without following links and recursively scan its
   entire membership using descriptor-relative operations.
4. Require the expected manifest digest and require the manifest's executable
   row to equal the independently hashed executable and executable mode.
5. Create the destination exclusively beneath the runner-owned temporary
   root, copy every authenticated member, flush files and directories, and
   validate the resulting snapshot's membership, metadata, manifest, and
   executable digest.

The child environment continues to remove caller values for
`CHROMIUM_EXECUTABLE_PATH`, `PLAYWRIGHT_EXECUTABLE_PATH`, and
`PLAYWRIGHT_BROWSERS_PATH` before setting the snapshot path.  Pre/post source
fingerprints, source-swap detection, snapshot tamper detection, cleanup by
the existing temporary-directory owner, the network boundary, and the
write-scope boundary are unchanged.  No external runtime lookup or fallback
is permitted after admission or after a browser launch failure.

## CLI and error contract

The parser continues to require all three Chromium flags.  Pairing and
lowercase-digest validation retain their existing errors.  Admission adds the
stable `.app` rejection above and otherwise retains the current fail-closed
`ValueError` classes for unavailable, symlinked, multiply linked, non-regular,
non-executable, changed, or digest-mismatched inputs.

The `.app` check is an admission property, not a browser capability probe.  It
must occur before cloning the design checkout, copying evidence, or launching
the child test process.  Consequently an approved Chrome `.app` triple becomes
an explicit unsupported-input error rather than a mechanically successful
preflight followed by `browser_failed`.  A compatible self-contained input
remains source-compatible with the existing CLI and snapshot protocol.

If a self-contained runtime passes admission but the browser itself later
exits, the existing `browser_failed` report remains the accurate runtime
failure.  The change must not reinterpret a browser failure as structural
success, run a fallback browser, or hide an evidence/scope failure.

## Known documented candidate versus authenticated runtime

The committed repository plan
`docs/superpowers/plans/2026-08-09-r4-privileged-runtime-materialization.md`
currently records a candidate Puppeteer headless-shell input:

```text
path: /Users/taejunoh/.cache/puppeteer/chrome-headless-shell/mac_arm-146.0.7680.153/chrome-headless-shell-mac-arm64/chrome-headless-shell
executable sha256: 454577a889a2c0b698c09f0ca8db047ea2e6cd604a42c36dc09b375c81362518
bundle sha256:     2ea5035c2085aaf0fb70be6d2949f51eeb42affaac47772044ddaa727cb4c253
```

These values are cited only from committed repository plans/configuration.
They identify a documented candidate; they do not prove that the path exists,
that its current bytes match, or that the runtime is authenticated.  The
runner must never discover or select a runtime by scanning external cache
directories.  A final exact run may use these values only after a user has
supplied and authenticated the corresponding self-contained directory and
the runner has recomputed both digests from held descriptors.

## Alternatives considered and rejected

| Alternative | Rejection |
| --- | --- |
| Support a whole macOS `.app` bundle and infer its launch root | Requires a new recursive bundle-root contract, executable-relative semantics, and a larger dependency surface.  Existing CLI hashes cover only the executable and immediate parent, so accepting `.app` would falsely authenticate omitted framework/resource files. |
| Copy a dependency closure discovered from Mach-O metadata or launch errors | Discovery is platform- and loader-dependent, can omit data files/helpers, and would turn an unauthenticated runtime lookup into authority.  It also changes the current complete-manifest boundary. |
| Relax executable or bundle hashes to accept partial parents | Allows substitution or omission of runtime members and destroys the fail-closed snapshot guarantee. |
| Look up an installed browser or fall back to another executable | Makes results depend on host state, violates reproducibility and the explicit CLI contract, and can escape the authenticated write/network boundary. |
| Keep accepting `.app` and merely improve the browser error | Leaves the parent-only root cause intact and preserves misleading masked failures. |

## Implementation boundaries

The implementation change is limited to the existing Chromium admission and
its tests:

- `scripts/run_task13_exact_suite.py`: add the structural `.app` admission
  guard before `_chromium_metadata()` and retain the existing
  `validate_chromium_runtime()`, `chromium_bundle_sha256()`,
  `provision_chromium_runtime()`, `validate_chromium_snapshot()`, and
  `build_child_environment()` boundaries.  Do not change the CLI names,
  manifest format, snapshot ownership, environment override removal, cleanup,
  or network/write policy.
- `tests/test_task13_exact_suite_runner.py`: add red coverage for `.app`
  rejection, then green coverage for a self-contained runtime, source-ancestor
  swap, executable/companion tamper, environment override removal, snapshot
  cleanup, and portable-verifier integration.  Existing descriptor and
  fingerprint regressions remain required.
- `tests/test_verify_v52_design.py`: add the portable-verifier integration
  assertion that an unsupported `.app` fails structurally before a browser
  child can report `browser_failed`; preserve the existing masked-error and
  no-network/no-write assertions for self-contained input.

No production model, report, schema, evidence, custody, authority, or
external-runtime lookup file is in scope.  No dependency-closure copier,
whole-`.app` parser, hash-relaxation path, or fallback selector may be added.

## TDD and rollout

1. **Red structural preflight.**  Build a temporary `.app/Contents/MacOS`
   fixture whose launcher and immediate parent would satisfy the old mechanical
   checks.  Assert that admission now raises the exact stable unsupported
   error and that no child test is started.
2. **Green self-contained path.**  Use the existing safe fixture format with
   an executable and complete parent directory.  Compute both digests through
   repository helpers, provision the runner-owned snapshot, and assert the
   child environment points only to that snapshot.
3. **Regression coverage.**  Keep or extend source-ancestor swap,
   executable/companion tamper, pre/post fingerprint, hostile environment,
   snapshot cleanup, and network/write-boundary tests.  Any mutation must fail
   closed and must not leave a usable partial runtime.
4. **Portable verifier integration.**  Verify that structural `.app`
   rejection occurs before `verify_design_deliverables()` can mask its later
   exit-integrity or scope checks.  Verify that a self-contained runtime still
   reaches the browser verifier and preserves the existing `browser_failed`
   semantics for genuine child exits.
5. **Focused verification.**  Run the Chromium runner tests, portable verifier
   tests, Ruff, and `git diff --check`.  Confirm that only the intended source
   and test files changed.
6. **Authenticated exact run.**  Invoke `scripts/run_task13_exact_suite.py`
   only after the user-authenticated runtime directory, executable digest,
   complete-parent manifest digest, and all existing authenticated evidence
   roots have been independently validated.  Do not scan, copy, synthesize, or
   substitute external evidence or runtime artifacts.
7. **Gate confirmation.**  Require all five prior Chromium tests to pass and
   the exact selected gate to report zero failures and zero errors, retaining
   the two expected skips.  The expected selected count is 1,480; if runner
   selection changes, report the actual count and explain the selection change.
   Recheck fixed-Git inputs, source/preflight hashes, parent-tree fingerprints,
   cleanup, and worktree scope before declaring success.

## Success criteria

- Every previously listed Chromium test is green, with no `browser_failed`
  masking of its intended assertion.
- Self-contained runtime admission, complete-parent manifest authentication,
  source/snapshot fingerprints, child environment isolation, cleanup, and
  network/write boundaries remain fail-closed.
- An approved Chrome `.app` triple produces the exact unsupported-admission
  error before any browser test or evidence mutation.
- The exact runner passes all selected tests (1,480 under the current
  selection, or an accurately reported final count), with zero failures/errors
  and only its expected skips.
- Ruff and `git diff --check` pass; the diff is limited to the documented
  implementation/test boundaries; no forbidden custody/authority path or
  protected evidence is accessed or modified.

## Non-goals

This design does not add whole-`.app` support, dependency-closure copying,
hash relaxation, external runtime lookup, browser fallback, new CLI flags,
new evidence, custody recovery, authority changes, or protected-runtime
materialization.  It does not identify an authenticated runtime by scanning
the host filesystem and does not treat documented candidate values as proof of
current bytes.
