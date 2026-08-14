import Darwin
import CryptoKit
import Foundation
import XCTest
@testable import R4ProtectedExecutionProvisioner

final class BootstrapTests: XCTestCase {
    func testProductionAdapterRejectsNonRootBeforeAnyProductionOpen() throws {
        guard geteuid() != 0 else { throw XCTSkip("non-root construction contract") }
        XCTAssertThrowsError(try DarwinBootstrapAdapters.makeProduction()) { error in
            XCTAssertEqual(error as? BootstrapError, .rootRequired)
        }
    }

    func testTemporaryEntrypointIsFixedFailClosedContract() {
        XCTAssertEqual(TemporaryEntrypoint.stderr, "ROOT_PRIVILEGE_REQUIRED: CLI_NOT_AVAILABLE\n")
        XCTAssertEqual(TemporaryEntrypoint.exitCode, 2)
    }

    func testACLProbeAcceptsOnlyConfirmedAbsenceAndRejectsAmbiguityOrEntries() throws {
        XCTAssertNoThrow(try POSIX.validateACLProbe(nilErrno: ENOENT, entryResult: nil, entryErrno: nil))
        XCTAssertNoThrow(try POSIX.validateACLProbe(nilErrno: nil, entryResult: 0, entryErrno: nil))
        XCTAssertThrowsError(try POSIX.validateACLProbe(nilErrno: 0, entryResult: nil, entryErrno: nil))
        XCTAssertThrowsError(try POSIX.validateACLProbe(nilErrno: EACCES, entryResult: nil, entryErrno: nil))
        XCTAssertThrowsError(try POSIX.validateACLProbe(nilErrno: nil, entryResult: 1, entryErrno: nil))
        XCTAssertThrowsError(try POSIX.validateACLProbe(nilErrno: nil, entryResult: -1, entryErrno: 0))
    }

    func testPOSIXDisposableRetainedTraversalNofollowRebindFsyncAndSwap() throws {
        let root = URL(fileURLWithPath: "/private/tmp/r4-posix-bootstrap-\(UUID().uuidString)", isDirectory: true)
        defer {
            if root.path.hasPrefix("/private/tmp/r4-posix-bootstrap-") {
                try? FileManager.default.removeItem(at: root)
            }
        }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        let parent = try POSIX.openDirectory(root.path)
        let child = try POSIX.mkdirAt(parent, name: "child", mode: 0o700)
        XCTAssertEqual(Darwin.fchmod(child.fd, 0o700), 0)
        var childStatus = stat()
        XCTAssertEqual(Darwin.fstat(child.fd, &childStatus), 0)
        XCTAssertEqual(childStatus.st_uid, getuid())
        XCTAssertEqual(childStatus.st_mode & 0o7777, 0o700)
        // The disposable traversal verifier is explicitly parameterized by observed ownership;
        // /private/tmp may inherit a group different from the test process's primary group.
        let expectedGID = childStatus.st_gid
        // ENOENT from acl_get_fd_np is the Darwin signal that this object has no extended ACL.
        // A complete descriptor metadata check must therefore accept these fresh objects.
        try POSIX.requireDirectory(child, uid: getuid(), gid: expectedGID, mode: 0o700)
        XCTAssertThrowsError(try POSIX.requireDirectory(child, uid: getuid(), gid: expectedGID &+ 1, mode: 0o700))
        try POSIX.fsync(child)
        _ = try child.rebind(in: parent, name: "child", directory: true)

        let file = try POSIX.createExclusive(child, name: "record", mode: 0o600)
        try POSIX.writeAll(file, Array("record".utf8))
        try POSIX.fsync(file)
        try POSIX.requireRegular(file, uid: getuid(), gid: expectedGID, mode: 0o600)
        XCTAssertThrowsError(try POSIX.requireRegular(file, uid: getuid(), gid: expectedGID &+ 1, mode: 0o600))
        _ = try file.rebind(in: child, name: "record")

        XCTAssertEqual(Darwin.symlink("record", root.appendingPathComponent("child/link").path), 0)
        XCTAssertThrowsError(try POSIX.openAt(child, name: "link", flags: O_RDONLY))

        XCTAssertEqual(Darwin.unlinkat(child.fd, "record", 0), 0)
        _ = try POSIX.createExclusive(child, name: "record", mode: 0o600)
        XCTAssertThrowsError(try file.rebind(in: child, name: "record"))

        XCTAssertEqual(Darwin.fchmod(child.fd, 0o777), 0)
        XCTAssertThrowsError(try POSIX.requireDirectory(child, uid: getuid(), gid: expectedGID, mode: 0o700))
    }

    func testPOSIXAbsoluteDirectoryWalkRejectsRepeatedSlashAlias() throws {
        let root = URL(fileURLWithPath: "/private/tmp/r4-posix-alias-\(UUID().uuidString)", isDirectory: true)
        defer {
            if root.path.hasPrefix("/private/tmp/r4-posix-alias-") {
                try? FileManager.default.removeItem(at: root)
            }
        }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)

        let alias = "/private//tmp/\(root.lastPathComponent)"
        XCTAssertThrowsError(try POSIX.openDirectory(alias))
    }

    func testFakeAdapterBootstrapCreatesPinnedKeyLifecycleInOrder() throws {
        try withFixture { fixture in
            fixture.usesCryptoRNG = true
            let result = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)

            XCTAssertEqual(result.events, [.openKeyANoFollow, .hashKeyA, .compareCompiledKeyAHash, .verifyApprovalSignature, .decodeApproval, .verifyApprovalDigest, .verifyHelperIdentity, .sealApproval, .sealApprovalSignature, .sealKeyA, .sealApprovalDigest, .generateKeyBGeneration1, .sealKeyBGeneration1])
            XCTAssertEqual(fixture.written[.approval], fixture.approvalBytes)
            XCTAssertEqual(fixture.written[.approvalDigest], Array(fixture.approvalDigest.utf8))
            XCTAssertEqual(fixture.modes[.generation1PrivateKey], 0o600)
            XCTAssertEqual(fixture.modes[.generation1PublicKey], 0o444)
            XCTAssertTrue(fixture.didPublish)
            XCTAssertNil(fixture.written[.generation1PrivateKey])
        }
    }

    func testFakeAdapterBootstrapPinsHeldKeyABeforeApprovalSignatureAndThenCreatesDistinctKeyB() throws {
        try withFixture { fixture in
            fixture.usesCryptoRNG = true
            let result = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            XCTAssertEqual(result.events, [.openKeyANoFollow, .hashKeyA, .compareCompiledKeyAHash, .verifyApprovalSignature, .decodeApproval, .verifyApprovalDigest, .verifyHelperIdentity, .sealApproval, .sealApprovalSignature, .sealKeyA, .sealApprovalDigest, .generateKeyBGeneration1, .sealKeyBGeneration1])
            XCTAssertNotEqual(fixture.approvalVerificationPublicKeyBytes, fixture.written[.generation1PublicKey])
        }
    }

    func testBootstrapRejectsBadKeyABeforeSignatureOrKeyB() throws {
        try withFixture { fixture in
            var badKeyA = fixture.approvalVerificationPublicKeyBytes
            badKeyA[0] ^= 1
            XCTAssertThrowsError(try Bootstrap.installForTest(request: try fixture.request(replacingVerificationPublicKey: badKeyA), root: fixture.root.path, adapters: fixture))
            XCTAssertFalse(fixture.didVerifyApprovalSignature)
            XCTAssertEqual(fixture.keyBGenerationRequests, 0)
            XCTAssertEqual(fixture.mutationCount, 0)
        }
    }

    func testBootstrapRejectsBadSignatureBeforeDecodeOrKeyB() throws {
        try withFixture { fixture in
            fixture.acceptApprovalSignature = false
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .invalidApprovalSignature) }
            XCTAssertTrue(fixture.didVerifyApprovalSignature)
            XCTAssertEqual(fixture.keyBGenerationRequests, 0)
            XCTAssertEqual(fixture.mutationCount, 0)
        }
    }

    func testBootstrapRejectsBadDigestAfterSignatureBeforeKeyB() throws {
        try withFixture { fixture in
            XCTAssertThrowsError(try Bootstrap.installForTest(request: try fixture.request(replacingDigest: String(repeating: "0", count: 64)), root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .approvalDigestMismatch) }
            XCTAssertTrue(fixture.didVerifyApprovalSignature)
            XCTAssertEqual(fixture.keyBGenerationRequests, 0)
            XCTAssertEqual(fixture.mutationCount, 0)
        }
    }

    func testConcreteEd25519VerifierRejectsOneBitMutationsOfCanonicalApproval() throws {
        let seed = Data((0..<32).map(UInt8.init))
        let keyA = try Curve25519.Signing.PrivateKey(rawRepresentation: seed)
        let publicKey = Array(keyA.publicKey.rawRepresentation)
        let approval = try CanonicalJSON.encode(.object([("kind", .string("R4_TEST_APPROVAL")), ("version", .integer(2))]))
        let signature = Array(try keyA.signature(for: Data(approval)))

        XCTAssertTrue(DarwinBootstrapAdapters.isValidApprovalSignature(verificationPublicKey: publicKey, approval: approval, signature: signature))
        var badApproval = approval; badApproval[0] ^= 1
        var badSignature = signature; badSignature[0] ^= 1
        var badPublicKey = publicKey; badPublicKey[0] ^= 1
        XCTAssertFalse(DarwinBootstrapAdapters.isValidApprovalSignature(verificationPublicKey: publicKey, approval: badApproval, signature: signature))
        XCTAssertFalse(DarwinBootstrapAdapters.isValidApprovalSignature(verificationPublicKey: publicKey, approval: approval, signature: badSignature))
        XCTAssertFalse(DarwinBootstrapAdapters.isValidApprovalSignature(verificationPublicKey: badPublicKey, approval: approval, signature: signature))
    }

    func testTestOnlyBootstrapCapabilityWipesKeyBSeedAndSerializedPrivateKeyAfterWrite() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            XCTAssertEqual(fixture.zeroizedKeyBBufferLengths, [32, 32])
            XCTAssertEqual(fixture.keyBBufferTrace, ["zeroize-seed", "write", "zeroize-private"])
        }
    }

    func testConcreteKeyBZeroizationOverwritesExistingMutableArrayStorageInPlace() {
        var privateBytes = Array(repeating: UInt8(0xA5), count: 32)
        let before = privateBytes.withUnsafeMutableBufferPointer { $0.baseAddress }
        DarwinBootstrapAdapters.overwriteKeyBBuffer(&privateBytes)
        let after = privateBytes.withUnsafeMutableBufferPointer { $0.baseAddress }
        XCTAssertEqual(before, after)
        XCTAssertTrue(privateBytes.allSatisfy { $0 == 0 })
    }

    func testBootstrapPrecreatesMutableRegistriesWithoutSealingThem() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)

            let mutable: Set<BootstrapObject> = [.controlDirectory, .keyringDirectory,
                                                   .revocationsDirectory, .admissionsDirectory,
                                                   .journalsDirectory,
                                                   .materializationWorkDirectory, .quarantineDirectory,
                                                   .authoritiesDirectory]
            for object in mutable {
                XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(object.rawValue).path), object.rawValue)
                XCTAssertEqual(fixture.modes[object], 0o700, object.rawValue)
                XCTAssertFalse(fixture.sealed.contains(object), object.rawValue)
            }
            XCTAssertTrue(fixture.sealed.isSuperset(of: [.bootstrapDirectory, .generation1Directory, .generation1, .generation1Signature, .generation1PrivateKey, .approvalVerificationPublicKey]))
            try fixture.appendAdmissionThroughCapability()
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent("control/admissions/00000001").path))
            XCTAssertThrowsError(try fixture.attemptArbitraryRegistryMutation())
        }
    }

    func testBootstrapCreatesEmptyJournalsRegistry() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let journals = fixture.root.appendingPathComponent("control/journals")
            XCTAssertTrue(FileManager.default.fileExists(atPath: journals.path))
            XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: journals.path), [])
        }
    }

    func testOperationalVerifierRequiresContiguousCanonicalGenerations() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let root = try POSIX.openDirectory(fixture.root.path)
            XCTAssertNoThrow(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: root))

            try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("control/keyring/generation-3"), withIntermediateDirectories: false)
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))

            try FileManager.default.removeItem(at: fixture.root.appendingPathComponent("control/keyring/generation-1"))
            try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("control/keyring/generation-01"), withIntermediateDirectories: false)
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testOperationalVerifierRejectsWrongJournalSequenceAndAdmissionDirectory() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let attempt = fixture.root.appendingPathComponent("control/journals/0123456789abcdef0123456789abcdef")
            try FileManager.default.createDirectory(at: attempt, withIntermediateDirectories: true)
            try Data("bad".utf8).write(to: attempt.appendingPathComponent("0001-wrong-phase.json"))
            XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: attempt.path), ["0001-wrong-phase.json"])
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))

            try FileManager.default.removeItem(at: attempt)
            try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("control/admissions/0123456789abcdef0123456789abcdef.json"), withIntermediateDirectories: true)
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testOperationalVerifierAcceptsOnlyNormativeSparseQuarantinePrefixes() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let journals = fixture.root.appendingPathComponent("control/journals")
            let prefixes: [[String]] = [
                ["0001-stage-created.json", "0004-quarantined.json"],
                ["0001-stage-created.json", "0002-presealed.json", "0004-quarantined.json"],
                ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json", "0004-quarantined.json"]
            ]
            for (index, names) in prefixes.enumerated() {
                let attempt = journals.appendingPathComponent(String(format: "%032x", index + 1))
                try FileManager.default.createDirectory(at: attempt, withIntermediateDirectories: false)
                for name in names { try Data("journal".utf8).write(to: attempt.appendingPathComponent(name)) }
            }
            XCTAssertNoThrow(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
            let foreign = journals.appendingPathComponent("0000000000000000000000000000000f")
            try FileManager.default.createDirectory(at: foreign, withIntermediateDirectories: false)
            for name in ["0001-stage-created.json", "0004-published.json"] { try Data("journal".utf8).write(to: foreign.appendingPathComponent(name)) }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testOperationalVerifierAcceptsOnlyExactRevocationRegistryName() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let revocations = fixture.root.appendingPathComponent("control/revocations")
            try FileManager.default.createDirectory(at: revocations.appendingPathComponent("revocation-00000000000000000001"), withIntermediateDirectories: false)
            XCTAssertNoThrow(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
            try FileManager.default.removeItem(at: revocations.appendingPathComponent("revocation-00000000000000000001"))
            try FileManager.default.createDirectory(at: revocations.appendingPathComponent("revocation-00000000000000000002"), withIntermediateDirectories: false)
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testOperationalVerifierRejectsFixedBootstrapTypeSwaps() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let policy = fixture.root.appendingPathComponent("bootstrap/policy.json")
            try FileManager.default.removeItem(at: policy)
            try FileManager.default.createDirectory(at: policy, withIntermediateDirectories: false)
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))

            try FileManager.default.removeItem(at: policy)
            try Data("replacement".utf8).write(to: policy)
            let journals = fixture.root.appendingPathComponent("control/journals")
            try FileManager.default.removeItem(at: journals)
            try Data("directory replaced by regular".utf8).write(to: journals)
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testOperationalVerifierRejectsAfterEnumerationGenerationAddition() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            var calls = 0
            DarwinBootstrapAdapters.operationalAfterFirstEnumerationHook = {
                calls += 1
                if calls == 2 {
                    try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("control/keyring/generation-0"), withIntermediateDirectories: false)
                }
            }
            defer { DarwinBootstrapAdapters.operationalAfterFirstEnumerationHook = nil }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent("control/keyring/generation-0").path))
        }
    }

    func testOperationalVerifierRejectsControlSwapAfterSnapshotAndPreservesForeignSentinel() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let control = fixture.root.appendingPathComponent("control")
            let foreign = fixture.root.appendingPathComponent("control-foreign")
            DarwinBootstrapAdapters.operationalAfterFirstEnumerationHook = {
                try FileManager.default.moveItem(at: control, to: foreign)
                try FileManager.default.createDirectory(at: control, withIntermediateDirectories: false)
            }
            defer { DarwinBootstrapAdapters.operationalAfterFirstEnumerationHook = nil }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
            XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.appendingPathComponent("keyring").path))
        }
    }

    func testOperationalVerifierRejectsJournalFileAdditionAfterAttemptSnapshot() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let attempt = fixture.root.appendingPathComponent("control/journals/0123456789abcdef0123456789abcdef")
            try FileManager.default.createDirectory(at: attempt, withIntermediateDirectories: false)
            try Data("sentinel".utf8).write(to: attempt.appendingPathComponent("0001-stage-created.json"))
            var calls = 0
            DarwinBootstrapAdapters.operationalAfterFirstEnumerationHook = {
                calls += 1
                if calls == 3 { try Data("added".utf8).write(to: attempt.appendingPathComponent("0002-presealed.json")) }
            }
            defer { DarwinBootstrapAdapters.operationalAfterFirstEnumerationHook = nil }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: try POSIX.openDirectory(fixture.root.path)))
            XCTAssertEqual(try Data(contentsOf: attempt.appendingPathComponent("0001-stage-created.json")), Data("sentinel".utf8))
        }
    }

    func testBootstrapVerifierRejectsMutableRegistryAdditionAfterEmptySnapshot() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            DarwinBootstrapAdapters.bootstrapAfterEmptyEnumerationHook = { registry in
                guard registry == "journals" else { return }
                try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("control/journals/foreign"), withIntermediateDirectories: false)
            }
            defer { DarwinBootstrapAdapters.bootstrapAfterEmptyEnumerationHook = nil }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyBootstrapLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testBootstrapVerifierRejectsControlSwapAfterInitialSnapshot() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let control = fixture.root.appendingPathComponent("control")
            let foreign = fixture.root.appendingPathComponent("control-foreign")
            DarwinBootstrapAdapters.bootstrapAfterEmptyEnumerationHook = { registry in
                guard registry == "control" else { return }
                try FileManager.default.moveItem(at: control, to: foreign)
                try FileManager.default.createDirectory(at: control, withIntermediateDirectories: false)
            }
            defer { DarwinBootstrapAdapters.bootstrapAfterEmptyEnumerationHook = nil }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyBootstrapLayout(root: try POSIX.openDirectory(fixture.root.path)))
            XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.appendingPathComponent("keyring").path))
        }
    }

    func testBootstrapVerifierRejectsKeyringChildAdditionAfterInitialSnapshot() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            DarwinBootstrapAdapters.bootstrapAfterEmptyEnumerationHook = { registry in
                guard registry == "keyring" else { return }
                try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("control/keyring/generation-0"), withIntermediateDirectories: false)
            }
            defer { DarwinBootstrapAdapters.bootstrapAfterEmptyEnumerationHook = nil }
            XCTAssertThrowsError(try DarwinBootstrapAdapters.verifyBootstrapLayout(root: try POSIX.openDirectory(fixture.root.path)))
        }
    }

    func testGeneration1SignatureBindsCanonicalRecordPreimage() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let record = try XCTUnwrap(fixture.written[.generation1])
            let signature = try XCTUnwrap(fixture.written[.generation1Signature])
            let publicKey = try XCTUnwrap(fixture.written[.generation1PublicKey])
            XCTAssertNoThrow(try KeyringRecord.decode(String(bytes: record, encoding: .utf8)!))
            XCTAssertNoThrow(try Bootstrap.verifyGeneration1(recordBytes: record, signature: signature, publicKey: publicKey))

            guard case .object(let pairs) = try CanonicalJSON.decode(bytes: record) else { return XCTFail("record shape") }
            let signed = pairs.filter { $0.0 != "sha256" }.map { $0.0 == "generation" ? ("generation", JSONValue.integer(2)) : $0 }
            let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(signed)))
            let forged = try CanonicalJSON.encode(.object(signed + [("sha256", .string(digest))]))
            XCTAssertThrowsError(try Bootstrap.verifyGeneration1(recordBytes: forged, signature: signature, publicKey: publicKey))
        }
    }

    func testBootstrapRejectsV1WrongHashAndWrongHelperIdentity() throws {
        try withFixture { fixture in
            let v1 = try fixture.rekinded("R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V1")
            XCTAssertThrowsError(try Bootstrap.installForTest(request: try fixture.request(replacingApproval: Array(v1.utf8), replacingDigest: CanonicalJSON.sha256(Array(v1.utf8))), root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .invalidApproval) }
            XCTAssertEqual(fixture.mutationCount, 0)
        }
        try withFixture { fixture in
            XCTAssertThrowsError(try Bootstrap.installForTest(request: try fixture.request(replacingDigest: String(repeating: "0", count: 64)), root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .approvalDigestMismatch) }
            XCTAssertEqual(fixture.mutationCount, 0)
        }
        try withFixture { fixture in
            let mixed = try fixture.mixedV1V2Approval()
            XCTAssertThrowsError(try Bootstrap.installForTest(request: try fixture.request(replacingApproval: Array(mixed.utf8), replacingDigest: CanonicalJSON.sha256(Array(mixed.utf8))), root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .invalidApproval) }
            XCTAssertEqual(fixture.mutationCount, 0)
        }
        let cases: [(BootstrapError, (BootstrapFixture) throws -> Void)] = [
            (.helperBinaryHashMismatch, { try $0.overrideHelper(binarySHA256: String(repeating: "1", count: 64)) }),
            (.helperDesignatedRequirementMismatch, { try $0.overrideHelper(requirementSHA256: String(repeating: "2", count: 64)) }),
            (.helperTeamIDMismatch, { try $0.overrideHelper(teamID: "BADTEAM") }),
            (.helperSigningIdentityMismatch, { try $0.overrideHelper(signingIdentitySHA1: String(repeating: "3", count: 40)) }),
        ]
        for (expected, configure) in cases {
            try withFixture { fixture in
                try configure(fixture)
                XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, expected) }
                XCTAssertFalse(fixture.didPublish)
                XCTAssertGreaterThan(fixture.mutationCount, 0)
            }
        }
    }

    func testBootstrapFailsClosedForObjectAndDurabilityFailures() throws {
        for failure in [BootstrapAdapterFailure.missing, .extra, .symlink, .swapped, .fsync, .seal, .signProbe] {
            try withFixture(failure: failure) { fixture in
                XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture), "\(failure)") { error in
                    XCTAssertEqual(error as? BootstrapError, .adapter(failure))
                }
                XCTAssertFalse(fixture.didPublish)
            }
        }
    }

    func testBootstrapRejectsPreexistingRootWithoutOverwrite() throws {
        try withFixture(rootExists: true) { fixture in
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) { error in
                XCTAssertEqual(error as? BootstrapError, .rootAlreadyExists)
            }
            XCTAssertTrue(fixture.written.isEmpty)
            XCTAssertFalse(fixture.didPublish)
        }
    }

    func testPostPublishInstalledHelperRebindFailureFailsClosed() throws {
        try withFixture { fixture in
            fixture.rebindFails = true
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) { error in
                XCTAssertEqual(error as? BootstrapError, .adapter(.swapped))
            }
            XCTAssertTrue(fixture.didPublish)
            XCTAssertTrue(fixture.didQuarantine)
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.root.path))
        }
    }

    func testQuarantineFailureSurfacesBootstrapIncompleteAndLeavesResidueRejected() throws {
        try withFixture { fixture in
            fixture.rebindFails = true
            fixture.quarantineFails = true
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) {
                XCTAssertEqual($0 as? BootstrapError, .bootstrapIncomplete)
            }
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.root.path))
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) {
                XCTAssertEqual($0 as? BootstrapError, .rootAlreadyExists)
            }
        }
    }

    func testPrepublishFailureAbortsOnlyOwnedStageAndLeavesNoPrivateKey() throws {
        try withFixture(failure: .seal) { fixture in
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture))
            XCTAssertTrue(fixture.didAbort)
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.root.path))
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(BootstrapObject.generation1PrivateKey.rawValue).path))
        }
    }

    func testDurabilityRecorderOrdersWriteSealPublishAndFinalRebind() throws {
        try withFixture { fixture in
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            let trace = fixture.durabilityTrace
            func order(_ names: [String]) {
                var cursor = -1
                for name in names {
                    guard let index = trace.firstIndex(of: name) else { return XCTFail("missing \(name): \(trace)") }
                    XCTAssertGreaterThan(index, cursor, "\(name): \(trace)")
                    cursor = index
                }
            }
            order(["writeAll:control/keyring/generation-1/authority-receipt-ed25519-private-key", "fsync:control/keyring/generation-1/authority-receipt-ed25519-private-key", "fullsync:control/keyring/generation-1/authority-receipt-ed25519-private-key", "chmod:control/keyring/generation-1/authority-receipt-ed25519-private-key", "chflags:control/keyring/generation-1/authority-receipt-ed25519-private-key", "seal-fsync:control/keyring/generation-1/authority-receipt-ed25519-private-key", "pre-rename-volume", "rename-excl", "parent-fsync", "post-rename-volume", "final-rebind"])
        }
    }

    func testDurabilityRecorderFailureNeverReturnsSuccessAndTerminalizes() throws {
        for seam in ["write-fullsync", "seal-fullsync", "pre-rename-volume", "parent-fsync", "final-rebind"] {
            try withFixture { fixture in
                fixture.durabilityFailureAt = seam
                XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture), seam)
                if fixture.didPublish {
                    XCTAssertTrue(fixture.didQuarantine, seam)
                    XCTAssertTrue(fixture.durabilityTrace.contains("quarantine-parent-fsync"), seam)
                    XCTAssertTrue(fixture.durabilityTrace.contains("quarantine-volume"), seam)
                } else {
                    XCTAssertTrue(fixture.didAbort, seam)
                    XCTAssertTrue(fixture.durabilityTrace.contains("abort-parent-fsync"), seam)
                    XCTAssertTrue(fixture.durabilityTrace.contains("abort-volume"), seam)
                }
            }
        }
    }

    func testPostPublishTreeVerifierRejectsEveryRecordedIntegrityClass() throws {
        for failure in [BootstrapAdapterFailure.missing, .extra, .symlink, .swapped, .acl, .aclAPI, .seal] {
            try withFixture { fixture in
                fixture.postPublishFailure = failure
                XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture), "\(failure)") { error in
                    XCTAssertEqual(error as? BootstrapError, .adapter(failure))
                }
                XCTAssertTrue(fixture.didPublish)
            }
        }
    }

    func testExternalBootstrapSourceSwapAfterHeldCopyCannotChangeAssessment() throws {
        try withFixture { fixture in
            fixture.swapSourceAfterCopy = true
            _ = try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)
            XCTAssertEqual(try Data(contentsOf: fixture.root.appendingPathComponent(BootstrapObject.provisioner.rawValue)), Data("fixture-binary".utf8))
            XCTAssertEqual(fixture.sourceBytes, Array("attacker-replacement".utf8))
        }
    }

    func testStagedProvisionerSwapFailsBeforePublish() throws {
        try withFixture { fixture in
            fixture.swapStagedBeforeAssessment = true
            XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .adapter(.swapped)) }
            XCTAssertFalse(fixture.didPublish)
        }
    }

    func testExtendedACLOrACLAPIFailureRejectsBeforeSealAndPublish() throws {
        for failure in [BootstrapAdapterFailure.acl, .aclAPI] {
            try withFixture(failure: failure) { fixture in
                XCTAssertThrowsError(try Bootstrap.installForTest(request: fixture.request, root: fixture.root.path, adapters: fixture)) { XCTAssertEqual($0 as? BootstrapError, .adapter(failure)) }
                XCTAssertFalse(fixture.didPublish)
                XCTAssertFalse(fixture.didSeal)
            }
        }
    }
}

private func withFixture(rootExists: Bool = false, failure: BootstrapAdapterFailure? = nil, _ body: (BootstrapFixture) throws -> Void) throws {
    let fixture = try BootstrapFixture(rootExists: rootExists, failure: failure)
    defer { fixture.removeExactlyOwnedRoot() }
    try body(fixture)
}

private final class BootstrapFixture: TestBootstrapAdapters {
    let parent: URL
    let root: URL
    let approvalString: String
    let approvalBytes: [UInt8]
    let approvalDigest: String
    let approvalVerificationPublicKeyBytes: [UInt8]
    let approvalSignature: [UInt8]
    let request: TrustedBootstrapRequest
    let helper: BootstrapHelperIdentity
    var helperOverride: BootstrapHelperIdentity?
    var rootWasPresent: Bool
    var failure: BootstrapAdapterFailure?
    var written: [BootstrapObject: [UInt8]] = [:]
    var modes: [BootstrapObject: Int] = [:]
    var didPublish = false
    var didAbort = false
    var didQuarantine = false
    var mutationCount = 0
    var usesCryptoRNG = false
    var rebindFails = false
    var quarantineFails = false
    var postPublishFailure: BootstrapAdapterFailure?
    var swapSourceAfterCopy = false
    var sourceBytes = Array("fixture-binary".utf8)
    var swapStagedBeforeAssessment = false
    var didSeal = false
    var sealed: Set<BootstrapObject> = []
    var durabilityTrace: [String] = []
    var durabilityFailureAt: String?
    var acceptApprovalSignature = true
    var didVerifyApprovalSignature = false
    var keyBGenerationRequests = 0
    var zeroizedKeyBBufferLengths: [Int] = []
    var keyBBufferTrace: [String] = []

    init(rootExists: Bool, failure: BootstrapAdapterFailure?) throws {
        parent = URL(fileURLWithPath: "/private/tmp/r4-bootstrap-tests-\(UUID().uuidString)", isDirectory: true)
        root = parent.appendingPathComponent("target", isDirectory: true)
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: false)
        if rootExists { try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false) }
        rootWasPresent = rootExists
        self.failure = failure
        let binary = CanonicalJSON.sha256(Array("fixture-binary".utf8))
        let requirement = CanonicalJSON.sha256(Array("fixture-requirement".utf8))
        helper = try BootstrapHelperIdentity(binarySHA256: SHA256Digest(binary), designatedRequirementSHA256: SHA256Digest(requirement), teamID: "3BMF4LM6TM", signingIdentitySHA1: "4C6503D0A78A9F51C86CC7C9683115E74A4902A7")
        approvalString = try Self.approval(binary: binary, requirement: requirement)
        approvalBytes = Array(approvalString.utf8)
        approvalDigest = CanonicalJSON.sha256(approvalBytes)
        approvalVerificationPublicKeyBytes = Self.hex("409181363d1c2e79a28e34d1b4382c59debeefdd7d542361f982d6779a7b91d3")
        approvalSignature = Array(repeating: 9, count: 64)
        request = TrustedBootstrapRequest.makeForTest(approvalVerificationPublicKeyBytes: approvalVerificationPublicKeyBytes, approvalBytes: approvalBytes, approvalSignature: approvalSignature, approvalSHA256: try SHA256Digest(approvalDigest))
    }

    func request(replacingApproval: [UInt8]? = nil, replacingVerificationPublicKey: [UInt8]? = nil, replacingDigest: String? = nil) throws -> TrustedBootstrapRequest {
        TrustedBootstrapRequest.makeForTest(approvalVerificationPublicKeyBytes: replacingVerificationPublicKey ?? approvalVerificationPublicKeyBytes, approvalBytes: replacingApproval ?? approvalBytes, approvalSignature: approvalSignature, approvalSHA256: try SHA256Digest(replacingDigest ?? approvalDigest))
    }

    func overrideHelper(binarySHA256: String? = nil, requirementSHA256: String? = nil, teamID: String? = nil, signingIdentitySHA1: String? = nil) throws {
        helperOverride = try BootstrapHelperIdentity(binarySHA256: SHA256Digest(binarySHA256 ?? helper.binarySHA256.value), designatedRequirementSHA256: SHA256Digest(requirementSHA256 ?? helper.designatedRequirementSHA256.value), teamID: teamID ?? helper.teamID, signingIdentitySHA1: signingIdentitySHA1 ?? helper.signingIdentitySHA1)
    }

    func rootExists(at root: String) throws -> Bool { FileManager.default.fileExists(atPath: self.root.path) }
    func entries(_ relativePath: String) throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: root.appendingPathComponent(relativePath).path).sorted()
    }
    func bytes(_ relativePath: String) throws -> [UInt8] {
        Array(try Data(contentsOf: root.appendingPathComponent(relativePath)))
    }
    func createFreshRoot(at root: String) throws {
        mutationCount += 1
        try FileManager.default.createDirectory(at: self.root, withIntermediateDirectories: false)
        rootWasPresent = true
    }
    func createDirectory(_ object: BootstrapObject) throws {
        mutationCount += 1
        let url = root.appendingPathComponent(object.rawValue)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
        modes[object] = 0o700
    }
    func write(_ bytes: [UInt8], to object: BootstrapObject, mode: Int) throws {
        durabilityTrace += ["writeAll:\(object.rawValue)", "fsync:\(object.rawValue)", "fullsync:\(object.rawValue)"]
        if durabilityFailureAt == "write-fullsync" { throw BootstrapAdapterFailure.fsync }
        mutationCount += 1
        modes[object] = mode
        let url = root.appendingPathComponent(object.rawValue)
        try Data(bytes).write(to: url, options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: url.path)
        if object == .generation1PrivateKey {
            keyBBufferTrace.append("write")
            return
        }
        written[object] = bytes
    }
    func fsync(_ object: BootstrapObject) throws { durabilityTrace += ["fsync:\(object.rawValue)", "fullsync:\(object.rawValue)"]; try failIf(.fsync) }
    func seal(_ object: BootstrapObject) throws {
        durabilityTrace += ["chmod:\(object.rawValue)", "chflags:\(object.rawValue)", "seal-fsync:\(object.rawValue)"]
        if durabilityFailureAt == "seal-fullsync" { throw BootstrapAdapterFailure.fsync }
        didSeal = true; sealed.insert(object); try failIf(.seal)
    }
    func verifyMetadata(_ object: BootstrapObject, expectedMode: Int) throws { try failIf(.acl); try failIf(.aclAPI) }
    func rebindAndVerify(_ object: BootstrapObject, expectedMode: Int) throws { try failIfObjectFailure() }
    func publishFreshRoot(at root: String) throws {
        durabilityTrace.append("pre-rename-volume")
        if durabilityFailureAt == "pre-rename-volume" { throw BootstrapAdapterFailure.fsync }
        durabilityTrace.append("rename-excl")
        didPublish = true
        durabilityTrace.append("parent-fsync")
        if durabilityFailureAt == "parent-fsync" { throw BootstrapAdapterFailure.fsync }
        durabilityTrace.append("post-rename-volume")
    }
    func abortFreshRoot() throws {
        didAbort = true
        durabilityTrace += ["abort-parent-fsync", "abort-volume"]
        if didPublish {
            _ = try quarantinePublishedRoot()
            return
        }
        if !didPublish, FileManager.default.fileExists(atPath: root.path) {
            try FileManager.default.removeItem(at: root)
        }
    }
    func quarantinePublishedRoot() throws -> Bool {
        guard didPublish else { return false }
        if quarantineFails { throw BootstrapAdapterFailure.operation }
        didQuarantine = true
        durabilityTrace += ["quarantine-parent-fsync", "quarantine-volume"]
        let target = parent.appendingPathComponent(".r4-incomplete-fixture")
        if FileManager.default.fileExists(atPath: root.path) {
            try FileManager.default.moveItem(at: root, to: target)
        }
        return true
    }
    func heldBootstrapInstallSource() throws -> BootstrapInstallSource {
        let held = sourceBytes
        if swapSourceAfterCopy { sourceBytes = Array("attacker-replacement".utf8) }
        return BootstrapInstallSource(provisionerBytes: held)
    }
    func assessStagedBootstrap(approval: Task14ApprovalV2, source: BootstrapInstallSource) throws -> BootstrapInstallIdentity {
        let staged = root.appendingPathComponent(BootstrapObject.provisioner.rawValue)
        if swapStagedBeforeAssessment {
            try FileManager.default.removeItem(at: staged)
            try Data("attacker-stage".utf8).write(to: staged, options: .withoutOverwriting)
        }
        guard (try? Data(contentsOf: staged)) == Data(source.provisionerBytes) else { throw BootstrapAdapterFailure.swapped }
        return BootstrapInstallIdentity(helper: helperOverride ?? helper, provisionerBytes: source.provisionerBytes, designatedRequirementBytes: Array("fixture-requirement".utf8))
    }
    func rebindInstalledHelper(expected: BootstrapHelperIdentity) throws {
        durabilityTrace.append("final-rebind")
        if durabilityFailureAt == "final-rebind" { throw BootstrapAdapterFailure.swapped }
        if let postPublishFailure { throw postPublishFailure }
        if rebindFails { throw BootstrapAdapterFailure.swapped }
        guard expected == helper else { throw BootstrapAdapterFailure.swapped }
    }
    func isValidApprovalSignature(verificationPublicKey: [UInt8], approval: [UInt8], signature: [UInt8]) throws -> Bool {
        didVerifyApprovalSignature = true
        return acceptApprovalSignature && verificationPublicKey == approvalVerificationPublicKeyBytes && signature == approvalSignature
    }
    func randomBytes(count: Int) throws -> [UInt8] {
        keyBGenerationRequests += 1
        if usesCryptoRNG { return Array(Curve25519.Signing.PrivateKey().rawRepresentation) }
        return Array(repeating: 7, count: count)
    }
    func zeroizeKeyB(_ bytes: inout [UInt8]) {
        let label = zeroizedKeyBBufferLengths.isEmpty ? "zeroize-seed" : "zeroize-private"
        zeroizedKeyBBufferLengths.append(bytes.count)
        bytes = Array(repeating: 0, count: bytes.count)
        keyBBufferTrace.append(label)
    }
    func verifySignProbe(publicKey: [UInt8], signature: [UInt8]) throws { try failIf(.signProbe) }

    private static func hex(_ value: String) -> [UInt8] {
        stride(from: 0, to: value.count, by: 2).map { UInt8(value[value.index(value.startIndex, offsetBy: $0)...value.index(value.startIndex, offsetBy: $0 + 1)], radix: 16)! }
    }

    /// Mirrors the future installed-helper capability boundary: this fixture exposes a single
    /// fixed admission-record operation, while arbitrary path mutation is not a capability.
    func appendAdmissionThroughCapability() throws {
        let file = root.appendingPathComponent("control/admissions/00000001")
        try Data("admission".utf8).write(to: file, options: .withoutOverwriting)
    }
    func attemptArbitraryRegistryMutation() throws { throw BootstrapAdapterFailure.operation }

    private func failIf(_ expected: BootstrapAdapterFailure) throws {
        if failure == expected { throw expected }
    }
    private func failIfObjectFailure() throws {
        let objectFailures: Set<BootstrapAdapterFailure> = [.missing, .extra, .symlink, .swapped]
        if let failure, objectFailures.contains(failure) { throw failure }
    }
    func removeExactlyOwnedRoot() {
        guard parent.path.hasPrefix("/private/tmp/r4-bootstrap-tests-") else { return }
        try? FileManager.default.removeItem(at: parent)
    }

    func rekinded(_ kind: String) throws -> String {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: approvalBytes) else { throw EvidenceError.invalidShape }
        let bare = pairs.filter { $0.0 != "sha256" }.map { $0.0 == "kind" ? ("kind", JSONValue.string(kind)) : $0 }
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(bare)))
        return String(bytes: try CanonicalJSON.encode(.object(bare + [("sha256", .string(digest))])), encoding: .utf8)!
    }

    func mixedV1V2Approval() throws -> String {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: approvalBytes) else { throw EvidenceError.invalidShape }
        let legacyHelpers: JSONValue = .array([.object([("path", .string("bootstrap/provisioner-v1")), ("sha256", .string(CanonicalJSON.sha256(Array("fixture-binary".utf8))) )])])
        let bare = pairs.filter { $0.0 != "sha256" }.map { $0.0 == "helper_hashes" ? ("helper_hashes", legacyHelpers) : $0 }
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(bare)))
        return String(bytes: try CanonicalJSON.encode(.object(bare + [("sha256", .string(digest))])), encoding: .utf8)!
    }

    private static func approval(binary: String, requirement: String) throws -> String {
        let h: (String) -> String = { CanonicalJSON.sha256(Array($0.utf8)) }
        let helpers: JSONValue = .array([
            .object([("path", .string("bootstrap/provisioner")), ("sha256", .string(binary))]),
            .object([("path", .string("bootstrap/provisioner.codesign-requirement.txt")), ("sha256", .string(requirement))]),
        ])
        let bare: [(String, JSONValue)] = [
            ("kind", .string("R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2")),
            ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("reviewed_head", .string(String(repeating: "a", count: 40))),
            ("reviewed_tree", .string(String(repeating: "b", count: 40))),
            ("required_blob_set_sha256", .string(h("blobs"))), ("exact_suite_log_sha256", .string(h("suite"))),
            ("input_bundle_manifest_sha256", .string(h("bundle"))), ("lock_sha256", .string(h("lock"))),
            ("python_archive_sha256", .string(h("archive"))), ("wheel_set_sha256", .string(h("wheels"))),
            ("task14_approval_verification_public_key_sha256", .string(ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)),
            ("materialization_policy_sha256", .string(h("policy"))), ("provisioner_swift_source_sha256", .string(h("source"))),
            ("review_package_sha256", .string(h("review"))), ("observation_authorization_sha256", .string(h("authorization"))),
            ("sandbox_launcher_path", .string("/usr/bin/sandbox-exec")), ("sandbox_launcher_descriptor_identity_sha256", .string(h("launcher-descriptor"))),
            ("sandbox_launcher_sha256", .string(h("launcher"))), ("sandbox_launcher_codesign_identity_sha256", .string(h("launcher-identity"))),
            ("sandbox_profile_template_sha256", .string(h("profile"))), ("child_command_template_sha256", .string(h("command"))),
            ("base_interpreter_path", .string("runtime/closure/python311/bin/python3.11")), ("base_interpreter_sha256", .string(h("interpreter"))),
            ("base_interpreter_mach_o_sha256", .string(h("interpreter-macho"))), ("base_interpreter_codesign_identity_sha256", .string(h("interpreter-identity"))),
            ("provisioner_binary_sha256", .string(binary)), ("provisioner_designated_requirement_sha256", .string(requirement)),
            ("helper_hashes", helpers), ("helper_team_id", .string("3BMF4LM6TM")), ("helper_signing_identity_sha1", .string("4C6503D0A78A9F51C86CC7C9683115E74A4902A7")),
        ]
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(bare)))
        return String(bytes: try CanonicalJSON.encode(.object(bare + [("sha256", .string(digest))])), encoding: .utf8)!
    }
}

final class DarwinBootstrapPublicationTests: XCTestCase {
    func testPostRenameStateImmediatelyCapturesFinalIdentityAndExactSuccessSequence() throws {
        try withDarwinPublicationFixture { fixture in
            try fixture.adapter.publishFreshRoot(at: fixture.root.path)
            XCTAssertEqual(fixture.stateAtFirstPostRenameSeam?.stageName, "target")
            XCTAssertEqual(fixture.stateAtFirstPostRenameSeam?.stageIdentity, fixture.stageIdentity)
            XCTAssertEqual(fixture.stateAtFirstPostRenameSeam?.isPublished, true)
            XCTAssertEqual(fixture.trace, ["rename", "open-final", "verify-final", "parent-fsync", "volume-fullsync", "security-rebind"])
        }
    }

    func testEveryPostRenameFailureQuarantinesTheCapturedFinalRoot() throws {
        for seam in ["open-final", "verify-final", "parent-fsync", "volume-fullsync", "security-rebind"] {
            try withDarwinPublicationFixture(failing: seam) { fixture in
                XCTAssertThrowsError(try fixture.adapter.publishFreshRoot(at: fixture.root.path), seam)
                XCTAssertNoThrow(try fixture.adapter.abortFreshRoot(), seam)
                XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.root.path), seam)
                XCTAssertTrue(fixture.hasQuarantine, seam)
                XCTAssertTrue(fixture.trace.contains("quarantine-rename"), seam)
                XCTAssertTrue(fixture.trace.contains("quarantine-parent-fsync"), seam)
                XCTAssertTrue(fixture.trace.contains("quarantine-volume-fullsync"), seam)
            }
        }
    }

    func testQuarantineFailureIsIncompleteAndResiduePreflightRejects() throws {
        try withDarwinPublicationFixture(failing: "quarantine-rename") { fixture in
            try fixture.adapter.publishFreshRoot(at: fixture.root.path)
            XCTAssertThrowsError(try fixture.adapter.abortFreshRoot())
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.root.path))
            XCTAssertTrue(try fixture.adapter.rootExists(at: fixture.root.path))
        }
    }

    func testPreRenameFailureCleansOnlyCapturedStage() throws {
        try withDarwinPublicationFixture(failing: "rename") { fixture in
            XCTAssertThrowsError(try fixture.adapter.publishFreshRoot(at: fixture.root.path))
            XCTAssertNoThrow(try fixture.adapter.abortFreshRoot())
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.stage.path))
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.root.path))
            XCTAssertFalse(fixture.hasQuarantine)
        }
    }

    func testForeignSwapIsNeverRemovedOrQuarantined() throws {
        try withDarwinPublicationFixture { fixture in
            try fixture.adapter.publishFreshRoot(at: fixture.root.path)
            try FileManager.default.removeItem(at: fixture.root)
            try FileManager.default.createDirectory(at: fixture.root, withIntermediateDirectories: false)
            try Data("foreign".utf8).write(to: fixture.root.appendingPathComponent("marker"))
            XCTAssertThrowsError(try fixture.adapter.abortFreshRoot())
            XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent("marker").path))
            XCTAssertFalse(fixture.hasQuarantine)
        }
    }

    func testProductionFactoryAndTestSeamsHaveTheIntendedVisibility() throws {
        let productionFactory: () throws -> DarwinBootstrapAdapters = DarwinBootstrapAdapters.makeProduction
        _ = productionFactory
        // This compiles only through @testable import: both the operation table and disposable
        // constructor remain module-internal and are absent from the production CLI surface.
        _ = DarwinBootstrapOperations.self
    }
}

private func withDarwinPublicationFixture(failing seam: String? = nil, _ body: (DarwinPublicationFixture) throws -> Void) throws {
    let fixture = try DarwinPublicationFixture(failing: seam)
    defer { fixture.remove() }
    try body(fixture)
}

private final class DarwinPublicationFixture {
    private final class Recorder {
        var events: [String] = []
        var stateAtOpen: DarwinBootstrapPublicationState?
    }
    let parent: URL
    let stage: URL
    let root: URL
    let stageIdentity: DarwinBootstrapPublicationIdentity
    let adapter: DarwinBootstrapAdapters
    private let recorder: Recorder
    var trace: [String] { recorder.events }
    var stateAtFirstPostRenameSeam: DarwinBootstrapPublicationState? { recorder.stateAtOpen }

    init(failing seam: String?) throws {
        parent = URL(fileURLWithPath: "/private/tmp/r4-darwin-publication-\(UUID().uuidString)", isDirectory: true)
        stage = parent.appendingPathComponent(".r4-bootstrap-stage", isDirectory: true)
        root = parent.appendingPathComponent("target", isDirectory: true)
        try FileManager.default.createDirectory(at: stage, withIntermediateDirectories: true)
        let parentDescriptor = try POSIX.openDirectory(parent.path)
        let traceRecorder = Recorder()
        var adapterReference: DarwinBootstrapAdapters?
        let stageDescriptor = try POSIX.openAt(parentDescriptor, name: stage.lastPathComponent, flags: O_RDONLY | O_DIRECTORY)
        stageIdentity = DarwinBootstrapPublicationIdentity(descriptor: stageDescriptor)
        let failure: (String) throws -> Void = { point in
            if seam == point { throw BootstrapAdapterFailure.fsync }
        }
        let operations = DarwinBootstrapOperations(
            renameExclusive: { from, name, to, destination in
                traceRecorder.events.append("rename")
                try failure("rename")
                try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
            },
            quarantineRename: { from, name, to, destination in
                traceRecorder.events.append("quarantine-rename")
                try failure("quarantine-rename")
                try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
            },
            openFinalRoot: { parent, name in
                traceRecorder.stateAtOpen = adapterReference?.testPublicationState
                traceRecorder.events.append("open-final"); try failure("open-final")
                return try POSIX.openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY)
            },
            verifyFinalMetadataAndTree: { _ in traceRecorder.events.append("verify-final"); try failure("verify-final") },
            durableParent: { _ in traceRecorder.events.append("parent-fsync"); try failure("parent-fsync") },
            volumeFullSync: { _ in traceRecorder.events.append("volume-fullsync"); try failure("volume-fullsync") },
            finalSecurityRebind: { _, _ in
                traceRecorder.events.append("security-rebind"); try failure("security-rebind")
                return DarwinBootstrapSecurityAssessment(designatedRequirementBytes: [], teamID: "test", signingIdentitySHA1: "test")
            },
            quarantineDurableParent: { _ in traceRecorder.events.append("quarantine-parent-fsync"); try failure("quarantine-parent-fsync") },
            quarantineVolumeFullSync: { _ in traceRecorder.events.append("quarantine-volume-fullsync"); try failure("quarantine-volume-fullsync") }
        )
        let disposable = DarwinBootstrapAdapters.makeDisposableForTest(parent: parentDescriptor, parentPath: parent.path, root: root.path, stageName: stage.lastPathComponent, stageDescriptor: stageDescriptor, operations: operations)
        adapter = disposable
        adapterReference = disposable
        recorder = traceRecorder
    }

    var hasQuarantine: Bool {
        (try? FileManager.default.contentsOfDirectory(atPath: parent.path).contains(where: { $0.hasPrefix(".r4-incomplete-") })) ?? false
    }

    func remove() { try? FileManager.default.removeItem(at: parent) }
}
