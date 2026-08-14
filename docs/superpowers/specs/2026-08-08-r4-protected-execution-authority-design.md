# R4 Protected Execution Authority Design

**Status:** Approved design — implementation and production rollout are separately authorized.

**Scope:** Replace the R4-disjoint execution-binding trust boundary with one root-owned, one-shot provisioning authority. A linked worktree and its Homebrew Python are untrusted candidate inputs. They may prepare a candidate, but cannot create execution authority, sign a receipt, install a helper, or influence the protected authority after validation. The design does not authorize a production custody operation.

## Decision

The approved architecture is a local, root-owned Swift provisioner, invoked once per fixed authority ID. It has no daemon, no listener, no network operation, no service account, and no recovery mode. It validates a pre-expanded candidate checkout and a pinned hermetic Python 3.11 runtime closure, copies the exact approved closure through descriptor-relative no-follow operations into root-owned staging, fully revalidates it, and atomically publishes a protected execution authority. It then signs a receipt with a root-only Ed25519 provisioning key.

The canonical protected authority root is:

```text
/Library/Application Support/mlb-walkforward-lab/protected-execution-authority
```

The only namespace and authority ID are fixed literals:

```text
namespace: org.mlb-walkforward.r4.protected-execution-authority/v1
authority_id: r4-disjoint-custody-v2-execution-v1
```

No command-line option, environment variable, symlink, current directory, repository configuration, `PATH`, `PYTHONHOME`, `PYTHONPATH`, `DYLD_*`, or candidate-supplied configuration can select a different namespace, authority ID, output root, signing key, manifest, or helper.

## Goals and Non-goals

### Goals

1. Permit the R4 issuer to execute only from a root-owned, sealed, fully enumerated code-and-runtime closure.
2. Preserve Git commit/tree/required-blob evidence for the reviewed candidate without trusting a live Git checkout at execution time.
3. Make every accepted byte, file type, mode, and dynamic dependency auditable from protected authority alone.
4. Make failure forensic: an interrupted or invalid attempt is quarantined and cannot be resumed, adopted, overwritten, or retried with the same authority ID.
5. Keep privileged production rollout as a distinct approval after Task 14 gates and protected provisioning both succeed.

### Non-goals and explicit exclusions

- This design does not issue, verify, alter, or inspect any production custody snapshot or prior custody evidence.
- It does not create a general code-deployment system, multi-user execution service, auto-update mechanism, network downloader, background daemon, or generic root file copier.
- It does not trust a candidate signature as authority. Candidate signatures may be preserved as non-authoritative evidence only.
- It does not make same-user compromise impossible before root provisioning. It converts that risk into a fail-closed candidate-validation boundary; only root-controlled installation and receipt issuance establish authority.
- It does not authorize bypassing macOS code-signing, System Integrity Protection, Gatekeeper, ACLs, or volume policy. An unsupported protected filesystem or immutable-flag operation rejects provisioning.

## Trust Model

| Principal or location | Trust level | Permitted role |
| --- | --- | --- |
| Linked worktree, user home, Homebrew Python, shell environment | Untrusted | Build a candidate directory and request provisioning. No authority. |
| Task 14 reviewed checkout and gates | Review evidence | Supplies exact approved manifest, Git evidence, and helper digests for administrator review. It is not executable authority by itself. |
| Root administrator during protected install | Trust anchor | Reviews fixed helper source/binary digests and Task 14 approval, installs the helper, and invokes one-shot provisioning. |
| Protected authority root | Authority storage | Root:wheel-owned, non-user-writable sealed closure, public key, policy, receipt, and revocation state. |
| Root-only Ed25519 key | Signing authority | Signs only a successful protected receipt. It never leaves protected root storage or appears in logs, manifests, candidate files, or process arguments. |

The candidate is adversarial for all validation purposes. It can replace paths during traversal, lie about Git state, supply a malicious Python executable, use hard links, use symlinks, exploit repository hooks/configuration, or change files after a hash. The provisioner must therefore bind held descriptors to descriptor-relative names before and after every security-relevant read/copy, reject all unsupported types, and make the root-owned copy—not the candidate pathname—the eventual execution target.

Root remains a high-impact trust boundary. A root compromise, boot-chain compromise, or approved-administrator compromise can subvert this design and is outside its ability to prevent. The design records the key and helper identities needed to investigate such an event and provides a revocation path.

## Bootstrap Trust Without Circularity

The provisioner cannot authenticate itself with the receipt it is about to create. Bootstrap is therefore an explicit, human-reviewed installation ceremony:

1. Task 14 produces an exact canonical candidate-manifest hash, Git HEAD/tree and required blob values, hermetic runtime manifest hash, provisioner Swift-source hash, and provisioner signed-binary SHA-256. These values are reviewed before any privileged install.
2. An administrator compares the reviewed Swift source and compiled binary digests against the immutable Task 14 approval envelope, verifies the binary's macOS code signature and designated requirement, and installs that one binary with root:wheel ownership into root-only installation staging. The install refuses an ad-hoc signature, an unexpected Team ID/designated requirement, or a binary/source digest mismatch.
3. Still in root-only installation staging and before any candidate exists, the installer generates the initial Ed25519 private key from the system cryptographic RNG. The private key is mode `0600`, root:wheel, and is never copied from the candidate. It derives the public key and then writes a bootstrap policy with the concrete public-key fingerprint, approved provisioner binary digest, code-signing requirement, fixed namespace/authority ID, Task 14 approval envelope hash, and fixed layout.
4. The installer atomically publishes the complete bootstrap tree, `fsync`s its parent, applies the system immutable flag to the bootstrap policy, binary, public key, and initial key-generation record, and separately seals the private key after validating that signing still works. Thus the public-key pin is present before the first receipt and no sealed policy needs to be rewritten to create it.
5. The installer reopens every installed object by no-follow descriptor, verifies identity, ownership, mode, system immutable flag, binary digest, signature requirement, bootstrap policy, public key, and absence of unexpected entries. Only then is the helper an installed identity and only then can it issue a receipt.

The administrator's review of source/binary digests is the initial trust decision. The root-only generated key and protected public-key pinning extend that decision to receipts. A candidate-provided public key, key seed, signature, checksum, or certificate never replaces any of those checks.

## Fixed Protected Layout

All paths below are relative to the canonical protected root; no other root is accepted.

```text
bootstrap/
  R4_PROTECTED_EXECUTION_BOOTSTRAP_V1.json
  provisioner
  provisioner.codesign-requirement.txt
  provisioner.sha256
  public.ed25519
  public.ed25519.sha256
  private.ed25519                         # root-only; never read by issuer
  initial-key-generation.json
control/                                  # root:wheel 0700; no user access
  keyring/
    00000001/
      keyring-record.json
      keyring-record.ed25519
      public.ed25519
      private.ed25519                     # root-only, 0600, system immutable
  revocations/
authorities/
  r4-disjoint-custody-v2-execution-v1/
    authority-manifest.json
    task14-approval.json
    runtime-manifest.json
    receipt.json
    receipt.ed25519
    closure/
      checkout/
      python311/
quarantine/
  r4-disjoint-custody-v2-execution-v1.<attempt-uuid>/
```

`authorities/<authority_id>` is published once. The provisioner refuses if that directory, a matching quarantine directory, or any unrecognized child of `authorities` exists. It never removes, renames, truncates, clears flags on, or reuses either published or quarantine state. A later authority requires a newly approved namespace/version and a different authority ID; it is not a retry.

Before sealing, authority files are root:wheel `0444` (private keys `0600`), authority directories are root:wheel `0555` (private-key parents `0700`), and every sealed authority artifact is `SF_IMMUTABLE` (the Darwin system immutable flag). Ownership, modes, flags, ACL absence, and descriptor/name identity are verified after each transition and again after publish. `control` is the sole root-only mutable registry parent: it is root:wheel `0700`, never user-writable, and may receive only new immutable keyring/revocation records by its separate administrative protocol. It contains no candidate-controlled object. If the volume, filesystem, kernel, or process cannot set and verify `SF_IMMUTABLE` for every sealed object, provisioning fails and quarantines the attempt; `UF_IMMUTABLE`, a read-only mount, or best-effort chmod alone is insufficient.

On macOS, immutable and code-signing behavior varies with APFS, SIP, MDM, and toolchain policy. The provisioner treats inability to apply or read back the required system flag, unexpected ACLs, an invalid designated requirement, or a changed code-signing assessment as an unsupported platform, never as a warning. Its own signature is verified before execution by the bootstrap installer and again by the provisioner from its held executable descriptor. The sealed Python closure is also code-signing-checked where it contains signed Mach-O executables/frameworks; unsigned Python wheels may exist only if their exact manifest hash is approved and their native dependency closure passes the rules below.

## Canonical Evidence Schemas

All canonical JSON uses UTF-8, RFC 8785 canonical JSON, no duplicate keys, no floats, newline-free bytes, lowercase hexadecimal SHA-256, and closed objects. A digest is SHA-256 over the exact canonical bytes of its object with the `sha256` field omitted. Arrays are ordered and duplicate paths are invalid.

### `authority-manifest.json`

```json
{
  "kind": "R4_PROTECTED_EXECUTION_MANIFEST_V1",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "git": {
    "head": "<40 lowercase git commit hex>",
    "tree": "<40 lowercase git tree hex>",
    "required_blobs": [
      {"path": "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md", "blob": "<40 hex>", "sha256": "<64 hex>"},
      {"path": "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md", "blob": "<40 hex>", "sha256": "<64 hex>"},
      {"path": "docs/superpowers/specs/2026-08-08-r4-protected-execution-authority-design.md", "blob": "<40 hex>", "sha256": "<64 hex>"},
      {"path": "mlb_backtest/v52_r4_disjoint_issuer.py", "blob": "<40 hex>", "sha256": "<64 hex>"},
      {"path": "mlb_backtest/v52_r4_disjoint_execution.py", "blob": "<40 hex>", "sha256": "<64 hex>"},
      {"path": "mlb_backtest/v52_r4_disjoint_policy.py", "blob": "<40 hex>", "sha256": "<64 hex>"}
    ]
  },
  "entries": [
    {"path": "checkout", "type": "directory", "mode": "0555", "size": 0, "sha256": "<64 hex>"},
    {"path": "checkout/mlb_backtest/v52_r4_disjoint_issuer.py", "type": "regular", "mode": "0444", "size": 1, "sha256": "<64 hex>"}
  ],
  "runtime_manifest_sha256": "<64 hex>",
  "sha256": "<64 hex>"
}
```

The real manifest is exhaustive, not illustrative: it has one entry for every directory, regular file, symlink prohibition decision, executable, Python standard-library file, extension module, package metadata, and approved runtime support object copied into `closure`. `path` is normalized POSIX relative syntax with no empty component, `.`, `..`, Unicode-normalization ambiguity, absolute prefix, or case-fold collision. `type` is exactly `directory` or `regular`; sockets, devices, FIFOs, symlinks, and `nlink != 1` regular files are rejected. Every entry has path, type, mode, size, and SHA-256: for a regular file it is the file-byte digest; for a directory it is the domain-separated canonical digest of its immediate child `(name, type, mode, size, sha256)` rows. Each directory has explicit size `0`. The manifest's lexicographic path order is part of the digest.

The manifest is generated from a pre-expanded candidate checkout, not from a live `git checkout`, `pip`, `venv`, Homebrew, or network action. Its Git evidence is checked by Task 14 with non-hook Git plumbing and is copied as evidence into the protected manifest. At runtime the issuer never queries the candidate repository to re-establish this evidence.

### `runtime-manifest.json`

```json
{
  "kind": "R4_HERMETIC_PYTHON311_RUNTIME_V1",
  "python_version": "3.11.<approved-patch>",
  "entrypoint": "closure/python311/bin/python3.11",
  "artifact_source": "OFFLINE_PINNED_ARTIFACT",
  "artifact_sha256": "<64 hex>",
  "entries_sha256": "<64 hex>",
  "mach_o_dependencies": [
    {"loader": "closure/python311/bin/python3.11", "dependency": "/usr/lib/libSystem.B.dylib", "resolved": "/usr/lib/libSystem.B.dylib"}
  ],
  "sha256": "<64 hex>"
}
```

The Python 3.11 runtime, all wheels, extension modules, scripts, CA data used by installed packages, and interpreter support files are acquired from an offline, content-addressed artifact already pinned by the Task 14 approval. Provisioning performs no package resolution, compilation, `pip install`, `brew`, download, subprocess dependency acquisition, or network access. The runtime's full closure must be in `closure/python311` except Mach-O dependencies resolved to `/usr/lib/...` or `/System/...`.

For every Mach-O executable, dylib, extension, framework binary, and the Swift provisioner itself, the provisioner resolves `LC_LOAD_DYLIB`, `LC_LOAD_WEAK_DYLIB`, `LC_REEXPORT_DYLIB`, `@loader_path`, `@executable_path`, and `@rpath` against the held copied object and its recorded `LC_RPATH` closure. A resolved dependency is accepted only when its canonical path begins with `/usr/lib/`, `/System/`, or the canonical protected authority root. A missing, ambiguous, symlinked, candidate-owned, Homebrew, user-home, developer-toolchain, network-mounted, or other external dependency rejects the entire attempt. Dynamic-loader environment variables are cleared and rejected; the signed receipt records that fact. This prevents any dynamic link back to the candidate's Homebrew paths.

### `task14-approval.json`

```json
{
  "kind": "R4_TASK14_PROTECTED_EXECUTION_APPROVAL_V1",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "reviewed_head": "<40 hex>",
  "reviewed_tree": "<40 hex>",
  "candidate_manifest_sha256": "<64 hex>",
  "runtime_manifest_sha256": "<64 hex>",
  "provisioner_swift_source_sha256": "<64 hex>",
  "provisioner_binary_sha256": "<64 hex>",
  "provisioner_designated_requirement_sha256": "<64 hex>",
  "helper_hashes": [
    {"path": "bootstrap/provisioner", "sha256": "<64 hex>"},
    {"path": "bootstrap/provisioner.codesign-requirement.txt", "sha256": "<64 hex>"}
  ],
  "required_blob_set_sha256": "<64 hex>",
  "exact_suite_log_sha256": "<64 hex>",
  "sha256": "<64 hex>"
}
```

Task 14 must populate every placeholder with a concrete exact value and reject an omitted, extra, reordered, duplicate, or mismatched helper hash. The approval binds the manifest and helper identities rather than merely reporting them. A review record without exact manifest and helper hashes cannot be installed or provisioned.

### `receipt.json` and detached signature

```json
{
  "kind": "R4_PROTECTED_EXECUTION_RECEIPT_V1",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "state": "SEALED",
  "manifest_sha256": "<64 hex>",
  "runtime_manifest_sha256": "<64 hex>",
  "task14_approval_sha256": "<64 hex>",
  "git_head": "<40 hex>",
  "git_tree": "<40 hex>",
  "required_blob_set_sha256": "<64 hex>",
  "provisioner_binary_sha256": "<64 hex>",
  "public_key_sha256": "<64 hex>",
  "published_closure_sha256": "<64 hex>",
  "dependency_closure_sha256": "<64 hex>",
  "receipt_sha256": "<64 hex>"
}
```

`receipt.ed25519` is a 64-byte Ed25519 signature of the exact canonical `receipt.json` bytes. Verification first pins the public key from `bootstrap/public.ed25519` through `bootstrap` policy, verifies the receipt signature, then revalidates the published closure and each referenced manifest. It never accepts a key or signature from the candidate checkout.

### Keyring and revocation records

`control/keyring/<eight-digit-generation>/` contains one closed keyring record, its detached prior-key signature, the new public key, and the new root-only private key. `control/revocations/<authority_id>/<eight-digit-sequence>.json` and its sibling `.ed25519` are closed, signed, immutable records. A keyring record contains the namespace, monotonically increasing generation, old/new public-key SHA-256 values, and canonical digest; its detached signature is made by the preceding key. A revocation record is `R4_PROTECTED_EXECUTION_REVOCATION_V1` and contains namespace, authority ID, key generation, monotonically increasing sequence, state (`ACTIVE` or `REVOKED`), a closed reason code, and canonical digest; its detached signature is made by the current key. The effective state is the final contiguous, signature-valid sequence from protected control records; a missing, duplicate, reordered, or invalid record fails closed. Every generation directory and record is root:wheel, has the mode prescribed by its type, and is system immutable after publication. Normal provisioning and the issuer have no write path to `control`.

## Provisioning Protocol and State Machine

The only privileged interface is one reviewed root-executed fixed binary with three closed subcommands:

```text
<reviewed-candidate-binary> bootstrap-install \
  --approval-envelope <absolute-file> \
  --expected-approval-sha256 <64-lowercase-hex>
bootstrap/provisioner provision --candidate-root <absolute-directory>
bootstrap/provisioner verify --authority-id r4-disjoint-custody-v2-execution-v1
```

`bootstrap-install` is available only while the fixed bootstrap root is absent. It is the explicit administrator trust ceremony: the administrator supplies the already reviewed Task 14 envelope and separately confirms its exact SHA-256 on the command line. The binary first hashes the held envelope and requires the independently supplied digest, then verifies that the envelope's Swift-source hash, binary hash, designated requirement, namespace, and fixed authority ID match its own held executable and code signature. Only that successful comparison permits it to create root-only install staging, generate the first key, and publish bootstrap. It refuses if invoked from the installed path, if bootstrap or control state already exists, or if any unexpected install-root entry exists. The expected digest is the one human trust input to bootstrap; it cannot select a namespace, authority ID, output root, key path, or candidate manifest, and it is never reused by `provision`.

`provision` is accepted only from the already installed and revalidated `bootstrap/provisioner`. It accepts exactly one absolute candidate directory and derives all candidate filenames from the protected bootstrap policy. It accepts no manifest path, output path, key path, runtime path, Git option, `--force`, `--resume`, `--adopt`, `--repair`, `--clean`, `--overwrite`, `--network`, or alternate authority ID. It closes standard input, uses a fixed sanitized environment, disables Git hooks/configuration if Git evidence is locally rechecked, and makes no network syscall by policy and test. `verify` accepts only the fixed authority ID shown above. Any other option, subcommand, spelling, or positional argument fails before a filesystem mutation.

| State | Entry condition | Allowed next state | Required behavior |
| --- | --- | --- | --- |
| `ABSENT` | No published authority or quarantine for fixed ID | `VALIDATING` | First provision only. |
| `VALIDATING` | Root holds verified helper and bootstrap descriptors | `STAGING` or `QUARANTINED` | Read candidate no-follow; validate Task 14 envelope, exact manifest, Git evidence, and offline runtime artifact. |
| `STAGING` | Fresh root-owned random staging sibling | `SEALED` or `QUARANTINED` | Descriptor-relative copy, fsync, full manifest/dependency validation, construct and sign the final receipt inside staging, then apply ownership/mode/system-immutable sealing to the complete closure including receipt and signature. |
| `SEALED` | Complete staging closure, receipt, and signature revalidate while sealed | `PUBLISHED` or `QUARANTINED` | Atomic no-replace rename, directory fsync, and full published revalidation without adding or modifying a file. |
| `PUBLISHED` | Receipt and signature pass protected verification | terminal `ACTIVE` | Read-only verify and execution only. |
| `QUARANTINED` | Any error, signal, validation mismatch, or post-copy drift | terminal | Retain evidence; no retry with this ID. |
| `REVOKED` | Root revocation record says `REVOKED` | terminal | Execution binding rejects it. |

Before the first candidate read, the provisioner requires `ABSENT`. It creates a staging directory with a system-generated UUID only under a root-owned private staging parent, mode `0700`, and writes no candidate-controlled filename outside it. It opens every candidate component by retained directory descriptor with `O_NOFOLLOW|O_CLOEXEC`; it copies regular files with descriptor-to-descriptor reads, hashes held descriptors, fsyncs the new descriptor, closes and reopens via the root-owned parent, and compares type/mode/size/hash/identity to the canonical manifest. After every closure byte is final, it constructs the receipt from held staging descriptors, signs it, writes and fsyncs the receipt and signature, and then seals and revalidates the complete staging tree. Publication never needs to write inside the sealed tree. It uses `renameatx_np(..., RENAME_EXCL)` or an equivalently proven no-replace atomic primitive; if unavailable it rejects rather than emulate with a time-of-check/time-of-use sequence. Parent directories are fsynced after every metadata transition and final rename.

No action deletes or clears flags from failed state. Before publication, failure moves the owned staging directory into the root-owned `quarantine/<authority_id>.<attempt-uuid>` by descriptor-relative atomic rename and fsync; if that move itself cannot be proven, the unmodified staging directory is still treated as quarantine and provisioning returns failure. After publication, any failure seals the partial published state as non-executable and writes no replacement. The fixed ID is permanently non-provisionable once an attempt exists.

## Protected Execution Binding

`capture_execution_binding` is replaced for the R4 protected path. It has no repository or Python-path parameters and reads only held descriptors under the protected authority root:

```python
def capture_execution_binding() -> ProtectedExecutionBinding: ...

@dataclass(frozen=True)
class ProtectedExecutionBinding:
    namespace: str
    authority_id: str
    receipt_sha256: str
    manifest_sha256: str
    runtime_manifest_sha256: str
    git_head: str
    git_tree: str
    required_blob_set_sha256: str
    repository_clean: bool
    protected_python_sha256: str
    protected_python_identity: ProtectedFileIdentity
    public_key_sha256: str
```

For this binding only, `repository_clean` is redefined as: **the exact sealed tracked closure named by the verified protected manifest equals the receipt-bound manifest, every entry has its manifest path/type/mode/size/SHA-256, no extra entry exists, all protected ownership/flag checks pass, the receipt signature and active non-revoked key verify, and Git HEAD/tree/required-blob evidence equals the Task 14 approval.** It does not mean `git status` of any live worktree is empty.

The R4 issuer invokes only `closure/python311/bin/python3.11` by a protected descriptor/rebound path and imports only from `closure/checkout` and `closure/python311`. It rejects any environment mutation, current-working-directory import, user-site package, external configuration, or dependency that would escape those roots. The binding is checked before issuer startup and immediately before authority-sensitive R4 issuance actions. A receipt or manifest mismatch fails closed.

## Implementation Boundaries

The implementation is split so that Python cannot become a privileged copier and Swift does not encode R4 business logic:

| Path | Responsibility |
| --- | --- |
| `tools/r4_protected_execution_authority/Package.swift` | Pinned Swift package manifest; no remote package dependencies. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/main.swift` | Root-only bootstrap verification, candidate descriptor traversal/copy, manifest/runtime validation, sealing, receipt signing, quarantine, and protected verification. |
| `schemas/r4_protected_execution_manifest.schema.json` | Closed authority-manifest schema. |
| `schemas/r4_hermetic_python311_runtime.schema.json` | Closed offline runtime/dependency schema. |
| `schemas/r4_task14_protected_execution_approval.schema.json` | Closed exact Task 14 envelope schema. |
| `schemas/r4_protected_execution_receipt.schema.json` | Closed signed receipt schema. |
| `schemas/r4_protected_execution_keyring.schema.json` | Closed key rotation and revocation-record schemas. |
| `mlb_backtest/v52_r4_protected_execution.py` | Unprivileged protected-descriptor verifier and `capture_execution_binding()` adapter; it has no candidate-copy, key-generation, or root-write capability. |
| `mlb_backtest/v52_r4_disjoint_issuer.py` | Selects the protected adapter only for the fixed R4 protected authority branch. |
| `tests/test_r4_protected_execution_authority.py` | Disposable-root state, receipt, manifest, runtime, and confinement tests. |
| `tests/test_v52_r4_protected_execution.py` | Python adapter and issuer integration tests. |

The Swift package uses only Apple system frameworks and locally vendored source captured by the approved helper digest; `swift package resolve`, external package plugins, and build-time network access are prohibited. The binary has exactly the one-time `bootstrap-install` ceremony and the installed-only `provision` and `verify` subcommands defined above. The Python adapter never spawns the provisioner, and no normal R4 command acquires root privileges.

## Key Rotation and Authority Revocation

Key rotation is a new root-authorized administrative ceremony, not a provisioner flag. The immutable bootstrap public key remains the root of the verification chain; it is never rewritten. The ceremony verifies the currently active protected key, generates a replacement root-only key in `control/keyring`, writes a new immutable rotation record signed by the preceding key with old/new public-key fingerprints and a monotonic generation, and retains old public keys solely for historical receipt verification. It requires a new reviewed installer/helper digest if the provisioner changes. Rotation never changes a receipt or turns a failed attempt into an active authority.

Revocation appends a root-signed immutable revocation record to `control/revocations/<authority_id>` with a closed reason code such as `KEY_COMPROMISE`, `HELPER_COMPROMISE`, `MANIFEST_COMPROMISE`, or `PLATFORM_ASSUMPTION_INVALID`. A revoked authority cannot execute, verify as active, be unrevoked, or be overwritten. Replacement requires a new approved authority ID and a new Task 14 approval. Root must retain all receipts, manifests, keyring, and revocation records for audit; no cleanup is implicit.

## Required Tests

| Area | Required cases |
| --- | --- |
| Bootstrap | Reject changed Swift source/binary digest, ad-hoc/wrong designated requirement, candidate key, unpinned public key, wrong root ownership/mode/flag, and bootstrap/public-key mismatch. |
| Canonical manifest | Reject path escape, Unicode/case collision, duplicate/reordered rows, extra/missing entry, symlink, special file, hard link, type/mode/size/hash drift, or descriptor/name swap before/during/after copy. |
| Git evidence | In disposable repositories, prove exact HEAD/tree/required blob values and reject hooks/config, detached/non-commit HEAD, dirty/index/untracked state during Task 14 construction, and any candidate claim that differs from the approved envelope. |
| Hermetic runtime | Reject dynamic dependency resolving to any user, Homebrew, developer, network, relative, ambiguous, missing, or candidate path; reject `DYLD_*`, `PYTHON*`, user-site import, runtime download, and non-pinned artifact. Prove accepted dependencies resolve only to `/usr/lib`, `/System`, or the protected root. |
| State/failure | Inject faults before/after each fsync, rebind, flag change, rename, receipt sign, and receipt seal. Assert quarantine/no success, no deletion, no same-ID retry, no resume/adopt/overwrite, and no active binding. |
| Receipt/binding | Reject candidate signature, invalid receipt, wrong public key, revoked key/authority, changed protected Python, changed closure object, extra protected object, receipt/manifest mismatch, and live untrusted repository changes after provisioning. |
| macOS controls | Test APFS/system-flag/readback failures, ACLs, code-signing failure, unavailable no-replace rename, and unsupported dynamic-loader resolution as hard failures. |
| Integration | Prove legacy Task 3 binding remains unchanged outside the protected R4 branch; prove protected R4 execution reads only protected descriptors and never touches the linked worktree, Homebrew runtime, production custody paths, or network. |

All tests use disposable roots and test keys. Tests must record filesystem and process-launch calls and reject any target outside fixture roots except read-only system dependency inspection.

## Migration Impact

### Task 3

Task 3's current `capture_execution_binding(repository, executable, ...)` is not sufficient for protected execution because it binds a user-owned worktree and executable. Preserve it for legacy/non-protected R4 compatibility. Add `ProtectedExecutionBinding` and a separate protected capture path; do not silently reinterpret legacy `repository_clean`. The protected issuer branch must select the new zero-argument capture function only when the fixed protected authority ID is required.

### Tasks 14–15

Task 14 gains a mandatory protected-execution gate before any rollout approval: build the pre-expanded candidate with no network, emit and independently verify the exact canonical manifest/runtime manifest, record exact Git evidence, and record exact reviewed helper source/binary/designated-requirement hashes in `task14-approval.json`. Independent reviews must inspect the Swift source/binary correspondence, full dependency resolver, state machine, bootstrap pinning, revocation behavior, and no-follow/rebind tests. Task 14 remains insufficient if it merely reports checksums without installing the reviewed protected helper.

Task 15 remains separately authorized. A production rollout approval must name both the reviewed candidate commit and the protected receipt SHA-256/public-key fingerprint. It must first verify the active, non-revoked protected authority in a fresh process. It must not invoke provisioning, issuance, or any custody action merely because Task 14 passed. A changed candidate HEAD, manifest, helper, protected key, receipt, or revocation state invalidates rollout approval and requires a new Task 14 review plus a newly authorized authority ID where applicable.

## Rejected Alternatives

| Alternative | Rejection reason |
| --- | --- |
| Best-effort binding to linked Git checkout | A clean status or Git hash does not prevent post-check replacement, hooks/configuration influence, user ownership, or dynamic imports from user paths. |
| Dulwich/libgit2 in the issuer | It reduces subprocess surface but leaves the checkout and interpreter untrusted; it does not solve the root-owned sealed-execution boundary. |
| Persistent privileged daemon | Adds a long-lived attack surface, request authentication/state recovery complexity, and network/IPC authority not needed for one-shot provisioning. |
| Manual service account | A service account still consumes user-controlled executables and paths unless it becomes a protected installer in practice; it provides weaker ownership and audit semantics than root:wheel sealing. |
| Candidate-generated signing key or candidate signature | Lets the untrusted candidate mint the very authority it is meant to be constrained by. |
| Homebrew Python or on-demand `pip` runtime | Its files, dependency graph, and loader paths are user/toolchain mutable and can dynamically link outside the reviewed closure. |
| Best-effort immutable modes/flags | chmod or user immutable flags are not a root-owned system seal; inability to establish the required immutable state must stop the attempt. |

## Completion Criteria

Implementation is ready for separate review only when it has a root-owned Swift provisioner with no daemon/network surface, all schemas are closed and exact, Task 14 records concrete exact manifest/helper values, every accepted closure object is descriptor-validated and sealed, receipts verify against a root-pinned public key, revocation/rotation are explicit, no failure admits retry for the same ID, and the complete test matrix passes. Production custody rollout remains outside this design and requires its own explicit approval.
