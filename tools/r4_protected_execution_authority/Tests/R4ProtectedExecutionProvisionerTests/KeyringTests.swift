import XCTest
import CryptoKit
@testable import R4ProtectedExecutionProvisioner

final class KeyringTests: XCTestCase {
    // This test names the production change that should make it pass: KeyringVerifier must
    // walk every closed generation rather than treating generation 1 as permanently active.
    func testActiveKeyIsHighestContiguousGenerationAndDerivesPublicBytes() throws {
        let fixture = try KeyringFixture.generationThree()
        var wipes: [(Int, Bool)] = []
        KeyringVerifier.zeroizationObservation = { wipes.append(($0, $1)) }
        defer { KeyringVerifier.zeroizationObservation = nil }
        let active = try KeyringVerifier.loadActiveForTest(fixture.root, keyA: fixture.keyA)
        XCTAssertEqual(active.generation, 3)
        XCTAssertEqual(active.publicKeySHA256, fixture.generation3PublicSHA256)
        let signature = try active.sign([0x72, 0x65, 0x63, 0x65, 0x69, 0x70, 0x74])
        XCTAssertTrue(try Curve25519.Signing.PublicKey(rawRepresentation: Data(fixture.generation3PublicBytes)).isValidSignature(Data(signature), for: Data([0x72, 0x65, 0x63, 0x65, 0x69, 0x70, 0x74])))
        XCTAssertEqual(wipes.count, 1)
        XCTAssertEqual(wipes.first?.0, 32)
        XCTAssertEqual(wipes.first?.1, true)
    }

    func testValidTerminalRevocationRejectsActiveAuthority() throws {
        let fixture = try KeyringFixture.revoked()
        XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(fixture.root, keyA: fixture.keyA))
    }

    func testVerifierRejectsGenerationGapsAliasesAndExtraChildren() throws {
        let fixture = try KeyringFixture.generationThree()
        try FileManager.default.removeItem(at: fixture.url.appendingPathComponent("control/keyring/generation-2"))
        XCTAssertThrowsError(try KeyringVerifier.verifyReadOnlyForTest(fixture.root, keyA: fixture.keyA))

        let alias = fixture.url.appendingPathComponent("control/keyring/generation-01")
        try FileManager.default.createDirectory(at: alias, withIntermediateDirectories: false)
        XCTAssertThrowsError(try KeyringVerifier.verifyReadOnlyForTest(fixture.root, keyA: fixture.keyA))
        XCTAssertTrue(FileManager.default.fileExists(atPath: alias.path))
    }

    func testReadOnlyVerificationDoesNotOpenOrDependOnPrivateKey() throws {
        let fixture = try KeyringFixture.generationThree()
        let privateKey = fixture.url.appendingPathComponent("control/keyring/generation-3/authority-receipt-ed25519-private-key")
        try Data([0x00]).write(to: privateKey)
        XCTAssertNoThrow(try KeyringVerifier.verifyReadOnlyForTest(fixture.root, keyA: fixture.keyA))

        var observations: [(Int, Bool)] = []
        KeyringVerifier.zeroizationObservation = { observations.append(($0, $1)) }
        defer { KeyringVerifier.zeroizationObservation = nil }
        XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(fixture.root, keyA: fixture.keyA))
        XCTAssertEqual(observations.count, 1)
        XCTAssertEqual(observations.first?.0, 1)
        XCTAssertEqual(observations.first?.1, true)
    }

    func testHeldKeyARejectsAKeyBGenerationWithTheSamePublicKey() throws {
        let fixture = try KeyringFixture.keyARoleConfusion()
        XCTAssertThrowsError(try KeyringVerifier.verifyReadOnlyForTest(fixture.root, keyA: fixture.keyA))
    }

    func testRevocationInsertedAfterSnapshotRejectsBeforePrivateOpen() throws {
        let fixture = try KeyringFixture.generationThree()
        KeyringVerifier.afterRevocationSnapshotHook = { try fixture.addValidRevocation() }
        defer { KeyringVerifier.afterRevocationSnapshotHook = nil }
        XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(fixture.root, keyA: fixture.keyA))
        XCTAssertTrue(FileManager.default.fileExists(atPath: fixture.url.appendingPathComponent("control/revocations/revocation-00000000000000000001").path))
    }

    func testKeyringReplacementBeforePrivateOpenRejectsAndPreservesForeignTree() throws {
        let fixture = try KeyringFixture.generationThree()
        let control = fixture.url.appendingPathComponent("control")
        let foreign = control.appendingPathComponent("keyring-foreign")
        KeyringVerifier.beforePrivateOpenHook = {
            try FileManager.default.moveItem(at: control.appendingPathComponent("keyring"), to: foreign)
            try FileManager.default.createDirectory(at: control.appendingPathComponent("keyring"), withIntermediateDirectories: false)
        }
        defer { KeyringVerifier.beforePrivateOpenHook = nil }
        XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(fixture.root, keyA: fixture.keyA))
        XCTAssertTrue(FileManager.default.fileExists(atPath: foreign.appendingPathComponent("generation-3").path))
    }

    func testPrivateReplacementAfterPublicVerificationRejectsAndWipes() throws {
        let fixture = try KeyringFixture.generationThree()
        let privatePath = fixture.url.appendingPathComponent("control/keyring/generation-3/authority-receipt-ed25519-private-key")
        KeyringVerifier.beforePrivateOpenHook = {
            try FileManager.default.removeItem(at: privatePath)
            try Data(Array(repeating: 0xFF, count: 32)).write(to: privatePath)
        }
        var opens = 0
        KeyringVerifier.privateOpenObservation = { opens += 1 }
        defer { KeyringVerifier.beforePrivateOpenHook = nil; KeyringVerifier.privateOpenObservation = nil }
        XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(fixture.root, keyA: fixture.keyA))
        XCTAssertEqual(opens, 1)
    }

    func testKeyringRejectsAReceiptKeyRoleConfusionAndGaps() throws {
        let fixture = try V2Fixture.complete()
        let receipt = try ReceiptV2.decode(fixture.receipt)
        let keyring = try KeyringRecord.decode(FixtureKeyring.genesis)
        XCTAssertThrowsError(try ReceiptKeyAuthority.validate(receipt: receipt,
                                                              keyring: [keyring],
                                                              receiptPublicKeySHA256: keyring.publicKeySHA256,
                                                              bootstrapKeyAPublicKeySHA256: keyring.publicKeySHA256))
        XCTAssertThrowsError(try ReceiptKeyAuthority.validate(receipt: receipt,
                                                              keyring: [],
                                                              receiptPublicKeySHA256: keyring.publicKeySHA256,
                                                              bootstrapKeyAPublicKeySHA256: try SHA256Digest(String(repeating: "f", count: 64))))
    }
}

/// A disposable descriptor-only Key-B graph.  It intentionally uses independently constructed
/// records and signatures rather than production generation helpers, so a shared bad preimage
/// cannot make the verifier test pass accidentally.
final class KeyringFixture {
    let root: HeldDescriptor
    let generation3PublicSHA256: R4ProtectedExecutionProvisioner.SHA256Digest
    let generation3PublicBytes: [UInt8]
    let keyA: KeyAIdentity
    var url: URL { disposable.root }
    private let disposable: DarwinDisposableRoot
    private let keyring: HeldDescriptor
    private let revocations: HeldDescriptor
    private let approvalSigner: Curve25519.Signing.PrivateKey
    private var generations: [(key: Curve25519.Signing.PrivateKey, public: [UInt8], record: KeyringRecord)] = []

    private init(keyAEqualsGenerationOne: Bool = false) throws {
        disposable = try DarwinDisposableRoot.create(prefix: "r4-keyring-fixture-")
        root = disposable.heldRoot
        let bootstrap = try Self.directory(root, "bootstrap", mode: 0o700)
        let keyASeed: [UInt8] = keyAEqualsGenerationOne ? Array((0..<32).map(UInt8.init)) : Array((128..<160).map(UInt8.init))
        let keyAKey = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(keyASeed))
        approvalSigner = keyAKey
        let keyADescriptor = try POSIX.createExclusive(bootstrap, name: "task14-approval-verification-public-key", mode: 0o444)
        try POSIX.writeAll(keyADescriptor, Array(keyAKey.publicKey.rawRepresentation))
        let readableKeyA = try POSIX.openAt(bootstrap, name: "task14-approval-verification-public-key", flags: O_RDONLY)
        keyA = try KeyringVerifier.heldKeyAForTest(bootstrap: bootstrap, descriptor: readableKeyA)
        let control = try Self.directory(root, "control", mode: 0o700)
        keyring = try Self.directory(control, "keyring", mode: 0o700)
        revocations = try Self.directory(control, "revocations", mode: 0o700)
        try Self.appendGeneration(to: keyring, index: 1, seed: Array(0..<32), previous: nil, output: &generations)
        try Self.appendGeneration(to: keyring, index: 2, seed: Array(32..<64), previous: generations.last, output: &generations)
        try Self.appendGeneration(to: keyring, index: 3, seed: Array(64..<96), previous: generations.last, output: &generations)
        generation3PublicBytes = generations[2].public
        generation3PublicSHA256 = generations[2].record.publicKeySHA256
    }

    deinit { try? disposable.removeOnlySelfOwnedFixture() }

    static func generationThree() throws -> KeyringFixture { try KeyringFixture() }
    static func keyARoleConfusion() throws -> KeyringFixture { try KeyringFixture(keyAEqualsGenerationOne: true) }

    static func revoked() throws -> KeyringFixture {
        let fixture = try KeyringFixture()
        let tip = fixture.generations[2]
        let core: [(String, JSONValue)] = [
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("kind", .string("R4_PROTECTED_EXECUTION_REVOCATION_V2")),
            ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("previous_revocation_sha256", .null), ("reason", .string("KEY_COMPROMISE")),
            ("revoked_at", .string("2026-08-12T00:00:00Z")), ("sequence", .integer(1)),
            ("signer_generation", .integer(3)), ("signer_key_record_sha256", .string(tip.record.sha256.value)),
            ("signer_public_key_sha256", .string(tip.record.publicKeySHA256.value)), ("state", .string("REVOKED")),
            ("target_generation", .integer(3)), ("target_key_record_sha256", .string(tip.record.sha256.value)),
            ("target_public_key_sha256", .string(tip.record.publicKeySHA256.value)),
        ]
        let signature = Array(try tip.key.signature(for: Data(Array("R4KEYREVOKE2\0".utf8) + CanonicalJSON.encode(.object(core)))) )
        let bytes = try Self.sealed(core + [("detached_signature_sha256", .string(CanonicalJSON.sha256(signature)))])
        let directory = try Self.directory(fixture.revocations, "revocation-00000000000000000001", mode: 0o700)
        try Self.file(directory, "record.json", bytes: bytes, mode: 0o444)
        try Self.file(directory, "record.ed25519", bytes: signature, mode: 0o444)
        return fixture
    }

    func addValidRevocation() throws {
        let tip = generations[2]
        let core: [(String, JSONValue)] = [
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("kind", .string("R4_PROTECTED_EXECUTION_REVOCATION_V2")),
            ("namespace", .string(ProtectedExecutionConstants.namespace)), ("previous_revocation_sha256", .null),
            ("reason", .string("KEY_COMPROMISE")), ("revoked_at", .string("2026-08-12T00:00:00Z")), ("sequence", .integer(1)),
            ("signer_generation", .integer(3)), ("signer_key_record_sha256", .string(tip.record.sha256.value)),
            ("signer_public_key_sha256", .string(tip.record.publicKeySHA256.value)), ("state", .string("REVOKED")),
            ("target_generation", .integer(3)), ("target_key_record_sha256", .string(tip.record.sha256.value)), ("target_public_key_sha256", .string(tip.record.publicKeySHA256.value)),
        ]
        let signature = Array(try tip.key.signature(for: Data(Array("R4KEYREVOKE2\0".utf8) + CanonicalJSON.encode(.object(core)))))
        let bytes = try Self.sealed(core + [("detached_signature_sha256", .string(CanonicalJSON.sha256(signature)))])
        let directory = try Self.directory(revocations, "revocation-00000000000000000001", mode: 0o700)
        try Self.file(directory, "record.json", bytes: bytes, mode: 0o444)
        try Self.file(directory, "record.ed25519", bytes: signature, mode: 0o444)
    }

    func installVerificationScaffold(approvalBytes: [UInt8], materializationPolicyBytes: [UInt8]) throws -> Verify.VerificationAuthority {
        let bootstrap = try POSIX.openAt(root, name: "bootstrap", flags: O_RDONLY | O_DIRECTORY)
        let approvalSignature = Array(try approvalSigner.signature(for: Data(approvalBytes)))
        for (name, bytes) in [
            ("task14-approval-v2.json", approvalBytes), ("task14-approval-v2.ed25519", approvalSignature),
            ("task14-approval-v2.sha256", Array(CanonicalJSON.sha256(approvalBytes).utf8)),
            ("policy.json", Array("policy".utf8)), ("materialization-policy.json", materializationPolicyBytes),
            ("provisioner", Array("provisioner".utf8)),
            ("provisioner.codesign-requirement.txt", Array("requirement".utf8))
        ] { try Self.file(bootstrap, name, bytes: bytes, mode: 0o444) }
        let control = try POSIX.openAt(root, name: "control", flags: O_RDONLY | O_DIRECTORY)
        _ = try Self.existingOrDirectory(control, "admissions", mode: 0o700)
        _ = try Self.existingOrDirectory(control, "journals", mode: 0o700)
        for name in ["materialization-work", "quarantine", "authorities"] { _ = try Self.existingOrDirectory(root, name, mode: 0o700) }
        return Verify.VerificationAuthority.makeForTest(root: root, expectedKeyAPublicSHA256: keyA.digest)
    }

    private static func appendGeneration(to keyring: HeldDescriptor, index: Int64, seed: [UInt8],
                                         previous: (key: Curve25519.Signing.PrivateKey, public: [UInt8], record: KeyringRecord)?,
                                         output: inout [(key: Curve25519.Signing.PrivateKey, public: [UInt8], record: KeyringRecord)]) throws {
        let key = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(seed))
        let publicBytes = Array(key.publicKey.rawRepresentation)
        let digest = CanonicalJSON.sha256(publicBytes)
        let core: [(String, JSONValue)]
        let signature: [UInt8]
        if let previous {
            core = [
                ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("generation", .integer(index)),
                ("issued_at", .string("2026-08-12T00:00:0\(index)Z")),
                ("kind", .string("R4_PROTECTED_EXECUTION_KEY_ROTATION_V2")),
                ("namespace", .string(ProtectedExecutionConstants.namespace)), ("new_public_key_sha256", .string(digest)),
                ("previous_generation", .integer(index - 1)), ("previous_public_key_sha256", .string(previous.record.publicKeySHA256.value)),
                ("previous_record_sha256", .string(previous.record.sha256.value)),
            ]
            signature = Array(try previous.key.signature(for: Data(Array("R4KEYROT2\0".utf8) + CanonicalJSON.encode(.object(core)))) )
        } else {
            core = [
                ("generation", .integer(1)), ("issued_at", .string("bootstrap-generation-1")),
                ("key_id", .string(digest)), ("kind", .string("R4_PROTECTED_EXECUTION_KEYRING_V1")),
                ("previous_record_sha256", .null), ("public_key_sha256", .string(digest)),
            ]
            signature = Array(try key.signature(for: Data(try CanonicalJSON.encode(.object(core)))))
        }
        let recordBytes = try sealed(core + [("detached_signature_sha256", .string(CanonicalJSON.sha256(signature)))])
        let record = try KeyringRecord.decode(String(decoding: recordBytes, as: UTF8.self))
        let directory = try self.directory(keyring, "generation-\(index)", mode: 0o700)
        try file(directory, "authority-receipt-ed25519-private-key", bytes: seed, mode: 0o600)
        try file(directory, "authority-receipt-ed25519-public-key", bytes: publicBytes, mode: 0o444)
        try file(directory, "record.json", bytes: recordBytes, mode: 0o444)
        try file(directory, "record.ed25519", bytes: signature, mode: 0o444)
        output.append((key, publicBytes, record))
    }

    private static func directory(_ parent: HeldDescriptor, _ name: String, mode: mode_t) throws -> HeldDescriptor {
        let descriptor = try POSIX.mkdirAt(parent, name: name, mode: mode)
        guard fchmod(descriptor.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
        return descriptor
    }

    private static func existingOrDirectory(_ parent: HeldDescriptor, _ name: String, mode: mode_t) throws -> HeldDescriptor {
        do { return try directory(parent, name, mode: mode) }
        catch POSIXFailure.system(let code) where code == EEXIST {
            let existing = try POSIX.openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY)
            guard try POSIX.metadata(existing).mode == mode else { throw POSIXFailure.unsafeObject }
            return try existing.rebindSameIdentity(in: parent, name: name, directory: true)
        }
    }

    private static func file(_ parent: HeldDescriptor, _ name: String, bytes: [UInt8], mode: mode_t) throws {
        let descriptor = try POSIX.createExclusive(parent, name: name, mode: mode)
        try POSIX.writeAll(descriptor, bytes)
        guard fchmod(descriptor.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
    }

    private static func sealed(_ fields: [(String, JSONValue)]) throws -> [UInt8] {
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(fields)))
        return try CanonicalJSON.encode(.object(fields + [("sha256", .string(digest))]))
    }
}

enum FixtureKeyring {
    static let keyID = String(repeating: "b", count: 64)
    static let publicKey = String(repeating: "b", count: 64)
    static let signature = String(repeating: "c", count: 64)
    static func seal(_ fields: [(String, JSONValue)]) throws -> String {
        let bare = try CanonicalJSON.encode(.object(fields))
        return String(decoding: try CanonicalJSON.encode(.object(fields + [("sha256", .string(CanonicalJSON.sha256(bare)))])), as: UTF8.self)
    }
    static var genesis: String { get throws { try seal([
        ("kind", .string("R4_PROTECTED_EXECUTION_KEYRING_V1")), ("key_id", .string(keyID)),
        ("generation", .integer(1)), ("public_key_sha256", .string(publicKey)),
        ("previous_record_sha256", .null), ("issued_at", .string("bootstrap-generation-1")),
        ("detached_signature_sha256", .string(signature))]) } }
    static var revocation: String { get throws {
        let record = try KeyringRecord.decode(genesis)
        return try seal([
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("detached_signature_sha256", .string(signature)),
            ("kind", .string("R4_PROTECTED_EXECUTION_REVOCATION_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("previous_revocation_sha256", .null), ("reason", .string("KEY_COMPROMISE")),
            ("revoked_at", .string("2026-08-12T00:00:01Z")), ("sequence", .integer(1)),
            ("signer_generation", .integer(1)), ("signer_key_record_sha256", .string(record.sha256.value)),
            ("signer_public_key_sha256", .string(publicKey)), ("state", .string("REVOKED")),
            ("target_generation", .integer(1)), ("target_key_record_sha256", .string(record.sha256.value)),
            ("target_public_key_sha256", .string(publicKey))])
    } }
}
