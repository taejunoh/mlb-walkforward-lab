import Foundation
import CryptoKit

public enum CanonicalJSONError: Error, Equatable { case invalidUTF8, invalidJSON, duplicateKey, nonCanonical, invalidNumber, resourceLimit }

public indirect enum JSONValue {
    case object([(String, JSONValue)]), array([JSONValue]), string(String), integer(Int64), bool(Bool), null
}

public enum CanonicalJSON {
    public static let maximumInputBytes = 64 * 1024 * 1024
    public static let maximumNesting = 128
    public static let maximumNodes = 500_000
    public static let maximumCollectionMembers = 250_000

    public static func decode(bytes: [UInt8]) throws -> JSONValue {
        guard bytes.count <= maximumInputBytes else { throw CanonicalJSONError.resourceLimit }
        guard !bytes.contains(0x0a), !bytes.contains(0x0d), String(bytes: bytes, encoding: .utf8) != nil else { throw CanonicalJSONError.invalidUTF8 }
        var parser = Parser(bytes)
        let value = try parser.value()
        guard parser.atEnd else { throw CanonicalJSONError.invalidJSON }
        guard try encode(value) == bytes else { throw CanonicalJSONError.nonCanonical }
        return value
    }

    /// File documents are a canonical JSON body followed by no more than one LF.  This is
    /// deliberately separate from `decode`: protocol values remain strict JSON bodies.
    public static func decodeDocument(bytes: [UInt8]) throws -> JSONValue {
        guard !bytes.contains(0x0d) else { throw CanonicalJSONError.nonCanonical }
        var body = bytes
        if body.last == 0x0a { body.removeLast() }
        guard !body.contains(0x0a) else { throw CanonicalJSONError.nonCanonical }
        return try decode(bytes: body)
    }

    /// Legacy lock files are closed JSON documents but intentionally preserve their reviewed
    /// pretty-printed bytes.  This parser is only for that V1 byte-boundary.
    public static func decodeLegacyDocument(bytes: [UInt8]) throws -> JSONValue {
        guard !bytes.contains(0x0d), let object = try? JSONSerialization.jsonObject(with: Data(bytes), options: []) else { throw CanonicalJSONError.invalidJSON }
        func value(_ raw: Any) throws -> JSONValue {
            if raw is NSNull { return .null }
            if let flag = raw as? Bool { return .bool(flag) }
            if let number = raw as? NSNumber { let integer = number.int64Value; guard NSNumber(value: integer) == number else { throw CanonicalJSONError.invalidNumber }; return .integer(integer) }
            if let text = raw as? String { return .string(text) }
            if let array = raw as? [Any] { return .array(try array.map(value)) }
            if let dictionary = raw as? [String: Any] { return .object(try dictionary.map { ($0.key, try value($0.value)) }) }
            throw CanonicalJSONError.invalidJSON
        }
        return try value(object)
    }

    public static func encode(_ value: JSONValue) throws -> [UInt8] {
        var nodes = 0
        return try encode(value, depth: 0, nodes: &nodes)
    }

    private static func encode(_ value: JSONValue, depth: Int, nodes: inout Int) throws -> [UInt8] {
        guard depth <= maximumNesting else { throw CanonicalJSONError.resourceLimit }
        nodes += 1; guard nodes <= maximumNodes else { throw CanonicalJSONError.resourceLimit }
        switch value {
        case .null: return Array("null".utf8)
        case .bool(let b): return Array((b ? "true" : "false").utf8)
        case .integer(let n): return Array(String(n).utf8)
        case .string(let s): return quote(s)
        case .array(let values):
            guard values.count <= maximumCollectionMembers else { throw CanonicalJSONError.resourceLimit }
            var out: [UInt8] = [91]
            for (index, value) in values.enumerated() { if index > 0 { out.append(44) }; out += try encode(value, depth: depth + 1, nodes: &nodes) }
            return out + [93]
        case .object(let pairs):
            guard pairs.count <= maximumCollectionMembers else { throw CanonicalJSONError.resourceLimit }
            var seen = Set<String>()
            for (key, _) in pairs { guard seen.insert(key).inserted else { throw CanonicalJSONError.duplicateKey } }
            let ordered = pairs.sorted { utf16($0.0) .lexicographicallyPrecedes(utf16($1.0)) }
            var out: [UInt8] = [123]
            for (index, pair) in ordered.enumerated() { if index > 0 { out.append(44) }; out += quote(pair.0); out.append(58); out += try encode(pair.1, depth: depth + 1, nodes: &nodes) }
            return out + [125]
        }
    }

    public static func sha256(_ bytes: [UInt8]) -> String { SHA256.hash(data: Data(bytes)).map { String(format: "%02x", $0) }.joined() }

    public static func domainSHA256(_ domain: String, _ value: JSONValue) throws -> String {
        var bytes = Array(domain.utf8)
        bytes += try encode(value)
        return sha256(bytes)
    }

    /// Convenience for fixture/tool generation. Existing `sha256` is discarded before sealing.
    public static func withSelfDigest(_ text: String) throws -> String {
        let value = try decode(bytes: Array(text.utf8))
        guard case .object(let pairs) = value else { throw CanonicalJSONError.invalidJSON }
        let bare = pairs.filter { $0.0 != "sha256" }
        let digest = sha256(try encode(.object(bare)))
        return String(bytes: try encode(.object(bare + [("sha256", .string(digest))])), encoding: .utf8)!
    }

    private static func utf16(_ s: String) -> [UInt16] { Array(s.utf16) }
    private static func quote(_ string: String) -> [UInt8] {
        var out: [UInt8] = [34]
        for scalar in string.unicodeScalars {
            switch scalar.value {
            case 0x08: out += Array("\\b".utf8)
            case 0x09: out += Array("\\t".utf8)
            case 0x0a: out += Array("\\n".utf8)
            case 0x0c: out += Array("\\f".utf8)
            case 0x0d: out += Array("\\r".utf8)
            case 0...0x1f: out += Array(String(format: "\\u%04x", scalar.value).utf8)
            case 34: out += [92, 34]
            case 92: out += [92, 92]
            default: out += Array(String(scalar).utf8)
            }
        }
        return out + [34]
    }

    private struct Parser {
        let bytes: [UInt8]; var index = 0; var nodes = 0
        init(_ bytes: [UInt8]) { self.bytes = bytes }
        var atEnd: Bool { index == bytes.count }
        mutating func value(_ depth: Int = 0) throws -> JSONValue {
            guard depth <= CanonicalJSON.maximumNesting else { throw CanonicalJSONError.resourceLimit }
            nodes += 1; guard nodes <= CanonicalJSON.maximumNodes else { throw CanonicalJSONError.resourceLimit }
            guard index < bytes.count else { throw CanonicalJSONError.invalidJSON }
            switch bytes[index] {
            case 123: return try object(depth)
            case 91: return try array(depth)
            case 34: return .string(try string())
            case 45, 48...57: return .integer(try integer())
            case 116: try literal("true"); return .bool(true)
            case 102: try literal("false"); return .bool(false)
            case 110: try literal("null"); return .null
            default: throw CanonicalJSONError.invalidJSON
            }
        }
        mutating func object(_ depth: Int) throws -> JSONValue {
            index += 1; var pairs: [(String, JSONValue)] = []; var seen = Set<String>()
            if consume(125) { return .object(pairs) }
            while true {
                guard index < bytes.count, bytes[index] == 34 else { throw CanonicalJSONError.invalidJSON }
                let key = try string(); guard seen.insert(key).inserted else { throw CanonicalJSONError.duplicateKey }
                guard consume(58) else { throw CanonicalJSONError.invalidJSON }
                guard pairs.count < CanonicalJSON.maximumCollectionMembers else { throw CanonicalJSONError.resourceLimit }
                pairs.append((key, try value(depth + 1)))
                if consume(125) { return .object(pairs) }
                guard consume(44) else { throw CanonicalJSONError.invalidJSON }
            }
        }
        mutating func array(_ depth: Int) throws -> JSONValue {
            index += 1; var values: [JSONValue] = []
            if consume(93) { return .array(values) }
            while true { guard values.count < CanonicalJSON.maximumCollectionMembers else { throw CanonicalJSONError.resourceLimit }; values.append(try value(depth + 1)); if consume(93) { return .array(values) }; guard consume(44) else { throw CanonicalJSONError.invalidJSON } }
        }
        mutating func string() throws -> String {
            guard consume(34) else { throw CanonicalJSONError.invalidJSON }; var out: [UInt8] = []
            while index < bytes.count {
                let byte = bytes[index]; index += 1
                if byte == 34 { guard let s = String(bytes: out, encoding: .utf8) else { throw CanonicalJSONError.invalidUTF8 }; return s }
                guard byte >= 0x20 else { throw CanonicalJSONError.invalidJSON }
                if byte != 92 { out.append(byte); continue }
                guard index < bytes.count else { throw CanonicalJSONError.invalidJSON }; let escaped = bytes[index]; index += 1
                switch escaped {
                case 34, 92, 47: out.append(escaped)
                case 98: out.append(8); case 102: out.append(12); case 110: out.append(10); case 114: out.append(13); case 116: out.append(9)
                case 117:
                    let first = try hex4(); var scalar = UInt32(first)
                    if (0xd800...0xdbff).contains(first) {
                        guard consume(92), consume(117) else { throw CanonicalJSONError.invalidJSON }; let low = try hex4()
                        guard (0xdc00...0xdfff).contains(low) else { throw CanonicalJSONError.invalidJSON }
                        scalar = 0x10000 + UInt32(first - 0xd800) * 0x400 + UInt32(low - 0xdc00)
                    } else if (0xdc00...0xdfff).contains(first) { throw CanonicalJSONError.invalidJSON }
                    guard let us = UnicodeScalar(scalar) else { throw CanonicalJSONError.invalidJSON }; out += Array(String(us).utf8)
                default: throw CanonicalJSONError.invalidJSON
                }
            }; throw CanonicalJSONError.invalidJSON
        }
        mutating func hex4() throws -> UInt16 {
            guard index + 4 <= bytes.count else { throw CanonicalJSONError.invalidJSON }; var value: UInt16 = 0
            for _ in 0..<4 { let b = bytes[index]; index += 1; let n: UInt16; switch b { case 48...57: n = UInt16(b-48); case 65...70: n = UInt16(b-55); case 97...102: n = UInt16(b-87); default: throw CanonicalJSONError.invalidJSON }; value = value * 16 + n }; return value
        }
        mutating func integer() throws -> Int64 {
            let start = index; if consume(45) { guard index < bytes.count else { throw CanonicalJSONError.invalidNumber } }
            guard index < bytes.count else { throw CanonicalJSONError.invalidNumber }
            if bytes[index] == 48 { index += 1 } else { guard bytes[index] >= 49 && bytes[index] <= 57 else { throw CanonicalJSONError.invalidNumber }; while index < bytes.count && bytes[index] >= 48 && bytes[index] <= 57 { index += 1 } }
            if index < bytes.count, bytes[index] == 46 || bytes[index] == 69 || bytes[index] == 101 { throw CanonicalJSONError.invalidNumber }
            guard let n = Int64(String(bytes: bytes[start..<index], encoding: .utf8)!) else { throw CanonicalJSONError.invalidNumber }; return n
        }
        mutating func literal(_ literal: String) throws { let expected = Array(literal.utf8); guard index + expected.count <= bytes.count && Array(bytes[index..<index+expected.count]) == expected else { throw CanonicalJSONError.invalidJSON }; index += expected.count }
        mutating func consume(_ byte: UInt8) -> Bool { guard index < bytes.count, bytes[index] == byte else { return false }; index += 1; return true }
    }
}
