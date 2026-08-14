#!/opt/homebrew/bin/python3.11
"""Independent, literal-only generator for the test target's V2 vectors."""
import argparse
import hashlib
import json
import os
import stat


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def digest(domain, value):
    return hashlib.sha256(domain.encode("utf-8") + canonical(value)).hexdigest()


def sealed(fields):
    fields = dict(fields)
    fields["sha256"] = hashlib.sha256(canonical(fields)).hexdigest()
    return canonical(fields)


def record(kind, preimage, fields):
    return {"kind": kind, "preimage_hex": preimage.hex(), "record_hex": sealed(fields).hex()}


def records():
    h = lambda char: char * 64
    namespace = "org.mlb-walkforward.r4.protected-execution-authority/v1"
    authority = "r4-disjoint-custody-v2-execution-v1"
    attempt = "0123456789abcdef0123456789abcdef"
    genesis = {"generation": 1, "issued_at": "bootstrap-generation-1", "key_id": h("b"), "kind": "R4_PROTECTED_EXECUTION_KEYRING_V1", "previous_record_sha256": None, "public_key_sha256": h("b")}
    output = [record("frozen_genesis", canonical(genesis), {**genesis, "detached_signature_sha256": h("a")})]
    rotation = {"authority_id": authority, "generation": 2, "issued_at": "2026-08-12T00:00:00Z", "kind": "R4_PROTECTED_EXECUTION_KEY_ROTATION_V2", "namespace": namespace, "new_public_key_sha256": h("c"), "previous_generation": 1, "previous_public_key_sha256": h("b"), "previous_record_sha256": h("d")}
    output.append(record("rotation", b"R4KEYROT2\0" + canonical(rotation), {**rotation, "detached_signature_sha256": h("a")}))
    revocation = {"authority_id": authority, "kind": "R4_PROTECTED_EXECUTION_REVOCATION_V2", "namespace": namespace, "previous_revocation_sha256": None, "reason": "KEY_COMPROMISE", "revoked_at": "2026-08-12T00:00:00Z", "sequence": 1, "signer_generation": 2, "signer_key_record_sha256": h("d"), "signer_public_key_sha256": h("c"), "state": "REVOKED", "target_generation": 2, "target_key_record_sha256": h("d"), "target_public_key_sha256": h("c")}
    output.append(record("revocation", b"R4KEYREVOKE2\0" + canonical(revocation), {**revocation, "detached_signature_sha256": h("a")}))
    locator = {"admission_record_sha256": h("a"), "attempt_uuid": attempt, "authority_id": authority, "kind": "R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2", "namespace": namespace, "registry_relative_path": f"control/admissions/{attempt}.json"}
    output.append(record("locator", b"R4ADMISSIONLOCATOR2\0" + canonical(locator), {**locator, "locator_sha256": digest("R4ADMISSIONLOCATOR2\0", locator)}))
    for phase, sequence, extra in [("STAGE_CREATED", 1, {"stage_name": attempt}), ("PRESEALED", 2, {"published_closure_sha256": h("b"), "receipt_file_sha256": h("c")}), ("RENAME_INTENT", 3, {"published_closure_sha256": h("b"), "receipt_file_sha256": h("c")}), ("PUBLISHED", 4, {"published_closure_sha256": h("b"), "receipt_file_sha256": h("c"), "published_device": 1, "published_inode": 2}), ("QUARANTINED", 4, {"failure_class": "PRE_RENAME_SEAL", "quarantine_name": attempt}), ("PUBLISH_INDETERMINATE", 4, {"failure_class": "POST_RENAME_SEAL", "published_closure_sha256": h("b"), "receipt_file_sha256": h("c")})]:
        core = {"admission_record_sha256": h("a"), "attempt_uuid": attempt, "authority_id": authority, "kind": "R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2", "namespace": namespace, "phase": phase, "sequence": sequence}
        output.append(record("journal_" + phase.lower(), b"R4JOURNAL2\0" + canonical(core), {**core, **extra, "journal_sha256": digest("R4JOURNAL2\0", core)}))
    return output


def rows():
    profile = "(version 1)\n(deny default)"
    command = {"argv":["/usr/bin/sandbox-exec","-p",profile,"/stage/runtime/closure/python311/bin/python3.11","-I"],"cwd":"runtime","environment":["LANG=C"],"interpreter_codesign_identity_sha256":"b" * 64,"interpreter_mach_o_sha256":"c" * 64,"interpreter_sha256":"d" * 64,"max_output_bytes":1,"program":"/usr/bin/sandbox-exec","sandbox_launcher_codesign_identity_sha256":"e" * 64,"sandbox_launcher_descriptor_identity_sha256":"f" * 64,"sandbox_launcher_sha256":"a" * 64,"sandbox_profile_sha256":"0" * 64,"stdin":"/dev/null","stdout_stderr":"ROOT_OWNED_CAPPED_PIPES","timeout_seconds":1}
    return {
        "R4ADMISSIONLOCATOR2\0": {"admission_record_sha256":"a" * 64,"attempt_uuid":"0123456789abcdef0123456789abcdef","authority_id":"r4-disjoint-custody-v2-execution-v1","kind":"R4_PRIVILEGED_MATERIALIZATION_ADMISSION_LOCATOR_V2","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","registry_relative_path":"control/admissions/0123456789abcdef0123456789abcdef.json"},
        "R4JOURNAL2\0": {"admission_record_sha256":"a" * 64,"attempt_uuid":"0123456789abcdef0123456789abcdef","authority_id":"r4-disjoint-custody-v2-execution-v1","kind":"R4_PRIVILEGED_MATERIALIZATION_JOURNAL_V2","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","phase":"PUBLISHED","sequence":4},
        "R4KEYREVOKE2\0": {"authority_id":"r4-disjoint-custody-v2-execution-v1","kind":"R4_PROTECTED_EXECUTION_REVOCATION_V2","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","previous_revocation_sha256":None,"reason":"KEY_COMPROMISE","revoked_at":"2026-08-12T00:00:00Z","sequence":1,"signer_generation":2,"signer_key_record_sha256":"d" * 64,"signer_public_key_sha256":"c" * 64,"state":"REVOKED","target_generation":2,"target_key_record_sha256":"d" * 64,"target_public_key_sha256":"c" * 64},
        "R4KEYROT2\0": {"authority_id":"r4-disjoint-custody-v2-execution-v1","generation":2,"issued_at":"2026-08-12T00:00:00Z","kind":"R4_PROTECTED_EXECUTION_KEY_ROTATION_V2","namespace":"org.mlb-walkforward.r4.protected-execution-authority/v1","new_public_key_sha256":"c" * 64,"previous_generation":1,"previous_public_key_sha256":"b" * 64,"previous_record_sha256":"d" * 64},
        "R4V2_CERTIFICATE_CHAIN\0": ["a" * 64],
        "R4V2_CHILD_COMMAND_TEMPLATE\0": {"cwd":"runtime","environment":["LANG=C"],"import_argv_template":["/usr/bin/sandbox-exec","-p",profile,"__R4_STAGE__/runtime/closure/python311/bin/python3.11","-I"],"launcher_path":"/usr/bin/sandbox-exec","max_output_bytes":1,"pip_argv_template":["/usr/bin/sandbox-exec","-p",profile,"__R4_STAGE__/runtime/closure/python311/bin/python3.11","-I"],"sandbox_profile_template_sha256":"1" * 64,"stdin":"/dev/null","stdout_stderr":"ROOT_OWNED_CAPPED_PIPES","timeout_seconds":1},
        "R4V2_CODESIGN_IDENTITY\0": {"certificate_chain_sha256":"2" * 64,"designated_requirement":"identifier x","identifier":"x","team_identifier":None},
        "R4V2_ENVIRONMENT\0": ["LANG=C"],
        "R4V2_IMPORT_COMMAND\0": command,
        "R4V2_INSTALL_COMMAND\0": command,
        "R4V2_LAUNCHER_DESCRIPTOR_IDENTITY\0": {"mode":493,"nlink":1,"path":"/usr/bin/sandbox-exec","size":1,"st_dev":1,"st_gen":1,"st_ino":1},
        "R4V2_MACHO_OBSERVATION\0": {"dependencies":[],"path":"runtime/closure/python311/bin/python3.11","slices":[]},
        "R4V2_PUBLISHED_CLOSURE\0": [],
        "R4V2_SANDBOX_PROFILE_ACTUAL\0": profile,
        "R4V2_SANDBOX_PROFILE_TEMPLATE\0": profile,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    output = parser.parse_args().output
    if os.path.basename(output) == "r4_v2_domain_vectors.json":
        raise SystemExit("refusing to overwrite committed fixture")
    info = os.stat(output, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit("output must be caller-created regular 0600 file")
    vectors = [{"domain": domain, "canonical_value": value, "expected_sha256": digest(domain, value)} for domain, value in sorted(rows().items())]
    signing = {"seed_hex":"000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f","public_key_hex":"03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8","signature_hex":"8eac612bd8c2902d86cb3858e720b6bea69fcd9dde9112545aa6ceea6f19ebf026e7c11f0cc52d0008942aea451e66e03b0f74378f0e16f79fe6063832955d04"}
    frozen_digest = "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c"
    frozen = {"generation":1,"issued_at":"bootstrap-generation-1","key_id":frozen_digest,"kind":"R4_PROTECTED_EXECUTION_KEYRING_V1","previous_record_sha256":None,"public_key_sha256":frozen_digest}
    signing["preimage_hex"] = canonical(frozen).hex()
    signing["record_hex"] = sealed({**frozen,"detached_signature_sha256":hashlib.sha256(bytes.fromhex(signing["signature_hex"])).hexdigest()}).hex()
    generated_records = records()
    payload = canonical({"frozen_genesis_signing":signing,"kind":"R4_V2_DOMAIN_VECTORS_V1","records":generated_records,"schema_version":2,"vectors":vectors})
    with open(output, "wb", closefd=True) as stream:
        stream.write(payload)


if __name__ == "__main__":
    main()
