# R4 V2 Keyring and Publication Protocol Design

**Status:** approved design specification

## Purpose and scope

This specification closes the missing Key-B rotation, revocation, publication,
and recovery protocol required by Task 9 and subsequent Tasks 10--14.  It is
normative for the installed privileged helper and its schemas, models, fixtures,
and tests.  It does not authorize production bootstrap, materialization,
rotation, revocation, capture, or custody work.

This amendment implements read-only rotation/revocation verification and
DEBUG/test-only deterministic fixture construction.  It deliberately creates
no production CLI command, public callable mutation API, or administrative
rotation/revocation authority.  Any real Key-B rotation or revocation requires
a separately authorized future administrative protocol.

The authority root, namespace, and authority ID are fixed constants:

```text
/Library/Application Support/mlb-walkforward-lab/protected-execution-authority
org.mlb-walkforward.r4.protected-execution-authority/v1
r4-disjoint-custody-v2-execution-v1
```

All paths below are relative to a retained, root-owned descriptor for that root
unless stated otherwise.  A verifier must not trust a reconstructed root path,
cwd, environment value, caller input, or a path embedded in an evidence file.
Each component is opened relative to its retained parent with no-follow,
rebound to the expected parent/name, and checked for the expected identity,
type, owner, group, mode, nlink, ACL, and immutable state before use.

`canonical_json(x)` means the repository's canonical JSON encoder.  `SHA256(x)`
is the lowercase hexadecimal SHA-256 digest.  `D || x` denotes byte
concatenation.  Every `sha256` field below is
`SHA256(canonical_json(the same object without its sha256 field))`.

In the normative record templates below, `g` is a JSON integer generation,
`u` is the admission UUID matching `^[0-9a-f]{32}$`, `t` is a UTC instant in
the exact `YYYY-MM-DDTHH:MM:SSZ` RFC3339 form, `P_g` is the raw 32-byte public
key for generation g, `H_g = SHA256(P_g)`, and `R_g` is the ordinary `sha256`
of generation g's `record.json`.  These are variables in an exact key-set
template, not optional fields or caller-selected names.

Timestamp fields validate only the exact syntax stated here.  They do not
participate in ordering, freshness, predecessor selection, or chronology
inference.  `receipt_file_sha256` means SHA256 of the exact complete canonical
`receipt.json` byte sequence, including its embedded ordinary `sha256` field;
it is distinct from that embedded field's value.

## Object rules

Every regular file is root:wheel, nlink 1, ACL-free, has no unexpected extended
attributes, and is a regular non-symlink file.  Every directory is root:wheel,
ACL-free, non-symlink, and has only its specified children.  `immutable` means
successful `SF_IMMUTABLE` application followed by descriptor-relative readback.

Sealed material has these final attributes:

| Object | Mode | Immutable |
| --- | --- | --- |
| Mutable registry directory | `0700` | no |
| Key-generation directory | `0555` | yes |
| Key-B private key | `0600` | yes |
| Other sealed regular file | `0444` | yes |
| Sealed tree directory | `0555` | yes |

The root, `control`, `control/keyring`, `control/revocations`,
`control/admissions`, `control/journals`, `materialization-work`, `quarantine`,
and `authorities` remain root-only mutable registries at `0700`.  No code may
delete, overwrite, replace, adopt, or clean up a pre-existing child of any of
those registries.

## Key-B generations

The complete key registry layout is:

```text
control/keyring/                                      0700
  generation-1/                                       0555, immutable
    authority-receipt-ed25519-private-key             0600, immutable, 32 bytes
    authority-receipt-ed25519-public-key              0444, immutable, 32 bytes
    record.json                                       0444, immutable
    record.ed25519                                    0444, immutable, 64 bytes
  generation-N/                                       0555, immutable; N >= 2
    authority-receipt-ed25519-private-key             0600, immutable, 32 bytes
    authority-receipt-ed25519-public-key              0444, immutable, 32 bytes
    record.json                                       0444, immutable
    record.ed25519                                    0444, immutable, 64 bytes
```

`generation-1` is the frozen bootstrap spelling.  For N >= 2 the sole legal
name is `generation-` followed by the shortest base-10 representation of N;
leading zeros, signs, whitespace, case aliases, and any other child name are
invalid.  The verifier enumerates all children, rejects anything outside this
grammar, and requires exactly every generation from 1 through the highest
generation.  It must not infer the active generation from lexical sorting.

### Frozen generation 1

Generation 1 retains the current `R4_PROTECTED_EXECUTION_KEYRING_V1` bytes and
signature protocol exactly.  Its signature preimage is the canonical JSON
object with these fields:

```json
{
  "generation": 1,
  "issued_at": "bootstrap-generation-1",
  "key_id": "H_1",
  "kind": "R4_PROTECTED_EXECUTION_KEYRING_V1",
  "previous_record_sha256": null,
  "public_key_sha256": "H_1"
}
```

`record.ed25519` is Ed25519 signed by the generation-1 private key over that
canonical byte sequence, without a new domain prefix.  `record.json` adds
`detached_signature_sha256 = SHA256(record.ed25519)` and its `sha256` field.
The verifier requires that `key_id`, `public_key_sha256`, and
`SHA256(raw-public-key)` are identical, then verifies the self-signature.

### V2 rotations

Generation N >= 2 uses `R4_PROTECTED_EXECUTION_KEY_ROTATION_V2`.  Its exact
signed object is:

```json
{
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "generation": g,
  "issued_at": "t",
  "kind": "R4_PROTECTED_EXECUTION_KEY_ROTATION_V2",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "new_public_key_sha256": "H_g",
  "previous_generation": g - 1,
  "previous_public_key_sha256": "H_(g-1)",
  "previous_record_sha256": "R_(g-1)"
}
```

The displayed generation expressions are JSON integers, not strings.  The
signature preimage is
`b"R4KEYROT2\\0" || canonical_json(signed object)`.  The signer is the verified
Key-B-(N-1) private key.  `record.json` is the signed object plus
`detached_signature_sha256`, then its ordinary record `sha256`; the detached
signature is exactly 64 bytes in `record.ed25519`.

To accept generation N, verification first accepts N-1, checks every
predecessor field against N-1's raw public bytes and record bytes, checks the
new raw public key against `new_public_key_sha256`, checks the signature-file
digest and record digest, and verifies the predecessor signature preimage.
The active key is the highest accepted contiguous generation.  Receipt signing
can use only that active generation's private key.

A rotation creates only the next exact directory directly under the retained
keyring descriptor using exclusive `mkdir`: it begins `0700`, receives all four
children by exclusive creation and per-file fsync/rebind, is fully verified,
then becomes `0555` and immutable before the keyring parent is fsynced.  The
new private key exists only in a zeroized in-memory buffer until its exclusive
`0600` write.  Before any receipt signature, the selected private key must
derive exactly the selected verified raw public key; both private and public
descriptors must then rebind to their expected generation directory names.  A
failure leaves the owned incomplete directory as forensic
residue; no rotation retry, replacement, deletion, or adoption is permitted.

## Terminal revocation

Revocation is a one-record terminal transition:

```text
control/revocations/                                  0700
  revocation-00000000000000000001/                    0555, immutable
    record.json                                       0444, immutable
    record.ed25519                                    0444, immutable, 64 bytes
```

The only active history is an empty `control/revocations`.  The only revoked
history is the exact child shown above.  Any other name, sequence, extra child,
missing file, or metadata mismatch is invalid.  After this child exists, the
authority is revoked: rotation, materialization, receipt issuance, and normal
active verification all fail closed.

The exact signed revocation object is:

```json
{
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "kind": "R4_PROTECTED_EXECUTION_REVOCATION_V2",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "previous_revocation_sha256": null,
  "reason": "one enum member from KEY_COMPROMISE, HELPER_COMPROMISE, MANIFEST_COMPROMISE, PLATFORM_ASSUMPTION_INVALID",
  "revoked_at": "t",
  "sequence": 1,
  "signer_generation": g,
  "signer_key_record_sha256": "R_g",
  "signer_public_key_sha256": "H_g",
  "state": "REVOKED",
  "target_generation": g,
  "target_key_record_sha256": "R_g",
  "target_public_key_sha256": "H_g"
}
```

The signature preimage is
`b"R4KEYREVOKE2\\0" || canonical_json(signed object)`.  It is signed by the
specified active Key-B private key.  `record.json` adds
`detached_signature_sha256` and its ordinary record `sha256`.  Verification
requires all signer and target fields to equal the active chain tip, verifies
the detached signature and both digests, and then rejects the authority because
valid revocation is terminal.  Key A is approval-only and must never sign a
receipt, rotation, or revocation.

## Published authority graph

The sole publication destination is:

```text
authorities/r4-disjoint-custody-v2-execution-v1/       0555, immutable
  checkout/                                            0555 tree, immutable
  runtime/                                             0555 tree, immutable
  runtime-manifest-v2.json                             0444, immutable
  manifest-v2.json                                     0444, immutable
  admission.json                                       0444, immutable
  admission-locator-v2.json                            0444, immutable
  receipt.json                                         0444, immutable
  receipt.ed25519                                      0444, immutable, 64 bytes
```

No other child is permitted.  `checkout` is the exact input-bundle checkout
closure.  `runtime` is the observed post-install runtime closure.  The runtime
manifest describes runtime rows; the authority manifest describes checkout rows.
The receipt binds both documents and the whole publishable graph.

`admission.json` is an exact byte copy of the immutable
`control/admissions/u.json` record.  Its locator has this exact
canonical object before its ordinary `sha256` field:

```json
{
  "admission_record_sha256": "A = SHA256(admission bytes)",
  "attempt_uuid": "u",
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "kind": "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "registry_relative_path": "control/admissions/u.json"
}
```

The locator is not signed: its authority comes from the sealed receipt closure
and its immutable, descriptor-verified admission copy.  The final locator is
exactly the displayed object plus
`locator_sha256 = SHA256(b"R4ADMISSIONLOCATOR2\\0" || canonical_json(displayed
object))` and then its ordinary `sha256` field.  The receiver must verify both
digests and require the two values to agree with their defined preimages.  It
then component-walks the fixed registry-relative path from the held root
descriptor, checks the external admission bytes equal the published copy, and
checks both equal the receipt's `admission_record_sha256`.

The published closure is
`SHA256(b"R4V2_PUBLISHED_CLOSURE\\0" || canonical_json(rows))`, where `rows` are
strict-path-ordered recursive `{path,type,mode,size,sha256}` rows for every
object below the authority directory except exactly `receipt.json` and
`receipt.ed25519`.  It includes directory rows, checkout, runtime, both
manifests, admission copy, and locator.  Directory row hashes use the existing
`R4DIR\\0` rule.  The authority root itself is not a row.  These two exclusions
are the only exclusions and avoid a receipt-to-closure hash cycle; the receipt
instead binds the closure before it is signed.

## Receipt and verification bindings

`ReceiptV2` must store and expose `key_generation` and `public_key_sha256`.
They are not parse-only fields.  Creation obtains them only from the selected
active verified Key-B generation.  The detached receipt signature is exactly
64 bytes and signs the full canonical `receipt.json` bytes with that key.  Its
existing `state: "SEALED"` means that all receipt-covered descendants have
passed preseal and immutable readback; it is not evidence that the authority
root rename and final journal have completed.  Only the terminal published
journal record proves publication.

Read-only active verification performs, in this order:

1. Verify the sealed bootstrap layout, compiled Key-A hash pin, approval bytes,
   and approval signature.
2. Verify the closed Key-B generation layout, frozen generation 1, every
   contiguous V2 rotation, and select the highest valid generation.
3. Parse receipt canonical bytes and require receipt generation/public digest
   to equal that active key; verify its detached signature with that exact raw
   public key.
4. Verify the terminal revocation registry; an accepted revocation rejects the
   authority.
5. Verify the exact authority child graph and metadata descriptor-relatively;
   recompute runtime, checkout, directory, and full published-closure rows.
6. Verify both admission representations and locator, then every existing V2
   cross-document binding: approval, bundle, lock, policy, wheel set, Git,
   helper, launcher, interpreter, environment, observation, runtime manifest,
   authority manifest, receipt, and admission record.
7. Require the terminal `published` journal record described below.

The verifier has no mutation capability and does not reinterpret malformed
records as a different protocol version.

## Append-only lifecycle journal

The lifecycle journal is outside the future immutable authority tree:

```text
control/journals/                                      0700
  u/                                                   0700
    0001-stage-created.json                            0444, immutable
    0002-presealed.json                                0444, immutable
    0003-rename-intent.json                            0444, immutable
    0004-(published|quarantined|publish-indeterminate).json
                                                         0444, immutable; exactly one
```

An attempt directory may contain exactly the required prefix and exactly one
terminal sequence-0004 file.  All fixed filename components are literal; `u` is the
admission's 32-lowercase-hex UUID.  Journal records are immutable audit records,
not a signing authority, and therefore have no detached signature.  Their
signature preimage is explicitly **none**.  Their final record is exactly
`core`, its phase-specific fields,
`journal_sha256 = SHA256(b"R4JOURNAL2\\0" || canonical_json(core))`, and then
its ordinary `sha256`; the latter hashes the complete record without `sha256`.

For every journal record, `core` has these fields:

```json
{
  "admission_record_sha256": "A",
  "attempt_uuid": "u",
  "authority_id": "r4-disjoint-custody-v2-execution-v1",
  "kind": "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2",
  "namespace": "org.mlb-walkforward.r4.protected-execution-authority/v1",
  "phase": "the literal phase named by its filename",
  "sequence": "the JSON integer named by its filename"
}
```

The final record adds phase-specific fields:

| Filename / phase | Required additional fields |
| --- | --- |
| `0001-stage-created` / `STAGE_CREATED` | `stage_name` equal to the attempt UUID |
| `0002-presealed` / `PRESEALED` | `published_closure_sha256`, `receipt_file_sha256` |
| `0003-rename-intent` / `RENAME_INTENT` | `published_closure_sha256`, `receipt_file_sha256` |
| `0004-published` / `PUBLISHED` | `published_closure_sha256`, `receipt_file_sha256`, `published_device`, `published_inode` |
| `0004-quarantined` / `QUARANTINED` | `failure_class`, `quarantine_name` equal to attempt UUID |
| `0004-publish-indeterminate` / `PUBLISH_INDETERMINATE` | `failure_class`, `published_closure_sha256`, `receipt_file_sha256` |

`failure_class` is exactly one of `PRE_RENAME_IO`, `PRE_RENAME_REBIND`,
`PRE_RENAME_SEAL`, `PRE_RENAME_DURABILITY`, `POST_RENAME_REBIND`,
`POST_RENAME_SEAL`, `POST_RENAME_DURABILITY`, or `POST_RENAME_JOURNAL`; it is
evidence only and cannot alter control flow.  All integer fields are JSON
integers.  `published_device` and `published_inode` are JSON integers in the
closed range 0 through `Int64.max`.  `QUARANTINED` accepts only a `PRE_*`
failure class; `PUBLISH_INDETERMINATE` accepts only a `POST_*` failure class;
`PUBLISHED` has no `failure_class` field.  `journal_sha256` is required in addition to
the core and phase-specific fields; it prevents a journal record from being
mistaken for an unrelated canonical JSON record.

The state machine is:

```text
ABSENT -> ADMITTED -> STAGE_CREATED -> PRESEALED -> RENAME_INTENT -> PUBLISHED
                         |                 |                  |
                         +-----------------+-------------------> QUARANTINED
                                                               -> PUBLISH_INDETERMINATE
```

`PUBLISHED`, `QUARANTINED`, and `PUBLISH_INDETERMINATE` are terminal.  Existing
mutable replacement `state.json` records are not part of this protocol.

## Transaction, failure, and restart rules

1. Create the admission as `u.json` with exclusive creation;
   fsync it, make it read-only and immutable, fsync its parent, and rebind it.
2. Create the stage only as `materialization-work/u` and create
   `control/journals/u/0001-stage-created.json` exclusively; fsync and rebind
   each object and its parent.
3. Exclusively create and fsync all publishable children, including immutable
   admission copy/locator, manifests, receipt, and detached signature.  Verify
   the complete prospective graph and all hashes from retained descriptors.
4. Preseal every descendant, verify immutable readback, then write
   `0002-presealed.json`.  The stage root remains mutable at this point solely
   to permit the exclusive publication rename.
5. Write, fsync, and rebind `0003-rename-intent.json`.
6. Perform exactly one `renameatx_np(RENAME_EXCL)` from the retained work
   descriptor and the fixed stage name to the retained `authorities` descriptor
   and the fixed authority ID.  No retry, replacement, alternate publication
   name, unlink, or cleanup is permitted.
7. Immediately rebind the pre-rename stage descriptor under the authorities
   parent and require unchanged device/inode.  Then seal the authority root,
   fsync the authority parent and volume barrier, and exclusively append
   `0004-published.json` with the rebound identity.

Before rename, a failure may terminalize only after exclusively moving the
retained, rebound stage to `quarantine/u`.  The move must be rebound by
device/inode and both parents and the volume must be durable before
`0004-quarantined.json` is written.  If any ownership, rebind, journal, move,
or durability proof fails, the helper leaves all residue untouched and returns
failure.

After the rename syscall is entered, any error before the final durable
`0004-published.json` is `PUBLISH_INDETERMINATE`.  The helper must not attempt
quarantine, a second rename, adoption, repair, overwrite, deletion, or retry.
It may append the fixed indeterminate journal record only if that append itself
is safely proven; inability to append leaves untouched residue.  **The human
explicitly approved permanent fail-closed behavior: a potentially renamed
authority without proven final published-journal durability is never
automatically adopted.**

On startup or a later materialization request, any authority, work, quarantine,
admission, or journal residue is a terminal preflight failure.  A later normal
operation cannot resume an attempt.  Active verification requires both a fully
valid sealed authority graph and a valid durable `0004-published.json`; a
plausible graph without that record remains unavailable.

## Migration and implementation impact

Generation 1 is retained byte-for-byte.  No deployed bootstrap root is rewritten
to change its record kind or its signature preimage.  Schemas accept frozen
`R4_PROTECTED_EXECUTION_KEYRING_V1` only at generation 1 and V2 rotation/
revocation records only at their specified locations.  New deployments create
the additional empty `control/journals` registry during bootstrap.

The materialization policy's closed `schema_versions` array gains exactly
`R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2` and
`R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2`.  This is an intentional policy
byte change: bundle, approval, bootstrap policy, runtime, authority, receipt,
and their dependent hash vectors must be regenerated and cross-validated.  The
existing six `CANONICAL_REQUIRED_BLOB_PATHS` remain exactly unchanged; this
design document is not a required blob and must not be added to that list.

Required implementation amendments are limited to this protocol surface:

- `schemas/r4_protected_execution_keyring.schema.json`: replace under-specified
  V1 successor branches with the frozen-genesis/V2-rotation/V2-revocation union.
- Add closed schemas for `R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2`
  and `R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2`; update receipt schema to make
  key generation/public digest operational bindings.
- `Bootstrap.swift` and `Adapters.swift`: distinguish the exact bootstrap-time
  layout from the later extensible-but-grammar-closed registry layout; add
  `control/journals`.
- `Models.swift` and `Keyring.swift`: retain receipt signer fields, decode and
  verify successor records/signatures, selected active key, locator, journal,
  and complete published closure.
- `Materialize.swift`, `StateMachine.swift`, `Storage.swift`, and `Verify`:
  implement the descriptor-only graph, append-only lifecycle, exclusive rename,
  final rebind/durability, residue rejection, and read-only verification.

Commit `727e198` remains correct only for its narrow checkout equality fix.  It
must not be treated as a complete publication closure: current
`PublishedAuthorityTree` and `TreeEvidence.publishedTree` describe checkout
only.  Commit `ca5bda5` must be amended for lifecycle ordering, graph contents,
and key protocol rather than patched with an implicit local interpretation.

## Threat model and acceptance matrix

The protocol defends against hostile caller paths and names, symlink and
replacement races, stale/partial artifacts, extra registry children,
cross-role Key-A/Key-B use, malformed canonical JSON, signature substitution,
closure substitution, post-admission failure, and ambiguous rename completion.
It does not claim availability after a crash or an unprovable durability result;
that loss is intentional fail-closed behavior.

Deterministic fixture-root TDD must cover at least the following matrix:

| Area | Acceptance cases |
| --- | --- |
| Genesis | exact frozen bytes/vector; wrong raw public key, record digest, signature digest, or signature fails |
| Rotation | N=2 and N=3 valid chains; every predecessor field, gap, extra child, alias, metadata mismatch, or wrong signer fails |
| Active signer | receipt succeeds only with highest contiguous generation; receipt generation/public digest mismatch fails |
| Revocation | empty registry is active; only exact sequence-1 record validates and then rejects authority; all alternatives fail |
| Authority graph | each required child is present; each extra/missing/symlink/wrong mode/ACL/immutable object fails; closure includes all required payload rows and excludes exactly receipt pair |
| Admission | copy, locator, external registry record, and receipt digest must all match; absolute or foreign locator paths fail |
| Journal | exact sequence/phase/field matrix; no replacement; invalid or absent published terminal record fails verification |
| Lifecycle | failures before rename quarantine only retained stage; failures after rename become indeterminate; no unlink/retry/adopt mutation occurs |
| Restart | every residue class rejects later materialization; only valid graph plus durable published journal is verifiable |
| Cross-document | independently recomputed V2 hashes and all approval/bundle/policy/runtime/receipt bindings reject one-field drift |
