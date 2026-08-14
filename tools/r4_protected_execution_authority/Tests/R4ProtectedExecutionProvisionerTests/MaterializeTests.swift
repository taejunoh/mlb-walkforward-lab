import Darwin
import CryptoKit
import XCTest
@testable import R4ProtectedExecutionProvisioner

final class MaterializeTests: XCTestCase {
    // Production change named by this test: the former prepare-only seam must execute the
    // retained one-shot publication and then hand the same full authority root to Verify.
    func testRunForTestPublishesThenReadOnlyVerifySucceeds() throws {
        let integration = try Self.integratedRunFixture()
        defer { try? integration.fixture.removeOnlySelfOwnedFixture() }

        let receipt: ReceiptV2
        do { receipt = try Materializer.runForTest(integration.fixture.request, adapters: integration.adapters) }
        catch { XCTFail("run state=\(integration.store.currentState) records=\(integration.store.journalRecords.count) journals=\(try! POSIX.directoryEntryNames(integration.adapters.attestation.journals)) error=\(error)"); return }

        let published = try POSIX.openAt(integration.adapters.attestation.authorities,
                                         name: ProtectedExecutionConstants.authorityID, flags: O_RDONLY | O_DIRECTORY)
        XCTAssertEqual(receipt, try ReceiptV2.decode(String(decoding: POSIX.readAll(
            try POSIX.openAt(published, name: "receipt.json", flags: O_RDONLY)), as: UTF8.self)))
        XCTAssertEqual(integration.store.currentState, .published)
        XCTAssertNoThrow(try Verify.activeForTest(authority: integration.authority))
    }

    // Production change named by this test: a successful publish must be read-only verified by
    // the same orchestration call before its receipt becomes observable to the caller.
    func testRunForTestVerifiesPublishedAuthorityBeforeReturningReceipt() throws {
        let integration = try Self.integratedRunFixture()
        defer { try? integration.fixture.removeOnlySelfOwnedFixture() }
        var verificationCount = 0
        Verify.afterAuthorityVerificationHook = { verificationCount += 1 }
        defer { Verify.afterAuthorityVerificationHook = nil }

        _ = try Materializer.runForTest(integration.fixture.request, adapters: integration.adapters)

        XCTAssertEqual(verificationCount, 1)
    }

    // Production change named by this test: a post-publish verification failure is fail-closed;
    // it must not return the receipt or attempt recovery after the single rename has completed.
    func testRunForTestPostPublishVerificationFailureLeavesPublishedAuthorityUntouched() throws {
        let integration = try Self.integratedRunFixture()
        defer { try? integration.fixture.removeOnlySelfOwnedFixture() }
        Verify.afterAuthorityVerificationHook = { throw EvidenceError.invalidValue }
        defer { Verify.afterAuthorityVerificationHook = nil }

        XCTAssertThrowsError(try Materializer.runForTest(integration.fixture.request, adapters: integration.adapters)) { error in
            XCTAssertEqual(error as? MaterializationSealFailure, .postPublishVerification)
        }
        XCTAssertEqual(integration.store.currentState, .published)
        XCTAssertEqual(try POSIX.directoryEntryNames(integration.adapters.attestation.authorities),
                       [ProtectedExecutionConstants.authorityID])
        let journal = try POSIX.openAt(integration.adapters.attestation.journals,
                                       name: "0123456789abcdef0123456789abcdef", flags: O_RDONLY | O_DIRECTORY)
        let modeledNames = try integration.store.journalRecords.map { try JournalRecordV2.decode($0).filename }
        XCTAssertEqual(try POSIX.directoryEntryNames(journal), Set(modeledNames))
        XCTAssertEqual(try POSIX.directoryEntryNames(integration.adapters.attestation.quarantine), [])
    }

    func testRunForTestSecondInvocationRejectsWithoutOverwritingPublishedReceipt() throws {
        let integration = try Self.integratedRunFixture()
        defer { try? integration.fixture.removeOnlySelfOwnedFixture() }
        _ = try Materializer.runForTest(integration.fixture.request, adapters: integration.adapters)
        let authorities = integration.adapters.attestation.authorities
        let root = try POSIX.openAt(authorities, name: ProtectedExecutionConstants.authorityID, flags: O_RDONLY | O_DIRECTORY)
        let receipt = try POSIX.readAll(try POSIX.openAt(root, name: "receipt.json", flags: O_RDONLY))
        let journals = integration.adapters.attestation.journals
        let journal = try POSIX.openAt(journals, name: "0123456789abcdef0123456789abcdef", flags: O_RDONLY | O_DIRECTORY)
        let journalNames = try POSIX.directoryEntryNames(journal)

        XCTAssertThrowsError(try Materializer.runForTest(integration.fixture.request, adapters: integration.adapters))
        XCTAssertEqual(try POSIX.readAll(try POSIX.openAt(root, name: "receipt.json", flags: O_RDONLY)), receipt)
        XCTAssertEqual(try POSIX.directoryEntryNames(journal), journalNames)
        XCTAssertEqual(try POSIX.directoryEntryNames(authorities), [ProtectedExecutionConstants.authorityID])
    }

    func testRevokedSignerRejectsBeforeAdmissionOrStageMutation() throws {
        let keyring = try KeyringFixture.revoked()
        let beforeControl = try POSIX.directoryEntryNames(try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY))
        XCTAssertThrowsError(try KeyringVerifier.loadActiveForTest(keyring.root, keyA: keyring.keyA))
        XCTAssertEqual(try POSIX.directoryEntryNames(try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY)), beforeControl)
        XCTAssertEqual(beforeControl, ["keyring", "revocations"])
    }

    // Production change named by this test: the shared materialization core, not a test cleanup
    // helper, must leave the stage canonical before preparation while preserving its sibling.
    func testSharedMaterializationCoreCanonicalizesStageWithoutTouchingSiblingSentinel() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let sentinel = try POSIX.createExclusive(root, name: "outside-stage-sentinel", mode: 0o600)
        try POSIX.writeAll(sentinel, Array("sentinel".utf8))

        let setup = try Self.makePublicationPreparationFixture(fixture: fixture)

        XCTAssertEqual(try POSIX.directoryEntryNames(setup.inputs.stage.root), Set(["checkout", "runtime"]))
        XCTAssertEqual(try POSIX.readAll(try POSIX.openAt(root, name: "outside-stage-sentinel", flags: O_RDONLY)),
                       Array("sentinel".utf8))
    }

    func testSharedMaterializationCoreRejectsScratchIdentitySwapWithoutTouchingSiblingSentinel() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let root = try POSIX.openDirectory(fixture.root.path)
        let sentinel = try POSIX.createExclusive(root, name: "outside-stage-sentinel", mode: 0o600)
        try POSIX.writeAll(sentinel, Array("sentinel".utf8))

        XCTAssertThrowsError(try Self.makePublicationPreparationFixture(fixture: fixture, afterInstall: { stage in
            let stageRoot = stage.root
            try POSIX.renameExclusive(from: stageRoot, name: "scratch", to: stageRoot, destination: "scratch-held")
            _ = try POSIX.mkdirAt(stageRoot, name: "scratch", mode: 0o700)
        }))

        XCTAssertEqual(fixture.durableStore.currentState, .quarantined)
        XCTAssertEqual(try POSIX.readAll(try POSIX.openAt(root, name: "outside-stage-sentinel", flags: O_RDONLY)),
                       Array("sentinel".utf8))
        XCTAssertEqual(try POSIX.directoryEntryNames(fixture.stage.root), Set(["runtime", "scratch", "scratch-held", "home", "tmp", "wheelhouse"]))
    }

    func testSharedMaterializationCoreQuarantinesEachLaunchRootSwapWithoutDeletingReplacement() throws {
        for name in ["home", "tmp", "wheelhouse"] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let root = try POSIX.openDirectory(fixture.root.path)
            let sentinel = try POSIX.createExclusive(root, name: "outside-stage-sentinel", mode: 0o600)
            try POSIX.writeAll(sentinel, Array("sentinel".utf8))

            XCTAssertThrowsError(try Self.makePublicationPreparationFixture(fixture: fixture, afterInstall: { stage in
                try POSIX.renameExclusive(from: stage.root, name: name, to: stage.root, destination: name + "-held")
                _ = try POSIX.mkdirAt(stage.root, name: name, mode: 0o700)
            }), name)

            XCTAssertEqual(fixture.durableStore.currentState, .quarantined, name)
            XCTAssertEqual(try POSIX.readAll(try POSIX.openAt(root, name: "outside-stage-sentinel", flags: O_RDONLY)),
                           Array("sentinel".utf8), name)
            let replacement = try POSIX.openAt(fixture.stage.root, name: name, flags: O_RDONLY | O_DIRECTORY)
            let original = try POSIX.openAt(fixture.stage.root, name: name + "-held", flags: O_RDONLY | O_DIRECTORY)
            XCTAssertNotEqual(replacement.inode, original.inode, name)
        }
    }

    static func preparedPublicationFixture(signer: ActiveReceiptSigner? = nil) throws -> (fixture: MaterializationFixture, prepared: PreparedPublication,
                                                         attestation: StorageAttestation, operations: PublicationTransactionOperations) {
        let setup = try Self.makePublicationPreparationFixture(signer: signer)
        let attestation = try publicationAttestation(setup.fixture)
        let prepared = try Materializer.prepareForPublicationForTest(setup.inputs)
        let name = "0123456789abcdef0123456789abcdef"
        try POSIX.renameExclusive(from: attestation.root, name: "fixture-stage", to: attestation.work, destination: name)
        let stageRoot = try POSIX.openAt(attestation.work, name: name, flags: O_RDONLY | O_DIRECTORY)
        let stage = try HeldMaterializationStage.makePublicationForTest(root: stageRoot, work: attestation.work,
                                                                        stageName: name, volume: attestation.root)
        let operations = PublicationTransactionOperations.fixture(
            syntheticImmutable: { descriptor, directory in
                if directory { try setup.immutable.requireDirectory(descriptor, mode: 0o555) }
                else {
                    let metadata = try POSIX.metadata(descriptor)
                    try setup.immutable.requireRegular(descriptor, mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555)
                }
            }, markSyntheticImmutable: { setup.immutable.mark($0) })
        return (setup.fixture, prepared.reboundForPublicationTest(stage: stage), attestation, operations)
    }

    static func publicationAttestation(_ fixture: MaterializationFixture) throws -> StorageAttestation {
        let root = try POSIX.openDirectory(fixture.root.path)
        let control = try POSIX.openAt(root, name: "control", flags: O_RDONLY | O_DIRECTORY)
        let admissions = try POSIX.openAt(control, name: "admissions", flags: O_RDONLY | O_DIRECTORY)
        let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
        let work = try POSIX.openAt(root, name: "materialization-work", flags: O_RDONLY | O_DIRECTORY)
        let quarantine = try POSIX.openAt(root, name: "quarantine", flags: O_RDONLY | O_DIRECTORY)
        let authorities = try POSIX.openAt(root, name: "authorities", flags: O_RDONLY | O_DIRECTORY)
        return StorageAttestation(rootParent: root, root: root, control: control, admissions: admissions,
                                  journals: journals, work: work, quarantine: quarantine,
                                  authorities: authorities, volumeUUID: "fixture")
    }

    /// Reuses the Task-11 full verification scaffold but keeps Task-9's prepared stage and
    /// Task-10 transaction intact.  The receipt/admission/journal are all installed before or
    /// during the one publication transaction, never backfilled after success.
    static func integratedRunFixture() throws -> (fixture: MaterializationFixture, adapters: TestMaterializationAdapters,
                                                  store: FakeDurableStore,
                                                  authority: Verify.VerificationAuthority, keyring: KeyringFixture) {
        let source = try MaterializationFixture.valid()
        let keyring = try KeyringFixture.generationThree()
        let signer = try KeyringVerifier.loadActiveForTest(keyring.root, keyA: keyring.keyA)
        let authority = try keyring.installVerificationScaffold(
            approvalBytes: Array(try Data(contentsOf: source.approvalURL)),
            materializationPolicyBytes: Array(try Data(contentsOf: source.request.inputBundle
                .appendingPathComponent("materialization-policy.json"))))
        let control = try POSIX.openAt(keyring.root, name: "control", flags: O_RDONLY | O_DIRECTORY)
        let admissions = try POSIX.openAt(control, name: "admissions", flags: O_RDONLY | O_DIRECTORY)
        let journals = try POSIX.openAt(control, name: "journals", flags: O_RDONLY | O_DIRECTORY)
        let authorities = try POSIX.openAt(keyring.root, name: "authorities", flags: O_RDONLY | O_DIRECTORY)
        let work = try POSIX.openAt(keyring.root, name: "materialization-work", flags: O_RDONLY | O_DIRECTORY)
        let quarantine = try POSIX.openAt(keyring.root, name: "quarantine", flags: O_RDONLY | O_DIRECTORY)
        let attestation = StorageAttestation(rootParent: keyring.root, root: keyring.root, control: control,
                                             admissions: admissions, journals: journals, work: work,
                                             quarantine: quarantine, authorities: authorities, volumeUUID: "fixture")
        let attempt = "0123456789abcdef0123456789abcdef"
        let targetFS = FakeFileSystem(root: keyring.url)
        let targetStore = FakeDurableStore(fileSystem: targetFS)
        let targetAdmission = BarrierBackedAdmissionStorage(fs: targetFS, barrier: AdmissionBarrier())
        targetAdmission.durableStore = targetStore
        let setup = try makePublicationPreparationFixture(fixture: source, storage: targetAdmission,
                                                           durableStore: targetStore, afterAdmission: { _, admission in
            guard admission.attempt == attempt else { throw AdmissionFailure.invalidAttempt }
            return try HeldMaterializationStage.allocateMaterializationForTest(work: work, stageName: attempt,
                                                                                volume: keyring.root)
        }, signer: signer)
        let admissionAttempt = setup.inputs.admission.attempt
        guard admissionAttempt == attempt else { throw AdmissionFailure.invalidAttempt }
        _ = try setup.inputs.admission.record.file.rebindSameIdentity(in: admissions, name: attempt + ".json")
        let journal = try POSIX.mkdirAt(journals, name: attempt, mode: 0o700)
        try targetStore.attachJournalDirectory(journal)
        let publicationOperations = PublicationTransactionOperations.fixture(
            syntheticImmutable: { descriptor, directory in
                if directory { try setup.immutable.requireDirectory(descriptor, mode: 0o555) }
                else {
                    let metadata = try POSIX.metadata(descriptor)
                    try setup.immutable.requireRegular(descriptor, mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555)
                }
            }, markSyntheticImmutable: { setup.immutable.mark($0) })
        let inputs = PublicationPreparationInputs(stage: setup.inputs.stage, bundle: setup.inputs.bundle,
                                                   admission: setup.inputs.admission, store: setup.inputs.store,
                                                   signer: setup.inputs.signer, installation: setup.inputs.installation,
                                                   operations: setup.inputs.operations)
        let adapters = TestMaterializationAdapters(request: source.request, preparation: inputs,
                                                   attestation: attestation, store: targetStore,
                                                   publicationOperations: publicationOperations,
                                                   verificationAuthority: authority)
        return (source, adapters, targetStore, authority, keyring)
    }

    func testPublicationEntryPointRequiresOpaquePreparedCapability() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        let outcome = try Publication.publishForTest(prepared: setup.prepared,
                                           attestation: setup.attestation, store: setup.fixture.durableStore,
                                           operations: setup.operations)
        XCTAssertEqual(outcome, .published)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .published)
    }

    func testFinalRootReplacementBeforeOrAfterVerifierIsRejectedWithoutRecoveryMutation() throws {
        enum Seam: CaseIterable { case beforeVerifier, afterVerifier }
        for seam in Seam.allCases {
            let setup = try Self.preparedPublicationFixture()
            defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
            let stageIdentity = (setup.prepared.testStage.device, setup.prepared.testStage.inode)
            var verified = false
            func replaceFinalName() throws {
                try POSIX.renameExclusive(from: setup.attestation.authorities,
                                          name: ProtectedExecutionConstants.authorityID,
                                          to: setup.attestation.authorities,
                                          destination: "fixture-replaced-\(seam)")
                _ = try POSIX.mkdirAt(setup.attestation.authorities,
                                      name: ProtectedExecutionConstants.authorityID, mode: 0o700)
            }
            let operations = PublicationTransactionOperations(
                revalidate: setup.operations.revalidate,
                renameExclusive: setup.operations.renameExclusive,
                rebindFinal: setup.operations.rebindFinal,
                sealRoot: { root in
                    try setup.operations.sealRoot(root)
                    if seam == .beforeVerifier { try replaceFinalName() }
                },
                durable: setup.operations.durable,
                verifyFinal: { root, prepared in
                    try setup.operations.verifyFinal(root, prepared)
                    verified = true
                    if seam == .afterVerifier { try replaceFinalName() }
                })
            XCTAssertEqual(try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                           store: setup.fixture.durableStore, operations: operations),
                           .publishIndeterminate, "\(seam)")
            XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [], "\(seam)")
            let replacement = try POSIX.openAt(setup.attestation.authorities,
                                                name: ProtectedExecutionConstants.authorityID,
                                                flags: O_RDONLY | O_DIRECTORY)
            XCTAssertTrue(replacement.device != stageIdentity.0 || replacement.inode != stageIdentity.1, "\(seam)")
            XCTAssertEqual(verified, seam == .afterVerifier, "\(seam)")
        }
    }

    func testPostRenameRebindFailureIsIndeterminateWithoutSecondRename() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        var renames = 0
        let operations = PublicationTransactionOperations(
            revalidate: {},
            renameExclusive: { from, name, to, destination in
                renames += 1; try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
            },
            rebindFinal: { _, _, _ in throw POSIXFailure.unsafeObject },
            sealRoot: { _ in XCTFail("must not seal after rebind failure") },
            durable: { _, _, _ in XCTFail("must not fsync after rebind failure") },
            verifyFinal: { _, _ in XCTFail("must not verify after rebind failure") })
        let outcome = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                      store: setup.fixture.durableStore, operations: operations)
        XCTAssertEqual(outcome, .publishIndeterminate)
        XCTAssertEqual(renames, 1)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .publishIndeterminate)
        XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [])
    }

    func testEveryPostRenameSeamTerminalizesIndeterminateExactlyOnce() throws {
        enum Seam: CaseIterable { case rebind, seal, verify, durable, publishedJournal }
        for seam in Seam.allCases {
            let setup = try Self.preparedPublicationFixture()
            defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
            var renames = 0
            let operations = PublicationTransactionOperations(
                revalidate: {},
                renameExclusive: { from, name, to, destination in
                    renames += 1; try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
                },
                rebindFinal: { root, parent, name in
                    if seam == .rebind { throw POSIXFailure.unsafeObject }
                    return try root.rebindSameIdentity(in: parent, name: name, directory: true)
                },
                sealRoot: { root in
                    if seam == .seal { throw POSIXFailure.system(EIO) }
                    guard Darwin.fchmod(root.fd, 0o555) == 0 else { throw POSIXFailure.system(errno) }
                },
                durable: { authorities, root, volume in
                    if seam == .durable { throw POSIXFailure.system(EIO) }
                    try POSIX.durableDirectory(authorities); try POSIX.durableDirectory(root); try POSIX.durableVolumeBarrier(volume)
                },
                verifyFinal: { _, _ in if seam == .verify { throw PublicationFailure.sourceChanged } })
            if seam == .publishedJournal { setup.fixture.durableStore.failAppendNumber = 4 }
            let outcome = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                          store: setup.fixture.durableStore, operations: operations)
            XCTAssertEqual(outcome, .publishIndeterminate, "\(seam)")
            XCTAssertEqual(renames, 1, "\(seam)")
            XCTAssertEqual(setup.fixture.durableStore.currentState, .publishIndeterminate, "\(seam)")
            let terminal = try JournalRecordV2.decode(try XCTUnwrap(setup.fixture.durableStore.journalRecords.last))
            XCTAssertEqual(terminal.phase, .publishIndeterminate, "\(seam)")
            XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [], "\(seam)")
        }
    }

    func testIndeterminateTerminalizationFailurePreservesPostRenameResidue() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        setup.fixture.durableStore.failPublishIndeterminateTerminalization = true
        var renames = 0
        let operations = PublicationTransactionOperations(
            revalidate: {}, renameExclusive: { from, name, to, destination in
                renames += 1; try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
            }, rebindFinal: { _, _, _ in throw POSIXFailure.unsafeObject },
            sealRoot: { _ in XCTFail("unreachable") }, durable: { _, _, _ in XCTFail("unreachable") },
            verifyFinal: { _, _ in XCTFail("unreachable") })
        do {
            _ = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                               store: setup.fixture.durableStore, operations: operations)
            XCTFail("terminalization failure must retain both causes")
        } catch let failure as Publication.PublicationTerminalizationFailure {
            XCTAssertEqual(failure.primary, "POSIX.unsafeObject")
            XCTAssertEqual(failure.terminalization, "State.terminalizationFailed")
        }
        XCTAssertEqual(renames, 1)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .renameIntent)
        XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [])
    }

    func testPublishedAppendFailureAfterRenameCarriesPrimaryAndTerminalizationCauses() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        setup.fixture.durableStore.failAppendAfterRecordNumber = 4
        do {
            _ = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                               store: setup.fixture.durableStore, operations: setup.operations)
            XCTFail("failed published append must not claim publication")
        } catch let failure as Publication.PublicationTerminalizationFailure {
            XCTAssertEqual(failure.primary, "FakeAdapterError")
            XCTAssertEqual(failure.terminalization, "State.terminalizationFailed")
        }
        XCTAssertEqual(setup.fixture.durableStore.currentState, .renameIntent)
        XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [])
        XCTAssertNotNil(try? POSIX.openAt(setup.attestation.authorities,
                                          name: ProtectedExecutionConstants.authorityID,
                                          flags: O_RDONLY | O_DIRECTORY))
    }

    func testRenameInvocationFailureIsIndeterminateWithoutQuarantine() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        let sentinel = try POSIX.createExclusive(setup.attestation.authorities, name: "foreign-sentinel", mode: 0o600)
        try POSIX.writeAll(sentinel, Array("foreign".utf8))
        var renames = 0
        let operations = PublicationTransactionOperations(
            revalidate: {}, renameExclusive: { _, _, _, _ in renames += 1; throw POSIXFailure.system(EEXIST) },
            rebindFinal: { _, _, _ in XCTFail("must not rebind after rename failure"); throw POSIXFailure.unsafeObject },
            sealRoot: { _ in XCTFail("must not seal after rename failure") },
            durable: { _, _, _ in XCTFail("must not fsync after rename failure") },
            verifyFinal: { _, _ in XCTFail("must not verify after rename failure") })
        let outcome = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                      store: setup.fixture.durableStore, operations: operations)
        XCTAssertEqual(outcome, .publishIndeterminate)
        XCTAssertEqual(renames, 1)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .publishIndeterminate)
        XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [])
        let reboundSentinel = try sentinel.rebindSameIdentity(in: setup.attestation.authorities, name: "foreign-sentinel")
        XCTAssertEqual(try POSIX.readAll(reboundSentinel), Array("foreign".utf8))
    }

    func testRenameIntentAppendFailureQuarantinesWithPreRenameIO() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        setup.fixture.durableStore.failAppendNumber = 3
        let outcome = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                      store: setup.fixture.durableStore, operations: setup.operations)
        XCTAssertEqual(outcome, .quarantined)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .quarantined)
        let terminal = try JournalRecordV2.decode(try XCTUnwrap(setup.fixture.durableStore.journalRecords.last))
        XCTAssertEqual(terminal.failureClass, .preRenameIO)
        XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.authorities), [])
    }

    func testAmbiguousRenameFailurePreservesPublishedStageAndTerminalizesIndeterminate() throws {
        let setup = try Self.preparedPublicationFixture()
        defer { try? setup.fixture.removeOnlySelfOwnedFixture() }
        let stageIdentity = (setup.prepared.testStage.device, setup.prepared.testStage.inode)
        var renames = 0
        let operations = PublicationTransactionOperations(
            revalidate: {}, renameExclusive: { from, name, to, destination in
                renames += 1
                try POSIX.renameExclusive(from: from, name: name, to: to, destination: destination)
                throw POSIXFailure.system(EIO)
            }, rebindFinal: { _, _, _ in XCTFail("ambiguous error must not rebind"); throw POSIXFailure.unsafeObject },
            sealRoot: { _ in XCTFail("ambiguous error must not seal") }, durable: { _, _, _ in XCTFail("ambiguous error must not fsync") },
            verifyFinal: { _, _ in XCTFail("ambiguous error must not verify") })
        let outcome = try Publication.publishForTest(prepared: setup.prepared, attestation: setup.attestation,
                                                      store: setup.fixture.durableStore, operations: operations)
        XCTAssertEqual(outcome, .publishIndeterminate)
        XCTAssertEqual(renames, 1)
        XCTAssertEqual(setup.fixture.durableStore.currentState, .publishIndeterminate)
        let destination = try POSIX.openAt(setup.attestation.authorities, name: ProtectedExecutionConstants.authorityID,
                                           flags: O_RDONLY | O_DIRECTORY)
        XCTAssertEqual(destination.device, stageIdentity.0)
        XCTAssertEqual(destination.inode, stageIdentity.1)
        XCTAssertEqual(try POSIX.directoryEntryNames(setup.attestation.quarantine), [])
    }

    func testProductionActiveSignerGatePrecedesAdmissionMutation() throws {
        let package = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(contentsOf: package.appendingPathComponent("Sources/R4ProtectedExecutionProvisioner/Materialize.swift"))
        let signer = try XCTUnwrap(source.range(of: "KeyringVerifier.loadActive()"))
        let admission = try XCTUnwrap(source.range(of: "let admission = try AdmissionController.admit"))
        XCTAssertLessThan(signer.lowerBound, admission.lowerBound,
                          "active keyring validation must gate admission, stage, and journal mutation")
    }

    func testSignerGateRejectsBeforeAdmissionStageGraphOrMaterializationCallbacks() throws {
        var events: [String] = []
        XCTAssertThrowsError(try Materializer.gateAdmissionOnActiveSignerForTest(signerFactory: {
            events.append("signer")
            throw PublicationFailure.sourceChanged
        }, afterValidation: {
            events += ["admission", "journal-stage", "graph", "materialize"]
        }))
        XCTAssertEqual(events, ["signer"])
    }

    func testSignerGateSuccessOrdersValidationBeforeAdmission() throws {
        let keyring = try KeyringFixture.generationThree()
        let signer = try KeyringVerifier.loadActiveForTest(keyring.root, keyA: keyring.keyA)
        var events: [String] = []
        try Materializer.gateAdmissionOnActiveSignerForTest(signerFactory: {
            events.append("signerValidated")
            return signer
        }, afterValidation: {
            events += ["admissionCreated", "stageCreated"]
        })
        XCTAssertEqual(events, ["signerValidated", "admissionCreated", "stageCreated"])
    }

    func testReceiptPairBindsActiveKeyAndIsImmutable() throws {
        let evidence = try V2Fixture.complete()
        let keyring = try KeyringFixture.generationThree()
        let signer = try KeyringVerifier.loadActiveForTest(keyring.root, keyA: keyring.keyA)
        let receipt = try ReceiptFactory.create(manifest: InputBundleManifest.decode(evidence.bundle),
                                                approval: Task14ApprovalV2.decode(evidence.approval),
                                                admission: AdmissionRecord.decode(evidence.admission),
                                                runtime: RuntimeManifestV2.decode(evidence.runtime, policy: MaterializationPolicy.decode(evidence.policy)),
                                                authority: AuthorityManifestV2.decode(evidence.authority),
                                                publishedTree: PublishedAuthorityTree.decode(evidence.publishedRows),
                                                keyGeneration: signer.generation, publicKeySHA256: signer.publicKeySHA256)
        let root = try DarwinDisposableRoot.create(prefix: "r4-receipt-publication-")
        defer { try? root.removeOnlySelfOwnedFixture() }
        let immutable = try SyntheticImmutableState(owner: root.heldRoot)

        let operations = PublicationPOSIX.Operations(
            setOwnership: { _, _, _ in }, setMode: { descriptor, mode in
                guard fchmod(descriptor.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
            }, writeAll: { try POSIX.writeAll($0, $1) }, durableRegular: { try POSIX.durableRegular($0) },
            setImmutable: { immutable.mark($0) }, rebind: { try $0.rebindSameIdentity(in: $1, name: $2) },
            readAll: { try POSIX.readAll($0) }, verifySealed: { try immutable.requireRegular($0, mode: $1) },
            durableDirectory: { try POSIX.durableDirectory($0) })
        _ = try ReceiptPublication.installForTest(into: root.heldRoot, receiptBytes: receipt, signer: signer, operations: operations)

        let decoded = try ReceiptV2.decode(String(decoding: receipt, as: UTF8.self))
        let canonicalAdmissionSHA256 = try SHA256Digest(CanonicalJSON.sha256(Array(evidence.admission.utf8)))
        // Locator and journal bind the complete immutable admission record, not the
        // record's embedded self-digest.  A receipt must bind the identical bytes.
        XCTAssertEqual(decoded.admissionRecordSHA256, canonicalAdmissionSHA256)
        XCTAssertNotEqual(decoded.admissionRecordSHA256,
                          try AdmissionRecord.decode(evidence.admission).sha256)
        XCTAssertEqual(decoded.keyGeneration, 3)
        XCTAssertEqual(decoded.publicKeySHA256, keyring.generation3PublicSHA256)
        let signature = try POSIX.readAll(try POSIX.openAt(root.heldRoot, name: "receipt.ed25519", flags: O_RDONLY))
        XCTAssertEqual(signature.count, 64)
        let publicKey = try Curve25519.Signing.PublicKey(rawRepresentation: Data(signer.publicKeyBytes))
        XCTAssertTrue(publicKey.isValidSignature(Data(signature), for: Data(receipt)))
        var mutated = signature; mutated[0] ^= 1
        XCTAssertFalse(publicKey.isValidSignature(Data(mutated), for: Data(receipt)))

        let wrongGeneration = try ReceiptFactory.create(manifest: InputBundleManifest.decode(evidence.bundle),
                                                        approval: Task14ApprovalV2.decode(evidence.approval),
                                                        admission: AdmissionRecord.decode(evidence.admission),
                                                        runtime: RuntimeManifestV2.decode(evidence.runtime, policy: MaterializationPolicy.decode(evidence.policy)),
                                                        authority: AuthorityManifestV2.decode(evidence.authority),
                                                        publishedTree: PublishedAuthorityTree.decode(evidence.publishedRows),
                                                        keyGeneration: 1, publicKeySHA256: signer.publicKeySHA256)
        let other = try DarwinDisposableRoot.create(prefix: "r4-receipt-wrong-generation-")
        defer { try? other.removeOnlySelfOwnedFixture() }
        XCTAssertThrowsError(try ReceiptPublication.installForTest(into: other.heldRoot, receiptBytes: wrongGeneration,
                                                                    signer: signer, operations: operations))
        XCTAssertEqual(try POSIX.directoryEntryNames(other.heldRoot), [])
    }

    func testPreparationValidatesThenPresealsBeforeAppendingPresealedJournal() throws {
        let prepared = try Self.makePublicationPreparationFixture()
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }
        let rootModeBefore = try POSIX.metadata(prepared.fixture.stage.root).mode

        let result = try Materializer.prepareForPublicationForTest(prepared.inputs)

        XCTAssertEqual(Array(prepared.operations.events.prefix(5)), ["admission", "runtime", "authority", "receipt", "preseal"])
        XCTAssertEqual(prepared.operations.events.last, "verify-final-tree")
        for name in Set(prepared.operations.events.compactMap { $0.split(separator: ":", maxSplits: 1).dropFirst().first.map(String.init) }) {
            let normalized = prepared.operations.events.firstIndex(of: "normalize:\(name)")
            let immutable = prepared.operations.events.firstIndex(of: "immutable:\(name)")
            let readback = prepared.operations.events.firstIndex(of: "readback:\(name)")
            let durable = prepared.operations.events.firstIndex(of: "durable:\(name)")
            let rebound = prepared.operations.events.firstIndex(of: "rebind:\(name)")
            if let normalized, let immutable, let readback, let durable, let rebound {
                XCTAssertLessThan(normalized, immutable, name)
                XCTAssertLessThan(immutable, readback, name)
                XCTAssertLessThan(readback, durable, name)
                XCTAssertLessThan(durable, rebound, name)
            }
        }
        XCTAssertEqual(prepared.fixture.durableStore.currentState, MaterializationState.presealed)
        XCTAssertEqual(prepared.fixture.durableStore.journalRecords.count, 2)
        XCTAssertEqual(result.testGraph.childNames, PublishedAuthorityGraph.requiredChildNames)
        let journal = try JournalRecordV2.decode(prepared.fixture.durableStore.journalRecords.last!)
        let receipt = try POSIX.readAll(try POSIX.openAt(result.testStage, name: "receipt.json", flags: O_RDONLY))
        XCTAssertEqual(journal.receiptFileSHA256, ReceiptV2.receiptFileSHA256(receipt))
        XCTAssertEqual(try POSIX.metadata(result.testStage).mode, rootModeBefore)
        XCTAssertEqual(try POSIX.metadata(try POSIX.openAt(result.testStage, name: "runtime", flags: O_RDONLY | O_DIRECTORY)).mode, 0o555)
    }

    func testPreparationFailureBeforePresealQuarantinesWithoutRename() throws {
        let prepared = try Self.makePublicationPreparationFixture(failingReceipt: true)
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }

        XCTAssertThrowsError(try Materializer.prepareForPublicationForTest(prepared.inputs))

        XCTAssertEqual(prepared.fixture.durableStore.currentState, MaterializationState.quarantined)
        XCTAssertEqual(prepared.operations.events, ["admission", "runtime", "authority", "receipt"])
        XCTAssertEqual(prepared.fixture.durableStore.journalRecords.count, 2)
    }

    func testFailureAfterOneSealedDescendantQuarantinesBeforeAnyPublication() throws {
        let prepared = try Self.makePublicationPreparationFixture(failingAfterOnePreseal: true)
        defer { try? prepared.fixture.removeOnlySelfOwnedFixture() }

        XCTAssertThrowsError(try Materializer.prepareForPublicationForTest(prepared.inputs))

        XCTAssertTrue(prepared.operations.events.contains("preseal"))
        XCTAssertEqual(prepared.operations.events.filter { $0.hasPrefix("durable:") }.count, 1)
        XCTAssertFalse(prepared.operations.events.contains("verify-final-tree"))
        XCTAssertEqual(prepared.fixture.durableStore.currentState, .quarantined)
        let terminal = try JournalRecordV2.decode(try XCTUnwrap(prepared.fixture.durableStore.journalRecords.last))
        XCTAssertEqual(terminal.phase, .quarantined)
        XCTAssertFalse(prepared.operations.events.contains { $0.contains("rename") || $0.contains("publish") || $0.contains("retry") })
    }

    func testProspectiveAuthorityTreeHasExactEightChildrenAndAcyclicClosure() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        let graph = try PublishedAuthorityGraph.build(root: fixture.stage.root)

        XCTAssertEqual(graph.childNames, ["admission-locator-v2.json", "admission.json", "checkout", "manifest-v2.json", "receipt.ed25519", "receipt.json", "runtime", "runtime-manifest-v2.json"])
        XCTAssertFalse(graph.closureRows.contains { $0.path == "receipt.json" || $0.path == "receipt.ed25519" })
        XCTAssertTrue(graph.closureRows.contains { $0.path == "checkout" })
        XCTAssertTrue(graph.closureRows.contains { $0.path == "runtime" })
        for path in ["admission-locator-v2.json", "admission.json", "checkout", "checkout/payload", "manifest-v2.json", "runtime", "runtime-manifest-v2.json"] {
            XCTAssertTrue(graph.closureRows.contains { $0.path == path }, path)
        }
        XCTAssertEqual(graph.closureSHA256.value.count, 64)
    }

    func testProspectiveAuthorityGraphRejectsExtraChildAndMissingNonReceiptChild() throws {
        for missing in ["admission-locator-v2.json", "admission.json", "checkout", "manifest-v2.json", "runtime", "runtime-manifest-v2.json"] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
            try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
            if missing == "checkout" {
                let directory = try POSIX.openAt(fixture.stage.root, name: missing, flags: O_RDONLY | O_DIRECTORY)
                try POSIX.unlinkAt(directory, name: "payload")
            }
            try POSIX.unlinkAt(fixture.stage.root, name: missing, directory: missing == "checkout" || missing == "runtime")
            XCTAssertThrowsError(try PublishedAuthorityGraph.build(root: fixture.stage.root), missing)
        }
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        let extra = try POSIX.createExclusive(fixture.stage.root, name: "extra", mode: 0o600)
        try POSIX.writeAll(extra, Array("extra".utf8))
        XCTAssertThrowsError(try PublishedAuthorityGraph.build(root: fixture.stage.root))
    }

    func testProspectiveAuthorityGraphRejectsReceiptSymlinkAndHardlinkedRegular() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        try POSIX.unlinkAt(fixture.stage.root, name: "receipt.json")
        XCTAssertEqual(symlinkat("/private/tmp", fixture.stage.root.fd, "receipt.json"), 0)
        XCTAssertThrowsError(try PublishedAuthorityGraph.build(root: fixture.stage.root))
        try POSIX.unlinkAt(fixture.stage.root, name: "receipt.json")
        try write(fixture.stage.root, "receipt.json", Array((try V2Fixture.complete()).receipt.utf8))
        try POSIX.unlinkAt(fixture.stage.root, name: "manifest-v2.json")
        XCTAssertEqual(linkat(fixture.stage.root.fd, "admission.json", fixture.stage.root.fd, "manifest-v2.json", 0), 0)
        XCTAssertThrowsError(try PublishedAuthorityGraph.build(root: fixture.stage.root))
    }

    func testProspectiveAuthorityGraphRequiresRetainedAdmissionArtifactsAndReceiptSignatureLength() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        XCTAssertThrowsError(try PublishedAuthorityGraph.build(stage: fixture.stage, admission: admission))
        try POSIX.unlinkAt(fixture.stage.root, name: "receipt.ed25519")
        try write(fixture.stage.root, "receipt.ed25519", Array(repeating: 0, count: 63))
        XCTAssertThrowsError(try PublishedAuthorityGraph.build(root: fixture.stage.root))
    }

    func testReboundStageRootRejectsSwapAfterValidationAndAfterRows() throws {
        for seam in [PublicationGraphSeam.afterArtifactValidation, .afterRows] {
            let fixture = try MaterializationFixture.valid()
            defer { try? fixture.removeOnlySelfOwnedFixture() }
            let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
            try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
            let artifacts = try retainedArtifacts(root: fixture.stage.root)
            let stageParent = try POSIX.openDirectory(fixture.root.path)
            let stageName = "fixture-stage"
            XCTAssertNoThrow(try PublishedAuthorityGraph.buildForTest(root: fixture.stage.root, stageParent: stageParent,
                                                                        stageName: stageName, artifacts: artifacts,
                                                                        verifySealed: typedSealedVerifier))
            let foreign = fixture.root.deletingLastPathComponent().appendingPathComponent("r4-foreign-sentinel-\(UUID().uuidString)")
            try Data("sentinel".utf8).write(to: foreign)
            defer { try? FileManager.default.removeItem(at: foreign) }
            XCTAssertThrowsError(try PublishedAuthorityGraph.buildForTest(root: fixture.stage.root, stageParent: stageParent,
                                                                            stageName: stageName, artifacts: artifacts,
                                                                            verifySealed: typedSealedVerifier,
                                                                            seam: { point in
                guard point == seam else { return }
                try POSIX.renameExclusive(from: stageParent, name: stageName, to: stageParent,
                                          destination: "moved-\(seam)")
            }))
            XCTAssertEqual(try Data(contentsOf: foreign), Data("sentinel".utf8))
        }
    }

    func testRetainedAdmissionArtifactsRejectReplacementBeforeFinalRebind() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        let artifacts = try retainedArtifacts(root: fixture.stage.root)
        let parent = try POSIX.openDirectory(fixture.root.path)
        XCTAssertThrowsError(try PublishedAuthorityGraph.buildForTest(root: fixture.stage.root, stageParent: parent,
                                                                        stageName: "fixture-stage", artifacts: artifacts,
                                                                        verifySealed: typedSealedVerifier,
                                                                        seam: { point in
            guard point == .afterRows else { return }
            try POSIX.unlinkAt(fixture.stage.root, name: "admission.json")
            try self.write(fixture.stage.root, "admission.json", Array("replacement".utf8))
        }))
    }

    func testProspectiveAuthorityGraphRejectsNestedSymlinkAndCasefoldCollision() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        let checkout = try POSIX.openAt(fixture.stage.root, name: "checkout", flags: O_RDONLY | O_DIRECTORY)
        XCTAssertEqual(symlinkat("/private/tmp", checkout.fd, "nested-link"), 0)
        XCTAssertThrowsError(try PublishedAuthorityGraph.build(root: fixture.stage.root))
        try POSIX.unlinkAt(checkout, name: "nested-link")
        XCTAssertThrowsError(try PublishedAuthorityGraph.requireCasefoldUnique(["payload", "PAYLOAD"]))
    }

    func testProspectiveClosureIncludesNestedReceiptNamedObjects() throws {
        let fixture = try MaterializationFixture.valid()
        defer { try? fixture.removeOnlySelfOwnedFixture() }
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: fixture.storage)
        try makeProspectiveAuthorityTree(fixture: fixture, admission: admission)
        let checkout = try POSIX.openAt(fixture.stage.root, name: "checkout", flags: O_RDONLY | O_DIRECTORY)
        let runtime = try POSIX.openAt(fixture.stage.root, name: "runtime", flags: O_RDONLY | O_DIRECTORY)
        try write(checkout, "receipt.json", Array("nested-checkout".utf8))
        try write(runtime, "receipt.ed25519", Array(repeating: 0, count: 64))
        let graph = try PublishedAuthorityGraph.build(root: fixture.stage.root)
        XCTAssertTrue(graph.closureRows.contains { $0.path == "checkout/receipt.json" })
        XCTAssertTrue(graph.closureRows.contains { $0.path == "runtime/receipt.ed25519" })
    }

    private func makeProspectiveAuthorityTree(fixture: MaterializationFixture, admission: Admission) throws {
        let root = fixture.stage.root
        try POSIX.unlinkAt(fixture.stage.scratch, name: "input", directory: true)
        try POSIX.unlinkAt(root, name: "scratch", directory: true)
        for name in ["home", "tmp", "wheelhouse"] { try POSIX.unlinkAt(root, name: name, directory: true) }
        _ = try POSIX.mkdirAt(root, name: "checkout", mode: 0o700)
        let checkout = try POSIX.openAt(root, name: "checkout", flags: O_RDONLY | O_DIRECTORY)
        let checkoutFile = try POSIX.createExclusive(checkout, name: "payload", mode: 0o600)
        try POSIX.writeAll(checkoutFile, Array("checkout".utf8))
        let admissionBytes = try POSIX.readAll(try admission.record.file.rebindSameIdentity(in: admission.record.parent, name: admission.record.name))
        try write(root, "admission.json", admissionBytes)
        try write(root, "admission-locator-v2.json", try PublishedAuthorityGraph.admissionLocatorBytesForTest(attempt: admission.attempt, admissionSHA256: admission.recordSHA256))
        try write(root, "manifest-v2.json", Array("manifest".utf8))
        try write(root, "runtime-manifest-v2.json", Array("runtime-manifest".utf8))
        try write(root, "receipt.json", Array((try V2Fixture.complete()).receipt.utf8))
        try write(root, "receipt.ed25519", Array(repeating: 0, count: 64))
    }

    static func makePublicationPreparationFixture(fixture suppliedFixture: MaterializationFixture? = nil,
                                                   stage suppliedStage: HeldMaterializationStage? = nil,
                                                   storage suppliedStorage: Storage? = nil,
                                                   durableStore suppliedDurableStore: FakeDurableStore? = nil,
                                                   afterAdmission: ((MaterializationFixture, Admission) throws -> HeldMaterializationStage)? = nil,
                                                   afterInstall: ((HeldMaterializationStage) throws -> Void)? = nil,
                                                   signer suppliedSigner: ActiveReceiptSigner? = nil, failingReceipt: Bool = false,
                                                   failingAfterOnePreseal: Bool = false) throws -> (fixture: MaterializationFixture, inputs: PublicationPreparationInputs, operations: Task9OperationLog, immutable: SyntheticImmutableState) {
        let fixture = try suppliedFixture ?? MaterializationFixture.valid()
        let storage = suppliedStorage ?? fixture.storage
        let durableStore = suppliedDurableStore ?? fixture.durableStore
        let admission = try AdmissionController.admit(fixture.validatedBundle, storage: storage)
        let heldStage: HeldMaterializationStage
        if let afterAdmission { heldStage = try afterAdmission(fixture, admission) }
        else { heldStage = suppliedStage ?? fixture.stage }
        try StateMachine.append(try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission),
                                using: durableStore, admission: admission)
        let observer = try Task9FinalLayoutObserver(machO: MachOObservation.decode(V2Fixture.complete().machOObservation),
                                                    policy: fixture.validatedBundle.policy)
        let stage = try POSIX.descriptorPath(heldStage.root)
        let runtime = stage + "/runtime/closure/python311"
        let report = "{\"files\":[\"\(runtime)/a\",\"\(runtime)/b\",\"\(runtime)/c\",\"\(runtime)/d\"],\"paths\":[\"\(runtime)\"],\"version\":\"\(fixture.validatedBundle.policy.pythonVersion)\"}"
        var launches = 0
        let installation = try Materializer.materializeOrQuarantineForTest(stage: heldStage, bundle: fixture.validatedBundle,
                                                                            admission: admission, store: durableStore) { layout in
            let launch = try VerifiedLaunchCapability(layout: layout, policy: fixture.validatedBundle.policy,
                                                      approval: fixture.validatedBundle.approval, observer: observer)
            let observation = try ProcessRunner.installAndProbe(launch) { authorization in
                try ProcessRunner.spawnPinnedLauncher(authorization) { command in
                    launches += 1
                    // The closed command and environment name root children.  Open those exact
                    // paths at the launch seam so this fixture cannot pass by string comparison
                    // while the descriptors still point into a hidden scratch directory.
                    guard command.environment.contains("HOME=\(stage)/home"),
                          command.environment.contains("TMPDIR=\(stage)/tmp"),
                          launches != 1 || command.argv.contains(where: { $0.hasPrefix(stage + "/wheelhouse/") }) else {
                        throw ArchiveFailure.invalidStage
                    }
                    for name in ["home", "tmp", "wheelhouse"] {
                        let held = try POSIX.openDirectory(stage + "/" + name)
                        guard try POSIX.metadata(held).type == S_IFDIR else { throw ArchiveFailure.invalidStage }
                    }
                    return ProcessResult(status: 0, output: launches == 1 ? [] : Array(report.utf8))
                }
            }
            try afterInstall?(heldStage)
            return observation
        }
        XCTAssertEqual(try POSIX.directoryEntryNames(heldStage.root), ["checkout", "runtime"])

        let log = Task9OperationLog(failAfterOnePreseal: failingAfterOnePreseal)
        let immutableState = try SyntheticImmutableState(owner: heldStage.root)
        let metadata = PublicationPOSIX.Operations(
            setOwnership: { _, _, _ in },
            setMode: { descriptor, mode in
                guard fchmod(descriptor.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
            },
            writeAll: { try POSIX.writeAll($0, $1) },
            durableRegular: { try POSIX.durableRegular($0) },
            // Synthetic kernel immutable flag for unprivileged Darwin.  Production uses
            // fchflags; this state is keyed to the descriptor's exact device/inode identity.
            setImmutable: { immutableState.mark($0) },
            rebind: { try $0.rebindSameIdentity(in: $1, name: $2) },
            readAll: { try POSIX.readAll($0) },
            verifySealed: { descriptor, mode in try immutableState.requireRegular(descriptor, mode: mode) },
            durableDirectory: { try POSIX.durableDirectory($0) }
        )
        let signer: ActiveReceiptSigner
        if let suppliedSigner { signer = suppliedSigner }
        else {
            let signerFixture = try KeyringFixture.generationThree()
            signer = try KeyringVerifier.loadActiveForTest(signerFixture.root, keyA: signerFixture.keyA)
        }
        let operations = PublicationPreparationOperations(
            admissionArtifacts: { root, heldAdmission in
                log.events.append("admission")
                return try PublishedAuthorityGraph.installAdmissionArtifactsForTest(into: root, admission: heldAdmission, operations: metadata)
            },
            immutable: { root, name, bytes in
                log.events.append(name == "runtime-manifest-v2.json" ? "runtime" : "authority")
                return try PublicationPOSIX.createImmutableExclusive(root, name, bytes, mode: 0o444, operations: metadata)
            },
            receipt: { root, bytes, heldSigner in
                log.events.append("receipt")
                if failingReceipt { throw PublicationFailure.sourceChanged }
                return try ReceiptPublication.installForTest(into: root, receiptBytes: bytes, signer: heldSigner, operations: metadata)
            },
            prospectiveGraph: { stage, artifacts in
                let graph = try PublishedAuthorityGraph.buildProspectiveForTest(stage: stage, artifacts: artifacts,
                                                                                 verifySealed: { try metadata.verifySealed($0, 0o444) })
                return graph
            },
            completeGraph: { stage, artifacts in
                try PublishedAuthorityGraph.buildForTest(stage: stage, artifacts: artifacts,
                                                         verifySealed: { try metadata.verifySealed($0, 0o444) })
            },
            preseal: { root in
                XCTAssertEqual(durableStore.journalRecords.count, 1)
                log.events.append("preseal")
                try presealFixtureDescendants(root, immutable: immutableState, log: log)
            },
            verifyPresealed: { root in
                log.events.append("verify-final-tree")
                try verifyFixturePresealedDescendants(root, immutable: immutableState)
            }
        )
        return (fixture, PublicationPreparationInputs(stage: heldStage, bundle: fixture.validatedBundle,
                                                       admission: admission, store: durableStore,
                                                       signer: signer, installation: installation, operations: operations), log, immutableState)
    }

    private static func presealFixtureDescendants(_ root: HeldDescriptor, immutable: SyntheticImmutableState,
                                           log: Task9OperationLog) throws {
        for name in try POSIX.directoryEntryNames(root) {
            let held = try POSIX.openAt(root, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(held)
            if metadata.type == S_IFDIR {
                try presealFixtureDescendants(held, immutable: immutable, log: log)
                guard fchmod(held.fd, 0o555) == 0 else { throw POSIXFailure.system(errno) }
                log.events.append("normalize:\(name)")
            } else if metadata.type == S_IFREG {
                let mode: mode_t = (metadata.mode & 0o111) == 0 ? 0o444 : 0o555
                guard fchmod(held.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
                log.events.append("normalize:\(name)")
            } else {
                throw PublicationFailure.invalidMetadata
            }
            try POSIX.requireNoExtendedACL(held)
            try POSIX.requireNoUnexpectedExtendedAttributes(held)
            immutable.mark(held); log.events.append("immutable:\(name)")
            if metadata.type == S_IFDIR {
                try immutable.requireDirectory(held, mode: 0o555)
                log.events.append("readback:\(name)")
                try POSIX.durableDirectory(held)
                log.events.append("durable:\(name)")
            } else {
                let mode: mode_t = (metadata.mode & 0o111) == 0 ? 0o444 : 0o555
                try immutable.requireRegular(held, mode: mode)
                log.events.append("readback:\(name)")
                try POSIX.durableRegular(held)
                log.events.append("durable:\(name)")
            }
            let rebound = try held.rebindSameIdentity(in: root, name: name, directory: metadata.type == S_IFDIR)
            log.events.append("rebind:\(name)")
            if metadata.type == S_IFDIR { try immutable.requireDirectory(rebound, mode: 0o555) }
            else { try immutable.requireRegular(rebound, mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555) }
            if log.failAfterOnePreseal {
                log.failAfterOnePreseal = false
                throw PublicationFailure.sourceChanged
            }
        }
        try POSIX.durableDirectory(root)
    }

    private static func verifyFixturePresealedDescendants(_ root: HeldDescriptor, immutable: SyntheticImmutableState) throws {
        for name in try POSIX.directoryEntryNames(root).sorted() {
            let held = try POSIX.openAt(root, name: name, flags: O_RDONLY)
            let metadata = try POSIX.metadata(held)
            if metadata.type == S_IFDIR {
                try verifyFixturePresealedDescendants(held, immutable: immutable)
                try immutable.requireDirectory(held, mode: 0o555)
            } else if metadata.type == S_IFREG {
                try immutable.requireRegular(held, mode: (metadata.mode & 0o111) == 0 ? 0o444 : 0o555)
            } else { throw PublicationFailure.invalidMetadata }
            _ = try held.rebindSameIdentity(in: root, name: name, directory: metadata.type == S_IFDIR)
        }
    }

    private func write(_ parent: HeldDescriptor, _ name: String, _ bytes: [UInt8]) throws {
        let file = try POSIX.createExclusive(parent, name: name, mode: 0o600)
        try POSIX.writeAll(file, bytes)
    }

    private func retainedArtifacts(root: HeldDescriptor) throws -> PublishedAdmissionArtifacts {
        let admission = try POSIX.openAt(root, name: "admission.json", flags: O_RDONLY)
        let locator = try POSIX.openAt(root, name: "admission-locator-v2.json", flags: O_RDONLY)
        return try PublishedAdmissionArtifacts(
            admission: HeldPublishedRegular(parent: root, name: "admission.json", file: admission, bytes: POSIX.readAll(admission)),
            locator: HeldPublishedRegular(parent: root, name: "admission-locator-v2.json", file: locator, bytes: POSIX.readAll(locator))
        )
    }

    private var typedSealedVerifier: (HeldDescriptor) throws -> Void {
        { _ in
            try POSIX.validateSealedMetadata(
                SealedMetadataSnapshot(type: S_IFREG, uid: 0, gid: 0, mode: 0o444,
                                       nlink: 1, flags: UInt32(SF_IMMUTABLE), acl: .none,
                                       xattrs: ["com.apple.provenance"]),
                expectedType: S_IFREG, mode: 0o444, exactNlink: true
            )
        }
    }

    func testObservedRuntimeManifestBindsCompletePostInstallGraph() throws {
        let fixture = try V2Fixture.complete()
        let bundle = try InputBundleManifest.decode(fixture.bundle)
        let policy = try MaterializationPolicy.decode(fixture.policy)
        let installation = try InstallationObservation(executionObservation: try fixture.observation(), probe: ImportProbeObservation(version: "3.11.15", sysPath: ["/private/tmp/stage/runtime/closure/python311"], moduleFiles: ["/private/tmp/stage/runtime/closure/python311/a", "/private/tmp/stage/runtime/closure/python311/b", "/private/tmp/stage/runtime/closure/python311/c", "/private/tmp/stage/runtime/closure/python311/d"], outputSHA256: policy.importProbeSHA256))

        let bytes = try ObservedRuntimeManifestFactory.create(bundle: bundle, policy: policy,
                                                               installation: installation,
                                                               sealedRuntimeEntries: fixture.runtimeEntries)
        let runtime = try RuntimeManifestV2.decode(String(decoding: bytes, as: UTF8.self), policy: policy)
        XCTAssertEqual(runtime.stagedMachOObservations, installation.executionObservation.stagedMachOObservations)
        XCTAssertEqual(runtime.preInstallStagedMachOObservations, installation.executionObservation.preInstallStagedMachOObservations)
        XCTAssertEqual(runtime.dependencyClosureSHA256, installation.postProbeDependencyClosureSHA256)
    }

    func testExecutionObservationRejectsExactAndCasefoldDuplicateStagedMachOPaths() throws {
        let fixture = try V2Fixture.complete()
        let observed = try fixture.observation()
        let duplicate = observed.stagedMachOObservations[0]
        XCTAssertThrowsError(try ExecutionObservation(stageRoot: observed.stageRoot, actualProfileUTF8: observed.actualProfileUTF8,
                                                       environment: observed.environment, installerCommand: observed.installerCommand,
                                                       importCommand: observed.importCommand, interpreterMachOObservation: observed.interpreterMachOObservation,
                                                       stagedMachOObservations: [duplicate, duplicate]))
        let casefoldPath = duplicate.path.uppercased()
        let casefoldMachO = MachOObservation(path: casefoldPath, slices: duplicate.machOObservation.slices, dependencies: duplicate.machOObservation.dependencies)
        let casefold = try StagedMachOObservation(path: casefoldPath, descriptorIdentitySHA256: duplicate.descriptorIdentitySHA256,
                                                   sha256: duplicate.sha256, codesignIdentitySHA256: duplicate.codesignIdentitySHA256,
                                                   machOObservation: casefoldMachO)
        XCTAssertThrowsError(try ExecutionObservation(stageRoot: observed.stageRoot, actualProfileUTF8: observed.actualProfileUTF8,
                                                       environment: observed.environment, installerCommand: observed.installerCommand,
                                                       importCommand: observed.importCommand, interpreterMachOObservation: observed.interpreterMachOObservation,
                                                       stagedMachOObservations: [duplicate, casefold]))
    }
}

final class Task9OperationLog {
    var events: [String] = []
    var failAfterOnePreseal: Bool

    init(failAfterOnePreseal: Bool = false) { self.failAfterOnePreseal = failAfterOnePreseal }
}

/// Unprivileged test substitute for Darwin's `SF_IMMUTABLE`.  The bit cannot be truthfully
/// mutated by the test user, so the seam records the exact descriptor identity and requires it
/// alongside real mode/owner/link/ACL/xattr readback.  Production has no such substitution.
final class SyntheticImmutableState {
    private let owner: (uid_t, gid_t)
    private var immutable: Set<String> = []

    init(owner: HeldDescriptor) throws {
        let metadata = try POSIX.metadata(owner)
        self.owner = (metadata.uid, metadata.gid)
    }

    func mark(_ descriptor: HeldDescriptor) {
        immutable.insert(identity(of: descriptor))
    }

    func requireRegular(_ descriptor: HeldDescriptor, mode: mode_t) throws {
        try require(descriptor, type: S_IFREG, mode: mode, exactNlink: true)
    }

    func requireDirectory(_ descriptor: HeldDescriptor, mode: mode_t) throws {
        try require(descriptor, type: S_IFDIR, mode: mode, exactNlink: false)
    }

    private func require(_ descriptor: HeldDescriptor, type: mode_t, mode: mode_t, exactNlink: Bool) throws {
        let value = try POSIX.metadata(descriptor)
        guard immutable.contains(identity(of: descriptor)), value.type == type,
              value.uid == owner.0, value.gid == owner.1, value.mode == mode,
              exactNlink ? value.nlink == 1 : value.nlink >= 1 else { throw PublicationFailure.invalidMetadata }
        try POSIX.requireNoExtendedACL(descriptor)
        try POSIX.requireNoUnexpectedExtendedAttributes(descriptor)
    }

    private func identity(of descriptor: HeldDescriptor) -> String {
        "\(descriptor.device):\(descriptor.inode)"
    }
}

private final class Task9FinalLayoutObserver: FinalLayoutObserver {
    private let machO: MachOObservation

    init(machO: MachOObservation, policy: MaterializationPolicy) throws {
        guard try machO.sha256() == policy.baseInterpreter.machOSHA256 else { throw FinalLayoutObserverFailure.drift }
        self.machO = machO
    }

    func verify(layout: HeldFinalLaunchLayout, policy: MaterializationPolicy, approval: Task14ApprovalV2,
                phase: LaunchObservationPhase) throws -> FinalLaunchObservation {
        guard approval.baseInterpreterSHA256 == policy.baseInterpreter.sha256,
              approval.baseInterpreterCodesignIdentitySHA256 == policy.baseInterpreter.codesignIdentity.sha256,
              try machO.sha256() == policy.baseInterpreter.machOSHA256 else { throw FinalLayoutObserverFailure.drift }
        let staged = try StagedMachOObservation(path: machO.path,
                                                descriptorIdentitySHA256: policy.baseInterpreter.sha256,
                                                sha256: policy.baseInterpreter.sha256,
                                                codesignIdentitySHA256: policy.baseInterpreter.codesignIdentity.sha256,
                                                machOObservation: machO)
        return FinalLaunchObservation(layout: try layout.rebindDescriptors(), interpreterMachOObservation: machO,
                                      stagedMachOObservations: [staged])
    }
}
