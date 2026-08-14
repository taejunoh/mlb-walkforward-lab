import XCTest
@testable import R4ProtectedExecutionProvisioner

final class CanonicalJSONTests: XCTestCase {
    func testCanonicalJSONRejectsAmbiguousRepresentations() throws {
        for source in [#"{"a":1,"a":2}"#, #"{ "a":1}"#, #"{"b":1,"a":2}"#, #"{"a":1.0}"#, #"{"a":1e0}"#, "{\"a\":1}\n"] {
            XCTAssertThrowsError(try CanonicalJSON.decode(bytes: Array(source.utf8)))
        }
    }

    func testCanonicalEncoderRejectsDuplicateKeysAndSorts() throws {
        XCTAssertThrowsError(try CanonicalJSON.encode(.object([("a", .integer(1)), ("a", .integer(2))])))
        XCTAssertEqual(String(bytes: try CanonicalJSON.encode(.object([("b", .integer(1)), ("a", .integer(2))])), encoding: .utf8), #"{"a":2,"b":1}"#)
    }

    func testCanonicalJSONResourceLimits() throws {
        let boundary = String(repeating: "[", count: CanonicalJSON.maximumNesting) + "0" + String(repeating: "]", count: CanonicalJSON.maximumNesting)
        XCTAssertNoThrow(try CanonicalJSON.decode(bytes: Array(boundary.utf8)))
        let depth = Int(ProcessInfo.processInfo.environment["R4_CANONICAL_DEPTH_PROBE"] ?? "\(CanonicalJSON.maximumNesting + 1)")!
        let deep = String(repeating: "[", count: depth) + "0" + String(repeating: "]", count: depth)
        XCTAssertThrowsError(try CanonicalJSON.decode(bytes: Array(deep.utf8)))
        XCTAssertThrowsError(try CanonicalJSON.decode(bytes: Array(repeating: 0x20, count: CanonicalJSON.maximumInputBytes + 1)))
        let members = "[" + String(repeating: "0,", count: CanonicalJSON.maximumCollectionMembers) + "0]"
        XCTAssertThrowsError(try CanonicalJSON.decode(bytes: Array(members.utf8)))
    }

    func testCanonicalJSONDepthProbeExitsNormallyInFreshProcess() throws {
        let package = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path
        let (status, scratch) = try runDepthProbe(package: package)
        XCTAssertEqual(status, 0)
        XCTAssertFalse(FileManager.default.fileExists(atPath: scratch.path))
    }

    private func runDepthProbe(package: String) throws -> (Int32, URL) {
        let scratch = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("r4-canonical-depth-\(ProcessInfo.processInfo.processIdentifier)")
        defer { try? FileManager.default.removeItem(at: scratch) }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["swift", "test", "--package-path", package, "--scratch-path", scratch.path, "--filter", "CanonicalJSONTests/testCanonicalJSONResourceLimits"]
        var environment = ProcessInfo.processInfo.environment
        environment["R4_CANONICAL_DEPTH_PROBE"] = "10000"
        process.environment = environment
        try process.run()
        process.waitUntilExit()
        return (process.terminationStatus, scratch)
    }

    func testAdministrativeV1GenesisClosedDecoderMatrix() throws {
        let source = try administrativeFixtures().genesis
        XCTAssertNoThrow(try Keyring.decode(source))
        try assertClosedRecordMatrix(source, decoder: Keyring.decode, semanticMutations: [
            ("key_id", .string("not-a-digest")),
            ("generation", .integer(2)),
            ("public_key_sha256", .string("not-a-digest")),
            ("previous_record_sha256", .string(String(repeating: "a", count: 64))),
            ("issued_at", .string("")),
            ("detached_signature_sha256", .string("not-a-digest"))
        ])
    }

    func testAdministrativeV2RotationClosedDecoderMatrix() throws {
        let fixtures = try administrativeFixtures()
        let source = fixtures.rotation
        XCTAssertNoThrow(try Keyring.decode(source))
        try assertClosedRecordMatrix(source, decoder: Keyring.decode, semanticMutations: [
            ("authority_id", .string("wrong")),
            ("namespace", .string("wrong")),
            ("generation", .integer(1)),
            ("previous_generation", .integer(0)),
            ("previous_record_sha256", .string("not-a-digest")),
            ("previous_public_key_sha256", .string("not-a-digest")),
            ("new_public_key_sha256", .string("not-a-digest")),
            ("issued_at", .string("")),
            ("detached_signature_sha256", .string("not-a-digest"))
        ])
        let pairs = try objectPairs(source)
        let prior = pairs.first { $0.0 == "previous_public_key_sha256" }!.1
        try assertError(.invalidValue, decoder: Keyring.decode, source: try resealed(replacing(pairs, "new_public_key_sha256", prior)))
        XCTAssertNoThrow(try KeyringRecord.validateContiguous([try Keyring.decode(fixtures.genesis), try Keyring.decode(source)]))
        let gap = try Keyring.decode(resealed(replacing(pairs, "previous_record_sha256", .string(String(repeating: "f", count: 64)))))
        XCTAssertThrowsError(try KeyringRecord.validateContiguous([try Keyring.decode(fixtures.genesis), gap])) { error in
            XCTAssertEqual(error as? EvidenceError, .invalidValue)
        }
    }

    func testAdministrativeV2RevocationClosedDecoderMatrix() throws {
        let source = try administrativeFixtures().revocation
        XCTAssertNoThrow(try RevocationRecord.decode(source))
        try assertClosedRecordMatrix(source, decoder: RevocationRecord.decode, semanticMutations: [
            ("namespace", .string("wrong")),
            ("authority_id", .string("wrong")),
            ("state", .string("ACTIVE")),
            ("sequence", .integer(0)),
            ("signer_generation", .integer(0)),
            ("target_generation", .integer(0)),
            ("signer_public_key_sha256", .string("not-a-digest")),
            ("target_public_key_sha256", .string("not-a-digest")),
            ("previous_revocation_sha256", .string("gap")),
            ("revoked_at", .string("")),
            ("reason", .string("OTHER")),
            ("detached_signature_sha256", .string("not-a-digest"))
        ])
    }

    private func administrativeFixtures() throws -> (genesis: String, rotation: String, revocation: String) {
        let genesis = try CanonicalJSON.withSelfDigest(#"{"detached_signature_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","generation":1,"issued_at":"bootstrap-generation-1","key_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","kind":"R4_PROTECTED_EXECUTION_KEYRING_V1","previous_record_sha256":null,"public_key_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#)
        let genesisRecord = try Keyring.decode(genesis)
        let rotationBare = String(format: #"{"authority_id":"r4-disjoint-custody-v2-execution-v1","detached_signature_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","generation":2,"issued_at":"2026-08-08T00:00:00Z","kind":"R4_PROTECTED_EXECUTION_KEY_ROTATION_V2","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","new_public_key_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","previous_generation":1,"previous_public_key_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","previous_record_sha256":"%@"}"#, genesisRecord.sha256.value)
        let rotation = try CanonicalJSON.withSelfDigest(rotationBare)
        let revocation = try CanonicalJSON.withSelfDigest(#"{"authority_id":"r4-disjoint-custody-v2-execution-v1","detached_signature_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","kind":"R4_PROTECTED_EXECUTION_REVOCATION_V2","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","previous_revocation_sha256":null,"reason":"KEY_COMPROMISE","revoked_at":"2026-08-08T00:00:00Z","sequence":1,"signer_generation":1,"signer_key_record_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","signer_public_key_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","state":"REVOKED","target_generation":1,"target_key_record_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","target_public_key_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#)
        return (genesis, rotation, revocation)
    }

    private func assertClosedRecordMatrix(_ source: String, decoder: (String) throws -> KeyringRecord, semanticMutations: [(String, JSONValue)]) throws {
        let pairs = try objectPairs(source)
        XCTAssertFalse(pairs.contains { if case .array = $0.1 { return true }; if case .object = $0.1 { return true }; return false }, "Administrative schemas have no nested values to extend.")
        for (key, _) in pairs {
            let omitted = pairs.filter { $0.0 != key }
            if key == "sha256" {
                try assertError(.invalidDigest, decoder: decoder, source: try canonical(omitted))
            } else {
                try assertError(.missingKey, decoder: decoder, source: try resealed(omitted))
            }
        }
        try assertError(.unknownKey, decoder: decoder, source: try resealed(pairs + [("unexpected", .integer(1))]))
        for (key, value) in semanticMutations {
            try assertError(.invalidValue, decoder: decoder, source: try resealed(replacing(pairs, key, value)))
        }
        let digest = try KeyringRecord.decode(source).sha256.value
        try assertError(.invalidDigest, decoder: decoder, source: source.replacingOccurrences(of: digest, with: String(repeating: "f", count: 64)))
    }

    private func objectPairs(_ source: String) throws -> [(String, JSONValue)] {
        guard case .object(let pairs) = try CanonicalJSON.decode(bytes: Array(source.utf8)) else { throw CanonicalJSONError.invalidJSON }
        return pairs
    }

    private func replacing(_ pairs: [(String, JSONValue)], _ key: String, _ value: JSONValue) -> [(String, JSONValue)] {
        pairs.map { $0.0 == key ? (key, value) : $0 }
    }

    private func canonical(_ pairs: [(String, JSONValue)]) throws -> String {
        String(bytes: try CanonicalJSON.encode(.object(pairs)), encoding: .utf8)!
    }

    private func resealed(_ pairs: [(String, JSONValue)]) throws -> String {
        try CanonicalJSON.withSelfDigest(canonical(pairs.filter { $0.0 != "sha256" }))
    }

    private func assertError(_ expected: EvidenceError, decoder: (String) throws -> KeyringRecord, source: String, file: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertThrowsError(try decoder(source), file: file, line: line) { error in
            XCTAssertEqual(error as? EvidenceError, expected, file: file, line: line)
        }
    }
}
