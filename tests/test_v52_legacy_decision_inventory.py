from __future__ import annotations

import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest import mock

import jsonschema

from mlb_backtest.manifest import RawStore
from mlb_backtest.statcast import statcast_url
from mlb_backtest.v52_evidence import logical_query_id
from mlb_backtest.v52_protected_inventory import build_protected_raw_inventory
from tests.test_v52_legacy import (
    _canonical_file,
    _legacy_schedule_query,
    _load,
    _schedule_raw,
    _statcast_query,
)
from tests.test_v52_query_plan import _statcast_authority_fixture


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT = Path("data/v5_1_bt/raw")
INVENTORY_PATH = Path("audit/v5_1_bt/protected_raw_inventory.json")
CONTRACT_PATH = Path("protocols/v5_2_qd/historical_source_acquisition.json")
CONTRACT_SCHEMA_PATH = Path("schemas/v5_2_acquisition_contract.schema.json")
DECISION_PATH = Path(
    "audit/v5_2_qd/historical/acquisition/legacy_decision_inventory.json"
)
DECISION_SCHEMA_PATH = Path("schemas/v5_2_legacy_decision_inventory.schema.json")
STATCAST_RAW = (
    ROOT / "tests/fixtures/v5_2_qd/sources/statcast_mini.csv"
).read_bytes()


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            _plain(value),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _legacy_decision_repository(parent: Path) -> tuple[Path, Path, str]:
    root = _statcast_authority_fixture(parent)
    legacy_root = root / LEGACY_ROOT
    RawStore(legacy_root).put(
        "baseball_savant_statcast",
        {
            "end": "2024-04-02",
            "game_type": "R",
            "start": "2024-04-02",
            "url": statcast_url("2024-04-02", "2024-04-02"),
        },
        STATCAST_RAW,
    )
    RawStore(legacy_root).put(
        "baseball_savant_statcast",
        {
            "end": "2025-04-02",
            "game_type": "R",
            "start": "2025-04-02",
            "url": statcast_url("2025-04-02", "2025-04-02"),
        },
        STATCAST_RAW,
    )
    RawStore(legacy_root).put(
        "mlb_schedule",
        _legacy_schedule_query(),
        _schedule_raw(),
    )
    inventory = build_protected_raw_inventory(root, legacy_root)
    inventory_path = root / INVENTORY_PATH
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_bytes(_canonical_file(inventory))
    inventory_sha256 = hashlib.sha256(inventory_path.read_bytes()).hexdigest()

    baseline = Path("protocols/v5_2_qd/v51_protected_inventory.json")
    (root / baseline).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / baseline, root / baseline)
    contract_path = root / CONTRACT_PATH
    contract = json.loads(contract_path.read_bytes())
    contract["legacy_protection"]["protected_inventory_sha256"] = (
        inventory_sha256
    )
    contract_path.write_bytes(_canonical_json(contract))
    schema_path = root / CONTRACT_SCHEMA_PATH
    schema = json.loads(schema_path.read_bytes())
    schema["const"] = contract
    schema_path.write_bytes(_canonical_json(schema))
    decision_schema_path = root / DECISION_SCHEMA_PATH
    decision_schema_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / DECISION_SCHEMA_PATH, decision_schema_path)
    _git(root, "add", CONTRACT_PATH.as_posix(), CONTRACT_SCHEMA_PATH.as_posix())
    _git(root, "add", baseline.as_posix())
    _git(root, "add", DECISION_SCHEMA_PATH.as_posix())
    _git(root, "commit", "-qm", "bind protected legacy fixture")
    from mlb_backtest.v52_query_plan import (
        build_statcast_root_batch,
        publish_query_batch,
    )

    root_batch = build_statcast_root_batch(root)
    publish_query_batch(root, root_batch)
    return root, inventory_path, inventory_sha256


def _statcast_root_batch_path(root: Path) -> Path:
    from mlb_backtest.v52_query_plan import build_statcast_root_batch

    batch = build_statcast_root_batch(root)
    return (
        root
        / "audit/v5_2_qd/historical/acquisition/query_batches"
        / f"{batch.batch_id}.json"
    )


class V52LegacyDecisionInventoryTest(unittest.TestCase):
    def test_public_api_schema_and_verified_record_are_closed(self) -> None:
        spec = importlib.util.find_spec(
            "mlb_backtest.v52_legacy_decision_inventory"
        )
        self.assertIsNotNone(spec, "legacy decision inventory module is missing")
        self.assertTrue((ROOT / DECISION_SCHEMA_PATH).is_file())

        from mlb_backtest import v52_legacy_decision_inventory as inventory_module

        self.assertEqual(
            tuple(
                field.name
                for field in dataclasses.fields(
                    inventory_module.VerifiedLegacyDecisionInventory
                )
            ),
            ("path", "sha256", "payload"),
        )
        self.assertTrue(
            inventory_module.VerifiedLegacyDecisionInventory.__dataclass_params__.frozen
        )
        self.assertEqual(
            inventory_module._LEGACY_DECISION_INVENTORY_RELATIVE,
            DECISION_PATH,
        )

    def test_candidate_evidence_is_stable_closed_and_repository_relative(self) -> None:
        from mlb_backtest.v52_legacy import (
            LegacyDecisionEvidence,
            PromotionDisposition,
            audit_legacy_candidate_evidence,
        )

        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(LegacyDecisionEvidence)),
            ("decision", "candidate_lineage", "semantic_sha256"),
        )
        self.assertTrue(LegacyDecisionEvidence.__dataclass_params__.frozen)
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            catalog = _load(root, root / LEGACY_ROOT, inventory_path, digest)
            query = _statcast_query(digest)
            evidence = audit_legacy_candidate_evidence(query, catalog)
            self.assertEqual(
                evidence,
                audit_legacy_candidate_evidence(query, catalog),
            )
            self.assertIs(
                evidence.decision.disposition,
                PromotionDisposition.PROMOTED,
            )
            self.assertEqual(evidence.decision.query_id, logical_query_id(query))
            self.assertRegex(evidence.semantic_sha256 or "", r"^[0-9a-f]{64}$")
            lineage = evidence.candidate_lineage
            self.assertIsNotNone(lineage)
            assert lineage is not None
            self.assertEqual(
                set(lineage),
                {"source", "manifest", "seal", "raw", "protected_inventory"},
            )
            self.assertEqual(set(lineage["manifest"]), {"path", "sha256"})
            self.assertEqual(set(lineage["seal"]), {"path", "sha256"})
            self.assertEqual(
                set(lineage["raw"]),
                {"path", "sha256", "byte_size", "row_count"},
            )
            self.assertEqual(
                set(lineage["protected_inventory"]), {"path", "sha256"}
            )
            for binding in (
                lineage["manifest"],
                lineage["seal"],
                lineage["raw"],
                lineage["protected_inventory"],
            ):
                self.assertFalse(Path(binding["path"]).is_absolute())

    def test_build_has_exact_scope_counts_order_and_decision_shapes(self) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            build_legacy_decision_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            payload = build_legacy_decision_inventory(root)
            self.assertEqual(
                set(payload),
                {
                    "schema_version",
                    "receipt_kind",
                    "run_id",
                    "run_spec",
                    "acquisition_contract",
                    "protected_inventory",
                    "statcast_root_batch",
                    "permitted_legacy_years",
                    "root_query_count",
                    "counts",
                    "decisions",
                },
            )
            self.assertEqual(payload["permitted_legacy_years"], [2022, 2023, 2024])
            self.assertEqual(payload["root_query_count"], 46)
            self.assertEqual(
                payload["counts"],
                {
                    "protected_catalog_record_count": 3,
                    "in_scope_statcast_record_count": 1,
                    "out_of_scope_record_count": 2,
                    "unique_candidate_range_count": 1,
                    "promoted_count": 1,
                    "fetch_required_count": 47,
                    "fatal_integrity_count": 0,
                },
            )
            decisions = payload["decisions"]
            self.assertEqual(len(decisions), 48)
            self.assertEqual(
                decisions,
                sorted(
                    decisions,
                    key=lambda item: (
                        item["range"]["year"],
                        item["range"]["start_date"],
                        item["range"]["end_date"],
                        item["candidate_query_id"],
                    ),
                ),
            )
            promoted = [
                item for item in decisions if item["disposition"] == "PROMOTED"
            ]
            fetched = [
                item
                for item in decisions
                if item["disposition"] == "FETCH_REQUIRED"
            ]
            self.assertEqual(len(promoted), 1)
            self.assertEqual(len(fetched), 47)
            self.assertEqual(promoted[0]["reason_codes"], [])
            self.assertIsNotNone(promoted[0]["candidate_lineage"])
            self.assertRegex(promoted[0]["semantic_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(all(item["reason_codes"] for item in fetched))
            self.assertTrue(
                all(item["candidate_lineage"] is None for item in fetched)
            )
            self.assertTrue(all(item["semantic_sha256"] is None for item in fetched))

    def test_publish_load_schema_idempotence_and_fatal_refusal_are_closed(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        from mlb_backtest.v52_legacy import (
            LegacyDecisionEvidence,
            PromotionDecision,
            PromotionDisposition,
            PromotionReasonCode,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            first = inventory_module.publish_legacy_decision_inventory(root)
            second = inventory_module.publish_legacy_decision_inventory(root)
            loaded = inventory_module.load_verified_legacy_decision_inventory(root)
            self.assertEqual(first, second)
            self.assertEqual(first, loaded)
            self.assertEqual(first.path, root / DECISION_PATH)
            self.assertEqual(first.path.read_bytes(), _canonical_json(first.payload))
            self.assertEqual(
                first.sha256,
                hashlib.sha256(first.path.read_bytes()).hexdigest(),
            )
            with self.assertRaises(TypeError):
                first.payload["unexpected"] = True
            with self.assertRaises(TypeError):
                first.payload["counts"]["promoted_count"] = 99
            with self.assertRaises(TypeError):
                first.payload["decisions"][0]["range"]["year"] = 2025

            schema = json.loads((root / DECISION_SCHEMA_PATH).read_bytes())
            jsonschema.Draft202012Validator(schema).validate(_plain(first.payload))
            for mutation in ("top", "decision", "fatal-lineage"):
                forged = copy.deepcopy(_plain(first.payload))
                if mutation == "top":
                    forged["unexpected"] = True
                elif mutation == "decision":
                    forged["decisions"][0]["unexpected"] = True
                else:
                    forged["decisions"][0]["disposition"] = "FATAL_INTEGRITY"
                    forged["decisions"][0]["reason_codes"] = [
                        "PROTECTED_INVENTORY_INVALID"
                    ]
                    forged["decisions"][0]["semantic_sha256"] = "a" * 64
                with self.subTest(mutation=mutation), self.assertRaises(
                    jsonschema.ValidationError
                ):
                    jsonschema.Draft202012Validator(schema).validate(forged)

            promoted = first.payload["decisions"][0]
            fatal = LegacyDecisionEvidence(
                decision=PromotionDecision(
                    query_id=promoted["candidate_query_id"],
                    disposition=PromotionDisposition.FATAL_INTEGRITY,
                    reason_codes=(PromotionReasonCode.CATALOG_NOT_UNIQUE,),
                    candidate=None,
                ),
                candidate_lineage=None,
                semantic_sha256=None,
            )
            with (
                mock.patch.object(
                    inventory_module,
                    "audit_legacy_candidate_evidence",
                    return_value=fatal,
                ),
                mock.patch.object(
                    socket,
                    "socket",
                    side_effect=AssertionError("network access is forbidden"),
                ) as network,
                self.assertRaises((RuntimeError, ValueError)),
            ):
                inventory_module.publish_legacy_decision_inventory(root)
            network.assert_not_called()

    def test_same_byte_authority_races_cleanup_owned_final_and_fail_closed(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            final = root / DECISION_PATH

            def replace_same_bytes(path: Path) -> None:
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)

            with (
                mock.patch.object(
                    inventory_module,
                    "_before_legacy_decision_inventory_authority_rebind",
                    side_effect=lambda: replace_same_bytes(inventory_path),
                ) as hook,
                self.assertRaises((RuntimeError, ValueError)),
            ):
                inventory_module.publish_legacy_decision_inventory(root)
            hook.assert_called_once_with()
            self.assertFalse(final.exists())

            published = inventory_module.publish_legacy_decision_inventory(root)
            original = published.path.read_bytes()
            published.path.write_bytes(original + b" ")
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.load_verified_legacy_decision_inventory(root)
            published.path.write_bytes(original)

            inventory = json.loads(inventory_path.read_bytes())
            raw_path = root / next(
                record["raw"]["path"]
                for record in inventory["records"]
                if record["source"] == "baseball_savant_statcast"
                and record["canonical_query"]["start"] == "2024-04-02"
            )
            with (
                mock.patch.object(
                    inventory_module,
                    "_before_legacy_decision_inventory_authority_rebind",
                    side_effect=lambda: replace_same_bytes(raw_path),
                ) as hook,
                self.assertRaises((RuntimeError, ValueError)),
            ):
                inventory_module.load_verified_legacy_decision_inventory(root)
            hook.assert_called_once_with()
            self.assertEqual(published.path.read_bytes(), original)

            alias = Path(directory) / "repository-alias"
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.load_verified_legacy_decision_inventory(alias)

    def test_root_batch_is_required_for_build_and_reverified_on_load(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        for mutation in ("build-missing", "load-deleted", "load-replaced"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, _inventory_path, _digest = _legacy_decision_repository(
                    Path(directory)
                )
                root_batch_path = _statcast_root_batch_path(root)
                if mutation == "build-missing":
                    root_batch_path.unlink()
                    with self.assertRaises((RuntimeError, ValueError)):
                        inventory_module.build_legacy_decision_inventory(root)
                    continue

                published = inventory_module.publish_legacy_decision_inventory(root)
                if mutation == "load-deleted":
                    root_batch_path.unlink()
                else:
                    replacement = root_batch_path.with_name("replacement.json")
                    replacement.write_bytes(b"{}\n")
                    os.replace(replacement, root_batch_path)
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.load_verified_legacy_decision_inventory(root)
                self.assertTrue(published.path.is_file())

    def test_root_batch_identity_race_fails_load_and_preserves_inventory(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            published = inventory_module.publish_legacy_decision_inventory(root)
            original = published.path.read_bytes()
            root_batch_path = _statcast_root_batch_path(root)

            def replace_root_batch() -> None:
                replacement = root_batch_path.with_name("same-bytes-replacement.json")
                replacement.write_bytes(root_batch_path.read_bytes())
                os.replace(replacement, root_batch_path)

            with (
                mock.patch.object(
                    inventory_module,
                    "_before_legacy_decision_inventory_authority_rebind",
                    side_effect=replace_root_batch,
                ) as hook,
                self.assertRaises((RuntimeError, ValueError)),
            ):
                inventory_module.load_verified_legacy_decision_inventory(root)
            hook.assert_called_once_with()
            self.assertEqual(published.path.read_bytes(), original)

    def test_postlink_winner_survives_authority_failure_without_private_temps(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            payload = inventory_module.build_legacy_decision_inventory(root)
            expected = _canonical_json(payload)
            final = root / DECISION_PATH
            root_batch_path = _statcast_root_batch_path(root)
            winner_identity: tuple[int, int] | None = None

            def replace_with_winner_then_break_authority() -> None:
                nonlocal winner_identity
                self.assertTrue(final.is_file(), "hook must run after final link")
                winner = final.with_name("concurrent-winner.json")
                winner.write_bytes(expected)
                os.replace(winner, final)
                metadata = final.stat()
                winner_identity = (metadata.st_dev, metadata.st_ino)
                root_batch_path.unlink()

            with (
                mock.patch.object(
                    inventory_module,
                    "_before_legacy_decision_inventory_authority_rebind",
                    side_effect=replace_with_winner_then_break_authority,
                ) as hook,
                self.assertRaises((AssertionError, RuntimeError, ValueError)),
            ):
                inventory_module.publish_legacy_decision_inventory(root)
            hook.assert_called_once_with()
            self.assertTrue(final.is_file())
            self.assertEqual(final.read_bytes(), expected)
            self.assertEqual(
                (final.stat().st_dev, final.stat().st_ino), winner_identity
            )
            self.assertEqual(list(final.parent.glob(f".{final.name}.*.tmp")), [])

    def test_acquisition_symlink_ancestor_is_rejected(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            external = Path(directory) / "external-acquisition"
            acquisition.rename(external)
            acquisition.symlink_to(external, target_is_directory=True)
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.build_legacy_decision_inventory(root)
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.publish_legacy_decision_inventory(root)

    def test_runtime_schema_and_assembled_payload_are_revalidated(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, _digest = _legacy_decision_repository(
                Path(directory)
            )
            schema_path = root / DECISION_SCHEMA_PATH
            original_schema = schema_path.read_bytes()
            schema = json.loads(schema_path.read_bytes())
            schema["additionalProperties"] = True
            schema_path.write_bytes(_canonical_json(schema))
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.build_legacy_decision_inventory(root)
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.publish_legacy_decision_inventory(root)
            schema_path.write_bytes(original_schema)
            published = inventory_module.publish_legacy_decision_inventory(root)
            schema_path.write_bytes(_canonical_json(schema))
            with self.assertRaises((RuntimeError, ValueError)):
                inventory_module.load_verified_legacy_decision_inventory(root)
            self.assertTrue(published.path.is_file())

        for mutation in ("count", "branch", "extra"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, _inventory_path, _digest = _legacy_decision_repository(
                    Path(directory)
                )
                original_assemble = inventory_module._assemble
                authority = original_assemble(root)
                forged = copy.deepcopy(_plain(authority.payload))
                if mutation == "count":
                    forged["counts"]["promoted_count"] += 1
                elif mutation == "branch":
                    promoted = next(
                        decision
                        for decision in forged["decisions"]
                        if decision["disposition"] == "PROMOTED"
                    )
                    promoted["candidate_lineage"] = None
                else:
                    forged["unexpected"] = True
                forged_authority = dataclasses.replace(
                    authority, payload=forged
                )
                with (
                    mock.patch.object(
                        inventory_module,
                        "_assemble",
                        return_value=forged_authority,
                    ),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    inventory_module.build_legacy_decision_inventory(root)
                with (
                    mock.patch.object(
                        inventory_module,
                        "_assemble",
                        return_value=forged_authority,
                    ),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    inventory_module.publish_legacy_decision_inventory(root)
                self.assertFalse((root / DECISION_PATH).exists())
                published = inventory_module.publish_legacy_decision_inventory(root)
                published.path.write_bytes(_canonical_json(forged))
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.load_verified_legacy_decision_inventory(root)


if __name__ == "__main__":
    unittest.main()
