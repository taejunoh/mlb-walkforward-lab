# Task 13 Python Gate Recovery Design

**Status:** Approved, repository-local TDD correction only.

**Goal:** Restore the Python execution registry's exact R4 reachability boundary while keeping all external-evidence and authority gates fail-closed.

## Decision

`mlb_backtest.execution_registry._BOUND_EXECUTION_CODE` must contain exactly the current bound closure plus these four reachable R4 modules:

```text
mlb_backtest/v52_r4_apfs.py
mlb_backtest/v52_r4_authority.py
mlb_backtest/v52_r4_offline_signer.py
mlb_backtest/v52_r4_production_public_key.py
```

The change is a repository-local TDD fix. It must not add `v52_r4_snapshot_issuer.py` (or any other issuer, snapshot, or custody producer) merely because it is nearby or imported by a separate production path. The registry list and its tests remain an exact closure: computed reachable Python imports, the explicit non-import Python extra, the schema set, and only the four approved R4 files above.

## Evidence and authority boundary

External evidence is never synthesized, copied from an untrusted checkout, fabricated, or committed by this recovery. The exact runner may receive an evidence root only when the user has authenticated and supplied that root. If no authenticated root is supplied, verification reports the missing input as blocked and performs no substitute provisioning. No test fixture may manufacture production custody evidence to make a gate pass.

Incomplete R4 authority remains fail-closed. This recovery does not weaken signature checks, key/public-key pinning, APFS custody checks, source-authority checks, immutable parent checks, or any policy that rejects incomplete authority. A missing R4 artifact is an error, not a reason to broaden selectors or accept a less authoritative path.

## Test contract

The exact registry tests are the contract:

- `test_task7d_runtime_authority_is_exactly_in_production_bound_code`
- `test_bound_execution_import_closure_is_computed_and_exact`
- `test_rejects_dirty_bound_execution_code_but_not_unrelated_files`
- `test_task7c_bound_runtime_rejects_dirty_initialize_and_load`
- `test_task7d_timezone_runtime_rejects_dirty_initialize_and_load`
- `test_rejects_committed_bound_code_head_drift_before_checkpoint`
- `test_rejects_dirty_bound_input_and_identity_race`

The first two prove exact membership and computed closure. The dirty-bound tests prove every bound file is protected at initialize/load time while unrelated files remain outside that boundary. The committed-head and identity-race tests preserve the Git and descriptor rebind protections.

## Forbidden scope

This change must not modify production snapshot issuance, add a snapshot issuer to the registry, synthesize or commit evidence, accept an unauthenticated evidence root, weaken R4 policy, alter protected parent inputs, or rewrite the approved Swift authority implementation. It is limited to the registry's four-file R4 membership and its tests/documentation.

The recovery must never access `/Users/taejunoh/Documents/MLB Custody/v5_1_bt_r4`, `/Users/taejunoh/Documents/MLB Custody/.v5_1_bt_r4.issuance.lock`, `/Users/taejunoh/Library/Application Support/mlb-walkforward-lab/custody-v2`, or `/Library/Application Support/mlb-walkforward-lab/protected-execution-authority`, and must never delete `/tmp/r4-readonly-tree-manifest.txt`. Any other evidence root requires explicit user authentication; without it, verification is blocked and no substitute evidence is provisioned.
