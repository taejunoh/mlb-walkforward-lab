"""Run the Task 13 unittest suite against isolated design and source trees."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mlb_backtest.diagnostics import canonical_json_bytes, sha256_file  # noqa: E402
from mlb_backtest.preflight import (  # noqa: E402
    build_preflight,
    render_preflight_markdown,
)
from mlb_backtest.protocols import (  # noqa: E402
    CURRENT_APPROVED_DESIGN_COMMIT,
    load_run_spec,
)
from mlb_backtest.verify_v52_design import (  # noqa: E402
    PROTECTED_TREES,
    V51_PROTECTED_BASELINE_COMMIT,
    V51_REQUIRED_FILES,
)
from mlb_backtest.v52_recovery_parent_evidence import (  # noqa: E402
    build_recovery_parent_catalog,
    capture_tree_fingerprint,
    PARENT_RELATIVES,
    TreeFingerprint,
)
from mlb_backtest.v52_duplicate_gamepk_disposition import (  # noqa: E402
    OLD_RAW_BYTE_COUNT,
    OLD_RAW_SHA256,
    OLD_RAW_RELATIVE,
    OLD_QUARANTINE_SHA256,
    OLD_REGISTRATION_RELATIVE,
    OLD_REGISTRATION_SHA256,
    OLD_QUARANTINE_RELATIVE,
)


CHILD_MODULES = (
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
)

OLD_EVIDENCE_RELATIVES = (
    OLD_REGISTRATION_RELATIVE,
    OLD_QUARANTINE_RELATIVE,
    OLD_RAW_RELATIVE,
)
OLD_EVIDENCE_SHA256 = {
    OLD_REGISTRATION_RELATIVE.as_posix(): OLD_REGISTRATION_SHA256,
    OLD_QUARANTINE_RELATIVE.as_posix(): OLD_QUARANTINE_SHA256,
    OLD_RAW_RELATIVE.as_posix(): OLD_RAW_SHA256,
}


def _recovery_parent_directory_relatives() -> tuple[Path, ...]:
    directories: set[Path] = set()
    for relatives in PARENT_RELATIVES.values():
        directories.add(relatives["registration"].parent)
        directories.add(relatives["quarantine"].parent)
    return tuple(sorted(directories))


def _recovery_parent_file_relatives() -> tuple[Path, ...]:
    files: set[Path] = set()
    for relatives in PARENT_RELATIVES.values():
        registration = relatives["registration"]
        query_directory = registration.parent
        files.update(
            (
                query_directory / ".query.lock",
                query_directory / "attempts/000001.json",
                query_directory / "attempts/000001.reservation.json",
                registration,
                relatives["quarantine"],
                relatives["raw"],
            )
        )
    return tuple(sorted(files))


RECOVERY_PARENT_DIRECTORY_RELATIVES = _recovery_parent_directory_relatives()
RECOVERY_PARENT_FILE_RELATIVES = _recovery_parent_file_relatives()
RECOVERY_AUTHORITY_TREE_RELATIVES = (
    Path("data/v5_2_qd/historical"),
    Path("audit/v5_2_qd/historical"),
    Path("data/v5_2_qd/historical_superseding_565534"),
    Path("audit/v5_2_qd/historical_superseding_565534"),
)
RECOVERY_AUTHORITY_TREE_FINGERPRINTS = {
    "data/v5_2_qd/historical": (22, "b1256b64be7a53250be36d80c9bc645c7f606454d6010607d0258b3fefccc6b1", "7653312388f3696f8e82755b36360aef7ab5c36f1e0c6af7ad311f70aaee8c5a"),
    "audit/v5_2_qd/historical": (6, "7578245a41176d52571b87b876afba65d8b320c8bb823996574bc2106d9959bd", "4e9ae2dfb6e7a11463f6492a5d888d85aafa8fabac35d010ae51f075d1495eaa"),
    "data/v5_2_qd/historical_superseding_565534": (40, "fa047ccccacb65c1ddf72d71ed04b26801b20d014f5bc978cd1f9d858be5abee", "27f22c97b3ad742bf0965d94119ff425d12990e431ea03701cee5848c58c7055"),
    "audit/v5_2_qd/historical_superseding_565534": (8, "d9ce5fd41d06cb3a43a82530b2f7de151aadd15ced54cb0b90d6761e767deda6", "11b6cf0602bee1d4f76229ea508272a4f2a42b2a060515dc49366ee81afc0f05"),
}
ORIGINAL_PREFLIGHT_RELATIVES = (
    Path("reports/v5_2_qd/walkforward_500_preflight.json"),
    Path("reports/v5_2_qd/walkforward_500_preflight.md"),
)
ORIGINAL_PREFLIGHT_SHA256 = {
    "reports/v5_2_qd/walkforward_500_preflight.json": (
        "8935c781a7984a244c510fb0018fa97ca8e0f225f708f9700723c44d0db0a082"
    ),
    "reports/v5_2_qd/walkforward_500_preflight.md": (
        "672ad17ca67a018b9cf438c28d8544ff5c1cc24c1d0e197eef7d4f51020404bc"
    ),
}
APPROVED_DESIGN_RELATIVE = Path(
    "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
)
_CHROMIUM_ENVIRONMENT_KEYS = (
    "CHROMIUM_EXECUTABLE_PATH",
    "PLAYWRIGHT_EXECUTABLE_PATH",
    "PLAYWRIGHT_BROWSERS_PATH",
)
_CHROMIUM_APP_ERROR = (
    "chromium .app bundle launchers are unsupported; provide a "
    "self-contained runtime directory"
)


@dataclass(frozen=True)
class ChromiumBundleEntry:
    relative: tuple[str, ...]
    kind: str
    mode: int
    size: int
    sha256: str
    fingerprint: tuple[int, ...]


@dataclass(frozen=True)
class ChromiumRuntimeSnapshot:
    root: Path
    executable_relative: tuple[str, ...]
    executable_sha256: str
    bundle_sha256: str
    entries: tuple[ChromiumBundleEntry, ...]

    @property
    def path(self) -> Path:
        return self.root.joinpath(*self.executable_relative)


def _regular_file(path: Path, label: str) -> Path:
    metadata = path.lstat()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"{label} must be a single-link file: {path}")
    return path


def _absolute_path_without_symlink(path: Path, label: str) -> Path:
    path = Path(path)
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts):
        raise ValueError(f"{label} must be an absolute normalized path")
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current /= component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise ValueError(f"{label} contains a symlink: {current}")
    except OSError as exc:
        raise ValueError(f"{label} is unavailable: {path}") from exc
    return current


def _reject_chromium_app_bundle(path: Path) -> None:
    if any(component.lower().endswith(".app") for component in Path(path).parts):
        raise ValueError(_CHROMIUM_APP_ERROR)


def _chromium_metadata(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"chromium executable is unavailable: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"chromium executable must be a single-link regular file: {path}")
    if not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise ValueError(f"chromium executable is not executable: {path}")
    return metadata


def _chromium_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _sha256_chromium_executable(path: Path) -> str:
    before = _chromium_metadata(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"chromium executable cannot be opened safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if _chromium_fingerprint(opened) != _chromium_fingerprint(before):
            raise ValueError("chromium executable changed before hashing")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    after = _chromium_metadata(path)
    if _chromium_fingerprint(after) != _chromium_fingerprint(before):
        raise ValueError("chromium executable changed during hashing")
    return digest.hexdigest()


def _validate_chromium_sha256(chromium_sha256: str | None) -> str:
    if (
        not isinstance(chromium_sha256, str)
        or len(chromium_sha256) != 64
        or any(character not in "0123456789abcdef" for character in chromium_sha256)
    ):
        raise ValueError("chromium sha256 must be a lowercase 64-character hex digest")
    return chromium_sha256


def validate_chromium_runtime(
    chromium_executable: Path | None, chromium_sha256: str | None
) -> Path:
    """Authenticate an external source before snapshot provisioning."""
    if (chromium_executable is None) != (chromium_sha256 is None):
        raise ValueError("chromium executable and sha256 are required together")
    if chromium_executable is None or chromium_sha256 is None:
        raise ValueError("chromium executable and sha256 are required")
    expected_sha256 = _validate_chromium_sha256(chromium_sha256)
    executable = _absolute_path_without_symlink(
        Path(chromium_executable), "chromium executable"
    )
    _reject_chromium_app_bundle(executable)
    before = _chromium_fingerprint(_chromium_metadata(executable))
    actual_sha256 = _sha256_chromium_executable(executable)
    after_path = _absolute_path_without_symlink(executable, "chromium executable")
    if _chromium_fingerprint(_chromium_metadata(after_path)) != before:
        raise ValueError("chromium executable changed during hashing")
    if actual_sha256 != expected_sha256:
        raise ValueError("chromium sha256 differs from the required digest")
    return executable


def _write_all(descriptor: int, contents: bytes) -> None:
    offset = 0
    while offset < len(contents):
        written = os.write(descriptor, contents[offset:])
        if written <= 0:
            raise ValueError("chromium runtime copy made no progress")
        offset += written


def _fdatasync_file(descriptor: int) -> None:
    """Flush file content, including on platforms without ``fdatasync``."""
    fdatasync = getattr(os, "fdatasync", None)
    if fdatasync is None:
        os.fsync(descriptor)
    else:
        fdatasync(descriptor)


def _metadata_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _chromium_bundle_entry(
    relative: tuple[str, ...], metadata: os.stat_result, digest: str = ""
) -> ChromiumBundleEntry:
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
        kind = "file"
    else:
        raise ValueError(f"chromium bundle has an unsafe entry: {'/'.join(relative)}")
    return ChromiumBundleEntry(
        relative=relative,
        kind=kind,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        sha256=digest,
        fingerprint=_metadata_fingerprint(metadata),
    )


def _open_directory_no_follow(path: Path, label: str) -> int:
    _absolute_path_without_symlink(path, label)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"{label} is not a directory")
    return descriptor


def _open_child_directory(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise ValueError(f"chromium bundle directory cannot be opened safely: {name}") from exc


def _read_bundle_file(
    parent_descriptor: int, name: str, expected: ChromiumBundleEntry | None = None
) -> tuple[os.stat_result, str, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        raise ValueError(f"chromium bundle file cannot be opened safely: {name}") from exc
    try:
        before = os.fstat(descriptor)
        entry = _chromium_bundle_entry((name,), before)
        if entry.kind != "file":
            raise ValueError(f"chromium bundle has an unsafe file: {name}")
        if expected is not None and entry.fingerprint != expected.fingerprint:
            raise ValueError(f"chromium bundle source changed: {'/'.join(expected.relative)}")
        digest = hashlib.sha256()
        contents = bytearray()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            contents.extend(chunk)
        if _metadata_fingerprint(os.fstat(descriptor)) != entry.fingerprint:
            raise ValueError(f"chromium bundle source changed while reading: {name}")
        return before, digest.hexdigest(), bytes(contents)
    finally:
        os.close(descriptor)


def _scan_chromium_bundle(directory_descriptor: int) -> tuple[ChromiumBundleEntry, ...]:
    entries: list[ChromiumBundleEntry] = []

    def visit(parent_descriptor: int, prefix: tuple[str, ...]) -> None:
        for name in sorted(os.listdir(parent_descriptor)):
            if not name or name in (".", "..") or "/" in name:
                raise ValueError("chromium bundle has an unsafe entry name")
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            relative = prefix + (name,)
            entry = _chromium_bundle_entry(relative, metadata)
            if entry.kind == "directory":
                entries.append(entry)
                child_descriptor = _open_child_directory(parent_descriptor, name)
                try:
                    if _metadata_fingerprint(os.fstat(child_descriptor)) != entry.fingerprint:
                        raise ValueError(f"chromium bundle source changed: {'/'.join(relative)}")
                    visit(child_descriptor, relative)
                finally:
                    os.close(child_descriptor)
            else:
                _, digest, _ = _read_bundle_file(parent_descriptor, name)
                entries.append(
                    ChromiumBundleEntry(
                        relative=relative,
                        kind=entry.kind,
                        mode=entry.mode,
                        size=entry.size,
                        sha256=digest,
                        fingerprint=entry.fingerprint,
                    )
                )

    visit(directory_descriptor, ())
    return tuple(entries)


def _chromium_bundle_manifest_bytes(entries: Sequence[ChromiumBundleEntry]) -> bytes:
    return canonical_json_bytes(
        [
            {
                "path": "/".join(entry.relative),
                "type": entry.kind,
                "mode": format(entry.mode, "04o"),
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in entries
        ]
    )


def chromium_bundle_sha256(bundle_root: Path) -> str:
    """Return the canonical hash of a closed, no-symlink Chromium bundle."""
    descriptor = _open_directory_no_follow(Path(bundle_root), "chromium bundle")
    try:
        return hashlib.sha256(_chromium_bundle_manifest_bytes(_scan_chromium_bundle(descriptor))).hexdigest()
    finally:
        os.close(descriptor)


def _create_chromium_runtime_directory(runtime_root: Path) -> Path:
    runtime_root = Path(runtime_root)
    if not runtime_root.is_absolute() or any(part in (".", "..") for part in runtime_root.parts):
        raise ValueError("chromium runtime directory must be an absolute normalized path")
    parent_descriptor = _open_directory_no_follow(
        runtime_root.parent, "chromium runtime directory parent"
    )
    try:
        try:
            os.mkdir(runtime_root.name, 0o700, dir_fd=parent_descriptor)
        except OSError as exc:
            raise ValueError("chromium runtime directory cannot be created exclusively") from exc
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return runtime_root


def _copy_bundle_file(
    source_parent_descriptor: int,
    entry: ChromiumBundleEntry,
    destination_parent: Path,
) -> None:
    _, digest, contents = _read_bundle_file(
        source_parent_descriptor, entry.relative[-1], entry
    )
    if digest != entry.sha256 or len(contents) != entry.size:
        raise ValueError(f"chromium bundle source changed: {'/'.join(entry.relative)}")
    destination = destination_parent / entry.relative[-1]
    temporary = destination.with_name(f".{destination.name}.partial")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(descriptor, contents)
            os.fchmod(descriptor, 0o700 if entry.mode & 0o111 else 0o600)
            _fdatasync_file(descriptor)
        finally:
            os.close(descriptor)
        os.link(temporary, destination)
        os.unlink(temporary)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_chromium_bundle(
    source_root_descriptor: int,
    entries: Sequence[ChromiumBundleEntry],
    destination_root: Path,
) -> None:
    parents: dict[tuple[str, ...], tuple[int, Path]] = {(): (source_root_descriptor, destination_root)}
    try:
        for entry in entries:
            prefix = entry.relative[:-1]
            source_parent_descriptor, destination_parent = parents[prefix]
            if entry.kind == "directory":
                destination = destination_parent / entry.relative[-1]
                os.mkdir(destination, 0o700)
                descriptor = _open_child_directory(source_parent_descriptor, entry.relative[-1])
                if _metadata_fingerprint(os.fstat(descriptor)) != entry.fingerprint:
                    os.close(descriptor)
                    raise ValueError(f"chromium bundle source changed: {'/'.join(entry.relative)}")
                parents[entry.relative] = (descriptor, destination)
            else:
                _copy_bundle_file(source_parent_descriptor, entry, destination_parent)
        for _, destination in parents.values():
            descriptor = _open_directory_no_follow(destination, "chromium runtime directory")
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        for relative, (descriptor, _) in parents.items():
            if relative:
                os.close(descriptor)


def provision_chromium_runtime(
    chromium_executable: Path | None,
    chromium_sha256: str | None,
    chromium_bundle_sha256: str | None,
    runtime_root: Path,
) -> ChromiumRuntimeSnapshot:
    """Copy an authenticated Chromium bundle into a runner-owned snapshot."""
    source = validate_chromium_runtime(chromium_executable, chromium_sha256)
    expected_executable_sha256 = _validate_chromium_sha256(chromium_sha256)
    expected_bundle_sha256 = _validate_chromium_sha256(chromium_bundle_sha256)
    source_root_descriptor = _open_directory_no_follow(source.parent, "chromium bundle")
    try:
        entries = _scan_chromium_bundle(source_root_descriptor)
        manifest_sha256 = hashlib.sha256(_chromium_bundle_manifest_bytes(entries)).hexdigest()
        if manifest_sha256 != expected_bundle_sha256:
            raise ValueError("chromium bundle sha256 differs from the required digest")
        executable_entry = next((entry for entry in entries if entry.relative == (source.name,)), None)
        if executable_entry is None or executable_entry.kind != "file":
            raise ValueError("chromium executable is absent from its bundle")
        if executable_entry.sha256 != expected_executable_sha256 or not executable_entry.mode & 0o111:
            raise ValueError("chromium executable differs inside its bundle")
        runtime_directory = _create_chromium_runtime_directory(runtime_root)
        _copy_chromium_bundle(source_root_descriptor, entries, runtime_directory)
        if _scan_chromium_bundle(source_root_descriptor) != entries:
            raise ValueError("chromium bundle source changed during snapshot provisioning")
    finally:
        os.close(source_root_descriptor)
    snapshot = ChromiumRuntimeSnapshot(
        root=runtime_directory,
        executable_relative=(source.name,),
        executable_sha256=expected_executable_sha256,
        bundle_sha256=expected_bundle_sha256,
        entries=entries,
    )
    validate_chromium_snapshot(snapshot)
    return snapshot


def validate_chromium_snapshot(snapshot: ChromiumRuntimeSnapshot) -> Path:
    if not isinstance(snapshot, ChromiumRuntimeSnapshot):
        raise ValueError("chromium snapshot capability is invalid")
    root_metadata = snapshot.root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise ValueError("chromium snapshot metadata changed")
    descriptor = _open_directory_no_follow(snapshot.root, "chromium snapshot")
    try:
        entries = _scan_chromium_bundle(descriptor)
    finally:
        os.close(descriptor)
    if len(entries) != len(snapshot.entries):
        raise ValueError("chromium snapshot membership changed")
    for observed, expected in zip(entries, snapshot.entries, strict=True):
        if observed.relative != expected.relative or observed.kind != expected.kind:
            raise ValueError("chromium snapshot membership changed")
        if observed.sha256 != expected.sha256 or observed.size != expected.size:
            raise ValueError("chromium snapshot content changed")
        expected_mode = 0o700 if expected.kind == "directory" or expected.mode & 0o111 else 0o600
        if observed.mode != expected_mode:
            raise ValueError("chromium snapshot metadata changed")
    manifest_sha256 = hashlib.sha256(_chromium_bundle_manifest_bytes(snapshot.entries)).hexdigest()
    if manifest_sha256 != snapshot.bundle_sha256:
        raise ValueError("chromium snapshot manifest is invalid")
    executable = snapshot.path
    if _sha256_chromium_executable(executable) != snapshot.executable_sha256:
        raise ValueError("chromium snapshot executable sha256 changed")
    return executable


def sha256_paths(root: Path, relatives: Iterable[str | Path]) -> dict[str, str]:
    root = Path(root).resolve(strict=True)
    result: dict[str, str] = {}
    for raw_relative in relatives:
        relative = Path(raw_relative)
        result[relative.as_posix()] = sha256_file(
            _regular_file(root / relative, "input")
        )
    return result


def validate_recovery_parent_inputs(repo: Path) -> Mapping[str, str]:
    """Authenticate the fixed 2020/2021 recovery parent evidence separately.

    This is deliberately independent of the legacy Task 13 input tuples: the
    recovery chain requires each registration, attempt, quarantine receipt,
    and raw body to be re-opened through its closed parent catalog.
    """
    catalog = build_recovery_parent_catalog(Path(repo).resolve(strict=True))
    hashes: dict[str, str] = {}
    for parent_name in (
        "superseding_2020_630882",
        "superseding_2021_633224",
    ):
        parent = catalog.parents[parent_name]
        if parent.attempt is None:
            raise ValueError("recovery parent attempt binding is missing")
        for artifact, evidence in (
            ("registration", parent.registration),
            ("attempt", parent.attempt),
            ("quarantine", parent.quarantine),
        ):
            catalog.read_json(parent_name, artifact)
            hashes[evidence.relative.as_posix()] = evidence.sha256
        catalog.read_raw(parent_name)
        hashes[parent.raw.relative.as_posix()] = parent.raw.sha256
    catalog.reauthenticate()
    return hashes


def capture_recovery_parent_trees(root: Path) -> dict[str, TreeFingerprint]:
    """Fingerprint the complete, closed historical authority input trees."""
    root = Path(root).resolve(strict=True)
    return {
        relative.as_posix(): capture_tree_fingerprint(root, relative)
        for relative in RECOVERY_AUTHORITY_TREE_RELATIVES
    }


def _assert_recovery_authority_tree_fingerprints(
    fingerprints: Mapping[str, TreeFingerprint],
) -> None:
    observed = {
        path: (fingerprint.file_count, fingerprint.path_sha256, fingerprint.sha256)
        for path, fingerprint in fingerprints.items()
    }
    if observed != RECOVERY_AUTHORITY_TREE_FINGERPRINTS:
        raise ValueError("recovery authority tree fingerprint differs")


def copy_recovery_parent_trees(
    source_root: Path, destination_root: Path
) -> dict[str, TreeFingerprint]:
    """Copy only the authenticated recovery-parent directory closure.

    Tree fingerprints authenticate the auxiliary lock/reservation records that
    are required by the parent catalog's exact directory memberships, in
    addition to the fixed semantic parent artifacts.
    """
    source_root = Path(source_root).resolve(strict=True)
    destination_root = Path(destination_root).resolve()
    before = capture_recovery_parent_trees(source_root)
    for relative in RECOVERY_AUTHORITY_TREE_RELATIVES:
        source = source_root / relative
        destination = destination_root / relative
        if destination.exists() or destination.is_symlink():
            raise ValueError("recovery parent destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
    if capture_recovery_parent_trees(source_root) != before:
        raise RuntimeError("recovery parent input changed while being copied")
    copied = capture_recovery_parent_trees(destination_root)
    if copied != before:
        raise RuntimeError("copied recovery parent input differs from source")
    return copied


def copy_files_with_hashes(
    source_root: Path, destination_root: Path, relatives: Iterable[str | Path]
) -> dict[str, str]:
    source_root = Path(source_root).resolve(strict=True)
    destination_root = Path(destination_root).resolve()
    copied: dict[str, str] = {}
    for raw_relative in relatives:
        relative = Path(raw_relative)
        source = _regular_file(source_root / relative, "source input")
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        _regular_file(destination, "copied input")
        copied[relative.as_posix()] = sha256_file(destination)
    return copied


def _run_git(args: Sequence[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    )


def validate_repository_root(repo: Path, *, runner_root: Path | None = None) -> Path:
    resolved_repo = Path(repo).resolve(strict=True)
    expected_root = Path(_REPO_ROOT if runner_root is None else runner_root).resolve(
        strict=True
    )
    if resolved_repo != expected_root:
        raise ValueError("repo must match the runner root checkout")
    status = _run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"], cwd=resolved_repo
    ).stdout
    if status:
        raise ValueError("repo must be clean before exact acceptance")
    return resolved_repo


def clone_repo_at_head(repo: Path, destination: Path) -> Path:
    repo = Path(repo).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    head = _run_git(["rev-parse", "--verify", "HEAD"], cwd=repo).stdout.strip()
    subprocess.run(
        [
            "git",
            "clone",
            "--no-local",
            "--no-checkout",
            "--no-tags",
            "--single-branch",
            "--depth=1",
            repo.as_uri(),
            str(destination),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    _run_git(["checkout", "--detach", head], cwd=destination)
    checked_out = _run_git(["rev-parse", "--verify", "HEAD"], cwd=destination).stdout.strip()
    if checked_out != head:
        raise RuntimeError("shallow clone did not checkout the source HEAD")
    return destination


def _provision_historical_commit(
    design_root: Path,
    *,
    commit: str,
    required_paths: Sequence[str | Path],
) -> str:
    """Fetch one pinned commit and verify required paths in its tree.

    The exact-suite checkout intentionally contains just the current tree.
    Historical protocol/inventory checks read pinned Git objects, so each
    required commit is fetched at depth one without changing the checkout's
    current HEAD.
    """
    design_root = Path(design_root).resolve(strict=True)
    before_head = _run_git(
        ["rev-parse", "--verify", "HEAD"], cwd=design_root
    ).stdout.strip()
    try:
        # ``fetch <abbreviated-sha>`` is rejected by the local file transport.
        # Resolve the pin against the local origin, then fetch the full object
        # ID with depth one.  This keeps the fetch narrow and avoids depending
        # on any remote ref containing the historical pin.
        origin = _run_git(
            ["config", "--get", "remote.origin.url"], cwd=design_root
        ).stdout.strip()
        parsed_origin = urlparse(origin)
        if parsed_origin.scheme not in ("", "file") or parsed_origin.netloc:
            raise RuntimeError("design origin must be a local repository")
        origin_value = (
            unquote(parsed_origin.path)
            if parsed_origin.scheme == "file"
            else origin
        )
        origin_repo = Path(origin_value).resolve(strict=True)
        pinned_commit = _run_git(
            [
                "rev-parse",
                "--verify",
                f"{commit}^{{commit}}",
            ],
            cwd=origin_repo,
        ).stdout.strip()
        _run_git(
            [
                "fetch",
                "--depth=1",
                "--no-tags",
                "origin",
                pinned_commit,
            ],
            cwd=design_root,
        )
        for required_path in required_paths:
            relative = Path(required_path).as_posix()
            completed = _run_git(
                [
                    "ls-tree",
                    "-r",
                    "-z",
                    "--full-tree",
                    pinned_commit,
                    "--",
                    relative,
                ],
                cwd=design_root,
            )
            if not completed.stdout:
                raise RuntimeError("historical pinned commit/path unavailable")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("historical pinned commit/path unavailable") from exc
    after_head = _run_git(
        ["rev-parse", "--verify", "HEAD"], cwd=design_root
    ).stdout.strip()
    if after_head != before_head:
        raise RuntimeError("historical provisioning changed design HEAD")
    return pinned_commit


def _provision_approved_design_commit(design_root: Path) -> str:
    return _provision_historical_commit(
        design_root,
        commit=CURRENT_APPROVED_DESIGN_COMMIT,
        required_paths=(APPROVED_DESIGN_RELATIVE,),
    )


def _provision_v51_baseline_commit(design_root: Path) -> str:
    return _provision_historical_commit(
        design_root,
        commit=V51_PROTECTED_BASELINE_COMMIT,
        required_paths=(*PROTECTED_TREES, "config/v5_1_bt.json"),
    )


def build_child_command(
    python: str | Path,
    *,
    modules: Sequence[str] = CHILD_MODULES,
    probe: Sequence[str] | None = None,
) -> list[str]:
    executable = str(python)
    if probe is not None:
        return [executable, *probe, *modules]
    return [executable, "-m", "unittest", *modules, "-v"]


def build_child_environment(
    design_root: Path,
    source_root: Path,
    *,
    chromium_runtime: ChromiumRuntimeSnapshot,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    validated_chromium = validate_chromium_snapshot(chromium_runtime)
    environment = dict(os.environ if base_environment is None else base_environment)
    for key in _CHROMIUM_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(Path(design_root).resolve())
    environment["MLB_V52_SOURCE_AUTHORITY_ROOT"] = str(Path(source_root).resolve())
    environment["MLB_V52_RECOVERY_PARENT_EVIDENCE_ROOT"] = str(
        Path(source_root).resolve()
    )
    environment["CHROMIUM_EXECUTABLE_PATH"] = str(validated_chromium)
    return environment


def _write_fixture_preflight(design_root: Path) -> None:
    historical = load_run_spec(
        design_root / "protocols/v5_2_qd/historical_walkforward_500.json"
    )
    report = build_preflight(design_root, historical)
    json_path = design_root / ORIGINAL_PREFLIGHT_RELATIVES[0]
    markdown_path = design_root / ORIGINAL_PREFLIGHT_RELATIVES[1]
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_bytes(canonical_json_bytes(report) + b"\n")
    markdown_path.write_text(render_preflight_markdown(report), encoding="utf-8")


def _walk_namespace_path(
    root: Path, relative: Path, *, required: bool
) -> Path | None:
    root = Path(root)
    if not root.exists():
        if root.is_symlink() or required:
            raise ValueError("historical namespace root is unavailable")
        return None
    current = root.resolve(strict=True)
    for component in relative.parts:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if required:
                raise ValueError("source evidence namespace is missing")
            return None
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("historical namespace contains symlink")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("historical namespace contains non-directory ancestor")
    return current


def _assert_no_design_historical_data(design_root: Path) -> None:
    for relative in (
        Path("data/v5_2_qd/historical"),
        Path("data/v5_2_qd/historical_superseding_565534"),
        Path("audit/v5_2_qd/historical"),
        Path("audit/v5_2_qd/historical_superseding_565534"),
    ):
        try:
            path = _walk_namespace_path(design_root, relative, required=False)
        except ValueError as exc:
            raise ValueError("design clone historical namespace is unsafe") from exc
        if path is not None:
            raise ValueError("design clone contains v5.2 historical data/audit")


def _assert_source_evidence_namespace(
    source_root: Path,
    *,
    expected_tree_fingerprints: Mapping[str, TreeFingerprint] | None = None,
) -> None:
    source_root = Path(source_root).resolve(strict=True)
    try:
        fingerprints = capture_recovery_parent_trees(source_root)
    except ValueError as exc:
        raise ValueError("source evidence namespace is unsafe") from exc
    expected_fingerprints = (
        RECOVERY_AUTHORITY_TREE_FINGERPRINTS
        if expected_tree_fingerprints is None
        else {
            path: (fingerprint.file_count, fingerprint.path_sha256, fingerprint.sha256)
            for path, fingerprint in expected_tree_fingerprints.items()
        }
    )
    observed_fingerprints = {
        path: (fingerprint.file_count, fingerprint.path_sha256, fingerprint.sha256)
        for path, fingerprint in fingerprints.items()
    }
    if observed_fingerprints != expected_fingerprints:
        raise ValueError("source evidence namespace fingerprint differs")
    expected = {
        Path(path)
        for fingerprint in fingerprints.values()
        for path in fingerprint.paths
    }
    observed: set[Path] = set()
    roots = (
        (Path("data/v5_2_qd/historical"), True),
        (Path("data/v5_2_qd/historical_superseding_565534"), True),
        (Path("audit/v5_2_qd/historical"), False),
        (Path("audit/v5_2_qd/historical_superseding_565534"), False),
    )
    for relative, required in roots:
        try:
            namespace = _walk_namespace_path(source_root, relative, required=required)
        except ValueError as exc:
            raise ValueError("source evidence namespace is unsafe") from exc
        if namespace is None:
            continue
        for path in namespace.rglob("*"):
            metadata = path.lstat()
            if metadata.st_mode & 0o170000 == 0o120000:
                raise ValueError("source evidence namespace contains symlink")
            if path.is_dir():
                continue
            if not path.is_file() or metadata.st_nlink != 1:
                raise ValueError("source evidence namespace contains non-regular file")
            observed.add(path.relative_to(source_root))
    if observed != expected:
        raise ValueError("source evidence namespace differs from recovery parent closure")


def _assert_fixture_namespaces(
    design_root: Path,
    source_root: Path,
    *,
    expected_tree_fingerprints: Mapping[str, TreeFingerprint] | None = None,
) -> None:
    _assert_no_design_historical_data(design_root)
    _assert_source_evidence_namespace(
        source_root, expected_tree_fingerprints=expected_tree_fingerprints
    )


def _assert_unchanged(before: Mapping[str, str], after_root: Path) -> None:
    after = sha256_paths(after_root, (Path(relative) for relative in before))
    if dict(before) != after:
        raise RuntimeError("Task 13 fixture input changed during exact suite")


def _assert_expected_hashes(actual: Mapping[str, str], expected: Mapping[str, str]) -> None:
    if dict(actual) != dict(expected):
        raise ValueError("fixed Task 13 evidence hash differs")


def run_exact_suite(
    *,
    repo: Path,
    v51_input_root: Path,
    old_v52_evidence_root: Path,
    chromium_executable: Path | None,
    chromium_sha256: str | None,
    chromium_bundle_sha256: str | None,
    python: str,
) -> int:
    repo = validate_repository_root(repo)
    v51_input_root = Path(v51_input_root).resolve(strict=True)
    old_v52_evidence_root = Path(old_v52_evidence_root).resolve(strict=True)
    tracked_hashes = sha256_paths(repo, ORIGINAL_PREFLIGHT_RELATIVES)
    _assert_expected_hashes(tracked_hashes, ORIGINAL_PREFLIGHT_SHA256)
    v51_hashes = sha256_paths(v51_input_root, V51_REQUIRED_FILES)
    old_hashes = sha256_paths(old_v52_evidence_root, OLD_EVIDENCE_RELATIVES)
    _assert_expected_hashes(old_hashes, OLD_EVIDENCE_SHA256)
    recovery_parent_hashes = validate_recovery_parent_inputs(old_v52_evidence_root)
    recovery_parent_trees = capture_recovery_parent_trees(old_v52_evidence_root)
    _assert_recovery_authority_tree_fingerprints(recovery_parent_trees)
    raw = _regular_file(old_v52_evidence_root / OLD_RAW_RELATIVE, "old raw evidence")
    if raw.stat().st_size != OLD_RAW_BYTE_COUNT:
        raise ValueError("old raw evidence byte count differs")

    with tempfile.TemporaryDirectory(prefix="task13-exact-") as directory:
        base = Path(directory).resolve(strict=True)
        chromium_runtime = provision_chromium_runtime(
            chromium_executable,
            chromium_sha256,
            chromium_bundle_sha256,
            base / "runtime",
        )
        design_root = clone_repo_at_head(repo, base / "design")
        _provision_approved_design_commit(design_root)
        _provision_v51_baseline_commit(design_root)
        source_root = clone_repo_at_head(repo, base / "source")
        copy_files_with_hashes(v51_input_root, design_root, V51_REQUIRED_FILES)
        design_input_hashes = sha256_paths(design_root, V51_REQUIRED_FILES)
        _write_fixture_preflight(design_root)
        fixture_preflight_hashes = sha256_paths(
            design_root, ORIGINAL_PREFLIGHT_RELATIVES
        )
        copied_recovery_parent_trees = copy_recovery_parent_trees(
            old_v52_evidence_root, source_root
        )
        if copied_recovery_parent_trees != recovery_parent_trees:
            raise RuntimeError("recovery parent copy fingerprint differs from source")
        _assert_recovery_authority_tree_fingerprints(copied_recovery_parent_trees)
        _assert_expected_hashes(
            sha256_paths(source_root, OLD_EVIDENCE_RELATIVES), OLD_EVIDENCE_SHA256
        )
        if validate_recovery_parent_inputs(source_root) != recovery_parent_hashes:
            raise RuntimeError("copied recovery parent authentication differs")
        if (source_root / OLD_RAW_RELATIVE).stat().st_size != OLD_RAW_BYTE_COUNT:
            raise ValueError("copied old raw evidence byte count differs")
        _assert_fixture_namespaces(design_root, source_root)

        try:
            completed = subprocess.run(
                build_child_command(python),
                cwd=design_root,
                env=build_child_environment(
                    design_root,
                    source_root,
                    chromium_runtime=chromium_runtime,
                ),
                check=False,
            )
        finally:
            validate_chromium_snapshot(chromium_runtime)
        _assert_unchanged(v51_hashes, v51_input_root)
        _assert_unchanged(v51_hashes, design_root)
        _assert_unchanged(design_input_hashes, design_root)
        _assert_unchanged(fixture_preflight_hashes, design_root)
        _assert_unchanged(old_hashes, old_v52_evidence_root)
        _assert_unchanged(old_hashes, source_root)
        if capture_recovery_parent_trees(old_v52_evidence_root) != recovery_parent_trees:
            raise RuntimeError("recovery parent source input changed during exact suite")
        if capture_recovery_parent_trees(source_root) != recovery_parent_trees:
            raise RuntimeError("copied recovery parent input changed during exact suite")
        _assert_recovery_authority_tree_fingerprints(
            capture_recovery_parent_trees(old_v52_evidence_root)
        )
        _assert_recovery_authority_tree_fingerprints(
            capture_recovery_parent_trees(source_root)
        )
        if validate_recovery_parent_inputs(old_v52_evidence_root) != recovery_parent_hashes:
            raise RuntimeError("recovery parent source authentication changed")
        if validate_recovery_parent_inputs(source_root) != recovery_parent_hashes:
            raise RuntimeError("copied recovery parent authentication changed")
        if (source_root / OLD_RAW_RELATIVE).stat().st_size != OLD_RAW_BYTE_COUNT:
            raise RuntimeError("copied old raw evidence changed during exact suite")
        _assert_fixture_namespaces(design_root, source_root)
        _assert_unchanged(tracked_hashes, repo)
        return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--v51-input-root", type=Path, required=True)
    parser.add_argument("--old-v52-evidence-root", type=Path, required=True)
    parser.add_argument("--chromium-executable", type=Path, required=True)
    parser.add_argument("--chromium-sha256", required=True)
    parser.add_argument("--chromium-bundle-sha256", required=True)
    parser.add_argument("--python", default=os.environ.get("PYTHON", "python3"))
    args = parser.parse_args(argv)
    return run_exact_suite(
        repo=args.repo,
        v51_input_root=args.v51_input_root,
        old_v52_evidence_root=args.old_v52_evidence_root,
        chromium_executable=args.chromium_executable,
        chromium_sha256=args.chromium_sha256,
        chromium_bundle_sha256=args.chromium_bundle_sha256,
        python=args.python,
    )


if __name__ == "__main__":
    raise SystemExit(main())
