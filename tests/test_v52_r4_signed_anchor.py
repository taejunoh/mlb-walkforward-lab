"""Tests for the closed, detached-signature R4 custody authority anchor."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class SignedAuthorityAnchorRedTests(unittest.TestCase):
    def test_anchor_api_is_available(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import (  # noqa: PLC0415
            build_authority_anchor,
            render_authority_anchor,
            verify_authority_anchor,
        )

        self.assertTrue(callable(build_authority_anchor))
        self.assertTrue(callable(render_authority_anchor))
        self.assertTrue(callable(verify_authority_anchor))


class SignedAuthorityAnchorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from tests.r4_issued_authority_fixture import (
            _RAW,
            _INVENTORY,
            _fixture_source_authority_profile,
            _write_source,
        )

        cls._temporary = tempfile.TemporaryDirectory()
        source_root = Path(cls._temporary.name)
        _write_source(source_root)
        cls._profile = _fixture_source_authority_profile(
            source_root / Path(*_RAW.parts), source_root / Path(*_INVENTORY.parts)
        )
        cls._raw_root = source_root / Path(*_RAW.parts)
        cls._source_inventory = source_root / Path(*_INVENTORY.parts)
        from tests import r4_issued_authority_fixture as fixture
        from mlb_backtest import v52_r4_authority as authority

        cls._authority_module = authority
        cls._resolver = patch.object(
            authority,
            "_resolve_source_authority",
            side_effect=lambda *args, **kwargs: (
                cls._profile
                if not args
                else fixture._IssuanceAuthority(
                    cls._profile, fixture._FIXTURE_ANCHOR_PRIVATE_KEY.sign
                )
            ),
        )
        cls._resolver.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._resolver.stop()
        cls._temporary.cleanup()

    def _authority(self):
        return self._authority_module._resolve_source_authority()

    def _issuance_authority(self):
        return self._authority_module._resolve_source_authority(
            self._raw_root, self._source_inventory
        )

    def _document(self) -> dict[str, object]:
        from mlb_backtest.v52_r4_signed_anchor import build_authority_anchor

        inventory = {"custody_identity_manifest_sha256": "1" * 64}
        receipt = {
            "comparison": {
                "source_content_manifest_sha256": "2" * 64,
                "snapshot_content_manifest_sha256": "2" * 64,
                "binding_comparison": {
                    "source_logical_binding_manifest_sha256": "3" * 64,
                    "snapshot_logical_binding_manifest_sha256": "3" * 64,
                },
            }
        }
        return dict(
            build_authority_anchor(
                key_id=self._authority().anchor_key_id,
                inventory=inventory,
                receipt=receipt,
                inventory_bytes=b'{"inventory":true}\n',
                receipt_bytes=b'{"receipt":true}\n',
                contract_bytes=b'{"contract":true}\n',
                run_spec_bytes=b'{"run_spec":true}\n',
                amendment_design_bytes=b"design\n",
                amendment_design_git_blob_id="a" * 40,
                amendment_plan_bytes=b"plan\n",
                amendment_plan_git_blob_id="b" * 40,
            )
        )

    def test_built_anchor_is_immediately_renderable(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import render_authority_anchor

        self.assertTrue(render_authority_anchor(self._document()).endswith(b"\n"))

    def _signed(self, document: dict[str, object]) -> tuple[bytes, bytes]:
        from mlb_backtest.v52_r4_signed_anchor import render_authority_anchor

        payload = render_authority_anchor(document)
        authority = self._issuance_authority()
        return payload, authority.sign_anchor_message(b"MLB-R4-CUSTODY-ANCHOR-v1\0" + payload)

    def test_canonical_newline_document_round_trips_only_after_signature(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        document = self._document()
        payload, signature = self._signed(document)
        self.assertEqual(payload, json.dumps(
            document, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8") + b"\n")
        self.assertEqual(
            verify_authority_anchor(
                payload,
                signature,
                public_key=self._authority().anchor_public_key,
                expected_key_id=self._authority().anchor_key_id,
            ),
            document,
        )

    def test_signature_failures_happen_before_anchor_parsing(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        payload, signature = self._signed(self._document())
        for bad_signature in (b"", signature[:-1], signature + b"x"):
            with self.subTest(length=len(bad_signature)):
                with self.assertRaises(InvalidSignature):
                    verify_authority_anchor(
                        b"not json", bad_signature, public_key=self._authority().anchor_public_key,
                        expected_key_id=self._authority().anchor_key_id,
                    )
        authority = self._authority()
        wrong_public = Ed25519PrivateKey.from_private_bytes(b"x" * 32).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        wrong_key_id = "ed25519-sha256:" + hashlib.sha256(wrong_public).hexdigest()
        with self.assertRaises(InvalidSignature):
            verify_authority_anchor(payload, signature, public_key=wrong_public, expected_key_id=wrong_key_id)
        with self.assertRaises(ValueError):
            verify_authority_anchor(
                payload, signature, public_key=authority.anchor_public_key,
                expected_key_id="ed25519-sha256:" + "0" * 64,
            )

    def test_public_key_must_derive_the_expected_key_id_before_signature_acceptance(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        payload = self._signed(self._document())[0]
        attacker = Ed25519PrivateKey.from_private_bytes(b"x" * 32)
        attacker_public = attacker.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        signature = attacker.sign(b"MLB-R4-CUSTODY-ANCHOR-v1\0" + payload)
        with self.assertRaises(ValueError):
            verify_authority_anchor(
                payload,
                signature,
                public_key=attacker_public,
                expected_key_id=self._authority().anchor_key_id,
            )

    def test_noncanonical_and_all_closed_object_extensions_fail(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        document = self._document()
        payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        signature = self._issuance_authority().sign_anchor_message(b"MLB-R4-CUSTODY-ANCHOR-v1\0" + payload)
        with self.assertRaises(ValueError):
            verify_authority_anchor(
                payload, signature, public_key=self._authority().anchor_public_key,
                expected_key_id=self._authority().anchor_key_id,
            )

        mutations = (
            lambda value: value.update(extra=True),
            lambda value: value["source_approval"].update(extra=True),
            lambda value: value["artifacts"].update(extra=True),
            lambda value: value["artifacts"]["inventory"].update(extra=True),
            lambda value: value["artifacts"]["receipt"].update(extra=True),
            lambda value: value["artifacts"]["contract"].update(extra=True),
            lambda value: value["artifacts"]["run_spec"].update(extra=True),
            lambda value: value["amendment"].update(extra=True),
            lambda value: value["amendment"]["design"].update(extra=True),
            lambda value: value["amendment"]["implementation_plan"].update(extra=True),
            lambda value: value["receipt_comparison"].update(extra=True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(document)
                mutate(changed)
                changed_payload, changed_signature = self._signed(changed)
                with self.assertRaises(ValueError):
                    verify_authority_anchor(
                        changed_payload, changed_signature, public_key=self._authority().anchor_public_key,
                        expected_key_id=self._authority().anchor_key_id,
                    )

    def test_every_fixed_path_hash_digest_and_source_approval_mutation_fails(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        document = self._document()
        mutations = []
        for artifact in ("inventory", "receipt", "contract", "run_spec"):
            mutations.extend((
                lambda value, artifact=artifact: value["artifacts"][artifact].update(path="wrong/path"),
                lambda value, artifact=artifact: value["artifacts"][artifact].update(sha256="0" * 64),
            ))
        for artifact in ("design", "implementation_plan"):
            mutations.extend((
                lambda value, artifact=artifact: value["amendment"][artifact].update(path="wrong/path"),
                lambda value, artifact=artifact: value["amendment"][artifact].update(sha256="0" * 64),
                lambda value, artifact=artifact: value["amendment"][artifact].update(git_blob_id="0" * 40),
            ))
        mutations.extend((
            lambda value: value["source_approval"].update(logical_raw_root="wrong"),
            lambda value: value["source_approval"].update(protected_inventory_path="wrong"),
            lambda value: value["source_approval"].update(protected_inventory_sha256="0" * 64),
            lambda value: value.update(custody_identity_manifest_sha256="0" * 64),
            lambda value: value["receipt_comparison"].update(source_content_manifest_sha256="0" * 64),
            lambda value: value["receipt_comparison"].update(snapshot_content_manifest_sha256="0" * 64),
            lambda value: value["receipt_comparison"].update(source_logical_binding_manifest_sha256="0" * 64),
            lambda value: value["receipt_comparison"].update(snapshot_logical_binding_manifest_sha256="0" * 64),
        ))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(document)
                mutate(changed)
                payload, signature = self._signed(document)
                changed_payload = json.dumps(
                    changed, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
                ).encode("utf-8") + b"\n"
                with self.assertRaises(InvalidSignature):
                    verify_authority_anchor(
                        changed_payload, signature, public_key=self._authority().anchor_public_key,
                        expected_key_id=self._authority().anchor_key_id,
                    )

    def test_boolean_is_never_an_integer_or_digest_substitute(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        document = self._document()
        document["custody_identity_manifest_sha256"] = True
        payload, signature = self._signed(document)
        with self.assertRaises(ValueError):
            verify_authority_anchor(
                payload, signature, public_key=self._authority().anchor_public_key,
                expected_key_id=self._authority().anchor_key_id,
            )

    def test_production_wrapper_has_no_key_or_environment_override(self) -> None:
        from mlb_backtest import v52_r4_signed_anchor as anchor

        self.assertEqual(
            tuple(anchor.verify_production_authority_anchor.__annotations__),
            ("anchor_bytes", "signature", "return"),
        )
        with patch.dict(os.environ, {"MLB_R4_PUBLIC_KEY": self._authority().anchor_public_key.hex()}):
            payload, signature = self._signed(self._document())
            with self.assertRaises(InvalidSignature):
                anchor.verify_production_authority_anchor(payload, signature)

    def test_successful_synthetic_anchor_signing_comes_from_one_private_profile_resolver(self) -> None:
        authority = self._authority()
        payload, signature = self._signed(self._document())
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        self.assertEqual(
            verify_authority_anchor(
                payload, signature, public_key=authority.anchor_public_key,
                expected_key_id=authority.anchor_key_id,
            )["key_id"],
            authority.anchor_key_id,
        )

    def test_verified_claim_is_deeply_immutable(self) -> None:
        from mlb_backtest.v52_r4_signed_anchor import verify_authority_anchor

        payload, signature = self._signed(self._document())
        claim = verify_authority_anchor(
            payload, signature, public_key=self._authority().anchor_public_key,
            expected_key_id=self._authority().anchor_key_id,
        )
        for value, key in (
            (claim, "kind"),
            (claim["source_approval"], "logical_raw_root"),
            (claim["artifacts"], "inventory"),
            (claim["artifacts"]["inventory"], "path"),
            (claim["amendment"]["design"], "git_blob_id"),
            (claim["receipt_comparison"], "source_content_manifest_sha256"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(TypeError):
                    value[key] = "mutated"  # type: ignore[index]

    def test_verifier_profiles_cannot_sign_but_raw_issuance_capability_can(self) -> None:
        verifier = self._authority()
        raw_free = self._authority_module._resolve_source_authority()
        issuance = self._issuance_authority()
        self.assertFalse(hasattr(verifier, "sign_anchor_message"))
        self.assertFalse(hasattr(raw_free, "sign_anchor_message"))
        self.assertTrue(callable(issuance.sign_anchor_message))

    def test_issued_inventory_authority_api_and_resolver_parameter_are_absent(self) -> None:
        authority = self._authority_module
        self.assertNotIn("verify_issued_r4_source_authority_profile", authority.__all__)
        self.assertFalse(hasattr(authority, "verify_issued_r4_source_authority_profile"))
        self.assertNotIn("r4_inventory", inspect.signature(authority._resolve_source_authority).parameters)
        self.assertIs(self._authority(), self._profile)

    def test_anchor_length_limit_rejects_before_ed25519_verification(self) -> None:
        from mlb_backtest import v52_r4_signed_anchor as anchor

        oversized = b"x" * (anchor.MAX_AUTHORITY_ANCHOR_BYTES + 1)
        with patch.object(anchor.Ed25519PublicKey, "from_public_bytes") as verifier:
            with self.assertRaises(InvalidSignature):
                anchor.verify_authority_anchor(
                    oversized, b"x" * 64,
                    public_key=self._authority().anchor_public_key,
                    expected_key_id=self._authority().anchor_key_id,
                )
        verifier.assert_not_called()


class AuthorityImportIsolationTests(unittest.TestCase):
    def test_authority_import_does_not_load_offline_signer_or_cryptography(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import mlb_backtest.v52_r4_authority; "
                "raise SystemExit(any(name == 'mlb_backtest.v52_r4_offline_signer' or name.startswith('cryptography') for name in sys.modules))",
            ],
            cwd=Path.cwd(),
            check=False,
        )
        self.assertEqual(result.returncode, 0)
