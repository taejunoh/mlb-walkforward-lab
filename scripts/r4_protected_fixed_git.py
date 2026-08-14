"""The only Task 2 subprocess boundary: fixed, local Git evidence reads."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

GIT_EXECUTABLE = "/usr/bin/git"
GIT_PREFIX = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
    "-c",
    "core.attributesfile=/dev/null",
    "-c",
    "diff.external=false",
    "--no-replace-objects",
    "--no-lazy-fetch",
)
GIT_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "file",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_EXTERNAL_DIFF": "",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
}
TIMEOUT_SECONDS = 30
OUTPUT_CAP = 4 * 1024 * 1024
GitRunner = Callable[["RecordedGitCall"], bytes]


@dataclass(frozen=True)
class RecordedGitCall:
    executable: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    stdin: str
    timeout_seconds: int
    output_cap: int


def _allowed(arguments: Sequence[str]) -> bool:
    fixed = {
        ("symbolic-ref", "-q", "HEAD"),
        ("status", "--porcelain=v1", "--ignored", "-z"),
        ("rev-parse", "--verify", "HEAD^{commit}"),
        ("rev-parse", "--verify", "HEAD^{tree}"),
        ("ls-tree", "-r", "-z", "--full-tree", "HEAD"),
    }
    return tuple(arguments) in fixed or (
        len(arguments) == 3
        and arguments[:2] == ("cat-file", "blob")
        and re.fullmatch(r"[0-9a-f]{40}", arguments[2]) is not None
    )


def _validate(call: RecordedGitCall) -> None:
    if call.executable != GIT_EXECUTABLE or call.environment != GIT_ENVIRONMENT:
        raise ValueError("non-fixed Git executable or environment")
    if (
        call.stdin != "DEVNULL"
        or call.timeout_seconds != TIMEOUT_SECONDS
        or call.output_cap != OUTPUT_CAP
    ):
        raise ValueError("non-fixed Git process limits")
    prefix_len = len(GIT_PREFIX)
    if call.argv[:prefix_len] != GIT_PREFIX or len(call.argv) <= prefix_len + 2:
        raise ValueError("non-fixed Git argv prefix")
    if call.argv[prefix_len] != "-C":
        raise ValueError("Git source must be supplied through -C")
    source = Path(call.argv[prefix_len + 1])
    try:
        canonical = str(source.resolve(strict=True))
    except OSError as exc:
        raise ValueError("Git source does not exist") from exc
    if (
        not source.is_absolute()
        or str(source) != canonical
        or not source.is_dir()
        or source.is_symlink()
    ):
        raise ValueError("Git source is not canonical")
    if not _allowed(call.argv[prefix_len + 2 :]):
        raise ValueError("Git evidence call is not allowlisted")


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Terminate a failed command's process group and always reap its leader.

    A command may have exited while a descendant still owns one of the output
    pipes.  In that case ``poll`` is non-``None`` but the group still needs to
    be drained/reaped; conversely a live group gets TERM followed by KILL only
    when the TERM grace period expires.  The helper deliberately leaves the
    original exception untouched.
    """
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # A second timeout would indicate a broken process adapter.  It is
            # still important to call wait so real children cannot be leaked.
            process.wait(timeout=1)
    else:
        # ``poll`` does not itself reap on every platform; wait is cheap and
        # makes the no-zombie guarantee explicit for both real and fake Popen.
        process.wait(timeout=1)


def _read_capped_pipes(
    process: subprocess.Popen[bytes], timeout: int, cap: int
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise ValueError("Git pipes unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    output: dict[object, bytearray] = {
        process.stdout: bytearray(),
        process.stderr: bytearray(),
    }
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("fixed Git command timed out")
            events = selector.select(remaining)
            if not events:
                raise TimeoutError("fixed Git command timed out")
            for key, _ in events:
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                output[key.fileobj].extend(block)
                if sum(len(value) for value in output.values()) > cap:
                    raise ValueError("fixed Git command exceeded output cap")
        remaining = max(0.001, deadline - time.monotonic())
        if process.wait(timeout=remaining) != 0:
            raise ValueError("fixed Git command failed")
        return bytes(output[process.stdout]), bytes(output[process.stderr])
    finally:
        selector.close()


def run_recorded_git(call: RecordedGitCall) -> bytes:
    _validate(call)
    process = subprocess.Popen(
        [call.executable, *call.argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd="/",
        env=dict(call.environment),
        shell=False,
        close_fds=True,
        start_new_session=True,
    )
    try:
        stdout, _ = _read_capped_pipes(process, call.timeout_seconds, call.output_cap)
        return stdout
    except BaseException:
        _terminate_group(process)
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def run_fixed_git(
    source: Path, arguments: Sequence[str], *, runner: GitRunner = run_recorded_git
) -> bytes:
    if not source.is_absolute() or source.is_symlink():
        raise ValueError("Git source must be an absolute non-symlink directory")
    normalized = str(source.resolve(strict=True))
    call = RecordedGitCall(
        GIT_EXECUTABLE,
        GIT_PREFIX + ("-C", normalized, *tuple(arguments)),
        dict(GIT_ENVIRONMENT),
        "DEVNULL",
        TIMEOUT_SECONDS,
        OUTPUT_CAP,
    )
    return runner(call)
