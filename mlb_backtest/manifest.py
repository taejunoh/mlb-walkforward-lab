import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
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
    return json.dumps(value, sort_keys=True, indent=2).encode("utf-8")


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
        folder = self.root / source
        data_path = folder / f"{digest}.raw"
        manifest_path = folder / f"{digest}.manifest.json"

        folder.mkdir(parents=True, exist_ok=True)
        if data_path.exists() and data_path.read_bytes() != payload:
            raise RuntimeError("immutable raw artifact hash collision")

        existing_manifest = None
        if manifest_path.exists():
            existing_manifest = self._read_manifest(manifest_path)
            expected = {
                "source": source,
                "query": normalized_query,
                "sha256": digest,
                "row_count": row_count,
                "schema_version": _SCHEMA_VERSION,
            }
            if any(existing_manifest.get(key) != value for key, value in expected.items()):
                raise RuntimeError("immutable raw manifest metadata mismatch")

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
            _atomic_write_once(manifest_path, _json_bytes(manifest))
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

    @staticmethod
    def _read_manifest(path: Path) -> dict:
        content = path.read_bytes()
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("immutable raw manifest is invalid") from exc
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
            raise RuntimeError("immutable raw manifest fields mismatch")
        retrieved_at = manifest.get("retrieved_at_utc")
        if not isinstance(retrieved_at, str):
            raise RuntimeError("immutable raw manifest timestamp is invalid")
        try:
            parsed_retrieved_at = datetime.fromisoformat(retrieved_at)
        except ValueError as exc:
            raise RuntimeError("immutable raw manifest timestamp is invalid") from exc
        if parsed_retrieved_at.tzinfo is None:
            raise RuntimeError("immutable raw manifest timestamp is invalid")
        if content != _json_bytes(manifest):
            raise RuntimeError("immutable raw manifest bytes differ")
        return manifest
