from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.run_task13_exact_suite import (
    CHILD_MODULES,
    ORIGINAL_PREFLIGHT_SHA256,
    OLD_EVIDENCE_RELATIVES,
    _assert_unchanged,
    _assert_fixture_namespaces,
    build_child_environment,
    build_child_command,
    clone_repo_at_head,
    copy_files_with_hashes,
    sha256_paths,
    validate_repository_root,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    )


class Task13ExactSuiteRunnerTest(unittest.TestCase):
    def test_fixture_namespace_guard_rejects_extra_source_or_design_historical_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            design = base / "design"
            for relative in OLD_EVIDENCE_RELATIVES:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture\n")
            _assert_fixture_namespaces(design, source)

            extra = source / "data/v5_2_qd/historical/extra.json"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source evidence"):
                _assert_fixture_namespaces(design, source)

            external = base / "external"
            source_alias = base / "source-alias"
            for relative in OLD_EVIDENCE_RELATIVES:
                path = external / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture\n")
            (source_alias / "data/v5_2_qd").mkdir(parents=True)
            (source_alias / "data/v5_2_qd/historical").symlink_to(
                external / "data/v5_2_qd/historical", target_is_directory=True
            )
            with self.assertRaisesRegex(ValueError, "source evidence"):
                _assert_fixture_namespaces(design, source_alias)

            extra.unlink()
            historical_root = design / "data/v5_2_qd/historical"
            historical_root.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, "design clone"):
                _assert_fixture_namespaces(design, source)
            historical_root.rmdir()
            historical_root.symlink_to(design / "missing-historical", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "design clone"):
                _assert_fixture_namespaces(design, source)

    def test_repository_root_must_match_runner_root_and_be_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            other = base / "other"
            for candidate in (repo, other):
                candidate.mkdir()
                _git(candidate, "init", "-q")
                _git(candidate, "config", "user.email", "runner@example.test")
                _git(candidate, "config", "user.name", "Runner Test")
                (candidate / "tracked.txt").write_text("tracked\n", encoding="utf-8")
                _git(candidate, "add", "tracked.txt")
                _git(candidate, "commit", "-qm", "fixture")

            validate_repository_root(repo, runner_root=repo)
            with self.assertRaisesRegex(ValueError, "runner root"):
                validate_repository_root(repo, runner_root=other)
            (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "clean"):
                validate_repository_root(repo, runner_root=repo)

    def test_original_preflight_hashes_are_pinned_literals(self) -> None:
        self.assertEqual(
            ORIGINAL_PREFLIGHT_SHA256,
            {
                "reports/v5_2_qd/walkforward_500_preflight.json":
                "8935c781a7984a244c510fb0018fa97ca8e0f225f708f9700723c44d0db0a082",
                "reports/v5_2_qd/walkforward_500_preflight.md":
                "672ad17ca67a018b9cf438c28d8544ff5c1cc24c1d0e197eef7d4f51020404bc",
            },
        )

    def test_fixture_hash_recheck_detects_post_child_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "evidence.raw"
            path.write_bytes(b"before\n")
            before = sha256_paths(root, ("evidence.raw",))
            path.write_bytes(b"after\n")
            with self.assertRaisesRegex(RuntimeError, "fixture input"):
                _assert_unchanged(before, root)

    def test_direct_script_help_works_without_external_pythonpath(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run(
            [
                sys.executable,
                str(repo_root / "scripts/run_task13_exact_suite.py"),
                "--help",
            ],
            cwd=repo_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--repo", completed.stdout)

    def test_clone_repo_and_copy_files_preserve_head_and_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo = base / "repo"
            repo.mkdir()
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "runner@example.test")
            _git(repo, "config", "user.name", "Runner Test")
            (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            _git(repo, "add", "tracked.txt")
            _git(repo, "commit", "-qm", "fixture")
            head = _git(repo, "rev-parse", "HEAD").stdout.strip()

            clone = clone_repo_at_head(repo, base / "clone")
            self.assertEqual(_git(clone, "rev-parse", "HEAD").stdout.strip(), head)

            source = base / "inputs"
            target = clone / "copied"
            (source / "nested").mkdir(parents=True)
            (source / "nested" / "input.bin").write_bytes(b"input\n")
            hashes = copy_files_with_hashes(
                source, target, (Path("nested/input.bin"),)
            )
            self.assertEqual(hashes, sha256_paths(target, (Path("nested/input.bin"),)))
            self.assertEqual((target / "nested/input.bin").read_bytes(), b"input\n")
            string_hashes = sha256_paths(source, ("nested/input.bin",))
            self.assertEqual(string_hashes, hashes)

    def test_child_command_and_environment_execute_real_child(self) -> None:
        self.assertEqual(
            build_child_command("python", modules=("tests.test_one",)),
            ["python", "-m", "unittest", "tests.test_one", "-v"],
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            design = base / "design"
            source = base / "source"
            design.mkdir()
            source.mkdir()
            script = (
                "import json, os, pathlib; "
                "print(json.dumps({'cwd': pathlib.Path.cwd().name, "
                "'source': os.environ['MLB_V52_SOURCE_AUTHORITY_ROOT'], "
                "'py': os.environ['PYTHONPATH'], 'count': len(__import__('sys').argv)-1}))"
            )
            command = build_child_command(
                sys.executable,
                modules=("tests.test_one", "tests.test_two"),
                probe=("-c", script),
            )
            environment = build_child_environment(design, source, base_environment={})
            completed = subprocess.run(
                command,
                cwd=design,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["cwd"], "design")
            self.assertEqual(payload["source"], str(source.resolve()))
            self.assertEqual(payload["py"], str(design.resolve()))
            self.assertEqual(payload["count"], 2)

    def test_exact_module_inventory_is_stable_and_contains_design_verifier(self) -> None:
        self.assertEqual(
            CHILD_MODULES,
            (
                "tests.test_v52_source_schedule",
                "tests.test_v52_evidence",
                "tests.test_v52_schedule_inventory",
                "tests.test_v52_legacy_decision_inventory",
                "tests.test_v52_timezone_review",
                "tests.test_v52_game_evidence_inventory",
                "tests.test_v52_query_plan",
                "tests.test_v52_verify_sources",
                "tests.test_v52_snapshot_store",
                "tests.test_execution_registry",
                "tests.test_v52_source_finalize",
                "tests.test_v52_source_history",
                "tests.test_feature_engine",
                "tests.test_protocols",
                "tests.test_v52_acquisition_contract",
                "tests.test_verify_v52_design",
            ),
        )


if __name__ == "__main__":
    unittest.main()
