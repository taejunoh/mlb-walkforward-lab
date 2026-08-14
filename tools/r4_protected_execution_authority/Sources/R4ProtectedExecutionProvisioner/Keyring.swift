import Foundation
import CryptoKit

/// A signing capability selected only after the complete public Key-B history is accepted.
/// Its private CryptoKit value is intentionally not exposed as bytes or a descriptor.
struct ActiveReceiptSigner {
    let generation: Int64
    let publicKeySHA256: SHA256Digest
    let publicKeyBytes: [UInt8]
    fileprivate let privateKey: Curve25519.Signing.PrivateKey

    func sign(_ receiptBytes: [UInt8]) throws -> [UInt8] {
        Array(try privateKey.signature(for: Data(receiptBytes)))
    }
}

/// Public, descriptor-derived chain evidence.  This contains no private-key material and is
/// therefore safe for read-only authority and receipt verification paths.
struct VerifiedKeyring {
    let generation: Int64
    let publicKeySHA256: SHA256Digest
    fileprivate let publicKeyBytes: [UInt8]
    fileprivate let records: [KeyringRecord]
    fileprivate let root: HeldDescriptor
    fileprivate let control: HeldDescriptor
    fileprivate let keyring: HeldDescriptor
    fileprivate let revocations: HeldDescriptor
    fileprivate let controlSnapshot: Set<String>
    fileprivate let keyringSnapshot: Set<String>
    fileprivate let revocationSnapshot: Set<String>
    fileprivate let keyA: KeyAIdentity
}

/// The approval key enters Key-B verification only as a fixed, descriptor-held bootstrap
/// capability. There is no caller-byte or caller-path route.
struct KeyAIdentity {
    fileprivate let bootstrap: HeldDescriptor
    fileprivate let descriptor: HeldDescriptor
    fileprivate let bytes: [UInt8]
    let digest: SHA256Digest
}

private struct KeyGenerationEvidence {
    let generation: Int64
    let directory: HeldDescriptor
    let publicBytes: [UInt8]
    let record: KeyringRecord
}

enum KeyringVerifier {
    static func verifyReadOnly() throws -> VerifiedKeyring {
        let root = try POSIX.openTrustedRootOwnedDirectory(ProtectedExecutionConstants.productionRoot)
        return try verifyReadOnly(root: root, strictMetadata: true)
    }

    /// Descriptor-only receiver entry.  The caller never supplies a public key, root path, or
    /// private-key capability; Key-A is opened and pinned from the held root itself.
    static func verifyReadOnly(root: HeldDescriptor, strictMetadata: Bool) throws -> VerifiedKeyring {
        try verifyReadOnly(root: root, keyA: try fixedKeyA(root), requireFullLayout: true,
                           strictMetadata: strictMetadata)
    }

    static func loadActive() throws -> ActiveReceiptSigner {
        let root = try POSIX.openTrustedRootOwnedDirectory(ProtectedExecutionConstants.productionRoot)
        return try loadActive(root: root, keyA: try fixedKeyA(root), requireFullLayout: true, strictMetadata: true)
    }

#if DEBUG
    /// A descriptor capability supplied by a disposable test root.  It is deliberately not a
    /// path-taking API, and does not exist in a non-DEBUG helper binary.
    static func verifyReadOnlyForTest(_ root: HeldDescriptor, keyA: KeyAIdentity) throws -> VerifiedKeyring {
        try verifyReadOnly(root: root, keyA: keyA, requireFullLayout: false, strictMetadata: false)
    }

    /// The held fixture root is the only test input.  The fixture Key-A is still opened from
    /// its fixed bootstrap name and the approval signature is checked cryptographically by
    /// Verify; production additionally enforces the compiled Key-A digest pin.
    static func verifyReadOnlyForTestRoot(_ root: HeldDescriptor) throws -> VerifiedKeyring {
        let bootstrap = try openedDirectory(root, "bootstrap")
        let descriptor = try openedRegular(bootstrap, "task14-approval-verification-public-key")
        return try verifyReadOnlyForTest(root, keyA: try heldKeyAForTest(bootstrap: bootstrap, descriptor: descriptor))
    }

    static func loadActiveForTest(_ root: HeldDescriptor, keyA: KeyAIdentity) throws -> ActiveReceiptSigner {
        try loadActive(root: root, keyA: keyA, requireFullLayout: false, strictMetadata: false)
    }

    static func heldKeyAForTest(bootstrap: HeldDescriptor, descriptor: HeldDescriptor) throws -> KeyAIdentity {
        let bytes = try POSIX.readAll(descriptor, maximum: 33)
        guard bytes.count == 32 else { throw EvidenceError.invalidValue }
        return KeyAIdentity(bootstrap: bootstrap, descriptor: descriptor, bytes: bytes,
                            digest: try SHA256Digest(CanonicalJSON.sha256(bytes)))
    }

    /// Reports only shape/zeroed-state, never private bytes.  Tests use this to prove that the
    /// one owned raw buffer is wiped even if key construction throws.
    static var zeroizationObservation: ((Int, Bool) -> Void)?
    static var afterRevocationSnapshotHook: (() throws -> Void)?
    static var beforePrivateOpenHook: (() throws -> Void)?
    static var privateOpenObservation: (() -> Void)?
#endif

    private static func verifyReadOnly(root: HeldDescriptor, keyA: KeyAIdentity, requireFullLayout: Bool,
                                       strictMetadata: Bool) throws -> VerifiedKeyring {
        if requireFullLayout { try Bootstrap.verifyOperationalRegistryLayout(root: root) }
        let control = try openedDirectory(root, "control")
        let keyring = try openedDirectory(control, "keyring")
        let revocations = try openedDirectory(control, "revocations")
        let controlSnapshot = try POSIX.directoryEntryNames(control)
        try requireMutableRegistry(root, strictMetadata: strictMetadata)
        try requireMutableRegistry(control, strictMetadata: strictMetadata)
        try requireMutableRegistry(keyring, strictMetadata: strictMetadata)
        let names = try POSIX.directoryEntryNames(keyring)
        let generations = try canonicalGenerationNames(names)
        var evidence: [KeyGenerationEvidence] = []
        for (generation, name) in generations {
            let item = try readGeneration(keyring: keyring, name: name, generation: generation,
                                          strictMetadata: strictMetadata)
            if let previous = evidence.last {
                try verifyRotation(item, predecessor: previous)
            } else {
                try verifyGenesis(item)
            }
            evidence.append(item)
        }
        let finalKeyring = try keyring.rebindSameIdentity(in: control, name: "keyring", directory: true)
        guard try POSIX.directoryEntryNames(finalKeyring) == names else { throw POSIXFailure.unsafeObject }
        let records = evidence.map(\.record)
        try KeyringRecord.validateContiguous(records)
        guard let active = evidence.last else { throw EvidenceError.invalidValue }
        let revocationSnapshot = try verifyTerminalRevocation(revocations: revocations, active: active, strictMetadata: strictMetadata)
        let finalControl = try control.rebindSameIdentity(in: root, name: "control", directory: true)
        let finalRevocations = try revocations.rebindSameIdentity(in: finalControl, name: "revocations", directory: true)
        let finalRevocationSnapshot = try POSIX.directoryEntryNames(finalRevocations)
        let finalControlSnapshot = try POSIX.directoryEntryNames(finalControl)
        guard finalRevocationSnapshot == revocationSnapshot,
              finalControlSnapshot == controlSnapshot else { throw POSIXFailure.unsafeObject }
        try validateKeyA(keyA, strictMetadata: strictMetadata)
        guard evidence.allSatisfy({ $0.publicBytes != keyA.bytes && $0.record.publicKeySHA256 != keyA.digest }) else { throw EvidenceError.invalidValue }
        return VerifiedKeyring(generation: active.generation, publicKeySHA256: active.record.publicKeySHA256,
                               publicKeyBytes: active.publicBytes, records: records, root: root, control: control,
                               keyring: keyring, revocations: revocations, controlSnapshot: controlSnapshot,
                               keyringSnapshot: names, revocationSnapshot: revocationSnapshot, keyA: keyA)
    }

    /// Rebind the exact public evidence accepted by `verifyReadOnly`.  This intentionally
    /// receives the retained descriptors rather than opening control/keyring by name: a
    /// same-name replacement must fail before any newly named registry can influence Verify.
    static func revalidateRetained(_ verified: VerifiedKeyring, from root: HeldDescriptor,
                                   strictMetadata: Bool) throws {
        guard verified.root.device == root.device, verified.root.inode == root.inode else { throw POSIXFailure.unsafeObject }
        let control = try verified.control.rebindSameIdentity(in: root, name: "control", directory: true)
        let keyring = try verified.keyring.rebindSameIdentity(in: control, name: "keyring", directory: true)
        let revocations = try verified.revocations.rebindSameIdentity(in: control, name: "revocations", directory: true)
        try requireMutableRegistry(root, strictMetadata: strictMetadata)
        try requireMutableRegistry(control, strictMetadata: strictMetadata)
        try requireMutableRegistry(keyring, strictMetadata: strictMetadata)
        try requireMutableRegistry(revocations, strictMetadata: strictMetadata)
        guard try POSIX.directoryEntryNames(control) == verified.controlSnapshot,
              try POSIX.directoryEntryNames(keyring) == verified.keyringSnapshot,
              try POSIX.directoryEntryNames(revocations) == verified.revocationSnapshot else {
            throw POSIXFailure.unsafeObject
        }
        let generations = try canonicalGenerationNames(verified.keyringSnapshot)
        var evidence: [KeyGenerationEvidence] = []
        for (generation, name) in generations {
            let item = try readGeneration(keyring: keyring, name: name, generation: generation,
                                          strictMetadata: strictMetadata)
            if let previous = evidence.last { try verifyRotation(item, predecessor: previous) }
            else { try verifyGenesis(item) }
            evidence.append(item)
        }
        guard evidence.map(\.record) == verified.records,
              let active = evidence.last, active.generation == verified.generation,
              active.publicBytes == verified.publicKeyBytes,
              active.record.publicKeySHA256 == verified.publicKeySHA256 else { throw EvidenceError.invalidValue }
        guard try verifyTerminalRevocation(revocations: revocations, active: active,
                                           strictMetadata: strictMetadata) == verified.revocationSnapshot else {
            throw POSIXFailure.unsafeObject
        }
        let bootstrap = try verified.keyA.bootstrap.rebindSameIdentity(in: root, name: "bootstrap", directory: true)
        let keyAFile = try verified.keyA.descriptor.rebindSameIdentity(in: bootstrap,
                                                                         name: "task14-approval-verification-public-key")
        let keyA = KeyAIdentity(bootstrap: bootstrap, descriptor: keyAFile, bytes: verified.keyA.bytes,
                                digest: verified.keyA.digest)
        guard try POSIX.readAll(keyAFile, maximum: 33) == verified.keyA.bytes else { throw POSIXFailure.unsafeObject }
        try validateKeyA(keyA, strictMetadata: strictMetadata)
    }

    private static func loadActive(root: HeldDescriptor, keyA: KeyAIdentity, requireFullLayout: Bool,
                                   strictMetadata: Bool) throws -> ActiveReceiptSigner {
        let verified = try verifyReadOnly(root: root, keyA: keyA, requireFullLayout: requireFullLayout,
                                          strictMetadata: strictMetadata)
#if DEBUG
        try beforePrivateOpenHook?()
#endif
        let control = try verified.control.rebindSameIdentity(in: verified.root, name: "control", directory: true)
        let keyring = try verified.keyring.rebindSameIdentity(in: control, name: "keyring", directory: true)
        let revocations = try verified.revocations.rebindSameIdentity(in: control, name: "revocations", directory: true)
        guard try POSIX.directoryEntryNames(control) == verified.controlSnapshot,
              try POSIX.directoryEntryNames(keyring) == verified.keyringSnapshot,
              try POSIX.directoryEntryNames(revocations) == verified.revocationSnapshot else { throw POSIXFailure.unsafeObject }
        try requireMutableRegistry(verified.root, strictMetadata: strictMetadata)
        try requireMutableRegistry(control, strictMetadata: strictMetadata)
        try requireMutableRegistry(keyring, strictMetadata: strictMetadata)
        try requireMutableRegistry(revocations, strictMetadata: strictMetadata)
        try validateKeyA(verified.keyA, strictMetadata: strictMetadata)
        let name = generationName(verified.generation)
        let generation = try openedDirectory(keyring, name)
        let publicKey = try openedRegular(generation, "authority-receipt-ed25519-public-key")
        guard try POSIX.readAll(publicKey, maximum: 33) == verified.publicKeyBytes else { throw POSIXFailure.unsafeObject }
        if strictMetadata {
            try generation.requireSealedDirectory(mode: 0o555)
            try publicKey.requireSealedRegular(mode: 0o444)
        }
        let finalGeneration = try generation.rebindSameIdentity(in: keyring, name: name, directory: true)
        _ = try publicKey.rebindSameIdentity(in: generation, name: "authority-receipt-ed25519-public-key")
        let finalKeyring = try keyring.rebindSameIdentity(in: control, name: "keyring", directory: true)
        let finalRevocations = try revocations.rebindSameIdentity(in: control, name: "revocations", directory: true)
        guard try POSIX.readAll(publicKey, maximum: 33) == verified.publicKeyBytes,
              try POSIX.directoryEntryNames(finalKeyring) == verified.keyringSnapshot,
              try POSIX.directoryEntryNames(finalRevocations) == verified.revocationSnapshot,
              finalGeneration.device == generation.device, finalGeneration.inode == generation.inode else { throw POSIXFailure.unsafeObject }
#if DEBUG
        privateOpenObservation?()
#endif
        let privateKey = try openedRegular(generation, "authority-receipt-ed25519-private-key")
        if strictMetadata { try privateKey.requireSealedRegular(mode: 0o600) }
        var privateBytes = try POSIX.readAll(privateKey, maximum: 33)
        defer { overwritePrivateBytes(&privateBytes) }
        guard privateBytes.count == 32 else { throw EvidenceError.invalidValue }
        let key = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(privateBytes))
        guard Array(key.publicKey.rawRepresentation) == verified.publicKeyBytes else { throw EvidenceError.invalidValue }
        // The public and private names are rebound only after all public evidence is accepted,
        // preventing an unverified private object from being opened during chain walking.
        _ = try publicKey.rebindSameIdentity(in: generation, name: "authority-receipt-ed25519-public-key")
        _ = try privateKey.rebindSameIdentity(in: generation, name: "authority-receipt-ed25519-private-key")
        _ = try generation.rebindSameIdentity(in: keyring, name: name, directory: true)
        return ActiveReceiptSigner(generation: verified.generation, publicKeySHA256: verified.publicKeySHA256,
                                   publicKeyBytes: verified.publicKeyBytes, privateKey: key)
    }

    private static func verifyGenesis(_ evidence: KeyGenerationEvidence) throws {
        guard evidence.generation == 1, evidence.record.variant == .frozenGenesis,
              evidence.record.publicKeySHA256.value == CanonicalJSON.sha256(evidence.publicBytes) else {
            throw EvidenceError.invalidValue
        }
        let signature = try readSignature(evidence.directory, evidence.record)
        try Bootstrap.verifyGeneration1(recordBytes: try recordBytes(evidence.directory, evidence.record),
                                        signature: signature, publicKey: evidence.publicBytes)
    }

    private static func verifyRotation(_ evidence: KeyGenerationEvidence,
                                       predecessor: KeyGenerationEvidence) throws {
        let record = evidence.record
        guard record.variant == .rotationV2, record.generation == predecessor.generation + 1,
              record.previousGeneration == predecessor.generation,
              record.previousRecordSHA256 == predecessor.record.sha256,
              record.previousPublicKeySHA256 == predecessor.record.publicKeySHA256,
              record.publicKeySHA256.value == CanonicalJSON.sha256(evidence.publicBytes) else {
            throw EvidenceError.invalidValue
        }
        let signature = try readSignature(evidence.directory, record)
        let signer = try Curve25519.Signing.PublicKey(rawRepresentation: Data(predecessor.publicBytes))
        guard signer.isValidSignature(Data(signature), for: Data(try record.rotationSignaturePreimage())) else {
            throw EvidenceError.invalidValue
        }
    }

    /// Valid terminal revocation is a proof of authority closure, not a recoverable condition.
    private static func verifyTerminalRevocation(revocations: HeldDescriptor, active: KeyGenerationEvidence,
                                                 strictMetadata: Bool) throws -> Set<String> {
        try requireMutableRegistry(revocations, strictMetadata: strictMetadata)
        let names = try POSIX.directoryEntryNames(revocations)
#if DEBUG
        try afterRevocationSnapshotHook?()
#endif
        guard names.isEmpty || names == ["revocation-00000000000000000001"] else { throw EvidenceError.invalidValue }
        guard let name = names.first else { return names }
        let directory = try openedDirectory(revocations, name)
        if strictMetadata { try directory.requireSealedDirectory(mode: 0o555) }
        let expected = Set(["record.json", "record.ed25519"])
        guard try POSIX.directoryEntryNames(directory) == expected else { throw EvidenceError.invalidValue }
        let recordFile = try openedRegular(directory, "record.json")
        let signatureFile = try openedRegular(directory, "record.ed25519")
        if strictMetadata {
            try recordFile.requireSealedRegular(mode: 0o444)
            try signatureFile.requireSealedRegular(mode: 0o444)
        }
        let bytes = try POSIX.readAll(recordFile)
        let signature = try POSIX.readAll(signatureFile, maximum: 65)
        guard signature.count == 64 else { throw EvidenceError.invalidValue }
        let record = try KeyringRecord.decode(String(decoding: bytes, as: UTF8.self))
        guard try record.canonicalRecordBytes() == bytes,
              record.variant == .revocationV2,
              record.generation == active.generation,
              record.publicKeySHA256 == active.record.publicKeySHA256,
              record.signerGeneration == active.generation,
              record.signerKeyRecordSHA256 == active.record.sha256,
              record.targetKeyRecordSHA256 == active.record.sha256,
              record.previousPublicKeySHA256 == active.record.publicKeySHA256,
              record.detachedSignatureSHA256.value == CanonicalJSON.sha256(signature) else {
            throw EvidenceError.invalidValue
        }
        let signer = try Curve25519.Signing.PublicKey(rawRepresentation: Data(active.publicBytes))
        guard signer.isValidSignature(Data(signature), for: Data(try record.revocationSignaturePreimage())) else {
            throw EvidenceError.invalidValue
        }
        _ = try directory.rebindSameIdentity(in: revocations, name: name, directory: true)
        throw EvidenceError.invalidValue
    }

    private static func fixedKeyA(_ root: HeldDescriptor) throws -> KeyAIdentity {
        let bootstrap = try openedDirectory(root, "bootstrap")
        let descriptor = try openedRegular(bootstrap, "task14-approval-verification-public-key")
        let bytes = try POSIX.readAll(descriptor, maximum: 33)
        guard bytes.count == 32,
              CanonicalJSON.sha256(bytes) == ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256 else {
            throw EvidenceError.invalidValue
        }
        return KeyAIdentity(bootstrap: bootstrap, descriptor: descriptor, bytes: bytes,
                            digest: try SHA256Digest(CanonicalJSON.sha256(bytes)))
    }

    private static func validateKeyA(_ keyA: KeyAIdentity, strictMetadata: Bool) throws {
        let rebound = try keyA.descriptor.rebindSameIdentity(in: keyA.bootstrap,
                                                             name: "task14-approval-verification-public-key")
        let bytes = try POSIX.readAll(rebound, maximum: 33)
        guard bytes == keyA.bytes, bytes.count == 32 else { throw POSIXFailure.unsafeObject }
        if strictMetadata { try rebound.requireSealedRegular(mode: 0o444) }
    }

    private static func readGeneration(keyring: HeldDescriptor, name: String, generation: Int64,
                                       strictMetadata: Bool) throws -> KeyGenerationEvidence {
        let directory = try openedDirectory(keyring, name)
        if strictMetadata { try directory.requireSealedDirectory(mode: 0o555) }
        let expected = Set(["authority-receipt-ed25519-private-key", "authority-receipt-ed25519-public-key", "record.json", "record.ed25519"])
        guard try POSIX.directoryEntryNames(directory) == expected else { throw EvidenceError.invalidValue }
        // `verifyReadOnly` intentionally does not open a private key.  The closed name set
        // still rejects additions/omissions; only loadActive opens the selected tip after every
        // public record and any terminal revocation has been verified.
        let publicKey = try openedRegular(directory, "authority-receipt-ed25519-public-key")
        let recordFile = try openedRegular(directory, "record.json")
        let signatureFile = try openedRegular(directory, "record.ed25519")
        if strictMetadata {
            try publicKey.requireSealedRegular(mode: 0o444)
            try recordFile.requireSealedRegular(mode: 0o444)
            try signatureFile.requireSealedRegular(mode: 0o444)
        }
        let publicBytes = try POSIX.readAll(publicKey, maximum: 33)
        guard publicBytes.count == 32 else { throw EvidenceError.invalidValue }
        let recordBytes = try POSIX.readAll(recordFile)
        let record = try KeyringRecord.decode(String(decoding: recordBytes, as: UTF8.self))
        guard record.generation == generation, try record.canonicalRecordBytes() == recordBytes else { throw EvidenceError.invalidValue }
        _ = try directory.rebindSameIdentity(in: keyring, name: name, directory: true)
        return KeyGenerationEvidence(generation: generation, directory: directory, publicBytes: publicBytes,
                                     record: record)
    }

    private static func recordBytes(_ directory: HeldDescriptor, _ record: KeyringRecord) throws -> [UInt8] {
        let descriptor = try openedRegular(directory, "record.json")
        let bytes = try POSIX.readAll(descriptor)
        guard try record.canonicalRecordBytes() == bytes else { throw EvidenceError.invalidValue }
        return bytes
    }

    private static func readSignature(_ directory: HeldDescriptor, _ record: KeyringRecord) throws -> [UInt8] {
        let descriptor = try openedRegular(directory, "record.ed25519")
        let bytes = try POSIX.readAll(descriptor, maximum: 65)
        guard bytes.count == 64, record.detachedSignatureSHA256.value == CanonicalJSON.sha256(bytes) else {
            throw EvidenceError.invalidValue
        }
        return bytes
    }

    private static func canonicalGenerationNames(_ names: Set<String>) throws -> [(Int64, String)] {
        guard !names.isEmpty else { throw EvidenceError.invalidValue }
        var entries: [(Int64, String)] = []
        for name in names {
            guard name.hasPrefix("generation-") else { throw EvidenceError.invalidValue }
            let suffix = String(name.dropFirst("generation-".count))
            guard !suffix.isEmpty, suffix.first != "0", suffix.allSatisfy({ $0.isASCII && $0.isNumber }),
                  let value = Int64(suffix), value > 0, name == generationName(value) else {
                throw EvidenceError.invalidValue
            }
            entries.append((value, name))
        }
        entries.sort { $0.0 < $1.0 }
        guard entries.first?.0 == 1,
              zip(entries, entries.dropFirst()).allSatisfy({ $0.1.0 == $0.0.0 + 1 }) else {
            throw EvidenceError.invalidValue
        }
        return entries
    }

    private static func generationName(_ generation: Int64) -> String { "generation-\(generation)" }

    private static func openedDirectory(_ parent: HeldDescriptor, _ name: String) throws -> HeldDescriptor {
        let descriptor = try POSIX.openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY)
        return try descriptor.rebindSameIdentity(in: parent, name: name, directory: true)
    }

    private static func openedRegular(_ parent: HeldDescriptor, _ name: String) throws -> HeldDescriptor {
        let descriptor = try POSIX.openAt(parent, name: name, flags: O_RDONLY)
        return try descriptor.rebindSameIdentity(in: parent, name: name)
    }

    private static func requireMutableRegistry(_ descriptor: HeldDescriptor, strictMetadata: Bool) throws {
        guard strictMetadata else { return }
        try POSIX.requireDirectory(descriptor, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireNoUnexpectedExtendedAttributes(descriptor)
        var status = stat(); try POSIX.fstatRetry(descriptor.fd, &status)
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw POSIXFailure.unsafeObject }
    }

    private static func overwritePrivateBytes(_ bytes: inout [UInt8]) {
        bytes.withUnsafeMutableBufferPointer { buffer in
            guard let base = buffer.baseAddress else { return }
            base.initialize(repeating: 0, count: buffer.count)
        }
#if DEBUG
        zeroizationObservation?(bytes.count, bytes.allSatisfy { $0 == 0 })
#endif
    }
}

enum ReceiptKeyAuthority {
    /// Verification has no persistence dependency and no mutation surface. Key A is solely an
    /// approval anchor; using it as a receipt key is fail-closed role confusion.
    static func validate(receipt: ReceiptV2, keyring: [KeyringRecord], receiptPublicKeySHA256: SHA256Digest,
                         bootstrapKeyAPublicKeySHA256: SHA256Digest) throws {
        guard receiptPublicKeySHA256 != bootstrapKeyAPublicKeySHA256 else { throw EvidenceError.invalidValue }
        try KeyringRecord.validateContiguous(keyring)
        guard keyring.last?.generation == Int64(keyring.count), keyring.last?.publicKeySHA256 == receiptPublicKeySHA256 else { throw EvidenceError.invalidValue }
        _ = receipt
    }

    static func validateRevocations(_ revocations: [KeyringRecord], receiptPublicKeySHA256: SHA256Digest,
                                    keyring: [KeyringRecord]) throws {
        guard let last = keyring.last else { throw EvidenceError.invalidValue }
        for revocation in revocations {
            guard revocation.variant == .revocationV2, revocation.keyID == last.keyID,
                  revocation.generation == last.generation, revocation.publicKeySHA256 == receiptPublicKeySHA256,
                  revocation.previousRecordSHA256 == last.sha256 else { throw EvidenceError.invalidValue }
            throw EvidenceError.invalidValue
        }
    }
}

enum Verify {
#if DEBUG
    // Deterministic race seams are intentionally confined to the test binary.  They execute
    // only after the authority was validated and immediately before the retained graph is
    // rebound, making stale-parent and late-registry swaps observable.
    static var afterAuthorityVerificationHook: (() throws -> Void)?
    static var afterKeyringVerificationHook: (() throws -> Void)?
    static var beforeFinalRebindHook: (() throws -> Void)?
#endif
    static func active(receipt: ReceiptV2, keyring: [KeyringRecord], revocations: [KeyringRecord],
                       receiptPublicKeySHA256: SHA256Digest, bootstrapKeyAPublicKeySHA256: SHA256Digest) throws {
        try ReceiptKeyAuthority.validate(receipt: receipt, keyring: keyring,
                                         receiptPublicKeySHA256: receiptPublicKeySHA256,
                                         bootstrapKeyAPublicKeySHA256: bootstrapKeyAPublicKeySHA256)
        try ReceiptKeyAuthority.validateRevocations(revocations, receiptPublicKeySHA256: receiptPublicKeySHA256, keyring: keyring)
    }

    static func active() throws {
        let root = try POSIX.openTrustedRootOwnedDirectory(ProtectedExecutionConstants.productionRoot)
        try verify(root: root, strictMetadata: true, keyring: { try KeyringVerifier.verifyReadOnly(root: root, strictMetadata: true) })
    }

#if DEBUG
    /// DEBUG verification authority binds a held disposable root to the independently expected
    /// Key-A digest.  It deliberately cannot be created from an arbitrary root alone, avoiding
    /// a weaker test verifier that merely trusts whatever Key-A happens to be installed there.
    struct VerificationAuthority {
        fileprivate let root: HeldDescriptor
        fileprivate let expectedKeyAPublicSHA256: SHA256Digest

        static func makeForTest(root: HeldDescriptor, expectedKeyAPublicSHA256: SHA256Digest) -> VerificationAuthority {
            VerificationAuthority(root: root, expectedKeyAPublicSHA256: expectedKeyAPublicSHA256)
        }
    }

    /// This symbol is compiled out of release builds; only immutable-flag readback is relaxed
    /// because ordinary test processes cannot set SF_IMMUTABLE.
    static func activeForTest(authority: VerificationAuthority) throws {
        try verify(root: authority.root, strictMetadata: false,
                   expectedKeyADigest: authority.expectedKeyAPublicSHA256,
                   keyring: { try KeyringVerifier.verifyReadOnlyForTestRoot(authority.root) })
    }
#endif

    private static func verify(root: HeldDescriptor, strictMetadata: Bool,
                               expectedKeyADigest: SHA256Digest = try! SHA256Digest(ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256),
                               keyring: () throws -> VerifiedKeyring) throws {
        let initialRoot = try POSIX.directoryEntryNames(root)
        guard initialRoot == Set(["bootstrap", "control", "materialization-work", "quarantine", "authorities"]) else {
            throw EvidenceError.invalidValue
        }
        let bootstrap = try openedDirectory(root, "bootstrap")
        let approval = try openedRegular(bootstrap, "task14-approval-v2.json")
        let approvalSignature = try openedRegular(bootstrap, "task14-approval-v2.ed25519")
        let approvalBytes = try POSIX.readAll(approval)
        let approvalSignatureBytes = try POSIX.readAll(approvalSignature, maximum: 65)
        try verifyFixedBootstrap(bootstrap, strictMetadata: strictMetadata)
        let verified = try keyring()
#if DEBUG
        // Deliberately after the original public chain is accepted and before any authority
        // object is consumed, exposing stale-registry replacement races to fixture tests.
        try afterKeyringVerificationHook?()
#endif
        let keyA = verified.keyA
        guard keyA.digest == expectedKeyADigest,
              approvalSignatureBytes.count == 64,
              DarwinBootstrapAdapters.isValidApprovalSignature(verificationPublicKey: keyA.bytes,
                                                                approval: approvalBytes,
                                                                signature: approvalSignatureBytes) else {
            throw EvidenceError.invalidValue
        }
        let approved = try Task14ApprovalV2.decodeDocument(approvalBytes)
        if strictMetadata {
            try bootstrap.requireSealedDirectory(mode: 0o555)
            try approval.requireSealedRegular(mode: 0o444)
            try approvalSignature.requireSealedRegular(mode: 0o444)
        }
        _ = try approval.rebindSameIdentity(in: bootstrap, name: "task14-approval-v2.json")
        _ = try approvalSignature.rebindSameIdentity(in: bootstrap, name: "task14-approval-v2.ed25519")

        let control = try openedDirectory(root, "control")
        let admissions = try openedDirectory(control, "admissions")
        let journals = try openedDirectory(control, "journals")
        let work = try openedDirectory(root, "materialization-work")
        let quarantine = try openedDirectory(root, "quarantine")
        let authorities = try openedDirectory(root, "authorities")
        let heldRootChildren = ["bootstrap": bootstrap, "control": control,
                                "materialization-work": work, "quarantine": quarantine,
                                "authorities": authorities]
        try requireOpenRegistry(control, strictMetadata: strictMetadata)
        try requireOpenRegistry(admissions, strictMetadata: strictMetadata)
        try requireOpenRegistry(journals, strictMetadata: strictMetadata)
        try requireOpenRegistry(work, strictMetadata: strictMetadata)
        try requireOpenRegistry(quarantine, strictMetadata: strictMetadata)
        try requireOpenRegistry(authorities, strictMetadata: strictMetadata)
        let controlSnapshot = try POSIX.directoryEntryNames(control)
        let admissionsSnapshot = try POSIX.directoryEntryNames(admissions)
        let journalsSnapshot = try POSIX.directoryEntryNames(journals)
        let authoritiesSnapshot = try POSIX.directoryEntryNames(authorities)
        guard controlSnapshot == Set(["keyring", "revocations", "admissions", "journals"]) else {
            throw EvidenceError.invalidValue
        }
        guard try POSIX.directoryEntryNames(work).isEmpty, try POSIX.directoryEntryNames(quarantine).isEmpty,
              try POSIX.directoryEntryNames(authorities) == [ProtectedExecutionConstants.authorityID] else {
            throw EvidenceError.invalidValue
        }
        let authority = try openedDirectory(authorities, ProtectedExecutionConstants.authorityID)
        let graph = try PublishedAuthorityGraph.verifyReadOnlyGraph(root: authority, strictMetadata: strictMetadata)
        let receiptFile = try openedRegular(authority, "receipt.json")
        let receiptSignatureFile = try openedRegular(authority, "receipt.ed25519")
        let receiptBytes = try POSIX.readAll(receiptFile)
        let receiptSignature = try POSIX.readAll(receiptSignatureFile, maximum: 65)
        guard receiptSignature.count == 64,
              try Curve25519.Signing.PublicKey(rawRepresentation: Data(verified.publicKeyBytes))
                .isValidSignature(Data(receiptSignature), for: Data(receiptBytes)) else { throw EvidenceError.invalidValue }
        let receipt = try ReceiptV2.decode(String(decoding: receiptBytes, as: UTF8.self))
        try active(receipt: receipt, keyring: verified.records, revocations: [],
                   receiptPublicKeySHA256: verified.publicKeySHA256,
                   bootstrapKeyAPublicKeySHA256: keyA.digest)
        guard receipt.keyGeneration == verified.generation,
              receipt.publicKeySHA256 == verified.publicKeySHA256,
              receipt.task14ApprovalSHA256 == approved.sha256 else { throw EvidenceError.invalidValue }

        let admissionFile = try openedRegular(authority, "admission.json")
        let locatorFile = try openedRegular(authority, "admission-locator-v2.json")
        let admissionBytes = try POSIX.readAll(admissionFile)
        let admission = try AdmissionRecord.decode(String(decoding: admissionBytes, as: UTF8.self))
        let admissionFileSHA256 = try SHA256Digest(CanonicalJSON.sha256(admissionBytes))
        let locatorBytes = try POSIX.readAll(locatorFile)
        let locator = try AdmissionLocatorV2.decode(locatorBytes)
        guard try locator.canonicalRecordBytes() == locatorBytes,
              locator.admissionRecordSHA256.value == CanonicalJSON.sha256(admissionBytes),
              locator.registryRelativePath == "control/admissions/" + locator.attemptUUID + ".json",
              admissionFileSHA256 == receipt.admissionRecordSHA256,
              try POSIX.directoryEntryNames(admissions) == [locator.attemptUUID + ".json"] else {
            throw EvidenceError.invalidValue
        }
        let externalAdmission = try openedRegular(admissions, locator.attemptUUID + ".json")
        if strictMetadata { try externalAdmission.requireSealedRegular(mode: 0o444) }
        guard try POSIX.readAll(externalAdmission) == admissionBytes else { throw EvidenceError.invalidValue }

        let runtime = try RuntimeManifestV2.decode(readUTF8(authority, "runtime-manifest-v2.json"),
                                                   policy: MaterializationPolicy.decode(readUTF8(bootstrap, "materialization-policy.json")))
        let manifest = try AuthorityManifestV2.decode(readUTF8(authority, "manifest-v2.json"))
        try V2Evidence.crossValidateAuthority(approved, admission, admissionFileSHA256, runtime, manifest, receipt,
                                              publishedTree: PublishedAuthorityTree(rows: graph.closureRows))
        try verifyPublishedJournal(journals: journals, attempt: locator.attemptUUID, admissionFileSHA256: admissionFileSHA256,
                                   receipt: receipt, authority: authority, graph: graph, strictMetadata: strictMetadata)
#if DEBUG
        try afterAuthorityVerificationHook?()
        try beforeFinalRebindHook?()
#endif

        // A final root-level pass is deliberately wider than the initial traversal: every
        // retained direct child must still be exactly the object first observed, so a swap of
        // bootstrap/work/quarantine/authorities cannot hide behind an earlier successful read.
        for (name, held) in heldRootChildren {
            let rebound = try held.rebindSameIdentity(in: root, name: name, directory: true)
            if name == "bootstrap", strictMetadata { try rebound.requireSealedDirectory(mode: 0o555) }
            else { try requireOpenRegistry(rebound, strictMetadata: strictMetadata) }
        }
        let finalControl = try control.rebindSameIdentity(in: root, name: "control", directory: true)
        for (name, held) in [("keyring", try openedDirectory(control, "keyring")),
                             ("revocations", try openedDirectory(control, "revocations")),
                             ("admissions", admissions), ("journals", journals)] {
            _ = try held.rebindSameIdentity(in: finalControl, name: name, directory: true)
        }
        let finalAuthorities = try authorities.rebindSameIdentity(in: root, name: "authorities", directory: true)
        let finalAuthority = try authority.rebindSameIdentity(in: finalAuthorities, name: ProtectedExecutionConstants.authorityID, directory: true)
        _ = try PublishedAuthorityGraph.verifyReadOnlyGraph(root: finalAuthority, strictMetadata: strictMetadata)
        try KeyringVerifier.revalidateRetained(verified, from: root, strictMetadata: strictMetadata)
        // A post-hook journal pass prevents the first successful journal read from becoming
        // authority for a same-name attempt-directory replacement.
        try verifyPublishedJournal(journals: journals, attempt: locator.attemptUUID,
                                   admissionFileSHA256: admissionFileSHA256, receipt: receipt,
                                   authority: finalAuthority, graph: graph, strictMetadata: strictMetadata)
        guard try POSIX.directoryEntryNames(root) == initialRoot,
              try POSIX.directoryEntryNames(control) == controlSnapshot,
              try POSIX.directoryEntryNames(admissions) == admissionsSnapshot,
              try POSIX.directoryEntryNames(journals) == journalsSnapshot,
              try POSIX.directoryEntryNames(authorities) == authoritiesSnapshot else { throw POSIXFailure.unsafeObject }
    }

    private static func verifyFixedBootstrap(_ bootstrap: HeldDescriptor, strictMetadata: Bool) throws {
        let expected = Set(["task14-approval-v2.json", "task14-approval-v2.ed25519",
                            "task14-approval-verification-public-key", "task14-approval-v2.sha256",
                            "policy.json", "materialization-policy.json", "provisioner",
                            "provisioner.codesign-requirement.txt"])
        guard try POSIX.directoryEntryNames(bootstrap) == expected else { throw EvidenceError.invalidValue }
        for name in expected {
            let file = try openedRegular(bootstrap, name)
            if strictMetadata { try file.requireSealedRegular(mode: 0o444) }
            else {
                let metadata = try POSIX.metadata(file)
                guard metadata.mode == 0o444, metadata.nlink == 1 else { throw POSIXFailure.unsafeObject }
                try POSIX.requireNoExtendedACL(file)
                try POSIX.requireNoUnexpectedExtendedAttributes(file)
            }
            _ = try POSIX.readAll(file)
            _ = try file.rebindSameIdentity(in: bootstrap, name: name)
        }
    }

    private static func requireOpenRegistry(_ descriptor: HeldDescriptor, strictMetadata: Bool) throws {
        if strictMetadata {
            try POSIX.requireDirectory(descriptor, uid: 0, gid: 0, mode: 0o700)
        } else {
            // Test roots cannot be root:wheel, but they must still exercise every other
            // registry invariant.  In particular this prevents DEBUG verification from
            // accepting a sealed or permission-drifted journal attempt.
            let metadata = try POSIX.metadata(descriptor)
            guard metadata.type == S_IFDIR, metadata.mode == 0o700 else {
                throw POSIXFailure.unsafeObject
            }
            try POSIX.requireNoExtendedACL(descriptor)
        }
        try POSIX.requireNoUnexpectedExtendedAttributes(descriptor)
        var status = stat(); try POSIX.fstatRetry(descriptor.fd, &status)
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw POSIXFailure.unsafeObject }
    }

    private static func verifyPublishedJournal(journals: HeldDescriptor, attempt: String, admissionFileSHA256: SHA256Digest,
                                               receipt: ReceiptV2, authority: HeldDescriptor,
                                               graph: PublishedAuthorityGraph, strictMetadata: Bool) throws {
        let journalNames = try POSIX.directoryEntryNames(journals)
        guard journalNames == [attempt] else { throw EvidenceError.invalidValue }
        let journal = try openedDirectory(journals, attempt)
        guard attempt.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }
        try requireOpenRegistry(journal, strictMetadata: strictMetadata)
        let expected = ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json", "0004-published.json"]
        let files = try POSIX.directoryEntryNames(journal)
        guard files == Set(expected) else { throw EvidenceError.invalidValue }
        var records: [JournalRecordV2] = []
        for name in expected {
            let file = try openedRegular(journal, name)
            if strictMetadata { try file.requireSealedRegular(mode: 0o444) }
            let bytes = try POSIX.readAll(file)
            let record = try JournalRecordV2.decode(bytes)
            guard record.filename == name, try record.canonicalRecordBytes() == bytes,
                  record.attemptUUID == attempt, record.admissionRecordSHA256 == admissionFileSHA256 else {
                throw EvidenceError.invalidValue
            }
            records.append(record)
            _ = try file.rebindSameIdentity(in: journal, name: name)
        }
        guard records.map(\.phase) == [.stageCreated, .presealed, .renameIntent, .published],
              let final = records.last,
              final.receiptFileSHA256 == ReceiptV2.receiptFileSHA256(try POSIX.readAll(try openedRegular(authority, "receipt.json"))),
              final.publishedClosureSHA256 == graph.closureSHA256,
              final.publishedDevice == Int64(authority.device), final.publishedInode == Int64(authority.inode) else {
            throw EvidenceError.invalidValue
        }
        _ = receipt
        _ = try journal.rebindSameIdentity(in: journals, name: attempt, directory: true)
        guard try POSIX.directoryEntryNames(journal) == files,
              try POSIX.directoryEntryNames(journals) == journalNames else { throw POSIXFailure.unsafeObject }
    }

    private static func openedDirectory(_ parent: HeldDescriptor, _ name: String) throws -> HeldDescriptor {
        try POSIX.openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY)
            .rebindSameIdentity(in: parent, name: name, directory: true)
    }

    private static func openedRegular(_ parent: HeldDescriptor, _ name: String) throws -> HeldDescriptor {
        try POSIX.openAt(parent, name: name, flags: O_RDONLY)
            .rebindSameIdentity(in: parent, name: name)
    }

    private static func readUTF8(_ parent: HeldDescriptor, _ name: String) throws -> String {
        String(decoding: try POSIX.readAll(try openedRegular(parent, name)), as: UTF8.self)
    }
}
