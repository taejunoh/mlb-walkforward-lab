import XCTest
import Foundation
@testable import R4ProtectedExecutionProvisioner

final class BundleTraversalTests: XCTestCase {
    func testAllPreAdmissionFailuresCreateNoAdmissionOrProcess() throws {
        for mutation in [FixtureMutation.v1Bundle, .mixedGraph, .wrongApproval, .badLock, .extraChild, .swappedArchive, .swappedWheel, .swappedCheckout, .symlink, .hardlink, .caseCollision, .nonRootEUID, .copiedHelper, .badStorage, .forbiddenEnvironment, .inheritedDescriptor] {
            let fixture = try MaterializationFixture.valid().mutating(mutation)
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            XCTAssertThrowsError(try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: fixture.adapters), "\(mutation)")
            XCTAssertEqual(fixture.fakeFS.admissionRecords, [], "\(mutation)")
            XCTAssertEqual(fixture.fakeProcess.launches, [], "\(mutation)")
        }
    }

    func testValidBundleIsTypedAndHeldBeforeAdmission() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let valid = try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: fixture.adapters)
        XCTAssertEqual(valid.root.path, fixture.request.inputBundle.path)
        XCTAssertEqual(fixture.fakeFS.admissionRecords, [])
    }

    func testValidatedBundleRetainsHeldFinalApprovalAndItsV2GraphBindings() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let valid = try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: fixture.adapters)
        let expectedApproval = try POSIX.open(fixture.approvalURL.path, flags: O_RDONLY)
        let handoff = try fixture.handoff.openAndRebind()
        let review = try Task14ReviewPackageV2.decode(String(decoding: try POSIX.readAll(handoff.reviewPackage), as: UTF8.self))
        let authorization = try Task14ObservationAuthorizationV2.decode(String(decoding: try POSIX.readAll(handoff.observationAuthorization), as: UTF8.self))

        XCTAssertEqual(valid.heldApproval.descriptor.device, expectedApproval.device)
        XCTAssertEqual(valid.heldApproval.descriptor.inode, expectedApproval.inode)
        XCTAssertNoThrow(try V2Evidence.validateApprovalGraph(review, authorization, valid.heldApproval.approval, valid.policy))
        XCTAssertEqual(fixture.fakeFS.admissionRecords, [])
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    func testFixtureBootstrapApprovalRejectsABAReplacementBetweenHeldReadAndRebind() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let adapters = MaterializationAdapters.fixture(fileSystem: fixture.fakeFS, process: fixture.fakeProcess,
                                                       approvalURL: fixture.approvalURL, installedHelperURL: fixture.helperURL,
                                                       approvalSwapAfterRead: { try self.replaceApprovalAWithBAndClonedA(in: fixture) })

        XCTAssertThrowsError(try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: adapters))
        XCTAssertEqual(fixture.fakeFS.admissionRecords, [])
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    func testHeldFinalApprovalGraphRejectsEveryLoadBearingMismatchBeforeAdmission() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let valid = try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: fixture.adapters)
        let handoff = try fixture.handoff.openAndRebind()
        let reviewSource = String(decoding: try POSIX.readAll(handoff.reviewPackage), as: UTF8.self)
        let authorizationSource = String(decoding: try POSIX.readAll(handoff.observationAuthorization), as: UTF8.self)
        let approvalSource = String(decoding: valid.heldApproval.bytes, as: UTF8.self)
        let resealer = try V2Fixture.complete()
        let mismatch = String(repeating: "c", count: 64)

        func assertRejected(_ label: String, review: String = reviewSource,
                            authorization: String = authorizationSource, approval: String = approvalSource) {
            XCTAssertThrowsError(try V2Evidence.validateApprovalGraph(Task14ReviewPackageV2.decode(review),
                                                                        Task14ObservationAuthorizationV2.decode(authorization),
                                                                        Task14ApprovalV2.decode(approval), valid.policy), label)
            XCTAssertEqual(fixture.fakeFS.admissionRecords, [], label)
            XCTAssertEqual(fixture.fakeProcess.launches, [], label)
        }

        for field in ["review_package_sha256", "input_bundle_manifest_sha256", "materialization_policy_sha256",
                      "provisioner_binary_sha256", "provisioner_designated_requirement_sha256",
                      "task14_approval_verification_public_key_sha256"] {
            assertRejected("authorization.\(field)", authorization: resealer.replacing(authorizationSource, field, .string(mismatch)))
        }
        for field in ["review_package_sha256", "observation_authorization_sha256", "input_bundle_manifest_sha256",
                      "materialization_policy_sha256", "provisioner_binary_sha256", "provisioner_designated_requirement_sha256",
                      "task14_approval_verification_public_key_sha256", "sandbox_launcher_descriptor_identity_sha256",
                      "sandbox_launcher_sha256", "sandbox_launcher_codesign_identity_sha256", "sandbox_profile_template_sha256",
                      "child_command_template_sha256", "base_interpreter_sha256", "base_interpreter_mach_o_sha256",
                      "base_interpreter_codesign_identity_sha256"] {
            assertRejected("approval.\(field)", approval: resealer.replacing(approvalSource, field, .string(mismatch)))
        }
    }

    private func replaceApprovalAWithBAndClonedA(in fixture: MaterializationFixture) throws {
        let parent = try POSIX.openDirectory(fixture.root.path)
        let approvalName = fixture.approvalURL.lastPathComponent
        let original = try POSIX.openAt(parent, name: approvalName, flags: O_RDONLY)
        let originalBytes = try POSIX.readAll(original)
        let suffix = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        let replacementB = "approval-b-\(suffix)"
        let replacementA = "approval-a-\(suffix)"
        let displacedA = "approval-original-\(suffix)"
        let displacedB = "approval-b-displaced-\(suffix)"
        let b = try POSIX.createExclusive(parent, name: replacementB, mode: 0o600)
        try POSIX.writeAll(b, Array("not-an-approval".utf8))
        let clonedA = try POSIX.createExclusive(parent, name: replacementA, mode: 0o600)
        try POSIX.writeAll(clonedA, originalBytes)
        try POSIX.replaceAtomically(in: parent, source: approvalName, destination: displacedA)
        try POSIX.replaceAtomically(in: parent, source: replacementB, destination: approvalName)
        try POSIX.replaceAtomically(in: parent, source: approvalName, destination: displacedB)
        try POSIX.replaceAtomically(in: parent, source: replacementA, destination: approvalName)
    }

    func testResealedApprovalCannotChangeGitOrRequiredBlobBindingsBeforeAdmission() throws {
        let vector = try V2Fixture.complete()
        for (field, value) in [("reviewed_head", JSONValue.string(String(repeating: "c", count: 40))), ("reviewed_tree", JSONValue.string(String(repeating: "c", count: 40))), ("required_blob_set_sha256", JSONValue.string(String(repeating: "c", count: 64)))] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let approval = try String(contentsOf: fixture.approvalURL, encoding: .utf8)
            try Data(vector.replacing(approval, field, value).utf8).write(to: fixture.approvalURL, options: .atomic)
            XCTAssertThrowsError(try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: fixture.adapters), field)
            XCTAssertEqual(fixture.fakeFS.admissionRecords, [], field)
            XCTAssertEqual(fixture.fakeProcess.launches, [], field)
        }
    }
}
