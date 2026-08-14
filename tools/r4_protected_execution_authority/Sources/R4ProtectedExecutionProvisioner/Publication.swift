import Darwin
import CryptoKit
import Foundation

/// The complete, descriptor-derived authority tree that a later task signs and publishes.
/// It deliberately contains no signing, sealing, rename, or pathname capability.
struct PublishedAuthorityGraph: Equatable {
    static let requiredChildNames = [
        "admission-locator-v2.json", "admission.json", "checkout", "manifest-v2.json",
        "receipt.ed25519", "receipt.json", "runtime", "runtime-manifest-v2.json",
    ]

    let childNames: [String]
    let closureRows: [ManifestEntry]
    let closureSHA256: SHA256Digest

    /// A caller holding only an admission cannot manufacture publication evidence.  Task 9 must
    /// retain the exclusive-write results returned by `installAdmissionArtifacts` and pass them
    /// to the capability-taking overload below.
    static func build(stage: HeldMaterializationStage, admission: Admission) throws -> PublishedAuthorityGraph {
        _ = stage; _ = admission
        throw PublicationFailure.artifactsRequired
    }

    /// Builds the prospective closure from the two retained immutable artifacts. The receipt
    /// pair is present in the child graph but excluded from the closure to avoid its hash cycle.
    static func build(stage: HeldMaterializationStage, artifacts: PublishedAdmissionArtifacts) throws -> PublishedAuthorityGraph {
        let root = try reboundProductionStageRoot(stage)
        return try build(root: root, owner: stage.destinationOwner, artifacts: artifacts,
                         verifySealed: { try $0.requireSealedRegular(mode: 0o444) }, seam: nil,
                         finalRoot: { try reboundProductionStageRoot(stage) })
    }

    /// The receipt pair is deliberately absent while its signed content is being derived: the
    /// closure excludes those two names and therefore has no receipt hash cycle.  This is a
    /// narrower capability than `build`; callers must still rebuild the complete eight-child
    /// graph after both exclusive receipt writes succeed.
    static func buildProspective(stage: HeldMaterializationStage,
                                 artifacts: PublishedAdmissionArtifacts) throws -> PublishedAuthorityGraph {
        let root = try reboundProductionStageRoot(stage)
        return try build(root: root, owner: stage.destinationOwner, artifacts: artifacts,
                         verifySealed: { try $0.requireSealedRegular(mode: 0o444) }, seam: nil,
                         requiredNames: Set(requiredChildNames).subtracting(["receipt.json", "receipt.ed25519"]),
                         finalRoot: { try reboundProductionStageRoot(stage) })
    }

    #if DEBUG
    /// Structural test route only. Production must retain the admission artifacts capability.
    static func build(root: HeldDescriptor) throws -> PublishedAuthorityGraph {
        try build(root: root, owner: nil, artifacts: nil, verifySealed: { _ in }, seam: nil, finalRoot: { root })
    }
    #endif

    private static func build(root: HeldDescriptor, owner: DestinationOwner?, artifacts: PublishedAdmissionArtifacts?,
                              verifySealed: (HeldDescriptor) throws -> Void,
                              seam: ((PublicationGraphSeam) throws -> Void)?,
                              requiredNames: Set<String> = Set(requiredChildNames),
                              finalRoot: () throws -> HeldDescriptor) throws -> PublishedAuthorityGraph {
        let initialChildNames = try POSIX.directoryEntryNames(root)
        guard initialChildNames == requiredNames else {
            throw PublicationFailure.invalidGraph
        }
        try requireCasefoldUnique(initialChildNames)
        if let artifacts { try validateArtifacts(artifacts, in: root, verifySealed: verifySealed) }
        try seam?(.afterArtifactValidation)

        var rows: [ManifestEntry] = []
        for name in requiredChildNames where requiredNames.contains(name) {
            let directory = name == "checkout" || name == "runtime"
            let rebound: HeldDescriptor
            if let artifacts, name == "admission.json" { rebound = artifacts.admission.file }
            else if let artifacts, name == "admission-locator-v2.json" { rebound = artifacts.locator.file }
            else {
                let held = try POSIX.openAt(root, name: name, flags: O_RDONLY | (directory ? O_DIRECTORY : 0))
                rebound = try held.rebindSameIdentity(in: root, name: name, directory: directory)
            }
            try requirePrivateObject(rebound, directory: directory, owner: owner)
            if name == "receipt.ed25519" {
                guard try POSIX.metadata(rebound).size == 64 else { throw PublicationFailure.invalidGraph }
                continue
            }
            if name == "receipt.json" { continue }
            rows += try recursiveRows(rebound, path: name, directory: directory, owner: owner)
        }
        rows.sort { $0.path < $1.path }
        try seam?(.afterRows)
        if let artifacts {
            let reboundRoot = try finalRoot()
            guard reboundRoot.device == root.device, reboundRoot.inode == root.inode,
                  try POSIX.directoryEntryNames(reboundRoot) == initialChildNames else { throw PublicationFailure.sourceChanged }
            try validateArtifacts(artifacts, in: reboundRoot, verifySealed: verifySealed)
        }
        let tree = try PublishedAuthorityTree.decode(.array(rows.map(rowValue)))
        return try PublishedAuthorityGraph(childNames: requiredChildNames, closureRows: tree.rows,
                                           closureSHA256: SHA256Digest(tree.sha256))
    }

    /// Copies the immutable held admission record and constructs its canonical closed locator.
    /// Both destination writes are exclusive, durable, immutable, rebound, and byte checked.
    static func installAdmissionArtifacts(into root: HeldDescriptor, admission: Admission) throws -> PublishedAdmissionArtifacts {
        let bytes = try admissionBytes(admission)

        let copied = try PublicationPOSIX.createImmutableExclusive(root, "admission.json", bytes, mode: 0o444)
        guard try POSIX.readAll(copied) == bytes,
              CanonicalJSON.sha256(try POSIX.readAll(copied)) == admission.recordSHA256.value else {
            throw PublicationFailure.sourceChanged
        }

        let locator = try locatorBytes(attempt: admission.attempt, admissionSHA256: admission.recordSHA256)
        let locatorFile = try PublicationPOSIX.createImmutableExclusive(root, "admission-locator-v2.json", locator, mode: 0o444)
        guard try POSIX.readAll(locatorFile) == locator else { throw PublicationFailure.sourceChanged }
        let decoded = try AdmissionLocatorV2.decode(locator)
        guard decoded.attemptUUID == admission.attempt,
              decoded.admissionRecordSHA256 == admission.recordSHA256 else { throw PublicationFailure.invalidGraph }
        try copied.requireSealedRegular(mode: 0o444)
        try locatorFile.requireSealedRegular(mode: 0o444)
        try POSIX.durableDirectory(root)
        return try PublishedAdmissionArtifacts(
            admission: HeldPublishedRegular(parent: root, name: "admission.json", file: copied, bytes: bytes),
            locator: HeldPublishedRegular(parent: root, name: "admission-locator-v2.json", file: locatorFile, bytes: locator)
        )
    }

#if DEBUG
    /// Test-only metadata adapter. The caller still supplies the held admission capability; raw
    /// admission bytes, locator rows, and paths never cross the preparation boundary.
    static func installAdmissionArtifactsForTest(into root: HeldDescriptor, admission: Admission,
                                                 operations: PublicationPOSIX.Operations) throws -> PublishedAdmissionArtifacts {
        let source = try admission.record.file.rebindSameIdentity(in: admission.record.parent,
                                                                   name: admission.record.name)
        let bytes = try POSIX.readAll(source)
        guard CanonicalJSON.sha256(bytes) == admission.recordSHA256.value else { throw PublicationFailure.sourceChanged }
        let copied = try PublicationPOSIX.createImmutableExclusive(root, "admission.json", bytes, mode: 0o444, operations: operations)
        let locator = try locatorBytes(attempt: admission.attempt, admissionSHA256: admission.recordSHA256)
        let locatorFile = try PublicationPOSIX.createImmutableExclusive(root, "admission-locator-v2.json", locator, mode: 0o444, operations: operations)
        guard try POSIX.readAll(copied) == bytes, try POSIX.readAll(locatorFile) == locator else { throw PublicationFailure.sourceChanged }
        try operations.verifySealed(copied, 0o444)
        try operations.verifySealed(locatorFile, 0o444)
        try operations.durableDirectory(root)
        return try PublishedAdmissionArtifacts(
            admission: HeldPublishedRegular(parent: root, name: "admission.json", file: copied, bytes: bytes),
            locator: HeldPublishedRegular(parent: root, name: "admission-locator-v2.json", file: locatorFile, bytes: locator)
        )
    }

    static func admissionLocatorBytesForTest(attempt: String, admissionSHA256: SHA256Digest) throws -> [UInt8] {
        try locatorBytes(attempt: attempt, admissionSHA256: admissionSHA256)
    }

    static func buildForTest(root: HeldDescriptor, stageParent: HeldDescriptor, stageName: String,
                             artifacts: PublishedAdmissionArtifacts,
                             verifySealed: @escaping (HeldDescriptor) throws -> Void,
                             seam: @escaping (PublicationGraphSeam) throws -> Void = { _ in }) throws -> PublishedAuthorityGraph {
        try build(root: root, owner: nil, artifacts: artifacts, verifySealed: verifySealed, seam: seam,
                  finalRoot: { try root.rebindSameIdentity(in: stageParent, name: stageName, directory: true) })
    }

    static func buildProspectiveForTest(stage: HeldMaterializationStage, artifacts: PublishedAdmissionArtifacts,
                                        verifySealed: @escaping (HeldDescriptor) throws -> Void) throws -> PublishedAuthorityGraph {
        try build(root: stage.root, owner: nil, artifacts: artifacts, verifySealed: verifySealed, seam: nil,
                  requiredNames: Set(requiredChildNames).subtracting(["receipt.json", "receipt.ed25519"]),
                  finalRoot: { stage.root })
    }

    static func buildForTest(stage: HeldMaterializationStage, artifacts: PublishedAdmissionArtifacts,
                             verifySealed: @escaping (HeldDescriptor) throws -> Void) throws -> PublishedAuthorityGraph {
        try build(root: stage.root, owner: nil, artifacts: artifacts, verifySealed: verifySealed, seam: nil,
                  finalRoot: { stage.root })
    }
    #endif

    private static func reboundProductionStageRoot(_ stage: HeldMaterializationStage) throws -> HeldDescriptor {
        guard AdmissionController.isAttemptUUID(stage.stageName) else { throw PublicationFailure.invalidGraph }
        let root = try stage.root.rebindSameIdentity(in: stage.work, name: stage.stageName, directory: true)
        try POSIX.requireDirectory(root, uid: 0, gid: 0, mode: 0o700)
        return root
    }

    private static func admissionBytes(_ admission: Admission) throws -> [UInt8] {
        let source = try admission.record.file.rebindSameIdentity(in: admission.record.parent,
                                                                   name: admission.record.name)
        try source.requireSealedRegular(mode: 0o444)
        let bytes = try POSIX.readAll(source)
        guard CanonicalJSON.sha256(bytes) == admission.recordSHA256.value else { throw PublicationFailure.sourceChanged }
        _ = try source.rebindSameIdentity(in: admission.record.parent, name: admission.record.name)
        return bytes
    }

    private static func validateArtifacts(_ artifacts: PublishedAdmissionArtifacts, in root: HeldDescriptor,
                                          verifySealed: (HeldDescriptor) throws -> Void) throws {
        let admission = try artifacts.admission.rebind(in: root)
        let locator = try artifacts.locator.rebind(in: root)
        try verifySealed(admission)
        try verifySealed(locator)
        guard try POSIX.readAll(admission) == artifacts.admission.bytes,
              CanonicalJSON.sha256(try POSIX.readAll(admission)) == artifacts.admission.sha256.value,
              try POSIX.readAll(locator) == artifacts.locator.bytes,
              CanonicalJSON.sha256(try POSIX.readAll(locator)) == artifacts.locator.sha256.value else {
            throw PublicationFailure.sourceChanged
        }
        let decoded = try AdmissionLocatorV2.decode(artifacts.locator.bytes)
        guard decoded.admissionRecordSHA256 == artifacts.admission.sha256,
              decoded.registryRelativePath == "control/admissions/" + decoded.attemptUUID + ".json" else {
            throw PublicationFailure.sourceChanged
        }
    }

    private static func recursiveRows(_ held: HeldDescriptor, path: String, directory: Bool, owner: DestinationOwner?) throws -> [ManifestEntry] {
        if !directory {
            let metadata = try POSIX.metadata(held)
            let mode = (metadata.mode & 0o111) == 0 ? "0444" : "0555"
            return [try ManifestEntry(path: path, type: "regular", mode: mode, size: metadata.size,
                                      sha256: SHA256Digest(CanonicalJSON.sha256(try POSIX.readAll(held))))]
        }
        var descendants: [ManifestEntry] = []
        let names = try POSIX.directoryEntryNames(held).sorted()
        try requireCasefoldUnique(names)
        for name in names {
            let child = try POSIX.openAt(held, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(child)
            let childDirectory = metadata.type == S_IFDIR
            guard childDirectory || metadata.type == S_IFREG else { throw PublicationFailure.invalidGraph }
            let rebound = try child.rebindSameIdentity(in: held, name: name, directory: childDirectory)
            try requirePrivateObject(rebound, directory: childDirectory, owner: owner)
            descendants += try recursiveRows(rebound, path: path + "/" + name, directory: childDirectory, owner: owner)
        }
        let digest = try directoryDigest(descendants, parent: path)
        let row = try ManifestEntry(path: path, type: "directory", mode: "0555", size: 0, sha256: SHA256Digest(digest))
        return [row] + descendants
    }

    private static func requirePrivateObject(_ held: HeldDescriptor, directory: Bool, owner: DestinationOwner?) throws {
        let metadata = try POSIX.metadata(held)
        guard metadata.type == (directory ? S_IFDIR : S_IFREG),
              (directory || metadata.nlink == 1),
              metadata.nlink >= 1 else { throw PublicationFailure.invalidMetadata }
        if let owner, (metadata.uid != owner.uid || metadata.gid != owner.gid) { throw PublicationFailure.invalidMetadata }
        guard (metadata.mode & 0o022) == 0 else { throw PublicationFailure.invalidGraph }
        try POSIX.requireNoExtendedACL(held)
        try POSIX.requireNoUnexpectedExtendedAttributes(held)
    }

    private static func directoryDigest(_ descendants: [ManifestEntry], parent: String) throws -> String {
        let prefix = parent + "/"
        let rows = descendants.compactMap { row -> JSONValue? in
            guard row.path.hasPrefix(prefix) else { return nil }
            let name = String(row.path.dropFirst(prefix.count))
            guard !name.contains("/") else { return nil }
            return .object([("name", .string(name)), ("type", .string(row.type)), ("mode", .string(row.mode)),
                            ("size", .integer(row.size)), ("sha256", .string(row.sha256.value))])
        }.sorted { try! CanonicalJSON.encode($0).lexicographicallyPrecedes(try! CanonicalJSON.encode($1)) }
        return try CanonicalJSON.domainSHA256("R4DIR\0", .array(rows))
    }

    private static func rowValue(_ row: ManifestEntry) -> JSONValue {
        .object([("path", .string(row.path)), ("type", .string(row.type)), ("mode", .string(row.mode)),
                 ("size", .integer(row.size)), ("sha256", .string(row.sha256.value))])
    }

    private static func locatorBytes(attempt: String, admissionSHA256: SHA256Digest) throws -> [UInt8] {
        let fields: [(String, JSONValue)] = [
            ("admission_record_sha256", .string(admissionSHA256.value)), ("attempt_uuid", .string(attempt)),
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2")),
            ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("registry_relative_path", .string("control/admissions/" + attempt + ".json")),
        ]
        let core = try CanonicalJSON.encode(.object(fields))
        let locator = CanonicalJSON.sha256(Array("R4ADMISSIONLOCATOR2\0".utf8) + core)
        let withLocator = fields + [("locator_sha256", .string(locator))]
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(withLocator)))
        return try CanonicalJSON.encode(.object(withLocator + [("sha256", .string(digest))]))
    }

    static func requireCasefoldUnique(_ names: some Sequence<String>) throws {
        var seen: Set<String> = []
        for name in names {
            guard seen.insert(name.lowercased()).inserted else { throw PublicationFailure.invalidGraph }
        }
    }
}

enum PublicationFailure: Error, Equatable { case invalidGraph, invalidMetadata, sourceChanged, artifactsRequired }
enum PublicationGraphSeam { case afterArtifactValidation, afterRows }

extension PublishedAuthorityGraph {
    /// Read-only receiver-side reconstruction of the closed authority graph.  It has no
    /// retained-write inputs and never creates, seals, adopts, or repairs an object.  The
    /// caller supplies the already-held authority descriptor and decides whether immutable
    /// kernel metadata is available (DEBUG fixtures cannot set SF_IMMUTABLE unprivileged).
    static func verifyReadOnlyGraph(root: HeldDescriptor, strictMetadata: Bool) throws -> PublishedAuthorityGraph {
        let rootNames = try POSIX.directoryEntryNames(root)
        guard rootNames == Set(requiredChildNames) else { throw PublicationFailure.invalidGraph }
        try requireCasefoldUnique(rootNames)
        if strictMetadata { try root.requireSealedDirectory(mode: 0o555) }
        for name in requiredChildNames {
            let directory = name == "checkout" || name == "runtime"
            let held = try POSIX.openAt(root, name: name, flags: O_RDONLY | (directory ? O_DIRECTORY : 0))
            let rebound = try held.rebindSameIdentity(in: root, name: name, directory: directory)
            let metadata = try POSIX.metadata(rebound)
            guard metadata.type == (directory ? S_IFDIR : S_IFREG), directory || metadata.nlink == 1 else {
                throw PublicationFailure.invalidMetadata
            }
            if strictMetadata {
                if directory { try rebound.requireSealedDirectory(mode: 0o555) }
                else { try rebound.requireSealedRegular(mode: 0o444) }
            } else {
                guard metadata.mode == (directory ? 0o555 : 0o444) else { throw PublicationFailure.invalidMetadata }
                try POSIX.requireNoExtendedACL(rebound)
                try POSIX.requireNoUnexpectedExtendedAttributes(rebound)
            }
        }
        try verifyReadOnlyDescendants(root, strictMetadata: strictMetadata)
        let graph = try build(root: root, owner: nil, artifacts: nil,
                              verifySealed: { _ in }, seam: nil, finalRoot: { root })
        // A second root snapshot plus every child rebind is the final no-adoption evidence;
        // root itself is rebound by the caller against the held authorities registry.
        guard try POSIX.directoryEntryNames(root) == rootNames else { throw PublicationFailure.sourceChanged }
        return graph
    }

    private static func verifyReadOnlyDescendants(_ directory: HeldDescriptor, strictMetadata: Bool) throws {
        let names = try POSIX.directoryEntryNames(directory)
        try requireCasefoldUnique(names)
        for name in names {
            let held = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(held)
            let isDirectory = metadata.type == S_IFDIR
            guard isDirectory || metadata.type == S_IFREG, isDirectory || metadata.nlink == 1 else {
                throw PublicationFailure.invalidMetadata
            }
            let rebound = try held.rebindSameIdentity(in: directory, name: name, directory: isDirectory)
            if strictMetadata {
                if isDirectory { try rebound.requireSealedDirectory(mode: 0o555) }
                else { try rebound.requireSealedRegular(mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555) }
            } else {
                guard metadata.mode == (isDirectory ? 0o555 : ((metadata.mode & 0o111) == 0 ? 0o444 : 0o555)) else {
                    throw PublicationFailure.invalidMetadata
                }
                try POSIX.requireNoExtendedACL(rebound)
                try POSIX.requireNoUnexpectedExtendedAttributes(rebound)
            }
            if isDirectory { try verifyReadOnlyDescendants(rebound, strictMetadata: strictMetadata) }
        }
    }

    /// Final verification deliberately rebuilds the complete Task 8/9 graph after the stage has
    /// its final authority name. It validates the retained receipt pair and signer binding too;
    /// publication never substitutes a weaker second verifier.
    static func verifyPublishedFinal(root: HeldDescriptor, prepared: PreparedPublication) throws {
        try root.requireSealedDirectory(mode: 0o555)
        try verifySealedDescendants(root)
        let final = try build(root: root, owner: prepared.stage.destinationOwner, artifacts: nil,
                              verifySealed: { descriptor in
                                  let metadata = try POSIX.metadata(descriptor)
                                  if metadata.type == S_IFDIR { try descriptor.requireSealedDirectory(mode: 0o555) }
                                  else { try descriptor.requireSealedRegular(mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555) }
                              }, seam: nil, finalRoot: { root })
        guard final.childNames == prepared.graph.childNames,
              final.closureRows == prepared.graph.closureRows,
              final.closureSHA256 == prepared.graph.closureSHA256 else { throw PublicationFailure.sourceChanged }
        let receipt = try POSIX.openAt(root, name: "receipt.json", flags: O_RDONLY)
        let signature = try POSIX.openAt(root, name: "receipt.ed25519", flags: O_RDONLY)
        _ = try receipt.rebindSameIdentity(in: root, name: "receipt.json")
        _ = try signature.rebindSameIdentity(in: root, name: "receipt.ed25519")
        try receipt.requireSealedRegular(mode: 0o444)
        try signature.requireSealedRegular(mode: 0o444)
        let bytes = try POSIX.readAll(receipt)
        let signatureBytes = try POSIX.readAll(signature, maximum: 65)
        guard bytes == prepared.receiptBytes,
              ReceiptV2.receiptFileSHA256(bytes) == prepared.receiptFileSHA256,
              signatureBytes == prepared.receiptArtifacts.signatureBytes,
              signatureBytes.count == 64,
              prepared.signerPublicKeyBytes.count == 32 else { throw PublicationFailure.sourceChanged }
        let decoded = try ReceiptV2.decode(String(decoding: bytes, as: UTF8.self))
        let signerDigest = try SHA256Digest(CanonicalJSON.sha256(prepared.signerPublicKeyBytes))
        guard decoded.publicKeySHA256 == signerDigest else {
            throw PublicationFailure.sourceChanged
        }
        let publicKey = try Curve25519.Signing.PublicKey(rawRepresentation: Data(prepared.signerPublicKeyBytes))
        guard publicKey.isValidSignature(Data(signatureBytes), for: Data(bytes)) else { throw PublicationFailure.sourceChanged }
    }

    private static func verifySealedDescendants(_ directory: HeldDescriptor) throws {
        let names = try POSIX.directoryEntryNames(directory)
        try requireCasefoldUnique(names)
        for name in names {
            let held = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(held)
            let isDirectory = metadata.type == S_IFDIR
            guard isDirectory || metadata.type == S_IFREG else { throw PublicationFailure.invalidMetadata }
            let rebound = try held.rebindSameIdentity(in: directory, name: name, directory: isDirectory)
            if isDirectory {
                try rebound.requireSealedDirectory(mode: 0o555)
                try verifySealedDescendants(rebound)
            } else {
                try rebound.requireSealedRegular(mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555)
            }
        }
    }

#if DEBUG
    /// Disposable fixtures cannot set SF_IMMUTABLE as an unprivileged process, but they still
    /// exercise the real descriptor identity, type, owner, mode, ACL/xattr, graph, receipt and
    /// signature checks. Synthetic immutable state remains an explicit fixture seam at preseal.
    static func verifyPublishedFixtureFinal(root: HeldDescriptor, prepared: PreparedPublication,
                                            syntheticImmutable: (HeldDescriptor, Bool) throws -> Void) throws {
        guard root.device == prepared.stage.root.device, root.inode == prepared.stage.root.inode else {
            throw PublicationFailure.sourceChanged
        }
        try verifyFixtureTree(root, owner: prepared.stage.destinationOwner, root: true,
                              syntheticImmutable: syntheticImmutable)
        let rebuilt = try build(root: root)
        guard rebuilt.childNames == prepared.graph.childNames,
              rebuilt.closureRows == prepared.graph.closureRows,
              rebuilt.closureSHA256 == prepared.graph.closureSHA256 else { throw PublicationFailure.sourceChanged }
        let receipt = try POSIX.openAt(root, name: "receipt.json", flags: O_RDONLY)
        let signature = try POSIX.openAt(root, name: "receipt.ed25519", flags: O_RDONLY)
        _ = try receipt.rebindSameIdentity(in: root, name: "receipt.json")
        _ = try signature.rebindSameIdentity(in: root, name: "receipt.ed25519")
        let bytes = try POSIX.readAll(receipt), signatureBytes = try POSIX.readAll(signature, maximum: 65)
        guard bytes == prepared.receiptBytes, signatureBytes == prepared.receiptArtifacts.signatureBytes,
              ReceiptV2.receiptFileSHA256(bytes) == prepared.receiptFileSHA256,
              prepared.signerPublicKeyBytes.count == 32 else { throw PublicationFailure.sourceChanged }
        let key = try Curve25519.Signing.PublicKey(rawRepresentation: Data(prepared.signerPublicKeyBytes))
        guard key.isValidSignature(Data(signatureBytes), for: Data(bytes)) else { throw PublicationFailure.sourceChanged }
    }

    private static func verifyFixtureTree(_ directory: HeldDescriptor, owner: DestinationOwner, root: Bool = false,
                                          syntheticImmutable: (HeldDescriptor, Bool) throws -> Void) throws {
        let meta = try POSIX.metadata(directory)
        guard meta.type == S_IFDIR, meta.uid == owner.uid, meta.gid == owner.gid, meta.mode == 0o555 else {
            throw PublicationFailure.invalidMetadata
        }
        try POSIX.requireNoExtendedACL(directory); try POSIX.requireNoUnexpectedExtendedAttributes(directory)
        try syntheticImmutable(directory, true)
        for name in try POSIX.directoryEntryNames(directory) {
            let child = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let meta = try POSIX.metadata(child), isDirectory = meta.type == S_IFDIR
            guard isDirectory || meta.type == S_IFREG, meta.uid == owner.uid, meta.gid == owner.gid else {
                throw PublicationFailure.invalidMetadata
            }
            let rebound = try child.rebindSameIdentity(in: directory, name: name, directory: isDirectory)
            if isDirectory { try verifyFixtureTree(rebound, owner: owner, syntheticImmutable: syntheticImmutable) }
            else {
                guard meta.nlink == 1, meta.mode == ((meta.mode & 0o111) == 0 ? 0o444 : 0o555) else {
                    throw PublicationFailure.invalidMetadata
                }
                try POSIX.requireNoExtendedACL(rebound); try POSIX.requireNoUnexpectedExtendedAttributes(rebound)
                try syntheticImmutable(rebound, false)
            }
        }
    }
#endif
}

/// A once-only transaction: pre-rename errors may quarantine the retained stage; every error
/// after the successful rename syscall is permanently indeterminate and never mutates the tree.
enum Publication {
    enum Outcome: Equatable { case published, quarantined, publishIndeterminate }

    /// A terminal journal failure cannot erase the causal publication failure.  Both values are
    /// deliberately bounded protocol descriptions rather than caller paths, receipt bytes, or
    /// raw filesystem payloads.
    struct PublicationTerminalizationFailure: Error, Equatable {
        let primary: String
        let terminalization: String
    }

    static func publish(_ prepared: PreparedPublication, attestation: StorageAttestation,
                        store: DurableStateStore) throws -> Outcome {
        try perform(prepared: prepared, attestation: attestation, store: store,
                    operations: .production(revalidate: { try attestation.revalidateMutableRegistries() }))
    }

    private static func perform(prepared: PreparedPublication, attestation: StorageAttestation,
                                store: DurableStateStore, operations: PublicationTransactionOperations) throws -> Outcome {
        guard store.currentState == .presealed,
              prepared.journal.phase == .presealed,
              prepared.stage.stageName == prepared.admission.attempt,
              AdmissionController.isAttemptUUID(prepared.admission.attempt) else { throw PublicationFailure.invalidGraph }
        do { try operations.revalidate() }
        catch { return try preRenameTerminal(store, prepared.admission, .preRenameDurability, primary: error) }
        do { _ = try prepared.stage.root.rebindSameIdentity(in: prepared.stage.work, name: prepared.stage.stageName, directory: true) }
        catch { return try preRenameTerminal(store, prepared.admission, .preRenameRebind, primary: error) }
        do {
            let intent = try JournalRecordV2.make(sequence: 3, phase: .renameIntent, admission: prepared.admission,
                                                   publishedClosureSHA256: prepared.graph.closureSHA256,
                                                   receiptFileSHA256: prepared.receiptFileSHA256)
            try StateMachine.append(intent, using: store, admission: prepared.admission)
        } catch { return try preRenameTerminal(store, prepared.admission, .preRenameIO, primary: error) }
        // From this point the rename syscall is being invoked.  Its error result is ambiguous:
        // kernels and filesystems may have changed the name before reporting failure.  Never
        // quarantine or retry after entering this call, including a destination EEXIST race.
        do {
            try operations.renameExclusive(prepared.stage.work, prepared.stage.stageName, attestation.authorities,
                                           ProtectedExecutionConstants.authorityID)
        } catch {
            return try postRenameTerminal(store, prepared.admission, .postRenameRebind, primary: error)
        }
        // Success has crossed the ambiguity boundary before rebind, seal, verify, or fsync.
        let root: HeldDescriptor
        do { root = try operations.rebindFinal(prepared.stage.root, attestation.authorities,
                                                ProtectedExecutionConstants.authorityID) }
        catch { return try postRenameTerminal(store, prepared.admission, .postRenameRebind, primary: error) }
        do {
            try operations.sealRoot(root)
            let verifiedRoot = try root.rebindSameIdentity(in: attestation.authorities,
                                                           name: ProtectedExecutionConstants.authorityID, directory: true)
            try operations.verifyFinal(verifiedRoot, prepared)
            _ = try verifiedRoot.rebindSameIdentity(in: attestation.authorities,
                                                     name: ProtectedExecutionConstants.authorityID, directory: true)
        }
        catch { return try postRenameTerminal(store, prepared.admission, .postRenameSeal, primary: error) }
        do { try operations.durable(attestation.authorities, root, prepared.stage.volume) }
        catch { return try postRenameTerminal(store, prepared.admission, .postRenameDurability, primary: error) }
        do {
            let record = try JournalRecordV2.make(sequence: 4, phase: .published, admission: prepared.admission,
                                                  publishedClosureSHA256: prepared.graph.closureSHA256,
                                                  receiptFileSHA256: prepared.receiptFileSHA256,
                                                  publishedDevice: Int64(root.device), publishedInode: Int64(root.inode))
            try StateMachine.append(record, using: store, admission: prepared.admission)
            return .published
        } catch { return try postRenameTerminal(store, prepared.admission, .postRenameJournal, primary: error) }
    }

    private static func preRenameTerminal(_ store: DurableStateStore, _ admission: Admission,
                                          _ failure: JournalFailureClass, primary: Error) throws -> Outcome {
        do { try StateMachine.terminalizePreRename(using: store, admission: admission, failure: failure) }
        catch { throw PublicationTerminalizationFailure(primary: errorDescription(primary), terminalization: errorDescription(error)) }
        return .quarantined
    }

    private static func postRenameTerminal(_ store: DurableStateStore, _ admission: Admission,
                                           _ failure: JournalFailureClass, primary: Error) throws -> Outcome {
        do { try StateMachine.terminalizePostRename(using: store, admission: admission, failure: failure) }
        catch { throw PublicationTerminalizationFailure(primary: errorDescription(primary), terminalization: errorDescription(error)) }
        return .publishIndeterminate
    }

    private static func errorDescription(_ error: Error) -> String {
        switch error {
        case let POSIXFailure.system(code): return "POSIX.system.\(code)"
        case POSIXFailure.unsafeObject: return "POSIX.unsafeObject"
        case POSIXFailure.unsupported: return "POSIX.unsupported"
        case StateFailure.illegalTransition: return "State.illegalTransition"
        case StateFailure.terminalizationFailed: return "State.terminalizationFailed"
        default: return String(describing: type(of: error))
        }
    }

#if DEBUG
    static func publishForTest(prepared: PreparedPublication, attestation: StorageAttestation,
                               store: DurableStateStore, operations: PublicationTransactionOperations) throws -> Outcome {
        try perform(prepared: prepared, attestation: attestation, store: store, operations: operations)
    }
#endif
}

/// A regular object retained from exclusive creation. Its bytes and identity are both part of
/// the capability, so a later graph walk never accepts a fresh name lookup as equivalent.
struct HeldPublishedRegular {
    let parent: HeldDescriptor
    let name: String
    let file: HeldDescriptor
    let bytes: [UInt8]
    let sha256: SHA256Digest

    init(parent: HeldDescriptor, name: String, file: HeldDescriptor, bytes: [UInt8]) throws {
        self.parent = parent; self.name = name; self.file = file; self.bytes = bytes
        sha256 = try SHA256Digest(CanonicalJSON.sha256(bytes))
    }

    func rebind(in expectedParent: HeldDescriptor) throws -> HeldDescriptor {
        guard parent.device == expectedParent.device, parent.inode == expectedParent.inode else { throw PublicationFailure.sourceChanged }
        return try file.rebindSameIdentity(in: parent, name: name)
    }
}

struct PublishedAdmissionArtifacts {
    let admission: HeldPublishedRegular
    let locator: HeldPublishedRegular
}

/// Descriptor-only metadata boundary for receipt preparation. Production always uses Darwin;
/// DEBUG fixtures may model root:wheel and immutable readback without granting path authority.
struct PublicationPreparationOperations {
    let admissionArtifacts: (HeldDescriptor, Admission) throws -> PublishedAdmissionArtifacts
    let immutable: (HeldDescriptor, String, [UInt8]) throws -> HeldDescriptor
    let receipt: (HeldDescriptor, [UInt8], ActiveReceiptSigner) throws -> HeldReceiptArtifacts
    let prospectiveGraph: (HeldMaterializationStage, PublishedAdmissionArtifacts) throws -> PublishedAuthorityGraph
    let completeGraph: (HeldMaterializationStage, PublishedAdmissionArtifacts) throws -> PublishedAuthorityGraph
    let preseal: (HeldDescriptor) throws -> Void
    let verifyPresealed: (HeldDescriptor) throws -> Void

}

#if DEBUG
/// Test-only holder for capabilities that production creates from the fixed root.  It cannot be
/// initialized from raw key bytes, receipt rows, or paths; this keeps fixture tests honest about
/// the production trust boundary.
struct PublicationPreparationInputs {
    let stage: HeldMaterializationStage
    let bundle: ValidatedBundle
    let admission: Admission
    let store: DurableStateStore
    let signer: ActiveReceiptSigner
    let installation: InstallationObservation
    let operations: PublicationPreparationOperations
}
#endif

/// Receipt creation is deliberately capability-taking: neither a raw private key nor a
/// caller-selected public key can enter the publication path.  The detached signature is
/// verified before either immutable object is committed to the prospective authority graph.
enum ReceiptPublication {
    static func install(into root: HeldDescriptor, receiptBytes: [UInt8], signer: ActiveReceiptSigner) throws -> HeldReceiptArtifacts {
        try install(into: root, receiptBytes: receiptBytes, signer: signer, create: { parent, name, bytes in
            try PublicationPOSIX.createImmutableExclusive(parent, name, bytes, mode: 0o444)
        }, verify: { try $0.requireSealedRegular(mode: 0o444) }, durable: { try POSIX.durableDirectory($0) })
    }

#if DEBUG
    static func installForTest(into root: HeldDescriptor, receiptBytes: [UInt8], signer: ActiveReceiptSigner,
                               operations: PublicationPOSIX.Operations) throws -> HeldReceiptArtifacts {
        try install(into: root, receiptBytes: receiptBytes, signer: signer, create: { parent, name, bytes in
            try PublicationPOSIX.createImmutableExclusive(parent, name, bytes, mode: 0o444, operations: operations)
        }, verify: { try operations.verifySealed($0, 0o444) }, durable: operations.durableDirectory)
    }
#endif

    private static func install(into root: HeldDescriptor, receiptBytes: [UInt8], signer: ActiveReceiptSigner,
                                create: (HeldDescriptor, String, [UInt8]) throws -> HeldDescriptor,
                                verify: (HeldDescriptor) throws -> Void,
                                durable: (HeldDescriptor) throws -> Void) throws -> HeldReceiptArtifacts {
        let receipt = try ReceiptV2.decode(String(decoding: receiptBytes, as: UTF8.self))
        guard receipt.keyGeneration == signer.generation,
              receipt.publicKeySHA256 == signer.publicKeySHA256 else { throw PublicationFailure.sourceChanged }
        let signature = try signer.sign(receiptBytes)
        let publicKey = try Curve25519.Signing.PublicKey(rawRepresentation: Data(signer.publicKeyBytes))
        guard signature.count == 64,
              publicKey.isValidSignature(Data(signature), for: Data(receiptBytes)) else {
            throw PublicationFailure.sourceChanged
        }
        let receiptFile = try create(root, "receipt.json", receiptBytes)
        let signatureFile = try create(root, "receipt.ed25519", signature)
        guard try POSIX.readAll(receiptFile) == receiptBytes,
              try POSIX.readAll(signatureFile, maximum: 65) == signature else { throw PublicationFailure.sourceChanged }
        try verify(receiptFile)
        try verify(signatureFile)
        try durable(root)
        let reboundReceipt = try receiptFile.rebindSameIdentity(in: root, name: "receipt.json")
        let reboundSignature = try signatureFile.rebindSameIdentity(in: root, name: "receipt.ed25519")
        return HeldReceiptArtifacts(receipt: reboundReceipt, signature: reboundSignature,
                                    receiptBytes: receiptBytes, signatureBytes: signature)
    }
}

/// Retained exclusive-write results; neither path nor raw signer is exposed.
struct HeldReceiptArtifacts {
    fileprivate let receipt: HeldDescriptor
    fileprivate let signature: HeldDescriptor
    fileprivate let receiptBytes: [UInt8]
    fileprivate let signatureBytes: [UInt8]
}
