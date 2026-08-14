import Foundation

enum MaterializationState: String, Codable, CaseIterable {
    case absent = "ABSENT"
    case admitted = "ADMITTED"
    case stageCreated = "STAGE_CREATED"
    case presealed = "PRESEALED"
    case renameIntent = "RENAME_INTENT"
    case published = "PUBLISHED"
    case quarantined = "QUARANTINED"
    case publishIndeterminate = "PUBLISH_INDETERMINATE"
}

enum StateFailure: Error, Equatable { case illegalTransition, terminalizationFailed }

protocol DurableStateStore: AnyObject {
    var currentState: MaterializationState { get }
    func append(_ record: JournalRecordV2, admission: Admission) throws
    func moveToQuarantine(admission: Admission, failure: JournalFailureClass) throws
    func markPublishIndeterminate(admission: Admission, failure: JournalFailureClass) throws
}

extension DurableStateStore {
    /// Compatibility for the process-global signal latch. Signals always occur before a
    /// publication rename in the current caller, so their only legal terminal evidence is a
    /// pre-rename quarantine record.
    func quarantineAfterFailure(_ admission: Admission) throws {
        try moveToQuarantine(admission: admission, failure: .preRenameIO)
    }
}

enum StateMachine {
    enum Seam: String, CaseIterable {
        case journalWrite, journalFsync, journalImmutable, journalRebind, sigint, sigterm
        static var allCases: [Seam] { [.journalWrite, .journalFsync, .journalImmutable, .journalRebind] }
    }

    static let successors: [MaterializationState: MaterializationState] = [
        .admitted: .stageCreated,
        .stageCreated: .presealed,
        .presealed: .renameIntent,
        .renameIntent: .published
    ]

    static func stageName(_ admission: Admission) throws -> String {
        try AdmissionController.stageName(admission.attempt)
    }

    static func append(_ record: JournalRecordV2, using store: DurableStateStore,
                       admission: Admission) throws {
        try store.append(record, admission: admission)
    }

    static func terminalizePreRename(using store: DurableStateStore, admission: Admission,
                                     failure: JournalFailureClass) throws {
        guard failure.rawValue.hasPrefix("PRE_") else { throw StateFailure.illegalTransition }
        do { try store.moveToQuarantine(admission: admission, failure: failure) }
        catch { throw StateFailure.terminalizationFailed }
    }

    static func terminalizePostRename(using store: DurableStateStore, admission: Admission,
                                      failure: JournalFailureClass) throws {
        guard failure.rawValue.hasPrefix("POST_") else { throw StateFailure.illegalTransition }
        do { try store.markPublishIndeterminate(admission: admission, failure: failure) }
        catch { throw StateFailure.terminalizationFailed }
    }
}

extension JournalRecordV2 {
    /// Constructs a fully self-authenticating, canonical record. There is intentionally no
    /// timestamp: V2 journals identify order by fixed sequence and immutable filename only.
    static func make(sequence: Int64, phase: JournalPhase, admission: Admission,
                     failure: JournalFailureClass? = nil,
                     publishedClosureSHA256: SHA256Digest? = nil,
                     receiptFileSHA256: SHA256Digest? = nil,
                     publishedDevice: Int64? = nil,
                     publishedInode: Int64? = nil) throws -> JournalRecordV2 {
        let isPreFailure = failure?.rawValue.hasPrefix("PRE_") == true
        let isPostFailure = failure?.rawValue.hasPrefix("POST_") == true
        switch phase {
        case .stageCreated:
            guard sequence == 1, failure == nil, publishedClosureSHA256 == nil,
                  receiptFileSHA256 == nil, publishedDevice == nil, publishedInode == nil else { throw EvidenceError.invalidValue }
        case .presealed:
            guard sequence == 2, failure == nil, publishedClosureSHA256 != nil,
                  receiptFileSHA256 != nil, publishedDevice == nil, publishedInode == nil else { throw EvidenceError.invalidValue }
        case .renameIntent:
            guard sequence == 3, failure == nil, publishedClosureSHA256 != nil,
                  receiptFileSHA256 != nil, publishedDevice == nil, publishedInode == nil else { throw EvidenceError.invalidValue }
        case .published:
            guard sequence == 4, failure == nil, publishedClosureSHA256 != nil,
                  receiptFileSHA256 != nil, let publishedDevice, let publishedInode,
                  publishedDevice >= 0, publishedInode >= 0 else { throw EvidenceError.invalidValue }
        case .quarantined:
            guard sequence == 4, isPreFailure, publishedClosureSHA256 == nil,
                  receiptFileSHA256 == nil, publishedDevice == nil, publishedInode == nil else { throw EvidenceError.invalidValue }
        case .publishIndeterminate:
            guard sequence == 4, isPostFailure, publishedClosureSHA256 != nil,
                  receiptFileSHA256 != nil, publishedDevice == nil, publishedInode == nil else { throw EvidenceError.invalidValue }
        }

        let placeholder = try JournalRecordV2(sequence: sequence, phase: phase,
                                               admissionRecordSHA256: admission.recordSHA256,
                                               receiptFileSHA256: receiptFileSHA256,
                                               failureClass: failure,
                                               publishedClosureSHA256: publishedClosureSHA256,
                                               publishedDevice: publishedDevice,
                                               publishedInode: publishedInode,
                                               attemptUUID: admission.attempt,
                                               sha256: SHA256Digest(String(repeating: "0", count: 64)))
        let journalSHA = try SHA256Digest(CanonicalJSON.sha256(placeholder.journalPreimage()))
        let core = try CanonicalJSON.decode(bytes: Array(placeholder.journalPreimage().dropFirst("R4JOURNAL2\0".utf8.count)))
        guard case .object(var fields) = core else { throw EvidenceError.invalidShape }
        switch phase {
        case .stageCreated: fields += [("stage_name", .string(admission.attempt))]
        case .presealed, .renameIntent:
            fields += [("published_closure_sha256", .string(publishedClosureSHA256!.value)),
                       ("receipt_file_sha256", .string(receiptFileSHA256!.value))]
        case .published:
            fields += [("published_closure_sha256", .string(publishedClosureSHA256!.value)),
                       ("receipt_file_sha256", .string(receiptFileSHA256!.value)),
                       ("published_device", .integer(publishedDevice!)),
                       ("published_inode", .integer(publishedInode!))]
        case .quarantined:
            fields += [("failure_class", .string(failure!.rawValue)), ("quarantine_name", .string(admission.attempt))]
        case .publishIndeterminate:
            fields += [("failure_class", .string(failure!.rawValue)),
                       ("published_closure_sha256", .string(publishedClosureSHA256!.value)),
                       ("receipt_file_sha256", .string(receiptFileSHA256!.value))]
        }
        fields += [("journal_sha256", .string(journalSHA.value))]
        let sha256 = try SHA256Digest(CanonicalJSON.sha256(try CanonicalJSON.encode(.object(fields))))
        let result = JournalRecordV2(sequence: sequence, phase: phase,
                                         admissionRecordSHA256: admission.recordSHA256,
                                         receiptFileSHA256: receiptFileSHA256,
                                         failureClass: failure,
                                         publishedClosureSHA256: publishedClosureSHA256,
                                         publishedDevice: publishedDevice,
                                         publishedInode: publishedInode,
                                         attemptUUID: admission.attempt, sha256: sha256)
        _ = try JournalRecordV2.decode(result.canonicalRecordBytes())
        return result
    }
}

/// One admission owns one immutable journal attempt. The held descriptors are the only write
/// capability; reopening a path is always followed by same-identity rebind before use.
final class DarwinDurableStateStore: DurableStateStore {
    private let attestation: StorageAttestation
    private let revalidateRegistries: () throws -> Void
    private let createImmutableRecord: (HeldDescriptor, String, [UInt8]) throws -> HeldDescriptor
    private let validateExistingPrefix: (HeldDescriptor, Set<String>, Admission) throws -> Void
    private let normalizeMutableDirectory: (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor
    private let validateMutableDirectory: (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor
    private var stage: HeldDescriptor
    private var journal: HeldDescriptor
    private let stageName: String
    private let lock = NSLock()
    private(set) var currentState: MaterializationState
    private var lastClosure: SHA256Digest?
    private var lastReceipt: SHA256Digest?
    private var materializationCapability: HeldMaterializationStage?

    convenience init(attestation: StorageAttestation, admission: Admission) throws {
        try self.init(attestation: attestation, admission: admission,
                      revalidateRegistries: { try attestation.revalidateMutableRegistries() },
                      createImmutableRecord: { parent, name, bytes in
                          try PublicationPOSIX.createImmutableExclusive(parent, name, bytes, mode: 0o444)
                      }, validateExistingPrefix: Self.validateProductionPrefix,
                      normalizeMutableDirectory: Self.normalizeProductionMutableDirectory,
                      validateMutableDirectory: Self.validateProductionMutableDirectory)
    }

    private init(attestation: StorageAttestation, admission: Admission,
                 revalidateRegistries: @escaping () throws -> Void,
                 createImmutableRecord: @escaping (HeldDescriptor, String, [UInt8]) throws -> HeldDescriptor,
                 validateExistingPrefix: @escaping (HeldDescriptor, Set<String>, Admission) throws -> Void,
                 normalizeMutableDirectory: @escaping (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor,
                 validateMutableDirectory: @escaping (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor) throws {
        self.attestation = attestation
        self.revalidateRegistries = revalidateRegistries
        self.createImmutableRecord = createImmutableRecord
        self.validateExistingPrefix = validateExistingPrefix
        self.normalizeMutableDirectory = normalizeMutableDirectory
        self.validateMutableDirectory = validateMutableDirectory
        let attemptName = try StateMachine.stageName(admission)
        self.stageName = attemptName
        let created: (HeldDescriptor, HeldDescriptor) = try AdmissionRegistryLock.withExclusive(attestation.journals) {
            try revalidateRegistries()
            // The journals registry lock serializes admission-attempt setup. Neither directory
            // can be adopted: O_EXCL mkdir failure leaves prior residue untouched.
            let stage = try normalizeMutableDirectory(try POSIX.mkdirAt(attestation.work, name: attemptName, mode: 0o700), attestation.work, attemptName)
            let journal = try normalizeMutableDirectory(try POSIX.mkdirAt(attestation.journals, name: attemptName, mode: 0o700), attestation.journals, attemptName)
            try POSIX.durableDirectory(attestation.work)
            try POSIX.durableDirectory(attestation.journals)
            try POSIX.durableDirectory(attestation.control)
            try POSIX.durableDirectory(attestation.root)
            try POSIX.durableVolumeBarrier(attestation.root)
            return (stage, journal)
        }
        self.stage = created.0
        self.journal = created.1
        self.currentState = .admitted
        let record = try JournalRecordV2.make(sequence: 1, phase: .stageCreated, admission: admission)
        try append(record, admission: admission)
    }

    func append(_ record: JournalRecordV2, admission: Admission) throws {
        lock.lock(); defer { lock.unlock() }
        try withAttemptJournalLock { try appendLocked(record, admission: admission) }
    }

    func heldMaterializationStage() throws -> HeldMaterializationStage {
        lock.lock(); defer { lock.unlock() }
        guard currentState == .stageCreated else { throw StateFailure.illegalTransition }
        if let materializationCapability { return materializationCapability }
        let capability = try HeldMaterializationStage.makeProduction(root: stage, volume: attestation.root,
                                                                       work: attestation.work, stageName: stageName)
        try POSIX.durableDirectory(stage)
        try POSIX.durableVolumeBarrier(attestation.root)
        materializationCapability = capability
        return capability
    }

    func moveToQuarantine(admission: Admission, failure: JournalFailureClass) throws {
        lock.lock(); defer { lock.unlock() }
        try withAttemptJournalLock {
            guard failure.rawValue.hasPrefix("PRE_"),
                  [.stageCreated, .presealed, .renameIntent].contains(currentState) else { throw StateFailure.illegalTransition }
            let oldStage = stage
            _ = try oldStage.rebindSameIdentity(in: attestation.work, name: stageName, directory: true)
            try POSIX.renameExclusive(from: attestation.work, name: stageName, to: attestation.quarantine, destination: stageName)
            let relocated = try oldStage.rebindSameIdentity(in: attestation.quarantine, name: stageName, directory: true)
            try POSIX.durableDirectory(attestation.work)
            try POSIX.durableDirectory(attestation.quarantine)
            try POSIX.durableVolumeBarrier(attestation.root)
            stage = relocated
            let record = try JournalRecordV2.make(sequence: 4, phase: .quarantined, admission: admission, failure: failure)
            try appendLocked(record, admission: admission)
        }
    }

    func markPublishIndeterminate(admission: Admission, failure: JournalFailureClass) throws {
        lock.lock(); defer { lock.unlock() }
        try withAttemptJournalLock {
            guard failure.rawValue.hasPrefix("POST_"), currentState == .renameIntent,
                  let lastClosure, let lastReceipt else { throw StateFailure.illegalTransition }
            let record = try JournalRecordV2.make(sequence: 4, phase: .publishIndeterminate, admission: admission,
                                                   failure: failure, publishedClosureSHA256: lastClosure,
                                                   receiptFileSHA256: lastReceipt)
            try appendLocked(record, admission: admission)
        }
    }

    /// The held attempt directory is the cross-process serialisation capability. The state
    /// successor test and exclusive append execute inside the same kernel lock, not merely this
    /// process-local lock.
    private func withAttemptJournalLock<T>(_ body: () throws -> T) throws -> T {
        journal = try validateMutableDirectory(journal, attestation.journals, stageName)
        return try AdmissionRegistryLock.withExclusive(journal, body)
    }

    private func appendLocked(_ record: JournalRecordV2, admission: Admission) throws {
        guard record.attemptUUID == admission.attempt,
              record.admissionRecordSHA256 == admission.recordSHA256,
              StateMachine.successors[currentState] == state(for: record.phase) ||
                (record.phase == .quarantined && [.stageCreated, .presealed, .renameIntent].contains(currentState)) ||
                (record.phase == .publishIndeterminate && currentState == .renameIntent) else { throw StateFailure.illegalTransition }
        try revalidateRegistries()
        journal = try validateMutableDirectory(journal, attestation.journals, stageName)
        let expectedNames = try expectedPrefix(before: record, currentState: currentState)
        try validateExistingPrefix(journal, expectedNames, admission)
        let bytes = try record.canonicalRecordBytes()
        let created = try createImmutableRecord(journal, record.filename, bytes)
        _ = try created.rebindSameIdentity(in: journal, name: record.filename)
        try POSIX.durableDirectory(journal)
        journal = try validateMutableDirectory(journal, attestation.journals, stageName)
        try POSIX.durableDirectory(attestation.journals)
        try POSIX.durableDirectory(attestation.control)
        try POSIX.durableDirectory(attestation.root)
        try POSIX.durableVolumeBarrier(attestation.root)
        currentState = state(for: record.phase)
        lastClosure = record.publishedClosureSHA256 ?? lastClosure
        lastReceipt = record.receiptFileSHA256 ?? lastReceipt
    }

    /// A later operation never trusts a filename-only prefix. Every predecessor is reopened
    /// without following links, rebound to the held attempt, checked as immutable metadata, and
    /// decoded back to the exact canonical bytes it originally committed.
    private static func validateProductionPrefix(_ journal: HeldDescriptor, _ expectedNames: Set<String>, _ admission: Admission) throws {
        guard try POSIX.directoryEntryNames(journal) == expectedNames else { throw StateFailure.illegalTransition }
        for name in expectedNames {
            let held = try POSIX.openAt(journal, name: name, flags: O_RDONLY)
            _ = try held.rebindSameIdentity(in: journal, name: name)
            try held.requireSealedRegular(mode: 0o444)
            let bytes = try POSIX.readAll(held)
            let decoded = try JournalRecordV2.decode(bytes)
            guard decoded.filename == name,
                  decoded.attemptUUID == admission.attempt,
                  decoded.admissionRecordSHA256 == admission.recordSHA256,
                  try decoded.canonicalRecordBytes() == bytes else { throw StateFailure.illegalTransition }
        }
    }

    private static func normalizeProductionMutableDirectory(_ directory: HeldDescriptor, _ parent: HeldDescriptor,
                                                            _ name: String) throws -> HeldDescriptor {
        try POSIX.retryEINTR { Darwin.fchown(directory.fd, 0, 0) }
        try POSIX.retryEINTR { Darwin.fchmod(directory.fd, 0o700) }
        let rebound = try directory.rebindSameIdentity(in: parent, name: name, directory: true)
        try requireMutableDirectory(rebound, uid: 0, gid: 0)
        try POSIX.durableDirectory(rebound)
        return rebound
    }

    private static func validateProductionMutableDirectory(_ directory: HeldDescriptor, _ parent: HeldDescriptor,
                                                           _ name: String) throws -> HeldDescriptor {
        let rebound = try directory.rebindSameIdentity(in: parent, name: name, directory: true)
        try requireMutableDirectory(rebound, uid: 0, gid: 0)
        return rebound
    }

    private static func requireMutableDirectory(_ directory: HeldDescriptor, uid: uid_t, gid: gid_t) throws {
        var status = stat()
        try POSIX.fstatRetry(directory.fd, &status)
        guard (status.st_flags & UInt32(SF_IMMUTABLE)) == 0 else { throw POSIXFailure.unsafeObject }
        try POSIX.requireDirectory(directory, uid: uid, gid: gid, mode: 0o700)
        try POSIX.requireNoUnexpectedExtendedAttributes(directory)
    }

    /// Read-only restart classification. It never resumes or repairs an attempt: only a complete,
    /// terminal immutable journal bound to the same admission can be described. Callers still
    /// treat every residue as a preflight failure for a new materialization.
    static func classify(attempt: String, admission: Admission, attestation: StorageAttestation) throws -> MaterializationState {
        // Revalidate root/control/journals parent identities and strict mutable metadata before
        // opening an attempt. The classifier never accepts a free-standing journals descriptor.
        try attestation.revalidateMutableRegistries()
        return try classify(attempt: attempt, admission: admission, journals: attestation.journals, validatePrefix: validateProductionPrefix,
                     validateDirectory: validateProductionMutableDirectory)
    }

    private static func classify(attempt: String, admission: Admission, journals: HeldDescriptor,
                                 validatePrefix: (HeldDescriptor, Set<String>, Admission) throws -> Void,
                                 validateDirectory: (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor) throws -> MaterializationState {
        guard attempt == admission.attempt, AdmissionController.isAttemptUUID(attempt) else { throw StateFailure.illegalTransition }
        let opened = try POSIX.openAt(journals, name: attempt, flags: O_RDONLY | O_DIRECTORY)
        let journal = try validateDirectory(opened, journals, attempt)
        return try AdmissionRegistryLock.withExclusive(journal) {
            let names = try POSIX.directoryEntryNames(journal)
            let completePrefix: Set<String> = ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json"]
            let terminals: [String: MaterializationState] = [
                "0004-published.json": .published,
                "0004-quarantined.json": .quarantined,
                "0004-publish-indeterminate.json": .publishIndeterminate
            ]
            guard let terminalName = terminals.keys.first(where: { names.contains($0) }) else { throw StateFailure.illegalTransition }
            let expectedNames: Set<String>
            switch terminalName {
            case "0004-quarantined.json":
                // A PRE_* fault is allowed from each pre-rename state.  The terminal record
                // remains numbered 0004, but it must never fabricate unperformed transitions.
                let allowed: [Set<String>] = [
                    ["0001-stage-created.json", terminalName],
                    ["0001-stage-created.json", "0002-presealed.json", terminalName],
                    completePrefix.union([terminalName])
                ]
                guard let matched = allowed.first(where: { $0 == names }) else { throw StateFailure.illegalTransition }
                expectedNames = matched
            default:
                expectedNames = completePrefix.union([terminalName])
                guard names == expectedNames else { throw StateFailure.illegalTransition }
            }
            // Directory enumeration advances an open-file description. Rebind before the next
            // snapshot so the validator cannot accidentally observe an exhausted stream.
            let validationJournal = try validateDirectory(journal, journals, attempt)
            try validatePrefix(validationJournal, expectedNames, admission)
            let rebound = try validateDirectory(journal, journals, attempt)
            guard try POSIX.directoryEntryNames(rebound) == expectedNames else { throw StateFailure.illegalTransition }
            return terminals[terminalName]!
        }
    }

#if DEBUG
    /// Test-only descriptor factory. It shares every create/rebind/durability call site with
    /// production while allowing a disposable unprivileged tree to record privileged metadata
    /// steps instead of mutating a protected authority path.
    static func makeDisposableForTest(attestation: StorageAttestation, admission: Admission,
                                      createImmutableRecord: @escaping (HeldDescriptor, String, [UInt8]) throws -> HeldDescriptor,
                                      validatePrefix: @escaping (HeldDescriptor, Set<String>, Admission) throws -> Void,
                                      normalizeMutableDirectory: @escaping (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor,
                                      validateMutableDirectory: @escaping (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor) throws -> DarwinDurableStateStore {
        try DarwinDurableStateStore(attestation: attestation, admission: admission,
                                    revalidateRegistries: {}, createImmutableRecord: createImmutableRecord,
                                    validateExistingPrefix: validatePrefix,
                                    normalizeMutableDirectory: normalizeMutableDirectory,
                                    validateMutableDirectory: validateMutableDirectory)
    }

    static func classifyDisposableForTest(attempt: String, admission: Admission, journals: HeldDescriptor,
                                          validatePrefix: @escaping (HeldDescriptor, Set<String>, Admission) throws -> Void,
                                          validateDirectory: @escaping (HeldDescriptor, HeldDescriptor, String) throws -> HeldDescriptor) throws -> MaterializationState {
        try classify(attempt: attempt, admission: admission, journals: journals, validatePrefix: validatePrefix,
                     validateDirectory: validateDirectory)
    }
#endif

    private func expectedPrefix(before record: JournalRecordV2, currentState: MaterializationState) throws -> Set<String> {
        if record.phase == .quarantined {
            switch currentState {
            case .stageCreated: return ["0001-stage-created.json"]
            case .presealed: return ["0001-stage-created.json", "0002-presealed.json"]
            case .renameIntent: return ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json"]
            default: throw StateFailure.illegalTransition
            }
        }
        switch record.sequence {
        case 1: return []
        case 2: return ["0001-stage-created.json"]
        case 3: return ["0001-stage-created.json", "0002-presealed.json"]
        case 4: return ["0001-stage-created.json", "0002-presealed.json", "0003-rename-intent.json"]
        default: throw StateFailure.illegalTransition
        }
    }

    private func state(for phase: JournalPhase) -> MaterializationState {
        switch phase {
        case .stageCreated: return .stageCreated
        case .presealed: return .presealed
        case .renameIntent: return .renameIntent
        case .published: return .published
        case .quarantined: return .quarantined
        case .publishIndeterminate: return .publishIndeterminate
        }
    }
}
