import Darwin
import CryptoKit
import Foundation
import Security

enum ProcessRunnerFailure: Error, Equatable {
    case unheldProgram, invocationMismatch, outputLimit, timeout, nonzeroExit, launchFailed, readFailed, processCleanupIndeterminate, processReapIndeterminate
}

/// The narrow syscall boundary around the real launcher engine.  It is internal
/// so production callers cannot substitute process authority; tests can only
/// reach it through a consumed opaque `SpawnAuthorization`.
enum SpawnFileAction: Equatable { case duplicate(source: Int32, destination: Int32), changeDirectory(fd: Int32), close(fd: Int32) }
struct SpawnAttributes: Equatable { let flags: Int32; let processGroup: pid_t }
struct SpawnRequest: Equatable { let command: ActualChildCommand; let cwd: String; let fileActions: [SpawnFileAction]; let attributes: SpawnAttributes }
enum SpawnReadResult { case bytes([UInt8]), endOfFile, wouldBlock, interrupted, failure }
/// `waitid(..., WNOWAIT)` observes a leader without releasing its PID.  The
/// leader stays waitable until the process group is known to be gone.
enum SpawnLeaderObservation { case running, exited(Int32), interrupted, failure }
/// `waitpid(..., WNOHANG)` never waits for a live leader.  A zero return keeps
/// the already-reserved PID under lifecycle ownership for another finite poll.
enum SpawnLeaderReapResult: Equatable { case stillRunning, reaped(Int32), interrupted, noChild, failure }
enum SpawnGroupProbeResult { case exists, absent, interrupted, failure }

struct RawWaitpidResult: Equatable {
    let returnedPID: pid_t
    let status: Int32
    let error: Int32
}

/// Maps the raw `waitpid` contract at the one production final-reap boundary.
/// DEBUG tests can interpose only this raw syscall result; release binaries
/// always call Darwin directly and expose no execution-authority injection.
enum ProductionWaitpidReaper {
    static func reapLeader(_ pid: pid_t) -> SpawnLeaderReapResult {
        var status: Int32 = 0
        let result = Darwin.waitpid(pid, &status, WNOHANG)
        return map(RawWaitpidResult(returnedPID: result, status: status, error: result == -1 ? errno : 0), for: pid)
    }

    #if DEBUG
    static func reapLeaderForTesting(pid: pid_t,
                                     invoking waitpid: (pid_t, Int32) -> RawWaitpidResult) -> SpawnLeaderReapResult {
        map(waitpid(pid, WNOHANG), for: pid)
    }
    #endif

    private static func map(_ raw: RawWaitpidResult, for pid: pid_t) -> SpawnLeaderReapResult {
        if raw.returnedPID == pid { return .reaped(raw.status) }
        if raw.returnedPID == 0 { return .stillRunning }
        if raw.error == EINTR { return .interrupted }
        // The leader was previously observed with WNOWAIT.  ECHILD is
        // therefore not evidence it was safely reaped by this lifecycle.
        return raw.error == ECHILD ? .noChild : .failure
    }
}

struct SpawnSyscalls {
    let openNull: () throws -> Int32
    let makePipe: () throws -> (Int32, Int32)
    let setCloseOnExec: (Int32) throws -> Void
    let spawn: (SpawnRequest) throws -> pid_t
    let close: (Int32) -> Void
    let read: (Int32, Int) -> SpawnReadResult
    let observeLeader: (pid_t) -> SpawnLeaderObservation
    let reapLeader: (pid_t) -> SpawnLeaderReapResult
    let probeGroup: (pid_t) -> SpawnGroupProbeResult
    let signalGroup: (pid_t, Int32) -> Void
    let nowNanoseconds: () -> UInt64
    let sleepNanoseconds: (UInt64) -> Void

    static let production = SpawnSyscalls(
        openNull: {
            let fd = Darwin.open("/dev/null", O_RDONLY | O_CLOEXEC)
            guard fd >= 0 else { throw ProcessRunnerFailure.launchFailed }; return fd
        },
        makePipe: {
            var pair = [Int32](repeating: -1, count: 2)
            guard pair.withUnsafeMutableBufferPointer({ Darwin.pipe($0.baseAddress!) }) == 0 else { throw ProcessRunnerFailure.launchFailed }
            guard fcntl(pair[0], F_SETFL, O_NONBLOCK) == 0 else {
                _ = Darwin.close(pair[0]); _ = Darwin.close(pair[1]); throw ProcessRunnerFailure.launchFailed
            }
            return (pair[0], pair[1])
        },
        setCloseOnExec: { fd in guard fcntl(fd, F_SETFD, FD_CLOEXEC) == 0 else { throw ProcessRunnerFailure.launchFailed } },
        spawn: { request in
            var actions: posix_spawn_file_actions_t? = nil
            guard posix_spawn_file_actions_init(&actions) == 0 else { throw ProcessRunnerFailure.launchFailed }
            defer { posix_spawn_file_actions_destroy(&actions) }
            for action in request.fileActions {
                let result: Int32
                switch action {
                case let .duplicate(source, destination): result = posix_spawn_file_actions_adddup2(&actions, source, destination)
                case let .changeDirectory(fd): result = posix_spawn_file_actions_addfchdir_np(&actions, fd)
                case let .close(fd): result = posix_spawn_file_actions_addclose(&actions, fd)
                }
                guard result == 0 else { throw ProcessRunnerFailure.launchFailed }
            }
            var attributes: posix_spawnattr_t? = nil
            guard posix_spawnattr_init(&attributes) == 0 else { throw ProcessRunnerFailure.launchFailed }
            defer { posix_spawnattr_destroy(&attributes) }
            guard posix_spawnattr_setflags(&attributes, Int16(request.attributes.flags)) == 0,
                  posix_spawnattr_setpgroup(&attributes, request.attributes.processGroup) == 0 else { throw ProcessRunnerFailure.launchFailed }
            let argv = request.command.argv.map { strdup($0) }; let env = request.command.environment.map { strdup($0) }
            defer { argv.forEach { free($0) }; env.forEach { free($0) } }
            var argvPointers = argv + [nil], envPointers = env + [nil], pid: pid_t = 0
            let status = request.command.program.withCString { posix_spawn(&pid, $0, &actions, &attributes, &argvPointers, &envPointers) }
            guard status == 0 else { throw ProcessRunnerFailure.launchFailed }; return pid
        },
        close: { _ = Darwin.close($0) },
        read: { fd, count in
            var bytes = [UInt8](repeating: 0, count: count); let read = Darwin.read(fd, &bytes, count)
            if read > 0 { return .bytes(Array(bytes.prefix(read))) }
            if read == 0 { return .endOfFile }
            if errno == EINTR { return .interrupted }
            if errno == EAGAIN || errno == EWOULDBLOCK { return .wouldBlock }
            return .failure
        },
        observeLeader: { pid in
            var info = siginfo_t()
            let result = Darwin.waitid(P_PID, id_t(pid), &info, WEXITED | WNOHANG | WNOWAIT)
            guard result == 0 else { return errno == EINTR ? .interrupted : .failure }
            guard info.si_pid != 0 else { return .running }
            return .exited(Int32(info.si_status))
        },
        reapLeader: { ProductionWaitpidReaper.reapLeader($0) },
        probeGroup: { group in
            if kill(-group, 0) == 0 || errno == EPERM { return .exists }
            return errno == ESRCH ? .absent : (errno == EINTR ? .interrupted : .failure)
        },
        signalGroup: { group, signal in _ = kill(-group, signal) },
        nowNanoseconds: { DispatchTime.now().uptimeNanoseconds },
        sleepNanoseconds: { nanos in usleep(useconds_t(min(nanos / 1_000, UInt64(UInt32.max)))) }
    )
}

struct ProcessResult: Equatable { let status: Int32; let output: [UInt8] }

enum FinalLayoutObserverFailure: Error, Equatable { case unavailable, drift, phase, consumed, unsafeLayout }
enum LaunchObservationPhase: String { case preInstall, postInstall, postProbe }
struct FinalLaunchObservation {
    let layout: ReboundFinalLaunchLayout
    let interpreterMachOObservation: MachOObservation
    let stagedMachOObservations: [StagedMachOObservation]
}
#if DEBUG
/// Injectable observation exists solely in debug test binaries.  Production
/// issuance below has no initializer parameter capable of replacing Darwin's
/// Security/Mach-O verification.
protocol FinalLayoutObserver {
    func verify(layout: HeldFinalLaunchLayout, policy: MaterializationPolicy, approval: Task14ApprovalV2,
                phase: LaunchObservationPhase) throws -> FinalLaunchObservation
}
#endif

/// The one production observer.  It intentionally performs all Security work
/// from descriptor-derived spellings and rebinds both before and after those
/// pathname-only Security APIs.  `SecStaticCode` cannot consume an fd on
/// Darwin, so the fixed root-owned parent/child relationship is an explicit
/// assumption checked at each rebind rather than hidden behind a raw caller path.
struct DarwinFinalLayoutObserver {
    func verify(layout: HeldFinalLaunchLayout, policy: MaterializationPolicy, approval: Task14ApprovalV2,
                phase: LaunchObservationPhase) throws -> FinalLaunchObservation {
        let bound = try layout.rebindDescriptors()
        // All materialization stage children are root-owned and fixed after the
        // descriptor rebind.  A non-root disposable fixture deliberately cannot
        // cross this boundary and therefore never synthesizes a Security success.
        try POSIX.requireDirectory(bound.work, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireDirectory(bound.root, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireDirectory(bound.runtime, uid: 0, gid: 0, mode: 0o700)
        let launcher = try observedLauncher()
        guard launcher.path == policy.sandboxLauncher.path,
              launcher.descriptorIdentity == policy.sandboxLauncher.descriptorIdentitySHA256,
              launcher.bytes == policy.sandboxLauncher.sha256,
              launcher.identity.sha256 == policy.sandboxLauncher.codesignIdentity.sha256,
              launcher.descriptorIdentity == approval.sandboxLauncherDescriptorIdentitySHA256,
              launcher.bytes == approval.sandboxLauncherSHA256,
              launcher.identity.sha256 == approval.sandboxLauncherCodesignIdentitySHA256,
              launcher.identity.identifier == "com.apple.sandbox-exec",
              launcher.identity.designatedRequirement == "identifier \"com.apple.sandbox-exec\" and anchor apple",
              launcher.identity.teamIdentifier == nil else { throw FinalLayoutObserverFailure.drift }

        let runtimeRoot = bound.stage + "/runtime"
        let finalRuntime = runtimeRoot + "/closure/python311"
        let objects = try enumerateRegularObjects(in: bound.runtime, absolute: runtimeRoot)
        let interpreterPath = bound.stage + "/" + policy.baseInterpreter.path
        guard let interpreter = objects[interpreterPath] else { throw FinalLayoutObserverFailure.unsafeLayout }
        let reboundInterpreter = try interpreter.descriptor.rebind(in: interpreter.parent, name: interpreter.name)
        guard reboundInterpreter.device == bound.interpreter.device, reboundInterpreter.inode == bound.interpreter.inode else { throw FinalLayoutObserverFailure.drift }

        var interpreterSlices: [MachOSlice] = []
        var interpreterDependencies: [MachODependency] = []
        var stagedMachOObservations: [StagedMachOObservation] = []
        for object in objects.values.sorted(by: { $0.path < $1.path }) {
            let before = try object.descriptor.rebind(in: object.parent, name: object.name)
            let bytes = try POSIX.readAll(before)
            let slices: [MachODescriptor]
            do { slices = try MachOParser.parseSlices(bytes) }
            catch MachOFailure.unsupported { continue }
            let security = try observeSecurity(descriptor: before, parent: object.parent, name: object.name, path: object.path)
            var dependencies: [MachODependency] = []
            for slice in slices {
                for dependency in slice.dependencies {
                    let resolved = try MachOValidator.resolve(dependency, loader: object.path, executable: interpreterPath,
                                                              rpaths: slice.rpaths, finalRuntime: finalRuntime) { candidate in
                        try reboundDependency(candidate, staged: objects)
                    }
                    let row = MachODependency(loader: relativeResolved(object.path, stage: bound.stage), dependency: dependency, resolved: relativeResolved(resolved, stage: bound.stage))
                    dependencies.append(row)
                    if object.path == interpreterPath { interpreterDependencies.append(row) }
                }
            }
            let machO = try machOObservation(path: relativeResolved(object.path, stage: bound.stage), slices: slices.map(\.slice), dependencies: dependencies)
            stagedMachOObservations.append(try StagedMachOObservation(path: machO.path, descriptorIdentitySHA256: descriptorIdentity(before, path: object.path), sha256: descriptorSHA256(before), codesignIdentitySHA256: security.sha256, machOObservation: machO))
            if object.path == interpreterPath { interpreterSlices = slices.map(\.slice) }
        }
        guard !interpreterSlices.isEmpty else { throw FinalLayoutObserverFailure.unsafeLayout }
        let interpreterSecurity = try observeSecurity(descriptor: reboundInterpreter, parent: interpreter.parent,
                                                      name: interpreter.name, path: interpreter.path)
        let interpreterBytes = try descriptorSHA256(reboundInterpreter)
        let observation = try machOObservation(path: policy.baseInterpreter.path, slices: interpreterSlices, dependencies: interpreterDependencies)
        guard interpreterBytes == policy.baseInterpreter.sha256,
              interpreterBytes == approval.baseInterpreterSHA256,
              try observation.sha256() == policy.baseInterpreter.machOSHA256,
              try observation.sha256() == approval.baseInterpreterMachOSHA256,
              interpreterSecurity.sha256 == policy.baseInterpreter.codesignIdentity.sha256,
              interpreterSecurity.sha256 == approval.baseInterpreterCodesignIdentitySHA256 else { throw FinalLayoutObserverFailure.drift }
        _ = phase // Phase is deliberately explicit in the issuance API and audit surface.
        return FinalLaunchObservation(layout: bound, interpreterMachOObservation: observation, stagedMachOObservations: stagedMachOObservations.sorted { $0.path < $1.path })
    }

    private struct ObservedFile {
        let path: String; let descriptorIdentity: SHA256Digest; let bytes: SHA256Digest; let identity: CodeSignIdentity
    }
    private struct RuntimeRegularObject {
        let parent: HeldDescriptor; let name: String; let descriptor: HeldDescriptor; let path: String
    }

    private func observedLauncher() throws -> ObservedFile {
        let held = try POSIX.openTrustedRootOwnedRegular("/usr/bin/sandbox-exec")
        let path = try POSIX.descriptorPath(held.file)
        guard path == "/usr/bin/sandbox-exec" else { throw FinalLayoutObserverFailure.drift }
        return try observedFile(descriptor: held.file, parent: held.parent, name: held.name, path: path)
    }

    private func enumerateRegularObjects(in directory: HeldDescriptor, absolute: String) throws -> [String: RuntimeRegularObject] {
        var result: [String: RuntimeRegularObject] = [:]
        for name in try POSIX.directoryEntryNames(directory).sorted() {
            let child = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(child)
            let path = absolute + "/" + name
            if metadata.type == S_IFDIR {
                guard metadata.uid == 0, metadata.gid == 0, metadata.nlink >= 1, (metadata.mode & 0o022) == 0 else { throw FinalLayoutObserverFailure.unsafeLayout }
                try POSIX.requireNoExtendedACL(child)
                let rebound = try child.rebind(in: directory, name: name, directory: true)
                try result.merge(try enumerateRegularObjects(in: rebound, absolute: path)) { _, _ in throw FinalLayoutObserverFailure.unsafeLayout }
            } else if metadata.type == S_IFREG {
                guard metadata.uid == 0, metadata.gid == 0, metadata.nlink == 1, (metadata.mode & 0o022) == 0 else { throw FinalLayoutObserverFailure.unsafeLayout }
                try POSIX.requireNoExtendedACL(child)
                let rebound = try child.rebind(in: directory, name: name)
                result[path] = RuntimeRegularObject(parent: directory, name: name, descriptor: rebound, path: path)
            } else { throw FinalLayoutObserverFailure.unsafeLayout }
        }
        return result
    }

    private func reboundDependency(_ path: String, staged: [String: RuntimeRegularObject]) throws -> Bool {
        if let object = staged[path] {
            _ = try object.descriptor.rebind(in: object.parent, name: object.name)
            return true
        }
        guard path.hasPrefix("/System/") || path.hasPrefix("/usr/lib/") else { return false }
        _ = try POSIX.openTrustedRootOwnedRegular(path)
        return true
    }

    private func relativeResolved(_ path: String, stage: String) -> String {
        path.hasPrefix(stage + "/") ? String(path.dropFirst(stage.count + 1)) : path
    }

    private func observedFile(descriptor: HeldDescriptor, parent: HeldDescriptor, name: String, path: String) throws -> ObservedFile {
        let before = try descriptor.rebind(in: parent, name: name)
        guard try POSIX.descriptorPath(before) == path else { throw FinalLayoutObserverFailure.drift }
        let identity = try observeSecurity(descriptor: before, parent: parent, name: name, path: path)
        let after = try descriptor.rebind(in: parent, name: name)
        guard try POSIX.descriptorPath(after) == path else { throw FinalLayoutObserverFailure.drift }
        return ObservedFile(path: path, descriptorIdentity: try descriptorIdentity(after, path: path), bytes: try descriptorSHA256(after), identity: identity)
    }

    private func observeSecurity(descriptor: HeldDescriptor, parent: HeldDescriptor, name: String, path: String) throws -> CodeSignIdentity {
        let before = try descriptor.rebind(in: parent, name: name)
        guard try POSIX.descriptorPath(before) == path else { throw FinalLayoutObserverFailure.drift }
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(URL(fileURLWithPath: path) as CFURL, SecCSFlags(), &code) == errSecSuccess,
              let code,
              SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSStrictValidate), nil) == errSecSuccess else { throw FinalLayoutObserverFailure.drift }
        var requirement: SecRequirement?
        var requirementText: CFString?
        guard SecCodeCopyDesignatedRequirement(code, SecCSFlags(), &requirement) == errSecSuccess, let requirement,
              SecRequirementCopyString(requirement, SecCSFlags(), &requirementText) == errSecSuccess,
              let requirementText else { throw FinalLayoutObserverFailure.drift }
        var signingInfo: CFDictionary?
        guard SecCodeCopySigningInformation(code, SecCSFlags(rawValue: kSecCSSigningInformation), &signingInfo) == errSecSuccess,
              let info = signingInfo as? [CFString: Any], let identifier = info[kSecCodeInfoIdentifier] as? String,
              let certificates = info[kSecCodeInfoCertificates] as? [SecCertificate], !certificates.isEmpty else { throw FinalLayoutObserverFailure.drift }
        let certificateHashes = certificates.map { CanonicalJSON.sha256(Array(SecCertificateCopyData($0) as Data)) }
        let chain = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_CERTIFICATE_CHAIN\0", .array(certificateHashes.map(JSONValue.string))))
        let team = info[kSecCodeInfoTeamIdentifier] as? String
        let canonical = JSONValue.object([("identifier", .string(identifier)), ("designated_requirement", .string(requirementText as String)), ("team_identifier", team.map(JSONValue.string) ?? .null), ("certificate_chain_sha256", .string(chain.value))])
        let digest = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", canonical))
        let after = try descriptor.rebind(in: parent, name: name)
        guard try POSIX.descriptorPath(after) == path else { throw FinalLayoutObserverFailure.drift }
        return CodeSignIdentity(identifier: identifier, designatedRequirement: requirementText as String, teamIdentifier: team, certificateChainSHA256: chain, sha256: digest)
    }

    private func descriptorSHA256(_ descriptor: HeldDescriptor) throws -> SHA256Digest {
        try SHA256Digest(CanonicalJSON.sha256(try POSIX.readAll(descriptor)))
    }

    private func descriptorIdentity(_ descriptor: HeldDescriptor, path: String) throws -> SHA256Digest {
        let metadata = try POSIX.metadata(descriptor)
        let canonical: JSONValue = .object([("path", .string(path)), ("st_dev", .integer(Int64(metadata.device))),
                                             ("st_ino", .integer(Int64(metadata.inode))), ("st_gen", .integer(Int64(metadata.generation))),
                                             ("mode", .integer(Int64(metadata.mode))), ("nlink", .integer(Int64(metadata.nlink))),
                                             ("size", .integer(metadata.size))])
        return try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0", canonical))
    }

    private func machOObservation(path: String, slices: [MachOSlice], dependencies: [MachODependency]) throws -> MachOObservation {
        let orderedSlices = try slices.map { slice -> JSONValue in
            guard let uuid = slice.uuid, let offset = slice.codeSignatureOffset, let size = slice.codeSignatureSize else { throw FinalLayoutObserverFailure.unsafeLayout }
            return .object([("cpu_type", .integer(slice.cpuType)), ("cpu_subtype", .integer(slice.cpuSubtype)),
                            ("file_type", .integer(slice.fileType)), ("uuid", .string(uuid)),
                            ("code_signature_offset", .integer(offset)), ("code_signature_size", .integer(size))])
        }.sorted { lhs, rhs in
            guard case .object(let left) = lhs, case .object(let right) = rhs else { return false }
            let tuple: ([(String, JSONValue)]) -> (Int64, Int64) = { values in
                let map = Dictionary(uniqueKeysWithValues: values); guard case .integer(let cpu) = map["cpu_type"], case .integer(let subtype) = map["cpu_subtype"] else { return (0, 0) }; return (cpu, subtype)
            }
            return tuple(left) < tuple(right)
        }
        let dependencyRows = dependencies.sorted { [$0.loader, $0.dependency, $0.resolved].lexicographicallyPrecedes([$1.loader, $1.dependency, $1.resolved]) }.map(\.canonicalValue)
        return MachOObservation(path: path, slices: .array(orderedSlices), dependencies: .array(dependencyRows))
    }
}

#if DEBUG
extension DarwinFinalLayoutObserver: FinalLayoutObserver {}
#endif

/// Only this verified authority can authorize a child spawn.  It binds the held
/// Task 7 layout to the held policy/Task 14 tuple and rebinds immediately before spawn.
final class VerifiedLaunchCapability {
    /// A reference capability deliberately has no copying initializer.  The
    /// lock makes aliases of the same authorization a single, atomic use.
    final class SpawnAuthorization {
        fileprivate let command: ActualChildCommand
        fileprivate let observation: FinalLaunchObservation
        private let layout: HeldFinalLaunchLayout
        private let policy: MaterializationPolicy
        private let approval: Task14ApprovalV2
        private let phase: LaunchObservationPhase
        private let verifyObservation: (LaunchObservationPhase) throws -> FinalLaunchObservation
        /// This is the descriptor actually supplied to `addfchdir_np`.  Its
        /// path is evidence only; no launch path is opened from a string.
        private let cwd: HeldDescriptor
        private let observedCWDPath: String
        private let lock = NSLock()
        private var consumed = false

        /// Private to the capability/authorization declaration; no module API
        /// accepts a raw command or plan.
        private init(command: ActualChildCommand, observation: FinalLaunchObservation, layout: HeldFinalLaunchLayout,
                     policy: MaterializationPolicy, approval: Task14ApprovalV2,
                     phase: LaunchObservationPhase, verifyObservation: @escaping (LaunchObservationPhase) throws -> FinalLaunchObservation,
                     cwd: HeldDescriptor, observedCWDPath: String) {
            self.command = command; self.observation = observation; self.layout = layout; self.policy = policy; self.approval = approval
            self.phase = phase; self.verifyObservation = verifyObservation; self.cwd = cwd; self.observedCWDPath = observedCWDPath
        }

        fileprivate static func issue(command: ActualChildCommand, observation: FinalLaunchObservation, layout: HeldFinalLaunchLayout,
                                      policy: MaterializationPolicy, approval: Task14ApprovalV2,
                                      phase: LaunchObservationPhase, verifyObservation: @escaping (LaunchObservationPhase) throws -> FinalLaunchObservation,
                                      cwd: HeldDescriptor, observedCWDPath: String) -> SpawnAuthorization {
            SpawnAuthorization(command: command, observation: observation, layout: layout, policy: policy, approval: approval,
                               phase: phase, verifyObservation: verifyObservation, cwd: cwd, observedCWDPath: observedCWDPath)
        }

        fileprivate func consumeImmediatelyBeforeSpawn() throws -> AuthorizedSpawn {
            lock.lock()
            guard !consumed else { lock.unlock(); throw FinalLayoutObserverFailure.consumed }
            consumed = true
            lock.unlock()

            // Reissue all path-derived evidence from the held authority just
            // before the only syscall boundary.  Equality covers profile,
            // argv, environment, launcher/interpreter tuple, and all digests.
            let rebound = try verifyObservation(phase)
            let freshCWDPath = try POSIX.descriptorPath(rebound.layout.runtime)
            let recomputed = try ProcessRunner.command(policy: policy, layout: rebound.layout, phase: phase)
            guard freshCWDPath == rebound.layout.stage + "/runtime",
                  freshCWDPath == observedCWDPath,
                  cwd.device == rebound.layout.runtime.device,
                  cwd.inode == rebound.layout.runtime.inode,
                  try POSIX.descriptorPath(cwd) == freshCWDPath,
                  command == recomputed else {
                throw ProcessRunnerFailure.invocationMismatch
            }
            return AuthorizedSpawn(command: command, cwd: cwd, cwdPath: freshCWDPath)
        }

        #if DEBUG
        /// Test evidence accessor.  It cannot mint, alter, or execute a raw
        /// command; tests still need a real held layout to obtain this object.
        var testCommand: ActualChildCommand { command }
        var testCWDPath: String { observedCWDPath }
        var testCWDFD: Int32 { cwd.fd }
        #endif
    }

    private let layout: HeldFinalLaunchLayout; let policy: MaterializationPolicy; let approval: Task14ApprovalV2; private let phase: LaunchObservationPhase; private let lock = NSLock(); private var consumed = false
    #if DEBUG
    private let testObserver: FinalLayoutObserver?
    #endif

    /// Production's sole factory.  It always invokes the concrete Darwin
    /// observer; no caller can substitute a successful Security result.
    init(layout: HeldFinalLaunchLayout, policy: MaterializationPolicy, approval: Task14ApprovalV2,
         phase: LaunchObservationPhase = .preInstall) throws {
        guard approval.materializationPolicySHA256 == policy.sha256,
              approval.sandboxLauncherPath == policy.sandboxLauncher.path,
              approval.sandboxLauncherDescriptorIdentitySHA256 == policy.sandboxLauncher.descriptorIdentitySHA256,
              approval.sandboxLauncherSHA256 == policy.sandboxLauncher.sha256,
              approval.sandboxLauncherCodesignIdentitySHA256 == policy.sandboxLauncher.codesignIdentity.sha256,
              approval.baseInterpreterSHA256 == policy.baseInterpreter.sha256,
              approval.baseInterpreterMachOSHA256 == policy.baseInterpreter.machOSHA256,
              approval.baseInterpreterCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256 else { throw FinalLayoutObserverFailure.drift }
        self.layout = layout; self.policy = policy; self.approval = approval; self.phase = phase
        #if DEBUG
        self.testObserver = nil
        #endif
        _ = try verify(phase)
    }

    #if DEBUG
    /// DEBUG-only test seam.  It is omitted from release products and tests
    /// only disposable descriptor-bound layouts; it never creates a release
    /// authority or synthesizes Darwin Security success.
    init(layout: HeldFinalLaunchLayout, policy: MaterializationPolicy, approval: Task14ApprovalV2,
         observer: FinalLayoutObserver, phase: LaunchObservationPhase = .preInstall) throws {
        guard approval.materializationPolicySHA256 == policy.sha256,
              approval.sandboxLauncherPath == policy.sandboxLauncher.path,
              approval.sandboxLauncherDescriptorIdentitySHA256 == policy.sandboxLauncher.descriptorIdentitySHA256,
              approval.sandboxLauncherSHA256 == policy.sandboxLauncher.sha256,
              approval.sandboxLauncherCodesignIdentitySHA256 == policy.sandboxLauncher.codesignIdentity.sha256,
              approval.baseInterpreterSHA256 == policy.baseInterpreter.sha256,
              approval.baseInterpreterMachOSHA256 == policy.baseInterpreter.machOSHA256,
              approval.baseInterpreterCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256 else { throw FinalLayoutObserverFailure.drift }
        self.layout = layout; self.policy = policy; self.approval = approval; self.phase = phase; self.testObserver = observer
        _ = try verify(phase)
    }
    #endif

    private func verify(_ requestedPhase: LaunchObservationPhase) throws -> FinalLaunchObservation {
        #if DEBUG
        if let testObserver { return try testObserver.verify(layout: layout, policy: policy, approval: approval, phase: requestedPhase) }
        #endif
        return try DarwinFinalLayoutObserver().verify(layout: layout, policy: policy, approval: approval, phase: requestedPhase)
    }
    fileprivate func consume(for expected: LaunchObservationPhase) throws -> FinalLaunchObservation {
        lock.lock()
        guard !consumed else { lock.unlock(); throw FinalLayoutObserverFailure.consumed }
        guard phase == expected else { lock.unlock(); throw FinalLayoutObserverFailure.phase }
        consumed = true
        lock.unlock()
        let rebound = try verify(phase)
        return rebound
    }
    fileprivate func next(after expected: LaunchObservationPhase) throws -> VerifiedLaunchCapability {
        lock.lock()
        let mayAdvance = consumed && phase == expected
        lock.unlock()
        guard mayAdvance else { throw FinalLayoutObserverFailure.phase }
        let next: LaunchObservationPhase
        switch expected { case .preInstall: next = .postInstall; case .postInstall: next = .postProbe; case .postProbe: throw FinalLayoutObserverFailure.phase }
        #if DEBUG
        if let testObserver { return try VerifiedLaunchCapability(layout: layout, policy: policy, approval: approval, observer: testObserver, phase: next) }
        #endif
        return try VerifiedLaunchCapability(layout: layout, policy: policy, approval: approval, phase: next)
    }

    fileprivate func issueAuthorization(for expected: LaunchObservationPhase) throws -> SpawnAuthorization {
        let observation = try consume(for: expected)
        let command = try ProcessRunner.command(policy: policy, layout: observation.layout, phase: expected)
        let cwdPath = try POSIX.descriptorPath(observation.layout.runtime)
        guard cwdPath == observation.layout.stage + "/runtime" else { throw ProcessRunnerFailure.invocationMismatch }
        return SpawnAuthorization.issue(command: command, observation: observation, layout: layout, policy: policy, approval: approval,
                                        phase: expected, verifyObservation: { requested in try self.verify(requested) }, cwd: observation.layout.runtime,
                                        observedCWDPath: cwdPath)
    }

    #if DEBUG
    /// Test-only fault injection remains capability-bound: it first consumes a
    /// real disposable final layout and cannot manufacture a Security/layout
    /// observation or a raw execution route.
    func issueTestAuthorization(for expected: LaunchObservationPhase,
                                mutating command: (ActualChildCommand) -> ActualChildCommand) throws -> SpawnAuthorization {
        let observation = try consume(for: expected)
        let exact = try ProcessRunner.command(policy: policy, layout: observation.layout, phase: expected)
        let cwdPath = try POSIX.descriptorPath(observation.layout.runtime)
        guard cwdPath == observation.layout.stage + "/runtime" else { throw ProcessRunnerFailure.invocationMismatch }
        return SpawnAuthorization.issue(command: command(exact), observation: observation, layout: layout, policy: policy, approval: approval,
                                        phase: expected, verifyObservation: { requested in try self.verify(requested) }, cwd: observation.layout.runtime,
                                        observedCWDPath: cwdPath)
    }
    #endif
}

private struct AuthorizedSpawn {
    let command: ActualChildCommand
    let cwd: HeldDescriptor
    let cwdPath: String
}

/// The process surface is intentionally launcher-only.  A stage pathname is not
/// authority: production callers must first obtain a descriptor-bound final
/// runtime capability, then use the closed authorization below after its rebind gate.
enum ProcessRunner {
    /// Retired compatibility entrypoint.  It must never convert an arbitrary
    /// pathname into a runnable command.
    static func command(policy: MaterializationPolicy, stagePath: String) throws -> ActualChildCommand {
        _ = policy; _ = stagePath
        throw ProcessRunnerFailure.unheldProgram
    }

    static func pipPlan(_ capability: VerifiedLaunchCapability) throws -> VerifiedLaunchCapability.SpawnAuthorization {
        let authorization = try capability.issueAuthorization(for: .preInstall)
        return authorization
    }

    static func importPlan(_ capability: VerifiedLaunchCapability) throws -> VerifiedLaunchCapability.SpawnAuthorization {
        try capability.issueAuthorization(for: .postInstall)
    }

    /// Reconstructs one exact command only from a freshly rebound final layout.
    /// It is intentionally private to the capability issuance/revalidation path.
    fileprivate static func command(policy: MaterializationPolicy, layout: ReboundFinalLaunchLayout,
                                    phase: LaunchObservationPhase) throws -> ActualChildCommand {
        let stage = layout.stage
        let childArgv: [String]
        switch phase {
        case .preInstall:
            let wheels = policy.wheelFilenames.map { layout.stage + "/wheelhouse/" + $0 }
            let fixed = ["-I", "-s", "-E", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check"]
            guard policy.pipArgv.count == fixed.count + policy.wheelFilenames.count,
                  Array(policy.pipArgv.prefix(fixed.count)) == fixed,
                  policy.pipArgv.dropFirst(fixed.count).elementsEqual(policy.wheelFilenames.map { MaterializationPolicy.stageToken + "/wheelhouse/" + $0 }),
                  !policy.pipArgv.contains(where: { $0 == MaterializationPolicy.stageToken || $0.hasPrefix("http") || $0 == "--find-links" }) else { throw ProcessRunnerFailure.invocationMismatch }
            childArgv = fixed + wheels
        case .postInstall:
            childArgv = policy.importArgv
        case .postProbe:
            throw FinalLayoutObserverFailure.phase
        }
        return try command(policy: policy, stage: stage, childArgv: childArgv)
    }

    static func spawnPinnedLauncher(_ authorization: VerifiedLaunchCapability.SpawnAuthorization) throws -> ProcessResult {
        try spawn(authorization, syscalls: .production)
    }

    /// Internal deterministic lifecycle seam.  It remains behind the same
    /// closed-plan validation as the production engine and accepts no paths,
    /// raw argv, or descriptor authority from a caller.  It is DEBUG-only so
    /// a release binary exposes no injected syscall/result surface.
    #if DEBUG
    static func spawnPinnedLauncher(_ authorization: VerifiedLaunchCapability.SpawnAuthorization, syscalls: SpawnSyscalls) throws -> ProcessResult {
        try spawn(authorization, syscalls: syscalls)
    }
    #endif

    #if DEBUG
    /// Test seam only.  The same closed validation executes before recording;
    /// fakes cannot report a successful direct-interpreter launch.  It is not
    /// compiled into release binaries.
    static func spawnPinnedLauncher(_ authorization: VerifiedLaunchCapability.SpawnAuthorization,
                                    execute: (ActualChildCommand) throws -> ProcessResult) throws -> ProcessResult {
        let authorized = try authorization.consumeImmediatelyBeforeSpawn()
        return try execute(authorized.command)
    }

    /// Closed two-launch ceremony.  Advancing to either later phase is possible
    /// only after the preceding capability was consumed and re-observed; any
    /// execution, observer, or report error returns before another launch.
    static func installAndProbe(_ preInstall: VerifiedLaunchCapability,
                                execute: (VerifiedLaunchCapability.SpawnAuthorization) throws -> ProcessResult) throws -> InstallationObservation {
        let installer = try pipPlan(preInstall)
        _ = try execute(installer)
        let postInstall = try preInstall.next(after: .preInstall)
        let importer = try importPlan(postInstall)
        let report = try execute(importer)
        let postProbe = try postInstall.next(after: .postInstall)
        let finalObservation = try postProbe.consume(for: .postProbe)
        guard preservesPreInstallObjects(installer.observation.stagedMachOObservations, in: importer.observation.stagedMachOObservations),
              importer.observation.stagedMachOObservations == finalObservation.stagedMachOObservations,
              try finalObservation.interpreterMachOObservation.sha256() == preInstall.policy.baseInterpreter.machOSHA256 else { throw FinalLayoutObserverFailure.drift }
        let stage = stagePrefix(installer.command.argv[3])
        let probe = try parseProbe(report.output, policy: preInstall.policy, stage: stage)
        let execution = try ExecutionObservation(stageRoot: stage, actualProfileUTF8: installer.command.argv[2],
                                                 environment: installer.command.environment, installerCommand: installer.command,
                                                 importCommand: importer.command,
                                                 interpreterMachOObservation: finalObservation.interpreterMachOObservation,
                                                 preInstallStagedMachOObservations: installer.observation.stagedMachOObservations,
                                                 stagedMachOObservations: finalObservation.stagedMachOObservations)
        return try InstallationObservation(executionObservation: execution, probe: probe)
    }
    #endif

    /// The only production ceremony entrypoint.  It offers no executor, path,
    /// or descriptor substitution: the held capability and real spawn engine
    /// remain the sole authorities.
    static func installAndProbe(_ preInstall: VerifiedLaunchCapability) throws -> InstallationObservation {
        let installer = try pipPlan(preInstall)
        _ = try spawnPinnedLauncher(installer)
        let postInstall = try preInstall.next(after: .preInstall)
        let importer = try importPlan(postInstall)
        let report = try spawnPinnedLauncher(importer)
        let postProbe = try postInstall.next(after: .postInstall)
        let finalObservation = try postProbe.consume(for: .postProbe)
        guard preservesPreInstallObjects(installer.observation.stagedMachOObservations, in: importer.observation.stagedMachOObservations),
              importer.observation.stagedMachOObservations == finalObservation.stagedMachOObservations,
              try finalObservation.interpreterMachOObservation.sha256() == preInstall.policy.baseInterpreter.machOSHA256 else { throw FinalLayoutObserverFailure.drift }
        let stage = stagePrefix(installer.command.argv[3])
        let probe = try parseProbe(report.output, policy: preInstall.policy, stage: stage)
        let execution = try ExecutionObservation(stageRoot: stage, actualProfileUTF8: installer.command.argv[2],
                                                 environment: installer.command.environment, installerCommand: installer.command,
                                                 importCommand: importer.command,
                                                 interpreterMachOObservation: finalObservation.interpreterMachOObservation,
                                                 preInstallStagedMachOObservations: installer.observation.stagedMachOObservations,
                                                 stagedMachOObservations: finalObservation.stagedMachOObservations)
        return try InstallationObservation(executionObservation: execution, probe: probe)
    }

    /// Pip may add wheel-owned extensions/dylibs under the authenticated output
    /// root, but it may never delete or alter an object observed before install.
    private static func preservesPreInstallObjects(_ pre: [StagedMachOObservation], in post: [StagedMachOObservation]) -> Bool {
        let byPath = Dictionary(uniqueKeysWithValues: post.map { ($0.path, $0) })
        return pre.allSatisfy { byPath[$0.path] == $0 }
    }

    private static func command(policy: MaterializationPolicy, stage: String, childArgv: [String]) throws -> ActualChildCommand {
        let profile = try policy.reboundSandboxProfile(stage: stage)
        let interpreter = stage + "/runtime/closure/python311/bin/python3.11"
        let command = ActualChildCommand(program: "/usr/bin/sandbox-exec",
                                         argv: ["/usr/bin/sandbox-exec", "-p", profile, interpreter] + childArgv,
                                         cwd: policy.workingDirectory,
                                         environment: try policy.reboundEnvironment(stage: stage),
                                         stdin: "/dev/null", stdoutStderr: "ROOT_OWNED_CAPPED_PIPES",
                                         timeoutSeconds: policy.timeoutSeconds, maxOutputBytes: policy.maxOutputBytes,
                                         sandboxLauncherDescriptorIdentitySHA256: policy.sandboxLauncher.descriptorIdentitySHA256,
                                         sandboxLauncherSHA256: policy.sandboxLauncher.sha256,
                                         sandboxLauncherCodesignIdentitySHA256: policy.sandboxLauncher.codesignIdentity.sha256,
                                         interpreterSHA256: policy.baseInterpreter.sha256,
                                         interpreterMachOSHA256: policy.baseInterpreter.machOSHA256,
                                         interpreterCodesignIdentitySHA256: policy.baseInterpreter.codesignIdentity.sha256,
                                         sandboxProfileSHA256: try ActualSandboxProfile.sha256(profile))
        return command
    }

    private static func stagePrefix(_ interpreter: String) -> String {
        String(interpreter.dropLast("/runtime/closure/python311/bin/python3.11".count))
    }

    private static func parseProbe(_ bytes: [UInt8], policy: MaterializationPolicy, stage: String) throws -> ImportProbeObservation {
        guard bytes.count <= Int(policy.maxOutputBytes), case .object(let values) = try CanonicalJSON.decode(bytes: bytes) else { throw ProcessRunnerFailure.invocationMismatch }
        let object = Dictionary(uniqueKeysWithValues: values)
        guard Set(object.keys) == Set(["version", "paths", "files"]), case let .string(version)? = object["version"],
              version == policy.pythonVersion, case let .array(paths)? = object["paths"], case let .array(files)? = object["files"] else { throw ProcessRunnerFailure.invocationMismatch }
        let pathStrings: [String] = try paths.map { value in guard case let .string(text) = value, !text.contains("\0") else { throw ProcessRunnerFailure.invocationMismatch }; return text }
        let fileStrings: [String] = try files.map { value in guard case let .string(text) = value, !text.contains("\0") else { throw ProcessRunnerFailure.invocationMismatch }; return text }
        let runtime = stage + "/runtime/closure/python311"
        guard !pathStrings.isEmpty, fileStrings.count == 4,
              pathStrings.allSatisfy({ $0 == runtime || $0.hasPrefix(runtime + "/") }),
              fileStrings.allSatisfy({ $0.hasPrefix(runtime + "/") }) else { throw ProcessRunnerFailure.invocationMismatch }
        return try ImportProbeObservation(version: version, sysPath: pathStrings, moduleFiles: fileStrings,
                                          outputSHA256: SHA256Digest(CanonicalJSON.sha256(bytes)))
    }

    private static func spawn(_ authorization: VerifiedLaunchCapability.SpawnAuthorization, syscalls: SpawnSyscalls) throws -> ProcessResult {
        var null: Int32 = -1, readFD: Int32 = -1, writeFD: Int32 = -1
        defer { [null, readFD, writeFD].filter { $0 >= 0 }.forEach(syscalls.close) }
        null = try syscalls.openNull()
        (readFD, writeFD) = try syscalls.makePipe()
        try syscalls.setCloseOnExec(readFD); try syscalls.setCloseOnExec(writeFD)
        // This final rebind/comparison is intentionally adjacent to the sole
        // spawn boundary.  `cwd` remains the retained descriptor, never a
        // pathname reopened from the observed command.
        let authorized = try authorization.consumeImmediatelyBeforeSpawn()
        let command = authorized.command
        let actions: [SpawnFileAction] = [
            .duplicate(source: null, destination: STDIN_FILENO),
            .duplicate(source: writeFD, destination: STDOUT_FILENO),
            .duplicate(source: writeFD, destination: STDERR_FILENO),
            .changeDirectory(fd: authorized.cwd.fd), .close(fd: readFD), .close(fd: writeFD), .close(fd: null), .close(fd: authorized.cwd.fd)
        ]
        let request = SpawnRequest(command: command, cwd: authorized.cwdPath, fileActions: actions,
                                   attributes: SpawnAttributes(flags: Int32(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT), processGroup: 0))
        let pid = try syscalls.spawn(request)
        syscalls.close(writeFD); writeFD = -1
        var output: [UInt8] = [], leaderStatus: Int32?, outputEOF = false, failure: ProcessRunnerFailure?
        let deadline = syscalls.nowNanoseconds() + UInt64(command.timeoutSeconds) * 1_000_000_000
        while leaderStatus == nil || !outputEOF {
            while !outputEOF {
                switch syscalls.read(readFD, 64 * 1024) {
                case let .bytes(bytes):
                    guard output.count + bytes.count <= command.maxOutputBytes else { failure = .outputLimit; break }
                    output += bytes
                case .endOfFile: outputEOF = true
                case .wouldBlock: break
                case .interrupted: continue
                case .failure: failure = .readFailed
                }
                break
            }
            if failure != nil || syscalls.nowNanoseconds() >= deadline { failure = failure ?? .timeout; break }
            guard leaderStatus == nil else { syscalls.sleepNanoseconds(10_000_000); continue }
            switch syscalls.observeLeader(pid) {
            case .running: syscalls.sleepNanoseconds(10_000_000)
            case let .exited(childStatus): leaderStatus = childStatus
            case .interrupted: continue
            case .failure: failure = .launchFailed
            }
        }
        if let failure {
            _ = try terminateGroupThenReap(leader: pid, syscalls: syscalls)
            throw failure
        }
        guard let leaderStatus else { _ = try terminateGroupThenReap(leader: pid, syscalls: syscalls); throw ProcessRunnerFailure.launchFailed }
        guard leaderStatus == 0 else { _ = try terminateGroupThenReap(leader: pid, syscalls: syscalls); throw ProcessRunnerFailure.nonzeroExit }
        guard try terminateGroupThenReap(leader: pid, expectedStatus: leaderStatus, syscalls: syscalls) == .alreadyAbsent else {
            throw ProcessRunnerFailure.launchFailed
        }
        return ProcessResult(status: leaderStatus, output: output)
    }

    /// The leader is deliberately unreaped until the group disappears.  Its
    /// PID therefore remains reserved, so the negative-PGID signals below can
    /// never target a recycled process group.
    private enum CleanupDisposition: Equatable { case alreadyAbsent, terminated }
    private enum GroupProbePhase { case preTerminate, termGrace, postKill }
    private enum GroupProbeState { case absent, exists, hardFailure, indeterminate }
    private static let lifecycleAttemptLimit = 64
    private static let lifecyclePollNanoseconds: UInt64 = 10_000_000

    /// The unreaped leader reserves its PGID through the entire finite cleanup
    /// state machine.  Phase deadlines all derive from one monotonic start, so
    /// retries cannot nest into an unbounded series of fresh grace periods.
    private static func terminateGroupThenReap(leader: pid_t, expectedStatus: Int32? = nil,
                                               syscalls: SpawnSyscalls) throws -> CleanupDisposition {
        let started = syscalls.nowNanoseconds()
        let termDeadline = deadline(started, plus: 1_000_000_000)
        let postKillDeadline = deadline(started, plus: 2_000_000_000)
        let reapDeadline = deadline(started, plus: 3_000_000_000)

        switch probeGroup(group: leader, phase: .preTerminate, deadline: termDeadline, syscalls: syscalls) {
        case .absent:
            try reapLeader(leader, expectedStatus: expectedStatus, deadline: reapDeadline, syscalls: syscalls)
            return .alreadyAbsent
        case .exists, .hardFailure, .indeterminate:
            // A failed or exhausted pre-TERM probe proves neither absence nor
            // safety to release the leader.  Preserve the PID and clean up.
            syscalls.signalGroup(leader, SIGTERM)
        }

        switch probeGroup(group: leader, phase: .termGrace, deadline: termDeadline, syscalls: syscalls) {
        case .absent:
            try reapLeader(leader, expectedStatus: expectedStatus, deadline: reapDeadline, syscalls: syscalls)
            return .terminated
        case .exists, .hardFailure, .indeterminate:
            // TERM grace is conservative: uncertain observation escalates
            // while the retained leader still makes the negative PGID safe.
            syscalls.signalGroup(leader, SIGKILL)
        }

        switch probeGroup(group: leader, phase: .postKill, deadline: postKillDeadline, syscalls: syscalls) {
        case .absent:
            try reapLeader(leader, expectedStatus: expectedStatus, deadline: reapDeadline, syscalls: syscalls)
            return .terminated
        case .exists, .hardFailure, .indeterminate:
            // KILL is the final signal.  Reap exactly once before exposing
            // uncertainty; no later operation can signal a recycled PGID.
            try reapLeader(leader, expectedStatus: expectedStatus, deadline: reapDeadline, syscalls: syscalls)
            throw ProcessRunnerFailure.processCleanupIndeterminate
        }
    }

    /// Every process-group observation uses this one finite state machine.
    /// A phase has both an injected-monotonic deadline and an attempt cap;
    /// `EINTR` therefore remains retryable without becoming an infinite loop.
    private static func probeGroup(group: pid_t, phase: GroupProbePhase, deadline: UInt64,
                                   syscalls: SpawnSyscalls) -> GroupProbeState {
        var observedLiveGroup = false
        for attempt in 0..<lifecycleAttemptLimit {
            if syscalls.nowNanoseconds() >= deadline {
                return observedLiveGroup ? .exists : .indeterminate
            }
            switch syscalls.probeGroup(group) {
            case .absent:
                return .absent
            case .failure:
                return .hardFailure
            case .interrupted:
                break
            case .exists:
                observedLiveGroup = true
                if phase == .preTerminate { return .exists }
            }
            if attempt == lifecycleAttemptLimit - 1 { return observedLiveGroup ? .exists : .indeterminate }
            syscalls.sleepNanoseconds(lifecyclePollNanoseconds)
        }
        return observedLiveGroup ? .exists : .indeterminate
    }

    private static func deadline(_ started: UInt64, plus duration: UInt64) -> UInt64 {
        started > UInt64.max - duration ? UInt64.max : started + duration
    }

    /// The final `waitpid(WNOHANG)` is bounded like group observation.  The
    /// observed-but-unreaped leader retains PID/PGID identity while live;
    /// ECHILD after its WNOWAIT observation is indeterminate, never reaping.
    private static func reapLeader(_ pid: pid_t, expectedStatus: Int32?, deadline: UInt64,
                                   syscalls: SpawnSyscalls) throws {
        for attempt in 0..<lifecycleAttemptLimit {
            if syscalls.nowNanoseconds() >= deadline { throw ProcessRunnerFailure.processReapIndeterminate }
            switch syscalls.reapLeader(pid) {
            case let .reaped(status):
                guard expectedStatus.map({ $0 == status }) ?? true else { throw ProcessRunnerFailure.launchFailed }
                return
            case .stillRunning, .interrupted:
                if attempt == lifecycleAttemptLimit - 1 { throw ProcessRunnerFailure.processReapIndeterminate }
                syscalls.sleepNanoseconds(lifecyclePollNanoseconds)
            case .noChild, .failure:
                throw ProcessRunnerFailure.processReapIndeterminate
            }
        }
        throw ProcessRunnerFailure.processReapIndeterminate
    }
}
