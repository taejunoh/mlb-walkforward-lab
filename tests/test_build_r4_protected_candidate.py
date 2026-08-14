"""V2 transport bundle and review-package boundary tests using disposable repos."""
# ruff: noqa: E402

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"

sys.path.insert(0, str(SCRIPTS))
from r4_protected_fixed_git import (
    GIT_ENVIRONMENT,
    GIT_EXECUTABLE,
    GIT_PREFIX,
    RecordedGitCall,
)  # noqa: E402


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


candidate = load("build_r4_protected_candidate")
approval = load("build_r4_task14_approval")
inputs_verifier = sys.modules["verify_r4_protected_inputs"]


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


class RecordingRunner:
    def __init__(self, source: Path) -> None:
        self.source = source.resolve()
        self.calls: list[RecordedGitCall] = []

    def __call__(self, call: RecordedGitCall) -> bytes:
        self.assert_call(call)
        self.calls.append(call)
        args = call.argv[len(GIT_PREFIX) + 2 :]
        return subprocess.check_output([GIT_EXECUTABLE, "-C", str(self.source), *args])

    def assert_call(self, call: RecordedGitCall) -> None:
        assert call.executable == GIT_EXECUTABLE
        assert call.environment == GIT_ENVIRONMENT and call.stdin == "DEVNULL"
        assert call.argv[: len(GIT_PREFIX)] == GIT_PREFIX
        assert call.argv[len(GIT_PREFIX) : len(GIT_PREFIX) + 2] == (
            "-C",
            str(self.source),
        )


class BundleTests(unittest.TestCase):
    def test_required_blob_paths_remain_the_fixed_six(self) -> None:
        self.assertEqual(
            candidate.CANONICAL_REQUIRED_BLOB_PATHS,
            (
                "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md",
                "docs/superpowers/specs/2026-08-08-r4-protected-execution-authority-design.md",
                "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md",
                "mlb_backtest/v52_r4_disjoint_execution.py",
                "mlb_backtest/v52_r4_disjoint_issuer.py",
                "mlb_backtest/v52_r4_disjoint_policy.py",
            ),
        )

    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(
            [GIT_EXECUTABLE, *args], cwd=repo, check=True, stdout=subprocess.DEVNULL
        )

    def repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "config", "user.email", "test@example.invalid")
        self.git(repo, "config", "user.name", "Test")
        for path in candidate.CANONICAL_REQUIRED_BLOB_PATHS:
            file = repo / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"{path}\n", encoding="ascii")
        source_root = repo / approval.PROVISIONER_SOURCE_ROOT
        for source in sorted(
            (ROOT / approval.PROVISIONER_SOURCE_ROOT).rglob("*.swift")
        ):
            target = source_root / source.relative_to(
                ROOT / approval.PROVISIONER_SOURCE_ROOT
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", "fixture")
        return repo

    def inputs(self, root: Path) -> tuple[Path, Path, Path]:
        downloads = root / "downloads"
        downloads.mkdir()
        archive = downloads / "python.tar"
        archive.write_bytes(b"python")
        wheel = downloads / "wheel.whl"
        wheel.write_bytes(b"wheel")
        lock = {
            "kind": "R4_PROTECTED_PYTHON311_LOCK_V1",
            "python": {
                "version": "3.11.15",
                "url": "https://example.invalid/python",
                "filename": archive.name,
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            },
            "wheels": [
                {
                    "filename": wheel.name,
                    "url": "https://example.invalid/wheel",
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                }
            ],
        }
        lock_path = root / "lock.json"
        lock_path.write_bytes(canonical(lock) + b"\n")
        policy = root / "policy.json"
        policy.write_bytes((ROOT / "config" / "r4_privileged_materialization_policy.json").read_bytes())
        return downloads, lock_path, policy

    def test_bundle_is_v2_exhaustive_and_has_exact_required_six(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            runner = RecordingRunner(repo)
            bundle = candidate.build_input_bundle(
                repo, downloads, lock, policy, root / "bundle", runner=runner
            )
            self.assertEqual(bundle.manifest["kind"], "R4_RUNTIME_INPUT_BUNDLE_V2")
            self.assertEqual(
                tuple(row["path"] for row in bundle.required_blobs),
                candidate.CANONICAL_REQUIRED_BLOB_PATHS,
            )
            self.assertEqual(
                sorted(
                    path.relative_to(bundle.root / "checkout").as_posix()
                    for path in (bundle.root / "checkout").rglob("*")
                    if path.is_file()
                ),
                sorted(path for path in self.tracked(repo)),
            )
            self.assertEqual(len(runner.calls), 5 + len(self.tracked(repo)))

    def tracked(self, repo: Path) -> list[str]:
        return subprocess.check_output(
            [GIT_EXECUTABLE, "-C", str(repo), "ls-files"], text=True
        ).splitlines()

    def test_download_mismatch_and_unsorted_required_rows_reject(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            (downloads / "extra").write_bytes(b"x")
            with self.assertRaises(ValueError):
                candidate.build_input_bundle(
                    repo,
                    downloads,
                    lock,
                    policy,
                    root / "bundle",
                    runner=RecordingRunner(repo),
                )
            rows = [
                {"path": path, "blob": "a" * 40, "sha256": "b" * 64}
                for path in reversed(candidate.CANONICAL_REQUIRED_BLOB_PATHS)
            ]
            with self.assertRaises(ValueError):
                candidate.validate_required_blob_rows(rows)

    def test_git_tree_rejects_casefold_aliases_before_checkout(self) -> None:
        data = b"100644 blob aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\tCase\0" + b"100644 blob bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\tcase\0"
        with self.assertRaises(ValueError):
            candidate._parse_tree(data)

    def test_review_package_rebuilds_and_binds_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            bundle = candidate.build_input_bundle(
                repo,
                downloads,
                lock,
                policy,
                root / "bundle",
                runner=RecordingRunner(repo),
            )
            helpers = root / "helpers" / "bootstrap"
            helpers.mkdir(parents=True)
            (helpers / "provisioner").write_bytes(b"binary")
            (helpers / "provisioner.codesign-requirement.txt").write_bytes(
                b"requirement"
            )
            suite = root / "suite.log"
            suite.write_bytes(b"suite")
            value = approval.build_task14_review_package_v2(
                repo,
                bundle.root,
                helpers.parent,
                suite,
                root / "review-package.json",
                runner=RecordingRunner(repo),
            )
            self.assertEqual(
                value["kind"], "R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2"
            )
            self.assertEqual(value["input_bundle_manifest_sha256"], bundle.manifest["sha256"])
            self.assertEqual(
                value["task14_approval_verification_public_key_sha256"],
                "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1",
            )

    def test_review_package_rejects_swapped_artifact_and_checkout_entries(self) -> None:
        """A manifest hash alone must not authorize changed held bundle contents."""
        for target in ("artifacts/python.tar", "checkout/mlb_backtest/v52_r4_disjoint_policy.py"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = self.repo(root)
                downloads, lock, policy = self.inputs(root)
                bundle = candidate.build_input_bundle(
                    repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
                )
                changed = bundle.root / target
                for directory in (bundle.root, changed.parent):
                    directory.chmod(0o755)
                changed.chmod(0o644)
                changed.write_bytes(b"substituted")
                changed.chmod(0o444)
                for directory in (changed.parent, bundle.root):
                    directory.chmod(0o555)
                helpers = root / "helpers" / "bootstrap"
                helpers.mkdir(parents=True)
                (helpers / "provisioner").write_bytes(b"binary")
                (helpers / "provisioner.codesign-requirement.txt").write_bytes(b"requirement")
                suite = root / "suite.log"
                suite.write_bytes(b"suite")
                with self.assertRaises(ValueError):
                    approval.build_task14_review_package_v2(
                        repo, bundle.root, helpers.parent, suite, root / "review-package.json",
                        runner=RecordingRunner(repo),
                    )

    def test_review_package_rejects_aliased_artifact_and_output_swap(self) -> None:
        """Hardlinks and output-name swaps must not be accepted or deleted as owned."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            bundle = candidate.build_input_bundle(
                repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
            )
            artifact_root = bundle.root / "artifacts"
            artifact_root.chmod(0o755)
            original = artifact_root / "python.tar"
            original.chmod(0o644)
            alias = artifact_root / "python-alias.tar"
            os.link(original, alias)
            original.chmod(0o444)
            artifact_root.chmod(0o555)
            helpers = root / "helpers" / "bootstrap"
            helpers.mkdir(parents=True)
            (helpers / "provisioner").write_bytes(b"binary")
            (helpers / "provisioner.codesign-requirement.txt").write_bytes(b"requirement")
            suite = root / "suite.log"
            suite.write_bytes(b"suite")
            with self.assertRaises(ValueError):
                approval.build_task14_review_package_v2(
                    repo, bundle.root, helpers.parent, suite, root / "review-package.json",
                    runner=RecordingRunner(repo),
                )
            artifact_root.chmod(0o755)
            alias.unlink()
            artifact_root.chmod(0o555)
            output = root / "review-package.json"

            def swap_output(stage: str) -> None:
                if stage == "after_file_fsync":
                    output.unlink()
                    output.write_bytes(b"foreign")

            with patch.object(approval, "_review_package_output_hook", side_effect=swap_output):
                with self.assertRaises(ValueError):
                    approval.build_task14_review_package_v2(
                        repo, bundle.root, helpers.parent, suite, output,
                        runner=RecordingRunner(repo),
                    )
            self.assertEqual(output.read_bytes(), b"foreign")

    def test_review_package_output_fsync_failure_cleans_only_created_output(self) -> None:
        """A failed durability boundary must leave no self-created review package."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            bundle = candidate.build_input_bundle(
                repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
            )
            helpers = root / "helpers" / "bootstrap"
            helpers.mkdir(parents=True)
            (helpers / "provisioner").write_bytes(b"binary")
            (helpers / "provisioner.codesign-requirement.txt").write_bytes(b"requirement")
            suite = root / "suite.log"
            suite.write_bytes(b"suite")
            output = root / "review-package.json"
            with patch.object(approval.os, "fsync", side_effect=OSError("injected fsync")):
                with self.assertRaisesRegex(OSError, "injected fsync"):
                    approval.build_task14_review_package_v2(
                        repo, bundle.root, helpers.parent, suite, output,
                        runner=RecordingRunner(repo),
                    )
            self.assertFalse(output.exists())

    def test_reviewed_swift_key_a_pin_is_single_exact_and_closed(self) -> None:
        """A missing, duplicate, malformed, or changed compile-time pin must reject."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            records = candidate._git_evidence(repo, RecordingRunner(repo))[2]
            self.assertEqual(
                approval._reviewed_swift_key_a_pin(repo, records),
                "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1",
            )
            constants = repo / approval.PROVISIONER_SOURCE_ROOT / "Constants.swift"
            original = constants.read_text(encoding="utf-8")
            constants.write_text(original + "\n// unrecorded live mutation\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                approval._reviewed_swift_key_a_pin(repo, records)
            constants.write_text(original, encoding="utf-8")
            for label, replacement in (
                ("missing", original.replace("task14ApprovalVerificationPublicKeySHA256", "otherPin")),
                ("changed", original.replace("c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1", "0" * 64)),
                ("duplicate", original + '\npublic static let task14ApprovalVerificationPublicKeySHA256 = "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1"\n'),
                ("malformed", original.replace("c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1", "not-a-sha")),
            ):
                with self.subTest(label=label):
                    constants.write_text(replacement, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", label)
                    current = candidate._git_evidence(repo, RecordingRunner(repo))[2]
                    with self.assertRaises(ValueError):
                        approval._reviewed_swift_key_a_pin(repo, current)
                    constants.write_text(original, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"restore-{label}")

    def test_reviewed_swift_key_a_pin_ignores_comment_and_string_tricks(self) -> None:
        """Only one lexical Swift declaration, never comment/string text, may supply Key A."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            constants = repo / approval.PROVISIONER_SOURCE_ROOT / "Constants.swift"
            original = constants.read_text(encoding="utf-8")
            pin = "c22f28c189032c60329c3299b98a4623153d3c73449548f6cc62e5f9242ecfb1"
            ignored_tricks = (
                f'/* public static let task14ApprovalVerificationPublicKeySHA256 = "{pin}" */',
                f'let text = """public static let task14ApprovalVerificationPublicKeySHA256 = \\"{pin}\\"""',
                f'let raw = #"public static let task14ApprovalVerificationPublicKeySHA256 = "{pin}""#',
                f'let rawMultiline = #"""public static let task14ApprovalVerificationPublicKeySHA256 = "{pin}"""#',
                f'/* outer /* public static let task14ApprovalVerificationPublicKeySHA256 = "{pin}" */ outer */',
                "// #if false",
                'let directiveText = "#if false"',
            )
            rejected_tricks = (
                f'public static let task14ApprovalVerificationPublicKeySHA256 = "{pin}" + ""',
                f'public static let task14ApprovalVerificationPublicKeySHA256 = "\\({pin})"',
                'public static let task14ApprovalVerificationPublicKeySHA256 = #"not-a-sha"#',
            )
            for index, trick in enumerate(ignored_tricks):
                with self.subTest(trick=index):
                    constants.write_text(original + "\n" + trick, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"trick-{index}")
                    records = candidate._git_evidence(repo, RecordingRunner(repo))[2]
                    self.assertEqual(approval._reviewed_swift_key_a_pin(repo, records), pin)
                    constants.write_text(original, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"restore-trick-{index}")
            for index, trick in enumerate(rejected_tricks):
                with self.subTest(rejected=index):
                    constants.write_text(original.replace(pin, "0" * 64) + "\n" + trick, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"rejected-{index}")
                    records = candidate._git_evidence(repo, RecordingRunner(repo))[2]
                    with self.assertRaises(ValueError):
                        approval._reviewed_swift_key_a_pin(repo, records)
                    constants.write_text(original, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"restore-rejected-{index}")
            for index, directive in enumerate((
                "#if false\n#endif",
                "#if true\n#endif",
                "#if false\n#if true\n#endif\n#endif",
            )):
                with self.subTest(directive=index):
                    constants.write_text(original + "\n" + directive, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"directive-{index}")
                    records = candidate._git_evidence(repo, RecordingRunner(repo))[2]
                    with self.assertRaises(ValueError):
                        approval._reviewed_swift_key_a_pin(repo, records)
                    constants.write_text(original, encoding="utf-8")
                    self.git(repo, "add", str(constants.relative_to(repo)))
                    self.git(repo, "commit", "-qm", f"restore-directive-{index}")

    def test_task2b_to_task2c_streams_current_size_checkout_file(self) -> None:
        """The current 5,341,210-byte tracked report is valid held evidence."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            self.assertEqual(
                (ROOT / "reports/v5_2_qd/walkforward_500_preflight.json").stat().st_size,
                5_341_210,
            )
            large = repo / "reports/v5_2_qd/walkforward_500_preflight.json"
            large.parent.mkdir(parents=True)
            large.write_bytes(b"x" * 5_341_210)
            self.git(repo, "add", str(large.relative_to(repo)))
            self.git(repo, "commit", "-qm", "large checkout evidence")
            downloads, lock, policy = self.inputs(root)
            bundle = candidate.build_input_bundle(
                repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
            )
            self.assertEqual(approval._held_bundle_evidence(bundle.root)[0], bundle.manifest)

    def test_held_budget_rejects_depth_count_and_total_before_reading(self) -> None:
        """Traversal budget limits use checked arithmetic without oversized fixtures."""
        budget = approval._HeldTreeBudget(max_rows=1, max_depth=2, max_bytes=3)
        budget.add_row("checkout/a", 1)
        with self.assertRaises(ValueError):
            budget.add_row("checkout/b", 1)
        with self.assertRaises(ValueError):
            approval._HeldTreeBudget(max_rows=4, max_depth=1, max_bytes=3).add_row(
                "checkout/a/b", 0
            )
        with self.assertRaises(ValueError):
            approval._HeldTreeBudget(max_rows=4, max_depth=2, max_bytes=3).reserve(4)

    def test_verify_only_rejects_fifo_and_oversize_before_reading(self) -> None:
        """A nonregular or contract-oversize review package never enters a blocking read."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fifo = root / "review.fifo"
            os.mkfifo(fifo)
            with self.assertRaises(ValueError):
                approval.verify_task14_review_package_v2(fifo)
            oversized = root / "review.json"
            oversized.write_bytes(b"x" * (approval.MAX_REVIEW_PACKAGE_BYTES + 1))
            with self.assertRaises(ValueError):
                approval.verify_task14_review_package_v2(oversized)

    def test_held_bundle_rejects_fifo_and_artifact_oversize_before_reading(self) -> None:
        """Descriptor-held bundle reads reject nonregular and manifest-oversize inputs."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            bundle = candidate.build_input_bundle(
                repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
            )
            os.chmod(bundle.root, 0o755)
            lock_path = bundle.root / "r4_protected_python311.lock.json"
            lock_path.unlink()
            os.mkfifo(lock_path)
            os.chmod(bundle.root, 0o555)
            with self.assertRaises(ValueError):
                approval._held_bundle_evidence(bundle.root)

            os.chmod(bundle.root, 0o755)
            lock_path.unlink()
            lock_path.write_bytes(lock.read_bytes())
            os.chmod(lock_path, 0o444)
            artifacts = bundle.root / "artifacts"
            os.chmod(artifacts, 0o755)
            artifact = next(artifacts.iterdir())
            os.chmod(artifact, 0o644)
            artifact.write_bytes(artifact.read_bytes() + b"x")
            os.chmod(artifact, 0o444)
            os.chmod(artifacts, 0o555)
            os.chmod(bundle.root, 0o555)
            with self.assertRaises(ValueError):
                approval._held_bundle_evidence(bundle.root)

    def test_held_bundle_rejects_declared_size_mismatch_without_hash_reads(self) -> None:
        """Changing a declared size must fail before the held artifact or nested file is read."""
        for kind in ("artifact", "nested-checkout"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = self.repo(root)
                downloads, lock, policy = self.inputs(root)
                bundle = candidate.build_input_bundle(
                    repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
                )
                manifest_path = bundle.root / "R4_RUNTIME_INPUT_BUNDLE_V2.json"
                manifest = json.loads(manifest_path.read_bytes())
                if kind == "artifact":
                    row = manifest["artifacts"][0]
                    target = bundle.root / row["path"]
                else:
                    row = next(
                        entry
                        for entry in manifest["checkout_entries"]
                        if entry["type"] == "regular" and entry["path"].count("/") >= 2
                    )
                    target = bundle.root / row["path"]
                self.assertGreater(row["size"], 0)
                row["size"] = 0
                manifest["sha256"] = hashlib.sha256(
                    canonical({key: value for key, value in manifest.items() if key != "sha256"})
                ).hexdigest()
                manifest_path.chmod(0o644)
                manifest_path.write_bytes(canonical(manifest) + b"\n")
                manifest_path.chmod(0o444)

                target_identity = (target.stat().st_dev, target.stat().st_ino)
                target_reads: list[int] = []
                real_read = approval.os.read

                def observe_read(descriptor: int, count: int) -> bytes:
                    details = os.fstat(descriptor)
                    if (details.st_dev, details.st_ino) == target_identity:
                        target_reads.append(count)
                    return real_read(descriptor, count)

                with patch.object(approval.os, "read", side_effect=observe_read), self.assertRaises(ValueError):
                    approval._held_bundle_evidence(bundle.root)
                self.assertEqual(target_reads, [])

    def test_held_bundle_rejects_self_digested_boolean_sizes(self) -> None:
        """JSON booleans must never satisfy Task2c's integer size contracts."""
        for kind in ("checkout-file", "checkout-directory", "python-archive", "wheel"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                repo = self.repo(root)
                if kind == "checkout-file":
                    (repo / "one-byte").write_bytes(b"x")
                    self.git(repo, "add", "one-byte")
                    self.git(repo, "commit", "-qm", "one-byte checkout evidence")
                downloads, lock, policy = self.inputs(root)
                bundle = candidate.build_input_bundle(
                    repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
                )
                manifest_path = bundle.root / "R4_RUNTIME_INPUT_BUNDLE_V2.json"
                manifest = json.loads(manifest_path.read_bytes())
                if kind == "checkout-file":
                    row = next(
                        entry
                        for entry in manifest["checkout_entries"]
                        if entry["path"] == "checkout/one-byte"
                    )
                    row["size"] = True
                elif kind == "checkout-directory":
                    row = next(
                        entry
                        for entry in manifest["checkout_entries"]
                        if entry["path"] == "checkout"
                    )
                    row["size"] = False
                else:
                    index = 0 if kind == "python-archive" else 1
                    row = manifest["artifacts"][index]
                    target = bundle.root / row["path"]
                    for directory in (bundle.root, target.parent):
                        directory.chmod(0o755)
                    target.chmod(0o644)
                    target.write_bytes(b"x")
                    target.chmod(0o444)
                    for directory in (target.parent, bundle.root):
                        directory.chmod(0o555)
                    row["size"] = True
                    row["sha256"] = hashlib.sha256(b"x").hexdigest()
                manifest["sha256"] = hashlib.sha256(
                    canonical({key: value for key, value in manifest.items() if key != "sha256"})
                ).hexdigest()
                manifest_path.chmod(0o644)
                manifest_path.write_bytes(canonical(manifest) + b"\n")
                manifest_path.chmod(0o444)
                with self.assertRaises(ValueError):
                    approval._held_bundle_evidence(bundle.root)

    def test_held_budget_rejects_boolean_numeric_inputs(self) -> None:
        """Boolean values cannot become row, depth, byte, or declared-size budgets."""
        for kwargs in (
            {"max_rows": True, "max_depth": 1, "max_bytes": 1},
            {"max_rows": 1, "max_depth": True, "max_bytes": 1},
            {"max_rows": 1, "max_depth": 1, "max_bytes": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                approval._HeldTreeBudget(**kwargs)
        budget = approval._HeldTreeBudget(max_rows=2, max_depth=2, max_bytes=2)
        with self.assertRaises(ValueError):
            budget.add_row("checkout/file", True)
        with self.assertRaises(ValueError):
            budget.reserve(True)
        with self.assertRaises(ValueError):
            budget.observe(True)

    def test_checkout_casefold_name_guard_rejects_synthetic_aliases(self) -> None:
        """Case-sensitive volumes must reject aliases before descriptor recursion."""
        with self.assertRaises(ValueError):
            approval._require_unique_checkout_names(["Case", "case"])

    def test_verify_only_reads_an_existing_closed_review_package(self) -> None:
        """Adding an evidence field or rebuilding from inputs must fail this test."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            bundle = candidate.build_input_bundle(
                repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
            )
            helpers = root / "helpers" / "bootstrap"
            helpers.mkdir(parents=True)
            (helpers / "provisioner").write_bytes(b"binary")
            (helpers / "provisioner.codesign-requirement.txt").write_bytes(b"requirement")
            suite = root / "suite.log"
            suite.write_bytes(b"suite")
            review_package = root / "review-package.json"
            approval.build_task14_review_package_v2(
                repo, bundle.root, helpers.parent, suite, review_package, runner=RecordingRunner(repo)
            )
            before = review_package.read_bytes()

            self.assertEqual(
                approval.main(["--verify-only", "--review-package", str(review_package)]),
                0,
            )

            self.assertEqual(review_package.read_bytes(), before)
            value = json.loads(before)
            value["unexpected"] = "evidence"
            malformed = root / "malformed-review-package.json"
            malformed.write_bytes(canonical(value) + b"\n")
            with self.assertRaises(ValueError):
                approval.main(["--verify-only", "--review-package", str(malformed)])

    def test_review_package_rejects_a_v1_runtime_manifest_as_evidence(self) -> None:
        """Changing the claimed bundle from V2 to V1 must reject before Git reads."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            legacy_bundle = root / "legacy-bundle"
            legacy_bundle.mkdir()
            (legacy_bundle / "R4_RUNTIME_INPUT_BUNDLE_V2.json").write_bytes(
                canonical(
                    {
                        "kind": "R4_RUNTIME_INPUT_BUNDLE_V1",
                        "namespace": candidate.NAMESPACE,
                        "authority_id": candidate.AUTHORITY_ID,
                        "sha256": "0" * 64,
                    }
                )
                + b"\n"
            )
            with self.assertRaises(ValueError):
                approval.build_task14_review_package_v2(
                    repo,
                    legacy_bundle,
                    root,
                    root / "missing-suite.log",
                    root / "review-package.json",
                    runner=RecordingRunner(repo),
                )

    def test_output_race_preserves_foreign_destination_and_never_overwrites(self) -> None:
        """A destination appearing after preflight must win the no-replace race."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            output = root / "bundle"

            def lose_race(*_args, **_kwargs):
                output.mkdir()
                (output / "foreign").write_bytes(b"do-not-replace")
                raise FileExistsError("simulated competing publisher")

            with patch.object(candidate, "rename_exclusive_at", side_effect=lose_race):
                with self.assertRaises(FileExistsError):
                    candidate.build_input_bundle(repo, downloads, lock, policy, output, runner=RecordingRunner(repo))
            self.assertEqual((output / "foreign").read_bytes(), b"do-not-replace")

    def test_owned_cleanup_does_not_follow_or_delete_foreign_staging_swap(self) -> None:
        """Cleanup is identity-bound: a replaced staging name is left untouched."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / ".r4-input-bundle-owned"
            staging.mkdir()
            identity = candidate._directory_identity(staging)
            foreign = root / "foreign"
            foreign.mkdir()
            marker = foreign / "marker"
            marker.write_bytes(b"foreign")
            staging.rmdir()
            staging.symlink_to(foreign, target_is_directory=True)
            candidate._remove_owned_tree(staging, identity)
            self.assertTrue(staging.is_symlink())
            self.assertEqual(marker.read_bytes(), b"foreign")

    def test_owned_failure_cleanup_removes_only_captured_staging_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staging = root / ".r4-input-bundle-owned"
            nested = staging / "nested"
            nested.mkdir(parents=True)
            (nested / "payload").write_bytes(b"owned")
            identity = candidate._directory_identity(staging)
            candidate._remove_owned_tree(staging, identity)
            self.assertFalse(staging.exists())

    def test_committed_pretty_v1_lock_is_accepted_byte_for_byte(self) -> None:
        """The legacy V1 lock is byte-bound, not normalized into a V2 document."""
        lock = ROOT / "config" / "r4_protected_python311.lock.json"
        payload = lock.read_bytes()
        self.assertIn(b"\n", payload)
        parsed = candidate.verify_download_set.__globals__["decode_v1_lock_bytes"](payload)
        self.assertEqual(parsed["kind"], "R4_PROTECTED_PYTHON311_LOCK_V1")
        self.assertNotEqual(payload, canonical(parsed) + b"\n")

    def test_builder_uses_held_pretty_lock_bytes_without_a_path_reread(self) -> None:
        """The builder copies the verifier-held V1 bytes, not a second pathname read."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            downloads, lock, policy = self.inputs(root)
            pretty = json.dumps(json.loads(lock.read_bytes()), indent=2, sort_keys=True).encode("ascii") + b"\n"
            lock.write_bytes(pretty)
            with patch.object(inputs_verifier, "_COMMITTED_V1_LOCK", lock), patch.object(
                candidate, "_read_canonical", wraps=candidate._read_canonical
            ) as read_canonical:
                bundle = candidate.build_input_bundle(
                    repo, downloads, lock, policy, root / "bundle", runner=RecordingRunner(repo)
                )
            self.assertEqual((bundle.root / "r4_protected_python311.lock.json").read_bytes(), pretty)
            self.assertEqual(bundle.manifest["lock_sha256"], hashlib.sha256(pretty).hexdigest())
            self.assertEqual([call.args[0] for call in read_canonical.call_args_list], [policy])

    def test_held_lock_rejects_malformed_changed_and_rebound_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads, lock, _ = self.inputs(root)
            lock.write_bytes(b"not-json")
            with self.assertRaises(ValueError):
                inputs_verifier.verify_download_set(lock, downloads)

            (root / "changed").mkdir()
            _, lock, _ = self.inputs(root / "changed")
            lock.write_bytes(lock.read_bytes() + b" ")
            with self.assertRaises(ValueError):
                inputs_verifier.verify_download_set(lock, root / "changed" / "downloads")

            (root / "rebound").mkdir()
            _, lock, _ = self.inputs(root / "rebound")
            original_stat = inputs_verifier.os.stat
            def rebound(path, *args, **kwargs):
                result = original_stat(path, *args, **kwargs)
                if Path(path) == lock and kwargs.get("follow_symlinks") is False:
                    return type(result)((
                        result.st_mode, result.st_ino + 1, result.st_dev, result.st_nlink,
                        result.st_uid, result.st_gid, result.st_size, result.st_atime,
                        result.st_mtime, result.st_ctime,
                    ))
                return result
            with patch.object(inputs_verifier.os, "stat", side_effect=rebound), self.assertRaises(ValueError):
                inputs_verifier.verify_download_set(lock, root / "rebound" / "downloads")

    def test_canonical_v2_input_documents_accept_zero_or_one_terminal_lf(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            payload = canonical({"fixture": "value"})
            for suffix in [b"", b"\n"]:
                path.write_bytes(payload + suffix)
                self.assertEqual(candidate._read_canonical(path), payload + suffix)

    def test_provisioner_source_digest_rejects_missing_extra_casefold_and_live_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = self.repo(root)
            records = candidate._git_evidence(repo, RecordingRunner(repo))[2]
            self.assertEqual(
                approval._swift_source_digest(repo, records),
                approval._swift_source_digest(repo, records),
            )
            for label in ["missing", "extra", "casefold", "live-mismatch"]:
                with self.subTest(label=label):
                    isolated = root / label
                    subprocess.run([GIT_EXECUTABLE, "clone", "-q", str(repo), str(isolated)], check=True)
                    isolated_source = isolated / approval.PROVISIONER_SOURCE_ROOT
                    if label == "missing":
                        (isolated_source / "Adapters.swift").unlink()
                    elif label == "extra":
                        (isolated_source / "Extra.swift").write_text("// extra\n", encoding="ascii")
                    elif label == "casefold":
                        # APFS test volumes are commonly case-insensitive, so use the
                        # Git-record seam to model the otherwise unrepresentable alias.
                        (isolated_source / "Models.swift").write_text("// stable\n", encoding="ascii")
                    else:
                        (isolated_source / "Models.swift").write_text("// modified\n", encoding="ascii")
                    self.git(isolated, "add", "-A")
                    self.git(isolated, "commit", "-qm", label)
                    current = candidate._git_evidence(isolated, RecordingRunner(isolated))[2]
                    if label == "casefold":
                        alias = (
                            approval.PROVISIONER_SOURCE_ROOT + "/adapters.swift",
                            "100644",
                            "a" * 40,
                        )
                        current = sorted([*current, alias])
                    elif label == "live-mismatch":
                        current = records
                    with self.assertRaises(ValueError):
                        approval._swift_source_digest(isolated, current)

    def test_download_verify_cli_is_deterministic_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            downloads, lock, _ = self.inputs(root)
            before = {path.name: path.read_bytes() for path in [lock, *downloads.iterdir()]}
            command = [sys.executable, str(SCRIPTS / "verify_r4_protected_inputs.py"), "--lock", str(lock), "--downloads", str(downloads)]
            first = subprocess.run(command, check=True, capture_output=True, text=True)
            second = subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(first.stderr, "")
            self.assertEqual(json.loads(first.stdout)[0]["kind"], "python_archive")
            self.assertEqual(before, {path.name: path.read_bytes() for path in [lock, *downloads.iterdir()]})


if __name__ == "__main__":
    unittest.main()
