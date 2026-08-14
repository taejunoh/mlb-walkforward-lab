# Task 2 report

## RED

Initial focused RED was:

```text
swift test --package-path tools/r4_protected_execution_authority --filter ModelsV2Tests
error: KeyringRecord has no rotationSignaturePreimage/previousGeneration;
ReceiptV2 has no keyGeneration/publicKeySHA256; locator and journal models are absent.
```

The historical terminal transcript for the original retained-signer RED is not
available verbatim. The round-1 frozen-genesis RED was reproduced by adding the
deterministic Bootstrap-byte test before `frozenGenesisRecordBytes` existed;
the test target failed to compile with `KeyringRecord has no member
frozenGenesisRecordBytes`.

## GREEN and vectors

Implemented closed frozen/V2 key records, receipt signer retention, locator and
journal models, exact domain preimages, and canonical full-record reconstruction.
The Python literal-only generator now emits preimage and full-record hex bytes
for frozen genesis, rotation, revocation, locator, and every journal phase.

```text
python3 .../generate_r4_v2_domain_vectors.py --output "$tmp"
cmp "$tmp" .../r4_v2_domain_vectors.json
```

The regenerated fixture compared byte-for-byte equal. Swift decodes each
independent byte vector and compares reconstructed preimage and full record
bytes before the vector digest assertions.

Focused GREEN: `ModelsV2Tests` (28 passed), `BootstrapTests` (27 passed), and
Python protocol schemas (142 passed).

## Mutation review

The deterministic frozen-genesis test changes `issued_at`, `key_id`, and
`public_key_sha256`; model decoding and Bootstrap signature verification reject
each mutation. The ordinary self-digest and detached-signature checks reject
the remaining record-byte drift during full Bootstrap verification.

## Schema handoff

The schema now fixes frozen `issued_at` to `bootstrap-generation-1`. Standard
JSON Schema cannot express equality between two arbitrary instance properties
(`key_id == public_key_sha256`) or the rotation arithmetic relation; the closed
Swift semantic decoder is mandatory after schema validation and enforces both.
No custom Python semantic validator was introduced in Task 2.

## Commits and cleanup

Initial implementation: `e597fa27646fb9e5e4b3634b0adf03775fe8f87a`.
Round-1 follow-up commit is recorded after final verification. Package-local
`.build` is removed after test execution; no generic clean was used.

## Round 2

The literal Ed25519 vector was bootstrapped once with Python `cryptography`
from seed `00..1f`; its seed, raw public key, and signature are now frozen
literals. The generator performs only local canonical JSON/SHA-256 work and
does not spawn Swift or a signing process. RED mutation evidence flips one bit
of both the signature and preimage; CryptoKit verification returns false.
`FixtureKeyring.keyID` now equals its public-key digest so KeyringTests reaches
the intended role-confusion and gap assertions.

## Round 3

RED was reproduced by changing the frozen seed's first byte from `00` to `01`;
the focused Swift test rejected the resulting seed-derived public key and
signature. On restoration, a fresh CryptoKit `signature(for:)` call produced a
different but valid 64-byte signature for the same seed/preimage (the platform
uses randomized signing), so byte equality of a newly produced signature is
not a valid deterministic assertion. The final test instead proves exact
seed-to-public bytes, verifies the frozen independent signature with that
derived public key, verifies a fresh signature, and rejects one-bit signature
and preimage mutations.
