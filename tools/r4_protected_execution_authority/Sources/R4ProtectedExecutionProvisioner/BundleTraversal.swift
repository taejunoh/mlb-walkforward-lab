import Foundation

/// Ceremony evidence is admitted only after its root-owned handoff record has been opened and
/// every child descriptor rebound.  It is not accepted by materialization admission.
struct HeldCeremonyInputs {
    let handoff: HeldCeremonyHandoff
    var reviewPackage: HeldDescriptor { handoff.reviewPackage }
    var observationAuthorization: HeldDescriptor { handoff.observationAuthorization }
    var observationSignature: HeldDescriptor { handoff.observationSignature }
    var keyAPublic: HeldDescriptor { handoff.keyAPublic }

    init(handoff: HeldCeremonyHandoff) throws {
        try handoff.revalidate()
        self.handoff = handoff
    }

    init(store: CeremonyHandoffStore) throws {
        try self.init(handoff: store.openAndRebind())
    }
}

struct ValidatedBundle {
    let root: URL
    let manifest: InputBundleManifest
    let lockBytes: [UInt8]
    let policy: MaterializationPolicy
    let approval: Task14ApprovalV2
    /// The accepted final approval remains descriptor-bound through admission.  Its V2 fields
    /// carry the Task 14 review-package, observation-authorization, and compiled Key-A links;
    /// materialization never reopens a caller-selected approval pathname.
    let heldApproval: HeldBootstrapApproval
    let held: HeldBundle
    var manifestDigest: String { manifest.sha256.value }

    #if DEBUG
    /// Test-target construction only; production enters through `openAndValidate`.
    static func fixture(root: URL) throws -> ValidatedBundle {
        let digest = try SHA256Digest(String(repeating: "a", count: 64))
        let git = try GitEvidence(head: GitObjectID(String(repeating: "a", count: 40)), tree: GitObjectID(String(repeating: "b", count: 40)), requiredBlobs: [])
        let manifest = InputBundleManifest(lockSHA256: digest, pythonArchiveFilename: "python.tar.gz", pythonArchiveVersion: "3.11.15", pythonArchiveURL: "https://example.invalid/python.tar.gz", pythonArchiveSHA256: digest, pythonArchiveSize: 0, wheelFilenames: [], wheelVersions: [], wheelURLs: [], wheelSHA256: [], wheelSizes: [], materializationPolicySHA256: digest, requiredBlobSetSHA256: digest, wheelSetSHA256: digest, git: git, entries: [], sha256: digest)
        let policy = MaterializationPolicy(wheelFilenames: [], pipArgv: [], importArgv: [], environment: [], timeoutSeconds: 0, maxOutputBytes: 0, importProbeSHA256: digest, sandboxProfileUTF8: "", sandboxProfileSHA256: digest, environmentSHA256: digest, workingDirectory: "runtime", sha256: digest)
        let approval = Task14ApprovalV2(reviewedHead: git.head, reviewedTree: git.tree, requiredBlobSetSHA256: digest, inputBundleManifestSHA256: digest, lockSHA256: digest, pythonArchiveSHA256: digest, wheelSetSHA256: digest, materializationPolicySHA256: digest, provisionerBinarySHA256: digest, provisionerDesignatedRequirementSHA256: digest, helperTeamID: "3BMF4LM6TM", helperSigningIdentitySHA1: "4C6503D0A78A9F51C86CC7C9683115E74A4902A7", sha256: digest)
        let held = HeldBundle(root: root, descriptor: try POSIX.openDirectory(root.path))
        return ValidatedBundle(root: root, manifest: manifest, lockBytes: [], policy: policy, approval: approval,
                               heldApproval: HeldBootstrapApproval(descriptor: held.descriptor, bytes: [], approval: approval), held: held)
    }
    #endif
}

enum BundleVerifier {
    static func verifyBeforeAdmission(_ request: MaterializationRequest, adapters: MaterializationAdapters = MaterializationAdapters()) throws -> ValidatedBundle {
        try adapters.verify(request)
    }
}

final class HeldBundle {
    static let childNames: Set<String> = ["R4_RUNTIME_INPUT_BUNDLE_V2.json", "r4_protected_python311.lock.json", "materialization-policy.json", "checkout", "artifacts"]
    let root: URL
    let descriptor: HeldDescriptor
    private(set) var children: [String: HeldDescriptor] = [:]
    /// Digest snapshots are captured while the V2 graph is validated.  The private-copy phase
    /// compares its held source before and after every descriptor-relative copy; it never trusts
    /// a later pathname lookup to tell it what was approved.
    private var fileDigests: [String: String] = [:]
    private var capturedMetadata: [String: DescriptorMetadata] = [:]
    fileprivate init(root: URL, descriptor: HeldDescriptor) { self.root = root; self.descriptor = descriptor }
    fileprivate func retain(_ descriptor: HeldDescriptor, at path: String, digest: String? = nil) {
        children[path] = descriptor
        if let digest { fileDigests[path] = digest }
        capturedMetadata[path] = try? POSIX.metadata(descriptor)
    }
    struct SourceCapability {
        let descriptor: HeldDescriptor; let digest: String?; let metadata: DescriptorMetadata
    }
    func source(at path: String) -> HeldDescriptor? { children[path] }
    func sourceCapability(at path: String) throws -> SourceCapability? {
        guard let descriptor = children[path] else { return nil }
        guard let metadata = capturedMetadata[path] else { throw POSIXFailure.unsafeObject }
        return SourceCapability(descriptor: descriptor, digest: fileDigests[path], metadata: metadata)
    }
    func digest(at path: String) -> String? { fileDigests[path] }
    func immediateChildren(of path: String) -> [String] {
        let prefix = path.isEmpty ? "" : path + "/"
        return children.keys.compactMap { candidate in
            guard candidate.hasPrefix(prefix) else { return nil }
            let remainder = String(candidate.dropFirst(prefix.count))
            return remainder.contains("/") ? nil : remainder
        }.sorted()
    }

    static func openAndValidate(_ request: MaterializationRequest, approval heldApproval: HeldBootstrapApproval) throws -> ValidatedBundle {
        // Do not compare Foundation's standardized URL spelling here: on Darwin it rewrites the
        // real `/private/tmp` directory to the `/tmp` alias.  `openDirectory` performs the
        // authoritative lexical component walk with `O_NOFOLLOW`, rejecting `.` and `..`.
        guard request.inputBundle.path.hasPrefix("/") else { throw MaterializationFailure.inputPath }
        let root = try POSIX.openDirectory(request.inputBundle.path)
        guard try POSIX.directoryEntryNames(root) == childNames else { throw MaterializationFailure.layout }
        let held = HeldBundle(root: request.inputBundle, descriptor: root)
        let manifestFile = try POSIX.openAt(root, name: "R4_RUNTIME_INPUT_BUNDLE_V2.json", flags: O_RDONLY)
        let lockFile = try POSIX.openAt(root, name: "r4_protected_python311.lock.json", flags: O_RDONLY)
        let policyFile = try POSIX.openAt(root, name: "materialization-policy.json", flags: O_RDONLY)
        let checkout = try POSIX.openAt(root, name: "checkout", flags: O_RDONLY | O_DIRECTORY)
        let artifacts = try POSIX.openAt(root, name: "artifacts", flags: O_RDONLY | O_DIRECTORY)
        held.children = ["R4_RUNTIME_INPUT_BUNDLE_V2.json": manifestFile, "r4_protected_python311.lock.json": lockFile, "materialization-policy.json": policyFile, "checkout": checkout, "artifacts": artifacts]
        for (path, descriptor) in held.children { held.capturedMetadata[path] = try POSIX.metadata(descriptor) }
        for file in [manifestFile, lockFile, policyFile] { do { try requirePrivateRegular(file) } catch { throw MaterializationFailure.metadata } }
        for directory in [checkout, artifacts] { do { try requirePrivateDirectory(directory) } catch { throw MaterializationFailure.metadata } }
        let manifestBytes = try POSIX.readAll(manifestFile)
        let lockBytes = try POSIX.readAll(lockFile)
        let policyBytes = try POSIX.readAll(policyFile)
        held.fileDigests["R4_RUNTIME_INPUT_BUNDLE_V2.json"] = CanonicalJSON.sha256(manifestBytes)
        held.fileDigests["r4_protected_python311.lock.json"] = CanonicalJSON.sha256(lockBytes)
        held.fileDigests["materialization-policy.json"] = CanonicalJSON.sha256(policyBytes)
        let manifest = try InputBundleManifest.decodeDocument(manifestBytes)
        let policy = try MaterializationPolicy.decodeDocument(policyBytes)
        let approval = heldApproval.approval
        let lock = try ProtectedPythonLock.decodeDocument(lockBytes)
        guard CanonicalJSON.sha256(lockBytes) == manifest.lockSHA256.value else { throw MaterializationFailure.lockDigest }
        // V2 JSON documents bind their canonical self-digest (the `sha256` field omitted),
        // which `MaterializationPolicy.decode` has already verified.  The V1 lock instead binds
        // its complete held bytes because it has no V2 self-digest field.
        guard policy.sha256 == manifest.materializationPolicySHA256 else { throw MaterializationFailure.policyDigest }
        guard manifest.sha256 == approval.inputBundleManifestSHA256 else { throw MaterializationFailure.approvalManifest }
        guard manifest.lockSHA256 == approval.lockSHA256 else { throw MaterializationFailure.approvalLock }
        guard manifest.pythonArchiveSHA256 == approval.pythonArchiveSHA256 else { throw MaterializationFailure.approvalArchive }
        guard manifest.wheelSetSHA256 == approval.wheelSetSHA256 else { throw MaterializationFailure.approvalWheelSet }
        guard policy.sha256 == approval.materializationPolicySHA256 else { throw MaterializationFailure.approvalPolicy }
        guard approval.reviewedHead == manifest.git.head,
              approval.reviewedTree == manifest.git.tree,
              approval.requiredBlobSetSHA256 == manifest.requiredBlobSetSHA256 else { throw MaterializationFailure.graph }
        guard policy.wheelFilenames == manifest.wheelFilenames else { throw MaterializationFailure.wheelNames }
        guard lock.python.filename == manifest.pythonArchiveFilename,
              lock.python.version == manifest.pythonArchiveVersion,
              lock.python.url == manifest.pythonArchiveURL,
              lock.python.sha256 == manifest.pythonArchiveSHA256 else { throw MaterializationFailure.lockArchive }
        guard lock.wheels.map(\.filename) == manifest.wheelFilenames,
              lock.wheels.map(\.url) == manifest.wheelURLs,
              lock.wheels.map(\.sha256) == manifest.wheelSHA256 else { throw MaterializationFailure.lockWheels }
        try checkArtifacts(manifest, descriptor: artifacts, held: held)
        try checkCheckout(manifest, descriptor: checkout, held: held)
        _ = try manifestFile.rebind(in: root, name: "R4_RUNTIME_INPUT_BUNDLE_V2.json")
        _ = try lockFile.rebind(in: root, name: "r4_protected_python311.lock.json")
        _ = try policyFile.rebind(in: root, name: "materialization-policy.json")
        _ = try checkout.rebind(in: root, name: "checkout", directory: true)
        _ = try artifacts.rebind(in: root, name: "artifacts", directory: true)
        return ValidatedBundle(root: request.inputBundle, manifest: manifest, lockBytes: lockBytes, policy: policy,
                               approval: approval, heldApproval: heldApproval, held: held)
    }

    private static func checkArtifacts(_ manifest: InputBundleManifest, descriptor: HeldDescriptor, held: HeldBundle) throws {
        let expected = Set([manifest.pythonArchiveFilename] + manifest.wheelFilenames)
        guard try POSIX.directoryEntryNames(descriptor) == expected else { throw MaterializationFailure.artifact }
        let artifactNames = [manifest.pythonArchiveFilename] + manifest.wheelFilenames
        for (index, name) in artifactNames.enumerated() {
            let file = try POSIX.openAt(descriptor, name: name, flags: O_RDONLY)
            do { try requirePrivateRegular(file) } catch { throw MaterializationFailure.artifactMetadata }
            let expectedDigest = index == 0 ? manifest.pythonArchiveSHA256.value : manifest.wheelSHA256[index - 1].value
            let expectedSize = index == 0 ? manifest.pythonArchiveSize : manifest.wheelSizes[index - 1]
            guard exactSize(file) == expectedSize,
                  CanonicalJSON.sha256(try POSIX.readAll(file)) == expectedDigest else { throw MaterializationFailure.artifact }
            _ = try file.rebind(in: descriptor, name: name)
            held.retain(file, at: "artifacts/\(name)", digest: expectedDigest)
        }
    }
    private static func checkCheckout(_ manifest: InputBundleManifest, descriptor: HeldDescriptor, held: HeldBundle) throws {
        let rows = manifest.entries
        try validateCheckoutDirectory(path: "checkout", descriptor: descriptor, rows: rows, held: held)
    }
    private static func validateCheckoutDirectory(path: String, descriptor: HeldDescriptor, rows: [ManifestEntry], held: HeldBundle) throws {
        let prefix = path + "/"
        let children = rows.filter { row in
            guard row.path.hasPrefix(prefix) else { return false }
            return !row.path.dropFirst(prefix.count).contains("/")
        }
        guard try POSIX.directoryEntryNames(descriptor) == Set(children.map { String($0.path.dropFirst(prefix.count)) }) else { throw MaterializationFailure.checkout }
        for row in children {
            let name = String(row.path.dropFirst(prefix.count))
            let file = try POSIX.openAt(descriptor, name: name, flags: O_RDONLY | (row.type == "directory" ? O_DIRECTORY : 0))
            if row.type == "directory" {
                do { try requirePrivateDirectory(file) } catch { throw MaterializationFailure.checkoutMetadata }
                try validateCheckoutDirectory(path: row.path, descriptor: file, rows: rows, held: held)
            } else {
                do { try requirePrivateRegular(file) } catch { throw MaterializationFailure.checkoutMetadata }
                guard exactSize(file) == row.size,
                      CanonicalJSON.sha256(try POSIX.readAll(file)) == row.sha256.value else { throw MaterializationFailure.checkout }
            }
            _ = try file.rebind(in: descriptor, name: name, directory: row.type == "directory")
            held.retain(file, at: row.path, digest: row.type == "regular" ? row.sha256.value : nil)
        }
    }
    private static func requirePrivateRegular(_ descriptor: HeldDescriptor) throws {
        var s = stat(); guard fstat(descriptor.fd, &s) == 0, (s.st_mode & S_IFMT) == S_IFREG, s.st_nlink == 1, (s.st_mode & 0o022) == 0 else { throw MaterializationFailure.bundle }
    }
    private static func requirePrivateDirectory(_ descriptor: HeldDescriptor) throws {
        var s = stat(); guard fstat(descriptor.fd, &s) == 0, (s.st_mode & S_IFMT) == S_IFDIR, (s.st_mode & 0o022) == 0 else { throw MaterializationFailure.bundle }
    }
    private static func exactSize(_ descriptor: HeldDescriptor) -> Int64 {
        var s = stat(); guard fstat(descriptor.fd, &s) == 0 else { return -1 }
        return Int64(s.st_size)
    }
}
