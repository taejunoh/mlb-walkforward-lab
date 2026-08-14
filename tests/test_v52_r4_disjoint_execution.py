"""Authority binding tests using only disposable repositories and executables."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE


GIT = "/usr/bin/git"
UUID = "0123456789abcdef0123456789abcdef"
SUPERSESSION = "a" * 64
SOURCE_FILES = {
    "mlb_backtest/v52_r4_disjoint_issuer.py": b"issuer = 'approved'\n",
    "mlb_backtest/v52_r4_disjoint_policy.py": b"policy = 'approved'\n",
    "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md": b"# approved design\n",
    "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md": b"# approved plan\n",
}


def git(repository: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        [GIT, "-C", str(repository), *args],
        check=check,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.decode("utf-8").strip()


class ExecutionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init")
        git(self.repository, "config", "user.email", "r4@example.invalid")
        git(self.repository, "config", "user.name", "R4 Test")
        for relative, payload in SOURCE_FILES.items():
            destination = self.repository / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "approved source")
        self.executable = self.root / "python-exact"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def capture(self):
        from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

        return capture_execution_binding(
            self.repository,
            self.executable,
            target_volume_uuid=UUID,
            supersession_sha256=SUPERSESSION,
        )

    def _capture_with_test_volume(self):
        with patch(
            "mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd",
            return_value=UUID,
        ):
            return self.capture()

    def _write_git_program(self, name: str, body: str) -> Path:
        program = self.root / name
        program.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        program.chmod(0o700)
        return program

    def _assert_terminated(self, process_id: int) -> None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        self.fail(f"Git subprocess descendant {process_id} remained alive")

    def _assert_bounded_git_program_fails_closed(
        self, program: Path, child: Path | None, *, stream_limit: int
    ) -> None:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "\n".join(
                    (
                        "import sys",
                        "from pathlib import Path",
                        "from mlb_backtest import v52_r4_disjoint_execution as execution",
                        "from mlb_backtest.v52_r4_apfs import CUSTODY_UNAVAILABLE",
                        "execution._GIT = sys.argv[1]",
                        "execution._GIT_TIMEOUT_SECONDS = 0.5",
                        "execution._GIT_STREAM_LIMIT_BYTES = int(sys.argv[3])",
                        "execution._GIT_TERMINATE_WAIT_SECONDS = 0.05",
                        "execution._GIT_KILL_WAIT_SECONDS = 0.2",
                        "try:",
                        "    execution._git(Path(sys.argv[2]), 'probe')",
                        "except CUSTODY_UNAVAILABLE:",
                        "    print('CUSTODY')",
                        "else:",
                        "    print('ACCEPTED')",
                    )
                ),
                str(program),
                str(self.repository),
                str(stream_limit),
            ],
            cwd=Path(__file__).parents[1],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = worker.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            os.killpg(worker.pid, signal.SIGKILL)
            stdout, stderr = worker.communicate()

        self.assertEqual(0, worker.returncode, stderr.decode("utf-8", errors="replace"))
        self.assertEqual(b"CUSTODY\n", stdout)
        if child is not None:
            self.assertTrue(child.exists(), "test Git program did not record its descendant")
            self._assert_terminated(int(child.read_text(encoding="ascii")))

    def test_captures_exact_clean_executable_and_checkout_binding(self) -> None:
        binding = self._capture_with_test_volume()

        self.assertTrue(binding.repository_clean)
        self.assertEqual(
            git(self.repository, "rev-parse", "--verify", "HEAD^{commit}"),
            binding.repository_head,
        )
        self.assertEqual(git(self.repository, "rev-parse", "HEAD^{tree}"), binding.repository_tree)
        self.assertEqual(UUID, binding.target_volume_uuid)
        self.assertEqual(SUPERSESSION, binding.supersedes_incomplete_attempt_sha256)
        self.assertEqual(
            "2ec8bf0237cc5bdb00ddb6d2236ac631bb793aed9ba79f308672a9ecb90809c6",
            binding.source_approval_sha256,
        )
        self.assertEqual("regular", binding.executable_identity.file_type)
        self.assertEqual(UUID, binding.executable_identity.volume_uuid)
        self.assertEqual(1, binding.executable_identity.nlink)
        self.assertEqual(
            (
                "executable_sha256",
                "executable_identity",
                "repository_head",
                "repository_tree",
                "repository_clean",
                "implementation_blob",
                "design_blob",
                "plan_blob",
                "source_approval_sha256",
                "target_volume_uuid",
                "supersedes_incomplete_attempt_sha256",
            ),
            tuple(field.name for field in fields(binding)),
        )

    def test_rejects_detached_head(self) -> None:
        git(self.repository, "checkout", "--detach")
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_rejects_dirty_index(self) -> None:
        path = self.repository / "mlb_backtest/v52_r4_disjoint_issuer.py"
        path.write_bytes(b"issuer = 'staged mutation'\n")
        git(self.repository, "add", str(path.relative_to(self.repository)))
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_rejects_dirty_worktree(self) -> None:
        (self.repository / "mlb_backtest/v52_r4_disjoint_issuer.py").write_bytes(b"changed\n")
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_permits_ignored_files_while_rejecting_untracked_status(self) -> None:
        (self.repository / ".gitignore").write_text("hidden\n", encoding="utf-8")
        git(self.repository, "add", ".gitignore")
        git(self.repository, "commit", "-m", "ignore hidden")
        (self.repository / "hidden").write_text("untracked\n", encoding="utf-8")
        binding = self._capture_with_test_volume()
        self.assertTrue(binding.repository_clean)

    def test_disables_repository_configured_fsmonitor_command(self) -> None:
        marker = self.root / "fsmonitor-ran"
        git(self.repository, "config", "core.fsmonitor", f"touch {marker}")

        binding = self._capture_with_test_volume()

        self.assertTrue(binding.repository_clean)
        self.assertFalse(marker.exists())

    def test_disables_git_lazy_fetch_for_each_git_subprocess(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        record = self.root / "git-invocation"
        program = self._write_git_program(
            "git-inspect",
            f"printf '%s\\n' \"$@\" > {shlex.quote(str(record))}\n"
            f"printf 'lazy=%s\\n' \"$GIT_NO_LAZY_FETCH\" >> {shlex.quote(str(record))}\n"
            "printf 'ok\\n'\n",
        )
        with patch.object(execution, "_GIT", str(program)):
            self.assertEqual(b"ok\n", execution._git(self.repository, "rev-parse", "--git-dir"))

        invocation = record.read_text(encoding="utf-8").splitlines()
        self.assertIn("--no-lazy-fetch", invocation)
        self.assertIn("lazy=1", invocation)

    def test_rejects_timed_out_git_and_terminates_its_process_group(self) -> None:
        child = self.root / "timeout-child"
        program = self._write_git_program(
            "git-timeout",
            "/bin/sh -c 'while :; do /bin/sleep 1; done' &\n"
            f"printf '%s' \"$!\" > {shlex.quote(str(child))}\n"
            "while :; do /bin/sleep 1; done\n",
        )

        self._assert_bounded_git_program_fails_closed(program, child, stream_limit=64)

    def test_rejects_git_stdout_above_cap_and_terminates_its_process_group(self) -> None:
        child = self.root / "stdout-child"
        program = self._write_git_program(
            "git-stdout-overflow",
            "/bin/sh -c 'while :; do /bin/sleep 1; done' &\n"
            f"printf '%s' \"$!\" > {shlex.quote(str(child))}\n"
            "while :; do printf '0123456789abcdef'; done\n",
        )

        self._assert_bounded_git_program_fails_closed(program, child, stream_limit=64)

    def test_rejects_git_stderr_above_cap(self) -> None:
        program = self._write_git_program(
            "git-stderr-overflow",
            "while :; do printf '0123456789abcdef' >&2; done\n",
        )

        self._assert_bounded_git_program_fails_closed(program, None, stream_limit=64)

    def test_rejects_ordinary_untracked_file(self) -> None:
        (self.repository / "ordinary-untracked").write_text("untracked\n", encoding="utf-8")
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_rejects_dirty_submodule_even_when_repository_config_ignores_it(self) -> None:
        submodule = self.root / "submodule-source"
        submodule.mkdir()
        git(submodule, "init")
        git(submodule, "config", "user.email", "r4@example.invalid")
        git(submodule, "config", "user.name", "R4 Test")
        tracked = submodule / "tracked"
        tracked.write_text("approved\n", encoding="utf-8")
        git(submodule, "add", "tracked")
        git(submodule, "commit", "-m", "approved submodule source")
        git(
            self.repository,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(submodule),
            "child",
        )
        git(self.repository, "commit", "-m", "add child submodule")
        git(self.repository, "config", "submodule.child.ignore", "all")
        (self.repository / "child/tracked").write_text("changed\n", encoding="utf-8")

        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_rejects_required_source_missing_from_head_tree(self) -> None:
        git(self.repository, "rm", "mlb_backtest/v52_r4_disjoint_issuer.py")
        git(self.repository, "commit", "-m", "remove issuer")
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_rejects_policy_missing_from_head_tree(self) -> None:
        git(self.repository, "rm", "mlb_backtest/v52_r4_disjoint_policy.py")
        git(self.repository, "commit", "-m", "remove policy")
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def _assert_rejects_live_source_blob_mismatch(self, relative: str) -> None:
        path = self.repository / relative
        path.write_bytes(b"different design bytes\n")
        git(self.repository, "update-index", "--assume-unchanged", str(path.relative_to(self.repository)))
        self.addCleanup(
            lambda: git(
                self.repository,
                "update-index",
                "--no-assume-unchanged",
                str(path.relative_to(self.repository)),
                check=False,
            )
        )
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_rejects_implementation_that_differs_from_head_blob_despite_clean_status(self) -> None:
        self._assert_rejects_live_source_blob_mismatch("mlb_backtest/v52_r4_disjoint_issuer.py")

    def test_rejects_design_that_differs_from_head_blob_despite_clean_status(self) -> None:
        self._assert_rejects_live_source_blob_mismatch(
            "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md"
        )

    def test_rejects_plan_that_differs_from_head_blob_despite_clean_status(self) -> None:
        self._assert_rejects_live_source_blob_mismatch(
            "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md"
        )

    def test_rejects_policy_that_differs_from_head_blob_despite_clean_status(self) -> None:
        self._assert_rejects_live_source_blob_mismatch("mlb_backtest/v52_r4_disjoint_policy.py")

    def test_rejects_executable_symlink_path_alias(self) -> None:
        alias = self.root / "python-alias"
        alias.symlink_to(self.executable)
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

                capture_execution_binding(
                    self.repository, alias, target_volume_uuid=UUID, supersession_sha256=SUPERSESSION
                )

    def test_rejects_executable_intermediate_symlink_path_alias(self) -> None:
        alias_directory = self.root / "executable-parent-alias"
        alias_directory.symlink_to(self.root, target_is_directory=True)
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

                capture_execution_binding(
                    self.repository,
                    alias_directory / self.executable.name,
                    target_volume_uuid=UUID,
                    supersession_sha256=SUPERSESSION,
                )

    def test_rejects_repository_intermediate_symlink_path_alias(self) -> None:
        alias_directory = self.root / "repository-parent-alias"
        alias_directory.symlink_to(self.root, target_is_directory=True)
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

            with self.assertRaises(CUSTODY_UNAVAILABLE):
                capture_execution_binding(
                    alias_directory / self.repository.name,
                    self.executable,
                    target_volume_uuid=UUID,
                    supersession_sha256=SUPERSESSION,
                )

    def test_rejects_required_source_intermediate_symlink(self) -> None:
        source_directory = self.repository / "mlb_backtest"
        moved_directory = self.repository / "moved-source-directory"
        os.replace(source_directory, moved_directory)
        source_directory.symlink_to(moved_directory.name, target_is_directory=True)
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            with self.assertRaises(CUSTODY_UNAVAILABLE):
                self.capture()

    def test_required_source_descriptor_walk_rejects_intermediate_symlink(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        source_directory = self.repository / "mlb_backtest"
        moved_directory = self.repository / "moved-source-directory"
        os.replace(source_directory, moved_directory)
        source_directory.symlink_to(moved_directory.name, target_is_directory=True)
        repository_fd = execution._open_directory(self.repository)
        self.addCleanup(os.close, repository_fd)
        head = git(self.repository, "rev-parse", "HEAD")

        with self.assertRaises(CUSTODY_UNAVAILABLE):
            execution._capture_required_sources(self.repository, repository_fd, head)

    def test_uses_neither_path_nor_home_environment_as_authority(self) -> None:
        with patch.dict(os.environ, {"PATH": "/definitely/not/git", "HOME": "/nowhere"}, clear=False):
            binding = self._capture_with_test_volume()
        self.assertEqual(git(self.repository, "rev-parse", "HEAD^{commit}"), binding.repository_head)

    def test_rejects_malformed_target_volume_uuid(self) -> None:
        from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

        with self.assertRaises(CUSTODY_UNAVAILABLE):
            capture_execution_binding(
                self.repository,
                self.executable,
                target_volume_uuid="not-an-apfs-uuid",
                supersession_sha256=SUPERSESSION,
            )

    def test_rejects_malformed_supersession_sha256(self) -> None:
        from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

        with self.assertRaises(CUSTODY_UNAVAILABLE):
            capture_execution_binding(
                self.repository,
                self.executable,
                target_volume_uuid=UUID,
                supersession_sha256="A" * 64,
            )

    def test_rejects_unborn_head(self) -> None:
        unborn = self.root / "unborn"
        unborn.mkdir()
        git(unborn, "init")
        with patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID):
            from mlb_backtest.v52_r4_disjoint_execution import capture_execution_binding

            with self.assertRaises(CUSTODY_UNAVAILABLE):
                capture_execution_binding(
                    unborn, self.executable, target_volume_uuid=UUID, supersession_sha256=SUPERSESSION
                )

    def test_rejects_tree_head_at_commit_verification(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        tree = git(self.repository, "rev-parse", "HEAD^{tree}")
        (self.repository / ".git/HEAD").write_text(tree + "\n", encoding="ascii")
        observed_arguments: list[str] = []
        original_identifier = execution._git_identifier

        def record_identifier(repository: Path, argument: str, label: str) -> str:
            observed_arguments.append(argument)
            return original_identifier(repository, argument, label)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._git_identifier", side_effect=record_identifier),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()
        self.assertEqual(["HEAD^{commit}"], observed_arguments)

    def test_rejects_blob_head_at_commit_verification(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        blob = git(self.repository, "rev-parse", "HEAD:mlb_backtest/v52_r4_disjoint_issuer.py")
        (self.repository / ".git/HEAD").write_text(blob + "\n", encoding="ascii")
        observed_arguments: list[str] = []
        original_identifier = execution._git_identifier

        def record_identifier(repository: Path, argument: str, label: str) -> str:
            observed_arguments.append(argument)
            return original_identifier(repository, argument, label)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._git_identifier", side_effect=record_identifier),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()
        self.assertEqual(["HEAD^{commit}"], observed_arguments)

    def test_closes_retained_source_fd_when_later_head_blob_capture_fails(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        repository_fd = execution._open_directory(self.repository)
        self.addCleanup(os.close, repository_fd)
        head = git(self.repository, "rev-parse", "HEAD")
        original_open_source = execution._open_source
        original_head_blob = execution._head_blob
        opened: list[int] = []

        def record_open_source(fd: int, source: str) -> int:
            opened_fd = original_open_source(fd, source)
            opened.append(opened_fd)
            return opened_fd

        def fail_on_second_source(repository: Path, captured_head: str, source: str):
            if source == "mlb_backtest/v52_r4_disjoint_policy.py":
                raise CUSTODY_UNAVAILABLE("synthetic later source failure")
            return original_head_blob(repository, captured_head, source)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution._open_source", side_effect=record_open_source),
            patch("mlb_backtest.v52_r4_disjoint_execution._head_blob", side_effect=fail_on_second_source),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            execution._capture_required_sources(self.repository, repository_fd, head)
        self.assertEqual(1, len(opened))
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_ignores_git_replace_objects_when_authenticating_head_tree_and_blobs(self) -> None:
        base = git(self.repository, "rev-parse", "HEAD")
        policy = self.repository / "mlb_backtest/v52_r4_disjoint_policy.py"
        policy.write_bytes(b"policy = 'replacement object'\n")
        git(self.repository, "add", "mlb_backtest/v52_r4_disjoint_policy.py")
        git(self.repository, "commit", "-m", "replacement object")
        replacement = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "reset", "--hard", base)
        git(self.repository, "replace", base, replacement)

        binding = self._capture_with_test_volume()

        self.assertEqual(base, binding.repository_head)

    def test_rejects_executable_mutated_in_place_while_hashing(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        original_hash = execution._hash_fd
        mutated = False

        def mutate_then_hash(fd: int) -> str:
            nonlocal mutated
            if not mutated:
                mutated = True
                writer = os.open(self.executable, os.O_WRONLY)
                try:
                    os.write(writer, b"X")
                finally:
                    os.close(writer)
            return original_hash(fd)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._hash_fd", side_effect=mutate_then_hash),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_git_subprocess_failure(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch.object(execution, "_GIT", str(self.root / "missing-git")),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_ambiguous_git_output(self) -> None:
        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._git", return_value=b"two\nlines\n"),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_executable_replaced_at_final_rebind(self) -> None:
        replacement = self.root / "replacement"
        replacement.write_bytes(b"replacement executable\n")
        replacement.chmod(0o700)

        def replace_executable() -> None:
            os.replace(replacement, self.executable)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._before_final_rebind", side_effect=replace_executable),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_repository_replaced_at_final_rebind(self) -> None:
        moved = self.root / "moved-repository"
        replacement = self.root / "replacement-repository"

        def replace_repository() -> None:
            os.replace(self.repository, moved)
            replacement.mkdir()
            git(replacement, "init")
            os.replace(replacement, self.repository)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._before_final_rebind", side_effect=replace_repository),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_head_advanced_after_prior_checkout_to_identical_source_commit(self) -> None:
        from mlb_backtest import v52_r4_disjoint_execution as execution

        original_rebind = execution._rebind_regular_at
        advanced = False

        def rebind_then_advance(*args) -> None:
            nonlocal advanced
            original_rebind(*args)
            if not advanced:
                advanced = True
                git(self.repository, "commit", "--allow-empty", "-m", "same source new head")

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._rebind_regular_at", side_effect=rebind_then_advance),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_required_source_replaced_with_same_bytes_at_final_rebind(self) -> None:
        source = self.repository / "mlb_backtest/v52_r4_disjoint_policy.py"
        replacement = self.root / "replacement-policy"
        replacement.write_bytes(source.read_bytes())

        def replace_source() -> None:
            os.replace(replacement, source)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._before_final_rebind", side_effect=replace_source),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()

    def test_rejects_required_source_mutated_in_place_after_last_checkout_check(self) -> None:
        source = self.repository / "mlb_backtest/v52_r4_disjoint_policy.py"

        def mutate_source() -> None:
            writer = os.open(source, os.O_WRONLY)
            try:
                os.write(writer, b"X")
            finally:
                os.close(writer)

        with (
            patch("mlb_backtest.v52_r4_disjoint_execution.apfs_volume_uuid_from_fd", return_value=UUID),
            patch("mlb_backtest.v52_r4_disjoint_execution._before_source_final_rebind", side_effect=mutate_source),
            self.assertRaises(CUSTODY_UNAVAILABLE),
        ):
            self.capture()


if __name__ == "__main__":
    unittest.main()
