import Darwin
import CryptoKit
import Foundation
import Security

/// Narrow protocol boundaries used by the root-only Task 14 observation path.  These protocols
/// intentionally expose descriptor-bound verification/observation only; they do not expose a
/// pathname, private Key-A bytes, or a process-launch capability.
enum CeremonyAdapterFailure: Error, Equatable {
    case descriptorRebind
    case reviewSignature
    case authorizationSignature
    case observationUnavailable
    case launcherObservation
    case childLaunch
}

struct LauncherSecurityObservation: Equatable {
    let identifier: String
    let designatedRequirement: String
    let teamID: String?
    let certificateChainSHA256: SHA256Digest
    let sha256: SHA256Digest
}

protocol KeyAStore {
    func openAndRebindReviewPackage() throws -> HeldDescriptor
    func openAndRebindObservationAuthorization() throws -> HeldDescriptor
    func verifyReviewPackageSignature() throws
    func verifyObservationAuthorizationSignature() throws
}

protocol ObservationSecurity {
    func observe(_ descriptor: HeldDescriptor, at path: URL) throws -> LauncherSecurityObservation
}

protocol PinnedLauncher {
    func observeSecurity(_ descriptor: HeldDescriptor, at path: URL) throws -> LauncherSecurityObservation
}

/// The candidate path must compare two independent descriptor-bound Security observations before
/// it can use a launcher identity. It never starts a process and never turns a fake observation
/// into a successful result.
struct CandidateObservationAdapter {
    let security: ObservationSecurity
    let launcher: PinnedLauncher

    func observeLauncher(descriptor: HeldDescriptor, at path: URL) throws -> LauncherSecurityObservation {
        let observed = try security.observe(descriptor, at: path)
        let launcherObserved = try launcher.observeSecurity(descriptor, at: path)
        guard observed == launcherObserved else { throw CeremonyAdapterFailure.launcherObservation }
        return observed
    }
}

/// The fallible authority transitions after a stage has been renamed.  This remains internal:
/// production always supplies the raw POSIX/Security implementation, while @testable tests can
/// exercise the same state machine with disposable descriptors.
struct DarwinBootstrapOperations {
    let renameExclusive: (HeldDescriptor, String, HeldDescriptor, String) throws -> Void
    let quarantineRename: (HeldDescriptor, String, HeldDescriptor, String) throws -> Void
    let openFinalRoot: (HeldDescriptor, String) throws -> HeldDescriptor
    let verifyFinalMetadataAndTree: (HeldDescriptor) throws -> Void
    let durableParent: (HeldDescriptor) throws -> Void
    let volumeFullSync: (HeldDescriptor) throws -> Void
    let finalSecurityRebind: (HeldDescriptor, URL) throws -> DarwinBootstrapSecurityAssessment
    let quarantineDurableParent: (HeldDescriptor) throws -> Void
    let quarantineVolumeFullSync: (HeldDescriptor) throws -> Void

    static func production() -> DarwinBootstrapOperations {
        DarwinBootstrapOperations(
            renameExclusive: { from, name, to, destination in
                try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
            },
            quarantineRename: { from, name, to, destination in
                try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
            },
            openFinalRoot: { parent, name in try POSIX.openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY) },
            verifyFinalMetadataAndTree: { root in
                try POSIX.requireDirectory(root, uid: 0, gid: 0, mode: 0o700)
                try DarwinBootstrapAdapters.verifyBootstrapLayout(root: root)
            },
            durableParent: { try POSIX.durableDirectory($0) },
            volumeFullSync: { try POSIX.durableVolumeBarrier($0) },
            finalSecurityRebind: { root, url in try DarwinBootstrapAdapters.securityAssessment(root: root, at: url) },
            quarantineDurableParent: { try POSIX.durableDirectory($0) },
            quarantineVolumeFullSync: { try POSIX.durableVolumeBarrier($0) }
        )
    }
}

/// Descriptor-only publication seams.  The operation table is module-internal; release callers
/// cannot obtain a pathname-based mutation capability or replace the production implementation.
struct DarwinPublicationOperations {
    let createImmutableExclusive: (HeldDescriptor, String, [UInt8], mode_t) throws -> HeldDescriptor
    let fsyncParents: (HeldDescriptor, HeldDescriptor, HeldDescriptor) throws -> Void

    static func production() -> DarwinPublicationOperations {
        DarwinPublicationOperations(
            createImmutableExclusive: { parent, name, bytes, mode in
                try PublicationPOSIX.createImmutableExclusive(parent, name, bytes, mode: mode)
            },
            fsyncParents: { first, second, root in
                try PublicationPOSIX.fsyncParents(first, second, root: root)
            }
        )
    }
}

/// Descriptor-only publication transaction seams. Production is fixed to these operations;
/// the table is available only to DEBUG fixtures so no caller can inject raw path behavior.
struct PublicationTransactionOperations {
    let revalidate: () throws -> Void
    let renameExclusive: (HeldDescriptor, String, HeldDescriptor, String) throws -> Void
    let rebindFinal: (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor
    let sealRoot: (HeldDescriptor) throws -> Void
    let durable: (HeldDescriptor, HeldDescriptor, HeldDescriptor) throws -> Void
    let verifyFinal: (HeldDescriptor, PreparedPublication) throws -> Void

    init(revalidate: @escaping () throws -> Void,
         renameExclusive: @escaping (HeldDescriptor, String, HeldDescriptor, String) throws -> Void,
         rebindFinal: @escaping (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor,
         sealRoot: @escaping (HeldDescriptor) throws -> Void,
         durable: @escaping (HeldDescriptor, HeldDescriptor, HeldDescriptor) throws -> Void,
         verifyFinal: @escaping (HeldDescriptor, PreparedPublication) throws -> Void) {
        self.revalidate = revalidate; self.renameExclusive = renameExclusive; self.rebindFinal = rebindFinal
        self.sealRoot = sealRoot; self.durable = durable; self.verifyFinal = verifyFinal
    }

    static func production(revalidate: @escaping () throws -> Void) -> PublicationTransactionOperations {
        PublicationTransactionOperations(
            revalidate: revalidate,
            renameExclusive: { try POSIX.renameExclusive(from: $0, name: $1, to: $2, destination: $3) },
            rebindFinal: { root, parent, name in try root.rebindSameIdentity(in: parent, name: name, directory: true) },
            sealRoot: { root in
                try POSIX.retryEINTR { Darwin.fchown(root.fd, 0, 0) }
                try POSIX.retryEINTR { Darwin.fchmod(root.fd, 0o555) }
                try POSIX.requireNoExtendedACL(root)
                try POSIX.requireNoUnexpectedExtendedAttributes(root)
                try POSIX.setImmutable(root)
                try root.requireSealedDirectory(mode: 0o555)
            },
            durable: { authorities, root, volume in
                try POSIX.durableDirectory(authorities)
                try POSIX.durableDirectory(root)
                try POSIX.durableVolumeBarrier(volume)
            },
            verifyFinal: { root, prepared in try PublishedAuthorityGraph.verifyPublishedFinal(root: root, prepared: prepared) }
        )
    }

#if DEBUG
    static func fixture(syntheticImmutable: @escaping (HeldDescriptor, Bool) throws -> Void,
                        markSyntheticImmutable: @escaping (HeldDescriptor) -> Void) -> PublicationTransactionOperations {
        PublicationTransactionOperations(
            revalidate: {},
            renameExclusive: { try POSIX.renameExclusive(from: $0, name: $1, to: $2, destination: $3) },
            rebindFinal: { root, parent, name in try root.rebindSameIdentity(in: parent, name: name, directory: true) },
            sealRoot: { root in
                guard Darwin.fchmod(root.fd, 0o555) == 0 else { throw POSIXFailure.system(errno) }
                markSyntheticImmutable(root)
            },
            durable: { authorities, root, volume in
                try POSIX.durableDirectory(authorities); try POSIX.durableDirectory(root); try POSIX.durableVolumeBarrier(volume)
            },
            verifyFinal: { root, prepared in
                try PublishedAuthorityGraph.verifyPublishedFixtureFinal(root: root, prepared: prepared,
                                                                          syntheticImmutable: syntheticImmutable)
            }
        )
    }
#endif
}

struct DarwinBootstrapSecurityAssessment {
    let designatedRequirementBytes: [UInt8]
    let teamID: String
    let signingIdentitySHA1: String
}

struct DarwinBootstrapPublicationIdentity: Equatable {
    let device: Int64
    let inode: UInt64

    init(descriptor: HeldDescriptor) {
        device = Int64(descriptor.device)
        inode = UInt64(descriptor.inode)
    }
}

struct DarwinBootstrapPublicationState: Equatable {
    let stageName: String?
    let stageIdentity: DarwinBootstrapPublicationIdentity?
    let isPublished: Bool
}

/// The only production mutation capability. Its initializer is private so callers cannot forge a
/// user-owned root or bypass the EUID gate.
public final class DarwinBootstrapAdapters: BootstrapCoreAdapters {
#if DEBUG
    /// Disposable verifier seam used only by deterministic ABA tests. Production has no hook.
    static var operationalAfterFirstEnumerationHook: (() throws -> Void)?
    static var bootstrapAfterEmptyEnumerationHook: ((String) throws -> Void)?
    private static func invokeOperationalHook() throws {
        try operationalAfterFirstEnumerationHook?()
    }
    private static func invokeBootstrapHook(_ registry: String) throws {
        try bootstrapAfterEmptyEnumerationHook?(registry)
    }
#endif
    static var installedProvisionerURL: URL {
        URL(fileURLWithPath: ProtectedExecutionConstants.productionRoot)
            .appendingPathComponent(BootstrapObject.provisioner.rawValue)
    }
    private let parent: HeldDescriptor
    private let parentPath: String
    private let expectedRoot: String
    private let finalName: String
    private let operations: DarwinBootstrapOperations
    private var stage: (name: String, descriptor: HeldDescriptor)?
    private var published: HeldDescriptor?
    private var stagedIdentity: BootstrapHelperIdentity?
    private var approvalBinding: Task14ApprovalV2?
    /// This descriptor-bound manifest is populated only after each exclusive create succeeds and
    /// remains held across the stage-to-final rename.  It makes the final-tree rebind check a
    /// comparison against the bytes that were actually sealed, rather than against caller input.
    private var expectedObjectDigests: [BootstrapObject: String] = [:]

    private init(parent: HeldDescriptor, parentPath: String, expectedRoot: String, finalName: String, operations: DarwinBootstrapOperations) {
        self.parent = parent
        self.parentPath = parentPath
        self.expectedRoot = expectedRoot
        self.finalName = finalName
        self.operations = operations
    }

    public static func makeProduction() throws -> DarwinBootstrapAdapters {
        guard geteuid() == 0 else { throw BootstrapError.rootRequired }
        let parent = try POSIX.openTrustedRootOwnedDirectory(ProtectedExecutionConstants.productionParent)
        return DarwinBootstrapAdapters(parent: parent, parentPath: ProtectedExecutionConstants.productionParent, expectedRoot: ProtectedExecutionConstants.productionRoot, finalName: ProtectedExecutionConstants.productionRootName, operations: .production())
    }

    /// Internal disposable-only construction route for @testable tests.  It cannot relax the
    /// public production factory's fixed-root or root:wheel requirements.
    static func makeDisposableForTest(parent: HeldDescriptor, parentPath: String, root: String, stageName: String, stageDescriptor: HeldDescriptor, operations: DarwinBootstrapOperations) -> DarwinBootstrapAdapters {
        let adapters = DarwinBootstrapAdapters(parent: parent, parentPath: parentPath, expectedRoot: root, finalName: URL(fileURLWithPath: root).lastPathComponent, operations: operations)
        adapters.stage = (stageName, stageDescriptor)
        return adapters
    }

    var testPublicationState: DarwinBootstrapPublicationState {
        DarwinBootstrapPublicationState(stageName: stage?.name, stageIdentity: stage.map { DarwinBootstrapPublicationIdentity(descriptor: $0.descriptor) }, isPublished: published != nil)
    }

    func rootExists(at root: String) throws -> Bool {
        guard root == expectedRoot else { throw BootstrapAdapterFailure.operation }
        let residue = try POSIX.directoryEntryNames(parent).contains { $0.hasPrefix(".r4-bootstrap-") || $0.hasPrefix(".r4-incomplete-") }
        if residue { return true }
        do { _ = try POSIX.openAt(parent, name: finalName, flags: O_RDONLY | O_DIRECTORY); return true }
        catch POSIXFailure.system(let code) where code == ENOENT { return false }
    }

    func createFreshRoot(at root: String) throws {
        guard root == expectedRoot, stage == nil else { throw BootstrapAdapterFailure.operation }
        var bytes = [UInt8](repeating: 0, count: 16)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw BootstrapAdapterFailure.operation }
        let suffix = bytes.map { String(format: "%02x", $0) }.joined()
        let name = ".r4-bootstrap-\(suffix)"
        do {
            let descriptor = try POSIX.mkdirAt(parent, name: name, mode: 0o700)
            guard fchown(descriptor.fd, 0, 0) == 0, fchmod(descriptor.fd, 0o700) == 0 else { throw BootstrapAdapterFailure.operation }
            try POSIX.requireDirectory(descriptor, uid: 0, gid: 0, mode: 0o700)
            try POSIX.durableDirectory(descriptor)
            stage = (name, descriptor)
        }
        catch { throw BootstrapAdapterFailure.operation }
    }

    func createDirectory(_ object: BootstrapObject) throws { _ = try directory(for: object.rawValue, create: true) }

    func write(_ bytes: [UInt8], to object: BootstrapObject, mode: Int) throws {
        let (container, name) = try parentForFile(object)
        let file = try POSIX.createExclusive(container, name: name, mode: mode_t(mode))
        guard fchown(file.fd, 0, 0) == 0, fchmod(file.fd, mode_t(mode)) == 0 else { throw BootstrapAdapterFailure.operation }
        try POSIX.durableRegular(file)
        try POSIX.writeAll(file, bytes)
        try POSIX.requireRegular(file, uid: 0, gid: 0, mode: mode_t(mode))
        try POSIX.durableRegular(file)
        expectedObjectDigests[object] = CanonicalJSON.sha256(bytes)
    }

    func fsync(_ object: BootstrapObject) throws {
        let held = try openObject(object)
        if object.isDirectory { try POSIX.durableDirectory(held) }
        else { try POSIX.durableRegular(held) }
    }
    func verifyMetadata(_ object: BootstrapObject, expectedMode: Int) throws {
        let held = try openObject(object)
        do {
            if object.isDirectory { try POSIX.requireDirectory(held, uid: 0, gid: 0, mode: mode_t(expectedMode)) }
            else { try POSIX.requireRegular(held, uid: 0, gid: 0, mode: mode_t(expectedMode)) }
        } catch POSIXFailure.unsafeObject { throw BootstrapAdapterFailure.acl }
        catch POSIXFailure.system { throw BootstrapAdapterFailure.aclAPI }
        catch { throw BootstrapAdapterFailure.operation }
    }
    func seal(_ object: BootstrapObject) throws {
        let held = try openObject(object)
        if object.isDirectory {
            guard object.requiresImmutableFlag else { throw BootstrapAdapterFailure.operation }
            // The stage is root-only while mutable; immutable authority directories are read-only.
            guard fchmod(held.fd, 0o555) == 0 else { throw BootstrapAdapterFailure.operation }
            try POSIX.requireDirectory(held, uid: 0, gid: 0, mode: 0o555)
            try POSIX.durableDirectory(held)
        }
        try POSIX.setImmutable(held)
        if object.isDirectory { try POSIX.durableDirectory(held) }
        else { try POSIX.durableRegular(held) }
    }

    func rebindAndVerify(_ object: BootstrapObject, expectedMode: Int) throws {
        if object.isDirectory {
            let held = try openObject(object)
            _ = try held.rebind(in: try parentForDirectory(object), name: object.rawValue.split(separator: "/").last.map(String.init)!, directory: true)
            try POSIX.requireDirectory(held, uid: 0, gid: 0, mode: mode_t(expectedMode))
            var status = stat(); guard fstat(held.fd, &status) == 0,
                  object.requiresImmutableFlag ? (status.st_flags & UInt32(SF_IMMUTABLE)) != 0 : (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw BootstrapAdapterFailure.operation }
            return
        }
        let (container, name) = try parentForFile(object)
        let held = try POSIX.openAt(container, name: name, flags: O_RDONLY)
        _ = try held.rebind(in: container, name: name)
        try POSIX.requireRegular(held, uid: 0, gid: 0, mode: mode_t(expectedMode))
        var status = stat()
        guard fstat(held.fd, &status) == 0, (status.st_flags & UInt32(SF_IMMUTABLE)) != 0 else { throw BootstrapAdapterFailure.operation }
    }

    func publishFreshRoot(at root: String) throws {
        guard root == expectedRoot, let stage else { throw BootstrapAdapterFailure.operation }
        try POSIX.durableDirectory(stage.descriptor)
        try POSIX.durableVolumeBarrier(parent)
        do { try operations.renameExclusive(parent, stage.name, parent, finalName) }
        catch POSIXFailure.system(let code) where code == EEXIST { throw BootstrapError.rootAlreadyExists }
        catch { throw BootstrapAdapterFailure.operation }
        // Rename has already changed authority state.  Capture that fact before any fallible
        // reopen/metadata/durability work so all later failures quarantine the real final root.
        self.stage = (finalName, stage.descriptor)
        self.published = stage.descriptor
        published = try operations.openFinalRoot(parent, finalName)
        guard let published else { throw BootstrapAdapterFailure.operation }
        // The root stays root-only mutable.  Every future authority mutation is constrained to
        // its precreated registry children; sealing root would make those records impossible.
        try operations.verifyFinalMetadataAndTree(published)
        try operations.durableParent(parent)
        try operations.volumeFullSync(parent)
        _ = try operations.finalSecurityRebind(published, finalProvisionerURL())
        self.stage = (finalName, published)
    }

    func abortFreshRoot() throws {
        guard let stage else { return }
        if stage.name == finalName {
            guard try quarantinePublishedRoot() else { throw BootstrapAdapterFailure.operation }
            return
        }
        // Rebind the root name first: any swap means this capability no longer owns the object.
        _ = try stage.descriptor.rebind(in: parent, name: stage.name, directory: true)
        for object in BootstrapObject.allCases.reversed() {
            if object.requiresImmutableFlag, let held = try? openObject(object) {
                try POSIX.clearImmutable(held)
                if object.isDirectory { try POSIX.durableDirectory(held) }
                else { try POSIX.durableRegular(held) }
            }
            guard let (container, name) = try? parentForFileOrDirectory(object) else { continue }
            do { try POSIX.unlinkAt(container, name: name, directory: object.isDirectory) }
            catch POSIXFailure.system(let code) where code == ENOENT { continue }
        }
        try POSIX.unlinkAt(parent, name: stage.name, directory: true)
        try POSIX.durableDirectory(parent)
        try POSIX.durableVolumeBarrier(parent)
        self.stage = nil
    }

    func quarantinePublishedRoot() throws -> Bool {
        guard let published = published ?? stage?.descriptor, stage?.name == finalName else { return false }
        do { _ = try published.rebind(in: parent, name: finalName, directory: true) }
        catch POSIXFailure.unsafeObject { return false }
        var bytes = [UInt8](repeating: 0, count: 16)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw BootstrapAdapterFailure.operation }
        let name = ".r4-incomplete-" + bytes.map { String(format: "%02x", $0) }.joined()
        try operations.quarantineRename(parent, finalName, parent, name)
        try operations.quarantineDurableParent(parent)
        try operations.quarantineVolumeFullSync(parent)
        self.stage = nil
        self.published = nil
        return true
    }

    func heldBootstrapInstallSource() throws -> BootstrapInstallSource {
        guard let executable = Bundle.main.executableURL else { throw BootstrapAdapterFailure.operation }
        let held = try POSIX.openTrustedRootOwnedRegular(executable.path)
        let before = try POSIX.readAll(held.file)
        let after = try POSIX.readAll(try held.file.rebind(in: held.parent, name: held.name))
        guard before == after else { throw BootstrapAdapterFailure.swapped }
        return BootstrapInstallSource(provisionerBytes: before)
    }
    func assessStagedBootstrap(approval: Task14ApprovalV2, source: BootstrapInstallSource) throws -> BootstrapInstallIdentity {
        let (container, name) = try parentForFile(.provisioner)
        let held = try POSIX.openAt(container, name: name, flags: O_RDONLY)
        let before = try POSIX.readAll(held)
        guard CanonicalJSON.sha256(before) == CanonicalJSON.sha256(source.provisionerBytes), CanonicalJSON.sha256(before) == approval.provisionerBinarySHA256.value else { throw BootstrapAdapterFailure.swapped }
        guard let stage else { throw BootstrapAdapterFailure.operation }
        let assessment = try operations.finalSecurityRebind(try stageDescriptor(), URL(fileURLWithPath: parentPath).appendingPathComponent(stage.name).appendingPathComponent(BootstrapObject.provisioner.rawValue))
        let after = try POSIX.readAll(try held.rebind(in: container, name: name))
        guard after == before else { throw BootstrapAdapterFailure.swapped }
        let identity = try BootstrapHelperIdentity(binarySHA256: SHA256Digest(CanonicalJSON.sha256(after)), designatedRequirementSHA256: SHA256Digest(CanonicalJSON.sha256(assessment.designatedRequirementBytes)), teamID: assessment.teamID, signingIdentitySHA1: assessment.signingIdentitySHA1)
        stagedIdentity = identity
        approvalBinding = approval
        return BootstrapInstallIdentity(helper: identity, provisionerBytes: after, designatedRequirementBytes: assessment.designatedRequirementBytes)
    }
    func rebindInstalledHelper(expected: BootstrapHelperIdentity) throws {
        guard published != nil, stagedIdentity == expected, let approvalBinding,
              Set(expectedObjectDigests.keys) == Set(BootstrapObject.allCases.filter { !$0.isDirectory }) else { throw BootstrapAdapterFailure.operation }
        try verifyClosedLayout()
        for object in BootstrapObject.allCases {
            let mode = object.finalMode
            try rebindAndVerify(object, expectedMode: mode)
            if !object.isDirectory {
                let bytes = try POSIX.readAll(try openObject(object))
                guard CanonicalJSON.sha256(bytes) == expectedObjectDigests[object] else { throw BootstrapAdapterFailure.swapped }
            }
        }
        let finalBytes = try POSIX.readAll(try openObject(.provisioner))
        let finalIdentity = try assessStagedBootstrap(approval: approvalBinding, source: BootstrapInstallSource(provisionerBytes: finalBytes))
        guard finalIdentity.helper == expected else { throw BootstrapAdapterFailure.swapped }
        try POSIX.durableDirectory(parent)
        try POSIX.durableVolumeBarrier(parent)
    }

    func isValidApprovalSignature(verificationPublicKey: [UInt8], approval: [UInt8], signature: [UInt8]) throws -> Bool {
        Self.isValidApprovalSignature(verificationPublicKey: verificationPublicKey, approval: approval, signature: signature)
    }

    static func isValidApprovalSignature(verificationPublicKey: [UInt8], approval: [UInt8], signature: [UInt8]) -> Bool {
        guard verificationPublicKey.count == 32, signature.count == 64 else { return false }
        do {
            let publicKey = try Curve25519.Signing.PublicKey(rawRepresentation: Data(verificationPublicKey))
            return publicKey.isValidSignature(Data(signature), for: Data(approval))
        } catch {
            return false
        }
    }

    func randomBytes(count: Int) throws -> [UInt8] {
        var bytes = [UInt8](repeating: 0, count: count)
        guard SecRandomCopyBytes(kSecRandomDefault, count, &bytes) == errSecSuccess else { throw BootstrapAdapterFailure.operation }
        return bytes
    }

    static func overwriteKeyBBuffer(_ bytes: inout [UInt8]) {
        bytes.withUnsafeMutableBufferPointer { buffer in
            for index in buffer.indices { buffer[index] = 0 }
        }
    }

    func zeroizeKeyB(_ bytes: inout [UInt8]) { Self.overwriteKeyBBuffer(&bytes) }

    func verifySignProbe(publicKey: [UInt8], signature: [UInt8]) throws {
        guard publicKey.count == 32, signature.count == 64 else { throw BootstrapAdapterFailure.signProbe }
    }

    private func stageDescriptor() throws -> HeldDescriptor {
        if let stage { return stage.descriptor }
        if let published { return published }
        throw BootstrapAdapterFailure.operation
    }
    private func directory(for path: String, create: Bool) throws -> HeldDescriptor {
        var current = try stageDescriptor()
        for component in path.split(separator: "/") {
            let name = String(component)
            do { current = try POSIX.openAt(current, name: name, flags: O_RDONLY | O_DIRECTORY) }
            catch POSIXFailure.system(let code) where create && code == ENOENT {
                let created = try POSIX.mkdirAt(current, name: name, mode: 0o700)
                guard fchown(created.fd, 0, 0) == 0, fchmod(created.fd, 0o700) == 0 else { throw BootstrapAdapterFailure.operation }
                try POSIX.requireDirectory(created, uid: 0, gid: 0, mode: 0o700)
                try POSIX.durableDirectory(created)
                current = created
            }
        }
        return current
    }
    private func parentForFile(_ object: BootstrapObject) throws -> (HeldDescriptor, String) {
        let components = object.rawValue.split(separator: "/").map(String.init)
        guard let name = components.last else { throw BootstrapAdapterFailure.operation }
        return (try directory(for: components.dropLast().joined(separator: "/"), create: false), name)
    }
    private func parentForDirectory(_ object: BootstrapObject) throws -> HeldDescriptor {
        let components = object.rawValue.split(separator: "/").map(String.init)
        return try directory(for: components.dropLast().joined(separator: "/"), create: false)
    }
    private func parentForFileOrDirectory(_ object: BootstrapObject) throws -> (HeldDescriptor, String) {
        let components = object.rawValue.split(separator: "/").map(String.init)
        guard let name = components.last else { throw BootstrapAdapterFailure.operation }
        return (try directory(for: components.dropLast().joined(separator: "/"), create: false), name)
    }
    private func openObject(_ object: BootstrapObject) throws -> HeldDescriptor {
        if object.isDirectory { return try directory(for: object.rawValue, create: false) }
        let (container, name) = try parentForFile(object)
        return try POSIX.openAt(container, name: name, flags: O_RDONLY)
    }

    private func finalProvisionerURL() -> URL {
        URL(fileURLWithPath: parentPath).appendingPathComponent(finalName).appendingPathComponent(BootstrapObject.provisioner.rawValue)
    }

    /// The installed authority root is a closed graph: unexpected objects are authority bypass
    /// candidates, and absent objects mean the graph is not the one that was sealed.
    private func verifyClosedLayout() throws { try Self.verifyBootstrapLayout(root: try stageDescriptor()) }

    private static let bootstrapRootChildren: [String: Bool] = ["bootstrap": true, "control": true, "materialization-work": true, "quarantine": true, "authorities": true]
    private static let bootstrapChildren: [String: Bool] = ["task14-approval-v2.json": false, "task14-approval-v2.ed25519": false, "task14-approval-verification-public-key": false, "task14-approval-v2.sha256": false, "policy.json": false, "materialization-policy.json": false, "provisioner": false, "provisioner.codesign-requirement.txt": false]
    private static let generationChildren: [String: Bool] = ["authority-receipt-ed25519-private-key": false, "authority-receipt-ed25519-public-key": false, "record.json": false, "record.ed25519": false]

    private static func directory(_ root: HeldDescriptor, path: String) throws -> HeldDescriptor {
        var current = root
        for component in path.split(separator: "/") {
            let name = String(component)
            let opened = try POSIX.openAt(current, name: name, flags: O_RDONLY | O_DIRECTORY)
            current = try opened.rebindSameIdentity(in: current, name: name, directory: true)
        }
        return current
    }

    private static func reboundDirectory(_ parent: HeldDescriptor, name: String) throws -> HeldDescriptor {
        let opened = try POSIX.openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY)
        return try opened.rebindSameIdentity(in: parent, name: name, directory: true)
    }

    private static func reboundRegular(_ parent: HeldDescriptor, name: String) throws -> HeldDescriptor {
        let opened = try POSIX.openAt(parent, name: name, flags: O_RDONLY)
        return try opened.rebindSameIdentity(in: parent, name: name, directory: false)
    }

    private static func requireExactEntries(_ directory: HeldDescriptor, _ expected: [String: Bool]) throws {
        let actual = try POSIX.directoryEntryNames(directory)
        try requireExactTypes(directory, expected: expected, actual: actual)
    }

    private static func requireExactTypes(_ directory: HeldDescriptor, expected: [String: Bool], actual: Set<String>) throws {
        let expectedNames = Set(expected.keys)
        if !expectedNames.isSubset(of: actual) { throw BootstrapAdapterFailure.missing }
        if !actual.isSubset(of: expectedNames) { throw BootstrapAdapterFailure.extra }
        for (name, isDirectory) in expected {
            let opened = try POSIX.openAt(directory, name: name, flags: O_RDONLY | (isDirectory ? O_DIRECTORY : 0))
            let rebound = try opened.rebindSameIdentity(in: directory, name: name, directory: isDirectory)
            var status = stat(); try POSIX.fstatRetry(rebound.fd, &status)
            guard (status.st_mode & S_IFMT) == (isDirectory ? S_IFDIR : S_IFREG) else { throw BootstrapAdapterFailure.extra }
        }
    }

    private static func requireMutable(_ directory: HeldDescriptor) throws {
        do { try POSIX.requireDirectory(directory, uid: 0, gid: 0, mode: 0o700) }
        catch POSIXFailure.unsafeObject { throw BootstrapAdapterFailure.acl }
        catch POSIXFailure.system { throw BootstrapAdapterFailure.aclAPI }
        var status = stat()
        try POSIX.fstatRetry(directory.fd, &status)
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw BootstrapAdapterFailure.extra }
    }

    private static func requireMutableEmpty(_ directory: HeldDescriptor, parent: HeldDescriptor? = nil, name: String? = nil) throws {
        try requireMutable(directory)
        let initial = try POSIX.directoryEntryNames(directory)
        guard initial.isEmpty else { throw BootstrapAdapterFailure.extra }
#if DEBUG
        try invokeBootstrapHook(name ?? "unknown")
#endif
        if let parent, let name {
            let final = try directory.rebind(in: parent, name: name, directory: true)
            guard try POSIX.directoryEntryNames(final) == initial else { throw BootstrapAdapterFailure.extra }
        }
    }

    /// Bootstrap publication is intentionally exact. In particular, every mutable registry is
    /// present, mode 0700, unsealed, and empty; accepting operational residue here would turn a
    /// rotated or partially recovered tree into a fresh trust anchor.
    static func verifyBootstrapLayout(root: HeldDescriptor) throws {
        let rootSnapshot = try POSIX.directoryEntryNames(root)
        try requireExactTypes(root, expected: bootstrapRootChildren, actual: rootSnapshot)
        try requireExactEntries(try directory(root, path: "bootstrap"), bootstrapChildren)
        let control = try reboundDirectory(root, name: "control")
        let controlSnapshot = try POSIX.directoryEntryNames(control)
        try requireExactTypes(control, expected: ["keyring": true, "revocations": true, "admissions": true, "journals": true], actual: controlSnapshot)
        let keyring = try reboundDirectory(control, name: "keyring")
        let keyringSnapshot = try POSIX.directoryEntryNames(keyring)
        try requireExactTypes(keyring, expected: ["generation-1": true], actual: keyringSnapshot)
#if DEBUG
        try invokeBootstrapHook("control")
        try invokeBootstrapHook("keyring")
#endif
        try requireExactTypes(control, expected: ["keyring": true, "revocations": true, "admissions": true, "journals": true], actual: try POSIX.directoryEntryNames(control))
        try requireExactTypes(keyring, expected: ["generation-1": true], actual: try POSIX.directoryEntryNames(keyring))
        try requireExactEntries(try directory(root, path: "control/keyring/generation-1"), generationChildren)
        try requireMutable(try directory(root, path: "control"))
        try requireMutable(try directory(root, path: "control/keyring"))
        let rootMutable: [(HeldDescriptor, String)] = [(root, "materialization-work"), (root, "quarantine"), (root, "authorities")]
        for (parent, name) in rootMutable { try requireMutableEmpty(try reboundDirectory(parent, name: name), parent: parent, name: name) }
        for name in ["revocations", "admissions", "journals"] {
            try requireMutableEmpty(try reboundDirectory(control, name: name), parent: control, name: name)
        }
        let finalRoot = root
        let finalControl = try control.rebind(in: finalRoot, name: "control", directory: true)
        guard try POSIX.directoryEntryNames(finalRoot) == rootSnapshot,
              try POSIX.directoryEntryNames(finalControl) == controlSnapshot else { throw BootstrapAdapterFailure.extra }
        let finalKeyring = try keyring.rebind(in: finalControl, name: "keyring", directory: true)
        guard try POSIX.directoryEntryNames(finalKeyring) == keyringSnapshot else { throw BootstrapAdapterFailure.extra }
    }

    private static func isCanonicalGeneration(_ name: String) -> Bool {
        guard name.hasPrefix("generation-") else { return false }
        let suffix = String(name.dropFirst("generation-".count))
        guard !suffix.isEmpty, suffix.first != "0", suffix.allSatisfy({ $0.isNumber && $0.isASCII }) else { return false }
        return Int64(suffix) != nil
    }

    private static func isLowercaseUUID(_ value: String) -> Bool {
        guard value.count == 32 else { return false }
        return value.allSatisfy { ($0 >= "0" && $0 <= "9") || ($0 >= "a" && $0 <= "f") }
    }

    private static func isAdmissionName(_ name: String) -> Bool {
        guard name.hasSuffix(".json") else { return false }
        return isLowercaseUUID(String(name.dropLast(5)))
    }

    private static func verifyJournalFiles(_ journal: HeldDescriptor) throws {
        let names = try POSIX.directoryEntryNames(journal)
        let sorted = names.sorted()
        let prefixes: [[String]] = [["0001-stage-created.json"], ["0001-stage-created.json", "0002-presealed.json"], ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json"]]
        let fullTerminal = ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json"]
        let valid = prefixes.contains { $0 == sorted }
            || prefixes.contains { $0 + ["0004-quarantined.json"] == sorted }
            || ["0004-published.json", "0004-publish-indeterminate.json"].contains(where: { fullTerminal + [$0] == sorted })
        guard valid else { throw BootstrapAdapterFailure.extra }
        for name in names { _ = try reboundRegular(journal, name: name) }
    }

    /// Read-only operational grammar. This deliberately does not decode or verify Key-B records;
    /// that cryptographic chain remains the keyring verifier's responsibility.
    static func verifyOperationalRegistryLayout(root: HeldDescriptor) throws {
        try requireExactEntries(root, bootstrapRootChildren)
        try requireExactEntries(try directory(root, path: "bootstrap"), bootstrapChildren)
        let control = try directory(root, path: "control")
        let controlSnapshot = try POSIX.directoryEntryNames(control)
        guard controlSnapshot == Set(["keyring", "revocations", "admissions", "journals"]) else { throw BootstrapAdapterFailure.extra }
#if DEBUG
        try invokeOperationalHook()
#endif
        try requireExactTypes(control, expected: ["keyring": true, "revocations": true, "admissions": true, "journals": true], actual: controlSnapshot)

        let keyring = try reboundDirectory(control, name: "keyring")
        let generationNames = try POSIX.directoryEntryNames(keyring)
#if DEBUG
        try invokeOperationalHook()
#endif
        var generations: [Int64] = []
        for name in generationNames {
            guard isCanonicalGeneration(name) else { throw BootstrapAdapterFailure.extra }
            // Task 4 validates only the generation-name grammar and object kind. The
            // generation's record/public/private bytes and successor continuity are Task 5.
            let suffix = String(name.dropFirst("generation-".count))
            guard let generation = Int64(suffix), generation > 0 else { throw BootstrapAdapterFailure.extra }
            generations.append(generation)
            _ = try reboundDirectory(keyring, name: name)
        }
        let orderedGenerations = generations.sorted()
        guard orderedGenerations.first == 1,
              zip(orderedGenerations, orderedGenerations.dropFirst()).allSatisfy({ $0.1 == $0.0 + 1 }) else { throw BootstrapAdapterFailure.missing }

        let revocations = try reboundDirectory(control, name: "revocations")
        let revocationNames = try POSIX.directoryEntryNames(revocations)
        guard revocationNames.isEmpty || (revocationNames.count == 1 && revocationNames.contains("revocation-00000000000000000001")) else { throw BootstrapAdapterFailure.extra }
        if let name = revocationNames.first { _ = try reboundDirectory(revocations, name: name) }

        let admissions = try reboundDirectory(control, name: "admissions")
        let admissionsSnapshot = try POSIX.directoryEntryNames(admissions)
        for name in admissionsSnapshot { guard isAdmissionName(name) else { throw BootstrapAdapterFailure.extra }; _ = try reboundRegular(admissions, name: name) }
        let journals = try reboundDirectory(control, name: "journals")
        let journalNames = try POSIX.directoryEntryNames(journals)
        for name in journalNames {
            guard isLowercaseUUID(name) else { throw BootstrapAdapterFailure.extra }
            let freshJournals = try reboundDirectory(control, name: "journals")
            let journal = try reboundDirectory(freshJournals, name: name)
            let firstFiles = try POSIX.directoryEntryNames(journal)
            try verifyJournalFiles(journal)
#if DEBUG
            try invokeOperationalHook()
#endif
            let finalJournal = try journal.rebind(in: journals, name: name, directory: true)
            guard try POSIX.directoryEntryNames(finalJournal) == firstFiles else { throw BootstrapAdapterFailure.extra }
        }
        let work = try reboundDirectory(root, name: "materialization-work")
        let quarantine = try reboundDirectory(root, name: "quarantine")
        let workSnapshot = try POSIX.directoryEntryNames(work)
        let quarantineSnapshot = try POSIX.directoryEntryNames(quarantine)
        for name in workSnapshot { guard isLowercaseUUID(name) else { throw BootstrapAdapterFailure.extra }; _ = try reboundDirectory(work, name: name) }
        for name in quarantineSnapshot { guard isLowercaseUUID(name) else { throw BootstrapAdapterFailure.extra }; _ = try reboundDirectory(quarantine, name: name) }
        let authorities = try reboundDirectory(root, name: "authorities")
        let authoritiesSnapshot = try POSIX.directoryEntryNames(authorities)
        for name in try POSIX.directoryEntryNames(authorities) {
            guard name == ProtectedExecutionConstants.authorityID else { throw BootstrapAdapterFailure.extra }
            _ = try reboundDirectory(authorities, name: name)
        }
        let keyringFinal = try keyring.rebind(in: control, name: "keyring", directory: true)
        let revocationsFinal = try revocations.rebind(in: control, name: "revocations", directory: true)
        let admissionsFinal = try admissions.rebind(in: control, name: "admissions", directory: true)
        let journalsFinal = try journals.rebind(in: control, name: "journals", directory: true)
        let workFinal = try work.rebind(in: root, name: "materialization-work", directory: true)
        let quarantineFinal = try quarantine.rebind(in: root, name: "quarantine", directory: true)
        let authoritiesFinal = try authorities.rebind(in: root, name: "authorities", directory: true)
        guard try POSIX.directoryEntryNames(keyringFinal) == Set(generationNames),
              try POSIX.directoryEntryNames(revocationsFinal) == Set(revocationNames),
              try POSIX.directoryEntryNames(admissionsFinal) == admissionsSnapshot,
              try POSIX.directoryEntryNames(journalsFinal) == Set(journalNames),
              try POSIX.directoryEntryNames(workFinal) == workSnapshot,
              try POSIX.directoryEntryNames(quarantineFinal) == quarantineSnapshot,
              try POSIX.directoryEntryNames(authoritiesFinal) == authoritiesSnapshot else { throw BootstrapAdapterFailure.extra }
        let controlFinal = try control.rebind(in: root, name: "control", directory: true)
        guard try POSIX.directoryEntryNames(controlFinal) == controlSnapshot else { throw BootstrapAdapterFailure.extra }
    }

    static func verifyClosedLayout(root: HeldDescriptor) throws {
        try verifyBootstrapLayout(root: root)
    }

    static func securityAssessment(root: HeldDescriptor, at url: URL) throws -> DarwinBootstrapSecurityAssessment {
        let bootstrap = try POSIX.openAt(root, name: BootstrapObject.bootstrapDirectory.rawValue, flags: O_RDONLY | O_DIRECTORY)
        let provisioner = try POSIX.openAt(bootstrap, name: "provisioner", flags: O_RDONLY)
        _ = try provisioner.rebind(in: bootstrap, name: "provisioner")
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(url as CFURL, SecCSFlags(), &code) == errSecSuccess, let code else { throw BootstrapAdapterFailure.operation }
        guard SecStaticCodeCheckValidity(code, SecCSFlags(), nil) == errSecSuccess else { throw BootstrapAdapterFailure.operation }
        var requirement: SecRequirement?
        guard SecCodeCopyDesignatedRequirement(code, SecCSFlags(), &requirement) == errSecSuccess, let requirement else { throw BootstrapAdapterFailure.operation }
        var requirementData: CFData?
        guard SecRequirementCopyData(requirement, SecCSFlags(), &requirementData) == errSecSuccess, let requirementData else { throw BootstrapAdapterFailure.operation }
        var signingInfo: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &signingInfo) == errSecSuccess,
              let info = signingInfo as? [CFString: Any], let team = info[kSecCodeInfoTeamIdentifier] as? String,
              let certificates = info[kSecCodeInfoCertificates] as? [SecCertificate], let leaf = certificates.first else { throw BootstrapAdapterFailure.operation }
        return DarwinBootstrapSecurityAssessment(designatedRequirementBytes: Array(requirementData as Data), teamID: team, signingIdentitySHA1: Insecure.SHA1.hash(data: SecCertificateCopyData(leaf) as Data).map { String(format: "%02X", $0) }.joined())
    }
}
