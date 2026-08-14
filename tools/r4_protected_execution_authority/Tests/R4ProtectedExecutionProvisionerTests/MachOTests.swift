import XCTest
@testable import R4ProtectedExecutionProvisioner

final class MachOTests: XCTestCase {
    func testParsesBoundedDylibAndRPathCommands() throws {
        let image = thin(commands: [command(0xc, "@rpath/libx.dylib"), command(0x8000001c, "@loader_path/../lib")])
        let descriptor = try MachOParser.parse(image)
        XCTAssertEqual(descriptor.dependencies, ["@rpath/libx.dylib"])
        XCTAssertEqual(descriptor.rpaths, ["@loader_path/../lib"])
    }

    func testRejectsMalformedAndAmbiguousOrEscapingLoaderPaths() throws {
        XCTAssertThrowsError(try MachOParser.parse([0xcf, 0xfa, 0xed]))
        let root = "/private/stage/runtime/closure/python311"
        let loader = root + "/bin/python3.11"
        XCTAssertThrowsError(try MachOValidator.resolve("@rpath/libx.dylib", loader: loader, executable: loader, rpaths: ["@loader_path/../lib", "@loader_path/../lib"], finalRuntime: root, exists: { _ in true }))
        for bad in ["/opt/homebrew/lib/x.dylib", "/Users/a/x.dylib", "relative/x.dylib", "@loader_path/../../outside"] {
            XCTAssertThrowsError(try MachOValidator.resolve(bad, loader: loader, executable: loader, rpaths: [], finalRuntime: root, exists: { _ in true }), bad)
        }
    }

    func testResolvesSystemAndSingleHeldRuntimeDependency() throws {
        let root = "/private/stage/runtime/closure/python311", loader = root + "/bin/python3.11"
        XCTAssertEqual(try MachOValidator.resolve("/usr/lib/libSystem.B.dylib", loader: loader, executable: loader, rpaths: [], finalRuntime: root, exists: { $0 == "/usr/lib/libSystem.B.dylib" }), "/usr/lib/libSystem.B.dylib")
        XCTAssertEqual(try MachOValidator.resolve("@rpath/libx.dylib", loader: loader, executable: loader, rpaths: ["@loader_path/../lib"], finalRuntime: root, exists: { $0 == root + "/lib/libx.dylib" }), root + "/lib/libx.dylib")
    }

    func testParsesEverySliceOfAFatMachOInsteadOfSelectingOne() throws {
        let first = thin(commands: [command(0xc, "/usr/lib/libSystem.B.dylib")])
        let second = thin(commands: [command(0xc, "/System/Library/Frameworks/Foundation.framework/Foundation")])
        let image = fat([first, second])

        XCTAssertEqual(try MachOParser.parseSlices(image).map(\.dependencies), [
            ["/usr/lib/libSystem.B.dylib"],
            ["/System/Library/Frameworks/Foundation.framework/Foundation"]
        ])
    }

    // Catches a FAT64 image being treated as a non-Mach-O and therefore
    // escaping the all-slice loader/Security observation gate.
    func testParsesEverySliceOfAFAT64MachO() throws {
        let first = thin(commands: [command(0xc, "/usr/lib/libSystem.B.dylib")])
        let second = thin(commands: [command(0xc, "/System/Library/Frameworks/Foundation.framework/Foundation")])
        XCTAssertEqual(try MachOParser.parseSlices(fat64([first, second])).map(\.dependencies), [
            ["/usr/lib/libSystem.B.dylib"],
            ["/System/Library/Frameworks/Foundation.framework/Foundation"]
        ])
    }

    // Catches accepting a truncated 64-bit offset/size record as an
    // unsupported image.  Such an image must fail closed before launch.
    func testRejectsMalformedFAT64MachO() {
        XCTAssertThrowsError(try MachOParser.parseSlices(be(0xcafebabf, 4) + be(1, 4) + [UInt8](repeating: 0, count: 31)))
        XCTAssertThrowsError(try MachOParser.parseSlices(be(0xcafebabf, 4) + be(1, 4) + be(1, 4) + be(0, 4) + be64(0xffff_ffff_ffff_fff0) + be64(64) + be(0, 4) + be(0, 4)))
    }

    private func thin(commands: [[UInt8]]) -> [UInt8] {
        let size = commands.reduce(0) { $0 + $1.count }; var bytes = le(0xfeedfacf, 4) + [UInt8](repeating: 0, count: 12) + le(UInt32(commands.count), 4) + le(UInt32(size), 4) + [UInt8](repeating: 0, count: 8)
        bytes += commands.flatMap { $0 }; return bytes
    }
    private func command(_ kind: UInt32, _ value: String) -> [UInt8] { let body = Array(value.utf8) + [0]; let size = 12 + body.count; return le(kind, 4) + le(UInt32(size), 4) + le(12, 4) + body }
    private func le(_ value: UInt32, _ count: Int) -> [UInt8] { (0..<count).map { UInt8((value >> UInt32($0 * 8)) & 0xff) } }
    private func be(_ value: UInt32, _ count: Int) -> [UInt8] { (0..<count).reversed().map { UInt8((value >> UInt32($0 * 8)) & 0xff) } }
    private func be64(_ value: UInt64) -> [UInt8] { (0..<8).reversed().map { UInt8((value >> UInt64($0 * 8)) & 0xff) } }
    private func fat(_ slices: [[UInt8]]) -> [UInt8] {
        let headerSize = 8 + slices.count * 20
        var offset = headerSize
        var entries: [[UInt8]] = []
        for (index, slice) in slices.enumerated() {
            entries.append(be(UInt32(index + 1), 4) + be(0, 4) + be(UInt32(offset), 4) + be(UInt32(slice.count), 4) + be(0, 4))
            offset += slice.count
        }
        return be(0xcafebabe, 4) + be(UInt32(slices.count), 4) + entries.flatMap { $0 } + slices.flatMap { $0 }
    }
    private func fat64(_ slices: [[UInt8]]) -> [UInt8] {
        let headerSize = 8 + slices.count * 32
        var offset = headerSize
        var entries: [[UInt8]] = []
        for (index, slice) in slices.enumerated() {
            entries.append(be(UInt32(index + 1), 4) + be(0, 4) + be64(UInt64(offset)) + be64(UInt64(slice.count)) + be(0, 4) + be(0, 4))
            offset += slice.count
        }
        return be(0xcafebabf, 4) + be(UInt32(slices.count), 4) + entries.flatMap { $0 } + slices.flatMap { $0 }
    }
}
