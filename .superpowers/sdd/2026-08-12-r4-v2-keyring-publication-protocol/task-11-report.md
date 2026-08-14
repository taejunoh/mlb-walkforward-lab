# Task 11 — V2 complete authority verification

## Result

Implemented the read-only `Verify.active()` authority verifier and the DEBUG-only
descriptor capability test entrypoint.  The verifier uses descriptor-relative,
`O_NOFOLLOW` traversal; never obtains a Key-B private key; validates the fixed
bootstrap/keyring graph, the sealed eight-child authority graph, receipt/signature,
admission locator/external copy, journal prefix, closure, and all persisted
cross-document bindings.

## TDD evidence

- **RED:** `swift test --filter MaterializeTests/testReceiptPairBindsActiveKeyAndIsImmutable`
  failed after the new assertion: receipt stored the admission document's embedded
  self-digest rather than SHA-256 of the complete canonical admission bytes.
- **GREEN:** corrected `ReceiptFactory` and `V2Evidence` to bind the complete admission
  record bytes, which is the same digest already used by the locator and actual journal.
  This was essential: the real Task 9/10 lifecycle previously produced receipt `a4ed…`
  versus locator/journal `6a5a…`; no journal was synthesized or reconstructed.

## Verification

- `swift test --filter VerifyTests` — 4 passed.
- Full fresh-fixture mutation table — accepted valid actual Task 9/10 publication, then
  rejected extra authority child, receipt byte tamper, external admission copy mismatch,
  missing PUBLISHED journal, terminal revocation, and canonical locator path escape.
  Each rejection asserted unchanged recursive inode/mtime snapshot and external sentinel bytes.
- `swift test --filter 'MaterializeTests|BootstrapTests|KeyringTests|StateMachineTests'` —
  87 passed.
- `swift test` — 248 passed, 0 failures (73.414 s).
- `swift build -c release` — passed.
- `nm -gU .build/release/R4ProtectedExecutionProvisioner | rg 'activeForTest|loadActiveForTest|VerificationAuthority'`
  — no release symbols.
- `git diff --check` — clean.

## Notes / residual concern

DEBUG fixtures cannot set the kernel immutable flag unprivileged, so the test verifier
still checks mode, link count, ACL/xattr absence, canonical bytes, and identities while
production performs the full sealed-metadata check.  Fixture cleanup now descriptor-walks
only the inode-owned disposable tree, restores owner-write modes solely for that owned tree,
and then removes it.

## Review fix round 1

- **RED:** a signed, resealed receipt whose `environment_sha256` did not equal the
  persisted runtime manifest was accepted by the previously weaker authority cross-check.
  A journal attempt changed from `0700` to `0755` was likewise accepted by the DEBUG
  verifier because it skipped mutable-registry metadata.
- **GREEN:** the persisted authority validator now binds checkout/runtime rows and all
  receipt runtime, dependency, Mach-O, sandbox, interpreter, environment, approval and
  admission fields.  DEBUG retains the production-independent UID/GID relaxation only;
  it now checks registry type, `0700`, ACL/xattr absence and nonimmutable status.
- The verifier retains every direct root child, rebinds all five from the held root at
  exit, rebinds all control registries, revalidates the authority through a freshly
  rebound authorities descriptor, takes second snapshots, and rechecks the published
  journal.  Deterministic DEBUG hooks prove rejection after a late authority child,
  authorities-registry replacement, and same-name journal-attempt replacement.
- **GREEN verification:** `VerifyTests` 5/5; Models/Keyring/Bootstrap/StateMachine 91/91;
  full `swift test` 249/249 (51.709 s); release build and DEBUG-symbol `nm` scan clean.

## Review fix round 2

- Retains the original public `VerifiedKeyring` descriptors and exact control/keyring/
  revocation snapshots to the final verification point.  The final pass rebinds those held
  descriptors rather than fresh-opening names, rereads and signature-validates the Key-B
  chain against the original evidence, and rebinds/reads the held Key-A bootstrap public file.
- DEBUG post-KeyringVerifier hooks replace `keyring` and `revocations` with foreign same-name
  registries while moving the originals into the isolated sentinel fixture.  Consequently the
  control name set is unchanged and rejection exercises retained identity rebind, not merely
  an extra-name check.
- Verification: Verify+Keyring 14/14; full `swift test` 249/249 (52.356 s); release build,
  DEBUG symbol scan, diff check, and derived-data reset clean.
