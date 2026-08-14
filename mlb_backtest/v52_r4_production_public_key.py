"""Pinned public verifier for the R4 custody signing authority."""

from hashlib import sha256 as _sha256

PRODUCTION_PUBLIC_KEY = bytes.fromhex("e3a7d632ff44b8fa8e1adcb2ea59156c04585e4f38517fc17888e202c50a9e47")
PRODUCTION_KEY_ID = "ed25519-sha256:" + _sha256(PRODUCTION_PUBLIC_KEY).hexdigest()
if PRODUCTION_KEY_ID != "ed25519-sha256:55dcd545df148b420b79d5d253f87ea4f60920e991cf6f99915b470eb80000a7":
    raise RuntimeError("R4 production public key mismatch")
