import Foundation

public enum EvidenceError: Error, Equatable { case invalidShape, unknownKey, missingKey, invalidValue, invalidDigest, unorderedArray }

public struct SHA256Digest: Equatable, Hashable { public let value: String; public init(_ value: String) throws { guard value.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }; self.value = value } }
public struct GitObjectID: Equatable, Hashable { public let value: String; public init(_ value: String) throws { guard value.range(of: "^[0-9a-f]{40}$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }; self.value = value } }

private func object(_ value: JSONValue, _ keys: Set<String>) throws -> [String: JSONValue] {
    guard case .object(let pairs) = value else { throw EvidenceError.invalidShape }
    guard Set(pairs.map(\.0)).count == pairs.count else { throw EvidenceError.invalidShape }
    let actual = Set(pairs.map(\.0)); guard actual == keys else { throw actual.isSubset(of: keys) ? EvidenceError.missingKey : EvidenceError.unknownKey }
    return Dictionary(uniqueKeysWithValues: pairs)
}
private func text(_ dict: [String: JSONValue], _ key: String) throws -> String { guard case .string(let value) = dict[key] else { throw EvidenceError.invalidShape }; return value }
private func integer(_ dict: [String: JSONValue], _ key: String) throws -> Int64 { guard case .integer(let value) = dict[key] else { throw EvidenceError.invalidShape }; return value }
private func array(_ dict: [String: JSONValue], _ key: String) throws -> [JSONValue] { guard case .array(let value) = dict[key] else { throw EvidenceError.invalidShape }; return value }
private func requiredSHA(_ dict: [String: JSONValue], _ key: String) throws -> SHA256Digest { try SHA256Digest(try text(dict, key)) }
private func require(_ actual: String, _ expected: String) throws { guard actual == expected else { throw EvidenceError.invalidValue } }
private func validPath(_ path: String) throws {
    guard !path.isEmpty, path.unicodeScalars.allSatisfy({ $0.value >= 0x21 && $0.value <= 0x7e }), !path.hasPrefix("/"), !path.hasSuffix("/"), !path.contains("\\") else { throw EvidenceError.invalidValue }
    let components = path.split(separator: "/", omittingEmptySubsequences: false)
    guard components.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else { throw EvidenceError.invalidValue }
}
private func validTimestamp(_ timestamp: String) throws {
    guard timestamp.range(of: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }
}
private func validAttemptUUID(_ value: String) throws {
    guard value.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }
}
private func validAbsoluteRuntimePath(_ path: String) throws {
    let roots = ["/usr/lib", "/System", ProtectedExecutionConstants.productionRoot]
    guard let root = roots.first(where: { path == $0 || path.hasPrefix($0 + "/") }) else { throw EvidenceError.invalidValue }
    if path != root { try validPath(String(path.dropFirst(root.count + 1))) }
}
private func validRuntimeDependency(_ path: String) throws {
    if path.hasPrefix("/") { return try validAbsoluteRuntimePath(path) }
    for prefix in ["@loader_path", "@executable_path", "@rpath"] where path == prefix || path.hasPrefix(prefix + "/") {
        if path != prefix { try validPath(String(path.dropFirst(prefix.count + 1))) }
        return
    }
    throw EvidenceError.invalidValue
}
private func orderedUniquePaths(_ paths: [String]) throws { guard paths == paths.sorted(), Set(paths).count == paths.count, Set(paths.map { $0.lowercased() }).count == paths.count else { throw EvidenceError.unorderedArray } }
private func orderedUniqueTuples(_ tuples: [[String]]) throws { guard tuples == tuples.sorted(by: { $0.lexicographicallyPrecedes($1) }), Set(tuples.map { $0.joined(separator: "\u{0}") }).count == tuples.count else { throw EvidenceError.unorderedArray } }
private func selfDigest(_ value: JSONValue) throws {
    guard case .object(let pairs) = value, let raw = pairs.first(where: { $0.0 == "sha256" })?.1, case .string(let claimed) = raw else { throw EvidenceError.invalidDigest }
    _ = try SHA256Digest(claimed)
    let bare = pairs.filter { $0.0 != "sha256" }
    guard CanonicalJSON.sha256(try CanonicalJSON.encode(.object(bare))) == claimed else { throw EvidenceError.invalidDigest }
}
private func decoded(_ source: String) throws -> JSONValue { try CanonicalJSON.decode(bytes: Array(source.utf8)) }

public struct ManifestEntry: Equatable {
    public let path: String; public let type: String; public let mode: String; public let size: Int64; public let sha256: SHA256Digest
    static func decode(_ value: JSONValue) throws -> ManifestEntry { let d = try object(value, ["path","type","mode","size","sha256"]); let p = try text(d,"path"); try validPath(p); let type = try text(d,"type"); guard ["directory","regular"].contains(type) else { throw EvidenceError.invalidValue }; let mode = try text(d,"mode"); let size = try integer(d,"size"); guard (type == "directory" ? mode == "0555" && size == 0 : ["0444", "0555"].contains(mode) && size >= 0) else { throw EvidenceError.invalidValue }; return try ManifestEntry(path:p,type:type,mode:mode,size:size,sha256:requiredSHA(d,"sha256")) }
}
public struct RequiredBlob: Equatable {
    public let path: String; public let blob: GitObjectID; public let sha256: SHA256Digest
    static func decode(_ value: JSONValue) throws -> RequiredBlob { let d = try object(value,["path","blob","sha256"]); let p = try text(d,"path"); try validPath(p); return try RequiredBlob(path:p,blob:GitObjectID(text(d,"blob")),sha256:requiredSHA(d,"sha256")) }
}
public struct GitEvidence: Equatable {
    public let head: GitObjectID; public let tree: GitObjectID; public let requiredBlobs: [RequiredBlob]
    static func decode(_ value: JSONValue) throws -> GitEvidence { let d = try object(value,["head","tree","required_blobs"]); let blobs = try array(d,"required_blobs").map(RequiredBlob.decode); try orderedUniquePaths(blobs.map(\.path)); return try GitEvidence(head:GitObjectID(text(d,"head")),tree:GitObjectID(text(d,"tree")),requiredBlobs:blobs) }
}
public struct HelperHash: Equatable { public let path: String; public let sha256: SHA256Digest; static func decode(_ v: JSONValue) throws -> HelperHash { let d = try object(v,["path","sha256"]); let p = try text(d,"path"); try validPath(p); return try HelperHash(path:p,sha256:requiredSHA(d,"sha256")) } }
public struct KeyringRecord: Equatable {
    public enum Variant: Equatable { case frozenGenesis, rotationV2, revocationV2 }
    public let variant: Variant; public let kind: String; public let keyID: SHA256Digest; public let generation: Int64; public let publicKeySHA256: SHA256Digest; public let previousRecordSHA256: SHA256Digest?; public let previousPublicKeySHA256: SHA256Digest?; public let previousGeneration: Int64?; public let issuedAt: String; public let detachedSignatureSHA256: SHA256Digest; public let sha256: SHA256Digest; public let namespace: String?; public let authorityID: String?; public let state: String?; public let sequence: Int64?; public let revokedAt: String?; public let revocationReason: String?; public let signerGeneration: Int64?; public let signerKeyRecordSHA256: SHA256Digest?; public let targetKeyRecordSHA256: SHA256Digest?
    public static func decode(_ source: String) throws -> KeyringRecord {
        let v = try decoded(source); try selfDigest(v); guard case .object(let pairs) = v, Set(pairs.map(\.0)).count == pairs.count else { throw EvidenceError.invalidShape }; let d0 = Dictionary(uniqueKeysWithValues:pairs); guard d0["kind"] != nil else { throw EvidenceError.missingKey }; let kind = try text(d0,"kind")
        let keys: Set<String>
        switch kind {
        case "R4_PROTECTED_EXECUTION_KEYRING_V1":
            keys = ["kind","key_id","generation","public_key_sha256","previous_record_sha256","issued_at","detached_signature_sha256","sha256"]
        case "R4_PROTECTED_EXECUTION_KEY_ROTATION_V2":
            keys = ["authority_id","generation","issued_at","kind","namespace","new_public_key_sha256","previous_generation","previous_public_key_sha256","previous_record_sha256","detached_signature_sha256","sha256"]
        case "R4_PROTECTED_EXECUTION_REVOCATION_V2":
            keys = ["authority_id","detached_signature_sha256","kind","namespace","previous_revocation_sha256","reason","revoked_at","sequence","sha256","signer_generation","signer_key_record_sha256","signer_public_key_sha256","state","target_generation","target_key_record_sha256","target_public_key_sha256"]
        default: throw EvidenceError.invalidValue
        }
        let d = try object(v,keys); let issued = kind == "R4_PROTECTED_EXECUTION_REVOCATION_V2" ? "" : try text(d,"issued_at"); guard kind == "R4_PROTECTED_EXECUTION_REVOCATION_V2" || !issued.isEmpty else { throw EvidenceError.invalidValue }
        let variant: Variant; let keyID: SHA256Digest; let generation: Int64; let publicKey: SHA256Digest; let previous: SHA256Digest?; let previousPublic: SHA256Digest?; let previousGeneration: Int64?; let namespace: String?; let authorityID: String?; let state: String?; let sequence: Int64?; let revokedAt: String?; let reason: String?; let signerGeneration: Int64?; let signerRecord: SHA256Digest?; let targetRecord: SHA256Digest?
        if kind == "R4_PROTECTED_EXECUTION_KEYRING_V1" {
            generation = try integer(d,"generation"); guard generation == 1, case .null = d["previous_record_sha256"] else { throw EvidenceError.invalidValue }
            publicKey = try requiredSHA(d,"public_key_sha256"); keyID = try requiredSHA(d,"key_id")
            guard issued == "bootstrap-generation-1", keyID == publicKey else { throw EvidenceError.invalidValue }
            variant = .frozenGenesis; previous = nil; previousPublic = nil; previousGeneration = nil; namespace = nil; authorityID = nil; state = nil; sequence = nil; revokedAt = nil; reason = nil; signerGeneration = nil; signerRecord = nil; targetRecord = nil
        } else if kind == "R4_PROTECTED_EXECUTION_KEY_ROTATION_V2" {
            try require(text(d,"namespace"), ProtectedExecutionConstants.namespace); try require(text(d,"authority_id"), ProtectedExecutionConstants.authorityID)
            generation = try integer(d,"generation"); let predecessor = try integer(d,"previous_generation"); guard generation >= 2, predecessor == generation - 1 else { throw EvidenceError.invalidValue }; try validTimestamp(issued)
            previous = try requiredSHA(d,"previous_record_sha256"); previousPublic = try requiredSHA(d,"previous_public_key_sha256"); publicKey = try requiredSHA(d,"new_public_key_sha256"); guard publicKey != previousPublic else { throw EvidenceError.invalidValue }
            variant = .rotationV2; keyID = publicKey; previousGeneration = predecessor; namespace = try text(d,"namespace"); authorityID = try text(d,"authority_id"); state = nil; sequence = nil; revokedAt = nil; reason = nil; signerGeneration = nil; signerRecord = nil; targetRecord = nil
        } else {
            try require(text(d,"namespace"), ProtectedExecutionConstants.namespace); try require(text(d,"authority_id"), ProtectedExecutionConstants.authorityID)
            generation = try integer(d,"target_generation"); let signer = try integer(d,"signer_generation"); let targetPublic = try requiredSHA(d,"target_public_key_sha256"); let signerPublic = try requiredSHA(d,"signer_public_key_sha256"); let signerKeyRecord = try requiredSHA(d,"signer_key_record_sha256"); let targetKeyRecord = try requiredSHA(d,"target_key_record_sha256"); guard generation >= 1, signer == generation, signerPublic == targetPublic, signerKeyRecord == targetKeyRecord, try integer(d,"sequence") == 1, try text(d,"state") == "REVOKED", ["KEY_COMPROMISE","HELPER_COMPROMISE","MANIFEST_COMPROMISE","PLATFORM_ASSUMPTION_INVALID"].contains(try text(d,"reason")), case .null = d["previous_revocation_sha256"] else { throw EvidenceError.invalidValue }
            let revoked = try text(d,"revoked_at"); try validTimestamp(revoked)
            variant = .revocationV2; publicKey = targetPublic; keyID = targetPublic; previous = targetKeyRecord; previousPublic = signerPublic; previousGeneration = nil; namespace = try text(d,"namespace"); authorityID = try text(d,"authority_id"); state = try text(d,"state"); sequence = try integer(d,"sequence"); revokedAt = revoked; reason = try text(d,"reason"); signerGeneration = signer; signerRecord = signerKeyRecord; targetRecord = targetKeyRecord
        }
        let signature = try requiredSHA(d,"detached_signature_sha256")
        return try KeyringRecord(variant:variant,kind:kind,keyID:keyID,generation:generation,publicKeySHA256:publicKey,previousRecordSHA256:previous,previousPublicKeySHA256:previousPublic,previousGeneration:previousGeneration,issuedAt:issued,detachedSignatureSHA256:signature,sha256:requiredSHA(d,"sha256"),namespace:namespace,authorityID:authorityID,state:state,sequence:sequence,revokedAt:revokedAt,revocationReason:reason,signerGeneration:signerGeneration,signerKeyRecordSHA256:signerRecord,targetKeyRecordSHA256:targetRecord)
    }
    public func frozenGenesisSignaturePreimage() throws -> [UInt8] {
        guard variant == .frozenGenesis else { throw EvidenceError.invalidValue }
        return try CanonicalJSON.encode(.object([("generation", .integer(generation)), ("issued_at", .string(issuedAt)), ("key_id", .string(keyID.value)), ("kind", .string(kind)), ("previous_record_sha256", .null), ("public_key_sha256", .string(publicKeySHA256.value))]))
    }
    public func frozenGenesisRecordBytes() throws -> [UInt8] {
        guard variant == .frozenGenesis else { throw EvidenceError.invalidValue }
        let signed = try CanonicalJSON.decode(bytes: frozenGenesisSignaturePreimage())
        guard case .object(let fields) = signed else { throw EvidenceError.invalidShape }
        let withSignature = fields + [("detached_signature_sha256", .string(detachedSignatureSHA256.value))]
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(withSignature)))
        guard digest == sha256.value else { throw EvidenceError.invalidDigest }
        return try CanonicalJSON.encode(.object(withSignature + [("sha256", .string(sha256.value))]))
    }
    public func canonicalRecordBytes() throws -> [UInt8] {
        switch variant {
        case .frozenGenesis: return try frozenGenesisRecordBytes()
        case .rotationV2:
            guard let namespace, let authorityID, let previousGeneration, let previousRecordSHA256, let previousPublicKeySHA256 else { throw EvidenceError.invalidValue }
            let fields: [(String, JSONValue)] = [("authority_id", .string(authorityID)), ("generation", .integer(generation)), ("issued_at", .string(issuedAt)), ("kind", .string(kind)), ("namespace", .string(namespace)), ("new_public_key_sha256", .string(publicKeySHA256.value)), ("previous_generation", .integer(previousGeneration)), ("previous_public_key_sha256", .string(previousPublicKeySHA256.value)), ("previous_record_sha256", .string(previousRecordSHA256.value)), ("detached_signature_sha256", .string(detachedSignatureSHA256.value))]
            guard CanonicalJSON.sha256(try CanonicalJSON.encode(.object(fields))) == sha256.value else { throw EvidenceError.invalidDigest }
            return try CanonicalJSON.encode(.object(fields + [("sha256", .string(sha256.value))]))
        case .revocationV2:
            guard let namespace, let authorityID, let sequence, let revokedAt, let revocationReason, let signerGeneration, let signerKeyRecordSHA256, let targetKeyRecordSHA256, let previousPublicKeySHA256, let state else { throw EvidenceError.invalidValue }
            let fields: [(String, JSONValue)] = [("authority_id", .string(authorityID)), ("detached_signature_sha256", .string(detachedSignatureSHA256.value)), ("kind", .string(kind)), ("namespace", .string(namespace)), ("previous_revocation_sha256", .null), ("reason", .string(revocationReason)), ("revoked_at", .string(revokedAt)), ("sequence", .integer(sequence)), ("signer_generation", .integer(signerGeneration)), ("signer_key_record_sha256", .string(signerKeyRecordSHA256.value)), ("signer_public_key_sha256", .string(previousPublicKeySHA256.value)), ("state", .string(state)), ("target_generation", .integer(generation)), ("target_key_record_sha256", .string(targetKeyRecordSHA256.value)), ("target_public_key_sha256", .string(publicKeySHA256.value))]
            guard CanonicalJSON.sha256(try CanonicalJSON.encode(.object(fields))) == sha256.value else { throw EvidenceError.invalidDigest }
            return try CanonicalJSON.encode(.object(fields + [("sha256", .string(sha256.value))]))
        }
    }
    public func rotationSignaturePreimage() throws -> [UInt8] {
        guard variant == .rotationV2, let namespace, let authorityID, let previousGeneration, let previousRecordSHA256, let previousPublicKeySHA256 else { throw EvidenceError.invalidValue }
        return Array("R4KEYROT2\0".utf8) + (try CanonicalJSON.encode(.object([("authority_id", .string(authorityID)), ("generation", .integer(generation)), ("issued_at", .string(issuedAt)), ("kind", .string(kind)), ("namespace", .string(namespace)), ("new_public_key_sha256", .string(publicKeySHA256.value)), ("previous_generation", .integer(previousGeneration)), ("previous_public_key_sha256", .string(previousPublicKeySHA256.value)), ("previous_record_sha256", .string(previousRecordSHA256.value))])))
    }
    public func revocationSignaturePreimage() throws -> [UInt8] {
        guard variant == .revocationV2, let namespace, let authorityID, let sequence, let revokedAt, let revocationReason, let signerGeneration, let signerKeyRecordSHA256, let targetKeyRecordSHA256, let previousPublicKeySHA256, let state else { throw EvidenceError.invalidValue }
        return Array("R4KEYREVOKE2\0".utf8) + (try CanonicalJSON.encode(.object([("authority_id", .string(authorityID)), ("kind", .string(kind)), ("namespace", .string(namespace)), ("previous_revocation_sha256", .null), ("reason", .string(revocationReason)), ("revoked_at", .string(revokedAt)), ("sequence", .integer(sequence)), ("signer_generation", .integer(signerGeneration)), ("signer_key_record_sha256", .string(signerKeyRecordSHA256.value)), ("signer_public_key_sha256", .string(previousPublicKeySHA256.value)), ("state", .string(state)), ("target_generation", .integer(generation)), ("target_key_record_sha256", .string(targetKeyRecordSHA256.value)), ("target_public_key_sha256", .string(publicKeySHA256.value))])))
    }
    public static func validateContiguous(_ records: [KeyringRecord]) throws {
        guard let first = records.first, first.kind == "R4_PROTECTED_EXECUTION_KEYRING_V1", first.generation == 1 else { throw EvidenceError.invalidValue }
        var previous = first
        for record in records.dropFirst() {
            guard record.variant == .rotationV2, record.generation == previous.generation + 1, record.previousGeneration == previous.generation, record.previousRecordSHA256 == previous.sha256, record.previousPublicKeySHA256 == previous.publicKeySHA256 else { throw EvidenceError.invalidValue }
            previous = record
        }
    }
}
public typealias Keyring = KeyringRecord
public typealias RevocationRecord = KeyringRecord

private let v2Namespace = "org.mlb-walkforward.r4.protected-execution-authority/v1"
private let v2AuthorityID = "r4-disjoint-custody-v2-execution-v1"
private let r4StageToken = "__R4_STAGE__"
private let v2Environment = ["HOME=__R4_STAGE__/home", "LANG=C", "LC_ALL=C", "PATH=/usr/bin:/bin", "PIP_CONFIG_FILE=/dev/null", "PIP_DISABLE_PIP_VERSION_CHECK=1", "PIP_NO_INPUT=1", "PYTHONNOUSERSITE=1", "TMPDIR=__R4_STAGE__/tmp"]
private let r4ImportProbe = "import importlib,json,sys\nmods=(\"cryptography\",\"jsonschema\",\"numpy\",\"scipy\")\nprint(json.dumps({\"version\":sys.version.split()[0],\"paths\":sys.path,\"files\":[importlib.import_module(name).__file__ for name in mods]},sort_keys=True,separators=(\",\",\":\")))"
private let r4SandboxProfile = "(version 1)\n(deny default)\n(deny network*)\n(allow file-read* (subpath \"__R4_STAGE__/runtime/closure/python311\") (subpath \"__R4_STAGE__/wheelhouse\") (subpath \"__R4_STAGE__/home\") (subpath \"__R4_STAGE__/tmp\") (literal \"/dev/null\") (subpath \"/System\") (subpath \"/usr/lib\"))\n(allow file-write* (subpath \"__R4_STAGE__/runtime\") (subpath \"__R4_STAGE__/home\") (subpath \"__R4_STAGE__/tmp\"))"

private func v2Header(_ value: JSONValue, _ keys: Set<String>, _ kind: String) throws -> [String: JSONValue] {
    try selfDigest(value)
    let d = try object(value, keys)
    try require(text(d, "kind"), kind)
    try require(text(d, "namespace"), v2Namespace)
    try require(text(d, "authority_id"), v2AuthorityID)
    return d
}
private func ascii(_ value: String) throws { guard !value.isEmpty, value.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value <= 0x7e }) else { throw EvidenceError.invalidValue } }
private func strings(_ value: JSONValue) throws -> [String] { guard case .array(let values) = value else { throw EvidenceError.invalidShape }; return try values.map { guard case .string(let s) = $0 else { throw EvidenceError.invalidShape }; try ascii(s); return s } }
private func rawStrings(_ value: JSONValue) throws -> [String] { guard case .array(let values) = value else { throw EvidenceError.invalidShape }; return try values.map { guard case .string(let s) = $0 else { throw EvidenceError.invalidShape }; return s } }
private func v2Entries(_ value: JSONValue, prefix: String) throws -> [ManifestEntry] {
    let rows = try array(["rows": value], "rows").map(ManifestEntry.decode)
    try orderedUniquePaths(rows.map(\.path))
    let byPath = Dictionary(uniqueKeysWithValues: rows.map { ($0.path, $0) })
    guard byPath[prefix]?.type == "directory" else { throw EvidenceError.invalidValue }
    for row in rows {
        guard row.path == prefix || row.path.hasPrefix(prefix + "/") else { throw EvidenceError.invalidValue }
        if row.path != prefix { let parent = String(row.path[..<row.path.lastIndex(of: "/")!]); guard byPath[parent]?.type == "directory" else { throw EvidenceError.invalidValue } }
    }
    for row in rows where row.type == "directory" {
        let children = rows.compactMap { child -> JSONValue? in
            let p = row.path + "/"
            guard child.path.hasPrefix(p), !String(child.path.dropFirst(p.count)).contains("/") else { return nil }
            return .object([("name", .string(String(child.path.dropFirst(p.count)))), ("type", .string(child.type)), ("mode", .string(child.mode)), ("size", .integer(child.size)), ("sha256", .string(child.sha256.value))])
        }
        let digest = try CanonicalJSON.domainSHA256("R4DIR\0", .array(children))
        guard row.sha256.value == digest else { throw EvidenceError.invalidDigest }
    }
    return rows
}
private func v2Git(_ value: JSONValue) throws -> GitEvidence { try GitEvidence.decode(value) }
private func standardHashFields(_ d: [String: JSONValue], _ keys: [String]) throws { for key in keys { _ = try requiredSHA(d, key) } }

public struct InputBundleManifest: Equatable {
    public let lockSHA256: SHA256Digest; public let pythonArchiveFilename: String; public let pythonArchiveVersion: String; public let pythonArchiveURL: String; public let pythonArchiveSHA256: SHA256Digest; public let pythonArchiveSize: Int64; public let wheelFilenames: [String]; public let wheelVersions: [String]; public let wheelURLs: [String]; public let wheelSHA256: [SHA256Digest]; public let wheelSizes: [Int64]; public let materializationPolicySHA256: SHA256Digest; public let requiredBlobSetSHA256: SHA256Digest; public let wheelSetSHA256: SHA256Digest; public let git: GitEvidence; public let entries: [ManifestEntry]; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> InputBundleManifest {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","lock_path","lock_sha256","materialization_policy_path","git","artifacts","checkout_entries","materialization_policy_sha256","sha256"], "R4_RUNTIME_INPUT_BUNDLE_V2")
        try require(text(d, "lock_path"), "r4_protected_python311.lock.json"); try require(text(d, "materialization_policy_path"), "materialization-policy.json")
        let git = try v2Git(d["git"]!); let required = try CanonicalJSON.domainSHA256("R4V2_REQUIRED_BLOBS\0", d["git"]!.objectValue("required_blobs")); let artifactValues = try array(d, "artifacts"); guard !artifactValues.isEmpty else { throw EvidenceError.invalidValue }
        var filenames: [String] = []; var versions: [String] = []; var urls: [String] = []; var wheels: [JSONValue] = []; var wheelDigests: [SHA256Digest] = []; var sizes: [Int64] = []; var archive: SHA256Digest?
        for (index, value) in artifactValues.enumerated() {
            let a = try object(value, ["path","kind","filename","version","url","sha256","size"]); let filename = try text(a, "filename"); try validPath(filename); guard !filename.contains("/") else { throw EvidenceError.invalidValue }; let kind = try text(a, "kind")
            guard kind == (index == 0 ? "python_archive" : "wheel"), try text(a, "path") == "artifacts/" + filename, try integer(a, "size") >= 0 else { throw EvidenceError.invalidValue }
            let version = try text(a, "version"); let url = try text(a, "url"); let size = try integer(a, "size")
            if index == 0 { try ascii(version) }
            else { guard version.unicodeScalars.allSatisfy({ $0.value >= 0x20 && $0.value <= 0x7e }) else { throw EvidenceError.invalidValue } }
            try ascii(url); _ = try requiredSHA(a, "sha256"); filenames.append(filename); versions.append(version); urls.append(url); sizes.append(size)
            if index == 0 { archive = try requiredSHA(a, "sha256") } else { let digest = try requiredSHA(a, "sha256"); wheelDigests.append(digest); wheels.append(.object([("filename", .string(filename)), ("version", .string(try text(a, "version"))), ("url", .string(try text(a, "url"))), ("sha256", .string(try text(a, "sha256"))), ("size", .integer(try integer(a, "size")))])) }
        }
        try orderedUniquePaths(filenames); let checkout = try v2Entries(d["checkout_entries"]!, prefix: "checkout")
        return try InputBundleManifest(lockSHA256: requiredSHA(d, "lock_sha256"), pythonArchiveFilename: filenames[0], pythonArchiveVersion: versions[0], pythonArchiveURL: urls[0], pythonArchiveSHA256: archive!, pythonArchiveSize: sizes[0], wheelFilenames: Array(filenames.dropFirst()), wheelVersions: Array(versions.dropFirst()), wheelURLs: Array(urls.dropFirst()), wheelSHA256: wheelDigests, wheelSizes: Array(sizes.dropFirst()), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), requiredBlobSetSHA256: SHA256Digest(required), wheelSetSHA256: SHA256Digest(try CanonicalJSON.domainSHA256("R4V2_WHEEL_SET\0", .array(wheels))), git: git, entries: checkout, sha256: requiredSHA(d, "sha256"))
    }
    static func decodeDocument(_ bytes: [UInt8]) throws -> InputBundleManifest { try decode(String(decoding: try CanonicalJSON.encode(CanonicalJSON.decodeDocument(bytes: bytes)), as: UTF8.self)) }
}

public struct LockedArtifact: Equatable {
    public let filename: String; public let version: String?; public let url: String; public let sha256: SHA256Digest
}

/// The committed lock is V1 by design, but its JSON representation is closed and canonical.
/// The V2 transport graph pins the exact bytes through `InputBundleManifest.lock_sha256`.
public struct ProtectedPythonLock: Equatable {
    public let python: LockedArtifact; public let wheels: [LockedArtifact]
    public static func decode(_ source: String) throws -> ProtectedPythonLock {
        let value = try decoded(source)
        guard try CanonicalJSON.encode(value) == Array(source.utf8) else { throw EvidenceError.invalidValue }
        let root = try object(value, ["kind", "python", "wheels"])
        try require(text(root, "kind"), "R4_PROTECTED_PYTHON311_LOCK_V1")
        func artifact(_ value: JSONValue, python: Bool) throws -> LockedArtifact {
            let fields: Set<String> = python ? ["version", "url", "filename", "sha256"] : ["url", "filename", "sha256"]
            let row = try object(value, fields)
            let filename = try text(row, "filename"); try validPath(filename); guard !filename.contains("/") else { throw EvidenceError.invalidValue }
            let url = try text(row, "url"); try ascii(url)
            let version = python ? try text(row, "version") : nil
            if let version { try ascii(version) }
            return try LockedArtifact(filename: filename, version: version, url: url, sha256: requiredSHA(row, "sha256"))
        }
        let python = try artifact(root["python"]!, python: true)
        let wheels = try array(root, "wheels").map { try artifact($0, python: false) }
        guard !wheels.isEmpty, wheels.map(\.filename) == wheels.map(\.filename).sorted(), Set(wheels.map(\.filename)).count == wheels.count else { throw EvidenceError.unorderedArray }
        return ProtectedPythonLock(python: python, wheels: wheels)
    }
    static func decodeDocument(_ bytes: [UInt8]) throws -> ProtectedPythonLock { try decode(String(decoding: try CanonicalJSON.encode(CanonicalJSON.decodeLegacyDocument(bytes: bytes)), as: UTF8.self)) }
}

public struct CodeSignIdentity: Equatable {
    public let identifier: String; public let designatedRequirement: String; public let teamIdentifier: String?; public let certificateChainSHA256: SHA256Digest; public let sha256: SHA256Digest
    public var preimage: JSONValue { .object([("identifier", .string(identifier)), ("designated_requirement", .string(designatedRequirement)), ("team_identifier", teamIdentifier.map(JSONValue.string) ?? .null), ("certificate_chain_sha256", .string(certificateChainSHA256.value))]) }
    static func decode(_ value: JSONValue) throws -> CodeSignIdentity {
        let d = try object(value, ["identifier", "designated_requirement", "team_identifier", "certificate_chain_sha256", "sha256"])
        let identifier = try text(d, "identifier"); let requirement = try text(d, "designated_requirement")
        try ascii(identifier); try ascii(requirement)
        let team: String?
        switch d["team_identifier"]! { case .null: team = nil; case .string(let value): try ascii(value); team = value; default: throw EvidenceError.invalidShape }
        let chain = try requiredSHA(d, "certificate_chain_sha256")
        let claimed = try requiredSHA(d, "sha256")
        let identity = CodeSignIdentity(identifier: identifier, designatedRequirement: requirement, teamIdentifier: team, certificateChainSHA256: chain, sha256: claimed)
        guard try CanonicalJSON.domainSHA256("R4V2_CODESIGN_IDENTITY\0", identity.preimage) == claimed.value else { throw EvidenceError.invalidDigest }
        return identity
    }
}

public struct SandboxLauncher: Equatable {
    public let path: String; public let descriptorIdentitySHA256: SHA256Digest; public let sha256: SHA256Digest; public let codesignIdentity: CodeSignIdentity
    static func decode(_ value: JSONValue) throws -> SandboxLauncher {
        let d = try object(value, ["path", "descriptor_identity_sha256", "sha256", "codesign_identity"])
        try require(text(d, "path"), "/usr/bin/sandbox-exec")
        let identity = try CodeSignIdentity.decode(d["codesign_identity"]!)
        guard identity.identifier == "com.apple.sandbox-exec", identity.designatedRequirement == "identifier \"com.apple.sandbox-exec\" and anchor apple", identity.teamIdentifier == nil else { throw EvidenceError.invalidValue }
        return try SandboxLauncher(path: text(d, "path"), descriptorIdentitySHA256: requiredSHA(d, "descriptor_identity_sha256"), sha256: requiredSHA(d, "sha256"), codesignIdentity: identity)
    }
}

public struct BaseInterpreter: Equatable {
    public let path: String; public let sha256: SHA256Digest; public let machOSHA256: SHA256Digest; public let codesignIdentity: CodeSignIdentity
    static func decode(_ value: JSONValue) throws -> BaseInterpreter {
        let d = try object(value, ["path", "sha256", "mach_o_sha256", "codesign_identity"])
        try require(text(d, "path"), "runtime/closure/python311/bin/python3.11")
        return try BaseInterpreter(path: text(d, "path"), sha256: requiredSHA(d, "sha256"), machOSHA256: requiredSHA(d, "mach_o_sha256"), codesignIdentity: CodeSignIdentity.decode(d["codesign_identity"]!))
    }
}

public struct ChildExecutionPolicy: Equatable {
    public let cwd: String; public let environment: [String]; public let pipArgv: [String]; public let importArgv: [String]; public let timeoutSeconds: Int64; public let maxOutputBytes: Int64; public let sandboxProfileUTF8: String; public let sandboxProfileTemplateSHA256: SHA256Digest; public let childCommandTemplateSHA256: SHA256Digest; public let readAllow: [String]; public let writeAllow: [String]; public let networkDenied: Bool
}

public struct ActualChildCommand: Equatable {
    public let program: String; public let argv: [String]; public let cwd: String; public let environment: [String]; public let stdin: String; public let stdoutStderr: String; public let timeoutSeconds: Int64; public let maxOutputBytes: Int64; public let sandboxLauncherDescriptorIdentitySHA256: SHA256Digest; public let sandboxLauncherSHA256: SHA256Digest; public let sandboxLauncherCodesignIdentitySHA256: SHA256Digest; public let interpreterSHA256: SHA256Digest; public let interpreterMachOSHA256: SHA256Digest; public let interpreterCodesignIdentitySHA256: SHA256Digest; public let sandboxProfileSHA256: SHA256Digest
    public var canonicalValue: JSONValue { .object([("program", .string(program)), ("argv", .array(argv.map(JSONValue.string))), ("cwd", .string(cwd)), ("environment", .array(environment.map(JSONValue.string))), ("stdin", .string(stdin)), ("stdout_stderr", .string(stdoutStderr)), ("timeout_seconds", .integer(timeoutSeconds)), ("max_output_bytes", .integer(maxOutputBytes)), ("sandbox_launcher_descriptor_identity_sha256", .string(sandboxLauncherDescriptorIdentitySHA256.value)), ("sandbox_launcher_sha256", .string(sandboxLauncherSHA256.value)), ("sandbox_launcher_codesign_identity_sha256", .string(sandboxLauncherCodesignIdentitySHA256.value)), ("interpreter_sha256", .string(interpreterSHA256.value)), ("interpreter_mach_o_sha256", .string(interpreterMachOSHA256.value)), ("interpreter_codesign_identity_sha256", .string(interpreterCodesignIdentitySHA256.value)), ("sandbox_profile_sha256", .string(sandboxProfileSHA256.value))]) }
    public static func decode(_ value: JSONValue) throws -> ActualChildCommand {
        let d = try object(value, ["program","argv","cwd","environment","stdin","stdout_stderr","timeout_seconds","max_output_bytes","sandbox_launcher_descriptor_identity_sha256","sandbox_launcher_sha256","sandbox_launcher_codesign_identity_sha256","interpreter_sha256","interpreter_mach_o_sha256","interpreter_codesign_identity_sha256","sandbox_profile_sha256"])
        let program = try text(d, "program"); try require(program, "/usr/bin/sandbox-exec")
        let argv = try rawStrings(d["argv"]!); guard argv.count >= 4, argv.allSatisfy({ !$0.contains("\0") }), argv[0] == program, argv[1] == "-p", argv[2].unicodeScalars.allSatisfy({ $0.value <= 0x7f }), argv[3].hasSuffix("/runtime/closure/python311/bin/python3.11") else { throw EvidenceError.invalidValue }
        let cwd = try text(d, "cwd"); try require(cwd, "runtime")
        let environment = try strings(d["environment"]!); guard !environment.isEmpty else { throw EvidenceError.invalidValue }
        try require(text(d, "stdin"), "/dev/null"); try require(text(d, "stdout_stderr"), "ROOT_OWNED_CAPPED_PIPES")
        let timeout = try integer(d, "timeout_seconds"); let maxOutput = try integer(d, "max_output_bytes"); guard timeout > 0, maxOutput > 0 else { throw EvidenceError.invalidValue }
        return try ActualChildCommand(program: program, argv: argv, cwd: cwd, environment: environment, stdin: text(d, "stdin"), stdoutStderr: text(d, "stdout_stderr"), timeoutSeconds: timeout, maxOutputBytes: maxOutput, sandboxLauncherDescriptorIdentitySHA256: requiredSHA(d, "sandbox_launcher_descriptor_identity_sha256"), sandboxLauncherSHA256: requiredSHA(d, "sandbox_launcher_sha256"), sandboxLauncherCodesignIdentitySHA256: requiredSHA(d, "sandbox_launcher_codesign_identity_sha256"), interpreterSHA256: requiredSHA(d, "interpreter_sha256"), interpreterMachOSHA256: requiredSHA(d, "interpreter_mach_o_sha256"), interpreterCodesignIdentitySHA256: requiredSHA(d, "interpreter_codesign_identity_sha256"), sandboxProfileSHA256: requiredSHA(d, "sandbox_profile_sha256"))
    }
    public func installCommandSHA256() throws -> SHA256Digest { try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_INSTALL_COMMAND\0", canonicalValue)) }
    public func importCommandSHA256() throws -> SHA256Digest { try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_IMPORT_COMMAND\0", canonicalValue)) }
}

public enum ActualSandboxProfile {
    public static func sha256(_ utf8: String) throws -> SHA256Digest {
        guard utf8.unicodeScalars.allSatisfy({ $0.value <= 0x7f }) else { throw EvidenceError.invalidValue }
        return try SHA256Digest(CanonicalJSON.sha256(Array("R4V2_SANDBOX_PROFILE_ACTUAL\0".utf8) + Array(utf8.utf8)))
    }
}

public struct MachODependency: Equatable {
    public let loader: String; public let dependency: String; public let resolved: String
    public var canonicalValue: JSONValue { .object([("loader", .string(loader)), ("dependency", .string(dependency)), ("resolved", .string(resolved))]) }
    public static func decode(_ value: JSONValue) throws -> MachODependency {
        let item = try object(value, ["loader", "dependency", "resolved"])
        return try MachODependency(loader: text(item, "loader"), dependency: text(item, "dependency"), resolved: text(item, "resolved"))
    }
}

public struct MachOObservation: Equatable {
    public let path: String; public let slices: JSONValue; public let dependencies: JSONValue
    public var canonicalValue: JSONValue { .object([("path", .string(path)), ("slices", slices), ("dependencies", dependencies)]) }
    public static func == (lhs: MachOObservation, rhs: MachOObservation) -> Bool {
        guard let left = try? CanonicalJSON.encode(lhs.canonicalValue), let right = try? CanonicalJSON.encode(rhs.canonicalValue) else { return false }
        return left == right
    }
    public static func decode(_ value: JSONValue) throws -> MachOObservation {
        let d = try object(value, ["path", "slices", "dependencies"])
        let path = try text(d, "path")
        guard path.hasPrefix("runtime/closure/python311/"), !path.hasSuffix("/"), !path.contains("..") else { throw EvidenceError.invalidValue }
        let slices = try array(d, "slices"); guard !slices.isEmpty else { throw EvidenceError.invalidValue }
        var prior: (Int64, Int64)?
        for slice in slices {
            let s = try object(slice, ["cpu_type", "cpu_subtype", "file_type", "uuid", "code_signature_offset", "code_signature_size"])
            let tuple = (try integer(s, "cpu_type"), try integer(s, "cpu_subtype")); guard prior.map({ $0 < tuple }) ?? true, try integer(s, "file_type") >= 0, try integer(s, "code_signature_offset") >= 0, try integer(s, "code_signature_size") >= 0 else { throw EvidenceError.invalidValue }
            let uuid = try text(s, "uuid"); guard uuid.range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }; prior = tuple
        }
        let dependencies = try array(d, "dependencies"); var priorDependency: [String]? = nil
        for row in dependencies {
            let item = try object(row, ["loader", "dependency", "resolved"]); let tuple = [try text(item, "loader"), try text(item, "dependency"), try text(item, "resolved")]
            guard priorDependency.map({ $0.lexicographicallyPrecedes(tuple) }) ?? true else { throw EvidenceError.unorderedArray }; priorDependency = tuple
        }
        return MachOObservation(path: path, slices: .array(slices), dependencies: .array(dependencies))
    }
    public func sha256() throws -> SHA256Digest { try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_MACHO_OBSERVATION\0", canonicalValue)) }
}

/// One descriptor-rebound, signed Mach-O object in the final runtime.  The
/// nested Mach-O observation intentionally excludes this object's digest so
/// collection hashing is acyclic and canonical ordering remains by path.
public struct StagedMachOObservation: Equatable {
    public let path: String; public let descriptorIdentitySHA256: SHA256Digest; public let sha256: SHA256Digest; public let codesignIdentitySHA256: SHA256Digest; public let machOObservation: MachOObservation; public let dependencyClosureSHA256: SHA256Digest
    public init(path: String, descriptorIdentitySHA256: SHA256Digest, sha256: SHA256Digest, codesignIdentitySHA256: SHA256Digest, machOObservation: MachOObservation) throws {
        guard path == machOObservation.path else { throw EvidenceError.invalidValue }
        self.path = path; self.descriptorIdentitySHA256 = descriptorIdentitySHA256; self.sha256 = sha256; self.codesignIdentitySHA256 = codesignIdentitySHA256; self.machOObservation = machOObservation
        dependencyClosureSHA256 = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", machOObservation.dependencies))
    }
    public var canonicalValue: JSONValue { .object([("path", .string(path)), ("descriptor_identity_sha256", .string(descriptorIdentitySHA256.value)), ("sha256", .string(sha256.value)), ("codesign_identity_sha256", .string(codesignIdentitySHA256.value)), ("mach_o_observation", machOObservation.canonicalValue), ("dependency_closure_sha256", .string(dependencyClosureSHA256.value))]) }
    public static func decode(_ value: JSONValue) throws -> StagedMachOObservation {
        let d = try object(value, ["path", "descriptor_identity_sha256", "sha256", "codesign_identity_sha256", "mach_o_observation", "dependency_closure_sha256"])
        let observation = try MachOObservation.decode(d["mach_o_observation"]!)
        let result = try StagedMachOObservation(path: text(d, "path"), descriptorIdentitySHA256: requiredSHA(d, "descriptor_identity_sha256"), sha256: requiredSHA(d, "sha256"), codesignIdentitySHA256: requiredSHA(d, "codesign_identity_sha256"), machOObservation: observation)
        let claim = try requiredSHA(d, "dependency_closure_sha256")
        guard result.dependencyClosureSHA256 == claim else { throw EvidenceError.invalidDigest }
        return result
    }
    public static func decodeArray(_ value: JSONValue) throws -> [StagedMachOObservation] {
        guard case .array(let values) = value else { throw EvidenceError.invalidShape }
        let objects = try values.map(StagedMachOObservation.decode)
        guard !objects.isEmpty else { throw EvidenceError.invalidValue }
        var prior: String?
        for object in objects { guard prior.map({ $0 < object.path }) ?? true else { throw EvidenceError.unorderedArray }; prior = object.path }
        return objects
    }
    public static func collectionSHA256(_ objects: [StagedMachOObservation]) throws -> SHA256Digest {
        try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_STAGED_MACHO_OBSERVATIONS\0", .array(objects.map(\.canonicalValue))))
    }
    public static func flattenedDependencies(_ objects: [StagedMachOObservation]) throws -> [MachODependency] {
        let dependencies = try objects.flatMap { object -> [MachODependency] in
            guard case .array(let values) = object.machOObservation.dependencies else { throw EvidenceError.invalidShape }
            return try values.map(MachODependency.decode)
        }
        let tuples = dependencies.map { [$0.loader, $0.dependency, $0.resolved] }
        guard tuples == tuples.sorted(by: { $0.lexicographicallyPrecedes($1) }), Set(tuples.map { $0.joined(separator: "\0") }).count == tuples.count else { throw EvidenceError.unorderedArray }
        return dependencies
    }
}

/// Held post-rebind observations.  A decoded manifest is not graph-valid until
/// these actual launch inputs have been compared with it.
public struct ExecutionObservation {
    public let stageRoot: String; public let actualProfileUTF8: String; public let environment: [String]; public let installerCommand: ActualChildCommand; public let importCommand: ActualChildCommand; public let interpreterMachOObservation: MachOObservation; public let preInstallStagedMachOObservations: [StagedMachOObservation]; public let stagedMachOObservations: [StagedMachOObservation]
    public init(stageRoot: String, actualProfileUTF8: String, environment: [String], installerCommand: ActualChildCommand, importCommand: ActualChildCommand, interpreterMachOObservation: MachOObservation, preInstallStagedMachOObservations: [StagedMachOObservation]? = nil, stagedMachOObservations: [StagedMachOObservation]? = nil) throws {
        guard stageRoot.hasPrefix("/"), !stageRoot.hasSuffix("/"), !stageRoot.contains("\0"), !stageRoot.contains("..") else { throw EvidenceError.invalidValue }
        guard !environment.isEmpty else { throw EvidenceError.invalidValue }
        let interpreter = try StagedMachOObservation(path: interpreterMachOObservation.path, descriptorIdentitySHA256: installerCommand.interpreterSHA256, sha256: installerCommand.interpreterSHA256, codesignIdentitySHA256: installerCommand.interpreterCodesignIdentitySHA256, machOObservation: interpreterMachOObservation)
        let post = stagedMachOObservations ?? [interpreter]; let pre = preInstallStagedMachOObservations ?? post
        // Do this before Dictionary(uniqueKeysWithValues:): the latter traps on hostile duplicate
        // evidence. Both exact and case-fold collisions are invalid on the case-insensitive
        // authority filesystem.
        guard Set(post.map(\.path)).count == post.count,
              Set(post.map { $0.path.lowercased() }).count == post.count,
              Set(pre.map(\.path)).count == pre.count,
              Set(pre.map { $0.path.lowercased() }).count == pre.count else { throw EvidenceError.unorderedArray }
        let postByPath = Dictionary(uniqueKeysWithValues: post.map { ($0.path, $0) })
        guard pre == pre.sorted(by: { $0.path < $1.path }), post == post.sorted(by: { $0.path < $1.path }), pre.allSatisfy({ postByPath[$0.path] == $0 }), post.contains(where: { $0.path == interpreter.path && $0.machOObservation == interpreterMachOObservation }) else { throw EvidenceError.invalidValue }
        self.stageRoot = stageRoot; self.actualProfileUTF8 = actualProfileUTF8; self.environment = environment; self.installerCommand = installerCommand; self.importCommand = importCommand; self.interpreterMachOObservation = interpreterMachOObservation; self.preInstallStagedMachOObservations = pre; self.stagedMachOObservations = post
    }

}

/// Bounded data reported by the fixed import probe.  This is evidence, never
/// a runnable path capability: the held descriptor layout remains private to
/// the observer/capability boundary.
public struct ImportProbeObservation: Equatable {
    public let version: String; public let sysPath: [String]; public let moduleFiles: [String]; public let outputSHA256: SHA256Digest
}

public struct InstallationObservation {
    /// The complete observed Task 1A execution tuple.  Its stage spelling is
    /// evidence only; no held descriptor or raw execution authority escapes.
    public let executionObservation: ExecutionObservation
    public let probe: ImportProbeObservation
    public let postProbeMachOObservationSHA256: SHA256Digest
    public let postProbeDependencyClosureSHA256: SHA256Digest
    public init(executionObservation: ExecutionObservation, probe: ImportProbeObservation) throws {
        self.executionObservation = executionObservation; self.probe = probe
        postProbeMachOObservationSHA256 = try executionObservation.interpreterMachOObservation.sha256()
        let dependencies = try StagedMachOObservation.flattenedDependencies(executionObservation.stagedMachOObservations)
        postProbeDependencyClosureSHA256 = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", .array(dependencies.map(\.canonicalValue))))
    }
}

public struct MaterializationPolicy: Equatable {
    public let pythonVersion: String; public let wheelFilenames: [String]; public let pipArgv: [String]; public let importArgv: [String]; public let environment: [String]; public let timeoutSeconds: Int64; public let maxOutputBytes: Int64; public let importProbeSHA256: SHA256Digest; public let sandboxProfileUTF8: String; public let sandboxProfileTemplateSHA256: SHA256Digest; public let childCommandTemplateSHA256: SHA256Digest; public let sandboxLauncher: SandboxLauncher; public let baseInterpreter: BaseInterpreter; public let environmentSHA256: SHA256Digest; public let workingDirectory: String; public let sha256: SHA256Digest
    /// Kept as a source-compatible alias for pre-migration callers.  No actual
    /// profile exists before a descriptor-rebound stage path is known.
    public var sandboxProfileSHA256: SHA256Digest { sandboxProfileTemplateSHA256 }
    public static let stageToken = r4StageToken
    public static let importProbe = r4ImportProbe
    public static let sandboxProfile = r4SandboxProfile
    public func reboundEnvironment(stage: String) throws -> [String] { try rebound(environment, stage: stage) }
    public func reboundPipArgv(stage: String) throws -> [String] { try rebound(pipArgv, stage: stage) }
    public func reboundSandboxProfile(stage: String) throws -> String { try rebound([sandboxProfileUTF8], stage: stage)[0] }
    private func rebound(_ strings: [String], stage: String) throws -> [String] {
        guard stage.hasPrefix("/"), !stage.hasSuffix("/"), !stage.contains("\0"), !stage.contains(MaterializationPolicy.stageToken) else { throw EvidenceError.invalidValue }
        return strings.map { $0.replacingOccurrences(of: MaterializationPolicy.stageToken, with: stage) }
    }
    public static func decode(_ source: String) throws -> MaterializationPolicy {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","python_version","archive","wheel_filenames","sandbox_launcher","base_interpreter","child_execution","allowed_loader_roots","import_modules","import_probe_sha256","schema_versions","sha256"], "R4_PRIVILEGED_MATERIALIZATION_POLICY_V2")
        let pythonVersion = try text(d, "python_version"); guard pythonVersion.range(of: "^3\\.11\\.[0-9]+$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }
        let archive = try object(d["archive"]!, ["archive_root","final_runtime_path","entrypoint","max_members","max_unpacked_bytes"]); try require(text(archive, "archive_root"), "python"); try require(text(archive, "final_runtime_path"), "runtime/closure/python311"); try require(text(archive, "entrypoint"), "runtime/closure/python311/bin/python3.11"); guard try integer(archive, "max_members") == 100000, try integer(archive, "max_unpacked_bytes") == 8_589_934_592 else { throw EvidenceError.invalidValue }
        let wheels = try strings(d["wheel_filenames"]!); try orderedUniquePaths(wheels); guard !wheels.isEmpty else { throw EvidenceError.invalidValue }
        let launcher = try SandboxLauncher.decode(d["sandbox_launcher"]!); let interpreter = try BaseInterpreter.decode(d["base_interpreter"]!)
        let child = try object(d["child_execution"]!, ["cwd","environment","pip_argv","import_argv","timeout_seconds","max_output_bytes","sandbox_profile_utf8","sandbox_profile_template_sha256","child_command_template_sha256","read_allow","write_allow","network_denied"]); try require(text(child, "cwd"), "runtime")
        let environment = try strings(child["environment"]!); guard environment == v2Environment else { throw EvidenceError.invalidValue }; let environmentSHA = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_ENVIRONMENT\0", child["environment"]!))
        let profile = try text(child, "sandbox_profile_utf8"); guard profile == r4SandboxProfile else { throw EvidenceError.invalidValue }; let profileClaimed = try text(child, "sandbox_profile_template_sha256"); guard CanonicalJSON.sha256(Array("R4V2_SANDBOX_PROFILE_TEMPLATE\0".utf8) + Array(profile.utf8)) == profileClaimed else { throw EvidenceError.invalidDigest }; let profileSHA = try requiredSHA(child, "sandbox_profile_template_sha256")
        guard try integer(child, "timeout_seconds") == 300, try integer(child, "max_output_bytes") == 1_000_000, case .bool(true) = child["network_denied"] else { throw EvidenceError.invalidValue }
        guard try strings(child["read_allow"]!) == ["runtime/closure/python311","wheelhouse","home","tmp","/dev/null","/System","/usr/lib"], try strings(child["write_allow"]!) == ["runtime","home","tmp"] else { throw EvidenceError.invalidValue }
        let pip = try strings(child["pip_argv"]!); let expectedPip = ["-I","-s","-E","-m","pip","--isolated","install","--no-index","--no-deps","--only-binary=:all:","--no-cache-dir","--disable-pip-version-check"] + wheels.map { "__R4_STAGE__/wheelhouse/" + $0 }; guard pip == expectedPip else { throw EvidenceError.invalidValue }
        let importArgv = try rawStrings(child["import_argv"]!); let importProbe = try text(d, "import_probe_sha256"); guard importArgv == ["-I","-s","-E","-c", r4ImportProbe], CanonicalJSON.sha256(Array(r4ImportProbe.utf8)) == importProbe else { throw EvidenceError.invalidValue }
        guard try strings(d["allowed_loader_roots"]!) == ["runtime/closure/python311","/System/","/usr/lib/"], try strings(d["import_modules"]!) == ["cryptography","jsonschema","numpy","scipy"], try strings(d["schema_versions"]!) == ["R4_RUNTIME_INPUT_BUNDLE_V2","R4_PRIVILEGED_MATERIALIZATION_POLICY_V2","R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2","R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2","R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2","R4_HERMETIC_PYTHON311_RUNTIME_V2","R4_PROTECTED_EXECUTION_MANIFEST_V2","R4_PROTECTED_EXECUTION_RECEIPT_V2"] else { throw EvidenceError.invalidValue }
        let commandTemplate: JSONValue = .object([("launcher_path", .string(launcher.path)), ("pip_argv_template", .array(([launcher.path, "-p", profile, "__R4_STAGE__/runtime/closure/python311/bin/python3.11"] + pip).map(JSONValue.string))), ("import_argv_template", .array(([launcher.path, "-p", profile, "__R4_STAGE__/runtime/closure/python311/bin/python3.11"] + importArgv).map(JSONValue.string))), ("cwd", .string(try text(child, "cwd"))), ("environment", .array(environment.map(JSONValue.string))), ("stdin", .string("/dev/null")), ("stdout_stderr", .string("ROOT_OWNED_CAPPED_PIPES")), ("timeout_seconds", .integer(try integer(child, "timeout_seconds"))), ("max_output_bytes", .integer(try integer(child, "max_output_bytes"))), ("sandbox_profile_template_sha256", .string(profileSHA.value))])
        let commandTemplateSHA = try requiredSHA(child, "child_command_template_sha256"); guard try CanonicalJSON.domainSHA256("R4V2_CHILD_COMMAND_TEMPLATE\0", commandTemplate) == commandTemplateSHA.value else { throw EvidenceError.invalidDigest }
        return try MaterializationPolicy(pythonVersion: pythonVersion, wheelFilenames: wheels, pipArgv: pip, importArgv: importArgv, environment: environment, timeoutSeconds: integer(child, "timeout_seconds"), maxOutputBytes: integer(child, "max_output_bytes"), importProbeSHA256: requiredSHA(d, "import_probe_sha256"), sandboxProfileUTF8: profile, sandboxProfileTemplateSHA256: profileSHA, childCommandTemplateSHA256: commandTemplateSHA, sandboxLauncher: launcher, baseInterpreter: interpreter, environmentSHA256: environmentSHA, workingDirectory: text(child, "cwd"), sha256: requiredSHA(d, "sha256"))
    }
    static func decodeDocument(_ bytes: [UInt8]) throws -> MaterializationPolicy { try decode(String(decoding: try CanonicalJSON.encode(CanonicalJSON.decodeDocument(bytes: bytes)), as: UTF8.self)) }

    // Transitional fixture initializer used by the pre-admission test seam.
    // It cannot decode an external document and therefore creates no authority.
    public init(wheelFilenames: [String], pipArgv: [String], importArgv: [String], environment: [String], timeoutSeconds: Int64, maxOutputBytes: Int64, importProbeSHA256: SHA256Digest, sandboxProfileUTF8: String, sandboxProfileSHA256: SHA256Digest, environmentSHA256: SHA256Digest, workingDirectory: String, sha256: SHA256Digest) {
        let emptyIdentity = CodeSignIdentity(identifier: "fixture", designatedRequirement: "fixture", teamIdentifier: nil, certificateChainSHA256: sha256, sha256: sha256)
        self.pythonVersion = "3.11.0"; self.wheelFilenames = wheelFilenames; self.pipArgv = pipArgv; self.importArgv = importArgv; self.environment = environment; self.timeoutSeconds = timeoutSeconds; self.maxOutputBytes = maxOutputBytes; self.importProbeSHA256 = importProbeSHA256; self.sandboxProfileUTF8 = sandboxProfileUTF8; self.sandboxProfileTemplateSHA256 = sandboxProfileSHA256; self.childCommandTemplateSHA256 = sha256; self.sandboxLauncher = SandboxLauncher(path: "/usr/bin/sandbox-exec", descriptorIdentitySHA256: sha256, sha256: sha256, codesignIdentity: emptyIdentity); self.baseInterpreter = BaseInterpreter(path: "runtime/closure/python311/bin/python3.11", sha256: sha256, machOSHA256: sha256, codesignIdentity: emptyIdentity); self.environmentSHA256 = environmentSHA256; self.workingDirectory = workingDirectory; self.sha256 = sha256
    }
    public init(pythonVersion: String = "3.11.0", wheelFilenames: [String], pipArgv: [String], importArgv: [String], environment: [String], timeoutSeconds: Int64, maxOutputBytes: Int64, importProbeSHA256: SHA256Digest, sandboxProfileUTF8: String, sandboxProfileTemplateSHA256: SHA256Digest, childCommandTemplateSHA256: SHA256Digest, sandboxLauncher: SandboxLauncher, baseInterpreter: BaseInterpreter, environmentSHA256: SHA256Digest, workingDirectory: String, sha256: SHA256Digest) {
        self.pythonVersion = pythonVersion; self.wheelFilenames = wheelFilenames; self.pipArgv = pipArgv; self.importArgv = importArgv; self.environment = environment; self.timeoutSeconds = timeoutSeconds; self.maxOutputBytes = maxOutputBytes; self.importProbeSHA256 = importProbeSHA256; self.sandboxProfileUTF8 = sandboxProfileUTF8; self.sandboxProfileTemplateSHA256 = sandboxProfileTemplateSHA256; self.childCommandTemplateSHA256 = childCommandTemplateSHA256; self.sandboxLauncher = sandboxLauncher; self.baseInterpreter = baseInterpreter; self.environmentSHA256 = environmentSHA256; self.workingDirectory = workingDirectory; self.sha256 = sha256
    }
}

public struct AdmissionRecord: Equatable {
    public let attemptUUID: String; public let storageVolumeUUID: String; public let task14ApprovalSHA256: SHA256Digest; public let inputBundleManifestSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> AdmissionRecord {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","attempt_uuid","task14_approval_sha256","input_bundle_manifest_sha256","materialization_policy_sha256","storage_volume_uuid","sha256"], "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2")
        guard try text(d, "attempt_uuid").range(of: "^[0-9a-f]{32}$", options: .regularExpression) != nil, try text(d, "storage_volume_uuid").range(of: "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }
        return try AdmissionRecord(attemptUUID: text(d, "attempt_uuid"), storageVolumeUUID: text(d, "storage_volume_uuid"), task14ApprovalSHA256: requiredSHA(d, "task14_approval_sha256"), inputBundleManifestSHA256: requiredSHA(d, "input_bundle_manifest_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), sha256: requiredSHA(d, "sha256"))
    }
    public func canonicalRecordBytes() throws -> [UInt8] {
        let fields: [(String, JSONValue)] = [
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("attempt_uuid", .string(attemptUUID)),
            ("input_bundle_manifest_sha256", .string(inputBundleManifestSHA256.value)),
            ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2")),
            ("materialization_policy_sha256", .string(materializationPolicySHA256.value)),
            ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("storage_volume_uuid", .string(storageVolumeUUID)),
            ("task14_approval_sha256", .string(task14ApprovalSHA256.value))
        ]
        let bare = try CanonicalJSON.encode(.object(fields))
        guard CanonicalJSON.sha256(bare) == sha256.value else { throw EvidenceError.invalidDigest }
        return try CanonicalJSON.encode(.object(fields + [("sha256", .string(sha256.value))]))
    }
}

public struct RuntimeManifestV2: Equatable {
    public let lockSHA256: SHA256Digest; public let materializationInputSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let wheelSetSHA256: SHA256Digest; public let sandboxLauncherPath: String; public let sandboxLauncherDescriptorIdentitySHA256: SHA256Digest; public let sandboxLauncherSHA256: SHA256Digest; public let sandboxLauncherCodesignIdentitySHA256: SHA256Digest; public let sandboxProfileTemplateSHA256: SHA256Digest; public let childCommandTemplateSHA256: SHA256Digest; public let installerCommandSHA256: SHA256Digest; public let importCommandSHA256: SHA256Digest; public let interpreterPreInstallSHA256: SHA256Digest; public let interpreterPreInstallMachOSHA256: SHA256Digest; public let interpreterPreInstallCodesignIdentitySHA256: SHA256Digest; public let interpreterPostInstallSHA256: SHA256Digest; public let interpreterPostInstallMachOSHA256: SHA256Digest; public let interpreterPostInstallCodesignIdentitySHA256: SHA256Digest; public let importProbeSHA256: SHA256Digest; public let sandboxProfileSHA256: SHA256Digest; public let environmentSHA256: SHA256Digest; public let workingDirectory: String; public let entries: [ManifestEntry]; public let entriesSHA256: SHA256Digest; public let runtimeOutputClosureSHA256: SHA256Digest; public let machODependencies: [MachODependency]; public let dependencyClosureSHA256: SHA256Digest; public let preInstallStagedMachOObservations: [StagedMachOObservation]; public let preInstallStagedMachOObservationsSHA256: SHA256Digest; public let stagedMachOObservations: [StagedMachOObservation]; public let stagedMachOObservationsSHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> RuntimeManifestV2 {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","python_version","entrypoint","lock_sha256","materialization_input_sha256","materialization_policy_sha256","wheel_set_sha256","sandbox_launcher_path","sandbox_launcher_descriptor_identity_sha256","sandbox_launcher_sha256","sandbox_launcher_codesign_identity_sha256","sandbox_profile_template_sha256","sandbox_profile_sha256","child_command_template_sha256","installer_command_sha256","import_command_sha256","interpreter_pre_install_sha256","interpreter_pre_install_mach_o_sha256","interpreter_pre_install_codesign_identity_sha256","interpreter_post_install_sha256","interpreter_post_install_mach_o_sha256","interpreter_post_install_codesign_identity_sha256","import_probe_sha256","environment_sha256","working_directory","entries","entries_sha256","runtime_output_closure_sha256","mach_o_dependencies","dependency_closure_sha256","pre_install_staged_mach_o_observations","pre_install_staged_mach_o_observations_sha256","staged_mach_o_observations","staged_mach_o_observations_sha256","sha256"], "R4_HERMETIC_PYTHON311_RUNTIME_V2")
        guard try text(d, "python_version").range(of: "^3\\.11\\.[0-9]+$", options: .regularExpression) != nil else { throw EvidenceError.invalidValue }; try require(text(d, "entrypoint"), "runtime/closure/python311/bin/python3.11"); try require(text(d, "working_directory"), "runtime")
        let runtimeEntries = try v2Entries(d["entries"]!, prefix: "runtime"); let entriesDigest = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_ENTRIES\0", d["entries"]!)); let entriesClaim = try requiredSHA(d, "entries_sha256"); let closureClaim = try requiredSHA(d, "runtime_output_closure_sha256"); guard entriesDigest == entriesClaim, entriesDigest == closureClaim else { throw EvidenceError.invalidDigest }
        var tuples: [[String]] = []; var machODependencies: [MachODependency] = []; let regular = Set(runtimeEntries.filter { $0.type == "regular" }.map(\.path))
        for value in try array(d, "mach_o_dependencies") { let dep = try MachODependency.decode(value); guard regular.contains(dep.loader) else { throw EvidenceError.invalidValue }; try validRuntimeDependency(dep.dependency); if dep.resolved.hasPrefix("runtime/closure/python311/") { try validPath(String(dep.resolved.dropFirst("runtime/".count))) } else if dep.resolved.hasPrefix("/System/") { try validPath(String(dep.resolved.dropFirst("/System/".count))) } else if dep.resolved.hasPrefix("/usr/lib/") { try validPath(String(dep.resolved.dropFirst("/usr/lib/".count))) } else { throw EvidenceError.invalidValue }; tuples.append([dep.loader, dep.dependency, dep.resolved]); machODependencies.append(dep) }
        try orderedUniqueTuples(tuples); let depDigest = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", d["mach_o_dependencies"]!)); let depClaim = try requiredSHA(d, "dependency_closure_sha256"); guard depDigest == depClaim else { throw EvidenceError.invalidDigest }
        let preStaged = try StagedMachOObservation.decodeArray(d["pre_install_staged_mach_o_observations"]!); let preStagedDigest = try StagedMachOObservation.collectionSHA256(preStaged)
        let preStagedClaim = try requiredSHA(d, "pre_install_staged_mach_o_observations_sha256")
        guard preStagedDigest == preStagedClaim else { throw EvidenceError.invalidDigest }
        let staged = try StagedMachOObservation.decodeArray(d["staged_mach_o_observations"]!); let stagedDigest = try StagedMachOObservation.collectionSHA256(staged)
        let stagedClaim = try requiredSHA(d, "staged_mach_o_observations_sha256")
        let postByPath = Dictionary(uniqueKeysWithValues: staged.map { ($0.path, $0) })
        guard stagedDigest == stagedClaim, preStaged.allSatisfy({ postByPath[$0.path] == $0 }), try CanonicalJSON.encode(.array(StagedMachOObservation.flattenedDependencies(staged).map(\.canonicalValue))) == CanonicalJSON.encode(d["mach_o_dependencies"]!) else { throw EvidenceError.invalidDigest }
        try require(text(d, "sandbox_launcher_path"), "/usr/bin/sandbox-exec"); try standardHashFields(d, ["lock_sha256","sandbox_launcher_descriptor_identity_sha256","sandbox_launcher_sha256","sandbox_launcher_codesign_identity_sha256","sandbox_profile_template_sha256","installer_command_sha256","import_command_sha256","interpreter_pre_install_sha256","interpreter_pre_install_mach_o_sha256","interpreter_pre_install_codesign_identity_sha256","interpreter_post_install_sha256","interpreter_post_install_mach_o_sha256","interpreter_post_install_codesign_identity_sha256","import_probe_sha256","sandbox_profile_sha256","environment_sha256"])
        return try RuntimeManifestV2(lockSHA256: requiredSHA(d, "lock_sha256"), materializationInputSHA256: requiredSHA(d, "materialization_input_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), wheelSetSHA256: requiredSHA(d, "wheel_set_sha256"), sandboxLauncherPath: text(d, "sandbox_launcher_path"), sandboxLauncherDescriptorIdentitySHA256: requiredSHA(d, "sandbox_launcher_descriptor_identity_sha256"), sandboxLauncherSHA256: requiredSHA(d, "sandbox_launcher_sha256"), sandboxLauncherCodesignIdentitySHA256: requiredSHA(d, "sandbox_launcher_codesign_identity_sha256"), sandboxProfileTemplateSHA256: requiredSHA(d, "sandbox_profile_template_sha256"), childCommandTemplateSHA256: requiredSHA(d, "child_command_template_sha256"), installerCommandSHA256: requiredSHA(d, "installer_command_sha256"), importCommandSHA256: requiredSHA(d, "import_command_sha256"), interpreterPreInstallSHA256: requiredSHA(d, "interpreter_pre_install_sha256"), interpreterPreInstallMachOSHA256: requiredSHA(d, "interpreter_pre_install_mach_o_sha256"), interpreterPreInstallCodesignIdentitySHA256: requiredSHA(d, "interpreter_pre_install_codesign_identity_sha256"), interpreterPostInstallSHA256: requiredSHA(d, "interpreter_post_install_sha256"), interpreterPostInstallMachOSHA256: requiredSHA(d, "interpreter_post_install_mach_o_sha256"), interpreterPostInstallCodesignIdentitySHA256: requiredSHA(d, "interpreter_post_install_codesign_identity_sha256"), importProbeSHA256: requiredSHA(d, "import_probe_sha256"), sandboxProfileSHA256: requiredSHA(d, "sandbox_profile_sha256"), environmentSHA256: requiredSHA(d, "environment_sha256"), workingDirectory: text(d, "working_directory"), entries: runtimeEntries, entriesSHA256: entriesDigest, runtimeOutputClosureSHA256: requiredSHA(d, "runtime_output_closure_sha256"), machODependencies: machODependencies, dependencyClosureSHA256: depDigest, preInstallStagedMachOObservations: preStaged, preInstallStagedMachOObservationsSHA256: preStagedDigest, stagedMachOObservations: staged, stagedMachOObservationsSHA256: stagedDigest, sha256: requiredSHA(d, "sha256"))
    }
    public static func decode(_ source: String, policy: MaterializationPolicy) throws -> RuntimeManifestV2 {
        let runtime = try decode(source)
        guard runtime.materializationPolicySHA256 == policy.sha256, runtime.importProbeSHA256 == policy.importProbeSHA256, runtime.sandboxLauncherPath == policy.sandboxLauncher.path, runtime.sandboxLauncherDescriptorIdentitySHA256 == policy.sandboxLauncher.descriptorIdentitySHA256, runtime.sandboxLauncherSHA256 == policy.sandboxLauncher.sha256, runtime.sandboxLauncherCodesignIdentitySHA256 == policy.sandboxLauncher.codesignIdentity.sha256, runtime.sandboxProfileTemplateSHA256 == policy.sandboxProfileTemplateSHA256, runtime.childCommandTemplateSHA256 == policy.childCommandTemplateSHA256, runtime.interpreterPreInstallSHA256 == policy.baseInterpreter.sha256, runtime.interpreterPreInstallMachOSHA256 == policy.baseInterpreter.machOSHA256, runtime.interpreterPreInstallCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256, runtime.workingDirectory == policy.workingDirectory else { throw EvidenceError.invalidDigest }
        return runtime
    }
    /// Validates the complete post-install staged closure.  This overload has
    /// no interpreter-only fallback: accepting a partial graph would silently
    /// omit wheel extensions and dylibs from the runtime dependency evidence.
    public static func decode(_ source: String, policy: MaterializationPolicy, actualProfileUTF8: String, installerCommand: ActualChildCommand, importCommand: ActualChildCommand, interpreterMachOObservation: MachOObservation, stagedMachOObservations: [StagedMachOObservation]) throws -> RuntimeManifestV2 {
        let runtime = try decode(source, policy: policy)
        let actualProfile = try ActualSandboxProfile.sha256(actualProfileUTF8)
        let machO = try interpreterMachOObservation.sha256()
        let dependenciesValue = JSONValue.array(try StagedMachOObservation.flattenedDependencies(stagedMachOObservations).map(\.canonicalValue))
        let dependencies = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", dependenciesValue))
        let stagedDigest = try StagedMachOObservation.collectionSHA256(stagedMachOObservations)
        let installerDigest = try installerCommand.installCommandSHA256()
        let importDigest = try importCommand.importCommandSHA256()
        guard runtime.sandboxProfileSHA256 == actualProfile,
              runtime.installerCommandSHA256 == installerDigest,
              runtime.importCommandSHA256 == importDigest,
              installerCommand.sandboxProfileSHA256 == actualProfile,
              importCommand.sandboxProfileSHA256 == actualProfile,
              runtime.stagedMachOObservations == stagedMachOObservations,
              runtime.stagedMachOObservationsSHA256 == stagedDigest,
              try CanonicalJSON.encode(.array(runtime.machODependencies.map(\.canonicalValue))) == CanonicalJSON.encode(dependenciesValue),
              runtime.dependencyClosureSHA256 == dependencies,
              runtime.interpreterPreInstallMachOSHA256 == machO,
              runtime.interpreterPostInstallMachOSHA256 == machO else { throw EvidenceError.invalidDigest }
        return runtime
    }
}

public struct AuthorityManifestV2: Equatable {
    public let git: GitEvidence; public let entries: [ManifestEntry]; public let runtimeManifestSHA256: SHA256Digest; public let inputBundleManifestSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> AuthorityManifestV2 {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","git","entries","runtime_manifest_sha256","input_bundle_manifest_sha256","materialization_policy_sha256","sha256"], "R4_PROTECTED_EXECUTION_MANIFEST_V2")
        return try AuthorityManifestV2(git: v2Git(d["git"]!), entries: v2Entries(d["entries"]!, prefix: "checkout"), runtimeManifestSHA256: requiredSHA(d, "runtime_manifest_sha256"), inputBundleManifestSHA256: requiredSHA(d, "input_bundle_manifest_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), sha256: requiredSHA(d, "sha256"))
    }
}

public struct Task14ReviewPackageV2: Equatable {
    public let reviewedHead: GitObjectID; public let reviewedTree: GitObjectID; public let requiredBlobSetSHA256: SHA256Digest; public let exactSuiteLogSHA256: SHA256Digest; public let inputBundleManifestSHA256: SHA256Digest; public let lockSHA256: SHA256Digest; public let pythonArchiveSHA256: SHA256Digest; public let wheelSetSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let provisionerSwiftSourceSHA256: SHA256Digest; public let provisionerBinarySHA256: SHA256Digest; public let provisionerDesignatedRequirementSHA256: SHA256Digest; public let helperTeamID: String; public let helperSigningIdentitySHA1: String; public let task14ApprovalVerificationPublicKeySHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> Task14ReviewPackageV2 {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","reviewed_head","reviewed_tree","required_blob_set_sha256","exact_suite_log_sha256","input_bundle_manifest_sha256","lock_sha256","python_archive_sha256","wheel_set_sha256","materialization_policy_sha256","provisioner_swift_source_sha256","provisioner_binary_sha256","provisioner_designated_requirement_sha256","helper_hashes","helper_team_id","helper_signing_identity_sha1","task14_approval_verification_public_key_sha256","sha256"], "R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2")
        let helpers = try array(d, "helper_hashes").map(HelperHash.decode); try orderedUniquePaths(helpers.map(\.path)); guard helpers.map(\.path) == ["bootstrap/provisioner","bootstrap/provisioner.codesign-requirement.txt"] else { throw EvidenceError.invalidValue }
        let binary = try requiredSHA(d, "provisioner_binary_sha256"); let requirement = try requiredSHA(d, "provisioner_designated_requirement_sha256"); guard helpers[0].sha256 == binary, helpers[1].sha256 == requirement, try text(d, "helper_team_id") == "3BMF4LM6TM", try text(d, "helper_signing_identity_sha1") == "4C6503D0A78A9F51C86CC7C9683115E74A4902A7" else { throw EvidenceError.invalidValue }
        let key = try requiredSHA(d, "task14_approval_verification_public_key_sha256"); guard key.value == ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256 else { throw EvidenceError.invalidValue }
        return try Task14ReviewPackageV2(reviewedHead: GitObjectID(text(d, "reviewed_head")), reviewedTree: GitObjectID(text(d, "reviewed_tree")), requiredBlobSetSHA256: requiredSHA(d, "required_blob_set_sha256"), exactSuiteLogSHA256: requiredSHA(d, "exact_suite_log_sha256"), inputBundleManifestSHA256: requiredSHA(d, "input_bundle_manifest_sha256"), lockSHA256: requiredSHA(d, "lock_sha256"), pythonArchiveSHA256: requiredSHA(d, "python_archive_sha256"), wheelSetSHA256: requiredSHA(d, "wheel_set_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), provisionerSwiftSourceSHA256: requiredSHA(d, "provisioner_swift_source_sha256"), provisionerBinarySHA256: binary, provisionerDesignatedRequirementSHA256: requirement, helperTeamID: text(d, "helper_team_id"), helperSigningIdentitySHA1: text(d, "helper_signing_identity_sha1"), task14ApprovalVerificationPublicKeySHA256: key, sha256: requiredSHA(d, "sha256"))
    }
}

public struct Task14ObservationAuthorizationV2: Equatable {
    public let reviewPackageSHA256: SHA256Digest; public let inputBundleManifestSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let provisionerBinarySHA256: SHA256Digest; public let provisionerDesignatedRequirementSHA256: SHA256Digest; public let task14ApprovalVerificationPublicKeySHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> Task14ObservationAuthorizationV2 {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","review_package_sha256","input_bundle_manifest_sha256","materialization_policy_sha256","provisioner_binary_sha256","provisioner_designated_requirement_sha256","task14_approval_verification_public_key_sha256","sha256"], "R4_TASK14_PRIVILEGED_MATERIALIZATION_OBSERVATION_AUTHORIZATION_V2")
        let key = try requiredSHA(d, "task14_approval_verification_public_key_sha256"); guard key.value == ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256 else { throw EvidenceError.invalidValue }
        return try Task14ObservationAuthorizationV2(reviewPackageSHA256: requiredSHA(d, "review_package_sha256"), inputBundleManifestSHA256: requiredSHA(d, "input_bundle_manifest_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), provisionerBinarySHA256: requiredSHA(d, "provisioner_binary_sha256"), provisionerDesignatedRequirementSHA256: requiredSHA(d, "provisioner_designated_requirement_sha256"), task14ApprovalVerificationPublicKeySHA256: key, sha256: requiredSHA(d, "sha256"))
    }
}

public struct Task14ApprovalV2: Equatable {
    public let reviewedHead: GitObjectID; public let reviewedTree: GitObjectID; public let requiredBlobSetSHA256: SHA256Digest; public let reviewPackageSHA256: SHA256Digest; public let observationAuthorizationSHA256: SHA256Digest; public let task14ApprovalVerificationPublicKeySHA256: SHA256Digest; public let inputBundleManifestSHA256: SHA256Digest; public let lockSHA256: SHA256Digest; public let pythonArchiveSHA256: SHA256Digest; public let wheelSetSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let sandboxLauncherPath: String; public let sandboxLauncherDescriptorIdentitySHA256: SHA256Digest; public let sandboxLauncherSHA256: SHA256Digest; public let sandboxLauncherCodesignIdentitySHA256: SHA256Digest; public let sandboxProfileTemplateSHA256: SHA256Digest; public let childCommandTemplateSHA256: SHA256Digest; public let baseInterpreterPath: String; public let baseInterpreterSHA256: SHA256Digest; public let baseInterpreterMachOSHA256: SHA256Digest; public let baseInterpreterCodesignIdentitySHA256: SHA256Digest; public let provisionerBinarySHA256: SHA256Digest; public let provisionerDesignatedRequirementSHA256: SHA256Digest; public let helperTeamID: String; public let helperSigningIdentitySHA1: String; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> Task14ApprovalV2 {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","reviewed_head","reviewed_tree","required_blob_set_sha256","exact_suite_log_sha256","review_package_sha256","observation_authorization_sha256","task14_approval_verification_public_key_sha256","input_bundle_manifest_sha256","lock_sha256","python_archive_sha256","wheel_set_sha256","materialization_policy_sha256","sandbox_launcher_path","sandbox_launcher_descriptor_identity_sha256","sandbox_launcher_sha256","sandbox_launcher_codesign_identity_sha256","sandbox_profile_template_sha256","child_command_template_sha256","base_interpreter_path","base_interpreter_sha256","base_interpreter_mach_o_sha256","base_interpreter_codesign_identity_sha256","provisioner_swift_source_sha256","provisioner_binary_sha256","provisioner_designated_requirement_sha256","helper_hashes","helper_team_id","helper_signing_identity_sha1","sha256"], "R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2")
        let helpers = try array(d, "helper_hashes").map(HelperHash.decode); try orderedUniquePaths(helpers.map(\.path)); guard helpers.map(\.path) == ["bootstrap/provisioner","bootstrap/provisioner.codesign-requirement.txt"] else { throw EvidenceError.invalidValue }
        try require(text(d, "helper_team_id"), "3BMF4LM6TM"); try require(text(d, "helper_signing_identity_sha1"), "4C6503D0A78A9F51C86CC7C9683115E74A4902A7")
        let binary = try requiredSHA(d, "provisioner_binary_sha256"); let requirement = try requiredSHA(d, "provisioner_designated_requirement_sha256"); guard helpers[0].sha256 == binary, helpers[1].sha256 == requirement else { throw EvidenceError.invalidValue }
        try standardHashFields(d, ["exact_suite_log_sha256","python_archive_sha256","provisioner_swift_source_sha256"]); let key = try requiredSHA(d, "task14_approval_verification_public_key_sha256"); guard key.value == ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256, try text(d, "sandbox_launcher_path") == "/usr/bin/sandbox-exec", try text(d, "base_interpreter_path") == "runtime/closure/python311/bin/python3.11" else { throw EvidenceError.invalidValue }
        return try Task14ApprovalV2(reviewedHead: GitObjectID(text(d, "reviewed_head")), reviewedTree: GitObjectID(text(d, "reviewed_tree")), requiredBlobSetSHA256: requiredSHA(d, "required_blob_set_sha256"), reviewPackageSHA256: requiredSHA(d, "review_package_sha256"), observationAuthorizationSHA256: requiredSHA(d, "observation_authorization_sha256"), task14ApprovalVerificationPublicKeySHA256: key, inputBundleManifestSHA256: requiredSHA(d, "input_bundle_manifest_sha256"), lockSHA256: requiredSHA(d, "lock_sha256"), pythonArchiveSHA256: requiredSHA(d, "python_archive_sha256"), wheelSetSHA256: requiredSHA(d, "wheel_set_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), sandboxLauncherPath: text(d, "sandbox_launcher_path"), sandboxLauncherDescriptorIdentitySHA256: requiredSHA(d, "sandbox_launcher_descriptor_identity_sha256"), sandboxLauncherSHA256: requiredSHA(d, "sandbox_launcher_sha256"), sandboxLauncherCodesignIdentitySHA256: requiredSHA(d, "sandbox_launcher_codesign_identity_sha256"), sandboxProfileTemplateSHA256: requiredSHA(d, "sandbox_profile_template_sha256"), childCommandTemplateSHA256: requiredSHA(d, "child_command_template_sha256"), baseInterpreterPath: text(d, "base_interpreter_path"), baseInterpreterSHA256: requiredSHA(d, "base_interpreter_sha256"), baseInterpreterMachOSHA256: requiredSHA(d, "base_interpreter_mach_o_sha256"), baseInterpreterCodesignIdentitySHA256: requiredSHA(d, "base_interpreter_codesign_identity_sha256"), provisionerBinarySHA256: binary, provisionerDesignatedRequirementSHA256: requirement, helperTeamID: text(d, "helper_team_id"), helperSigningIdentitySHA1: text(d, "helper_signing_identity_sha1"), sha256: requiredSHA(d, "sha256"))
    }
    static func decodeDocument(_ bytes: [UInt8]) throws -> Task14ApprovalV2 { try decode(String(decoding: try CanonicalJSON.encode(CanonicalJSON.decodeDocument(bytes: bytes)), as: UTF8.self)) }
    // Transitional fixture-only initializer retained for existing pre-admission
    // seams. External approval documents always pass through `decode` above.
    public init(reviewedHead: GitObjectID, reviewedTree: GitObjectID, requiredBlobSetSHA256: SHA256Digest, inputBundleManifestSHA256: SHA256Digest, lockSHA256: SHA256Digest, pythonArchiveSHA256: SHA256Digest, wheelSetSHA256: SHA256Digest, materializationPolicySHA256: SHA256Digest, provisionerBinarySHA256: SHA256Digest, provisionerDesignatedRequirementSHA256: SHA256Digest, helperTeamID: String, helperSigningIdentitySHA1: String, sha256: SHA256Digest) {
        self.reviewedHead = reviewedHead; self.reviewedTree = reviewedTree; self.requiredBlobSetSHA256 = requiredBlobSetSHA256; self.reviewPackageSHA256 = sha256; self.observationAuthorizationSHA256 = sha256; self.task14ApprovalVerificationPublicKeySHA256 = sha256; self.inputBundleManifestSHA256 = inputBundleManifestSHA256; self.lockSHA256 = lockSHA256; self.pythonArchiveSHA256 = pythonArchiveSHA256; self.wheelSetSHA256 = wheelSetSHA256; self.materializationPolicySHA256 = materializationPolicySHA256; self.sandboxLauncherPath = "/usr/bin/sandbox-exec"; self.sandboxLauncherDescriptorIdentitySHA256 = sha256; self.sandboxLauncherSHA256 = sha256; self.sandboxLauncherCodesignIdentitySHA256 = sha256; self.sandboxProfileTemplateSHA256 = sha256; self.childCommandTemplateSHA256 = sha256; self.baseInterpreterPath = "runtime/closure/python311/bin/python3.11"; self.baseInterpreterSHA256 = sha256; self.baseInterpreterMachOSHA256 = sha256; self.baseInterpreterCodesignIdentitySHA256 = sha256; self.provisionerBinarySHA256 = provisionerBinarySHA256; self.provisionerDesignatedRequirementSHA256 = provisionerDesignatedRequirementSHA256; self.helperTeamID = helperTeamID; self.helperSigningIdentitySHA1 = helperSigningIdentitySHA1; self.sha256 = sha256
    }
    public init(reviewedHead: GitObjectID, reviewedTree: GitObjectID, requiredBlobSetSHA256: SHA256Digest, reviewPackageSHA256: SHA256Digest, observationAuthorizationSHA256: SHA256Digest, task14ApprovalVerificationPublicKeySHA256: SHA256Digest, inputBundleManifestSHA256: SHA256Digest, lockSHA256: SHA256Digest, pythonArchiveSHA256: SHA256Digest, wheelSetSHA256: SHA256Digest, materializationPolicySHA256: SHA256Digest, sandboxLauncherPath: String, sandboxLauncherDescriptorIdentitySHA256: SHA256Digest, sandboxLauncherSHA256: SHA256Digest, sandboxLauncherCodesignIdentitySHA256: SHA256Digest, sandboxProfileTemplateSHA256: SHA256Digest, childCommandTemplateSHA256: SHA256Digest, baseInterpreterPath: String, baseInterpreterSHA256: SHA256Digest, baseInterpreterMachOSHA256: SHA256Digest, baseInterpreterCodesignIdentitySHA256: SHA256Digest, provisionerBinarySHA256: SHA256Digest, provisionerDesignatedRequirementSHA256: SHA256Digest, helperTeamID: String, helperSigningIdentitySHA1: String, sha256: SHA256Digest) {
        self.reviewedHead = reviewedHead; self.reviewedTree = reviewedTree; self.requiredBlobSetSHA256 = requiredBlobSetSHA256; self.reviewPackageSHA256 = reviewPackageSHA256; self.observationAuthorizationSHA256 = observationAuthorizationSHA256; self.task14ApprovalVerificationPublicKeySHA256 = task14ApprovalVerificationPublicKeySHA256; self.inputBundleManifestSHA256 = inputBundleManifestSHA256; self.lockSHA256 = lockSHA256; self.pythonArchiveSHA256 = pythonArchiveSHA256; self.wheelSetSHA256 = wheelSetSHA256; self.materializationPolicySHA256 = materializationPolicySHA256; self.sandboxLauncherPath = sandboxLauncherPath; self.sandboxLauncherDescriptorIdentitySHA256 = sandboxLauncherDescriptorIdentitySHA256; self.sandboxLauncherSHA256 = sandboxLauncherSHA256; self.sandboxLauncherCodesignIdentitySHA256 = sandboxLauncherCodesignIdentitySHA256; self.sandboxProfileTemplateSHA256 = sandboxProfileTemplateSHA256; self.childCommandTemplateSHA256 = childCommandTemplateSHA256; self.baseInterpreterPath = baseInterpreterPath; self.baseInterpreterSHA256 = baseInterpreterSHA256; self.baseInterpreterMachOSHA256 = baseInterpreterMachOSHA256; self.baseInterpreterCodesignIdentitySHA256 = baseInterpreterCodesignIdentitySHA256; self.provisionerBinarySHA256 = provisionerBinarySHA256; self.provisionerDesignatedRequirementSHA256 = provisionerDesignatedRequirementSHA256; self.helperTeamID = helperTeamID; self.helperSigningIdentitySHA1 = helperSigningIdentitySHA1; self.sha256 = sha256
    }
}

public struct ReceiptV2: Equatable {
    public let keyGeneration: Int64; public let publicKeySHA256: SHA256Digest; public let manifestSHA256: SHA256Digest; public let runtimeManifestSHA256: SHA256Digest; public let task14ApprovalSHA256: SHA256Digest; public let inputBundleManifestSHA256: SHA256Digest; public let lockSHA256: SHA256Digest; public let materializationPolicySHA256: SHA256Digest; public let wheelSetSHA256: SHA256Digest; public let runtimeOutputClosureSHA256: SHA256Digest; public let dependencyClosureSHA256: SHA256Digest; public let preInstallStagedMachOObservationsSHA256: SHA256Digest; public let stagedMachOObservationsSHA256: SHA256Digest; public let admissionRecordSHA256: SHA256Digest; public let gitHead: GitObjectID; public let gitTree: GitObjectID; public let requiredBlobSetSHA256: SHA256Digest; public let provisionerBinarySHA256: SHA256Digest; public let helperCodesignRequirementSHA256: SHA256Digest; public let helperTeamID: String; public let helperSigningIdentitySHA1: String; public let publishedClosureSHA256: SHA256Digest; public let sandboxLauncherPath: String; public let sandboxLauncherDescriptorIdentitySHA256: SHA256Digest; public let sandboxLauncherSHA256: SHA256Digest; public let sandboxLauncherCodesignIdentitySHA256: SHA256Digest; public let sandboxProfileTemplateSHA256: SHA256Digest; public let sandboxProfileSHA256: SHA256Digest; public let childCommandTemplateSHA256: SHA256Digest; public let installerCommandSHA256: SHA256Digest; public let importCommandSHA256: SHA256Digest; public let interpreterPreInstallSHA256: SHA256Digest; public let interpreterPreInstallMachOSHA256: SHA256Digest; public let interpreterPreInstallCodesignIdentitySHA256: SHA256Digest; public let interpreterPostInstallSHA256: SHA256Digest; public let interpreterPostInstallMachOSHA256: SHA256Digest; public let interpreterPostInstallCodesignIdentitySHA256: SHA256Digest; public let environmentSHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ source: String) throws -> ReceiptV2 {
        let v = try decoded(source); let d = try v2Header(v, ["kind","namespace","authority_id","state","key_generation","manifest_sha256","runtime_manifest_sha256","task14_approval_sha256","input_bundle_manifest_sha256","lock_sha256","materialization_policy_sha256","wheel_set_sha256","runtime_output_closure_sha256","dependency_closure_sha256","pre_install_staged_mach_o_observations_sha256","staged_mach_o_observations_sha256","admission_record_sha256","git_head","git_tree","required_blob_set_sha256","provisioner_binary_sha256","helper_codesign_requirement_sha256","helper_team_id","helper_signing_identity_sha1","public_key_sha256","published_closure_sha256","sandbox_launcher_path","sandbox_launcher_descriptor_identity_sha256","sandbox_launcher_sha256","sandbox_launcher_codesign_identity_sha256","sandbox_profile_template_sha256","sandbox_profile_sha256","child_command_template_sha256","installer_command_sha256","import_command_sha256","interpreter_pre_install_sha256","interpreter_pre_install_mach_o_sha256","interpreter_pre_install_codesign_identity_sha256","interpreter_post_install_sha256","interpreter_post_install_mach_o_sha256","interpreter_post_install_codesign_identity_sha256","environment_sha256","sha256"], "R4_PROTECTED_EXECUTION_RECEIPT_V2")
        try require(text(d, "state"), "SEALED"); guard try integer(d, "key_generation") > 0 else { throw EvidenceError.invalidValue }; _ = try GitObjectID(text(d, "git_head")); _ = try GitObjectID(text(d, "git_tree")); try require(text(d, "helper_team_id"), "3BMF4LM6TM"); try require(text(d, "helper_signing_identity_sha1"), "4C6503D0A78A9F51C86CC7C9683115E74A4902A7")
        try require(text(d, "sandbox_launcher_path"), "/usr/bin/sandbox-exec"); try standardHashFields(d, ["required_blob_set_sha256","provisioner_binary_sha256","helper_codesign_requirement_sha256","public_key_sha256","published_closure_sha256","sandbox_launcher_descriptor_identity_sha256","sandbox_launcher_sha256","sandbox_launcher_codesign_identity_sha256","sandbox_profile_template_sha256","sandbox_profile_sha256","child_command_template_sha256","installer_command_sha256","import_command_sha256","interpreter_pre_install_sha256","interpreter_pre_install_mach_o_sha256","interpreter_pre_install_codesign_identity_sha256","interpreter_post_install_sha256","interpreter_post_install_mach_o_sha256","interpreter_post_install_codesign_identity_sha256","environment_sha256"])
        return try ReceiptV2(keyGeneration: integer(d, "key_generation"), publicKeySHA256: requiredSHA(d, "public_key_sha256"), manifestSHA256: requiredSHA(d, "manifest_sha256"), runtimeManifestSHA256: requiredSHA(d, "runtime_manifest_sha256"), task14ApprovalSHA256: requiredSHA(d, "task14_approval_sha256"), inputBundleManifestSHA256: requiredSHA(d, "input_bundle_manifest_sha256"), lockSHA256: requiredSHA(d, "lock_sha256"), materializationPolicySHA256: requiredSHA(d, "materialization_policy_sha256"), wheelSetSHA256: requiredSHA(d, "wheel_set_sha256"), runtimeOutputClosureSHA256: requiredSHA(d, "runtime_output_closure_sha256"), dependencyClosureSHA256: requiredSHA(d, "dependency_closure_sha256"), preInstallStagedMachOObservationsSHA256: requiredSHA(d, "pre_install_staged_mach_o_observations_sha256"), stagedMachOObservationsSHA256: requiredSHA(d, "staged_mach_o_observations_sha256"), admissionRecordSHA256: requiredSHA(d, "admission_record_sha256"), gitHead: GitObjectID(text(d, "git_head")), gitTree: GitObjectID(text(d, "git_tree")), requiredBlobSetSHA256: requiredSHA(d, "required_blob_set_sha256"), provisionerBinarySHA256: requiredSHA(d, "provisioner_binary_sha256"), helperCodesignRequirementSHA256: requiredSHA(d, "helper_codesign_requirement_sha256"), helperTeamID: text(d, "helper_team_id"), helperSigningIdentitySHA1: text(d, "helper_signing_identity_sha1"), publishedClosureSHA256: requiredSHA(d, "published_closure_sha256"), sandboxLauncherPath: text(d, "sandbox_launcher_path"), sandboxLauncherDescriptorIdentitySHA256: requiredSHA(d, "sandbox_launcher_descriptor_identity_sha256"), sandboxLauncherSHA256: requiredSHA(d, "sandbox_launcher_sha256"), sandboxLauncherCodesignIdentitySHA256: requiredSHA(d, "sandbox_launcher_codesign_identity_sha256"), sandboxProfileTemplateSHA256: requiredSHA(d, "sandbox_profile_template_sha256"), sandboxProfileSHA256: requiredSHA(d, "sandbox_profile_sha256"), childCommandTemplateSHA256: requiredSHA(d, "child_command_template_sha256"), installerCommandSHA256: requiredSHA(d, "installer_command_sha256"), importCommandSHA256: requiredSHA(d, "import_command_sha256"), interpreterPreInstallSHA256: requiredSHA(d, "interpreter_pre_install_sha256"), interpreterPreInstallMachOSHA256: requiredSHA(d, "interpreter_pre_install_mach_o_sha256"), interpreterPreInstallCodesignIdentitySHA256: requiredSHA(d, "interpreter_pre_install_codesign_identity_sha256"), interpreterPostInstallSHA256: requiredSHA(d, "interpreter_post_install_sha256"), interpreterPostInstallMachOSHA256: requiredSHA(d, "interpreter_post_install_mach_o_sha256"), interpreterPostInstallCodesignIdentitySHA256: requiredSHA(d, "interpreter_post_install_codesign_identity_sha256"), environmentSHA256: requiredSHA(d, "environment_sha256"), sha256: requiredSHA(d, "sha256"))
    }
    public static func receiptFileSHA256(_ bytes: [UInt8]) -> SHA256Digest { try! SHA256Digest(CanonicalJSON.sha256(bytes)) }
}

public struct AdmissionLocatorV2: Equatable {
    public let attemptUUID: String; public let admissionRecordSHA256: SHA256Digest; public let registryRelativePath: String; public let locatorSHA256: SHA256Digest; public let sha256: SHA256Digest
    public static func decode(_ bytes: [UInt8]) throws -> AdmissionLocatorV2 {
        let value = try CanonicalJSON.decode(bytes: bytes); try selfDigest(value)
        let d = try object(value, ["admission_record_sha256", "attempt_uuid", "authority_id", "kind", "namespace", "registry_relative_path", "locator_sha256", "sha256"])
        try require(text(d, "authority_id"), ProtectedExecutionConstants.authorityID); try require(text(d, "kind"), "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2"); try require(text(d, "namespace"), ProtectedExecutionConstants.namespace)
        let attempt = try text(d, "attempt_uuid"); try validAttemptUUID(attempt)
        let path = try text(d, "registry_relative_path"); guard path == "control/admissions/\(attempt).json" else { throw EvidenceError.invalidValue }
        let admission = try requiredSHA(d, "admission_record_sha256"); let locator = try requiredSHA(d, "locator_sha256")
        let core: JSONValue = .object([("admission_record_sha256", .string(admission.value)), ("attempt_uuid", .string(attempt)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("registry_relative_path", .string(path))])
        guard CanonicalJSON.sha256(Array("R4ADMISSIONLOCATOR2\0".utf8) + (try CanonicalJSON.encode(core))) == locator.value else { throw EvidenceError.invalidDigest }
        return try AdmissionLocatorV2(attemptUUID: attempt, admissionRecordSHA256: admission, registryRelativePath: path, locatorSHA256: locator, sha256: requiredSHA(d, "sha256"))
    }
    public static func decode(_ source: String) throws -> AdmissionLocatorV2 { try decode(Array(source.utf8)) }
    public func locatorPreimage() throws -> [UInt8] { Array("R4ADMISSIONLOCATOR2\0".utf8) + (try CanonicalJSON.encode(.object([("admission_record_sha256", .string(admissionRecordSHA256.value)), ("attempt_uuid", .string(attemptUUID)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("registry_relative_path", .string(registryRelativePath))]))) }
    public func canonicalRecordBytes() throws -> [UInt8] {
        let preimage = try locatorPreimage(); let core = try CanonicalJSON.decode(bytes: Array(preimage.dropFirst("R4ADMISSIONLOCATOR2\0".utf8.count)))
        guard case .object(let fields) = core, CanonicalJSON.sha256(preimage) == locatorSHA256.value else { throw EvidenceError.invalidDigest }
        let withLocator = fields + [("locator_sha256", .string(locatorSHA256.value))]
        guard CanonicalJSON.sha256(try CanonicalJSON.encode(.object(withLocator))) == sha256.value else { throw EvidenceError.invalidDigest }
        return try CanonicalJSON.encode(.object(withLocator + [("sha256", .string(sha256.value))]))
    }
}

public enum JournalPhase: String, Equatable { case stageCreated = "STAGE_CREATED", presealed = "PRESEALED", renameIntent = "RENAME_INTENT", published = "PUBLISHED", quarantined = "QUARANTINED", publishIndeterminate = "PUBLISH_INDETERMINATE" }
public enum JournalFailureClass: String, Equatable { case preRenameIO = "PRE_RENAME_IO", preRenameRebind = "PRE_RENAME_REBIND", preRenameSeal = "PRE_RENAME_SEAL", preRenameDurability = "PRE_RENAME_DURABILITY", postRenameRebind = "POST_RENAME_REBIND", postRenameSeal = "POST_RENAME_SEAL", postRenameDurability = "POST_RENAME_DURABILITY", postRenameJournal = "POST_RENAME_JOURNAL" }

public struct JournalRecordV2: Equatable {
    public let sequence: Int64; public let phase: JournalPhase; public let admissionRecordSHA256: SHA256Digest; public let receiptFileSHA256: SHA256Digest?; public let failureClass: JournalFailureClass?; public let publishedClosureSHA256: SHA256Digest?; public let publishedDevice: Int64?; public let publishedInode: Int64?; public let attemptUUID: String; public let sha256: SHA256Digest
    public static func decode(_ bytes: [UInt8]) throws -> JournalRecordV2 {
        let value = try CanonicalJSON.decode(bytes: bytes); try selfDigest(value)
        guard case .object(let pairs) = value, let rawPhase = pairs.first(where: { $0.0 == "phase" })?.1, case .string(let phaseText) = rawPhase, let phase = JournalPhase(rawValue: phaseText) else { throw EvidenceError.invalidValue }
        let common: Set<String> = ["admission_record_sha256", "attempt_uuid", "authority_id", "kind", "namespace", "phase", "sequence", "journal_sha256", "sha256"]
        let extras: Set<String>
        let expectedSequence: Int64
        switch phase {
        case .stageCreated: extras = ["stage_name"]; expectedSequence = 1
        case .presealed, .renameIntent: extras = ["published_closure_sha256", "receipt_file_sha256"]; expectedSequence = phase == .presealed ? 2 : 3
        case .published: extras = ["published_closure_sha256", "receipt_file_sha256", "published_device", "published_inode"]; expectedSequence = 4
        case .quarantined: extras = ["failure_class", "quarantine_name"]; expectedSequence = 4
        case .publishIndeterminate: extras = ["failure_class", "published_closure_sha256", "receipt_file_sha256"]; expectedSequence = 4
        }
        let d = try object(value, common.union(extras)); try require(text(d, "authority_id"), ProtectedExecutionConstants.authorityID); try require(text(d, "kind"), "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2"); try require(text(d, "namespace"), ProtectedExecutionConstants.namespace)
        let attempt = try text(d, "attempt_uuid"); try validAttemptUUID(attempt); let sequence = try integer(d, "sequence"); guard sequence == expectedSequence else { throw EvidenceError.invalidValue }
        let admission = try requiredSHA(d, "admission_record_sha256"); let core: JSONValue = .object([("admission_record_sha256", .string(admission.value)), ("attempt_uuid", .string(attempt)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("phase", .string(phase.rawValue)), ("sequence", .integer(sequence))])
        guard CanonicalJSON.sha256(Array("R4JOURNAL2\0".utf8) + (try CanonicalJSON.encode(core))) == (try requiredSHA(d, "journal_sha256")).value else { throw EvidenceError.invalidDigest }
        let receipt: SHA256Digest?; let failure: JournalFailureClass?; let closure: SHA256Digest?; let device: Int64?; let inode: Int64?
        switch phase {
        case .stageCreated:
            guard try text(d, "stage_name") == attempt else { throw EvidenceError.invalidValue }; receipt = nil; failure = nil; closure = nil; device = nil; inode = nil
        case .presealed, .renameIntent:
            receipt = try requiredSHA(d, "receipt_file_sha256"); failure = nil; closure = try requiredSHA(d, "published_closure_sha256"); device = nil; inode = nil
        case .published:
            let dvalue = try integer(d, "published_device"); let ivalue = try integer(d, "published_inode"); guard dvalue >= 0, ivalue >= 0 else { throw EvidenceError.invalidValue }; receipt = try requiredSHA(d, "receipt_file_sha256"); failure = nil; closure = try requiredSHA(d, "published_closure_sha256"); device = dvalue; inode = ivalue
        case .quarantined:
            guard try text(d, "quarantine_name") == attempt, let value = JournalFailureClass(rawValue: try text(d, "failure_class")), value.rawValue.hasPrefix("PRE_") else { throw EvidenceError.invalidValue }; receipt = nil; failure = value; closure = nil; device = nil; inode = nil
        case .publishIndeterminate:
            guard let value = JournalFailureClass(rawValue: try text(d, "failure_class")), value.rawValue.hasPrefix("POST_") else { throw EvidenceError.invalidValue }; receipt = try requiredSHA(d, "receipt_file_sha256"); failure = value; closure = try requiredSHA(d, "published_closure_sha256"); device = nil; inode = nil
        }
        return try JournalRecordV2(sequence: sequence, phase: phase, admissionRecordSHA256: admission, receiptFileSHA256: receipt, failureClass: failure, publishedClosureSHA256: closure, publishedDevice: device, publishedInode: inode, attemptUUID: attempt, sha256: requiredSHA(d, "sha256"))
    }
    public static func decode(_ source: String) throws -> JournalRecordV2 { try decode(Array(source.utf8)) }
    public var filename: String {
        switch phase {
        case .stageCreated: return "0001-stage-created.json"
        case .presealed: return "0002-presealed.json"
        case .renameIntent: return "0003-rename-intent.json"
        case .published: return "0004-published.json"
        case .quarantined: return "0004-quarantined.json"
        case .publishIndeterminate: return "0004-publish-indeterminate.json"
        }
    }
    public func journalPreimage() throws -> [UInt8] { Array("R4JOURNAL2\0".utf8) + (try CanonicalJSON.encode(.object([("admission_record_sha256", .string(admissionRecordSHA256.value)), ("attempt_uuid", .string(attemptUUID)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("phase", .string(phase.rawValue)), ("sequence", .integer(sequence))]))) }
    public func canonicalRecordBytes() throws -> [UInt8] {
        let preimage = try journalPreimage(); let journalDigest = try requiredJournalSHA(); let core = try CanonicalJSON.decode(bytes: Array(preimage.dropFirst("R4JOURNAL2\0".utf8.count))); guard case .object(var fields) = core, CanonicalJSON.sha256(preimage) == journalDigest else { throw EvidenceError.invalidDigest }
        switch phase { case .stageCreated: fields += [("stage_name", .string(attemptUUID))]; case .presealed, .renameIntent: fields += [("published_closure_sha256", .string(publishedClosureSHA256!.value)), ("receipt_file_sha256", .string(receiptFileSHA256!.value))]; case .published: fields += [("published_closure_sha256", .string(publishedClosureSHA256!.value)), ("receipt_file_sha256", .string(receiptFileSHA256!.value)), ("published_device", .integer(publishedDevice!)), ("published_inode", .integer(publishedInode!))]; case .quarantined: fields += [("failure_class", .string(failureClass!.rawValue)), ("quarantine_name", .string(attemptUUID))]; case .publishIndeterminate: fields += [("failure_class", .string(failureClass!.rawValue)), ("published_closure_sha256", .string(publishedClosureSHA256!.value)), ("receipt_file_sha256", .string(receiptFileSHA256!.value))] }
        fields += [("journal_sha256", .string(journalDigest))]; guard CanonicalJSON.sha256(try CanonicalJSON.encode(.object(fields))) == sha256.value else { throw EvidenceError.invalidDigest }; return try CanonicalJSON.encode(.object(fields + [("sha256", .string(sha256.value))]))
    }
    private func requiredJournalSHA() throws -> String { try SHA256Digest(CanonicalJSON.sha256(journalPreimage())).value }
}

public struct PublishedAuthorityTree: Equatable {
    public let rows: [ManifestEntry]
    public static func decode(_ value: JSONValue) throws -> PublishedAuthorityTree {
        guard case .array(let values) = value else { throw EvidenceError.invalidShape }
        let rows = try values.map(ManifestEntry.decode)
        try orderedUniquePaths(rows.map(\.path))
        guard !rows.isEmpty, !rows.contains(where: { $0.path == "receipt.json" || $0.path == "receipt.ed25519" }) else { throw EvidenceError.invalidValue }
        return PublishedAuthorityTree(rows: rows)
    }
    public var canonicalRows: JSONValue { .array(rows.map { .object([("path", .string($0.path)), ("type", .string($0.type)), ("mode", .string($0.mode)), ("size", .integer($0.size)), ("sha256", .string($0.sha256.value))]) }) }
    public var sha256: String { get throws { try CanonicalJSON.domainSHA256("R4V2_PUBLISHED_CLOSURE\0", canonicalRows) } }
}

public enum V2Evidence {
    /// Receiver-side evidence available inside a sealed authority.  The original transport
    /// bundle and policy are deliberately not copied into the authority checkout; their hashes
    /// are bound by the immutable manifest/receipt/approval chain instead of becoming a second
    /// path authority at verification time.
    public static func crossValidateAuthority(_ approval: Task14ApprovalV2, _ admission: AdmissionRecord,
                                              _ admissionFileSHA256: SHA256Digest,
                                              _ runtime: RuntimeManifestV2, _ authority: AuthorityManifestV2,
                                              _ receipt: ReceiptV2, publishedTree: PublishedAuthorityTree) throws {
        let checkoutRows = publishedTree.rows.filter { $0.path == "checkout" || $0.path.hasPrefix("checkout/") }
        let runtimeRows = publishedTree.rows.filter { $0.path == "runtime" || $0.path.hasPrefix("runtime/") }
        let fixedRows = publishedTree.rows.filter { !($0.path == "checkout" || $0.path.hasPrefix("checkout/") || $0.path == "runtime" || $0.path.hasPrefix("runtime/")) }
        guard authority.runtimeManifestSHA256 == runtime.sha256,
              authority.entries == checkoutRows, runtime.entries == runtimeRows,
              Set(fixedRows.map(\.path)) == Set(["admission-locator-v2.json", "admission.json", "manifest-v2.json", "runtime-manifest-v2.json"]),
              fixedRows.allSatisfy({ $0.type == "regular" && $0.mode == "0444" && $0.size > 0 }),
              receipt.manifestSHA256 == authority.sha256,
              receipt.runtimeManifestSHA256 == runtime.sha256,
              receipt.task14ApprovalSHA256 == approval.sha256,
              receipt.admissionRecordSHA256 == admissionFileSHA256,
              receipt.inputBundleManifestSHA256 == authority.inputBundleManifestSHA256,
              receipt.materializationPolicySHA256 == authority.materializationPolicySHA256,
              runtime.materializationInputSHA256 == authority.inputBundleManifestSHA256,
              runtime.materializationPolicySHA256 == authority.materializationPolicySHA256,
              receipt.lockSHA256 == runtime.lockSHA256,
              receipt.wheelSetSHA256 == runtime.wheelSetSHA256,
              receipt.publishedClosureSHA256.value == (try publishedTree.sha256),
              receipt.gitHead == authority.git.head, receipt.gitTree == authority.git.tree,
              receipt.requiredBlobSetSHA256 == approval.requiredBlobSetSHA256,
              receipt.runtimeOutputClosureSHA256 == runtime.runtimeOutputClosureSHA256,
              receipt.dependencyClosureSHA256 == runtime.dependencyClosureSHA256,
              receipt.preInstallStagedMachOObservationsSHA256 == runtime.preInstallStagedMachOObservationsSHA256,
              receipt.stagedMachOObservationsSHA256 == runtime.stagedMachOObservationsSHA256,
              receipt.sandboxLauncherPath == runtime.sandboxLauncherPath,
              receipt.sandboxLauncherDescriptorIdentitySHA256 == runtime.sandboxLauncherDescriptorIdentitySHA256,
              receipt.sandboxLauncherSHA256 == runtime.sandboxLauncherSHA256,
              receipt.sandboxLauncherCodesignIdentitySHA256 == runtime.sandboxLauncherCodesignIdentitySHA256,
              receipt.sandboxProfileTemplateSHA256 == runtime.sandboxProfileTemplateSHA256,
              receipt.sandboxProfileSHA256 == runtime.sandboxProfileSHA256,
              receipt.childCommandTemplateSHA256 == runtime.childCommandTemplateSHA256,
              receipt.installerCommandSHA256 == runtime.installerCommandSHA256,
              receipt.importCommandSHA256 == runtime.importCommandSHA256,
              receipt.interpreterPreInstallSHA256 == runtime.interpreterPreInstallSHA256,
              receipt.interpreterPreInstallMachOSHA256 == runtime.interpreterPreInstallMachOSHA256,
              receipt.interpreterPreInstallCodesignIdentitySHA256 == runtime.interpreterPreInstallCodesignIdentitySHA256,
              receipt.interpreterPostInstallSHA256 == runtime.interpreterPostInstallSHA256,
              receipt.interpreterPostInstallMachOSHA256 == runtime.interpreterPostInstallMachOSHA256,
              receipt.interpreterPostInstallCodesignIdentitySHA256 == runtime.interpreterPostInstallCodesignIdentitySHA256,
              receipt.environmentSHA256 == runtime.environmentSHA256,
              receipt.provisionerBinarySHA256 == approval.provisionerBinarySHA256,
              receipt.helperCodesignRequirementSHA256 == approval.provisionerDesignatedRequirementSHA256,
              receipt.helperTeamID == approval.helperTeamID,
              receipt.helperSigningIdentitySHA1 == approval.helperSigningIdentitySHA1,
              admission.task14ApprovalSHA256 == approval.sha256,
              admission.inputBundleManifestSHA256 == authority.inputBundleManifestSHA256,
              admission.materializationPolicySHA256 == authority.materializationPolicySHA256 else {
            throw EvidenceError.invalidValue
        }
    }
    public static func validateApprovalGraph(_ review: Task14ReviewPackageV2, _ authorization: Task14ObservationAuthorizationV2, _ approval: Task14ApprovalV2, _ policy: MaterializationPolicy) throws {
        guard review.sha256 == authorization.reviewPackageSHA256,
              review.sha256 == approval.reviewPackageSHA256,
              authorization.sha256 == approval.observationAuthorizationSHA256,
              review.inputBundleManifestSHA256 == authorization.inputBundleManifestSHA256,
              review.inputBundleManifestSHA256 == approval.inputBundleManifestSHA256,
              review.materializationPolicySHA256 == authorization.materializationPolicySHA256,
              review.materializationPolicySHA256 == approval.materializationPolicySHA256,
              review.provisionerBinarySHA256 == authorization.provisionerBinarySHA256,
              review.provisionerBinarySHA256 == approval.provisionerBinarySHA256,
              review.provisionerDesignatedRequirementSHA256 == authorization.provisionerDesignatedRequirementSHA256,
              review.provisionerDesignatedRequirementSHA256 == approval.provisionerDesignatedRequirementSHA256,
              review.task14ApprovalVerificationPublicKeySHA256 == authorization.task14ApprovalVerificationPublicKeySHA256,
              review.task14ApprovalVerificationPublicKeySHA256 == approval.task14ApprovalVerificationPublicKeySHA256,
              approval.sandboxLauncherPath == policy.sandboxLauncher.path,
              approval.sandboxLauncherDescriptorIdentitySHA256 == policy.sandboxLauncher.descriptorIdentitySHA256,
              approval.sandboxLauncherSHA256 == policy.sandboxLauncher.sha256,
              approval.sandboxLauncherCodesignIdentitySHA256 == policy.sandboxLauncher.codesignIdentity.sha256,
              approval.sandboxProfileTemplateSHA256 == policy.sandboxProfileTemplateSHA256,
              approval.childCommandTemplateSHA256 == policy.childCommandTemplateSHA256,
              approval.baseInterpreterPath == policy.baseInterpreter.path,
              approval.baseInterpreterSHA256 == policy.baseInterpreter.sha256,
              approval.baseInterpreterMachOSHA256 == policy.baseInterpreter.machOSHA256,
              approval.baseInterpreterCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256 else { throw EvidenceError.invalidValue }
    }
    private static func baseCrossValidate(_ bundle: InputBundleManifest, _ policy: MaterializationPolicy, _ approval: Task14ApprovalV2, _ admission: AdmissionRecord, _ runtime: RuntimeManifestV2, _ authority: AuthorityManifestV2, _ receipt: ReceiptV2, publishedTree: PublishedAuthorityTree) throws {
        let publishedDigest = try publishedTree.sha256
        let checkoutRows = publishedTree.rows.filter { $0.path == "checkout" || $0.path.hasPrefix("checkout/") }
        let runtimeRows = publishedTree.rows.filter { $0.path == "runtime" || $0.path.hasPrefix("runtime/") }
        let fixedRows = publishedTree.rows.filter {
            !($0.path == "checkout" || $0.path.hasPrefix("checkout/")) &&
            !($0.path == "runtime" || $0.path.hasPrefix("runtime/"))
        }
        let expectedFixed = Set(["admission-locator-v2.json", "admission.json", "manifest-v2.json", "runtime-manifest-v2.json"])
        guard Set(fixedRows.map(\.path)) == expectedFixed,
              fixedRows.allSatisfy({ $0.type == "regular" && $0.mode == "0444" && $0.size > 0 }) else { throw EvidenceError.invalidShape }
        guard bundle.entries == authority.entries, bundle.entries == checkoutRows, runtime.entries == runtimeRows else { throw EvidenceError.invalidDigest }
        guard policy.wheelFilenames == bundle.wheelFilenames, approval.reviewedHead == bundle.git.head, approval.reviewedTree == bundle.git.tree, approval.inputBundleManifestSHA256 == bundle.sha256, admission.task14ApprovalSHA256 == approval.sha256, runtime.materializationInputSHA256 == bundle.sha256, authority.inputBundleManifestSHA256 == bundle.sha256, receipt.inputBundleManifestSHA256 == bundle.sha256, approval.materializationPolicySHA256 == policy.sha256, runtime.materializationPolicySHA256 == policy.sha256, receipt.materializationPolicySHA256 == policy.sha256, approval.lockSHA256 == bundle.lockSHA256, runtime.lockSHA256 == bundle.lockSHA256, receipt.lockSHA256 == bundle.lockSHA256, approval.wheelSetSHA256 == bundle.wheelSetSHA256, runtime.wheelSetSHA256 == bundle.wheelSetSHA256, receipt.wheelSetSHA256 == bundle.wheelSetSHA256, bundle.git == authority.git, runtime.importProbeSHA256 == policy.importProbeSHA256, runtime.sandboxLauncherSHA256 == policy.sandboxLauncher.sha256, runtime.sandboxProfileTemplateSHA256 == policy.sandboxProfileTemplateSHA256, runtime.childCommandTemplateSHA256 == policy.childCommandTemplateSHA256, receipt.sandboxLauncherSHA256 == runtime.sandboxLauncherSHA256, receipt.sandboxProfileTemplateSHA256 == runtime.sandboxProfileTemplateSHA256, receipt.childCommandTemplateSHA256 == runtime.childCommandTemplateSHA256, receipt.environmentSHA256 == runtime.environmentSHA256, receipt.preInstallStagedMachOObservationsSHA256 == runtime.preInstallStagedMachOObservationsSHA256, receipt.stagedMachOObservationsSHA256 == runtime.stagedMachOObservationsSHA256, authority.runtimeManifestSHA256 == runtime.sha256, receipt.runtimeManifestSHA256 == runtime.sha256, receipt.manifestSHA256 == authority.sha256, receipt.task14ApprovalSHA256 == approval.sha256, receipt.admissionRecordSHA256 == (try SHA256Digest(CanonicalJSON.sha256(try admission.canonicalRecordBytes()))), receipt.runtimeOutputClosureSHA256 == runtime.runtimeOutputClosureSHA256, receipt.dependencyClosureSHA256 == runtime.dependencyClosureSHA256, receipt.provisionerBinarySHA256 == approval.provisionerBinarySHA256, receipt.helperCodesignRequirementSHA256 == approval.provisionerDesignatedRequirementSHA256, receipt.helperTeamID == approval.helperTeamID, receipt.helperSigningIdentitySHA1 == approval.helperSigningIdentitySHA1, receipt.publishedClosureSHA256.value == publishedDigest else { throw EvidenceError.invalidValue }
        guard runtime.sandboxLauncherPath == policy.sandboxLauncher.path,
              runtime.sandboxLauncherDescriptorIdentitySHA256 == policy.sandboxLauncher.descriptorIdentitySHA256,
              runtime.sandboxLauncherCodesignIdentitySHA256 == policy.sandboxLauncher.codesignIdentity.sha256,
              runtime.interpreterPreInstallSHA256 == policy.baseInterpreter.sha256,
              runtime.interpreterPreInstallMachOSHA256 == policy.baseInterpreter.machOSHA256,
              runtime.interpreterPreInstallCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256,
              runtime.interpreterPostInstallSHA256 == runtime.interpreterPreInstallSHA256,
              runtime.interpreterPostInstallMachOSHA256 == runtime.interpreterPreInstallMachOSHA256,
              runtime.interpreterPostInstallCodesignIdentitySHA256 == runtime.interpreterPreInstallCodesignIdentitySHA256,
              runtime.workingDirectory == policy.workingDirectory,
              receipt.sandboxLauncherPath == runtime.sandboxLauncherPath,
              receipt.sandboxLauncherDescriptorIdentitySHA256 == runtime.sandboxLauncherDescriptorIdentitySHA256,
              receipt.sandboxLauncherSHA256 == runtime.sandboxLauncherSHA256,
              receipt.sandboxLauncherCodesignIdentitySHA256 == runtime.sandboxLauncherCodesignIdentitySHA256,
              receipt.sandboxProfileSHA256 == runtime.sandboxProfileSHA256,
              receipt.installerCommandSHA256 == runtime.installerCommandSHA256,
              receipt.importCommandSHA256 == runtime.importCommandSHA256,
              receipt.interpreterPreInstallSHA256 == runtime.interpreterPreInstallSHA256,
              receipt.interpreterPreInstallMachOSHA256 == runtime.interpreterPreInstallMachOSHA256,
              receipt.interpreterPreInstallCodesignIdentitySHA256 == runtime.interpreterPreInstallCodesignIdentitySHA256,
              receipt.interpreterPostInstallSHA256 == runtime.interpreterPostInstallSHA256,
              receipt.interpreterPostInstallMachOSHA256 == runtime.interpreterPostInstallMachOSHA256,
              receipt.interpreterPostInstallCodesignIdentitySHA256 == runtime.interpreterPostInstallCodesignIdentitySHA256 else { throw EvidenceError.invalidValue }
    }
    /// Receiver-side graph verification has no live child-process observation to replay.  It
    /// nevertheless binds every stored checkout/runtime/approval/admission/receipt digest and
    /// the complete published closure before a caller accepts the authority.
    public static func crossValidatePublished(_ bundle: InputBundleManifest, _ policy: MaterializationPolicy,
                                              _ approval: Task14ApprovalV2, _ admission: AdmissionRecord,
                                              _ runtime: RuntimeManifestV2, _ authority: AuthorityManifestV2,
                                              _ receipt: ReceiptV2, publishedTree: PublishedAuthorityTree) throws {
        try baseCrossValidate(bundle, policy, approval, admission, runtime, authority, receipt,
                              publishedTree: publishedTree)
    }
    public static func crossValidate(_ bundle: InputBundleManifest, _ policy: MaterializationPolicy, _ approval: Task14ApprovalV2, _ admission: AdmissionRecord, _ runtime: RuntimeManifestV2, _ authority: AuthorityManifestV2, _ receipt: ReceiptV2, publishedTree: PublishedAuthorityTree, observation: ExecutionObservation) throws {
        try baseCrossValidate(bundle, policy, approval, admission, runtime, authority, receipt, publishedTree: publishedTree)
        let expectedProfile = try policy.reboundSandboxProfile(stage: observation.stageRoot)
        let expectedEnvironment = try policy.reboundEnvironment(stage: observation.stageRoot)
        let interpreter = observation.stageRoot + "/runtime/closure/python311/bin/python3.11"
        let expectedInstaller = ["/usr/bin/sandbox-exec", "-p", expectedProfile, interpreter] + (try policy.reboundPipArgv(stage: observation.stageRoot))
        let expectedImport = ["/usr/bin/sandbox-exec", "-p", expectedProfile, interpreter] + policy.importArgv.map { $0.replacingOccurrences(of: MaterializationPolicy.stageToken, with: observation.stageRoot) }
        guard observation.actualProfileUTF8 == expectedProfile,
              observation.environment == expectedEnvironment,
              observation.installerCommand.program == "/usr/bin/sandbox-exec",
              observation.importCommand.program == "/usr/bin/sandbox-exec",
              observation.installerCommand.argv == expectedInstaller,
              observation.importCommand.argv == expectedImport,
              observation.installerCommand.environment == observation.environment,
              observation.importCommand.environment == observation.environment,
              observation.installerCommand.cwd == policy.workingDirectory,
              observation.importCommand.cwd == policy.workingDirectory,
              observation.installerCommand.timeoutSeconds == policy.timeoutSeconds,
              observation.importCommand.timeoutSeconds == policy.timeoutSeconds,
              observation.installerCommand.maxOutputBytes == policy.maxOutputBytes,
              observation.importCommand.maxOutputBytes == policy.maxOutputBytes,
              observation.installerCommand.sandboxLauncherDescriptorIdentitySHA256 == policy.sandboxLauncher.descriptorIdentitySHA256,
              observation.installerCommand.sandboxLauncherSHA256 == policy.sandboxLauncher.sha256,
              observation.installerCommand.sandboxLauncherCodesignIdentitySHA256 == policy.sandboxLauncher.codesignIdentity.sha256,
              observation.installerCommand.interpreterSHA256 == policy.baseInterpreter.sha256,
              observation.installerCommand.interpreterMachOSHA256 == policy.baseInterpreter.machOSHA256,
              observation.installerCommand.interpreterCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256,
              observation.importCommand.sandboxLauncherDescriptorIdentitySHA256 == observation.installerCommand.sandboxLauncherDescriptorIdentitySHA256,
              observation.importCommand.sandboxLauncherSHA256 == observation.installerCommand.sandboxLauncherSHA256,
              observation.importCommand.sandboxLauncherCodesignIdentitySHA256 == observation.installerCommand.sandboxLauncherCodesignIdentitySHA256,
              observation.importCommand.interpreterSHA256 == observation.installerCommand.interpreterSHA256,
              observation.importCommand.interpreterMachOSHA256 == observation.installerCommand.interpreterMachOSHA256,
              observation.importCommand.interpreterCodesignIdentitySHA256 == observation.installerCommand.interpreterCodesignIdentitySHA256,
              observation.interpreterMachOObservation.path == policy.baseInterpreter.path else { throw EvidenceError.invalidValue }
        let actualProfile = try ActualSandboxProfile.sha256(observation.actualProfileUTF8)
        let actualEnvironment = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_ENVIRONMENT\0", .array(observation.environment.map(JSONValue.string))))
        let machO = try observation.interpreterMachOObservation.sha256()
        let observedDependencies = try StagedMachOObservation.flattenedDependencies(observation.stagedMachOObservations)
        let observedDependencyValue = JSONValue.array(observedDependencies.map(\.canonicalValue))
        let dependencies = try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", observedDependencyValue))
        let preInstallStagedMachO = try StagedMachOObservation.collectionSHA256(observation.preInstallStagedMachOObservations)
        let stagedMachO = try StagedMachOObservation.collectionSHA256(observation.stagedMachOObservations)
        let installerDigest = try observation.installerCommand.installCommandSHA256()
        let importDigest = try observation.importCommand.importCommandSHA256()
        guard runtime.sandboxProfileSHA256 == actualProfile,
              receipt.sandboxProfileSHA256 == actualProfile,
              runtime.installerCommandSHA256 == installerDigest,
              receipt.installerCommandSHA256 == installerDigest,
              runtime.importCommandSHA256 == importDigest,
              receipt.importCommandSHA256 == importDigest,
              observation.installerCommand.sandboxProfileSHA256 == actualProfile,
              observation.importCommand.sandboxProfileSHA256 == actualProfile,
              runtime.environmentSHA256 == actualEnvironment,
              receipt.environmentSHA256 == actualEnvironment,
              runtime.preInstallStagedMachOObservationsSHA256 == preInstallStagedMachO,
              runtime.preInstallStagedMachOObservations == observation.preInstallStagedMachOObservations,
              receipt.preInstallStagedMachOObservationsSHA256 == preInstallStagedMachO,
              runtime.stagedMachOObservationsSHA256 == stagedMachO,
              runtime.stagedMachOObservations == observation.stagedMachOObservations,
              try CanonicalJSON.encode(.array(runtime.machODependencies.map(\.canonicalValue))) == CanonicalJSON.encode(observedDependencyValue),
              runtime.dependencyClosureSHA256 == dependencies,
              receipt.dependencyClosureSHA256 == dependencies,
              runtime.interpreterPreInstallMachOSHA256 == machO,
              runtime.interpreterPostInstallMachOSHA256 == machO,
              receipt.interpreterPreInstallMachOSHA256 == machO,
              receipt.interpreterPostInstallMachOSHA256 == machO else { throw EvidenceError.invalidDigest }
    }

    /// Task 9 consumes the sealed Task 8 result directly.  No caller may
    /// reconstruct the execution tuple from paths or command fragments.
    public static func crossValidate(_ bundle: InputBundleManifest, _ policy: MaterializationPolicy, _ approval: Task14ApprovalV2, _ admission: AdmissionRecord, _ runtime: RuntimeManifestV2, _ authority: AuthorityManifestV2, _ receipt: ReceiptV2, publishedTree: PublishedAuthorityTree, installation: InstallationObservation) throws {
        try crossValidate(bundle, policy, approval, admission, runtime, authority, receipt, publishedTree: publishedTree, observation: installation.executionObservation)
    }
}

private extension JSONValue {
    func objectValue(_ key: String) -> JSONValue { guard case .object(let pairs) = self, let value = pairs.first(where: { $0.0 == key })?.1 else { fatalError("closed object key missing") }; return value }
}
