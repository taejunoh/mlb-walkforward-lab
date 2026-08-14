// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "R4ProtectedExecutionProvisioner",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "R4ProtectedExecutionProvisioner", targets: ["R4ProtectedExecutionProvisioner"])],
    targets: [
        .executableTarget(name: "R4ProtectedExecutionProvisioner"),
        .testTarget(
            name: "R4ProtectedExecutionProvisionerTests",
            dependencies: ["R4ProtectedExecutionProvisioner"],
            exclude: ["Fixtures/generate_r4_v2_domain_vectors.py"],
            resources: [.process("Fixtures/r4_v2_domain_vectors.json")]
        ),
    ]
)
