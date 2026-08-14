from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import mlb_backtest.protocols as protocol_module
from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file
from mlb_backtest.execution_registry import (
    advance_execution_phase,
    initialize_execution_registry,
)
from mlb_backtest.preflight import build_preflight, parse_preflight, protected_inventory
from mlb_backtest.protocols import (
    expected_historical_run,
    load_prospective_spec,
    load_run_spec,
    load_v52_config,
    parse_run_spec,
)
from mlb_backtest.verify_v52_design import (
    CANONICAL_JSON_FILES,
    DELIVERY_RECEIPT_RELATIVE,
    FINAL_HTML_PATH,
    build_git_inventory,
    fitted_model_card_count,
    historical_execution_status,
    authenticated_historical_phase,
    load_canonical_json,
    make_tiny_git_fixture,
    main,
    observation_namespace_status,
    verify_artifact_agreement,
    verify_contract_schemas,
    verify_design_deliverables,
    verify_html_payload,
    verify_historical_phase_namespaces,
    validate_delivery_paths,
    validate_delivery_receipt_payload,
    verify_leakage_evidence,
    verify_markdown_agreement,
    verify_namespaces_differ,
    verify_notebook_agreement,
    verify_protected_inventory,
    verify_protected_snapshot,
    verify_preflight_payload,
    verify_task11_phase,
    write_git_inventory,
)
from tests.test_preflight import make_complete_inventory
from tests.test_execution_registry import (
    CONTRACT_RELATIVE as REGISTRY_CONTRACT_RELATIVE,
    DESIGN_RELATIVE as REGISTRY_DESIGN_RELATIVE,
    PLAN_RELATIVE as REGISTRY_PLAN_RELATIVE,
    PROTECTED_RELATIVE as REGISTRY_PROTECTED_RELATIVE,
    ExecutionRegistryTest,
)
import mlb_backtest.verify_v52_design as verifier_module


ROOT = Path(__file__).resolve().parents[1]


class DesignVerificationTest(unittest.TestCase):
    HISTORICAL_AUTHORITY_RELATIVES = (
        "protocols/v5_2_qd/historical_walkforward_500.json",
        "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
        (
            "protocols/v5_2_qd/"
            "historical_walkforward_500_recovery_schedule_conflicts_r2.json"
        ),
    )

    def copy_historical_authorities(self, root: Path) -> None:
        for relative in self.HISTORICAL_AUTHORITY_RELATIVES:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if relative.endswith("_recovery_schedule_conflicts_r2.json"):
                target.write_bytes(
                    canonical_json_bytes(protocol_module.expected_recovery_source_run())
                    + b"\n"
                )
            else:
                shutil.copy2(ROOT / relative, target)

    def test_all_recovery_r2_schema_branches_are_exact(self) -> None:
        recovery_schemas = frozenset(
            {
                "v5_2_acquisition_checkpoint.schema.json",
                "v5_2_acquisition_contract.schema.json",
                "v5_2_clean_source_replay_receipt.schema.json",
                "v5_2_execution_receipt.schema.json",
                "v5_2_game_evidence_inventory.schema.json",
                "v5_2_normalized_snapshot.schema.json",
                "v5_2_run_spec.schema.json",
                "v5_2_schedule_anchor_inventory.schema.json",
                "v5_2_source_inventory.schema.json",
                "v5_2_source_receipt_commit.schema.json",
                "v5_2_source_review_receipt.schema.json",
                "v5_2_timezone_candidate.schema.json",
                "v5_2_timezone_review_receipt.schema.json",
                "v5_2_timezone_seal_receipt.schema.json",
                "v5_2_venue_timezones.schema.json",
            }
        )
        recovery_id = "historical_walkforward_500_recovery_schedule_conflicts_r2"
        r3_recovery_id = (
            "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3"
        )
        r4_recovery_id = (
            "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4"
        )
        former_recovery_id = "historical_walkforward_500_recovery_schedule_conflicts"
        successor_authority_paths = {
            recovery_id: (
                "protocols/v5_2_qd/"
                "historical_walkforward_500_recovery_schedule_conflicts_r2.json",
                "protocols/v5_2_qd/"
                "historical_source_acquisition_recovery_schedule_conflicts_r2.json",
            ),
            r3_recovery_id: (
                "protocols/v5_2_qd/"
                "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json",
                "protocols/v5_2_qd/"
                "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json",
            ),
            r4_recovery_id: (
                "protocols/v5_2_qd/"
                "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r4.json",
                "protocols/v5_2_qd/"
                "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r4.json",
            ),
        }
        recovery_branch_digests = {
            ("v5_2_acquisition_checkpoint.schema.json", "oneOf", 2): "7e406484ec19dd235dce819bb7f10247173b54f7745e6d757a22a2aa8792a51b",
            ("v5_2_acquisition_contract.schema.json", "oneOf", 2): "a1202b7f9f85fbce2a0d5b4d36a2419316b6997f5aa2ff7d64cecc4c0f70e34c",
            ("v5_2_clean_source_replay_receipt.schema.json", "oneOf", 2): "c584af03a61f9f7d73e7d552c261560a93091eb7ec48f79c21089aa8f6e2e3b3",
            ("v5_2_execution_receipt.schema.json", "oneOf", 0): "704badb5dc23ff8e318fd722a85f3c1b320a5a8108675000cbcfd5fd12b2bee3",
            ("v5_2_normalized_snapshot.schema.json", "oneOf", 3): "a7bb65bc5cebc7216bac7f333757de33a164f06723556c50b3fe9065f29782de",
            ("v5_2_normalized_snapshot.schema.json", "oneOf", 4): "d437d60fcfb6806dc226f4183b453e349bebc57949dd6b2d0cc44589e3af5c8f",
            ("v5_2_normalized_snapshot.schema.json", "oneOf", 10): "f03f19f4b3bdbf9a721f5b0784b49b1cda15d50addb789414f055aef683b275e",
            ("v5_2_normalized_snapshot.schema.json", "oneOf", 11): "aacae095957f1b2d9a8ec69063e4a7a66bace10a400d9df266c6030edd1abc88",
            ("v5_2_run_spec.schema.json", "oneOf", 2): "8ae189531e93c373590c1ce47b39b17a502f66bfb0dedeceea18730f8a7b722d",
            ("v5_2_schedule_anchor_inventory.schema.json", "oneOf", 2): "b3bbdedbee1ef3e8032e3fe71ca3d7185e6bc487547016113d5b716d4004c9cc",
            ("v5_2_source_inventory.schema.json", "oneOf", 2): "0daf3fff9bd3b9e8ecd1f6e27e372f12a81d09ff9cc0cf2e43ef7098fae2447e",
            ("v5_2_source_receipt_commit.schema.json", "oneOf", 2): "c3482bde0725a8b4b0b885b75cc09fd710d8f2636cc38b34db73f3c098bc5d08",
            ("v5_2_source_review_receipt.schema.json", "oneOf", 2): "2c9251941c123e30520036d4786ebdd174a5fb937e6a592d3eb95807786cdcda",
        }
        timezone_authority_branch_digests = {
            "v5_2_timezone_candidate.schema.json": {
                recovery_id: "8e2346160fc385f4bded0c2e6ca4a5788a00d4d7c689f2967cab64d801f86020",
                r3_recovery_id: "d02abc86275a8fbcab8561b45d254cc9b4111f511dbcffcbb0c8f50d1385efea",
            },
            "v5_2_timezone_review_receipt.schema.json": {
                recovery_id: "e0e3c2b55fad875d5ec064c0e9ca6219dfae5b87e594c9e3998545ac28300933",
                r3_recovery_id: "6be3baf83ee237ff079245203e0dacd3b79dd1c01ca2ec59f58bbb84db250897",
            },
            "v5_2_timezone_seal_receipt.schema.json": {
                recovery_id: "3372c11d477e93d2bad3d3e6e2d679a340bbea830bc32bc958b88a73e423992a",
                r3_recovery_id: "28bc04a5ef7c7a8bdd056ce3315c4c8e9dea503e666df867b3db5acbcbe6e7ce",
            },
            "v5_2_venue_timezones.schema.json": {
                recovery_id: "18ef64b4c5ec2b25d48ca3d6cf4342cb67a5c3cc953d62a0aeb8e29ad66dfdac",
                r3_recovery_id: "68849f66af629c78e372b01d36c33a0afcded8ee66b577562bac2b07a00196fb",
            },
        }
        game_authority_branches = {
            "historical_walkforward_500": {
                "run_spec_path": "protocols/v5_2_qd/historical_walkforward_500.json",
                "contract_path": "protocols/v5_2_qd/historical_source_acquisition.json",
                "digest": "4ca892c6d9e15ff46c14e9f3018d2dd9d09738c817fc3d0cc15b1d1d593a34b1",
            },
            "historical_walkforward_500_superseding_565534": {
                "run_spec_path": (
                    "protocols/v5_2_qd/"
                    "historical_walkforward_500_superseding_565534.json"
                ),
                "contract_path": (
                    "protocols/v5_2_qd/"
                    "historical_source_acquisition_superseding_565534.json"
                ),
                "digest": "d882df1539d421879743d41f036291be8ee4eaa0c671d5fa2d54ede7f833ebd5",
            },
            recovery_id: {
                "run_spec_path": (
                    "protocols/v5_2_qd/"
                    "historical_walkforward_500_recovery_schedule_conflicts_r2.json"
                ),
                "contract_path": (
                    "protocols/v5_2_qd/"
                    "historical_source_acquisition_recovery_schedule_conflicts_r2.json"
                ),
                "digest": "36a90d274a3e1888a2e18c8b317f2360adcac22a5dc8e677a750ffce4d3861fd",
            },
            r3_recovery_id: {
                "run_spec_path": (
                    "protocols/v5_2_qd/"
                    "historical_walkforward_500_recovery_schedule_conflicts_missing_venue_r3.json"
                ),
                "contract_path": (
                    "protocols/v5_2_qd/"
                    "historical_source_acquisition_recovery_schedule_conflicts_missing_venue_r3.json"
                ),
                "digest": "65a090d7896612c7ccd219d876d46542d0780033f37d68dd36f5d50b0942e636",
            },
        }
        parent_branch_digests = {
            ("v5_2_acquisition_checkpoint.schema.json", 0): "2bdec33826fed06217a0596c917226db189d7b8d42db47e6ffe8dae4f833408b",
            ("v5_2_acquisition_checkpoint.schema.json", 1): "a0553e9ace0693ccfd590fe2d92bc0e23778ea514f25f72fa113af80d2525c39",
            ("v5_2_acquisition_contract.schema.json", 0): "cffa359607167101262d8761ae4aac945673831e6b04ca25e878303c9a7ba5af",
            ("v5_2_acquisition_contract.schema.json", 1): "0d93ff43f4a1dae276ea015dfc4406f225d336ece96c4f78c9b238c54e2d47e6",
            ("v5_2_clean_source_replay_receipt.schema.json", 0): "063b4b339461aa46f09f9c81b3a0e7a7f15d16a32a8bf85276fcc2741626f09d",
            ("v5_2_clean_source_replay_receipt.schema.json", 1): "96e985a9c38a8f50a38968032e210b260b8cf1278b0c70b4bc72d9579564818e",
            ("v5_2_execution_receipt.schema.json", 1): "9777dc57a036432891478da9a0742e73d35452a8784511f3253034775dfcf4a8",
            ("v5_2_execution_receipt.schema.json", 2): "7a78f4a66e38758d32f9bcd669e7f7467359038c0649b0eca75a29857447c257",
            ("v5_2_normalized_snapshot.schema.json", 1): "53496e58282c3cfc92cfc6afb16399cd3c25ffae0d6c935fce833fc3e67b5064",
            ("v5_2_normalized_snapshot.schema.json", 2): "4eced70f6b86cee132bdec6d12e06619adaf44571309208e9b2c5abb4932e638",
            ("v5_2_normalized_snapshot.schema.json", 5): "dda9357ed6e9fdd6e44b097a4fa59a96b058286b1f7f91fc76bd57b4f75c7c46",
            ("v5_2_normalized_snapshot.schema.json", 6): "a5c3a5eacdc582063d68648dd3958f31ba9824333dcbdd2c980eebb73dd39836",
            ("v5_2_run_spec.schema.json", 0): "a7ab5cf21ce58619c5d71ed6bf7fba77dec47e1f1c20d0483ab045dbe8cb3c18",
            ("v5_2_run_spec.schema.json", 1): "05dd8e03c6dab3f0cded5dbc39128480913faf1312308d09ff76901d87ed02ac",
            ("v5_2_schedule_anchor_inventory.schema.json", 0): "3fbd2b0bfdb2432ce4a1f6af87ae0c5acbb32da1e83907432bbcc8c7e2851d2c",
            ("v5_2_schedule_anchor_inventory.schema.json", 1): "dbd1a866b6d91cf080e459fcfe533bc9f162e0c93c3675c3e1d419b7936b9dfc",
            ("v5_2_source_inventory.schema.json", 0): "d44af09fecea9ebf5493755ec2976e291a787b9c679d20d6513ddb7cedb49dcd",
            ("v5_2_source_inventory.schema.json", 1): "1f7b9a1a120bb9b343ae6c199a74b5cd0eeb2158fc9a7bc658292f59a9bb8701",
            ("v5_2_source_receipt_commit.schema.json", 0): "8d02b1bc40db65fe8d6694635389caa04df384324bd42b0c24bc442cbfd07369",
            ("v5_2_source_receipt_commit.schema.json", 1): "0ae93b815ed538f0c4c941b01df47e6a85415458035f3fd3acb4accfb3874e53",
            ("v5_2_source_review_receipt.schema.json", 0): "4bf3c74afe9df4301ec09f2fe1ff7f6d58cf3ad55925449b146e578ee10e3588",
            ("v5_2_source_review_receipt.schema.json", 1): "89b7da72b2f4d164483f2de52114d16260e7692b270750add14f4c94e0abc21b",
        }

        def exact_scalar_values(value: object) -> list[object]:
            if isinstance(value, dict):
                values: list[object] = []
                for child in value.values():
                    values.extend(exact_scalar_values(child))
                return values
            if isinstance(value, list):
                values = []
                for child in value:
                    values.extend(exact_scalar_values(child))
                return values
            return [value]

        observed_recovery_schemas = frozenset(
            path.name
            for path in (ROOT / "schemas").glob("*.schema.json")
            if any(
                token in exact_scalar_values(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                for token in (recovery_id, successor_authority_paths[recovery_id][0])
            )
        )
        self.assertEqual(observed_recovery_schemas, recovery_schemas)
        for name in recovery_schemas - {"v5_2_run_spec.schema.json"}:
            schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            self.assertNotIn(
                former_recovery_id,
                exact_scalar_values(schema),
            )

        for (name, collection, index), expected_digest in recovery_branch_digests.items():
            branch = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))[collection][index]
            with self.subTest(schema=name, collection=collection, index=index):
                self.assertIn(recovery_id, exact_scalar_values(branch))
                self.assertEqual(
                    hashlib.sha256(canonical_json_bytes(branch)).hexdigest(),
                    expected_digest,
                )

        successor_authority_branches = {
            recovery_id: {
                "run_spec_digest": "8ae189531e93c373590c1ce47b39b17a502f66bfb0dedeceea18730f8a7b722d",
                "contract_digest": "a1202b7f9f85fbce2a0d5b4d36a2419316b6997f5aa2ff7d64cecc4c0f70e34c",
            },
            r3_recovery_id: {
                "run_spec_digest": "f569e3914f1b24b1e48cc2621a02a5029db92608d35d95109ad52bf0b5fa65a8",
                "contract_digest": "8cdbba9eb09fa91865dd438e1622764326ade22460a05aa19b38df8f3c4ce252",
            },
            r4_recovery_id: {
                "run_spec_digest": "4ca16938527d65f99ea8e20ad3a801cdef97be3f2946f05acadd7eb40e00bd21",
                "contract_digest": "cc2ce2d365bb6a31d4c55ecd74f0db64dc71a5f74f339eb8aab5532696b8e3b7",
            },
        }
        run_schema = json.loads(
            (ROOT / "schemas/v5_2_run_spec.schema.json").read_text(encoding="utf-8")
        )
        contract_schema = json.loads(
            (ROOT / "schemas/v5_2_acquisition_contract.schema.json").read_text(
                encoding="utf-8"
            )
        )
        for run_id, expected in successor_authority_branches.items():
            run_matches = [
                branch
                for branch in run_schema["oneOf"]
                if branch.get("const", {}).get("run_id") == run_id
            ]
            contract_matches = [
                branch
                for branch in contract_schema["oneOf"]
                if (
                    branch.get("properties", {}).get("run_id", {}).get("const")
                    == run_id
                    and branch.get("properties", {})
                    .get("bindings", {})
                    .get("properties", {})
                    .get("run_spec", {})
                    .get("properties", {})
                    .get("path", {})
                    .get("const")
                    == successor_authority_paths[run_id][0]
                )
            ]
            with self.subTest(schema="v5_2_run_spec.schema.json", run_id=run_id):
                self.assertEqual(len(run_matches), 1)
                self.assertEqual(
                    hashlib.sha256(canonical_json_bytes(run_matches[0])).hexdigest(),
                    expected["run_spec_digest"],
                )
            with self.subTest(
                schema="v5_2_acquisition_contract.schema.json", run_id=run_id
            ):
                self.assertEqual(len(contract_matches), 1)
                self.assertEqual(
                    hashlib.sha256(canonical_json_bytes(contract_matches[0])).hexdigest(),
                    expected["contract_digest"],
                )

        for run_id, (_, contract_path) in successor_authority_paths.items():
            artifact_path = ROOT / contract_path
            if run_id == r4_recovery_id:
                # The canonical R4 contract remains intentionally absent; the
                # production no-fallback behavior is covered by the query-plan
                # loader test, while this oracle preserves the boundary.
                self.assertFalse(artifact_path.exists())
                continue
            contract = json.loads(artifact_path.read_text(encoding="utf-8"))
            with self.subTest(contract_artifact=artifact_path.name, run_id=run_id):
                self.assertEqual(contract["run_id"], run_id)
                self.assertEqual(
                    contract["bindings"]["run_spec"]["path"],
                    successor_authority_paths[run_id][0],
                )
                run_spec_path = ROOT / contract["bindings"]["run_spec"]["path"]
                self.assertEqual(
                    contract["bindings"]["run_spec"]["sha256"],
                    hashlib.sha256(run_spec_path.read_bytes()).hexdigest(),
                )

        # The contract schema carries the run-spec binding; its own file path is
        # verified against the checked-in contract artifact above.
        contract_authority_pairs = {
            (
                branch["properties"]["run_id"]["const"],
                branch["properties"]["bindings"]["properties"]["run_spec"]
                ["properties"]["path"]["const"],
            )
            for branch in contract_schema["oneOf"]
            if branch.get("properties", {}).get("run_id", {}).get("const")
            in successor_authority_paths
        }
        r4_contract_matches = [
            branch
            for branch in contract_schema["oneOf"]
            if branch.get("properties", {}).get("run_id", {}).get("const")
            == r4_recovery_id
            and branch.get("properties", {})
            .get("bindings", {})
            .get("properties", {})
            .get("run_spec", {})
            .get("properties", {})
            .get("path", {})
            .get("const")
            == successor_authority_paths[r4_recovery_id][0]
        ]
        self.assertEqual(len(r4_contract_matches), 1)
        r4_contract_run_spec_path = (
            r4_contract_matches[0]["properties"]["bindings"]["properties"]["run_spec"]
            ["properties"]["path"]["const"]
        )
        self.assertEqual(
            r4_contract_run_spec_path, successor_authority_paths[r4_recovery_id][0]
        )
        self.assertNotEqual(
            r4_contract_run_spec_path, successor_authority_paths[r3_recovery_id][0]
        )
        self.assertNotIn(
            (r4_recovery_id, successor_authority_paths[r3_recovery_id][0]),
            contract_authority_pairs,
        )
        self.assertNotIn(
            (r4_recovery_id, successor_authority_paths[recovery_id][0]),
            contract_authority_pairs,
        )

        game_schema = json.loads(
            (ROOT / "schemas/v5_2_game_evidence_inventory.schema.json").read_text(
                encoding="utf-8"
            )
        )

        def binding_path(binding: dict[str, object]) -> str | None:
            reference = binding.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                binding = game_schema["$defs"][reference.removeprefix("#/$defs/")]
            properties = binding.get("properties", {})
            if not isinstance(properties, dict):
                return None
            path = properties.get("path", {})
            return path.get("const") if isinstance(path, dict) else None

        for run_id, expected in game_authority_branches.items():
            matches = [
                branch
                for branch in game_schema["oneOf"]
                if branch.get("properties", {})
                .get("run_id", {})
                .get("const")
                == run_id
            ]
            with self.subTest(schema="v5_2_game_evidence_inventory.schema.json", run_id=run_id):
                self.assertEqual(len(matches), 1)
                branch = matches[0]
                properties = branch["properties"]
                self.assertEqual(
                    binding_path(properties["run_spec"]), expected["run_spec_path"]
                )
                self.assertEqual(
                    binding_path(properties["acquisition_contract"]),
                    expected["contract_path"],
                )
                self.assertEqual(
                    hashlib.sha256(canonical_json_bytes(branch)).hexdigest(),
                    expected["digest"],
                )

        authority_paths = successor_authority_paths
        for name, digests in timezone_authority_branch_digests.items():
            schema = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))
            for run_id, expected_digest in digests.items():
                run_path, contract_path = authority_paths[run_id]
                matches = []
                for branch in schema["allOf"]:
                    authority = (
                        branch.get("then", {})
                        .get("properties", {})
                        .get("authority", {})
                        .get("properties", {})
                    )
                    if (
                        authority.get("run_spec", {})
                        .get("properties", {})
                        .get("path", {})
                        .get("const")
                        == run_path
                        and authority.get("acquisition_contract", {})
                        .get("properties", {})
                        .get("path", {})
                        .get("const")
                        == contract_path
                    ):
                        matches.append(branch)
                with self.subTest(schema=name, run_id=run_id):
                    self.assertEqual(len(matches), 1)
                    self.assertEqual(
                        hashlib.sha256(canonical_json_bytes(matches[0])).hexdigest(),
                        expected_digest,
                    )

        for (name, index), expected_digest in parent_branch_digests.items():
            branch = json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))["oneOf"][index]
            with self.subTest(schema=name, branch=index):
                self.assertEqual(
                    hashlib.sha256(canonical_json_bytes(branch)).hexdigest(),
                    expected_digest,
                )

    def test_phase_namespace_scan_rejects_directory_identity_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = ExecutionRegistryTest().make_repo(Path(directory))
            initialize_execution_registry(
                root,
                run_spec,
                design_path=root / REGISTRY_DESIGN_RELATIVE,
                plan_path=root / REGISTRY_PLAN_RELATIVE,
                acquisition_contract_path=root / REGISTRY_CONTRACT_RELATIVE,
            )
            namespace = root / "data/v5_2_qd/historical"
            namespace.mkdir(parents=True)
            hook = getattr(verifier_module, "_namespace_scan_hook", None)
            self.assertIsNotNone(hook, "descriptor namespace scan hook is missing")
            raced = False

            def replace(_root: Path, relative: str) -> None:
                nonlocal raced
                if raced or relative != "data/v5_2_qd/historical":
                    return
                raced = True
                namespace.rename(namespace.with_name("historical-old"))
                namespace.mkdir()

            with patch.object(verifier_module, "_namespace_scan_hook", side_effect=replace), self.assertRaisesRegex(ValueError, "identity|changed"):
                verify_historical_phase_namespaces(root, run_spec)

    def test_design_verifier_reads_phase_from_registry_not_file_presence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = ExecutionRegistryTest().make_repo(Path(directory))
            initialize_execution_registry(
                root,
                run_spec,
                design_path=root / REGISTRY_DESIGN_RELATIVE,
                plan_path=root / REGISTRY_PLAN_RELATIVE,
                acquisition_contract_path=root / REGISTRY_CONTRACT_RELATIVE,
            )
            source = root / "data/v5_2_qd/historical/sources/apparent_ready.json"
            source.parent.mkdir(parents=True)
            source.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                authenticated_historical_phase(root, run_spec), "DESIGN_ACCEPTED"
            )
            with self.assertRaisesRegex(ValueError, "DESIGN_ACCEPTED|namespace"):
                verify_historical_phase_namespaces(root, run_spec)
            source.unlink()
            advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / REGISTRY_PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            source.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                authenticated_historical_phase(root, run_spec), "ACQUIRING"
            )
            self.assertEqual(
                verify_historical_phase_namespaces(root, run_spec), "ACQUIRING"
            )
            forbidden = root / "data/v5_2_qd/historical/outcomes/too_early.json"
            forbidden.parent.mkdir(parents=True)
            forbidden.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "forbidden|namespace"):
                verify_historical_phase_namespaces(root, run_spec)

    def test_design_and_v51_baseline_use_distinct_reviewed_commits(self) -> None:
        self.assertEqual(
            getattr(verifier_module, "CURRENT_APPROVED_DESIGN_COMMIT", None),
            "fd04e8b",
        )
        self.assertEqual(
            getattr(verifier_module, "CURRENT_APPROVED_DESIGN_SHA256", None),
            "2439a5b355f0257c584abfb18ab6c714a0b24e543e5acdee97f2739e74cb8d9a",
        )
        self.assertEqual(
            getattr(verifier_module, "V51_PROTECTED_BASELINE_COMMIT", None),
            "a4aa8d2",
        )
        self.assertNotEqual(
            verifier_module.CURRENT_APPROVED_DESIGN_COMMIT,
            verifier_module.V51_PROTECTED_BASELINE_COMMIT,
        )

    def test_current_scope_fingerprints_design_and_implementation_plan(self) -> None:
        required_docs = {
            "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md",
            "docs/superpowers/plans/2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md",
            "mlb_backtest/execution_registry.py",
            "tests/test_execution_registry.py",
        }
        self.assertTrue(
            required_docs.issubset(set(verifier_module.CURRENT_SCOPE_SINGLE_FILES))
        )
        scope = verifier_module.capture_current_scope(ROOT)
        self.assertTrue(required_docs.issubset(set(scope["single_files"])))

    def test_task7d_timezone_authority_is_required_canonical_pinned_and_current_scope_bound(self) -> None:
        required_schemas = {
            "schemas/v5_2_acquisition_checkpoint.schema.json",
            "schemas/v5_2_game_evidence_inventory.schema.json",
            "schemas/v5_2_schedule_anchor_inventory.schema.json",
            "schemas/v5_2_split_receipt.schema.json",
            "schemas/v5_2_timezone_candidate.schema.json",
            "schemas/v5_2_timezone_review_receipt.schema.json",
            "schemas/v5_2_timezone_seal_receipt.schema.json",
            "schemas/v5_2_venue_timezones.schema.json",
        }
        required_code = {
            "mlb_backtest/v52_game_evidence_inventory.py",
            "mlb_backtest/v52_query_plan.py",
            "mlb_backtest/v52_schedule_inventory.py",
            "mlb_backtest/v52_timezone_review.py",
            "tests/test_v52_query_plan.py",
            "tests/test_v52_game_evidence_inventory.py",
            "tests/test_v52_schedule_inventory.py",
            "tests/test_v52_timezone_review.py",
        }
        required_docs = {
            "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md",
            "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md",
            "docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md",
        }
        self.assertTrue(
            required_schemas.issubset(set(verifier_module.EXPECTED_CURRENT_FILES))
        )
        self.assertTrue(required_schemas.issubset(set(CANONICAL_JSON_FILES)))
        self.assertTrue(
            (required_code | required_docs).issubset(
                set(verifier_module.CURRENT_SCOPE_SINGLE_FILES)
            )
        )
        pins = getattr(verifier_module, "TASK7D_SCHEMA_DOCUMENT_SHA256", {})
        self.assertTrue(required_schemas.issubset(set(pins)))
        self.assertEqual(
            pins["schemas/v5_2_game_evidence_inventory.schema.json"],
            "fd56b0632f819c81848ebca1facff771673686800de7a21732e713affb6e6e0d",
        )
        for relative in required_schemas:
            self.assertEqual(pins[relative], sha256_file(ROOT / relative))
        scope = verifier_module.capture_current_scope(ROOT)
        self.assertTrue(
            (required_code | required_docs).issubset(set(scope["single_files"]))
        )
        schema_nodes = scope["trees"]["schemas"]["nodes"]
        self.assertTrue(required_schemas.issubset(set(schema_nodes)))

    def test_namespace_second_recursive_scan_detects_prior_subtree_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            namespace = root / "data/v5_2_qd/historical"
            prior = namespace / "raw/prior.json"
            later = namespace / "sources/later.json"
            prior.parent.mkdir(parents=True)
            later.parent.mkdir(parents=True)
            prior.write_text("before\n", encoding="utf-8")
            later.write_text("stable\n", encoding="utf-8")

            def mutate_prior(_root: Path, relative: str) -> None:
                self.assertEqual(relative, "data/v5_2_qd/historical")
                prior.write_text("after!\n", encoding="utf-8")

            with patch.object(
                verifier_module,
                "_namespace_scan_hook",
                side_effect=mutate_prior,
            ), self.assertRaisesRegex(ValueError, "changed|snapshot|identity"):
                verifier_module._verify_namespace_children(
                    root,
                    "data/v5_2_qd/historical",
                    frozenset({"raw", "sources"}),
                )

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_namespace_path_open_stat_failures_do_not_leak_fds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "namespace"
            target.mkdir()
            target_inode = target.stat().st_ino
            original_fstat = os.fstat
            before = len(os.listdir("/dev/fd"))

            def fail_target(descriptor: int):
                metadata = original_fstat(descriptor)
                if metadata.st_ino == target_inode:
                    raise OSError("injected target fstat failure")
                return metadata

            with patch.object(
                verifier_module.os, "fstat", side_effect=fail_target
            ):
                for _ in range(20):
                    with self.assertRaisesRegex(ValueError, "unavailable"):
                        verifier_module._verify_namespace_children(
                            root, "namespace", frozenset()
                        )
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_change_contract_rejects_working_design_that_differs_from_pin(self) -> None:
        design_relative = (
            "docs/superpowers/specs/"
            "2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
        )
        approved_bytes = (ROOT / design_relative).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for relative in (
                "config/v5_2_qd.json",
                "protocols/v5_2_qd/change_contract.json",
                design_relative,
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, target)
            (root / design_relative).write_bytes(approved_bytes + b"late mutation\n")

            def fake_git(_root: Path, *args: str) -> bytes:
                if args[:2] == ("rev-parse", f"{verifier_module.CURRENT_APPROVED_DESIGN_COMMIT}:{design_relative}"):
                    return b"approved-blob\n"
                if args == ("cat-file", "blob", "approved-blob"):
                    return approved_bytes
                raise AssertionError(args)

            with (
                patch("mlb_backtest.verify_v52_design.git", side_effect=fake_git),
                self.assertRaisesRegex(ValueError, "working design.*approved|approved.*working design"),
            ):
                verifier_module.verify_change_contract(root)

    def test_current_deliverables_cross_validate(self) -> None:
        receipt = verify_design_deliverables(ROOT, outputs=None)
        html = receipt.pop("html")
        self.assertEqual(
            receipt,
            {
                "status": "PASS",
                "diagnostic": {"primary": {"W": 67, "L": 33, "N": 100}},
                "v52": {
                    "model_version": "v5.2-QD",
                    "fitted_model_card_count": 0,
                },
                "preflight": {"status": "BLOCKED_REQUIRED_ACQUISITION"},
                "historical": {"execution_status": "NOT_EXECUTED"},
                "prospective": {
                    "first_permitted_date": "2026-07-13",
                    "observation_status": "NOT_STARTED",
                },
                "immutability": {
                    "approved_commit": "a4aa8d2",
                    "tracked_blob_count": 6,
                    "full_tree_guard": "metadata_path_size_mtime_inode_mode",
                    "small_sealed_guard": "sha256",
                },
                "task11": {
                    "html": "PRESENT",
                    "delivery_receipt": "PRESENT",
                },
            },
        )
        self.assertIsInstance(html, dict)
        self.assertEqual(html.get("path"), str(FINAL_HTML_PATH))
        self.assertEqual(
            html.get("receipt", {}).get("html"), str(FINAL_HTML_PATH)
        )

    def test_every_deterministic_json_rejects_trailing_whitespace(self) -> None:
        for relative in CANONICAL_JSON_FILES:
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory).resolve()
                path = root / relative
                path.parent.mkdir(parents=True)
                path.write_bytes((ROOT / relative).read_bytes() + b" ")
                with self.assertRaisesRegex(ValueError, "canonical JSON"):
                    load_canonical_json(root, relative)

    def test_required_json_rejects_external_symlink_and_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            external = base / "external"
            external.mkdir()
            target = external / "config.json"
            target.write_bytes((ROOT / "config/v5_2_qd.json").read_bytes())

            direct_root = base / "direct"
            (direct_root / "config").mkdir(parents=True)
            (direct_root / "config/v5_2_qd.json").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink|regular file"):
                load_canonical_json(direct_root, "config/v5_2_qd.json")

            ancestor_root = base / "ancestor"
            ancestor_root.mkdir()
            (ancestor_root / "config").symlink_to(external, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink|ancestor"):
                load_canonical_json(ancestor_root, "config/config.json")

            hardlink_root = base / "hardlink"
            (hardlink_root / "config").mkdir(parents=True)
            os.link(target, hardlink_root / "config/v5_2_qd.json")
            with self.assertRaisesRegex(ValueError, "unalias|regular file"):
                load_canonical_json(hardlink_root, "config/v5_2_qd.json")

    def test_namespace_scan_rejects_broken_symlink_special_and_hardlink(self) -> None:
        for kind in ("broken_symlink", "fifo", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                namespace = root / "audit/v5_2_qd/historical"
                namespace.parent.mkdir(parents=True)
                if kind == "broken_symlink":
                    namespace.symlink_to(root / "missing", target_is_directory=True)
                else:
                    namespace.mkdir()
                    path = namespace / "unexpected"
                    if kind == "fifo":
                        os.mkfifo(path)
                        self.assertTrue(stat.S_ISFIFO(path.lstat().st_mode))
                    else:
                        source = root / "source"
                        source.write_text("x", encoding="utf-8")
                        os.link(source, path)
                with self.assertRaisesRegex(
                    ValueError, "namespace.*symlink|special|hardlink|unexpected"
                ):
                    historical_execution_status(root)

    def test_exit_integrity_recheck_uses_mock_without_touching_sealed_files(
        self,
    ) -> None:
        import mlb_backtest.verify_v52_design as verifier_module

        def sealed_metadata() -> dict[str, tuple[int, ...]]:
            paths = [ROOT / "config/v5_1_bt.json"]
            for relative in ("data/v5_1_bt", "audit/v5_1_bt"):
                paths.extend((ROOT / relative).rglob("*"))
            return {
                path.relative_to(ROOT).as_posix(): (
                    metadata.st_mode,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_nlink,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
                for path in paths
                if (metadata := path.lstat())
            }

        inventory_path = ROOT / "protocols/v5_2_qd/v51_protected_inventory.json"
        entry, evidence = verifier_module.capture_v51_integrity(ROOT, inventory_path)
        exit_fingerprint = json.loads(json.dumps(entry))
        exit_fingerprint["predictions_content_sha256"] = "0" * 64
        before = sealed_metadata()
        with (
            patch(
                "mlb_backtest.verify_v52_design.capture_v51_integrity",
                side_effect=((entry, evidence), (exit_fingerprint, evidence)),
            ),
            self.assertRaisesRegex(ValueError, "v5.1 integrity"),
        ):
            verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(sealed_metadata(), before)

    def test_task11_artifacts_are_phase_gated(self) -> None:
        for status in (
            {"html": "ABSENT", "delivery_receipt": "ABSENT"},
            {"html": "PRESENT", "delivery_receipt": "PRESENT"},
        ):
            verify_task11_phase(status)
        for status in (
            {"html": "PRESENT", "delivery_receipt": "ABSENT"},
            {"html": "ABSENT", "delivery_receipt": "PRESENT"},
        ):
            with self.assertRaisesRegex(ValueError, "Task 11 delivery artifact"):
                verify_task11_phase(status)

    def test_task11_artifact_pair_cannot_change_during_verification(self) -> None:
        absent = {"html": "ABSENT", "delivery_receipt": "ABSENT"}
        present = {"html": "PRESENT", "delivery_receipt": "PRESENT"}
        with (
            patch(
                "mlb_backtest.verify_v52_design.task11_artifact_status",
                side_effect=(absent, present),
            ),
            self.assertRaisesRegex(
                ValueError, "Task 11 delivery artifact changed during verification"
            ),
        ):
            verify_design_deliverables(ROOT, outputs=None)

    def test_delivery_paths_are_exact_constants(self) -> None:
        html, receipt = validate_delivery_paths(ROOT, FINAL_HTML_PATH.parent)
        self.assertEqual(html, FINAL_HTML_PATH)
        self.assertEqual(receipt, ROOT / DELIVERY_RECEIPT_RELATIVE)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "output path mismatch"):
                validate_delivery_paths(ROOT, Path(directory))

    def test_delivery_receipt_requires_full_installed_contract_shape(self) -> None:
        artifact = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json").read_text(
                encoding="utf-8"
            )
        )
        good = {
            "ok": True,
            "html": str(FINAL_HTML_PATH),
            "stages": {
                "validation": "passed",
                "package": "passed",
                "verification": "passed",
            },
            "counts": {
                "blocks": 17,
                "charts": 4,
                "html": 0,
                "metrics": 0,
                "tables": 2,
            },
            "sourceDialog": "passed",
            "sourceInteraction": "semantic_click",
            "viewports": [1440, 390],
            "timings": {
                "validateAndPackageMs": 1,
                "staticChartExtractionMs": 1,
                "browserRunMs": 1,
                "readerReadyMs": 1,
                "contentChecksMs": 1,
                "sourceDialogMs": 1,
                "responsiveChecksMs": 1,
                "verificationMs": 2,
                "totalMs": 4,
            },
        }
        installed_order = json.dumps(good, separators=(",", ":")).encode() + b"\n"
        sorted_order = canonical_json_bytes(good) + b"\n"
        self.assertNotEqual(installed_order, sorted_order)
        self.assertEqual(
            validate_delivery_receipt_payload(installed_order, artifact), good
        )
        cases = {
            "minimal": {"ok": True, "stages": good["stages"]},
            "extra": {**good, "artifact_sha256": "0" * 64},
            "cross_run_path": {**good, "html": "/tmp/old-report.html"},
            "wrong_counts": {**good, "counts": {**good["counts"], "charts": 3}},
            "wrong_stage": {
                **good,
                "stages": {**good["stages"], "verification": "structural_only"},
            },
            "wrong_viewports": {**good, "viewports": [1280, 390]},
        }
        for name, payload in cases.items():
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "delivery receipt"),
            ):
                validate_delivery_receipt_payload(
                    canonical_json_bytes(payload) + b"\n", artifact
                )
        invalid_encodings = (
            installed_order[:-1],
            sorted_order,
            json.dumps(good, indent=2).encode() + b"\n",
            installed_order + b" ",
            installed_order + b"\n",
            installed_order + b"{}",
            installed_order + b"\ntrailing",
        )
        for encoded in invalid_encodings:
            with (
                self.subTest(encoded=encoded[:30]),
                self.assertRaisesRegex(ValueError, "delivery receipt"),
            ):
                validate_delivery_receipt_payload(encoded, artifact)

    def test_html_verifier_uses_direct_node_and_parses_final_json_line(self) -> None:
        artifact = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json").read_text()
        )
        counts = {
            "blocks": 17,
            "charts": 4,
            "html": 0,
            "metrics": 0,
            "tables": 2,
        }
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory).resolve()
            root = temporary_root / "repo"
            output = temporary_root / "outputs"
            artifact_path = root / "reports/v5_1_bt/diagnostic_100_artifact.json"
            receipt_path = root / DELIVERY_RECEIPT_RELATIVE
            html_path = output / "mlb_v5_1_100_pick_diagnostic.html"
            artifact_path.parent.mkdir(parents=True)
            output.mkdir()
            artifact_path.write_bytes(canonical_json_bytes(artifact) + b"\n")
            html_path.write_text("<!doctype html>", encoding="utf-8")
            receipt = {
                "ok": True,
                "html": str(html_path),
                "stages": {
                    "validation": "passed",
                    "package": "passed",
                    "verification": "passed",
                },
                "counts": counts,
                "sourceDialog": "passed",
                "sourceInteraction": "semantic_click",
                "viewports": [1440, 390],
                "timings": {
                    "validateAndPackageMs": 1,
                    "staticChartExtractionMs": 1,
                    "browserRunMs": 1,
                    "readerReadyMs": 1,
                    "contentChecksMs": 1,
                    "sourceDialogMs": 1,
                    "responsiveChecksMs": 1,
                    "verificationMs": 1,
                    "totalMs": 2,
                },
            }
            receipt_path.write_bytes(
                json.dumps(receipt, separators=(",", ":")).encode() + b"\n"
            )
            verifier = {
                "ok": True,
                "counts": counts,
                "html": str(html_path),
                "sourceDialog": "passed",
                "sourceInteraction": "semantic_click",
                "timings": {
                    "browserRunMs": 1,
                    "readerReadyMs": 1,
                    "contentChecksMs": 1,
                    "sourceDialogMs": 1,
                    "responsiveChecksMs": 1,
                    "totalMs": 2,
                },
                "viewports": [1440, 390],
            }
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="npm notice update available\n"
                + json.dumps(verifier, separators=(",", ":"))
                + "\n",
                stderr="",
            )
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                result = verify_html_payload(html_path, artifact_path, receipt_path)
            self.assertEqual(result["sha256"], sha256_file(html_path))
            self.assertEqual(result["path"], str(html_path))
            self.assertEqual(result["fingerprint"]["sha256"], result["sha256"])
            self.assertEqual(result["receipt"]["html"], result["path"])
            command = run.call_args.args[0]
            self.assertEqual(command[0], "node")
            self.assertTrue(command[1].endswith("verify_portable_artifact.mjs"))
            self.assertNotIn("npm", command)

            forged = {**verifier, "unexpected": True}
            completed.stdout = json.dumps(forged) + "\n"
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(ValueError, "portable HTML verification"),
            ):
                verify_html_payload(html_path, artifact_path, receipt_path)

            forged_counts = {
                **verifier,
                "counts": {**counts, "metrics": False},
            }
            completed.stdout = json.dumps(forged_counts) + "\n"
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(ValueError, "portable HTML verification"),
            ):
                verify_html_payload(html_path, artifact_path, receipt_path)

            completed.stdout = json.dumps(verifier) + "\n"

            def replace_html_during_verification(*_args, **_kwargs):
                replacement = output / "replacement.html"
                replacement.write_text("<!doctype html>changed", encoding="utf-8")
                os.replace(replacement, html_path)
                return completed

            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    side_effect=replace_html_during_verification,
                ),
                self.assertRaisesRegex(ValueError, "HTML.*changed|identity|snapshot"),
            ):
                verify_html_payload(html_path, artifact_path, receipt_path)

    def test_html_verifier_reports_stderr_when_node_fails_before_json_parse(self) -> None:
        artifact = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json").read_text()
        )
        with tempfile.TemporaryDirectory() as directory:
            temporary_root = Path(directory).resolve()
            root = temporary_root / "repo"
            output = temporary_root / "outputs"
            artifact_path = root / "reports/v5_1_bt/diagnostic_100_artifact.json"
            receipt_path = root / DELIVERY_RECEIPT_RELATIVE
            html_path = output / "mlb_v5_1_100_pick_diagnostic.html"
            artifact_path.parent.mkdir(parents=True)
            output.mkdir()
            artifact_path.write_bytes(canonical_json_bytes(artifact) + b"\n")
            html_path.write_text("<!doctype html>", encoding="utf-8")
            receipt = json.loads(
                (ROOT / DELIVERY_RECEIPT_RELATIVE).read_text(encoding="utf-8")
            )
            receipt["html"] = str(html_path)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_bytes(
                json.dumps(receipt, separators=(",", ":")).encode() + b"\n"
            )
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr='{"ok":false,"code":"browser_failed","error":"Chromium failed"}\n',
            )
            with (
                patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                patch(
                    "mlb_backtest.verify_v52_design.subprocess.run",
                    return_value=completed,
                ),
                self.assertRaisesRegex(
                    ValueError,
                    r"portable HTML verification failed:.*browser_failed",
                ),
            ):
                verify_html_payload(html_path, artifact_path, receipt_path)

    def test_final_mode_rechecks_html_after_all_exit_checks(self) -> None:
        import mlb_backtest.verify_v52_design as verifier_module

        real_scope = verifier_module.capture_current_scope
        for late_swap in (False, True):
            with (
                self.subTest(late_swap=late_swap),
                tempfile.TemporaryDirectory() as directory,
            ):
                temporary_root = Path(directory).resolve()
                output = temporary_root / "outputs"
                output.mkdir()
                html_path = output / "mlb_v5_1_100_pick_diagnostic.html"
                html_path.write_text("<!doctype html>verified", encoding="utf-8")
                fingerprint = verifier_module._absolute_regular_file_fingerprint(
                    html_path
                )
                verified_html = {
                    "path": str(html_path),
                    "sha256": fingerprint["sha256"],
                    "fingerprint": fingerprint,
                    "receipt": {"html": str(html_path)},
                }
                scope_calls = 0

                def capture_scope(root):
                    nonlocal scope_calls
                    scope_calls += 1
                    captured = real_scope(root)
                    if late_swap and scope_calls == 2:
                        replacement = output / "late-replacement.html"
                        replacement.write_text(
                            "<!doctype html>late swap", encoding="utf-8"
                        )
                        os.replace(replacement, html_path)
                    return captured

                contexts = (
                    patch("mlb_backtest.verify_v52_design.FINAL_HTML_PATH", html_path),
                    patch(
                        "mlb_backtest.verify_v52_design.task11_artifact_status",
                        return_value={
                            "html": "PRESENT",
                            "delivery_receipt": "PRESENT",
                        },
                    ),
                    patch(
                        "mlb_backtest.verify_v52_design.verify_html_payload",
                        return_value=verified_html,
                    ),
                    patch(
                        "mlb_backtest.verify_v52_design.capture_current_scope",
                        side_effect=capture_scope,
                    ),
                )
                with ExitStack() as stack:
                    for context in contexts:
                        stack.enter_context(context)
                    if late_swap:
                        with self.assertRaisesRegex(
                            ValueError, "HTML.*changed|fingerprint|identity"
                        ):
                            verify_design_deliverables(ROOT, outputs=output)
                    else:
                        result = verify_design_deliverables(ROOT, outputs=output)
                        self.assertEqual(result["html"], verified_html)

    def test_current_scope_fingerprint_detects_late_protocol_append(self) -> None:
        import mlb_backtest.verify_v52_design as verifier_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for relative in (
                "schemas",
                "protocols/v5_2_qd",
                "reports/v5_1_bt",
                "reports/v5_2_qd",
                "analysis/v5_1_bt",
                "config",
                "mlb_backtest",
                "scripts",
                "tests",
                ".gitignore",
                "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md",
                "docs/superpowers/plans/2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md",
                "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md",
                "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md",
                "docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md",
            ):
                source = ROOT / relative
                target = root / relative
                if source.is_dir():
                    shutil.copytree(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            before = verifier_module.capture_current_scope(root)
            protocol = root / "protocols/v5_2_qd/prospective_2026.json"
            protocol_node = before["trees"]["protocols/v5_2_qd"]["nodes"][
                "protocols/v5_2_qd/prospective_2026.json"
            ]
            self.assertEqual(
                set(protocol_node),
                {
                    "kind",
                    "dev",
                    "ino",
                    "mode",
                    "nlink",
                    "size",
                    "mtime_ns",
                    "ctime_ns",
                    "sha256",
                },
            )
            self.assertEqual(
                set(before["forbidden_namespaces"]),
                {
                    *verifier_module.HISTORICAL_NAMESPACE_ROOTS,
                    *verifier_module.PROSPECTIVE_OBSERVATION_ROOTS,
                    verifier_module.FITTED_NAMESPACE_ROOT,
                },
            )
            protocol.write_bytes(protocol.read_bytes() + b" ")
            after = verifier_module.capture_current_scope(root)
            self.assertNotEqual(before, after)

            shutil.copy2(ROOT / "protocols/v5_2_qd/prospective_2026.json", protocol)
            before_design_mutation = verifier_module.capture_current_scope(root)
            design = (
                root
                / "docs/superpowers/specs/"
                "2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
            )
            design.write_bytes(design.read_bytes() + b" ")
            after_design_mutation = verifier_module.capture_current_scope(root)
            self.assertNotEqual(before_design_mutation, after_design_mutation)

    def test_final_boundary_rechecks_scope_and_forbidden_namespaces(self) -> None:
        import mlb_backtest.verify_v52_design as verifier_module

        entry_scope = verifier_module.capture_current_scope(ROOT)
        changed_scope = json.loads(json.dumps(entry_scope))
        changed_scope["late_protocol_append"] = True
        with (
            patch(
                "mlb_backtest.verify_v52_design.capture_current_scope",
                side_effect=(entry_scope, changed_scope),
            ),
            self.assertRaisesRegex(ValueError, "current-scope.*changed"),
        ):
            verify_design_deliverables(ROOT, outputs=None)

        with (
            patch(
                "mlb_backtest.verify_v52_design.historical_execution_status",
                side_effect=("NOT_EXECUTED", "PARTIAL_OUTPUT_PRESENT"),
            ),
            self.assertRaisesRegex(ValueError, "historical outputs"),
        ):
            verify_design_deliverables(ROOT, outputs=None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(historical_execution_status(root), "NOT_EXECUTED")
            late_report = root / "reports/v5_2_qd/historical/late.json"
            late_report.parent.mkdir(parents=True)
            late_report.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                historical_execution_status(root), "PARTIAL_OUTPUT_PRESENT"
            )

    def test_direct_api_rejects_root_with_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(ROOT.parent, target_is_directory=True)
            alias = linked_parent / ROOT.name
            with self.assertRaisesRegex(ValueError, "root.*symlink|ancestor"):
                load_canonical_json(alias, "config/v5_2_qd.json")

    def test_fitted_model_card_scan_rejects_broken_symlink_and_fifo(self) -> None:
        for kind in ("symlink", "broken_symlink", "fifo", "socket", "hardlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                base = root / "models/v5_2_qd/folds"
                base.mkdir(parents=True)
                unexpected = base / "unexpected"
                if kind in {"symlink", "hardlink"}:
                    source = root / "source"
                    source.write_text("x", encoding="utf-8")
                    if kind == "symlink":
                        unexpected.symlink_to(source)
                    else:
                        os.link(source, unexpected)
                elif kind == "broken_symlink":
                    unexpected.symlink_to(root / "missing")
                elif kind == "fifo":
                    os.mkfifo(unexpected)
                else:
                    unix_socket = socket.socket(socket.AF_UNIX)
                    self.addCleanup(unix_socket.close)
                    unix_socket.bind(str(unexpected))
                with self.assertRaisesRegex(
                    ValueError, "namespace.*symlink|special|hardlink|unexpected"
                ):
                    fitted_model_card_count(root)

    def test_verifier_uses_no_network_or_python_write_api(self) -> None:
        # Regression defense only: production authority is the verifier's
        # lstat/no-follow input validation and entry/exit integrity recheck.
        before = protected_inventory(ROOT)
        real_builtin_open = builtins.open
        real_io_open = io.open
        real_os_open = os.open
        real_popen = subprocess.Popen
        real_subprocess_run = subprocess.run

        def validate_subprocess_invocation(args, cwd, environment):
            node_command = [
                "node",
                (
                    "/Users/taejunoh/.codex/plugins/cache/openai-curated-remote/"
                    "data-analytics/0.2.8-13ceeea1f599/skills/build-report/scripts/"
                    "verify_portable_artifact.mjs"
                ),
                "--html",
                str(FINAL_HTML_PATH),
                "--artifact",
                str(ROOT / "reports/v5_1_bt/diagnostic_100_artifact.json"),
            ]
            if args == node_command:
                if (
                    cwd
                    != Path(
                        "/Users/taejunoh/.codex/plugins/cache/openai-curated-remote/"
                        "data-analytics/0.2.8-13ceeea1f599"
                    )
                    or environment is not None
                ):
                    raise AssertionError("HTML verifier execution context differs")
                return
            allowed_prefixes = {
                "ls-tree": ["git", "ls-tree", "-r", "-z", "--full-tree"],
                "ls-files": ["git", "ls-files", "-z", "--"],
                "hash-object": ["git", "hash-object", "--"],
                "status": [
                    "git",
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                    "--",
                ],
                "rev-parse": ["git", "rev-parse"],
                "cat-file": ["git", "cat-file", "blob"],
            }
            if not isinstance(args, list) or len(args) < 2:
                raise AssertionError(f"subprocess forbidden: {args!r}")
            prefix = allowed_prefixes.get(args[1])
            if prefix is None or args[: len(prefix)] != prefix or "-w" in args:
                raise AssertionError(f"subprocess argv differs: {args!r}")
            if cwd != ROOT:
                raise AssertionError(f"subprocess cwd differs: {cwd!r}")
            expected_environment = os.environ.copy()
            expected_environment["GIT_OPTIONAL_LOCKS"] = "0"
            if environment != expected_environment:
                raise AssertionError("git environment differs")

        def guarded_open(opener):
            def call(file, mode="r", *args, **kwargs):
                if any(flag in mode for flag in "wax+"):
                    raise AssertionError("write-mode open forbidden")
                return opener(file, mode, *args, **kwargs)

            return call

        def guarded_os_open(path, flags, *args, **kwargs):
            write_flags = (
                os.O_WRONLY
                | os.O_RDWR
                | os.O_CREAT
                | os.O_TRUNC
                | os.O_APPEND
                | os.O_EXCL
            )
            if flags & write_flags:
                raise AssertionError("write-mode os.open forbidden")
            return real_os_open(path, flags, *args, **kwargs)

        def guarded_subprocess_run(args, *positional, **kwargs):
            validate_subprocess_invocation(
                args, kwargs.get("cwd"), kwargs.get("env")
            )
            if args[0] == "node":
                if (
                    set(kwargs) != {"cwd", "check", "capture_output", "text"}
                    or kwargs["check"] is not False
                    or kwargs["capture_output"] is not True
                    or kwargs["text"] is not True
                ):
                    raise AssertionError("HTML verifier execution mode differs")
                return real_subprocess_run(args, *positional, **kwargs)
            if set(kwargs) != {"cwd", "check", "capture_output", "env"}:
                raise AssertionError(f"subprocess kwargs differ: {kwargs!r}")
            if kwargs["check"] is not False or kwargs["capture_output"] is not True:
                raise AssertionError("subprocess execution mode differs")
            return real_subprocess_run(args, *positional, **kwargs)

        def guarded_popen(args, *positional, **kwargs):
            validate_subprocess_invocation(
                args, kwargs.get("cwd"), kwargs.get("env")
            )
            return real_popen(args, *positional, **kwargs)

        patches = (
            patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "http.client.HTTPConnection.request",
                side_effect=AssertionError("network forbidden"),
            ),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("network forbidden"),
            ),
            patch("socket.socket", side_effect=AssertionError("network forbidden")),
            patch("builtins.open", side_effect=guarded_open(real_builtin_open)),
            patch("io.open", side_effect=guarded_open(real_io_open)),
            patch("os.open", side_effect=guarded_os_open),
            *(
                patch(name, side_effect=AssertionError("mutation forbidden"))
                for name in (
                    "pathlib.Path.write_bytes",
                    "pathlib.Path.write_text",
                    "pathlib.Path.rename",
                    "pathlib.Path.replace",
                    "pathlib.Path.unlink",
                    "pathlib.Path.mkdir",
                    "pathlib.Path.touch",
                    "pathlib.Path.symlink_to",
                    "pathlib.Path.hardlink_to",
                    "pathlib.Path.chmod",
                    "os.replace",
                    "os.rename",
                    "os.remove",
                    "os.unlink",
                    "os.link",
                    "os.symlink",
                    "os.chmod",
                    "os.fchmod",
                    "os.truncate",
                    "os.ftruncate",
                    "os.system",
                )
            ),
            patch("subprocess.Popen", side_effect=guarded_popen),
            patch(
                "subprocess.check_output",
                side_effect=AssertionError("check_output forbidden"),
            ),
            patch(
                "subprocess.check_call",
                side_effect=AssertionError("check_call forbidden"),
            ),
            patch(
                "mlb_backtest.verify_v52_design.subprocess.run",
                side_effect=guarded_subprocess_run,
            ),
        )
        with ExitStack() as stack:
            for guard in patches:
                stack.enter_context(guard)
            __import__("mlb_backtest.verify_v52_design")
            receipt = verify_design_deliverables(ROOT, outputs=None)
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(before, protected_inventory(ROOT))

    def test_main_and_module_cli_success_output_and_failure(self) -> None:
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertEqual(main(["--root", str(ROOT), "--skip-html"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "PASS")
        with (
            self.assertRaises(SystemExit) as raised,
            patch("sys.stderr", io.StringIO()),
        ):
            main(["--root", str(ROOT)])
        self.assertEqual(raised.exception.code, 2)
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "output path mismatch"),
        ):
            main(["--root", str(ROOT), "--outputs", directory])

        success = subprocess.run(
            [
                "python3",
                "-m",
                "mlb_backtest.verify_v52_design",
                "--root",
                ".",
                "--skip-html",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(json.loads(success.stdout), json.loads(stdout.getvalue()))
        failure = subprocess.run(
            [
                "python3",
                "-m",
                "mlb_backtest.verify_v52_design",
                "--root",
                ".",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(failure.returncode, 2)
        self.assertIn("--outputs is required", failure.stderr)

    def test_mutating_any_sealed_v51_artifact_fails(self) -> None:
        for change in ("mutate", "add", "delete"):
            with (
                self.subTest(change=change),
                tempfile.TemporaryDirectory() as directory,
            ):
                repo = make_tiny_git_fixture(Path(directory).resolve())
                inventory = write_git_inventory(repo, "HEAD")
                target = repo / "data/v5_1_bt/locked/predictions.jsonl"
                if change == "mutate":
                    target.write_bytes(b"changed\n")
                elif change == "add":
                    (target.parent / "added.json").write_text("{}\n", encoding="utf-8")
                else:
                    target.unlink()
                with self.assertRaisesRegex(ValueError, "protected v5.1 inventory"):
                    verify_protected_inventory(repo, inventory)

    def test_full_tree_metadata_guard_detects_ignored_addition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_tiny_git_fixture(Path(directory).resolve())
            (repo / ".gitignore").write_text("*.bin\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "ignore fixture"], cwd=repo, check=True
            )
            snapshot = protected_inventory(repo)
            (repo / "data/v5_1_bt/locked/ignored.bin").write_bytes(b"x")
            with self.assertRaisesRegex(ValueError, "protected evidence metadata"):
                verify_protected_snapshot(repo, snapshot)

    def test_small_sealed_hash_detects_metadata_preserving_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            path = root / "data/v5_1_bt/locked/predictions.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"abc\n")
            before = protected_inventory(root)
            file_stat = path.stat()
            path.write_bytes(b"xyz\n")
            os.utime(path, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
            after = protected_inventory(root)
            self.assertEqual(
                before["metadata_path_size_mtime_inode_mode"],
                after["metadata_path_size_mtime_inode_mode"],
            )
            self.assertNotEqual(
                before["small_sealed_sha256"], after["small_sealed_sha256"]
            )
            with self.assertRaisesRegex(ValueError, "small sealed digest"):
                verify_protected_snapshot(root, before)

    def test_inventory_is_pinned_to_approved_design_commit(self) -> None:
        inventory_path = ROOT / "protocols/v5_2_qd/v51_protected_inventory.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        self.assertEqual(verifier_module.V51_PROTECTED_BASELINE_COMMIT, "a4aa8d2")
        self.assertEqual(inventory["approved_commit"], "a4aa8d2")
        self.assertEqual(inventory, build_git_inventory(ROOT, "a4aa8d2"))
        self.assertEqual(
            inventory_path.read_bytes(), canonical_json_bytes(inventory) + b"\n"
        )

    def test_task0_protected_addition_is_exactly_declared_and_hash_bound(
        self,
    ) -> None:
        expected = {
            "audit/v5_1_bt/protected_raw_inventory.json": (
                "2ec8bf0237cc5bdb00ddb6d2236ac631"
                "bb793aed9ba79f308672a9ecb90809c6"
            )
        }
        self.assertEqual(verifier_module.APPROVED_PROTECTED_ADDITIONS, expected)
        inventory_path = ROOT / "protocols/v5_2_qd/v51_protected_inventory.json"
        verify_protected_inventory(ROOT, inventory_path)
        with (
            patch(
                "mlb_backtest.verify_v52_design.sha256_file",
                return_value="0" * 64,
            ),
            self.assertRaisesRegex(ValueError, "protected v5.1 inventory"),
        ):
            verify_protected_inventory(ROOT, inventory_path)

    def test_absence_status_is_derived_not_hardcoded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(historical_execution_status(root), "NOT_EXECUTED")
            self.assertEqual(observation_namespace_status(root), "NOT_STARTED")
            path = root / "audit/v5_2_qd/historical/nested/unexpected.bin"
            path.parent.mkdir(parents=True)
            path.write_text("x", encoding="utf-8")
            self.assertEqual(
                historical_execution_status(root), "PARTIAL_OUTPUT_PRESENT"
            )
            observation = root / "data/v5_2_qd/prospective_2026/observations.jsonl"
            observation.parent.mkdir(parents=True)
            observation.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                observation_namespace_status(root), "PARTIAL_OUTPUT_PRESENT"
            )

    def test_inventory_baseline_cannot_be_repointed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = make_tiny_git_fixture(Path(directory).resolve())
            inventory = write_git_inventory(repo, "HEAD")
            payload = json.loads(inventory.read_text(encoding="utf-8"))
            payload["approved_commit"] = "HEAD~1"
            inventory.write_bytes(canonical_json_bytes(payload) + b"\n")
            with self.assertRaisesRegex(ValueError, "protected v5.1 inventory"):
                verify_protected_inventory(repo, inventory)

    def test_leakage_status_tamper_fails(self) -> None:
        diagnostic = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(encoding="utf-8")
        )
        diagnostic["leakage"]["statistical_as_of_safety"][
            "lock_to_outcome_join_sequence_status"
        ] = "PASS"
        with self.assertRaisesRegex(ValueError, "leakage evidence differs"):
            verify_leakage_evidence(diagnostic)

    def test_cross_artifact_tampering_fails(self) -> None:
        diagnostic = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(encoding="utf-8")
        )
        cases = ("markdown", "notebook_source", "notebook_output", "chart_map")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                (root / "reports/v5_1_bt").mkdir(parents=True)
                (root / "analysis/v5_1_bt").mkdir(parents=True)
                for relative in (
                    "reports/v5_1_bt/diagnostic_100.md",
                    "reports/v5_1_bt/diagnostic_100_artifact.json",
                    "reports/v5_1_bt/diagnostic_100_chart_map.json",
                    "analysis/v5_1_bt/diagnostic_100.ipynb",
                ):
                    shutil.copy2(ROOT / relative, root / relative)
                if case == "markdown":
                    path = root / "reports/v5_1_bt/diagnostic_100.md"
                    path.write_text(path.read_text() + "tamper\n", encoding="utf-8")
                    verifier = verify_markdown_agreement
                elif case.startswith("notebook"):
                    path = root / "analysis/v5_1_bt/diagnostic_100.ipynb"
                    notebook = json.loads(path.read_text(encoding="utf-8"))
                    if case == "notebook_source":
                        notebook["cells"][1]["source"] = ["print('tamper')\n"]
                    else:
                        notebook["cells"][-1]["outputs"][0]["text"] = ["{}\n"]
                    path.write_text(json.dumps(notebook), encoding="utf-8")
                    verifier = None
                else:
                    path = root / "reports/v5_1_bt/diagnostic_100_chart_map.json"
                    chart_map = json.loads(path.read_text(encoding="utf-8"))
                    chart_map["monthly"]["takeaway"] = "tamper"
                    path.write_text(json.dumps(chart_map), encoding="utf-8")
                    verifier = verify_artifact_agreement
                with self.assertRaises(ValueError):
                    if case.startswith("notebook"):
                        verify_notebook_agreement(ROOT, diagnostic, notebook_path=path)
                    else:
                        assert verifier is not None
                        verifier(root, diagnostic)

    def test_every_notebook_cell_source_output_and_metadata_tamper_fails(self) -> None:
        diagnostic = json.loads(
            (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_text(encoding="utf-8")
        )
        canonical_path = ROOT / "analysis/v5_1_bt/diagnostic_100.ipynb"
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        mutations = [
            ("source", index, None) for index in range(len(canonical["cells"]))
        ] + [
            ("output", cell_index, output_index)
            for cell_index, cell in enumerate(canonical["cells"])
            for output_index in range(len(cell["outputs"]))
        ]
        mutations.append(("metadata", None, None))
        for kind, cell_index, output_index in mutations:
            with (
                self.subTest(
                    kind=kind, cell_index=cell_index, output_index=output_index
                ),
                tempfile.TemporaryDirectory() as directory,
            ):
                notebook = json.loads(json.dumps(canonical))
                if kind == "source":
                    notebook["cells"][cell_index]["source"].append("# tamper\n")
                elif kind == "output":
                    notebook["cells"][cell_index]["outputs"][output_index][
                        "text"
                    ].append("tamper\n")
                else:
                    notebook["metadata"]["tamper"] = True
                path = Path(directory) / "diagnostic.ipynb"
                path.write_bytes(canonical_json_bytes(notebook) + b"\n")
                with self.assertRaisesRegex(ValueError, "deterministic execution"):
                    verify_notebook_agreement(ROOT, diagnostic, notebook_path=path)

    def test_schema_const_tampering_fails(self) -> None:
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "schemas").mkdir()
            for name in (
                "v5_2_qd.schema.json",
                "v5_2_run_spec.schema.json",
                "v5_2_prospective.schema.json",
                "v5_2_preflight.schema.json",
                "v5_2_split_receipt.schema.json",
                "v5_2_acquisition_checkpoint.schema.json",
                "v5_2_execution_receipt.schema.json",
                "v5_2_game_evidence_inventory.schema.json",
                "v5_2_schedule_anchor_inventory.schema.json",
                "v5_2_timezone_candidate.schema.json",
                "v5_2_timezone_review_receipt.schema.json",
                "v5_2_timezone_seal_receipt.schema.json",
                "v5_2_venue_timezones.schema.json",
            ):
                shutil.copy2(ROOT / "schemas" / name, root / "schemas" / name)
            self.copy_historical_authorities(root)
            for mutation in ("const", "additionalProperties", "properties"):
                with self.subTest(mutation=mutation):
                    path = root / "schemas/v5_2_qd.schema.json"
                    shutil.copy2(ROOT / "schemas/v5_2_qd.schema.json", path)
                    schema = json.loads(path.read_text(encoding="utf-8"))
                    if mutation == "const":
                        schema["const"]["model_version"] = "tampered"
                    elif mutation == "additionalProperties":
                        schema["additionalProperties"] = True
                    else:
                        schema["properties"].pop("model_version")
                    path.write_text(json.dumps(schema), encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError, "schema/parser disagreement"
                    ):
                        verify_contract_schemas(
                            root, config, historical, prospective, preflight
                        )

    def test_combined_run_schema_writer_and_design_verifier_preserve_both_exact_branches(self) -> None:
        self.assertTrue(hasattr(protocol_module, "historical_authority_schema"))
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        source = protocol_module.load_source_run_spec(
            ROOT
            / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json"
        )
        recovery = protocol_module.parse_source_run_spec(
            protocol_module.expected_recovery_source_run()
        )
        r3 = protocol_module.parse_source_run_spec(
            protocol_module.expected_r3_recovery_source_run()
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_bytes()
            )
        )
        schema = json.loads((ROOT / "schemas/v5_2_run_spec.schema.json").read_bytes())
        self.assertEqual(schema, protocol_module.historical_authority_schema())
        self.assertEqual(
            canonical_json_bytes(source.raw),
            canonical_json_bytes(schema["oneOf"][1]["const"]),
        )
        self.assertEqual(
            canonical_json_bytes(recovery.raw),
            canonical_json_bytes(schema["oneOf"][2]["const"]),
        )
        self.assertEqual(
            canonical_json_bytes(r3.raw),
            canonical_json_bytes(schema["oneOf"][3]["const"]),
        )
        verify_contract_schemas(ROOT, config, historical, prospective, preflight)

    def test_task13_runner_validates_recovery_parents_separately(self) -> None:
        import scripts.run_task13_exact_suite as runner

        parent_names = (
            "superseding_2020_630882",
            "superseding_2021_633224",
        )
        catalog = SimpleNamespace(
            parents={
                name: SimpleNamespace(
                    registration=SimpleNamespace(
                        relative=Path(f"parent/{name}/registration.json"),
                        sha256=f"{index}1" * 32,
                    ),
                    attempt=SimpleNamespace(
                        relative=Path(f"parent/{name}/attempt.json"),
                        sha256=f"{index}2" * 32,
                    ),
                    quarantine=SimpleNamespace(
                        relative=Path(f"parent/{name}/quarantine.json"),
                        sha256=f"{index}3" * 32,
                    ),
                    raw=SimpleNamespace(
                        relative=Path(f"parent/{name}/body.raw"),
                        sha256=f"{index}4" * 32,
                    ),
                )
                for index, name in enumerate(parent_names, start=1)
            },
            read_json=Mock(),
            read_raw=Mock(),
            reauthenticate=Mock(),
        )
        with patch.object(runner, "build_recovery_parent_catalog", return_value=catalog):
            observed = runner.validate_recovery_parent_inputs(ROOT)

        self.assertEqual(
            set(observed),
            {
                f"parent/{name}/{artifact}"
                for name in parent_names
                for artifact in (
                    "registration.json",
                    "attempt.json",
                    "quarantine.json",
                    "body.raw",
                )
            },
        )
        self.assertEqual(
            catalog.read_json.call_args_list,
            [
                call(name, artifact)
                for name in parent_names
                for artifact in ("registration", "attempt", "quarantine")
            ],
        )
        self.assertEqual(
            catalog.read_raw.call_args_list,
            [call(name) for name in parent_names],
        )
        catalog.reauthenticate.assert_called_once_with()

    def assert_missing_historical_authority_fails(self, missing: str) -> None:
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_bytes()
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shutil.copytree(ROOT / "schemas", root / "schemas")
            for relative in self.HISTORICAL_AUTHORITY_RELATIVES:
                if relative == missing:
                    continue
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative.endswith("_recovery_schedule_conflicts_r2.json"):
                    target.write_bytes(
                        canonical_json_bytes(
                            protocol_module.expected_recovery_source_run()
                        )
                        + b"\n"
                    )
                else:
                    shutil.copy2(ROOT / relative, target)
            with self.assertRaisesRegex(
                ValueError,
                rf"required input.*{Path(missing).name}|unavailable.*{Path(missing).name}",
            ):
                verify_contract_schemas(
                    root, config, historical, prospective, preflight
                )

    def test_contract_schema_verifier_requires_checked_in_old_authority(self) -> None:
        self.assert_missing_historical_authority_fails(
            self.HISTORICAL_AUTHORITY_RELATIVES[0]
        )

    def test_contract_schema_verifier_requires_checked_in_source_authority(self) -> None:
        self.assert_missing_historical_authority_fails(
            self.HISTORICAL_AUTHORITY_RELATIVES[1]
        )

    def test_task7d_closed_schema_semantic_tampering_fails(self) -> None:
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (
                    ROOT / "reports/v5_2_qd/walkforward_500_preflight.json"
                ).read_bytes()
            )
        )
        filenames = (
            "v5_2_split_receipt.schema.json",
            "v5_2_acquisition_checkpoint.schema.json",
            "v5_2_execution_receipt.schema.json",
            "v5_2_game_evidence_inventory.schema.json",
            "v5_2_schedule_anchor_inventory.schema.json",
            "v5_2_timezone_candidate.schema.json",
            "v5_2_timezone_review_receipt.schema.json",
            "v5_2_timezone_seal_receipt.schema.json",
            "v5_2_venue_timezones.schema.json",
        )
        malformed = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "additionalProperties": False,
            "type": "object",
        }
        verify_contract_schemas(ROOT, config, historical, prospective, preflight)
        for filename in filenames:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                shutil.copytree(ROOT / "schemas", root / "schemas")
                self.copy_historical_authorities(root)
                (root / "schemas" / filename).write_bytes(
                    canonical_json_bytes(malformed) + b"\n"
                )
                with self.assertRaisesRegex(
                    ValueError, "schema/parser disagreement"
                ):
                    verify_contract_schemas(
                        root, config, historical, prospective, preflight
                    )

    def test_schedule_inventory_schema_requires_nested_closure_and_known_formats(self):
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_bytes()
            )
        )
        relative = "schemas/v5_2_schedule_anchor_inventory.schema.json"
        for mutation in (
            "definition-closure",
            "inline-counts-closure",
            "inline-static-batch-closure",
            "unknown-format",
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                shutil.copytree(ROOT / "schemas", root / "schemas")
                self.copy_historical_authorities(root)
                path = root / relative
                schema = json.loads(path.read_bytes())
                if mutation == "definition-closure":
                    schema["$defs"]["ordinarySeasonSource"][
                        "additionalProperties"
                    ] = True
                elif mutation == "inline-counts-closure":
                    schema["properties"]["counts"]["additionalProperties"] = True
                elif mutation == "inline-static-batch-closure":
                    schema["properties"]["static_batch"][
                        "additionalProperties"
                    ] = True
                else:
                    schema["$defs"]["anchorCore"]["properties"]["official_date"][
                        "format"
                    ] = "unknown-date-format"
                path.write_bytes(canonical_json_bytes(schema) + b"\n")
                pins = {
                    **verifier_module.TASK7D_SCHEMA_DOCUMENT_SHA256,
                    relative: sha256_file(path),
                }
                with (
                    patch.object(
                        verifier_module,
                        "TASK7D_SCHEMA_DOCUMENT_SHA256",
                        pins,
                    ),
                    self.assertRaisesRegex(
                        ValueError, "schema/parser disagreement"
                    ),
                ):
                    verify_contract_schemas(
                        root, config, historical, prospective, preflight
                    )

    def test_nested_schema_refs_closure_and_condition_tampering_fails(self) -> None:
        config = load_v52_config(ROOT / "config/v5_2_qd.json")
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        preflight = parse_preflight(
            json.loads(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        cases = (
            ("historical_ref", "v5_2_run_spec.schema.json"),
            ("historical_closure", "v5_2_run_spec.schema.json"),
            ("prospective_closure", "v5_2_prospective.schema.json"),
            ("preflight_ref", "v5_2_preflight.schema.json"),
            ("preflight_condition", "v5_2_preflight.schema.json"),
            ("preflight_properties", "v5_2_preflight.schema.json"),
        )
        for case, filename in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                shutil.copytree(ROOT / "schemas", root / "schemas")
                self.copy_historical_authorities(root)
                path = root / "schemas" / filename
                schema = json.loads(path.read_text(encoding="utf-8"))
                if case == "historical_ref":
                    schema["oneOf"][0]["properties"]["folds"]["items"][
                        "$ref"
                    ] = "#/$defs/x"
                elif case == "historical_closure":
                    schema["$defs"]["fold"]["additionalProperties"] = True
                elif case == "prospective_closure":
                    schema["properties"]["polling"]["additionalProperties"] = True
                elif case == "preflight_ref":
                    schema["properties"]["protected_tree_after"]["$ref"] = "#/$defs/x"
                elif case == "preflight_condition":
                    schema["$defs"]["query_evidence"].pop("allOf")
                else:
                    schema["$defs"]["source_record"]["properties"].pop("source")
                path.write_bytes(canonical_json_bytes(schema) + b"\n")
                with self.assertRaisesRegex(ValueError, "schema/parser disagreement"):
                    verify_contract_schemas(
                        root, config, historical, prospective, preflight
                    )

    def test_historical_and_prospective_namespace_overlap_fails(self) -> None:
        historical = load_run_spec(
            ROOT / "protocols/v5_2_qd/historical_walkforward_500.json"
        )
        prospective = load_prospective_spec(
            ROOT / "protocols/v5_2_qd/prospective_2026.json"
        )
        overlap = replace(prospective, namespace=historical.raw["namespaces"]["locked"])
        with self.assertRaisesRegex(ValueError, "namespaces overlap"):
            verify_namespaces_differ(historical, overlap)

    def test_forged_preflight_inventory_digest_fails(self) -> None:
        saved = json.loads(
            (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").read_text(
                encoding="utf-8"
            )
        )
        forged = json.loads(json.dumps(saved))
        forged["generated_from_inventory_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "recomputed preflight"):
            verify_preflight_payload(forged, saved)

    def test_manifest_and_raw_hash_tampering_change_recomputed_preflight(self) -> None:
        for mutation in ("manifest", "raw"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = make_complete_inventory(Path(directory).resolve() / "root")
                run = parse_run_spec(expected_historical_run())
                saved = build_preflight(root, run)
                manifest_path = next((root / "inventory").rglob("*.manifest.json"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if mutation == "manifest":
                    manifest["raw_sha256"] = "0" * 64
                    manifest_path.write_bytes(canonical_json_bytes(manifest) + b"\n")
                else:
                    raw_path = root / manifest["raw_path"]
                    contents = raw_path.read_bytes()
                    file_stat = raw_path.stat()
                    raw_path.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
                    os.utime(
                        raw_path,
                        ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns),
                    )
                recomputed = build_preflight(root, run)
                self.assertIn(
                    "RAW_HASH_MISMATCH",
                    {
                        field
                        for season in recomputed["seasons"].values()
                        for source in season["sources"].values()
                        for field in source["missing_critical_fields"]
                    },
                )
                with self.assertRaisesRegex(ValueError, "recomputed preflight"):
                    verify_preflight_payload(saved, recomputed)


if __name__ == "__main__":
    unittest.main()
