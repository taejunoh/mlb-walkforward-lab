# R4 V2 Keyring and Publication Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved V2 Key-B verification, sealed authority publication, and fail-closed lifecycle protocol without adding a production administrative mutation interface.

**Architecture:** Preserve bootstrap generation-1 bytes as the trust anchor, then verify only contiguous V2 successor records descriptor-relatively. Materialization writes one complete eight-child authority tree, journals every durable lifecycle boundary outside that tree, and accepts publication only after final rebind and durable published journal evidence. Python changes only closed schemas, policy bytes, vectors, and source-list review inputs; it does not obtain runtime, child-process, network, or authority-mutation capability.

**Tech Stack:** Swift 5.9, Foundation, Darwin, CryptoKit, XCTest, Python 3.11 standard library, JSON Schema Draft 2020-12, Ruff, macOS APFS immutable flags and descriptor-relative syscalls.

---

## File map and fixed interfaces

| Path | Responsibility |
| --- | --- |
| `schemas/r4_protected_execution_keyring.schema.json` | Frozen V1 genesis plus V2 rotation and revocation record union. |
| `schemas/r4_privileged_materialization_admission_locator_v2.schema.json` | Closed admission copy locator. |
| `schemas/r4_privileged_materialization_journal_v2.schema.json` | Closed append-only journal records. |
| `schemas/r4_protected_execution_receipt.schema.json` | Receipt signer and complete publication bindings. |
| `schemas/r4_privileged_materialization_policy_v2.schema.json` | Exact expanded schema-version list. |
| `config/r4_privileged_materialization_policy.json` | Canonical policy bytes and cascaded digest. |
| `scripts/build_r4_task14_approval.py` | Closed Swift source-list update for new Swift files. |
| `tests/test_protocols.py` | Python schema and policy-vector validity cases. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Models.swift` | Record, receipt, locator, journal models and canonical validation. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Keyring.swift` | Descriptor-held Key-B chain, revocation, and signing capability. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift` | Exact authority graph, prospective closure, seal, and publication transaction. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/StateMachine.swift` | Append-only journal state machine only. |
| `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Bootstrap,Adapters,Storage,Admission,Materialize}.swift` | Registry bootstrap, descriptor seams, UUID admission, storage preflight, and orchestration. |
| `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/{ModelsV2,Keyring,StateMachine,Storage,Materialize,Verify,Bootstrap,PythonTransportIntegration,DomainVectorFixture}Tests.swift` | Fixture-root regressions, vectors, and no-production-path evidence. |

Task 1 preserves the reviewed Swift source tuple as-is. Task 8 must add the
real `Publication.swift` implementation and append its filename in sorted order
after `ProcessRunner.swift` and before `SignalMonitor.swift` in
`CANONICAL_PROVISIONER_SWIFT_SOURCE_PATHS`, together with the Python transport
fixture update, as one atomic change. The existing six
`CANONICAL_REQUIRED_BLOB_PATHS` are unchanged; neither this plan nor its design
document is added to them. The prior Task 9 commits `ca5bda5` and `727e198` are
superseded by refactoring in place; do not reset, checkout, or destructively
revert them.

Before Task 1, record one execution-session baseline and retain it unchanged
through Task 13:

```bash
export PLAN_BASE="$(git rev-parse HEAD)"
printf '%s\n' "$PLAN_BASE"
```

### Task 1: Close schemas, policy bytes, Python vectors, and reviewed source list

**Files:**
- Create: `schemas/r4_privileged_materialization_admission_locator_v2.schema.json`, `schemas/r4_privileged_materialization_journal_v2.schema.json`
- Modify: `schemas/r4_protected_execution_keyring.schema.json`, `schemas/r4_protected_execution_receipt.schema.json`, `schemas/r4_privileged_materialization_policy_v2.schema.json`, `config/r4_privileged_materialization_policy.json`, `scripts/build_r4_task14_approval.py`, `tests/test_protocols.py`, `tests/test_build_r4_protected_candidate.py`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/PythonTransportIntegrationTests.swift`

- [ ] **Step 1: Write Python RED cases for the closed schema identifiers and unchanged required blobs.**

```python
def test_policy_requires_locator_and_journal_schema_versions(self) -> None:
    policy = self.valid_documents()["r4_privileged_materialization_policy_v2.schema.json"]
    policy["schema_versions"] += [
        "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2",
        "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2",
    ]
    self.assert_valid("r4_privileged_materialization_policy_v2.schema.json", policy)

def test_required_blob_paths_remain_the_fixed_six(self) -> None:
    self.assertEqual(len(candidate.CANONICAL_REQUIRED_BLOB_PATHS), 6)
    self.assertNotIn("docs/superpowers/specs/2026-08-12-r4-v2-keyring-publication-protocol-design.md", candidate.CANONICAL_REQUIRED_BLOB_PATHS)
```

- [ ] **Step 2: Run RED.**

Run: `python3 -m unittest tests.test_protocols tests.test_build_r4_protected_candidate -v`

Expected: FAIL because the policy schema rejects the two V2 identifiers and the new schemas do not exist.

- [ ] **Step 3: Add exact schema branches and policy cascade.**

```json
"schema_versions": [
  "R4_RUNTIME_INPUT_BUNDLE_V2",
  "R4_PRIVILEGED_MATERIALIZATION_POLICY_V2",
  "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2",
  "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2",
  "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2",
  "R4_HERMETIC_PYTHON311_RUNTIME_V2",
  "R4_PROTECTED_EXECUTION_MANIFEST_V2",
  "R4_PROTECTED_EXECUTION_RECEIPT_V2"
]
```

Define schema `kind` constants `R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2` and `R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2`; require every field named in the approved specification, reject additional properties, require integer `published_device` and `published_inode` in `[0, 9223372036854775807]`, and constrain journal filename/phase semantics in model tests where JSON Schema cannot express cross-file state. Update policy digest and all dependent fixture approval/bundle hashes using the repository canonical digest builder. Leave the reviewed Swift source tuple and Python transport fixture unchanged until Task 8 adds the real `Publication.swift` atomically; leave `CANONICAL_REQUIRED_BLOB_PATHS` untouched.

- [ ] **Step 4: Run GREEN and canonical-format checks.**

Run: `python3 -m unittest tests.test_protocols tests.test_build_r4_protected_candidate -v && ruff check scripts tests`

Expected: PASS; policy fixture uses eight exact schema identifiers and required blob count remains six.

- [ ] **Step 5: Commit schema and vector foundation.**

```bash
git add schemas config/r4_privileged_materialization_policy.json scripts/build_r4_task14_approval.py tests/test_protocols.py tests/test_build_r4_protected_candidate.py tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/PythonTransportIntegrationTests.swift
git commit -m "feat: define v2 publication protocol schemas"
```

### Task 2: Model frozen genesis, V2 records, receipt signer bindings, locator, and journal

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Models.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ModelsV2Tests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/DomainVectorFixture.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures/generate_r4_v2_domain_vectors.py`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures/r4_v2_domain_vectors.json`

- [ ] **Step 1: Write RED model vectors.**

```swift
func testRotationPreimageUsesV2DomainAndExactPredecessorFields() throws {
    let record = try V2Fixtures.rotationRecord(generation: 2)
    XCTAssertEqual(try record.rotationSignaturePreimage().prefix(10), Array("R4KEYROT2\0".utf8))
    XCTAssertEqual(record.previousGeneration, 1)
}

func testReceiptRetainsSignerGenerationAndPublicDigest() throws {
    let receipt = try ReceiptV2.decode(V2Fixtures.receiptJSON(generation: 2, publicKeySHA256: V2Fixtures.sha("b")))
    XCTAssertEqual(receipt.keyGeneration, 2)
    XCTAssertEqual(receipt.publicKeySHA256, try SHA256Digest(V2Fixtures.sha("b")))
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests`

Expected: FAIL because `ReceiptV2` does not retain signer properties and V2 rotation records have no decoder or domain preimage.

- [ ] **Step 3: Implement closed model APIs.**

```swift
struct AdmissionLocatorV2: Equatable {
    let attemptUUID: String
    let admissionRecordSHA256: SHA256Digest
    let registryRelativePath: String
    let locatorSHA256: SHA256Digest
    let sha256: SHA256Digest
    static func decode(_ bytes: [UInt8]) throws -> AdmissionLocatorV2
}

struct JournalRecordV2: Equatable {
    let sequence: Int64
    let phase: MaterializationState
    let admissionRecordSHA256: SHA256Digest
    let receiptFileSHA256: SHA256Digest?
    let failureClass: JournalFailureClass?
    static func decode(_ bytes: [UInt8]) throws -> JournalRecordV2
}
```

Make `KeyringRecord` a discriminated union for frozen genesis, `R4_PROTECTED_EXECUTION_KEY_ROTATION_V2`, and `R4_PROTECTED_EXECUTION_REVOCATION_V2`. Add `rotationSignaturePreimage()` and `revocationSignaturePreimage()` returning the exact domain-prefixed bytes. Require timestamp syntax `^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$` only; do not compare timestamps. Make `ReceiptV2` store `keyGeneration` and `publicKeySHA256`; add `receiptFileSHA256(_ bytes: [UInt8]) -> SHA256Digest` using complete bytes.

- [ ] **Step 4: Run GREEN and regenerate vectors.**

Run: `python3 tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures/generate_r4_v2_domain_vectors.py && swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests`

Expected: PASS; frozen generation-1 vector bytes are unchanged, `Bootstrap.generation1Bytes` still equals that vector, and V2 vectors contain domain-prefixed rotation, revocation, locator, and journal digests.

- [ ] **Step 5: Commit models and vectors.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Models.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/ModelsV2Tests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/DomainVectorFixture.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fixtures
git commit -m "feat: model v2 authority records"
```

### Task 3: Add descriptor metadata and immutable-operation seams

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/POSIX.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/FakesTests.swift`

- [ ] **Step 1: Write RED descriptor tests.**

```swift
func testSealedMetadataRejectsWrongNlinkACLAndImmutableReadback() throws {
    for defect in ["nlink", "acl", "immutable"] {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-publication-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try fixture.fakeMetadata(defect).requireSealedRegular(mode: 0o444))
    }
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter FakesTests`

Expected: FAIL because sealed metadata has no combined verifier.

- [ ] **Step 3: Implement the narrow descriptor primitives.**

```swift
extension HeldDescriptor {
    func requireSealedRegular(mode: mode_t) throws
    func requireSealedDirectory(mode: mode_t) throws
    func rebindSameIdentity(in parent: HeldDescriptor, name: String, directory: Bool) throws -> HeldDescriptor
}

enum PublicationPOSIX {
    static func createImmutableExclusive(_ parent: HeldDescriptor, _ name: String, _ bytes: [UInt8], mode: mode_t) throws -> HeldDescriptor
    static func fsyncParents(_ first: HeldDescriptor, _ second: HeldDescriptor, root: HeldDescriptor) throws
}

struct FixtureOperationLog: Equatable {
    var launchPrograms: [String] = []
    var networkRequests: [String] = []
    var accessedPaths: [String] = []
}
```

`FixtureOperationLog` belongs only in test-target `Fakes.swift`; it is populated
by the existing fake filesystem/process adapters and is never added to a
production source file. Use `O_NOFOLLOW`, `fstat`, `fchmod`, `fchown`, ACL
inspection, `fchflags`, rebind, and directory fsync. Do not add raw path
overloads.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'FakesTests|StorageTests'`

Expected: PASS; all fake defects reject without mutating a foreign sentinel.

- [ ] **Step 5: Commit descriptor seams.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/POSIX.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/FakesTests.swift
git commit -m "feat: verify sealed descriptor metadata"
```

### Task 4: Bootstrap journals registry and split layout verification

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Bootstrap.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/BootstrapTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`

- [ ] **Step 1: Write RED bootstrap layout tests.**

```swift
func testBootstrapCreatesEmptyJournalsRegistryAndPreservesGenerationOneBytes() throws {
    let fixture = try BootstrapFixture.make()
    defer { try? fixture.removeOnlySelfOwnedFixture() }
    _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture.adapters)
    XCTAssertEqual(try fixture.entries("control/journals"), [])
    XCTAssertEqual(try fixture.bytes("control/keyring/generation-1/record.json"), fixture.expectedGeneration1Record)
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter BootstrapTests`

Expected: FAIL because `control/journals` is absent.

- [ ] **Step 3: Implement explicit bootstrap and operational verifiers.**

```swift
enum BootstrapObject: String, CaseIterable {
    case journalsDirectory = "control/journals"
}

static func verifyBootstrapLayout(root: HeldDescriptor) throws
static func verifyOperationalRegistryLayout(root: HeldDescriptor) throws

static func installTrusted(request: TrustedBootstrapRequest) throws -> BootstrapResult
#if DEBUG
static func installForTest(request: TrustedBootstrapRequest, root: String, adapters: some TestBootstrapAdapters) throws -> BootstrapResult
#endif
```

`verifyBootstrapLayout` requires generation-1 and empty mutable registries at bootstrap publication. `verifyOperationalRegistryLayout` accepts only grammar-closed generation, admission UUID, journal UUID, work UUID, quarantine UUID, and fixed authority children; it rejects all extra names.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter BootstrapTests`

Expected: PASS; generation-1 bytes match the pre-amendment vector exactly.

- [ ] **Step 5: Commit bootstrap layout.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Bootstrap.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/BootstrapTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift
git commit -m "feat: add authority journals registry"
```

### Task 5: Verify Key-B chains, terminal revocation, and active signing capability

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Keyring.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/KeyringTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`

- [ ] **Step 1: Write RED chain and capability tests.**

```swift
func testActiveKeyIsHighestContiguousGenerationAndDerivesPublicBytes() throws {
    let fixture = try KeyringFixture.generationThree()
    let active = try KeyringVerifier.loadActiveForTest(fixture.root)
    XCTAssertEqual(active.generation, 3)
    XCTAssertEqual(active.publicKeySHA256, fixture.generation3PublicSHA256)
    XCTAssertNoThrow(try active.sign(V2Fixtures.receiptBytes))
}

func testValidTerminalRevocationRejectsActiveAuthority() throws {
    XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(try KeyringFixture.revoked().root))
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter KeyringTests`

Expected: FAIL because the implementation reads only generation-1.

- [ ] **Step 3: Implement descriptor-held verifier with no mutation API.**

```swift
struct ActiveReceiptSigner {
    let generation: Int64
    let publicKeySHA256: SHA256Digest
    fileprivate let privateKey: Curve25519.Signing.PrivateKey
    func sign(_ receiptBytes: [UInt8]) throws -> [UInt8]
}

enum KeyringVerifier {
    static func loadActive() throws -> ActiveReceiptSigner
    static func verifyReadOnly() throws -> VerifiedKeyring
    #if DEBUG
    static func loadActiveForTest(_ root: HeldDescriptor) throws -> ActiveReceiptSigner
    static func verifyReadOnlyForTest(_ root: HeldDescriptor) throws -> VerifiedKeyring
    #endif
}
```

Production functions construct only the fixed Darwin/root descriptor capability;
the `ForTest` functions are inside `#if DEBUG` and accept only disposable
fixture descriptors. Walk and validate every record/signature/public descriptor
before opening the selected private descriptor. Read its raw private key into
one owned mutable `[UInt8]` buffer, require exactly 32 bytes, derive the public
key and require exact equality with held verified public bytes, rebind both
private/public descriptors, and zeroize the raw mutable buffer in place on
every success and error exit. `ActiveReceiptSigner` retains only the CryptoKit
private key and returns no raw private bytes. Accept only empty revocation
registry or reject after validating its sole terminal record. Keep all
generation construction helpers inside `#if DEBUG`; expose no production
rotation/revocation function and no CLI branch.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter KeyringTests`

Expected: PASS; gaps, aliases, malformed timestamps, predecessor drift, Key-A role confusion, and all revocation variants fail.

- [ ] **Step 5: Commit keyring verifier.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Keyring.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/KeyringTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift
git commit -m "feat: verify contiguous v2 keyring"
```

### Task 6: Make admission UUID-first and reject all durable residue

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Admission.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Storage.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/AdmissionTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/StorageTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`

- [ ] **Step 1: Write RED UUID location and residue cases.**

```swift
func testAdmissionUsesUUIDFilenameAndStageLocation() throws {
    let fixture = try MaterializationFixture.valid()
    let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
    XCTAssertEqual(admission.record.name, admission.attempt + ".json")
    XCTAssertEqual(try fixture.store.stageName(admission), admission.attempt)
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'AdmissionTests|StorageTests'`

Expected: FAIL because the stage name currently prefixes the authority ID and journals are absent from preflight.

- [ ] **Step 3: Implement held journal storage and exact residue checks.**

```swift
final class StorageAttestation {
    let journals: HeldDescriptor
}

static func admissionName(_ attempt: String) throws -> String { return attempt + ".json" }
static func stageName(_ attempt: String) throws -> String { return attempt }
```

Attestation opens and validates `control/journals`. Preflight rejects any child in admissions, journals, work, quarantine, or authorities. Every name is parsed before open and rebind; no raw caller path enters this API.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'AdmissionTests|StorageTests'`

Expected: PASS; each foreign sentinel remains unchanged and every residue class prevents admission.

- [ ] **Step 5: Commit admission/storage changes.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Admission.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Storage.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/AdmissionTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/StorageTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift
git commit -m "feat: bind admission lifecycle to uuid"
```

### Task 7: Replace mutable state with append-only journal states

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/StateMachine.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/StateMachineTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`

- [ ] **Step 1: Write RED transition and failure-class tests.**

```swift
func testJournalRejectsWrongTerminalFailureClass() throws {
    let admission = try MaterializationFixture.valid().admission()
    XCTAssertThrowsError(try JournalRecordV2.make(sequence: 4, phase: .quarantined, admission: admission, failure: .postRenameRebind))
    XCTAssertThrowsError(try JournalRecordV2.make(sequence: 4, phase: .publishIndeterminate, admission: admission, failure: .preRenameSeal))
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter StateMachineTests`

Expected: FAIL because `state.json` replacement is the current persistence mechanism.

- [ ] **Step 3: Implement immutable journal API.**

```swift
enum MaterializationState: String, Codable { case absent, admitted, stageCreated, presealed, renameIntent, published, quarantined, publishIndeterminate }

protocol DurableStateStore: AnyObject {
    var currentState: MaterializationState { get }
    func append(_ record: JournalRecordV2, admission: Admission) throws
    func moveToQuarantine(admission: Admission, failure: JournalFailureClass) throws
    func markPublishIndeterminate(admission: Admission, failure: JournalFailureClass) throws
}
```

Create `control/journals/u` and each numbered file with exclusive create, fsync, immutable readback, parent fsync, and rebind. Delete `replaceAtomically`, random temporary state names, and all state-file adoption paths.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter StateMachineTests`

Expected: PASS; journal sequence is exact, timestamps are syntax-only, and no journal record is replaced.

- [ ] **Step 5: Commit append-only journal.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/StateMachine.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/StateMachineTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift
git commit -m "feat: journal materialization lifecycle"
```

### Task 8: Construct the exact authority graph and prospective closure

**Files:**
- Create: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift`
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Materialize.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Models.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/VerifyTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`

- [ ] **Step 1: Write RED graph/closure tests.**

```swift
func testProspectiveAuthorityTreeHasExactEightChildrenAndAcyclicClosure() throws {
    let fixture = try MaterializationFixture.valid()
    let graph = try PublishedAuthorityGraph.build(stage: fixture.stage, admission: fixture.admission)
    XCTAssertEqual(graph.childNames, ["admission-locator-v2.json", "admission.json", "checkout", "manifest-v2.json", "receipt.ed25519", "receipt.json", "runtime", "runtime-manifest-v2.json"])
    XCTAssertFalse(graph.closureRows.contains { $0.path == "receipt.json" || $0.path == "receipt.ed25519" })
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|VerifyTests'`

Expected: FAIL because the current closure contains checkout only.

- [ ] **Step 3: Implement graph builder and locator copy.**

```swift
struct PublishedAuthorityGraph {
    let childNames: [String]
    let closureRows: [ManifestEntry]
    let closureSHA256: SHA256Digest
    static func build(stage: HeldMaterializationStage, admission: Admission) throws -> PublishedAuthorityGraph
}

struct PreparedPublication {
    let stage: HeldDescriptor
    let receipt: ReceiptV2
    let receiptBytes: [UInt8]
    let receiptFileSHA256: SHA256Digest
    let graph: PublishedAuthorityGraph
    let journal: JournalRecordV2
}

struct PublicationFixture {
    static func presealed() throws -> PublicationFixture
    func failing(_ seam: PublicationSeam) -> PublicationFixture
    let prepared: PreparedPublication
    let attestation: StorageAttestation
    let store: DurableStateStore
    let operationLog: FixtureOperationLog
    let root: URL
}
```

Create only the eight listed children. Copy exact admission bytes from its held record to `admission.json`; create locator bytes from the canonical model. Recompute recursive rows for checkout, runtime, manifests, admission files, and directories; exclude exactly the receipt pair. Do not include the authority root row and do not accept caller-supplied rows.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|VerifyTests'`

Expected: PASS; each missing, extra, symlink, receipt-included, or receipt-omitted-nonreceipt row fails.

- [ ] **Step 5: Commit published graph.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Materialize.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Models.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/VerifyTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift
git commit -m "feat: bind complete published authority graph"
```

### Task 9: Sign receipt, validate prospective graph, and preseal descendants

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/{Materialize,Publication,Models}.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift`

- [ ] **Step 1: Write RED signing/preseal test.**

```swift
func testReceiptBindsActiveKeyAndPresealedClosureBeforeRename() throws {
    let fixture = try MaterializationFixture.valid()
    let result = try Materializer.prepareForPublicationForTest(fixture.request, adapters: fixture.adapters)
    XCTAssertEqual(result.receipt.keyGeneration, 1)
    XCTAssertEqual(result.receipt.publicKeySHA256, fixture.keyring.activePublicKeySHA256)
    XCTAssertEqual(result.journal.phase, .presealed)
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter MaterializeTests`

Expected: FAIL because receipt creation has no active-signing capability or full graph validation.

- [ ] **Step 3: Implement preparation contract.**

```swift
static func prepareForPublication(_ request: MaterializationRequest) throws -> PreparedPublication
#if DEBUG
static func prepareForPublicationForTest(_ request: MaterializationRequest, adapters: TestMaterializationAdapters) throws -> PreparedPublication
#endif
```

The production function constructs concrete Darwin adapters and fixed-root
attestation internally. The DEBUG-only function receives test adapters. Both
load `ActiveReceiptSigner`, create receipt with its generation/public digest,
require a 64-byte signature, write both receipt files exclusively, validate
every cross-document edge and prospective closure, seal all descendants, then
append `PRESEALED` with `receipt_file_sha256` computed from complete receipt
bytes. The stage root remains mutable.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter MaterializeTests`

Expected: PASS; wrong derived key, signature length, closure, receipt-file hash, or preseal metadata prevents rename intent.

- [ ] **Step 5: Commit receipt preparation.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Materialize.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Models.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift
git commit -m "feat: preseal signed authority receipt"
```

### Task 10: Execute no-replace publication and terminalization boundaries

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/StateMachine.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/StateMachineTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift`

- [ ] **Step 1: Write RED pre/post-rename fault table.**

```swift
func testPostRenameFaultIsIndeterminateAndNeverQuarantinedOrRetried() throws {
    let fixture = try PublicationFixture.presealed().failing(.finalRebind)
    XCTAssertThrowsError(try Publication.publishForTest(fixture.prepared, attestation: fixture.attestation, store: fixture.store))
    XCTAssertEqual(fixture.journal.last?.phase, .publishIndeterminate)
    XCTAssertEqual(fixture.renameCalls, 1)
    XCTAssertEqual(fixture.unlinkCalls, 0)
    XCTAssertEqual(fixture.quarantineMoves, 0)
    XCTAssertEqual(fixture.operationLog.launchPrograms, [])
    XCTAssertEqual(fixture.operationLog.networkRequests, [])
    XCTAssertTrue(fixture.operationLog.accessedPaths.allSatisfy { $0.hasPrefix(fixture.root.path + "/") })
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|StateMachineTests'`

Expected: FAIL because current catch logic attempts generic quarantine after sealing.

- [ ] **Step 3: Implement transaction boundary.**

```swift
enum PublicationOutcome { case published, quarantined, publishIndeterminate }

static func publish(_ prepared: PreparedPublication, attestation: StorageAttestation, store: DurableStateStore) throws -> PublicationOutcome
#if DEBUG
static func publishForTest(_ prepared: PreparedPublication, attestation: StorageAttestation, store: DurableStateStore) throws -> PublicationOutcome
#endif
```

`publish` receives only the internally minted production attestation and store;
`publishForTest` is `#if DEBUG` only. Append `RENAME_INTENT`, invoke one
descriptor-relative `renameExclusive`, immediately rebind same inode at the
fixed authority ID, seal root, fsync authority parent and volume, then append
`PUBLISHED`. Before rename, only retained-stage exclusive quarantine may append
`QUARANTINED` with `PRE_*`. After entering rename, only
`PUBLISH_INDETERMINATE` with `POST_*` is permitted. The `PUBLISHED` record
contains no failure class.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|StateMachineTests'`

Expected: PASS; every injected pre-rename seam quarantines retained identity, every post-rename seam is permanently indeterminate, and sentinel files are unchanged.

- [ ] **Step 5: Commit publication transaction.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/StateMachine.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Adapters.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/StateMachineTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/Fakes.swift
git commit -m "feat: publish sealed authority once"
```

### Task 11: Make `Verify.active` a complete descriptor-relative read-only verifier

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Keyring.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/VerifyTests.swift`

- [ ] **Step 1: Write RED complete-graph verification cases.**

```swift
func testActiveRejectsMissingPublishedJournalAndAdmissionRegistryDrift() throws {
    for defect in ["missing-published-journal", "locator-copy-mismatch", "registry-copy-mismatch", "extra-authority-child"] {
        let fixture = try VerificationFixture.sealed(defect: defect)
        XCTAssertThrowsError(try Verify.activeForTest(root: fixture.root))
        XCTAssertEqual(fixture.mutations, [])
    }
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter VerifyTests`

Expected: FAIL because `Verify.active` reads fixed files without full closure, journal, or admission verification.

- [ ] **Step 3: Implement verification entry point.**

```swift
enum Verify {
    static func active() throws
    #if DEBUG
    static func activeForTest(root: HeldDescriptor) throws
    #endif
}
```

`active()` constructs the fixed production root internally. The `ForTest` entry
point is compiled only under `#if DEBUG` and accepts only the disposable fixture
root descriptor. Both verify bootstrap, operational registry layout, active
Key-B chain, revocation terminal state, exact eight-child authority layout,
receipt signature and signer binding, closure, runtime and checkout rows,
admission copy/locator/external record, all V2 cross-hashes, and durable
`PUBLISHED` journal. Neither entry point creates a write capability or mutates
any registry.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter VerifyTests && swift build --package-path tools/r4_protected_execution_authority -c release && ! nm -gU tools/r4_protected_execution_authority/.build/release/R4ProtectedExecutionProvisioner | rg 'activeForTest|loadActiveForTest'`

Expected: PASS; every single-byte tamper, journal absence, locator path escape, key drift, revocation, and forbidden extra child rejects read-only, and the release executable exports neither DEBUG-only fixture verifier symbol.

- [ ] **Step 5: Commit full verifier.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Keyring.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Publication.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/VerifyTests.swift
git commit -m "feat: verify complete v2 authority graph"
```

### Task 12: Integrate fixture-root materialization without expanding authority surface

**Files:**
- Modify: `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Materialize.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/main.swift`, `tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/CLI.swift`
- Modify: `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CLITests.swift`, `tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/PythonTransportIntegrationTests.swift`

- [ ] **Step 1: Write RED integration and authority-surface tests.**

```swift
func testRunForTestPublishesThenReadOnlyVerifySucceeds() throws {
    let fixture = try MaterializationFixture.valid()
    _ = try Materializer.runForTest(fixture.request, adapters: fixture.adapters)
    XCTAssertNoThrow(try Verify.activeForTest(root: fixture.authorityRoot))
}
```

- [ ] **Step 2: Run RED.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|CLITests|PythonTransportIntegrationTests'`

Expected: FAIL because `Materializer.runForTest` does not exist and the current Task 9 orchestration cannot produce a valid published journal graph.

- [ ] **Step 3: Route materialize and verify only.**

```swift
static func run(_ request: MaterializationRequest) throws -> ReceiptV2 {
    return try runProduction(request)
}

#if DEBUG
static func runForTest(_ request: MaterializationRequest, adapters: TestMaterializationAdapters) throws -> ReceiptV2 {
    return try runFixture(request, adapters: adapters)
}
#endif
```

`runProduction` constructs concrete Darwin adapters and fixed-root attestation
inside the production module. `runFixture` is DEBUG-only and receives the
existing test-only adapter protocol. After the end-to-end GREEN assertion is
passing, add the regression assertions
`XCTAssertThrowsError(try CLI.parse(["rotate-key"]))` and
`XCTAssertThrowsError(try CLI.parse(["revoke-key"]))`; they are GREEN
regressions, not RED evidence. Keep `main` dispatch limited to existing
`materialize` and `verify` verbs. Do not add rotate/revoke flags, IPC, daemon,
socket, XPC, network client, child launch path beyond Task 8's fixed launch, or
protected-root access in ordinary tests.

- [ ] **Step 4: Run GREEN.**

Run: `swift test --package-path tools/r4_protected_execution_authority --filter 'MaterializeTests|CLITests|PythonTransportIntegrationTests'`

Expected: PASS; fixture-root integration publishes once, read-only verification succeeds, and no administrative command is parseable.

- [ ] **Step 5: Commit integration.**

```bash
git add tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/Materialize.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/main.swift tools/r4_protected_execution_authority/Sources/R4ProtectedExecutionProvisioner/CLI.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/MaterializeTests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/CLITests.swift tools/r4_protected_execution_authority/Tests/R4ProtectedExecutionProvisionerTests/PythonTransportIntegrationTests.swift
git commit -m "feat: integrate sealed authority materialization"
```

### Task 13: Run cross-language, release, boundary, and scratch verification

**Files:**
- No source changes. This task verifies the regression assertions already added in Tasks 3, 5, 6, 10, and 12.

- [ ] **Step 1: Create and record unique disposable scratch roots.**

```bash
export PLAN_SCRATCH_FOCUSED="/private/tmp/r4-v2-focused-$PLAN_BASE"
export PLAN_SCRATCH_FULL="/private/tmp/r4-v2-full-$PLAN_BASE"
export PLAN_SCRATCH_RELEASE="/private/tmp/r4-v2-release-$PLAN_BASE"
test ! -e "$PLAN_SCRATCH_FOCUSED"
test ! -e "$PLAN_SCRATCH_FULL"
test ! -e "$PLAN_SCRATCH_RELEASE"
mkdir "$PLAN_SCRATCH_FOCUSED" "$PLAN_SCRATCH_FULL" "$PLAN_SCRATCH_RELEASE"
```

- [ ] **Step 2: Run the exact validation suite.**

Run: `TMPDIR="$PLAN_SCRATCH_FOCUSED" swift test --package-path tools/r4_protected_execution_authority --filter 'KeyringTests|MaterializeTests|VerifyTests|StateMachineTests|StorageTests|ProcessRunnerTests' && TMPDIR="$PLAN_SCRATCH_FULL" swift test --package-path tools/r4_protected_execution_authority && TMPDIR="$PLAN_SCRATCH_RELEASE" swift build --package-path tools/r4_protected_execution_authority -c release && python3 -m unittest discover -s tests -p 'test_*.py' && ruff check scripts tests && git diff --check`

Expected: all Swift tests, release build, Python tests, Ruff, and diff check pass.

- [ ] **Step 3: Verify source closure and working tree.**

Run: `python3 -m unittest tests.test_r4_protected_fixed_git -v && git diff --name-only "$PLAN_BASE..HEAD" && find "$PLAN_SCRATCH_FOCUSED" -depth -delete && find "$PLAN_SCRATCH_FULL" -depth -delete && find "$PLAN_SCRATCH_RELEASE" -depth -delete && test ! -e "$PLAN_SCRATCH_FOCUSED" && test ! -e "$PLAN_SCRATCH_FULL" && test ! -e "$PLAN_SCRATCH_RELEASE" && git status --short`

Expected: required blobs remain six, reviewed Swift source list contains `Publication.swift`, all three exact scratch paths are absent, and the implementation range contains only intended protocol files.

Typed operation logs in existing `Fakes.swift`, `MaterializeTests.swift`, and
`ProcessRunnerTests.swift` are the boundary evidence: each pre-launch rejection
asserts zero launch and network events, and every path recorded by an accepted
fixture operation has the unique fixture root as its prefix. These are
consumer-visible adapter outcomes, not argument-inspection or source-grep
tests.

## Coverage self-review matrix

| Approved specification requirement | Plan task |
| --- | --- |
| Frozen generation-1 compatibility and successor preimages | 1, 2, 4, 5 |
| Rotation/revocation descriptor verification without production mutator | 2, 5, 12 |
| Exact modes, immutable metadata, and rebind checks | 3, 4, 8, 9, 10, 11 |
| Locator, admission copy, and full acyclic closure | 1, 2, 8, 11 |
| Receipt signer and complete-file digest binding | 2, 5, 9, 10, 11 |
| UUID stage/quarantine locations and residue preflight | 6, 7, 10 |
| Append-only journal state machine and failure-class split | 1, 2, 7, 10, 11 |
| One rename, final rebind, durable published journal, indeterminate rule | 9, 10, 11 |
| Policy schema cascade and source/required-blob controls | 1, 13 |
| Fixture-only behavior and no protected path/child/network/sentinel mutation | 3, 5, 6, 10, 12, 13 |
