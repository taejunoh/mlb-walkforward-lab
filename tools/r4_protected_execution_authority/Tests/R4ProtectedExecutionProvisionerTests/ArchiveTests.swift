import XCTest
import Darwin
@testable import R4ProtectedExecutionProvisioner

final class ArchiveTests: XCTestCase {
    func testCopyRebindsEveryAcceptedBundleObject() throws {
        var physicallySwappedPaths = Set<String>()
        for seam in CopySeam.allCases {
            let fixture = try MaterializationFixture.valid().swapBefore(seam)
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            XCTAssertNotNil(fixture.lastCopySwapPath)
            physicallySwappedPaths.insert(fixture.lastCopySwapPath!)
            XCTAssertThrowsError(try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage))
            XCTAssertEqual(fixture.fakeProcess.launches, [])
        }
        XCTAssertEqual(physicallySwappedPaths.count, CopySeam.allCases.count)
    }

    func testCopyRejectsMetadataOnlySourceMutationAndCreatesPrivateDestinationMetadata() throws {
        let valid = try MaterializationFixture.valid()
        defer { try? valid.removeOnlySelfOwnedFixture() }
        let source = try XCTUnwrap(valid.validatedBundle.held.source(at: "artifacts/python.tar.gz"))
        let sourceMetadata = try POSIX.metadata(source)
        XCTAssertNotEqual(sourceMetadata.uid, HeldMaterializationStage.productionDestinationOwner.uid)
        XCTAssertEqual(HeldMaterializationStage.productionDestinationOwner, DestinationOwner(uid: 0, gid: 0))
        var appliedOwners = Set<DestinationOwner>()
        let operations = CopyOperations(configureDestination: { descriptor, owner, directory in
            appliedOwners.insert(owner)
            guard fchown(descriptor.fd, owner.uid, owner.gid) == 0 else { throw POSIXFailure.system(errno) }
            guard fchmod(descriptor.fd, directory ? 0o700 : 0o600) == 0 else { throw POSIXFailure.system(errno) }
        })
        let copied = try BundleCopier.copy(valid.validatedBundle, into: valid.stage, operations: operations)
        XCTAssertEqual(appliedOwners, Set([valid.stage.destinationOwner]))
        let artifacts = try POSIX.openAt(copied.input, name: "artifacts", flags: O_RDONLY | O_DIRECTORY)
        let destination = try POSIX.openAt(artifacts, name: copied.archiveName, flags: O_RDONLY)
        let destinationMetadata = try POSIX.metadata(destination)
        XCTAssertEqual(destinationMetadata.type, sourceMetadata.type)
        XCTAssertEqual(destinationMetadata.uid, valid.stage.destinationOwner.uid)
        XCTAssertEqual(destinationMetadata.gid, valid.stage.destinationOwner.gid)
        XCTAssertEqual(destinationMetadata.mode, 0o600)
        XCTAssertEqual(destinationMetadata.nlink, 1)
        XCTAssertEqual(destinationMetadata.size, sourceMetadata.size)
        XCTAssertEqual(valid.fakeProcess.launches, [])

        let changed = try MaterializationFixture.valid()
        defer { try? changed.removeOnlySelfOwnedFixture() }
        let held = try XCTUnwrap(changed.validatedBundle.held.source(at: "artifacts/python.tar.gz"))
        let original = try POSIX.metadata(held)
        XCTAssertEqual(fchmod(held.fd, original.mode ^ 0o100), 0)
        XCTAssertThrowsError(try BundleCopier.copy(changed.validatedBundle, into: changed.stage))
        XCTAssertEqual(changed.fakeProcess.launches, [])
    }

    func testGzipStreamsLargeSpoolAndRejectsConfiguredCap() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let payload = [UInt8](repeating: 0x61, count: Gzip.ioChunkSize * 4 + 17)
        let compressedBytes = Array(FixtureTarGzip.gzip(payload))
        let root = try POSIX.openDirectory(fixture.root.path)
        let compressed = try POSIX.createExclusive(root, name: "large-stream.gz", mode: 0o600)
        try POSIX.writeAll(compressed, compressedBytes)
        let source = try compressed.rebind(in: root, name: "large-stream.gz")

        let spool = try Gzip.inflate(source, into: fixture.stage)
        XCTAssertGreaterThan(payload.count, Gzip.ioChunkSize)
        XCTAssertEqual(try POSIX.metadata(spool).size, Int64(payload.count))

        let capRootURL = fixture.root.appendingPathComponent("cap-stage", isDirectory: true)
        try FileManager.default.createDirectory(at: capRootURL, withIntermediateDirectories: false)
        let capStage = try HeldMaterializationStage.makeDisposableForTest(rootURL: capRootURL)
        XCTAssertThrowsError(try Gzip.inflate(source, into: capStage, maximumOutputBytes: UInt64(Gzip.ioChunkSize)))
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    func testArchiveRenameUsesFinalLayoutBeforeAnyInspection() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
        var events: [ArchiveEvent] = []
        let finalInterpreter = try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: copied) { events.append($0) }
        XCTAssertEqual(Array(events.suffix(5)), [.rebindSource, .rename, .fsyncUnpack, .fsyncRuntime, .rebindDestination])
        XCTAssertEqual(events, [.fsyncRuntimeParent, .rebindClosure, .rebindSource, .rename, .fsyncUnpack, .fsyncRuntime, .rebindDestination])
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: fixture.stageRuntimeURL.appendingPathComponent("closure/python311/bin/python3.11").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.stageRoot.appendingPathComponent("unpack/python").path))
        XCTAssertEqual(finalInterpreter.relativePath, "runtime/closure/python311")
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    // Production change named by this test: archive unpack/spool residue belongs to the held
    // scratch subtree, never the publish-stage root that canonicalization later validates.
    func testArchiveEphemeraIsConfinedToHeldScratchBeforeCanonicalization() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)

        _ = try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: copied)

        XCTAssertFalse((try POSIX.directoryEntryNames(fixture.stage.root)).contains("unpack"))
        XCTAssertFalse((try POSIX.directoryEntryNames(fixture.stage.root)).contains("archive.tar"))
        XCTAssertEqual(try POSIX.directoryEntryNames(fixture.stage.scratch),
                       Set(["input", "unpack", "archive.tar"]))
        XCTAssertNotNil(try? POSIX.openAt(fixture.stage.scratch, name: "unpack", flags: O_RDONLY | O_DIRECTORY))
        XCTAssertNotNil(try? POSIX.openAt(fixture.stage.scratch, name: "archive.tar", flags: O_RDONLY))
    }

    func testArchiveRejectsTraversalAndLinkHeadersBeforeFinalRename() throws {
        for archiveBytes in [FixtureTarGzip.member(name: "../escape", type: 48, bytes: [1]), FixtureTarGzip.member(name: "python", type: 50, bytes: [])] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
            let artifacts = try POSIX.openAt(copied.input, name: "artifacts", flags: O_RDONLY | O_DIRECTORY)
            try POSIX.unlinkAt(artifacts, name: copied.archiveName)
            let replacement = try POSIX.createExclusive(artifacts, name: copied.archiveName, mode: 0o600)
            try POSIX.writeAll(replacement, Array(archiveBytes))
            let malicious = CopiedBundle(input: copied.input, archive: replacement, archiveName: copied.archiveName)
            XCTAssertThrowsError(try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: malicious))
            XCTAssertFalse(FileManager.default.fileExists(atPath: fixture.stageRuntimeURL.appendingPathComponent("closure/python311").path))
            XCTAssertEqual(fixture.fakeProcess.launches, [])
        }
    }

    func testGzipRejectsMalformedAndAmbiguousStreams() throws {
        let valid = Array(FixtureTarGzip.gzip(FixtureTarGzip.tar([("python/", 53, [], 0o700)])))
        var reserved = valid; reserved[3] = 0x20
        for malformed in [[UInt8]([0x1f, 0x8b, 0x08]), Array(valid.dropLast()), valid + valid, valid + [0]] {
            try assertGzipRejected(malformed)
        }
        try assertGzipRejected(reserved)
    }

    func testTarRejectsClosedHeaderTypeAndPathMatrices() throws {
        let names = ["/absolute", ".", "..", "a//b", "a\\b", "a\u{7f}", "π", "a/../b"]
        for name in names { try assertTarRejected(FixtureTarGzip.tar([(name, 48, [1], 0o600)])) }
        for type in [UInt8(49), 50, 51, 52, 54, 55, 65, 66, 67, 76, 75, 103, 120] {
            try assertTarRejected(FixtureTarGzip.tar([("python", type, [], 0o700)]))
        }
        try assertTarRejected(FixtureTarGzip.tar([("python/a", 48, [1], 0o600), ("python/a", 48, [2], 0o600)]))
        try assertTarRejected(FixtureTarGzip.tar([("python/A", 48, [1], 0o600), ("python/a", 48, [2], 0o600)]))
        try assertTarRejected(FixtureTarGzip.tar([("python", 48, [1], 0o600), ("python/bin", 48, [2], 0o600)]))
    }

    func testTarRejectsChecksumNumericPaddingAndTerminatorAmbiguity() throws {
        let base = FixtureTarGzip.tar([("python", 48, [1], 0o600)])
        var checksum = base; checksum[0] ^= 1; try assertTarRejected(checksum)
        var numeric = base; numeric[124] = 56; try assertTarRejected(numeric)
        var oversized = base; for index in 124..<136 { oversized[index] = 55 }; oversized[135] = 0; try assertTarRejected(oversized)
        try assertTarRejected(Array(base.dropLast(1024)))
        var trailing = base; trailing.append(1); try assertTarRejected(trailing)
    }

    func testInstallerRejectsMissingEntrypointAndExistingDestination() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
        let artifacts = try POSIX.openAt(copied.input, name: "artifacts", flags: O_RDONLY | O_DIRECTORY)
        try POSIX.unlinkAt(artifacts, name: copied.archiveName)
        let replacement = try POSIX.createExclusive(artifacts, name: copied.archiveName, mode: 0o600)
        try POSIX.writeAll(replacement, Array(FixtureTarGzip.gzip(FixtureTarGzip.tar([("python/", 53, [], 0o700)]))))
        XCTAssertThrowsError(try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: CopiedBundle(input: copied.input, archive: replacement, archiveName: copied.archiveName)))
        XCTAssertEqual(fixture.fakeProcess.launches, [])

        let second = try MaterializationFixture.valid()
        defer { try? second.removeOnlySelfOwnedFixture() }
        let secondCopied = try BundleCopier.copy(second.validatedBundle, into: second.stage)
        try FileManager.default.createDirectory(at: second.stageRuntimeURL.appendingPathComponent("closure/python311"), withIntermediateDirectories: true)
        XCTAssertThrowsError(try ArchiveInstaller.extractAndPlace(stage: second.stage, copied: secondCopied))
        XCTAssertEqual(second.fakeProcess.launches, [])
    }

    func testInstallerDoesNotProceedPastTypedRenameFsyncOrRebindFailure() throws {
        for fault in ["rename", "fsync", "rebind"] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
            let operations = archiveOperations(failing: fault)
            XCTAssertThrowsError(try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: copied, operations: operations), fault)
            XCTAssertEqual(fixture.fakeProcess.launches, [], fault)
        }
    }

    private func assertGzipRejected(_ bytes: [UInt8], file: StaticString = #filePath, line: UInt = #line) throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let fileName = "gzip-\(UUID().uuidString.replacingOccurrences(of: "-", with: ""))"
        let fileDescriptor = try POSIX.createExclusive(root, name: fileName, mode: 0o600)
        try POSIX.writeAll(fileDescriptor, bytes)
        let source = try fileDescriptor.rebind(in: root, name: fileName)
        XCTAssertThrowsError(try Gzip.inflate(source, into: fixture.stage), file: file, line: line)
        XCTAssertEqual(fixture.fakeProcess.launches, [], file: file, line: line)
    }

    private func assertTarRejected(_ bytes: [UInt8], file: StaticString = #filePath, line: UInt = #line) throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let name = "tar-\(UUID().uuidString.replacingOccurrences(of: "-", with: "")).gz"
        let compressed = try POSIX.createExclusive(root, name: name, mode: 0o600)
        try POSIX.writeAll(compressed, Array(FixtureTarGzip.gzip(bytes)))
        let source = try compressed.rebind(in: root, name: name)
        let spool = try Gzip.inflate(source, into: fixture.stage)
        XCTAssertThrowsError(try Tar.validate(spool), file: file, line: line)
        XCTAssertEqual(fixture.fakeProcess.launches, [], file: file, line: line)
    }

    private func archiveOperations(failing seam: String) -> ArchiveOperations {
        ArchiveOperations(renameExclusive: { from, name, to, destination in
            if seam == "rename" { throw ArchiveFailure.unsafeArchive }
            try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
        }, durableDirectory: { descriptor in
            if seam == "fsync" { throw ArchiveFailure.unsafeArchive }
            try POSIX.durableDirectory(descriptor)
        }, rebindDirectory: { descriptor, parent, name in
            if seam == "rebind" { throw ArchiveFailure.unsafeArchive }
            return try descriptor.rebind(in: parent, name: name, directory: true)
        })
    }
}
