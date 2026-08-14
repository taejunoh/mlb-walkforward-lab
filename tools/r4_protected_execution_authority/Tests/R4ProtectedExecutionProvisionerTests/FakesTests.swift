import Foundation
import XCTest
@testable import R4ProtectedExecutionProvisioner

private enum FutureCopySeam: FixtureFaultSeam, FixtureSwapSeam {
    case copy
    var fixtureFault: FixtureFault { .copy }
    var fixtureSwap: FixtureFault { .copy }
}

private enum FutureAdmissionSeam: FixtureAdmissionFaultSeam {
    case rebind
    var fixtureAdmissionFault: AdmissionFault { .admissionRebind }
}

final class FakesTests: XCTestCase {
    func testDirectoryEntrySnapshotsAreRepeatableWithoutAdvancingHeldDescriptor() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-directory-snapshot-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let first = try POSIX.createExclusive(root, name: "first", mode: 0o600)
        let second = try POSIX.createExclusive(root, name: "second", mode: 0o600)
        try POSIX.writeAll(first, Array("a".utf8))
        try POSIX.writeAll(second, Array("b".utf8))

        XCTAssertEqual(try POSIX.directoryEntryNames(root), Set(["first", "second"]))
        XCTAssertEqual(try POSIX.directoryEntryNames(root), Set(["first", "second"]))
        XCTAssertEqual(try POSIX.readAll(try POSIX.openAt(root, name: "first", flags: O_RDONLY)), Array("a".utf8))
    }

    func testDirectoryEntrySnapshotDoesNotResetOrAdvanceOriginalDirectoryOffset() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-directory-offset-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        _ = try POSIX.createExclusive(root, name: "first", mode: 0o600)
        _ = try POSIX.createExclusive(root, name: "second", mode: 0o600)
        let before = Darwin.lseek(root.fd, 0, SEEK_END)
        guard before >= 0 else { throw POSIXFailure.system(errno) }
        XCTAssertNotEqual(before, 0)

        XCTAssertEqual(try POSIX.directoryEntryNames(root), Set(["first", "second"]))
        let after = Darwin.lseek(root.fd, 0, SEEK_CUR)
        guard after >= 0 else { throw POSIXFailure.system(errno) }
        XCTAssertEqual(after, before)
    }

    func testSealedMetadataClassifierAcceptsBaselineAndRejectsEachMetadataMutation() throws {
        let baseline = SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o444,
                                              nlink: 1, flags: UInt32(SF_IMMUTABLE),
                                              acl: .none, xattrs: ["com.apple.provenance"])
        XCTAssertNoThrow(try POSIX.validateSealedMetadata(baseline, expectedType: S_IFREG,
                                                           mode: 0o444, exactNlink: true))
        let directory = SealedMetadataSnapshot(type: S_IFDIR, uid: 0, gid: 0, mode: 0o555,
                                                nlink: 2, flags: UInt32(SF_IMMUTABLE),
                                                acl: .none, xattrs: ["com.apple.provenance"])
        XCTAssertNoThrow(try POSIX.validateSealedMetadata(directory, expectedType: S_IFDIR,
                                                           mode: 0o555, exactNlink: false))
        let missingDirectoryLinks = SealedMetadataSnapshot(type: S_IFDIR, uid: 0, gid: 0, mode: 0o555,
                                                           nlink: 0, flags: UInt32(SF_IMMUTABLE),
                                                           acl: .none, xattrs: ["com.apple.provenance"])
        XCTAssertThrowsError(try POSIX.validateSealedMetadata(missingDirectoryLinks,
                                                              expectedType: S_IFDIR,
                                                              mode: 0o555, exactNlink: false))
        let mutations: [SealedMetadataSnapshot] = [
            SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o444, nlink: 2, flags: baseline.flags, acl: .none, xattrs: baseline.xattrs),
            SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o444, nlink: 1, flags: baseline.flags, acl: .entries, xattrs: baseline.xattrs),
            SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o444, nlink: 1, flags: 0, acl: .none, xattrs: baseline.xattrs),
            SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o444, nlink: 1, flags: baseline.flags, acl: .none, xattrs: ["com.apple.r4-unexpected"]),
            SealedMetadataSnapshot(type: S_IFDIR, uid: 0, gid: 0, mode: 0o444, nlink: 1, flags: baseline.flags, acl: .none, xattrs: baseline.xattrs),
            SealedMetadataSnapshot(type: S_IFREG, uid: 501, gid: 20, mode: 0o444, nlink: 1, flags: baseline.flags, acl: .none, xattrs: baseline.xattrs),
            SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o600, nlink: 1, flags: baseline.flags, acl: .none, xattrs: baseline.xattrs)
        ]
        for mutation in mutations {
            XCTAssertThrowsError(try POSIX.validateSealedMetadata(mutation, expectedType: S_IFREG,
                                                                  mode: 0o444, exactNlink: true))
        }
    }

    func testPublicationOperationsRecordImmutableDurabilityAndParentOrder() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-publication-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let first = try POSIX.mkdirAt(root, name: "first", mode: 0o700)
        let second = try POSIX.mkdirAt(root, name: "second", mode: 0o700)
        var events: [String] = []
        let operations = PublicationPOSIX.Operations(
            setOwnership: { _, _, _ in events.append("ownership") },
            setMode: { _, _ in events.append("mode") },
            writeAll: { descriptor, bytes in events.append("write"); try POSIX.writeAll(descriptor, bytes) },
            durableRegular: { _ in events.append("durable-file") },
            setImmutable: { _ in events.append("immutable") },
            rebind: { descriptor, parent, name in events.append("rebind"); return try descriptor.rebindSameIdentity(in: parent, name: name) },
            readAll: { descriptor in events.append("read"); return try POSIX.readAll(descriptor) },
            verifySealed: { _, _ in events.append("verify") },
            durableDirectory: { descriptor in
                events.append("durable-parent-" + String(descriptor.device) + ":" + String(descriptor.inode))
            }
        )
        _ = try PublicationPOSIX.createImmutableExclusive(root, "record", Array("bytes".utf8), mode: 0o444, operations: operations)
        try PublicationPOSIX.fsyncParents(first, second, root: root, operations: operations)
        XCTAssertEqual(Array(events.prefix(9)), ["ownership", "mode", "write", "durable-file", "immutable",
                                                  "durable-file", "rebind", "read", "verify"])
        XCTAssertEqual(Array(events.dropFirst(9)), [
            "durable-parent-" + String(first.device) + ":" + String(first.inode),
            "durable-parent-" + String(second.device) + ":" + String(second.inode),
            "durable-parent-" + String(root.device) + ":" + String(root.inode)
        ])
    }

    func testBoundedEINTRRetrySucceedsThenFailsClosedOnPersistentOrHardError() throws {
        var interruptedAttempts = 0
        XCTAssertNoThrow(try POSIX.retryEINTR(3) {
            interruptedAttempts += 1
            if interruptedAttempts < 2 { errno = EINTR; return -1 }
            return 0
        })
        XCTAssertEqual(interruptedAttempts, 2)

        var persistentAttempts = 0
        XCTAssertThrowsError(try POSIX.retryEINTR(3) {
            persistentAttempts += 1
            errno = EINTR
            return -1
        }) { error in XCTAssertEqual(error as? POSIXFailure, .system(EINTR)) }
        XCTAssertEqual(persistentAttempts, 3)

        XCTAssertThrowsError(try POSIX.retryEINTR(3) {
            errno = EACCES
            return -1
        }) { error in XCTAssertEqual(error as? POSIXFailure, .system(EACCES)) }
    }

    func testPublicationParentDurabilityPropagatesFailureWithoutAttemptingRootBarrier() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-publication-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let first = try POSIX.mkdirAt(root, name: "first", mode: 0o700)
        let second = try POSIX.mkdirAt(root, name: "second", mode: 0o700)
        var calls = 0
        let operations = PublicationPOSIX.Operations(
            setOwnership: { _, _, _ in }, setMode: { _, _ in }, writeAll: { _, _ in },
            durableRegular: { _ in }, setImmutable: { _ in },
            rebind: { descriptor, parent, name in try descriptor.rebindSameIdentity(in: parent, name: name) },
            readAll: { _ in [] }, verifySealed: { _, _ in },
            durableDirectory: { _ in
                calls += 1
                if calls == 2 { throw POSIXFailure.system(EIO) }
            }
        )
        XCTAssertThrowsError(try PublicationPOSIX.fsyncParents(first, second, root: root, operations: operations)) { error in
            XCTAssertEqual(error as? POSIXFailure, .system(EIO))
        }
        XCTAssertEqual(calls, 2)
    }

    func testDisposableMetadataMutationsAreObservedByRealDarwinSyscalls() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-publication-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let hardlinked = try fixture.fakeMetadata("nlink")
        XCTAssertEqual(try POSIX.metadata(hardlinked).nlink, 2)
        let acl = try fixture.fakeMetadata("acl")
        XCTAssertThrowsError(try POSIX.requireNoExtendedACL(acl))
        let xattr = try fixture.fakeMetadata("xattr")
        XCTAssertThrowsError(try POSIX.requireNoUnexpectedExtendedAttributes(xattr))
    }

    func testUnprivilegedMetadataWithProviderXattrFailsClosedAndPreservesForeignSentinel() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-publication-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let foreign = fixture.root.appendingPathComponent("foreign-sentinel")
        try Data("sentinel".utf8).write(to: foreign)
        XCTAssertThrowsError(try fixture.fakeMetadata("xattr").requireSealedRegular(mode: 0o444))
        XCTAssertEqual(try Data(contentsOf: foreign), Data("sentinel".utf8))
    }

    func testRebindSameIdentityRejectsReplacementAndPreservesForeignBytes() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-publication-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let original = try POSIX.createExclusive(root, name: "identity", mode: 0o600)
        try POSIX.writeAll(original, Array("original".utf8))
        let replacement = try POSIX.createExclusive(root, name: "replacement", mode: 0o600)
        try POSIX.writeAll(replacement, Array("foreign".utf8))
        try POSIX.replaceAtomically(in: root, source: "identity", destination: "displaced")
        try POSIX.replaceAtomically(in: root, source: "replacement", destination: "identity")
        XCTAssertThrowsError(try original.rebindSameIdentity(in: root, name: "identity"))
        XCTAssertEqual(try Data(contentsOf: fixture.root.appendingPathComponent("identity")), Data("foreign".utf8))
    }

    func testFakeFixtureFactoryDefinesAllLaterFixtures() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }

        XCTAssertNotNil(f.fakeFS)
        XCTAssertNotNil(f.fakeProcess)
        XCTAssertNotNil(f.validArchive)
        XCTAssertNotNil(f.bundleWithExtraChild())
        XCTAssertNotNil(f.swapBeforeCopy())
        XCTAssertTrue(FileManager.default.fileExists(atPath: f.request.inputBundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json").path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: f.approvalURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: f.helperURL.path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: f.validArchive.path.path))
        XCTAssertTrue(f.fakeFS.hasMutation(.bundleExtraChild))
        XCTAssertTrue(f.fakeFS.hasMutation(.swapBeforeCopy))
        XCTAssertFalse(f.fakeFS.hasQuarantineOrRetainedWork)
    }

    func testFakeFSRejectsMutationOutsideFixtureRoot() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }

        XCTAssertThrowsError(try f.fakeFS.mkdir("/Library/x"))
        XCTAssertThrowsError(try f.fakeFS.createFile(at: "/private/tmp/not-this-fixture", bytes: [1]))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testFakeFixtureCleanupOwnsOnlyItsCapturedRoot() throws {
        let f = try MaterializationFixture.valid()
        let foreign = URL(fileURLWithPath: "/private/tmp/r4-materialization-foreign-\(UUID().uuidString)")
        defer {
            try? f.removeOnlySelfOwnedFixture()
            if let attrs = try? FileManager.default.attributesOfItem(atPath: f.root.path), attrs[.type] as? FileAttributeType == .typeSymbolicLink { try? FileManager.default.removeItem(at: f.root) }
            try? FileManager.default.removeItem(at: foreign)
        }
        try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: false)
        try f.removeOnlySelfOwnedFixture()
        XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: f.root.path))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testFakeFixtureCleanupNeverDeletesForeignSwapOrSymlinkTarget() throws {
        let f = try MaterializationFixture.valid()
        let foreign = URL(fileURLWithPath: "/private/tmp/r4-materialization-foreign-\(UUID().uuidString)")
        defer { try? f.removeOnlySelfOwnedFixture(); try? FileManager.default.removeItem(at: foreign) }
        try FileManager.default.removeItem(at: f.root)
        try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: false)
        try Data("foreign".utf8).write(to: foreign.appendingPathComponent("sentinel"))
        try FileManager.default.createSymbolicLink(atPath: f.root.path, withDestinationPath: foreign.path)
        try f.removeOnlySelfOwnedFixture()
        XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.appendingPathComponent("sentinel").path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: f.root.path))
        try FileManager.default.removeItem(at: f.root)
        XCTAssertFalse(FileManager.default.fileExists(atPath: f.root.path))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testFakeFSRejectsRootAndIntermediateSymlinkSwapsWithoutTouchingSentinel() throws {
        let f = try MaterializationFixture.valid()
        let foreign = URL(fileURLWithPath: "/private/tmp/r4-materialization-foreign-\(UUID().uuidString)")
        defer {
            try? f.removeOnlySelfOwnedFixture()
            if let attrs = try? FileManager.default.attributesOfItem(atPath: f.root.path), attrs[.type] as? FileAttributeType == .typeSymbolicLink { try? FileManager.default.removeItem(at: f.root) }
            try? FileManager.default.removeItem(at: foreign)
        }
        try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: false)
        try Data("sentinel".utf8).write(to: foreign.appendingPathComponent("sentinel"))
        try FileManager.default.createSymbolicLink(atPath: f.root.appendingPathComponent("bundle/escape").path, withDestinationPath: foreign.path)
        XCTAssertThrowsError(try f.fakeFS.createFile(at: f.root.appendingPathComponent("bundle/escape/outside").path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.appendingPathComponent("sentinel").path))
        try FileManager.default.removeItem(at: f.root)
        try FileManager.default.createSymbolicLink(atPath: f.root.path, withDestinationPath: foreign.path)
        XCTAssertThrowsError(try f.fakeFS.mkdir(f.root.appendingPathComponent("outside").path))
        XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.appendingPathComponent("sentinel").path))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testMutationAndFaultHelpersChangeTypedObservableSeams() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        _ = f.swapBefore(.copy)
        _ = f.fault(after: .fsync)
        _ = f.admissionFault(.afterOpen)
        XCTAssertTrue(f.fakeFS.hasMutation(.swapBeforeCopy))
        let swapped = try MaterializationFixture.valid().swapBeforeCopy()
        defer { try? swapped.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try swapped.fakeFS.open(swapped.request.inputBundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json").path))
        XCTAssertEqual(f.fakeFS.fault, .fsync)
        XCTAssertEqual(f.storage.admissionFault, .afterOpen)
        XCTAssertThrowsError(try f.storage.admit())
        XCTAssertTrue(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertEqual(f.fakeProcess.launches, [])
        XCTAssertEqual(swapped.fakeProcess.launches, [])
    }

    func testSingleAdmissionDoesNotArmOrWaitOnTheRaceBarrier() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertFalse(f.admissionBarrier.isArmed)
        XCTAssertFalse(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertNoThrow(try f.storage.admit())
        XCTAssertFalse(f.admissionBarrier.isArmed)
        XCTAssertFalse(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testClosedTask4MutationCatalogHasDistinctFixtureObservations() throws {
        XCTAssertEqual(FixtureMutation.allCases.count, 17)
        XCTAssertEqual(Set(FixtureMutation.allCases.map(\.rawValue)).count, FixtureMutation.allCases.count)
        for mutation in FixtureMutation.allCases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            _ = f.mutating(mutation)
            XCTAssertTrue(f.fakeFS.hasMutation(mutation), "missing observation for \(mutation)")
            switch mutation {
            case .nonRootEUID: XCTAssertEqual(f.fakeProcess.effectiveEUID, 501)
            case .forbiddenEnvironment: XCTAssertEqual(f.fakeProcess.environmentOverride["HOME"], "/tmp/forbidden")
            case .inheritedDescriptor: XCTAssertTrue(f.fakeProcess.inheritedDescriptor)
            case .badStorage: XCTAssertTrue(f.fakeFS.storageRejected)
            default: break
            }
        }
    }

    func testClosedAdmissionFaultCatalogFailsAtItsNamedSeam() throws {
        XCTAssertEqual(AdmissionFault.allCases.count, 5)
        for fault in AdmissionFault.allCases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            _ = f.admissionFault(fault)
            XCTAssertThrowsError(try f.storage.admit(), "fault \(fault)")
            if fault == .beforeOpen { XCTAssertTrue(f.fakeFS.admissionRecords.isEmpty) }
            else {
                XCTAssertTrue(f.fakeFS.hasQuarantineOrRetainedWork)
                if fault == .ownerDeath { XCTAssertTrue(f.fakeFS.events.contains(.admissionOwnerDeath)) }
                if fault == .admissionFsync { XCTAssertTrue(f.fakeFS.events.contains(.admissionFsyncFailure)) }
                if fault == .admissionRebind { XCTAssertTrue(f.fakeFS.events.contains(.admissionRebindFailure)) }
            }
            XCTAssertEqual(f.fakeProcess.launches, [], "fault \(fault)")
        }
    }

    func testFakeProcessRequiresExactApprovedInvocation() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let target = f.fakeProcess.approvedExecutable
        XCTAssertNoThrow(try f.fakeProcess.launch(executable: target, argv: f.fakeProcess.approvedArgv,
                                                    cwd: f.fakeProcess.approvedCWD,
                                                    environment: f.fakeProcess.approvedEnvironment,
                                                    profile: f.fakeProcess.approvedProfile,
                                                    stdin: "/dev/null", capabilities: []))
        XCTAssertThrowsError(try f.fakeProcess.launch(executable: "/usr/bin/env", argv: [], cwd: f.root.path,
                                                       environment: [:], profile: "", stdin: "/dev/null", capabilities: []))
        let alternate = "/usr/bin/fixture-tool"
        f.fakeProcess.approve(executable: alternate)
        XCTAssertNoThrow(try f.fakeProcess.launch(alternate))
    }

    func testAdmissionBarrierRunsBothCallersBeforeReleasingExclusiveSeam() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let results = try f.raceTwo(startBarrier: f.admissionBarrier) { _ in
            try f.storage.admit()
        }
        XCTAssertEqual(results.count, 2)
        let successes = results.filter { if case .success = $0 { return true }; return false }
        XCTAssertEqual(successes.count, 1)
        XCTAssertEqual(f.fakeFS.events.filter { $0 == .openExclusiveAttempt }.count, 1)
        XCTAssertEqual(f.fakeFS.events.filter { $0 == .openExclusive }.count, 1)
        guard let successIndex = f.fakeFS.events.firstIndex(of: .openExclusive) else { return XCTFail("missing exclusive success") }
        XCTAssertEqual(f.fakeFS.events[..<successIndex].filter { $0 == .admissionBarrierArrival }.count, 2)
        XCTAssertEqual(f.fakeFS.events[..<successIndex].filter { $0 == .openExclusiveAttempt }.count, 1)
        XCTAssertEqual(f.fakeFS.admissionRecords.count, 1)
        XCTAssertEqual(Array(f.fakeFS.events.suffix(5)), [.writeAdmission, .fsyncAdmission,
                                                           .immutableAdmission, .rebind,
                                                           .fsyncAdmissionsParent])
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testFutureTypedSeamsMapToRealFaultAndSwapBehavior() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        _ = f.fault(after: FutureCopySeam.copy)
        XCTAssertThrowsError(try f.fakeFS.open(f.request.inputBundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json").path))
        let swapped = try MaterializationFixture.valid()
        defer { try? swapped.removeOnlySelfOwnedFixture() }
        _ = swapped.swapBefore(FutureCopySeam.copy)
        XCTAssertTrue(swapped.fakeFS.hasMutation(.swapBeforeCopy))
        XCTAssertThrowsError(try swapped.fakeFS.open(swapped.request.inputBundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json").path))
        _ = f.admissionFault(FutureAdmissionSeam.rebind)
        XCTAssertThrowsError(try f.storage.admit())
        XCTAssertTrue(f.fakeFS.events.contains(.admissionRebindFailure))
        XCTAssertEqual(f.fakeProcess.launches, [])
        XCTAssertEqual(swapped.fakeProcess.launches, [])
    }

    func testMutationSecurityCasesCauseBehavioralRejection() throws {
        let cases: [FixtureMutation] = [.badStorage, .nonRootEUID, .forbiddenEnvironment, .inheritedDescriptor]
        for mutation in cases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            _ = f.mutating(mutation)
            if mutation == .badStorage {
                XCTAssertThrowsError(try f.storage.admit())
            } else {
                XCTAssertThrowsError(try f.fakeProcess.launch(executable: f.fakeProcess.approvedExecutable,
                                                               argv: f.fakeProcess.approvedArgv,
                                                               cwd: f.fakeProcess.approvedCWD,
                                                               environment: f.fakeProcess.approvedEnvironment,
                                                               profile: f.fakeProcess.approvedProfile,
                                                               stdin: "/dev/null", capabilities: []))
            }
            XCTAssertEqual(f.fakeProcess.launches, [], "\(mutation)")
        }
    }

    func testDurableQuarantineMarksFakeFileSystemState() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertFalse(f.fakeFS.quarantineState)
        try f.durableStore.quarantineAfterFailure(try f.storage.placeholderAdmission("a"))
        XCTAssertTrue(f.fakeFS.quarantineState)
        XCTAssertTrue(f.fakeFS.retainedWorkState)
    }

    func testAdmissionsParentFsyncUsesDescriptorAndInjectsFailure() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertNoThrow(try f.fakeFS.fsyncAdmissionsParent())
        f.fakeFS.fault = .fsync
        XCTAssertThrowsError(try f.fakeFS.fsyncAdmissionsParent())
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testMigrationFixturesExposeObservationAndHandoffFaults() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        f.handoff.swapAfterOpening(.reviewPackage)
        XCTAssertThrowsError(try HeldCeremonyInputs(store: f.handoff))
        XCTAssertThrowsError(try f.observation.failAfterDescriptorRebind())
        XCTAssertThrowsError(try f.archive.failAfterFinalRename())
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testMigrationFixturesKeepCeremonyHandoffDistinctFromAdmission() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let handoff = try f.handoff.openAndRebind()
        _ = try HeldCeremonyInputs(handoff: handoff)
        XCTAssertEqual(Set([handoff.root, handoff.reviewPackage, handoff.observationAuthorization, handoff.observationSignature, handoff.keyAPublic].map { ObjectIdentifier($0) }).count, 5)
        XCTAssertEqual(f.fakeFS.admissionRecords, [])
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testConfiguredKeyAFaultsAreObservedThroughTheHandoffConsumer() throws {
        let review = try MaterializationFixture.valid()
        defer { try? review.removeOnlySelfOwnedFixture() }
        review.keyAStore.failReviewRebind = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: review.handoff))
        XCTAssertEqual(review.keyAStore.reviewRebinds, 0)
        XCTAssertEqual(review.fakeProcess.launches, [])

        let authorization = try MaterializationFixture.valid()
        defer { try? authorization.removeOnlySelfOwnedFixture() }
        authorization.keyAStore.failAuthorizationRebind = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: authorization.handoff))
        XCTAssertEqual(authorization.keyAStore.authorizationRebinds, 0)
        XCTAssertEqual(authorization.fakeProcess.launches, [])

        let signature = try MaterializationFixture.valid()
        defer { try? signature.removeOnlySelfOwnedFixture() }
        signature.keyAStore.failReviewSignature = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: signature.handoff))
        XCTAssertGreaterThan(signature.keyAStore.reviewRebinds, 0)
        XCTAssertEqual(signature.fakeProcess.launches, [])

        let authorizationSignature = try MaterializationFixture.valid()
        defer { try? authorizationSignature.removeOnlySelfOwnedFixture() }
        authorizationSignature.keyAStore.failAuthorizationSignature = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: authorizationSignature.handoff))
        XCTAssertGreaterThan(authorizationSignature.keyAStore.authorizationRebinds, 0)
        XCTAssertEqual(authorizationSignature.fakeProcess.launches, [])
    }

    func testObservationConsumerExercisesSecurityAndPinnedLauncherSeamsWithoutSuccessSynthesis() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let descriptor = try POSIX.open(f.helperURL.path, flags: O_RDONLY)
        let consumer = CandidateObservationAdapter(security: f.observation, launcher: f.launcher)
        XCTAssertThrowsError(try consumer.observeLauncher(descriptor: descriptor, at: f.helperURL))
        XCTAssertEqual(f.observation.descriptorRebinds, 1)
        XCTAssertEqual(f.launcher.observations, 0)
        XCTAssertThrowsError(try f.launcher.observeSecurity(descriptor, at: f.helperURL))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testObservationFixtureFailsClosedAfterABASwapBeforePinnedLauncher() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let held = try POSIX.open(f.helperURL.path, flags: O_RDONLY)
        let original = try Data(contentsOf: f.helperURL)
        f.observation.performABASwapAfterDescriptorRebind()
        let consumer = CandidateObservationAdapter(security: f.observation, launcher: f.launcher)
        XCTAssertThrowsError(try consumer.observeLauncher(descriptor: held, at: f.helperURL)) { error in
            XCTAssertEqual(error as? CeremonyAdapterFailure, .observationUnavailable)
        }
        XCTAssertEqual(try Data(contentsOf: f.helperURL), original)
        XCTAssertEqual(f.observation.abaSwaps, 1)
        XCTAssertEqual(f.launcher.observations, 0)
        let launcherHeld = try POSIX.open(f.helperURL.path, flags: O_RDONLY)
        f.launcher.performABASwapAfterDescriptorRebind()
        XCTAssertThrowsError(try f.launcher.observeSecurity(launcherHeld, at: f.helperURL)) { error in
            XCTAssertEqual(error as? CeremonyAdapterFailure, .launcherObservation)
        }
        XCTAssertEqual(try Data(contentsOf: f.helperURL), original)
        XCTAssertEqual(f.launcher.descriptorRebinds, 1)
        XCTAssertEqual(f.launcher.abaSwaps, 1)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testObservationFakeRejectsIntermediateSymlinkBeforeForeignABASwap() throws {
        let f = try MaterializationFixture.valid()
        let foreign = URL(fileURLWithPath: "/private/tmp/r4-materialization-foreign-\(UUID().uuidString)")
        defer {
            try? f.removeOnlySelfOwnedFixture()
            try? FileManager.default.removeItem(at: foreign)
        }
        try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: false)
        let sentinel = foreign.appendingPathComponent("candidate")
        try Data("foreign-sentinel".utf8).write(to: sentinel)
        let hop = f.root.appendingPathComponent("observation-hop")
        try FileManager.default.createSymbolicLink(atPath: hop.path, withDestinationPath: foreign.path)
        let path = hop.appendingPathComponent("candidate")
        let held = try POSIX.open(path.path, flags: O_RDONLY)
        f.observation.performABASwapAfterDescriptorRebind()

        XCTAssertThrowsError(try f.observation.observe(held, at: path)) { error in
            XCTAssertEqual(error as? CeremonyAdapterFailure, .descriptorRebind)
        }
        XCTAssertEqual(try Data(contentsOf: sentinel), Data("foreign-sentinel".utf8))
        XCTAssertEqual(f.observation.abaSwaps, 0)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testPinnedLauncherFakeRejectsIntermediateSymlinkBeforeForeignABASwap() throws {
        let f = try MaterializationFixture.valid()
        let foreign = URL(fileURLWithPath: "/private/tmp/r4-materialization-foreign-\(UUID().uuidString)")
        defer {
            try? f.removeOnlySelfOwnedFixture()
            try? FileManager.default.removeItem(at: foreign)
        }
        try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: false)
        let sentinel = foreign.appendingPathComponent("candidate")
        try Data("foreign-sentinel".utf8).write(to: sentinel)
        let hop = f.root.appendingPathComponent("launcher-hop")
        try FileManager.default.createSymbolicLink(atPath: hop.path, withDestinationPath: foreign.path)
        let path = hop.appendingPathComponent("candidate")
        let held = try POSIX.open(path.path, flags: O_RDONLY)
        f.launcher.performABASwapAfterDescriptorRebind()

        XCTAssertThrowsError(try f.launcher.observeSecurity(held, at: path)) { error in
            XCTAssertEqual(error as? CeremonyAdapterFailure, .descriptorRebind)
        }
        XCTAssertEqual(try Data(contentsOf: sentinel), Data("foreign-sentinel".utf8))
        XCTAssertEqual(f.launcher.abaSwaps, 0)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testCeremonyHandoffNormalizesEachChildReplacement() throws {
        for child in FakeCeremonyHandoffChild.allCases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            let handoff = try f.handoff.openAndRebind()
            try f.keyAStore.replaceHeldChild(child)
            XCTAssertThrowsError(try HeldCeremonyInputs(handoff: handoff), "\(child)") { error in
                XCTAssertEqual(error as? CeremonyAdapterFailure, .descriptorRebind)
            }
            XCTAssertEqual(f.keyAStore.swappedChildren, [child])
            XCTAssertEqual(f.fakeProcess.launches, [])
        }
    }

    func testArchiveAdapterConfiguredFaultsTraverseSharedCopyAndExtractor() throws {
        let success = try MaterializationFixture.valid()
        defer { try? success.removeOnlySelfOwnedFixture() }
        let final = try success.archive.copyAndExtract()
        XCTAssertEqual(final.relativePath, "runtime/closure/python311")
        XCTAssertTrue(success.archive.usedSharedExtractor)
        XCTAssertEqual(success.fakeProcess.launches, [])

        for fault in FakeArchiveFault.allCases {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            f.archive.fault = fault
            XCTAssertThrowsError(try f.archive.copyAndExtract(), "\(fault)")
            XCTAssertEqual(f.fakeProcess.launches, [])
        }
    }

    func testMigrationFixturesExposeEachKeyAAndArchiveFault() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        f.keyAStore.failReviewRebind = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: f.handoff))
        f.keyAStore.failReviewRebind = false
        f.keyAStore.failAuthorizationRebind = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: f.handoff))
        f.keyAStore.failAuthorizationRebind = false
        f.keyAStore.failReviewSignature = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: f.handoff))
        f.keyAStore.failReviewSignature = false
        f.keyAStore.failAuthorizationSignature = true
        XCTAssertThrowsError(try HeldCeremonyInputs(store: f.handoff))
        for fault in FakeArchiveFault.allCases {
            f.archive.fault = fault
            XCTAssertThrowsError(try f.archive.copyAndExtract(), "\(fault)")
            f.archive.fault = nil
        }
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testInjectedFixtureInitializationLeavesNoResidue() throws {
        let root = URL(fileURLWithPath: "/private/tmp/r4-materialization-init-failure-\(UUID().uuidString)")
        XCTAssertThrowsError(try MaterializationFixture.valid(injecting: .afterApproval, at: root))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
    }

    func testRaceWorkersRendezvousBeforeOperationAndJoinBeforeReturn() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let live = NSLock(); var workersLive = 0
        let results = try f.raceTwo(startBarrier: f.admissionBarrier) { _ in
            live.lock(); workersLive += 1; live.unlock()
            defer { live.lock(); workersLive -= 1; live.unlock() }
            return try f.storage.admit()
        }
        XCTAssertEqual(results.count, 2)
        live.lock(); let remaining = workersLive; live.unlock()
        XCTAssertEqual(remaining, 0)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testRaceTimeoutCancelsCooperativeOperationAndJoinsBothWorkers() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let lock = NSLock(); var calls = 0; var completed = 0
        XCTAssertThrowsError(try f.raceTwo(startBarrier: f.admissionBarrier, timeout: 0.05) { cancellation in
            lock.lock(); calls += 1; let call = calls; lock.unlock()
            if call == 1 {
                _ = cancellation.waitUntilCancelled(timeout: 2)
                lock.lock(); completed += 1; lock.unlock()
                throw FakeAdapterError.barrierTimeout
            }
            do {
                _ = try f.storage.admit()
                XCTFail("cancelled storage worker must not admit")
            } catch { }
            lock.lock(); completed += 1; lock.unlock()
            return try f.storage.placeholderAdmission("timeout-drain")
        })
        lock.lock(); let finalCalls = calls; let finalCompleted = completed; lock.unlock()
        XCTAssertEqual(finalCalls, 2)
        XCTAssertEqual(finalCompleted, 2)
        XCTAssertEqual(f.fakeFS.events.filter { $0 == .openExclusive }.count, 0)
        XCTAssertEqual(f.fakeFS.events.filter { $0 == .writeAdmission }.count, 0)
        XCTAssertTrue(f.fakeFS.admissionRecords.isEmpty)
        XCTAssertTrue(f.admissionBarrier.isCancelled)
        XCTAssertTrue(f.admissionBarrier.isReleased)
        XCTAssertFalse(f.admissionBarrier.isBlocking)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testDelayedCancelledWorkersCannotEnterStorageCreateAndBothJoin() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let lock = NSLock(); var completed = 0
        XCTAssertThrowsError(try f.raceTwo(startBarrier: f.admissionBarrier, timeout: 0.05) { cancellation in
            _ = cancellation.waitUntilCancelled(timeout: 2)
            do {
                _ = try f.storage.admit()
                XCTFail("late cancelled admission must fail closed")
            } catch { }
            lock.lock(); completed += 1; lock.unlock()
            throw FakeAdapterError.barrierTimeout
        })
        lock.lock(); let joined = completed; lock.unlock()
        XCTAssertEqual(joined, 2)
        XCTAssertEqual(f.fakeFS.events.filter { $0 == .openExclusive }.count, 0)
        XCTAssertEqual(f.fakeFS.events.filter { $0 == .writeAdmission }.count, 0)
        XCTAssertTrue(f.fakeFS.admissionRecords.isEmpty)
        XCTAssertTrue(f.admissionBarrier.isCancelled)
        XCTAssertTrue(f.admissionBarrier.isReleased)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }
}
