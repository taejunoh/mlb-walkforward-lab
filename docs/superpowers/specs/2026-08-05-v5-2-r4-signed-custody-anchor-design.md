# v5.2 R4 Signed Custody Anchor Design

**Status:** Approved option 1 on 2026-08-05

**Scope:** Add an issuance-specific Ed25519 trust anchor to the already approved R4 physical-custody protocol. This amendment closes the raw-free verification and lock-release TOCTOU gaps found in the Task 11 quality review. It does not change R2/R3 bytes, v5.2.3 source semantics, the approved R4 RunSpec, or the existing R4 inventory/receipt/contract schemas.

## Decision

The existing fixed v5.1 source inventory SHA authenticates logical source records, sizes, and payload hashes without reopening the external raw tree. It cannot authenticate the R4 snapshot's newly created APFS volume/inode/ctime identities because those values exist only after issuance. Therefore an attacker able to replace the snapshot, inventory, receipt, and contract consistently could create a self-authenticating replacement.

R4 will publish an issuance-specific signed authority anchor after the contract and before owner-lock release. The anchor is the independent root of trust for the generated snapshot identity. Verification authenticates the signature first, then the exact artifact hashes, then rehashes and identity-rebinds the live immutable snapshot against the signed inventory.

The pre-existing plan remains an immutable baseline because the R4 RunSpec and contract already bind it. This amendment is a separate committed design and plan; the signed anchor binds both amendment documents by path, SHA-256, and Git blob ID.

## Authority Artifacts

Task 12 adds exactly these final files:

```text
audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4.json
audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4.ed25519
schemas/v5_2_r4_physical_custody_authority_anchor.schema.json
```

The JSON document is canonical `canonical_json_bytes(document) + b"\n"`. The detached signature is exactly 64 raw bytes over:

```text
b"MLB-R4-CUSTODY-ANCHOR-v1\0" + canonical_anchor_json_bytes
```

The anchor schema is closed at every level and contains exactly:

- `schema_version = "v5.2-r4-physical-custody-authority-anchor-v1"`;
- `kind = "MLB_R4_PHYSICAL_CUSTODY_AUTHORITY"`;
- the fixed production `key_id`;
- `source_approval` with logical root, protected-inventory path, and fixed SHA `2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6`;
- exact path/SHA-256 bindings for the R4 inventory, transition receipt, acquisition contract, and R4 RunSpec;
- exact path/SHA-256/Git-blob bindings for this amendment design and implementation plan;
- the inventory `custody_identity_manifest_sha256`;
- the receipt physical-content and canonical-binding comparison digests.

The contract does not hash the anchor. The anchor is published after and hashes the contract, avoiding an anchor/contract cycle. Downstream R4 admission requires the fixed anchor path, signature path, key ID, and successful signature verification as an external protocol rule.

## Signing Key Custody

The production Ed25519 private seed is generated once with `cryptography` and stored outside the repository in a dedicated offline file. The path is resolved from the OS account home returned by `pwd.getpwuid(os.getuid()).pw_dir` (never `HOME` or any other environment variable):

```text
~/Library/Application Support/mlb-walkforward-lab/keys/v5_2_r4_custody_anchor.ed25519.seed
```

The parent directory is created with mode `0700`. The seed file is exactly 32 raw bytes, a regular file owned by the current UID, mode `0400`, link count `1`, and has the macOS `UF_IMMUTABLE` flag. Production signing opens it with no-follow semantics, captures a complete `fstat` metadata tuple (device, inode, mode, UID, nlink, size, and flags) before reading, and requires the identical full tuple after reading; parent identity is also rebound. It copies the bytes into a mutable buffer, derives the Ed25519 public key, and requires equality with the pinned public key module before any signature is returned. Mutable buffers are overwritten on every exit path on a best-effort basis. The seed never crosses argv, an environment variable, repository files, scratch files, stdout, stderr, or exception text.

The rejected CLI/PTTY design is not a fallback: its explicit-target prompt flow hung or failed because this Mac has no user-domain default Keychain. The legacy login-Keychain attempt is also rejected for production custody: the system synchronously renamed `login.keychain-db` to `login_renamed_1.keychain-db` during production Security.framework access, making that path operationally unstable. A Data Protection Keychain `SecItemAdd` probe returned `-34018` (missing entitlement) from bare Python. These are recorded evidence, not implementation alternatives.

The offline file is created exclusively (no overwrite), fsynced, reopened through a no-follow descriptor, revalidated, and then frozen with Darwin `fchflags(..., UF_IMMUTABLE)`. The signer refuses symlinks, hard-link aliases, ownership/mode/size/flag drift, and public-key mismatch. The committed production verifier hard-codes the public key and key ID before Task 12, and an explicit import-time comparison raises `RuntimeError` on mismatch even under `python -O`. The key ID is:

```text
ed25519-sha256:<sha256(raw_32_byte_public_key)>
```

The default CLI and Python production paths accept no key, public-key, key-id, authority, environment, or test override. Tests receive a deterministic test signer only through the same single private `_resolve_source_authority` provider seam already used by the full synthetic fixture.

FileVault and filesystem permissions protect the seed at rest. A same-UID/process attacker able to read or replace the seed while the issuing process runs, or inspect transient copies retained by CPython, `cryptography`, or OpenSSL, is outside this protocol's threat model; that actor can already control the trusted checkout or issuing process. Descriptor no-follow and pre/post identity checks are defense-in-depth and do not claim immunity from a privileged kernel peer.

### One-time legacy migration

Migration is a separate, one-time operation and is forbidden until the implementation and both independent reviews are green. It first checks whether the offline target already exists and is a valid frozen seed; a valid target returns a seed-free `ready` result without touching the legacy item. Otherwise it reads the exact existing legacy item from `login_renamed_1.keychain-db` into a mutable buffer, derives and verifies the pinned public key, then exclusively creates the offline file, fsyncs its parent and contents, sets mode/ownership/immutable state with Darwin `fchflags`, reopens and performs a sign/verify self-test. The migration only copies and verifies: it leaves the legacy item untouched and emits a seed-free ready receipt. Automated deletion is forbidden because a legacy Security.framework pathname handle cannot be cryptographically or atomically bound to its backing inode; pathname deletion would add a final TOCTOU. Manual legacy cleanup is explicitly deferred outside this automated protocol and is not a Task 12 gate. The migration never emits or logs the seed and zeroizes all application buffers/references on exit.

### Failed creation and retry behavior

After a candidate seed or temporary output has been validated, no path-based automatic unlink or cleanup is permitted: unlink has its own final TOCTOU window and could remove an unrelated replacement. A failed or partially created target is therefore fail-closed; the signer closes descriptors, records a seed-free failure, and blocks retry until manual inspection resolves the path. On every invocation, a valid existing frozen target is checked first and returns `ready` idempotently. No restart adopts an incomplete target, and no automated step deletes legacy or offline custody material. The separate owner-lock unlink occurs only in the successful descriptor-authenticated issuance transaction described below.

## Issuance Transaction

The owner-only order becomes:

```text
snapshot -> R4 inventory -> transition receipt -> contract
-> signed anchor JSON -> detached signature
-> freeze static artifacts -> descriptor-bundle authentication
-> owner-lock release
```

The owner publishes the anchor and signature exclusively through authenticated same-directory temporary files. Signer denial, malformed key material, offline-seed path conflict or custody drift, signature mismatch, or freeze failure leaves the issuance lock and incomplete state. No restarted process adopts or completes the transaction.

Before release, inventory, receipt, contract, anchor, and signature are set to read-only and `UF_IMMUTABLE`. The owner opens no-follow descriptors for the snapshot root, RunSpec, all static artifacts, and their parents and retains them in one `ReleaseBundle`. It verifies signature and hashes from those descriptors, recursively rehashes/rebinds all 4,711 members, four locks, ancestors, modes, and immutable flags against the signed inventory, rebinds every final directory entry to the held descriptor identity, rechecks the owner lock and absence of the temporary root, then unlinks only the owner lock and fsyncs the custody parent.

There is no claim of cross-directory atomicity against a privileged hostile kernel peer. The security boundary is the existing Darwin no-follow descriptor identity plus `UF_IMMUTABLE`. Any observed path/identity/hash/flag change before release keeps the lock; any later change makes the next observer fail closed.

## Raw-Free Verification

The standalone `verify` command needs no external raw-root argument. It:

1. verifies the detached signature with the hard-coded production public key and exact key ID;
2. authenticates the amendment documents and inventory/receipt/contract/RunSpec against signed hashes;
3. authenticates the fixed v5.1 protected inventory by its fixed `2ec8...` SHA when resolving the logical source closure;
4. validates the R4 inventory against those expected source records;
5. recursively rehashes and APFS-identity-rebinds the live snapshot against the signed inventory; and
6. requires all static artifacts and snapshot objects to retain their required modes and `UF_IMMUTABLE` flags.

The issued R4 inventory is never used as its own expected authority profile.

## Other Quality Findings

- Issuance preflight observes the output repository named by the RunSpec/contract targets, not the external source repository. Existing output authority files fail before snapshot publication and leave no final snapshot or lock.
- Public `expected_sha256` fixture overrides are removed. Production authority constructors remain fixed and fail closed; the single private provider seam is the only synthetic fixture entry.
- `v52_source_contracts`, RunSpec parsing, and the shared fixture do not import `v52_query_plan`, diagnostics, or SciPy.

## Tests and Acceptance

Tests must first demonstrate failure for: consistent inventory/receipt/contract/snapshot replacement; wrong signature; wrong key ID; wrong anchor hash; changed live member with updated mutable artifacts; artifact swaps before bundle creation, during validation, and immediately before final rebind/unlink; existing output evidence before preflight; missing/symlink/hard-link/mode/owner/nlink/size/flag drift in the offline seed; post-read `fstat` changes; failed migration copy/verify with legacy item preserved; valid-target idempotent readiness without legacy access; no path-based cleanup after a failed creation; and every public override attempt. A real Darwin integration test must exercise `fchflags(UF_IMMUTABLE)` and verify the frozen target's flags and full `fstat` tuple.

Task 11 is complete only after:

- all RED cases pass after the minimal implementation;
- the full fixture uses distinct source and output repositories and no output v5.1 raw tree;
- focused issuer/artifact/contract/protocol tests pass under `/opt/homebrew/bin/python3.11`;
- Ruff and `git diff --check` pass;
- independent specification review reports compliant; and
- independent quality review reports no P0, P1, or P2 issue.

Task 12 remains forbidden until those gates pass and the production offline-signer public key/key ID are committed in verifier code. The migration itself must have a successful, seed-free copy-and-verify receipt before issuance is permitted; deferred manual legacy cleanup is not a gate.
