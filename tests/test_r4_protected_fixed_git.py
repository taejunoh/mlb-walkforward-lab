"""Contract tests for the sole fixed-Git subprocess adapter.

The builders receive a recording runner in these tests.  A call is accepted
only after every field and its position in the complete sequence match; the
runner then returns bytes from a precomputed, disposable-repository fixture.
No production command is spawned by the builder tests.
"""
# ruff: noqa: E402

from __future__ import annotations

import ast
import hashlib
import importlib.util
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest.mock import call as mock_call, patch

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from r4_protected_fixed_git import (  # noqa: E402
    GIT_ENVIRONMENT,
    GIT_EXECUTABLE,
    GIT_PREFIX,
    OUTPUT_CAP,
    TIMEOUT_SECONDS,
    RecordedGitCall,
    run_fixed_git,
    run_recorded_git,
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


candidate = _load("build_r4_protected_candidate")
approval = _load("build_r4_task14_approval")


def _canonical(value: object) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class ScriptedRecordingRunner:
    """Return fixture bytes only after exact ordered call equality succeeds."""

    def __init__(
        self,
        source: Path,
        expected_calls: Sequence[RecordedGitCall],
        fixture_bytes: dict[tuple[str, ...], bytes],
        *,
        scripted_calls: Sequence[RecordedGitCall] | None = None,
    ) -> None:
        self.source = source.resolve(strict=True)
        self.expected_calls = tuple(expected_calls)
        self.scripted_calls = self.expected_calls if scripted_calls is None else tuple(scripted_calls)
        self.fixture_bytes = fixture_bytes
        self.calls: list[RecordedGitCall] = []

    def __call__(self, call: RecordedGitCall) -> bytes:
        index = len(self.calls)
        # The canonical sequence is authoritative: a scripted list may omit,
        # reorder, or append calls only to prove that those mutations reject.
        if index >= len(self.expected_calls) or index >= len(self.scripted_calls):
            raise AssertionError(f"unexpected extra Git call at index {index}: {call!r}")
        expected = self.scripted_calls[index]
        observed_fields = (
            call.executable,
            call.argv,
            call.environment,
            call.stdin,
            call.timeout_seconds,
            call.output_cap,
        )
        expected_fields = (
            expected.executable,
            expected.argv,
            expected.environment,
            expected.stdin,
            expected.timeout_seconds,
            expected.output_cap,
        )
        if observed_fields != expected_fields:
            raise AssertionError(
                f"Git call {index} differs: {observed_fields!r} != {expected_fields!r}"
            )
        arguments = tuple(call.argv[len(GIT_PREFIX) + 2 :])
        try:
            payload = self.fixture_bytes[arguments]
        except KeyError as exc:
            raise AssertionError(f"no fixture for Git arguments {arguments!r}") from exc
        self.calls.append(call)
        return payload

    def assert_complete(self) -> None:
        self.assert_calls(self.expected_calls)

    def assert_calls(self, expected: Sequence[RecordedGitCall]) -> None:
        self.assertEqual(len(self.calls), len(expected))
        for index, (observed, wanted) in enumerate(zip(self.calls, expected)):
            self.assertEqual(observed, wanted, f"Git call {index} differs")

    def assertEqual(self, observed: object, wanted: object, message: str = "") -> None:
        if observed != wanted:
            raise AssertionError(message or f"{observed!r} != {wanted!r}")


class FakeGitProcess:
    """Minimal deterministic Popen double used only with patched Popen."""

    def __init__(self, waits: list[object], poll_result: int | None = None) -> None:
        self.pid = 731
        self.waits = waits
        self.poll_result = poll_result
        self.wait_calls: list[object] = []
        self.stdout = None
        self.stderr = None

    def poll(self) -> int | None:
        return self.poll_result

    def wait(self, timeout: int) -> int:
        self.wait_calls.append(timeout)
        result = self.waits.pop(0)
        if isinstance(result, BaseException):
            raise result
        return int(result)


class FixedGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")
        for path in candidate.CANONICAL_REQUIRED_BLOB_PATHS:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"fixture:{path}\n", encoding="ascii")
        source_root = self.repo / approval.PROVISIONER_SOURCE_ROOT
        for source in sorted((ROOT / approval.PROVISIONER_SOURCE_ROOT).rglob("*.swift")):
            target = source_root / source.relative_to(ROOT / approval.PROVISIONER_SOURCE_ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        self._git("add", ".")
        self._git("commit", "-qm", "fixture")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run([GIT_EXECUTABLE, *arguments], cwd=self.repo, check=True, stdout=subprocess.DEVNULL)

    def _expected_calls(self, *, review_package_calls: bool = False) -> tuple[tuple[RecordedGitCall, ...], dict[tuple[str, ...], bytes]]:
        source = str(self.repo.resolve(strict=True))
        fixed_args: tuple[tuple[str, ...], ...] = (
            ("symbolic-ref", "-q", "HEAD"),
            ("status", "--porcelain=v1", "--ignored", "-z"),
            ("rev-parse", "--verify", "HEAD^{commit}"),
            ("rev-parse", "--verify", "HEAD^{tree}"),
            ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
        )
        tree = subprocess.check_output(
            [GIT_EXECUTABLE, "-C", source, "ls-tree", "-r", "-z", "--full-tree", "HEAD"]
        )
        records = [record for record in tree.split(b"\0") if record]
        paths_and_blobs: list[tuple[str, str]] = []
        for record in records:
            metadata, raw_path = record.split(b"\t", 1)
            _mode, _kind, blob = metadata.decode("ascii").split(" ")
            paths_and_blobs.append((raw_path.decode("ascii"), blob))
        cat_paths = (
            [path for path, _ in paths_and_blobs]
            if not review_package_calls
            else list(candidate.CANONICAL_REQUIRED_BLOB_PATHS)
        )
        arguments = [*fixed_args, *(('cat-file', 'blob', blob) for path, blob in paths_and_blobs if path in cat_paths)]
        expected = tuple(
            RecordedGitCall(
                GIT_EXECUTABLE,
                GIT_PREFIX + ("-C", source, *args),
                dict(GIT_ENVIRONMENT),
                "DEVNULL",
                TIMEOUT_SECONDS,
                OUTPUT_CAP,
            )
            for args in arguments
        )
        fixture: dict[tuple[str, ...], bytes] = {}
        for args in fixed_args:
            fixture[args] = subprocess.check_output([GIT_EXECUTABLE, "-C", source, *args])
        for path, blob in paths_and_blobs:
            if path in cat_paths:
                fixture[("cat-file", "blob", blob)] = subprocess.check_output(
                    [GIT_EXECUTABLE, "-C", source, "cat-file", "blob", blob]
                )
        return expected, fixture

    def _inputs(self) -> tuple[Path, Path, Path]:
        root = Path(self.temp.name)
        downloads = root / "downloads"
        downloads.mkdir()
        archive = downloads / "python.tar"
        wheel = downloads / "wheel.whl"
        archive.write_bytes(b"python")
        wheel.write_bytes(b"wheel")
        lock = {
            "kind": "R4_PROTECTED_PYTHON311_LOCK_V1",
            "python": {"version": "3.11.15", "url": "https://example.invalid/python", "filename": archive.name, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
            "wheels": [{"filename": wheel.name, "url": "https://example.invalid/wheel", "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}],
        }
        lock_path = root / "lock.json"
        lock_path.write_bytes(_canonical(lock) + b"\n")
        policy = root / "policy.json"
        # The builder binds the V2 policy's self-digest, so use the committed
        # canonical fixture (including its required terminal LF) rather than a
        # placeholder object that cannot pass the transport schema.
        policy_bytes = (ROOT / "config" / "r4_privileged_materialization_policy.json").read_bytes()
        if not policy_bytes.endswith(b"\n"):
            raise AssertionError("canonical policy fixture must end with one LF")
        policy.write_bytes(policy_bytes)
        return downloads, lock_path, policy

    def test_real_fixed_git_and_every_field_is_equality_checked(self) -> None:
        output = run_fixed_git(self.repo, ("rev-parse", "--verify", "HEAD^{commit}"))
        self.assertRegex(output.decode("ascii"), r"^[0-9a-f]{40}\n$")
        good = RecordedGitCall(
            GIT_EXECUTABLE,
            GIT_PREFIX + ("-C", str(self.repo.resolve()), "rev-parse", "--verify", "HEAD^{commit}"),
            dict(GIT_ENVIRONMENT),
            "DEVNULL",
            TIMEOUT_SECONDS,
            OUTPUT_CAP,
        )
        invalid = (
            replace(good, executable="/usr/bin/env"),
            replace(good, argv=("status",)),
            replace(good, environment={"PATH": "/usr/bin:/bin"}),
            replace(good, stdin="PIPE"),
            replace(good, timeout_seconds=TIMEOUT_SECONDS + 1),
            replace(good, output_cap=OUTPUT_CAP - 1),
        )
        with patch("r4_protected_fixed_git.subprocess.Popen") as popen:
            for bad in invalid:
                with self.subTest(field=bad):
                    with self.assertRaises(ValueError):
                        run_recorded_git(bad)
            popen.assert_not_called()

    def test_task2b_and_task2c_record_complete_ordered_calls(self) -> None:
        downloads, lock, policy = self._inputs()
        expected, fixture = self._expected_calls()
        bundle_runner = ScriptedRecordingRunner(self.repo, expected, fixture)
        bundle = candidate.build_input_bundle(self.repo, downloads, lock, policy, Path(self.temp.name) / "bundle", runner=bundle_runner)
        bundle_runner.assert_complete()
        self.assertEqual(bundle.manifest["kind"], "R4_RUNTIME_INPUT_BUNDLE_V2")

        helper = Path(self.temp.name) / "helpers"
        (helper / "bootstrap").mkdir(parents=True)
        (helper / "bootstrap" / "provisioner").write_bytes(b"binary")
        (helper / "bootstrap" / "provisioner.codesign-requirement.txt").write_bytes(b"requirement")
        suite = Path(self.temp.name) / "suite.log"
        suite.write_bytes(b"suite")
        review_expected, review_fixture = self._expected_calls(review_package_calls=True)
        review_runner = ScriptedRecordingRunner(self.repo, review_expected, review_fixture)
        value = approval.build_task14_review_package_v2(self.repo, bundle.root, helper, suite, Path(self.temp.name) / "review-package.json", runner=review_runner)
        review_runner.assert_complete()
        self.assertEqual(value["kind"], "R4_TASK14_PRIVILEGED_MATERIALIZATION_REVIEW_PACKAGE_V2")

    def test_recording_rejects_omitted_extra_and_reordered_calls(self) -> None:
        expected, fixture = self._expected_calls()
        for label, sequence, fail_at_end in (
            ("omitted", expected[:-1], False),
            ("reordered", (expected[1], expected[0], *expected[2:]), True),
            ("extra", (*expected, expected[-1]), False),
        ):
            with self.subTest(sequence=label):
                runner = ScriptedRecordingRunner(self.repo, expected, fixture, scripted_calls=sequence)
                if label == "reordered":
                    with self.assertRaises(AssertionError):
                        runner(expected[0])
                else:
                    for call in expected:
                        if len(runner.calls) >= len(sequence):
                            break
                        runner(call)
                    if label == "extra":
                        with self.assertRaises(AssertionError):
                            runner(expected[-1])
                if fail_at_end:
                    with self.assertRaises(AssertionError):
                        runner.assert_complete()
                elif label == "omitted":
                    with self.assertRaises(AssertionError):
                        runner.assert_complete()

    def test_timeout_cap_read_error_and_nonzero_always_terminate_and_reap(self) -> None:
        call = RecordedGitCall(
            GIT_EXECUTABLE,
            GIT_PREFIX + ("-C", str(self.repo.resolve()), "status", "--porcelain=v1", "--ignored", "-z"),
            dict(GIT_ENVIRONMENT),
            "DEVNULL",
            TIMEOUT_SECONDS,
            OUTPUT_CAP,
        )
        for failure in (TimeoutError("timeout"), ValueError("cap"), OSError("read")):
            process = FakeGitProcess([subprocess.TimeoutExpired([GIT_EXECUTABLE], 1), 0])
            with self.subTest(failure=type(failure).__name__), patch("r4_protected_fixed_git.subprocess.Popen", return_value=process), patch("r4_protected_fixed_git._read_capped_pipes", side_effect=failure), patch("r4_protected_fixed_git.os.killpg") as killpg:
                with self.assertRaises(type(failure)):
                    run_recorded_git(call)
                self.assertEqual(killpg.call_args_list, [mock_call(731, signal.SIGTERM), mock_call(731, signal.SIGKILL)])
                self.assertGreaterEqual(len(process.wait_calls), 2)

        process = FakeGitProcess([7], poll_result=7)
        with patch("r4_protected_fixed_git.subprocess.Popen", return_value=process), patch("r4_protected_fixed_git._read_capped_pipes", side_effect=ValueError("nonzero")), patch("r4_protected_fixed_git.os.killpg") as killpg:
            with self.assertRaises(ValueError):
                run_recorded_git(call)
            killpg.assert_not_called()
            self.assertEqual(process.wait_calls, [1])

    def test_ast_subprocess_boundary_is_exclusive_and_network_free(self) -> None:
        forbidden_imports = {"subprocess", "socket", "urllib", "http", "tarfile"}
        forbidden_calls = {
            "os.system", "os.popen", "os.execv", "os.execve", "os.execl", "os.execvp",
            "os.spawnv", "os.spawnve", "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output",
        }
        for name in ("verify_r4_protected_inputs.py", "prepare_r4_protected_runtime.py", "build_r4_protected_candidate.py", "build_r4_task14_approval.py"):
            tree = ast.parse((SCRIPTS / name).read_text())
            imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
            imports |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
            calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
            self.assertFalse(imports & forbidden_imports, name)
            self.assertFalse(calls & forbidden_calls, name)
        tree = ast.parse((SCRIPTS / "r4_protected_fixed_git.py").read_text())
        imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        self.assertIn("subprocess", imports)
        self.assertFalse(imports & {"socket", "urllib", "http"})
        self.assertIn("subprocess.Popen", calls)
        self.assertFalse(calls & forbidden_calls - {"subprocess.Popen"})

    def test_task2c_has_only_the_review_package_and_fixed_git_boundary(self) -> None:
        """Replacing the review package with final approval material must fail this test."""
        tree = ast.parse((SCRIPTS / "build_r4_task14_approval.py").read_text())
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imports |= {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertFalse(imports & {"subprocess", "Security", "cryptography"})
        self.assertFalse(calls & {
            "subprocess.run", "subprocess.call", "subprocess.check_call",
            "subprocess.check_output", "os.system", "os.popen", "os.execv",
            "os.execve", "os.spawnv", "os.spawnve",
        })
        forbidden_literals = {
            "R4_TASK14_PRIVILEGED_MATERIALIZATION_APPROVAL_V2",
            "/usr/bin/sandbox-exec",
            "otool",
            "codesign",
            "Ed25519PrivateKey",
        }
        self.assertFalse(literals & forbidden_literals)
        identifiers = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        normalized = {
            value.replace("_", "").replace("-", "").lower()
            for value in {*literals, *identifiers}
        }
        self.assertFalse(any("keyb" in value for value in normalized))
        forbidden_execution_calls = {
            "tarfile.open", "zipfile.ZipFile", "shutil.unpack_archive",
            "pip.main", "runpy.run_path", "runpy.run_module", "exec", "eval",
            "os.execv", "os.execve", "os.execl", "os.execvp", "os.spawnv",
            "os.spawnve",
        }
        self.assertFalse(calls & forbidden_execution_calls)

    def test_task2c_ast_policy_rejects_direct_and_dynamic_execution_escapes(self) -> None:
        """Adding any dynamic import, reflection, native, or process escape is a bug."""
        source = (SCRIPTS / "build_r4_task14_approval.py").read_text()
        approval.validate_task2c_ast_policy(ast.parse(source))
        for escape in (
            "__import__('os')",
            "import importlib",
            "getattr(os, 'system')('true')",
            "os.__dict__['system']('true')",
            "import ctypes",
            "os.posix_spawn('/bin/true', [], {})",
            "from os import system\nsystem('true')",
            "from os import posix_spawn\nposix_spawn('/bin/true', [], {})",
            "from os import execv\nexecv('/bin/true', [])",
            "from os import execve\nexecve('/bin/true', [], {})",
            "from os import execl\nexecl('/bin/true', 'true')",
            "from os import system as harmless\nharmless('true')",
            "from builtins import __import__\n__import__('os')",
            "import tarfile\ntarfile.open('artifact.tar')",
            "import zipfile\nzipfile.ZipFile('wheel.whl')",
            "import pip\npip.main([])",
            "import runpy\nrunpy.run_path('interpreter.py')",
            "import subprocess\nsubprocess.run(['interpreter'])",
            "task14_authority_receipt_key_b = 'forbidden'",
        ):
            with self.subTest(escape=escape):
                with self.assertRaises(ValueError):
                    approval.validate_task2c_ast_policy(ast.parse(source + "\n" + escape))


if __name__ == "__main__":
    unittest.main()
