import Foundation
import Dispatch
import Darwin
@testable import R4ProtectedExecutionProvisioner

// These types intentionally live in the test target.  They model the seams consumed by later
// materialization tests without widening the production helper's filesystem or process surface.

enum FakeAdapterError: Error, Equatable {
    case outsideFixtureRoot
    case unapprovedExecutable
    case invocationMismatch
    case alreadyExists
    case injectedFault(String)
    case barrierTimeout
}

struct FixtureOperationLog: Equatable {
    var launchPrograms: [String] = []
    var accessedPaths: [String] = []
}

/// Disposable descriptor fixture for tests that exercise the real Darwin metadata syscalls.
/// It never resolves or mutates a production path and cleanup adopts only the exact directory
/// created by `create`.
final class DarwinDisposableRoot {
    let root: URL
    private let descriptor: HeldDescriptor
    private let identity: (dev_t, ino_t)
    private let cleanupPrefix: String

    private init(root: URL, descriptor: HeldDescriptor, identity: (dev_t, ino_t), cleanupPrefix: String) {
        self.root = root
        self.descriptor = descriptor
        self.identity = identity
        self.cleanupPrefix = cleanupPrefix
    }

    static func create(prefix: String) throws -> DarwinDisposableRoot {
        let name = prefix + UUID().uuidString.lowercased()
        let root = URL(fileURLWithPath: "/private/tmp").appendingPathComponent(name)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false,
                                                 attributes: [.posixPermissions: 0o700])
        let descriptor = try POSIX.openDirectory(root.path)
        var status = stat()
        guard fstat(descriptor.fd, &status) == 0 else { throw POSIXFailure.system(errno) }
        return DarwinDisposableRoot(root: root, descriptor: descriptor,
                                    identity: (status.st_dev, status.st_ino), cleanupPrefix: prefix)
    }

    /// Descriptor-only fixture capability.  It deliberately has no path reconstruction API.
    var heldRoot: HeldDescriptor { descriptor }

    func fakeMetadata(_ defect: String) throws -> HeldDescriptor {
        let name = "object-" + UUID().uuidString.lowercased()
        let file = try POSIX.createExclusive(descriptor, name: name, mode: 0o600)
        try POSIX.writeAll(file, Array("sealed".utf8))
        switch defect {
        case "nlink":
            let alias = name + "-alias"
            guard linkat(descriptor.fd, name, descriptor.fd, alias, 0) == 0 else {
                throw POSIXFailure.system(errno)
            }
        case "acl":
            var aclValue: acl_t? = acl_init(1)
            guard let acl = aclValue else { throw POSIXFailure.system(errno) }
            defer { _ = acl_free(UnsafeMutableRawPointer(acl)) }
            var entry: acl_entry_t?
            guard acl_create_entry(&aclValue, &entry) == 0, let entry,
                  acl_set_tag_type(entry, ACL_EXTENDED_ALLOW) == 0,
                  acl_set_fd_np(file.fd, acl, ACL_TYPE_EXTENDED) == 0 else {
                throw POSIXFailure.system(errno == 0 ? EIO : errno)
            }
        case "xattr":
            let marker: [UInt8] = [1]
            let result = marker.withUnsafeBytes { bytes in
                fsetxattr(file.fd, "com.apple.fileprovider.r4-test", bytes.baseAddress, bytes.count, 0, 0)
            }
            guard result == 0 else { throw POSIXFailure.system(errno) }
        case "immutable":
            break
        default:
            throw POSIXFailure.unsafeObject
        }
        guard fchmod(file.fd, 0o444) == 0 else { throw POSIXFailure.system(errno) }
        return file
    }

    func removeOnlySelfOwnedFixture() throws {
        guard root.path.hasPrefix("/private/tmp/" + cleanupPrefix) else { return }
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: root.path),
              let device = attributes[.systemNumber] as? NSNumber,
              let inode = attributes[.systemFileNumber] as? NSNumber,
              device.uint64Value == UInt64(identity.0), inode.uint64Value == UInt64(identity.1) else { return }
        // Task 9/10 deliberately seals descendants before publication.  This fixture owns
        // the root by inode, so DEBUG-only cleanup first relaxes only descriptor-rebound
        // descendants; it never follows a symlink or adopts a pathname outside this tree.
        try makeOwnedTreeRemovable(descriptor)
        try FileManager.default.removeItem(at: root)
    }

    private func makeOwnedTreeRemovable(_ directory: HeldDescriptor) throws {
        for name in try POSIX.directoryEntryNames(directory) {
            let child = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(child)
            if metadata.type == S_IFDIR {
                try makeOwnedTreeRemovable(child)
                guard fchmod(child.fd, 0o700) == 0 else { throw POSIXFailure.system(errno) }
                _ = try child.rebindSameIdentity(in: directory, name: name, directory: true)
            } else if metadata.type == S_IFREG {
                guard fchmod(child.fd, 0o600) == 0 else { throw POSIXFailure.system(errno) }
                _ = try child.rebindSameIdentity(in: directory, name: name)
            } else {
                throw POSIXFailure.unsafeObject
            }
        }
        guard fchmod(directory.fd, 0o700) == 0 else { throw POSIXFailure.system(errno) }
    }
}

enum FixtureMutation: String, CaseIterable, Hashable {
    case v1Bundle, mixedGraph, wrongApproval, badLock, extraChild, swappedArchive, swappedWheel, swappedCheckout
    case symlink, hardlink, caseCollision, nonRootEUID, copiedHelper, badStorage, forbiddenEnvironment, inheritedDescriptor
    case swapBeforeCopy
}

enum AdmissionFault: String, CaseIterable, Hashable {
    case ownerDeath, admissionFsync, admissionRebind, beforeOpen, afterOpen
}

enum FixtureFault: String, CaseIterable, Hashable { case copy, create, rename, fsync }

enum FixtureInitFault: String, CaseIterable, Hashable {
    case afterRoot, afterControl, afterBundle, afterApproval, afterHelper, afterArchive, afterManifest, afterStage
}

protocol FixtureFaultSeam { var fixtureFault: FixtureFault { get } }
protocol FixtureAdmissionFaultSeam { var fixtureAdmissionFault: AdmissionFault { get } }
protocol FixtureSwapSeam { var fixtureSwap: FixtureFault { get } }

extension FixtureFault: FixtureFaultSeam {
    var fixtureFault: FixtureFault { self }
}
extension FixtureFault: FixtureSwapSeam {
    var fixtureSwap: FixtureFault { self }
}
extension AdmissionFault: FixtureAdmissionFaultSeam {
    var fixtureAdmissionFault: AdmissionFault { self }
}

extension StateMachine.Seam: FixtureFaultSeam {
    var fixtureFault: FixtureFault {
        // Durable-state faults are injected by FakeDurableStore.  Keep the generic file fault
        // inert through admission so its typed seam cannot alter pre-admission ordering.
        .copy
    }
}

extension AdmissionSeam: FixtureAdmissionFaultSeam {
    var fixtureAdmissionFault: AdmissionFault { AdmissionFault(rawValue: rawValue)! }
}

extension FixtureMutation {
    static var bundleExtraChild: Self { .extraChild }
}

enum FakeFileSystemEvent: Equatable {
    case open, create, admissionBarrierArrival, openExclusiveAttempt, openExclusive, writeAdmission, rename, fsync, fsyncAdmission, fsyncAdmissionsParent
    case admissionOwnerDeath, admissionFsyncFailure, admissionRebindFailure
    case chmod, chflags, immutableAdmission, acl, volume, security
    case mkdir, unlink, rebind
}

final class FakeFileSystem {
    let root: URL
    private let rootHandle: HeldDescriptor?
    private var admissionsHandle: HeldDescriptor?
    private let capturedIdentity: (NSNumber, NSNumber)?
    private let lock = NSRecursiveLock()
    private let exclusiveAttemptCondition = NSCondition()
    private(set) var events: [FakeFileSystemEvent] = []
    private(set) var eventPaths: [(FakeFileSystemEvent, String)] = []
    private(set) var admissionRecords: [Data] = []
    private(set) var loserCleanupCalls = 0
    private(set) var loserMutations = 0
    var fault: FixtureFault?
    private(set) var swapBeforeCopyArmed = false
    private(set) var mutations: Set<FixtureMutation> = []
    private(set) var quarantineState = false
    private(set) var retainedWorkState = false
    private(set) var storageRejected = false
    private var exclusiveAttemptGateArmed = false
    private var exclusiveAttemptCount = 0
    private weak var exclusiveAttemptBarrier: AdmissionBarrier?

    init(root: URL) {
        self.root = root
        guard let handle = try? POSIX.openDirectory(self.root.path) else { preconditionFailure("fixture root descriptor open failed") }
        self.rootHandle = handle
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: self.root.path),
              let device = attrs[.systemNumber] as? NSNumber,
              let inode = attrs[.systemFileNumber] as? NSNumber else { preconditionFailure("fixture root identity unavailable") }
        capturedIdentity = (device, inode)
    }

    func mkdir(_ path: String) throws {
        let parts = try components(path)
        guard let rootHandle else { throw FakeAdapterError.outsideFixtureRoot }
        var parent = rootHandle
        for component in parts {
            do { parent = try POSIX.openAt(parent, name: component, flags: O_RDONLY | O_DIRECTORY) }
            catch POSIXFailure.system(let code) where code == ENOENT { parent = try POSIX.mkdirAt(parent, name: component, mode: 0o700) }
        }
        record(.mkdir, URL(fileURLWithPath: path))
    }

    func addMutation(_ mutation: FixtureMutation) { lock.lock(); mutations.insert(mutation); if mutation == .swapBeforeCopy { swapBeforeCopyArmed = true }; lock.unlock() }
    func hasMutation(_ mutation: FixtureMutation) -> Bool { lock.lock(); defer { lock.unlock() }; return mutations.contains(mutation) || (mutation == .swapBeforeCopy && swapBeforeCopyArmed) }
    private func shouldFault(_ seam: FixtureFault) -> Bool { fault == seam || (seam == .copy && swapBeforeCopyArmed) }
    func markQuarantine() { lock.lock(); quarantineState = true; lock.unlock() }
    func markRetainedWork() { lock.lock(); retainedWorkState = true; lock.unlock() }
    func markStorageRejected() { lock.lock(); storageRejected = true; lock.unlock() }

    func createFile(at path: String, bytes: [UInt8] = []) throws {
        let (parent, name) = try parentAndName(path)
        let file = try POSIX.createExclusive(parent, name: name, mode: 0o600)
        try POSIX.writeAll(file, bytes)
        record(.create, URL(fileURLWithPath: path))
    }

    func open(_ path: String, forMutation: Bool = false) throws -> URL {
        if shouldFault(.copy) { throw FakeAdapterError.injectedFault(FixtureFault.copy.rawValue) }
        _ = try descriptor(path, flags: forMutation ? O_RDWR : O_RDONLY)
        let url = URL(fileURLWithPath: path); record(.open, url); return url
    }

    func createExclusive(_ path: String, mode: UInt16 = 0o600) throws -> URL {
        if shouldFault(.create) { throw FakeAdapterError.injectedFault(FixtureFault.create.rawValue) }
        let url = URL(fileURLWithPath: path)
        let (parent, name) = try parentAndName(path)
        record(.openExclusiveAttempt, url)
        noteExclusiveAttempt()
        exclusiveAttemptCondition.lock()
        if exclusiveAttemptGateArmed {
            let deadline = Date().addingTimeInterval(2)
            while exclusiveAttemptCount < 2,
                  !(exclusiveAttemptBarrier?.isCancelled ?? true),
                  Date() < deadline {
                exclusiveAttemptCondition.wait(until: Date().addingTimeInterval(0.01))
            }
            if exclusiveAttemptCount < 2 { exclusiveAttemptGateArmed = false }
        }
        exclusiveAttemptCondition.unlock()
        do { _ = try POSIX.createExclusive(parent, name: name, mode: mode_t(mode)) }
        catch POSIXFailure.system(let code) where code == EEXIST { throw FakeAdapterError.alreadyExists }
        record(.openExclusive, url)
        return URL(fileURLWithPath: path)
    }

    func armExclusiveAttemptGate(using barrier: AdmissionBarrier) {
        exclusiveAttemptCondition.lock()
        if !exclusiveAttemptGateArmed {
            exclusiveAttemptGateArmed = true
            exclusiveAttemptBarrier = barrier
            exclusiveAttemptCount = 0
        }
        exclusiveAttemptCondition.unlock()
    }

    private func noteExclusiveAttempt() {
        exclusiveAttemptCondition.lock()
        exclusiveAttemptCount += 1
        exclusiveAttemptCondition.broadcast()
        exclusiveAttemptCondition.unlock()
    }

    func writeAdmission(_ data: Data, to path: String) throws {
        let file = try descriptor(path, flags: O_WRONLY)
        try POSIX.writeAll(file, Array(data))
        lock.lock(); admissionRecords.append(data); lock.unlock()
        record(.writeAdmission, URL(fileURLWithPath: path))
    }

    func rename(_ source: String, to destination: String) throws {
        if shouldFault(.rename) { throw FakeAdapterError.injectedFault(FixtureFault.rename.rawValue) }
        let (fromParent, fromName) = try parentAndName(source)
        let (toParent, toName) = try parentAndName(destination)
        try POSIX.renameExclusive(from: fromParent, name: fromName, to: toParent, destination: toName)
        let to = URL(fileURLWithPath: destination)
        record(.rename, to)
    }

    func fsync(_ path: String, admission: Bool = false) throws {
        let descriptor = try self.descriptor(path, flags: O_RDONLY)
        if fault == .fsync || (admission && fault == .fsync) { throw FakeAdapterError.injectedFault(FixtureFault.fsync.rawValue) }
        try POSIX.fsync(descriptor)
        record(admission ? .fsyncAdmission : .fsync, URL(fileURLWithPath: path))
    }
    func fsyncAdmissionsParent() throws {
        let path = root.appendingPathComponent("control/admissions")
        lock.lock()
        let descriptor: HeldDescriptor
        do {
            if admissionsHandle == nil { admissionsHandle = try self.descriptor(path.path, flags: O_RDONLY | O_DIRECTORY) }
            guard let retained = admissionsHandle else { lock.unlock(); throw FakeAdapterError.outsideFixtureRoot }
            descriptor = retained
        } catch {
            lock.unlock()
            throw error
        }
        lock.unlock()
        if fault == .fsync { throw FakeAdapterError.injectedFault(FixtureFault.fsync.rawValue) }
        try POSIX.fsync(descriptor)
        record(.fsyncAdmissionsParent, path)
    }

    func chmod(_ path: String, mode: UInt16) throws {
        let descriptor = try self.descriptor(path, flags: O_RDONLY)
        guard Darwin.fchmod(descriptor.fd, mode_t(mode)) == 0 else { throw POSIXFailure.system(errno) }
        record(.chmod, URL(fileURLWithPath: path))
    }

    func chflags(_ path: String) throws { let url = try confined(path); record(.chflags, url) }
    func setAndReadBackImmutable(_ path: String) throws { let url = try confined(path); record(.immutableAdmission, url) }
    func checkACL(_ path: String) throws { let url = try confined(path); record(.acl, url) }
    func fullVolumeSync() throws { record(.volume, root) }
    func securityAssessment() throws { record(.security, root) }
    func rebind(_ path: String) throws { let url = try confined(path); record(.rebind, url) }
    func unlink(_ path: String) throws {
        let (parent, name) = try parentAndName(path)
        try POSIX.unlinkAt(parent, name: name)
        record(.unlink, URL(fileURLWithPath: path))
    }

    func record(_ event: FakeFileSystemEvent, _ url: URL) {
        lock.lock(); events.append(event); eventPaths.append((event, url.path)); lock.unlock()
    }
    func appendAdmissionRecord(_ bytes: Data) { lock.lock(); admissionRecords.append(bytes); lock.unlock() }

    func registryEntryNames(_ relativePath: String) throws -> Set<String> {
        let directory = try descriptor(root.appendingPathComponent(relativePath).path, flags: O_RDONLY | O_DIRECTORY)
        return try POSIX.directoryEntryNames(directory)
    }

    private func confined(_ path: String) throws -> URL {
        if let expected = capturedIdentity {
            guard let attrs = try? FileManager.default.attributesOfItem(atPath: root.path),
                  let device = attrs[.systemNumber] as? NSNumber,
                  let inode = attrs[.systemFileNumber] as? NSNumber,
                  device == expected.0, inode == expected.1 else {
                throw FakeAdapterError.outsideFixtureRoot
            }
        }
        let url = URL(fileURLWithPath: path)
        let prefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
        guard url.path == root.path || url.path.hasPrefix(prefix) else { throw FakeAdapterError.outsideFixtureRoot }
        let resolvedRoot = root.resolvingSymlinksInPath().path
        var existing = url
        while !FileManager.default.fileExists(atPath: existing.path) { existing.deleteLastPathComponent() }
        let resolved = existing.resolvingSymlinksInPath().path
        let resolvedPrefix = resolvedRoot.hasSuffix("/") ? resolvedRoot : resolvedRoot + "/"
        guard resolved == resolvedRoot || resolved.hasPrefix(resolvedPrefix) else { throw FakeAdapterError.outsideFixtureRoot }
        return url
    }

    private func components(_ path: String) throws -> [String] {
        _ = try confined(path)
        let prefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
        guard path == root.path || path.hasPrefix(prefix) else { throw FakeAdapterError.outsideFixtureRoot }
        let relative = String(path.dropFirst(prefix.count))
        let parts = relative.split(separator: "/").map(String.init)
        guard !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else { throw FakeAdapterError.outsideFixtureRoot }
        return parts
    }

    private func parentAndName(_ path: String) throws -> (HeldDescriptor, String) {
        guard let rootHandle else { throw FakeAdapterError.outsideFixtureRoot }
        let parts = try components(path)
        guard let name = parts.last else { throw FakeAdapterError.outsideFixtureRoot }
        var parent = rootHandle
        for component in parts.dropLast() { parent = try POSIX.openAt(parent, name: component, flags: O_RDONLY | O_DIRECTORY) }
        return (parent, name)
    }

    private func descriptor(_ path: String, flags: Int32 = O_RDONLY) throws -> HeldDescriptor {
        guard let rootHandle else { throw FakeAdapterError.outsideFixtureRoot }
        let parts = try components(path)
        guard let name = parts.last else { return rootHandle }
        var parent = rootHandle
        for component in parts.dropLast() { parent = try POSIX.openAt(parent, name: component, flags: O_RDONLY | O_DIRECTORY) }
        return try POSIX.openAt(parent, name: name, flags: flags)
    }

    var hasQuarantineOrRetainedWork: Bool {
        lock.lock(); defer { lock.unlock() }; return quarantineState || retainedWorkState
    }

    var paths: [String] { eventPaths.map { $0.1 } }
}

struct FakeProcessLaunch: Equatable, Hashable {
    let executable: String
    let argv: [String]
    let cwd: String
    let environment: [String: String]
    let profile: String
    let stdin: String
    let capabilities: [String]
}

final class FakeProcessRunner {
    let fixtureRoot: URL
    private(set) var launches: [FakeProcessLaunch] = []
    private(set) var approvedExecutables: Set<String> = []
    private var approvedLaunches: Set<FakeProcessLaunch> = []
    let approvedExecutable: String
    let approvedArgv: [String]
    let approvedCWD: String
    let approvedEnvironment: [String: String]
    let approvedProfile: String
    var effectiveEUID = 0
    var inheritedDescriptor = false
    var environmentOverride: [String: String] = [:]

    init(root: URL) {
        fixtureRoot = root
        approvedExecutable = root.appendingPathComponent("runtime/closure/python311/bin/python3.11").path
        approvedArgv = [approvedExecutable, "-I", "-s", "-E"]
        approvedCWD = root.appendingPathComponent("runtime").path
        approvedEnvironment = ["PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C"].reduce(into: [:]) { $0[$1.components(separatedBy: "=")[0]] = $1.split(separator: "=", maxSplits: 1).last.map(String.init) }
        approvedProfile = "fixture-profile"
        approvedExecutables.insert(approvedExecutable)
    }

    func approve(executable: String) {
        approvedExecutables.insert(executable)
        var argv = approvedArgv; if !argv.isEmpty { argv[0] = executable }
        approvedLaunches.insert(FakeProcessLaunch(executable: executable, argv: argv, cwd: approvedCWD,
                                                   environment: approvedEnvironment, profile: approvedProfile,
                                                   stdin: "/dev/null", capabilities: []))
    }

    @discardableResult
    func launch(executable: String, argv: [String], cwd: String, environment: [String: String],
                profile: String, stdin: String, capabilities: [String]) throws -> FakeProcessLaunch {
        if effectiveEUID != 0 { throw FakeAdapterError.injectedFault(FixtureMutation.nonRootEUID.rawValue) }
        if inheritedDescriptor { throw FakeAdapterError.injectedFault(FixtureMutation.inheritedDescriptor.rawValue) }
        if !environmentOverride.isEmpty { throw FakeAdapterError.injectedFault(FixtureMutation.forbiddenEnvironment.rawValue) }
        guard approvedExecutables.contains(executable) else { throw FakeAdapterError.unapprovedExecutable }
        let launch = FakeProcessLaunch(executable: executable, argv: argv, cwd: cwd,
                                       environment: environment, profile: profile, stdin: stdin,
                                       capabilities: capabilities)
        let canonical = executable == approvedExecutable && argv == approvedArgv && cwd == approvedCWD
            && environment == approvedEnvironment && profile == approvedProfile && stdin == "/dev/null" && capabilities.isEmpty
        guard canonical || approvedLaunches.contains(launch) else { throw FakeAdapterError.invocationMismatch }
        launches.append(launch)
        return launch
    }

    @discardableResult
    func launch(_ executable: String) throws -> FakeProcessLaunch {
        var argv = approvedArgv
        if !argv.isEmpty { argv[0] = executable }
        return try launch(executable: executable, argv: argv, cwd: approvedCWD,
                   environment: approvedEnvironment, profile: approvedProfile,
                   stdin: "/dev/null", capabilities: [])
    }

    @discardableResult
    func run(executable: String, argv: [String], cwd: String, environment: [String: String],
             profile: String, stdin: String = "/dev/null", capabilities: [String] = []) throws -> FakeProcessLaunch {
        try launch(executable: executable, argv: argv, cwd: cwd, environment: environment,
                   profile: profile, stdin: stdin, capabilities: capabilities)
    }
}

enum FakeCeremonyHandoffChild: CaseIterable, Equatable {
    case reviewPackage, observationAuthorization, observationSignature, keyAPublic

    var name: String {
        switch self {
        case .reviewPackage: "review-package-v2.json"
        case .observationAuthorization: "observation-authorization-v2.json"
        case .observationSignature: "observation-authorization-v2.ed25519"
        case .keyAPublic: "key-a-public"
        }
    }
}

/// Fixture-only Key-A boundary. It holds descriptors inside the disposable fixture root and
/// never reads the production ceremony directory or exposes private key bytes.
final class FakeKeyAStore: KeyAStore, CeremonyHandoffStore {
    let root: URL
    private(set) var reviewRebinds = 0
    private(set) var authorizationRebinds = 0
    var failReviewRebind = false
    var failAuthorizationRebind = false
    var failReviewSignature = false
    var failAuthorizationSignature = false
    private var childToSwapAfterOpen: FakeCeremonyHandoffChild?
    private(set) var swappedChildren: [FakeCeremonyHandoffChild] = []

    init(root: URL) { self.root = root }

    func openAndRebindReviewPackage() throws -> HeldDescriptor {
        guard !failReviewRebind else { throw CeremonyAdapterFailure.descriptorRebind }
        reviewRebinds += 1
        return try openAndRebind(.reviewPackage)
    }

    func openAndRebindObservationAuthorization() throws -> HeldDescriptor {
        guard !failAuthorizationRebind else { throw CeremonyAdapterFailure.descriptorRebind }
        authorizationRebinds += 1
        return try openAndRebind(.observationAuthorization)
    }

    func verifyReviewPackageSignature() throws {
        guard !failReviewSignature else { throw CeremonyAdapterFailure.reviewSignature }
    }

    func verifyObservationAuthorizationSignature() throws {
        guard !failAuthorizationSignature else { throw CeremonyAdapterFailure.authorizationSignature }
    }

    func openAndRebind() throws -> HeldCeremonyHandoff {
        let review = try openAndRebindReviewPackage()
        let authorization = try openAndRebindObservationAuthorization()
        try verifyReviewPackageSignature()
        try verifyObservationAuthorizationSignature()
        let rootDescriptor = try POSIX.openDirectory(root.path)
        let signature = try openAndRebind(.observationSignature)
        let key = try openAndRebind(.keyAPublic)
        return try HeldCeremonyHandoff.make(root: rootDescriptor, reviewPackage: review,
                                            observationAuthorization: authorization,
                                            observationSignature: signature, keyAPublic: key)
    }

    func swapAfterOpening(_ child: FakeCeremonyHandoffChild) {
        childToSwapAfterOpen = child
    }

    func replaceHeldChild(_ child: FakeCeremonyHandoffChild) throws {
        do {
            let rootDescriptor = try POSIX.openDirectory(root.path)
            try replace(child, in: rootDescriptor)
            swappedChildren.append(child)
        } catch {
            throw CeremonyAdapterFailure.descriptorRebind
        }
    }

    private func openAndRebind(_ child: FakeCeremonyHandoffChild) throws -> HeldDescriptor {
        do {
            let rootDescriptor = try POSIX.openDirectory(root.path)
            let opened = try POSIX.openAt(rootDescriptor, name: child.name, flags: O_RDONLY)
            if childToSwapAfterOpen == child {
                try replace(child, in: rootDescriptor)
                childToSwapAfterOpen = nil
                swappedChildren.append(child)
            }
            _ = try opened.rebind(in: rootDescriptor, name: child.name)
            return opened
        } catch {
            throw CeremonyAdapterFailure.descriptorRebind
        }
    }

    private func replace(_ child: FakeCeremonyHandoffChild, in rootDescriptor: HeldDescriptor) throws {
        let replacement = "replacement-\(UUID().uuidString)"
        let displaced = "displaced-\(UUID().uuidString)"
        try Data("replacement".utf8).write(to: root.appendingPathComponent(replacement), options: .atomic)
        try POSIX.replaceAtomically(in: rootDescriptor, source: child.name, destination: displaced)
        try POSIX.replaceAtomically(in: rootDescriptor, source: replacement, destination: child.name)
    }
}

/// Security observation is intentionally non-synthesizing: a fixture can fault the seam, but it
/// cannot manufacture a successful Apple Security result.
private final class FixtureRootCandidateAuthority {
    struct Candidate {
        let parent: HeldDescriptor
        let name: String
        let descriptor: HeldDescriptor
    }

    private let rootPath: String
    private let rootComponents: [String]
    private let root: HeldDescriptor

    init(root: URL) throws {
        rootPath = root.path
        rootComponents = root.pathComponents
        self.root = try POSIX.openDirectory(root.path)
    }

    func candidate(at path: URL) throws -> Candidate {
        let parts = try relativeComponents(for: path)
        guard let name = parts.last else { throw POSIXFailure.unsafeObject }
        var parent = root
        for component in parts.dropLast() {
            let directory = try POSIX.openAt(parent, name: component, flags: O_RDONLY | O_DIRECTORY)
            _ = try directory.rebind(in: parent, name: component, directory: true)
            parent = directory
        }
        let opened = try POSIX.openAt(parent, name: name, flags: O_RDONLY)
        let rebound = try opened.rebind(in: parent, name: name)
        return Candidate(parent: parent, name: name, descriptor: rebound)
    }

    func makeTemporaryCandidate(named name: String) throws -> (path: URL, descriptor: HeldDescriptor) {
        let descriptor = try POSIX.createExclusive(root, name: name, mode: 0o600)
        try POSIX.writeAll(descriptor, Array("candidate".utf8))
        return (URL(fileURLWithPath: rootPath).appendingPathComponent(name), descriptor)
    }

    func removeDirectChild(named name: String) throws {
        try POSIX.unlinkAt(root, name: name)
    }

    private func relativeComponents(for path: URL) throws -> [String] {
        let pathComponents = path.pathComponents
        guard pathComponents.count > rootComponents.count,
              Array(pathComponents.prefix(rootComponents.count)) == rootComponents else {
            throw POSIXFailure.unsafeObject
        }
        let parts = Array(pathComponents.dropFirst(rootComponents.count))
        guard !parts.isEmpty, !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else {
            throw POSIXFailure.unsafeObject
        }
        return parts
    }
}

final class FakeObservationSecurity: ObservationSecurity {
    private var candidateAuthority: FixtureRootCandidateAuthority?
    private(set) var descriptorRebinds = 0
    private(set) var abaSwaps = 0
    private var swapsAfterDescriptorRebind = false

    init() {}
    func bind(root: URL) {
        guard let authority = try? FixtureRootCandidateAuthority(root: root) else {
            preconditionFailure("fixture root descriptor open failed")
        }
        candidateAuthority = authority
    }

    func observe(_ descriptor: HeldDescriptor, at path: URL) throws -> LauncherSecurityObservation {
        let candidate = try requireSameDescriptor(descriptor, at: path)
        descriptorRebinds += 1
        if swapsAfterDescriptorRebind { try performABASwap(candidate) }
        throw CeremonyAdapterFailure.observationUnavailable
    }

    func failAfterDescriptorRebind() throws {
        guard let candidateAuthority else { throw CeremonyAdapterFailure.descriptorRebind }
        let name = "observation-candidate-\(UUID().uuidString)"
        let candidate = try candidateAuthority.makeTemporaryCandidate(named: name)
        defer { try? candidateAuthority.removeDirectChild(named: name) }
        _ = try observe(candidate.descriptor, at: candidate.path)
    }

    func performABASwapAfterDescriptorRebind() { swapsAfterDescriptorRebind = true }

    private func requireSameDescriptor(_ descriptor: HeldDescriptor, at path: URL) throws -> FixtureRootCandidateAuthority.Candidate {
        guard let candidateAuthority else { throw CeremonyAdapterFailure.descriptorRebind }
        do {
            let candidate = try candidateAuthority.candidate(at: path)
            guard candidate.descriptor.device == descriptor.device, candidate.descriptor.inode == descriptor.inode else {
                throw CeremonyAdapterFailure.descriptorRebind
            }
            return candidate
        } catch {
            throw CeremonyAdapterFailure.descriptorRebind
        }
    }

    private func performABASwap(_ candidate: FixtureRootCandidateAuthority.Candidate) throws {
        let replacement = "observation-replacement-\(UUID().uuidString)"
        let displaced = "observation-displaced-\(UUID().uuidString)"
        let replacementDescriptor = try POSIX.createExclusive(candidate.parent, name: replacement, mode: 0o600)
        try POSIX.writeAll(replacementDescriptor, Array("replacement".utf8))
        try POSIX.replaceAtomically(in: candidate.parent, source: candidate.name, destination: displaced)
        try POSIX.replaceAtomically(in: candidate.parent, source: replacement, destination: candidate.name)
        try POSIX.unlinkAt(candidate.parent, name: candidate.name)
        try POSIX.replaceAtomically(in: candidate.parent, source: displaced, destination: candidate.name)
        abaSwaps += 1
    }
}

/// Candidate observation never launches a process. The launcher protocol is observation-only at
/// this migration stage; child execution remains owned by the later materialization adapter.
final class FakePinnedLauncher: PinnedLauncher {
    private var candidateAuthority: FixtureRootCandidateAuthority?
    private(set) var observations = 0
    private(set) var descriptorRebinds = 0
    private(set) var abaSwaps = 0
    private var swapsAfterDescriptorRebind = false

    func bind(root: URL) {
        guard let authority = try? FixtureRootCandidateAuthority(root: root) else {
            preconditionFailure("fixture root descriptor open failed")
        }
        candidateAuthority = authority
    }

    func observeSecurity(_ descriptor: HeldDescriptor, at path: URL) throws -> LauncherSecurityObservation {
        let candidate = try requireSameDescriptor(descriptor, at: path)
        descriptorRebinds += 1
        observations += 1
        if swapsAfterDescriptorRebind { try performABASwap(candidate) }
        throw CeremonyAdapterFailure.launcherObservation
    }

    func performABASwapAfterDescriptorRebind() { swapsAfterDescriptorRebind = true }

    private func requireSameDescriptor(_ descriptor: HeldDescriptor, at path: URL) throws -> FixtureRootCandidateAuthority.Candidate {
        guard let candidateAuthority else { throw CeremonyAdapterFailure.descriptorRebind }
        do {
            let candidate = try candidateAuthority.candidate(at: path)
            guard candidate.descriptor.device == descriptor.device, candidate.descriptor.inode == descriptor.inode else {
                throw CeremonyAdapterFailure.descriptorRebind
            }
            return candidate
        } catch {
            throw CeremonyAdapterFailure.descriptorRebind
        }
    }

    private func performABASwap(_ candidate: FixtureRootCandidateAuthority.Candidate) throws {
        let replacement = "launcher-replacement-\(UUID().uuidString)"
        let displaced = "launcher-displaced-\(UUID().uuidString)"
        let replacementDescriptor = try POSIX.createExclusive(candidate.parent, name: replacement, mode: 0o600)
        try POSIX.writeAll(replacementDescriptor, Array("replacement".utf8))
        try POSIX.replaceAtomically(in: candidate.parent, source: candidate.name, destination: displaced)
        try POSIX.replaceAtomically(in: candidate.parent, source: replacement, destination: candidate.name)
        try POSIX.unlinkAt(candidate.parent, name: candidate.name)
        try POSIX.replaceAtomically(in: candidate.parent, source: displaced, destination: candidate.name)
        abaSwaps += 1
    }
}

enum FakeArchiveFault: String, CaseIterable {
    case copy, extract, rename
}

final class FakeArchiveObservationAdapter: InProcessArchiveExtractor {
    private(set) var finalRenameObserved = false
    private(set) var copyObserved = false
    private(set) var extractionObserved = false
    var fault: FakeArchiveFault?
    private var bundle: ValidatedBundle?
    private var stage: HeldMaterializationStage?
    private(set) var usedSharedExtractor = false

    func bind(bundle: ValidatedBundle, stage: HeldMaterializationStage) {
        self.bundle = bundle
        self.stage = stage
    }

    func copyAndExtract() throws -> HeldInterpreterCapability {
        guard let bundle, let stage else { throw ArchiveFailure.invalidStage }
        if fault == nil {
            usedSharedExtractor = true
            return try SharedInProcessArchiveExtractor().copyAndExtract(bundle, into: stage)
        }
        var copyOperations = CopyOperations.production
        if fault == .copy {
            copyOperations = CopyOperations(configureDestination: { [weak self] _, _, _ in
                self?.copyObserved = true
                throw ArchiveFailure.sourceChanged
            })
        }
        let copied = try BundleCopier.copy(bundle, into: stage, operations: copyOperations)
        var archiveOperations = ArchiveOperations.production
        if fault == .extract {
            archiveOperations = ArchiveOperations(renameExclusive: ArchiveOperations.production.renameExclusive,
                                                   durableDirectory: { [weak self] _ in
                                                       self?.extractionObserved = true
                                                       throw ArchiveFailure.unsafeArchive
                                                   },
                                                   rebindDirectory: ArchiveOperations.production.rebindDirectory)
        } else if fault == .rename {
            archiveOperations = ArchiveOperations(renameExclusive: { [weak self] _, _, _, _ in
                self?.finalRenameObserved = true
                throw ArchiveFailure.sourceChanged
            }, durableDirectory: ArchiveOperations.production.durableDirectory,
            rebindDirectory: ArchiveOperations.production.rebindDirectory)
        }
        return try ArchiveInstaller.extractAndPlace(stage: stage, copied: copied, operations: archiveOperations)
    }

    func copyAndExtract(_ bundle: ValidatedBundle, into stage: HeldMaterializationStage) throws -> HeldInterpreterCapability {
        bind(bundle: bundle, stage: stage)
        return try copyAndExtract()
    }

    func failAfterCopy() throws {
        fault = .copy
        _ = try copyAndExtract()
    }
    func failAfterExtract() throws {
        fault = .extract
        _ = try copyAndExtract()
    }
    func failAfterFinalRename() throws {
        fault = .rename
        _ = try copyAndExtract()
    }
}

struct ValidArchive: Equatable { let path: URL; let bytes: Data }
struct RecordedGit: Equatable { let executable: String; let argv: [String]; let cwd: URL; let environment: [String: String] }

/// A deliberately small stored-deflate USTAR archive.  It is generated in the test target so
/// the production parser is exercised against bytes rather than a Foundation archive shortcut.
enum FixtureTarGzip {
    static func python() -> Data {
        let members: [(String, UInt8, [UInt8], UInt16)] = [
            ("python/", 53, [], 0o700), ("python/bin/", 53, [], 0o700),
            ("python/bin/python3.11", 48, Array("#!/bin/sh\nexit 0\n".utf8), 0o700)
        ]
        return gzip(tar(members))
    }
    static func member(name: String, type: UInt8, bytes: [UInt8]) -> Data { gzip(tar([(name, type, bytes, 0o700)])) }
    static func tar(_ members: [(String, UInt8, [UInt8], UInt16)]) -> [UInt8] {
        var output: [UInt8] = []
        for (name, type, bytes, mode) in members {
            var header = [UInt8](repeating: 0, count: 512)
            put(Array(name.utf8), in: &header, at: 0, width: 100)
            putOctal(UInt64(mode), in: &header, at: 100, width: 8)
            putOctal(UInt64(bytes.count), in: &header, at: 124, width: 12)
            for index in 148..<156 { header[index] = 32 }
            header[156] = type
            put(Array("ustar".utf8), in: &header, at: 257, width: 6); header[262] = 0
            put(Array("00".utf8), in: &header, at: 263, width: 2)
            putOctal(UInt64(header.reduce(0) { $0 + UInt64($1) }), in: &header, at: 148, width: 8)
            output += header; output += bytes; output += [UInt8](repeating: 0, count: (512 - bytes.count % 512) % 512)
        }
        output += [UInt8](repeating: 0, count: 1024); return output
    }
    private static func put(_ value: [UInt8], in header: inout [UInt8], at offset: Int, width: Int) { for (index, byte) in value.prefix(width).enumerated() { header[offset + index] = byte } }
    private static func putOctal(_ value: UInt64, in header: inout [UInt8], at offset: Int, width: Int) {
        let text = String(value, radix: 8); let bytes = Array(text.utf8)
        for index in 0..<(width - 1) { header[offset + index] = 48 }
        for (index, byte) in bytes.enumerated() { header[offset + width - 1 - bytes.count + index] = byte }
        header[offset + width - 1] = 0
    }
    static func gzip(_ data: [UInt8]) -> Data {
        var output: [UInt8] = [31, 139, 8, 0, 0, 0, 0, 0, 0, 255]
        var offset = 0
        while offset < data.count {
            let count = min(65_535, data.count - offset); let final = offset + count == data.count
            output.append(final ? 1 : 0); output.append(UInt8(count & 0xff)); output.append(UInt8(count >> 8))
            let inverse = 0xffff - count; output.append(UInt8(inverse & 0xff)); output.append(UInt8(inverse >> 8))
            output += data[offset..<(offset + count)]; offset += count
        }
        let checksum = crc32(data); output += [UInt8(checksum & 0xff), UInt8((checksum >> 8) & 0xff), UInt8((checksum >> 16) & 0xff), UInt8(checksum >> 24)]
        let size = UInt32(truncatingIfNeeded: data.count); output += [UInt8(size & 0xff), UInt8((size >> 8) & 0xff), UInt8((size >> 16) & 0xff), UInt8(size >> 24)]
        return Data(output)
    }
    private static func crc32(_ bytes: [UInt8]) -> UInt32 {
        var value = UInt32.max
        for byte in bytes {
            value ^= UInt32(byte)
            for _ in 0..<8 { value = (value & 1) == 0 ? value >> 1 : (value >> 1) ^ 0xedb88320 }
        }
        return value ^ UInt32.max
    }
}

private enum FixtureJSON {
    static func reseal(_ source: String, replacing values: [String: JSONValue]) throws -> String {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: Array(source.utf8)) else { throw CanonicalJSONError.invalidJSON }
        let bare = pairs.filter { $0.0 != "sha256" }.map { pair in
            values[pair.0].map { (pair.0, $0) } ?? pair
        }
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(bare)))
        return String(bytes: try CanonicalJSON.encode(.object(bare + [("sha256", .string(digest))])), encoding: .utf8)!
    }
    static func replacingPythonDigest(_ source: String, _ digest: String) throws -> String {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: Array(source.utf8)),
              case .object(let python)? = pairs.first(where: { $0.0 == "python" })?.1 else { throw CanonicalJSONError.invalidJSON }
        let updated = JSONValue.object(python.map { $0.0 == "sha256" ? ($0.0, .string(digest)) : $0 })
        let replacement = pairs.map { $0.0 == "python" ? ($0.0, updated) : $0 }
        // The protected-Python lock is V1 and deliberately has no V2 self-digest field.
        return String(bytes: try CanonicalJSON.encode(.object(replacement)), encoding: .utf8)!
    }
    static func replacingArchive(_ source: String, digest: String, size: Int64) throws -> String {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: Array(source.utf8)),
              case .array(let rows)? = pairs.first(where: { $0.0 == "artifacts" })?.1 else { throw CanonicalJSONError.invalidJSON }
        let updated = rows.enumerated().map { index, row -> JSONValue in
            guard index == 0, case .object(let fields) = row else { return row }
            return .object(fields.map { field in field.0 == "sha256" ? (field.0, .string(digest)) : (field.0 == "size" ? (field.0, .integer(size)) : field) })
        }
        return try reseal(source, replacing: ["artifacts": .array(updated)])
    }
}

extension MaterializationAdapters {
    static func fixture(fileSystem: FakeFileSystem, process: FakeProcessRunner, approvalURL: URL, installedHelperURL: URL,
                        approvalSwapAfterRead: (() throws -> Void)? = nil) -> MaterializationAdapters {
        MaterializationAdapters(
            identityCheck: {
                guard process.effectiveEUID == 0, !process.inheritedDescriptor else { throw FakeAdapterError.injectedFault("identity") }
                guard process.environmentOverride.isEmpty else { throw FakeAdapterError.injectedFault("environment") }
                let parent = try POSIX.openDirectory(approvalURL.deletingLastPathComponent().path)
                let name = approvalURL.lastPathComponent
                let held = try POSIX.openAt(parent, name: name, flags: O_RDONLY)
                let metadata = try POSIX.metadata(held)
                guard metadata.type == S_IFREG, metadata.nlink == 1,
                      metadata.size > 0, metadata.size <= Int64(CanonicalJSON.maximumInputBytes) else {
                    throw POSIXFailure.unsafeObject
                }
                let bytes = try POSIX.readAll(held, maximum: CanonicalJSON.maximumInputBytes)
                guard metadata.size == Int64(bytes.count) else { throw POSIXFailure.unsafeObject }
                try approvalSwapAfterRead?()
                let rebound = try held.rebind(in: parent, name: name)
                guard try POSIX.metadata(held) == metadata,
                      try POSIX.metadata(rebound) == metadata,
                      try POSIX.readAll(rebound, maximum: CanonicalJSON.maximumInputBytes) == bytes else {
                    throw POSIXFailure.unsafeObject
                }
                let approval = try Task14ApprovalV2.decodeDocument(bytes)
                // The test seam has no ambient helper argument: it verifies the same fixed,
                // fixture-installed helper bytes that its approval pins.
                guard CanonicalJSON.sha256(Array(try Data(contentsOf: installedHelperURL))) == approval.provisionerBinarySHA256.value else {
                    throw FakeAdapterError.injectedFault(FixtureMutation.copiedHelper.rawValue)
                }
                return HeldBootstrapApproval(descriptor: held, bytes: bytes, approval: approval)
            },
            storageCheck: {
                guard !fileSystem.storageRejected else { throw FakeAdapterError.injectedFault(FixtureMutation.badStorage.rawValue) }
            },
            bundleCheck: { request, approval in
                try HeldBundle.openAndValidate(request, approval: approval)
            })
    }
}

final class AdmissionBarrier {
    private let condition = NSCondition()
    private var arrivals = 0
    private var released = false
    private var armed = false
    func arm() { condition.lock(); armed = true; arrivals = 0; released = false; cancelled = false; condition.unlock() }
    func disarm() { condition.lock(); armed = false; released = true; condition.broadcast(); condition.unlock() }
    var isArmed: Bool { condition.lock(); defer { condition.unlock() }; return armed }
    var isBlocking: Bool { condition.lock(); defer { condition.unlock() }; return armed && !released }
    var isReleased: Bool { condition.lock(); defer { condition.unlock() }; return released }
    private(set) var cancelled = false
    var isCancelled: Bool { condition.lock(); defer { condition.unlock() }; return cancelled }
    func arriveAndWait(timeout: TimeInterval = 2) throws {
        condition.lock(); arrivals += 1; condition.broadcast()
        let deadline = Date().addingTimeInterval(timeout)
        while !released && Date() < deadline { condition.wait(until: deadline) }
        guard released else { condition.unlock(); throw FakeAdapterError.barrierTimeout }
        condition.unlock()
    }
    func waitForBoth(timeout: TimeInterval = 5) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        condition.lock(); defer { condition.unlock() }
        while arrivals < 2 && Date() < deadline { condition.wait(until: deadline) }
        return arrivals >= 2
    }
    func release() { condition.lock(); released = true; condition.broadcast(); condition.unlock() }
    func cancelAndRelease() { condition.lock(); cancelled = true; released = true; condition.broadcast(); condition.unlock() }
}

final class RaceCancellation {
    private let condition = NSCondition()
    private var cancelled = false
    func cancel() { condition.lock(); cancelled = true; condition.broadcast(); condition.unlock() }
    var isCancelled: Bool { condition.lock(); defer { condition.unlock() }; return cancelled }
    func waitUntilCancelled(timeout: TimeInterval = 5) -> Bool {
        condition.lock(); defer { condition.unlock() }
        let deadline = Date().addingTimeInterval(timeout)
        while !cancelled && Date() < deadline { condition.wait(until: deadline) }
        return cancelled
    }
}

final class BarrierBackedAdmissionStorage: Storage {
    let fs: FakeFileSystem
    let barrier: AdmissionBarrier
    private let transactionLock = NSLock()
    private let attemptLock = NSLock()
    var admissionFault: AdmissionFault?
    var terminalizationFails = false
    weak var durableStore: FakeDurableStore?
    private(set) var issuedAttempts: [String] = []
    private var postSnapshotResidueRegistry: String?
    init(fs: FakeFileSystem, barrier: AdmissionBarrier) { self.fs = fs; self.barrier = barrier }
    var storageVolumeUUID: String { "123e4567-e89b-12d3-a456-426614174000" }
    private func admissionPath(_ name: String) -> URL {
        fs.root.appendingPathComponent("control/admissions/\(name)")
    }

    func stageName(_ admission: Admission) throws -> String {
        try AdmissionController.stageName(admission.attempt)
    }

    /// Kept for Task-3 fixture assertions; Task-6 routes through the protocol methods below.
    func admit() throws -> Admission {
        let attempt = nextAttemptUUID()
        var record: HeldAdmissionRecord?
        do {
            let opened = try preflightAndCreateExclusiveAdmission(attempt: attempt)
            record = opened
            try writeAdmission(Array(attempt.utf8), to: opened)
            try fsyncAdmission(opened)
            try immutableReadbackAdmission(opened)
            try rebindAdmission(opened)
            try fsyncAdmissionsParent(opened)
            return Admission(attempt: attempt, record: opened, recordSHA256: try SHA256Digest(CanonicalJSON.sha256(Array(attempt.utf8))))
        } catch {
            if let record { try? quarantineAfterAdmissionFailure(record) }
            throw error
        }
    }

    func injectPostSnapshotResidue(in registry: String) {
        precondition(["control/admissions", "control/journals", "materialization-work", "quarantine", "authorities"].contains(registry))
        transactionLock.lock(); postSnapshotResidueRegistry = registry; transactionLock.unlock()
    }

    func preflightAndCreateExclusiveAdmission(attempt: String) throws -> HeldAdmissionRecord {
        let name = try AdmissionController.admissionName(attempt)
        let path = admissionPath(name)
        if barrier.isCancelled { throw FakeAdapterError.injectedFault("admission cancelled") }
        if barrier.isBlocking {
            fs.armExclusiveAttemptGate(using: barrier)
            fs.record(.admissionBarrierArrival, path)
            try barrier.arriveAndWait()
            if barrier.isCancelled { throw FakeAdapterError.injectedFault("admission cancelled") }
        }
        transactionLock.lock(); defer { transactionLock.unlock() }
        if barrier.isCancelled { throw FakeAdapterError.injectedFault("admission cancelled") }
        if fs.storageRejected { throw FakeAdapterError.injectedFault(FixtureMutation.badStorage.rawValue) }
        let first = try registrySnapshots()
        if let registry = postSnapshotResidueRegistry {
            postSnapshotResidueRegistry = nil
            try fs.createFile(at: fs.root.appendingPathComponent(registry).appendingPathComponent("post-snapshot-sentinel").path, bytes: Array("foreign".utf8))
        }
        let second = try registrySnapshots()
        guard first == second, second.allSatisfy(\.isEmpty) else { throw AdmissionFailure.residue }
        do {
            if admissionFault == .beforeOpen { throw FakeAdapterError.injectedFault(AdmissionFault.beforeOpen.rawValue) }
            _ = try fs.createExclusive(path.path)
            if admissionFault == .afterOpen || admissionFault == .ownerDeath {
                if admissionFault == .ownerDeath { fs.record(.admissionOwnerDeath, path) }
                fs.markRetainedWork(); throw FakeAdapterError.injectedFault(admissionFault!.rawValue)
            }
            let parent = try POSIX.openDirectory(path.deletingLastPathComponent().path)
            let file = try POSIX.openAt(parent, name: path.lastPathComponent, flags: O_RDWR)
            return HeldAdmissionRecord(parent: parent, name: name, file: file)
        } catch FakeAdapterError.alreadyExists { throw AdmissionFailure.alreadyAdmitted }
    }

    func nextAttemptUUID() -> String {
        attemptLock.lock(); defer { attemptLock.unlock() }
        let attempt = issuedAttempts.isEmpty ? "0123456789abcdef0123456789abcdef" : String(format: "%032x", issuedAttempts.count)
        issuedAttempts.append(attempt)
        return attempt
    }

    func writeAdmission(_ bytes: [UInt8], to record: HeldAdmissionRecord) throws { try POSIX.writeAll(record.file, bytes); fs.record(.writeAdmission, admissionPath(record.name)); fs.appendAdmissionRecord(Data(bytes)) }
    func fsyncAdmission(_ record: HeldAdmissionRecord) throws {
        try POSIX.fsync(record.file)
        fs.record(.fsyncAdmission, admissionPath(record.name))
        if admissionFault == .admissionFsync { fs.record(.admissionFsyncFailure, admissionPath(record.name)); throw FakeAdapterError.injectedFault(AdmissionFault.admissionFsync.rawValue) }
    }
    func chmodAdmissionReadOnly(_ record: HeldAdmissionRecord) throws { guard Darwin.fchmod(record.file.fd, 0o444) == 0 else { throw POSIXFailure.system(errno) }; fs.record(.chmod, admissionPath(record.name)) }
    func immutableReadbackAdmission(_ record: HeldAdmissionRecord) throws { fs.record(.immutableAdmission, admissionPath(record.name)) }
    func fsyncAdmissionsParent(_ record: HeldAdmissionRecord) throws { try POSIX.fsync(record.parent); fs.record(.fsyncAdmissionsParent, admissionPath(record.name)) }
    func rebindAdmission(_ record: HeldAdmissionRecord) throws {
        if admissionFault == .admissionRebind { fs.record(.admissionRebindFailure, admissionPath(record.name)); throw FakeAdapterError.injectedFault(AdmissionFault.admissionRebind.rawValue) }
        _ = try record.file.rebind(in: record.parent, name: record.name)
        fs.record(.rebind, admissionPath(record.name))
    }
    func markAdmitted() throws { durableStore?.startAdmitted() }
    func quarantineAfterAdmissionFailure(_ record: HeldAdmissionRecord) throws {
        if terminalizationFails { fs.markRetainedWork(); throw FakeAdapterError.injectedFault("terminalization") }
        fs.markRetainedWork(); fs.markQuarantine()
    }
    func reboundAdmissionInode() throws -> ino_t {
        guard let name = issuedAttempts.first.map({ $0 + ".json" }) else { throw POSIXFailure.unsafeObject }
        let path = admissionPath(name)
        return try POSIX.openAt(try POSIX.openDirectory(path.deletingLastPathComponent().path), name: path.lastPathComponent, flags: O_RDONLY).inode
    }
    func placeholderAdmission(_ attempt: String) throws -> Admission {
        let parent = try POSIX.openDirectory(fs.root.path)
        let file = try POSIX.openAt(parent, name: "control", flags: O_RDONLY | O_DIRECTORY)
        return Admission(attempt: attempt, record: HeldAdmissionRecord(parent: parent, name: "control", file: file), recordSHA256: try SHA256Digest(CanonicalJSON.sha256([])))
    }

    private func registrySnapshots() throws -> [Set<String>] {
        try ["control/admissions", "control/journals", "materialization-work", "quarantine", "authorities"].map { try fs.registryEntryNames($0) }
    }
}

final class FakeDurableStore: DurableStateStore {
    private weak var fileSystem: FakeFileSystem?
    private(set) var currentState: MaterializationState = .absent
    var faultAt: StateMachine.Seam?
    /// DEBUG-only fault point for the terminal PUBLISHED append; indeterminate terminalization
    /// deliberately bypasses this injected failure so tests can distinguish the two records.
    var failAppendNumber: Int?
    var failAppendAfterRecordNumber: Int?
    var failPublishIndeterminateTerminalization = false
    init(fileSystem: FakeFileSystem? = nil) { self.fileSystem = fileSystem }
    private(set) var journalRecords: [[UInt8]] = []
    private var publishedJournalDirectory: HeldDescriptor?

    /// Test-only durability mirror for a real fixture registry.  It is configured before the
    /// publication transaction; records are emitted as each state transition occurs, never
    /// reconstructed after publication for verifier convenience.
    func attachJournalDirectory(_ directory: HeldDescriptor) throws {
        guard try POSIX.directoryEntryNames(directory).isEmpty else { throw POSIXFailure.unsafeObject }
        publishedJournalDirectory = directory
        for bytes in journalRecords { try materializeJournalRecord(bytes, into: directory) }
    }
    func throwingAt(_ seam: StateMachine.Seam) -> FakeDurableStore { faultAt = seam; return self }
    func startAdmitted() { currentState = .admitted }
    private let transitionLock = NSLock()
    func append(_ record: JournalRecordV2, admission: Admission) throws {
        transitionLock.lock(); defer { transitionLock.unlock() }
        let next = state(for: record.phase)
        guard record.attemptUUID == admission.attempt,
              record.admissionRecordSHA256 == admission.recordSHA256,
              StateMachine.successors[currentState] == next else { throw StateFailure.illegalTransition }
        if failAppendNumber == journalRecords.count + 1 { throw FakeAdapterError.injectedFault("append-number") }
        try fail(.journalWrite)
        let bytes = try record.canonicalRecordBytes()
        try fail(.journalFsync); try fail(.journalImmutable); try fail(.journalRebind)
        if faultAt == .sigint || faultAt == .sigterm { throw FakeAdapterError.injectedFault(faultAt!.rawValue) }
        journalRecords.append(bytes)
        if let publishedJournalDirectory { try materializeJournalRecord(bytes, into: publishedJournalDirectory) }
        if failAppendAfterRecordNumber == journalRecords.count { throw FakeAdapterError.injectedFault("append-after-record") }
        currentState = next
    }
    func moveToQuarantine(admission: Admission, failure: JournalFailureClass) throws {
        transitionLock.lock(); defer { transitionLock.unlock() }
        guard failure.rawValue.hasPrefix("PRE_"),
              [.stageCreated, .presealed, .renameIntent].contains(currentState) else { throw StateFailure.illegalTransition }
        let record = try JournalRecordV2.make(sequence: 4, phase: .quarantined, admission: admission, failure: failure)
        journalRecords.append(try record.canonicalRecordBytes())
        fileSystem?.markRetainedWork()
        fileSystem?.markQuarantine()
        currentState = .quarantined
    }
    func markPublishIndeterminate(admission: Admission, failure: JournalFailureClass) throws {
        transitionLock.lock(); defer { transitionLock.unlock() }
        if failPublishIndeterminateTerminalization { throw FakeAdapterError.injectedFault("indeterminate-terminalization") }
        if journalRecords.contains(where: { (try? JournalRecordV2.decode($0))?.sequence == 4 }) {
            throw StateFailure.terminalizationFailed
        }
        guard failure.rawValue.hasPrefix("POST_"), currentState == .renameIntent else { throw StateFailure.illegalTransition }
        let digest = try SHA256Digest(String(repeating: "a", count: 64))
        let record = try JournalRecordV2.make(sequence: 4, phase: .publishIndeterminate, admission: admission,
                                               failure: failure, publishedClosureSHA256: digest,
                                               receiptFileSHA256: digest)
        journalRecords.append(try record.canonicalRecordBytes())
        currentState = .publishIndeterminate
    }
    // Legacy fixture-only probe: it exercises fixture cleanup markers before an admission has
    // produced a journal attempt. Production callers never use this path.
    func quarantineAfterFailure(_ admission: Admission) throws {
        transitionLock.lock(); defer { transitionLock.unlock() }
        if currentState == .absent {
            fileSystem?.markRetainedWork()
            fileSystem?.markQuarantine()
            currentState = .quarantined
            return
        }
        guard [.stageCreated, .presealed, .renameIntent].contains(currentState) else { throw StateFailure.illegalTransition }
        let record = try JournalRecordV2.make(sequence: 4, phase: .quarantined, admission: admission, failure: .preRenameIO)
        journalRecords.append(try record.canonicalRecordBytes())
        fileSystem?.markRetainedWork()
        fileSystem?.markQuarantine()
        currentState = .quarantined
    }
    private func state(for phase: JournalPhase) -> MaterializationState {
        switch phase {
        case .stageCreated: return .stageCreated
        case .presealed: return .presealed
        case .renameIntent: return .renameIntent
        case .published: return .published
        case .quarantined: return .quarantined
        case .publishIndeterminate: return .publishIndeterminate
        }
    }
    private func materializeJournalRecord(_ bytes: [UInt8], into directory: HeldDescriptor) throws {
        let record = try JournalRecordV2.decode(bytes)
        let file = try POSIX.createExclusive(directory, name: record.filename, mode: 0o444)
        try POSIX.writeAll(file, bytes)
        guard fchmod(file.fd, 0o444) == 0 else { throw POSIXFailure.system(errno) }
        try POSIX.durableRegular(file)
        _ = try file.rebindSameIdentity(in: directory, name: record.filename)
        try POSIX.durableDirectory(directory)
    }
    private func fail(_ seam: StateMachine.Seam) throws { if faultAt == seam { throw FakeAdapterError.injectedFault(seam.rawValue) } }
}

final class MaterializationFixture {
    let root: URL
    private let capturedIdentity: (NSNumber, NSNumber)?
    let request: MaterializationRequest
    let approvalURL: URL
    let helperURL: URL
    let adapters: MaterializationAdapters
    let storage: BarrierBackedAdmissionStorage
    let durableStore: FakeDurableStore
    let stage: HeldMaterializationStage
    let stageRoot: URL
    let stageInputURL: URL
    let stageRuntimeURL: URL
    private(set) var validatedBundle: ValidatedBundle!
    let validArchive: ValidArchive
    let fakeFS: FakeFileSystem
    let fakeProcess: FakeProcessRunner
    let keyAStore: FakeKeyAStore
    var handoff: FakeKeyAStore { keyAStore }
    var operationLog: FixtureOperationLog {
        FixtureOperationLog(launchPrograms: fakeProcess.launches.map(\.executable),
                            accessedPaths: fakeFS.paths)
    }
    let observation: FakeObservationSecurity
    let archive: FakeArchiveObservationAdapter
    let launcher: FakePinnedLauncher
    let recordedGit: RecordedGit
    let admissionBarrier: AdmissionBarrier
    private(set) var lastCopySwapPath: String?
    private var mutationCases: Set<FixtureMutation> = []

    private init(root: URL, capturedIdentity: (NSNumber, NSNumber), injectedFault: FixtureInitFault?, mutations: Set<FixtureMutation> = []) throws {
        self.root = root
        self.capturedIdentity = capturedIdentity
        self.mutationCases = mutations
        let stageRoot = root.appendingPathComponent("fixture-stage", isDirectory: true)
        self.stageRoot = stageRoot
        self.stageInputURL = stageRoot.appendingPathComponent("input", isDirectory: true)
        self.stageRuntimeURL = stageRoot.appendingPathComponent("runtime", isDirectory: true)
        self.fakeFS = FakeFileSystem(root: root)
        self.fakeProcess = FakeProcessRunner(root: root)
        self.keyAStore = FakeKeyAStore(root: root.appendingPathComponent("ceremony-handoff", isDirectory: true))
        self.observation = FakeObservationSecurity()
        self.observation.bind(root: root)
        self.archive = FakeArchiveObservationAdapter()
        self.launcher = FakePinnedLauncher()
        self.launcher.bind(root: root)
        self.admissionBarrier = AdmissionBarrier()
        self.storage = BarrierBackedAdmissionStorage(fs: fakeFS, barrier: admissionBarrier)
        self.durableStore = FakeDurableStore(fileSystem: fakeFS)
        self.storage.durableStore = durableStore
        let bundle = root.appendingPathComponent("bundle", isDirectory: true)
        let approval = root.appendingPathComponent("approval.json")
        let helper = root.appendingPathComponent("runtime/closure/python311/bin/python3.11")
        self.request = MaterializationRequest(inputBundle: bundle)
        self.approvalURL = approval
        self.helperURL = helper
        self.validArchive = ValidArchive(path: root.appendingPathComponent("python.tar.gz"), bytes: FixtureTarGzip.python())
        self.recordedGit = RecordedGit(executable: "/usr/bin/git", argv: ["rev-parse", "--verify", "HEAD"], cwd: root, environment: ["PATH": "/usr/bin:/bin"])
        if injectedFault == .afterRoot { throw FakeAdapterError.injectedFault(FixtureInitFault.afterRoot.rawValue) }
        try FileManager.default.createDirectory(at: root.appendingPathComponent("control/admissions"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("control/journals"), withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("materialization-work"), withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("quarantine"), withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("authorities"), withIntermediateDirectories: false)
        if injectedFault == .afterControl { throw FakeAdapterError.injectedFault(FixtureInitFault.afterControl.rawValue) }
        let ceremonyRoot = root.appendingPathComponent("ceremony-handoff", isDirectory: true)
        try FileManager.default.createDirectory(at: ceremonyRoot, withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: bundle, withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: bundle.appendingPathComponent("checkout"), withIntermediateDirectories: false)
        try FileManager.default.createDirectory(at: bundle.appendingPathComponent("artifacts"), withIntermediateDirectories: false)
        if injectedFault == .afterBundle { throw FakeAdapterError.injectedFault(FixtureInitFault.afterBundle.rawValue) }
        let v2 = try V2Fixture.complete()
        let archiveSHA = CanonicalJSON.sha256(Array(validArchive.bytes))
        let lockJSON = try FixtureJSON.replacingPythonDigest(v2.lock, archiveSHA)
        let lockSHA = CanonicalJSON.sha256(Array(lockJSON.utf8))
        let archiveBoundBundle = try FixtureJSON.replacingArchive(v2.bundle, digest: archiveSHA, size: Int64(validArchive.bytes.count))
        let bundleJSON = try FixtureJSON.reseal(archiveBoundBundle, replacing: ["lock_sha256": .string(lockSHA)])
        let bundleSHA = try InputBundleManifest.decode(bundleJSON).sha256.value
        let reviewJSON = try FixtureJSON.reseal(v2.reviewPackageJSON, replacing: ["lock_sha256": .string(lockSHA), "python_archive_sha256": .string(archiveSHA), "input_bundle_manifest_sha256": .string(bundleSHA)])
        let reviewSHA = try Task14ReviewPackageV2.decode(reviewJSON).sha256.value
        let authorizationJSON = try FixtureJSON.reseal(v2.observationAuthorizationJSON, replacing: ["review_package_sha256": .string(reviewSHA), "input_bundle_manifest_sha256": .string(bundleSHA)])
        let authorizationSHA = try Task14ObservationAuthorizationV2.decode(authorizationJSON).sha256.value
        let approvalJSON = try FixtureJSON.reseal(v2.approval, replacing: ["lock_sha256": .string(lockSHA), "python_archive_sha256": .string(archiveSHA), "input_bundle_manifest_sha256": .string(bundleSHA), "review_package_sha256": .string(reviewSHA), "observation_authorization_sha256": .string(authorizationSHA)])
        try Data(approvalJSON.utf8).write(to: approval, options: .atomic)
        try Data(reviewJSON.utf8).write(to: ceremonyRoot.appendingPathComponent("review-package-v2.json"), options: .atomic)
        try Data(authorizationJSON.utf8).write(to: ceremonyRoot.appendingPathComponent("observation-authorization-v2.json"), options: .atomic)
        try Data("fixture-observation-authorization-signature".utf8).write(to: ceremonyRoot.appendingPathComponent("observation-authorization-v2.ed25519"), options: .atomic)
        try Data(v2.keyAPublicBytes).write(to: ceremonyRoot.appendingPathComponent("key-a-public"), options: .atomic)
        if injectedFault == .afterApproval { throw FakeAdapterError.injectedFault(FixtureInitFault.afterApproval.rawValue) }
        try FileManager.default.createDirectory(at: helper.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("binary".utf8).write(to: helper, options: .atomic)
        if injectedFault == .afterHelper { throw FakeAdapterError.injectedFault(FixtureInitFault.afterHelper.rawValue) }
        try validArchive.bytes.write(to: validArchive.path, options: .atomic)
        if injectedFault == .afterArchive { throw FakeAdapterError.injectedFault(FixtureInitFault.afterArchive.rawValue) }
        try Data(bundleJSON.utf8).write(to: bundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json"), options: .atomic)
        try Data(lockJSON.utf8).write(to: bundle.appendingPathComponent("r4_protected_python311.lock.json"), options: .atomic)
        try Data(v2.policy.utf8).write(to: bundle.appendingPathComponent("materialization-policy.json"), options: .atomic)
        try validArchive.bytes.write(to: bundle.appendingPathComponent("artifacts/python.tar.gz"), options: .atomic)
        try Data("wheel".utf8).write(to: bundle.appendingPathComponent("artifacts/wheel.whl"), options: .atomic)
        try Data("a".utf8).write(to: bundle.appendingPathComponent("checkout/a"), options: .atomic)
        self.adapters = .fixture(fileSystem: fakeFS, process: fakeProcess, approvalURL: approval, installedHelperURL: helper)
        self.validatedBundle = try BundleVerifier.verifyBeforeAdmission(request, adapters: adapters)
        if injectedFault == .afterManifest { throw FakeAdapterError.injectedFault(FixtureInitFault.afterManifest.rawValue) }
        try FileManager.default.createDirectory(at: stageRoot, withIntermediateDirectories: true)
        self.stage = try HeldMaterializationStage.makeDisposableForTest(rootURL: stageRoot)
        self.archive.bind(bundle: self.validatedBundle, stage: self.stage)
        if injectedFault == .afterStage { throw FakeAdapterError.injectedFault(FixtureInitFault.afterStage.rawValue) }
    }

    static func valid() throws -> MaterializationFixture {
        let root = URL(fileURLWithPath: "/private/tmp").appendingPathComponent("r4-materialization-\(UUID().uuidString)", isDirectory: true)
        return try valid(at: root, injecting: nil)
    }

    static func valid(injecting fault: FixtureInitFault, at root: URL) throws -> MaterializationFixture {
        try valid(at: root, injecting: fault)
    }

    private static func valid(at root: URL, injecting fault: FixtureInitFault?) throws -> MaterializationFixture {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        let attrs = try FileManager.default.attributesOfItem(atPath: root.path)
        guard let device = attrs[.systemNumber] as? NSNumber,
              let inode = attrs[.systemFileNumber] as? NSNumber else {
            try? FileManager.default.removeItem(at: root)
            throw FakeAdapterError.injectedFault("fixture identity unavailable")
        }
        let identity = (device, inode)
        do {
            return try MaterializationFixture(root: root, capturedIdentity: identity, injectedFault: fault)
        } catch {
            if let current = try? FileManager.default.attributesOfItem(atPath: root.path),
               let type = current[.type] as? FileAttributeType, type != .typeSymbolicLink,
               let currentDevice = current[.systemNumber] as? NSNumber,
               let currentInode = current[.systemFileNumber] as? NSNumber,
               currentDevice == identity.0, currentInode == identity.1 {
                try? FileManager.default.removeItem(at: root)
            }
            throw error
        }
    }

    func removeOnlySelfOwnedFixture() throws {
        let path = root.path
        guard path.hasPrefix("/private/tmp/r4-materialization-") || path.hasPrefix(NSTemporaryDirectory() + "r4-materialization-") else { return }
        if let expected = capturedIdentity,
           let attrs = try? FileManager.default.attributesOfItem(atPath: path),
           let device = attrs[.systemNumber] as? NSNumber,
           let inode = attrs[.systemFileNumber] as? NSNumber,
           device != expected.0 || inode != expected.1 {
            return
        }
        try? FileManager.default.removeItem(at: root)
    }

    func mutating(_ mutation: (MaterializationFixture) -> Void) -> MaterializationFixture { mutation(self); return self }
    func mutating(_ mutation: FixtureMutation) -> MaterializationFixture { apply(mutation); return self }
    private func apply(_ mutation: FixtureMutation) {
        mutationCases.insert(mutation); fakeFS.addMutation(mutation)
        switch mutation {
        case .extraChild: do { try fakeFS.mkdir(request.inputBundle.appendingPathComponent("unexpected").path) } catch { preconditionFailure("fixture extra-child failed: \(error)") }
        case .v1Bundle, .mixedGraph: do { try Data(mutation == .v1Bundle ? "R4_RUNTIME_INPUT_BUNDLE_V1".utf8 : "MIXED_V1_V2_GRAPH".utf8).write(to: request.inputBundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json"), options: .atomic) } catch { preconditionFailure("fixture bundle mutation failed: \(error)") }
        case .wrongApproval: do { try Data("wrong-approval".utf8).write(to: approvalURL, options: .atomic) } catch { preconditionFailure("fixture approval mutation failed: \(error)") }
        case .badLock: do { try Data("bad-lock".utf8).write(to: request.inputBundle.appendingPathComponent("r4_protected_python311.lock.json"), options: .atomic) } catch { preconditionFailure("fixture lock mutation failed: \(error)") }
        case .swappedArchive: do { try Data("swapped-archive".utf8).write(to: request.inputBundle.appendingPathComponent("artifacts/python.tar.gz"), options: .atomic) } catch { preconditionFailure("fixture archive mutation failed: \(error)") }
        case .swappedWheel: do { try Data("swapped-wheel".utf8).write(to: request.inputBundle.appendingPathComponent("artifacts/wheel.whl"), options: .atomic) } catch { preconditionFailure("fixture wheel mutation failed: \(error)") }
        case .swappedCheckout: do { try Data("swapped-checkout".utf8).write(to: request.inputBundle.appendingPathComponent("checkout/file"), options: .atomic) } catch { preconditionFailure("fixture checkout mutation failed: \(error)") }
        case .symlink: do { try FileManager.default.createSymbolicLink(atPath: request.inputBundle.appendingPathComponent("linked").path, withDestinationPath: "/private/tmp") } catch { preconditionFailure("fixture symlink failed: \(error)") }
        case .caseCollision: do { try Data("collision".utf8).write(to: request.inputBundle.appendingPathComponent("Manifest.json"), options: .atomic) } catch { preconditionFailure("fixture case-collision failed: \(error)") }
        case .hardlink: do { try FileManager.default.linkItem(at: request.inputBundle.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json"), to: request.inputBundle.appendingPathComponent("manifest-hardlink")) } catch { preconditionFailure("fixture hardlink failed: \(error)") }
        case .nonRootEUID: fakeProcess.effectiveEUID = 501
        case .copiedHelper: do { try Data("copied-helper".utf8).write(to: helperURL, options: .atomic) } catch { preconditionFailure("fixture helper mutation failed: \(error)") }
        case .badStorage: fakeFS.markStorageRejected()
        case .forbiddenEnvironment: fakeProcess.environmentOverride["HOME"] = "/tmp/forbidden"
        case .inheritedDescriptor: fakeProcess.inheritedDescriptor = true
        case .swapBeforeCopy: break
        }
    }
    func bundleWithExtraChild() -> MaterializationFixture {
        fakeFS.addMutation(.bundleExtraChild)
        do { try fakeFS.mkdir(request.inputBundle.appendingPathComponent("unexpected").path) } catch { preconditionFailure("fixture extra-child failed: \(error)") }
        return self
    }
    func swapBeforeCopy() -> MaterializationFixture {
        fakeFS.addMutation(.swapBeforeCopy)
        // Atomic replacement preserves the old held archive FD but changes the logical child
        // beneath artifacts; the copy's source rebind must refuse to adopt it.
        try? Data("post-validation-archive-swap".utf8).write(to: request.inputBundle.appendingPathComponent("artifacts/python.tar.gz"), options: .atomic)
        return self
    }
    func fault<S: FixtureFaultSeam>(after seam: S) -> MaterializationFixture { fakeFS.fault = seam.fixtureFault; return self }
    func fault(after seam: FixtureFault) -> MaterializationFixture { fakeFS.fault = seam; return self }
    func admissionFault<S: FixtureAdmissionFaultSeam>(_ seam: S) -> MaterializationFixture { storage.admissionFault = seam.fixtureAdmissionFault; return self }
    func admissionFault(_ seam: AdmissionFault) -> MaterializationFixture { storage.admissionFault = seam; return self }
    func swapBefore<S: FixtureSwapSeam>(_ seam: S) -> MaterializationFixture {
        guard seam.fixtureSwap == .copy else { preconditionFailure("unsupported swap seam") }
        return swapBeforeCopy()
    }
    func swapBefore(_ seam: FixtureFault) -> MaterializationFixture {
        guard seam == .copy else { preconditionFailure("unsupported swap seam") }
        return swapBeforeCopy()
    }
    func swapBefore(_ seam: CopySeam) -> MaterializationFixture {
        let relative: String
        switch seam {
        case .manifest: relative = "R4_RUNTIME_INPUT_BUNDLE_V2.json"
        case .lock: relative = "r4_protected_python311.lock.json"
        case .policy: relative = "materialization-policy.json"
        case .checkoutDirectory: relative = "checkout"
        case .checkoutRegular: relative = "checkout/a"
        case .archive: relative = "artifacts/python.tar.gz"
        case .wheel0: relative = "artifacts/wheel.whl"
        }
        lastCopySwapPath = relative
        let target = request.inputBundle.appendingPathComponent(relative)
        if seam == .checkoutDirectory {
            try? FileManager.default.removeItem(at: target)
            try? FileManager.default.createDirectory(at: target, withIntermediateDirectories: false)
        } else { try? Data("post-validation-\(seam.rawValue)".utf8).write(to: target, options: .atomic) }
        return self
    }

    func raceTwo<T>(startBarrier: AdmissionBarrier, timeout: TimeInterval = 3, _ operation: @escaping (RaceCancellation) throws -> T) throws -> [Result<T, Error>] {
        try materializationRaceTwo(startBarrier: startBarrier, timeout: timeout, operation)
    }
}

/// Shared deterministic concurrency primitive used by later admission tests.
func raceTwo<T>(startBarrier: AdmissionBarrier, timeout: TimeInterval = 3, _ operation: @escaping (RaceCancellation) throws -> T) throws -> [Result<T, Error>] {
    try materializationRaceTwo(startBarrier: startBarrier, timeout: timeout, operation)
}

private func materializationRaceTwo<T>(startBarrier: AdmissionBarrier, timeout: TimeInterval, _ operation: @escaping (RaceCancellation) throws -> T) throws -> [Result<T, Error>] {
    startBarrier.arm()
    defer { startBarrier.release(); startBarrier.disarm() }
    let group = DispatchGroup()
    let resultLock = NSLock()
    let cancellation = RaceCancellation()
    var results = Array<Result<T, Error>?>(repeating: nil, count: 2)
    for index in 0..<2 {
        group.enter()
        DispatchQueue.global().async {
            defer { group.leave() }
            do {
                let value = try operation(cancellation)
                resultLock.lock(); results[index] = .success(value); resultLock.unlock()
            } catch {
                resultLock.lock(); results[index] = .failure(error); resultLock.unlock()
            }
        }
    }
    let rendezvousSucceeded = startBarrier.waitForBoth(timeout: timeout)
    guard rendezvousSucceeded else {
        cancellation.cancel()
        startBarrier.cancelAndRelease()
        guard group.wait(timeout: .now() + timeout) == .success else { cancellation.cancel(); group.wait(); throw FakeAdapterError.barrierTimeout }
        throw FakeAdapterError.barrierTimeout
    }
    startBarrier.release()
    guard group.wait(timeout: .now() + timeout) == .success else {
        cancellation.cancel()
        startBarrier.cancelAndRelease()
        group.wait()
        throw FakeAdapterError.barrierTimeout
    }
    guard let first = results[0], let second = results[1] else { throw FakeAdapterError.barrierTimeout }
    return [first, second]
}
