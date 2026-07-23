# MLB v5.2 Superseding-Run Downstream Continuation Amendment

**Status:** Non-runtime documentation amendment; no execution is authorized.

## Scope and authority

This amendment supersedes only the downstream-continuation interpretation for
the isolated `gamePk=565534` superseding source run. It is not runtime-approved
design authority and does not re-pin or reapprove the 2026-07-12 diagnostic
and 500-pick design. That design remains byte-identical to its approved
runtime blob.

The superseding source authority is closed and namespace-parametric. Its exact
disposition authority is the contract-bound derived disposition, rather than a
replacement raw artifact. This amendment does not change old-run selection
behavior, evidence, defaults, or receipt semantics.

The exact superseding source authority binds
`historical_source_acquisition_superseding_565534.json` to its authenticated
exact disposition sidecar authority. This amendment does not authorize
downstream execution.

## Downstream gate

For a later continuation, only a `SOURCES_READY` source inventory bound to the
superseding run may be considered, and the old run remains blocked. Any later
consumer must propagate the exact neutralization projection authenticated by
the superseding authority. It may not silently select a "latest" registry,
inventory, review, replay, or source root; every input must identify the exact
superseding authority.

No Plan B/C artifact is created. No `SOURCES_READY` inventory is asserted to
exist. Fit, OOF, cards, targets, outcomes, and 500-pick work require a
separately approved plan that explicitly selects the superseding run.
