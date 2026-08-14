import CryptoKit
import Darwin
import Foundation
import zlib

enum ArchiveFailure: Error, Equatable { case unsafeArchive, invalidStage, sourceChanged, destinationExists, missingEntrypoint, checksum, magic }

/// Typed points at which a test can replace an already-validated source.  The copier itself has
/// no callback: every seam is detected by the real descriptor rebind that follows the copy.
enum CopySeam: String, CaseIterable { case manifest, lock, policy, checkoutDirectory, checkoutRegular, archive, wheel0 }

struct DestinationOwner: Hashable { let uid: uid_t; let gid: gid_t }

/// Opaque descriptor capability for the one durable materialization attempt.  Production creates
/// this only from `DarwinDurableStateStore`; the disposable constructor is internal for tests.
final class HeldMaterializationStage {
    static let productionDestinationOwner = DestinationOwner(uid: 0, gid: 0)
    let root: HeldDescriptor
    /// The only mutable, disposable subtree.  It is created with the attempt and is never
    /// caller-selected; materialization removes this exact held identity before PRESEALED.
    let scratch: HeldDescriptor
    let input: HeldDescriptor
    let runtime: HeldDescriptor
    let volume: HeldDescriptor
    let work: HeldDescriptor
    let stageName: String
    let stagePath: String
    let home: HeldDescriptor
    let temporary: HeldDescriptor
    let wheelhouse: HeldDescriptor
    let destinationOwner: DestinationOwner
    private init(root: HeldDescriptor, scratch: HeldDescriptor, input: HeldDescriptor, runtime: HeldDescriptor, volume: HeldDescriptor, work: HeldDescriptor,
                 stageName: String, stagePath: String, home: HeldDescriptor, temporary: HeldDescriptor, wheelhouse: HeldDescriptor,
                 destinationOwner: DestinationOwner) {
        self.root = root; self.scratch = scratch; self.input = input; self.runtime = runtime; self.volume = volume; self.work = work; self.stageName = stageName; self.stagePath = stagePath; self.home = home; self.temporary = temporary; self.wheelhouse = wheelhouse; self.destinationOwner = destinationOwner
    }
    #if DEBUG
    static func makeDisposableForTest(rootURL: URL) throws -> HeldMaterializationStage {
        let root = try POSIX.openDirectory(rootURL.path)
        let scratch: HeldDescriptor
        do { scratch = try POSIX.openAt(root, name: "scratch", flags: O_RDONLY | O_DIRECTORY) }
        catch POSIXFailure.system(let code) where code == ENOENT { scratch = try POSIX.mkdirAt(root, name: "scratch", mode: 0o700) }
        let input: HeldDescriptor
        do { input = try POSIX.openAt(scratch, name: "input", flags: O_RDONLY | O_DIRECTORY) }
        catch POSIXFailure.system(let code) where code == ENOENT { input = try POSIX.mkdirAt(scratch, name: "input", mode: 0o700) }
        let runtime: HeldDescriptor
        do { runtime = try POSIX.openAt(root, name: "runtime", flags: O_RDONLY | O_DIRECTORY) }
        catch POSIXFailure.system(let code) where code == ENOENT { runtime = try POSIX.mkdirAt(root, name: "runtime", mode: 0o700) }
        try privateDirectory(root); try privateDirectory(scratch); try privateDirectory(input); try privateDirectory(runtime)
        let metadata = try POSIX.metadata(root)
        let home = try POSIX.mkdirAt(root, name: "home", mode: 0o700), temporary = try POSIX.mkdirAt(root, name: "tmp", mode: 0o700), wheelhouse = try POSIX.mkdirAt(root, name: "wheelhouse", mode: 0o700)
        return HeldMaterializationStage(root: root, scratch: scratch, input: input, runtime: runtime, volume: root, work: root,
                                        stageName: "", stagePath: try POSIX.descriptorPath(root), home: home, temporary: temporary, wheelhouse: wheelhouse,
                                        destinationOwner: DestinationOwner(uid: metadata.uid, gid: metadata.gid))
    }

    /// Fixture-only equivalent of production stage allocation after the fixture has moved its
    /// empty stage beneath the real disposable `materialization-work/<attempt>` registry.  This
    /// keeps all observed child paths stable from copy/probe through receipt construction.
    static func makeMaterializationForTest(root: HeldDescriptor, work: HeldDescriptor,
                                           stageName: String, volume: HeldDescriptor) throws -> HeldMaterializationStage {
        guard AdmissionController.isAttemptUUID(stageName) else { throw ArchiveFailure.invalidStage }
        let rebound = try root.rebindSameIdentity(in: work, name: stageName, directory: true)
        let scratch = try POSIX.openAt(rebound, name: "scratch", flags: O_RDONLY | O_DIRECTORY)
        let input = try POSIX.openAt(scratch, name: "input", flags: O_RDONLY | O_DIRECTORY)
        let runtime = try POSIX.openAt(rebound, name: "runtime", flags: O_RDONLY | O_DIRECTORY)
        let home = try POSIX.openAt(rebound, name: "home", flags: O_RDONLY | O_DIRECTORY)
        let temporary = try POSIX.openAt(rebound, name: "tmp", flags: O_RDONLY | O_DIRECTORY)
        let wheelhouse = try POSIX.openAt(rebound, name: "wheelhouse", flags: O_RDONLY | O_DIRECTORY)
        let owner = try POSIX.metadata(rebound)
        return HeldMaterializationStage(root: rebound, scratch: scratch, input: input, runtime: runtime, volume: volume, work: work,
                                        stageName: stageName, stagePath: try POSIX.descriptorPath(rebound),
                                        home: home, temporary: temporary, wheelhouse: wheelhouse,
                                        destinationOwner: DestinationOwner(uid: owner.uid, gid: owner.gid))
    }

    static func allocateMaterializationForTest(work: HeldDescriptor, stageName: String,
                                               volume: HeldDescriptor) throws -> HeldMaterializationStage {
        guard AdmissionController.isAttemptUUID(stageName) else { throw ArchiveFailure.invalidStage }
        let root = try POSIX.mkdirAt(work, name: stageName, mode: 0o700)
        let scratch = try POSIX.mkdirAt(root, name: "scratch", mode: 0o700)
        _ = try POSIX.mkdirAt(root, name: "runtime", mode: 0o700)
        _ = try POSIX.mkdirAt(scratch, name: "input", mode: 0o700)
        for name in ["home", "tmp", "wheelhouse"] { _ = try POSIX.mkdirAt(root, name: name, mode: 0o700) }
        return try makeMaterializationForTest(root: root, work: work, stageName: stageName, volume: volume)
    }

    /// Publication fixtures retain the same descriptor shape as production after explicitly
    /// moving the fixture stage under the disposable work registry. No pathname is retained.
    static func makePublicationForTest(root: HeldDescriptor, work: HeldDescriptor,
                                       stageName: String, volume: HeldDescriptor) throws -> HeldMaterializationStage {
        guard AdmissionController.isAttemptUUID(stageName) else { throw ArchiveFailure.invalidStage }
        let rebound = try root.rebindSameIdentity(in: work, name: stageName, directory: true)
        let runtime = try POSIX.openAt(rebound, name: "runtime", flags: O_RDONLY | O_DIRECTORY)
        let owner = try POSIX.metadata(rebound)
        return HeldMaterializationStage(root: rebound, scratch: runtime, input: runtime, runtime: runtime, volume: volume, work: work,
                                        stageName: stageName, stagePath: try POSIX.descriptorPath(rebound),
                                        home: runtime, temporary: runtime, wheelhouse: runtime,
                                        destinationOwner: DestinationOwner(uid: owner.uid, gid: owner.gid))
    }
    #endif
    static func makeProduction(root: HeldDescriptor, volume: HeldDescriptor, work: HeldDescriptor, stageName: String) throws -> HeldMaterializationStage {
        let rebound = try root.rebind(in: work, name: stageName, directory: true)
        let stagePath = try POSIX.descriptorPath(rebound)
        let scratch = try POSIX.mkdirAt(root, name: "scratch", mode: 0o700)
        let input = try POSIX.mkdirAt(scratch, name: "input", mode: 0o700)
        let runtime = try POSIX.mkdirAt(root, name: "runtime", mode: 0o700)
        let home = try POSIX.mkdirAt(root, name: "home", mode: 0o700), temporary = try POSIX.mkdirAt(root, name: "tmp", mode: 0o700), wheelhouse = try POSIX.mkdirAt(root, name: "wheelhouse", mode: 0o700)
        try POSIX.requireDirectory(root, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireDirectory(scratch, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireDirectory(input, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireDirectory(runtime, uid: 0, gid: 0, mode: 0o700)
        try POSIX.requireDirectory(home, uid: 0, gid: 0, mode: 0o700); try POSIX.requireDirectory(temporary, uid: 0, gid: 0, mode: 0o700); try POSIX.requireDirectory(wheelhouse, uid: 0, gid: 0, mode: 0o700)
        return HeldMaterializationStage(root: root, scratch: scratch, input: input, runtime: runtime, volume: volume, work: work,
                                        stageName: stageName, stagePath: stagePath, home: home, temporary: temporary, wheelhouse: wheelhouse,
                                        destinationOwner: productionDestinationOwner)
    }

    func bindFinalLaunchLayout(_ interpreter: HeldInterpreterCapability) throws -> HeldFinalLaunchLayout {
        guard interpreter.relativePath == "runtime/closure/python311" else { throw ArchiveFailure.invalidStage }
        let closure = try POSIX.openAt(runtime, name: "closure", flags: O_RDONLY | O_DIRECTORY)
        let python = try interpreter.descriptor.rebind(in: closure, name: "python311", directory: true)
        let bin = try POSIX.openAt(python, name: "bin", flags: O_RDONLY | O_DIRECTORY)
        let executable = try POSIX.openAt(bin, name: "python3.11", flags: O_RDONLY)
        let metadata = try POSIX.metadata(executable)
        guard metadata.type == S_IFREG, metadata.nlink == 1, (metadata.mode & 0o022) == 0 else { throw ArchiveFailure.unsafeArchive }
        try POSIX.requireNoExtendedACL(executable)
        return HeldFinalLaunchLayout(stage: self, interpreterDirectory: python, executable: executable)
    }
}

/// Opaque final-layout authority.  It derives every executable operand from retained
/// descriptor identities and never accepts a caller-supplied path.
final class HeldFinalLaunchLayout {
    private let stage: HeldMaterializationStage; private let interpreterDirectory: HeldDescriptor; private let executable: HeldDescriptor
    fileprivate init(stage: HeldMaterializationStage, interpreterDirectory: HeldDescriptor, executable: HeldDescriptor) { self.stage = stage; self.interpreterDirectory = interpreterDirectory; self.executable = executable }
    func rebindDescriptors() throws -> ReboundFinalLaunchLayout {
        #if DEBUG
        let root: HeldDescriptor
        if stage.stageName.isEmpty { root = stage.root }
        else { root = try stage.root.rebind(in: stage.work, name: stage.stageName, directory: true) }
        #else
        guard !stage.stageName.isEmpty else { throw ArchiveFailure.invalidStage }
        let root = try stage.root.rebind(in: stage.work, name: stage.stageName, directory: true)
        #endif
        guard try POSIX.descriptorPath(root) == stage.stagePath else { throw ArchiveFailure.sourceChanged }
        let runtime = try stage.runtime.rebind(in: root, name: "runtime", directory: true)
        let closure = try POSIX.openAt(runtime, name: "closure", flags: O_RDONLY | O_DIRECTORY)
        _ = try closure.rebind(in: runtime, name: "closure", directory: true)
        let python = try interpreterDirectory.rebind(in: closure, name: "python311", directory: true)
        let bin = try POSIX.openAt(python, name: "bin", flags: O_RDONLY | O_DIRECTORY)
        _ = try bin.rebind(in: python, name: "bin", directory: true)
        let interpreter = try executable.rebind(in: bin, name: "python3.11")
        let home = try stage.home.rebind(in: root, name: "home", directory: true)
        let temporary = try stage.temporary.rebind(in: root, name: "tmp", directory: true)
        let wheelhouse = try stage.wheelhouse.rebind(in: root, name: "wheelhouse", directory: true)
        return ReboundFinalLaunchLayout(stage: stage.stagePath, work: stage.work, root: root, runtime: runtime, closure: closure,
                                        python: python, bin: bin, interpreter: interpreter, home: home,
                                        temporary: temporary, wheelhouse: wheelhouse)
    }
    func rebind() throws -> (stage: String, runtime: String, interpreter: String, home: String, temporary: String, wheelhouse: String) {
        let bound = try rebindDescriptors()
        return (bound.stage, bound.stage + "/runtime", bound.stage + "/runtime/closure/python311/bin/python3.11", bound.stage + "/home", bound.stage + "/tmp", bound.stage + "/wheelhouse")
    }
}

/// Internal observer input.  Every pathname here is derived from the single
/// retained stage descriptor after rebind; callers cannot provide one.
struct ReboundFinalLaunchLayout {
    let stage: String
    let work: HeldDescriptor
    let root: HeldDescriptor
    let runtime: HeldDescriptor
    let closure: HeldDescriptor
    let python: HeldDescriptor
    let bin: HeldDescriptor
    let interpreter: HeldDescriptor
    let home: HeldDescriptor
    let temporary: HeldDescriptor
    let wheelhouse: HeldDescriptor
}

enum ArchiveEvent: Equatable { case rebindSource, fsyncRuntimeParent, rebindClosure, rename, fsyncUnpack, fsyncRuntime, rebindDestination }

struct ArchiveOperations {
    let renameExclusive: (HeldDescriptor, String, HeldDescriptor, String) throws -> Void
    let durableDirectory: (HeldDescriptor) throws -> Void
    let rebindDirectory: (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor
    static let production = ArchiveOperations(renameExclusive: { try POSIX.renameExclusive(from: $0, name: $1, to: $2, destination: $3) },
                                              durableDirectory: { try POSIX.durableDirectory($0) },
                                              rebindDirectory: { try $0.rebind(in: $1, name: $2, directory: true) })
}

/// The private copy is a capability, not an input pathname.  `archive` remains open across
/// extraction so a later pathname replacement cannot change the bytes examined by zlib.
final class CopiedBundle {
    let input: HeldDescriptor
    let archive: HeldDescriptor
    let archiveName: String
    private let retained: [String: HeldDescriptor]
    init(input: HeldDescriptor, archive: HeldDescriptor, archiveName: String, retained: [String: HeldDescriptor] = [:]) {
        self.input = input; self.archive = archive; self.archiveName = archiveName; self.retained = retained
    }
}

struct CopyOperations {
    let configureDestination: (HeldDescriptor, DestinationOwner, Bool) throws -> Void
    static let production = CopyOperations(configureDestination: { descriptor, owner, directory in
        try setPrivateOwnershipAndMode(descriptor, owner: owner, directory: directory)
    })
}

/// Descriptor-bound final interpreter returned by the shared in-process extractor. Candidate
/// observation and materialization consume the same capability; neither receives an unpack path
/// or a wheelhouse.
struct HeldInterpreterCapability {
    let descriptor: HeldDescriptor
    let relativePath: String
}

/// Candidate observation consumes this shared descriptor-copy/extract/rename path. It has no
/// wheelhouse or process callback and therefore cannot accidentally execute an archive member.
protocol InProcessArchiveExtractor {
    func copyAndExtract(_ bundle: ValidatedBundle, into stage: HeldMaterializationStage) throws -> HeldInterpreterCapability
}

struct SharedInProcessArchiveExtractor: InProcessArchiveExtractor {
    func copyAndExtract(_ bundle: ValidatedBundle, into stage: HeldMaterializationStage) throws -> HeldInterpreterCapability {
        let copied = try BundleCopier.copy(bundle, into: stage)
        return try ArchiveInstaller.extractAndPlace(stage: stage, copied: copied)
    }
}

enum BundleCopier {
    static func copy(_ bundle: ValidatedBundle, into stage: HeldMaterializationStage,
                     operations: CopyOperations = .production) throws -> CopiedBundle {
        let input = stage.input
        try privateDirectory(input)
        guard try POSIX.directoryEntryNames(input).isEmpty else { throw ArchiveFailure.destinationExists }

        var retained: [String: HeldDescriptor] = [:]
        let rootChildren = HeldBundle.childNames.sorted()
        try copyDirectory(source: bundle.held.descriptor, sourcePath: "", target: input,
                          children: rootChildren, bundle: bundle, stage: stage, operations: operations, retained: &retained)
        let archivePath = "artifacts/\(bundle.manifest.pythonArchiveFilename)"
        guard let archive = retained[archivePath] else { throw ArchiveFailure.unsafeArchive }
        return CopiedBundle(input: input, archive: archive, archiveName: bundle.manifest.pythonArchiveFilename, retained: retained)
    }

    private static func copyDirectory(source: HeldDescriptor, sourcePath: String, target: HeldDescriptor,
                                      children: [String], bundle: ValidatedBundle,
                                      stage: HeldMaterializationStage, operations: CopyOperations,
                                      retained: inout [String: HeldDescriptor]) throws {
        for name in children.sorted() {
            let path = sourcePath.isEmpty ? name : sourcePath + "/" + name
            guard let capability = try bundle.held.sourceCapability(at: path) else { throw ArchiveFailure.unsafeArchive }
            let held = capability.descriptor
            let isDirectory = try type(of: held) == S_IFDIR
            let before = try held.rebind(in: source, name: name, directory: isDirectory)
            guard try POSIX.metadata(before) == capability.metadata else { throw ArchiveFailure.sourceChanged }
            if isDirectory {
                let made = try POSIX.mkdirAt(target, name: name, mode: 0o700)
                try operations.configureDestination(made, stage.destinationOwner, true)
                let descendants = bundle.held.immediateChildren(of: path)
                try copyDirectory(source: held, sourcePath: path, target: made, children: descendants,
                                  bundle: bundle, stage: stage, operations: operations, retained: &retained)
                try POSIX.durableDirectory(made)
                let rebound = try held.rebind(in: source, name: name, directory: true)
                guard try POSIX.metadata(rebound) == capability.metadata else { throw ArchiveFailure.sourceChanged }
                let targetRebound = try made.rebind(in: target, name: name, directory: true)
                let targetMetadata = try POSIX.metadata(targetRebound)
                guard targetMetadata.type == capability.metadata.type,
                      targetMetadata.uid == stage.destinationOwner.uid,
                      targetMetadata.gid == stage.destinationOwner.gid,
                      targetMetadata.mode == 0o700,
                      targetMetadata.nlink == capability.metadata.nlink,
                      targetMetadata.size == capability.metadata.size else { throw ArchiveFailure.sourceChanged }
                retained[path] = targetRebound
            } else {
                let made = try POSIX.createExclusive(target, name: name, mode: 0o600)
                try operations.configureDestination(made, stage.destinationOwner, false)
                let copiedHash = try copyContents(from: held, to: made)
                try POSIX.durableRegular(made)
                let rebound = try held.rebind(in: source, name: name)
                guard let expected = capability.digest,
                      copiedHash == expected, try digest(rebound) == expected,
                      try POSIX.metadata(rebound) == capability.metadata else { throw ArchiveFailure.sourceChanged }
                let targetRebound = try made.rebind(in: target, name: name)
                let targetMetadata = try POSIX.metadata(targetRebound)
                guard targetRebound.device == made.device, targetRebound.inode == made.inode,
                      try digest(targetRebound) == copiedHash,
                      targetMetadata.type == capability.metadata.type,
                      targetMetadata.uid == stage.destinationOwner.uid,
                      targetMetadata.gid == stage.destinationOwner.gid,
                      targetMetadata.mode == 0o600,
                      targetMetadata.nlink == 1,
                      targetMetadata.size == capability.metadata.size else { throw ArchiveFailure.sourceChanged }
                retained[path] = targetRebound
            }
        }
        try POSIX.durableDirectory(target)
    }
}

enum ArchiveInstaller {
    static func extractAndPlace(stage: HeldMaterializationStage, copied: CopiedBundle,
                                operations: ArchiveOperations = .production,
                                event: (ArchiveEvent) -> Void = { _ in }) throws -> HeldInterpreterCapability {
        let unpack = try POSIX.mkdirAt(stage.scratch, name: "unpack", mode: 0o700)
        try privateDirectory(unpack)
        let spool = try Gzip.inflate(copied.archive, into: stage)
        let members = try Tar.validate(spool)
        try Tar.extract(spool, members: members, into: unpack)
        let python = try POSIX.openAt(unpack, name: "python", flags: O_RDONLY | O_DIRECTORY)
        let entryBin = try POSIX.openAt(python, name: "bin", flags: O_RDONLY | O_DIRECTORY)
        let entry = try POSIX.openAt(entryBin, name: "python3.11", flags: O_RDONLY)
        try executableRegular(entry)
        let openedClosure: HeldDescriptor
        do { openedClosure = try POSIX.openAt(stage.runtime, name: "closure", flags: O_RDONLY | O_DIRECTORY) }
        catch POSIXFailure.system(let code) where code == ENOENT { openedClosure = try POSIX.mkdirAt(stage.runtime, name: "closure", mode: 0o700) }
        try privateDirectory(openedClosure)
        try operations.durableDirectory(stage.runtime); event(.fsyncRuntimeParent)
        let closure = try operations.rebindDirectory(openedClosure, stage.runtime, "closure"); event(.rebindClosure)
        let source = try operations.rebindDirectory(python, unpack, "python")
        event(.rebindSource)
        let sourceDigest = try treeDigest(source)
        do { _ = try POSIX.openAt(closure, name: "python311", flags: O_RDONLY | O_DIRECTORY); throw ArchiveFailure.destinationExists }
        catch POSIXFailure.system(let code) where code == ENOENT { }
        try operations.renameExclusive(unpack, "python", closure, "python311")
        event(.rename)
        try operations.durableDirectory(unpack); event(.fsyncUnpack)
        try operations.durableDirectory(closure); event(.fsyncRuntime)
        try POSIX.durableVolumeBarrier(stage.volume)
        let destination = try operations.rebindDirectory(source, closure, "python311")
        guard try treeDigest(destination) == sourceDigest else { throw ArchiveFailure.sourceChanged }
        event(.rebindDestination)
        return HeldInterpreterCapability(descriptor: destination, relativePath: "runtime/closure/python311")
    }
}

enum Gzip {
    static let ioChunkSize = 64 * 1024
    static let maximumOutputBytes: UInt64 = 8_589_934_592
    static func inflate(_ source: HeldDescriptor, into stage: HeldMaterializationStage,
                        maximumOutputBytes: UInt64 = Gzip.maximumOutputBytes) throws -> HeldDescriptor {
        let spool = try POSIX.createExclusive(stage.scratch, name: "archive.tar", mode: 0o600)
        guard lseek(source.fd, 0, SEEK_SET) >= 0 else { throw ArchiveFailure.unsafeArchive }
        var stream = z_stream()
        guard inflateInit2_(&stream, 15 + 16, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size)) == Z_OK else { throw ArchiveFailure.unsafeArchive }
        defer { inflateEnd(&stream) }
        var total: UInt64 = 0, ended = false
        var input = [UInt8](repeating: 0, count: ioChunkSize)
        while !ended {
            let count = Darwin.read(source.fd, &input, input.count)
            guard count >= 0 else { throw ArchiveFailure.unsafeArchive }
            if count == 0 {
                stream.next_in = nil; stream.avail_in = 0
                var output = [UInt8](repeating: 0, count: ioChunkSize); let capacity = output.count
                let step = output.withUnsafeMutableBufferPointer { buffer -> (Int32, Int) in
                    stream.next_out = buffer.baseAddress; stream.avail_out = uInt(capacity)
                    return (zlib.inflate(&stream, Z_NO_FLUSH), capacity - Int(stream.avail_out))
                }
                guard total <= maximumOutputBytes, UInt64(step.1) <= maximumOutputBytes - total else { throw ArchiveFailure.unsafeArchive }
                if step.1 > 0 { try POSIX.writeAll(spool, Array(output.prefix(step.1))); total += UInt64(step.1) }
                guard step.0 == Z_STREAM_END else { throw ArchiveFailure.unsafeArchive }
                ended = true
                continue
            }
            try input.withUnsafeMutableBufferPointer { inBuffer in
                stream.next_in = inBuffer.baseAddress; stream.avail_in = uInt(count)
                while stream.avail_in > 0 && !ended {
                    var output = [UInt8](repeating: 0, count: ioChunkSize); let capacity = output.count
                    let step = output.withUnsafeMutableBufferPointer { buffer -> (Int32, Int) in
                        stream.next_out = buffer.baseAddress; stream.avail_out = uInt(capacity)
                        return (zlib.inflate(&stream, Z_NO_FLUSH), capacity - Int(stream.avail_out))
                    }
                    guard total <= maximumOutputBytes, UInt64(step.1) <= maximumOutputBytes - total else { throw ArchiveFailure.unsafeArchive }
                    if step.1 > 0 { try POSIX.writeAll(spool, Array(output.prefix(step.1))); total += UInt64(step.1) }
                    if step.0 == Z_STREAM_END { guard stream.avail_in == 0 else { throw ArchiveFailure.unsafeArchive }; ended = true; break }
                    guard step.0 == Z_OK else { throw ArchiveFailure.unsafeArchive }
                }
            }
        }
        var extra: UInt8 = 0; guard Darwin.read(source.fd, &extra, 1) == 0 else { throw ArchiveFailure.unsafeArchive }
        try POSIX.durableRegular(spool)
        return try spool.rebind(in: stage.scratch, name: "archive.tar")
    }
    #if DEBUG
    // Test-only byte-array oracle; the production materializer calls `inflate(_:into:)`.
    // zlib's gzip-only window validates the RFC 1952 header/trailer CRC and ISIZE.  A stream end
    // with any remaining input, or any following byte, is a concatenated/trailing ambiguity.
    static func decompress(_ descriptor: HeldDescriptor) throws -> [UInt8] {
        guard lseek(descriptor.fd, 0, SEEK_SET) >= 0 else { throw ArchiveFailure.unsafeArchive }
        var stream = z_stream()
        guard inflateInit2_(&stream, 15 + 16, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size)) == Z_OK else { throw ArchiveFailure.unsafeArchive }
        defer { inflateEnd(&stream) }
        var result: [UInt8] = []
        let chunk = 64 * 1024
        var ended = false
        var input = [UInt8](repeating: 0, count: chunk)
        while !ended {
            let count = Darwin.read(descriptor.fd, &input, input.count)
            guard count > 0 else { throw ArchiveFailure.unsafeArchive }
            try input.withUnsafeMutableBufferPointer { inBuffer in
                stream.next_in = inBuffer.baseAddress
                stream.avail_in = uInt(count)
                while stream.avail_in > 0 {
                    var output = [UInt8](repeating: 0, count: chunk)
                    let capacity = output.count
                    let resultStep = output.withUnsafeMutableBufferPointer { outBuffer -> (Int32, Int) in
                        stream.next_out = outBuffer.baseAddress
                        stream.avail_out = uInt(capacity)
                        let code = zlib.inflate(&stream, Z_NO_FLUSH)
                        let produced = capacity - Int(stream.avail_out)
                        return (code, produced)
                    }
                    let (status, produced) = resultStep
                    guard result.count <= 8_589_934_592 - produced else { throw ArchiveFailure.unsafeArchive }
                    result += output.prefix(produced)
                    if status == Z_STREAM_END {
                        guard stream.avail_in == 0 else { throw ArchiveFailure.unsafeArchive }
                        ended = true; break
                    }
                    guard status == Z_OK else { throw ArchiveFailure.unsafeArchive }
                }
            }
        }
        var trailing: UInt8 = 0
        guard Darwin.read(descriptor.fd, &trailing, 1) == 0 else { throw ArchiveFailure.unsafeArchive }
        return result
    }
    #endif
}

enum Tar {
    struct Member { let path: String; let directory: Bool; let executable: Bool; let size: UInt64; let payloadOffset: off_t }
    static func validate(_ archive: HeldDescriptor) throws -> [Member] {
        guard lseek(archive.fd, 0, SEEK_SET) >= 0 else { throw ArchiveFailure.unsafeArchive }
        var members: [Member] = [], seen = Set<String>(), folded = Set<String>(), directories: [String: Bool] = [:]; var total: UInt64 = 0
        while true {
            let header = try readExactly(archive, 512)
            if header.allSatisfy({ $0 == 0 }) {
                guard (try readExactly(archive, 512)).allSatisfy({ $0 == 0 }) else { throw ArchiveFailure.unsafeArchive }
                var extra: UInt8 = 0; guard Darwin.read(archive.fd, &extra, 1) == 0 else { throw ArchiveFailure.unsafeArchive }
                break
            }
            guard members.count < 100_000, checksum(header), Array(header[257..<262]) == Array("ustar".utf8) else { throw ArchiveFailure.unsafeArchive }
            let type = header[156]; guard type == 0 || type == 48 || type == 53 else { throw ArchiveFailure.unsafeArchive }
            let path = try path(name: field(header, 0, 100), prefix: field(header, 345, 155), directory: type == 53)
            guard seen.insert(path).inserted, folded.insert(path.lowercased()).inserted else { throw ArchiveFailure.unsafeArchive }
            var ancestor = ""
            for part in path.split(separator: "/").dropLast() {
                ancestor += ancestor.isEmpty ? String(part) : "/" + part
                guard directories[ancestor] != false else { throw ArchiveFailure.unsafeArchive }
            }
            if type != 53 { guard !directories.keys.contains(where: { $0.hasPrefix(path + "/") }) else { throw ArchiveFailure.unsafeArchive } }
            directories[path] = type == 53
            let size = try octal(header, 124, 12); guard size <= 8_589_934_592, total <= 8_589_934_592 - size else { throw ArchiveFailure.unsafeArchive }
            if type == 53, size != 0 { throw ArchiveFailure.unsafeArchive }
            let offset = lseek(archive.fd, 0, SEEK_CUR); guard offset >= 0 else { throw ArchiveFailure.unsafeArchive }
            let padded = (size + 511) & ~UInt64(511); guard padded >= size, lseek(archive.fd, off_t(padded), SEEK_CUR) >= 0 else { throw ArchiveFailure.unsafeArchive }
            members.append(Member(path: path, directory: type == 53, executable: (try octal(header, 100, 8) & 0o111) != 0, size: size, payloadOffset: offset)); total += size
        }
        guard members.contains(where: { !$0.directory && $0.path == "python/bin/python3.11" && $0.executable }) else { throw ArchiveFailure.missingEntrypoint }
        return members
    }
    static func extract(_ archive: HeldDescriptor, members: [Member], into root: HeldDescriptor) throws {
        for member in members {
            if member.directory { try makeDirectory(member.path, root); continue }
            let pieces = member.path.split(separator: "/").map(String.init); guard let name = pieces.last else { throw ArchiveFailure.unsafeArchive }
            var parent = root
            for part in pieces.dropLast() { do { parent = try POSIX.openAt(parent, name: part, flags: O_RDONLY | O_DIRECTORY) } catch POSIXFailure.system(let code) where code == ENOENT { parent = try POSIX.mkdirAt(parent, name: part, mode: 0o700) } }
            let file = try POSIX.createExclusive(parent, name: name, mode: member.executable ? 0o700 : 0o600)
            guard lseek(archive.fd, member.payloadOffset, SEEK_SET) >= 0 else { throw ArchiveFailure.unsafeArchive }
            var remaining = member.size; var buffer = [UInt8](repeating: 0, count: 64 * 1024)
            while remaining > 0 { let want = min(buffer.count, Int(remaining)); let count = Darwin.read(archive.fd, &buffer, want); guard count > 0 else { throw ArchiveFailure.unsafeArchive }; try POSIX.writeAll(file, Array(buffer.prefix(count))); remaining -= UInt64(count) }
            try POSIX.durableRegular(file); _ = try file.rebind(in: parent, name: name)
        }
    }
    #if DEBUG
    /// Test-only parser oracle.  Production always validates and extracts the held spool.
    static func extract(_ bytes: [UInt8], into root: HeldDescriptor) throws {
        var offset = 0, members = 0
        var declared: UInt64 = 0
        var seen = Set<String>(), folded = Set<String>()
        var zeros = 0
        while offset + 512 <= bytes.count {
            let block = Array(bytes[offset..<(offset + 512)]); offset += 512
            if block.allSatisfy({ $0 == 0 }) { zeros += 1; if zeros == 2 { break }; continue }
            guard zeros == 0 else { throw ArchiveFailure.unsafeArchive }
            members += 1; guard members <= 100_000 else { throw ArchiveFailure.unsafeArchive }
            guard checksum(block) else { throw ArchiveFailure.checksum }
            guard Array(block[257..<262]) == Array("ustar".utf8) else { throw ArchiveFailure.magic }
            let type = block[156]
            guard type == 0 || type == 48 || type == 53 else { throw ArchiveFailure.unsafeArchive }
            let name = try path(name: field(block, 0, 100), prefix: field(block, 345, 155), directory: type == 53)
            guard seen.insert(name).inserted, folded.insert(name.lowercased()).inserted else { throw ArchiveFailure.unsafeArchive }
            let size = try octal(block, 124, 12)
            guard size <= 8_589_934_592, declared <= 8_589_934_592 - size else { throw ArchiveFailure.unsafeArchive }
            declared += size
            let padded = Int((size + 511) / 512) * 512
            guard padded >= Int(size), offset + padded <= bytes.count else { throw ArchiveFailure.unsafeArchive }
            if type == 53 { guard size == 0 else { throw ArchiveFailure.unsafeArchive }; try makeDirectory(name, root) }
            else { try writeFile(name, bytes: Array(bytes[offset..<(offset + Int(size))]), root: root, executable: (try octal(block, 100, 8) & 0o111) != 0) }
            offset += padded
        }
        guard zeros == 2, offset == bytes.count else { throw ArchiveFailure.unsafeArchive }
    }
    #endif

    private static func checksum(_ block: [UInt8]) -> Bool {
        guard let declared = try? octal(block, 148, 8) else { return false }
        var copy = block; for index in 148..<156 { copy[index] = 32 }
        return UInt64(copy.reduce(0) { $0 + UInt64($1) }) == declared
    }
    private static func readExactly(_ descriptor: HeldDescriptor, _ count: Int) throws -> [UInt8] {
        var result = [UInt8](repeating: 0, count: count), offset = 0
        while offset < count {
            let read = result.withUnsafeMutableBytes { Darwin.read(descriptor.fd, $0.baseAddress!.advanced(by: offset), count - offset) }
            guard read > 0 else { throw ArchiveFailure.unsafeArchive }; offset += read
        }
        return result
    }
    private static func field(_ bytes: [UInt8], _ start: Int, _ count: Int) -> [UInt8] { Array(bytes[start..<(start + count)].prefix { $0 != 0 }) }
    private static func octal(_ bytes: [UInt8], _ start: Int, _ count: Int) throws -> UInt64 {
        let raw = field(bytes, start, count); guard !raw.isEmpty, raw.allSatisfy({ $0 == 32 || ($0 >= 48 && $0 <= 55) }) else { throw ArchiveFailure.unsafeArchive }
        let digits = raw.drop { $0 == 32 }; guard !digits.isEmpty else { throw ArchiveFailure.unsafeArchive }
        var value: UInt64 = 0
        for byte in digits {
            guard byte >= 48, byte <= 55 else { throw ArchiveFailure.unsafeArchive }
            let digit = UInt64(byte - 48)
            guard value <= (UInt64.max - digit) / 8 else { throw ArchiveFailure.unsafeArchive }
            value = value * 8 + digit
        }
        return value
    }
    private static func path(name: [UInt8], prefix: [UInt8], directory: Bool) throws -> String {
        let raw = prefix + (prefix.isEmpty ? [] : [47]) + name
        guard !raw.isEmpty, raw.allSatisfy({ $0 >= 0x21 && $0 <= 0x7e }), let value = String(bytes: raw, encoding: .ascii) else { throw ArchiveFailure.unsafeArchive }
        let normalized = directory && value.hasSuffix("/") ? String(value.dropLast()) : value
        let components = normalized.split(separator: "/", omittingEmptySubsequences: false)
        guard !normalized.isEmpty, !normalized.hasPrefix("/"), !normalized.contains("\\"),
              !components.contains(where: { $0 == "." || $0 == ".." || $0.isEmpty }) else { throw ArchiveFailure.unsafeArchive }
        return normalized
    }
    private static func makeDirectory(_ path: String, _ root: HeldDescriptor) throws {
        var parent = root
        for part in path.split(separator: "/").map(String.init) {
            do { parent = try POSIX.openAt(parent, name: part, flags: O_RDONLY | O_DIRECTORY) }
            catch POSIXFailure.system(let code) where code == ENOENT { parent = try POSIX.mkdirAt(parent, name: part, mode: 0o700); try privateDirectory(parent) }
        }
    }
    private static func writeFile(_ path: String, bytes: [UInt8], root: HeldDescriptor, executable: Bool) throws {
        let pieces = path.split(separator: "/").map(String.init); guard let name = pieces.last else { throw ArchiveFailure.unsafeArchive }
        var parent = root
        for part in pieces.dropLast() {
            do { parent = try POSIX.openAt(parent, name: part, flags: O_RDONLY | O_DIRECTORY) }
            catch POSIXFailure.system(let code) where code == ENOENT { parent = try POSIX.mkdirAt(parent, name: part, mode: 0o700); try privateDirectory(parent) }
        }
        let file = try POSIX.createExclusive(parent, name: name, mode: executable ? 0o700 : 0o600)
        try POSIX.writeAll(file, bytes); try POSIX.durableRegular(file); try setPrivateMode(file, directory: false, executable: executable); _ = try file.rebind(in: parent, name: name)
    }
}

private func type(of descriptor: HeldDescriptor) throws -> mode_t { var status = stat(); guard fstat(descriptor.fd, &status) == 0 else { throw POSIXFailure.system(errno) }; return status.st_mode & S_IFMT }
private func privateDirectory(_ descriptor: HeldDescriptor) throws { var status = stat(); guard fstat(descriptor.fd, &status) == 0, (status.st_mode & S_IFMT) == S_IFDIR, status.st_nlink >= 1, (status.st_mode & 0o022) == 0 else { throw ArchiveFailure.unsafeArchive }; try POSIX.requireNoExtendedACL(descriptor) }
private func executableRegular(_ descriptor: HeldDescriptor) throws { var status = stat(); guard fstat(descriptor.fd, &status) == 0, (status.st_mode & S_IFMT) == S_IFREG, status.st_nlink == 1, (status.st_mode & 0o111) != 0, (status.st_mode & 0o022) == 0 else { throw ArchiveFailure.missingEntrypoint }; try POSIX.requireNoExtendedACL(descriptor) }
private func setPrivateOwnershipAndMode(_ descriptor: HeldDescriptor, owner: DestinationOwner, directory: Bool, executable: Bool = false) throws {
    guard fchown(descriptor.fd, owner.uid, owner.gid) == 0 else { throw POSIXFailure.system(errno) }
    try setPrivateMode(descriptor, directory: directory, executable: executable)
}
private func setPrivateMode(_ descriptor: HeldDescriptor, directory: Bool, executable: Bool = false) throws {
    let mode: mode_t = directory ? 0o700 : (executable ? 0o700 : 0o600)
    guard fchmod(descriptor.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
}
private func digest(_ descriptor: HeldDescriptor) throws -> String {
    guard lseek(descriptor.fd, 0, SEEK_SET) >= 0 else { throw POSIXFailure.system(errno) }
    var hasher = SHA256(); var buffer = [UInt8](repeating: 0, count: 64 * 1024)
    while true {
        let count = Darwin.read(descriptor.fd, &buffer, buffer.count)
        guard count >= 0 else { throw POSIXFailure.system(errno) }
        if count == 0 { return hasher.finalize().map { String(format: "%02x", $0) }.joined() }
        hasher.update(data: Data(buffer.prefix(count)))
    }
}
private func copyContents(from source: HeldDescriptor, to target: HeldDescriptor) throws -> String {
    guard lseek(source.fd, 0, SEEK_SET) >= 0 else { throw POSIXFailure.system(errno) }
    var hasher = SHA256(); var buffer = [UInt8](repeating: 0, count: 64 * 1024)
    while true {
        let count = Darwin.read(source.fd, &buffer, buffer.count)
        guard count >= 0 else { throw POSIXFailure.system(errno) }
        if count == 0 { return hasher.finalize().map { String(format: "%02x", $0) }.joined() }
        let chunk = Array(buffer.prefix(count)); try POSIX.writeAll(target, chunk); hasher.update(data: Data(chunk))
    }
}
private func treeDigest(_ root: HeldDescriptor) throws -> String {
    var rows: [JSONValue] = []
    for name in try POSIX.directoryEntryNames(root).sorted() {
        let child = try POSIX.openAt(root, name: name, flags: O_RDONLY); var status = stat(); guard fstat(child.fd, &status) == 0 else { throw POSIXFailure.system(errno) }
        let kind = status.st_mode & S_IFMT
        let itemDigest: String; let type: String; let size: Int64
        if kind == S_IFDIR { itemDigest = try treeDigest(child); type = "directory"; size = 0 }
        else if kind == S_IFREG, status.st_nlink == 1 { itemDigest = try digest(child); type = "regular"; size = Int64(status.st_size) }
        else { throw ArchiveFailure.unsafeArchive }
        rows.append(.object([("name", .string(name)), ("type", .string(type)), ("mode", .string(String(format: "%04o", status.st_mode & 0o777))), ("size", .integer(size)), ("sha256", .string(itemDigest))]))
    }
    return try CanonicalJSON.domainSHA256("R4DIR\0", .array(rows))
}
