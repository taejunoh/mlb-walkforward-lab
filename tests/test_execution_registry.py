from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import jsonschema
from jsonschema import FormatChecker

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.protocols import load_run_spec


FORMAT_CHECKER = FormatChecker()


def _valid_date_time(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError:
        return False
    return True


FORMAT_CHECKER.checkers = {
    **FORMAT_CHECKER.checkers,
    "date-time": (_valid_date_time, ()),
}


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
)
PLAN_RELATIVE = Path(
    "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md"
)
AMENDMENT_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-14-mlb-v5-2-plan-a-contract-amendment.md"
)
CONTRACT_RELATIVE = Path("protocols/v5_2_qd/historical_source_acquisition.json")
ACQUISITION_SCHEMA_RELATIVE = Path("schemas/v5_2_acquisition_contract.schema.json")
BASELINE_RELATIVE = Path("protocols/v5_2_qd/v51_protected_inventory.json")
PROTECTED_RELATIVE = Path("audit/v5_1_bt/protected_raw_inventory.json")
SOURCE_INVENTORY_RELATIVE = Path(
    "data/v5_2_qd/historical/sources/source_inventory.json"
)
RECEIPTS_RELATIVE = Path(
    "audit/v5_2_qd/historical/execution/phase_receipts"
)
BOUND_EXECUTION_CODE = (
    Path("mlb_backtest/execution_registry.py"),
    Path("mlb_backtest/verify_v52_design.py"),
    Path("schemas/v5_2_acquisition_checkpoint.schema.json"),
    Path("schemas/v5_2_execution_receipt.schema.json"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class ExecutionRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        if importlib.util.find_spec("mlb_backtest.execution_registry") is None:
            self.fail("mlb_backtest.execution_registry is not implemented")
        from mlb_backtest import execution_registry

        self.registry = execution_registry

    def make_repo(self, parent: Path) -> tuple[Path, object]:
        root = (parent / "repo").resolve()
        for relative in (
            RUN_SPEC_RELATIVE,
            DESIGN_RELATIVE,
            PLAN_RELATIVE,
            AMENDMENT_RELATIVE,
            CONTRACT_RELATIVE,
            ACQUISITION_SCHEMA_RELATIVE,
            BASELINE_RELATIVE,
            PROTECTED_RELATIVE,
            *BOUND_EXECUTION_CODE,
        ):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / RUN_SPEC_RELATIVE, root / RUN_SPEC_RELATIVE)
        shutil.copy2(ROOT / DESIGN_RELATIVE, root / DESIGN_RELATIVE)
        shutil.copy2(ROOT / PLAN_RELATIVE, root / PLAN_RELATIVE)
        shutil.copy2(ROOT / AMENDMENT_RELATIVE, root / AMENDMENT_RELATIVE)
        shutil.copy2(
            ROOT / ACQUISITION_SCHEMA_RELATIVE,
            root / ACQUISITION_SCHEMA_RELATIVE,
        )
        shutil.copy2(ROOT / BASELINE_RELATIVE, root / BASELINE_RELATIVE)
        shutil.copy2(ROOT / PROTECTED_RELATIVE, root / PROTECTED_RELATIVE)
        for relative in BOUND_EXECUTION_CODE:
            shutil.copy2(ROOT / relative, root / relative)
        (root / "unrelated.txt").write_text("clean\n", encoding="utf-8")
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "registry@example.test")
        _git(root, "config", "user.name", "Registry Test")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "fixture inputs")
        template = json.loads((ROOT / CONTRACT_RELATIVE).read_text(encoding="utf-8"))
        template["bindings"] = {
            "approved_design": {
                "path": DESIGN_RELATIVE.as_posix(),
                "sha256": _sha256(root / DESIGN_RELATIVE),
            },
            "implementation_plan": {
                "git_blob_id": _git(root, "rev-parse", f"HEAD:{PLAN_RELATIVE}"),
                "path": PLAN_RELATIVE.as_posix(),
                "working_tree_sha256": _sha256(root / PLAN_RELATIVE),
            },
            "plan_a_amendment": {
                "path": AMENDMENT_RELATIVE.as_posix(),
                "sha256": _sha256(root / AMENDMENT_RELATIVE),
            },
            "run_spec": {
                "path": RUN_SPEC_RELATIVE.as_posix(),
                "sha256": _sha256(root / RUN_SPEC_RELATIVE),
            },
        }
        template["legacy_protection"].update(
            {
                "baseline_inventory_path": BASELINE_RELATIVE.as_posix(),
                "baseline_inventory_sha256": _sha256(root / BASELINE_RELATIVE),
                "protected_inventory_path": PROTECTED_RELATIVE.as_posix(),
                "protected_inventory_sha256": _sha256(root / PROTECTED_RELATIVE),
            }
        )
        (root / CONTRACT_RELATIVE).write_bytes(canonical_json_bytes(template) + b"\n")
        _git(root, "add", CONTRACT_RELATIVE.as_posix())
        _git(root, "commit", "-qm", "fixture contract")
        return root, load_run_spec(root / RUN_SPEC_RELATIVE)

    def initialize(self, root: Path, run_spec: object):
        return self.registry.initialize_execution_registry(
            root,
            run_spec,
            design_path=root / DESIGN_RELATIVE,
            plan_path=root / PLAN_RELATIVE,
            acquisition_contract_path=root / CONTRACT_RELATIVE,
        )

    def write_artifact(self, root: Path, relative: str, value: object) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes(value) + b"\n")
        return path

    def write_real_source_evidence(
        self,
        root: Path,
        *,
        terminal_checkpoint: bool = True,
    ) -> tuple[Path, tuple[Path, Path, Path]]:
        snapshots = ["a" * 64, "b" * 64]
        reviewed_query_ids = ["query-001", "query-002"]
        inventory = self.write_artifact(
            root,
            SOURCE_INVENTORY_RELATIVE.as_posix(),
            {
                "critical_errors": [],
                "logical_query_ids": reviewed_query_ids,
                "network_attempts": 0,
                "run_id": "historical_walkforward_500",
                "schema_version": 1,
                "snapshot_sha256s": snapshots,
                "source_gate_ready": True,
                "status": "READY_CANDIDATE",
            },
        )
        common = {
            "critical_errors": [],
            "inventory_sha256": _sha256(inventory),
            "reviewed_query_ids": reviewed_query_ids,
            "run_id": "historical_walkforward_500",
            "schema_version": 1,
            "snapshot_sha256s": snapshots,
        }
        review = self.write_artifact(
            root,
            "audit/v5_2_qd/historical/acquisition/source_reviews/review.json",
            {
                **common,
                "decision": "APPROVED",
                "receipt_kind": "SOURCE_REVIEW",
                "reviewer_identity": "independent-source-auditor",
            },
        )
        replays = tuple(
            self.write_artifact(
                root,
                f"audit/v5_2_qd/historical/acquisition/source_replays/replay-{index}.json",
                {
                    **common,
                    "network_attempts": 0,
                    "recorded_at_utc": f"2026-07-13T00:00:0{index}.000000Z",
                    "receipt_kind": "CLEAN_SOURCE_REPLAY",
                    "status": "PASS",
                },
            )
            for index in (1, 2)
        )
        if terminal_checkpoint:
            self.install_terminal_checkpoint_stub(root)
        return inventory, (review, *replays)

    def install_terminal_checkpoint_stub(self, root: Path) -> None:
        checkpoint_dir = (
            root
            / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
        )
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        contents = canonical_json_bytes({"fixture": "terminal-checkpoint"}) + b"\n"
        digest = hashlib.sha256(contents).hexdigest()
        name = f"00000000-{digest}.json"
        (checkpoint_dir / name).write_bytes(contents)
        checkpoint = self.registry.AcquisitionCheckpoint(
            sequence=0,
            receipt_sha256=digest,
            prior_checkpoint_sha256=None,
            stage="REMAINING",
            recorded_at_utc="2026-07-14T00:00:00.000000Z",
            git_head="a" * 40,
            execution_receipt_sha256="b" * 64,
            query_batch_ids=(),
            query_batch_sha256s=(),
            query_evidence_receipt_sha256s=(),
            stage_evidence_receipt_sha256s=(),
            split_receipt_sha256s=(),
            completed_query_ids=(),
            pending_query_ids=(),
            quarantined_query_ids=(),
            split_query_ids=(),
        )
        payload = {
            "pending_query_ids": [],
            "quarantined_query_ids": [],
        }
        patcher = patch.object(
            self.registry,
            "_load_checkpoints_locked",
            return_value=((checkpoint,), (payload,), (name,)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def replace_receipt_payload(self, receipt: Path, payload: object) -> Path:
        contents = canonical_json_bytes(payload) + b"\n"
        digest = hashlib.sha256(contents).hexdigest()
        prefix = receipt.name.rsplit("-", 1)[0]
        replacement = receipt.with_name(f"{prefix}-{digest}.json")
        receipt.unlink()
        replacement.write_bytes(contents)
        return replacement

    def checkpoint_fixture(self, root: Path, run_spec: object):
        from mlb_backtest.v52_evidence import logical_query_id
        from mlb_backtest.v52_query_plan import (
            build_static_batch,
            publish_query_batch,
        )

        self.initialize(root, run_spec)
        self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
        batch = build_static_batch(root / RUN_SPEC_RELATIVE, root / CONTRACT_RELATIVE)
        publish_query_batch(root, batch)
        query_ids = tuple(
            sorted(logical_query_id(query) for query in batch.queries)
        )
        return batch, query_ids

    def publish_pending_checkpoint(
        self,
        root: Path,
        run_spec: object,
        batch: object,
        query_ids: tuple[str, ...],
        *,
        expected_prior: str | None,
    ):
        return self.registry.publish_acquisition_checkpoint(
            root,
            run_spec,
            expected_prior_checkpoint_sha256=expected_prior,
            stage="STATIC_SCHEDULES",
            query_batch_ids=(batch.batch_id,),
            completed_query_ids=(),
            pending_query_ids=query_ids,
            quarantined_query_ids=(),
            split_query_ids=(),
        )

    def test_acquisition_checkpoint_schema_and_public_api_are_closed(self) -> None:
        self.assertTrue(hasattr(self.registry, "AcquisitionCheckpoint"))
        self.assertTrue(hasattr(self.registry, "publish_acquisition_checkpoint"))
        self.assertTrue(hasattr(self.registry, "load_latest_acquisition_checkpoint"))
        schema_path = ROOT / "schemas/v5_2_acquisition_checkpoint.schema.json"
        schema = json.loads(schema_path.read_bytes())
        jsonschema.Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertEqual(
            schema_path.read_bytes(), canonical_json_bytes(schema) + b"\n"
        )

    def test_acquisition_checkpoint_genesis_chain_latest_and_idempotent_retry(
        self,
    ) -> None:
        from mlb_backtest.v52_evidence import AttemptCapture, V52EvidenceStore

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            first = self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            self.assertEqual(first.sequence, 0)
            self.assertIsNone(first.prior_checkpoint_sha256)
            self.assertEqual(first.stage, "STATIC_SCHEDULES")
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                first,
            )
            self.assertEqual(
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                ),
                first,
            )

            raw_root = root / run_spec.raw["namespaces"]["raw"]
            raw_root.parent.mkdir(parents=True, exist_ok=True)
            store = V52EvidenceStore(raw_root)
            query = batch.queries[0]
            with store.acquire_query(query) as lease:
                number = store.next_attempt_number(lease)
                store.record_attempt(
                    query,
                    number,
                    AttemptCapture(
                        started_at_utc="2026-07-14T00:00:00.000000Z",
                        ended_at_utc="2026-07-14T00:00:01.000000Z",
                        final_url=None,
                        http_status=None,
                        response_headers=(),
                        body=None,
                        error_classification="TIMEOUT",
                    ),
                    outcome="TRANSIENT_NETWORK",
                )
            second = self.publish_pending_checkpoint(
                root,
                run_spec,
                batch,
                query_ids,
                expected_prior=first.receipt_sha256,
            )
            self.assertEqual(second.sequence, 1)
            self.assertEqual(second.prior_checkpoint_sha256, first.receipt_sha256)
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                second,
            )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(len(list(checkpoint_dir.iterdir())), 2)

    def test_acquisition_checkpoint_rejects_partition_stage_and_stale_fork(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            with self.assertRaisesRegex(ValueError, "partition|status|query"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(query_ids[0],),
                    pending_query_ids=query_ids[1:],
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )
            with self.assertRaisesRegex(ValueError, "stage"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="TIMEZONE_REVIEW",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(),
                    pending_query_ids=query_ids,
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )

            first = self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            with self.assertRaisesRegex(ValueError, "stale|fork|prior|replay"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256="f" * 64,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(),
                    pending_query_ids=query_ids,
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )
            self.assertEqual(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                first,
            )

    def test_acquisition_checkpoint_rejects_gap_tamper_and_unknown_membership(
        self,
    ) -> None:
        for attack in ("gap", "tamper", "unknown"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                batch, query_ids = self.checkpoint_fixture(root, run_spec)
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                )
                checkpoint_dir = (
                    root
                    / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                )
                receipt = next(checkpoint_dir.iterdir())
                if attack == "gap":
                    receipt.rename(receipt.with_name("00000002-" + receipt.name[9:]))
                elif attack == "tamper":
                    receipt.write_bytes(receipt.read_bytes() + b" ")
                else:
                    (checkpoint_dir / "unknown.tmp").write_bytes(b"keep")
                with self.assertRaisesRegex(
                    (ValueError, RuntimeError), "checkpoint|gap|fork|unknown|hash"
                ):
                    self.registry.load_latest_acquisition_checkpoint(root, run_spec)

    def test_acquisition_checkpoint_partial_write_cleans_and_retry_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            original_write = self.registry.os.write
            writes = 0

            def interrupted_write(descriptor: int, data: object) -> int:
                nonlocal writes
                writes += 1
                if writes == 1:
                    return original_write(descriptor, bytes(data)[:11])
                raise OSError("injected checkpoint interruption")

            with (
                patch.object(
                    self.registry.os, "write", side_effect=interrupted_write
                ),
                self.assertRaisesRegex(ValueError, "published|publication|receipt"),
            ):
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(list(checkpoint_dir.iterdir()), [])
            self.assertEqual(
                self.publish_pending_checkpoint(
                    root, run_spec, batch, query_ids, expected_prior=None
                ).sequence,
                0,
            )

    def test_acquisition_checkpoint_concurrent_identical_publish_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def publish() -> None:
                try:
                    barrier.wait(timeout=5)
                    results.append(
                        self.publish_pending_checkpoint(
                            root,
                            run_spec,
                            batch,
                            query_ids,
                            expected_prior=None,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=publish) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(results[0], results[1])
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(len(list(checkpoint_dir.iterdir())), 1)

    def test_acquisition_checkpoint_requires_acquiring_and_documents_tail_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            with self.assertRaisesRegex(ValueError, "ACQUIRING"):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(),
                    completed_query_ids=(),
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            next(checkpoint_dir.iterdir()).unlink()
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec),
                "a tail deleted before read is outside local detection until a later "
                "immutable checkpoint, phase receipt, or completion receipt binds it",
            )

    def test_acquisition_checkpoint_stage_advance_requires_stage_work(self) -> None:
        batch = {
            "batch_id": "a" * 64,
            "path": "audit/static.json",
            "sha256": "b" * 64,
            "stage": "STATIC_SCHEDULES",
        }
        previous = {
            "stage": "STATIC_SCHEDULES",
            "pending_query_ids": [],
            "quarantined_query_ids": [],
            "query_batches": [batch],
            "query_evidence_receipts": [],
            "stage_evidence_receipts": [],
            "split_receipts": [],
            "completed_query_ids": ["c" * 64],
            "split_query_ids": [],
            "recorded_at_utc": "2026-07-14T00:00:00.000000Z",
        }
        reference = {
            **previous,
            "stage": "REFERENCE_INPUTS",
            "recorded_at_utc": "2026-07-14T00:00:01.000000Z",
        }
        with self.assertRaisesRegex(ValueError, "REFERENCE_INPUTS|batch|stage"):
            self.registry._validate_checkpoint_progression(previous, reference)

        reference_batch = {**batch, "batch_id": "d" * 64, "stage": "REFERENCE_INPUTS"}
        reference = {**reference, "query_batches": [batch, reference_batch]}
        timezone = {
            **reference,
            "stage": "TIMEZONE_REVIEW",
            "recorded_at_utc": "2026-07-14T00:00:02.000000Z",
        }
        with self.assertRaisesRegex(ValueError, "TIMEZONE_REVIEW|evidence|gate"):
            self.registry._validate_checkpoint_progression(reference, timezone)

    def test_checkpoint_runtime_rejects_duplicate_binding_arrays(self) -> None:
        binding = {"path": "audit/evidence.json", "sha256": "a" * 64}
        payload = {
            "acquisition_contract": binding,
            "completed_query_ids": [],
            "execution_phase": "ACQUIRING",
            "execution_phase_receipt": binding,
            "git_head": "b" * 40,
            "pending_query_ids": ["c" * 64],
            "prior_checkpoint_sha256": None,
            "quarantined_query_ids": [],
            "query_batches": [
                {
                    "batch_id": "d" * 64,
                    "path": "audit/batch.json",
                    "sha256": "e" * 64,
                    "stage": "STATIC_SCHEDULES",
                }
            ],
            "query_evidence_receipts": [],
            "receipt_kind": "ACQUISITION_CHECKPOINT",
            "recorded_at_utc": "2026-07-14T00:00:00.000000Z",
            "run_id": "historical_walkforward_500",
            "run_spec": binding,
            "schema_version": 1,
            "sequence": 0,
            "split_query_ids": [],
            "split_receipts": [],
            "stage": "STATIC_SCHEDULES",
            "stage_evidence_receipts": [],
        }
        duplicates = {
            "query_batches": payload["query_batches"][0],
            "query_evidence_receipts": {
                "path": "audit/attempt.json",
                "query_id": "c" * 64,
                "receipt_kind": "ATTEMPT",
                "sha256": "f" * 64,
            },
            "stage_evidence_receipts": binding,
            "split_receipts": {
                "parent_query_id": "c" * 64,
                "path": "audit/split.json",
                "sha256": "1" * 64,
                "split_receipt_id": "2" * 64,
            },
        }
        for key, item in duplicates.items():
            with self.subTest(key=key):
                forged = json.loads(json.dumps(payload))
                forged[key] = [item, item]
                with self.assertRaisesRegex(ValueError, "duplicate|unique|order"):
                    self.registry._validate_checkpoint_payload(forged)

    def test_checkpoint_split_requires_canonical_verified_split_graph(self) -> None:
        from mlb_backtest import v52_query_plan

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint = self.publish_pending_checkpoint(
                root, run_spec, batch, query_ids, expected_prior=None
            )
            checkpoint_path = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
                / f"{checkpoint.sequence:08d}-{checkpoint.receipt_sha256}.json"
            )
            payload = json.loads(checkpoint_path.read_bytes())
            parent = query_ids[0]
            payload["pending_query_ids"].remove(parent)
            payload["split_query_ids"] = [parent]
            fake_split = self.write_artifact(
                root,
                f"audit/v5_2_qd/historical/acquisition/split_receipts/{parent}.json",
                {"parent_query_id": parent, "split_receipt_id": "f" * 64},
            )
            payload["split_receipts"] = [
                {
                    "parent_query_id": parent,
                    "path": fake_split.relative_to(root).as_posix(),
                    "sha256": _sha256(fake_split),
                    "split_receipt_id": "f" * 64,
                }
            ]
            paths = self.registry._paths(root, run_spec)
            with (
                patch.object(
                    v52_query_plan,
                    "load_split_receipt",
                    return_value=None,
                ) as load_split,
                self.assertRaisesRegex(ValueError, "split|child|receipt"),
            ):
                self.registry._checkpoint_artifacts_and_population(
                    paths,
                    run_spec,
                    payload,
                )
            load_split.assert_called_once_with(
                root,
                parent,
                evidence_root=(
                    root / run_spec.raw["namespaces"]["raw"]
                ),
            )

    def test_checkpoint_publish_evidence_race_leaves_no_poisoned_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            stage_evidence = self.write_artifact(
                root, "audit/checkpoint-stage-evidence.json", {"version": 1}
            )

            def mutate_evidence() -> None:
                stage_evidence.write_bytes(
                    canonical_json_bytes({"version": 2}) + b"\n"
                )

            with (
                patch.object(
                    self.registry,
                    "_before_registry_publish",
                    side_effect=mutate_evidence,
                ),
                self.assertRaisesRegex(ValueError, "artifact|evidence|snapshot"),
            ):
                self.registry.publish_acquisition_checkpoint(
                    root,
                    run_spec,
                    expected_prior_checkpoint_sha256=None,
                    stage="STATIC_SCHEDULES",
                    query_batch_ids=(batch.batch_id,),
                    completed_query_ids=(),
                    pending_query_ids=query_ids,
                    quarantined_query_ids=(),
                    split_query_ids=(),
                    stage_evidence_receipts=(stage_evidence,),
                )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            self.assertEqual(list(checkpoint_dir.iterdir()), [])
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec)
            )

    def test_checkpoint_post_link_membership_failure_cleans_owned_final(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            unknown = checkpoint_dir / "unknown.json"

            def add_unknown_after_final_read() -> None:
                unknown.write_bytes(b"{}\n")

            with (
                patch.object(
                    self.registry,
                    "_after_registry_receipt_read",
                    side_effect=add_unknown_after_final_read,
                ),
                self.assertRaisesRegex(
                    ValueError, "checkpoint|membership|gap|fork|unknown"
                ),
            ):
                self.publish_pending_checkpoint(
                    root,
                    run_spec,
                    batch,
                    query_ids,
                    expected_prior=None,
                )
            self.assertEqual(list(checkpoint_dir.iterdir()), [unknown])
            unknown.unlink()
            self.assertIsNone(
                self.registry.load_latest_acquisition_checkpoint(root, run_spec)
            )

    def test_checkpoint_membership_cleanup_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            unknown = checkpoint_dir / "unknown.json"

            def add_unknown_after_final_read() -> None:
                unknown.write_bytes(b"{}\n")

            with (
                patch.object(
                    self.registry,
                    "_after_registry_receipt_read",
                    side_effect=add_unknown_after_final_read,
                ),
                patch.object(
                    self.registry,
                    "_cleanup_created_receipt",
                    side_effect=OSError("injected cleanup failure"),
                ),
                self.assertRaisesRegex(ValueError, "cleanup ownership verification"),
            ):
                self.publish_pending_checkpoint(
                    root,
                    run_spec,
                    batch,
                    query_ids,
                    expected_prior=None,
                )

    def test_sources_ready_requires_terminal_checkpoint_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(
                root, terminal_checkpoint=False
            )
            with self.assertRaisesRegex(ValueError, "checkpoint|artifact evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )

    def test_non_static_checkpoint_stages_fail_closed_until_authority_exists(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            for stage in (
                "REFERENCE_INPUTS",
                "TIMEZONE_REVIEW",
                "PARTICIPANT_DERIVED",
                "REMAINING",
            ):
                with self.subTest(stage=stage), self.assertRaisesRegex(
                    ValueError, "authority|implemented|unavailable"
                ):
                    self.registry.publish_acquisition_checkpoint(
                        root,
                        run_spec,
                        expected_prior_checkpoint_sha256=None,
                        stage=stage,
                        query_batch_ids=(batch.batch_id,),
                        completed_query_ids=(),
                        pending_query_ids=query_ids,
                        quarantined_query_ids=(),
                        split_query_ids=(),
                    )

    def test_static_checkpoint_replays_canonical_builder_authority(self) -> None:
        from dataclasses import replace

        from mlb_backtest import v52_query_plan

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            batch, query_ids = self.checkpoint_fixture(root, run_spec)
            different = replace(batch, batch_id="f" * 64)
            with (
                patch.object(
                    v52_query_plan,
                    "build_static_batch",
                    return_value=different,
                ),
                self.assertRaisesRegex(ValueError, "STATIC_SCHEDULES|authority"),
            ):
                self.publish_pending_checkpoint(
                    root,
                    run_spec,
                    batch,
                    query_ids,
                    expected_prior=None,
                )

    def test_sources_ready_publication_rechecks_full_checkpoint_membership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(
                root, terminal_checkpoint=False
            )
            checkpoint_dir = (
                root
                / "audit/v5_2_qd/historical/execution/acquisition_checkpoints"
            )
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            first_contents = canonical_json_bytes({"fixture": "interior"}) + b"\n"
            head_contents = canonical_json_bytes({"fixture": "head"}) + b"\n"
            first_sha = hashlib.sha256(first_contents).hexdigest()
            head_sha = hashlib.sha256(head_contents).hexdigest()
            names = (
                f"00000000-{first_sha}.json",
                f"00000001-{head_sha}.json",
            )
            first_path = checkpoint_dir / names[0]
            first_path.write_bytes(first_contents)
            (checkpoint_dir / names[1]).write_bytes(head_contents)

            def checkpoint(sequence: int, digest: str):
                return self.registry.AcquisitionCheckpoint(
                    sequence=sequence,
                    receipt_sha256=digest,
                    prior_checkpoint_sha256=first_sha if sequence else None,
                    stage="REMAINING",
                    recorded_at_utc="2026-07-14T00:00:00.000000Z",
                    git_head="a" * 40,
                    execution_receipt_sha256="b" * 64,
                    query_batch_ids=(),
                    query_batch_sha256s=(),
                    query_evidence_receipt_sha256s=(),
                    stage_evidence_receipt_sha256s=(),
                    split_receipt_sha256s=(),
                    completed_query_ids=(),
                    pending_query_ids=(),
                    quarantined_query_ids=(),
                    split_query_ids=(),
                )

            checkpoints = (checkpoint(0, first_sha), checkpoint(1, head_sha))
            payloads = (
                {"pending_query_ids": [], "quarantined_query_ids": []},
                {"pending_query_ids": [], "quarantined_query_ids": []},
            )

            def load_chain(*args: object, **kwargs: object):
                del args, kwargs
                if tuple(sorted(path.name for path in checkpoint_dir.iterdir())) != names:
                    raise self.registry._ReceiptMembershipError(
                        "checkpoint membership changed"
                    )
                return checkpoints, payloads, names

            def delete_interior() -> None:
                first_path.unlink(missing_ok=True)

            with (
                patch.object(
                    self.registry,
                    "_load_checkpoints_locked",
                    side_effect=load_chain,
                ),
                patch.object(
                    self.registry,
                    "_after_registry_publish",
                    side_effect=delete_interior,
                ),
                self.assertRaisesRegex(ValueError, "checkpoint|membership"),
            ):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertFalse(
                any(
                    path.name.startswith("000002-SOURCES_READY-")
                    for path in (root / RECEIPTS_RELATIVE).iterdir()
                )
            )

    def test_sources_ready_rejects_consistent_review_ids_unrelated_to_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            for receipt in evidence:
                payload = json.loads(receipt.read_bytes())
                payload["reviewed_query_ids"] = ["unrelated-query"]
                receipt.write_bytes(canonical_json_bytes(payload) + b"\n")

            with self.assertRaisesRegex(ValueError, "artifact|evidence|query"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=evidence,
                )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_sources_ready_allows_audited_review_subset_with_full_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            review, replay_one, replay_two = evidence
            review_payload = json.loads(review.read_bytes())
            review_payload["reviewed_query_ids"] = ["query-001"]
            review.write_bytes(canonical_json_bytes(review_payload) + b"\n")

            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=(review, replay_one, replay_two),
            )
            self.assertEqual(state.phase, "SOURCES_READY")
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), state
            )

    def test_sources_ready_rejects_invalid_review_subset_or_partial_replay(self) -> None:
        cases = ("empty", "outside", "duplicate", "unsorted", "partial_replay")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                review, replay_one, replay_two = evidence
                target = replay_one if case == "partial_replay" else review
                payload = json.loads(target.read_bytes())
                replacements = {
                    "empty": [],
                    "outside": ["outside-query"],
                    "duplicate": ["query-001", "query-001"],
                    "unsorted": ["query-002", "query-001"],
                    "partial_replay": ["query-001"],
                }
                payload["reviewed_query_ids"] = replacements[case]
                target.write_bytes(canonical_json_bytes(payload) + b"\n")

                with self.assertRaisesRegex(ValueError, "artifact|evidence|query"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=(review, replay_one, replay_two),
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )

    def test_dynamic_phase_evidence_is_stable_through_publication(self) -> None:
        for artifact_kind in ("inventory", "review", "replay"):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
                inventory, evidence = self.write_real_source_evidence(root)
                target = inventory if artifact_kind == "inventory" else evidence[0 if artifact_kind == "review" else 1]

                def mutate_evidence() -> None:
                    target.write_bytes(target.read_bytes() + b" ")

                with patch.object(
                    self.registry,
                    "_before_registry_publish",
                    side_effect=mutate_evidence,
                ), self.assertRaisesRegex(ValueError, "evidence|identity|snapshot|changed"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 2)
                target.write_bytes(target.read_bytes()[:-1])
                self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_publication_rejects_receipt_parent_or_file_replacement_after_hook(self) -> None:
        for attack in ("parent", "file"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE
                displaced = receipts.with_name("phase_receipts-displaced")

                def replace_published_path() -> None:
                    published = next(path for path in receipts.iterdir() if path.name.startswith("000001-"))
                    if attack == "parent":
                        receipts.rename(displaced)
                        receipts.mkdir()
                    else:
                        replacement = published.with_suffix(".replacement")
                        replacement.write_bytes(published.read_bytes())
                        os.replace(replacement, published)

                with patch.object(
                    self.registry,
                    "_after_registry_publish",
                    side_effect=replace_published_path,
                ), self.assertRaisesRegex(ValueError, "identity|receipt|publication"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="DESIGN_ACCEPTED",
                        next_phase="ACQUIRING",
                        inventory_path=root / PROTECTED_RELATIVE,
                        validation_receipts=(),
                    )
                if attack == "parent":
                    self.assertEqual(list(receipts.iterdir()), [])
                    self.assertEqual(len(list(displaced.iterdir())), 1)
                self.assertEqual(initial.phase, "DESIGN_ACCEPTED")

    def test_publication_final_rebind_rejects_disappeared_or_replaced_receipt(self) -> None:
        for attack in ("disappear", "replace"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE

                def attack_after_final_read() -> None:
                    published = next(
                        path
                        for path in receipts.iterdir()
                        if path.name.startswith("000001-")
                    )
                    if attack == "disappear":
                        published.unlink()
                    else:
                        replacement = published.with_suffix(".replacement")
                        replacement.write_bytes(b"tampered\n")
                        os.replace(replacement, published)

                hook = getattr(
                    self.registry, "_after_registry_receipt_read", None
                )
                self.assertIsNotNone(
                    hook, "final receipt read/rebind race hook is missing"
                )
                with patch.object(
                    self.registry,
                    "_after_registry_receipt_read",
                    side_effect=attack_after_final_read,
                ), self.assertRaisesRegex(
                    ValueError, "identity|receipt|publication|cleanup"
                ):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="DESIGN_ACCEPTED",
                        next_phase="ACQUIRING",
                        inventory_path=root / PROTECTED_RELATIVE,
                        validation_receipts=(),
                    )
                if attack == "disappear":
                    self.assertFalse(
                        any(
                            path.name.startswith("000001-")
                            for path in receipts.iterdir()
                        )
                    )
                    self.assertEqual(
                        self.registry.load_execution_registry(root, run_spec), initial
                    )
                else:
                    self.assertTrue(
                        any(
                            path.name.startswith("000001-")
                            for path in receipts.iterdir()
                        )
                    )
                    with self.assertRaises(ValueError):
                        self.registry.load_execution_registry(root, run_spec)

    def test_publication_accepts_equivalent_receipt_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipts = root / RECEIPTS_RELATIVE

            def replace_with_equivalent_inode() -> None:
                published = next(
                    path
                    for path in receipts.iterdir()
                    if path.name.startswith("000001-")
                )
                intended = published.read_bytes()
                original_inode = published.stat().st_ino
                replacement = published.with_suffix(".replacement")
                replacement.write_bytes(intended)
                self.assertEqual(replacement.stat().st_nlink, 1)
                self.assertNotEqual(replacement.stat().st_ino, original_inode)
                os.replace(replacement, published)

            with patch.object(
                self.registry,
                "_after_registry_receipt_read",
                side_effect=replace_with_equivalent_inode,
            ):
                advanced = self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

            self.assertEqual(advanced.phase, "ACQUIRING")
            self.assertEqual(
                len(
                    [
                        path
                        for path in receipts.iterdir()
                        if path.name.startswith("000001-")
                    ]
                ),
                1,
            )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), advanced
            )

    def test_publication_rejects_final_same_sequence_receipt_fork(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipts = root / RECEIPTS_RELATIVE
            fork_path: Path | None = None

            def add_valid_same_sequence_fork() -> None:
                nonlocal fork_path
                published = next(
                    path
                    for path in receipts.iterdir()
                    if path.name.startswith("000001-")
                )
                payload = json.loads(published.read_bytes())
                payload["recorded_at_utc"] = "2099-01-01T00:00:00.000000Z"
                contents = canonical_json_bytes(payload) + b"\n"
                digest = hashlib.sha256(contents).hexdigest()
                fork_path = receipts / f"000001-ACQUIRING-{digest}.json"
                fork_path.write_bytes(contents)

            with patch.object(
                self.registry,
                "_after_registry_receipt_read",
                side_effect=add_valid_same_sequence_fork,
            ), self.assertRaisesRegex(ValueError, "fork|receipt|membership|chain"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

            self.assertIsNotNone(fork_path)
            self.assertTrue(fork_path.is_file())
            self.assertEqual(
                [
                    path
                    for path in receipts.iterdir()
                    if path.name.startswith("000001-")
                ],
                [fork_path],
                "the caller-created final is cleaned without deleting the fork",
            )

    def test_partial_write_or_fsync_failure_removes_created_receipt(self) -> None:
        for failure in ("partial_write", "fsync"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE
                execution_fd, descriptors = self.registry._open_directory(
                    root,
                    Path("audit/v5_2_qd/historical/execution"),
                    create=False,
                )
                if failure == "partial_write":
                    original_write = os.write
                    calls = 0

                    def fail_write(descriptor: int, contents: object) -> int:
                        nonlocal calls
                        calls += 1
                        if calls == 1:
                            return original_write(descriptor, bytes(contents[:8]))
                        raise OSError("injected partial write failure")

                    context = patch.object(self.registry.os, "write", side_effect=fail_write)
                else:
                    original_fsync = os.fsync

                    def fail_receipt_fsync(descriptor: int) -> None:
                        metadata = os.fstat(descriptor)
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_size:
                            raise OSError("injected receipt fsync failure")
                        original_fsync(descriptor)

                    context = patch.object(self.registry.os, "fsync", side_effect=fail_receipt_fsync)
                try:
                    with context, self.assertRaisesRegex(ValueError, "published|publication|safely"):
                        self.registry._write_exclusive(
                            execution_fd,
                            Path("phase_receipts/injected.json"),
                            b"receipt contents\n",
                            "execution receipt",
                            lambda: None,
                        )
                finally:
                    self.registry._close_directories(descriptors)
                self.assertEqual(len(list(receipts.iterdir())), 1)
                self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_git_binding_cannot_change_between_evidence_validation_and_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            original_capture = self.registry._capture_git_snapshot

            def mutate_and_commit(repo: Path, evidence: object):
                (root / DESIGN_RELATIVE).write_bytes((root / DESIGN_RELATIVE).read_bytes() + b"changed\n")
                _git(root, "add", DESIGN_RELATIVE.as_posix())
                _git(root, "commit", "-qm", "racing design")
                return original_capture(repo, evidence)

            with patch.object(
                self.registry,
                "_capture_git_snapshot",
                side_effect=mutate_and_commit,
            ), self.assertRaisesRegex(ValueError, "binding|Git|snapshot|design"):
                self.initialize(root, run_spec)
            self.assertEqual(list((root / RECEIPTS_RELATIVE).iterdir()), [])

    def test_skip_worktree_cannot_hide_dirty_bound_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            relative = BOUND_EXECUTION_CODE[0]
            _git(root, "update-index", "--skip-worktree", relative.as_posix())
            (root / relative).write_bytes((root / relative).read_bytes() + b"dirty\n")
            self.assertEqual(
                _git(root, "status", "--porcelain=v1", "--", relative.as_posix()),
                "",
            )
            with self.assertRaisesRegex(ValueError, "dirty|Git|index|HEAD|worktree"):
                self.initialize(root, run_spec)

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_directory_child_stat_failures_do_not_leak_fds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            child = root / "child"
            child.mkdir()
            child_inode = child.stat().st_ino
            original_fstat = os.fstat
            before = len(os.listdir("/dev/fd"))

            def fail_child(descriptor: int):
                metadata = original_fstat(descriptor)
                if metadata.st_ino == child_inode:
                    raise OSError("injected child fstat failure")
                return metadata

            with patch.object(self.registry.os, "fstat", side_effect=fail_child):
                for _ in range(20):
                    with self.assertRaises(OSError):
                        self.registry._open_directory(
                            root, Path("child"), create=False
                        )
            self.assertEqual(len(os.listdir("/dev/fd")), before)

    @unittest.skipUnless(Path("/dev/fd").is_dir(), "/dev/fd is unavailable")
    def test_registry_lock_flock_errors_close_every_descriptor(self) -> None:
        for failure in ("acquire", "release"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                paths = self.registry._paths(root, run_spec)
                original_flock = self.registry.fcntl.flock
                before = len(os.listdir("/dev/fd"))

                def fail_flock(descriptor: int, operation: int) -> None:
                    if failure == "acquire" or operation == self.registry.fcntl.LOCK_UN:
                        raise OSError(f"injected {failure} flock failure")
                    original_flock(descriptor, operation)

                with patch.object(
                    self.registry.fcntl, "flock", side_effect=fail_flock
                ):
                    for _ in range(20):
                        with self.assertRaisesRegex(
                            OSError, f"injected {failure} flock failure"
                        ):
                            with self.registry._registry_lock(
                                paths, create=False, exclusive=True
                            ):
                                pass
                self.assertEqual(len(os.listdir("/dev/fd")), before)

    def test_load_rejects_forged_semantically_impossible_phase_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            receipt = next(path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000001-"))
            unrelated = self.write_artifact(root, "unrelated-inventory.json", {"rows": 1})
            payload = json.loads(receipt.read_bytes())
            payload["inventory"] = {
                "path": unrelated.relative_to(root).as_posix(),
                "sha256": _sha256(unrelated),
            }
            self.replace_receipt_payload(receipt, payload)
            with self.assertRaisesRegex(ValueError, "ACQUIRING|phase|inventory|evidence"):
                self.registry.load_execution_registry(root, run_spec)

    def test_load_rejects_forged_sources_ready_review_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING")
            inventory, evidence = self.write_real_source_evidence(root)
            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
            review = evidence[0]
            review_payload = json.loads(review.read_bytes())
            review_payload["reviewed_query_ids"] = ["unrelated-query"]
            review.write_bytes(canonical_json_bytes(review_payload) + b"\n")
            receipt = next(path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000002-"))
            payload = json.loads(receipt.read_bytes())
            payload["validation_receipts"][0]["sha256"] = _sha256(review)
            self.replace_receipt_payload(receipt, payload)
            with self.assertRaisesRegex(ValueError, "source|query|phase|evidence"):
                self.registry.load_execution_registry(root, run_spec)

    def test_acquiring_binds_actual_protected_inventory_without_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            receipt = next(
                path
                for path in (root / RECEIPTS_RELATIVE).iterdir()
                if path.name.startswith("000001-")
            )
            payload = json.loads(receipt.read_bytes())
            self.assertEqual(
                payload["inventory"],
                {
                    "path": PROTECTED_RELATIVE.as_posix(),
                    "sha256": _sha256(root / PROTECTED_RELATIVE),
                },
            )
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(state.phase, "ACQUIRING")

    def test_sources_ready_binds_real_inventory_review_and_two_clean_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            inventory, evidence = self.write_real_source_evidence(root)
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="ACQUIRING",
                next_phase="SOURCES_READY",
                inventory_path=inventory,
                validation_receipts=evidence,
            )
            receipt = next(
                path
                for path in (root / RECEIPTS_RELATIVE).iterdir()
                if path.name.startswith("000002-")
            )
            payload = json.loads(receipt.read_bytes())
            self.assertEqual(state.phase, "SOURCES_READY")
            self.assertEqual(
                payload["inventory"]["sha256"], _sha256(inventory)
            )
            self.assertEqual(
                [item["sha256"] for item in payload["validation_receipts"]],
                [_sha256(path) for path in evidence],
            )

    def test_rejects_inventory_and_validation_through_external_symlink_alias(self) -> None:
        for artifact_kind in ("inventory", "validation"):
            with self.subTest(artifact_kind=artifact_kind), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve()
                root, run_spec = self.make_repo(base)
                self.initialize(root, run_spec)
                alias = base / "repo_alias"
                alias.symlink_to(root, target_is_directory=True)
                if artifact_kind == "inventory":
                    initial = self.registry.load_execution_registry(root, run_spec)
                    expected_phase = "DESIGN_ACCEPTED"
                    next_phase = "ACQUIRING"
                    inventory = alias / PROTECTED_RELATIVE
                    evidence: tuple[Path, ...] = ()
                else:
                    initial = self.advance(
                        root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                    )
                    expected_phase = "ACQUIRING"
                    next_phase = "SOURCES_READY"
                    inventory, source_evidence = self.write_real_source_evidence(root)
                    evidence = (
                        alias / source_evidence[0].relative_to(root),
                        *source_evidence[1:],
                    )
                with self.assertRaisesRegex(ValueError, "root|symlink|path"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase=expected_phase,
                        next_phase=next_phase,
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )

    def advance(self, root: Path, run_spec: object, expected: str, phase: str, *, stem: str = "phase"):
        del stem
        if phase == "ACQUIRING":
            inventory = root / PROTECTED_RELATIVE
            evidence: tuple[Path, ...] = ()
        else:
            inventory, evidence = self.write_real_source_evidence(root)
        return self.registry.advance_execution_phase(
            root,
            run_spec,
            expected_phase=expected,
            next_phase=phase,
            inventory_path=inventory,
            validation_receipts=evidence,
        )

    def test_invalid_real_source_inventory_review_or_replay_quarantines(self) -> None:
        cases = (
            "inventory_status",
            "critical_errors",
            "network_attempts",
            "review_decision",
            "replay_status",
            "missing_replay",
            "inventory_hash",
            "snapshot_hashes",
            "reviewed_query_ids",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                initial = self.advance(
                    root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
                )
                inventory, evidence = self.write_real_source_evidence(root)
                review, replay_one, replay_two = evidence
                if case == "inventory_status":
                    payload = json.loads(inventory.read_bytes())
                    payload["status"] = "FAILED"
                    inventory.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "critical_errors":
                    payload = json.loads(review.read_bytes())
                    payload["critical_errors"] = [{"code": "FAILED"}]
                    review.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "network_attempts":
                    payload = json.loads(replay_one.read_bytes())
                    payload["network_attempts"] = 1
                    replay_one.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "review_decision":
                    payload = json.loads(review.read_bytes())
                    payload["decision"] = "REJECTED"
                    review.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "replay_status":
                    payload = json.loads(replay_two.read_bytes())
                    payload["status"] = "FAIL"
                    replay_two.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "missing_replay":
                    evidence = (review, replay_one)
                elif case == "inventory_hash":
                    payload = json.loads(review.read_bytes())
                    payload["inventory_sha256"] = "f" * 64
                    review.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "snapshot_hashes":
                    payload = json.loads(replay_one.read_bytes())
                    payload["snapshot_sha256s"] = ["c" * 64]
                    replay_one.write_bytes(canonical_json_bytes(payload) + b"\n")
                elif case == "reviewed_query_ids":
                    payload = json.loads(replay_two.read_bytes())
                    payload["reviewed_query_ids"] = ["different-query"]
                    replay_two.write_bytes(canonical_json_bytes(payload) + b"\n")
                with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                    self.registry.advance_execution_phase(
                        root,
                        run_spec,
                        expected_phase="ACQUIRING",
                        next_phase="SOURCES_READY",
                        inventory_path=inventory,
                        validation_receipts=evidence,
                    )
                self.assertEqual(
                    self.registry.load_execution_registry(root, run_spec), initial
                )
                quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
                self.assertEqual(len(list(quarantine.iterdir())), 1)

    def test_invalid_acquiring_inventory_quarantines_without_validation_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = self.write_artifact(
                root, "audit/v5_2_qd/historical/acquisition/wrong_inventory.json", {"wrong": True}
            )
            with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=inventory,
                    validation_receipts=(),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )
            quarantine_dir = root / "audit/v5_2_qd/historical/execution/quarantine"
            receipts = list(quarantine_dir.iterdir())
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_bytes())
            self.assertEqual(
                payload["inventory"],
                {
                    "path": inventory.relative_to(root).as_posix(),
                    "sha256": _sha256(inventory),
                },
            )
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(
                payload["error_classification"], "DIRECT_INVENTORY_MISMATCH"
            )
            self.assertEqual(payload["attempted_phase"], "ACQUIRING")
            self.assertEqual(payload["current_phase"], "DESIGN_ACCEPTED")
            self.assertEqual(
                payload["current_receipt_sha256"], initial.receipt_sha256
            )
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            schema = json.loads(
                (root / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
            )
            jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)
            self.assertEqual(
                receipts[0].read_bytes(), canonical_json_bytes(payload) + b"\n"
            )

    def test_sources_ready_missing_receipts_quarantines_with_inventory_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(
                root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
            )
            inventory, _evidence = self.write_real_source_evidence(root)
            with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=(),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )
            quarantine_dir = root / "audit/v5_2_qd/historical/execution/quarantine"
            receipts = list(quarantine_dir.iterdir())
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_bytes())
            self.assertEqual(
                payload["inventory"],
                {
                    "path": SOURCE_INVENTORY_RELATIVE.as_posix(),
                    "sha256": _sha256(inventory),
                },
            )
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(
                payload["error_classification"],
                "SOURCE_GATE_EVIDENCE_INVALID",
            )

    def test_duplicate_source_review_quarantines_without_advancing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            initial = self.advance(
                root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING"
            )
            inventory, evidence = self.write_real_source_evidence(root)
            review, replay_one, replay_two = evidence
            with self.assertRaisesRegex(ValueError, "artifact|evidence"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="ACQUIRING",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=(
                        review,
                        review,
                        replay_one,
                        replay_two,
                    ),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )
            quarantine_dir = root / "audit/v5_2_qd/historical/execution/quarantine"
            receipts = list(quarantine_dir.iterdir())
            self.assertEqual(len(receipts), 1)
            payload = json.loads(receipts[0].read_bytes())
            self.assertEqual(
                payload["error_classification"],
                "SOURCE_GATE_EVIDENCE_INVALID",
            )
            self.assertEqual(
                payload["validation_receipts"],
                [
                    {
                        "path": path.relative_to(root).as_posix(),
                        "sha256": _sha256(path),
                    }
                    for path in (review, review, replay_one, replay_two)
                ],
            )
            schema = json.loads(
                (root / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
            )
            jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)

    def test_publication_rejects_replaced_execution_namespace(self) -> None:
        for operation in ("initialize", "advance", "quarantine"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                initial = None
                if operation != "initialize":
                    initial = self.initialize(root, run_spec)
                execution = root / "audit/v5_2_qd/historical/execution"
                displaced = execution.with_name("execution-displaced")

                def replace_namespace() -> None:
                    execution.rename(displaced)
                    (execution / "phase_receipts").mkdir(parents=True)
                    (execution / "quarantine").mkdir()
                    (displaced / ".registry.lock").rename(
                        execution / ".registry.lock"
                    )

                hook = getattr(self.registry, "_before_registry_publish", None)
                self.assertIsNotNone(hook, "registry publish race hook is missing")
                with patch.object(
                    self.registry,
                    "_before_registry_publish",
                    side_effect=replace_namespace,
                ), self.assertRaisesRegex(ValueError, "identity|namespace|lock"):
                    if operation == "initialize":
                        self.initialize(root, run_spec)
                    elif operation == "advance":
                        self.registry.advance_execution_phase(
                            root,
                            run_spec,
                            expected_phase="DESIGN_ACCEPTED",
                            next_phase="ACQUIRING",
                            inventory_path=root / PROTECTED_RELATIVE,
                            validation_receipts=(),
                        )
                    else:
                        failed = self.write_artifact(
                            root, "failed.json", {"status": "FAIL"}
                        )
                        self.registry.quarantine_execution_transition(
                            root,
                            run_spec,
                            attempted_phase="ACQUIRING",
                            validation_receipts=(failed,),
                        )

                self.assertEqual(list((execution / "phase_receipts").iterdir()), [])
                self.assertEqual(list((execution / "quarantine").iterdir()), [])
                (execution / ".registry.lock").rename(
                    displaced / ".registry.lock"
                )
                shutil.rmtree(execution)
                displaced.rename(execution)
                if initial is None:
                    self.assertEqual(
                        list((execution / "phase_receipts").iterdir()), []
                    )
                else:
                    self.assertEqual(
                        self.registry.load_execution_registry(root, run_spec),
                        initial,
                    )

    def test_publication_rolls_back_after_late_namespace_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            execution = root / "audit/v5_2_qd/historical/execution"
            displaced = execution.with_name("execution-displaced")

            def replace_namespace() -> None:
                execution.rename(displaced)
                (execution / "phase_receipts").mkdir(parents=True)
                (execution / "quarantine").mkdir()
                (displaced / ".registry.lock").rename(
                    execution / ".registry.lock"
                )

            hook = getattr(self.registry, "_after_registry_publish", None)
            self.assertIsNotNone(hook, "late registry publish hook is missing")
            with patch.object(
                self.registry,
                "_after_registry_publish",
                side_effect=replace_namespace,
            ), self.assertRaisesRegex(ValueError, "identity|namespace|lock"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=root / PROTECTED_RELATIVE,
                    validation_receipts=(),
                )

            self.assertEqual(list((execution / "phase_receipts").iterdir()), [])
            self.assertEqual(
                len(list((displaced / "phase_receipts").iterdir())), 1
            )
            (execution / ".registry.lock").rename(displaced / ".registry.lock")
            shutil.rmtree(execution)
            displaced.rename(execution)
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )

    def test_rejects_git_head_identity_race_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            hook = getattr(self.registry, "_before_registry_publish", None)
            self.assertIsNotNone(hook, "registry publish race hook is missing")

            def race():
                _git(root, "commit", "--allow-empty", "-qm", "racing head")

            with patch.object(self.registry, "_before_registry_publish", side_effect=race), self.assertRaisesRegex(ValueError, "HEAD|identity|snapshot"):
                self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=root / PROTECTED_RELATIVE, validation_receipts=())
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 1)
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)

    def test_rejects_lock_inode_replacement_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            original = self.registry._success_contents
            lock = root / "audit/v5_2_qd/historical/execution/.registry.lock"

            def replace_lock(payload):
                contents = original(payload)
                lock.unlink()
                lock.write_bytes(b"")
                return contents

            with patch.object(self.registry, "_success_contents", side_effect=replace_lock), self.assertRaisesRegex(ValueError, "lock.*identity|lock.*replaced"):
                self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=root / PROTECTED_RELATIVE, validation_receipts=())
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 1)

    def test_rejects_root_with_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            real_parent = base / "real"
            real_parent.mkdir()
            root, run_spec = self.make_repo(real_parent)
            self.initialize(root, run_spec)
            alias = base / "alias"
            alias.symlink_to(real_parent, target_is_directory=True)
            aliased_root = alias / root.name
            with self.assertRaisesRegex(ValueError, "ancestor|symlink"):
                self.registry.load_execution_registry(aliased_root, run_spec)

    def test_initializes_design_accepted_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            first = self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            metadata = receipt.stat()
            before = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                receipt.read_bytes(),
            )
            second = self.initialize(root, run_spec)
            self.assertEqual(first, second)
            self.assertEqual(first.phase, "DESIGN_ACCEPTED")
            self.assertEqual(first.sequence, 0)
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 1)
            metadata = receipt.stat()
            self.assertEqual(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    receipt.read_bytes(),
                ),
                before,
            )
            self.assertTrue(receipt.read_bytes().endswith(b"\n"))
            self.assertFalse(receipt.read_bytes().endswith(b"\n\n"))
            self.assertEqual(
                receipt.name,
                f"000000-DESIGN_ACCEPTED-{first.receipt_sha256}.json",
            )

    def test_transition_requires_immediate_next_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = self.write_artifact(root, "inventory.json", {"rows": 1})
            with self.assertRaisesRegex(ValueError, "immediate|phase"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="SOURCES_READY",
                    inventory_path=inventory,
                    validation_receipts=(),
                )
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
            self.assertEqual(list(quarantine.iterdir()), [])

    def test_transition_binds_prior_receipt_commit_specs_and_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = root / PROTECTED_RELATIVE
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=inventory,
                validation_receipts=(),
            )
            receipt_path = next(
                path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000001-")
            )
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["prior_receipt_sha256"], initial.receipt_sha256)
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            self.assertEqual(payload["run_spec"], {"path": RUN_SPEC_RELATIVE.as_posix(), "sha256": _sha256(root / RUN_SPEC_RELATIVE)})
            self.assertEqual(payload["inventory"], {"path": PROTECTED_RELATIVE.as_posix(), "sha256": _sha256(inventory)})
            self.assertEqual(payload["validation_receipts"], [])
            self.assertEqual(payload["implementation_plan"]["git_blob_id"], _git(root, "rev-parse", f"HEAD:{PLAN_RELATIVE}"))
            self.assertEqual(state.inventory_sha256, _sha256(inventory))

    def test_failed_validation_quarantines_without_advancing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            failed = self.write_artifact(root, "failed.json", {"status": "FAIL"})
            path = self.registry.quarantine_execution_transition(
                root,
                run_spec,
                attempted_phase="ACQUIRING",
                validation_receipts=(failed,),
            )
            self.assertIn("execution/quarantine", path.as_posix())
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), initial)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["attempted_phase"], "ACQUIRING")
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            path.write_bytes(path.read_bytes()[:-2] + b"X\n")
            with self.assertRaisesRegex(ValueError, "quarantine.*hash"):
                self.registry.load_execution_registry(root, run_spec)

    def test_rejects_chain_gap_fork_tamper_symlink_hardlink_special_and_rollback(self) -> None:
        attacks = ("gap", "fork", "tamper", "symlink", "hardlink", "special", "rollback")
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                self.initialize(root, run_spec)
                receipts = root / RECEIPTS_RELATIVE
                first = next(receipts.iterdir())
                if attack == "rollback":
                    self.advance(root, run_spec, "DESIGN_ACCEPTED", "ACQUIRING", stem="one")
                    self.advance(root, run_spec, "ACQUIRING", "SOURCES_READY", stem="two")
                    next(path for path in receipts.iterdir() if path.name.startswith("000001-")).unlink()
                elif attack == "gap":
                    first.rename(receipts / first.name.replace("000000", "000001", 1))
                elif attack == "fork":
                    shutil.copy2(first, receipts / f"000000-DESIGN_ACCEPTED-{'0' * 64}.json")
                elif attack == "tamper":
                    first.write_bytes(first.read_bytes()[:-2] + b"X\n")
                else:
                    body = first.read_bytes()
                    first.unlink()
                    external = root / "external"
                    if attack == "symlink":
                        external.write_bytes(body)
                        first.symlink_to(external)
                    elif attack == "hardlink":
                        external.write_bytes(body)
                        os.link(external, first)
                    else:
                        os.mkfifo(first)
                        self.assertTrue(stat.S_ISFIFO(first.lstat().st_mode))
                with self.assertRaises((ValueError, RuntimeError, OSError)):
                    self.registry.load_execution_registry(root, run_spec)

    def test_concurrent_transition_has_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            inventory = root / PROTECTED_RELATIVE

            barrier = threading.Barrier(2)

            def transition():
                barrier.wait()
                try:
                    state = self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=inventory, validation_receipts=())
                except ValueError as exc:
                    return "failure", str(exc)
                return "success", state

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: transition(), range(2)))
            self.assertEqual(sorted(outcome[0] for outcome in outcomes), ["failure", "success"])
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 2)

    def test_identical_transition_replay_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            inventory = root / PROTECTED_RELATIVE
            arguments = {
                "expected_phase": "DESIGN_ACCEPTED",
                "next_phase": "ACQUIRING",
                "inventory_path": inventory,
                "validation_receipts": (),
            }
            self.registry.advance_execution_phase(root, run_spec, **arguments)
            with self.assertRaisesRegex(ValueError, "replay|expected phase"):
                self.registry.advance_execution_phase(root, run_spec, **arguments)
            quarantine = root / "audit/v5_2_qd/historical/execution/quarantine"
            self.assertEqual(list(quarantine.iterdir()), [])

    def test_registry_load_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            expected = self.initialize(root, run_spec)
            execution = root / "audit/v5_2_qd/historical/execution"
            before = {path.relative_to(execution): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None) for path in execution.rglob("*")}
            self.assertEqual(self.registry.load_execution_registry(root, run_spec), expected)
            after = {path.relative_to(execution): (path.lstat().st_ino, path.lstat().st_size, path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() else None) for path in execution.rglob("*")}
            self.assertEqual(after, before)

    def test_schema_is_closed_and_receipts_are_canonical(self) -> None:
        schema = json.loads((ROOT / "schemas/v5_2_execution_receipt.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        for branch in schema["oneOf"]:
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["properties"]), set(branch["required"]))
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            payload = json.loads(receipt.read_bytes())
            jsonschema.validate(payload, schema, format_checker=FORMAT_CHECKER)
            self.assertEqual(receipt.read_bytes(), canonical_json_bytes(payload) + b"\n")

            failed = self.write_artifact(root, "failed.json", {"status": "FAIL"})
            quarantine = self.registry.quarantine_execution_transition(
                root,
                run_spec,
                attempted_phase="ACQUIRING",
                validation_receipts=(failed,),
            )
            jsonschema.validate(
                json.loads(quarantine.read_bytes()),
                schema,
                format_checker=FORMAT_CHECKER,
            )

            self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=root / PROTECTED_RELATIVE,
                validation_receipts=(),
            )
            transition = next(
                path
                for path in (root / RECEIPTS_RELATIVE).iterdir()
                if path.name.startswith("000001-")
            )
            jsonschema.validate(
                json.loads(transition.read_bytes()),
                schema,
                format_checker=FORMAT_CHECKER,
            )

    def test_schema_rejects_impossible_phase_sequence_and_prior_combinations(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            initial = json.loads(receipt.read_bytes())
            impossible = (
                {**initial, "phase": "ACQUIRING"},
                {
                    **initial,
                    "sequence": 1,
                    "prior_receipt_sha256": "0" * 64,
                },
            )
            for payload in impossible:
                with self.subTest(payload=payload), self.assertRaises(
                    jsonschema.ValidationError
                ):
                    jsonschema.validate(
                        payload, schema, format_checker=FORMAT_CHECKER
                    )

    def test_automatic_quarantine_schema_and_runtime_enforce_phase_classification_parity(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            wrong = self.write_artifact(root, "wrong.json", {"wrong": True})
            with self.assertRaises(ValueError):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=wrong,
                    validation_receipts=(),
                )
            quarantine = next(
                (root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()
            )
            payload = json.loads(quarantine.read_bytes())
            payload["error_classification"] = "SOURCE_GATE_EVIDENCE_INVALID"
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    payload, schema, format_checker=FORMAT_CHECKER
                )
            with self.assertRaisesRegex(ValueError, "classification|phase|schema"):
                self.registry._validate_quarantine_receipt(payload)

            invalid_timestamp = json.loads(quarantine.read_bytes())
            invalid_timestamp["recorded_at_utc"] = "2026-02-30T00:00:00.000000Z"
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    invalid_timestamp,
                    schema,
                    format_checker=FORMAT_CHECKER,
                )

    def test_automatic_quarantine_schema_accepts_only_supported_phase_mapping(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_bytes()
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            wrong = self.write_artifact(root, "wrong.json", {"wrong": True})
            with self.assertRaises(ValueError):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=wrong,
                    validation_receipts=(),
                )
            quarantine = next(
                (root / "audit/v5_2_qd/historical/execution/quarantine").iterdir()
            )
            acquiring = json.loads(quarantine.read_bytes())
            sources_ready = {
                **acquiring,
                "attempted_phase": "SOURCES_READY",
                "error_classification": "SOURCE_GATE_EVIDENCE_INVALID",
            }
            for valid in (acquiring, sources_ready):
                jsonschema.validate(
                    valid, schema, format_checker=FORMAT_CHECKER
                )
            invalid = (
                {**acquiring, "attempted_phase": "OOF_SEALED"},
                {
                    **acquiring,
                    "error_classification": "SOURCE_GATE_EVIDENCE_INVALID",
                },
                {
                    **sources_ready,
                    "error_classification": "DIRECT_INVENTORY_MISMATCH",
                },
            )
            for payload in invalid:
                with self.subTest(payload=payload), self.assertRaises(
                    jsonschema.ValidationError
                ):
                    jsonschema.validate(
                        payload, schema, format_checker=FORMAT_CHECKER
                    )

    def test_rejects_invalid_receipt_paths_on_emit_and_load(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v5_2_execution_receipt.schema.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            bad_inventory = self.write_artifact(
                root, "bad\\inventory.json", {"rows": 1}
            )
            with self.assertRaisesRegex(ValueError, "path|backslash|schema"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=bad_inventory,
                    validation_receipts=(),
                )
            bad_validation = self.write_artifact(
                root, "bad\\validation.json", {"status": "FAIL"}
            )
            with self.assertRaisesRegex(ValueError, "path|backslash|schema"):
                self.registry.quarantine_execution_transition(
                    root,
                    run_spec,
                    attempted_phase="ACQUIRING",
                    validation_receipts=(bad_validation,),
                )
            self.assertEqual(
                self.registry.load_execution_registry(root, run_spec), initial
            )

            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            payload = json.loads(receipt.read_bytes())
            invalid_path = "bad\\inventory.json"
            (root / invalid_path).write_bytes((root / PROTECTED_RELATIVE).read_bytes())
            payload["inventory"]["path"] = invalid_path
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(
                    payload, schema, format_checker=FORMAT_CHECKER
                )
            contents = canonical_json_bytes(payload) + b"\n"
            digest = hashlib.sha256(contents).hexdigest()
            replacement = receipt.with_name(
                f"000000-DESIGN_ACCEPTED-{digest}.json"
            )
            receipt.unlink()
            replacement.write_bytes(contents)
            with self.assertRaisesRegex(ValueError, "path|backslash|schema"):
                self.registry.load_execution_registry(root, run_spec)

    def test_rejects_dirty_bound_execution_code_but_not_unrelated_files(self) -> None:
        for relative in BOUND_EXECUTION_CODE:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root, run_spec = self.make_repo(Path(directory))
                with (root / relative).open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                    self.initialize(root, run_spec)

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            with (root / BOUND_EXECUTION_CODE[0]).open("a", encoding="utf-8") as handle:
                handle.write("\n")
            inventory = self.write_artifact(root, "inventory.json", {"rows": 1})
            with self.assertRaisesRegex(ValueError, "execution code|dirty"):
                self.registry.advance_execution_phase(
                    root,
                    run_spec,
                    expected_phase="DESIGN_ACCEPTED",
                    next_phase="ACQUIRING",
                    inventory_path=inventory,
                    validation_receipts=(),
                )

        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            (root / "unrelated.txt").write_text("dirty\n", encoding="utf-8")
            self.assertEqual(self.initialize(root, run_spec).phase, "DESIGN_ACCEPTED")

    def test_rejects_dirty_bound_input_and_identity_race(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            (root / DESIGN_RELATIVE).write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError, "dirty|binding|hash|HEAD|index|worktree"
            ):
                self.initialize(root, run_spec)
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            original = self.registry._read_fd_all

            def replace_after_read(descriptor: int) -> bytes:
                data = original(descriptor)
                replacement = receipt.with_suffix(".replacement")
                replacement.write_bytes(data)
                os.replace(replacement, receipt)
                return data

            with patch.object(self.registry, "_read_fd_all", side_effect=replace_after_read), self.assertRaisesRegex((ValueError, RuntimeError), "changed|identity"):
                self.registry.load_execution_registry(root, run_spec)


    def test_plan_a_amendment_binding_is_rehashed_on_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            (root / AMENDMENT_RELATIVE).write_bytes(
                (root / AMENDMENT_RELATIVE).read_bytes() + b"tampered\n"
            )
            with self.assertRaisesRegex(ValueError, "amendment|binding|hash|dirty|worktree"):
                self.registry.load_execution_registry(root, run_spec)


if __name__ == "__main__":
    unittest.main()
