import Foundation

enum AdmissionFailure: Error, Equatable { case residue, alreadyAdmitted, invalidAttempt, terminalizationFailed }

/// Descriptor capability for the one admission record.  Callers cannot substitute a pathname:
/// every mutation is relative to this retained parent/name/file identity triple.
struct HeldAdmissionRecord {
    let parent: HeldDescriptor
    let name: String
    let file: HeldDescriptor
}

struct Admission: Equatable {
    let attempt: String
    let record: HeldAdmissionRecord
    let recordSHA256: SHA256Digest
    static func == (lhs: Admission, rhs: Admission) -> Bool {
        lhs.attempt == rhs.attempt && lhs.record.file.device == rhs.record.file.device && lhs.record.file.inode == rhs.record.file.inode && lhs.recordSHA256 == rhs.recordSHA256
    }
}

enum AdmissionSeam: String, CaseIterable { case ownerDeath, admissionFsync, admissionRebind, beforeOpen, afterOpen }

protocol Storage: AnyObject {
    var storageVolumeUUID: String { get }
    func nextAttemptUUID() -> String
    /// The storage implementation must hold its interprocess registry transaction from the
    /// first empty snapshot through the exclusive admission creation.
    func preflightAndCreateExclusiveAdmission(attempt: String) throws -> HeldAdmissionRecord
    func writeAdmission(_ bytes: [UInt8], to record: HeldAdmissionRecord) throws
    func fsyncAdmission(_ record: HeldAdmissionRecord) throws
    func chmodAdmissionReadOnly(_ record: HeldAdmissionRecord) throws
    func immutableReadbackAdmission(_ record: HeldAdmissionRecord) throws
    func rebindAdmission(_ record: HeldAdmissionRecord) throws
    func fsyncAdmissionsParent(_ record: HeldAdmissionRecord) throws
    func markAdmitted() throws
    func quarantineAfterAdmissionFailure(_ record: HeldAdmissionRecord) throws
}

enum AdmissionController {
    static func admit(_ bundle: ValidatedBundle, storage: Storage) throws -> Admission {
        let attempt = storage.nextAttemptUUID()
        _ = try admissionName(attempt)
        var record: HeldAdmissionRecord?
        do {
            let opened = try storage.preflightAndCreateExclusiveAdmission(attempt: attempt)
            record = opened
            let bytes = try document(bundle: bundle, attempt: attempt, volumeUUID: storage.storageVolumeUUID)
            try storage.writeAdmission(bytes, to: opened)
            try storage.fsyncAdmission(opened)
            try storage.chmodAdmissionReadOnly(opened)
            try storage.immutableReadbackAdmission(opened)
            try storage.fsyncAdmission(opened)
            try storage.fsyncAdmissionsParent(opened)
            try storage.rebindAdmission(opened)
            let admission = Admission(attempt: attempt, record: opened, recordSHA256: try SHA256Digest(CanonicalJSON.sha256(bytes)))
            try storage.markAdmitted()
            return admission
        } catch AdmissionFailure.alreadyAdmitted { throw AdmissionFailure.alreadyAdmitted
        } catch {
            guard let record else { throw error }
            do { try storage.quarantineAfterAdmissionFailure(record) }
            catch { throw AdmissionFailure.terminalizationFailed }
            throw error
        }
    }

    static func admissionName(_ attempt: String) throws -> String {
        guard isAttemptUUID(attempt) else { throw AdmissionFailure.invalidAttempt }
        return attempt + ".json"
    }

    static func stageName(_ attempt: String) throws -> String {
        guard isAttemptUUID(attempt) else { throw AdmissionFailure.invalidAttempt }
        return attempt
    }

    static func isAttemptUUID(_ value: String) -> Bool {
        value.utf8.count == 32 && value.utf8.allSatisfy { byte in
            (48...57).contains(byte) || (97...102).contains(byte)
        }
    }

    static func document(bundle: ValidatedBundle, attempt: String, volumeUUID: String) throws -> [UInt8] {
        _ = try admissionName(attempt)
        let fields: [(String, JSONValue)] = [
            ("kind", .string("R4_PRIVILEGED_MATERIALIZATION_ADMISSION_V2")), ("namespace", .string(ProtectedExecutionConstants.namespace)),
            ("authority_id", .string(ProtectedExecutionConstants.authorityID)), ("attempt_uuid", .string(attempt)),
            ("task14_approval_sha256", .string(bundle.approval.sha256.value)), ("input_bundle_manifest_sha256", .string(bundle.manifest.sha256.value)),
            ("materialization_policy_sha256", .string(bundle.policy.sha256.value)), ("storage_volume_uuid", .string(volumeUUID))]
        let digest = CanonicalJSON.sha256(try CanonicalJSON.encode(.object(fields)))
        return try CanonicalJSON.encode(.object(fields + [("sha256", .string(digest))]))
    }
}
