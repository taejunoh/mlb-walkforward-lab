# R4 Privileged Runtime Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Python a V2 sealed-input-bundle producer only, and make the installed root-owned Swift helper the sole V2 runtime materializer, without performing production bootstrap, materialization, or custody work in this plan.

**Architecture:** The implementation is a single V2 evidence graph. Python verifies copied lock/download/Git bytes and writes only a user-owned transport bundle plus a Task 14 **review package**; it never creates an approval. A separately authorized root-only no-child observation ceremony uses the candidate helper to create the signed final approval before bootstrap. Swift bootstrap pins Key A against a compile-time source constant before it accepts that approval, seals the accepted public material, and only then generates distinct Key B generation 1. The installed helper validates a typed `ValidatedBundle`, materializes only in a root-owned private stage, launches only the pinned `/usr/bin/sandbox-exec` with the final held interpreter as its operand, measures observed closures, signs/seals/publishes once, and read-only verifies the result. Production adapters are constructed only by the installed helper, while ordinary tests use fixture-root adapters.

**Tech Stack:** Swift 5.9 (`Foundation`, `Darwin`, `Security`, `CryptoKit`), Python 3.11 standard library, JSON Schema Draft 2020-12, XCTest, `unittest`, Ruff, macOS APFS/FileProvider APIs, Seatbelt, `posix_spawn`, Git plumbing.

---

## Non-negotiable authority boundary

- Work only below a disposable test root returned by `TemporaryDirectory()` or `DarwinDisposableRoot.create(prefix: "r4-materialization-")`. Tests and ordinary commands must never create, open, modify, delete, chmod, chflags, or traverse production `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`, any other production `/Library` path, or any custody root. Every test root has `defer { try? root.removeOnlySelfOwnedFixture() }` immediately after creation.
- The fixed namespace, authority ID, and production root are `org.mlb-walkforward.r4.protected-execution-authority/v1`, `r4-disjoint-custody-v2-execution-v1`, and `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`. No option, environment, cwd, caller path, helper path, lock path, staging path, package list, shell, IPC request, or dynamic-loader variable alters them.
- Production capture is not part of ordinary tests: at a later separately approved ceremony it reads only the fixed protected root using read-only V2 descriptor validation. It never reads the worktree, Homebrew runtime, bundle, or custody data.
- Python Task2a has no process or executable-artifact capability. The only Task2 subprocess call is `scripts/r4_protected_fixed_git.py:run_recorded_git`; Task2b and Task2c can reach it only through its recorded fixed `/usr/bin/git` grammar in Task 5. They never run `codesign`, an interpreter, `pip`, `otool`, archive extractor, shell, compiler, downloader, helper, or network client.
- The helper is not setuid/setgid and exposes no daemon, XPC, listener, socket, request protocol, or IPC endpoint. The caller's normal OS root authorization is outside the helper; `geteuid() == 0` is required before bundle access and real UID is not an authorization input.
- There is no resume/adopt/repair/overwrite/cleanup command. A post-admission failure has terminal forensic state; no user-owned input or pre-existing authority/quarantine/custody object is a cleanup target.

## Exact file map and ownership

| Path | Action | Owner/responsibility |
| --- | --- | --- |
| `schemas/r4_runtime_input_bundle_v2.schema.json` | Create | Closed bundle object. |
| `schemas/r4_privileged_materialization_policy_v2.schema.json` | Create | Closed policy/Seatbelt/child-command object. |
| `schemas/r4_privileged_materialization_admission_v2.schema.json` | Create | Acyclic durable admission object. |
| `schemas/r4_{hermetic_python311_runtime,protected_execution_manifest,task14_protected_execution_approval,protected_execution_receipt,protected_execution_keyring}.schema.json` | Modify | Replace materialization V1 forms with closed V2 forms; retain only closed Key-B keyring/revocation record branches. |
| `schemas/r4_task14_privileged_materialization_{review_package,observation_authorization}_v2.schema.json` | Create | Closed Key-A review-package and one-use observation-authorization objects. |
| `config/r4_privileged_materialization_policy.json` | Create | Canonical reviewed V2 policy bytes. |
| `scripts/verify_r4_protected_inputs.py` | Create | Task2a descriptor/hash-only verification. |
| `scripts/prepare_r4_protected_runtime.py` | Modify | Error-only deprecation wrapper; no runtime code. |
| `scripts/r4_protected_fixed_git.py` | Create | The only Task2 subprocess adapter: validate and run the fixed, sterile local Git evidence grammar. |
| `scripts/build_r4_protected_candidate.py` | Modify | Task2b V2 bundle-only builder. |
| `scripts/build_r4_task14_approval.py` | Modify | Task2c Git-only V2 review-package builder/verifier; it has no final-approval or signing path. |
| `tests/test_prepare_r4_protected_runtime.py`, `tests/test_build_r4_protected_candidate.py`, `tests/test_r4_protected_fixed_git.py` | Modify/create | Python AST boundary, exact call recording, runner failure cleanup, and V2 bundle/approval regressions. |
| `tools/r4_protected_execution_authority/Package.swift` | Modify | Executable plus library, no remote dependencies. |
| `Sources/R4ProtectedExecutionProvisioner/{Constants,CanonicalJSON,Models,POSIX,Adapters,CLI,Bootstrap,Storage,Admission,BundleTraversal,StateMachine,Archive,MachO,ProcessRunner,Materialize,Keyring,Verify,main}.swift` | Create/modify | One file per privileged capability; `Constants.swift` owns the compile-time Key-A SHA-256 and no caller supplies an expected-key hash. |
| `Tests/R4ProtectedExecutionProvisionerTests/{CanonicalJSON,ModelsV2,Bootstrap,Fakes,CLI,Storage,BundleTraversal,StateMachine,Admission,Archive,MachO,ProcessRunner,Materialize,Keyring,Verify,DarwinConformance}Tests.swift` | Create/modify | Fake-root tests first; real-Darwin disposable tests separately. |
| `mlb_backtest/v52_r4_protected_execution.py`, `mlb_backtest/v52_r4_disjoint_issuer.py` | Create/modify | Read-only V2 capture and fixed branch only. |
| `tests/test_r4_protected_execution_authority.py`, `tests/test_v52_r4_protected_execution.py` | Create/modify | Cross-language state and legacy isolation. |
| `scripts/run_task13_exact_suite.py`, `tests/test_task13_exact_suite_runner.py` | Modify | Stable exact suite registration. |

## Declared interfaces and fixtures (defined before use)

```swift
enum ProvisionError: Error, Equatable {
    case rootPrivilegeRequired, loadedHelperIdentityMismatch, storageRejected
    case approvalReviewPackageInvalid, approvalObservationAuthorizationInvalid
    case approvalPreimageObservationFailed, approvalVerificationKeyInvalid, approvalSignatureInvalid
    case keyRoleConfusion, sandboxLauncherMismatch, interpreterIdentityMismatch
    case bundleArgumentInvalid, bundleManifestInvalid, bundleApprovalMismatch
    case bundleDescriptorRebindFailed, lockArtifactMismatch, admissionCreateFailed
    case admissionFsyncFailed, privateStageInvalid, archiveUnsafe, extractedRuntimeRenameFailed
    case baseRuntimeLoaderRejected, sandboxUnavailable, pipExitNonzero, processTimeout
    case processOutputLimit, importConfinementFailed, postinstallClosureRejected
    case codesignIdentityMismatch, sealReadbackFailed, publishExclusiveUnavailable, priorAttemptExists
}

struct ValidatedBundle: Equatable {
    fileprivate let heldRoot: DirectoryHandle
    fileprivate let manifest: InputBundleManifest
    fileprivate let lock: LockedArtifacts
    fileprivate let policy: MaterializationPolicy
    fileprivate let approval: Task14ApprovalV2
}

// `Constants.swift` defines `task14ApprovalVerificationPublicKeySHA256` as the
// reviewed literal lowercase 64-hex hash of the externally held Key-A public
// bytes. It is neither an initializer parameter nor a caller-provided value.

protocol FileSystem {
    func openDirectory(_ parent: DirectoryHandle?, _ name: String, flags: OpenFlags) throws -> DirectoryHandle
    func openRegular(_ parent: DirectoryHandle, _ name: String, flags: OpenFlags) throws -> RegularHandle
    func createExclusive(_ parent: DirectoryHandle, _ name: String, mode: UInt16) throws -> RegularHandle
    func renameExclusive(_ from: DirectoryHandle, _ name: String, _ to: DirectoryHandle, _ destination: String) throws
    func fsync(_ handle: any DescriptorHandle) throws
    func setAndReadBackImmutable(_ handle: any DescriptorHandle) throws
}

enum MaterializationState: String, Codable {
    case absent = "ABSENT", bundleValidating = "BUNDLE_VALIDATING", admitted = "ADMITTED"
    case privateCopy = "PRIVATE_COPY", extracting = "EXTRACTING", installing = "INSTALLING"
    case postinstallValidating = "POSTINSTALL_VALIDATING", sealed = "SEALED"
    case published = "PUBLISHED", quarantined = "QUARANTINED"
}

protocol DurableStateStore {
    var currentState: MaterializationState { get }
    func persist(_ state: MaterializationState, admission: Admission) throws
    func fsyncAndRebind(_ state: MaterializationState) throws
    func quarantineAfterFailure(_ admission: Admission) throws
}

struct StateMachine {
    static func transition(to: MaterializationState, using store: DurableStateStore,
                           admission: Admission) throws
}

enum V2Fixtures { static func complete() -> V2FixtureSet }
struct BootstrapFixture {
    static func make() -> BootstrapFixture
    func removeOnlySelfOwnedFixture() throws
    let root: URL
    let events: [BootstrapEvent]
}
```

`ValidatedBundle` has no public initializer. Only the `BundleVerifier.verifyBeforeAdmission` method creates it after all held-descriptor checks; `AdmissionController.admit(_ bundle: ValidatedBundle, storage: Storage)` accepts no path, manifest, approval, or caller-provided digest. `Fakes.swift` is created in Task 3, before any test uses `fakeFS`, `fakeProcess`, `validArchive`, or mutation helpers.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence
import os
import selectors
import signal
import subprocess
import time

GIT_EXECUTABLE = "/usr/bin/git"
GIT_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                   "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file"}
GIT_PREFIX = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c",
              "core.untrackedCache=false", "-c", "core.attributesfile=/dev/null", "-c",
              "diff.external=false", "-c", "protocol.file.allow=always", "--no-replace-objects",
              "--no-lazy-fetch")
GIT_ALLOWED_ARGUMENTS = (("symbolic-ref", "-q", "HEAD"),
                         ("status", "--porcelain=v1", "--ignored", "-z"),
                         ("rev-parse", "--verify", "HEAD^{commit}"),
                         ("rev-parse", "--verify", "HEAD^{tree}"),
                         ("ls-tree", "-r", "-z", "--full-tree", "HEAD"))

@dataclass(frozen=True)
class RecordedGitCall:
    executable: str
    argv: Sequence[str]
    environment: dict[str, str]
    stdin: str
    timeout_seconds: int
    output_cap: int

GitRunner = Callable[[RecordedGitCall], bytes]

def run_fixed_git(source: Path, arguments: Sequence[str], *,
                  runner: GitRunner) -> bytes:
    normalized_source = str(source.resolve(strict=True))
    call = RecordedGitCall(GIT_EXECUTABLE,
                           GIT_PREFIX + ("-C", normalized_source) + tuple(arguments),
                           GIT_ENVIRONMENT, "DEVNULL", 30, 4 * 1024 * 1024)
    return runner(call)

# scripts/r4_protected_fixed_git.py owns this implementation and is the only
# Task2 module allowed to import subprocess.
def run_recorded_git(call: RecordedGitCall) -> bytes:
    _validate_recorded_git_call(call)
    process = subprocess.Popen((call.executable, *call.argv), cwd="/", env=dict(GIT_ENVIRONMENT),
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, close_fds=True, start_new_session=True)
    try:
        stdout, _stderr = _read_capped_pipes(process, call.timeout_seconds, call.output_cap)
        if process.wait(timeout=1) != 0:
            raise ValueError("fixed Git evidence command failed")
        return stdout
    except BaseException:
        _terminate_and_reap_process_group(process)
        raise

def _validate_recorded_git_call(call: RecordedGitCall) -> None:
    if call.executable != GIT_EXECUTABLE or call.environment != GIT_ENVIRONMENT:
        raise ValueError("Git executable or environment is not fixed")
    if call.stdin != "DEVNULL" or call.timeout_seconds != 30 or call.output_cap != 4 * 1024 * 1024:
        raise ValueError("Git stdin, timeout, or output cap is not fixed")
    prefix_size = len(GIT_PREFIX)
    if len(call.argv) < prefix_size + 3:
        raise ValueError("Git argv is incomplete")
    if tuple(call.argv[:prefix_size]) != GIT_PREFIX or tuple(call.argv[prefix_size:prefix_size + 1]) != ("-C",):
        raise ValueError("Git argv prefix is not fixed")
    source = Path(call.argv[prefix_size + 1]).resolve(strict=True)
    if not source.is_dir() or call.argv[prefix_size + 1] != str(source):
        raise ValueError("Git source is not a normalized directory")
    arguments = tuple(call.argv[prefix_size + 2:])
    cat_file = len(arguments) == 3 and arguments[0:2] == ("cat-file", "blob") and len(arguments[2]) == 40 and set(arguments[2]) <= set("0123456789abcdef")
    if arguments not in GIT_ALLOWED_ARGUMENTS and not cat_file:
        raise ValueError("Git arguments are outside the evidence allowlist")

def _read_capped_pipes(process: subprocess.Popen[bytes], timeout_seconds: int,
                        output_cap: int) -> tuple[bytes, bytes]:
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + timeout_seconds
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("fixed Git command timed out")
            for key, _ in selector.select(remaining):
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                captured[key.data].extend(block)
                if len(captured["stdout"]) + len(captured["stderr"]) > output_cap:
                    raise ValueError("fixed Git command exceeded output cap")
        return bytes(captured["stdout"]), bytes(captured["stderr"])
    finally:
        selector.close()

def _terminate_and_reap_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)

def build_input_bundle(source: Path, downloads: Path, lock_path: Path, policy_path: Path,
                       output: Path, runner: GitRunner = run_recorded_git) -> InputBundle:
    evidence = read_exhaustive_git_evidence(source, runner)
    artifacts = verify_download_set(lock_path, downloads)
    return write_bundle(output, evidence, artifacts, read_canonical(lock_path), read_canonical(policy_path))

def build_task14_approval_v2(repository: Path, bundle: Path, helper: Path, exact_suite_log: Path,
                              output: Path, runner: GitRunner = run_recorded_git) -> dict[str, object]:
    evidence = read_fixed_six_git_evidence(repository, runner)
    held_bundle = read_validated_bundle(bundle)
    return write_approval_v2(output, evidence, held_bundle, helper, exact_suite_log)
```

## Completed baseline boundary for Tasks 1–7

Tasks 1–7 below record the pre-amendment implementation sequence and must not be re-run as RED/GREEN work at current HEAD: their named files and several baseline APIs already exist. They remain evidence of completed history only. Execute only the explicitly labeled migration tasks 1A, 2A, 3A, 4A, 5A, and 7A; each RED command asserts a newly required symbol, field, vector, or behavior that is absent at current `865d67b` before its implementation step. Migration tasks preserve fixture-root confinement and do not treat checkpoint `654b79c..7d95ba0` as proof of the approved V2 graph.

## Task 1: Completed baseline/history — original pure V2 schemas and decoders

**Files:** create the three V2 schema files; modify the five protected schemas, `CanonicalJSON.swift`, and `Models.swift`; create `ModelsV2Tests.swift`.

The normative digest functions are byte-exact and use `SHA256(domain || canonical_bytes(value))`:

| Digest | Exact domain and canonical value |
| --- | --- |
| directory row | `SHA256("R4DIR\0" || canonical_bytes(immediate_child_rows))`, where each row is exactly `name,type,mode,size,sha256` in strict name order. |
| wheel set | `SHA256("R4V2_WHEEL_SET\0" || canonical_bytes(lock_order_wheel_rows))`, each row exactly `filename,version,url,sha256,size`. |
| runtime entries | `SHA256("R4V2_RUNTIME_ENTRIES\0" || canonical_bytes(entries))`, each entry exactly `path,type,mode,size,sha256`. |
| dependency closure | `SHA256("R4V2_RUNTIME_DEPENDENCIES\0" || canonical_bytes(mach_o_dependencies))`, each row exactly `loader,dependency,resolved`. |
| installer command | `SHA256("R4V2_INSTALL_COMMAND\0" || canonical_bytes({program,argv,cwd,environment,stdin,stdout_stderr,timeout_seconds,max_output_bytes,sandbox_launcher_descriptor_identity_sha256,sandbox_launcher_sha256,sandbox_launcher_codesign_identity_sha256,interpreter_sha256,interpreter_mach_o_sha256,interpreter_codesign_identity_sha256,sandbox_profile_sha256}))`. |
| environment | `SHA256("R4V2_ENVIRONMENT\0" || canonical_bytes(environment))`, where environment is the exact ordered nine-string array in Task 8. |
| required blobs | `SHA256("R4V2_REQUIRED_BLOBS\0" || canonical_bytes(required_blobs))`, each row exactly `path,blob,sha256`. |
| published closure | `SHA256("R4V2_PUBLISHED_CLOSURE\0" || canonical_bytes(published_tree_rows_excluding_receipt_and_signature))`, with the same exact entry row fields. |

The V2 fixture computes every expected value independently with the literal domains above; no production-looking digest is hard-coded.

- [ ] **Step 1: Write pure decoder RED tests (2 minutes).** Do not introduce filesystem adapters, aggregate evidence wrappers, or any stage mutation in this task.

```swift
func testEachV2DecoderRejectsItsOwnV1Kind() throws {
    XCTAssertThrowsError(try InputBundleManifest.decode(v1InputBundle))
    XCTAssertThrowsError(try MaterializationPolicy.decode(v1Policy))
    XCTAssertThrowsError(try AdmissionRecord.decode(v1Admission))
    XCTAssertThrowsError(try RuntimeManifestV2.decode(v1RuntimeManifest))
    XCTAssertThrowsError(try AuthorityManifestV2.decode(v1AuthorityManifest))
    XCTAssertThrowsError(try ReceiptV2.decode(v1Receipt))
    XCTAssertThrowsError(try Task14ApprovalV2.decode(v1Approval))
    XCTAssertThrowsError(try Task14ApprovalV2.decode(mixedV1V2Approval))
}
func testSixV2FixturesRoundTripAndCrossHash() throws {
    let f = V2Fixtures.complete()
    XCTAssertEqual(try InputBundleManifest.decode(f.bundle).sha256, f.approval.inputBundleManifestSHA256)
    XCTAssertEqual(try RuntimeManifestV2.decode(f.runtime).sha256, f.authority.runtimeManifestSHA256)
    XCTAssertEqual(f.receipt.manifestSHA256, f.authority.sha256)
}
func testNormativeDigestFixtureUsesLiteralDomains() throws {
    let f = V2Fixtures.complete()
    XCTAssertEqual(f.directorySHA256, sha256("R4DIR\0".utf8 + canonical(f.immediateChildRows)))
    XCTAssertEqual(f.wheelSetSHA256, sha256("R4V2_WHEEL_SET\0".utf8 + canonical(f.wheelRows)))
    XCTAssertEqual(f.runtimeEntriesSHA256, sha256("R4V2_RUNTIME_ENTRIES\0".utf8 + canonical(f.runtimeEntries)))
    XCTAssertEqual(f.dependencyClosureSHA256, sha256("R4V2_RUNTIME_DEPENDENCIES\0".utf8 + canonical(f.dependencies)))
    XCTAssertEqual(f.installerCommandSHA256, sha256("R4V2_INSTALL_COMMAND\0".utf8 + canonical(f.installerCommand)))
    XCTAssertEqual(f.environmentSHA256, sha256("R4V2_ENVIRONMENT\0".utf8 + canonical(f.environment)))
    XCTAssertEqual(f.requiredBlobSetSHA256, sha256("R4V2_REQUIRED_BLOBS\0".utf8 + canonical(f.requiredBlobs)))
    XCTAssertEqual(f.publishedClosureSHA256, sha256("R4V2_PUBLISHED_CLOSURE\0".utf8 + canonical(f.publishedRows)))
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests`

Historical baseline: do not run this RED command at current HEAD; Task 1A supplies the truthful migration RED command.

- [ ] **Step 3: Implement canonical types and the six serialization/cross-hash fixtures (5 minutes).** Implement exact closed decoders for input bundle, policy, admission, runtime, authority, and receipt. Reject duplicate/unknown/missing keys, newline, float/exponent, non-ASCII path, unordered/duplicate/case-folding row, bad lower-case SHA/Git hash, and every V1 kind. `V2Fixtures.complete()` serializes all six canonical documents and asserts: bundle↔approval↔admission↔runtime↔authority↔receipt input/policy/lock/wheel/Git/runtime/authority/admission equalities; runtime entries/output closure equality; dependency, profile, environment, and command domain digests; and the acyclic ordering.

```swift
static func crossValidate(_ b: InputBundleManifest, _ p: MaterializationPolicy,
                          _ a: Task14ApprovalV2, _ d: AdmissionRecord,
                          _ r: RuntimeManifestV2, _ m: AuthorityManifestV2,
                          _ receipt: ReceiptV2) throws {
    guard a.inputBundleManifestSHA256 == b.sha256, d.inputBundleManifestSHA256 == b.sha256,
          r.materializationInputSHA256 == b.sha256, m.runtimeManifestSHA256 == r.sha256,
          receipt.manifestSHA256 == m.sha256, receipt.admissionRecordSHA256 == d.sha256 else {
        throw ProvisionError.bundleApprovalMismatch
    }
}
```

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'CanonicalJSONTests|ModelsV2Tests'`

Expected: PASS; V1 has no parser branch for this ID.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add schemas tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{CanonicalJSON,Models}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ModelsV2Tests.swift
git commit -m "feat: define closed v2 materialization evidence"
```

### Task 1A: Amend closed V2 evidence for the approved Key-A, launcher, and interpreter graph

**Why this is an amendment:** Tasks 1–7 are retained as completed-plan history. The current checkpoint (`654b79c..7d95ba0`) added partial policy, Mach-O, and direct-process types, but its schemas/models omit the approved review-package, observation-authorization, Key-A bootstrap pin, launcher identity, base-interpreter identity, and full runtime/receipt command bindings. This task changes those existing files; it does not claim their present V2 decoders already implement this graph.

**Files:** create `schemas/r4_task14_privileged_materialization_review_package_v2.schema.json`, `schemas/r4_task14_privileged_materialization_observation_authorization_v2.schema.json`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures/r4_v2_domain_vectors.json`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures/generate_r4_v2_domain_vectors.py`, and `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/DomainVectorFixture.swift`; modify `schemas/r4_task14_protected_execution_approval.schema.json`, `schemas/r4_privileged_materialization_policy_v2.schema.json`, `schemas/r4_hermetic_python311_runtime.schema.json`, `schemas/r4_protected_execution_receipt.schema.json`, `config/r4_privileged_materialization_policy.json`, `tests/test_protocols.py`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Constants,CanonicalJSON,Models}.swift`, and `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ModelsV2Tests.swift`.

- [ ] **Step 1: Write RED closed-schema and domain-formula tests (5 minutes).** Add a one-key mutation table for every new/changed object. The fixture must reject a V1 approval/runtime/receipt, an extra key, a Key-A/Key-B byte equality, and every cross-document mismatch below.

```swift
func testApprovalGraphIsClosedAndPinsKeyARoles() throws {
    let f = V2Fixtures.complete()
    XCTAssertEqual(f.reviewPackage.task14ApprovalVerificationPublicKeySHA256,
                   ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
    XCTAssertEqual(f.observationAuthorization.task14ApprovalVerificationPublicKeySHA256,
                   ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
    XCTAssertEqual(f.approval.task14ApprovalVerificationPublicKeySHA256,
                   ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
    XCTAssertNotEqual(f.bootstrap.keyAPublicBytes, f.keyring.generation1PublicBytes)
    XCTAssertThrowsError(try Task14ApprovalV2.decode(f.approval.replacing("sandbox_launcher_path", "/bin/sh")))
    XCTAssertThrowsError(try ReceiptV2.decode(f.receipt.replacing("state", "ACTIVE")))
}

func testLauncherInterpreterAndCommandDigestDomainsAreExact() throws {
    let f = V2Fixtures.complete()
    XCTAssertEqual(f.launcher.descriptorIdentitySHA256,
        sha256("R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0".utf8 + canonical(f.launcherDescriptorRow)))
    XCTAssertEqual(f.launcher.codesignIdentity.certificateChainSHA256,
        sha256("R4V2_CERTIFICATE_CHAIN\0".utf8 + canonical(f.launcherCertificateDERHashes)))
    XCTAssertEqual(f.launcher.codesignIdentity.sha256,
        sha256("R4V2_CODESIGN_IDENTITY\0".utf8 + canonical(f.launcher.codesignIdentity.preimage)))
    XCTAssertEqual(f.policy.sandboxProfileTemplateSHA256,
        sha256("R4V2_SANDBOX_PROFILE_TEMPLATE\0".utf8 + f.policy.sandboxProfileUTF8.utf8))
    XCTAssertEqual(f.policy.childCommandTemplateSHA256,
        sha256("R4V2_CHILD_COMMAND_TEMPLATE\0".utf8 + canonical(f.childCommandTemplate)))
}

func testIndependentFixedVectorsCoverEveryNewDomain() throws {
    let vectors = try DomainVectorFixture.decode("r4_v2_domain_vectors.json")
    XCTAssertEqual(Set(vectors.map(\.domain)), Set([
        "R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\\0", "R4V2_CERTIFICATE_CHAIN\\0",
        "R4V2_CODESIGN_IDENTITY\\0", "R4V2_MACHO_OBSERVATION\\0",
        "R4V2_SANDBOX_PROFILE_TEMPLATE\\0", "R4V2_SANDBOX_PROFILE_ACTUAL\\0",
        "R4V2_CHILD_COMMAND_TEMPLATE\\0", "R4V2_INSTALL_COMMAND\\0",
        "R4V2_IMPORT_COMMAND\\0", "R4V2_ENVIRONMENT\\0", "R4V2_PUBLISHED_CLOSURE\\0"
    ]))
    for vector in vectors {
        XCTAssertEqual(CanonicalJSON.domainSHA256(vector.domain, vector.canonicalValue), vector.expectedSHA256)
    }
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests`

Expected: FAIL because the existing checkpoint models lack the two approval-preimage objects, vector fixture, and full launcher/interpreter/actual-command fields.

- [ ] **Step 3: Implement the exact closed types and formulas (5 minutes).** Add `Task14ReviewPackageV2`, `Task14ObservationAuthorizationV2`, `Task14ApprovalV2`, `SandboxLauncher`, `BaseInterpreter`, `CodeSignIdentity`, `ChildExecutionPolicy`, `ActualChildCommand`, and the expanded runtime/receipt models. Each has exactly the top-level field sequence named in the approved design; each document hash is the canonical object with only top-level `sha256` omitted. Both detached Key-A signatures are exactly 64 bytes and are outside their JSON self-digests.

`MaterializationPolicy` has exactly `kind`, `namespace`, `authority_id`, `python_version`, `archive`, `wheel_filenames`, `sandbox_launcher`, `base_interpreter`, `child_execution`, `allowed_loader_roots`, `import_modules`, `import_probe_sha256`, `schema_versions`, `sha256`. `sandbox_launcher` has exactly `path`, `descriptor_identity_sha256`, `sha256`, `codesign_identity`; `base_interpreter` has exactly `path`, `sha256`, `mach_o_sha256`, `codesign_identity`; `child_execution` has exactly `cwd`, `environment`, `pip_argv`, `import_argv`, `timeout_seconds`, `max_output_bytes`, `sandbox_profile_utf8`, `sandbox_profile_template_sha256`, `child_command_template_sha256`, `read_allow`, `write_allow`, `network_denied`.

Implement the exact domain formulas: `R4V2_MACHO_OBSERVATION\0`, `R4V2_RUNTIME_ENTRIES\0`, `R4V2_RUNTIME_DEPENDENCIES\0`, `R4V2_WHEEL_SET\0`, `R4V2_SANDBOX_PROFILE_TEMPLATE\0`, `R4V2_SANDBOX_PROFILE_ACTUAL\0`, `R4V2_CHILD_COMMAND_TEMPLATE\0`, `R4V2_INSTALL_COMMAND\0`, `R4V2_IMPORT_COMMAND\0`, `R4V2_ENVIRONMENT\0`, and `R4V2_PUBLISHED_CLOSURE\0`, each followed by precisely the approved canonical preimage. `ActualChildCommand` contains exactly `program`, `argv`, `cwd`, `environment`, `stdin`, `stdout_stderr`, `timeout_seconds`, `max_output_bytes`, `sandbox_launcher_descriptor_identity_sha256`, `sandbox_launcher_sha256`, `sandbox_launcher_codesign_identity_sha256`, `interpreter_sha256`, `interpreter_mach_o_sha256`, `interpreter_codesign_identity_sha256`, `sandbox_profile_sha256`.

`r4_v2_domain_vectors.json` is a committed ordinary repository fixture, owned solely by the `R4ProtectedExecutionProvisionerTests` target and not linked into the provisioner executable or any production bundle. It is UTF-8 canonical JSON with no newline and exactly `schema_version: 1`, `kind: "R4_V2_DOMAIN_VECTORS_V1"`, and `vectors`; each row has exactly `domain`, `canonical_value`, and `expected_sha256`, is strictly lexicographically ordered by `domain`, and occurs once. Define test-only `struct DomainVectorFixture { let schemaVersion: Int; let kind: String; let vectors: [DomainVector] }` and `struct DomainVector { let domain: String; let canonicalValue: JSONValue; let expectedSHA256: String }` in `DomainVectorFixture.swift`; `DomainVectorFixture.decode(_ resourceName: String) throws -> DomainVectorFixture` uses `CanonicalJSON.decode` only to parse the resource and rejects a different resource name, noncanonical bytes, wrong version/kind, duplicate/unknown/missing key, unordered row, or non-lowercase digest.

`generate_r4_v2_domain_vectors.py` is a standalone non-production generator. It implements its own RFC-8785 subset encoder over fixed literal fixture values, prefixes UTF-8 domain bytes including the NUL byte, and uses only `hashlib.sha256`; it must not import `mlb_backtest`, `scripts`, Swift source, `CanonicalJSON`, a production builder, or the committed vector file. It writes bytes only to the caller-supplied 0600 temporary output and never overwrites the fixture. The following exact block fail-closes: `set -e` stops at any generator, digest, or comparison failure, the EXIT trap removes the temporary file without becoming the checked result, and `cmp -s` is the final checked command.

```bash
set -e
vectors_dir="tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures"
generated_vectors="$(mktemp -t r4-v2-domain-vectors)"
cleanup_generated_vectors() { /bin/rm -f -- "$generated_vectors"; }
trap cleanup_generated_vectors EXIT
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 "$vectors_dir/generate_r4_v2_domain_vectors.py" --output "$generated_vectors"
/usr/bin/shasum -a 256 "$generated_vectors"
cmp -s "$generated_vectors" "$vectors_dir/r4_v2_domain_vectors.json"
```

Expected: `cmp -s` exits zero; a mismatch remains a nonzero shell exit after cleanup, and the generator is an independent oracle.

In `tests/test_protocols.py`, replace the current policy fixture's `--find-links`, stage-directory search path, bare wheel filename, and `sandbox_profile_sha256` field with `sandbox_launcher`, `base_interpreter`, `sandbox_profile_template_sha256`, `child_command_template_sha256`, and an ordered operand for each policy wheel formed by concatenating `__R4_STAGE__/wheelhouse/` with that exact `wheel_filenames` value. Add the complete approval/runtime/receipt fields, review-package/observation-authorization fixtures, and one-key removal, unknown-key addition, and V1-kind rejection cases for policy, approval, runtime, and receipt.

- [ ] **Step 4: Run GREEN (2 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter 'CanonicalJSONTests|ModelsV2Tests'
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_protocols -v
set -e
vectors_dir="tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures"
generated_vectors="$(mktemp -t r4-v2-domain-vectors)"
cleanup_generated_vectors() { /bin/rm -f -- "$generated_vectors"; }
trap cleanup_generated_vectors EXIT
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 "$vectors_dir/generate_r4_v2_domain_vectors.py" --output "$generated_vectors"
/usr/bin/shasum -a 256 "$generated_vectors"
cmp -s "$generated_vectors" "$vectors_dir/r4_v2_domain_vectors.json"
```

Expected: PASS; every document is V2-only, complete, acyclic, and rejects before any storage or process adapter is invoked.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add schemas/r4_task14_privileged_materialization_review_package_v2.schema.json schemas/r4_task14_privileged_materialization_observation_authorization_v2.schema.json schemas/r4_task14_protected_execution_approval.schema.json schemas/r4_privileged_materialization_policy_v2.schema.json schemas/r4_hermetic_python311_runtime.schema.json schemas/r4_protected_execution_receipt.schema.json config/r4_privileged_materialization_policy.json tests/test_protocols.py tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Constants,CanonicalJSON,Models}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{ModelsV2Tests,DomainVectorFixture}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures/{r4_v2_domain_vectors.json,generate_r4_v2_domain_vectors.py}
git commit -m "feat: close v2 approval and launcher evidence"
```

## Task 2A: Migration — replace the baseline bootstrap/key lifecycle with Key-A then Key-B

**Files:** modify `Bootstrap.swift`, `CLI.swift`, `Constants.swift`, `Models.swift`, `Adapters.swift`, `main.swift`, `BootstrapTests.swift`, and `CLITests.swift`.

- [ ] **Step 1: Write bootstrap RED ordering and sealed-input tests (5 minutes).**

```swift
func testBootstrapPinsHeldKeyABeforeApprovalSignatureAndThenCreatesKeyB() throws {
    let f = BootstrapFixture.make()
    defer { try? f.removeOnlySelfOwnedFixture() }
    try Bootstrap.installForTest(f.bootstrapRequest, adapters: f.adapters)
    XCTAssertEqual(f.events, [.openKeyANoFollow, .hashKeyA, .compareCompiledKeyAHash,
        .verifyApprovalSignature, .decodeApproval, .verifyApprovalDigest, .verifyHelperIdentity,
        .sealApproval, .sealApprovalSignature, .sealKeyA, .sealApprovalDigest,
        .generateKeyBGeneration1, .sealKeyBGeneration1])
    XCTAssertNotEqual(f.bootstrap.keyAPublicBytes, f.keyring.generation1PublicBytes)
}
func testBootstrapRejectsBadKeyABeforeSignatureOrKeyB() throws {
    for mutation in [wrongKeyAHash, nonRootKeyA, keyASymlink, invalidKeyAEncoding,
                     callerExpectedKeyHashFlag] {
        let f = BootstrapFixture.make().mutating(mutation)
        XCTAssertThrowsError(try Bootstrap.installForTest(f.bootstrapRequest, adapters: f.adapters))
        XCTAssertFalse(f.events.contains(.verifyApprovalSignature))
        XCTAssertFalse(f.events.contains(.generateKeyBGeneration1))
    }
}
func testBootstrapRejectsBadSignatureAfterKeyAPinBeforeKeyB() throws {
    let f = BootstrapFixture.make().mutating(badApprovalSignature)
    XCTAssertThrowsError(try Bootstrap.installForTest(f.bootstrapRequest, adapters: f.adapters))
    XCTAssertTrue(f.events.contains(.verifyApprovalSignature))
    XCTAssertFalse(f.events.contains(.decodeApproval))
    XCTAssertFalse(f.events.contains(.generateKeyBGeneration1))
}
func testBootstrapRejectsBadDigestAfterSignatureBeforeKeyB() throws {
    let f = BootstrapFixture.make().mutating(badApprovalDigest)
    XCTAssertThrowsError(try Bootstrap.installForTest(f.bootstrapRequest, adapters: f.adapters))
    XCTAssertTrue(f.events.contains(.verifyApprovalSignature))
    XCTAssertTrue(f.events.contains(.decodeApproval))
    XCTAssertTrue(f.events.contains(.verifyApprovalDigest))
    XCTAssertFalse(f.events.contains(.generateKeyBGeneration1))
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'BootstrapTests|CLITests'`

Expected: FAIL because the existing bootstrap lifecycle still models a single generated key and does not require a detached Key-A approval signature.

- [ ] **Step 3: Implement the closed bootstrap grammar and Key-A/Key-B order (5 minutes).** `CLI.parse` accepts `bootstrap-install` only with these ordered flags: `--task14-approval`, `--task14-approval-signature`, `--task14-approval-verification-public-key`, `--task14-approval-sha256`; all paths are absolute, non-symlinked, root-owned regular files, the digest is 64 lowercase hex, and no repeated/extra flag is accepted. Remove `--approval-envelope`, `--expected-approval-sha256`, and every expected-Key-A hash input.

`Bootstrap.install` opens the held Key-A descriptor with no-follow semantics, requires exact public-key encoding and root ownership, hashes its held bytes, and compares that result to `ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256` before it parses or verifies the approval signature. It then verifies the 64-byte detached signature, canonical V2 approval bytes, independently supplied approval digest, and held executable byte SHA-256/designated requirement/Developer-ID chain/Team ID. It writes only byte-identical `bootstrap/task14-approval-v2.json`, `bootstrap/task14-approval-v2.ed25519`, `bootstrap/task14-approval-verification-public-key`, and `bootstrap/task14-approval-v2.sha256`; each is root:wheel, fsynced, sealed, reopened, and descriptor-rebound. Only after all four read back does it cryptographically generate the distinct Key-B generation-1 private/public pair at `control/keyring/generation-1/authority-receipt-ed25519-private-key` (0600) and `authority-receipt-ed25519-public-key` (0444), seal/rebind both, and write the V1 keyring record. Key B never verifies an approval and Key A never enters `control/keyring/`.

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'BootstrapTests|CLITests'`

Expected: PASS; any Key-A mismatch ends before signature verification/bootstrap writes/Key-B generation, and bootstrap writes no Key-A private material.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Bootstrap,CLI,Constants,Models,Adapters,main}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{Bootstrap,CLI}Tests.swift
git commit -m "feat: pin key a before materialization bootstrap"
```

## Task 3: Completed baseline/history — original fixture-root adapters

**Files:** create `Fakes.swift`, `FakesTests.swift`, `POSIX.swift`, and `Adapters.swift`.

- [ ] **Step 1: Write RED adapter-confinement tests (2 minutes).**

```swift
func testFakeFixtureFactoryDefinesAllLaterFixtures() throws {
    let f = MaterializationFixture.valid()
    defer { try? f.removeOnlySelfOwnedFixture() }
    XCTAssertNotNil(f.fakeFS); XCTAssertNotNil(f.fakeProcess); XCTAssertNotNil(f.validArchive)
    XCTAssertNotNil(f.bundleWithExtraChild()); XCTAssertNotNil(f.swapBeforeCopy())
}
func testFakeFSRejectsMutationOutsideFixtureRoot() throws {
    let f = MaterializationFixture.valid()
    defer { try? f.removeOnlySelfOwnedFixture() }
    XCTAssertThrowsError(try f.fakeFS.mkdir("/Library/x"))
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter FakesTests`

Historical baseline: do not run this RED command at current HEAD; Task 3A supplies the truthful migration RED command.

- [ ] **Step 3: Implement fixture APIs (5 minutes).** Define `MaterializationFixture.valid()`, `removeOnlySelfOwnedFixture()`, `request`, `adapters`, `storage`, `durableStore`, `stage`, `validatedBundle`, `validArchive`, `fakeFS`, `fakeProcess`, `mutating(_:)`, `bundleWithExtraChild`, `swapBeforeCopy`, `swapBefore(_:)`, `fault(after:)`, `admissionFault(_:)`, `recordedGit`, `admissionBarrier`, and `raceTwo(startBarrier:_:)`. `raceTwo` starts two closures on a `DispatchGroup`, releases them only after `BarrierBackedAdmissionStorage` has both callers at its real `openat(O_CREAT|O_EXCL)` seam, and returns two `Result<Admission, Error>` values. Every fixture-producing test immediately registers `defer { try? fixture.removeOnlySelfOwnedFixture() }`. `FakeFileSystem` records `open/create/rename/fsync/chmod/chflags` and rejects mutation outside its root; `FakeProcessRunner` records argv/cwd/environment/profile and rejects all executable targets unless explicitly fixture-approved. Production `DarwinFileSystem` is a separate adapter and is not called by fixture tests.

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter FakesTests`

Expected: PASS; every later Swift test has a defined fixture and a root-confinement assertion.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority
git commit -m "test: add materialization fixture adapters"
```

### Task 3A: Migration — extend fakes for Key-A, observation, launcher, and terminal admission seams

**Files:** modify `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{Fakes,FakesTests,BundleTraversalTests,AdmissionTests,ArchiveTests,StorageTests}.swift` and `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Adapters,BundleTraversal,Admission,Archive,Storage}.swift`.

- [ ] **Step 1: Write current-HEAD RED migration tests (5 minutes).** Add `FakeKeyAStore`, `FakeObservationSecurity`, and `FakePinnedLauncher` to `Fakes.swift`. Verify the fixture can fault each descriptor rebind, review/authorization signature verification, archive-copy/extract/rename, launcher Security observation, and root admission handoff seam while recording zero child launches and zero mutation outside its fixture root.

```swift
func testMigrationFixturesExposeObservationAndHandoffFaults() throws {
    let f = MaterializationFixture.valid()
    XCTAssertThrowsError(try f.handoff.reviewPackage.swapAfterCopy())
    XCTAssertThrowsError(try f.observation.security.failAfterDescriptorRebind())
    XCTAssertThrowsError(try f.archive.failAfterFinalRename())
    XCTAssertEqual(f.fakeProcess.launches, [])
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'FakesTests|BundleTraversalTests|AdmissionTests|ArchiveTests|StorageTests'`

Expected: FAIL because the current fakes have no Key-A handoff or candidate-observation seams.

- [ ] **Step 3: Implement the fixture migration (5 minutes).** Add explicit fake adapters only for the exact production protocols: no test helper may invoke `ProcessRunner`, access `/Library`, or synthesize a successful Security observation. `BundleTraversal` receives `HeldCeremonyInputs` only after the root-owned handoff record has been opened/rebound; `Admission` keeps materialization admission distinct from ceremony handoff; `Archive` exposes the shared in-process extractor to the candidate observation adapter without a wheelhouse; existing copy/rename tests add the resulting no-child assertion.

- [ ] **Step 4: Run GREEN and commit (2 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter 'FakesTests|BundleTraversalTests|AdmissionTests|ArchiveTests|StorageTests'
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Adapters,BundleTraversal,Admission,Archive,Storage}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{Fakes,FakesTests,BundleTraversal,Admission,Archive,Storage}Tests.swift
git commit -m "test: add r4 observation migration seams"
```

Expected: PASS; every dependent migration starts from a fixture whose new failure seam is explicit.

## Task 4A: Migration — closed CLI, installed-helper identity, storage preflight, and typed pre-admission validation

**Files:** create `CLI.swift`, `Storage.swift`, `BundleTraversal.swift`, `CLITests.swift`, `StorageTests.swift`, and `BundleTraversalTests.swift`; modify `POSIX.swift`, `Adapters.swift`, and `main.swift`.

- [ ] **Step 1: Write RED tests proving every bad input fails before admission (2 minutes).**

```swift
func testAllPreAdmissionFailuresCreateNoAdmissionRecord() throws {
    for fixture in [v1Bundle, mixedGraph, wrongApproval, badLock, extraChild, swappedArchive,
                    swappedWheel, swappedCheckout, symlink, hardlink, caseCollision, nonRootEUID,
                    copiedHelper, badStorage, forbiddenEnvironment, inheritedDescriptor] {
        let f = MaterializationFixture.valid().mutating(fixture)
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try BundleVerifier.verifyBeforeAdmission(f.request, adapters: f.adapters))
        XCTAssertEqual(f.fakeFS.admissionRecords, [])
        XCTAssertEqual(f.fakeProcess.launches, [])
    }
}
func testClosedGrammarRejectsBeforeMutation() throws {
    for argv in [["provision"], ["materialize-and-provision"], ["materialize-and-provision", "--input-bundle", "x"], ["verify", "--authority-id", "other"]] {
        XCTAssertThrowsError(try CLI.parse(argv))
    }
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'CLITests|StorageTests|BundleTraversalTests'`

Expected: FAIL because current HEAD's existing CLI/storage/verifier do not enforce the new closed Key-A bootstrap grammar, installed-command rejection, or expanded V2 bindings.

- [ ] **Step 3: Implement pre-admission APIs (5 minutes).** Preserve Task 2's exact `bootstrap-install --task14-approval /absolute/approval.json --task14-approval-signature /absolute/approval.ed25519 --task14-approval-verification-public-key /absolute/key-a-public --task14-approval-sha256 64-lowercase-hex` grammar; `materialize-and-provision` accepts exactly `--input-bundle /absolute/input-bundle`; `verify`, Key-B rotation, and revocation retain their fixed grammars. Reject unknown spelling/options, repeated flags, a caller-selected expected-key hash, and `task14-observe-approval-preimages` in every installed-helper grammar. Before reading a bundle, `InstalledHelper.requireRootAndIdentity` checks EUID, closes inherited descriptors, rejects `PATH`, `PWD`, `HOME`, `PYTHON*`, `PIP_*`, `DYLD_*`, `VIRTUAL_ENV`, rebinding loaded executable to bootstrap provisioner and its approval-pinned hash/requirement/Team/Developer-ID identity. `StoragePreflight` descriptor-walks fixed ancestors, requires root:wheel/ACL-free/non-symlink local writable internal non-removable APFS and stable UUID, agrees `NSURL` values with descriptor xattrs, and rejects FileProvider/iCloud xattrs.

```swift
static func verifyBeforeAdmission(_ request: MaterializationRequest,
                                  adapters: MaterializationAdapters) throws -> ValidatedBundle {
    try adapters.installedHelper.requireRootAndIdentity()
    try StoragePreflight.verifyFixedParents(adapters.fs)
    let held = try HeldBundle.openExactlyFiveChildren(request.inputBundle, fs: adapters.fs)
    let values = try held.parseAndRebindAllV2(adapters.bootstrapApproval)
    try V2CrossHash.validate(values)
    return ValidatedBundle(heldRoot: held.root, manifest: values.manifest,
                           lock: values.lock, policy: values.policy, approval: values.approval)
}
```

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'CLITests|StorageTests|BundleTraversalTests'`

Expected: PASS; every V1/approval/path/storage/identity/swap error occurs before `O_EXCL` and no root-owned stage/admission/process exists.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority
git commit -m "feat: validate v2 bundle before admission"
```

## Task 5: Completed baseline/history — original Task2 transport graph

**Files:** create `scripts/verify_r4_protected_inputs.py`, `scripts/r4_protected_fixed_git.py`, and `tests/test_r4_protected_fixed_git.py`; modify `scripts/prepare_r4_protected_runtime.py`, `scripts/build_r4_protected_candidate.py`, `scripts/build_r4_task14_approval.py`, `config/r4_privileged_materialization_policy.json`, `tests/test_prepare_r4_protected_runtime.py`, and `tests/test_build_r4_protected_candidate.py`.

- [ ] **Step 1: Write RED fixed-Git runner, AST-boundary, and full-recording tests (5 minutes).** `tests/test_r4_protected_fixed_git.py` defines the test-only helpers below before invoking the production runner. `ScriptedRecordingRunner` is injected into both builders; it returns deterministic fixture bytes only after the complete ordered call equality succeeds. `FakeGitProcess` is used only by `unittest.mock.patch` of `r4_protected_fixed_git.subprocess.Popen`; no production builder receives a process factory.

```python
from dataclasses import replace
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import call as mock_call, patch
import signal
import subprocess

from r4_protected_fixed_git import GIT_ENVIRONMENT, GIT_EXECUTABLE, GIT_PREFIX, RecordedGitCall, run_fixed_git, run_recorded_git
from build_r4_protected_candidate import (APPROVED_SOURCE_PATHS,
                                          CANONICAL_REQUIRED_BLOB_PATHS,
                                          build_input_bundle,
                                          validate_required_blob_rows)
from build_r4_task14_approval import build_task14_approval_v2

EXPECTED_CANONICAL_REQUIRED_BLOB_PATHS = tuple(sorted({
    "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md",
    "docs/superpowers/specs/2026-08-08-r4-protected-execution-authority-design.md",
    "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md",
    "mlb_backtest/v52_r4_disjoint_execution.py",
    "mlb_backtest/v52_r4_disjoint_issuer.py",
    "mlb_backtest/v52_r4_disjoint_policy.py",
}))
assert APPROVED_SOURCE_PATHS == CANONICAL_REQUIRED_BLOB_PATHS
assert CANONICAL_REQUIRED_BLOB_PATHS == EXPECTED_CANONICAL_REQUIRED_BLOB_PATHS
assert CANONICAL_REQUIRED_BLOB_PATHS == tuple(sorted(CANONICAL_REQUIRED_BLOB_PATHS))

FIXTURE_BLOB_BYTES = {
    CANONICAL_REQUIRED_BLOB_PATHS[0]: b"fixture approved source 1\n",
    CANONICAL_REQUIRED_BLOB_PATHS[1]: b"fixture approved source 2\n",
    CANONICAL_REQUIRED_BLOB_PATHS[2]: b"fixture approved source 3\n",
    CANONICAL_REQUIRED_BLOB_PATHS[3]: b"fixture approved source 4\n",
    CANONICAL_REQUIRED_BLOB_PATHS[4]: b"fixture approved source 5\n",
    CANONICAL_REQUIRED_BLOB_PATHS[5]: b"fixture approved source 6\n",
}
FIXTURE_BLOB_IDS = {
    CANONICAL_REQUIRED_BLOB_PATHS[0]: "e3ce8f8152a89641228fbec0e34c623f8652643e",
    CANONICAL_REQUIRED_BLOB_PATHS[1]: "5bed9f362a257900ecea919326773f57df44422c",
    CANONICAL_REQUIRED_BLOB_PATHS[2]: "a17282871ed5cdd54916b77c7a07b7dee51e2d52",
    CANONICAL_REQUIRED_BLOB_PATHS[3]: "b5c1647a5f33799493a90cdcc015280d34a91a51",
    CANONICAL_REQUIRED_BLOB_PATHS[4]: "e699907c217951de4a260b8d5e61612d9a5217f1",
    CANONICAL_REQUIRED_BLOB_PATHS[5]: "ac905c37bb627053ff39d4d92f32650128cb45ff",
}
FIXTURE_LS_TREE = b"".join(
    f"100644 blob {FIXTURE_BLOB_IDS[path]}\t{path}\0".encode("utf-8")
    for path in CANONICAL_REQUIRED_BLOB_PATHS
)
FIXTURE_BYTES_BY_ARGUMENTS = {
    ("symbolic-ref", "-q", "HEAD"): b"refs/heads/main\n",
    ("status", "--porcelain=v1", "--ignored", "-z"): b"",
    ("rev-parse", "--verify", "HEAD^{commit}"): b"0123456789abcdef0123456789abcdef01234567\n",
    ("rev-parse", "--verify", "HEAD^{tree}"): b"89abcdef0123456789abcdef0123456789abcdef\n",
    ("ls-tree", "-r", "-z", "--full-tree", "HEAD"): FIXTURE_LS_TREE,
    **{
        ("cat-file", "blob", FIXTURE_BLOB_IDS[path]): FIXTURE_BLOB_BYTES[path]
        for path in CANONICAL_REQUIRED_BLOB_PATHS
    },
}
EXPECTED_GIT_ARGUMENTS = (
    ("symbolic-ref", "-q", "HEAD"),
    ("status", "--porcelain=v1", "--ignored", "-z"),
    ("rev-parse", "--verify", "HEAD^{commit}"),
    ("rev-parse", "--verify", "HEAD^{tree}"),
    ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
    *(("cat-file", "blob", FIXTURE_BLOB_IDS[path]) for path in CANONICAL_REQUIRED_BLOB_PATHS),
)

def expected_recorded_git_calls(source: Path) -> Sequence[RecordedGitCall]:
    normalized_source = str(source.resolve(strict=True))
    return tuple(
        RecordedGitCall(
            executable=GIT_EXECUTABLE,
            argv=GIT_PREFIX + ("-C", normalized_source) + arguments,
            environment=dict(GIT_ENVIRONMENT),
            stdin="DEVNULL",
            timeout_seconds=30,
            output_cap=4 * 1024 * 1024,
        )
        for arguments in EXPECTED_GIT_ARGUMENTS
    )

def assert_full_call_list(actual: Sequence[RecordedGitCall], expected: Sequence[RecordedGitCall]) -> None:
    if len(actual) != len(expected):
        raise AssertionError(f"expected {len(expected)} Git calls, got {len(actual)}")
    for index, (observed, wanted) in enumerate(zip(actual, expected)):
        observed_fields = (observed.executable, observed.argv, observed.environment,
                           observed.stdin, observed.timeout_seconds, observed.output_cap)
        wanted_fields = (wanted.executable, wanted.argv, wanted.environment,
                         wanted.stdin, wanted.timeout_seconds, wanted.output_cap)
        if observed_fields != wanted_fields:
            raise AssertionError(f"Git call {index} differs: {observed_fields!r} != {wanted_fields!r}")

class ScriptedRecordingRunner:
    def __init__(self, source: Path,
                 expected_calls: Sequence[RecordedGitCall] | None = None) -> None:
        self.expected_calls = (expected_recorded_git_calls(source)
                               if expected_calls is None else expected_calls)
        self.calls: list[RecordedGitCall] = []

    def __call__(self, call: RecordedGitCall) -> bytes:
        index = len(self.calls)
        if index >= len(self.expected_calls):
            raise AssertionError(f"unexpected extra Git call at index {index}: {call!r}")
        assert_full_call_list([call], (self.expected_calls[index],))
        self.calls.append(call)
        arguments = tuple(call.argv[len(GIT_PREFIX) + 2:])
        try:
            return FIXTURE_BYTES_BY_ARGUMENTS[arguments]
        except KeyError as error:
            raise AssertionError(f"no deterministic fixture for Git arguments {arguments!r}") from error

    def assert_complete(self) -> None:
        assert_full_call_list(self.calls, self.expected_calls)

class FakeGitProcess:
    def __init__(self, waits: list[object]) -> None:
        self.pid = 731
        self.waits = waits
        self.stdout = None
        self.stderr = None

    def poll(self) -> None:
        return None

    def wait(self, timeout: int) -> int:
        result = self.waits.pop(0)
        if isinstance(result, BaseException):
            raise result
        return int(result)

def test_runner_rejects_each_nonfixed_call_shape(self) -> None:
    source = self.repo.resolve(strict=True)
    call = RecordedGitCall(GIT_EXECUTABLE, GIT_PREFIX + ("-C", str(source), "rev-parse", "--verify", "HEAD^{commit}"), GIT_ENVIRONMENT, "DEVNULL", 30, 4 * 1024 * 1024)
    invalid = [replace(call, executable="/usr/bin/env"),
               replace(call, argv=("status",)),
               replace(call, environment={"PATH": "/usr/bin:/bin"}),
               replace(call, stdin="PIPE"), replace(call, timeout_seconds=31),
               replace(call, output_cap=4 * 1024 * 1024 - 1)]
    for bad in invalid:
        with self.assertRaises(ValueError):
            run_recorded_git(bad)

def test_runner_uses_real_fixed_git(self) -> None:
    output = run_fixed_git(self.repo, ("rev-parse", "--verify", "HEAD^{commit}"),
                           runner=run_recorded_git)
    self.assertRegex(output.decode("ascii"), r"^[0-9a-f]{40}\n$")

def test_timeout_oversize_and_descendant_failures_kill_the_group(self) -> None:
    source = self.repo.resolve(strict=True)
    call = RecordedGitCall(GIT_EXECUTABLE, GIT_PREFIX + ("-C", str(source), "status", "--porcelain=v1", "--ignored", "-z"), GIT_ENVIRONMENT, "DEVNULL", 30, 4 * 1024 * 1024)
    failure_cases = [TimeoutError("fixed Git command timed out"),
                     ValueError("fixed Git command exceeded output cap")]
    for failure in failure_cases:
        process = FakeGitProcess([0])
        with patch("r4_protected_fixed_git.subprocess.Popen", return_value=process), \
             patch("r4_protected_fixed_git._read_capped_pipes", side_effect=failure), \
             patch("r4_protected_fixed_git.os.killpg") as killpg:
            with self.assertRaises(type(failure)):
                run_recorded_git(call)
            killpg.assert_called_once_with(731, signal.SIGTERM)
    descendant = FakeGitProcess([subprocess.TimeoutExpired([GIT_EXECUTABLE], 1), 0])
    with patch("r4_protected_fixed_git.subprocess.Popen", return_value=descendant), \
         patch("r4_protected_fixed_git._read_capped_pipes", side_effect=TimeoutError("timeout")), \
         patch("r4_protected_fixed_git.os.killpg") as killpg:
        with self.assertRaises(TimeoutError):
            run_recorded_git(call)
        self.assertEqual(killpg.call_args_list, [mock_call(731, signal.SIGTERM), mock_call(731, signal.SIGKILL)])

def test_task2bc_record_complete_fixed_calls(self) -> None:
    source = self.repo.resolve(strict=True)
    expected = expected_recorded_git_calls(source)
    candidate = ScriptedRecordingRunner(source)
    build_input_bundle(self.repo, self.downloads, self.lock, self.policy, self.output,
                       runner=candidate)
    candidate.assert_complete()
    assert_full_call_list(candidate.calls, expected)

    approval = ScriptedRecordingRunner(source)
    build_task14_approval_v2(self.repo, self.output, self.helper, self.log, self.approval,
                             runner=approval)
    approval.assert_complete()
    assert_full_call_list(approval.calls, expected)

def test_task2bc_recording_rejects_omitted_extra_and_reordered_calls(self) -> None:
    source = self.repo.resolve(strict=True)
    expected = expected_recorded_git_calls(source)
    for label, scripted, should_fail_while_consuming in (
        ("omitted", expected[:-1], True),
        ("reordered", (expected[1], expected[0], *expected[2:]), True),
        ("extra", (*expected, expected[-1]), False),
    ):
        with self.subTest(sequence=label):
            runner = ScriptedRecordingRunner(source, scripted)
            if should_fail_while_consuming:
                with self.assertRaises(AssertionError):
                    for call in expected:
                        runner(call)
            else:
                for call in expected:
                    runner(call)
                with self.assertRaises(AssertionError):
                    runner.assert_complete()

def test_task2a_ast_has_no_process_or_executable_artifact_capability(self) -> None:
    tree = ast.parse((ROOT / "scripts" / "verify_r4_protected_inputs.py").read_text())
    imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imports |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    forbidden_modules = {"subprocess", "socket", "urllib", "http", "tarfile"}
    forbidden_calls = {"os.system", "os.popen", "os.execv", "os.execve", "os.execl", "os.execvp",
                       "os.spawnv", "os.spawnve", "subprocess.run", "subprocess.Popen", "subprocess.call",
                       "subprocess.check_call", "subprocess.check_output", "tarfile.open"}
    self.assertFalse(imports & forbidden_modules)
    self.assertFalse(calls & forbidden_calls)

def test_only_dedicated_runner_may_import_subprocess(self) -> None:
    for script in ("build_r4_protected_candidate.py", "build_r4_task14_approval.py"):
        tree = ast.parse((ROOT / "scripts" / script).read_text())
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertFalse(imported & {"subprocess", "socket", "urllib", "http", "tarfile"})
        self.assertFalse(calls & {"os.system", "os.popen", "os.execv", "os.execve", "os.execl", "os.execvp", "os.spawnv", "os.spawnve"})
        self.assertIn("run_fixed_git", calls)
    runner_tree = ast.parse((ROOT / "scripts" / "r4_protected_fixed_git.py").read_text())
    runner_imports = {alias.name.split(".")[0] for node in ast.walk(runner_tree) if isinstance(node, ast.Import) for alias in node.names}
    runner_imports |= {node.module.split(".")[0] for node in ast.walk(runner_tree) if isinstance(node, ast.ImportFrom) and node.module}
    runner_calls = {ast.unparse(node.func) for node in ast.walk(runner_tree) if isinstance(node, ast.Call)}
    self.assertIn("subprocess", runner_imports)
    self.assertFalse(runner_imports & {"socket", "urllib", "http"})
    self.assertFalse(runner_calls & {"os.system", "os.popen", "os.execv", "os.execve", "os.execl", "os.execvp", "os.spawnv", "os.spawnve"})
    self.assertIn("subprocess.Popen", runner_calls)

def test_required_blobs_are_exact_fixed_six_while_checkout_is_exhaustive(self) -> None:
    bundle = build_input_bundle(self.repo, self.downloads, self.lock, self.policy, self.output)
    validate_required_blob_rows(bundle.required_blobs)
    self.assertEqual(tuple(row["path"] for row in bundle.required_blobs),
                     CANONICAL_REQUIRED_BLOB_PATHS)
    self.assertEqual(checkout_paths(bundle.root / "checkout"), git_regular_paths(self.repo))

def test_unsorted_required_blobs_are_rejected(self) -> None:
    rows = [{"path": path, "blob": FIXTURE_BLOB_IDS[path], "sha256": "a" * 64}
            for path in reversed(CANONICAL_REQUIRED_BLOB_PATHS)]
    with self.assertRaises(ValueError):
        validate_required_blob_rows(rows)
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_r4_protected_fixed_git tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate -v`

Historical baseline: do not run this RED command at current HEAD; Task 5A supplies the truthful Task2c migration RED command.

- [ ] **Step 3: Implement the only Task2 subprocess adapter (5 minutes).** Create `scripts/r4_protected_fixed_git.py` with the declared `run_recorded_git` implementation. It validates the complete executable, `GIT_PREFIX`, canonical `-C` source, allowlisted evidence arguments, exact sterile environment, `/dev/null` stdin token, 30-second limit, and 4 MiB combined pipe cap before `Popen`. It starts a separate process group with `shell=False`, `cwd="/"`, and `close_fds=True`; selector-driven reads terminate and reap the entire group on timeout, cap breach, read error, or nonzero exit. It permits no command other than `symbolic-ref -q HEAD`, `status --porcelain=v1 --ignored -z`, both verified `rev-parse` forms, `ls-tree -r -z --full-tree HEAD`, and `cat-file blob` plus a lower-case 40-hex object ID. The `GIT_ALLOW_PROTOCOL=file`, hooks-disabled, fsmonitor-disabled, external-diff-disabled, replacement-disabled, and lazy-fetch-disabled settings are part of equality, not optional process settings.

- [ ] **Step 4: Implement Task2a and its deprecation wrapper (5 minutes).** `verify_download_set(lock_path, downloads)` uses no-follow single-link descriptors and returns only held archive/wheel metadata; lock/download mismatch, extra/missing/link/swap fails. `prepare_r4_protected_runtime.py` raises `R4_TASK2A_DEPRECATED_NO_EXECUTION` and imports no old implementation. The AST assertion is the enforcement mechanism; do not use a broad text scan.

- [ ] **Step 5: Implement Task2b/Task2c V2 builders, default runner wiring, and approval (5 minutes).** Define the canonical paths exactly before any manifest or digest code:

```python
CANONICAL_REQUIRED_BLOB_PATHS = tuple(sorted({
    "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md",
    "docs/superpowers/specs/2026-08-08-r4-protected-execution-authority-design.md",
    "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md",
    "mlb_backtest/v52_r4_disjoint_execution.py",
    "mlb_backtest/v52_r4_disjoint_issuer.py",
    "mlb_backtest/v52_r4_disjoint_policy.py",
}))
APPROVED_SOURCE_PATHS = CANONICAL_REQUIRED_BLOB_PATHS
assert CANONICAL_REQUIRED_BLOB_PATHS == tuple(sorted(CANONICAL_REQUIRED_BLOB_PATHS))
```

Task2b writes exactly `R4_RUNTIME_INPUT_BUNDLE_V2.json`, lock copy, policy copy, exhaustive `checkout/`, and `artifacts/`; its `required_blobs` rows, manifest serialization, and `R4V2_REQUIRED_BLOBS` digest all iterate this same canonical six-path tuple, while checkout contains every tracked regular file. Both builders call `validate_required_blob_rows` before serialization, so an omitted, duplicate, or unsorted row is rejected. Task2c independently rebuilds the same canonical six via `run_fixed_git`, rejects unequal rows, and emits V2 approval binding bundle/lock/archive/wheel-set/policy/full Git evidence/helper evidence/exact suite. Both CLI `main` functions call their builders with `runner=run_recorded_git`; unit tests call the same builders with `ScriptedRecordingRunner`.

```python
from collections.abc import Sequence
from r4_protected_fixed_git import GitRunner, run_fixed_git, run_recorded_git

def validate_required_blob_rows(rows: list[dict[str, str]]) -> None:
    paths = tuple(row["path"] for row in rows)
    if paths != CANONICAL_REQUIRED_BLOB_PATHS:
        raise ValueError("required_blobs must use the canonical sorted six-path order")

def _git(source: Path, arguments: Sequence[str], *, runner: GitRunner) -> bytes:
    return run_fixed_git(source, arguments, runner=runner)

def build_input_bundle(source: Path, downloads: Path, lock_path: Path,
                       policy_path: Path, output: Path,
                       runner: GitRunner = run_recorded_git) -> InputBundle:
    evidence = read_exhaustive_git_evidence(source, runner)
    artifacts = verify_download_set(lock_path, downloads)
    return write_bundle(output, evidence=evidence, artifacts=artifacts,
                        lock_bytes=read_canonical(lock_path), policy_bytes=read_canonical(policy_path))

def main(argv: Sequence[str] | None = None) -> int:
    parsed = parse_closed_candidate_arguments(argv)
    build_input_bundle(parsed.source, parsed.downloads, parsed.lock, parsed.policy,
                       parsed.output, runner=run_recorded_git)
    return 0

# scripts/build_r4_task14_approval.py exports its own `main` with this body.
def main(argv: Sequence[str] | None = None) -> int:
    parsed = parse_closed_approval_arguments(argv)
    build_task14_approval_v2(parsed.repository, parsed.bundle, parsed.helper_build,
                              parsed.exact_suite_log, parsed.output,
                              runner=run_recorded_git)
    return 0
```

- [ ] **Step 6: Run GREEN (2 minutes).**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_r4_protected_fixed_git tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate -v
/opt/homebrew/bin/python3.11 -m ruff check scripts/r4_protected_fixed_git.py scripts/verify_r4_protected_inputs.py scripts/prepare_r4_protected_runtime.py scripts/build_r4_protected_candidate.py scripts/build_r4_task14_approval.py tests/test_r4_protected_fixed_git.py tests/test_prepare_r4_protected_runtime.py tests/test_build_r4_protected_candidate.py
git diff --check
```

Expected: PASS; wrong executable/argv/environment/stdin/timeout/cap, timeout/oversize/descendant residue, and every subprocess import outside the runner reject. Both normal Task2 CLIs run the real fixed Git adapter without test injection; f24 V1 producer/candidate digest incompatibility is rejected as retired V1 output, never normalized or accepted.

- [ ] **Step 7: Commit (2 minutes).**

```bash
git diff --check
git add config scripts tests/test_r4_protected_fixed_git.py tests/test_prepare_r4_protected_runtime.py tests/test_build_r4_protected_candidate.py
git commit -m "refactor: make r4 task2 input-only v2"
```

### Task 5A: Correct Task2c from final approval to review package

**Why this is an amendment:** the retained Task 5 examples call `build_task14_approval_v2` and write an approval. That behavior is obsolete. The current `scripts/build_r4_task14_approval.py`, its tests, and every Task 12 consumer must be migrated; an unprivileged script must not acquire Key A, inspect a launcher/interpreter, or sign a final approval.

**Files:** modify `scripts/build_r4_task14_approval.py`, `scripts/build_r4_protected_candidate.py`, `tests/test_build_r4_protected_candidate.py`, `tests/test_r4_protected_fixed_git.py`, `scripts/run_task13_exact_suite.py`, and `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/PythonTransportIntegrationTests.swift`.

- [ ] **Step 1: Write RED Task2c boundary tests (5 minutes).** Assert `build_task14_review_package_v2(repository, bundle, helper_build, exact_suite_log, output, runner=run_recorded_git)` returns only canonical `R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2`. AST and recording tests must reject `R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2`, `Ed25519PrivateKey`, `subprocess` except `r4_protected_fixed_git.run_recorded_git`, `/usr/bin/sandbox-exec`, `otool`, `codesign`, archive extraction, wheel installation, interpreter execution, and every Key-B name. The `--verify-only` CLI reads only an existing review package and cannot turn it into an approval.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_build_r4_protected_candidate tests.test_r4_protected_fixed_git -v`

Expected: FAIL because the existing Task2c builder/function name still constructs the obsolete final approval shape.

- [ ] **Step 3: Implement the review-package-only API (5 minutes).** Rename `build_task14_approval_v2` to `build_task14_review_package_v2`; accept `--review-package` rather than `--output`, and make the CLI verify exactly that canonical document. Its exact fields are the approved review-package field list: the V2 bundle, lock, archive, wheel-set, policy, Git head/tree/required-blob set, exact-suite log, candidate helper binary/designated-requirement/Team/Developer-ID evidence, `helper_hashes`, and `task14_approval_verification_public_key_sha256` copied from the reviewed Swift source constant. It obtains Git evidence solely with the existing fixed `/usr/bin/git` recorded grammar. It neither accepts a key file nor writes a signature. A V1 runtime manifest supplied as claimed materialization evidence is rejected.

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate tests.test_r4_protected_fixed_git -v`

Expected: PASS; Task2a/2b/2c do not execute an artifact and Task2c's only artifact is the review package.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add scripts/build_r4_task14_approval.py scripts/build_r4_protected_candidate.py tests/test_build_r4_protected_candidate.py tests/test_r4_protected_fixed_git.py scripts/run_task13_exact_suite.py tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/PythonTransportIntegrationTests.swift
git commit -m "fix: make task2c review-package only"
```

## Task 6: Completed baseline/history — original state machine and typed admission

**Files:** create `StateMachine.swift`, `Admission.swift`, `StateMachineTests.swift`, and `AdmissionTests.swift`; modify `Storage.swift`, `Fakes.swift`, and `main.swift`.

| Current state | Durable record/location | Allowed successor | Error/signal rule |
| --- | --- | --- | --- |
| `ABSENT` | no fixed-ID authority/work/quarantine/admission | `BUNDLE_VALIDATING` | pre-admission error creates nothing. |
| `BUNDLE_VALIDATING` | in-memory only | `ADMITTED` | any error returns `ABSENT`; no `O_EXCL`. |
| `ADMITTED` | immutable `control/admissions/r4-disjoint-custody-v2-execution-v1.json` | `PRIVATE_COPY` | any error becomes terminal `QUARANTINED`. |
| `PRIVATE_COPY` | root-owned `materialization-work/r4-disjoint-custody-v2-execution-v1.0123456789abcdef0123456789abcdef/state.json` | `EXTRACTING` | error/SIGINT/SIGTERM quarantines stage. |
| `EXTRACTING` | same state record | `INSTALLING` | error/signal quarantines. |
| `INSTALLING` | same state record | `POSTINSTALL_VALIDATING` | error/signal kills/reaps child then quarantines. |
| `POSTINSTALL_VALIDATING` | same state record | `SEALED` | error/signal quarantines. |
| `SEALED` | sealed stage receipt/signature/state | `PUBLISHED` | exclusive publish failure quarantines. |
| `PUBLISHED` | sealed `authorities/r4-disjoint-custody-v2-execution-v1/` | none | read-only fresh verify only. |
| `QUARANTINED` | `quarantine/r4-disjoint-custody-v2-execution-v1.0123456789abcdef0123456789abcdef/` or proven retained work stage | none | no retry/adopt/unseal/delete. |

- [ ] **Step 1: Write RED transition, concurrency, and signal tests (2 minutes).**

```swift
func testAdmissionAcceptsOnlyValidatedBundleAndPersistsBeforeStage() throws {
    let f = MaterializationFixture.valid()
    defer { try? f.removeOnlySelfOwnedFixture() }
    let bundle = try BundleVerifier.verifyBeforeAdmission(f.request, adapters: f.adapters)
    let _: (ValidatedBundle, Storage) throws -> Admission = AdmissionController.admit
    _ = try AdmissionController.admit(bundle, storage: f.storage)
    XCTAssertEqual(f.fakeFS.events.suffix(5), [.openExclusive, .writeAdmission, .fsyncAdmission,
        .immutableAdmission, .fsyncAdmissionsParent])
}
func testStateMachineMarksPostAdmissionFaultAndSignalTerminal() throws {
    for seam in StateMachine.Seam.allCases + [.sigint, .sigterm] {
        let f = MaterializationFixture.valid().fault(after: seam)
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let store = f.durableStore.throwingAt(seam)
        XCTAssertThrowsError(try StateMachine.transition(to: .privateCopy, using: store, admission: admission))
        XCTAssertEqual(f.fakeFS.admissionRecords.count, 1)
        XCTAssertTrue(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage))
    }
}
func testConcurrentAdmissionsHaveOneWinnerAndNoLoserMutation() throws {
    let f = MaterializationFixture.valid()
    defer { try? f.removeOnlySelfOwnedFixture() }
    let bundle = try BundleVerifier.verifyBeforeAdmission(f.request, adapters: f.adapters)
    let results = raceTwo(startBarrier: f.admissionBarrier) {
        try AdmissionController.admit(bundle, storage: f.storage)
    }
    let successfulAdmissions = results.compactMap { result -> Admission? in
        guard case let .success(admission) = result else { return nil }
        return admission
    }
    XCTAssertEqual(successfulAdmissions.count, 1)
    XCTAssertEqual(f.fakeFS.admissionRecords.count, 1)
    XCTAssertEqual(f.fakeFS.loserCleanupCalls, 0)
    XCTAssertEqual(f.fakeFS.loserMutations, 0)
}
func testOwnerDeathFsyncAndRebindAreTerminalNoRetry() throws {
    for seam in [AdmissionSeam.ownerDeath, .admissionFsync, .admissionRebind] {
        let f = MaterializationFixture.valid().admissionFault(seam)
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage))
        XCTAssertEqual(f.fakeFS.admissionRecords.count, 1)
        XCTAssertTrue(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage))
    }
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'StateMachineTests|AdmissionTests'`

Historical baseline: do not run this RED command at current HEAD; Task 7A supplies the truthful expanded-graph migration RED command before Task 8.

- [ ] **Step 3: Implement durable transitions and admission (5 minutes).** Persist canonical `state.json` with exact keys `kind`, `namespace`, `authority_id`, `attempt_uuid`, `state`, `admission_record_sha256`, `sha256`; create/write/fsync/rebind it on each transition below a root-owned stage. `AdmissionController.admit` obtains `ValidatedBundle` only, rejects any existing admission/authority/work/quarantine, calls `openat(O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600)`, writes canonical V2 admission containing approval/bundle/policy/UUID/volume, fsyncs, chmods 0444, immutable-readbacks, fsyncs/rebinds parent, then returns `Admission`. `BarrierBackedAdmissionStorage` waits until both callers have reached the `openat` seam and delegates both calls to real fake `O_EXCL`, never a simulated winner flag.

```swift
static func transition(to state: MaterializationState, using store: DurableStateStore,
                       admission: Admission) throws {
    let allowedSuccessor: [MaterializationState: MaterializationState] = [
        .absent: .bundleValidating, .bundleValidating: .admitted, .admitted: .privateCopy,
        .privateCopy: .extracting, .extracting: .installing, .installing: .postinstallValidating,
        .postinstallValidating: .sealed, .sealed: .published
    ]
    guard allowedSuccessor[store.currentState] == state else {
        throw ProvisionError.privateStageInvalid
    }
    do { try store.persist(state, admission: admission); try store.fsyncAndRebind(state) }
    catch { try store.quarantineAfterFailure(admission); throw error }
}
```

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'StateMachineTests|AdmissionTests'`

Expected: PASS; `raceTwo` has exactly one `O_EXCL` success, one durable admission record, zero losing cleanup/mutations, and owner death/fsync/rebind/signal failures are terminal with no retry. Every input/approval/swap test in Task4 remains before `O_EXCL`.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority
git commit -m "feat: persist r4 materialization state"
```

## Task 7: Completed baseline/history — original copy/extraction/final-layout placement

**Files:** create `Archive.swift`, `ArchiveTests.swift`; modify `BundleTraversal.swift` and `Fakes.swift`.

- [ ] **Step 1: Write RED copy/extraction/rename tests (2 minutes).**

```swift
func testCopyRebindsEveryAcceptedBundleObject() throws {
    for seam in CopySeam.allCases {
        let f = MaterializationFixture.valid().swapBefore(seam)
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try BundleCopier.copy(f.validatedBundle, into: f.stage.input, fs: f.fakeFS))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }
}
func testArchiveRenameUsesFinalLayoutBeforeAnyInspection() throws {
    let f = MaterializationFixture.valid()
    defer { try? f.removeOnlySelfOwnedFixture() }
    try ArchiveInstaller.extractAndPlace(stage: f.stage, archive: f.validArchive, fs: f.fakeFS)
    XCTAssertEqual(f.fakeFS.events.suffix(5), [.rebindSource, .rename, .fsyncUnpack,
        .fsyncRuntime, .rebindDestination])
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter ArchiveTests`

Historical baseline: do not run this RED command at current HEAD; Task 7A supplies the truthful expanded-graph migration RED command.

- [ ] **Step 3: Implement descriptor operations (5 minutes).** Create random root-owned 0700 stage under fixed work parent; descriptor-copy accepted bundle objects to logical `input/` locations, fsync each file and parent, close/reopen/rebind and compare held type/owner/mode/nlink/size/hash/identity. Parse copied archive in Swift; allow only normalized ASCII directories/regular files, deny absolute/`.`/`..`, duplicate/case-fold collision, symlink/hardlink/device/FIFO/sparse/unsafe extended member, more than 100000 members or 8589934592 bytes. Require `unpack/python/bin/python3.11`; descriptor-rebind source, require destination absent, `renameat` to `runtime/closure/python311`, fsync both parents, rebind destination and compare tree digest. No Mach-O/pip/import process can refer to `unpack/`.

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter ArchiveTests`

Expected: PASS; `PRIVATE_COPY` then `EXTRACTING` never uses an untrusted pathname after validation.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority
git commit -m "feat: extract authenticated r4 materialization input"
```

### Task 7A: Migration — rebind copied-input, admission, and archive tests to the expanded V2 graph

**Files:** modify `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{BundleTraversal,Admission,Archive,Storage}.swift` and `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{BundleTraversal,Admission,Archive,Storage,Fakes}Tests.swift`.

- [ ] **Step 1: Write current-HEAD RED graph-migration tests (5 minutes).** Require `HeldBundle.openAndValidate` to return Key-A approval/review-package bindings; require `AdmissionController` to record no future launcher/interpreter/runtime output digest; require `ArchiveInstaller.extractAndPlace` to return a final-layout held interpreter capability for both Task 8 and candidate observation; and require all existing swap/rename/failure tests to assert no child launch.

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'BundleTraversalTests|AdmissionTests|ArchiveTests|StorageTests'`

Expected: FAIL because current HEAD's partial `MaterializationPolicy`, `Task14ApprovalV2`, and fakes cannot supply the new closed bindings.

- [ ] **Step 3: Implement the migration and run GREEN (5 minutes).** Preserve exactly one `O_EXCL` admission record with only approval/input/policy/attempt/volume fields. Thread `HeldCeremonyInputs` only through the candidate observation path; do not add it to materialization admission. Reuse archive extraction/final-layout rename with no process callback. Then run the RED command and `git diff --check`; expected PASS with zero fake launches on every traversal/admission/archive failure seam.

- [ ] **Step 4: Commit (2 minutes).**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{BundleTraversal,Admission,Archive,Storage}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{BundleTraversal,Admission,Archive,Storage,Fakes}Tests.swift
git commit -m "fix: migrate r4 preinstaller graph bindings"
```

## Task 8: Enforce pinned launcher-only execution, Mach-O identities, and bounded root-only offline installation

**Files:** modify `config/r4_privileged_materialization_policy.json`, `schemas/r4_privileged_materialization_policy_v2.schema.json`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Models,MachO,ProcessRunner,Adapters,BundleTraversal}.swift`, and `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{ModelsV2,MachO,ProcessRunner,Fakes}Tests.swift`.

The canonical V2 policy has exactly `kind`, `namespace`, `authority_id`, `python_version`, `archive`, `wheel_filenames`, `sandbox_launcher`, `base_interpreter`, `child_execution`, `allowed_loader_roots`, `import_modules`, `import_probe_sha256`, `schema_versions`, `sha256`. `archive` is exactly `archive_root=python`, `final_runtime_path=runtime/closure/python311`, `entrypoint=runtime/closure/python311/bin/python3.11`, `max_members=100000`, `max_unpacked_bytes=8589934592`. `sandbox_launcher` is exactly `path=/usr/bin/sandbox-exec`, `descriptor_identity_sha256`, `sha256`, and `codesign_identity`; `base_interpreter` is exactly `path=runtime/closure/python311/bin/python3.11`, `sha256`, `mach_o_sha256`, and `codesign_identity`. `child_execution` has exactly `cwd=runtime`, the nine ordered environment strings, exact `pip_argv`, exact `import_argv`, `timeout_seconds=300`, `max_output_bytes=1000000`, `sandbox_profile_utf8`, `sandbox_profile_template_sha256`, `child_command_template_sha256`, ordered `read_allow`, ordered `write_allow`, and `network_denied=true`.

The canonical stage token is the ASCII literal `__R4_STAGE__`; it is part of policy hashing and is substituted with an authenticated root-owned absolute stage path only after descriptor rebind. The nine exact ordered environment values are `HOME=__R4_STAGE__/home`, `LANG=C`, `LC_ALL=C`, `PATH=/usr/bin:/bin`, `PIP_CONFIG_FILE=/dev/null`, `PIP_DISABLE_PIP_VERSION_CHECK=1`, `PIP_NO_INPUT=1`, `PYTHONNOUSERSITE=1`, `TMPDIR=__R4_STAGE__/tmp`. The exact import probe source UTF-8 bytes are:

```python
import importlib,json,sys
mods=("cryptography","jsonschema","numpy","scipy")
print(json.dumps({"version":sys.version.split()[0],"paths":sys.path,"files":[importlib.import_module(name).__file__ for name in mods]},sort_keys=True,separators=(",",":")))
```

The literal Seatbelt profile UTF-8 bytes are:

```text
(version 1)
(deny default)
(deny network*)
(allow file-read* (subpath "__R4_STAGE__/runtime/closure/python311") (subpath "__R4_STAGE__/wheelhouse") (subpath "__R4_STAGE__/home") (subpath "__R4_STAGE__/tmp") (literal "/dev/null") (subpath "/System") (subpath "/usr/lib"))
(allow file-write* (subpath "__R4_STAGE__/runtime") (subpath "__R4_STAGE__/home") (subpath "__R4_STAGE__/tmp"))
```

`sandbox_profile_template_sha256` is `SHA256("R4V2_SANDBOX_PROFILE_TEMPLATE\0" || profile UTF-8 bytes)` and `child_command_template_sha256` is `SHA256("R4V2_CHILD_COMMAND_TEMPLATE\0" || canonical_bytes({launcher_path,pip_argv_template,import_argv_template,cwd,environment,stdin,stdout_stderr,timeout_seconds,max_output_bytes,sandbox_profile_template_sha256}))`. `read_allow` is `runtime/closure/python311`, `wheelhouse`, `home`, `tmp`, `/dev/null`, `/System`, `/usr/lib`; `write_allow` is `runtime`, `home`, `tmp`; loader roots are `runtime/closure/python311`, `/System/`, `/usr/lib/`; modules are `cryptography`, `jsonschema`, `numpy`, `scipy`; schema versions are the six V2 output kinds in the approved order. `sandbox_launcher.descriptor_identity_sha256` is `SHA256("R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0" || canonical_bytes({path,st_dev,st_ino,st_gen,mode,nlink,size}))`; `codesign_identity.sha256` is `SHA256("R4V2_CODESIGN_IDENTITY\0" || canonical_bytes({identifier,designated_requirement,team_identifier,certificate_chain_sha256}))`; `certificate_chain_sha256` is `SHA256("R4V2_CERTIFICATE_CHAIN\0" || canonical_bytes(ordered DER-certificate hashes))`; and the interpreter Mach-O observation is `SHA256("R4V2_MACHO_OBSERVATION\0" || canonical_bytes({path,slices,dependencies}))`.

- [ ] **Step 1: Write RED policy, loader, and process tests (2 minutes).**

```swift
func testPolicyRejectsAnyFieldByteOrOrderChange() throws {
    for mutation in [wrongProfileByte, wrongProfileHash, reorderedEnvironment, extraEnvironment,
                     wrongCwd, wrongPipArgv, extraLoaderRoot, wrongImportModule] {
        XCTAssertThrowsError(try MaterializationPolicy.decode(mutation.bytes))
    }
}
func testOnlyPinnedReboundLauncherCanLaunchAChild() throws {
    for target in ["/dev/fd/7", stage + "/unpack/python/bin/python3.11", stage + "/runtime/closure/python311/bin/python3.11", "/bin/sh", "/usr/bin/env"] {
        XCTAssertThrowsError(try fakeProcess.launch(program: target))
    }
    XCTAssertEqual(try fakeProcess.launch(program: "/usr/bin/sandbox-exec", argv: fixture.expectedPipArgv).program,
                   "/usr/bin/sandbox-exec")
    XCTAssertThrowsError(try MachOValidator.validate(fixtureWithAmbiguousRPath))
}
func testLauncherReplacementSecuritySeamAndInterpreterDriftLaunchNothing() throws {
    for seam in LauncherSeam.allCases + InterpreterSeam.allCases {
        let f = MaterializationFixture.valid().replaceAt(seam)
        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(f.validPlan))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }
}
func testPipArgvUsesOnlyAbsoluteWheelOperandsInLockOrder() throws {
    let f = MaterializationFixture.valid()
    let argv = try ProcessRunner.pipPlan(f.validPlan).argv
    XCTAssertEqual(Array(argv.suffix(f.policy.wheelFilenames.count)),
        f.policy.wheelFilenames.map { f.stage.path + "/wheelhouse/" + $0 })
    XCTAssertFalse(argv.contains("--find-links"))
    XCTAssertFalse(argv.contains(where: { f.policy.wheelFilenames.contains($0) }))
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MachOTests|ProcessRunnerTests'`

Expected: FAIL because current HEAD's partial policy/parser/Mach-O/process types do not enforce the pinned launcher, exact absolute wheel operands, or complete actual-command identity object.

- [ ] **Step 3: Implement the final-layout loader and identity gate (5 minutes).** Parse every final staged runtime Mach-O descriptor and resolve `LC_LOAD_DYLIB`, `LC_LOAD_WEAK_DYLIB`, `LC_REEXPORT_DYLIB`, `@loader_path`, `@executable_path`, and `@rpath`; allow only a descriptor-rebound staged object, `/System/`, or `/usr/lib/`. Descriptor-walk trusted `/usr`, open only `sandbox-exec` no-follow, and rebind/hash before and after the public Security calls. Require launcher identifier `com.apple.sandbox-exec`, requirement `identifier "com.apple.sandbox-exec" and anchor apple`, null Team ID, and policy/approval descriptor/byte/code-sign digests. Require the final held interpreter and every staged Mach-O to pass strict public-Security validation and match the policy/approval byte/Mach-O/code-sign tuple before installation, after installation, and after the import probe.

- [ ] **Step 4: Implement sole-launcher command construction and process adapter (5 minutes).** `ProcessRunner` exposes `spawnPinnedLauncher(_:)`, not a direct-interpreter API. It accepts a revalidated launcher, revalidated interpreter, and closed child plan; it rejects any program other than `/usr/bin/sandbox-exec`, all forbidden inherited environment rows and descriptors, and every alternative execution route. It invokes `posix_spawn` with `/usr/bin/sandbox-exec`, stdin `/dev/null`, root-owned capped stdout/stderr pipes, a new process group, TERM/KILL reap, timeout 300, and output cap 1000000.

```swift
let pipArgv = ["/usr/bin/sandbox-exec", "-p", actualProfile, finalInterpreterPath]
    + ["-I", "-s", "-E", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
       "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check"]
    + policy.wheelFilenames.map { stagePath + "/wheelhouse/" + $0 }
let plan = try ChildExecutionPlan(program: "/usr/bin/sandbox-exec", argv: pipArgv,
    cwd: stagePath + "/runtime", environment: policy.reboundEnvironment(stage: stagePath),
    stdin: "/dev/null", stdoutStderr: "ROOT_OWNED_CAPPED_PIPES", timeoutSeconds: 300,
    maxOutputBytes: 1_000_000, launcher: launcher, interpreter: interpreter, profile: actualProfile)
let result = try ProcessRunner.spawnPinnedLauncher(plan)
```

The wheel suffix is each exact root-owned absolute `stagePath + "/wheelhouse/" + filename` in lock/policy order. `--find-links`, bare filenames, URLs, requirement specifiers, resolver-selected operands, a shell, copied launcher, `PATH`, `/dev/fd`, `fexecve`, `execveat`, and a direct interpreter program fail. Hash the complete actual command object from Task 1A as `R4V2_INSTALL_COMMAND\0` and `R4V2_IMPORT_COMMAND\0`.

- [ ] **Step 5: Implement post-install probe and output observation (5 minutes).** Revalidate launcher/interpreter before the probe, then call the same launcher with argv `[/usr/bin/sandbox-exec, -p, actual-profile, final-held-interpreter-path, -I, -s, -E, -c, import-probe-bytes]`. Require the bounded report's version/module files/`sys.path` under `runtime/`; re-enumerate runtime entries/directories and rerun Mach-O/signature closure including extensions. Form the actual profile by exactly one literal replacement of `__R4_STAGE__` with the descriptor-rebound stage path; calculate the actual-profile/environment/output/dependency and both command digests from observed values.

- [ ] **Step 6: Run GREEN (2 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter 'MachOTests|ProcessRunnerTests'
swift test --package-path tools/r4_protected_execution_authority
```

Expected: PASS; every recorded child program is `/usr/bin/sandbox-exec`, with exact `-p` profile and final held interpreter operand. Profile activation failure and network/resolver/index/cache/build/output-limit/timeout/descendant/loader/launcher/interpreter drift reject before launch.

- [ ] **Step 7: Commit (2 minutes).**

```bash
git diff --check
git add config/r4_privileged_materialization_policy.json schemas/r4_privileged_materialization_policy_v2.schema.json tools/r4_protected_execution_authority
git commit -m "feat: constrain privileged runtime materialization"
```

## Task 9: Build V2 observed closure, receipt/keyring verification, seal, and no-replace publish

**Files:** create `Materialize.swift`, `Keyring.swift`, `Verify.swift`, `MaterializeTests.swift`, `KeyringTests.swift`, and `VerifyTests.swift`; modify `StateMachine.swift`, `main.swift`, and fakes.

Task 9 consumes Task 8's `InstallationObservation`, not a caller command or predicted closure. `RuntimeManifestV2` writes the V2 input/policy/wheel hashes; launcher path/descriptor/byte/code-sign values; profile-template/actual-profile and command-template hashes; installer/import actual-command hashes; interpreter pre-install/post-install byte/Mach-O/code-sign values; import-probe/environment hashes; cwd; entries/output closure; and dependency closure. `ReceiptV2` repeats the approved runtime identities, binds `admission_record_sha256`, approval/helper/Git/key-B fields, and has `state == "SEALED"`. `Verify.active` must pin sealed bootstrap Key A and approval before contiguous Key-B keyring, receipt, and revocation verification; it rejects Key-A/Key-B role confusion and cannot write a keyring record.

- [ ] **Step 1: Write RED seal/receipt/revocation/residue tests (2 minutes).**

```swift
func testReceiptSignsExactlyObservedV2GraphAfterInstallation() throws {
    let f = MaterializationFixture.valid()
    defer { try? f.removeOnlySelfOwnedFixture() }
    let receipt = try Materializer.run(f.request, adapters: f.adapters)
    XCTAssertEqual(receipt.state, "SEALED")
    XCTAssertEqual(receipt.runtimeOutputClosureSHA256, f.runtime.entriesSHA256)
    XCTAssertEqual(receipt.admissionRecordSHA256, f.admission.sha256)
    XCTAssertEqual(f.fakeFS.events.suffix(6), [.writeReceipt, .fsyncReceipt, .writeSignature,
        .fsyncSignature, .sealTree, .renameExclusive])
}
func testGapBadSignatureRevocationAndUnsafeCleanupFailClosed() throws {
    for f in [gapKeyring, badReceiptSignature, revokedAuthority, userBundleSentinel, oldCustodySentinel] {
        XCTAssertThrowsError(try Verify.active(f))
        XCTAssertEqual(f.userOwnedMutations, [])
    }
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|KeyringTests|VerifyTests'`

Expected: FAIL because observed manifests, receipt signing, verification, sealing, and exclusive publication are absent.

- [ ] **Step 3: Implement Materializer orchestration and receipt creation (5 minutes).** This is the first task that calls `Materializer.run`: it executes `verifyBeforeAdmission`, typed `admit`, `BundleCopier.copy`, `ArchiveInstaller.extractAndPlace`, then Task 8's `installAndProbe` and creates manifests only from `InstallationObservation`. `ReceiptFactory.create` binds approval/bundle/lock/policy/wheel/runtime/output/dependency/admission/helper/Git/Key-B/public-key/published-closure fields plus the complete Task 8 launcher, profile, command, interpreter, and environment values; it validates every Task 1A cross-document equality and never accepts a Python-produced runtime manifest.

```swift
let receipt = try ReceiptFactory.create(admission: admission, runtime: runtimeManifest,
    authority: authorityManifest, bootstrap: bootstrap, policy: bundle.policy)
try stage.writeCanonical("receipt.json", receipt.canonicalBytes)
try stage.fsyncFileAndParent("receipt.json")
```

- [ ] **Step 4: Implement detached signing (2 minutes).** Use only bootstrap generation-1 or a contiguous active keyring key; sign complete canonical receipt bytes, require exactly 64 signature bytes, write `receipt.ed25519`, fsync it and its parent.

```swift
let signature = try keyring.activeKey.sign(receipt.canonicalBytes)
guard signature.count == 64 else { throw ProvisionError.sealReadbackFailed }
try stage.writeBytes("receipt.ed25519", signature)
try stage.fsyncFileAndParent("receipt.ed25519")
```

- [ ] **Step 5: Implement sealing and publish (2 minutes).** Rebind complete tree; set dirs 0555, regular files 0444, private keys 0600; verify root:wheel/ACL-free/name identity and `SF_IMMUTABLE` readback. Call `renameatx_np(RENAME_EXCL)` only after fresh staged verification, then fsync authorities parent. On any post-admission failure, `Quarantine.moveOrRetain(stage, admission)` preserves a root-owned terminal state.

- [ ] **Step 6: Implement read-only verification (2 minutes).** `Verify.active` pins bootstrap, verifies contiguous keyring, receipt signature, V2 cross-hashes/closure, and contiguous revocation state. It exposes no control write method.

- [ ] **Step 7: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|KeyringTests|VerifyTests'`

Expected: PASS; a failed quarantine move retains verified work as quarantine, success removes only re-bound root-owned unsealed work children, and no user/prior/custody sentinel is mutated.

- [ ] **Step 8: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority
git commit -m "feat: seal v2 materialized authority"
```

## Task 10: Run real Darwin conformance separately from fakes

**Files:** create `DarwinConformanceTests.swift`; modify `POSIX.swift`, `Adapters.swift`, and `tests/test_r4_protected_execution_authority.py`.

- [ ] **Step 1: Write RED real-Darwin tests (2 minutes).**

```swift
func testPrivateTMPAPFSControlsAndNoReplace() throws {
    let root = try DarwinDisposableRoot.create(prefix: "r4-materialization-darwin-")
    defer { try? root.removeOnlySelfOwnedFixture() }
    XCTAssertEqual(try DarwinFileSystem.volumeInfo(root.path).filesystem, "apfs")
    try DarwinFileSystem.setAndReadBackImmutable(root.fixtureFile)
    try DarwinFileSystem.renameExclusive(root.source, root.destination)
    XCTAssertEqual(try root.read(root.destination), Data("source".utf8))
    try root.write(root.existingDestination, Data("original".utf8))
    XCTAssertThrowsError(try DarwinFileSystem.renameExclusive(root.secondSource, root.existingDestination))
    XCTAssertEqual(try root.read(root.existingDestination), Data("original".utf8))
    XCTAssertThrowsError(try DarwinFileSystem.renameExclusiveUnavailableFallback(root.thirdSource, root.thirdDestination))
}
func testActualPinnedLauncherSpawnAndSeatbeltFailureAreTerminal() throws {
    let fixture = try DarwinDisposableRoot.create(prefix: "r4-materialization-darwin-")
    defer { try? fixture.removeOnlySelfOwnedFixture() }
    XCTAssertEqual(try DarwinProcessRunner.spawnPinnedLauncher(
        launcher: "/usr/bin/sandbox-exec", profile: fixture.validSeatbeltProfile,
        interpreter: fixture.fixedStagedExecutable, pythonArgv: ["-c", "import sys;sys.exit(0)"]), 0)
    XCTAssertThrowsError(try DarwinProcessRunner.spawnPinnedLauncher(
        launcher: "/usr/bin/sandbox-exec", profile: "(version 1) (invalid)",
        interpreter: fixture.fixedStagedExecutable, pythonArgv: ["-c", "import sys;sys.exit(0)"]))
    XCTAssertThrowsError(try DarwinProcessRunner.rejectPseudoDescriptorPath("/dev/fd/7"))
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter DarwinConformanceTests`

Expected: FAIL because actual APFS/renameatx/immutable/spawn Seatbelt conformance cases do not exist.

- [ ] **Step 3: Implement real-only adapter calls (5 minutes).** `DarwinDisposableRoot.create` makes only `/private/tmp/r4-materialization-darwin-` followed by `UUID().uuidString`, and initializes `source`, `secondSource`, `thirdSource`, absent `destination` and `thirdDestination`, pre-existing `existingDestination`, `fixtureFile`, `fixedStagedExecutable`, and `validSeatbeltProfile`; `removeOnlySelfOwnedFixture` refuses anything outside that canonical prefix. Query `URLResourceKey.volumeUUIDStringKey`, `volumeIsLocalKey`, `volumeIsInternalKey`, `volumeIsRemovableKey`, `volumeIsReadOnlyKey`, `isUbiquitousKey`, then `fstatfs` APFS and descriptor xattrs. `renameExclusive` calls `renameatx_np(sourceFD, sourceName, destinationFD, destinationName, RENAME_EXCL)` and the test proves its successful source-to-absent-destination rename; the pre-existing destination rejection leaves its `original` bytes unchanged. `renameExclusiveUnavailableFallback` returns `ProvisionError.publishExclusiveUnavailable` without calling `renameat`. Exercise `fchflags(SF_IMMUTABLE)` plus `fstat` readback and real `posix_spawn` only of `/usr/bin/sandbox-exec` with argv `[launcher, "-p", valid-profile, fixed-staged-interpreter, "-c", "import sys;sys.exit(0)"]`; verify the no-follow launcher descriptor/byte/Security identity before/after and reject the invalid profile through the launcher exit path. The test skips only with an explicit unsupported-platform assertion; it never substitutes fake success or names production root.

- [ ] **Step 4: Run GREEN (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter DarwinConformanceTests`

Expected: PASS on a supported local APFS Mac; unsupported Darwin controls fail closed in production adapter tests.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add tools/r4_protected_execution_authority tests/test_r4_protected_execution_authority.py
git commit -m "test: prove r4 materialization darwin controls"
```

## Task 11: Add read-only V2 protected capture and preserve legacy Task3

**Files:** create/modify `mlb_backtest/v52_r4_protected_execution.py`, `mlb_backtest/v52_r4_disjoint_issuer.py`, `tests/test_v52_r4_protected_execution.py`, and `tests/test_r4_protected_execution_authority.py`.

- [ ] **Step 1: Write RED protected/legacy isolation tests (2 minutes).**

```python
def test_capture_is_zero_argument_and_v2_only(self) -> None:
    self.assertEqual(tuple(inspect.signature(capture_execution_binding).parameters), ())
    self.assertTrue(capture_execution_binding_for_test(self.v2_fixture).repository_clean)
    with self.assertRaises(ProtectedExecutionUnavailable):
        capture_execution_binding_for_test(self.v1_fixture)

def test_legacy_branch_never_uses_protected_capture(self) -> None:
    with patch("mlb_backtest.v52_r4_disjoint_issuer.capture_protected_execution_binding") as protected:
        build_execution_context(authority_id="legacy", legacy=self.legacy_inputs)
    protected.assert_not_called()
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_protected_execution -v`

Expected: FAIL because V2-only read-only capture and branch selector are absent.

- [ ] **Step 3: Implement minimal reader (5 minutes).** `capture_execution_binding()` invokes `_capture_from_root(PRODUCTION_ROOT, DescriptorOps.real())` only in later production use; tests use `capture_execution_binding_for_test(disposable_root, ops)`. It verifies sealed Key-A bootstrap objects and compiled-key hash equality, contiguous Key-B keyring, receipt/revocation, closed V2 manifests, and every input/policy/launcher/interpreter/profile/command/environment/output/dependency cross-hash with no extra entry. It never starts helper/process/network or opens a worktree. Issuer selects it only for fixed ID and retains the legacy signature `capture_execution_binding(repository, executable, target_volume_uuid=target_volume_uuid, supersession_sha256=supersession_sha256)` behavior.

- [ ] **Step 4: Run GREEN (2 minutes).**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_v52_r4_disjoint_execution tests.test_v52_r4_protected_execution tests.test_r4_protected_execution_authority -v
/opt/homebrew/bin/python3.11 -m ruff check mlb_backtest/v52_r4_protected_execution.py mlb_backtest/v52_r4_disjoint_issuer.py tests/test_v52_r4_protected_execution.py tests/test_r4_protected_execution_authority.py
```

Expected: PASS; production root is merely a fixed read-only constant until later exact approval.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git diff --check
git add mlb_backtest tests/test_v52_r4_protected_execution.py tests/test_r4_protected_execution_authority.py
git commit -m "feat: read v2 protected materialization authority"
```

## Task 12: Register the exact suite and produce the non-privileged Task14 review package

**Files:** modify `scripts/run_task13_exact_suite.py` and `tests/test_task13_exact_suite_runner.py`; evidence files are created only outside Git under `/private/tmp/r4-v2-materialization-$(git rev-parse HEAD)`.

- [ ] **Step 1: Write exact-suite ordering RED test (2 minutes).**

```python
def test_v2_materialization_modules_append_once_after_existing_children(self) -> None:
    expected = ("tests.test_prepare_r4_protected_runtime", "tests.test_build_r4_protected_candidate",
                "tests.test_r4_protected_execution_authority", "tests.test_v52_r4_protected_execution")
    self.assertEqual(CHILD_MODULES[-4:], expected)
    self.assertTrue(all(CHILD_MODULES.count(name) == 1 for name in expected))
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_task13_exact_suite_runner -v`

Expected: FAIL because V2 modules are not registered in fixed order.

- [ ] **Step 3: Implement ordering (2 minutes).** Append the four modules exactly once after the existing final child; do not reorder preceding children.

- [ ] **Step 4: Run GREEN (2 minutes).**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_task13_exact_suite_runner -v
/opt/homebrew/bin/python3.11 -m ruff check scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git diff --check
```

Expected: PASS; current child order is otherwise unchanged.

- [ ] **Step 5: Commit (2 minutes).**

```bash
git add scripts/run_task13_exact_suite.py tests/test_task13_exact_suite_runner.py
git commit -m "test: add v2 materialization exact suite"
```

- [ ] **Step 6: Run the exact approved suite and record reproducible evidence (5 minutes).**

```bash
reviewed_head="$(git rev-parse HEAD)"
evidence_root="/private/tmp/r4-v2-materialization-${reviewed_head}"
test ! -e "$evidence_root"
mkdir -m 700 "$evidence_root"
set +e
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/run_task13_exact_suite.py \
  --repo . \
  --v51-input-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --old-v52-evidence-root '/Users/taejunoh/Documents/MLB Analysis/.worktrees/mlb-v5-1-backtest' \
  --chromium-executable '/Users/taejunoh/.cache/puppeteer/chrome-headless-shell/mac_arm-146.0.7680.153/chrome-headless-shell-mac-arm64/chrome-headless-shell' \
  --chromium-sha256 454577a889a2c0b698c09f0ca8db047ea2e6cd604a42c36dc09b375c81362518 \
  --chromium-bundle-sha256 2ea5035c2085aaf0fb70be6d2949f51eeb42affaac47772044ddaa727cb4c253 \
  --python /opt/homebrew/bin/python3.11 >"$evidence_root/exact-suite.log" 2>&1
exact_suite_status=$?
set -e
printf '%s\n' "$exact_suite_status" >"$evidence_root/exact-suite.exit"
test "$exact_suite_status" -eq 0
/usr/bin/shasum -a 256 "$evidence_root/exact-suite.log" >"$evidence_root/exact-suite.log.sha256"
test "$(/usr/bin/awk '{print $1}' "$evidence_root/exact-suite.log.sha256")" = "$(/usr/bin/shasum -a 256 "$evidence_root/exact-suite.log" | /usr/bin/awk '{print $1}')"
```

Expected: exit file contains `0`; log reports all child modules passed; log digest has exactly 64 lowercase hex characters; no production root or custody action occurs.

- [ ] **Step 7: Generate and byte-verify review evidence without materialization (5 minutes).** These are normal Task2b/Task2c CLI invocations: they pass no runner argument, so each must reach `run_recorded_git` through the production default wiring established in Task 5.

```bash
bundle_root="$evidence_root/input-bundle"
review_package="$evidence_root/task14-review-package-v2.json"
swift build -c release --package-path tools/r4_protected_execution_authority --scratch-path "$evidence_root/swift-build"
helper_build="$(swift build -c release --show-bin-path --package-path tools/r4_protected_execution_authority --scratch-path "$evidence_root/swift-build")"
helper_binary="$helper_build/R4ProtectedExecutionProvisioner"
test -x "$helper_binary"
/usr/bin/ditto "$helper_binary" "$evidence_root/provisioner"
/usr/bin/codesign --force --options runtime --timestamp --sign 'Developer ID Application: Taejun Oh (3BMF4LM6TM)' "$evidence_root/provisioner"
/usr/bin/codesign --verify --strict --deep --verbose=4 "$evidence_root/provisioner"
/usr/bin/codesign -d -r- "$evidence_root/provisioner" >"$evidence_root/provisioner.codesign-requirement.txt" 2>&1
/usr/bin/codesign -dvv "$evidence_root/provisioner" >"$evidence_root/provisioner.codesign-details.txt" 2>&1
/usr/bin/grep -Fx 'TeamIdentifier=3BMF4LM6TM' "$evidence_root/provisioner.codesign-details.txt"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/verify_r4_protected_inputs.py --lock "$PWD/config/r4_protected_python311.lock.json" --downloads "$PWD/.artifacts/r4-protected-python311/downloads" >"$evidence_root/input-verification.json"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/build_r4_protected_candidate.py --source "$PWD" --downloads "$PWD/.artifacts/r4-protected-python311/downloads" --lock "$PWD/config/r4_protected_python311.lock.json" --policy "$PWD/config/r4_privileged_materialization_policy.json" --output "$bundle_root"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/build_r4_task14_approval.py --repository "$PWD" --bundle "$bundle_root" --helper-build "$evidence_root" --exact-suite-log "$evidence_root/exact-suite.log" --review-package "$review_package"
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 scripts/build_r4_task14_approval.py --verify-only --review-package "$review_package"
/usr/bin/shasum -a 256 "$review_package" >"$evidence_root/task14-review-package-v2.sha256"
printf '%s\n' "/private/var/db/mlb-walkforward-lab/task14-ceremony-authority/outputs/${reviewed_head}" >"$evidence_root/task14-expected-output-path.txt"
```

Expected: the review package binds exact V2 bundle/lock/archive/wheels/policy/head/tree/required blobs/exhaustive checkout/helper binary+requirement+Team/compile-time Key-A hash/complete exact suite; its second invocation verifies byte identity. It has no launcher, interpreter, Key-B, runtime, authority, receipt, final-approval, or signature field. `task14-expected-output-path.txt` records but does not create the later root-owned output location. These commands create only `/private/tmp` evidence and user-owned bundle artifacts.

- [ ] **Step 8: Perform final source checks and stop (2 minutes).**

```bash
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_prepare_r4_protected_runtime tests.test_build_r4_protected_candidate tests.test_r4_protected_execution_authority tests.test_v52_r4_protected_execution tests.test_task13_exact_suite_runner -v
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_protocols -v
swift test --package-path tools/r4_protected_execution_authority --scratch-path "$evidence_root/swift-build"
git diff --check
```

Expected: PASS. Task 12 ends with a review package only. It does not authorize the Task14 root-only observation ceremony, `bootstrap-install`, `materialize-and-provision`, production `verify`, or custody work.

## Task 13: Implement and test the candidate-only approval-preimage observer

**Files:** create `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/ObservationCeremony.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/CeremonyOutputRegistry.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ObservationCeremonyTests.swift`, and `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CeremonyOutputRegistryTests.swift`; modify `CLI.swift`, `main.swift`, `Adapters.swift`, `Archive.swift`, `MachO.swift`, `Models.swift`, `Fakes.swift`, `Package.swift`, and `tests/test_protocols.py`.

`ObservationCeremony.run(_:)` is compiled into the reviewed candidate executable but is not an installed-helper command. `CLI.parseInstalled` rejects `task14-observe-approval-preimages` unconditionally; `CandidateCLI.parseObservation` is reachable only when `LoadedExecutable.requireNonInstalledCandidate` has rebound the executing binary to the review-package helper digest, designated requirement, Team ID, and Developer-ID identity. It has no bootstrap, Key-B, admission, runtime-materialization, receipt, `ProcessRunner`, network, or IPC dependency.

- [ ] **Step 1: Write current-HEAD RED candidate-only tests (5 minutes).**

```swift
func testCandidateObservationGrammarIsClosedAndInstalledHelperRejectsIt() throws {
    let argv = ["task14-observe-approval-preimages", "--ceremony-root", fixture.ceremonyAuthority,
        "--review-package", fixture.inputs.reviewPackage, "--observation-authorization", fixture.inputs.authorization,
        "--observation-authorization-signature", fixture.inputs.authorizationSignature,
        "--task14-approval-verification-public-key", fixture.inputs.keyAPublic,
        "--input-bundle", fixture.untrustedBundle]
    XCTAssertNoThrow(try CandidateCLI.parseObservation(argv))
    XCTAssertThrowsError(try CLI.parseInstalled(argv))
}
func testObservationCreatesOnlyApprovalAndSignatureWithoutChild() throws {
    let f = ObservationFixture.valid()
    let result = try ObservationCeremony.run(f.request, adapters: f.adapters)
    XCTAssertEqual(result.children, ["task14-approval-v2.ed25519", "task14-approval-v2.json"])
    XCTAssertEqual(f.fakeProcess.launches, [])
    XCTAssertFalse(f.fakeFS.contains("wheelhouse"))
    XCTAssertFalse(f.fakeFS.contains("runtime-manifest.json"))
}
func testObservationOutputAttemptIsExclusiveAndNeverUsesUserEvidenceAncestor() throws {
    for mutation in [userWritableOutputAncestor, fileProviderOutputParent, outputACL, existingAttempt] {
        let f = ObservationFixture.valid().mutating(mutation)
        XCTAssertThrowsError(try ObservationCeremony.run(f.request, adapters: f.adapters))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }
}
func testRegistryCreatesOneExclusiveAttemptAndQuarantinesOnlyItsOwnFault() throws {
    let f = ObservationFixture.valid()
    let attempt = try CeremonyOutputRegistry.open(f.ceremonyAuthority, reviewedHead: f.reviewedHead)
    XCTAssertThrowsError(try CeremonyOutputRegistry.open(f.ceremonyAuthority, reviewedHead: f.reviewedHead))
    XCTAssertNoThrow(try CeremonyOutputRegistry.quarantine(attempt, ceremonyRoot: f.ceremonyAuthority, reviewedHead: f.reviewedHead))
    XCTAssertEqual(f.fakeFS.children(at: f.quarantine), [f.reviewedHead])
}
```

- [ ] **Step 2: Run RED (2 minutes).**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'ObservationCeremonyTests|CeremonyOutputRegistryTests|CLITests|ArchiveTests|MachOTests'`

Expected: FAIL because current HEAD has no `ObservationCeremony`, `CandidateCLI`, held ceremony input type, or candidate-only entrypoint.

- [ ] **Step 3: Implement the candidate-only closed grammar and held-input verification (5 minutes).** `CandidateCLI.parseObservation` accepts exactly the 13 tokens shown in Step 1, in that order; each option appears once, `--ceremony-root` names the absolute fixed ceremony-authority directory, `--input-bundle` names an absolute directory, and the other four option values are absolute regular paths. After deriving `reviewed-head` from the review package, reject unless those four paths are byte-for-byte the four fixed children of `ceremony-root/inputs/<reviewed-head>`; no arbitrary absolute input is accepted. Require EUID 0 before reading any input. `CeremonyInputHandoff.open` descriptor-walks only `ceremony-root/inputs/<reviewed-head>`, opens exactly `review-package-v2.json`, `observation-authorization-v2.json`, `observation-authorization-v2.ed25519`, and `key-a-public`, rebinds every child, checks root:wheel ownership/mode/ACL/immutable state, checks the 64-byte authorization signature, and requires Key-A byte hash to equal the compile-time constant, review package, and authorization before signature verification. `KeyAAuthority.openPrivateForObservation` opens only the separate root-owned private key descriptor and never returns its bytes to a caller.

- [ ] **Step 4: Implement no-child observation and registry output sealing (5 minutes).** `CeremonyOutputRegistry.open(_ ceremonyRoot: HeldDescriptor, reviewedHead: String) throws -> HeldDescriptor` and `CeremonyOutputRegistry.quarantine(_ attempt: HeldDescriptor, ceremonyRoot: HeldDescriptor, reviewedHead: String) throws` descriptor-walk the fixed `/private/var/db/mlb-walkforward-lab/task14-ceremony-authority/` root and its exact `outputs/` and `quarantine/` children. Every ancestor/parent must be root:wheel 0700, ACL-free, no-follow, local/internal/writable APFS, stable Volume UUID, and free of FileProvider/iCloud xattrs; a user-writable ancestor, contradictory resource value, or unavailable detector is fatal. `open` creates `outputs/<reviewed-head>` only with descriptor-relative `mkdirat(outputsDescriptor.fd, reviewedHead, 0o700)` exclusive (`EEXIST` is fatal, the directory form of an `O_EXCL` attempt) semantics, fsyncs/rebinds it, and never uses the Task12 `/private/tmp` evidence tree as an ancestor. Under that exclusive attempt, create only one cryptographic 32-lowercase-hex `work/` child. Descriptor-copy the approved archive from the untrusted bundle, invoke the shared `ArchiveInstaller.extractAndPlace` and `MachOValidator` in process, and make the exact public-Security observations of final interpreter and `/usr/bin/sandbox-exec`. Recompute profile-template and child-command-template hashes, construct the closed final approval, and sign it in process with the held Key-A private descriptor. Write exclusively `task14-approval-v2.json` and `task14-approval-v2.ed25519`, fsync file/parent, close/reopen/rebind/hash, chmod root:wheel 0444, apply/read back `SF_IMMUTABLE`, and fsync the attempt then `outputs/` parent. No actual profile substitution, actual command digest, child launch, wheelhouse, admission, runtime/authority manifest, receipt, Key-B record, bootstrap object, production root, or custody object is permitted. On failure before both output rebinding checks, publish neither output and `quarantine` uses `renameat` only on its descriptor-proven root-owned attempt into fixed `quarantine/<reviewed-head>`; if the rename cannot be proven, retain that attempt read-only in `outputs/` and return failure. No attempt is auto-cleaned, overwritten, or retried for the same reviewed head.

- [ ] **Step 5: Run GREEN and commit (2 minutes).**

```bash
swift test --package-path tools/r4_protected_execution_authority --filter 'ObservationCeremonyTests|CeremonyOutputRegistryTests|CLITests|ArchiveTests|MachOTests'
PYTHONDONTWRITEBYTECODE=1 /opt/homebrew/bin/python3.11 -m unittest tests.test_protocols -v
git diff --check
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{ObservationCeremony,CeremonyOutputRegistry,CLI,main,Adapters,Archive,MachO,Models}.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{ObservationCeremonyTests,CeremonyOutputRegistryTests,Fakes}.swift tools/r4_protected_execution_authority/Package.swift tests/test_protocols.py
git commit -m "feat: observe task14 approval preimages"
```

Expected: PASS; the candidate-only executable observes only static preimages and the installed helper cannot parse its command.

## Task 14: Separately authorized root-only no-child approval-preimage ceremony

**Terminal authorization boundary:** This is not an implementation task and must not run merely because Tasks 1–13 pass. It requires a separate written administrator authorization naming the reviewed commit, candidate helper digest, review-package digest, observation-authorization digest, Key-A public-key digest, immutable held-input directory, the fixed ceremony-authority registry, its fresh exclusive `outputs/<reviewed-head>` attempt, and this exact command. It authorizes neither bootstrap installation, Key-B generation, materialization, production `/Library` access, `verify`, nor custody.

**Files/evidence:** no repository file is written. The Key-A private-key authority directory is the pre-existing root-owned `/private/var/db/mlb-walkforward-lab/task14-key-a/`, mode 0700 root:wheel, with its only child `approval-signing-key`, mode 0600 root:wheel; it is never inside an output tree. The separate root-owned ceremony authority root is `/private/var/db/mlb-walkforward-lab/task14-ceremony-authority/`, mode 0700 root:wheel, with exactly `inputs/`, `outputs/`, and `quarantine/`, each mode 0700 root:wheel. `inputs/${reviewed_head}/` contains exactly `review-package-v2.json`, `observation-authorization-v2.json`, `observation-authorization-v2.ed25519`, and `key-a-public`, each mode 0444 root:wheel immutable. `outputs/${reviewed_head}/` is created only by the Task14 root invocation through descriptor-relative exclusive `mkdirat` (`EEXIST` fatal: directory-form `O_EXCL`); no Task12 evidence directory is an ancestor. Task12 records the expected absolute registry path under its user evidence root but does not create, open, or traverse the registry.

- [ ] **Step 1: Root-provision the ceremony authority registry and hand off Task12 evidence (5 minutes).** Before copying any input, descriptor-walk `/private/var/db/mlb-walkforward-lab/task14-ceremony-authority/`; reject an unexpected child, symlink, non-root:wheel ownership, mode/ACL drift, non-APFS/nonlocal/noninternal/removable/read-only volume, unstable UUID, unavailable/contradictory resource detector, or FileProvider/iCloud xattr. Create absent fixed `inputs`, `outputs`, and `quarantine` only beneath a retained root descriptor using `mkdirat` mode 0700, then fsync/rebind/verify every parent. The administrator descriptor-opens the user-owned Task12 review package and separately authorized observation authorization/signature/Key-A public files read-only/no-follow; canonical-validates/rebinds/hashes each; verifies the authorization signature under the held Key-A public bytes; then creates `inputs/${reviewed_head}` with `mkdirat` mode 0700 and `openat(O_CREAT|O_EXCL|O_NOFOLLOW)` copies each exact byte sequence into its fixed child name. Fsync every file and parent, close/reopen/rebind/hash, chmod root:wheel 0444, apply/read back `SF_IMMUTABLE`, and fsync the input attempt then `inputs/`. The Key-A private key remains only in `/private/var/db/mlb-walkforward-lab/task14-key-a/approval-signing-key`; it is not copied, linked, renamed, opened through `outputs/`, or included in a handoff record. Reject an extra/missing child, alias, non-root owner, changed descriptor, wrong Key-A hash, non-64-byte authorization signature, existing input/output/quarantine attempt, or any Task12 evidence-tree ancestor. On handoff failure, delete only a just-created descriptor-proven unsealed input child; never touch the user source, Key-A authority directory, production root, or custody.

- [ ] **Step 2: Run the exact no-child command once (5 minutes).**

```bash
reviewed_head="$(git rev-parse HEAD)"
evidence_root="/private/tmp/r4-v2-materialization-${reviewed_head}"
ceremony_authority="/private/var/db/mlb-walkforward-lab/task14-ceremony-authority"
held_inputs="$ceremony_authority/inputs/${reviewed_head}"
output_root="$ceremony_authority/outputs/${reviewed_head}"
key_a_authority="/private/var/db/mlb-walkforward-lab/task14-key-a"
candidate="$evidence_root/provisioner"
test -x "$candidate"
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$key_a_authority")" = 'root:wheel:700'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$key_a_authority/approval-signing-key")" = 'root:wheel:600'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$ceremony_authority")" = 'root:wheel:700'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$ceremony_authority/inputs")" = 'root:wheel:700'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$ceremony_authority/outputs")" = 'root:wheel:700'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$ceremony_authority/quarantine")" = 'root:wheel:700'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$held_inputs")" = 'root:wheel:700'
test ! -e "$output_root"
sudo "$candidate" task14-observe-approval-preimages \
  --ceremony-root "$ceremony_authority" \
  --review-package "$held_inputs/review-package-v2.json" \
  --observation-authorization "$held_inputs/observation-authorization-v2.json" \
  --observation-authorization-signature "$held_inputs/observation-authorization-v2.ed25519" \
  --task14-approval-verification-public-key "$held_inputs/key-a-public" \
  --input-bundle "$evidence_root/input-bundle"
```

Expected: the candidate validates its loaded binary and held Key-A hash before authorization-signature verification; it opens its Key-A private descriptor only from the separate authority directory; it creates `output_root` as one fresh descriptor-relative exclusive `mkdirat` (directory-form `O_EXCL`) registry attempt, extracts only the approved archive in-process beneath `output_root/work/`, observes the final staged interpreter and `/usr/bin/sandbox-exec`, and launches no child. It creates no wheelhouse, materialization admission, runtime manifest, authority manifest, receipt, Key-B material, actual profile, or actual child-command digest. Any failure publishes no approval; another attempt needs a new administrator authorization, new handoff directory named for a new reviewed head, and fresh unused registry output child.

- [ ] **Step 3: Verify and preserve final approval evidence (5 minutes).** Require exactly the two output children, descriptor-rebound after fsync, root:wheel 0444, immutable, and 64-byte signature. Hash without altering them.

```bash
test -f "$output_root/task14-approval-v2.json"
test -f "$output_root/task14-approval-v2.ed25519"
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$output_root/task14-approval-v2.json")" = 'root:wheel:444'
test "$(/usr/bin/stat -f '%Su:%Sg:%Lp' "$output_root/task14-approval-v2.ed25519")" = 'root:wheel:444'
test "$(/usr/bin/stat -f '%z' "$output_root/task14-approval-v2.ed25519")" = 64
/usr/bin/shasum -a 256 "$output_root/task14-approval-v2.json" >"$evidence_root/task14-approval-v2.sha256"
/usr/bin/shasum -a 256 "$output_root/task14-approval-v2.ed25519" >"$evidence_root/task14-approval-v2.ed25519.sha256"
```

Expected: final approval binds review-package/authorization, launcher descriptor/byte/code-sign identity, profile-template/command-template hashes, and base-interpreter byte/Mach-O/code-sign identity. Its Key-A signature is not bootstrap authorization; `bootstrap-install` remains separately approved.

## Spec coverage matrix and self-review

| Requirement | Tasks |
| --- | --- |
| Pure V2 closed schemas, Key-A review/authorization/approval, launcher/interpreter identities, serializations/cross-hashes, no V1 parser | 1, 1A |
| Compile-time Key-A pin, held Key-A signature verification, sealed bootstrap objects, then distinct Key-B generation-1 keyring | 2A |
| Fixture migration for Key-A/observation/handoff plus fake/real adapter separation | 3A, 10 |
| EUID/installed identity/no IPC; APFS/FileProvider preflight | 4A, 10 |
| Task2a no execution; Task2b fixed recorded Git; Task2c review-package only and no final approval | 5, 5A |
| O_EXCL accepts only `ValidatedBundle`; pre-admission proof | 4A, 6, 7A |
| Exact state table, durable transition records, signals/fault terminal rules | 6, 7A |
| Descriptor copy, archive denylist, final-layout rename/fsync/rebind | 7, 7A |
| Pinned `/usr/bin/sandbox-exec`, exact profile/argv/environment/absolute wheels, bounded pip/probe, Mach-O/signature gates | 8 |
| Observed launcher/interpreter/profile/commands/closure, receipt signature, Key-B keyring/revocation, residue/publish | 9 |
| Read-only V2 capture and legacy Task3 preservation | 11 |
| Exact suite parameters/log/exit/hash and reproducible review-package evidence | 12 |
| Candidate-only no-child preimage observer and installed-command rejection | 13 |
| Separately approved root handoff, no-child preimage observation, and final Key-A approval signature | 14 |
| No production `/Library`/custody before separate approval | boundary, 10–12, 14 |

Before handoff, inspect this plan for undefined types, ordering errors, and placeholders:

```bash
review_pattern='T'"ODO|T"'BD|\.'"\.\.|<'"policy|similar '"to"
rg -n "$review_pattern" docs/superpowers/plans/2026-08-09-r4-privileged-runtime-materialization.md
rg -n 'Materialization'"Evidence" docs/superpowers/plans/2026-08-09-r4-privileged-runtime-materialization.md
git diff --check
```

Expected: no red-flag result; fixture adapter references begin only after Task 3; no aggregate V2 evidence wrapper; every task has separate RED, run-RED, implementation, run-GREEN, and commit actions, and all production authority actions remain outside the plan.
