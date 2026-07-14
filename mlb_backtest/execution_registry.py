from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from mlb_backtest.diagnostics import canonical_json_bytes
from mlb_backtest.protocols import RunSpec


PHASES: tuple[str, ...] = (
    "DESIGN_ACCEPTED",
    "ACQUIRING",
    "SOURCES_READY",
    "OOF_SEALED",
    "CARDS_SEALED",
    "TARGET_LOCKED",
    "OUTCOMES_JOINED",
    "SAMPLE_500_SEALED",
    "VERIFIED",
)

_SUCCESS_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "run_id",
        "sequence",
        "phase",
        "prior_receipt_sha256",
        "recorded_at_utc",
        "git_head",
        "run_spec",
        "approved_design",
        "implementation_plan",
        "acquisition_contract",
        "task0_lineage",
        "inventory",
        "validation_receipts",
    }
)
_QUARANTINE_KEYS = frozenset(
    {
        "schema_version",
        "receipt_kind",
        "run_id",
        "attempted_phase",
        "current_phase",
        "current_receipt_sha256",
        "recorded_at_utc",
        "git_head",
        "run_spec",
        "validation_receipts",
    }
)
_BINDING_KEYS = frozenset({"path", "sha256"})
_PLAN_BINDING_KEYS = frozenset({"path", "sha256", "git_blob_id"})
_LINEAGE_KEYS = frozenset({"baseline_inventory", "protected_inventory"})
_SUCCESS_NAME = re.compile(
    r"^(?P<sequence>[0-9]{6})-(?P<phase>[A-Z0-9_]+)-(?P<sha>[0-9a-f]{64})\.json$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_HEAD = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$"
)
_BOUND_EXECUTION_CODE = (
    "mlb_backtest/execution_registry.py",
    "mlb_backtest/verify_v52_design.py",
    "schemas/v5_2_execution_receipt.schema.json",
)
_PHASE_INVENTORY_KEYS = frozenset(
    {
        "critical_errors",
        "inventory_kind",
        "run_id",
        "schema_version",
        "source_gate_ready",
        "status",
        "target_phase",
    }
)
_VALIDATION_EVIDENCE_KEYS = frozenset(
    {
        "critical_errors",
        "inventory_sha256",
        "run_id",
        "schema_version",
        "status",
        "validated_phase",
    }
)


@dataclass(frozen=True)
class ExecutionRegistryState:
    phase: str
    sequence: int
    receipt_sha256: str
    inventory_sha256: str


@dataclass(frozen=True)
class _RegistryPaths:
    root: Path
    execution_relative: Path
    receipts_relative: Path
    quarantine_relative: Path
    lock_relative: Path


@dataclass(frozen=True)
class _GitSnapshot:
    head: str
    file_sha256: tuple[tuple[str, str], ...]


def _exact_object(value: object, keys: frozenset[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} keys differ")
    return value


def _sha256_bytes(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _validate_artifact_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or ".." in Path(value).parts
    ):
        raise ValueError(f"{label} path violates the receipt schema")
    return value


def _validate_binding_value(
    value: object, label: str, *, plan: bool = False
) -> Mapping[str, object]:
    binding = _exact_object(
        value, _PLAN_BINDING_KEYS if plan else _BINDING_KEYS, label
    )
    _validate_artifact_path(binding.get("path"), label)
    digest = binding.get("sha256")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"{label} SHA-256 violates the receipt schema")
    if plan:
        blob = binding.get("git_blob_id")
        if not isinstance(blob, str) or not _GIT_HEAD.fullmatch(blob):
            raise ValueError(f"{label} Git blob violates the receipt schema")
    return binding


def _identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _root_path(root: Path) -> Path:
    supplied = Path(os.path.abspath(os.fspath(root)))
    try:
        for node in (*reversed(supplied.parents), supplied):
            metadata = node.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(
                    "execution registry root ancestor must be a no-follow directory"
                )
    except OSError as exc:
        raise ValueError("execution registry root is unavailable") from exc
    return supplied


def _paths(root: Path, run_spec: RunSpec) -> _RegistryPaths:
    root_path = _root_path(root)
    namespaces = run_spec.raw.get("namespaces")
    if not isinstance(namespaces, Mapping) or namespaces.get("audit") != "audit/v5_2_qd/historical":
        raise ValueError("execution registry requires the frozen historical audit namespace")
    execution = Path("audit/v5_2_qd/historical/execution")
    return _RegistryPaths(
        root=root_path,
        execution_relative=execution,
        receipts_relative=execution / "phase_receipts",
        quarantine_relative=execution / "quarantine",
        lock_relative=execution / ".registry.lock",
    )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _open_directory(root: Path, relative: Path, *, create: bool) -> tuple[int, list[int]]:
    descriptors: list[int] = []
    current = os.open(root, _directory_flags())
    descriptors.append(current)
    try:
        for component in relative.parts:
            if component in {"", ".", ".."}:
                raise ValueError("execution registry path traversal is forbidden")
            created = False
            try:
                child = os.open(component, _directory_flags(), dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(component, _directory_flags(), dir_fd=current)
            linked = os.stat(component, dir_fd=current, follow_symlinks=False)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(linked.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                os.close(child)
                raise ValueError("execution registry directory identity changed")
            if created:
                os.fsync(current)
            descriptors.append(child)
            current = child
        return current, descriptors
    except BaseException:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise


def _close_directories(descriptors: Sequence[int]) -> None:
    for descriptor in reversed(descriptors):
        os.close(descriptor)


def _relative_path(root: Path, path: Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        lexical = Path(os.path.abspath(candidate))
        try:
            lexical_relative = lexical.relative_to(root)
        except ValueError:
            lexical_relative = None
        if lexical_relative is not None:
            current = root
            for component in lexical_relative.parts:
                current = current / component
                component_stat = current.lstat()
                if stat.S_ISLNK(component_stat.st_mode):
                    raise ValueError(f"{label} symlink ancestor is forbidden")
        linked = candidate.lstat()
        if stat.S_ISLNK(linked.st_mode):
            raise ValueError(f"{label} symlink is forbidden")
        normalized = candidate.resolve(strict=True)
        relative = normalized.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{label} must be an existing path under repository root") from exc
    if not relative.parts:
        raise ValueError(f"{label} must be a file")
    return relative


def _read_fd_all(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _read_relative(root: Path, relative: Path, label: str) -> bytes:
    parent_fd, descriptors = _open_directory(root, relative.parent, create=False)
    descriptor = -1
    try:
        before = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"{label} must be an unaliased regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(relative.name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise ValueError(f"{label} identity changed before read")
        contents = _read_fd_all(descriptor)
        after = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        final_opened = os.fstat(descriptor)
        if _identity(after) != _identity(before) or _identity(final_opened) != _identity(before):
            raise ValueError(f"{label} identity changed during read")
        return contents
    except OSError as exc:
        raise ValueError(f"{label} is unsafe or unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directories(descriptors)


def _read_path(root: Path, path: Path, label: str) -> tuple[str, bytes]:
    relative = _relative_path(root, path, label)
    return relative.as_posix(), _read_relative(root, relative, label)


def _canonical_object(contents: bytes, label: str) -> Mapping[str, object]:
    try:
        payload = json.loads(contents)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not JSON") from exc
    if not isinstance(payload, Mapping) or canonical_json_bytes(payload) + b"\n" != contents:
        raise ValueError(f"{label} is not canonical closed JSON with one newline")
    return payload


def _phase_evidence_payload(
    contents: bytes, *, phase: str, run_id: str, inventory_sha256: str | None
) -> Mapping[str, object]:
    label = "phase inventory" if inventory_sha256 is None else "validation receipt"
    payload = _canonical_object(contents, label)
    if inventory_sha256 is None:
        _exact_object(payload, _PHASE_INVENTORY_KEYS, label)
        if (
            payload.get("schema_version") != 1
            or payload.get("inventory_kind") != "EXECUTION_PHASE_INVENTORY"
            or payload.get("run_id") != run_id
            or payload.get("target_phase") != phase
            or payload.get("status") != "PASS"
            or payload.get("critical_errors") != []
            or payload.get("source_gate_ready") is not (phase == "SOURCES_READY")
        ):
            raise ValueError("phase inventory validation failed")
    else:
        _exact_object(payload, _VALIDATION_EVIDENCE_KEYS, label)
        if (
            payload.get("schema_version") != 1
            or payload.get("run_id") != run_id
            or payload.get("validated_phase") != phase
            or payload.get("inventory_sha256") != inventory_sha256
            or payload.get("status") != "PASS"
            or payload.get("critical_errors") != []
        ):
            raise ValueError("validation receipt failed")
    return payload


def _binding(root: Path, path: Path, label: str) -> dict[str, str]:
    relative, contents = _read_path(root, path, label)
    _validate_artifact_path(relative, label)
    return {"path": relative, "sha256": _sha256_bytes(contents)}


def _git(root: Path, *args: str) -> str:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", *args], cwd=root, check=False, capture_output=True, env=environment
    )
    if completed.returncode:
        raise ValueError(completed.stderr.decode(errors="replace").strip() or "git evidence unavailable")
    return completed.stdout.decode().strip()


def _require_tracked_clean(root: Path, relative: str, label: str) -> None:
    _git(root, "ls-files", "--error-unmatch", "--", relative)
    status_output = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--", relative)
    if status_output:
        raise ValueError(f"{label} is dirty")
    _git(root, "rev-parse", f"HEAD:{relative}")


def _require_bound_execution_code_clean(root: Path) -> None:
    for relative in _BOUND_EXECUTION_CODE:
        _read_relative(root, Path(relative), "bound execution code")
        _require_tracked_clean(root, relative, "bound execution code")


def _git_bound_paths(evidence: Mapping[str, object]) -> tuple[str, ...]:
    paths = list(_BOUND_EXECUTION_CODE)
    for key in (
        "run_spec",
        "approved_design",
        "implementation_plan",
        "acquisition_contract",
    ):
        binding = _validate_binding_value(
            evidence[key], key, plan=key == "implementation_plan"
        )
        paths.append(str(binding["path"]))
    lineage = _exact_object(
        evidence["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage"
    )
    for key in ("baseline_inventory", "protected_inventory"):
        paths.append(
            str(_validate_binding_value(lineage[key], key)["path"])
        )
    return tuple(dict.fromkeys(paths))


def _capture_git_snapshot(
    root: Path, evidence: Mapping[str, object]
) -> _GitSnapshot:
    head = _git(root, "rev-parse", "HEAD")
    bindings: list[tuple[str, str]] = []
    for relative in _git_bound_paths(evidence):
        _require_tracked_clean(root, relative, "bound Git evidence")
        binding = _binding(root, root / relative, "bound Git evidence")
        bindings.append((relative, binding["sha256"]))
    if _git(root, "rev-parse", "HEAD") != head:
        raise ValueError("Git HEAD changed during trusted snapshot")
    return _GitSnapshot(head=head, file_sha256=tuple(bindings))


def _verify_git_snapshot(root: Path, snapshot: _GitSnapshot) -> None:
    if _git(root, "rev-parse", "HEAD") != snapshot.head:
        raise ValueError("Git HEAD changed after trusted snapshot")
    for relative, digest in snapshot.file_sha256:
        _require_tracked_clean(root, relative, "bound Git evidence")
        if _binding(root, root / relative, "bound Git evidence")["sha256"] != digest:
            raise ValueError("bound Git file identity changed after trusted snapshot")
    if _git(root, "rev-parse", "HEAD") != snapshot.head:
        raise ValueError("Git HEAD changed after trusted snapshot")


def _before_registry_publish() -> None:
    """Deterministic race-test hook; production intentionally does nothing."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_timestamp(value: object, label: str) -> None:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError(f"{label} timestamp differs")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} timestamp differs") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{label} timestamp differs")


def _write_exclusive(root: Path, relative: Path, contents: bytes, label: str) -> None:
    parent_fd, descriptors = _open_directory(root, relative.parent, create=False)
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(relative.name, flags, 0o600, dir_fd=parent_fd)
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("receipt write made no progress")
            view = view[written:]
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} publication was aliased")
        os.fsync(descriptor)
        linked = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(linked) != _identity(metadata):
            raise ValueError(f"{label} identity changed during publication")
        os.fsync(parent_fd)
    except FileExistsError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} could not be published safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_directories(descriptors)


def _ensure_registry_directories(paths: _RegistryPaths) -> None:
    for relative in (paths.receipts_relative, paths.quarantine_relative):
        _, descriptors = _open_directory(paths.root, relative, create=True)
        _close_directories(descriptors)


def _open_lock(paths: _RegistryPaths, *, create: bool) -> int:
    parent_fd, descriptors = _open_directory(paths.root, paths.execution_relative, create=False)
    try:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        created = False
        if create:
            try:
                descriptor = os.open(paths.lock_relative.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                descriptor = os.open(paths.lock_relative.name, flags, dir_fd=parent_fd)
        else:
            descriptor = os.open(paths.lock_relative.name, flags, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        linked = os.stat(paths.lock_relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != 0
            or _identity(metadata) != _identity(linked)
        ):
            os.close(descriptor)
            raise ValueError("execution registry lock must be an empty unaliased regular file")
        if created:
            os.fsync(descriptor)
            os.fsync(parent_fd)
        return descriptor
    except OSError as exc:
        raise ValueError("execution registry lock is unsafe") from exc
    finally:
        _close_directories(descriptors)


def _verify_lock_binding(paths: _RegistryPaths, descriptor: int) -> None:
    parent_fd, descriptors = _open_directory(
        paths.root, paths.execution_relative, create=False
    )
    try:
        opened = os.fstat(descriptor)
        linked = os.stat(
            paths.lock_relative.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != 0
            or _identity(opened) != _identity(linked)
        ):
            raise ValueError("execution registry lock identity was replaced")
    except OSError as exc:
        raise ValueError("execution registry lock identity was replaced") from exc
    finally:
        _close_directories(descriptors)


@contextmanager
def _registry_lock(paths: _RegistryPaths, *, create: bool, exclusive: bool) -> Iterator[int]:
    descriptor = _open_lock(paths, create=create)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        _verify_lock_binding(paths, descriptor)
        yield descriptor
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _artifact_binding(root: Path, raw: object, label: str, *, plan: bool = False) -> Mapping[str, object]:
    binding = _validate_binding_value(raw, label, plan=plan)
    path = str(binding["path"])
    digest = str(binding["sha256"])
    actual = _binding(root, root / path, label)
    if actual != {"path": path, "sha256": digest}:
        raise ValueError(f"{label} hash binding differs")
    _require_tracked_clean(root, path, label)
    if plan:
        blob = binding["git_blob_id"]
        if not isinstance(blob, str) or not _GIT_HEAD.fullmatch(blob) or _git(root, "rev-parse", f"HEAD:{path}") != blob:
            raise ValueError(f"{label} Git blob binding differs")
    return binding


def _validate_common_bindings(root: Path, payload: Mapping[str, object], run_spec: RunSpec) -> None:
    run_binding = _artifact_binding(root, payload["run_spec"], "run spec")
    _, run_bytes = _read_path(root, root / str(run_binding["path"]), "run spec")
    if canonical_json_bytes(run_spec.raw) + b"\n" != run_bytes:
        raise ValueError("caller RunSpec differs from receipt binding")
    _artifact_binding(root, payload["approved_design"], "approved design")
    _artifact_binding(root, payload["implementation_plan"], "implementation plan", plan=True)
    contract_binding = _artifact_binding(root, payload["acquisition_contract"], "acquisition contract")
    contract = _canonical_object(
        _read_path(root, root / str(contract_binding["path"]), "acquisition contract")[1],
        "acquisition contract",
    )
    bindings = contract.get("bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("acquisition contract bindings differ")
    expected_plan = dict(payload["implementation_plan"])
    expected_plan["working_tree_sha256"] = expected_plan.pop("sha256")
    if (
        bindings.get("approved_design") != payload["approved_design"]
        or bindings.get("run_spec") != payload["run_spec"]
        or bindings.get("implementation_plan") != expected_plan
    ):
        raise ValueError("execution receipt differs from acquisition contract bindings")
    lineage = _exact_object(payload["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage")
    protection = contract.get("legacy_protection")
    if not isinstance(protection, Mapping):
        raise ValueError("acquisition contract legacy protection differs")
    for key, prefix in (("baseline_inventory", "baseline_inventory"), ("protected_inventory", "protected_inventory")):
        value = _artifact_binding(root, lineage[key], f"Task0 {key}")
        if value != {"path": protection.get(f"{prefix}_path"), "sha256": protection.get(f"{prefix}_sha256")}:
            raise ValueError("Task0 lineage differs from acquisition contract")


def _validation_bindings(root: Path, paths: Sequence[Path]) -> list[dict[str, str]]:
    bindings = [_binding(root, path, "validation receipt") for path in paths]
    if len({item["path"] for item in bindings}) != len(bindings):
        raise ValueError("validation receipt paths must be unique")
    return bindings


def _binding_with_contents(
    root: Path, path: Path, label: str
) -> tuple[dict[str, str], bytes]:
    relative, contents = _read_path(root, path, label)
    _validate_artifact_path(relative, label)
    return {"path": relative, "sha256": _sha256_bytes(contents)}, contents


def _validate_stored_artifacts(root: Path, values: object, label: str) -> None:
    if not isinstance(values, list):
        raise ValueError(f"{label} must be a list")
    seen: set[str] = set()
    for value in values:
        binding = _validate_binding_value(value, label)
        path = binding.get("path")
        digest = binding.get("sha256")
        if not isinstance(path, str) or path in seen or not isinstance(digest, str):
            raise ValueError(f"{label} binding differs")
        seen.add(path)
        if _binding(root, root / path, label) != dict(binding):
            raise ValueError(f"{label} hash binding differs")


def _receipt_payload(contents: bytes, expected_sha: str, filename_phase: str, filename_sequence: int) -> Mapping[str, object]:
    if _sha256_bytes(contents) != expected_sha:
        raise ValueError("execution receipt filename hash differs")
    payload = _canonical_object(contents, "execution receipt")
    _validate_success_receipt(payload)
    if (
        payload.get("schema_version") != 1
        or payload.get("receipt_kind") != "PHASE_TRANSITION"
        or payload.get("phase") != filename_phase
        or type(payload.get("sequence")) is not int
        or payload.get("sequence") != filename_sequence
    ):
        raise ValueError("execution receipt identity differs")
    _validate_timestamp(payload.get("recorded_at_utc"), "execution receipt")
    return payload


def _validate_success_receipt(payload: object) -> Mapping[str, object]:
    receipt = _exact_object(payload, _SUCCESS_KEYS, "execution receipt")
    sequence = receipt.get("sequence")
    phase = receipt.get("phase")
    prior = receipt.get("prior_receipt_sha256")
    git_head = receipt.get("git_head")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "PHASE_TRANSITION"
        or receipt.get("run_id") != "historical_walkforward_500"
        or type(sequence) is not int
        or not 0 <= sequence < len(PHASES)
        or phase != PHASES[sequence]
        or (sequence == 0 and prior is not None)
        or (
            sequence > 0
            and (not isinstance(prior, str) or not _SHA256.fullmatch(prior))
        )
        or not isinstance(git_head, str)
        or not _GIT_HEAD.fullmatch(git_head)
    ):
        raise ValueError("execution receipt violates the receipt schema")
    _validate_timestamp(receipt.get("recorded_at_utc"), "execution receipt")
    _validate_binding_value(receipt.get("run_spec"), "run spec")
    _validate_binding_value(receipt.get("approved_design"), "approved design")
    _validate_binding_value(
        receipt.get("implementation_plan"), "implementation plan", plan=True
    )
    _validate_binding_value(
        receipt.get("acquisition_contract"), "acquisition contract"
    )
    lineage = _exact_object(
        receipt.get("task0_lineage"), _LINEAGE_KEYS, "Task0 lineage"
    )
    _validate_binding_value(lineage.get("baseline_inventory"), "baseline inventory")
    _validate_binding_value(
        lineage.get("protected_inventory"), "protected inventory"
    )
    _validate_binding_value(receipt.get("inventory"), "phase inventory")
    validations = receipt.get("validation_receipts")
    if not isinstance(validations, list):
        raise ValueError("validation receipts violate the receipt schema")
    paths = [
        str(_validate_binding_value(value, "validation receipt")["path"])
        for value in validations
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("validation receipts violate the receipt schema")
    return receipt


def _validate_quarantine_receipt(payload: object) -> Mapping[str, object]:
    receipt = _exact_object(payload, _QUARANTINE_KEYS, "quarantine receipt")
    git_head = receipt.get("git_head")
    current_hash = receipt.get("current_receipt_sha256")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("receipt_kind") != "QUARANTINED_TRANSITION"
        or receipt.get("run_id") != "historical_walkforward_500"
        or receipt.get("current_phase") not in PHASES
        or receipt.get("attempted_phase") not in PHASES
        or not isinstance(current_hash, str)
        or not _SHA256.fullmatch(current_hash)
        or not isinstance(git_head, str)
        or not _GIT_HEAD.fullmatch(git_head)
    ):
        raise ValueError("quarantine receipt violates the receipt schema")
    _validate_timestamp(receipt.get("recorded_at_utc"), "quarantine receipt")
    _validate_binding_value(receipt.get("run_spec"), "run spec")
    validations = receipt.get("validation_receipts")
    if not isinstance(validations, list) or not validations:
        raise ValueError("quarantine receipt requires validation evidence")
    paths = [
        str(_validate_binding_value(value, "validation receipt")["path"])
        for value in validations
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("validation receipts violate the receipt schema")
    return receipt


def _validate_quarantine_locked(
    paths: _RegistryPaths,
    run_spec: RunSpec,
    payloads: Sequence[Mapping[str, object]],
    phase_receipt_sha: Mapping[str, str],
) -> None:
    quarantine_fd, descriptors = _open_directory(
        paths.root, paths.quarantine_relative, create=False
    )
    try:
        names = sorted(os.listdir(quarantine_fd))
    finally:
        _close_directories(descriptors)
    for name in names:
        match = _SUCCESS_NAME.fullmatch(name)
        if match is None:
            raise ValueError("execution quarantine contains an ambiguous or partial receipt")
        contents = _read_relative(
            paths.root, paths.quarantine_relative / name, "quarantine receipt"
        )
        if _sha256_bytes(contents) != match.group("sha"):
            raise ValueError("quarantine receipt filename hash differs")
        payload = _canonical_object(contents, "quarantine receipt")
        _validate_quarantine_receipt(payload)
        current_phase = payload.get("current_phase")
        attempted = payload.get("attempted_phase")
        if (
            payload.get("schema_version") != 1
            or payload.get("receipt_kind") != "QUARANTINED_TRANSITION"
            or payload.get("run_id") != run_spec.run_id
            or current_phase not in PHASES
            or attempted not in PHASES
            or PHASES.index(str(attempted)) != PHASES.index(str(current_phase)) + 1
            or match.group("phase") != attempted
            or int(match.group("sequence")) != PHASES.index(str(attempted))
            or payload.get("current_receipt_sha256")
            != phase_receipt_sha.get(str(current_phase))
        ):
            raise ValueError("quarantine receipt transition binding differs")
        git_head = payload.get("git_head")
        if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
            raise ValueError("quarantine receipt Git head differs")
        _validate_timestamp(payload.get("recorded_at_utc"), "quarantine receipt")
        if payload.get("run_spec") != payloads[0]["run_spec"]:
            raise ValueError("quarantine receipt RunSpec binding differs")
        validation_values = payload.get("validation_receipts")
        if not isinstance(validation_values, list) or not validation_values:
            raise ValueError("quarantine receipt requires validation evidence")
        _validate_stored_artifacts(
            paths.root, validation_values, "validation receipts"
        )


def _load_locked(paths: _RegistryPaths, run_spec: RunSpec) -> tuple[ExecutionRegistryState, list[Mapping[str, object]]]:
    _require_bound_execution_code_clean(paths.root)
    receipt_fd, descriptors = _open_directory(paths.root, paths.receipts_relative, create=False)
    try:
        names = sorted(os.listdir(receipt_fd))
    finally:
        _close_directories(descriptors)
    if not names:
        raise FileNotFoundError("execution registry has no phase receipts")
    payloads: list[Mapping[str, object]] = []
    phase_receipt_sha: dict[str, str] = {}
    prior_sha: str | None = None
    stable_bindings: dict[str, object] | None = None
    for expected_sequence, name in enumerate(names):
        match = _SUCCESS_NAME.fullmatch(name)
        if match is None:
            raise ValueError("execution registry contains an ambiguous or partial receipt")
        sequence = int(match.group("sequence"))
        phase = match.group("phase")
        receipt_sha = match.group("sha")
        if sequence != expected_sequence or sequence >= len(PHASES) or phase != PHASES[sequence]:
            raise ValueError("execution registry chain has a gap, fork, replay, or rollback anchor")
        contents = _read_relative(paths.root, paths.receipts_relative / name, "execution receipt")
        payload = _receipt_payload(contents, receipt_sha, phase, sequence)
        if payload.get("run_id") != run_spec.run_id or payload.get("prior_receipt_sha256") != prior_sha:
            raise ValueError("execution registry prior hash or run identity differs")
        git_head = payload.get("git_head")
        if not isinstance(git_head, str) or not _GIT_HEAD.fullmatch(git_head):
            raise ValueError("execution receipt Git head differs")
        _validate_common_bindings(paths.root, payload, run_spec)
        inventory = _exact_object(payload.get("inventory"), _BINDING_KEYS, "phase inventory")
        if _binding(paths.root, paths.root / str(inventory["path"]), "phase inventory") != dict(inventory):
            raise ValueError("phase inventory hash binding differs")
        _validate_stored_artifacts(paths.root, payload.get("validation_receipts"), "validation receipts")
        current_bindings = {key: payload[key] for key in ("run_spec", "approved_design", "implementation_plan", "acquisition_contract", "task0_lineage")}
        if stable_bindings is None:
            stable_bindings = current_bindings
        elif current_bindings != stable_bindings:
            raise ValueError("execution registry binding fork differs")
        payloads.append(payload)
        phase_receipt_sha[phase] = receipt_sha
        prior_sha = receipt_sha
    last = payloads[-1]
    _validate_quarantine_locked(paths, run_spec, payloads, phase_receipt_sha)
    inventory = _exact_object(last["inventory"], _BINDING_KEYS, "phase inventory")
    return (
        ExecutionRegistryState(
            phase=str(last["phase"]),
            sequence=int(last["sequence"]),
            receipt_sha256=str(prior_sha),
            inventory_sha256=str(inventory["sha256"]),
        ),
        payloads,
    )


def _initial_evidence(root: Path, run_spec: RunSpec, design_path: Path, plan_path: Path, acquisition_contract_path: Path) -> dict[str, object]:
    _require_bound_execution_code_clean(root)
    contract_binding = _binding(root, acquisition_contract_path, "acquisition contract")
    _require_tracked_clean(root, contract_binding["path"], "acquisition contract")
    contract = _canonical_object(_read_path(root, acquisition_contract_path, "acquisition contract")[1], "acquisition contract")
    bindings = contract.get("bindings")
    protection = contract.get("legacy_protection")
    if not isinstance(bindings, Mapping) or not isinstance(protection, Mapping):
        raise ValueError("acquisition contract evidence is incomplete")
    design = _binding(root, design_path, "approved design")
    plan = _binding(root, plan_path, "implementation plan")
    run_binding_raw = bindings.get("run_spec")
    if not isinstance(run_binding_raw, Mapping) or not isinstance(run_binding_raw.get("path"), str):
        raise ValueError("acquisition contract RunSpec binding differs")
    run_binding = _binding(root, root / str(run_binding_raw["path"]), "run spec")
    if canonical_json_bytes(run_spec.raw) + b"\n" != _read_path(root, root / run_binding["path"], "run spec")[1]:
        raise ValueError("caller RunSpec differs from acquisition contract")
    plan_contract = bindings.get("implementation_plan")
    if not isinstance(plan_contract, Mapping):
        raise ValueError("implementation plan binding differs")
    plan_with_blob = {**plan, "git_blob_id": plan_contract.get("git_blob_id")}
    lineage = {
        "baseline_inventory": _binding(root, root / str(protection.get("baseline_inventory_path")), "Task0 baseline inventory"),
        "protected_inventory": _binding(root, root / str(protection.get("protected_inventory_path")), "Task0 protected inventory"),
    }
    evidence = {
        "run_spec": run_binding,
        "approved_design": design,
        "implementation_plan": plan_with_blob,
        "acquisition_contract": contract_binding,
        "task0_lineage": lineage,
    }
    probe = {**evidence}
    _validate_common_bindings(root, probe, run_spec)
    return evidence


def _success_contents(payload: Mapping[str, object]) -> tuple[bytes, str]:
    _validate_success_receipt(payload)
    contents = canonical_json_bytes(payload) + b"\n"
    return contents, _sha256_bytes(contents)


def _publish_quarantine_locked(
    paths: _RegistryPaths,
    run_spec: RunSpec,
    state: ExecutionRegistryState,
    base: Mapping[str, object],
    attempted_phase: str,
    validations: list[dict[str, str]],
    snapshot: _GitSnapshot,
    lock_descriptor: int,
) -> Path:
    if not validations:
        raise ValueError("quarantine transition requires validation evidence")
    payload = {
        "schema_version": 1,
        "receipt_kind": "QUARANTINED_TRANSITION",
        "run_id": run_spec.run_id,
        "attempted_phase": attempted_phase,
        "current_phase": state.phase,
        "current_receipt_sha256": state.receipt_sha256,
        "recorded_at_utc": _utc_now(),
        "git_head": snapshot.head,
        "run_spec": base["run_spec"],
        "validation_receipts": validations,
    }
    _validate_quarantine_receipt(payload)
    contents = canonical_json_bytes(payload) + b"\n"
    digest = _sha256_bytes(contents)
    relative = (
        paths.quarantine_relative
        / f"{state.sequence + 1:06d}-{attempted_phase}-{digest}.json"
    )
    _before_registry_publish()
    _verify_git_snapshot(paths.root, snapshot)
    _verify_lock_binding(paths, lock_descriptor)
    _write_exclusive(paths.root, relative, contents, "quarantine receipt")
    return paths.root / relative


def initialize_execution_registry(
    root: Path,
    run_spec: RunSpec,
    *,
    design_path: Path,
    plan_path: Path,
    acquisition_contract_path: Path,
) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    evidence = _initial_evidence(paths.root, run_spec, design_path, plan_path, acquisition_contract_path)
    _ensure_registry_directories(paths)
    with _registry_lock(paths, create=True, exclusive=True) as lock_descriptor:
        receipt_fd, descriptors = _open_directory(paths.root, paths.receipts_relative, create=False)
        try:
            existing = os.listdir(receipt_fd)
        finally:
            _close_directories(descriptors)
        if existing:
            state, payloads = _load_locked(paths, run_spec)
            expected = {key: evidence[key] for key in evidence}
            actual = {key: payloads[0][key] for key in evidence}
            if state.phase not in PHASES or actual != expected:
                raise ValueError("execution registry is already initialized and differs")
            return state
        protected = _exact_object(evidence["task0_lineage"], _LINEAGE_KEYS, "Task0 lineage")["protected_inventory"]
        snapshot = _capture_git_snapshot(paths.root, evidence)
        payload = {
            "schema_version": 1,
            "receipt_kind": "PHASE_TRANSITION",
            "run_id": run_spec.run_id,
            "sequence": 0,
            "phase": "DESIGN_ACCEPTED",
            "prior_receipt_sha256": None,
            "recorded_at_utc": _utc_now(),
            "git_head": snapshot.head,
            **evidence,
            "inventory": protected,
            "validation_receipts": [],
        }
        contents, digest = _success_contents(payload)
        relative = paths.receipts_relative / f"000000-DESIGN_ACCEPTED-{digest}.json"
        _before_registry_publish()
        _verify_git_snapshot(paths.root, snapshot)
        _verify_lock_binding(paths, lock_descriptor)
        _write_exclusive(paths.root, relative, contents, "execution receipt")
        return ExecutionRegistryState("DESIGN_ACCEPTED", 0, digest, str(protected["sha256"]))


def load_execution_registry(root: Path, run_spec: RunSpec) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=False):
        state, _ = _load_locked(paths, run_spec)
        return state


def advance_execution_phase(
    root: Path,
    run_spec: RunSpec,
    *,
    expected_phase: str,
    next_phase: str,
    inventory_path: Path,
    validation_receipts: Sequence[Path],
) -> ExecutionRegistryState:
    paths = _paths(root, run_spec)
    if expected_phase not in PHASES or next_phase not in PHASES or PHASES.index(next_phase) != PHASES.index(expected_phase) + 1:
        raise ValueError("execution transition must use the immediate next phase")
    if next_phase not in {"ACQUIRING", "SOURCES_READY"}:
        raise ValueError("execution transition fails closed until a phase validator exists")
    with _registry_lock(paths, create=False, exclusive=True) as lock_descriptor:
        state, payloads = _load_locked(paths, run_spec)
        if state.phase != expected_phase:
            raise ValueError(
                "execution transition replay or expected phase differs from "
                "authenticated head"
            )
        base = payloads[0]
        snapshot = _capture_git_snapshot(paths.root, base)
        inventory, inventory_contents = _binding_with_contents(
            paths.root, inventory_path, "phase inventory"
        )
        validation_values = [
            _binding_with_contents(paths.root, path, "validation receipt")
            for path in validation_receipts
        ]
        validations = [binding for binding, _contents in validation_values]
        try:
            _phase_evidence_payload(
                inventory_contents,
                phase=next_phase,
                run_id=run_spec.run_id,
                inventory_sha256=None,
            )
            if not validation_values:
                raise ValueError("validation receipt evidence is required")
            for _binding_value, contents in validation_values:
                _phase_evidence_payload(
                    contents,
                    phase=next_phase,
                    run_id=run_spec.run_id,
                    inventory_sha256=inventory["sha256"],
                )
        except ValueError as exc:
            if validations:
                _publish_quarantine_locked(
                    paths,
                    run_spec,
                    state,
                    base,
                    next_phase,
                    validations,
                    snapshot,
                    lock_descriptor,
                )
            raise ValueError("phase inventory or validation evidence failed") from exc
        payload = {
            "schema_version": 1,
            "receipt_kind": "PHASE_TRANSITION",
            "run_id": run_spec.run_id,
            "sequence": state.sequence + 1,
            "phase": next_phase,
            "prior_receipt_sha256": state.receipt_sha256,
            "recorded_at_utc": _utc_now(),
            "git_head": snapshot.head,
            **{key: base[key] for key in ("run_spec", "approved_design", "implementation_plan", "acquisition_contract", "task0_lineage")},
            "inventory": inventory,
            "validation_receipts": validations,
        }
        contents, digest = _success_contents(payload)
        relative = paths.receipts_relative / f"{state.sequence + 1:06d}-{next_phase}-{digest}.json"
        _before_registry_publish()
        _verify_git_snapshot(paths.root, snapshot)
        _verify_lock_binding(paths, lock_descriptor)
        _write_exclusive(paths.root, relative, contents, "execution receipt")
        return ExecutionRegistryState(next_phase, state.sequence + 1, digest, inventory["sha256"])


def quarantine_execution_transition(
    root: Path,
    run_spec: RunSpec,
    *,
    attempted_phase: str,
    validation_receipts: Sequence[Path],
) -> Path:
    paths = _paths(root, run_spec)
    with _registry_lock(paths, create=False, exclusive=True) as lock_descriptor:
        validations = _validation_bindings(paths.root, validation_receipts)
        if not validations:
            raise ValueError("quarantine transition requires validation evidence")
        state, payloads = _load_locked(paths, run_spec)
        if state.sequence + 1 >= len(PHASES) or attempted_phase != PHASES[state.sequence + 1]:
            raise ValueError("quarantine attempted phase must be the immediate next phase")
        snapshot = _capture_git_snapshot(paths.root, payloads[0])
        return _publish_quarantine_locked(
            paths,
            run_spec,
            state,
            payloads[0],
            attempted_phase,
            validations,
            snapshot,
            lock_descriptor,
        )


# A local-only append log detects an interior deletion while a later receipt survives.
# Coordinated deletion of the entire unanchored local tail is outside this threat model
# until a later task publishes an external anchor.
