import Foundation
@testable import R4ProtectedExecutionProvisioner

struct DomainVector { let domain: String; let canonicalValue: JSONValue; let expectedSHA256: SHA256Digest }
struct RecordVector { let kind: String; let preimage: [UInt8]; let record: [UInt8] }
struct DomainVectorFixture {
    let schemaVersion: Int; let kind: String; let vectors: [DomainVector]; let records: [RecordVector]; let frozenGenesisSigning: (seed: [UInt8], publicKey: [UInt8], signature: [UInt8], preimage: [UInt8], record: [UInt8])
    static func decode(_ resourceName: String) throws -> DomainVectorFixture {
        guard resourceName == "r4_v2_domain_vectors.json", let url = Bundle.module.url(forResource: resourceName, withExtension: nil) else { throw EvidenceError.invalidValue }
        return try decode(bytes: Array(try Data(contentsOf: url)))
    }
    static func decode(bytes: [UInt8]) throws -> DomainVectorFixture {
        let value = try CanonicalJSON.decode(bytes: bytes)
        guard try CanonicalJSON.encode(value) == bytes else { throw EvidenceError.invalidValue }
        let root = try closedObject(value, ["schema_version", "kind", "vectors", "records", "frozen_genesis_signing"])
        guard case .integer(let version) = root["schema_version"], version == 2, case .string(let kind) = root["kind"], kind == "R4_V2_DOMAIN_VECTORS_V1", case .array(let values) = root["vectors"], case .array(let records) = root["records"] else { throw EvidenceError.invalidValue }
        let allowed: Set<String> = [
            "R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0", "R4V2_CERTIFICATE_CHAIN\0",
            "R4V2_CODESIGN_IDENTITY\0", "R4V2_MACHO_OBSERVATION\0",
            "R4V2_SANDBOX_PROFILE_TEMPLATE\0", "R4V2_SANDBOX_PROFILE_ACTUAL\0",
            "R4V2_CHILD_COMMAND_TEMPLATE\0", "R4V2_INSTALL_COMMAND\0",
            "R4V2_IMPORT_COMMAND\0", "R4V2_ENVIRONMENT\0", "R4V2_PUBLISHED_CLOSURE\0",
            "R4KEYROT2\0", "R4KEYREVOKE2\0", "R4ADMISSIONLOCATOR2\0", "R4JOURNAL2\0"
        ]
        guard values.count == allowed.count else { throw EvidenceError.invalidValue }
        var previous: String?; var vectors: [DomainVector] = []
        for value in values {
            let row = try closedObject(value, ["domain", "canonical_value", "expected_sha256"])
            guard case .string(let domain) = row["domain"], allowed.contains(domain), case .string(let rawDigest) = row["expected_sha256"] else { throw EvidenceError.invalidValue }
            let digest = try SHA256Digest(rawDigest); guard rawDigest == digest.value, previous.map({ $0 < domain }) ?? true else { throw EvidenceError.unorderedArray }
            previous = domain; vectors.append(DomainVector(domain: domain, canonicalValue: row["canonical_value"]!, expectedSHA256: digest))
        }
        let expectedKinds: Set<String> = ["frozen_genesis", "rotation", "revocation", "locator", "journal_stage_created", "journal_presealed", "journal_rename_intent", "journal_published", "journal_quarantined", "journal_publish_indeterminate"]
        guard records.count == expectedKinds.count else { throw EvidenceError.invalidValue }
        var decodedRecords: [RecordVector] = []; var seen = Set<String>()
        for value in records {
            let row = try closedObject(value, ["kind", "preimage_hex", "record_hex"])
            guard case .string(let kind) = row["kind"], expectedKinds.contains(kind), seen.insert(kind).inserted, case .string(let preimageHex) = row["preimage_hex"], case .string(let recordHex) = row["record_hex"], let preimage = decodeHex(preimageHex), let record = decodeHex(recordHex), !preimage.isEmpty, !record.isEmpty else { throw EvidenceError.invalidValue }
            decodedRecords.append(RecordVector(kind: kind, preimage: preimage, record: record))
        }
        let signing = try closedObject(root["frozen_genesis_signing"]!, ["seed_hex", "public_key_hex", "signature_hex", "preimage_hex", "record_hex"])
        guard case .string(let seedHex) = signing["seed_hex"], case .string(let publicHex) = signing["public_key_hex"], case .string(let signatureHex) = signing["signature_hex"], case .string(let preimageHex) = signing["preimage_hex"], case .string(let recordHex) = signing["record_hex"], let seed = decodeHex(seedHex), let publicKey = decodeHex(publicHex), let signature = decodeHex(signatureHex), let preimage = decodeHex(preimageHex), let record = decodeHex(recordHex), seed.count == 32, publicKey.count == 32, signature.count == 64 else { throw EvidenceError.invalidValue }
        return DomainVectorFixture(schemaVersion: 2, kind: kind, vectors: vectors, records: decodedRecords, frozenGenesisSigning: (seed, publicKey, signature, preimage, record))
    }
    private static func closedObject(_ value: JSONValue, _ keys: Set<String>) throws -> [String: JSONValue] {
        guard case .object(let pairs) = value, pairs.count == keys.count, Set(pairs.map(\.0)) == keys else { throw EvidenceError.invalidShape }
        return Dictionary(uniqueKeysWithValues: pairs)
    }
    private static func decodeHex(_ hex: String) -> [UInt8]? {
        guard hex.count.isMultiple(of: 2), hex.unicodeScalars.allSatisfy({ $0.value.isMultiple(of: 1) && ((48...57).contains($0.value) || (97...102).contains($0.value)) }) else { return nil }
        return stride(from: 0, to: hex.count, by: 2).compactMap { UInt8(hex[hex.index(hex.startIndex, offsetBy: $0)..<hex.index(hex.startIndex, offsetBy: $0 + 2)], radix: 16) }
    }
}
