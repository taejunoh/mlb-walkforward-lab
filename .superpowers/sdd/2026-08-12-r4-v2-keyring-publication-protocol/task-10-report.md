# Task 10 report — sealed authority publication transaction

## RED

Added `testPublicationEntryPointRequiresOpaquePreparedCapability` before the
publication API existed. The focused Swift test failed to compile because
`Publication.publishForTest`, its fixed attestation helper, and its DEBUG
operation table were absent. This established the intended opaque capability
boundary before production code was added.

## GREEN

- `PreparedPublication` now retains the held stage/work/attempt capability,
  admission evidence, signed receipt bytes, and public signer binding; it has
  no public initializer or raw-path input.
- `Publication` appends `RENAME_INTENT`, performs exactly one descriptor
  relative exclusive rename, and treats every successful rename as the
  irreversible post-rename boundary.
- Pre-rename errors terminalize only through the retained-stage quarantine
  move. Post-rename errors terminalize only as `PUBLISH_INDETERMINATE`; a
  failed terminal append is an explicit error and leaves residue unchanged.
- Final publication rebinds the same inode under the fixed authority ID,
  seals the root, rebinds it again, verifies every descendant's sealed
  metadata plus the Task 8/9 graph and receipt signature, applies parent and
  volume durability, then appends `PUBLISHED`.
- DEBUG fixtures use a disposable `/private/tmp` root and typed descriptor
  operation table only. They record no launch or network capability.

## Verification

- Focused publication fault tests: pass.
- Required focused suite (`MaterializeTests|StateMachineTests|StorageTests|BootstrapTests`):
  83 tests pass, exit code 0.
- Full package suite: 245 tests pass, 0 failures (captured completion output).
- `swift build -c release --package-path tools/r4_protected_execution_authority`: pass.
- `git diff --check`: pass.

## Fix round 1

- RED: injected rename `EEXIST` and a real rename followed by `EIO` both
  incorrectly quarantined under the first implementation.
- GREEN: the boundary now begins before invoking the one rename syscall. Any
  syscall error is `PUBLISH_INDETERMINATE`; the ambiguous fixture retains the
  stage inode at the fixed destination and performs no quarantine move.
- Terminal journal failure is a `PublicationTerminalizationFailure` containing
  bounded primary and terminalization descriptions. A simulated PUBLISHED
  record that persisted before durability failure prevents a second terminal
  append and surfaces both causes.
- The DEBUG final verifier is no longer a no-op: it validates retained identity
  and actual fixture metadata recursively, checks a synthetic immutable map
  keyed by each descriptor's device/inode (including the root marked at
  publication), then rebuilds the graph and checks receipt bytes and signature.
  Fixture immutable state remains synthetic only because unprivileged
  `/private/tmp` cannot set `SF_IMMUTABLE`.
- A deterministic replacement table swaps the fixed final name immediately
  before or after the final verifier. Both cases reject the retained stage
  inode as indeterminate and make no recovery mutation.

## Commit

`fix: classify rename invocation as indeterminate` (SHA recorded after commit).

## Concerns

Task 10 validates the same final inode/tree and receipt binding after rename;
the fixed-location read-only verifier remains Task 11's responsibility.
