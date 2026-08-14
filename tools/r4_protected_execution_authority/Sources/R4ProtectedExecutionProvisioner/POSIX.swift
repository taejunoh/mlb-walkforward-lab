import Darwin
import Foundation

enum POSIXFailure: Error, Equatable { case system(Int32), unsafeObject, unsupported }

/// Retained descriptor plus the device/inode identity observed when it was opened.
final class HeldDescriptor {
    let fd: Int32
    let device: dev_t
    let inode: ino_t

    init(_ fd: Int32) throws {
        var status = stat()
        try POSIX.fstatRetry(fd, &status)
        self.fd = fd
        device = status.st_dev
        inode = status.st_ino
    }

    deinit { _ = Darwin.close(fd) }

    func rebind(in parent: HeldDescriptor, name: String, directory: Bool = false) throws -> HeldDescriptor {
        let reopened = try POSIX.openAt(parent, name: name, flags: O_RDONLY | (directory ? O_DIRECTORY : 0))
        guard reopened.device == device, reopened.inode == inode else { throw POSIXFailure.unsafeObject }
        return reopened
    }
}

extension HeldDescriptor {
    /// Verifies a sealed regular object exclusively through its retained descriptor.  Sealed
    /// objects are always root:wheel, link-count one, ACL-free, have only policy-approved xattrs,
    /// and are immutable.
    func requireSealedRegular(mode: mode_t) throws {
        try POSIX.requireSealedMetadata(self, type: S_IFREG, mode: mode, exactNlink: true)
    }

    /// Verifies a sealed directory through its retained descriptor.  Directory link counts are
    /// filesystem-managed and therefore only need to remain non-zero.
    func requireSealedDirectory(mode: mode_t) throws {
        try POSIX.requireSealedMetadata(self, type: S_IFDIR, mode: mode, exactNlink: false)
    }

    /// Reopens a child without following a symlink and proves that it is still the same object.
    /// The expected type is checked as part of the rebind so callers cannot accidentally retain a
    /// descriptor for a different object kind.
    func rebindSameIdentity(in parent: HeldDescriptor, name: String, directory: Bool = false) throws -> HeldDescriptor {
        let rebound = try rebind(in: parent, name: name, directory: directory)
        var status = stat()
        try POSIX.fstatRetry(rebound.fd, &status)
        let expected = directory ? S_IFDIR : S_IFREG
        guard (status.st_mode & S_IFMT) == expected,
              rebound.device == device, rebound.inode == inode else { throw POSIXFailure.unsafeObject }
        return rebound
    }
}

struct DescriptorMetadata: Equatable {
    let type: mode_t; let uid: uid_t; let gid: gid_t; let mode: mode_t
    let nlink: nlink_t; let size: Int64; let device: dev_t; let inode: ino_t; let generation: UInt32
}

enum SealedACLObservation: Equatable {
    case none
    case entries
    case failure(Int32)
}

struct SealedMetadataSnapshot: Equatable {
    let type: mode_t
    let uid: uid_t
    let gid: gid_t
    let mode: mode_t
    let nlink: nlink_t
    let flags: UInt32
    let acl: SealedACLObservation
    let xattrs: [String]
}

enum POSIX {
    static let noFollow = O_NOFOLLOW | O_CLOEXEC

    /// Retries interruptible Darwin calls a bounded number of times. Persistent EINTR is a hard
    /// failure rather than an unbounded wait, preserving fail-closed behavior.
    static func retryEINTR(_ maximumAttempts: Int = 8, _ operation: () -> Int32) throws {
        guard maximumAttempts > 0 else { throw POSIXFailure.system(EINVAL) }
        for _ in 0..<maximumAttempts {
            if operation() == 0 { return }
            if errno != EINTR { throw POSIXFailure.system(errno) }
        }
        throw POSIXFailure.system(EINTR)
    }

    static func fstatRetry(_ fd: Int32, _ status: UnsafeMutablePointer<stat>) throws {
        try retryEINTR { Darwin.fstat(fd, status) }
    }

    /// Privileged entrypoints never inherit an ambient descriptor capability.  Standard streams
    /// are the only descriptors deliberately retained for the closed CLI's diagnostics.
    static func closeInheritedDescriptors() throws {
        let maximum = getdtablesize()
        guard maximum > 3 else { return }
        for fd in 3..<maximum where fcntl(fd, F_GETFD) != -1 {
            guard Darwin.close(fd) == 0 else { throw POSIXFailure.system(errno) }
        }
    }

    static func openDirectory(_ path: String) throws -> HeldDescriptor {
        let root = try open("/", flags: O_RDONLY | O_DIRECTORY | noFollow)
        var current = root
        for component in try absolutePathComponents(path) {
            current = try openAt(current, name: String(component), flags: O_RDONLY | O_DIRECTORY)
        }
        return current
    }

    /// `F_GETPATH` is used only to record a spelling for an already-held descriptor.
    /// The result is rejected unless it is the one canonical absolute form accepted by
    /// every other descriptor walker in this authority boundary.
    static func descriptorPath(_ descriptor: HeldDescriptor) throws -> String {
        var bytes = [CChar](repeating: 0, count: Int(MAXPATHLEN))
        guard Darwin.fcntl(descriptor.fd, F_GETPATH, &bytes) == 0 else { throw POSIXFailure.system(errno) }
        let path = String(cString: bytes)
        guard !path.isEmpty, !path.contains("\0"), path.hasPrefix("/") else { throw POSIXFailure.unsafeObject }
        let parts = try absolutePathComponents(path)
        let canonical = parts.isEmpty ? "/" : "/" + parts.joined(separator: "/")
        guard canonical == path else { throw POSIXFailure.unsafeObject }
        return path
    }

    static func metadata(_ descriptor: HeldDescriptor) throws -> DescriptorMetadata {
        var status = stat(); try fstatRetry(descriptor.fd, &status)
        return DescriptorMetadata(type: status.st_mode & S_IFMT, uid: status.st_uid, gid: status.st_gid,
                                  mode: status.st_mode & 0o7777, nlink: status.st_nlink,
                                  size: Int64(status.st_size), device: status.st_dev, inode: status.st_ino,
                                  generation: status.st_gen)
    }

    static func openTrustedRootOwnedDirectory(_ path: String) throws -> HeldDescriptor {
        let root = try open("/", flags: O_RDONLY | O_DIRECTORY | noFollow)
        try requireTrustedAncestor(root)
        var current = root
        for component in try absolutePathComponents(path) {
            let name = String(component)
            let child = try openAt(current, name: name, flags: O_RDONLY | O_DIRECTORY)
            _ = try child.rebind(in: current, name: name, directory: true)
            try requireTrustedAncestor(child)
            current = child
        }
        return current
    }

    static func openTrustedRootOwnedRegular(_ path: String) throws -> (parent: HeldDescriptor, name: String, file: HeldDescriptor) {
        let components = try absolutePathComponents(path)
        guard let name = components.last.map(String.init) else { throw POSIXFailure.unsafeObject }
        let parentPath = "/" + components.dropLast().joined(separator: "/")
        let parent = try openTrustedRootOwnedDirectory(parentPath)
        let file = try openAt(parent, name: name, flags: O_RDONLY)
        _ = try file.rebind(in: parent, name: name)
        var status = stat()
        try fstatRetry(file.fd, &status)
        guard (status.st_mode & S_IFMT) == S_IFREG,
              status.st_uid == 0, status.st_gid == 0, status.st_nlink == 1,
              (status.st_mode & 0o022) == 0 else { throw POSIXFailure.unsafeObject }
        try requireNoExtendedACL(file)
        return (parent, name, file)
    }

    /// Preserves the sole leading root separator while rejecting every lexical alias below it.
    /// Callers receive components only after `//`, `/.`, `/..`, and trailing separators have
    /// been rejected; `/` itself remains the one valid zero-component absolute path.
    static func absolutePathComponents(_ path: String) throws -> [Substring] {
        guard path.hasPrefix("/") else { throw POSIXFailure.unsafeObject }
        guard path != "/" else { return [] }
        let components = path.dropFirst().split(separator: "/", omittingEmptySubsequences: false)
        guard !components.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else {
            throw POSIXFailure.unsafeObject
        }
        return components
    }

    static func open(_ path: String, flags: Int32) throws -> HeldDescriptor {
        let fd = path.withCString { Darwin.open($0, flags) }
        guard fd >= 0 else { throw POSIXFailure.system(errno) }
        return try HeldDescriptor(fd)
    }

    static func openAt(_ parent: HeldDescriptor, name: String, flags: Int32) throws -> HeldDescriptor {
        try validName(name)
        let fd = name.withCString { Darwin.openat(parent.fd, $0, flags | noFollow) }
        guard fd >= 0 else { throw POSIXFailure.system(errno) }
        return try HeldDescriptor(fd)
    }

    static func mkdirAt(_ parent: HeldDescriptor, name: String, mode: mode_t) throws -> HeldDescriptor {
        try validName(name)
        let result = name.withCString { Darwin.mkdirat(parent.fd, $0, mode) }
        guard result == 0 else { throw POSIXFailure.system(errno) }
        return try openAt(parent, name: name, flags: O_RDONLY | O_DIRECTORY)
    }

    static func createExclusive(_ parent: HeldDescriptor, name: String, mode: mode_t) throws -> HeldDescriptor {
        try validName(name)
        let fd = name.withCString { Darwin.openat(parent.fd, $0, O_WRONLY | O_CREAT | O_EXCL | noFollow, mode) }
        guard fd >= 0 else { throw POSIXFailure.system(errno) }
        return try HeldDescriptor(fd)
    }

    static func writeAll(_ descriptor: HeldDescriptor, _ bytes: [UInt8]) throws {
        var offset = 0
        while offset < bytes.count {
            let count = bytes.withUnsafeBytes { raw in Darwin.write(descriptor.fd, raw.baseAddress!.advanced(by: offset), bytes.count - offset) }
            if count < 0, errno == EINTR { continue }
            guard count > 0 else { throw POSIXFailure.system(errno) }
            offset += count
        }
    }

    static func readAll(_ descriptor: HeldDescriptor, maximum: Int = 128 * 1024 * 1024) throws -> [UInt8] {
        guard lseek(descriptor.fd, 0, SEEK_SET) >= 0 else { throw POSIXFailure.system(errno) }
        var bytes: [UInt8] = []
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            var count: ssize_t = -1
            for _ in 0..<8 {
                count = Darwin.read(descriptor.fd, &buffer, buffer.count)
                if count >= 0 { break }
                if errno != EINTR { throw POSIXFailure.system(errno) }
            }
            guard count >= 0 else { throw POSIXFailure.system(EINTR) }
            if count == 0 { return bytes }
            guard bytes.count + count <= maximum else { throw POSIXFailure.unsafeObject }
            bytes += buffer.prefix(count)
        }
    }

    static func fsync(_ descriptor: HeldDescriptor) throws {
        while Darwin.fsync(descriptor.fd) != 0 {
            if errno == EINTR { continue }
            throw POSIXFailure.system(errno)
        }
    }

    /// Darwin regular-file durability is a two step barrier.  An unavailable full-media flush is
    /// not silently downgraded: bootstrap publication must fail closed.
    static func durableRegular(_ descriptor: HeldDescriptor) throws {
        try fsync(descriptor)
        while Darwin.fcntl(descriptor.fd, F_FULLFSYNC) != 0 {
            if errno == EINTR { continue }
            throw POSIXFailure.system(errno)
        }
    }

    static func durableDirectory(_ descriptor: HeldDescriptor) throws { try fsync(descriptor) }

    static func durableVolumeBarrier(_ descriptor: HeldDescriptor) throws {
        try retryEINTR {
            Darwin.fsync_volume_np(descriptor.fd, SYNC_VOLUME_FULLSYNC | SYNC_VOLUME_WAIT)
        }
    }

    /// Enumerates an already-held directory descriptor.  No pathname lookup is performed here.
    static func directoryEntryNames(_ descriptor: HeldDescriptor) throws -> Set<String> {
        var copied: Int32 = -1
        for _ in 0..<8 {
            copied = Darwin.openat(descriptor.fd, ".", O_RDONLY | O_DIRECTORY | noFollow)
            if copied >= 0 { break }
            if errno != EINTR { throw POSIXFailure.system(errno) }
        }
        guard copied >= 0 else { throw POSIXFailure.system(EINTR) }
        var status = stat()
        do { try fstatRetry(copied, &status) }
        catch { _ = Darwin.close(copied); throw error }
        guard status.st_dev == descriptor.device, status.st_ino == descriptor.inode else {
            _ = Darwin.close(copied)
            throw POSIXFailure.unsafeObject
        }
        guard let directory = Darwin.fdopendir(copied) else {
            _ = Darwin.close(copied)
            throw POSIXFailure.system(errno)
        }
        defer { _ = Darwin.closedir(directory) }
        var names: Set<String> = []
        while true {
            errno = 0
            guard let entry = Darwin.readdir(directory) else {
                guard errno == 0 else { throw POSIXFailure.system(errno) }
                break
            }
            let name = withUnsafePointer(to: &entry.pointee.d_name.0) {
                $0.withMemoryRebound(to: CChar.self, capacity: Int(MAXNAMLEN) + 1) { String(cString: $0) }
            }
            if name != "." && name != ".." { names.insert(name) }
        }
        var finalStatus = stat()
        try fstatRetry(copied, &finalStatus)
        guard finalStatus.st_dev == descriptor.device, finalStatus.st_ino == descriptor.inode else {
            throw POSIXFailure.unsafeObject
        }
        return names
    }

    static func requireRegular(_ descriptor: HeldDescriptor, uid: uid_t, gid: gid_t, mode: mode_t) throws {
        var status = stat()
        try fstatRetry(descriptor.fd, &status)
        guard (status.st_mode & S_IFMT) == S_IFREG, status.st_uid == uid, status.st_gid == gid,
              (status.st_mode & 0o7777) == mode, status.st_nlink == 1 else { throw POSIXFailure.unsafeObject }
        try requireNoExtendedACL(descriptor)
    }

    static func requireDirectory(_ descriptor: HeldDescriptor, uid: uid_t, gid: gid_t, mode: mode_t) throws {
        var status = stat()
        try fstatRetry(descriptor.fd, &status)
        guard (status.st_mode & S_IFMT) == S_IFDIR, status.st_uid == uid, status.st_gid == gid,
              (status.st_mode & 0o7777) == mode, status.st_nlink >= 1 else { throw POSIXFailure.unsafeObject }
        try requireNoExtendedACL(descriptor)
    }

    static func requireNoExtendedACL(_ descriptor: HeldDescriptor) throws {
        errno = 0
        guard let acl = acl_get_fd_np(descriptor.fd, ACL_TYPE_EXTENDED) else {
            return try validateACLProbe(nilErrno: errno, entryResult: nil, entryErrno: nil)
        }
        defer { _ = acl_free(UnsafeMutableRawPointer(acl)) }
        var entry: acl_entry_t?
        errno = 0
        let result = acl_get_entry(acl, ACL_FIRST_ENTRY.rawValue, &entry)
        if result == 0, entry != nil { throw POSIXFailure.unsafeObject }
        try validateACLProbe(nilErrno: nil, entryResult: result, entryErrno: errno)
    }

    /// Sealed objects follow the existing storage object policy and allow only the platform's
    /// ordinary provenance attribute.  Provider/cloud attributes and arbitrary caller-added
    /// attributes are unexpected and rejected.  Provenance is attached automatically on macOS,
    /// so rejecting it would make otherwise valid sealed objects unverifiable.
    static func requireNoUnexpectedExtendedAttributes(_ descriptor: HeldDescriptor) throws {
        let names = try extendedAttributeNames(descriptor)
        guard names.allSatisfy({ $0 == "com.apple.provenance" }) else { throw POSIXFailure.unsafeObject }
    }

    static func extendedAttributeNames(_ descriptor: HeldDescriptor) throws -> [String] {
        var count: ssize_t
        repeat {
            count = flistxattr(descriptor.fd, nil, 0, 0)
            if count < 0, errno == EINTR { continue }
            break
        } while true
        guard count >= 0 else { throw POSIXFailure.system(errno) }
        guard count > 0 else { return [] }
        var bytes = [CChar](repeating: 0, count: Int(count))
        var filled: ssize_t
        repeat {
            filled = flistxattr(descriptor.fd, &bytes, bytes.count, 0)
            if filled < 0, errno == EINTR { continue }
            break
        } while true
        guard filled == count else {
            if filled < 0 { throw POSIXFailure.system(errno) }
            throw POSIXFailure.unsafeObject
        }
        return bytes.split(separator: 0).map { String(decoding: $0.map(UInt8.init(bitPattern:)), as: UTF8.self) }
    }

    static func validateSealedMetadata(_ snapshot: SealedMetadataSnapshot, expectedType: mode_t,
                                       mode: mode_t, exactNlink: Bool) throws {
        guard snapshot.type == expectedType,
              snapshot.uid == 0, snapshot.gid == 0,
              snapshot.mode == mode,
              exactNlink ? snapshot.nlink == 1 : snapshot.nlink >= 1,
              (snapshot.flags & UInt32(SF_IMMUTABLE)) != 0 else {
            throw POSIXFailure.unsafeObject
        }
        switch snapshot.acl {
        case .none: break
        case .entries: throw POSIXFailure.unsafeObject
        case .failure(let code): throw POSIXFailure.system(code == 0 ? EIO : code)
        }
        guard snapshot.xattrs.allSatisfy({ $0 == "com.apple.provenance" }) else {
            throw POSIXFailure.unsafeObject
        }
    }

    static func requireSealedMetadata(_ descriptor: HeldDescriptor, type: mode_t,
                                      mode: mode_t, exactNlink: Bool) throws {
        var status = stat()
        try fstatRetry(descriptor.fd, &status)
        let acl: SealedACLObservation
        errno = 0
        if let extended = acl_get_fd_np(descriptor.fd, ACL_TYPE_EXTENDED) {
            defer { _ = acl_free(UnsafeMutableRawPointer(extended)) }
            var entry: acl_entry_t?
            errno = 0
            let result = acl_get_entry(extended, ACL_FIRST_ENTRY.rawValue, &entry)
            if result == 0, entry != nil { acl = .entries }
            else if result == 0 { acl = .none }
            else if result == 1 { acl = .entries }
            else { acl = .failure(errno) }
        } else {
            acl = errno == ENOENT ? .none : .failure(errno)
        }
        let snapshot = SealedMetadataSnapshot(type: status.st_mode & S_IFMT, uid: status.st_uid,
                                               gid: status.st_gid, mode: status.st_mode & 0o7777,
                                               nlink: status.st_nlink, flags: UInt32(status.st_flags),
                                               acl: acl, xattrs: try extendedAttributeNames(descriptor))
        try validateSealedMetadata(snapshot, expectedType: type, mode: mode, exactNlink: exactNlink)
    }

    /// Isolated so tests can exercise Darwin's ambiguous ACL outcomes without manufacturing a
    /// privileged ACL.  ENOENT after a zeroed errno is the only nil result meaning ACL-free.
    static func validateACLProbe(nilErrno: Int32?, entryResult: Int32?, entryErrno: Int32?) throws {
        if let nilErrno {
            guard nilErrno == ENOENT else { throw POSIXFailure.system(nilErrno == 0 ? EIO : nilErrno) }
            return
        }
        guard let entryResult else { throw POSIXFailure.system(EIO) }
        switch entryResult {
        case 0: return                    // no entries
        case 1: throw POSIXFailure.unsafeObject // one or more extended entries
        case -1:
            let code = entryErrno ?? 0
            throw POSIXFailure.system(code == 0 ? EIO : code)
        default: throw POSIXFailure.system(EIO)
        }
    }

    private static func requireTrustedAncestor(_ descriptor: HeldDescriptor) throws {
        var status = stat()
        try fstatRetry(descriptor.fd, &status)
        guard (status.st_mode & S_IFMT) == S_IFDIR,
              status.st_uid == 0, status.st_gid == 0, (status.st_mode & 0o022) == 0 else { throw POSIXFailure.unsafeObject }
        try requireNoExtendedACL(descriptor)
    }

    static func setImmutable(_ descriptor: HeldDescriptor) throws {
        try retryEINTR { Darwin.fchflags(descriptor.fd, UInt32(SF_IMMUTABLE)) }
        var status = stat()
        try retryEINTR { Darwin.fstat(descriptor.fd, &status) }
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) != 0 else { throw POSIXFailure.unsafeObject }
    }

    static func clearImmutable(_ descriptor: HeldDescriptor) throws {
        try retryEINTR { Darwin.fchflags(descriptor.fd, 0) }
        var status = stat()
        try retryEINTR { Darwin.fstat(descriptor.fd, &status) }
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw POSIXFailure.unsafeObject }
    }

    static func unlinkAt(_ parent: HeldDescriptor, name: String, directory: Bool = false) throws {
        try validName(name)
        let result = name.withCString { Darwin.unlinkat(parent.fd, $0, directory ? AT_REMOVEDIR : 0) }
        guard result == 0 else { throw POSIXFailure.system(errno) }
    }

    static func renameExclusive(from parent: HeldDescriptor, name: String, to destinationParent: HeldDescriptor, destination: String) throws {
        try validName(name); try validName(destination)
        let result = name.withCString { source in destination.withCString { target in
            Darwin.renameatx_np(parent.fd, source, destinationParent.fd, target, UInt32(RENAME_EXCL))
        } }
        guard result == 0 else { throw POSIXFailure.system(errno) }
    }

    /// Descriptor-relative atomic replacement used only for mutable state snapshots. Both names
    /// are held beneath the same already-attested directory; no ambient pathname is resolved.
    static func replaceAtomically(in parent: HeldDescriptor, source: String, destination: String) throws {
        try validName(source); try validName(destination)
        let result = source.withCString { from in destination.withCString { to in Darwin.renameat(parent.fd, from, parent.fd, to) } }
        guard result == 0 else { throw POSIXFailure.system(errno) }
    }

    private static func validName(_ name: String) throws {
        guard !name.isEmpty, !name.contains("/"), name != ".", name != "..", name.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7e }) else { throw POSIXFailure.unsafeObject }
    }
}

/// Descriptor-relative operations used by the publication protocol.  A failed operation leaves
/// the exclusively-created object in place as forensic residue; this type deliberately has no
/// cleanup or path-based fallback.
enum PublicationPOSIX {
#if DEBUG
    struct Operations {
        let setOwnership: (HeldDescriptor, uid_t, gid_t) throws -> Void
        let setMode: (HeldDescriptor, mode_t) throws -> Void
        let writeAll: (HeldDescriptor, [UInt8]) throws -> Void
        let durableRegular: (HeldDescriptor) throws -> Void
        let setImmutable: (HeldDescriptor) throws -> Void
        let rebind: (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor
        let readAll: (HeldDescriptor) throws -> [UInt8]
        let verifySealed: (HeldDescriptor, mode_t) throws -> Void
        let durableDirectory: (HeldDescriptor) throws -> Void

        static func production() -> Operations {
            Operations(
                setOwnership: { descriptor, uid, gid in
                    while fchown(descriptor.fd, uid, gid) != 0 {
                        if errno == EINTR { continue }
                        throw POSIXFailure.system(errno)
                    }
                },
                setMode: { descriptor, mode in
                    while fchmod(descriptor.fd, mode) != 0 {
                        if errno == EINTR { continue }
                        throw POSIXFailure.system(errno)
                    }
                },
                writeAll: { try POSIX.writeAll($0, $1) },
                durableRegular: { try POSIX.durableRegular($0) },
                setImmutable: { try POSIX.setImmutable($0) },
                rebind: { try $0.rebindSameIdentity(in: $1, name: $2) },
                readAll: { try POSIX.readAll($0) },
                verifySealed: { try $0.requireSealedRegular(mode: $1) },
                durableDirectory: { try POSIX.durableDirectory($0) }
            )
        }
    }
#endif

    static func createImmutableExclusive(_ parent: HeldDescriptor, _ name: String,
                                          _ bytes: [UInt8], mode: mode_t) throws -> HeldDescriptor {
        #if DEBUG
        return try createImmutableExclusive(parent, name, bytes, mode: mode, operations: .production())
        #else
        let created = try POSIX.createExclusive(parent, name: name, mode: mode)
        while fchown(created.fd, 0, 0) != 0 {
            if errno == EINTR { continue }
            throw POSIXFailure.system(errno)
        }
        while fchmod(created.fd, mode) != 0 {
            if errno == EINTR { continue }
            throw POSIXFailure.system(errno)
        }
        try POSIX.writeAll(created, bytes)
        try POSIX.durableRegular(created)
        try POSIX.setImmutable(created)
        try POSIX.durableRegular(created)
        let rebound = try created.rebindSameIdentity(in: parent, name: name)
        guard try POSIX.readAll(rebound) == bytes else { throw POSIXFailure.unsafeObject }
        try rebound.requireSealedRegular(mode: mode)
        return rebound
        #endif
    }

#if DEBUG
    static func createImmutableExclusive(_ parent: HeldDescriptor, _ name: String,
                                          _ bytes: [UInt8], mode: mode_t,
                                          operations: Operations) throws -> HeldDescriptor {
        let created = try POSIX.createExclusive(parent, name: name, mode: mode)
        try operations.setOwnership(created, 0, 0)
        try operations.setMode(created, mode)
        try operations.writeAll(created, bytes)
        try operations.durableRegular(created)
        try operations.setImmutable(created)
        try operations.durableRegular(created)
        let rebound = try operations.rebind(created, parent, name)
        guard try operations.readAll(rebound) == bytes else { throw POSIXFailure.unsafeObject }
        try operations.verifySealed(rebound, mode)
        return rebound
    }
#endif

    static func fsyncParents(_ first: HeldDescriptor, _ second: HeldDescriptor,
                             root: HeldDescriptor) throws {
        // This is deliberately parent-only durability. A caller that requires a media barrier
        // must invoke POSIX.durableVolumeBarrier explicitly after this operation.
#if DEBUG
        try fsyncParents(first, second, root: root, operations: .production())
#else
        try POSIX.durableDirectory(first)
        try POSIX.durableDirectory(second)
        try POSIX.durableDirectory(root)
#endif
    }

#if DEBUG
    static func fsyncParents(_ first: HeldDescriptor, _ second: HeldDescriptor,
                             root: HeldDescriptor, operations: Operations) throws {
        try operations.durableDirectory(first)
        try operations.durableDirectory(second)
        try operations.durableDirectory(root)
    }
#endif
}
