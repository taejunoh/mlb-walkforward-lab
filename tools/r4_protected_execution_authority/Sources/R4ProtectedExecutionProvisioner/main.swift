import Darwin
import Foundation

/// Compatibility value retained for no-argument release behavior.
public enum TemporaryEntrypoint {
    public static let stderr = "ROOT_PRIVILEGE_REQUIRED: CLI_NOT_AVAILABLE\n"
    public static let exitCode: Int32 = 2
}

private func failClosed() -> Never {
    _ = "ROOT_PRIVILEGE_REQUIRED: INVALID_OR_UNAUTHORIZED_REQUEST\n".withCString { fputs($0, Darwin.stderr) }
    exit(2)
}

do {
    switch try CLI.parse(Array(CommandLine.arguments.dropFirst())) {
    case .verify:
        try Verify.active()
    case let .materialize(bundle):
        _ = try Materializer.run(MaterializationRequest(inputBundle: URL(fileURLWithPath: bundle)))
    }
} catch {
    failClosed()
}
