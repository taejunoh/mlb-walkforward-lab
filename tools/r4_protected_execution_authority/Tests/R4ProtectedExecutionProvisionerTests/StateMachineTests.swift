import Darwin
import XCTest
@testable import R4ProtectedExecutionProvisioner

private final class JournalOperationLog {
    var events: [String] = []
}

final class StateMachineTests: XCTestCase {
    private func disposableAttestation(_ root: DarwinDisposableRoot) throws -> StorageAttestation {
        let control = try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY)
        let admissions = try POSIX.openAt(control, name: "admissions", flags: O_RDONLY | O_DIRECTORY)
        let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
        let work = try POSIX.openAt(root.heldRoot, name: "materialization-work", flags: O_RDONLY | O_DIRECTORY)
        let quarantine = try POSIX.openAt(root.heldRoot, name: "quarantine", flags: O_RDONLY | O_DIRECTORY)
        let authorities = try POSIX.openAt(root.heldRoot, name: "authorities", flags: O_RDONLY | O_DIRECTORY)
        return StorageAttestation(rootParent: root.heldRoot, root: root.heldRoot, control: control,
                                  admissions: admissions, journals: journals, work: work,
                                  quarantine: quarantine, authorities: authorities,
                                  volumeUUID: "123e4567-e89b-12d3-a456-426614174000")
    }

    private func prepareDisposableRoot() throws -> DarwinDisposableRoot {
        let root = try DarwinDisposableRoot.create(prefix: "r4-journal-")
        let control = try POSIX.mkdirAt(root.heldRoot, name: "control", mode: 0o700)
        _ = try POSIX.mkdirAt(control, name: "admissions", mode: 0o700)
        _ = try POSIX.mkdirAt(control, name: "journals", mode: 0o700)
        _ = try POSIX.mkdirAt(root.heldRoot, name: "materialization-work", mode: 0o700)
        _ = try POSIX.mkdirAt(root.heldRoot, name: "quarantine", mode: 0o700)
        _ = try POSIX.mkdirAt(root.heldRoot, name: "authorities", mode: 0o700)
        return root
    }

    private func disposablePrefix(_ journal: HeldDescriptor, _ names: Set<String>, _ admission: Admission) throws {
        guard try POSIX.directoryEntryNames(journal) == names else { throw StateFailure.illegalTransition }
        for name in names {
            let file = try POSIX.openAt(journal, name: name, flags: O_RDONLY)
            _ = try file.rebindSameIdentity(in: journal, name: name)
            try validateDisposableSealedRecord(file)
            let record = try JournalRecordV2.decode(POSIX.readAll(file))
            guard record.filename == name, record.attemptUUID == admission.attempt,
                  record.admissionRecordSHA256 == admission.recordSHA256,
                  try record.canonicalRecordBytes() == POSIX.readAll(file) else { throw StateFailure.illegalTransition }
        }
    }

    private func validateDisposableSealedRecord(_ file: HeldDescriptor) throws {
        let actual = try POSIX.metadata(file)
        guard actual.type == S_IFREG, actual.uid == getuid(), actual.gid == getgid(), actual.mode == 0o444 else { throw POSIXFailure.unsafeObject }
        try POSIX.requireNoExtendedACL(file)
        try POSIX.validateSealedMetadata(SealedMetadataSnapshot(type: actual.type, uid: 0, gid: 0,
                                                                 mode: actual.mode, nlink: actual.nlink,
                                                                 flags: UInt32(SF_IMMUTABLE), acl: .none,
                                                                 xattrs: try POSIX.extendedAttributeNames(file)),
                                        expectedType: S_IFREG, mode: 0o444, exactNlink: true)
    }

    private func normalizeDisposableDirectory(_ directory: HeldDescriptor, _ parent: HeldDescriptor,
                                              _ name: String) throws -> HeldDescriptor {
        guard Darwin.fchown(directory.fd, getuid(), getgid()) == 0,
              Darwin.fchmod(directory.fd, 0o700) == 0 else { throw POSIXFailure.system(errno) }
        let rebound = try directory.rebindSameIdentity(in: parent, name: name, directory: true)
        _ = try validateDisposableDirectory(rebound, parent, name)
        try POSIX.durableDirectory(rebound)
        return rebound
    }

    private func validateDisposableDirectory(_ directory: HeldDescriptor, _ parent: HeldDescriptor,
                                             _ name: String) throws -> HeldDescriptor {
        let rebound = try directory.rebindSameIdentity(in: parent, name: name, directory: true)
        try POSIX.requireDirectory(rebound, uid: getuid(), gid: getgid(), mode: 0o700)
        try POSIX.requireNoUnexpectedExtendedAttributes(rebound)
        var status = stat(); try POSIX.fstatRetry(rebound.fd, &status)
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw POSIXFailure.unsafeObject }
        return rebound
    }

    private func createDisposableImmutableRecord(_ parent: HeldDescriptor, _ name: String,
                                                 _ bytes: [UInt8], log: JournalOperationLog) throws -> HeldDescriptor {
        log.events += ["exclusive", "write"]
        let file = try POSIX.createExclusive(parent, name: name, mode: 0o600)
        guard Darwin.fchown(file.fd, getuid(), getgid()) == 0,
              Darwin.fchmod(file.fd, 0o444) == 0 else { throw POSIXFailure.system(errno) }
        try POSIX.writeAll(file, bytes)
        log.events.append("file-fsync")
        try POSIX.fsync(file)
        // `/private/tmp` cannot set SF_IMMUTABLE as an unprivileged test process. The typed
        // policy classifier receives the real type/owner/mode/xattrs plus the one privileged
        // immutable bit it cannot observe, and rejects every other real metadata drift.
        try validateDisposableSealedRecord(file)
        log.events += ["immutable-policy-readback", "rebind"]
        return try file.rebindSameIdentity(in: parent, name: name)
    }

    private func disposableStore(_ admission: Admission) throws -> (DarwinDisposableRoot, DarwinDurableStateStore, JournalOperationLog) {
        let root = try prepareDisposableRoot()
        let attestation = try disposableAttestation(root)
        let log = JournalOperationLog()
        let store = try DarwinDurableStateStore.makeDisposableForTest(attestation: attestation, admission: admission,
            createImmutableRecord: { try self.createDisposableImmutableRecord($0, $1, $2, log: log) }, validatePrefix: disposablePrefix,
               normalizeMutableDirectory: normalizeDisposableDirectory,
               validateMutableDirectory: validateDisposableDirectory)
        return (root, store, log)
    }

    func testDescriptorJournalUsesExactNamesAndExclusiveImmutableAppendOrder() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let (root, store, log) = try disposableStore(admission)
        defer { try? root.removeOnlySelfOwnedFixture() }
        let digest = try SHA256Digest(String(repeating: "a", count: 64))

        XCTAssertEqual(try POSIX.directoryEntryNames(try POSIX.openAt(try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY), name: "journals", flags: O_RDONLY | O_DIRECTORY)), [admission.attempt])
        try StateMachine.append(try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                                          publishedClosureSHA256: digest, receiptFileSHA256: digest), using: store, admission: admission)
        try StateMachine.append(try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                                          publishedClosureSHA256: digest, receiptFileSHA256: digest), using: store, admission: admission)
        let journal = try POSIX.openAt(try POSIX.openAt(try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY), name: "journals", flags: O_RDONLY | O_DIRECTORY), name: admission.attempt, flags: O_RDONLY | O_DIRECTORY)
        XCTAssertEqual(try POSIX.directoryEntryNames(journal), ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json"])
        XCTAssertEqual(log.events.filter { $0 == "exclusive" }.count, 3)
        XCTAssertEqual(log.events.filter { $0 == "immutable-policy-readback" }.count, 3)
        XCTAssertEqual(try POSIX.metadata(journal).mode, 0o700)
        XCTAssertEqual(try POSIX.metadata(try POSIX.openAt(journal, name: "0001-stage-created.json", flags: O_RDONLY)).mode, 0o444)
        XCTAssertThrowsError(try StateMachine.append(try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                                                               publishedClosureSHA256: digest, receiptFileSHA256: digest), using: store, admission: admission))
    }

    func testDescriptorPrefixRejectsExtraCaseAliasWrongTypeAndNoncanonicalRecord() throws {
        enum Mutation: CaseIterable { case extra, caseAlias, wrongType, noncanonical }
        for mutation in Mutation.allCases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
            let (root, store, _) = try disposableStore(admission)
            defer { try? root.removeOnlySelfOwnedFixture() }
            let control = try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY)
            let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
            let journal = try POSIX.openAt(journals, name: admission.attempt, flags: O_RDONLY | O_DIRECTORY)
            switch mutation {
            case .extra:
                _ = try POSIX.createExclusive(journal, name: "foreign.json", mode: 0o600)
            case .caseAlias:
                // The development volume is case-folding, so an alias cannot coexist with the
                // immutable literal name. EEXIST is the required no-adoption result.
                XCTAssertThrowsError(try POSIX.createExclusive(journal, name: "0001-Stage-created.json", mode: 0o600))
                continue
            case .wrongType:
                try POSIX.unlinkAt(journal, name: "0001-stage-created.json")
                _ = try POSIX.mkdirAt(journal, name: "0001-stage-created.json", mode: 0o700)
            case .noncanonical:
                let readonly = try POSIX.openAt(journal, name: "0001-stage-created.json", flags: O_RDONLY)
                guard Darwin.fchmod(readonly.fd, 0o600) == 0 else { throw POSIXFailure.system(errno) }
                let file = try POSIX.openAt(journal, name: "0001-stage-created.json", flags: O_WRONLY)
                guard Darwin.ftruncate(file.fd, 0) == 0 else { throw POSIXFailure.system(errno) }
            }
            let digest = try SHA256Digest(String(repeating: "a", count: 64))
            let next = try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                                 publishedClosureSHA256: digest, receiptFileSHA256: digest)
            XCTAssertThrowsError(try StateMachine.append(next, using: store, admission: admission), "\(mutation)")
        }
    }

    func testRestartClassifierAcceptsOnlyCompleteTerminalImmutablePrefix() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let (root, store, _) = try disposableStore(admission)
        defer { try? root.removeOnlySelfOwnedFixture() }
        let digest = try SHA256Digest(String(repeating: "a", count: 64))
        for record in [
            try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest),
            try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest),
            try JournalRecordV2.make(sequence: 4, phase: .published, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest,
                                     publishedDevice: 1, publishedInode: 2)
        ] { try StateMachine.append(record, using: store, admission: admission) }
        let control = try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY)
        let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
        XCTAssertEqual(try DarwinDurableStateStore.classifyDisposableForTest(attempt: admission.attempt,
                                                                              admission: admission, journals: journals,
                                                                              validatePrefix: disposablePrefix,
                                                                              validateDirectory: validateDisposableDirectory), .published)
    }

    func testRestartClassifierAcceptsEveryNormativeSparseQuarantinePrefix() throws {
        for state in [MaterializationState.stageCreated, .presealed, .renameIntent] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
            let (root, store, _) = try disposableStore(admission)
            defer { try? root.removeOnlySelfOwnedFixture() }
            let digest = try SHA256Digest(String(repeating: "a", count: 64))
            if state == .presealed || state == .renameIntent {
                try StateMachine.append(try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                                                 publishedClosureSHA256: digest, receiptFileSHA256: digest), using: store, admission: admission)
            }
            if state == .renameIntent {
                try StateMachine.append(try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                                                 publishedClosureSHA256: digest, receiptFileSHA256: digest), using: store, admission: admission)
            }
            try StateMachine.terminalizePreRename(using: store, admission: admission, failure: .preRenameSeal)
            let control = try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY)
            let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
            XCTAssertEqual(try DarwinDurableStateStore.classifyDisposableForTest(attempt: admission.attempt,
                                                                                  admission: admission, journals: journals,
                                                                                  validatePrefix: disposablePrefix,
                                                                                  validateDirectory: validateDisposableDirectory), .quarantined)
        }
    }

    func testRestartClassifierRejectsMutableAttemptDirectoryMetadataDrift() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let (root, store, _) = try disposableStore(admission)
        defer { try? root.removeOnlySelfOwnedFixture() }
        let digest = try SHA256Digest(String(repeating: "a", count: 64))
        for record in [
            try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest),
            try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest),
            try JournalRecordV2.make(sequence: 4, phase: .published, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest,
                                     publishedDevice: 1, publishedInode: 2)
        ] { try StateMachine.append(record, using: store, admission: admission) }
        let control = try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY)
        let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
        let attempt = try POSIX.openAt(journals, name: admission.attempt, flags: O_RDONLY | O_DIRECTORY)
        guard Darwin.fchmod(attempt.fd, 0o755) == 0 else { throw POSIXFailure.system(errno) }
        XCTAssertThrowsError(try DarwinDurableStateStore.classifyDisposableForTest(attempt: admission.attempt,
                                                                                    admission: admission, journals: journals,
                                                                                    validatePrefix: disposablePrefix,
                                                                                    validateDirectory: validateDisposableDirectory))
    }

    func testConcurrentIndependentStoresCannotAdoptOneAttempt() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let root = try prepareDisposableRoot()
        defer { try? root.removeOnlySelfOwnedFixture() }
        let create: (HeldDescriptor, String, [UInt8]) throws -> HeldDescriptor = { parent, name, bytes in
            let file = try POSIX.createExclusive(parent, name: name, mode: 0o600)
            guard Darwin.fchown(file.fd, getuid(), getgid()) == 0,
                  Darwin.fchmod(file.fd, 0o444) == 0 else { throw POSIXFailure.system(errno) }
            try POSIX.writeAll(file, bytes)
            _ = try file.rebindSameIdentity(in: parent, name: name)
            return file
        }
        let results = try raceTwo(startBarrier: f.admissionBarrier) { _ in
            try f.admissionBarrier.arriveAndWait()
            _ = try DarwinDurableStateStore.makeDisposableForTest(attestation: self.disposableAttestation(root),
                                                                    admission: admission, createImmutableRecord: create,
                                                                    validatePrefix: self.disposablePrefix,
                                                                    normalizeMutableDirectory: self.normalizeDisposableDirectory,
                                                                    validateMutableDirectory: self.validateDisposableDirectory)
        }
        XCTAssertEqual(results.filter { if case .success = $0 { return true }; return false }.count, 1)
        let journals = try POSIX.openAt(try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY), name: "journals", flags: O_RDONLY | O_DIRECTORY)
        XCTAssertEqual(try POSIX.directoryEntryNames(journals), [admission.attempt])
    }

    func testJournalRejectsWrongTerminalFailureClass() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)

        XCTAssertThrowsError(try JournalRecordV2.make(sequence: 4, phase: .quarantined,
                                                       admission: admission,
                                                       failure: .postRenameRebind))
        XCTAssertThrowsError(try JournalRecordV2.make(sequence: 4, phase: .publishIndeterminate,
                                                       admission: admission,
                                                       failure: .preRenameSeal))
    }

    func testJournalRequiresExactImmutablePrefixBeforeEachSuccessor() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let digest = try SHA256Digest(String(repeating: "a", count: 64))

        let stage = try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission)
        let presealed = try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                                  publishedClosureSHA256: digest, receiptFileSHA256: digest)
        let intent = try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                               publishedClosureSHA256: digest, receiptFileSHA256: digest)
        let published = try JournalRecordV2.make(sequence: 4, phase: .published, admission: admission,
                                                  publishedClosureSHA256: digest, receiptFileSHA256: digest,
                                                  publishedDevice: 1, publishedInode: 2)

        XCTAssertThrowsError(try StateMachine.append(presealed, using: f.durableStore, admission: admission))
        XCTAssertNoThrow(try StateMachine.append(stage, using: f.durableStore, admission: admission))
        XCTAssertNoThrow(try StateMachine.append(presealed, using: f.durableStore, admission: admission))
        XCTAssertNoThrow(try StateMachine.append(intent, using: f.durableStore, admission: admission))
        XCTAssertNoThrow(try StateMachine.append(published, using: f.durableStore, admission: admission))
        XCTAssertEqual(f.durableStore.currentState, .published)
        XCTAssertEqual(f.durableStore.journalRecords.count, 4)
        XCTAssertThrowsError(try StateMachine.append(published, using: f.durableStore, admission: admission))
    }

    func testPreRenameFailureQuarantinesAndPostRenameFailureIsIndeterminate() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let stage = try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission)
        try StateMachine.append(stage, using: f.durableStore, admission: admission)

        try StateMachine.terminalizePreRename(using: f.durableStore, admission: admission, failure: .preRenameSeal)
        XCTAssertEqual(f.durableStore.currentState, .quarantined)
        XCTAssertTrue(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertThrowsError(try StateMachine.terminalizePostRename(using: f.durableStore, admission: admission,
                                                                      failure: .postRenameRebind))
    }

    func testPreRenameQuarantineMovesOnlyHeldStageAndRetainsForeignWorkSentinel() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        let (root, store, _) = try disposableStore(admission)
        defer { try? root.removeOnlySelfOwnedFixture() }
        let work = try POSIX.openAt(root.heldRoot, name: "materialization-work", flags: O_RDONLY | O_DIRECTORY)
        let quarantine = try POSIX.openAt(root.heldRoot, name: "quarantine", flags: O_RDONLY | O_DIRECTORY)
        let foreign = try POSIX.createExclusive(work, name: "foreign-sentinel", mode: 0o600)
        try POSIX.writeAll(foreign, Array("foreign".utf8))

        try StateMachine.terminalizePreRename(using: store, admission: admission, failure: .preRenameSeal)

        XCTAssertEqual(store.currentState, .quarantined)
        XCTAssertEqual(try POSIX.directoryEntryNames(work), ["foreign-sentinel"])
        XCTAssertEqual(try POSIX.directoryEntryNames(quarantine), [admission.attempt])
        let relocated = try POSIX.openAt(quarantine, name: admission.attempt, flags: O_RDONLY | O_DIRECTORY)
        _ = try relocated.rebindSameIdentity(in: quarantine, name: admission.attempt, directory: true)
        let journals = try POSIX.openAt(try POSIX.openAt(root.heldRoot, name: "control", flags: O_RDONLY | O_DIRECTORY),
                                        name: "journals", flags: O_RDONLY | O_DIRECTORY)
        let journal = try POSIX.openAt(journals, name: admission.attempt, flags: O_RDONLY | O_DIRECTORY)
        XCTAssertTrue(try POSIX.directoryEntryNames(journal).contains("0004-quarantined.json"))
    }

    func testPostRenameFailureAppendsIndeterminateWithoutQuarantineMove() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let digest = try SHA256Digest(String(repeating: "a", count: 64))
        for record in [
            try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission),
            try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest),
            try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: admission,
                                     publishedClosureSHA256: digest, receiptFileSHA256: digest)
        ] { try StateMachine.append(record, using: f.durableStore, admission: admission) }

        try StateMachine.terminalizePostRename(using: f.durableStore, admission: admission, failure: .postRenameDurability)
        XCTAssertEqual(f.durableStore.currentState, .publishIndeterminate)
        XCTAssertFalse(f.fakeFS.quarantineState)
        XCTAssertEqual(try JournalRecordV2.decode(f.durableStore.journalRecords.last!).filename,
                       "0004-publish-indeterminate.json")
    }

    func testEveryJournalSeamRejectsTheAppendWithoutReplacingPriorRecord() throws {
        for seam in StateMachine.Seam.allCases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
            let stage = try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission)

            XCTAssertThrowsError(try StateMachine.append(stage, using: f.durableStore.throwingAt(seam), admission: admission))
            XCTAssertEqual(f.durableStore.currentState, .admitted, "\(seam)")
            XCTAssertTrue(f.durableStore.journalRecords.isEmpty, "\(seam)")
        }
    }

    func testConflictingConcurrentJournalAppendsHaveOneWinner() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        let stage = try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission)
        let results = try raceTwo(startBarrier: f.admissionBarrier) { _ in
            try f.admissionBarrier.arriveAndWait()
            try StateMachine.append(stage, using: f.durableStore, admission: admission)
        }
        XCTAssertEqual(results.filter { if case .success = $0 { return true }; return false }.count, 1)
        XCTAssertEqual(f.durableStore.journalRecords.count, 1)
    }
}
