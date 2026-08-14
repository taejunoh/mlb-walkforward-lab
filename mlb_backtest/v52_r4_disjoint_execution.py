"""Descriptor-bound executable and Git checkout authority for disjoint R4.

This module deliberately has one public capture entry point.  In particular,
callers provide the already-preflighted target volume and supersession digest,
but cannot provide any executable, checkout, source, or approval digest.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .v52_r4_apfs import CUSTODY_UNAVAILABLE, apfs_volume_uuid_from_fd
from .v52_r4_authority import PRODUCTION_INVENTORY_SHA256


_GIT = "/usr/bin/git"
_GIT_TIMEOUT_SECONDS = 5.0
_GIT_STREAM_LIMIT_BYTES = 8 * 1024 * 1024
_GIT_READ_BYTES = 64 * 1024
_GIT_TERMINATE_WAIT_SECONDS = 0.25
_GIT_KILL_WAIT_SECONDS = 0.25
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_GIT_ID_RE = re.compile(r"[0-9a-f]{40}\Z")
_VOLUME_UUID_RE = re.compile(r"[0-9a-f]{32}\Z")
_REQUIRED_BLOBS = {
    "implementation": "mlb_backtest/v52_r4_disjoint_issuer.py",
    "policy": "mlb_backtest/v52_r4_disjoint_policy.py",
    "design": "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md",
    "plan": "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md",
}


@dataclass(frozen=True)
class ExecutableIdentity:
    """Complete regular-file identity retained in the signed binding."""

    volume_uuid: str
    device: int
    inode: int
    ctime_ns: int
    file_type: str
    mode: int
    uid: int
    nlink: int
    byte_size: int


@dataclass(frozen=True)
class ExecutionBinding:
    """The closed preflight fact that later verification can compare."""

    executable_sha256: str
    executable_identity: ExecutableIdentity
    repository_head: str
    repository_tree: str
    repository_clean: bool
    implementation_blob: str
    design_blob: str
    plan_blob: str
    source_approval_sha256: str
    target_volume_uuid: str
    supersedes_incomplete_attempt_sha256: str


@dataclass(frozen=True)
class _DirectoryIdentity:
    device: int
    inode: int
    ctime_ns: int
    mode: int
    nlink: int


@dataclass(frozen=True)
class _RegularIdentity:
    device: int
    inode: int
    ctime_ns: int
    mode: int
    nlink: int
    byte_size: int


@dataclass
class _SourceBinding:
    source: str
    blob: str
    expected_bytes: bytes
    fd: int
    identity: _RegularIdentity
    payload: bytes


class _GitSubprocessBound(Exception):
    """Git exceeded its fixed execution or output bounds."""


def _before_final_rebind() -> None:
    """A deliberately narrow test seam between capture and final rebinding."""


def _before_source_final_rebind() -> None:
    """A narrow test seam after the last Git/source read and before rebinding."""


def _require_hex(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CUSTODY_UNAVAILABLE(f"{label} is malformed")
    return value


def _safe_absolute_path(value: Path, label: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute() or any(
        part in {"", ".", ".."} for part in value.parts
    ):
        raise CUSTODY_UNAVAILABLE(f"{label} path differs")
    return value


def _directory_flags() -> int:
    required = ("O_RDONLY", "O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise CUSTODY_UNAVAILABLE("safe directory-open API is unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _regular_flags() -> int:
    required = ("O_RDONLY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        raise CUSTODY_UNAVAILABLE("safe regular-file-open API is unavailable")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_directory(path: Path) -> int:
    return _open_absolute(path, _directory_flags(), "repository directory")


def _open_regular(path: Path) -> int:
    return _open_absolute(path, _regular_flags(), "executable")


def _open_absolute(path: Path, final_flags: int, label: str) -> int:
    """Open every absolute component from root without following an alias."""
    current_fd = -1
    try:
        current_fd = os.open("/", _directory_flags())
        parts = path.parts[1:]
        if not parts:
            return current_fd
        for index, part in enumerate(parts):
            next_fd = os.open(
                part,
                final_flags if index == len(parts) - 1 else _directory_flags(),
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except (OSError, OverflowError) as exc:
        if current_fd >= 0:
            os.close(current_fd)
        raise CUSTODY_UNAVAILABLE(f"{label} is unavailable") from exc


def _directory_identity(fd: int) -> _DirectoryIdentity:
    try:
        observed = os.fstat(fd)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("repository descriptor is unavailable") from exc
    if not stat.S_ISDIR(observed.st_mode) or observed.st_ino <= 0 or observed.st_nlink <= 0:
        raise CUSTODY_UNAVAILABLE("repository directory differs")
    return _DirectoryIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        ctime_ns=observed.st_ctime_ns,
        mode=stat.S_IMODE(observed.st_mode),
        nlink=observed.st_nlink,
    )


def _rebind_directory(fd: int, expected: _DirectoryIdentity) -> None:
    if _directory_identity(fd) != expected:
        raise CUSTODY_UNAVAILABLE("repository descriptor identity differs")


def _regular_identity(fd: int, label: str) -> _RegularIdentity:
    try:
        observed = os.fstat(fd)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE(f"{label} descriptor is unavailable") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_ino <= 0 or observed.st_nlink != 1:
        raise CUSTODY_UNAVAILABLE(f"{label} identity differs")
    return _RegularIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        ctime_ns=observed.st_ctime_ns,
        mode=stat.S_IMODE(observed.st_mode),
        nlink=observed.st_nlink,
        byte_size=observed.st_size,
    )


def _capture_executable(fd: int) -> tuple[str, ExecutableIdentity]:
    try:
        observed = os.fstat(fd)
        volume_uuid = apfs_volume_uuid_from_fd(fd)
    except (OSError, OverflowError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("executable identity is unavailable") from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_ino <= 0
        or observed.st_nlink != 1
        or observed.st_size <= 0
        or observed.st_uid < 0
    ):
        raise CUSTODY_UNAVAILABLE("executable identity differs")
    identity = ExecutableIdentity(
        volume_uuid=_require_hex(volume_uuid, _VOLUME_UUID_RE, "executable volume UUID"),
        device=observed.st_dev,
        inode=observed.st_ino,
        ctime_ns=observed.st_ctime_ns,
        file_type="regular",
        mode=stat.S_IMODE(observed.st_mode),
        uid=observed.st_uid,
        nlink=observed.st_nlink,
        byte_size=observed.st_size,
    )
    digest = _hash_fd(fd)
    if _regular_identity(fd, "executable") != _RegularIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        ctime_ns=observed.st_ctime_ns,
        mode=stat.S_IMODE(observed.st_mode),
        nlink=observed.st_nlink,
        byte_size=observed.st_size,
    ):
        raise CUSTODY_UNAVAILABLE("executable changed while hashing")
    return digest, identity


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("descriptor hashing failed") from exc
    return digest.hexdigest()


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        os.lseek(fd, 0, os.SEEK_SET)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("source descriptor cannot be read") from exc
    return b"".join(chunks)


def _rebind_regular_at(
    parent_path: Path,
    name: str,
    held_fd: int,
    expected_digest: str,
    expected_identity: ExecutableIdentity,
) -> None:
    digest, identity = _capture_executable(held_fd)
    if digest != expected_digest or identity != expected_identity:
        raise CUSTODY_UNAVAILABLE("executable descriptor identity differs")
    parent_fd = _open_directory(parent_path)
    rebound_fd = -1
    try:
        rebound_fd = os.open(name, _regular_flags(), dir_fd=parent_fd)
        rebound_digest, rebound_identity = _capture_executable(rebound_fd)
        if rebound_digest != expected_digest or rebound_identity != expected_identity:
            raise CUSTODY_UNAVAILABLE("executable name binding differs")
        digest, identity = _capture_executable(held_fd)
        if digest != expected_digest or identity != expected_identity:
            raise CUSTODY_UNAVAILABLE("executable descriptor identity differs")
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("executable cannot be rebound") from exc
    finally:
        if rebound_fd >= 0:
            os.close(rebound_fd)
        os.close(parent_fd)


def _rebind_repository_at(
    parent_path: Path,
    name: str,
    held_fd: int,
    expected_parent: _DirectoryIdentity,
    expected_repository: _DirectoryIdentity,
) -> None:
    _rebind_directory(held_fd, expected_repository)
    parent_fd = _open_directory(parent_path)
    rebound_fd = -1
    try:
        if _directory_identity(parent_fd) != expected_parent:
            raise CUSTODY_UNAVAILABLE("repository parent identity differs")
        rebound_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        if _directory_identity(rebound_fd) != expected_repository:
            raise CUSTODY_UNAVAILABLE("repository name binding differs")
        _rebind_directory(held_fd, expected_repository)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("repository cannot be rebound") from exc
    finally:
        if rebound_fd >= 0:
            os.close(rebound_fd)
        os.close(parent_fd)


def _terminate_git_process_group(process: subprocess.Popen[bytes]) -> None:
    """Stop the isolated Git process group without trusting a child to exit."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        process.wait(timeout=_GIT_TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=_GIT_KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _bounded_git_output(
    command: list[str], repository: Path, environment: dict[str, str]
) -> tuple[int, bytes, bytes]:
    """Run Git in an isolated group with a deadline and independent stream caps."""
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    streams: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    completed = False
    try:
        process = subprocess.Popen(
            command,
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        for label, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            if pipe is None:
                raise OSError("Git output pipe is unavailable")
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)

        deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _GitSubprocessBound("Git exceeded its deadline")
            ready = selector.select(remaining)
            if not ready:
                raise _GitSubprocessBound("Git exceeded its deadline")
            for key, _ in ready:
                pipe = key.fileobj
                stream = streams[key.data]
                try:
                    payload = os.read(
                        pipe.fileno(),
                        min(_GIT_READ_BYTES, _GIT_STREAM_LIMIT_BYTES - len(stream) + 1),
                    )
                except BlockingIOError:
                    continue
                if not payload:
                    selector.unregister(pipe)
                    pipe.close()
                    continue
                if len(stream) + len(payload) > _GIT_STREAM_LIMIT_BYTES:
                    raise _GitSubprocessBound("Git output exceeded its stream limit")
                stream.extend(payload)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _GitSubprocessBound("Git exceeded its deadline")
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            raise _GitSubprocessBound("Git exceeded its deadline") from exc
        completed = True
        return returncode, bytes(streams["stdout"]), bytes(streams["stderr"])
    finally:
        selector.close()
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            if not completed:
                _terminate_git_process_group(process)


def _git(repository: Path, *arguments: str, allowed: frozenset[int] = frozenset({0})) -> bytes:
    """Run only the fixed system Git executable under a bounded authority-free env."""
    command = [
        _GIT,
        "--no-replace-objects",
        "--no-lazy-fetch",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "diff.external=false",
        "-c",
        "core.attributesfile=/dev/null",
        "-C",
        str(repository),
        *arguments,
    ]
    environment = {
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    try:
        returncode, stdout, stderr = _bounded_git_output(command, repository, environment)
    except (OSError, OverflowError, ValueError, _GitSubprocessBound) as exc:
        raise CUSTODY_UNAVAILABLE("Git plumbing is unavailable") from exc
    if returncode not in allowed or stderr:
        raise CUSTODY_UNAVAILABLE("Git plumbing failed")
    return stdout


def _single_line(payload: bytes, label: str) -> str:
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise CUSTODY_UNAVAILABLE(f"Git {label} output is ambiguous")
    try:
        return payload[:-1].decode("ascii")
    except UnicodeDecodeError as exc:
        raise CUSTODY_UNAVAILABLE(f"Git {label} output is malformed") from exc


def _git_identifier(repository: Path, argument: str, label: str) -> str:
    return _require_hex(_single_line(_git(repository, "rev-parse", "--verify", argument), label), _GIT_ID_RE, label)


def _require_checkout(repository: Path) -> tuple[str, str]:
    top_level = _single_line(_git(repository, "rev-parse", "--show-toplevel"), "top-level")
    try:
        same_top_level = os.path.samefile(top_level, repository)
    except (OSError, ValueError) as exc:
        raise CUSTODY_UNAVAILABLE("repository top-level is unavailable") from exc
    if not same_top_level:
        raise CUSTODY_UNAVAILABLE("repository path alias differs")
    head = _git_identifier(repository, "HEAD^{commit}", "HEAD")
    tree = _git_identifier(repository, "HEAD^{tree}", "HEAD tree")
    branch = _single_line(_git(repository, "symbolic-ref", "-q", "HEAD"), "symbolic HEAD")
    if not branch.startswith("refs/heads/") or len(branch) == len("refs/heads/"):
        raise CUSTODY_UNAVAILABLE("repository HEAD is detached")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"):
        raise CUSTODY_UNAVAILABLE("repository is not clean")
    return head, tree


def _head_blob(repository: Path, head: str, source: str) -> tuple[str, bytes]:
    tree_entry = _git(repository, "ls-tree", "-z", head, "--", source)
    expected_prefix = b"100644 blob "
    expected_suffix = b"\t" + source.encode("ascii") + b"\0"
    if not tree_entry.startswith(expected_prefix) or not tree_entry.endswith(expected_suffix):
        raise CUSTODY_UNAVAILABLE("required source is not a regular HEAD blob")
    blob_payload = tree_entry[len(expected_prefix) : -len(expected_suffix)]
    try:
        blob = blob_payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CUSTODY_UNAVAILABLE("required source blob output is malformed") from exc
    blob = _require_hex(blob, _GIT_ID_RE, "required source blob")
    return blob, _git(repository, "cat-file", "blob", f"{head}:{source}")


def _open_source(repository_fd: int, source: str) -> int:
    current_fd = os.dup(repository_fd)
    try:
        parts = source.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return os.open(parts[-1], _regular_flags(), dir_fd=current_fd)
    except (OSError, OverflowError) as exc:
        raise CUSTODY_UNAVAILABLE("required source is unavailable") from exc
    finally:
        os.close(current_fd)


def _capture_required_sources(
    repository: Path, repository_fd: int, head: str,
) -> dict[str, _SourceBinding]:
    bindings: dict[str, _SourceBinding] = {}
    try:
        for label, source in _REQUIRED_BLOBS.items():
            blob, expected_bytes = _head_blob(repository, head, source)
            fd = _open_source(repository_fd, source)
            try:
                identity = _regular_identity(fd, "required source")
                payload = _read_fd(fd)
                if _regular_identity(fd, "required source") != identity:
                    raise CUSTODY_UNAVAILABLE("required source changed while reading")
                if payload != expected_bytes:
                    raise CUSTODY_UNAVAILABLE("required source differs from its HEAD blob")
                bindings[label] = _SourceBinding(source, blob, expected_bytes, fd, identity, payload)
            except BaseException:
                os.close(fd)
                raise
        return bindings
    except BaseException:
        _close_sources(bindings)
        raise


def _rebind_source(repository_fd: int, binding: _SourceBinding) -> None:
    if _regular_identity(binding.fd, "required source") != binding.identity:
        raise CUSTODY_UNAVAILABLE("required source descriptor identity differs")
    if _read_fd(binding.fd) != binding.payload:
        raise CUSTODY_UNAVAILABLE("required source descriptor bytes differ")
    rebound_fd = _open_source(repository_fd, binding.source)
    try:
        if _regular_identity(rebound_fd, "required source") != binding.identity:
            raise CUSTODY_UNAVAILABLE("required source name binding differs")
        if _read_fd(rebound_fd) != binding.expected_bytes:
            raise CUSTODY_UNAVAILABLE("required source name bytes differ")
    finally:
        os.close(rebound_fd)


def _rebind_sources(repository_fd: int, bindings: dict[str, _SourceBinding]) -> None:
    for binding in bindings.values():
        _rebind_source(repository_fd, binding)


def _close_sources(bindings: dict[str, _SourceBinding]) -> None:
    for binding in bindings.values():
        try:
            os.close(binding.fd)
        except OSError:
            pass


def capture_execution_binding(
    repository: Path,
    executable: Path,
    *,
    target_volume_uuid: str,
    supersession_sha256: str,
) -> ExecutionBinding:
    """Capture the closed executable/check-out authority used before issuance.

    Every return field is either descriptor-derived, Git-plumbing-derived, or
    a fixed protected-authority constant.  The final descriptor/name rebinds
    make any observed executable or repository replacement fail closed.
    """
    repository = _safe_absolute_path(repository, "repository")
    executable = _safe_absolute_path(executable, "executable")
    target_volume_uuid = _require_hex(target_volume_uuid, _VOLUME_UUID_RE, "target volume UUID")
    supersession_sha256 = _require_hex(supersession_sha256, _SHA256_RE, "supersession SHA-256")
    repository_fd = executable_fd = -1
    sources: dict[str, _SourceBinding] = {}
    try:
        repository_parent_fd = _open_directory(repository.parent)
        try:
            repository_parent = _directory_identity(repository_parent_fd)
        finally:
            os.close(repository_parent_fd)
        repository_fd = _open_directory(repository)
        repository_identity = _directory_identity(repository_fd)

        executable_fd = _open_regular(executable)
        executable_sha256, executable_identity = _capture_executable(executable_fd)
        head, tree = _require_checkout(repository)
        sources = _capture_required_sources(repository, repository_fd, head)

        _before_final_rebind()
        if _require_checkout(repository) != (head, tree):
            raise CUSTODY_UNAVAILABLE("repository checkout changed during capture")
        _before_source_final_rebind()
        _rebind_sources(repository_fd, sources)
        _rebind_regular_at(
            executable.parent,
            executable.name,
            executable_fd,
            executable_sha256,
            executable_identity,
        )
        _rebind_repository_at(
            repository.parent,
            repository.name,
            repository_fd,
            repository_parent,
            repository_identity,
        )
        if _require_checkout(repository) != (head, tree):
            raise CUSTODY_UNAVAILABLE("repository checkout changed after final rebind")
        return ExecutionBinding(
            executable_sha256=executable_sha256,
            executable_identity=executable_identity,
            repository_head=head,
            repository_tree=tree,
            repository_clean=True,
            implementation_blob=sources["implementation"].blob,
            design_blob=sources["design"].blob,
            plan_blob=sources["plan"].blob,
            source_approval_sha256=PRODUCTION_INVENTORY_SHA256,
            target_volume_uuid=target_volume_uuid,
            supersedes_incomplete_attempt_sha256=supersession_sha256,
        )
    except CUSTODY_UNAVAILABLE:
        raise
    except (OSError, OverflowError, ValueError, TypeError) as exc:
        raise CUSTODY_UNAVAILABLE("execution authority is unavailable") from exc
    finally:
        _close_sources(sources)
        if executable_fd >= 0:
            os.close(executable_fd)
        if repository_fd >= 0:
            os.close(repository_fd)


__all__ = ["ExecutableIdentity", "ExecutionBinding", "capture_execution_binding"]
