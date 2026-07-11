import fcntl
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import RawArtifact


_SCHEMA_VERSION = 1
_MANIFEST_FIELDS = {
    "source",
    "query",
    "sha256",
    "retrieved_at_utc",
    "row_count",
    "schema_version",
}


def _canonical_query(query: dict) -> dict:
    try:
        encoded = json.dumps(
            query,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        normalized = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ValueError("query must contain JSON-compatible values") from exc
    return normalized


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8")


def _seal_bytes(content: bytes) -> bytes:
    return f"{hashlib.sha256(content).hexdigest()}\n".encode("utf-8")


def _atomic_write_once(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"immutable artifact differs: {path.name}")
        return

    fd, temporary_name = tempfile.mkstemp(dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if path.read_bytes() != content:
                raise RuntimeError(f"immutable artifact differs: {path.name}")
        else:
            os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class RawStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, source: str, query: dict, payload: bytes) -> RawArtifact:
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a nonempty path segment")
        source_path = Path(source)
        if (
            source_path.is_absolute()
            or source in {".", ".."}
            or "/" in source
            or "\\" in source
        ):
            raise ValueError("source must be a single path segment")
        if not isinstance(query, dict):
            raise ValueError("query must be a dict")
        if not isinstance(payload, bytes):
            raise ValueError("payload must be bytes")

        normalized_query = _canonical_query(query)
        digest = hashlib.sha256(payload).hexdigest()
        row_count = sum(bool(line.strip()) for line in payload.splitlines())
        resolved_root = self._prepare_root()
        lock_path = self.root / f".{source}.lock"
        with self._open_lock(lock_path, resolved_root) as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                self._validate_open_lock(
                    lock_handle.fileno(), lock_path, resolved_root
                )
                if self.root.is_symlink() or self.root.resolve() != resolved_root:
                    raise ValueError("raw root changed while acquiring lock")
                return self._put_locked(
                    resolved_root,
                    source,
                    normalized_query,
                    payload,
                    digest,
                    row_count,
                )
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _open_lock(cls, lock_path: Path, resolved_root: Path):
        if lock_path.is_symlink():
            raise ValueError("raw lock path cannot be a symlink")
        if lock_path.exists() and not lock_path.is_file():
            raise ValueError("raw lock path must be a regular file")

        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise ValueError("raw lock path cannot be opened safely") from exc

        try:
            cls._validate_open_lock(descriptor, lock_path, resolved_root)
            return os.fdopen(descriptor, "r+", encoding="utf-8")
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _validate_open_lock(
        descriptor: int, lock_path: Path, resolved_root: Path
    ) -> None:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("raw lock descriptor must be a regular file")
        try:
            linked = os.stat(lock_path, follow_symlinks=False)
            resolved_lock = lock_path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeError("raw lock path changed after opening") from exc
        if not stat.S_ISREG(linked.st_mode):
            raise ValueError("raw lock path must be a regular file")
        if (linked.st_dev, linked.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("raw lock path changed after opening")
        if resolved_lock.parent != resolved_root:
            raise ValueError("raw lock path must be directly under raw root")

    def _put_locked(
        self,
        resolved_root: Path,
        source: str,
        normalized_query: dict,
        payload: bytes,
        digest: str,
        row_count: int,
    ) -> RawArtifact:
        folder = self.root / source
        if folder.is_symlink():
            raise ValueError("source folder cannot be a symlink")
        folder.mkdir(exist_ok=True)
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError("source folder must be a real directory")
        resolved_folder = folder.resolve(strict=True)
        if resolved_folder.parent != resolved_root:
            raise ValueError("source folder must be directly under raw root")

        data_path = folder / f"{digest}.raw"
        manifest_path = folder / f"{digest}.manifest.json"
        seal_path = folder / f"{digest}.manifest.sha256"
        if data_path.exists() and data_path.read_bytes() != payload:
            raise RuntimeError("immutable raw artifact hash collision")

        existing_manifest = None
        if manifest_path.exists():
            if not seal_path.is_file():
                raise RuntimeError("immutable raw manifest integrity seal is missing")
            manifest_content = manifest_path.read_bytes()
            if seal_path.read_bytes() != _seal_bytes(manifest_content):
                raise RuntimeError("immutable raw manifest integrity mismatch")
            existing_manifest = self._read_manifest(manifest_content)
            expected = {
                "source": source,
                "query": normalized_query,
                "sha256": digest,
                "row_count": row_count,
                "schema_version": _SCHEMA_VERSION,
            }
            if any(existing_manifest.get(key) != value for key, value in expected.items()):
                raise RuntimeError("immutable raw manifest metadata mismatch")
        elif seal_path.exists():
            raise RuntimeError("immutable raw manifest integrity seal is orphaned")

        _atomic_write_once(data_path, payload)
        if existing_manifest is None:
            manifest = {
                "source": source,
                "query": normalized_query,
                "sha256": digest,
                "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "row_count": row_count,
                "schema_version": _SCHEMA_VERSION,
            }
            manifest_content = _json_bytes(manifest)
            _atomic_write_once(manifest_path, manifest_content)
            _atomic_write_once(seal_path, _seal_bytes(manifest_content))
        else:
            manifest = existing_manifest

        return RawArtifact(
            source,
            digest,
            data_path,
            manifest_path,
            manifest["retrieved_at_utc"],
            manifest["row_count"],
        )

    def _prepare_root(self) -> Path:
        root = self.root
        if root.is_symlink():
            raise ValueError("raw root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ValueError("raw root must be a real directory")
        return root.resolve(strict=True)

    @staticmethod
    def _read_manifest(content: bytes) -> dict:
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("immutable raw manifest is invalid") from exc
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
            raise RuntimeError("immutable raw manifest fields mismatch")
        if type(manifest["row_count"]) is not int:
            raise RuntimeError("immutable raw manifest row_count must be an int")
        if type(manifest["schema_version"]) is not int:
            raise RuntimeError("immutable raw manifest schema_version must be an int")
        retrieved_at = manifest.get("retrieved_at_utc")
        if not isinstance(retrieved_at, str):
            raise RuntimeError("immutable raw manifest timestamp is invalid")
        try:
            parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
        except ValueError as exc:
            raise RuntimeError("immutable raw manifest timestamp is invalid") from exc
        if parsed_retrieved_at.utcoffset() != timedelta(0):
            raise RuntimeError("immutable raw manifest timestamp is invalid")
        if content != _json_bytes(manifest):
            raise RuntimeError("immutable raw manifest bytes differ")
        return manifest
