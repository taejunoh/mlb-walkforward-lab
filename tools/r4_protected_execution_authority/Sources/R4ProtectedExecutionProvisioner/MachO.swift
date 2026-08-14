import Foundation

/// A deliberately small Mach-O reader.  It accepts only the load-command subset which can
/// affect the dynamic loader; unknown commands are skipped only after their bounds are proved.
enum MachOFailure: Error, Equatable { case malformed, unsupported, dependencyEscape, ambiguousDependency, missingDependency, codeSignDrift }

struct MachODescriptor: Equatable {
    let dependencies: [String]
    let rpaths: [String]
    let slice: MachOSlice
}

struct MachOSlice: Equatable {
    let cpuType: Int64
    let cpuSubtype: Int64
    let fileType: Int64
    let uuid: String?
    let codeSignatureOffset: Int64?
    let codeSignatureSize: Int64?
}

enum MachOParser {
    private static let mhMagic32: UInt32 = 0xfeedface
    private static let mhMagic64: UInt32 = 0xfeedfacf
    private static let fatMagic: UInt32 = 0xcafebabe
    private static let fatMagic64: UInt32 = 0xcafebabf
    private static let lcLoadDylib: UInt32 = 0xc
    private static let lcLoadWeakDylib: UInt32 = 0x18 | 0x80000000
    private static let lcReexportDylib: UInt32 = 0x1f | 0x80000000
    private static let lcRpath: UInt32 = 0x1c | 0x80000000
    private static let lcUUID: UInt32 = 0x1b
    private static let lcCodeSignature: UInt32 = 0x1d

    static func parse(_ bytes: [UInt8]) throws -> MachODescriptor {
        let slices = try parseSlices(bytes)
        guard slices.count == 1, let slice = slices.first else { throw MachOFailure.unsupported }
        return slice
    }

    /// A fat executable has one dependency graph per architecture.  Choosing a
    /// convenient slice would leave an unobserved loader path executable on a
    /// different host, so callers that validate a closure must consume all of
    /// these descriptors.
    static func parseSlices(_ bytes: [UInt8]) throws -> [MachODescriptor] {
        guard bytes.count >= 4 else { throw MachOFailure.malformed }
        switch be32(bytes, 0) {
        case fatMagic: return try parseFat(bytes)
        case fatMagic64: return try parseFat64(bytes)
        default: return [try parseThin(bytes)]
        }
    }

    private static func parseThin(_ bytes: [UInt8]) throws -> MachODescriptor {
        let magic = le32(bytes, 0)
        guard magic == mhMagic32 || magic == mhMagic64 else { throw MachOFailure.unsupported }
        let header = magic == mhMagic64 ? 32 : 28
        guard bytes.count >= header else { throw MachOFailure.malformed }
        let commandCount = Int(try value32(bytes, 16))
        let commandsSize = Int(try value32(bytes, 20))
        guard commandCount <= 16_384, commandsSize >= 0, header <= bytes.count - commandsSize else { throw MachOFailure.malformed }
        var cursor = header; let end = header + commandsSize
        var dependencies: [String] = []; var rpaths: [String] = []
        var uuid: String?; var codeSignature: (Int64, Int64)?
        for _ in 0..<commandCount {
            guard cursor <= end - 8 else { throw MachOFailure.malformed }
            let command = try value32(bytes, cursor), size = Int(try value32(bytes, cursor + 4))
            guard size >= 8, cursor <= end - size else { throw MachOFailure.malformed }
            if [lcLoadDylib, lcLoadWeakDylib, lcReexportDylib, lcRpath].contains(command) {
                guard size >= 12 else { throw MachOFailure.malformed }
                let offset = Int(try value32(bytes, cursor + 8))
                guard offset >= 12, offset < size else { throw MachOFailure.malformed }
                let value = try cString(bytes, cursor + offset, cursor + size)
                if command == lcRpath { rpaths.append(value) } else { dependencies.append(value) }
            }
            if command == lcUUID {
                guard size == 24, uuid == nil else { throw MachOFailure.malformed }
                uuid = bytes[(cursor + 8)..<(cursor + 24)].map { String(format: "%02x", $0) }.joined()
            }
            if command == lcCodeSignature {
                guard size == 16, codeSignature == nil else { throw MachOFailure.malformed }
                let offset = Int64(try value32(bytes, cursor + 8))
                let length = Int64(try value32(bytes, cursor + 12))
                guard offset >= 0, length > 0, offset <= Int64(bytes.count) - length else { throw MachOFailure.malformed }
                codeSignature = (offset, length)
            }
            cursor += size
        }
        guard cursor == end else { throw MachOFailure.malformed }
        return MachODescriptor(dependencies: dependencies, rpaths: rpaths,
                               slice: MachOSlice(cpuType: Int64(Int32(bitPattern: try value32(bytes, 4))),
                                                 cpuSubtype: Int64(Int32(bitPattern: try value32(bytes, 8))),
                                                 fileType: Int64(try value32(bytes, 12)), uuid: uuid,
                                                 codeSignatureOffset: codeSignature?.0,
                                                 codeSignatureSize: codeSignature?.1))
    }

    private static func parseFat(_ bytes: [UInt8]) throws -> [MachODescriptor] {
        // FAT headers are big-endian. Every slice is part of the executable
        // closure; rejecting a malformed/unsupported one is safer than silently
        // selecting the current host architecture.
        guard bytes.count >= 8 else { throw MachOFailure.malformed }
        let count = Int(be32(bytes, 4)); guard count > 0, count <= 32, bytes.count >= 8 + count * 20 else { throw MachOFailure.malformed }
        var candidates: [MachODescriptor] = []
        for index in 0..<count {
            let base = 8 + index * 20, offset = Int(be32(bytes, base + 8)), size = Int(be32(bytes, base + 12))
            guard offset >= 0, size > 0, offset <= bytes.count - size else { throw MachOFailure.malformed }
            candidates.append(try parseThin(Array(bytes[offset..<(offset + size)])))
        }
        return candidates
    }

    private static func parseFat64(_ bytes: [UInt8]) throws -> [MachODescriptor] {
        // `fat_arch_64` contains 64-bit big-endian offset/size fields.  Do not
        // coerce them through Int until their range and addition are proved.
        guard bytes.count >= 8 else { throw MachOFailure.malformed }
        let count = Int(be32(bytes, 4))
        guard count > 0, count <= 32, count <= (bytes.count - 8) / 32 else { throw MachOFailure.malformed }
        var candidates: [MachODescriptor] = []
        var ranges: [Range<Int>] = []
        for index in 0..<count {
            let base = 8 + index * 32
            let offset64 = be64(bytes, base + 8), size64 = be64(bytes, base + 16)
            guard offset64 <= UInt64(Int.max), size64 > 0, size64 <= UInt64(Int.max) else { throw MachOFailure.malformed }
            let offset = Int(offset64), size = Int(size64)
            guard offset <= bytes.count, size <= bytes.count - offset else { throw MachOFailure.malformed }
            let range = offset..<(offset + size)
            guard !ranges.contains(where: { $0.overlaps(range) }) else { throw MachOFailure.malformed }
            ranges.append(range)
            candidates.append(try parseThin(Array(bytes[range])))
        }
        return candidates
    }

    private static func value32(_ bytes: [UInt8], _ offset: Int) throws -> UInt32 { guard offset >= 0, offset <= bytes.count - 4 else { throw MachOFailure.malformed }; return le32(bytes, offset) }
    private static func le32(_ b: [UInt8], _ o: Int) -> UInt32 { UInt32(b[o]) | UInt32(b[o + 1]) << 8 | UInt32(b[o + 2]) << 16 | UInt32(b[o + 3]) << 24 }
    private static func be32(_ b: [UInt8], _ o: Int) -> UInt32 { UInt32(b[o]) << 24 | UInt32(b[o + 1]) << 16 | UInt32(b[o + 2]) << 8 | UInt32(b[o + 3]) }
    private static func be64(_ b: [UInt8], _ o: Int) -> UInt64 {
        UInt64(b[o]) << 56 | UInt64(b[o + 1]) << 48 | UInt64(b[o + 2]) << 40 | UInt64(b[o + 3]) << 32 |
        UInt64(b[o + 4]) << 24 | UInt64(b[o + 5]) << 16 | UInt64(b[o + 6]) << 8 | UInt64(b[o + 7])
    }
    private static func cString(_ b: [UInt8], _ start: Int, _ end: Int) throws -> String {
        guard let nul = b[start..<end].firstIndex(of: 0), nul > start,
              let text = String(bytes: b[start..<nul], encoding: .utf8), text.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value <= 0x7e }) else { throw MachOFailure.malformed }
        return text
    }
}

enum MachOValidator {
    static func resolve(_ dependency: String, loader: String, executable: String, rpaths: [String], finalRuntime: String, exists: (String) throws -> Bool) throws -> String {
        let loaderDirectory = URL(fileURLWithPath: loader).deletingLastPathComponent().path
        func join(_ base: String, _ tail: String) -> String { URL(fileURLWithPath: base).appendingPathComponent(tail).standardized.path }
        func candidate(_ raw: String) -> String? {
            if raw.hasPrefix("/System/") || raw.hasPrefix("/usr/lib/") { return raw }
            if raw.hasPrefix("@loader_path/") { return join(loaderDirectory, String(raw.dropFirst("@loader_path/".count))) }
            if raw.hasPrefix("@executable_path/") { return join(URL(fileURLWithPath: executable).deletingLastPathComponent().path, String(raw.dropFirst("@executable_path/".count))) }
            return nil
        }
        if let fixed = candidate(dependency) { guard admissible(fixed, finalRuntime: finalRuntime), try exists(fixed) else { throw MachOFailure.dependencyEscape }; return fixed }
        guard dependency.hasPrefix("@rpath/") else { throw MachOFailure.dependencyEscape }
        let tail = String(dependency.dropFirst("@rpath/".count)); guard !tail.isEmpty, !tail.contains("..") else { throw MachOFailure.dependencyEscape }
        let candidates = rpaths.compactMap { candidate($0 + "/" + tail) }.filter { admissible($0, finalRuntime: finalRuntime) }
        let matches = try candidates.filter { try exists($0) }
        guard matches.count == 1 else { throw matches.isEmpty ? MachOFailure.missingDependency : MachOFailure.ambiguousDependency }
        return matches[0]
    }
    static func validate(_ bytes: [UInt8], loader: String, executable: String, finalRuntime: String, exists: (String) throws -> Bool) throws -> [(String, String)] {
        try MachOParser.parseSlices(bytes).flatMap { descriptor in
            try descriptor.dependencies.map { dependency in
                (dependency, try resolve(dependency, loader: loader, executable: executable, rpaths: descriptor.rpaths, finalRuntime: finalRuntime, exists: exists))
            }
        }
    }
    private static func admissible(_ path: String, finalRuntime: String) -> Bool {
        guard path.hasPrefix("/"), !path.contains("/../"), !path.hasPrefix("/opt/homebrew/"), !path.hasPrefix("/Users/"), !path.hasPrefix("/Network/") else { return false }
        return path.hasPrefix("/System/") || path.hasPrefix("/usr/lib/") || path == finalRuntime || path.hasPrefix(finalRuntime + "/")
    }
}
