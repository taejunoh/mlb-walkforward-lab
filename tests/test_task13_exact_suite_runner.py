from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_task13_exact_suite import (
    CHILD_MODULES,
    ORIGINAL_PREFLIGHT_SHA256,
    RECOVERY_AUTHORITY_TREE_FINGERPRINTS,
    RECOVERY_AUTHORITY_TREE_RELATIVES,
    RECOVERY_PARENT_FILE_RELATIVES,
    _assert_unchanged,
    _assert_fixture_namespaces,
    build_child_environment,
    build_child_command,
    capture_recovery_parent_trees,
    clone_repo_at_head,
    chromium_bundle_sha256,
    copy_recovery_parent_trees,
    copy_files_with_hashes,
    provision_chromium_runtime,
    APPROVED_DESIGN_RELATIVE,
    _provision_approved_design_commit,
    _provision_v51_baseline_commit,
    sha256_paths,
    validate_chromium_runtime,
    validate_repository_root,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    )


_FIXTURE_TAG = "task13-fixture-tip-tag"
_FIXTURE_UNRELATED_BRANCH = "task13-fixture-unrelated"


def _make_historical_blob_repo(base: Path) -> tuple[Path, str, str]:
    """Create a tip-only fixture whose historical parent blob is unavailable."""
    repo = base / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "runner@example.test")
    _git(repo, "config", "user.name", "Runner Test")
    _git(repo, "config", "gc.auto", "0")
    (repo / "obsolete.txt").write_bytes(b"historical-only\n")
    _git(repo, "add", "obsolete.txt")
    _git(repo, "commit", "-qm", "parent")
    blob = _git(repo, "rev-parse", "HEAD:obsolete.txt").stdout.strip()
    (repo / "obsolete.txt").unlink()
    (repo / "current.txt").write_bytes(b"current tree\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "tip")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    source_branch = _git(
        repo, "symbolic-ref", "--quiet", "--short", "HEAD"
    ).stdout.strip()
    _git(repo, "tag", _FIXTURE_TAG, head)
    _git(repo, "branch", _FIXTURE_UNRELATED_BRANCH, head)
    _git(repo, "checkout", "-q", _FIXTURE_UNRELATED_BRANCH)
    (repo / "unrelated.txt").write_bytes(b"unrelated tree\n")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-qm", "unrelated")
    unrelated_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", source_branch)
    unrelated_file = repo / "unrelated.txt"
    if unrelated_file.exists():
        unrelated_file.unlink()
    self_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if self_head != head or unrelated_head == head:
        raise AssertionError("fixture refs changed the intended source HEAD")
    if _git(repo, "rev-parse", "--verify", f"refs/tags/{_FIXTURE_TAG}").stdout.strip() != head:
        raise AssertionError("fixture tag is not anchored at the source HEAD")
    if _git(repo, "rev-parse", "--verify", f"refs/heads/{_FIXTURE_UNRELATED_BRANCH}").stdout.strip() == head:
        raise AssertionError("fixture unrelated branch still points at source HEAD")
    loose_blob = repo / ".git" / "objects" / blob[:2] / blob[2:]
    if not loose_blob.is_file() or loose_blob.is_symlink():
        raise AssertionError("historical blob was not stored as a loose object")
    loose_blob.unlink()
    return repo, head, blob


def _write_recovery_parent_trees(root: Path) -> None:
    for relative in RECOVERY_AUTHORITY_TREE_RELATIVES:
        marker = root / relative / ".complete-tree-marker"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"tree\n")
    for relative in RECOVERY_PARENT_FILE_RELATIVES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture\n")


def _safe_chromium(base: Path) -> tuple[Path, str]:
    executable = base / "chrome-headless-shell"
    executable.write_bytes(b"test chromium runtime\n")
    executable.chmod(0o700)
    return executable, hashlib.sha256(executable.read_bytes()).hexdigest()


def _safe_chromium_app(
    base: Path,
    *,
    app_name: str = "Google Chrome.app",
    executable_name: str = "Google Chrome",
) -> tuple[Path, str]:
    executable = base / app_name / "Contents" / "MacOS" / executable_name
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"test chromium app launcher\n")
    executable.chmod(0o700)
    return executable, hashlib.sha256(executable.read_bytes()).hexdigest()


def _safe_chromium_bundle(base: Path) -> tuple[Path, str, str]:
    executable, digest = _safe_chromium(base)
    (base / "icudtl.dat").write_bytes(b"test ICU data\n")
    return executable, digest, chromium_bundle_sha256(base)


def _real_tempdir() -> str:
    return str(Path(tempfile.gettempdir()).resolve(strict=True))


def _replace_directory_with_symlink(directory: Path, target: Path) -> None:
    original = directory.with_name(f"{directory.name}-original")
    directory.rename(original)
    directory.symlink_to(target, target_is_directory=True)


class Task13ExactSuiteRunnerTest(unittest.TestCase):
    def _assert_fixture_refs(self, repo: Path, expected_head: str) -> None:
        self.assertEqual(
            _git(
                repo, "rev-parse", "--verify", f"refs/tags/{_FIXTURE_TAG}"
            ).stdout.strip(),
            expected_head,
        )
        unrelated_head = _git(
            repo,
            "rev-parse",
            "--verify",
            f"refs/heads/{_FIXTURE_UNRELATED_BRANCH}",
        ).stdout.strip()
        self.assertNotEqual(unrelated_head, expected_head)

    def _assert_independent_shallow_clone(
        self, destination: Path, expected_head: str
    ) -> None:
        self.assertEqual(
            _git(destination, "rev-parse", "--verify", "HEAD").stdout.strip(),
            expected_head,
        )
        self.assertEqual(
            _git(destination, "rev-parse", "--is-shallow-repository").stdout.strip(),
            "true",
        )
        self.assertEqual(
            _git(destination, "rev-list", "--count", "HEAD").stdout.strip(), "1"
        )
        symbolic_head = subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"],
            cwd=destination,
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(symbolic_head.returncode, 0)

        alternates = destination / ".git" / "objects" / "info" / "alternates"
        self.assertFalse(alternates.exists())
        refs = _git(
            destination, "for-each-ref", "--format=%(refname)"
        ).stdout.splitlines()
        self.assertTrue(refs, "destination refs are empty")
        self.assertEqual([], [ref for ref in refs if ref.startswith("refs/tags/")])
        self.assertNotIn(f"refs/tags/{_FIXTURE_TAG}", refs)
        self.assertNotIn(
            f"refs/heads/{_FIXTURE_UNRELATED_BRANCH}", refs
        )
        self.assertNotIn(
            f"refs/remotes/origin/{_FIXTURE_UNRELATED_BRANCH}", refs
        )
        for ref in refs:
            self.assertEqual(
                _git(destination, "rev-parse", "--verify", ref).stdout.strip(),
                expected_head,
            )

        objects = destination / ".git" / "objects"
        object_files = []
        for path in objects.rglob("*"):
            if path.is_symlink():
                self.fail(f"object storage contains symlink: {path}")
            if path.is_file():
                object_files.append(path)
                self.assertEqual(path.stat().st_nlink, 1, path)
        self.assertTrue(object_files, "destination object storage is empty")

    def test_external_parent_capability_module_is_in_exact_suite(self) -> None:
        self.assertIn("tests.test_v52_external_protected_parent", CHILD_MODULES)
        self.assertEqual(
            CHILD_MODULES.count("tests.test_v52_external_protected_parent"), 1
        )

    def test_fixture_namespace_guard_rejects_extra_source_or_design_historical_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / "source"
            design = base / "design"
            _write_recovery_parent_trees(source)
            source_fingerprints = capture_recovery_parent_trees(source)
            _assert_fixture_namespaces(
                design, source, expected_tree_fingerprints=source_fingerprints
            )

            extra = source / "data/v5_2_qd/historical/extra.json"
            extra.parent.mkdir(parents=True, exist_ok=True)
            extra.write_text("extra\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source evidence"):
                _assert_fixture_namespaces(
                    design, source, expected_tree_fingerprints=source_fingerprints
                )

            external = base / "external"
            source_alias = base / "source-alias"
            _write_recovery_parent_trees(external)
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

    def test_recovery_parent_tree_closure_is_captured_copied_and_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            source = base / "source"
            destination = base / "destination"
            _write_recovery_parent_trees(source)

            source_fingerprints = capture_recovery_parent_trees(source)
            copied_fingerprints = copy_recovery_parent_trees(source, destination)

            self.assertEqual(copied_fingerprints, source_fingerprints)
            self.assertEqual(
                set(source_fingerprints),
                {relative.as_posix() for relative in RECOVERY_AUTHORITY_TREE_RELATIVES},
            )
            self.assertEqual(
                set(RECOVERY_AUTHORITY_TREE_FINGERPRINTS),
                {relative.as_posix() for relative in RECOVERY_AUTHORITY_TREE_RELATIVES},
            )
            _assert_fixture_namespaces(
                base / "design",
                destination,
                expected_tree_fingerprints=source_fingerprints,
            )

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

    def test_chromium_runtime_requires_a_matching_explicit_pair(self) -> None:
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            executable, digest = _safe_chromium(Path(directory))
            self.assertEqual(
                validate_chromium_runtime(executable, digest), executable.resolve()
            )
            with self.assertRaisesRegex(ValueError, "required together"):
                validate_chromium_runtime(executable, None)
            with self.assertRaisesRegex(ValueError, "required together"):
                validate_chromium_runtime(None, digest)
            with self.assertRaisesRegex(ValueError, "required"):
                validate_chromium_runtime(None, None)

    def test_chromium_runtime_rejects_unsafe_or_mismatched_input(self) -> None:
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            executable, digest = _safe_chromium(base)
            directory_path = base / "directory"
            directory_path.mkdir()
            symlink = base / "symlink"
            symlink.symlink_to(executable)
            non_executable = base / "not-executable"
            non_executable.write_bytes(b"not executable\n")
            non_executable.chmod(0o600)
            ancestor_target = base / "ancestor-target"
            ancestor_target.mkdir()
            nested = ancestor_target / "nested-shell"
            nested.write_bytes(b"nested runtime\n")
            nested.chmod(0o700)
            ancestor_alias = base / "ancestor-alias"
            ancestor_alias.symlink_to(ancestor_target, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "sha256 differs"):
                validate_chromium_runtime(executable, "0" * 64)
            with self.assertRaisesRegex(ValueError, "sha256 must"):
                validate_chromium_runtime(executable, "A" * 64)
            linked = base / "linked"
            os.link(executable, linked)

            for candidate, candidate_digest in (
                (Path("relative-chromium"), digest),
                (base / "missing", digest),
                (directory_path, digest),
                (symlink, digest),
                (linked, digest),
                (non_executable, hashlib.sha256(non_executable.read_bytes()).hexdigest()),
                (
                    ancestor_alias / nested.name,
                    hashlib.sha256(nested.read_bytes()).hexdigest(),
                ),
            ):
                with self.subTest(candidate=candidate):
                    with self.assertRaisesRegex(ValueError, "chromium"):
                        validate_chromium_runtime(candidate, candidate_digest)

    def test_chromium_app_launchers_reject_before_hashing_or_snapshot_creation(self) -> None:
        import scripts.run_task13_exact_suite as runner_module

        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            candidates = (
                _safe_chromium_app(base, app_name="Google Chrome.app"),
                _safe_chromium_app(
                    base, app_name="Google Chrome Upper.APP", executable_name="Chrome"
                ),
                _safe_chromium_app(
                    base, app_name="Outer.APP/Contents/Framework.APP"
                ),
            )
            for index, (launcher, digest) in enumerate(candidates):
                with self.subTest(launcher=launcher):
                    runtime_root = base / f"runtime-app-{index}"
                    with (
                        patch.object(
                            runner_module,
                            "_chromium_metadata",
                            side_effect=AssertionError("metadata must not be read"),
                        ) as metadata,
                        patch.object(
                            runner_module,
                            "_sha256_chromium_executable",
                            side_effect=AssertionError("executable must not be hashed"),
                        ) as executable_hash,
                        self.assertRaisesRegex(
                            ValueError,
                            r"^chromium \.app bundle launchers are unsupported; provide a self-contained runtime directory$",
                        ),
                    ):
                        validate_chromium_runtime(launcher, digest)
                    metadata.assert_not_called()
                    executable_hash.assert_not_called()
                    with self.assertRaisesRegex(
                        ValueError,
                        r"^chromium \.app bundle launchers are unsupported; provide a self-contained runtime directory$",
                    ):
                        provision_chromium_runtime(
                            launcher,
                            digest,
                            "0" * 64,
                            runtime_root,
                        )
                    self.assertFalse(runtime_root.exists())

            ordinary_root = base / "self-contained"
            ordinary_root.mkdir()
            ordinary, ordinary_digest = _safe_chromium(ordinary_root)
            self.assertEqual(
                validate_chromium_runtime(ordinary, ordinary_digest), ordinary.resolve()
            )

            non_app_root = base / "safe.appdata" / "my.app.backup"
            non_app_root.mkdir(parents=True)
            non_app, non_app_digest = _safe_chromium(non_app_root)
            self.assertEqual(
                validate_chromium_runtime(non_app, non_app_digest), non_app.resolve()
            )

    def test_chromium_runtime_rejects_ancestor_swap_after_hash(self) -> None:
        import scripts.run_task13_exact_suite as runner_module

        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            approved = base / "approved"
            approved.mkdir()
            executable, digest = _safe_chromium(approved)
            malicious = base / "malicious"
            malicious.mkdir()
            _safe_chromium(malicious)
            real_hash = runner_module._sha256_chromium_executable

            def hash_then_swap(path: Path) -> str:
                observed = real_hash(path)
                _replace_directory_with_symlink(approved, malicious)
                return observed

            with patch.object(
                runner_module,
                "_sha256_chromium_executable",
                side_effect=hash_then_swap,
            ), self.assertRaisesRegex(ValueError, "symlink|changed"):
                validate_chromium_runtime(executable, digest)

    def test_provisioned_chromium_snapshot_survives_source_swap(self) -> None:
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            approved = base / "approved"
            approved.mkdir()
            executable, digest = _safe_chromium(approved)
            bundle_digest = chromium_bundle_sha256(approved)
            runtime = provision_chromium_runtime(
                executable, digest, bundle_digest, base / "runtime"
            )
            malicious = base / "malicious"
            malicious.mkdir()
            _safe_chromium(malicious)
            _replace_directory_with_symlink(approved, malicious)
            design = base / "design"
            source = base / "source"
            design.mkdir()
            source.mkdir()

            environment = build_child_environment(
                design,
                source,
                chromium_runtime=runtime,
                base_environment={"CHROMIUM_EXECUTABLE_PATH": "/hostile/chromium"},
            )

            self.assertEqual(
                environment["CHROMIUM_EXECUTABLE_PATH"], str(runtime.path)
            )
            self.assertNotEqual(runtime.path, executable)
            self.assertEqual(stat.S_IMODE(runtime.root.stat().st_mode), 0o700)
            self.assertEqual(runtime.path.read_bytes(), b"test chromium runtime\n")

    def test_child_environment_rejects_tampered_chromium_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            executable, digest = _safe_chromium(base)
            bundle_digest = chromium_bundle_sha256(base)
            runtime = provision_chromium_runtime(
                executable,
                digest,
                bundle_digest,
                base.parent / f"{base.name}-runtime",
            )
            runtime.path.write_bytes(b"tampered runtime\n")
            design = base / "design"
            source = base / "source"
            design.mkdir()
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "chromium snapshot"):
                build_child_environment(design, source, chromium_runtime=runtime)

    def test_chromium_bundle_rejects_manifest_mutation_and_unsafe_members(self) -> None:
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            bundle = base / "bundle"
            bundle.mkdir()
            executable, digest, bundle_digest = _safe_chromium_bundle(bundle)
            companion = bundle / "icudtl.dat"

            companion.write_bytes(b"tampered ICU data\n")
            with self.assertRaisesRegex(ValueError, "bundle sha256"):
                provision_chromium_runtime(
                    executable, digest, bundle_digest, base / "runtime-mutated"
                )
            companion.write_bytes(b"test ICU data\n")
            companion.unlink()
            with self.assertRaisesRegex(ValueError, "bundle sha256"):
                provision_chromium_runtime(
                    executable, digest, bundle_digest, base / "runtime-missing"
                )
            companion.write_bytes(b"test ICU data\n")
            (bundle / "unexpected.dat").write_bytes(b"extra\n")
            with self.assertRaisesRegex(ValueError, "bundle sha256"):
                provision_chromium_runtime(
                    executable, digest, bundle_digest, base / "runtime-extra"
                )
            (bundle / "unexpected.dat").unlink()
            (bundle / "linked.dat").symlink_to(companion)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                chromium_bundle_sha256(bundle)
            (bundle / "linked.dat").unlink()
            os.link(companion, bundle / "hard-linked.dat")
            with self.assertRaisesRegex(ValueError, "unsafe"):
                chromium_bundle_sha256(bundle)

    def test_chromium_bundle_fd_survives_source_ancestor_swap_after_open(self) -> None:
        import scripts.run_task13_exact_suite as runner_module

        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            approved = base / "approved"
            approved.mkdir()
            executable, digest, bundle_digest = _safe_chromium_bundle(approved)
            malicious = base / "malicious"
            malicious.mkdir()
            _safe_chromium_bundle(malicious)
            real_scan = runner_module._scan_chromium_bundle
            scan_count = 0

            def scan_then_swap(descriptor: int):
                nonlocal scan_count
                entries = real_scan(descriptor)
                if scan_count == 0:
                    _replace_directory_with_symlink(approved, malicious)
                scan_count += 1
                return entries

            with patch.object(
                runner_module,
                "_scan_chromium_bundle",
                side_effect=scan_then_swap,
            ):
                runtime = provision_chromium_runtime(
                    executable, digest, bundle_digest, base / "runtime"
                )

            self.assertEqual(runtime.path.read_bytes(), b"test chromium runtime\n")
            self.assertEqual((runtime.root / "icudtl.dat").read_bytes(), b"test ICU data\n")

    def test_child_environment_rejects_tampered_chromium_bundle_companion(self) -> None:
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            bundle = base / "bundle"
            bundle.mkdir()
            executable, digest, bundle_digest = _safe_chromium_bundle(bundle)
            runtime = provision_chromium_runtime(
                executable, digest, bundle_digest, base / "runtime"
            )
            (runtime.root / "icudtl.dat").write_bytes(b"tampered ICU data\n")
            design = base / "design"
            source = base / "source"
            design.mkdir()
            source.mkdir()

            with self.assertRaisesRegex(ValueError, "chromium snapshot"):
                build_child_environment(design, source, chromium_runtime=runtime)

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
        self.assertIn("--chromium-executable", completed.stdout)
        self.assertIn("--chromium-sha256", completed.stdout)
        self.assertIn("--chromium-bundle-sha256", completed.stdout)

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

    def test_clone_repo_at_head_uses_current_head_for_attached_source(self) -> None:
        """Catch full-history/shared-object clones that traverse a missing parent blob."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, head, blob = _make_historical_blob_repo(base)
            self._assert_fixture_refs(repo, head)

            full_clone = subprocess.run(
                [
                    "git",
                    "clone",
                    "--no-local",
                    "--no-tags",
                    repo.as_uri(),
                    str(base / "full-clone"),
                ],
                check=False,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(full_clone.returncode, 0)
            self.assertIn(blob, full_clone.stdout + full_clone.stderr)

            original_run = subprocess.run
            clone_argv: list[list[str]] = []

            def record_clone(command, *args, **kwargs):
                if command[:2] == ["git", "clone"]:
                    clone_argv.append(list(command))
                return original_run(command, *args, **kwargs)

            with patch(
                "scripts.run_task13_exact_suite.subprocess.run",
                side_effect=record_clone,
            ):
                clone = clone_repo_at_head(repo, base / "clone")
            self._assert_independent_shallow_clone(clone, head)
            self.assertEqual(len(clone_argv), 1)
            command = clone_argv[0]
            self.assertEqual(command[:2], ["git", "clone"])
            for flag in (
                "--no-local",
                "--no-checkout",
                "--no-tags",
                "--single-branch",
                "--depth=1",
            ):
                self.assertEqual(command.count(flag), 1)
            self.assertIn(repo.resolve().as_uri(), command)
            self.assertIn(str((base / "clone").resolve()), command)

    def test_clone_repo_at_head_uses_current_head_for_detached_source(self) -> None:
        """Catch helpers that preserve an attached branch instead of detaching HEAD."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, head, _ = _make_historical_blob_repo(base)
            self._assert_fixture_refs(repo, head)
            _git(repo, "checkout", "--detach", head)

            clone = clone_repo_at_head(repo, base / "clone")
            self._assert_independent_shallow_clone(clone, head)

    def test_design_clone_provisions_pinned_blob_without_changing_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = Path(__file__).resolve().parents[1]
            clone = clone_repo_at_head(repo_root, base / "design")
            head = _git(clone, "rev-parse", "--verify", "HEAD").stdout.strip()

            _provision_approved_design_commit(clone)

            self.assertEqual(
                _git(clone, "rev-parse", "--verify", "HEAD").stdout.strip(),
                head,
            )
            self.assertEqual(
                _git(clone, "rev-parse", "--is-shallow-repository").stdout.strip(),
                "true",
            )
            self.assertEqual(_git(clone, "rev-list", "--count", "HEAD").stdout.strip(), "1")
            _git(
                clone,
                "cat-file",
                "-e",
                f"fd04e8b:{APPROVED_DESIGN_RELATIVE.as_posix()}",
            )

    def test_design_clone_provisions_v51_baseline_tree_without_changing_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = Path(__file__).resolve().parents[1]
            clone = clone_repo_at_head(repo_root, base / "design")
            head = _git(clone, "rev-parse", "--verify", "HEAD").stdout.strip()

            _provision_v51_baseline_commit(clone)

            self.assertEqual(
                _git(clone, "rev-parse", "--verify", "HEAD").stdout.strip(),
                head,
            )
            self.assertEqual(
                _git(clone, "rev-parse", "--is-shallow-repository").stdout.strip(),
                "true",
            )
            self.assertEqual(_git(clone, "rev-list", "--count", "HEAD").stdout.strip(), "1")
            baseline = _git(
                clone,
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                "a4aa8d2",
                "--",
                "data/v5_1_bt",
                "audit/v5_1_bt",
                "config/v5_1_bt.json",
            )
            self.assertTrue(baseline.stdout)

    def test_design_clone_rejects_wrong_or_unavailable_pinned_design(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo_root = Path(__file__).resolve().parents[1]
            clone = clone_repo_at_head(repo_root, base / "design")

            with patch(
                "scripts.run_task13_exact_suite.CURRENT_APPROVED_DESIGN_COMMIT",
                "0" * 40,
            ):
                with self.assertRaisesRegex(RuntimeError, "historical"):
                    _provision_approved_design_commit(clone)

            with patch(
                "scripts.run_task13_exact_suite.V51_PROTECTED_BASELINE_COMMIT",
                "0" * 40,
            ):
                with self.assertRaisesRegex(RuntimeError, "historical"):
                    _provision_v51_baseline_commit(clone)

            with patch(
                "scripts.run_task13_exact_suite.APPROVED_DESIGN_RELATIVE",
                Path("docs/missing-approved-design.md"),
            ):
                with self.assertRaisesRegex(RuntimeError, "historical"):
                    _provision_approved_design_commit(clone)

    def test_clone_repo_at_head_fails_closed_when_source_head_changes_during_clone(
        self,
    ) -> None:
        """Catch clones that silently accept a source-head race."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, head, _ = _make_historical_blob_repo(base)
            original_run = subprocess.run

            def run_with_source_race(command, *args, **kwargs):
                if command[:2] == ["git", "clone"]:
                    (repo / "new-tip.txt").write_bytes(b"new tip\n")
                    original_run(
                        ["git", "-C", str(repo), "add", "new-tip.txt"],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                    original_run(
                        ["git", "-C", str(repo), "commit", "-qm", "race"],
                        check=True,
                        text=True,
                        capture_output=True,
                    )
                return original_run(command, *args, **kwargs)

            with patch(
                "scripts.run_task13_exact_suite.subprocess.run",
                side_effect=run_with_source_race,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    clone_repo_at_head(repo, base / "clone")

            self.assertNotEqual(_git(repo, "rev-parse", "HEAD").stdout.strip(), head)

    def test_child_command_and_environment_execute_real_child(self) -> None:
        self.assertEqual(
            build_child_command("python", modules=("tests.test_one",)),
            ["python", "-m", "unittest", "tests.test_one", "-v"],
        )
        with tempfile.TemporaryDirectory(dir=_real_tempdir()) as directory:
            base = Path(directory)
            design = base / "design"
            source = base / "source"
            hostile_recovery = base / "hostile-recovery"
            design.mkdir()
            source.mkdir()
            hostile_recovery.mkdir()
            chromium, chromium_sha256 = _safe_chromium(base)
            chromium_bundle_digest = chromium_bundle_sha256(base)
            runtime = provision_chromium_runtime(
                chromium,
                chromium_sha256,
                chromium_bundle_digest,
                base.parent / f"{base.name}-runtime",
            )
            script = (
                "import json, os, pathlib; "
                "print(json.dumps({'cwd': pathlib.Path.cwd().name, "
                "'source': os.environ['MLB_V52_SOURCE_AUTHORITY_ROOT'], "
                "'recovery': os.environ['MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT'], "
                "'py': os.environ['PYTHONPATH'], "
                "'chromium': os.environ['CHROMIUM_EXECUTABLE_PATH'], "
                "'legacy': os.environ.get('PLAYWRIGHT_EXECUTABLE_PATH'), "
                "'browsers': os.environ.get('PLAYWRIGHT_BROWSERS_PATH'), "
                "'count': len(__import__('sys').argv)-1}))"
            )
            command = build_child_command(
                sys.executable,
                modules=("tests.test_one", "tests.test_two"),
                probe=("-c", script),
            )
            environment = build_child_environment(
                design,
                source,
                base_environment={
                    "MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT": str(
                        hostile_recovery
                    ),
                    "CHROMIUM_EXECUTABLE_PATH": "/hostile/chromium",
                    "PLAYWRIGHT_EXECUTABLE_PATH": "/hostile/playwright",
                    "PLAYWRIGHT_BROWSERS_PATH": "/hostile/browsers",
                },
                chromium_runtime=runtime,
            )
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
            self.assertEqual(payload["recovery"], str(source.resolve()))
            self.assertEqual(payload["py"], str(design.resolve()))
            self.assertEqual(payload["chromium"], str(runtime.path))
            self.assertIsNone(payload["legacy"])
            self.assertIsNone(payload["browsers"])
            self.assertEqual(payload["count"], 2)

    def test_exact_module_inventory_is_stable_and_contains_design_verifier(self) -> None:
        self.assertEqual(
            CHILD_MODULES,
            (
                "tests.test_v52_source_schedule",
                "tests.test_v52_missing_venue_recovery_disposition",
                "tests.test_v52_recovery_r2_tdd",
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
                "tests.test_v52_external_protected_parent",
                "tests.test_verify_v52_design",
            ),
        )


if __name__ == "__main__":
    unittest.main()
