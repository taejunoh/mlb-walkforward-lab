from __future__ import annotations

import copy
import contextlib
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


def _legacy_decision_repository(
    parent: Path, *, production_shaped: bool = False
) -> tuple[Path, Path, str]:
    """Build a synthetic local catalog with deliberately unbound authority.

    The checked-in historical contract remains immutable.  Callers exercising
    the public builder/publisher must opt into
    :func:`_verified_legacy_decision_authority` below.
    """
    root = _statcast_authority_fixture(parent)
    legacy_root = root / LEGACY_ROOT
    candidate_ranges = (
        (("2024-04-01", "2024-04-02"), ("2024-04-05", "2024-04-08"))
        if production_shaped
        else (("2024-04-02", "2024-04-02"),)
    )
    for start, end in candidate_ranges:
        RawStore(legacy_root).put(
            "baseball_savant_statcast",
            {
                "end": end,
                "game_type": "R",
                "start": start,
                "url": statcast_url(start, end),
            },
            STATCAST_RAW.replace(b"2024-04-02", start.encode()),
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
    decision_schema_path = root / DECISION_SCHEMA_PATH
    decision_schema_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / DECISION_SCHEMA_PATH, decision_schema_path)
    _git(root, "add", baseline.as_posix())
    _git(root, "add", DECISION_SCHEMA_PATH.as_posix())
    _git(root, "commit", "-qm", "add local protected legacy fixture")
    from mlb_backtest.v52_query_plan import (
        build_statcast_root_batch,
        publish_query_batch,
    )

    root_batch = build_statcast_root_batch(root)
    publish_query_batch(root, root_batch)
    return root, inventory_path, inventory_sha256


def _bind_synthetic_external_legacy_catalog(
    target: Path, parent: Path, *, bind: bool = True
) -> tuple[object | None, Path, str]:
    """Bind a descriptor-verified synthetic catalog to one authority root.

    This deliberately scopes synthetic bytes to the external protected parent
    capability: the target's frozen schema and acquisition contracts remain
    unchanged.  The target inventory snapshot is still bound so replacement,
    read, and rebind checks exercise the production capability path.
    """
    from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
    from mlb_backtest.v52_legacy import _snapshot
    from mlb_backtest.v52_protected_inventory import (
        build_protected_raw_inventory,
        parse_protected_raw_inventory_document,
    )

    physical = (parent / "physical").resolve()
    physical.mkdir()
    physical_raw = physical / LEGACY_ROOT
    for start, end in (("2024-04-01", "2024-04-02"), ("2024-04-05", "2024-04-08")):
        RawStore(physical_raw).put(
            "baseball_savant_statcast",
            {
                "end": end,
                "game_type": "R",
                "start": start,
                "url": statcast_url(start, end),
            },
            STATCAST_RAW.replace(b"2024-04-02", start.encode()),
        )
    RawStore(physical_raw).put(
        "baseball_savant_statcast",
        {
            "end": "2025-04-02",
            "game_type": "R",
            "start": "2025-04-02",
            "url": statcast_url("2025-04-02", "2025-04-02"),
        },
        STATCAST_RAW,
    )
    RawStore(physical_raw).put("mlb_schedule", _legacy_schedule_query(), _schedule_raw())
    inventory = build_protected_raw_inventory(physical, physical_raw)
    physical_inventory = physical / INVENTORY_PATH
    physical_inventory.parent.mkdir(parents=True, exist_ok=True)
    physical_inventory.write_bytes(_canonical_file(inventory))
    digest = hashlib.sha256(physical_inventory.read_bytes()).hexdigest()
    target_inventory = target / INVENTORY_PATH
    target_inventory.parent.mkdir(parents=True, exist_ok=True)
    target_inventory.write_bytes(physical_inventory.read_bytes())
    if not bind:
        return None, target_inventory, digest
    for name in (".mlb_stats_pitcher_bf.lock", ".mlb_stats_pitching.lock"):
        (physical_raw / name).write_bytes(b"")
    external = _bind_external_inventory(
        target,
        physical_raw,
        parse_protected_raw_inventory_document(physical_inventory.read_bytes()),
        digest,
        inventory_path=target_inventory,
        inventory_snapshot=_snapshot(target, target_inventory),
    )
    return external, target_inventory, digest


def _split_legacy_decision_capabilities(
    parent: Path,
) -> tuple[Path, object, object, Path, str]:
    """Build one target authority with local and split-root legacy readers."""
    from mlb_backtest.v52_external_protected_parent import _bind_external_inventory
    from mlb_backtest.v52_legacy import (
        _snapshot,
        adapt_root_local_legacy_catalog,
        load_protected_legacy_catalog,
    )
    from mlb_backtest.v52_protected_inventory import (
        parse_protected_raw_inventory_document,
    )

    target = _statcast_authority_fixture(parent / "target")
    baseline = Path("protocols/v5_2_qd/v51_protected_inventory.json")
    (target / baseline).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / baseline, target / baseline)
    decision_schema_path = target / DECISION_SCHEMA_PATH
    decision_schema_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / DECISION_SCHEMA_PATH, decision_schema_path)
    _git(target, "add", baseline.as_posix(), DECISION_SCHEMA_PATH.as_posix())
    _git(target, "commit", "-qm", "add local protected legacy fixture")
    from mlb_backtest.v52_query_plan import build_statcast_root_batch, publish_query_batch

    publish_query_batch(target, build_statcast_root_batch(target))
    _unused_external, target_inventory, digest = (
        _bind_synthetic_external_legacy_catalog(
            target, parent, bind=False
        )
    )
    physical = (parent / "physical").resolve()
    physical_raw = physical / LEGACY_ROOT
    physical_inventory = physical / INVENTORY_PATH
    local = adapt_root_local_legacy_catalog(
        load_protected_legacy_catalog(
            physical,
            legacy_root=physical_raw,
            protected_inventory_path=physical_inventory,
            protected_inventory_sha256=digest,
        )
    )
    for name in (".mlb_stats_pitcher_bf.lock", ".mlb_stats_pitching.lock"):
        (physical_raw / name).write_bytes(b"")
    external = _bind_external_inventory(
        target,
        physical_raw,
        parse_protected_raw_inventory_document(physical_inventory.read_bytes()),
        digest,
        inventory_path=target_inventory,
        inventory_snapshot=_snapshot(target, target_inventory),
    )
    return target, local, external, target_inventory, digest


@contextlib.contextmanager
def _verified_legacy_decision_authority(
    root: Path, protected_inventory_sha256: str
):
    """Inject a verified local inventory hash without mutating authority files.

    This is a test-only seam for synthetic raw fixtures.  The production
    contract, schema, and inventory files remain byte-identical on disk; only
    the contract snapshot consumed at the ``_assemble`` boundary is rebound in
    memory to the fixture's authenticated inventory bytes.
    """
    from mlb_backtest import v52_legacy_decision_inventory as inventory_module

    original_snapshot = inventory_module._json_snapshot
    contract_relatives = frozenset(
        {
            inventory_module._ACQUISITION_CONTRACT_RELATIVE,
            Path(
                "protocols/v5_2_qd/"
                "historical_source_acquisition_superseding_565534.json"
            ),
        }
    )

    def snapshot_override(snapshot_root: Path, path: Path):
        if snapshot_root == root:
            relative = path if not path.is_absolute() else path.relative_to(root)
            if relative in contract_relatives:
                contract, snapshot = original_snapshot(snapshot_root, path)
                rebound_contract = copy.deepcopy(contract)
                legacy = rebound_contract.get("legacy_protection")
                if not isinstance(legacy, dict):
                    raise AssertionError(
                        "synthetic authority contract lacks legacy protection"
                    )
                legacy["protected_inventory_sha256"] = protected_inventory_sha256
                return rebound_contract, dataclasses.replace(
                    snapshot,
                    contents=_canonical_json(rebound_contract),
                )
        return original_snapshot(snapshot_root, path)

    with mock.patch.object(
        inventory_module,
        "_json_snapshot",
        side_effect=snapshot_override,
    ):
        yield


@contextlib.contextmanager
def _inject_verified_legacy_decision_inventory(
    root: Path,
    verified_inventory: object,
):
    """Inject one already-verified inventory at the downstream loader seam.

    Downstream query-plan and acquisition tests exercise the public replay
    APIs, which load the inventory through this module.  This test-only seam
    supplies a ``VerifiedLegacyDecisionInventory`` that was authenticated by
    ``_verified_legacy_decision_authority`` without weakening the production
    loader's default trust rejection or changing any authority files.
    """
    from mlb_backtest import v52_legacy_decision_inventory as inventory_module

    if not isinstance(
        verified_inventory,
        inventory_module.VerifiedLegacyDecisionInventory,
    ):
        raise TypeError("verified_inventory must be a verified decision inventory")
    absolute_root = Path(os.path.abspath(os.fspath(root)))
    expected_inventory_path = absolute_root / DECISION_PATH
    if verified_inventory.path != expected_inventory_path:
        raise ValueError("verified decision inventory path differs")
    payload = verified_inventory.payload
    if not isinstance(payload, Mapping):
        raise TypeError("verified decision inventory payload must be a mapping")
    if hashlib.sha256(_canonical_json(payload)).hexdigest() != verified_inventory.sha256:
        raise ValueError("verified decision inventory hash differs")
    bindings = {
        "run_spec_path": (
            inventory_module._RUN_SPEC_RELATIVE,
            "run_spec",
        ),
        "acquisition_contract_path": (
            inventory_module._ACQUISITION_CONTRACT_RELATIVE,
            "acquisition_contract",
        ),
    }
    expected_paths: dict[str, Path] = {}
    for argument, (default_relative, payload_key) in bindings.items():
        binding = payload.get(payload_key)
        if not isinstance(binding, Mapping):
            raise ValueError(f"verified decision inventory {payload_key} binding differs")
        bound_path = binding.get("path")
        bound_sha256 = binding.get("sha256")
        if (
            not isinstance(bound_path, str)
            or not bound_path
            or Path(bound_path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(bound_path).parts)
            or not isinstance(bound_sha256, str)
            or len(bound_sha256) != 64
            or any(character not in "0123456789abcdef" for character in bound_sha256)
        ):
            raise ValueError(f"verified decision inventory {payload_key} binding differs")
        expected_paths[argument] = absolute_root / Path(bound_path)
        if expected_paths[argument] != absolute_root / default_relative:
            raise ValueError(f"verified decision inventory {payload_key} authority differs")
    original_loader = inventory_module.load_verified_legacy_decision_inventory

    def loader_override(repository_root: Path, *args: object, **kwargs: object):
        if Path(os.path.abspath(os.fspath(repository_root))) == absolute_root:
            if args:
                raise TypeError("legacy inventory loader authority arguments differ")
            requested = {}
            if set(kwargs) - set(bindings):
                raise TypeError("legacy inventory loader authority arguments differ")
            if set(requested) & set(kwargs):
                raise TypeError("legacy inventory loader authority arguments differ")
            requested.update(kwargs)
            for argument, expected in expected_paths.items():
                supplied = requested.get(argument)
                supplied_path = (
                    expected
                    if supplied is None
                    else Path(os.path.abspath(os.fspath(supplied)))
                )
                if supplied_path != expected:
                    raise ValueError(f"legacy inventory {argument} differs")
            return verified_inventory
        return original_loader(repository_root, *args, **kwargs)

    with mock.patch.object(
        inventory_module,
        "load_verified_legacy_decision_inventory",
        side_effect=loader_override,
    ):
        yield verified_inventory


def _statcast_root_batch_path(root: Path) -> Path:
    from mlb_backtest.v52_query_plan import build_statcast_root_batch

    batch = build_statcast_root_batch(root)
    return (
        root
        / "audit/v5_2_qd/historical/acquisition/query_batches"
        / f"{batch.batch_id}.json"
    )


def _superseding_legacy_decision_repository(
    parent: Path,
) -> tuple[Path, Path, Path, object, str]:
    """Build a small authenticated legacy fixture for source-pair tests.

    The checked-out repository intentionally does not carry the protected
    legacy raw directory.  Keep this test hermetic by using the existing
    synthetic protected catalog bound through the external parent capability.
    The source authority remains genuine: its frozen RunSpec, contract,
    schema, and supersedes metadata are unchanged.
    """
    root, _inventory_path, _digest = _legacy_decision_repository(parent)
    source_contract_path = root / (
        "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json"
    )
    external, _target_inventory, digest = _bind_synthetic_external_legacy_catalog(
        root, parent
    )
    if external is None:
        raise AssertionError("synthetic external capability was not bound")
    (
        root
        / "audit/v5_2_qd/historical_superseding_565534/acquisition/query_batches"
    ).mkdir(parents=True, exist_ok=True)
    return (
        root,
        root
        / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
        source_contract_path,
        external,
        digest,
    )


class V52LegacyDecisionInventoryTest(unittest.TestCase):
    def test_superseding_pair_authenticates_then_requires_source_batch(self) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            build_legacy_decision_inventory,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, source_run, source_contract, external, digest = (
                _superseding_legacy_decision_repository(Path(directory))
            )
            self.addCleanup(external.close)
            with self.assertRaisesRegex(
                ValueError, "query batch|ancestors|missing|source"
            ), _verified_legacy_decision_authority(root, digest):
                build_legacy_decision_inventory(
                    root,
                    run_spec_path=source_run,
                    acquisition_contract_path=source_contract,
                    legacy_evidence=external,
                )
            with self.assertRaises(ValueError):
                build_legacy_decision_inventory(
                    root,
                    run_spec_path=source_run,
                    acquisition_contract_path=root
                    / "protocols/v5_2_qd/historical_source_acquisition.json",
                )

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
        self.assertTrue(
            {
                "LegacyDecisionPreview",
                "preview_legacy_decisions",
            }.issubset(inventory_module.__all__)
        )

        import mlb_backtest.v52_legacy as legacy_module

        self.assertTrue(
            {
                "audit_legacy_candidates_evidence",
                "trusted_legacy_repository_root",
            }.issubset(legacy_module.__all__)
        )
        from mlb_backtest.v52_query_plan import (
            build_legacy_partition_batch_from_catalog,
        )

        self.assertTrue(callable(build_legacy_partition_batch_from_catalog))

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

    def test_unverified_synthetic_catalog_fails_closed_after_preview(self) -> None:
        """Synthetic preview bytes cannot impersonate the frozen authority."""
        from mlb_backtest.v52_legacy_decision_inventory import (
            build_legacy_decision_inventory,
            preview_legacy_decisions,
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            catalog = _load(
                root, root / LEGACY_ROOT, inventory_path, digest
            )
            preview = preview_legacy_decisions(
                build_statcast_root_batch(root),
                catalog,
                protected_inventory_sha256=digest,
            )
            self.assertEqual(
                preview.counts,
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
            decisions = preview.decisions
            self.assertEqual(len(decisions), 48)
            self.assertEqual(
                list(decisions),
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
            self.assertEqual(tuple(promoted[0]["reason_codes"]), ())
            self.assertIsNotNone(promoted[0]["candidate_lineage"])
            self.assertRegex(promoted[0]["semantic_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(all(item["reason_codes"] for item in fetched))
            self.assertTrue(
                all(item["candidate_lineage"] is None for item in fetched)
            )
            self.assertTrue(all(item["semantic_sha256"] is None for item in fetched))
            self.assertFalse((root / DECISION_PATH).exists())
            with self.assertRaisesRegex(ValueError, "protected inventory hash differs"):
                build_legacy_decision_inventory(root)
            with self.assertRaisesRegex(ValueError, "protected inventory hash differs"):
                publish_legacy_decision_inventory(root)
            self.assertFalse((root / DECISION_PATH).exists())

    def test_verified_inventory_loader_seam_binds_path_and_authority_pair(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            with _verified_legacy_decision_authority(root, digest):
                verified = inventory_module.publish_legacy_decision_inventory(root)
            with _inject_verified_legacy_decision_inventory(root, verified):
                loaded = inventory_module.load_verified_legacy_decision_inventory(
                    root,
                    run_spec_path=root
                    / "protocols/v5_2_qd/historical_walkforward_500.json",
                    acquisition_contract_path=root
                    / "protocols/v5_2_qd/historical_source_acquisition.json",
                )
            self.assertEqual(loaded, verified)

            other_root = Path(directory) / "other"
            with self.assertRaisesRegex(ValueError, "path differs"):
                with _inject_verified_legacy_decision_inventory(
                    other_root, verified
                ):
                    pass
            with _inject_verified_legacy_decision_inventory(root, verified):
                with self.assertRaisesRegex(ValueError, "run_spec_path differs"):
                    inventory_module.load_verified_legacy_decision_inventory(
                        root,
                        run_spec_path=root
                        / "protocols/v5_2_qd/historical_walkforward_500_superseding_565534.json",
                    )
                with self.assertRaisesRegex(
                    ValueError, "acquisition_contract_path differs"
                ):
                    inventory_module.load_verified_legacy_decision_inventory(
                        root,
                        acquisition_contract_path=root
                        / "protocols/v5_2_qd/historical_source_acquisition_superseding_565534.json",
                    )
                with self.assertRaisesRegex(TypeError, "authority arguments differ"):
                    inventory_module.load_verified_legacy_decision_inventory(
                        root,
                        root
                        / "protocols/v5_2_qd/historical_walkforward_500.json",
                    )

    def test_read_only_preview_is_exact_and_builder_uses_identical_decisions(
        self,
    ) -> None:
        import mlb_backtest.v52_legacy as legacy_module

        from mlb_backtest.v52_legacy_decision_inventory import (
            LegacyDecisionPreview,
            build_legacy_decision_inventory,
            preview_legacy_decisions,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch

        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(LegacyDecisionPreview)),
            ("partition_batch", "decisions", "counts"),
        )
        self.assertTrue(LegacyDecisionPreview.__dataclass_params__.frozen)
        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _legacy_decision_repository(Path(directory))
            catalog = _load(
                root, root / LEGACY_ROOT, inventory_path, digest
            )
            root_batch = build_statcast_root_batch(root)
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with (
                mock.patch.object(
                    socket,
                    "socket",
                    side_effect=AssertionError("network access is forbidden"),
                ) as network,
                mock.patch.object(
                    legacy_module,
                    "_revalidate_catalog",
                    wraps=legacy_module._revalidate_catalog,
                ) as revalidate,
            ):
                first = preview_legacy_decisions(
                    root_batch,
                    catalog,
                    protected_inventory_sha256=digest,
                )
                self.assertLessEqual(revalidate.call_count, 4)
                second = preview_legacy_decisions(
                    root_batch,
                    catalog,
                    protected_inventory_sha256=digest,
                )
            network.assert_not_called()
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(first, second)
            self.assertEqual(after, before)
            self.assertEqual(len(first.partition_batch.queries), 48)
            self.assertEqual(first.counts["promoted_count"], 1)
            self.assertEqual(first.counts["fetch_required_count"], 47)
            self.assertEqual(first.counts["fatal_integrity_count"], 0)
            with self.assertRaises(TypeError):
                first.counts["promoted_count"] = 2

            with _verified_legacy_decision_authority(root, digest):
                built = build_legacy_decision_inventory(root)
            self.assertEqual(built["decisions"], _plain(first.decisions))
            self.assertEqual(built["counts"], _plain(first.counts))

    def test_preview_selects_first_eligible_three_way_partition(self) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            preview_legacy_decisions,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch

        with tempfile.TemporaryDirectory() as directory:
            root, inventory_path, digest = _legacy_decision_repository(
                Path(directory), production_shaped=True
            )
            catalog = _load(
                root, root / LEGACY_ROOT, inventory_path, digest
            )
            preview = preview_legacy_decisions(
                build_statcast_root_batch(root),
                catalog,
                protected_inventory_sha256=digest,
            )
            self.assertEqual(len(preview.partition_batch.queries), 48)
            self.assertEqual(preview.counts["promoted_count"], 1)
            self.assertEqual(preview.counts["fetch_required_count"], 47)
            promoted = next(
                decision
                for decision in preview.decisions
                if decision["disposition"] == "PROMOTED"
            )
            self.assertEqual(
                dict(promoted["range"]),
                {
                    "year": 2024,
                    "start_date": "2024-04-05",
                    "end_date": "2024-04-08",
                },
            )

    def test_root_and_external_capabilities_have_equal_preview_and_inventory_bytes(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            build_legacy_decision_inventory,
            load_verified_legacy_decision_inventory,
            preview_legacy_decisions,
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch

        with tempfile.TemporaryDirectory() as directory:
            target, local, external, _inventory, digest = (
                _split_legacy_decision_capabilities(Path(directory))
            )
            self.addCleanup(external.close)
            root_batch = build_statcast_root_batch(target)
            with _verified_legacy_decision_authority(target, digest):
                external_preview = preview_legacy_decisions(
                    root_batch,
                    legacy_evidence=external,
                    protected_inventory_sha256=digest,
                )
                external_payload = build_legacy_decision_inventory(
                    target, legacy_evidence=external
                )
                external_published = publish_legacy_decision_inventory(
                    target, legacy_evidence=external
                )
                external_loaded = load_verified_legacy_decision_inventory(
                    target, legacy_evidence=external
                )
            external.close()
            extra_locks = (".mlb_stats_pitcher_bf.lock", ".mlb_stats_pitching.lock")
            physical_raw = Path(directory) / "physical" / LEGACY_ROOT
            for name in extra_locks:
                (physical_raw / name).unlink()
            with _verified_legacy_decision_authority(target, digest):
                local_preview = preview_legacy_decisions(
                    root_batch, legacy_evidence=local, protected_inventory_sha256=digest
                )
                local_payload = build_legacy_decision_inventory(
                    target, legacy_evidence=local
                )
                local_published = publish_legacy_decision_inventory(
                    target, legacy_evidence=local
                )
                local_loaded = load_verified_legacy_decision_inventory(
                    target, legacy_evidence=local
                )
            self.assertEqual(local_preview, external_preview)
            self.assertEqual(_plain(local_payload), _plain(external_payload))
            self.assertEqual(local_published, external_published)
            self.assertEqual(local_loaded, external_loaded)

    def test_external_parent_or_target_inventory_swap_after_preview_blocks_publish(
        self,
    ) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            preview_legacy_decisions,
            publish_legacy_decision_inventory,
        )
        from mlb_backtest.v52_query_plan import build_statcast_root_batch

        for mutation in ("parent", "target"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                target, _local, external, target_inventory, digest = (
                    _split_legacy_decision_capabilities(Path(directory))
                )
                self.addCleanup(external.close)
                root_batch = build_statcast_root_batch(target)
                with _verified_legacy_decision_authority(target, digest):
                    preview_legacy_decisions(
                        root_batch,
                        legacy_evidence=external,
                        protected_inventory_sha256=digest,
                    )
                if mutation == "parent":
                    inventory = json.loads(target_inventory.read_bytes())
                    raw_path = Path(directory) / "physical" / next(
                        record["raw"]["path"]
                        for record in inventory["records"]
                        if record["source"] == "baseball_savant_statcast"
                    )
                    replacement = raw_path.with_name(raw_path.name + ".replacement")
                    replacement.write_bytes(raw_path.read_bytes())
                    os.replace(replacement, raw_path)
                else:
                    replacement = target_inventory.with_name("replacement.json")
                    replacement.write_bytes(target_inventory.read_bytes())
                    os.replace(replacement, target_inventory)
                with _verified_legacy_decision_authority(target, digest):
                    with self.assertRaises((RuntimeError, ValueError)):
                        publish_legacy_decision_inventory(
                            target, legacy_evidence=external
                        )
                self.assertFalse(
                    (target / DECISION_PATH).exists(),
                    f"{mutation} mutation must not publish a decision inventory",
                )
                self.assertFalse(
                    list(
                        (target / DECISION_PATH).parent.glob(
                            f".{DECISION_PATH.name}.*.tmp"
                        )
                    )
                )

    def test_prepublication_rebind_rejects_protected_leaf_replacement(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target, _local, external, target_inventory, digest = (
                _split_legacy_decision_capabilities(base)
            )
            self.addCleanup(external.close)
            inventory = json.loads(target_inventory.read_bytes())
            raw_path = base / "physical" / next(
                record["raw"]["path"]
                for record in inventory["records"]
                if record["source"] == "baseball_savant_statcast"
            )

            def replace_protected_leaf() -> None:
                replacement = raw_path.with_name(raw_path.name + ".replacement")
                replacement.write_bytes(raw_path.read_bytes())
                os.replace(replacement, raw_path)

            with _verified_legacy_decision_authority(target, digest):
                with (
                    mock.patch.object(
                        inventory_module,
                        "_before_legacy_decision_inventory_capability_rebind",
                        side_effect=replace_protected_leaf,
                    ) as hook,
                    self.assertRaises(RuntimeError),
                ):
                    inventory_module.publish_legacy_decision_inventory(
                        target, legacy_evidence=external
                    )
            hook.assert_called_once_with()
            final = target / DECISION_PATH
            self.assertFalse(final.exists())
            self.assertEqual(list(final.parent.glob(f".{final.name}.*.tmp")), [])

    def test_prepublication_rebind_rejects_target_inventory_replacement(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            target, _local, external, target_inventory, digest = (
                _split_legacy_decision_capabilities(Path(directory))
            )
            self.addCleanup(external.close)

            def replace_target_inventory() -> None:
                replacement = target_inventory.with_name("replacement.json")
                replacement.write_bytes(target_inventory.read_bytes())
                os.replace(replacement, target_inventory)

            with _verified_legacy_decision_authority(target, digest):
                with (
                    mock.patch.object(
                        inventory_module,
                        "_before_legacy_decision_inventory_capability_rebind",
                        side_effect=replace_target_inventory,
                    ) as hook,
                    self.assertRaises(RuntimeError),
                ):
                    inventory_module.publish_legacy_decision_inventory(
                        target, legacy_evidence=external
                    )
            hook.assert_called_once_with()
            final = target / DECISION_PATH
            self.assertFalse(final.exists())
            self.assertEqual(list(final.parent.glob(f".{final.name}.*.tmp")), [])

    def test_exact_r3_rejects_issued_root_local_legacy_evidence(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        from tests.test_v52_query_plan import (
            R3_RECOVERY_CONTRACT,
            R3_RECOVERY_RUN_SPEC,
            _r3_authority_fixture,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            r3_root = _r3_authority_fixture(
                base / "r3",
                additional_files=(DECISION_SCHEMA_PATH, INVENTORY_PATH),
            )
            local_root, local_inventory, local_digest = (
                _legacy_decision_repository(base / "local")
            )
            local = _load(
                local_root,
                local_root / LEGACY_ROOT,
                local_inventory,
                local_digest,
            )
            run_spec_path = r3_root / R3_RECOVERY_RUN_SPEC.relative_to(ROOT)
            contract_path = r3_root / R3_RECOVERY_CONTRACT.relative_to(ROOT)
            final = r3_root / (
                "audit/v5_2_qd/"
                "historical_recovery_schedule_conflicts_missing_venue_r3/"
                "acquisition/legacy_decision_inventory.json"
            )

            for operation in (
                inventory_module.build_legacy_decision_inventory,
                inventory_module.publish_legacy_decision_inventory,
            ):
                with (
                    self.subTest(operation=operation.__name__),
                    self.assertRaisesRegex(TypeError, "external"),
                ):
                    operation(
                        r3_root,
                        run_spec_path=run_spec_path,
                        acquisition_contract_path=contract_path,
                        legacy_evidence=local,
                    )
                self.assertFalse(final.exists())

    def test_publish_load_schema_idempotence_and_fatal_refusal_are_closed(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        from mlb_backtest.v52_legacy import (
            LegacyDecisionEvidence,
            PromotionDecision,
            PromotionDisposition,
            PromotionReasonCode,
        )

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            with _verified_legacy_decision_authority(root, digest):
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
            with _verified_legacy_decision_authority(root, digest):
                with (
                    mock.patch.object(
                        inventory_module,
                        "audit_legacy_candidates_evidence",
                        side_effect=lambda queries, _catalog: tuple(
                            fatal for _query in queries
                        ),
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
            root, inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            final = root / DECISION_PATH

            def replace_same_bytes(path: Path) -> None:
                replacement = path.with_name(path.name + ".replacement")
                replacement.write_bytes(path.read_bytes())
                os.replace(replacement, path)

            with _verified_legacy_decision_authority(root, digest):
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

            with _verified_legacy_decision_authority(root, digest):
                published = inventory_module.publish_legacy_decision_inventory(root)
            original = published.path.read_bytes()
            published.path.write_bytes(original + b" ")
            with _verified_legacy_decision_authority(root, digest):
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
            with _verified_legacy_decision_authority(root, digest):
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
                root, _inventory_path, digest = _legacy_decision_repository(
                    Path(directory)
                )
                root_batch_path = _statcast_root_batch_path(root)
                if mutation == "build-missing":
                    root_batch_path.unlink()
                    with _verified_legacy_decision_authority(root, digest):
                        with self.assertRaises((RuntimeError, ValueError)):
                            inventory_module.build_legacy_decision_inventory(root)
                    continue

                with _verified_legacy_decision_authority(root, digest):
                    published = inventory_module.publish_legacy_decision_inventory(root)
                if mutation == "load-deleted":
                    root_batch_path.unlink()
                else:
                    replacement = root_batch_path.with_name("replacement.json")
                    replacement.write_bytes(b"{}\n")
                    os.replace(replacement, root_batch_path)
                with _verified_legacy_decision_authority(root, digest):
                    with self.assertRaises((RuntimeError, ValueError)):
                        inventory_module.load_verified_legacy_decision_inventory(root)
                self.assertTrue(published.path.is_file())

    def test_root_batch_identity_race_fails_load_and_preserves_inventory(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            with _verified_legacy_decision_authority(root, digest):
                published = inventory_module.publish_legacy_decision_inventory(root)
            original = published.path.read_bytes()
            root_batch_path = _statcast_root_batch_path(root)

            def replace_root_batch() -> None:
                replacement = root_batch_path.with_name("same-bytes-replacement.json")
                replacement.write_bytes(root_batch_path.read_bytes())
                os.replace(replacement, root_batch_path)

            with _verified_legacy_decision_authority(root, digest):
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
            root, _inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            with _verified_legacy_decision_authority(root, digest):
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

            with _verified_legacy_decision_authority(root, digest):
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
            root, _inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            acquisition = root / "audit/v5_2_qd/historical/acquisition"
            external = Path(directory) / "external-acquisition"
            acquisition.rename(external)
            acquisition.symlink_to(external, target_is_directory=True)
            with _verified_legacy_decision_authority(root, digest):
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.build_legacy_decision_inventory(root)
            with _verified_legacy_decision_authority(root, digest):
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.publish_legacy_decision_inventory(root)

    def test_runtime_schema_and_assembled_payload_are_revalidated(self) -> None:
        import mlb_backtest.v52_legacy_decision_inventory as inventory_module

        with tempfile.TemporaryDirectory() as directory:
            root, _inventory_path, digest = _legacy_decision_repository(
                Path(directory)
            )
            schema_path = root / DECISION_SCHEMA_PATH
            original_schema = schema_path.read_bytes()
            schema = json.loads(schema_path.read_bytes())
            schema["additionalProperties"] = True
            schema_path.write_bytes(_canonical_json(schema))
            with _verified_legacy_decision_authority(root, digest):
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.build_legacy_decision_inventory(root)
            with _verified_legacy_decision_authority(root, digest):
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.publish_legacy_decision_inventory(root)
            schema_path.write_bytes(original_schema)
            with _verified_legacy_decision_authority(root, digest):
                published = inventory_module.publish_legacy_decision_inventory(root)
            schema_path.write_bytes(_canonical_json(schema))
            with _verified_legacy_decision_authority(root, digest):
                with self.assertRaises((RuntimeError, ValueError)):
                    inventory_module.load_verified_legacy_decision_inventory(root)
            self.assertTrue(published.path.is_file())

        for mutation in ("count", "branch", "extra"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                root, _inventory_path, digest = _legacy_decision_repository(
                    Path(directory)
                )
                with _verified_legacy_decision_authority(root, digest):
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
                with _verified_legacy_decision_authority(root, digest):
                    published = inventory_module.publish_legacy_decision_inventory(root)
                published.path.write_bytes(_canonical_json(forged))
                with _verified_legacy_decision_authority(root, digest):
                    with self.assertRaises((RuntimeError, ValueError)):
                        inventory_module.load_verified_legacy_decision_inventory(root)

class SourceLegacyDecisionAuthorityTest(unittest.TestCase):
    def test_source_partition_stays_in_source_authority_namespace(self) -> None:
        from mlb_backtest.v52_legacy_decision_inventory import (
            preview_legacy_decisions,
        )
        from mlb_backtest.v52_query_plan import (
            _batch_root_for_batch,
            build_statcast_root_batch_for_authority,
            publish_query_batch,
        )
        from tests.test_v52_schedule_inventory import (
            ROOT as SCHEDULE_ROOT,
            SOURCE_RUN_SPEC_PATH,
            _local_disposition_fixture_boundary,
            _source_registry_fixture,
        )

        with tempfile.TemporaryDirectory() as directory, _local_disposition_fixture_boundary():
            base = Path(directory)
            fixture = _source_registry_fixture(base / "source")
            legacy_root, inventory_path, inventory_sha256 = (
                _legacy_decision_repository(base / "legacy")
            )
            catalog = _load(
                legacy_root,
                legacy_root / LEGACY_ROOT,
                inventory_path,
                inventory_sha256,
            )
            run_path = fixture.root / SOURCE_RUN_SPEC_PATH.relative_to(SCHEDULE_ROOT)
            root_batch = build_statcast_root_batch_for_authority(
                fixture.root,
                run_spec_path=run_path,
                acquisition_contract_path=fixture.contract_path,
            )
            publish_query_batch(fixture.root, root_batch)
            preview = preview_legacy_decisions(
                root_batch,
                catalog,
                protected_inventory_sha256=inventory_sha256,
            )
            partition = preview.partition_batch
            selected_root = _batch_root_for_batch(fixture.root, partition)

            self.assertEqual(
                selected_root,
                Path(
                    "audit/v5_2_qd/historical_superseding_565534/"
                    "acquisition/query_batches"
                ),
            )
            self.assertFalse(
                (
                    fixture.root
                    / "audit/v5_2_qd/historical/acquisition/query_batches"
                    / f"{partition.batch_id}.json"
                ).exists()
            )
            self.assertEqual(partition.derived_from_sha256, inventory_sha256)
            self.assertEqual(len(partition.queries), 48)
            self.assertTrue(
                all(
                    query.run_spec_sha256
                    == hashlib.sha256(run_path.read_bytes()).hexdigest()
                    for query in partition.queries
                )
            )


if __name__ == "__main__":
    unittest.main()
