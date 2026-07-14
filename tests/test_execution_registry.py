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
from pathlib import Path
from unittest.mock import patch

import jsonschema

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.protocols import load_run_spec


ROOT = Path(__file__).resolve().parents[1]
RUN_SPEC_RELATIVE = Path("protocols/v5_2_qd/historical_walkforward_500.json")
DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-13-mlb-v5-2-acquire-fit-500-design.md"
)
PLAN_RELATIVE = Path(
    "docs/superpowers/plans/2026-07-13-mlb-v5-2-hybrid-acquisition.md"
)
CONTRACT_RELATIVE = Path("protocols/v5_2_qd/historical_source_acquisition.json")
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
            CONTRACT_RELATIVE,
            BASELINE_RELATIVE,
            PROTECTED_RELATIVE,
            *BOUND_EXECUTION_CODE,
        ):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / RUN_SPEC_RELATIVE, root / RUN_SPEC_RELATIVE)
        shutil.copy2(ROOT / DESIGN_RELATIVE, root / DESIGN_RELATIVE)
        shutil.copy2(ROOT / PLAN_RELATIVE, root / PLAN_RELATIVE)
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
        self, root: Path
    ) -> tuple[Path, tuple[Path, Path, Path]]:
        snapshots = ["a" * 64, "b" * 64]
        reviewed_query_ids = ["query-001", "query-002"]
        inventory = self.write_artifact(
            root,
            SOURCE_INVENTORY_RELATIVE.as_posix(),
            {
                "critical_errors": [],
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
        return inventory, (review, *replays)

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
            jsonschema.validate(payload, schema)
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
            jsonschema.validate(payload, schema)
            self.assertEqual(receipt.read_bytes(), canonical_json_bytes(payload) + b"\n")

            failed = self.write_artifact(root, "failed.json", {"status": "FAIL"})
            quarantine = self.registry.quarantine_execution_transition(
                root,
                run_spec,
                attempted_phase="ACQUIRING",
                validation_receipts=(failed,),
            )
            jsonschema.validate(json.loads(quarantine.read_bytes()), schema)

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
            jsonschema.validate(json.loads(transition.read_bytes()), schema)

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
                    jsonschema.validate(payload, schema)

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
                jsonschema.validate(payload, schema)
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
            with self.assertRaisesRegex(ValueError, "dirty|binding|hash"):
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


if __name__ == "__main__":
    unittest.main()
