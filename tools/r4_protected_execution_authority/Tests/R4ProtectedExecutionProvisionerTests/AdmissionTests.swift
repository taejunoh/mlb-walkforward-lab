import XCTest
@testable import R4ProtectedExecutionProvisioner

final class AdmissionTests: XCTestCase {
    func testAdmissionAcceptsOnlyValidatedBundleAndPersistsBeforeStage() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let bundle = try BundleVerifier.verifyBeforeAdmission(f.request, adapters: f.adapters)
        let _: (ValidatedBundle, Storage) throws -> Admission = AdmissionController.admit
        let admission = try AdmissionController.admit(bundle, storage: f.storage)
        XCTAssertEqual(f.fakeFS.admissionRecords.count, 1)
        XCTAssertEqual(admission.attempt, "0123456789abcdef0123456789abcdef")
        XCTAssertEqual(f.durableStore.currentState, .admitted)
        XCTAssertTrue(f.fakeFS.events.contains(.writeAdmission))
    }

    func testAdmissionReturnsHeldDescriptorAndUsesAttestedVolume() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        XCTAssertEqual(admission.record.name, admission.attempt + ".json")
        XCTAssertEqual(try f.storage.stageName(admission), admission.attempt)
        XCTAssertEqual(admission.record.file.inode, try f.storage.reboundAdmissionInode())
        guard let bytes = f.fakeFS.admissionRecords.first else { return XCTFail("missing admission bytes") }
        guard case .object(let fields) = try CanonicalJSON.decode(bytes: [UInt8](bytes)) else { return XCTFail("admission is not an object") }
        XCTAssertEqual(Set(fields.map { $0.0 }), ["kind", "namespace", "authority_id", "attempt_uuid",
                                                "task14_approval_sha256", "input_bundle_manifest_sha256",
                                                "materialization_policy_sha256", "storage_volume_uuid", "sha256"])
        XCTAssertEqual(String(decoding: bytes, as: UTF8.self).contains("\"storage_volume_uuid\":\"123e4567-e89b-12d3-a456-426614174000\""), true)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testAdmissionRejectsEveryDurableRegistryResidueBeforeCreatingRecord() throws {
        for registry in ["control/admissions", "control/journals", "materialization-work", "quarantine", "authorities"] {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            let sentinel = f.root.appendingPathComponent(registry).appendingPathComponent("foreign-sentinel")
            try Data("foreign".utf8).write(to: sentinel, options: .withoutOverwriting)

            XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage), registry)
            XCTAssertEqual(try Data(contentsOf: sentinel), Data("foreign".utf8), registry)
            XCTAssertTrue(f.fakeFS.admissionRecords.isEmpty, registry)
            XCTAssertFalse(f.fakeFS.events.contains(.openExclusive), registry)
        }
    }

    func testAdmissionRejectsSymlinkAndMalformedResidueWithoutFollowingOrReplacingIt() throws {
        for name in ["0123456789ABCDEF0123456789ABCDEF", "0123456789abcdef0123456789abcdeg"] {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            let sentinel = f.root.appendingPathComponent("control/journals").appendingPathComponent(name)
            try FileManager.default.createSymbolicLink(atPath: sentinel.path, withDestinationPath: "/private/tmp")

            XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage), name)
            XCTAssertEqual(try FileManager.default.destinationOfSymbolicLink(atPath: sentinel.path), "/private/tmp")
            XCTAssertFalse(f.fakeFS.events.contains(.openExclusive), name)
        }
    }

    func testAdmissionRejectsMalformedAndAliasedAttemptIdentifiers() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        for attempt in ["0123456789ABCDEF0123456789ABCDEF", "0123456789abcdef0123456789abcdeg", ".", "..", "0123456789abcdef0123456789abcdef.json"] {
            XCTAssertThrowsError(try AdmissionController.document(bundle: f.validatedBundle, attempt: attempt, volumeUUID: f.storage.storageVolumeUUID), attempt)
        }
    }

    func testAdmissionPreconditionErrorLeavesNoRecord() throws {
        let f = try MaterializationFixture.valid().mutating(.badStorage)
        defer { try? f.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage))
        XCTAssertTrue(f.fakeFS.admissionRecords.isEmpty)
        XCTAssertFalse(f.fakeFS.hasQuarantineOrRetainedWork)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testConcurrentAdmissionsHaveOneWinnerAndNoLoserMutation() throws {
        let f = try MaterializationFixture.valid()
        defer { try? f.removeOnlySelfOwnedFixture() }
        let results = try raceTwo(startBarrier: f.admissionBarrier) { _ in
            try AdmissionController.admit(f.validatedBundle, storage: f.storage)
        }
        XCTAssertEqual(results.compactMap { try? $0.get() }.count, 1)
        XCTAssertEqual(f.storage.issuedAttempts.count, 2)
        XCTAssertEqual(Set(f.storage.issuedAttempts).count, 2)
        XCTAssertEqual(f.fakeFS.admissionRecords.count, 1)
        XCTAssertEqual(f.fakeFS.loserCleanupCalls, 0)
        XCTAssertEqual(f.fakeFS.loserMutations, 0)
        XCTAssertEqual(f.fakeProcess.launches, [])
    }

    func testPostSnapshotResidueInEveryRegistryRejectsBeforeExclusiveCreate() throws {
        for registry in ["control/admissions", "control/journals", "materialization-work", "quarantine", "authorities"] {
            let f = try MaterializationFixture.valid()
            defer { try? f.removeOnlySelfOwnedFixture() }
            f.storage.injectPostSnapshotResidue(in: registry)

            XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage), registry)
            let sentinel = f.root.appendingPathComponent(registry).appendingPathComponent("post-snapshot-sentinel")
            XCTAssertEqual(try Data(contentsOf: sentinel), Data("foreign".utf8), registry)
            XCTAssertFalse(f.fakeFS.events.contains(.openExclusive), registry)
        }
    }

    func testEveryPostOpenAdmissionFaultIsTerminalAndCannotRetry() throws {
        for seam in AdmissionSeam.allCases where seam != .beforeOpen {
            let f = try MaterializationFixture.valid().admissionFault(seam)
            defer { try? f.removeOnlySelfOwnedFixture() }
            XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage), "\(seam)")
            XCTAssertTrue(f.fakeFS.hasQuarantineOrRetainedWork)
            XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage))
            XCTAssertEqual(f.fakeProcess.launches, [], "\(seam)")
        }
    }

    func testTerminalizationFailureIsExplicitAndLeavesRetainedResidue() throws {
        let f = try MaterializationFixture.valid().admissionFault(.admissionFsync)
        defer { try? f.removeOnlySelfOwnedFixture() }
        f.storage.terminalizationFails = true
        XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage)) { error in
            XCTAssertEqual(error as? AdmissionFailure, .terminalizationFailed)
        }
        XCTAssertTrue(f.fakeFS.retainedWorkState)
        XCTAssertThrowsError(try AdmissionController.admit(f.validatedBundle, storage: f.storage))
        XCTAssertEqual(f.fakeProcess.launches, [])
    }
}
