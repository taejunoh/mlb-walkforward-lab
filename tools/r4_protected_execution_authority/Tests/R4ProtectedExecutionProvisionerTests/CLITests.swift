import XCTest
@testable import R4ProtectedExecutionProvisioner

final class CLITests: XCTestCase {
    func testClosedGrammarAcceptsOnlyExactCommands() throws {
        XCTAssertEqual(try CLI.parse(["verify"]), .verify)
        XCTAssertEqual(try CLI.parse(["materialize", "--input-bundle", "/private/tmp/input-bundle"]), .materialize(inputBundle: "/private/tmp/input-bundle"))
    }

    func testClosedGrammarRejectsAliasesOrderAndExtrasBeforeMutation() throws {
        let valid = ["materialize", "--input-bundle", "/private/tmp/input-bundle"]
        let invalid = [["provision"], ["verify", "--authority-id", "other"], ["materialize"], ["materialize", "--input-bundle", "relative"], ["materialize", "--input-bundle", "/private/tmp/a/../bundle"], ["materialize-and-provision", "--input-bundle", "/private/tmp/a"], ["bootstrap-install"], ["rotate-key"], ["revoke-key"], valid + ["--input-bundle", "/private/tmp/second"]]
        for argv in invalid { XCTAssertThrowsError(try CLI.parse(argv), "\(argv)") }
    }

    func testClosedGrammarRejectsRepeatedSlashAliasesInEveryCallerPathSlot() throws {
        let digest = String(repeating: "a", count: 64)
        let invalid = [
            ["materialize", "--input-bundle", "/private//tmp/input-bundle"],
            ["materialize", "--input-bundle", "/private/tmp/./input-bundle"],
            ["materialize", "--input-bundle", "/private/tmp/a/../input-bundle"],
            ["materialize", "--input-bundle", digest]
        ]

        for argv in invalid { XCTAssertThrowsError(try CLI.parse(argv), "\(argv)") }
    }
}
