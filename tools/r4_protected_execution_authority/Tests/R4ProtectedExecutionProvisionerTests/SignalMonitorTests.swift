import XCTest
@testable import R4ProtectedExecutionProvisioner

final class SignalMonitorTests: XCTestCase {
    func testSignalMonitorHasExclusiveLifecycleAndRestoresOwnershipOnStop() throws {
        let first = try MaterializationSignalLatch()
        XCTAssertThrowsError(try MaterializationSignalLatch()) { error in
            XCTAssertEqual(error as? SignalMonitorFailure, .alreadyOwned)
        }
        first.stop()
        let replacement = try MaterializationSignalLatch()
        replacement.stop()
    }
}
