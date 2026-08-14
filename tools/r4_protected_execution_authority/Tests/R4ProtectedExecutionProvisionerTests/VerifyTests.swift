import XCTest
@testable import R4ProtectedExecutionProvisioner

final class VerifyTests: XCTestCase {
    private struct DescriptorSnapshot: Equatable {
        let path: String; let inode: UInt64; let mtimeSeconds: Int64; let mtimeNanoseconds: Int64
    }
    // Uses the real Task 9 preparation plus Task 10 one-shot publication fixture rather than a
    // hand-built receipt tree.  VerificationFixture will assemble its bootstrap graph around
    // this retained published result in the next cases.
    func testSharedPublicationFixtureProducesDurablePublishedJournal() throws {
        let setup = try MaterializeTests.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        XCTAssertEqual(try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                       store: setup.fixture.durableStore, operations: setup.operations),
                       .published)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .published)
        XCTAssertEqual(try JournalRecordV2.decode(try XCTUnwrap(setup.fixture.durableStore.journalRecords.last)).phase,
                       .published)
    }

    func testActiveForTestAcceptsPublishedAuthorityAndRejectsMutationTable() throws {
        typealias Mutation = (KeyringFixture, HeldDescriptor, HeldDescriptor, HeldDescriptor, HeldDescriptor) throws -> Void
        func exercise(_ defect: String, hookMutation: Bool = false, _ mutate: Mutation) throws {
            defer { Verify.afterKeyringVerificationHook = nil; Verify.afterAuthorityVerificationHook = nil; Verify.beforeFinalRebindHook = nil }
            let keyring = try KeyringFixture.generationThree()
            let signer = try KeyringVerifier.loadActiveForTest(keyring.root, keyA: keyring.keyA)
            let published = try MaterializeTests.preparedPublicationFixture(signer: signer)
            defer { try? published.fixture.removeOnlySelfOwnedFixture() }
            let sentinelRoot = try DarwinDisposableRoot.create(prefix: "r4-verify-sentinel-")
            defer { try? sentinelRoot.removeOnlySelfOwnedFixture() }
            let authority = try keyring.installVerificationScaffold(approvalBytes: Array(try Data(contentsOf: published.fixture.approvalURL)),
                materializationPolicyBytes: Array(try Data(contentsOf: published.fixture.request.inputBundle.appendingPathComponent("materialization-policy.json"))))
            let control = try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY)
            let admissions = try POSIX.openAt(control, name: "admissions", flags: O_RDONLY | O_DIRECTORY)
            let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
            let authorities = try POSIX.openAt(keyring.root, name: "authorities", flags: O_RDONLY | O_DIRECTORY)
            let target = StorageAttestation(rootParent: keyring.root, root: keyring.root, control: control, admissions: admissions,
                                            journals: journals, work: published.attestation.work,
                                            quarantine: try POSIX.openAt(keyring.root, name: "quarantine", flags: O_RDONLY | O_DIRECTORY),
                                            authorities: authorities, volumeUUID: "fixture")
            XCTAssertEqual(try Publication.publishForTest(prepared: published.prepared, attestation: target,
                                                           store: published.fixture.durableStore, operations: published.operations), .published)
            let root = try POSIX.openAt(authorities, name: ProtectedExecutionConstants.authorityID, flags: O_RDONLY | O_DIRECTORY)
            let record = try POSIX.openAt(root, name: "admission.json", flags: O_RDONLY)
            let admissionBytes = try POSIX.readAll(record)
            let attempt = "0123456789abcdef0123456789abcdef"
            let admission = try POSIX.createExclusive(admissions, name: attempt + ".json", mode: 0o444)
            try POSIX.writeAll(admission, admissionBytes)
            let journal = try POSIX.mkdirAt(journals, name: attempt, mode: 0o700)
            for bytes in published.fixture.durableStore.journalRecords {
                let entry = try JournalRecordV2.decode(bytes)
                let file = try POSIX.createExclusive(journal, name: entry.filename, mode: 0o444)
                try POSIX.writeAll(file, bytes)
            }
            let sentinel = try POSIX.createExclusive(sentinelRoot.heldRoot, name: "fixture-sentinel", mode: 0o444)
            try POSIX.writeAll(sentinel, Array("never-written-by-verify".utf8))
            let sentinelReadOnly = try POSIX.openAt(sentinelRoot.heldRoot, name: "fixture-sentinel", flags: O_RDONLY)
            let sentinelBytes = try POSIX.readAll(sentinelReadOnly)
            XCTAssertNoThrow(try Verify.activeForTest(authority: authority), "baseline \(defect)")
            try mutate(keyring, root, admissions, journal, sentinelRoot.heldRoot)
            let before = try Self.descriptorSnapshot(keyring.root)
            XCTAssertThrowsError(try Verify.activeForTest(authority: authority), defect)
            if !hookMutation { XCTAssertEqual(try Self.descriptorSnapshot(keyring.root), before, "verifier mutated \(defect)") }
            XCTAssertEqual(try POSIX.readAll(sentinelReadOnly), sentinelBytes, "verifier wrote sentinel \(defect)")
        }
        try exercise("extra authority child") { _, root, _, _, _ in
            try Self.withWritableOwnedDirectory(root) { writable in
                let extra = try POSIX.createExclusive(writable, name: "extra", mode: 0o444); try POSIX.writeAll(extra, [0])
            }
        }
        try exercise("receipt byte") { _, root, _, _, _ in
            try Self.withWritableOwnedDirectory(root) { writable in
                let receipt = try POSIX.openAt(writable, name: "receipt.json", flags: O_RDONLY)
                var changed = try POSIX.readAll(receipt); changed[0] ^= 1
                try POSIX.unlinkAt(writable, name: "receipt.json")
                let replacement = try POSIX.createExclusive(writable, name: "receipt.json", mode: 0o444); try POSIX.writeAll(replacement, changed)
            }
        }
        try exercise("registry admission copy") { _, _, admissions, _, _ in
            let file = try POSIX.openAt(admissions, name: "0123456789abcdef0123456789abcdef.json", flags: O_RDONLY)
            var changed = try POSIX.readAll(file); changed[0] ^= 1
            try POSIX.unlinkAt(admissions, name: "0123456789abcdef0123456789abcdef.json")
            let replacement = try POSIX.createExclusive(admissions, name: "0123456789abcdef0123456789abcdef.json", mode: 0o444); try POSIX.writeAll(replacement, changed)
        }
        try exercise("missing published journal") { _, _, _, journal, _ in
            try POSIX.unlinkAt(journal, name: "0004-published.json")
        }
        // The journal attempt is mutable registry state, but it must remain an ordinary,
        // non-sealed 0700 directory.  This used to be skipped by the DEBUG verifier.
        try exercise("journal attempt mode") { _, _, _, journal, _ in
            guard fchmod(journal.fd, 0o755) == 0 else { throw POSIXFailure.system(errno) }
        }
        try exercise("terminal revocation") { keyring, _, _, _, _ in try keyring.addValidRevocation() }
        try exercise("locator path escape") { _, root, _, _, _ in
            try Self.withWritableOwnedDirectory(root) { writable in
                let locator = try POSIX.openAt(writable, name: "admission-locator-v2.json", flags: O_RDONLY)
                let decoded = try AdmissionLocatorV2.decode(try POSIX.readAll(locator))
                let path = "control/admissions/../" + decoded.attemptUUID + ".json"
                let core: [(String, JSONValue)] = [
                    ("admission_record_sha256", .string(decoded.admissionRecordSHA256.value)),
                    ("attempt_uuid", .string(decoded.attemptUUID)),
                    ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
                    ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2")),
                    ("namespace", .string(ProtectedExecutionConstants.namespace)),
                    ("registry_relative_path", .string(path))
                ]
                let locatorSHA = CanonicalJSON.sha256(Array("R4ADMISSIONLOCATOR2\0".utf8) + (try CanonicalJSON.encode(.object(core))))
                let withLocator = core + [("locator_sha256", .string(locatorSHA))]
                let changed = try CanonicalJSON.encode(.object(withLocator + [("sha256", .string(CanonicalJSON.sha256(try CanonicalJSON.encode(.object(withLocator)))))]))
                try POSIX.unlinkAt(writable, name: "admission-locator-v2.json")
                let replacement = try POSIX.createExclusive(writable, name: "admission-locator-v2.json", mode: 0o444); try POSIX.writeAll(replacement, changed)
            }
        }
        try exercise("late authority child", hookMutation: true) { _, root, _, _, _ in
            Verify.afterAuthorityVerificationHook = {
                try Self.withWritableOwnedDirectory(root) { writable in
                    let extra = try POSIX.createExclusive(writable, name: "late-extra", mode: 0o444)
                    try POSIX.writeAll(extra, [0])
                }
            }
        }
        // The verifier retained the original authorities descriptor, so only a rebind from
        // the root can detect this replacement.  The hook mutation itself is intentionally
        // excluded from the verifier's no-write snapshot; the sibling sentinel remains proof
        // that rejection performed no follow-up repair or cleanup.
        try exercise("late authorities registry swap", hookMutation: true) { keyring, _, _, _, _ in
            Verify.afterAuthorityVerificationHook = {
                let foreign = try POSIX.mkdirAt(keyring.root, name: "authorities-foreign", mode: 0o700)
                let marker = try POSIX.createExclusive(foreign, name: "foreign-sentinel", mode: 0o444)
                try POSIX.writeAll(marker, Array("foreign-authorities".utf8))
                try POSIX.replaceAtomically(in: keyring.root, source: "authorities", destination: "authorities-displaced")
                try POSIX.replaceAtomically(in: keyring.root, source: "authorities-foreign", destination: "authorities")
            }
        }
        try exercise("late journal attempt swap", hookMutation: true) { keyring, _, _, _, _ in
            Verify.afterAuthorityVerificationHook = {
                let control = try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY)
                let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
                let foreign = try POSIX.mkdirAt(journals, name: "attempt-foreign", mode: 0o700)
                let marker = try POSIX.createExclusive(foreign, name: "foreign-sentinel", mode: 0o444)
                try POSIX.writeAll(marker, Array("foreign-journal".utf8))
                try POSIX.replaceAtomically(in: journals, source: "0123456789abcdef0123456789abcdef", destination: "attempt-displaced")
                try POSIX.replaceAtomically(in: journals, source: "attempt-foreign", destination: "0123456789abcdef0123456789abcdef")
            }
        }
        try exercise("late keyring registry swap", hookMutation: true) { keyring, _, _, _, sentinelRoot in
            Verify.afterKeyringVerificationHook = {
                let control = try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY)
                let foreign = try POSIX.mkdirAt(control, name: "keyring-foreign", mode: 0o700)
                let marker = try POSIX.createExclusive(foreign, name: "foreign-sentinel", mode: 0o444)
                try POSIX.writeAll(marker, Array("foreign-keyring".utf8))
                try POSIX.renameExclusive(from: control, name: "keyring", to: sentinelRoot, destination: "keyring-displaced")
                try POSIX.replaceAtomically(in: control, source: "keyring-foreign", destination: "keyring")
            }
        }
        try exercise("late revocations registry swap", hookMutation: true) { keyring, _, _, _, sentinelRoot in
            Verify.afterKeyringVerificationHook = {
                let control = try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY)
                let foreign = try POSIX.mkdirAt(control, name: "revocations-foreign", mode: 0o700)
                let marker = try POSIX.createExclusive(foreign, name: "foreign-sentinel", mode: 0o444)
                try POSIX.writeAll(marker, Array("foreign-revocations".utf8))
                try POSIX.renameExclusive(from: control, name: "revocations", to: sentinelRoot, destination: "revocations-displaced")
                try POSIX.replaceAtomically(in: control, source: "revocations-foreign", destination: "revocations")
            }
        }
    }

    private static func withWritableOwnedDirectory(_ directory: HeldDescriptor, _ body: (HeldDescriptor) throws -> Void) throws {
        let original = try POSIX.metadata(directory)
        guard original.type == S_IFDIR, original.mode == 0o555 else { throw POSIXFailure.unsafeObject }
        guard fchmod(directory.fd, 0o700) == 0 else { throw POSIXFailure.system(errno) }
        defer { _ = fchmod(directory.fd, 0o555) }
        try body(directory)
    }

    private static func descriptorSnapshot(_ directory: HeldDescriptor, path: String = "") throws -> [DescriptorSnapshot] {
        var status = stat(); guard fstat(directory.fd, &status) == 0 else { throw POSIXFailure.system(errno) }
        var rows = [DescriptorSnapshot(path: path, inode: UInt64(status.st_ino), mtimeSeconds: Int64(status.st_mtimespec.tv_sec), mtimeNanoseconds: Int64(status.st_mtimespec.tv_nsec))]
        for name in try POSIX.directoryEntryNames(directory).sorted() {
            let child = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let childPath = path.isEmpty ? name : path + "/" + name
            let metadata = try POSIX.metadata(child)
            if metadata.type == S_IFDIR { rows += try descriptorSnapshot(child, path: childPath) }
            else { var childStatus = stat(); guard fstat(child.fd, &childStatus) == 0 else { throw POSIXFailure.system(errno) }; rows.append(DescriptorSnapshot(path: childPath, inode: UInt64(childStatus.st_ino), mtimeSeconds: Int64(childStatus.st_mtimespec.tv_sec), mtimeNanoseconds: Int64(childStatus.st_mtimespec.tv_nsec))) }
        }
        return rows
    }

    // This names the new production boundary: Verify must accept only a descriptor-held
    // root in DEBUG and must fail closed without creating or repairing sparse registry state.
    func testActiveForTestRejectsSparseDescriptorRootWithoutMutation() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-verify-fixture-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let before = try POSIX.directoryEntryNames(fixture.heldRoot)

        let authority = Verify.VerificationAuthority.makeForTest(root: fixture.heldRoot,
            expectedKeyAPublicSHA256: try SHA256Digest(String(repeating: "a", count: 64)))
        XCTAssertThrowsError(try Verify.activeForTest(authority: authority))
        XCTAssertEqual(try POSIX.directoryEntryNames(fixture.heldRoot), before)
    }

    func testVerifyActiveIsReadOnlyAndRejectsRevokedReceiptAuthority() throws {
        let fixture = try V2Fixture.complete()
        let receipt = try ReceiptV2.decode(fixture.receipt)
        let keyring = try KeyringRecord.decode(FixtureKeyring.genesis)
        let revoked = try KeyringRecord.decode(FixtureKeyring.revocation)
        XCTAssertThrowsError(try Verify.active(receipt: receipt, keyring: [keyring], revocations: [revoked],
                                                receiptPublicKeySHA256: keyring.publicKeySHA256,
                                                bootstrapKeyAPublicKeySHA256: try SHA256Digest(String(repeating: "e", count: 64))))
    }

    func testPublishedCrossValidatorRejectsReceiptRuntimeFieldDrift() throws {
        let fixture = try V2Fixture.complete()
        let approval = try Task14ApprovalV2.decode(fixture.approval)
        let admission = try AdmissionRecord.decode(fixture.admission)
        let runtime = try RuntimeManifestV2.decode(fixture.runtime)
        let manifest = try AuthorityManifestV2.decode(fixture.authority)
        let receipt = try ReceiptV2.decode(fixture.replacing(fixture.receipt, "environment_sha256", .string(String(repeating: "d", count: 64))))
        XCTAssertThrowsError(try V2Evidence.crossValidateAuthority(approval, admission,
            try SHA256Digest(CanonicalJSON.sha256(Array(fixture.admission.utf8))), runtime, manifest, receipt,
            publishedTree: try PublishedAuthorityTree.decode(fixture.publishedRows)))
    }
}
