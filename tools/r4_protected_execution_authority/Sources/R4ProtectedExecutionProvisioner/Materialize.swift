import CryptoKit
import Darwin
import Foundation

/// The runtime manifest is derived after the two closed launches have completed.  In particular,
/// it accepts the Task-8 observation rather than a caller command, predicted profile, or partial
/// Mach-O graph.
enum ObservedRuntimeManifestFactory {
    static func create(bundle: InputBundleManifest, policy: MaterializationPolicy,
                       installation: InstallationObservation, sealedRuntimeEntries: JSONValue) throws -> [UInt8] {
        let observation = installation.executionObservation
        let expectedEnvironment = try policy.reboundEnvironment(stage: observation.stageRoot)
        let expectedProfile = try policy.reboundSandboxProfile(stage: observation.stageRoot)
        guard observation.environment == expectedEnvironment,
              observation.actualProfileUTF8 == expectedProfile,
              installation.probe.outputSHA256.value.count == 64 else { throw EvidenceError.invalidValue }
        let staged = observation.stagedMachOObservations
        let pre = observation.preInstallStagedMachOObservations
        let interpreterPath = "runtime/closure/python311/bin/python3.11"
        guard let preInterpreter = pre.first(where: { $0.path == interpreterPath }),
              let postInterpreter = staged.first(where: { $0.path == interpreterPath }) else { throw EvidenceError.invalidValue }
        let dependencies = try StagedMachOObservation.flattenedDependencies(staged)
        let fields: [(String, JSONValue)] = [
            ("kind", .string("R4_HERMETIC_PYTHON311_RUNTIME_V2")),
            ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("python_version", .string(policy.pythonVersion)), ("entrypoint", .string(interpreterPath)),
            ("lock_sha256", .string(bundle.lockSHA256.value)), ("materialization_input_sha256", .string(bundle.sha256.value)),
            ("materialization_policy_sha256", .string(policy.sha256.value)), ("wheel_set_sha256", .string(bundle.wheelSetSHA256.value)),
            ("sandbox_launcher_path", .string(policy.sandboxLauncher.path)),
            ("sandbox_launcher_descriptor_identity_sha256", .string(policy.sandboxLauncher.descriptorIdentitySHA256.value)),
            ("sandbox_launcher_sha256", .string(policy.sandboxLauncher.sha256.value)),
            ("sandbox_launcher_codesign_identity_sha256", .string(policy.sandboxLauncher.codesignIdentity.sha256.value)),
            ("sandbox_profile_template_sha256", .string(policy.sandboxProfileTemplateSHA256.value)),
            ("sandbox_profile_sha256", .string(try ActualSandboxProfile.sha256(observation.actualProfileUTF8).value)),
            ("child_command_template_sha256", .string(policy.childCommandTemplateSHA256.value)),
            ("installer_command_sha256", .string(try observation.installerCommand.installCommandSHA256().value)),
            ("import_command_sha256", .string(try observation.importCommand.importCommandSHA256().value)),
            ("interpreter_pre_install_sha256", .string(preInterpreter.sha256.value)),
            ("interpreter_pre_install_mach_o_sha256", .string(try preInterpreter.machOObservation.sha256().value)),
            ("interpreter_pre_install_codesign_identity_sha256", .string(preInterpreter.codesignIdentitySHA256.value)),
            ("interpreter_post_install_sha256", .string(postInterpreter.sha256.value)),
            ("interpreter_post_install_mach_o_sha256", .string(try postInterpreter.machOObservation.sha256().value)),
            ("interpreter_post_install_codesign_identity_sha256", .string(postInterpreter.codesignIdentitySHA256.value)),
            ("import_probe_sha256", .string(policy.importProbeSHA256.value)),
            ("environment_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_ENVIRONMENT\0", .array(observation.environment.map(JSONValue.string))))),
            ("working_directory", .string(policy.workingDirectory)), ("entries", sealedRuntimeEntries),
            ("entries_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_RUNTIME_ENTRIES\0", sealedRuntimeEntries))),
            ("runtime_output_closure_sha256", .string(try CanonicalJSON.domainSHA256("R4V2_RUNTIME_ENTRIES\0", sealedRuntimeEntries))),
            ("mach_o_dependencies", .array(dependencies.map(\.canonicalValue))),
            ("dependency_closure_sha256", .string(installation.postProbeDependencyClosureSHA256.value)),
            ("pre_install_staged_mach_o_observations", .array(pre.map(\.canonicalValue))),
            ("pre_install_staged_mach_o_observations_sha256", .string(try StagedMachOObservation.collectionSHA256(pre).value)),
            ("staged_mach_o_observations", .array(staged.map(\.canonicalValue))),
            ("staged_mach_o_observations_sha256", .string(try StagedMachOObservation.collectionSHA256(staged).value))]
        let bare = try CanonicalJSON.encode(.object(fields))
        let bytes = try CanonicalJSON.encode(.object(fields + [("sha256", .string(CanonicalJSON.sha256(bare)))]))
        // The closed decoder is the final guard that all staged observations and all domain
        // hashes made it into the exact V2 byte representation.
        _ = try RuntimeManifestV2.decode(String(decoding: bytes, as: UTF8.self), policy: policy)
        return bytes
    }
}

enum MaterializationSealFailure: Error, Equatable { case readback, key, publication, postPublishVerification }

/// The Task 9 handoff is deliberately opaque: only this file's preparation ceremony can mint
/// it, after the signed pair, final graph, preseal, and PRESEALED journal all exist.
struct PreparedPublication {
    let stage: HeldMaterializationStage
    let graph: PublishedAuthorityGraph
    let receipt: ReceiptV2
    let receiptBytes: [UInt8]
    let receiptFileSHA256: SHA256Digest
    let receiptArtifacts: HeldReceiptArtifacts
    let journal: JournalRecordV2
    let admission: Admission
    let signerPublicKeyBytes: [UInt8]

    private init(stage: HeldMaterializationStage, graph: PublishedAuthorityGraph, receipt: ReceiptV2,
                 receiptBytes: [UInt8], receiptFileSHA256: SHA256Digest,
                 receiptArtifacts: HeldReceiptArtifacts, journal: JournalRecordV2, admission: Admission,
                 signerPublicKeyBytes: [UInt8]) {
        self.stage = stage; self.graph = graph; self.receipt = receipt; self.receiptBytes = receiptBytes
        self.receiptFileSHA256 = receiptFileSHA256; self.receiptArtifacts = receiptArtifacts
        self.journal = journal; self.admission = admission; self.signerPublicKeyBytes = signerPublicKeyBytes
    }

    fileprivate static func mint(stage: HeldMaterializationStage, graph: PublishedAuthorityGraph, receipt: ReceiptV2,
                                 receiptBytes: [UInt8], receiptFileSHA256: SHA256Digest,
                                 receiptArtifacts: HeldReceiptArtifacts, journal: JournalRecordV2,
                                 admission: Admission, signerPublicKeyBytes: [UInt8]) -> PreparedPublication {
        PreparedPublication(stage: stage, graph: graph, receipt: receipt, receiptBytes: receiptBytes,
                            receiptFileSHA256: receiptFileSHA256, receiptArtifacts: receiptArtifacts,
                            journal: journal, admission: admission, signerPublicKeyBytes: signerPublicKeyBytes)
    }

#if DEBUG
    // These are inspection-only, deliberately unavailable to production and not constructors.
    var testStage: HeldDescriptor { stage.root }
    var testGraph: PublishedAuthorityGraph { graph }

    func reboundForPublicationTest(stage: HeldMaterializationStage) -> PreparedPublication {
        PreparedPublication(stage: stage, graph: graph, receipt: receipt, receiptBytes: receiptBytes,
                            receiptFileSHA256: receiptFileSHA256, receiptArtifacts: receiptArtifacts,
                            journal: journal, admission: admission, signerPublicKeyBytes: signerPublicKeyBytes)
    }
#endif
}

/// The first production path allowed to create an admission.  It intentionally has no overload
/// accepting a command, an observation prediction, or a path to a stage: those are created and
/// observed only by the retained capabilities from Tasks 6–8.
enum Materializer {
    /// Production prepares a stage only after constructing every root-owned capability itself.
    /// Publication/rename is deliberately a later transaction boundary.
    static func prepareForPublication(_ request: MaterializationRequest) throws -> PreparedPublication {
        try productionPreparation(request).prepared
    }

    private static func productionPreparation(_ request: MaterializationRequest) throws -> (prepared: PreparedPublication, attestation: StorageAttestation, store: DurableStateStore) {
        let approval = try InstalledHelper.requireRootAndIdentity()
        let attestation = try StoragePreflight.attest()
        // This gate must precede any admission attempt, O_EXCL attempt allocation, journal,
        // stage, or materialization mutation.  Retain this exact verified capability below;
        // KeyringVerifier is never reloaded later in the ceremony.
        let signer = try withActiveReceiptSigner(factory: { try KeyringVerifier.loadActive() }) { $0 }
        let bundle = try HeldBundle.openAndValidate(request, approval: approval)
        let storage = DarwinAdmissionStorage(attestation: attestation)
        let admission = try AdmissionController.admit(bundle, storage: storage)
        let store = try DarwinDurableStateStore(attestation: attestation, admission: admission)
        let stage = try store.heldMaterializationStage()
        let prepared = try prepare(stage: stage, bundle: bundle, admission: admission, store: store,
                                   signer: signer, installation: try materializeOrQuarantine(stage: stage, bundle: bundle,
                                                                                               admission: admission, store: store),
                                   operations: productionPreparationOperations)
        return (prepared, attestation, store)
    }

#if DEBUG
    /// Opaque fixture bundle: no raw signer, no caller rows, and no caller path.  Fixture
    /// construction is intentionally kept in the test target; this route only accepts already
    /// held capabilities that mirror the production boundary above.
    static func prepareForPublicationForTest(_ inputs: PublicationPreparationInputs) throws -> PreparedPublication {
        try prepare(stage: inputs.stage, bundle: inputs.bundle, admission: inputs.admission, store: inputs.store,
                    signer: inputs.signer, installation: inputs.installation, operations: inputs.operations)
    }

    /// Narrow order seam: it accepts a typed held signer only from its DEBUG factory and makes
    /// no admission/stage/graph/materialization callback reachable until validation succeeds.
    static func gateAdmissionOnActiveSignerForTest(signerFactory: () throws -> ActiveReceiptSigner,
                                                   afterValidation: () throws -> Void) throws {
        _ = try withActiveReceiptSigner(factory: signerFactory) { signer in
            try afterValidation()
            return signer
        }
    }

    /// Fixture callers can supply only retained capabilities assembled by the test target; they
    /// cannot inject paths, raw rows, or signing bytes into the orchestration boundary.
    static func runForTest(_ request: MaterializationRequest, adapters: TestMaterializationAdapters) throws -> ReceiptV2 {
        guard request == adapters.request else { throw MaterializationFailure.inputPath }
        let inputs = adapters.preparation
        let prepared = try prepare(stage: inputs.stage, bundle: inputs.bundle, admission: inputs.admission,
                                   store: inputs.store, signer: inputs.signer, installation: inputs.installation,
                                   operations: inputs.operations)
        guard try Publication.publishForTest(prepared: prepared, attestation: adapters.attestation,
                                             store: adapters.store, operations: adapters.publicationOperations) == .published else {
            throw MaterializationSealFailure.publication
        }
        do { try Verify.activeForTest(authority: adapters.verificationAuthority) }
        catch { throw MaterializationSealFailure.postPublishVerification }
        return prepared.receipt
    }
#endif

    private static func withActiveReceiptSigner<T>(factory: () throws -> ActiveReceiptSigner,
                                                   _ continuation: (ActiveReceiptSigner) throws -> T) throws -> T {
        let signer = try factory()
        return try continuation(signer)
    }

    private static func materialize(stage: HeldMaterializationStage, bundle: ValidatedBundle,
                                    install: (HeldFinalLaunchLayout) throws -> InstallationObservation) throws -> InstallationObservation {
        let copied = try BundleCopier.copy(bundle, into: stage)
        try copyWheels(bundle: bundle, copied: copied, stage: stage)
        let interpreter = try ArchiveInstaller.extractAndPlace(stage: stage, copied: copied)
        let layout = try stage.bindFinalLaunchLayout(interpreter)
        let observation = try install(layout)
        try canonicalizeOwnedStage(stage: stage, copied: copied)
        return observation
    }

    private static func materialize(stage: HeldMaterializationStage, bundle: ValidatedBundle) throws -> InstallationObservation {
        try materialize(stage: stage, bundle: bundle, install: { layout in
            try ProcessRunner.installAndProbe(VerifiedLaunchCapability(layout: layout, policy: bundle.policy, approval: bundle.approval))
        })
    }

    private static func materializeOrQuarantine(stage: HeldMaterializationStage, bundle: ValidatedBundle,
                                                admission: Admission, store: DurableStateStore,
                                                install: (HeldFinalLaunchLayout) throws -> InstallationObservation) throws -> InstallationObservation {
        do { return try materialize(stage: stage, bundle: bundle, install: install) }
        catch {
            if store.currentState == .stageCreated {
                try StateMachine.terminalizePreRename(using: store, admission: admission, failure: .preRenameIO)
            }
            throw error
        }
    }

    private static func materializeOrQuarantine(stage: HeldMaterializationStage, bundle: ValidatedBundle,
                                                admission: Admission, store: DurableStateStore) throws -> InstallationObservation {
        try materializeOrQuarantine(stage: stage, bundle: bundle, admission: admission, store: store, install: { layout in
            try ProcessRunner.installAndProbe(VerifiedLaunchCapability(layout: layout, policy: bundle.policy, approval: bundle.approval))
        })
    }

#if DEBUG
    /// The fixture process seam still uses the production copy/extract/canonicalization core.
    /// It can substitute only the already-held launch execution, never the stage layout.
    static func materializeForTest(stage: HeldMaterializationStage, bundle: ValidatedBundle,
                                   install: (HeldFinalLaunchLayout) throws -> InstallationObservation) throws -> InstallationObservation {
        try materialize(stage: stage, bundle: bundle, install: install)
    }

    static func materializeOrQuarantineForTest(stage: HeldMaterializationStage, bundle: ValidatedBundle,
                                               admission: Admission, store: DurableStateStore,
                                               install: (HeldFinalLaunchLayout) throws -> InstallationObservation) throws -> InstallationObservation {
        try materializeOrQuarantine(stage: stage, bundle: bundle, admission: admission, store: store, install: install)
    }
#endif

    private static let productionPreparationOperations = PublicationPreparationOperations(
        admissionArtifacts: { try PublishedAuthorityGraph.installAdmissionArtifacts(into: $0, admission: $1) },
        immutable: { try PublicationPOSIX.createImmutableExclusive($0, $1, $2, mode: 0o444) },
        receipt: { try ReceiptPublication.install(into: $0, receiptBytes: $1, signer: $2) },
        prospectiveGraph: { try PublishedAuthorityGraph.buildProspective(stage: $0, artifacts: $1) },
        completeGraph: { try PublishedAuthorityGraph.build(stage: $0, artifacts: $1) },
        preseal: { try TreeEvidence.presealDescendants($0) },
        verifyPresealed: { try TreeEvidence.verifyPresealedDescendants($0) }
    )

    private static func prepare(stage: HeldMaterializationStage, bundle: ValidatedBundle, admission: Admission,
                                store: DurableStateStore, signer: ActiveReceiptSigner,
                                installation: InstallationObservation,
                                operations: PublicationPreparationOperations) throws -> PreparedPublication {
        do {
            let runtimeEntries = try TreeEvidence.runtimeEntries(stage.runtime)
            let runtimeBytes = try ObservedRuntimeManifestFactory.create(bundle: bundle.manifest, policy: bundle.policy,
                                                                          installation: installation, sealedRuntimeEntries: runtimeEntries)
            let runtime = try RuntimeManifestV2.decode(String(decoding: runtimeBytes, as: UTF8.self), policy: bundle.policy)
            let authorityBytes = try AuthorityManifestFactory.create(bundle: bundle, runtime: runtime)
            let authority = try AuthorityManifestV2.decode(String(decoding: authorityBytes, as: UTF8.self))
            let artifacts = try operations.admissionArtifacts(stage.root, admission)
            _ = try operations.immutable(stage.root, "runtime-manifest-v2.json", runtimeBytes)
            _ = try operations.immutable(stage.root, "manifest-v2.json", authorityBytes)
            let prospective = try operations.prospectiveGraph(stage, artifacts)
            let tree = PublishedAuthorityTree(rows: prospective.closureRows)
            let admissionBytes = try POSIX.readAll(try admission.record.file.rebindSameIdentity(in: admission.record.parent, name: admission.record.name))
            let admissionRecord = try AdmissionRecord.decode(String(decoding: admissionBytes, as: UTF8.self))
            let receiptBytes = try ReceiptFactory.create(bundle: bundle, admission: admission, runtime: runtime, authority: authority,
                                                         publishedTree: tree, keyGeneration: signer.generation,
                                                         publicKeySHA256: signer.publicKeySHA256)
            let receipt = try ReceiptV2.decode(String(decoding: receiptBytes, as: UTF8.self))
            try V2Evidence.crossValidate(bundle.manifest, bundle.policy, bundle.approval, admissionRecord, runtime, authority,
                                         receipt, publishedTree: tree, installation: installation)
            let receiptArtifacts = try operations.receipt(stage.root, receiptBytes, signer)
            let graph = try operations.completeGraph(stage, artifacts)
            guard graph.closureSHA256 == prospective.closureSHA256 else { throw MaterializationSealFailure.readback }
            try operations.preseal(stage.root)
            // Re-open every sealed descendant before minting the opaque capability.  This catches
            // metadata swaps after the initial graph and makes the capability bind final bytes.
            try operations.verifyPresealed(stage.root)
            let finalGraph = try operations.completeGraph(stage, artifacts)
            guard finalGraph.closureSHA256 == graph.closureSHA256,
                  finalGraph.closureRows == graph.closureRows else { throw MaterializationSealFailure.readback }
            let finalTree = PublishedAuthorityTree(rows: finalGraph.closureRows)
            try V2Evidence.crossValidate(bundle.manifest, bundle.policy, bundle.approval, admissionRecord, runtime, authority,
                                         receipt, publishedTree: finalTree, installation: installation)
            try POSIX.durableVolumeBarrier(stage.volume)
            let receiptSHA = ReceiptV2.receiptFileSHA256(receiptBytes)
            let journal = try JournalRecordV2.make(sequence: 2, phase: .presealed, admission: admission,
                                                   publishedClosureSHA256: graph.closureSHA256, receiptFileSHA256: receiptSHA)
            try StateMachine.append(journal, using: store, admission: admission)
            return PreparedPublication.mint(stage: stage, graph: finalGraph, receipt: receipt,
                                            receiptBytes: receiptBytes, receiptFileSHA256: receiptSHA,
                                            receiptArtifacts: receiptArtifacts, journal: journal,
                                            admission: admission, signerPublicKeyBytes: signer.publicKeyBytes)
        } catch {
            if store.currentState == .stageCreated {
                try StateMachine.terminalizePreRename(using: store, admission: admission, failure: .preRenameIO)
            }
            throw error
        }
    }

    /// Kept as the CLI compatibility boundary until Task 12 reconnects this entrypoint to the
    /// separate publication transaction.  It must never retain the pre-Task-9 Key-B bypass.
    static func run(_ request: MaterializationRequest) throws -> ReceiptV2 {
        let preparation = try productionPreparation(request)
        guard try Publication.publish(preparation.prepared, attestation: preparation.attestation,
                                      store: preparation.store) == .published else {
            throw MaterializationSealFailure.publication
        }
        do { try Verify.active() }
        catch { throw MaterializationSealFailure.postPublishVerification }
        return preparation.prepared.receipt
    }

    private static func copyWheels(bundle: ValidatedBundle, copied: CopiedBundle, stage: HeldMaterializationStage) throws {
        for filename in bundle.manifest.wheelFilenames {
            let source = try POSIX.openAt(copied.input, name: "artifacts", flags: O_RDONLY | O_DIRECTORY)
            let wheel = try POSIX.openAt(source, name: filename, flags: O_RDONLY)
            let output = try POSIX.createExclusive(stage.wheelhouse, name: filename, mode: 0o600)
            let bytes = try POSIX.readAll(wheel)
            try POSIX.writeAll(output, bytes)
            guard CanonicalJSON.sha256(bytes) == bundle.manifest.wheelSHA256[bundle.manifest.wheelFilenames.firstIndex(of: filename)!].value else { throw ArchiveFailure.sourceChanged }
            try POSIX.durableRegular(output)
            _ = try output.rebind(in: stage.wheelhouse, name: filename)
        }
        try POSIX.durableDirectory(stage.wheelhouse)
    }

    /// The closed launch policy requires root `home`, `tmp`, and `wheelhouse` while install and
    /// probe run.  Only after the complete observation returns are those held directories moved
    /// into the fresh scratch identity with the copy/extract residue and recursively unlinked.
    /// Any name or inode replacement fails closed before PRESEALED, allowing the existing
    /// pre-publish quarantine path to own recovery.
    private static func canonicalizeOwnedStage(stage: HeldMaterializationStage, copied: CopiedBundle) throws {
        let root = try rebindStageRoot(stage)
        let scratch = try stage.scratch.rebindSameIdentity(in: root, name: "scratch", directory: true)
        let copiedInput = try copied.input.rebindSameIdentity(in: scratch, name: "input", directory: true)
        let checkout = try POSIX.openAt(copiedInput, name: "checkout", flags: O_RDONLY | O_DIRECTORY)
        _ = try checkout.rebindSameIdentity(in: copiedInput, name: "checkout", directory: true)
        try POSIX.renameExclusive(from: copiedInput, name: "checkout", to: root, destination: "checkout")
        try POSIX.durableDirectory(copiedInput)
        try POSIX.durableDirectory(root)
        try moveHeldLaunchResidue(stage.home, named: "home", from: root, into: scratch)
        try moveHeldLaunchResidue(stage.temporary, named: "tmp", from: root, into: scratch)
        try moveHeldLaunchResidue(stage.wheelhouse, named: "wheelhouse", from: root, into: scratch)
        try unlinkHeldOwnedTree(scratch, from: root, name: "scratch")
        let reboundRoot = try rebindStageRoot(stage)
        _ = try checkout.rebindSameIdentity(in: reboundRoot, name: "checkout", directory: true)
        _ = try stage.runtime.rebindSameIdentity(in: reboundRoot, name: "runtime", directory: true)
        guard try POSIX.directoryEntryNames(reboundRoot) == Set(["checkout", "runtime"]) else { throw ArchiveFailure.invalidStage }
        try POSIX.durableDirectory(reboundRoot)
    }

    private static func moveHeldLaunchResidue(_ held: HeldDescriptor, named name: String,
                                              from root: HeldDescriptor, into scratch: HeldDescriptor) throws {
        let source = try held.rebindSameIdentity(in: root, name: name, directory: true)
        do { _ = try POSIX.openAt(scratch, name: name, flags: O_RDONLY | O_DIRECTORY); throw ArchiveFailure.destinationExists }
        catch POSIXFailure.system(let code) where code == ENOENT { }
        try POSIX.renameExclusive(from: root, name: name, to: scratch, destination: name)
        _ = try source.rebindSameIdentity(in: scratch, name: name, directory: true)
        try POSIX.durableDirectory(root)
        try POSIX.durableDirectory(scratch)
    }

    private static func rebindStageRoot(_ stage: HeldMaterializationStage) throws -> HeldDescriptor {
#if DEBUG
        if stage.stageName.isEmpty { return stage.root }
#endif
        return try stage.root.rebindSameIdentity(in: stage.work, name: stage.stageName, directory: true)
    }

    private static func unlinkHeldOwnedTree(_ held: HeldDescriptor, from parent: HeldDescriptor, name: String) throws {
        let directory = try held.rebindSameIdentity(in: parent, name: name, directory: true)
        for childName in try POSIX.directoryEntryNames(directory) {
            let child = try POSIX.openAt(directory, name: childName, flags: O_RDONLY)
            let metadata = try POSIX.metadata(child)
            switch metadata.type {
            case S_IFDIR:
                try unlinkHeldOwnedTree(child, from: directory, name: childName)
            case S_IFREG:
                _ = try child.rebindSameIdentity(in: directory, name: childName)
                try POSIX.unlinkAt(directory, name: childName)
            default:
                throw ArchiveFailure.invalidStage
            }
        }
        _ = try directory.rebindSameIdentity(in: parent, name: name, directory: true)
        try POSIX.unlinkAt(parent, name: name, directory: true)
        try POSIX.durableDirectory(parent)
    }

}

#if DEBUG
/// Opaque fixture-only transaction inputs. The type is compiled out of the release helper and
/// makes a test hand over retained preparation/publication capabilities rather than synthetic
/// receipt or journal documents.
struct TestMaterializationAdapters {
    let request: MaterializationRequest
    /// Held inputs after fixture-only identity/bundle/admission/stage/materialization seams have
    /// completed.  `runForTest` owns the prepare, preseal journal, rename-intent, publication,
    /// and receipt-return ordering itself.
    let preparation: PublicationPreparationInputs
    let attestation: StorageAttestation
    let store: DurableStateStore
    let publicationOperations: PublicationTransactionOperations
    /// Read-only verification capability assembled by the Task-11 fixture.  Publication never
    /// receives it, so test orchestration cannot mutate authority state after PUBLISHED.
    let verificationAuthority: Verify.VerificationAuthority
}
#endif

private enum AuthorityManifestFactory {
    static func create(bundle: ValidatedBundle, runtime: RuntimeManifestV2) throws -> [UInt8] {
        let fields: [(String, JSONValue)] = [("kind", .string("R4_PROTECTED_EXECUTION_MANIFEST_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)),
            ("git", .object([("head", .string(bundle.manifest.git.head.value)), ("tree", .string(bundle.manifest.git.tree.value)), ("required_blobs", .array(bundle.manifest.git.requiredBlobs.map { .object([("path", .string($0.path)), ("blob", .string($0.blob.value)), ("sha256", .string($0.sha256.value))]) }))])),
            ("entries", .array(bundle.manifest.entries.map { .object([("path", .string($0.path)), ("type", .string($0.type)), ("mode", .string($0.mode)), ("size", .integer($0.size)), ("sha256", .string($0.sha256.value))]) })),
            ("runtime_manifest_sha256", .string(runtime.sha256.value)), ("input_bundle_manifest_sha256", .string(bundle.manifest.sha256.value)), ("materialization_policy_sha256", .string(bundle.policy.sha256.value))]
        let bare = try CanonicalJSON.encode(.object(fields)); return try CanonicalJSON.encode(.object(fields + [("sha256", .string(CanonicalJSON.sha256(bare)))]))
    }
}

enum ReceiptFactory {
    static func create(bundle: ValidatedBundle, admission: Admission, runtime: RuntimeManifestV2, authority: AuthorityManifestV2,
                       publishedTree: PublishedAuthorityTree, keyGeneration: Int64, publicKeySHA256: SHA256Digest) throws -> [UInt8] {
        let bytes = try POSIX.readAll(admission.record.file)
        let record = try AdmissionRecord.decode(String(decoding: bytes, as: UTF8.self))
        // The admission locator and journal bind the complete canonical admission file.
        // Keep the signed receipt on that same byte-level binding, rather than on the
        // admission document's nested self-digest.
        guard bytes == (try record.canonicalRecordBytes()) else { throw EvidenceError.invalidDigest }
        return try create(manifest: bundle.manifest, approval: bundle.approval, admission: record,
                          admissionFileSHA256: try SHA256Digest(CanonicalJSON.sha256(bytes)), runtime: runtime,
                          authority: authority, publishedTree: publishedTree, keyGeneration: keyGeneration,
                          publicKeySHA256: publicKeySHA256)
    }

    static func create(manifest: InputBundleManifest, approval: Task14ApprovalV2, admission: AdmissionRecord,
                       runtime: RuntimeManifestV2, authority: AuthorityManifestV2,
                       publishedTree: PublishedAuthorityTree, keyGeneration: Int64,
                       publicKeySHA256: SHA256Digest) throws -> [UInt8] {
        return try create(manifest: manifest, approval: approval, admission: admission,
                          admissionFileSHA256: try SHA256Digest(CanonicalJSON.sha256(try admission.canonicalRecordBytes())),
                          runtime: runtime, authority: authority, publishedTree: publishedTree,
                          keyGeneration: keyGeneration, publicKeySHA256: publicKeySHA256)
    }

    private static func create(manifest: InputBundleManifest, approval: Task14ApprovalV2, admission: AdmissionRecord,
                               admissionFileSHA256: SHA256Digest, runtime: RuntimeManifestV2,
                               authority: AuthorityManifestV2, publishedTree: PublishedAuthorityTree,
                               keyGeneration: Int64, publicKeySHA256: SHA256Digest) throws -> [UInt8] {
        let fields: [(String, JSONValue)] = [("kind", .string("R4_PROTECTED_EXECUTION_RECEIPT_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)), ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("state", .string("SEALED")), ("key_generation", .integer(keyGeneration)),
            ("manifest_sha256", .string(authority.sha256.value)), ("runtime_manifest_sha256", .string(runtime.sha256.value)), ("task14_approval_sha256", .string(approval.sha256.value)), ("input_bundle_manifest_sha256", .string(manifest.sha256.value)), ("lock_sha256", .string(manifest.lockSHA256.value)), ("materialization_policy_sha256", .string(manifest.materializationPolicySHA256.value)), ("wheel_set_sha256", .string(manifest.wheelSetSHA256.value)), ("runtime_output_closure_sha256", .string(runtime.runtimeOutputClosureSHA256.value)), ("dependency_closure_sha256", .string(runtime.dependencyClosureSHA256.value)), ("pre_install_staged_mach_o_observations_sha256", .string(runtime.preInstallStagedMachOObservationsSHA256.value)), ("staged_mach_o_observations_sha256", .string(runtime.stagedMachOObservationsSHA256.value)), ("admission_record_sha256", .string(admissionFileSHA256.value)), ("git_head", .string(manifest.git.head.value)), ("git_tree", .string(manifest.git.tree.value)), ("required_blob_set_sha256", .string(manifest.requiredBlobSetSHA256.value)), ("provisioner_binary_sha256", .string(approval.provisionerBinarySHA256.value)), ("helper_codesign_requirement_sha256", .string(approval.provisionerDesignatedRequirementSHA256.value)), ("helper_team_id", .string(approval.helperTeamID)), ("helper_signing_identity_sha1", .string(approval.helperSigningIdentitySHA1)), ("public_key_sha256", .string(publicKeySHA256.value)), ("published_closure_sha256", .string(try publishedTree.sha256)),
            ("sandbox_launcher_path", .string(runtime.sandboxLauncherPath)), ("sandbox_launcher_descriptor_identity_sha256", .string(runtime.sandboxLauncherDescriptorIdentitySHA256.value)), ("sandbox_launcher_sha256", .string(runtime.sandboxLauncherSHA256.value)), ("sandbox_launcher_codesign_identity_sha256", .string(runtime.sandboxLauncherCodesignIdentitySHA256.value)), ("sandbox_profile_template_sha256", .string(runtime.sandboxProfileTemplateSHA256.value)), ("sandbox_profile_sha256", .string(runtime.sandboxProfileSHA256.value)), ("child_command_template_sha256", .string(runtime.childCommandTemplateSHA256.value)), ("installer_command_sha256", .string(runtime.installerCommandSHA256.value)), ("import_command_sha256", .string(runtime.importCommandSHA256.value)), ("interpreter_pre_install_sha256", .string(runtime.interpreterPreInstallSHA256.value)), ("interpreter_pre_install_mach_o_sha256", .string(runtime.interpreterPreInstallMachOSHA256.value)), ("interpreter_pre_install_codesign_identity_sha256", .string(runtime.interpreterPreInstallCodesignIdentitySHA256.value)), ("interpreter_post_install_sha256", .string(runtime.interpreterPostInstallSHA256.value)), ("interpreter_post_install_mach_o_sha256", .string(runtime.interpreterPostInstallMachOSHA256.value)), ("interpreter_post_install_codesign_identity_sha256", .string(runtime.interpreterPostInstallCodesignIdentitySHA256.value)), ("environment_sha256", .string(runtime.environmentSHA256.value))]
        let bare = try CanonicalJSON.encode(.object(fields)); return try CanonicalJSON.encode(.object(fields + [("sha256", .string(CanonicalJSON.sha256(bare)))]))
    }
}

private enum TreeEvidence {
    static func runtimeEntries(_ root: HeldDescriptor) throws -> JSONValue {
        let descendants = try entries(root, path: "runtime")
        let digest = try directoryDigest(descendants, parent: "runtime")
        return .array(([try ManifestEntry(path: "runtime", type: "directory", mode: "0555", size: 0, sha256: SHA256Digest(digest))] + descendants).map(value))
    }
    private static func entries(_ directory: HeldDescriptor, path: String) throws -> [ManifestEntry] {
        let names = try POSIX.directoryEntryNames(directory).sorted(); var result: [ManifestEntry] = []
        for name in names {
            let held = try POSIX.openAt(directory, name: name, flags: O_RDONLY)
            let meta = try POSIX.metadata(held); let child = path + "/" + name
            if meta.type == S_IFDIR {
                let descendants = try entries(held, path: child); let digest = try directoryDigest(descendants.filter { $0.path.hasPrefix(child + "/") }, parent: child)
                result.append(try ManifestEntry(path: child, type: "directory", mode: "0555", size: 0, sha256: SHA256Digest(digest))); result += descendants
            } else if meta.type == S_IFREG {
                let mode = (meta.mode & 0o111) == 0 ? "0444" : "0555"
                result.append(try ManifestEntry(path: child, type: "regular", mode: mode, size: meta.size, sha256: SHA256Digest(CanonicalJSON.sha256(try POSIX.readAll(held)))))
            } else { throw MaterializationSealFailure.readback }
        }
        return result.sorted { $0.path < $1.path }
    }
    private static func directoryDigest(_ descendants: [ManifestEntry], parent: String) throws -> String {
        let rows = descendants.compactMap { row -> JSONValue? in let prefix = parent + "/"; guard row.path.hasPrefix(prefix), !row.path.dropFirst(prefix.count).contains("/") else { return nil }; return .object([("name", .string(String(row.path.dropFirst(prefix.count)))), ("type", .string(row.type)), ("mode", .string(row.mode)), ("size", .integer(row.size)), ("sha256", .string(row.sha256.value))]) }.sorted { try! CanonicalJSON.encode($0).lexicographicallyPrecedes(try! CanonicalJSON.encode($1)) }
        return try CanonicalJSON.domainSHA256("R4DIR\0", .array(rows))
    }
    private static func value(_ row: ManifestEntry) -> JSONValue { .object([("path", .string(row.path)), ("type", .string(row.type)), ("mode", .string(row.mode)), ("size", .integer(row.size)), ("sha256", .string(row.sha256.value))]) }
    static func presealDescendants(_ root: HeldDescriptor) throws {
        for name in try POSIX.directoryEntryNames(root).sorted() {
            let held = try POSIX.openAt(root, name: name, flags: O_RDONLY); let meta = try POSIX.metadata(held)
            if meta.type == S_IFDIR {
                try presealDescendants(held)
                guard fchmod(held.fd, 0o555) == 0 else { throw POSIXFailure.system(errno) }
            }
            else if meta.type == S_IFREG {
                let executable = (meta.mode & 0o111) != 0
                let mode: mode_t = executable ? 0o555 : 0o444
                guard fchmod(held.fd, mode) == 0 else { throw POSIXFailure.system(errno) }
            }
            else { throw MaterializationSealFailure.readback }
            try POSIX.requireNoExtendedACL(held)
            try POSIX.requireNoUnexpectedExtendedAttributes(held)
            try POSIX.setImmutable(held)
            if meta.type == S_IFDIR {
                try held.requireSealedDirectory(mode: 0o555)
                try POSIX.durableDirectory(held)
            } else {
                let mode: mode_t = (meta.mode & 0o111) == 0 ? 0o444 : 0o555
                try held.requireSealedRegular(mode: mode)
                try POSIX.durableRegular(held)
            }
            let rebound = try held.rebindSameIdentity(in: root, name: name, directory: meta.type == S_IFDIR)
            if meta.type == S_IFDIR { try rebound.requireSealedDirectory(mode: 0o555) }
            else { try rebound.requireSealedRegular(mode: (meta.mode & 0o111) == 0 ? 0o444 : 0o555) }
        }
        // The stage root is intentionally left mutable (0700) until Task 10 has entered its
        // no-replace rename boundary.  Only its descendants are receipt-covered and presealed.
        try POSIX.durableDirectory(root)
    }

    static func verifyPresealedDescendants(_ root: HeldDescriptor) throws {
        for name in try POSIX.directoryEntryNames(root).sorted() {
            let held = try POSIX.openAt(root, name: name, flags: O_RDONLY)
            let meta = try POSIX.metadata(held)
            if meta.type == S_IFDIR {
                try verifyPresealedDescendants(held)
                try held.requireSealedDirectory(mode: 0o555)
            } else if meta.type == S_IFREG {
                try held.requireSealedRegular(mode: (meta.mode & 0o111) == 0 ? 0o444 : 0o555)
            } else { throw MaterializationSealFailure.readback }
            _ = try held.rebindSameIdentity(in: root, name: name, directory: meta.type == S_IFDIR)
        }
    }
}
