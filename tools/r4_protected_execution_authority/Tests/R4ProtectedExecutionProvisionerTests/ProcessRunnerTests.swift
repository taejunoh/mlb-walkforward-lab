import XCTest
@testable import R4ProtectedExecutionProvisioner

final class ProcessRunnerTests: XCTestCase {
    func testProductionWaitpidReaperUsesNonblockingFlagAndMapsRawResults() {
        let cases: [(raw: RawWaitpidResult, expected: SpawnLeaderReapResult)] = [
            (RawWaitpidResult(returnedPID: 4242, status: 7 << 8, error: 0), .reaped(7 << 8)),
            (RawWaitpidResult(returnedPID: 0, status: 0, error: 0), .stillRunning),
            (RawWaitpidResult(returnedPID: -1, status: 0, error: EINTR), .interrupted),
            (RawWaitpidResult(returnedPID: -1, status: 0, error: ECHILD), .noChild),
            (RawWaitpidResult(returnedPID: -1, status: 0, error: EINVAL), .failure),
        ]

        for testCase in cases {
            var invocation: (pid_t, Int32)?
            let result = ProductionWaitpidReaper.reapLeaderForTesting(pid: 4242) { pid, flags in
                invocation = (pid, flags)
                return testCase.raw
            }
            XCTAssertEqual(invocation?.0, 4242)
            XCTAssertEqual(invocation?.1, Int32(WNOHANG))
            XCTAssertEqual(result, testCase.expected)
        }
    }

    func testFinalReapStillRunningForeverAfterKillIsFiniteAndExplicit() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(
            reads: [.failure], reaps: Array(repeating: .stillRunning, count: 65),
            groups: [.exists, .exists, .failure], exhaustedGroupResult: .failure
        )

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processReapIndeterminate)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders.count, 64)
        XCTAssertEqual(syscalls.sleeps, Array(repeating: 10_000_000, count: 64))
        guard let kill = syscalls.events.lastIndex(of: "kill"), let reap = syscalls.events.firstIndex(of: "reap") else {
            return XCTFail("cleanup must kill before its first final-reap poll")
        }
        XCTAssertLessThan(kill, reap)
        XCTAssertEqual(syscalls.events.last, "reap")
    }

    func testFinalReapStillRunningThenReapedReturnsOrdinarySuccessAfterOnePoll() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile], observations: [.exited(0)],
                                              reaps: [.stillRunning, .reaped(0)])

        XCTAssertEqual(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table),
                       ProcessResult(status: 0, output: []))
        XCTAssertEqual(syscalls.reapedLeaders, [4242, 4242])
        XCTAssertEqual(syscalls.sleeps, [10_000_000])
        XCTAssertTrue(syscalls.signals.isEmpty)
    }

    func testFinalReapRawNonzeroWaitStatusPreservesOriginalNonzeroExit() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        // waitid reports an exit code while waitpid reports the encoded raw
        // status.  The final reap must complete before returning nonzeroExit.
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile], observations: [.exited(7)],
                                              reaps: [.reaped(7 << 8)])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .nonzeroExit)
        }
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertTrue(syscalls.signals.isEmpty)
    }

    func testExpiredFinalReapDeadlineIsExplicitWithoutAnotherPoll() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(
            reads: [.failure], reaps: [.stillRunning], groups: [.absent],
            clock: [0, 0, 0, 3_000_000_000]
        )

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processReapIndeterminate)
        }
        XCTAssertTrue(syscalls.reapedLeaders.isEmpty)
        XCTAssertTrue(syscalls.sleeps.isEmpty)
        XCTAssertTrue(syscalls.signals.isEmpty)
    }

    func testPersistentFinalReapInterruptionIsFiniteAfterKill() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(
            reads: [.failure], reaps: Array(repeating: .interrupted, count: 64) + [.reaped(0)],
            groups: [.exists, .exists, .absent],
            clock: [0, 0, 0, 1_000_000_000, 1_000_000_000]
        )

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processReapIndeterminate)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders.count, 64)
        XCTAssertEqual(syscalls.sleeps, Array(repeating: 10_000_000, count: 64))
        XCTAssertLessThan(syscalls.events.lastIndex(of: "kill")!, syscalls.events.firstIndex(of: "reap")!)
    }

    func testFinalReapRetriesInterruptedWaitpidThenReturnsOrdinarySuccess() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile], observations: [.exited(0)],
                                              reaps: [.interrupted, .reaped(0)])

        XCTAssertEqual(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table),
                       ProcessResult(status: 0, output: []))
        XCTAssertEqual(syscalls.reapedLeaders, [4242, 4242])
        XCTAssertEqual(syscalls.sleeps, [10_000_000])
        XCTAssertTrue(syscalls.signals.isEmpty)
    }

    func testFinalReapHardFailureIsExplicitInsteadOfOriginalTerminalFailure() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.failure], reaps: [.failure])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processReapIndeterminate)
        }
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertTrue(syscalls.sleeps.isEmpty)
        XCTAssertTrue(syscalls.signals.isEmpty)
    }

    func testFinalReapNoChildAfterWNOWAITIsExplicitlyIndeterminate() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.failure], reaps: [.noChild])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processReapIndeterminate)
        }
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertTrue(syscalls.sleeps.isEmpty)
        XCTAssertTrue(syscalls.signals.isEmpty)
    }

    func testPersistentPreTermProbeInterruptionStillEscalatesAndReapsExactlyOnce() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        // A pre-TERM probe that cannot distinguish absence from existence must
        // not suppress termination.  The grace phase remains live through its
        // shared deadline, so cleanup must KILL; only the post-KILL absence
        // makes the original launch failure reportable.
        let syscalls = RecordingSpawnSyscalls(
            reads: [.failure],
            groups: Array(repeating: .interrupted, count: 64)
                + Array(repeating: .exists, count: 64) + [.absent]
        )

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .readFailed)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertEqual(syscalls.probedGroups.count, 129)
        XCTAssertEqual(syscalls.events.last, "reap")
    }

    func testPersistentTermGraceProbeInterruptionEscalatesThenReapsExactlyOnce() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(
            reads: [.failure],
            groups: [.exists] + Array(repeating: .interrupted, count: 64) + [.absent]
        )

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .readFailed)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertEqual(syscalls.probedGroups.count, 66)
        XCTAssertEqual(syscalls.events.last, "reap")
    }

    func testIndeterminatePostKillProbeMapsReapFailureToReapIndeterminate() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(
            reads: [.failure], reaps: [.failure],
            groups: [.exists, .exists, .failure],
            clock: [0, 0, 0, 1_000_000_000, 1_000_000_000]
        )

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processReapIndeterminate)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertEqual(syscalls.events.last, "reap")
    }

    func testPersistentPostKillProbeFailureReapsThenSurfacesExplicitCleanupFailure() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        // A hard failure remains persistent; cleanup must still finish after
        // its bounded post-KILL decision and must not claim group absence.
        let syscalls = RecordingSpawnSyscalls(reads: [.failure],
                                              groups: [.exists, .exists, .failure], exhaustedGroupResult: .failure,
                                              clock: [0, 0, 0, 1_000_000_000, 1_000_000_000])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processCleanupIndeterminate)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertEqual(syscalls.probedGroups.count, 3)
        XCTAssertFalse(syscalls.events.contains("group:absent"))
        XCTAssertEqual(syscalls.events.last, "reap")
    }

    func testPostKillInterruptedProbeExhaustionIsFiniteAndReapedOnce() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.failure],
                                              groups: [.exists, .exists]
                                                  + Array(repeating: .interrupted, count: 128) + [.absent],
                                              clock: [0, 0, 0, 1_000_000_000, 1_000_000_000]
                                                  + Array(repeating: 1_000_000_000, count: 128))

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .processCleanupIndeterminate)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertEqual(syscalls.probedGroups.count, 65)
        XCTAssertFalse(syscalls.events.contains("group:absent"))
    }

    func testPostKillProbeEventuallyGoneReturnsOriginalTerminalFailureAfterReap() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.failure],
                                              groups: [.exists, .exists, .interrupted, .exists, .absent],
                                              clock: [0, 0, 0, 1_000_000_000, 1_000_000_000]
                                                  + Array(repeating: 1_000_000_000, count: 8))

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .readFailed)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertLessThan(syscalls.events.lastIndex(of: "group:absent")!, syscalls.events.lastIndex(of: "reap")!)
    }

    func testExitedLeaderWithTermIgnoringDescendantKillsGroupBeforeFinalReap() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        // The child leader has exited, but its process group remains alive
        // because a descendant ignores TERM.  A leader-only wait must not
        // suppress group KILL or make the PGID reusable first.
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile], observations: [.exited(0)], reaps: [.reaped(0)],
                                              groups: [.exists, .exists, .absent],
                                              clock: [0, 0, 0, 1_000_000_000, 1_000_000_000])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table))
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertLessThan(syscalls.events.lastIndex(of: "group:absent")!, syscalls.events.lastIndex(of: "reap")!)
    }

    func testReadFailureAfterLeaderExitTerminatesGroupBeforeReapingLeader() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.wouldBlock, .failure], observations: [.exited(0)],
                                              groups: [.exists, .exists, .absent],
                                              clock: [0, 0, 0, 1_000_000_000, 1_000_000_000])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .readFailed)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertLessThan(syscalls.events.lastIndex(of: "group:absent")!, syscalls.events.lastIndex(of: "reap")!)
    }

    func testTimeoutAfterLeaderExitRetriesInterruptedGroupProbeBeforeKillAndReap() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.wouldBlock, .wouldBlock], observations: [.exited(0)],
                                              groups: [.exists, .interrupted, .exists, .absent],
                                              clock: [0, 0, 300_000_000_000, 300_000_000_000,
                                                      300_000_000_000, 301_000_000_000, 301_000_000_000])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .timeout)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertLessThan(syscalls.events.lastIndex(of: "group:absent")!, syscalls.events.lastIndex(of: "reap")!)
    }

    func testSpawnAuthorizationCannotBeReusedAfterFirstSpawn() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile])

        _ = try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)
        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table))
        XCTAssertEqual(syscalls.requests.count, 1)
    }

    func testAlteredCapabilityBoundCommandFieldsNeverReachSpawn() throws {
        let mutations: [(ActualChildCommand) -> ActualChildCommand] = [
            { self.replacing($0, profile: "(deny default)") },
            { command in var argv = command.argv; argv[4] = "-X"; return self.replacing(command, argv: argv) },
            { command in var environment = command.environment; environment[0] = "HOME=/tmp"; return self.replacing(command, environment: environment) },
            { command in self.replacing(command, interpreterSHA256: command.sandboxLauncherSHA256) }
        ]
        for mutate in mutations {
            let prepared = try lifecycleFixture()
            defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
            let authorization = try prepared.capability.issueTestAuthorization(for: .preInstall, mutating: mutate)
            let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile])

            XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table))
            XCTAssertEqual(syscalls.requests.count, 0)
        }
    }

    func testCopiedAuthorizationHasOneAtomicUseAcrossConcurrentCallers() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let copied = authorization
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile])
        let resultLock = NSLock(); var successes = 0; var failures = 0
        DispatchQueue.concurrentPerform(iterations: 2) { index in
            do {
                _ = try ProcessRunner.spawnPinnedLauncher(index == 0 ? authorization : copied, syscalls: syscalls.table)
                resultLock.lock(); successes += 1; resultLock.unlock()
            } catch {
                resultLock.lock(); failures += 1; resultLock.unlock()
            }
        }
        XCTAssertEqual(successes, 1)
        XCTAssertEqual(failures, 1)
        XCTAssertEqual(syscalls.requests.count, 1)
    }

    func testCWDNameReplacementOrABACloneFailsBeforeSpawn() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let runtime = prepared.fixture.stageRuntimeURL
        let original = runtime.deletingLastPathComponent().appendingPathComponent("runtime-original")
        let replacement = runtime.deletingLastPathComponent().appendingPathComponent("runtime-cloned")
        try FileManager.default.copyItem(at: runtime, to: replacement)
        try FileManager.default.moveItem(at: runtime, to: original)
        try FileManager.default.moveItem(at: replacement, to: runtime)
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table))
        XCTAssertEqual(syscalls.requests.count, 0)
    }

    func testSpawnSyscallsUseClosedDescriptorsAndAcceptExactOutputCap() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let command = authorization.testCommand; let cwd = authorization.testCWDPath; let cwdFD = authorization.testCWDFD
        let syscalls = RecordingSpawnSyscalls(reads: [.bytes(Array(repeating: 7, count: 1_000_000)), .endOfFile],
                                              observations: [.interrupted, .running, .exited(0)])

        let result = try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)

        XCTAssertEqual(result.output.count, 1_000_000)
        XCTAssertEqual(syscalls.requests.count, 1)
        XCTAssertEqual(syscalls.requests[0].command, command)
        XCTAssertEqual(syscalls.requests[0].cwd, cwd)
        XCTAssertEqual(syscalls.requests[0].fileActions, [
            .duplicate(source: 11, destination: STDIN_FILENO),
            .duplicate(source: 13, destination: STDOUT_FILENO),
            .duplicate(source: 13, destination: STDERR_FILENO),
            .changeDirectory(fd: cwdFD),
            .close(fd: 12), .close(fd: 13), .close(fd: 11), .close(fd: cwdFD)
        ])
        XCTAssertEqual(syscalls.requests[0].attributes,
                       SpawnAttributes(flags: Int32(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT), processGroup: 0))
        XCTAssertEqual(syscalls.closed.sorted(), [11, 12, 13])
        XCTAssertTrue(syscalls.closeOnExec.contains(12))
        XCTAssertTrue(syscalls.closeOnExec.contains(13))
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertLessThan(syscalls.events.lastIndex(of: "group:absent")!, syscalls.events.lastIndex(of: "reap")!)
    }

    func testSpawnSyscallsRejectCapPlusOneAndReapsProcessGroupAfterEintr() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.bytes(Array(repeating: 7, count: 1_000_001))],
                                              groups: [.exists, .exists, .absent],
                                              clock: [0, 0, 1_000_000_000, 1_000_000_000])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .outputLimit)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertEqual(syscalls.closed.sorted(), [11, 12, 13])
    }

    func testSpawnSyscallsFailsReadErrorAndStillTerminatesAndReapsLeader() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.failure])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .readFailed)
        }
        XCTAssertTrue(syscalls.signals.isEmpty)
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
        XCTAssertLessThan(syscalls.events.lastIndex(of: "group:absent")!, syscalls.events.lastIndex(of: "reap")!)
    }

    func testSpawnSyscallsTimesOutWithNoOutputAndKillsTheWholeProcessGroup() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.wouldBlock], groups: [.exists, .exists, .absent],
                                              clock: [0, 300_000_000_000, 300_000_000_000,
                                                      300_000_000_000, 301_000_000_000, 301_000_000_000])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .timeout)
        }
        XCTAssertEqual(syscalls.signals.map { [$0.0, $0.1] }, [[4242, SIGTERM], [4242, SIGKILL]])
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
    }

    func testSpawnSyscallsRejectsNonzeroExit() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let authorization = try ProcessRunner.pipPlan(prepared.capability)
        let syscalls = RecordingSpawnSyscalls(reads: [.endOfFile], observations: [.exited(256)], reaps: [.reaped(256)])

        XCTAssertThrowsError(try ProcessRunner.spawnPinnedLauncher(authorization, syscalls: syscalls.table)) { error in
            XCTAssertEqual(error as? ProcessRunnerFailure, .nonzeroExit)
        }
        XCTAssertTrue(syscalls.signals.isEmpty)
        XCTAssertEqual(syscalls.reapedLeaders, [4242])
    }

    func testClosedLifecycleReturnsPostProbeExecutionObservationForTask9Consumer() throws {
        let prepared = try lifecycleFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let runtime = prepared.stage + "/runtime/closure/python311"
        let report = "{\"files\":[\"" + runtime + "/a\",\"" + runtime + "/b\",\"" + runtime + "/c\",\"" + runtime + "/d\"],\"paths\":[\"" + runtime + "\"],\"version\":\"3.11.15\"}"
        var launches: [ActualChildCommand] = []

        let completed = try ProcessRunner.installAndProbe(prepared.capability) { authorization in
            try ProcessRunner.spawnPinnedLauncher(authorization) { command in
                launches.append(command)
                return ProcessResult(status: 0, output: launches.count == 1 ? [] : Array(report.utf8))
            }
        }

        XCTAssertEqual(launches, [completed.executionObservation.installerCommand, completed.executionObservation.importCommand])
        XCTAssertEqual(completed.executionObservation.stageRoot, prepared.stage)
        XCTAssertEqual(try completed.executionObservation.interpreterMachOObservation.sha256(), try prepared.machO.sha256())
        XCTAssertEqual(completed.postProbeMachOObservationSHA256, try prepared.machO.sha256())
        XCTAssertEqual(completed.postProbeDependencyClosureSHA256, try SHA256Digest(CanonicalJSON.domainSHA256("R4V2_RUNTIME_DEPENDENCIES\0", prepared.machO.dependencies)))
        XCTAssertEqual(prepared.observer.phases, [.preInstall, .preInstall, .preInstall, .postInstall, .postInstall, .postInstall, .postProbe, .postProbe])
    }

    func testPostProbeDependencyDriftRejectsCompletedEvidence() throws {
        let prepared = try lifecycleFixture(postProbeDependencyDrift: true)
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let runtime = prepared.stage + "/runtime/closure/python311"
        let report = "{\"files\":[\"\(runtime)/a\",\"\(runtime)/b\",\"\(runtime)/c\",\"\(runtime)/d\"],\"paths\":[\"\(runtime)\"],\"version\":\"3.11.15\"}"
        var launches = 0
        XCTAssertThrowsError(try ProcessRunner.installAndProbe(prepared.capability) { authorization in
            try ProcessRunner.spawnPinnedLauncher(authorization) { _ in
                launches += 1; return ProcessResult(status: 0, output: launches == 1 ? [] : Array(report.utf8))
            }
        })
        XCTAssertEqual(launches, 2)
        XCTAssertEqual(prepared.observer.phases.last, .postProbe)
    }

    // Catches a post-probe signature identity change even when the interpreter
    // path and dependency rows are otherwise unchanged.
    func testPostProbeIdentityDriftRejectsCompletedEvidence() throws {
        let prepared = try lifecycleFixture(postProbeIdentityDrift: true)
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let runtime = prepared.stage + "/runtime/closure/python311"
        let report = "{\"files\":[\"\(runtime)/a\",\"\(runtime)/b\",\"\(runtime)/c\",\"\(runtime)/d\"],\"paths\":[\"\(runtime)\"],\"version\":\"3.11.15\"}"
        var launches = 0
        XCTAssertThrowsError(try ProcessRunner.installAndProbe(prepared.capability) { authorization in
            try ProcessRunner.spawnPinnedLauncher(authorization) { _ in
                launches += 1; return ProcessResult(status: 0, output: launches == 1 ? [] : Array(report.utf8))
            }
        })
        XCTAssertEqual(launches, 2)
    }

    // Catches treating an authenticated wheel-installed extension as forbidden
    // merely because it was absent before `pip install`; the unchanged probe
    // observation must be accepted while pre-existing objects remain pinned.
    func testWheelInstalledMachOAdditionIsAllowedButPostProbeMustRemainExact() throws {
        let prepared = try lifecycleFixture(postInstallExtension: true)
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let runtime = prepared.stage + "/runtime/closure/python311"
        let report = "{\"files\":[\"" + runtime + "/a\",\"" + runtime + "/b\",\"" + runtime + "/c\",\"" + runtime + "/d\"],\"paths\":[\"" + runtime + "\"],\"version\":\"3.11.15\"}"
        var launches = 0
        XCTAssertNoThrow(try ProcessRunner.installAndProbe(prepared.capability) { authorization in
            try ProcessRunner.spawnPinnedLauncher(authorization) { _ in
                launches += 1; return ProcessResult(status: 0, output: launches == 1 ? [] : Array(report.utf8))
            }
        })
        XCTAssertEqual(launches, 2)
    }

    func testNoPlanCanBeConstructedWithoutFinalLayoutObservation() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
        let interpreter = try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: copied)
        let layout = try fixture.stage.bindFinalLaunchLayout(interpreter)
        let policy = try MaterializationPolicy.decode(V2Fixture.complete().policy)
        let approval = try Task14ApprovalV2.decode(V2Fixture.complete().approval)
        XCTAssertThrowsError(try VerifiedLaunchCapability(layout: layout, policy: policy, approval: approval))
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    func testRejectsUnheldStagePathsBeforeItCanConstructALaunchCommand() throws {
        let policy = try MaterializationPolicy.decode(V2Fixture.complete().policy)
        for stage in ["relative", "/dev/fd/7", "/private/stage/unpack", "/private/r4-root-owned-stage"] {
            XCTAssertThrowsError(try ProcessRunner.command(policy: policy, stagePath: stage), stage)
        }
    }

    func testConcreteObserverRejectsUntrustedFixtureBeforeAnyCapabilityOrLaunch() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
        let interpreter = try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: copied)
        let layout = try fixture.stage.bindFinalLaunchLayout(interpreter)
        let policy = try MaterializationPolicy.decode(V2Fixture.complete().policy)
        let approval = try Task14ApprovalV2.decode(V2Fixture.complete().approval)

        XCTAssertThrowsError(try VerifiedLaunchCapability(layout: layout, policy: policy, approval: approval,
                                                          observer: DarwinFinalLayoutObserver(), phase: .preInstall))
        XCTAssertEqual(fixture.fakeProcess.launches, [])
    }

    private func lifecycleFixture(postProbeDependencyDrift: Bool = false, postProbeIdentityDrift: Bool = false, postInstallExtension: Bool = false) throws -> (fixture: MaterializationFixture, stage: String, capability: VerifiedLaunchCapability, observer: LifecycleObserver, machO: MachOObservation) {
        let fixture = try MaterializationFixture.valid()
        let copied = try BundleCopier.copy(fixture.validatedBundle, into: fixture.stage)
        let interpreter = try ArchiveInstaller.extractAndPlace(stage: fixture.stage, copied: copied)
        let layout = try fixture.stage.bindFinalLaunchLayout(interpreter)
        let policy = try MaterializationPolicy.decode(V2Fixture.complete().policy)
        let approval = try Task14ApprovalV2.decode(V2Fixture.complete().approval)
        let machO = try MachOObservation.decode(V2Fixture.complete().machOObservation)
        let observer = LifecycleObserver(machO: machO, postProbeDependencyDrift: postProbeDependencyDrift, postProbeIdentityDrift: postProbeIdentityDrift, postInstallExtension: postInstallExtension)
        let capability = try VerifiedLaunchCapability(layout: layout, policy: policy, approval: approval, observer: observer)
        return (fixture, try layout.rebind().stage, capability, observer, machO)
    }

    private func replacing(_ command: ActualChildCommand, argv: [String]? = nil, environment: [String]? = nil,
                           profile: String? = nil, interpreterSHA256: SHA256Digest? = nil) -> ActualChildCommand {
        var nextArgv = argv ?? command.argv
        if let profile { nextArgv[2] = profile }
        return ActualChildCommand(program: command.program, argv: nextArgv, cwd: command.cwd,
                                  environment: environment ?? command.environment, stdin: command.stdin,
                                  stdoutStderr: command.stdoutStderr, timeoutSeconds: command.timeoutSeconds,
                                  maxOutputBytes: command.maxOutputBytes,
                                  sandboxLauncherDescriptorIdentitySHA256: command.sandboxLauncherDescriptorIdentitySHA256,
                                  sandboxLauncherSHA256: command.sandboxLauncherSHA256,
                                  sandboxLauncherCodesignIdentitySHA256: command.sandboxLauncherCodesignIdentitySHA256,
                                  interpreterSHA256: interpreterSHA256 ?? command.interpreterSHA256,
                                  interpreterMachOSHA256: command.interpreterMachOSHA256,
                                  interpreterCodesignIdentitySHA256: command.interpreterCodesignIdentitySHA256,
                                  sandboxProfileSHA256: command.sandboxProfileSHA256)
    }

}

private final class LifecycleObserver: FinalLayoutObserver {
    let machO: MachOObservation; let postProbeDependencyDrift: Bool; let postProbeIdentityDrift: Bool; let postInstallExtension: Bool
    private(set) var phases: [LaunchObservationPhase] = []
    init(machO: MachOObservation, postProbeDependencyDrift: Bool, postProbeIdentityDrift: Bool = false, postInstallExtension: Bool = false) { self.machO = machO; self.postProbeDependencyDrift = postProbeDependencyDrift; self.postProbeIdentityDrift = postProbeIdentityDrift; self.postInstallExtension = postInstallExtension }
    func verify(layout: HeldFinalLaunchLayout, policy: MaterializationPolicy, approval: Task14ApprovalV2, phase: LaunchObservationPhase) throws -> FinalLaunchObservation {
        phases.append(phase)
        let observed: MachOObservation
        if phase == .postProbe && postProbeDependencyDrift {
            observed = MachOObservation(path: machO.path, slices: machO.slices, dependencies: .array([MachODependency(loader: machO.path, dependency: "/usr/lib/libchanged.dylib", resolved: "/usr/lib/libchanged.dylib").canonicalValue]))
        } else { observed = machO }
        let staged = try StagedMachOObservation(path: observed.path,
                                                descriptorIdentitySHA256: SHA256Digest(String(repeating: phase == .postProbe && postProbeIdentityDrift ? "d" : "a", count: 64)),
                                                sha256: SHA256Digest(String(repeating: "b", count: 64)),
                                                codesignIdentitySHA256: SHA256Digest(String(repeating: "c", count: 64)),
                                                machOObservation: observed)
        var objects = [staged]
        if postInstallExtension && phase != .preInstall {
            let extensionObservation = MachOObservation(path: "runtime/closure/python311/lib/wheel-extension.dylib", slices: observed.slices, dependencies: .array([]))
            objects.append(try StagedMachOObservation(path: extensionObservation.path, descriptorIdentitySHA256: SHA256Digest(String(repeating: "d", count: 64)), sha256: SHA256Digest(String(repeating: "e", count: 64)), codesignIdentitySHA256: SHA256Digest(String(repeating: "f", count: 64)), machOObservation: extensionObservation))
        }
        return FinalLaunchObservation(layout: try layout.rebindDescriptors(), interpreterMachOObservation: observed, stagedMachOObservations: objects)
    }
}

private final class RecordingSpawnSyscalls {
    private var reads: [SpawnReadResult]
    private var leaderObservations: [SpawnLeaderObservation]
    private var leaderReaps: [SpawnLeaderReapResult]
    private var groupExistence: [SpawnGroupProbeResult]
    private let exhaustedGroupResult: SpawnGroupProbeResult
    private(set) var requests: [SpawnRequest] = []
    private(set) var closed: [Int32] = []
    private(set) var closeOnExec: [Int32] = []
    private(set) var signals: [(pid_t, Int32)] = []
    private(set) var observedLeaders: [pid_t] = []
    private(set) var reapedLeaders: [pid_t] = []
    private(set) var probedGroups: [pid_t] = []
    private(set) var sleeps: [UInt64] = []
    private var now: UInt64 = 0
    private var clockValues: [UInt64]
    private let clockStep: UInt64
    private(set) var events: [String] = []

    init(reads: [SpawnReadResult], observations: [SpawnLeaderObservation] = [.exited(0)],
         reaps: [SpawnLeaderReapResult] = [.reaped(0)],
         groups: [SpawnGroupProbeResult] = [.absent], exhaustedGroupResult: SpawnGroupProbeResult = .absent,
         clock: [UInt64] = [], clockStep: UInt64 = 0) {
        self.reads = reads; leaderObservations = observations; leaderReaps = reaps; groupExistence = groups
        self.exhaustedGroupResult = exhaustedGroupResult; clockValues = clock; self.clockStep = clockStep
    }

    var table: SpawnSyscalls {
        SpawnSyscalls(openNull: { 11 }, makePipe: { (12, 13) }, setCloseOnExec: { self.closeOnExec.append($0) },
                      spawn: { request in self.requests.append(request); return 4242 }, close: { self.closed.append($0) },
                      read: { _, _ in self.reads.isEmpty ? .endOfFile : self.reads.removeFirst() },
                      observeLeader: { pid in self.observedLeaders.append(pid); self.events.append("observe"); return self.leaderObservations.isEmpty ? .exited(0) : self.leaderObservations.removeFirst() },
                      reapLeader: { pid in self.reapedLeaders.append(pid); self.events.append("reap"); return self.leaderReaps.isEmpty ? .reaped(0) : self.leaderReaps.removeFirst() },
                      probeGroup: { pid in
                          self.probedGroups.append(pid)
                          let result = self.groupExistence.isEmpty ? self.exhaustedGroupResult : self.groupExistence.removeFirst()
                          switch result { case .exists: self.events.append("group:exists"); case .absent: self.events.append("group:absent"); case .interrupted: self.events.append("group:eintr"); case .failure: self.events.append("group:failure") }
                          return result
                      },
                      signalGroup: { pid, signal in self.signals.append((pid, signal)); self.events.append(signal == SIGTERM ? "term" : "kill") },
                      nowNanoseconds: { self.clock() }, sleepNanoseconds: { amount in self.sleeps.append(amount); self.now += amount })
    }

    private func clock() -> UInt64 {
        if !clockValues.isEmpty { return clockValues.removeFirst() }
        defer { now += clockStep }; return now
    }
}
