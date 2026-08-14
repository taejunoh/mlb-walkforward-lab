import CryptoKit
import Foundation

public enum BootstrapEvent: Equatable {
    case openKeyANoFollow
    case hashKeyA
    case compareCompiledKeyAHash
    case verifyApprovalSignature
    case decodeApproval
    case verifyApprovalDigest
    case verifyHelperIdentity
    case sealApproval
    case sealApprovalSignature
    case sealKeyA
    case sealApprovalDigest
    case generateKeyBGeneration1
    case sealKeyBGeneration1
}

public enum BootstrapAdapterFailure: Error, Equatable, Hashable {
    case missing
    case extra
    case symlink
    case swapped
    case fsync
    case seal
    case signProbe
    case acl
    case aclAPI
    case operation
}

public enum BootstrapError: Error, Equatable {
    case rootRequired
    case invalidApproval
    case invalidApprovalSignature
    case invalidApprovalVerificationKey
    case approvalDigestMismatch
    case helperBinaryHashMismatch
    case helperDesignatedRequirementMismatch
    case helperTeamIDMismatch
    case helperSigningIdentityMismatch
    case rootAlreadyExists
    case bootstrapIncomplete
    case keyGenerationFailed
    case adapter(BootstrapAdapterFailure)
}

public enum BootstrapObject: String, CaseIterable, Hashable {
    case bootstrapDirectory = "bootstrap"
    case controlDirectory = "control"
    case keyringDirectory = "control/keyring"
    case revocationsDirectory = "control/revocations"
    case admissionsDirectory = "control/admissions"
    case journalsDirectory = "control/journals"
    case materializationWorkDirectory = "materialization-work"
    case quarantineDirectory = "quarantine"
    case authoritiesDirectory = "authorities"
    case approvalVerificationPublicKey = "bootstrap/task14-approval-verification-public-key"
    case approval = "bootstrap/task14-approval-v2.json"
    case approvalSignature = "bootstrap/task14-approval-v2.ed25519"
    case approvalDigest = "bootstrap/task14-approval-v2.sha256"
    case policy = "bootstrap/policy.json"
    case materializationPolicy = "bootstrap/materialization-policy.json"
    case provisioner = "bootstrap/provisioner"
    case designatedRequirement = "bootstrap/provisioner.codesign-requirement.txt"
    case generation1Directory = "control/keyring/generation-1"
    case generation1PrivateKey = "control/keyring/generation-1/authority-receipt-ed25519-private-key"
    case generation1PublicKey = "control/keyring/generation-1/authority-receipt-ed25519-public-key"
    case generation1 = "control/keyring/generation-1/record.json"
    case generation1Signature = "control/keyring/generation-1/record.ed25519"

    var isDirectory: Bool {
        switch self {
        case .bootstrapDirectory, .controlDirectory, .keyringDirectory, .revocationsDirectory,
             .admissionsDirectory, .materializationWorkDirectory, .quarantineDirectory,
             .authoritiesDirectory, .journalsDirectory, .generation1Directory:
            return true
        default:
            return false
        }
    }

    /// Registries deliberately remain root-only mutable: later installed helpers receive a
    /// capability to append fixed-ID sealed records, not an ambient path mutation API.
    var isMutableRegistry: Bool {
        switch self {
        case .controlDirectory, .keyringDirectory, .revocationsDirectory, .admissionsDirectory,
             .journalsDirectory, .materializationWorkDirectory, .quarantineDirectory, .authoritiesDirectory:
            return true
        default:
            return false
        }
    }

    var finalMode: Int {
        if isDirectory { return isMutableRegistry ? 0o700 : 0o555 }
        return self == .generation1PrivateKey ? 0o600 : 0o444
    }

    var requiresImmutableFlag: Bool { !isMutableRegistry }
}

/// Opaque, descriptor-backed bootstrap input.  This is deliberately module-internal: production
/// callers can mint it only through `loadTrusted`, while @testable tests use `makeForTest`.
struct TrustedBootstrapRequest: Equatable {
    fileprivate let approvalVerificationPublicKeyBytes: [UInt8]
    fileprivate let approvalBytes: [UInt8]
    fileprivate let approvalSignature: [UInt8]
    fileprivate let approvalSHA256: SHA256Digest

    fileprivate init(approvalVerificationPublicKeyBytes: [UInt8], approvalBytes: [UInt8], approvalSignature: [UInt8], approvalSHA256: SHA256Digest) {
        self.approvalVerificationPublicKeyBytes = approvalVerificationPublicKeyBytes
        self.approvalBytes = approvalBytes
        self.approvalSignature = approvalSignature
        self.approvalSHA256 = approvalSHA256
    }

    /// The CLI is the only production source for these external inputs.  Each path is opened
    /// through a root-owned, no-follow descriptor and rebound before its held bytes are used.
    static func loadTrusted(approval: String, signature: String, verificationPublicKey: String, approvalSHA256: SHA256Digest) throws -> TrustedBootstrapRequest {
        func bytes(_ path: String) throws -> [UInt8] {
            let held = try POSIX.openTrustedRootOwnedRegular(path)
            let before = try POSIX.readAll(held.file)
            let after = try POSIX.readAll(try held.file.rebind(in: held.parent, name: held.name))
            guard before == after else { throw BootstrapError.invalidApproval }
            return before
        }
        return TrustedBootstrapRequest(approvalVerificationPublicKeyBytes: try bytes(verificationPublicKey), approvalBytes: try bytes(approval), approvalSignature: try bytes(signature), approvalSHA256: approvalSHA256)
    }

    /// Test-only construction capability.  It is internal and therefore absent to external
    /// clients of the executable product; real callers must traverse `loadTrusted` above.
    static func makeForTest(approvalVerificationPublicKeyBytes: [UInt8], approvalBytes: [UInt8], approvalSignature: [UInt8], approvalSHA256: SHA256Digest) -> TrustedBootstrapRequest {
        TrustedBootstrapRequest(approvalVerificationPublicKeyBytes: approvalVerificationPublicKeyBytes, approvalBytes: approvalBytes, approvalSignature: approvalSignature, approvalSHA256: approvalSHA256)
    }
}

public struct BootstrapHelperIdentity: Equatable {
    public let binarySHA256: SHA256Digest
    public let designatedRequirementSHA256: SHA256Digest
    public let teamID: String
    public let signingIdentitySHA1: String

    public init(binarySHA256: SHA256Digest, designatedRequirementSHA256: SHA256Digest, teamID: String, signingIdentitySHA1: String) {
        self.binarySHA256 = binarySHA256
        self.designatedRequirementSHA256 = designatedRequirementSHA256
        self.teamID = teamID
        self.signingIdentitySHA1 = signingIdentitySHA1
    }
}

/// Identity held from the administrator-authorized external bootstrap helper before the authority
/// root exists. The production adapter must obtain all bytes through a rebound descriptor.
public struct BootstrapInstallIdentity: Equatable {
    public let helper: BootstrapHelperIdentity
    public let provisionerBytes: [UInt8]
    public let designatedRequirementBytes: [UInt8]
    public init(helper: BootstrapHelperIdentity, provisionerBytes: [UInt8], designatedRequirementBytes: [UInt8]) {
        self.helper = helper; self.provisionerBytes = provisionerBytes; self.designatedRequirementBytes = designatedRequirementBytes
    }
}

public struct BootstrapInstallSource: Equatable {
    public let provisionerBytes: [UInt8]
    public init(provisionerBytes: [UInt8]) { self.provisionerBytes = provisionerBytes }
}

/// Identity of a later installed helper, which must rebind to bootstrap/provisioner.
public struct InstalledHelperIdentity: Equatable {
    public let helper: BootstrapHelperIdentity
    public init(helper: BootstrapHelperIdentity) { self.helper = helper }
}

/// Immutable evidence returned only after the published authority root has been rebound and
/// completely verified.  It deliberately exposes neither mutation handles nor private key data.
public struct BootstrapState: Equatable {
    public let publicKeySHA256: SHA256Digest
    public let generation: Int

    public init(publicKeySHA256: SHA256Digest, generation: Int) {
        self.publicKeySHA256 = publicKeySHA256
        self.generation = generation
    }
}

public struct BootstrapResult: Equatable {
    public let events: [BootstrapEvent]
    public let state: BootstrapState
    public var publicKeySHA256: SHA256Digest { state.publicKeySHA256 }
    public var generation: Int { state.generation }
}

/// Mutation capability. It is module-internal: public consumers only receive immutable state.
protocol BootstrapCoreAdapters: AnyObject {
    func rootExists(at root: String) throws -> Bool
    func createFreshRoot(at root: String) throws
    func createDirectory(_ object: BootstrapObject) throws
    func write(_ bytes: [UInt8], to object: BootstrapObject, mode: Int) throws
    func fsync(_ object: BootstrapObject) throws
    func verifyMetadata(_ object: BootstrapObject, expectedMode: Int) throws
    func seal(_ object: BootstrapObject) throws
    func rebindAndVerify(_ object: BootstrapObject, expectedMode: Int) throws
    func publishFreshRoot(at root: String) throws
    func abortFreshRoot() throws
    func quarantinePublishedRoot() throws -> Bool
    func heldBootstrapInstallSource() throws -> BootstrapInstallSource
    func assessStagedBootstrap(approval: Task14ApprovalV2, source: BootstrapInstallSource) throws -> BootstrapInstallIdentity
    func rebindInstalledHelper(expected: BootstrapHelperIdentity) throws
    func isValidApprovalSignature(verificationPublicKey: [UInt8], approval: [UInt8], signature: [UInt8]) throws -> Bool
    func randomBytes(count: Int) throws -> [UInt8]
    func zeroizeKeyB(_ bytes: inout [UInt8])
    func verifySignProbe(publicKey: [UInt8], signature: [UInt8]) throws
}

/// Disposable test capability; no production factory or public API accepts this type.
protocol TestBootstrapAdapters: BootstrapCoreAdapters {}

public enum Bootstrap {
    /// Verifies the exact graph emitted by bootstrap, including empty mutable registries.
    /// This intentionally remains stricter than the operational verifier: a rotated or
    /// residue-bearing registry must never be accepted as a fresh bootstrap publication.
    static func verifyBootstrapLayout(root: HeldDescriptor) throws {
        try DarwinBootstrapAdapters.verifyBootstrapLayout(root: root)
    }

    /// Read-only grammar check used after bootstrap. It validates names and object kinds only;
    /// cryptographic Key-B record verification belongs to the keyring verifier (Task 5).
    static func verifyOperationalRegistryLayout(root: HeldDescriptor) throws {
        try DarwinBootstrapAdapters.verifyOperationalRegistryLayout(root: root)
    }

    /// Module-internal production facade: its opaque argument can be minted only from trusted
    /// descriptor reads in this module, never from arbitrary external bytes.
    static func installTrusted(request: TrustedBootstrapRequest) throws -> BootstrapResult {
        let adapters = try DarwinBootstrapAdapters.makeProduction()
        return try install(request: request, root: ProtectedExecutionConstants.productionRoot, adapters: adapters)
    }

    /// Disposable-root entry point reserved for deterministic tests.
    static func installForTest(request: TrustedBootstrapRequest, root: String, adapters: some TestBootstrapAdapters) throws -> BootstrapResult {
        return try install(request: request, root: root, adapters: adapters)
    }

    private static func install(request: TrustedBootstrapRequest, root: String, adapters: some BootstrapCoreAdapters) throws -> BootstrapResult {
        var events: [BootstrapEvent] = []
        let approval = try validate(request: request, adapters: adapters, events: &events)
        var stageCreated = false
        var published = false
        do {
            guard try !adapters.rootExists(at: root) else { throw BootstrapError.rootAlreadyExists }
            try adapters.createFreshRoot(at: root)
            stageCreated = true
            for directory in [BootstrapObject.bootstrapDirectory, .controlDirectory, .keyringDirectory,
                              .revocationsDirectory, .admissionsDirectory, .journalsDirectory, .materializationWorkDirectory,
                              .quarantineDirectory, .authoritiesDirectory, .generation1Directory] {
                try adapters.createDirectory(directory)
            }
            let source = try adapters.heldBootstrapInstallSource()
            guard CanonicalJSON.sha256(source.provisionerBytes) == approval.provisionerBinarySHA256.value else { throw BootstrapError.helperBinaryHashMismatch }
            try adapters.write(source.provisionerBytes, to: .provisioner, mode: 0o444)
            let bootstrapIdentity = try adapters.assessStagedBootstrap(approval: approval, source: source)
            let assessed = bootstrapIdentity.helper
            guard assessed.binarySHA256 == approval.provisionerBinarySHA256 else { throw BootstrapError.helperBinaryHashMismatch }
            guard assessed.designatedRequirementSHA256 == approval.provisionerDesignatedRequirementSHA256,
                  CanonicalJSON.sha256(bootstrapIdentity.designatedRequirementBytes) == approval.provisionerDesignatedRequirementSHA256.value else { throw BootstrapError.helperDesignatedRequirementMismatch }
            guard assessed.teamID == approval.helperTeamID else { throw BootstrapError.helperTeamIDMismatch }
            guard assessed.signingIdentitySHA1 == approval.helperSigningIdentitySHA1 else { throw BootstrapError.helperSigningIdentityMismatch }
            events.append(.verifyHelperIdentity)

            // Key-A is public, externally held approval authority.  Only exact held input bytes
            // are copied into bootstrap; no Key-A private material enters this tree.
            try adapters.write(request.approvalBytes, to: .approval, mode: 0o444)
            try adapters.write(request.approvalSignature, to: .approvalSignature, mode: 0o444)
            try adapters.write(request.approvalVerificationPublicKeyBytes, to: .approvalVerificationPublicKey, mode: 0o444)
            try adapters.write(Array(request.approvalSHA256.value.utf8), to: .approvalDigest, mode: 0o444)
            for object in [BootstrapObject.approval, .approvalSignature, .approvalVerificationPublicKey, .approvalDigest] {
                try adapters.fsync(object)
                try adapters.verifyMetadata(object, expectedMode: object.finalMode)
                try adapters.seal(object)
                try adapters.rebindAndVerify(object, expectedMode: object.finalMode)
            }
            events += [.sealApproval, .sealApprovalSignature, .sealKeyA, .sealApprovalDigest]

            let privateKey: Curve25519.Signing.PrivateKey
            do {
                var seed = try adapters.randomBytes(count: 32)
                defer { adapters.zeroizeKeyB(&seed) }
                guard seed.count == 32 else { throw BootstrapError.keyGenerationFailed }
                do {
                    privateKey = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(seed))
                } catch {
                    throw BootstrapError.keyGenerationFailed
                }
            }
            let publicKey = Array(privateKey.publicKey.rawRepresentation)
            let publicKeySHA256 = try SHA256Digest(CanonicalJSON.sha256(publicKey))
            events.append(.generateKeyBGeneration1)

            do {
                var serializedPrivateKey = Array(privateKey.rawRepresentation)
                defer { adapters.zeroizeKeyB(&serializedPrivateKey) }
                try adapters.write(serializedPrivateKey, to: .generation1PrivateKey, mode: 0o600)
            }
            try adapters.write(publicKey, to: .generation1PublicKey, mode: 0o444)
            try adapters.write(bootstrapIdentity.designatedRequirementBytes, to: .designatedRequirement, mode: 0o444)
            try adapters.write(try policyBytes(approval: approval, approvalSHA256: request.approvalSHA256, publicKeySHA256: publicKeySHA256, requirementBytes: bootstrapIdentity.designatedRequirementBytes), to: .policy, mode: 0o444)
            try adapters.write(materializationPolicyBytes(), to: .materializationPolicy, mode: 0o444)

            let keyringSignature: [UInt8]
            do {
                keyringSignature = try Array(privateKey.signature(for: Data(try generation1PreimageBytes(publicKeySHA256: publicKeySHA256))))
            } catch {
                throw BootstrapError.keyGenerationFailed
            }
            let generation1Record = try generation1Bytes(publicKeySHA256: publicKeySHA256, signature: keyringSignature)
            do { try verifyGeneration1(recordBytes: generation1Record, signature: keyringSignature, publicKey: publicKey) }
            catch { throw BootstrapError.keyGenerationFailed }
            try adapters.write(generation1Record, to: .generation1, mode: 0o444)
            try adapters.write(keyringSignature, to: .generation1Signature, mode: 0o444)

            let probe = Data("R4 bootstrap sign probe".utf8)
            let probeSignature: [UInt8]
            do { probeSignature = try Array(privateKey.signature(for: probe)) }
            catch { throw BootstrapError.keyGenerationFailed }
            do {
                let verifier = try Curve25519.Signing.PublicKey(rawRepresentation: Data(publicKey))
                guard verifier.isValidSignature(Data(probeSignature), for: probe) else { throw BootstrapError.adapter(.signProbe) }
            } catch let error as BootstrapError {
                throw error
            } catch {
                throw BootstrapError.adapter(.signProbe)
            }
            try adapters.verifySignProbe(publicKey: publicKey, signature: probeSignature)

            for object in BootstrapObject.allCases {
                try adapters.fsync(object)
            }

            for object in BootstrapObject.allCases {
                let mode = object.isDirectory ? 0o700 : object.finalMode
                try adapters.verifyMetadata(object, expectedMode: mode)
            }

            for object in BootstrapObject.allCases where (object.requiresImmutableFlag || !object.isDirectory) && ![.approval, .approvalSignature, .approvalVerificationPublicKey, .approvalDigest].contains(object) {
                try adapters.seal(object)
            }
            events.append(.sealKeyBGeneration1)

            for object in BootstrapObject.allCases {
                try adapters.rebindAndVerify(object, expectedMode: object.finalMode)
            }
            try adapters.publishFreshRoot(at: root)
            published = true
            try adapters.rebindInstalledHelper(expected: bootstrapIdentity.helper)
            return BootstrapResult(events: events, state: BootstrapState(publicKeySHA256: publicKeySHA256, generation: 1))
        } catch {
            if published {
                do {
                    guard try adapters.quarantinePublishedRoot() else { throw BootstrapError.bootstrapIncomplete }
                } catch {
                    throw BootstrapError.bootstrapIncomplete
                }
            } else if stageCreated {
                // Only the captured root-owned stage capability may clean up; a replacement or
                // unrecognized child leaves residue and is never pathname-deleted.
                do { try adapters.abortFreshRoot() }
                catch { throw BootstrapError.bootstrapIncomplete }
            }
            if let bootstrapError = error as? BootstrapError { throw bootstrapError }
            if let failure = error as? BootstrapAdapterFailure { throw BootstrapError.adapter(failure) }
            throw BootstrapError.adapter(.operation)
        }
    }

    private static func validate(request: TrustedBootstrapRequest, adapters: some BootstrapCoreAdapters, events: inout [BootstrapEvent]) throws -> Task14ApprovalV2 {
        events.append(.openKeyANoFollow)
        guard request.approvalVerificationPublicKeyBytes.count == 32 else { throw BootstrapError.invalidApprovalVerificationKey }
        events.append(.hashKeyA)
        events.append(.compareCompiledKeyAHash)
        guard CanonicalJSON.sha256(request.approvalVerificationPublicKeyBytes) == ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256 else { throw BootstrapError.invalidApprovalVerificationKey }
        guard request.approvalSignature.count == 64 else { throw BootstrapError.invalidApprovalSignature }
        guard try adapters.isValidApprovalSignature(verificationPublicKey: request.approvalVerificationPublicKeyBytes, approval: request.approvalBytes, signature: request.approvalSignature) else { throw BootstrapError.invalidApprovalSignature }
        events.append(.verifyApprovalSignature)
        let source = String(bytes: request.approvalBytes, encoding: .utf8) ?? ""
        let approval: Task14ApprovalV2
        do {
            approval = try Task14ApprovalV2.decodeDocument(Array(source.utf8))
        } catch { throw BootstrapError.invalidApproval }
        events.append(.decodeApproval)
        guard CanonicalJSON.sha256(request.approvalBytes) == request.approvalSHA256.value else { throw BootstrapError.approvalDigestMismatch }
        events.append(.verifyApprovalDigest)
        return approval
    }

    private static func policyBytes(approval: Task14ApprovalV2, approvalSHA256: SHA256Digest, publicKeySHA256: SHA256Digest, requirementBytes: [UInt8]) throws -> [UInt8] {
        try CanonicalJSON.encode(.object([
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("key_generation", .integer(1)),
            ("kind", .string("R4_PROTECTED_EXECUTION_BOOTSTRAP_POLICY_V1")),
            ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("public_key_sha256", .string(publicKeySHA256.value)),
            ("task14_approval_sha256", .string(approvalSHA256.value)),
            ("provisioner_binary_sha256", .string(approval.provisionerBinarySHA256.value)),
            ("provisioner_designated_requirement_sha256", .string(approval.provisionerDesignatedRequirementSHA256.value)),
            ("helper_team_id", .string(approval.helperTeamID)),
            ("helper_signing_identity_sha1", .string(approval.helperSigningIdentitySHA1)),
            ("designated_requirement_utf8", .string(String(bytes: requirementBytes, encoding: .utf8) ?? "")),
            ("layout", .string("R4_BOOTSTRAP_FIXED_LAYOUT_V1")),
        ]))
    }

    private static func materializationPolicyBytes() throws -> [UInt8] {
        try CanonicalJSON.encode(.object([("kind", .string("R4_BOOTSTRAP_MATERIALIZATION_POLICY_V1")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("schema", .string("R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2"))]))
    }

    static func generation1PreimageBytes(publicKeySHA256: SHA256Digest) throws -> [UInt8] {
        try CanonicalJSON.encode(.object(generation1Preimage(publicKeySHA256: publicKeySHA256)))
    }

    static func generation1Bytes(publicKeySHA256: SHA256Digest, signature: [UInt8]) throws -> [UInt8] {
        let signed = generation1Preimage(publicKeySHA256: publicKeySHA256) + [
            ("detached_signature_sha256", .string(CanonicalJSON.sha256(signature))),
        ]
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(signed)))
        return try CanonicalJSON.encode(.object(signed + [("sha256", .string(digest))]))
    }

    static func verifyGeneration1(recordBytes: [UInt8], signature: [UInt8], publicKey: [UInt8]) throws {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: recordBytes) else { throw EvidenceError.invalidShape }
        let names = pairs.map(\.0)
        guard Set(names) == Set(["kind", "key_id", "generation", "public_key_sha256", "previous_record_sha256", "issued_at", "detached_signature_sha256", "sha256"]), names.count == 8 else { throw EvidenceError.invalidShape }
        func value(_ name: String) -> JSONValue { pairs.first { $0.0 == name }!.1 }
        guard case .string(let signatureDigest) = value("detached_signature_sha256"), signatureDigest == CanonicalJSON.sha256(signature),
              case .string(let actualDigest) = value("sha256") else { throw EvidenceError.invalidValue }
        let signed = pairs.filter { $0.0 != "sha256" }
        guard actualDigest == CanonicalJSON.sha256(try CanonicalJSON.encode(.object(signed))) else { throw EvidenceError.invalidValue }
        let preimage = try CanonicalJSON.encode(.object(pairs.filter { $0.0 != "detached_signature_sha256" && $0.0 != "sha256" }))
        guard case .string(let publicDigest) = value("public_key_sha256"),
              CanonicalJSON.sha256(publicKey) == publicDigest,
              case .string(let keyID) = value("key_id"), keyID == publicDigest else { throw EvidenceError.invalidValue }
        let verifier = try Curve25519.Signing.PublicKey(rawRepresentation: Data(publicKey))
        guard verifier.isValidSignature(Data(signature), for: Data(preimage)) else { throw EvidenceError.invalidValue }
    }

    private static func generation1Preimage(publicKeySHA256: SHA256Digest) -> [(String, JSONValue)] {
        [
            ("kind", .string("R4_PROTECTED_EXECUTION_KEYRING_V1")),
            ("key_id", .string(publicKeySHA256.value)),
            ("generation", .integer(1)),
            ("public_key_sha256", .string(publicKeySHA256.value)),
            ("previous_record_sha256", .null),
            ("issued_at", .string("bootstrap-generation-1")),
        ]
    }
}
