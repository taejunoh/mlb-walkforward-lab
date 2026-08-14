import XCTest
import Darwin
import Foundation
@testable import R4ProtectedExecutionProvisioner

final class StorageTests: XCTestCase {
    private func observations() -> [StoragePreflight.Observation] {
        StoragePreflight.fixedAncestors.enumerated().map { index, path in
            StoragePreflight.Observation(path: path, device: dev_t(index + 1), inode: ino_t(index + 1), filesystem: "apfs", volumeUUID: "123e4567-e89b-12d3-a456-426614174000", local: true, internalVolume: true, removable: false, readOnly: false, ubiquitous: false, xattrs: [])
        }
    }

    func testEveryFixedAncestorRejectsEachStorageSignal() throws {
        try StoragePreflight.validate(observations())
        let mutations: [(String, (StoragePreflight.Observation) -> StoragePreflight.Observation)] = [
            ("filesystem", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: "nfs", volumeUUID: $0.volumeUUID, local: $0.local, internalVolume: $0.internalVolume, removable: $0.removable, readOnly: $0.readOnly, ubiquitous: $0.ubiquitous, xattrs: $0.xattrs) }),
            ("uuid", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", local: $0.local, internalVolume: $0.internalVolume, removable: $0.removable, readOnly: $0.readOnly, ubiquitous: $0.ubiquitous, xattrs: $0.xattrs) }),
            ("url", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: $0.volumeUUID, local: false, internalVolume: $0.internalVolume, removable: $0.removable, readOnly: $0.readOnly, ubiquitous: $0.ubiquitous, xattrs: $0.xattrs) }),
            ("external", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: $0.volumeUUID, local: $0.local, internalVolume: false, removable: $0.removable, readOnly: $0.readOnly, ubiquitous: $0.ubiquitous, xattrs: $0.xattrs) }),
            ("removable", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: $0.volumeUUID, local: $0.local, internalVolume: $0.internalVolume, removable: true, readOnly: $0.readOnly, ubiquitous: $0.ubiquitous, xattrs: $0.xattrs) }),
            ("readOnly", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: $0.volumeUUID, local: $0.local, internalVolume: $0.internalVolume, removable: $0.removable, readOnly: true, ubiquitous: $0.ubiquitous, xattrs: $0.xattrs) }),
            ("ubiquitous", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: $0.volumeUUID, local: $0.local, internalVolume: $0.internalVolume, removable: $0.removable, readOnly: $0.readOnly, ubiquitous: true, xattrs: $0.xattrs) }),
            ("xattr", { .init(path: $0.path, device: $0.device, inode: $0.inode, filesystem: $0.filesystem, volumeUUID: $0.volumeUUID, local: $0.local, internalVolume: $0.internalVolume, removable: $0.removable, readOnly: $0.readOnly, ubiquitous: $0.ubiquitous, xattrs: ["com.apple.fileprovider.test"]) })
        ]
        for (name, mutation) in mutations {
            for index in StoragePreflight.fixedAncestors.indices {
                var bad = observations(); bad[index] = mutation(bad[index])
                XCTAssertThrowsError(try StoragePreflight.validate(bad), "\(name) @ \(index)")
            }
        }
    }

    func testProviderXattrMatchingIsASCIICaseInsensitiveAndNearMissesAreAccepted() throws {
        for name in ["com.apple.FileProvider", "com.apple.FileProvider.Document", "com.apple.iCloud", "com.apple.iCloud.container", "com.apple.fileprovider.document", "com.apple.icloud.container", "com.apple.file-provider-domain-id", "com.apple.file-provider-domain-id.example"] {
            for index in StoragePreflight.fixedAncestors.indices {
                var bad = observations()
                let item = bad[index]
                bad[index] = .init(path: item.path, device: item.device, inode: item.inode, filesystem: item.filesystem, volumeUUID: item.volumeUUID, local: item.local, internalVolume: item.internalVolume, removable: item.removable, readOnly: item.readOnly, ubiquitous: item.ubiquitous, xattrs: [name])
                XCTAssertThrowsError(try StoragePreflight.validate(bad), "\(name) @ \(index)")
            }
        }
        for name in ["com.apple.fileproviderx", "com.apple.icloudx", "com.apple.file-provider-domain-identical"] {
            var accepted = observations()
            let item = accepted[0]
            accepted[0] = .init(path: item.path, device: item.device, inode: item.inode, filesystem: item.filesystem, volumeUUID: item.volumeUUID, local: item.local, internalVolume: item.internalVolume, removable: item.removable, readOnly: item.readOnly, ubiquitous: item.ubiquitous, xattrs: [name])
            XCTAssertNoThrow(try StoragePreflight.validate(accepted), name)
        }
    }

    func testEveryAncestorRejectsDescriptorUUIDAbsenceMalformedOrNSURLContradiction() throws {
        for descriptorUUID in ["not-a-uuid", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"] {
            for index in StoragePreflight.fixedAncestors.indices {
                var bad = observations()
                let item = bad[index]
                bad[index] = .init(path: item.path, device: item.device, inode: item.inode, filesystem: item.filesystem, volumeUUID: item.volumeUUID, descriptorVolumeUUID: descriptorUUID, local: item.local, internalVolume: item.internalVolume, removable: item.removable, readOnly: item.readOnly, ubiquitous: item.ubiquitous, xattrs: item.xattrs)
                XCTAssertThrowsError(try StoragePreflight.validate(bad), "\(String(describing: descriptorUUID)) @ \(index)")
            }
        }
        for index in StoragePreflight.fixedAncestors.indices {
            var bad = observations()
            let item = bad[index]
            bad[index] = .init(path: item.path, device: item.device, inode: item.inode, filesystem: item.filesystem, volumeUUID: item.volumeUUID, descriptorVolumeUUID: item.descriptorVolumeUUID, descriptorUUIDAvailable: false, local: item.local, internalVolume: item.internalVolume, removable: item.removable, readOnly: item.readOnly, ubiquitous: item.ubiquitous, xattrs: item.xattrs)
            XCTAssertThrowsError(try StoragePreflight.validate(bad), "unavailable @ \(index)")
        }
    }

    func testPreflightFailureOccursBeforeAdmission() throws {
        let fixture = try MaterializationFixture.valid().mutating(.badStorage)
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try BundleVerifier.verifyBeforeAdmission(fixture.request, adapters: fixture.adapters))
        XCTAssertEqual(fixture.fakeFS.admissionRecords, [])
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    func testDescriptorAdmissionLockExcludesIndependentOpenDescriptors() throws {
        let fixture = try DarwinDisposableRoot.create(prefix: "r4-task6-flock-")
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let first = try POSIX.openDirectory(fixture.root.path)
        let second = try POSIX.openDirectory(fixture.root.path)
        XCTAssertNoThrow(try AdmissionRegistryLock.withExclusive(first) {
            XCTAssertFalse(try AdmissionRegistryLock.tryExclusive(second))
        })
        XCTAssertTrue(try AdmissionRegistryLock.tryExclusive(second))
        XCTAssertEqual(flock(second.fd, LOCK_UN), 0)
    }
}
