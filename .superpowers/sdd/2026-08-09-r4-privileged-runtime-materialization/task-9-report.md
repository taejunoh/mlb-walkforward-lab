# Task 9 implementation report

## Result

Implemented the V2 materialization closure in the production helper.

- `Materializer.run` is now the first admission-creating orchestration path.  It verifies the held bootstrap approval, admits once, retains the Task 6 stage, copies/extracts descriptor-bound inputs, obtains the Task 8 `InstallationObservation`, and builds V2 runtime evidence solely from that observation.
- The runtime, authority manifest, and sealed receipt use canonical bytes and are cross-validated with `V2Evidence` before they are written.
- The generation-1 Key-B descriptor is read only for Ed25519 signing; its bytes are never exposed through a caller API and the temporary copy is overwritten.  Receipt signatures are exactly 64 bytes.
- Every staged output is rebound, ACL-checked, mode-sealed, immutable-read-back, and the root is published only with descriptor-relative `renameatx_np(RENAME_EXCL)`. Failures after admission route to the retained Task 6 quarantine terminalization path.
- `Verify.active()` is read-only. It pins/validates bootstrap Key-A and approval first, validates the contiguous Key-B genesis record and its detached signature, rejects any revocation evidence, verifies the receipt signature, and rejects Key-A/Key-B role confusion.
- The materialize CLI now calls `Materializer.run`; the verify CLI calls `Verify.active`.

## Task 8 deferred duplicate-path close

Task 9 constructs and consumes `ExecutionObservation`, so the deferred exact/casefold duplicate staged-Mach-O-path issue was fixed here.  `ExecutionObservation` rejects duplicates before `Dictionary(uniqueKeysWithValues:)`, avoiding a trap on hostile evidence. Regression coverage is in `MaterializeTests`.

## Verification evidence

- `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|KeyringTests|VerifyTests'` — 4 tests passed.
- `swift test --package-path tools/r4_protected_execution_authority --filter PythonTransportIntegrationTests` — passed after the reviewed-source allowlist was updated for the two new production source files.
- `swift test --package-path tools/r4_protected_execution_authority` — passed.
- `swift build --package-path tools/r4_protected_execution_authority -c release` — passed.
- `git diff --check` — passed.

## Scope notes

No protected production path, Key-A private material, network operation, child-process materialization, or Task 14 ceremony was accessed. Tests remain disposable `/private/tmp` fixtures and verification seams only.

## Review round 1 — blocked architecture finding

The review correctly found that the committed implementation is not safe to approve.  I traced the failure to missing closed protocol/layout inputs rather than attempting to invent a signing protocol:

- Bootstrap's closed tree has only `control/keyring/generation-1`; `Adapters.verifyClosedLayout` rejects any rotation child. `KeyringRecord` rotation documents contain only hashes and a digest of a detached signature, but no specified on-disk record/signature/public-key layout, signature preimage, or verifier. Therefore there is no correct way to discover or validate a contiguous active rotated Key-B key from the existing sealed graph.
- `ReceiptV2.decode` parses `key_generation` and `public_key_sha256` but discards both. Its model cannot bind an exact signer generation/public key. This requires a schema/model amendment coordinated with the canonical V2 vectors and Python transport, not a local interpretation.
- `Verify.active` lacks a descriptor-bound published-tree reader/model capable of reconstructing the full actual authority closure and comparing it to the authority/runtime manifests. Existing `PublishedAuthorityTree` only decodes supplied JSON rows; it neither specifies the on-disk published layout nor an admission-record retained descriptor after publication.
- The existing durable state machine persists `SEALED` before the tree is immutable. A post-seal/rename failure cannot safely create `QUARANTINED`; the state schema needs an explicit post-publication recovery state/location contract.

One independently specified defect was fixed with strict RED→GREEN: V2 cross-validation now requires `publishedTree.rows == authority.entries == bundle.entries`, closing the predicted-authority-versus-observed-tree gap.

- RED: `swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests.testCrossValidationRejectsPublishedClosureThatDiffersFromApprovedCheckout` failed because resealing the receipt's published closure allowed a changed tree.
- GREEN: the same command passed after equality was added to `V2Evidence.baseCrossValidate`; `git diff --check` passed.
