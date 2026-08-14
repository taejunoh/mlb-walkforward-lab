# v5.2 R4 Signed Custody Anchor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the R4 raw-free anti-forgery and owner-lock release gaps with one production Ed25519 authority anchor whose private key remains outside Git in a frozen offline seed file.

**Architecture:** The existing R4 inventory, receipt, contract, and RunSpec remain unchanged. A new signed anchor is published last and binds those documents plus issuance-specific APFS identity digests; verification authenticates the signature before trusting the inventory, then descriptor-rehashes the live immutable snapshot. Static authority artifacts are frozen and authenticated as one descriptor bundle before the original owner lock is released.

**Tech Stack:** Python 3.11, `cryptography` Ed25519, Darwin no-follow/file-flag APIs, existing descriptor/APFS/secure-fs modules, JSON Schema, `unittest`, Ruff.

## Global Constraints

- Existing R2/R3 bytes and accepted behavior remain unchanged.
- Existing R4 RunSpec, inventory, receipt, and contract schemas/bytes are not retroactively edited.
- Baseline plan `docs/superpowers/plans/2026-08-04-v5-2-r4-physical-custody-authority.md` remains unchanged.
- Approved amendment design is `docs/superpowers/specs/2026-08-05-v5-2-r4-signed-custody-anchor-design.md`; its final blob/path hash is bound when the signed anchor is issued.
- Production source inventory SHA remains `2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6`.
- Production signing seed is never written to argv, environment, repository, filesystem scratch, logs, exceptions, test snapshots, or command output.
- The production offline seed path comes from `pwd.getpwuid(os.getuid()).pw_dir`, never `HOME`: `~/Library/Application Support/mlb-walkforward-lab/keys/v5_2_r4_custody_anchor.ed25519.seed`.
- The parent directory is mode `0700`; the regular 32-byte seed is mode `0400`, current-UID owned, nlink `1`, and `UF_IMMUTABLE`. Same-UID/process compromise and transient in-process key copies are explicit out-of-scope host compromises; FileVault and permissions protect the file at rest.
- Signer reads use no-follow descriptors with lstat/rebind, UID/type/size/mode/nlink/flag checks; mutable application seed buffers are overwritten best-effort after use.
- Production CLI/Python APIs expose no key, key-id, authority, digest, environment, or fixture override.
- Synthetic tests use only the existing private `_resolve_source_authority` provider seam.
- No real Task 12 custody/audit/protocol output is created until Tasks 1–4 and both independent reviews are green.
- Run every Python command with `/opt/homebrew/bin/python3.11` and `PYTHONDONTWRITEBYTECODE=1`.

## File Map

| Path | Responsibility |
| --- | --- |
| `mlb_backtest/v52_r4_offline_signer.py` | Exclusive offline seed creation/retrieval, no-follow custody checks, Ed25519 signing, mutable-buffer zeroization, and no-secret errors. |
| `scripts/migrate_v52_r4_signing_key_to_offline.py` | One-time exact legacy-item copy-and-verify migration to the frozen offline seed; leaves legacy material untouched. |
| `mlb_backtest/v52_r4_production_public_key.py` | Generated, committed production public key and derived key ID; contains no private material. |
| `mlb_backtest/v52_r4_signed_anchor.py` | Closed anchor build/render/read/schema/signature/hash validation. |
| `schemas/v5_2_r4_physical_custody_authority_anchor.schema.json` | Closed anchor JSON schema. |
| `mlb_backtest/v52_r4_snapshot_issuer.py` | Publish anchor/signature, freeze static files, descriptor `ReleaseBundle`, raw-free verify, output preflight. |
| `mlb_backtest/v52_r4_authority.py` | Fixed production authority resolution and one private fixture seam; no public digest override. |
| `tests/r4_issued_authority_fixture.py` | Deterministic test key/profile through the one private provider seam. |
| `tests/test_v52_r4_offline_signer.py` | Offline seed custody, migration ordering, and secret-handling tests. |
| `tests/test_v52_r4_signed_anchor.py` | Schema/signature/hash/anti-forgery tests. |
| `tests/test_v52_r4_snapshot_issuer.py` | Transaction, preflight, release-race, CLI, and full raw-free verification tests. |

---

### Task 1: Provision and pin the production signing authority

**Files:**
- Create: `mlb_backtest/v52_r4_offline_signer.py`
- Create: `scripts/migrate_v52_r4_signing_key_to_offline.py`
- Generate: `mlb_backtest/v52_r4_production_public_key.py`
- Create: `tests/test_v52_r4_offline_signer.py`

**Interfaces:**
- Produces: `sign_anchor_message(message: bytes) -> bytes`
- Produces: `PRODUCTION_PUBLIC_KEY: bytes`, `PRODUCTION_KEY_ID: str`
- Offline seed path: `~/Library/Application Support/mlb-walkforward-lab/keys/v5_2_r4_custody_anchor.ed25519.seed`, resolved with `pwd.getpwuid(os.getuid()).pw_dir`

- [ ] **Step 1: Write RED tests for secret-safe key management.**

Add tests for exclusive parent/file creation, mode `0700`/`0400`, current UID, regular type, nlink `1`, no symlink or hard-link alias, exact 32-byte length, `UF_IMMUTABLE`, no-follow descriptor reads, full pre/post-read `fstat` metadata and parent rebind checks, pinned-public-key equality, signature length `64`, and best-effort mutable-buffer clearing. Assert the seed never appears in argv, environment, stdout, stderr, exception text, or the generated public module. Include migration tests proving that a valid frozen target returns ready without legacy access, a failed public-key check or offline fsync/sign/verify leaves both target and exact legacy item untouched, copy-and-verify never deletes legacy material, and failed creation blocks automatic retry/cleanup. A real Darwin integration test must call `fchflags(UF_IMMUTABLE)` and verify flags plus the complete `fstat` tuple.

- [ ] **Step 2: Run RED.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_offline_signer -v
```

Expected: FAIL because the offline signer/migration modules do not exist.

- [ ] **Step 3: Implement the minimal offline signer and migration script.**

Resolve the parent from `pwd.getpwuid(os.getuid()).pw_dir`; ignore `HOME`. If the target already exists, validate its complete frozen metadata and return an idempotent ready result when valid. Otherwise create the parent with `0700`, exclusively create the raw 32-byte seed, fsync contents and parent, set `0400` and Darwin `fchflags(UF_IMMUTABLE)`, and reopen through `O_NOFOLLOW`. Before reading and again after reading, validate an identical full `fstat` tuple (device, inode, mode, UID, nlink, size, and flags), plus parent identity. Read into a mutable buffer, derive Ed25519 public key, require equality with the pinned module, sign, and zeroize the buffer in `finally`. If creation or validation fails, do not path-unlink a candidate or temporary output; retain the incomplete state, fail closed, and block retry pending manual inspection. The owner-lock unlink remains restricted to the successful descriptor-authenticated issuance transaction. No public or test override may inject key material into production resolution.

The one-time migration script first validates an existing offline target and returns ready without legacy access when valid. Otherwise it reads the exact legacy item from `login_renamed_1.keychain-db` into a mutable buffer, derives and checks the pinned public key, exclusively writes and freezes the offline file, reopens and sign/verifies, and emits a seed-free ready receipt while leaving the legacy item untouched. Automated deletion is forbidden because a Security.framework pathname handle cannot be cryptographically or atomically bound to its backing inode; manual legacy cleanup is deferred outside this protocol and is not a Task 12 gate. Any failed creation remains fail-closed without automatic path cleanup or retry. It never prints or logs the seed.

- [ ] **Step 4: Verify GREEN before migration.**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_offline_signer -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_offline_signer.py scripts/migrate_v52_r4_signing_key_to_offline.py tests/test_v52_r4_offline_signer.py
git diff --check
```

Expected: PASS with no production Keychain or offline-seed mutation. Use only temporary directories and fake legacy bindings in unit tests; do not probe or rename production Keychain paths. The Darwin integration test may exercise `fchflags` only inside a disposable temporary target.

- [ ] **Step 5: Migrate once after both reviews.**

After implementation, specification review, and quality review are green, run the copy-and-verify migration exactly once. It first returns `ready` for an already-valid frozen target; otherwise it emits only a seed-free receipt (`offline_seed_ready` and public key ID) after fresh sign/verify. Confirm the exact legacy item remains untouched. Manual legacy cleanup is deferred and is not required for Task 12.

- [ ] **Step 6: Commit.**

```bash
git add mlb_backtest/v52_r4_offline_signer.py mlb_backtest/v52_r4_production_public_key.py scripts/migrate_v52_r4_signing_key_to_offline.py tests/test_v52_r4_offline_signer.py
git rm mlb_backtest/v52_r4_keychain_signer.py scripts/provision_v52_r4_signing_key.py
git commit -m "feat: move r4 signer to offline authority"
```

### Task 2: Implement the closed signed anchor

**Files:**
- Create: `mlb_backtest/v52_r4_signed_anchor.py`
- Create: `schemas/v5_2_r4_physical_custody_authority_anchor.schema.json`
- Create: `tests/test_v52_r4_signed_anchor.py`
- Modify: `mlb_backtest/v52_r4_authority.py`
- Modify: `tests/r4_issued_authority_fixture.py`

**Interfaces:**
- Produces: `render_authority_anchor(document: Mapping[str, object]) -> bytes`
- Produces: `build_authority_anchor(...) -> Mapping[str, object]`
- Produces: `verify_authority_anchor(anchor_bytes: bytes, signature: bytes, *, public_key: bytes, expected_key_id: str) -> Mapping[str, object]`
- Domain separator: `b"MLB-R4-CUSTODY-ANCHOR-v1\0"`

- [ ] **Step 1: Write RED schema, signature, and mutation tests.**

Require canonical newline JSON, closed objects, exact paths/kind/version/key ID, fixed production source approval, exact document/amendment bindings, identity-manifest and receipt comparison digests. Test wrong signature length, wrong public key, wrong key ID, noncanonical JSON, every extra property, every mutated hash, and a valid deterministic Ed25519 test key.

- [ ] **Step 2: Run RED.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_signed_anchor -v
```

Expected: FAIL because anchor support does not exist.

- [ ] **Step 3: Implement minimal closed anchor support.**

The verifier validates signature before parsing any mutable inventory as authority. It then schema-validates and performs strict canonical-byte equality. Production wrappers use only `PRODUCTION_PUBLIC_KEY` and `PRODUCTION_KEY_ID`. Remove exported/public `expected_sha256` parameters from `v52_r4_authority.py`; the private provider returns the test public key and signer capability only inside fixture execution.

- [ ] **Step 4: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_signed_anchor tests.test_v52_r4_offline_signer -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_signed_anchor.py mlb_backtest/v52_r4_authority.py tests/test_v52_r4_signed_anchor.py tests/r4_issued_authority_fixture.py
git diff --check
git add mlb_backtest/v52_r4_signed_anchor.py mlb_backtest/v52_r4_authority.py schemas/v5_2_r4_physical_custody_authority_anchor.schema.json tests/test_v52_r4_signed_anchor.py tests/r4_issued_authority_fixture.py
git commit -m "feat: add signed r4 custody anchor"
```

### Task 3: Make issuance and verification anchor-rooted

**Files:**
- Modify: `mlb_backtest/v52_r4_snapshot_issuer.py`
- Modify: `tests/test_v52_r4_snapshot_issuer.py`
- Modify: `tests/r4_issued_authority_fixture.py`

**Interfaces:**
- Produces: internal immutable `ReleaseBundle`
- `issue_and_bind` publishes anchor JSON/signature after contract while retaining the original owner capability
- `verify` requires anchor/signature and no external raw argument

- [ ] **Step 1: Write four independent RED groups.**

Add tests proving:

1. consistent replacement of inventory/receipt/contract and changed snapshot cannot pass the original signed anchor;
2. swaps before bundle creation, during authentication, and immediately before final rebind/unlink keep the owner lock and never report `EXECUTABLE`;
3. pre-existing output inventory/receipt/contract/anchor/signature fails preflight before snapshot publication and leaves no final root, temp, or lock; and
4. output repository has no `data/v5_1_bt/raw`, yet post-release and fresh-process CLI verify succeed from signed artifacts and the fixed protected inventory.

- [ ] **Step 2: Run RED and record the expected first failure for each group.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_snapshot_issuer -v
```

Expected: the anti-forgery case passes incorrectly or errors in missing anchor support; release race permits unlink; output preflight observes the wrong repository.

- [ ] **Step 3: Implement output preflight and owner-only anchor publication.**

Resolve output state from the RunSpec/contract repository before lock acquisition. After contract publication, build/sign/publish anchor JSON and the exact 64-byte signature exclusively. A signer or publication failure retains the lock.

- [ ] **Step 4: Implement freeze and descriptor `ReleaseBundle`.**

Freeze inventory, receipt, contract, anchor, and signature to read-only plus `UF_IMMUTABLE`. Hold no-follow descriptors for all static artifacts, their parents, RunSpec, and snapshot root. Authenticate signature/hashes and recursively rehash/rebind 4,711 members, four locks, all ancestors, modes, and flags. Rebind every final directory entry and the owner lock immediately before unlink; any mismatch retains the lock.

After any pre-freeze or validation failure, do not path-unlink a candidate or temporary output: unlink has a final TOCTOU window. Close descriptors, record a seed-free failure, and block retry pending manual inspection. A valid already-frozen target is checked first and may return ready idempotently; no restart adopts incomplete state.

- [ ] **Step 5: Replace self-authenticating raw-free verify.**

Verify signature first, use the fixed `2ec8...` protected inventory bytes to reconstruct expected logical records, compare R4 inventory to that authority, and compare the live snapshot to the signed inventory. Never derive the expected source profile from the mutable issued inventory.

- [ ] **Step 6: Run GREEN and commit.**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_snapshot_issuer tests.test_v52_r4_custody_artifacts tests.test_v52_r4_signed_anchor tests.test_v52_r4_offline_signer -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_snapshot_issuer.py tests/test_v52_r4_snapshot_issuer.py tests/r4_issued_authority_fixture.py
git diff --check
git add mlb_backtest/v52_r4_snapshot_issuer.py tests/test_v52_r4_snapshot_issuer.py tests/r4_issued_authority_fixture.py
git commit -m "fix: anchor r4 issuance and lock release"
```

### Task 4: Re-run Task 11 gates

**Files:**
- Review only: all Task 1–3 changes and `f7a07da..HEAD`

- [ ] **Step 1: Run the full focused suite and import-isolation check.**

```bash
MLB_V52_SOURCE_AUTHORITY_ROOT='/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_offline_signer tests.test_v52_r4_signed_anchor tests.test_v52_r4_snapshot_issuer tests.test_v52_r4_custody_artifacts tests.test_v52_acquisition_contract tests.test_v52_query_plan tests.test_protocols -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest tests scripts
git diff --check
```

Expected: PASS; importing issuer/fixture/source-contract/RunSpec paths does not load `mlb_backtest.v52_query_plan`, `mlb_backtest.diagnostics`, or `scipy`.

- [ ] **Step 2: Independent specification review.**

Reviewer checks the 2026-08-04 baseline plus this amendment and reports `✅ Spec compliant` with no open item.

- [ ] **Step 3: Independent quality/security review.**

Fresh reviewer checks key secrecy, public override absence, signed anti-forgery, physical snapshot rebind, output preflight, immutable release bundle, fail-closed failure retention (no path cleanup), and test realism. Expected: APPROVED with no P0–P2.

### Task 5: Operate the signed Task 12 issuance exactly once

**Files created only in this task:**
- `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4`
- R4 inventory, receipt, contract from the baseline plan
- `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4.json`
- `audit/v5_2_qd/historical_recovery_schedule_conflicts_missing_venue_r4/acquisition/physical_custody_authority_anchor_r4.ed25519`

- [ ] **Step 1: Prove a clean committed issuer and offline signer.**

Require clean Git status, committed Task 11 HEAD, successful public-key/signature self-test, valid frozen offline seed custody (or an idempotent ready target), absent final custody root, absent owner lock/temp, and absent all five static R4 authority outputs. Do not require or perform automated legacy-item deletion.

- [ ] **Step 2: Run baseline preflight/provision/preflight with the external v5.1 source and separated output repository.**

Expected: initial custody-unavailable where specified by the baseline, then `ABSENT` with exact 1,675/4,711/5,025 and no output mutation.

- [ ] **Step 3: Run `issue-and-bind` once.**

Expected order: snapshot→inventory→receipt→contract→anchor→signature→freeze→bundle authentication→lock release. Any failure leaves observer-incomplete and stops without retry/adoption.

- [ ] **Step 4: Run fresh-process signed `verify`.**

Expected: `EXECUTABLE`; signature and all hashes pass; live snapshot rehash/identity/flags pass; no lock/temp; output repository has no v5.1 raw tree.

- [ ] **Step 5: Commit only the generated authority artifacts.**

Stage inventory, receipt, contract, anchor, and signature explicitly. Do not stage custody bytes, offline seed bytes, legacy Keychain data, logs, or unrelated files. Commit as `audit: issue signed r4 custody authority`.

### Task 6: Require the signed anchor downstream

**Files:**
- Modify during baseline Tasks 13–21: R4 external protected-parent, acquire, registry/evidence, readiness/finalization, terminal verifier, exact-suite tests

- [ ] **Step 1: Add RED admission tests.**

Every R4 downstream authority entry rejects absent/invalid anchor, signature, key ID, artifact hash, snapshot identity, mode, or immutable flag before reading/acquiring source data. R2/R3 behavior remains unchanged.

- [ ] **Step 2: Thread one read-only verified authority capability.**

Only the signed verifier issues the R4 external-parent capability. Downstream layers consume that opaque capability rather than re-parsing mutable artifacts independently.

- [ ] **Step 3: Run each original baseline task gate and final exact suite.**

Expected: all baseline Task 13–21 acceptance conditions plus signed-anchor admission pass; protected old evidence hashes remain unchanged.
