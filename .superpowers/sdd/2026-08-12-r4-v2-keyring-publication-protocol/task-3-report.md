# Task 3 report

## Scope

Added descriptor-held sealed metadata verification and immutable publication
operation seams only. No production path overloads, execution-audit methods,
network/process capability, or cleanup/adoption behavior was added.

## TDD evidence

- RED: the descriptor test initially failed to compile because the sealed
  verifier and disposable metadata fixture did not exist.
- GREEN: the real Darwin fixture tests now exercise no-follow replacement,
  hard-link nlink changes, extended ACL creation, provider xattr insertion,
  and foreign-sentinel preservation. A pure metadata classifier test accepts
  a synthetic root:wheel immutable baseline and rejects exactly one mutation
  of type, owner, mode, nlink, ACL, immutable flags, or xattrs.
- Publication seam tests record the exact order:
  `write -> durable-file -> immutable -> durable-file -> rebind -> read ->
  verify`, followed by three parent-only durability calls. Volume barriers
  remain a separate explicit operation.
- The production immutable path performs the second durable regular-file
  barrier after setting `SF_IMMUTABLE`, before rebind and byte/metadata
  readback. A bounded retry helper covers fchflags/fstat readback and volume
  barriers on EINTR.
- Separate disposable integration coverage observes an actual hard-link count
  change, ACL entry, and forbidden provider xattr through Darwin descriptor
  calls. It does not claim unprivileged immutable success.
- EINTR tests cover interrupted-then-success, bounded persistent EINTR, and
  immediate hard-error failure.

## Platform limitation

The disposable test process is unprivileged (`geteuid() != 0`), so it does not
claim a successful root:wheel/SF_IMMUTABLE production mutation. Synthetic
metadata tests cover the accepted baseline and every named defect; the
DEBUG-only publication operation seam verifies production ordering without
forging privileged success. Production closures remain concrete and always
require uid 0, gid 0, and immutable-flag readback.

## Verification

Focused `FakesTests|StorageTests|BootstrapTests` (67 tests) and the release
build pass.
The generated package `.build` directory and scratch artifacts were removed
before handoff.

## Round 2 review closure

- RED: direct `fstat`/read-loop paths remained outside the bounded retry seam,
  and the durability test reused one directory descriptor for all three
  barriers. GREEN routes descriptor metadata and read-loop EINTR handling
  through bounded retry logic and records distinct first/second/root
  device-inode identities in order.
- The old unprivileged named-defect smoke test was removed; the remaining
  unprivileged xattr smoke test is explicitly named as fail-closed only. The
  synthetic snapshot test is the sole individual-defect proof.
- RED: the parent durability seam reused one descriptor and did not prove
  identity ordering. GREEN uses three distinct held directories and records
  each device/inode in first/second/root order. Held-descriptor construction,
  metadata snapshots, sealed verification, and read loops now all retry EINTR
  with bounded fail-closed behavior.
- RED/GREEN: parent durability failure propagation is covered; a second-parent
  error stops before any root durability attempt.
