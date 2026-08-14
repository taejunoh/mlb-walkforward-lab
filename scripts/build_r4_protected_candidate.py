#!/usr/bin/env python3
"""Build the sealed V2 input bundle; this program never materializes a runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mlb_backtest.secure_fs import rename_exclusive_at
from r4_protected_fixed_git import GitRunner, run_fixed_git, run_recorded_git
from verify_r4_protected_inputs import verify_download_set

NAMESPACE = "org.mlb-walkforward.r4.protected-execution-authority/v1"
AUTHORITY_ID = "r4-disjoint-custody-v2-execution-v1"
CANONICAL_REQUIRED_BLOB_PATHS = tuple(
    sorted(
        {
            "docs/superpowers/plans/2026-08-08-v5-2-r4-disjoint-custody-recovery.md",
            "docs/superpowers/specs/2026-08-08-r4-protected-execution-authority-design.md",
            "docs/superpowers/specs/2026-08-08-v5-2-r4-disjoint-custody-recovery-design.md",
            "mlb_backtest/v52_r4_disjoint_execution.py",
            "mlb_backtest/v52_r4_disjoint_issuer.py",
            "mlb_backtest/v52_r4_disjoint_policy.py",
        }
    )
)
APPROVED_SOURCE_PATHS = CANONICAL_REQUIRED_BLOB_PATHS
assert CANONICAL_REQUIRED_BLOB_PATHS == tuple(sorted(CANONICAL_REQUIRED_BLOB_PATHS))


@dataclass(frozen=True)
class InputBundle:
    root: Path
    manifest: dict[str, Any]
    required_blobs: list[dict[str, str]]


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _domain_sha(domain: str, value: Any) -> str:
    return _sha(domain.encode("ascii") + _canonical(value))


def _safe_path(path: str) -> tuple[str, ...]:
    if not path or path.startswith("/") or "\\" in path or not path.isascii():
        raise ValueError("unsafe Git path")
    parts = tuple(path.split("/"))
    if any(not item or item in {".", "..", ".git"} for item in parts):
        raise ValueError("unsafe Git path")
    return parts


def _git(source: Path, arguments: Sequence[str], runner: GitRunner) -> bytes:
    return run_fixed_git(source, arguments, runner=runner)


def _parse_tree(data: bytes) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    folds: set[str] = set()
    for record in data.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split(" ")
            path = raw_path.decode("ascii")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("malformed Git tree") from exc
        _safe_path(path)
        if (
            path in seen
            or path.casefold() in folds
            or mode not in {"100644", "100755"}
            or kind != "blob"
            or len(blob) != 40
        ):
            raise ValueError("non-regular or duplicate Git entry")
        seen.add(path)
        folds.add(path.casefold())
        rows.append((path, mode, blob))
    if not rows:
        raise ValueError("empty Git checkout")
    return sorted(rows)


def validate_required_blob_rows(rows: list[dict[str, str]]) -> None:
    paths = tuple(row.get("path") for row in rows)
    if paths != CANONICAL_REQUIRED_BLOB_PATHS:
        raise ValueError("required_blobs must use the canonical sorted six-path order")
    if any(
        set(row) != {"path", "blob", "sha256"}
        or len(row["blob"]) != 40
        or len(row["sha256"]) != 64
        for row in rows
    ):
        raise ValueError("required_blobs are malformed")


def _git_evidence(
    source: Path, runner: GitRunner
) -> tuple[str, str, list[tuple[str, str, str]]]:
    if source.is_symlink():
        raise ValueError("source must not be a symlink")
    source = source.resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise ValueError("source must be a canonical directory")
    if not _git(source, ("symbolic-ref", "-q", "HEAD"), runner).strip():
        raise ValueError("source has detached HEAD")
    if _git(source, ("status", "--porcelain=v1", "--ignored", "-z"), runner):
        raise ValueError("source is not clean")
    head = (
        _git(source, ("rev-parse", "--verify", "HEAD^{commit}"), runner)
        .decode("ascii")
        .strip()
    )
    tree = (
        _git(source, ("rev-parse", "--verify", "HEAD^{tree}"), runner)
        .decode("ascii")
        .strip()
    )
    if len(head) != 40 or len(tree) != 40:
        raise ValueError("unsupported Git object id")
    return (
        head,
        tree,
        _parse_tree(
            _git(source, ("ls-tree", "-r", "-z", "--full-tree", "HEAD"), runner)
        ),
    )


def _entry_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in [root, *sorted(root.rglob("*"), key=lambda item: item.as_posix())]:
        details = os.lstat(path)
        relative = path.relative_to(root.parent).as_posix()
        if stat.S_ISDIR(details.st_mode):
            children = []
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                info = os.lstat(child)
                children.append(
                    {
                        "name": child.name,
                        "type": "directory"
                        if stat.S_ISDIR(info.st_mode)
                        else "regular",
                        "mode": "0555"
                        if stat.S_ISDIR(info.st_mode)
                        or stat.S_IMODE(info.st_mode) == 0o555
                        else "0444",
                        "size": 0 if stat.S_ISDIR(info.st_mode) else info.st_size,
                        "sha256": "",
                    }
                )
            rows.append(
                {
                    "path": relative,
                    "type": "directory",
                    "mode": "0555",
                    "size": 0,
                    "sha256": _domain_sha("R4DIR\0", children),
                }
            )
        elif stat.S_ISREG(details.st_mode) and details.st_nlink == 1:
            payload = path.read_bytes()
            rows.append(
                {
                    "path": relative,
                    "type": "regular",
                    "mode": f"{stat.S_IMODE(details.st_mode):04o}",
                    "size": len(payload),
                    "sha256": _sha(payload),
                }
            )
        else:
            raise ValueError("checkout contains unsafe entry")
    # Directory child hashes must be the finished child metadata, not placeholders.
    for row in sorted(
        (row for row in rows if row["type"] == "directory"),
        key=lambda row: row["path"].count("/"),
        reverse=True,
    ):
        prefix = row["path"] + "/"
        children = []
        for candidate in rows:
            if (
                candidate["path"].startswith(prefix)
                and "/" not in candidate["path"][len(prefix) :]
            ):
                children.append(
                    {
                        "name": candidate["path"][len(prefix) :],
                        "type": candidate["type"],
                        "mode": candidate["mode"],
                        "size": candidate["size"],
                        "sha256": candidate["sha256"],
                    }
                )
        row["sha256"] = _domain_sha("R4DIR\0", children)
    return sorted(rows, key=lambda row: row["path"])


def _read_canonical(path: Path) -> bytes:
    value = path.read_bytes()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("input document is not JSON") from exc
    if value not in {_canonical(parsed), _canonical(parsed) + b"\n"}:
        raise ValueError("input document is not canonical JSON")
    return value


def _v2_document_digest(value: bytes, *, kind: str) -> str:
    """Return the V2 self-digest, which deliberately excludes the `sha256` member."""
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("input document is not JSON") from exc
    if not isinstance(document, dict) or document.get("kind") != kind:
        raise ValueError("input document has the wrong V2 kind")
    digest = _sha(_canonical({key: item for key, item in document.items() if key != "sha256"}))
    if document.get("sha256") != digest:
        raise ValueError("input document self digest mismatch")
    return digest


def _directory_identity(path: Path) -> tuple[int, int]:
    """Return the no-follow identity of a staging directory we created ourselves."""
    details = os.lstat(path)
    if not stat.S_ISDIR(details.st_mode):
        raise ValueError("staging path is not a directory")
    return details.st_dev, details.st_ino


def _same_identity(details: os.stat_result, identity: tuple[int, int]) -> bool:
    return (details.st_dev, details.st_ino) == identity


def _remove_owned_children(descriptor: int) -> None:
    """Unlink only entries reachable through the held no-follow directory descriptor."""
    # The bundle is deliberately sealed read-only before publication.  This descriptor is
    # still the captured, private staging object, so temporarily reopening its own mode is
    # safe and is necessary to unlink its children after a failed publication race.
    os.fchmod(descriptor, 0o700)
    for name in os.listdir(descriptor):
        details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            try:
                if not _same_identity(os.fstat(child), (details.st_dev, details.st_ino)):
                    continue
                _remove_owned_children(child)
            finally:
                os.close(child)
            rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _same_identity(rebound, (details.st_dev, details.st_ino)):
                os.rmdir(name, dir_fd=descriptor)
        else:
            # Symlinks, regular files, and all other non-directories are unlinked as
            # directory entries, never traversed.
            rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if _same_identity(rebound, (details.st_dev, details.st_ino)):
                os.unlink(name, dir_fd=descriptor)


def _remove_owned_tree(path: Path, identity: tuple[int, int]) -> None:
    """Best-effort cleanup of a captured staging directory, never a name replacement.

    A failed build must not recursively follow a staging name that another actor has
    replaced.  Every traversal is descriptor-relative; if the root no longer has its
    captured device/inode pair, it is intentionally retained for operator inspection.
    """
    try:
        if not _same_identity(os.lstat(path), identity):
            return
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if not _same_identity(os.fstat(root), identity):
                    return
                _remove_owned_children(root)
            finally:
                os.close(root)
            rebound = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _same_identity(rebound, identity):
                os.rmdir(path.name, dir_fd=parent)
        finally:
            os.close(parent)
    except FileNotFoundError:
        return


def build_input_bundle(
    source: Path,
    downloads: Path,
    lock_path: Path,
    policy_path: Path,
    output: Path,
    *,
    runner: GitRunner = run_recorded_git,
) -> InputBundle:
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        raise ValueError("bundle output must be a fresh absolute path")
    policy_bytes = _read_canonical(policy_path)
    policy_digest = _v2_document_digest(
        policy_bytes, kind="R4_PRIVILEGED_MATERIALIZATION_POLICY_V2"
    )
    verified_inputs = verify_download_set(lock_path, downloads)
    lock_bytes = verified_inputs.lock_bytes
    artifacts = verified_inputs.artifacts
    head, tree, records = _git_evidence(source, runner)
    by_path = {path: (mode, blob) for path, mode, blob in records}
    if set(CANONICAL_REQUIRED_BLOB_PATHS) - set(by_path):
        raise ValueError("source is missing a canonical required path")
    staging = Path(tempfile.mkdtemp(prefix=".r4-input-bundle-", dir=output.parent))
    staging_identity = _directory_identity(staging)
    published = False
    try:
        checkout = staging / "checkout"
        checkout.mkdir()
        required: list[dict[str, str]] = []
        for path, mode, blob in records:
            payload = _git(source, ("cat-file", "blob", blob), runner)
            target = checkout.joinpath(*_safe_path(path))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            os.chmod(target, 0o555 if mode == "100755" else 0o444)
            if path in CANONICAL_REQUIRED_BLOB_PATHS:
                required.append({"path": path, "blob": blob, "sha256": _sha(payload)})
        for directory in sorted(
            [checkout, *[item for item in checkout.rglob("*") if item.is_dir()]],
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            os.chmod(directory, 0o555)
        required.sort(key=lambda row: row["path"])
        validate_required_blob_rows(required)
        artifact_dir = staging / "artifacts"
        artifact_dir.mkdir()
        artifact_rows = []
        for artifact in artifacts:
            (artifact_dir / artifact.filename).write_bytes(artifact.payload)
            os.chmod(artifact_dir / artifact.filename, 0o444)
            artifact_rows.append(
                {
                    "path": f"artifacts/{artifact.filename}",
                    "kind": artifact.kind,
                    "filename": artifact.filename,
                    "version": artifact.version,
                    "url": artifact.url,
                    "sha256": artifact.sha256,
                    "size": artifact.size,
                }
            )
        os.chmod(artifact_dir, 0o555)
        (staging / "r4_protected_python311.lock.json").write_bytes(lock_bytes)
        (staging / "materialization-policy.json").write_bytes(policy_bytes)
        os.chmod(staging / "r4_protected_python311.lock.json", 0o444)
        os.chmod(staging / "materialization-policy.json", 0o444)
        manifest = {
            "kind": "R4_RUNTIME_INPUT_BUNDLE_V2",
            "namespace": NAMESPACE,
            "authority_id": AUTHORITY_ID,
            "lock_path": "r4_protected_python311.lock.json",
            "lock_sha256": verified_inputs.lock_sha256,
            "materialization_policy_path": "materialization-policy.json",
            "git": {"head": head, "tree": tree, "required_blobs": required},
            "artifacts": artifact_rows,
            "checkout_entries": _entry_rows(checkout),
            "materialization_policy_sha256": policy_digest,
            "sha256": "",
        }
        manifest["sha256"] = _sha(
            _canonical(
                {key: value for key, value in manifest.items() if key != "sha256"}
            )
        )
        (staging / "R4_RUNTIME_INPUT_BUNDLE_V2.json").write_bytes(
            _canonical(manifest) + b"\n"
        )
        os.chmod(staging / "R4_RUNTIME_INPUT_BUNDLE_V2.json", 0o444)
        os.chmod(staging, 0o555)
        parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            rename_exclusive_at(staging.name, output.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        published = True
        return InputBundle(output, manifest, required)
    finally:
        if not published:
            _remove_owned_tree(staging, staging_identity)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--downloads", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    build_input_bundle(
        args.source,
        args.downloads,
        args.lock,
        args.policy,
        args.output,
        runner=run_recorded_git,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
