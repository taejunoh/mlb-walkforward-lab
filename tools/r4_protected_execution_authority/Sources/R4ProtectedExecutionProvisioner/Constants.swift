import Foundation

/// Compile-time Key-A pin.  This names only the externally held approval
/// verification public key; it is deliberately distinct from Key-B keyring
/// material generated during bootstrap.
public enum ProvisionerConstants {
    public static let task14ApprovalVerificationPublicKeySHA256 = "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1"
}

public enum ProtectedExecutionConstants {
    public static let namespace = "org.mlb-walkforward.r4.protected-execution-authority/v1"
    public static let authorityID = "r4-disjoint-custody-v2-execution-v1"
    public static let productionRoot = "/Library/Application Support/mlb-walkforward-lab/protected-execution-authority"
    public static let productionParent = "/Library/Application Support/mlb-walkforward-lab"
    public static let productionRootName = "protected-execution-authority"
}
