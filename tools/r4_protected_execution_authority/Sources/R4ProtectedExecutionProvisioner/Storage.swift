import Darwin
import Foundation
import Security

enum MaterializationFailure: Error, Equatable { case identity, environment, storage, bundle, graph, artifact, checkout, layout, metadata, inputPath, artifactMetadata, checkoutMetadata, lockDigest, policyDigest, approvalManifest, approvalLock, approvalArchive, approvalWheelSet, approvalPolicy, wheelNames, lockArchive, lockWheels }

/// Capability-bearing request.  The command-line entrypoint constructs this only from fixed,
/// root-owned bootstrap locations; its test-only initializer is internal to this module.
public struct MaterializationRequest: Equatable {
    public let inputBundle: URL
    public init(inputBundle: URL) { self.inputBundle = inputBundle }
}

struct HeldBootstrapApproval {
    let descriptor: HeldDescriptor
    let bytes: [UInt8]
    let approval: Task14ApprovalV2
}

/// Root-owned ceremony handoff capability.  The ceremony inputs are deliberately separate from
/// the materialization admission record: opening this record never creates an admission and does
/// not carry any future runtime, launcher, or receipt digest.
struct HeldCeremonyHandoff {
    let root: HeldDescriptor
    let reviewPackage: HeldDescriptor
    let observationAuthorization: HeldDescriptor
    let observationSignature: HeldDescriptor
    let keyAPublic: HeldDescriptor

    private init(root: HeldDescriptor, reviewPackage: HeldDescriptor,
                 observationAuthorization: HeldDescriptor,
                 observationSignature: HeldDescriptor, keyAPublic: HeldDescriptor) throws {
        self.root = root
        self.reviewPackage = reviewPackage
        self.observationAuthorization = observationAuthorization
        self.observationSignature = observationSignature
        self.keyAPublic = keyAPublic
        try revalidate()
    }

    func revalidate() throws {
        // Every handoff consumer repeats the boundary check. Each held child is rebound against
        // the retained handoff root and must still name the exact object that was opened.
        let checks: [(HeldDescriptor, String)] = [
            (reviewPackage, "review-package-v2.json"),
            (observationAuthorization, "observation-authorization-v2.json"),
            (observationSignature, "observation-authorization-v2.ed25519"),
            (keyAPublic, "key-a-public")
        ]
        for (descriptor, name) in checks {
            do {
                let rebound = try descriptor.rebind(in: root, name: name)
                guard rebound.device == descriptor.device, rebound.inode == descriptor.inode else {
                    throw CeremonyAdapterFailure.descriptorRebind
                }
            } catch {
                throw CeremonyAdapterFailure.descriptorRebind
            }
        }
    }

    static func make(root: HeldDescriptor, reviewPackage: HeldDescriptor,
                     observationAuthorization: HeldDescriptor,
                     observationSignature: HeldDescriptor, keyAPublic: HeldDescriptor) throws -> HeldCeremonyHandoff {
        try HeldCeremonyHandoff(root: root, reviewPackage: reviewPackage,
                                observationAuthorization: observationAuthorization,
                                observationSignature: observationSignature, keyAPublic: keyAPublic)
    }

    #if DEBUG
    static func fixture(root: URL) throws -> HeldCeremonyHandoff {
        let descriptor = try POSIX.openDirectory(root.path)
        let review = try POSIX.openAt(descriptor, name: "review-package-v2.json", flags: O_RDONLY)
        let authorization = try POSIX.openAt(descriptor, name: "observation-authorization-v2.json", flags: O_RDONLY)
        let signature = try POSIX.openAt(descriptor, name: "observation-authorization-v2.ed25519", flags: O_RDONLY)
        let key = try POSIX.openAt(descriptor, name: "key-a-public", flags: O_RDONLY)
        return try HeldCeremonyHandoff.make(root: descriptor, reviewPackage: review,
                                            observationAuthorization: authorization,
                                            observationSignature: signature, keyAPublic: key)
    }
    #endif
}

protocol CeremonyHandoffStore {
    func openAndRebind() throws -> HeldCeremonyHandoff
}

#if DEBUG
extension HeldBootstrapApproval {
    static func fixture(root: URL) throws -> HeldBootstrapApproval {
        let bundle = try ValidatedBundle.fixture(root: root)
        return HeldBootstrapApproval(descriptor: try POSIX.openDirectory(root.path), bytes: [], approval: bundle.approval)
    }
}
#endif

struct MaterializationAdapters {
    private let identityCheck: () throws -> HeldBootstrapApproval
    private let storageCheck: () throws -> Void
    private let bundleCheck: (MaterializationRequest, HeldBootstrapApproval) throws -> ValidatedBundle

    init() {
        identityCheck = { try InstalledHelper.requireRootAndIdentity() }
        storageCheck = { try StoragePreflight.verifyFixedParents() }
        bundleCheck = { try HeldBundle.openAndValidate($0, approval: $1) }
    }
    init(identityCheck: @escaping () throws -> HeldBootstrapApproval, storageCheck: @escaping () throws -> Void, bundleCheck: @escaping (MaterializationRequest, HeldBootstrapApproval) throws -> ValidatedBundle) {
        self.identityCheck = identityCheck; self.storageCheck = storageCheck; self.bundleCheck = bundleCheck
    }
    func verify(_ request: MaterializationRequest) throws -> ValidatedBundle {
        let approval = try identityCheck(); try storageCheck(); return try bundleCheck(request, approval)
    }
}

enum InstalledHelper {
    static func requireRootAndIdentity() throws -> HeldBootstrapApproval {
        guard geteuid() == 0 else { throw MaterializationFailure.identity }
        let environment = ProcessInfo.processInfo.environment
        guard !environment.keys.contains(where: { $0 == "PATH" || $0 == "PWD" || $0 == "HOME" || $0.hasPrefix("PYTHON") || $0.hasPrefix("PIP_") || $0.hasPrefix("DYLD_") || $0 == "VIRTUAL_ENV" }) else { throw MaterializationFailure.environment }
        try POSIX.closeInheritedDescriptors()
        let helper = DarwinBootstrapAdapters.installedProvisionerURL
        guard let loaded = Bundle.main.executableURL else { throw MaterializationFailure.identity }
        let approvalURL = URL(fileURLWithPath: ProtectedExecutionConstants.productionRoot).appendingPathComponent("bootstrap/task14-approval-v2.json")
        let approvalHeld = try POSIX.openTrustedRootOwnedRegular(approvalURL.path)
        let approvalBytes = try POSIX.readAll(approvalHeld.file)
        let approval = try Task14ApprovalV2.decodeDocument(approvalBytes)
        let held = try POSIX.openTrustedRootOwnedRegular(helper.path)
        let executing = try POSIX.openTrustedRootOwnedRegular(loaded.path)
        guard held.file.device == executing.file.device, held.file.inode == executing.file.inode else { throw MaterializationFailure.identity }
        let bytes = try POSIX.readAll(held.file)
        guard CanonicalJSON.sha256(bytes) == approval.provisionerBinarySHA256.value else { throw MaterializationFailure.identity }
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(helper as CFURL, SecCSFlags(), &code) == errSecSuccess,
              let code, SecStaticCodeCheckValidity(code, SecCSFlags(), nil) == errSecSuccess else { throw MaterializationFailure.identity }
        let bootstrapRoot = try POSIX.openTrustedRootOwnedDirectory(ProtectedExecutionConstants.productionRoot)
        let assessment = try DarwinBootstrapAdapters.securityAssessment(root: bootstrapRoot, at: helper)
        guard CanonicalJSON.sha256(assessment.designatedRequirementBytes) == approval.provisionerDesignatedRequirementSHA256.value,
              assessment.teamID == approval.helperTeamID, assessment.signingIdentitySHA1 == approval.helperSigningIdentitySHA1 else { throw MaterializationFailure.identity }
        _ = try held.file.rebind(in: held.parent, name: held.name)
        _ = try executing.file.rebind(in: executing.parent, name: executing.name)
        _ = try approvalHeld.file.rebind(in: approvalHeld.parent, name: approvalHeld.name)
        let rebound = try DarwinBootstrapAdapters.securityAssessment(root: bootstrapRoot, at: helper)
        guard rebound.designatedRequirementBytes == assessment.designatedRequirementBytes,
              rebound.teamID == assessment.teamID, rebound.signingIdentitySHA1 == assessment.signingIdentitySHA1 else { throw MaterializationFailure.identity }
        return HeldBootstrapApproval(descriptor: approvalHeld.file, bytes: approvalBytes, approval: approval)
    }

    static func heldBootstrapApproval() throws -> HeldBootstrapApproval { try requireRootAndIdentity() }
}

enum StoragePreflight {
    struct Observation: Equatable {
        let path: String; let device: dev_t; let inode: ino_t; let filesystem: String
        let volumeUUID: String?; let descriptorVolumeUUID: String?; let descriptorUUIDAvailable: Bool; let local: Bool?; let internalVolume: Bool?; let removable: Bool?
        let readOnly: Bool?; let ubiquitous: Bool?; let xattrs: [String]

        init(path: String, device: dev_t, inode: ino_t, filesystem: String, volumeUUID: String?, descriptorVolumeUUID: String? = nil, descriptorUUIDAvailable: Bool = true, local: Bool?, internalVolume: Bool?, removable: Bool?, readOnly: Bool?, ubiquitous: Bool?, xattrs: [String]) {
            self.path = path; self.device = device; self.inode = inode; self.filesystem = filesystem
            self.volumeUUID = volumeUUID; self.descriptorVolumeUUID = descriptorVolumeUUID ?? volumeUUID; self.descriptorUUIDAvailable = descriptorUUIDAvailable
            self.local = local; self.internalVolume = internalVolume; self.removable = removable
            self.readOnly = readOnly; self.ubiquitous = ubiquitous; self.xattrs = xattrs
        }
    }

    static let fixedAncestors = ["/", "/Library", "/Library/Application Support", ProtectedExecutionConstants.productionParent, ProtectedExecutionConstants.productionRoot]

    static func verifyFixedParents() throws { _ = try attest() }

    /// Opaque descriptor capability for the fixed authority layout. It is minted only after the
    /// ancestor/volume checks below and deliberately has no public pathname initializer.
    static func attest() throws -> StorageAttestation {
        let before = try fixedAncestors.map(observe)
        try validate(before)
        let rootParent = try POSIX.openTrustedRootOwnedDirectory(ProtectedExecutionConstants.productionParent)
        let rootName = URL(fileURLWithPath: ProtectedExecutionConstants.productionRoot).lastPathComponent
        let root = try POSIX.openAt(rootParent, name: rootName, flags: O_RDONLY | O_DIRECTORY)
        _ = try root.rebindSameIdentity(in: rootParent, name: rootName, directory: true)
        try POSIX.requireDirectory(root, uid: 0, gid: 0, mode: 0o700)
        let control = try POSIX.openAt(root, name: "control", flags: O_RDONLY | O_DIRECTORY)
        let admissions = try POSIX.openAt(control, name: "admissions", flags: O_RDONLY | O_DIRECTORY)
        let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
        let work = try POSIX.openAt(root, name: "materialization-work", flags: O_RDONLY | O_DIRECTORY)
        let quarantine = try POSIX.openAt(root, name: "quarantine", flags: O_RDONLY | O_DIRECTORY)
        let authorities = try POSIX.openAt(root, name: "authorities", flags: O_RDONLY | O_DIRECTORY)
        _ = try control.rebindSameIdentity(in: root, name: "control", directory: true)
        try POSIX.requireDirectory(control, uid: 0, gid: 0, mode: 0o700)
        for (parent, name, directory) in [(control, "admissions", admissions), (control, "journals", journals), (root, "materialization-work", work), (root, "quarantine", quarantine), (root, "authorities", authorities)] {
            _ = try directory.rebindSameIdentity(in: parent, name: name, directory: true)
            try POSIX.requireDirectory(directory, uid: 0, gid: 0, mode: 0o700)
        }
        let after = try fixedAncestors.map(observe)
        guard before == after else { throw MaterializationFailure.storage }
        guard let volume = normalizedUUID(before.last?.descriptorVolumeUUID) else { throw MaterializationFailure.storage }
        return StorageAttestation(rootParent: rootParent, root: root, control: control, admissions: admissions, journals: journals, work: work, quarantine: quarantine, authorities: authorities, volumeUUID: volume)
    }

    /// Kept internal so operation-table tests can prove every rejection without creating or
    /// touching the production authority path.
    static func validate(_ observations: [Observation]) throws {
        guard observations.map(\.path) == fixedAncestors,
              let volume = normalizedUUID(observations.first?.volumeUUID),
              let descriptorVolume = normalizedUUID(observations.first?.descriptorVolumeUUID),
              volume == descriptorVolume else { throw MaterializationFailure.storage }
        for observation in observations {
            guard observation.filesystem == "apfs", observation.descriptorUUIDAvailable,
                  normalizedUUID(observation.volumeUUID) == volume,
                  normalizedUUID(observation.descriptorVolumeUUID) == descriptorVolume,
                  normalizedUUID(observation.volumeUUID) == normalizedUUID(observation.descriptorVolumeUUID),
                  observation.local == true, observation.internalVolume == true,
                  observation.removable == false, observation.readOnly == false,
                  observation.ubiquitous == false,
                  !observation.xattrs.contains(where: isProviderXattr) else { throw MaterializationFailure.storage }
        }
    }

    private static func observe(_ path: String) throws -> Observation {
        let held = try POSIX.openTrustedRootOwnedDirectory(path)
        var identity = stat()
        guard fstat(held.fd, &identity) == 0 else { throw MaterializationFailure.storage }
        var statfsValue = statfs()
        guard fstatfs(held.fd, &statfsValue) == 0 else { throw MaterializationFailure.storage }
        let type = withUnsafePointer(to: &statfsValue.f_fstypename.0) { $0.withMemoryRebound(to: CChar.self, capacity: Int(MFSTYPENAMELEN)) { String(cString: $0) } }
        let values = try URL(fileURLWithPath: path).resourceValues(forKeys: [.volumeUUIDStringKey, .volumeIsLocalKey, .volumeIsReadOnlyKey, .volumeIsInternalKey, .volumeIsRemovableKey, .isUbiquitousItemKey])
        let byteCount = flistxattr(held.fd, nil, 0, 0)
        guard byteCount >= 0 else { throw MaterializationFailure.storage }
        var names = [CChar](repeating: 0, count: max(1, byteCount))
        guard flistxattr(held.fd, &names, names.count, 0) == byteCount else { throw MaterializationFailure.storage }
        let listed = names.split(separator: 0).map { String(decoding: $0.map(UInt8.init(bitPattern:)), as: UTF8.self) }
        if path != "/" {
            let parentPath = URL(fileURLWithPath: path).deletingLastPathComponent().path
            let rebound = try held.rebind(in: POSIX.openTrustedRootOwnedDirectory(parentPath), name: URL(fileURLWithPath: path).lastPathComponent, directory: true)
            guard rebound.device == held.device, rebound.inode == held.inode else { throw MaterializationFailure.storage }
        }
        return Observation(path: path, device: identity.st_dev, inode: identity.st_ino, filesystem: type, volumeUUID: values.volumeUUIDString, descriptorVolumeUUID: try descriptorVolumeUUID(held), local: values.volumeIsLocal, internalVolume: values.volumeIsInternal, removable: values.volumeIsRemovable, readOnly: values.volumeIsReadOnly, ubiquitous: values.isUbiquitousItem, xattrs: listed)
    }

    private static func descriptorVolumeUUID(_ held: HeldDescriptor) throws -> String {
        var attributes = attrlist()
        attributes.bitmapcount = UInt16(ATTR_BIT_MAP_COUNT)
        attributes.volattr = UInt32(ATTR_VOL_UUID)
        var bytes = [UInt8](repeating: 0, count: 20)
        let result = bytes.withUnsafeMutableBytes { raw in
            withUnsafeMutablePointer(to: &attributes) { list in
                fgetattrlist(held.fd, list, raw.baseAddress, raw.count, 0)
            }
        }
        guard result == 0 else { throw MaterializationFailure.storage }
        let length = UInt32(bytes[0]) | (UInt32(bytes[1]) << 8) | (UInt32(bytes[2]) << 16) | (UInt32(bytes[3]) << 24)
        let uuid = Array(bytes[4..<20])
        guard length >= 20, uuid.contains(where: { $0 != 0 }) else { throw MaterializationFailure.storage }
        let hex = uuid.map { String(format: "%02x", $0) }.joined()
        let value = "\(hex.prefix(8))-\(hex.dropFirst(8).prefix(4))-\(hex.dropFirst(12).prefix(4))-\(hex.dropFirst(16).prefix(4))-\(hex.dropFirst(20))"
        guard normalizedUUID(value) != nil else { throw MaterializationFailure.storage }
        return value
    }

    private static func normalizedUUID(_ value: String?) -> String? {
        guard let value, let uuid = UUID(uuidString: value) else { return nil }
        return uuid.uuidString.lowercased()
    }

    private static func isProviderXattr(_ name: String) -> Bool {
        let lower = name.unicodeScalars.map { scalar -> UnicodeScalar in
            guard scalar.value >= 0x41, scalar.value <= 0x5a else { return scalar }
            return UnicodeScalar(scalar.value + 0x20)!
        }
        let normalized = String(String.UnicodeScalarView(lower))
        func namespace(_ base: String) -> Bool { normalized == base || normalized.hasPrefix(base + ".") }
        return namespace("com.apple.fileprovider") || namespace("com.apple.icloud") || namespace("com.apple.file-provider-domain-id")
    }
}

/// Kept internal so only a successful fixed-parent preflight can produce mutation capability.
final class StorageAttestation {
    let rootParent: HeldDescriptor
    let root: HeldDescriptor
    let control: HeldDescriptor
    let admissions: HeldDescriptor
    let journals: HeldDescriptor
    let work: HeldDescriptor
    let quarantine: HeldDescriptor
    let authorities: HeldDescriptor
    let volumeUUID: String
    init(rootParent: HeldDescriptor, root: HeldDescriptor, control: HeldDescriptor, admissions: HeldDescriptor, journals: HeldDescriptor, work: HeldDescriptor, quarantine: HeldDescriptor, authorities: HeldDescriptor, volumeUUID: String) {
        self.rootParent = rootParent; self.root = root; self.control = control; self.admissions = admissions; self.journals = journals; self.work = work; self.quarantine = quarantine; self.authorities = authorities; self.volumeUUID = volumeUUID
    }

    func revalidateMutableRegistries() throws {
        let rootName = URL(fileURLWithPath: ProtectedExecutionConstants.productionRoot).lastPathComponent
        _ = try root.rebindSameIdentity(in: rootParent, name: rootName, directory: true)
        try POSIX.requireDirectory(root, uid: 0, gid: 0, mode: 0o700)
        _ = try control.rebindSameIdentity(in: root, name: "control", directory: true)
        try POSIX.requireDirectory(control, uid: 0, gid: 0, mode: 0o700)
        for (parent, name, descriptor) in [(control, "admissions", admissions), (control, "journals", journals), (root, "materialization-work", work), (root, "quarantine", quarantine), (root, "authorities", authorities)] {
            _ = try descriptor.rebindSameIdentity(in: parent, name: name, directory: true)
            try POSIX.requireDirectory(descriptor, uid: 0, gid: 0, mode: 0o700)
        }
    }
}

/// A held admissions-directory descriptor is the one interprocess serialisation capability for
/// admission creation.  The lock is acquired before any registry snapshot and released only
/// after the exclusive admission creation succeeds or fails.  All authority writers must use
/// this same descriptor-relative boundary.
enum AdmissionRegistryLock {
    static func withExclusive<T>(_ directory: HeldDescriptor, _ body: () throws -> T) throws -> T {
        try acquire(directory, operation: LOCK_EX)
        do {
            let result = try body()
            try release(directory)
            return result
        } catch {
            // Preserve the admission/preflight error, while still making a bounded best effort to
            // relinquish the kernel capability. A release failure cannot turn the primary error
            // into a misleading success.
            _ = try? release(directory)
            throw error
        }
    }

    #if DEBUG
    /// Test-visible nonblocking form used to prove that independently opened descriptors obey
    /// the same kernel lock. A successful caller owns the lock and must unlock it.
    static func tryExclusive(_ directory: HeldDescriptor) throws -> Bool {
        do {
            try acquire(directory, operation: LOCK_EX | LOCK_NB)
            return true
        } catch POSIXFailure.system(let code) where code == EWOULDBLOCK || code == EAGAIN {
            return false
        }
    }
    #endif

    private static func acquire(_ directory: HeldDescriptor, operation: Int32) throws {
        for _ in 0..<8 {
            if flock(directory.fd, operation) == 0 { return }
            if errno != EINTR { throw POSIXFailure.system(errno) }
        }
        throw POSIXFailure.system(EINTR)
    }

    private static func release(_ directory: HeldDescriptor) throws {
        try acquire(directory, operation: LOCK_UN)
    }
}

/// Production storage uses only the descriptor capability retained in StorageAttestation.
final class DarwinAdmissionStorage: Storage {
    private let attestation: StorageAttestation
    init(attestation: StorageAttestation) { self.attestation = attestation }
    var storageVolumeUUID: String { attestation.volumeUUID }
    func nextAttemptUUID() -> String { UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased() }

    func preflightAndCreateExclusiveAdmission(attempt: String) throws -> HeldAdmissionRecord {
        let name = try Self.admissionName(attempt)
        return try AdmissionRegistryLock.withExclusive(attestation.admissions) {
            try attestation.revalidateMutableRegistries()
            let first = try registrySnapshots()
            try attestation.revalidateMutableRegistries()
            let second = try registrySnapshots()
            guard first == second, second.allEmpty else { throw AdmissionFailure.residue }
            do {
                return HeldAdmissionRecord(parent: attestation.admissions, name: name,
                                           file: try POSIX.createExclusive(attestation.admissions, name: name, mode: 0o600))
            } catch POSIXFailure.system(let code) where code == EEXIST {
                throw AdmissionFailure.alreadyAdmitted
            }
        }
    }

    private func registrySnapshots() throws -> RegistrySnapshots {
        let registries: [(HeldDescriptor, (String) throws -> Void)] = [
            (attestation.admissions, { _ = try Self.attempt(fromAdmissionName: $0) }),
            (attestation.journals, { try Self.requireAttempt($0) }),
            (attestation.work, { try Self.requireAttempt($0) }),
            (attestation.quarantine, { try Self.requireAttempt($0) }),
            (attestation.authorities, { guard $0 == ProtectedExecutionConstants.authorityID else { throw POSIXFailure.unsafeObject } })
        ]
        var snapshots: [Set<String>] = []
        for (registry, parse) in registries {
            let names = try POSIX.directoryEntryNames(registry)
            for name in names {
                do { try parse(name) }
                catch { throw AdmissionFailure.residue }
            }
            snapshots.append(names)
        }
        return RegistrySnapshots(entries: snapshots)
    }
    func writeAdmission(_ bytes: [UInt8], to record: HeldAdmissionRecord) throws { try require(record); try POSIX.writeAll(record.file, bytes) }
    func fsyncAdmission(_ record: HeldAdmissionRecord) throws { try require(record); try POSIX.durableRegular(record.file) }
    func chmodAdmissionReadOnly(_ record: HeldAdmissionRecord) throws { try require(record); guard Darwin.fchmod(record.file.fd, 0o444) == 0 else { throw POSIXFailure.system(errno) } }
    func immutableReadbackAdmission(_ record: HeldAdmissionRecord) throws { try require(record); try POSIX.setImmutable(record.file) }
    func rebindAdmission(_ record: HeldAdmissionRecord) throws { try require(record); _ = try record.file.rebind(in: record.parent, name: record.name) }
    func fsyncAdmissionsParent(_ record: HeldAdmissionRecord) throws { try require(record); try POSIX.durableDirectory(record.parent) }
    func markAdmitted() throws { }
    func quarantineAfterAdmissionFailure(_ record: HeldAdmissionRecord) throws {
        try require(record)
        // The immutable admission is retained descriptor-bound; a subsequent preflight rejects
        // it rather than adopting, deleting, or retrying it.
        try POSIX.durableRegular(record.file)
        try POSIX.durableDirectory(record.parent)
    }
    private func require(_ record: HeldAdmissionRecord) throws {
        _ = try Self.attempt(fromAdmissionName: record.name)
        guard record.parent.device == attestation.admissions.device, record.parent.inode == attestation.admissions.inode else { throw POSIXFailure.unsafeObject }
        _ = try record.file.rebindSameIdentity(in: record.parent, name: record.name)
    }

    private static func requireAttempt(_ name: String) throws {
        guard AdmissionController.isAttemptUUID(name) else { throw POSIXFailure.unsafeObject }
    }

    private static func attempt(fromAdmissionName name: String) throws -> String {
        guard name.hasSuffix(".json") else { throw POSIXFailure.unsafeObject }
        let attempt = String(name.dropLast(5))
        try requireAttempt(attempt)
        guard name == attempt + ".json" else { throw POSIXFailure.unsafeObject }
        return attempt
    }

    private static func admissionName(_ attempt: String) throws -> String {
        try AdmissionController.admissionName(attempt)
    }
}

private struct RegistrySnapshots: Equatable {
    let entries: [Set<String>]
    var allEmpty: Bool { entries.allSatisfy(\.isEmpty) }
}
