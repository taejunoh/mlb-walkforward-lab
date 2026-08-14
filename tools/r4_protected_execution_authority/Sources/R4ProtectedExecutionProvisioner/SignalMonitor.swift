import Darwin
import Dispatch
import Foundation

enum SignalMonitorFailure: Error, Equatable { case alreadyOwned }

/// Process-global signal ownership. The dispatch handler only stores a pending signal; all
/// filesystem work remains in the caller's normal control flow.
final class MaterializationSignalLatch {
    private static let ownershipLock = NSLock()
    private static var owned = false
    private let lock = NSLock()
    private var pending: StateMachine.Seam?
    private let interrupt: DispatchSourceSignal
    private let terminate: DispatchSourceSignal
    private let previousInterrupt: sig_t?
    private let previousTerminate: sig_t?
    private var stopped = false

    init() throws {
        Self.ownershipLock.lock()
        guard !Self.owned else { Self.ownershipLock.unlock(); throw SignalMonitorFailure.alreadyOwned }
        Self.owned = true
        Self.ownershipLock.unlock()
        previousInterrupt = signal(SIGINT, SIG_IGN)
        previousTerminate = signal(SIGTERM, SIG_IGN)
        interrupt = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
        terminate = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
        interrupt.setEventHandler { [weak self] in self?.set(.sigint) }
        terminate.setEventHandler { [weak self] in self?.set(.sigterm) }
        interrupt.resume(); terminate.resume()
    }
    deinit { stop() }
    func stop() {
        lock.lock(); defer { lock.unlock() }
        guard !stopped else { return }
        stopped = true; interrupt.cancel(); terminate.cancel()
        Self.ownershipLock.lock()
        if Self.owned { _ = signal(SIGINT, previousInterrupt); _ = signal(SIGTERM, previousTerminate); Self.owned = false }
        Self.ownershipLock.unlock()
    }
    private func set(_ seam: StateMachine.Seam) { lock.lock(); pending = seam; lock.unlock() }
    func takePending() -> StateMachine.Seam? { lock.lock(); defer { lock.unlock() }; let value = pending; pending = nil; return value }
    func terminalizeIfSignalled(store: DurableStateStore, admission: Admission) throws {
        guard takePending() != nil else { return }
        do { try store.quarantineAfterFailure(admission) }
        catch { throw StateFailure.terminalizationFailed }
    }
}
