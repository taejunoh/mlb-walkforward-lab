# Task 1 RED/GREEN/self-review report

## RED

Added the policy schema-version and fixed-six required-blob regression cases,
then ran the required Python suite before implementation. The policy case
failed because the schema still accepted only the original six identifiers;
the new locator/journal schemas were absent.

## GREEN

- Added closed Draft 2020-12 admission-locator and lifecycle-journal schemas,
  including exact kinds, UUID/path/hash constraints, phase branches, failure
  classes, and bounded integer device/inode fields.
- Added V2 key-rotation and terminal-revocation branches while preserving all
  existing V1 keyring branches; tightened receipt key-generation bounds.
- Expanded policy schema/config to the exact eight identifiers and regenerated
  the canonical policy self-digest (`6c933c08520aca1c53ecc30b493aeb35ac411c2a8c4c4e3eb27513037b0a4452`).
- Preserved the exact six required blobs and left the reviewed Swift source
  tuple and transport fixtures unchanged for Task 8’s atomic update.
- Added locator/journal and V2 keyring Python vectors.

Verification:

`python3 -m unittest tests.test_protocols tests.test_build_r4_protected_candidate -q` — 171 passed.

`ruff check scripts tests` — all checks passed.

`swift test --package-path tools/r4_protected_execution_authority --filter PythonTransportIntegrationTests` — passed.

`git diff --check` — passed.

## Self-review

- All new schema objects reject unknown fields and require every normative
  field; booleans cannot satisfy integer constraints under Draft 2020-12.
- `published_device` and `published_inode` are integer-only and bounded to
  `[0, 9223372036854775807]`; journal phase/sequence branches are closed.
- The policy digest was computed from the canonical object excluding its
  `sha256` member, then written back; no placeholder cascade values remain.
- The Task 1 source tuple is unchanged and has no `Publication.swift`; Task 8
  must insert the real file after `ProcessRunner.swift` and before
  `SignalMonitor.swift`, atomically with its transport fixture update. Required
  blobs are unchanged and remain the exact six paths.

The initial implementation briefly inserted a temporary `Publication.swift`
fixture solely for local checks; that historical setup was removed and is not
part of the Task 1 state or commit.

## Fix round 1 evidence (follow-up commit)

RED: after changing the Swift fixture policy to the ordered eight identifiers,
`swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests`
failed at the existing six-identifier decoder guard. Python V1 successor
vectors likewise became invalid against the V2-only keyring schema.

GREEN: `MaterializationPolicy.decode` and all Swift policy vectors now require
the exact ordered eight identifiers. Keyring schemas and Swift `KeyringRecord`
vectors preserve only the frozen V1 genesis and use exact V2 rotation and
terminal-revocation shapes; V1 successor documents and missing/unknown,
type/constant/null negatives reject. The required-blob regression now asserts
the exact six-path tuple. Task 1 leaves the reviewed source tuple and Python
transport fixture unchanged; Task 8 owns their atomic `Publication.swift`
addition.

Operational Keyring.swift chain/revocation binding and receipt-binding findings
are explicitly deferred to planned Tasks 2 and 5. Locator/journal cross-field
equality and transition validation are deferred to Task 2; the Task 1 schemas
remain closed shapes only.
