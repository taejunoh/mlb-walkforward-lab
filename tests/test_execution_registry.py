from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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
RECEIPTS_RELATIVE = Path(
    "audit/v5_2_qd/historical/execution/phase_receipts"
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
        ):
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / RUN_SPEC_RELATIVE, root / RUN_SPEC_RELATIVE)
        shutil.copy2(ROOT / DESIGN_RELATIVE, root / DESIGN_RELATIVE)
        shutil.copy2(ROOT / PLAN_RELATIVE, root / PLAN_RELATIVE)
        shutil.copy2(ROOT / BASELINE_RELATIVE, root / BASELINE_RELATIVE)
        shutil.copy2(ROOT / PROTECTED_RELATIVE, root / PROTECTED_RELATIVE)
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

    def test_transition_binds_prior_receipt_commit_specs_and_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            initial = self.initialize(root, run_spec)
            inventory = self.write_artifact(root, "inventory.json", {"rows": 1})
            validation = self.write_artifact(root, "validation.json", {"status": "PASS"})
            state = self.registry.advance_execution_phase(
                root,
                run_spec,
                expected_phase="DESIGN_ACCEPTED",
                next_phase="ACQUIRING",
                inventory_path=inventory,
                validation_receipts=(validation,),
            )
            receipt_path = next(
                path for path in (root / RECEIPTS_RELATIVE).iterdir() if path.name.startswith("000001-")
            )
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["prior_receipt_sha256"], initial.receipt_sha256)
            self.assertEqual(payload["git_head"], _git(root, "rev-parse", "HEAD"))
            self.assertEqual(payload["run_spec"], {"path": RUN_SPEC_RELATIVE.as_posix(), "sha256": _sha256(root / RUN_SPEC_RELATIVE)})
            self.assertEqual(payload["inventory"], {"path": "inventory.json", "sha256": _sha256(inventory)})
            self.assertEqual(payload["validation_receipts"], [{"path": "validation.json", "sha256": _sha256(validation)}])
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
                    inv1 = self.write_artifact(root, "inv1.json", {"n": 1})
                    self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=inv1, validation_receipts=())
                    inv2 = self.write_artifact(root, "inv2.json", {"n": 2})
                    self.registry.advance_execution_phase(root, run_spec, expected_phase="ACQUIRING", next_phase="SOURCES_READY", inventory_path=inv2, validation_receipts=())
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
            inventory = self.write_artifact(root, "inventory.json", {"rows": 1})

            def transition():
                return self.registry.advance_execution_phase(root, run_spec, expected_phase="DESIGN_ACCEPTED", next_phase="ACQUIRING", inventory_path=inventory, validation_receipts=())

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: transition(), range(2)))
            self.assertEqual(outcomes[0], outcomes[1])
            self.assertEqual(len(list((root / RECEIPTS_RELATIVE).iterdir())), 2)

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
        for branch in schema["oneOf"]:
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["properties"]), set(branch["required"]))
        with tempfile.TemporaryDirectory() as directory:
            root, run_spec = self.make_repo(Path(directory))
            self.initialize(root, run_spec)
            receipt = next((root / RECEIPTS_RELATIVE).iterdir())
            payload = json.loads(receipt.read_bytes())
            self.assertEqual(receipt.read_bytes(), canonical_json_bytes(payload) + b"\n")

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
