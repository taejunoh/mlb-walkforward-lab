import XCTest
import CryptoKit
@testable import R4ProtectedExecutionProvisioner

final class ModelsV2Tests: XCTestCase {
    func testRotationPreimageUsesV2DomainAndExactPredecessorFields() throws {
        let record = try V2Fixtures.rotationRecord(generation: 2)
        XCTAssertEqual(Array(try record.rotationSignaturePreimage().prefix(10)), Array("R4KEYROT2\0".utf8))
        XCTAssertEqual(record.previousGeneration, 1)
    }

    func testV2AdministrativePreimagesMatchIndependentDomainVectors() throws {
        let vectors = try DomainVectorFixture.decode("r4_v2_domain_vectors.json").vectors
        let expected = Dictionary(uniqueKeysWithValues: vectors.map { ($0.domain, $0.expectedSHA256.value) })
        XCTAssertEqual(CanonicalJSON.sha256(try V2Fixtures.rotationRecord(generation: 2).rotationSignaturePreimage()), expected["R4KEYROT2\0"])
        XCTAssertEqual(CanonicalJSON.sha256(try KeyringRecord.decode(V2Fixtures.revocationJSON()).revocationSignaturePreimage()), expected["R4KEYREVOKE2\0"])
    }

    func testIndependentRecordVectorsMatchExactModelPreimageAndRecordBytes() throws {
        let fixture = try DomainVectorFixture.decode("r4_v2_domain_vectors.json")
        for vector in fixture.records {
            let source = String(decoding: vector.record, as: UTF8.self)
            switch vector.kind {
            case "frozen_genesis":
                let record = try KeyringRecord.decode(source)
                XCTAssertEqual(try record.frozenGenesisSignaturePreimage(), vector.preimage)
                XCTAssertEqual(try record.canonicalRecordBytes(), vector.record)
            case "rotation":
                let record = try KeyringRecord.decode(source)
                XCTAssertEqual(try record.rotationSignaturePreimage(), vector.preimage)
                XCTAssertEqual(try record.canonicalRecordBytes(), vector.record)
            case "revocation":
                let record = try KeyringRecord.decode(source)
                XCTAssertEqual(try record.revocationSignaturePreimage(), vector.preimage)
                XCTAssertEqual(try record.canonicalRecordBytes(), vector.record)
            case "locator":
                let record = try AdmissionLocatorV2.decode(vector.record)
                XCTAssertEqual(try record.locatorPreimage(), vector.preimage)
                XCTAssertEqual(try record.canonicalRecordBytes(), vector.record)
            default:
                let record = try JournalRecordV2.decode(vector.record)
                XCTAssertEqual(try record.journalPreimage(), vector.preimage)
                XCTAssertEqual(try record.canonicalRecordBytes(), vector.record)
            }
        }
    }

    func testReceiptRetainsSignerGenerationAndPublicDigest() throws {
        let bytes = Array(V2Fixtures.receiptJSON(generation: 2, publicKeySHA256: V2Fixtures.sha("b")).utf8)
        let receipt = try ReceiptV2.decode(String(decoding: bytes, as: UTF8.self))
        XCTAssertEqual(receipt.keyGeneration, 2)
        XCTAssertEqual(receipt.publicKeySHA256, try SHA256Digest(V2Fixtures.sha("b")))
        XCTAssertEqual(ReceiptV2.receiptFileSHA256(bytes).value, CanonicalJSON.sha256(bytes))
    }

    func testFrozenGenesisPreimageRetainsExactCanonicalBytes() throws {
        let privateKey = try Curve25519.Signing.PrivateKey(rawRepresentation: Data((0..<32).map(UInt8.init)))
        let publicKey = Array(privateKey.publicKey.rawRepresentation)
        let publicDigest = try SHA256Digest(CanonicalJSON.sha256(publicKey))
        let preimage = try Bootstrap.generation1PreimageBytes(publicKeySHA256: publicDigest)
        let signature = Array(try privateKey.signature(for: Data(preimage)))
        let bytes = try Bootstrap.generation1Bytes(publicKeySHA256: publicDigest, signature: signature)
        let record = try KeyringRecord.decode(String(decoding: bytes, as: UTF8.self))
        XCTAssertEqual(try record.frozenGenesisSignaturePreimage(), preimage)
        XCTAssertEqual(try record.frozenGenesisRecordBytes(), bytes)
        for (field, value) in [("issued_at", JSONValue.string("2026-08-12T00:00:00Z")), ("key_id", .string(V2Fixtures.sha("a"))), ("public_key_sha256", .string(V2Fixtures.sha("a")))] {
            let mutated = V2Fixtures.mutating(String(decoding: bytes, as: UTF8.self), field, value)
            XCTAssertThrowsError(try KeyringRecord.decode(mutated), field)
            XCTAssertThrowsError(try Bootstrap.verifyGeneration1(recordBytes: Array(mutated.utf8), signature: signature, publicKey: publicKey), field)
        }
    }

    func testIndependentFrozenGenesisSigningVectorMatchesBootstrapAndCryptoKit() throws {
        let vector = try DomainVectorFixture.decode("r4_v2_domain_vectors.json").frozenGenesisSigning
        let digest = try SHA256Digest(CanonicalJSON.sha256(vector.publicKey))
        XCTAssertEqual(try Bootstrap.generation1PreimageBytes(publicKeySHA256: digest), vector.preimage)
        XCTAssertEqual(try Bootstrap.generation1Bytes(publicKeySHA256: digest, signature: vector.signature), vector.record)
        let privateKey = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(vector.seed))
        XCTAssertEqual(Array(privateKey.publicKey.rawRepresentation), vector.publicKey)
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: Data(vector.publicKey))
        XCTAssertTrue(key.isValidSignature(Data(vector.signature), for: Data(vector.preimage)))
        XCTAssertTrue(key.isValidSignature(try privateKey.signature(for: Data(vector.preimage)), for: Data(vector.preimage)))
        var badSignature = vector.signature; badSignature[0] ^= 1
        var badPreimage = vector.preimage; badPreimage[0] ^= 1
        XCTAssertFalse(key.isValidSignature(Data(badSignature), for: Data(vector.preimage)))
        XCTAssertFalse(key.isValidSignature(Data(vector.signature), for: Data(badPreimage)))
    }

    func testLocatorAndJournalRequireExactDomainBoundClosedShapes() throws {
        let locator = try AdmissionLocatorV2.decode(V2Fixtures.locatorJSON())
        XCTAssertEqual(locator.registryRelativePath, "control/admissions/0123456789abcdef0123456789abcdef.json")
        XCTAssertEqual(try JournalRecordV2.decode(V2Fixtures.journalJSON(phase: "PUBLISHED")).filename, "0004-published.json")
        XCTAssertNoThrow(try JournalRecordV2.decode(V2Fixtures.journalJSON(phase: "QUARANTINED")))
        XCTAssertNoThrow(try JournalRecordV2.decode(V2Fixtures.journalJSON(phase: "PUBLISH_INDETERMINATE")))
        for field in ["registry_relative_path", "locator_sha256"] {
            XCTAssertThrowsError(try AdmissionLocatorV2.decode(V2Fixtures.mutating(V2Fixtures.locatorJSON(), field, .null)))
        }
        XCTAssertThrowsError(try JournalRecordV2.decode(V2Fixtures.adding(V2Fixtures.journalJSON(phase: "PUBLISHED"), "failure_class", .string("POST_RENAME_SEAL"))))
    }

    func testV2ModelsRejectInvalidTimestampBooleanIntegerAndCasefoldedPath() throws {
        XCTAssertThrowsError(try KeyringRecord.decode(V2Fixtures.rotationJSON(generation: 2, issuedAt: "2026-8-12T00:00:00Z")))
        XCTAssertThrowsError(try KeyringRecord.decode(V2Fixtures.rotationJSON(generation: 2, issuedAt: "2026-08-12T00:00:00+00:00")))
        XCTAssertThrowsError(try JournalRecordV2.decode(V2Fixtures.mutating(V2Fixtures.journalJSON(phase: "PUBLISHED"), "sequence", .bool(true))))
        XCTAssertThrowsError(try AdmissionLocatorV2.decode(V2Fixtures.mutating(V2Fixtures.locatorJSON(), "registry_relative_path", .string("control/admissions/0123456789ABCDEF0123456789ABCDEF.json"))))
    }

    // Catches a bootstrap graph that authenticates Key-B or accepts an approval
    // without the independently signed review and observation preimages.
    func testApprovalGraphIsClosedAndPinsKeyARoles() throws {
        let f = try V2Fixture.complete()
        let review = try Task14ReviewPackageV2.decode(f.reviewPackageJSON)
        let authorization = try Task14ObservationAuthorizationV2.decode(f.observationAuthorizationJSON)
        let approval = try Task14ApprovalV2.decode(f.approval)
        let policy = try MaterializationPolicy.decode(f.policy)
        XCTAssertNoThrow(try V2Evidence.validateApprovalGraph(review, authorization, approval, policy))
        XCTAssertEqual(try Task14ReviewPackageV2.decode(f.reviewPackageJSON).task14ApprovalVerificationPublicKeySHA256.value,
                       ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
        XCTAssertEqual(try Task14ObservationAuthorizationV2.decode(f.observationAuthorizationJSON).task14ApprovalVerificationPublicKeySHA256.value,
                       ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
        XCTAssertEqual(try Task14ApprovalV2.decode(f.approval).task14ApprovalVerificationPublicKeySHA256.value,
                       ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
        XCTAssertEqual(f.keyAPublicBytes.count, 32)
        XCTAssertEqual(f.keyBGeneration1PublicBytes.count, 32)
        XCTAssertNotEqual(f.keyAPublicBytes, f.keyBGeneration1PublicBytes)
        XCTAssertThrowsError(try Task14ApprovalV2.decode(f.replacing(f.approval, "sandbox_launcher_path", .string("/bin/sh"))))
        XCTAssertThrowsError(try V2Evidence.validateApprovalGraph(review, authorization, try Task14ApprovalV2.decode(f.replacing(f.approval, "sandbox_launcher_sha256", .string(String(repeating: "a", count: 64)))), policy))
        XCTAssertThrowsError(try ReceiptV2.decode(f.replacing(f.receipt, "state", .string("ACTIVE"))))
    }

    // Catches a digest that omits the pinned launcher, interpreter, profile, or
    // complete command-template preimage.
    func testLauncherInterpreterAndCommandDigestDomainsAreExact() throws {
        let f = try V2Fixture.complete()
        let policy = try MaterializationPolicy.decode(f.policy)
        XCTAssertEqual(policy.sandboxLauncher.descriptorIdentitySHA256.value,
                       try CanonicalJSON.domainSHA256("R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0", f.launcherDescriptorRow))
        XCTAssertEqual(policy.sandboxLauncher.codesignIdentity.certificateChainSHA256.value,
                       try CanonicalJSON.domainSHA256("R4V2_CERTIFICATE_CHAIN\0", f.launcherCertificateDERHashes))
        XCTAssertEqual(policy.sandboxLauncher.codesignIdentity.sha256.value,
                       try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", policy.sandboxLauncher.codesignIdentity.preimage))
        XCTAssertEqual(policy.sandboxProfileTemplateSHA256.value,
                       CanonicalJSON.sha256(Array("R4V2_SANDBOX_PROFILE_TEMPLATE\0".utf8) + Array(policy.sandboxProfileUTF8.utf8)))
        XCTAssertEqual(policy.childCommandTemplateSHA256.value,
                       try CanonicalJSON.domainSHA256("R4V2_CHILD_COMMAND_TEMPLATE\0", f.childCommandTemplate))
    }

    // Catches production code and test vectors drifting together: this fixture
    // is generated by a standalone Python literal-only oracle.
    func testIndependentFixedVectorsCoverEveryNewDomain() throws {
        let vectors = try DomainVectorFixture.decode("r4_v2_domain_vectors.json")
        XCTAssertEqual(Set(vectors.vectors.map(\.domain)), Set([
            "R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0", "R4V2_CERTIFICATE_CHAIN\0",
            "R4V2_CODESIGN_IDENTITY\0", "R4V2_MACHO_OBSERVATION\0",
            "R4V2_SANDBOX_PROFILE_TEMPLATE\0", "R4V2_SANDBOX_PROFILE_ACTUAL\0",
            "R4V2_CHILD_COMMAND_TEMPLATE\0", "R4V2_INSTALL_COMMAND\0",
            "R4V2_IMPORT_COMMAND\0", "R4V2_ENVIRONMENT\0", "R4V2_PUBLISHED_CLOSURE\0",
            "R4KEYROT2\0", "R4KEYREVOKE2\0", "R4ADMISSIONLOCATOR2\0", "R4JOURNAL2\0"
        ]))
        for vector in vectors.vectors {
            XCTAssertEqual(try CanonicalJSON.domainSHA256(vector.domain, vector.canonicalValue), vector.expectedSHA256.value)
        }
    }

    // Catches a repository policy that merely satisfies JSON/schema checks but
    // cannot pass the production closed decoder for Apple's launcher identity.
    func testCommittedPolicyDecodesWithPinnedAppleSandboxIdentity() throws {
        let testFile = URL(fileURLWithPath: #filePath)
        let repositoryRoot = testFile
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
        let bytes = try Data(contentsOf: repositoryRoot.appendingPathComponent("config/r4_privileged_materialization_policy.json"))
        let policy = try MaterializationPolicy.decodeDocument(Array(bytes))
        XCTAssertEqual(policy.sandboxLauncher.codesignIdentity.identifier, "com.apple.sandbox-exec")
        XCTAssertEqual(policy.sandboxLauncher.codesignIdentity.designatedRequirement, "identifier \"com.apple.sandbox-exec\" and anchor apple")
        XCTAssertNil(policy.sandboxLauncher.codesignIdentity.teamIdentifier)
    }

    // Catches hash-only evidence: all four domains must be derived from the
    // exact observed preimages, including every actual-command identity field.
    func testActualObservationAndCommandDerivationsBindRuntimeAndReceipt() throws {
        let f = try V2Fixture.complete()
        let policy = try MaterializationPolicy.decode(f.policy)
        let installer = try ActualChildCommand.decode(f.installerCommand)
        let importer = try ActualChildCommand.decode(f.importCommand)
        let machO = try MachOObservation.decode(f.machOObservation)
        let observation = try ExecutionObservation(stageRoot: "/private/r4-stage", actualProfileUTF8: f.actualProfile, environment: installer.environment, installerCommand: installer, importCommand: importer, interpreterMachOObservation: machO)
        XCTAssertEqual(try machO.sha256().value, f.interpreterMachOSHA256)
        XCTAssertEqual(try ActualSandboxProfile.sha256(f.actualProfile).value, f.actualProfileSHA256)
        XCTAssertEqual(try installer.installCommandSHA256().value, f.installerCommandSHA256)
        XCTAssertEqual(try importer.importCommandSHA256().value, f.importCommandSHA256)
        let staged = try RuntimeManifestV2.decode(f.runtime).stagedMachOObservations
        XCTAssertNoThrow(try RuntimeManifestV2.decode(f.runtime, policy: policy, actualProfileUTF8: f.actualProfile, installerCommand: installer, importCommand: importer, interpreterMachOObservation: machO, stagedMachOObservations: staged))
        XCTAssertNoThrow(try V2Evidence.crossValidate(try InputBundleManifest.decode(f.bundle), policy, try Task14ApprovalV2.decode(f.approval), try AdmissionRecord.decode(f.admission), try RuntimeManifestV2.decode(f.runtime), try AuthorityManifestV2.decode(f.authority), try ReceiptV2.decode(f.receipt), publishedTree: try PublishedAuthorityTree.decode(f.publishedRows), observation: observation))
        XCTAssertThrowsError(try RuntimeManifestV2.decode(f.runtime, policy: policy, actualProfileUTF8: f.actualProfile + " ", installerCommand: installer, importCommand: importer, interpreterMachOObservation: machO, stagedMachOObservations: staged))
    }

    // Catches the former public interpreter-only overload: an independently
    // observed wheel extension must be supplied to the actual-observation
    // decoder, not silently omitted from its dependency closure.
    func testActualObservationDecoderRejectsPartialStagedClosure() throws {
        let f = try V2Fixture.complete()
        let policy = try MaterializationPolicy.decode(f.policy)
        let installer = try ActualChildCommand.decode(f.installerCommand)
        let importer = try ActualChildCommand.decode(f.importCommand)
        let machO = try MachOObservation.decode(f.machOObservation)
        let base = try RuntimeManifestV2.decode(f.runtime)
        let extensionMachO = MachOObservation(path: "runtime/closure/python311/lib/wheel-extension.dylib", slices: machO.slices, dependencies: .array([]))
        let extensionObservation = try StagedMachOObservation(path: extensionMachO.path,
                                                               descriptorIdentitySHA256: SHA256Digest(String(repeating: "a", count: 64)),
                                                               sha256: SHA256Digest(String(repeating: "b", count: 64)),
                                                               codesignIdentitySHA256: SHA256Digest(String(repeating: "c", count: 64)),
                                                               machOObservation: extensionMachO)
        let complete = (base.stagedMachOObservations + [extensionObservation]).sorted { $0.path < $1.path }
        let completeValue = JSONValue.array(complete.map(\.canonicalValue))
        let runtime = f.replacing(f.replacing(f.runtime, "staged_mach_o_observations", completeValue),
                                  "staged_mach_o_observations_sha256",
                                  .string(try StagedMachOObservation.collectionSHA256(complete).value))
        XCTAssertNoThrow(try RuntimeManifestV2.decode(runtime, policy: policy, actualProfileUTF8: f.actualProfile,
                                                       installerCommand: installer, importCommand: importer,
                                                       interpreterMachOObservation: machO, stagedMachOObservations: complete))
        XCTAssertThrowsError(try RuntimeManifestV2.decode(runtime, policy: policy, actualProfileUTF8: f.actualProfile,
                                                           installerCommand: installer, importCommand: importer,
                                                           interpreterMachOObservation: machO,
                                                           stagedMachOObservations: base.stagedMachOObservations))
    }

    func testCrossValidationRejectsCoherentMaliciousActualObservation() throws {
        let f = try V2Fixture.complete()
        let bundle = try InputBundleManifest.decode(f.bundle), policy = try MaterializationPolicy.decode(f.policy), approval = try Task14ApprovalV2.decode(f.approval), admission = try AdmissionRecord.decode(f.admission), runtime = try RuntimeManifestV2.decode(f.runtime), authority = try AuthorityManifestV2.decode(f.authority), receipt = try ReceiptV2.decode(f.receipt), tree = try PublishedAuthorityTree.decode(f.publishedRows)
        let original = try f.observation()
        func check(_ observation: ExecutionObservation) {
            XCTAssertThrowsError(try V2Evidence.crossValidate(bundle, policy, approval, admission, runtime, authority, receipt, publishedTree: tree, observation: observation))
        }
        let badArgv = ActualChildCommand(program: original.installerCommand.program, argv: original.installerCommand.argv + ["--evil"], cwd: original.installerCommand.cwd, environment: original.installerCommand.environment, stdin: original.installerCommand.stdin, stdoutStderr: original.installerCommand.stdoutStderr, timeoutSeconds: original.installerCommand.timeoutSeconds, maxOutputBytes: original.installerCommand.maxOutputBytes, sandboxLauncherDescriptorIdentitySHA256: original.installerCommand.sandboxLauncherDescriptorIdentitySHA256, sandboxLauncherSHA256: original.installerCommand.sandboxLauncherSHA256, sandboxLauncherCodesignIdentitySHA256: original.installerCommand.sandboxLauncherCodesignIdentitySHA256, interpreterSHA256: original.installerCommand.interpreterSHA256, interpreterMachOSHA256: original.installerCommand.interpreterMachOSHA256, interpreterCodesignIdentitySHA256: original.installerCommand.interpreterCodesignIdentitySHA256, sandboxProfileSHA256: original.installerCommand.sandboxProfileSHA256)
        check(try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8, environment: original.environment, installerCommand: badArgv, importCommand: original.importCommand, interpreterMachOObservation: original.interpreterMachOObservation))
        check(try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8 + " ", environment: original.environment, installerCommand: original.installerCommand, importCommand: original.importCommand, interpreterMachOObservation: original.interpreterMachOObservation))
        check(try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8, environment: original.environment + ["EVIL=1"], installerCommand: original.installerCommand, importCommand: original.importCommand, interpreterMachOObservation: original.interpreterMachOObservation))
        let badIdentity = ActualChildCommand(program: original.installerCommand.program, argv: original.installerCommand.argv, cwd: original.installerCommand.cwd, environment: original.installerCommand.environment, stdin: original.installerCommand.stdin, stdoutStderr: original.installerCommand.stdoutStderr, timeoutSeconds: original.installerCommand.timeoutSeconds, maxOutputBytes: original.installerCommand.maxOutputBytes, sandboxLauncherDescriptorIdentitySHA256: original.installerCommand.sandboxLauncherDescriptorIdentitySHA256, sandboxLauncherSHA256: original.installerCommand.sandboxLauncherSHA256, sandboxLauncherCodesignIdentitySHA256: try SHA256Digest(String(repeating: "d", count: 64)), interpreterSHA256: original.installerCommand.interpreterSHA256, interpreterMachOSHA256: original.installerCommand.interpreterMachOSHA256, interpreterCodesignIdentitySHA256: original.installerCommand.interpreterCodesignIdentitySHA256, sandboxProfileSHA256: original.installerCommand.sandboxProfileSHA256)
        check(try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8, environment: original.environment, installerCommand: badIdentity, importCommand: original.importCommand, interpreterMachOObservation: original.interpreterMachOObservation))
        let wrongProfile = try SHA256Digest(String(repeating: "e", count: 64))
        let badInstallerProfile = ActualChildCommand(program: original.installerCommand.program, argv: original.installerCommand.argv, cwd: original.installerCommand.cwd, environment: original.installerCommand.environment, stdin: original.installerCommand.stdin, stdoutStderr: original.installerCommand.stdoutStderr, timeoutSeconds: original.installerCommand.timeoutSeconds, maxOutputBytes: original.installerCommand.maxOutputBytes, sandboxLauncherDescriptorIdentitySHA256: original.installerCommand.sandboxLauncherDescriptorIdentitySHA256, sandboxLauncherSHA256: original.installerCommand.sandboxLauncherSHA256, sandboxLauncherCodesignIdentitySHA256: original.installerCommand.sandboxLauncherCodesignIdentitySHA256, interpreterSHA256: original.installerCommand.interpreterSHA256, interpreterMachOSHA256: original.installerCommand.interpreterMachOSHA256, interpreterCodesignIdentitySHA256: original.installerCommand.interpreterCodesignIdentitySHA256, sandboxProfileSHA256: wrongProfile)
        check(try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8, environment: original.environment, installerCommand: badInstallerProfile, importCommand: original.importCommand, interpreterMachOObservation: original.interpreterMachOObservation))
        let badImportProfile = ActualChildCommand(program: original.importCommand.program, argv: original.importCommand.argv, cwd: original.importCommand.cwd, environment: original.importCommand.environment, stdin: original.importCommand.stdin, stdoutStderr: original.importCommand.stdoutStderr, timeoutSeconds: original.importCommand.timeoutSeconds, maxOutputBytes: original.importCommand.maxOutputBytes, sandboxLauncherDescriptorIdentitySHA256: original.importCommand.sandboxLauncherDescriptorIdentitySHA256, sandboxLauncherSHA256: original.importCommand.sandboxLauncherSHA256, sandboxLauncherCodesignIdentitySHA256: original.importCommand.sandboxLauncherCodesignIdentitySHA256, interpreterSHA256: original.importCommand.interpreterSHA256, interpreterMachOSHA256: original.importCommand.interpreterMachOSHA256, interpreterCodesignIdentitySHA256: original.importCommand.interpreterCodesignIdentitySHA256, sandboxProfileSHA256: wrongProfile)
        check(try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8, environment: original.environment, installerCommand: original.installerCommand, importCommand: badImportProfile, interpreterMachOObservation: original.interpreterMachOObservation))
    }

    func testCrossValidationRejectsMachODependencyEvidenceMismatchDespiteUpdatedAggregateHash() throws {
        let f = try V2Fixture.complete()
        let changed = try f.mismatchedMachOGraph()
        let original = try f.observation()
        let observation = try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8, environment: original.environment, installerCommand: original.installerCommand, importCommand: original.importCommand, interpreterMachOObservation: MachOObservation.decode(changed.observation))
        XCTAssertThrowsError(try V2Evidence.crossValidate(try InputBundleManifest.decode(f.bundle), try MaterializationPolicy.decode(f.policy), try Task14ApprovalV2.decode(f.approval), try AdmissionRecord.decode(f.admission), try RuntimeManifestV2.decode(changed.runtime), try AuthorityManifestV2.decode(changed.authority), try ReceiptV2.decode(changed.receipt), publishedTree: try PublishedAuthorityTree.decode(f.publishedRows), observation: observation))
    }

    // Catches the old interpreter-only graph: an extension/dylib can retain the
    // interpreter tuple while changing its own signed bytes or dependency row.
    func testCrossValidationRejectsExtensionObservationDrift() throws {
        let f = try V2Fixture.complete()
        let original = try f.observation()
        let extensionObservation = try StagedMachOObservation(path: "runtime/closure/python311/lib/extension.dylib",
                                                               descriptorIdentitySHA256: SHA256Digest(String(repeating: "a", count: 64)),
                                                               sha256: SHA256Digest(String(repeating: "b", count: 64)),
                                                               codesignIdentitySHA256: SHA256Digest(String(repeating: "c", count: 64)),
                                                               machOObservation: MachOObservation(path: "runtime/closure/python311/lib/extension.dylib", slices: original.interpreterMachOObservation.slices, dependencies: .array([])))
        let expanded = try ExecutionObservation(stageRoot: original.stageRoot, actualProfileUTF8: original.actualProfileUTF8,
                                                environment: original.environment, installerCommand: original.installerCommand,
                                                importCommand: original.importCommand,
                                                interpreterMachOObservation: original.interpreterMachOObservation,
                                                stagedMachOObservations: original.stagedMachOObservations + [extensionObservation])
        XCTAssertThrowsError(try V2Evidence.crossValidate(try InputBundleManifest.decode(f.bundle), try MaterializationPolicy.decode(f.policy), try Task14ApprovalV2.decode(f.approval), try AdmissionRecord.decode(f.admission), try RuntimeManifestV2.decode(f.runtime), try AuthorityManifestV2.decode(f.authority), try ReceiptV2.decode(f.receipt), publishedTree: try PublishedAuthorityTree.decode(f.publishedRows), observation: expanded))
    }

    func testDomainVectorFixtureRejectsUnknownDomainItself() throws {
        let unknown = JSONValue.object([("kind", .string("R4_V2_DOMAIN_VECTORS_V1")), ("schema_version", .integer(1)), ("vectors", .array([.object([("canonical_value", .array([])), ("domain", .string("R4V2_UNKNOWN\\0")), ("expected_sha256", .string(String(repeating: "a", count: 64)))])]))])
        XCTAssertThrowsError(try DomainVectorFixture.decode(bytes: CanonicalJSON.encode(unknown)))
    }

    func testV1LockRetainsExactArtifactIdentityAndRejectsUnknownFields() throws {
        let fixture = try V2Fixture.complete()
        let lock = try ProtectedPythonLock.decode(fixture.lock)
        XCTAssertEqual(lock.python.filename, "python.tar.gz")
        XCTAssertEqual(lock.python.version, "3.11.15")
        XCTAssertEqual(lock.python.url, "https://example.invalid/python.tar.gz")
        XCTAssertEqual(lock.wheels.map(\.filename), ["wheel.whl"])
        XCTAssertEqual(lock.wheels.map(\.url), ["https://example.invalid/wheel.whl"])
        let invalid = try String(decoding: CanonicalJSON.encode(.object([
            ("kind", .string("R4_PROTECTED_PYTHON311_LOCK_V1")),
            ("python", .object([("filename", .string("python.tar.gz")), ("sha256", .string(String(repeating: "a", count: 64))), ("url", .string("https://example.invalid/python.tar.gz")), ("version", .string("3.11.15"))])),
            ("unexpected", .bool(true)),
            ("wheels", .array([]))
        ])), as: UTF8.self)
        XCTAssertThrowsError(try ProtectedPythonLock.decode(invalid))
    }

    func testTransportAllowsOnlyV1DerivedEmptyWheelVersionAndBindsItIntoTheWheelSet() throws {
        let fixture = try V2Fixture.complete()
        let emptyWheel = fixture.replacingArtifact("version", .string(""))
        let decoded = try InputBundleManifest.decode(emptyWheel)
        XCTAssertEqual(decoded.wheelVersions, [""])
        XCTAssertNotEqual(decoded.wheelSetSHA256.value, fixture.wheelSetSHA256)
        XCTAssertThrowsError(try InputBundleManifest.decode(fixture.replacingArchiveArtifact("version", .string(""))))
    }

    func testEachV2DecoderRejectsItsOwnV1Kind() throws {
        let fixture = try V2Fixture.complete()
        XCTAssertThrowsError(try InputBundleManifest.decode(fixture.rekinded(fixture.bundle, "R4_RUNTIME_INPUT_BUNDLE_V1")))
        XCTAssertThrowsError(try MaterializationPolicy.decode(fixture.rekinded(fixture.policy, "R4_PRIVILEGED_MATERIALIZATION_POLICY_V1")))
        XCTAssertThrowsError(try AdmissionRecord.decode(fixture.rekinded(fixture.admission, "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V1")))
        XCTAssertThrowsError(try RuntimeManifestV2.decode(fixture.rekinded(fixture.runtime, "R4_HERMETIC_PYTHON311_RUNTIME_V1")))
        XCTAssertThrowsError(try AuthorityManifestV2.decode(fixture.rekinded(fixture.authority, "R4_PROTECTED_EXECUTION_MANIFEST_V1")))
        XCTAssertThrowsError(try ReceiptV2.decode(fixture.rekinded(fixture.receipt, "R4_PROTECTED_EXECUTION_RECEIPT_V1")))
        XCTAssertThrowsError(try Task14ApprovalV2.decode(fixture.rekinded(fixture.approval, "R4_TASK14_PROTECTED_EXECUTION_APPROVAL_V1")))
        XCTAssertThrowsError(try Task14ApprovalV2.decode(fixture.mixedApproval))
    }

    func testV2FixturesRoundTripAndCrossHash() throws {
        let f = try V2Fixture.complete()
        let bundle = try InputBundleManifest.decode(f.bundle)
        let policy = try MaterializationPolicy.decode(f.policy)
        let approval = try Task14ApprovalV2.decode(f.approval)
        let admission = try AdmissionRecord.decode(f.admission)
        let runtime = try RuntimeManifestV2.decode(f.runtime)
        let authority = try AuthorityManifestV2.decode(f.authority)
        let receipt = try ReceiptV2.decode(f.receipt)
        XCTAssertEqual(bundle.pythonArchiveFilename, "python.tar.gz")
        XCTAssertEqual(bundle.sha256, approval.inputBundleManifestSHA256)
        XCTAssertEqual(bundle.sha256, admission.inputBundleManifestSHA256)
        XCTAssertEqual(bundle.sha256, runtime.materializationInputSHA256)
        XCTAssertEqual(runtime.sha256, authority.runtimeManifestSHA256)
        XCTAssertEqual(authority.sha256, receipt.manifestSHA256)
        XCTAssertEqual(try SHA256Digest(CanonicalJSON.sha256(Array(f.admission.utf8))), receipt.admissionRecordSHA256)
        XCTAssertEqual(policy.sha256, runtime.materializationPolicySHA256)
        XCTAssertEqual(runtime.entriesSHA256, runtime.runtimeOutputClosureSHA256)
        XCTAssertEqual(runtime.dependencyClosureSHA256, receipt.dependencyClosureSHA256)
        XCTAssertNoThrow(try RuntimeManifestV2.decode(f.runtime, policy: policy))
        XCTAssertNoThrow(try V2Evidence.crossValidate(bundle, policy, approval, admission, runtime, authority, receipt, publishedTree: try PublishedAuthorityTree.decode(f.publishedRows), observation: try f.observation()))
    }

    func testNormativeDomainDigestsMatchIndependentFixedVectors() throws {
        let vectors = [
            ("R4DIR\0", #"[{"mode":"0444","name":"a","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":1,"type":"regular"}]"#, "77368436d5962f2b4deef2daaed169b5e31314e4fc92e00ff23861a28141fa9f"),
            ("R4V2_WHEEL_SET\0", #"[{"filename":"a.whl","sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","size":2,"url":"https://x.invalid/a.whl","version":"1"}]"#, "621cf0f27c9098c3fcb0bcf023309b49a249ea3d3e3dee093834dc6f995d5324"),
            ("R4V2_RUNTIME_ENTRIES\0", #"[{"mode":"0555","path":"runtime","sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","size":0,"type":"directory"}]"#, "b58338d213975a178e291ecda5e1c89d88e44681827434998f6711bb5372b59a"),
            ("R4V2_RUNTIME_DEPENDENCIES\0", #"[{"dependency":"/usr/lib/libx.dylib","loader":"runtime/x","resolved":"/usr/lib/libx.dylib"}]"#, "e30eb64a3bfebc48b59a2b0469c4e75356f23bbb4f6005d4f1b25db4d44cdc61"),
            ("R4V2_INSTALL_COMMAND\0", #"{"argv":["-I"],"cwd":"runtime","environment":["LANG=C"],"max_output_bytes":1,"program":"<STAGE>/python","sandbox_profile_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","stdin":"/dev/null","stdout_stderr":"ROOT_OWNED_CAPPED_PIPES","timeout_seconds":1}"#, "a6eed9003bfaeaf34914e01e12f1b01a1089b78095a828142e76e16419fe54d7"),
            ("R4V2_ENVIRONMENT\0", #"["HOME=<STAGE>/home","LANG=C"]"#, "5b2533f93e2f7f9bf4b4b94aaf029ea3c7021c88f747c1b62ea135516116f459"),
            ("R4V2_REQUIRED_BLOBS\0", #"[{"blob":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","path":"x","sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}]"#, "785c740e9be346b1e666b634642e72ba46d212b6eccedb889afb920cc99d7d79"),
            ("R4V2_PUBLISHED_CLOSURE\0", #"[{"mode":"0444","path":"checkout/x","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","size":1,"type":"regular"}]"#, "d1d10c5e365a9fbb9adab805b65d429d9b9c3986b4057cb4d049d88758de82db")
        ]
        for (domain, canonicalBytes, expected) in vectors {
            let value = try CanonicalJSON.decode(bytes: Array(canonicalBytes.utf8))
            XCTAssertEqual(try CanonicalJSON.domainSHA256(domain, value), expected, domain)
        }
    }

    func testV2DecodersRejectClosedNegativeShapes() throws {
        let f = try V2Fixture.complete()
        XCTAssertThrowsError(try InputBundleManifest.decode(f.bundle + "\n"))
        XCTAssertThrowsError(try InputBundleManifest.decode(f.adding(f.bundle, "unexpected", .integer(1))))
        XCTAssertThrowsError(try InputBundleManifest.decode(f.replacing(f.bundle, "lock_sha256", .string(String(repeating: "A", count: 64)))))
        XCTAssertThrowsError(try InputBundleManifest.decode(f.mutatingArray(f.bundle, "artifacts") { Array($0.reversed()) }))
        XCTAssertThrowsError(try AuthorityManifestV2.decode(f.mutatingArray(f.authority, "entries") { $0 + [V2Fixture.entry("checkout/A", "regular", "0444", 1, String(repeating: "a", count: 64))] }))
        XCTAssertThrowsError(try RuntimeManifestV2.decode(f.replacing(f.runtime, "entries_sha256", .string(String(repeating: "a", count: 64)))))
    }

    func testInputBundleRejectsArtifactComponentAliasesAndPathMismatches() throws {
        let f = try V2Fixture.complete()
        for filename in [".", "..", "", "wheel/name", "wheel\\name"] {
            XCTAssertThrowsError(try InputBundleManifest.decode(f.replacingArtifact("filename", .string(filename))))
        }
        for path in ["artifacts/.", "artifacts/..", "artifacts/", "artifacts/wheel/name", "artifacts/wheel\\name", "artifacts/not-the-filename"] {
            XCTAssertThrowsError(try InputBundleManifest.decode(f.replacingArtifact("path", .string(path))))
        }
    }

    func testPolicyRejectsEmptyAndNonASCIISandboxProfileAfterRedigest() throws {
        let f = try V2Fixture.complete()
        for profile in ["", "deny \u{03C0}"] {
            XCTAssertThrowsError(try MaterializationPolicy.decode(f.withSandboxProfile(profile))) { error in
                XCTAssertEqual(error as? EvidenceError, .invalidValue)
            }
        }
    }

    func testCrossValidationRejectsIndependentGraphMutations() throws {
        let f = try V2Fixture.complete()
        let b = try InputBundleManifest.decode(f.bundle), p = try MaterializationPolicy.decode(f.policy), a = try Task14ApprovalV2.decode(f.approval), d = try AdmissionRecord.decode(f.admission), r = try RuntimeManifestV2.decode(f.runtime), m = try AuthorityManifestV2.decode(f.authority), receipt = try ReceiptV2.decode(f.receipt)
        let tree = try PublishedAuthorityTree.decode(f.publishedRows)
        XCTAssertThrowsError(try V2Evidence.crossValidate(b, p, a, try AdmissionRecord.decode(f.replacing(f.admission, "task14_approval_sha256", .string(String(repeating: "d", count: 64)))), r, m, receipt, publishedTree: tree, observation: try f.observation()))
        XCTAssertThrowsError(try V2Evidence.crossValidate(b, p, try Task14ApprovalV2.decode(f.replacing(f.approval, "python_archive_sha256", .string(String(repeating: "d", count: 64)))), d, r, m, receipt, publishedTree: tree, observation: try f.observation()))
        XCTAssertThrowsError(try V2Evidence.crossValidate(b, p, a, d, try RuntimeManifestV2.decode(f.replacing(f.runtime, "lock_sha256", .string(String(repeating: "d", count: 64)))), m, receipt, publishedTree: tree, observation: try f.observation()))
        XCTAssertThrowsError(try V2Evidence.crossValidate(b, p, a, d, r, m, try ReceiptV2.decode(f.replacing(f.receipt, "helper_team_id", .string("BAD"))), publishedTree: tree, observation: try f.observation()))
        XCTAssertThrowsError(try PublishedAuthorityTree.decode(.array([])))
        XCTAssertThrowsError(try PublishedAuthorityTree.decode(.array([V2Fixture.entry("receipt.json", "regular", "0444", 1, String(repeating: "a", count: 64))])))
    }

    func testPublishedClosureExcludesOnlyTheTwoExactRootReceiptRows() throws {
        let nested = [
            V2Fixture.entry("checkout", "directory", "0555", 0, String(repeating: "a", count: 64)),
            V2Fixture.entry("checkout/receipt.json", "regular", "0444", 1, String(repeating: "b", count: 64)),
            V2Fixture.entry("runtime", "directory", "0555", 0, String(repeating: "c", count: 64)),
            V2Fixture.entry("runtime/receipt.ed25519", "regular", "0444", 64, String(repeating: "d", count: 64)),
        ]
        let tree = try PublishedAuthorityTree.decode(.array(nested))
        XCTAssertEqual(tree.rows.map(\.path), nested.compactMap { value in
            guard case .object(let fields) = value,
                  case .string(let path) = fields.first(where: { $0.0 == "path" })?.1 else { return nil }
            return path
        })
        XCTAssertEqual(try tree.sha256, try PublishedAuthorityTree.decode(.array(nested)).sha256)
    }

    func testCrossValidationRejectsPublishedClosureThatDiffersFromApprovedCheckout() throws {
        let f = try V2Fixture.complete()
        let rows = try PublishedAuthorityTree.decode(f.publishedRows)
        let changed = try PublishedAuthorityTree.decode(.array(rows.rows.map { row in
            row.path == "checkout/a"
                ? V2Fixture.entry(row.path, row.type, row.mode, row.size, String(repeating: "d", count: 64))
                : V2Fixture.entry(row.path, row.type, row.mode, row.size, row.sha256.value)
        }))
        let receipt = try ReceiptV2.decode(f.replacing(f.receipt, "published_closure_sha256", .string(try changed.sha256)))
        XCTAssertThrowsError(try V2Evidence.crossValidate(try InputBundleManifest.decode(f.bundle), try MaterializationPolicy.decode(f.policy), try Task14ApprovalV2.decode(f.approval), try AdmissionRecord.decode(f.admission), try RuntimeManifestV2.decode(f.runtime), try AuthorityManifestV2.decode(f.authority), receipt, publishedTree: changed, observation: try f.observation()))
    }

    // Catches a receipt that names a different observed launcher/interpreter
    // identity while retaining a valid self-digest.
    func testCrossValidationBindsEveryRuntimeAndReceiptObservation() throws {
        let f = try V2Fixture.complete()
        let bundle = try InputBundleManifest.decode(f.bundle), policy = try MaterializationPolicy.decode(f.policy), approval = try Task14ApprovalV2.decode(f.approval), admission = try AdmissionRecord.decode(f.admission), runtime = try RuntimeManifestV2.decode(f.runtime), authority = try AuthorityManifestV2.decode(f.authority), receipt = try ReceiptV2.decode(f.receipt), tree = try PublishedAuthorityTree.decode(f.publishedRows)
        XCTAssertNoThrow(try V2Evidence.crossValidate(bundle, policy, approval, admission, runtime, authority, receipt, publishedTree: tree, observation: try f.observation()))
        let changed = String(repeating: "d", count: 64)
        for field in ["sandbox_launcher_codesign_identity_sha256", "interpreter_pre_install_sha256", "interpreter_pre_install_codesign_identity_sha256", "interpreter_post_install_codesign_identity_sha256"] {
            let mutatedRuntime = try RuntimeManifestV2.decode(f.replacing(f.runtime, field, .string(changed)))
            XCTAssertThrowsError(try V2Evidence.crossValidate(bundle, policy, approval, admission, mutatedRuntime, authority, receipt, publishedTree: tree, observation: try f.observation()), field)
            let mutatedReceipt = try ReceiptV2.decode(f.replacing(f.receipt, field, .string(changed)))
            XCTAssertThrowsError(try V2Evidence.crossValidate(bundle, policy, approval, admission, runtime, authority, mutatedReceipt, publishedTree: tree, observation: try f.observation()), field)
        }
    }
}

struct V2Fixture {
    let bundle: String; let lock: String; let policy: String; let reviewPackageJSON: String; let observationAuthorizationJSON: String; let approval: String; let admission: String; let runtime: String; let authority: String; let receipt: String
    let directorySHA256: String; let wheelSetSHA256: String; let runtimeEntriesSHA256: String; let dependencyClosureSHA256: String; let installerCommandSHA256: String; let importCommandSHA256: String; let environmentSHA256: String; let requiredBlobSetSHA256: String; let publishedClosureSHA256: String; let interpreterMachOSHA256: String; let actualProfileSHA256: String
    let immediateChildRows: JSONValue; let wheelRows: JSONValue; let runtimeEntries: JSONValue; let dependencies: JSONValue; let installerCommand: JSONValue; let importCommand: JSONValue; let environment: JSONValue; let requiredBlobs: JSONValue; let publishedRows: JSONValue; let launcherDescriptorRow: JSONValue; let launcherCertificateDERHashes: JSONValue; let childCommandTemplate: JSONValue; let machOObservation: JSONValue; let actualProfile: String; let keyAPublicBytes: [UInt8]; let keyBGeneration1PublicBytes: [UInt8]

    static func complete() throws -> V2Fixture {
        let h = { (s: String) in CanonicalJSON.sha256(Array(s.utf8)) }
        let namespace = "org.mlb-walkforward.r4.protected-execution-authority/v1"
        let authorityID = "r4-disjoint-custody-v2-execution-v1"
        let blobRows: JSONValue = .array([])
        let requiredBlobSet = try CanonicalJSON.domainSHA256("R4V2_REQUIRED_BLOBS\0", blobRows)
        let immediateRows: JSONValue = .array([.object([("name", .string("a")), ("type", .string("regular")), ("mode", .string("0444")), ("size", .integer(1)), ("sha256", .string(h("a")))])])
        let directory = try CanonicalJSON.domainSHA256("R4DIR\0", immediateRows)
        let checkoutRows: JSONValue = .array([entry("checkout", "directory", "0555", 0, directory), entry("checkout/a", "regular", "0444", 1, h("a"))])
        let wheelRows: JSONValue = .array([.object([("filename", .string("wheel.whl")), ("version", .string("1.0")), ("url", .string("https://example.invalid/wheel.whl")), ("sha256", .string(h("wheel"))), ("size", .integer(5))])])
        let wheelSet = try CanonicalJSON.domainSHA256("R4V2_WHEEL_SET\0", wheelRows)
        let artifacts: JSONValue = .array([
            .object([("path", .string("artifacts/python.tar.gz")), ("kind", .string("python_archive")), ("filename", .string("python.tar.gz")), ("version", .string("3.11.15")), ("url", .string("https://example.invalid/python.tar.gz")), ("sha256", .string(h("python"))), ("size", .integer(6))]),
            .object([("path", .string("artifacts/wheel.whl")), ("kind", .string("wheel")), ("filename", .string("wheel.whl")), ("version", .string("1.0")), ("url", .string("https://example.invalid/wheel.whl")), ("sha256", .string(h("wheel"))), ("size", .integer(5))])
        ])
        let lock = String(decoding: try CanonicalJSON.encode(.object([("kind", .string("R4_PROTECTED_PYTHON311_LOCK_V1")), ("python", .object([("version", .string("3.11.15")), ("url", .string("https://example.invalid/python.tar.gz")), ("filename", .string("python.tar.gz")), ("sha256", .string(h("python")))])), ("wheels", .array([.object([("filename", .string("wheel.whl")), ("url", .string("https://example.invalid/wheel.whl")), ("sha256", .string(h("wheel")))])]))])), as: UTF8.self)
        let lockSHA = h("lock")
        let environment: JSONValue = .array(["HOME=__R4_STAGE__/home", "LANG=C", "LC_ALL=C", "PATH=/usr/bin:/bin", "PIP_CONFIG_FILE=/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK=1", "PIP_NO_INPUT=1", "PYTHONNOUSERSITE=1", "TMPDIR=__R4_STAGE__/tmp"].map(JSONValue.string))
        let profile = "(version 1)\n(deny default)\n(deny network*)\n(allow file-read* (subpath \"__R4_STAGE__/runtime/closure/python311\") (subpath \"__R4_STAGE__/wheelhouse\") (subpath \"__R4_STAGE__/home\") (subpath \"__R4_STAGE__/tmp\") (literal \"/dev/null\") (subpath \"/System\") (subpath \"/usr/lib\"))\n(allow file-write* (subpath \"__R4_STAGE__/runtime\") (subpath \"__R4_STAGE__/home\") (subpath \"__R4_STAGE__/tmp\"))"
        let probe = "import importlib,json,sys\nmods=(\"cryptography\",\"jsonschema\",\"numpy\",\"scipy\")\nprint(json.dumps({\"version\":sys.version.split()[0],\"paths\":sys.path,\"files\":[importlib.import_module(name).__file__ for name in mods]},sort_keys=True,separators=(\",\",\":\")))"
        let pip: JSONValue = .array(["-I", "-s", "-E", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check", "__R4_STAGE__/wheelhouse/wheel.whl"].map(JSONValue.string))
        let importArgv: JSONValue = .array(["-I", "-s", "-E", "-c", probe].map(JSONValue.string))
        let certificateHashes: JSONValue = .array([.string(h("certificate-der"))])
        let chainSHA = try CanonicalJSON.domainSHA256("R4V2_CERTIFICATE_CHAIN\0", certificateHashes)
        let launcherIdentityPreimage: JSONValue = .object([("identifier", .string("com.apple.sandbox-exec")), ("designated_requirement", .string("identifier \"com.apple.sandbox-exec\" and anchor apple")), ("team_identifier", .null), ("certificate_chain_sha256", .string(chainSHA))])
        let launcherIdentity: JSONValue = .object([("identifier", .string("com.apple.sandbox-exec")), ("designated_requirement", .string("identifier \"com.apple.sandbox-exec\" and anchor apple")), ("team_identifier", .null), ("certificate_chain_sha256", .string(chainSHA)), ("sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", launcherIdentityPreimage)))])
        let interpreterIdentityPreimage: JSONValue = .object([("identifier", .string("org.example.python311")), ("designated_requirement", .string("identifier \"org.example.python311\"")), ("team_identifier", .string("EXAMPLE")), ("certificate_chain_sha256", .string(chainSHA))])
        let interpreterIdentity: JSONValue = .object([("identifier", .string("org.example.python311")), ("designated_requirement", .string("identifier \"org.example.python311\"")), ("team_identifier", .string("EXAMPLE")), ("certificate_chain_sha256", .string(chainSHA)), ("sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage)))])
        let launcherDescriptor: JSONValue = .object([("path", .string("/usr/bin/sandbox-exec")), ("st_dev", .integer(1)), ("st_ino", .integer(2)), ("st_gen", .integer(3)), ("mode", .integer(493)), ("nlink", .integer(1)), ("size", .integer(4))])
        let launcherDescriptorSHA = try CanonicalJSON.domainSHA256("R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0", launcherDescriptor)
        let launcher: JSONValue = .object([("path", .string("/usr/bin/sandbox-exec")), ("descriptor_identity_sha256", .string(launcherDescriptorSHA)), ("sha256", .string(h("launcher"))), ("codesign_identity", launcherIdentity)])
        let dependencies: JSONValue = .array([.object([("loader", .string("runtime/closure/python311/bin/python3.11")), ("dependency", .string("/usr/lib/libSystem.B.dylib")), ("resolved", .string("/usr/lib/libSystem.B.dylib"))])])
        let machOObservation: JSONValue = .object([("path", .string("runtime/closure/python311/bin/python3.11")), ("slices", .array([.object([("cpu_type", .integer(1)), ("cpu_subtype", .integer(2)), ("file_type", .integer(2)), ("uuid", .string("0123456789abcdef0123456789abcdef")), ("code_signature_offset", .integer(64)), ("code_signature_size", .integer(128))])])), ("dependencies", dependencies)])
        let machOSHA = try CanonicalJSON.domainSHA256("R4V2_MACHO_OBSERVATION\0", machOObservation)
        let interpreter: JSONValue = .object([("path", .string("runtime/closure/python311/bin/python3.11")), ("sha256", .string(h("entrypoint"))), ("mach_o_sha256", .string(machOSHA)), ("codesign_identity", interpreterIdentity)])
        let profileTemplateSHA = CanonicalJSON.sha256(Array("R4V2_SANDBOX_PROFILE_TEMPLATE\0".utf8) + Array(profile.utf8))
        let pipStrings = try stringsForFixture(pip); let importStrings = try stringsForFixture(importArgv)
        let commandTemplate: JSONValue = .object([("launcher_path", .string("/usr/bin/sandbox-exec")), ("pip_argv_template", .array((["/usr/bin/sandbox-exec", "-p", profile, "__R4_STAGE__/runtime/closure/python311/bin/python3.11"] + pipStrings).map(JSONValue.string))), ("import_argv_template", .array((["/usr/bin/sandbox-exec", "-p", profile, "__R4_STAGE__/runtime/closure/python311/bin/python3.11"] + importStrings).map(JSONValue.string))), ("cwd", .string("runtime")), ("environment", environment), ("stdin", .string("/dev/null")), ("stdout_stderr", .string("ROOT_OWNED_CAPPED_PIPES")), ("timeout_seconds", .integer(300)), ("max_output_bytes", .integer(1_000_000)), ("sandbox_profile_template_sha256", .string(profileTemplateSHA))])
        let child: JSONValue = .object([("cwd", .string("runtime")), ("environment", environment), ("pip_argv", pip), ("import_argv", importArgv), ("timeout_seconds", .integer(300)), ("max_output_bytes", .integer(1_000_000)), ("sandbox_profile_utf8", .string(profile)), ("sandbox_profile_template_sha256", .string(profileTemplateSHA)), ("child_command_template_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CHILD_COMMAND_TEMPLATE\0", commandTemplate))), ("read_allow", .array(["runtime/closure/python311", "wheelhouse", "home", "tmp", "/dev/null", "/System", "/usr/lib"].map(JSONValue.string))), ("write_allow", .array(["runtime", "home", "tmp"].map(JSONValue.string))), ("network_denied", .bool(true))])
        let policy = try sealed([("kind", .string("R4_PRIVILEGED_MATERIALIZATION_POLICY_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("python_version", .string("3.11.15")), ("archive", .object([("archive_root", .string("python")), ("final_runtime_path", .string("runtime/closure/python311")), ("entrypoint", .string("runtime/closure/python311/bin/python3.11")), ("max_members", .integer(100000)), ("max_unpacked_bytes", .integer(8_589_934_592))])), ("wheel_filenames", .array([.string("wheel.whl")])), ("sandbox_launcher", launcher), ("base_interpreter", interpreter), ("child_execution", child), ("allowed_loader_roots", .array([.string("runtime/closure/python311"), .string("/System/"), .string("/usr/lib/")])), ("import_modules", .array(["cryptography", "jsonschema", "numpy", "scipy"].map(JSONValue.string))), ("import_probe_sha256", .string(h(probe))), ("schema_versions", .array(["R4_RUNTIME_INPUT_BUNDLE_V2", "R4_PRIVILEGED_MATERIALIZATION_POLICY_V2", "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2", "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2", "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2", "R4_HERMETIC_PYTHON311_RUNTIME_V2", "R4_PROTECTED_EXECUTION_MANIFEST_V2", "R4_PROTECTED_EXECUTION_RECEIPT_V2"].map(JSONValue.string)))])
        let policySHA = try digest(policy)
        let bundle = try sealed([("kind", .string("R4_RUNTIME_INPUT_BUNDLE_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("lock_path", .string("r4_protected_python311.lock.json")), ("lock_sha256", .string(lockSHA)), ("materialization_policy_path", .string("materialization-policy.json")), ("git", .object([("head", .string(String(repeating: "a", count: 40))), ("tree", .string(String(repeating: "b", count: 40))), ("required_blobs", blobRows)])), ("artifacts", artifacts), ("checkout_entries", checkoutRows), ("materialization_policy_sha256", .string(policySHA))])
        let bundleSHA = try digest(bundle)
        let helpers: JSONValue = .array([.object([("path", .string("bootstrap/provisioner")), ("sha256", .string(h("binary")))]), .object([("path", .string("bootstrap/provisioner.codesign-requirement.txt")), ("sha256", .string(h("requirement")))])])
        let keyA = ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256
        let reviewPackage = try sealed([("kind", .string("R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("reviewed_head", .string(String(repeating: "a", count: 40))), ("reviewed_tree", .string(String(repeating: "b", count: 40))), ("required_blob_set_sha256", .string(requiredBlobSet)), ("exact_suite_log_sha256", .string(h("suite"))), ("input_bundle_manifest_sha256", .string(bundleSHA)), ("lock_sha256", .string(lockSHA)), ("python_archive_sha256", .string(h("python"))), ("wheel_set_sha256", .string(wheelSet)), ("materialization_policy_sha256", .string(policySHA)), ("provisioner_swift_source_sha256", .string(h("source"))), ("provisioner_binary_sha256", .string(h("binary"))), ("provisioner_designated_requirement_sha256", .string(h("requirement"))), ("helper_hashes", helpers), ("helper_team_id", .string("3BMF4LM6TM")), ("helper_signing_identity_sha1", .string("4C6503D0A78A9F51C86CC7C9683115E74A4902A7")), ("task14_approval_verification_public_key_sha256", .string(keyA))])
        let reviewSHA = try digest(reviewPackage)
        let authorization = try sealed([("kind", .string("R4_TASK14_PRIVILEGED_MATERIALIZATION_OBSERVATION_AUTHORIZATION_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("review_package_sha256", .string(reviewSHA)), ("input_bundle_manifest_sha256", .string(bundleSHA)), ("materialization_policy_sha256", .string(policySHA)), ("provisioner_binary_sha256", .string(h("binary"))), ("provisioner_designated_requirement_sha256", .string(h("requirement"))), ("task14_approval_verification_public_key_sha256", .string(keyA))])
        let authorizationSHA = try digest(authorization)
        let approval = try sealed([("kind", .string("R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("reviewed_head", .string(String(repeating: "a", count: 40))), ("reviewed_tree", .string(String(repeating: "b", count: 40))), ("required_blob_set_sha256", .string(requiredBlobSet)), ("exact_suite_log_sha256", .string(h("suite"))), ("review_package_sha256", .string(reviewSHA)), ("observation_authorization_sha256", .string(authorizationSHA)), ("task14_approval_verification_public_key_sha256", .string(keyA)), ("input_bundle_manifest_sha256", .string(bundleSHA)), ("lock_sha256", .string(lockSHA)), ("python_archive_sha256", .string(h("python"))), ("wheel_set_sha256", .string(wheelSet)), ("materialization_policy_sha256", .string(policySHA)), ("sandbox_launcher_path", .string("/usr/bin/sandbox-exec")), ("sandbox_launcher_descriptor_identity_sha256", .string(launcherDescriptorSHA)), ("sandbox_launcher_sha256", .string(h("launcher"))), ("sandbox_launcher_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", launcherIdentityPreimage))), ("sandbox_profile_template_sha256", .string(profileTemplateSHA)), ("child_command_template_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CHILD_COMMAND_TEMPLATE\0", commandTemplate))), ("base_interpreter_path", .string("runtime/closure/python311/bin/python3.11")), ("base_interpreter_sha256", .string(h("entrypoint"))), ("base_interpreter_mach_o_sha256", .string(machOSHA)), ("base_interpreter_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("provisioner_swift_source_sha256", .string(h("source"))), ("provisioner_binary_sha256", .string(h("binary"))), ("provisioner_designated_requirement_sha256", .string(h("requirement"))), ("helper_hashes", helpers), ("helper_team_id", .string("3BMF4LM6TM")), ("helper_signing_identity_sha1", .string("4C6503D0A78A9F51C86CC7C9683115E74A4902A7"))])
        let approvalSHA = try digest(approval)
        let admission = try sealed([("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("attempt_uuid", .string(String(repeating: "c", count: 32))), ("task14_approval_sha256", .string(approvalSHA)), ("input_bundle_manifest_sha256", .string(bundleSHA)), ("materialization_policy_sha256", .string(policySHA)), ("storage_volume_uuid", .string("123e4567-e89b-12d3-a456-426614174000"))])
        let admissionFileSHA = CanonicalJSON.sha256(Array(admission.utf8))
        let entrypointSHA = h("entrypoint")
        let binSHA = try directorySHA("python3.11", "regular", "0555", 1, entrypointSHA)
        let pythonSHA = try directorySHA("bin", "directory", "0555", 0, binSHA)
        let closureSHA = try directorySHA("python311", "directory", "0555", 0, pythonSHA)
        let runtimeSHA = try directorySHA("closure", "directory", "0555", 0, closureSHA)
        let runtimeEntries: JSONValue = .array([entry("runtime", "directory", "0555", 0, runtimeSHA), entry("runtime/closure", "directory", "0555", 0, closureSHA), entry("runtime/closure/python311", "directory", "0555", 0, pythonSHA), entry("runtime/closure/python311/bin", "directory", "0555", 0, binSHA), entry("runtime/closure/python311/bin/python3.11", "regular", "0555", 1, entrypointSHA)])
        let runtimeEntriesSHA = try CanonicalJSON.domainSHA256("R4V2_RUNTIME_ENTRIES\0", runtimeEntries)
        let dependenciesSHA = try CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", dependencies)
        let stagedMachOObservations: JSONValue = .array([.object([("path", .string("runtime/closure/python311/bin/python3.11")), ("descriptor_identity_sha256", .string(h("entrypoint"))), ("sha256", .string(h("entrypoint"))), ("codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("mach_o_observation", machOObservation), ("dependency_closure_sha256", .string(dependenciesSHA))])])
        let stagedMachOObservationsSHA = try CanonicalJSON.domainSHA256("R4V2_STAGED_MACHO_OBSERVATIONS\0", stagedMachOObservations)
        let stage = "/private/r4-stage"; let actualProfile = profile.replacingOccurrences(of: "__R4_STAGE__", with: stage)
        let actualEnvironment: JSONValue = .array(try stringsForFixture(environment).map { .string($0.replacingOccurrences(of: "__R4_STAGE__", with: stage)) })
        let actualEnvironmentSHA = try CanonicalJSON.domainSHA256("R4V2_ENVIRONMENT\0", actualEnvironment)
        let actualProfileSHA = CanonicalJSON.sha256(Array("R4V2_SANDBOX_PROFILE_ACTUAL\0".utf8) + Array(actualProfile.utf8))
        let actualPipArgv = pipStrings.map { $0.replacingOccurrences(of: "__R4_STAGE__", with: stage) }
        let actualImportArgv = importStrings.map { $0.replacingOccurrences(of: "__R4_STAGE__", with: stage) }
        func actualCommand(_ pythonArgv: [String]) -> JSONValue { .object([("program", .string("/usr/bin/sandbox-exec")), ("argv", .array((["/usr/bin/sandbox-exec", "-p", actualProfile, stage + "/runtime/closure/python311/bin/python3.11"] + pythonArgv).map(JSONValue.string))), ("cwd", .string("runtime")), ("environment", actualEnvironment), ("stdin", .string("/dev/null")), ("stdout_stderr", .string("ROOT_OWNED_CAPPED_PIPES")), ("timeout_seconds", .integer(300)), ("max_output_bytes", .integer(1_000_000)), ("sandbox_launcher_descriptor_identity_sha256", .string(launcherDescriptorSHA)), ("sandbox_launcher_sha256", .string(h("launcher"))), ("sandbox_launcher_codesign_identity_sha256", .string(try! CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", launcherIdentityPreimage))), ("interpreter_sha256", .string(h("entrypoint"))), ("interpreter_mach_o_sha256", .string(machOSHA)), ("interpreter_codesign_identity_sha256", .string(try! CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("sandbox_profile_sha256", .string(actualProfileSHA))]) }
        let command = actualCommand(actualPipArgv); let importCommand = actualCommand(actualImportArgv)
        let commandSHA = try CanonicalJSON.domainSHA256("R4V2_INSTALL_COMMAND\0", command)
        let importCommandSHA = try CanonicalJSON.domainSHA256("R4V2_IMPORT_COMMAND\0", importCommand)
        let runtime = try sealed([("kind", .string("R4_HERMETIC_PYTHON311_RUNTIME_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("python_version", .string("3.11.15")), ("entrypoint", .string("runtime/closure/python311/bin/python3.11")), ("lock_sha256", .string(h("lock"))), ("materialization_input_sha256", .string(bundleSHA)), ("materialization_policy_sha256", .string(policySHA)), ("wheel_set_sha256", .string(wheelSet)), ("sandbox_launcher_path", .string("/usr/bin/sandbox-exec")), ("sandbox_launcher_descriptor_identity_sha256", .string(launcherDescriptorSHA)), ("sandbox_launcher_sha256", .string(h("launcher"))), ("sandbox_launcher_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", launcherIdentityPreimage))), ("sandbox_profile_template_sha256", .string(profileTemplateSHA)), ("child_command_template_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CHILD_COMMAND_TEMPLATE\0", commandTemplate))), ("installer_command_sha256", .string(commandSHA)), ("import_command_sha256", .string(importCommandSHA)), ("interpreter_pre_install_sha256", .string(h("entrypoint"))), ("interpreter_pre_install_mach_o_sha256", .string(machOSHA)), ("interpreter_pre_install_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("interpreter_post_install_sha256", .string(h("entrypoint"))), ("interpreter_post_install_mach_o_sha256", .string(machOSHA)), ("interpreter_post_install_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("import_probe_sha256", .string(h(probe))), ("sandbox_profile_sha256", .string(actualProfileSHA)), ("environment_sha256", .string(actualEnvironmentSHA)), ("working_directory", .string("runtime")), ("entries", runtimeEntries), ("entries_sha256", .string(runtimeEntriesSHA)), ("runtime_output_closure_sha256", .string(runtimeEntriesSHA)), ("mach_o_dependencies", dependencies), ("dependency_closure_sha256", .string(dependenciesSHA)), ("pre_install_staged_mach_o_observations", stagedMachOObservations), ("pre_install_staged_mach_o_observations_sha256", .string(stagedMachOObservationsSHA)), ("staged_mach_o_observations", stagedMachOObservations), ("staged_mach_o_observations_sha256", .string(stagedMachOObservationsSHA))])
        let runtimeManifestSHA = try digest(runtime)
        let authority = try sealed([("kind", .string("R4_PROTECTED_EXECUTION_MANIFEST_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("git", .object([("head", .string(String(repeating: "a", count: 40))), ("tree", .string(String(repeating: "b", count: 40))), ("required_blobs", blobRows)])), ("entries", checkoutRows), ("runtime_manifest_sha256", .string(runtimeManifestSHA)), ("input_bundle_manifest_sha256", .string(bundleSHA)), ("materialization_policy_sha256", .string(policySHA))])
        let authoritySHA = try digest(authority)
        // Task 9 signs the complete prospective non-receipt closure, not just the approved
        // checkout.  The receipt pair itself is absent to avoid a hash cycle.
        let publishedRows: JSONValue = .array([
            entry("admission-locator-v2.json", "regular", "0444", 1, h("locator")),
            entry("admission.json", "regular", "0444", 1, h("admission-record")),
            entry("checkout", "directory", "0555", 0, directory),
            entry("checkout/a", "regular", "0444", 1, h("a")),
            entry("manifest-v2.json", "regular", "0444", 1, h("authority-manifest")),
            entry("runtime", "directory", "0555", 0, runtimeSHA),
            entry("runtime-manifest-v2.json", "regular", "0444", 1, h("runtime-manifest")),
            entry("runtime/closure", "directory", "0555", 0, closureSHA),
            entry("runtime/closure/python311", "directory", "0555", 0, pythonSHA),
            entry("runtime/closure/python311/bin", "directory", "0555", 0, binSHA),
            entry("runtime/closure/python311/bin/python3.11", "regular", "0555", 1, entrypointSHA),
        ])
        let publishedSHA = try CanonicalJSON.domainSHA256("R4V2_PUBLISHED_CLOSURE\0", publishedRows)
        let receipt = try sealed([("kind", .string("R4_PROTECTED_EXECUTION_RECEIPT_V2")), ("namespace", .string(namespace)), ("authority_id", .string(authorityID)), ("state", .string("SEALED")), ("key_generation", .integer(1)), ("manifest_sha256", .string(authoritySHA)), ("runtime_manifest_sha256", .string(runtimeManifestSHA)), ("task14_approval_sha256", .string(approvalSHA)), ("input_bundle_manifest_sha256", .string(bundleSHA)), ("lock_sha256", .string(h("lock"))), ("materialization_policy_sha256", .string(policySHA)), ("wheel_set_sha256", .string(wheelSet)), ("runtime_output_closure_sha256", .string(runtimeEntriesSHA)), ("dependency_closure_sha256", .string(dependenciesSHA)), ("pre_install_staged_mach_o_observations_sha256", .string(stagedMachOObservationsSHA)), ("staged_mach_o_observations_sha256", .string(stagedMachOObservationsSHA)), ("admission_record_sha256", .string(admissionFileSHA)), ("git_head", .string(String(repeating: "a", count: 40))), ("git_tree", .string(String(repeating: "b", count: 40))), ("required_blob_set_sha256", .string(requiredBlobSet)), ("provisioner_binary_sha256", .string(h("binary"))), ("helper_codesign_requirement_sha256", .string(h("requirement"))), ("helper_team_id", .string("3BMF4LM6TM")), ("helper_signing_identity_sha1", .string("4C6503D0A78A9F51C86CC7C9683115E74A4902A7")), ("public_key_sha256", .string(h("public"))), ("published_closure_sha256", .string(publishedSHA)), ("sandbox_launcher_path", .string("/usr/bin/sandbox-exec")), ("sandbox_launcher_descriptor_identity_sha256", .string(launcherDescriptorSHA)), ("sandbox_launcher_sha256", .string(h("launcher"))), ("sandbox_launcher_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", launcherIdentityPreimage))), ("sandbox_profile_template_sha256", .string(profileTemplateSHA)), ("sandbox_profile_sha256", .string(actualProfileSHA)), ("child_command_template_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CHILD_COMMAND_TEMPLATE\0", commandTemplate))), ("installer_command_sha256", .string(commandSHA)), ("import_command_sha256", .string(importCommandSHA)), ("interpreter_pre_install_sha256", .string(h("entrypoint"))), ("interpreter_pre_install_mach_o_sha256", .string(machOSHA)), ("interpreter_pre_install_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("interpreter_post_install_sha256", .string(h("entrypoint"))), ("interpreter_post_install_mach_o_sha256", .string(machOSHA)), ("interpreter_post_install_codesign_identity_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", interpreterIdentityPreimage))), ("environment_sha256", .string(actualEnvironmentSHA))])
        let keyABytes = hexBytes("409181363d1c2e79a28e34d1b4382c59debeefdd7d542361f982d6779a7b91d3")
        let keyBBytes = hexBytes("a15ce84d0937f26bb5c01e74d8f30a916e4ac27f9db0e5634817cf2098ab6d42")
        return V2Fixture(bundle: bundle, lock: lock, policy: policy, reviewPackageJSON: reviewPackage, observationAuthorizationJSON: authorization, approval: approval, admission: admission, runtime: runtime, authority: authority, receipt: receipt, directorySHA256: directory, wheelSetSHA256: wheelSet, runtimeEntriesSHA256: runtimeEntriesSHA, dependencyClosureSHA256: dependenciesSHA, installerCommandSHA256: commandSHA, importCommandSHA256: importCommandSHA, environmentSHA256: actualEnvironmentSHA, requiredBlobSetSHA256: requiredBlobSet, publishedClosureSHA256: publishedSHA, interpreterMachOSHA256: machOSHA, actualProfileSHA256: actualProfileSHA, immediateChildRows: immediateRows, wheelRows: wheelRows, runtimeEntries: runtimeEntries, dependencies: dependencies, installerCommand: command, importCommand: importCommand, environment: actualEnvironment, requiredBlobs: blobRows, publishedRows: publishedRows, launcherDescriptorRow: launcherDescriptor, launcherCertificateDERHashes: certificateHashes, childCommandTemplate: commandTemplate, machOObservation: machOObservation, actualProfile: actualProfile, keyAPublicBytes: keyABytes, keyBGeneration1PublicBytes: keyBBytes)
    }

    func rekinded(_ source: String, _ kind: String) -> String { try! V2Fixture.sealed(V2Fixture.replacingKind(source, kind)) }
    func adding(_ source: String, _ key: String, _ value: JSONValue) -> String { try! V2Fixture.sealed(V2Fixture.unsealed(source) + [(key, value)]) }
    func replacing(_ source: String, _ key: String, _ value: JSONValue) -> String { try! V2Fixture.sealed(V2Fixture.replacing(V2Fixture.unsealed(source), key, value)) }
    func mutatingArray(_ source: String, _ key: String, _ transform: ([JSONValue]) -> [JSONValue]) -> String { try! V2Fixture.sealed(V2Fixture.unsealed(source).map { pair in guard pair.0 == key, case .array(let values) = pair.1 else { return pair }; return (key, .array(transform(values))) }) }
    func replacingArtifact(_ key: String, _ value: JSONValue) -> String {
        mutatingArray(bundle, "artifacts") { rows in rows.enumerated().map { index, row in
            guard index == 1, case .object(let pairs) = row else { return row }
            return .object(V2Fixture.replacing(pairs, key, value))
        } }
    }
    func replacingArchiveArtifact(_ key: String, _ value: JSONValue) -> String {
        mutatingArray(bundle, "artifacts") { rows in rows.enumerated().map { index, row in
            guard index == 0, case .object(let pairs) = row else { return row }
            return .object(V2Fixture.replacing(pairs, key, value))
        } }
    }
    func withSandboxProfile(_ profile: String) -> String {
        let child = V2Fixture.unsealed(policy).first { $0.0 == "child_execution" }!.1
        guard case .object(let childPairs) = child else { fatalError() }
        let updatedChild: JSONValue = .object(V2Fixture.replacing(V2Fixture.replacing(childPairs, "sandbox_profile_utf8", .string(profile)), "sandbox_profile_template_sha256", .string(CanonicalJSON.sha256(Array("R4V2_SANDBOX_PROFILE_TEMPLATE\0".utf8) + Array(profile.utf8)))))
        return try! V2Fixture.sealed(V2Fixture.replacing(V2Fixture.unsealed(policy), "child_execution", updatedChild))
    }
    func mismatchedMachOGraph() throws -> (runtime: String, authority: String, receipt: String, observation: JSONValue) {
        let observedDependencies: JSONValue = .array([.object([("loader", .string("runtime/closure/python311/bin/python3.11")), ("dependency", .string("/usr/lib/libchanged.dylib")), ("resolved", .string("/usr/lib/libchanged.dylib"))])])
        guard case .object(let observationPairs) = machOObservation else { throw CanonicalJSONError.invalidJSON }
        let observation = JSONValue.object(V2Fixture.replacing(observationPairs, "dependencies", observedDependencies))
        let machO = try CanonicalJSON.domainSHA256("R4V2_MACHO_OBSERVATION\0", observation)
        var runtimePairs = V2Fixture.unsealed(runtime)
        runtimePairs = V2Fixture.replacing(runtimePairs, "interpreter_pre_install_mach_o_sha256", .string(machO))
        runtimePairs = V2Fixture.replacing(runtimePairs, "interpreter_post_install_mach_o_sha256", .string(machO))
        let runtime = try V2Fixture.sealed(runtimePairs)
        var authorityPairs = V2Fixture.unsealed(authority)
        authorityPairs = V2Fixture.replacing(authorityPairs, "runtime_manifest_sha256", .string(try V2Fixture.digest(runtime)))
        let authority = try V2Fixture.sealed(authorityPairs)
        var receiptPairs = V2Fixture.unsealed(receipt)
        receiptPairs = V2Fixture.replacing(receiptPairs, "manifest_sha256", .string(try V2Fixture.digest(authority)))
        receiptPairs = V2Fixture.replacing(receiptPairs, "runtime_manifest_sha256", .string(try V2Fixture.digest(runtime)))
        receiptPairs = V2Fixture.replacing(receiptPairs, "interpreter_pre_install_mach_o_sha256", .string(machO))
        receiptPairs = V2Fixture.replacing(receiptPairs, "interpreter_post_install_mach_o_sha256", .string(machO))
        return (runtime, authority, try V2Fixture.sealed(receiptPairs), observation)
    }
    func observation() throws -> ExecutionObservation {
        let installer = try ActualChildCommand.decode(installerCommand)
        return try ExecutionObservation(stageRoot: "/private/r4-stage", actualProfileUTF8: actualProfile, environment: installer.environment, installerCommand: installer, importCommand: ActualChildCommand.decode(importCommand), interpreterMachOObservation: MachOObservation.decode(machOObservation))
    }
    var mixedApproval: String { try! V2Fixture.sealed(V2Fixture.unsealed(approval) + [("runtime_manifest_sha256", .string(String(repeating: "a", count: 64)))]) }
    static func entry(_ path: String, _ type: String, _ mode: String, _ size: Int64, _ sha: String) -> JSONValue { .object([("path", .string(path)), ("type", .string(type)), ("mode", .string(mode)), ("size", .integer(size)), ("sha256", .string(sha))]) }
    private static func hexBytes(_ hex: String) -> [UInt8] { stride(from: 0, to: hex.count, by: 2).map { UInt8(hex[hex.index(hex.startIndex, offsetBy: $0)..<hex.index(hex.startIndex, offsetBy: $0 + 2)], radix: 16)! } }
    private static func stringsForFixture(_ value: JSONValue) throws -> [String] { guard case .array(let values) = value else { throw CanonicalJSONError.invalidJSON }; return try values.map { guard case .string(let text) = $0 else { throw CanonicalJSONError.invalidJSON }; return text } }
    private static func directorySHA(_ name: String, _ type: String, _ mode: String, _ size: Int64, _ sha: String) throws -> String { try CanonicalJSON.domainSHA256("R4DIR\0", .array([.object([("name", .string(name)), ("type", .string(type)), ("mode", .string(mode)), ("size", .integer(size)), ("sha256", .string(sha))])])) }
    private static func digest(_ source: String) throws -> String { guard case .object(let pairs) = try CanonicalJSON.decode(bytes: Array(source.utf8)), case .string(let digest)? = pairs.first(where: { $0.0 == "sha256" })?.1 else { throw CanonicalJSONError.invalidJSON }; return digest }
    private static func sealed(_ pairs: [(String, JSONValue)]) throws -> String { let bare = JSONValue.object(pairs); let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(bare)); return String(bytes: try CanonicalJSON.encode(.object(pairs + [("sha256", .string(digest))])), encoding: .utf8)! }
    private static func unsealed(_ source: String) -> [(String, JSONValue)] { guard case .object(let pairs) = try! CanonicalJSON.decode(bytes: Array(source.utf8)) else { fatalError() }; return pairs.filter { $0.0 != "sha256" } }
    private static func replacingKind(_ source: String, _ kind: String) -> [(String, JSONValue)] { replacing(unsealed(source), "kind", .string(kind)) }
    private static func replacing(_ pairs: [(String, JSONValue)], _ key: String, _ value: JSONValue) -> [(String, JSONValue)] { pairs.map { $0.0 == key ? (key, value) : $0 } }
}

enum V2Fixtures {
    static let namespace = ProtectedExecutionConstants.namespace
    static let authorityID = ProtectedExecutionConstants.authorityID
    static let attemptUUID = "0123456789abcdef0123456789abcdef"

    static func sha(_ text: String) -> String { String(repeating: text, count: 64 / text.count) }

    static func rotationRecord(generation: Int64) throws -> KeyringRecord {
        try KeyringRecord.decode(rotationJSON(generation: generation))
    }

    static func rotationJSON(generation: Int64, issuedAt: String = "2026-08-12T00:00:00Z") -> String {
        sealed([
            ("authority_id", .string(authorityID)), ("detached_signature_sha256", .string(sha("a"))),
            ("generation", .integer(generation)), ("issued_at", .string(issuedAt)),
            ("kind", .string("R4_PROTECTED_EXECUTION_KEY_ROTATION_V2")), ("namespace", .string(namespace)),
            ("new_public_key_sha256", .string(sha("c"))), ("previous_generation", .integer(generation - 1)),
            ("previous_public_key_sha256", .string(sha("b"))), ("previous_record_sha256", .string(sha("d")))
        ])
    }

    static func revocationJSON() -> String {
        sealed([
            ("authority_id", .string(authorityID)), ("detached_signature_sha256", .string(sha("a"))),
            ("kind", .string("R4_PROTECTED_EXECUTION_REVOCATION_V2")), ("namespace", .string(namespace)),
            ("previous_revocation_sha256", .null), ("reason", .string("KEY_COMPROMISE")),
            ("revoked_at", .string("2026-08-12T00:00:00Z")), ("sequence", .integer(1)),
            ("signer_generation", .integer(2)), ("signer_key_record_sha256", .string(sha("d"))),
            ("signer_public_key_sha256", .string(sha("c"))), ("state", .string("REVOKED")),
            ("target_generation", .integer(2)), ("target_key_record_sha256", .string(sha("d"))),
            ("target_public_key_sha256", .string(sha("c")))
        ])
    }

    static func receiptJSON(generation: Int64, publicKeySHA256: String) -> String {
        let fixture = try! V2Fixture.complete()
        return fixture.replacing(fixture.replacing(fixture.receipt, "key_generation", .integer(generation)), "public_key_sha256", .string(publicKeySHA256))
    }

    static func locatorJSON() -> String {
        let core: [(String, JSONValue)] = [
            ("admission_record_sha256", .string(sha("a"))), ("attempt_uuid", .string(attemptUUID)),
            ("authority_id", .string(authorityID)), ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2")),
            ("namespace", .string(namespace)), ("registry_relative_path", .string("control/admissions/\(attemptUUID).json"))
        ]
        let locator = CanonicalJSON.sha256(Array("R4ADMISSIONLOCATOR2\0".utf8) + (try! CanonicalJSON.encode(.object(core))))
        return sealed(core + [("locator_sha256", .string(locator))])
    }

    static func journalJSON(phase: String) -> String {
        let core: [(String, JSONValue)] = [
            ("admission_record_sha256", .string(sha("a"))), ("attempt_uuid", .string(attemptUUID)),
            ("authority_id", .string(authorityID)), ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2")),
            ("namespace", .string(namespace)), ("phase", .string(phase)),
            ("sequence", .integer(phase == "STAGE_CREATED" ? 1 : phase == "PRESEALED" ? 2 : phase == "RENAME_INTENT" ? 3 : 4))
        ]
        let extras: [(String, JSONValue)]
        switch phase {
        case "STAGE_CREATED": extras = [("stage_name", .string(attemptUUID))]
        case "PRESEALED", "RENAME_INTENT": extras = [("published_closure_sha256", .string(sha("b"))), ("receipt_file_sha256", .string(sha("c")))]
        case "PUBLISHED": extras = [("published_closure_sha256", .string(sha("b"))), ("receipt_file_sha256", .string(sha("c"))), ("published_device", .integer(1)), ("published_inode", .integer(2))]
        case "QUARANTINED": extras = [("failure_class", .string("PRE_RENAME_SEAL")), ("quarantine_name", .string(attemptUUID))]
        default: extras = [("failure_class", .string("POST_RENAME_SEAL")), ("published_closure_sha256", .string(sha("b"))), ("receipt_file_sha256", .string(sha("c")))]
        }
        let journal = CanonicalJSON.sha256(Array("R4JOURNAL2\0".utf8) + (try! CanonicalJSON.encode(.object(core))))
        return sealed(core + extras + [("journal_sha256", .string(journal))])
    }

    static func mutating(_ source: String, _ field: String, _ value: JSONValue) -> String {
        guard case .object(let pairs) = try! CanonicalJSON.decode(bytes: Array(source.utf8)) else { fatalError() }
        return sealed(pairs.filter { $0.0 != "sha256" }.map { $0.0 == field ? (field, value) : $0 })
    }


    static func adding(_ source: String, _ field: String, _ value: JSONValue) -> String {
        guard case .object(let pairs) = try! CanonicalJSON.decode(bytes: Array(source.utf8)) else { fatalError() }
        return sealed(pairs.filter { $0.0 != "sha256" } + [(field, value)])
    }

    private static func sealed(_ fields: [(String, JSONValue)]) -> String {
        let digest = CanonicalJSON.sha256(try! CanonicalJSON.encode(.object(fields)))
        return String(decoding: try! CanonicalJSON.encode(.object(fields + [("sha256", .string(digest))])), as: UTF8.self)
    }
}
