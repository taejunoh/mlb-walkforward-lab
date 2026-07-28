"""Closed authentication for the three recovery schedule parent authorities.

The historical trees are provenance only.  This module intentionally has no
caller-selectable path, hash, or parent-object parameters.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .v52_source_contracts import canonical_json_bytes


_SCHEDULE_SOURCE = "mlb_schedule_game_feed"
_SCHEDULE_ENDPOINT = "https://statsapi.mlb.com/api/v1/schedule"

_ORIGINAL_2019_QUERY_ID = (
    "11beeec110e975d6299c4a9a4b422c034e00962db8843dc65d28453e2967feb8"
)
_SUPERSEDING_2020_QUERY_ID = (
    "8cd76b83ef6709344bda2627e460510ac7312205e4898dc6e748a77032495816"
)
_SUPERSEDING_2021_QUERY_ID = (
    "91b1f546d01454c1503ad09941ac50d4fd84dca6539a75676bc8a7ab2ff8a5d2"
)


def _query_relative(namespace: str, query_id: str) -> Path:
    return (
        Path("data/v5_2_qd")
        / namespace
        / "raw/queries"
        / _SCHEDULE_SOURCE
        / query_id
        / "query.json"
    )


def _attempt_relative(namespace: str, query_id: str) -> Path:
    return _query_relative(namespace, query_id).parent / "attempts/000001.json"


def _quarantine_relative(namespace: str, query_id: str) -> Path:
    return (
        Path("data/v5_2_qd")
        / namespace
        / "raw/quarantine"
        / _SCHEDULE_SOURCE
        / query_id
        / "000001.json"
    )


def _raw_relative(namespace: str, query_id: str, raw_sha256: str) -> Path:
    return _quarantine_relative(namespace, query_id).parent / f"{raw_sha256}.raw"


# This is deliberately the only parent-path selection surface in the module.
PARENT_RELATIVES: Mapping[str, Mapping[str, Path]] = MappingProxyType(
    {
        "original_2019_565534": MappingProxyType(
            {
                "registration": _query_relative("historical", _ORIGINAL_2019_QUERY_ID),
                "quarantine": _quarantine_relative("historical", _ORIGINAL_2019_QUERY_ID),
                "raw": _raw_relative(
                    "historical",
                    _ORIGINAL_2019_QUERY_ID,
                    "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0",
                ),
            }
        ),
        "superseding_2020_630882": MappingProxyType(
            {
                "registration": _query_relative(
                    "historical_superseding_565534", _SUPERSEDING_2020_QUERY_ID
                ),
                "attempt": _attempt_relative(
                    "historical_superseding_565534", _SUPERSEDING_2020_QUERY_ID
                ),
                "quarantine": _quarantine_relative(
                    "historical_superseding_565534", _SUPERSEDING_2020_QUERY_ID
                ),
                "raw": _raw_relative(
                    "historical_superseding_565534",
                    _SUPERSEDING_2020_QUERY_ID,
                    "b085d068a09a4101a77a0f3c2fabe6b159fd8c9df564ee31994aa5d10271fb1b",
                ),
            }
        ),
        "superseding_2021_633224": MappingProxyType(
            {
                "registration": _query_relative(
                    "historical_superseding_565534", _SUPERSEDING_2021_QUERY_ID
                ),
                "attempt": _attempt_relative(
                    "historical_superseding_565534", _SUPERSEDING_2021_QUERY_ID
                ),
                "quarantine": _quarantine_relative(
                    "historical_superseding_565534", _SUPERSEDING_2021_QUERY_ID
                ),
                "raw": _raw_relative(
                    "historical_superseding_565534",
                    _SUPERSEDING_2021_QUERY_ID,
                    "84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17",
                ),
            }
        ),
    }
)


@dataclass(frozen=True)
class _ParentSpec:
    query_id: str
    registration_sha256: str
    registration_byte_count: int
    attempt_sha256: str | None
    attempt_byte_count: int | None
    quarantine_sha256: str
    quarantine_byte_count: int
    raw_sha256: str
    raw_byte_count: int
    year: int


_PARENT_SPECS: Mapping[str, _ParentSpec] = MappingProxyType(
    {
        "original_2019_565534": _ParentSpec(
            query_id=_ORIGINAL_2019_QUERY_ID,
            registration_sha256=(
                "5071511a96867ff6e16979d310de8ca4c597775dfda4cdad89906278400eaf95"
            ),
            registration_byte_count=675,
            attempt_sha256=None,
            attempt_byte_count=None,
            quarantine_sha256=(
                "a2f26dd297fc2f0de084c857e6deb575973f147e8c9712a724d82308a9a693f8"
            ),
            quarantine_byte_count=938,
            raw_sha256=(
                "6eeedd3877eeef18dae400dba8920dcde0cc79b3ab76b08316385868e89df1e0"
            ),
            raw_byte_count=21182105,
            year=2019,
        ),
        "superseding_2020_630882": _ParentSpec(
            query_id=_SUPERSEDING_2020_QUERY_ID,
            registration_sha256=(
                "e5af77c4750d9de564d6a7203564b30056a89627f70c868e7ff0a1753d674106"
            ),
            registration_byte_count=675,
            attempt_sha256=(
                "fe37c66d26cc435d1375432b4f31c4b21546f28e46648de6379eeb67b1c527e2"
            ),
            attempt_byte_count=662,
            quarantine_sha256=(
                "47bd466897b87b23a3c625737c0cae5aad1475491fdee93454489df9c38752cc"
            ),
            quarantine_byte_count=936,
            raw_sha256=(
                "b085d068a09a4101a77a0f3c2fabe6b159fd8c9df564ee31994aa5d10271fb1b"
            ),
            raw_byte_count=7898917,
            year=2020,
        ),
        "superseding_2021_633224": _ParentSpec(
            query_id=_SUPERSEDING_2021_QUERY_ID,
            registration_sha256=(
                "b915233fb3c3ea796330486490f3b90d6746ce82e1f03b30b9c964ba6569cfd3"
            ),
            registration_byte_count=675,
            attempt_sha256=(
                "b7d76d96191bc80c578965ec5242a3abeb44dade601a5ca3c76c30d4c45f1652"
            ),
            attempt_byte_count=664,
            quarantine_sha256=(
                "d3c9d9f197cab51d5ab509887002e4df3f300cb20c23a888120395e9b7042c36"
            ),
            quarantine_byte_count=938,
            raw_sha256=(
                "84f103b39485151f816726e6a6943ea2498b8ac769c1a27d9e8603a7ca2e4b17"
            ),
            raw_byte_count=21223271,
            year=2021,
        ),
    }
)


@dataclass(frozen=True)
class AuthenticatedParentFile:
    """An exact immutable file authenticated from a fixed parent authority."""

    relative: Path
    sha256: str
    byte_count: int
    device: int
    inode: int


@dataclass(frozen=True)
class RecoveryParentEvidence:
    """One closed parent object and its authenticated evidence chain."""

    name: str
    query_id: str
    parent_query_id: None
    registration: AuthenticatedParentFile
    attempt: AuthenticatedParentFile | None
    quarantine: AuthenticatedParentFile
    raw: AuthenticatedParentFile


@dataclass(frozen=True)
class RecoveryParentCatalog:
    """Immutable parent evidence, bound to the repository state at creation."""

    repository_root: Path
    parents: Mapping[str, RecoveryParentEvidence]

    def reauthenticate(self) -> RecoveryParentCatalog:
        rebound = build_recovery_parent_catalog(self.repository_root)
        if rebound != self:
            raise ValueError("recovery parent evidence changed during reauthentication")
        return rebound

    def read_json(self, parent_name: str, artifact: str) -> Mapping[str, object]:
        parent = self._parent(parent_name)
        files = {
            "registration": parent.registration,
            "quarantine": parent.quarantine,
        }
        if parent.attempt is not None:
            files["attempt"] = parent.attempt
        try:
            evidence = files[artifact]
        except KeyError as exc:
            raise ValueError("parent JSON artifact is not fixed") from exc
        self.reauthenticate()
        contents, identity = _read_regular_single_link(
            self.repository_root, evidence.relative, f"{parent_name} {artifact}"
        )
        if identity != (evidence.device, evidence.inode):
            raise ValueError("parent JSON artifact identity changed after authentication")
        _verify_file_contents(
            contents,
            evidence.sha256,
            evidence.byte_count,
            f"{parent_name} {artifact}",
        )
        self.reauthenticate()
        return _canonical_json_object(contents, f"{parent_name} {artifact}")

    def read_raw(self, parent_name: str) -> bytes:
        parent = self._parent(parent_name)
        self.reauthenticate()
        contents, identity = _read_regular_single_link(
            self.repository_root, parent.raw.relative, f"{parent_name} raw"
        )
        if identity != (parent.raw.device, parent.raw.inode):
            raise ValueError("parent raw identity changed after authentication")
        _verify_file_contents(
            contents, parent.raw.sha256, parent.raw.byte_count, f"{parent_name} raw"
        )
        self.reauthenticate()
        return contents

    def _parent(self, parent_name: str) -> RecoveryParentEvidence:
        try:
            return self.parents[parent_name]
        except KeyError as exc:
            raise ValueError("parent evidence name is not fixed") from exc


@dataclass(frozen=True)
class TreeFingerprint:
    """Canonical membership and content digest for one immutable evidence tree."""

    relative: Path
    file_count: int
    paths: tuple[str, ...]
    sha256: str


def _safe_relative_parts(relative: Path, label: str) -> tuple[str, ...]:
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError(f"{label} must be a repository-relative path")
    return relative.parts


def _repository_root(root: Path) -> Path:
    candidate, _root_fd, descriptors, bindings = _open_authenticated_root(root)
    try:
        _assert_directory_bindings(bindings)
    finally:
        _close_descriptors(descriptors)
    return candidate


_DirectoryBinding = tuple[int, str, int, tuple[int, int, int, int], str]


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _file_identity(node: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        node.st_dev,
        node.st_ino,
        node.st_mode,
        node.st_nlink,
        node.st_size,
    )


def _directory_identity(node: os.stat_result) -> tuple[int, int, int, int]:
    return (node.st_dev, node.st_ino, node.st_mode, node.st_nlink)


def _require_directory(node: os.stat_result, label: str) -> None:
    if stat.S_ISLNK(node.st_mode) or not stat.S_ISDIR(node.st_mode):
        raise ValueError(f"{label} must be a real directory")


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_directory_component(
    parent_fd: int, component: str, label: str
) -> tuple[int, _DirectoryBinding]:
    descriptor = -1
    try:
        linked_before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        _require_directory(linked_before, label)
        descriptor = os.open(component, _directory_flags(), dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        linked_after = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        _require_directory(opened, label)
        _require_directory(linked_after, label)
        if (
            _directory_identity(linked_before) != _directory_identity(linked_after)
            or _directory_identity(opened) != _directory_identity(linked_after)
        ):
            raise ValueError(f"{label} identity changed while opening")
        return descriptor, (
            parent_fd,
            component,
            descriptor,
            _directory_identity(opened),
            label,
        )
    except OSError as exc:
        raise ValueError(f"{label} cannot be opened safely") from exc
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _open_authenticated_root(
    root: Path,
) -> tuple[Path, int, list[int], list[_DirectoryBinding]]:
    candidate = Path(os.path.abspath(os.fspath(root)))
    descriptors: list[int] = []
    bindings: list[_DirectoryBinding] = []
    try:
        current = os.open(os.sep, _directory_flags())
        descriptors.append(current)
        _require_directory(os.fstat(current), "repository filesystem root")
        for component in candidate.parts[1:]:
            child, binding = _open_directory_component(
                current, component, "repository root ancestor"
            )
            descriptors.append(child)
            bindings.append(binding)
            current = child
        return candidate, current, descriptors, bindings
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _assert_directory_bindings(bindings: list[_DirectoryBinding]) -> None:
    for parent_fd, component, child_fd, expected, label in bindings:
        try:
            linked = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(child_fd)
        except OSError as exc:
            raise ValueError(f"{label} path binding changed") from exc
        _require_directory(linked, label)
        _require_directory(opened, label)
        if (
            _directory_identity(linked) != expected
            or _directory_identity(opened) != expected
        ):
            raise ValueError(f"{label} path binding changed")


def _require_directory_membership(
    root: Path,
    relative: Path,
    expected_names: frozenset[str],
    directory_names: frozenset[str],
    label: str,
) -> None:
    parts = _safe_relative_parts(relative, label)
    _root, directory_fd, descriptors, bindings = _open_authenticated_root(root)
    try:
        for component in parts:
            child, binding = _open_directory_component(
                directory_fd, component, f"{label} ancestor"
            )
            descriptors.append(child)
            bindings.append(binding)
            directory_fd = child
        names_before = set(os.listdir(directory_fd))
        if names_before != expected_names:
            raise ValueError(f"{label} membership differs")
        for name in expected_names:
            node = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if name in directory_names:
                _require_directory(node, f"{label} {name}")
            elif (
                stat.S_ISLNK(node.st_mode)
                or not stat.S_ISREG(node.st_mode)
                or node.st_nlink != 1
            ):
                raise ValueError(f"{label} {name} must be a regular single-link file")
        _assert_directory_bindings(bindings)
        if set(os.listdir(directory_fd)) != names_before:
            raise ValueError(f"{label} membership changed while being read")
    except OSError as exc:
        raise ValueError(f"{label} cannot be inspected safely") from exc
    finally:
        _close_descriptors(descriptors)


def _read_regular_single_link(
    root: Path, relative: Path, label: str
) -> tuple[bytes, tuple[int, int]]:
    parts = _safe_relative_parts(relative, label)
    _root, parent_fd, descriptors, bindings = _open_authenticated_root(root)
    descriptor = -1
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for component in parts[:-1]:
            child, binding = _open_directory_component(
                parent_fd, component, f"{label} ancestor"
            )
            descriptors.append(child)
            bindings.append(binding)
            parent_fd = child
        leaf = parts[-1]
        linked_before = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(linked_before.st_mode)
            or not stat.S_ISREG(linked_before.st_mode)
            or linked_before.st_nlink != 1
        ):
            raise ValueError(f"{label} must be a regular single-link file")
        descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or opened_before.st_nlink != 1
            or (opened_before.st_dev, opened_before.st_ino)
            != (linked_before.st_dev, linked_before.st_ino)
            or opened_before.st_size != linked_before.st_size
        ):
            raise ValueError(f"{label} identity differs before reading")
        remaining = opened_before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"{label} ended before its authenticated byte count")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ValueError(f"{label} grew while being read")
        opened_after = os.fstat(descriptor)
        linked_after = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(linked_before) != _file_identity(linked_after)
            or (opened_after.st_dev, opened_after.st_ino)
            != (linked_after.st_dev, linked_after.st_ino)
            or not stat.S_ISREG(opened_after.st_mode)
            or opened_after.st_nlink != 1
            or opened_after.st_size != linked_after.st_size
        ):
            raise ValueError(f"{label} identity changed while being read")
        _assert_directory_bindings(bindings)
        return b"".join(chunks), (linked_after.st_dev, linked_after.st_ino)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        _close_descriptors(descriptors)


def _verify_file_contents(
    contents: bytes, expected_sha256: str, expected_byte_count: int, label: str
) -> None:
    if len(contents) != expected_byte_count:
        raise ValueError(f"{label} byte count differs")
    if hashlib.sha256(contents).hexdigest() != expected_sha256:
        raise ValueError(f"{label} sha256 differs")


def _deep_freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _canonical_json_object(contents: bytes, label: str) -> Mapping[str, object]:
    try:
        value = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not UTF-8 JSON") from exc
    if (
        not isinstance(value, dict)
        or canonical_json_bytes(value) + b"\n" != contents
    ):
        raise ValueError(f"{label} is not canonical JSON")
    frozen = _deep_freeze(value)
    if not isinstance(frozen, Mapping):
        raise AssertionError("canonical JSON object did not freeze as a mapping")
    return frozen


def _artifact(
    root: Path,
    relative: Path,
    expected_sha256: str,
    expected_byte_count: int,
    label: str,
    *,
    json_required: bool,
) -> tuple[AuthenticatedParentFile, Mapping[str, object] | None]:
    contents, identity = _read_regular_single_link(root, relative, label)
    _verify_file_contents(contents, expected_sha256, expected_byte_count, label)
    payload = _canonical_json_object(contents, label) if json_required else None
    return (
        AuthenticatedParentFile(
            relative=relative,
            sha256=expected_sha256,
            byte_count=expected_byte_count,
            device=identity[0],
            inode=identity[1],
        ),
        payload,
    )


def _require_mapping_value(
    payload: Mapping[str, object], key: str, expected: object, label: str
) -> None:
    if payload.get(key) != expected:
        raise ValueError(f"{label} {key} differs")


def _authenticate_parent(
    root: Path, name: str, spec: _ParentSpec
) -> RecoveryParentEvidence:
    relatives = PARENT_RELATIVES[name]
    expected_artifacts = (
        {"registration", "quarantine", "raw"}
        if spec.attempt_sha256 is None
        else {"registration", "attempt", "quarantine", "raw"}
    )
    if set(relatives) != expected_artifacts:
        raise AssertionError("closed parent relative membership differs")
    query_directory = relatives["registration"].parent
    _require_directory_membership(
        root,
        query_directory,
        frozenset({".query.lock", "attempts", "objects", "query.json", "sidecars"}),
        frozenset({"attempts", "objects", "sidecars"}),
        f"{name} query directory",
    )
    _require_directory_membership(
        root,
        query_directory / "attempts",
        frozenset({"000001.json", "000001.reservation.json"}),
        frozenset(),
        f"{name} attempt directory",
    )
    _require_directory_membership(
        root,
        query_directory / "objects",
        frozenset(),
        frozenset(),
        f"{name} object directory",
    )
    _require_directory_membership(
        root,
        query_directory / "sidecars",
        frozenset(),
        frozenset(),
        f"{name} sidecar directory",
    )
    _require_directory_membership(
        root,
        relatives["quarantine"].parent,
        frozenset({"000001.json", relatives["raw"].name}),
        frozenset(),
        f"{name} quarantine directory",
    )
    registration, registration_payload = _artifact(
        root,
        relatives["registration"],
        spec.registration_sha256,
        spec.registration_byte_count,
        f"{name} registration",
        json_required=True,
    )
    quarantine, quarantine_payload = _artifact(
        root,
        relatives["quarantine"],
        spec.quarantine_sha256,
        spec.quarantine_byte_count,
        f"{name} quarantine",
        json_required=True,
    )
    raw, _raw_payload = _artifact(
        root,
        relatives["raw"],
        spec.raw_sha256,
        spec.raw_byte_count,
        f"{name} raw",
        json_required=False,
    )
    attempt: AuthenticatedParentFile | None = None
    attempt_payload: Mapping[str, object] | None = None
    if spec.attempt_sha256 is not None and spec.attempt_byte_count is not None:
        attempt, attempt_payload = _artifact(
            root,
            relatives["attempt"],
            spec.attempt_sha256,
            spec.attempt_byte_count,
            f"{name} attempt",
            json_required=True,
        )
    if registration_payload is None or quarantine_payload is None:
        raise AssertionError("closed JSON parent artifact was not decoded")
    _require_mapping_value(registration_payload, "source", _SCHEDULE_SOURCE, name)
    _require_mapping_value(registration_payload, "grain", "season_schedule_and_game", name)
    _require_mapping_value(registration_payload, "endpoint", _SCHEDULE_ENDPOINT, name)
    _require_mapping_value(registration_payload, "parent_query_id", None, name)
    requested_range = registration_payload.get("requested_range")
    if not isinstance(requested_range, Mapping) or requested_range.get("year") != spec.year:
        raise ValueError(f"{name} registration requested range differs")
    _require_mapping_value(quarantine_payload, "query_id", spec.query_id, name)
    _require_mapping_value(quarantine_payload, "raw_sha256", spec.raw_sha256, name)
    _require_mapping_value(quarantine_payload, "body_sha256", spec.raw_sha256, name)
    _require_mapping_value(quarantine_payload, "byte_count", spec.raw_byte_count, name)
    if attempt_payload is not None:
        _require_mapping_value(attempt_payload, "query_id", spec.query_id, name)
        _require_mapping_value(attempt_payload, "body_sha256", spec.raw_sha256, name)
        _require_mapping_value(attempt_payload, "byte_count", spec.raw_byte_count, name)
    return RecoveryParentEvidence(
        name=name,
        query_id=spec.query_id,
        parent_query_id=None,
        registration=registration,
        attempt=attempt,
        quarantine=quarantine,
        raw=raw,
    )


def build_recovery_parent_catalog(root: Path) -> RecoveryParentCatalog:
    """Authenticate the exact three historical parent objects under ``root``."""
    repository_root = _repository_root(root)
    if tuple(PARENT_RELATIVES) != (
        "original_2019_565534",
        "superseding_2020_630882",
        "superseding_2021_633224",
    ) or tuple(_PARENT_SPECS) != tuple(PARENT_RELATIVES):
        raise AssertionError("closed recovery parent catalog membership differs")
    parents = {
        name: _authenticate_parent(repository_root, name, _PARENT_SPECS[name])
        for name in PARENT_RELATIVES
    }
    rebound = {
        name: _authenticate_parent(repository_root, name, _PARENT_SPECS[name])
        for name in PARENT_RELATIVES
    }
    if rebound != parents:
        raise ValueError("recovery parent evidence changed during catalog authentication")
    return RecoveryParentCatalog(repository_root, MappingProxyType(parents))


def _tree_file_records(root: Path, tree: Path, relative: Path) -> list[dict[str, str]]:
    node = tree.lstat()
    if stat.S_ISLNK(node.st_mode) or not stat.S_ISDIR(node.st_mode):
        raise ValueError("fingerprint tree must be a real directory")
    records: list[dict[str, str]] = []
    with os.scandir(tree) as entries:
        children = sorted(entries, key=lambda item: item.name)
    for child in children:
        child_path = tree / child.name
        child_node = child_path.lstat()
        child_relative = relative / child.name
        if stat.S_ISLNK(child_node.st_mode):
            raise ValueError("fingerprint tree contains a symlink")
        if stat.S_ISDIR(child_node.st_mode):
            records.extend(_tree_file_records(root, child_path, child_relative))
            continue
        if not stat.S_ISREG(child_node.st_mode) or child_node.st_nlink != 1:
            raise ValueError("fingerprint tree contains a non-single-link file")
        contents, _identity = _read_regular_single_link(
            root, child_relative, "fingerprint tree file"
        )
        records.append(
            {
                "path": child_relative.as_posix(),
                "sha256": hashlib.sha256(contents).hexdigest(),
            }
        )
    return records


def capture_tree_fingerprint(root: Path, relative: Path) -> TreeFingerprint:
    """Return a canonical file-count/path/content digest for a fixed tree."""
    repository_root = _repository_root(root)
    _safe_relative_parts(relative, "fingerprint tree")
    tree = repository_root / relative
    records = _tree_file_records(repository_root, tree, relative)
    records.sort(key=lambda record: record["path"])
    digest = hashlib.sha256(
        canonical_json_bytes({"file_count": len(records), "files": records})
    ).hexdigest()
    return TreeFingerprint(
        relative=relative,
        file_count=len(records),
        paths=tuple(record["path"] for record in records),
        sha256=digest,
    )


__all__ = [
    "AuthenticatedParentFile",
    "PARENT_RELATIVES",
    "RecoveryParentCatalog",
    "RecoveryParentEvidence",
    "TreeFingerprint",
    "build_recovery_parent_catalog",
    "capture_tree_fingerprint",
]
