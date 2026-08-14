"""Read-only no-follow verification for the Task 2 offline download set."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import argparse
from dataclasses import dataclass
from pathlib import Path


_COMMITTED_V1_LOCK = (
    Path(__file__).resolve().parents[1] / "config" / "r4_protected_python311.lock.json"
)


@dataclass(frozen=True)
class VerifiedArtifact:
    filename: str
    kind: str
    version: str
    url: str
    sha256: str
    size: int
    payload: bytes


@dataclass(frozen=True)
class VerifiedDownloadSet:
    """Single no-follow read of the V1 lock plus verified immutable artifacts."""

    lock: dict[str, object]
    lock_bytes: bytes
    lock_sha256: str
    artifacts: tuple[VerifiedArtifact, ...]


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _read_private(path: Path) -> tuple[bytes, os.stat_result]:
    if path.is_symlink():
        raise ValueError("protected input is a symlink")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("protected input is not a private regular file")
        chunks: list[bytes] = []
        while block := os.read(fd, 1024 * 1024):
            chunks.append(block)
        return b"".join(chunks), info
    finally:
        os.close(fd)


def decode_v1_lock_bytes(lock_bytes: bytes) -> dict[str, object]:
    """Decode an exact legacy lock without silently canonicalizing its reviewed bytes."""
    try:
        lock = json.loads(lock_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("lock is not JSON") from exc
    if not isinstance(lock, dict) or lock.get("kind") != "R4_PROTECTED_PYTHON311_LOCK_V1":
        raise ValueError("lock is not canonical V1 bytes")
    canonical = _canonical(lock)
    # Newly generated disposable locks use canonical bytes; the one approved production V1
    # lock remains pretty-printed and must be copied byte-for-byte, not normalized.
    if lock_bytes not in {canonical, canonical + b"\n"} and lock_bytes != _COMMITTED_V1_LOCK.read_bytes():
        raise ValueError("lock is not canonical V1 bytes")
    if set(lock) != {"kind", "python", "wheels"} or not isinstance(
        lock["python"], dict
    ) or not isinstance(lock["wheels"], list):
        raise ValueError("lock is not a closed V1 structure")
    python = lock["python"]
    if set(python) != {"version", "url", "filename", "sha256"}:
        raise ValueError("lock python artifact is not closed")
    for item in lock["wheels"]:
        if not isinstance(item, dict) or set(item) != {"url", "filename", "sha256"}:
            raise ValueError("lock wheel artifact is not closed")
        if not all(isinstance(item.get(key), str) for key in item):
            raise ValueError("lock artifact values are malformed")
    if not all(isinstance(python.get(key), str) for key in python):
        raise ValueError("lock python artifact values are malformed")
    return lock


def verify_download_set(lock_path: Path, downloads: Path) -> VerifiedDownloadSet:
    if downloads.is_symlink() or not downloads.is_dir():
        raise ValueError("downloads must be a directory")
    lock_bytes, lock_stat = _read_private(lock_path)
    lock = decode_v1_lock_bytes(lock_bytes)
    artifacts = [("python_archive", lock.get("python"))] + [
        ("wheel", value) for value in lock.get("wheels", [])
    ]
    if not artifacts or any(not isinstance(item, dict) for _, item in artifacts):
        raise ValueError("lock artifact set is malformed")
    names = [item["filename"] for _, item in artifacts]
    if (
        len(names) != len(set(names))
        or len({name.casefold() for name in names}) != len(names)
        or sorted(path.name for path in downloads.iterdir()) != sorted(names)
    ):
        raise ValueError("downloads do not exactly match the lock")
    result: list[VerifiedArtifact] = []
    for kind, item in artifacts:
        assert isinstance(item, dict)
        filename, claimed = item.get("filename"), item.get("sha256")
        if (
            not isinstance(filename, str)
            or "/" in filename
            or not isinstance(claimed, str)
        ):
            raise ValueError("lock artifact is malformed")
        path = downloads / filename
        payload, info = _read_private(path)
        if hashlib.sha256(payload).hexdigest() != claimed:
            raise ValueError("download digest mismatch")
        rebound = os.stat(path, follow_symlinks=False)
        if (rebound.st_dev, rebound.st_ino, rebound.st_nlink) != (
            info.st_dev,
            info.st_ino,
            1,
        ):
            raise ValueError("download rebound during verification")
        result.append(
            VerifiedArtifact(
                filename,
                kind,
                item.get("version", ""),
                item.get("url", ""),
                claimed,
                len(payload),
                payload,
            )
        )
    rebound_lock = os.stat(lock_path, follow_symlinks=False)
    if (rebound_lock.st_dev, rebound_lock.st_ino, rebound_lock.st_nlink) != (
        lock_stat.st_dev,
        lock_stat.st_ino,
        1,
    ):
        raise ValueError("lock rebound during verification")
    return VerifiedDownloadSet(
        lock=lock,
        lock_bytes=lock_bytes,
        lock_sha256=hashlib.sha256(lock_bytes).hexdigest(),
        artifacts=tuple(result),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--downloads", required=True, type=Path)
    args = parser.parse_args()
    rows = verify_download_set(args.lock, args.downloads).artifacts
    payload = [{"filename": row.filename, "kind": row.kind, "version": row.version, "url": row.url, "sha256": row.sha256, "size": row.size} for row in rows]
    print(_canonical(payload).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
