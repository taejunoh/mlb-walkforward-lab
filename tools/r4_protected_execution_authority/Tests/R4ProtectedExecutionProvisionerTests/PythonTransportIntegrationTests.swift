import Foundation
import XCTest
@testable import R4ProtectedExecutionProvisioner

final class PythonTransportIntegrationTests: XCTestCase {
    func testPythonBuiltV2BundleAndReviewPackageAreAcceptedAtReviewBoundary() throws {
        let fixture = try MaterializationFixture.valid()
        defer {
            makeFixtureWritable(fixture.root)
            try? fixture.removeOnlySelfOwnedFixture()
        }
        let input = fixture.request.inputBundle
        let reviewPackage = fixture.root.appendingPathComponent("transport-review-package-v2.json")
        let lock = fixture.root.appendingPathComponent("transport-lock.json")
        let policy = fixture.root.appendingPathComponent("transport-policy.json")
        let downloads = fixture.root.appendingPathComponent("transport-downloads", isDirectory: true)
        let parsed = try ProtectedPythonLock.decodeDocument(Array(Data(contentsOf: input.appendingPathComponent("r4_protected_python311.lock.json"))))
        try FileManager.default.copyItem(at: input.appendingPathComponent("r4_protected_python311.lock.json"), to: lock)
        try FileManager.default.copyItem(at: input.appendingPathComponent("materialization-policy.json"), to: policy)
        try FileManager.default.createDirectory(at: downloads, withIntermediateDirectories: false)
        for name in [parsed.python.filename] + parsed.wheels.map(\.filename) {
            try FileManager.default.copyItem(at: input.appendingPathComponent("artifacts/\(name)"), to: downloads.appendingPathComponent(name))
        }
        try FileManager.default.removeItem(at: input)

        let workspace = workspaceRoot()
        let script = #"""
import shutil, subprocess, sys
from pathlib import Path
workspace, lock, policy, downloads, output, helper, review_package = map(Path, sys.argv[1:])
sys.path.insert(0, str(workspace / "scripts"))
from build_r4_protected_candidate import CANONICAL_REQUIRED_BLOB_PATHS, build_input_bundle
from build_r4_task14_approval import PROVISIONER_SOURCE_ROOT, build_task14_review_package_v2
repo = output.parent / "transport-repo"
repo.mkdir()
subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repo, check=True)
subprocess.run(["/usr/bin/git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
subprocess.run(["/usr/bin/git", "config", "user.name", "Test"], cwd=repo, check=True)
for relative in CANONICAL_REQUIRED_BLOB_PATHS:
    destination = repo / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("# sealed transport fixture: " + relative + "\n", encoding="ascii")
for source in (workspace / PROVISIONER_SOURCE_ROOT).rglob("*.swift"):
    destination = repo / PROVISIONER_SOURCE_ROOT / source.relative_to(workspace / PROVISIONER_SOURCE_ROOT)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
subprocess.run(["/usr/bin/git", "add", "."], cwd=repo, check=True)
subprocess.run(["/usr/bin/git", "commit", "-qm", "transport fixture"], cwd=repo, check=True)
build_input_bundle(repo, downloads, lock, policy, output)
helper_root = output.parent / "transport-helper" / "bootstrap"
helper_root.mkdir(parents=True)
shutil.copyfile(helper, helper_root / "provisioner")
(helper_root / "provisioner.codesign-requirement.txt").write_bytes(b"fixture-requirement")
suite = output.parent / "transport-suite.log"; suite.write_bytes(b"fixture-suite")
build_task14_review_package_v2(repo, output, helper_root.parent, suite, review_package)
"""#
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-c", script, workspace.path, lock.path, policy.path, downloads.path, input.path, fixture.helperURL.path, reviewPackage.path]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = workspace.path
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        try process.run()
        process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)

        XCTAssertNoThrow(try InputBundleManifest.decodeDocument(Array(Data(contentsOf: input.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json")))))
        XCTAssertNoThrow(try ProtectedPythonLock.decodeDocument(Array(Data(contentsOf: input.appendingPathComponent("r4_protected_python311.lock.json")))))
        XCTAssertNoThrow(try MaterializationPolicy.decodeDocument(Array(Data(contentsOf: input.appendingPathComponent("materialization-policy.json")))))
        let reviewDocument = try CanonicalJSON.decodeDocument(
            bytes: Array(Data(contentsOf: reviewPackage))
        )
        let review = try Task14ReviewPackageV2.decode(
            String(decoding: try CanonicalJSON.encode(reviewDocument), as: UTF8.self)
        )
        let manifest = try InputBundleManifest.decodeDocument(
            Array(Data(contentsOf: input.appendingPathComponent("R4_RUNTIME_INPUT_BUNDLE_V2.json")))
        )
        XCTAssertEqual(review.inputBundleManifestSHA256.value, manifest.sha256.value)
        XCTAssertEqual(review.task14ApprovalVerificationPublicKeySHA256.value, ProvisionerConstants.task14ApprovalVerificationPublicKeySHA256)
        XCTAssertEqual(fixture.fakeFS.admissionRecords, [])
    }

    private func workspaceRoot() -> URL {
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { root.deleteLastPathComponent() }
        return root
    }

    private func makeFixtureWritable(_ root: URL) {
        let manager = FileManager.default
        guard let enumerator = manager.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey]) else { return }
        for case let item as URL in enumerator {
            try? manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: item.path)
        }
        try? manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: root.path)
    }
}
